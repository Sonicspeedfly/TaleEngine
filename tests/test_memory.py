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

    async def fake_complete(messages, params=None, connection=None, kind="service"):
        # Суммаризатору передаётся и старая сводка, и новые события.
        joined = str(messages)
        assert "Новые события" in joined and "событие номер 0" in joined
        # Расход служебных вызовов учитывается отдельно от самого чата.
        assert kind == "summary"
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


@pytest.mark.asyncio
async def test_history_files_limited_by_default():
    """
    Файлы истории: по умолчанию действует лимит по ОБЪЁМУ (экономия квоты).

    Раньше дефолтом было «без лимита», и одно присланное видео пересылалось
    модели заново на КАЖДОМ ходу до конца чата — главная статья перерасхода.
    Полную память по файлам можно вернуть явно (history_files_mb=0).
    """
    from types import SimpleNamespace

    from backend.attachments import load_history_attachments
    from backend.main import _hist_files_limit
    from backend.schemas import GenerationParams

    big = "data:video/mp4;base64," + "A" * 20_000_000  # ~20 МБ base64
    msgs = [SimpleNamespace(id=1, role="user", content="видео",
                            attachments=[{"type": "video", "data": big, "mime": "video/mp4"}])]

    # Дефолт: лимит есть — тяжёлое видео в историю повторно НЕ пересылается.
    limit = _hist_files_limit(None)
    assert limit is not None
    assert await load_history_attachments(None, msgs, limit) == {}

    # Полная память по файлам — по явному запросу (0 = без лимита).
    assert _hist_files_limit(GenerationParams(history_files_mb=0)) is None
    att_map = await load_history_attachments(None, msgs, None)
    assert 1 in att_map and att_map[1][0]["data"] == big


@pytest.mark.asyncio
async def test_history_files_age_window_drops_old_attachments():
    """
    Возрастное окно: файлы несут только N ПОСЛЕДНИХ сообщений.

    Лимит в МБ этого не решает — полсотни мелких картинок по отдельности дёшевы,
    но вместе висят в каждом запросе до конца жизни чата.
    """
    from types import SimpleNamespace

    from backend.attachments import load_history_attachments
    from backend.main import _hist_files_turns
    from backend.schemas import GenerationParams

    def _msg(i: int):
        return SimpleNamespace(
            id=i, role="user", content=f"фото {i}",
            attachments=[{"type": "image", "data": f"data:image/png;base64,{i}", "mime": "image/png"}],
        )

    msgs = [_msg(i) for i in range(1, 11)]  # 10 сообщений с картинками

    # Окно в 3 сообщения: доходят только три последних, остальные — пометкой.
    att_map = await load_history_attachments(None, msgs, None, 3)
    assert sorted(att_map) == [8, 9, 10]

    # Без окна (0/None) поведение прежнее — доходят все.
    all_map = await load_history_attachments(None, msgs, None, None)
    assert set(all_map) == {m.id for m in msgs}

    # UI может отключить окно, выставив 0; по умолчанию окно включено.
    assert _hist_files_turns(GenerationParams(history_files_turns=0)) is None
    assert _hist_files_turns(None) is not None
