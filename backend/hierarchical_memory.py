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
готовыми MemoryMessage, модель — колбэком.
Поэтому весь контракт проверяется тестами без сети и без базы
(tests/test_hierarchical_memory.py), а backend/memory_service.py — лишь
адаптер к хранилищу.
"""
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

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
MAX_SNAPSHOT_CHARS = 200_000


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
    batch_size: int = 20
    batch_max_chars: int = 80_000
    delay_ms: int = 1500
    max_retries: int = 4
    backoff_base_s: float = 2.0
    backoff_max_s: float = 60.0
    snapshot_tokens: int = 12_000
    validation_retries: int = 2
    shrink_ratio: float = 0.5          # новый снимок < 50% старого — брак
    shrink_min_tokens: int = 800       # проверку «сдувания» включаем от этого размера
    keep_recent_chronicle: int = 12    # сколько последних записей хроники не сжимать в арки


def _fmt_int(n) -> str:
    """Число с разделителем тысяч — неразрывным пробелом, чтобы «4 200» не
    разрывалось переносом строки в прогресс-баре."""
    try:
        return f"{int(n):,}".replace(",", "\u00a0")
    except (TypeError, ValueError):
        return str(n)


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


# ============================================================================
# Нормализация сообщения
# ============================================================================
# Скрытые рассуждения моделей не часть реплики. Вырезаются по всему тексту,
# даже внутри блока кода: мысли не бывают содержимым, а их размер сопоставим
# с самим ответом.
_THINK_RE = re.compile(r"<(think|thinking)\b[^>]*>[\s\S]*?</\1\s*>", re.IGNORECASE)
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
_STYLE_SCRIPT_RE = re.compile(r"<(style|script)\b[^>]*>[\s\S]*?</\1\s*>|<!--[\s\S]*?-->",
                              re.IGNORECASE)
# Теги, после которых в отображаемом тексте был бы перенос строки.
_BREAK_RE = re.compile(r"<br\s*/?>|</(?:p|div|li|tr|h[1-6]|blockquote)\s*>", re.IGNORECASE)
# Прочие HTML-теги (оформление карточек SillyTavern, <span style=…>): снимаем
# сам тег, внутренний текст остаётся. Имя тега обязано начинаться с буквы,
# поэтому «<3» и «a < b» не задеваются.
_TAG_RE = re.compile(r"</?[A-Za-z][\w:-]*(?:\s[^<>]*)?/?>")
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


def _strip_markup(text: str) -> str:
    """Снять HTML-оформление вне блоков кода."""
    text = _STYLE_SCRIPT_RE.sub("", text)
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
    text = _THINK_RE.sub("", text)
    text = _DATA_URI_RE.sub("", text)
    text = _B64_RUN_RE.sub(_drop_base64, text)
    text = _outside_code(text, _strip_markup)
    text = _BLANK_LINES_RE.sub("\n\n", text)
    return text.strip()


def _attachments_line(attachments) -> str:
    """«изображение «map.png»; аудио «voice.ogg»» — только подписи, без содержимого.

    Что внутри медиа, модель узнаёт из текста реплик: ответ ассистента,
    разбирающего файл, идёт в тот же пакет.
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
    строк, и одно гигантское сообщение не должно его переполнять.
    """
    keep = max(0, max_chars - len(LONG_MESSAGE_MARK))
    head = keep // 2
    tail = keep - head
    return line[:head] + LONG_MESSAGE_MARK + (line[len(line) - tail:] if tail else "")


def normalize_message(msg: MemoryMessage, max_chars: int = 80_000) -> str:
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
    line = f"[{' · '.join(head)}]" + (f" {body}" if body else "")
    if files:
        line += f"\n📎 {files}"
    if max_chars and max_chars > 0 and len(line) > max_chars:
        line = _cut_middle(line, max_chars)
    return line


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
_UNCLOSED_THINK_RE = re.compile(r"<(?:think|thinking)\b[^>]*>[\s\S]*$", re.IGNORECASE)
_FENCE_LINE_RE = re.compile(r"^[ \t]*```[\w-]*[ \t]*$", re.MULTILINE)
_OPEN_RE = re.compile(re.escape(ENVELOPE_OPEN), re.IGNORECASE)
_CLOSE_RE = re.compile(re.escape(ENVELOPE_CLOSE), re.IGNORECASE)


def extract_snapshot(raw) -> tuple[str, bool]:
    """
    Текст снимка из сырого ответа → (текст, обрезан ли).

    Мысли (<think>) и ограждения ``` вырезаются. Есть обёртка — берём её
    содержимое; открыта, но не закрыта — ответ упёрся в лимит вывода, текст
    после открытия возвращается с флагом True. Обёртки нет вовсе — мягкий
    режим: весь текст (разделы потом проверит parse_sections).
    """
    text = _THINK_RE.sub("", str(raw or ""))
    # Незакрытый <think> — модель оборвалась посреди рассуждений: всё после него мысли.
    text = _UNCLOSED_THINK_RE.sub("", text)
    text = _FENCE_LINE_RE.sub("", text)
    opened = _OPEN_RE.search(text)
    if opened:
        closed = _CLOSE_RE.search(text, opened.end())
        if not closed:
            return text[opened.end():].strip(), True
        return text[opened.end():closed.start()].strip(), False
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
    if len(text) > MAX_SNAPSHOT_CHARS:
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


def _heading_key(line: str) -> str:
    return _norm_key(line.strip().lstrip("#"))


def _jaccard(a: set, b: set) -> float:
    union = a | b
    return len(a & b) / len(union) if union else 0.0


def _similar(key: str, keys: set, token_sets: list) -> bool:
    """Точное совпадение ключа или Жаккар по словам ≥ 0.6 (переименование «Орден
    Зари» → «Тайный орден Зари» — та же запись, а не пропавшая)."""
    if key in keys:
        return True
    tokens = set(key.split())
    return any(_jaccard(tokens, other) >= _JACCARD_MIN for other in token_sets)


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

    Если в группе нет ни одного маркера, делить не по чему — каждая строка
    своя запись. Строки до первого маркера делятся так же: продолжать им нечего.
    """
    entries: list[list[str]] = []
    in_entry = False  # встретился ли уже маркер: есть ли что продолжать
    for line in lines:
        if not line.strip():
            continue
        if _ENTRY_MARKER_RE.match(line):
            entries.append([line])
            in_entry = True
        elif in_entry:
            entries[-1].append(line)
        else:
            entries.append([line])
    return entries


def _group_keys(groups) -> list[str]:
    return [k for _, lines in groups for e in _parse_entries(lines) if (k := _entry_key(e[0]))]


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
    «артем — здоров (было: ранен)») он не дублирует: ключ тот же.

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
        keys = _group_keys(groups)
        key_set, token_sets = set(keys), [set(k.split()) for k in keys]
        lost = 0
        for heading, lines in _split_groups(old_body):
            for entry in _parse_entries(lines):
                key = _entry_key(entry[0])
                if not key or _similar(key, key_set, token_sets):
                    continue
                target = _target_group(groups, heading)[1]
                while target and not target[-1].strip():
                    target.pop()
                target.extend(entry)
                restored.append(key)
                lost += 1
        if lost:
            new_secs[sec] = _render_groups(groups)
    return render_snapshot(new_secs), restored


# ============================================================================
# Скользящее окно (Tier 3)
# ============================================================================
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

    Перенесено из horae_recall без изменений; horae_recall.window_start —
    реэкспорт (тесты подменяют именно его).
    """
    n = len(history_ids or [])
    try:
        window = int(window)
    except (TypeError, ValueError):
        window = DEFAULT_WINDOW
    if window <= 0 or n <= window:
        return 0
    try:
        covered = int(covered or 0)
    except (TypeError, ValueError):
        covered = 0
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
        try:
            window = int(self.window)
        except (TypeError, ValueError):
            window = DEFAULT_WINDOW
        if window <= 0:
            return []
        try:
            covered = int(covered or 0)
        except (TypeError, ValueError):
            covered = 0
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
        facts = [" ".join(str(f).split()) for f in facts]
        lines += ["", "---", "", f"## Приложение: атомарные факты ({len(facts)})"]
        lines += [f"- {f}" for f in facts if f] or ["—"]
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
COMPACT_PROMPT = f"""Снимок памяти превышает бюджет {{budget}} токенов. Сократи ТОЛЬКО раздел [{SEC_CHRONICLE}]: объедини старые записи в арки вида
- [#a–#b] Арка «название»: суть, ключевые решения, последствия
Последние {{keep}} записей хроники оставь без изменений. Диапазоны арок должны покрывать объединённые записи без пропусков; имена, числа, цитаты, решения и договорённости из объединяемых записей не теряй, ничего не выдумывай.
Разделы [{SEC_CHARACTERS}], [{SEC_REGISTRY}] и [{SEC_LISTS}] перенеси ДОСЛОВНО, символ в символ: ничего не удаляй, не сокращай и не переупорядочивай.
Ответ — полный снимок из всех четырёх разделов в обёртке {ENVELOPE_OPEN}…{ENVELOPE_CLOSE}, без пояснений."""
