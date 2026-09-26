"""
Horae State Engine — ядро: формат меты сообщения, разбор тегов, повтор
(агрегация) состояния чата, журнал правок пользователя, рендер блока
состояния и хронологии для промпта.

Порт `core/horaeManager.js` плагина Horae (SillyTavern). Модуль чистый: ни БД,
ни FastAPI, ни litellm — всё проверяется тестами без сети.

Как это работает. Модель в конце каждого ответа пишет служебные теги:

    <horae>
    time:2026/2/4 15:00
    location:Таверна·зал
    characters:Вольф, Марина
    costume:Вольф=кожаная куртка
    item!:🗝Ключ от подвала|ржавый=Марина@карман фартука
    npc:Вольф|серая шерсть=молчалив@постоянный гость {{user}}~age:35
    affection:Вольф=40
    </horae>
    <horaeevent>
    event:important|Марина отдала Вольфу ключ от подвала
    </horaeevent>

parse_reply вырезает их из текста (в чате и в истории их нет) и возвращает
мету. replay проигрывает меты всех сообщений по порядку плюс журнал правок
пользователя и получает текущее состояние. render_state_block превращает его
в компактный блок, который на каждом ходу уходит в хвост промпта.

Отличия от плагина (исправленные ошибки) отмечены в комментариях по месту.
"""
from __future__ import annotations

import copy
import re
from datetime import datetime, timezone

from backend import horae_rpg, horae_tables, horae_time

LEVELS = ("normal", "important", "critical")
LEVEL_MARK = {"critical": "★", "important": "●", "normal": "○"}
LEVEL_RU = {"critical": "ключевое", "important": "важное", "normal": "обычное"}

META_KEYS = (
    "time", "scene", "costumes", "mood", "items", "items_removed", "events",
    "affection", "npcs", "agenda", "agenda_done", "relationships", "tables", "rpg",
)
NPC_FIELDS = ("appearance", "personality", "relationship", "gender", "age", "race",
              "job", "birthday", "note")
# Поля NPC, которые ИИ может перезаписать, и «защищённые» — только если пусто.
# Защищённые — то, что у живого человека не меняется: плагин не давал модели
# «переиграть» пол или расу персонажа случайной строкой через сто ходов.
_NPC_UPDATABLE = ("appearance", "personality", "relationship", "age", "job", "note")
_NPC_PROTECTED = ("gender", "race", "birthday")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def empty_meta() -> dict:
    """Пустая мета сообщения (все ключи — см. дизайн §4.2)."""
    return {
        "time": {"date": "", "time": ""},
        "scene": {"location": "", "atmosphere": "", "characters": [], "desc": []},
        "costumes": {}, "mood": {}, "items": {}, "items_removed": [], "events": [],
        "affection": {}, "npcs": {}, "agenda": [], "agenda_done": [],
        "relationships": [], "tables": [], "rpg": None,
    }


def meta_has_data(meta) -> bool:
    """Есть ли в мете хоть что-то содержательное."""
    if not isinstance(meta, dict):
        return False
    t = meta.get("time") or {}
    sc = meta.get("scene") or {}
    return bool(
        t.get("date") or t.get("time") or sc.get("location") or sc.get("characters")
        or sc.get("desc") or sc.get("atmosphere")
        or any(meta.get(k) for k in (
            "costumes", "mood", "items", "items_removed", "events", "affection", "npcs",
            "agenda", "agenda_done", "relationships", "tables"))
        or (meta.get("rpg") and horae_rpg.has_changes(meta.get("rpg")))
    )


def meta_has_events(meta) -> bool:
    return bool(isinstance(meta, dict) and any(
        (e or {}).get("text") for e in (meta.get("events") or [])))


# ===========================================================================
# Разбор тегов
# ===========================================================================

class ParseContext:
    """Что нужно разбору кроме текста: имя пользователя (для RPG «только
    пользователь») и теги, которые перед разбором вырезаются целиком."""

    def __init__(self, user_name: str = "", user_only=(), strip_tags: str = ""):
        self.user_name = user_name or "Пользователь"
        self.user_only = frozenset(user_only or ())
        self.strip_tags = strip_tags or ""


_THINK_RE = re.compile(r"<think(?:ing)?\b[^>]*>[\s\S]*?</think(?:ing)?>", re.IGNORECASE)
_HORAE_RE = re.compile(r"<horae>([\s\S]*?)</horae>", re.IGNORECASE)
_HORAE_COMMENT_RE = re.compile(r"<!--\s*horae([\s\S]*?)-->", re.IGNORECASE)
_EVENT_RE = re.compile(r"<horaeevent>([\s\S]*?)</horaeevent>", re.IGNORECASE)
_RPG_RE = re.compile(r"<horaerpg>([\s\S]*?)</horaerpg>", re.IGNORECASE)
# Все служебные блоки разом — для вырезания из текста.
_ALL_BLOCKS_RE = re.compile(
    r"<horae>[\s\S]*?</horae>|<!--\s*horae[\s\S]*?-->|<horaeevent>[\s\S]*?</horaeevent>"
    r"|<horaerpg>[\s\S]*?</horaerpg>"
    r"|<horaetable\s*[:：][^>]*>[\s\S]*?</horaetable(?:\s*[:：][^>]*)?>",
    re.IGNORECASE,
)
# Незакрытый хвостовой блок (ответ оборван или ещё стримится).
_OPEN_TAIL_RE = re.compile(r"<(?:horae|horaeevent|horaerpg|horaetable\s*[:：][^>]*)>(?![\s\S]*</horae)[\s\S]*$",
                           re.IGNORECASE)
_LINE_RE = re.compile(r"^\s*([A-Za-z_]+(?:!{1,2}|-)?)\s*[:：]\s*(.*?)\s*$")
_FIELD_LINE_KEYS = frozenset({
    "time", "location", "atmosphere", "scene_desc", "characters", "costume", "item",
    "item!", "item!!", "item-", "event", "affection", "npc", "agenda", "agenda-", "rel", "mood",
})


def _outside_think(text: str, fn) -> str:
    """Применить fn только к частям текста ВНЕ <think>: рассуждения модели
    часто цитируют формат тегов, и плагин их не трогал (превращал в ‹horae›)."""
    out = []
    pos = 0
    for m in _THINK_RE.finditer(text):
        out.append(fn(text[pos:m.start()]))
        out.append(m.group(0))
        pos = m.end()
    out.append(fn(text[pos:]))
    return "".join(out)


def _without_think(text: str) -> str:
    return _THINK_RE.sub("", text or "")


def _strip_custom(text: str, tags: str) -> str:
    """Вырезать блоки тегов из настройки strip_tags (мини-театр, заметки и т. п.)."""
    for tag in re.split(r"[,，\s]+", tags or ""):
        tag = tag.strip().strip("<>/")
        if not tag or not re.fullmatch(r"[\w:-]+", tag):
            continue
        text = re.sub(rf"<{re.escape(tag)}(?:\s[^>]*)?>[\s\S]*?</{re.escape(tag)}>", "", text,
                      flags=re.IGNORECASE)
    return text


def strip_tags_text(text: str, partial: bool = False) -> str:
    """
    Текст без служебных блоков Horae (вне <think>). partial=True отрезает и
    незакрытый хвостовой блок — для стрима и оборванных ответов.
    """
    if not text:
        return text or ""

    def cut(part: str) -> str:
        part = _ALL_BLOCKS_RE.sub("", part)
        if partial:
            part = _OPEN_TAIL_RE.sub("", part)
        return part

    cleaned = _outside_think(text, cut)
    return re.sub(r"\n{3,}", "\n\n", cleaned).rstrip()


def has_tags(text: str) -> bool:
    return bool(text) and bool(_ALL_BLOCKS_RE.search(_without_think(text)))


def _pick_block(blocks: list[str], marker: re.Pattern) -> str | None:
    """Несколько блоков — последний, в котором есть строка поля; иначе последний."""
    if not blocks:
        return None
    for body in reversed(blocks):
        if marker.search(body):
            return body
    return blocks[-1]


_HORAE_FIELD_MARK = re.compile(
    r"^\s*(time|location|atmosphere|scene_desc|characters|costume|item!{0,2}|item-|event"
    r"|affection|npc|agenda-?|rel|mood)\s*[:：]", re.IGNORECASE | re.MULTILINE)
_EVENT_MARK = re.compile(r"^\s*event\s*[:：]", re.IGNORECASE | re.MULTILINE)

# --- предметы ---
_EMOJI_CHAR = (
    "[\U0001F300-\U0001FAFF☀-➿⬀-⯿⌀-⏿←-⇿"
    "〰〽㊗㊙©®™]"
)
_EMOJI_PREFIX_RE = re.compile(
    "^\\s*(?:[\U0001F1E6-\U0001F1FF]{2}|" + _EMOJI_CHAR
    + "[️\U0001F3FB-\U0001F3FF]?(?:‍" + _EMOJI_CHAR + "[️\U0001F3FB-\U0001F3FF]?)*)️?"
)
# Количество в конце имени: «(3 бутылки)», «(50L)», «(1,5 кг)», «(2/3)».
# У плагина единицы только латиницей/CJK без пробела — «Пиво(3 бутылки)» и
# «Пиво(2 бутылки)» становились разными предметами (дубль при каждой смене
# количества).
_QTY_TAIL_RE = re.compile(r"\s*[\(（]\s*\d[\d.,/]*\s*[^()（）]{0,24}[\)）]\s*$")
_ONE_TAIL_RE = re.compile(r"\s*[\(（]\s*1\s*(?:шт\.?|штука|ед\.?|个|把|条|块|张|根|件|只|枚)?\s*[\)）]\s*$",
                          re.IGNORECASE)
_ZERO_TAIL_RE = re.compile(r"[\(（]\s*0(?:[.,]0+)?(?:\s*[^()（）\d][^()（）]{0,20})?[\)）]\s*$")
_CONSUMED_IN_NAME_RE = re.compile(
    r"[\(（]\s*(?:израсходован[оаы]?|использован[оаы]?|уничтожен[оаы]?|съеден[оаы]?|выпит[оаы]?"
    r"|сломан[оаы]?|потерян[оаы]?|закончил(?:ся|ась|ось|ись)|已消耗|已用完|已销毁|已銷毀|消耗殆尽"
    r"|消耗殆盡|消耗|用尽|用盡|consumed|used\s*up|destroyed|depleted)\s*[\)）]",
    re.IGNORECASE)
_CONSUMED_HOLDER_RE = re.compile(
    r"^(?:нет|никто|никого|израсходован[оаы]?|использован[оаы]?|уничтожен[оаы]?|потерян[оаы]?"
    r"|отсутствует|消耗|已消耗|已用完|消耗殆尽|消耗殆盡|用尽|用盡|无|無|consumed|used\s*up"
    r"|depleted|none)$", re.IGNORECASE)
_IMPORTANCE_RANK = {"": 0, "!": 1, "!!": 2}


def item_base_name(name: str) -> str:
    """Имя предмета без количества в конце: «Пиво(3 бутылки)» → «Пиво»."""
    return _QTY_TAIL_RE.sub("", name or "").strip()


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower().replace("ё", "е"))


def _split_icon(name_part: str) -> tuple[str, str]:
    m = _EMOJI_PREFIX_RE.match(name_part)
    if not m or not m.group(0).strip():
        return "", name_part.strip()
    icon = m.group(0).strip().replace("️", "")
    return icon, name_part[m.end():].strip()


def _parse_item(rest: str, importance: str) -> tuple[str, dict] | None:
    eq = rest.find("=")
    if eq <= 0:
        return None  # как в плагине: без «=владелец» строка предмета не принимается
    name_part, where = rest[:eq].strip(), rest[eq + 1:].strip()
    icon, name_part = _split_icon(name_part)
    desc = ""
    pipe = name_part.find("|")
    if pipe > 0:
        name_part, desc = name_part[:pipe].strip(), name_part[pipe + 1:].strip()
    name = _ONE_TAIL_RE.sub("", name_part).strip().replace("️", "")
    if not name:
        return None
    at = where.find("@")
    holder = (where[:at].strip() if at >= 0 else where) or ""
    location = where[at + 1:].strip() if at >= 0 else ""
    info = {"icon": icon, "importance": importance, "holder": holder, "location": location}
    if desc:
        info["description"] = desc
    return name, info


def _parse_pairs(value: str) -> dict[str, str]:
    """«Имя=значение», несколько через «;»/«|» — только если каждый кусок пара."""
    cand = [c for c in re.split(r"\s*[;；|｜]\s*", value) if c]
    segs = cand if len(cand) > 1 and all(re.match(r"^[^=]+=[^=]", c) for c in cand) else [value]
    out = {}
    for seg in segs:
        eq = seg.find("=")
        if eq > 0:
            key, val = seg[:eq].strip(), seg[eq + 1:].strip()
            if key and val:
                out[key] = val
    return out


def normalize_level(raw: str) -> str:
    """Уровень события: важность в любом из принятых написаний → normal/important/critical."""
    low = _norm(raw).strip("*!")
    if low in ("critical", "crit", "key", "ключевое", "ключевой", "критическое", "критичное",
               "критический", "критично", "关键", "關鍵"):
        return "critical"
    if low in ("important", "важное", "важный", "важно", "重要"):
        return "important"
    return "normal"


_AFF_ABS_RE = re.compile(r"^(.+?)=\s*([+\-]?\d+(?:[.,]\d+)?)")
_AFF_REL_RE = re.compile(r"^(.+?)\s*([+\-]\d+(?:[.,]\d+)?)")

_NPC_KEYS = {
    "gender": ("gender", "sex", "пол", "性别"),
    "age": ("age", "возраст", "年龄", "年纪"),
    "race": ("race", "раса", "вид", "种族", "族裔", "族群"),
    # «occupation» писал английский промпт плагина, а парсер его не понимал.
    "job": ("job", "class", "occupation", "profession", "профессия", "работа", "род занятий",
            "должность", "занятие", "职业", "职务", "身份"),
    "birthday": ("birthday", "birth", "день рождения", "др", "дата рождения", "生日"),
    "note": ("note", "notes", "примечание", "примечания", "заметка", "прочее", "补充", "备注", "其他"),
}
_NPC_KEY_MAP = {alias: field for field, aliases in _NPC_KEYS.items() for alias in aliases}


def _parse_npc(value: str) -> tuple[str, dict] | None:
    parts = value.split("~")
    main = parts[0].strip()
    info: dict = {}
    for extra in parts[1:]:
        colon = re.search(r"[:：]", extra)
        if not colon or colon.start() <= 0:
            continue
        key = _norm(extra[:colon.start()])
        val = extra[colon.end():].strip()
        field = _NPC_KEY_MAP.get(key)
        if field and val:
            info[field] = val
    pipe = main.find("|")
    if pipe > 0:
        name, desc = main[:pipe].strip(), main[pipe + 1:].strip()
        if "=" in desc or "@" in desc:
            at = desc.find("@")
            before = desc[:at] if at >= 0 else desc
            rel = desc[at + 1:].strip() if at >= 0 else ""
            eq = before.find("=")
            app = (before[:eq] if eq >= 0 else before).strip()
            per = before[eq + 1:].strip() if eq >= 0 else ""
            for key, val in (("appearance", app), ("personality", per), ("relationship", rel)):
                if val:
                    info[key] = val
        elif desc:
            legacy = [p.strip() for p in desc.split("|")]
            for key, val in zip(("appearance", "personality", "relationship"), legacy):
                if val:
                    info[key] = val
    else:
        name = main
    name = name.strip()
    return (name, info) if name else None


_AGENDA_DONE_RE = re.compile(
    r"[\(（]\s*(?:выполнено|выполнен[оа]?|завершено|завершен[оа]?|отменено|отменен[оа]?|неактуально"
    r"|готово|done|finished|completed|cancell?ed|完成|已完成|失效|取消|已取消)\s*[\)）]\s*$",
    re.IGNORECASE)


def _apply_line(meta: dict, key: str, value: str) -> bool:
    """Применить одну строку «ключ:значение» к мете. True — строка понята."""
    key = key.lower()
    if key == "time":
        date, time = horae_time.split_time(value)
        if date or time:
            meta["time"] = {"date": date, "time": time}
        return True
    if key == "location":
        if value:
            meta["scene"]["location"] = value
        return True
    if key == "atmosphere":
        if value:
            meta["scene"]["atmosphere"] = value
        return True
    if key == "scene_desc":
        # Описание места привязывается к месту, названному выше в этом же блоке
        # (в одном ответе персонажи могут пройти несколько мест).
        if value:
            meta["scene"]["desc"].append({"location": meta["scene"]["location"], "desc": value})
        return True
    if key == "characters":
        names = [n.strip() for n in re.split(r"[,，;；]", value) if n.strip()]
        if names:
            meta["scene"]["characters"] = names
        return True
    if key in ("costume", "mood"):
        meta["costumes" if key == "costume" else "mood"].update(_parse_pairs(value))
        return True
    if key == "item-":
        name = _split_icon(value)[1].strip()
        if name and name not in meta["items_removed"]:
            meta["items_removed"].append(name)
        return True
    if key in ("item", "item!", "item!!"):
        parsed = _parse_item(value, key[4:])
        if parsed:
            meta["items"][parsed[0]] = parsed[1]
        return True
    if key == "event":
        pipe = value.find("|")
        if pipe < 0:
            # Без уровня плагин строку выбрасывал; событие без уровня — обычное.
            text = value.strip()
            level = "normal"
        else:
            level = normalize_level(value[:pipe])
            text = value[pipe + 1:].strip()
        if text:
            meta["events"].append({"level": level, "text": text})
        return True
    if key == "affection":
        m = _AFF_ABS_RE.match(value)
        if m:
            meta["affection"][m.group(1).strip()] = {"mode": "set", "value": float(m.group(2).replace(",", "."))}
        else:
            m = _AFF_REL_RE.match(value)
            if m:
                meta["affection"][m.group(1).strip()] = {"mode": "add", "value": float(m.group(2).replace(",", "."))}
        return True
    if key == "npc":
        parsed = _parse_npc(value)
        if parsed:
            name, info = parsed
            meta["npcs"].setdefault(name, {}).update(info)
        return True
    if key == "agenda-":
        # «дата|текст» или «|текст» (пустая дата) — у плагина `pipe > 0`, и
        # «|текст» оставался с палкой в начале.
        pipe = value.find("|")
        text = value[pipe + 1:].strip() if pipe >= 0 else value.strip()
        if text and text not in meta["agenda_done"]:
            meta["agenda_done"].append(text)
        return True
    if key == "agenda":
        pipe = value.find("|")
        date, text = (value[:pipe].strip(), value[pipe + 1:].strip()) if pipe >= 0 else ("", value.strip())
        if not text:
            return True
        if _AGENDA_DONE_RE.search(text):
            done = _AGENDA_DONE_RE.sub("", text).strip()
            if done and done not in meta["agenda_done"]:
                meta["agenda_done"].append(done)
        elif not any(a["text"] == text for a in meta["agenda"]):
            meta["agenda"].append({"date": date, "text": text})
        return True
    if key == "rel":
        arrow, eq = value.find(">"), value.find("=")
        if arrow > 0 and eq > arrow:
            src, dst, rest = value[:arrow].strip(), value[arrow + 1:eq].strip(), value[eq + 1:]
            pipe = rest.find("|")
            rtype = (rest[:pipe] if pipe > 0 else rest).strip()
            note = rest[pipe + 1:].strip() if pipe > 0 else ""
            if src and dst and rtype:
                meta["relationships"].append({"from": src, "to": dst, "type": rtype, "note": note})
        return True
    return False


def _parse_lines(meta: dict, body: str) -> int:
    count = 0
    for line in (body or "").split("\n"):
        m = _LINE_RE.match(line)
        if m and _apply_line(meta, m.group(1), m.group(2)):
            count += 1
    return count


def _loose_tail(text: str) -> tuple[str, str] | None:
    """
    Свободный разбор: модель забыла обёртку и написала строки полей голыми.

    Плагин искал `time:`/`location:` где угодно в тексте без привязки к началу
    строки — и «showtime:» или реплика «Location: …» в прозе давали ложные
    данные. Здесь разбирается только ХВОСТОВОЙ блок строк полей (с пустыми
    строками между ними), и только если в нём ≥ 2 разных поля, одно из
    которых — время, место или событие. Этот блок и вырезается из текста.
    """
    lines = text.rstrip().split("\n")
    start = len(lines)
    keys = set()
    for i in range(len(lines) - 1, -1, -1):
        line = lines[i]
        if not line.strip():
            continue
        m = _LINE_RE.match(line)
        if not m or m.group(1).lower() not in _FIELD_LINE_KEYS:
            break
        keys.add(m.group(1).lower())
        start = i
    if len(keys) < 2 or not keys & {"time", "location", "event"}:
        return None
    return "\n".join(lines[:start]).rstrip(), "\n".join(lines[start:])


def parse_reply(text: str, ctx: ParseContext | None = None) -> tuple[str, dict | None]:
    """
    Разобрать ответ модели: (текст без служебных тегов, мета | None).

    None — в ответе нет данных Horae (ни тегов, ни хвоста строк полей):
    тогда при включённом auto_analyze их извлечёт фоновый ИИ-анализ.
    """
    ctx = ctx or ParseContext()
    original = text or ""
    source_text = _strip_custom(_without_think(original), ctx.strip_tags)

    horae_body = _pick_block(_HORAE_RE.findall(source_text), _HORAE_FIELD_MARK)
    if horae_body is None:
        comments = _HORAE_COMMENT_RE.findall(source_text)
        horae_body = comments[-1] if comments else None
    event_body = _pick_block(_EVENT_RE.findall(source_text), _EVENT_MARK)
    rpg_blocks = [b for b in _RPG_RE.findall(source_text) if b.strip()]
    tables = horae_tables.parse_blocks(source_text)

    meta = empty_meta()
    found = horae_body is not None or event_body is not None or rpg_blocks or tables
    clean = strip_tags_text(original, partial=True)
    raw_parts = []
    if found:
        # Строки обоих блоков — одним списком, как у плагина: модель нередко
        # кладёт event: внутрь <horae> или наоборот.
        _parse_lines(meta, horae_body or "")
        _parse_lines(meta, event_body or "")
        if horae_body is not None:
            raw_parts.append("<horae>\n" + horae_body.strip() + "\n</horae>")
        if event_body is not None:
            raw_parts.append("<horaeevent>\n" + event_body.strip() + "\n</horaeevent>")
        if rpg_blocks:
            rpg = horae_rpg.parse_block(rpg_blocks[-1], user_name=ctx.user_name,
                                        user_only=ctx.user_only)
            if rpg:
                meta["rpg"] = rpg
            raw_parts.append("<horaerpg>\n" + rpg_blocks[-1].strip() + "\n</horaerpg>")
        for t in tables:
            meta["tables"].append({"name": t["name"], "cells": dict(t["cells"])})
            raw_parts.append(f"<horaetable:{t['name']}>…</horaetable>")
        meta["source"] = "tags"
    else:
        loose = _loose_tail(clean)
        if loose is None:
            return clean, None
        clean, block = loose
        _parse_lines(meta, block)
        raw_parts.append(block)
        meta["source"] = "loose"
    meta["raw"] = "\n".join(raw_parts)
    meta["at"] = _now_iso()
    if not meta_has_data(meta):
        return clean, None
    return clean, meta


def merge_meta(base: dict | None, new: dict | None) -> dict | None:
    """
    Слить мету продолжения/анализа с прежней (плагин: mergeParsedToMeta).
    Скаляры — новые, если заданы; словари — слияние; события и отношения —
    заменяются новыми, если они есть (модель в конце ответа пишет итог хода
    целиком); списки удалений и планы — объединение.
    """
    if not new:
        return copy.deepcopy(base) if base else None
    if not base:
        return copy.deepcopy(new)
    out = copy.deepcopy(base)
    for key in META_KEYS:
        out.setdefault(key, copy.deepcopy(empty_meta()[key]))
    nt = new.get("time") or {}
    if nt.get("date"):
        out["time"]["date"] = nt["date"]
    if nt.get("time"):
        out["time"]["time"] = nt["time"]
    ns = new.get("scene") or {}
    for k in ("location", "atmosphere"):
        if ns.get(k):
            out["scene"][k] = ns[k]
    if ns.get("characters"):
        out["scene"]["characters"] = list(ns["characters"])
    out["scene"].setdefault("desc", [])
    out["scene"]["desc"].extend(copy.deepcopy(ns.get("desc") or []))
    for k in ("costumes", "mood", "items", "affection"):
        out[k].update(copy.deepcopy(new.get(k) or {}))
    for name, fields in (new.get("npcs") or {}).items():
        out["npcs"].setdefault(name, {}).update(fields)
    for k in ("items_removed", "agenda_done"):
        for v in new.get(k) or []:
            if v not in out[k]:
                out[k].append(v)
    if new.get("events"):
        out["events"] = copy.deepcopy(new["events"])
    for a in new.get("agenda") or []:
        if not any(x.get("text") == a.get("text") for x in out["agenda"]):
            out["agenda"].append(dict(a))
    if new.get("relationships"):
        out["relationships"] = copy.deepcopy(new["relationships"])
    out["tables"] = (out.get("tables") or []) + copy.deepcopy(new.get("tables") or [])
    if new.get("rpg"):
        out["rpg"] = copy.deepcopy(new["rpg"])
    raw = "\n".join(p for p in (base.get("raw"), new.get("raw")) if p)
    if raw:
        out["raw"] = raw
    out["source"] = new.get("source") or out.get("source")
    out["at"] = new.get("at") or _now_iso()
    return out


def _clean_str(value, limit: int = 2000) -> str:
    if value is None:
        return ""
    return str(value).strip()[:limit]


def normalize_meta(raw) -> dict | None:
    """
    Мета из интерфейса (редактор под сообщением) → проверенный META.
    Лишнее отбрасывается, длины режутся: мета уходит в промпт каждого хода.
    """
    if not isinstance(raw, dict):
        return None
    meta = empty_meta()
    t = raw.get("time") or {}
    if isinstance(t, dict):
        meta["time"] = {"date": _clean_str(t.get("date"), 100), "time": _clean_str(t.get("time"), 50)}
    sc = raw.get("scene") or {}
    if isinstance(sc, dict):
        meta["scene"]["location"] = _clean_str(sc.get("location"), 200)
        meta["scene"]["atmosphere"] = _clean_str(sc.get("atmosphere"), 300)
        chars = sc.get("characters") or []
        if isinstance(chars, str):
            chars = re.split(r"[,，;；]", chars)
        meta["scene"]["characters"] = [_clean_str(c, 100) for c in chars if _clean_str(c, 100)][:50]
        for d in (sc.get("desc") or [])[:20]:
            if isinstance(d, dict) and _clean_str(d.get("desc")):
                meta["scene"]["desc"].append({"location": _clean_str(d.get("location"), 200),
                                              "desc": _clean_str(d.get("desc"), 3000)})
    for key in ("costumes", "mood"):
        src = raw.get(key) or {}
        if isinstance(src, dict):
            meta[key] = {_clean_str(k, 100): _clean_str(v, 1000) for k, v in list(src.items())[:100]
                         if _clean_str(k, 100) and _clean_str(v, 1000)}
    items = raw.get("items") or {}
    if isinstance(items, dict):
        for name, info in list(items.items())[:300]:
            name = _clean_str(name, 200)
            if not name or not isinstance(info, dict):
                continue
            imp = info.get("importance") or ""
            item = {
                "icon": _clean_str(info.get("icon"), 16),
                "importance": imp if imp in _IMPORTANCE_RANK else "",
                "holder": _clean_str(info.get("holder"), 200),
                "location": _clean_str(info.get("location"), 300),
            }
            if _clean_str(info.get("description")):
                item["description"] = _clean_str(info.get("description"), 2000)
            meta["items"][name] = item
    meta["items_removed"] = [_clean_str(x, 200) for x in (raw.get("items_removed") or [])[:100]
                             if _clean_str(x, 200)]
    for ev in (raw.get("events") or [])[:50]:
        if isinstance(ev, dict) and _clean_str(ev.get("text")):
            meta["events"].append({"level": normalize_level(ev.get("level") or ""),
                                   "text": _clean_str(ev.get("text"), 4000)})
    aff = raw.get("affection") or {}
    if isinstance(aff, dict):
        for name, v in list(aff.items())[:100]:
            name = _clean_str(name, 100)
            if not name:
                continue
            if isinstance(v, dict):
                mode = "add" if v.get("mode") == "add" else "set"
                val = v.get("value")
            else:
                mode, val = "set", v
            try:
                meta["affection"][name] = {"mode": mode, "value": float(str(val).replace(",", "."))}
            except (TypeError, ValueError):
                continue
    npcs = raw.get("npcs") or {}
    if isinstance(npcs, dict):
        for name, fields in list(npcs.items())[:200]:
            name = _clean_str(name, 100)
            if not name or not isinstance(fields, dict):
                continue
            meta["npcs"][name] = {f: _clean_str(fields.get(f), 2000) for f in NPC_FIELDS
                                  if _clean_str(fields.get(f), 2000)}
    for a in (raw.get("agenda") or [])[:100]:
        if isinstance(a, dict) and _clean_str(a.get("text")):
            meta["agenda"].append({"date": _clean_str(a.get("date"), 100), "text": _clean_str(a.get("text"), 1000)})
    meta["agenda_done"] = [_clean_str(x, 1000) for x in (raw.get("agenda_done") or [])[:100] if _clean_str(x, 1000)]
    for r in (raw.get("relationships") or [])[:100]:
        if isinstance(r, dict) and _clean_str(r.get("from")) and _clean_str(r.get("to")) and _clean_str(r.get("type")):
            meta["relationships"].append({k: _clean_str(r.get(k), 500) for k in ("from", "to", "type", "note")})
    # Таблицы и RPG из редактора не правятся (у них свои вкладки) — сохраняем как были.
    if isinstance(raw.get("tables"), list):
        meta["tables"] = [t for t in raw["tables"] if isinstance(t, dict) and t.get("name")]
    if isinstance(raw.get("rpg"), dict):
        meta["rpg"] = raw["rpg"]
    meta["raw"] = _clean_str(raw.get("raw"), 20000)
    meta["source"] = "user"
    meta["at"] = _now_iso()
    return meta


# ===========================================================================
# Повтор (агрегация) состояния
# ===========================================================================

def empty_state() -> dict:
    return {
        "time": {"date": "", "time": ""}, "prev_location": "",
        "scene": {"location": "", "atmosphere": "", "characters": []},
        "costumes": {}, "mood": {}, "items": {}, "affection": {}, "npcs": {},
        "agenda": [], "blocked_agenda": [], "relationships": [], "locations": {},
        "events": [], "rpg": horae_rpg.empty_state(), "aliases": {}, "loc_aliases": {},
        "deleted_npcs": [], "counters": {"item": 0, "npc": 0}, "last_mid": 0,
    }


class Names:
    """Имена сцены: кто пользователь (персона) и кто персонаж чата."""

    def __init__(self, user: str = "", char: str = ""):
        self.user = user or "Пользователь"
        self.char = char or "Персонаж"


_NPC_ID_RE = re.compile(r"^N?(\d{1,4})\s+(.+)$")


class _Replay:
    """Один проход повтора. Состояние — обычный словарь (см. дизайн §6.1)."""

    def __init__(self, state: dict, names: Names, rpg_config: dict, settings: dict):
        self.s = state
        self.names = names
        self.rpg_config = rpg_config or {}
        self.settings = settings or {}
        self.user_only = frozenset(self.settings.get("rpg_user_only") or ())
        self.mid = 0

    # ---------- имена ----------
    def alias(self, name: str) -> str:
        name = (name or "").strip()
        if not name:
            return ""
        name = re.sub(r"\{\{user\}\}", self.names.user, name, flags=re.IGNORECASE)
        name = re.sub(r"\{\{char\}\}", self.names.char, name, flags=re.IGNORECASE)
        seen = set()
        aliases = self.s["aliases"]
        while name in aliases and name not in seen:
            seen.add(name)
            name = aliases[name]
        return name

    def loc_alias(self, name: str) -> str:
        name = (name or "").strip()
        seen = set()
        aliases = self.s["loc_aliases"]
        while name in aliases and name not in seen:
            seen.add(name)
            name = aliases[name]
        return name

    def resolve_owner(self, owner: str) -> str:
        owner = self.alias(owner)
        m = _NPC_ID_RE.match(owner)
        if m:
            wanted = m.group(1).zfill(3)
            for name, npc in self.s["npcs"].items():
                if npc.get("id") == wanted:
                    return name
            return self.alias(m.group(2))
        return owner

    def deleted(self, name: str) -> bool:
        return name in self.s["deleted_npcs"]

    # ---------- предметы ----------
    def find_item(self, name: str) -> str | None:
        if name in self.s["items"]:
            return name
        base = _norm(item_base_name(name))
        for key in self.s["items"]:
            if _norm(item_base_name(key)) == base:
                return key
        return None

    def remove_items(self, name: str) -> None:
        base = _norm(item_base_name(name))
        low = _norm(name)
        for key in list(self.s["items"]):
            if _norm(key) == low or _norm(item_base_name(key)) == base:
                del self.s["items"][key]

    def new_item_id(self) -> str:
        self.s["counters"]["item"] += 1
        return str(self.s["counters"]["item"]).zfill(3)

    def upsert_item(self, name: str, info: dict, *, user: bool = False) -> None:
        name = name.strip()
        if not name:
            return
        holder = self.alias(info.get("holder") or "")
        if _ZERO_TAIL_RE.search(name) or _CONSUMED_IN_NAME_RE.search(name) or _CONSUMED_HOLDER_RE.match(holder):
            clean = _CONSUMED_IN_NAME_RE.sub("", name).strip() or name
            self.remove_items(clean)
            return
        key = self.find_item(name)
        if key is None:
            item = {
                "id": self.new_item_id(), "icon": info.get("icon") or "",
                "importance": info.get("importance") if info.get("importance") in _IMPORTANCE_RANK else "",
                "holder": holder, "location": info.get("location") or "",
                "description": info.get("description") or "", "locked": False, "mid": self.mid,
            }
            self.s["items"][name] = item
            return
        item = dict(self.s["items"][key])
        locked = item.get("locked") and not user
        if not locked:
            if info.get("icon"):
                item["icon"] = info["icon"]
            new_imp = info.get("importance") if info.get("importance") in _IMPORTANCE_RANK else ""
            # ИИ важность только повышает — «важное» не должно стать обычным от
            # строки обновления количества, где модель не написала «!».
            if user or _IMPORTANCE_RANK[new_imp] >= _IMPORTANCE_RANK.get(item.get("importance") or "", 0):
                item["importance"] = new_imp
            if (info.get("description") or "").strip():
                item["description"] = info["description"].strip()
        if "holder" in info:
            item["holder"] = holder
        if "location" in info:
            item["location"] = info.get("location") or ""
        item["mid"] = self.mid
        if key != name:
            del self.s["items"][key]
        self.s["items"][name] = item

    # ---------- NPC ----------
    def new_npc_id(self) -> str:
        self.s["counters"]["npc"] += 1
        return str(self.s["counters"]["npc"]).zfill(3)

    def upsert_npc(self, name: str, fields: dict, *, user: bool = False) -> None:
        name = self.alias(name)
        if not name or (self.deleted(name) and not user):
            return
        npcs = self.s["npcs"]
        date = self.s["time"].get("date") or ""
        if name not in npcs:
            npc = {"id": self.new_npc_id(), **{f: "" for f in NPC_FIELDS},
                   "aliases": [], "age_ref": "", "first_mid": self.mid, "last_mid": self.mid,
                   "user": user}
            npcs[name] = npc
        npc = npcs[name]
        for f in NPC_FIELDS:
            if f not in fields:
                continue
            val = (fields.get(f) or "").strip()
            if not user and f in _NPC_PROTECTED and npc.get(f):
                continue
            if f == "age" and val:
                old = npc.get("age") or ""
                if not npc.get("age_ref") or _int_prefix(old) != _int_prefix(val):
                    npc["age_ref"] = date
            if user or val:
                npc[f] = val
        npc["last_mid"] = self.mid
        if user:
            npc["user"] = True

    # ---------- планы ----------
    def add_agenda(self, date: str, text: str, source: str) -> None:
        text = (text or "").strip()
        if not text:
            return
        key = _norm(text)
        if source == "ai" and key in self.s["blocked_agenda"]:
            return
        if any(_norm(a["text"]) == key for a in self.s["agenda"]):
            return
        self.s["agenda"].append({"date": (date or "").strip(), "text": text, "source": source,
                                 "mid": self.mid})

    def done_agenda(self, text: str) -> None:
        """
        Пункт выполнен/отменён. Совпадение — по тексту или подстроке в обе
        стороны (модель пишет «agenda-:» ключевыми словами пункта). Подстрока —
        только от 4 символов: иначе «в» или «Анна» снимали бы чужие планы.
        """
        key = _norm(text)
        if not key:
            return

        def match(a) -> bool:
            other = _norm(a["text"])
            if other == key:
                return True
            short = min(len(other), len(key))
            return short >= 4 and (key in other or other in key)

        self.s["agenda"] = [a for a in self.s["agenda"] if not match(a)]

    # ---------- отношения ----------
    def set_rel(self, src: str, dst: str, rtype: str, note: str, *, user: bool) -> None:
        src, dst = self.alias(src), self.alias(dst)
        if not (src and dst and rtype):
            return
        for rel in self.s["relationships"]:
            if rel["from"] == src and rel["to"] == dst:
                if rel.get("user") and not user:
                    return  # правку пользователя ИИ не перезаписывает
                rel["type"] = rtype
                if note or user:
                    rel["note"] = note
                rel["user"] = rel.get("user") or user
                rel["mid"] = self.mid
                return
        self.s["relationships"].append({"from": src, "to": dst, "type": rtype, "note": note or "",
                                        "user": user, "mid": self.mid})

    # ---------- места ----------
    def set_location(self, name: str, desc: str, *, user: bool) -> None:
        name = self.loc_alias(name)
        if not name or not desc:
            return
        locs = self.s["locations"]
        entry = locs.get(name)
        if entry and not user and (entry.get("user") or entry.get("deleted")):
            return
        if entry is None:
            entry = {"desc": "", "first_mid": self.mid, "mid": self.mid, "user": False,
                     "deleted": False, "aliases": []}
            locs[name] = entry
        entry["desc"] = desc
        entry["mid"] = self.mid
        if user:
            entry["user"] = True
            entry["deleted"] = False

    # ---------- применение меты ----------
    def apply_meta(self, mid: int, meta: dict) -> None:
        self.mid = mid
        s = self.s
        t = meta.get("time") or {}
        if t.get("date"):
            s["time"]["date"] = t["date"]
        if t.get("time"):
            s["time"]["time"] = t["time"]
        sc = meta.get("scene") or {}
        if sc.get("location"):
            s["prev_location"] = s["scene"]["location"]
            s["scene"]["location"] = self.loc_alias(sc["location"])
        if sc.get("atmosphere"):
            s["scene"]["atmosphere"] = sc["atmosphere"]
        if sc.get("characters"):
            chars = []
            for c in sc["characters"]:
                c = self.alias(c)
                if c and c not in chars and not self.deleted(c):
                    chars.append(c)
            s["scene"]["characters"] = chars
        for pair in sc.get("desc") or []:
            self.set_location(pair.get("location") or sc.get("location") or s["scene"]["location"],
                              (pair.get("desc") or "").strip(), user=False)
        for key in ("costumes", "mood"):
            for name, value in (meta.get(key) or {}).items():
                name = self.alias(name)
                if name and not self.deleted(name) and value:
                    s[key][name] = value
        for name, info in (meta.get("items") or {}).items():
            self.upsert_item(name, info or {})
        for name in meta.get("items_removed") or []:
            self.remove_items(name)
        for name, a in (meta.get("affection") or {}).items():
            name = self.alias(name)
            if not name or self.deleted(name) or not isinstance(a, dict):
                continue
            try:
                value = float(a.get("value"))
            except (TypeError, ValueError):
                continue
            s["affection"][name] = value if a.get("mode") != "add" else s["affection"].get(name, 0.0) + value
        for name, fields in (meta.get("npcs") or {}).items():
            self.upsert_npc(name, fields or {})
        for a in meta.get("agenda") or []:
            self.add_agenda(a.get("date") or "", a.get("text") or "", "ai")
        for text in meta.get("agenda_done") or []:
            self.done_agenda(text)
        for rel in meta.get("relationships") or []:
            self.set_rel(rel.get("from"), rel.get("to"), (rel.get("type") or "").strip(),
                         (rel.get("note") or "").strip(), user=False)
        for i, ev in enumerate(meta.get("events") or []):
            text = (ev or {}).get("text") or ""
            if text.strip():
                s["events"].append({
                    "mid": mid, "i": i, "level": ev.get("level") if ev.get("level") in LEVELS else "normal",
                    "text": text.strip(),
                    # Своя дата сообщения; нет — текущая дата сюжета на тот момент.
                    "date": t.get("date") or s["time"]["date"], "time": t.get("time") or s["time"]["time"],
                })
        if meta.get("rpg"):
            horae_rpg.apply_changes(s["rpg"], meta["rpg"], self._rpg_ctx())
        s["last_mid"] = max(s["last_mid"], mid)

    def _rpg_ctx(self):
        def take(name: str):
            key = self.find_item(name)
            if key is None:
                return None
            return self.s["items"].pop(key)

        def give(name: str, info: dict):
            info = dict(info or {})
            info.setdefault("icon", "📦")
            self.upsert_item(name, info, user=True)

        return horae_rpg.ApplyContext(
            resolve_owner=self.resolve_owner, config=self.rpg_config, user_name=self.names.user,
            user_only=self.user_only, take_item=take, give_item=give, mid=self.mid,
        )

    # ---------- правки пользователя ----------
    def apply_op(self, op: dict) -> None:
        kind = op.get("kind") or ""
        s = self.s
        if kind.startswith("rpg."):
            horae_rpg.apply_op(s["rpg"], op, self._rpg_ctx())
            return
        handler = _OP_HANDLERS.get(kind)
        if handler:
            handler(self, op)


def _int_prefix(value: str) -> int | None:
    m = re.match(r"\s*(\d+)", value or "")
    return int(m.group(1)) if m else None


# --- обработчики правок (дизайн §6.2) ---
def _op_npc_set(r: _Replay, op):
    name = r.alias(op.get("name"))
    if name in r.s["npcs"]:
        r.upsert_npc(name, {k: v for k, v in (op.get("fields") or {}).items() if k in NPC_FIELDS},
                     user=True)


def _op_npc_add(r: _Replay, op):
    name = (op.get("name") or "").strip()
    if not name:
        return
    if name in r.s["deleted_npcs"]:
        r.s["deleted_npcs"].remove(name)
    r.upsert_npc(name, {k: v for k, v in (op.get("fields") or {}).items() if k in NPC_FIELDS}, user=True)
    npc = r.s["npcs"].get(r.alias(name))
    if npc is not None:
        for alias in op.get("aliases") or []:
            alias = (alias or "").strip()
            if alias and alias != name:
                r.s["aliases"][alias] = name
                if alias not in npc["aliases"]:
                    npc["aliases"].append(alias)


def _rename_key(d: dict, old: str, new: str) -> None:
    if old in d:
        value = d.pop(old)
        if new not in d:
            d[new] = value


def _op_npc_rename(r: _Replay, op):
    old, new = r.alias(op.get("from")), (op.get("to") or "").strip()
    if not old or not new or old == new:
        return
    s = r.s
    if old in s["npcs"]:
        npc = s["npcs"].pop(old)
        if new in s["npcs"]:
            # Слияние с существующим: пустые поля берём у переименованного.
            target = s["npcs"][new]
            for f in NPC_FIELDS:
                if not target.get(f) and npc.get(f):
                    target[f] = npc[f]
            target["aliases"] = sorted(set(target.get("aliases", [])) | set(npc.get("aliases", [])) | {old})
        else:
            npc["aliases"] = sorted(set(npc.get("aliases", [])) | {old})
            s["npcs"][new] = npc
    for key in ("affection", "costumes", "mood"):
        _rename_key(s[key], old, new)
    s["scene"]["characters"] = [new if c == old else c for c in s["scene"]["characters"]]
    for rel in s["relationships"]:
        if rel["from"] == old:
            rel["from"] = new
        if rel["to"] == old:
            rel["to"] = new
    for item in s["items"].values():
        if item.get("holder") == old:
            item["holder"] = new
    rpg = s.get("rpg") or {}
    for key in ("bars", "status", "skills", "attrs", "reputation", "equipment", "levels", "xp", "currency"):
        if isinstance(rpg.get(key), dict):
            _rename_key(rpg[key], old, new)
    s["aliases"][old] = new
    s["aliases"].pop(new, None)


def _op_npc_delete(r: _Replay, op):
    name = r.alias(op.get("name"))
    if not name:
        return
    s = r.s
    s["npcs"].pop(name, None)
    for key in ("affection", "costumes", "mood"):
        s[key].pop(name, None)
    s["scene"]["characters"] = [c for c in s["scene"]["characters"] if c != name]
    if name not in s["deleted_npcs"]:
        s["deleted_npcs"].append(name)


def _op_affection_set(r: _Replay, op):
    name = r.alias(op.get("name"))
    try:
        value = float(str(op.get("value")).replace(",", "."))
    except (TypeError, ValueError):
        return
    if name:
        r.s["affection"][name] = value


def _op_affection_delete(r: _Replay, op):
    r.s["affection"].pop(r.alias(op.get("name")), None)


def _op_item_set(r: _Replay, op):
    key = r.find_item(op.get("name") or "")
    if key is None:
        return
    fields = op.get("fields") or {}
    item = dict(r.s["items"][key])
    for f in ("icon", "description", "holder", "location"):
        if f in fields:
            item[f] = r.alias(fields[f]) if f == "holder" else (fields[f] or "").strip()
    if fields.get("importance") in _IMPORTANCE_RANK:
        item["importance"] = fields["importance"]
    new_name = (op.get("rename") or "").strip()
    del r.s["items"][key]
    r.s["items"][new_name or key] = item


def _op_item_add(r: _Replay, op):
    name = (op.get("name") or "").strip()
    fields = dict(op.get("fields") or {})
    fields.setdefault("holder", "")
    fields.setdefault("location", "")
    if name:
        r.upsert_item(name, fields, user=True)


def _op_item_delete(r: _Replay, op):
    r.remove_items(op.get("name") or "")


def _op_item_lock(r: _Replay, op):
    key = r.find_item(op.get("name") or "")
    if key is not None:
        r.s["items"][key] = {**r.s["items"][key], "locked": bool(op.get("locked"))}


def _op_agenda_add(r: _Replay, op):
    text = (op.get("text") or "").strip()
    if text and _norm(text) in r.s["blocked_agenda"]:
        r.s["blocked_agenda"].remove(_norm(text))
    r.add_agenda(op.get("date") or "", text, "user")


def _op_agenda_edit(r: _Replay, op):
    key = _norm(op.get("text"))
    for a in r.s["agenda"]:
        if _norm(a["text"]) == key:
            new_text = (op.get("new_text") or "").strip()
            if new_text:
                a["text"] = new_text
            if "date" in op:
                a["date"] = (op.get("date") or "").strip()
            a["source"] = "user"
            return


def _op_agenda_delete(r: _Replay, op):
    key = _norm(op.get("text"))
    if not key:
        return
    r.s["agenda"] = [a for a in r.s["agenda"] if _norm(a["text"]) != key]
    # Как _deletedAgendaTexts плагина: удалённое пользователем модель не вернёт.
    if key not in r.s["blocked_agenda"]:
        r.s["blocked_agenda"].append(key)


def _op_rel_set(r: _Replay, op):
    r.set_rel(op.get("from"), op.get("to"), (op.get("type") or "").strip(),
              (op.get("note") or "").strip(), user=True)


def _op_rel_delete(r: _Replay, op):
    src, dst = r.alias(op.get("from")), r.alias(op.get("to"))
    r.s["relationships"] = [x for x in r.s["relationships"] if not (x["from"] == src and x["to"] == dst)]


def _op_location_set(r: _Replay, op):
    r.set_location(op.get("name") or "", (op.get("desc") or "").strip(), user=True)


def _op_location_rename(r: _Replay, op):
    old, new = r.loc_alias(op.get("from")), (op.get("to") or "").strip()
    if not old or not new or old == new:
        return
    locs = r.s["locations"]
    # Дочерние места («Таверна·зал») переименовываются вместе с родителем.
    for key in list(locs):
        if key == old or key.startswith(old + "·"):
            new_key = new + key[len(old):]
            entry = locs.pop(key)
            entry["aliases"] = sorted(set(entry.get("aliases", [])) | {key})
            entry["user"] = True
            locs[new_key] = entry
            r.s["loc_aliases"][key] = new_key
    sc = r.s["scene"]
    if sc["location"] == old or sc["location"].startswith(old + "·"):
        sc["location"] = new + sc["location"][len(old):]


def _op_location_delete(r: _Replay, op):
    name = r.loc_alias(op.get("name"))
    if not name:
        return
    entry = r.s["locations"].setdefault(name, {"desc": "", "first_mid": r.mid, "mid": r.mid,
                                                "user": True, "aliases": []})
    entry["deleted"] = True


def _op_location_merge(r: _Replay, op):
    src, dst = r.loc_alias(op.get("from")), r.loc_alias(op.get("to"))
    locs = r.s["locations"]
    if not src or not dst or src == dst or src not in locs:
        return
    source = locs.pop(src)
    target = locs.setdefault(dst, {"desc": "", "first_mid": r.mid, "mid": r.mid, "user": True,
                                   "deleted": False, "aliases": []})
    if source.get("desc") and source["desc"] not in target.get("desc", ""):
        target["desc"] = (target.get("desc", "") + "\n" + source["desc"]).strip()
    target["aliases"] = sorted(set(target.get("aliases", [])) | set(source.get("aliases", [])) | {src})
    target["user"] = True
    r.s["loc_aliases"][src] = dst


def _op_scene_set(r: _Replay, op):
    s = r.s
    if "date" in op:
        s["time"]["date"] = (op.get("date") or "").strip()
    if "time" in op:
        s["time"]["time"] = (op.get("time") or "").strip()
    if "location" in op:
        new = (op.get("location") or "").strip()
        if new != s["scene"]["location"]:
            s["prev_location"] = s["scene"]["location"]
        s["scene"]["location"] = new
    if "atmosphere" in op:
        s["scene"]["atmosphere"] = (op.get("atmosphere") or "").strip()
    if "characters" in op:
        chars = op.get("characters") or []
        if isinstance(chars, str):
            chars = re.split(r"[,，;；]", chars)
        s["scene"]["characters"] = [r.alias(c) for c in chars if (c or "").strip()]


def _op_costume_set(r: _Replay, op, key="costumes"):
    name = r.alias(op.get("name"))
    value = (op.get("value") or "").strip()
    if not name:
        return
    if value:
        r.s[key][name] = value
    else:
        r.s[key].pop(name, None)


_OP_HANDLERS = {
    "npc.set": _op_npc_set,
    "npc.add": _op_npc_add,
    "npc.rename": _op_npc_rename,
    "npc.delete": _op_npc_delete,
    "affection.set": _op_affection_set,
    "affection.delete": _op_affection_delete,
    "item.set": _op_item_set,
    "item.add": _op_item_add,
    "item.delete": _op_item_delete,
    "item.lock": _op_item_lock,
    "agenda.add": _op_agenda_add,
    "agenda.edit": _op_agenda_edit,
    "agenda.delete": _op_agenda_delete,
    "rel.set": _op_rel_set,
    "rel.delete": _op_rel_delete,
    "location.set": _op_location_set,
    "location.rename": _op_location_rename,
    "location.delete": _op_location_delete,
    "location.merge": _op_location_merge,
    "scene.set": _op_scene_set,
    "costume.set": _op_costume_set,
    "mood.set": lambda r, op: _op_costume_set(r, op, "mood"),
}

OP_KINDS = frozenset(_OP_HANDLERS) | frozenset({
    "rpg.bar", "rpg.status", "rpg.attr", "rpg.skill.add", "rpg.skill.delete", "rpg.level",
    "rpg.xp", "rpg.currency", "rpg.rep", "rpg.equip", "rpg.unequip", "rpg.base", "rpg.base.delete",
})


def replay(entries, *, ops=(), seed=None, until: int | None = None, settings: dict | None = None,
           names: Names | None = None, rpg_config: dict | None = None) -> dict:
    """
    Проиграть меты сообщений и правки пользователя → STATE (дизайн §6.1).

    :param entries: [(mid, role, meta, side)] по возрастанию mid.
    :param ops: журнал правок, в любом порядке (сортируется по (at, seq)).
    :param seed: стартовое состояние (перенос в новый чат) или None.
    :param until: учитывать сообщения с mid <= until и правки с at <= until.
    """
    state = copy.deepcopy(seed) if isinstance(seed, dict) else empty_state()
    base = empty_state()
    for key, value in base.items():
        state.setdefault(key, value)
    state["events"] = list(state.get("events") or [])
    r = _Replay(state, names or Names(), rpg_config or {}, settings or {})
    pending = sorted(
        (op for op in (ops or []) if isinstance(op, dict)),
        key=lambda op: (int(op.get("at") or 0), int(op.get("seq") or 0)),
    )
    if until is not None:
        pending = [op for op in pending if int(op.get("at") or 0) <= until]
    pos = 0
    for mid, _role, meta, side in entries:
        if until is not None and mid > until:
            break
        while pos < len(pending) and int(pending[pos].get("at") or 0) < mid:
            r.mid = int(pending[pos].get("at") or 0)
            _safe(r.apply_op, pending[pos])
            pos += 1
        if side or not isinstance(meta, dict):
            continue
        _safe(r.apply_meta, mid, meta)
    while pos < len(pending):
        _safe(r.apply_op, pending[pos])
        pos += 1
    return state


def _safe(fn, *args) -> None:
    """
    Одна битая мета (руками правленый JSON, импорт старой версии) или правка
    не должна ронять пересчёт всего состояния: тогда чат остался бы без
    Хроники целиком. Такая запись пропускается, остальное считается.
    """
    try:
        fn(*args)
    except Exception:  # noqa: BLE001
        import logging

        logging.getLogger("aichat.horae").warning("Horae: запись пропущена при пересчёте", exc_info=True)


# ===========================================================================
# Хронология и свёртки
# ===========================================================================

def summary_covers(summary: dict, mid: int) -> bool:
    rng = summary.get("range") or [0, 0]
    try:
        return int(rng[0]) <= mid <= int(rng[1]) and summary.get("kind") != "carry"
    except (TypeError, ValueError, IndexError):
        return False


def covering_summary(summaries, mid: int, *, active_only: bool = True) -> dict | None:
    """Верхнеуровневая свёртка, покрывающая сообщение (активная, если active_only)."""
    for s in summaries or []:
        if (not active_only or s.get("active", True)) and summary_covers(s, mid):
            return s
    return None


def timeline(state: dict, summaries, *, calendar=None) -> list[dict]:
    """
    Хронология для интерфейса: все события и верхнеуровневые свёртки по
    порядку чата. У события — covered_by (id активной свёртки) и rel
    (относительное время от текущей даты сюжета).
    """
    cur = state["time"].get("date") or ""
    items: list[dict] = []
    for s in summaries or []:
        if s.get("kind") == "carry":
            pos = -1
        else:
            pos = int((s.get("range") or [0])[0])
        items.append({
            "kind": "summary", "id": s.get("id"), "range": list(s.get("range") or [0, 0]),
            "text": s.get("text") or "", "depth": int(s.get("depth") or 1),
            "active": bool(s.get("active", True)), "auto": s.get("kind") == "auto",
            "kind_detail": s.get("kind") or "manual", "date_from": s.get("date_from") or "",
            "date_to": s.get("date_to") or "", "events": int(s.get("events") or 0),
            "rel": horae_time.relative_label(s.get("date_from") or "", cur, calendar) if cur else "",
            "_pos": (pos, 0),
        })
    for ev in state.get("events") or []:
        cov = covering_summary(summaries, ev["mid"])
        items.append({
            "kind": "event", "mid": ev["mid"], "i": ev["i"], "level": ev["level"], "text": ev["text"],
            "date": ev.get("date") or "", "time": ev.get("time") or "",
            "rel": horae_time.relative_label(ev.get("date") or "", cur, calendar) if cur and ev.get("date") else "",
            "covered_by": cov.get("id") if cov else None,
            "_pos": (ev["mid"], 1 + ev["i"]),
        })
    items.sort(key=lambda x: x["_pos"])
    for it in items:
        it.pop("_pos", None)
    return items


def render_timeline(state: dict, summaries, settings: dict, *, calendar=None,
                    snapshot_upto: int = 0) -> str:
    """
    Раздел «[Сюжетная линия]» блока состояния (порт generateCompactPrompt):
    все ключевые и важные события и активные свёртки + последние
    context_depth обычных; покрытое активной свёрткой не идёт.

    :param snapshot_upto: до какого сообщения историю в этом ходе уже несёт
        мастер-снимок (0 — снимка в ходе нет). События до него снимок уже
        пересказал — в ленте остаются только ключевые (точки отсчёта времени),
        остальное заменяет одна строка-ссылка на снимок. Так же уходят свёртки
        этого чата, целиком лежащие до него (ручные, импортированные из
        SillyTavern); свёртки «Ранее» — из прошлого чата, снимок их не знает.
    """
    depth = int(settings.get("context_depth", 15) or 0)
    cur = state["time"].get("date") or ""
    rows: list[tuple[tuple, str]] = []
    normal: list[tuple[tuple, str]] = []
    in_snapshot = 0

    def rel(date: str) -> str:
        label = horae_time.relative_label(date, cur, calendar) if (cur and date) else ""
        return f"({label})" if label else ""

    for s in summaries or []:
        if not s.get("active", True):
            continue
        if (snapshot_upto and s.get("kind") != "carry"
                and int((s.get("range") or [0, 0])[-1] or 0) <= snapshot_upto):
            in_snapshot += 1
            continue
        dates = [d for d in (s.get("date_from"), s.get("date_to")) if d]
        span = ""
        if dates:
            span = "·" + (dates[0] if len(set(dates)) == 1 else f"{dates[0]}~{dates[-1]}")
        label = "📚 [Ранее" if s.get("kind") == "carry" else "📋 [Сводка"
        pos = -1 if s.get("kind") == "carry" else int((s.get("range") or [0])[0])
        rows.append(((pos, 0), f"{label}{span}]{rel(s.get('date_from') or '')}: {s.get('text', '').strip()}"))
    for ev in state.get("events") or []:
        if covering_summary(summaries, ev["mid"]):
            continue
        if snapshot_upto and ev["mid"] <= snapshot_upto and ev["level"] != "critical":
            in_snapshot += 1
            continue
        date = ev.get("date") or ""
        line = (f"{LEVEL_MARK.get(ev['level'], '○')} #{ev['mid']} {date or '?'}"
                f"{(' ' + ev['time']) if ev.get('time') else ''}{rel(date)}: {ev['text']}")
        key = (ev["mid"], 1 + ev["i"])
        if ev["level"] in ("critical", "important"):
            rows.append((key, line))
        else:
            normal.append((key, line))
    if depth > 0:
        rows.extend(normal[-depth:])
    if in_snapshot:
        rows.append(((-1, 1), f"📜 [До #{snapshot_upto}] Остальные события — в мастер-снимке выше."))
    if not rows:
        return ""
    rows.sort(key=lambda x: x[0])
    return "\n[Сюжетная линия]\n" + "\n".join(line for _, line in rows)


# ===========================================================================
# Рендер блока состояния
# ===========================================================================

_LOC_SEP_RE = re.compile(r"[·・/|]| - ")


def find_location(state: dict, current: str, previous: str = "") -> tuple[str, dict] | None:
    """Память сцены для места (порт _findLocationMemory): имя, псевдоним,
    ближайший родитель, родитель прошлого места."""
    locs = {k: v for k, v in (state.get("locations") or {}).items() if not v.get("deleted")}
    if not current or not locs:
        return None
    if current in locs:
        return current, locs[current]
    for name, entry in locs.items():
        if current in (entry.get("aliases") or []):
            return name, entry
    parts = [p.strip() for p in _LOC_SEP_RE.split(current) if p.strip()]
    for i in range(len(parts) - 1, 0, -1):
        partial = "·".join(parts[:i])
        if partial in locs:
            return partial, locs[partial]
        for name, entry in locs.items():
            if partial in (entry.get("aliases") or []):
                return name, entry
    if previous and parts:
        prev_parent = _LOC_SEP_RE.split(previous)[0].strip()
        if prev_parent != parts[0] and parts[0] in prev_parent and prev_parent in locs:
            return prev_parent, locs[prev_parent]
    return None


def _fmt_num(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:.1f}".rstrip("0").rstrip(".")


def _costume_for(costumes: dict, name: str) -> str:
    if costumes.get(name):
        return costumes[name]
    for key, value in costumes.items():
        if value and (key in name or name in key):
            return value
    return ""


def is_main_npc(name: str, npc: dict, names: Names, pinned) -> bool:
    candidates = {name, *(npc.get("aliases") or [])}
    return names.char in candidates or bool(candidates & set(pinned or ()))


def render_state_block(state: dict, settings: dict, *, names: Names, summaries=(),
                       tables_block: str = "", rpg_block: str = "", pinned=(), calendar=None,
                       snapshot_upto: int = 0) -> str:
    """
    Блок «[Снимок текущего состояния …]» для хвоста промпта (порт
    generateCompactPrompt, русские подписи). Пустое состояние — "".
    snapshot_upto — см. render_timeline.
    """
    s = settings or {}
    lines = ["[Снимок текущего состояния — сравните с сюжетом этого раунда, выводите в <horae> "
             "только существенно изменившиеся поля]"]
    date, time = state["time"].get("date") or "", state["time"].get("time") or ""
    has_content = False
    if date or time:
        lines.append(f"[Время|{horae_time.format_date(date, time, calendar)}]")
        has_content = True
        if s.get("send_timeline", True) and date:
            ref = horae_time.time_reference(date, calendar)
            if ref:
                lines.append(ref)
    sc = state["scene"]
    loc = sc.get("location") or ""
    if loc:
        has_content = True
        lines.append(f"[Сцена|{loc}|{sc['atmosphere']}]" if sc.get("atmosphere") else f"[Сцена|{loc}]")
        if s.get("send_location_memory"):
            found = find_location(state, loc, state.get("prev_location") or "")
            if found and found[1].get("desc"):
                lines.append(f"[Память сцены|{found[1]['desc']}]")
            parts = [p.strip() for p in _LOC_SEP_RE.split(loc) if p.strip()]
            if len(parts) > 1:
                parent = parts[0]
                entry = (state.get("locations") or {}).get(parent)
                if entry and not entry.get("deleted") and entry.get("desc") and (not found or found[0] != parent):
                    lines.append(f"[Память сцены:{parent}|{entry['desc']}]")
    present = list(sc.get("characters") or [])
    if s.get("send_characters", True):
        if present:
            has_content = True
            bits = []
            for name in present:
                costume = _costume_for(state["costumes"], name)
                bits.append(f"{name}({costume})" if costume else name)
            lines.append("[Присутствуют|" + "|".join(bits) + "]")
            if s.get("send_mood"):
                moods = [f"{n}:{state['mood'][n]}" for n in present if state["mood"].get(n)]
                if moods:
                    lines.append("[Настроение|" + "|".join(moods) + "]")
        if s.get("send_relationships"):
            rels = [x for x in state.get("relationships") or [] if x["from"] in present or x["to"] in present]
            if rels:
                lines.append("\n[Сеть отношений]")
                for x in rels:
                    lines.append(f"{x['from']}→{x['to']}: {x['type']}" + (f"({x['note']})" if x.get("note") else ""))
    if s.get("send_items", True):
        equipped = set()
        if s.get("rpg_enabled") and s.get("rpg_equipment"):
            for slots in ((state.get("rpg") or {}).get("equipment") or {}).values():
                for entries in slots.values():
                    equipped.update(e.get("name") for e in entries)
        items = [(n, i) for n, i in state["items"].items() if n not in equipped]
        if items:
            has_content = True
            lines.append("\n[Список предметов]")
            for name, it in items:
                tag = {"!!": "[критич.]", "!": "[важно]"}.get(it.get("importance") or "", "")
                desc = f" | {it['description']}" if it.get("description") else ""
                where = (it.get("holder") or "") + (f"@{it['location']}" if it.get("location") else "")
                lines.append(f"#{it.get('id', '???')} {it.get('icon') or ''}{name}{tag}{desc} = {where}")
        elif has_content:
            lines.append("\n[Список предметов] (пусто)")
    if s.get("send_affection", True):
        aff = [f"{n}:{'+' if v > 0 else ''}{_fmt_num(v)}" for n, v in state["affection"].items() if v]
        if aff:
            has_content = True
            lines.append("[Расположение|" + "|".join(aff) + "]")
    if s.get("send_characters", True) and state["npcs"]:
        has_content = True
        lines.append("\n[Известные NPC]")
        for name, npc in state["npcs"].items():
            per = npc.get("personality") or ""
            if not s.get("send_main_personality", True) and is_main_npc(name, npc, names, pinned):
                per = ""
            line = f"N{npc.get('id', '???')} {name}"
            app, rel = npc.get("appearance") or "", npc.get("relationship") or ""
            if app or per or rel:
                line += f"｜{app}={per}@{rel}"
            extras = []
            if npc.get("aliases"):
                extras.append("псевдонимы:" + "/".join(npc["aliases"]))
            if npc.get("gender"):
                extras.append("пол:" + npc["gender"])
            if npc.get("age"):
                extras.append("возраст:" + horae_time.current_age(
                    npc["age"], npc.get("birthday") or "", npc.get("age_ref") or "", date, calendar))
            for key, label in (("race", "раса"), ("job", "профессия"), ("birthday", "день рождения"),
                               ("note", "примечания")):
                if npc.get(key):
                    extras.append(f"{label}:{npc[key]}")
            if extras:
                line += "~" + "~".join(extras)
            lines.append(line)
    if s.get("send_agenda", True) and state.get("agenda"):
        has_content = True
        lines.append("\n[Список дел]")
        for a in state["agenda"]:
            lines.append("· " + (f"{a['date']} " if a.get("date") else "") + a["text"])
    if rpg_block:
        has_content = True
        lines.append(rpg_block.rstrip())
    if s.get("send_timeline", True):
        tl = render_timeline(state, summaries, s, calendar=calendar, snapshot_upto=snapshot_upto)
        if tl:
            has_content = True
            lines.append(tl)
    if tables_block:
        has_content = True
        lines.append(tables_block.rstrip())
    if not has_content:
        return ""
    return "\n".join(lines).strip()


def analysis_context(state: dict, names: Names) -> str:
    """Лёгкое «что известно до этого сообщения» для ИИ-анализа ({{context}})."""
    s = state
    out = [f"- пользователь: {names.user}"]
    if s["time"].get("date") or s["time"].get("time"):
        out.append(f"- время: {s['time'].get('date', '')} {s['time'].get('time', '')}".rstrip())
    if s["scene"].get("location"):
        out.append(f"- место: {s['scene']['location']}")
    if s["scene"].get("atmosphere"):
        out.append(f"- атмосфера: {s['scene']['atmosphere']}")
    chars = s["scene"].get("characters") or []
    if chars:
        out.append("- присутствуют: " + ", ".join(
            f"{c} ({_costume_for(s['costumes'], c)})" if _costume_for(s["costumes"], c) else c
            for c in chars[:12]))
    if s["npcs"]:
        out.append("- NPC: " + ", ".join(
            f"{n}({v.get('relationship')})" if v.get("relationship") else n
            for n, v in list(s["npcs"].items())[:15]))
    if s["items"]:
        out.append("- предметы: " + ", ".join(
            f"{i.get('icon') or ''}{n}={i.get('holder') or ''}" + (f"@{i['location']}" if i.get("location") else "")
            for n, i in list(s["items"].items())[:20]))
    moods = [f"{n}:{m}" for n, m in s["mood"].items() if n in chars]
    if moods:
        out.append("- настроение: " + ", ".join(moods))
    return "\n".join(out)


def message_brief(meta: dict | None) -> str | None:
    """Строка под ответом в чате: «2026/2/4 15:00 · Таверна · 3 перс. · ●Событие…»."""
    if not meta_has_data(meta):
        return None
    parts = []
    t = meta.get("time") or {}
    when = " ".join(x for x in (t.get("date"), t.get("time")) if x)
    if when:
        parts.append(when)
    sc = meta.get("scene") or {}
    if sc.get("location"):
        parts.append(sc["location"])
    if sc.get("characters"):
        parts.append(f"{len(sc['characters'])} перс.")
    events = [e for e in meta.get("events") or [] if e.get("text")]
    if events:
        first = events[0]
        text = first["text"] if len(first["text"]) <= 90 else first["text"][:87].rstrip() + "…"
        more = f" (+{len(events) - 1})" if len(events) > 1 else ""
        parts.append(f"{LEVEL_MARK.get(first.get('level'), '○')}{text}{more}")
    if meta.get("items"):
        parts.append(f"предметов: {len(meta['items'])}")
    return " · ".join(parts) or "данные Horae"


def build_document(meta: dict | None) -> str:
    """
    Документ сообщения для поиска (порт buildVectorDocument): события, строка
    «место · персонажи · дата время» и RPG. Внешность, костюмы и предметы
    намеренно не входят — они повторяются от хода к ходу и забивают сходство.
    """
    if not isinstance(meta, dict):
        return ""
    blocks = []
    events = [e["text"].strip() for e in meta.get("events") or [] if (e or {}).get("text")]
    if events:
        blocks.append("\n".join(events))
    sc = meta.get("scene") or {}
    t = meta.get("time") or {}
    anchor = " ".join(x for x in (
        sc.get("location") or "", " ".join(sc.get("characters") or []),
        " ".join(y for y in (t.get("date"), t.get("time")) if y)) if x)
    if anchor:
        blocks.append(anchor)
    if meta.get("rpg"):
        rpg_lines = horae_rpg.document_lines(meta["rpg"])
        if rpg_lines:
            blocks.append("\n".join(rpg_lines))
    return "\n\n".join(blocks).strip()


# ===========================================================================
# Состояние для API
# ===========================================================================

def affection_level(value: float) -> str:
    """Словесный уровень расположения (у плагина пороги с китайскими подписями)."""
    for bound, word in ((80, "обожание"), (60, "близость"), (40, "дружба"), (20, "симпатия"),
                        (0, "нейтрально"), (-20, "холодность"), (-40, "неприязнь"), (-60, "вражда")):
        if value >= bound:
            return word
    return "ненависть"


def to_api(state: dict, *, settings: dict, names: Names, pinned=(), favorite=(), calendar=None) -> dict:
    """STATE → словарь для GET /horae/state (списки вместо словарей, вычисленные поля)."""
    date = state["time"].get("date") or ""
    present = set(state["scene"].get("characters") or [])
    loc = state["scene"].get("location") or ""
    found = find_location(state, loc, state.get("prev_location") or "") if loc else None
    parent_desc = ""
    parts = [p.strip() for p in _LOC_SEP_RE.split(loc) if p.strip()] if loc else []
    if len(parts) > 1:
        entry = (state.get("locations") or {}).get(parts[0])
        if entry and not entry.get("deleted") and (not found or found[0] != parts[0]):
            parent_desc = entry.get("desc") or ""
    pinned, favorite = set(pinned or ()), set(favorite or ())
    npcs = []
    for name, npc in state["npcs"].items():
        npcs.append({
            "id": npc.get("id"), "name": name, **{f: npc.get(f) or "" for f in NPC_FIELDS},
            "age_display": horae_time.current_age(npc.get("age") or "", npc.get("birthday") or "",
                                                  npc.get("age_ref") or "", date, calendar)
            if npc.get("age") else "",
            "aliases": list(npc.get("aliases") or []), "present": name in present,
            "pinned": name in pinned or name == names.char, "favorite": name in favorite,
            "first_mid": npc.get("first_mid"), "last_mid": npc.get("last_mid"), "user": bool(npc.get("user")),
        })
    return {
        "time": {"date": date, "time": state["time"].get("time") or "",
                 "display": horae_time.format_date(date, state["time"].get("time") or "", calendar)
                 if (date or state["time"].get("time")) else ""},
        "scene": {"location": loc, "atmosphere": state["scene"].get("atmosphere") or "",
                  "characters": list(state["scene"].get("characters") or []),
                  "desc": found[1].get("desc") if found else "", "parent_desc": parent_desc},
        "costumes": dict(state["costumes"]),
        "mood": dict(state["mood"]),
        "items": [{"id": i.get("id"), "name": n, "icon": i.get("icon") or "",
                   "importance": i.get("importance") or "", "holder": i.get("holder") or "",
                   "location": i.get("location") or "", "description": i.get("description") or "",
                   "locked": bool(i.get("locked")), "mid": i.get("mid")}
                  for n, i in state["items"].items()],
        "affection": [{"name": n, "value": v, "level": affection_level(v)} for n, v in state["affection"].items()],
        "npcs": npcs,
        "agenda": [dict(a) for a in state.get("agenda") or []],
        "relationships": [{k: x.get(k) for k in ("from", "to", "type", "note")} | {"user": bool(x.get("user"))}
                          for x in state.get("relationships") or []],
        "locations": [{"name": n, "desc": e.get("desc") or "", "current": bool(found and found[0] == n),
                       "user": bool(e.get("user")), "aliases": list(e.get("aliases") or []), "mid": e.get("mid")}
                      for n, e in (state.get("locations") or {}).items() if not e.get("deleted")],
        "rpg": state.get("rpg") if settings.get("rpg_enabled") else None,
    }


_OP_LABELS = {
    "npc.set": "NPC «{name}»: изменён профиль",
    "npc.add": "Добавлен NPC «{name}»",
    "npc.rename": "NPC «{from}» переименован в «{to}»",
    "npc.delete": "Удалён NPC «{name}»",
    "npc.pin": "NPC «{name}»: главный персонаж",
    "npc.favorite": "NPC «{name}»: отметка",
    "affection.set": "Расположение {name} = {value}",
    "affection.delete": "Удалено расположение {name}",
    "item.set": "Предмет «{name}»: изменён",
    "item.add": "Добавлен предмет «{name}»",
    "item.delete": "Удалён предмет «{name}»",
    "item.lock": "Предмет «{name}»: защита",
    "agenda.add": "План: «{text}»",
    "agenda.edit": "План изменён: «{text}»",
    "agenda.delete": "План удалён: «{text}»",
    "rel.set": "Отношения {from} → {to}: {type}",
    "rel.delete": "Удалены отношения {from} → {to}",
    "location.set": "Память сцены «{name}»",
    "location.rename": "Место «{from}» → «{to}»",
    "location.delete": "Удалена память сцены «{name}»",
    "location.merge": "Место «{from}» объединено с «{to}»",
    "scene.set": "Поправлена сцена",
    "costume.set": "Костюм: {name}",
    "mood.set": "Настроение: {name}",
}


def op_label(op: dict) -> str:
    """Подпись правки для журнала во вкладке «Настройки»."""
    kind = op.get("kind") or ""
    template = _OP_LABELS.get(kind)
    if template is None:
        if kind.startswith("rpg."):
            return f"RPG: {kind[4:]} — {op.get('owner') or op.get('path') or ''}".rstrip(" —")
        return kind

    class _Safe(dict):
        def __missing__(self, key):
            return ""

    text = template.format_map(_Safe({k: (str(v)[:60] if v is not None else "") for k, v in op.items()}))
    return text
