"""
Дневник персонажа и дела человека (backend/diary.py, 2.28.0).

  * разбор чата: запись дня от лица персонажа, дело с точной датой (только с
    цитатой из реплик человека и не в прошлом), отметка «сделано»;
  * блок промпта: сегодняшняя дата, последние дни, ближайшие дела, «уже
    напомнил сегодня» после ответа, где дело упомянуто;
  * дела видны в чатах с другими персонажами (настройка), записи дней — нет;
  * общие чаты дневник не получают;
  * API: правка записи (edited), ручное дело, статус, удаление, настройки.
"""
import functools
import json
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from backend import diary
from backend.config import settings


def _chunk(text):
    class _Delta:
        content = text

    class _Choice:
        delta = _Delta()

    class _Chunk:
        choices = [_Choice()]

    return _Chunk()


def _llm(chat_reply="Ок.", diary_reply=None, seen=None):
    async def fake(*args, **kwargs):
        messages = kwargs.get("messages") or []
        is_diary = bool(messages) and "ЛИЧНЫЙ ДНЕВНИК" in str(messages[0].get("content") or "")
        if seen is not None:
            seen.append(("diary" if is_diary else "chat", messages))
        text = json.dumps(diary_reply or {"days": [], "plans": [], "done": [], "cancel": []},
                          ensure_ascii=False) if is_diary else chat_reply

        async def gen():
            yield _chunk(text)
        return gen()
    return fake


@pytest.fixture
def diary_on(client, monkeypatch):
    monkeypatch.setattr(settings, "DIARY", True)
    client.put("/api/diary/settings", json={"enabled": True, "auto": True, "plans_shared": True})
    for p in client.get("/api/diary").json()["plans"]:
        client.delete(f"/api/diary/plans/{p['id']}")
    yield
    for p in client.get("/api/diary").json()["plans"]:
        client.delete(f"/api/diary/plans/{p['id']}")


def _chat(client, name):
    cid = client.post("/api/characters", json={"name": name, "description": "Тёплая и внимательная."}).json()["id"]
    return cid, client.post(f"/api/sessions?character_id={cid}").json()["session_id"]


def _say(client, sid, text, reply="Ок.", seen=None):
    with patch("backend.llm_gateway.litellm.acompletion", new=_llm(reply, seen=seen)):
        with client.websocket_connect(f"/ws/chat/{sid}") as ws:
            ws.send_json({"type": "user_message", "content": text})
            for _ in range(100):
                if ws.receive_json()["type"] in ("done", "error"):
                    break


def _scan(client, sid, reply, seen=None):
    with patch("backend.llm_gateway.litellm.acompletion", new=_llm(diary_reply=reply, seen=seen)):
        return client.portal.call(functools.partial(diary.maybe_update, sid, force=True))


def _block(client, sid):
    tail = client.get(f"/api/sessions/{sid}/context").json().get("tail") or []
    return next((b["text"] for b in tail if b["key"] == "diary"), "")


# ---------------------------------------------------------------- чистые функции
def test_when_label_and_mentions():
    today = date(2026, 11, 19)   # четверг
    assert diary.when_label("2026-11-19", "18:00", today) == "сегодня в 18:00"
    assert diary.when_label("2026-11-20", "", today) == "завтра, пт 20.11"
    assert diary.when_label("2026-11-24", "", today) == "вт 24.11"
    assert diary.when_label("2026-11-18", "", today).endswith("(прошло)")
    assert diary.mentions("Не забудь, завтра стоматолог в шесть!", "сходить к стоматологу")
    assert not diary.mentions("Как прошёл день?", "сходить к стоматологу")
    # Одно общее слово — не напоминание.
    assert not diary.mentions("Давай сходим погулять!", "сходить к врачу")
    assert diary.mentions("Не забудь: тебе завтра сходить к врачу", "сходить к врачу")


def test_screen_plan_needs_quote_date_and_future():
    today = date(2026, 11, 19)
    said = "завтра мне к стоматологу в шесть, а в пятницу сдаю отчёт"
    norm, words = diary._norm_key(said), diary.terms(said)
    ok = diary.screen_plan({"due": "2026-11-20", "time": "18:00", "text": "сходить к стоматологу",
                            "quote": "завтра мне к стоматологу"}, today, norm, words)
    assert ok == {"due": "2026-11-20", "time": "18:00", "text": "сходить к стоматологу",
                  "quote": "завтра мне к стоматологу"}
    assert diary.screen_plan({"due": "2026-11-20", "text": "полететь на Марс", "quote": "лечу на марс"},
                             today, norm, words) is None          # цитаты нет в репликах
    assert diary.screen_plan({"due": "2026-11-10", "text": "сдать отчёт", "quote": "сдаю отчёт"},
                             today, norm, words) is None          # в прошлом
    assert diary.screen_plan({"due": "в пятницу", "text": "сдать отчёт", "quote": "сдаю отчёт"},
                             today, norm, words) is None          # не дата
    assert diary.screen_plan({"due": "2026-11-21", "time": "25:00", "text": "сдать отчёт",
                              "quote": "в пятницу сдаю отчёт"}, today, norm, words)["time"] == ""


def test_parse_reply_tolerates_noise():
    assert diary.parse_reply('Вот: {"days": [{"day": "2026-11-19"}], "plans": []} готово')["days"] \
        == [{"day": "2026-11-19"}]
    assert diary.parse_reply("не json") == {"days": [], "plans": [], "done": [], "cancel": []}


# ---------------------------------------------------------------- разбор и промпт
def test_diary_flow(client, diary_on):
    today = datetime.now(timezone.utc).date()
    tomorrow = (today + timedelta(days=1)).isoformat()
    cid, sid = _chat(client, "Дневниковая")
    _say(client, sid, "Слушай, завтра мне к стоматологу в шесть вечера, немного боюсь")
    seen = []
    stats = _scan(client, sid, {
        "days": [{"day": today.isoformat(), "mood": "🥺",
                  "entry": "Сегодня он признался, что боится стоматолога. Мне захотелось его обнять."},
                 {"day": "1999-01-01", "entry": "чужой день — не из переписки"}],
        "plans": [{"due": tomorrow, "time": "18:00", "text": "сходить к стоматологу",
                   "quote": "завтра мне к стоматологу в шесть"},
                  {"due": tomorrow, "text": "выдуманное дело", "quote": "этого никто не говорил"}],
    }, seen=seen)
    assert stats == {"days": 1, "plans": 1, "done": 0, "cancelled": 0}
    # Модель пишет от лица персонажа и видит переписку по дням.
    req = seen[0][1]
    assert "Ты — Дневниковая" in req[0]["content"]
    assert f"== {today.isoformat()}" in req[1]["content"] and "стоматологу" in req[1]["content"]

    data = client.get(f"/api/diary?character_id={cid}&session_id={sid}").json()
    assert [d["text"] for d in data["days"]][0].startswith("Сегодня он признался")
    plan = next(p for p in data["plans"] if p["text"] == "сходить к стоматологу")
    assert plan["due"] == tomorrow and plan["time"] == "18:00" and plan["status"] == "planned"
    assert "Сегодня 🥺: Сегодня он признался" in data["block"]
    assert "завтра" in data["block"] and "в 18:00 — сходить к стоматологу" in data["block"]

    # Блок — в контексте хода (инспектор).
    block = _block(client, sid)
    assert "сходить к стоматологу" in block and "(уже напомнил сегодня)" not in block

    # Ответ персонажа упомянул дело — «уже напомнил сегодня».
    _say(client, sid, "как дела?", reply="Хорошо! А ты не забыл, что завтра стоматолог?")
    client.portal.call(functools.partial(diary.note_reminders, sid))
    assert "сходить к стоматологу (уже напомнил сегодня)" in _block(client, sid)

    # Повтор разбора не дублирует дело; «сделал» закрывает его по номеру.
    _say(client, sid, "сходил к стоматологу, всё ок!")
    _scan(client, sid, {"plans": [{"due": tomorrow, "time": "18:00", "text": "Сходить к стоматологу",
                                   "quote": "завтра мне к стоматологу в шесть"}], "done": [1]})
    plans = [p for p in client.get("/api/diary").json()["plans"] if "стоматолог" in p["text"].lower()]
    assert len(plans) == 1 and plans[0]["status"] == "done"
    assert "— сходить к стоматологу" not in _block(client, sid)

    # Другой персонаж: дела видны (по умолчанию), записи дней — нет.
    client.post("/api/diary/plans", json={"due": tomorrow, "text": "позвонить маме", "character_id": cid})
    cid2, sid2 = _chat(client, "Другая")
    other = _block(client, sid2)
    assert "позвонить маме" in other and "признался" not in other
    client.put("/api/diary/settings", json={"plans_shared": False})
    assert "позвонить маме" not in _block(client, sid2)
    client.put("/api/diary/settings", json={"plans_shared": True})

    # Выключенный дневник — ни блока, ни разбора.
    client.put("/api/diary/settings", json={"enabled": False})
    assert _block(client, sid) == ""
    client.put("/api/diary/settings", json={"enabled": True})


def test_diary_api_edit_and_plans(client, diary_on):
    cid, sid = _chat(client, "Правочная")
    today = datetime.now(timezone.utc).date().isoformat()
    day = client.post("/api/diary/days", json={"character_id": cid, "day": today, "text": "Хороший был день."}).json()
    assert day["edited"] is True
    r = client.patch(f"/api/diary/days/{day['id']}", json={"text": "Очень хороший день.", "mood": "😊"}).json()
    assert r["text"] == "Очень хороший день." and r["mood"] == "😊"
    assert client.patch(f"/api/diary/days/{day['id']}", json={"text": "  "}).status_code == 422

    # Правка человека модель видит с пометкой «сохрани».
    _say(client, sid, "привет, я вернулся с работы и очень устал сегодня")
    seen = []
    _scan(client, sid, {"days": [{"day": today, "entry": "Очень хороший день. А вечером он пришёл уставший."}]},
          seen=seen)
    assert "Очень хороший день. (поправлено" not in seen[0][1][1]["content"]
    assert "(поправлено человеком — сохрани): Очень хороший день." in seen[0][1][1]["content"]

    assert client.post("/api/diary/plans", json={"due": "завтра", "text": "x"}).status_code == 422
    p = client.post("/api/diary/plans", json={"due": today, "time": "9:05", "text": "выпить витамины"}).json()
    assert p["time"] == "09:05" and p["source"] == "manual"
    p = client.patch(f"/api/diary/plans/{p['id']}", json={"status": "cancelled"}).json()
    assert p["status"] == "cancelled"
    assert client.patch(f"/api/diary/plans/{p['id']}", json={"status": "??"}).status_code == 422
    assert client.delete(f"/api/diary/plans/{p['id']}").json() == {"ok": True}
    assert client.delete(f"/api/diary/days/{day['id']}").json() == {"ok": True}
    assert client.delete(f"/api/diary/days/{day['id']}").status_code == 404


def test_group_chat_has_no_diary(client, diary_on):
    a = client.post("/api/characters", json={"name": "ГрА"}).json()["id"]
    b = client.post("/api/characters", json={"name": "ГрБ"}).json()["id"]
    gid = client.post("/api/groups", json={"name": "Г", "character_ids": [a, b]}).json()["session_id"]
    assert client.portal.call(functools.partial(diary.maybe_update, gid, force=True)) is None


def test_review_fixes_pointer_manual_and_conflict(client, diary_on):
    """Ручной режим копит реплики до кнопки; правка во время запроса не затирается."""
    cid, sid = _chat(client, "Ручная")
    client.put("/api/diary/settings", json={"auto": False})
    try:
        _say(client, sid, "сегодня был трудный день на работе, устал")
        assert client.portal.call(functools.partial(diary.maybe_update, sid)) is None
        today = datetime.now(timezone.utc).date().isoformat()
        stats = _scan(client, sid, {"days": [{"day": today, "entry": "Он устал на работе. Я хотела его согреть."}]})
        assert stats["days"] == 1   # «Записать сейчас» видит накопленное
    finally:
        client.put("/api/diary/settings", json={"auto": True})

    # Пока модель думала, человек поправил запись — её не затираем, пакет повторим.
    _say(client, sid, "а вечером посмотрел хороший фильм")
    row = client.get(f"/api/diary?character_id={cid}").json()["days"][0]

    async def edit_then_reply(*args, **kwargs):
        messages = kwargs.get("messages") or []
        if messages and "ЛИЧНЫЙ ДНЕВНИК" in str(messages[0].get("content")):
            from backend.database import AsyncSessionLocal
            from backend import models
            async with AsyncSessionLocal() as db:
                r = await db.get(models.DiaryDay, row["id"])
                r.text, r.edited = "Моя правка дня.", True
                await db.commit()
            text = json.dumps({"days": [{"day": today, "entry": "Модель затёрла бы правку."}]}, ensure_ascii=False)
        else:
            text = "Ок."

        async def gen():
            yield _chunk(text)
        return gen()

    with patch("backend.llm_gateway.litellm.acompletion", new=edit_then_reply):
        client.portal.call(functools.partial(diary.maybe_update, sid, force=True))
    day = client.get(f"/api/diary?character_id={cid}").json()["days"][0]
    assert day["text"] == "Моя правка дня."
    # Указатель не сдвинут — следующий разбор видит правку с пометкой «сохрани».
    seen = []
    _scan(client, sid, {"days": [{"day": today, "entry": "Моя правка дня. А вечером — фильм."}]}, seen=seen)
    assert "(поправлено человеком — сохрани): Моя правка дня." in seen[0][1][1]["content"]
    assert "фильм" in seen[0][1][1]["content"]
    assert client.get(f"/api/diary?character_id={cid}").json()["days"][0]["text"].endswith("фильм.")


def test_backlog_is_processed_in_order(client, diary_on, monkeypatch):
    """Хвост больше пакета не теряется: следующий разбор продолжает с указателя."""
    monkeypatch.setattr(diary, "BATCH_MESSAGES", 4)
    cid, sid = _chat(client, "Очередная")
    _say(client, sid, "первая реплика про утро")
    _scan(client, sid, {})                       # первый разбор: указатель в конец
    for i in range(4):
        _say(client, sid, f"реплика номер {i}")
    seen = []
    _scan(client, sid, {}, seen=seen)
    _scan(client, sid, {}, seen=seen)
    first, second = (s[1][1]["content"] for s in seen)
    assert "реплика номер 0" in first and "реплика номер 1" in first and "реплика номер 2" not in first
    assert "реплика номер 2" in second and "реплика номер 3" in second


def test_parse_reply_skips_braces_before_json():
    raw = 'Думаю {так}. Ответ: {"days": [], "plans": [{"due": "2030-01-01"}], "done": ["1", true]}'
    out = diary.parse_reply(raw)
    assert out["plans"] == [{"due": "2030-01-01"}]
    assert [diary._as_index(x) for x in out["done"]] == [1, None]


def test_entry_is_appended_not_rewritten(client, diary_on):
    """2.29.2: модель дописывает день («add»), прежнее (и правку человека) не переписывает."""
    cid, sid = _chat(client, "Дописная")
    today = datetime.now(timezone.utc).date().isoformat()
    _say(client, sid, "утром ходил на пробежку, было здорово")
    _scan(client, sid, {"days": [{"day": today, "mood": "🙂", "add": "Утром он бегал и вернулся счастливым."}]})
    _say(client, sid, "а вечером поругался с начальником")
    seen = []
    _scan(client, sid, {"days": [{"day": today, "mood": "😟", "add": "Вечером он поссорился с начальником, мне за него тревожно."}]},
          seen=seen)
    assert "Утром он бегал" in seen[0][1][1]["content"]          # модель видит прежнее
    assert "НЕ переписывай" in seen[0][1][0]["content"]
    day = client.get(f"/api/diary?character_id={cid}").json()["days"][0]
    assert day["text"] == ("Утром он бегал и вернулся счастливым.\n\n"
                           "Вечером он поссорился с начальником, мне за него тревожно.")
    assert day["mood"] == "😟"
    # Повтор уже записанного не дублируется; запись целиком (старый формат) — только хвост.
    _say(client, sid, "ну вот так")
    _scan(client, sid, {"days": [{"day": today, "add": "Утром он бегал и вернулся счастливым."}]})
    _say(client, sid, "пойду спать")
    _scan(client, sid, {"days": [{"day": today, "entry": day["text"] + " Ночью он пожелал мне спокойной ночи."}]})
    text = client.get(f"/api/diary?character_id={cid}").json()["days"][0]["text"]
    assert text.count("Утром он бегал") == 1 and text.endswith("Ночью он пожелал мне спокойной ночи.")


def test_backlog_catches_up_in_one_run(client, diary_on, monkeypatch):
    """2.29.6: накопилось больше порции — разбор догоняет подряд, а не по порции за ход."""
    monkeypatch.setattr(diary, "BATCH_MESSAGES", 4)
    cid, sid = _chat(client, "Догоняющая")
    _say(client, sid, "начало разговора")
    _scan(client, sid, {})
    for i in range(6):
        _say(client, sid, f"реплика номер {i}")
    seen = []
    _scan(client, sid, {}, seen=seen)
    assert len(seen) >= 3                       # несколько порций за один вызов
    assert "реплика номер 5" in seen[-1][1][1]["content"]
    assert client.portal.call(functools.partial(diary._has_backlog, sid)) is False


def test_long_day_is_not_cut_in_storage(client, diary_on):
    """Длинный день не теряет середину при дописывании (раньше — потолок 4000)."""
    cid, sid = _chat(client, "Длинная")
    today = datetime.now(timezone.utc).date().isoformat()
    for i in range(6):
        _say(client, sid, f"кусок дня {i}")
        _scan(client, sid, {"days": [{"day": today, "add": f"Абзац {i}. " + "подробности дня " * 60}]})
    text = client.get(f"/api/diary?character_id={cid}").json()["days"][0]["text"]
    assert len(text) > 5000 and all(f"Абзац {i}." in text for i in range(6)) and " … " not in text
