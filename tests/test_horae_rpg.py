"""
RPG-слой Horae (backend/horae_rpg.py): разбор <horaerpg>, повтор, правки, рендер, правила.

Модуль чистый — ни БД, ни сети. Отдельно проверяются исправленные ошибки
плагина: `xp:` больше не шкала, русские «нормально/нет» очищают статусы,
подпись шкалы не теряется, снаряжение соблюдает слоты и возвращает предметы.
"""
import pytest

from backend import horae_rpg as rpg

USER = "Алекс"


def _ctx(config=None, *, inventory=None, returned=None, user_only=frozenset(), mid=1,
         aliases=None):
    """Контекст с подменным инвентарём: take_item забирает, give_item кладёт обратно."""
    inv = inventory if inventory is not None else {}
    back = returned if returned is not None else {}
    amap = aliases or {}
    return rpg.ApplyContext(
        resolve_owner=lambda n: amap.get(n, USER if n == "{{user}}" else n),
        config=config or {},
        user_name=USER,
        user_only=frozenset(user_only),
        take_item=lambda name: inv.pop(name, None),
        give_item=lambda name, info: back.__setitem__(name, info),
        mid=mid,
    )


def _apply(body, state=None, ctx=None, **parse_kw):
    st = state if state is not None else rpg.empty_state()
    ch = rpg.parse_block(body, user_name=USER, **parse_kw)
    rpg.apply_changes(st, ch, ctx or _ctx())
    return st


# ==================== Разбор строк ====================

class TestParse:
    def test_bar_with_label_and_fullwidth_colon(self):
        ch = rpg.parse_block("hp:Вольф=80/100(Здоровье)\nMP：Вольф=20 / 50", user_name=USER)
        assert ch["bars"] == {"Вольф": {"hp": [80, 100, "Здоровье"], "mp": [20, 50]}}

    def test_xp_is_not_a_bar(self):
        ch = rpg.parse_block("xp:Вольф=50/100\nlevel:Вольф=3", user_name=USER)
        assert ch["bars"] == {}
        assert ch["xp"] == {"Вольф": [50, 100]}
        assert ch["levels"] == {"Вольф": 3}

    @pytest.mark.parametrize("key", ["level", "status", "skill", "rep", "attr", "currency", "base"])
    def test_known_prefixes_never_bars(self, key):
        ch = rpg.parse_block(f"{key}:Вольф=10/20", user_name=USER) or rpg.empty_changes()
        assert ch["bars"] == {}

    def test_keys_case_insensitive(self):
        ch = rpg.parse_block("STATUS:Вольф=Отравлен\nXP:Вольф=1/2\nSkill:Вольф|Рывок", user_name=USER)
        assert ch["status"] == {"Вольф": ["Отравлен"]}
        assert ch["xp"] == {"Вольф": [1, 2]}
        assert ch["skills"][0]["name"] == "Рывок"

    def test_status_effects_split(self):
        ch = rpg.parse_block("status:Вольф=Отравлен / Кровотечение/Отравлен", user_name=USER)
        assert ch["status"] == {"Вольф": ["Отравлен", "Кровотечение"]}

    @pytest.mark.parametrize("word", ["нормально", "Норма", "нет", "без отклонений", "正常", "无",
                                      "none", "Normal", "clear", "нормально."])
    def test_status_clear_words(self, word):
        ch = rpg.parse_block(f"status:Вольф={word}", user_name=USER)
        assert ch["status"] == {"Вольф": []}

    def test_status_empty_value_clears(self):
        assert rpg.parse_block("status:Вольф=", user_name=USER)["status"] == {"Вольф": []}

    def test_skills(self):
        ch = rpg.parse_block("skill:Вольф|Удар щитом|2|Оглушает\nskill-:Вольф|Бег", user_name=USER)
        assert ch["skills"] == [{"owner": "Вольф", "name": "Удар щитом", "level": "2", "desc": "Оглушает"}]
        assert ch["skills_removed"] == [{"owner": "Вольф", "name": "Бег"}]

    def test_skill_needs_owner_and_name(self):
        assert rpg.parse_block("skill:Вольф", user_name=USER) is None

    def test_equip_and_unequip(self):
        ch = rpg.parse_block("equip:Вольф|Голова|Шлем|def=2, atk=-1\nunequip:Вольф|Руки|Перчатки",
                             user_name=USER)
        assert ch["equip"] == [{"owner": "Вольф", "slot": "Голова", "name": "Шлем",
                                "attrs": {"def": 2, "atk": -1}}]
        assert ch["unequip"] == [{"owner": "Вольф", "slot": "Руки", "name": "Перчатки"}]

    def test_rep_level_currency_attr(self):
        ch = rpg.parse_block(
            "rep:Вольф|Гильдия=-20\n"
            "currency:Вольф|Золото=+10|Серебро=-3|Медь=50\n"
            "attr:Вольф|STR=60|dex=40|мусор\n",
            user_name=USER)
        assert ch["reputation"] == {"Вольф": {"Гильдия": -20}}
        assert ch["currency"] == [
            {"owner": "Вольф", "name": "Золото", "value": 10, "delta": True},
            {"owner": "Вольф", "name": "Серебро", "value": -3, "delta": True},
            {"owner": "Вольф", "name": "Медь", "value": 50, "delta": False},
        ]
        assert ch["attrs"] == {"Вольф": {"str": 60, "dex": 40}}

    def test_base_forms(self):
        ch = rpg.parse_block(
            "base:Поместье>Кузница>Печь=2\n"
            "base:Поместье|desc=Каменное, со стенами\n"
            "base:Поместье|level=3\n"
            "base:Башня=древняя\n",
            user_name=USER)
        assert ch["base"] == [
            {"path": "Поместье>Кузница>Печь", "field": "level", "value": 2},
            {"path": "Поместье", "field": "desc", "value": "Каменное, со стенами"},
            {"path": "Поместье", "field": "level", "value": 3},
            {"path": "Башня", "field": "desc", "value": "древняя"},
        ]

    def test_nothing_parsed_returns_none(self):
        assert rpg.parse_block("", user_name=USER) is None
        assert rpg.parse_block("просто текст\nhp:без значения", user_name=USER) is None

    def test_has_changes(self):
        assert not rpg.has_changes(rpg.empty_changes())
        assert not rpg.has_changes(None)
        assert rpg.has_changes({"status": {"A": []}})


class TestParseUserOnly:
    def test_bars_and_status_without_owner(self):
        ch = rpg.parse_block("hp:80/100(HP)\nstatus:нормально", user_name=USER, user_only={"bars"})
        assert ch["bars"] == {USER: {"hp": [80, 100, "HP"]}}
        assert ch["status"] == {USER: []}

    def test_bars_owner_form_drops_owner(self):
        ch = rpg.parse_block("hp:Вольф=10/20", user_name=USER, user_only={"bars"})
        assert ch["bars"] == {USER: {"hp": [10, 20]}}

    def test_ownerless_bar_ignored_without_user_only(self):
        assert rpg.parse_block("hp:80/100", user_name=USER) is None

    def test_other_modules(self):
        ch = rpg.parse_block(
            "skill:Рывок|1|Быстрый\nskill-:Бег\nequip:Голова|Шлем|def=1\nunequip:Руки|Перчатки\n"
            "rep:Гильдия=5\nlevel:4\nxp:10/400\ncurrency:Золото=+5\nattr:str=50",
            user_name=USER,
            user_only={"skills", "equipment", "reputation", "level", "currency", "attrs"})
        assert ch["skills"] == [{"owner": USER, "name": "Рывок", "level": "1", "desc": "Быстрый"}]
        assert ch["skills_removed"] == [{"owner": USER, "name": "Бег"}]
        assert ch["equip"] == [{"owner": USER, "slot": "Голова", "name": "Шлем", "attrs": {"def": 1}}]
        assert ch["unequip"] == [{"owner": USER, "slot": "Руки", "name": "Перчатки"}]
        assert ch["reputation"] == {USER: {"Гильдия": 5}}
        assert ch["levels"] == {USER: 4}
        assert ch["xp"] == {USER: [10, 400]}
        assert ch["currency"] == [{"owner": USER, "name": "Золото", "value": 5, "delta": True}]
        assert ch["attrs"] == {USER: {"str": 50}}

    def test_user_only_tolerates_written_owner(self):
        ch = rpg.parse_block(f"equip:{USER}|Голова|Шлем\ncurrency:{USER}|Золото=7\nlevel:{USER}=2",
                             user_name=USER, user_only={"equipment", "currency", "level"})
        assert ch["equip"][0]["slot"] == "Голова" and ch["equip"][0]["name"] == "Шлем"
        assert ch["currency"] == [{"owner": USER, "name": "Золото", "value": 7, "delta": False}]
        assert ch["levels"] == {USER: 2}

    def test_empty_user_name_uses_placeholder(self):
        ch = rpg.parse_block("level:3", user_name="", user_only={"level"})
        assert ch["levels"] == {"{{user}}": 3}


# ==================== Применение изменений ====================

class TestApply:
    def test_bars_merge_and_label_kept(self):
        st = _apply("hp:Вольф=80/100(Здоровье)\nmp:Вольф=10/20")
        st = _apply("hp:Вольф=60/100", st)
        assert st["bars"]["Вольф"] == {"hp": {"cur": 60, "max": 100, "label": "Здоровье"},
                                       "mp": {"cur": 10, "max": 20, "label": ""}}

    def test_status_replaced(self):
        st = _apply("status:Вольф=Отравлен/Кровотечение")
        st = _apply("status:Вольф=нормально", st)
        assert st["status"]["Вольф"] == []

    def test_owner_resolved(self):
        ctx = _ctx(aliases={"N001 Вольф": "Вольф", "{{user}}": USER})
        st = _apply("hp:N001 Вольф=5/10\nlevel:{{user}}=2", ctx=ctx)
        assert "Вольф" in st["bars"] and st["levels"] == {USER: 2}

    def test_skills_upsert_keeps_level(self):
        st = _apply("skill:Вольф|Рывок|2|Быстрый бросок")
        st = _apply("skill:вольф|Рывок", st, ctx=_ctx(aliases={"вольф": "Вольф"}))
        st = _apply("skill:Вольф|рывок|3", st)
        assert st["skills"]["Вольф"] == [{"name": "Рывок", "level": "3", "desc": "Быстрый бросок",
                                         "user": False}]

    def test_skills_removed(self):
        st = _apply("skill:Вольф|Рывок|1\nskill:Вольф|Бег|1")
        st = _apply("skill-:Вольф|РЫВОК", st)
        assert [s["name"] for s in st["skills"]["Вольф"]] == ["Бег"]
        st = _apply("skill-:Вольф|Бег", st)
        assert "Вольф" not in st["skills"]

    def test_deleted_skills_from_config_blocked(self):
        st = _apply("skill:Вольф|Рывок|1\nskill:Вольф|Бег|1",
                    ctx=_ctx({"deleted_skills": [["Вольф", "рывок"]]}))
        assert [s["name"] for s in st["skills"]["Вольф"]] == ["Бег"]

    def test_attrs_merge(self):
        st = _apply("attr:Вольф|str=60")
        st = _apply("attr:Вольф|dex=40|str=65", st)
        assert st["attrs"]["Вольф"] == {"str": 65, "dex": 40}

    def test_levels_and_xp_overwrite(self):
        st = _apply("level:Вольф=2\nxp:Вольф=10/200")
        st = _apply("level:Вольф=3\nxp:Вольф=0/300", st)
        assert st["levels"] == {"Вольф": 3} and st["xp"] == {"Вольф": [0, 300]}

    def test_user_only_drops_npc_entries(self):
        ch = {"bars": {"Вольф": {"hp": [1, 2]}, USER: {"hp": [3, 4]}},
              "levels": {"Вольф": 5, USER: 6},
              "currency": [{"owner": "Вольф", "name": "Золото", "value": 1, "delta": False}]}
        st = rpg.empty_state()
        rpg.apply_changes(st, ch, _ctx(user_only={"bars", "level", "currency"}))
        assert list(st["bars"]) == [USER]
        assert st["levels"] == {USER: 6}
        assert st["currency"] == {}

    def test_plugin_format_changes(self):
        ch = {"removedSkills": [], "attributes": {"Вольф": {"str": 5}},
              "equipment": [{"owner": "Вольф", "slot": "Голова", "name": "Шлем", "attrs": {}}],
              "currency": [{"owner": "Вольф", "name": "Золото", "value": 3, "isDelta": True}],
              "baseChanges": [{"path": "Дом", "field": "level", "value": 1}]}
        st = rpg.empty_state()
        rpg.apply_changes(st, ch, _ctx())
        rpg.apply_changes(st, ch, _ctx())
        assert st["attrs"] == {"Вольф": {"str": 5}}
        assert st["equipment"]["Вольф"]["Голова"][0]["name"] == "Шлем"
        assert st["currency"] == {"Вольф": {"Золото": 6}}
        assert st["strongholds"][0]["level"] == 1

    def test_empty_or_none_changes_noop(self):
        st = rpg.empty_state()
        rpg.apply_changes(st, None, _ctx())
        rpg.apply_changes(st, {}, _ctx())
        assert st == rpg.empty_state()


class TestEquipment:
    def test_equip_takes_item_from_inventory(self):
        inv = {"Шлем": {"icon": "⛑", "description": "Стальной", "importance": "!"}}
        st = _apply("equip:Вольф|Голова|Шлем|def=2", ctx=_ctx(inventory=inv))
        assert inv == {}
        assert st["equipment"]["Вольф"]["Голова"] == [
            {"name": "Шлем", "attrs": {"def": 2},
             "item": {"icon": "⛑", "description": "Стальной", "importance": "!"}}]

    def test_equip_unknown_item_stores_none(self):
        st = _apply("equip:Вольф|Голова|Шлем")
        assert st["equipment"]["Вольф"]["Голова"][0]["item"] is None

    def test_reequip_updates_attrs_only(self):
        inv = {"Шлем": {"icon": "⛑"}}
        back = {}
        ctx = _ctx(inventory=inv, returned=back)
        st = _apply("equip:Вольф|Голова|Шлем|def=1", ctx=ctx)
        st = _apply("equip:Вольф|Голова|Шлем|def=3", st, ctx=ctx)
        assert st["equipment"]["Вольф"]["Голова"] == [
            {"name": "Шлем", "attrs": {"def": 3}, "item": {"icon": "⛑"}}]
        assert back == {}

    def test_slot_overflow_returns_oldest(self):
        back = {}
        cfg = {"equipment": {"chars": {"Вольф": {"slots": [{"name": "Кольцо", "max": 2}]}}}}
        inv = {"Кольцо силы": {"icon": "💍", "description": "Сила"}, "Кольцо льда": {}, "Кольцо огня": {}}
        ctx = _ctx(cfg, inventory=inv, returned=back)
        st = _apply("equip:Вольф|Кольцо|Кольцо силы\nequip:Вольф|Кольцо|Кольцо льда", ctx=ctx)
        st = _apply("equip:Вольф|Кольцо|Кольцо огня", st, ctx=ctx)
        assert [e["name"] for e in st["equipment"]["Вольф"]["Кольцо"]] == ["Кольцо льда", "Кольцо огня"]
        assert back == {"Кольцо силы": {"icon": "💍", "description": "Сила",
                                        "holder": "Вольф", "location": ""}}

    def test_default_max_is_one(self):
        back = {}
        st = _apply("equip:Вольф|Голова|Шлем\nequip:Вольф|Голова|Корона", ctx=_ctx(returned=back))
        assert [e["name"] for e in st["equipment"]["Вольф"]["Голова"]] == ["Корона"]
        assert back["Шлем"]["holder"] == "Вольф" and back["Шлем"]["icon"] == "📦"

    def test_listed_slots_only_and_case_normalized(self):
        cfg = {"equipment": {"chars": {"Вольф": {"slots": [{"name": "Голова", "max": 1}]}}}}
        st = _apply("equip:Вольф|голова|Шлем\nequip:Вольф|Хвост|Бант", ctx=_ctx(cfg))
        assert st["equipment"] == {"Вольф": {"Голова": [{"name": "Шлем", "attrs": {}, "item": None}]}}

    def test_locked_rejects_unconfigured_owner(self):
        cfg = {"equipment": {"locked": True, "chars": {}}}
        st = _apply("equip:Вольф|Голова|Шлем", ctx=_ctx(cfg))
        assert st["equipment"] == {}

    def test_unlocked_without_config_accepts_any_slot(self):
        st = _apply("equip:Вольф|Хвост|Бант", ctx=_ctx({"equipment": {"locked": False}}))
        assert "Хвост" in st["equipment"]["Вольф"]

    def test_form_slots_used_when_no_slots_key(self):
        cfg = {"equipment": {"chars": {"Лиса": {
            "forms": [{"id": "human", "name": "Человеческая", "slots": [{"name": "Голова", "max": 1}]},
                      {"id": "fox", "name": "Лисья", "slots": [{"name": "Ошейник", "max": 1}]}],
            "form": "fox"}}}}
        st = _apply("equip:Лиса|Голова|Шлем\nequip:Лиса|Ошейник|Бубенец", ctx=_ctx(cfg))
        assert list(st["equipment"]["Лиса"]) == ["Ошейник"]

    def test_unequip_returns_item_first(self):
        inv = {"Шлем": {"icon": "⛑", "description": "Стальной"}}
        back = {}
        ctx = _ctx(inventory=inv, returned=back)
        st = _apply("equip:Вольф|Голова|Шлем", ctx=ctx)
        # В одном сообщении снятие применяется раньше экипировки.
        st = _apply("equip:Вольф|Голова|Корона\nunequip:Вольф|Голова|Шлем", st, ctx=ctx)
        assert back["Шлем"] == {"icon": "⛑", "description": "Стальной", "holder": "Вольф", "location": ""}
        assert [e["name"] for e in st["equipment"]["Вольф"]["Голова"]] == ["Корона"]

    def test_unequip_prunes_empty(self):
        st = _apply("equip:Вольф|Голова|Шлем")
        st = _apply("unequip:Вольф|Руки|шлем", st)  # слот перепутан, предмет по имени найден
        assert st["equipment"] == {}

    def test_move_between_slots_keeps_item(self):
        inv = {"Кинжал": {"icon": "🗡"}}
        ctx = _ctx(inventory=inv)
        st = _apply("equip:Вольф|Пояс|Кинжал", ctx=ctx)
        st = _apply("equip:Вольф|Руки|Кинжал", st, ctx=ctx)
        assert st["equipment"]["Вольф"] == {"Руки": [{"name": "Кинжал", "attrs": {}, "item": {"icon": "🗡"}}]}


class TestReputation:
    CFG = {"reputation": [{"name": "Гильдия", "min": -50, "max": 50}, {"name": "Город"}]}

    def test_clamp_to_category_range(self):
        st = _apply("rep:Вольф|Гильдия=80\nrep:Вольф|Город=-500", ctx=_ctx(self.CFG))
        assert st["reputation"]["Вольф"] == {"Гильдия": {"value": 50, "sub": {}},
                                             "Город": {"value": -100, "sub": {}}}

    def test_only_registered_when_configured(self):
        st = _apply("rep:Вольф|Слава=10\nrep:Вольф|гильдия=5", ctx=_ctx(self.CFG))
        assert st["reputation"]["Вольф"] == {"Гильдия": {"value": 5, "sub": {}}}

    def test_any_category_when_none_configured(self):
        st = _apply("rep:Вольф|Слава=500")
        assert st["reputation"]["Вольф"]["Слава"]["value"] == 100

    def test_user_edited_not_overwritten(self):
        ctx = _ctx(self.CFG)
        st = _apply("rep:Вольф|Гильдия=10", ctx=ctx)
        rpg.apply_op(st, {"kind": "rpg.rep", "owner": "Вольф", "cat": "Гильдия", "value": 99}, ctx)
        assert st["reputation"]["Вольф"]["Гильдия"] == {"value": 50, "sub": {}, "user": True}
        st = _apply("rep:Вольф|Гильдия=-20", st, ctx=ctx)
        assert st["reputation"]["Вольф"]["Гильдия"]["value"] == 50


class TestCurrency:
    def test_delta_and_absolute(self):
        st = _apply("currency:Вольф|Золото=+10")
        st = _apply("currency:Вольф|Золото=-3", st)
        assert st["currency"] == {"Вольф": {"Золото": 7}}
        st = _apply("currency:Вольф|Золото=100", st)
        assert st["currency"] == {"Вольф": {"Золото": 100}}

    def test_whitelist_when_configured(self):
        cfg = {"currencies": [{"name": "Золото", "rate": 100}, {"name": "Серебро", "rate": 1}]}
        st = _apply("currency:Вольф|золото=+5|Медь=+9|Серебро=3", ctx=_ctx(cfg))
        assert st["currency"] == {"Вольф": {"Золото": 5, "Серебро": 3}}

    def test_deleted_currency_at(self):
        cfg = {"deleted_currencies": [{"name": "Золото", "at": 5}, "Медь"]}
        st = _apply("currency:Вольф|Золото=+5|Медь=1", ctx=_ctx(cfg, mid=5))
        assert st["currency"] == {}
        st = _apply("currency:Вольф|Золото=+5|Медь=1", st, ctx=_ctx(cfg, mid=6))
        assert st["currency"] == {"Вольф": {"Золото": 5}}


class TestStrongholds:
    def test_tree_created_with_deterministic_ids(self):
        st = _apply("base:Поместье>Кузница>Печь=2\nbase:Поместье|desc=Каменное\nbase:Поместье>Сад")
        st = _apply("base:Поместье>Сад=1", st)
        assert st["strongholds"] == [
            {"id": "sh_1", "name": "Поместье", "level": None, "desc": "Каменное", "parent": None},
            {"id": "sh_2", "name": "Кузница", "level": None, "desc": "", "parent": "sh_1"},
            {"id": "sh_3", "name": "Печь", "level": 2, "desc": "", "parent": "sh_2"},
            {"id": "sh_4", "name": "Сад", "level": 1, "desc": "", "parent": "sh_1"},
        ]

    def test_same_name_under_different_parents(self):
        st = _apply("base:А>Склад=1\nbase:Б>Склад=2")
        sklad = [n for n in st["strongholds"] if n["name"] == "Склад"]
        assert len(sklad) == 2 and {n["level"] for n in sklad} == {1, 2}

    def test_user_delete_blocks_ai_and_edit_lifts(self):
        ctx = _ctx()
        st = _apply("base:Поместье>Кузница=1\nbase:Поместье>Кузница>Печь=2")
        rpg.apply_op(st, {"kind": "rpg.base.delete", "path": "Поместье>Кузница"}, ctx)
        assert [n["name"] for n in st["strongholds"]] == ["Поместье"]
        st = _apply("base:Поместье>Кузница>Печь=3", st)
        assert [n["name"] for n in st["strongholds"]] == ["Поместье"]
        rpg.apply_op(st, {"kind": "rpg.base", "path": "Поместье>Кузница", "level": 4}, ctx)
        assert st["strongholds"][-1]["name"] == "Кузница" and st["strongholds"][-1]["level"] == 4
        st = _apply("base:Поместье>Кузница|desc=Новая", st)
        assert st["strongholds"][-1]["desc"] == "Новая"


# ==================== Правки пользователя ====================

class TestOps:
    def test_bar_status_attr_level_xp_currency(self):
        st = rpg.empty_state()
        ctx = _ctx()
        rpg.apply_op(st, {"kind": "rpg.bar", "owner": "Вольф", "key": "HP", "cur": 5, "max": 10}, ctx)
        rpg.apply_op(st, {"kind": "rpg.bar", "owner": "Вольф", "key": "hp", "cur": 7}, ctx)
        rpg.apply_op(st, {"kind": "rpg.status", "owner": "Вольф", "effects": ["Яд", " "]}, ctx)
        rpg.apply_op(st, {"kind": "rpg.attr", "owner": "Вольф", "key": "STR", "value": "70"}, ctx)
        rpg.apply_op(st, {"kind": "rpg.level", "owner": "Вольф", "value": 4}, ctx)
        rpg.apply_op(st, {"kind": "rpg.xp", "owner": "Вольф", "cur": 10, "max": 400}, ctx)
        rpg.apply_op(st, {"kind": "rpg.currency", "owner": "Вольф", "name": "Золото", "value": 12}, ctx)
        assert st["bars"] == {"Вольф": {"hp": {"cur": 7, "max": 10, "label": ""}}}
        assert st["status"] == {"Вольф": ["Яд"]}
        assert st["attrs"] == {"Вольф": {"str": 70}}
        assert st["levels"] == {"Вольф": 4}
        assert st["xp"] == {"Вольф": [10, 400]}
        assert st["currency"] == {"Вольф": {"Золото": 12}}
        rpg.apply_op(st, {"kind": "rpg.currency", "owner": "Вольф", "name": "Золото", "value": None}, ctx)
        rpg.apply_op(st, {"kind": "rpg.bar", "owner": "Вольф", "key": "hp"}, ctx)
        assert st["currency"] == {} and st["bars"] == {}

    def test_skill_add_delete_tombstone(self):
        ctx = _ctx()
        st = _apply("skill:Вольф|Рывок|1")
        rpg.apply_op(st, {"kind": "rpg.skill.delete", "owner": "Вольф", "name": "Рывок"}, ctx)
        assert "Вольф" not in st["skills"]
        st = _apply("skill:Вольф|Рывок|2", st)
        assert "Вольф" not in st["skills"]  # ИИ не воскрешает удалённое пользователем
        rpg.apply_op(st, {"kind": "rpg.skill.add", "owner": "Вольф", "name": "Рывок",
                          "level": "5", "desc": "Сам"}, ctx)
        assert st["skills"]["Вольф"] == [{"name": "Рывок", "level": "5", "desc": "Сам", "user": True}]
        st = _apply("skill:Вольф|Рывок|6", st)
        assert st["skills"]["Вольф"][0]["level"] == "6" and st["skills"]["Вольф"][0]["user"] is True

    def test_equip_unequip_ops_bypass_validation(self):
        inv = {"Бант": {"icon": "🎀"}}
        back = {}
        ctx = _ctx({"equipment": {"locked": True, "chars": {}}}, inventory=inv, returned=back)
        st = rpg.empty_state()
        rpg.apply_op(st, {"kind": "rpg.equip", "owner": "Вольф", "slot": "Хвост", "name": "Бант",
                          "attrs": {"cha": "2"}}, ctx)
        assert st["equipment"]["Вольф"]["Хвост"] == [{"name": "Бант", "attrs": {"cha": 2},
                                                      "item": {"icon": "🎀"}}]
        rpg.apply_op(st, {"kind": "rpg.unequip", "owner": "Вольф", "slot": "Хвост", "name": "Бант"}, ctx)
        assert st["equipment"] == {} and back["Бант"]["holder"] == "Вольф"

    def test_unknown_kind_ignored(self):
        st = rpg.empty_state()
        rpg.apply_op(st, {"kind": "rpg.nope", "owner": "X"}, _ctx())
        rpg.apply_op(st, None, _ctx())
        assert st == rpg.empty_state()


# ==================== Рендер ====================

ALL_ON = {"rpg_bars": True, "rpg_skills": True, "rpg_attrs": True, "rpg_reputation": True,
          "rpg_equipment": True, "rpg_level": True, "rpg_currency": True, "rpg_stronghold": True}


def _full_state():
    inv = {"Шлем": {"icon": "⛑", "description": "Стальной"}}
    body = (
        "hp:Вольф=80/100(Здоровье)\nmp:Вольф=20/50\nstatus:Вольф=Отравлен\nstatus:Марина=Страх\n"
        "skill:Вольф|Рывок|2|Быстро\nattr:Вольф|str=60\nequip:Вольф|Голова|Шлем|def=2,atk=-1\n"
        "rep:Вольф|Гильдия=10\nlevel:Вольф=3\nxp:Вольф=50/300\n"
        "currency:Вольф|Серебро=5|Золото=2\nbase:Поместье>Кузница=2\nbase:Поместье|desc=Каменное\n"
        f"hp:{USER}=10/10\n"
    )
    st = _apply(body, ctx=_ctx(inventory=inv))
    st["reputation"]["Вольф"]["Гильдия"]["sub"] = {"Торговцы": 3, "Воры": -2}
    return st


CONFIG = {"currencies": [{"name": "Золото", "rate": 100}, {"name": "Серебро", "rate": 10}],
          "reputation": [{"name": "Гильдия"}]}


class TestRender:
    def test_all_sections(self):
        text = rpg.render_block(_full_state(), ALL_ON, present=["Вольф"], npc_ids={"Вольф": "001"},
                                user_name=USER, config=CONFIG)
        assert text == (
            "\n[RPG-статус]\n"
            "N001 Вольф: Здоровье 80/100 | MP 20/50 | статус:Отравлен\n"
            "\n[Список навыков]\n"
            "N001 Вольф:\n"
            "  Рывок 2 | Быстро\n"
            "\n[Атрибуты]\n"
            "N001 Вольф: Сила60 | Ловкость? | Выносливость? | Интеллект? | Мудрость? | Харизма?\n"
            "\n[Снаряжение]\n"
            "N001 Вольф: [Голова]Шлем{def+2,atk-1} \"Стальной\"\n"
            "\n[Репутация]\n"
            "N001 Вольф: Гильдия:10（Торговцы:+3 / Воры:-2）\n"
            "\n[Уровень]\n"
            "N001 Вольф: Lv.3 (опыт: 50/300)\n"
            "\n[Валюта]\n"
            "N001 Вольф: Золото×2, Серебро×5\n"
            "\n[Опорный пункт]\n"
            "Поместье — Каменное\n"
            "  Кузница Lv.2"
        )

    def test_defaults_hide_optional_modules(self):
        text = rpg.render_block(_full_state(), {}, present=[], npc_ids={}, user_name=USER, config=CONFIG)
        assert "[RPG-статус]" in text and "[Список навыков]" in text and "[Атрибуты]" in text
        for title in ("[Снаряжение]", "[Репутация]", "[Уровень]", "[Валюта]", "[Опорный пункт]"):
            assert title not in text

    def test_no_present_shows_everyone_without_ids(self):
        text = rpg.render_block(_full_state(), {"rpg_skills": False, "rpg_attrs": False},
                                present=[], npc_ids={}, user_name=USER, config={})
        assert "Вольф: Здоровье 80/100" in text
        assert f"{USER}: HP 10/10" in text
        assert "Марина: статус:Страх" in text

    def test_present_fuzzy_filter(self):
        text = rpg.render_block(_full_state(), {"rpg_skills": False, "rpg_attrs": False},
                                present=["Марина Иванова"], npc_ids={}, user_name=USER, config={})
        assert text == "\n[RPG-статус]\nМарина: статус:Страх"

    def test_strict_present_with_nobody_shows_nothing(self):
        assert rpg.render_block(_full_state(), {"rpg_strict_present": True}, present=[], npc_ids={},
                                user_name=USER, config={}) == ""

    def test_user_only_no_prefix_and_only_user(self):
        text = rpg.render_block(_full_state(), {"rpg_user_only": ["bars"], "rpg_skills": False,
                                                "rpg_attrs": False},
                                present=[], npc_ids={}, user_name=USER, config={})
        assert text == "\n[RPG-статус]\nHP 10/10"

    def test_header_skipped_when_owners_filtered(self):
        st = rpg.empty_state()
        rpg.apply_changes(st, rpg.parse_block("skill:Вольф|Рывок", user_name=USER), _ctx())
        text = rpg.render_block(st, {}, present=["Вольф"], npc_ids={}, user_name=USER, config={})
        assert text == "\n[Список навыков]\nВольф:\n  Рывок"
        assert rpg.render_block(st, {"rpg_user_only": ["skills"]}, present=[], npc_ids={},
                                user_name=USER, config={}) == ""

    def test_inactive_form_equipment_hidden(self):
        cfg = {"equipment": {"chars": {"Вольф": {"slots": [{"name": "Ошейник", "max": 1}]}}}}
        text = rpg.render_block(_full_state(), {"rpg_equipment": True, "rpg_bars": False,
                                                "rpg_skills": False, "rpg_attrs": False},
                                present=[], npc_ids={}, user_name=USER, config=cfg)
        assert text == ""

    def test_currency_without_config_shows_all(self):
        text = rpg.render_block(_full_state(), {"rpg_currency": True, "rpg_bars": False,
                                                "rpg_skills": False, "rpg_attrs": False},
                                present=[], npc_ids={}, user_name=USER, config={})
        assert text == "\n[Валюта]\nВольф: Серебро×5, Золото×2"

    def test_empty_state(self):
        assert rpg.render_block(rpg.empty_state(), ALL_ON, present=[], npc_ids={}, user_name=USER,
                                config={}) == ""
        assert rpg.render_block(None, ALL_ON, present=[], npc_ids={}, user_name=USER, config={}) == ""


# ==================== Правила ====================

class TestRules:
    def test_all_off_gives_empty_sections(self):
        off = {k: False for k in ALL_ON}
        secs = rpg.rules_sections(off, user_name=USER, present=[], config={}, state=rpg.empty_state())
        assert set(secs) == {"header", "bars", "attrs", "skills", "equipment", "reputation", "level",
                             "currency", "stronghold"}
        assert all(v == "" for v in secs.values())

    def test_default_sections(self):
        secs = rpg.rules_sections({}, user_name=USER, present=[], config={}, state=rpg.empty_state())
        assert secs["header"] == (
            "═══ [RPG] ═══\nВаш ответ ДОЛЖЕН включать тег <horaerpg> в конце.\n"
            f"Формат владельца следует нумерации NPC: N## полное имя. {USER} пишется напрямую без N.")
        assert secs["bars"].startswith("[Шкалы статуса — обязательны каждый ход, пропуск = провал!]")
        assert "✅ hp:владелец=текущее/макс(HP)" in secs["bars"]
        assert "status:владелец=эффект1/эффект2  ← если нет отклонений, пишите =нормально" in secs["bars"]
        assert "hp(HP: макс. по умолчанию 100; Здоровье" in secs["bars"]
        assert "attr:владелец|str=значение|dex=значение" in secs["attrs"]
        assert "str(Сила: Физическая атака" in secs["attrs"]
        assert "skill-:владелец|название навыка" in secs["skills"]
        for k in ("equipment", "reputation", "level", "currency", "stronghold"):
            assert secs[k] == ""

    def test_some_user_only(self):
        secs = rpg.rules_sections({"rpg_user_only": ["bars"]}, user_name=USER, present=[], config={},
                                  state=rpg.empty_state())
        assert f"Некоторые модули отслеживают только {USER} (отмечено ниже)." in secs["header"]
        assert "✅ hp:текущее/макс(HP)" in secs["bars"]
        assert f"Выводите только шкалы статуса и состояние {USER}:" in secs["bars"]
        assert "пропуск кого-либо" not in secs["bars"]
        assert "skill:владелец|" in secs["skills"]

    def test_all_enabled_user_only(self):
        s = {"rpg_user_only": ["bars", "skills", "attrs"]}
        secs = rpg.rules_sections(s, user_name=USER, present=[], config={}, state=rpg.empty_state())
        assert f"Все RPG-данные отслеживают только {USER}" in secs["header"]
        assert "attr:str=значение" in secs["attrs"]
        assert "skill:название навыка|уровень|описание эффекта" in secs["skills"]

    def test_optional_modules(self):
        cfg = {
            "equipment": {"locked": True, "chars": {
                "Вольф": {"slots": [{"name": "Голова", "max": 1}, {"name": "Хвост", "max": 1, "desc": "Бант"}],
                          "forms": [{"id": "base", "name": "Обычная", "slots": []}], "form": "base"},
                "Марина": {"slots": [{"name": "Голова", "max": 1}]}}},
            "reputation": [{"name": "Гильдия", "min": -10, "max": 10, "default": 0, "sub": ["Торговцы"]}],
            "currencies": [{"name": "Золото", "rate": 100}, {"name": "Серебро", "rate": 10},
                           {"name": "Медь", "rate": 1}],
        }
        st = _apply("base:Поместье>Кузница=2\nbase:Поместье=3\nbase:Башня|desc=Древняя\nbase:Пустошь")
        secs = rpg.rules_sections(ALL_ON, user_name=USER, present=["Вольф"], config=cfg, state=st)
        eq = secs["equipment"]
        assert "equip:владелец|слот|предмет|стат1=значение,стат2=значение" in eq
        assert "Вольф слоты текущая форма:Обычная: Голова(×1), Хвост(×1: Бант)" in eq
        assert "Марина" not in eq  # не в сцене
        assert "Слоты снаряжения заблокированы" in eq
        rep = secs["reputation"]
        assert "  - Гильдия（-10~10; по умолчанию 0; подэлементы:Торговцы）" in rep
        assert "rep:владелец|категория=текущее значение" in rep
        assert "рекомендуемая формула = уровень × 100" in secs["level"]
        cur = secs["currency"]
        assert f"  currency:{USER}|Золото=+10" in cur and f"  currency:{USER}|Серебро=+50" in cur
        assert "Зарегистрированные валюты: Золото, Серебро, Медь" in cur
        assert "Курсы обмена: 1Медь = 10Серебро = 100Золото" in cur
        assert "Текущие крепости: Поместье Lv.3(Кузница); Башня" in secs["stronghold"]

    def test_reputation_and_currency_need_config(self):
        secs = rpg.rules_sections(ALL_ON, user_name=USER, present=[], config={}, state=rpg.empty_state())
        assert secs["reputation"] == "" and secs["currency"] == ""
        assert secs["equipment"].startswith("[Снаряжение]")
        assert secs["stronghold"].startswith("[Крепости]")

    def test_fill_prompt(self):
        secs = rpg.rules_sections({}, user_name=USER, present=[], config={}, state=rpg.empty_state())
        text = rpg.fill_rpg_prompt(rpg.DEFAULT_RPG_TEMPLATE, secs)
        assert "[[" not in text and "\n\n\n" not in text
        assert text.startswith("═══ [RPG] ═══") and text.endswith("skill-:владелец|название навыка")
        assert rpg.fill_rpg_prompt("До\n[[rpg.full]]\nПосле", secs) == "До\n" + text + "\nПосле"
        assert rpg.fill_rpg_prompt("[[ RPG.Header ]]", secs) == secs["header"]

    def test_default_template_order(self):
        assert rpg.DEFAULT_RPG_TEMPLATE == (
            "[[rpg.header]]\n\n[[rpg.bars]]\n\n[[rpg.attrs]]\n\n[[rpg.skills]]\n\n"
            "[[rpg.equipment]]\n\n[[rpg.reputation]]\n\n[[rpg.level]]\n\n[[rpg.currency]]\n\n"
            "[[rpg.stronghold]]")


# ==================== Конфиги, шаблоны, мелочи ====================

class TestDefaultsAndHelpers:
    def test_default_configs(self):
        bars = rpg.default_bar_config()
        assert [(b["key"], b["name"], b["color"], b["max"]) for b in bars] == [
            ("hp", "HP", "#22c55e", 100), ("mp", "MP", "#6366f1", 100), ("sp", "SP", "#f59e0b", 100)]
        assert bars[0]["desc"].startswith("Здоровье;")
        attrs = rpg.default_attr_config()
        assert [a["name"] for a in attrs] == ["Сила", "Ловкость", "Выносливость", "Интеллект",
                                             "Мудрость", "Харизма"]
        assert all(a["desc"] for a in attrs)

    def test_equipment_templates_shape(self):
        tpls = rpg.default_equipment_templates()
        ids = [t["id"] for t in tpls]
        assert ids == ["human", "orc", "pigman", "winged", "centaur", "lamia", "demon", "kitsune",
                       "shapeshifter", "feathered_serpent"]
        human = tpls[0]
        assert human["name"] == "Человек" and "человек" in human["aliases"]
        assert human["forms"][0]["id"] == "base" and human["forms"][0]["name"] == "Обычная"
        assert {"name": "Голова", "max": 1} in human["forms"][0]["slots"]
        assert {"name": "Кольцо", "max": 2} in human["forms"][0]["slots"]
        centaur = next(t for t in tpls if t["id"] == "centaur")
        assert {"name": "Подкова", "max": 4} in centaur["forms"][0]["slots"]
        kitsune = next(t for t in tpls if t["id"] == "kitsune")
        assert [f["id"] for f in kitsune["forms"]] == ["human", "hybrid", "fox"]
        tail = next(s for s in kitsune["forms"][1]["slots"] if s["name"] == "Украшение хвоста")
        assert tail["max"] == 9 and tail["desc"]

    @pytest.mark.parametrize("race,expected", [
        ("Человек", "human"), ("кентавр", "centaur"), ("Кентавры степей", "centaur"),
        ("Ёкай", "shapeshifter"), ("лиса-оборотень", "kitsune"), ("Kitsune", "kitsune"),
        ("ламия", "lamia"), ("  ОРК ", "orc"),
    ])
    def test_match_template(self, race, expected):
        tpl = rpg.match_template_by_race(race, rpg.default_equipment_templates())
        assert tpl is not None and tpl["id"] == expected

    @pytest.mark.parametrize("race", ["", "эльф", "зверолюд", "монстр", "крылатый демон"])
    def test_match_template_none(self, race):
        assert rpg.match_template_by_race(race, rpg.default_equipment_templates()) is None

    def test_document_lines(self):
        ch = rpg.parse_block(
            "level:Вольф=3\nequip:Вольф|Голова|Шлем\nunequip:Вольф|Руки|Перчатки\n"
            "base:Поместье>Кузница=2\nbase:Башня|desc=Древняя\nhp:Вольф=1/2\nskill:Вольф|Рывок|2",
            user_name=USER)
        assert rpg.document_lines(ch) == [
            "Вольф уровень 3", "Вольф навык Рывок 2", "Вольф экипировал Шлем (Голова)",
            "Вольф снял Перчатки (Руки)", "база Поместье>Кузница уровень 2", "база Башня: Древняя"]
        assert rpg.document_lines(None) == []

    @pytest.mark.parametrize("effect,icon", [
        ("Отравлен", "☠️"), ("Кровотечение", "🩸"), ("ожог", "🔥"), ("Заморожен", "❄️"),
        ("Оглушён", "💫"), ("сон", "💤"), ("Страх", "😱"), ("Ярость", "😡"), ("Poisoned", "☠️"),
        ("中毒", "☠️"), ("тяжело ранен", "💀"), ("ранен", "🩹"), ("нормально", "✅"),
        ("взгляд", "•"), ("ненормальный", "•"), ("", "•"), ("что-то новое", "•"),
    ])
    def test_status_icon(self, effect, icon):
        assert rpg.status_icon(effect) == icon

    def test_modules_constant(self):
        assert rpg.MODULES == ("bars", "skills", "attrs", "reputation", "equipment", "level",
                               "currency", "stronghold")
        assert rpg.empty_changes() == {"bars": {}, "status": {}, "skills": [], "skills_removed": [],
                                       "attrs": {}, "reputation": {}, "equip": [], "unequip": [],
                                       "levels": {}, "xp": {}, "currency": [], "base": []}
        assert rpg.empty_state() == {"bars": {}, "status": {}, "skills": {}, "attrs": {},
                                     "reputation": {}, "equipment": {}, "levels": {}, "xp": {},
                                     "currency": {}, "strongholds": []}
