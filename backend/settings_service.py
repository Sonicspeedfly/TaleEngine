"""
Сервис настроек подключения к LiteLLM.

Главная идея: параметры подключения (адрес прокси, ключ, модель по умолчанию)
можно менять прямо в интерфейсе. Они сохраняются в БД (таблица app_settings,
ключ 'connection') и имеют приоритет над дефолтами из .env.

ВАЖНО: эти настройки читает и применяет ТОЛЬКО сервер — браузер никогда не ходит
в LiteLLM напрямую. Вся обработка остаётся на стороне сервера.
"""
import httpx
from sqlalchemy import select

from backend.config import settings
from backend.models import AppSetting

CONNECTION_KEY = "connection"


def default_connection() -> dict:
    """Дефолтные настройки подключения из .env (если в БД ещё ничего не сохранено)."""
    return {
        "use_proxy": settings.LITELLM_USE_PROXY,
        "base_url": settings.LITELLM_BASE_URL,
        "api_key": settings.LITELLM_API_KEY or "",
        "default_model": settings.DEFAULT_MODEL,
        "image_model": settings.LITELLM_IMAGE_MODEL or "",
        "image_via_chat": False,
        # Запасная модель на случай сбоя основной (+ авто-повтор ею).
        "fallback_model": "",
        "auto_fallback": True,
    }


async def get_connection(db) -> dict:
    """Возвращает действующие настройки подключения: сохранённые в БД поверх дефолтов."""
    row = await db.get(AppSetting, CONNECTION_KEY)
    conn = default_connection()
    if row and isinstance(row.value, dict):
        conn.update({k: v for k, v in row.value.items() if v is not None})
    return conn


async def set_connection(db, data: dict) -> dict:
    """Сохраняет (upsert) настройки подключения в БД и возвращает действующие."""
    row = await db.get(AppSetting, CONNECTION_KEY)
    # Берём только известные поля; строки чистим от пробелов/табов/переводов строк
    # (частая беда при копипасте — невидимый \t ломает URL у провайдера).
    allowed = {
        "use_proxy", "base_url", "api_key", "default_model", "image_model",
        "image_via_chat", "fallback_model", "auto_fallback",
    }
    clean = {}
    for k, v in data.items():
        if k not in allowed:
            continue
        clean[k] = v.strip() if isinstance(v, str) else v
    if row is None:
        row = AppSetting(key=CONNECTION_KEY, value=clean)
        db.add(row)
    else:
        # Сливаем со старым значением (частичное обновление).
        merged = dict(row.value or {})
        merged.update(clean)
        row.value = merged
    await db.commit()
    return await get_connection(db)


def _models_url(base_url: str) -> str:
    """Аккуратно строит URL до OpenAI-совместимого /v1/models у прокси."""
    base = base_url.rstrip("/")
    if base.endswith("/v1"):
        return f"{base}/models"
    return f"{base}/v1/models"


async def fetch_proxy_models(conn: dict) -> list[str]:
    """
    Спрашивает у LiteLLM-прокси список доступных моделей (GET /v1/models).
    Используется для выпадающего списка моделей и кнопки «Проверить подключение».
    """
    headers = {}
    if conn.get("api_key"):
        headers["Authorization"] = f"Bearer {conn['api_key']}"
    url = _models_url(conn["base_url"])
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        data = resp.json()
    # Ответ формата OpenAI: {"data": [{"id": "gpt-4o"}, ...]}
    return [m["id"] for m in data.get("data", []) if "id" in m]
