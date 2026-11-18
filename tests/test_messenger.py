"""
Общий чат как мессенджер (2.27.0, backend/messenger.py).

  * у реплики человека есть автор: в ленте имя, в истории модели «Имя: текст»;
  * реплику людям персонаж не отвечает — она только публикуется (posted);
  * реплику персонажу он отвечает; «позвать» отвечает на реплику людям;
  * режим «always» (только владелец) — ответ на каждую реплику, как раньше;
  * правила без модели: имя персонажа → ему, имя другого человека → людям.
"""
from unittest.mock import patch

from backend import messenger


def _chunk(text):
    class _Delta:
        content = text

    class _Choice:
        delta = _Delta()

    class _Chunk:
        choices = [_Choice()]

    return _Chunk()


def _llm(state: dict, seen: list):
    async def fake(*args, **kwargs):
        messages = kwargs.get("messages") or []
        is_decide = bool(messages) and messages[0].get("content") == messenger.DECIDE_PROMPT
        seen.append(("decide" if is_decide else "chat", messages))
        text = state.get("decide", "AI") if is_decide else "Ответ персонажа."

        async def gen():
            yield _chunk(text)
        return gen()
    return fake


def _send(ws, payload):
    ws.send_json(payload)
    events = []
    for _ in range(200):
        ev = ws.receive_json()
        events.append(ev)
        if ev["type"] in ("done", "error"):
            break
    return events


def _types(events):
    return [e["type"] for e in events]


# ---------------------------------------------------------------- правила
def test_quick_rules():
    people = [{"id": 1, "name": "Олег"}, {"id": 2, "name": "Марина"}]
    assert messenger.quick_decide("Джеми, привет!", 1, ["Джеми"], people) == messenger.AI
    assert messenger.quick_decide("+Джеми расскажи", 1, ["Джеми"], people) == messenger.AI
    assert messenger.quick_decide("Марина, ты тут?", 1, ["Джеми"], people) == messenger.PEOPLE
    assert messenger.quick_decide("@марина глянь", 1, ["Джеми"], people) == messenger.PEOPLE
    # Своё имя в тексте — не обращение к другому.
    assert messenger.quick_decide("Олег на связи", 1, ["Джеми"], people) is None
    assert messenger.quick_decide("ну что думаете?", 1, ["Джеми"], people) is None
    # Имя персонажа важнее имени человека.
    assert messenger.quick_decide("Марина, спроси у Джеми", 1, ["Джеми"], people) == messenger.AI


def test_parse_answer():
    assert messenger.parse_answer("AI") == messenger.AI
    assert messenger.parse_answer(" people.") == messenger.PEOPLE
    assert messenger.parse_answer("Ответ: PEOPLE") == messenger.PEOPLE
    assert messenger.parse_answer("AI или PEOPLE") is None
    assert messenger.parse_answer("") is None


def test_history_labels():
    from backend.horae_memory import messages_to_history

    class M:
        def __init__(self, role, content, author_id=None):
            self.role, self.content, self.author_id, self.attachments = role, content, author_id, []

    hist = messages_to_history([M("user", "привет", 7), M("assistant", "здравствуй"), M("user", "ещё", None)],
                               labels={7: "Олег"})
    assert [h["content"] for h in hist] == ["Олег: привет", "здравствуй", "ещё"]


# ---------------------------------------------------------------- общий чат
def test_shared_chat_messenger_flow(client):
    a = client.post("/api/auth/register", json={"username": "мсгвладелец", "password": "pw"}).json()
    b = client.post("/api/auth/register", json={"username": "Борис", "password": "pw"}).json()
    ah, bh = {"X-User-Token": a["token"]}, {"X-User-Token": b["token"]}
    client.put("/api/admin/security", json={"accounts_enabled": True, "admin_password": "msg_pw"})
    try:
        cid = client.post("/api/characters", json={"name": "Джеми"}, headers=ah).json()["id"]
        sid = client.post(f"/api/sessions?character_id={cid}", headers=ah).json()["session_id"]
        state, seen = {"decide": "AI"}, []
        fake = _llm(state, seen)

        # Пока чат личный — каждая реплика ход модели, без диспетчера.
        with patch("backend.llm_gateway.litellm.acompletion", new=fake):
            with client.websocket_connect(f"/ws/chat/{sid}?token={a['token']}") as ws:
                ev = _send(ws, {"type": "user_message", "content": "просто привет"})
        assert "job" in _types(ev) and "posted" not in _types(ev)
        assert not [s for s in seen if s[0] == "decide"]

        client.post("/api/friends/add", json={"username": "Борис"}, headers=ah)
        inc = client.get("/api/friends", headers=bh).json()["incoming"]
        client.post(f"/api/friends/{inc[0]['friendship_id']}/accept", headers=bh)
        client.post(f"/api/sessions/{sid}/share", json={"username": "Борис"}, headers=ah)

        def count():
            return len(client.get(f"/api/sessions/{sid}/messages", headers=ah).json())

        with patch("backend.llm_gateway.litellm.acompletion", new=fake):
            with client.websocket_connect(f"/ws/chat/{sid}?token={b['token']}") as ws:
                # Имя другого человека — людям: модель не зовут вовсе.
                n0, seen[:] = count(), []
                ev = _send(ws, {"type": "user_message", "content": "мсгвладелец, ты тут?"})
                posted = next(e for e in ev if e["type"] == "posted")
                assert posted["address"] == "people" and posted["decided_by"] == "rule"
                assert "job" not in _types(ev) and seen == []
                assert count() == n0 + 1

                # Неясно кому — решает быстрая модель: людям.
                state["decide"] = "PEOPLE"
                ev = _send(ws, {"type": "user_message", "content": "ахаха согласен"})
                posted = next(e for e in ev if e["type"] == "posted")
                assert posted["address"] == "people" and posted["decided_by"] == "model"
                assert [s[0] for s in seen] == ["decide"]
                decide_user = seen[0][1][1]["content"]
                assert "Борис: ахаха согласен" in decide_user and "Джеми" in decide_user

                # Персонажу — ход; модель видит подписи людей.
                state["decide"], seen[:] = "AI", []
                ev = _send(ws, {"type": "user_message", "content": "а что скажешь ты?"})
                posted = next(e for e in ev if e["type"] == "posted")
                assert posted["address"] == "ai" and posted.get("job_id")
                assert "job" in _types(ev) and ev[-1]["type"] == "done"
                chat = [s[1] for s in seen if s[0] == "chat"][-1]
                texts = [m["content"] if isinstance(m["content"], str) else str(m["content"]) for m in chat]
                assert any("Борис: а что скажешь ты?" in t for t in texts)
                assert any("Борис: ахаха согласен" in t for t in texts)

                # Явный выбор в окне ввода сильнее модели.
                seen[:] = []
                ev = _send(ws, {"type": "user_message", "content": "это не тебе", "address": "people"})
                assert next(e for e in ev if e["type"] == "posted")["decided_by"] == "explicit"
                assert seen == []

                # «Позвать персонажа» — ответ на реплику людям.
                rows = client.get(f"/api/sessions/{sid}/messages", headers=bh).json()
                target = next(r for r in rows if r["content"] == "это не тебе")
                assert target["address"] == "people"
                assert target["author"]["name"] == "Борис"
                ev = _send(ws, {"type": "answer", "message_id": target["id"]})
                assert "job" in _types(ev)
                chat = [s[1] for s in seen if s[0] == "chat"][-1]
                assert "Борис: это не тебе" in str(chat[-1]["content"])

        rows = client.get(f"/api/sessions/{sid}/messages", headers=ah).json()
        assert next(r for r in rows if r["content"] == "это не тебе")["address"] == "ai"
        assert rows[-1]["role"] == "assistant"
        own = next(r for r in rows if r["content"] == "просто привет")
        assert own["author"]["name"] == "мсгвладелец" and own["address"] is None

        # Режим «на каждую реплику» меняет только владелец.
        assert client.patch(f"/api/sessions/{sid}", json={"reply_mode": "always"},
                            headers=bh).status_code == 403
        assert client.patch(f"/api/sessions/{sid}", json={"reply_mode": "always"},
                            headers=ah).status_code == 200
        seen[:] = []
        with patch("backend.llm_gateway.litellm.acompletion", new=fake):
            with client.websocket_connect(f"/ws/chat/{sid}?token={b['token']}") as ws:
                ev = _send(ws, {"type": "user_message", "content": "мсгвладелец, глянь"})
        assert "job" in _types(ev) and "posted" not in _types(ev)
        assert [s[0] for s in seen] == ["chat"]
        assert "Борис: мсгвладелец, глянь" in str(seen[0][1][-1]["content"])
    finally:
        client.put("/api/admin/security", json={"accounts_enabled": False, "admin_password": ""},
                   headers={**ah, "X-Admin-Password": "msg_pw"})
