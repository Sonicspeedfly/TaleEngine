# Бэкенд: модули

Все модули — в `backend/`. Это общий пакет: им пользуются и веб-сервер, и
Telegram-бот. Ниже — назначение каждого файла и его ключевые функции/классы.

## Ядро и инфраструктура

### `main.py`
FastAPI-приложение. Содержит:
- **Middleware** (порядок добавления → внешний слой срабатывает первым):
  `BasicAuthMiddleware` (внешний барьер) → `CORSMiddleware` → `AccessMiddleware`
  (код доступа/токен/роль) → `NoCacheStaticMiddleware` (запрет кэша на статику).
- **REST-эндпоинты** для персонажей, чатов, сообщений, памяти, персон, пресетов,
  настроек, аккаунтов, друзей, шаринга, админки. Полный список — [API.md](API.md).
- **WebSocket** `/ws/chat/{session_id}` — приём сообщений и стриминг ответа.
- **SSE** `/sse/job/{job_id}` — дослушивание оборванной генерации.
- Зависимость `current_user` и helper'ы доступа: `_can_access_session`,
  `_can_access_horae`, `_are_friends`, `_existing_friendship`.
- Раздача `frontend/` через StaticFiles (mount на `/`).

### `config.py`
`Settings` (pydantic-settings) — читает `.env`. Главное: `DATABASE_URL`,
`LITELLM_USE_PROXY/BASE_URL/API_KEY`, `DEFAULT_MODEL`, дефолтные `DEFAULT_*`-параметры
генерации, `CONTEXT_TOKEN_BUDGET`, ключи провайдеров, `TELEGRAM_*`. Валидатор
`_blank_to_none` превращает пустые строки в `None` (иначе пустой
`TELEGRAM_DEFAULT_CHARACTER_ID=` ронял запуск). Импорт: `from backend.config import settings`.

### `database.py`
Async-движок SQLAlchemy + `AsyncSessionLocal` + зависимость `get_session()`.
`init_db()` создаёт таблицы (`create_all`) и выполняет **лёгкие авто-миграции**
(`_sqlite_add_missing_columns`) — добавляет недостающие колонки в существующую БД без
Alembic. Новые таблицы появляются сами через `create_all` при старте.

### `models.py`
ORM-модели — **единственный источник правды о структуре БД**. См. [DATA_MODEL.md](DATA_MODEL.md).

### `schemas.py`
Pydantic-DTO для валидации запросов/ответов: `GenerationParams` (параметры семплинга),
`AttachmentIn` (вложение: image/audio/document + name), `WSUserMessage`,
`HoraeEntry*`, `CharacterBase/Read/Update` и т.д.

## LLM и генерация

### `llm_gateway.py`
Шлюз к LiteLLM.
- `build_user_content(text, attachments)` — собирает мультимодальный контент
  (текст + блоки картинок/аудио/документов); `_content_from_attachment` маршрутизирует
  по типу, документы отдаёт в `document_service.prepare_document`.
- `_route_kwargs(connection, model)` — выбирает маршрут: через прокси
  (`litellm_proxy/<model>` + `api_base`) или напрямую. Всегда подставляет
  `DUMMY_PROXY_KEY`, если ключ пуст.
- `_merge_params(params)` — слияние дефолтов из `.env` с параметрами из UI.
- `stream_completion(...)` — асинхронный генератор токенов; `generate_image(...)` — арты.
- `SAFETY_SETTINGS` — отключение фильтров (Zero-Censorship) для совместимых провайдеров.

### `generation.py`
`generation_manager` — реестр фоновых задач генерации. `start_runner()` запускает
asyncio-задачу, которая стримит токены подписчикам (WebSocket) **и** копит их в буфер,
чтобы клиент мог дослушать по SSE при обрыве. Поддерживает стоп (отмена с сохранением
частичного результата) и переживает отключение клиента.

### `horae_memory.py`
Сборка контекста для модели.
- `assemble_context(...)` — **чистая** функция (без БД/сети, легко тестируется):
  системный промпт = паспорт персонажа + персона + сработавшие записи Horae +
  `STYLE_GUIDE` (подсказка по портативной разметке); затем история под бюджет токенов
  и текущее сообщение (текст или мультимодальный контент).
- `build_context_from_db(...)` — обёртка, читающая данные из БД и зовущая
  `assemble_context`. Подробности про память — в [HORAE.md](HORAE.md).

### `group_chat.py`
Логика групповых чатов: определяет, кто из персонажей отвечает (по упоминанию имени
или через ИИ-режиссёра), и формирует очередь реплик. Используется раннером генерации
для мультиспикерного стриминга.

## Импорт / экспорт

### `characters.py`
Импорт карточек SillyTavern (PNG с tEXt-чанком или JSON V1/V2) и экспорт в карточку
V2. `extract_horae_entries` достаёт лорбук (`character_book`) → записи Horae;
`build_character_book` — обратно.

### `chat_import.py`
Импорт чатов SillyTavern (.jsonl). `parse_sillytavern_chat` разбирает реплики, выводит
имя персонажа из реплик при заглушке, **вырезает встроенные теги Horae**
(`<horae>`/`<horaeevent>`) из текста и собирает из них снимок состояния + хронологию
событий. См. [IMPORT_EXPORT.md](IMPORT_EXPORT.md).

### `native_io.py`
**Нативный формат AiChat** (`"format":"aichat.chat"`). `build_chat_export(...)` —
самодостаточный экспорт чата (персонаж, персона, сцена, сообщения со свайпами/автором/
ответами-на-сообщение, память Horae). `is_native_chat(data)` — определение формата при
импорте. Создание строк в БД делает `_import_native_chat` в `main.py`.

### `document_service.py`
Подготовка документов к отправке в нейросеть. `prepare_document(data, mime, name)`:
PDF → отдаём как есть; Word/DOC/ODT/RTF → конвертация в PDF через LibreOffice (если
установлен), иначе извлечение текста (`python-docx`); TXT/MD/CSV → текст.
`is_document(mime, name)` — определение типа вложения. Здесь же **экспорт канваса**:
`markdown_to_docx(content, title)` (python-docx) и `markdown_to_pdf(content, title)`
(LibreOffice если есть, иначе `fpdf2` с системным шрифтом для кириллицы).

### Канвас (эндпоинты в `main.py`)
`/api/canvas` (CRUD), `/api/canvas/{id}/revise` (доработка нейросетью через `complete`:
модель видит текущее содержимое и возвращает обновлённую версию), `/api/canvas/{id}/export`
(Docx/PDF). Модель — `Canvas`; доступ — через сессию канваса (`_canvas_or_403`).

## Аккаунты, доступ, настройки

### `accounts.py`
Режим аккаунтов: pbkdf2-пароли, регистрация/логин, таблица токенов
(`user_from_token`), генерация и обмен кода привязки Telegram (`make_link_code`/
`consume_link_code`/`bind_telegram`), `scope_query()` — фильтрация выборок по владельцу
(админ видит всё, пользователь — своё).

### `admin_service.py`
Настройки уровня приложения в таблице `app_settings`, кэшируемые в памяти:
`security` (код доступа, пароль админа, `accounts_enabled`, `basic_auth`) и `telegram`
(токен, белый список, заявки, модель бота, `open_to_all`). `security_cache()` /
`telegram_cache()` читаются синхронно из middleware и бота.

### `settings_service.py`
Настройки подключения к LiteLLM (`connection` в `app_settings`): `get_connection`,
сохранение, маскировка ключа для не-админов, `fetch_proxy_models` (опрос `/v1/models`).

## Telegram

### `telegram_runtime.py`
Telegram-бот **внутри процесса** (aiogram polling как asyncio-задача, старт/стоп из
админки). Хэндлеры команд и кнопок, доступ по белому списку, привязка аккаунта,
друзья, приём текста/голоса/фото/документов, агрегация альбомов в один ход
(`_handle_media`), отправка длинных ответов (`send_long`). См. [TELEGRAM.md](TELEGRAM.md).

### `telegram_format.py`
`split_message(text, limit)` — умная разбивка длинных ответов (по абзацам/строкам, с
балансировкой код-блоков ```` ``` ````). `markdown_to_html(text)` — Markdown → безопасный
Telegram-HTML. `render_for_telegram(text)` — готовые HTML-куски.

### `debug_log.py`
Кольцевой буфер последних запросов к LLM (модель, что отправлено/получено, ошибки) —
показывается в UI по кнопке 🐞 и доступен через `/api/debug/log`.
