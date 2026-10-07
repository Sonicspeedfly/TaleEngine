"""
Память о пользователе (backend/user_memory.py): общие сведения о человеке для
всех его чатов — без тем, планов и сюжета отдельных разговоров.

Проверяем:
  * фильтр отсекает планы («скину порты в JSON»), код, имена персонажей,
    сведения без цитаты или с цитатой, которой нет в репликах;
  * модель видит только реплики пользователя, не ответы персонажа;
  * новое сведение — кандидат и в промпт не идёт; повтор другой фразой в
    другом чате, реплика вне роли или подтверждение человеком его включают;
  * копия реплик (ветка) — не повтор; чат, открытый другу, не разбирается
    и памяти не получает;
  * API: список, ручная запись, правка, подтверждение, удаление, настройки.
"""
import functools
import json
from unittest.mock import patch

import pytest

from backend import user_memory as um


def _chunk(text):
    class _Delta:
        content = text

    class _Choice:
        delta = _Delta()

    class _Chunk:
        choices = [_Choice()]

    return _Chunk()


def _llm(chat_reply="Ок.", profile=None, seen=None):
    """Подменная модель: на разбор памяти — JSON из profile, на ход — chat_reply."""
    async def fake(*args, **kwargs):
        messages = kwargs.get("messages") or []
        is_profile = bool(messages) and messages[0].get("content") == um.PROFILE_PROMPT
        if seen is not None:
            seen.append(("profile" if is_profile else "chat", messages))
        text = json.dumps(profile if profile is not None else {"add": [], "drop": []},
                          ensure_ascii=False) if is_profile else chat_reply

        async def gen():
            yield _chunk(text)
        return gen()
    return fake


@pytest.fixture
def clean_memory(client):
    client.delete("/api/user-memory")
    client.put("/api/user-memory/settings", json={"enabled": True, "auto": True, "instant": False})
    yield
    client.delete("/api/user-memory")


def _chat(client, name="Джеми"):
    cid = client.post("/api/characters", json={"name": name}).json()["id"]
    return cid, client.post(f"/api/sessions?character_id={cid}").json()["session_id"]


def _say(client, sid, text, seen=None, reply="Ок."):
    with patch("backend.llm_gateway.litellm.acompletion", new=_llm(reply, seen=seen)):
        with client.websocket_connect(f"/ws/chat/{sid}") as ws:
            ws.send_json({"type": "user_message", "content": text})
            for _ in range(100):
                if ws.receive_json()["type"] in ("done", "error"):
                    break


def _scan(client, sid, profile, seen=None):
    with patch("backend.llm_gateway.litellm.acompletion", new=_llm(profile=profile, seen=seen)):
        return client.portal.call(functools.partial(um.maybe_update, sid, force=True))


def _items(client):
    return client.get("/api/user-memory").json()["items"]


def _system_of(client, sid):
    blocks = client.get(f"/api/sessions/{sid}/context").json().get("blocks") or []
    return next((b["text"] for b in blocks if b["key"] == "user_memory"), "")


# ---------------------------------------------------------------- фильтр
def test_screen_rejects_chat_topics_plans_code_and_roles():
    src = ("скину тебе порты в json по dungeons and dragons. я python-разработчик уже лет пять. "
           "джеми, обними меня. отвечай покороче пожалуйста")
    src_norm, src_terms = um._norm_key(src), um.terms(src)
    names = um._names_pattern(["Джеми", "Артур"])

    def ok(cat, text, quote):
        return um.screen({"cat": cat, "text": text, "quote": quote}, src_norm, src_terms, names)

    assert ok("work", "Python-разработчик", "я python-разработчик уже лет пять")
    assert ok("style", "Просит отвечать коротко", "отвечай покороче пожалуйста")
    assert ok("interests", "Скинет порты в JSON по D&D", "скину тебе порты в json") is None   # план
    assert ok("work", "Пишет config.json", "я python-разработчик") is None                     # файл/код
    assert ok("interests", "Обнимает Джеми", "джеми, обними меня") is None                     # персонаж
    assert ok("work", "Python-разработчик", "") is None                                        # без цитаты
    assert ok("work", "Врач", "я работаю врачом в больнице") is None                           # цитаты нет в репликах
    assert ok("mood", "Устал", "я python-разработчик") is None                                 # не та категория
    assert ok("interests", "Сегодня играет в D&D", "по dungeons and dragons") is None          # время


def test_parse_reply_tolerates_fences_and_garbage():
    assert um.parse_reply('```json\n{"add":[{"cat":"lang"}],"drop":[]}\n```')["add"] == [{"cat": "lang"}]
    assert um.parse_reply("Вот: {\"add\": []}") == {"add": [], "drop": []}
    assert um.parse_reply("снимок сюжета без JSON") == {"add": [], "drop": []}
    assert um.parse_reply("[1, 2]") == {"add": [], "drop": []}


# ---------------------------------------------------------------- разбор
def test_only_user_lines_go_to_model_and_new_fact_waits(client, clean_memory):
    _, sid = _chat(client)
    _say(client, sid, "Привет, я python-разработчик", reply="Пришли мне порты в JSON, Артур!")
    seen = []
    stats = _scan(client, sid, {"add": [
        {"cat": "work", "text": "Python-разработчик", "quote": "я python-разработчик"},
        {"cat": "interests", "text": "Пришлёт порты в JSON", "quote": "пришли мне порты в JSON"},
    ]}, seen=seen)
    asked = [m for kind, m in seen if kind == "profile"][0]
    assert "python-разработчик" in asked[1]["content"]
    assert "Пришли мне порты" not in asked[1]["content"]          # ответ персонажа модель не видит
    assert stats["added"] == 1
    items = _items(client)
    assert [(i["content"], i["status"]) for i in items] == [("Python-разработчик", "candidate")]
    assert _system_of(client, sid) == ""                           # кандидат в промпт не идёт


def test_repeat_in_other_chat_with_other_words_activates(client, clean_memory):
    _, chat_a = _chat(client)
    _, chat_b = _chat(client, name="Лира")
    _say(client, chat_a, "я python-разработчик")
    _scan(client, chat_a, {"add": [{"cat": "work", "text": "Python-разработчик", "quote": "я python-разработчик"}]})
    # Ветка чата A — копия тех же слов: не повтор.
    first_user = [m for m in client.get(f"/api/sessions/{chat_a}/messages").json() if m["role"] == "user"][0]
    fork = client.post(f"/api/sessions/{chat_a}/fork", json={"message_id": first_user["id"]}).json()["session_id"]
    assert _scan(client, fork, {"add": [{"cat": "work", "text": "Python-разработчик",
                                         "quote": "я python-разработчик"}]}) is None
    assert _items(client)[0]["status"] == "candidate"

    _say(client, chat_b, "Работаю python-разработчиком в банке")
    stats = _scan(client, chat_b, {"add": [{"cat": "work", "text": "Python разработчик",
                                            "quote": "работаю python-разработчиком"}]})
    assert stats["activated"] == 1
    item = _items(client)[0]
    assert item["status"] == "active" and item["chats"] == 2
    # Теперь сведение — в системном промпте любого чата этого пользователя.
    _, chat_c = _chat(client, name="Мира")
    block = _system_of(client, chat_c)
    assert "Python-разработчик" in block and "НЕ события" in block


def test_out_of_role_statement_is_active_at_once(client, clean_memory):
    _, sid = _chat(client)
    _say(client, sid, "((зови меня Лёша и на ты))")
    _scan(client, sid, {"add": [{"cat": "address", "text": "Просит звать Лёшей и на «ты»",
                                 "quote": "зови меня Лёша и на ты"}]})
    assert _items(client)[0]["status"] == "active"
    assert "Лёшей" in _system_of(client, sid)


def test_shared_chat_is_neither_scanned_nor_given_memory(client, clean_memory):
    from backend import models
    from backend.database import AsyncSessionLocal

    _, sid = _chat(client)
    client.post("/api/user-memory", json={"category": "lang", "content": "Пишет по-русски"})
    assert "по-русски" in _system_of(client, sid)

    async def share():
        async with AsyncSessionLocal() as db:
            db.add(models.SessionShare(session_id=sid, user_id=1))
            await db.commit()

    client.portal.call(share)
    _say(client, sid, "я python-разработчик")
    assert _scan(client, sid, {"add": [{"cat": "work", "text": "Python-разработчик",
                                        "quote": "я python-разработчик"}]}) is None
    assert _system_of(client, sid) == ""


# ---------------------------------------------------------------- API
def test_api_manual_edit_confirm_disable(client, clean_memory):
    _, sid = _chat(client)
    a = client.post("/api/user-memory", json={"category": "name", "content": "Алексей"}).json()
    assert a["status"] == "active" and a["locked"]
    b = client.post("/api/user-memory", json={"category": "name", "content": "Лёша"}).json()
    names = [i for i in _items(client) if i["category"] == "name"]
    assert [i["id"] for i in names] == [b["id"]]            # имя одно — новое заменило старое

    _say(client, sid, "люблю настольные игры")
    _scan(client, sid, {"add": [{"cat": "interests", "text": "Любит настольные игры",
                                 "quote": "люблю настольные игры"}]})
    cand = next(i for i in _items(client) if i["category"] == "interests")
    assert cand["status"] == "candidate" and cand["quote"] == "люблю настольные игры"
    ok = client.patch(f"/api/user-memory/{cand['id']}", json={"status": "active"}).json()
    assert ok["status"] == "active" and ok["locked"]
    assert "настольные" in _system_of(client, sid)

    client.patch(f"/api/user-memory/{cand['id']}", json={"enabled": False})
    assert "настольные" not in _system_of(client, sid)
    edited = client.patch(f"/api/user-memory/{b['id']}", json={"content": "Алекс"}).json()
    assert edited["content"] == "Алекс"
    assert client.patch(f"/api/user-memory/{cand['id']}", json={"category": "name",
                                                                "content": "Алекс"}).status_code == 409

    client.put("/api/user-memory/settings", json={"enabled": False})
    assert _system_of(client, sid) == ""
    client.put("/api/user-memory/settings", json={"enabled": True, "auto": False})
    assert _scan(client, sid, {"add": []}) is None          # сбор выключен — разбора нет
    assert client.delete(f"/api/user-memory/{b['id']}").json()["ok"]
    assert all(i["id"] != b["id"] for i in _items(client))
    assert client.patch("/api/user-memory/999999", json={"enabled": False}).status_code == 404


def test_pointer_skips_old_backlog_and_threshold(client, clean_memory):
    _, sid = _chat(client)
    _say(client, sid, "привет")
    with patch.object(um.settings, "USER_MEMORY", True):
        # Одна короткая свежая реплика — рано: разбора нет.
        with patch("backend.llm_gateway.litellm.acompletion", new=_llm(profile={"add": []})):
            assert client.portal.call(um.maybe_update, sid) is None
    _scan(client, sid, {"add": []})
    msgs = client.get(f"/api/sessions/{sid}/messages").json()
    last_user = [m["id"] for m in msgs if m["role"] == "user"][-1]

    async def upto():
        from backend import models
        from backend.database import AsyncSessionLocal

        async with AsyncSessionLocal() as db:
            return (await db.get(models.ChatSession, sid)).profile_upto

    assert client.portal.call(upto) == last_user
