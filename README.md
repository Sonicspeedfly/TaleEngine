# TaleEngine

> Ранее проект назывался «AiChat SSF». Внутренние идентификаторы, путь рабочей
> папки и имя файла БД (`data/aichat.db`) пока сохраняют старое имя — это не влияет
> на работу; постепенно переименуем.

Самостоятельная, более стабильная и расширяемая альтернатива SillyTavern: «тонкий»
веб-клиент + мощный Python-бэкенд, который берёт на себя всю работу с нейросетями,
файлами, аудио и памятью. Один и тот же бэкенд обслуживает веб-интерфейс и
Telegram-бота (бот работает **в том же процессе**, отдельный запускать не нужно).

```
Браузер (Vue 3, без сборки)  ─┐
                              ├─►  FastAPI  ──►  LiteLLM-прокси :4000  ──►  Gemini / OpenAI / …
Telegram (aiogram, in-proc)  ─┘        │
                                       └──►  SQLite (общая БД: персонажи, чаты, память Horae)
```

**Стек:** FastAPI · WebSocket/SSE · LiteLLM · SQLAlchemy 2 (async) + SQLite · aiogram 3 ·
Vue 3 (CDN, без сборки) · Pytest. **Node.js не нужен** — статику раздаёт сам бэкенд.
Комментарии в коде — на русском, идентификаторы — английские.

## Возможности

- 🎭 **Персонажи** (карточки в духе SillyTavern), импорт/экспорт PNG/JSON V2.
- 💬 **Чаты**: свайпы, редактирование, регенерация, продолжение (⏩), ответы на сообщения.
- 👥 **Групповые чаты** с несколькими персонажами и ИИ-режиссёром (🎬).
- 🧠 **Память Horae** — гибрид World Info и снимков состояния ([docs/HORAE.md](docs/HORAE.md)).
- 🖼 **Арты** и мультимодальность: картинки, аудио (запись с микрофона), **документы Word/PDF**; предпросмотр вложений (миниатюры, аудиоплеер, лайтбокс).
- 📋 **Канвас** (как в Gemini): двухоконный режим (чат слева, документ/код справа),
  **контекстное редактирование выделенного фрагмента**, быстрые действия (сократить,
  тон, комментарии, ревью, перевод кода), история версий (undo), экспорт в **Docx/PDF**.
- 📥 **Импорт/экспорт чатов**: нативный формат (полная точность) + импорт из SillyTavern.
- 🤝 **Аккаунты, друзья, шаринг чатов**, уведомления (опциональный режим).
- 🔒 Три уровня доступа: код доступа · аккаунты · HTTP Basic Auth ([docs/SECURITY.md](docs/SECURITY.md)).
- 🤖 **Telegram-бот**: общение, файлы/альбомы одним ходом, умная разбивка длинных ответов.

## Быстрый старт

**Windows, локально:** дважды кликните [`start.bat`](start.bat) — он создаст `.venv`,
поставит зависимости, выберет свободный порт, дождётся `/api/health` и откроет браузер.

**Linux-сервер:** `./start.sh` (слушает `0.0.0.0:8000`; порт — через `PORT=...`).

**Docker:** `docker compose up --build`.

Настройки подключения к нейросети задаются прямо в интерфейсе (⚙ → «Подключение»),
`.env` необязателен. Подробности — в [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md).

## Документация

| Документ | О чём |
|----------|-------|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Архитектура, потоки данных, жизненный цикл генерации |
| [docs/BACKEND.md](docs/BACKEND.md) | Назначение каждого модуля `backend/` и ключевые функции |
| [docs/API.md](docs/API.md) | Справочник REST / WebSocket / SSE эндпоинтов |
| [docs/DATA_MODEL.md](docs/DATA_MODEL.md) | Таблицы БД (ORM-модели) и авто-миграции |
| [docs/FRONTEND.md](docs/FRONTEND.md) | Устройство `frontend/app.js`, состояние, рендеринг |
| [docs/IMPORT_EXPORT.md](docs/IMPORT_EXPORT.md) | Нативный формат чата, SillyTavern, карточки персонажей |
| [docs/CANVAS.md](docs/CANVAS.md) | Канвас: двухоконный режим, inline-правка, быстрые действия, undo |
| [docs/HORAE.md](docs/HORAE.md) | Подсистема памяти Horae |
| [docs/TELEGRAM.md](docs/TELEGRAM.md) | Telegram-бот: доступ, файлы, разбивка ответов |
| [docs/SECURITY.md](docs/SECURITY.md) | Режимы доступа и приватность данных |
| [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) | Установка, запуск, тесты, соглашения, как добавить фичу |

## Структура проекта

```
AiChat SSF/
├─ start.bat / start.sh           # запуск без Docker (Windows / Linux)
├─ reset_db.bat                   # очистка data/aichat.db
├─ Dockerfile / docker-compose.yml
├─ litellm.config.example.yaml    # пример конфига LiteLLM-прокси (Gemini/Vertex)
├─ backend/                       # вся серверная логика (общая для веба и бота)
│  ├─ main.py                     # FastAPI: REST + WebSocket + SSE + раздача UI + middleware
│  ├─ config.py                   # настройки из .env (pydantic-settings)
│  ├─ database.py                 # async SQLAlchemy + SQLite, init_db() + авто-миграции
│  ├─ models.py                   # ORM-модели (единственный источник правды о БД)
│  ├─ schemas.py                  # Pydantic DTO (GenerationParams, AttachmentIn, …)
│  ├─ accounts.py                 # аккаунты: пароли, токены, scope по владельцу
│  ├─ admin_service.py            # security/telegram настройки в app_settings (+ кэш)
│  ├─ settings_service.py         # подключение к LiteLLM + опрос /v1/models
│  ├─ llm_gateway.py              # шлюз к LiteLLM: роутинг, мультимодальность, safety
│  ├─ generation.py               # фоновые генерации: стоп, буфер при обрыве связи
│  ├─ horae_memory.py             # сборка контекста: память Horae + персона + Author's Note
│  ├─ group_chat.py               # логика групповых чатов (режиссёр, очередь реплик)
│  ├─ characters.py               # импорт/экспорт карточек SillyTavern (PNG/JSON, лорбук)
│  ├─ chat_import.py              # импорт чатов SillyTavern (.jsonl, встроенные теги Horae)
│  ├─ native_io.py                # нативный формат экспорта/импорта чатов AiChat
│  ├─ document_service.py         # Word/PDF/текст → контент для нейросети
│  ├─ telegram_runtime.py         # Telegram-бот внутри процесса (aiogram polling)
│  ├─ telegram_format.py          # разбивка длинных ответов + Markdown → Telegram-HTML
│  ├─ debug_log.py                # кольцевой буфер запросов к LLM (для 🐞 в UI)
│  └─ requirements.txt
├─ frontend/                      # тонкий клиент БЕЗ сборки (раздаётся бэкендом)
│  ├─ index.html                  # подключает Vue / markdown-it / DOMPurify с CDN
│  ├─ app.js                      # всё приложение (состояние + методы + шаблон-строка)
│  └─ styles.css                  # тёмная тема
├─ tests/                         # Pytest с моками (без реальных вызовов к API)
└─ data/                          # файл SQLite (создаётся при первом запуске)
```

## Тесты

```
.venv\Scripts\python.exe -m pytest -q
```

Все вызовы к LLM замоканы — проверяются роутинг, сборка промптов с памятью Horae,
импорт/экспорт, документы, форматирование для Telegram, аккаунты и сквозной путь по WebSocket.
