"""
Память и Хроника вместе: историю чата сжимает ровно один механизм —
мастер-снимок или свёртки Хроники (horae_engine.compression_engine), — а
блоки хода не повторяют друг друга.
"""
from unittest.mock import patch

from backend import horae_engine as he
from backend import horae_recall as hr
from backend import horae_state as hs


def test_compression_engine_picks_exactly_one():
    on = {"enabled": True, "summary_enabled": True}
    assert he.compression_engine(on, {}) == "horae"
    assert he.compression_engine(on, {"auto_summary": False}) == "horae"
    # Хроника выключена в чате — её свёртки не в счёт.
    assert he.compression_engine({"enabled": False, "summary_enabled": True}, {}) == "snapshot"
    assert he.compression_engine({"enabled": True}, {}) == "snapshot"
    assert he.compression_engine({"enabled": True}, {"auto_summary": False}) == "off"
    assert he.compression_engine(None, None) == "snapshot"


def _state_with_events(events):
    state = hs.empty_state()
    state["events"] = [
        {"mid": mid, "i": 0, "level": level, "text": text, "date": "", "time": ""}
        for mid, level, text in events
    ]
    return state


def test_timeline_leaves_snapshot_covered_events_to_the_snapshot():
    state = _state_with_events([
        (2, "normal", "купили хлеб"),
        (4, "important", "нашли карту"),
        (6, "critical", "погиб наставник"),
        (12, "normal", "вышли к реке"),
        (14, "important", "встретили стражу"),
    ])
    out = hs.render_timeline(state, [], {"context_depth": 15}, snapshot_upto=10)
    assert "купили хлеб" not in out and "нашли карту" not in out
    assert "погиб наставник" in out            # ключевой поворот — точка отсчёта времени
    assert "вышли к реке" in out and "встретили стражу" in out
    assert "[До #10] Остальные события — в мастер-снимке выше." in out
    # Без снимка лента прежняя.
    full = hs.render_timeline(state, [], {"context_depth": 15})
    assert "купили хлеб" in full and "мастер-снимке" not in full


def test_timeline_drops_summaries_the_snapshot_already_holds():
    state = _state_with_events([(2, "normal", "купили хлеб"), (12, "normal", "вышли к реке")])
    summaries = [
        {"id": "s_1", "kind": "auto", "range": [1, 4], "text": "Пришли в город.", "active": True},
        {"id": "s_2", "kind": "carry", "range": [0, 0], "text": "Прошлый чат: война.", "active": True},
        {"id": "s_3", "kind": "manual", "range": [11, 12], "text": "У реки.", "active": True},
    ]
    out = hs.render_timeline(state, summaries, {"context_depth": 15}, snapshot_upto=10)
    assert "Пришли в город." not in out and "купили хлеб" not in out
    assert "Прошлый чат: война." in out       # «Ранее» — из другого чата, снимок его не знает
    assert "У реки." in out                   # свёртка после снимка остаётся
    assert "в мастер-снимке выше" in out


# ==================== Через БД ====================

async def _fresh_db():
    from backend.database import engine, init_db

    await engine.dispose()
    await init_db()


async def _make_chat(n: int) -> tuple[int, int, list[int]]:
    """Чат из n реплик; у ответов модели — мета Хроники с событием и местом."""
    from backend import models
    from backend.database import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        ch = models.Character(name="Эльвира")
        db.add(ch)
        await db.flush()
        sess = models.ChatSession(character_id=ch.id, user_key="test:compression")
        db.add(sess)
        await db.flush()
        msgs = []
        for i in range(n):
            m = models.Message(session_id=sess.id, role="user" if i % 2 == 0 else "assistant",
                               content=f"реплика {i}")
            if i % 2:
                meta = hs.empty_meta()
                meta["scene"]["location"] = "Таверна"
                meta["events"] = [{"level": "critical" if i == 3 else "normal",
                                   "text": f"событие {i}"}]
                m.horae = {"metas": [meta], "side": False}
            db.add(m)
            msgs.append(m)
        await db.commit()
        return ch.id, sess.id, [m.id for m in msgs]


async def _set_snapshot(sid: int, last_id: int) -> None:
    from backend import models
    from backend.database import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        db.add(models.HoraeEntry(
            session_id=sid, category="summary", title="Память чата (авто)",
            content="Герои пришли в Дольн.", always_on=True, enabled=True,
            meta={"last_message_id": last_id, "v": 2},
        ))
        await db.commit()


async def _chronicle_compresses(sid: int, lo: int, hi: int) -> None:
    """Свёртки Хроники включены на уровне чата, одна свёртка покрывает [lo, hi]."""
    from backend.database import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        data = await he.load_chat_data(db, sid)
        data["settings"] = {"summary_enabled": True}
        data["summaries"] = he.place_summary(data.get("summaries") or [], he._new_summary(
            data, lo=lo, hi=hi, text="Свёртка: герои собрались в путь.", kind="auto",
            depth=0, children=None, state=None))
        await he.save_chat_data(db, sid, data)
        await db.commit()


async def _build(sid, char_id):
    from backend import models
    from backend.database import AsyncSessionLocal
    from backend.horae_memory import build_context_from_db

    report: dict = {}
    async with AsyncSessionLocal() as db:
        sess = await db.get(models.ChatSession, sid)
        character = await db.get(models.Character, char_id)
        messages = await build_context_from_db(db, sess, character, "дальше", None, 200_000,
                                               report=report)
    return "\n".join(str(m["content"]) for m in messages), report


async def test_snapshot_chat_gets_history_once_and_state_wins():
    from backend.database import engine

    await _fresh_db()
    char_id, sid, ids = await _make_chat(40 + hr.DEFAULT_WINDOW)
    await _set_snapshot(sid, ids[39])

    text, report = await _build(sid, char_id)
    assert report["memory"]["engine"] == "snapshot"
    assert report["memory"]["dropped"] == 40
    # Снимок — история, блок состояния — «сейчас», и модель знает, кто главнее.
    assert "Герои пришли в Дольн." in text
    assert "[ИСТОРИЯ ЧАТА — мастер-снимок]" in text and "при расхождении верь ему" in text
    assert "Снимок текущего состояния" in text
    # События из пересказанной снимком части в ленту не идут — кроме ключевых.
    assert "событие 5" not in text
    assert "событие 3" in text
    assert f"[До #{ids[39]}] Остальные события — в мастер-снимке выше." in text
    assert f"событие {40 + hr.DEFAULT_WINDOW - 1}" in text
    await engine.dispose()


async def test_chronicle_summaries_replace_the_snapshot_in_a_turn():
    from backend.database import engine

    await _fresh_db()
    char_id, sid, ids = await _make_chat(40 + hr.DEFAULT_WINDOW)
    await _set_snapshot(sid, ids[39])
    await _chronicle_compresses(sid, ids[0], ids[19])

    text, report = await _build(sid, char_id)
    mem = report["memory"]
    assert mem["engine"] == "horae"
    # Окно выбрасывает только покрытое свёрткой Хроники, а не снимком.
    assert mem["covered_upto"] == ids[19] and mem["dropped"] == 20
    assert "реплика 20" in text and "реплика 19" not in text
    # Снимок — та же история второй раз и уже устаревшая: в ход не идёт.
    assert "Герои пришли в Дольн." not in text and "мастер-снимок" not in text
    assert "Свёртка: герои собрались в путь." in text
    await engine.dispose()


async def test_snapshot_stands_still_while_chronicle_compresses():
    from backend import main, memory_service
    from backend.database import AsyncSessionLocal, engine

    await _fresh_db()
    _, sid, _ = await _make_chat(12 + hr.DEFAULT_WINDOW)
    await _chronicle_compresses(sid, 0, 0)
    called = []

    async def spy(*a, **kw):  # noqa: ANN002, ANN003
        called.append(1)
        return ""

    with patch("backend.main.complete", new=spy):
        await main._maybe_update_summary(sid)
    assert called == []
    async with AsyncSessionLocal() as db:
        status = await memory_service.status(db, sid)
    assert status["compression"] == {"engine": "horae", "summary_layer": "chat"}
    assert status["snapshot"]["exists"] is False
    await engine.dispose()


async def test_state_api_reports_who_compresses(client):
    from backend import models
    from backend.database import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        ch = models.Character(name="Кай")
        db.add(ch)
        await db.flush()
        sess = models.ChatSession(character_id=ch.id, user_key="test:compression-api")
        db.add(sess)
        await db.commit()
        sid = sess.id

    r = client.get(f"/api/sessions/{sid}/horae/state")
    assert r.status_code == 200
    assert r.json()["compression"] == {"engine": "snapshot", "summary_layer": "default"}

    r = client.put(f"/api/sessions/{sid}/horae/settings", json={"summary_enabled": True})
    assert r.status_code == 200
    assert client.get(f"/api/sessions/{sid}/horae/state").json()["compression"] == {
        "engine": "horae", "summary_layer": "chat"}
    assert client.get(f"/api/sessions/{sid}/memory").json()["compression"]["engine"] == "horae"
