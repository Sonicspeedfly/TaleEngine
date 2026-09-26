"""
Общая настройка тестов.

Главное: ДО импорта любого кода backend подменяем DATABASE_URL на отдельный
временный файл, чтобы интеграционные тесты не трогали рабочую базу data/aichat.db.
conftest.py исполняется pytest раньше тест-модулей, поэтому настройка применится
к кэшируемым настройкам приложения.
"""
import os
import pathlib
import tempfile

_test_db = pathlib.Path(tempfile.gettempdir()) / "aichat_test.db"
# Свежая БД на каждый прогон тестов.
if _test_db.exists():
    try:
        _test_db.unlink()
    except OSError:
        pass

os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///" + _test_db.as_posix()

# Тесты не ждут паузу между запросами памяти; тесты паузы задают её явно
# (и подменяют sleep/clock) — иначе весь набор незаметно тормозился бы на
# 1500 мс за каждый пакет сжатия по дефолту из .env.
os.environ.setdefault("MEMORY_DELAY_MS", "0")
# И не ждут бэкофф повторов: сервис памяти повторяет сбой модели (в том числе
# извлечения фактов) до MEMORY_MAX_RETRIES раз с паузами 2+4+8+16 с, и тест,
# чья подменная модель падает нарочно, шёл бы полминуты. Повторы ядра
# проверяются в tests/test_hierarchical_memory.py с явным MemoryConfig.
os.environ.setdefault("MEMORY_MAX_RETRIES", "0")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture
def client():
    """FastAPI TestClient с прогоном lifespan (init_db + загрузка кэшей доступа)."""
    from backend.main import app

    with TestClient(app) as c:
        yield c
