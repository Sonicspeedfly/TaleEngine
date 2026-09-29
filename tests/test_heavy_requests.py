"""
Тяжёлые запросы с файлами: чистый base64 в каждом медиа-блоке и повтор хода
без файлов истории, если прокси или провайдер не приняли запрос целиком.
"""
import base64
from unittest.mock import patch

from backend.llm_gateway import build_user_content, split_base64, stream_completion
from backend.schemas import AttachmentIn

RAW = base64.b64encode(b"\x00\x01binary-bytes" * 50).decode()


def test_split_base64_cleans_browser_mime_newlines_and_urlsafe():
    wrapped = "\n".join(RAW[i:i + 76] for i in range(0, len(RAW), 76))
    mime, data = split_base64("data:video/webm;codecs=vp8,opus;base64," + wrapped)
    assert mime == "video/webm" and data == RAW
    urlsafe = RAW.replace("+", "-").replace("/", "_").rstrip("=")
    assert split_base64(urlsafe, "audio/mpeg") == ("audio/mpeg", RAW)
    assert split_base64(RAW, "image/png; charset=x") == ("image/png", RAW)


def test_media_blocks_carry_clean_data_uris():
    blocks = build_user_content("смотри", [
        AttachmentIn(type="video", data="data:video/webm;codecs=vp8,opus;base64," + RAW,
                     mime="video/webm;codecs=vp8,opus", name="запись.webm"),
        AttachmentIn(type="image", data=RAW, mime="image/png", name="кадр.png"),
        AttachmentIn(type="audio", data="data:audio/ogg; codecs=opus;base64," + RAW,
                     mime="audio/ogg; codecs=opus", name="голос.ogg"),
    ])
    urls = [b["image_url"]["url"] for b in blocks if b.get("type") == "image_url"]
    assert urls == ["data:video/webm;base64," + RAW, "data:image/png;base64," + RAW]
    audio = next(b["input_audio"] for b in blocks if b.get("type") == "input_audio")
    assert audio == {"data": RAW, "format": "ogg"}


def _chunk(text):
    class _Delta:
        content = text

    class _Choice:
        delta = _Delta()

    class _Chunk:
        choices = [_Choice()]

    return _Chunk()


def _messages():
    old = build_user_content("вот аудио", [AttachmentIn(type="audio", data=RAW, mime="audio/mpeg", name="a.mp3")])
    now = build_user_content("а это фото", [AttachmentIn(type="image", data=RAW, mime="image/png", name="b.png")])
    return [
        {"role": "system", "content": "ты — рассказчик"},
        {"role": "user", "content": old},
        {"role": "assistant", "content": "слушаю"},
        {"role": "user", "content": now},
    ]


def _media(message):
    c = message["content"]
    return [b for b in c if isinstance(b, dict) and b.get("type") in ("image_url", "input_audio")] \
        if isinstance(c, list) else []


async def test_heavy_request_is_retried_without_history_files():
    calls = []

    async def fake(**kw):
        calls.append(kw["messages"])
        if len(calls) == 1:
            raise RuntimeError("VertexAIException - Base64 decoding failed for inline_data")

        async def gen():
            for t in ("Го", "тово"):
                yield _chunk(t)

        return gen()

    notes = []
    with patch("backend.llm_gateway.litellm.acompletion", new=fake):
        out = [t async for t in stream_completion(_messages(), on_notice=notes.append)]
    assert "".join(out) == "Готово"
    assert len(calls) == 2
    first, second = calls
    assert _media(first[1]) and not _media(second[1])        # файл истории снят
    assert _media(second[-1]) == _media(first[-1])           # файл этой реплики остался
    assert any("[Файл ранее присланный: «a.mp3» — аудио]" in (b.get("text") or "")
               for b in second[1]["content"])                # подпись о файле осталась
    assert len(notes) == 1 and "без 1 старых файлов" in notes[0]


async def test_unrelated_errors_and_requests_without_history_files_are_not_retried():
    calls = []

    async def failing(**kw):
        calls.append(1)
        raise RuntimeError("Base64 decoding failed")

    msgs = [{"role": "user", "content": "привет"}]
    with patch("backend.llm_gateway.litellm.acompletion", new=failing):
        try:
            [t async for t in stream_completion(msgs)]
        except RuntimeError:
            pass
        else:
            raise AssertionError("ошибка должна дойти до вызывающего")
    assert len(calls) == 1

    calls.clear()

    async def auth_error(**kw):
        calls.append(1)
        raise RuntimeError("AuthenticationError: invalid api key")

    with patch("backend.llm_gateway.litellm.acompletion", new=auth_error):
        try:
            [t async for t in stream_completion(_messages())]
        except RuntimeError:
            pass
    assert len(calls) == 1
