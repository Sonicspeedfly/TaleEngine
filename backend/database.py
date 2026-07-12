"""
Настройка асинхронного подключения к БД через SQLAlchemy 2.0.

SQLite выбран ради простоты развёртывания: один файл, нулевая настройка.
И веб-сервер, и Telegram-бот используют ОДИН и тот же файл -> общая база данных.
"""
import os

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import NullPool

from backend.config import settings


class Base(DeclarativeBase):
    """Базовый класс для всех ORM-моделей (см. backend/models.py)."""
    pass


# echo=settings.DEBUG — печатать выполняемый SQL в консоль в режиме отладки.
# SQLite-нюансы:
#   * у файла ЕДИНСТВЕННЫЙ писатель, а фоновые задачи (авто-сводка Horae) пишут
#     параллельно с ходами — соединение должно ЖДАТЬ снятия блокировки
#     (connect_args timeout), а не падать сразу с "database is locked";
#   * NullPool: не переиспользуем соединения. Отмена asyncio-задачи посреди
#     запроса может вернуть в пул соединение с НЕЗАКРЫТОЙ транзакцией — оно
#     держит файл залоченным для всех. Свежее соединение на сессию дёшево.
_is_sqlite = settings.DATABASE_URL.startswith("sqlite")
engine = create_async_engine(
    settings.DATABASE_URL,
    echo=settings.DEBUG,
    future=True,
    connect_args={"timeout": 30} if _is_sqlite else {},
    **({"poolclass": NullPool} if _is_sqlite else {}),
)

# Фабрика сессий. expire_on_commit=False — чтобы ORM-объекты оставались
# пригодными к чтению после commit (удобно для возврата из эндпоинтов).
AsyncSessionLocal = async_sessionmaker(
    engine, expire_on_commit=False, class_=AsyncSession
)


async def get_session():
    """FastAPI-зависимость: выдаёт сессию БД и гарантированно закрывает её."""
    async with AsyncSessionLocal() as session:
        yield session


async def init_db() -> None:
    """
    Создаёт таблицы при старте, если их ещё нет.
    Вызывается и веб-сервером, и ботом — кто стартует первым, тот и создаст схему.
    """
    # Для SQLite убедимся, что директория для файла БД существует (например ./data).
    if settings.DATABASE_URL.startswith("sqlite"):
        db_path = settings.DATABASE_URL.split("///")[-1]
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)

    # Импортируем модели, чтобы они зарегистрировались в Base.metadata.
    from backend import models  # noqa: F401

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Лёгкая dev-миграция: дозаливаем недостающие колонки в уже существующую БД,
        # чтобы при обновлении схемы не приходилось удалять файл aichat.db вручную.
        await conn.run_sync(_sqlite_add_missing_columns)
        # Разовая чистка «сирот» от старого некаскадного удаления чатов (см. ниже).
        await conn.run_sync(_cleanup_orphans)


def _cleanup_orphans(sync_conn) -> None:
    """
    Одноразовая (идемпотентная) чистка данных, осиротевших из-за старого бага: до
    каскадного удаления чата удалялись только сообщения, а group_members / canvases /
    session_shares / session-horae оставались в БД. В SQLite id удалённого чата
    ПЕРЕИСПОЛЬЗУЕТСЯ, и новый групповой чат наследовал чужих участников (группа
    «пухла» с каждым пересозданием). Здесь: (1) удаляем строки, ссылающиеся на
    несуществующие чаты; (2) схлопываем дубли участников (оставляем самую раннюю).
    Выполняется при старте — когда пользователь обновит и перезапустит сервер.
    """
    from sqlalchemy import inspect, text

    tables = set(inspect(sync_conn).get_table_names())
    if "chat_sessions" not in tables:
        return
    # 1. Осиротевшие дочерние строки (чат, на который они ссылаются, уже удалён).
    for tbl in ("messages", "group_members", "canvases", "session_shares"):
        if tbl in tables:
            sync_conn.execute(text(
                f"DELETE FROM {tbl} WHERE session_id NOT IN (SELECT id FROM chat_sessions)"
            ))
    if "horae_entries" in tables:  # глобальные (session_id IS NULL) не трогаем
        sync_conn.execute(text(
            "DELETE FROM horae_entries WHERE session_id IS NOT NULL "
            "AND session_id NOT IN (SELECT id FROM chat_sessions)"
        ))
    # 2. Дубли участников группы: оставляем по одной строке на (чат, персонаж).
    if "group_members" in tables:
        sync_conn.execute(text(
            "DELETE FROM group_members WHERE id NOT IN "
            "(SELECT MIN(id) FROM group_members GROUP BY session_id, character_id)"
        ))


def _sqlite_add_missing_columns(sync_conn) -> None:
    """
    Добавляет недостающие колонки в существующие таблицы (только для SQLite).
    Это не полноценный Alembic, а удобство для разработки: новые поля появляются
    автоматически. Для прод-миграций используйте Alembic.
    """
    from sqlalchemy import inspect, text

    # Какие колонки должны быть (имя -> DDL-тип со значением по умолчанию).
    wanted = {
        "chat_sessions": {
            "author_note": "TEXT DEFAULT ''",
            "persona_id": "INTEGER",
            "background": "TEXT DEFAULT ''",
            "is_group": "BOOLEAN DEFAULT 0",
            "director": "BOOLEAN DEFAULT 0",
            "owner_id": "INTEGER",
            "scenario": "TEXT DEFAULT ''",
            "timezone": "VARCHAR(64) DEFAULT ''",
        },
        "messages": {
            "swipes": "JSON",
            "active_swipe": "INTEGER DEFAULT 0",
            "model_used": "VARCHAR(200)",
            "speaker_name": "VARCHAR(200)",
            "reply_to_id": "INTEGER",
            "canvas_id": "INTEGER",
        },
        "horae_entries": {
            "character_id": "INTEGER",
        },
        "sampling_presets": {
            "is_default": "BOOLEAN DEFAULT 0",
            "owner_id": "INTEGER",
        },
        "characters": {
            "owner_id": "INTEGER",
        },
        "personas": {
            "owner_id": "INTEGER",
            "avatar_path": "TEXT",
        },
        "users": {
            "telegram_id": "INTEGER",
        },
        "canvases": {
            "history": "JSON",
        },
    }
    inspector = inspect(sync_conn)
    existing_tables = set(inspector.get_table_names())
    for table, columns in wanted.items():
        if table not in existing_tables:
            continue
        present = {col["name"] for col in inspector.get_columns(table)}
        for name, ddl in columns.items():
            if name not in present:
                sync_conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
