"""
Тесты «памяти» нейросети: окно контекста (context_tokens) и авто-сводка сюжета.

Авто-сводка — фоновая задача: каждые ~12 новых сообщений сжимает события чата
в always_on запись Horae, чтобы вылетевшая из окна контекста история не терялась.
"""
from unittest.mock import patch

import pytest
from sqlalchemy import select


def test_ctx_budget_ui_overrides_default():
    from backend.config import settings
    from backend.main import _ctx_budget
    from backend.schemas import GenerationParams

    assert _ctx_budget(None) == settings.CONTEXT_TOKEN_BUDGET
    assert _ctx_budget(GenerationParams()) == settings.CONTEXT_TOKEN_BUDGET
    assert _ctx_budget(GenerationParams(context_tokens=32000)) == 32000


@pytest.mark.asyncio
async def test_auto_summary_creates_entry_and_tracks_progress():
    from backend import hierarchical_memory as hm
    from backend import main, models
    from backend.database import AsyncSessionLocal, engine, init_db
    from backend.horae_recall import DEFAULT_WINDOW, FACTS_PROMPT

    # Пул соединений мог быть создан в чужом event loop (TestClient) — сбрасываем.
    await engine.dispose()
    await init_db()

    async with AsyncSessionLocal() as db:
        ch = models.Character(name="Мемо")
        db.add(ch)
        await db.commit()
        await db.refresh(ch)
        sess = models.ChatSession(character_id=ch.id, user_key="test:memory")
        db.add(sess)
        await db.commit()
        await db.refresh(sess)
        # 12 сообщений старше активного окна плюс само окно: сводка сжимает только
        # то, что старше окна (последние реплики модель и так видит дословно).
        for i in range(12 + DEFAULT_WINDOW):
            db.add(models.Message(
                session_id=sess.id,
                role="user" if i % 2 == 0 else "assistant",
                content=f"событие номер {i}",
            ))
        await db.commit()
        sid = sess.id

    async def fake_complete(messages, params=None, connection=None, kind="service"):
        # Суммаризатору передаётся и старая сводка, и новые события.
        joined = str(messages)
        assert "Новые события" in joined and "событие номер 0" in joined
        # Реплики активного окна ждут: их ещё могут перегенерировать или править.
        assert "событие номер 12" not in joined and "событие номер 31" not in joined
        # Расход служебных вызовов учитывается отдельно от самого чата.
        assert kind == "summary"
        if messages[0]["content"] == FACTS_PROMPT:
            return "Герои пережили двенадцать событий и заключили союз."
        return hm.render_snapshot(
            {hm.SEC_CHRONICLE: "- [#1–#12] Герои пережили двенадцать событий и заключили союз."})

    with patch("backend.main.complete", new=fake_complete):
        await main._maybe_update_summary(sid)

    async with AsyncSessionLocal() as db:
        entry = (await db.execute(select(models.HoraeEntry).where(
            models.HoraeEntry.session_id == sid,
            models.HoraeEntry.category == "summary",
        ))).scalars().first()
    assert entry is not None
    assert entry.always_on and entry.enabled  # подмешивается в каждый запрос
    assert "союз" in entry.content
    # Указатель «до какого сообщения учтено» живёт в служебном meta, а НЕ в
    # keywords: keywords пользователь правит руками, и раньше правка ключевых
    # слов записи «Память чата (авто)» ломала сводку.
    assert (entry.meta or {}).get("last_message_id"), "нужен указатель последнего учтённого"
    assert not [k for k in (entry.keywords or []) if str(k).startswith("last:")]

    # Новых сообщений мало (0) — повторный вызов сводку НЕ трогает.
    async def fail_complete(*a, **kw):  # noqa: ANN001
        raise AssertionError("суммаризатор не должен вызываться без новых сообщений")

    with patch("backend.main.complete", new=fail_complete):
        await main._maybe_update_summary(sid)

    # Выключатель auto_summary=false отключает механизм.
    async with AsyncSessionLocal() as db:
        for i in range(12):
            db.add(models.Message(session_id=sid, role="user", content=f"ещё {i}"))
        row = await db.get(models.AppSetting, "ui")
        if row is None:
            row = models.AppSetting(key="ui", value={"auto_summary": False})
            db.add(row)
        else:
            row.value = {**(row.value or {}), "auto_summary": False}
        await db.commit()
    with patch("backend.main.complete", new=fail_complete):
        await main._maybe_update_summary(sid)
    # Возвращаем настройку, чтобы не влиять на другие тесты.
    async with AsyncSessionLocal() as db:
        row = await db.get(models.AppSetting, "ui")
        row.value = {**(row.value or {}), "auto_summary": True}
        await db.commit()
    await engine.dispose()  # не оставляем соединения этого event loop другим тестам


def test_history_files_settings_are_gone_but_old_keys_are_accepted():
    """
    «Файлы в памяти диалога» (history_files_mb / history_files_turns) убраны:
    файлы истории идут ссылками на хранилище модели (backend/media_refs.py).
    Сохранённые настройки и пресеты со старыми ключами не ломаются — pydantic
    их просто игнорирует.
    """
    from backend import main
    from backend.schemas import GenerationParams

    p = GenerationParams(history_files_mb=8, history_files_turns=12, temperature=0.5)
    assert p.temperature == 0.5
    assert "history_files_mb" not in p.model_dump()
    assert not hasattr(main, "_hist_files_limit") and not hasattr(main, "_hist_files_turns")


def test_history_keeps_every_file_without_age_window():
    """
    Возрастного окна больше нет: файл есть у каждого сообщения истории, а что
    из них уйдёт ссылкой, целиком или пометкой — решается перед запросом.
    Данные файлов при сборке истории не читаются (нет обращений к БД).
    """
    from types import SimpleNamespace

    from backend.horae_memory import messages_to_history
    from backend.media_refs import MARK

    msgs = [SimpleNamespace(
        id=i, role="user", content=f"фото {i}",
        attachments=[{"type": "image", "mime": "image/png", "name": f"{i}.png", "size": 1000,
                      "blob_id": 100 + i}],
    ) for i in range(1, 11)]
    hist = messages_to_history(msgs)
    marks = [b[MARK] for h in hist for b in h["content"] if isinstance(b, dict) and MARK in b]
    assert [m["blob_id"] for m in marks] == [100 + i for i in range(1, 11)]
    assert all(m["kind"] == "image" and m["bytes"] == 1000 for m in marks)
