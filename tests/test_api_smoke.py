"""
Интеграционные («дымовые») тесты REST + WebSocket через FastAPI TestClient.

Реальные вызовы к LLM замоканы. Проверяем сквозной путь: создание персонажа,
сессия с приветствием, настройки подключения, и полный цикл по WebSocket с
сохранением ответа ассистента (включая свайпы).
"""
import json
from unittest.mock import patch

# Фикстура `client` определена в tests/conftest.py и доступна всем тестам.


def _fake_chunk(text: str):
    class _Delta:
        content = text

    class _Choice:
        delta = _Delta()

    class _Chunk:
        choices = [_Choice()]

    return _Chunk()


async def _fake_acompletion(*args, **kwargs):
    async def gen():
        for token in ["Прив", "ет", "!"]:
            yield _fake_chunk(token)

    return gen()


async def _fake_aimage(*args, **kwargs):
    class _Item:
        url = "http://img/test.png"

    class _Resp:
        data = [_Item()]

    return _Resp()


def test_health_and_static_index(client):
    assert client.get("/api/health").json()["status"] == "ok"
    # Веб-интерфейс отдаётся сервером с корня (StaticFiles).
    r = client.get("/")
    assert r.status_code == 200
    assert "TaleEngine" in r.text


def test_character_and_session_with_greeting(client):
    created = client.post(
        "/api/characters", json={"name": "Smoke", "first_message": "Привет, гость!"}
    ).json()
    cid = created["id"]
    assert any(c["id"] == cid for c in client.get("/api/characters").json())

    client.patch(f"/api/characters/{cid}", json={"description": "обновлено"})

    sid = client.post(f"/api/sessions?character_id={cid}").json()["session_id"]
    msgs = client.get(f"/api/sessions/{sid}/messages").json()
    # Первое сообщение персонажа показывается сразу (как в SillyTavern).
    assert msgs and msgs[0]["role"] == "assistant"
    assert "Привет, гость!" in msgs[0]["content"]


def test_connection_settings_roundtrip(client):
    conn = client.get("/api/settings/connection").json()
    assert "base_url" in conn and "use_proxy" in conn
    updated = client.put(
        "/api/settings/connection",
        json={
            "use_proxy": True,
            "base_url": "http://localhost:4000",
            "api_key": "sk-test",
            "default_model": "gpt-4o",
        },
    ).json()
    assert updated["default_model"] == "gpt-4o"
    assert updated["base_url"] == "http://localhost:4000"


def test_websocket_user_turn_streams_and_persists(client):
    cid = client.post("/api/characters", json={"name": "WS"}).json()["id"]
    sid = client.post(f"/api/sessions?character_id={cid}").json()["session_id"]

    with patch("backend.llm_gateway.litellm.acompletion", new=_fake_acompletion):
        with client.websocket_connect(f"/ws/chat/{sid}") as ws:
            ws.send_json({"type": "user_message", "content": "привет"})
            collected = ""
            for _ in range(50):
                ev = ws.receive_json()
                if ev["type"] == "token":
                    collected += ev["content"]
                if ev["type"] in ("done", "error"):
                    break

    assert collected == "Привет!"
    msgs = client.get(f"/api/sessions/{sid}/messages").json()
    assistant_msgs = [m for m in msgs if m["role"] == "assistant"]
    assert assistant_msgs and assistant_msgs[-1]["content"] == "Привет!"
    # Ответ сохранён как первый свайп (для механики альтернативных вариантов).
    assert assistant_msgs[-1]["swipes"] == ["Привет!"]


def test_ui_settings_roundtrip(client):
    client.put("/api/settings/ui", json={"params": {"temperature": 0.3}})
    ui = client.get("/api/settings/ui").json()
    assert ui["params"]["temperature"] == 0.3


def test_chat_import_endpoint(client):
    lines = "\n".join(
        [
            json.dumps({"character_name": "ImportChar", "user_name": "U"}),
            json.dumps({"is_user": False, "mes": "greeting"}),
            json.dumps({"is_user": True, "mes": "hello"}),
        ]
    )
    r = client.post(
        "/api/sessions/import", files={"file": ("chat.jsonl", lines, "application/json")}
    )
    assert r.status_code == 200
    data = r.json()
    assert data["count"] == 2
    msgs = client.get(f"/api/sessions/{data['session_id']}/messages").json()
    assert msgs[0]["content"] == "greeting"


def test_image_generation_endpoint(client):
    cid = client.post("/api/characters", json={"name": "Art"}).json()["id"]
    sid = client.post(f"/api/sessions?character_id={cid}").json()["session_id"]
    with patch("backend.llm_gateway.litellm.aimage_generation", new=_fake_aimage):
        r = client.post(f"/api/sessions/{sid}/image", json={"prompt": "a cat"})
    assert r.status_code == 200
    msgs = client.get(f"/api/sessions/{sid}/messages").json()
    assert any("http://img/test.png" in m["content"] for m in msgs)


def test_art_from_message(client):
    cid = client.post(
        "/api/characters", json={"name": "ArtMsg", "first_message": "тёмный лес ночью"}
    ).json()["id"]
    sid = client.post(f"/api/sessions?character_id={cid}").json()["session_id"]
    mid = client.get(f"/api/sessions/{sid}/messages").json()[0]["id"]
    with patch("backend.llm_gateway.litellm.acompletion", new=_fake_acompletion), patch(
        "backend.llm_gateway.litellm.aimage_generation", new=_fake_aimage
    ):
        r = client.post(
            f"/api/sessions/{sid}/image", json={"mode": "scene", "from_message_id": mid}
        )
    assert r.status_code == 200
    out = client.get(f"/api/sessions/{sid}/messages").json()
    assert any("http://img/test.png" in m["content"] for m in out)


def test_art_prompt_with_attachments(client):
    cid = client.post("/api/characters", json={"name": "ArtAtt"}).json()["id"]
    sid = client.post(f"/api/sessions?character_id={cid}").json()["session_id"]
    with patch("backend.llm_gateway.litellm.acompletion", new=_fake_acompletion), patch(
        "backend.llm_gateway.litellm.aimage_generation", new=_fake_aimage
    ):
        r = client.post(
            f"/api/sessions/{sid}/image",
            json={
                "mode": "prompt",
                "prompt": "девушка в плаще",
                "attachments": [{"type": "image", "data": "data:image/png;base64,AAAA"}],
            },
        )
    assert r.status_code == 200
    out = client.get(f"/api/sessions/{sid}/messages").json()
    assert any("http://img/test.png" in m["content"] for m in out)


def test_default_preset(client):
    client.post("/api/presets", json={"name": "P1", "params": {"temperature": 0.2}})
    p2 = client.post("/api/presets", json={"name": "P2", "params": {"temperature": 0.8}}).json()
    client.post(f"/api/presets/{p2['id']}/default")
    presets = client.get("/api/presets").json()
    defaults = [p for p in presets if p["is_default"]]
    assert len(defaults) == 1 and defaults[0]["id"] == p2["id"]


def test_session_background_roundtrip(client):
    cid = client.post("/api/characters", json={"name": "BG"}).json()["id"]
    sid = client.post(f"/api/sessions?character_id={cid}").json()["session_id"]
    client.patch(f"/api/sessions/{sid}", json={"background": "linear-gradient(#000,#111)"})
    sessions = client.get(f"/api/sessions?character_id={cid}").json()
    assert any(s["id"] == sid and s["background"] == "linear-gradient(#000,#111)" for s in sessions)


def test_art_scene_mode_uses_context(client):
    cid = client.post("/api/characters", json={"name": "Scene"}).json()["id"]
    sid = client.post(f"/api/sessions?character_id={cid}").json()["session_id"]
    with patch("backend.llm_gateway.litellm.acompletion", new=_fake_acompletion), patch(
        "backend.llm_gateway.litellm.aimage_generation", new=_fake_aimage
    ):
        r = client.post(f"/api/sessions/{sid}/image", json={"prompt": "", "mode": "scene"})
    assert r.status_code == 200
    msgs = client.get(f"/api/sessions/{sid}/messages").json()
    assert any("http://img/test.png" in m["content"] for m in msgs)


def test_debug_log_records_chat(client):
    cid = client.post("/api/characters", json={"name": "Dbg"}).json()["id"]
    sid = client.post(f"/api/sessions?character_id={cid}").json()["session_id"]
    client.delete("/api/debug/log")
    with patch("backend.llm_gateway.litellm.acompletion", new=_fake_acompletion):
        with client.websocket_connect(f"/ws/chat/{sid}") as ws:
            ws.send_json({"type": "user_message", "content": "hi"})
            for _ in range(50):
                if ws.receive_json()["type"] in ("done", "error"):
                    break
    log = client.get("/api/debug/log").json()
    assert any(e["kind"] == "chat" and e["status"] == "ok" for e in log)
    assert any("messages" in e for e in log)  # есть сводка по сообщениям


def test_group_create_and_list(client):
    a = client.post("/api/characters", json={"name": "X1"}).json()
    b = client.post("/api/characters", json={"name": "X2"}).json()
    g = client.post(
        "/api/groups", json={"name": "GG", "character_ids": [a["id"], b["id"]]}
    ).json()
    groups = client.get("/api/groups").json()
    grp = [x for x in groups if x["id"] == g["session_id"]][0]
    assert grp["title"] == "GG"
    assert len(grp["members"]) == 2


def test_group_turn_one_speaker_responds(client):
    a = client.post("/api/characters", json={"name": "Алиса"}).json()
    b = client.post("/api/characters", json={"name": "Боб"}).json()
    g = client.post(
        "/api/groups",
        json={"name": "G", "character_ids": [a["id"], b["id"]], "director": False},
    ).json()
    sid = g["session_id"]
    with patch("backend.llm_gateway.litellm.acompletion", new=_fake_acompletion):
        with client.websocket_connect(f"/ws/chat/{sid}") as ws:
            ws.send_json({"type": "user_message", "content": "привет всем"})
            speakers = []
            for _ in range(100):
                ev = ws.receive_json()
                if ev["type"] == "speaker":
                    speakers.append(ev["name"])
                if ev["type"] in ("done", "error"):
                    break
    assert speakers, "хотя бы один персонаж должен ответить"
    msgs = client.get(f"/api/sessions/{sid}/messages").json()
    labeled = [m for m in msgs if m["role"] == "assistant" and m["speaker_name"]]
    assert labeled and labeled[-1]["content"] == "Привет!"


def test_group_mention_targets_named_character(client):
    a = client.post("/api/characters", json={"name": "Алиса"}).json()
    b = client.post("/api/characters", json={"name": "Боб"}).json()
    g = client.post(
        "/api/groups", json={"name": "G", "character_ids": [a["id"], b["id"]]}
    ).json()
    sid = g["session_id"]
    with patch("backend.llm_gateway.litellm.acompletion", new=_fake_acompletion):
        with client.websocket_connect(f"/ws/chat/{sid}") as ws:
            ws.send_json({"type": "user_message", "content": "Боб, что скажешь?"})
            speakers = []
            for _ in range(100):
                ev = ws.receive_json()
                if ev["type"] == "speaker":
                    speakers.append(ev["name"])
                if ev["type"] in ("done", "error"):
                    break
    assert speakers == ["Боб"]  # ответил только упомянутый


def test_reply_to_message_saved(client):
    cid = client.post("/api/characters", json={"name": "Rep", "first_message": "привет"}).json()["id"]
    sid = client.post(f"/api/sessions?character_id={cid}").json()["session_id"]
    mid = client.get(f"/api/sessions/{sid}/messages").json()[0]["id"]  # приветствие
    with patch("backend.llm_gateway.litellm.acompletion", new=_fake_acompletion):
        with client.websocket_connect(f"/ws/chat/{sid}") as ws:
            ws.send_json({"type": "user_message", "content": "мой ответ", "reply_to_message_id": mid})
            for _ in range(50):
                if ws.receive_json()["type"] in ("done", "error"):
                    break
    msgs = client.get(f"/api/sessions/{sid}/messages").json()
    user_msg = [m for m in msgs if m["role"] == "user"][-1]
    assert user_msg["reply_to_id"] == mid


def test_http_send_with_attachment_streams_via_sse(client):
    """
    Большое вложение не влезает в WebSocket-кадр (~16 МБ) → ход уходит по HTTP
    (`/send`), ответ слушается по SSE. Проверяем: job создаётся, сообщение с
    вложением сохраняется, ответ ассистента появляется.
    """
    cid = client.post("/api/characters", json={"name": "Аудио"}).json()["id"]
    sid = client.post(f"/api/sessions?character_id={cid}").json()["session_id"]
    # Крупноватое «аудио» (имитация): data-URI с большим base64.
    big_audio = "data:audio/webm;base64," + ("A" * 100000)
    with patch("backend.llm_gateway.litellm.acompletion", new=_fake_acompletion):
        r = client.post(
            f"/api/sessions/{sid}/send",
            json={"content": "послушай", "attachments": [
                {"type": "audio", "data": big_audio, "mime": "audio/webm", "name": "Голос"}
            ]},
        )
        assert r.status_code == 200
        job_id = r.json()["job_id"]
        assert job_id
        # Дослушиваем задачу по SSE до конца.
        with client.stream("GET", f"/sse/job/{job_id}") as resp:
            assert resp.status_code == 200
            done = False
            for line in resp.iter_lines():
                if line and '"done"' in line or (line and '"error"' in line):
                    done = True
                    break
            assert done
    msgs = client.get(f"/api/sessions/{sid}/messages").json()
    user_msg = [m for m in msgs if m["role"] == "user"][-1]
    assert user_msg["content"] == "послушай"
    assert user_msg["attachments"] and user_msg["attachments"][0]["type"] == "audio"
    assert any(m["role"] == "assistant" for m in msgs)  # ответ пришёл


def test_cancel_job_endpoint(client):
    """POST /api/jobs/{id}/cancel не падает (job уже мог завершиться/не существовать)."""
    assert client.post("/api/jobs/nonexistent/cancel").status_code == 200


def test_attachment_served_separately_not_in_list(client):
    """
    Оптимизация загрузки: base64-данные вложений НЕ в списке сообщений (иначе чат
    весит десятки МБ), а отдаются отдельным кэшируемым эндпоинтом по мере показа.
    """
    import base64

    cid = client.post("/api/characters", json={"name": "Влож"}).json()["id"]
    sid = client.post(f"/api/sessions?character_id={cid}").json()["session_id"]
    raw = b"\x89PNG\r\n\x1a\n" + b"fake-image-bytes" * 50
    data_uri = "data:image/png;base64," + base64.b64encode(raw).decode()
    with patch("backend.llm_gateway.litellm.acompletion", new=_fake_acompletion):
        r = client.post(
            f"/api/sessions/{sid}/send",
            json={"content": "смотри фото", "attachments": [
                {"type": "image", "data": data_uri, "mime": "image/png", "name": "p.png"}
            ]},
        )
        jid = r.json()["job_id"]
        with client.stream("GET", f"/sse/job/{jid}") as resp:
            for line in resp.iter_lines():
                if line and ('"done"' in line or '"error"' in line):
                    break
    msgs = client.get(f"/api/sessions/{sid}/messages").json()
    umsg = [m for m in msgs if m["role"] == "user"][-1]
    att = umsg["attachments"][0]
    # Мета есть, тяжёлого base64 `data` — НЕТ.
    assert att["type"] == "image" and att["mime"] == "image/png" and att["name"] == "p.png"
    assert "data" not in att and att["size"] > 0

    # Байты отдаёт отдельный эндпоинт с правильным content-type и кэшем.
    resp = client.get(f"/api/messages/{umsg['id']}/att/0")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("image/png")
    assert "max-age" in resp.headers.get("cache-control", "")
    assert resp.content == raw

    # Несуществующий индекс/сообщение — 404.
    assert client.get(f"/api/messages/{umsg['id']}/att/9").status_code == 404
    assert client.get("/api/messages/999999/att/0").status_code == 404


def test_messages_pagination(client):
    """Ленивая подгрузка: limit=последние N; before=<id> — порция старше него."""
    cid = client.post("/api/characters", json={"name": "Пейдж"}).json()["id"]
    sid = client.post(f"/api/sessions?character_id={cid}").json()["session_id"]
    # Наполняем чат сообщениями напрямую через PATCH? Нет — шлём по HTTP /send с мок-LLM.
    with patch("backend.llm_gateway.litellm.acompletion", new=_fake_acompletion):
        for i in range(12):
            r = client.post(f"/api/sessions/{sid}/send", json={"content": f"msg{i}"})
            jid = r.json()["job_id"]
            with client.stream("GET", f"/sse/job/{jid}") as resp:
                for line in resp.iter_lines():
                    if line and ('"done"' in line or '"error"' in line):
                        break
    all_msgs = client.get(f"/api/sessions/{sid}/messages").json()
    assert len(all_msgs) == 24  # 12 user + 12 assistant

    # limit=5 → последние 5 (в хронологическом порядке).
    last5 = client.get(f"/api/sessions/{sid}/messages?limit=5").json()
    assert len(last5) == 5
    assert [m["id"] for m in last5] == [m["id"] for m in all_msgs[-5:]]

    # before=<id первого из last5> & limit=5 → 5 сообщений СТАРШЕ него.
    older = client.get(f"/api/sessions/{sid}/messages?before={last5[0]['id']}&limit=5").json()
    assert len(older) == 5
    assert [m["id"] for m in older] == [m["id"] for m in all_msgs[-10:-5]]
    assert older[-1]["id"] < last5[0]["id"]  # порядок соблюдён


async def _fail_acompletion(*args, **kwargs):
    raise RuntimeError("LLM недоступен")


def _empty_chunk(finish_reason=None):
    """Чанк без контента (как при блокировке фильтрами у Gemini)."""
    class _Delta:
        content = None

    class _Choice:
        delta = _Delta()

    _Choice.finish_reason = finish_reason

    class _Chunk:
        choices = [_Choice()]

    return _Chunk()


async def _empty_acompletion(*args, **kwargs):
    async def gen():
        yield _empty_chunk()
        yield _empty_chunk(finish_reason="content_filter")

    return gen()


def test_empty_llm_response_is_explicit_error(client):
    """
    Модель «успешно» вернула ноль токенов (фильтры/лимит размышлений): раньше это
    было ТИХОЕ ничего (нет ни ответа, ни ошибки). Теперь — явная ошибка в
    отладочном логе, ответ в чат не пишется, реплика юзера остаётся для retry.
    """
    cid = client.post("/api/characters", json={"name": "Пустой"}).json()["id"]
    sid = client.post(f"/api/sessions?character_id={cid}").json()["session_id"]
    client.delete("/api/debug/log")
    with patch("backend.llm_gateway.litellm.acompletion", new=_empty_acompletion):
        with client.websocket_connect(f"/ws/chat/{sid}") as ws:
            ws.send_json({"type": "user_message", "content": "привет"})
            for _ in range(50):
                if ws.receive_json()["type"] in ("done", "error"):
                    break
    # Ответа нет, но причина зафиксирована явно (не молчим).
    msgs = client.get(f"/api/sessions/{sid}/messages").json()
    assert [m["role"] for m in msgs] == ["user"]
    log = client.get("/api/debug/log").json()
    entry = log[0]
    assert entry["status"] == "error"
    assert "ПУСТОЙ ответ" in entry["error"]
    assert "content_filter" in entry["error"]  # finish_reason попал в объяснение


def test_retry_after_failed_turn_no_user_duplicate(client):
    """
    Retry после сбоя: реплика юзера уже в БД (ответ не родился) → «retry» отвечает
    на неё заново, НЕ дублируя её; если ответ есть → новый свайп (regenerate).
    """
    cid = client.post("/api/characters", json={"name": "Ретрай"}).json()["id"]
    sid = client.post(f"/api/sessions?character_id={cid}").json()["session_id"]

    # 1. Ход, в котором LLM падает: сообщение юзера сохранено, ответа нет.
    # (Событие error может уйти в эфир ДО подписки клиента — мгновенный фейл;
    # поэтому проверяем не событие, а итоговое состояние чата.)
    with patch("backend.llm_gateway.litellm.acompletion", new=_fail_acompletion):
        with client.websocket_connect(f"/ws/chat/{sid}") as ws:
            ws.send_json({"type": "user_message", "content": "ответь мне"})
            for _ in range(50):
                if ws.receive_json()["type"] in ("done", "error"):
                    break
    msgs = client.get(f"/api/sessions/{sid}/messages").json()
    assert [m["role"] for m in msgs] == ["user"]  # ответ так и не родился

    # 2. Retry: LLM ожил — ответ появляется, реплика юзера НЕ задвоилась.
    with patch("backend.llm_gateway.litellm.acompletion", new=_fake_acompletion):
        with client.websocket_connect(f"/ws/chat/{sid}") as ws:
            ws.send_json({"type": "retry"})
            for _ in range(50):
                if ws.receive_json()["type"] in ("done", "error"):
                    break
    msgs = client.get(f"/api/sessions/{sid}/messages").json()
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[-1]["content"] == "Привет!"

    # 3. Retry при уже имеющемся ответе = новый свайп (не новое сообщение).
    with patch("backend.llm_gateway.litellm.acompletion", new=_fake_acompletion):
        with client.websocket_connect(f"/ws/chat/{sid}") as ws:
            ws.send_json({"type": "retry"})
            for _ in range(50):
                if ws.receive_json()["type"] in ("done", "error"):
                    break
    msgs = client.get(f"/api/sessions/{sid}/messages").json()
    assert [m["role"] for m in msgs] == ["user", "assistant"]  # сообщений не прибавилось
    assert len(msgs[-1]["swipes"]) == 2  # добавился свайп


def test_continue_appends_to_last_assistant(client):
    cid = client.post("/api/characters", json={"name": "Cont"}).json()["id"]
    sid = client.post(f"/api/sessions?character_id={cid}").json()["session_id"]
    with patch("backend.llm_gateway.litellm.acompletion", new=_fake_acompletion):
        with client.websocket_connect(f"/ws/chat/{sid}") as ws:
            ws.send_json({"type": "user_message", "content": "hi"})
            for _ in range(50):
                if ws.receive_json()["type"] in ("done", "error"):
                    break
            ws.send_json({"type": "continue"})
            for _ in range(50):
                if ws.receive_json()["type"] in ("done", "error"):
                    break
    msgs = client.get(f"/api/sessions/{sid}/messages").json()
    asst = [m for m in msgs if m["role"] == "assistant"][-1]
    # Исходный ответ "Привет!" + дописанное продолжение "Привет!".
    assert asst["content"] == "Привет!Привет!"
