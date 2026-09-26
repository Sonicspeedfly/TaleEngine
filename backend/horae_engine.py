"""
Horae State Engine — сервисный слой: БД ↔ чистое ядро (horae_state и др.).

Здесь нет вызовов модели (они в horae_tasks): только чтение/запись мет
сообщений и состояния чата, пересчёт состояния, блоки для промпта, журнал
правок, события, свёртки, таблицы, ветвление, перенос и экспорт.

Запись состояния чата — чтение-правка-запись JSON, поэтому все изменения
идут под замком чата (chat_lock): правка из интерфейса и фоновая свёртка
иначе затирали бы друг друга.
"""
from __future__ import annotations

import asyncio
import copy
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import delete as sql_delete, select

from backend import horae_rpg, horae_settings, horae_state as hs, horae_tables, horae_time
from backend import models

log = logging.getLogger("aichat.horae")

SETTINGS_KEY = "horae"
LIBRARY_KEY = "horae_library"
DATA_VERSION = 1


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Замки
# ---------------------------------------------------------------------------
_locks: dict = {}


def loop_lock(registry: dict, key) -> asyncio.Lock:
    """
    Замок из реестра, привязанный к текущему циклу событий.

    asyncio.Lock привязывается к циклу при первом ожидании; реестр живёт
    дольше цикла (тесты гоняют каждый в своём, TestClient — в своём потоке),
    и замок из чужого цикла дал бы «RuntimeError: … bound to a different
    event loop». Поэтому при смене цикла замок создаётся заново.
    """
    loop = asyncio.get_running_loop()
    entry = registry.get(key)
    if entry is None or entry[0] is not loop:
        entry = registry[key] = (loop, asyncio.Lock())
    return entry[1]


def chat_lock(session_id: int) -> asyncio.Lock:
    """Замок записи данных Horae чата (мета сообщений + состояние чата). Не реентерабелен."""
    return loop_lock(_locks, session_id)


def forget_chat(session_id: int) -> None:
    _locks.pop(session_id, None)


# ---------------------------------------------------------------------------
# Настройки
# ---------------------------------------------------------------------------
async def _app_setting(db, key: str) -> dict:
    row = await db.get(models.AppSetting, key)
    return dict(row.value) if row and isinstance(row.value, dict) else {}


async def _save_app_setting(db, key: str, value: dict) -> None:
    row = await db.get(models.AppSetting, key)
    if row is None:
        db.add(models.AppSetting(key=key, value=value))
    else:
        row.value = value
    await db.flush()


async def global_layer(db) -> dict:
    return horae_settings.sanitize(await _app_setting(db, SETTINGS_KEY))


async def save_global_layer(db, patch: dict) -> dict:
    layer = horae_settings.merge_overrides(await global_layer(db), patch)
    await _save_app_setting(db, SETTINGS_KEY, layer)
    return layer


async def library(db) -> dict:
    lib = await _app_setting(db, LIBRARY_KEY)
    lib.setdefault("global_tables", [])
    lib.setdefault("prompt_presets", [])
    lib.setdefault("equipment_templates", [])
    return lib


async def save_library(db, lib: dict) -> None:
    await _save_app_setting(db, LIBRARY_KEY, lib)


def character_layer(character) -> dict:
    profile = getattr(character, "horae_profile", None) or {}
    return horae_settings.sanitize(profile.get("settings") if isinstance(profile, dict) else {})


def character_tables(character) -> list:
    profile = getattr(character, "horae_profile", None) or {}
    tables = profile.get("tables") if isinstance(profile, dict) else None
    return list(tables) if isinstance(tables, list) else []


async def effective_settings(db, session, character=None, chat_data: dict | None = None) -> dict:
    """Действующие настройки чата: умолчания ← глобальные ← персонаж ← чат."""
    if character is None and session is not None:
        character = await db.get(models.Character, session.character_id)
    if chat_data is None and session is not None:
        chat_data = await load_chat_data(db, session.id)
    return horae_settings.resolve(
        await global_layer(db), character_layer(character), (chat_data or {}).get("settings"))


# ---------------------------------------------------------------------------
# Состояние чата (HoraeChatState.data)
# ---------------------------------------------------------------------------
def blank_chat_data() -> dict:
    return {
        "v": DATA_VERSION, "ops": [], "summaries": [], "tables": [], "table_overlays": {},
        "rpg_config": {}, "settings": {}, "seed": None, "pinned_npcs": [], "favorite_npcs": [],
        "scan": None, "summary_error": None, "seq": 0,
    }


async def load_chat_data(db, session_id: int) -> dict:
    row = await db.get(models.HoraeChatState, session_id)
    data = blank_chat_data()
    if row and isinstance(row.data, dict):
        data.update(copy.deepcopy(row.data))
    return data


async def save_chat_data(db, session_id: int, data: dict) -> None:
    row = await db.get(models.HoraeChatState, session_id)
    payload = copy.deepcopy(data)
    payload["v"] = DATA_VERSION
    if row is None:
        db.add(models.HoraeChatState(session_id=session_id, data=payload))
    else:
        row.data = payload
    await db.flush()


def next_seq(data: dict) -> int:
    data["seq"] = int(data.get("seq") or 0) + 1
    return data["seq"]


# ---------------------------------------------------------------------------
# Меты сообщений
# ---------------------------------------------------------------------------
def active_meta(horae, active_swipe: int = 0) -> dict | None:
    """Мета активного свайпа из Message.horae (или None)."""
    if not isinstance(horae, dict):
        return None
    metas = horae.get("metas") or []
    idx = active_swipe or 0
    if 0 <= idx < len(metas) and isinstance(metas[idx], dict):
        return metas[idx]
    return None


def is_side(horae) -> bool:
    return bool(isinstance(horae, dict) and horae.get("side"))


def with_meta(horae, index: int, meta: dict | None) -> dict:
    """Новый Message.horae, где у свайпа index — мета meta (список растёт)."""
    out = copy.deepcopy(horae) if isinstance(horae, dict) else {}
    metas = list(out.get("metas") or [])
    while len(metas) <= index:
        metas.append(None)
    metas[index] = meta
    out["metas"] = metas
    out.setdefault("side", False)
    return out


def with_appended_meta(horae, swipes_count: int, meta: dict | None) -> dict:
    """Новый свайп дописан в конец: мета встаёт на его индекс (swipes_count − 1)."""
    return with_meta(horae, max(0, swipes_count - 1), meta)


async def load_entries(db, session_id: int) -> list[tuple[int, str, dict | None, bool]]:
    """[(id, роль, мета активного свайпа, побочная)] по возрастанию id — без текстов."""
    rows = (await db.execute(
        select(models.Message.id, models.Message.role, models.Message.horae,
               models.Message.active_swipe)
        .where(models.Message.session_id == session_id)
        .order_by(models.Message.id)
    )).all()
    return [(r.id, r.role, active_meta(r.horae, r.active_swipe), is_side(r.horae)) for r in rows]


# ---------------------------------------------------------------------------
# Имена сцены
# ---------------------------------------------------------------------------
async def scene_names(db, session, character=None) -> hs.Names:
    """Кто пользователь (персона чата, иначе имя аккаунта) и кто персонаж."""
    user = ""
    if session is not None and session.persona_id:
        persona = await db.get(models.Persona, session.persona_id)
        if persona and (persona.name or "").strip():
            user = persona.name.strip()
    if not user and session is not None and session.owner_id:
        owner = await db.get(models.User, session.owner_id)
        if owner:
            user = owner.username
    if character is None and session is not None:
        character = await db.get(models.Character, session.character_id)
    return hs.Names(user=user or "Пользователь", char=getattr(character, "name", "") or "Персонаж")


def parse_context(settings: dict, names: hs.Names) -> hs.ParseContext:
    return hs.ParseContext(user_name=names.user, user_only=settings.get("rpg_user_only") or (),
                           strip_tags=settings.get("strip_tags") or "")


# ---------------------------------------------------------------------------
# Пересчёт состояния
# ---------------------------------------------------------------------------
@dataclass
class Computed:
    settings: dict
    names: hs.Names
    data: dict
    entries: list
    state: dict
    tables: list = field(default_factory=list)
    table_results: dict = field(default_factory=dict)
    calendar: dict | None = None
    until: int | None = None


async def resolved_tables(db, session, character, data: dict) -> list:
    lib = await library(db)
    return horae_tables.resolve(
        lib.get("global_tables") or [], character_tables(character),
        data.get("tables") or [], data.get("table_overlays") or {},
    )


async def compute(db, session, *, character=None, until: int | None = None,
                  settings: dict | None = None, data: dict | None = None) -> Computed:
    """Состояние чата на момент until (None — всё) + таблицы."""
    if character is None:
        character = await db.get(models.Character, session.character_id)
    if data is None:
        data = await load_chat_data(db, session.id)
    if settings is None:
        settings = await effective_settings(db, session, character, data)
    names = await scene_names(db, session, character)
    entries = await load_entries(db, session.id)
    state = hs.replay(entries, ops=data.get("ops") or [], seed=data.get("seed"), until=until,
                      settings=settings, names=names, rpg_config=data.get("rpg_config") or {})
    tables = await resolved_tables(db, session, character, data)
    contributions = [
        (mid, meta.get("tables") or []) for mid, _role, meta, side in entries
        if not side and isinstance(meta, dict) and meta.get("tables")
        and (until is None or mid <= until)
    ]
    results = horae_tables.replay(tables, contributions) if tables else {}
    return Computed(settings=settings, names=names, data=data, entries=entries, state=state,
                    tables=tables, table_results=results,
                    calendar=horae_time.normalize_calendar(settings.get("calendar")), until=until)


def rpg_block(comp: Computed) -> str:
    s = comp.settings
    if not s.get("rpg_enabled"):
        return ""
    npc_ids = {n: v.get("id") for n, v in comp.state["npcs"].items()}
    return horae_rpg.render_block(
        comp.state.get("rpg") or horae_rpg.empty_state(), s,
        present=list(comp.state["scene"].get("characters") or []), npc_ids=npc_ids,
        user_name=comp.names.user, config=comp.data.get("rpg_config") or {})


def tables_block(comp: Computed) -> str:
    return horae_tables.render_block(comp.tables, comp.table_results) if comp.tables else ""


def rules_text(comp: Computed) -> str:
    """Правила тегов для системного промпта (или хвоста)."""
    from backend import horae_prompts

    s = comp.settings
    suffix = None
    if any(t.get("name") for t in comp.tables):
        suffix = horae_tables.rules_suffix(comp.tables, comp.table_results)
    rpg_prompt = ""
    if s.get("rpg_enabled"):
        sections = horae_rpg.rules_sections(
            s, user_name=comp.names.user, present=list(comp.state["scene"].get("characters") or []),
            config=comp.data.get("rpg_config") or {}, state=comp.state.get("rpg") or horae_rpg.empty_state())
        rpg_prompt = horae_rpg.fill_rpg_prompt(horae_prompts.get(s, "rpg") or horae_rpg.DEFAULT_RPG_TEMPLATE,
                                               sections)
    calendar_line = horae_time.calendar_prompt(s.get("calendar")) if (s.get("calendar") or {}).get("enabled") else ""
    return horae_prompts.rules_prompt(
        s, user=comp.names.user, char=comp.names.char, tables_suffix=suffix,
        rpg_prompt=rpg_prompt, calendar_line=calendar_line)


def state_block(comp: Computed) -> str:
    return hs.render_state_block(
        comp.state, comp.settings, names=comp.names, summaries=comp.data.get("summaries") or [],
        tables_block=tables_block(comp), rpg_block=rpg_block(comp),
        pinned=comp.data.get("pinned_npcs") or [], calendar=comp.calendar)


# ---------------------------------------------------------------------------
# Блоки для сборки контекста хода
# ---------------------------------------------------------------------------
@dataclass
class HoraeParts:
    """Что слой Horae добавляет в ход (см. horae_memory.assemble_context)."""
    rules: str = ""
    rules_in_tail: bool = False
    state_block: str = ""
    reminder: str = ""
    recall: list = field(default_factory=list)
    recall_settings: dict = field(default_factory=dict)
    current_date: str = ""
    calendar: dict | None = None
    report: dict = field(default_factory=dict)


async def context_parts(db, session, character, *, until: int | None = None, ooc: bool = False,
                        user_message: str = "", tags: bool = True, connection: dict | None = None,
                        history_ids: list | None = None) -> HoraeParts | None:
    """
    Блоки Horae для хода. None — слой выключен для этого чата.

    :param until: состояние на момент (перегенерация/«Продолжить» — без меты
        перегенерируемого ответа, как skipLast плагина).
    :param ooc: реплика «вне роли» — теги не просим (ни правил, ни напоминания).
    :param tags: False — путь, где теги не нужны вовсе (канвас).
    """
    from backend import horae_prompts

    data = await load_chat_data(db, session.id)
    settings = await effective_settings(db, session, character, data)
    if not settings.get("enabled"):
        return None
    comp = await compute(db, session, character=character, until=until, settings=settings, data=data)
    parts = HoraeParts(current_date=comp.state["time"].get("date") or "", calendar=comp.calendar,
                       recall_settings=settings)
    want_tags = tags and not ooc and settings.get("parse_tags")
    if want_tags:
        parts.rules = rules_text(comp)
        parts.rules_in_tail = settings.get("rules_position") == "tail"
        if settings.get("tag_reminder"):
            parts.reminder = horae_prompts.reminder(settings, user=comp.names.user, char=comp.names.char)
    if settings.get("inject_state"):
        parts.state_block = state_block(comp)
    if settings.get("recall_enabled") and user_message.strip():
        try:
            from backend import horae_vector

            parts.recall = await horae_vector.recall(
                db, session, comp, user_message=user_message, connection=connection or {},
                history_ids=history_ids)
        except Exception:  # noqa: BLE001 — вспоминание не должно ронять ход
            log.exception("Вспоминание Horae чата %s не сработало", session.id)
            parts.recall = []
    parts.report = {
        "enabled": True, "rules_position": settings.get("rules_position"),
        "tags": bool(want_tags), "until": until,
        "events": len(comp.state.get("events") or []),
        "npcs": len(comp.state["npcs"]), "items": len(comp.state["items"]),
    }
    return parts


# ---------------------------------------------------------------------------
# Сохранение ответа модели
# ---------------------------------------------------------------------------
async def reply_rules(db, session_id: int) -> tuple[dict, hs.Names] | None:
    """Настройки и имена для разбора ответа; None — слой или разбор выключен."""
    session = await db.get(models.ChatSession, session_id)
    if session is None:
        return None
    character = await db.get(models.Character, session.character_id)
    settings = await effective_settings(db, session, character)
    if not settings.get("enabled") or not settings.get("parse_tags"):
        return None
    return settings, await scene_names(db, session, character)


async def split_reply(db, session_id: int, text: str) -> tuple[str, dict | None]:
    """
    Ответ модели → (текст без тегов, мета | None). Слой выключен — теги всё
    равно вырезаются (модель могла дописать их по памяти прошлых ходов), но
    мета не сохраняется.
    """
    try:
        rules = await reply_rules(db, session_id)
    except Exception:  # noqa: BLE001 — сохранение ответа важнее памяти
        log.exception("Horae: настройки чата %s не прочитались", session_id)
        rules = None
    if rules is None:
        return hs.strip_tags_text(text, partial=True), None
    settings, names = rules
    try:
        return hs.parse_reply(text, parse_context(settings, names))
    except Exception:  # noqa: BLE001 — колбэк сохранения глотает ошибки: упади разбор,
        # и ответ не сохранился бы вовсе. Лучше ответ без меты (её доизвлечёт анализ).
        log.exception("Horae: теги ответа в чате %s не разобрались", session_id)
        return hs.strip_tags_text(text, partial=True), None


# ---------------------------------------------------------------------------
# Правки пользователя
# ---------------------------------------------------------------------------
class OpError(ValueError):
    """Правка отклонена (неизвестный вид, пустые поля)."""


_OP_REQUIRED = {
    "npc.set": ("name",), "npc.add": ("name",), "npc.rename": ("from", "to"), "npc.delete": ("name",),
    "affection.set": ("name", "value"), "affection.delete": ("name",),
    "item.set": ("name",), "item.add": ("name",), "item.delete": ("name",), "item.lock": ("name",),
    "agenda.add": ("text",), "agenda.edit": ("text",), "agenda.delete": ("text",),
    "rel.set": ("from", "to", "type"), "rel.delete": ("from", "to"),
    "location.set": ("name", "desc"), "location.rename": ("from", "to"), "location.delete": ("name",),
    "location.merge": ("from", "to"), "costume.set": ("name",), "mood.set": ("name",),
}


def _trim_payload(value, depth: int = 0):
    """Правка идёт в JSON и в повтор каждого хода — режем строки и вложенность."""
    if depth > 4:
        return None
    if isinstance(value, str):
        return value[:4000]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, list):
        return [_trim_payload(v, depth + 1) for v in value[:200]]
    if isinstance(value, dict):
        return {str(k)[:100]: _trim_payload(v, depth + 1) for k, v in list(value.items())[:100]}
    return None


async def last_message_id(db, session_id: int) -> int:
    row = (await db.execute(
        select(models.Message.id).where(models.Message.session_id == session_id)
        .order_by(models.Message.id.desc()).limit(1)
    )).scalar()
    return int(row or 0)


async def add_op(db, session_id: int, payload: dict) -> dict:
    """Записать правку пользователя в журнал (дизайн §6.2). Возвращает правку."""
    kind = (payload or {}).get("kind") or ""
    if kind in ("npc.pin", "npc.favorite"):
        return await _toggle_npc_list(db, session_id, kind, payload)
    if kind not in hs.OP_KINDS:
        raise OpError(f"неизвестная правка: {kind or '—'}")
    for key in _OP_REQUIRED.get(kind, ()):
        value = payload.get(key)
        if value is None or (isinstance(value, str) and not value.strip()):
            raise OpError(f"не заполнено поле «{key}»")
    async with chat_lock(session_id):
        data = await load_chat_data(db, session_id)
        op = {k: _trim_payload(v) for k, v in payload.items() if k not in ("id", "seq", "at", "created_at")}
        seq = next_seq(data)
        op.update(id=f"op_{seq}", seq=seq, at=await last_message_id(db, session_id), created_at=now_iso())
        data["ops"].append(op)
        await save_chat_data(db, session_id, data)
        await db.commit()
    return op


async def _toggle_npc_list(db, session_id: int, kind: str, payload: dict) -> dict:
    name = (payload.get("name") or "").strip()
    if not name:
        raise OpError("не заполнено поле «name»")
    key = "pinned_npcs" if kind == "npc.pin" else "favorite_npcs"
    flag = payload.get("pinned") if kind == "npc.pin" else payload.get("favorite")
    async with chat_lock(session_id):
        data = await load_chat_data(db, session_id)
        names = [n for n in data.get(key) or [] if n != name]
        if flag:
            names.append(name)
        data[key] = names
        await save_chat_data(db, session_id, data)
        await db.commit()
    return {"kind": kind, "name": name, "value": bool(flag)}


async def delete_op(db, session_id: int, op_id: str) -> bool:
    async with chat_lock(session_id):
        data = await load_chat_data(db, session_id)
        before = len(data["ops"])
        data["ops"] = [op for op in data["ops"] if op.get("id") != op_id]
        if len(data["ops"]) == before:
            return False
        await save_chat_data(db, session_id, data)
        await db.commit()
    return True


# ---------------------------------------------------------------------------
# Мета сообщения (редактор под сообщением, события хронологии)
# ---------------------------------------------------------------------------
async def set_message_meta(db, message, meta: dict | None) -> None:
    """Заменить мету активного свайпа сообщения (под замком чата — снаружи)."""
    message.horae = with_meta(message.horae, message.active_swipe or 0, meta)
    await db.flush()


async def set_side(db, message, side: bool) -> None:
    horae = copy.deepcopy(message.horae) if isinstance(message.horae, dict) else {"metas": []}
    horae["side"] = bool(side)
    message.horae = horae
    await db.flush()


def message_view(message) -> dict:
    meta = active_meta(message.horae, message.active_swipe)
    return {
        "message_id": message.id, "role": message.role, "meta": meta, "side": is_side(message.horae),
        "swipe": message.active_swipe or 0, "brief": hs.message_brief(meta),
    }


async def insert_event(db, session_id: int, mid: int, level: str, text: str,
                       index: int | None = None) -> None:
    text = (text or "").strip()
    if not text:
        raise OpError("пустое событие")
    async with chat_lock(session_id):
        msg = await db.get(models.Message, mid)
        if msg is None or msg.session_id != session_id:
            raise OpError("сообщение не найдено")
        meta = copy.deepcopy(active_meta(msg.horae, msg.active_swipe)) or {**hs.empty_meta(), "source": "user"}
        events = list(meta.get("events") or [])
        pos = len(events) if index is None else max(0, min(int(index), len(events)))
        events.insert(pos, {"level": hs.normalize_level(level), "text": text[:4000]})
        meta["events"] = events
        await set_message_meta(db, msg, meta)
        await db.commit()


async def edit_event(db, session_id: int, mid: int, i: int, *, level: str | None = None,
                     text: str | None = None) -> None:
    async with chat_lock(session_id):
        msg = await db.get(models.Message, mid)
        if msg is None or msg.session_id != session_id:
            raise OpError("сообщение не найдено")
        meta = copy.deepcopy(active_meta(msg.horae, msg.active_swipe))
        if not meta or not (0 <= i < len(meta.get("events") or [])):
            raise OpError("событие не найдено")
        ev = meta["events"][i]
        if level is not None:
            ev["level"] = hs.normalize_level(level)
        if text is not None:
            if not text.strip():
                meta["events"].pop(i)
            else:
                ev["text"] = text.strip()[:4000]
        await set_message_meta(db, msg, meta)
        await db.commit()


async def delete_events(db, session_id: int, refs: list[dict]) -> int:
    """Удалить события по ссылкам {mid, i}. Индексы одного сообщения — с конца."""
    by_mid: dict[int, set[int]] = {}
    for ref in refs or []:
        try:
            by_mid.setdefault(int(ref["mid"]), set()).add(int(ref["i"]))
        except (KeyError, TypeError, ValueError):
            continue
    removed = 0
    async with chat_lock(session_id):
        for mid, idxs in by_mid.items():
            msg = await db.get(models.Message, mid)
            if msg is None or msg.session_id != session_id:
                continue
            meta = copy.deepcopy(active_meta(msg.horae, msg.active_swipe))
            if not meta:
                continue
            events = meta.get("events") or []
            kept = [e for j, e in enumerate(events) if j not in idxs]
            removed += len(events) - len(kept)
            meta["events"] = kept
            await set_message_meta(db, msg, meta)
        await db.commit()
    return removed


# ---------------------------------------------------------------------------
# Свёртки хронологии
# ---------------------------------------------------------------------------
def _range(s: dict) -> tuple[int, int]:
    rng = s.get("range") or [0, 0]
    try:
        return int(rng[0]), int(rng[1])
    except (TypeError, ValueError, IndexError):
        return 0, 0


def place_summary(summaries: list, summary: dict) -> list:
    """
    Вставить свёртку в список верхнего уровня без пересечений: пересекающиеся
    свёртки уходят в её children (частично пересекающиеся расширяют её
    диапазон — иначе одно событие покрывали бы две свёртки сразу).
    """
    lo, hi = _range(summary)
    changed = True
    rest = [s for s in summaries if s.get("kind") == "carry"]
    top = [s for s in summaries if s.get("kind") != "carry"]
    children = list(summary.get("children") or [])
    while changed:
        changed = False
        keep = []
        for s in top:
            a, b = _range(s)
            if a <= hi and b >= lo:
                children.append(s)
                lo, hi = min(lo, a), max(hi, b)
                changed = True
            else:
                keep.append(s)
        top = keep
    summary["range"] = [lo, hi]
    summary["children"] = children
    if children:
        summary["depth"] = max(int(summary.get("depth") or 1),
                               1 + max(int(c.get("depth") or 1) for c in children))
    top.append(summary)
    top.sort(key=lambda s: _range(s)[0])
    return rest + top


def remove_summary(summaries: list, summary_id: str) -> list:
    """Удалить свёртку; её дети возвращаются на верхний уровень."""
    out = []
    for s in summaries:
        if s.get("id") == summary_id:
            out.extend(s.get("children") or [])
        else:
            out.append(s)
    out.sort(key=lambda s: (s.get("kind") != "carry", _range(s)[0]))
    return out


def events_dates(state: dict, lo: int, hi: int) -> tuple[str, str, int]:
    evs = [e for e in state.get("events") or [] if lo <= e["mid"] <= hi]
    dates = [e.get("date") for e in evs if e.get("date")]
    return (dates[0] if dates else ""), (dates[-1] if dates else ""), len(evs)


async def add_summary(db, session_id: int, *, lo: int, hi: int, text: str, kind: str,
                      depth: int = 1, children: list | None = None, state: dict | None = None) -> dict:
    """Записать свёртку (под замком чата)."""
    async with chat_lock(session_id):
        data = await load_chat_data(db, session_id)
        summary = _new_summary(data, lo=lo, hi=hi, text=text, kind=kind, depth=depth,
                               children=children, state=state)
        # Дети, переданные явно, уже вынуты вызывающим из верхнего уровня.
        ids = {c.get("id") for c in children or []}
        data["summaries"] = place_summary([s for s in data["summaries"] if s.get("id") not in ids], summary)
        data["summary_error"] = None
        await save_chat_data(db, session_id, data)
        await db.commit()
    return summary


def _new_summary(data: dict, *, lo: int, hi: int, text: str, kind: str, depth: int,
                 children: list | None, state: dict | None) -> dict:
    date_from, date_to, count = events_dates(state or {}, lo, hi) if state else ("", "", 0)
    return {
        "id": f"s_{next_seq(data)}", "kind": kind, "range": [lo, hi], "text": text.strip(),
        "depth": depth, "active": True, "created_at": now_iso(), "date_from": date_from,
        "date_to": date_to, "events": count, "children": list(children or []),
    }


async def update_summary(db, session_id: int, summary_id: str, *, text: str | None = None,
                         active: bool | None = None) -> bool:
    async with chat_lock(session_id):
        data = await load_chat_data(db, session_id)
        for s in data["summaries"]:
            if s.get("id") == summary_id:
                if text is not None and text.strip():
                    s["text"] = text.strip()
                if active is not None:
                    s["active"] = bool(active)
                await save_chat_data(db, session_id, data)
                await db.commit()
                return True
    return False


async def delete_summary(db, session_id: int, summary_id: str) -> bool:
    async with chat_lock(session_id):
        data = await load_chat_data(db, session_id)
        if not any(s.get("id") == summary_id for s in data["summaries"]):
            return False
        data["summaries"] = remove_summary(data["summaries"], summary_id)
        await save_chat_data(db, session_id, data)
        await db.commit()
    return True


def covered_pointer(entries, summaries, snapshot_pointer: int) -> int:
    """
    Указатель «учтено до» для активного окна с учётом свёрток хронологии
    (настройка summary_hides): наибольший id P, до которого каждое сообщение
    покрыто мастер-снимком (id ≤ snapshot_pointer) или активной свёрткой.
    Сообщения без меты и реплики пользователя внутри диапазона свёртки
    покрыты ею так же, как в плагине (/hide скрывал весь диапазон).
    """
    active = [_range(s) for s in summaries or [] if s.get("active", True) and s.get("kind") != "carry"]
    pointer = snapshot_pointer
    for mid, _role, _meta, _side in entries:
        if mid <= snapshot_pointer:
            continue
        if any(a <= mid <= b for a, b in active):
            pointer = mid
        else:
            break
    return pointer


# ---------------------------------------------------------------------------
# Удаление сообщений / чата, ветка
# ---------------------------------------------------------------------------
async def on_messages_deleted(db, session_id: int, deleted_ids: list[int]) -> None:
    """
    После удаления сообщений: свёртки, чей диапазон доходит до удалённого,
    снимаются (их текст пересказывает удалённое; дети, целиком старше,
    возвращаются), якоря правок и таблиц прижимаются к последнему id —
    SQLite выдаст удалённые id новым сообщениям, и те иначе сошли бы за
    «уже учтённые». Документы поиска удалённых сообщений — удаляются.
    """
    if not deleted_ids:
        return
    first = min(deleted_ids)
    await db.execute(sql_delete(models.HoraeMemoryDoc).where(
        models.HoraeMemoryDoc.session_id == session_id,
        models.HoraeMemoryDoc.message_id.in_(list(deleted_ids))))
    row = await db.get(models.HoraeChatState, session_id)
    if row is None:
        return
    async with chat_lock(session_id):
        data = await load_chat_data(db, session_id)
        last = await last_message_id(db, session_id)

        def cascade(items: list) -> list:
            out = []
            for s in items:
                if s.get("kind") != "carry" and _range(s)[1] >= first:
                    out.extend(cascade(s.get("children") or []))
                else:
                    out.append(s)
            return out

        data["summaries"] = cascade(data["summaries"])
        for op in data["ops"]:
            if int(op.get("at") or 0) > last:
                op["at"] = last
        for table in data.get("tables") or []:
            if int(table.get("base_anchor") or 0) > last:
                table["base_anchor"] = last
        for overlay in (data.get("table_overlays") or {}).values():
            if int(overlay.get("base_anchor") or 0) > last:
                overlay["base_anchor"] = last
        await save_chat_data(db, session_id, data)


async def on_session_deleted(db, session_id: int) -> None:
    await db.execute(sql_delete(models.HoraeChatState).where(models.HoraeChatState.session_id == session_id))
    await db.execute(sql_delete(models.HoraeMemoryDoc).where(models.HoraeMemoryDoc.session_id == session_id))
    forget_chat(session_id)


def _map_id(id_map: dict[int, int], old: int) -> int:
    """Новый id для старого: точный или ближайший меньший (0 — нет такого)."""
    if old in id_map:
        return id_map[old]
    lower = [k for k in id_map if k <= old]
    return id_map[max(lower)] if lower else 0


def remap_chat_data(data: dict, id_map: dict[int, int], *, pivot: int | None = None) -> dict:
    """
    Состояние чата для копии (ветка, импорт): id сообщений → новые. pivot —
    последний скопированный id: правки и свёртки позже него не переносятся.
    """
    out = copy.deepcopy(data)
    ops = []
    for op in out.get("ops") or []:
        at = int(op.get("at") or 0)
        if pivot is not None and at > pivot:
            continue
        op["at"] = _map_id(id_map, at)
        ops.append(op)
    out["ops"] = ops

    def remap_summaries(items: list) -> list:
        res = []
        for s in items:
            lo, hi = _range(s)
            if s.get("kind") != "carry":
                if pivot is not None and hi > pivot:
                    res.extend(remap_summaries(s.get("children") or []))
                    continue
                s["range"] = [_map_id(id_map, lo) or _map_id(id_map, hi), _map_id(id_map, hi)]
            s["children"] = remap_summaries(s.get("children") or [])
            res.append(s)
        return res

    out["summaries"] = remap_summaries(out.get("summaries") or [])
    for table in out.get("tables") or []:
        table["base_anchor"] = _map_id(id_map, int(table.get("base_anchor") or 0))
    for overlay in (out.get("table_overlays") or {}).values():
        overlay["base_anchor"] = _map_id(id_map, int(overlay.get("base_anchor") or 0))
    out["scan"] = None
    out["summary_error"] = None
    out.pop("docs_synced", None)   # у копии своих документов поиска ещё нет
    return out


async def copy_to_fork(db, src_id: int, dst_id: int, id_map: dict[int, int], pivot: int) -> None:
    """Ветка чата: состояние Horae — до развилки, с новыми id сообщений."""
    row = await db.get(models.HoraeChatState, src_id)
    if row is None or not isinstance(row.data, dict):
        return
    data = blank_chat_data()
    data.update(copy.deepcopy(row.data))
    await save_chat_data(db, dst_id, remap_chat_data(data, id_map, pivot=pivot))


# ---------------------------------------------------------------------------
# Экспорт / импорт данных Horae чата
# ---------------------------------------------------------------------------
async def export_chat(db, session_id: int) -> dict:
    rows = (await db.execute(
        select(models.Message.id, models.Message.role, models.Message.horae, models.Message.active_swipe)
        .where(models.Message.session_id == session_id).order_by(models.Message.id)
    )).all()
    return {
        "type": "horae-chat", "version": 1, "exported_at": now_iso(),
        "messages": [{"id": r.id, "role": r.role, "horae": r.horae, "active_swipe": r.active_swipe or 0}
                     for r in rows if r.horae],
        "chat": await load_chat_data(db, session_id),
    }


def _clean_imported_data(chat) -> dict:
    data = blank_chat_data()
    if isinstance(chat, dict):
        for key in data:
            if key in chat and chat[key] is not None:
                data[key] = copy.deepcopy(chat[key])
    data["settings"] = horae_settings.sanitize(data.get("settings"))
    data["scan"] = None
    data["summary_error"] = None
    data.pop("docs_synced", None)
    return data


async def import_chat(db, session_id: int, payload: dict, *, mode: str = "by_id") -> dict:
    """
    Импорт файла «Экспорт данных Horae».

    by_id — тот же чат (или его точная копия): меты кладутся сообщениям с
    теми же id, состояние чата заменяется.
    initial — данные другого чата как стартовое состояние этого: их состояние
    становится seed, а свёртки и события — пересказом в начале хронологии
    (как «Новый чат с памятью», но в уже существующий чат).
    """
    messages = [m for m in payload.get("messages") or [] if isinstance(m, dict)]
    chat = payload.get("chat") if isinstance(payload.get("chat"), dict) else {}
    if mode not in ("by_id", "initial"):
        raise OpError("неизвестный режим импорта")
    async with chat_lock(session_id):
        if mode == "by_id":
            ids = [int(m.get("id") or 0) for m in messages]
            rows = {r.id: r for r in (await db.execute(
                select(models.Message).where(models.Message.session_id == session_id,
                                             models.Message.id.in_(ids))
            )).scalars().all()}
            applied = 0
            for m in messages:
                row = rows.get(int(m.get("id") or 0))
                if row is not None and isinstance(m.get("horae"), dict):
                    row.horae = copy.deepcopy(m["horae"])
                    applied += 1
            await save_chat_data(db, session_id, _clean_imported_data(chat))
            await db.commit()
            return {"messages": applied}
        imported = _clean_imported_data(chat)
        entries = []
        for m in sorted(messages, key=lambda x: int(x.get("id") or 0)):
            h = m.get("horae") if isinstance(m.get("horae"), dict) else None
            entries.append((int(m.get("id") or 0), m.get("role") or "assistant",
                            active_meta(h, int(m.get("active_swipe") or 0)), is_side(h)))
        state = hs.replay(entries, ops=imported.get("ops") or [], seed=imported.get("seed"),
                          settings=horae_settings.resolve(None, None, imported.get("settings")),
                          rpg_config=imported.get("rpg_config") or {})
        contributions = [(mid, meta.get("tables") or []) for mid, _r, meta, side in entries
                         if not side and isinstance(meta, dict) and meta.get("tables")]
        local = [t for t in imported.get("tables") or [] if isinstance(t, dict)]
        results = horae_tables.replay(local, contributions) if local else {}
        data = await load_chat_data(db, session_id)
        data["seed"] = seed_from_state(state)
        recaps = recap_summaries(imported, state, 10 ** 12)
        data["summaries"] = recaps + [s for s in data.get("summaries") or [] if s.get("kind") != "carry"]
        for t in local:
            res = results.get(t.get("id")) or {}
            data["tables"].append({**t, "base": dict(res.get("data") or t.get("base") or {}),
                                   "rows": res.get("rows", t.get("rows")), "cols": res.get("cols", t.get("cols")),
                                   "base_anchor": 0})
        if imported.get("rpg_config") and not data.get("rpg_config"):
            data["rpg_config"] = imported["rpg_config"]
        await save_chat_data(db, session_id, data)
        await db.commit()
        return {"messages": len(entries), "recaps": len(recaps)}


async def clear_chat(db, session_id: int) -> int:
    """Стереть данные Horae чата: меты сообщений, состояние, документы поиска."""
    async with chat_lock(session_id):
        msgs = (await db.execute(
            select(models.Message).where(models.Message.session_id == session_id,
                                         models.Message.horae.is_not(None))
        )).scalars().all()
        for m in msgs:
            m.horae = None
        await db.execute(sql_delete(models.HoraeChatState).where(models.HoraeChatState.session_id == session_id))
        await db.execute(sql_delete(models.HoraeMemoryDoc).where(models.HoraeMemoryDoc.session_id == session_id))
        await db.commit()
    return len(msgs)


def seed_from_state(state: dict) -> dict:
    """Стартовое состояние нового чата: всё, кроме событий (они идут пересказом)."""
    seed = copy.deepcopy(state)
    seed["events"] = []
    for npc in seed.get("npcs", {}).values():
        npc["first_mid"] = 0
        npc["last_mid"] = 0
    for item in seed.get("items", {}).values():
        item["mid"] = 0
    for loc in seed.get("locations", {}).values():
        loc["mid"] = 0
        loc["first_mid"] = 0
        loc["user"] = True  # перенесённое — как правленое: ИИ его не перезапишет молча
    for rel in seed.get("relationships", []):
        rel["mid"] = 0
    for a in seed.get("agenda", []):
        a["mid"] = 0
    seed["last_mid"] = 0
    return seed


def recap_summaries(data: dict, state: dict, cut_mid: int) -> list[dict]:
    """
    Пересказ прошлого для нового чата: тексты свёрток старше cut_mid и
    непокрытые события блоками по 8 строк («N. [дата время] [уровень] текст»).
    """
    recaps: list[str] = []
    summaries = data.get("summaries") or []
    for s in summaries:
        if s.get("kind") == "carry" or (_range(s)[1] < cut_mid and s.get("active", True)):
            if (s.get("text") or "").strip() and s["text"].strip() not in recaps:
                recaps.append(s["text"].strip())
    loose = []
    for ev in state.get("events") or []:
        if ev["mid"] >= cut_mid or hs.covering_summary(summaries, ev["mid"]):
            continue
        when = " ".join(x for x in (ev.get("date"), ev.get("time")) if x)
        loose.append(f"[{when or '?'}] [{hs.LEVEL_RU.get(ev['level'], 'обычное')}] {ev['text']}")
    for i in range(0, len(loose), 8):
        chunk = loose[i:i + 8]
        recaps.append("\n".join(f"{i + j + 1}. {line}" for j, line in enumerate(chunk)))
    out = []
    for n, text in enumerate(recaps, 1):
        out.append({"id": f"s_carry_{n}", "kind": "carry", "range": [0, 0], "text": text, "depth": 1,
                    "active": True, "created_at": now_iso(), "date_from": "", "date_to": "",
                    "events": 0, "children": []})
    return out
