"""
Тесты запасной модели (fallback) в менеджере генерации.

stream_completion замокан: «основная» модель падает, «запасная» отвечает.
Проверяем: событие 'fallback', сброс буфера, запись фактической модели в
on_complete и поведение при выключенном авто-режиме / двойном сбое.
"""
import asyncio
from unittest.mock import patch

import pytest

from backend.generation import GenerationManager
from backend.schemas import GenerationParams


def _fake_stream(failing_models):
    """Мок stream_completion: модели из failing_models падают, остальные отвечают."""
    calls = []

    async def fake(messages, params=None, connection=None):
        model = (params.model if params and params.model else None) or (
            (connection or {}).get("default_model") or "gpt-4o"
        )
        calls.append(model)
        if model in failing_models:
            raise RuntimeError("провайдер недоступен")
        for token in ("Прив", "ет"):
            yield token

    fake.calls = calls
    return fake


def _collect(queue):
    """Выгребает уже лежащие в очереди события (без ожидания)."""
    events = []
    while not queue.empty():
        events.append(queue.get_nowait())
    return events


@pytest.mark.asyncio
async def test_fallback_takes_over_and_records_model():
    conn = {"default_model": "main-model", "fallback_model": "backup-model", "auto_fallback": True}
    fake = _fake_stream({"main-model"})
    saved = {}

    async def on_complete(session_id, content, model_override=None):
        saved.update(session_id=session_id, content=content, model=model_override)

    gm = GenerationManager()
    with patch("backend.generation.stream_completion", new=fake):
        job = await gm.start("j1", 1, [{"role": "user", "content": "hi"}], None, on_complete, conn)
        queue = job.subscribe()
        await job.task
    events = _collect(queue)
    types = [e["type"] for e in events]
    assert "fallback" in types            # клиенту сказали сбросить live-текст
    assert types[-1] == "done"
    assert "error" not in types           # ошибки нет — запасная ответила
    assert job.buffer == "Привет"
    assert fake.calls == ["main-model", "backup-model"]
    assert saved["model"] == "backup-model"  # в model_used попадёт фактическая модель


@pytest.mark.asyncio
async def test_no_fallback_when_auto_disabled():
    conn = {"default_model": "main-model", "fallback_model": "backup-model", "auto_fallback": False}
    fake = _fake_stream({"main-model"})
    gm = GenerationManager()
    with patch("backend.generation.stream_completion", new=fake):
        job = await gm.start("j2", 1, [{"role": "user", "content": "hi"}], None, None, conn)
        queue = job.subscribe()
        await job.task
    types = [e["type"] for e in _collect(queue)]
    assert "error" in types and "fallback" not in types
    assert fake.calls == ["main-model"]   # запасную не трогали


@pytest.mark.asyncio
async def test_error_mentions_both_models_when_fallback_fails_too():
    conn = {"default_model": "main-model", "fallback_model": "backup-model", "auto_fallback": True}
    fake = _fake_stream({"main-model", "backup-model"})
    gm = GenerationManager()
    with patch("backend.generation.stream_completion", new=fake):
        job = await gm.start("j3", 1, [{"role": "user", "content": "hi"}], None, None, conn)
        await job.task
    assert job.error and "Запасная" in job.error
    assert fake.calls == ["main-model", "backup-model"]


@pytest.mark.asyncio
async def test_no_fallback_when_it_equals_primary():
    """Запасная = основной: повторять той же моделью бессмысленно — сразу ошибка."""
    conn = {"default_model": "same", "fallback_model": "same", "auto_fallback": True}
    fake = _fake_stream({"same"})
    gm = GenerationManager()
    with patch("backend.generation.stream_completion", new=fake):
        job = await gm.start("j4", 1, [{"role": "user", "content": "hi"}], GenerationParams(), None, conn)
        await job.task
    assert fake.calls == ["same"]
    assert job.error
