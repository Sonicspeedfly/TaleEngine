"""
Тесты чистой логики Telegram-бота: склейка длинного пользовательского ввода
(клиент режет >4096 на куски) и сниппеты для списка чатов/истории, плюс
интеграция с БД: выбор существующего чата (активная сессия) и показ истории.
"""
import asyncio

from sqlalchemy import select

from backend import models
from backend import telegram_runtime as tr
from backend.database import AsyncSessionLocal, init_db
from backend.telegram_format import TG_LIMIT


def test_smart_join_single_message():
    assert tr._smart_join(["привет"]) == "привет"
    assert tr._smart_join([]) == ""


def test_smart_join_separate_messages_blank_line():
    # Короткие отдельные сообщения — склейка через пустую строку.
    assert tr._smart_join(["первое", "второе"]) == "первое\n\nвторое"


def test_smart_join_reconstructs_client_split():
    # Клиент Telegram режет длинный текст на ~4096: первый кусок «полный»
    # (>= _SPLIT_HINT) — продолжение клеим ВСТЫК, без вставки переносов.
    head = "A" * tr._SPLIT_HINT
    tail = "B" * 100
    assert tr._smart_join([head, tail]) == head + tail


def test_smart_join_roundtrip_over_4096():
    # Большое сообщение -> порезано клиентом -> собрано обратно точь-в-точь.
    original = ("Ложь и правда. " * 400).strip()  # заведомо > 4096
    assert len(original) > TG_LIMIT
    chunks = [original[i:i + TG_LIMIT] for i in range(0, len(original), TG_LIMIT)]
    assert len(chunks) > 1 and len(chunks[0]) >= tr._SPLIT_HINT
    assert tr._smart_join(chunks) == original


def test_preview_collapses_and_truncates():
    assert tr._preview("  много   пробелов\nи перенос ") == "много пробелов и перенос"
    long = "сло " * 100
    out = tr._preview(long, limit=20)
    assert len(out) == 21 and out.endswith("…")  # 20 символов + многоточие
    assert tr._preview("") == ""


def test_split_hint_below_tg_limit():
    # Порог «похоже на обрезку» должен быть меньше жёсткого лимита Telegram.
    assert tr._SPLIT_HINT < TG_LIMIT


# ----------------------- Интеграция с БД -----------------------
class _FakeUser:
    def __init__(self, uid):
        self.id = uid


class _FakeMsg:
    """Минимальный заменитель aiogram Message — ловим отправленные ответы."""
    def __init__(self, uid):
        self.from_user = _FakeUser(uid)
        self.sent: list[str] = []

    async def answer(self, text, reply_markup=None):
        self.sent.append(text)


async def _add_msg(session_id, role, content):
    async with AsyncSessionLocal() as db:
        db.add(models.Message(session_id=session_id, role=role, content=content))
        await db.commit()


async def _runtime_scenario():
    await init_db()
    async with AsyncSessionLocal() as db:
        ch = models.Character(name="ТгПерс")
        db.add(ch)
        await db.commit()
        await db.refresh(ch)
    tg = 55501

    # Два чата у одного пользователя Telegram.
    s1 = await tr._create_session(tg, ch.id)
    await _add_msg(s1, "user", "привет из первого чата")
    await _add_msg(s1, "assistant", "и тебе привет")
    s2 = await tr._create_session(tg, ch.id)  # новый — становится активным
    await _add_msg(s2, "user", "это второй чат")

    # Активным считается последний созданный.
    assert tr._active_sessions[tg] == s2
    assert await tr._get_or_create_session(tg) == s2

    # Переключаемся на СТАРЫЙ чат — он и остаётся активным (мутация выбора).
    assert await tr._set_active_session(tg, s1) is True
    assert await tr._get_or_create_session(tg) == s1

    # История выбранного чата человекочитаема и содержит реплики.
    hist = await tr._history_text(s1)
    assert "ТгПерс" in hist and "привет из первого чата" in hist

    # Список чатов: оба чата, с именем персонажа и счётчиком — SQL func.count/max/in_.
    msg = _FakeMsg(tg)
    await tr._show_chats(msg)
    assert msg.sent, "нет ответа со списком чатов"
    listing = msg.sent[0]
    assert "ТгПерс" in listing and "сообщ." in listing
    assert "это второй чат" in listing  # сниппет последней реплики второго чата

    # Устаревший активный id (чат удалён/чужой) — сбрасывается, откат на последний.
    assert await tr._set_active_session(tg, 999999) is False
    tr._active_sessions[tg] = 999999
    assert await tr._get_or_create_session(tg) == s2  # вернулись к последней сессии


def test_runtime_chat_selection_and_history():
    asyncio.run(_runtime_scenario())


async def _mk_session(character_id, user_key, owner_id=None, is_group=False, title="чат"):
    async with AsyncSessionLocal() as db:
        s = models.ChatSession(
            character_id=character_id, user_key=user_key, owner_id=owner_id,
            is_group=is_group, title=title,
        )
        db.add(s)
        await db.commit()
        await db.refresh(s)
        return s.id


async def _linked_account_scenario():
    await init_db()
    async with AsyncSessionLocal() as db:
        ch = models.Character(name="АккПерс")
        db.add(ch)
        await db.commit()
        await db.refresh(ch)
    tg = 55502
    owner = 42
    tr._active_sessions.pop(tg, None)

    # Чат, созданный в ВЕБЕ этим аккаунтом (user_key web:*, owner_id=42).
    web_sid = await _mk_session(ch.id, "web:abc-123", owner_id=owner, title="Веб-чат")
    await _add_msg(web_sid, "user", "это мой веб-чат")
    # Чат, созданный в TELEGRAM этим же аккаунтом.
    tg_sid = await tr._create_session(tg, ch.id, owner_id=owner)
    await _add_msg(tg_sid, "assistant", "телеграм-чат жив")
    # Групповой чат аккаунта — теперь ТОЖЕ доступен из Telegram.
    grp_sid = await _mk_session(ch.id, "web:grp", owner_id=owner, is_group=True, title="Отряд")
    # Чужой чат (другой владелец) — не наш.
    other_sid = await _mk_session(ch.id, "web:other", owner_id=99, title="Чужой")

    # Список чатов привязанного аккаунта: и веб, и телеграм, и группа; без чужого.
    msg = _FakeMsg(tg)
    await tr._show_chats(msg, owner_id=owner)
    listing = msg.sent[0]
    assert "это мой веб-чат" in listing          # веб-чат виден в Telegram
    assert "телеграм-чат жив" in listing          # и телеграм-чат
    assert "👥" in listing and "Отряд" in listing  # групповой — показан с меткой
    assert "Чужой" not in listing                 # чужой аккаунт — не наш
    # Владение: можем ПРОДОЛЖИТЬ веб-чат из Telegram, но не чужой.
    assert await tr._set_active_session(tg, web_sid, owner) is True
    assert await tr._get_or_create_session(tg, owner) == web_sid
    assert await tr._set_active_session(tg, other_sid, owner) is False

    # Без привязки (общий режим, owner_id=None) веб-чаты аккаунта НЕ видны —
    # только начатые в Telegram.
    assert tr._owns_session(await _get(web_sid), tg, None) is False
    assert tr._owns_session(await _get(tg_sid), tg, None) is True


async def _get(sid):
    async with AsyncSessionLocal() as db:
        return await db.get(models.ChatSession, sid)


def test_runtime_linked_account_sees_web_chats():
    asyncio.run(_linked_account_scenario())


# ---- Групповые чаты в боте: сводка (info) + ход по упоминанию/кругу ----
def _fake_chunk(text):
    class _D:  # delta
        def __init__(self, c): self.content = c
    class _C:  # choice
        def __init__(self, c): self.delta = _D(c)
    class _Ch:  # chunk
        def __init__(self, c): self.choices = [_C(c)]
    return _Ch(text)


async def _fake_acompletion(*args, **kwargs):
    async def gen():
        for t in ["Привет", ", это ответ"]:
            yield _fake_chunk(t)
    return gen()


async def _group_scenario():
    from unittest.mock import patch

    await init_db()
    async with AsyncSessionLocal() as db:
        alice = models.Character(name="Алиса")
        bob = models.Character(name="Боб")
        db.add_all([alice, bob])
        await db.commit()
        await db.refresh(alice)
        await db.refresh(bob)
        gsid = (await _mk_session(alice.id, "tg:55503", is_group=True, title="Отряд"))
        db.add_all([
            models.GroupMember(session_id=gsid, character_id=alice.id),
            models.GroupMember(session_id=gsid, character_id=bob.id),
        ])
        await db.commit()

    # Сводка по группе: участники, режиссёр, тип.
    info = await tr._chat_info_text(gsid)
    assert "Групповой" in info and "Алиса" in info and "Боб" in info and "Режиссёр" in info

    with patch("backend.llm_gateway.litellm.acompletion", new=_fake_acompletion):
        # Упомянут Боб — отвечает именно он (одной репликой, с подписью).
        res = await tr._generate_group_reply(gsid, "привет, Боб, как дела?", [])
        assert res == [("Боб", "Привет, это ответ")]
        # Без упоминания, режиссёр выключен — отвечает следующий по кругу.
        res2 = await tr._generate_group_reply(gsid, "просто реплика без имён", [])
        assert len(res2) == 1 and res2[0][0] in ("Алиса", "Боб")

    # Реплики сохранены с указанием говорящего (speaker_name).
    async with AsyncSessionLocal() as db:
        saved = (await db.execute(
            select(models.Message).where(
                models.Message.session_id == gsid, models.Message.role == "assistant"
            )
        )).scalars().all()
    assert any(m.speaker_name == "Боб" for m in saved)

    # История группы показывает КАЖДОГО говорящего, а не только «ведущего».
    hist = await tr._history_text(gsid)
    assert "Боб" in hist


def test_runtime_group_chat_info_and_turn():
    asyncio.run(_group_scenario())
