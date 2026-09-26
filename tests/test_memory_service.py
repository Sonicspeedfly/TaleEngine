"""
Сервис памяти (backend/memory_service.py): ежеходный инкремент на новом движке,
задания пересборки, статус, сброс и экспорт. LLM подменяется через
backend.main.complete — как во всех тестах памяти.
"""
import asyncio
from unittest.mock import patch

import pytest
from sqlalchemy import select

from backend import hierarchical_memory as hm
from backend import horae_recall as hr


@pytest.fixture(autouse=True)
def _no_jobs_from_other_tests():
    """
    Реестр заданий живёт в процессе, а тестовая БД общая: SQLite отдаёт id
    удалённого последнего чата новому, и завершённое задание чужого теста
    всплывало бы в статусе чата этого. Исход тогда зависел бы от порядка
    тестов (подмножество падало, полный файл — нет).
    """
    from backend import memory_service
    memory_service._jobs.clear()
    yield
    memory_service._jobs.clear()


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


async def test_batch_time_honours_offset_and_survives_impossible_one():
    """
    Пояс чата — свободный ввод в настройках. IANA-имя и смещения «UTC+3» и
    «+03:00» переводят время строки пакета, а мусор, неизвестный пояс и
    невозможное «UTC+25» оставляют его в UTC. Раньше timezone() бросал
    ValueError на смещении от 24 часов: загрузка пакета падала на каждом ходу,
    и память чата переставала обновляться.
    """
    from datetime import datetime

    from backend import main, models
    from backend.database import AsyncSessionLocal, engine
    await _fresh_db()
    for tz, shown in (("Europe/Moscow", "14:03"), ("UTC+3", "14:03"), ("+03:00", "14:03"),
                      ("Nowhere/Land", "11:03"), ("мусор", "11:03"), ("UTC+25", "11:03")):
        _, sid, ids = await _make_chat(12 + hr.DEFAULT_WINDOW)
        async with AsyncSessionLocal() as db:
            (await db.get(models.ChatSession, sid)).timezone = tz
            (await db.get(models.Message, ids[0])).created_at = datetime(2026, 9, 20, 11, 3)  # naive UTC
            await db.commit()
        seen = []
        with patch("backend.main.complete", new=_snapshot_llm(seen)):
            await main._maybe_update_summary(sid)
        merges = [m[1]["content"] for m in seen if m[0]["content"] == hm.MASTER_STATE_PROMPT]
        assert merges, f"{tz}: пакет не ушёл модели"
        assert f"[#{ids[0]} · 2026-09-20 {shown} · Пользователь] событие 0" in merges[0], tz
        assert (await _entry(sid)).meta["last_message_id"] == ids[11], tz
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


# ============================================================================
# Задания «Пересобрать»/«Догнать», статус, сброс, экспорт (§6.5–§6.6, §7)
# ============================================================================
async def _run_rebuild(sid, **kw):
    from backend import main, memory_service
    job = await memory_service.start_job(sid, main._memory_deps(), mode=kw.pop("mode", "rebuild"), **kw)
    await memory_service.wait_job(sid)
    return job


async def test_rebuild_keeps_old_snapshot_until_atomic_swap():
    from backend import models
    from backend.database import AsyncSessionLocal, engine
    await _fresh_db()
    _, sid, ids = await _make_chat(40 + hr.DEFAULT_WINDOW)
    async with AsyncSessionLocal() as db:
        db.add(models.HoraeEntry(session_id=sid, category="summary", title="t",
                                 content="СТАРЫЙ СНИМОК", always_on=True, enabled=True,
                                 meta={"last_message_id": ids[39], "v": 2}))
        await db.commit()
    during = []

    async def fake_complete(messages, params=None, connection=None, kind="service"):
        during.append((await _entry(sid)).content)
        return await _snapshot_llm([])(messages, params, connection, kind)

    with patch("backend.main.complete", new=fake_complete):
        job = await _run_rebuild(sid, batch_size=10)
    assert job.status == "done" and job.processed == 40
    assert all(c == "СТАРЫЙ СНИМОК" for c in during)       # до подмены работает старый
    entry = await _entry(sid)
    assert "СТАРЫЙ СНИМОК" not in entry.content and "rebuild" not in entry.meta
    assert entry.meta["updated_by"] == "rebuild" and entry.meta["last_message_id"] == ids[39]
    assert "[Обработано 40/40 сообщений" in job.line
    await engine.dispose()


async def test_rebuild_resumes_from_checkpoint():
    from backend import models
    from backend.database import AsyncSessionLocal, engine
    await _fresh_db()
    _, sid, ids = await _make_chat(30 + hr.DEFAULT_WINDOW)
    async with AsyncSessionLocal() as db:
        db.add(models.HoraeEntry(
            session_id=sid, category="summary", title="t", content="СТАРЫЙ", always_on=True,
            enabled=True, meta={"last_message_id": ids[29], "v": 2, "rebuild": {
                "content": hm.render_snapshot({hm.SEC_CHRONICLE: "- [#a–#b] уже сделано"}),
                "last_message_id": ids[19], "manual": True}}))
        await db.commit()
    seen = []
    with patch("backend.main.complete", new=_snapshot_llm(seen)):
        job = await _run_rebuild(sid, resume=True, batch_size=20)
    merges = [m for m in seen if m[0]["content"] == hm.MASTER_STATE_PROMPT]
    assert len(merges) == 1 and "уже сделано" in merges[0][1]["content"]
    assert f"#{ids[20]}–#{ids[29]}" in merges[0][1]["content"]
    assert job.processed == 10
    await engine.dispose()


async def test_cancelled_rebuild_keeps_buffer_shows_staging_and_resumes():
    """
    Остановка идущей пересборки: начатый пакет доводится и остаётся в буфере,
    старый снимок работает дальше, статус показывает прерванную (остановленную)
    пересборку, а бэклог — от живого снимка: остановленный буфер ежеходные
    проходы не продолжают (задача 11). «Продолжить» доделывает остаток и
    подменяет снимок.
    """
    from backend import memory_service, models
    from backend.database import AsyncSessionLocal, engine
    await _fresh_db()
    _, sid, ids = await _make_chat(30 + hr.DEFAULT_WINDOW)
    async with AsyncSessionLocal() as db:
        db.add(models.HoraeEntry(session_id=sid, category="summary", title="t",
                                 content="СТАРЫЙ", always_on=True, enabled=True,
                                 meta={"last_message_id": ids[29], "v": 2}))
        await db.commit()

    async def cancel_on_first_merge(messages, params=None, connection=None, kind="service"):
        if messages[0]["content"] == hm.MASTER_STATE_PROMPT:
            assert memory_service.cancel_job(sid) is not None   # задание идёт — отмена принята
        return await _snapshot_llm([])(messages, params, connection, kind)

    with patch("backend.main.complete", new=cancel_on_first_merge):
        job = await _run_rebuild(sid, batch_size=10)
    assert job.status == "cancelled" and job.processed == 10 and job.finished_at
    entry = await _entry(sid)
    assert entry.content == "СТАРЫЙ" and entry.meta["rebuild"]["last_message_id"] == ids[9]
    async with AsyncSessionLocal() as db:
        st = await memory_service.status(db, sid)
    assert st["staging"]["last_message_id"] == ids[9] and st["staging"]["manual"] is True
    assert st["staging"]["paused"] is True
    assert st["backlog"]["pending"] == 0 and st["snapshot"]["covered_upto"] == ids[29]
    assert st["job"]["status"] == "cancelled"

    with patch("backend.main.complete", new=_snapshot_llm([])):
        job = await _run_rebuild(sid, resume=True, batch_size=10)
    assert job.status == "done" and job.processed == 20
    entry = await _entry(sid)
    assert entry.content != "СТАРЫЙ" and "rebuild" not in entry.meta
    await engine.dispose()


async def test_catchup_folds_backlog_into_live_snapshot():
    from backend.database import engine
    await _fresh_db()
    _, sid, ids = await _make_chat(45 + hr.DEFAULT_WINDOW)
    with patch("backend.main.complete", new=_snapshot_llm([])):
        job = await _run_rebuild(sid, mode="catchup", batch_size=20)
    assert job.status == "done" and job.processed == 45 and job.batches == 3
    assert (await _entry(sid)).meta["last_message_id"] == ids[44]
    await engine.dispose()


async def test_api_error_marks_job_failed_and_keeps_pointer():
    from backend.database import engine
    await _fresh_db()
    _, sid, _ = await _make_chat(30 + hr.DEFAULT_WINDOW)

    class Denied(Exception):
        status_code = 401

    async def denied(messages, params=None, connection=None, kind="service"):
        raise Denied("no key")

    with patch("backend.main.complete", new=denied):
        job = await _run_rebuild(sid, mode="catchup")
    assert job.status == "error" and job.error
    assert await _entry(sid) is None
    await engine.dispose()


async def test_job_ends_with_error_when_chat_is_deleted_mid_run():
    """
    Чат удалили, пока модель сворачивала пакет: задание не падает и не
    отчитывается «готово» (памяти больше некуда писать), а завершается ошибкой;
    записи памяти удалённого чата не воскресают.
    """
    from sqlalchemy import delete

    from backend import models
    from backend.database import AsyncSessionLocal, engine
    await _fresh_db()
    _, sid, _ = await _make_chat(30 + hr.DEFAULT_WINDOW)

    async def delete_chat_then_answer(messages, params=None, connection=None, kind="service"):
        async with AsyncSessionLocal() as db:
            for model in (models.Message, models.HoraeEntry, models.HoraeFact):
                await db.execute(delete(model).where(model.session_id == sid))
            await db.execute(delete(models.ChatSession).where(models.ChatSession.id == sid))
            await db.commit()
        return await _snapshot_llm([])(messages, params, connection, kind)

    with patch("backend.main.complete", new=delete_chat_then_answer):
        job = await _run_rebuild(sid, mode="catchup")
    assert job.status == "error" and "удал" in job.error and job.finished_at
    assert await _entry(sid) is None
    await engine.dispose()


async def test_job_of_deleted_chat_never_writes_into_new_chat_with_same_id():
    """
    Чат удалили посреди задания и сразу завели новый — SQLite отдал ему тот же
    id, а сообщениям те же id и тот же текст (чат удалили и загрузили заново).
    Сверка куска такое пропускает. Старое задание всё равно не пишет в новый
    чат ни пакета и не берёт из него следующий, завершается ошибкой, а в
    реестре нового чата его нет: «Догнать» у нового чата запускается.
    """
    from backend import main, memory_service
    from backend.database import AsyncSessionLocal, engine
    await _fresh_db()
    _, sid, ids = await _make_chat(30 + hr.DEFAULT_WINDOW)
    merges = []

    async def delete_and_recreate(messages, params=None, connection=None, kind="service"):
        if messages[0]["content"] == hm.MASTER_STATE_PROMPT:
            merges.append(messages)
            if len(merges) == 1:
                async with AsyncSessionLocal() as db:
                    await main.delete_session(sid, user=None, db=db)
                _, new_sid, new_ids = await _make_chat(30 + hr.DEFAULT_WINDOW)
                assert (new_sid, new_ids) == (sid, ids)      # id достались новому чату
        return await _snapshot_llm([])(messages, params, connection, kind)

    with patch("backend.main.complete", new=delete_and_recreate):
        job = await _run_rebuild(sid, mode="catchup", batch_size=10)
    assert job.status == "error" and "удал" in job.error
    assert len(merges) == 1                   # следующий пакет из нового чата не взят
    assert await _entry(sid) is None          # и в его память ничего не записано
    assert memory_service.get_job(sid) is None
    async with AsyncSessionLocal() as db:
        assert (await memory_service.status(db, sid))["job"] is None

    with patch("backend.main.complete", new=_snapshot_llm([])):
        fresh = await _run_rebuild(sid, mode="catchup", batch_size=10)
    assert fresh is not job and fresh.status == "done" and fresh.processed == 30
    await engine.dispose()


async def test_export_markdown_names_file_by_title_and_appends_facts():
    """
    Имя файла — «memory-<название>-<id>.md»: буквы и цифры (латиница и
    кириллица) остаются, всё прочее — «_». Факты — приложением, только по просьбе.
    """
    from backend import memory_service, models
    from backend.database import AsyncSessionLocal, engine
    await _fresh_db()
    _, sid, ids = await _make_chat(3)
    async with AsyncSessionLocal() as db:
        (await db.get(models.ChatSession, sid)).title = "Замок / Tower #1"
        db.add(models.HoraeEntry(
            session_id=sid, category="summary", title="t", always_on=True, enabled=True,
            content=hm.render_snapshot({hm.SEC_CHRONICLE: "- [#1–#2] ворота открыты"}),
            meta={"last_message_id": ids[1], "v": 2, "schema": hm.SNAPSHOT_SCHEMA, "tokens": 42}))
        db.add(models.HoraeFact(session_id=sid, content="ключ у стража", source_message_id=ids[1]))
        await db.commit()
        name, text = await memory_service.export_markdown(db, sid, include_facts=True)
        _, plain = await memory_service.export_markdown(db, sid)
    assert name == f"memory-Замок_Tower_1-{sid}.md"
    assert text.startswith("# Мастер-снимок памяти — «Замок / Tower #1»")
    assert "Персонаж: Эльвира" in text and f"Учтено до: #{ids[1]}" in text
    assert "ворота открыты" in text and "## Приложение: атомарные факты (1)\n- ключ у стража" in text
    assert "Приложение" not in plain
    await engine.dispose()


def test_memory_api_status_conflict_purge_export(client):
    from backend import memory_service, models
    from backend.database import AsyncSessionLocal
    cid = client.post("/api/characters", json={"name": "Хранитель"}).json()["id"]
    sid = client.post(f"/api/sessions?character_id={cid}").json()["session_id"]

    async def seed():
        async with AsyncSessionLocal() as db:
            for i in range(80):
                db.add(models.Message(session_id=sid, role="user" if i % 2 == 0 else "assistant",
                                      content=f"реплика {i}"))
            await db.commit()
    client.portal.call(seed)

    st = client.get(f"/api/sessions/{sid}/memory").json()
    assert st["snapshot"]["exists"] is False and st["backlog"]["pending"] == 80 - hr.DEFAULT_WINDOW
    assert st["backlog"]["window"] == hr.DEFAULT_WINDOW and st["job"] is None
    assert client.get(f"/api/sessions/{sid}/memory/export").status_code == 404

    with patch("backend.main.complete", new=_snapshot_llm([])):
        r = client.post(f"/api/sessions/{sid}/memory/rebuild", json={"mode": "rebuild", "batch_size": 10})
        assert r.status_code == 202
        client.portal.call(memory_service.wait_job, sid)
    st = client.get(f"/api/sessions/{sid}/memory").json()
    assert st["job"]["status"] == "done" and st["snapshot"]["exists"] and st["snapshot"]["schema"] == "hms-1"

    exp = client.get(f"/api/sessions/{sid}/memory/export?facts=1")
    assert exp.status_code == 200 and exp.headers["content-type"].startswith("text/markdown")
    assert "attachment" in exp.headers["content-disposition"]
    assert exp.text.startswith("# Мастер-снимок памяти") and f"## [{hm.SEC_CHRONICLE}]" in exp.text

    async def add_fact():
        async with AsyncSessionLocal() as db:
            db.add(models.HoraeFact(session_id=sid, content="факт", source_message_id=1))
            await db.commit()
    client.portal.call(add_fact)
    gone = client.delete(f"/api/sessions/{sid}/memory").json()
    assert gone["snapshot_deleted"] is True and gone["facts_deleted"] >= 1
    st = client.get(f"/api/sessions/{sid}/memory").json()
    assert st["snapshot"]["exists"] is False and st["facts"]["count"] == 0
    assert st["backlog"]["messages_total"] == 80                # сообщения на месте


def test_second_job_is_rejected_with_409(client):
    from backend import memory_service
    cid = client.post("/api/characters", json={"name": "Очередь"}).json()["id"]
    sid = client.post(f"/api/sessions?character_id={cid}").json()["session_id"]
    lock = memory_service.session_lock(sid)

    async def hold():
        await lock.acquire()
    client.portal.call(hold)
    try:
        assert client.post(f"/api/sessions/{sid}/memory/rebuild", json={"mode": "catchup"}).status_code == 202
        assert client.post(f"/api/sessions/{sid}/memory/rebuild", json={"mode": "catchup"}).status_code == 409
        assert client.get(f"/api/sessions/{sid}/memory").json()["job"]["status"] == "queued"
        assert client.post(f"/api/sessions/{sid}/memory/cancel").json()["ok"] is True
    finally:
        async def release():
            lock.release()  # asyncio.Lock отпускаем в его же цикле событий
        client.portal.call(release)
        client.portal.call(memory_service.wait_job, sid)
    assert memory_service.get_job(sid).status == "cancelled"


def test_deleting_chat_forgets_its_job_for_next_chat_with_same_id(client):
    """
    Удаление чата снимает и забывает его задание памяти. SQLite отдаёт id
    удалённого последнего чата новому: иначе новый чат показывал бы во вкладке
    «Память» чужое задание, а пока то в очереди — получал бы 409 на «Пересобрать».
    """
    from backend import memory_service
    cid = client.post("/api/characters", json={"name": "Наследник"}).json()["id"]
    sid = client.post(f"/api/sessions?character_id={cid}").json()["session_id"]
    lock = memory_service.session_lock(sid)

    async def hold():
        await lock.acquire()  # ежеходный проход «идёт» — задание ждёт в очереди
    client.portal.call(hold)
    try:
        assert client.post(f"/api/sessions/{sid}/memory/rebuild", json={"mode": "catchup"}).status_code == 202
        old = memory_service.get_job(sid)
        assert client.delete(f"/api/sessions/{sid}").json()["ok"] is True
        assert old.status == "cancelled" and memory_service.get_job(sid) is None
        assert client.post(f"/api/sessions?character_id={cid}").json()["session_id"] == sid
        assert client.get(f"/api/sessions/{sid}/memory").json()["job"] is None
        assert client.post(f"/api/sessions/{sid}/memory/rebuild", json={"mode": "catchup"}).status_code == 202
    finally:
        async def release():
            lock.release()  # asyncio.Lock отпускаем в его же цикле событий
        client.portal.call(release)
        client.portal.call(memory_service.wait_job, sid)
    assert memory_service.get_job(sid) is not old and memory_service.get_job(sid).status == "done"


def test_memory_endpoints_answer_404_for_missing_chat(client):
    """Нет чата — 404, а не 403: интерфейс отличает удалённый чат от чужого."""
    for method, path in (("get", ""), ("post", "/rebuild"), ("post", "/cancel"),
                         ("delete", ""), ("get", "/export")):
        kw = {"json": {"mode": "catchup"}} if path == "/rebuild" else {}
        r = getattr(client, method)(f"/api/sessions/987654321/memory{path}", **kw)
        assert r.status_code == 404, (method, path, r.status_code)


def test_memory_endpoints_respect_access(client):
    """
    Чужой чат в режиме аккаунтов — 403 на всех эндпоинтах памяти.

    Порядок — как в tests/test_horae_privacy.py: B регистрируется после A и
    потому точно не админ; режим аккаунтов выключается своим admin_password, а
    не правами глобального админа, личность которого в общей БД тесту неизвестна.
    """
    a = client.post("/api/auth/register", json={"username": "hms_a", "password": "pw"}).json()
    b = client.post("/api/auth/register", json={"username": "hms_b", "password": "pw"}).json()
    ha, hb = {"X-User-Token": a["token"]}, {"X-User-Token": b["token"]}
    client.put("/api/admin/security", json={"accounts_enabled": True, "admin_password": "hms_pw"})
    try:
        cid = client.post("/api/characters", json={"name": "Чужой"}, headers=ha).json()["id"]
        sid = client.post(f"/api/sessions?character_id={cid}", headers=ha).json()["session_id"]
        for method, path in (("get", ""), ("post", "/rebuild"), ("post", "/cancel"),
                             ("delete", ""), ("get", "/export")):
            kw = {"json": {"mode": "catchup"}} if path == "/rebuild" else {}
            r = getattr(client, method)(f"/api/sessions/{sid}/memory{path}", headers=hb, **kw)
            assert r.status_code == 403, (method, path, r.status_code)
    finally:
        client.put(
            "/api/admin/security",
            json={"accounts_enabled": False, "admin_password": ""},
            headers={**ha, "X-Admin-Password": "hms_pw"},
        )


# ============================================================================
# Доработки по ревью задач 5–6 (задача 9)
# ============================================================================
async def test_fact_vector_backfill_runs_once_per_chat_at_a_time():
    """
    Досчёт векторов фактов идёт вне замка памяти чата (_busy снят до него).
    Долгий досчёт (смена модели эмбеддингов — до сотен векторов) и следующий
    ход запускали второй досчёт тех же фактов — платные вызовы впустую.
    """
    from backend import main
    from backend.database import engine
    await _fresh_db()
    _, sid, _ = await _make_chat(3)
    calls = []

    async def slow_backfill(db, session_id, connection, *a, **k):
        calls.append(session_id)
        await asyncio.sleep(0.05)
        return 0

    with patch("backend.horae_recall.backfill_all", new=slow_backfill):
        await asyncio.gather(main._backfill_fact_vectors(sid), main._backfill_fact_vectors(sid))
        assert calls == [sid]
        await main._backfill_fact_vectors(sid)  # первый кончился — следующий ход досчитывает снова
    assert calls == [sid, sid] and sid not in main._backfill_running
    await engine.dispose()


@pytest.mark.parametrize("older, merges, upto", [(30, 2, 29), (25, 1, 19)])
async def test_incremental_continues_manual_rebuild_buffer_and_swaps_when_caught_up(
        older, merges, upto):
    """
    Ручная пересборка (v=2 + буфер manual: true), прерванная, например,
    перезапуском сервера. Ежеходный проход продолжает буфер, пока в бэклоге
    что-то есть, а в контекст идёт прежний снимок. Буфер догнал бэклог —
    подмена: последним пакетом (30 старше окна: пакеты по 20 и 10) или без
    вызова модели, когда остаток меньше summary_every (25: пакет 20, остаток 5).
    Проходы по одному пакету (_summary_pass), чтобы видеть и промежуточный буфер.
    """
    from backend import main, models
    from backend.database import AsyncSessionLocal, engine
    await _fresh_db()
    _, sid, ids = await _make_chat(older + hr.DEFAULT_WINDOW)
    async with AsyncSessionLocal() as db:
        db.add(models.HoraeEntry(
            session_id=sid, category="summary", title="t", content="ЖИВОЙ", always_on=True,
            enabled=True, meta={"last_message_id": ids[older - 1], "v": 2, "rebuild": {
                "content": "", "last_message_id": 0, "manual": True}}))
        await db.commit()
    live = []
    seen = []
    fake = _snapshot_llm(seen)

    async def watch_live(messages, params=None, connection=None, kind="service"):
        if messages[0]["content"] == hm.MASTER_STATE_PROMPT:  # факты идут и после подмены
            live.append((await _entry(sid)).content)
        return await fake(messages, params, connection, kind)

    with patch("backend.main.complete", new=watch_live):
        assert await main._summary_pass(sid) is True             # бэклог ещё не догнан
        entry = await _entry(sid)
        assert entry.content == "ЖИВОЙ" and entry.meta["rebuild"]["last_message_id"] == ids[19]
        assert entry.meta["rebuild"]["manual"] is True
        assert await main._summary_pass(sid) is False
    merged = [m[1]["content"] for m in seen if m[0]["content"] == hm.MASTER_STATE_PROMPT]
    assert len(merged) == merges and all(c == "ЖИВОЙ" for c in live)
    assert merged[0].startswith(f"[Текущая память]\n{hm.EMPTY_STATE}")  # с нуля, а не поверх живого
    if merges == 2:
        assert f"- [#{ids[0]}–#{ids[19]}] сжато" in merged[1]          # второй пакет — поверх буфера
    entry = await _entry(sid)
    assert "rebuild" not in entry.meta and entry.meta["last_message_id"] == ids[upto]
    assert entry.meta["updated_by"] == "incremental" and entry.meta["schema"] == hm.SNAPSHOT_SCHEMA
    assert "ЖИВОЙ" not in entry.content and f"## [{hm.SEC_CHRONICLE}]" in entry.content
    await engine.dispose()


async def test_job_error_texts_for_invalid_snapshot_and_source_conflict():
    """Тексты ошибок задания для брака снимка и для куска, менявшегося под моделью."""
    from backend import models
    from backend.database import AsyncSessionLocal, engine
    await _fresh_db()
    _, sid, ids = await _make_chat(30 + hr.DEFAULT_WINDOW)

    async def garbage(messages, params=None, connection=None, kind="service"):
        return "просто пересказ без разделов"

    with patch("backend.main.complete", new=garbage):
        job = await _run_rebuild(sid, mode="catchup")
    assert job.status == "error" and job.error.startswith("модель вернула снимок не по схеме: ")
    assert f"нет раздела [{hm.SEC_LISTS}]" in job.error
    assert await _entry(sid) is None

    edits = []

    async def edit_under_model(messages, params=None, connection=None, kind="service"):
        if messages[0]["content"] == hm.MASTER_STATE_PROMPT:
            edits.append(1)
            async with AsyncSessionLocal() as db:
                (await db.get(models.Message, ids[0])).content = f"правка {len(edits)}"
                await db.commit()
        return await _snapshot_llm([])(messages, params, connection, kind)

    with patch("backend.main.complete", new=edit_under_model):
        job = await _run_rebuild(sid, mode="catchup")
    assert job.status == "error" and job.error == "переписка менялась во время сжатия, повторите"
    assert len(edits) == 4                    # пакет и три пересборки подряд
    assert await _entry(sid) is None
    await engine.dispose()


async def test_purge_during_running_job_cancels_it_and_deletes_memory():
    """
    «Сбросить» посреди пересборки: задание останавливается, начатый пакет
    доводится, сброс ждёт его конца — и только потом стирает снимок и факты.
    Иначе следующий пакет воскресил бы только что стёртую память.
    """
    from backend import main, memory_service, models
    from backend.database import AsyncSessionLocal, engine
    await _fresh_db()
    _, sid, ids = await _make_chat(30 + hr.DEFAULT_WINDOW)
    async with AsyncSessionLocal() as db:
        db.add(models.HoraeFact(session_id=sid, content="ключ у стража", source_message_id=ids[0]))
        await db.commit()
    entered, release = asyncio.Event(), asyncio.Event()
    merges = []

    async def slow(messages, params=None, connection=None, kind="service"):
        if messages[0]["content"] == hm.MASTER_STATE_PROMPT:
            merges.append(1)
            entered.set()
            await release.wait()
        return await _snapshot_llm([])(messages, params, connection, kind)

    async def purge():
        async with AsyncSessionLocal() as db:
            return await memory_service.purge(db, sid)

    with patch("backend.main.complete", new=slow):
        job = await memory_service.start_job(sid, main._memory_deps(), mode="catchup", batch_size=10)
        await entered.wait()
        assert job.status == "running"
        purging = asyncio.create_task(purge())
        for _ in range(20):
            if job.cancel.is_set():
                break
            await asyncio.sleep(0)
        assert job.cancel.is_set() and not purging.done()   # отменил и ждёт конца пакета
        release.set()
        gone = await purging
    assert job.status == "cancelled" and len(merges) == 1  # следующего пакета не было
    assert gone == {"snapshot_deleted": True, "facts_deleted": 1}
    assert await _entry(sid) is None
    async with AsyncSessionLocal() as db:
        assert (await memory_service.status(db, sid))["facts"]["count"] == 0
    await engine.dispose()


async def test_api_error_with_existing_snapshot_and_buffer_keeps_both():
    """
    Ошибка API посреди продолженной пересборки при живом снимке: снимок и его
    указатель не тронуты, буфер стоит на последнем записанном пакете —
    «Продолжить» доделает остаток (§6.5).
    """
    from backend import models
    from backend.database import AsyncSessionLocal, engine
    await _fresh_db()
    _, sid, ids = await _make_chat(30 + hr.DEFAULT_WINDOW)
    async with AsyncSessionLocal() as db:
        db.add(models.HoraeEntry(
            session_id=sid, category="summary", title="t", content="СТАРЫЙ", always_on=True,
            enabled=True, meta={"last_message_id": ids[29], "v": 2, "rebuild": {
                "content": hm.render_snapshot({hm.SEC_CHRONICLE: "- [#a–#b] уже сделано"}),
                "last_message_id": ids[9], "manual": True}}))
        await db.commit()

    class Denied(Exception):
        status_code = 401

    merges = []

    async def fail_second(messages, params=None, connection=None, kind="service"):
        if messages[0]["content"] == hm.MASTER_STATE_PROMPT:
            merges.append(1)
            if len(merges) == 2:
                raise Denied("no key")
        return await _snapshot_llm([])(messages, params, connection, kind)

    with patch("backend.main.complete", new=fail_second):
        job = await _run_rebuild(sid, resume=True, batch_size=10)
    assert job.status == "error" and job.error.startswith("Доступ к модели отклонён")
    entry = await _entry(sid)
    assert entry.content == "СТАРЫЙ" and entry.meta["last_message_id"] == ids[29]
    buffer = entry.meta["rebuild"]
    assert buffer["last_message_id"] == ids[19] and buffer["manual"] is True
    assert f"- [#{ids[10]}–#{ids[19]}] сжато" in buffer["content"]
    await engine.dispose()


async def test_first_unreadable_batch_keeps_the_entry_off_until_text_appears():
    """
    Самый первый пакет чата нечитаем (одни <think>): указатель его проходит,
    но пустая запись не включается — иначе в контекст уходил бы пустой блок
    «Что было в истории». Первый непустой снимок её включает.
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
    entry = await _entry(sid)
    assert seen == [] and entry.meta["last_message_id"] == ids[11]
    assert entry.content == "" and entry.enabled is False and entry.meta["tokens"] == 0

    async with AsyncSessionLocal() as db:  # ещё 12 реплик — из окна вышли настоящие
        db.add_all(models.Message(session_id=sid, role="user", content=f"новое {i}")
                   for i in range(12))
        await db.commit()
    with patch("backend.main.complete", new=_snapshot_llm(seen)):
        await main._maybe_update_summary(sid)
    entry = await _entry(sid)
    assert entry.enabled is True and f"## [{hm.SEC_CHRONICLE}]" in entry.content
    assert entry.meta["last_message_id"] == ids[23]
    await engine.dispose()


def test_adopt_summary_counts_tokens_once_and_empty_weighs_nothing():
    """
    meta.tokens — одна оценка (horae_memory.count_tokens, как у менеджера):
    переданное значение не пересчитывается, пустой снимок весит ноль.
    """
    from types import SimpleNamespace

    from backend import memory_service
    from backend.horae_memory import estimate_tokens
    entry = SimpleNamespace(meta={}, title="", content="", keywords=[], always_on=False,
                            enabled=False, priority=0)
    snap = hm.render_snapshot({hm.SEC_CHRONICLE: "- [#1–#2] ворота открыты"})
    memory_service.adopt_summary(entry, snap, 5)
    assert entry.meta["tokens"] == estimate_tokens(snap) == memory_service.snapshot_tokens(snap)
    with patch("backend.memory_service.count_tokens", side_effect=AssertionError("пересчёт")):
        memory_service.adopt_summary(entry, snap, 6, tokens=7)
    assert entry.meta["tokens"] == 7 and entry.enabled is True
    memory_service.adopt_summary(entry, "  \n", 7)
    assert entry.meta["tokens"] == 0 and entry.enabled is False


def test_export_filename_is_capped():
    """Название чата — до 300 символов; имя файла длиннее 255 браузер обрежет или не сохранит."""
    from backend import memory_service
    title = ("Долгая дорога домой. " * 15)[:300]
    name = memory_service.export_filename(title, 7)
    slug = name[len("memory-"):-len("-7.md")]
    assert name.startswith("memory-Долгая_дорога_домой_") and name.endswith("-7.md")
    assert len(slug) <= 80 and not slug.endswith("_")


async def test_rebuild_with_nothing_older_than_window_keeps_snapshot_and_warns():
    """
    Пересобирать нечего (чат короче окна): прежний снимок остаётся, а задание
    объясняет, почему «готово» ничего не изменило.
    """
    from backend import models
    from backend.database import AsyncSessionLocal, engine
    await _fresh_db()
    _, sid, ids = await _make_chat(10)
    async with AsyncSessionLocal() as db:
        db.add(models.HoraeEntry(session_id=sid, category="summary", title="t",
                                 content="СТАРЫЙ", always_on=True, enabled=True,
                                 meta={"last_message_id": ids[5], "v": 2}))
        await db.commit()
    seen = []
    with patch("backend.main.complete", new=_snapshot_llm(seen)):
        job = await _run_rebuild(sid)
    assert job.status == "done" and seen == []
    assert job.warnings == ["пересборка не нашла сообщений старше окна — прежний снимок оставлен"]
    entry = await _entry(sid)
    assert entry.content == "СТАРЫЙ" and "rebuild" not in entry.meta
    await engine.dispose()


async def test_status_of_legacy_summary_agrees_with_its_backlog():
    """
    Сводка старого формата (без meta.v) пересобирается неявно: бэклог считается
    от нуля. Статус показывает эту же цель — неявный буфер (manual: false,
    указатель 0), иначе «учтено до #N» и «ждут сжатия 40» противоречили бы.
    """
    from backend import memory_service, models
    from backend.database import AsyncSessionLocal, engine
    await _fresh_db()
    _, sid, ids = await _make_chat(40 + hr.DEFAULT_WINDOW)
    async with AsyncSessionLocal() as db:
        db.add(models.HoraeEntry(session_id=sid, category="summary", title="t",
                                 content="Старая свободная сводка.", always_on=True, enabled=True,
                                 meta={"last_message_id": ids[39]}))
        await db.commit()
        st = await memory_service.status(db, sid)
    assert st["snapshot"]["covered_upto"] == ids[39] and st["backlog"]["pending"] == 40
    assert st["staging"] == {"last_message_id": 0, "tokens": 0, "manual": False, "started_at": None,
                             "paused": False}
    await engine.dispose()


async def test_job_is_started_when_it_leaves_the_queue():
    """queued_at — постановка в очередь; started_at — переход в running, а не постановка."""
    from backend import main, memory_service
    from backend.database import engine
    await _fresh_db()
    _, sid, _ = await _make_chat(3)
    lock = memory_service.session_lock(sid)
    await lock.acquire()                      # ежеходный проход «идёт»
    try:
        job = await memory_service.start_job(sid, main._memory_deps(), mode="catchup")
        await asyncio.sleep(0)
        queued = job.to_dict()
        assert queued["status"] == "queued" and queued["queued_at"] and queued["started_at"] is None
    finally:
        lock.release()
    await memory_service.wait_job(sid)
    done = job.to_dict()
    assert done["status"] == "done" and done["started_at"] >= done["queued_at"]
    await engine.dispose()


async def test_free_chat_lock_is_dropped_but_never_while_someone_waits():
    """
    Реестр замков не копит чаты: asyncio.Lock привязывается к циклу событий
    при первом ожидании, а id чатов повторяются (SQLite), и замок из чужого
    цикла дал бы RuntimeError. Но замок, которого кто-то ждёт, из реестра не
    убирается: следующий получил бы новый замок, и два прогона пошли бы разом.
    """
    from backend import main, memory_service
    from backend.database import engine
    await _fresh_db()
    _, sid, _ = await _make_chat(30 + hr.DEFAULT_WINDOW)
    with patch("backend.main.complete", new=_snapshot_llm([])):
        await main._maybe_update_summary(sid)
    assert sid not in memory_service._locks

    held = []

    async def check(messages, params=None, connection=None, kind="service"):
        held.append(memory_service._locks.get(sid))
        return await _snapshot_llm([])(messages, params, connection, kind)

    with patch("backend.main.complete", new=check):
        async with memory_service.chat_lock(sid):
            lock = memory_service._locks[sid]
            job = await memory_service.start_job(sid, main._memory_deps(), mode="rebuild")
            await asyncio.sleep(0)
            assert job.status == "queued"
        assert memory_service._locks.get(sid) is lock  # задание ждёт — замок на месте
        await memory_service.wait_job(sid)
    assert job.status == "done" and held and all(h is lock for h in held)
    assert sid not in memory_service._locks
    await engine.dispose()


# ============================================================================
# Доработки по финальному ревью (задача 11)
# ============================================================================
def _stream_chunk(text, finish_reason=None):
    """Чанк стрима LiteLLM для подмены litellm.acompletion (настоящий шлюз)."""
    class _Delta:
        content = text

    class _Choice:
        delta = _Delta()

    _Choice.finish_reason = finish_reason

    class _Chunk:
        choices = [_Choice()]

    return _Chunk()


async def _no_persist(*a, **k):
    return None


def _ago(minutes):
    from datetime import datetime, timedelta, timezone
    stamp = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    return stamp.isoformat(timespec="seconds").replace("+00:00", "Z")


async def test_truncated_stream_is_length_and_the_turn_pass_backs_off():
    """
    Финальное ревью (I1), сквозь настоящий шлюз: стрим оборван лимитом вывода
    (непустой текст, finish_reason=length). Раньше — корректирующие ходы с
    тем же лимитом, три полноразмерных вызова, отказ, и так на КАЖДОМ ходу.
    Теперь это length: слияние и один повтор (сжимать пока нечего), ошибка
    записана, а следующий ход модель не зовёт, пока не выйдет пауза.
    """
    from backend import main, memory_service
    from backend.database import AsyncSessionLocal, engine
    await _fresh_db()
    _, sid, _ = await _make_chat(12 + hr.DEFAULT_WINDOW)
    requests = []

    async def truncated(**kw):
        requests.append(kw["messages"])

        async def gen():
            yield _stream_chunk(hm.ENVELOPE_OPEN + f"\n## [{hm.SEC_CHRONICLE}]\n- [#1–#2] оборв")
            yield _stream_chunk("ано", "length")
        return gen()

    with patch("backend.llm_gateway.litellm.acompletion", new=truncated), \
            patch("backend.usage_stats._persist", new=_no_persist):
        await main._maybe_update_summary(sid)
        assert len(requests) == 2   # слияние и один повтор — без корректирующих ходов
        assert not any("Ответ отклонён" in str(r[-1]["content"]) for r in requests)
        await main._maybe_update_summary(sid)
        assert len(requests) == 2   # следующий ход ждёт паузу, а не платит снова
    async with AsyncSessionLocal() as db:
        st = await memory_service.status(db, sid)
    err = st["snapshot"]["last_error"]
    assert err["kind"] == "length" and err["failures"] == 1 and "лимит" in err["message"]
    assert st["snapshot"]["retry_after"] > err["at"]
    assert await _entry(sid) is None
    await engine.dispose()


async def test_turn_failure_backs_off_grows_and_clears_on_success():
    """
    Ошибка ежеходного прохода — в meta.last_error; следующий проход ждёт
    min(6 ч, 10 мин · 2^(failures−1)) с момента ошибки. Ручное задание паузу
    не ждёт; удачная запись снимка ошибку убирает.
    """
    from backend import main, memory_service, models
    from backend.database import AsyncSessionLocal, engine
    await _fresh_db()
    _, sid, ids = await _make_chat(40 + hr.DEFAULT_WINDOW)
    async with AsyncSessionLocal() as db:
        db.add(models.HoraeEntry(session_id=sid, category="summary", title="t",
                                 content=hm.render_snapshot({hm.SEC_CHRONICLE: "- [#1–#2] старое"}),
                                 always_on=True, enabled=True,
                                 meta={"last_message_id": ids[9], "v": 2}))
        await db.commit()

    class Denied(Exception):
        status_code = 401

    calls = []

    async def denied(messages, params=None, connection=None, kind="service"):
        calls.append(1)
        raise Denied("no key")

    async def shift_error(minutes):
        async with AsyncSessionLocal() as db:
            entry = (await db.execute(select(models.HoraeEntry).where(
                models.HoraeEntry.session_id == sid))).scalars().first()
            entry.meta = {**entry.meta, "last_error": {**entry.meta["last_error"],
                                                       "at": _ago(minutes)}}
            await db.commit()

    with patch("backend.main.complete", new=denied):
        await main._maybe_update_summary(sid)
        first = (await _entry(sid)).meta["last_error"]
        assert first["kind"] == "auth" and first["failures"] == 1 and len(calls) == 1
        await main._maybe_update_summary(sid)
        assert len(calls) == 1                        # пауза 10 мин ещё идёт
        await shift_error(11)
        await main._maybe_update_summary(sid)
        assert len(calls) == 2 and (await _entry(sid)).meta["last_error"]["failures"] == 2
        await shift_error(11)                         # второй раз пауза уже 20 мин
        await main._maybe_update_summary(sid)
        assert len(calls) == 2
    async with AsyncSessionLocal() as db:
        st = await memory_service.status(db, sid)
    assert st["snapshot"]["last_error"]["failures"] == 2 and st["snapshot"]["retry_after"]

    with patch("backend.main.complete", new=_snapshot_llm([])):
        job = await _run_rebuild(sid, mode="catchup", batch_size=10)   # ручное — без паузы
    assert job.status == "done"
    entry = await _entry(sid)
    assert "last_error" not in entry.meta and entry.meta["last_message_id"] == ids[39]
    async with AsyncSessionLocal() as db:
        st = await memory_service.status(db, sid)
    assert st["snapshot"]["last_error"] is None and st["snapshot"]["retry_after"] is None
    await engine.dispose()


async def test_memory_output_is_clamped_to_the_model_limit():
    """
    Финальное ревью (API I1): max_tokens памяти (1,4 × бюджет + 1024) не
    сверялся с лимитом модели — у gpt-4o (модель по умолчанию) 16 384, запрос
    с 17 824 получал 400 на каждом проходе. Теперь вывод и рабочий бюджет
    снимка прижаты к лимиту, если LiteLLM знает модель памяти.
    """
    import litellm

    from backend import llm_gateway, memory_service
    limit = litellm.get_model_info("gpt-4o")["max_output_tokens"]
    seen = []

    async def spy(messages, params=None, connection=None, kind="service"):
        seen.append(dict(llm_gateway._SAMPLING_OVERRIDES.get() or {}))
        return "ok"

    deps = memory_service.MemoryDeps(complete=spy, get_connection=None)
    for conn, max_tokens in (({"default_model": "gpt-4o"}, limit),
                             ({"default_model": "alias-x", "summary_model": "gpt-4o"}, limit),
                             ({"default_model": "alias-that-litellm-does-not-know"}, 17_824)):
        manager, _ = memory_service.build_manager(conn, {"memory_snapshot_tokens": 12_000}, deps)
        await manager.call([{"role": "user", "content": "x"}])
        assert seen[-1]["max_tokens"] == max_tokens and manager.config.output_tokens == max_tokens
        expected_budget = 12_000 if max_tokens == 17_824 else int((limit - 1024) / 1.4)
        assert manager.config.snapshot_tokens == expected_budget


async def test_status_reports_the_largest_snapshot_budget_the_model_can_write():
    from backend import memory_service
    from backend.database import AsyncSessionLocal, engine
    await _fresh_db()
    _, sid, _ = await _make_chat(3)
    async with AsyncSessionLocal() as db:
        known = await memory_service.status(db, sid, connection={"default_model": "gpt-4o"})
        unknown = await memory_service.status(db, sid, connection={"default_model": "alias-x"})
        plain = await memory_service.status(db, sid)
    max_budget = known["settings"]["max_snapshot_tokens"]
    assert isinstance(max_budget, int) and 1000 < max_budget < known["snapshot"]["budget"]
    assert any(hm._fmt_int(max_budget) in w for w in known["snapshot"]["warnings"])
    assert unknown["settings"]["max_snapshot_tokens"] is None
    assert plain["settings"]["max_snapshot_tokens"] is None
    assert unknown["snapshot"]["warnings"] == [] == plain["snapshot"]["warnings"]
    await engine.dispose()


async def test_rejected_max_tokens_is_retried_once_with_the_default():
    """
    Провайдер отверг max_tokens (400 со словом max_tokens/maxOutputTokens):
    один повтор с DEFAULT_MAX_TOKENS и предупреждение; дальше в прогоне —
    сразу с ним. Другие 400 не повторяются.
    """
    from backend import llm_gateway, memory_service
    from backend.config import settings
    seen = []

    class BadRequestError(Exception):
        status_code = 400

    async def picky(messages, params=None, connection=None, kind="service"):
        wanted = (llm_gateway._SAMPLING_OVERRIDES.get() or {}).get("max_tokens")
        seen.append(wanted)
        if messages[0]["content"] == "другое":
            raise BadRequestError("model not found")
        if wanted != settings.DEFAULT_MAX_TOKENS:
            raise BadRequestError(f"Unable to submit request because it has a maxOutputTokens "
                                  f"value of {wanted} but the supported range is 1 to 8192")
        return "ok"

    deps = memory_service.MemoryDeps(complete=picky, get_connection=None)
    manager, _ = memory_service.build_manager({"default_model": "alias-x"}, {}, deps)
    assert await manager.call([{"role": "user", "content": "x"}]) == "ok"
    assert await manager.call([{"role": "user", "content": "y"}]) == "ok"
    assert seen[0] > settings.DEFAULT_MAX_TOKENS
    assert seen[1:] == [settings.DEFAULT_MAX_TOKENS] * 2
    assert any("max_tokens" in w for w in manager.warnings)
    with pytest.raises(hm.MemoryLLMError) as e:
        await manager.call([{"role": "system", "content": "другое"}])
    assert e.value.kind == "bad_request" and seen[-1] == settings.DEFAULT_MAX_TOKENS


async def test_job_reports_snapshot_tokens_and_why_nothing_was_done():
    """
    Итог задания для тоста: snapshot_tokens — размер принятого снимка, а
    задание без работы объясняет почему (весь чат в окне / всё уже в снимке).
    """
    from backend import memory_service
    from backend.database import AsyncSessionLocal, engine
    await _fresh_db()
    _, sid, _ = await _make_chat(30 + hr.DEFAULT_WINDOW)
    with patch("backend.main.complete", new=_snapshot_llm([])):
        job = await _run_rebuild(sid, mode="catchup", batch_size=10)
        async with AsyncSessionLocal() as db:
            st = await memory_service.status(db, sid)
        assert job.to_dict()["snapshot_tokens"] == st["snapshot"]["tokens"] > 0
        assert job.warnings == []
        again = await _run_rebuild(sid, mode="catchup")
    assert again.status == "done" and again.processed == 0
    assert again.warnings == [memory_service.NOTHING_TO_CATCH_UP_WARNING]

    _, small, _ = await _make_chat(10)                     # весь чат в окне, снимка нет
    with patch("backend.main.complete", new=_snapshot_llm([])):
        job = await _run_rebuild(small)
    assert job.status == "done" and job.processed == 0
    assert job.warnings == [memory_service.NOTHING_TO_COMPRESS_WARNING]
    assert job.to_dict()["snapshot_tokens"] == 0
    await engine.dispose()


async def test_stopped_rebuild_is_not_continued_by_turn_passes():
    """
    Финальное ревью (сервис, M2): после «Остановить» буфер пересборки
    оставался, и ежеходные проходы продолжали его — до шести платных слияний
    за ход, а живой снимок не обновлялся. Теперь остановленный буфер ждёт
    «Продолжить», а ежеходные проходы пишут в живой снимок.
    """
    from backend import main, memory_service, models
    from backend.database import AsyncSessionLocal, engine
    await _fresh_db()
    _, sid, ids = await _make_chat(30 + hr.DEFAULT_WINDOW)
    live = hm.render_snapshot({hm.SEC_CHRONICLE: "- [#0–#9] СТАРЫЙ"})
    async with AsyncSessionLocal() as db:
        db.add(models.HoraeEntry(session_id=sid, category="summary", title="t", content=live,
                                 always_on=True, enabled=True,
                                 meta={"last_message_id": ids[9], "v": 2}))
        await db.commit()

    async def cancel_on_first_merge(messages, params=None, connection=None, kind="service"):
        if messages[0]["content"] == hm.MASTER_STATE_PROMPT:
            memory_service.cancel_job(sid)
        return await _snapshot_llm([])(messages, params, connection, kind)

    with patch("backend.main.complete", new=cancel_on_first_merge):
        job = await _run_rebuild(sid, batch_size=10)
    assert job.status == "cancelled"
    assert (await _entry(sid)).meta["rebuild"]["last_message_id"] == ids[9]

    seen = []
    with patch("backend.main.complete", new=_snapshot_llm(seen)):
        await main._maybe_update_summary(sid)
    merges = [m[1]["content"] for m in seen if m[0]["content"] == hm.MASTER_STATE_PROMPT]
    assert merges and merges[0].startswith("[Текущая память]\n" + live)   # поверх живого
    entry = await _entry(sid)
    assert entry.meta["last_message_id"] == ids[29] and entry.content != live
    buffer = entry.meta["rebuild"]
    assert buffer["last_message_id"] == ids[9] and buffer["paused"] is True   # буфер ждёт
    async with AsyncSessionLocal() as db:
        st = await memory_service.status(db, sid)
    assert st["staging"]["last_message_id"] == ids[9] and st["staging"]["paused"] is True
    assert st["backlog"]["pending"] == 0

    seen.clear()
    with patch("backend.main.complete", new=_snapshot_llm(seen)):
        job = await _run_rebuild(sid, resume=True, batch_size=10)   # «Продолжить»
    assert job.status == "done" and job.processed == 20
    merges = [m[1]["content"] for m in seen if m[0]["content"] == hm.MASTER_STATE_PROMPT]
    assert merges[0].startswith("[Текущая память]\n" + buffer["content"])  # с буфера
    entry = await _entry(sid)
    assert "rebuild" not in entry.meta and entry.meta["updated_by"] == "rebuild"
    await engine.dispose()


async def test_only_one_memory_job_runs_at_a_time():
    """
    Пауза между запросами действует внутри одного прогона: задания разных
    чатов разом множили частоту запросов к общему ключу. Теперь в процессе
    идёт одно задание памяти, остальные ждут в очереди.
    """
    from backend import main, memory_service
    from backend.database import engine
    await _fresh_db()
    _, first, _ = await _make_chat(12 + hr.DEFAULT_WINDOW)
    _, second, _ = await _make_chat(12 + hr.DEFAULT_WINDOW)
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow_first(messages, params=None, connection=None, kind="service"):
        if messages[0]["content"] == hm.MASTER_STATE_PROMPT and not release.is_set():
            entered.set()
            await release.wait()
        return await _snapshot_llm([])(messages, params, connection, kind)

    with patch("backend.main.complete", new=slow_first):
        a = await memory_service.start_job(first, main._memory_deps(), mode="catchup")
        await entered.wait()
        b = await memory_service.start_job(second, main._memory_deps(), mode="catchup")
        for _ in range(20):
            await asyncio.sleep(0)
        assert a.status == "running" and b.status == "queued"
        release.set()
        await memory_service.wait_job(first)
        await memory_service.wait_job(second)
    assert a.status == b.status == "done" and b.started_at >= a.finished_at
    await engine.dispose()


async def test_cancel_stops_a_job_during_the_pause_between_requests():
    """«Остановить» посреди паузы между запросами не ждёт её конца (здесь — минуту)."""
    from backend import main, memory_service
    from backend.database import engine
    await _fresh_db()
    _, sid, _ = await _make_chat(30 + hr.DEFAULT_WINDOW)
    with patch("backend.main.complete", new=_snapshot_llm([])):
        job = await memory_service.start_job(sid, main._memory_deps(), mode="catchup",
                                             batch_size=10, delay_ms=60_000)
        for _ in range(200):
            if job.phase == "wait":
                break
            await asyncio.sleep(0.01)
        assert job.phase == "wait"
        memory_service.cancel_job(sid)
        await asyncio.wait_for(memory_service.wait_job(sid), 5)
    assert job.status == "cancelled" and job.processed == 10
    await engine.dispose()


async def test_purge_stops_a_running_turn_pass_after_its_current_call():
    """
    Сброс памяти посреди ежеходного прохода: раньше сброс ждал, пока проход
    доделает все свои пакеты (до шести платных слияний). Теперь проход
    останавливается после текущего запроса.
    """
    from backend import main, memory_service
    from backend.database import AsyncSessionLocal, engine
    await _fresh_db()
    _, sid, _ = await _make_chat(60 + hr.DEFAULT_WINDOW)
    entered, release = asyncio.Event(), asyncio.Event()
    merges = []

    async def slow(messages, params=None, connection=None, kind="service"):
        if messages[0]["content"] == hm.MASTER_STATE_PROMPT:
            merges.append(1)
            entered.set()
            await release.wait()
        return await _snapshot_llm([])(messages, params, connection, kind)

    async def purge():
        async with AsyncSessionLocal() as db:
            return await memory_service.purge(db, sid)

    with patch("backend.main.complete", new=slow):
        turn = asyncio.create_task(main._maybe_update_summary(sid))
        await entered.wait()
        purging = asyncio.create_task(purge())
        for _ in range(20):
            await asyncio.sleep(0)
        release.set()
        await asyncio.wait_for(purging, 5)
        await turn
    assert len(merges) == 1 and await _entry(sid) is None
    await engine.dispose()


async def test_facts_skipped_by_a_failed_call_are_extracted_with_the_next_batch():
    """
    Финальное ревью (сервис, M1): снимок пакета записан, а ответ модели фактов
    не пришёл (сбой или остановка сервера) — факты этого пакета не
    извлекались уже никогда. Теперь следующий пакет берёт сообщения от
    последнего разобранного фактами, а не только свои.
    """
    from backend import main, models
    from backend.database import AsyncSessionLocal, engine
    await _fresh_db()
    _, sid, ids = await _make_chat(40 + hr.DEFAULT_WINDOW)
    facts_requests = []

    class Boom(Exception):
        status_code = 400

    async def facts_fail_once(messages, params=None, connection=None, kind="service"):
        if messages[0]["content"] == hr.FACTS_PROMPT:
            facts_requests.append(messages[1]["content"])
            if len(facts_requests) == 1:
                raise Boom("facts provider down")
            return "- Эльвира нашла карту\n- Артур ранен"
        return await _snapshot_llm([])(messages, params, connection, kind)

    with patch("backend.main.complete", new=facts_fail_once):
        assert await main._summary_pass(sid) is True
        assert await main._summary_pass(sid) is False
    assert len(facts_requests) == 2
    assert f"[#{ids[0]} · " in facts_requests[1] and f"[#{ids[39]} · " in facts_requests[1]
    async with AsyncSessionLocal() as db:
        count = (await db.execute(select(models.HoraeFact).where(
            models.HoraeFact.session_id == sid))).scalars().all()
    assert count
    await engine.dispose()


async def test_facts_pass_without_facts_is_not_sent_again():
    """
    Удачный разбор фактов без единого факта двигает отметку meta.facts_upto:
    иначе (отметкой служил бы только источник сохранённых фактов) следующий
    пакет слал бы эти сообщения модели фактов ещё раз.
    """
    from backend import main
    from backend.database import engine
    await _fresh_db()
    _, sid, ids = await _make_chat(40 + hr.DEFAULT_WINDOW)
    seen = []
    with patch("backend.main.complete", new=_snapshot_llm(seen)):   # факты — всегда пусто
        await main._summary_pass(sid)
        await main._summary_pass(sid)
    facts = [m[1]["content"] for m in seen if m[0]["content"] == hr.FACTS_PROMPT]
    assert len(facts) == 2 and f"[#{ids[0]} · " not in facts[1]
    assert (await _entry(sid)).meta["facts_upto"] == ids[39]
    await engine.dispose()


async def test_facts_of_a_batch_stopped_before_its_facts_call_come_with_the_next_run():
    """
    «Остановить» посреди слияния: запрос слияния доводится и пакет пишется, а
    запрос фактов после него уже не делается (новых платных запросов после
    отмены нет). Раньше факты такого пакета не извлекались уже никогда — у
    следующего прогона отметка стояла бы на них. Теперь отметка facts_upto не
    двигается, и следующий прогон доизвлекает их вместе со своим пакетом.
    """
    from backend import memory_service
    from backend.database import engine
    await _fresh_db()
    _, sid, ids = await _make_chat(20 + hr.DEFAULT_WINDOW)
    seen = []
    plain = _snapshot_llm(seen)

    async def stop_during_first_merge(messages, params=None, connection=None, kind="service"):
        if messages[0]["content"] == hm.MASTER_STATE_PROMPT and not seen:
            memory_service.cancel_job(sid)
        return await plain(messages, params, connection, kind)

    with patch("backend.main.complete", new=stop_during_first_merge):
        job = await _run_rebuild(sid, mode="catchup", batch_size=10)
    assert job.status == "cancelled" and job.processed == 10
    assert not [m for m in seen if m[0]["content"] == hr.FACTS_PROMPT]
    assert "facts_upto" not in (await _entry(sid)).meta

    seen.clear()
    with patch("backend.main.complete", new=_snapshot_llm(seen)):
        job = await _run_rebuild(sid, mode="catchup", batch_size=10)
    assert job.status == "done" and job.processed == 10
    facts = [m[1]["content"] for m in seen if m[0]["content"] == hr.FACTS_PROMPT]
    assert len(facts) == 1 and f"[#{ids[0]} · " in facts[0] and f"[#{ids[19]} · " in facts[0]
    assert (await _entry(sid)).meta["facts_upto"] == ids[19]
    await engine.dispose()
