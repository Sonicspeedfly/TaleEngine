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
