"""
Тесты администрирования и контроля доступа (режим «контроль доступа»).
"""


def test_open_by_default(client):
    # Без заданного кода приложение открыто.
    assert client.get("/api/auth/status").json()["access_required"] is False
    assert client.get("/api/characters").status_code == 200


def test_access_code_gate(client):
    client.put("/api/admin/security", json={"access_code": "secret", "admin_password": "admin1"})
    try:
        st = client.get("/api/auth/status").json()
        assert st["access_required"] is True and st["admin_set"] is True

        # Без кода — 401 на защищённом эндпоинте.
        assert client.get("/api/characters").status_code == 401
        # С кодом — доступ есть.
        assert client.get("/api/characters", headers={"X-Access-Code": "secret"}).status_code == 200

        # Проверка кода входа.
        assert client.post("/api/auth/login", json={"code": "secret"}).json()["ok"] is True
        assert client.post("/api/auth/login", json={"code": "nope"}).json()["ok"] is False

        # Админка без пароля — 403, с паролем — 200.
        assert client.get("/api/admin/telegram", headers={"X-Access-Code": "secret"}).status_code == 403
        assert client.get("/api/admin/telegram", headers={"X-Admin-Password": "admin1"}).status_code == 200
    finally:
        # Снимаем код, чтобы не сломать остальные тесты (общий кэш + БД).
        client.put(
            "/api/admin/security",
            json={"access_code": "", "admin_password": ""},
            headers={"X-Admin-Password": "admin1"},
        )


def test_basic_auth_gate(client):
    """HTTP Basic Auth — внешний барьер: без учётки 401+WWW-Authenticate, /health открыт."""
    import base64

    client.put(
        "/api/admin/security",
        json={"basic_auth": {"enabled": True, "username": "u", "password": "p"}},
    )
    cred = "Basic " + base64.b64encode(b"u:p").decode()
    try:
        # Без заголовка — 401 и приглашение браузера ввести логин/пароль.
        r = client.get("/api/auth/status")
        assert r.status_code == 401
        assert "Basic" in r.headers.get("WWW-Authenticate", "")
        # /api/health открыт всегда (его опрашивает start.bat при запуске).
        assert client.get("/api/health").status_code == 200
        # С верной учёткой — проходит; с неверной — нет.
        assert client.get("/api/auth/status", headers={"Authorization": cred}).status_code == 200
        bad = "Basic " + base64.b64encode(b"u:wrong").decode()
        assert client.get("/api/auth/status", headers={"Authorization": bad}).status_code == 401
    finally:
        # Выключаем (запрос обязан нести верную учётку, пока Basic Auth активен).
        client.put(
            "/api/admin/security",
            json={"basic_auth": {"enabled": False}},
            headers={"Authorization": cred},
        )


def test_telegram_config_and_whitelist(client):
    tg = client.put(
        "/api/admin/telegram", json={"token": "123:abc", "enabled": False}
    ).json()
    assert tg["token"] == "123:abc"

    client.post("/api/admin/telegram/whitelist/555")
    assert 555 in client.get("/api/admin/telegram").json()["whitelist"]

    client.delete("/api/admin/telegram/whitelist/555")
    assert 555 not in client.get("/api/admin/telegram").json()["whitelist"]


def test_whitelist_enforced_by_default():
    """Без open_to_all не-внесённые в список получают ОТКАЗ (а не ответ бота)."""
    from backend import admin_service

    saved = admin_service._telegram_cache
    try:
        admin_service._telegram_cache = {"open_to_all": False, "whitelist": [111]}
        assert admin_service.is_whitelisted(111) is True
        assert admin_service.is_whitelisted(222) is False  # не в списке -> отказ

        admin_service._telegram_cache = {"open_to_all": False, "whitelist": []}
        assert admin_service.is_whitelisted(999) is False  # пустой список = никого

        admin_service._telegram_cache = {"open_to_all": True, "whitelist": []}
        assert admin_service.is_whitelisted(999) is True  # явно открыт всем
    finally:
        admin_service._telegram_cache = saved
