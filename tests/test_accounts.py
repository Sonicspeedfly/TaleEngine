"""
Тесты режима аккаунтов (опциональный, по умолчанию выключен).
Один самодостаточный тест: первый зарегистрированный — админ, поэтому им же
выключаем режим в finally (чтобы не повлиять на остальные тесты — общий кэш/БД).
"""


def test_link_code_roundtrip():
    from backend import accounts

    code = accounts.make_link_code(42)
    assert accounts.consume_link_code(code) == 42      # привязали к user 42
    assert accounts.consume_link_code(code) is None    # код одноразовый
    assert accounts.consume_link_code("NOPE") is None  # неизвестный код


def test_accounts_full_flow(client):
    admin = client.post("/api/auth/register", json={"username": "acc_admin", "password": "pw"}).json()
    assert admin["user"]["role"] == "admin"  # первый пользователь — администратор
    user = client.post("/api/auth/register", json={"username": "acc_user", "password": "pw"}).json()
    assert user["user"]["role"] == "user"
    ah = {"X-User-Token": admin["token"]}
    uh = {"X-User-Token": user["token"]}

    # Логин и типичные ошибки.
    assert client.post("/api/auth/login_user", json={"username": "acc_user", "password": "pw"}).status_code == 200
    assert client.post("/api/auth/login_user", json={"username": "acc_user", "password": "bad"}).status_code == 401
    assert client.post("/api/auth/register", json={"username": "acc_admin", "password": "x"}).status_code == 409

    client.put("/api/admin/security", json={"accounts_enabled": True})
    try:
        # Без токена защищённый эндпоинт недоступен.
        assert client.get("/api/characters").status_code == 401
        assert client.get("/api/auth/me", headers=uh).json()["username"] == "acc_user"

        client.post("/api/characters", json={"name": "UserChar"}, headers=uh)
        client.post("/api/characters", json={"name": "AdminChar"}, headers=ah)
        user_names = [c["name"] for c in client.get("/api/characters", headers=uh).json()]
        admin_names = [c["name"] for c in client.get("/api/characters", headers=ah).json()]
        # Обычный пользователь видит только своё; админ — всё.
        assert "UserChar" in user_names and "AdminChar" not in user_names
        assert "UserChar" in admin_names and "AdminChar" in admin_names

        # Друзья.
        client.post("/api/friends/add", json={"username": "acc_user"}, headers=ah)
        incoming = client.get("/api/friends", headers=uh).json()["incoming"]
        assert any(i["username"] == "acc_admin" for i in incoming)
        client.post(f"/api/friends/{incoming[0]['friendship_id']}/accept", headers=uh)
        friends = client.get("/api/friends", headers=ah).json()["friends"]
        assert any(f["username"] == "acc_user" for f in friends)

        # Повторная заявка тому же другу не плодит дубликаты.
        before = len(client.get("/api/friends", headers=ah).json()["friends"])
        client.post("/api/friends/add", json={"username": "acc_user"}, headers=ah)
        assert len(client.get("/api/friends", headers=ah).json()["friends"]) == before

        # Шаринг чата другу: доступ только после share, и только владелец делится.
        ch = client.post("/api/characters", json={"name": "ShareChar"}, headers=ah).json()
        sid = client.post(f"/api/sessions?character_id={ch['id']}", headers=ah).json()["session_id"]
        assert client.get(f"/api/sessions/{sid}/messages", headers=uh).status_code == 403
        client.post(f"/api/sessions/{sid}/share", json={"username": "acc_user"}, headers=ah)
        assert any(s["id"] == sid for s in client.get("/api/sessions/shared", headers=uh).json())
        assert client.get(f"/api/sessions/{sid}/messages", headers=uh).status_code == 200
        assert client.post(
            f"/api/sessions/{sid}/share", json={"username": "acc_admin"}, headers=uh
        ).status_code == 403  # не-владелец делиться не может

        # Безопасность: подключение (доступ к Gemini) — только админ.
        client.put(
            "/api/settings/connection",
            json={"use_proxy": True, "base_url": "http://x", "api_key": "sk-secret", "default_model": "m"},
            headers=ah,
        )
        assert client.get("/api/settings/connection", headers=ah).json()["api_key"] == "sk-secret"
        assert client.get("/api/settings/connection", headers=uh).json()["api_key"] == "***"  # ключ скрыт
        assert client.put(
            "/api/settings/connection",
            json={"use_proxy": True, "base_url": "y", "api_key": "x", "default_model": "m"},
            headers=uh,
        ).status_code == 403  # обычный пользователь не может менять
        # Управление пользователями — только админ.
        assert client.get("/api/admin/users", headers=uh).status_code == 403
        assert any(u["username"] == "acc_user" for u in client.get("/api/admin/users", headers=ah).json())

        # Отклонение заявки удаляет её и не создаёт дружбу.
        u2 = client.post("/api/auth/register", json={"username": "acc_user2", "password": "pw"}).json()
        u2h = {"X-User-Token": u2["token"]}
        client.post("/api/friends/add", json={"username": "acc_user2"}, headers=ah)
        inc2 = client.get("/api/friends", headers=u2h).json()["incoming"]
        assert any(i["username"] == "acc_admin" for i in inc2)
        client.post(f"/api/friends/{inc2[0]['friendship_id']}/decline", headers=u2h)
        assert client.get("/api/friends", headers=u2h).json()["incoming"] == []
        assert not any(f["username"] == "acc_user2" for f in client.get("/api/friends", headers=ah).json()["friends"])
    finally:
        client.put("/api/admin/security", json={"accounts_enabled": False}, headers=ah)
