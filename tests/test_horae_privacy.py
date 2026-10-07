"""
Приватность памяти Horae в режиме аккаунтов (регрессия на утечку чужой памяти).

Без привязки владельца эндпоинт GET /api/horae отдавал ВСЕ записи, и чужой
пользователь видел в «Памяти» приватные записи чужих сессий. Здесь проверяем, что:
  * приватная (сессионная) память пользователя A не видна постороннему B;
  * глобальный лор мира по-прежнему виден всем;
  * память расшаренной сессии видна соавтору (тому, кому чат пошарили);
  * лорбук персонажа A не виден B, который этим персонажем не владеет;
  * B не может править/удалять чужую запись и создавать память в чужой сессии;
  * запись «для всех чатов» в режиме аккаунтов заводит и правит только админ.

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

    # Лор мира заводят до включения аккаунтов (тогда доступ полный): в режиме
    # аккаунтов запись «для всех чатов» добавляет только администратор.
    global_h = client.post(
        "/api/horae",
        json={"category": "lore", "title": "Лор мира", "content": "общий для всех", "session_id": None},
    ).json()
    # Общий персонаж (без владельца), глобальная таблица и память «local» — тоже до аккаунтов.
    public = client.post("/api/characters", json={"name": "HoraePublic"}).json()
    pub_chat = client.post(f"/api/sessions?character_id={public['id']}").json()["session_id"]
    gtable = client.post(f"/api/sessions/{pub_chat}/horae/tables",
                         json={"name": "Мир", "rows": 2, "cols": 2, "scope": "global"}).json()["id"]
    client.post("/api/user-memory", json={"category": "work", "content": "Хирург в городской больнице"})

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
        # Запись «для всех чатов» ушла бы в чаты ВСЕХ пользователей — обычному
        # пользователю её не завести и не поменять, и свою в неё не перенести.
        # (B — точно не админ, A мог им оказаться: первый пользователь в БД.)
        assert client.post("/api/horae", json={"title": "мой лор", "session_id": None},
                           headers=bh).status_code == 403
        assert client.patch(f"/api/horae/{global_h['id']}", json={"title": "взлом"}, headers=bh).status_code == 403
        assert client.delete(f"/api/horae/{global_h['id']}", headers=bh).status_code == 403
        char_b = client.post("/api/characters", json={"name": "HoraeCharB"}, headers=bh).json()
        own = client.post(f"/api/sessions?character_id={char_b['id']}", headers=bh).json()["session_id"]
        own_h = client.post("/api/horae", json={"title": "моё", "session_id": own}, headers=bh).json()
        assert client.patch(f"/api/horae/{own_h['id']}", json={"scope": "global"}, headers=bh).status_code == 403
        # Свою запись в чужой чат тоже не перенести; в общий с ним — можно.
        assert client.patch(f"/api/horae/{own_h['id']}", json={"scope": "session", "session_id": priv},
                            headers=bh).status_code == 403
        moved = client.patch(f"/api/horae/{own_h['id']}", json={"scope": "session", "session_id": shared},
                             headers=bh)
        assert moved.status_code == 200 and moved.json()["session_id"] == shared
        # Запись с чужим чатом и своим персонажем всё равно попала бы в чужой чат.
        assert client.post("/api/horae", json={"title": "в чужой чат", "session_id": priv,
                                               "character_id": char_b["id"]}, headers=bh).status_code == 403
        # Лорбук общего персонажа уходит в чаты всех, кто с ним говорит, — только админ.
        assert client.post("/api/horae", json={"title": "в общего", "character_id": public["id"]},
                           headers=bh).status_code == 403
        # Заголовок глобальной таблицы виден во всех чатах — его правит только админ.
        b_pub = client.post(f"/api/sessions?character_id={public['id']}", headers=bh)
        assert b_pub.status_code == 200
        assert client.patch(f"/api/sessions/{b_pub.json()['session_id']}/horae/tables/{gtable}",
                            json={"cell": {"r": 0, "c": 1, "value": "B_WAS_HERE"}},
                            headers=bh).status_code == 403
        # Токен в адресе — тот же пользователь, а не «никто» с чужой памятью «local».
        mem = client.get(f"/api/user-memory?token={b['token']}").json()
        assert mem["profile"].startswith("u:") and not mem["items"]
        assert client.get("/api/user-memory?profile=local", headers=bh).status_code == 403

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
