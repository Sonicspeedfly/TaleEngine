"""
Черновики сообщений, привязанные к чату (2.14.0): у каждого чата свой черновик,
он лежит на сервере (продолжить на другом устройстве) и очищается при отправке.
"""
from unittest.mock import patch

from backend.config import settings


def _chunk(text):
    class _Delta:
        content = text
        reasoning_content = None

    class _Choice:
        delta = _Delta()
        finish_reason = None

    class _Chunk:
        choices = [_Choice()]

    return _Chunk()


async def _fake_llm(*args, **kwargs):
    async def gen():
        yield _chunk("Ответ.")
    return gen()


def _chat(client, name="Черновик"):
    cid = client.post("/api/characters", json={"name": name}).json()["id"]
    return client.post(f"/api/sessions?character_id={cid}").json()["session_id"]


def test_draft_is_per_chat_and_versioned(client):
    a, b = _chat(client), _chat(client, "Другой")
    assert client.get(f"/api/sessions/{a}/draft").json() == {
        "text": "", "reply_to_id": None, "files": [], "version": 0, "file_max_mb": 25}
    r = client.put(f"/api/sessions/{a}/draft", json={"text": "недописанное", "base_version": 0})
    assert r.status_code == 200 and r.json()["version"] == 1
    # В другом чате — свой (пустой) черновик: недописанное не переезжает.
    assert client.get(f"/api/sessions/{b}/draft").json()["text"] == ""
    assert client.get(f"/api/sessions/{a}/draft").json()["text"] == "недописанное"
    # Правка с другого устройства на старой версии не затирает свежую.
    client.put(f"/api/sessions/{a}/draft", json={"text": "с телефона", "base_version": 1})
    stale = client.put(f"/api/sessions/{a}/draft", json={"text": "со старой вкладки", "base_version": 1})
    assert stale.status_code == 409 and stale.json()["draft"]["text"] == "с телефона"


def test_draft_files_roundtrip_and_removal(client):
    sid = _chat(client)
    up = client.post(f"/api/sessions/{sid}/draft/files", data={"kind": "image"},
                     files={"file": ("кот.png", b"\x89PNG-bytes", "image/png")})
    assert up.status_code == 200, up.text
    fid = up.json()["fid"]
    d = client.get(f"/api/sessions/{sid}/draft").json()
    assert [f["name"] for f in d["files"]] == ["кот.png"] and d["files"][0]["type"] == "image"
    got = client.get(f"/api/sessions/{sid}/draft/files/{fid}")
    assert got.status_code == 200 and got.content == b"\x89PNG-bytes" and got.headers["content-type"] == "image/png"
    # Файл убрали из черновика — он удаляется и с сервера.
    client.put(f"/api/sessions/{sid}/draft", json={"text": "", "files": [], "base_version": d["version"]})
    assert client.get(f"/api/sessions/{sid}/draft/files/{fid}").status_code == 404
    # Номер удалённого файла не достаётся новому: вкладка, помнящая старый
    # файл под этим номером, не примет за него чужой.
    again = client.post(f"/api/sessions/{sid}/draft/files", files={"file": ("пёс.png", b"dog", "image/png")})
    assert again.json()["fid"] > fid


def test_too_big_draft_file_stays_local(client, monkeypatch):
    sid = _chat(client)
    monkeypatch.setattr(settings, "DRAFT_FILE_MAX_MB", 0)
    r = client.post(f"/api/sessions/{sid}/draft/files", files={"file": ("big.bin", b"x" * 10, "application/octet-stream")})
    assert r.status_code == 413


def test_sending_clears_draft(client):
    sid = _chat(client)
    client.put(f"/api/sessions/{sid}/draft", json={"text": "привет"})
    client.post(f"/api/sessions/{sid}/draft/files", files={"file": ("a.txt", b"hello", "text/plain")})
    with patch("backend.llm_gateway.litellm.acompletion", new=_fake_llm):
        with client.websocket_connect(f"/ws/chat/{sid}") as ws:
            ws.send_json({"type": "user_message", "content": "привет", "client_id": "c1"})
            for _ in range(100):
                if ws.receive_json()["type"] == "done":
                    break
    d = client.get(f"/api/sessions/{sid}/draft").json()
    assert d["text"] == "" and d["files"] == [] and d["version"] >= 3


def test_deleting_chat_removes_its_draft(client):
    sid = _chat(client)
    client.put(f"/api/sessions/{sid}/draft", json={"text": "черновик удалённого"})
    client.delete(f"/api/sessions/{sid}")
    new = _chat(client, "Новый")
    # id чата SQLite может отдать новому — чужой черновик там всплывать не должен.
    assert client.get(f"/api/sessions/{new}/draft").json()["text"] == ""
