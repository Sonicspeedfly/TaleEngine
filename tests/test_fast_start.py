"""2.18.0: карточка одного чата — по ней страница открывает последний чат, не дожидаясь списков."""


def test_session_card_matches_list_row(client):
    cid = client.post("/api/characters", json={"name": "Быстрый"}).json()["id"]
    sid = client.post(f"/api/sessions?character_id={cid}").json()["session_id"]
    client.patch(f"/api/sessions/{sid}", json={"author_note": "Пиши мрачно", "title": "Пещера"})
    card = client.get(f"/api/sessions/{sid}/card").json()
    row = next(s for s in client.get("/api/sessions").json() if s["id"] == sid)
    assert card == row
    assert card["author_note"] == "Пиши мрачно" and card["character_name"] == "Быстрый"


def test_session_card_unknown_or_group_is_404(client):
    assert client.get("/api/sessions/987654/card").status_code == 404
    cid = client.post("/api/characters", json={"name": "Группа"}).json()["id"]
    g = client.post("/api/groups", json={"title": "Г", "character_ids": [cid]})
    if g.status_code == 200:
        gid = g.json().get("id") or g.json().get("session_id")
        if gid:
            assert client.get(f"/api/sessions/{gid}/card").status_code == 404
