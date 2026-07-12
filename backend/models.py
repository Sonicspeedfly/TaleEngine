"""
ORM-модели — единственный источник правды о структуре БД.
Этими же моделями пользуются и веб-сервер, и Telegram-бот.
"""
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.database import Base


class Character(Base):
    """Карточка персонажа. Поля совместимы с форматом карточек SillyTavern."""
    __tablename__ = "characters"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Владелец (для режима аккаунтов). NULL = общий/легаси (виден всем/админу).
    owner_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(200), index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    personality: Mapped[str] = mapped_column(Text, default="")
    scenario: Mapped[str] = mapped_column(Text, default="")
    first_message: Mapped[str] = mapped_column(Text, default="")
    system_prompt: Mapped[str] = mapped_column(Text, default="")
    # Аватар: data URI (base64) или путь/URL. Для простоты — строка.
    avatar_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Персональные параметры генерации, переопределяющие дефолты из .env.
    generation_params: Mapped[dict] = mapped_column(JSON, default=dict)
    model: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    sessions = relationship("ChatSession", back_populates="character")


class ChatSession(Base):
    """
    Сессия чата. user_key различает, кто ведёт диалог:
      'web:<uuid>'            — пользователь веб-интерфейса;
      'tg:<telegram_user_id>' — пользователь Telegram.
    """
    __tablename__ = "chat_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True, index=True
    )
    character_id: Mapped[int] = mapped_column(ForeignKey("characters.id"))
    user_key: Mapped[str] = mapped_column(String(200), index=True)
    title: Mapped[str] = mapped_column(String(300), default="Новый чат")
    # «Сцена» группового чата — общая ролевая обстановка для всех персонажей.
    scenario: Mapped[str] = mapped_column(Text, default="")
    # Заметка автора (Author's Note) — подмешивается в конец контекста.
    author_note: Mapped[str] = mapped_column(Text, default="")
    # Фон чата: пусто | CSS-градиент (пресет) | data:URI / URL картинки.
    background: Mapped[str] = mapped_column(Text, default="")
    # Групповой чат (несколько персонажей). character_id тогда — «ведущий»/первый.
    is_group: Mapped[bool] = mapped_column(Boolean, default=False)
    # Режиссёр: ИИ решает, кто из персонажей отвечает (иначе — по упоминанию имени).
    director: Mapped[bool] = mapped_column(Boolean, default=False)
    # Активная персона пользователя для этой сессии (кто такой "я" в ролевой игре).
    persona_id: Mapped[int | None] = mapped_column(
        ForeignKey("personas.id"), nullable=True
    )
    # Часовой пояс пользователя ДЛЯ ЭТОГО чата (IANA-имя или смещение "+03:00").
    # Нейросеть видит по нему текущее время собеседника; настраивается в UI на чат.
    timezone: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    character = relationship("Character", back_populates="sessions")
    messages = relationship(
        "Message", back_populates="session", order_by="Message.id"
    )


class Message(Base):
    """
    Одно сообщение в чате.

    Для поддержки «свайпов» (как в SillyTavern — несколько вариантов ответа) у
    ответа ассистента хранится список альтернатив `swipes` и индекс активной
    `active_swipe`. Поле `content` всегда зеркалит активный свайп — так старый
    код и история продолжают работать без изменений.
    """
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("chat_sessions.id"), index=True
    )
    role: Mapped[str] = mapped_column(String(20))  # 'user' | 'assistant' | 'system'
    content: Mapped[str] = mapped_column(Text, default="")
    attachments: Mapped[list] = mapped_column(JSON, default=list)
    # Альтернативные варианты ответа (свайпы) и индекс активного.
    swipes: Mapped[list] = mapped_column(JSON, default=list)
    active_swipe: Mapped[int] = mapped_column(Integer, default=0)
    # Какой моделью сгенерирован ответ (для информации в UI).
    model_used: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # В групповом чате — имя персонажа, который это сказал.
    speaker_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # Ответ на конкретное сообщение (id) — чтобы модель понимала, к чему обращаются.
    reply_to_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Если задано — это «плашка документа»: ответ ИИ открывается в Канвасе по клику,
    # а не выводится полотном в чат. content тогда — короткий заголовок/превью.
    canvas_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    session = relationship("ChatSession", back_populates="messages")


class AttachmentBlob(Base):
    """
    ДАННЫЕ (base64) вложения — отдельно от строки сообщения.

    В messages.attachments хранится только лёгкая мета {type, mime, name, size,
    blob_id}. Раньше base64 лежал прямо в JSON-колонке — и каждый ход/открытие
    чата поднимал в память сотни МБ (чат с парой видео = 170+ МБ на КАЖДОЕ
    чтение истории). Теперь тяжёлые данные достаются точечно и только когда
    реально нужны (показ вложения, retry, экспорт, вложения истории в лимите).
    """
    __tablename__ = "attachment_blobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    message_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    data: Mapped[str] = mapped_column(Text, default="")


class GroupMember(Base):
    """Участник группового чата (связь сессия ↔ персонаж)."""
    __tablename__ = "group_members"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("chat_sessions.id"), index=True
    )
    character_id: Mapped[int] = mapped_column(ForeignKey("characters.id"))


class HoraeEntry(Base):
    """
    Запись подсистемы памяти Horae.

    Совмещает идеи World Info (срабатывание по ключевым словам) и «снимков
    состояния» (always_on=True — подмешивается всегда: инвентарь, скрытые
    характеристики, текущее положение сюжета).
    """
    __tablename__ = "horae_entries"

    id: Mapped[int] = mapped_column(primary_key=True)
    # session_id IS NULL -> глобальная запись (общий лор мира для всех чатов).
    session_id: Mapped[int | None] = mapped_column(
        ForeignKey("chat_sessions.id"), nullable=True, index=True
    )
    # Привязка к персонажу (лорбук из карточки SillyTavern) — применяется в чатах
    # с этим персонажем. NULL = не привязано к персонажу.
    character_id: Mapped[int | None] = mapped_column(
        ForeignKey("characters.id"), nullable=True, index=True
    )
    category: Mapped[str] = mapped_column(String(50), default="lore")
    title: Mapped[str] = mapped_column(String(300), default="")
    content: Mapped[str] = mapped_column(Text, default="")
    keywords: Mapped[list] = mapped_column(JSON, default=list)
    always_on: Mapped[bool] = mapped_column(Boolean, default=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    priority: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class Persona(Base):
    """Персона пользователя — кем он отыгрывает (имя + описание + внешность)."""
    __tablename__ = "personas"

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    # Внешность ролевика (data:URI/URL) — можно показать нейросети (галочка в UI).
    avatar_path: Mapped[str | None] = mapped_column(Text, nullable=True)


class User(Base):
    """Аккаунт пользователя (режим аккаунтов). Первый зарегистрированный — админ."""
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(300))
    role: Mapped[str] = mapped_column(String(20), default="user")  # user | admin
    # Привязка Telegram: к аккаунту можно привязать Telegram-ID, и бот будет
    # работать с приватными персонажами/чатами этого пользователя.
    telegram_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    # Внешность ролевика по умолчанию (можно показать нейросети).
    avatar_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class UserToken(Base):
    """Токен сессии пользователя (простая авторизация по заголовку X-User-Token)."""
    __tablename__ = "user_tokens"

    token: Mapped[str] = mapped_column(String(100), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Friendship(Base):
    """Дружба между ролевиками. status: pending (заявка) | accepted."""
    __tablename__ = "friendships"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    friend_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class SessionShare(Base):
    """Доступ друга к чату: он видит чат у себя и может в нём участвовать."""
    __tablename__ = "session_shares"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("chat_sessions.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)


class Canvas(Base):
    """
    Канвас — документ или код, который правится рядом с чатом (как в Gemini).
    Создаётся из сообщения, редактируется вручную и дорабатывается с ИИ, экспортируется
    в Docx/PDF. Привязан к сессии (и опционально к исходному сообщению).
    """
    __tablename__ = "canvases"

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True, index=True
    )
    session_id: Mapped[int | None] = mapped_column(
        ForeignKey("chat_sessions.id"), nullable=True, index=True
    )
    source_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    title: Mapped[str] = mapped_column(String(300), default="Без названия")
    kind: Mapped[str] = mapped_column(String(20), default="document")  # document | code
    language: Mapped[str | None] = mapped_column(String(50), nullable=True)  # для кода
    content: Mapped[str] = mapped_column(Text, default="")
    # История версий (снимки прошлого содержимого) — для отката (undo).
    history: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class SamplingPreset(Base):
    """Сохранённый набор параметров генерации (Temperature, Top P и т.д.)."""
    __tablename__ = "sampling_presets"

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(200), unique=True)
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    # Пресет по умолчанию применяется автоматически при загрузке UI.
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)


class AppSetting(Base):
    """
    Универсальное key-value хранилище настроек уровня приложения.
    Сейчас используется для настроек подключения к LiteLLM (ключ 'connection'),
    чтобы их можно было менять прямо из интерфейса, а не только в .env.
    """
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON, default=dict)
