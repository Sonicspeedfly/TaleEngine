"""
Регрессии из ревью 2.13.0: «Стоп» во время передачи хода, удаление чата во время
подготовки хода, «Стоп» сразу после конца реплики персонажа в группе.
"""
import asyncio, threading, time
from types import SimpleNamespace
from unittest.mock import patch
from backend import horae_engine
from backend import main as M
from backend.generation import GenerationManager
from backend.generation import generation_manager


def _chunk(text):
    return SimpleNamespace(choices=[SimpleNamespace(
        delta=SimpleNamespace(content=text, reasoning_content=None), finish_reason=None)])


def _gated_llm(gate):
    async def fake(*a, **k):
        async def gen():
            yield _chunk("A")
            while not gate.is_set():
                await asyncio.sleep(0.02)
            yield _chunk("B")
        return gen()
    return fake


def _chat(client, name="Probe"):
    cid = client.post("/api/characters", json={"name": name}).json()["id"]
    return client.post(f"/api/sessions?character_id={cid}").json()["session_id"]


def _recv_until(ws, pred, limit=300):
    seen = []
    for _ in range(limit):
        ev = ws.receive_json()
        seen.append(ev)
        if pred(ev):
            return seen
    raise AssertionError(seen[-5:])


def test_stop_lost_while_new_turn_waits_for_saving_job(client):
    """Стоп → новая реплика, пока первый ход сохраняется → Стоп: останавливается второй ход."""
    sid = _chat(client)
    gate = threading.Event()
    orig_split = horae_engine.split_reply

    async def slow_split(db, session_id, content):
        await asyncio.sleep(1.5)   # slow save of job1 (e.g. busy DB)
        return await orig_split(db, session_id, content)

    with patch("backend.llm_gateway.litellm.acompletion", new=_gated_llm(gate)), \
         patch("backend.horae_engine.split_reply", new=slow_split):
        with client.websocket_connect(f"/ws/chat/{sid}") as ws:
            ws.send_json({"type": "user_message", "content": "one", "client_id": "c1"})
            ev = _recv_until(ws, lambda e: e["type"] == "token")
            job1 = next(e for e in ev if e["type"] == "job")["job_id"]
            ws.send_json({"type": "stop"})                       # job1 -> saving (slow)
            _recv_until(ws, lambda e: e["type"] == "status" and e.get("stage") == "saving")
            ws.send_json({"type": "user_message", "content": "two", "client_id": "c2"})
            _recv_until(ws, lambda e: e["type"] == "accepted" and e.get("client_id") == "c2")
            ws.send_json({"type": "stop"})                       # user stops the NEW turn
            evs = _recv_until(ws, lambda e: e["type"] == "job" and e.get("client_id") == "c2")
            job2 = evs[-1]["job_id"]
            # job2 should have been stopped; check its state after a moment
            time.sleep(0.5)
            j2 = generation_manager.get(job2)
            gate.set()
            rest = _recv_until(ws, lambda e: e["type"] == "done" and e.get("job_id") == job2)
            assert rest[-1]["stopped"] is True, "«Стоп» во время передачи хода потерялся"



def test_delete_chat_while_turn_preparing(client):
    sid = _chat(client, "Del")
    orig = M.build_context_from_db
    started = threading.Event()

    async def slow_ctx(*a, **k):
        started.set()
        await asyncio.sleep(1.0)
        return await orig(*a, **k)

    gate = threading.Event()
    with patch("backend.llm_gateway.litellm.acompletion", new=_gated_llm(gate)), \
         patch.object(M, "build_context_from_db", new=slow_ctx):
        with client.websocket_connect(f"/ws/chat/{sid}") as ws:
            ws.send_json({"type": "user_message", "content": "orphan?", "client_id": "c1"})
            _recv_until(ws, lambda e: e["type"] == "accepted")
            assert started.wait(5)
            r = client.delete(f"/api/sessions/{sid}")
            evs = _recv_until(ws, lambda e: e["type"] in ("job", "done"))
            # new chat created right after
            sid2 = _chat(client, "New")
            gate.set()
            if evs[-1]["type"] == "job":
                _recv_until(ws, lambda e: e["type"] == "done")
        msgs = client.get(f"/api/sessions/{sid2}/messages").json()
        assert msgs == [], "ход удалённого чата всплыл в новом"



async def test_group_cancel_right_after_stream_finishes_loses_reply():
    m = GenerationManager()
    saved = []

    async def gen(on_thought, on_notice):
        yield "full reply"

    async def runner(job):
        orig_create = asyncio.create_task
        text, exc = await m.stream_part(job, gen_with_hook(job))
        m.set_stage(job, "saving")
        saved.append(text)

    def gen_with_hook(job):
        def make(on_thought, on_notice):
            async def g():
                yield "full reply"
                # stream finishes now; a Stop request lands in the same loop
                # iteration, before the runner resumes from asyncio.wait
                asyncio.get_running_loop().call_soon(m.cancel, job.job_id)
            return g()
        return make

    job = await m.start_runner("g1", 1, runner)
    await asyncio.wait_for(job.finished.wait(), 3)
    assert saved == ["full reply"], "дописанная реплика персонажа не сохранилась"
