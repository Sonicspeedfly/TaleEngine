"""
Учёт расхода токенов и «кэш-дружелюбная» сборка контекста.

Смысл обоих механизмов — экономия квоты:
  * usage_stats показывает, СКОЛЬКО и на что ушло (без цифр экономия — гадание);
  * стабильная граница обрезки истории даёт провайдеру попадание в кэш промпта
    (скидка 75–90% на совпадающее начало запроса).
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import patch

from backend import usage_stats


def _chunk(text: str = "", usage=None):
    """Чанк стрима: с содержимым и/или со служебным полем usage в конце."""
    c = SimpleNamespace(
        choices=[SimpleNamespace(
            delta=SimpleNamespace(content=text, reasoning_content=None),
            finish_reason=None,
        )] if text else [],
    )
    if usage is not None:
        c.usage = usage
    return c


# ---------- Разбор usage от разных провайдеров ----------
def test_extract_usage_openai_style():
    u = SimpleNamespace(
        prompt_tokens=1200, completion_tokens=300,
        prompt_tokens_details=SimpleNamespace(cached_tokens=1000),
        completion_tokens_details=SimpleNamespace(reasoning_tokens=120),
    )
    got = usage_stats.extract_usage(SimpleNamespace(usage=u))
    assert got == {"prompt": 1200, "cached": 1000, "completion": 300, "reasoning": 120}


def test_extract_usage_gemini_style_dict():
    """Vertex/Anthropic кладут кэш в cache_read_input_tokens; формат — словарь."""
    got = usage_stats.extract_usage({"usage": {
        "input_tokens": 900, "output_tokens": 50, "cache_read_input_tokens": 800,
    }})
    assert got == {"prompt": 900, "cached": 800, "completion": 50, "reasoning": 0}


def test_extract_usage_missing_is_empty_not_crash():
    assert usage_stats.extract_usage(SimpleNamespace()) == {}
    assert usage_stats.extract_usage(SimpleNamespace(usage=None)) == {}
    assert usage_stats.extract_usage({"usage": {"foo": 1}}) == {}


def test_cached_never_exceeds_prompt():
    """Кэш — ЧАСТЬ входа. Кривые данные провайдера не должны давать «скидку >100%»."""
    got = usage_stats.extract_usage({"usage": {
        "prompt_tokens": 100, "completion_tokens": 10, "cached_tokens": 999,
    }})
    assert got["cached"] == 100


# ---------- Учёт расхода в реальном стриме ----------
def test_stream_records_usage_with_kind():
    """Расход пишется с пометкой вида запроса — видно, что жрут ФОНОВЫЕ вызовы."""
    from backend.llm_gateway import stream_completion

    async def _acompletion(*a, **k):
        # Просим провайдера прислать usage — иначе расход не посчитать вообще.
        assert k.get("stream_options") == {"include_usage": True}

        async def gen():
            yield _chunk("При")
            yield _chunk("вет")
            yield _chunk(usage={"prompt_tokens": 5000, "completion_tokens": 2,
                                "cached_tokens": 4000})
        return gen()

    async def _no_persist(*a, **k):
        """Проверяем сам учёт, не запись в БД: иначе фоновая задача переживёт
        закрытие event loop теста и насорит предупреждениями."""

    before = len(usage_stats.recent())
    with patch("backend.llm_gateway.litellm.acompletion", new=_acompletion), \
            patch("backend.usage_stats._persist", new=_no_persist):
        async def _run():
            return "".join([t async for t in stream_completion(
                [{"role": "user", "content": "привет"}], kind="summary"
            )])
        assert asyncio.run(_run()) == "Привет"

    rec = usage_stats.recent()
    assert len(rec) == before + 1
    assert rec[0]["kind"] == "summary"
    assert rec[0]["prompt"] == 5000 and rec[0]["cached"] == 4000


def test_stream_options_rejection_disables_tracking_not_generation():
    """
    Прокси не принял stream_options → учёт токенов гаснет, генерация ЖИВЁТ.

    Иначе один несовместимый прокси ломал бы вообще все ответы ради статистики.
    """
    from backend import llm_gateway

    calls = []

    async def _acompletion(*a, **k):
        calls.append("stream_options" in k)
        if "stream_options" in k:
            raise TypeError("litellm.BadRequestError: Unrecognized request argument "
                            "supplied: stream_options")

        async def gen():
            yield _chunk("Живо")
        return gen()

    saved = llm_gateway._ask_usage
    try:
        llm_gateway._ask_usage = True
        with patch("backend.llm_gateway.litellm.acompletion", new=_acompletion):
            async def _run():
                return "".join([t async for t in llm_gateway.stream_completion(
                    [{"role": "user", "content": "?"}]
                )])
            assert asyncio.run(_run()) == "Живо"   # ответ дошёл до пользователя
        assert calls == [True, False]              # повтор уже без stream_options
        assert llm_gateway._ask_usage is False     # больше не просим (до перезапуска)
    finally:
        llm_gateway._ask_usage = saved


def test_unrelated_error_is_not_swallowed_as_param_problem():
    """Настоящая ошибка провайдера должна дойти наверх, а не «чиниться» повтором."""
    from backend import llm_gateway

    async def _acompletion(*a, **k):
        raise RuntimeError("429 RESOURCE_EXHAUSTED: quota exceeded")

    saved = llm_gateway._ask_usage
    try:
        llm_gateway._ask_usage = True
        with patch("backend.llm_gateway.litellm.acompletion", new=_acompletion):
            async def _run():
                return "".join([t async for t in llm_gateway.stream_completion(
                    [{"role": "user", "content": "?"}]
                )])
            try:
                asyncio.run(_run())
                raise AssertionError("ошибка провайдера должна пробрасываться")
            except RuntimeError as exc:
                assert "RESOURCE_EXHAUSTED" in str(exc)
        assert llm_gateway._ask_usage is True  # учёт не выключен зря
    finally:
        llm_gateway._ask_usage = saved


def test_stream_without_usage_does_not_break_generation():
    """Провайдер не прислал usage — генерация обязана работать как обычно."""
    from backend.llm_gateway import stream_completion

    async def _acompletion(*a, **k):
        async def gen():
            yield _chunk("Ок")
        return gen()

    with patch("backend.llm_gateway.litellm.acompletion", new=_acompletion):
        async def _run():
            return "".join([t async for t in stream_completion(
                [{"role": "user", "content": "?"}]
            )])
        assert asyncio.run(_run()) == "Ок"


# ---------- Стабильная граница обрезки истории (кэш промпта) ----------
def test_trim_start_keeps_all_when_it_fits():
    from backend.horae_memory import stable_trim_start

    assert stable_trim_start([10] * 5, budget=1000) == 0
    assert stable_trim_start([], budget=1000) == 0
    assert stable_trim_start([10] * 5, budget=0) == 0  # 0 = без бюджета


def test_trim_start_moves_once_per_step_not_every_turn():
    """
    Граница обрезки сдвигается РЕДКО — примерно раз в ступень ходов.

    Это и есть смысл квантования: без него начало запроса уезжало бы на каждом
    ходу, кэш промпта провайдера промахивался бы всегда, и весь вход (сотни тысяч
    токенов) тарифицировался бы по полной цене вместо ~25%.
    """
    from backend.horae_memory import _TRIM_STEP, stable_trim_start

    turns = 160
    boundaries = [
        stable_trim_start([100] * (300 + n), budget=5000) for n in range(turns)
    ]
    assert boundaries[0] > 0  # история не влезает — обрезка есть
    moves = sum(1 for a, b in zip(boundaries, boundaries[1:]) if a != b)
    # Без квантования сдвиг был бы на КАЖДОМ ходу (turns - 1 раз).
    assert moves <= turns // _TRIM_STEP + 1, f"граница дёргается слишком часто: {moves}"
    assert moves >= 1, "память должна освобождаться по мере роста чата"
    # Каждая ступень — ровно кратна _TRIM_STEP: индекс привязан к нумерации сообщений.
    assert all(b % _TRIM_STEP == 0 for b in boundaries)


def test_trim_start_never_empties_history():
    """Даже если бюджет крошечный — хвост истории остаётся, а не обнуляется."""
    from backend.horae_memory import stable_trim_start

    costs = [1000] * 5
    start = stable_trim_start(costs, budget=1500)
    assert start < len(costs)


def test_assemble_context_history_boundary_is_stable():
    """То же самое на уровне сборки контекста личного чата."""
    from backend.horae_memory import assemble_context

    history = [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"реплика {i} " + "текст " * 20}
        for i in range(400)
    ]
    char = {"name": "Тест", "description": "", "personality": "", "scenario": "", "system_prompt": ""}

    def _first_history_line(msgs):
        for m in msgs:
            if m["role"] in ("user", "assistant") and isinstance(m["content"], str):
                return m["content"]
        return ""

    base = assemble_context(character=char, horae_records=[], history=history,
                            user_message="ход", token_budget=8000)
    start_line = _first_history_line(base)
    assert "реплика 0 " not in start_line  # обрезка действительно произошла

    grown = list(history)
    for i in range(6):
        grown.append({"role": "user", "content": f"новая {i}"})
        got = assemble_context(character=char, horae_records=[], history=grown,
                               user_message="ход", token_budget=8000)
        assert _first_history_line(got) == start_line, f"граница уехала на ходу {i}"
