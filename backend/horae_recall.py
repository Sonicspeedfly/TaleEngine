"""
Долгая память Horae для длинных чатов (вплоть до ~1M токенов).

Проблема, ради которой модуль заведён: в длинном чате модель получала ВСЮ
переписку, насколько хватало бюджета, и тонула в ней (Context Dilution). Ключевые
факты и хронология из начала чата размывались сотнями реплик, а то, что не влезло
в бюджет, просто отрезалось — вместе с обещанием, данным на сотом сообщении.

Теперь контекст собирается трёхслойным конвейером:

  1. Активное окно (Active Sliding Window) — последние DEFAULT_WINDOW сообщений
     уходят в модель дословно, без изменений (см. window_start). Всё, что старше,
     выбрасывается, но ТОЛЬКО если уже учтено скользящей сводкой: пока сводка
     догоняет длинный или импортированный чат, модель видит переписку целиком.

  2. Скользящая сводка (Rolling Hierarchical Compressor) — фоном, каждые
     summary_every сообщений, быстрая модель дописывает инкрементальную хронику:
     состояние сюжета и цель, решения и события, статусы персонажей и инвентарь
     (main._maybe_update_summary). В контекст она идёт блоком
     [ХРОНИКА И СОСТОЯНИЕ ЧАТА] у конца (horae_memory.assemble_context).

  3. Семантическая память фактов (Horae Vector Memory Engine) — этот модуль.
     В тот же фоновый проход модель выписывает из свежего куска переписки
     АТОМАРНЫЕ факты («Эльвира пообещала Артуру вернуть кинжал»), они
     индексируются эмбеддингами (таблица horae_facts). На каждом ходу ищем
     до TOP_K фактов, похожих на текущую реплику, с порогом сходства и поправкой
     на свежесть, и кладём их блоком [HORAE RECALLED MEMORY: …]. Нет похожих —
     нет и блока: пустой или натянутый блок памяти модель охотно «дополняет»
     выдумкой.

Индексируются именно факты, а не сырые реплики: реплика ролевого чата — это
абзац действия, реплик и настроения, её вектор «про всё сразу» и похож на всё
понемногу. Факт — одно утверждение с именами, его вектор точен.

Эмбеддинги необязательны. Модель эмбеддингов в подключении по умолчанию пустая,
и тогда поиск идёт по словам (lexical_similarities): основы слов, IDF по фактам
этого чата, вес имён. Это режим большинства пользователей, поэтому он настроен
на точность, а не на полноту.
"""
import json
import logging
import math
import re
from array import array
from bisect import bisect_right
from collections import Counter, OrderedDict
from functools import lru_cache
from operator import mul

logger = logging.getLogger("aichat.horae")

# ---- Слой 1: активное окно -------------------------------------------------

# Сколько последних сообщений идёт в модель дословно. Требование — 15–25 реплик;
# 20 — середина. Клиент показывает тот же дефолт, держите значения равными.
DEFAULT_WINDOW = 20
# Граница окна двигается ступенями, а не на каждом сообщении. Иначе начало
# истории сдвигалось бы каждый ход, и кэш промпта провайдера (скидка 75–90% на
# вход) не попадал бы никогда — та же причина, что у stable_trim_start. Дословно
# модель видит от DEFAULT_WINDOW до DEFAULT_WINDOW + WINDOW_STEP - 1 сообщений.
WINDOW_STEP = 4
# Версия формата авто-сводки (ключ "v" в HoraeEntry.meta). Сводка до 2.4.0
# резала каждую реплику до 1500 символов, а большой бэклог — до первых 24 000
# символов, и всё равно ставила указатель «учтено до» на последнее сообщение.
# Такому указателю окно верить не может: оно выбросило бы из контекста хвосты
# длинных ответов и целые куски импортированных чатов, которых в сводке нет.
# Старую сводку пересобирает новый сводчик (см. main._summary_pass), и только
# его указатель — с этой версией — окно считает правдой.
SUMMARY_FORMAT = 2

# ---- Слой 3: отбор фактов --------------------------------------------------

# Порог косинусного сходства векторов (из требований). Ниже — факт считается
# не относящимся к текущему моменту, сколько бы свежим он ни был.
SIM_THRESHOLD = 0.75
# Порог для поиска по словам. С косинусом векторов НЕ сравним: это доля «веса»
# факта (IDF × вес имени), названная в текущем разговоре. 0.2 — одно имя из
# факта средней длины проходит, одно общее слово («вернуть», «дом») — нет.
LEXICAL_THRESHOLD = 0.2
# Слова, записанные с заглавной посреди предложения (имена, места), весят
# больше: вопрос «где Ирвен?» — это вопрос про Ирвена, а не про глагол из факта.
ENTITY_BOOST = 2.5
# «Вездесущее» слово — то, что стоит хотя бы в UBIQUITOUS_MIN_DF фактах и в
# UBIQUITOUS_SHARE всех фактов чата (обычно имена главных героев). Оно ничего не
# выделяет: одно совпадение по нему факт не вспоминает, и вес имени ему не
# положен. Минимум по числу фактов — чтобы в начале чата, пока фактов пять,
# второе упоминание имени не делало его «вездесущим».
UBIQUITOUS_SHARE = 0.25
UBIQUITOUS_MIN_DF = 3
# Вес хвоста разговора относительно самой реплики: хвост помогает понять, о чём
# речь, но факт должен касаться того, что пользователь написал сейчас.
TAIL_WEIGHT = 0.3
# Сколько фактов максимум уходит в контекст за ход.
TOP_K = 5
# Поправка на свежесть: вклад сходства падает вдвое за HALF_LIFE_MESSAGES
# сообщений, но не ниже RECENCY_FLOOR. Пол обязателен: обещание с 50-го
# сообщения должно вспоминаться и на 5000-м — свежесть лишь решает, кто выше
# при равном сходстве, а не вычёркивает старое.
HALF_LIFE_MESSAGES = 300
RECENCY_FLOOR = 0.5

# ---- Извлечение и хранение фактов ------------------------------------------

MAX_FACTS_PER_CHUNK = 20
MAX_FACT_CHARS = 300
# Почти-дубли: факт с косинусом >= DUP_COSINE к уже известному не сохраняется
# (модель на каждом куске заново пересказывает «Эльвира — дочь мельника»).
# Сравниваем с последними DUP_SCAN_RECENT фактами: дубли почти всегда рядом, а
# полный перебор тысяч векторов в чистом Python стоил бы секунды на каждый проход.
DUP_COSINE = 0.95
DUP_SCAN_RECENT = 400
# Сколько старых фактов за один запрос получают вектор новой модели эмбеддингов,
# и сколько таких запросов максимум за один фоновый запуск (см. backfill_all).
BACKFILL_BATCH = 50
BACKFILL_MAX_BATCHES = 10
# Поиск по векторам включается, только когда векторы текущей модели есть почти
# у всех фактов. Иначе, пока старые факты досчитываются, поиск шёл бы по одной
# свежей горстке с векторами, а долгая память чата была бы недоступна.
VECTOR_COVERAGE = 0.95
# Запрос к памяти длиннее этого режем: вектор длинного текста «размазан».
QUERY_CHARS = 2000

FACTS_PROMPT = (
    "Ты извлекаешь АТОМАРНЫЕ ФАКТЫ из фрагмента ролевого чата для долговременной "
    "памяти. Верни ТОЛЬКО факты, по одному на строку, без нумерации, заголовков, "
    "вступлений и пояснений.\n\n"
    "Правила:\n"
    "- Один факт — одно утверждение: кто есть кто, отношения, обещания и долги, "
    "предметы и у кого они (инвентарь), принятые решения, состояния и раны, места "
    "и их особенности, тайны.\n"
    "- Каждый факт понятен БЕЗ остального текста: называй всех по именам, никаких "
    "местоимений («он», «она», «там», «это»).\n"
    "- Только то, что прямо произошло или сказано во фрагменте. Никаких догадок, "
    "оценок и пересказа атмосферы.\n"
    "- Бери то, что может понадобиться сюжету позже; пустяковые реплики пропускай.\n"
    f"- Не больше {MAX_FACTS_PER_CHUNK} фактов.\n"
    "- Пиши на языке переписки.\n"
    "- Если значимых фактов нет — верни пустой ответ."
)


# ============================================================================
# Слой 1: активное окно
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


def trusted_pointer(meta) -> int:
    """
    Указатель «учтено до», которому может верить окно: только у сводки
    текущего формата (SUMMARY_FORMAT). У сводки старого формата — 0, то есть
    окно не выбрасывает ничего, и модель видит историю так же, как до 2.4.0,
    пока новый сводчик не пересоберёт память.
    """
    if not isinstance(meta, dict) or meta.get("v") != SUMMARY_FORMAT:
        return 0
    try:
        return max(0, int(meta.get("last_message_id") or 0))
    except (TypeError, ValueError):
        return 0


# ============================================================================
# Слой 3: разбор ответа модели с фактами
# ============================================================================
_BULLET_RE = re.compile(r"^\s*(?:[-*•·–—>]+|\(?\d{1,3}[.)]|#+)\s*")
_FENCE_RE = re.compile(r"```[\w-]*")
# Ответы вида «фактов нет» — это не факт, хранить их нельзя: такой «факт» потом
# вспоминался бы на каждое «нет» в разговоре.
_NO_FACTS = frozenset({
    "нет", "нет фактов", "нет новых фактов", "фактов нет", "значимых фактов нет",
    "нет значимых фактов", "none", "no facts", "no new facts", "n a",
})


def _norm_key(text: str) -> str:
    """Ключ для сравнения фактов: регистр, «ё», пунктуация и пробелы не важны."""
    t = (text or "").lower().replace("ё", "е")
    t = re.sub(r"[^\w\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def parse_facts(text) -> list[str]:
    """
    Список фактов из ответа модели.

    Модели не держат формат: одна нумерует, другая ставит маркеры, третья
    заворачивает всё в ```json [...]```, четвёртая добавляет «Вот факты:». Всё
    это снимаем, дубли (без учёта регистра) выкидываем, длину и число режем:
    раздутый факт — уже не атомарный, а сотня фактов с куска — мусор.
    """
    raw = _FENCE_RE.sub("", str(text or "")).strip()
    if not raw:
        return []
    items: list | None = None
    if raw[0] in "[{":
        try:
            data = json.loads(raw)
        except ValueError:
            data = None
        if isinstance(data, dict):
            data = data.get("facts") or data.get("факты")
        if isinstance(data, list):
            items = []
            for it in data:
                if isinstance(it, dict):
                    it = it.get("fact") or it.get("content") or it.get("text") or ""
                items.append(str(it))
    if items is None:
        items = raw.splitlines()

    out: list[str] = []
    seen: set[str] = set()
    for it in items:
        s = _BULLET_RE.sub("", str(it)).replace("**", "").replace("__", "")
        s = re.sub(r"\s+", " ", s).strip().strip("\"'`«»“”,").strip()
        if not s or s.endswith(":"):
            continue  # пусто или заголовок («Факты:»)
        key = _norm_key(s)
        if len(key.split()) < 2 or key in _NO_FACTS:
            continue
        if len(s) > MAX_FACT_CHARS:
            s = s[:MAX_FACT_CHARS].rstrip() + "…"
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
        if len(out) >= MAX_FACTS_PER_CHUNK:
            break
    return out


# ============================================================================
# Слой 3: сходство и ранжирование (чистые функции — без БД и сети)
# ============================================================================
_WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)

# ---- Основы слов ----
# Раньше основой были первые пять букв без последней гласной. Короткие имена —
# самые частые в ролевых чатах — при этом не склонялись вовсе: «Ира/Иру»,
# «Аня/Аней», «Кай/Каем» давали разные основы, и вопрос «Спроси Иру про амулет»
# не находил факт «Ира отдала Лису амулет». Приставка ломала совпадение так же:
# «обещала» и «пообещала» не сходились. Теперь с конца срезается НАСТОЯЩЕЕ
# окончание (самое длинное подходящее), у короткого слова основа может остаться
# в две буквы («Аня» → «ан», «Кай» → «ка»), а приставка и беглое «ер» дают
# основе запасные формы (см. _forms).
#
# Именные окончания могут оставить основу в две буквы — иначе короткие имена
# снова не склоняются. Глагольные — не короче трёх: они совпадают с концом
# множества существительных («амулЕТ», «кинжАЛ»), и «амулет» → «ам» склеил бы
# половину словаря.
_NOUN_ENDINGS = (
    "ами", "ями", "ого", "его", "ому", "ему", "ыми", "ими", "иям", "иях", "ией",
    "ой", "ей", "ом", "ем", "ов", "ев", "ах", "ях", "ам", "ям", "ью", "ию",
    "ии", "ие", "ия", "ий", "ый", "ая", "яя", "ое", "ее", "ые", "ую", "юю",
    "ым", "им", "ых", "их",
    "а", "я", "у", "ю", "ы", "и", "е", "о", "й", "ь",
)
_VERB_ENDINGS = (
    "ешь", "ишь", "ете", "ите", "ает", "яет", "еет", "ует", "ают", "яют", "еют",
    "уют", "ала", "яла", "ила", "ыла", "ела", "али", "яли", "или", "ыли", "ели",
    "ало", "ило", "ать", "ять", "ить", "еть", "уть", "ыть",
    "ет", "ит", "ут", "ют", "ат", "ят", "ла", "ло", "ли", "ал", "ял", "ил", "ыл",
    "ел", "ть", "ти",
)
# (окончание, минимальная длина оставшейся основы) — длинные окончания первыми.
_ENDINGS = sorted(
    [(e, 2) for e in _NOUN_ENDINGS] + [(e, 3) for e in _VERB_ENDINGS],
    key=lambda pair: -len(pair[0]),
)
# Приставки глаголов: «пообещала» ↔ «обещала». Запасная форма без приставки
# появляется, только если остаток не короче четырёх букв: иначе «поле» стало бы
# «ле», а «замок» — «мок».
_VERB_PREFIXES = ("пере", "при", "про", "под", "раз", "рас", "от", "об", "вы",
                  "за", "на", "по", "до", "из")
# Беглое «ер»: «мать → матери», «дочь → дочери». Общим правилом его не срезать —
# «вечер», «ветер», «Питер» склеились бы с чужими словами; а слов таких два.
_FLEETING = {"матер": "мат", "дочер": "доч"}
_SENT_END = ".!?…\n"
_GAP_STRIP = " \t\r\"'«»„“”‘’()[]—–-"

_STOPWORDS = frozenset("""
это эта этот эти этой этого этому этим этих эту том тому того тем теми той
тот такой такая такое такие так также тоже весь вся всё все всем всех всеми
всего всю ещё еще уже уж или либо если когда тогда потом теперь сейчас здесь
там тут где куда откуда зачем почему потому чтобы чтоб что чего чем кто кого
кому как какой какая какое какие который которая которое которые которого
которой которую которых нибудь было были была будет будут быть есть нет даже
очень только лишь ведь вот ну нее неё него ней ним ними них его её ему она они
оно мне меня мной мой моя моё мои мою моего моей твой твоя твоё твои тебя тебе
тобой свой своя своё свои свою своего своей своим себя себе собой нас нам нами
вас вам вами наш наша наше наши ваш ваша ваше ваши для без под над при про
через после перед между около ради вдруг опять снова тоже может можно нельзя
надо нужно просто совсем почти более менее больше меньше хоть хотя пока разве
the and for are but not you all any can had her was one our out has him his how
its may new now old see two who did get let she too use that with have this will
your from they know want been good much some time very when come here just like
long make many more only over such take than them well were what into then there
these those would could should about after again which while where their also
""".replace("ё", "е").split())  # слова сравниваются уже с «е» вместо «ё», см. _analyze


@lru_cache(maxsize=16384)
def _stem(word: str) -> str:
    """Основа слова (слово уже в нижнем регистре и с «е» вместо «ё»)."""
    if word.isascii():
        # Английский: только множественное число, как и раньше.
        if len(word) >= 4 and word.endswith("s") and not word.endswith("ss"):
            return word[:-1]
        return word
    w = word
    # Возвратная частица: «старался» → «старал», «вернуться» → «вернуть».
    if len(w) >= 5 and w.endswith(("ся", "сь")):
        w = w[:-2]
    for end, keep in _ENDINGS:
        if w.endswith(end) and len(w) - len(end) >= keep:
            return w[:-len(end)]
    return w


@lru_cache(maxsize=16384)
def _forms(stem: str) -> tuple[frozenset, frozenset]:
    """
    Формы основы для сравнения: (полные, усечённые).

    Полные — сама основа, основа без приставки и замена беглого «ер». Усечённые —
    полные без ещё одного окончания. Окончание срезается однажды, и «кинжал»
    (→ «кинж», глагольное «-ал») без этого не сошёлся бы с «кинжалом» (→ «кинжал»).
    Усечённые формы сравниваются только с ПОЛНЫМИ формами другого слова, а не
    друг с другом: иначе «страж» и «страх» сошлись бы на «стра».
    """
    full = {stem}
    if stem in _FLEETING:
        full.add(_FLEETING[stem])
    for pre in _VERB_PREFIXES:
        if stem.startswith(pre) and len(stem) - len(pre) >= 4:
            full.add(stem[len(pre):])
            break
    cut = set()
    for f in full:
        for end, _keep in _ENDINGS:
            if f.endswith(end) and len(f) - len(end) >= 3:
                cut.add(f[:-len(end)])
    return frozenset(full), frozenset(cut - full)


@lru_cache(maxsize=8192)
def _analyze(text: str) -> tuple[frozenset, frozenset]:
    """
    (основы значимых слов, основы слов с заглавной ВНУТРИ предложения).

    Заглавная в начале предложения ни о чём не говорит. Раньше именем считалось
    любое слово с заглавной, и факт «Старый маг Ирвен знает тайну медальона»
    получал вес имени у слова «старый» — реплика «Он старался не шуметь»
    вспоминала его. Имя узнаётся по заглавной посреди предложения — в любом
    факте чата или в самом разговоре (см. lexical_similarities). Результат
    кэшируется: факты чата разбираются на каждом ходу, а меняются редко.
    """
    t = (text or "").replace("ё", "е").replace("Ё", "Е")
    stems: set[str] = set()
    caps: set[str] = set()
    prev = ""  # последний значимый символ перед словом; "" — начало текста
    pos = 0
    for m in _WORD_RE.finditer(t):
        gap = t[pos:m.start()].strip(_GAP_STRIP)
        if gap:
            prev = gap[-1]
        pos = m.end()
        w = m.group()
        low = w.lower()
        mid_sentence = prev != "" and prev not in _SENT_END
        prev = "a"  # за словом — середина предложения
        if len(low) < 3 or low in _STOPWORDS:
            continue
        s = _stem(low)
        stems.add(s)
        if mid_sentence and w[0].isupper():
            caps.add(s)
    return frozenset(stems), frozenset(caps)


def terms(text: str) -> set[str]:
    """Основы значимых слов текста: без стоп-слов и слов короче 3 букв."""
    return set(_analyze(text)[0])


def _keys(stems) -> tuple[set, set]:
    """Полные и усечённые формы всех основ запроса — для быстрого сравнения."""
    full: set[str] = set()
    cut: set[str] = set()
    for s in stems:
        f, c = _forms(s)
        full |= f
        cut |= c
    return full, cut


def _hit(stem: str, keys: tuple[set, set]) -> bool:
    full, cut = keys
    f, c = _forms(stem)
    return bool(f & full or f & cut or c & full)


def lexical_similarities(query: str, facts: list[dict], context: str = "") -> list[float]:
    """
    Сходство «по словам» каждого факта с запросом, в [0, 1].

    Это доля веса факта, которую называет разговор. Вес слова — его IDF среди
    фактов ЭТОГО чата, у имён — ещё и ENTITY_BOOST.

    :param query: реплика пользователя — именно её факт и должен касаться.
    :param context: хвост разговора. Он лишь помогает понять реплику и весит
        TAIL_WEIGHT; если в реплике нет ни одного значимого слова («ок», «а что
        с ним?»), факт ищется по хвосту целиком.

    Раньше реплика и хвост шли одним текстом. В ролевом чате прошлая реплика
    персонажа почти всегда называет главных героев, и совпадение по одним лишь
    их именам проходило порог в любом коротком факте: на «ок» блок памяти
    заполнялся пятью посторонними фактами про героев почти на каждом ходу, и
    с ростом чата это не проходило. Теперь совпадение засчитывается, только
    если среди совпавших слов реплики есть не «вездесущее»: слово из
    UBIQUITOUS_SHARE фактов чата и больше ничего не выделяет, и вес имени ему
    не положен. Запрос в нормировке не участвует: его длина ничего не говорит
    о том, насколько к нему относится конкретный факт.
    """
    if not facts:
        return []
    q_stems, q_caps = _analyze(query or "")
    c_stems, c_caps = _analyze(context or "")
    if not q_stems and not c_stems:
        return [0.0] * len(facts)
    analyzed = [_analyze(f.get("content") or "") for f in facts]
    n = len(facts)
    df = Counter(s for stems, _ in analyzed for s in stems)
    ubiquitous = {
        s for s, k in df.items() if k >= UBIQUITOUS_MIN_DF and k / n >= UBIQUITOUS_SHARE
    }
    entities = set(q_caps) | set(c_caps)
    for _, caps in analyzed:
        entities |= caps
    user_keys = _keys(q_stems)
    tail_keys = _keys(c_stems)

    def weight(s: str) -> float:
        # log((n+1)/df), а не log(1 + n/df): слово из КАЖДОГО факта весит почти
        # ноль, а не половину веса редкого — иначе вездесущее имя тянуло долю вверх.
        w = math.log((n + 1) / df[s])
        if s in entities and s not in ubiquitous:
            w *= ENTITY_BOOST
        return w

    sims: list[float] = []
    for stems, _ in analyzed:
        if not stems:
            sims.append(0.0)
            continue
        by_user = {s for s in stems if _hit(s, user_keys)} if q_stems else set()
        by_tail = {s for s in stems if s not in by_user and _hit(s, tail_keys)}
        if q_stems:
            if not by_user - ubiquitous:
                sims.append(0.0)
                continue
            got = sum(weight(s) for s in by_user) + TAIL_WEIGHT * sum(weight(s) for s in by_tail)
        else:
            if not by_tail - ubiquitous:
                sims.append(0.0)
                continue
            got = sum(weight(s) for s in by_tail)
        total = sum(weight(s) for s in stems)
        sims.append(min(1.0, got / total) if total else 0.0)
    return sims


def _norm(vec) -> float:
    return math.sqrt(sum(map(mul, vec, vec))) if vec else 0.0


def cosine(a, b, norm_a: float | None = None, norm_b: float | None = None) -> float:
    """Косинус двух векторов; разная размерность (разные модели) — 0."""
    if not a or not b or len(a) != len(b):
        return 0.0
    na = norm_a or _norm(a)
    nb = norm_b or _norm(b)
    if not na or not nb:
        return 0.0
    return sum(map(mul, a, b)) / (na * nb)


def recency_decay(age: int) -> float:
    """Множитель свежести: 1.0 для свежего факта, не ниже RECENCY_FLOOR для старого."""
    age = max(0, int(age or 0))
    return RECENCY_FLOOR + (1.0 - RECENCY_FLOOR) * 0.5 ** (age / HALF_LIFE_MESSAGES)


def rank_facts(
    facts: list[dict],
    newest_id: int = 0,
    *,
    query_vec=None,
    query_text: str = "",
    context_text: str = "",
    top_k: int | None = TOP_K,
    threshold: float | None = None,
) -> list[dict]:
    """
    Отбор фактов для текущего хода. Чистая функция: ни БД, ни сети.

    :param facts: [{content, source_message_id, embedding?, norm?, age?}]. age —
        сколько сообщений чата прошло после факта; нет — считается по разнице id.
    :param query_vec: вектор запроса. Задан — режим векторов: факты без вектора
        пропускаются (векторы разных моделей несопоставимы), порог SIM_THRESHOLD.
        Не задан — режим слов по query_text (реплика) и context_text (хвост
        разговора, см. lexical_similarities), порог LEXICAL_THRESHOLD.
    :param top_k: None — все факты выше порога, по убыванию score: так отбирает
        сборка контекста, которой нужно ещё отсеять факты из видимой истории.

    Порог применяется к «сырому» сходству, а порядок — по score = сходство ×
    свежесть. Иначе свежесть протаскивала бы в контекст свежий, но посторонний
    факт, а старое точное попадание срезалось бы порогом только за возраст.
    Возвращает не больше top_k словарей {content, similarity, score, source_message_id}.
    """
    if not facts or (top_k is not None and top_k <= 0):
        return []
    if query_vec is not None:
        pool = [f for f in facts if f.get("embedding")]
        qn = _norm(query_vec)
        if not pool or not qn:
            return []
        sims = [cosine(query_vec, f["embedding"], qn, f.get("norm")) for f in pool]
        cut = SIM_THRESHOLD if threshold is None else threshold
    else:
        pool = list(facts)
        sims = lexical_similarities(query_text, pool, context_text)
        cut = LEXICAL_THRESHOLD if threshold is None else threshold

    newest = max([int(newest_id or 0)] + [int(f.get("source_message_id") or 0) for f in pool])
    out: list[dict] = []
    for f, sim in zip(pool, sims):
        # Допуск на округление: косинус «ровно 0.75» в float бывает 0.7499999999.
        if sim < cut - 1e-9:
            continue
        src = int(f.get("source_message_id") or 0)
        age = f.get("age")
        if age is None:
            age = newest - src
        out.append({
            "content": (f.get("content") or "").strip(),
            "similarity": round(sim, 4),
            "score": round(sim * recency_decay(age), 4),
            "source_message_id": src,
        })
    out.sort(key=lambda r: (-r["score"], -r["source_message_id"]))
    return out if top_k is None else out[:top_k]


def render_recalled(facts: list[dict] | None) -> str:
    """
    Блок отобранных фактов для контекста. Пусто — пустая строка (блока нет).

    Факты идут в порядке событий чата, а не сходства: так модель видит
    хронологию. Формулировка намеренно осторожная: это справка, а не приказ —
    иначе модель начинает вставлять «вспомненное» в каждую реплику.
    """
    items = [f for f in (facts or []) if (f.get("content") or "").strip()]
    if not items:
        return ""
    items.sort(key=lambda f: int(f.get("source_message_id") or 0))
    body = "\n".join(f"- {f['content'].strip()}" for f in items)
    return (
        "[HORAE RECALLED MEMORY: факты из более ранней части ЭТОГО чата, похожие на "
        "то, что происходит сейчас. Опирайся на них, только если они к месту; не "
        "пересказывай их дословно и не упоминай, что это память.]\n" + body
    )


# ============================================================================
# Слой 3: хранение и поиск (БД + эмбеддинги)
# ============================================================================
def _embedding_model(connection) -> str:
    return ((connection or {}).get("embedding_model") or "").strip()


def _unit(vec) -> list[float] | None:
    """Вектор единичной длины: косинус потом — просто скалярное произведение."""
    n = _norm(vec)
    if not n:
        return None
    return [round(x / n, 6) for x in vec]


async def _embed(texts: list[str], connection) -> list[list[float]] | None:
    # Через модуль, а не from-импорт: так тесты подменяют llm_gateway.embed.
    from backend import llm_gateway

    return await llm_gateway.embed(texts, connection)


async def store_facts(db, session_id: int, facts: list[str], newest_id: int, connection) -> int:
    """
    Сохраняет новые факты чата (с векторами, если эмбеддинги настроены).

    Вызывается ПОСЛЕ того, как сводка уже записана и закоммичена, и никогда не
    бросает исключений: сбой фактов не должен откатывать сводку или ронять
    фоновую задачу. Возвращает число сохранённых фактов.
    """
    from sqlalchemy import select

    from backend.models import HoraeFact

    try:
        facts = [f.strip() for f in (facts or []) if (f or "").strip()]
        if not facts:
            return 0
        known = {
            _norm_key(c) for c in (await db.execute(
                select(HoraeFact.content).where(HoraeFact.session_id == session_id)
            )).scalars().all()
        }
        fresh: list[str] = []
        for f in facts:
            key = _norm_key(f)
            if key and key not in known:
                known.add(key)
                fresh.append(f)
        if not fresh:
            return 0

        model = _embedding_model(connection)
        vectors = await _embed(fresh, connection) if model else None
        keep: list[tuple[str, list[float] | None]] = []
        if vectors and len(vectors) == len(fresh):
            recent = (await db.execute(
                select(HoraeFact.embedding).where(
                    HoraeFact.session_id == session_id, HoraeFact.embed_model == model,
                ).order_by(HoraeFact.id.desc()).limit(DUP_SCAN_RECENT)
            )).scalars().all()
            pool = [(v, _norm(v)) for v in recent if isinstance(v, list) and v]
            for text, vec in zip(fresh, vectors):
                u = _unit(vec)
                if u is None:
                    keep.append((text, None))
                    continue
                if any(cosine(u, v, 1.0, n) >= DUP_COSINE for v, n in pool):
                    continue  # почти-дубль уже известного факта
                pool.append((u, 1.0))
                keep.append((text, u))
        else:
            keep = [(t, None) for t in fresh]

        for text, u in keep:
            row = HoraeFact(
                session_id=session_id, content=text, source_message_id=int(newest_id or 0),
            )
            if u is not None:
                # Без вектора поле не трогаем вовсе: присвоенный None в JSON-колонке
                # лёг бы строкой 'null', а не SQL NULL.
                row.embedding = u
                row.embed_model = model
            db.add(row)
        await db.commit()
        return len(keep)
    except Exception:  # noqa: BLE001 — факты не должны ронять сводку
        logger.exception("Факты чата %s не сохранились", session_id)
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return 0


async def backfill_embeddings(db, session_id: int, connection, limit: int = BACKFILL_BATCH) -> int:
    """
    Досчитывает векторы старых фактов чата под текущую модель эмбеддингов.

    Эмбеддинги можно включить (или сменить модель) посреди жизни чата. Старые
    факты без вектора этой модели в поиске по векторам не участвуют — без
    досчёта включение эмбеддингов «забывало» бы всю память до этого момента.
    Одна порция в limit фактов — один запрос к сервису; до конца досчитывает
    backfill_all. Никогда не бросает исключений.
    """
    from sqlalchemy import select

    from backend.models import HoraeFact

    model = _embedding_model(connection)
    if not model:
        return 0
    try:
        rows = (await db.execute(
            select(HoraeFact).where(
                HoraeFact.session_id == session_id, HoraeFact.embed_model != model,
            ).order_by(HoraeFact.id.desc()).limit(limit)
        )).scalars().all()
        if not rows:
            return 0
        vectors = await _embed([r.content for r in rows], connection)
        if not vectors or len(vectors) != len(rows):
            return 0
        done = 0
        for r, vec in zip(rows, vectors):
            u = _unit(vec)
            if u is not None:
                r.embedding = u
                r.embed_model = model
                done += 1
        await db.commit()
        return done
    except Exception:  # noqa: BLE001
        logger.exception("Векторы фактов чата %s не досчитались", session_id)
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return 0


async def backfill_all(db, session_id: int, connection, max_batches: int = BACKFILL_MAX_BATCHES) -> int:
    """
    Досчёт векторов до конца (не больше max_batches порций за раз). Никогда не бросает.

    Раньше досчитывалось по одной порции за проход сводки, а проход бывает раз в
    summary_every сообщений: в чате с двумя тысячами фактов векторы догоняли бы
    историю сотни сообщений. Теперь фоновая задача после каждого хода крутит
    порции, пока не кончатся старые факты или не упрётся в потолок.
    """
    total = 0
    for _ in range(max(1, int(max_batches or 1))):
        done = await backfill_embeddings(db, session_id, connection, BACKFILL_BATCH)
        total += done
        if done < BACKFILL_BATCH:
            break  # старых фактов не осталось или сервис не ответил
    return total


# Векторы фактов, уже прочитанные из базы: {(чат, модель): {id факта: (текст,
# вектор, норма)}}. Вектор в JSON — это ~30 КБ текста; разбирать тысячи таких
# на КАЖДОМ ходу длинного чата стоило бы сотни мегабайт и секунды. array('f')
# вчетверо легче списка float. Текст хранится для проверки: SQLite может
# переиспользовать id удалённого факта, и чужой вектор не должен «прилипнуть».
_VEC_CACHE: "OrderedDict[tuple[int, str], dict[int, tuple[str, array, float]]]" = OrderedDict()
_VEC_CACHE_CHATS = 4
# Векторы последних запросов: инспектор и ход, а в группе — каждый отвечающий
# персонаж спрашивают память одним и тем же текстом.
_QUERY_CACHE: "OrderedDict[tuple[str, str], list[float]]" = OrderedDict()
_QUERY_CACHE_MAX = 32


async def _fact_vectors(db, session_id: int, model: str, wanted: dict[int, str]) -> dict:
    from sqlalchemy import select

    from backend.models import HoraeFact

    key = (session_id, model)
    cache = _VEC_CACHE.get(key)
    if cache is None:
        cache = _VEC_CACHE[key] = {}
    _VEC_CACHE.move_to_end(key)
    while len(_VEC_CACHE) > _VEC_CACHE_CHATS:
        _VEC_CACHE.popitem(last=False)

    missing = [i for i, text in wanted.items() if i not in cache or cache[i][0] != text]
    for pos in range(0, len(missing), 500):  # лимит параметров SQLite
        part = missing[pos:pos + 500]
        rows = (await db.execute(
            select(HoraeFact.id, HoraeFact.embedding).where(HoraeFact.id.in_(part))
        )).all()
        for fid, emb in rows:
            if isinstance(emb, list) and emb:
                vec = array("f", emb)
                cache[fid] = (wanted[fid], vec, _norm(vec))
    return {i: cache[i] for i in wanted if i in cache and cache[i][0] == wanted[i]}


async def _query_vector(query: str, connection, model: str) -> list[float] | None:
    key = (model, query)
    if key in _QUERY_CACHE:
        _QUERY_CACHE.move_to_end(key)
        return _QUERY_CACHE[key]
    vectors = await _embed([query], connection)
    if not vectors:
        return None
    _QUERY_CACHE[key] = vectors[0]
    while len(_QUERY_CACHE) > _QUERY_CACHE_MAX:
        _QUERY_CACHE.popitem(last=False)
    return vectors[0]


async def recall(
    db,
    session_id: int,
    query: str,
    connection,
    newest_id: int = 0,
    exclude_from_id: int | None = None,
    *,
    context: str = "",
    top_k: int | None = TOP_K,
    stats: dict | None = None,
) -> list[dict]:
    """
    Факты чата, относящиеся к текущему ходу (см. rank_facts). Никогда не бросает.

    :param query: реплика пользователя — то, чего факт должен касаться.
    :param context: хвост разговора: помогает понять реплику («а что с ним?»),
        но сам по себе факт не вспоминает (см. lexical_similarities).
    :param exclude_from_id: факты, извлечённые из сообщений с id >= этого, не
        берём — их источник и так лежит в контексте дословно, повтор только
        тратил бы токены. None — ничего не исключаем.
    :param top_k: None — все факты выше порога (отсеет и обрежет вызывающий).
    :param stats: сюда кладётся {"mode": "vector" | "lexical", "candidates": N}
        для инспектора хода, а пока векторы досчитываются — ещё и "backfilling".

    Векторы — если в подключении задана модель эмбеддингов, вектор запроса
    посчитался и векторы этой модели есть у VECTOR_COVERAGE фактов. Иначе — по
    словам по всем фактам. Смешивать режимы нельзя: косинус векторов и доля
    совпавших слов — разные шкалы с разными порогами.
    """
    from sqlalchemy import select

    from backend.models import HoraeFact, Message

    model = _embedding_model(connection)
    info = stats if stats is not None else {}
    info["mode"] = "vector" if model else "lexical"
    info["candidates"] = 0
    try:
        query = (query or "").strip()[:QUERY_CHARS]
        context = (context or "").strip()[:QUERY_CHARS]
        if not query and not context:
            return []
        q = select(
            HoraeFact.id, HoraeFact.content, HoraeFact.source_message_id, HoraeFact.embed_model,
        ).where(HoraeFact.session_id == session_id)
        if exclude_from_id is not None:
            q = q.where(HoraeFact.source_message_id < exclude_from_id)
        rows = (await db.execute(q)).all()
        info["candidates"] = len(rows)
        if not rows:
            return []

        # Возраст факта — в сообщениях ЭТОГО чата, а не в разнице id: id
        # сообщений общие на все чаты, и в соседних чатах набегали бы тысячи.
        msg_ids = (await db.execute(
            select(Message.id).where(Message.session_id == session_id).order_by(Message.id)
        )).scalars().all()
        total = len(msg_ids)
        facts = [
            {
                "id": fid, "content": content or "", "source_message_id": int(src or 0),
                "embed_model": emb_model or "",
                "age": total - bisect_right(msg_ids, int(src or 0)) if total else None,
            }
            for fid, content, src, emb_model in rows
        ]
        newest = msg_ids[-1] if msg_ids else int(newest_id or 0)

        if model:
            with_vec = {f["id"]: f["content"] for f in facts if f["embed_model"] == model}
            if with_vec and len(with_vec) < VECTOR_COVERAGE * len(facts):
                # Эмбеддинги включили посреди жизни чата: векторы есть у горстки
                # свежих фактов. Искать только по ним — значит спрятать всю
                # старую память, пока идёт досчёт; поэтому до его конца — слова.
                with_vec = {}
                info["backfilling"] = True
            # Вектор запроса — по реплике вместе с хвостом: у векторов нет
            # «совпадения по имени», а местоимение без хвоста не понять.
            vec_text = "\n".join(p for p in (query, context) if p)[:QUERY_CHARS]
            qvec = await _query_vector(vec_text, connection, model) if with_vec else None
            if qvec is not None:
                vecs = await _fact_vectors(db, session_id, model, with_vec)
                pool = []
                for f in facts:
                    got = vecs.get(f["id"])
                    if got is not None:
                        pool.append({**f, "embedding": got[1], "norm": got[2]})
                if pool:
                    return rank_facts(pool, newest, query_vec=qvec, top_k=top_k)
            info["mode"] = "lexical"  # векторов нет или сервис не ответил
        return rank_facts(facts, newest, query_text=query, context_text=context, top_k=top_k)
    except Exception:  # noqa: BLE001 — память не должна ронять ход
        logger.exception("Отбор фактов чата %s не удался", session_id)
        return []
