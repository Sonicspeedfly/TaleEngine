"""
Групповые чаты: дедуп участников (баг дублей), сопоставление имён режиссёром и
устойчивость режиссёра (пустой/ошибочный ответ → фолбэк, а не падение/зависание).
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import patch

from backend import group_chat, models
from backend.database import AsyncSessionLocal, init_db


# ---------- Чистая логика: сопоставление имён ----------
def _members(*names):
    return [SimpleNamespace(id=i + 1, name=n) for i, n in enumerate(names)]


def test_match_names_basic_and_order():
    ms = _members("Алиса", "Боб", "Кокос")
    got = group_chat._match_names(ms, "Пусть ответит Боб")
    assert [m.name for m in got] == ["Боб"]
    # Порядок — как названы в тексте режиссёра.
    got = group_chat._match_names(ms, "Кокос, потом Алиса")
    assert [m.name for m in got] == ["Кокос", "Алиса"]


def test_match_names_longest_first_no_substring_clash():
    # «Bot» не должен ложно срабатывать вместо «Bot редактор».
    ms = _members("Bot редактор", "Bot")
    got = group_chat._match_names(ms, "отвечает Bot редактор")
    assert got[0].name == "Bot редактор"


# ---------- Режиссёр: фолбэк вместо падения ----------
def _fake_chunk(text):
    return SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=text),
                                                    finish_reason=None)])


def _stream(*tokens):
    async def _acompletion(*a, **k):
        async def gen():
            for t in tokens:
                yield _fake_chunk(t)
        return gen()
    return _acompletion


def _empty_stream():
    async def _acompletion(*a, **k):
        async def gen():
            if False:
                yield None  # пустой поток
        return gen()
    return _acompletion


def test_director_picks_named_character():
    ms = _members("Алиса", "Боб")
    with patch("backend.llm_gateway.litellm.acompletion", new=_stream("Б", "об")):
        picked = asyncio.run(group_chat.director_pick(ms, "диалог", {}, last_user="Боб?"))
    assert [m.name for m in picked] == ["Боб"]


def test_director_empty_response_returns_empty_not_raise():
    """Пустой ответ режиссёра (фильтры/лимит) → [] (потом round-robin), НЕ исключение."""
    ms = _members("Алиса", "Боб")
    with patch("backend.llm_gateway.litellm.acompletion", new=_empty_stream()):
        picked = asyncio.run(group_chat.director_pick(ms, "диалог", {}))
    assert picked == []


def test_director_says_nobody():
    ms = _members("Алиса", "Боб")
    with patch("backend.llm_gateway.litellm.acompletion", new=_stream("никто")):
        picked = asyncio.run(group_chat.director_pick(ms, "диалог", {}))
    assert picked == []


def test_round_robin_next_after_last_speaker():
    ms = _members("Алиса", "Боб", "Кокос")
    assert group_chat.round_robin_next(ms, "Боб")[0].name == "Кокос"
    assert group_chat.round_robin_next(ms, "Кокос")[0].name == "Алиса"  # по кругу
    assert group_chat.round_robin_next(ms, None)[0].name == "Алиса"


# ---------- Дедуп участников в БД ----------
async def _dedupe_scenario():
    await init_db()
    async with AsyncSessionLocal() as db:
        a = models.Character(name="Дубль-А")
        b = models.Character(name="Дубль-Б")
        db.add_all([a, b])
        await db.commit()
        await db.refresh(a)
        await db.refresh(b)
        sess = models.ChatSession(character_id=a.id, user_key="web:x", is_group=True, title="Дубли")
        db.add(sess)
        await db.commit()
        await db.refresh(sess)
        # Порча данных: каждого персонажа добавляем несколько раз.
        for cid in [a.id, b.id, a.id, b.id, a.id]:
            db.add(models.GroupMember(session_id=sess.id, character_id=cid))
        await db.commit()

    # load_members лечит на чтении: 2 уникальных, порядок первого появления.
    async with AsyncSessionLocal() as db:
        members = await group_chat.load_members(db, sess.id)
    assert [m.name for m in members] == ["Дубль-А", "Дубль-Б"]

    # dedupe_members физически удаляет лишние строки.
    async with AsyncSessionLocal() as db:
        removed = await group_chat.dedupe_members(db, sess.id)
    assert removed == 3
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            models.GroupMember.__table__.select().where(
                models.GroupMember.session_id == sess.id
            )
        )).all()
    assert len(rows) == 2  # осталось по одному на персонажа
    # Повторный вызов — уже нечего удалять.
    async with AsyncSessionLocal() as db:
        assert await group_chat.dedupe_members(db, sess.id) == 0


def test_group_member_dedupe():
    asyncio.run(_dedupe_scenario())


def test_create_group_dedupes_ids(client):
    """POST /api/groups с повторяющимися id создаёт по одному участнику."""
    a = client.post("/api/characters", json={"name": "ГА"}).json()["id"]
    b = client.post("/api/characters", json={"name": "ГБ"}).json()["id"]
    g = client.post(
        "/api/groups", json={"name": "Гр", "character_ids": [a, a, b, b, a]}
    ).json()
    groups = client.get("/api/groups").json()
    grp = [x for x in groups if x["id"] == g["session_id"]][0]
    assert [m["id"] for m in grp["members"]] == [a, b]  # без дублей, порядок сохранён


def test_delete_group_cascades_no_accumulation(client):
    """
    КОРЕНЬ бага «складывает с прошлым результатом»: удаление чата должно удалять и
    его group_members. В SQLite id переиспользуется — без каскада новый чат наследовал
    осиротевших участников. Проверяем: удалили группу → пересоздали → без наследования.
    """
    a = client.post("/api/characters", json={"name": "КаскА"}).json()["id"]
    b = client.post("/api/characters", json={"name": "КаскБ"}).json()["id"]
    g1 = client.post("/api/groups", json={"name": "К1", "character_ids": [a, b]}).json()
    sid = g1["session_id"]
    assert client.delete(f"/api/sessions/{sid}").status_code == 200
    assert all(x["id"] != sid for x in client.get("/api/groups").json())  # чат пропал

    # Пересоздаём группу — она НЕ должна унаследовать участников удалённой.
    g2 = client.post("/api/groups", json={"name": "К2", "character_ids": [a]}).json()
    grp = [x for x in client.get("/api/groups").json() if x["id"] == g2["session_id"]][0]
    assert [m["id"] for m in grp["members"]] == [a]  # ровно один, без «пухнущих» дублей


async def _orphan_cleanup_scenario():
    await init_db()
    async with AsyncSessionLocal() as db:
        char = models.Character(name="Сирота")
        db.add(char)
        await db.commit()
        await db.refresh(char)
        sess = models.ChatSession(character_id=char.id, user_key="web:o", is_group=True, title="С")
        db.add(sess)
        await db.commit()
        await db.refresh(sess)
        # Дубли для существующего чата + строка-сирота на несуществующий чат.
        for _ in range(3):
            db.add(models.GroupMember(session_id=sess.id, character_id=char.id))
        db.add(models.GroupMember(session_id=999999, character_id=char.id))
        await db.commit()

    await init_db()  # повторный старт запускает _cleanup_orphans (идемпотентно)

    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            models.GroupMember.__table__.select()
        )).all()
    by_sess = {}
    for r in rows:
        by_sess.setdefault(r.session_id, 0)
        by_sess[r.session_id] += 1
    assert by_sess.get(sess.id) == 1        # дубли схлопнуты до одного
    assert 999999 not in by_sess            # сирота удалена


def test_startup_cleanup_removes_orphans_and_dupes():
    asyncio.run(_orphan_cleanup_scenario())
