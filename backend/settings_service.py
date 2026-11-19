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
        # Память Horae: быстрая модель для фоновой сводки и извлечения фактов
        # (пусто = модель чата) и модель эмбеддингов для поиска фактов (пусто =
        # сравнение по словам, без векторов).
        "summary_model": "",
        "embedding_model": "",
        # Сколько ждать ответа модели, секунды (2.29.1): 0 — без своего
        # ограничения (до потолка хода), N — до первого ответа и тишины посреди
        # него. Главнее REQUEST_TIMEOUT/STREAM_IDLE_TIMEOUT из .env.
        "model_wait": 0,
        # Аккаунты Google на прокси (2.25.0): у каждого — префикс имён моделей
        # в LiteLLM («acc2/» → в config.yaml шаблон «acc2/*» со своим ключом).
        # Активный дописывает свой префикс ко ВСЕМ запросам: ход, память,
        # Хроника, картинки, эмбеддинги, файлы. Первый — основной, без префикса.
        "accounts": [{"name": "Основной", "prefix": ""}],
        "active_account": 0,
    }


def clean_accounts(raw) -> list[dict]:
    """Список аккаунтов из запроса: имя до 40 знаков, префикс без пробелов и с «/» на конце."""
    out: list[dict] = []
    seen: set[str] = set()
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()[:40]
        prefix = "".join(str(item.get("prefix") or "").split())[:40]
        if prefix and not prefix.endswith("/"):
            prefix += "/"
        if prefix in seen:
            continue    # два аккаунта с одним префиксом — это один аккаунт
        seen.add(prefix)
        out.append({"name": name or (f"Аккаунт {len(out) + 1}" if prefix else "Основной"), "prefix": prefix})
    if not out or out[0]["prefix"]:
        # Первый — всегда основной (без префикса): им пользуется всё, что не
        # знает про аккаунты, и к нему можно вернуться одним нажатием.
        out = [{"name": "Основной", "prefix": ""}] + [a for a in out if a["prefix"]]
    return out[:8]


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
        "summary_model", "embedding_model", "model_wait", "accounts", "active_account",
    }
    clean = {}
    for k, v in data.items():
        if k not in allowed:
            continue
        if k in ("accounts", "active_account") and v is None:
            continue    # не прислали — оставляем сохранённое
        clean[k] = v.strip() if isinstance(v, str) else v
    if "accounts" in clean:
        clean["accounts"] = clean_accounts(clean["accounts"])
    if "accounts" in clean and "active_account" not in clean and "active_prefix" not in data:
        # Список сменился, а активный не прислали — держимся прежнего по префиксу.
        old = (row.value or {}) if row else {}
        old_list = old.get("accounts") or []
        try:
            old_idx = int(old.get("active_account") or 0)
        except (TypeError, ValueError):
            old_idx = 0
        prev = old_list[old_idx].get("prefix", "") if 0 <= old_idx < len(old_list) and isinstance(old_list[old_idx], dict) else ""
        clean["active_account"] = next((i for i, a in enumerate(clean["accounts"]) if a.get("prefix") == prev), 0)
    elif "active_account" in clean or "active_prefix" in data:
        accounts = clean.get("accounts") or ((row.value or {}).get("accounts") if row else None) \
            or default_connection()["accounts"]
        # Активный — по ПРЕФИКСУ, а не по номеру: чистка списка выкидывает
        # пустые и повторные строки, и номер из запроса указал бы на соседа —
        # запросы ушли бы в чужой проект (ревью 2.25.0).
        want = data.get("active_prefix")
        if want is None:
            raw = data.get("accounts") if isinstance(data.get("accounts"), list) else None
            try:
                idx = int(clean.get("active_account") or 0)
            except (TypeError, ValueError):
                idx = 0
            if raw is not None and 0 <= idx < len(raw) and isinstance(raw[idx], dict):
                want = raw[idx].get("prefix") or ""
            elif 0 <= idx < len(accounts):
                want = accounts[idx].get("prefix") or ""
            else:
                want = ""
        want = "".join(str(want).split())
        if want and not want.endswith("/"):
            want += "/"
        clean["active_account"] = next((i for i, a in enumerate(accounts) if a.get("prefix") == want), 0)
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
    # Шаблоны («acc2/*») и модели других аккаунтов в выбор не идут: модель
    # выбирают одну, а аккаунт к ней дописывается сам (2.25.0).
    prefixes = [a["prefix"] for a in clean_accounts(conn.get("accounts")) if a["prefix"]]
    return [m["id"] for m in data.get("data", []) if "id" in m
            and "*" not in m["id"] and not any(m["id"].startswith(p) for p in prefixes)]
