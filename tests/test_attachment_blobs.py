"""
Тесты хранения вложений в blob-таблице (attachment_blobs).

Суть фикса: base64 больше НЕ лежит в JSON-колонке messages.attachments (иначе
каждый ход/открытие чата поднимал в память сотни МБ), а живёт в отдельной
таблице и достаётся точечно. Здесь проверяем весь контур: сохранение, отдачу
байтов, миграцию легаси-строк, гидратацию истории для модели, retry и экспорт.
"""
import base64
import json
import os
import sqlite3
from unittest.mock import patch


def _fake_chunk(text: str):
    class _Delta:
        content = text

    class _Choice:
        delta = _Delta()

    class _Chunk:
        choices = [_Choice()]

    return _Chunk()


async def _fake_acompletion(*args, **kwargs):
    _fake_acompletion.captured = kwargs

    async def gen():
        for token in ["Ок", "!"]:
            yield _fake_chunk(token)

    return gen()


async def _fail_acompletion(*args, **kwargs):
    raise RuntimeError("LLM недоступен")


def _db_path() -> str:
    return os.environ["DATABASE_URL"].split("///")[-1]


def _wait_done(client, job_id: str) -> None:
    with client.stream("GET", f"/sse/job/{job_id}") as resp:
        for line in resp.iter_lines():
            if line and ('"done"' in line or '"error"' in line):
                break


def _send_image(client, sid: int, raw: bytes, text: str = "фото") -> None:
    data_uri = "data:image/png;base64," + base64.b64encode(raw).decode()
    with patch("backend.llm_gateway.litellm.acompletion", new=_fake_acompletion):
        r = client.post(f"/api/sessions/{sid}/send", json={
            "content": text,
            "attachments": [{"type": "image", "data": data_uri, "mime": "image/png", "name": "p.png"}],
        })
        _wait_done(client, r.json()["job_id"])


def test_send_stores_blob_not_inline_and_serves_bytes(client):
    cid = client.post("/api/characters", json={"name": "Блоб"}).json()["id"]
    sid = client.post(f"/api/sessions?character_id={cid}").json()["session_id"]
    raw = b"\x89PNG-blob-bytes" * 64
    _send_image(client, sid, raw)

    msgs = client.get(f"/api/sessions/{sid}/messages").json()
    umsg = [m for m in msgs if m["role"] == "user"][-1]
    mid = umsg["id"]

    # В строке сообщения — только мета с blob_id, БЕЗ base64.
    con = sqlite3.connect(_db_path())
    row = con.execute("SELECT attachments FROM messages WHERE id=?", (mid,)).fetchone()[0]
    atts = json.loads(row)
    assert atts[0].get("blob_id") and "data" not in atts[0]
    assert len(row) < 500  # строка лёгкая, без мегабайт base64
    blob = con.execute(
        "SELECT data, message_id FROM attachment_blobs WHERE id=?", (atts[0]["blob_id"],)
    ).fetchone()
    con.close()
    assert blob[1] == mid and blob[0].startswith("data:image/png;base64,")

    # Эндпоинт вложения по-прежнему отдаёт исходные байты.
    resp = client.get(f"/api/messages/{mid}/att/0")
    assert resp.status_code == 200 and resp.content == raw

    # Удаление сообщения зачищает и blob.
    client.delete(f"/api/messages/{mid}")
    con = sqlite3.connect(_db_path())
    left = con.execute(
        "SELECT COUNT(*) FROM attachment_blobs WHERE id=?", (atts[0]["blob_id"],)
    ).fetchone()[0]
    con.close()
    assert left == 0


def test_attachment_served_with_original_filename(client):
    """Скачивание вложения сохраняет ОРИГИНАЛЬНОЕ имя (Content-Disposition, кириллица)."""
    cid = client.post("/api/characters", json={"name": "Имя"}).json()["id"]
    sid = client.post(f"/api/sessions?character_id={cid}").json()["session_id"]
    raw = b"named-bytes" * 20
    data_uri = "data:image/png;base64," + base64.b64encode(raw).decode()
    with patch("backend.llm_gateway.litellm.acompletion", new=_fake_acompletion):
        r = client.post(f"/api/sessions/{sid}/send", json={
            "content": "фото", "attachments": [
                {"type": "image", "data": data_uri, "mime": "image/png", "name": "мой кот.png"}
            ],
        })
        _wait_done(client, r.json()["job_id"])
    msgs = client.get(f"/api/sessions/{sid}/messages").json()
    mid = [m for m in msgs if m["role"] == "user"][-1]["id"]
    resp = client.get(f"/api/messages/{mid}/att/0")
    cd = resp.headers.get("content-disposition", "")
    # RFC 5987: кириллица уходит через filename*=UTF-8''… (проценты).
    assert "filename*=UTF-8''" in cd
    assert "%D0%BA%D0%BE%D1%82" in cd  # «кот» percent-encoded
    assert resp.content == raw


def test_history_hydrates_blob_attachment_for_model(client):
    """На следующем ходу модель «видит» ранее присланную картинку (данные из blobs)."""
    cid = client.post("/api/characters", json={"name": "Гидра"}).json()["id"]
    sid = client.post(f"/api/sessions?character_id={cid}").json()["session_id"]
    raw = b"hydrate-me" * 20
    _send_image(client, sid, raw, text="запомни фото")

    with patch("backend.llm_gateway.litellm.acompletion", new=_fake_acompletion):
        with client.websocket_connect(f"/ws/chat/{sid}") as ws:
            ws.send_json({"type": "user_message", "content": "что было на фото?"})
            for _ in range(50):
                if ws.receive_json()["type"] in ("done", "error"):
                    break
    sent = _fake_acompletion.captured["messages"]
    b64 = base64.b64encode(raw).decode()
    multimodal = [
        b for m in sent if isinstance(m.get("content"), list) for b in m["content"]
        if isinstance(b, dict) and b.get("type") == "image_url"
    ]
    assert any(b64 in (b["image_url"]["url"] or "") for b in multimodal), \
        "картинка из истории должна дойти до модели (гидратация из blob)"


def test_retry_rehydrates_attachment(client):
    """Retry хода с вложением: данные достаются из blob-таблицы заново."""
    cid = client.post("/api/characters", json={"name": "РетрайБлоб"}).json()["id"]
    sid = client.post(f"/api/sessions?character_id={cid}").json()["session_id"]
    raw = b"retry-bytes" * 30
    data_uri = "data:image/png;base64," + base64.b64encode(raw).decode()
    # 1. Ход падает: сообщение с вложением сохранено, ответа нет.
    with patch("backend.llm_gateway.litellm.acompletion", new=_fail_acompletion):
        r = client.post(f"/api/sessions/{sid}/send", json={
            "content": "смотри", "attachments": [{"type": "image", "data": data_uri, "mime": "image/png"}],
        })
        _wait_done(client, r.json()["job_id"])
    # 2. Retry: LLM ожил; вложение должно уйти модели снова.
    with patch("backend.llm_gateway.litellm.acompletion", new=_fake_acompletion):
        with client.websocket_connect(f"/ws/chat/{sid}") as ws:
            ws.send_json({"type": "retry"})
            for _ in range(50):
                if ws.receive_json()["type"] in ("done", "error"):
                    break
    msgs = client.get(f"/api/sessions/{sid}/messages").json()
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    sent = _fake_acompletion.captured["messages"]
    b64 = base64.b64encode(raw).decode()
    assert any(
        isinstance(m.get("content"), list) and any(
            isinstance(b, dict) and b.get("type") == "image_url" and b64 in b["image_url"]["url"]
            for b in m["content"]
        )
        for m in sent
    )


def test_export_includes_attachment_data(client):
    cid = client.post("/api/characters", json={"name": "Экспорт"}).json()["id"]
    sid = client.post(f"/api/sessions?character_id={cid}").json()["session_id"]
    raw = b"export-bytes" * 10
    _send_image(client, sid, raw)
    data = client.get(f"/api/sessions/{sid}/export").json()
    umsg = [m for m in data["messages"] if m["role"] == "user"][-1]
    att = umsg["attachments"][0]
    assert "blob_id" not in att  # чужой БД blob_id ни к чему
    assert att["data"].endswith(base64.b64encode(raw).decode())


def test_legacy_inline_data_migrates_on_startup(client):
    """Старые строки с инлайн-base64 переезжают в blobs при старте сервера."""
    cid = client.post("/api/characters", json={"name": "Легаси"}).json()["id"]
    sid = client.post(f"/api/sessions?character_id={cid}").json()["session_id"]
    raw = b"legacy-inline" * 40
    data_uri = "data:application/pdf;base64," + base64.b64encode(raw).decode()
    # Вставляем «старую» строку с data прямо в JSON (как писали прошлые версии).
    con = sqlite3.connect(_db_path())
    cur = con.execute(
        "INSERT INTO messages (session_id, role, content, attachments, swipes, active_swipe) "
        "VALUES (?, 'user', 'старое', ?, '[]', 0)",
        (sid, json.dumps([{"type": "document", "data": data_uri, "mime": "application/pdf", "name": "old.pdf"}])),
    )
    mid = cur.lastrowid
    con.commit()
    con.close()

    # Новый запуск приложения (lifespan) выполняет миграцию.
    from fastapi.testclient import TestClient

    from backend.main import app

    with TestClient(app) as c2:
        con = sqlite3.connect(_db_path())
        atts = json.loads(con.execute(
            "SELECT attachments FROM messages WHERE id=?", (mid,)
        ).fetchone()[0])
        con.close()
        assert atts[0].get("blob_id") and "data" not in atts[0]
        assert atts[0]["name"] == "old.pdf"
        # И эндпоинт отдаёт исходные байты после миграции.
        resp = c2.get(f"/api/messages/{mid}/att/0")
        assert resp.status_code == 200 and resp.content == raw


def test_audio_attachment_supports_range_requests(client):
    """
    Перемотка аудио и видео: браузер просит куски файла (Range). Без 206 любой
    прыжок по полосе начинал голосовое с нуля.
    """
    cid = client.post("/api/characters", json={"name": "Диапазон"}).json()["id"]
    sid = client.post(f"/api/sessions?character_id={cid}").json()["session_id"]
    raw = bytes(range(256)) * 40
    data_uri = "data:audio/wav;base64," + base64.b64encode(raw).decode()
    with patch("backend.llm_gateway.litellm.acompletion", new=_fake_acompletion):
        r = client.post(f"/api/sessions/{sid}/send", json={
            "content": "голосовое", "attachments": [
                {"type": "audio", "data": data_uri, "mime": "audio/wav", "name": "voice.wav"}
            ],
        })
        _wait_done(client, r.json()["job_id"])
    mid = [m for m in client.get(f"/api/sessions/{sid}/messages").json() if m["role"] == "user"][-1]["id"]
    url = f"/api/messages/{mid}/att/0"

    full = client.get(url)
    assert full.status_code == 200 and full.content == raw and full.headers["accept-ranges"] == "bytes"
    part = client.get(url, headers={"Range": "bytes=100-199"})
    assert part.status_code == 206 and part.content == raw[100:200]
    assert part.headers["content-range"] == f"bytes 100-199/{len(raw)}"
    tail = client.get(url, headers={"Range": "bytes=-16"})
    assert tail.status_code == 206 and tail.content == raw[-16:]
    open_end = client.get(url, headers={"Range": f"bytes={len(raw) - 10}-"})
    assert open_end.status_code == 206 and open_end.content == raw[-10:]
    assert client.get(url, headers={"Range": f"bytes={len(raw)}-"}).status_code == 416

    # Сообщение удалили — кэш раскодированных файлов не отдаёт его байты новому
    # сообщению с тем же id (SQLite их переиспользует).
    client.delete(f"/api/messages/{mid}")
    from backend import attachments
    assert not attachments._att_cache


def test_range_response_has_validator_and_304(client):
    """Ответ 206 без ETag браузер не кэширует — каждое открытие качало бы файлы заново."""
    cid = client.post("/api/characters", json={"name": "Валидатор"}).json()["id"]
    sid = client.post(f"/api/sessions?character_id={cid}").json()["session_id"]
    raw = b"etag-bytes" * 50
    with patch("backend.llm_gateway.litellm.acompletion", new=_fake_acompletion):
        r = client.post(f"/api/sessions/{sid}/send", json={"content": "a", "attachments": [
            {"type": "audio", "data": "data:audio/wav;base64," + base64.b64encode(raw).decode(),
             "mime": "audio/wav", "name": "a.wav"}]})
        _wait_done(client, r.json()["job_id"])
    mid = [m for m in client.get(f"/api/sessions/{sid}/messages").json() if m["role"] == "user"][-1]["id"]
    part = client.get(f"/api/messages/{mid}/att/0", headers={"Range": "bytes=0-"})
    etag = part.headers.get("etag")
    assert part.status_code == 206 and etag
    assert client.get(f"/api/messages/{mid}/att/0", headers={"If-None-Match": etag}).status_code == 304
    # If-Range с чужим валидатором — файл целиком, а не кусок другой версии.
    stale = client.get(f"/api/messages/{mid}/att/0", headers={"Range": "bytes=0-9", "If-Range": '"old"'})
    assert stale.status_code == 200 and stale.content == raw


def test_fork_gets_own_copy_of_files(client):
    """
    Ветка ссылалась на файл исходного чата: после его удаления файл пропадал, а
    освободившийся id получал файл ДРУГОГО чата — и ветка показывала чужое.
    """
    cid = client.post("/api/characters", json={"name": "Ветвистый"}).json()["id"]
    src = client.post(f"/api/sessions?character_id={cid}").json()["session_id"]
    raw = b"A-SECRET-" * 30
    _send_image(client, src, raw)
    umsg = [m for m in client.get(f"/api/sessions/{src}/messages").json() if m["role"] == "user"][-1]
    fork = client.post(f"/api/sessions/{src}/fork", json={"message_id": umsg["id"]}).json()["session_id"]
    fmsg = [m for m in client.get(f"/api/sessions/{fork}/messages").json() if m["role"] == "user"][-1]
    con = sqlite3.connect(_db_path())
    src_blob = json.loads(con.execute("SELECT attachments FROM messages WHERE id=?", (umsg["id"],)).fetchone()[0])[0]["blob_id"]
    fork_blob = json.loads(con.execute("SELECT attachments FROM messages WHERE id=?", (fmsg["id"],)).fetchone()[0])[0]["blob_id"]
    con.close()
    assert src_blob != fork_blob
    client.delete(f"/api/sessions/{src}")
    resp = client.get(f"/api/messages/{fmsg['id']}/att/0")
    assert resp.status_code == 200 and resp.content == raw


def test_shared_blob_links_split_on_startup(client):
    """Старые ветки (до 2.10.0) получают свои копии файлов; ссылка на чужой файл — убирается."""
    cid = client.post("/api/characters", json={"name": "Старая ветка"}).json()["id"]
    sid = client.post(f"/api/sessions?character_id={cid}").json()["session_id"]
    raw = b"OWNED-BY-SOURCE" * 20
    _send_image(client, sid, raw)
    src = [m for m in client.get(f"/api/sessions/{sid}/messages").json() if m["role"] == "user"][-1]
    con = sqlite3.connect(_db_path())
    atts, created = con.execute("SELECT attachments, created_at FROM messages WHERE id=?", (src["id"],)).fetchone()
    # «Ветка» по-старому: то же время и роль, ссылка на тот же blob.
    copy_id = con.execute(
        "INSERT INTO messages (session_id, role, content, attachments, swipes, active_swipe, created_at) "
        "VALUES (?, 'user', 'копия', ?, '[]', 0, ?)", (sid, atts, created)).lastrowid
    # Постороннее сообщение со ссылкой на тот же blob (как после переиспользования id).
    alien_id = con.execute(
        "INSERT INTO messages (session_id, role, content, attachments, swipes, active_swipe, created_at) "
        "VALUES (?, 'assistant', 'чужое', ?, '[]', 0, '2001-01-01 00:00:00')", (sid, atts)).lastrowid
    con.commit()
    con.close()

    from fastapi.testclient import TestClient

    from backend.main import app

    with TestClient(app) as c2:
        con = sqlite3.connect(_db_path())
        copy_atts = json.loads(con.execute("SELECT attachments FROM messages WHERE id=?", (copy_id,)).fetchone()[0])
        alien_atts = json.loads(con.execute("SELECT attachments FROM messages WHERE id=?", (alien_id,)).fetchone()[0])
        con.close()
        assert copy_atts[0]["blob_id"] != json.loads(atts)[0]["blob_id"]
        assert "blob_id" not in alien_atts[0]
        assert c2.get(f"/api/messages/{copy_id}/att/0").content == raw
        assert c2.get(f"/api/messages/{alien_id}/att/0").status_code == 404
