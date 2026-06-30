"""
Тесты шлюза LiteLLM.

Реальные вызовы к провайдерам ЗАМОКАНЫ — проверяем именно нашу логику:
сборку мультимодального payload, проброс safety_settings (Zero-Censorship) и стриминг.
Это и есть «тесты с самовосстановлением» из ТЗ: они ловят регрессии без затрат на API.
"""
from unittest.mock import patch

import pytest

from backend.llm_gateway import _merge_params, build_user_content, stream_completion
from backend.schemas import AttachmentIn, GenerationParams


def _fake_chunk(text: str):
    """Имитируем структуру чанка LiteLLM: chunk.choices[0].delta.content."""

    class _Delta:
        content = text

    class _Choice:
        delta = _Delta()

    class _Chunk:
        choices = [_Choice()]

    return _Chunk()


async def _fake_acompletion(*args, **kwargs):
    """Подмена litellm.acompletion: запоминает kwargs и возвращает async-генератор."""
    _fake_acompletion.captured = kwargs

    async def gen():
        for token in ["Прив", "ет", "!"]:
            yield _fake_chunk(token)

    return gen()


@pytest.mark.asyncio
async def test_stream_concatenates_tokens():
    with patch("backend.llm_gateway.litellm.acompletion", new=_fake_acompletion):
        tokens = [
            t
            async for t in stream_completion([{"role": "user", "content": "hi"}])
        ]
    assert "".join(tokens) == "Привет!"


@pytest.mark.asyncio
async def test_disable_safety_adds_block_none():
    with patch("backend.llm_gateway.litellm.acompletion", new=_fake_acompletion):
        params = GenerationParams(disable_safety=True)
        _ = [
            t
            async for t in stream_completion(
                [{"role": "user", "content": "hi"}], params
            )
        ]
    captured = _fake_acompletion.captured
    assert "safety_settings" in captured
    assert all(s["threshold"] == "BLOCK_NONE" for s in captured["safety_settings"])


@pytest.mark.asyncio
async def test_no_safety_when_flag_off():
    with patch("backend.llm_gateway.litellm.acompletion", new=_fake_acompletion):
        _ = [
            t
            async for t in stream_completion(
                [{"role": "user", "content": "hi"}], GenerationParams()
            )
        ]
    assert "safety_settings" not in _fake_acompletion.captured


@pytest.mark.asyncio
async def test_proxy_routing_prefixes_model_and_sets_base_url():
    """С use_proxy=True запрос уходит в LiteLLM-прокси: префикс модели + api_base."""
    conn = {
        "use_proxy": True,
        "base_url": "http://localhost:4000",
        "api_key": "sk-test",
        "default_model": "gpt-4o",
    }
    with patch("backend.llm_gateway.litellm.acompletion", new=_fake_acompletion):
        _ = [
            t
            async for t in stream_completion(
                [{"role": "user", "content": "hi"}], None, conn
            )
        ]
    captured = _fake_acompletion.captured
    assert captured["model"] == "litellm_proxy/gpt-4o"
    assert captured["api_base"] == "http://localhost:4000"
    assert captured["api_key"] == "sk-test"


@pytest.mark.asyncio
async def test_proxy_empty_key_uses_dummy():
    """Если ключ прокси не задан, всё равно передаём заглушку (иначе LiteLLM упадёт)."""
    conn = {"use_proxy": True, "base_url": "http://localhost:4000", "api_key": "", "default_model": "gpt-4o"}
    with patch("backend.llm_gateway.litellm.acompletion", new=_fake_acompletion):
        _ = [t async for t in stream_completion([{"role": "user", "content": "hi"}], None, conn)]
    assert _fake_acompletion.captured["api_key"] == "sk-no-key-required"


@pytest.mark.asyncio
async def test_direct_routing_uses_plain_model():
    """С use_proxy=False LiteLLM маршрутизирует напрямую по имени модели."""
    conn = {"use_proxy": False, "default_model": "gemini/gemini-1.5-pro"}
    with patch("backend.llm_gateway.litellm.acompletion", new=_fake_acompletion):
        _ = [
            t
            async for t in stream_completion(
                [{"role": "user", "content": "hi"}], None, conn
            )
        ]
    captured = _fake_acompletion.captured
    assert captured["model"] == "gemini/gemini-1.5-pro"
    assert "api_base" not in captured


@pytest.mark.asyncio
async def test_ui_model_overrides_connection_default():
    """Модель из UI (params.model) важнее модели по умолчанию из настроек подключения."""
    conn = {"use_proxy": True, "base_url": "http://localhost:4000", "default_model": "gpt-4o"}
    with patch("backend.llm_gateway.litellm.acompletion", new=_fake_acompletion):
        _ = [
            t
            async for t in stream_completion(
                [{"role": "user", "content": "hi"}], GenerationParams(model="claude-3-5-sonnet"), conn
            )
        ]
    assert _fake_acompletion.captured["model"] == "litellm_proxy/claude-3-5-sonnet"


def test_build_user_content_text_only_returns_string():
    # Без вложений — простая строка (дешевле и совместимее).
    assert build_user_content("hello", []) == "hello"


def test_build_user_content_with_image_returns_blocks():
    att = AttachmentIn(type="image", data="data:image/png;base64,AAAA")
    content = build_user_content("look at this", [att])
    assert isinstance(content, list)
    assert content[0]["type"] == "text"
    assert content[1]["type"] == "image_url"


def test_build_user_content_with_audio_strips_data_uri():
    # Аудио для Gemini: base64 без префикса data URI + формат файла.
    att = AttachmentIn(
        type="audio", data="data:audio/ogg;base64,QUJD", mime="audio/ogg"
    )
    content = build_user_content("", [att])
    block = content[0]
    assert block["type"] == "input_audio"
    assert block["input_audio"]["data"] == "QUJD"   # префикс data URI отрезан
    assert block["input_audio"]["format"] == "ogg"


def test_ui_params_override_defaults():
    merged = _merge_params(GenerationParams(temperature=0.1, top_p=0.5))
    assert merged["temperature"] == 0.1
    assert merged["top_p"] == 0.5
    # Незаданные в UI поля берутся из дефолтов .env.
    assert "max_tokens" in merged


async def _fake_image_chat(*args, **kwargs):
    class _Msg:
        images = [{"image_url": {"url": "data:image/png;base64,ZZZ"}}]
        content = ""

    class _Choice:
        message = _Msg()

    class _Resp:
        choices = [_Choice()]

    return _Resp()


@pytest.mark.asyncio
async def test_generate_image_chat_extracts_image():
    """Картинку из ответа chat-модели (nano-banana) достаём корректно."""
    from backend.llm_gateway import generate_image_chat

    conn = {"use_proxy": True, "base_url": "http://localhost:4000", "image_model": "nano-banana"}
    with patch("backend.llm_gateway.litellm.acompletion", new=_fake_image_chat):
        url = await generate_image_chat("нарисуй", ["data:image/png;base64,AAAA"], conn)
    assert url == "data:image/png;base64,ZZZ"
