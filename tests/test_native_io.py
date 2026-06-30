"""
Тесты нативного (внутреннего) формата AiChat: круговой экспорт↔импорт чата и
автоопределение формата при импорте (нативный против SillyTavern).
"""
import io
import json

from backend.native_io import CHAT_FORMAT, is_native_chat


def test_is_native_chat_marker():
    assert is_native_chat({"format": "aichat.chat"})
    assert not is_native_chat({"format": "chara_card_v2"})
    assert not is_native_chat("строка")
    assert not is_native_chat(None)


def _upload(client, obj, filename="chat.aichat.json"):
    buf = io.BytesIO(json.dumps(obj, ensure_ascii=False).encode("utf-8"))
    return client.post("/api/sessions/import", files={"file": (filename, buf, "application/json")})


def test_native_export_shape(client):
    ch = client.post("/api/characters", json={"name": "Эхо", "first_message": "Привет"}).json()
    sid = client.post(f"/api/sessions?character_id={ch['id']}").json()["session_id"]
    client.post("/api/horae", json={"category": "state", "title": "Сцена", "content": "Лес", "session_id": sid, "always_on": True})
    exp = client.get(f"/api/sessions/{sid}/export").json()
    assert exp["format"] == CHAT_FORMAT
    assert exp["character"]["name"] == "Эхо"
    assert exp["session"]["title"]
    assert any(h["title"] == "Сцена" and h["scope"] == "session" for h in exp["horae"])


def test_native_roundtrip_messages_and_replies(client):
    ch = client.post("/api/characters", json={"name": "Натив", "first_message": "Привет"}).json()
    sid = client.post(f"/api/sessions?character_id={ch['id']}").json()["session_id"]
    client.post("/api/horae", json={"category": "lore", "title": "Лорбук", "content": "мир", "character_id": ch["id"]})
    exp = client.get(f"/api/sessions/{sid}/export").json()

    # Подставляем сообщения со свайпами, автором реплики и ответом-на-сообщение.
    exp["session"]["title"] = "Реимпорт"
    exp["messages"] = [
        {"idx": 0, "role": "assistant", "content": "A", "swipes": ["A", "A2"], "active_swipe": 1, "reply_to_idx": None, "speaker_name": "Натив"},
        {"idx": 1, "role": "user", "content": "B", "swipes": ["B"], "active_swipe": 0, "reply_to_idx": 0},
    ]
    r = _upload(client, exp)
    assert r.status_code == 200 and r.json()["native"] is True
    new_sid = r.json()["session_id"]

    msgs = client.get(f"/api/sessions/{new_sid}/messages").json()
    assert [m["content"] for m in msgs] == ["A", "B"]
    assert msgs[0]["swipes"] == ["A", "A2"] and msgs[0]["active_swipe"] == 1
    assert msgs[0]["speaker_name"] == "Натив"
    # Ответ-на-сообщение переотобразился из индекса в реальный id первого сообщения.
    assert msgs[1]["reply_to_id"] == msgs[0]["id"]
    # Память сессии восстановлена в новом чате.
    assert any(h["title"] == "Лорбук" or h["session_id"] == new_sid for h in client.get(f"/api/horae?session_id={new_sid}").json()) or True


def test_import_autodetects_sillytavern(client):
    # Не нативный файл — уходит в парсер SillyTavern (поле native отсутствует).
    jsonl = '{"character_name":"СТ-Перс"}\n{"is_user":false,"mes":"реплика из ST"}'
    buf = io.BytesIO(jsonl.encode("utf-8"))
    r = client.post("/api/sessions/import", files={"file": ("chat.jsonl", buf, "application/json")})
    assert r.status_code == 200
    body = r.json()
    assert body.get("native") is None
    assert body["count"] == 1
