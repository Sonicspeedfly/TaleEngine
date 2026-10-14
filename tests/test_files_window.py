"""
Окно файлов (2.11.0): модель получает файлы только из N последних сообщений.

Даже ссылкой на хранилище файл стоит токенов на КАЖДОМ ходу (минута голосового
≈ 2 тыс., видео — десятки тысяч), поэтому человек сам решает, сколько свежих
сообщений отдают модели свои файлы. Старше — пометка с именем файла.

Проверяем: само окно (0 / N / -1, файлы отвечаемой реплики), значение по
умолчанию, личный чат через инспектор хода (те же параметры, что у хода),
групповой чат и то, что параметр не уходит провайдеру.
"""
import base64

from backend import media_refs as mr
from backend.config import settings
from backend.horae_memory import files_window_note, resolve_files_window, window_history_media
from backend.schemas import GenerationParams


def _hist(n_pairs: int, kind="image"):
    out = []
    for i in range(n_pairs):
        meta = {"type": kind, "mime": "image/png", "name": f"f{i}.png", "size": 1000, "blob_id": i + 1}
        out.append({"role": "user", "content": mr.history_content(f"сообщение {i}", [meta])})
        out.append({"role": "assistant", "content": "ок"})
    return out


def _marks(m) -> int:
    c = m.get("content")
    return sum(1 for b in c if isinstance(b, dict) and mr.MARK in b) if isinstance(c, list) else 0


def test_window_keeps_files_of_last_messages_only():
    hist = _hist(5)                                   # 10 сообщений, файлы у 0, 2, 4, 6, 8
    out = window_history_media(hist, 4)              # последние 4: индексы 6..9
    assert [_marks(m) for m in out] == [0, 0, 0, 0, 0, 0, 1, 0, 1, 0]
    old = out[0]["content"]
    assert files_window_note(4) in [b.get("text") for b in old]       # пометка вместо файла
    assert any("«f0.png»" in (b.get("text") or "") for b in old)      # имя файла осталось
    assert old[0]["text"] == "сообщение 0"                            # и текст сообщения
    assert window_history_media(hist, -1) is hist                     # -1 — все файлы
    assert window_history_media(hist, None) is hist
    assert window_history_media(hist, 50) is hist                     # окно шире истории
    assert sum(_marks(m) for m in window_history_media(hist, 0)) == 0  # 0 — ни одного из истории


def test_files_of_answered_message_always_go():
    meta = {"type": "audio", "mime": "audio/ogg", "name": "v.ogg", "size": 1000, "blob_id": 9}
    hist = [{"role": "user", "content": mr.history_content("ответь на это", [meta], priority=2)},
            *(_hist(3))]
    out = window_history_media(hist, 0)
    assert _marks(out[0]) == 1                         # перегенерация видит файлы своей реплики


def test_default_and_explicit_window(monkeypatch):
    monkeypatch.setattr(settings, "FILES_WINDOW", 10)
    assert resolve_files_window(None) == 10
    assert resolve_files_window(GenerationParams()) == 10
    assert resolve_files_window(GenerationParams(files_window=3)) == 3
    assert resolve_files_window(GenerationParams(files_window=0)) == 0
    assert resolve_files_window(GenerationParams(files_window=-1)) == -1


def test_window_is_not_sent_to_provider():
    from backend.llm_gateway import _merge_params

    merged = _merge_params(GenerationParams(files_window=3, temperature=0.5))
    assert "files_window" not in merged and merged["temperature"] == 0.5


# ---------------------------------------------------------------- через API

def _chat_with_images(client, n: int) -> int:
    png = base64.b64encode(b"\x89PNG" + b"x" * 64).decode()
    msgs = []
    for i in range(n):
        msgs.append({"role": "user", "content": f"фото {i}", "swipes": [f"фото {i}"], "active_swipe": 0,
                     "attachments": [{"type": "image", "mime": "image/png", "name": f"p{i}.png",
                                      "data": "data:image/png;base64," + png}]})
        msgs.append({"role": "assistant", "content": "вижу", "swipes": ["вижу"], "active_swipe": 0})
    data = {"format": "aichat.chat", "version": 1, "character": {"name": "Окно"},
            "session": {"title": "Фото"}, "messages": msgs, "horae": []}
    import json

    r = client.post("/api/sessions/import", files={"file": ("c.json", json.dumps(data), "application/json")})
    return r.json()["session_id"]


def _context(client, sid, window):
    client.put("/api/settings/ui", json={"params": {"files_window": window}})
    return client.get(f"/api/sessions/{sid}/context").json()


def test_inspector_counts_files_outside_window(client):
    sid = _chat_with_images(client, 6)                # 12 сообщений, 6 фото
    try:
        rep = _context(client, sid, 4)                # последние 4 сообщения — 2 фото
        assert rep["files_window"] == 4 and rep["files_windowed"] == 4
        assert rep["files"]["total"] == 2
        rep = _context(client, sid, -1)
        assert rep["files_windowed"] == 0 and rep["files"]["total"] == 6
        rep = _context(client, sid, 0)
        assert rep["files_windowed"] == 6 and not (rep.get("files") or {}).get("total")
    finally:
        client.put("/api/settings/ui", json={})


async def test_group_chat_files_outside_window_become_notes(client):
    from backend import group_chat, models
    from backend.database import AsyncSessionLocal

    sid = _chat_with_images(client, 5)

    async def build(window):
        async with AsyncSessionLocal() as db:
            sess = await db.get(models.ChatSession, sid)
            char = await db.get(models.Character, sess.character_id)
            msgs = await group_chat.build_group_messages(
                db, sess, char, 200_000, params=GenerationParams(files_window=window))
        content = msgs[-1]["content"]
        return content if isinstance(content, list) else []

    blocks = client.portal.call(build, 2)
    texts = [b.get("text") or "" for b in blocks]
    assert sum(1 for b in blocks if isinstance(b, dict) and mr.MARK in b) == 2   # новейшая + 1 в окне
    assert texts.count(files_window_note(2)) == 3
    blocks = client.portal.call(build, -1)
    assert sum(1 for b in blocks if isinstance(b, dict) and mr.MARK in b) == 5
