"""
Классификация НАСТОЯЩИХ исключений litellm ядром памяти (§4.4).

Ядро (backend/hierarchical_memory.py) litellm не импортирует: вид ошибки оно
узнаёт по имени класса в MRO, по status_code и заголовку Retry-After. Поэтому
таблица §4.4 держится на именах чужих классов, и переименование или смена
иерархии при обновлении litellm сломали бы её молча: auth-ошибка стала бы
повторяться, а обрыв связи — выглядеть сбоем сервера. Этот тест ловит такое
здесь, а не в статусе задания у пользователя. Импорт litellm — только в тесте.
"""
import ast
import sys
from pathlib import Path

import httpx
import litellm
import pytest

from backend import hierarchical_memory as hm

# Минимальные аргументы, которые принимает конструктор любого из классов ниже.
_ARGS = {"message": "boom", "llm_provider": "openai", "model": "gpt-test"}


@pytest.mark.parametrize("name, kind, retryable", [
    ("RateLimitError", "rate_limit", True),
    ("Timeout", "timeout", True),
    ("APIConnectionError", "network", True),
    ("InternalServerError", "server", True),
    ("ServiceUnavailableError", "server", True),
    ("AuthenticationError", "auth", False),
    ("BadRequestError", "bad_request", False),
    ("ContextWindowExceededError", "bad_request", False),
    ("ContentPolicyViolationError", "blocked", False),
])
def test_real_litellm_exceptions_follow_the_table(name, kind, retryable):
    err = hm.classify_error(getattr(litellm, name)(**_ARGS))
    assert (err.kind, err.retryable) == (kind, retryable)
    assert "boom" in err.message  # хвост исходной ошибки — для поиска в логах прокси


def _rate_limit_with_retry_after(seconds):
    request = httpx.Request("POST", "https://api.example.test/v1/chat/completions")
    response = httpx.Response(429, headers={"Retry-After": str(seconds)}, request=request)
    return litellm.RateLimitError(response=response, **_ARGS)


def test_rate_limit_retry_after_header_is_read():
    err = hm.classify_error(_rate_limit_with_retry_after(17))
    assert (err.kind, err.retryable, err.retry_after) == ("rate_limit", True, 17.0)


async def test_manager_waits_the_retry_after_of_a_real_rate_limit():
    attempts, sleeps = [], []

    async def llm(messages):
        attempts.append(1)
        if len(attempts) == 1:
            raise _rate_limit_with_retry_after(17)
        return "ok"

    async def sleep(seconds):
        sleeps.append(seconds)

    m = hm.HierarchicalMemoryManager(llm, hm.MemoryConfig(delay_ms=0), sleep=sleep)
    assert await m.call([{"role": "user", "content": "x"}]) == "ok"
    assert sleeps == [17.0]


def test_core_imports_only_stdlib():
    # Ядро проверяется без сети и без БД; litellm — забота сервиса (§6.1).
    tree = ast.parse(Path(hm.__file__).read_text(encoding="utf-8"))
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "относительный импорт тянет пакет backend"
            roots.add(node.module.split(".")[0])
    assert roots <= set(sys.stdlib_module_names), roots - set(sys.stdlib_module_names)
