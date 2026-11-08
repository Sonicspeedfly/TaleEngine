"""2.20.0: файл, уже лежащий в черновике, уходит с сообщением ссылкой — без второй загрузки."""
import base64
from unittest.mock import patch

from tests.test_attachment_blobs import _fake_acompletion, _wait_done


def _chat(client, name="Ссылка"):
    cid = client.post("/api/characters", json={"name": name}).json()["id"]
    return client.post(f"/api/sessions?character_id={cid}").json()["session_id"]


def _draft_file(client, sid, raw=b"ID3-song", name="песня.mp3", mime="audio/mpeg"):
    r = client.post(f"/api/sessions/{sid}/draft/files", data={"kind": "audio"}, files={"file": (name, raw, mime)})
    assert r.status_code == 200, r.text
    return r.json()


def test_send_with_draft_ref_stores_file_and_clears_draft(client):
    sid = _chat(client)
    f = _draft_file(client, sid)
    d = client.get(f"/api/sessions/{sid}/draft").json()
    with patch("backend.llm_gateway.litellm.acompletion", new=_fake_acompletion):
        r = client.post(f"/api/sessions/{sid}/send", json={
            "content": "слушай", "draft_version": d["version"], "draft_fids": [f["fid"]],
            "attachments": [{"type": "audio", "mime": "audio/mpeg", "name": "песня.mp3", "draft_fid": f["fid"]}],
        })
        assert r.status_code == 200, r.text
        _wait_done(client, r.json()["job_id"])
    msgs = client.get(f"/api/sessions/{sid}/messages").json()
    umsg = [m for m in msgs if m["role"] == "user"][-1]
    assert [a["name"] for a in umsg["attachments"]] == ["песня.mp3"]
    assert client.get(f"/api/messages/{umsg['id']}/att/0").content == b"ID3-song"
    # Черновик очищен — файл ушёл с сообщением, а не остался висеть.
    assert client.get(f"/api/sessions/{sid}/draft").json()["files"] == []


def test_draft_ref_of_other_chat_is_rejected(client):
    a, b = _chat(client, "А"), _chat(client, "Б")
    f = _draft_file(client, a)
    r = client.post(f"/api/sessions/{b}/send", json={
        "content": "чужое", "attachments": [{"type": "audio", "name": "x", "draft_fid": f["fid"]}]})
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "draft_file_missing" and r.json()["detail"]["missing"] == [f["fid"]]
    # Пустое вложение без ссылки — тоже ошибка, а не «файл» без данных.
    r = client.post(f"/api/sessions/{b}/send", json={"content": "пусто", "attachments": [{"type": "image"}]})
    assert r.status_code == 422


def test_send_form_mixes_draft_ref_and_upload(client):
    sid = _chat(client, "Смесь")
    f = _draft_file(client, sid, raw=b"draft-bytes", name="из-черновика.txt", mime="text/plain")
    import json
    payload = {"content": "два файла", "attachments": [
        {"type": "document", "name": "из-черновика.txt", "mime": "text/plain", "draft_fid": f["fid"]},
        {"type": "document", "name": "большой.txt", "mime": "text/plain", "file_index": 0},
    ]}
    with patch("backend.llm_gateway.litellm.acompletion", new=_fake_acompletion):
        r = client.post(f"/api/sessions/{sid}/send_form", data={"payload": json.dumps(payload)},
                        files=[("files", ("большой.txt", b"multipart-bytes", "text/plain"))])
        assert r.status_code == 200, r.text
        _wait_done(client, r.json()["job_id"])
    umsg = [m for m in client.get(f"/api/sessions/{sid}/messages").json() if m["role"] == "user"][-1]
    assert [a["name"] for a in umsg["attachments"]] == ["из-черновика.txt", "большой.txt"]
    assert client.get(f"/api/messages/{umsg['id']}/att/0").content == b"draft-bytes"
    assert client.get(f"/api/messages/{umsg['id']}/att/1").content == b"multipart-bytes"
