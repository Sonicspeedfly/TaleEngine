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
async def test_safety_off_uses_threshold_off():
    """Zero-Censorship: порог OFF (самый пермиссивный) на все категории."""
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
    assert all(s["threshold"] == "OFF" for s in captured["safety_settings"])


@pytest.mark.asyncio
async def test_safety_off_by_default_and_for_service_calls():
    """
    Полная свобода по умолчанию: disable_safety=True в схеме → фильтры сняты даже
    без явного флага. Служебные вызовы (params=None, напр. режиссёр) — тоже без фильтров.
    """
    with patch("backend.llm_gateway.litellm.acompletion", new=_fake_acompletion):
        _ = [t async for t in stream_completion([{"role": "user", "content": "hi"}], GenerationParams())]
    assert "safety_settings" in _fake_acompletion.captured  # дефолт = свобода

    with patch("backend.llm_gateway.litellm.acompletion", new=_fake_acompletion):
        _ = [t async for t in stream_completion([{"role": "user", "content": "hi"}], None)]
    assert "safety_settings" in _fake_acompletion.captured  # служебный вызов тоже


@pytest.mark.asyncio
async def test_safety_kept_when_user_opts_in():
    """Если пользователь ЯВНО вернул фильтры (disable_safety=False) — их не снимаем."""
    with patch("backend.llm_gateway.litellm.acompletion", new=_fake_acompletion):
        _ = [
            t
            async for t in stream_completion(
                [{"role": "user", "content": "hi"}], GenerationParams(disable_safety=False)
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


@pytest.mark.asyncio
async def test_context_tokens_is_local_param_not_sent_to_provider():
    """context_tokens управляет обрезкой истории у НАС — провайдеру не уходит."""
    with patch("backend.llm_gateway.litellm.acompletion", new=_fake_acompletion):
        _ = [t async for t in stream_completion(
            [{"role": "user", "content": "hi"}], GenerationParams(context_tokens=64000)
        )]
    assert "context_tokens" not in _fake_acompletion.captured


# ----- Рассуждения (thinking / reasoning_effort) -----
@pytest.mark.asyncio
async def test_reasoning_effort_passed_when_set():
    """Явный выбор пользователя уходит в LiteLLM как reasoning_effort."""
    with patch("backend.llm_gateway.litellm.acompletion", new=_fake_acompletion):
        _ = [t async for t in stream_completion(
            [{"role": "user", "content": "hi"}], GenerationParams(reasoning_effort="high")
        )]
    assert _fake_acompletion.captured["reasoning_effort"] == "high"

    with patch("backend.llm_gateway.litellm.acompletion", new=_fake_acompletion):
        _ = [t async for t in stream_completion(
            [{"role": "user", "content": "hi"}], GenerationParams(reasoning_effort="disable")
        )]
    assert _fake_acompletion.captured["reasoning_effort"] == "disable"


@pytest.mark.asyncio
async def test_reasoning_auto_boosts_for_files():
    """Файл в запросе + режим «авто» -> форсируем medium (Gemini не думает над файлами сам)."""
    att = AttachmentIn(type="video", data="data:video/mp4;base64,AAAA", mime="video/mp4")
    content = build_user_content("что на видео?", [att])
    with patch("backend.llm_gateway.litellm.acompletion", new=_fake_acompletion):
        _ = [t async for t in stream_completion(
            [{"role": "user", "content": content}], GenerationParams()
        )]
    assert _fake_acompletion.captured["reasoning_effort"] == "medium"


@pytest.mark.asyncio
async def test_reasoning_not_forced_without_files_or_when_off():
    # Текст без файлов: авто -> параметр не передаём (решает провайдер).
    with patch("backend.llm_gateway.litellm.acompletion", new=_fake_acompletion):
        _ = [t async for t in stream_completion([{"role": "user", "content": "hi"}], GenerationParams())]
    assert "reasoning_effort" not in _fake_acompletion.captured

    # Файл есть, но галка file_reasoning снята -> тоже не передаём.
    att = AttachmentIn(type="image", data="data:image/png;base64,AAAA")
    content = build_user_content("", [att])
    with patch("backend.llm_gateway.litellm.acompletion", new=_fake_acompletion):
        _ = [t async for t in stream_completion(
            [{"role": "user", "content": content}], GenerationParams(file_reasoning=False)
        )]
    assert "reasoning_effort" not in _fake_acompletion.captured


def _thinking_chunk(thought=None, text=None):
    """Чанк с reasoning_content (мысли) и/или content (ответ)."""
    class _Delta:
        pass

    d = _Delta()
    d.content = text
    d.reasoning_content = thought

    class _Choice:
        pass

    c = _Choice()
    c.delta = d
    c.finish_reason = None

    class _Chunk:
        pass

    ch = _Chunk()
    ch.choices = [c]
    return ch


async def _fake_thinking_completion(*args, **kwargs):
    async def gen():
        yield _thinking_chunk(thought="прикидываю варианты…")
        yield _thinking_chunk(text="Ответ")
    return gen()


@pytest.mark.asyncio
async def test_on_thought_callback_receives_reasoning():
    """Мысли модели идут в колбэк on_thought, а в ответ не попадают."""
    thoughts = []
    with patch("backend.llm_gateway.litellm.acompletion", new=_fake_thinking_completion):
        tokens = [t async for t in stream_completion(
            [{"role": "user", "content": "hi"}], None, None, on_thought=thoughts.append
        )]
    assert thoughts == ["прикидываю варианты…"]
    assert "".join(tokens) == "Ответ"


@pytest.mark.asyncio
async def test_large_multimodal_payload_gets_bigger_timeout():
    """Большое inline-видео не должно падать по обычному 120-с таймауту."""
    from backend.config import settings

    big = AttachmentIn(type="video", data="data:video/mp4;base64," + "A" * 10_000_000, mime="video/mp4")
    content = build_user_content("смотри", [big])
    with patch("backend.llm_gateway.litellm.acompletion", new=_fake_acompletion):
        _ = [t async for t in stream_completion([{"role": "user", "content": content}])]
    assert _fake_acompletion.captured["timeout"] == max(
        settings.REQUEST_TIMEOUT, settings.LARGE_REQUEST_TIMEOUT
    )


@pytest.mark.asyncio
async def test_timeout_scales_with_payload_size():
    """Очень большой файл получает таймаут пропорционально размеру (~10 с/МБ):
    в таймаут входит и заливка payload'а из прокси в Vertex."""
    big = AttachmentIn(
        type="video", data="data:video/mp4;base64," + "A" * 120_000_000, mime="video/mp4"
    )
    content = build_user_content("", [big])
    with patch("backend.llm_gateway.litellm.acompletion", new=_fake_acompletion):
        _ = [t async for t in stream_completion([{"role": "user", "content": content}])]
    # ~114 МБ -> больше базового LARGE_REQUEST_TIMEOUT (900 с), но в пределах потолка.
    assert 900 < _fake_acompletion.captured["timeout"] <= 1800


async def _conn_error_acompletion(*args, **kwargs):
    raise Exception("litellm.InternalServerError: Litellm_proxyException - Connection error.")


@pytest.mark.asyncio
async def test_connection_error_on_big_payload_gets_friendly_message():
    """Обрыв соединения на большом файле объясняется по-человечески (не голым
    «Connection error»), исходный текст ошибки сохраняется для отладки."""
    big = AttachmentIn(
        type="video", data="data:video/mp4;base64," + "A" * 10_000_000, mime="video/mp4"
    )
    content = build_user_content("", [big])
    with patch("backend.llm_gateway.litellm.acompletion", new=_conn_error_acompletion):
        with pytest.raises(RuntimeError) as ei:
            _ = [t async for t in stream_completion([{"role": "user", "content": content}])]
    assert "Соединение оборвалось" in str(ei.value)
    assert "Connection error" in str(ei.value)


@pytest.mark.asyncio
async def test_small_payload_keeps_default_timeout():
    from backend.config import settings

    with patch("backend.llm_gateway.litellm.acompletion", new=_fake_acompletion):
        _ = [t async for t in stream_completion([{"role": "user", "content": "привет"}])]
    assert _fake_acompletion.captured["timeout"] == settings.REQUEST_TIMEOUT


def test_build_user_content_with_video_data_uri():
    # Видео уходит тем же путём, что и PDF: data:URI внутри image_url ->
    # LiteLLM превращает его в inline_data для Gemini (видео нативно).
    att = AttachmentIn(type="video", data="data:video/mp4;base64,AAAA", mime="video/mp4", name="clip.mp4")
    content = build_user_content("смотри", [att])
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:video/mp4;base64,")


def test_build_user_content_video_bare_base64_gets_data_uri():
    # Голый base64 (например из Telegram) оборачивается в data:URI по mime.
    att = AttachmentIn(type="video", data="AAAA", mime="video/webm")
    content = build_user_content("", [att])
    assert content[0]["image_url"]["url"] == "data:video/webm;base64,AAAA"


def test_legacy_video_saved_as_document_is_rerouted():
    # Регресс: раньше видео сохранялось с type="document" (и декодировалось как
    # текст-мусор). Такие старые записи в истории перенаправляются по mime.
    att = AttachmentIn(type="document", data="data:video/mp4;base64,AAAA", mime="video/mp4", name="старое.mp4")
    content = build_user_content("", [att])
    assert content[0]["type"] == "image_url"
    assert content[0]["image_url"]["url"].startswith("data:video/mp4;base64,")


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
