"""
Импорт данных плагина Horae из SillyTavern (backend/horae_import.py) — без БД.

Проверяем перевод меты сообщения плагина (китайские уровни, {type, value}
расположения, «r-c» таблиц, _rpgChanges) в нашу META и данных chat[0]
(свёртки с переводом индексов файла в id БД, правки пользователя, таблицы,
настройки RPG) в HoraeChatState.data. Кривой вход не должен ронять импорт.
"""
import copy

import pytest

from backend import horae_import as hi


def _plugin_meta():
    return {
        "timestamp": {"story_date": "2026/2/4", "story_time": "15:00", "absolute": "2026-09-26T10:00:00Z"},
        "scene": {"location": "Таверна·зал", "characters_present": ["Вольф", "Марина", ""],
                  "atmosphere": "шумно", "scene_desc": "дубовые столы, камин"},
        "costumes": {"Вольф": "кожаная куртка", "": "ничья", "Марина": ""},
        "items": {
            "🍺️Старый квас(3 бутылки)": {"icon": "🍾", "importance": "重要", "holder": "{{user}}",
                                              "location": "полка кладовой", "description": "кисловатый",
                                              "_id": "001", "_locked": True},
            "Кинжал": {"icon": None, "importance": "关键", "holder": None, "location": ""},
            "Сломанный": "не словарь",
        },
        "deletedItems": ["Пиво", "Пиво", ""],
        "deletedAgenda": ["купить хлеб"],
        "events": [
            {"is_important": False, "level": "一般", "summary": "Вольф вошёл в таверну"},
            {"is_important": True, "level": "关键", "summary": "Марина раскрыла тайну"},
            {"is_important": True, "level": "摘要", "summary": "карточка свёртки", "isSummary": True,
             "_summaryId": "as_1"},
            {"is_important": True, "level": "重要", "summary": ""},
        ],
        "affection": {"Вольф": {"type": "absolute", "value": 30}, "Марина": {"type": "relative", "value": "+5"},
                      "Старик": 3, "Кот": "-2.5", "Сломан": {"type": "relative", "value": "abc"}},
        "npcs": {"Вольф": {"appearance": "серебряный мех", "personality": "", "age": 35,
                           "first_seen": "2026-09-26T10:00:00Z", "_aliases": ["Волк"]}},
        "agenda": [{"date": "2026/2/10", "text": "Вернуть кинжал", "source": "ai", "done": False},
                   {"text": "удалённое", "_deleted": True}, {"text": "сделано", "done": True}],
        "mood": {"Марина": "напряжена"},
        "relationships": [{"from": "Вольф", "to": "Марина", "type": "друзья", "note": ""},
                          {"from": "А", "to": "", "type": "враги"},
                          {"from": "Вольф", "to": "{{user}}", "type": "наставник", "_userEdited": True}],
        "tableContributions": [{"name": "Квесты", "updates": {"1-1": "Найти меч", "1-2": "", "bad": "x"}},
                               {"name": "Квесты", "updates": {"2-1": "правка"}, "_isUserEdit": True}],
        "_rpgChanges": {
            "bars": {"Вольф": {"hp": [80, 100, "Здоровье"]}}, "status": {},
            "removedSkills": [{"owner": "Вольф", "name": "Рык"}],
            "attributes": {"Вольф": {"str": 60}},
            "equipment": [{"owner": "Вольф", "slot": "Голова", "name": "Шлем", "attrs": {"def": 2}}],
            "baseChanges": [{"path": "Поместье", "field": "level", "value": 2}],
            "currency": [{"owner": "Вольф", "name": "Золото", "value": 10, "isDelta": True},
                         {"owner": "Вольф", "name": "Серебро", "value": "много", "isDelta": False}],
            "levels": {"Вольф": 5},
        },
    }


# ==================== Мета сообщения ====================

def test_convert_message_meta_full():
    meta = hi.convert_message_meta(_plugin_meta())
    assert meta["source"] == "import"
    assert meta["time"] == {"date": "2026/2/4", "time": "15:00"}
    assert meta["scene"] == {"location": "Таверна·зал", "atmosphere": "шумно",
                             "characters": ["Вольф", "Марина"],
                             "desc": [{"location": "Таверна·зал", "desc": "дубовые столы, камин"}]}
    assert meta["costumes"] == {"Вольф": "кожаная куртка"}
    assert meta["mood"] == {"Марина": "напряжена"}
    assert meta["items_removed"] == ["Пиво"]
    assert meta["agenda"] == [{"date": "2026/2/10", "text": "Вернуть кинжал"}]
    assert meta["agenda_done"] == ["купить хлеб"]
    # Отношение, правленное пользователем, придёт правкой rel.set, а не словами ИИ.
    assert meta["relationships"] == [{"from": "Вольф", "to": "Марина", "type": "друзья", "note": ""}]


def test_items_importance_icon_and_variation_selector():
    items = hi.convert_message_meta(_plugin_meta())["items"]
    assert set(items) == {"🍺Старый квас(3 бутылки)", "Кинжал"}     # U+FE0F срезан, мусор пропущен
    assert items["🍺Старый квас(3 бутылки)"] == {
        "icon": "🍾", "importance": "!", "holder": "{{user}}", "location": "полка кладовой",
        "description": "кисловатый"}
    # Нет описания — нет ключа («не перезаписывать»); null → пустая строка.
    assert items["Кинжал"] == {"icon": "", "importance": "!!", "holder": "", "location": ""}


@pytest.mark.parametrize("raw,expected", [
    ("一般", "normal"), ("重要", "important"), ("关键", "critical"), ("關鍵", "critical"),
    ("normal", "normal"), ("Important", "important"), ("critical", "critical"), ("Key", "critical"),
    ("важное", "important"), ("ключевое", "critical"),
])
def test_event_levels(raw, expected):
    meta = hi.convert_message_meta({"events": [{"level": raw, "summary": "событие"}]})
    assert meta["events"] == [{"level": expected, "text": "событие"}]


def test_summary_cards_and_empty_events_are_skipped():
    events = hi.convert_message_meta(_plugin_meta())["events"]
    assert events == [{"level": "normal", "text": "Вольф вошёл в таверну"},
                      {"level": "critical", "text": "Марина раскрыла тайну"}]
    only_cards = {"events": [{"level": "摘要", "summary": "свёртка", "isSummary": True, "_summaryId": "cs_1"},
                             {"level": "回顾", "summary": "пересказ", "isSummary": True, "_carryoverSeed": True},
                             {"level": "重要", "summary": "с id", "_summaryId": "ms_2"}]}
    assert hi.convert_message_meta(only_cards) is None


def test_legacy_single_event():
    meta = hi.convert_message_meta({"event": {"is_important": True, "level": "", "summary": "старое"}})
    assert meta["events"] == [{"level": "important", "text": "старое"}]


def test_affection_modes():
    aff = hi.convert_message_meta(_plugin_meta())["affection"]
    assert aff == {"Вольф": {"mode": "set", "value": 30.0}, "Марина": {"mode": "add", "value": 5.0},
                   "Старик": {"mode": "add", "value": 3.0}, "Кот": {"mode": "add", "value": -2.5}}


def test_npcs_keep_only_present_fields():
    npcs = hi.convert_message_meta(_plugin_meta())["npcs"]
    assert npcs == {"Вольф": {"appearance": "серебряный мех", "age": "35"}}
    # NPC без полей — всё равно появление персонажа.
    assert hi.convert_message_meta({"npcs": {"Тень": {"first_seen": "x"}}})["npcs"] == {"Тень": {}}


def test_table_contributions_skip_user_edit_snapshot():
    assert hi.convert_message_meta(_plugin_meta())["tables"] == [
        {"name": "Квесты", "cells": {"1,1": "Найти меч"}}]


def test_rpg_changes_renamed():
    src = _plugin_meta()
    rpg = hi.convert_message_meta(src)["rpg"]
    assert rpg == {
        "bars": {"Вольф": {"hp": [80, 100, "Здоровье"]}},
        "skills_removed": [{"owner": "Вольф", "name": "Рык"}],
        "attrs": {"Вольф": {"str": 60}},
        "equip": [{"owner": "Вольф", "slot": "Голова", "name": "Шлем", "attrs": {"def": 2}}],
        "base": [{"path": "Поместье", "field": "level", "value": 2}],
        "currency": [{"owner": "Вольф", "name": "Золото", "value": 10, "delta": True}],
        "levels": {"Вольф": 5},
    }
    rpg["bars"]["Вольф"]["hp"][0] = 1
    assert src["_rpgChanges"]["bars"]["Вольф"]["hp"][0] == 80       # вход не делит объекты с выходом


def test_story_time_split_from_date_when_missing():
    meta = hi.convert_message_meta({"timestamp": {"story_date": "2026/2/4 15:00", "story_time": ""}})
    assert meta["time"] == {"date": "2026/2/4", "time": "15:00"}


def test_scene_desc_pairs_and_desc_without_location():
    meta = hi.convert_message_meta({"scene": {"location": "Лес", "scene_desc": "последнее",
                                              "_descPairs": [{"location": "Дом", "desc": "тепло"},
                                                             {"location": "Лес", "desc": "темно"}]}})
    assert meta["scene"]["desc"] == [{"location": "Дом", "desc": "тепло"}, {"location": "Лес", "desc": "темно"}]
    # scene_desc без места привязать не к чему.
    assert hi.convert_message_meta({"scene": {"scene_desc": "где-то"}}) is None


def test_empty_and_garbage_meta_return_none():
    empty = {"timestamp": {"story_date": "", "story_time": "", "absolute": "x"},
             "scene": {"location": "", "characters_present": [], "atmosphere": ""}, "costumes": {},
             "items": {}, "deletedItems": [], "deletedAgenda": [], "events": [], "affection": {},
             "npcs": {}, "agenda": [], "mood": {}, "relationships": []}
    assert hi.convert_message_meta(empty) is None
    garbage = {"items": [1, 2], "events": "x", "affection": None, "npcs": {"А": "строка"},
               "timestamp": 5, "scene": "?", "tableContributions": {"a": 1}, "_rpgChanges": [1]}
    assert hi.convert_message_meta(garbage) is None
    assert hi.convert_message_meta(None) is None
    assert hi.convert_message_meta("строка") is None


def test_inline_tags_win_for_scalars_and_merge_collections():
    horae_meta = {"timestamp": {"story_date": "2026/2/4", "story_time": "10:00"},
                  "scene": {"location": "Таверна", "characters_present": ["Вольф"]},
                  "costumes": {"Вольф": "куртка", "Марина": "платье"},
                  "deletedItems": ["Пиво"],
                  "events": [{"level": "一般", "summary": "старая редакция"}],
                  "agenda": [{"text": "Вернуть кинжал"}],
                  "npcs": {"Вольф": {"appearance": "мех", "age": "35"}},
                  "tableContributions": [{"name": "Квесты", "updates": {"1-1": "a", "1-2": "b"}}]}
    text_meta = {"time": {"date": "2026/2/5", "time": ""},
                 "scene": {"location": "Площадь", "characters": []},
                 "costumes": {"Вольф": "плащ"},
                 "items_removed": ["Меч"],
                 "events": [{"level": "important", "text": "новая редакция"}],
                 "agenda": [{"date": "", "text": "Вернуть кинжал"}, {"date": "", "text": "Найти меч"}],
                 "npcs": {"Вольф": {"age": "36"}},
                 "tables": [{"name": "Квесты", "cells": {"1,2": "B"}}],
                 "raw": "<horae>time:2026/2/5</horae>", "source": "tags"}
    meta = hi.convert_message_meta(horae_meta, text_meta=text_meta)
    assert meta["time"] == {"date": "2026/2/5", "time": "10:00"}
    assert meta["scene"] == {"location": "Площадь", "characters": ["Вольф"]}
    assert meta["costumes"] == {"Вольф": "плащ", "Марина": "платье"}
    assert meta["items_removed"] == ["Пиво", "Меч"]
    assert meta["events"] == [{"level": "important", "text": "новая редакция"}]
    assert [a["text"] for a in meta["agenda"]] == ["Вернуть кинжал", "Найти меч"]
    assert meta["npcs"] == {"Вольф": {"appearance": "мех", "age": "36"}}
    assert meta["tables"] == [{"name": "Квесты", "cells": {"1,1": "a", "1,2": "B"}}]
    assert meta["raw"] == "<horae>time:2026/2/5</horae>"
    assert meta["source"] == "import"


def test_inline_tags_alone():
    meta = hi.convert_message_meta({}, text_meta={"scene": {"location": "Лес"}, "source": "tags"})
    assert meta == {"scene": {"location": "Лес"}, "source": "import"}


def test_is_side():
    assert hi.is_side({"_skipHorae": True}) is True
    assert hi.is_side({"_skipHorae": False}) is False
    assert hi.is_side({}) is False and hi.is_side(None) is False
    # Побочная сцена всё равно конвертируется — флаг читает вызывающий.
    assert hi.convert_message_meta({"_skipHorae": True, "scene": {"location": "Сон"}})["scene"] == {
        "location": "Сон"}


def test_summary_card_texts():
    assert hi.summary_card_texts(_plugin_meta()) == {"as_1": "карточка свёртки"}
    assert hi.summary_card_texts(None) == {}


# ==================== Данные чата ====================

# Индексы 3 и 6 не импортированы (системные сообщения) — у них нет id.
INDEX_TO_MID = {0: 100, 1: 101, 2: 102, 4: 104, 5: 105, 7: 107}


def _orig(idx, date):
    return {"msgIdx": idx, "evtIdx": 0, "event": {"summary": "e"}, "timestamp": {"story_date": date}}


def _chat0():
    return {
        "events": [{"is_important": True, "level": "回顾", "summary": "Пересказ прошлого чата",
                    "isSummary": True, "_carryoverSeed": True}],
        "autoSummaries": [
            {"id": "as_1700", "range": [3, 6], "coveredIndices": [3, 4, 5, 6], "summaryText": "Авто",
             "originalEvents": [_orig(4, "2026/2/4"), _orig(5, ""), _orig(5, "2026/2/6")],
             "depth": 1, "active": True, "createdAt": "2026-09-01T00:00:00Z", "auto": True},
            {"id": "cs_1800", "range": [1, 7], "summaryText": "Сжатие", "depth": 2, "active": False,
             "auto": False, "originalEvents": [],
             "mergedSummaries": [
                 {"id": "as_1600", "range": [1, 2], "summaryText": "Ранняя", "depth": 1, "active": True,
                  "auto": True, "originalEvents": [_orig(1, "2026/1/1"), _orig(2, "2026/1/3")]},
                 {"id": "ms_1650", "range": [3, 3], "summaryText": "Недостижимая", "manual": True},
             ]},
            {"id": "as_bad", "range": [6, 6], "summaryText": "Без сообщений"},
            {"id": "ms_1900", "range": [7, 4], "summaryText": "", "manual": True, "auto": False},
            {"id": "as_old", "range": [0, 1], "auto": True},
            "мусор",
        ],
        "locationMemory": {"Таверна": {"desc": "Дубовые столы", "_userEdited": True},
                           "Пещера": {"desc": "x", "_deleted": True},
                           "Лес": {"desc": "описание ИИ"}},
        "relationships": [{"from": "Вольф", "to": "Марина", "type": "брат", "note": "старший", "_userEdited": True},
                          {"from": "А", "to": "Б", "type": "ИИ"}],
        "agenda": [{"text": "Купить меч", "date": "2026/2/10", "source": "user", "done": False},
                   {"text": "из приветствия", "source": "ai"},
                   {"text": "удалено", "source": "user", "_deleted": True},
                   {"text": "Купить меч", "source": "user"}],
        "_deletedAgendaTexts": ["Старое дело", "Старое дело"],
        "_deletedNpcs": ["Гоблин"],
        "customTables": [{"id": "1700000", "name": "Квесты", "rows": 3, "cols": 3, "prompt": "Задания героя",
                          "data": {"0-1": "Задание", "1-0": "1", "1-1": "Найти меч", "4-2": "x", "1-2": ""},
                          "lockedRows": [1], "lockedCols": ["2"], "lockedCells": ["1-1", "bad"],
                          "baseData": {}, "baseRows": 3, "baseCols": 3},
                         None, 5],
        "globalTableData": {"tpl_1": {"data": {"1-1": "глоб"}, "rows": 2, "cols": 2}},
        "_rpgConfigs": {
            "reputationConfig": {"categories": [{"name": "Гильдия", "min": -50, "max": 50, "default": 0,
                                                 "subItems": ["Торговцы"]},
                                                {"name": "Удалённая"}],
                                 "_deletedCategories": ["Удалённая"]},
            "currencyConfig": {"denominations": [{"name": "Золото", "rate": 100, "emoji": "💰"}]},
            "equipmentConfig": {"locked": True, "perChar": {"Вольф": {
                "slots": [{"name": "Голова", "maxCount": 1}, {"name": "Кольцо", "maxCount": 2}, {"name": "Хвост"}],
                "_deletedSlots": ["Хвост"],
                "forms": [{"id": "human", "name": "Человек", "slots": [{"name": "Голова", "maxCount": 1}]}],
                "currentForm": "human"}}},
            "_deletedSkills": [{"owner": "Вольф", "name": "Укус"}],
            "strongholds": [{"id": "sh_1", "name": "Поместье", "level": 3, "desc": "", "parent": None,
                             "_userAdded": True},
                            {"id": "sh_2", "name": "Кузница", "level": None, "desc": "жарко", "parent": "sh_1",
                             "_userAdded": True},
                            {"id": "sh_3", "name": "От ИИ", "parent": "sh_1"}],
        },
        "rpg": {"skills": {"Вольф": [{"name": "Рык", "level": "1", "desc": "", "_userAdded": True},
                                     {"name": "От ИИ"}]},
                "reputation": {"Вольф": {"Гильдия": {"value": 20, "_userEdited": True, "subItems": {}},
                                         "Другое": {"value": 5}}}},
    }


def _data(**kw):
    return hi.convert_chat_meta(_chat0(), INDEX_TO_MID, **kw)


def test_chat_data_shape():
    data = _data()
    for key in ("v", "ops", "summaries", "tables", "table_overlays", "rpg_config", "settings", "seed",
                "pinned_npcs", "favorite_npcs", "scan", "summary_error", "seq"):
        assert key in data
    assert data["v"] == 1


def test_ops_from_user_edits_and_tombstones():
    ops = _data()["ops"]
    kinds = [(op["kind"], op.get("name") or op.get("text") or op.get("from") or op.get("path")) for op in ops]
    assert kinds[:6] == [("location.set", "Таверна"), ("location.delete", "Пещера"), ("rel.set", "Вольф"),
                         ("agenda.add", "Купить меч"), ("agenda.delete", "Старое дело"),
                         ("npc.delete", "Гоблин")]
    assert ops[0]["desc"] == "Дубовые столы"
    assert ops[2] == {**ops[2], "to": "Марина", "type": "брат", "note": "старший"}
    assert ops[3]["date"] == "2026/2/10"
    # Все правки — «до первого сообщения», id и seq идут подряд.
    assert [op["id"] for op in ops] == [f"op_{i}" for i in range(1, len(ops) + 1)]
    assert [op["seq"] for op in ops] == list(range(1, len(ops) + 1))
    assert all(op["at"] == 0 and op["created_at"] for op in ops)
    assert list(ops[0])[:4] == ["id", "seq", "at", "kind"] and list(ops[0])[-1] == "created_at"


def test_rpg_user_data_becomes_ops():
    rpg_ops = [op for op in _data()["ops"] if op["kind"].startswith("rpg.")]
    stripped = [{k: v for k, v in op.items() if k not in ("id", "seq", "at", "created_at")} for op in rpg_ops]
    assert stripped == [
        {"kind": "rpg.skill.add", "owner": "Вольф", "name": "Рык", "level": "1", "desc": ""},
        {"kind": "rpg.skill.delete", "owner": "Вольф", "name": "Укус"},
        {"kind": "rpg.rep", "owner": "Вольф", "cat": "Гильдия", "value": 20},
        {"kind": "rpg.base", "path": "Поместье", "level": 3},
        {"kind": "rpg.base", "path": "Поместье>Кузница", "desc": "жарко"},
    ]


def test_summaries_map_indices_to_nearest_mids():
    summaries = _data()["summaries"]
    assert [s["kind"] for s in summaries] == ["carry", "auto", "compress"]
    carry, auto, comp = summaries
    assert carry["range"] == [0, 0] and carry["text"] == "Пересказ прошлого чата"
    # [3, 6]: у 3 и 6 нет id — края сдвигаются внутрь, к 4 и 5.
    assert auto["range"] == [104, 105]
    assert auto["text"] == "Авто" and auto["depth"] == 1 and auto["active"] is True
    assert auto["created_at"] == "2026-09-01T00:00:00Z"
    assert (auto["date_from"], auto["date_to"], auto["events"]) == ("2026/2/4", "2026/2/6", 3)
    assert comp["range"] == [101, 107] and comp["depth"] == 2 and comp["active"] is False
    # Ребёнок без сообщений в диапазоне отброшен, второй — сохранён рекурсивно.
    assert [c["text"] for c in comp["children"]] == ["Ранняя"]
    child = comp["children"][0]
    assert child["range"] == [101, 102] and child["kind"] == "auto"
    # Даты родителя без своих событий — по детям.
    assert (comp["date_from"], comp["date_to"]) == ("2026/1/1", "2026/1/3")


def test_summary_ids_continue_op_sequence():
    data = _data()
    n_ops = len(data["ops"])
    ids = []

    def walk(items):
        for s in items:
            ids.append(s["id"])
            walk(s["children"])

    walk(data["summaries"])
    assert ids == [f"s_{i}" for i in range(n_ops + 1, n_ops + 1 + len(ids))]
    assert data["seq"] == n_ops + len(ids)


def test_summary_text_from_cards():
    texts = {"as_old": "из карточки", "ms_1900": "ручная"}
    summaries = _data(summary_texts=texts)["summaries"]
    by_text = {s["text"]: s for s in summaries}
    assert by_text["из карточки"]["range"] == [100, 101] and by_text["из карточки"]["kind"] == "auto"
    assert by_text["ручная"]["range"] == [104, 107] and by_text["ручная"]["kind"] == "manual"


def test_local_tables():
    tables = _data()["tables"]
    assert len(tables) == 1
    t = tables[0]
    assert t["id"] == "1700000" and t["name"] == "Квесты" and t["prompt"] == "Задания героя"
    assert (t["rows"], t["cols"]) == (5, 3)                  # таблица выросла под ячейку 4,2
    assert t["base"] == {"0,1": "Задание", "1,0": "1", "1,1": "Найти меч", "4,2": "x"}
    assert t["headers"] == {"0,1": "Задание", "1,0": "1"}
    assert t["locked_rows"] == [1] and t["locked_cols"] == [2] and t["locked_cells"] == ["1,1"]
    assert t["base_anchor"] == 107


def test_table_overlays():
    assert _data()["table_overlays"] == {"tpl_1": {"base": {"1,1": "глоб"}, "base_anchor": 107,
                                                   "rows": 2, "cols": 2}}


def test_rpg_config():
    cfg = _data()["rpg_config"]
    assert cfg["reputation"] == [{"name": "Гильдия", "min": -50, "max": 50, "default": 0, "sub": ["Торговцы"]}]
    assert cfg["currencies"] == [{"name": "Золото", "rate": 100.0, "emoji": "💰"}]
    assert cfg["equipment"] == {"locked": True, "chars": {"Вольф": {
        "slots": [{"name": "Голова", "max": 1}, {"name": "Кольцо", "max": 2}],
        "forms": [{"id": "human", "name": "Человек", "slots": [{"name": "Голова", "max": 1}]}],
        "form": "human"}}}


def test_rpg_config_falls_back_to_legacy_rpg_keys():
    chat0 = {"rpg": {"currencyConfig": {"denominations": [{"name": "Медь", "rate": 1}]}}}
    assert hi.convert_chat_meta(chat0, {})["rpg_config"] == {
        "currencies": [{"name": "Медь", "rate": 1.0, "emoji": ""}]}


def test_chat_meta_never_raises_on_garbage():
    empty = hi.convert_chat_meta(None, None)
    assert empty["ops"] == [] and empty["summaries"] == [] and empty["seq"] == 0
    junk = {"autoSummaries": "мусор", "locationMemory": [1, 2], "customTables": [None, 5, {"data": "x"}],
            "relationships": "x", "_rpgConfigs": [], "rpg": {"skills": "x", "reputation": {"А": 5}},
            "agenda": [None, 3, {"text": 7}], "_deletedNpcs": [None, 1], "events": None,
            "globalTableData": {"t": None}}
    data = hi.convert_chat_meta(junk, {"a": "b", 1: "x", "2": "102"})
    assert data["summaries"] == []
    assert [op["kind"] for op in data["ops"]] == ["agenda.add", "npc.delete"]   # числа — строкой
    assert data["tables"] == [{"id": "t_3", "name": "", "prompt": "", "rows": 2, "cols": 2, "locked_rows": [],
                               "locked_cols": [], "locked_cells": [], "headers": {}, "base": {},
                               "base_anchor": 102}]


def test_chat_meta_does_not_mutate_input():
    chat0 = _chat0()
    before = copy.deepcopy(chat0)
    hi.convert_chat_meta(chat0, INDEX_TO_MID)
    assert chat0 == before
