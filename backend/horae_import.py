"""
Импорт данных плагина Horae (SillyTavern) в форматы Horae State Engine.

Плагин хранит мету каждого сообщения в `chat[i].horae_meta` — с китайскими
уровнями событий, `{type, value}` у расположения, ячейками таблиц «r-c», — а
данные всего чата в `chat[0].horae_meta`: свёртки хронологии, память сцен,
правки пользователя («надгробия» удалённого), таблицы, настройки RPG. Здесь это
переводится в нашу META сообщения (дизайн §4.2) и HoraeChatState.data (§4.3).

Модуль чистый: без БД и сети. Индексы сообщений файла в id нашей БД переводит
вызывающий (параметр `index_to_mid`).

Импорт терпим к мусору: файлы чатов правят руками, старые версии плагина писали
другие поля. Битый кусок пропускается — импорт не должен падать из-за одной
кривой записи в тысяче сообщений.
"""
import copy
import logging
import math
import re
from bisect import bisect_left, bisect_right
from datetime import datetime, timezone

from backend.horae_time import split_time

logger = logging.getLogger("aichat.horae")

NPC_FIELDS = ("appearance", "personality", "relationship", "gender", "age", "race",
              "job", "birthday", "note")

# Уровни событий: китайские перечисления плагина + английские/русские слова
# (плагин понимал только critical/important, «key» и «важное» уходили в «обычное»).
_LEVELS = {
    "critical": "critical", "key": "critical", "关键": "critical", "關鍵": "critical",
    "ключевое": "critical", "критическое": "critical", "критичное": "critical",
    "important": "important", "重要": "important", "важное": "important", "важно": "important",
    "normal": "normal", "general": "normal", "一般": "normal", "обычное": "normal",
}
# Уровни карточек свёрток и пересказов переноса: такие «события» — не события,
# они вернутся через свёртки (convert_chat_meta).
_SUMMARY_LEVELS = {"摘要", "回顾", "summary"}
_IMPORTANCE = {
    "!": "!", "!!": "!!", "重要": "!", "important": "!", "важно": "!", "важное": "!",
    "关键": "!!", "關鍵": "!!", "critical": "!!", "критич.": "!!", "критично": "!!",
    "ключевое": "!!",
}
_RPG_RENAMES = {"removedSkills": "skills_removed", "attributes": "attrs",
                "equipment": "equip", "baseChanges": "base"}
_CELL_RE = re.compile(r"\s*(\d+)\s*[-,]\s*(\d+)\s*")
# Поля меты, по которым решаем «есть ли что импортировать».
_CONTENT_KEYS = ("time", "scene", "costumes", "mood", "items", "items_removed", "events",
                 "affection", "npcs", "agenda", "agenda_done", "relationships", "tables", "rpg")


# ---- Мелкие помощники ------------------------------------------------------

def _s(value) -> str:
    """Строка без пробелов по краям; числа — строкой, прочее — ""."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, bool) or value is None:
        return ""
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            return ""
        return str(int(value)) if value.is_integer() else str(value)
    return ""


def _int(value) -> int | None:
    """parseInt из JS: ведущие цифры строки, иначе None."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if math.isfinite(value) else None
    m = re.match(r"\s*([+-]?\d+)", str(value))
    return int(m.group(1)) if m else None


def _float(value) -> float | None:
    """parseFloat из JS: «+5» → 5.0, «18(+0)» → 18.0; мусор — None."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        return v if math.isfinite(v) else None
    m = re.match(r"\s*([+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)", str(value))
    return float(m.group(1)) if m else None


def _dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _list(value) -> list:
    return value if isinstance(value, list) else []


def _unique(values) -> list:
    out = []
    for v in values:
        if v and v not in out:
            out.append(v)
    return out


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _safe(fn, *args, default=None):
    """Один кривой раздел меты не должен ронять импорт всего сообщения."""
    try:
        return fn(*args)
    except Exception:  # noqa: BLE001 — входные данные произвольны
        logger.debug("horae import: пропущен раздел %s", getattr(fn, "__name__", fn), exc_info=True)
        return default


def _cell_key(key) -> str | None:
    """«2-3» (плагин) или «2,3» → «2,3»."""
    m = _CELL_RE.fullmatch(str(key))
    return f"{int(m.group(1))},{int(m.group(2))}" if m else None


def _cells(data) -> dict:
    out = {}
    for k, v in _dict(data).items():
        key, val = _cell_key(k), _s(v)
        if key and val:
            out[key] = val
    return out


def _item_name(name) -> str:
    # U+FE0F после эмодзи плагин оставлял в имени — «🍺️Пиво» и «Пиво» расходились.
    return _s(name).replace("️", "").strip()


# ---- Мета сообщения --------------------------------------------------------

def _level(raw, is_important=False) -> str:
    level = _LEVELS.get(_s(raw).lower())
    if level:
        return level
    return "important" if is_important else "normal"


def _importance(raw) -> str:
    return _IMPORTANCE.get(_s(raw).lower(), "")


def _is_summary_event(event: dict) -> bool:
    return bool(event.get("isSummary") or event.get("_summaryId") or event.get("_carryoverSeed")
                or _s(event.get("level")) in _SUMMARY_LEVELS)


def _conv_time(meta: dict):
    ts = _dict(meta.get("timestamp"))
    date, time = _s(ts.get("story_date")), _s(ts.get("story_time"))
    if date and not time:
        date, time = split_time(date)
    if not date and not time:
        return None
    return {"date": date, "time": time}


def _conv_scene(meta: dict) -> dict:
    scene = _dict(meta.get("scene"))
    out = {}
    location, atmosphere = _s(scene.get("location")), _s(scene.get("atmosphere"))
    if location:
        out["location"] = location
    if atmosphere:
        out["atmosphere"] = atmosphere
    chars = scene.get("characters_present")
    if isinstance(chars, str):
        chars = re.split(r"[,，]", chars)
    chars = _unique(_s(c) for c in _list(chars))
    if chars:
        out["characters"] = chars
    # Пары место→описание плагин не сохранял (только последнюю scene_desc) —
    # берём, что есть: пары, если уцелели, иначе scene_desc при месте блока.
    desc = []
    for pair in _list(scene.get("_descPairs")):
        loc, text = _s(_dict(pair).get("location")), _s(_dict(pair).get("desc"))
        if loc and text:
            desc = [d for d in desc if d["location"] != loc] + [{"location": loc, "desc": text}]
    scene_desc = _s(scene.get("scene_desc"))
    if not desc and scene_desc and location:
        desc = [{"location": location, "desc": scene_desc}]
    if desc:
        out["desc"] = desc
    return out


def _conv_str_map(value) -> dict:
    out = {}
    for k, v in _dict(value).items():
        key, val = _s(k), _s(v)
        if key and val:
            out[key] = val
    return out


def _conv_items(meta: dict) -> dict:
    out = {}
    for name, info in _dict(meta.get("items")).items():
        key = _item_name(name)
        if not key or not isinstance(info, dict):
            continue
        item = {"icon": _s(info.get("icon")), "importance": _importance(info.get("importance")),
                "holder": _s(info.get("holder")), "location": _s(info.get("location"))}
        # Нет описания — ключа нет: «не перезаписывать» при повторе.
        description = _s(info.get("description"))
        if description:
            item["description"] = description
        out[key] = item
    return out


def _conv_events(meta: dict) -> list:
    events = _list(meta.get("events"))
    if not events and isinstance(meta.get("event"), dict):
        events = [meta["event"]]  # старый формат: одно событие
    out = []
    for event in events:
        if isinstance(event, str):
            text, level = event.strip(), "normal"
        elif isinstance(event, dict):
            if _is_summary_event(event):
                continue
            text = _s(event.get("summary")) or _s(event.get("text"))
            level = _level(event.get("level"), bool(event.get("is_important")))
        else:
            continue
        if text:
            out.append({"level": level, "text": text})
    return out


def _conv_affection(meta: dict) -> dict:
    out = {}
    for name, value in _dict(meta.get("affection")).items():
        key = _s(name)
        if not key:
            continue
        if isinstance(value, dict):
            mode = "set" if _s(value.get("type")) == "absolute" else "add"
            number = _float(value.get("value"))
        else:
            mode, number = "add", _float(value)  # старое число/строка — это сдвиг
        if number is not None:
            out[key] = {"mode": mode, "value": number}
    return out


def _conv_npcs(meta: dict) -> dict:
    out = {}
    for name, info in _dict(meta.get("npcs")).items():
        key = _s(name)
        if not key or not isinstance(info, dict):
            continue
        # Только пришедшие поля: пустое поле в мете значит «не менялось».
        out[key] = {f: _s(info.get(f)) for f in NPC_FIELDS if _s(info.get(f))}
    return out


def _conv_agenda(meta: dict) -> list:
    out, seen = [], set()
    for item in _list(meta.get("agenda")):
        if isinstance(item, str):
            item = {"text": item}
        if not isinstance(item, dict) or item.get("_deleted") or item.get("done") is True:
            continue
        text = _s(item.get("text"))
        if text and text not in seen:
            seen.add(text)
            out.append({"date": _s(item.get("date")), "text": text})
    return out


def _conv_relationships(meta: dict) -> list:
    out = []
    for rel in _list(meta.get("relationships")):
        # Правленные пользователем отношения живут в chat[0] и приходят правкой
        # rel.set (convert_chat_meta), а не как слова ИИ.
        if not isinstance(rel, dict) or rel.get("_userEdited"):
            continue
        a, b, kind = _s(rel.get("from")), _s(rel.get("to")), _s(rel.get("type"))
        if a and b and kind:
            out.append({"from": a, "to": b, "type": kind, "note": _s(rel.get("note"))})
    return out


def _conv_tables(meta: dict) -> list:
    out = []
    for contrib in _list(meta.get("tableContributions")):
        # Снимок правки пользователя уже в данных таблицы (chat[0]) — повторять
        # его как вклад ИИ нельзя: он обошёл бы блокировки ячеек.
        if not isinstance(contrib, dict) or contrib.get("_isUserEdit"):
            continue
        name, cells = _s(contrib.get("name")), _cells(contrib.get("updates"))
        if name and cells:
            out.append({"name": name, "cells": cells})
    return out


def _conv_rpg(meta: dict) -> dict:
    changes = meta.get("_rpgChanges")
    if not isinstance(changes, dict):
        return {}
    out = {}
    for key, value in changes.items():
        if not isinstance(key, str) or key.startswith("_"):
            continue
        key = _RPG_RENAMES.get(key, key)
        if key == "currency":
            value = [{"owner": _s(c.get("owner")), "name": _s(c.get("name")), "value": _int(c.get("value")),
                      "delta": bool(c.get("delta", c.get("isDelta", False)))}
                     for c in _list(value) if isinstance(c, dict) and _int(c.get("value")) is not None]
        if not isinstance(value, (dict, list)) or not value:
            continue
        out[key] = copy.deepcopy(value)
    return out


def _convert_plugin_meta(meta: dict) -> dict:
    out = {
        "time": _safe(_conv_time, meta),
        "scene": _safe(_conv_scene, meta, default={}),
        "costumes": _safe(_conv_str_map, meta.get("costumes"), default={}),
        "mood": _safe(_conv_str_map, meta.get("mood"), default={}),
        "items": _safe(_conv_items, meta, default={}),
        "items_removed": _safe(lambda: _unique(_item_name(x) for x in _list(meta.get("deletedItems"))),
                               default=[]),
        "events": _safe(_conv_events, meta, default=[]),
        "affection": _safe(_conv_affection, meta, default={}),
        "npcs": _safe(_conv_npcs, meta, default={}),
        "agenda": _safe(_conv_agenda, meta, default=[]),
        "agenda_done": _safe(lambda: _unique(_s(x) for x in _list(meta.get("deletedAgenda"))), default=[]),
        "relationships": _safe(_conv_relationships, meta, default=[]),
        "tables": _safe(_conv_tables, meta, default=[]),
        "rpg": _safe(_conv_rpg, meta, default={}),
    }
    return out


def _merge_meta(base: dict, inline: dict) -> dict:
    """Мета из horae_meta + мета из встроенных тегов того же сообщения.

    Теги в тексте — то, что модель написала последним (плагин переписывал их
    при правке), поэтому скаляры берутся из тегов; словари сливаются с
    приоритетом тегов, списки объединяются. События и отношения заменяются
    целиком, если в тегах они есть, — как mergeParsedToMeta плагина: иначе одни
    и те же события пришли бы дважды в разной редакции.
    """
    out = copy.deepcopy(base)
    t_base, t_in = _dict(out.get("time")), _dict(inline.get("time"))
    date = _s(t_in.get("date")) or _s(t_base.get("date"))
    time = _s(t_in.get("time")) or _s(t_base.get("time"))
    out["time"] = {"date": date, "time": time} if date or time else None

    s_base, s_in = _dict(out.get("scene")), _dict(inline.get("scene"))
    scene = dict(s_base)
    for key in ("location", "atmosphere"):
        if _s(s_in.get(key)):
            scene[key] = _s(s_in.get(key))
    chars = [c for c in (_s(x) for x in _list(s_in.get("characters"))) if c]
    if chars:
        scene["characters"] = chars
    desc = {d["location"]: d for d in _list(s_base.get("desc")) if isinstance(d, dict) and d.get("location")}
    for d in _list(s_in.get("desc")):
        if isinstance(d, dict) and _s(d.get("location")) and _s(d.get("desc")):
            desc[_s(d["location"])] = {"location": _s(d["location"]), "desc": _s(d["desc"])}
    if desc:
        scene["desc"] = list(desc.values())
    out["scene"] = scene

    for key in ("costumes", "mood", "affection", "items"):
        merged = dict(_dict(out.get(key)))
        merged.update(copy.deepcopy(_dict(inline.get(key))))
        out[key] = merged
    npcs = copy.deepcopy(_dict(out.get("npcs")))
    for name, fields in _dict(inline.get("npcs")).items():
        if isinstance(fields, dict):
            npcs[name] = {**_dict(npcs.get(name)), **copy.deepcopy(fields)}
    out["npcs"] = npcs

    for key in ("items_removed", "agenda_done"):
        out[key] = _unique(list(_list(out.get(key))) + [_s(x) for x in _list(inline.get(key))])
    for key in ("events",):
        if _list(inline.get(key)):
            out[key] = copy.deepcopy(inline[key])
    rels = {(r.get("from"), r.get("to")): r for r in _list(out.get("relationships")) if isinstance(r, dict)}
    for r in _list(inline.get("relationships")):
        if isinstance(r, dict):
            rels[(r.get("from"), r.get("to"))] = copy.deepcopy(r)
    out["relationships"] = list(rels.values())
    agenda = list(_list(out.get("agenda")))
    texts = {a.get("text") for a in agenda if isinstance(a, dict)}
    for a in _list(inline.get("agenda")):
        if isinstance(a, dict) and a.get("text") and a.get("text") not in texts:
            texts.add(a["text"])
            agenda.append(copy.deepcopy(a))
    out["agenda"] = agenda

    tables = {t["name"]: {"name": t["name"], "cells": dict(t.get("cells") or {})}
              for t in _list(out.get("tables")) if isinstance(t, dict) and t.get("name")}
    for t in _list(inline.get("tables")):
        if isinstance(t, dict) and t.get("name") and isinstance(t.get("cells"), dict):
            tables.setdefault(t["name"], {"name": t["name"], "cells": {}})["cells"].update(t["cells"])
    out["tables"] = list(tables.values())

    rpg = copy.deepcopy(_dict(out.get("rpg")))
    for key, value in _dict(inline.get("rpg")).items():
        if isinstance(value, dict) and isinstance(rpg.get(key), dict):
            rpg[key] = {**rpg[key], **copy.deepcopy(value)}
        elif value:
            rpg[key] = copy.deepcopy(value)
    out["rpg"] = rpg
    if _s(inline.get("raw")):
        out["raw"] = inline["raw"]
    return out


def _prune(meta: dict) -> dict:
    """Пустые поля опускаются (дизайн §4.2 это разрешает)."""
    out = {}
    for key, value in meta.items():
        if key == "scene" and isinstance(value, dict):
            value = {k: v for k, v in value.items() if v not in ("", None, [], {})}
        elif key == "time" and isinstance(value, dict):
            value = value if _s(value.get("date")) or _s(value.get("time")) else None
        if value in (None, "", [], {}):
            continue
        out[key] = value
    return out


def is_side(horae_meta) -> bool:
    """Побочная сцена (`_skipHorae` плагина): сообщение не участвует в состоянии."""
    return isinstance(horae_meta, dict) and bool(horae_meta.get("_skipHorae"))


def convert_message_meta(horae_meta: dict, *, text_meta: dict | None = None) -> dict | None:
    """horae_meta сообщения плагина → наша META (source="import") или None.

    `text_meta` — мета, разобранная из встроенных тегов того же сообщения;
    при слиянии скаляры берутся из неё. Карточки свёрток и пересказы переноса
    пропускаются: они вернутся свёртками через convert_chat_meta. Побочную
    сцену вызывающий узнаёт через is_side(horae_meta).
    """
    try:
        meta = _convert_plugin_meta(horae_meta) if isinstance(horae_meta, dict) else {}
        if isinstance(text_meta, dict) and text_meta:
            meta = _merge_meta(meta, text_meta)
        meta = _prune(meta)
    except Exception:  # noqa: BLE001 — страховка поверх _safe: мета произвольна
        logger.warning("horae import: мета сообщения пропущена", exc_info=True)
        return None
    if not any(key in meta for key in _CONTENT_KEYS):
        return None
    meta["source"] = "import"
    return meta


def summary_card_texts(horae_meta) -> dict:
    """{id свёртки: текст} из карточек свёрток в событиях сообщения.

    Старые версии плагина хранили текст свёртки только в карточке (событие с
    `_summaryId`), а `summaryText` в chat[0] появился позже. Вызывающий
    собирает карточки всех сообщений и передаёт их в convert_chat_meta.
    """
    out = {}
    for event in _list(_dict(horae_meta).get("events")):
        if isinstance(event, dict) and event.get("_summaryId") and _s(event.get("summary")):
            out[_s(event["_summaryId"])] = _s(event["summary"])
    return out


# ---- Данные чата -----------------------------------------------------------

class _Seq:
    """Общий счётчик id правок и свёрток: движок продолжит его с data["seq"]."""

    def __init__(self):
        self.value = 0

    def next(self) -> int:
        self.value += 1
        return self.value


def _empty_chat_data() -> dict:
    return {"v": 1, "ops": [], "summaries": [], "tables": [], "table_overlays": {},
            "rpg_config": {}, "settings": {}, "seed": None, "pinned_npcs": [],
            "favorite_npcs": [], "scan": None, "summary_error": None, "seq": 0}


def _index_map(index_to_mid) -> dict[int, int]:
    out = {}
    for k, v in _dict(index_to_mid).items():
        idx, mid = _int(k), _int(v)
        if idx is not None and mid is not None:
            out[idx] = mid
    return out


class _RangeMapper:
    """Диапазон индексов файла → диапазон id БД.

    Не у каждого индекса есть id (системные сообщения при импорте пропускаются),
    поэтому край диапазона сдвигается к ближайшему существующему сообщению
    внутри диапазона. Внутри нет ни одного — свёртку не к чему привязать.
    """

    def __init__(self, mapping: dict[int, int]):
        self.mapping = mapping
        self.keys = sorted(mapping)

    def map(self, lo: int, hi: int):
        if lo > hi:
            lo, hi = hi, lo
        i, j = bisect_left(self.keys, lo), bisect_right(self.keys, hi) - 1
        if i > j:
            return None
        return [self.mapping[self.keys[i]], self.mapping[self.keys[j]]]


def _summary_kind(entry: dict) -> str:
    sid = _s(entry.get("id"))
    for prefix, kind in (("as_", "auto"), ("ms_", "manual"), ("cs_", "compress")):
        if sid.startswith(prefix):
            return kind
    if entry.get("auto"):
        return "auto"
    return "manual" if entry.get("manual") else "compress"


def _entry_range(entry: dict):
    rng = entry.get("range")
    if isinstance(rng, list) and len(rng) >= 2:
        lo, hi = _int(rng[0]), _int(rng[1])
        if lo is not None and hi is not None:
            return lo, hi
    covered = [i for i in (_int(x) for x in _list(entry.get("coveredIndices"))) if i is not None]
    return (min(covered), max(covered)) if covered else None


def _conv_summary(entry, mapper: _RangeMapper, texts: dict, seq: _Seq, now: str):
    if not isinstance(entry, dict):
        return None
    rng = _entry_range(entry)
    mapped = mapper.map(*rng) if rng else None
    text = _s(entry.get("summaryText")) or texts.get(_s(entry.get("id")), "")
    if not mapped or not text:
        return None
    sid = f"s_{seq.next()}"
    children = [c for c in (_conv_summary(x, mapper, texts, seq, now)
                            for x in _list(entry.get("mergedSummaries"))) if c]
    originals = [e for e in _list(entry.get("originalEvents")) if isinstance(e, dict)]
    dates = [d for d in (_s(_dict(e.get("timestamp")).get("story_date")) for e in originals) if d]
    if not dates:
        dates = [d for c in children for d in (c["date_from"], c["date_to"]) if d]
    depth = _int(entry.get("depth"))
    return {
        "id": sid, "kind": _summary_kind(entry), "range": mapped, "text": text,
        "depth": depth if depth and depth > 0 else 1,
        "active": entry.get("active") is not False,
        "created_at": _s(entry.get("createdAt")) or now,
        "date_from": dates[0] if dates else "", "date_to": dates[-1] if dates else "",
        "events": len(originals), "children": children,
    }


def _conv_summaries(meta: dict, mapper: _RangeMapper, texts: dict, seq: _Seq, now: str) -> list:
    out = []
    # Пересказы, перенесённые плагином из прошлого чата, — события-карточки
    # «回顾» в chat[0]; у нас это свёртки kind="carry" в начале хронологии.
    for event in _list(meta.get("events")):
        if isinstance(event, dict) and event.get("_carryoverSeed") and _s(event.get("summary")):
            out.append({"id": f"s_{seq.next()}", "kind": "carry", "range": [0, 0],
                        "text": _s(event["summary"]), "depth": 1, "active": True,
                        "created_at": now, "date_from": "", "date_to": "", "events": 0,
                        "children": []})
    for entry in _list(meta.get("autoSummaries")):
        summary = _safe(_conv_summary, entry, mapper, texts, seq, now)
        if summary:
            out.append(summary)
    return out


def _location_ops(meta: dict) -> list:
    ops = []
    for name, entry in _dict(meta.get("locationMemory")).items():
        key = _s(name)
        if not key or not isinstance(entry, dict):
            continue
        # Описания ИИ пересоберутся из scene_desc сообщений; переносим только
        # то, что сделал пользователь: правки и надгробия удалённых мест.
        if entry.get("_deleted"):
            ops.append({"kind": "location.delete", "name": key})
        elif entry.get("_userEdited"):
            ops.append({"kind": "location.set", "name": key, "desc": _s(entry.get("desc"))})
    return ops


def _relationship_ops(meta: dict) -> list:
    ops = []
    for rel in _list(meta.get("relationships")):
        if not isinstance(rel, dict) or not rel.get("_userEdited"):
            continue
        a, b, kind = _s(rel.get("from")), _s(rel.get("to")), _s(rel.get("type"))
        if a and b and kind:
            ops.append({"kind": "rel.set", "from": a, "to": b, "type": kind, "note": _s(rel.get("note"))})
    return ops


def _agenda_ops(meta: dict) -> list:
    ops, seen = [], set()
    for item in _list(meta.get("agenda")):
        # В chat[0].agenda лежат и пункты пользователя, и пункты ИИ из приветствия;
        # пункты ИИ придут с метой сообщения.
        if not isinstance(item, dict) or _s(item.get("source")) == "ai":
            continue
        if item.get("_deleted") or item.get("done") is True:
            continue
        text = _s(item.get("text"))
        if text and text not in seen:
            seen.add(text)
            ops.append({"kind": "agenda.add", "date": _s(item.get("date")), "text": text})
    for text in _unique(_s(x) for x in _list(meta.get("_deletedAgendaTexts"))):
        ops.append({"kind": "agenda.delete", "text": text})
    return ops


def _npc_ops(meta: dict) -> list:
    return [{"kind": "npc.delete", "name": name}
            for name in _unique(_s(x) for x in _list(meta.get("_deletedNpcs")))]


def _rpg_sources(meta: dict) -> tuple[dict, dict]:
    """(_rpgConfigs, rpg) — конфиги плагин держал в обоих местах (миграция)."""
    return _dict(meta.get("_rpgConfigs")), _dict(meta.get("rpg"))


def _stronghold_path(node: dict, by_id: dict) -> str:
    parts, seen = [], set()
    while isinstance(node, dict) and _s(node.get("id")) not in seen:
        seen.add(_s(node.get("id")))
        if _s(node.get("name")):
            parts.append(_s(node["name"]))
        node = by_id.get(_s(node.get("parent")))
    return ">".join(reversed(parts))


def _rpg_ops(meta: dict) -> list:
    """Ручные данные RPG → правки (лучшее, что можно восстановить).

    Плагин отличал ручное от накопленного ИИ флагами: `_userAdded` у навыков и
    опорных пунктов, `_userEdited` у репутации, `_deletedSkills`. Остальное
    (полосы, атрибуты, валюта) пересоберётся повтором `rpg` из мет сообщений.
    """
    cfgs, rpg = _rpg_sources(meta)
    ops = []
    for owner, skills in _dict(rpg.get("skills")).items():
        for skill in _list(skills):
            if isinstance(skill, dict) and skill.get("_userAdded") and _s(skill.get("name")) and _s(owner):
                ops.append({"kind": "rpg.skill.add", "owner": _s(owner), "name": _s(skill["name"]),
                            "level": _s(skill.get("level")), "desc": _s(skill.get("desc"))})
    deleted = _list(cfgs.get("_deletedSkills")) or _list(rpg.get("_deletedSkills"))
    for entry in deleted:
        if isinstance(entry, dict) and _s(entry.get("owner")) and _s(entry.get("name")):
            ops.append({"kind": "rpg.skill.delete", "owner": _s(entry["owner"]), "name": _s(entry["name"])})
    for owner, cats in _dict(rpg.get("reputation")).items():
        for cat, value in _dict(cats).items():
            if isinstance(value, dict) and value.get("_userEdited"):
                number = _int(value.get("value"))
                if number is not None and _s(owner) and _s(cat):
                    ops.append({"kind": "rpg.rep", "owner": _s(owner), "cat": _s(cat), "value": number})
    nodes = [n for n in (_list(cfgs.get("strongholds")) or _list(rpg.get("strongholds"))) if isinstance(n, dict)]
    by_id = {_s(n.get("id")): n for n in nodes if _s(n.get("id"))}
    for node in nodes:
        if not node.get("_userAdded"):
            continue
        path = _stronghold_path(node, by_id)
        if not path:
            continue
        op = {"kind": "rpg.base", "path": path}
        level = _int(node.get("level"))
        if level is not None:
            op["level"] = level
        if _s(node.get("desc")):
            op["desc"] = _s(node["desc"])
        ops.append(op)
    return ops


def _conv_rpg_config(meta: dict) -> dict:
    """Настройки RPG чата (дизайн §9 RPG_CONFIG) из _rpgConfigs/rpg.*Config."""
    cfgs, rpg = _rpg_sources(meta)
    out = {}
    rep = _dict(cfgs.get("reputationConfig")) or _dict(rpg.get("reputationConfig"))
    deleted = set(_s(x) for x in _list(rep.get("_deletedCategories")))
    categories = []
    for cat in _list(rep.get("categories")):
        if not isinstance(cat, dict) or not _s(cat.get("name")) or _s(cat.get("name")) in deleted:
            continue
        lo, hi, default = _int(cat.get("min")), _int(cat.get("max")), _int(cat.get("default"))
        categories.append({"name": _s(cat["name"]), "min": -100 if lo is None else lo,
                           "max": 100 if hi is None else hi, "default": 0 if default is None else default,
                           "sub": _unique(_s(x) for x in _list(cat.get("subItems") or cat.get("sub")))})
    if categories:
        out["reputation"] = categories
    cur = _dict(cfgs.get("currencyConfig")) or _dict(rpg.get("currencyConfig"))
    currencies = []
    for d in _list(cur.get("denominations")):
        if isinstance(d, dict) and _s(d.get("name")):
            rate = _float(d.get("rate"))
            currencies.append({"name": _s(d["name"]), "rate": rate if rate and rate > 0 else 1,
                               "emoji": _s(d.get("emoji"))})
    if currencies:
        out["currencies"] = currencies
    eq = _dict(cfgs.get("equipmentConfig")) or _dict(rpg.get("equipmentConfig"))
    chars = {}
    for owner, cfg in _dict(eq.get("perChar")).items():
        if not _s(owner) or not isinstance(cfg, dict):
            continue
        gone = set(_s(x) for x in _list(cfg.get("_deletedSlots")))

        def slots(raw, gone=gone):
            return [{"name": _s(s.get("name")), "max": max(1, _int(s.get("maxCount") or s.get("max")) or 1)}
                    for s in _list(raw) if isinstance(s, dict) and _s(s.get("name"))
                    and _s(s.get("name")) not in gone]

        forms = [{"id": _s(f.get("id")) or f"form_{i}", "name": _s(f.get("name")), "slots": slots(f.get("slots"))}
                 for i, f in enumerate(_list(cfg.get("forms"))) if isinstance(f, dict)]
        entry = {"slots": slots(cfg.get("slots")), "forms": forms, "form": _s(cfg.get("currentForm"))}
        if entry["slots"] or forms:
            chars[_s(owner)] = entry
    if chars or eq.get("locked"):
        out["equipment"] = {"locked": bool(eq.get("locked")), "chars": chars}
    return out


def _table_payload(table: dict) -> dict:
    """Общее у таблицы и оверлея: размеры и данные с ключами «r,c»."""
    cells = _cells(table.get("data"))
    rows, cols = _int(table.get("rows")) or 2, _int(table.get("cols")) or 2
    for key in cells:
        r, c = (int(x) for x in key.split(","))
        rows, cols = max(rows, r + 1), max(cols, c + 1)
    return {"rows": max(2, rows), "cols": max(2, cols), "cells": cells}


def _conv_local_table(table, index: int, anchor: int) -> dict | None:
    if not isinstance(table, dict):
        return None
    body = _table_payload(table)
    cells = body["cells"]
    locked_cells = _unique(_cell_key(x) for x in _list(table.get("lockedCells")))
    return {
        "id": _s(table.get("id")) or f"t_{index + 1}",
        "name": _s(table.get("name")),
        "prompt": _s(table.get("prompt")),
        "rows": body["rows"], "cols": body["cols"],
        "locked_rows": sorted({i for i in (_int(x) for x in _list(table.get("lockedRows"))) if i is not None}),
        "locked_cols": sorted({i for i in (_int(x) for x in _list(table.get("lockedCols"))) if i is not None}),
        "locked_cells": [k for k in locked_cells if k],
        "headers": {k: v for k, v in cells.items() if k.startswith("0,") or k.endswith(",0")},
        # Данные плагина — уже итог (база + вклады всех сообщений): база с якорем
        # на последнем сообщении, чтобы вклады импортированных мет не легли дважды.
        "base": cells,
        "base_anchor": anchor,
    }


def _conv_overlays(meta: dict, anchor: int) -> dict:
    out = {}
    for source in ("globalTableData", "charTableData"):
        for key, overlay in _dict(meta.get(source)).items():
            if not _s(key) or not isinstance(overlay, dict):
                continue
            body = _table_payload(overlay)
            out[_s(key)] = {"base": body["cells"], "base_anchor": anchor,
                            "rows": body["rows"], "cols": body["cols"]}
    return out


def convert_chat_meta(chat0_meta: dict, index_to_mid: dict[int, int], *,
                      summary_texts: dict | None = None) -> dict:
    """chat[0].horae_meta плагина → HoraeChatState.data (дизайн §4.3).

    `index_to_mid` — индекс сообщения в файле SillyTavern → id в нашей БД.
    `summary_texts` — {id свёртки: текст} из карточек (summary_card_texts) для
    старых свёрток без `summaryText`. Правки получают `at=0` (до всех
    сообщений): это исходные решения пользователя, ИИ поверх них пишет дальше.
    """
    data = _empty_chat_data()
    if not isinstance(chat0_meta, dict):
        return data
    now = _now_iso()
    mapping = _index_map(index_to_mid)
    anchor = max(mapping.values(), default=0)
    seq = _Seq()

    ops = []
    for builder in (_location_ops, _relationship_ops, _agenda_ops, _npc_ops, _rpg_ops):
        ops.extend(_safe(builder, chat0_meta, default=[]))
    for op in ops:
        n = seq.next()
        data["ops"].append({"id": f"op_{n}", "seq": n, "at": 0, **op, "created_at": now})

    texts = {k: v for k, v in ((_s(k), _s(v)) for k, v in _dict(summary_texts).items()) if k and v}
    data["summaries"] = _safe(_conv_summaries, chat0_meta, _RangeMapper(mapping), texts, seq, now,
                              default=[])
    data["tables"] = [t for t in (_safe(_conv_local_table, t, i, anchor)
                                  for i, t in enumerate(_list(chat0_meta.get("customTables")))) if t]
    data["table_overlays"] = _safe(_conv_overlays, chat0_meta, anchor, default={})
    data["rpg_config"] = _safe(_conv_rpg_config, chat0_meta, default={})
    data["seq"] = seq.value
    return data
