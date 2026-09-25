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


async def test_batch_time_honours_offset_and_survives_impossible_one():
    """
    Пояс чата — свободный ввод в настройках. Смещение «+03:00» переводит время
    строки пакета, а невозможное «UTC+25» оставляет его в UTC. Раньше
    timezone() бросал ValueError на смещении от 24 часов: загрузка пакета
    падала на каждом ходу, и память чата переставала обновляться.
    """
    from datetime import datetime

    from backend import main, models
    from backend.database import AsyncSessionLocal, engine
    await _fresh_db()
    for tz, shown in (("+03:00", "14:03"), ("UTC+25", "11:03")):
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
    старый снимок работает дальше, статус показывает прерванную пересборку и
    бэклог от её указателя. «Продолжить» доделывает остаток и подменяет снимок.
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
    assert st["backlog"]["pending"] == 20 and st["snapshot"]["covered_upto"] == ids[29]
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
