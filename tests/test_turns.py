"""
Надёжность хода (2.13.0): жизненный цикл генерации, один ход на чат, уведомления.

Корень «завис, ничего не удалить, помогает только перезапуск»: кнопка «Стоп»
шлёт ДВЕ отмены (HTTP и WS). Вторая прилетала, когда ход уже сохранял ответ;
CancelledError пролетал мимо `except Exception`, 'done' не уходил, job навсегда
оставался «идущим», а соединение SQLite с незакоммиченным INSERT запирало базу
на запись до перезапуска. Первый тест воспроизводит именно это и падает на
старом backend/generation.py.

Остальное:
  * ошибка сохранения логируется и приходит клиенту (раньше — `pass`);
  * ошибка модели — ПОСЛЕ сохранения частичного ответа, за ней всегда 'done';
  * тишина модели между чанками и общий потолок хода обрывают ход с ошибкой;
  * поздний подписчик получает статус, текст, пояснения и ошибку;
  * ход чата находится после F5 (active_job, WS resumed), второй ход — busy/409;
  * шина /sse/events и проверки доступа.
Таймауты подменяются через monkeypatch — тесты не ждут реальные минуты.
"""
import asyncio
import json
import logging
import sqlite3
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from starlette.requests import Request

from backend import generation, llm_gateway, models
from backend.config import settings
from backend.database import AsyncSessionLocal, init_db


# ----------------------------------------------------------------- помощники
def _db_path() -> str:
    return settings.DATABASE_URL.split("///")[-1]


def _db_write_free() -> bool:
    """Посторонний писатель может взять запись SQLite за 0.5 с (база не заперта)."""
    con = sqlite3.connect(_db_path(), timeout=0.5)
    try:
        con.execute("BEGIN IMMEDIATE")
        con.rollback()
        return True
    except sqlite3.OperationalError:
        return False
    finally:
        con.close()


async def _new_chat(owner_id=None) -> int:
    await init_db()
    async with AsyncSessionLocal() as db:
        ch = models.Character(name="Ходок")
        db.add(ch)
        await db.flush()
        sess = models.ChatSession(character_id=ch.id, user_key="web:turns", owner_id=owner_id)
        db.add(sess)
        await db.commit()
        return sess.id


async def _assistant_texts(session_id: int) -> list[str]:
    from sqlalchemy import select
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(select(models.Message).where(
            models.Message.session_id == session_id, models.Message.role == "assistant"
        ).order_by(models.Message.id))).scalars().all()
        return [m.content for m in rows]


def _saver(log: list | None = None, delay: float = 0.0, started: asyncio.Event | None = None):
    """
    on_complete, пишущий ответ настоящей транзакцией: INSERT + flush (запись
    SQLite захвачена), пауза delay (окно для отмены), commit. Возвращает id.
    """
    async def cb(session_id, content, model_override=None):
        async with AsyncSessionLocal() as db:
            msg = models.Message(session_id=session_id, role="assistant", content=content,
                                 swipes=[content], active_swipe=0)
            db.add(msg)
            await db.flush()
            if started is not None:
                started.set()
            if delay:
                await asyncio.sleep(delay)
            await db.commit()
            if log is not None:
                log.append(content)
            return msg.id
    return cb


def _fake_stream(tokens=(), then=None, delay=0.0):
    """Подмена stream_completion: токены, затем then ('hang' | исключение | None)."""
    async def fake(messages, params=None, connection=None, on_thought=None, kind="chat", on_notice=None):
        if delay:
            await asyncio.sleep(delay)
        for t in tokens:
            yield t
            await asyncio.sleep(0)
        if then == "hang":
            await asyncio.sleep(3600)
        elif isinstance(then, BaseException):
            raise then
    return fake


async def _drain(q: asyncio.Queue, timeout: float = 5.0) -> list[dict]:
    """События очереди до 'done' включительно (или до таймаута)."""
    out = []
    deadline = time.monotonic() + timeout
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            return out
        try:
            ev = await asyncio.wait_for(q.get(), left)
        except asyncio.TimeoutError:
            return out
        out.append(ev)
        if ev["type"] == "done":
            return out


async def _wait_until(cond, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        await asyncio.sleep(0.01)
    return cond()


# ------------------------------------------------- жизненный цикл (generation.py)
async def test_double_cancel_during_save_finishes_and_releases_db():
    """
    Две отмены (HTTP + WS от одной кнопки «Стоп»), вторая — посреди сохранения.
    Ход обязан закончиться: done=True, событие 'done', ответ в базе, база не заперта.
    На старом коде вторая отмена рвала сохранение: done не наступал никогда.
    """
    sid = await _new_chat()
    gm = generation.GenerationManager()
    saved: list = []
    in_save = asyncio.Event()
    with patch.object(generation, "stream_completion", new=_fake_stream(["Частичный ", "ответ"], then="hang")):
        job = await gm.start("dc1", sid, [], None, on_complete=_saver(saved, delay=0.3, started=in_save))
        q = job.subscribe()
        assert await _wait_until(lambda: job.buffer == "Частичный ответ")
        gm.cancel(job.job_id)                    # первая отмена: Stop
        await asyncio.wait_for(in_save.wait(), 3)
        gm.cancel(job.job_id)                    # вторая — уже в сохранении
        events = await _drain(q, timeout=3)

    assert job.done, "ход так и не закончился (вторая отмена прервала сохранение)"
    assert events and events[-1]["type"] == "done"
    assert saved == ["Частичный ответ"]
    assert "Частичный ответ" in await _assistant_texts(sid)
    assert _db_write_free(), "база заперта на запись незакоммиченной транзакцией"
    assert gm.active() == 0 and gm.active_for(sid) is None
    assert events[-1]["stopped"] is True and events[-1]["message_id"]


async def test_cancel_is_idempotent_and_ignored_while_saving():
    sid = await _new_chat()
    gm = generation.GenerationManager()
    in_save = asyncio.Event()
    with patch.object(generation, "stream_completion", new=_fake_stream(["а"], then="hang")):
        job = await gm.start("idem", sid, [], None, on_complete=_saver(delay=0.2, started=in_save))
        assert await _wait_until(lambda: job.buffer == "а")
        assert gm.cancel(job.job_id) is True
        assert gm.cancel(job.job_id) is False            # повтор — ничего
        await asyncio.wait_for(in_save.wait(), 3)
        assert job.stage == "saving"
        assert gm.cancel(job.job_id) is False            # сохранение не трогаем
        assert await gm.wait_done(job, 3)
    assert gm.cancel(job.job_id) is False                # закончившийся — тоже
    assert gm.cancel("нет-такого") is False


async def test_save_failure_is_logged_and_reported_then_done(caplog):
    sid = await _new_chat()
    gm = generation.GenerationManager()

    async def broken_save(session_id, content, model_override=None):
        raise RuntimeError("database is locked")

    with patch.object(generation, "stream_completion", new=_fake_stream(["Готовый ответ"])), \
            caplog.at_level(logging.ERROR, logger="aichat.generation"):
        job = await gm.start("sf1", sid, [], None, on_complete=broken_save)
        q = job.subscribe()
        events = await _drain(q)
    types = [e["type"] for e in events]
    assert types[-2:] == ["error", "done"]
    assert "Ответ не сохранился" in events[-2]["content"] and "database is locked" in events[-2]["content"]
    assert events[-1]["ok"] is False
    assert any("Ответ не сохранился" in r.getMessage() for r in caplog.records)


async def test_cancelled_save_is_reported_not_silent():
    """Сохранение само упало CancelledError (BaseException) — это тоже ошибка, и 'done' приходит."""
    sid = await _new_chat()
    gm = generation.GenerationManager()

    async def cancelled_save(session_id, content, model_override=None):
        raise asyncio.CancelledError()

    with patch.object(generation, "stream_completion", new=_fake_stream(["x"])):
        job = await gm.start("sf2", sid, [], None, on_complete=cancelled_save)
        events = await _drain(job.subscribe())
    assert [e["type"] for e in events][-2:] == ["error", "done"]
    assert "Ответ не сохранился" in events[-2]["content"]
    assert job.done


async def test_model_error_after_partial_save_then_done():
    """[token…] → сохранение частичного → error → done (клиент на done перечитывает чат)."""
    sid = await _new_chat()
    gm = generation.GenerationManager()
    seq_at_save = {}

    async def save(session_id, content, model_override=None):
        seq_at_save["seq"] = job.seq
        return await _saver()(session_id, content)

    with patch.object(generation, "stream_completion",
                      new=_fake_stream(["Начало ", "реплики"], then=RuntimeError("[Errno 5] обрыв"))):
        job = await gm.start("err1", sid, [], None, on_complete=save)
        q = job.subscribe()
        events = await _drain(q)
    types = [e["type"] for e in events]
    assert "error" in types and types[-1] == "done"
    err = next(e for e in events if e["type"] == "error")
    assert "обрыв" in err["content"]
    assert err["seq"] > seq_at_save["seq"], "ошибка ушла раньше сохранения частичного ответа"
    assert "Начало реплики" in await _assistant_texts(sid)
    # Каждое событие — с job_id и возрастающим seq.
    live = [e for e in events if not e.get("replay")]
    assert all(e["job_id"] == "err1" for e in events)
    assert [e["seq"] for e in live] == sorted(e["seq"] for e in live)


async def test_late_subscriber_gets_status_sticky_error_and_done():
    sid = await _new_chat()
    gm = generation.GenerationManager()

    async def fake(messages, params=None, connection=None, on_thought=None, kind="chat", on_notice=None):
        on_notice("Запрос облегчён")
        on_thought("думаю…")
        yield "Кусок"
        raise RuntimeError("сбой модели")

    with patch.object(generation, "stream_completion", new=fake):
        job = await gm.start("late1", sid, [], None, on_complete=_saver())
        assert await gm.wait_done(job, 3)
    events = await _drain(job.subscribe())
    types = [e["type"] for e in events]
    assert types == ["status", "notice", "thought", "token", "error", "done"]
    assert all(e["job_id"] == "late1" and "seq" in e and e.get("replay") for e in events)
    assert events[0]["stage"] == "done"
    assert events[3]["content"] == "Кусок" and events[4]["content"] == "сбой модели"


async def test_status_pulse_while_waiting_for_first_chunk(monkeypatch):
    monkeypatch.setattr(generation, "STATUS_PULSE", 0.1)
    sid = await _new_chat()
    gm = generation.GenerationManager()
    with patch.object(generation, "stream_completion", new=_fake_stream(["ок"], delay=0.45)):
        job = await gm.start("pulse", sid, [], None, on_complete=_saver())
        events = await _drain(job.subscribe())
    before_token = []
    for e in events:
        if e["type"] == "token":
            break
        before_token.append(e)
    waiting = [e for e in before_token if e["type"] == "status" and e["stage"] == "waiting"]
    assert len(waiting) >= 3, before_token   # повтор при подписке + пульсы
    stages = [e["stage"] for e in events if e["type"] == "status"]
    assert "streaming" in stages and "saving" in stages


async def test_generation_ceiling_stops_endless_thinking(monkeypatch):
    """Модель начала ответ и ушла в бесконечные размышления — потолок хода обрывает её."""
    monkeypatch.setattr(settings, "GENERATION_MAX_SECONDS", 1)
    sid = await _new_chat()
    gm = generation.GenerationManager()

    async def thinker(messages, params=None, connection=None, on_thought=None, kind="chat", on_notice=None):
        yield "Начало"
        while True:
            on_thought("хм… ")
            await asyncio.sleep(0.02)

    t0 = time.monotonic()
    with patch.object(generation, "stream_completion", new=thinker):
        job = await gm.start("ceil", sid, [], None, on_complete=_saver())
        events = await _drain(job.subscribe(), timeout=5)
    assert time.monotonic() - t0 < 4
    assert job.done and job.timed_out
    err = [e for e in events if e["type"] == "error"]
    assert err and "не закончила ответ" in err[0]["content"]
    assert events[-1]["type"] == "done"
    assert "Начало" in await _assistant_texts(sid)


async def test_stream_idle_timeout_cuts_silent_model(monkeypatch):
    """Модель прислала первый чанк и замолчала — ход обрывается за ~STREAM_IDLE_TIMEOUT."""
    monkeypatch.setattr(settings, "STREAM_IDLE_TIMEOUT", 0.3)

    def chunk(text):
        return SimpleNamespace(choices=[SimpleNamespace(
            delta=SimpleNamespace(content=text, reasoning_content=None), finish_reason=None)])

    async def silent(*a, **k):
        async def gen():
            await asyncio.sleep(0.5)        # первый чанк ждём дольше лимита — это можно
            yield chunk("Привет")
            await asyncio.sleep(30)         # а дальше тишина
            yield chunk("никогда")
        return gen()

    got = []
    t0 = time.monotonic()
    with patch("backend.llm_gateway.litellm.acompletion", new=silent):
        with pytest.raises(RuntimeError, match="замолчала"):
            async for tok in llm_gateway.stream_completion([{"role": "user", "content": "hi"}]):
                got.append(tok)
    assert got == ["Привет"]
    assert time.monotonic() - t0 < 3


# --------------------------------------------------------- HTTP / WebSocket
def _chunk(text):
    return SimpleNamespace(choices=[SimpleNamespace(
        delta=SimpleNamespace(content=text, reasoning_content=None), finish_reason=None)])


def _gated_llm(gate: threading.Event):
    """LLM, который пишет «Нача», ждёт gate (из потока теста) и дописывает «ло»."""
    async def fake(*a, **k):
        async def gen():
            yield _chunk("Нача")
            while not gate.is_set():
                await asyncio.sleep(0.02)
            yield _chunk("ло")
        return gen()
    return fake


def _chat(client, name="Турн"):
    cid = client.post("/api/characters", json={"name": name}).json()["id"]
    return client.post(f"/api/sessions?character_id={cid}").json()["session_id"]


def _recv_until(ws, pred, limit=200):
    seen = []
    for _ in range(limit):
        ev = ws.receive_json()
        seen.append(ev)
        if pred(ev):
            return seen
    raise AssertionError(f"не дождались события: {seen[-5:]}")


def test_active_job_resume_and_busy(client):
    sid = _chat(client)
    gate = threading.Event()
    with patch("backend.llm_gateway.litellm.acompletion", new=_gated_llm(gate)):
        with client.websocket_connect(f"/ws/chat/{sid}") as ws:
            ws.send_json({"type": "user_message", "content": "привет", "client_id": "c-1"})
            first = _recv_until(ws, lambda e: e["type"] == "token")
            assert first[0] == {"type": "accepted", "client_id": "c-1"}
            assert first[1]["type"] == "status" and first[1]["stage"] == "preparing"
            job_ev = next(e for e in first if e["type"] == "job")
            job_id = job_ev["job_id"]
            assert job_ev["kind"] == "send" and job_ev["client_id"] == "c-1"

            # Ход виден после F5 / из списка чатов.
            aj = client.get(f"/api/sessions/{sid}/active_job").json()["job"]
            assert aj["job_id"] == job_id and aj["kind"] == "send"
            assert aj["stage"] in ("streaming", "waiting") and aj["chars"] >= 4 and aj["started_at"] > 0
            assert any(j["job_id"] == job_id and j["session_id"] == sid
                       for j in client.get("/api/jobs/active").json()["jobs"])

            # Второе подключение к чату сразу получает идущий ход.
            with client.websocket_connect(f"/ws/chat/{sid}") as ws2:
                ev = ws2.receive_json()
                assert ev == {"type": "job", "job_id": job_id, "kind": "send", "resumed": True}
                replay = _recv_until(ws2, lambda e: e["type"] == "token")
                assert replay[-1]["content"] == "Нача" and replay[-1]["job_id"] == job_id
                # Второй ход в том же чате не запускается: busy и подписка на идущий.
                ws2.send_json({"type": "user_message", "content": "ещё", "client_id": "c-2"})
                busy = _recv_until(ws2, lambda e: e["type"] == "busy")[-1]
                assert busy["job_id"] == job_id and busy["client_id"] == "c-2"
                # retry при идущем ходе — тоже просто подписка.
                ws2.send_json({"type": "retry", "client_id": "c-3"})
                assert _recv_until(ws2, lambda e: e["type"] == "busy")[-1]["job_id"] == job_id

            # HTTP-отправка в занятый чат — 409 с job_id.
            r = client.post(f"/api/sessions/{sid}/send", json={"content": "и ещё"})
            assert r.status_code == 409 and r.json()["job_id"] == job_id

            gate.set()
            rest = _recv_until(ws, lambda e: e["type"] == "done")
    # События самого job (не служебное 'job' сокета) — с seq, по возрастанию, без дублей.
    job_events = [e for e in first + rest
                  if e.get("job_id") == job_id and e["type"] != "job" and not e.get("replay")]
    assert job_events and all("seq" in e for e in job_events)
    seqs = [e["seq"] for e in job_events]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    done = rest[-1]
    assert done["ok"] is True and done["stopped"] is False and done["message_id"]
    assert client.get(f"/api/sessions/{sid}/active_job").json() == {"job": None}
    msgs = client.get(f"/api/sessions/{sid}/messages").json()
    assert [m["role"] for m in msgs] == ["user", "assistant"]       # второй ход не запускался
    assert msgs[-1]["id"] == done["message_id"] and msgs[-1]["content"] == "Начало"


def test_ws_stop_cancels_chat_job_started_elsewhere(client):
    """Stop из другого сокета (после F5) останавливает ход чата; ответ сохраняется частично."""
    sid = _chat(client)
    gate = threading.Event()
    with patch("backend.llm_gateway.litellm.acompletion", new=_gated_llm(gate)):
        r = client.post(f"/api/sessions/{sid}/send", json={"content": "привет"})
        job_id = r.json()["job_id"]
        with client.websocket_connect(f"/ws/chat/{sid}") as ws:
            assert ws.receive_json()["job_id"] == job_id
            _recv_until(ws, lambda e: e["type"] == "token")
            ws.send_json({"type": "stop"})
            done = _recv_until(ws, lambda e: e["type"] == "done")[-1]
        # Повторная отмена по HTTP — без ошибок и без эффекта.
        assert client.post(f"/api/jobs/{job_id}/cancel").json() == {"ok": True, "cancelled": False}
    gate.set()
    assert done["stopped"] is True
    msgs = client.get(f"/api/sessions/{sid}/messages").json()
    assert msgs[-1]["role"] == "assistant" and msgs[-1]["content"] == "Нача"


def test_ws_turn_start_failure_is_error_then_done(client):
    """Сбой старта хода не рвёт сокет: error + done с client_id, дальше сокет жив."""
    sid = _chat(client)
    with client.websocket_connect(f"/ws/chat/{sid}") as ws:
        ws.send_json({"type": "regenerate", "client_id": "r-1"})   # ответа ещё нет → 400
        evs = _recv_until(ws, lambda e: e["type"] == "done")
        err = next(e for e in evs if e["type"] == "error")
        assert err["client_id"] == "r-1" and "Нет ответа" in err["content"]
        assert evs[-1] == {"type": "done", "content": "", "client_id": "r-1"}
        ws.send_json({"type": "ping"})
        assert ws.receive_json() == {"type": "pong"}


def test_sse_job_forwards_past_error_and_replays_to_late_subscriber(client):
    sid = _chat(client)

    async def fail(*a, **k):
        raise RuntimeError("провайдер недоступен")

    with patch("backend.llm_gateway.litellm.acompletion", new=fail):
        job_id = client.post(f"/api/sessions/{sid}/send", json={"content": "привет"}).json()["job_id"]
        events = []
        with client.stream("GET", f"/sse/job/{job_id}") as resp:
            assert resp.status_code == 200
            for line in resp.iter_lines():
                if line.startswith("data: "):
                    events.append(json.loads(line[6:]))
                    if events[-1]["type"] == "done":
                        break
    types = [e["type"] for e in events]
    assert "error" in types and types[-1] == "done"
    assert types.index("error") < types.index("done")
    assert all(e["job_id"] == job_id and "seq" in e for e in events)
    assert "провайдер недоступен" in next(e for e in events if e["type"] == "error")["content"]
    assert client.get("/sse/job/нет-такого").status_code == 404


# -------------------------------------------------------- доступ (аккаунты)
def test_message_edit_delete_and_jobs_need_chat_access(client):
    a = client.post("/api/auth/register", json={"username": "turns_a", "password": "pw"}).json()
    b = client.post("/api/auth/register", json={"username": "turns_b", "password": "pw"}).json()
    ah = {"X-User-Token": a["token"]}
    bh = {"X-User-Token": b["token"]}
    client.put("/api/admin/security", json={"accounts_enabled": True, "admin_password": "turns_pw"})
    try:
        cid = client.post("/api/characters", json={"name": "TurnsA"}, headers=ah).json()["id"]
        sid = client.post(f"/api/sessions?character_id={cid}", headers=ah).json()["session_id"]
        with patch("backend.llm_gateway.litellm.acompletion", new=_gated_llm(threading.Event())):
            job_id = client.post(f"/api/sessions/{sid}/send", json={"content": "секрет"},
                                 headers=ah).json()["job_id"]
            # Чужой не видит ход и не может его остановить или слушать.
            assert client.get(f"/api/sessions/{sid}/active_job", headers=bh).status_code == 403
            assert all(j["job_id"] != job_id
                       for j in client.get("/api/jobs/active", headers=bh).json()["jobs"])
            assert any(j["job_id"] == job_id
                       for j in client.get("/api/jobs/active", headers=ah).json()["jobs"])
            assert client.post(f"/api/jobs/{job_id}/cancel", headers=bh).status_code == 403
            assert client.get(f"/sse/job/{job_id}?token={b['token']}").status_code == 403
            assert client.get(f"/sse/job/{job_id}").status_code == 401
            assert client.get("/sse/events").status_code == 401
            # Владелец останавливает свой ход.
            assert client.post(f"/api/jobs/{job_id}/cancel", headers=ah).json()["cancelled"] is True
            with client.stream("GET", f"/sse/job/{job_id}?token={a['token']}") as resp:
                assert resp.status_code == 200
                for line in resp.iter_lines():
                    if '"done"' in line:
                        break
        msg = client.get(f"/api/sessions/{sid}/messages", headers=ah).json()[0]
        assert client.patch(f"/api/messages/{msg['id']}", json={"content": "взлом"},
                            headers=bh).status_code == 403
        assert client.delete(f"/api/messages/{msg['id']}", headers=bh).status_code == 403
        assert client.get(f"/api/sessions/{sid}/messages", headers=ah).json()[0]["content"] == "секрет"
        assert client.patch(f"/api/messages/{msg['id']}", json={"content": "правка"},
                            headers=ah).status_code == 200
        assert client.delete(f"/api/messages/{msg['id']}", headers=ah).status_code == 200
    finally:
        client.put(
            "/api/admin/security",
            json={"accounts_enabled": False, "admin_password": ""},
            headers={**ah, "X-Admin-Password": "turns_pw"},
        )


def test_deleting_answered_user_message_cancels_turn_without_saving(client):
    sid = _chat(client)
    with patch("backend.llm_gateway.litellm.acompletion", new=_gated_llm(threading.Event())):
        job_id = client.post(f"/api/sessions/{sid}/send", json={"content": "передумал"}).json()["job_id"]
        user_msg = client.get(f"/api/sessions/{sid}/messages").json()[-1]
        assert client.delete(f"/api/messages/{user_msg['id']}").status_code == 200
        assert client.get(f"/api/sessions/{sid}/active_job").json() == {"job": None}
    assert client.get(f"/api/sessions/{sid}/messages").json() == []   # ответ не повис на удалённом
    assert client.post(f"/api/jobs/{job_id}/cancel").json()["cancelled"] is False


def test_deleting_chat_cancels_its_turn(client):
    sid = _chat(client)
    with patch("backend.llm_gateway.litellm.acompletion", new=_gated_llm(threading.Event())):
        job_id = client.post(f"/api/sessions/{sid}/send", json={"content": "пока"}).json()["job_id"]
        assert client.delete(f"/api/sessions/{sid}").status_code == 200
        assert all(j["job_id"] != job_id for j in client.get("/api/jobs/active").json()["jobs"])


async def test_delete_message_takes_horae_lock_before_writing():
    """
    Регресс инверсии замков: удаление ждёт замок Horae чата, НЕ держа запись
    SQLite. Пока замок занят, посторонний писатель свободно берёт запись.
    """
    from backend import horae_engine, main

    sid = await _new_chat()
    async with AsyncSessionLocal() as db:
        db.add(models.HoraeChatState(session_id=sid, data={}))
        msg = models.Message(session_id=sid, role="user", content="удали меня")
        db.add(msg)
        await db.commit()
        mid = msg.id

    lock = horae_engine.chat_lock(sid)
    await lock.acquire()
    try:
        async with AsyncSessionLocal() as db:
            deleting = asyncio.create_task(main.delete_message(mid, user=None, db=db))
            await asyncio.sleep(0.3)
            assert not deleting.done()          # ждёт замок Horae…
            free = await asyncio.to_thread(_db_write_free)
            assert free, "удаление держит запись SQLite, пока ждёт замок Horae"
            lock.release()
            await asyncio.wait_for(deleting, 5)
    finally:
        if lock.locked():
            lock.release()
    async with AsyncSessionLocal() as db:
        assert await db.get(models.Message, mid) is None


# ------------------------------------------------------- шина /sse/events
def _sse_request(query: str = "") -> Request:
    return Request({"type": "http", "method": "GET", "path": "/sse/events",
                    "query_string": query.encode(), "headers": []})


async def _next_data(it, timeout=3.0) -> dict:
    while True:
        chunk = await asyncio.wait_for(it.__anext__(), timeout)
        if chunk.startswith("data: "):
            return json.loads(chunk[6:])


async def test_sse_events_snapshot_and_turn_notifications():
    from backend import main

    sid = await _new_chat()
    gate = asyncio.Event()

    async def gated(messages, params=None, connection=None, on_thought=None, kind="chat", on_notice=None):
        yield "Ответ готов, "
        await gate.wait()
        yield "держи"

    with patch.object(generation, "stream_completion", new=gated):
        running = await generation.generation_manager.start("bus-run", sid, [], None, on_complete=_saver())
        resp = await main.sse_events(_sse_request())
        it = resp.body_iterator
        try:
            snap = await _next_data(it)
            assert snap["type"] == "snapshot"
            assert any(j["job_id"] == "bus-run" and j["session_id"] == sid for j in snap["jobs"])
            gate.set()
            done = await _next_data(it)
            while done.get("job_id") != "bus-run":
                done = await _next_data(it)
            assert done["type"] == "turn_done" and done["ok"] is True and done["stopped"] is False
            assert done["message_id"] and done["preview"] == "Ответ готов, держи"
            assert done["kind"] == "send" and done["session_id"] == sid

            nxt = await generation.generation_manager.start("bus-2", sid, [], None, on_complete=_saver())
            started = await _next_data(it)
            assert started == {"type": "turn_started", "session_id": sid, "job_id": "bus-2", "kind": "send"}
            await generation.generation_manager.wait_done(nxt, 3)
        finally:
            await it.aclose()
    assert running.done


async def test_sse_events_filters_by_chat_access_in_accounts_mode(monkeypatch):
    from backend import accounts, admin_service, main

    me = SimpleNamespace(id=424242, role="user")
    monkeypatch.setattr(admin_service, "security_cache", lambda: {"accounts_enabled": True})

    async def user_from_token(db, token):
        return me if token == "mine" else None
    monkeypatch.setattr(accounts, "user_from_token", user_from_token)

    await init_db()
    async with AsyncSessionLocal() as db:
        db.add(models.User(id=me.id, username="turns_bus_me", password_hash="x", role="user"))
        await db.commit()
    own = await _new_chat(owner_id=me.id)
    foreign = await _new_chat(owner_id=None)

    assert (await main.sse_events(_sse_request("token=bad"))).status_code == 401
    with patch.object(generation, "stream_completion", new=_fake_stream(["ок"])):
        resp = await main.sse_events(_sse_request("token=mine"))
        it = resp.body_iterator
        try:
            assert (await _next_data(it))["type"] == "snapshot"
            j1 = await generation.generation_manager.start("bus-foreign", foreign, [], None, on_complete=_saver())
            await generation.generation_manager.wait_done(j1, 3)
            j2 = await generation.generation_manager.start("bus-own", own, [], None, on_complete=_saver())
            await generation.generation_manager.wait_done(j2, 3)
            seen = []
            while len([e for e in seen if e["type"] == "turn_done"]) < 1:
                seen.append(await _next_data(it))
        finally:
            await it.aclose()
    assert {e["job_id"] for e in seen} == {"bus-own"}
    assert [e["type"] for e in seen] == ["turn_started", "turn_done"]
