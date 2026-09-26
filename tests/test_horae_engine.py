"""
Horae State Engine целиком: ход по WebSocket с тегами → чистый текст и мета,
блоки Horae в контексте следующего хода, перегенерация без своей меты,
выключение на чат, правки, события, свёртки, таблицы, ветка, удаление, импорт
из SillyTavern и нативный экспорт. LLM подменяется на уровне litellm.
"""
import json
from unittest.mock import patch

TAGGED = (
    "Вольф кивнул и сел у окна.\n"
    "<horae>\ntime:2026/2/4 15:00\nlocation:{loc}\ncharacters:Вольф\ncostume:Вольф=куртка\n"
    "npc:Вольф|шрам=молчалив@гость~age:35\nitem!:🗝Ключ|ржавый=Вольф@карман\n</horae>\n"
    "<horaeevent>\nevent:important|Вольф пришёл в {loc}\n</horaeevent>"
)


def _chunk(text):
    class _Delta:
        content = text

    class _Choice:
        delta = _Delta()

    class _Chunk:
        choices = [_Choice()]

    return _Chunk()


def _llm(reply, seen=None):
    async def fake(*args, **kwargs):
        if seen is not None:
            seen.append(kwargs.get("messages") or [])

        async def gen():
            yield _chunk(reply)
        return gen()
    return fake


def _chat(client, name="Марина"):
    cid = client.post("/api/characters", json={"name": name}).json()["id"]
    sid = client.post(f"/api/sessions?character_id={cid}").json()["session_id"]
    return cid, sid


def _turn(client, sid, reply, text="привет", seen=None, kind="user_message"):
    with patch("backend.llm_gateway.litellm.acompletion", new=_llm(reply, seen)):
        with client.websocket_connect(f"/ws/chat/{sid}") as ws:
            ws.send_json({"type": kind, "content": text} if kind == "user_message" else {"type": kind})
            for _ in range(100):
                if ws.receive_json()["type"] in ("done", "error"):
                    break


def _system_text(messages):
    return "\n".join(m["content"] for m in messages if m["role"] == "system" and isinstance(m["content"], str))


def test_turn_with_tags_saves_clean_text_meta_and_state(client):
    _, sid = _chat(client)
    _turn(client, sid, TAGGED.format(loc="Таверна"))
    msgs = client.get(f"/api/sessions/{sid}/messages").json()
    ai = msgs[-1]
    assert ai["content"] == "Вольф кивнул и сел у окна." and "<horae" not in ai["swipes"][0]
    assert "Таверна" in ai["horae_brief"] and ai["horae_side"] is False
    meta = client.get(f"/api/messages/{ai['id']}/horae").json()["meta"]
    assert meta["scene"]["location"] == "Таверна" and meta["source"] == "tags"
    st = client.get(f"/api/sessions/{sid}/horae/state").json()
    assert st["enabled"] is True
    assert st["state"]["scene"]["location"] == "Таверна"
    assert st["state"]["items"][0]["name"] == "Ключ" and st["state"]["items"][0]["importance"] == "!"
    assert st["state"]["npcs"][0]["name"] == "Вольф"
    assert [t["text"] for t in st["timeline"] if t["kind"] == "event"] == ["Вольф пришёл в Таверна"]
    assert st["stats"]["injection_tokens"] > 0


def test_next_turn_context_has_rules_state_and_reminder(client):
    _, sid = _chat(client)
    _turn(client, sid, TAGGED.format(loc="Таверна"))
    seen = []
    _turn(client, sid, TAGGED.format(loc="Подвал"), text="идём дальше", seen=seen)
    messages = seen[0]
    assert "【Система памяти Horae】" in messages[0]["content"]
    system = _system_text(messages)
    assert "[Снимок текущего состояния" in system and "[Сцена|Таверна]" in system
    assert "#001 🗝Ключ[важно] | ржавый = Вольф@карман" in system
    assert "[Horae — формат ответа]" in system
    # Напоминание — последним system перед фокусом и репликой.
    tail = [m for m in messages if m["role"] == "system"][-2:]
    assert tail[0]["content"].startswith("[Horae — формат ответа]")


def test_regenerate_does_not_see_own_meta(client):
    _, sid = _chat(client)
    _turn(client, sid, TAGGED.format(loc="Таверна"))
    _turn(client, sid, TAGGED.format(loc="Подвал"), text="в подвал")
    seen = []
    _turn(client, sid, TAGGED.format(loc="Чердак"), seen=seen, kind="regenerate")
    system = _system_text(seen[0])
    assert "[Сцена|Таверна]" in system and "Подвал" not in system.split("[Снимок текущего состояния")[1]
    msgs = client.get(f"/api/sessions/{sid}/messages").json()
    ai = msgs[-1]
    assert len(ai["swipes"]) == 2 and "Чердак" in ai["horae_brief"]
    # Свайп назад — мета следует за свайпом.
    client.patch(f"/api/messages/{ai['id']}", json={"active_swipe": 0})
    st = client.get(f"/api/sessions/{sid}/horae/state").json()
    assert st["state"]["scene"]["location"] == "Подвал"


def test_disabled_for_chat_strips_tags_without_meta(client):
    _, sid = _chat(client)
    r = client.put(f"/api/sessions/{sid}/horae/settings", json={"enabled": False})
    assert r.json()["effective"]["enabled"] is False
    seen = []
    _turn(client, sid, TAGGED.format(loc="Таверна"), seen=seen)
    assert "Система памяти Horae" not in _system_text(seen[0])
    ai = client.get(f"/api/sessions/{sid}/messages").json()[-1]
    assert ai["content"] == "Вольф кивнул и сел у окна." and ai["horae_brief"] is None
    # null снимает переопределение — снова действует глобальное «включено».
    r = client.put(f"/api/sessions/{sid}/horae/settings", json={"enabled": None})
    assert r.json()["overrides"] == {} and r.json()["effective"]["enabled"] is True


def test_ops_journal_and_undo(client):
    _, sid = _chat(client)
    _turn(client, sid, TAGGED.format(loc="Таверна"))
    r = client.post(f"/api/sessions/{sid}/horae/ops", json={"kind": "npc.add", "name": "Кай",
                                                             "fields": {"job": "моряк"}})
    op_id = r.json()["op"]["id"]
    st = client.get(f"/api/sessions/{sid}/horae/state").json()
    kai = next(n for n in st["state"]["npcs"] if n["name"] == "Кай")
    assert kai["job"] == "моряк" and st["ops"][-1]["label"] == "Добавлен NPC «Кай»"
    assert client.delete(f"/api/sessions/{sid}/horae/ops/{op_id}").json()["ok"]
    st = client.get(f"/api/sessions/{sid}/horae/state").json()
    assert all(n["name"] != "Кай" for n in st["state"]["npcs"])
    assert client.post(f"/api/sessions/{sid}/horae/ops", json={"kind": "bogus"}).status_code == 400
    assert client.post(f"/api/sessions/{sid}/horae/ops", json={"kind": "npc.add"}).status_code == 400
    client.post(f"/api/sessions/{sid}/horae/ops", json={"kind": "npc.pin", "name": "Вольф", "pinned": True})
    st = client.get(f"/api/sessions/{sid}/horae/state").json()
    assert next(n for n in st["state"]["npcs"] if n["name"] == "Вольф")["pinned"] is True


def test_events_and_manual_summaries(client):
    _, sid = _chat(client)
    _turn(client, sid, TAGGED.format(loc="Таверна"))
    _turn(client, sid, TAGGED.format(loc="Подвал"), text="в подвал")
    msgs = client.get(f"/api/sessions/{sid}/messages").json()
    ai_ids = [m["id"] for m in msgs if m["role"] == "assistant" and m["horae_brief"]]
    first = ai_ids[0]
    client.post(f"/api/sessions/{sid}/horae/events", json={"mid": first, "level": "critical", "text": "Клятва"})
    client.patch(f"/api/sessions/{sid}/horae/events", json={"mid": first, "i": 0, "text": "Пришёл в таверну"})
    tl = client.get(f"/api/sessions/{sid}/horae/state").json()["timeline"]
    assert [e["text"] for e in tl if e["kind"] == "event"][:2] == ["Пришёл в таверну", "Клятва"]
    r = client.post(f"/api/sessions/{sid}/horae/summaries",
                    json={"from_mid": msgs[0]["id"], "to_mid": first, "text": "Начало истории"})
    summary_id = r.json()["summary"]["id"]
    st = client.get(f"/api/sessions/{sid}/horae/state").json()
    events = [e for e in st["timeline"] if e["kind"] == "event" and e["mid"] == first]
    assert all(e["covered_by"] == summary_id for e in events)
    client.patch(f"/api/sessions/{sid}/horae/summaries/{summary_id}", json={"active": False})
    st = client.get(f"/api/sessions/{sid}/horae/state").json()
    assert all(e["covered_by"] is None for e in st["timeline"] if e["kind"] == "event")
    client.delete(f"/api/sessions/{sid}/horae/summaries/{summary_id}")
    removed = client.post(f"/api/sessions/{sid}/horae/events/delete",
                          json={"refs": [{"mid": first, "i": 0}, {"mid": first, "i": 1}]}).json()["removed"]
    assert removed == 2


def test_summary_covers_window(client):
    """Активная свёртка позволяет окну выбросить покрытые сообщения (summary_hides)."""
    from backend import horae_engine as he

    entries = [(i, "assistant", None, False) for i in (1, 2, 3, 4, 5)]
    summaries = [{"id": "s_1", "kind": "auto", "range": [1, 3], "active": True}]
    assert he.covered_pointer(entries, summaries, 0) == 3
    assert he.covered_pointer(entries, [{**summaries[0], "active": False}], 0) == 0
    assert he.covered_pointer(entries, summaries, 4) == 4


def test_tables_from_tags_and_api(client):
    _, sid = _chat(client)
    r = client.post(f"/api/sessions/{sid}/horae/tables", json={"name": "Квесты", "rows": 3, "cols": 3})
    tid = r.json()["id"]
    client.patch(f"/api/sessions/{sid}/horae/tables/{tid}", json={"cell": {"r": 0, "c": 1, "value": "Цель"}})
    seen = []
    _turn(client, sid, "Ок.\n<horaetable:Квесты>\n1,1:Найти ключ\n</horaetable>", seen=seen)
    assert "Правила пользовательских таблиц" in seen[0][0]["content"]
    table = client.get(f"/api/sessions/{sid}/horae/state").json()["tables"][0]
    assert table["data"]["1,1"] == "Найти ключ" and table["data"]["0,1"] == "Цель"
    client.patch(f"/api/sessions/{sid}/horae/tables/{tid}", json={"lock": {"type": "row", "r": 1, "locked": True}})
    _turn(client, sid, "Ок.\n<horaetable:Квесты>\n1,1:Другое\n2,1:Второй квест\n</horaetable>", text="ещё")
    table = client.get(f"/api/sessions/{sid}/horae/state").json()["tables"][0]
    assert table["data"]["1,1"] == "Найти ключ" and table["data"]["2,1"] == "Второй квест"
    client.patch(f"/api/sessions/{sid}/horae/tables/{tid}",
                 json={"structure": {"op": "add_row_below", "index": 2}})
    assert client.get(f"/api/sessions/{sid}/horae/state").json()["tables"][0]["rows"] >= 4
    assert client.delete(f"/api/sessions/{sid}/horae/tables/{tid}").json()["ok"]
    assert client.get(f"/api/sessions/{sid}/horae/state").json()["tables"] == []


def test_fork_and_delete_message(client):
    _, sid = _chat(client)
    _turn(client, sid, TAGGED.format(loc="Таверна"))
    _turn(client, sid, TAGGED.format(loc="Подвал"), text="в подвал")
    msgs = client.get(f"/api/sessions/{sid}/messages").json()
    first_ai = next(m for m in msgs if m["role"] == "assistant" and m["horae_brief"])
    client.post(f"/api/sessions/{sid}/horae/ops", json={"kind": "npc.add", "name": "Кай"})
    client.post(f"/api/sessions/{sid}/horae/summaries",
                json={"from_mid": msgs[0]["id"], "to_mid": first_ai["id"], "text": "Начало"})
    fork = client.post(f"/api/sessions/{sid}/fork", json={"message_id": first_ai["id"]}).json()
    st = client.get(f"/api/sessions/{fork['session_id']}/horae/state").json()
    assert st["state"]["scene"]["location"] == "Таверна"
    # Правка сделана после развилки — в ветку не попала; свёртка до развилки — попала.
    assert all(n["name"] != "Кай" for n in st["state"]["npcs"])
    assert any(t["kind"] == "summary" for t in st["timeline"])
    # Удаление сообщения внутри свёртки снимает свёртку.
    client.delete(f"/api/messages/{first_ai['id']}")
    st = client.get(f"/api/sessions/{sid}/horae/state").json()
    assert not any(t["kind"] == "summary" for t in st["timeline"])
    assert client.delete(f"/api/sessions/{fork['session_id']}").json()["ok"]


def test_side_scene_and_message_meta_edit(client):
    _, sid = _chat(client)
    _turn(client, sid, TAGGED.format(loc="Таверна"))
    ai = client.get(f"/api/sessions/{sid}/messages").json()[-1]
    view = client.post(f"/api/messages/{ai['id']}/horae/side", json={"side": True}).json()
    assert view["side"] is True
    assert client.get(f"/api/sessions/{sid}/horae/state").json()["state"]["scene"]["location"] == ""
    client.post(f"/api/messages/{ai['id']}/horae/side", json={"side": False})
    meta = view["meta"]
    meta["scene"]["location"] = "Кухня"
    meta["events"].append({"level": "normal", "text": "Добавил руками"})
    view = client.put(f"/api/messages/{ai['id']}/horae", json={"meta": meta}).json()
    assert view["meta"]["source"] == "user" and "Кухня" in view["brief"]
    st = client.get(f"/api/sessions/{sid}/horae/state").json()
    assert st["state"]["scene"]["location"] == "Кухня"


def test_global_settings_character_profile_and_prompts(client):
    cid, sid = _chat(client)
    g = client.get("/api/horae/settings").json()
    assert g["defaults"]["enabled"] is True and "prompts" in g["effective"]
    try:
        client.put("/api/horae/settings", json={"send_mood": True})
        client.put(f"/api/characters/{cid}/horae_profile", json={"settings": {"send_mood": False,
                                                                              "rpg_enabled": True}})
        eff = client.get(f"/api/sessions/{sid}/horae/settings").json()["effective"]
        assert eff["send_mood"] is False and eff["rpg_enabled"] is True
    finally:
        client.put("/api/horae/settings", json={"send_mood": None})
    prompts = client.get("/api/horae/prompts").json()
    assert "【Система памяти Horae】" in prompts["defaults"]["system"]
    assert [p["id"] for p in prompts["presets"][:2]] == ["default", "extended"]
    preset = client.post("/api/horae/prompts/presets", json={"name": "Мой", "prompts": {"reminder": "Коротко"}}).json()
    assert preset["prompts"] == {"reminder": "Коротко"}
    client.delete(f"/api/horae/prompts/presets/{preset['id']}")
    assert all(p["name"] != "Мой" for p in client.get("/api/horae/prompts").json()["presets"])


def test_sillytavern_import_keeps_structured_horae(client):
    chat0 = {
        "timestamp": {"story_date": "2026/1/1", "story_time": "09:00"},
        "scene": {"location": "Площадь", "characters_present": ["Кай"]},
        "events": [], "items": {}, "npcs": {}, "affection": {}, "costumes": {},
        "autoSummaries": [{"id": "as_1", "range": [0, 1], "summaryText": "Встретились на площади",
                           "active": True, "depth": 1, "coveredIndices": [0, 1],
                           "originalEvents": [{"msgIdx": 0, "evtIdx": 0, "timestamp": {"story_date": "2026/1/1"}}]}],
        "locationMemory": {"Площадь": {"desc": "мощёная, с фонтаном", "_userEdited": True}},
    }
    meta2 = {
        "timestamp": {"story_date": "2026/1/2", "story_time": "18:00"},
        "scene": {"location": "Порт", "characters_present": ["Кай", "Лея"]},
        "items": {"Карта": {"icon": "🗺", "importance": "!", "holder": "Лея", "location": "сумка"}},
        "npcs": {"Кай": {"appearance": "высокий", "relationship": "проводник"}},
        "affection": {"Кай": {"type": "absolute", "value": 30}},
        "events": [{"is_important": True, "level": "重要", "summary": "Кай отдал Лее карту"}],
        "costumes": {}, "deletedItems": [], "agenda": [], "mood": {}, "relationships": [],
    }
    lines = "\n".join([
        json.dumps({"character_name": "Кай", "user_name": "Лея"}),
        json.dumps({"is_user": False, "name": "Кай", "mes": "Привет на площади!", "horae_meta": chat0}),
        json.dumps({"is_user": True, "name": "Лея", "mes": "Идём в порт"}),
        json.dumps({"is_user": False, "name": "Кай", "mes": "Вот карта.\n<horae>\nlocation:Порт\n</horae>",
                    "horae_meta": meta2}),
    ])
    r = client.post("/api/sessions/import", files={"file": ("chat.jsonl", lines, "application/json")})
    data = r.json()
    assert data["horae_structured"] >= 1 and data["horae_chat_saved"] is True
    assert data["horae_saved"] is False   # текстовый дубль состояния не создаётся
    sid = data["session_id"]
    msgs = client.get(f"/api/sessions/{sid}/messages").json()
    assert msgs[-1]["content"] == "Вот карта."
    st = client.get(f"/api/sessions/{sid}/horae/state").json()
    assert st["state"]["scene"]["location"] == "Порт"
    assert st["state"]["items"][0]["name"] == "Карта"
    assert st["state"]["affection"][0]["value"] == 30
    summary = next(t for t in st["timeline"] if t["kind"] == "summary")
    assert summary["text"] == "Встретились на площади"
    assert any(loc["name"] == "Площадь" and "фонтаном" in loc["desc"] for loc in st["state"]["locations"])


def test_native_export_roundtrip_keeps_horae(client):
    _, sid = _chat(client)
    _turn(client, sid, TAGGED.format(loc="Таверна"))
    client.post(f"/api/sessions/{sid}/horae/ops", json={"kind": "npc.add", "name": "Кай"})
    exported = client.get(f"/api/sessions/{sid}/export").json()
    assert exported["horae_chat"]["ops"][0]["name"] == "Кай"
    r = client.post("/api/sessions/import",
                    files={"file": ("chat.json", json.dumps(exported), "application/json")})
    new_sid = r.json()["session_id"]
    st = client.get(f"/api/sessions/{new_sid}/horae/state").json()
    assert st["state"]["scene"]["location"] == "Таверна"
    assert {n["name"] for n in st["state"]["npcs"]} == {"Вольф", "Кай"}


def test_horae_export_import_and_clear(client):
    _, sid = _chat(client)
    _turn(client, sid, TAGGED.format(loc="Таверна"))
    dump = client.get(f"/api/sessions/{sid}/horae/export").json()
    assert dump["type"] == "horae-chat" and dump["messages"]
    _, other = _chat(client, "Другой")
    r = client.post(f"/api/sessions/{other}/horae/import", json={"data": dump, "mode": "initial"})
    assert r.json()["ok"]
    st = client.get(f"/api/sessions/{other}/horae/state").json()
    assert st["state"]["scene"]["location"] == "Таверна"
    assert any(t["kind"] == "summary" and t["kind_detail"] == "carry" for t in st["timeline"])
    assert client.delete(f"/api/sessions/{sid}/horae").json()["messages"] >= 1
    st = client.get(f"/api/sessions/{sid}/horae/state").json()
    assert st["state"]["scene"]["location"] == ""


def test_carryover_new_chat(client):
    _, sid = _chat(client)
    for loc in ("Таверна", "Подвал", "Лес"):
        _turn(client, sid, TAGGED.format(loc=loc), text=f"идём в {loc}")
    r = client.post(f"/api/sessions/{sid}/horae/carryover", json={"keep": 1, "vectors": True})
    new_sid = r.json()["session_id"]
    msgs = client.get(f"/api/sessions/{new_sid}/messages").json()
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    st = client.get(f"/api/sessions/{new_sid}/horae/state").json()
    assert st["state"]["scene"]["location"] == "Лес"
    assert any(n["name"] == "Вольф" for n in st["state"]["npcs"])
    recap = [t for t in st["timeline"] if t["kind"] == "summary"]
    assert recap and "Таверна" in recap[0]["text"]


def test_horae_endpoints_check_chat_access(client):
    """Чужой пользователь не видит и не правит данные Horae приватного чата."""
    a = client.post("/api/auth/register", json={"username": "horae_se_a", "password": "pw"}).json()
    b = client.post("/api/auth/register", json={"username": "horae_se_b", "password": "pw"}).json()
    ah = {"X-User-Token": a["token"]}
    bh = {"X-User-Token": b["token"]}
    client.put("/api/admin/security", json={"accounts_enabled": True, "admin_password": "horae_se_pw"})
    try:
        cid = client.post("/api/characters", json={"name": "HoraeSE"}, headers=ah).json()["id"]
        sid = client.post(f"/api/sessions?character_id={cid}", headers=ah).json()["session_id"]
        with patch("backend.llm_gateway.litellm.acompletion", new=_llm(TAGGED.format(loc="Таверна"))):
            with client.websocket_connect(f"/ws/chat/{sid}?token={a['token']}") as ws:
                ws.send_json({"type": "user_message", "content": "привет"})
                for _ in range(100):
                    if ws.receive_json()["type"] in ("done", "error"):
                        break
        ai = client.get(f"/api/sessions/{sid}/messages", headers=ah).json()[-1]
        assert client.get(f"/api/sessions/{sid}/horae/state", headers=ah).status_code == 200
        assert client.get(f"/api/sessions/{sid}/horae/state", headers=bh).status_code == 403
        assert client.get(f"/api/messages/{ai['id']}/horae", headers=bh).status_code == 403
        assert client.put(f"/api/messages/{ai['id']}/horae", json={"meta": None}, headers=bh).status_code == 403
        assert client.post(f"/api/sessions/{sid}/horae/ops", json={"kind": "npc.add", "name": "X"},
                           headers=bh).status_code == 403
        assert client.get(f"/api/sessions/{sid}/horae/export", headers=bh).status_code == 403
        assert client.delete(f"/api/sessions/{sid}/horae", headers=bh).status_code == 403
        # Глобальные настройки и общую библиотеку Horae меняет только администратор.
        assert client.put("/api/horae/settings", json={"send_mood": True}, headers=bh).status_code == 403
        assert client.post("/api/horae/prompts/presets", json={"name": "x", "prompts": {}},
                           headers=bh).status_code == 403
        assert client.put("/api/horae/equipment_templates", json={"custom": []}, headers=bh).status_code == 403
    finally:
        client.put(
            "/api/admin/security",
            json={"accounts_enabled": False, "admin_password": ""},
            headers={**ah, "X-Admin-Password": "horae_se_pw"},
        )
