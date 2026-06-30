"""
Telegram-бот внутри веб-сервера (управляется из админ-панели).

Возможности прямо в Telegram:
  * /start            — начать (если есть доступ);
  * /request          — запросить доступ (заявка попадёт в админку);
  * /characters       — выбрать персонажа кнопками;
  * /new              — начать новый чат с текущим персонажем;
  * /model            — выбрать модель (нейросеть) из прокси кнопками;
  * текст и голосовые  — обычное общение (аудио уходит модели нативно).

Доступ: open_to_all=True — для всех; иначе только белый список (по Telegram-ID).
Бот, память Horae и LiteLLM — те же, что у веба (общая БД и логика).
"""
import asyncio
import base64
import logging

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, KeyboardButton
from aiogram.types import Message as TgMessage
from aiogram.types import ReplyKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select

from backend import accounts, admin_service, models
from backend.config import settings
from backend.database import AsyncSessionLocal
from backend.document_service import is_document
from backend.horae_memory import build_context_from_db
from backend.llm_gateway import build_user_content, stream_completion
from backend.schemas import AttachmentIn, GenerationParams
from backend.settings_service import fetch_proxy_models, get_connection
from backend.telegram_format import TG_LIMIT, markdown_to_html, split_message

logger = logging.getLogger("aichat.telegram")

_task: asyncio.Task | None = None
_bot: Bot | None = None
_last_error: str = ""
# Буфер альбомов (несколько файлов одним сообщением) по media_group_id.
_media_buffers: dict[str, dict] = {}
_MEDIA_DEBOUNCE = 1.3  # сколько ждать остальные файлы альбома, прежде чем отвечать


def is_running() -> bool:
    return _task is not None and not _task.done()


def status() -> dict:
    return {"running": is_running(), "error": _last_error}


async def notify(tg_id: int, text: str, reply_markup=None) -> None:
    """Отправить сообщение пользователю в Telegram (если бот запущен)."""
    if not is_running() or _bot is None:
        return
    try:
        await _bot.send_message(int(tg_id), text, reply_markup=reply_markup)
    except Exception:  # noqa: BLE001
        logger.exception("Не удалось отправить уведомление в Telegram")


async def send_long(message: TgMessage, text: str, reply_markup=None) -> None:
    """
    Ответ нейросети в Telegram: режем на части ≤4096 и шлём как Telegram-HTML
    (Markdown → аккуратная разметка). Если HTML не распарсился — откатываемся на
    обычный текст, чтобы сообщение точно дошло. Клавиатуру вешаем на ПОСЛЕДНЮЮ часть.
    """
    parts = split_message(text or "", limit=TG_LIMIT - 96) or ["(пустой ответ)"]
    for i, part in enumerate(parts):
        markup = reply_markup if i == len(parts) - 1 else None
        try:
            await message.answer(
                markdown_to_html(part), parse_mode="HTML",
                disable_web_page_preview=True, reply_markup=markup,
            )
        except TelegramBadRequest:
            await message.answer(part, reply_markup=markup, disable_web_page_preview=True)


async def notify_friend_request(tg_id: int, from_username: str, friendship_id: int) -> None:
    """Уведомление о заявке в друзья с кнопками «Принять/Отклонить»."""
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Принять", callback_data=f"fr:a:{friendship_id}")
    kb.button(text="✖ Отклонить", callback_data=f"fr:d:{friendship_id}")
    await notify(
        tg_id, f"👥 {from_username} хочет добавить вас в друзья.", reply_markup=kb.as_markup()
    )


def _deny_text(user_id: int) -> str:
    return (
        f"Вас нет в списке доступа. Ваш ID: {user_id}.\n"
        "Отправьте /request, чтобы запросить доступ у администратора."
    )


def _bot_params() -> GenerationParams:
    """Параметры генерации для бота: модель берём из настроек Telegram."""
    model = (admin_service.telegram_cache().get("model") or "").strip()
    return GenerationParams(model=model) if model else GenerationParams()


async def _latest_session(tg_user_id: int):
    user_key = f"tg:{tg_user_id}"
    async with AsyncSessionLocal() as db:
        q = (
            select(models.ChatSession)
            .where(models.ChatSession.user_key == user_key)
            .order_by(models.ChatSession.id.desc())
        )
        return (await db.execute(q)).scalars().first()


async def _resolve_owner(tg_user_id: int):
    """
    В режиме аккаунтов возвращает (статус, owner_id):
      * ('off', None)      — режим аккаунтов выключен (общие данные);
      * ('ok', user_id)    — Telegram привязан к аккаунту;
      * ('unlinked', None) — режим аккаунтов включён, но привязки нет.
    """
    if not admin_service.security_cache().get("accounts_enabled"):
        return ("off", None)
    async with AsyncSessionLocal() as db:
        user = await accounts.user_by_telegram(db, tg_user_id)
    return ("ok", user.id) if user else ("unlinked", None)


async def _create_session(tg_user_id: int, character_id: int, owner_id=None) -> int:
    async with AsyncSessionLocal() as db:
        sess = models.ChatSession(
            character_id=character_id, user_key=f"tg:{tg_user_id}",
            title="Telegram chat", owner_id=owner_id,
        )
        db.add(sess)
        await db.commit()
        await db.refresh(sess)
        return sess.id


async def _get_or_create_session(tg_user_id: int, owner_id=None) -> int | None:
    """Активная (последняя) сессия пользователя; создаём с дефолтным персонажем."""
    sess = await _latest_session(tg_user_id)
    if sess:
        return sess.id
    char_id = admin_service.telegram_cache().get("default_character_id")
    async with AsyncSessionLocal() as db:
        # Персонаж по умолчанию: в режиме аккаунтов — из персонажей владельца.
        q = select(models.Character)
        if owner_id is not None:
            q = q.where(models.Character.owner_id == owner_id)
        char = (await db.execute(q)).scalars().first()
        if char_id is None:
            if char is None:
                return None
            char_id = char.id
    return await _create_session(tg_user_id, char_id, owner_id=owner_id)


async def _generate_reply(session_id: int, text: str, attachments: list[AttachmentIn]) -> str:
    async with AsyncSessionLocal() as db:
        sess = await db.get(models.ChatSession, session_id)
        character = await db.get(models.Character, sess.character_id)
        connection = await get_connection(db)
        user_content = build_user_content(text, attachments)
        messages = await build_context_from_db(
            db, sess, character, text, user_content, settings.CONTEXT_TOKEN_BUDGET
        )
        db.add(
            models.Message(
                session_id=session_id, role="user", content=text,
                attachments=[a.model_dump() for a in attachments],
            )
        )
        await db.commit()

    reply = ""
    async for token in stream_completion(messages, _bot_params(), connection):
        reply += token

    async with AsyncSessionLocal() as db:
        db.add(
            models.Message(
                session_id=session_id, role="assistant", content=reply,
                swipes=[reply], active_swipe=0,
            )
        )
        await db.commit()
    return reply


# Постоянные кнопки ПОД полем ввода (reply-клавиатура) — основные действия.
BTN_CHARS = "🎭 Персонажи"
BTN_NEW = "🆕 Новый чат"
BTN_MODEL = "🧠 Модель"
BTN_HELP = "❓ Помощь"


def _main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_CHARS), KeyboardButton(text=BTN_NEW)],
            [KeyboardButton(text=BTN_MODEL), KeyboardButton(text=BTN_HELP)],
        ],
        resize_keyboard=True,
    )


async def _gate(message: TgMessage):
    """Проверка доступа: возвращает (ok, owner_id). При отказе сам шлёт ответ."""
    if not admin_service.is_whitelisted(message.from_user.id):
        await message.answer(_deny_text(message.from_user.id))
        return (False, None)
    state, owner_id = await _resolve_owner(message.from_user.id)
    if state == "unlinked":
        await message.answer(
            "Аккаунт не привязан. В веб-приложении нажмите 👤 → «Привязать Telegram», "
            "получите код и пришлите сюда: /link КОД"
        )
        return (False, None)
    return (True, owner_id)


async def _show_characters(message: TgMessage, owner_id=None):
    async with AsyncSessionLocal() as db:
        q = select(models.Character)
        if owner_id is not None:
            q = q.where(models.Character.owner_id == owner_id)
        chars = (await db.execute(q)).scalars().all()
    if not chars:
        await message.answer("Персонажей пока нет — создайте их в веб-интерфейсе.")
        return
    kb = InlineKeyboardBuilder()
    for c in chars:
        kb.button(text=c.name, callback_data=f"char:{c.id}")
    kb.adjust(2)
    await message.answer("Выберите персонажа:", reply_markup=kb.as_markup())


async def _new_chat(message: TgMessage, owner_id=None):
    sess = await _latest_session(message.from_user.id)
    if not sess:
        await message.answer("Сначала выберите персонажа (🎭).")
        return
    await _create_session(message.from_user.id, sess.character_id, owner_id=owner_id)
    await message.answer("Начат новый чат с текущим персонажем.")


async def _show_models(message: TgMessage):
    async with AsyncSessionLocal() as db:
        conn = await get_connection(db)
    try:
        mdls = await fetch_proxy_models(conn)
    except Exception:  # noqa: BLE001
        mdls = []
    if not mdls:
        await message.answer("Не удалось получить список моделей из прокси.")
        return
    kb = InlineKeyboardBuilder()
    for m in mdls[:20]:
        kb.button(text=m, callback_data=f"model:{m}")
    kb.adjust(1)
    await message.answer("Выберите модель:", reply_markup=kb.as_markup())


async def _attachment_from_message(message: TgMessage) -> AttachmentIn | None:
    """Скачать вложение из сообщения Telegram и превратить в AttachmentIn."""
    if message.photo:
        f = await _bot.get_file(message.photo[-1].file_id)  # самый большой размер
        buf = await _bot.download_file(f.file_path)
        b64 = base64.b64encode(buf.read()).decode()
        return AttachmentIn(type="image", data="data:image/jpeg;base64," + b64, mime="image/jpeg")
    if message.document:
        doc = message.document
        f = await _bot.get_file(doc.file_id)
        buf = await _bot.download_file(f.file_path)
        b64 = base64.b64encode(buf.read()).decode()
        mime = doc.mime_type or "application/octet-stream"
        name = doc.file_name or "document"
        data_uri = f"data:{mime};base64,{b64}"
        if (mime or "").startswith("image/"):
            return AttachmentIn(type="image", data=data_uri, mime=mime, name=name)
        if (mime or "").startswith("audio/"):
            return AttachmentIn(type="audio", data=b64, mime=mime, name=name)
        # Документ Word/PDF/текст (или что-то ещё — document_service разберётся).
        return AttachmentIn(type="document", data=data_uri, mime=mime, name=name)
    if message.audio:
        f = await _bot.get_file(message.audio.file_id)
        buf = await _bot.download_file(f.file_path)
        b64 = base64.b64encode(buf.read()).decode()
        return AttachmentIn(type="audio", data=b64, mime=message.audio.mime_type or "audio/mpeg")
    return None


async def _process_messages(messages: list[TgMessage]) -> None:
    """Обработать одно сообщение или альбом как ОДИН ход к нейросети."""
    first = messages[0]
    ok, owner_id = await _gate(first)
    if not ok:
        return
    session_id = await _get_or_create_session(first.from_user.id, owner_id=owner_id)
    if session_id is None:
        await first.answer("В системе нет персонажей — создайте их в веб-интерфейсе.")
        return

    text_parts: list[str] = []
    attachments: list[AttachmentIn] = []
    for m in messages:
        caption = (m.caption or m.text or "").strip()
        if caption:
            text_parts.append(caption)
        try:
            att = await _attachment_from_message(m)
        except Exception:  # noqa: BLE001
            logger.exception("Не удалось скачать вложение из Telegram")
            att = None
        if att:
            attachments.append(att)

    text = "\n\n".join(text_parts)
    if not text and not attachments:
        return
    await _bot.send_chat_action(first.chat.id, "typing")
    reply = await _generate_reply(session_id, text, attachments)
    await send_long(first, reply)


async def _flush_group(gid: str) -> None:
    """Через паузу обработать накопленный альбом (все файлы пришли)."""
    try:
        await asyncio.sleep(_MEDIA_DEBOUNCE)
    except asyncio.CancelledError:
        return  # пришёл ещё файл альбома — таймер перезапустят
    buf = _media_buffers.pop(gid, None)
    if not buf or not buf["messages"]:
        return
    try:
        await _process_messages(buf["messages"])
    except Exception:  # noqa: BLE001
        logger.exception("Ошибка обработки альбома Telegram")


async def _handle_media(message: TgMessage) -> None:
    """Один файл — отвечаем сразу; альбом — копим по media_group_id и ждём остальные."""
    gid = message.media_group_id
    if not gid:
        await _process_messages([message])
        return
    buf = _media_buffers.setdefault(gid, {"messages": [], "task": None})
    buf["messages"].append(message)
    if buf["task"]:
        buf["task"].cancel()
    buf["task"] = asyncio.create_task(_flush_group(gid))


def _register(dp: Dispatcher) -> None:
    @dp.message(CommandStart())
    async def on_start(message: TgMessage):
        ok, owner_id = await _gate(message)
        if not ok:
            return
        await _get_or_create_session(message.from_user.id, owner_id=owner_id)
        await message.answer(
            "Привет! Пользуйся кнопками снизу или командами /characters /new /model.",
            reply_markup=_main_keyboard(),
        )

    @dp.message(Command("request"))
    async def on_request(message: TgMessage):
        async with AsyncSessionLocal() as db:
            await admin_service.add_request(
                db, message.from_user.id,
                message.from_user.username or "",
                message.from_user.first_name or "",
            )
        await message.answer(
            f"Заявка отправлена. Ваш ID: {message.from_user.id}. "
            "Администратор увидит её в панели и выдаст доступ."
        )

    @dp.message(Command("link"))
    async def on_link(message: TgMessage):
        # /link работает даже без привязки — это и есть способ привязаться.
        if not admin_service.is_whitelisted(message.from_user.id):
            await message.answer(_deny_text(message.from_user.id))
            return
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) < 2:
            await message.answer("Использование: /link КОД (код — в веб-приложении: 👤 → «Привязать Telegram»).")
            return
        user_id = accounts.consume_link_code(parts[1])
        if not user_id:
            await message.answer("Код неверный или истёк. Получите новый в веб-приложении.")
            return
        async with AsyncSessionLocal() as db:
            await accounts.bind_telegram(db, user_id, message.from_user.id)
        await message.answer("Готово! Telegram привязан к аккаунту — бот работает с вашими персонажами и чатами.")

    @dp.message(Command("friends"))
    async def on_friends(message: TgMessage):
        ok, _ = await _gate(message)
        if not ok:
            return
        async with AsyncSessionLocal() as db:
            user = await accounts.user_by_telegram(db, message.from_user.id)
            if not user:
                await message.answer("Аккаунт не привязан. Привяжите: /link КОД")
                return
            reqs = (await db.execute(
                select(models.Friendship).where(
                    models.Friendship.friend_id == user.id,
                    models.Friendship.status == "pending",
                )
            )).scalars().all()
            if not reqs:
                await message.answer("Заявок в друзья нет.")
                return
            for f in reqs:
                other = await db.get(models.User, f.user_id)
                kb = InlineKeyboardBuilder()
                kb.button(text="✅ Принять", callback_data=f"fr:a:{f.id}")
                kb.button(text="✖ Отклонить", callback_data=f"fr:d:{f.id}")
                await message.answer(
                    f"👥 Заявка от {other.username if other else '?'}",
                    reply_markup=kb.as_markup(),
                )

    @dp.callback_query(F.data.startswith("fr:"))
    async def on_friend_action(cb: CallbackQuery):
        try:
            _, action, fid = cb.data.split(":")
            fid = int(fid)
        except ValueError:
            await cb.answer()
            return
        async with AsyncSessionLocal() as db:
            f = await db.get(models.Friendship, fid)
            user = await accounts.user_by_telegram(db, cb.from_user.id)
            if not f or not user or f.friend_id != user.id:
                await cb.answer("Заявка не найдена", show_alert=True)
                return
            if action == "a":
                f.status = "accepted"
                await db.commit()
                await cb.message.answer("✅ Заявка принята — теперь вы друзья.")
            else:
                await db.delete(f)
                await db.commit()
                await cb.message.answer("Заявка отклонена.")
        await cb.answer()

    @dp.message(Command("characters"))
    @dp.message(F.text == BTN_CHARS)
    async def on_characters(message: TgMessage):
        ok, owner_id = await _gate(message)
        if ok:
            await _show_characters(message, owner_id)

    @dp.callback_query(F.data.startswith("char:"))
    async def on_char_pick(cb: CallbackQuery):
        if not admin_service.is_whitelisted(cb.from_user.id):
            await cb.answer("Нет доступа", show_alert=True)
            return
        _, owner_id = await _resolve_owner(cb.from_user.id)
        cid = int(cb.data.split(":")[1])
        async with AsyncSessionLocal() as db:
            char = await db.get(models.Character, cid)
        if not char:
            await cb.answer("Персонаж не найден", show_alert=True)
            return
        await _create_session(cb.from_user.id, cid, owner_id=owner_id)
        await cb.message.answer(f"Теперь общаемся с персонажем: {char.name}")
        await cb.answer()

    @dp.message(Command("new"))
    @dp.message(F.text == BTN_NEW)
    async def on_new(message: TgMessage):
        ok, owner_id = await _gate(message)
        if ok:
            await _new_chat(message, owner_id)

    @dp.message(Command("model"))
    @dp.message(F.text == BTN_MODEL)
    async def on_model(message: TgMessage):
        ok, _ = await _gate(message)
        if ok:
            await _show_models(message)

    @dp.message(F.text == BTN_HELP)
    async def on_help(message: TgMessage):
        await message.answer(
            "🎭 Персонажи — выбрать персонажа\n🆕 Новый чат — начать заново\n"
            "🧠 Модель — сменить нейросеть\nКоманды: /characters /new /model /request"
        )

    @dp.callback_query(F.data.startswith("model:"))
    async def on_model_pick(cb: CallbackQuery):
        if not admin_service.is_whitelisted(cb.from_user.id):
            await cb.answer("Нет доступа", show_alert=True)
            return
        model = cb.data.split(":", 1)[1]
        async with AsyncSessionLocal() as db:
            await admin_service.set_telegram(db, {"model": model})
        await cb.message.answer(f"Модель бота: {model}")
        await cb.answer()

    @dp.message(F.voice)
    async def on_voice(message: TgMessage):
        ok, owner_id = await _gate(message)
        if not ok:
            return
        session_id = await _get_or_create_session(message.from_user.id, owner_id=owner_id)
        if session_id is None:
            await message.answer("В системе нет персонажей — создайте их в веб-интерфейсе.")
            return
        file = await _bot.get_file(message.voice.file_id)
        buf = await _bot.download_file(file.file_path)
        audio_b64 = base64.b64encode(buf.read()).decode()
        att = AttachmentIn(type="audio", data=audio_b64, mime="audio/ogg")
        await _bot.send_chat_action(message.chat.id, "typing")
        reply = await _generate_reply(session_id, "", [att])
        await send_long(message, reply)

    @dp.message(F.photo)
    @dp.message(F.document)
    @dp.message(F.audio)
    async def on_media(message: TgMessage):
        # Файлы (фото/документ/аудио). Альбом из нескольких файлов с подписью
        # собираем в ОДИН ход к нейросети (см. _handle_media).
        await _handle_media(message)

    @dp.message(F.text)
    async def on_text(message: TgMessage):
        ok, owner_id = await _gate(message)
        if not ok:
            return
        session_id = await _get_or_create_session(message.from_user.id, owner_id=owner_id)
        if session_id is None:
            await message.answer("В системе нет персонажей — создайте их в веб-интерфейсе.")
            return
        await _bot.send_chat_action(message.chat.id, "typing")
        reply = await _generate_reply(session_id, message.text, [])
        await send_long(message, reply)


async def start() -> None:
    """Запускает polling бота с токеном из настроек. Бросает, если токена нет."""
    global _task, _bot, _last_error
    if is_running():
        return
    token = (admin_service.telegram_cache().get("token") or "").strip()
    if not token:
        raise RuntimeError("Не задан токен бота (вкладка «Telegram» в админке)")
    _last_error = ""
    _bot = Bot(token=token)
    dp = Dispatcher()
    _register(dp)

    async def _run():
        global _last_error
        try:
            await dp.start_polling(_bot)
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001
            _last_error = str(exc)
            logger.exception("Telegram-бот остановился с ошибкой")
        finally:
            try:
                await _bot.session.close()
            except Exception:  # noqa: BLE001
                pass

    _task = asyncio.create_task(_run())


async def stop() -> None:
    global _task
    if _task and not _task.done():
        _task.cancel()
    _task = None
