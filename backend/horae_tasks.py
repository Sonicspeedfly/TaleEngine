"""
Служебные задачи Horae, которым нужна модель: ИИ-анализ ответа, пакетный скан
истории, авто-свёртка хронологии (и свёртка свёрток), ручное сжатие,
«ИИ-заполнение» NPC, переписывание запроса для поиска; плюс перенос в новый
чат с памятью.

Все запросы идут через одну «дверь» aux_complete: последовательно на процесс
и с паузой aux_delay_ms между ними — аналог очереди «вспомогательного API»
плагина. Иначе фоновый анализ, скан и свёртка разом упирались бы в лимит
частоты провайдера на общем ключе.

Модель — настройка aux_model; пусто — summary_model подключения (та же
«быстрая» модель, что у мастер-снимка), пусто и там — модель чата.
"""
from __future__ import annotations

import asyncio
import copy
import logging
import time
from dataclasses import dataclass, field

from sqlalchemy import select

from backend import horae_engine as he, horae_prompts as hp, horae_state as hs, horae_tables
from backend import llm_gateway, models
from backend.database import AsyncSessionLocal

log = logging.getLogger("aichat.horae")

SCAN_MAX_MESSAGES = 20        # сообщений в пакете скана: вывод ~500 токенов на сообщение
SCAN_MIN_CHARS = 20           # короче — «пустой» ответ, сканировать нечего
ANALYSIS_MAX_CHARS = 16000
PREV_USER_CHARS = 2000
RESUMMARY_ROUNDS = 4


class HoraeTaskError(RuntimeError):
    """Служебная задача не удалась — текст для интерфейса."""


class TaskCancelled(Exception):
    pass


# ---------------------------------------------------------------------------
# Дверь служебных запросов
# ---------------------------------------------------------------------------
_gates: dict = {}
_last_call = 0.0


def aux_connection(connection: dict, settings: dict) -> dict:
    connection = dict(connection or {})
    model = (settings.get("aux_model") or "").strip() or (connection.get("summary_model") or "").strip()
    if model:
        connection["default_model"] = model
    return connection


async def _sleep(seconds: float, cancel: asyncio.Event | None) -> None:
    if seconds <= 0:
        return
    if cancel is None:
        await asyncio.sleep(seconds)
        return
    try:
        await asyncio.wait_for(cancel.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        return
    raise TaskCancelled()


async def aux_complete(messages: list[dict], settings: dict, connection: dict, *, max_tokens: int = 4096,
                       temperature: float = 0.3, cancel: asyncio.Event | None = None) -> str:
    """Один служебный запрос: очередь, пауза, своя модель, свои max_tokens/температура."""
    global _last_call
    async with he.loop_lock(_gates, "aux"):
        if cancel is not None and cancel.is_set():
            raise TaskCancelled()
        delay = int(settings.get("aux_delay_ms") or 0) / 1000
        wait = _last_call + delay - time.monotonic()
        await _sleep(wait, cancel)
        try:
            with llm_gateway.sampling_overrides(max_tokens=max_tokens, temperature=temperature):
                text = await llm_gateway.complete(
                    messages, None, aux_connection(connection, settings), kind="horae")
        finally:
            _last_call = time.monotonic()
    if not (text or "").strip():
        raise HoraeTaskError("модель вернула пустой ответ")
    return text


async def _connection(db) -> dict:
    from backend.settings_service import get_connection

    return await get_connection(db)


async def _load(db, session_id: int):
    session = await db.get(models.ChatSession, session_id)
    if session is None:
        raise HoraeTaskError("чат не найден")
    character = await db.get(models.Character, session.character_id)
    data = await he.load_chat_data(db, session_id)
    settings = await he.effective_settings(db, session, character, data)
    names = await he.scene_names(db, session, character)
    return session, character, data, settings, names


# ---------------------------------------------------------------------------
# ИИ-анализ одного ответа
# ---------------------------------------------------------------------------
async def _prev_user(db, session_id: int, before_id: int) -> str:
    rows = (await db.execute(
        select(models.Message.role, models.Message.content, models.Message.horae)
        .where(models.Message.session_id == session_id, models.Message.id < before_id)
        .order_by(models.Message.id.desc()).limit(6)
    )).all()
    for r in rows:
        if r.role == "user" and not he.is_side(r.horae):
            return (r.content or "")[:PREV_USER_CHARS]
    return ""


async def analyze_message(session_id: int, message_id: int, *, merge: bool = True) -> dict:
    """
    Извлечь данные Horae из готового ответа отдельным запросом («волшебная
    палочка» плагина). Нужна, когда модель забыла теги или ответ старый.
    Результат сливается с имеющейся метой (merge) и пишется в тот свайп,
    который был активен на старте, — если за время запроса его не переписали.
    """
    async with AsyncSessionLocal() as db:
        msg = await db.get(models.Message, message_id)
        if msg is None or msg.session_id != session_id:
            raise HoraeTaskError("сообщение не найдено")
        if msg.role != "assistant":
            raise HoraeTaskError("анализ — только для ответов персонажа")
        session, character, data, settings, names = await _load(db, session_id)
        swipe = msg.active_swipe or 0
        original = msg.content or ""
        content = hs.strip_tags_text(original).strip()
        if not content:
            raise HoraeTaskError("в ответе нет текста")
        comp = await he.compute(db, session, character=character, until=message_id - 1,
                                settings=settings, data=data)
        context = hs.analysis_context(comp.state, names)
        prev_user = await _prev_user(db, session_id, message_id)
        connection = await _connection(db)
    prompt = hp.analysis_prompt(settings, context=context, prev_user=prev_user,
                                content=content[:ANALYSIS_MAX_CHARS], user=names.user, char=names.char)
    text = await aux_complete([{"role": "system", "content": hp.EXTRACTOR_SYSTEM},
                               {"role": "user", "content": prompt}], settings, connection)
    _, meta = hs.parse_reply(text, he.parse_context(settings, names))
    if not meta:
        raise HoraeTaskError("модель не вернула разметку Horae")
    meta["source"] = "ai"
    async with AsyncSessionLocal() as db:
        async with he.chat_lock(session_id):
            msg = await db.get(models.Message, message_id)
            if msg is None:
                raise HoraeTaskError("сообщение удалено во время анализа")
            swipes = list(msg.swipes or [msg.content])
            current = swipes[swipe] if swipe < len(swipes) else None
            if current is not None and current != original and msg.content != original:
                raise HoraeTaskError("ответ изменился во время анализа — повторите")
            existing = (msg.horae or {}).get("metas", [None] * (swipe + 1))
            existing = existing[swipe] if swipe < len(existing) else None
            final = hs.merge_meta(existing, meta) if (merge and existing) else meta
            msg.horae = he.with_meta(msg.horae, swipe, final)
            await db.commit()
        await _sync_docs(db, session_id, [message_id], connection)
    return final


async def _sync_docs(db, session_id: int, ids: list[int] | None, connection: dict) -> None:
    from backend import horae_vector

    try:
        await horae_vector.sync_documents(db, session_id, connection, only_ids=ids)
        await db.commit()
    except Exception:  # noqa: BLE001 — поиск догонит на следующем ходу
        log.exception("Документы поиска Horae чата %s не обновились", session_id)
        await db.rollback()


# ---------------------------------------------------------------------------
# После хода
# ---------------------------------------------------------------------------
_turn_locks: dict = {}


def _turn_lock(session_id: int) -> asyncio.Lock:
    return he.loop_lock(_turn_locks, session_id)


async def after_turn(session_id: int, message_ids: list[int] | None = None) -> None:
    """
    Фоновая работа после ответа: анализ ответов без тегов (auto_analyze),
    документы поиска, авто-свёртка хронологии. Ничего не роняет.
    """
    async with _turn_lock(session_id):
        try:
            async with AsyncSessionLocal() as db:
                session = await db.get(models.ChatSession, session_id)
                if session is None:
                    return
                settings = await he.effective_settings(db, session)
                if not settings.get("enabled"):
                    return
                connection = await _connection(db)
                to_analyze = []
                mode = settings.get("auto_analyze") or "off"
                if settings.get("parse_tags") and mode != "off" and message_ids:
                    rows = (await db.execute(
                        select(models.Message).where(models.Message.id.in_(message_ids))
                    )).scalars().all()
                    for m in rows:
                        if (m.role == "assistant" and not he.is_side(m.horae)
                                and he.active_meta(m.horae, m.active_swipe) is None
                                and len(hs.strip_tags_text(m.content or "").strip()) >= SCAN_MIN_CHARS):
                            to_analyze.append(m.id)
                    if to_analyze and mode == "gaps" and not await _model_writes_tags(db, session_id):
                        to_analyze = []
            for mid in to_analyze:
                try:
                    await analyze_message(session_id, mid)
                except (HoraeTaskError, TaskCancelled) as exc:
                    log.info("Horae: анализ ответа %s не удался: %s", mid, exc)
                except Exception:  # noqa: BLE001
                    log.exception("Horae: анализ ответа %s упал", mid)
            async with AsyncSessionLocal() as db:
                # Полная сверка документов поиска — один раз на чат (старый чат,
                # импорт, ветка, после «Стереть»), дальше — только ответы хода.
                data = await he.load_chat_data(db, session_id)
                full = not data.get("docs_synced") or not message_ids
                await _sync_docs(db, session_id, None if full else message_ids, connection)
                if not data.get("docs_synced"):
                    async with he.chat_lock(session_id):
                        data = await he.load_chat_data(db, session_id)
                        data["docs_synced"] = True
                        await he.save_chat_data(db, session_id, data)
                        await db.commit()
            if settings.get("summary_enabled"):
                try:
                    await run_auto_summary(session_id, max_batches=1)
                except Exception:  # noqa: BLE001
                    log.exception("Horae: авто-свёртка чата %s упала", session_id)
        except Exception:  # noqa: BLE001 — фон не должен ничего ронять
            log.exception("Horae: фоновая обработка чата %s упала", session_id)


async def _model_writes_tags(db, session_id: int, recent: int = 10) -> bool:
    """Есть ли теги (source «tags»/«loose») хотя бы в одном из последних ответов."""
    rows = (await db.execute(
        select(models.Message.horae, models.Message.active_swipe)
        .where(models.Message.session_id == session_id, models.Message.role == "assistant")
        .order_by(models.Message.id.desc()).limit(recent)
    )).all()
    for r in rows:
        meta = he.active_meta(r.horae, r.active_swipe)
        if meta and meta.get("source") in ("tags", "loose"):
            return True
    return False


# ---------------------------------------------------------------------------
# Задания (скан, свёртка) — одно на чат, статус в памяти процесса
# ---------------------------------------------------------------------------
@dataclass
class HoraeJob:
    session_id: int
    kind: str
    status: str = "queued"
    processed: int = 0
    total: int = 0
    batches: int = 0
    error: str = ""
    line: str = ""
    warnings: list = field(default_factory=list)
    started_at: float = 0.0
    finished_at: float = 0.0
    cancel: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task | None = None

    def to_dict(self) -> dict:
        return {"kind": self.kind, "status": self.status, "processed": self.processed, "total": self.total,
                "batches": self.batches, "error": self.error or None, "line": self.line,
                "warnings": list(self.warnings)}


_jobs: dict[int, HoraeJob] = {}


def get_job(session_id: int) -> HoraeJob | None:
    return _jobs.get(session_id)


def job_active(session_id: int) -> bool:
    job = _jobs.get(session_id)
    return bool(job and job.status in ("queued", "running"))


def start_job(session_id: int, kind: str, runner) -> HoraeJob:
    if job_active(session_id):
        raise HoraeTaskError("для этого чата уже идёт задание Horae")
    job = HoraeJob(session_id=session_id, kind=kind)
    _jobs[session_id] = job

    async def wrapped():
        async with he.loop_lock(_gates, "jobs"):  # по одному заданию на процесс — общий ключ провайдера
            if job.cancel.is_set():
                job.status = "cancelled"
                return
            job.status = "running"
            job.started_at = time.time()
            try:
                await runner(job)
                job.status = "cancelled" if job.cancel.is_set() else "done"
            except TaskCancelled:
                job.status = "cancelled"
            except HoraeTaskError as exc:
                job.status, job.error = "error", str(exc)
            except Exception as exc:  # noqa: BLE001
                log.exception("Задание Horae чата %s упало", session_id)
                job.status, job.error = "error", f"{type(exc).__name__}: {exc}"
            finally:
                job.finished_at = time.time()

    job.task = asyncio.create_task(wrapped())
    return job


def cancel_job(session_id: int) -> HoraeJob | None:
    job = _jobs.get(session_id)
    if job and job.status in ("queued", "running"):
        job.cancel.set()
    return job


def forget_job(session_id: int) -> None:
    job = _jobs.pop(session_id, None)
    if job and job.status in ("queued", "running"):
        job.cancel.set()
    _turn_locks.pop(session_id, None)


# ---------------------------------------------------------------------------
# Пакетный скан истории
# ---------------------------------------------------------------------------
def _filter_scan(meta: dict, include: dict) -> dict:
    """
    Что скан пишет в старую историю. Костюмы, планы и удаления предметов —
    никогда (по одному сообщению вне хода их не восстановить честно, как и в
    плагине); сцена, NPC, расположение и отношения — только по галочкам.
    """
    meta = copy.deepcopy(meta)
    meta["costumes"] = {}
    meta["agenda"] = []
    meta["agenda_done"] = []
    meta["items_removed"] = []
    if not include.get("scene"):
        meta["scene"] = {"location": "", "atmosphere": "", "characters": [], "desc": []}
    if not include.get("npc"):
        meta["npcs"] = {}
    if not include.get("affection"):
        meta["affection"] = {}
    if not include.get("relationships"):
        meta["relationships"] = []
    return meta


def start_scan(session_id: int, *, batch_tokens: int = 80000, include: dict | None = None) -> HoraeJob:
    include = {k: bool((include or {}).get(k)) for k in ("npc", "affection", "scene", "relationships")}
    batch_tokens = max(10000, min(int(batch_tokens or 80000), 400000))

    async def runner(job: HoraeJob):
        from backend.horae_memory import estimate_tokens

        async with AsyncSessionLocal() as db:
            session, character, data, settings, names = await _load(db, session_id)
            connection = await _connection(db)
            rows = (await db.execute(
                select(models.Message).where(models.Message.session_id == session_id).order_by(models.Message.id)
            )).scalars().all()
        targets = []
        last_user = ""
        for m in rows:
            if m.role == "user":
                last_user = hs.strip_tags_text(m.content or "")
                continue
            if m.role != "assistant" or he.is_side(m.horae):
                continue
            if hs.meta_has_events(he.active_meta(m.horae, m.active_swipe)):
                continue
            text = hs.strip_tags_text(m.content or "").strip()
            if len(text) < SCAN_MIN_CHARS:
                continue
            text = text[:ANALYSIS_MAX_CHARS]
            if settings.get("anti_paraphrase") and last_user:
                text = f"[ДЕЙСТВИЕ ПОЛЬЗОВАТЕЛЯ]\n{last_user[:PREV_USER_CHARS]}\n\n[ОТВЕТ]\n{text}"
            targets.append((m.id, text, m.content or ""))
        job.total = len(targets)
        if not targets:
            job.warnings.append("сканировать нечего — у всех ответов уже есть события")
            return
        batches, cur, cur_tokens = [], [], 0
        for item in targets:
            cost = estimate_tokens(item[1])
            if cur and (cur_tokens + cost > batch_tokens or len(cur) >= SCAN_MAX_MESSAGES):
                batches.append(cur)
                cur, cur_tokens = [], 0
            cur.append(item)
            cur_tokens += cost
        if cur:
            batches.append(cur)
        ctx = he.parse_context(settings, names)
        failed = 0
        for batch in batches:
            if job.cancel.is_set():
                raise TaskCancelled()
            job.line = f"Пакет {job.batches + 1} из {len(batches)} · сообщений {len(batch)}"
            prompt = hp.batch_prompt(settings, [(mid, text) for mid, text, _ in batch],
                                     include=include, user=names.user, char=names.char)
            text = await aux_complete(
                [{"role": "system", "content": hp.EXTRACTOR_SYSTEM}, {"role": "user", "content": prompt}],
                settings, connection, max_tokens=min(32768, 700 * len(batch) + 1024), cancel=job.cancel)
            parts = hp.split_batch_response(text)
            written = []
            async with AsyncSessionLocal() as db:
                async with he.chat_lock(session_id):
                    for mid, _text, original in batch:
                        chunk = parts.get(mid)
                        if not chunk:
                            continue
                        _, meta = hs.parse_reply(chunk, ctx)
                        if not meta:
                            continue
                        meta = _filter_scan(meta, include)
                        if not hs.meta_has_data(meta):
                            continue
                        msg = await db.get(models.Message, mid)
                        if msg is None or (msg.content or "") != original:
                            continue  # удалено или переписано во время скана
                        pre = he.active_meta(msg.horae, msg.active_swipe)
                        merged = hs.merge_meta(pre, meta)
                        merged["source"] = "scan"
                        merged["scanned"] = True
                        merged["pre_scan"] = {k: v for k, v in (pre or {}).items() if k != "pre_scan"} if pre else None
                        await he.set_message_meta(db, msg, merged)
                        written.append(mid)
                    data = await he.load_chat_data(db, session_id)
                    scan = data.get("scan") or {"mids": [], "at": he.now_iso()}
                    scan["mids"] = sorted(set(scan.get("mids") or []) | set(written))
                    scan["at"] = he.now_iso()
                    data["scan"] = scan
                    await he.save_chat_data(db, session_id, data)
                    await db.commit()
                if written:
                    await _sync_docs(db, session_id, written, connection)
            if not written:
                failed += 1
            job.processed += len(batch)
            job.batches += 1
            job.line = f"[Обработано {job.processed}/{job.total} сообщений]"
        if failed == len(batches):
            raise HoraeTaskError("модель не вернула разметку ни для одного пакета — проверьте промпт скана")
        if failed:
            job.warnings.append(f"пакетов без разметки: {failed} из {len(batches)}")

    return start_job(session_id, "scan", runner)


async def undo_scan(session_id: int) -> int:
    """Отменить пакетный скан: меты просканированных сообщений — как до скана."""
    restored = 0
    async with AsyncSessionLocal() as db:
        async with he.chat_lock(session_id):
            data = await he.load_chat_data(db, session_id)
            mids = (data.get("scan") or {}).get("mids") or []
            for mid in mids:
                msg = await db.get(models.Message, mid)
                if msg is None or msg.session_id != session_id:
                    continue
                meta = he.active_meta(msg.horae, msg.active_swipe)
                if meta and meta.get("scanned"):
                    await he.set_message_meta(db, msg, meta.get("pre_scan"))
                    restored += 1
            data["scan"] = None
            await he.save_chat_data(db, session_id, data)
            await db.commit()
        connection = await _connection(db)
        await _sync_docs(db, session_id, mids or None, connection)
    return restored


# ---------------------------------------------------------------------------
# Авто-свёртка хронологии
# ---------------------------------------------------------------------------
_summary_locks: dict = {}


def _summary_lock(session_id: int) -> asyncio.Lock:
    return he.loop_lock(_summary_locks, session_id)


def _event_line(ev: dict) -> str:
    when = " ".join(x for x in (ev.get("date"), ev.get("time")) if x)
    return f"[{hs.LEVEL_RU.get(ev['level'], 'обычное')}] {when or '?'}: {ev['text']}"


def resummary_plan(summaries: list, cutoff: int, threshold: int) -> list | None:
    """Свёртки одного уровня, которых набралось на свёртку выше (порт _pickAutoResummaryPlan)."""
    if threshold <= 0:
        return None
    top = [s for s in summaries if s.get("kind") != "carry" and s.get("active", True)
           and he._range(s)[1] < cutoff]
    by_depth: dict[int, list] = {}
    for s in top:
        by_depth.setdefault(int(s.get("depth") or 1), []).append(s)
    for depth in sorted(by_depth):
        group = sorted(by_depth[depth], key=lambda s: he._range(s)[0])
        if len(group) >= max(2, threshold):
            return group[:threshold]
    return None


async def _messages_text(db, session_id: int, lo: int, hi: int, metas: dict) -> tuple[str, int]:
    from backend.horae_memory import estimate_tokens

    rows = (await db.execute(
        select(models.Message.id, models.Message.role, models.Message.content, models.Message.horae)
        .where(models.Message.session_id == session_id, models.Message.id >= lo, models.Message.id <= hi)
        .order_by(models.Message.id)
    )).all()
    parts, tokens = [], 0
    for r in rows:
        if he.is_side(r.horae):
            continue
        text = hs.strip_tags_text(r.content or "").strip()
        if not text:
            continue
        meta = metas.get(r.id) or {}
        when = " ".join(x for x in ((meta.get("time") or {}).get("date"), (meta.get("time") or {}).get("time")) if x)
        who = "пользователь" if r.role == "user" else ""
        head = " ".join(x for x in (f"#{r.id}", when, who) if x)
        parts.append(f"【{head}】\n{text}")
        tokens += estimate_tokens(text)
    return "\n\n".join(parts), tokens


async def run_auto_summary(session_id: int, *, max_batches: int = 1, force: bool = False,
                           job: HoraeJob | None = None) -> dict:
    """
    Порт checkAutoSummary. Ответы ИИ старше последних summary_keep_recent,
    не покрытые свёртками, копятся в «буфер»; когда их (или их токенов)
    больше порога — самый старый пакет сворачивается моделью в свёртку
    (📋 в хронологии), а покрытые сообщения окно может выбросить из промпта
    (summary_hides). Перед этим — свёртка свёрток, если их набралось.

    force — «Свернуть сейчас»: порог буфера не ждём (хватит одного ответа).
    """
    result = {"created": 0, "resummaries": 0, "reason": ""}
    async with _summary_lock(session_id):
        rounds = 0
        while True:
            if job is not None and job.cancel.is_set():
                raise TaskCancelled()
            async with AsyncSessionLocal() as db:
                session, character, data, settings, names = await _load(db, session_id)
                if not settings.get("enabled") or (not settings.get("summary_enabled") and not force):
                    result["reason"] = "авто-свёртка выключена"
                    return result
                connection = await _connection(db)
                comp = await he.compute(db, session, character=character, settings=settings, data=data)
                entries = comp.entries
                metas = {mid: meta for mid, _r, meta, _s in entries if isinstance(meta, dict)}
                ai = [mid for mid, role, _m, side in entries if role == "assistant" and not side]
                keep = int(settings.get("summary_keep_recent") or 5)
                if len(ai) <= keep:
                    result["reason"] = "мало ответов — всё в зоне последних"
                    return result
                cutoff = ai[-keep]
                summaries = data.get("summaries") or []
                plan = resummary_plan(summaries, cutoff, int(settings.get("resummary_threshold") or 0))
                if plan and rounds < RESUMMARY_ROUNDS:
                    rounds += 1
                    done = await _resummary(db, session_id, plan, comp, settings, connection, names, job)
                    if done:
                        result["resummaries"] += 1
                        continue
                # Буфер: ответы ИИ перед зоной последних, не покрытые ничем
                # (ни активной, ни развёрнутой свёрткой), — от самого нового назад.
                region = []
                for mid in reversed([m for m in ai if m < cutoff]):
                    if any(s.get("kind") != "carry" and he._range(s)[0] <= mid <= he._range(s)[1]
                           for s in summaries):
                        break
                    region.append(mid)
                region.reverse()
                if not region:
                    result["reason"] = result["reason"] or "сворачивать нечего"
                    return result
                prev_end = max([he._range(s)[1] for s in summaries
                                if s.get("kind") != "carry" and he._range(s)[1] < region[0]] or [0])
                lo = next((mid for mid, *_ in entries if mid > prev_end), region[0])
                _, buffer_tokens = await _messages_text(db, session_id, lo, cutoff - 1, metas)
                mode = settings.get("summary_buffer_mode")
                reached = (buffer_tokens > int(settings.get("summary_buffer_tokens") or 30000)
                           if mode == "tokens" else len(region) >= int(settings.get("summary_buffer_messages") or 10))
                if not reached and not force:
                    result["reason"] = "буфер ещё не набрался"
                    return result
                # Пакет: самые старые ответы буфера, пока не упрёмся в потолки.
                batch_max = int(settings.get("summary_batch_messages") or 50)
                token_cap = int(settings.get("summary_batch_tokens") or 80000)
                from backend.horae_memory import estimate_tokens

                rows = (await db.execute(
                    select(models.Message.id, models.Message.content)
                    .where(models.Message.id.in_(region))
                )).all()
                costs = {r.id: estimate_tokens(r.content or "") for r in rows}
                picked, used = [], 0
                for mid in region:
                    if picked and (len(picked) >= batch_max or used + costs.get(mid, 0) > token_cap):
                        break
                    picked.append(mid)
                    used += costs.get(mid, 0)
                hi = picked[-1]
                events = [e for e in comp.state["events"] if lo <= e["mid"] <= hi]
                events_text = "\n".join(_event_line(e) for e in events)
                fulltext = ""
                if settings.get("summary_source") == "fulltext":
                    fulltext, _ = await _messages_text(db, session_id, lo, hi, metas)
                if not events_text and not fulltext:
                    result["reason"] = "в буфере нет ни событий, ни текста"
                    return result
            if job is not None:
                job.line = f"Свёртка сообщений #{lo}–#{hi}"
            prompt = hp.summary_prompt(settings, events=events_text or "(событий нет)", fulltext=fulltext,
                                       count=len(events), user=names.user,
                                       source=settings.get("summary_source") or "fulltext")
            text = await _summarize(prompt, settings, connection, session_id, job)
            async with AsyncSessionLocal() as db:
                await he.add_summary(db, session_id, lo=lo, hi=hi, text=text, kind="auto", state=comp.state)
            result["created"] += 1
            if job is not None:
                job.processed += len(picked)
                job.batches += 1
            if result["created"] >= max_batches:
                return result


async def _summarize(prompt: str, settings: dict, connection: dict, session_id: int,
                     job: HoraeJob | None) -> str:
    try:
        text = await aux_complete([{"role": "system", "content": hp.SUMMARIZER_SYSTEM},
                                   {"role": "user", "content": prompt}], settings, connection,
                                  max_tokens=4096, cancel=job.cancel if job else None)
        summary = hp.extract_summary(text)
    except hp.TruncatedSummary as exc:
        await _record_summary_error(session_id, str(exc))
        raise HoraeTaskError(str(exc)) from exc
    except HoraeTaskError as exc:
        await _record_summary_error(session_id, str(exc))
        raise
    if not summary:
        msg = "модель не обернула свёртку в <horaesummary> — свёртка не записана"
        await _record_summary_error(session_id, msg)
        raise HoraeTaskError(msg)
    return summary


async def _record_summary_error(session_id: int, message: str) -> None:
    try:
        async with AsyncSessionLocal() as db:
            async with he.chat_lock(session_id):
                data = await he.load_chat_data(db, session_id)
                data["summary_error"] = {"message": message, "at": he.now_iso()}
                await he.save_chat_data(db, session_id, data)
                await db.commit()
    except Exception:  # noqa: BLE001
        log.exception("Horae: ошибка свёртки не записалась")


async def _resummary(db, session_id: int, plan: list, comp, settings: dict, connection: dict, names,
                     job: HoraeJob | None) -> bool:
    lo = min(he._range(s)[0] for s in plan)
    hi = max(he._range(s)[1] for s in plan)
    records = []
    for s in plan:
        a, b = he._range(s)
        records.append((a, f"[Свёртка L{int(s.get('depth') or 1)} · #{a}–#{b}] {s.get('text', '').strip()}"))
    for ev in comp.state["events"]:
        if lo <= ev["mid"] <= hi and not any(he._range(s)[0] <= ev["mid"] <= he._range(s)[1] for s in plan):
            records.append((ev["mid"], _event_line(ev)))
    records.sort(key=lambda r: r[0])
    body = "\n".join(text for _, text in records)
    if len(body) < int(settings.get("resummary_min_chars") or 0):
        return False
    prompt = hp.resummary_prompt(settings, records=body, count=len(records), user=names.user)
    text = await _summarize(prompt, settings, connection, session_id, job)
    depth = 1 + max(int(s.get("depth") or 1) for s in plan)
    await he.add_summary(db, session_id, lo=lo, hi=hi, text=text, kind="auto", depth=depth,
                         children=plan, state=comp.state)
    return True


def start_summary_job(session_id: int) -> HoraeJob:
    async def runner(job: HoraeJob):
        result = await run_auto_summary(session_id, max_batches=20, force=True, job=job)
        job.line = (f"Создано свёрток: {result['created']}, свёрток выше: {result['resummaries']}"
                    if result["created"] or result["resummaries"] else "")
        if not result["created"] and not result["resummaries"] and result.get("reason"):
            job.warnings.append(result["reason"])

    return start_job(session_id, "summary", runner)


# ---------------------------------------------------------------------------
# Ручное сжатие выбранных событий
# ---------------------------------------------------------------------------
async def compress(session_id: int, refs: list[dict], summary_ids: list[str], mode: str = "events") -> dict:
    """Сжать выбранные события (и свёртки) в одну свёртку (порт compressSelectedTimelineEvents)."""
    wanted = set()
    for ref in refs or []:
        try:
            wanted.add((int(ref["mid"]), int(ref["i"])))
        except (KeyError, TypeError, ValueError):
            continue
    async with AsyncSessionLocal() as db:
        session, character, data, settings, names = await _load(db, session_id)
        connection = await _connection(db)
        comp = await he.compute(db, session, character=character, settings=settings, data=data)
        events = [e for e in comp.state["events"] if (e["mid"], e["i"]) in wanted]
        chosen = [s for s in data.get("summaries") or [] if s.get("id") in set(summary_ids or [])
                  and s.get("kind") != "carry"]
        if len(events) + len(chosen) < 2:
            raise HoraeTaskError("выберите хотя бы два события или свёртки")
        mids = [e["mid"] for e in events] + [m for s in chosen for m in he._range(s)]
        lo, hi = min(mids), max(mids)
        records = [(e["mid"], _event_line(e)) for e in events]
        records += [(he._range(s)[0], f"[Свёртка] {s.get('text', '').strip()}") for s in chosen]
        records.sort(key=lambda r: r[0])
        events_text = "\n".join(t for _, t in records)
        fulltext = ""
        if mode == "fulltext":
            metas = {mid: meta for mid, _r, meta, _s in comp.entries if isinstance(meta, dict)}
            fulltext, _ = await _messages_text(db, session_id, lo, hi, metas)
    prompt = hp.compress_prompt(settings, "fulltext" if mode == "fulltext" else "events", events=events_text,
                                fulltext=fulltext, count=len(records), user=names.user)
    text = await _summarize(prompt, settings, connection, session_id, None)
    depth = 1 + max([int(s.get("depth") or 1) for s in chosen] or [0])
    async with AsyncSessionLocal() as db:
        return await he.add_summary(db, session_id, lo=lo, hi=hi, text=text, kind="compress",
                                    depth=max(1, depth), children=chosen, state=comp.state)


# ---------------------------------------------------------------------------
# ИИ-заполнение профиля NPC
# ---------------------------------------------------------------------------
async def npc_enrich(session_id: int, name: str, aliases: list[str] | None = None) -> dict:
    name = (name or "").strip()
    if not name:
        raise HoraeTaskError("не указано имя")
    aliases = [a.strip() for a in aliases or [] if a and a.strip()]
    async with AsyncSessionLocal() as db:
        session, character, data, settings, names = await _load(db, session_id)
        connection = await _connection(db)
        rows = (await db.execute(
            select(models.Message.id, models.Message.role, models.Message.content)
            .where(models.Message.session_id == session_id).order_by(models.Message.id)
        )).all()
    keys = [k.lower()[:-1] if len(k) >= 5 else k.lower() for k in [name, *aliases]]
    hits = [r for r in rows if any(k in (r.content or "").lower() for k in keys)][-20:]
    if not hits:
        raise HoraeTaskError(f"в истории нет упоминаний «{name}»")
    snippets = [f"[#{r.id}|{'ПОЛЬЗОВАТЕЛЬ' if r.role == 'user' else 'ИИ'}] "
                f"{hs.strip_tags_text(r.content or '')[:1000]}" for r in hits]
    text = await aux_complete(hp.npc_enrich_messages(name, aliases, snippets, names.user), settings, connection,
                              max_tokens=1024)
    fields = hp.parse_npc_enrich(text)
    if not fields:
        raise HoraeTaskError("модель не вернула профиль")
    return {"fields": fields, "hits": len(hits)}


# ---------------------------------------------------------------------------
# Переписывание запроса для поиска
# ---------------------------------------------------------------------------
async def rewrite_query(db, session, settings: dict, connection: dict) -> tuple[str, list[str]]:
    rows = (await db.execute(
        select(models.Message.role, models.Message.content).where(models.Message.session_id == session.id)
        .order_by(models.Message.id.desc()).limit(6)
    )).all()
    lines = []
    for r in reversed(rows):
        text = hs.strip_tags_text(r.content or "").strip()
        if len(text) > 1500:
            text = text[:600] + "\n…(середина пропущена)…\n" + text[-600:]
        if text:
            lines.append(f"[{'user' if r.role == 'user' else 'assistant'}] {text}")
    if not lines:
        return "", []
    text = await aux_complete(hp.query_rewrite_messages(settings, "\n\n".join(lines)), settings, connection,
                              max_tokens=1024, temperature=0.7)
    return hp.parse_query_rewrite(text)


# ---------------------------------------------------------------------------
# Новый чат с памятью
# ---------------------------------------------------------------------------
async def carryover(session_id: int, *, keep: int = 5, vectors: bool = True, user=None) -> int:
    """
    Новый чат, который продолжает этот (порт createNewChatWithCarryover):
    последние keep ответов с репликами между ними — дословно, всё до них —
    стартовым состоянием (seed) и пересказом (свёртки и непокрытые события),
    плюс, если vectors, память поиска этого чата и её предков.
    """
    keep = max(1, min(int(keep or 5), 200))
    async with AsyncSessionLocal() as db:
        src = await db.get(models.ChatSession, session_id)
        if src is None:
            raise HoraeTaskError("чат не найден")
        character = await db.get(models.Character, src.character_id)
        data = await he.load_chat_data(db, session_id)
        settings = await he.effective_settings(db, src, character, data)
        rows = (await db.execute(
            select(models.Message).where(models.Message.session_id == session_id).order_by(models.Message.id)
        )).scalars().all()
        ai = [m for m in rows if m.role == "assistant" and not he.is_side(m.horae)]
        if ai:
            first = ai[-keep:][0]
            prev = [m for m in rows if m.id < first.id]
            start_id = prev[-1].id if prev and prev[-1].role == "user" else first.id
        else:
            start_id = rows[-1].id if rows else 0
        comp = await he.compute(db, src, character=character, until=start_id - 1, settings=settings, data=data)
        base = (src.title or "Чат").strip()
        dst = models.ChatSession(
            title=(base + " · продолжение")[:200], character_id=src.character_id, user_key=src.user_key,
            owner_id=src.owner_id, author_note=src.author_note, persona_id=src.persona_id,
            background=src.background, timezone=src.timezone, is_group=src.is_group,
            director=src.director, scenario=src.scenario,
        )
        db.add(dst)
        await db.flush()
        for m in rows:
            if m.id < start_id:
                continue
            db.add(models.Message(
                session_id=dst.id, role=m.role, content=m.content, attachments=list(m.attachments or []),
                swipes=list(m.swipes or []), active_swipe=m.active_swipe, model_used=m.model_used,
                speaker_name=m.speaker_name, reply_to_id=None, canvas_id=None,
                horae=copy.deepcopy(m.horae) if m.horae else None, created_at=m.created_at,
            ))
        if src.is_group:
            for gm in (await db.execute(
                select(models.GroupMember).where(models.GroupMember.session_id == session_id)
            )).scalars().all():
                db.add(models.GroupMember(session_id=dst.id, character_id=gm.character_id))
        new_data = he.blank_chat_data()
        new_data["seed"] = he.seed_from_state(comp.state)
        new_data["summaries"] = he.recap_summaries(data, comp.state, start_id)
        for key in ("settings", "rpg_config", "pinned_npcs", "favorite_npcs"):
            new_data[key] = copy.deepcopy(data.get(key) or new_data[key])
        for table in comp.tables:
            current = (comp.table_results.get(table["id"]) or {})
            carried = {**table, "base": dict(current.get("data") or table.get("base") or {}),
                       "rows": current.get("rows", table.get("rows")), "cols": current.get("cols", table.get("cols")),
                       "base_anchor": 0}
            template, overlay = horae_tables.split_effective(carried)
            if template is None:
                new_data["tables"].append(overlay)
            else:
                new_data["table_overlays"][table["id"]] = overlay
        await he.save_chat_data(db, dst.id, new_data)
        if vectors:
            from backend import horae_vector

            await horae_vector.carry_documents(db, session_id, dst.id)
        await db.commit()
        return dst.id
