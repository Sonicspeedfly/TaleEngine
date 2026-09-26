"""
Пользовательские таблицы Horae (backend/horae_tables.py).

Проверяется весь путь без БД: разбор <horaetable:…> из ответа, склейка
шаблонов с оверлеями чата, повтор вкладов ИИ поверх base (якорь, заголовки,
блокировки, рост), правки пользователя (ячейка, очистка, структура,
блокировки), рендер для промпта и импорт файла плагина.
"""
import pytest

from backend import horae_tables as ht


def _local(name="Квесты", rows=3, cols=3, base=None, anchor=0, **kw):
    t = ht.new_table(name, rows, cols, kw.pop("prompt", ""))
    t["base"] = dict(base or {})
    t["base_anchor"] = anchor
    t.update(kw)
    return t


# ==================== Разбор ====================

class TestParse:
    def test_basic_and_variants(self):
        text = ("Сюжет.\n<horaetable:Квесты>\n1,1:Найти меч | 1,2:активен\n"
                "2-1：Спасти кота｜2,2: ждёт \n</horaetable>")
        assert ht.parse_blocks(text) == [{"name": "Квесты", "cells": {
            "1,1": "Найти меч", "1,2": "активен", "2,1": "Спасти кота", "2,2": "ждёт"}}]

    def test_fullwidth_colon_in_tag_and_named_close(self):
        text = "<horaetable：Связи >\n1,1:Вольф\n</horaetable:Связи>"
        assert ht.parse_blocks(text) == [{"name": "Связи", "cells": {"1,1": "Вольф"}}]

    @pytest.mark.parametrize("value", ["(пусто)", "пусто", "Пусто", "空", "(空)", "（空）", "-", "—", "---", ""])
    def test_placeholders_skipped(self, value):
        text = f"<horaetable:Т>\n1,1:{value}\n1,2:ok\n</horaetable>"
        assert ht.parse_blocks(text) == [{"name": "Т", "cells": {"1,2": "ok"}}]

    def test_several_blocks_and_empty_dropped(self):
        text = ("<horaetable:А>\n1,1:x\n</horaetable>\n<horaetable:Б>\n1,1:-\nмусор\n</horaetable>"
                "<horaetable:В>\n0,1:Заголовок\n</horaetable>")
        assert [b["name"] for b in ht.parse_blocks(text)] == ["А", "В"]

    def test_later_value_wins(self):
        text = "<horaetable:А>\n1,1:старое\n1,1:новое\n</horaetable>"
        assert ht.parse_blocks(text)[0]["cells"] == {"1,1": "новое"}

    def test_strip_blocks(self):
        text = "До<horaetable:А>\n1,1:x\n</horaetable>После<horaetable：Б>1,1:y</horaetable:Б>!"
        assert ht.strip_blocks(text) == "ДоПосле!"
        assert ht.parse_blocks("") == [] and ht.strip_blocks(None) == ""


# ==================== Таблицы и шаблоны ====================

class TestTablesAndTemplates:
    def test_new_table(self):
        t = ht.new_table("Квесты", 1, 5, "Промпт", "character")
        assert t["id"].startswith("t_") and len(t["id"]) == 10
        assert (t["rows"], t["cols"]) == (2, 5)
        assert t["scope"] == "character" and t["prompt"] == "Промпт"
        assert t["locked_rows"] == [] and t["locked_cols"] == [] and t["locked_cells"] == []
        assert t["base"] == {} and t["base_anchor"] == 0
        assert ht.new_table("X")["id"] != ht.new_table("X")["id"]
        assert ht.new_table("X", scope="weird")["scope"] == "local"

    def test_make_template_headers_only(self):
        t = _local(base={"0,1": "Задача", "1,0": "Первая", "1,1": "данные"},
                   locked_rows=[1], locked_cells=["1-1"], scope="global")
        tpl = ht.make_template(t)
        assert tpl["headers"] == {"0,1": "Задача", "1,0": "Первая"}
        assert "base" not in tpl
        assert tpl["scope"] == "global" and tpl["id"] == t["id"]
        assert tpl["locked_rows"] == [1] and tpl["locked_cells"] == ["1,1"]
        assert (tpl["rows"], tpl["cols"]) == (3, 3)
        # Текущие данные (data) свежее base.
        assert ht.make_template({**t, "data": {"0,1": "Цель"}})["headers"]["0,1"] == "Цель"

    def test_resolve_order_and_overlays(self):
        g = {"id": "g1", "name": "Глобальная", "rows": 3, "cols": 2, "prompt": "p",
             "locked_cols": [1], "headers": {"0,1": "Кол"}}
        g_noname = {"id": "g2", "name": "  ", "rows": 2, "cols": 2}
        c = {"id": "c1", "name": "Персонажа", "rows": 2, "cols": 2, "headers": {}}
        loc = _local("Локальная", base={"1,1": "x"}, anchor=4)
        overlays = {"g1": {"base": {"0,1": "устарело", "1,1": "данные"}, "base_anchor": 7,
                           "rows": 5, "cols": 2}}
        tables = ht.resolve([g, g_noname], [c], [loc, _local("")], overlays)
        assert [(t["name"], t["scope"]) for t in tables] == [
            ("Глобальная", "global"), ("Персонажа", "character"), ("Локальная", "local")]
        gt = tables[0]
        assert gt["id"] == "g1" and gt["prompt"] == "p" and gt["locked_cols"] == [1]
        assert gt["base"] == {"0,1": "Кол", "1,1": "данные"}  # заголовок шаблона главнее
        assert (gt["base_anchor"], gt["rows"], gt["cols"]) == (7, 5, 2)
        ct = tables[1]
        assert ct["base"] == {} and ct["base_anchor"] == 0
        lt = tables[2]
        assert lt["id"] == loc["id"] and lt["base"] == {"1,1": "x"} and lt["base_anchor"] == 4
        # resolve не делит изменяемые объекты с входом
        lt["base"]["1,1"] = "изменено"
        assert loc["base"]["1,1"] == "x"

    def test_resolve_rows_max_of_template_and_overlay(self):
        g = {"id": "g1", "name": "Т", "rows": 6, "cols": 4, "headers": {}}
        tables = ht.resolve([g], [], [], {"g1": {"base": {}, "rows": 3, "cols": 5}})
        assert (tables[0]["rows"], tables[0]["cols"]) == (6, 5)


# ==================== Повтор ====================

class TestReplay:
    def test_base_plus_contributions_after_anchor(self):
        t = _local(base={"0,1": "Задача", "1,1": "из base"}, anchor=5)
        res = ht.replay([t], [
            (4, [{"name": "Квесты", "cells": {"1,1": "старый вклад", "2,1": "старый"}}]),
            (5, [{"name": "Квесты", "cells": {"2,1": "тоже старый"}}]),
            (6, [{"name": "квесты ", "cells": {"2,1": "новый"}}]),
        ])
        assert res[t["id"]] == {"data": {"0,1": "Задача", "1,1": "из base", "2,1": "новый"},
                                "rows": 3, "cols": 3}

    def test_headers_only_if_empty(self):
        t = _local(base={"0,1": "Задача"})
        res = ht.replay([t], [(1, [{"name": "Квесты", "cells": {"0,1": "Цель", "0,2": "Статус",
                                                                "1,0": "Первый"}}])])
        assert res[t["id"]]["data"] == {"0,1": "Задача", "0,2": "Статус", "1,0": "Первый"}

    def test_locks(self):
        t = _local(locked_rows=[1], locked_cols=[2], locked_cells=["2,1"])
        res = ht.replay([t], [(1, [{"name": "Квесты", "cells": {
            "1,1": "нет", "2,2": "нет", "2,1": "нет", "3,1": "да"}}])])
        assert res[t["id"]]["data"] == {"3,1": "да"}

    def test_grows(self):
        t = _local(rows=2, cols=2)
        res = ht.replay([t], [(1, [{"name": "Квесты", "cells": {"4,6": "далеко"}}])])
        assert (res[t["id"]]["rows"], res[t["id"]]["cols"]) == (5, 7)

    def test_local_wins_over_character_and_global(self):
        g = {"id": "g1", "name": "Квесты", "rows": 3, "cols": 3, "headers": {}}
        c = {"id": "c1", "name": "КВЕСТЫ", "rows": 3, "cols": 3, "headers": {}}
        loc = _local("квесты")
        tables = ht.resolve([g], [c], [loc], {})
        res = ht.replay(tables, [(1, [{"name": "Квесты", "cells": {"1,1": "x"}}])])
        assert res[loc["id"]]["data"] == {"1,1": "x"}
        assert res["g1"]["data"] == {} and res["c1"]["data"] == {}
        tables = ht.resolve([g], [c], [], {})
        res = ht.replay(tables, [(1, [{"name": "Квесты", "cells": {"1,1": "x"}}])])
        assert res["c1"]["data"] == {"1,1": "x"} and res["g1"]["data"] == {}

    def test_unknown_table_and_plugin_keys(self):
        t = _local()
        res = ht.replay([t], [(1, [{"name": "Нет такой", "cells": {"1,1": "x"}},
                                   {"name": "Квесты", "cells": {"1-2": "дефис"}}])])
        assert res[t["id"]]["data"] == {"1,2": "дефис"}

    def test_no_contributions(self):
        t = _local(base={"1,1": "x"})
        assert ht.replay([t], []) == {t["id"]: {"data": {"1,1": "x"}, "rows": 3, "cols": 3}}


# ==================== Правки пользователя ====================

class TestEdits:
    def test_set_cell(self):
        t = _local(rows=2, cols=2, base={"1,1": "a"})
        data = {"1,1": "a", "1,2": "из ИИ"}
        new = ht.set_cell(t, data, 3, 1, "новое", anchor=9)
        assert new["base"] == {"1,1": "a", "1,2": "из ИИ", "3,1": "новое"}
        assert (new["base_anchor"], new["rows"], new["cols"]) == (9, 4, 3)
        assert t["base"] == {"1,1": "a"} and t["rows"] == 2  # вход не меняется
        cleared = ht.set_cell(new, new["base"], 1, 2, "  ", anchor=10)
        assert "1,2" not in cleared["base"]
        with pytest.raises(ValueError):
            ht.set_cell(t, data, -1, 0, "x", anchor=1)

    def test_set_cell_then_replay_ignores_old_contributions(self):
        t = _local()
        contribs = [(3, [{"name": "Квесты", "cells": {"1,1": "ИИ-3"}}])]
        data = ht.replay([t], contribs)[t["id"]]["data"]
        t2 = ht.set_cell(t, data, 1, 2, "человек", anchor=3)
        contribs.append((4, [{"name": "Квесты", "cells": {"2,1": "ИИ-4"}}]))
        assert ht.replay([t2], contribs)[t2["id"]]["data"] == {
            "1,1": "ИИ-3", "1,2": "человек", "2,1": "ИИ-4"}

    def test_clear_data_keeps_headers(self):
        t = _local()
        new = ht.clear_data(t, {"0,1": "Задача", "2,0": "Вторая", "1,1": "x"}, anchor=4)
        assert new["base"] == {"0,1": "Задача", "2,0": "Вторая"} and new["base_anchor"] == 4

    def test_add_rows(self):
        t = _local(rows=3, cols=2, locked_rows=[2], locked_cells=["2,1"])
        data = {"0,1": "H", "1,1": "a", "2,1": "b"}
        above = ht.structure(t, data, "add_row_above", 2, anchor=1)
        assert above["base"] == {"0,1": "H", "1,1": "a", "3,1": "b"}
        assert above["rows"] == 4 and above["locked_rows"] == [3] and above["locked_cells"] == ["3,1"]
        below = ht.structure(t, data, "add_row_below", 2, anchor=1)
        assert below["base"] == data and below["rows"] == 4 and below["locked_rows"] == [2]
        top = ht.structure(t, data, "add_row_above", 0, anchor=1)  # выше заголовка нельзя
        assert top["base"] == {"0,1": "H", "2,1": "a", "3,1": "b"}

    def test_add_cols(self):
        t = _local(rows=2, cols=3, locked_cols=[1])
        data = {"0,1": "A", "0,2": "B", "1,1": "a", "1,2": "b"}
        left = ht.structure(t, data, "add_col_left", 1, anchor=2)
        assert left["base"] == {"0,2": "A", "0,3": "B", "1,2": "a", "1,3": "b"}
        assert left["cols"] == 4 and left["locked_cols"] == [2] and left["base_anchor"] == 2
        right = ht.structure(t, data, "add_col_right", 2, anchor=2)
        assert right["base"] == data and right["cols"] == 4

    def test_delete_row_and_col(self):
        t = _local(rows=4, cols=3, locked_rows=[2, 3], locked_cells=["3,2", "1,1"], locked_cols=[2])
        data = {"0,1": "A", "0,2": "B", "1,1": "a1", "2,1": "a2", "3,1": "a3", "3,2": "b3"}
        dr = ht.structure(t, data, "delete_row", 2, anchor=5)
        assert dr["base"] == {"0,1": "A", "0,2": "B", "1,1": "a1", "2,1": "a3", "2,2": "b3"}
        assert dr["rows"] == 3 and dr["locked_rows"] == [2] and dr["locked_cells"] == ["1,1", "2,2"]
        dc = ht.structure(t, data, "delete_col", 1, anchor=5)
        assert dc["base"] == {"0,1": "B", "3,1": "b3"}
        assert dc["cols"] == 2 and dc["locked_cols"] == [1] and dc["locked_cells"] == ["3,1"]

    def test_structure_errors(self):
        t = _local(rows=2, cols=2)
        with pytest.raises(ValueError):
            ht.structure(t, {}, "delete_row", 1, anchor=0)  # меньше 2×2 нельзя
        big = _local(rows=4, cols=4)
        with pytest.raises(ValueError):
            ht.structure(big, {}, "delete_col", 0, anchor=0)  # заголовок
        with pytest.raises(ValueError):
            ht.structure(big, {}, "delete_row", 4, anchor=0)
        with pytest.raises(ValueError):
            ht.structure(big, {}, "explode", 1, anchor=0)

    def test_set_lock(self):
        t = _local()
        t = ht.set_lock(t, "row", 2, 0, True)
        t = ht.set_lock(t, "col", 0, 1, True)
        t = ht.set_lock(t, "cell", 1, 2, True)
        assert (t["locked_rows"], t["locked_cols"], t["locked_cells"]) == ([2], [1], ["1,2"])
        t2 = ht.set_lock(t, "cell", 1, 2, False)
        assert t2["locked_cells"] == [] and t["locked_cells"] == ["1,2"]
        with pytest.raises(ValueError):
            ht.set_lock(t, "table", 0, 0, True)

    def test_split_effective(self):
        g = {"id": "g1", "name": "Т", "rows": 3, "cols": 3, "prompt": "p", "headers": {"0,1": "A"}}
        eff = ht.resolve([g], [], [], {"g1": {"base": {"1,1": "x"}, "base_anchor": 2, "rows": 3, "cols": 3}})[0]
        eff = ht.set_cell(eff, eff["base"], 0, 2, "B", anchor=6)
        tpl, overlay = ht.split_effective(eff)
        assert tpl["id"] == "g1" and tpl["scope"] == "global" and tpl["prompt"] == "p"
        assert tpl["headers"] == {"0,1": "A", "0,2": "B"}
        assert overlay == {"base": {"0,1": "A", "0,2": "B", "1,1": "x"}, "base_anchor": 6,
                           "rows": 3, "cols": 3}
        loc = _local()
        none, same = ht.split_effective(loc)
        assert none is None and same == loc and same is not loc


# ==================== Рендер ====================

class TestRender:
    def test_full_format(self):
        t = _local("Квесты", rows=5, cols=4, prompt=" Отмечайте статус ",
                   locked_cols=[2], locked_rows=[1], locked_cells=["2,1"])
        data = {"0,1": "Задача", "0,2": "Статус", "1,1": "Меч", "1,2": "активен", "2,1": "Кот"}
        text = ht.render_block([t], {t["id"]: {"data": data, "rows": 5, "cols": 4}})
        assert text == (
            "\n[Квесты](4строк×3столбцов)\n"
            "(Инструкции: Отмечайте статус)\n"
            "[0,0]Заголовок | [0,1]Задача | [0,2]Статус🔒 | [0,3]Столбец3\n"
            "[1,0]1🔒 | [1,1]Меч | [1,2]активен | [1,3]\n"
            "[2,0]2 | [2,1]Кот🔒 | [2,2] | [2,3]\n"
            "(всего 4 строк, строки 3-4 пусты)\n"
            "(Столбец3: нет данных, заполните, если в сюжете есть соответствующая информация)"
        )

    def test_skip_empty_without_prompt(self):
        empty = _local("Пустая")
        prompted = _local("С промптом", rows=2, cols=2, prompt="Заполнять")
        text = ht.render_block([empty, prompted], {})
        assert "Пустая" not in text
        assert text.startswith("\n[С промптом](1строк×1столбцов)\n(Инструкции: Заполнять)")
        assert "[1,0]1 | [1,1]" in text
        assert ht.render_block([empty], {}) == ""

    def test_headers_count_as_content_and_row_label(self):
        t = _local(rows=3, cols=2)
        text = ht.render_block([t], {t["id"]: {"data": {"2,0": "Второй"}, "rows": 3, "cols": 2}})
        assert "[1,0]1 | [1,1]\n[2,0]Второй | [2,1]" in text
        assert "строки" not in text.split("\n")[-2]  # последняя строка с данными — 2

    def test_rules_suffix(self):
        empty = _local("Пустая", rows=4, cols=4)
        full = _local("Полная", rows=3, cols=2, base={"1,1": "x"})
        res = ht.replay([empty, full], [(1, [{"name": "Полная", "cells": {"3,3": "y"}}])])
        suffix = ht.rules_suffix([empty, full], res)
        assert suffix == (
            "\n★ Таблица «Полная» размер: 3 строк × 3 столбцов (данные: строки 1-3, столбцы 1-3)"
            "\nПример (заполните пустые ячейки или обновите изменённые):"
            "\n<horaetable:Полная>\n1,1:Содержимое A\n1,2:Содержимое B\n2,1:Содержимое C\n</horaetable>")
        # Ничего не выводится — берём первую таблицу, иначе модель о ней не узнает.
        assert "«Пустая» размер: 3 строк × 3 столбцов" in ht.rules_suffix([empty], {})
        assert ht.rules_suffix([], {}) == ""


# ==================== Импорт / экспорт ====================

class TestImportExport:
    def test_roundtrip(self):
        t = _local("Связи", rows=3, cols=3, prompt="кто кому кто", locked_rows=[1], locked_cells=["2,2"])
        data = {"0,1": "Кто", "1,1": "Вольф", "5,1": "за краем"}
        exported = ht.to_export(t, data)
        assert exported["data"] == data and (exported["rows"], exported["cols"]) == (6, 3)
        imported = ht.from_import(exported)
        assert imported["id"] != t["id"] and imported["id"].startswith("t_")
        assert imported["scope"] == "local" and imported["name"] == "Связи"
        assert imported["base"] == data and imported["base_anchor"] == 0
        assert (imported["rows"], imported["cols"]) == (6, 3)
        assert imported["locked_rows"] == [1] and imported["locked_cells"] == ["2,2"]
        assert imported["prompt"] == "кто кому кто"

    def test_plugin_format(self):
        plugin = {"name": "Квесты", "rows": 3, "cols": 3, "prompt": "p",
                  "data": {"0-1": "Задача", "1-1": "Меч", "2-2": "  "},
                  "lockedRows": [1], "lockedCols": ["2"], "lockedCells": ["1-2"],
                  "baseData": {"0-1": "Задача"}, "baseRows": 3, "baseCols": 3}
        t = ht.from_import(plugin)
        assert t["base"] == {"0,1": "Задача", "1,1": "Меч"}
        assert (t["locked_rows"], t["locked_cols"], t["locked_cells"]) == ([1], [2], ["1,2"])
        assert (t["rows"], t["cols"]) == (3, 3)

    def test_wrapped_and_invalid(self):
        assert ht.from_import({"table": {"name": "", "data": {}}})["name"] == "Импортированная таблица"
        with pytest.raises(ValueError):
            ht.from_import([1, 2])
