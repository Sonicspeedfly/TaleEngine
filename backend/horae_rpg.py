"""
RPG-слой Horae: разбор <horaerpg>, применение изменений, рендер и правила (только stdlib).

Зачем модуль. В плагине Horae RPG-данные (шкалы HP/MP, статусы, навыки,
атрибуты, снаряжение, репутация, уровень, валюта, опорные пункты) модель
пишет строками в теге <horaerpg> в конце ответа, плагин копит их в chat[0]
и на каждом ходу подмешивает в промпт. У нас состояние не хранится, а
считается повтором (replay) мет сообщений и журнала правок пользователя, поэтому
здесь только чистые функции:

- parse_block   — строки <horaerpg> → RPG_CHANGES одного сообщения;
- apply_changes — RPG_CHANGES → мутация RPG_STATE (шаг повтора);
- apply_op      — правка пользователя (rpg.*) → мутация RPG_STATE;
- render_block  — RPG_STATE → русские разделы блока состояния;
- rules_sections / fill_rpg_prompt — правила формата для системного промпта.

Модуль не знает ни про БД, ни про предметы и NPC: разрешение владельцев
(«N001 Имя», псевдонимы, {{user}}) и перенос предметов между инвентарём и
снаряжением делает вызывающий через ApplyContext. Поэтому весь контракт
проверяется тестами без базы (tests/test_horae_rpg.py).

Исправленные ошибки плагина (см. spec_core §2.4, §3.5):
- `xp:Имя=50/100` и другие известные префиксы больше не принимаются за шкалу;
- «нормально/норма/нет/без отклонений» (их подсказывает русский промпт)
  очищают список статусов, а не становятся статусом «нормально»;
- ключи строк регистронезависимы, двоеточие — `:` или `：`;
- подпись шкалы (`hp:…(Здоровье)`) пишется только при первом упоминании —
  она больше не теряется при следующем обновлении без подписи;
- навык без уровня/описания в повторной строке не стирает прежние;
- id опорных пунктов детерминированы (sh_1, sh_2…), иначе каждый повтор
  выдавал бы новые случайные id.

Необязательные служебные ключи RPG_STATE (появляются лениво, только после
правок пользователя): `deleted_skills` — [[владелец, имя]], `deleted_bases` —
[[имя, имя_родителя|None]]. Это «надгробия»: удалённое пользователем ИИ не
воскрешает (в плагине — `_deletedSkills` / `_deletedStrongholds`). Они живут
в состоянии, а не в конфиге, потому что правка — запись журнала: откат правки
снимает и надгробие.
"""
from __future__ import annotations

import copy
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Callable

# ============================================================================
# Модули и настройки
# ============================================================================

MODULES = ("bars", "skills", "attrs", "reputation", "equipment", "level", "currency", "stronghold")
# Модули с полем владельца: только они бывают «только для пользователя».
# У опорных пунктов владельца нет — это общий для сюжета объект.
OWNER_MODULES = MODULES[:7]

# Ключ настройки (design §7) → модуль. По умолчанию включены шкалы, навыки и
# атрибуты — как в плагине (sendRpg* !== false), остальные надо включить явно.
_SETTING_KEYS = {
    "bars": "rpg_bars", "skills": "rpg_skills", "attrs": "rpg_attrs",
    "reputation": "rpg_reputation", "equipment": "rpg_equipment",
    "level": "rpg_level", "currency": "rpg_currency", "stronghold": "rpg_stronghold",
}
_DEFAULT_ON = frozenset({"bars", "skills", "attrs"})

# Раздел RPG_CHANGES → модуль (для фильтра «только пользователь»).
_CHANGE_MODULE = {
    "bars": "bars", "status": "bars", "skills": "skills", "skills_removed": "skills",
    "attrs": "attrs", "reputation": "reputation", "equip": "equipment",
    "unequip": "equipment", "levels": "level", "xp": "level", "currency": "currency",
}


def _module_on(settings: dict, module: str) -> bool:
    val = (settings or {}).get(_SETTING_KEYS[module])
    if val is None:
        return module in _DEFAULT_ON
    return bool(val)


def _user_only_set(settings: dict) -> frozenset:
    raw = (settings or {}).get("rpg_user_only") or ()
    if isinstance(raw, str):
        raw = [p for p in re.split(r"[,\s]+", raw) if p]
    return frozenset(str(m).strip() for m in raw if str(m).strip() in OWNER_MODULES)


# ============================================================================
# Конфиги по умолчанию (русские подписи из locales/ru.json плагина)
# ============================================================================

def default_bar_config() -> list[dict]:
    """Шкалы статуса по умолчанию. `max` — максимум новой шкалы по умолчанию."""
    return [
        {"key": "hp", "name": "HP", "color": "#22c55e", "max": 100,
         "desc": "Здоровье; меняется от ранений, яда, лечения и отдыха"},
        {"key": "mp", "name": "MP", "color": "#6366f1", "max": 100,
         "desc": "Мана; меняется от заклинаний, расхода маны, медитации или восстанавливающих предметов"},
        {"key": "sp", "name": "SP", "color": "#f59e0b", "max": 100,
         "desc": "Выносливость; меняется от бега, уклонения, рывков в ближнем бою, усталости и отдыха"},
    ]


def default_attr_config() -> list[dict]:
    """Шесть атрибутов D&D с русскими названиями; значения 0–100."""
    return [
        {"key": "str", "name": "Сила", "desc": "Физическая атака, грузоподъёмность и урон в ближнем бою"},
        {"key": "dex", "name": "Ловкость", "desc": "Рефлексы, уклонение и точность дальнего боя"},
        {"key": "con", "name": "Выносливость", "desc": "Жизненная сила, стойкость и сопротивление ядам"},
        {"key": "int", "name": "Интеллект", "desc": "Знания, магия и аналитические способности"},
        {"key": "wis", "name": "Мудрость", "desc": "Проницательность, интуиция и сила воли"},
        {"key": "cha", "name": "Харизма", "desc": "Убеждение, лидерство и обаяние"},
    ]


# Русские названия слотов (locales/ru.json → equipmentTemplates.slots).
_SLOT_NAMES = {
    "head": "Голова", "torso": "Торс", "hands": "Руки", "belt": "Пояс", "legs": "Ноги",
    "feet": "Обувь", "neck": "Ожерелье", "amulet": "Амулет", "ring": "Кольцо",
    "tail": "Хвост", "wings": "Крылья", "collar": "Ошейник", "clawGuard": "Когтевые накладки",
    "tailOrnament": "Украшение хвоста", "serpentTailOrnament": "Украшение змеиного хвоста",
    "barding": "Конская броня", "horseshoe": "Подкова", "hornOrnament": "Украшение рогов",
}
_SLOT_DESCS = {
    "tail": "Снаряжение или украшение хвоста; не подходит формам без пригодного хвоста",
    "wings": "Защита крыльев, перьевые украшения или средства полёта",
    "tailOrnament": "Обычно одно украшение на каждый пригодный хвост",
    "serpentTailOrnament": "Защита, кольца или снаряжение, подогнанное под змеиный хвост",
}

# Псевдонимы рас для автоподбора шаблона. Русские — первыми; английские и
# китайские оставлены ради чатов, импортированных из SillyTavern.
_TEMPLATE_ALIASES = {
    "human": ["человек", "люди", "людской", "human", "人类", "人類", "人間", "인간"],
    "orc": ["орк", "орчиха", "orc", "オーク", "欧克", "歐克", "奥克", "奧克", "兽化人形", "獸化人形", "오크"],
    "pigman": ["свинолюд", "pigfolk", "pigman", "猪人", "豬人", "豚人"],
    "winged": ["крылатый", "крылатая", "крылатые", "winged", "翼族", "翼人", "有翼种", "有翼種"],
    "centaur": ["кентавр", "centaur", "人马", "人馬", "ケンタウロス"],
    "lamia": ["ламия", "змеинохвост", "lamia", "serpentine", "拉弥亚", "拉彌亞", "蛇尾人", "ラミア"],
    "demon": ["демон", "демоница", "demon", "恶魔", "惡魔", "悪魔", "악마"],
    "kitsune": ["кицунэ", "кицуне", "лисий дух", "лиса-оборотень", "kitsune", "fox spirit",
                "九尾狐", "狐妖", "구미호"],
    "shapeshifter": ["оборотень", "ёкай", "shapeshifter", "yokai", "youkai", "妖怪", "变身者", "變身者"],
    "feathered_serpent": ["пернатый змей", "пернатая змея", "feathered serpent", "羽蛇人"],
}
# Слишком общие слова: «зверолюд» может быть и орком, и кицунэ — шаблон по
# такой расе не подбираем (в плагине HORAEEQ_AMBIGUOUS_TEMPLATE_ALIASES).
_AMBIGUOUS_RACES = frozenset({
    "зверолюд", "зверолюди", "зверочеловек", "монстр", "beastfolk", "beastman", "beastwoman",
    "monster", "兽人", "獸人", "獣人", "수인", "妖怪", "yokai", "youkai", "shapeshifter",
    "变身者", "變身者", "変身者",
})


def default_equipment_templates() -> list[dict]:
    """
    Расовые шаблоны слотов снаряжения (порт _getDefaultEquipTemplates).

    Шаблон: {id, name, aliases, forms:[{id, name, slots:[{name, max[, desc]}]}]}.
    У одноформенных рас единственная форма — «base» («Обычная»); у кицунэ и
    оборотня формы переключаются, и снаряжение чужой формы становится неактивным.
    """
    def slot(key: str, max_count: int = 1) -> dict:
        s = {"name": _SLOT_NAMES[key], "max": max_count}
        if key in _SLOT_DESCS:
            s["desc"] = _SLOT_DESCS[key]
        return s

    def humanoid() -> list[dict]:
        return [slot("head"), slot("torso"), slot("hands"), slot("belt"), slot("legs"),
                slot("feet"), slot("neck"), slot("amulet"), slot("ring", 2)]

    def tpl(tid: str, name: str, slots: list[dict] | None = None,
            forms: list[dict] | None = None) -> dict:
        return {"id": tid, "name": name, "aliases": list(_TEMPLATE_ALIASES.get(tid, [])),
                "forms": forms or [{"id": "base", "name": "Обычная", "slots": slots or []}]}

    return [
        tpl("human", "Человек", humanoid()),
        tpl("orc", "Орк / зверолюд-гуманоид",
            [slot("head"), slot("torso"), slot("hands"), slot("belt"), slot("legs"),
             slot("feet"), slot("neck"), slot("ring", 2)]),
        tpl("pigman", "Свинолюд",
            [slot("head"), slot("torso"), slot("hands"), slot("belt"), slot("legs"),
             slot("feet"), slot("neck"), slot("ring", 2)]),
        tpl("winged", "Крылатый",
            [slot("head"), slot("torso"), slot("hands"), slot("belt"), slot("legs"),
             slot("feet"), slot("wings"), slot("neck"), slot("ring", 2)]),
        tpl("centaur", "Кентавр",
            [slot("head"), slot("torso"), slot("hands"), slot("belt"), slot("barding"),
             slot("horseshoe", 4), slot("neck"), slot("ring", 2)]),
        tpl("lamia", "Ламия / змеинохвостый",
            [slot("head"), slot("torso"), slot("hands"), slot("belt"),
             slot("serpentTailOrnament"), slot("neck"), slot("amulet"), slot("ring", 2)]),
        tpl("demon", "Демон",
            [slot("head"), slot("hornOrnament"), slot("torso"), slot("hands"), slot("belt"),
             slot("legs"), slot("feet"), slot("wings"), slot("tail"), slot("neck"),
             slot("ring", 2)]),
        tpl("kitsune", "Кицунэ / лисий дух", forms=[
            {"id": "human", "name": "Человеческая форма", "slots": humanoid()},
            {"id": "hybrid", "name": "Полулисья форма",
             "slots": [slot("head"), slot("torso"), slot("hands"), slot("belt"), slot("legs"),
                       slot("feet"), slot("tailOrnament", 9), slot("neck"), slot("ring", 2)]},
            {"id": "fox", "name": "Лисья форма",
             "slots": [slot("collar"), slot("tailOrnament", 9), slot("clawGuard", 4)]},
        ]),
        tpl("shapeshifter", "Ёкай / оборотень", forms=[
            {"id": "human", "name": "Человеческая форма", "slots": humanoid()},
            {"id": "hybrid", "name": "Полузвериная форма",
             "slots": [slot("head"), slot("torso"), slot("hands"), slot("belt"), slot("legs"),
                       slot("feet"), slot("tail"), slot("neck"), slot("ring", 2)]},
            {"id": "animal", "name": "Звериная форма",
             "slots": [slot("collar"), slot("tail"), slot("clawGuard", 4)]},
        ]),
        tpl("feathered_serpent", "Пернатый змей",
            [slot("head"), slot("torso"), slot("hands"), slot("belt"), slot("wings"),
             slot("serpentTailOrnament"), slot("neck"), slot("ring", 2)]),
    ]


def _norm_match(text) -> str:
    """Ключ сравнения рас: NFKC, регистр, ё=е, без кавычек и пробелов (как в плагине)."""
    s = unicodedata.normalize("NFKC", str(text or "")).casefold().replace("ё", "е")
    return re.sub(r"[「」『』\"'«»\s]", "", s)


def match_template_by_race(race: str, templates: list[dict]) -> dict | None:
    """
    Шаблон снаряжения по расе NPC — только однозначный.

    Сначала точное совпадение с псевдонимом (или id/именем шаблона), затем —
    псевдоним как подстрока расы («высший эльф-кентавр» → кентавр). Если
    подходят два шаблона («крылатый демон»), возвращаем None: неверно
    подобранные слоты хуже пустых — ИИ начнёт надевать хвостовые украшения
    человеку, а пользователь не поймёт, откуда они взялись.
    """
    key = _norm_match(race)
    if not key or key in _AMBIGUOUS_RACES:
        return None
    tpls = [t for t in (templates or []) if isinstance(t, dict)]

    def aliases(t: dict) -> set[str]:
        raw = [t.get("id"), t.get("name"), *(t.get("aliases") or [])]
        return {a for a in (_norm_match(x) for x in raw if x) if a and a not in _AMBIGUOUS_RACES}

    exact = [t for t in tpls if key in aliases(t)]
    if exact:
        return exact[0] if len(exact) == 1 else None
    partial = [t for t in tpls if any(len(a) >= 3 and a in key for a in aliases(t))]
    return partial[0] if len(partial) == 1 else None


# ============================================================================
# RPG_CHANGES и разбор <horaerpg>
# ============================================================================

def empty_changes() -> dict:
    return {"bars": {}, "status": {}, "skills": [], "skills_removed": [], "attrs": {},
            "reputation": {}, "equip": [], "unequip": [], "levels": {}, "xp": {},
            "currency": [], "base": []}


def has_changes(changes) -> bool:
    if not isinstance(changes, dict):
        return False
    return any(bool(changes.get(k)) for k in empty_changes())


# Ключ строки: латиница, цифры, «_», необязательный хвостовой «-» (skill-:).
_KEY_RE = re.compile(r"^([A-Za-z][A-Za-z0-9_]*-?)\s*[:：]\s*(.*)$", re.S)
# Шкала: владелец=текущее/макс(подпись). Скобки подписи — и полноширинные.
_BAR_RE = re.compile(r"^(.+?)\s*=\s*(-?\d+)\s*/\s*(-?\d+)\s*(?:[(（]\s*(.*?)\s*[)）])?\s*$")
_BAR_UO_RE = re.compile(r"^(-?\d+)\s*/\s*(-?\d+)\s*(?:[(（]\s*(.*?)\s*[)）])?\s*$")
_KV_INT_RE = re.compile(r"^(.+?)\s*=\s*([+-]?\d+)$")
_ATTR_KV_RE = re.compile(r"^([A-Za-z0-9_]+)\s*=\s*([+-]?\d+)$")
_XP_RE = re.compile(r"^(\d+)\s*/\s*(\d+)$")
_PIPE_RE = re.compile(r"\s*[|｜]\s*")
_BASE_FIELD_RE = re.compile(r"^(desc|level|описание|уровень)\s*=\s*(.+)$", re.I | re.S)
_INT_PREFIX_RE = re.compile(r"\s*([+-]?\d+)")
# Слова «нет отклонений». Русские нужны потому, что русский промпт плагина сам
# велит писать «=нормально», а плагин понимал только 正常/none/normal/clear —
# и «нормально» копилось у персонажа как статус.
_STATUS_CLEAR_RE = re.compile(
    r"^(?:нормально|норма|в норме|нет|без отклонений|正常|无|無|none|normal|clear)$", re.I)
# Известные префиксы. Всё прочее вида `ключ:владелец=N/M` — шкала; без этого
# списка `xp:Имя=50/100` в плагине становился шкалой «XP» и не доходил до опыта.
_KNOWN_KEYS = frozenset({"status", "skill", "skill-", "equip", "unequip", "rep", "level",
                         "xp", "currency", "attr", "base"})


def _js_int(value) -> int | None:
    """parseInt из JS: ведущие цифры со знаком («2-й» → 2), иначе None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    m = _INT_PREFIX_RE.match(str(value or ""))
    return int(m.group(1)) if m else None


def _split(rest: str) -> list[str]:
    return [p.strip() for p in _PIPE_RE.split(rest.strip())] if rest.strip() else []


def _part(parts: list[str], i: int) -> str:
    return parts[i] if len(parts) > i else ""


def _drop_uo_owner(parts: list[str], user: str, min_len: int) -> list[str]:
    """
    В режиме «только пользователь» модель иногда всё же пишет владельца
    (`equip:Имя|Голова|Шлем`). Если первая часть — имя пользователя и частей
    хватает, отбрасываем её, иначе слот стал бы «Имя», а предмет — «Голова».
    """
    if len(parts) >= min_len and parts and parts[0].casefold() == user.casefold():
        return parts[1:]
    return parts


def _effects(value: str) -> list[str]:
    v = value.strip().rstrip(".!。")
    if not v or _STATUS_CLEAR_RE.match(v):
        return []
    out: list[str] = []
    for e in re.split(r"[/／]", v):
        e = e.strip()
        if e and not _STATUS_CLEAR_RE.match(e) and e not in out:
            out.append(e)
    return out


def _parse_bar(ch: dict, key: str, rest: str, user: str, uo: frozenset) -> None:
    m = _BAR_RE.match(rest)
    if m:
        owner = user if "bars" in uo else m.group(1).strip()
        if owner:
            _put_bar(ch, owner, key, m.group(2), m.group(3), m.group(4))
        return
    if "bars" in uo:
        m = _BAR_UO_RE.match(rest)
        if m:
            _put_bar(ch, user, key, m.group(1), m.group(2), m.group(3))


def _put_bar(ch: dict, owner: str, key: str, cur: str, mx: str, label: str | None) -> None:
    val = [int(cur), int(mx)]
    if label and label.strip():
        val.append(label.strip())
    ch["bars"].setdefault(owner, {})[key] = val


def _parse_status(ch: dict, rest: str, user: str, uo: frozenset) -> None:
    eq = rest.find("=")
    if "bars" in uo and eq < 0:
        ch["status"][user] = _effects(rest)
    elif eq > 0:
        owner = user if "bars" in uo else rest[:eq].strip()
        if owner:
            ch["status"][owner] = _effects(rest[eq + 1:])


def _parse_skill(ch: dict, rest: str, user: str, uo: frozenset) -> None:
    parts = _split(rest)
    if "skills" in uo:
        parts = _drop_uo_owner(parts, user, 3)
        if _part(parts, 0):
            ch["skills"].append({"owner": user, "name": parts[0], "level": _part(parts, 1),
                                 "desc": "|".join(parts[2:])})
    elif len(parts) >= 2 and parts[0] and parts[1]:
        ch["skills"].append({"owner": parts[0], "name": parts[1], "level": _part(parts, 2),
                             "desc": "|".join(parts[3:])})


def _parse_skill_removed(ch: dict, rest: str, user: str, uo: frozenset) -> None:
    parts = _split(rest)
    if "skills" in uo:
        parts = _drop_uo_owner(parts, user, 2)
        if _part(parts, 0):
            ch["skills_removed"].append({"owner": user, "name": parts[0]})
    elif len(parts) >= 2 and parts[0] and parts[1]:
        ch["skills_removed"].append({"owner": parts[0], "name": parts[1]})


def _parse_attrs_text(text: str) -> dict:
    """«atk=5,def=-1» → {"atk": 5, "def": -1}; ключи — как написаны."""
    out = {}
    for kv in re.split(r"[,，]", text or ""):
        m = _KV_INT_RE.match(kv.strip())
        if m:
            out[m.group(1).strip()] = int(m.group(2))
    return out


def _parse_equip(ch: dict, rest: str, user: str, uo: frozenset, *, remove: bool) -> None:
    parts = _split(rest)
    if "equipment" in uo:
        parts = _drop_uo_owner(parts, user, 3)
        if len(parts) < 2:
            return
        owner, slot, name, attrs = user, parts[0], parts[1], _part(parts, 2)
    else:
        if len(parts) < 3:
            return
        owner, slot, name, attrs = parts[0], parts[1], parts[2], _part(parts, 3)
    if not (owner and slot and name):
        return
    if remove:
        ch["unequip"].append({"owner": owner, "slot": slot, "name": name})
    else:
        ch["equip"].append({"owner": owner, "slot": slot, "name": name,
                            "attrs": _parse_attrs_text(attrs)})


def _parse_rep(ch: dict, rest: str, user: str, uo: frozenset) -> None:
    parts = _split(rest)
    if "reputation" in uo:
        owner, kvs = user, parts
    else:
        if len(parts) < 2:
            return
        owner, kvs = parts[0], parts[1:]
    for kv in kvs:
        m = _KV_INT_RE.match(kv)
        if m and owner:
            ch["reputation"].setdefault(owner, {})[m.group(1).strip()] = int(m.group(2))


def _parse_level(ch: dict, rest: str, user: str, uo: frozenset) -> None:
    eq = rest.find("=")
    if "level" in uo:
        val = _js_int(rest[eq + 1:] if eq >= 0 else rest)
        if val is not None:
            ch["levels"][user] = val
    elif eq > 0:
        owner, val = rest[:eq].strip(), _js_int(rest[eq + 1:])
        if owner and val is not None:
            ch["levels"][owner] = val


def _parse_xp(ch: dict, rest: str, user: str, uo: frozenset) -> None:
    eq = rest.find("=")
    if "level" in uo:
        m = _XP_RE.match((rest[eq + 1:] if eq >= 0 else rest).strip())
        if m:
            ch["xp"][user] = [int(m.group(1)), int(m.group(2))]
    elif eq > 0:
        owner = rest[:eq].strip()
        m = _XP_RE.match(rest[eq + 1:].strip())
        if owner and m:
            ch["xp"][owner] = [int(m.group(1)), int(m.group(2))]


def _parse_currency(ch: dict, rest: str, user: str, uo: frozenset) -> None:
    parts = _split(rest)
    if "currency" in uo:
        owner, kvs = user, parts
    else:
        if len(parts) < 2:
            return
        owner, kvs = parts[0], parts[1:]
    # Несколько валют в одной строке (`currency:Имя|Золото=+5|Серебро=-2`) —
    # плагин брал только первую, остальные молча терялись.
    for kv in kvs:
        m = _KV_INT_RE.match(kv)
        if m and owner:
            raw = m.group(2)
            ch["currency"].append({"owner": owner, "name": m.group(1).strip(),
                                   "value": int(raw), "delta": raw[0] in "+-"})


def _parse_attr(ch: dict, rest: str, user: str, uo: frozenset) -> None:
    parts = _split(rest)
    if not parts:
        return
    if "attrs" in uo:
        owner, kvs = user, parts
    else:
        owner, kvs = parts[0], parts[1:]
    vals = {}
    for kv in kvs:
        m = _ATTR_KV_RE.match(kv)
        if m:
            vals[m.group(1).lower()] = int(m.group(2))
    if owner and vals:
        ch["attrs"].setdefault(owner, {}).update(vals)


def _parse_base(ch: dict, rest: str, user: str, uo: frozenset) -> None:
    m = re.search(r"[|｜]", rest)
    if m:
        path, tail = rest[:m.start()].strip(), rest[m.end():].strip()
        fm = _BASE_FIELD_RE.match(tail)
        if not (path and fm):
            return
        is_level = fm.group(1).lower() in ("level", "уровень")
        value = fm.group(2).strip()
        if is_level:
            num = _js_int(value)
            if num is not None:
                ch["base"].append({"path": path, "field": "level", "value": num})
        elif value:
            ch["base"].append({"path": path, "field": "desc", "value": value})
        return
    eq = rest.find("=")
    if eq < 0:
        return
    path, value = rest[:eq].strip(), rest[eq + 1:].strip()
    if not path or not value:
        return
    # Как parseInt в плагине: ведущие цифры означают уровень («2» → level 2).
    num = _js_int(value)
    if num is not None:
        ch["base"].append({"path": path, "field": "level", "value": num})
    else:
        ch["base"].append({"path": path, "field": "desc", "value": value})


_LINE_HANDLERS: dict[str, Callable] = {
    "status": _parse_status,
    "skill": _parse_skill,
    "skill-": _parse_skill_removed,
    "equip": lambda ch, r, u, uo: _parse_equip(ch, r, u, uo, remove=False),
    "unequip": lambda ch, r, u, uo: _parse_equip(ch, r, u, uo, remove=True),
    "rep": _parse_rep,
    "level": _parse_level,
    "xp": _parse_xp,
    "currency": _parse_currency,
    "attr": _parse_attr,
    "base": _parse_base,
}


def parse_block(body: str, *, user_name: str,
                user_only: set[str] | frozenset = frozenset()) -> dict | None:
    """
    Строки тела <horaerpg> → RPG_CHANGES (design §9) или None, если ничего не разобрано.

    user_only — модули, где у строк нет поля владельца: владелец — user_name
    (пустое имя → «{{user}}», его разрешит вызывающий).
    """
    uo = frozenset(user_only or ())
    user = (user_name or "").strip() or "{{user}}"
    ch = empty_changes()
    for raw in (body or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        m = _KEY_RE.match(line)
        if not m:
            continue
        key, rest = m.group(1).lower(), m.group(2).strip()
        handler = _LINE_HANDLERS.get(key)
        if handler is not None:
            handler(ch, rest, user, uo)
        elif key not in _KNOWN_KEYS and not key.endswith("-"):
            _parse_bar(ch, key, rest, user, uo)
    return ch if has_changes(ch) else None


def _normalize_changes(changes) -> dict:
    """
    Приводит изменения к RPG_CHANGES. Понимает и формат плагина
    (`removedSkills`, `attributes`, `equipment`, `baseChanges`, `isDelta`) —
    меты, импортированные из SillyTavern, иначе теряли бы часть RPG.
    """
    out = empty_changes()
    if not isinstance(changes, dict):
        return out
    out["bars"] = changes.get("bars") or {}
    out["status"] = changes.get("status") or {}
    out["skills"] = changes.get("skills") or []
    out["skills_removed"] = changes.get("skills_removed") or changes.get("removedSkills") or []
    out["attrs"] = changes.get("attrs") or changes.get("attributes") or {}
    out["reputation"] = changes.get("reputation") or {}
    equip = changes.get("equip")
    if equip is None and isinstance(changes.get("equipment"), list):
        equip = changes.get("equipment")
    out["equip"] = equip or []
    out["unequip"] = changes.get("unequip") or []
    out["levels"] = changes.get("levels") or {}
    out["xp"] = changes.get("xp") or {}
    out["currency"] = [
        {"owner": c.get("owner"), "name": c.get("name"), "value": c.get("value"),
         "delta": bool(c.get("delta", c.get("isDelta")))}
        for c in (changes.get("currency") or []) if isinstance(c, dict)
    ]
    out["base"] = changes.get("base") or changes.get("baseChanges") or []
    return out


# ============================================================================
# RPG_STATE и применение изменений
# ============================================================================

def empty_state() -> dict:
    return {"bars": {}, "status": {}, "skills": {}, "attrs": {}, "reputation": {},
            "equipment": {}, "levels": {}, "xp": {}, "currency": {}, "strongholds": []}


def _ensure_state(state: dict) -> None:
    for k, v in empty_state().items():
        if not isinstance(state.get(k), type(v)):
            state[k] = v


def _bar_dict(val) -> dict:
    """
    Шкала состояния как {cur, max, label}. Принимает и список [cur, max, label?]
    — так шкалы лежат в RPG плагина, и стартовое состояние (seed), перенесённое
    из импортированного чата, не должно ронять повтор.
    """
    if isinstance(val, dict):
        return val
    if isinstance(val, (list, tuple)) and len(val) >= 2:
        return {"cur": val[0], "max": val[1], "label": val[2] if len(val) > 2 and val[2] else ""}
    return {}


def _identity(name: str) -> str:
    return name


def _no_item(name: str) -> dict | None:
    return None


def _drop_item(name: str, info: dict) -> None:
    return None


@dataclass
class ApplyContext:
    """
    Всё, что шаг повтора RPG берёт снаружи.

    resolve_owner — «N001 Имя», псевдонимы NPC, {{user}} → каноническое имя;
    config — RPG_CONFIG чата (design §9) плюс необязательные надгробия
    `deleted_skills` ([[владелец, имя]]) и `deleted_currencies` ([{name, at}]);
    take_item/give_item — перенос предмета из инвентаря в слот и обратно
    (инвентарь — чужая часть состояния, её ведёт horae_state);
    mid — id сообщения, чьи изменения применяются (для `deleted_currencies.at`).
    """
    resolve_owner: Callable[[str], str] = _identity
    config: dict = field(default_factory=dict)
    user_name: str = ""
    user_only: frozenset = frozenset()
    take_item: Callable[[str], dict | None] = _no_item
    give_item: Callable[[str, dict], None] = _drop_item
    mid: int = 0


def _resolve(ctx: ApplyContext, raw) -> str:
    name = str(raw or "").strip()
    if not name:
        return ""
    return str(ctx.resolve_owner(name) or "").strip()


def _owner_for(ctx: ApplyContext, raw, module: str) -> str:
    """Каноническое имя владельца или "" — если строку надо отбросить."""
    owner = _resolve(ctx, raw)
    if owner and module in (ctx.user_only or ()) and owner != ctx.user_name:
        return ""
    return owner


def _int_or(value, default):
    n = _js_int(value)
    return default if n is None else n


def _skill_tombstones(state: dict, ctx: ApplyContext) -> set[tuple[str, str]]:
    out = set()
    for src in (ctx.config.get("deleted_skills") or [], state.get("deleted_skills") or []):
        for d in src:
            if isinstance(d, dict):
                owner, name = d.get("owner"), d.get("name")
            elif isinstance(d, (list, tuple)) and len(d) >= 2:
                owner, name = d[0], d[1]
            else:
                continue
            out.add((str(owner or ""), str(name or "").casefold()))
    return out


def _find_by_name(items: list[dict], name: str) -> int:
    """Индекс записи с таким именем: сначала точно, потом без учёта регистра."""
    for i, it in enumerate(items):
        if it.get("name") == name:
            return i
    low = name.casefold()
    for i, it in enumerate(items):
        if str(it.get("name") or "").casefold() == low:
            return i
    return -1


def _upsert_skill(state: dict, owner: str, name: str, level, desc, *, user: bool) -> None:
    skills = state["skills"].setdefault(owner, [])
    idx = _find_by_name(skills, name)
    level, desc = str(level or "").strip(), str(desc or "").strip()
    if idx >= 0:
        sk = skills[idx]
        # Пустой уровень/описание в повторной строке — «не менялось», а не
        # «стереть»: модель пишет навык целиком только при изучении.
        if level:
            sk["level"] = level
        if desc:
            sk["desc"] = desc
        if user:
            sk["user"] = True
    else:
        skills.append({"name": name, "level": level, "desc": desc, "user": user})


def _remove_skill(state: dict, owner: str, name: str) -> None:
    skills = state["skills"].get(owner)
    if not skills:
        return
    low = name.casefold()
    state["skills"][owner] = [s for s in skills if str(s.get("name") or "").casefold() != low]
    if not state["skills"][owner]:
        del state["skills"][owner]


# ---------- снаряжение ----------

def _owner_slot_cfg(config: dict, owner: str) -> tuple[list[dict] | None, set[str]]:
    """
    Слоты владельца из RPG_CONFIG.equipment.chars: (список слотов | None, удалённые).

    `slots` — слоты текущей формы; если их нет, берём слоты формы `form`.
    None — у персонажа нет настройки слотов вовсе (тогда слот любой).
    """
    eq = (config or {}).get("equipment") or {}
    chars = eq.get("chars") or {}
    cfg = chars.get(owner)
    if cfg is None:
        low = owner.casefold()
        cfg = next((v for k, v in chars.items() if str(k).casefold() == low), None)
    if not isinstance(cfg, dict):
        return None, set()
    deleted = {str(s) for s in (cfg.get("deleted_slots") or [])}
    slots = cfg.get("slots")
    if not isinstance(slots, list):
        forms = cfg.get("forms") or []
        form = next((f for f in forms if f.get("id") == cfg.get("form")), forms[0] if forms else None)
        slots = (form or {}).get("slots")
    if not isinstance(slots, list):
        return None, deleted
    return [s for s in slots if isinstance(s, dict) and s.get("name")], deleted


def _slot_max(slot: dict | None) -> int:
    if not slot:
        return 1
    n = _js_int(slot.get("max", slot.get("maxCount")))
    return n if n and n > 0 else 1


def _return_item(entry: dict, owner: str, ctx: ApplyContext) -> None:
    """Снятый предмет возвращается в инвентарь владельцу (как _returnItemFromEquip)."""
    info = dict(entry.get("item") or {})
    info["holder"] = owner
    info["location"] = ""
    if not info.get("icon"):
        info["icon"] = "📦"
    ctx.give_item(entry.get("name") or "", info)


def _unequip(state: dict, owner: str, slot: str, name: str, ctx: ApplyContext) -> None:
    slots = state["equipment"].get(owner)
    if not slots:
        return
    low_slot = str(slot or "").casefold()
    # Сначала названный слот, затем остальные: модель путает «Руки»/«Перчатки»,
    # а предмет по имени всё равно однозначен.
    order = sorted(slots, key=lambda s: 0 if s.casefold() == low_slot else 1)
    for s in order:
        items = slots[s]
        if not name:
            if s.casefold() != low_slot:
                continue
            removed, slots[s] = items, []
        else:
            idx = _find_by_name(items, name)
            if idx < 0:
                continue
            removed = [items.pop(idx)]
        for e in removed:
            _return_item(e, owner, ctx)
        if not slots[s]:
            del slots[s]
        break
    if not slots:
        del state["equipment"][owner]


def _equip(state: dict, owner: str, slot: str, name: str, attrs, ctx: ApplyContext,
           *, validate: bool) -> None:
    slot, name = str(slot or "").strip(), str(name or "").strip()
    if not (owner and slot and name):
        return
    eq_cfg = ctx.config.get("equipment") or {}
    slots_cfg, deleted = _owner_slot_cfg(ctx.config, owner)
    slot_cfg = None
    if slots_cfg:
        low = slot.casefold()
        slot_cfg = next((s for s in slots_cfg if str(s["name"]).casefold() == low), None)
        if slot_cfg is not None:
            slot = str(slot_cfg["name"])
    if validate:
        listed = slot_cfg is not None and slot not in deleted
        # Замок: ИИ не может заводить слоты. Без замка, но с настроенными
        # слотами — тоже только из списка (текущей формы).
        if eq_cfg.get("locked") and not listed:
            return
        if slots_cfg and not listed:
            return
    attrs = attrs if isinstance(attrs, dict) else _parse_attrs_text(str(attrs or ""))
    attrs = {str(k): _int_or(v, 0) for k, v in attrs.items()}
    owner_slots = state["equipment"].setdefault(owner, {})
    items = owner_slots.setdefault(slot, [])
    idx = _find_by_name(items, name)
    if idx >= 0:
        items[idx]["attrs"] = attrs
        return
    # Тот же предмет в другом слоте того же владельца — перенос, а не второй
    # экземпляр (плагин брал бы предмет из инвентаря заново и не находил).
    moved = None
    for other in list(owner_slots):
        if other == slot:
            continue
        j = _find_by_name(owner_slots[other], name)
        if j >= 0:
            moved = owner_slots[other].pop(j)
            if not owner_slots[other]:
                del owner_slots[other]
            break
    max_n = _slot_max(slot_cfg)
    while len(items) >= max_n:
        _return_item(items.pop(0), owner, ctx)
    if moved is not None:
        item = moved.get("item")
    else:
        taken = ctx.take_item(name)
        item = dict(taken) if taken else None
    items.append({"name": name, "attrs": attrs, "item": item})


# ---------- опорные пункты ----------

def _base_parts(path) -> list[str]:
    return [p.strip() for p in re.split(r"[>＞]", str(path or "")) if p.strip()]


def _new_base_id(state: dict) -> str:
    # Детерминированный id: состояние считается повтором на каждом запросе, и
    # случайный id (как sh_<rand> в плагине) менялся бы между запросами.
    top = 0
    for n in state["strongholds"]:
        m = re.match(r"^sh_(\d+)$", str(n.get("id") or ""))
        if m:
            top = max(top, int(m.group(1)))
    return f"sh_{top + 1}"


def _base_node(state: dict, path, *, create: bool, lift: bool) -> dict | None:
    """
    Узел по пути «A>B>C». create — достраивать недостающие узлы; lift — снять
    надгробия на пути (явная правка пользователя), иначе путь через удалённый
    пользователем узел блокируется (ИИ не воскрешает снесённое).
    """
    parts = _base_parts(path)
    if not parts:
        return None
    nodes = state["strongholds"]
    tombs = state.get("deleted_bases") or []
    parent = None
    for part in parts:
        parent_id = parent["id"] if parent else None
        parent_name = parent["name"] if parent else None
        tomb = [part, parent_name]
        if tomb in tombs:
            if not lift:
                return None
            tombs.remove(tomb)
        node = next((n for n in nodes if n.get("name") == part and n.get("parent") == parent_id), None)
        if node is None:
            low = part.casefold()
            node = next((n for n in nodes if str(n.get("name") or "").casefold() == low
                         and n.get("parent") == parent_id), None)
        if node is None:
            if not create:
                return None
            node = {"id": _new_base_id(state), "name": part, "level": None, "desc": "",
                    "parent": parent_id}
            nodes.append(node)
        parent = node
    return parent


def _set_base_field(node: dict, fld: str, value) -> None:
    if fld == "level":
        num = _js_int(value)
        node["level"] = num
    elif fld == "desc":
        node["desc"] = str(value if value is not None else "").strip()


def _delete_base(state: dict, path) -> None:
    node = _base_node(state, path, create=False, lift=False)
    if node is None:
        return
    doomed = {node["id"]}
    changed = True
    while changed:
        changed = False
        for n in state["strongholds"]:
            if n.get("parent") in doomed and n["id"] not in doomed:
                doomed.add(n["id"])
                changed = True
    parent = next((n for n in state["strongholds"] if n["id"] == node.get("parent")), None)
    state["strongholds"] = [n for n in state["strongholds"] if n["id"] not in doomed]
    tomb = [node["name"], parent["name"] if parent else None]
    tombs = state.setdefault("deleted_bases", [])
    if tomb not in tombs:
        tombs.append(tomb)


# ---------- валюта и репутация ----------

def _currency_deleted(name: str, entries, mid: int) -> bool:
    for e in entries or []:
        if isinstance(e, str):
            if e == name:
                return True
        elif isinstance(e, dict) and e.get("name") == name:
            at = e.get("at")
            # {name, at}: валюту удалили, когда последним было сообщение `at`;
            # история до `at` включительно для неё больше не считается.
            if at is None or mid <= at:
                return True
    return False


def _rep_categories(config: dict) -> dict[str, dict]:
    return {str(c["name"]).casefold(): c for c in ((config or {}).get("reputation") or [])
            if isinstance(c, dict) and c.get("name")}


def _clamp_rep(value: int, cfg: dict | None) -> int:
    lo = _int_or((cfg or {}).get("min"), -100)
    hi = _int_or((cfg or {}).get("max"), 100)
    return max(lo, min(hi, value))


def apply_changes(state: dict, changes: dict, ctx: ApplyContext) -> None:
    """
    Шаг повтора: изменения одного сообщения вливаются в RPG_STATE (spec_core §3.5).

    Порядок — как в _mergeRpgData: шкалы, статусы, навыки, атрибуты, снаряжение
    (сначала снятия, потом экипировка), репутация, уровни, опыт, валюта, базы.
    """
    if not isinstance(state, dict):
        return
    _ensure_state(state)
    ch = _normalize_changes(changes)
    if not has_changes(ch):
        return

    for raw, bars in ch["bars"].items():
        owner = _owner_for(ctx, raw, "bars")
        if not owner or not isinstance(bars, dict):
            continue
        dst = state["bars"].setdefault(owner, {})
        for key, val in bars.items():
            if isinstance(val, dict):
                cur, mx, label = val.get("cur"), val.get("max"), val.get("label")
            elif isinstance(val, (list, tuple)) and len(val) >= 2:
                cur, mx, label = val[0], val[1], (val[2] if len(val) > 2 else None)
            else:
                continue
            old = _bar_dict(dst.get(str(key).lower()))
            dst[str(key).lower()] = {
                "cur": _int_or(cur, old.get("cur", 0)),
                "max": _int_or(mx, old.get("max", 0)),
                # Подпись модель пишет лишь при первом упоминании шкалы.
                "label": str(label).strip() if label else old.get("label", ""),
            }

    for raw, effects in ch["status"].items():
        owner = _owner_for(ctx, raw, "bars")
        if owner:
            eff = effects if isinstance(effects, list) else _effects(str(effects or ""))
            state["status"][owner] = [str(e) for e in eff if str(e).strip()]

    tombs = _skill_tombstones(state, ctx)
    for sk in ch["skills"]:
        if not isinstance(sk, dict):
            continue
        owner = _owner_for(ctx, sk.get("owner"), "skills")
        name = str(sk.get("name") or "").strip()
        if not owner or not name or (owner, name.casefold()) in tombs:
            continue
        _upsert_skill(state, owner, name, sk.get("level"), sk.get("desc"), user=False)
    for sk in ch["skills_removed"]:
        if not isinstance(sk, dict):
            continue
        owner = _owner_for(ctx, sk.get("owner"), "skills")
        name = str(sk.get("name") or "").strip()
        if owner and name:
            _remove_skill(state, owner, name)

    for raw, vals in ch["attrs"].items():
        owner = _owner_for(ctx, raw, "attrs")
        if owner and isinstance(vals, dict):
            dst = state["attrs"].setdefault(owner, {})
            for k, v in vals.items():
                n = _js_int(v)
                if n is not None:
                    dst[str(k).lower()] = n

    for u in ch["unequip"]:
        if isinstance(u, dict):
            owner = _owner_for(ctx, u.get("owner"), "equipment")
            if owner:
                _unequip(state, owner, u.get("slot"), str(u.get("name") or "").strip(), ctx)
    for e in ch["equip"]:
        if isinstance(e, dict):
            owner = _owner_for(ctx, e.get("owner"), "equipment")
            if owner:
                _equip(state, owner, e.get("slot"), e.get("name"), e.get("attrs") or {}, ctx,
                       validate=True)

    cats = _rep_categories(ctx.config)
    for raw, vals in ch["reputation"].items():
        owner = _owner_for(ctx, raw, "reputation")
        if not owner or not isinstance(vals, dict):
            continue
        for cat, val in vals.items():
            cfg = cats.get(str(cat).strip().casefold())
            # Есть зарегистрированные категории — принимаем только их: иначе
            # модель плодит «Репутация в городе», «Слава в городе» и т. п.
            if cats and cfg is None:
                continue
            num = _js_int(val)
            if num is None:
                continue
            name = str(cfg["name"]) if cfg else str(cat).strip()
            dst = state["reputation"].setdefault(owner, {})
            entry = dst.get(name)
            if not isinstance(entry, dict):
                dst[name] = {"value": _clamp_rep(num, cfg), "sub": {}}
            elif not entry.get("user"):
                entry["value"] = _clamp_rep(num, cfg)

    for raw, val in ch["levels"].items():
        owner = _owner_for(ctx, raw, "level")
        num = _js_int(val)
        if owner and num is not None:
            state["levels"][owner] = num
    for raw, val in ch["xp"].items():
        owner = _owner_for(ctx, raw, "level")
        if owner and isinstance(val, (list, tuple)) and len(val) >= 2:
            state["xp"][owner] = [_int_or(val[0], 0), _int_or(val[1], 0)]

    denoms = {str(d["name"]).casefold(): str(d["name"])
              for d in (ctx.config.get("currencies") or []) if isinstance(d, dict) and d.get("name")}
    deleted_cur = ctx.config.get("deleted_currencies") or []
    for c in ch["currency"]:
        owner = _owner_for(ctx, c.get("owner"), "currency")
        name = str(c.get("name") or "").strip()
        num = _js_int(c.get("value"))
        if not owner or not name or num is None:
            continue
        if denoms:
            if name.casefold() not in denoms:
                continue
            name = denoms[name.casefold()]
        if _currency_deleted(name, deleted_cur, ctx.mid):
            continue
        coins = state["currency"].setdefault(owner, {})
        coins[name] = (coins.get(name, 0) + num) if c.get("delta") else num

    for bc in ch["base"]:
        if not isinstance(bc, dict):
            continue
        node = _base_node(state, bc.get("path"), create=True, lift=False)
        if node is not None:
            _set_base_field(node, bc.get("field"), bc.get("value"))


# ============================================================================
# Правки пользователя (design §9)
# ============================================================================

def apply_op(state: dict, op: dict, ctx: ApplyContext) -> None:
    """
    Правка пользователя rpg.* встаёт в повтор в свой момент (design §2).

    Фильтр «только пользователь» и проверки слотов здесь не действуют: правку
    сделал человек осознанно. Неизвестный kind пропускается — старый журнал не
    должен ломать повтор.
    """
    if not isinstance(state, dict) or not isinstance(op, dict):
        return
    _ensure_state(state)
    kind = str(op.get("kind") or "")
    owner = _resolve(ctx, op.get("owner"))

    if kind == "rpg.bar" and owner and op.get("key"):
        key = str(op["key"]).strip().lower()
        bars = state["bars"].setdefault(owner, {})
        if op.get("cur") is None and op.get("max") is None:
            bars.pop(key, None)
        else:
            old = _bar_dict(bars.get(key))
            cur = _int_or(op.get("cur"), old.get("cur", 0))
            bars[key] = {"cur": cur, "max": _int_or(op.get("max"), old.get("max", cur)),
                         "label": str(op.get("label") or old.get("label") or "")}
        if not bars:
            del state["bars"][owner]
    elif kind == "rpg.status" and owner:
        eff = op.get("effects")
        eff = eff if isinstance(eff, list) else _effects(str(eff or ""))
        state["status"][owner] = [str(e).strip() for e in eff if str(e).strip()]
    elif kind == "rpg.attr" and owner and op.get("key"):
        attrs = state["attrs"].setdefault(owner, {})
        num = _js_int(op.get("value"))
        if num is None:
            attrs.pop(str(op["key"]).lower(), None)
        else:
            attrs[str(op["key"]).lower()] = num
        if not attrs:
            del state["attrs"][owner]
    elif kind == "rpg.skill.add" and owner and str(op.get("name") or "").strip():
        name = str(op["name"]).strip()
        tombs = state.get("deleted_skills") or []
        state["deleted_skills"] = [t for t in tombs
                                   if not (t[0] == owner and str(t[1]).casefold() == name.casefold())]
        _upsert_skill(state, owner, name, op.get("level"), op.get("desc"), user=True)
    elif kind == "rpg.skill.delete" and owner and str(op.get("name") or "").strip():
        name = str(op["name"]).strip()
        _remove_skill(state, owner, name)
        tombs = state.setdefault("deleted_skills", [])
        if [owner, name] not in tombs:
            tombs.append([owner, name])
    elif kind == "rpg.level" and owner:
        num = _js_int(op.get("value"))
        if num is None:
            state["levels"].pop(owner, None)
        else:
            state["levels"][owner] = num
    elif kind == "rpg.xp" and owner:
        if op.get("cur") is None and op.get("max") is None:
            state["xp"].pop(owner, None)
        else:
            old = state["xp"].get(owner) or [0, 0]
            state["xp"][owner] = [_int_or(op.get("cur"), old[0]), _int_or(op.get("max"), old[1])]
    elif kind == "rpg.currency" and owner and str(op.get("name") or "").strip():
        name = str(op["name"]).strip()
        num = _js_int(op.get("value"))
        coins = state["currency"].setdefault(owner, {})
        if num is None:
            coins.pop(name, None)
        else:
            coins[name] = num
        if not coins:
            del state["currency"][owner]
    elif kind == "rpg.rep" and owner and str(op.get("cat") or "").strip():
        cat = str(op["cat"]).strip()
        cfg = _rep_categories(ctx.config).get(cat.casefold())
        if cfg:
            cat = str(cfg["name"])
        dst = state["reputation"].setdefault(owner, {})
        entry = dst.get(cat)
        if not isinstance(entry, dict):
            entry = dst[cat] = {"value": _js_int(entry) or 0, "sub": {}}
        num = _js_int(op.get("value"))
        if num is not None:
            entry["value"] = _clamp_rep(num, cfg)
        if isinstance(op.get("sub"), dict):
            entry["sub"] = dict(op["sub"])
        # Отметка «правил человек»: последующие значения ИИ её не перезапишут.
        entry["user"] = True
    elif kind == "rpg.equip" and owner:
        _equip(state, owner, op.get("slot"), op.get("name"), op.get("attrs") or {}, ctx,
               validate=False)
    elif kind == "rpg.unequip" and owner:
        _unequip(state, owner, op.get("slot"), str(op.get("name") or "").strip(), ctx)
    elif kind == "rpg.base":
        node = _base_node(state, op.get("path"), create=True, lift=True)
        if node is not None:
            if "level" in op:
                _set_base_field(node, "level", op.get("level"))
            if "desc" in op:
                _set_base_field(node, "desc", op.get("desc"))
    elif kind == "rpg.base.delete":
        _delete_base(state, op.get("path"))


# ============================================================================
# Рендер для блока состояния (spec_core §4 п.10)
# ============================================================================

def _fmt_num(v) -> str:
    """Число как в JS: 50, а не 50.0."""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def _all_owners(state: dict) -> list[str]:
    seen: dict[str, None] = {}
    for k in ("bars", "status", "skills", "attrs", "reputation", "equipment", "levels", "xp", "currency"):
        for name in (state.get(k) or {}):
            seen.setdefault(name, None)
    return list(seen)


def _present_allowed(present, owners: list[str], user_name: str) -> set[str]:
    """Кто из владельцев RPG сейчас в сцене (нечёткое совпадение, как в плагине)."""
    allowed: set[str] = set()
    names = set(owners)
    for p in present or []:
        n = str(p or "").strip()
        if not n:
            continue
        if n in names:
            allowed.add(n)
            continue
        if n == user_name and user_name in names:
            allowed.add(user_name)
            continue
        for rn in owners:
            if rn in n or n in rn:
                allowed.add(rn)
                break
    return allowed


def _bar_names(settings: dict) -> dict[str, str]:
    cfg = settings.get("rpg_bar_config")
    if cfg is None:
        cfg = default_bar_config()
    return {str(b.get("key")): str(b.get("name") or "") for b in cfg if isinstance(b, dict)}


def _stronghold_tree(nodes: list[dict], parent, depth: int, out: list[str]) -> None:
    for n in nodes:
        if n.get("parent") != parent:
            continue
        line = "  " * depth + str(n.get("name") or "")
        if n.get("level") is not None:
            line += f" Lv.{n['level']}"
        if n.get("desc"):
            line += f" — {n['desc']}"
        out.append(line)
        _stronghold_tree(nodes, n.get("id"), depth + 1, out)


def render_block(state: dict, settings: dict, *, present: list[str], npc_ids: dict[str, str],
                 user_name: str, config: dict) -> str:
    """
    Русские RPG-разделы блока состояния. Каждый раздел начинается с
    "\\n[Заголовок]" (пустая строка перед ним, как в плагине); "" — показать нечего.

    Фильтр присутствия: если кто-то из владельцев RPG есть в сцене (или включён
    rpg_strict_present), показываются только они — RPG всех NPC чата раздувал бы
    промпт на каждом ходу. Заголовок раздела без строк не выводится (в плагине
    выводился пустой «[RPG-статус]», когда все владельцы отфильтрованы).
    """
    if not isinstance(state, dict):
        return ""
    s = settings or {}
    cfg = config or {}
    npc_ids = npc_ids or {}
    uo = _user_only_set(s)
    allowed = _present_allowed(present, _all_owners(state), user_name)
    filtered = bool(allowed) or bool(s.get("rpg_strict_present"))

    def visible(name: str, module: str) -> bool:
        if module in uo and name != user_name:
            return False
        return not filtered or name in allowed

    def prefix(name: str, module: str) -> str:
        if module in uo:
            return ""
        nid = npc_ids.get(name)
        return f"N{nid} {name}: " if nid else f"{name}: "

    lines: list[str] = []

    def section(title: str, body: list[str]) -> None:
        if body:
            lines.append(f"\n[{title}]")
            lines.extend(body)

    bars = state.get("bars") or {}
    status = state.get("status") or {}
    if _module_on(s, "bars"):
        names = _bar_names(s)
        body = []
        for name, owner_bars in bars.items():
            if not visible(name, "bars"):
                continue
            parts = []
            for key, val in (owner_bars or {}).items():
                val = _bar_dict(val)
                if not val:
                    continue
                label = (val.get("label") or names.get(key) or key.upper())
                parts.append(f"{label} {_fmt_num(val.get('cur', 0))}/{_fmt_num(val.get('max', 0))}")
            if status.get(name):
                parts.append("статус:" + "/".join(status[name]))
            if parts:
                body.append(prefix(name, "bars") + " | ".join(parts))
        for name, effects in status.items():
            if name in bars or not effects or not visible(name, "bars"):
                continue
            body.append(prefix(name, "bars") + "статус:" + "/".join(effects))
        section("RPG-статус", body)

    if _module_on(s, "skills"):
        body = []
        for name, skills in (state.get("skills") or {}).items():
            if not skills or not visible(name, "skills"):
                continue
            if "skills" not in uo:
                body.append(prefix(name, "skills").rstrip())
            for sk in skills:
                lv = f" {sk['level']}" if sk.get("level") else ""
                desc = f" | {sk['desc']}" if sk.get("desc") else ""
                body.append(f"  {sk.get('name', '')}{lv}{desc}")
        section("Список навыков", body)

    attr_cfg = s.get("rpg_attr_config")
    if attr_cfg is None:
        attr_cfg = default_attr_config()
    attr_cfg = [a for a in attr_cfg if isinstance(a, dict) and a.get("key")]
    if _module_on(s, "attrs") and attr_cfg:
        body = []
        for name, vals in (state.get("attrs") or {}).items():
            if not visible(name, "attrs"):
                continue
            vals = vals or {}
            # Владелец без единого настроенного атрибута дал бы строку из «?».
            if not any(a["key"] in vals for a in attr_cfg):
                continue
            parts = [f"{a.get('name') or a['key']}{_fmt_num(vals.get(a['key'], '?'))}" for a in attr_cfg]
            body.append(prefix(name, "attrs") + " | ".join(parts))
        section("Атрибуты", body)

    if _module_on(s, "equipment"):
        body = []
        for name, slots in (state.get("equipment") or {}).items():
            if not visible(name, "equipment"):
                continue
            slots_cfg, deleted = _owner_slot_cfg(cfg, name)
            valid = {str(x["name"]) for x in slots_cfg} if slots_cfg else set()
            parts = []
            for slot_name, items in (slots or {}).items():
                # Снаряжение неактивной формы (слота нет в текущей) не показываем.
                if slot_name in deleted or (valid and slot_name not in valid):
                    continue
                for it in items or []:
                    attrs = ",".join(f"{k}{'+' if v >= 0 else ''}{v}"
                                     for k, v in (it.get("attrs") or {}).items())
                    desc = ((it.get("item") or {}).get("description") or "").strip()
                    parts.append(f"[{slot_name}]{it.get('name', '')}"
                                 + (f"{{{attrs}}}" if attrs else "")
                                 + (f' "{desc}"' if desc else ""))
            if parts:
                body.append(prefix(name, "equipment") + " | ".join(parts))
        section("Снаряжение", body)

    if _module_on(s, "reputation"):
        cats = _rep_categories(cfg)
        body = []
        for name, owner_cats in (state.get("reputation") or {}).items():
            if not visible(name, "reputation"):
                continue
            parts = []
            for cat, data in (owner_cats or {}).items():
                if cats and cat.casefold() not in cats:
                    continue
                value = data.get("value") if isinstance(data, dict) else data
                sub = data.get("sub") if isinstance(data, dict) else None
                sub_text = " / ".join(
                    f"{k}:{'+' if isinstance(v, (int, float)) and v > 0 else ''}{_fmt_num(v)}"
                    for k, v in (sub or {}).items() if v not in (None, ""))
                parts.append(f"{cat}:{_fmt_num(value)}" + (f"（{sub_text}）" if sub_text else ""))
            if parts:
                body.append(prefix(name, "reputation") + " | ".join(parts))
        section("Репутация", body)

    if _module_on(s, "level"):
        levels, xp = state.get("levels") or {}, state.get("xp") or {}
        body = []
        for name in dict.fromkeys([*levels, *xp]):
            if not visible(name, "level"):
                continue
            lv, x = levels.get(name), xp.get(name)
            if lv is None and not x:
                continue
            text = f"Lv.{lv}" if lv is not None else ""
            if x:
                text += f" (опыт: {x[0]}/{x[1]})"
            body.append(prefix(name, "level") + text.strip())
        section("Уровень", body)

    if _module_on(s, "currency"):
        denoms = [str(d["name"]) for d in (cfg.get("currencies") or [])
                  if isinstance(d, dict) and d.get("name")]
        body = []
        for name, coins in (state.get("currency") or {}).items():
            if not visible(name, "currency"):
                continue
            coins = coins or {}
            # Без настроенных валют показываем всё, что есть (в плагине раздел
            # тогда пропадал целиком, хотя данные копились).
            order = denoms or list(coins)
            parts = [f"{d}×{_fmt_num(coins[d])}" for d in order if coins.get(d) is not None]
            if parts:
                body.append(prefix(name, "currency") + ", ".join(parts))
        section("Валюта", body)

    if _module_on(s, "stronghold"):
        body: list[str] = []
        _stronghold_tree(state.get("strongholds") or [], None, 0, body)
        section("Опорный пункт", body)

    return "\n".join(lines)


# ============================================================================
# Правила формата (порт русской ветки getDefaultRpgPrompt)
# ============================================================================

_SECTION_KEYS = ("header", "bars", "attrs", "skills", "equipment", "reputation", "level",
                 "currency", "stronghold")

DEFAULT_RPG_TEMPLATE = "\n\n".join(f"[[rpg.{k}]]" for k in _SECTION_KEYS)

_OWN = "владелец"


def _bar_definition(bar: dict) -> str:
    meta = []
    if bar.get("min") is not None and bar.get("defaultMax") is not None:
        # Конфиг в формате плагина: min/max — диапазон, defaultMax — максимум новой шкалы.
        meta.append(f"{bar['min']}~{bar.get('max', 100)}")
        meta.append(f"макс. по умолчанию {bar['defaultMax']}")
    elif bar.get("max") is not None:
        meta.append(f"макс. по умолчанию {bar['max']}")
    if bar.get("required") is False:
        meta.append("необязательно")
    if bar.get("desc"):
        meta.append(str(bar["desc"]))
    name = bar.get("name") or str(bar.get("key", "")).upper()
    return f"{bar.get('key')}({name}" + (f": {'; '.join(meta)}" if meta else "") + ")"


def _slot_text(slot: dict) -> str:
    n = _slot_max(slot)
    return f"{slot['name']}(×{n}: {slot['desc']})" if slot.get("desc") else f"{slot['name']}(×{n})"


def _rate_text(denoms: list[dict]) -> str:
    if len(denoms) < 2:
        return ""
    def rate(d):
        r = _js_int(d.get("rate"))
        return r if r and r > 0 else 1
    ordered = sorted(denoms, key=rate)
    base = rate(ordered[0])
    return " = ".join(f"{_fmt_num(rate(d) / base)}{d['name']}" for d in ordered)


def _form_text(char_cfg: dict) -> str:
    forms = char_cfg.get("forms") or []
    form_id = char_cfg.get("form")
    if not form_id or not forms:
        return ""
    form = next((f for f in forms if f.get("id") == form_id), None)
    return f" текущая форма:{(form or {}).get('name') or form_id}"


def rules_sections(settings: dict, *, user_name: str, present: list[str], config: dict,
                   state: dict) -> dict[str, str]:
    """
    Разделы правил <horaerpg> (русская ветка getDefaultRpgPrompt).

    Ключи: header, bars, attrs, skills, equipment, reputation, level, currency,
    stronghold. Пустая строка — модуль выключен (или ему нечего сказать:
    репутация без категорий, валюта без номиналов). Разделы без ведущих и
    хвостовых переводов строк; склеивает их fill_rpg_prompt.
    """
    s = settings or {}
    cfg = config or {}
    user = (user_name or "").strip() or "{{user}}"
    out = {k: "" for k in _SECTION_KEYS}
    enabled = {m: _module_on(s, m) for m in MODULES}
    if not any(enabled.values()):
        return out
    uo = _user_only_set(s)
    owner_mods = [m for m in OWNER_MODULES if enabled[m]]
    uo_on = [m for m in owner_mods if m in uo]

    header = "═══ [RPG] ═══\nВаш ответ ДОЛЖЕН включать тег <horaerpg> в конце."
    # «Все модули только пользователя» считаем по включённым модулям: в плагине
    # нужны были все семь флагов, и при двух включённых модулях модель зря
    # получала формат владельца.
    if owner_mods and len(uo_on) == len(owner_mods):
        header += (f"\nВсе RPG-данные отслеживают только {user}. Формат не содержит поля "
                   f"владельца. НЕ выводите RPG-строки для NPC.")
    elif owner_mods:
        header += f"\nФормат владельца следует нумерации NPC: N## полное имя. {user} пишется напрямую без N."
        if uo_on:
            header += f" Некоторые модули отслеживают только {user} (отмечено ниже)."
    out["header"] = header
    uo_mark = f"  (только {user}; поле владельца не пишется)"

    if enabled["bars"]:
        bar_cfg = s.get("rpg_bar_config")
        if bar_cfg is None:
            bar_cfg = default_bar_config()
        bar_cfg = [b for b in bar_cfg if isinstance(b, dict) and b.get("key")]
        lines = ["[Шкалы статуса — обязательны каждый ход, пропуск = провал!]"]
        if "bars" in uo:
            lines.append(f"Выводите только шкалы статуса и состояние {user}:")
            for b in bar_cfg:
                lines.append(f"  ✅ {b['key']}:текущее/макс({b.get('name') or b['key'].upper()})"
                             "  ← при первом использовании укажите отображаемое имя")
            lines.append("  ✅ status:эффект1/эффект2  ← если нет отклонений, пишите нормально")
        else:
            lines.append("НЕОБХОДИМО вывести ВСЕ шкалы статуса и состояние для КАЖДОГО "
                         "присутствующего персонажа в списке characters:")
            for b in bar_cfg:
                lines.append(f"  ✅ {b['key']}:{_OWN}=текущее/макс({b.get('name') or b['key'].upper()})"
                             "  ← при первом использовании укажите отображаемое имя")
            lines.append(f"  ✅ status:{_OWN}=эффект1/эффект2  ← если нет отклонений, пишите =нормально")
        lines.append("Правила:")
        if bar_cfg:
            lines.append("  Зарегистрированные шкалы статуса: "
                         + ", ".join(_bar_definition(b) for b in bar_cfg))
        lines.append("  - Бой/ранение/заклинание/расход → обоснованное уменьшение; "
                     "восстановление/отдых → обоснованное увеличение")
        if "bars" not in uo:
            lines.append("  - Каждая шкала каждого присутствующего персонажа ДОЛЖНА быть записана; "
                         "пропуск кого-либо = провал")
        lines.append("  - Даже если значения не изменились в этом ходу, НЕОБХОДИМО записать текущие значения")
        out["bars"] = "\n".join(lines)

    attr_cfg = s.get("rpg_attr_config")
    if attr_cfg is None:
        attr_cfg = default_attr_config()
    attr_cfg = [a for a in attr_cfg if isinstance(a, dict) and a.get("key")]
    if enabled["attrs"] and attr_cfg:
        kv = "|".join(f"{a['key']}=значение" for a in attr_cfg)
        lines = ["[Многомерные атрибуты] Записывайте только при первом появлении или изменении; "
                 "пропускайте, если без изменений"]
        lines.append(f"  attr:{kv}" if "attrs" in uo else f"  attr:{_OWN}|{kv}")
        if "attrs" in uo:
            lines.append(uo_mark)
        meaning = ", ".join(
            f"{a['key']}({a.get('name') or a['key']}: {a['desc']})" if a.get("desc")
            else f"{a['key']}({a.get('name') or a['key']})" for a in attr_cfg)
        lines.append(f"  Диапазон значений 0-100. Значения атрибутов: {meaning}")
        out["attrs"] = "\n".join(lines)

    if enabled["skills"]:
        lines = ["[Навыки] Записывайте только при изучении/повышении/потере; пропускайте, если без изменений"]
        if "skills" in uo:
            lines += ["  skill:название навыка|уровень|описание эффекта", "  skill-:название навыка", uo_mark]
        else:
            lines += [f"  skill:{_OWN}|название навыка|уровень|описание эффекта",
                      f"  skill-:{_OWN}|название навыка"]
        out["skills"] = "\n".join(lines)

    if enabled["equipment"]:
        eq_cfg = cfg.get("equipment") or {}
        chars = eq_cfg.get("chars") or {}
        present_set = {str(p).strip() for p in (present or []) if str(p).strip()}
        slot_lines = []
        if "equipment" in uo:
            ucfg = chars.get(user_name) if user_name else None
            slots, _ = _owner_slot_cfg(cfg, user_name) if user_name else (None, set())
            if slots:
                slot_lines.append(f"  Слоты{_form_text(ucfg or {})}: " + ", ".join(_slot_text(x) for x in slots))
        else:
            for owner, ccfg in chars.items():
                slots, _ = _owner_slot_cfg(cfg, owner)
                if not slots or (present_set and owner not in present_set):
                    continue
                slot_lines.append(f"  {owner} слоты{_form_text(ccfg or {})}: "
                                  + ", ".join(_slot_text(x) for x in slots))
        # Замок без единого слота — любая строка equip отвергается, правило
        # только сбивало бы модель.
        has_slots = any(_owner_slot_cfg(cfg, o)[0] for o in chars)
        if not (eq_cfg.get("locked") and not has_slots):
            lines = ["[Снаряжение] Записывайте при экипировке/снятии; пропускайте, если без изменений"]
            if "equipment" in uo:
                lines += ["  equip:слот|предмет|стат1=значение,стат2=значение",
                          "  unequip:слот|предмет", uo_mark]
            else:
                lines += [f"  equip:{_OWN}|слот|предмет|стат1=значение,стат2=значение",
                          f"  unequip:{_OWN}|слот|предмет"]
            lines += slot_lines
            lines += [
                "  ⚠ Каждый персонаж может использовать только слоты текущей формы. "
                "Значения характеристик — целые числа.",
                "  ⚠ При смене формы несовместимое снаряжение неактивно; не снимайте его "
                "автоматически и не возвращайте в инвентарь.",
                "  ⚠ Обычная одежда без зачарования или особых материалов НЕ должна иметь "
                "высоких значений характеристик.",
            ]
            if eq_cfg.get("locked"):
                lines.append("  ⚠ Слоты снаряжения заблокированы: не создавайте новые слоты; "
                             "отсутствующие в списке будут проигнорированы.")
            out["equipment"] = "\n".join(lines)

    cats = [c for c in (cfg.get("reputation") or []) if isinstance(c, dict) and c.get("name")]
    if enabled["reputation"] and cats:
        lines = ["[Репутация] Записывайте только при изменении репутации; пропускайте, если без изменений"]
        if "reputation" in uo:
            lines += ["  rep:категория=текущее значение", uo_mark]
        else:
            lines.append(f"  rep:{_OWN}|категория=текущее значение")
        lines.append("  Зарегистрированные категории репутации:")
        for c in cats:
            meta = [f"{_int_or(c.get('min'), -100)}~{_int_or(c.get('max'), 100)}"]
            if c.get("default") is not None:
                meta.append(f"по умолчанию {c['default']}")
            if c.get("sub"):
                meta.append("подэлементы:" + ", ".join(str(x) for x in c["sub"]))
            lines.append(f"  - {c['name']}（{'; '.join(meta)}）")
        lines.append("  ⚠ НЕ создавайте новые категории репутации. "
                     "Используйте только зарегистрированные названия выше.")
        out["reputation"] = "\n".join(lines)

    if enabled["level"]:
        lines = ["[Уровень и опыт] Записывайте только при повышении/понижении уровня или "
                 "изменении опыта; пропускайте, если без изменений"]
        if "level" in uo:
            lines += ["  level:число уровня", "  xp:текущий опыт/необходимо для повышения", uo_mark]
        else:
            lines += [f"  level:{_OWN}=число уровня", f"  xp:{_OWN}=текущий опыт/необходимо для повышения"]
        lines += [
            "  Справка по получению опыта:",
            "  - Испытание близкое к уровню персонажа или выше: больше опыта (10~50+)",
            "  - Разница уровней ≥10, тривиальное испытание: только 1 очко опыта",
            "  - Повседневные действия/диалог/исследование: немного опыта (1~5)",
            "  - Необходимый опыт растёт с уровнем: рекомендуемая формула = уровень × 100",
        ]
        out["level"] = "\n".join(lines)

    denoms = [d for d in (cfg.get("currencies") or []) if isinstance(d, dict) and d.get("name")]
    if enabled["currency"] and denoms:
        d0 = denoms[0]["name"]
        who = "" if "currency" in uo else f"{user}|"
        lines = ["[Валюта — ОБЯЗАТЕЛЬНО записывать при любой сделке/подборе/трате!]"]
        if "currency" in uo:
            lines.append("Формат: currency:валюта=±сумма")
        else:
            lines.append(f"Формат: currency:{_OWN}|валюта=±сумма")
        lines += ["Примеры:", f"  currency:{who}{d0}=+10", f"  currency:{who}{d0}=-3"]
        if len(denoms) > 1:
            lines.append(f"  currency:{who}{denoms[1]['name']}=+50")
        if "currency" in uo:
            lines += ["Абсолютное значение тоже допустимо: currency:валюта=количество", uo_mark.strip()]
        else:
            lines.append(f"Абсолютное значение тоже допустимо: currency:{_OWN}|валюта=количество")
        lines.append("Зарегистрированные валюты: " + ", ".join(str(d["name"]) for d in denoms))
        rate = _rate_text(denoms)
        if rate:
            lines.append(f"Курсы обмена: {rate}")
        lines.append("⚠ НЕ используйте незарегистрированные названия валют. Любое действие с деньгами "
                     "(покупка/продажа/подбор/награда/кража) ДОЛЖНО содержать строку currency.")
        out["currency"] = "\n".join(lines)

    if enabled["stronghold"]:
        lines = [
            "[Крепости] Записывайте при изменении статуса крепости (улучшение/строительство/"
            "разрушение/обновление описания); пропускайте, если без изменений. Существующие "
            "крепости ДОЛЖНЫ использовать точно такие же названия, как в списке ниже — "
            "сокращения, переименования и варианты с префиксами запрещены",
            "Формат: base:путь крепости=уровень или base:путь крепости|desc=описание",
            "Используйте > для разделения уровней иерархии",
            "Примеры:",
            "  base:Поместье героя=3",
            "  base:Поместье героя>Кузница>Печь=2",
            "  base:Поместье героя|desc=Каменное поместье в речной долине со стенами и сторожевой башней",
        ]
        nodes = (state or {}).get("strongholds") or []
        roots = [n for n in nodes if not n.get("parent")]
        if roots:
            summary = []
            for r in roots:
                kids = [str(k.get("name")) for k in nodes if k.get("parent") == r.get("id")]
                summary.append(f"{r.get('name')}" + (f" Lv.{r['level']}" if r.get("level") is not None else "")
                               + (f"({', '.join(kids)})" if kids else ""))
            lines.append("Текущие крепости: " + "; ".join(summary))
        out["stronghold"] = "\n".join(lines)
    return out


_PLACEHOLDER_RE = re.compile(
    r"\[\[\s*rpg\.(full|header|bars|attrs|skills|equipment|reputation|level|currency|stronghold)\s*\]\]",
    re.I)


def fill_rpg_prompt(template: str, sections: dict[str, str]) -> str:
    """
    Подставляет [[rpg.*]] в шаблон промпта RPG (свой или DEFAULT_RPG_TEMPLATE).

    [[rpg.full]] — все непустые разделы подряд. Пустые разделы выключенных
    модулей оставляют пустые строки — 3+ перевода строки схлопываются в два.
    """
    secs = sections or {}
    full = "\n\n".join(secs.get(k, "").strip() for k in _SECTION_KEYS if (secs.get(k) or "").strip())

    def sub(m: re.Match) -> str:
        key = m.group(1).lower()
        return full if key == "full" else (secs.get(key) or "").strip()

    text = _PLACEHOLDER_RE.sub(sub, template or "")
    return re.sub(r"\n{3,}", "\n\n", text).strip()


# ============================================================================
# Документ для поиска и значки статусов
# ============================================================================

def document_lines(changes: dict) -> list[str]:
    """
    Русские строки RPG-событий сообщения для документа вспоминания.

    Шкалы и статусы сюда не идут: они меняются каждый ход и забили бы поиск
    шумом; в память попадает то, что можно спросить («когда он надел кольцо?»).
    """
    ch = _normalize_changes(changes)
    out: list[str] = []
    for owner, lv in ch["levels"].items():
        out.append(f"{owner} уровень {lv}")
    for sk in ch["skills"]:
        if isinstance(sk, dict) and sk.get("name"):
            lv = f" {sk['level']}" if sk.get("level") else ""
            out.append(f"{sk.get('owner', '')} навык {sk['name']}{lv}".strip())
    for e in ch["equip"]:
        if isinstance(e, dict) and e.get("name"):
            out.append(f"{e.get('owner', '')} экипировал {e['name']} ({e.get('slot', '')})".strip())
    for u in ch["unequip"]:
        if isinstance(u, dict) and u.get("name"):
            out.append(f"{u.get('owner', '')} снял {u['name']} ({u.get('slot', '')})".strip())
    for b in ch["base"]:
        if not isinstance(b, dict) or not b.get("path"):
            continue
        if b.get("field") == "level":
            out.append(f"база {b['path']} уровень {b.get('value')}")
        elif b.get("value"):
            out.append(f"база {b['path']}: {b['value']}")
    return out


# Ключевые слова → эмодзи. Порядок важен: «тяжело ранен» раньше «ранен»,
# «отравл» раньше общего «слаб». Кириллицу и латиницу ищем только с начала
# слова: простая подстрока находила «яд» во «взгляде» и «норма» в
# «ненормальном». Иероглифы — подстрокой (в китайском нет пробелов).
_STATUS_ICONS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("при смерти", "смертельн", "тяжело ранен", "тяжёлое ранение", "dying", "濒死", "瀕死",
      "重伤", "重傷"), "💀"),
    (("окамен", "petrif", "石化"), "🗿"),
    (("яд", "отрав", "токсин", "poison", "venom", "毒", "腐蚀", "腐蝕"), "☠️"),
    (("кровотеч", "кровоточ", "кровопотер", "bleed", "流血", "出血"), "🩸"),
    (("ожог", "горит", "горение", "пылает", "огонь", "burn", "fire", "火", "烧", "燒", "灼", "燃", "炎"), "🔥"),
    (("заморож", "замерз", "обморож", "озноб", "лёд", "freez", "frozen", "chill",
      "冻", "凍", "冰", "寒"), "❄️"),
    (("оглуш", "контуж", "головокруж", "stun", "daze", "dizz", "眩", "晕", "暈", "昏"), "💫"),
    (("паралич", "парализ", "онемен", "paraly", "麻", "痹", "痺"), "⚡"),
    (("сон", "спит", "усыпл", "дремот", "sleep", "asleep", "眠", "睡"), "💤"),
    (("страх", "испуг", "ужас", "паник", "fear", "terrif", "panic", "恐", "惧", "懼", "惊", "驚"), "😱"),
    (("ярост", "бешенств", "гнев", "берсерк", "rage", "fury", "berserk", "狂暴", "愤怒", "憤怒", "暴怒"), "😡"),
    (("смятен", "замешат", "спутан", "confus", "混乱", "混亂", "乱", "亂"), "😵"),
    (("слеп", "ослеп", "blind", "盲", "失明"), "🙈"),
    (("немот", "безмолв", "молчан", "silence", "mute", "沉默", "禁言"), "🔇"),
    (("замедл", "slow", "减速", "減速", "迟缓", "遲緩"), "🐌"),
    (("оков", "связан", "скован", "опутан", "обездвиж", "bound", "root", "缚", "縛", "禁锢", "禁錮"), "⛓️"),
    (("голод", "hunger", "hungry", "starv", "饥", "飢", "饿", "餓"), "🍖"),
    (("жажд", "обезвож", "thirst", "dehydr", "渴", "脱水", "脫水"), "💧"),
    (("опьян", "пьян", "drunk", "intoxic", "醉"), "🍺"),
    (("болезн", "болен", "лихорад", "простуд", "disease", "sick", "fever", "病"), "🤒"),
    (("проклят", "curse", "诅咒", "詛咒"), "🧿"),
    (("очарован", "зачарован", "charm", "魅惑"), "💘"),
    (("устал", "утомл", "истощ", "изнур", "fatigue", "tired", "exhaust", "疲", "累", "倦", "乏"), "😩"),
    (("ранен", "рана", "травм", "ушиб", "перелом", "wound", "injur", "hurt", "伤", "傷", "创", "創"), "🩹"),
    (("слаб", "ослабл", "weak", "弱", "衰", "虚", "虛"), "🥀"),
    (("регенер", "исцел", "лечен", "восстанов", "regen", "heal", "愈", "恢复", "恢復", "再生"), "💚"),
    (("невидим", "скрыт", "маскир", "stealth", "invisib", "hidden", "隐", "隱", "潜行", "潛行", "伪装", "偽裝"), "👻"),
    (("щит", "защит", "барьер", "shield", "barrier", "护盾", "護盾", "防御", "防禦"), "🛡️"),
    (("благослов", "воодушев", "bless", "inspir", "祝福", "神圣", "神聖"), "✨"),
    (("ускор", "haste", "加速"), "💨"),
    (("норма", "в порядке", "normal", "正常"), "✅"),
)


def _icon_pattern(words: tuple[str, ...]) -> re.Pattern:
    parts = []
    for w in words:
        esc = re.escape(w.casefold())
        parts.append(r"(?<![a-zа-яё0-9])" + esc if re.match(r"[a-zа-яё]", w.casefold()) else esc)
    return re.compile("|".join(parts))


_STATUS_ICON_PATTERNS = tuple((_icon_pattern(words), icon) for words, icon in _STATUS_ICONS)


def status_icon(effect: str) -> str:
    """Эмодзи для статуса (для HUD и панели RPG); незнакомый статус — «•»."""
    text = str(effect or "").casefold()
    if not text:
        return "•"
    for pattern, icon in _STATUS_ICON_PATTERNS:
        if pattern.search(text):
            return icon
    return "•"
