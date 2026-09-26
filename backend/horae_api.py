"""
Эндпоинты Horae State Engine (см. docs/API.md, раздел Horae).

Роутер собирается фабрикой build_router(current_user, can_access_session):
зависимости доступа живут в main.py, а импорт main отсюда был бы циклическим.
main.py подключает роутер ДО раздачи статики (иначе её mount на «/»
перехватывал бы эти пути).
"""
from __future__ import annotations

import copy

from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend import horae_engine as he, horae_prompts, horae_settings, horae_state as hs
from backend import horae_tables, horae_tasks, models
from backend.database import get_session


def _is_admin(user) -> bool:
    return user is None or getattr(user, "role", "") == "admin"


def build_router(current_user, can_access_session) -> APIRouter:
    router = APIRouter(prefix="/api")

    async def chat(db, session_id: int, user):
        sess = await db.get(models.ChatSession, session_id)
        if not await can_access_session(db, sess, user):
            raise HTTPException(403, "Нет доступа к этому чату")
        return sess

    async def message(db, message_id: int, user):
        msg = await db.get(models.Message, message_id)
        if msg is None:
            raise HTTPException(404, "Сообщение не найдено")
        await chat(db, msg.session_id, user)
        return msg

    def bad(exc: Exception):
        raise HTTPException(400, str(exc)) from exc

    # ------------------------------------------------------------------ состояние
    async def state_payload(db, sess, at: int | None = None) -> dict:
        from backend.horae_memory import estimate_tokens

        comp = await he.compute(db, sess, until=at)
        data = comp.data
        api_state = hs.to_api(comp.state, settings=comp.settings, names=comp.names,
                              pinned=data.get("pinned_npcs"), favorite=data.get("favorite_npcs"),
                              calendar=comp.calendar)
        timeline = hs.timeline(comp.state, data.get("summaries") or [], calendar=comp.calendar)
        tables = []
        for t in comp.tables:
            res = comp.table_results.get(t["id"]) or {}
            tables.append({
                "id": t["id"], "name": t.get("name") or "", "scope": t.get("scope") or "local",
                "prompt": t.get("prompt") or "", "rows": res.get("rows", t.get("rows")),
                "cols": res.get("cols", t.get("cols")), "data": res.get("data") or {},
                "locked_rows": list(t.get("locked_rows") or []), "locked_cols": list(t.get("locked_cols") or []),
                "locked_cells": list(t.get("locked_cells") or []),
            })
        ai = [(mid, meta, side) for mid, role, meta, side in comp.entries if role == "assistant"]
        injection = ""
        if comp.settings.get("enabled"):
            injection = (he.rules_text(comp) if comp.settings.get("parse_tags") else "") + "\n" + (
                he.state_block(comp) if comp.settings.get("inject_state") else "")
        job = horae_tasks.get_job(sess.id)
        squeeze = await he.chat_compression(db, sess, data=data)
        return {
            "enabled": bool(comp.settings.get("enabled")),
            "settings": comp.settings,
            # Кто сжимает историю чата: snapshot / horae / off (he.compression_engine).
            "compression": {"engine": squeeze["engine"], "summary_layer": squeeze["summary_layer"]},
            "state": api_state,
            "timeline": timeline,
            "tables": tables,
            "rpg_config": data.get("rpg_config") or {},
            "ops": [{"id": op.get("id"), "kind": op.get("kind"), "at": op.get("at"),
                     "label": hs.op_label(op), "created_at": op.get("created_at")}
                    for op in data.get("ops") or []],
            "stats": {
                "messages": len(comp.entries),
                "ai_messages": len(ai),
                "with_meta": sum(1 for _m, meta, side in ai if meta and not side),
                "without_meta": sum(1 for _m, meta, side in ai if not side and not hs.meta_has_events(meta)),
                "side": sum(1 for _m, _meta, side in ai if side),
                "injection_tokens": estimate_tokens(injection) if injection.strip() else 0,
                "summaries": len([s for s in data.get("summaries") or [] if s.get("kind") != "carry"]),
                "scan": data.get("scan"),
                "summary_error": data.get("summary_error"),
            },
            "job": job.to_dict() if job else None,
        }

    @router.get("/sessions/{session_id}/horae/state")
    async def get_state(session_id: int, at: int | None = None, user=Depends(current_user),
                        db: AsyncSession = Depends(get_session)):
        sess = await chat(db, session_id, user)
        return await state_payload(db, sess, at)

    # ------------------------------------------------------------------ мета сообщения
    @router.get("/messages/{message_id}/horae")
    async def get_message_meta(message_id: int, user=Depends(current_user),
                               db: AsyncSession = Depends(get_session)):
        return he.message_view(await message(db, message_id, user))

    @router.put("/messages/{message_id}/horae")
    async def put_message_meta(message_id: int, payload: dict = Body(...), user=Depends(current_user),
                               db: AsyncSession = Depends(get_session)):
        msg = await message(db, message_id, user)
        raw = payload.get("meta")
        meta = hs.normalize_meta(raw) if raw is not None else None
        if meta is not None and not hs.meta_has_data(meta):
            meta = None
        async with he.chat_lock(msg.session_id):
            await he.set_message_meta(db, msg, meta)
            await db.commit()
        await horae_tasks._sync_docs(db, msg.session_id, [msg.id], await horae_tasks._connection(db))
        return he.message_view(msg)

    @router.post("/messages/{message_id}/horae/analyze")
    async def analyze(message_id: int, user=Depends(current_user), db: AsyncSession = Depends(get_session)):
        msg = await message(db, message_id, user)
        try:
            await horae_tasks.analyze_message(msg.session_id, msg.id)
        except horae_tasks.HoraeTaskError as exc:
            raise HTTPException(400, str(exc)) from exc
        await db.refresh(msg)
        return he.message_view(msg)

    @router.post("/messages/{message_id}/horae/side")
    async def set_side(message_id: int, payload: dict = Body(...), user=Depends(current_user),
                       db: AsyncSession = Depends(get_session)):
        msg = await message(db, message_id, user)
        async with he.chat_lock(msg.session_id):
            await he.set_side(db, msg, bool(payload.get("side")))
            await db.commit()
        await horae_tasks._sync_docs(db, msg.session_id, [msg.id], await horae_tasks._connection(db))
        return he.message_view(msg)

    # ------------------------------------------------------------------ правки
    @router.post("/sessions/{session_id}/horae/ops")
    async def post_op(session_id: int, payload: dict = Body(...), user=Depends(current_user),
                      db: AsyncSession = Depends(get_session)):
        await chat(db, session_id, user)
        try:
            op = await he.add_op(db, session_id, payload)
        except he.OpError as exc:
            bad(exc)
        return {"ok": True, "op": op}

    @router.delete("/sessions/{session_id}/horae/ops/{op_id}")
    async def undo_op(session_id: int, op_id: str, user=Depends(current_user),
                      db: AsyncSession = Depends(get_session)):
        await chat(db, session_id, user)
        if not await he.delete_op(db, session_id, op_id):
            raise HTTPException(404, "Правка не найдена")
        return {"ok": True}

    # ------------------------------------------------------------------ события
    @router.post("/sessions/{session_id}/horae/events")
    async def add_event(session_id: int, payload: dict = Body(...), user=Depends(current_user),
                        db: AsyncSession = Depends(get_session)):
        await chat(db, session_id, user)
        try:
            await he.insert_event(db, session_id, int(payload.get("mid") or 0), payload.get("level") or "normal",
                                  payload.get("text") or "", payload.get("index"))
        except (he.OpError, ValueError) as exc:
            bad(exc)
        await horae_tasks._sync_docs(db, session_id, [int(payload.get("mid") or 0)], await horae_tasks._connection(db))
        return {"ok": True}

    @router.patch("/sessions/{session_id}/horae/events")
    async def patch_event(session_id: int, payload: dict = Body(...), user=Depends(current_user),
                          db: AsyncSession = Depends(get_session)):
        await chat(db, session_id, user)
        try:
            await he.edit_event(db, session_id, int(payload.get("mid") or 0), int(payload.get("i") or 0),
                                level=payload.get("level"), text=payload.get("text"))
        except (he.OpError, ValueError) as exc:
            bad(exc)
        await horae_tasks._sync_docs(db, session_id, [int(payload.get("mid") or 0)], await horae_tasks._connection(db))
        return {"ok": True}

    @router.post("/sessions/{session_id}/horae/events/delete")
    async def delete_events(session_id: int, payload: dict = Body(...), user=Depends(current_user),
                            db: AsyncSession = Depends(get_session)):
        await chat(db, session_id, user)
        refs = payload.get("refs") or []
        removed = await he.delete_events(db, session_id, refs)
        mids = sorted({int(r.get("mid") or 0) for r in refs if isinstance(r, dict)})
        await horae_tasks._sync_docs(db, session_id, mids or None, await horae_tasks._connection(db))
        return {"ok": True, "removed": removed}

    # ------------------------------------------------------------------ свёртки
    @router.post("/sessions/{session_id}/horae/compress")
    async def compress(session_id: int, payload: dict = Body(...), user=Depends(current_user),
                       db: AsyncSession = Depends(get_session)):
        await chat(db, session_id, user)
        try:
            summary = await horae_tasks.compress(session_id, payload.get("refs") or [],
                                                 payload.get("summary_ids") or [],
                                                 payload.get("mode") or "events")
        except horae_tasks.HoraeTaskError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"ok": True, "summary": summary}

    @router.post("/sessions/{session_id}/horae/summaries")
    async def add_summary(session_id: int, payload: dict = Body(...), user=Depends(current_user),
                          db: AsyncSession = Depends(get_session)):
        sess = await chat(db, session_id, user)
        text = (payload.get("text") or "").strip()
        try:
            lo, hi = int(payload.get("from_mid") or 0), int(payload.get("to_mid") or 0)
        except (TypeError, ValueError):
            raise HTTPException(400, "Неверный диапазон сообщений") from None
        if not text or lo <= 0 or hi < lo:
            raise HTTPException(400, "Нужны текст и диапазон сообщений «с — по»")
        comp = await he.compute(db, sess)
        summary = await he.add_summary(db, session_id, lo=lo, hi=hi, text=text, kind="manual", state=comp.state)
        return {"ok": True, "summary": summary}

    @router.patch("/sessions/{session_id}/horae/summaries/{summary_id}")
    async def patch_summary(session_id: int, summary_id: str, payload: dict = Body(...),
                            user=Depends(current_user), db: AsyncSession = Depends(get_session)):
        await chat(db, session_id, user)
        ok = await he.update_summary(db, session_id, summary_id, text=payload.get("text"),
                                     active=payload.get("active"))
        if not ok:
            raise HTTPException(404, "Свёртка не найдена")
        return {"ok": True}

    @router.delete("/sessions/{session_id}/horae/summaries/{summary_id}")
    async def delete_summary(session_id: int, summary_id: str, user=Depends(current_user),
                             db: AsyncSession = Depends(get_session)):
        await chat(db, session_id, user)
        if not await he.delete_summary(db, session_id, summary_id):
            raise HTTPException(404, "Свёртка не найдена")
        return {"ok": True}

    @router.post("/sessions/{session_id}/horae/summaries/run", status_code=202)
    async def run_summaries(session_id: int, user=Depends(current_user), db: AsyncSession = Depends(get_session)):
        await chat(db, session_id, user)
        try:
            job = horae_tasks.start_summary_job(session_id)
        except horae_tasks.HoraeTaskError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"job": job.to_dict()}

    # ------------------------------------------------------------------ скан и задания
    @router.post("/sessions/{session_id}/horae/scan", status_code=202)
    async def scan(session_id: int, payload: dict = Body(default={}), user=Depends(current_user),
                   db: AsyncSession = Depends(get_session)):
        await chat(db, session_id, user)
        try:
            job = horae_tasks.start_scan(session_id, batch_tokens=payload.get("batch_tokens") or 80000,
                                         include=payload.get("include") or {})
        except horae_tasks.HoraeTaskError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"job": job.to_dict()}

    @router.get("/sessions/{session_id}/horae/job")
    async def job_status(session_id: int, user=Depends(current_user), db: AsyncSession = Depends(get_session)):
        await chat(db, session_id, user)
        job = horae_tasks.get_job(session_id)
        return {"job": job.to_dict() if job else None}

    @router.post("/sessions/{session_id}/horae/job/cancel")
    async def job_cancel(session_id: int, user=Depends(current_user), db: AsyncSession = Depends(get_session)):
        await chat(db, session_id, user)
        job = horae_tasks.cancel_job(session_id)
        return {"job": job.to_dict() if job else None}

    @router.post("/sessions/{session_id}/horae/scan/undo")
    async def scan_undo(session_id: int, user=Depends(current_user), db: AsyncSession = Depends(get_session)):
        await chat(db, session_id, user)
        if horae_tasks.job_active(session_id):
            raise HTTPException(409, "Дождитесь конца задания Horae")
        return {"ok": True, "restored": await horae_tasks.undo_scan(session_id)}

    # ------------------------------------------------------------------ таблицы
    async def _table_context(db, sess):
        comp = await he.compute(db, sess)
        return comp

    async def _save_table(db, sess, table: dict) -> None:
        """Разложить действующую таблицу обратно: шаблон — по его месту, данные — в чат."""
        template, overlay = horae_tables.split_effective(table)
        scope = table.get("scope") or "local"
        data = await he.load_chat_data(db, sess.id)
        if template is None:
            data["tables"] = [overlay if t.get("id") == table["id"] else t for t in data.get("tables") or []]
            if not any(t.get("id") == table["id"] for t in data["tables"]):
                data["tables"].append(overlay)
        else:
            data.setdefault("table_overlays", {})[table["id"]] = overlay
            if scope == "global":
                lib = await he.library(db)
                lib["global_tables"] = [template if t.get("id") == table["id"] else t for t in lib["global_tables"]]
                if not any(t.get("id") == table["id"] for t in lib["global_tables"]):
                    lib["global_tables"].append(template)
                await he.save_library(db, lib)
            else:
                character = await db.get(models.Character, sess.character_id)
                profile = copy.deepcopy(character.horae_profile or {})
                tables = [template if t.get("id") == table["id"] else t for t in profile.get("tables") or []]
                if not any(t.get("id") == table["id"] for t in tables):
                    tables.append(template)
                profile["tables"] = tables
                character.horae_profile = profile
        await he.save_chat_data(db, sess.id, data)

    async def _remove_table(db, sess, table: dict) -> None:
        data = await he.load_chat_data(db, sess.id)
        data["tables"] = [t for t in data.get("tables") or [] if t.get("id") != table["id"]]
        (data.get("table_overlays") or {}).pop(table["id"], None)
        scope = table.get("scope") or "local"
        if scope == "global":
            lib = await he.library(db)
            lib["global_tables"] = [t for t in lib["global_tables"] if t.get("id") != table["id"]]
            await he.save_library(db, lib)
        elif scope == "character":
            character = await db.get(models.Character, sess.character_id)
            profile = copy.deepcopy(character.horae_profile or {})
            profile["tables"] = [t for t in profile.get("tables") or [] if t.get("id") != table["id"]]
            character.horae_profile = profile
        await he.save_chat_data(db, sess.id, data)

    def _check_scope(scope: str, user) -> None:
        if scope == "global" and not _is_admin(user):
            raise HTTPException(403, "Глобальные таблицы меняет только администратор")

    @router.post("/sessions/{session_id}/horae/tables")
    async def create_table(session_id: int, payload: dict = Body(...), user=Depends(current_user),
                           db: AsyncSession = Depends(get_session)):
        sess = await chat(db, session_id, user)
        scope = payload.get("scope") if payload.get("scope") in ("local", "global", "character") else "local"
        _check_scope(scope, user)
        name = (payload.get("name") or "").strip()[:100]
        if not name:
            raise HTTPException(400, "Нужно название таблицы")
        table = horae_tables.new_table(name, int(payload.get("rows") or 3), int(payload.get("cols") or 3),
                                       (payload.get("prompt") or "")[:2000], scope)
        async with he.chat_lock(session_id):
            await _save_table(db, sess, table)
            await db.commit()
        return {"ok": True, "id": table["id"]}

    @router.post("/sessions/{session_id}/horae/tables/import")
    async def import_table(session_id: int, payload: dict = Body(...), user=Depends(current_user),
                           db: AsyncSession = Depends(get_session)):
        sess = await chat(db, session_id, user)
        try:
            table = horae_tables.from_import(payload.get("table") or payload)
        except (ValueError, TypeError, KeyError) as exc:
            raise HTTPException(400, f"Не похоже на таблицу: {exc}") from exc
        table["scope"] = "local"
        table["base_anchor"] = await he.last_message_id(db, session_id)
        async with he.chat_lock(session_id):
            await _save_table(db, sess, table)
            await db.commit()
        return {"ok": True, "id": table["id"]}

    @router.patch("/sessions/{session_id}/horae/tables/{table_id}")
    async def patch_table(session_id: int, table_id: str, payload: dict = Body(...), user=Depends(current_user),
                          db: AsyncSession = Depends(get_session)):
        sess = await chat(db, session_id, user)
        async with he.chat_lock(session_id):
            comp = await _table_context(db, sess)
            table = next((t for t in comp.tables if t["id"] == table_id), None)
            if table is None:
                raise HTTPException(404, "Таблица не найдена")
            current = (comp.table_results.get(table_id) or {}).get("data") or dict(table.get("base") or {})
            anchor = await he.last_message_id(db, session_id)
            scope = table.get("scope") or "local"
            structural = any(k in payload for k in ("name", "prompt", "structure", "lock"))
            if structural:
                _check_scope(scope, user)
            if "name" in payload or "lock" in payload:
                # Данные таблицы = база + вклады ИИ после базы. Новый замок или
                # имя применились бы к повтору задним числом: замок отсёк бы уже
                # записанные ячейки, а старые вклады под прежним именем
                # перестали бы находиться. Поэтому сначала текущие данные
                # становятся базой — замок и имя действуют на будущие записи.
                table = {**table, "base": dict(current), "base_anchor": anchor}
            if "name" in payload and (payload.get("name") or "").strip():
                table = {**table, "name": payload["name"].strip()[:100]}
            if "prompt" in payload:
                table = {**table, "prompt": (payload.get("prompt") or "")[:2000]}
            if isinstance(payload.get("cell"), dict):
                cell = payload["cell"]
                table = horae_tables.set_cell(table, current, int(cell.get("r") or 0), int(cell.get("c") or 0),
                                              str(cell.get("value") or "")[:2000], anchor)
            if isinstance(payload.get("structure"), dict):
                st = payload["structure"]
                table = horae_tables.structure(table, current, st.get("op") or "", int(st.get("index") or 0), anchor)
            if isinstance(payload.get("lock"), dict):
                lk = payload["lock"]
                table = horae_tables.set_lock(table, lk.get("type") or "cell", int(lk.get("r") or 0),
                                              int(lk.get("c") or 0), bool(lk.get("locked")))
            if payload.get("clear"):
                table = horae_tables.clear_data(table, current, anchor)
            new_scope = payload.get("scope")
            if new_scope in ("local", "global", "character") and new_scope != scope:
                _check_scope(new_scope, user)
                _check_scope(scope, user)
                await _remove_table(db, sess, table)
                table = {**table, "scope": new_scope, "base": dict(current), "base_anchor": anchor}
            await _save_table(db, sess, table)
            await db.commit()
        return {"ok": True}

    @router.delete("/sessions/{session_id}/horae/tables/{table_id}")
    async def delete_table(session_id: int, table_id: str, user=Depends(current_user),
                           db: AsyncSession = Depends(get_session)):
        sess = await chat(db, session_id, user)
        async with he.chat_lock(session_id):
            comp = await _table_context(db, sess)
            table = next((t for t in comp.tables if t["id"] == table_id), None)
            if table is None:
                raise HTTPException(404, "Таблица не найдена")
            _check_scope(table.get("scope") or "local", user)
            await _remove_table(db, sess, table)
            await db.commit()
        return {"ok": True}

    # ------------------------------------------------------------------ настройки
    @router.get("/horae/settings")
    async def get_global_settings(user=Depends(current_user), db: AsyncSession = Depends(get_session)):
        layer = await he.global_layer(db)
        return {"defaults": horae_settings.defaults(), "global": layer,
                "effective": horae_settings.resolve(layer)}

    @router.put("/horae/settings")
    async def put_global_settings(payload: dict = Body(...), user=Depends(current_user),
                                  db: AsyncSession = Depends(get_session)):
        if not _is_admin(user):
            raise HTTPException(403, "Глобальные настройки Horae меняет только администратор")
        layer = await he.save_global_layer(db, payload)
        await db.commit()
        return {"defaults": horae_settings.defaults(), "global": layer,
                "effective": horae_settings.resolve(layer)}

    @router.get("/sessions/{session_id}/horae/settings")
    async def get_chat_settings(session_id: int, user=Depends(current_user), db: AsyncSession = Depends(get_session)):
        sess = await chat(db, session_id, user)
        character = await db.get(models.Character, sess.character_id)
        data = await he.load_chat_data(db, session_id)
        return {"overrides": horae_settings.sanitize(data.get("settings")),
                "character": he.character_layer(character),
                "effective": await he.effective_settings(db, sess, character, data)}

    @router.put("/sessions/{session_id}/horae/settings")
    async def put_chat_settings(session_id: int, payload: dict = Body(...), user=Depends(current_user),
                                db: AsyncSession = Depends(get_session)):
        sess = await chat(db, session_id, user)
        async with he.chat_lock(session_id):
            data = await he.load_chat_data(db, session_id)
            data["settings"] = horae_settings.merge_overrides(data.get("settings"), payload)
            await he.save_chat_data(db, session_id, data)
            await db.commit()
        character = await db.get(models.Character, sess.character_id)
        return {"overrides": data["settings"], "character": he.character_layer(character),
                "effective": await he.effective_settings(db, sess, character, data)}

    async def _character(db, character_id: int, user, write: bool = False):
        character = await db.get(models.Character, character_id)
        if character is None:
            raise HTTPException(404, "Персонаж не найден")
        if not _is_admin(user) and character.owner_id not in (None, user.id):
            raise HTTPException(403, "Нет доступа к этому персонажу")
        if write and not _is_admin(user) and character.owner_id is None:
            raise HTTPException(403, "Общего персонажа меняет только администратор")
        return character

    @router.get("/characters/{character_id}/horae_profile")
    async def get_profile(character_id: int, user=Depends(current_user), db: AsyncSession = Depends(get_session)):
        character = await _character(db, character_id, user)
        profile = character.horae_profile or {}
        return {"settings": horae_settings.sanitize(profile.get("settings")), "tables": profile.get("tables") or []}

    @router.put("/characters/{character_id}/horae_profile")
    async def put_profile(character_id: int, payload: dict = Body(...), user=Depends(current_user),
                          db: AsyncSession = Depends(get_session)):
        character = await _character(db, character_id, user, write=True)
        profile = copy.deepcopy(character.horae_profile or {})
        if "settings" in payload:
            profile["settings"] = horae_settings.merge_overrides(profile.get("settings"), payload.get("settings") or {})
        if isinstance(payload.get("tables"), list):
            profile["tables"] = [horae_tables.make_template(horae_tables.from_import(t))
                                 for t in payload["tables"] if isinstance(t, dict)]
        character.horae_profile = profile
        await db.commit()
        return {"settings": horae_settings.sanitize(profile.get("settings")), "tables": profile.get("tables") or []}

    # ------------------------------------------------------------------ промпты
    @router.get("/horae/prompts")
    async def get_prompts(user=Depends(current_user), db: AsyncSession = Depends(get_session)):
        lib = await he.library(db)
        return {"defaults": horae_prompts.defaults(),
                "presets": horae_prompts.builtin_presets() + [
                    {**p, "builtin": False} for p in lib.get("prompt_presets") or []]}

    def _library_writer(user) -> None:
        # Библиотека общая для всех пользователей: наборы промптов, шаблоны
        # снаряжения и глобальные таблицы меняет администратор (свои промпты
        # чата или персонажа — в их настройках, это доступно всем).
        if not _is_admin(user):
            raise HTTPException(403, "Общую библиотеку Horae меняет только администратор")

    @router.post("/horae/prompts/presets")
    async def save_preset(payload: dict = Body(...), user=Depends(current_user),
                          db: AsyncSession = Depends(get_session)):
        _library_writer(user)
        name = (payload.get("name") or "").strip()[:100]
        if not name:
            raise HTTPException(400, "Нужно название набора")
        prompts = horae_settings.sanitize({"prompts": payload.get("prompts") or {}}).get("prompts") or {}
        lib = await he.library(db)
        presets = [p for p in lib.get("prompt_presets") or [] if p.get("name") != name]
        import hashlib

        preset = {"id": "p_" + hashlib.md5(name.encode("utf-8")).hexdigest()[:10], "name": name, "prompts": prompts}
        presets.append(preset)
        lib["prompt_presets"] = presets
        await he.save_library(db, lib)
        await db.commit()
        return {**preset, "builtin": False}

    @router.delete("/horae/prompts/presets/{preset_id}")
    async def delete_preset(preset_id: str, user=Depends(current_user), db: AsyncSession = Depends(get_session)):
        _library_writer(user)
        lib = await he.library(db)
        lib["prompt_presets"] = [p for p in lib.get("prompt_presets") or [] if p.get("id") != preset_id]
        await he.save_library(db, lib)
        await db.commit()
        return {"ok": True}

    # ------------------------------------------------------------------ RPG
    @router.get("/sessions/{session_id}/horae/rpg_config")
    async def get_rpg_config(session_id: int, user=Depends(current_user), db: AsyncSession = Depends(get_session)):
        await chat(db, session_id, user)
        return (await he.load_chat_data(db, session_id)).get("rpg_config") or {}

    @router.put("/sessions/{session_id}/horae/rpg_config")
    async def put_rpg_config(session_id: int, payload: dict = Body(...), user=Depends(current_user),
                             db: AsyncSession = Depends(get_session)):
        await chat(db, session_id, user)
        async with he.chat_lock(session_id):
            data = await he.load_chat_data(db, session_id)
            data["rpg_config"] = _clean_rpg_config(payload)
            await he.save_chat_data(db, session_id, data)
            await db.commit()
        return data["rpg_config"]

    @router.get("/horae/equipment_templates")
    async def get_templates(user=Depends(current_user), db: AsyncSession = Depends(get_session)):
        from backend import horae_rpg

        lib = await he.library(db)
        return {"builtin": horae_rpg.default_equipment_templates(), "custom": lib.get("equipment_templates") or []}

    @router.put("/horae/equipment_templates")
    async def put_templates(payload: dict = Body(...), user=Depends(current_user),
                            db: AsyncSession = Depends(get_session)):
        _library_writer(user)
        lib = await he.library(db)
        custom = payload.get("custom") if isinstance(payload.get("custom"), list) else []
        lib["equipment_templates"] = [t for t in custom if isinstance(t, dict) and t.get("name")][:100]
        await he.save_library(db, lib)
        await db.commit()
        return {"custom": lib["equipment_templates"]}

    # ------------------------------------------------------------------ данные чата
    @router.get("/sessions/{session_id}/horae/export")
    async def export(session_id: int, user=Depends(current_user), db: AsyncSession = Depends(get_session)):
        await chat(db, session_id, user)
        return await he.export_chat(db, session_id)

    @router.post("/sessions/{session_id}/horae/import")
    async def import_data(session_id: int, payload: dict = Body(...), user=Depends(current_user),
                          db: AsyncSession = Depends(get_session)):
        await chat(db, session_id, user)
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        mode = payload.get("mode") or "by_id"
        if not isinstance(data, dict) or data.get("type") != "horae-chat":
            raise HTTPException(400, "Это не экспорт данных Horae")
        try:
            result = await he.import_chat(db, session_id, data, mode=mode)
        except he.OpError as exc:
            bad(exc)
        await horae_tasks._sync_docs(db, session_id, None, await horae_tasks._connection(db))
        return {"ok": True, **result}

    @router.delete("/sessions/{session_id}/horae")
    async def clear(session_id: int, user=Depends(current_user), db: AsyncSession = Depends(get_session)):
        await chat(db, session_id, user)
        if horae_tasks.job_active(session_id):
            horae_tasks.cancel_job(session_id)
        return {"ok": True, "messages": await he.clear_chat(db, session_id)}

    @router.post("/sessions/{session_id}/horae/carryover")
    async def carryover(session_id: int, payload: dict = Body(default={}), user=Depends(current_user),
                        db: AsyncSession = Depends(get_session)):
        await chat(db, session_id, user)
        try:
            new_id = await horae_tasks.carryover(session_id, keep=int(payload.get("keep") or 5),
                                                 vectors=payload.get("vectors") is not False, user=user)
        except horae_tasks.HoraeTaskError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"ok": True, "session_id": new_id}

    @router.post("/sessions/{session_id}/horae/npc_enrich")
    async def npc_enrich(session_id: int, payload: dict = Body(...), user=Depends(current_user),
                         db: AsyncSession = Depends(get_session)):
        await chat(db, session_id, user)
        try:
            return await horae_tasks.npc_enrich(session_id, payload.get("name") or "", payload.get("aliases") or [])
        except horae_tasks.HoraeTaskError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.post("/sessions/{session_id}/horae/reindex")
    async def reindex(session_id: int, user=Depends(current_user), db: AsyncSession = Depends(get_session)):
        from backend import horae_vector

        await chat(db, session_id, user)
        connection = await horae_tasks._connection(db)
        count = await horae_vector.reindex(db, session_id, connection)
        await db.commit()
        return {"ok": True, "documents": count}

    @router.get("/sessions/{session_id}/horae/recall")
    async def recall_debug(session_id: int, q: str = "", user=Depends(current_user),
                           db: AsyncSession = Depends(get_session)):
        from backend import horae_vector

        sess = await chat(db, session_id, user)
        comp = await he.compute(db, sess)
        connection = await horae_tasks._connection(db)
        found = await horae_vector.recall(db, sess, comp, user_message=q or "", connection=connection)
        top = found[: int(comp.settings.get("recall_top_k") or 5)]
        return {"results": top, "text": horae_vector.render(top, current_date=comp.state["time"].get("date") or "",
                                                            calendar=comp.calendar)}

    return router


def _clean_rpg_config(payload) -> dict:
    """Настройки RPG чата из интерфейса: только известные разделы, длины режутся."""
    if not isinstance(payload, dict):
        return {}
    out: dict = {}
    reps = []
    for r in (payload.get("reputation") or [])[:40]:
        if not isinstance(r, dict) or not str(r.get("name") or "").strip():
            continue
        try:
            lo, hi = int(r.get("min", -100)), int(r.get("max", 100))
            default = int(r.get("default", 0))
        except (TypeError, ValueError):
            lo, hi, default = -100, 100, 0
        if lo > hi:
            lo, hi = hi, lo
        reps.append({"name": str(r["name"]).strip()[:60], "min": lo, "max": hi,
                     "default": max(lo, min(hi, default)),
                     "sub": [str(s).strip()[:60] for s in (r.get("sub") or [])[:20] if str(s).strip()]})
    out["reputation"] = reps
    curs = []
    for c in (payload.get("currencies") or [])[:20]:
        if not isinstance(c, dict) or not str(c.get("name") or "").strip():
            continue
        try:
            rate = max(1, int(c.get("rate") or 1))
        except (TypeError, ValueError):
            rate = 1
        curs.append({"name": str(c["name"]).strip()[:40], "rate": rate, "emoji": str(c.get("emoji") or "💰")[:8]})
    out["currencies"] = curs
    eq = payload.get("equipment") if isinstance(payload.get("equipment"), dict) else {}
    chars = {}
    for owner, cfg in list((eq.get("chars") or {}).items())[:50]:
        if not isinstance(cfg, dict):
            continue
        slots = [{"name": str(s.get("name")).strip()[:40], "max": max(1, min(20, int(s.get("max") or 1)))}
                 for s in (cfg.get("slots") or [])[:40] if isinstance(s, dict) and str(s.get("name") or "").strip()]
        forms = [f for f in (cfg.get("forms") or [])[:10] if isinstance(f, dict) and f.get("id")]
        chars[str(owner)[:100]] = {"slots": slots, "forms": forms, "form": cfg.get("form") or "",
                                   "template": str(cfg.get("template") or "")[:60]}
    out["equipment"] = {"locked": bool(eq.get("locked")), "chars": chars}
    for key in ("deleted_skills", "deleted_currencies", "deleted_strongholds"):
        if isinstance(payload.get(key), list):
            out[key] = payload[key][:500]
    return out
