"""
2.25.0: несколько аккаунтов Google на одном LiteLLM-прокси. У аккаунта —
префикс имён моделей; активный дописывает его ко всем запросам.
"""
from backend import llm_gateway, media_refs
from backend.settings_service import clean_accounts

CONN = {
    "use_proxy": True, "base_url": "http://proxy:4000", "api_key": "k", "default_model": "gemini-4-carbon",
    "accounts": [{"name": "Основной", "prefix": ""}, {"name": "Второй", "prefix": "acc2/"}],
    "active_account": 1,
}


def test_clean_accounts_normalizes():
    out = clean_accounts([{"name": " B ", "prefix": "acc2"}, {"name": "dup", "prefix": "acc2/"}, "мусор"])
    assert out == [{"name": "Основной", "prefix": ""}, {"name": "B", "prefix": "acc2/"}]
    assert clean_accounts(None) == [{"name": "Основной", "prefix": ""}]


def test_prefix_applied_to_proxy_requests():
    kw = llm_gateway._route_kwargs(CONN, "gemini-4-carbon")
    assert kw["model"] == "litellm_proxy/acc2/gemini-4-carbon"
    # Явно выбранная модель другого аккаунта не перекрашивается.
    assert llm_gateway._route_kwargs(CONN, "acc2/gemini-4-argon")["model"] == "litellm_proxy/acc2/gemini-4-argon"
    main = {**CONN, "active_account": 0}
    assert llm_gateway._route_kwargs(main, "gemini-4-carbon")["model"] == "litellm_proxy/gemini-4-carbon"
    # Без прокси префиксы не применяются.
    assert llm_gateway._route_kwargs({**CONN, "use_proxy": False}, "gemini-4-carbon")["model"] == "gemini-4-carbon"
    # Сломанный индекс — основной.
    assert llm_gateway.account_prefix({**CONN, "active_account": 9}) == ""


def test_media_route_follows_account_but_not_explicit_alias():
    assert media_refs.route_for(None, CONN).alias == "acc2/gemini-4-carbon"
    # Маршрут из записи проверки хранилища — имя уже полное.
    assert media_refs.route_for(None, CONN, model="gemini-4-carbon").alias == "gemini-4-carbon"


def test_connection_roundtrip_and_switch(client):
    saved = client.get("/api/settings/connection").json()
    try:
        r = client.put("/api/settings/connection", json={**saved, "accounts": [
            {"name": "Основной", "prefix": ""}, {"name": "Free Trial", "prefix": "trial"}]}).json()
        assert r["accounts"][1] == {"name": "Free Trial", "prefix": "trial/"} and r["active_account"] == 0
        r = client.post("/api/settings/account", json={"index": 1}).json()
        assert r["active_account"] == 1
        # PUT без полей аккаунтов их не стирает.
        body = {k: v for k, v in r.items() if k not in ("accounts", "active_account")}
        r = client.put("/api/settings/connection", json=body).json()
        assert r["active_account"] == 1 and len(r["accounts"]) == 2
        assert client.post("/api/settings/account", json={"index": 7}).json()["active_account"] == 0
    finally:
        client.put("/api/settings/connection", json={**saved, "accounts": [{"name": "Основной", "prefix": ""}],
                                                     "active_account": 0})


async def test_model_list_hides_patterns_and_other_accounts(monkeypatch):
    import httpx

    from backend import settings_service

    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"data": [{"id": "gemini-4-carbon"}, {"id": "acc2/*"}, {"id": "acc2/gemini-4-argon"},
                             {"id": "gemini-4-argon"}]}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, *a, **k):
            return FakeResp()

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    assert await settings_service.fetch_proxy_models(CONN) == ["gemini-4-carbon", "gemini-4-argon"]


def test_active_account_follows_prefix_when_list_is_cleaned(client):
    saved = client.get("/api/settings/connection").json()
    try:
        accs = [{"name": "Основной", "prefix": ""}, {"name": "A", "prefix": "acc2/"}, {"name": "B", "prefix": "acc3/"}]
        client.put("/api/settings/connection", json={**saved, "accounts": accs, "active_account": 2})
        # A потерял префикс (его стёрли, чтобы перепечатать) — B должен остаться активным.
        broken = [accs[0], {"name": "A", "prefix": ""}, accs[2], {"name": "C", "prefix": "acc4/"}]
        r = client.put("/api/settings/connection", json={**saved, "accounts": broken, "active_account": 2}).json()
        assert r["accounts"][r["active_account"]]["prefix"] == "acc3/"
        # Переключение по префиксу.
        r = client.post("/api/settings/account", json={"prefix": "acc4"}).json()
        assert r["accounts"][r["active_account"]]["prefix"] == "acc4/"
        # Список без активного: держимся прежнего префикса.
        body = {k: v for k, v in r.items() if k != "active_account"}
        body["accounts"] = [accs[0], {"name": "C", "prefix": "acc4/"}]
        r = client.put("/api/settings/connection", json=body).json()
        assert r["accounts"][r["active_account"]]["prefix"] == "acc4/"
    finally:
        client.put("/api/settings/connection", json={**saved, "accounts": [{"name": "Основной", "prefix": ""}],
                                                     "active_account": 0})


async def test_fallback_to_same_account_model_is_skipped(monkeypatch):
    from backend.generation import GenerationManager

    calls = []

    async def fake_fallback(self, job, messages, params, connection, fb, exc):
        calls.append(fb)

    monkeypatch.setattr(GenerationManager, "_run_fallback", fake_fallback)

    class Job:
        job_id = "j"
        stopping = False
        error = None

    gm = GenerationManager()

    async def boom(job, gen):
        return RuntimeError("429")

    monkeypatch.setattr(gm, "_drive", boom)
    monkeypatch.setattr(gm, "_stream", lambda *a, **k: None)
    conn = {**CONN, "fallback_model": "acc2/gemini-4-carbon", "auto_fallback": True}
    job = Job()
    await gm._generate(job, [], None, conn)
    assert calls == [] and job.error == "429"
    # С основного аккаунта запасная «acc2/…» — другой аккаунт: повтор идёт.
    await gm._generate(Job(), [], None, {**conn, "active_account": 0})
    assert calls == ["acc2/gemini-4-carbon"]
