"""
Время сюжета Horae (backend/horae_time.py) — чистый модуль, без БД и сети.

Проверяем порт timeUtils.js плагина: разбор дат (включая то, что плагин читал
неверно: русское D.M.YYYY, месяцы словами, порядковые «третий день»),
относительные метки хронологии в прошлое и будущее, справку по времени,
свой календарь с годами и возраст NPC.
"""
import datetime as dt
import random

import pytest

from backend import horae_time as ht

CAL = {"enabled": True, "months": [{"name": "Инея", "days": 30}, {"name": "Ветров", "days": 31},
                                   {"name": "Жатвы", "days": 30}]}
TODAY = "2026/2/4"  # среда


def _p(text, calendar=None):
    return ht.parse_story_date(text, calendar)


def _ymd(p):
    return p["type"], p["year"], p["month"], p["day"]


# ==================== Календарная арифметика ====================

def test_day_numbers_match_datetime_and_survive_odd_years():
    rng = random.Random(7)
    for _ in range(2000):
        d = dt.date(1, 1, 1) + dt.timedelta(days=rng.randrange(0, 3_650_000))
        z = ht._days_from_civil(d.year, d.month, d.day)
        assert z == (d - dt.date(1970, 1, 1)).days
        assert ht._civil_from_days(z) == (d.year, d.month, d.day)
        assert ht._weekday(z) == d.weekday()
    # Фэнтези-эпохи: год 0 и отрицательный — datetime их не умеет, мы — да.
    assert ht._civil_from_days(ht._days_from_civil(-5, 3, 1)) == (-5, 3, 1)
    # Переполнение дня — как у JS Date: 30 февраля = 2 марта.
    assert ht._civil_from_days(ht._days_from_civil(2026, 2, 30)) == (2026, 3, 2)


# ==================== split_time ====================

@pytest.mark.parametrize("value,expected", [
    ("2026/2/4 15:00", ("2026/2/4", "15:00")),
    ("2026/2/4", ("2026/2/4", "")),
    ("  Инея 3 7:05 ", ("Инея 3", "7:05")),
    ("八月十六日20:30", ("八月十六日", "20:30")),   # \b как в JS: ASCII-граница
    (None, ("", "")),
])
def test_split_time(value, expected):
    assert ht.split_time(value) == expected


# ==================== Разбор: стандартные даты ====================

@pytest.mark.parametrize("text", ["2026/2/4", "2026-02-04", "2026.2.4", "2026/2/4 15:00",
                                  "2026/2/4 (ср)", "2026/2/4 (三)", "04.02.2026", "4.2.2026 15:00",
                                  "4 февраля 2026", "4 февраля 2026 года", "4-го февраля 2026 г.",
                                  "Среда, 4 февраля 2026", "4 фев. 2026"])
def test_standard_forms_parse_to_same_date(text):
    p = _p(text)
    assert _ymd(p) == ("standard", 2026, 2, 4)
    assert p["prefix"] == ""
    assert p["raw"] == text.strip()


def test_russian_day_first_is_not_fantasy():
    # Плагин читал «04.02.2026» как фэнтези с днём 4 — теперь это 4 февраля.
    assert _ymd(_p("04.02.2026")) == ("standard", 2026, 2, 4)
    assert _ymd(_p("12.03.1024")) == ("standard", 1024, 3, 12)
    assert _ymd(_p("04.02")) == ("standard", None, 2, 4)


def test_month_day_without_year():
    assert _ymd(_p("2/4")) == ("standard", None, 2, 4)
    assert _ymd(_p("15 марта")) == ("standard", None, 3, 15)


def test_russian_month_words_with_era_prefix():
    p = _p("15 марта 1024 г.")
    assert _ymd(p) == ("standard", 1024, 3, 15) and p["prefix"] == ""
    p = _p("Эра Драконов, 15 марта 1024 г.")
    assert _ymd(p) == ("standard", 1024, 3, 15)
    assert p["prefix"] == "Эра Драконов"


def test_month_only_is_fantasy_without_year_as_day():
    p = _p("февраль 2026")
    assert p["type"] == "fantasy" and p["month_id"] == "февраль" and p["day"] is None


@pytest.mark.parametrize("text,expected,prefix", [
    ("2024年8月16日", (2024, 8, 16), ""),
    ("萬曆十五年八月十六日", (15, 8, 16), "萬曆"),
    ("玄昭十五年 8/16", (15, 8, 16), "玄昭"),
    ("二〇二四年八月十六日", (2024, 8, 16), ""),
    ("八月十六日", (None, 8, 16), ""),
    ("8月16日", (None, 8, 16), ""),
])
def test_chinese_forms_kept_from_plugin(text, expected, prefix):
    p = _p(text)
    assert p["type"] == "standard"
    assert (p["year"], p["month"], p["day"]) == expected
    assert p["prefix"] == prefix


def test_invalid_month_day_does_not_become_standard():
    assert _p("2026/13/4")["type"] == "fantasy"
    assert _p("") is None and _p("   ") is None and _p(None) is None


# ==================== Разбор: фэнтези и свой календарь ====================

@pytest.mark.parametrize("text,month_id,day", [
    ("третий день месяца инея", "инея", 3),
    ("Третий день Месяца Инея", "инея", 3),       # регистр месяца не важен
    ("двадцать первое число месяца Мороза", "мороза", 21),
    ("3-й день Инея", None, 3),
    ("Год 1024, 3-й день", None, 3),               # 3-й, а не первое число 1024
    ("春之月3日", "春之月", 3),
    ("第三日", None, 3),
    ("Day 5", None, 5),
])
def test_fantasy_month_and_day(text, month_id, day):
    p = _p(text)
    assert p["type"] == "fantasy"
    assert p["month_id"] == month_id
    assert p["day"] == day


def test_fantasy_clock_is_not_taken_as_day():
    # Раньше «15» из часов становилось днём фэнтези-даты.
    assert _p("месяц Инея 15:00")["day"] is None


def test_unknown_marker_and_nothing_found():
    for text in ("xx", "??", "хх.хх"):
        p = _p(text)
        assert p["type"] == "fantasy" and p["day"] is None and p["month_id"] is None
    assert _p("Эпоха ветров") is None


@pytest.mark.parametrize("text,year,day", [
    ("Год 5, 3 Инея", 5, 3),
    ("5 год, Инея 3", 5, 3),
    ("Год 5 Инея 3", 5, 3),            # год вырезается раньше, чем ищется день
    ("3-й день Инея", None, 3),
    ("третий день месяца инея", None, 3),
    ("15 Инея 1024", 1024, 15),
    ("ИНЕЯ 30", None, 30),
])
def test_custom_calendar_dates(text, year, day):
    p = _p(text, CAL)
    assert p["type"] == "custom"
    assert (p["year"], p["month_index"], p["day"]) == (year, 0, day)
    assert p["month_id"] == "Инея"


def test_custom_calendar_rejects_day_beyond_month():
    assert _p("Инея 31", CAL)["type"] == "fantasy"


def test_normalize_calendar():
    cal = ht.normalize_calendar(CAL)
    assert cal["offsets"] == [0, 30, 61] and cal["year_len"] == 91
    assert ht.normalize_calendar(cal) == cal                      # уже нормализованный
    assert ht.normalize_calendar({"enabled": True, "monthNames": ["А", "Б"], "monthDays": [10, "20"]})[
        "year_len"] == 30                                          # формат плагина
    for bad in ({**CAL, "enabled": False}, {"enabled": True, "months": []},
                {"enabled": True, "months": [{"name": "А", "days": 0}]},
                {"enabled": True, "months": [{"name": " ", "days": 5}]}, None, "x"):
        assert ht.normalize_calendar(bad) is None


# ==================== Относительные дни ====================

def test_relative_days_rules():
    assert ht.relative_days("2026/2/3", TODAY) == 1
    assert ht.relative_days("2026/2/5", TODAY) == -1
    assert ht.relative_days("2026/2/4 10:00", "2026/2/4 15:00") == 0   # время срезается
    assert ht.relative_days("03.02.2026", "2026/2/4") == 1              # разные записи — одна ось
    assert ht.relative_days("2/3", TODAY) == 1                          # год берётся у соседа
    assert ht.relative_days("2/28", "3/1") == 2                         # по умолчанию 2024 (високосный)
    assert ht.relative_days("萬曆十五年八月十六日", "崇禎十五年八月十六日") is None  # разные эпохи
    assert ht.relative_days("", TODAY) is None
    assert ht.relative_days("Эпоха ветров", TODAY) is None


def test_relative_days_fantasy_and_custom():
    assert ht.relative_days("третий день месяца инея", "пятый день месяца Инея") == 2
    assert ht.relative_days("霜月3日", "火月25日") is None            # разные месяцы не упорядочить
    assert ht.relative_days("第3日", "第5日") == 2
    assert ht.relative_days("xx", "第5日") == ht.SPECIAL_EARLIER
    assert ht.relative_days("Год 5, 3 Инея", "Год 5, 2 Ветров", CAL) == 29
    assert ht.relative_days("Год 5, 30 Жатвы", "Год 6, 1 Инея", CAL) == 1
    assert ht.relative_days("3 Инея", TODAY, CAL) is None               # свой календарь с григорианским


# ==================== Относительные метки ====================

@pytest.mark.parametrize("from_date,label", [
    (TODAY, "сегодня"),
    ("2026/2/3", "вчера"),
    ("2026/2/2", "позавчера"),
    ("2026/2/1", "3 дня назад"),
    ("2026/1/30", "5 дн. назад"),
    ("2026/1/27", "прошлый вт"),               # 8 дней, прошлая неделя
    ("2026/1/22", "позапрошлый чт"),           # 13 дней, неделя до прошлой
    ("2026/1/20", "прошлый месяц 20-го"),
    ("2025/10/4", "4 мес. назад"),
    ("2025/3/15", "прошлый год 3/15"),
    ("2024/3/15", "позапрошлый год 3/15"),
    ("2022/1/1", "4 г. 1 мес. назад"),
    ("2010/1/1", "16 г. назад"),
])
def test_relative_label_past(from_date, label):
    assert ht.relative_label(from_date, TODAY) == label


@pytest.mark.parametrize("from_date,label", [
    ("2026/2/5", "завтра"),
    ("2026/2/6", "послезавтра"),
    ("2026/2/7", "через 3 дня"),
    ("2026/2/9", "через 5 дн."),
    ("2026/2/12", "в следующий чт"),
    ("2026/2/17", "через неделю во вт"),
    ("2026/3/15", "в следующем месяце 15-го"),
    ("2026/7/4", "через 5 мес."),
    ("2027/2/4", "через 1 г."),
    ("2027/6/4", "через 1 г. 4 мес."),
])
def test_relative_label_future(from_date, label):
    assert ht.relative_label(from_date, TODAY) == label


def test_relative_label_non_standard_and_unknown():
    assert ht.relative_label("третий день месяца инея", "пятый день месяца инея") == "позавчера"
    assert ht.relative_label("Год 5, 30 Жатвы", "Год 6, 1 Инея", CAL) == "вчера"
    assert ht.relative_label("xx", "第5日") == ""                       # особое «раньше» — без метки
    assert ht.relative_label("萬曆十五年八月十六日", "崇禎十五年八月十六日") == ""
    # Разные месяцы фэнтези: неделя/месяц без реальных дат не считаются.
    assert ht.relative_label("第1日", "第9日") == "8 дн. назад"


def test_relative_label_without_year_matches_time_reference(monkeypatch):
    # Нет года — текущий год и для метки, и для справки: 2026 не високосный,
    # 28 февраля — «вчера» от 1 марта (по 2024-му вышло бы «позавчера»).
    monkeypatch.setattr(ht, "_current_year", lambda: 2026)
    assert ht.relative_label("2/28", "3/1") == "вчера"
    assert "вчера=2/28" in ht.time_reference("3/1")


# ==================== Вывод ====================

def test_weekday_ru():
    assert ht.weekday_ru(TODAY) == "ср"
    assert ht.weekday_ru("2026/2/1") == "вс"
    assert ht.weekday_ru("Инея 3") == ""


def test_weekday_ru_without_year_uses_current_year(monkeypatch):
    monkeypatch.setattr(ht, "_current_year", lambda: 2026)
    assert ht.weekday_ru("2/4") == "ср"


def test_format_date():
    assert ht.format_date(TODAY, "15:00") == "2026/2/4 (ср) 15:00"
    assert ht.format_date(TODAY, "15:00", weekday=False) == "2026/2/4 15:00"
    assert ht.format_date("4 февраля 2026 года") == "2026/2/4 (ср)"   # нормализуется
    assert ht.format_date("2026/2/4 15:00") == "2026/2/4 (ср) 15:00"  # время из даты не теряется
    assert ht.format_date("Год 5, 3 Инея", "15:00", CAL) == "Год 5, 3 Инея 15:00"
    assert ht.format_date("инея 3", "", CAL) == "3 Инея"
    assert ht.format_date("третий день месяца инея", "утро") == "третий день месяца инея утро"
    assert ht.format_date("", "15:00") == "15:00"
    assert ht.format_date("", "") == ""


def test_format_date_keeps_era_prefix_round_trip():
    text = ht.format_date("Эра Драконов, 15 марта 1024 г.", weekday=False)
    assert text == "Эра Драконов, 15 марта 1024 г."
    p = _p(ht.format_date("萬曆十五年八月十六日"))
    assert (p["prefix"], p["year"], p["month"], p["day"]) == ("萬曆", 15, 8, 16)


def test_time_reference():
    assert ht.time_reference(TODAY) == "[Время (справка)|вчера=2/3 (вт)|позавчера=2/2 (пн)|3 дня назад=2/1 (вс)]"
    assert ht.time_reference("2026/3/1") == (
        "[Время (справка)|вчера=2/28 (сб)|позавчера=2/27 (пт)|3 дня назад=2/26 (чт)]")
    assert ht.time_reference("третий день месяца инея") == (
        "[Время (справка)|Режим фэнтезийного календаря, см. относительные метки времени в сюжетной линии]")
    assert ht.time_reference("3 Инея", CAL) == (
        "[Время (справка)|Пользовательский календарь, см. относительное время в сюжетной линии]")
    assert ht.time_reference("") == "" and ht.time_reference("Эпоха ветров") == ""


def test_subtract_days():
    assert ht.subtract_days("2026/3/1", 1) == "2026/2/28"
    assert ht.subtract_days("3/1", 1) == "2/29"                         # без года — 2024, как в плагине
    assert ht.subtract_days("Эра Драконов, 1 марта 1024 г.", 1) == "Эра Драконов, 29 февраля 1024 г."
    assert ht.subtract_days("Год 5, 1 Инея", 1, CAL) == "Год 4, 30 Жатвы"
    assert ht.subtract_days("5 Ветров", 10, CAL) == "25 Инея"
    assert ht.subtract_days("1 Инея", 1, CAL) == "1 Инея"               # раньше нулевого года нельзя
    assert ht.subtract_days("третий день месяца инея", 1) == "третий день месяца инея"


# ==================== Возраст ====================

def test_current_age_without_birthday_grows_from_reference():
    assert ht.current_age("30", "", "2020/5/1", TODAY) == "35"          # 5 полных лет
    assert ht.current_age("30", "", "2020/1/1", TODAY) == "36"
    assert ht.current_age("35 лет", "", "2020/5/1", TODAY) == "40"      # parseInt: ведущие цифры
    assert ht.current_age("30", "", "2026/1/1", TODAY) == "30"          # год не прошёл


def test_current_age_with_birthday():
    assert ht.current_age("30", "1990/3/15", "", TODAY) == "35"         # день рождения ещё впереди
    assert ht.current_age("30", "1990/1/15", "", TODAY) == "36"
    assert ht.current_age("30", "15.03.1990", "", TODAY) == "35"        # русская запись дня рождения
    assert ht.current_age("30", "род. 1990-03-15", "", TODAY) == "35"
    # Только день и месяц: год рождения из возраста на дату записи.
    assert ht.current_age("30", "3/15", "2020/5/1", TODAY) == "35"
    assert ht.current_age("30", "3/15", "2026/1/1", TODAY) == "30"      # моложе записанного не станет


def test_current_age_keeps_original_when_not_computable():
    assert ht.current_age("около тридцати", "", "2020/5/1", TODAY) == "около тридцати"
    assert ht.current_age("30", "", "", TODAY) == "30"                  # нет даты записи
    assert ht.current_age("30", "", "2020/5/1", "третий день месяца инея") == "30"
    assert ht.current_age("30", "", "2020/5/1", "2/4") == "30"          # текущая дата без года
    assert ht.current_age("", "", "", TODAY) == ""


@pytest.mark.parametrize("bad", [None, 5, [], {}, "   "])
def test_garbage_inputs_never_raise(bad):
    assert ht.parse_story_date(bad) is None
    assert ht.relative_days(bad, TODAY) is None
    assert ht.relative_label(bad, TODAY) == ""
    assert ht.format_date(bad) == "" and ht.weekday_ru(bad) == "" and ht.time_reference(bad) == ""
    assert ht.time_of_day(bad) == ""
    assert ht.current_age(None if bad == 5 else bad, bad, bad, bad) in ("", "   ")
    assert isinstance(ht.subtract_days(bad, 1), str)


# ==================== Время суток и календарь в промпте ====================

@pytest.mark.parametrize("value,expected", [
    ("03:00", "ночь"), ("05:00", "утро"), ("10:59", "утро"), ("11:00", "день"),
    ("16:59", "день"), ("17:00", "вечер"), ("22:30", "вечер"), ("23:00", "ночь"),
    ("", ""), ("скоро", ""), ("утром", "утро"), ("下午", "день"),
])
def test_time_of_day(value, expected):
    assert ht.time_of_day(value) == expected


def test_calendar_prompt():
    text = ht.calendar_prompt(CAL)
    assert text.startswith("В этом мире свой календарь из 3 месяцев: Инея(30), Ветров(31), Жатвы(30).")
    assert "«Год N, <день> <месяц>»" in text and "«Год 1, 15 Инея»" in text
    one = ht.calendar_prompt({"enabled": True, "months": [{"name": "Вечность", "days": 400}]})
    assert "из 1 месяца:" in one
    assert ht.calendar_prompt({**CAL, "enabled": False}) == ""
    assert ht.calendar_prompt(None) == ""
