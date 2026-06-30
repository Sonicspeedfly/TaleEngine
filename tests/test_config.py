"""
Регрессионный тест к реальному багу: из-за пустого значения в .env
(TELEGRAM_DEFAULT_CHARACTER_ID=) pydantic не мог распарсить '' как int, и сервер
вообще не запускался (ERR_CONNECTION_REFUSED в браузере).

Проверяем, что пустые опциональные поля трактуются как «не задано» (None).
"""
from backend.config import Settings


def test_blank_optional_int_becomes_none():
    s = Settings(TELEGRAM_DEFAULT_CHARACTER_ID="")
    assert s.TELEGRAM_DEFAULT_CHARACTER_ID is None


def test_valid_optional_int_is_parsed():
    s = Settings(TELEGRAM_DEFAULT_CHARACTER_ID="7")
    assert s.TELEGRAM_DEFAULT_CHARACTER_ID == 7


def test_blank_vertex_project_becomes_none():
    s = Settings(VERTEX_PROJECT="")
    assert s.VERTEX_PROJECT is None
