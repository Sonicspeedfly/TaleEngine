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


def _send_ws(client, sid, payload):
    with patch("backend.llm_gateway.litellm.acompletion", new=_fake_llm):
        with client.websocket_connect(f"/ws/chat/{sid}") as ws:
            ws.send_json({"type": "user_message", "client_id": "c1", **payload})
            for _ in range(100):
                if ws.receive_json()["type"] == "done":
                    break


def test_send_clears_only_what_was_sent(client):
    """Пока ход стартовал, человек начал следующее сообщение — его не стираем."""
    sid = _chat(client)
    client.put(f"/api/sessions/{sid}/draft", json={"text": "первое"})
    f1 = client.post(f"/api/sessions/{sid}/draft/files", files={"file": ("a.txt", b"a", "text/plain")}).json()
    sent_version = f1["version"]
    # После «Отправить» (до старта хода на сервере): новый текст и новый файл.
    client.put(f"/api/sessions/{sid}/draft", json={"text": "второе", "files": [f1["fid"]], "base_version": sent_version})
    f2 = client.post(f"/api/sessions/{sid}/draft/files", files={"file": ("b.txt", b"b", "text/plain")}).json()
    _send_ws(client, sid, {"content": "первое", "draft_version": sent_version, "draft_fids": [f1["fid"]]})
    d = client.get(f"/api/sessions/{sid}/draft").json()
    assert d["text"] == "второе"
    assert [f["fid"] for f in d["files"]] == [f2["fid"]]


def test_send_clears_text_saved_just_before_send(client):
    """Сохранение, ушедшее за миг до «Отправить», двинуло версию — текст тот же, его чистим."""
    sid = _chat(client)
    v = client.put(f"/api/sessions/{sid}/draft", json={"text": "привет"}).json()["version"]
    client.put(f"/api/sessions/{sid}/draft", json={"text": "привет, мир  ", "base_version": v})
    _send_ws(client, sid, {"content": "привет, мир", "draft_version": v, "draft_fids": []})
    assert client.get(f"/api/sessions/{sid}/draft").json()["text"] == ""


def test_draft_file_is_not_served_as_active_content(client):
    sid = _chat(client)
    up = client.post(f"/api/sessions/{sid}/draft/files",
                     files={"file": ("x.html", b"<script>alert(1)</script>", "text/html")}).json()
    r = client.get(f"/api/sessions/{sid}/draft/files/{up['fid']}")
    assert r.headers["content-type"] == "application/octet-stream"
    assert r.headers["content-disposition"].startswith("attachment")
    assert r.headers["x-content-type-options"] == "nosniff" and "sandbox" in r.headers["content-security-policy"]
    img = client.post(f"/api/sessions/{sid}/draft/files", files={"file": ("k.png", b"png", "image/png")}).json()
    r = client.get(f"/api/sessions/{sid}/draft/files/{img['fid']}")
    assert r.headers["content-type"] == "image/png" and r.headers["content-disposition"].startswith("inline")


def test_concurrent_saves_with_same_base_do_not_both_win(client):
    """Два устройства сохраняют разом с одной базой: одно проходит, другое — 409 (сольёт)."""
    import asyncio

    from backend import drafts
    from backend.database import AsyncSessionLocal

    sid = _chat(client)
    v = client.put(f"/api/sessions/{sid}/draft", json={"text": "база"}).json()["version"]

    async def both():
        async def one(text):
            async with AsyncSessionLocal() as db:
                ok, _ = await drafts.save(db, sid, 0, text=text, reply_to_id=None, files=[], base_version=v)
                return ok
        return await asyncio.gather(one("с телефона"), one("с компьютера"))

    assert sorted(client.portal.call(both)) == [False, True]
    d = client.get(f"/api/sessions/{sid}/draft").json()
    assert d["version"] == v + 1 and d["text"] in ("с телефона", "с компьютера")
