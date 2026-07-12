"""
Центральная конфигурация приложения.

ВСЕ настройки читаются из переменных окружения / файла .env, чтобы не хардкодить
ключи и параметры прямо в коде. Используем pydantic-settings — он валидирует типы
и подставляет значения по умолчанию.

Как пользоваться: `from backend.config import settings` и далее `settings.DEFAULT_MODEL`.
"""
from functools import lru_cache
from typing import List, Optional

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Откуда читать настройки: файл .env в корне проекта.
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",  # лишние переменные в .env не роняют запуск
    )

    # ----- Общие настройки сервера -----
    APP_NAME: str = "AiChat SSF"
    HOST: str = "0.0.0.0"
    PORT: int = 8000
    DEBUG: bool = False

    # CORS: какие origin'ы фронтенда пускать. В .env пишем через запятую,
    # а удобный список достаём через свойство cors_origins_list ниже.
    CORS_ORIGINS: str = "http://localhost:5173,http://localhost:3000"

    # ----- База данных (ОБЩАЯ для backend и Telegram-бота) -----
    # SQLite ради простоты развёртывания. При росте нагрузки меняем строку на
    # postgresql+asyncpg://... — модели менять не придётся.
    DATABASE_URL: str = "sqlite+aiosqlite:///./data/aichat.db"

    # ----- LLM / LiteLLM -----
    DEFAULT_MODEL: str = "gpt-4o"
    REQUEST_TIMEOUT: int = 120  # секунды на запрос к провайдеру
    # Таймаут для БОЛЬШИХ мультимодальных запросов (видео/аудио inline):
    # загрузка в прокси/Vertex и обработка медиа до первого токена занимают
    # заметно дольше обычного текстового запроса. Реальный таймаут ещё и растёт
    # с размером файла (~10 с на МБ, см. llm_gateway._request_timeout).
    LARGE_REQUEST_TIMEOUT: int = 900

    # ----- LiteLLM Proxy -----
    # Если у вас уже запущен LiteLLM-прокси (litellm --port 4000), все запросы
    # идут туда, а провайдеров/ключи настраивает сам прокси. Эти значения —
    # ДЕФОЛТЫ; их можно переопределить прямо в интерфейсе (вкладка «Подключение»),
    # и они сохранятся в БД (таблица app_settings). Обработка всегда на сервере.
    LITELLM_USE_PROXY: bool = True
    LITELLM_BASE_URL: str = "http://localhost:4000"  # в Docker: http://host.docker.internal:4000
    LITELLM_API_KEY: Optional[str] = None  # master key прокси (sk-...), если включён
    # Модель генерации картинок (артов) в вашем прокси, например imagen/dall-e.
    LITELLM_IMAGE_MODEL: Optional[str] = None

    # Бюджет контекста (в приблизительных токенах) — сколько ИСТОРИИ диалога
    # видит модель на каждый ход. По умолчанию — максимум Gemini (~1 млн):
    # память важнее экономии (пожелание владельца). Уменьшается в UI
    # («Окно контекста» в Генерации, поле context_tokens), если станет
    # дорого/медленно: у Gemini контекст свыше ~200 тыс. тарифицируется дороже.
    CONTEXT_TOKEN_BUDGET: int = 1_000_000
    # Лимит ФАЙЛОВ истории (МБ на один ход): вложения прежних реплик пересылаются
    # модели заново, чтобы она их «видела». 0 — БЕЗ лимита (полная память по файлам,
    # пожелание владельца); N — от свежих к старым до N МБ, остальное — пометкой.
    # Переопределяется в UI (Генерация -> «Файлы в памяти», поле history_files_mb).
    HISTORY_FILES_MB: int = 0

    # Дефолтные параметры генерации. Их можно переопределить на уровне
    # персонажа или прямо из UI (см. backend/schemas.py -> GenerationParams).
    DEFAULT_TEMPERATURE: float = 0.9
    DEFAULT_TOP_P: float = 0.95
    DEFAULT_TOP_K: int = 40
    # max_tokens — лимит ДЛИНЫ ОДНОГО ОТВЕТА (вывода), не «памяти» и не входа.
    # У «думающих» моделей рассуждения тратят этот же лимит, поэтому дефолт щедрый.
    DEFAULT_MAX_TOKENS: int = 8192
    DEFAULT_REPETITION_PENALTY: float = 1.1

    # ----- Ключи провайдеров -----
    # LiteLLM сам читает большинство ключей из окружения (OPENAI_API_KEY и т.д.),
    # но мы дублируем их сюда для явности и единой точки конфигурации.
    OPENAI_API_KEY: Optional[str] = None
    ANTHROPIC_API_KEY: Optional[str] = None
    GEMINI_API_KEY: Optional[str] = None
    VERTEX_PROJECT: Optional[str] = None
    VERTEX_LOCATION: Optional[str] = "us-central1"

    # ----- Telegram -----
    TELEGRAM_BOT_TOKEN: Optional[str] = None
    # С каким персонажем начинать чат в Telegram, если у пользователя ещё нет сессии.
    TELEGRAM_DEFAULT_CHARACTER_ID: Optional[int] = None

    @field_validator(
        "TELEGRAM_DEFAULT_CHARACTER_ID", "VERTEX_PROJECT", mode="before"
    )
    @classmethod
    def _blank_to_none(cls, v):
        """
        В .env числовые/опциональные поля часто оставляют пустыми
        (например `TELEGRAM_DEFAULT_CHARACTER_ID=`). Пустую строку трактуем как
        «не задано» (None), иначе pydantic не смог бы распарсить '' как int и
        сервер не запустился бы.
        """
        if isinstance(v, str) and v.strip() == "":
            return None
        return v

    @property
    def cors_origins_list(self) -> List[str]:
        """Превращает строку 'a,b,c' в список ['a','b','c'] для middleware CORS."""
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    """Кэшируем настройки, чтобы не перечитывать .env на каждый запрос."""
    return Settings()


# Готовый к импорту singleton.
settings = get_settings()
