"""
Счёт Google Cloud (2.12.0): состояние оплаты, оценка по токенам и экспорт BigQuery.
Google не вызывается: тесты подменяют gcp_billing._request и цены прокси.
"""
import json
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from backend import gcp_billing as gb
from backend.config import settings

PROJECT = "proj-1"
ACCOUNT = "billingAccounts/0000-1111-2222"


@pytest.fixture
async def db_ready(tmp_path, monkeypatch):
    from backend.database import init_db

    await init_db()
    adc = tmp_path / "adc.json"
    adc.write_text(json.dumps({"type": "authorized_user", "client_id": "cid", "client_secret": "sec",
                               "refresh_token": "rt", "quota_project_id": PROJECT}))
    monkeypatch.setattr(settings, "GOOGLE_APPLICATION_CREDENTIALS", str(adc))
    monkeypatch.setattr(settings, "VERTEX_PROJECT", None)
    gb._cache.clear()
    gb._fetched.clear()
    gb._token.update(value="", exp=0.0, key="")
    await _wipe()
    yield
    await _wipe()
    gb._cache.clear()


async def _wipe():
    from sqlalchemy import delete

    from backend.database import AsyncSessionLocal
    from backend.models import AppSetting, UsageDay

    async with AsyncSessionLocal() as db:
        await db.execute(delete(UsageDay))
        await db.execute(delete(AppSetting).where(AppSetting.key == gb.SETTINGS_KEY))
        await db.commit()


async def _usage(day: str, model: str, **cols):
    from backend.database import AsyncSessionLocal
    from backend.models import UsageDay

    async with AsyncSessionLocal() as db:
        db.add(UsageDay(day=day, model=model, kind="chat", requests=1, **cols))
        await db.commit()


def _google(account_open=True, billing_enabled=True, bq_rows=None, calls=None, tables=True):
    """Поддельный Google: маршрутизация по адресу запроса."""
    async def fake(method, url, *, token="", quota="", json_body=None, data=None, timeout=30):
        if calls is not None:
            calls.append((method, url, quota))
        if url == gb.TOKEN_URL:
            assert data["refresh_token"] == "rt"
            return 200, {"access_token": "tok", "expires_in": 3600}
        assert token == "tok"
        if url.endswith(f"/projects/{PROJECT}/billingInfo"):
            return 200, {"billingAccountName": ACCOUNT, "billingEnabled": billing_enabled}
        if url.endswith(ACCOUNT):
            return 200, {"open": account_open, "displayName": "My Billing Account", "currencyCode": "USD"}
        if url.endswith(f"/projects/{PROJECT}/datasets?maxResults=200"):
            return 200, {"datasets": [{"datasetReference": {"datasetId": "billing_export"}}]}
        if "/datasets/billing_export/tables" in url:
            tid = "gcp_billing_export_v1_0000_1111_2222" if tables else "other"
            return 200, {"tables": [{"tableReference": {"tableId": tid}}]}
        if url.endswith(f"/projects/{PROJECT}/queries"):
            assert "gcp_billing_export_v1_0000_1111_2222" in json_body["query"]
            fields = ["day", "service", "currency", "cost", "credits", "promo", "rate", "exported"]
            return 200, {"jobComplete": True, "schema": {"fields": [{"name": f} for f in fields]},
                         "rows": [{"f": [{"v": v} for v in r]} for r in (bq_rows or [])]}
        raise AssertionError(f"неожиданный запрос {method} {url}")
    return fake


def _no_proxy_prices(price=None):
    async def fake(connection):
        return {"gem": {"price": price, "model": "vertex_ai/gemini-x"}} if price else {}
    return fake


PRICE = {"in": 1e-06, "out": 1e-05, "cache": 1e-07}


# ---------------------------------------------------------------- учёт токенов

async def test_usage_records_audio_and_long_prompts(db_ready):
    from sqlalchemy import select

    from backend import usage_stats
    from backend.database import AsyncSessionLocal
    from backend.models import UsageDay

    chunk = {"usage": {"prompt_tokens": 250_000, "completion_tokens": 900,
                       "prompt_tokens_details": {"cached_tokens": 50_000, "audio_tokens": 3000}}}
    usage = usage_stats.extract_usage(chunk)
    assert usage["audio"] == 3000
    await usage_stats._persist("chat", "litellm_proxy/gem", usage)
    await usage_stats._persist("chat", "litellm_proxy/gem", {"prompt": 1000, "cached": 0, "completion": 10})
    async with AsyncSessionLocal() as db:
        row = (await db.execute(select(UsageDay).where(UsageDay.model == "gem"))).scalar_one()
    assert row.prompt_tokens == 251_000 and row.audio_tokens == 3000
    # Только длинный запрос попал в long_* — короткий нет.
    assert (row.long_requests, row.long_prompt_tokens, row.long_cached_tokens,
            row.long_completion_tokens) == (1, 250_000, 50_000, 900)


def test_row_cost_uses_cache_audio_and_long_tier():
    p = {**PRICE, "audio": 3e-06, "in_long": 2e-06, "out_long": 2e-05, "cache_long": 2e-07}
    r = SimpleNamespace(prompt_tokens=1_250_000, cached_tokens=100_000, completion_tokens=2000,
                        audio_tokens=10_000, long_prompt_tokens=250_000, long_cached_tokens=50_000,
                        long_completion_tokens=1000)
    # Короткие: 1 000 000 входа (50 000 из кэша, 10 000 аудио) + 1000 ответа.
    short = (950_000 - 10_000) * 1e-06 + 10_000 * 3e-06 + 50_000 * 1e-07 + 1000 * 1e-05
    long = 200_000 * 2e-06 + 50_000 * 2e-07 + 1000 * 2e-05
    assert gb.row_cost(r, p) == pytest.approx(short + long)
    # Без цен длинного тарифа — по обычным (старые строки без long_* — тоже).
    old = SimpleNamespace(prompt_tokens=1000, cached_tokens=0, completion_tokens=100)
    assert gb.row_cost(old, PRICE) == pytest.approx(1000 * 1e-06 + 100 * 1e-05)


def test_resolve_price_prefers_manual_then_proxy_then_local():
    proxy = {"gem": {"price": dict(PRICE), "model": "vertex_ai/gemini-x"}}
    price, src = gb.resolve_price("gem", proxy, {})
    assert src == "proxy" and price["out"] == 1e-05
    price, src = gb.resolve_price("gem", proxy, {"gem": {"out": 20}})       # $ за 1 млн
    assert src == "manual" and price["out"] == pytest.approx(2e-05) and price["in"] == 1e-06
    # Прокси цен не знает — LiteLLM этого сервера по имени модели.
    price, src = gb.resolve_price("gemini-2.5-flash", {}, {})
    assert src == "local" and price["in"] > 0
    assert gb.resolve_price("совсем-неизвестная", {}, {}) == (None, "")
    # Префикс провайдера, которого нет в таблице, — по имени без него.
    assert gb.resolve_price("x", {"x": {"price": None, "model": "vertex_ai/gemini-2.5-pro"}}, {})[1] == "local"


def test_local_price_never_calls_get_model_info(monkeypatch):
    """get_model_info для ollama/… ходит в сеть и висит минутами — в потоке сервера нельзя."""
    import litellm

    def boom(*a, **kw):
        raise AssertionError("get_model_info вызывать нельзя")
    monkeypatch.setattr(litellm, "get_model_info", boom)
    assert gb._local_price("ollama/my-model:7b") is None
    assert gb._local_price("gemini-2.5-flash")["in"] > 0


def test_clean_settings_drops_garbage():
    cfg = gb.clean_settings({"credit_amount": "300", "credit_since": "вчера", "warn_below": -5,
                             "prices": {"gem": {"in": "1.5", "out": "x"}, "bad": "1"}, "evil": 1})
    assert cfg["credit_amount"] == 300.0 and cfg["credit_since"] == "" and cfg["warn_below"] == 0.0
    assert cfg["prices"] == {"gem": {"in": 1.5}} and "evil" not in cfg


# ---------------------------------------------------------------- вход Google

async def test_access_token_refreshes_user_login_and_caches(db_ready, monkeypatch):
    calls = []
    monkeypatch.setattr(gb, "_request", _google(calls=calls))
    token, info = await gb.access_token()
    assert token == "tok" and info["quota_project"] == PROJECT and info["type"] == "authorized_user"
    await gb.access_token()
    assert [c[1] for c in calls].count(gb.TOKEN_URL) == 1            # второй раз — из кэша


async def test_missing_login_is_explained(db_ready, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "GOOGLE_APPLICATION_CREDENTIALS", str(tmp_path / "нет.json"))
    monkeypatch.setattr(gb, "_gcloud_bin", lambda: "")
    with pytest.raises(gb.GoogleError, match="application-default login"):
        await gb.access_token()


def test_disabled_api_is_explained():
    body = {"error": {"code": 403, "status": "PERMISSION_DENIED",
                      "message": "Cloud Billing API has not been used in project 123 before or it is disabled.",
                      "details": [{"reason": "SERVICE_DISABLED"}]}}
    text = gb.explain_google(403, body, "cloudbilling.googleapis.com", PROJECT)
    assert "cloudbilling.googleapis.com" in text and PROJECT in text


# ---------------------------------------------------------------- сводка

async def test_snapshot_without_bigquery_estimates_and_counts_credit(db_ready, monkeypatch):
    from backend.database import AsyncSessionLocal

    today = date.today()
    await _usage(today.isoformat(), "gem", prompt_tokens=1_000_000, cached_tokens=0, completion_tokens=100_000)
    monkeypatch.setattr(gb, "_request", _google())
    monkeypatch.setattr(gb, "proxy_prices", _no_proxy_prices(PRICE))
    async with AsyncSessionLocal() as db:
        await gb.save_settings(db, {"credit_amount": 300, "credit_since": today.isoformat(),
                                    "credit_expires": (today + timedelta(days=30)).isoformat(),
                                    "bigquery": False})
        snap = await gb.snapshot(db, None)
    assert snap["configured"] and snap["project"] == PROJECT
    assert snap["status"]["account_open"] and snap["status"]["billing_enabled"]
    assert snap["bigquery"]["state"] == "off"
    spent = 1_000_000 * 1e-06 + 100_000 * 1e-05                        # = $2
    assert snap["today"]["spent"] == pytest.approx(spent)
    assert snap["credit"]["remaining"] == pytest.approx(300 - spent)
    assert snap["credit"]["days_left"] == 30 and not snap["credit"]["low"] and not snap["alerts"]
    assert snap["models"][0]["price"]["out"] == pytest.approx(10)         # $ за 1 млн


async def test_snapshot_merges_bigquery_and_alerts(db_ready, monkeypatch):
    from backend.database import AsyncSessionLocal

    today = date.today()
    d1, d2 = (today - timedelta(days=2)).isoformat(), (today - timedelta(days=1)).isoformat()
    # Оценка за d1 — $5, Google за d1 — $3 (полный день: Google точнее);
    # за d2 Google видел лишь начало дня ($0.5), оценка — $2: берём большее.
    await _usage(d1, "gem", prompt_tokens=5_000_000, cached_tokens=0, completion_tokens=0)
    await _usage(d2, "gem", prompt_tokens=2_000_000, cached_tokens=0, completion_tokens=0)
    rows = [
        [d1, "Vertex AI", "USD", "3.5", "-3.5", "-3.0", "1", "2026-10-24T05:00:00Z"],   # 0.5 — free tier
        [d2, "Vertex AI", "USD", "0.5", "-0.5", "-0.5", "1", "2026-10-24T06:00:00Z"],
    ]
    calls = []
    monkeypatch.setattr(gb, "_request", _google(account_open=False, bq_rows=rows, calls=calls))
    monkeypatch.setattr(gb, "proxy_prices", _no_proxy_prices(PRICE))
    async with AsyncSessionLocal() as db:
        await gb.save_settings(db, {"credit_amount": 10, "credit_since": d1, "warn_below": 6})
        snap = await gb.snapshot(db, None)
        days = {r["day"]: r for r in snap["by_day"]}
        assert days[d1]["spent"] == pytest.approx(3.0) and days[d1]["exact"] == pytest.approx(3.0)
        assert days[d2]["spent"] == pytest.approx(2.0)
        assert snap["credit"]["remaining"] == pytest.approx(5.0) and snap["credit"]["low"]
        assert snap["bigquery"]["state"] == "ok" and snap["bigquery"]["exported_at"].endswith("06:00:00Z")
        assert any("закрыт" in a for a in snap["alerts"]) and any("мало" in a for a in snap["alerts"])
        # Квота запросов — на проект; ответы Google кэшируются.
        assert all(q == PROJECT for m, u, q in calls if u != gb.TOKEN_URL)
        n = len(calls)
        await gb.snapshot(db, None)
        assert len(calls) == n
        await gb.snapshot(db, None, force=True)
        assert len(calls) > n


async def test_snapshot_reports_missing_export_table(db_ready, monkeypatch):
    from backend.database import AsyncSessionLocal

    monkeypatch.setattr(gb, "_request", _google(tables=False))
    monkeypatch.setattr(gb, "proxy_prices", _no_proxy_prices())
    async with AsyncSessionLocal() as db:
        snap = await gb.snapshot(db, None)
    assert snap["bigquery"]["state"] == "no_table" and snap["bigquery"]["datasets"] == ["billing_export"]
    assert snap["credit"] is None


async def test_snapshot_survives_google_errors(db_ready, monkeypatch):
    from backend.database import AsyncSessionLocal

    async def broken(method, url, **kw):
        if url == gb.TOKEN_URL:
            return 200, {"access_token": "tok", "expires_in": 3600}
        return 403, {"error": {"code": 403, "message": "Cloud Billing API has not been used in project 1 "
                               "before or it is disabled.", "status": "PERMISSION_DENIED"}}
    monkeypatch.setattr(gb, "_request", broken)
    monkeypatch.setattr(gb, "proxy_prices", _no_proxy_prices())
    async with AsyncSessionLocal() as db:
        snap = await gb.snapshot(db, None)
    assert not snap["status"]["ok"] and "cloudbilling.googleapis.com" in snap["status"]["error"]
    assert snap["bigquery"]["state"] == "error" and "bigquery.googleapis.com" in snap["bigquery"]["error"]


# ---------------------------------------------------------------- API

def test_billing_api_saves_settings(client, monkeypatch):
    async def fake_snapshot(db, connection, force=False):
        return {"settings": await gb.load_settings(db), "force": force}
    monkeypatch.setattr(gb, "snapshot", fake_snapshot)
    try:
        r = client.put("/api/billing/gcp/settings", json={"credit_amount": 250, "credit_since": "2026-10-01",
                                                          "prices": {"gem": {"in": 1.25, "out": None}}})
        assert r.status_code == 200, r.text
        assert r.json()["settings"]["credit_amount"] == 250 and r.json()["settings"]["prices"] == {"gem": {"in": 1.25}}
        assert client.get("/api/billing/gcp?force=true").json()["force"] is True
        assert client.put("/api/billing/gcp/settings", json={"credit_since": "1 окт"}).status_code == 422
    finally:
        client.put("/api/billing/gcp/settings", json={})


async def test_billing_api_is_admin_only():
    from fastapi import HTTPException

    from backend import main

    with pytest.raises(HTTPException) as exc:
        await main.gcp_billing_get(force=False, user=SimpleNamespace(role="user"), db=None)
    assert exc.value.status_code == 403
    with pytest.raises(HTTPException):
        await main.gcp_billing_settings(gb_settings(), user=SimpleNamespace(role="user"), db=None)


def gb_settings():
    from backend.schemas import GcpBillingSettings

    return GcpBillingSettings()
