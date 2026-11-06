"""
Правка вложений готового сообщения (2.15.0): докинуть забытый файл или убрать
лишний, не пересоздавая сообщение.
"""
import sqlite3

from tests.test_attachment_blobs import _db_path, _send_image


def _chat_with_image(client):
    cid = client.post("/api/characters", json={"name": "Правка файлов"}).json()["id"]
    sid = client.post(f"/api/sessions?character_id={cid}").json()["session_id"]
    _send_image(client, sid, b"\x89PNG-first", text="альбом")
    msgs = client.get(f"/api/sessions/{sid}/messages").json()
    return sid, [m for m in msgs if m["role"] == "user"][-1]


def test_add_forgotten_song_to_sent_message(client):
    sid, umsg = _chat_with_image(client)
    mid = umsg["id"]
    r = client.post(f"/api/messages/{mid}/attachments",
                    files=[("files", ("песня.mp3", b"ID3-song-bytes", "audio/mpeg"))])
    assert r.status_code == 200, r.text
    atts = r.json()["attachments"]
    assert [a["name"] for a in atts] == ["p.png", "песня.mp3"]
    assert atts[1]["type"] == "audio" and atts[1]["v"]
    # Байты нового файла отдаются по своему индексу; старый на месте.
    assert client.get(f"/api/messages/{mid}/att/1").content == b"ID3-song-bytes"
    assert client.get(f"/api/messages/{mid}/att/0").content == b"\x89PNG-first"
    # И в списке сообщений чата.
    msgs = client.get(f"/api/sessions/{sid}/messages").json()
    assert [a["name"] for a in next(m for m in msgs if m["id"] == mid)["attachments"]] == ["p.png", "песня.mp3"]


def test_remove_attachment_checks_version_and_drops_blob(client):
    _, umsg = _chat_with_image(client)
    mid = umsg["id"]
    client.post(f"/api/messages/{mid}/attachments", files=[("files", ("doc.txt", b"text", "text/plain"))])
    first_v = umsg["attachments"][0]["v"]
    # Устаревшая версия (индекс указывает на другой файл) — 409, ничего не трогаем.
    bad = client.post(f"/api/messages/{mid}/attachments", data={"remove": '[{"idx": 0, "v": 999999}]'})
    assert bad.status_code == 409
    r = client.post(f"/api/messages/{mid}/attachments", data={"remove": f'[{{"idx": 0, "v": {first_v}}}]'})
    assert r.status_code == 200, r.text
    assert [a["name"] for a in r.json()["attachments"]] == ["doc.txt"]
    assert client.get(f"/api/messages/{mid}/att/0").content == b"text"
    con = sqlite3.connect(_db_path())
    assert con.execute("SELECT COUNT(*) FROM attachment_blobs WHERE id=?", (first_v,)).fetchone()[0] == 0


def test_attachment_edit_requires_existing_message(client):
    assert client.post("/api/messages/987654/attachments",
                       files=[("files", ("a.txt", b"a", "text/plain"))]).status_code == 404
