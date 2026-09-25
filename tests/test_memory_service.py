"""
Сервис памяти (backend/memory_service.py): ежеходный инкремент на новом движке,
задания пересборки, статус, сброс и экспорт. LLM подменяется через
backend.main.complete — как во всех тестах памяти.
"""
from unittest.mock import patch

from sqlalchemy import select

from backend import hierarchical_memory as hm
from backend import horae_recall as hr


async def _fresh_db():
    from backend.database import engine, init_db
    await engine.dispose()
    await init_db()


async def _make_chat(n, prefix="событие", persona=None):
    from backend import models
    from backend.database import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        ch = models.Character(name="Эльвира")
        db.add(ch)
        await db.flush()
        pid = None
        if persona:
            p = models.Persona(name=persona)
            db.add(p)
            await db.flush()
            pid = p.id
        sess = models.ChatSession(character_id=ch.id, user_key="test:hms", persona_id=pid)
        db.add(sess)
        await db.flush()
        msgs = [models.Message(session_id=sess.id, role="user" if i % 2 == 0 else "assistant",
                               content=f"{prefix} {i}") for i in range(n)]
        db.add_all(msgs)
        await db.commit()
        return ch.id, sess.id, [m.id for m in msgs]


async def _entry(sid):
    from backend import models
    from backend.database import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        return (await db.execute(select(models.HoraeEntry).where(
            models.HoraeEntry.session_id == sid,
            models.HoraeEntry.category == "summary"))).scalars().first()


def _snapshot_llm(seen):
    async def fake_complete(messages, params=None, connection=None, kind="service"):
        seen.append(messages)
        if messages[0]["content"] == hr.FACTS_PROMPT:
            return ""
        user = messages[-1]["content"] if messages[-1]["role"] == "user" else messages[1]["content"]
        rng = user.split("[Новые события ", 1)[1].split("]", 1)[0] if "[Новые события " in user else "#?"
        return hm.ENVELOPE_OPEN + hm.render_snapshot({hm.SEC_CHRONICLE: f"- [{rng}] сжато"}) + hm.ENVELOPE_CLOSE
    return fake_complete


async def test_incremental_uses_master_prompt_names_and_new_schema():
    from backend import main
    from backend.database import engine
    await _fresh_db()
    _, sid, ids = await _make_chat(12 + hr.DEFAULT_WINDOW, persona="Артур")
    seen = []
    with patch("backend.main.complete", new=_snapshot_llm(seen)):
        await main._maybe_update_summary(sid)
    first = seen[0]
    assert first[0]["content"] == hm.MASTER_STATE_PROMPT
    assert "· Артур] событие 0" in first[1]["content"]    # имя персоны, а не «Пользователь»
    assert "· Эльвира] событие 1" in first[1]["content"]  # имя персонажа, а не «Персонаж»
    entry = await _entry(sid)
    assert entry.meta["schema"] == hm.SNAPSHOT_SCHEMA and entry.meta["v"] == hr.SUMMARY_FORMAT
    assert entry.meta["last_message_id"] == ids[11] and entry.meta["tokens"] > 0
    assert f"## [{hm.SEC_CHRONICLE}]" in entry.content
    await engine.dispose()


async def test_incremental_passes_sampling_overrides_and_keeps_safety_off():
    from backend import llm_gateway, main
    from backend.database import engine
    await _fresh_db()
    _, sid, _ = await _make_chat(12 + hr.DEFAULT_WINDOW)
    seen = []

    async def fake_complete(messages, params=None, connection=None, kind="service"):
        seen.append((params, dict(llm_gateway._SAMPLING_OVERRIDES.get() or {}), kind))
        return await _snapshot_llm([])(messages, params, connection, kind)

    with patch("backend.main.complete", new=fake_complete):
        await main._maybe_update_summary(sid)
    params, overrides, kind = seen[0]
    assert params is None and kind == "summary"
    assert overrides["temperature"] == 0.2 and overrides["max_tokens"] >= 12_000
    await engine.dispose()


async def test_long_snapshot_is_not_cut_to_6000_chars():
    from backend import main
    from backend.database import engine
    await _fresh_db()
    _, sid, _ = await _make_chat(12 + hr.DEFAULT_WINDOW)
    big = "\n".join(f"- [#{i}–#{i}] длинное событие номер {i} с подробностями" for i in range(400))

    async def fake_complete(messages, params=None, connection=None, kind="service"):
        if messages[0]["content"] == hr.FACTS_PROMPT:
            return ""
        return hm.render_snapshot({hm.SEC_CHRONICLE: big})

    with patch("backend.main.complete", new=fake_complete):
        await main._maybe_update_summary(sid)
    entry = await _entry(sid)
    assert len(entry.content) > 6000 and "номер 399" in entry.content
    await engine.dispose()


async def test_incremental_batch_carries_files_time_and_skips_blank_messages():
    """
    Сообщение только с вложением идёт в пакет строкой «📎» и проходит сверку
    куска перед записью: «непустое» у загрузки и у сверки — одно правило, иначе
    пакет с картинкой не записался бы никогда. Пустое сообщение модели не
    уходит. Время — в поясе чата, автор system-сообщения — «Система».
    """
    from datetime import datetime

    from backend import main, models
    from backend.database import AsyncSessionLocal, engine
    await _fresh_db()
    _, sid, ids = await _make_chat(12 + hr.DEFAULT_WINDOW)
    async with AsyncSessionLocal() as db:
        (await db.get(models.ChatSession, sid)).timezone = "Europe/Moscow"
        (await db.get(models.Message, ids[0])).created_at = datetime(2026, 9, 20, 11, 3)  # naive UTC
        pic = await db.get(models.Message, ids[2])
        pic.content = ""
        pic.attachments = [{"type": "image", "name": "map.png", "mime": "image/png", "size": 10}]
        (await db.get(models.Message, ids[3])).content = "  \n "
        (await db.get(models.Message, ids[4])).role = "system"
        await db.commit()
    seen = []
    with patch("backend.main.complete", new=_snapshot_llm(seen)):
        await main._maybe_update_summary(sid)
    merge = next(m for m in seen if m[0]["content"] == hm.MASTER_STATE_PROMPT)[1]["content"]
    assert f"[#{ids[0]} · 2026-09-20 14:03 · Пользователь] событие 0" in merge
    assert f"[#{ids[2]} · " in merge and "📎 изображение «map.png»" in merge
    assert f"[#{ids[3]} · " not in merge
    assert "· Система] событие 4" in merge
    assert (await _entry(sid)).meta["last_message_id"] == ids[11]
    await engine.dispose()


async def test_rebuild_mode_writes_every_batch_to_the_buffer():
    """
    Режим задания «Пересобрать»: каждый пакет — в буфер (и последний тоже:
    подмена снимка — дело задания), живой снимок не трогается, поля буфера,
    которые завело задание, сохраняются.
    """
    from backend import memory_service, models
    from backend.database import AsyncSessionLocal, engine
    await _fresh_db()
    _, sid, ids = await _make_chat(30 + hr.DEFAULT_WINDOW)
    async with AsyncSessionLocal() as db:
        db.add(models.HoraeEntry(
            session_id=sid, category="summary", title="t", content="ЖИВОЙ", always_on=True,
            enabled=True, meta={"last_message_id": ids[29], "v": 2, "rebuild": {
                "content": "", "last_message_id": 0, "manual": True}}))
        await db.commit()

    async def no_connection(db):
        return {}

    deps = memory_service.MemoryDeps(complete=_snapshot_llm([]), get_connection=no_connection)
    manager, _ = memory_service.build_manager({}, {"memory_batch": 20}, deps)
    source = memory_service.DbBatchSource(
        sid, mode="rebuild", threshold=1, manager=manager, want_facts=False,
        connection={}, window=hr.DEFAULT_WINDOW)
    result = await manager.scan_and_compress_history(source)
    assert result.status == "done" and result.batches == 2 and result.processed == 30
    entry = await _entry(sid)
    assert entry.content == "ЖИВОЙ" and entry.meta["last_message_id"] == ids[29]
    buffer = entry.meta["rebuild"]
    assert buffer["last_message_id"] == ids[29] and buffer["manual"] is True
    assert f"## [{hm.SEC_CHRONICLE}]" in buffer["content"]
    await engine.dispose()


async def test_summary_pass_reports_backlog_left_not_the_limit_status():
    """
    _summary_pass — один пакет; True, только если после него бэклог остался.
    Статус прогона тут не годится: при max_batches=1 ядро ставит «limit» и
    тогда, когда пакет забрал всё.
    """
    from backend import main
    from backend.database import engine
    await _fresh_db()
    _, sid, ids = await _make_chat(30 + hr.DEFAULT_WINDOW)  # 30 старше окна, пакет — 20
    with patch("backend.main.complete", new=_snapshot_llm([])):
        assert await main._summary_pass(sid) is True
        assert (await _entry(sid)).meta["last_message_id"] == ids[19]
        assert await main._summary_pass(sid) is False
    assert (await _entry(sid)).meta["last_message_id"] == ids[29]
    await engine.dispose()


async def test_batch_without_readable_text_still_moves_the_pointer():
    """
    Пакет, где после чистки не осталось текста (одни рассуждения <think>),
    модели не отправляется, но указатель его проходит: иначе каждый ход
    собирал бы тот же пакет, и память застряла бы на нём навсегда.
    """
    from backend import main, models
    from backend.database import AsyncSessionLocal, engine
    await _fresh_db()
    _, sid, ids = await _make_chat(12 + hr.DEFAULT_WINDOW)
    async with AsyncSessionLocal() as db:
        for mid in ids[:12]:
            (await db.get(models.Message, mid)).content = "<think>размышления модели</think>"
        await db.commit()
    seen = []
    with patch("backend.main.complete", new=_snapshot_llm(seen)):
        await main._maybe_update_summary(sid)
    assert seen == []
    assert (await _entry(sid)).meta["last_message_id"] == ids[11]
    await engine.dispose()
