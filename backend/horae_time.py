"""
Время сюжета Horae: разбор дат, относительное время, свой календарь, возраст NPC.

Порт utils/timeUtils.js плагина Horae (SillyTavern). Модель пишет дату сюжета
в строке `time:` как придётся: «2026/2/4 15:00», «04.02.2026», «4 февраля 2026
года», «третий день месяца Инея», «萬曆十五年八月十六日». Здесь всё это
приводится к одной структуре, по которой считаются относительные метки
хронологии («вчера», «прошлый вт») и справка по времени в блоке состояния.

Типы дат — как в плагине:
  * standard — григорианская дата (года может не быть), возможно с префиксом
    эпохи («Эра Драконов», «萬曆»): даты с разными префиксами несравнимы;
  * custom   — дата пользовательского календаря (настройка `calendar`);
  * fantasy  — всё прочее, где нашёлся хотя бы «месяц» или «день».

Отличия от плагина (намеренные):
  * русская запись D.M.YYYY («04.02.2026») — стандартная дата, день первым;
    плагин читал её как фэнтези с днём 4. «04.02» без года — тоже день первым;
  * русские месяцы словами («4 февраля 2026 года», «15 марта 1024 г.»);
  * порядковые числительные для фэнтези и своего календаря: «третий день
    месяца Инея», «3-й день Инея»; месяц фэнтези — и из «месяца X», а не
    только из «…月»; сравнивается без учёта регистра;
  * хвостовое время «15:00» срезается перед разбором: иначе фэнтези-дата без
    числа получала день 15 из часов;
  * вывод на русском, дни недели — пн…вс, неделя с понедельника;
  * арифметика дат — своя (номер дня от эпохи), а не datetime: у фэнтези-эпох
    бывают годы 0, отрицательные и больше 9999, а JS Date плагина их понимал.
    Переполнение дня («2026/2/30» → 2 марта) — как у JS Date;
  * относительная метка для дат без года берёт текущий год, как и день недели
    в [Время|…] и справке, — иначе у 28.02→01.03 выходило «позавчера» при
    справке «вчера=2/28» (плагин считал дни по 2024-му, а неделю — по текущему).
"""
import re
from datetime import date as _date

# Особое значение relative_days: обе даты фэнтези, но сравнить нечем
# (нет чисел). Хронология ставит такое событие «раньше», без метки.
SPECIAL_EARLIER = -999
# Год по умолчанию для relative_days, если его нет ни у одной даты (как в плагине).
DEFAULT_YEAR = 2024

WEEKDAYS_RU = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")
MONTHS_RU = ("январь", "февраль", "март", "апрель", "май", "июнь", "июль",
             "август", "сентябрь", "октябрь", "ноябрь", "декабрь")
MONTHS_RU_GEN = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля",
                 "августа", "сентября", "октября", "ноября", "декабря")
# Сокращения месяцев («15 мар. 2026»); только после числа, так что с
# фэнтези-словами не путаются.
MONTHS_RU_SHORT = {"янв": 1, "фев": 2, "февр": 2, "мар": 3, "апр": 4, "июн": 6,
                   "июл": 7, "авг": 8, "сен": 9, "сент": 9, "окт": 10, "ноя": 11,
                   "нояб": 11, "дек": 12}
# Основы порядковых числительных: «трет|ий», «трет|ьего», «двадцат|ое».
# Длинные основы раньше коротких: «девятнадцат» не должна читаться как «девят».
ORDINAL_STEMS_RU = (
    ("одиннадцат", 11), ("двенадцат", 12), ("тринадцат", 13), ("четырнадцат", 14),
    ("пятнадцат", 15), ("шестнадцат", 16), ("семнадцат", 17), ("восемнадцат", 18),
    ("девятнадцат", 19), ("двадцат", 20), ("тридцат", 30),
    ("четв[её]рт", 4), ("седьм", 7), ("восьм", 8), ("девят", 9), ("десят", 10),
    ("перв", 1), ("втор", 2), ("трет", 3), ("пят", 5), ("шест", 6),
)
# Китайские числительные плагина (元年 = первый год).
CN_NUMS = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7,
    "八": 8, "九": 9, "十": 10, "十一": 11, "十二": 12, "十三": 13, "十四": 14,
    "十五": 15, "十六": 16, "十七": 17, "十八": 18, "十九": 19, "二十": 20, "廿": 20,
    "廿一": 21, "廿二": 22, "廿三": 23, "廿四": 24, "廿五": 25, "廿六": 26, "廿七": 27,
    "廿八": 28, "廿九": 29, "三十": 30, "三十一": 31, "卅": 30, "卅一": 31, "元": 1,
}

# ---- Регулярные выражения ---------------------------------------------------

# Время в конце строки. re.ASCII — как `\b` в JS: «日20:30» тоже время.
_CLOCK_RE = re.compile(r"\b(\d{1,2}:\d{2})\s*$", re.ASCII)
# Для сравнения «та же дата?»: срезать хвост со временем (как stripTime плагина).
_STRIP_CLOCK_RE = re.compile(r"\s+\d{1,2}[:：]\d{2}.*$", re.DOTALL)
_STRIP_CN_TOD_RE = re.compile(
    r"\s+(凌晨|早上|上午|中午|下午|傍晚|晚上|深夜|子时|丑时|寅时|卯时|辰时|巳时|午时|未时"
    r"|申时|酉时|戌时|亥时).*$", re.DOTALL)
# Пометка дня недели, которую модель дописывает к дате: «(ср)», «(三)».
_WEEKDAY_MARK_RE = re.compile(
    r"\s*[(（]\s*(?:[日一二三四五六]|пн|вт|ср|чт|пт|сб|вс|понедельник|вторник|среда"
    r"|четверг|пятница|суббота|воскресенье)\.?\s*[)）]\s*", re.I)
# «Дата неизвестна»: xx/?? (плагин) и кириллическое «хх».
_UNKNOWN_RE = re.compile(r"[xX]{2}|[?？]{2}|(?<![а-яё])[хХ]{2,}(?![а-яё])", re.I)

_FULL_RE = re.compile(r"^(\d{4,})[/\-.](\d{1,2})[/\-.](\d{1,2})")
_SHORT_RE = re.compile(r"^(\d{1,2})[/\-](\d{1,2})(?:\s|$)")
_DMY_RE = re.compile(r"^(\d{1,2})\.(\d{1,2})\.(\d{3,})(?!\d)")
_DM_RE = re.compile(r"^(\d{1,2})\.(\d{1,2})(?=\s|,|$)")

_MONTH_WORDS = {w: i + 1 for i, w in enumerate(MONTHS_RU)}
_MONTH_WORDS.update({w: i + 1 for i, w in enumerate(MONTHS_RU_GEN)})
_MONTH_WORDS.update(MONTHS_RU_SHORT)
_MONTH_ALT = "|".join(sorted(_MONTH_WORDS, key=len, reverse=True))
# «4 февраля 2026 года», «15-го марта», «15 мар. 1024 г.».
_RU_DATE_RE = re.compile(
    r"(?<!\d)(\d{1,2})(?:-?(?:го|е|ое|ого))?\s+(" + _MONTH_ALT + r")\.?(?![а-яё])"
    r"(?:\s*,?\s*(\d{1,6})(?![\d:])(?:\s*(?:гг?\.|г(?![а-яё])|год[а-яё]*))?)?", re.I)
_RU_MONTH_ONLY_RE = re.compile(r"(?<![а-яё])(" + _MONTH_ALT + r")(?![а-яё])", re.I)

_CN = "零〇一二三四五六七八九十廿卅元"
_YEAR_CN_RE = re.compile(r"(\d+)年\s*(\d{1,2})月(\d{1,2})日?")
_ERA_RE = re.compile(
    rf"^([^\s\d{_CN}年月日][^\s年月日]*?)([{_CN}]+|\d+)年\s*([{_CN}]+|\d+)\s*月\s*"
    rf"([{_CN}]+|\d+)\s*日?")
# `\b` плагина после числа — ASCII-граница: «8/16日» подходит.
_YEAR_SLASH_RE = re.compile(
    rf"^([^\s\d{_CN}年月日]?[^\s年月日]*?)([{_CN}]+|\d+)年\s*(\d{{1,2}})[/\-](\d{{1,2}})"
    r"(?![0-9A-Za-z_])")
_CN_YEAR_MIXED_RE = re.compile(
    r"(\d+|[零〇一二三四五六七八九]+)年\s*([零〇一二三四五六七八九十廿卅]+)月"
    r"([零〇一二三四五六七八九十廿卅]+)日?")
_CN_MD_RE = re.compile(r"(\d{1,2})月(\d{1,2})日?")
_CN_MONTH_DAY_RE = re.compile(r"([零〇一二三四五六七八九十廿卅]+)月([零〇一二三四五六七八九十廿卅]+)日?")

# Порядковое числительное: «третий», «двадцать первое», «тридцатого».
_ORD_END = (r"(?:ьего|ьему|ьим|ьем|ьей|ьих|ье|ья|ьи|ого|его|ому|ему|ый|ий|ой|ое|ее"
            r"|ая|яя|ую|юю|ом|ем|ым|им|ых|их)")
_ORD_SRC = (r"(?P<tens>двадцать\s+|тридцать\s+)?(?P<stem>"
            + "|".join(s for s, _ in ORDINAL_STEMS_RU) + r")" + _ORD_END)
_ORD_STEM_RES = tuple((re.compile(s, re.I), v) for s, v in ORDINAL_STEMS_RU)
_RU_ORD_RE = re.compile(r"(?<![а-яё])" + _ORD_SRC + r"(?![а-яё])", re.I)
_NUM_ORD_SUFFIX = r"(?:-?(?:ый|ий|ой|ого|го|ое|ье|ья|е|й|я))"

# Число дня в фэнтези-дате — по убыванию надёжности.
_DAY_EXPLICIT_RE = re.compile(r"(?<!\d)(\d+)" + _NUM_ORD_SUFFIX + r"?\s+(?:день|дня|числ[оа])(?![а-яё])", re.I)
_DAY_PREFIXED_RE = re.compile(r"(?:第|day\s*|день\s*)(\d+)(?:日)?", re.I)
_DAY_CN_SUFFIX_RE = re.compile(r"(\d+)(?:日|号)")
_DAY_NUM_ORD_RE = re.compile(r"(?<!\d)(\d+)-?(?:ый|ий|ой|ого|го|ое|ье|ья|е|й|я)(?![а-яё])", re.I)
_ANY_NUM_RE = re.compile(r"(\d+)")
# Китайский день словами: «第三日», «月十五日»; длинные числа раньше коротких.
_CN_DAY_RES = tuple(
    (re.compile(f"第{cn}日|第{cn}(?![一-龥])|月{cn}日|{cn}日"), num)
    for cn, num in sorted(CN_NUMS.items(), key=lambda kv: -len(kv[0])))
_CN_MONTH_ID_RE = re.compile(r"([^\s\d]+月)")
_RU_MONTH_ID_RE = re.compile(r"(?<![а-яё])месяц[аеу]?\s+([^\s\d,.;:!?()«»\"'—–]+)", re.I)

# Свой календарь: имя месяца заменяется маркером, год и день ищутся вокруг него.
_M = "\x01"
_CUSTOM_YEAR_RES = (
    re.compile(r"(?<![а-яёa-z])(?:год|г\.|year)\s*(\d+)(?!\d)", re.I),
    re.compile(r"(?<!\d)(\d+)" + _NUM_ORD_SUFFIX + r"?\s*(?:год[а-яё]*|г\.?)(?![а-яё])", re.I),
    re.compile(rf"([{_CN}]+|\d+)年"),
)
_CUSTOM_DAY_BEFORE_RE = re.compile(
    r"(?<!\d)(\d+)" + _NUM_ORD_SUFFIX + r"?\s*(?:(?:день|дня|числ[оа])\s*)?(?:месяц[аеу]?\s*)?" + _M,
    re.I)
_CUSTOM_ORD_BEFORE_RE = re.compile(
    r"(?<![а-яё])" + _ORD_SRC + r"\s+(?:(?:день|дня|числ[оа])\s+)?(?:месяц[аеу]?\s+)?" + _M, re.I)
_CUSTOM_DAY_AFTER_RE = re.compile(
    _M + r"\s*[,.]?\s*(?:(?:день|число)\s*)?(\d+|[零〇一二三四五六七八九十廿卅]+)" + _NUM_ORD_SUFFIX
    + r"?日?", re.I)
_CUSTOM_ORD_AFTER_RE = re.compile(_M + r"\s*[,.]?\s*" + _ORD_SRC + r"(?![а-яё])", re.I)
_CUSTOM_TRAILING_YEAR_RE = re.compile(_M + r"\s*,?\s*(\d+)(?!\d)")

# Префикс эпохи чистим от дня недели и времени суток: «Среда, 4 февраля 2026»
# и «4 февраля 2026» — одна эпоха, иначе они стали бы несравнимы.
_PREFIX_NOISE_RE = re.compile(
    r"(?<![а-яё])(?:(?:в|во|на)\s+)?(?:понедельник|вторник|сред[аеу]|четверг|пятниц[аеу]"
    r"|суббот[аеу]|воскресенье|пн|вт|ср|чт|пт|сб|вс|утр[оа]м?|вечер(?:ом|а)?|ноч(?:ь|ью|и)"
    r"|днём|днем|день|полдень|полночь)\.?(?![а-яё])", re.I)
_PREFIX_TRIM = " \t\r\n,;:.—–-"

_TOD_WORDS = (
    (re.compile(r"(?<![а-яё])(?:полноч[ьи]|ноч(?:ь|ью|и)|深夜|凌晨)", re.I), "ночь"),
    (re.compile(r"(?<![а-яё])(?:утр(?:о|ом|а)|рассвет|早上|上午)", re.I), "утро"),
    (re.compile(r"(?<![а-яё])(?:полдень|полдня|днём|днем|день|中午|下午)", re.I), "день"),
    (re.compile(r"(?<![а-яё])(?:вечер(?:ом|а)?|закат|сумерк|傍晚|晚上)", re.I), "вечер"),
)
_EARTHLY_BRANCH_HOURS = {"子": 23, "丑": 1, "寅": 3, "卯": 5, "辰": 7, "巳": 9,
                         "午": 11, "未": 13, "申": 15, "酉": 17, "戌": 19, "亥": 21}


# ---- Календарная арифметика ------------------------------------------------
# Номер дня от 1970-01-01 по пролептическому григорианскому календарю
# (алгоритм Хиннанта): работает для любого целого года, в отличие от datetime.

def _days_from_civil(y: int, m: int, d: int) -> int:
    """Номер дня; день сверх длины месяца переносится вперёд, как в JS Date."""
    y -= m <= 2
    era = y // 400
    yoe = y - era * 400
    doy = (153 * (m + (-3 if m > 2 else 9)) + 2) // 5
    doe = yoe * 365 + yoe // 4 - yoe // 100 + doy
    return era * 146097 + doe - 719468 + (d - 1)


def _civil_from_days(z: int) -> tuple[int, int, int]:
    z += 719468
    era = z // 146097
    doe = z - era * 146097
    yoe = (doe - doe // 1460 + doe // 36524 - doe // 146096) // 365
    doy = doe - (365 * yoe + yoe // 4 - yoe // 100)
    mp = (5 * doy + 2) // 153
    d = doy - (153 * mp + 2) // 5 + 1
    m = mp + 3 if mp < 10 else mp - 9
    return yoe + era * 400 + (m <= 2), m, d


def _weekday(z: int) -> int:
    """0 — понедельник (1970-01-01 был четвергом)."""
    return (z + 3) % 7


def _current_year() -> int:
    return _date.today().year


# ---- Мелкие помощники ------------------------------------------------------

def _js_int(value) -> int | None:
    """parseInt из JS: ведущие цифры строки («35 лет» → 35), иначе None."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value == value and value not in (float("inf"), float("-inf")) else None
    m = re.match(r"\s*([+-]?\d+)", str(value))
    return int(m.group(1)) if m else None


def _num(s: str) -> int | None:
    """Арабское или китайское число дня/месяца."""
    return int(s) if s.isdecimal() else _cn_num_to_int(s)


def _cn_num_to_int(s: str) -> int | None:
    if not s:
        return None
    if s in CN_NUMS:
        return CN_NUMS[s]
    m = re.fullmatch(r"([一二三四五六七八九])?十([一二三四五六七八九])?", s)
    if m:
        tens = CN_NUMS[m.group(1)] if m.group(1) else 1
        return tens * 10 + (CN_NUMS[m.group(2)] if m.group(2) else 0)
    m = re.fullmatch(r"([廿卅])([一二三四五六七八九])?", s)
    if m:
        return (20 if m.group(1) == "廿" else 30) + (CN_NUMS[m.group(2)] if m.group(2) else 0)
    return None


def _cn_year_to_int(s: str) -> int | None:
    """Год цифра за цифрой: «二〇二四» → 2024 (сотни/тысячи словами — нет)."""
    if not s:
        return None
    if s.isdecimal():
        return int(s)
    n = 0
    for ch in s:
        v = CN_NUMS.get(ch)
        if v is None or v > 9:
            return None
        n = n * 10 + v
    return n or None


def _ordinal_value(m: re.Match) -> int | None:
    stem = m.group("stem")
    for rx, value in _ORD_STEM_RES:
        if rx.fullmatch(stem):
            tens = m.group("tens")
            if tens:
                value += 30 if tens.lower().startswith("тр") else 20
            return value
    return None


def _clean_prefix(text: str) -> str:
    return _PREFIX_NOISE_RE.sub(" ", text).strip(_PREFIX_TRIM).strip()


def _cal(calendar) -> dict | None:
    """Календарь в любом виде (настройка или уже нормализованный) → нормализованный."""
    if not isinstance(calendar, dict):
        return None
    return normalize_calendar(calendar)


def _result(kind: str, raw: str, *, year=None, month=None, day=None, month_index=None,
            month_id=None, prefix: str = "") -> dict:
    return {"type": kind, "year": year, "month": month, "day": day,
            "month_index": month_index, "month_id": month_id, "prefix": prefix or "",
            "raw": raw}


def _valid_md(month, day) -> bool:
    return month is not None and day is not None and 1 <= month <= 12 and 1 <= day <= 31


# ---- Календарь -------------------------------------------------------------

def normalize_calendar(cfg) -> dict | None:
    """Настройка календаря → {"months", "offsets", "year_len"} или None.

    Принимает нашу настройку `{"enabled", "months": [{"name", "days"}]}`, формат
    плагина `{"enabled", "monthNames", "monthDays"}` и уже нормализованный
    календарь (с `offsets`). Любая дыра (нет месяцев, пустое имя, дней <= 0) —
    None: полуживой календарь дал бы неверную арифметику дат молча.
    """
    if not isinstance(cfg, dict):
        return None
    if "offsets" not in cfg and not cfg.get("enabled"):
        return None
    months_in = cfg.get("months")
    if months_in is None and ("monthNames" in cfg or "monthDays" in cfg):
        names, days = cfg.get("monthNames"), cfg.get("monthDays")
        if not isinstance(names, list) or not isinstance(days, list) or len(names) != len(days):
            return None
        months_in = [{"name": n, "days": d} for n, d in zip(names, days)]
    if not isinstance(months_in, list) or not months_in:
        return None
    months, offsets, acc = [], [], 0
    for item in months_in:
        if not isinstance(item, dict):
            return None
        name = item.get("name")
        name = name.strip() if isinstance(name, str) else ""
        days = _js_int(item.get("days"))
        if not name or days is None or days <= 0:
            return None
        months.append({"name": name, "days": days})
        offsets.append(acc)
        acc += days
    return {"months": months, "offsets": offsets, "year_len": acc}


def _custom_linear(p: dict, cal: dict) -> int:
    return (p["year"] or 0) * cal["year_len"] + cal["offsets"][p["month_index"]] + p["day"] - 1


def _custom_str(year, index: int, day: int, cal: dict) -> str:
    name = cal["months"][index]["name"]
    return f"Год {year}, {day} {name}" if year is not None else f"{day} {name}"


def _parse_custom(s: str, raw: str, cal: dict) -> dict | None:
    """Дата своего календаря: «Год 5, 3 Инея», «5 год, Инея 3», «3-й день Инея».

    Имя месяца ищется без учёта регистра, длинные имена раньше коротких
    («Весна» не должна съесть «Весна Поздняя»). Сначала вырезается год, потом
    ищется день рядом с месяцем — иначе в «Год 5 Инея 3» днём стало бы 5.
    """
    order = sorted(range(len(cal["months"])), key=lambda i: -len(cal["months"][i]["name"]))
    for i in order:
        name = cal["months"][i]["name"]
        # Граница — только для букв латиницы/кириллицы: китайские имена
        # («春之月») пишутся слитно с числами и 年.
        m = re.search(r"(?<![A-Za-zА-Яа-яЁё])" + re.escape(name) + r"(?![A-Za-zА-Яа-яЁё])", s, re.I)
        if not m:
            continue
        work = s[:m.start()] + _M + s[m.end():]
        year = None
        for rx in _CUSTOM_YEAR_RES:
            ym = rx.search(work)
            if ym:
                year = _cn_num_to_int(ym.group(1)) if not ym.group(1).isdecimal() else int(ym.group(1))
                work = work[:ym.start()] + " " + work[ym.end():]
                break
        day = None
        before = False
        dm = _CUSTOM_DAY_BEFORE_RE.search(work)
        if dm:
            day, before = int(dm.group(1)), True
        else:
            dm = _CUSTOM_ORD_BEFORE_RE.search(work)
            if dm:
                day, before = _ordinal_value(dm), True
            else:
                dm = _CUSTOM_DAY_AFTER_RE.search(work)
                if dm:
                    day = _num(dm.group(1))
                else:
                    dm = _CUSTOM_ORD_AFTER_RE.search(work)
                    if dm:
                        day = _ordinal_value(dm)
        if before and year is None:
            # «15 Инея 1024» — число после месяца при дне перед ним — это год.
            tm = _CUSTOM_TRAILING_YEAR_RE.search(work)
            if tm:
                year = int(tm.group(1))
        if day is not None and 1 <= day <= cal["months"][i]["days"]:
            return _result("custom", raw, year=year, day=day, month_index=i, month_id=name)
    return None


# ---- Разбор даты -----------------------------------------------------------

def split_time(value: str) -> tuple[str, str]:
    """«2026/2/4 15:00» → («2026/2/4», «15:00»); без часов — (value, "")."""
    if not isinstance(value, str):
        return "", ""
    m = _CLOCK_RE.search(value)
    if not m:
        return value.strip(), ""
    clock = m.group(1)
    return value[:value.rfind(clock)].strip(), clock


def _extract_day(s: str) -> int | None:
    """Число дня фэнтези-даты (extractDayNumber плагина + русские формы)."""
    for rx in (_DAY_EXPLICIT_RE, _DAY_PREFIXED_RE, _DAY_CN_SUFFIX_RE, _DAY_NUM_ORD_RE):
        m = rx.search(s)
        if m:
            return int(m.group(1))
    m = _RU_ORD_RE.search(s)
    if m:
        value = _ordinal_value(m)
        if value is not None:
            return value
    for rx, num in _CN_DAY_RES:
        if rx.search(s):
            return num
    m = _ANY_NUM_RE.search(s)
    return int(m.group(1)) if m else None


def _parse_fantasy(s: str, raw: str) -> dict | None:
    work = s
    month_id = None
    gregorian_word = False
    m = _CN_MONTH_ID_RE.search(work)
    if m:
        month_id = m.group(1)
    else:
        m = _RU_MONTH_ID_RE.search(work)
        if m:
            month_id = m.group(1).casefold()
        else:
            m = _RU_MONTH_ONLY_RE.search(work)
            if m:
                # «февраль 2026»: месяц без числа. Сравнимо только с тем же месяцем.
                month_id = MONTHS_RU[_MONTH_WORDS[m.group(1).lower()] - 1]
                gregorian_word = True
        if m:
            # Слово месяца не должно стать числом дня («месяца Первого снега»).
            work = work[:m.start()] + " " + work[m.end():]
    day = _extract_day(work)
    if gregorian_word and day is not None and not 1 <= day <= 31:
        day = None  # «февраль 2026» — 2026 это год, а не день
    if month_id is None and day is None:
        return None
    return _result("fantasy", raw, day=day, month_id=month_id)


def parse_story_date(text: str, calendar=None) -> dict | None:
    """Строка даты сюжета → структура (см. модуль) или None, если дат нет.

    Порядок правил важен (как в плагине): числовые формы → свой календарь →
    русские месяцы словами → китайские формы → фэнтези. Хвостовое время и
    пометка дня недели «(ср)» перед разбором срезаются.
    """
    if not isinstance(text, str) or not text.strip():
        return None
    raw = text.strip()
    s = _WEEKDAY_MARK_RE.sub(" ", raw).strip()
    s, _ = split_time(s)
    if not s:
        return None
    if _UNKNOWN_RE.search(s):
        return _result("fantasy", raw)

    m = _FULL_RE.match(s)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if _valid_md(mo, d):
            return _result("standard", raw, year=y, month=mo, day=d)
    # Русская запись — день первым (плагин читал «04.02.2026» как фэнтези).
    m = _DMY_RE.match(s)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if _valid_md(mo, d):
            return _result("standard", raw, year=y, month=mo, day=d)
    m = _DM_RE.match(s)
    if m:
        d, mo = int(m.group(1)), int(m.group(2))
        if _valid_md(mo, d):
            return _result("standard", raw, month=mo, day=d)
    m = _SHORT_RE.match(s)
    if m:
        mo, d = int(m.group(1)), int(m.group(2))
        if _valid_md(mo, d):
            return _result("standard", raw, month=mo, day=d)

    cal = _cal(calendar)
    if cal:
        custom = _parse_custom(s, raw, cal)
        if custom:
            return custom

    m = _RU_DATE_RE.search(s)
    if m:
        d, mo = int(m.group(1)), _MONTH_WORDS[m.group(2).lower()]
        y = int(m.group(3)) if m.group(3) else None
        if _valid_md(mo, d):
            return _result("standard", raw, year=y, month=mo, day=d,
                           prefix=_clean_prefix(s[:m.start()]))

    # Китайские формы плагина: с годом — раньше, чем без года.
    m = _YEAR_CN_RE.search(s)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if _valid_md(mo, d):
            return _result("standard", raw, year=y, month=mo, day=d,
                           prefix=_clean_prefix(s[:m.start()]))
    m = _ERA_RE.match(s)
    if m:
        y, mo, d = _num(m.group(2)), _num(m.group(3)), _num(m.group(4))
        if y is not None and y >= 1 and _valid_md(mo, d):
            return _result("standard", raw, year=y, month=mo, day=d, prefix=_clean_prefix(m.group(1)))
    m = _YEAR_SLASH_RE.match(s)
    if m:
        yr = m.group(2)
        y = int(yr) if yr.isdecimal() else (_cn_num_to_int(yr) if _cn_num_to_int(yr) is not None
                                            else _cn_year_to_int(yr))
        mo, d = int(m.group(3)), int(m.group(4))
        if y is not None and y >= 1 and _valid_md(mo, d):
            return _result("standard", raw, year=y, month=mo, day=d, prefix=_clean_prefix(m.group(1)))
    m = _CN_YEAR_MIXED_RE.search(s)
    if m:
        y, mo, d = _cn_year_to_int(m.group(1)), _cn_num_to_int(m.group(2)), _cn_num_to_int(m.group(3))
        if y is not None and _valid_md(mo, d):
            return _result("standard", raw, year=y, month=mo, day=d,
                           prefix=_clean_prefix(s[:m.start()]))
    m = _CN_MD_RE.search(s)
    if m:
        mo, d = int(m.group(1)), int(m.group(2))
        if _valid_md(mo, d):
            return _result("standard", raw, month=mo, day=d)
    m = _CN_MONTH_DAY_RE.search(s)
    if m:
        mo, d = _cn_num_to_int(m.group(1)), _cn_num_to_int(m.group(2))
        if _valid_md(mo, d):
            return _result("standard", raw, month=mo, day=d)

    return _parse_fantasy(s, raw)


# ---- Относительное время ---------------------------------------------------

def _strip_time(s: str) -> str:
    s = _STRIP_CLOCK_RE.sub("", s.strip())
    return _STRIP_CN_TOD_RE.sub("", s).strip()


def _standard_pair(fp: dict, tp: dict, default_year: int) -> tuple[int, int]:
    """Номера дней двух стандартных дат; нет года — берётся у соседней."""
    fy = fp["year"] or tp["year"] or default_year
    ty = tp["year"] or fp["year"] or default_year
    return (_days_from_civil(fy, fp["month"], fp["day"]),
            _days_from_civil(ty, tp["month"], tp["day"]))


def _relative(from_date, to_date, cal, default_year: int):
    """(дни, разбор from, разбор to) — ядро calculateRelativeTime плагина."""
    if not isinstance(from_date, str) or not isinstance(to_date, str):
        return None, None, None
    if not from_date.strip() or not to_date.strip():
        return None, None, None
    if _strip_time(from_date) == _strip_time(to_date):
        return 0, None, None
    fp, tp = parse_story_date(from_date, cal), parse_story_date(to_date, cal)
    if not fp or not tp:
        return None, fp, tp
    kinds = (fp["type"], tp["type"])
    if "custom" in kinds:
        # Свой календарь со стандартной или фэнтези-датой не сравнить.
        if kinds != ("custom", "custom") or not cal:
            return None, fp, tp
        return _custom_linear(tp, cal) - _custom_linear(fp, cal), fp, tp
    if kinds == ("standard", "standard"):
        if fp["prefix"] != tp["prefix"]:
            return None, fp, tp  # разные эпохи — нет общей оси времени
        a, b = _standard_pair(fp, tp, default_year)
        return b - a, fp, tp
    fday, tday = fp["day"], tp["day"]
    fmonth, tmonth = fp["month_id"] or fp["month"], tp["month_id"] or tp["month"]
    if fday is not None and tday is not None:
        # Разные месяцы фэнтези-календаря не упорядочить: «Мороза 3» и
        # «Жатвы 25» — порядок месяцев знает только автор мира.
        if fmonth and tmonth and fmonth != tmonth:
            return None, fp, tp
        return tday - fday, fp, tp
    return SPECIAL_EARLIER, fp, tp


def relative_days(from_date: str, to_date: str, calendar=None) -> int | None:
    """Сколько дней от from до to (положительное — from в прошлом); None — неизвестно."""
    return _relative(from_date, to_date, _cal(calendar), DEFAULT_YEAR)[0]


def _month_diff(fd: int, td: int) -> int:
    fy, fm, _ = _civil_from_days(fd)
    ty, tm, _ = _civil_from_days(td)
    return (ty - fy) * 12 + (tm - fm)


def _week_diff(fd: int, td: int) -> int:
    """Разница недель, неделя начинается с понедельника."""
    return ((td - _weekday(td)) - (fd - _weekday(fd))) // 7


def _js_round(x: float) -> int:
    return int(x + 0.5) if x >= 0 else -int(-x + 0.5)


def _relative_meta(days, fd=None, td=None) -> tuple[str, dict]:
    """Ключ относительного времени (getRelativeTimeMeta плагина) и его данные.

    fd/td — номера дней обеих дат, только для стандартных: без реальных дат
    не бывает «прошлый вт» и «прошлый месяц 15-го».
    """
    if days is None:
        return "unknown", {}
    if days == SPECIAL_EARLIER:
        return "special_earlier", {}
    simple = {0: "today", 1: "yesterday", 2: "day_before_yesterday", 3: "three_days_ago",
              -1: "tomorrow", -2: "day_after_tomorrow", -3: "in_three_days"}
    if days in simple:
        return simple[days], {}
    both = fd is not None and td is not None
    n = abs(days)
    past = days > 0
    if n < 7:
        return ("days_ago" if past else "days_later"), {"n": n}
    if n <= 13 and fd is not None:
        wd = _week_diff(fd, td) if td is not None else (1 if past else -1)
        wd = wd if past else -wd
        if wd == 1:
            return ("last_weekday" if past else "next_weekday"), {"wd": _weekday(fd)}
        if wd == 2:
            return ("week_before_last_weekday" if past else "week_after_next_weekday"), {"wd": _weekday(fd)}
    if n < 60 and both:
        md = _month_diff(fd, td)
        if md == (1 if past else -1):
            _, m, d = _civil_from_days(fd)
            return ("last_month_day" if past else "next_month_day"), {"m": m, "d": d}
    if past and days >= 300 and both:
        fy, m, d = _civil_from_days(fd)
        yd = _civil_from_days(td)[0] - fy
        if yd == 1:
            return "last_year_date", {"m": m, "d": d}
        if yd == 2:
            return "year_before_last_date", {"m": m, "d": d}
    if n < 30:
        return ("days_ago" if past else "days_later"), {"n": n}
    if n < 365:
        md = _month_diff(fd, td) if both else None
        md = md if past or md is None else -md
        return ("months_ago" if past else "months_later"), {"n": max(1, md if md and md > 0 else n // 30)}
    years, months = n // 365, _js_round((n % 365) / 30)
    if months > 0 and years < 5:
        return ("years_months_ago" if past else "years_months_later"), {"y": years, "m": months}
    return ("years_ago" if past else "years_later"), {"y": years}


def _in_weekday(wd: int) -> str:
    return ("во " if wd == 1 else "в ") + WEEKDAYS_RU[wd]


_LABELS = {
    "today": lambda i: "сегодня",
    "yesterday": lambda i: "вчера",
    "day_before_yesterday": lambda i: "позавчера",
    "three_days_ago": lambda i: "3 дня назад",
    "tomorrow": lambda i: "завтра",
    "day_after_tomorrow": lambda i: "послезавтра",
    "in_three_days": lambda i: "через 3 дня",
    "last_weekday": lambda i: f"прошлый {WEEKDAYS_RU[i['wd']]}",
    "week_before_last_weekday": lambda i: f"позапрошлый {WEEKDAYS_RU[i['wd']]}",
    "next_weekday": lambda i: f"в следующий {WEEKDAYS_RU[i['wd']]}",
    "week_after_next_weekday": lambda i: f"через неделю {_in_weekday(i['wd'])}",
    "last_month_day": lambda i: f"прошлый месяц {i['d']}-го",
    "next_month_day": lambda i: f"в следующем месяце {i['d']}-го",
    "last_year_date": lambda i: f"прошлый год {i['m']}/{i['d']}",
    "year_before_last_date": lambda i: f"позапрошлый год {i['m']}/{i['d']}",
    "days_ago": lambda i: f"{i['n']} дн. назад",
    "days_later": lambda i: f"через {i['n']} дн.",
    "months_ago": lambda i: f"{i['n']} мес. назад",
    "months_later": lambda i: f"через {i['n']} мес.",
    "years_months_ago": lambda i: f"{i['y']} г. {i['m']} мес. назад",
    "years_months_later": lambda i: f"через {i['y']} г. {i['m']} мес.",
    "years_ago": lambda i: f"{i['y']} г. назад",
    "years_later": lambda i: f"через {i['y']} г.",
}


def relative_label(from_date: str, to_date: str, calendar=None) -> str:
    """Русская метка «насколько from раньше to» без скобок; "" — неизвестно.

    В отличие от плагина, метки есть и для будущего («через 3 дн.») и для
    «N г. M мес. назад»: плагин считал их, но в хронологию не выводил.
    """
    cal = _cal(calendar)
    year = _current_year()
    days, fp, tp = _relative(from_date, to_date, cal, year)
    fd = td = None
    if days is not None and fp and tp and fp["type"] == tp["type"] == "standard":
        fd, td = _standard_pair(fp, tp, year)
    key, info = _relative_meta(days, fd, td)
    label = _LABELS.get(key)
    return label(info) if label else ""


# ---- Вывод -----------------------------------------------------------------

def _standard_days(p: dict) -> int:
    return _days_from_civil(p["year"] or _current_year(), p["month"], p["day"])


def weekday_ru(date: str, calendar=None) -> str:
    """День недели стандартной даты («ср»); без года — по текущему году."""
    p = parse_story_date(date, calendar)
    if not p or p["type"] != "standard":
        return ""
    return WEEKDAYS_RU[_weekday(_standard_days(p))]


def _standard_str(p: dict, with_weekday: bool) -> str:
    y, m, d = p["year"], p["month"], p["day"]
    if p["prefix"]:
        # Эпоха пишется словами — такую строку parse_story_date прочтёт обратно
        # с тем же префиксом.
        text = f"{p['prefix']}, {d} {MONTHS_RU_GEN[m - 1]}" + (f" {y} г." if y else "")
    elif y:
        text = f"{y}/{m}/{d}"
    else:
        text = f"{m}/{d}"
    if with_weekday:
        text += f" ({WEEKDAYS_RU[_weekday(_standard_days(p))]})"
    return text


def _join(date: str, time: str) -> str:
    return f"{date} {time}".strip() if time else date


def format_date(date: str, time: str = "", calendar=None, weekday: bool = True) -> str:
    """Дата для промпта и панели: «2026/2/4 (ср) 15:00», «Год 5, 3 Инея 15:00».

    Фэнтези и неразобранное — как написано. Время, приклеенное к дате, если
    отдельного нет, отделяется (иначе нормализация даты его бы потеряла).
    """
    date = date.strip() if isinstance(date, str) else ""
    time = time.strip() if isinstance(time, str) else ""
    if not date:
        return time
    if not time:
        date, time = split_time(date)
    cal = _cal(calendar)
    p = parse_story_date(date, cal)
    if not p or p["type"] == "fantasy":
        return _join(date, time)
    if p["type"] == "custom":
        return _join(_custom_str(p["year"], p["month_index"], p["day"], cal), time)
    return _join(_standard_str(p, weekday), time)


def time_reference(date: str, calendar=None) -> str:
    """Строка [Время (справка)|…] для блока состояния; "" — без даты.

    Модель путается в «вчера/позавчера» относительно даты сюжета — справка
    даёт ей готовые даты с днями недели.
    """
    p = parse_story_date(date, calendar)
    if not p:
        return ""
    if p["type"] == "fantasy":
        return "[Время (справка)|Режим фэнтезийного календаря, см. относительные метки времени в сюжетной линии]"
    if p["type"] == "custom":
        return "[Время (справка)|Пользовательский календарь, см. относительное время в сюжетной линии]"
    base = _standard_days(p)
    parts = []
    for label, offset in (("вчера", 1), ("позавчера", 2), ("3 дня назад", 3)):
        z = base - offset
        _, m, d = _civil_from_days(z)
        parts.append(f"{label}={m}/{d} ({WEEKDAYS_RU[_weekday(z)]})")
    return "[Время (справка)|" + "|".join(parts) + "]"


def subtract_days(date: str, n: int, calendar=None) -> str:
    """Дата на n дней раньше; фэнтези и неразобранное возвращаются как есть.

    В отличие от плагина, префикс эпохи сохраняется (плагин его терял).
    """
    if not isinstance(date, str):
        return ""
    cal = _cal(calendar)
    p = parse_story_date(date, cal)
    if not p or p["type"] == "fantasy":
        return date
    try:
        n = int(n)
    except (TypeError, ValueError):
        return date
    if p["type"] == "custom":
        linear = _custom_linear(p, cal) - n
        if linear < 0:
            return date
        year, rem = divmod(linear, cal["year_len"])
        index = 0
        while index < len(cal["months"]) - 1 and rem >= cal["months"][index]["days"]:
            rem -= cal["months"][index]["days"]
            index += 1
        return _custom_str(year if p["year"] is not None else None, index, rem + 1, cal)
    y, m, d = _civil_from_days(_days_from_civil(p["year"] or DEFAULT_YEAR, p["month"], p["day"]) - n)
    if p["prefix"]:
        return _standard_str(_result("standard", "", year=y if p["year"] else None,
                                     month=m, day=d, prefix=p["prefix"]), False)
    return f"{y}/{m}/{d}" if p["year"] else f"{m}/{d}"


# ---- Возраст ---------------------------------------------------------------

def _parse_birthday(text: str) -> tuple[int | None, int, int] | None:
    """День рождения → (год|None, месяц, день).

    Сначала общим разбором дат: регэксп плагина читал русское «15.03.1990»
    как год 15, месяц 3, день 19. Затем — регэкспы плагина для записей
    с мусором вокруг («род. 1990-03-15»).
    """
    if not isinstance(text, str) or not text.strip():
        return None
    p = parse_story_date(text)
    if p and p["type"] == "standard":
        return p["year"], p["month"], p["day"]
    m = re.search(r"(\d{2,4})[/\-.](\d{1,2})[/\-.](\d{1,2})", text)
    if m:
        return int(m.group(1)), int(m.group(2)), int(m.group(3))
    m = re.fullmatch(r"\s*(\d{1,2})[/\-.](\d{1,2})\s*", text)
    if m:
        return None, int(m.group(1)), int(m.group(2))
    return None


def _before(m1: int, d1: int, m2: int, d2: int) -> bool:
    return m1 < m2 or (m1 == m2 and (d1 or 1) < (d2 or 1))


def current_age(age: str, birthday: str, age_ref: str, current_date: str, calendar=None) -> str:
    """Возраст NPC на текущую дату сюжета (calcCurrentAge плагина).

    Возраст, записанный моделью, верен на дату `age_ref` (когда его написали).
    Прошли годы сюжета — возраст растёт. Нечисловой возраст, фэнтези-даты и
    даты без года — возраст как записан.
    """
    if isinstance(age, str):
        original = age
    elif isinstance(age, (int, float)) and not isinstance(age, bool):
        original = str(age)
    else:
        original = ""
    if not original or not isinstance(current_date, str) or not current_date:
        return original
    age_num = _js_int(original)
    if age_num is None:
        return original
    cur = parse_story_date(current_date, calendar)
    if not cur or cur["type"] != "standard" or not cur["year"]:
        return original
    bd = _parse_birthday(birthday)
    if bd and bd[0]:
        # Полный день рождения — возраст точно.
        years = cur["year"] - bd[0]
        if _before(cur["month"], cur["day"], bd[1], bd[2]):
            years -= 1
        return str(max(0, years))
    ref = parse_story_date(age_ref, calendar) if isinstance(age_ref, str) and age_ref else None
    if not ref or ref["type"] != "standard" or not ref["year"]:
        return original
    if bd:
        # Только день и месяц: год рождения выводим из возраста на дату age_ref.
        birth_year = ref["year"] - age_num
        if _before(ref["month"], ref["day"], bd[1], bd[2]):
            birth_year -= 1
        now_age = cur["year"] - birth_year
        if _before(cur["month"], cur["day"], bd[1], bd[2]):
            now_age -= 1
        return str(now_age) if now_age > age_num else original
    year_diff = cur["year"] - ref["year"]
    if _before(cur["month"], cur["day"], ref["month"], ref["day"]):
        year_diff -= 1
    return str(age_num + year_diff) if year_diff > 0 else original


# ---- Время суток и календарь в промпте ------------------------------------

def time_of_day(time_str: str) -> str:
    """Время суток по часам: ночь [0,5), утро [5,11), день [11,17), вечер [17,23).

    Без часов — по слову («утром», «на закате», 下午), иначе "".
    """
    if not isinstance(time_str, str) or not time_str.strip():
        return ""
    hour = None
    m = re.search(r"(\d{1,2})[:：]", time_str)
    if m:
        hour = int(m.group(1))
    else:
        for rx, word in _TOD_WORDS:
            if rx.search(time_str):
                return word
        m = re.search(r"([子丑寅卯辰巳午未申酉戌亥])时?(?:初|正)?", time_str)
        if m:
            base = _EARTHLY_BRANCH_HOURS[m.group(1)]
            hour = (base + 1) % 24 if "正" in m.group(0) else base
    if hour is None:
        return ""
    if 5 <= hour < 11:
        return "утро"
    if 11 <= hour < 17:
        return "день"
    if 17 <= hour < 23:
        return "вечер"
    return "ночь"


def calendar_prompt(calendar_cfg) -> str:
    """Правило для модели о своём календаре; "" — календарь выключен/битый."""
    cal = _cal(calendar_cfg)
    if not cal:
        return ""
    months = cal["months"]
    n = len(months)
    word = "месяца" if n % 10 == 1 and n % 100 != 11 else "месяцев"
    listing = ", ".join(f"{m['name']}({m['days']})" for m in months)
    sample = months[0]["name"]
    return (f"В этом мире свой календарь из {n} {word}: {listing}. "
            f"Пишите дату как «Год N, <день> <месяц>», например «Год 1, 15 {sample}» "
            f"(время — после даты: «Год 1, 15 {sample} 14:00»). "
            f"Не используйте григорианские даты вида М/Д и слова «сегодня/вчера».")
