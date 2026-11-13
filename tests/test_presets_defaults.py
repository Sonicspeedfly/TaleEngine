"""
2.23.0: пресет по умолчанию — общий (⭐, задаёт админ) и личный у каждого
пользователя (👤). При запуске страница берёт личный, а без него — общий.
"""


def _names(client, h=None):
    return {p["name"]: p for p in client.get("/api/presets", headers=h).json()}


def test_global_default_without_accounts(client):
    p1 = client.post("/api/presets", json={"name": "GD-1", "params": {"model": "cheap"}}).json()
    p2 = client.post("/api/presets", json={"name": "GD-2", "params": {"model": "pricey"}}).json()
    assert p1["shared"] and p1["can_edit"]
    # Без аккаунтов «по умолчанию» один — общий, и scope=mine ведёт туда же.
    assert client.post(f"/api/presets/{p1['id']}/default").status_code == 200
    assert client.post(f"/api/presets/{p2['id']}/default?scope=mine").status_code == 200
    got = _names(client)
    assert got["GD-2"]["is_default"] and not got["GD-1"]["is_default"]
    assert not got["GD-2"]["my_default"]
    assert client.delete("/api/presets/default?scope=global").status_code == 200
    assert not any(p["is_default"] for p in client.get("/api/presets").json())
    assert client.delete(f"/api/presets/{p1['id']}").status_code == 200
    assert "GD-1" not in _names(client)


def test_personal_default_beats_shared(client):
    from backend import admin_service

    shared = client.post("/api/presets", json={"name": "PD-shared", "params": {"model": "flash"}}).json()
    client.post(f"/api/presets/{shared['id']}/default")   # общий ⭐ (режим без аккаунтов = админ)

    cache = admin_service.security_cache()
    old = dict(cache)
    cache["accounts_enabled"] = True
    try:
        # Первый зарегистрированный становится админом: берёт эту роль на себя.
        client.post("/api/auth/register", json={"username": "pd_first", "password": "pass12345"})
        a = client.post("/api/auth/register", json={"username": "pd_a", "password": "pass12345"}).json()
        b = client.post("/api/auth/register", json={"username": "pd_b", "password": "pass12345"}).json()
        if not (a.get("token") and b.get("token")):
            return   # регистрация закрыта в этой конфигурации тестов
        ha, hb = {"X-User-Token": a["token"]}, {"X-User-Token": b["token"]}

        # Общий виден всем, но править и удалять его может только админ.
        got = _names(client, ha)
        assert got["PD-shared"]["is_default"] and not got["PD-shared"]["can_edit"]
        assert client.delete(f"/api/presets/{shared['id']}", headers=ha).status_code == 403
        assert client.post(f"/api/presets/{shared['id']}/default?scope=global", headers=ha).status_code == 403
        assert client.post("/api/presets", json={"name": "PD-shared", "params": {}}, headers=ha).status_code == 409

        # Свой пресет — личный: второй пользователь его не видит и не занимает имя.
        mine = client.post("/api/presets", json={"name": "PD-a-argon", "params": {"model": "argon"}},
                           headers=ha).json()
        assert not mine["shared"] and mine["can_edit"]
        assert "PD-a-argon" not in _names(client, hb)
        assert client.post("/api/presets", json={"name": "PD-a-argon", "params": {}}, headers=hb).status_code == 409
        assert client.post(f"/api/presets/{mine['id']}/default?scope=mine", headers=hb).status_code == 404
        assert client.delete(f"/api/presets/{mine['id']}", headers=hb).status_code == 404

        # Личный по умолчанию — только у A; у B остаётся общий.
        assert client.post(f"/api/presets/{mine['id']}/default?scope=mine", headers=ha).status_code == 200
        got_a, got_b = _names(client, ha), _names(client, hb)
        assert got_a["PD-a-argon"]["my_default"]
        assert not any(p["my_default"] for p in got_b.values())
        assert got_b["PD-shared"]["is_default"]

        # Без scope обычный пользователь ставит личный, общий не трогает.
        assert client.post(f"/api/presets/{shared['id']}/default", headers=hb).status_code == 200
        assert _names(client, hb)["PD-shared"]["my_default"]
        assert not _names(client, ha)["PD-shared"]["my_default"]

        # Снять свой — вернуться к общему.
        assert client.delete("/api/presets/default?scope=mine", headers=ha).status_code == 200
        assert not any(p["my_default"] for p in _names(client, ha).values())
        assert client.delete("/api/presets/default?scope=global", headers=ha).status_code == 403
    finally:
        cache.clear()
        cache.update(old)
        client.delete("/api/presets/default?scope=global")


def test_deleted_personal_default_does_not_stick_to_reused_id(client):
    from backend import admin_service

    cache = admin_service.security_cache()
    old = dict(cache)
    cache["accounts_enabled"] = True
    try:
        client.post("/api/auth/register", json={"username": "pr_first", "password": "pass12345"})
        a = client.post("/api/auth/register", json={"username": "pr_a", "password": "pass12345"}).json()
        if not a.get("token"):
            return
        ha = {"X-User-Token": a["token"]}
        x = client.post("/api/presets", json={"name": "PR-x", "params": {}}, headers=ha).json()
        client.post(f"/api/presets/{x['id']}/default?scope=mine", headers=ha)
        assert client.delete(f"/api/presets/{x['id']}", headers=ha).status_code == 200
        # SQLite может отдать новому пресету тот же номер — 👤 на нём быть не должно.
        y = client.post("/api/presets", json={"name": "PR-y", "params": {}}, headers=ha).json()
        assert not any(p["my_default"] for p in client.get("/api/presets", headers=ha).json()), y
    finally:
        cache.clear()
        cache.update(old)


def test_resave_keeps_shared_preset_shared(client):
    p = client.post("/api/presets", json={"name": "RS-shared", "params": {"model": "a"}}).json()
    client.post(f"/api/presets/{p['id']}/default")
    # Пересохранение без флажка «общий» меняет только параметры.
    again = client.post("/api/presets", json={"name": "RS-shared", "params": {"model": "b"}}).json()
    assert again["shared"] and again["is_default"] and again["params"] == {"model": "b"}
    client.delete("/api/presets/default?scope=global")


def test_chat_preset_roundtrip(client):
    cid = client.post("/api/characters", json={"name": "ЧатПресет", "first_message": "Привет"}).json()["id"]
    sid = client.post(f"/api/sessions?character_id={cid}").json()["session_id"]
    p = client.post("/api/presets", json={"name": "CP-argon", "params": {"model": "argon"}}).json()

    assert client.patch(f"/api/sessions/{sid}", json={"preset_id": p["id"]}).status_code == 200
    assert client.get(f"/api/sessions/{sid}/card").json()["preset_id"] == p["id"]
    rows = client.get(f"/api/sessions?character_id={cid}").json()
    assert any(r["id"] == sid and r["preset_id"] == p["id"] for r in rows)

    # Ветка наследует пресет чата.
    mid = client.get(f"/api/sessions/{sid}/messages").json()
    assert mid, "у персонажа есть приветствие — в чате одно сообщение"
    fork = client.post(f"/api/sessions/{sid}/fork", json={"message_id": mid[-1]["id"]}).json()
    assert client.get(f"/api/sessions/{fork['session_id']}/card").json()["preset_id"] == p["id"]

    # Несуществующий пресет не прилипает; 0 — снять.
    assert client.patch(f"/api/sessions/{sid}", json={"preset_id": 999999}).status_code == 404
    assert client.patch(f"/api/sessions/{sid}", json={"preset_id": 0}).status_code == 200
    assert client.get(f"/api/sessions/{sid}/card").json()["preset_id"] is None

    # Удалённый пресет снимается с чатов (номер может достаться новому).
    client.patch(f"/api/sessions/{sid}", json={"preset_id": p["id"]})
    client.delete(f"/api/presets/{p['id']}")
    assert client.get(f"/api/sessions/{sid}/card").json()["preset_id"] is None


def test_unhandled_error_has_readable_detail(monkeypatch):
    from fastapi.testclient import TestClient

    from backend import knowledge
    from backend.main import app

    async def boom(*a, **k):
        raise RuntimeError("database is locked")

    with TestClient(app, raise_server_exceptions=False) as c:
        cid = c.post("/api/characters", json={"name": "Сбой"}).json()["id"]
        sid = c.post(f"/api/sessions?character_id={cid}").json()["session_id"]
        monkeypatch.setattr(knowledge, "list_files", boom)
        r = c.delete(f"/api/sessions/{sid}")
        assert r.status_code == 500
        assert "База занята" in r.json()["detail"]


def test_error_detail_hides_sql_and_keys(monkeypatch):
    from fastapi.testclient import TestClient
    from sqlalchemy.exc import OperationalError

    from backend import knowledge
    from backend.main import app

    async def boom(*a, **k):
        raise OperationalError("UPDATE app_settings SET value=?", ({"api_key": "sk-secret123456"},),
                               Exception("disk I/O error"))

    with TestClient(app, raise_server_exceptions=False) as c:
        cid = c.post("/api/characters", json={"name": "СбойSQL"}).json()["id"]
        sid = c.post(f"/api/sessions?character_id={cid}").json()["session_id"]
        monkeypatch.setattr(knowledge, "list_files", boom)
        detail = c.delete(f"/api/sessions/{sid}").json()["detail"]
        assert "disk I/O error" in detail
        assert "sk-secret" not in detail and "UPDATE" not in detail


def test_context_report_uses_chat_preset(client):
    cid = client.post("/api/characters", json={"name": "Инспектор", "first_message": "Привет"}).json()["id"]
    sid = client.post(f"/api/sessions?character_id={cid}").json()["session_id"]
    p = client.post("/api/presets", json={"name": "CTX-preset", "params": {"model": "chat-preset-model"}}).json()
    client.patch(f"/api/sessions/{sid}", json={"preset_id": p["id"]})
    r = client.get(f"/api/sessions/{sid}/context")
    assert r.status_code == 200 and r.json()["model"] == "chat-preset-model"


def test_double_delete_is_idempotent(client):
    cid = client.post("/api/characters", json={"name": "Дважды"}).json()["id"]
    sid = client.post(f"/api/sessions?character_id={cid}").json()["session_id"]
    assert client.delete(f"/api/sessions/{sid}").status_code == 200
    assert client.delete(f"/api/sessions/{sid}").status_code == 200
