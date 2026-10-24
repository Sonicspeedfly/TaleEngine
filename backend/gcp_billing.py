"""
Счёт Google Cloud прямо в TaleEngine (2.12.0): чтобы не заходить в биллинг,
пока кредит не на исходе.

Три источника, от точного к приблизительному:

  1. Cloud Billing API — включена ли оплата у проекта и открыт ли платёжный
     аккаунт. Закрытый аккаунт ломает и чат, и файлы («The billing account for
     the owning project is disabled»), поэтому это видно сразу.
  2. Оценка по токенам TaleEngine (таблица usage_days) × цены моделей. Цены
     берутся у прокси (/model/info: его LiteLLM знает цены своих моделей), если
     там нет — из LiteLLM этого сервера, можно задать и вручную. Видит только
     запросы TaleEngine: хранение файлов и другие сервисы проекта в неё не
     попадают.
  3. Экспорт биллинга в BigQuery — фактический расход по сервисам и списанные
     кредиты. Приходит с задержкой в несколько часов, поэтому за последний день
     берётся большее из факта и оценки.

Остаток кредита: сумма с экрана Billing → Credits на дату, которую указал
пользователь, минус расход после неё. Сам остаток пробного кредита Google по
API не отдаёт.

Вход в Google — тот же, что у прокси LiteLLM: Application Default Credentials
(`gcloud auth application-default login`) или файл ключа из
GOOGLE_APPLICATION_CREDENTIALS. Библиотека google-auth не нужна: токен входа
пользователя обновляется прямым запросом, остальные виды входа — через
google-auth (если установлена) или `gcloud auth application-default
print-access-token`.
"""
import asyncio
import json
import logging
import os
import re
import shutil
import time
from datetime import date, datetime, timedelta

from backend.config import settings

log = logging.getLogger("aichat.billing")

SETTINGS_KEY = "gcp_billing"
BILLING_API = "https://cloudbilling.googleapis.com/v1"
BQ_API = "https://bigquery.googleapis.com/bigquery/v2"
TOKEN_URL = "https://oauth2.googleapis.com/token"
# Как часто ходить в Google (секунды): состояние оплаты меняется редко,
# экспорт BigQuery обновляется несколько раз в день, цены — ещё реже.
STATUS_TTL = 3600
BQ_TTL = 3 * 3600
PRICES_TTL = 6 * 3600
ERROR_TTL = 600          # неудачу не повторяем на каждом открытии страницы
MAX_DAYS = 120           # глубже не считаем (и не сканируем BigQuery)

DEFAULTS = {
    "credit_amount": None,   # сколько кредита было на дату credit_since
    "credit_since": "",      # YYYY-MM-DD
    "credit_expires": "",    # YYYY-MM-DD — до какого дня действует кредит
    "warn_below": None,      # предупредить, когда остаток ниже (по умолчанию 10%)
    "project_id": "",        # пусто — проект квоты из входа Google
    "bigquery": True,        # читать экспорт биллинга, если он есть
    "bq_table": "",          # project.dataset.table — если автопоиск не нашёл
    "prices": {},            # {модель: {"in", "out", "cache"}} — $ за 1 млн токенов
}

_cache: dict[str, tuple[float, object]] = {}
_fetched: dict[str, float] = {}   # когда ответ действительно получен (для «проверено в …»)
_lock = asyncio.Lock()
_token: dict = {"value": "", "exp": 0.0, "key": ""}


def _now() -> float:
    return time.time()


def _today() -> date:
    return date.today()


# ================================ настройки ================================

def clean_settings(raw: dict | None) -> dict:
    """Настройки с умолчаниями; мусор отбрасывается, а не роняет страницу."""
    cfg = dict(DEFAULTS)
    for k, v in (raw or {}).items():
        if k in DEFAULTS:
            cfg[k] = v
    for k in ("credit_amount", "warn_below"):
        try:
            cfg[k] = None if cfg[k] in (None, "") else max(0.0, float(cfg[k]))
        except (TypeError, ValueError):
            cfg[k] = None
    for k in ("credit_since", "credit_expires"):
        cfg[k] = cfg[k] if _parse_day(cfg[k]) else ""
    cfg["project_id"] = str(cfg["project_id"] or "").strip()
    cfg["bq_table"] = str(cfg["bq_table"] or "").strip().strip("`")
    cfg["bigquery"] = bool(cfg["bigquery"])
    prices = {}
    for model, p in (cfg["prices"] or {}).items() if isinstance(cfg["prices"], dict) else []:
        if not isinstance(p, dict):
            continue
        row = {}
        for f in ("in", "out", "cache"):
            try:
                if p.get(f) not in (None, ""):
                    row[f] = max(0.0, float(p[f]))
            except (TypeError, ValueError):
                pass
        if row:
            prices[str(model)] = row
    cfg["prices"] = prices
    return cfg


async def load_settings(db) -> dict:
    from backend.models import AppSetting

    row = await db.get(AppSetting, SETTINGS_KEY)
    return clean_settings(row.value if row else None)


async def save_settings(db, values: dict) -> dict:
    from backend.models import AppSetting

    cfg = clean_settings(values)
    row = await db.get(AppSetting, SETTINGS_KEY)
    if row is None:
        db.add(AppSetting(key=SETTINGS_KEY, value=cfg))
    else:
        row.value = cfg
    await db.commit()
    # Проект или таблица могли смениться — прежние ответы Google не годятся.
    for key in ("status", "bq"):
        _cache.pop(key, None)
    return cfg


def _parse_day(s) -> date | None:
    try:
        return datetime.strptime(str(s or ""), "%Y-%m-%d").date()
    except ValueError:
        return None


# ================================ вход Google ================================

def adc_path() -> str:
    """Где лежит вход Google: GOOGLE_APPLICATION_CREDENTIALS или файл gcloud."""
    p = (settings.GOOGLE_APPLICATION_CREDENTIALS or "").strip()
    if p:
        return os.path.expanduser(p)
    if os.name == "nt":
        base = os.path.join(os.environ.get("APPDATA", ""), "gcloud")
    else:
        base = os.environ.get("CLOUDSDK_CONFIG") or os.path.join(os.path.expanduser("~"), ".config", "gcloud")
    return os.path.join(base, "application_default_credentials.json")


def _read_adc() -> dict:
    path = adc_path()
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise GoogleError(f"Не удалось прочитать вход Google ({path}): {exc}") from exc
    return data if isinstance(data, dict) else {}


class GoogleError(Exception):
    """Ошибка, которую можно показать человеку как есть."""


async def _request(method: str, url: str, *, token: str = "", quota: str = "",
                   json_body=None, data=None, timeout: float = 30) -> tuple[int, dict]:
    """Запрос к Google. Тесты подменяют эту функцию."""
    import httpx

    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if quota:
        headers["x-goog-user-project"] = quota
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.request(method, url, headers=headers, json=json_body, data=data)
    try:
        body = resp.json()
    except ValueError:
        body = {"error": {"message": resp.text[:300]}}
    return resp.status_code, body if isinstance(body, dict) else {"data": body}


def _gcloud_bin() -> str:
    found = shutil.which("gcloud")
    if found:
        return found
    home = os.path.join(os.path.expanduser("~"), "google-cloud-sdk", "bin", "gcloud")
    return home if os.path.exists(home) else ""


async def _token_from_gcloud() -> str:
    gcloud = _gcloud_bin()
    if not gcloud:
        return ""
    proc = await asyncio.create_subprocess_exec(
        gcloud, "auth", "application-default", "print-access-token",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, _err = await asyncio.wait_for(proc.communicate(), 40)
    except asyncio.TimeoutError:
        proc.kill()
        return ""
    return out.decode().strip() if proc.returncode == 0 else ""


async def _token_from_google_auth() -> str:
    try:
        import google.auth
        import google.auth.transport.requests
    except ImportError:
        return ""

    def _get():
        creds, _project = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        creds.refresh(google.auth.transport.requests.Request())
        return creds.token or ""

    return await asyncio.to_thread(_get)


async def access_token() -> tuple[str, dict]:
    """
    Токен Google и сведения о входе: {type, quota_project, path}.
    Бросает GoogleError с понятной причиной.
    """
    adc = _read_adc()
    info = {"type": adc.get("type") or "", "quota_project": adc.get("quota_project_id") or "",
            "path": adc_path()}
    key = f"{info['path']}|{adc.get('refresh_token') or adc.get('client_email') or ''}"
    if _token["value"] and _token["key"] == key and _token["exp"] > _now() + 120:
        return _token["value"], info

    token, ttl = "", 3000
    if info["type"] == "authorized_user" and adc.get("refresh_token"):
        code, body = await _request("POST", TOKEN_URL, data={
            "grant_type": "refresh_token", "client_id": adc.get("client_id", ""),
            "client_secret": adc.get("client_secret", ""), "refresh_token": adc["refresh_token"],
        })
        if code == 200 and body.get("access_token"):
            token, ttl = body["access_token"], int(body.get("expires_in") or 3600)
        elif code in (400, 401):
            raise GoogleError("Вход Google устарел или отозван. На сервере выполните "
                              "`gcloud auth application-default login --no-launch-browser`. "
                              "Ответ Google: " + _google_message(body))
    if not token:
        try:
            token = await _token_from_google_auth()
        except Exception as exc:  # noqa: BLE001
            log.debug("google-auth не выдал токен: %s", exc)
    if not token:
        token = await _token_from_gcloud()
    if not token:
        if not adc:
            raise GoogleError(f"На сервере нет входа Google ({info['path']}). Выполните "
                              "`gcloud auth application-default login --no-launch-browser` "
                              "под тем же пользователем, от которого работает TaleEngine, "
                              "или укажите файл ключа в GOOGLE_APPLICATION_CREDENTIALS.")
        raise GoogleError(f"Не удалось получить токен Google для входа типа «{info['type'] or '?'}». "
                          "Установите google-auth (pip install google-auth) или gcloud на сервер.")
    _token.update(value=token, exp=_now() + ttl, key=key)
    return token, info


def _google_message(body: dict) -> str:
    err = (body or {}).get("error")
    if isinstance(err, dict):
        return str(err.get("message") or err.get("status") or "")[:300]
    return str(body.get("error_description") or err or "")[:300]


def explain_google(code: int, body: dict, api: str, project: str) -> str:
    """Ответ Google с ошибкой → что сделать человеку."""
    msg = _google_message(body)
    low = json.dumps(body, ensure_ascii=False).lower()
    if "service_disabled" in low or "has not been used in project" in low or "it is disabled" in low:
        return (f"В проекте {project} выключен {api}. Включите его: console.cloud.google.com/apis/library/"
                f"{api}?project={project} — или командой из подсказки ниже.")
    if code == 401:
        return ("Google не принял вход. Выполните на сервере "
                "`gcloud auth application-default login --no-launch-browser`.")
    if code == 403:
        return f"Нет прав ({msg})."
    if code == 404:
        return f"Не найдено: {msg or 'проверьте ID проекта'}."
    return f"Google ответил {code}: {msg}"


# ============================ 1. состояние оплаты ============================

async def fetch_status(token: str, quota: str, project: str) -> dict:
    code, info = await _request("GET", f"{BILLING_API}/projects/{project}/billingInfo",
                                token=token, quota=quota)
    if code != 200:
        return {"ok": False, "error": explain_google(code, info, "cloudbilling.googleapis.com", project)}
    account = info.get("billingAccountName") or ""
    out = {"ok": True, "billing_enabled": bool(info.get("billingEnabled")),
           "account_id": account.split("/")[-1] if account else "",
           "account_open": None, "account_name": "", "currency": ""}
    if account:
        code, acc = await _request("GET", f"{BILLING_API}/{account}", token=token, quota=quota)
        if code == 200:
            out.update(account_open=bool(acc.get("open")), account_name=acc.get("displayName") or "",
                       currency=acc.get("currencyCode") or "")
        else:
            out["account_error"] = explain_google(code, acc, "cloudbilling.googleapis.com", project)
    return out


# ============================ 3. экспорт BigQuery ============================

EXPORT_PREFIX = "gcp_billing_export_v1_"
_TABLE_RE = re.compile(r"^[\w-]+(\.[\w-]+){2}$")

_SQL = """
SELECT FORMAT_DATE('%Y-%m-%d', DATE(usage_start_time, @tz)) AS day,
       IFNULL(service.description, '?') AS service,
       ANY_VALUE(currency) AS currency,
       SUM(cost) AS cost,
       SUM(IFNULL((SELECT SUM(c.amount) FROM UNNEST(credits) c), 0)) AS credits,
       SUM(IFNULL((SELECT SUM(c.amount) FROM UNNEST(credits) c WHERE c.type = 'PROMOTION'), 0)) AS promo,
       AVG(currency_conversion_rate) AS rate,
       FORMAT_TIMESTAMP('%Y-%m-%dT%H:%M:%SZ', MAX(export_time)) AS exported
FROM `{table}`
WHERE usage_start_time >= TIMESTAMP(@since, @tz)
GROUP BY day, service
"""


async def find_export_table(token: str, quota: str, project: str) -> tuple[str, list[str]]:
    """Таблица стандартного экспорта в проекте и список наборов данных (для подсказки)."""
    code, body = await _request("GET", f"{BQ_API}/projects/{project}/datasets?maxResults=200",
                                token=token, quota=quota)
    if code != 200:
        raise GoogleError(explain_google(code, body, "bigquery.googleapis.com", project))
    datasets = [((d.get("datasetReference") or {}).get("datasetId") or "") for d in body.get("datasets") or []]
    for ds in filter(None, datasets):
        code, tb = await _request("GET", f"{BQ_API}/projects/{project}/datasets/{ds}/tables?maxResults=1000",
                                  token=token, quota=quota)
        if code != 200:
            continue
        for t in tb.get("tables") or []:
            tid = (t.get("tableReference") or {}).get("tableId") or ""
            if tid.startswith(EXPORT_PREFIX):
                return f"{project}.{ds}.{tid}", datasets
    return "", [d for d in datasets if d]


def _tz_offset() -> str:
    """Часовой пояс сервера для BigQuery (+HH:MM): дни те же, что в usage_days."""
    off = time.strftime("%z") or "+0000"
    return f"{off[:3]}:{off[3:5]}"


def _rows(body: dict) -> list[dict]:
    fields = [f.get("name") for f in ((body.get("schema") or {}).get("fields") or [])]
    out = []
    for r in body.get("rows") or []:
        vals = [c.get("v") for c in r.get("f") or []]
        out.append(dict(zip(fields, vals)))
    return out


async def query_export(token: str, quota: str, table: str, since: date) -> dict:
    """Расход по дням и сервисам из экспорта (с даты since)."""
    project = table.split(".", 1)[0]
    payload = {
        "query": _SQL.format(table=table), "useLegacySql": False, "timeoutMs": 30000,
        "parameterMode": "NAMED",
        "queryParameters": [
            {"name": "since", "parameterType": {"type": "DATE"}, "parameterValue": {"value": since.isoformat()}},
            {"name": "tz", "parameterType": {"type": "STRING"}, "parameterValue": {"value": _tz_offset()}},
        ],
    }
    code, body = await _request("POST", f"{BQ_API}/projects/{project}/queries", token=token,
                                quota=quota, json_body=payload, timeout=60)
    if code != 200:
        raise GoogleError(explain_google(code, body, "bigquery.googleapis.com", project))
    # Небольшой экспорт отвечает сразу; иначе дожидаемся задания.
    for _ in range(4):
        if body.get("jobComplete", True):
            break
        job = body.get("jobReference") or {}
        loc = f"&location={job['location']}" if job.get("location") else ""
        code, body = await _request(
            "GET", f"{BQ_API}/projects/{project}/queries/{job.get('jobId')}?timeoutMs=30000{loc}",
            token=token, quota=quota, timeout=60)
        if code != 200:
            raise GoogleError(explain_google(code, body, "bigquery.googleapis.com", project))
    if not body.get("jobComplete", True):
        raise GoogleError("BigQuery не успел ответить — попробуйте обновить позже.")
    days: dict[str, dict] = {}
    services: list[dict] = []
    currency, exported, rates = "", "", []

    def num(v) -> float:
        try:
            return float(v or 0)
        except (TypeError, ValueError):
            return 0.0

    for r in _rows(body):
        cost, credits, promo = num(r.get("cost")), num(r.get("credits")), num(r.get("promo"))
        d = days.setdefault(r.get("day") or "", {"cost": 0.0, "credits": 0.0, "promo": 0.0})
        d["cost"] += cost
        d["credits"] += credits
        d["promo"] += promo
        services.append({"day": r.get("day") or "", "service": r.get("service") or "?",
                         "cost": cost, "credits": credits})
        currency = currency or (r.get("currency") or "")
        exported = max(exported, r.get("exported") or "")
        if r.get("rate") not in (None, ""):
            rates.append(num(r.get("rate")))
    return {"days": days, "services": services, "currency": currency, "exported_at": exported,
            "rate": (sum(rates) / len(rates)) if rates else None}


# ============================ 2. цены моделей ============================

_PRICE_FIELDS = {
    "in": "input_cost_per_token",
    "out": "output_cost_per_token",
    "cache": "cache_read_input_token_cost",
    "in_long": "input_cost_per_token_above_200k_tokens",
    "out_long": "output_cost_per_token_above_200k_tokens",
    "cache_long": "cache_read_input_token_cost_above_200k_tokens",
    "audio": "input_cost_per_audio_token",
}


def _price_from_info(info: dict | None) -> dict | None:
    """Цены за токен из model_info LiteLLM; None — цен нет."""
    if not isinstance(info, dict):
        return None
    p = {}
    for short, field in _PRICE_FIELDS.items():
        v = info.get(field)
        if isinstance(v, (int, float)) and v >= 0:
            p[short] = float(v)
    if "in" not in p or "out" not in p:
        return None
    return p


def _local_price(model: str) -> dict | None:
    """
    Цены из таблицы LiteLLM этого сервера (если прокси их не отдал). Только
    поиск по словарю litellm.model_cost: litellm.get_model_info для части
    провайдеров (ollama/…) ходит в сеть и может висеть минутами — а здесь мы
    в потоке сервера.
    """
    if not model:
        return None
    try:
        import litellm
    except ImportError:
        return None
    table = getattr(litellm, "model_cost", None) or {}
    bare = model.split("/", 1)[1] if "/" in model else model
    for name in (model, bare, f"vertex_ai/{bare}", f"gemini/{bare}"):
        got = _price_from_info(table.get(name))
        if got:
            return got
    return None


async def proxy_prices(connection: dict | None) -> dict[str, dict]:
    """
    {псевдоним: {"price": {...} | None, "model": модель провайдера}} из /model/info
    прокси. Пусто — прокси не задан или не ответил.
    """
    from backend.media_refs import route_for

    route = route_for(None, connection, model="_")
    if route is None:
        return {}
    import httpx

    headers = {"Authorization": f"Bearer {route.api_key}"}
    for path in ("/model/info", "/v1/model/info"):
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(route.base_url + path, headers=headers)
            if resp.status_code != 200:
                continue
            data = resp.json().get("data") or []
        except Exception:  # noqa: BLE001
            continue
        out: dict[str, dict] = {}
        for d in data:
            if not isinstance(d, dict) or not d.get("model_name"):
                continue
            under = (d.get("litellm_params") or {}).get("model") or ""
            price = _price_from_info(d.get("model_info"))
            prev = out.get(d["model_name"])
            if prev is None or (prev["price"] is None and price):
                out[d["model_name"]] = {"price": price, "model": under}
        return out
    return {}


def resolve_price(model: str, proxy: dict, overrides: dict) -> tuple[dict | None, str]:
    """Цена модели и откуда она: manual / proxy / local / ""."""
    base = None
    source = ""
    known = proxy.get(model) or {}
    if known.get("price"):
        base, source = dict(known["price"]), "proxy"
    else:
        for name in (known.get("model"), model):
            got = _local_price(name or "")
            if got:
                base, source = got, "local"
                break
    manual = overrides.get(model) or {}
    if manual:
        base = dict(base or {})
        for short in ("in", "out", "cache"):
            if short in manual:
                base[short] = manual[short] / 1e6
        source = "manual"
        if "in" not in base or "out" not in base:
            return None, ""
    return base, source


def row_cost(r, p: dict) -> float:
    """Стоимость строки usage_days по ценам за токен (в долларах)."""
    inp, out = p["in"], p["out"]
    cache = p.get("cache", inp)
    long_p = getattr(r, "long_prompt_tokens", 0) or 0
    long_c = min(getattr(r, "long_cached_tokens", 0) or 0, long_p)
    long_o = getattr(r, "long_completion_tokens", 0) or 0
    prompt = max(0, (r.prompt_tokens or 0) - long_p)
    cached = min(max(0, (r.cached_tokens or 0) - long_c), prompt)
    output = max(0, (r.completion_tokens or 0) - long_o)
    fresh = prompt - cached
    audio = min(getattr(r, "audio_tokens", 0) or 0, fresh)
    cost = (fresh - audio) * inp + audio * p.get("audio", inp) + cached * cache + output * out
    cost += ((long_p - long_c) * p.get("in_long", inp) + long_c * p.get("cache_long", cache)
             + long_o * p.get("out_long", out))
    return cost


async def estimate(db, since: date, prices: dict, overrides: dict) -> dict:
    """Оценка по usage_days с даты since: по дням и по моделям (в долларах)."""
    from sqlalchemy import select

    from backend.models import UsageDay

    rows = (await db.execute(select(UsageDay).where(UsageDay.day >= since.isoformat()))).scalars().all()
    by_day: dict[str, float] = {}
    by_model: dict[str, dict] = {}
    for r in rows:
        model = r.model or "?"
        m = by_model.get(model)
        if m is None:
            price, source = resolve_price(model, prices, overrides)
            m = by_model[model] = {"model": model, "cost": 0.0, "tokens": 0, "price": price,
                                   "source": source, "under": (prices.get(model) or {}).get("model") or ""}
        m["tokens"] += (r.prompt_tokens or 0) + (r.completion_tokens or 0)
        if m["price"] is None:
            continue
        c = row_cost(r, m["price"])
        m["cost"] += c
        by_day[r.day] = by_day.get(r.day, 0.0) + c
    return {"by_day": by_day, "models": by_model}


# ================================ сводка ================================

async def _cached(name: str, ttl: float, force: bool, fn, empty_is_error: bool = False):
    hit = _cache.get(name)
    if hit and not force and _now() - hit[0] < ttl:
        return hit[1]
    try:
        value = await fn()
    except GoogleError as exc:
        value = {"error": str(exc)}
    except Exception as exc:  # noqa: BLE001 — сеть, таймаут: показываем, а не падаем
        log.warning("Биллинг Google: %s", exc)
        value = {"error": f"Не удалось связаться с Google: {type(exc).__name__}: {exc}"[:300]}
    failed = isinstance(value, dict) and (value.get("error") or (empty_is_error and not value))
    # Неудачу кэшируем коротко: исправили настройку — через 10 минут увидим.
    _cache[name] = (_now() - (ttl - ERROR_TTL if failed and ttl > ERROR_TTL else 0), value)
    _fetched[name] = _now()
    return value


def _money(x: float | None) -> float | None:
    return None if x is None else round(x, 4)


async def snapshot(db, connection: dict | None, force: bool = False) -> dict:
    """Всё для карточки «Google Cloud» одним ответом."""
    async with _lock:
        return await _snapshot(db, connection, force)


async def _snapshot(db, connection, force):
    cfg = await load_settings(db)
    today = _today()
    month_start = today.replace(day=1)
    credit_since = _parse_day(cfg["credit_since"])
    since = min(month_start, credit_since or month_start)
    since = max(since, today - timedelta(days=MAX_DAYS))
    # alerts — срочное (оплата выключена, кредит на исходе): интерфейс покажет
    # уведомлением; notes — пояснения к цифрам.
    alerts: list[str] = []
    notes: list[str] = []

    token = ""
    try:
        token, info = await access_token()
        auth = {"ok": True, **info}
    except GoogleError as exc:
        auth = {"ok": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 — сеть до Google
        auth = {"ok": False, "error": f"Не удалось связаться с Google: {type(exc).__name__}: {exc}"[:300]}
    project = cfg["project_id"] or (settings.VERTEX_PROJECT or "").strip() or auth.get("quota_project") or ""
    quota = project

    status = None
    if token and project:
        status = await _cached("status", STATUS_TTL, force,
                               lambda: fetch_status(token, quota, project))
        if status.get("ok"):
            if status.get("account_open") is False:
                alerts.append("Платёжный аккаунт Google Cloud закрыт — запросы к моделям и файлам "
                                "будут отклоняться. Откройте Billing в консоли.")
            elif not status.get("billing_enabled"):
                alerts.append(f"У проекта {project} не включена оплата (нет платёжного аккаунта).")
    elif auth.get("ok") and not project:
        auth = {**auth, "error": "Не знаю ID проекта: впишите его в настройках карточки."}

    bq = {"state": "off"}
    if cfg["bigquery"] and token and project:
        async def _bq():
            table = cfg["bq_table"] if _TABLE_RE.match(cfg["bq_table"] or "") else ""
            datasets: list[str] = []
            if not table:
                table, datasets = await find_export_table(token, quota, project)
            if not table:
                return {"state": "no_table", "datasets": datasets}
            data = await query_export(token, quota, table, since)
            return {"state": "ok", "table": table, **data}
        bq = await _cached("bq", BQ_TTL, force, _bq)
        if bq.get("error"):
            bq = {"state": "error", "error": bq["error"]}

    prices = await _cached("prices", PRICES_TTL, force, lambda: proxy_prices(connection), empty_is_error=True)
    if not isinstance(prices, dict) or prices.get("error"):
        prices = {}
    est = await estimate(db, since, prices, cfg["prices"])

    # Валюта счёта; оценка считается в долларах и переводится по курсу экспорта.
    currency = (status or {}).get("currency") or bq.get("currency") or "USD"
    rate = 1.0
    if currency != "USD":
        if bq.get("rate"):
            rate = float(bq["rate"])
        else:
            notes.append(f"Счёт в {currency}, а оценка — в долларах: точный курс появится с экспортом BigQuery.")

    exact_days: dict = (bq.get("days") or {}) if bq.get("state") == "ok" else {}
    last_exact = max(exact_days) if exact_days else ""
    by_day = []
    for d in sorted(set(est["by_day"]) | set(exact_days)):
        if d < since.isoformat():
            continue
        e = est["by_day"].get(d, 0.0) * rate
        x = exact_days.get(d)
        # Потрачено против кредита: расход за вычетом скидок, кроме самого
        # пробного кредита (он и есть то, что уменьшается).
        spend_exact = None if x is None else max(0.0, x["cost"] + x["credits"] - x["promo"])
        if spend_exact is not None and d < last_exact:
            spent = spend_exact
        else:
            spent = max(spend_exact or 0.0, e)
        by_day.append({"day": d, "estimate": _money(e), "exact": _money(spend_exact),
                       "net": _money(None if x is None else x["cost"] + x["credits"]), "spent": _money(spent)})

    def total(rows, start: str, end: str | None = None, key="spent"):
        return sum((r[key] or 0) for r in rows if r["day"] >= start and (end is None or r["day"] <= end))

    month = month_start.isoformat()
    month_rows = [r for r in by_day if r["day"] >= month]
    out = {
        "configured": bool(auth.get("ok")),
        "auth": {k: v for k, v in auth.items() if k in ("ok", "type", "quota_project", "path", "error")},
        "project": project,
        "status": status,
        "bigquery": {k: v for k, v in bq.items() if k in ("state", "table", "exported_at", "error", "datasets")},
        "currency": currency,
        "rate": rate,
        "today": {"spent": _money(total(by_day, today.isoformat(), today.isoformat())),
                  "estimate": _money(est["by_day"].get(today.isoformat(), 0.0) * rate)},
        "month": {"spent": _money(total(month_rows, month)),
                  "net": _money(sum(r["net"] for r in month_rows if r["net"] is not None))
                  if any(r["net"] is not None for r in month_rows) else None,
                  "exact_until": last_exact or None},
        "by_day": list(reversed(by_day)),
        "by_service": _services(bq, month),
        "models": [
            {"model": m["model"], "cost": _money(m["cost"] * rate), "tokens": m["tokens"],
             "source": m["source"], "under": m["under"],
             "price": None if not m["price"] else {k: round(m["price"][k] * 1e6, 4)
                                                   for k in ("in", "out", "cache") if k in m["price"]}}
            for m in sorted(est["models"].values(), key=lambda m: -m["cost"])
        ],
        "credit": None,
        "settings": cfg,
        "alerts": alerts,
        "notes": notes,
        "checked_at": datetime.fromtimestamp(_fetched.get("status") or _now()).isoformat(timespec="minutes"),
    }
    unpriced = [m["model"] for m in out["models"] if m["price"] is None and m["tokens"]]
    if unpriced:
        notes.append("Нет цены для: " + ", ".join(unpriced[:5]) + " — их расход не вошёл в оценку. "
                        "Цену можно задать вручную.")
    if cfg["credit_amount"] is not None and credit_since:
        used = total(by_day, credit_since.isoformat())
        remaining = cfg["credit_amount"] - used
        expires = _parse_day(cfg["credit_expires"])
        threshold = cfg["warn_below"] if cfg["warn_below"] is not None else cfg["credit_amount"] * 0.1
        low = remaining <= threshold
        out["credit"] = {
            "amount": cfg["credit_amount"], "since": credit_since.isoformat(),
            "used": _money(used), "remaining": _money(remaining), "threshold": _money(threshold),
            "expires": expires.isoformat() if expires else None,
            "days_left": (expires - today).days if expires else None,
            "low": low, "exact_until": last_exact or None,
        }
        if remaining <= 0:
            alerts.append("Кредит Google Cloud, судя по расходу, закончился — дальше оплата идёт с карты.")
        elif low:
            alerts.append(f"Кредита Google Cloud осталось мало: ≈ {_fmt(remaining, currency)}.")
        if expires and 0 <= (expires - today).days <= 7:
            alerts.append(f"Кредит Google Cloud истекает {expires.strftime('%d.%m.%Y')}.")
    return out


def _fmt(amount: float, currency: str) -> str:
    return f"${amount:.2f}" if currency == "USD" else f"{amount:.2f} {currency}"


def _services(bq: dict, month: str) -> list[dict]:
    if bq.get("state") != "ok":
        return []
    acc: dict[str, dict] = {}
    for s in bq.get("services") or []:
        if s["day"] < month:
            continue
        a = acc.setdefault(s["service"], {"service": s["service"], "cost": 0.0, "credits": 0.0})
        a["cost"] += s["cost"]
        a["credits"] += s["credits"]
    return [{**a, "cost": _money(a["cost"]), "credits": _money(a["credits"])}
            for a in sorted(acc.values(), key=lambda a: -a["cost"])]
