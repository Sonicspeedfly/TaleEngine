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
    from backend import main, models
    from backend.database import AsyncSessionLocal, engine, init_db

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
        for i in range(12):
            db.add(models.Message(
                session_id=sess.id,
                role="user" if i % 2 == 0 else "assistant",
                content=f"событие номер {i}",
            ))
        await db.commit()
        sid = sess.id

    async def fake_complete(messages, params=None, connection=None):
        # Суммаризатору передаётся и старая сводка, и новые события.
        joined = str(messages)
        assert "Новые события" in joined and "событие номер 0" in joined
        return "Герои пережили двенадцать событий и заключили союз."

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
    last_marks = [k for k in (entry.keywords or []) if str(k).startswith("last:")]
    assert last_marks, "должна храниться метка последнего учтённого сообщения"

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
