"""
«Сколько ждать ответа модели» (2.29.1): думающая модель молчит дольше двух
минут — по умолчанию (0) ход её ждёт, а не падает на REQUEST_TIMEOUT=120 из .env.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from backend import llm_gateway
from backend.config import settings


def test_wait_semantics(monkeypatch):
    monkeypatch.setattr(settings, "REQUEST_TIMEOUT", 120)
    monkeypatch.setattr(settings, "STREAM_IDLE_TIMEOUT", 120)
    monkeypatch.setattr(settings, "GENERATION_MAX_SECONDS", 1800)
    msgs = [{"role": "user", "content": "привет"}]
    # Нет настройки (служебный вызов без подключения) — как раньше, из .env.
    assert llm_gateway.first_timeout(msgs, None) == 120
    assert llm_gateway.idle_limit(None) == 120
    # 0 — без своего ограничения: до потолка хода, тишина не обрывает.
    assert llm_gateway.first_timeout(msgs, {"model_wait": 0}) == 1800
    assert llm_gateway.idle_limit({"model_wait": 0}) == 0
    monkeypatch.setattr(settings, "GENERATION_MAX_SECONDS", 0)
    assert llm_gateway.first_timeout(msgs, {"model_wait": 0}) == 3600
    # Число — столько и ждём (но большой файл всё равно получает своё).
    assert llm_gateway.first_timeout(msgs, {"model_wait": 300}) == 300
    assert llm_gateway.idle_limit({"model_wait": 300}) == 300
    assert llm_gateway.first_timeout(msgs, {"model_wait": 30}) == 120
    assert llm_gateway.model_wait({"model_wait": "мусор"}) == 0


@pytest.mark.asyncio
async def test_slow_thinking_model_is_not_cut(monkeypatch):
    """Модель молчит дольше STREAM_IDLE_TIMEOUT посреди ответа — при 0 ход её ждёт."""
    monkeypatch.setattr(settings, "STREAM_IDLE_TIMEOUT", 0.2)
    captured = {}

    def chunk(text):
        return SimpleNamespace(choices=[SimpleNamespace(
            delta=SimpleNamespace(content=text, reasoning_content=None), finish_reason=None)])

    async def slow(*a, **k):
        captured.update(k)

        async def gen():
            yield chunk("Думаю")
            await asyncio.sleep(0.6)      # дольше «старого» лимита тишины
            yield chunk("… ответ")
        return gen()

    conn = {"use_proxy": True, "base_url": "http://x", "api_key": "", "default_model": "m", "model_wait": 0}
    with patch("backend.llm_gateway.litellm.acompletion", new=slow):
        got = [t async for t in llm_gateway.stream_completion([{"role": "user", "content": "hi"}], None, conn)]
    assert "".join(got) == "Думаю… ответ"
    assert captured["timeout"] >= 1800 or captured["timeout"] == 3600


def test_connection_api_roundtrip(client):
    before = client.get("/api/settings/connection").json()
    assert before["model_wait"] == 0
    r = client.put("/api/settings/connection", json={**before, "model_wait": 600}).json()
    assert r["model_wait"] == 600
    assert client.put("/api/settings/connection", json={**before, "model_wait": -5}).status_code == 422
    client.put("/api/settings/connection", json={**before, "model_wait": 0})


@pytest.mark.asyncio
async def test_timeout_error_says_where_to_change():
    import litellm

    async def boom(*a, **k):
        raise litellm.Timeout("Request timed out.", model="m", llm_provider="openai")

    conn = {"use_proxy": True, "base_url": "http://x", "api_key": "", "default_model": "m", "model_wait": 45}
    with patch("backend.llm_gateway.litellm.acompletion", new=boom):
        with pytest.raises(RuntimeError) as ei:
            _ = [t async for t in llm_gateway.stream_completion([{"role": "user", "content": "hi"}], None, conn)]
    text = str(ei.value)
    assert "Сколько ждать ответа модели" in text and "Request timed out" in text
