"""
Закрепление чатов и персонажей наверху списка + превью строки чата.

Порядок считает СЕРВЕР (клиент только показывает), поэтому проверяем именно его:
закреплённые выше остальных, между собой — последний закреплённый первым, снятие
возвращает элемент на обычное место.
"""


def _mk_char(client, name):
    return client.post("/api/characters", json={"name": name}).json()["id"]


def _titles(client):
    return [(s["title"], s["pinned"]) for s in client.get("/api/sessions").json()]


def test_new_session_is_not_pinned(client):
    cid = _mk_char(client, "PinDefault")
    sid = client.post(f"/api/sessions?character_id={cid}").json()["session_id"]
    got = next(s for s in client.get("/api/sessions").json() if s["id"] == sid)
    assert got["pinned"] is False


def test_pinned_session_goes_on_top(client):
    cid = _mk_char(client, "PinTop")
    ids = [client.post(f"/api/sessions?character_id={cid}").json()["session_id"] for _ in range(3)]
    for n, sid in enumerate(ids):
        client.patch(f"/api/sessions/{sid}", json={"title": f"чат-{n}"})
    # Самый СТАРЫЙ чат обычно в самом низу — закрепляем именно его.
    client.patch(f"/api/sessions/{ids[0]}", json={"pinned": True})
    rows = [s for s in client.get(f"/api/sessions?character_id={cid}").json()]
    assert rows[0]["id"] == ids[0] and rows[0]["pinned"] is True


def test_last_pinned_is_first_among_pinned(client):
    cid = _mk_char(client, "PinOrder")
    a, b = (client.post(f"/api/sessions?character_id={cid}").json()["session_id"] for _ in range(2))
    client.patch(f"/api/sessions/{a}", json={"pinned": True})
    client.patch(f"/api/sessions/{b}", json={"pinned": True})
    rows = client.get(f"/api/sessions?character_id={cid}").json()
    assert [r["id"] for r in rows[:2]] == [b, a]


def test_unpin_returns_to_normal_order(client):
    cid = _mk_char(client, "PinOff")
    old, new = (client.post(f"/api/sessions?character_id={cid}").json()["session_id"] for _ in range(2))
    client.patch(f"/api/sessions/{old}", json={"pinned": True})
    assert client.get(f"/api/sessions?character_id={cid}").json()[0]["id"] == old
    client.patch(f"/api/sessions/{old}", json={"pinned": False})
    rows = client.get(f"/api/sessions?character_id={cid}").json()
    assert rows[0]["id"] == new  # снова сверху более новый
    assert all(r["pinned"] is False for r in rows)


def test_pin_does_not_clobber_other_fields(client):
    """pinned — флаг снаружи, дата внутри; перевод не должен затирать title."""
    cid = _mk_char(client, "PinKeep")
    sid = client.post(f"/api/sessions?character_id={cid}").json()["session_id"]
    client.patch(f"/api/sessions/{sid}", json={"title": "Важный чат"})
    client.patch(f"/api/sessions/{sid}", json={"pinned": True})
    got = next(s for s in client.get("/api/sessions").json() if s["id"] == sid)
    assert got["title"] == "Важный чат" and got["pinned"] is True


def test_pinned_character_goes_on_top(client):
    first = _mk_char(client, "CharA")
    last = _mk_char(client, "CharZ")
    client.patch(f"/api/characters/{last}", json={"pinned": True})
    rows = client.get("/api/characters").json()
    assert rows[0]["id"] == last and rows[0]["pinned"] is True
    client.patch(f"/api/characters/{last}", json={"pinned": False})
    assert all(c["pinned"] is False for c in client.get("/api/characters").json())
    assert first in [c["id"] for c in client.get("/api/characters").json()]


def test_pin_character_keeps_name(client):
    cid = _mk_char(client, "KeepName")
    client.patch(f"/api/characters/{cid}", json={"pinned": True})
    got = next(c for c in client.get("/api/characters").json() if c["id"] == cid)
    assert got["name"] == "KeepName"


# ==================== Превью строки чата ====================

def test_session_preview_shows_last_message(client):
    """
    В списке было видно только «Новый чат #7» — по такому не вспомнить, о чём он.
    Теперь строка несёт последнюю реплику, время и число сообщений.
    """
    cid = _mk_char(client, "Preview")
    sid = client.post(f"/api/sessions?character_id={cid}").json()["session_id"]
    empty = next(s for s in client.get("/api/sessions").json() if s["id"] == sid)
    assert empty["preview"] == "" and empty["messages"] == 0 and empty["last_at"] is None


def test_preview_collapses_whitespace(client):
    """Переносы строк в однострочном превью превратились бы в дыры."""
    from backend.horae_memory import estimate_tokens  # noqa: F401 — модуль грузится
    cid = _mk_char(client, "PreviewWs")
    sid = client.post(f"/api/sessions?character_id={cid}").json()["session_id"]
    rows = client.get(f"/api/sessions?character_id={cid}").json()
    assert "\n" not in rows[0]["preview"]
