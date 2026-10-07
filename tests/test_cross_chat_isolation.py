"""
Изоляция чатов: то, что обсуждали в одном чате, не должно всплывать в другом.

Каналы, через которые чат «вспоминал» чужой разговор:
  * ответ на сообщение (reply_to_message_id) с id из ДРУГОГО чата — браузер не
    сбрасывал выбор при переходе, и модель получала «(В ответ на …: «чужой текст»)»;
  * общий шаблон таблицы Horae (global/character): правка любой ячейки
    переписывала шаблон заголовками из текущих данных чата, включая подписи
    строк, которые вписал ИИ, — и они появлялись во всех чатах;
  * лорбук удалённого персонажа: SQLite отдаёт id удалённой строки новому
    персонажу, и тот наследовал чужие записи;
  * список лорбука: показывал записи всех чатов (for_session отдаёт только те,
    что действуют в открытом чате).
"""
from unittest.mock import patch


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


def _chat(client, cid=None, name="Изолда"):
    if cid is None:
        cid = client.post("/api/characters", json={"name": name}).json()["id"]
    sid = client.post(f"/api/sessions?character_id={cid}").json()["session_id"]
    return cid, sid


def _turn(client, sid, reply, text="привет", seen=None, reply_to=None):
    payload = {"type": "user_message", "content": text}
    if reply_to is not None:
        payload["reply_to_message_id"] = reply_to
    with patch("backend.llm_gateway.litellm.acompletion", new=_llm(reply, seen)):
        with client.websocket_connect(f"/ws/chat/{sid}") as ws:
            ws.send_json(payload)
            for _ in range(100):
                if ws.receive_json()["type"] in ("done", "error"):
                    break


def _all_text(messages):
    out = []
    for m in messages:
        c = m.get("content")
        if isinstance(c, str):
            out.append(c)
        elif isinstance(c, list):
            out.extend(b.get("text") or "" for b in c if isinstance(b, dict))
    return "\n".join(out)


def test_reply_to_message_of_other_chat_is_ignored(client):
    cid, chat_a = _chat(client)
    _, chat_b = _chat(client, cid)
    _turn(client, chat_a, "Жду.", text="Скину тебе порты JSON по Dungeons and Dragons")
    foreign = client.get(f"/api/sessions/{chat_a}/messages").json()[-1]["id"]

    seen = []
    _turn(client, chat_b, "Привет!", text="Как дела?", seen=seen, reply_to=foreign)
    assert seen, "модель не вызывалась"
    assert "JSON" not in _all_text(seen[0]) and "В ответ на" not in _all_text(seen[0])
    mine = [m for m in client.get(f"/api/sessions/{chat_b}/messages").json() if m["role"] == "user"]
    assert mine[-1]["reply_to_id"] is None

    # Ответ в пределах своего чата по-прежнему доходит до модели.
    own = client.get(f"/api/sessions/{chat_b}/messages").json()[-1]["id"]
    seen = []
    _turn(client, chat_b, "Ок.", text="Про это", seen=seen, reply_to=own)
    assert "В ответ на" in _all_text(seen[0])


def _state_table(client, sid, tid):
    return next(t for t in client.get(f"/api/sessions/{sid}/horae/state").json()["tables"] if t["id"] == tid)


def test_shared_table_keeps_chat_row_labels_in_chat(client):
    cid, chat_a = _chat(client, name="Табличный")
    _, chat_b = _chat(client, cid)
    tid = client.post(f"/api/sessions/{chat_a}/horae/tables",
                      json={"name": "Герои", "rows": 3, "cols": 3, "scope": "global"}).json()["id"]
    try:
        # Заголовок столбца — схема таблицы: общий для всех чатов.
        client.patch(f"/api/sessions/{chat_a}/horae/tables/{tid}", json={"cell": {"r": 0, "c": 1, "value": "Роль"}})
        assert _state_table(client, chat_b, tid)["data"].get("0,1") == "Роль"

        # ИИ в чате A вписал строку: подпись строки (столбец 0) — данные этого чата.
        _turn(client, chat_a, "Ок.\n<horaetable:Герои>\n1,0:Драконоборец\n1,1:маг\n</horaetable>")
        assert _state_table(client, chat_a, tid)["data"].get("1,0") == "Драконоборец"
        # Пользователь правит обычную ячейку в чате A — раньше это уносило
        # «Драконоборца» в общий шаблон, и он появлялся в чате B.
        client.patch(f"/api/sessions/{chat_a}/horae/tables/{tid}", json={"cell": {"r": 1, "c": 1, "value": "воин"}})
        client.patch(f"/api/sessions/{chat_a}/horae/tables/{tid}", json={"name": "Герои"})
        client.patch(f"/api/sessions/{chat_a}/horae/tables/{tid}",
                     json={"structure": {"op": "add_col_right", "index": 2}})
        a = _state_table(client, chat_a, tid)["data"]
        assert a.get("1,0") == "Драконоборец" and a.get("1,1") == "воин" and a.get("0,1") == "Роль"
        b = _state_table(client, chat_b, tid)["data"]
        assert "1,0" not in b and "1,1" not in b and b.get("0,1") == "Роль"

        # ИИ чата B может вписать свою подпись строки — чужая её не занимает.
        _turn(client, chat_b, "Ок.\n<horaetable:Герои>\n1,0:Лучница\n</horaetable>")
        assert _state_table(client, chat_b, tid)["data"].get("1,0") == "Лучница"
        assert _state_table(client, chat_a, tid)["data"].get("1,0") == "Драконоборец"

        # Закреплённая подпись строки — явное решение: она становится общей.
        client.patch(f"/api/sessions/{chat_a}/horae/tables/{tid}",
                     json={"cell": {"r": 2, "c": 0, "value": "Итого"}})
        client.patch(f"/api/sessions/{chat_a}/horae/tables/{tid}", json={"lock": {"type": "row", "r": 2, "locked": True}})
        assert _state_table(client, chat_b, tid)["data"].get("2,0") == "Итого"
    finally:
        client.delete(f"/api/sessions/{chat_a}/horae/tables/{tid}")


def test_promoting_local_table_shares_only_column_headers(client):
    cid, chat_a = _chat(client, name="Повышатель")
    _, chat_b = _chat(client, cid)
    tid = client.post(f"/api/sessions/{chat_a}/horae/tables", json={"name": "Квесты", "rows": 3, "cols": 3}).json()["id"]
    try:
        client.patch(f"/api/sessions/{chat_a}/horae/tables/{tid}", json={"cell": {"r": 0, "c": 1, "value": "Цель"}})
        _turn(client, chat_a, "Ок.\n<horaetable:Квесты>\n1,0:Порты JSON\n1,1:скинуть\n</horaetable>")
        client.patch(f"/api/sessions/{chat_a}/horae/tables/{tid}", json={"scope": "global"})
        a = _state_table(client, chat_a, tid)
        assert a["scope"] == "global" and a["data"].get("1,0") == "Порты JSON"
        b = _state_table(client, chat_b, tid)["data"]
        assert b.get("0,1") == "Цель" and "1,0" not in b and "1,1" not in b
    finally:
        client.delete(f"/api/sessions/{chat_a}/horae/tables/{tid}")


def test_deleting_character_removes_its_lorebook(client):
    cid = client.post("/api/characters", json={"name": "Удаляемый"}).json()["id"]
    entry = client.post("/api/horae", json={"title": "лор удалённого", "character_id": cid}).json()
    client.delete(f"/api/characters/{cid}")
    assert entry["id"] not in {h["id"] for h in client.get("/api/horae").json()}


def test_lorebook_for_session_lists_only_entries_of_that_chat(client):
    cid, chat_a = _chat(client, name="Лорный")
    other_cid, chat_b = _chat(client, name="Чужой")
    mine = client.post("/api/horae", json={"title": "только A", "session_id": chat_a}).json()["id"]
    theirs = client.post("/api/horae", json={"title": "только B", "session_id": chat_b}).json()["id"]
    char = client.post("/api/horae", json={"title": "лор A", "character_id": cid}).json()["id"]
    other_char = client.post("/api/horae", json={"title": "лор B", "character_id": other_cid}).json()["id"]
    everywhere = client.post("/api/horae", json={"title": "мир"}).json()["id"]
    try:
        ids = {h["id"] for h in client.get(f"/api/horae?for_session={chat_a}").json()}
        assert {mine, char, everywhere} <= ids and not ({theirs, other_char} & ids)

        # Запись переносится между «этот чат» и «все чаты».
        moved = client.patch(f"/api/horae/{everywhere}", json={"scope": "session", "session_id": chat_b}).json()
        assert moved["session_id"] == chat_b
        assert everywhere not in {h["id"] for h in client.get(f"/api/horae?for_session={chat_a}").json()}
        back = client.patch(f"/api/horae/{everywhere}", json={"scope": "global"}).json()
        assert back["session_id"] is None and back["character_id"] is None
    finally:
        for i in (mine, theirs, char, other_char, everywhere):
            client.delete(f"/api/horae/{i}")
