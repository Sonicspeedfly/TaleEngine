"""
Облегчённый повтор после 429 «Resource exhausted» (2.29.4): тяжёлый ход Google
не принял — пауза и один повтор без старой части окна и без файлов истории.
"""
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from backend import llm_gateway

BIG = "слово " * 20000          # ~20 тыс. токенов на сообщение


def _req():
    return [
        {"role": "system", "content": "Ты — Джеми."},
        {"role": "user", "content": [{"type": "text", "text": "аватар"}]},
        {"role": "system", "content": "Снимок памяти."},
        {"role": "assistant", "content": "старт " + BIG},
        {"role": "user", "content": "u1 " + BIG},
        {"role": "assistant", "content": "a1 " + BIG},
        {"role": "user", "content": "u2"},
        {"role": "assistant", "content": "a2 " + BIG},
        {"role": "user", "content": "u3"},
        {"role": "assistant", "content": "a3"},
        {"role": "system", "content": "Хвост: дневник и время."},
        {"role": "user", "content": "текущая реплика"},
    ]


def test_lighten_history_keeps_head_tail_and_recent():
    msgs = _req()
    light, cut = llm_gateway.lighten_history(msgs, target_tokens=30_000)
    assert cut > 0
    roles = [m["role"] for m in light]
    # Начало (системный промпт, аватар, снимок), хвост и реплика — на месте.
    assert light[:3] == msgs[:3] and light[-2:] == msgs[-2:]
    # История после обрезки начинается с реплики человека и держит последние 4.
    hist = light[3:-2]
    assert hist[0]["role"] == "user" and len(hist) >= 4
    assert hist[-1]["content"] == "a3"
    assert "старт" not in str(light)
    assert roles.count("system") == 3


def test_lighten_history_nothing_to_cut():
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "привет"}]
    assert llm_gateway.lighten_history(msgs, 10) == (msgs, 0)


def _chunk(text):
    return SimpleNamespace(choices=[SimpleNamespace(
        delta=SimpleNamespace(content=text, reasoning_content=None), finish_reason=None)])


@pytest.mark.asyncio
async def test_429_on_heavy_request_retries_lighter(monkeypatch):
    monkeypatch.setattr(llm_gateway, "LIGHT_RETRY_PAUSE", 0)
    calls = []

    async def fake(*a, **k):
        calls.append(k["messages"])
        if len(calls) == 1:
            raise Exception('litellm.RateLimitError: RateLimitError: Litellm_proxyException - '
                            '{"error": {"code": 429, "message": "Resource exhausted. Please try again later.", '
                            '"status": "RESOURCE_EXHAUSTED"}}')

        async def gen():
            yield _chunk("Ответ")
        return gen()

    notes = []
    with patch("backend.llm_gateway.litellm.acompletion", new=fake):
        got = [t async for t in llm_gateway.stream_completion(_req(), on_notice=notes.append)]
    assert got == ["Ответ"] and len(calls) == 2
    first = sum(len(str(m.get("content"))) for m in calls[0])
    second = sum(len(str(m.get("content"))) for m in calls[1])
    assert second < first * 0.7
    assert "текущая реплика" in str(calls[1][-1]["content"])
    assert notes and "Resource exhausted" in notes[0] and "облегчённым" in notes[0]


@pytest.mark.asyncio
async def test_429_on_light_request_is_not_retried(monkeypatch):
    monkeypatch.setattr(llm_gateway, "LIGHT_RETRY_PAUSE", 0)
    calls = []

    async def fake(*a, **k):
        calls.append(1)
        raise Exception("RateLimitError: Resource exhausted. Please try again later.")

    with patch("backend.llm_gateway.litellm.acompletion", new=fake):
        with pytest.raises(Exception, match="Resource exhausted"):
            _ = [t async for t in llm_gateway.stream_completion([{"role": "user", "content": "привет"}])]
    assert len(calls) == 1
