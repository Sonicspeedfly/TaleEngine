"""
Сервис администрирования и безопасности.

Хранит в БД (таблица app_settings) две группы настроек:
  * 'security' — код доступа к приложению и пароль администратора;
  * 'telegram' — токен бота, флаг включения, персонаж по умолчанию,
                 белый список Telegram-ID и заявки на доступ.

Значения кэшируются в памяти процесса, чтобы middleware проверки доступа и
Telegram-бот не дёргали БД на каждый запрос/сообщение. Кэш обновляется при записи.
"""
from backend.config import settings as cfg
from backend.models import AppSetting

SECURITY_KEY = "security"
TELEGRAM_KEY = "telegram"

# Значение по умолчанию для HTTP Basic Auth (защита на уровне браузера).
def _default_basic_auth() -> dict:
    return {"enabled": False, "username": "", "password": ""}


# Кэш в памяти (читается синхронно из middleware и бота).
_security_cache: dict = {"access_code": "", "admin_password": "", "basic_auth": _default_basic_auth()}
_telegram_cache: dict = {
    "token": "",
    "enabled": False,
    "default_character_id": None,
    "whitelist": [],
    "requests": [],
}


def default_security() -> dict:
    # accounts_enabled=True -> вход по логину/паролю (режим аккаунтов), данные приватны.
    # basic_auth -> HTTP Basic Auth: браузер спрашивает логин/пароль ДО загрузки
    # приложения (защита на уровне HTTP, поверх внутреннего входа).
    return {
        "access_code": "",
        "admin_password": "",
        "accounts_enabled": False,
        "basic_auth": _default_basic_auth(),
    }


def default_telegram() -> dict:
    return {
        "token": cfg.TELEGRAM_BOT_TOKEN or "",
        "enabled": False,
        "default_character_id": cfg.TELEGRAM_DEFAULT_CHARACTER_ID,
        # Модель, которой отвечает бот (имя как в прокси). Пусто = модель по умолчанию.
        "model": "",
        # open_to_all=False -> доступ ТОЛЬКО из белого списка (по умолчанию закрыто).
        # True -> бот открыт всем. Так не-внесённые в список получают отказ, а не ответ.
        "open_to_all": False,
        "whitelist": [],
        "requests": [],
    }


async def _load(db, key: str, default: dict) -> dict:
    row = await db.get(AppSetting, key)
    data = dict(default)
    if row and isinstance(row.value, dict):
        data.update(row.value)
    return data


async def _save(db, key: str, data: dict) -> None:
    row = await db.get(AppSetting, key)
    if row is None:
        db.add(AppSetting(key=key, value=data))
    else:
        row.value = data
    await db.commit()


async def load_caches(db) -> None:
    """Загрузить оба кэша из БД (вызывается на старте сервера)."""
    global _security_cache, _telegram_cache
    _security_cache = await _load(db, SECURITY_KEY, default_security())
    _telegram_cache = await _load(db, TELEGRAM_KEY, default_telegram())


def security_cache() -> dict:
    return _security_cache


def telegram_cache() -> dict:
    return _telegram_cache


# ---------- Безопасность ----------
async def get_security(db) -> dict:
    return await _load(db, SECURITY_KEY, default_security())


async def set_security(db, data: dict) -> dict:
    global _security_cache
    cur = await get_security(db)
    for k in ("access_code", "admin_password"):
        if k in data and data[k] is not None:
            cur[k] = str(data[k])
    if "accounts_enabled" in data and data["accounts_enabled"] is not None:
        cur["accounts_enabled"] = bool(data["accounts_enabled"])
    if isinstance(data.get("basic_auth"), dict):
        ba = dict(cur.get("basic_auth") or _default_basic_auth())
        incoming = data["basic_auth"]
        if "enabled" in incoming:
            ba["enabled"] = bool(incoming["enabled"])
        for k in ("username", "password"):
            if incoming.get(k) is not None:
                ba[k] = str(incoming[k])
        cur["basic_auth"] = ba
    await _save(db, SECURITY_KEY, cur)
    _security_cache = cur
    return cur


# ---------- Telegram ----------
async def get_telegram(db) -> dict:
    return await _load(db, TELEGRAM_KEY, default_telegram())


async def set_telegram(db, data: dict) -> dict:
    global _telegram_cache
    cur = await get_telegram(db)
    for k in ("token", "enabled", "default_character_id", "model", "open_to_all"):
        if k in data:
            cur[k] = data[k]
    await _save(db, TELEGRAM_KEY, cur)
    _telegram_cache = cur
    return cur


async def add_whitelist(db, tg_id: int) -> dict:
    global _telegram_cache
    cur = await get_telegram(db)
    wl = set(int(i) for i in cur.get("whitelist", []))
    wl.add(int(tg_id))
    cur["whitelist"] = sorted(wl)
    # Если был в заявках — убираем оттуда.
    cur["requests"] = [r for r in cur.get("requests", []) if r.get("id") != int(tg_id)]
    await _save(db, TELEGRAM_KEY, cur)
    _telegram_cache = cur
    return cur


async def remove_whitelist(db, tg_id: int) -> dict:
    global _telegram_cache
    cur = await get_telegram(db)
    cur["whitelist"] = [i for i in cur.get("whitelist", []) if int(i) != int(tg_id)]
    await _save(db, TELEGRAM_KEY, cur)
    _telegram_cache = cur
    return cur


async def add_request(db, tg_id: int, username: str = "", first_name: str = "") -> dict:
    """Добавляет заявку на доступ (команда /request в боте)."""
    global _telegram_cache
    cur = await get_telegram(db)
    if int(tg_id) in [int(i) for i in cur.get("whitelist", [])]:
        return cur  # уже есть доступ
    reqs = cur.get("requests", [])
    if not any(r.get("id") == int(tg_id) for r in reqs):
        reqs.append({"id": int(tg_id), "username": username or "", "first_name": first_name or ""})
        cur["requests"] = reqs
        await _save(db, TELEGRAM_KEY, cur)
        _telegram_cache = cur
    return cur


def is_whitelisted(tg_id: int) -> bool:
    """
    Доступ к боту:
      * open_to_all=True  -> пускаем всех;
      * иначе              -> только тех, кто в белом списке (пустой список = никого).
    Так пользователи не из списка получают отказ, а не сгенерированный ответ.
    """
    if _telegram_cache.get("open_to_all"):
        return True
    wl = [int(i) for i in _telegram_cache.get("whitelist", [])]
    return int(tg_id) in wl
