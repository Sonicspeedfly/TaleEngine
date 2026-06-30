"""
Telegram-бот на aiogram 3.x.

Ключевая идея ТЗ: бот и веб-интерфейс используют ОБЩУЮ базу данных и ОДИН и тот же
конвейер (Horae + LiteLLM). Пользователь может продолжить тот же чат в Telegram,
включая отправку голосовых — Gemini 1.5 Pro принимает аудио нативно (Whisper не нужен).

Запуск:  python -m telegram_bot.bot
"""
import asyncio
import base64

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import Message as TgMessage
from sqlalchemy import select

from backend import models
from backend.config import settings
from backend.database import AsyncSessionLocal, init_db
from backend.horae_memory import build_context_from_db
from backend.llm_gateway import build_user_content, stream_completion
from backend.schemas import AttachmentIn, GenerationParams
from backend.settings_service import get_connection

bot = Bot(token=settings.TELEGRAM_BOT_TOKEN)
dp = Dispatcher()


async def _get_or_create_session(tg_user_id: int) -> int:
    """
    Находит последнюю сессию Telegram-пользователя или создаёт новую с дефолтным
    персонажем. user_key в формате 'tg:<id>' — тот же формат сессий, что и в вебе.
    """
    user_key = f"tg:{tg_user_id}"
    async with AsyncSessionLocal() as db:
        q = (
            select(models.ChatSession)
            .where(models.ChatSession.user_key == user_key)
            .order_by(models.ChatSession.id.desc())
        )
        sess = (await db.execute(q)).scalars().first()
        if sess:
            return sess.id

        # Новой сессии нужен персонаж: берём из .env или первого в БД.
        char_id = settings.TELEGRAM_DEFAULT_CHARACTER_ID
        if char_id is None:
            char = (await db.execute(select(models.Character))).scalars().first()
            if char is None:
                raise RuntimeError(
                    "В БД нет ни одного персонажа — создайте его в веб-интерфейсе."
                )
            char_id = char.id

        sess = models.ChatSession(
            character_id=char_id, user_key=user_key, title="Telegram chat"
        )
        db.add(sess)
        await db.commit()
        await db.refresh(sess)
        return sess.id


async def _generate_reply(
    session_id: int, text: str, attachments: list[AttachmentIn]
) -> str:
    """
    Полный цикл: собрать контекст через Horae, сохранить сообщение пользователя,
    получить ответ LLM и сохранить его. В Telegram отправляем ответ целиком.
    """
    async with AsyncSessionLocal() as db:
        sess = await db.get(models.ChatSession, session_id)
        character = await db.get(models.Character, sess.character_id)
        # Те же настройки подключения к LiteLLM, что и в вебе (общая БД).
        connection = await get_connection(db)

        user_content = build_user_content(text, attachments)
        # Контекст строим ДО сохранения нового сообщения (иначе задвоится).
        messages = await build_context_from_db(
            db,
            sess,
            character,
            text,
            user_content,
            settings.CONTEXT_TOKEN_BUDGET,
        )

        db.add(
            models.Message(
                session_id=session_id,
                role="user",
                content=text,
                attachments=[a.model_dump() for a in attachments],
            )
        )
        await db.commit()

    # Собираем ответ из стрима. Те же параметры/настройки, что и в вебе.
    params = GenerationParams()  # при желании — подтянуть персональные настройки персонажа
    reply = ""
    async for token in stream_completion(messages, params, connection):
        reply += token

    # Сохраняем ответ ассистента в ту же общую БД (со свайпом для совместимости).
    async with AsyncSessionLocal() as db:
        db.add(
            models.Message(
                session_id=session_id,
                role="assistant",
                content=reply,
                swipes=[reply],
                active_swipe=0,
            )
        )
        await db.commit()
    return reply


@dp.message(CommandStart())
async def on_start(message: TgMessage):
    await _get_or_create_session(message.from_user.id)
    await message.answer(
        "Привет! Я подключён к той же памяти и персонажу, что и веб-чат.\n"
        "Пиши текстом или присылай голосовые."
    )


@dp.message(F.voice)
async def on_voice(message: TgMessage):
    """Голосовое: скачиваем файл и отдаём как аудио-вложение (без транскрипции)."""
    session_id = await _get_or_create_session(message.from_user.id)

    # Скачиваем голосовое сообщение (Telegram отдаёт ogg/opus).
    file = await bot.get_file(message.voice.file_id)
    buf = await bot.download_file(file.file_path)
    audio_b64 = base64.b64encode(buf.read()).decode()
    att = AttachmentIn(type="audio", data=audio_b64, mime="audio/ogg")

    await bot.send_chat_action(message.chat.id, "typing")
    reply = await _generate_reply(session_id, "", [att])
    await message.answer(reply or "(пустой ответ)")


@dp.message(F.text)
async def on_text(message: TgMessage):
    session_id = await _get_or_create_session(message.from_user.id)
    await bot.send_chat_action(message.chat.id, "typing")
    reply = await _generate_reply(session_id, message.text, [])
    await message.answer(reply or "(пустой ответ)")


async def main() -> None:
    # На случай, если бот стартовал раньше веб-сервера — создадим схему сами.
    await init_db()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
