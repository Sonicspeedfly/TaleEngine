"""
Иерархическая пакетная память: чистое ядро (только stdlib).

Зачем модуль. Прежняя фоновая сводка Horae писала свободный пересказ, делала
не больше шести кусков за ход и только после ответа ассистента, а
_adopt_summary резал готовую сводку слайсом [:6000] символов — хвостовые
разделы (списки, реестр) пропадали молча, и модель «забывала» плейлисты,
атрибуты персонажей и договорённости из начала чата.

Здесь то же сжатие устроено как свёртка State_N = merge(State_{N-1}, Block_N):
история режется на непересекающиеся пакеты, каждый пакет вливается в
МАСТЕР-СНИМОК строгой схемы из четырёх разделов, ответ модели проверяется
(обёртка, разделы, «сдувание»), а потерянные записи разделов 2–4 страж
детерминированно возвращает из предыдущего снимка.

Ядро не знает ни про БД, ни про FastAPI, ни про litellm: сообщения приходят
готовыми MemoryMessage (или через BatchSource), модель — колбэком, а паузы,
повторы и цикл свёртки держит HierarchicalMemoryManager.
Поэтому весь контракт проверяется тестами без сети и без базы
(tests/test_hierarchical_memory.py), а backend/memory_service.py — лишь
адаптер к хранилищу.
"""
import asyncio
import re
import time
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Awaitable, Callable, Protocol, Sequence, runtime_checkable

# ============================================================================
# Константы схемы снимка
# ============================================================================

# Метка схемы в HoraeEntry.meta["schema"]. Формат указателя (meta.v =
# SUMMARY_FORMAT = 2) не меняется: новый сводчик видит всё, что учитывает, и
# указателю по-прежнему можно верить. Схема помечается отдельно, чтобы UI
# отличал старый свободный пересказ от снимка и предлагал пересборку.
SNAPSHOT_SCHEMA = "hms-1"

SEC_CHRONICLE = "ХРОНИКА И СОБЫТИЙНЫЙ КАРКАС"
SEC_CHARACTERS = "АКТИВНЫЕ ПЕРСОНАЖИ И ИХ СТАТУСЫ"
SEC_REGISTRY = "ФАКТОЛОГИЧЕСКИЙ РЕЕСТР И ЛОР"
SEC_LISTS = "СПИСКИ И МЕДИА-АНКОРЫ"
# Канонический порядок: в нём разделы рендерятся и в нём их ждёт промпт.
SECTIONS = (SEC_CHRONICLE, SEC_CHARACTERS, SEC_REGISTRY, SEC_LISTS)
# Хронику страж не охраняет: её законно сжимает compact() в арки. Остальные
# разделы — «справочник», из которого ничего не должно исчезать.
GUARDED_SECTIONS = (SEC_CHARACTERS, SEC_REGISTRY, SEC_LISTS)

# Что модель видит в [Текущая память], когда снимка ещё нет.
EMPTY_STATE = "(пока пусто)"
# Обёртка ответа. Нужна ради одного: отличить полный ответ от обрезанного
# лимитом вывода. Без закрывающего тега снимок нельзя принимать — в нём нет
# хвостовых разделов — тех самых списков и реестра, что раньше терялись молча.
ENVELOPE_OPEN = "<master_state>"
ENVELOPE_CLOSE = "</master_state>"
# Маркер вырезанной середины длинного сообщения. Текст фиксирован: на него
# опираются тесты, и модель по нему понимает, что это не обрыв реплики.
LONG_MESSAGE_MARK = "\n[…середина длинного сообщения пропущена…]\n"

# Сколько последних сообщений идёт в модель дословно (Tier 3). В 2.5.0 поднято
# с 20 до 50: всё, что старше окна, модель видит только через мастер-снимок.
# horae_recall.DEFAULT_WINDOW и клиент берут этот же дефолт — держите равными.
DEFAULT_WINDOW = 50
# Граница окна двигается ступенями, а не на каждом сообщении. Иначе начало
# истории сдвигалось бы каждый ход, и кэш промпта провайдера (скидка 75–90% на
# вход) не попадал бы никогда. Дословно модель видит от window до
# window + WINDOW_STEP - 1 сообщений.
WINDOW_STEP = 4
# Порог «неправдоподобно длинного» снимка. При бюджете 12 000 токенов нормальный
# снимок — десятки тысяч символов; 200 000 — скорее зацикленная генерация.
# Это нижняя граница: порог растёт с бюджетом (_max_snapshot_chars) — русский
# текст около 1,9 символа на токен, и при бюджете больше ~100 тыс. токенов
# снимок В ПРЕДЕЛАХ бюджета отвергался бы как «неправдоподобный».
MAX_SNAPSHOT_CHARS = 200_000
# Сколько символов на токен бюджета допускает порог — с запасом вчетверо
# против русского текста (≈ 1,9 символа на токен).
_SNAPSHOT_CHARS_PER_TOKEN = 8
# Нижняя граница бюджета пакета (MemoryConfig.batch_max_chars). Шапка строки
# «[#id · время · автор]» и маркер вырезанной середины вместе занимают около
# сотни символов, а MEMORY_BATCH_CHARS в настройках снизу не ограничен: при
# бюджете в пару сотен символов от реплики не осталось бы ничего.
_MIN_BATCH_CHARS = 2000
# Бюджет пакета по умолчанию (MemoryConfig.batch_max_chars не задан или 0).
_DEFAULT_BATCH_CHARS = 80_000


# ============================================================================
# Данные
# ============================================================================
@dataclass(frozen=True)
class MemoryMessage:
    """Сообщение истории в том виде, в каком его видит ядро."""
    id: int
    role: str                          # user | assistant | system
    speaker: str                       # уже разрешённое имя автора
    text: str                          # активный свайп (Message.content)
    created_at: datetime | None = None   # уже в поясе чата (или naive UTC)
    attachments: tuple[dict, ...] = ()    # лёгкая мета вложений: {type, name, mime}


@dataclass(frozen=True)
class Batch:
    """
    Пакет подряд идущих сообщений для одного слияния.

    messages — все ПОГЛОЩЁННЫЕ сообщения, включая пустые (их строка — ""): по
    last_id двигается указатель «учтено до», и пустое сообщение, не попавшее в
    пакет, навсегда осталось бы «неучтённым». Поэтому transcript может быть
    пустым, если в пакет попали одни пустые сообщения.
    """
    messages: tuple[MemoryMessage, ...]
    transcript: str                   # нормализованные строки через "\n\n"
    first_id: int
    last_id: int


@dataclass
class MemoryConfig:
    """
    Параметры менеджера.

    batch_max_chars: None, 0, пустое или нечисловое значение — «по умолчанию»
    (_DEFAULT_BATCH_CHARS = 80 000), а не «без лимита» и не 2000; затем
    нижняя граница _MIN_BATCH_CHARS. ПОЧЕМУ: сервис берёт значение из
    настроек (MEMORY_BATCH_CHARS), где его может не оказаться, — None раньше
    ронял конструктор TypeError, а 0 молча давал пакеты по 2000 символов.
    """
    batch_size: int = 20
    batch_max_chars: int = _DEFAULT_BATCH_CHARS
    delay_ms: int = 1500
    max_retries: int = 4
    backoff_base_s: float = 2.0
    backoff_max_s: float = 60.0
    snapshot_tokens: int = 12_000
    validation_retries: int = 2
    shrink_ratio: float = 0.5          # новый снимок < 50% старого — брак
    shrink_min_tokens: int = 800       # проверку «сдувания» включаем от этого размера
    keep_recent_chronicle: int = 12    # сколько последних записей хроники не сжимать в арки
    # Лимит вывода модели памяти в токенах (max_tokens, который уходит в запрос;
    # None — неизвестен, проверки нет). Модель переписывает снимок ЦЕЛИКОМ,
    # поэтому снимок больше лимита заведомо оборвётся: merge_block не зовёт
    # модель, если в лимит не влезают даже разделы 2–4 (их сжатие не трогает),
    # и сначала сжимает хронику, если не влезает весь снимок.
    output_tokens: int | None = None

    def __post_init__(self):
        try:
            chars = int(self.batch_max_chars or 0)
        except (TypeError, ValueError):
            chars = 0
        self.batch_max_chars = max(_MIN_BATCH_CHARS, chars or _DEFAULT_BATCH_CHARS)


def _fmt_int(n) -> str:
    """Число с разделителем тысяч — неразрывным пробелом, чтобы «4 200» не
    разрывалось переносом строки в прогресс-баре."""
    try:
        return f"{int(n):,}".replace(",", "\u00a0")
    except (TypeError, ValueError):
        return str(n)


def _plural_ru(n: int, one: str, few: str, many: str) -> str:
    """Форма слова после числа: 1 запись, 2–4 записи, 5+ записей; 11–14 — «записей»."""
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


@dataclass
class ScanProgress:
    processed: int
    total: int
    batches: int
    state_tokens: int
    phase: str                         # "merge" | "compact" | "wait" | "retry" | "done"
    retry_in_s: float | None = None
    last_range: tuple[int, int] | None = None

    def line(self) -> str:
        """Строка прогресса из ТЗ: «[Обработано 140/800 сообщений | Сжато до 4 200 токенов]»."""
        return (f"[Обработано {self.processed}/{self.total} сообщений | "
                f"Сжато до {_fmt_int(self.state_tokens)} токенов]")


@dataclass
class ScanResult:
    state: str
    processed: int
    batches: int
    status: str                        # "done" | "cancelled" | "limit" | "conflict"
    state_tokens: int
    warnings: list[str] = field(default_factory=list)


# ============================================================================
# Исключения
# ============================================================================
class MemoryLLMError(Exception):
    """
    Ошибка вызова модели, уже классифицированная для повтора и для UI.

    kind: rate_limit | timeout | server | network | auth | bad_request |
    blocked | length | empty | unknown. message — по-русски, его видит
    пользователь в статусе задания.
    """

    def __init__(self, kind: str, message: str, *, retryable: bool,
                 retry_after: float | None = None):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.retryable = retryable
        self.retry_after = retry_after


class SnapshotValidationError(Exception):
    """Модель так и не вернула снимок по схеме: в память ничего не пишется."""

    def __init__(self, problems):
        self.problems = list(problems)
        super().__init__("снимок памяти не прошёл проверку: " + "; ".join(self.problems))


class SourceConflictError(Exception):
    """Кусок истории трижды подряд менялся, пока модель его сворачивала."""


class MemoryCancelled(Exception):
    """
    Прогон остановили («Остановить», сброс памяти) посреди паузы между
    запросами или паузы перед повтором: следующий платный запрос не делается.

    ПОЧЕМУ своё исключение, а не asyncio.CancelledError: это не отмена задачи,
    а просьба прогона. scan_and_compress_history превращает её в статус
    «cancelled» и сохраняет уже сделанное; CancelledError же ядро не
    перехватывает никогда — её ждёт тот, кто отменял задачу.
    """


# ============================================================================
# Нормализация сообщения
# ============================================================================
# Скрытые рассуждения моделей не часть реплики. Вырезаются по всему тексту,
# даже внутри блока кода: мысли не бывают содержимым, а их размер сопоставим
# с самим ответом.
#
# Парные блоки (<think>…</think>, <style>…</style>, <!--…-->) режет не одна
# регулярка вида «<x>[\s\S]*?</x>», а сканер _drop_paired. ПОЧЕМУ: на тексте с
# тысячами НЕзакрытых <think>/<!--/<style> такая регулярка от каждого открытия
# искала закрытие до конца текста — квадратичное время: 80 000 символов —
# 3–5 с синхронно в цикле событий, и так на каждом ходу, пока пакет не записан
# (финальное ревью, M5). Атрибуты открывающего тега ограничены по длине по той
# же причине: «[^>]*» на «<think <think <think…» без «>» тоже сканировал до
# конца текста от каждого вхождения.
_TAG_ATTRS_MAX = 512
_THINK_OPEN_RE = re.compile(r"<(think|thinking)\b[^>]{0,%d}>" % _TAG_ATTRS_MAX, re.IGNORECASE)
_THINK_CLOSE = {name: re.compile(rf"</{name}\s*>", re.IGNORECASE) for name in ("think", "thinking")}
# data:-URI картинок, вставленных прямо в текст: сотни килобайт base64, которые
# модель не прочтёт, а бюджет пакета съедят целиком.
_DATA_URI_RE = re.compile(r"\bdata:[a-z]+/[\w.+-]+(?:;[\w.+=-]+)*,[^\s\"'<>()\]]*",
                          re.IGNORECASE)
# Кандидат в бинарь: пробег символов алфавита base64 без пробелов (переносы
# строк допускаются — MIME режет base64 по 76 символов). Длину и состав
# проверяет _drop_base64.
_B64_RUN_RE = re.compile(r"[A-Za-z0-9+/=]{16,}(?:\r?\n[A-Za-z0-9+/=]{16,})*")
_B64_BINARY_HINT_RE = re.compile(r"[A-Z0-9+/]")
_B64_MIN_CHARS = 200
_B64_MIN_HINT_SHARE = 1 / 3
# Блок кода — ```…``` или `…`. Внутри него теги и разметку не трогаем:
# `vector<int>` или пример HTML — это содержимое, а не мусор оформления.
_CODE_RE = re.compile(r"```[\s\S]*?```|`[^`\n]+`")
# <style>, <script> и HTML-комментарии вырезаются вместе с содержимым (сканером
# _drop_paired, см. выше). У комментария имени нет — его ключ в словаре закрытий "".
_STYLE_SCRIPT_OPEN_RE = re.compile(r"<(style|script)\b[^>]{0,%d}>|<!--" % _TAG_ATTRS_MAX,
                                   re.IGNORECASE)
_STYLE_SCRIPT_CLOSE = {
    "style": re.compile(r"</style\s*>", re.IGNORECASE),
    "script": re.compile(r"</script\s*>", re.IGNORECASE),
    "": re.compile(r"-->"),
}
# Теги, после которых в отображаемом тексте был бы перенос строки.
_BREAK_RE = re.compile(r"<br\s*/?>|</(?:p|div|li|tr|h[1-6]|blockquote)\s*>", re.IGNORECASE)
# Известные HTML-теги (оформление карточек SillyTavern, <span style=…>):
# снимаем сам тег, внутренний текст остаётся. ПОЧЕМУ только известные имена:
# в ролевом чате угловые скобки — не только HTML. Прежнее правило «любое
# <слово …>» съедало вместе с «тегом» OOC-ремарки («<OOC: давай завтра
# продолжим>») и текст вроде «x <y and z> w». Атрибуты — по синтаксису HTML
# (имя латиницей, значение в кавычках или без пробелов), поэтому и
# «<i думаю, что он врёт>» остаётся текстом. style и script вырезаются вместе
# с содержимым раньше (_STYLE_SCRIPT_OPEN_RE). Регистр имён не важен (<linearGradient>).
_HTML_TAG_NAMES = (
    "a", "b", "i", "u", "s", "em", "strong", "span", "div", "p", "br", "hr", "font",
    "center", "small", "big", "sub", "sup", "code", "pre", "blockquote", "ul", "ol",
    "li", "table", "thead", "tbody", "tfoot", "tr", "td", "th", "caption", "img",
    "details", "summary", "h[1-6]", "section", "article", "header", "footer", "nav",
    "main", "aside", "mark", "ruby", "rt", "rp", "del", "ins", "strike", "q", "cite",
    "abbr", "time", "figure", "figcaption", "dl", "dt", "dd", "label", "button",
    "input", "select", "option", "textarea", "iframe", "video", "audio", "source",
    # SVG: сама картинка модели не нужна, а её разметка — шум во входе.
    "svg", "path", "g", "circle", "rect", "line", "polygon", "polyline", "ellipse",
    "text", "tspan", "defs", "stop", "lineargradient", "radialgradient",
)
# Атрибут без значения — только булев атрибут HTML. ПОЧЕМУ не любое слово:
# тогда «тегом» были и англоязычные ремарки — «<time skip>», «<a few hours
# later>», «<small talk>», «<summary of events>» (имя тега + «атрибуты» без
# значений), и сообщение из одной ремарки выпадало из пакета целиком. Атрибут
# со значением (style=…, href=…) по-прежнему любой: в ремарке «=» не бывает.
_BOOLEAN_ATTRS = (
    "open", "hidden", "controls", "autoplay", "loop", "muted", "disabled", "checked",
    "selected", "readonly", "required", "multiple", "novalidate", "default", "reversed",
    "async", "defer", "playsinline", "allowfullscreen", "inert", "itemscope",
)
_TAG_ATTR = (r"""\s+(?:[A-Za-z_:][\w:.-]*\s*=\s*(?:"[^"]*"|'[^']*'|[^\s"'<>`]+)"""
             r"|(?:" + "|".join(_BOOLEAN_ATTRS) + r")(?![\w:.-]))")
# (?![\w:-]) после имени: «<bold>» и «<b-side>» — не <b>.
_TAG_RE = re.compile(r"</?(?:" + "|".join(_HTML_TAG_NAMES) + r")(?![\w:-])"
                     r"(?:" + _TAG_ATTR + r")*\s*/?>", re.IGNORECASE)
_ZERO_WIDTH_RE = re.compile("[\u200b-\u200f\u2060\ufeff]")
# Три и больше переводов строки (две и больше пустых строк) — до одной пустой:
# после снятия <p>/<br> их остаются десятки.
_BLANK_LINES_RE = re.compile(r"\n[ \t]*\n(?:[ \t]*\n)+")

_ATTACHMENT_LABELS = {
    "image": "изображение",
    "audio": "аудио",
    "video": "видео",
    "document": "документ",
}


def _drop_base64(m: re.Match) -> str:
    """
    Вырезать пробег, если это бинарь, а не текст.

    ПОЧЕМУ не просто «200 символов подряд»: растянутые реплики вида «Nooooo…»
    и «hahaha…» латиницей — тоже сплошные пробеги, и это текст. Отличает их
    состав: в алфавите base64 заглавные, цифры, «+» и «/» — 38 символов из 64,
    и в настоящем base64 (случайные байты, нули заголовка PNG, закодированный
    английский или русский текст) их доля от 0,45 до 1. У текстовых пробегов —
    от нуля («Nooo…») до ~0,25 (длинный camelCase). Порог 1/3 лежит между.
    """
    run = m.group()
    size = len(run) - run.count("\n") - run.count("\r")
    if size < _B64_MIN_CHARS:
        return run
    if len(_B64_BINARY_HINT_RE.findall(run)) >= size * _B64_MIN_HINT_SHARE:
        return ""
    return run


def _drop_paired(text: str, open_re: re.Pattern, closers: dict) -> str:
    """
    Вырезать парные блоки «открывающий тег … ближайшее закрытие» вместе с
    содержимым — то же, что делала регулярка «<x>[\\s\\S]*?</x>», но за
    линейное время.

    open_re находит открытие (group(1) — имя тега, у комментария None);
    closers — закрытие по имени. Блок режется до ПЕРВОГО закрытия после
    открытия, незакрытый блок остаётся текстом. ПОЧЕМУ линейно: если после
    открытия закрытия нет, его нет и после любого следующего открытия того же
    вида, поэтому этот вид дальше не ищется вовсе (dead), — а регулярка
    честно искала закрытие от каждого из тысяч открытий до конца текста.
    """
    out: list[str] = []
    keep = pos = 0      # keep — начало ещё не скопированного текста
    dead: set[str] = set()
    while len(dead) < len(closers):
        opened = open_re.search(text, pos)
        if opened is None:
            break
        kind = (opened.group(1) or "").lower()
        close = None if kind in dead else closers[kind].search(text, opened.end())
        if close is None:
            dead.add(kind)
            # С соседней позиции, а не с конца тега: внутри «открытия» без пары
            # может начаться блок другого вида (так было и у регулярки).
            pos = opened.start() + 1
            continue
        out.append(text[keep:opened.start()])
        keep = pos = close.end()
    out.append(text[keep:])
    return "".join(out)


def _strip_markup(text: str) -> str:
    """Снять HTML-оформление вне блоков кода."""
    text = _drop_paired(text, _STYLE_SCRIPT_OPEN_RE, _STYLE_SCRIPT_CLOSE)
    text = _BREAK_RE.sub("\n", text)
    return _TAG_RE.sub("", text)


def _outside_code(text: str, fn: Callable[[str], str]) -> str:
    """Применить fn только к кускам текста вне блоков кода (Markdown и код не трогаем)."""
    out, pos = [], 0
    for m in _CODE_RE.finditer(text):
        out.append(fn(text[pos:m.start()]))
        out.append(m.group())
        pos = m.end()
    out.append(fn(text[pos:]))
    return "".join(out)


def _clean_text(text: str) -> str:
    text = _ZERO_WIDTH_RE.sub("", text or "")
    text = _drop_paired(text, _THINK_OPEN_RE, _THINK_CLOSE)
    text = _DATA_URI_RE.sub("", text)
    text = _B64_RUN_RE.sub(_drop_base64, text)
    text = _outside_code(text, _strip_markup)
    text = _BLANK_LINES_RE.sub("\n\n", text)
    return text.strip()


def _attachments_line(attachments) -> str:
    """«изображение «map.png»; аудио «voice.ogg»» — только подписи, без содержимого.

    Что внутри медиа, модель узнаёт из текста реплик — обычно из ответа
    ассистента, разбирающего файл. Обычно он в том же пакете, но не
    обязательно: сообщение с файлом может оказаться последним в пакете (или
    последним перед окном), и разбор тогда попадёт в следующий пакет — модель
    видит его там поверх снимка, где файл уже упомянут.
    """
    parts = []
    for att in attachments or ():
        if not isinstance(att, dict):
            continue
        label = _ATTACHMENT_LABELS.get(str(att.get("type") or "").lower(), "вложение")
        name = str(att.get("name") or "").strip()
        parts.append(f"{label} «{name}»" if name else label)
    return "; ".join(parts)


def _cut_middle(line: str, max_chars: int) -> str:
    """Оставить начало и конец строки по половине, середину заменить маркером.

    Итог не длиннее max_chars вместе с маркером: пакет считает бюджет по длине
    строк, и одно гигантское сообщение не должно его переполнять. Бюджет
    меньше маркера — остаётся один маркер (без висящего перевода строки).
    """
    keep = max(0, max_chars - len(LONG_MESSAGE_MARK))
    head = keep // 2
    tail = keep - head
    if not tail:
        return (line[:head] + LONG_MESSAGE_MARK).rstrip("\n")
    return line[:head] + LONG_MESSAGE_MARK + line[len(line) - tail:]


def normalize_message(msg: MemoryMessage, max_chars: int = _DEFAULT_BATCH_CHARS) -> str:
    """
    Строка сообщения для пакета:

        [#1234 · 2026-09-20 14:03 · Эльвира] текст реплики
        📎 изображение «map.png»; аудио «voice.ogg»

    "" — сообщение пропускается (нет ни текста, ни вложений). Сообщение только
    с вложениями попадает в пакет строкой с 📎: иначе следующий за ним разбор
    файла ассистентом был бы непонятен.
    """
    body = _clean_text(msg.text)
    files = _attachments_line(msg.attachments)
    if not body and not files:
        return ""
    head = [f"#{msg.id}"]
    stamp = msg.created_at
    if stamp is not None and hasattr(stamp, "strftime"):
        head.append(stamp.strftime("%Y-%m-%d %H:%M"))
    head.append(str(msg.speaker or msg.role or "?"))
    header = f"[{' · '.join(head)}]"
    rest = (f" {body}" if body else "") + (f"\n📎 {files}" if files else "")
    if max_chars and max_chars > 0 and len(header) + len(rest) > max_chars:
        # Режется только текст, шапка остаётся целиком: без id и автора
        # строка в пакете ничья, а «#id» и имя модель переносит в снимок
        # именно из шапки. Поэтому при крошечном max_chars строка может выйти
        # длиннее бюджета — шапка и маркер важнее (MemoryConfig не даёт
        # бюджету опуститься ниже _MIN_BATCH_CHARS).
        rest = _cut_middle(rest, max_chars - len(header))
    return header + rest


# ============================================================================
# Пакеты
# ============================================================================
def plan_batch(messages, batch_size: int, max_chars: int) -> Batch | None:
    """
    Следующий пакет: подряд до batch_size сообщений, пока транскрипт (строки
    через "\\n\\n") не длиннее max_chars. Хотя бы одно сообщение с текстом
    берётся всегда — даже если оно одно больше бюджета (normalize_message уже
    обрезал его середину). None — сообщений нет.
    """
    if not messages:
        return None
    limit = max(1, int(batch_size or 1))
    taken, lines, size = [], [], 0
    for msg in messages:
        if len(taken) >= limit:
            break
        line = normalize_message(msg, max_chars)
        if line:
            extra = len(line) + (2 if lines else 0)
            if lines and max_chars and max_chars > 0 and size + extra > max_chars:
                break
            lines.append(line)
            size += extra
        taken.append(msg)
    return Batch(messages=tuple(taken), transcript="\n\n".join(lines),
                 first_id=taken[0].id, last_id=taken[-1].id)


def plan_batches(messages, batch_size: int, max_chars: int) -> list[Batch]:
    """Все пакеты подряд, без пересечений и пропусков."""
    messages = list(messages or ())
    batches, pos = [], 0
    while pos < len(messages):
        batch = plan_batch(messages[pos:], batch_size, max_chars)
        batches.append(batch)
        pos += len(batch.messages)
    return batches


# ============================================================================
# Схема снимка: разбор и рендер
# ============================================================================
def _fold(s: str) -> str:
    """Регистр и ё/е не различаем: модели пишут заголовки как придётся."""
    return s.lower().replace("ё", "е")


_SECTION_BY_FOLDED = {" ".join(_fold(s).split()): s for s in SECTIONS}
# Заголовок раздела — отдельная строка: «## [ИМЯ]», «### ИМЯ», «[ИМЯ]», «ИМЯ:».
# 1–4 решётки, скобки необязательны. Сравнивается сложенная строка (_fold, без
# «*»), поэтому «**[Хроника и событийный каркас]**» тоже узнаётся.
_SECTION_HEADING_RE = re.compile(
    r"^\s{0,3}(?:#{1,4}\s*)?\[?\s*("
    + "|".join(r"\s+".join(map(re.escape, name.split())) for name in _SECTION_BY_FOLDED)
    + r")\s*\]?\s*:?\s*$"
)
# Строка-заглушка пустого раздела («—» по схеме; модели пишут и «-»/«–»).
_EMPTY_BODIES = {"—", "–", "-"}


def _section_of(line: str) -> str | None:
    m = _SECTION_HEADING_RE.match(_fold(line).replace("*", ""))
    if not m:
        return None
    return _SECTION_BY_FOLDED.get(" ".join(m.group(1).split()))


def parse_sections(text) -> dict[str, str]:
    """
    Разделы снимка → {канон. имя: тело}. Только найденные разделы (так
    валидация видит пропавший), в каноническом порядке. Текст до первого
    заголовка отбрасывается (вступления модели); повтор заголовка дописывает
    тело к тому же разделу, а не затирает его. Тело «—» → "".
    """
    found: dict[str, list[str]] = {}
    current = None
    for line in (text or "").splitlines():
        sec = _section_of(line)
        if sec:
            current = sec
            found.setdefault(sec, [])
        elif current:
            found[current].append(line)
    out = {}
    for sec in SECTIONS:
        if sec in found:
            body = "\n".join(found[sec]).strip()
            out[sec] = "" if body in _EMPTY_BODIES else body
    return out


def render_snapshot(sections: dict) -> str:
    """Канонический вид: «## [ИМЯ]» в порядке SECTIONS, пустой раздел — «—», без обёртки."""
    parts = []
    for sec in SECTIONS:
        body = str((sections or {}).get(sec) or "").strip()
        parts.append(f"## [{sec}]\n{body or '—'}")
    return "\n\n".join(parts)


def is_structured(text) -> bool:
    """Снимок новой схемы (≥ 3 из 4 заголовков), а не свободный пересказ старой сводки."""
    return len(parse_sections(text)) >= 3


# ============================================================================
# Разбор и проверка ответа модели
# ============================================================================
_FENCE_LINE_RE = re.compile(r"^[ \t]*```[\w-]*[ \t]*$", re.MULTILINE)
_OPEN_RE = re.compile(re.escape(ENVELOPE_OPEN), re.IGNORECASE)
_CLOSE_RE = re.compile(re.escape(ENVELOPE_CLOSE), re.IGNORECASE)


def extract_snapshot(raw) -> tuple[str, bool]:
    """
    Текст снимка из сырого ответа → (текст, обрезан ли).

    Мысли (<think>) и ограждения ``` вырезаются. Обёртка есть — кандидат
    каждое открытие <master_state> до ближайшего следующего закрытия (или до
    конца текста — такой кандидат «открыт»). Берётся последний ЗАКРЫТЫЙ
    кандидат со снимком по схеме (is_structured); нет такого — последний
    закрытый; закрытых нет — последний открытый: ответ упёрся в лимит
    вывода, текст возвращается с флагом True. Обёртки нет вовсе — мягкий
    режим: весь текст (разделы потом проверит parse_sections).
    """
    text = _drop_paired(str(raw or ""), _THINK_OPEN_RE, _THINK_CLOSE)
    # Незакрытый <think> — модель оборвалась посреди рассуждений: всё после него мысли.
    unclosed_think = _THINK_OPEN_RE.search(text)
    if unclosed_think:
        text = text[:unclosed_think.start()]
    text = _FENCE_LINE_RE.sub("", text)
    # ПОЧЕМУ не первая и не последняя обёртка. Модель упоминает тег и до
    # снимка («Вот снимок в формате <master_state>…</master_state>:»), и после
    # («Обёртка <master_state> закрыта.»). Первая пара в первом случае —
    # лишь упоминание, последнее открытие во втором — незакрытый хвост из
    # одного слова с флагом «обрезан». Оба раза годный снимок получал четыре
    # «нет раздела» и платный корректирующий ход; выбор по содержимому
    # кандидата различает упоминание и снимок в обоих случаях.
    opens = list(_OPEN_RE.finditer(text))
    if opens:
        closed, unclosed = [], []
        for opened in opens:
            end = _CLOSE_RE.search(text, opened.end())
            if end:
                closed.append(text[opened.end():end.start()].strip())
            else:
                unclosed.append(text[opened.end():].strip())
        if closed:
            return next((c for c in reversed(closed) if is_structured(c)), closed[-1]), False
        return unclosed[-1], True
    closed = _CLOSE_RE.search(text)
    if closed:
        text = text[:closed.start()]
    return text.strip(), False


def default_estimate_tokens(text) -> int:
    """
    Грубая оценка токенов (как horae_memory._tokens_heuristic): кириллица, CJK
    и эмодзи в BPE режутся мелко — ≈ 2 символа на токен, прочее ≈ 4. Сервис
    подставляет точный horae_memory.estimate_tokens (tiktoken).
    """
    text = str(text or "")
    heavy = sum(1 for ch in text if ord(ch) > 0x02FF)
    return max(1, int(heavy / 2 + (len(text) - heavy) / 4))


def _max_snapshot_chars(config: MemoryConfig) -> int:
    """Порог «неправдоподобно длинного» снимка: MAX_SNAPSHOT_CHARS или больше — по бюджету."""
    try:
        budget = int(config.snapshot_tokens or 0)
    except (TypeError, ValueError):
        budget = 0
    return max(MAX_SNAPSHOT_CHARS, budget * _SNAPSHOT_CHARS_PER_TOKEN)


def validate_snapshot(raw, prev, *, config: MemoryConfig | None = None,
                      estimate_tokens: Callable[[str], int] | None = None,
                      check_shrink: bool = True) -> tuple[str, list[str]]:
    """
    Проверить ответ модели → (текст, проблемы).

    Нет проблем — текст уже канонический (render_snapshot(parse_sections(…))).
    Есть — возвращается извлечённый текст как есть, а проблемы (по-русски)
    уходят модели в корректирующий ход.
    """
    config = config or MemoryConfig()
    est = estimate_tokens or default_estimate_tokens
    text, truncated = extract_snapshot(raw)
    problems = []
    if truncated:
        problems.append(f"ответ обрезан (нет закрывающего {ENVELOPE_CLOSE})")
    if not text:
        problems.append("пустой ответ")
        return text, problems
    if len(text) > _max_snapshot_chars(config):
        problems.append("снимок неправдоподобно длинный")
    sections = parse_sections(text)
    problems.extend(f"нет раздела [{sec}]" for sec in SECTIONS if sec not in sections)
    canonical = render_snapshot(sections)
    # «Сдувание» ловим только относительно снимка новой схемы: сводка старой
    # схемы (или пустая память) — это конвертация, и там новый снимок законно
    # бывает короче. Маленький снимок не проверяем — у него шум оценки велик.
    if check_shrink and prev and is_structured(prev):
        before = est(prev)
        if before >= config.shrink_min_tokens:
            after = est(canonical)
            if after < config.shrink_ratio * before:
                problems.append("снимок потерял больше половины содержимого "
                                f"({before} → {after} токенов)")
    return (text if problems else canonical), problems


# ============================================================================
# Страж записей (§4.6): детерминированная гарантия фактов
# ============================================================================
# Маркер записи верхнего уровня: «- », «* », «• », «1. », отступ ≤ 1 пробела.
_ENTRY_MARKER_RE = re.compile(r"^ ?(?:[-*•]|\d{1,3}[.)])\s+")
_SUBHEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+\S")
# Ключ записи — текст до первого из « — », « – », « - », «:», «|», «(».
_KEY_STOP_RE = re.compile(r" [—–-] |[:|(]")
_NON_WORD_RE = re.compile(r"[^\w\s]|_")
_KEY_MAX = 80
_JACCARD_MIN = 0.6


def _norm_key(text: str) -> str:
    """Нижний регистр, ё→е, без пунктуации, пробелы схлопнуты, ≤ 80 символов."""
    words = _NON_WORD_RE.sub(" ", _fold(text)).split()
    return " ".join(words)[:_KEY_MAX].strip()


def _entry_key(first_line: str) -> str:
    text = _ENTRY_MARKER_RE.sub("", first_line.strip(), count=1)
    stop = _KEY_STOP_RE.search(text)
    return _norm_key(text[:stop.start()] if stop else text)


# Хвостовые пометки, которые модель дописывает к записи при обновлении на
# месте: «(#12)», «(было: ранен, #5)». Расширенный ключ их не учитывает —
# иначе запись, обновлённая на месте, выглядела бы потерянной. Пометка —
# скобка без вложенных скобок, начинается с «#» или слова «было».
_NOTE_HEAD_RE = re.compile(r"(?:#|было\b)", re.IGNORECASE)
_EXT_KEY_MAX = 200


def _strip_trailing_notes(text: str) -> str:
    """
    Снять с конца строки цепочку пометок «(#id)»/«(было: …)» вместе с
    пробелами между ними.

    ПОЧЕМУ сканер с конца, а не регулярка «(?:\\s*\\((?:#|было)[^()]*\\))+\\s*$»:
    она пробовала цепочку от КАЖДОЙ позиции строки и на каждой перебирала
    число пометок — на 20 000 символах «(#1)(#1)…x» около секунды синхронно
    в цикле событий, и так для каждой такой записи на каждом пакете. Сканер
    проходит строку один раз: каждая пометка отрезается по ближайшей «(».
    """
    end = len(text.rstrip())
    while end and text[end - 1] == ")":
        start = text.rfind("(", 0, end - 1)
        inner = text[start + 1:end - 1] if start >= 0 else ""
        if start < 0 or ")" in inner or not _NOTE_HEAD_RE.match(inner):
            break
        end = start
        while end and text[end - 1].isspace():
            end -= 1
    return text[:end]


def _extended_key(first_line: str) -> str:
    """
    Расширенный ключ записи — вся первая строка (без маркера и хвостовых
    «(#id)»/«(было: …)»), нормализованная как _norm_key. Нужен, когда
    обычный ключ в старом разделе повторяется: у «Linkin Park — «Numb» — …» и
    «Linkin Park — «In the End» — …» ключ один — «linkin park».
    """
    text = _strip_trailing_notes(_ENTRY_MARKER_RE.sub("", first_line.strip(), count=1))
    return " ".join(_NON_WORD_RE.sub(" ", _fold(text)).split())[:_EXT_KEY_MAX].strip()


def _heading_key(line: str) -> str:
    return _norm_key(line.strip().lstrip("#"))


def _jaccard(a: set, b: set) -> float:
    union = a | b
    return len(a & b) / len(union) if union else 0.0


def _lost_entries(old: list, new_entries: list) -> list:
    """
    Записи старого раздела, которым в новом не нашлось пары: [(подзаголовок, запись), …].

    Сопоставление — МУЛЬТИМНОЖЕСТВО: одна запись нового снимка покрывает одну
    запись старого. Раньше запись считалась сохранённой, если её ключ (или
    похожий по Жаккару ≥ 0,6) был где угодно в разделе, и разные записи с
    одним ключом прикрывали друг друга: пропавший трек «Linkin Park — «In the
    End»» не возвращался, пока в списке оставался «Linkin Park — «Numb»», как
    и «Кольцо (золотое)» рядом с «Кольцо (серебряное)» (финальное ревью, I2).

    Ключ сравнения: обычный (имя до « — »/«:»/«(»), а для ключей, повторяющихся
    в старом разделе, — расширенный (_extended_key). Сначала точные совпадения,
    потом похожие (Жаккар ≥ 0,6; из свободных — самое похожее): иначе
    «Кинжал из белой стали» занял бы пару «Кинжала из чёрной стали», похожего на
    него ровно на 0,6. Лишний возврат (модель слила две записи в одну строку)
    безопаснее потери. Одинаковые старые записи считаются одной.
    """
    new_keys = [_entry_key(e[0]) for e in new_entries]
    new_ext = [_extended_key(e[0]) for e in new_entries]
    by_key: dict[str, list[int]] = {}
    by_ext: dict[str, list[int]] = {}
    for j, (key, ext) in enumerate(zip(new_keys, new_ext)):
        if key:
            by_key.setdefault(key, []).append(j)
            by_ext.setdefault(ext, []).append(j)
    old_keys = [_entry_key(e[0]) for _, e in old]
    repeats = {k for k, n in Counter(old_keys).items() if k and n > 1}
    used = [False] * len(new_entries)
    pending, seen = [], set()
    for (heading, entry), key in zip(old, old_keys):
        whole = " ".join(_NON_WORD_RE.sub(" ", _fold("\n".join(entry))).split())
        if not key or whole in seen:
            continue  # пустой ключ не охраняется; копия записи — та же запись
        seen.add(whole)
        extended = key in repeats
        probe = _extended_key(entry[0]) if extended else key
        pool = (by_ext if extended else by_key).get(probe, ())
        j = next((j for j in pool if not used[j]), None)
        if j is None:
            pending.append((heading, entry, probe, extended))
        else:
            used[j] = True
    lost = []
    key_tokens = [set(k.split()) for k in new_keys] if pending else []
    ext_tokens = [set(x.split()) for x in new_ext] if pending else []
    for heading, entry, probe, extended in pending:
        tokens = set(probe.split())
        best, best_score = None, 0.0
        for j, other in enumerate(ext_tokens if extended else key_tokens):
            if used[j] or not new_keys[j]:
                continue
            score = _jaccard(tokens, other)
            if score >= _JACCARD_MIN and score > best_score:
                best, best_score = j, score
        if best is None:
            lost.append((heading, entry))
        else:
            used[best] = True
    return lost


def _split_groups(body: str) -> list[list]:
    """Тело раздела → [[подзаголовок | None, строки], …]; первая группа — до подзаголовков."""
    groups = [[None, []]]
    for line in body.splitlines():
        if _SUBHEADING_RE.match(line):
            groups.append([line.strip(), []])
        else:
            groups[-1][1].append(line)
    return groups


def _parse_entries(lines: list) -> list[list[str]]:
    """
    Записи (§4.6): строка с маркером верхнего уровня плюс продолжение — все
    следующие строки без такого маркера, с отступом или без (вложенные пункты
    тоже). Пустые строки не входят в запись и не рвут её.

    ПОЧЕМУ продолжение и без отступа: модель часто пишет атрибут персонажа
    отдельной строкой у края («здоровье: ранен в плечо»). Когда такая строка
    сама была записью, её ключ «здоровье» совпадал с атрибутом другого
    персонажа: при потере персонажа страж возвращал только первую строку, а
    атрибут пропадал молча. А если модель сливала продолжение с первой строкой,
    «пропавшее» продолжение дописывалось в конец раздела оторванным фрагментом.

    До первого маркера группы (и в группе совсем без маркеров) запись — каждая
    строка у края, а строка с отступом продолжает предыдущую, как требует
    §4.6. ПОЧЕМУ отступ важен и здесь: частый у модели формат «**Имя**» с
    вложенным списком атрибутов вообще не имеет маркера верхнего уровня. Если
    «  - здоровье: …» станет своей записью, повторится та же тихая потеря
    атрибута. А строки у края без маркеров склеивать нельзя: раздел из строк
    «Имя: статус» превратился бы в одну запись.
    """
    entries: list[list[str]] = []
    in_entry = False  # встретился ли уже маркер: есть ли что продолжать
    for line in lines:
        if not line.strip():
            continue
        if _ENTRY_MARKER_RE.match(line):
            entries.append([line])
            in_entry = True
        elif in_entry or (entries and line.startswith(("  ", "\t"))):
            entries[-1].append(line)
        else:
            entries.append([line])
    return entries


def _target_group(groups: list, heading: str | None) -> list:
    """Группа нового снимка для восстановленной записи; нет подсписка — создать."""
    if heading is None:
        return groups[0]
    key = _heading_key(heading)
    # Сначала точное совпадение, потом похожее: «### Плейлист Эльвиры» не
    # должен уехать в «### Плейлист Эльвиры (старый)», если есть точный.
    for g in groups[1:]:
        if _heading_key(g[0]) == key:
            return g
    tokens = set(key.split())
    for g in groups[1:]:
        if tokens and _jaccard(tokens, set(_heading_key(g[0]).split())) >= _JACCARD_MIN:
            return g
    group = [heading, []]
    groups.append(group)
    return group


def _render_groups(groups: list) -> str:
    out: list[str] = []
    for heading, lines in groups:
        while lines and not lines[-1].strip():
            lines.pop()
        if heading is not None:
            if out:
                out.append("")
            out.append(heading)
        out.extend(lines)
    return "\n".join(out).strip()


def guard_entries(prev, new) -> tuple[str, list[str]]:
    """
    Вернуть в новый снимок записи разделов 2–4, которые модель потеряла.

    ПОЧЕМУ детерминированно: промпт требует переносить записи дословно, но при
    десятках слияний подряд модель рано или поздно «забывает» хвост списка или
    персонажа второго плана — и тихо, без ошибки. Страж сравнивает ключи
    записей (имя до « — »/«:»; регистр, ё/е и пунктуация не важны) и
    дописывает пропавшие исходным текстом в конец своего раздела, а записи
    списка — в свой подсписок. Обновлённую на месте запись («Артём — ранен» →
    «артем — здоров (было: ранен)») он не дублирует: ключ тот же. Записи с
    одинаковым ключом друг друга не прикрывают — см. _lost_entries.

    → (канонический текст, ключи восстановленных записей).
    """
    prev_secs = parse_sections(prev)
    new_secs = parse_sections(new)
    restored: list[str] = []
    for sec in GUARDED_SECTIONS:
        old_body = prev_secs.get(sec, "")
        if not old_body:
            continue
        groups = _split_groups(new_secs.get(sec, ""))
        new_entries = [e for _, lines in groups for e in _parse_entries(lines)]
        old = [(heading, entry) for heading, lines in _split_groups(old_body)
               for entry in _parse_entries(lines)]
        lost = _lost_entries(old, new_entries)
        for heading, entry in lost:
            target = _target_group(groups, heading)[1]
            while target and not target[-1].strip():
                target.pop()
            target.extend(entry)
            restored.append(_entry_key(entry[0]))
        if lost:
            new_secs[sec] = _render_groups(groups)
    return render_snapshot(new_secs), restored


# ============================================================================
# Вызов модели: классификация ошибок (§4.4)
# ============================================================================
# Колбэк модели: сообщения чата → текст ответа. Сервис заворачивает в него
# llm_gateway.complete, тесты — подставную корутину.
LLMCall = Callable[[list[dict]], Awaitable[str]]

# Вид ошибки по имени класса. ПОЧЕМУ по имени, а не isinstance: ядро не
# импортирует litellm, а имя класса в MRO — единственный общий с ним язык.
# MRO обходится от самого производного класса, поэтому спорные случаи
# решаются сами: litellm.Timeout — потомок APIConnectionError,
# ContextWindowExceededError и ContentPolicyViolationError — потомки
# BadRequestError, и первым находится самый точный признак. По этой же
# причине класс важнее status_code: у litellm.APIConnectionError он 500, и
# обрыв связи иначе выглядел бы сбоем сервера.
_KIND_BY_CLASS = {
    "RateLimitError": "rate_limit",
    "Timeout": "timeout",
    "APITimeoutError": "timeout",
    "TimeoutError": "timeout",           # и asyncio.TimeoutError
    "InternalServerError": "server",
    "ServiceUnavailableError": "server",
    "BadGatewayError": "server",
    "APIConnectionError": "network",
    "ConnectionError": "network",
    "OSError": "network",
    "AuthenticationError": "auth",
    "PermissionDeniedError": "auth",
    "BadRequestError": "bad_request",
    "NotFoundError": "bad_request",
    "UnprocessableEntityError": "bad_request",
    "ContextWindowExceededError": "bad_request",
    "ContentPolicyViolationError": "blocked",
}
# Пустой стрим llm_gateway превращает в RuntimeError с текстом
# censorship.explain_block, где последняя строка — «Технически: ПУСТОЙ ответ,
# finish_reason=…». Список блокировок — как _CONFIGURABLE/_NON_CONFIGURABLE в
# censorship: повторять заблокированный запрос бессмысленно, фильтр ответит
# тем же.
_FINISH_REASON_RE = re.compile(r"finish_reason=([A-Za-z_]+)")
_BLOCK_REASONS = frozenset({
    "SAFETY", "CONTENT_FILTER", "CONTENT_POLICY_VIOLATION", "PROHIBITED_CONTENT",
    "BLOCKLIST", "SPII", "RECITATION", "IMAGE_SAFETY", "IMAGE_PROHIBITED_CONTENT",
})
_LENGTH_REASONS = frozenset({"LENGTH", "MAX_TOKENS"})
_EMPTY_MARK = "ПУСТОЙ ответ"
# Что есть смысл повторить. auth/bad_request/blocked ответят тем же на любой
# попытке; length лечится не повтором, а сжатием снимка (merge_block).
_RETRYABLE_KINDS = frozenset({"rate_limit", "timeout", "server", "network", "empty", "unknown"})
# Начало сообщения для статуса задания; хвост — сокращённый текст исходной
# ошибки, по нему ищут причину в логах прокси.
_KIND_TEXT = {
    "rate_limit": "Провайдер ограничил частоту запросов",
    "timeout": "Модель не ответила вовремя",
    "server": "Сбой на стороне провайдера",
    "network": "Нет связи с провайдером или прокси",
    "auth": "Доступ к модели отклонён — проверьте ключ API и права",
    "bad_request": "Провайдер отклонил запрос (параметры, имя модели или длина контекста)",
    "blocked": "Фильтр провайдера заблокировал ответ",
    "length": "Ответ модели упёрся в лимит длины вывода",
    "empty": "Модель вернула пустой ответ",
    "unknown": "Ошибка вызова модели",
}
_ERROR_DETAIL_CHARS = 300
# Потолок ожидания по Retry-After: заведомо огромный заголовок (суточная
# квота) иначе подвесил бы задание без движения и без объяснений.
_MAX_RETRY_AFTER_S = 300.0


def _status_code(exc) -> int | None:
    for obj in (exc, getattr(exc, "response", None)):
        try:
            code = int(getattr(obj, "status_code", None))
        except (TypeError, ValueError):
            continue
        if 100 <= code <= 599:
            return code
    return None


def _kind_by_status(code: int) -> str | None:
    if code == 429:
        return "rate_limit"
    if code == 408:
        return "timeout"
    if code in (401, 403):
        return "auth"
    if code in (400, 404, 422):
        return "bad_request"
    if 500 <= code <= 599:
        return "server"
    return None


def _kind_by_text(text: str) -> str | None:
    m = _FINISH_REASON_RE.search(text)
    reason = m.group(1).upper() if m else ""
    if reason in _LENGTH_REASONS:
        return "length"
    if reason in _BLOCK_REASONS:
        return "blocked"
    if _EMPTY_MARK in text:
        return "empty"
    return None


def _retry_after(exc) -> float | None:
    """Секунды из заголовка Retry-After ответа провайдера (HTTP-дата не разбирается)."""
    headers = getattr(getattr(exc, "response", None), "headers", None)
    if not headers:
        return None
    value = headers.get("retry-after") or headers.get("Retry-After")
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    return seconds if seconds > 0 else None


def classify_error(exc) -> MemoryLLMError:
    """
    Любое исключение вызова модели → MemoryLLMError с видом, признаком повтора
    и русским текстом для статуса задания (таблица §4.4).

    Порядок признаков: имя класса (по MRO) → HTTP-статус (у исключения или у
    его response) → текст RuntimeError от llm_gateway (finish_reason /
    «ПУСТОЙ ответ») → unknown, который повторяется: неизвестный сбой чаще
    временный, а число попыток всё равно ограничено.
    """
    if isinstance(exc, MemoryLLMError):
        return exc
    kind = next((k for cls in type(exc).__mro__
                 if (k := _KIND_BY_CLASS.get(cls.__name__))), None)
    if kind is None:
        code = _status_code(exc)
        kind = _kind_by_status(code) if code else None
    if kind is None and isinstance(exc, RuntimeError):
        kind = _kind_by_text(str(exc))
    kind = kind or "unknown"
    detail = " ".join(str(exc).split())
    if len(detail) > _ERROR_DETAIL_CHARS:
        detail = detail[:_ERROR_DETAIL_CHARS].rstrip() + "…"
    message = f"{_KIND_TEXT[kind]}: {detail}" if detail else _KIND_TEXT[kind]
    return MemoryLLMError(kind, message, retryable=kind in _RETRYABLE_KINDS,
                          retry_after=_retry_after(exc))


# ============================================================================
# Источник пакетов (§4.8)
# ============================================================================
@runtime_checkable
class BatchSource(Protocol):
    """
    Откуда цикл свёртки берёт сообщения и куда отдаёт результат пакета.

    Ядро не знает про БД: memory_service.DbBatchSource читает чат и пишет
    HoraeEntry, ListSource — просто список. commit → False значит «кусок
    истории изменился, пока модель его сворачивала» (правка, удаление, свайп):
    такой снимок писать нельзя, пакет собирается заново.
    """

    async def pending(self) -> int: ...                          # сколько сообщений ждут

    async def current_state(self, state: str) -> str: ...        # свежий снимок перед пакетом

    async def next_messages(self, limit: int) -> list[MemoryMessage]: ...  # [] — нечего

    async def commit(self, batch: Batch, new_state: str) -> bool: ...     # False — кусок изменился


class ListSource:
    """Готовый список сообщений как BatchSource: снимок никто не правит, сверять не с чем."""

    def __init__(self, messages: Sequence[MemoryMessage]):
        self._messages = list(messages or ())
        self._pos = 0

    async def pending(self) -> int:
        return len(self._messages) - self._pos

    async def current_state(self, state: str) -> str:
        return state

    async def next_messages(self, limit: int) -> list[MemoryMessage]:
        return self._messages[self._pos:self._pos + max(1, int(limit or 1))]

    async def commit(self, batch: Batch, new_state: str) -> bool:
        self._pos += len(batch.messages)
        return True


# Сколько раз подряд задание пересобирает пакет, чей кусок истории менялся
# под моделью. Дальше — SourceConflictError: кто-то правит этот кусок прямо
# сейчас, и крутить модель вхолостую бессмысленно.
_MAX_CONFLICTS = 3
# Цель сжатия — доля бюджета снимка (гистерезис). ПОЧЕМУ ниже бюджета: снимок,
# сжатый ровно до бюджета, превысит его уже на следующем пакете, и сжатие —
# полный переписанный моделью снимок — шло бы на каждом пакете. Запас в 20 %
# отодвигает следующее сжатие на несколько пакетов. Превышение бюджета при
# этом по-прежнему считается от полного snapshot_tokens.
_COMPACT_TARGET = 0.8
# Начало предупреждения о бюджете. X в «X из Y токенов» растёт с каждым
# пакетом, поэтому новое такое предупреждение заменяет прежнее (см. _warn).
_OVER_BUDGET = "снимок превышает бюджет:"
# Арка — запись хроники, в которую compact() уже свернул старые записи:
# «- [#a–#b] Арка «название»: …» (формат задаёт COMPACT_PROMPT). Модель
# выделяет её жирным, пишет строчными или ставит диапазон в конец — узнаём и
# так. Проверяется текст после маркера записи. Ошибки узнавания безвредны для
# данных: арка без слова «Арка» сойдёт за обычную запись (сжатие позовут
# раньше), а сюжетная запись «Арка ворот рухнула» — за арку (позовут позже).
_ARC_RE = re.compile(r"[*_\s]*(?:\[[^\]\n]*\][*_\s]*)?арк[аи]\b", re.IGNORECASE)


# Тексты ошибки length, которые ядро ставит само (а не классифицирует ошибку
# провайдера). Их видит пользователь: в статусе задания и в last_error
# ежеходного прохода во вкладке «Память».
_TRUNCATED_TEXT = (f"{_KIND_TEXT['length']}: снимок оборван на полуслове "
                   f"(нет закрывающего {ENVELOPE_CLOSE})")
_GUARDED_OVER_OUTPUT = ("реестр и списки больше лимита вывода модели памяти "
                        "(≈ {tokens} из {limit} токенов): поднимите лимит/бюджет "
                        "или почистите снимок вручную")
_SNAPSHOT_OVER_OUTPUT = ("снимок больше лимита вывода модели памяти даже после сжатия "
                         "хроники (≈ {tokens} из {limit} токенов): поднимите лимит/бюджет "
                         "или почистите снимок вручную")
_MESSAGE_ID_RE = re.compile(r"#(\d+)")


def _chronicle_loss(before: str, after: str) -> str | None:
    """
    Нижняя граница ответа сжатия: что он потерял из хроники (None — ничего).

    ПОЧЕМУ: раньше проверялось только «хроника стала короче», и ответ с
    хроникой «—» принимался — вся хроника (десятки записей) стиралась молча, и
    вернуть её могла только пересборка (финальное ревью, M1). Последние
    keep_recent_chronicle записей промпт велит оставить как есть, поэтому
    самый поздний номер сообщения самой свежей записи («[#a–#b]» → b)
    обязан остаться в хронике — в этой записи или концом арки. В новой
    хронике номер ищется без «#»: «[#1-79]» — тоже честная арка. Числа без
    «#» (суммы, даты) в расчёт не идут; записи без номеров не проверяются.
    """
    if not before.strip():
        return None
    if not after.strip():
        return "вернуло пустую хронику"
    entries = _parse_entries(before.splitlines())
    numbers = [int(n) for n in _MESSAGE_ID_RE.findall(entries[-1][0])] if entries else []
    if numbers:
        last = max(numbers)
        if not re.search(rf"(?<!\d){last}(?!\d)", after):
            return f"потеряло последние записи хроники (#{last})"
    return None


def _fold_threshold(keep: int) -> int:
    """
    Сколько записей для свёртки (старше keep и ещё не арок) нужно, чтобы
    compact() позвал модель, — гистерезис по записям, в пару к _COMPACT_TARGET
    по токенам.

    ПОЧЕМУ: в длинном чате хроника — «арки + keep последних», и каждый пакет
    добавляет по записи. Если бюджет держат разделы 2–4 (их сжатие не трогает),
    снимок сверх бюджета и после сжатия, и гистерезис по токенам не помогает:
    модель переписывала весь снимок ради одной записи на КАЖДОМ пакете (пропуск
    «записей ≤ keep» не срабатывал — арка тоже запись). С порогом — раз в
    keep // 2 пакетов.
    """
    return max(2, max(0, keep) // 2)


# ============================================================================
# Менеджер: слияние, сжатие, цикл свёртки (§4.4–§4.8)
# ============================================================================
class HierarchicalMemoryManager:
    """
    Свёртка истории в мастер-снимок: State_N = merge(State_{N-1}, Block_N).

    Экземпляр обслуживает один чат (сервис строит его на прогон).
    Пауза-ограничитель считается от конца ПРЕДЫДУЩЕГО вызова этого менеджера,
    поэтому слияния, сжатия и факты (сервис зовёт call() и для них) проходят
    через одну «дверь» и не превышают лимит частоты провайдера. sleep и clock
    внедряются ради тестов без реального ожидания.

    Экземпляр рассчитан на ОДИН прогон scan_and_compress_history за раз:
    прогресс и отсчёт предупреждений прогона — поля экземпляра, и второй
    параллельный прогон перепутал бы их. А вот параллельные call() допустимы
    (сервис зовёт факты через call() из commit, посреди прогона): они
    сериализуются asyncio.Lock вокруг «пауза + вызов». Замок не реентерабелен —
    колбэк модели не должен сам звать call().
    """

    def __init__(self, llm: LLMCall, config: MemoryConfig | None = None, *,
                 sleep: Callable[[float], Awaitable] = asyncio.sleep,
                 clock: Callable[[], float] = time.monotonic,
                 estimate_tokens: Callable[[str], int] | None = None):
        self._llm = llm
        self.config = config or MemoryConfig()
        self._sleep = sleep
        self._clock = clock
        self._est = estimate_tokens or default_estimate_tokens
        # Предупреждения для UI (страж вернул записи, сжатие не удалось, снимок
        # сверх бюджета). Копятся за всю жизнь менеджера, без повторов внутри
        # прогона; прогон отдаёт свои — те, что после _warn_from.
        self.warnings: list[str] = []
        self._warn_from = 0
        self._last_end: float | None = None
        self._lock = asyncio.Lock()
        # Прогресс идущего прогона scan_and_compress_history. Вне прогона None,
        # и события пауз, повторов и сжатия никуда не уходят.
        self._progress: ScanProgress | None = None
        self._on_progress: Callable[[ScanProgress], None] | None = None
        # (phase, retry_in_s) последнего отданного события — то, что сейчас
        # видит UI; call() по нему решает, нужно ли вернуть фазу работы.
        self._shown: tuple[str, float | None] | None = None
        # Событие отмены идущего прогона (None — прогона нет или отмены не
        # просили): паузы call() его слушают, см. _pause.
        self._cancel: asyncio.Event | None = None

    # ---------------------------------------------------------------- пакеты
    def plan_batch(self, messages) -> Batch | None:
        """Модульный plan_batch с размером пакета и бюджетом символов из config."""
        return plan_batch(messages, self.config.batch_size, self.config.batch_max_chars)

    def plan_batches(self, messages) -> list[Batch]:
        return plan_batches(messages, self.config.batch_size, self.config.batch_max_chars)

    def _tokens(self, text: str) -> int:
        return self._est(text) if text else 0

    def _emit(self, **fields) -> None:
        """Событие прогресса: текущее состояние прогона + изменённые поля (фаза и т. п.)."""
        if self._on_progress is None or self._progress is None:
            return
        event = replace(self._progress, **fields)
        self._shown = (event.phase, event.retry_in_s)
        self._on_progress(event)

    def _warn(self, text: str, *, supersedes: str | None = None) -> None:
        """
        Предупреждение для UI — без повторов в пределах прогона.

        ПОЧЕМУ: одна и та же беда повторяется на каждом пакете (модель снова
        роняет ту же запись, сжатие снова не сокращает хронику), и десятки
        копий вытеснили бы из meta.warnings (там последние ≤ 5) всё остальное.
        supersedes — начало предупреждения, которое новое заменяет: у «снимок
        превышает бюджет: X из Y» X меняется с каждым пакетом, а UI нужна одна
        строка — с последними числами. Вне прогона повторы ищутся среди
        предупреждений, выданных после конца последнего прогона.
        """
        run = self.warnings[self._warn_from:]
        if text in run:
            return
        if supersedes:
            self.warnings[self._warn_from:] = [w for w in run if not w.startswith(supersedes)]
        self.warnings.append(text)

    def warn(self, text: str) -> None:
        """Предупреждение прогона извне ядра (сервис: модель не приняла max_tokens и т. п.)."""
        self._warn(text)

    def _forget_over_budget(self) -> None:
        """
        Снимок снова в бюджете — убрать из предупреждений прогона «снимок
        превышает бюджет». Раньше оно оставалось и после удачного сжатия
        следующего пакета и уходило в meta.warnings и итог задания неправдой
        (финальное ревью, M2).
        """
        run = self.warnings[self._warn_from:]
        self.warnings[self._warn_from:] = [w for w in run if not w.startswith(_OVER_BUDGET)]

    def _foldable(self, state: str, *, arcs: bool = False) -> int:
        """
        Сколько записей хроники compact() может свернуть: старше последних
        keep_recent_chronicle и ещё не арки. Арки не считаются: иначе после
        первого же сжатия хроника «арка + keep» всегда длиннее keep, и порог
        _fold_threshold ничего бы не сдерживал.

        :param arcs: считать и арки — путь length, где их сворачивают в арку
            более высокого уровня (см. merge_block).
        """
        chronicle = parse_sections(state).get(SEC_CHRONICLE, "")
        entries = _parse_entries(chronicle.splitlines())
        older = entries[:max(0, len(entries) - max(0, self.config.keep_recent_chronicle))]
        if arcs:
            return len(older)
        return sum(1 for e in older
                   if not _ARC_RE.match(_ENTRY_MARKER_RE.sub("", e[0].strip(), count=1)))

    def _guarded_tokens(self, state: str) -> int:
        """Вес разделов 2–4 без хроники — той части снимка, которую сжатие не трогает."""
        sections = parse_sections(state)
        return self._tokens("\n\n".join(f"## [{sec}]\n{sections.get(sec) or '—'}"
                                        for sec in GUARDED_SECTIONS))

    def _warn_over_budget(self, state: str, *, compacted: bool = False) -> None:
        """
        «снимок превышает бюджет: X из Y токенов», если превышает.

        Превышение держат разделы 2–4 — и это пишется в скобках, — если
        сворачивать в хронике уже нечего (меньше порога _fold_threshold) или
        если хронику только что сжали (compacted), а разделы 2–4 сами по себе
        больше бюджета: никакое сжатие хроники тогда снимок в бюджет не
        вернёт. Голое «X из Y» не объясняло ни причины, ни того, что
        автоматически это не пройдёт. После НЕудачного сжатия «хроника уже
        сжата» было бы неправдой — там решает только порог.
        """
        budget = self.config.snapshot_tokens
        tokens = self._tokens(state)
        if tokens <= budget:
            self._forget_over_budget()
            return
        text = f"{_OVER_BUDGET} {_fmt_int(tokens)} из {_fmt_int(budget)} токенов"
        if (self._foldable(state) < _fold_threshold(self.config.keep_recent_chronicle)
                or (compacted and self._guarded_tokens(state) > budget)):
            text += " (хроника уже сжата; реестр и списки не сжимаются автоматически)"
        self._warn(text, supersedes=_OVER_BUDGET)

    def _guard(self, prev: str, new: str) -> str:
        """Страж записей (§4.6) + предупреждение, если модель что-то выронила."""
        new, restored = guard_entries(prev, new)
        if restored:
            n = len(restored)
            self._warn(f"модель потеряла {n} {_plural_ru(n, 'запись', 'записи', 'записей')} — "
                       "возвращены из предыдущего снимка")
        return new

    # ------------------------------------------------------------ вызов модели
    def _cancel_requested(self) -> bool:
        return self._cancel is not None and self._cancel.is_set()

    async def _pause(self, seconds: float) -> None:
        """
        Пауза между запросами или перед повтором, которую прерывает отмена
        прогона → MemoryCancelled.

        ПОЧЕМУ: отмена проверялась только между пакетами, а пауза перед
        повтором по Retry-After длится до 300 с (до max_retries раз на запрос):
        «Остановить» и «Сбросить память» ждали минутами, и после паузы шли ещё
        платные попытки (финальное ревью, M3). Вне прогона (_cancel is None) —
        обычный сон.
        """
        if self._cancel is None:
            await self._sleep(seconds)
            return
        if self._cancel.is_set():
            raise MemoryCancelled()
        sleeper = asyncio.ensure_future(self._sleep(seconds))
        waiter = asyncio.ensure_future(self._cancel.wait())
        try:
            await asyncio.wait({sleeper, waiter}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            # И при отмене самой задачи (CancelledError из wait): висящие
            # сон и ожидание события иначе пережили бы прогон.
            for pending in (sleeper, waiter):
                if not pending.done():
                    pending.cancel()
        if self._cancel.is_set():
            raise MemoryCancelled()

    async def call(self, messages: list[dict], *, phase: str = "merge") -> str:
        """
        Один запрос к модели: пауза-ограничитель + повтор с бэкоффом (§4.4).

        Пауза стоит между ЛЮБЫМИ запросами менеджера: провайдер режет частоту
        по всем запросам ключа, а не отдельно по слияниям или фактам. Замок
        держится на всём вызове, включая повторы: без него два одновременных
        call() прочли бы один _last_end, выждали одинаково и ушли к провайдеру
        разом, а бэкофф одного пропускал бы вперёд запрос другого.

        Отмена прогона (cancel в scan_and_compress_history) прерывает паузы и
        не пускает новый запрос: MemoryCancelled. Уже начатый запрос к модели
        доводится — он оплачен, и его результат пакет ещё может записать.

        :param phase: работа запроса для событий прогресса — "merge" (слияние
            и факты пакета) или "compact". Перед каждым запросом к модели
            событие с этой фазой уходит, если UI видит другое: паузу, повтор
            или фазу прошлого запроса.
        """
        async with self._lock:
            if self._cancel_requested():
                raise MemoryCancelled()
            delay = max(0.0, self.config.delay_ms / 1000)
            if self._last_end is not None:
                wait = delay - (self._clock() - self._last_end)
                if wait > 0:
                    self._emit(phase="wait", retry_in_s=round(wait, 1))
                    await self._pause(wait)
            attempt = 0
            while True:
                if self._shown != (phase, None):
                    # «wait» и «retry» уходят ПЕРЕД сном, а следующее событие
                    # прогона — только после записи пакета. Без возврата фазы
                    # опрос вкладки «Память» весь платный запрос видел «пауза
                    # между запросами» или замерший «повтор через 2 с», а
                    # «сжатие хроники» тут же перетиралось паузой (ревью
                    # задачи 10, I1). То же после сжатия: повтор слияния на
                    # пути length и факты пакета — уже не сжатие.
                    self._emit(phase=phase, retry_in_s=None)
                try:
                    out = await self._llm(messages)
                except asyncio.CancelledError:
                    # Отмена задания или остановка сервера — не сбой провайдера:
                    # её нельзя ни повторять, ни глотать.
                    raise
                except Exception as exc:  # noqa: BLE001 — классифицируем любую ошибку провайдера
                    # Конец вызова отмечается при любом исходе, кроме отмены. В том
                    # числе когда колбэк сам бросил MemoryLLMError (сервис мог
                    # классифицировать ошибку заранее): это тоже был запрос к
                    # провайдеру, и следующий обязан выдержать паузу. Раньше такой
                    # исход проскакивал мимо, и сжатие после length уходило без паузы.
                    self._last_end = self._clock()
                    # MemoryLLMError возвращается как есть: retryable=True от
                    # колбэка повторяется так же, как классифицированная ошибка.
                    err = classify_error(exc)
                    if not err.retryable or attempt >= self.config.max_retries:
                        if err is exc:
                            raise
                        raise err from exc
                    wait = min(self.config.backoff_max_s,
                               self.config.backoff_base_s * 2 ** attempt)
                    if err.retry_after:
                        wait = min(_MAX_RETRY_AFTER_S, max(wait, err.retry_after))
                    # Повтор — тоже запрос: короткий бэкофф не должен обходить паузу.
                    wait = max(wait, delay)
                    self._emit(phase="retry", retry_in_s=wait)
                    await self._pause(wait)
                    attempt += 1
                else:
                    self._last_end = self._clock()
                    return out

    async def _ask_snapshot(self, messages: list[dict], *, prev: str,
                            check_shrink: bool, phase: str = "merge") -> str:
        """
        Запросить снимок и проверить его; брак вернуть модели корректирующим
        ходом, до validation_retries раз, затем SnapshotValidationError.

        В повторный запрос идёт только ПОСЛЕДНИЙ брак (исходный запрос + сырой
        ответ + «Ответ отклонён: …»): каждая попытка размером со снимок, и
        копить их все — раздувать вход с каждой неудачей.

        Ответ, оборванный лимитом вывода (открытая обёртка без закрытия), —
        не брак, а MemoryLLMError("length"): корректирующий ход с тем же
        лимитом оборвался бы снова. Раньше так и было — три полноразмерных
        платных вызова, отказ, и на КАЖДОМ следующем ходу то же самое, а путь
        «сжать хронику и повторить» (merge_block) не срабатывал никогда: шлюз
        сообщает о length только при ПУСТОМ ответе (финальное ревью, I1).
        """
        convo = messages
        problems: list[str] = []
        for _ in range(max(0, self.config.validation_retries) + 1):
            raw = await self.call(convo, phase=phase)
            if extract_snapshot(raw)[1]:
                raise MemoryLLMError("length", _TRUNCATED_TEXT, retryable=False)
            text, problems = validate_snapshot(raw, prev, config=self.config,
                                               estimate_tokens=self._est,
                                               check_shrink=check_shrink)
            if not problems:
                return text
            convo = [*messages,
                     {"role": "assistant", "content": str(raw or "")},
                     {"role": "user",
                      "content": CORRECTION_PROMPT.format(problems="; ".join(problems))}]
        raise SnapshotValidationError(problems)

    # ---------------------------------------------------------------- слияние
    async def _merge(self, state: str, batch: Batch) -> str:
        user = (f"[Текущая память]\n{state.strip() or EMPTY_STATE}\n\n"
                f"[Новые события #{batch.first_id}–#{batch.last_id}]\n{batch.transcript}")
        return await self._ask_snapshot(
            [{"role": "system", "content": MASTER_STATE_PROMPT},
             {"role": "user", "content": user}],
            prev=state, check_shrink=True)

    async def merge_block(self, state: str, batch: Batch) -> str:
        """
        Влить пакет в снимок (§4.5) → новый снимок в каноническом виде.

        Брак ответа уходит модели корректирующим ходом; не исправила —
        SnapshotValidationError, и вызывающий ничего не пишет. После
        валидного ответа страж возвращает потерянные записи, а снимок сверх
        бюджета сжимается в арки.
        """
        state = state or ""
        if not batch.transcript.strip():
            # В пакете одни пустые сообщения: вливать нечего, а указатель всё
            # равно должен их пройти (см. Batch) — вызов модели был бы впустую.
            return state
        state = await self._fit_output(state)
        try:
            new = await self._merge(state, batch)
        except MemoryLLMError as err:
            if err.kind != "length":
                raise
            # Модель упёрлась в лимит вывода: она переписывает снимок целиком,
            # и большой снимок сам съедает бюджет ответа. Сжимаем хронику и
            # пробуем ещё раз; повторная length уходит наверх. Гистерезис по
            # записям тут не к месту: без сжатия повтор почти наверняка снова
            # упрётся в лимит, так что сворачиваем хоть одну запись — и арки
            # тоже. В длинном чате хроника — «десятки арок + keep последних»,
            # обычных записей для свёртки нет, и без арок сжатие не звалось
            # вовсе: повтор слияния упирался в тот же лимит.
            state = await self.compact(state, min_fold=1, fold_arcs=True)
            if self._cancel_requested():
                # Сжатие прервала отмена — повтор слияния не делается.
                raise MemoryCancelled() from err
            new = await self._merge(state, batch)
        if is_structured(state):
            new = self._guard(state, new)
        if self._tokens(new) > self.config.snapshot_tokens:
            new = await self.compact(new)
        else:
            self._forget_over_budget()
        return new

    async def _fit_output(self, state: str) -> str:
        """
        Снимок перед слиянием должен влезать в лимит вывода модели
        (config.output_tokens): модель переписывает его целиком плюс события.

        Разделы 2–4 сами больше лимита — MemoryLLMError("length") без вызова
        модели: их сжатие не трогает, любой ответ будет оборван, и каждая
        попытка — полноразмерный платный вызов впустую. Весь снимок больше
        лимита, а разделы 2–4 влезают — сначала сжатие хроники (с арками, как
        на пути length), потом слияние; не помогло — та же ошибка. Лимит
        неизвестен или снимок старой схемы — проверки нет.
        """
        limit = self.config.output_tokens
        if not limit or limit <= 0 or not is_structured(state):
            return state
        guarded = self._guarded_tokens(state)
        if guarded >= limit:
            raise MemoryLLMError(
                "length", _GUARDED_OVER_OUTPUT.format(tokens=_fmt_int(guarded),
                                                      limit=_fmt_int(limit)),
                retryable=False)
        if self._tokens(state) >= limit:
            state = await self.compact(state, min_fold=1, fold_arcs=True)
            if self._cancel_requested():
                raise MemoryCancelled()
            tokens = self._tokens(state)
            if tokens >= limit:
                raise MemoryLLMError(
                    "length", _SNAPSHOT_OVER_OUTPUT.format(tokens=_fmt_int(tokens),
                                                           limit=_fmt_int(limit)),
                    retryable=False)
        return state

    # ----------------------------------------------------------------- сжатие
    async def compact(self, state: str, *, min_fold: int | None = None,
                      fold_arcs: bool = False) -> str:
        """
        Сжать хронику снимка в арки (§4.7) → снимок.

        Разделы 2–4 не сжимаются никогда: из ответа модели берётся ТОЛЬКО
        хроника, а разделы 2–4 остаются из входного снимка символ в символ.
        Раньше ответ шёл в снимок целиком, а страж сверяет лишь ключи записей:
        модель честно сворачивала хронику и «заодно» ужимала «Эльвира —
        здоровье: …; при себе: ключ №7…; скрытые мотивы: ищет брата» до
        «Эльвира — ранена», и это принималось молча — раз в несколько пакетов,
        с накоплением потерь (финальное ревью, I3). Любая неудача (брак после
        корректирующих ходов, ошибка API, хроника не стала короче или потеряла
        свежие записи — _chronicle_loss) — не повод терять готовый снимок:
        предупреждение и прежний текст. Отмена прогона посреди паузы перед
        сжатием — прежний текст без предупреждения: сожмёт следующий прогон.
        Гистерезис двойной: цель в промпте — _COMPACT_TARGET от бюджета, а
        модель зовётся, только когда накопилось что сворачивать.

        :param min_fold: сколько записей для свёртки (старше keep и не арок)
            нужно, чтобы звать модель; None — _fold_threshold(keep). Меньше —
            снимок возвращается как есть.
        :param fold_arcs: сворачивать и старые арки (путь length в
            merge_block): в счёт min_fold идут все записи старше keep, а к
            промпту добавляется COMPACT_ARCS_PROMPT — разрешение объединить
            арки в арку более высокого уровня. Обычное сжатие (снимок сверх
            бюджета) зовётся без него: там арки уже свёрнуты, и порог
            _fold_threshold считает только обычные записи.
        """
        if not is_structured(state):
            # Пустая память или пересказ старой схемы: хроники-раздела нет,
            # сжимать нечего.
            return state
        if min_fold is None:
            min_fold = _fold_threshold(self.config.keep_recent_chronicle)
        if self._foldable(state, arcs=fold_arcs) < max(1, min_fold):
            # Сокращать нечего или почти нечего: последние keep записей промпт
            # велит оставить как есть, арки уже свёрнуты, а раздуты разделы 2–4,
            # которые не сжимаются никогда. Раньше модель и тут получала запрос
            # сжатия — на КАЖДОМ пакете: полный снимок сверх бюджета на входе и
            # на выходе ради одной записи (или ответ «не короче»), вдвое больше
            # вызовов. Накопившиеся записи свернёт следующее сжатие.
            self._warn_over_budget(state)
            return state
        self._emit(phase="compact")
        budget = self.config.snapshot_tokens
        chronicle = parse_sections(state).get(SEC_CHRONICLE, "")
        system = COMPACT_PROMPT.format(budget=int(budget * _COMPACT_TARGET),
                                       keep=self.config.keep_recent_chronicle)
        if fold_arcs:
            system += "\n" + COMPACT_ARCS_PROMPT
        result, done = state, False
        try:
            compacted = await self._ask_snapshot(
                [{"role": "system", "content": system},
                 {"role": "user", "content": "[Текущая память]\n" + state}],
                prev=state, check_shrink=False, phase="compact")
        except MemoryCancelled:
            return state
        except (SnapshotValidationError, MemoryLLMError) as err:
            self._warn(f"сжатие хроники не удалось ({err}) — оставлен прежний снимок")
        else:
            new_chronicle = parse_sections(compacted).get(SEC_CHRONICLE, "")
            loss = _chronicle_loss(chronicle, new_chronicle)
            if loss:
                self._warn(f"сжатие {loss} — оставлен прежний снимок")
            elif self._tokens(new_chronicle) < self._tokens(chronicle):
                result = render_snapshot({**parse_sections(state), SEC_CHRONICLE: new_chronicle})
                done = True
            else:
                self._warn("сжатие не сократило хронику — оставлен прежний снимок")
        self._warn_over_budget(result, compacted=done)
        return result

    # ------------------------------------------------------------ цикл свёртки
    async def scan_and_compress_history(
            self, source: Sequence[MemoryMessage] | BatchSource, state: str = "", *,
            batch_size: int | None = None,
            delay_ms: int | None = None,
            on_progress: Callable[[ScanProgress], None] | None = None,
            cancel: asyncio.Event | None = None,
            max_batches: int | None = None,
            retry_conflicts: bool = True) -> ScanResult:
        """
        Свернуть всё, что ждёт в source, в снимок (§4.8).

        Пакеты идут строго по очереди: запрос пакета N несёт снимок после
        пакета N-1 — в этом вся свёртка. Готовый пакет сразу уходит в
        source.commit, поэтому отмена, лимит или ошибка не теряют сделанного.

        Сигнатура ТЗ — scan_and_compress_history(chat_history, batch_size=20,
        delay_ms=1500): список сообщений и оба параметра подходят как есть.
        batch_size и delay_ms — переопределения конфигурации на ЭТОТ прогон
        (None — из MemoryConfig); после прогона config прежний.

        :param source: BatchSource или просто список MemoryMessage.
        :param cancel: проверяется МЕЖДУ пакетами — начатый запрос к модели
            доводится, а слитый пакет сохраняется. Паузы между запросами и
            перед повторами отмена прерывает сразу, и новых запросов после неё
            нет (_pause): пакет, чьё слияние не успело, не пишется, у слитого
            пропускается только сжатие.
        :param max_batches: сколько попыток, дошедших до commit (принятых и
            отвергнутых), сделать за прогон; ежеходный проход так не занимает
            модель надолго.
        :param retry_conflicts: False — отвергнутый commit завершает прогон
            статусом "conflict" (ежеходный проход: следующий ход начнёт
            заново); True — пакет собирается заново, но не больше
            _MAX_CONFLICTS отказов подряд (задание).

        Исключения merge_block пробрасываются: что они значат, решает
        вызывающий (задание → статус error, ежеходный проход → лог).
        """
        if not isinstance(source, BatchSource):
            source = ListSource(source)
        saved_config = self.config
        overrides = {k: v for k, v in (("batch_size", batch_size), ("delay_ms", delay_ms))
                     if v is not None}
        if overrides:
            self.config = replace(self.config, **overrides)
        # Прогон отдаёт только свои предупреждения, даже если менеджер уже
        # поработал раньше; повторы _warn тоже ищет только среди них.
        warn_from = self._warn_from = len(self.warnings)
        processed = batches = attempts = conflicts = 0
        self._on_progress = on_progress
        self._cancel = cancel
        try:
            self._progress = ScanProgress(processed=0, total=await source.pending(), batches=0,
                                          state_tokens=self._tokens(state), phase="merge")
            self._emit()
            while True:
                if cancel is not None and cancel.is_set():
                    status = "cancelled"
                    break
                if max_batches is not None and attempts >= max_batches:
                    status = "limit"
                    break
                state = await source.current_state(state)
                batch = self.plan_batch(await source.next_messages(self.config.batch_size))
                if batch is None:
                    status = "done"
                    break
                try:
                    new = await self.merge_block(state, batch)
                except MemoryCancelled:
                    # Отмена прервала паузу до слияния (или повтор после
                    # обрыва): писать нечего, готовые пакеты уже в source.
                    status = "cancelled"
                    break
                attempts += 1
                if not await source.commit(batch, new):
                    if not retry_conflicts:
                        status = "conflict"
                        break
                    conflicts += 1
                    if conflicts > _MAX_CONFLICTS:
                        raise SourceConflictError(
                            f"кусок истории #{batch.first_id}–#{batch.last_id} менялся "
                            f"{conflicts} раза подряд, пока модель его сворачивала")
                    continue
                conflicts = 0
                state = new
                processed += len(batch.messages)
                batches += 1
                self._progress = replace(
                    self._progress, processed=processed,
                    total=processed + await source.pending(), batches=batches,
                    state_tokens=self._tokens(state), last_range=(batch.first_id, batch.last_id))
                self._emit()
            tokens = self._tokens(state)
            self._progress = replace(self._progress, state_tokens=tokens)
            self._emit(phase="done")
            return ScanResult(state=state, processed=processed, batches=batches, status=status,
                              state_tokens=tokens, warnings=self.warnings[warn_from:])
        finally:
            self._on_progress = None
            self._progress = None
            self._cancel = None
            self.config = saved_config
            # Отсчёт повторов — только на время прогона. Иначе прямой вызов
            # merge_block/compact после прогона сверял бы свои предупреждения с
            # предупреждениями прогона и молча не добавлял уже выданное им.
            self._warn_from = len(self.warnings)


# ============================================================================
# Скользящее окно (Tier 3)
# ============================================================================
def _window_args(window, covered) -> tuple[int, int]:
    """
    Окно и указатель «учтено до» из настроек и БД → int: окно не число —
    DEFAULT_WINDOW, указатель не число — 0 («сводки нет»). Один помощник на
    window_start и SlidingWindow.pending_for_summary: пойми они мусор
    по-разному, дословное окно и «что ждёт сжатия» разошлись бы.
    """
    try:
        window = int(window)
    except (TypeError, ValueError):
        window = DEFAULT_WINDOW
    try:
        covered = int(covered or 0)
    except (TypeError, ValueError):
        covered = 0
    return window, covered


def window_start(history_ids, covered, window, step: int = WINDOW_STEP) -> int:
    """
    Индекс первого элемента истории, который уходит в модель дословно.

    :param history_ids: id сообщений по элементам истории (None — id неизвестен).
    :param covered: id последнего сообщения, учтённого сводкой (0 — сводки нет).
    :param window: сколько последних сообщений держать всегда; <= 0 — вся история.

    Выбрасывать можно ТОЛЬКО то, что уже в сводке. Раньше подобные окна резали
    историю по счёту, и в импортированном чате, где сводка ещё не догнала
    переписку, середина чата пропадала бесследно: ни дословно, ни пересказом.
    Сообщение с неизвестным id считаем неучтённым — на нём отбрасывание
    останавливается.

    Перенесено из horae_recall без изменений поведения (приведение аргументов
    вынесено в _window_args); horae_recall.window_start — реэкспорт (тесты
    подменяют именно его).
    """
    n = len(history_ids or [])
    window, covered = _window_args(window, covered)
    if window <= 0 or n <= window:
        return 0
    limit = n - window
    drop = 0
    while drop < limit:
        mid = history_ids[drop]
        if mid is None or mid > covered:
            break
        drop += 1
    if step and step > 1:
        drop -= drop % step
    return drop


class SlidingWindow:
    """
    Обёртка над window_start для встраивания вне TaleEngine: строгий FIFO —
    сообщения старше окна идут в модель только через снимок, последние
    window — дословно.
    """

    def __init__(self, window: int, step: int = WINDOW_STEP):
        self.window = window
        self.step = step

    def start(self, ids, covered) -> int:
        return window_start(ids, covered, self.window, self.step)

    def split(self, ids, covered) -> tuple[list, list]:
        """→ (выброшенные id — они уже в снимке, id дословного окна)."""
        ids = list(ids or ())
        cut = self.start(ids, covered)
        return ids[:cut], ids[cut:]

    def pending_for_summary(self, ids, covered) -> list:
        """Что старше окна и ещё не учтено снимком — работа для сжатия."""
        ids = list(ids or ())
        window, covered = _window_args(self.window, covered)
        if window <= 0:
            return []
        return [i for i in ids[:max(0, len(ids) - window)] if i is not None and i > covered]


# ============================================================================
# Монитор токенов и экспорт
# ============================================================================
def budget_tiers(*, system, memory, window, current, budget, model_limit) -> dict:
    """Суммы уровней контекста и их доли от бюджета хода и от лимита модели (%)."""
    tiers = {
        "system": int(system or 0),
        "memory": int(memory or 0),
        "window": int(window or 0),
        "current": int(current or 0),
    }
    total = sum(tiers.values())
    budget = int(budget or 0)
    model_limit = int(model_limit or 0)
    return {
        **tiers,
        "total": total,
        "budget": budget,
        "model_limit": model_limit,
        "pct_budget": round(total * 100 / budget, 1) if budget > 0 else 0.0,
        "pct_limit": round(total * 100 / model_limit, 1) if model_limit > 0 else 0.0,
    }


def render_export_markdown(*, title, character, snapshot, covered_upto, messages_total,
                           tokens, budget, schema, exported_at, facts=None) -> str:
    """
    Мастер-снимок как .md-файл: шапка с метаданными, сам снимок и — только если
    facts не None — приложение с атомарными фактами (по одному в строку).
    """
    covered = f"#{covered_upto}" if covered_upto else "—"
    lines = [
        f"# Мастер-снимок памяти — «{title or 'без названия'}»",
        "",
        f"- Персонаж: {character or '—'} · Сообщений в чате: {_fmt_int(messages_total)}"
        f" · Учтено до: {covered}",
        f"- Размер: {_fmt_int(tokens)} из {_fmt_int(budget)} токенов · Схема: {schema or '—'}"
        f" · Выгружено: {exported_at}",
        "",
        "---",
        "",
        str(snapshot or "").strip() or EMPTY_STATE,
    ]
    if facts is not None:
        # Пустые строки отбрасываются ДО подсчёта: иначе заголовок обещал бы
        # «(3)», а под ним стоял бы один пункт.
        facts = [f for f in (" ".join(str(f).split()) for f in facts) if f]
        lines += ["", "---", "", f"## Приложение: атомарные факты ({len(facts)})"]
        lines += [f"- {f}" for f in facts] or ["—"]
    return "\n".join(lines) + "\n"


# ============================================================================
# Промпты (§5)
# ============================================================================
# Один промпт и для ежеходного инкремента, и для полной пересборки: снимок,
# собранный кнопкой «Пересобрать», и снимок, дописанный после хода, обязаны
# быть одной схемы. Метки [Текущая память] и [Новые события #a–#b] — те же,
# что в запросе слияния (их формирует менеджер).
MASTER_STATE_PROMPT = f"""Ты — компрессор долговременной памяти ролевого чата. Твоя задача — не пересказ, а МАСТЕР-СНИМОК по строгой схеме: СНИМОК_N = слияние(СНИМОК_{{N-1}}, НОВЫЙ БЛОК). Снимок заменяет модели всю старую часть переписки: всё, что ты потеряешь, исчезнет из памяти навсегда.

ВХОД
- [Текущая память] — проверенные данные, только для чтения. Не перефразируй, не сокращай и не «улучшай» то, чего новый блок не касается, — переноси дословно, символ в символ. «{EMPTY_STATE}» — памяти ещё нет, собери снимок с нуля.
- [Новые события #a–#b] — сообщения чата по порядку, строка вида «[#id · время · автор] текст». Строка «📎 …» — вложения (изображение, аудио, видео, документ): сам файл ты не видишь, что в нём — известно только из текста реплик.

ЖЁСТКИЕ ПРАВИЛА
1. Только то, что прямо сказано в новом блоке или уже есть в снимке. Не додумывай чувства, мотивы, даты, числа, имена. Неясное помечай «(?)»; время не указано — пиши «время не указано». Никогда не выдумывай конкретное время.
2. Имена, названия, числа, цитаты, названия произведений и треков — символ в символ, без синонимов и переводов. Цитаты — в «кавычках» с указанием автора.
3. Ни одна запись разделов [{SEC_CHARACTERS}], [{SEC_REGISTRY}] и [{SEC_LISTS}] не исчезает. Изменилось — обнови запись на месте: новое значение, а в скобках прежнее и с какого сообщения («было: …, #id»). Утратило силу — допиши «(неактуально с #id: причина)», но не удаляй.
4. Хроника дополняется в конец в хронологическом порядке; каждая запись начинается с диапазона [#a–#b]. Старые записи и арки не переписывай.
5. Противоречия: для текущих статусов новое важнее старого; для фактов прошлого верна более конкретная запись, а расхождение отметь.
6. Конкретика вместо «воды»: кто кому что сказал, дал, пообещал и что решили.
   ❌ «договорились о встрече»
   ✅ «Эльвира пообещала Артуру встретиться у старого маяка в полночь 12 мая (#1234)»
7. Бытовые реплики без последствий не записывай. Необратимые решения, разоблачения, смерть, предательство, договоры, передачу важных предметов записывай всегда.
8. Скрытые мотивы — только если они прямо показаны в тексте, иначе «—».
9. Пустой раздел — заголовок и строка «—».
10. Каждая запись — отдельная строка, начинающаяся с «- »; в [{SEC_LISTS}] каждый список — под своим подзаголовком «### Название списка».

СХЕМА ОТВЕТА — строго она, без вступлений, пояснений и комментариев до и после:
{ENVELOPE_OPEN}
## [{SEC_CHRONICLE}]
- [#a–#b] (время сюжета, если известно) кто → что → результат/решение
## [{SEC_CHARACTERS}]
- Имя — здоровье: …; локация: …; психологический вектор: …; скрытые мотивы: …; при себе: …; отношения: …
## [{SEC_REGISTRY}]
- Сущность — что это / правило / решение (#id)
## [{SEC_LISTS}]
### Название списка
- элемент — значение/роль (#id)
{ENVELOPE_CLOSE}

САМОПРОВЕРКА ПЕРЕД ОТВЕТОМ
- все четыре раздела на месте, заголовки ровно как в схеме;
- ни одна запись разделов 2–4 из текущей памяти не пропала;
- списки не сокращены и не переупорядочены;
- нет выдуманных деталей;
- обёртка закрыта: ответ заканчивается строкой {ENVELOPE_CLOSE}."""

# Сжатие снимка сверх бюджета. Плейсхолдеры {budget} и {keep} подставляет
# менеджер через str.format — других фигурных скобок в тексте быть не должно.
# {budget} — цель сжатия (_COMPACT_TARGET от snapshot_tokens), а не сам бюджет.
COMPACT_PROMPT = f"""Снимок памяти превышает бюджет {{budget}} токенов. Сократи ТОЛЬКО раздел [{SEC_CHRONICLE}]: объедини старые записи в арки вида
- [#a–#b] Арка «название»: суть, ключевые решения, последствия
Последние {{keep}} записей хроники оставь без изменений. Диапазоны арок должны покрывать объединённые записи без пропусков; имена, числа, цитаты, решения и договорённости из объединяемых записей не теряй, ничего не выдумывай.
Разделы [{SEC_CHARACTERS}], [{SEC_REGISTRY}] и [{SEC_LISTS}] перенеси ДОСЛОВНО, символ в символ: ничего не удаляй, не сокращай и не переупорядочивай.
Ответ — полный снимок из всех четырёх разделов в обёртке {ENVELOPE_OPEN}…{ENVELOPE_CLOSE}, без пояснений."""

# Добавка к COMPACT_PROMPT, когда ответ модели упёрся в лимит длины вывода
# (merge_block → compact(fold_arcs=True)). В длинном чате хроника — «десятки
# арок + последние записи», и объединять, кроме арок, нечего: без этого
# разрешения модель не имела права сократить хронику. В обычное сжатие не
# добавляется: там арки уже свёрнуты, а поводом служат накопившиеся записи.
# Формат через str.format не проходит, но фигурных скобок в тексте тоже нет.
COMPACT_ARCS_PROMPT = """Снимок уже не помещается в лимит длины ответа, поэтому сокращай глубже: объединяй и старые арки — несколько соседних арок в одну арку более высокого уровня того же вида
- [#a–#b] Арка «название»: суть, ключевые решения, последствия
где диапазон — от начала первой объединённой арки до конца последней. Последние записи хроники по-прежнему не трогай; суть, решения и договорённости объединяемых арок сохрани."""

# Корректирующий ход после брака (§4.5): модель видит свой сырой ответ и
# список проблем. {problems} подставляет менеджер через str.format — других
# фигурных скобок в тексте быть не должно.
CORRECTION_PROMPT = (f"Ответ отклонён: {{problems}}. Верни ПОЛНЫЙ снимок заново строго по "
                     f"схеме, в обёртке {ENVELOPE_OPEN}…{ENVELOPE_CLOSE}, без пояснений.")
