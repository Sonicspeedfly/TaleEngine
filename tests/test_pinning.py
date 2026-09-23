"""
Закрепление чатов и персонажей наверху списка + превью строки чата.

Порядок считает СЕРВЕР, клиент сортирует тем же ключом (sortChats в app.js):
закреплённые выше остальных, внутри обеих групп — по последней активности
(реплика или создание чата), снятие возвращает элемент на обычное место.
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


# ==================== Закреп сильнее активности ====================

def _add_message(client, sid, text="новое сообщение"):
    """Реплика прямо в базу: отправка через чат требует живой модели."""
    from backend import models
    from backend.database import AsyncSessionLocal

    async def add():
        async with AsyncSessionLocal() as db:
            db.add(models.Message(session_id=sid, role="user", content=text))
            await db.commit()

    client.portal.call(add)


def test_new_message_never_lifts_unpinned_chat_above_pinned(client):
    """
    Сортировка по активности появилась позже закрепа, и главный риск — что свежая
    реплика в обычном чате поднимет его над закреплённым. Закреп — первый ключ.
    """
    cid = _mk_char(client, "PinVsActivity")
    pinned, other = (client.post(f"/api/sessions?character_id={cid}").json()["session_id"] for _ in range(2))
    client.patch(f"/api/sessions/{pinned}", json={"pinned": True})
    _add_message(client, other)
    rows = client.get(f"/api/sessions?character_id={cid}").json()
    assert [r["id"] for r in rows] == [pinned, other]
    # Закреплённые между собой — по активности, а не по времени закрепа: чат,
    # в котором только что писали, выше закреплённого позже, но молчащего.
    second = client.post(f"/api/sessions?character_id={cid}").json()["session_id"]
    client.patch(f"/api/sessions/{second}", json={"pinned": True})
    _add_message(client, pinned, "свежая реплика в раньше закреплённом")
    rows = client.get(f"/api/sessions?character_id={cid}").json()
    assert [r["id"] for r in rows] == [pinned, second, other]
    assert rows[0]["pinned_at"] and rows[2]["pinned_at"] is None


def _set_times(client, session_times=None, message_times=None):
    """
    Даты прямо в базу. func.now() пишет с точностью до секунды, а тест целиком
    укладывается в одну секунду — порядок по времени иначе не проверить.
    """
    from datetime import datetime

    from sqlalchemy import update

    from backend import models
    from backend.database import AsyncSessionLocal

    async def run():
        async with AsyncSessionLocal() as db:
            for sid, when in (session_times or {}).items():
                await db.execute(update(models.ChatSession)
                                 .where(models.ChatSession.id == sid)
                                 .values(created_at=datetime.fromisoformat(when)))
            for sid, when in (message_times or {}).items():
                await db.execute(update(models.Message)
                                 .where(models.Message.session_id == sid)
                                 .values(created_at=datetime.fromisoformat(when)))
            await db.commit()

    client.portal.call(run)


def test_new_empty_chat_goes_above_older_activity(client):
    """
    У пустого чата ключ активности был 0, и только что созданный чат падал в
    самый низ — под брошенные полгода назад. Теперь активность пустого чата —
    время создания, и новый чат стоит над теми, где писали раньше.
    """
    cid = _mk_char(client, "FreshOnTop")
    old = client.post(f"/api/sessions?character_id={cid}").json()["session_id"]
    _add_message(client, old)
    new = client.post(f"/api/sessions?character_id={cid}").json()["session_id"]
    _set_times(client, {old: "2026-01-01T10:00:00", new: "2026-01-02T10:00:00"},
               {old: "2026-01-01T11:00:00"})
    rows = client.get(f"/api/sessions?character_id={cid}").json()
    assert [r["id"] for r in rows] == [new, old]
    assert rows[0]["activity_at"].startswith("2026-01-02T10:00:00")
    assert rows[1]["activity_at"].startswith("2026-01-01T11:00:00")
    assert "_last_dt" not in rows[1]  # служебный ключ наружу не уходит
    # Реплика в старом чате поднимает его обратно.
    _set_times(client, message_times={old: "2026-01-03T09:00:00"})
    rows = client.get(f"/api/sessions?character_id={cid}").json()
    assert [r["id"] for r in rows] == [old, new]


def test_same_second_tie_prefers_chat_with_messages(client):
    """Ничья по секундам: чат с репликой выше пустого, созданного в ту же секунду."""
    cid = _mk_char(client, "SameSecond")
    a, b = (client.post(f"/api/sessions?character_id={cid}").json()["session_id"] for _ in range(2))
    _add_message(client, a)
    _set_times(client, {a: "2026-02-01T10:00:00", b: "2026-02-01T10:00:00"},
               {a: "2026-02-01T10:00:00"})
    rows = client.get(f"/api/sessions?character_id={cid}").json()
    assert [r["id"] for r in rows] == [a, b]


def test_unpinned_chats_are_ordered_by_last_activity(client):
    cid = _mk_char(client, "ActivityOrder")
    a, b = (client.post(f"/api/sessions?character_id={cid}").json()["session_id"] for _ in range(2))
    _add_message(client, a)  # старый чат ожил — поднимается над новым пустым
    rows = client.get(f"/api/sessions?character_id={cid}").json()
    assert [r["id"] for r in rows] == [a, b]


def test_pinned_group_stays_on_top_of_active_groups(client):
    c1, c2 = _mk_char(client, "GrpA"), _mk_char(client, "GrpB")
    g1 = client.post("/api/groups", json={"name": "Закреплённая", "character_ids": [c1, c2]}).json()["session_id"]
    g2 = client.post("/api/groups", json={"name": "Активная", "character_ids": [c1, c2]}).json()["session_id"]
    client.patch(f"/api/sessions/{g1}", json={"pinned": True})
    _add_message(client, g2)
    rows = [r for r in client.get("/api/groups").json() if r["id"] in (g1, g2)]
    assert [r["id"] for r in rows] == [g1, g2]
    assert rows[0]["pinned"] is True and rows[0]["pinned_at"]
