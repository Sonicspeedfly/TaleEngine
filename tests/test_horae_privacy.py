"""
Приватность памяти Horae в режиме аккаунтов (регрессия на утечку чужой памяти).

Без привязки владельца эндпоинт GET /api/horae отдавал ВСЕ записи, и чужой
пользователь видел в «Памяти» приватные записи чужих сессий. Здесь проверяем, что:
  * приватная (сессионная) память пользователя A не видна постороннему B;
  * глобальный лор мира по-прежнему виден всем;
  * память расшаренной сессии видна соавтору (тому, кому чат пошарили);
  * лорбук персонажа A не виден B, который этим персонажем не владеет;
  * B не может править/удалять чужую запись и создавать память в чужой сессии.

Тест самодостаточен и не зависит от порядка запуска: B регистрируется после A,
поэтому гарантированно не админ; режим аккаунтов выключаем в finally своим
admin_password (а не правами глобального админа, личность которого тесту неизвестна).
"""


def test_horae_privacy_scoped_by_owner(client):
    # B регистрируется после A → точно не админ (первый в БД уже есть).
    a = client.post("/api/auth/register", json={"username": "horae_a", "password": "pw"}).json()
    b = client.post("/api/auth/register", json={"username": "horae_b", "password": "pw"}).json()
    ah = {"X-User-Token": a["token"]}
    bh = {"X-User-Token": b["token"]}

    client.put("/api/admin/security", json={"accounts_enabled": True, "admin_password": "horae_pw"})
    try:
        # A заводит персонажа и две сессии: приватную и расшаренную с B.
        char_a = client.post("/api/characters", json={"name": "HoraeCharA"}, headers=ah).json()
        priv = client.post(f"/api/sessions?character_id={char_a['id']}", headers=ah).json()["session_id"]
        shared = client.post(f"/api/sessions?character_id={char_a['id']}", headers=ah).json()["session_id"]

        # Шарить чат можно только другу — сперва дружим A и B.
        client.post("/api/friends/add", json={"username": "horae_b"}, headers=ah)
        inc = client.get("/api/friends", headers=bh).json()["incoming"]
        client.post(f"/api/friends/{inc[0]['friendship_id']}/accept", headers=bh)
        client.post(f"/api/sessions/{shared}/share", json={"username": "horae_b"}, headers=ah)

        # Память, созданная A: приватная сессия, расшаренная сессия, персонаж, глобальный лор.
        priv_h = client.post(
            "/api/horae",
            json={"category": "state", "title": "Секрет A", "content": "приватное состояние", "session_id": priv},
            headers=ah,
        ).json()
        shared_h = client.post(
            "/api/horae",
            json={"category": "state", "title": "Состояние общего чата", "content": "видно соавтору", "session_id": shared},
            headers=ah,
        ).json()
        char_h = client.post(
            "/api/horae",
            json={"category": "lore", "title": "Лорбук A", "content": "лор персонажа A", "character_id": char_a["id"]},
            headers=ah,
        ).json()
        global_h = client.post(
            "/api/horae",
            json={"category": "lore", "title": "Лор мира", "content": "общий для всех", "session_id": None},
            headers=ah,
        ).json()

        # B видит у себя только глобальный лор и расшаренную сессию.
        b_ids = {h["id"] for h in client.get("/api/horae", headers=bh).json()}
        assert priv_h["id"] not in b_ids      # приватная память чужой сессии скрыта (фикс утечки)
        assert char_h["id"] not in b_ids      # чужой лорбук персонажа скрыт
        assert global_h["id"] in b_ids        # глобальный лор виден всем
        assert shared_h["id"] in b_ids        # соавтор видит память расшаренного чата

        # A видит все свои записи.
        a_ids = {h["id"] for h in client.get("/api/horae", headers=ah).json()}
        assert {priv_h["id"], shared_h["id"], char_h["id"], global_h["id"]} <= a_ids

        # Фильтрация по конкретной сессии тоже уважает доступ: к приватной сессии A — пусто у B.
        assert client.get(f"/api/horae?session_id={priv}", headers=bh).json() == []

        # B не может ни изменить, ни удалить чужую запись, ни создать память в чужой сессии.
        assert client.patch(f"/api/horae/{priv_h['id']}", json={"title": "взлом"}, headers=bh).status_code == 403
        assert client.delete(f"/api/horae/{priv_h['id']}", headers=bh).status_code == 403
        assert client.post("/api/horae", json={"title": "чужое", "session_id": priv}, headers=bh).status_code == 403
    finally:
        # Выключаем режим аккаунтов своим admin_password (токен A + пароль работает,
        # даже если A не глобальный админ); заодно стираем пароль, чтобы не мешать другим тестам.
        client.put(
            "/api/admin/security",
            json={"accounts_enabled": False, "admin_password": ""},
            headers={**ah, "X-Admin-Password": "horae_pw"},
        )
