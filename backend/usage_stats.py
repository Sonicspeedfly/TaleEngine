"""
Учёт расхода токенов.

Зачем: без цифр «экономия» — гадание. Провайдер в конце стрима присылает usage
(сколько токенов реально ушло во ВХОД, сколько из них взято из КЭША и сколько
сгенерировано в ВЫВОД). Мы это складываем по дням/моделям/видам запроса, и в
интерфейсе видно, что именно жжёт квоту: длинный контекст, пересылка файлов,
размышления или фоновые служебные вызовы (сводка сюжета, режиссёр группы).

Почему это важно для цены:
  * вход из кэша дешевле обычного входа в 4–10 раз (у Gemini — 75–90% скидки);
  * вывод дороже входа в разы, а «размышления» тарифицируются как вывод;
  * у Gemini вход СВЫШЕ ~200 тыс. токенов идёт по удвоенному тарифу целиком.

Запись идёт ФОНОМ и никогда не роняет генерацию: не смогли посчитать — молчим.
"""
import asyncio
import logging
import time

logger = logging.getLogger("aichat.usage")

# Последние N запросов с разбивкой по токенам — для панели 🐞 (в памяти процесса).
_recent: list[dict] = []
_RECENT_MAX = 100


def extract_usage(response_or_chunk) -> dict:
    """
    Достаёт usage из ответа/чанка LiteLLM в единый вид. Формат полей у провайдеров
    разный (и меняется от версии к версии), поэтому читаем максимально терпимо и
    любое расхождение трактуем как «данных нет».
    """
    u = getattr(response_or_chunk, "usage", None)
    if u is None and isinstance(response_or_chunk, dict):
        u = response_or_chunk.get("usage")
    if not u:
        return {}

    def _get(obj, *names):
        for n in names:
            v = getattr(obj, n, None)
            if v is None and isinstance(obj, dict):
                v = obj.get(n)
            if isinstance(v, (int, float)):
                return int(v)
        return 0

    prompt = _get(u, "prompt_tokens", "input_tokens")
    completion = _get(u, "completion_tokens", "output_tokens")
    # Кэшированный вход: OpenAI кладёт его в prompt_tokens_details.cached_tokens,
    # Gemini/Vertex — в cache_read_input_tokens, Anthropic — в cache_read_input_tokens.
    details = getattr(u, "prompt_tokens_details", None)
    if details is None and isinstance(u, dict):
        details = u.get("prompt_tokens_details")
    cached = _get(details, "cached_tokens") if details else 0
    if not cached:
        cached = _get(u, "cache_read_input_tokens", "cached_tokens")
    # Размышления: completion_tokens_details.reasoning_tokens (тратятся как вывод).
    cdet = getattr(u, "completion_tokens_details", None)
    if cdet is None and isinstance(u, dict):
        cdet = u.get("completion_tokens_details")
    reasoning = _get(cdet, "reasoning_tokens") if cdet else 0

    if not (prompt or completion):
        return {}
    return {
        "prompt": prompt,
        "cached": min(cached, prompt),  # кэш — часть входа, не сверх него
        "completion": completion,
        "reasoning": reasoning,
    }


def recent() -> list[dict]:
    """Последние запросы с токенами (новые сверху) — для отладочной панели."""
    return list(reversed(_recent))


def record(kind: str, model: str, usage: dict) -> None:
    """
    Зафиксировать расход. Синхронная и НЕблокирующая: складывает в память сразу,
    запись в БД уходит фоновой задачей. Вызывается из стрима генерации, поэтому
    обязана быть дешёвой и не бросать исключений.
    """
    if not usage:
        return
    _recent.append({
        "ts": time.strftime("%H:%M:%S"),
        "kind": kind,
        "model": model,
        **usage,
    })
    del _recent[:-_RECENT_MAX]
    try:
        asyncio.get_running_loop().create_task(_persist(kind, model, usage))
    except RuntimeError:
        pass  # нет активного цикла (тесты/синхронный контекст) — хватит памяти


async def _persist(kind: str, model: str, usage: dict) -> None:
    """Прибавляет расход к дневному агрегату (день, модель, вид)."""
    try:
        from sqlalchemy import select

        from backend import models
        from backend.database import AsyncSessionLocal

        day = time.strftime("%Y-%m-%d")
        # Имя модели чистим от служебного префикса маршрутизации litellm_proxy/.
        model = (model or "").split("/", 1)[-1] if (model or "").startswith("litellm_proxy/") else model
        async with AsyncSessionLocal() as db:
            row = (await db.execute(
                select(models.UsageDay).where(
                    models.UsageDay.day == day,
                    models.UsageDay.model == (model or ""),
                    models.UsageDay.kind == kind,
                )
            )).scalars().first()
            if row is None:
                row = models.UsageDay(day=day, model=model or "", kind=kind)
                db.add(row)
            row.requests = (row.requests or 0) + 1
            row.prompt_tokens = (row.prompt_tokens or 0) + usage.get("prompt", 0)
            row.cached_tokens = (row.cached_tokens or 0) + usage.get("cached", 0)
            row.completion_tokens = (row.completion_tokens or 0) + usage.get("completion", 0)
            row.reasoning_tokens = (row.reasoning_tokens or 0) + usage.get("reasoning", 0)
            await db.commit()
    except Exception:  # noqa: BLE001 — статистика не имеет права ломать чат
        logger.debug("Не удалось записать расход токенов", exc_info=True)


async def summary(db, days: int = 7) -> dict:
    """
    Сводка расхода за последние `days` дней: итоги + разбивка по дням, моделям и
    видам запроса. Отдаётся в UI (кнопка 📊 «Расход»).
    """
    from sqlalchemy import select

    from backend import models

    since = time.strftime("%Y-%m-%d", time.localtime(time.time() - days * 86400))
    rows = (await db.execute(
        select(models.UsageDay).where(models.UsageDay.day >= since)
    )).scalars().all()

    def _blank() -> dict:
        return {"requests": 0, "prompt": 0, "cached": 0, "completion": 0, "reasoning": 0}

    def _add(acc: dict, r) -> None:
        acc["requests"] += r.requests or 0
        acc["prompt"] += r.prompt_tokens or 0
        acc["cached"] += r.cached_tokens or 0
        acc["completion"] += r.completion_tokens or 0
        acc["reasoning"] += r.reasoning_tokens or 0

    total, by_day, by_model, by_kind = _blank(), {}, {}, {}
    for r in rows:
        _add(total, r)
        for bucket, key in ((by_day, r.day), (by_model, r.model or "?"), (by_kind, r.kind or "?")):
            _add(bucket.setdefault(key, _blank()), r)

    today = time.strftime("%Y-%m-%d")
    return {
        "days": days,
        "total": total,
        "today": by_day.get(today, _blank()),
        "by_day": [{"day": d, **v} for d, v in sorted(by_day.items(), reverse=True)],
        "by_model": [{"model": m, **v} for m, v in sorted(
            by_model.items(), key=lambda kv: -(kv[1]["prompt"] + kv[1]["completion"])
        )],
        "by_kind": [{"kind": k, **v} for k, v in sorted(
            by_kind.items(), key=lambda kv: -(kv[1]["prompt"] + kv[1]["completion"])
        )],
        "recent": recent()[:25],
    }
