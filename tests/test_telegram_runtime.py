"""
Тесты чистой логики Telegram-бота: склейка длинного пользовательского ввода
(клиент режет >4096 на куски) и сниппеты для списка чатов/истории, плюс
интеграция с БД: выбор существующего чата (активная сессия) и показ истории.
"""
import asyncio

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
