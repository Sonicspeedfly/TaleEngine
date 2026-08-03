"""
Обход цензуры: пороги фильтров, диагностика блокировок, глобальные инструкции.

Главное, что здесь проверяется — приложение РАЗЛИЧАЕТ два вида блокировок:
настраиваемые фильтры (снимаются порогами) и неотключаемые фильтры Google
(порогами НЕ снимаются). Раньше на всё выдавалась одна общая отписка, и
пользователь крутил настройки, которые к его случаю отношения не имели.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import patch

from backend import censorship


# ---------- Пороги ----------
def test_default_preset_turns_every_category_off():
    got = censorship.safety_settings("off")
    assert len(got) == len(censorship.SAFETY_CATEGORIES)
    assert {s["threshold"] for s in got} == {"OFF"}
    # Гражданская добропорядочность тоже снимается — её часто забывают.
    assert any(s["category"] == "HARM_CATEGORY_CIVIC_INTEGRITY" for s in got)


def test_provider_preset_sends_nothing():
    """«Решает провайдер» = не передавать safety_settings вовсе."""
    assert censorship.safety_settings("provider") == []


def test_overrides_win_over_preset():
    """Душит одна категория — её порог можно поднять, не трогая остальные."""
    got = censorship.safety_settings(
        "off", {"HARM_CATEGORY_DANGEROUS_CONTENT": "BLOCK_ONLY_HIGH"}
    )
    by_cat = {s["category"]: s["threshold"] for s in got}
    assert by_cat["HARM_CATEGORY_DANGEROUS_CONTENT"] == "BLOCK_ONLY_HIGH"
    assert by_cat["HARM_CATEGORY_HATE_SPEECH"] == "OFF"


def test_override_can_drop_single_category():
    """Пустой порог у категории = не передавать именно её."""
    got = censorship.safety_settings("off", {"HARM_CATEGORY_HATE_SPEECH": ""})
    assert all(s["category"] != "HARM_CATEGORY_HATE_SPEECH" for s in got)
    assert len(got) == len(censorship.SAFETY_CATEGORIES) - 1


def test_unknown_preset_falls_back_to_off():
    """Мусор в настройке не должен молча возвращать фильтры провайдера."""
    assert {s["threshold"] for s in censorship.safety_settings("абракадабра")} == {"OFF"}


# ---------- Диагностика блокировок ----------
def test_non_configurable_block_says_settings_wont_help():
    """
    PROHIBITED_CONTENT порогами НЕ снимается. Пользователь должен это узнать,
    а не крутить Zero-Censorship впустую.
    """
    msg = censorship.explain_block("PROHIBITED_CONTENT", safety_off=True)
    assert "НЕотключаемый" in msg
    assert "НЕ помогут" in msg
    assert "PROHIBITED_CONTENT" in msg  # техническая строка на месте


def test_recitation_and_spii_have_own_advice():
    """У каждого неотключаемого фильтра свой осмысленный совет, не общий."""
    rec = censorship.explain_block("RECITATION")
    spii = censorship.explain_block("SPII")
    assert "авторским правом" in rec and "своими словами" in rec
    assert "персональные данные" in spii
    assert rec != spii


def test_configurable_block_suggests_turning_filters_off():
    """Фильтры включены и сработали — предлагаем то, что реально поможет."""
    msg = censorship.explain_block("SAFETY", safety_off=False)
    assert "НАСТРАИВАЕМЫЙ" in msg
    assert "Zero-Censorship" in msg


def test_configurable_block_when_already_off_points_elsewhere():
    """Фильтры уже сняты — не советуем «снять фильтры» ещё раз."""
    msg = censorship.explain_block("SAFETY", safety_off=True)
    assert "уже сняты" in msg


def test_length_block_blames_reasoning_budget():
    msg = censorship.explain_block("MAX_TOKENS", thought_len=9000, max_tokens=1024)
    assert "Лимит длины" in msg
    assert "Размышления съели" in msg


def test_reasoning_conflict_is_warned_about():
    """
    Ловушка: включённые вручную размышления возвращают цензуру Gemini даже при
    пороге OFF. Сам пользователь этой связи не увидит — предупреждаем явно.
    """
    assert censorship.reasoning_conflict("high", safety_off=True)
    assert not censorship.reasoning_conflict("disable", safety_off=True)
    assert not censorship.reasoning_conflict("", safety_off=True)
    assert not censorship.reasoning_conflict("high", safety_off=False)

    msg = censorship.explain_block("SAFETY", safety_off=True, reasoning="high")
    assert "размышления" in msg and "собственную модерацию" in msg


# ---------- Глобальные инструкции ----------
def test_jailbreak_block_respects_switch():
    assert censorship.jailbreak_block({"enabled": False, "text": "мой текст"}) == ""
    assert censorship.jailbreak_block({"enabled": True, "text": "  мой текст  "}) == "мой текст"
    assert censorship.jailbreak_block(None) == ""
    assert censorship.jailbreak_block({"enabled": True}) == ""


def test_global_instructions_go_to_the_very_end_of_context():
    """
    Позиция решает: глобальные инструкции идут в САМЫЙ конец — после Post-History
    персонажа, но ПЕРЕД текущей репликой пользователя (сильнейшая позиция).
    """
    from backend.horae_memory import assemble_context

    msgs = assemble_context(
        character={"name": "Тест", "description": "", "personality": "",
                   "scenario": "", "system_prompt": ""},
        horae_records=[], history=[], user_message="ход",
        post_history_instructions="ПРАВИЛО ПЕРСОНАЖА",
        global_instructions="ОБЩЕЕ ПРАВИЛО",
    )
    texts = [m["content"] for m in msgs if isinstance(m.get("content"), str)]
    assert "ПРАВИЛО ПЕРСОНАЖА" in texts and "ОБЩЕЕ ПРАВИЛО" in texts
    assert texts.index("ОБЩЕЕ ПРАВИЛО") > texts.index("ПРАВИЛО ПЕРСОНАЖА")
    assert msgs[-1]["content"] == "ход"  # реплика пользователя всё равно последняя


def test_global_instructions_load_is_crash_proof():
    """Сломанная настройка не должна ронять ход — вернётся пустая строка."""

    class _Boom:
        async def get(self, *a, **k):
            raise RuntimeError("БД недоступна")

    assert asyncio.run(censorship.load_global_instructions(_Boom())) == ""


# ---------- Проброс порогов в реальный запрос ----------
def test_safety_overrides_reach_the_provider_request():
    from backend.llm_gateway import stream_completion
    from backend.schemas import GenerationParams

    captured = {}

    async def _acompletion(*a, **k):
        captured.update(k)

        async def gen():
            yield SimpleNamespace(choices=[SimpleNamespace(
                delta=SimpleNamespace(content="Ок", reasoning_content=None),
                finish_reason=None,
            )])
        return gen()

    params = GenerationParams(
        disable_safety=True, safety_preset="off",
        safety_overrides={"HARM_CATEGORY_HATE_SPEECH": "BLOCK_ONLY_HIGH"},
    )
    with patch("backend.llm_gateway.litellm.acompletion", new=_acompletion):
        async def _run():
            return "".join([t async for t in stream_completion(
                [{"role": "user", "content": "?"}], params
            )])
        assert asyncio.run(_run()) == "Ок"

    by_cat = {s["category"]: s["threshold"] for s in captured["safety_settings"]}
    assert by_cat["HARM_CATEGORY_HATE_SPEECH"] == "BLOCK_ONLY_HIGH"
    assert by_cat["HARM_CATEGORY_SEXUALLY_EXPLICIT"] == "OFF"
    # Настройки обхода — НЕ сэмплинг-параметры, провайдеру их слать нельзя.
    assert "safety_preset" not in captured and "safety_overrides" not in captured


def _stub_stream():
    """Заглушка LLM, запоминающая ОТПРАВЛЕННЫЕ сообщения."""

    async def _acompletion(*a, **k):
        _acompletion.captured = k

        async def gen():
            yield SimpleNamespace(choices=[SimpleNamespace(
                delta=SimpleNamespace(content="Ок", reasoning_content=None),
                finish_reason=None,
            )])
        return gen()

    return _acompletion


def _wait_done(client, job_id: str) -> None:
    with client.stream("GET", f"/sse/job/{job_id}") as resp:
        for line in resp.iter_lines():
            if line and ('"done"' in line or '"error"' in line):
                break


def test_global_instructions_reach_the_model_in_a_real_turn(client):
    """
    СКВОЗНАЯ проверка: сохранили в настройках -> дошло до провайдера.

    Именно здесь ломались похожие вещи раньше (окно контекста принималось, но не
    применялось в группах). Загрузка идёт внутри сборки контекста, чтобы ни один
    режим — веб, группа, Telegram, retry — не остался без инструкций.
    """
    client.put("/api/settings/ui", json={
        "jailbreak": {"enabled": True, "text": "ГЛОБАЛЬНОЕ ПРАВИЛО ОБХОДА", "presets": []},
    })
    cid = client.post("/api/characters", json={
        "name": "Цензор", "post_history_instructions": "ПРАВИЛО ПЕРСОНАЖА",
    }).json()["id"]
    sid = client.post(f"/api/sessions?character_id={cid}").json()["session_id"]

    stub = _stub_stream()
    with patch("backend.llm_gateway.litellm.acompletion", new=stub):
        r = client.post(f"/api/sessions/{sid}/send", json={"content": "привет"})
        _wait_done(client, r.json()["job_id"])

    texts = [m["content"] for m in stub.captured["messages"] if isinstance(m.get("content"), str)]
    assert "ГЛОБАЛЬНОЕ ПРАВИЛО ОБХОДА" in texts
    # Порядок: общее правило идёт ПОСЛЕ правила персонажа (общее важнее частного).
    assert texts.index("ГЛОБАЛЬНОЕ ПРАВИЛО ОБХОДА") > texts.index("ПРАВИЛО ПЕРСОНАЖА")

    # Выключатель реально выключает.
    client.put("/api/settings/ui", json={
        "jailbreak": {"enabled": False, "text": "ГЛОБАЛЬНОЕ ПРАВИЛО ОБХОДА", "presets": []},
    })
    with patch("backend.llm_gateway.litellm.acompletion", new=stub):
        r = client.post(f"/api/sessions/{sid}/send", json={"content": "ещё"})
        _wait_done(client, r.json()["job_id"])
    texts = [m["content"] for m in stub.captured["messages"] if isinstance(m.get("content"), str)]
    assert "ГЛОБАЛЬНОЕ ПРАВИЛО ОБХОДА" not in texts


def test_global_instructions_reach_group_chats_too(client):
    """Группы — отдельная ветка сборки контекста, её легко забыть (и забывали)."""
    client.put("/api/settings/ui", json={
        "jailbreak": {"enabled": True, "text": "ОБЩЕЕ ДЛЯ ГРУППЫ", "presets": []},
    })
    ids = [client.post("/api/characters", json={"name": n}).json()["id"]
           for n in ("Альфа", "Бета")]
    gid = client.post("/api/groups", json={
        "name": "Тестовая группа", "character_ids": ids,
    }).json()["session_id"]

    stub = _stub_stream()
    with patch("backend.llm_gateway.litellm.acompletion", new=stub):
        r = client.post(f"/api/sessions/{gid}/send", json={"content": "Альфа, привет"})
        _wait_done(client, r.json()["job_id"])

    texts = [m["content"] for m in stub.captured["messages"] if isinstance(m.get("content"), str)]
    assert "ОБЩЕЕ ДЛЯ ГРУППЫ" in texts
    client.put("/api/settings/ui", json={})  # не влияем на другие тесты


def test_filters_stay_on_when_user_asks_for_them():
    """Снятая галка Zero-Censorship = safety_settings не отправляются вовсе."""
    from backend.llm_gateway import stream_completion
    from backend.schemas import GenerationParams

    captured = {}

    async def _acompletion(*a, **k):
        captured.update(k)

        async def gen():
            yield SimpleNamespace(choices=[SimpleNamespace(
                delta=SimpleNamespace(content="Ок", reasoning_content=None),
                finish_reason=None,
            )])
        return gen()

    with patch("backend.llm_gateway.litellm.acompletion", new=_acompletion):
        async def _run():
            return "".join([t async for t in stream_completion(
                [{"role": "user", "content": "?"}], GenerationParams(disable_safety=False)
            )])
        asyncio.run(_run())
    assert "safety_settings" not in captured
