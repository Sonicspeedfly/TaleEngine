"""
Личные сообщения и пересылка (backend/direct.py, 2.29.0).

  * писать можно только другу; чужая переписка закрыта;
  * непрочитанные считаются, открытие переписки их читает;
  * пересылка — снимок реплики (кто сказал, из какого чата), но только той,
    к чату которой есть доступ у пересылающего;
  * событие шины «dm» видят только двое из переписки.
"""
from types import SimpleNamespace

from backend import direct


def test_visible_to():
    ev = {"type": "dm", "users": [1, 2]}
    assert direct.visible_to(ev, SimpleNamespace(id=1)) is True
    assert direct.visible_to(ev, SimpleNamespace(id=3)) is False
    assert direct.visible_to(ev, None) is False
    assert direct.visible_to({"type": "turn_done"}, SimpleNamespace(id=3)) is None
    assert direct.pair_of(7, 3) == direct.pair_of(3, 7) == "3:7"


def test_direct_messages_and_forward(client):
    a = client.post("/api/auth/register", json={"username": "дм_анна", "password": "pw"}).json()
    b = client.post("/api/auth/register", json={"username": "дм_боря", "password": "pw"}).json()
    c = client.post("/api/auth/register", json={"username": "дм_чужой", "password": "pw"}).json()
    ah, bh, ch = ({"X-User-Token": x["token"]} for x in (a, b, c))
    aid, bid = a["user"]["id"], b["user"]["id"]
    client.put("/api/admin/security", json={"accounts_enabled": True, "admin_password": "dm_pw"})
    try:
        # Не друзья — писать нельзя.
        assert client.post(f"/api/dm/{bid}", json={"content": "привет"}, headers=ah).status_code == 403
        client.post("/api/friends/add", json={"username": "дм_боря"}, headers=ah)
        inc = client.get("/api/friends", headers=bh).json()["incoming"]
        client.post(f"/api/friends/{inc[0]['friendship_id']}/accept", headers=bh)

        assert client.post(f"/api/dm/{bid}", json={"content": "  "}, headers=ah).status_code == 422
        m1 = client.post(f"/api/dm/{bid}", json={"content": "привет, Боря!"}, headers=ah).json()
        assert m1["sender_id"] == aid and m1["read"] is False
        th = client.get("/api/dm", headers=bh).json()
        assert th["unread"] == 1 and th["threads"][0]["user"]["name"] == "дм_анна"
        assert th["threads"][0]["last"]["content"] == "привет, Боря!"
        msgs = client.get(f"/api/dm/{aid}", headers=bh).json()["messages"]
        assert [m["content"] for m in msgs] == ["привет, Боря!"]
        assert client.get("/api/dm", headers=bh).json()["unread"] == 0
        assert client.get(f"/api/dm/{bid}", headers=ah).json()["messages"][0]["read"] is True
        # Больше страницы непрочитанных — открытие читает все.
        for i in range(55):
            client.post(f"/api/dm/{bid}", json={"content": f"п{i}"}, headers=ah)
        assert client.get("/api/dm", headers=bh).json()["unread"] == 55
        page = client.get(f"/api/dm/{aid}", headers=bh).json()
        assert len(page["messages"]) == 50 and page["more"] is True
        assert client.get("/api/dm", headers=bh).json()["unread"] == 0
        older = client.get(f"/api/dm/{aid}?before={page['messages'][0]['id']}", headers=bh).json()
        assert older["messages"][-1]["id"] < page["messages"][0]["id"]
        client.post(f"/api/dm/{bid}", json={"content": "ещё"}, headers=ah)
        assert client.post(f"/api/dm/{aid}/read", headers=bh).json() == {"read": 1}
        assert client.post(f"/api/dm/{aid}/read", headers=ch).status_code == 403
        # Посторонний чужую переписку не читает.
        assert client.get(f"/api/dm/{aid}", headers=ch).status_code == 403

        # Пересылка реплики из своего чата.
        cid = client.post("/api/characters", json={"name": "ДМ-Джеми", "first_message": "Я так рада тебя видеть!"},
                          headers=ah).json()["id"]
        sid = client.post(f"/api/sessions?character_id={cid}", headers=ah).json()["session_id"]
        rows = client.get(f"/api/sessions/{sid}/messages", headers=ah).json()
        src = next(r for r in rows if r["content"] == "Я так рада тебя видеть!")
        fw = client.post(f"/api/dm/{bid}", json={"content": "смотри, что она сказала",
                                                 "forward_message_id": src["id"]}, headers=ah).json()
        assert fw["forward"]["speaker"] == "ДМ-Джеми" and fw["forward"]["message_id"] == src["id"]
        assert fw["forward"]["content"] == "Я так рада тебя видеть!" and fw["forward"]["session_id"] == sid
        # Боря видит снимок, но переслать реплику чужого (не открытого ему) чата не может.
        got = client.get(f"/api/dm/{aid}", headers=bh).json()["messages"][-1]
        assert got["forward"]["content"] == "Я так рада тебя видеть!"
        assert client.post(f"/api/dm/{aid}", json={"forward_message_id": src["id"]},
                           headers=bh).status_code == 404
        # Чат открыли Боре — теперь можно.
        client.post(f"/api/sessions/{sid}/share", json={"username": "дм_боря"}, headers=ah)
        ok = client.post(f"/api/dm/{aid}", json={"forward_message_id": src["id"]}, headers=bh)
        assert ok.status_code == 200 and ok.json()["content"] == ""
        # Чужой чат не поправить по id (раньше PATCH не проверял доступ).
        csid = client.post(f"/api/sessions?character_id={cid}", headers=ah).json()["session_id"]
        assert client.patch(f"/api/sessions/{csid}", json={"timezone": "UTC+5"}, headers=ch).status_code == 403
    finally:
        client.put("/api/admin/security", json={"accounts_enabled": False, "admin_password": ""},
                   headers={**ah, "X-Admin-Password": "dm_pw"})


def test_dm_disabled_without_accounts(client):
    assert client.get("/api/dm").json() == {"enabled": False, "threads": [], "unread": 0}
    assert client.post("/api/dm/1", json={"content": "x"}).status_code == 409
