"""
Аккаунты пользователей (опциональный режим).

Простая, но безопасная авторизация без сторонних зависимостей:
  * пароли хранятся как pbkdf2-hash ('salt$hash');
  * вход выдаёт токен (X-User-Token), который резолвится в пользователя.

Режим аккаунтов включается в админке (security.accounts_enabled). Пока он выключен —
приложение работает как раньше (общие данные + код доступа). Данные каждого
пользователя помечаются owner_id; админ видит всё, обычный пользователь — своё.
"""
import hashlib
import hmac
import secrets
import time

from sqlalchemy import func as sqlfunc
from sqlalchemy import select

from backend.models import User, UserToken

# Одноразовые коды привязки Telegram: code -> (user_id, истекает_в).
_link_codes: dict[str, tuple[int, float]] = {}


def make_link_code(user_id: int) -> str:
    code = secrets.token_hex(3).upper()  # 6 hex-символов, удобно ввести в боте
    _link_codes[code] = (user_id, time.time() + 600)  # действителен 10 минут
    return code


def consume_link_code(code: str):
    entry = _link_codes.pop((code or "").strip().upper(), None)
    if not entry:
        return None
    user_id, expiry = entry
    return user_id if time.time() <= expiry else None


async def user_by_telegram(db, tg_id: int):
    return (
        await db.execute(select(User).where(User.telegram_id == int(tg_id)))
    ).scalars().first()


async def bind_telegram(db, user_id: int, tg_id: int) -> None:
    """Привязывает Telegram-ID к аккаунту (сняв его с других аккаунтов)."""
    # У этого tg_id не должно остаться привязки к другому аккаунту.
    for u in (await db.execute(select(User).where(User.telegram_id == int(tg_id)))).scalars().all():
        u.telegram_id = None
    user = await db.get(User, user_id)
    if user:
        user.telegram_id = int(tg_id)
    await db.commit()


def hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 200_000).hex()
    return f"{salt}${digest}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt, digest = stored.split("$", 1)
    except ValueError:
        return False
    check = hash_password(password, salt).split("$", 1)[1]
    return hmac.compare_digest(check, digest)


async def count_users(db) -> int:
    return (await db.execute(select(sqlfunc.count(User.id)))).scalar() or 0


async def register(db, username: str, password: str) -> User:
    """Создаёт пользователя. ПЕРВЫЙ зарегистрированный становится админом."""
    role = "admin" if (await count_users(db)) == 0 else "user"
    user = User(username=username, password_hash=hash_password(password), role=role)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def username_taken(db, username: str) -> bool:
    return (
        await db.execute(select(User).where(User.username == username))
    ).scalars().first() is not None


async def authenticate(db, username: str, password: str):
    user = (
        await db.execute(select(User).where(User.username == username))
    ).scalars().first()
    if user and verify_password(password, user.password_hash):
        return user
    return None


async def create_token(db, user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    db.add(UserToken(token=token, user_id=user_id))
    await db.commit()
    return token


async def user_from_token(db, token: str | None):
    if not token:
        return None
    row = await db.get(UserToken, token)
    if not row:
        return None
    return await db.get(User, row.user_id)


def scope_query(query, model, user):
    """
    Ограничивает выборку владельцем:
      * user is None (режим аккаунтов выключен / аноним) — без ограничений;
      * админ — видит всё;
      * обычный пользователь — только свои записи (owner_id == user.id).
    """
    if user is None or user.role == "admin":
        return query
    return query.where(model.owner_id == user.id)
