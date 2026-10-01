"""
Файлы для модели ссылкой (backend/media_refs.py): файл один раз загружается в
хранилище провайдера через LiteLLM-прокси, дальше модель получает ссылку.

Проверяем без сети: заготовки в истории, лимит «целиком» на запрос, ссылки из
таблицы media_refs, разбор ошибок чтения ссылок, фоновую загрузку (подменный
клиент OpenAI SDK к /files прокси), проверку хранилища, триггер удаления и доли
окна под файлы.
"""
import base64
import os
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from backend import media_refs as mr
from backend.config import settings

PROXY = {"use_proxy": True, "base_url": "http://proxy.test:4000", "api_key": "sk-test",
         "default_model": "gem"}
GS = "gs://bucket-a/litellm-vertex-files/uploads/abc-te-1.ogg"


def _b64(n: int, fill: bytes = b"x") -> str:
    return base64.b64encode(fill * n).decode()


def _chunk(text):
    class _Delta:
        content = text

    class _Choice:
        delta = _Delta()

    class _Chunk:
        choices = [_Choice()]

    return _Chunk()


def _ok_stream(*tokens):
    async def gen():
        for t in tokens:
            yield _chunk(t)
    return gen()


@pytest.fixture
async def db_ready(monkeypatch):
    from backend.database import init_db

    # Ссылки включены (в conftest MEDIA_REFS=0 — чтобы не стартовал загрузчик).
    monkeypatch.setattr(settings, "MEDIA_REFS", True)
    await init_db()
    mr._caps.clear()
    mr._suspects.clear()
    # Проверки хранилища тесты кладут в кэш сами — не перечитывать его из базы.
    mr._caps_read_at = float("inf")
    yield
    mr._caps.clear()
    mr._suspects.clear()


async def _blob(data: str, message_id=None) -> int:
    from backend.database import AsyncSessionLocal
    from backend.models import AttachmentBlob

    async with AsyncSessionLocal() as db:
        b = AttachmentBlob(message_id=message_id, data=data)
        db.add(b)
        await db.commit()
        return b.id


async def _ref(blob_id: int, uri: str, *, scope=None, owner=None, status="ready", mime="audio/ogg",
               **extra) -> None:
    from backend.database import AsyncSessionLocal
    from backend.models import MediaRef

    async with AsyncSessionLocal() as db:
        db.add(MediaRef(blob_id=blob_id, blob_owner=owner, scope=scope or _gcs_scope(), family="gcs",
                        uri=uri, file_id=mr.encode_file_id(uri, "gem"), mime=mime, bytes=1000,
                        status=status, **extra))
        await db.commit()


def _fake_openai(create=None, delete=None, seen=None):
    """
    Подменный openai.AsyncOpenAI: файлы грузятся через OpenAI SDK к /files
    прокси. create(**kw) получает file/purpose/extra_body, как files.create.
    """
    class _Files:
        async def create(self, **kw):
            if seen is not None:
                seen.setdefault("calls", []).append(kw)
            return await create(**kw)

        async def delete(self, file_id):
            return await (delete(file_id) if delete else _none())

    async def _none():
        return None

    class _Client:
        def __init__(self, **kw):
            if seen is not None:
                seen["client"] = kw
            self.files = _Files()

        async def close(self):
            pass

    return patch("openai.AsyncOpenAI", _Client)


def _raising(exc):
    async def f(**kw):
        raise exc
    return f


def _route():
    return mr.route_for(None, PROXY)


def _gcs_ok():
    route = _route()
    mr._caps[route.cap_key] = {"family": "gcs", "ok": True, "bucket": "bucket-a", "alias": "gem",
                               "proxy": route.base_url, "checked_at": 0, "until": 0}
    return route


def _gcs_scope():
    route = _route()
    return f"gcs:{route.proxy_key}:bucket-a"


def _hist(metas, text="вот"):
    return [{"role": "user", "content": mr.history_content(text, metas)},
            {"role": "assistant", "content": "ок"}]


# ---------------------------------------------------------------- заготовки

def test_placeholders_carry_meta_not_data():
    metas = [
        {"type": "audio", "mime": "audio/ogg; codecs=opus", "name": "голос.ogg", "size": 40_000, "blob_id": 7},
        {"type": "document", "mime": "video/mp4", "name": "old.mp4", "size": 10, "blob_id": 8},
        {"type": "document", "mime": "application/pdf", "name": "a.pdf", "size": 10, "blob_id": 9},
        {"type": "document", "mime": "", "name": "b.docx", "size": 10, "blob_id": 10},
        {"type": "image", "mime": "image/png", "name": "no-data.png"},        # без данных — пропуск
    ]
    content = mr.history_content("смотри", metas)
    marks = [b[mr.MARK] for b in content if mr.MARK in b]
    assert [m["kind"] for m in marks] == ["audio", "video", "pdf", "document"]
    assert all("data" not in m for m in marks)
    labels = [b["text"] for b in content if b.get("type") == "text" and mr.MARK not in b]
    assert "[Файл ранее присланный: «голос.ogg» — аудио]" in labels
    assert "[Файл ранее присланный: «b.docx» — документ]" in labels   # имя документа не теряется


def test_norm_mime_and_names_are_safe():
    assert mr.norm_mime("audio", "audio/x-wav; rate=1") == "audio/wav"
    assert mr.norm_mime("image", "image/jpg") == "image/jpeg"
    assert mr.norm_mime("video", "") == "video/mp4"
    name = mr._filename(42, "abcdef0123456789", "audio/ogg")
    assert name == "te-42-abcdef012345.ogg" and name.isascii()


def test_media_tokens_follow_real_weight():
    minute_voice = mr.media_tokens("audio", "audio/ogg", 4_000 * 60)
    assert 1500 <= minute_voice <= 2500                     # 32 токена в секунду
    minute_video = mr.media_tokens("video", "video/mp4", 400_000 * 60)
    assert 15_000 <= minute_video <= 20_000                 # ~300 токенов в секунду
    assert mr.media_tokens("image", "image/png", 10) == 1100
    ph = mr.placeholder({"type": "audio", "mime": "audio/ogg", "size": 4_000 * 60, "blob_id": 1})
    assert mr.block_tokens(ph) == minute_voice


def test_file_id_roundtrip_and_raw_ids():
    fid = mr.encode_file_id(GS, "gem")
    assert fid.startswith("file-") and "=" not in fid
    assert mr.decode_file_id(fid) == GS
    assert mr.decode_file_id(GS) == GS                       # старый прокси отдаёт ссылку как есть
    assert mr.family_of(GS) == "gcs" and mr.gcs_bucket(GS) == "bucket-a"
    assert mr.family_of(mr.FILES_API_PREFIX + "x1") == "files_api"


def test_route_requires_proxy():
    assert mr.route_for(None, {"use_proxy": False, "default_model": "gem"}) is None
    assert mr.route_for(None, {**PROXY, "base_url": ""}) is None
    r = mr.route_for(None, {**PROXY, "api_key": ""})
    assert r.api_key == "sk-no-key-required" and r.alias == "gem"


# ---------------------------------------------------------------- «целиком»

async def test_inline_budget_is_per_request_newest_first(db_ready, monkeypatch):
    monkeypatch.setattr(settings, "INLINE_FILES_MB", 1)
    old = await _blob(_b64(400_000, b"o"))
    new = await _blob(_b64(400_000, b"n"))
    hist = _hist([{"type": "audio", "mime": "audio/mpeg", "name": "old.mp3", "size": 400_000, "blob_id": old}]) \
        + _hist([{"type": "audio", "mime": "audio/mpeg", "name": "new.mp3", "size": 400_000, "blob_id": new}])
    built = await mr.materialize(hist, connection={"use_proxy": False})
    audio = [b for m in built.messages for b in (m["content"] if isinstance(m["content"], list) else [])
             if b.get("type") == "input_audio"]
    assert len(audio) == 1                                   # влез только свежий
    assert base64.b64decode(audio[0]["input_audio"]["data"])[:1] == b"n"
    assert mr.NOTE_OVER in [b.get("text") for b in built.messages[0]["content"]]
    assert not mr.has_markers(built.messages)

    # Файл текущей реплики лимит истории НЕ меняет: иначе набор файлов истории
    # скакал бы от хода к ходу и сбивал кэш провайдера.
    current = {"role": "user", "content": [
        {"type": "text", "text": "и это"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + _b64(700_000)}}]}
    again = await mr.materialize(hist + [current], connection={"use_proxy": False})
    assert again.messages[:4] == built.messages


async def test_regenerate_files_and_knowledge_go_first(db_ready, monkeypatch):
    monkeypatch.setattr(settings, "INLINE_FILES_MB", 1)
    kb = await _blob(_b64(500_000, b"k"))
    cur = await _blob(_b64(900_000, b"c"))     # больше лимита — но это файл отвечаемой реплики
    hist_b = await _blob(_b64(500_000, b"h"))
    msgs = [
        {"role": "user", "content": [mr.placeholder(
            {"type": "image", "mime": "image/png", "size": 500_000, "blob_id": kb}, priority=1)]},
        *_hist([{"type": "image", "mime": "image/png", "size": 500_000, "blob_id": hist_b}]),
        {"role": "user", "content": mr.history_content(
            "перегенерируй", [{"type": "image", "mime": "image/png", "size": 900_000, "blob_id": cur}],
            current=True, priority=2)},
    ]
    built = await mr.materialize(msgs, connection={"use_proxy": False})
    urls = {base64.b64decode(b["image_url"]["url"].split(",", 1)[1])[:1]
            for m in built.messages for b in (m["content"] if isinstance(m["content"], list) else [])
            if b.get("type") == "image_url"}
    assert urls == {b"c", b"k"}                              # история — третьей, не влезла
    assert "[Файл в этом сообщении" in str(built.messages[-1]["content"])
    assert "ВНИМАНИЕ" in str(built.messages[-1]["content"])   # как в исходном ходе


# ---------------------------------------------------------------- ссылки

async def test_ready_ref_replaces_file_and_skips_data(db_ready):
    _gcs_ok()
    bid = await _blob(_b64(1000), message_id=None)
    await _ref(bid, GS)
    hist = _hist([{"type": "audio", "mime": "audio/ogg", "name": "v.ogg", "size": 1000, "blob_id": bid}])
    with patch.object(mr, "_load_blobs", side_effect=AssertionError("данные ссылки читать нельзя")) as lb:
        lb.side_effect = None
        lb.return_value = {}
        built = await mr.materialize(hist, connection=PROXY)
        assert lb.call_args.args[0] == []
    block = next(b for b in built.messages[0]["content"] if b.get("type") == "image_url")
    assert block == {"type": "image_url", "image_url": {"url": GS, "format": "audio/ogg"}}
    assert built.used == [{"uri": GS, "bytes": 1000}]
    assert built.summary["refs"] == 1


async def test_ref_of_reused_blob_id_is_ignored(db_ready):
    _gcs_ok()
    bid = await _blob(_b64(1000), message_id=555)
    await _ref(bid, GS, owner=111)                          # ссылка от прежнего владельца id
    built = await mr.materialize(
        _hist([{"type": "audio", "mime": "audio/ogg", "size": 1000, "blob_id": bid}]), connection=PROXY)
    assert built.used == []
    assert any(b.get("type") == "input_audio" for b in built.messages[0]["content"])


async def test_waiting_and_expired_refs_are_not_used(db_ready):
    _gcs_ok()
    a = await _blob(_b64(1000))
    b = await _blob(_b64(1000))
    await _ref(a, GS, usable_after=mr._now() + timedelta(minutes=5))
    await _ref(b, GS + "2", expires_at=mr._now() + timedelta(minutes=10))
    built = await mr.materialize(
        _hist([{"type": "audio", "mime": "audio/ogg", "size": 1000, "blob_id": a},
               {"type": "audio", "mime": "audio/ogg", "size": 1000, "blob_id": b}]), connection=PROXY)
    assert built.used == []


async def test_deleting_blob_drops_its_refs_via_trigger(db_ready):
    from sqlalchemy import delete, select

    from backend.database import AsyncSessionLocal
    from backend.models import AttachmentBlob, MediaRef, MediaRefTrash

    bid = await _blob(_b64(10))
    await _ref(bid, GS + "-trig")
    async with AsyncSessionLocal() as db:
        await db.execute(delete(AttachmentBlob).where(AttachmentBlob.id == bid))
        await db.commit()
        assert (await db.execute(select(MediaRef).where(MediaRef.blob_id == bid))).first() is None
        trash = (await db.execute(select(MediaRefTrash.uri))).scalars().all()
    assert GS + "-trig" in trash
    trash_rows = await mr.sweep_orphans()
    assert any(t[1:] == (_gcs_scope(), GS + "-trig") for t in trash_rows)
    # Строка корзины живёт, пока хранилище не подтвердило удаление копии.
    assert any(t[1:] == (_gcs_scope(), GS + "-trig") for t in await mr.sweep_orphans())
    await mr._drop_trash([t[0] for t in trash_rows])
    assert not any(t[2] == GS + "-trig" for t in await mr.sweep_orphans())


# ---------------------------------------------------------------- сбои ссылок

async def test_named_missing_ref_is_invalidated_and_turn_retried_inline(db_ready):
    from sqlalchemy import select

    from backend.database import AsyncSessionLocal
    from backend.llm_gateway import stream_completion
    from backend.models import MediaRef

    _gcs_ok()
    bid = await _blob(_b64(1000))
    await _ref(bid, GS)
    calls = []

    async def fake(**kw):
        calls.append(kw["messages"])
        if len(calls) == 1:
            raise RuntimeError(f"VertexAIException - No such object: {GS[5:]}")
        return _ok_stream("ок")

    msgs = [{"role": "system", "content": "s"},
            *_hist([{"type": "audio", "mime": "audio/ogg", "size": 1000, "blob_id": bid}]),
            {"role": "user", "content": "ну?"}]
    notes = []
    with patch("backend.llm_gateway.litellm.acompletion", new=fake):
        out = [t async for t in stream_completion(msgs, None, PROXY, on_notice=notes.append)]
    assert "".join(out) == "ок" and len(calls) == 2
    assert any(b.get("image_url", {}).get("url") == GS for b in calls[0][1]["content"])
    assert any(b.get("type") == "input_audio" for b in calls[1][1]["content"])  # повтор — целиком
    assert notes and "по ссылке" in notes[0]
    async with AsyncSessionLocal() as db:
        row = (await db.execute(select(MediaRef).where(MediaRef.blob_id == bid))).scalar_one()
    assert row.status == "failed"


async def test_ambiguous_ref_error_invalidates_nothing(db_ready):
    from sqlalchemy import select

    from backend.database import AsyncSessionLocal
    from backend.models import MediaRef

    route = _gcs_ok()
    bid = await _blob(_b64(1000))
    await _ref(bid, GS)
    probes = []
    with patch.object(mr.uploader, "enqueue_probe", side_effect=lambda r, force=False: probes.append(r)):
        bad = await mr.handle_ref_error([{"uri": GS, "bytes": 1}], RuntimeError(
            "400 Unable to fetch the file from the provided url"), None, PROXY)
    assert bad == {GS} and probes == [route]
    async with AsyncSessionLocal() as db:
        row = (await db.execute(select(MediaRef).where(MediaRef.blob_id == bid))).scalar_one()
    assert row.status == "ready"                              # никаких массовых перезагрузок
    mr.note_success([{"uri": GS}], None, PROXY)
    assert not mr._suspects
    # Ошибка не про ссылки — повтора нет.
    assert await mr.handle_ref_error([{"uri": GS}], RuntimeError("quota exceeded"), None, PROXY) is None


async def test_inactive_file_is_deferred_not_failed(db_ready):
    from sqlalchemy import select

    from backend.database import AsyncSessionLocal
    from backend.models import MediaRef

    _gcs_ok()
    bid = await _blob(_b64(1000))
    await _ref(bid, GS)
    await mr.handle_ref_error([{"uri": GS}], RuntimeError(
        "400 FAILED_PRECONDITION: The File is not in an ACTIVE state"), None, PROXY)
    async with AsyncSessionLocal() as db:
        row = (await db.execute(select(MediaRef).where(MediaRef.blob_id == bid))).scalar_one()
    assert row.status == "ready" and row.usable_after > mr._now()


async def test_timeout_with_only_refs_does_not_strip_them():
    from backend.llm_gateway import _history_media_count, _strip_history_media

    msgs = [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": GS, "format": "audio/ogg"}}]},
            {"role": "user", "content": "что там?"}]
    assert _history_media_count(msgs) == 0
    assert _strip_history_media(msgs) is msgs
    glued = {"type": "input_audio", "input_audio": {"data": "AAAA", "format": "mp3"}}
    group = [{"role": "user", "content": [{"type": "text", "text": "транскрипт"}, glued]}]
    assert _history_media_count(group) == 0                  # последняя реплика — не история…
    assert _history_media_count(group, {id(glued)}) == 1     # …но приклеенные файлы группы — да
    assert _strip_history_media(group, {id(glued)})[0]["content"][1]["type"] == "text"


# ---------------------------------------------------------------- загрузчик

async def test_upload_writes_ready_ref_with_unique_name(db_ready):
    from sqlalchemy import select

    from backend.database import AsyncSessionLocal
    from backend.models import MediaRef

    route = _gcs_ok()
    raw = os.urandom(3000)
    bid = await _blob("data:audio/ogg;base64," + base64.b64encode(raw).decode(), message_id=None)
    seen = {}

    async def fake_create(**kw):
        name, fh, mime = kw["file"]
        seen.update(name=name, body=fh.read(), mime=mime,
                    body_model=(kw.get("extra_body") or {}).get("model"))
        uri = f"gs://bucket-a/litellm-vertex-files/uploads/u-{name}"
        return SimpleNamespace(id=mr.encode_file_id(uri, "gem"), bytes=len(raw))

    job = mr._Job(mr._LANE_TURN, 1, "upload", route,
                  te=mr.placeholder({"type": "audio", "mime": "audio/ogg", "size": 3000, "blob_id": bid})[mr.MARK])
    with _fake_openai(fake_create, seen=seen):
        await mr.uploader._upload(job)
    assert seen["body"] == raw and seen["mime"] == "audio/ogg"
    assert seen["name"].startswith(f"te-{bid}-") and seen["name"].endswith(".ogg")
    assert seen["body_model"] == "gem"
    client = seen["client"]                                  # прямо к /files прокси, без повторов
    assert client["base_url"] == PROXY["base_url"] and client["api_key"] == "sk-test"
    assert client["max_retries"] == 0 and client["default_headers"] == {"x-litellm-model": "gem"}
    async with AsyncSessionLocal() as db:
        row = (await db.execute(select(MediaRef).where(MediaRef.blob_id == bid))).scalar_one()
    assert row.status == "ready" and row.uri.startswith("gs://bucket-a/") and row.bytes == 3000
    assert row.expires_at is None


async def test_upload_errors_are_classified(db_ready):
    from sqlalchemy import select

    from backend.database import AsyncSessionLocal
    from backend.models import MediaRef

    route = _gcs_ok()

    def _job(bid):
        return mr._Job(mr._LANE_BACKFILL, 1, "upload", route,
                       te=mr.placeholder({"type": "image", "mime": "image/png", "size": 100, "blob_id": bid})[mr.MARK])

    async def _status(bid):
        async with AsyncSessionLocal() as db:
            return (await db.execute(select(MediaRef).where(MediaRef.blob_id == bid))).scalar_one()

    # 413 — файл не примут никогда: «rejected», без повторов.
    a = await _blob(_b64(100))
    with _fake_openai(_raising(RuntimeError("413 Request Entity Too Large"))):
        await mr.uploader._upload(_job(a))
    assert (await _status(a)).status == "rejected"

    # Обрыв сети — временный сбой: повтор позже.
    b = await _blob(_b64(100))
    with _fake_openai(_raising(RuntimeError("Connection reset by peer"))):
        await mr.uploader._upload(_job(b))
    row = await _status(b)
    assert row.status == "failed" and row.attempts == 1 and row.next_try_at > mr._now()

    # Бакет пропал — ломается хранилище целиком: модель выключается до перепроверки.
    c = await _blob(_b64(100))
    with _fake_openai(_raising(RuntimeError("500 GCS bucket_name is required"))):
        await mr.uploader._upload(_job(c))
    assert mr.usable(route) == "" and "GCS_BUCKET_NAME" in mr.capability(route)["error"]


async def test_upload_is_deferred_when_server_is_short_of_memory(db_ready):
    route = _gcs_ok()
    bid = await _blob(_b64(100))
    job = mr._Job(mr._LANE_BACKFILL, 1, "upload", route,
                  te=mr.placeholder({"type": "image", "mime": "image/png", "size": 100, "blob_id": bid})[mr.MARK])
    from sqlalchemy import select

    from backend.database import AsyncSessionLocal
    from backend.models import MediaRef

    calls = []

    async def fake_create(**kw):
        calls.append(kw)

    with patch.object(mr, "_mem_available", return_value=10), _fake_openai(fake_create):
        await mr.uploader._upload(job)
    assert calls == []                                        # ничего не грузили
    async with AsyncSessionLocal() as db:
        row = (await db.execute(select(MediaRef).where(MediaRef.blob_id == bid))).scalar_one()
    assert row.status == "failed" and row.attempts == 0      # «не сейчас», а не сбой
    assert "памяти" in row.error and row.next_try_at > mr._now() + timedelta(minutes=9)


async def test_plan_never_enqueues_files_over_upload_cap(db_ready, monkeypatch):
    monkeypatch.setattr(settings, "MEDIA_UPLOAD_MAX_MB", 1)
    _gcs_ok()
    bid = await _blob(_b64(10))
    p, _ = await mr.plan(_hist([{"type": "video", "mime": "video/mp4", "size": 5 * 1024 * 1024,
                                 "blob_id": bid}]), None, PROXY)
    assert p.enqueue == []


# ---------------------------------------------------------------- проверка

async def test_probe_marks_gemini_on_vertex_as_working(db_ready):
    route = _route()
    uri = "gs://bucket-a/litellm-vertex-files/uploads/p-taleengine-probe.png"
    created = {}

    async def fake_create(**kw):
        created["body"] = kw["file"][1]
        return SimpleNamespace(id=mr.encode_file_id(uri, "gem"))

    async def fake_chat(**kw):
        created["block"] = kw["messages"][0]["content"][1]
        return SimpleNamespace()

    deleted = []
    with patch.object(mr, "_alias_deployments", return_value=["vertex_ai/gemini-2.5-pro"]), \
            _fake_openai(fake_create), \
            patch("litellm.acompletion", new=fake_chat), \
            patch.object(mr, "_delete_remote", side_effect=lambda r, f: deleted.append(f)):
        cap = await mr.probe(route)
    assert cap["ok"] and cap["family"] == "gcs" and cap["bucket"] == "bucket-a"
    assert created["body"][:4] == b"\x89PNG"                   # двоичный файл, а не текст
    assert created["block"] == {"type": "image_url", "image_url": {"url": uri, "format": "image/png"}}
    assert deleted                                              # тестовый файл убран


async def test_probe_rejects_non_gemini_and_explains_missing_bucket(db_ready):
    route = _route()
    with patch.object(mr, "_alias_deployments", return_value=["vertex_ai/claude-sonnet"]):
        cap = await mr.probe(route)
    assert not cap["ok"] and "Gemini" in cap["error"]
    with patch.object(mr, "_alias_deployments", return_value=None), \
            _fake_openai(_raising(RuntimeError("GCS bucket_name is required"))):
        cap = await mr.probe(route)
    assert not cap["ok"] and "GCS_BUCKET_NAME" in cap["error"]


# ---------------------------------------------------------------- доля окна

def test_media_share_of_window_is_capped_and_text_survives():
    from backend.horae_memory import NOTE_MEDIA_WINDOW, cap_history_media

    video = {"type": "video", "mime": "video/mp4", "size": 400_000 * 60, "blob_id": 1}   # ≈18 тыс.
    hist = _hist([video], "старое видео") + _hist([dict(video, blob_id=2)], "новое видео")
    capped = cap_history_media(hist, 20_000)
    first, second = capped[0]["content"], capped[2]["content"]
    assert any(b.get("text") == NOTE_MEDIA_WINDOW for b in first)      # старое — пометкой
    assert any(mr.MARK in b for b in second)                           # новое осталось
    assert first[0]["text"] == "старое видео"                          # текст на месте
    assert cap_history_media(hist, 0) is hist


# ---------------------------------------------------------------- API и прочее

def test_media_status_endpoint_reports_direct_mode(client):
    with patch("backend.main.get_connection", return_value={"use_proxy": False, "default_model": "gem"}):
        body = client.get("/api/media/status?model=gem").json()
    assert body["direct"] is True and body["ok"] is False
    with patch("backend.main.get_connection", return_value=PROXY):
        body = client.get("/api/media/status?model=gem").json()
    assert body["direct"] is False and body["model"] == "gem"
    assert {"ok", "checked", "counts", "queued", "inline_mb"} <= set(body)


def test_stored_size_is_computed_from_data():
    import asyncio

    from backend.attachments import store_attachments

    class _Db:
        def add(self, obj):
            obj.id = 1

        async def flush(self):
            pass

    metas = asyncio.run(store_attachments(_Db(), 1, [
        {"type": "image", "data": "data:image/png;base64," + _b64(3000), "size": 999_999_999}]))
    assert metas[0]["size"] == 3000


def test_prestart_prunes_old_copies_only_when_that_helps(tmp_path):
    from scripts import prestart

    db = tmp_path / "aichat.db"
    db.write_bytes(b"x" * 1000)
    backups = tmp_path / "backups"
    backups.mkdir()
    for i in range(3):
        f = backups / f"aichat-2026-01-0{i + 1}.db"
        f.write_bytes(b"y" * 1000)
        os.utime(f, (i, i))
    free = {"v": 500}

    def fake_free(_):
        used = sum(p.stat().st_size for p in backups.glob("*.db"))
        return free["v"] + (3000 - used)

    # Не хватит и без всех копий — копии не трогаем.
    assert prestart.ensure_backup_space(db, backups, headroom=5000, free=fake_free) is False
    assert len(list(backups.glob("*.db"))) == 3
    # Хватит, если убрать самую старую, — убирается только она.
    assert prestart.ensure_backup_space(db, backups, headroom=300, free=fake_free) is True
    left = sorted(p.name for p in backups.glob("*.db"))
    assert left == ["aichat-2026-01-02.db", "aichat-2026-01-03.db"]


def test_regenerate_sends_files_of_the_answered_message(client):
    """Раньше перегенерация теряла файлы реплики, на которую отвечает; теперь они идут."""
    captured = {}

    async def fake(**kw):
        captured["messages"] = kw["messages"]
        return _ok_stream("Ок")

    cid = client.post("/api/characters", json={"name": "Реген"}).json()["id"]
    sid = client.post(f"/api/sessions?character_id={cid}").json()["session_id"]
    raw = b"regen-photo" * 30
    with patch("backend.llm_gateway.litellm.acompletion", new=fake):
        with client.websocket_connect(f"/ws/chat/{sid}") as ws:
            ws.send_json({"type": "user_message", "content": "что на фото?", "attachments": [
                {"type": "image", "data": "data:image/png;base64," + base64.b64encode(raw).decode(),
                 "mime": "image/png", "name": "p.png"}]})
            for _ in range(50):
                if ws.receive_json()["type"] in ("done", "error"):
                    break
            captured.clear()
            ws.send_json({"type": "regenerate"})
            for _ in range(50):
                if ws.receive_json()["type"] in ("done", "error"):
                    break
    last = captured["messages"][-1]["content"]
    assert isinstance(last, list)
    assert any(base64.b64encode(raw).decode() in ((b.get("image_url") or {}).get("url") or "") for b in last)
    assert any("[Файл в этом сообщении: «p.png» — изображение]" == b.get("text") for b in last)
    assert not mr.has_markers(captured["messages"])


async def test_blob_deleted_during_upload_leaves_no_ref(db_ready):
    """Файл удалили посреди загрузки — ссылка не достаётся новому файлу с тем же id."""
    from sqlalchemy import delete, select

    from backend.database import AsyncSessionLocal
    from backend.models import AttachmentBlob, MediaRef

    route = _gcs_ok()
    bid = await _blob(_b64(500))
    deletes = []

    async def fake_create(**kw):
        async with AsyncSessionLocal() as db:   # пока файл «летит», его удаляют
            await db.execute(delete(AttachmentBlob).where(AttachmentBlob.id == bid))
            await db.commit()
        uri = "gs://bucket-a/litellm-vertex-files/uploads/z-" + kw["file"][0]
        return SimpleNamespace(id=mr.encode_file_id(uri, "gem"), bytes=500)

    job = mr._Job(mr._LANE_TURN, 1, "upload", route,
                  te=mr.placeholder({"type": "image", "mime": "image/png", "size": 500, "blob_id": bid})[mr.MARK])
    with _fake_openai(fake_create), \
            patch.object(mr.uploader, "enqueue_delete", side_effect=lambda r, f: deletes.append(f)):
        await mr.uploader._upload(job)
    async with AsyncSessionLocal() as db:
        assert (await db.execute(select(MediaRef).where(MediaRef.blob_id == bid))).first() is None
    assert deletes and mr.decode_file_id(deletes[0]).startswith("gs://bucket-a/")


def test_unsendable_files_become_notes_before_costing(monkeypatch):
    monkeypatch.setattr(settings, "INLINE_FILES_MB", 1)
    monkeypatch.setattr(settings, "MEDIA_REFS", True)
    hist = _hist([{"type": "video", "mime": "video/mp4", "size": 900_000, "blob_id": 1}]) \
        + _hist([{"type": "video", "mime": "video/mp4", "size": 600_000, "blob_id": 2}])
    route = mr.route_for(None, PROXY)                    # хранилище не проверено
    pruned = mr.prune_unsendable(hist, route)
    assert mr.NOTE_OVER in [b.get("text") for b in pruned[0]["content"]]   # старое не влезло
    assert any(mr.MARK in b for b in pruned[2]["content"])                  # свежее осталось
    _gcs_ok()
    try:
        assert mr.prune_unsendable(hist, route) is hist   # хранилище есть — всё пойдёт ссылкой
    finally:
        mr._caps.clear()


async def test_token_limit_error_retries_without_history_refs(db_ready):
    from backend.llm_gateway import stream_completion

    _gcs_ok()
    bid = await _blob(_b64(1000))
    await _ref(bid, GS)
    calls = []

    async def fake(**kw):
        calls.append(kw["messages"])
        if len(calls) == 1:
            raise RuntimeError("400 The input token count (1300000) exceeds the maximum number of tokens")
        return _ok_stream("ок")

    msgs = [*_hist([{"type": "audio", "mime": "audio/ogg", "size": 1000, "blob_id": bid}]),
            {"role": "user", "content": "дальше"}]
    with patch("backend.llm_gateway.litellm.acompletion", new=fake):
        out = [t async for t in stream_completion(msgs, None, PROXY)]
    assert "".join(out) == "ок" and len(calls) == 2
    assert not any(b.get("type") == "image_url" for b in calls[1][0]["content"])


def test_auto_reasoning_looks_at_current_files_only():
    from backend.llm_gateway import effective_reasoning
    from backend.schemas import GenerationParams

    p = GenerationParams(disable_safety=False, file_reasoning=True)
    photo = {"type": "image_url", "image_url": {"url": GS, "format": "image/png"}}
    old_photo = [{"role": "user", "content": [photo]}, {"role": "user", "content": "просто текст"}]
    assert effective_reasoning(p, old_photo) == ""
    assert effective_reasoning(p, [{"role": "user", "content": ["", photo][1:]}]) == "medium"


def test_documents_are_costed_by_what_is_sent():
    """docx на 2 МБ — пара страниц PDF, а не 500 тыс. токенов «текста»."""
    docx = mr.placeholder({"type": "document", "mime": "", "name": "report.docx", "size": 2_000_000,
                           "blob_id": 5})
    assert mr.block_tokens(docx) <= 20_000
    txt = mr.placeholder({"type": "document", "mime": "text/plain", "name": "a.txt", "size": 4000,
                          "blob_id": 6})
    assert mr.block_tokens(txt) == 1000
    zip_ = mr.placeholder({"type": "document", "mime": "application/zip", "name": "a.zip",
                           "size": 50_000_000, "blob_id": 7})
    assert mr.block_tokens(zip_) == 64


async def test_transient_probe_failure_keeps_working_storage(db_ready):
    import litellm

    route = _gcs_ok()
    uri = "gs://bucket-a/litellm-vertex-files/uploads/p-taleengine-probe.png"

    async def fake_create(**kw):
        return SimpleNamespace(id=mr.encode_file_id(uri, "gem"))

    async def busy(**kw):
        raise litellm.RateLimitError("429 RESOURCE_EXHAUSTED", llm_provider="vertex_ai", model="gem")

    with patch.object(mr, "_alias_deployments", return_value=None), \
            _fake_openai(fake_create), patch("litellm.acompletion", new=busy), \
            patch.object(mr, "_delete_remote", return_value=True):
        cap = await mr.probe(route)
    assert cap["ok"] and mr.usable(route) == "gcs"          # 429 не выключает рабочие ссылки
    assert mr._cap_stale(dict(cap, checked_at=cap["checked_at"] - 1000))   # но скоро перепроверим

