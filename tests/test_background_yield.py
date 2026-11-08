"""
2.22.0 «Пользователь — первым»: фоновые запросы к модели (память чата, память о
пользователе, Хроника) не начинаются, пока идёт или готовится ход человека, и
идут по одному; начатый ход останавливает оставшиеся пакеты памяти.
"""
import asyncio

from backend import llm_gateway, memory_service
from backend.config import settings


def _fake_stream(calls: list, gate: asyncio.Event | None = None):
    async def stream(messages, params, connection, kind="chat", **_):
        calls.append(("start", kind))
        if gate is not None:
            await gate.wait()
        yield "ok"
        calls.append(("end", kind))
    return stream


async def test_background_waits_for_preparing_turn(monkeypatch):
    calls: list = []
    monkeypatch.setattr(llm_gateway, "stream_completion", _fake_stream(calls))
    monkeypatch.setattr(settings, "BG_QUIET_SECONDS", 0.0)
    with llm_gateway.turn_preparing():
        task = asyncio.create_task(llm_gateway.complete([], None, {}, kind="summary"))
        await asyncio.sleep(0.8)
        assert calls == [], "фон не должен стартовать, пока ход собирает контекст"
    assert await asyncio.wait_for(task, 5) == "ok"
    assert calls == [("start", "summary"), ("end", "summary")]


async def test_foreground_and_chat_do_not_wait(monkeypatch):
    calls: list = []
    monkeypatch.setattr(llm_gateway, "stream_completion", _fake_stream(calls))
    with llm_gateway.turn_preparing():
        # Запрос самого хода (переписывание запроса памяти) — без очереди.
        with llm_gateway.foreground():
            assert await asyncio.wait_for(
                llm_gateway.complete([], None, {}, kind="horae"), 1) == "ok"
        # Невфоновые виды не ждут вовсе.
        assert await asyncio.wait_for(
            llm_gateway.complete([], None, {}, kind="service"), 1) == "ok"
    assert [c for c in calls if c[0] == "start"] == [("start", "horae"), ("start", "service")]


async def test_background_requests_go_one_at_a_time(monkeypatch):
    calls: list = []
    gate = asyncio.Event()
    monkeypatch.setattr(llm_gateway, "stream_completion", _fake_stream(calls, gate))
    monkeypatch.setattr(settings, "BG_QUIET_SECONDS", 0.0)
    a = asyncio.create_task(llm_gateway.complete([], None, {}, kind="summary"))
    b = asyncio.create_task(llm_gateway.complete([], None, {}, kind="profile"))
    await asyncio.sleep(0.3)
    assert calls == [("start", "summary")]
    assert llm_gateway.background_inflight() == 1
    gate.set()
    assert await asyncio.wait_for(asyncio.gather(a, b), 5) == ["ok", "ok"]
    assert calls == [("start", "summary"), ("end", "summary"), ("start", "profile"), ("end", "profile")]
    assert llm_gateway.background_inflight() == 0


async def test_background_runs_after_max_wait(monkeypatch):
    calls: list = []
    monkeypatch.setattr(llm_gateway, "stream_completion", _fake_stream(calls))
    monkeypatch.setattr(settings, "BG_MAX_WAIT_SECONDS", 0)
    with llm_gateway.turn_preparing():
        # Люди заняты бесконечно — фон всё равно не голодает дольше предела.
        assert await asyncio.wait_for(
            llm_gateway.complete([], None, {}, kind="summary"), 2) == "ok"


async def test_cancel_while_waiting_sends_nothing(monkeypatch):
    """«Стоп»/«Сбросить память», пока фоновый запрос ждёт модель: запроса нет."""
    calls: list = []
    monkeypatch.setattr(llm_gateway, "stream_completion", _fake_stream(calls))
    stop = asyncio.Event()
    with llm_gateway.turn_preparing():
        async def bg():
            with llm_gateway.stop_when(stop.is_set):
                return await llm_gateway.complete([], None, {}, kind="horae")
        task = asyncio.create_task(bg())
        await asyncio.sleep(0.3)
        stop.set()
        try:
            await asyncio.wait_for(task, 3)
            raise AssertionError("ожидался BackgroundCancelled")
        except llm_gateway.BackgroundCancelled:
            pass
    assert calls == []
    assert llm_gateway.background_inflight() == 0
    # Семафор освобождён: следующий фоновый запрос проходит.
    monkeypatch.setattr(settings, "BG_QUIET_SECONDS", 0.0)
    assert await asyncio.wait_for(llm_gateway.complete([], None, {}, kind="summary"), 3) == "ok"


async def test_memory_core_does_not_retry_cancel():
    """MemoryCancelled из колбэка модели — не сбой провайдера: без повторов."""
    from backend import hierarchical_memory as hm

    n = 0

    async def llm(messages):
        nonlocal n
        n += 1
        raise hm.MemoryCancelled()

    mgr = hm.HierarchicalMemoryManager(llm, hm.MemoryConfig(delay_ms=0))
    try:
        await mgr.call([{"role": "user", "content": "x"}])
        raise AssertionError("ожидался MemoryCancelled")
    except hm.MemoryCancelled:
        pass
    assert n == 1


def test_turn_start_stops_memory_passes_softly():
    a, b = asyncio.Event(), asyncio.Event()
    hard = asyncio.Event()
    memory_service._turn_yields[901] = a
    memory_service._turn_yields[902] = b
    memory_service._turn_cancels[901] = hard
    try:
        assert memory_service.yield_to_turns() == 2
        assert a.is_set() and b.is_set()
        # Жёсткая отмена (факты записанного пакета) ход не трогает.
        assert not hard.is_set()
        assert memory_service.yield_to_turns() == 0
    finally:
        memory_service._turn_yields.pop(901, None)
        memory_service._turn_yields.pop(902, None)
        memory_service._turn_cancels.pop(901, None)
