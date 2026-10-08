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
        # Переносим base64-данные вложений из строк сообщений в attachment_blobs
        # (одноразово; см. докстринг — иначе каждый ход тянет в память сотни МБ).
        await conn.run_sync(_migrate_attachment_blobs)
        # Разовая чистка «сирот» от старого некаскадного удаления чатов (см. ниже).
        await conn.run_sync(_cleanup_orphans)
        # Ветки и продолжения до 2.10.0 ссылались на файлы исходного чата —
        # даём им свои копии (см. _split_shared_blobs).
        await conn.run_sync(_split_shared_blobs)
        # Ссылки на копии файлов в хранилище модели уходят вместе с файлом.
        await conn.run_sync(_media_ref_trigger)


def _media_ref_trigger(sync_conn) -> None:
    """
    Триггер: удалили строку attachment_blobs (сообщение, чат, файл базы знаний —
    любым путём) — её ссылки на копии в хранилище провайдера (media_refs) тут же
    удаляются, а сами копии попадают в корзину media_ref_trash на удаление.
    Без этого SQLite, переиспользующий id удалённых строк, мог бы отдать модели
    ссылку на старый чужой файл вместо нового.
    """
    if not _is_sqlite:
        return
    from sqlalchemy import text

    sync_conn.execute(text(
        "CREATE TRIGGER IF NOT EXISTS media_refs_blob_gone AFTER DELETE ON attachment_blobs "
        "BEGIN "
        "INSERT INTO media_ref_trash(scope, uri) SELECT scope, uri FROM media_refs "
        "WHERE blob_id = OLD.id AND uri <> ''; "
        "DELETE FROM media_refs WHERE blob_id = OLD.id; "
        "END"
    ))


def _migrate_attachment_blobs(sync_conn) -> None:
    """
    Одноразовая (идемпотентная) миграция: инлайн-`data` из messages.attachments
    переезжает в таблицу attachment_blobs, в сообщении остаётся мета с blob_id.
    Без этого чат с видео весил сотни МБ ПРЯМО в строках таблицы, и каждое
    чтение истории (каждый ход, каждое открытие чата) поднимало их в память.
    """
    import json as _json

    from sqlalchemy import inspect, text

    tables = set(inspect(sync_conn).get_table_names())
    if "messages" not in tables or "attachment_blobs" not in tables:
        return
    rows = sync_conn.execute(text(
        "SELECT id, attachments FROM messages "
        "WHERE attachments IS NOT NULL AND attachments LIKE '%\"data\"%'"
    )).fetchall()
    moved = 0
    for mid, raw in rows:
        try:
            atts = _json.loads(raw)
        except Exception:  # noqa: BLE001 — битый JSON не должен ломать старт
            continue
        if not isinstance(atts, list):
            continue
        changed = False
        new_atts = []
        for a in atts:
            if isinstance(a, dict) and a.get("data"):
                data = a["data"]
                res = sync_conn.execute(
                    text("INSERT INTO attachment_blobs (message_id, data) VALUES (:m, :d)"),
                    {"m": mid, "d": data},
                )
                new_atts.append({
                    "type": a.get("type"), "mime": a.get("mime"), "name": a.get("name"),
                    "size": a.get("size") or int(len(data) * 0.75),
                    "blob_id": res.lastrowid,
                })
                changed = True
                moved += 1
            else:
                new_atts.append(a)
        if changed:
            sync_conn.execute(
                text("UPDATE messages SET attachments = :a WHERE id = :i"),
                {"a": _json.dumps(new_atts, ensure_ascii=False), "i": mid},
            )
    if moved:
        print(f"[migrate] Вложения вынесены из сообщений в attachment_blobs: {moved} шт. "
              "(файл БД можно ужать командой VACUUM при желании)")


def _split_shared_blobs(sync_conn) -> None:
    """
    Идемпотентно: у каждого сообщения — только свои файлы.

    До 2.10.0 ветка и «новый чат с памятью» копировали в сообщение ссылку на
    файл исходного сообщения (blob_id). Пока исходный чат жив, это работало;
    после его удаления файл пропадал, а освободившийся id SQLite отдавал файлу
    ДРУГОГО чата — и ветка показывала (и отправляла модели) чужой файл.

    Ссылку на файл копии (тот же автор и то же время создания — их копирование
    сохраняет) заменяем своей копией данных. Ссылку на файл постороннего
    сообщения — убираем: это уже чужой файл, остаётся пометка о вложении.
    """
    import json as _json

    from sqlalchemy import inspect, text

    tables = set(inspect(sync_conn).get_table_names())
    if "messages" not in tables or "attachment_blobs" not in tables:
        return
    owners = {bid: (mid, created, role) for bid, mid, created, role in sync_conn.execute(text(
        "SELECT b.id, b.message_id, m.created_at, m.role FROM attachment_blobs b "
        "LEFT JOIN messages m ON m.id = b.message_id WHERE b.message_id IS NOT NULL"
    )).fetchall()}
    rows = sync_conn.execute(text(
        "SELECT id, created_at, role, attachments FROM messages "
        "WHERE attachments IS NOT NULL AND attachments LIKE '%\"blob_id\"%'"
    )).fetchall()
    copied = dropped = 0
    for mid, created, role, raw in rows:
        try:
            atts = _json.loads(raw)
        except Exception:  # noqa: BLE001 — битый JSON не должен ломать старт
            continue
        if not isinstance(atts, list):
            continue
        changed = False
        for a in atts:
            if not isinstance(a, dict) or not a.get("blob_id"):
                continue
            try:
                bid = int(a["blob_id"])
            except (TypeError, ValueError):
                continue
            owner = owners.get(bid)
            if owner is None or owner[0] == mid:
                continue   # своё (или файла уже нет — отдаётся 404)
            if owner[1] == created and owner[2] == role:
                res = sync_conn.execute(text(
                    "INSERT INTO attachment_blobs (message_id, data) "
                    "SELECT :m, data FROM attachment_blobs WHERE id = :b"), {"m": mid, "b": bid})
                a["blob_id"] = res.lastrowid
                copied += 1
            else:
                a.pop("blob_id", None)
                dropped += 1
            changed = True
        if changed:
            sync_conn.execute(text("UPDATE messages SET attachments = :a WHERE id = :i"),
                              {"a": _json.dumps(atts, ensure_ascii=False), "i": mid})
    if copied or dropped:
        print(f"[migrate] Файлы веток отделены от исходных чатов: копий {copied}, "
              f"чужих ссылок убрано {dropped}")


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
    for tbl in ("messages", "group_members", "canvases", "session_shares", "horae_facts",
                "horae_chat_state", "horae_memory_docs"):
        if tbl in tables:
            sync_conn.execute(text(
                f"DELETE FROM {tbl} WHERE session_id NOT IN (SELECT id FROM chat_sessions)"
            ))
    if "horae_entries" in tables:  # глобальные (session_id IS NULL) не трогаем
        sync_conn.execute(text(
            "DELETE FROM horae_entries WHERE session_id IS NOT NULL "
            "AND session_id NOT IN (SELECT id FROM chat_sessions)"
        ))
        # Лорбук удалённого персонажа (до 2.9.0 удаление его не трогало): SQLite
        # отдаёт освободившийся id новому персонажу, и тот унаследовал бы чужие
        # записи. Записи с живым чатом остаются при чате, без персонажа.
        if "characters" in tables:
            sync_conn.execute(text(
                "DELETE FROM horae_entries WHERE session_id IS NULL AND character_id IS NOT NULL "
                "AND character_id NOT IN (SELECT id FROM characters)"
            ))
            sync_conn.execute(text(
                "UPDATE horae_entries SET character_id = NULL WHERE character_id IS NOT NULL "
                "AND character_id NOT IN (SELECT id FROM characters)"
            ))
    if "attachment_blobs" in tables:  # данные вложений удалённых сообщений
        sync_conn.execute(text(
            "DELETE FROM attachment_blobs WHERE message_id IS NOT NULL "
            "AND message_id NOT IN (SELECT id FROM messages)"
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
            "pinned_at": "DATETIME",
            "scenario": "TEXT DEFAULT ''",
            "timezone": "VARCHAR(64) DEFAULT ''",
            "profile_upto": "INTEGER DEFAULT 0",
            "profile_root": "INTEGER DEFAULT 0",
        },
        "messages": {
            "swipes": "JSON",
            "active_swipe": "INTEGER DEFAULT 0",
            "model_used": "VARCHAR(200)",
            "speaker_name": "VARCHAR(200)",
            "reply_to_id": "INTEGER",
            "canvas_id": "INTEGER",
            "horae": "JSON",
        },
        "horae_entries": {
            "character_id": "INTEGER",
            "meta": "JSON",
        },
        "sampling_presets": {
            "is_default": "BOOLEAN DEFAULT 0",
            "owner_id": "INTEGER",
        },
        "characters": {
            "owner_id": "INTEGER",
            "mes_example": "TEXT DEFAULT ''",
            "post_history_instructions": "TEXT DEFAULT ''",
            "pinned_at": "DATETIME",
            "horae_profile": "JSON",
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
