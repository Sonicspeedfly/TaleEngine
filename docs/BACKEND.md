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
- Фоновая память после хода: `_maybe_update_summary` и `_summary_pass` — тонкие
  обёртки над `memory_service` (зависимости передаёт `_memory_deps()`).
- Раздача `frontend/` через StaticFiles (mount на `/`).

### `config.py`
`Settings` (pydantic-settings) — читает `.env`. Главное: `DATABASE_URL`,
`LITELLM_USE_PROXY/BASE_URL/API_KEY`, `DEFAULT_MODEL`, дефолтные `DEFAULT_*`-параметры
генерации, `CONTEXT_TOKEN_BUDGET`, настройки мастер-памяти `MEMORY_*` и
`MODEL_CONTEXT_LIMIT` (см. [HORAE.md](HORAE.md)), ключи провайдеров, `TELEGRAM_*`. Валидатор
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
  Если ответ приходит ПУСТЫМ (провайдер заблокировал контент неотключаемым фильтром
  или «думающая» модель исчерпала `max_tokens` на рассуждения) — бросаем понятную
  ошибку с `finish_reason` (в отладочном логе и клиенту), а не молчим.
- `sampling_overrides(max_tokens=…, temperature=…, top_p=…)` — контекстный менеджер
  на `ContextVar`: внутри блока `stream_completion` кладёт эти значения поверх
  `_merge_params`. Так служебный вызов памяти получает свои температуру и длинный
  вывод, оставаясь при `params=None` (фильтры безопасности выключены), а сигнатуры
  `complete`/`stream_completion` не меняются.
- `finish_reason_probe()` — контекстный менеджер на том же приёме: внутри блока
  `stream_completion` пишет в отданный словарь `finish_reason` стрима. Нужен
  памяти: непустой ответ, оборванный `max_tokens`, шлюз отдаёт как успех
  (исключение — только при пустом), а для снимка это обрыв. `is_length_finish`
  узнаёт `length`/`max_tokens`.
- `model_max_output_tokens(model)` — лимит вывода модели по карте LiteLLM
  (`max_output_tokens`; имя пробуется и без префикса `litellm_proxy/`), `None` —
  модель неизвестна. По нему память прижимает свой `max_tokens` и бюджет снимка.
- `GEMINI_SAFETY_OFF` — снятие настраиваемых фильтров Gemini/Vertex AI. Порог **`OFF`**
  (сильнее `BLOCK_NONE`: полностью выключает фильтр; для Gemini 2.5/3 это дефолт) на все
  категории, включая `CIVIC_INTEGRITY`. Применяется, когда `params is None` (служебные
  вызовы — режиссёр и т.п.) ИЛИ `disable_safety` (по умолчанию `True` — проекту нужна
  полная свобода; пользователь может вернуть фильтры галкой). Неотключаемые фильтры
  Google (например CSAM) снять нельзя. Не-Gemini провайдерам параметр отбрасывает
  `drop_params`.

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
  и текущее сообщение (текст или мультимодальный контент). Всё, что от обрезки
  истории не зависит (системный промпт, аватары, база знаний, блок снимка —
  `_summary_tail`, статичный хвост — `_static_tail`, текущее сообщение),
  собирается до неё и резервируется в бюджете (`stable_trim_start(reserved=…)`);
  вне резерва — только вспомненные факты и манифест файлов, они зависят от обрезки.
- `build_context_from_db(...)` — обёртка, читающая данные из БД и зовущая
  `assemble_context`. Подробности про память — в [HORAE.md](HORAE.md).
- Отчёт для инспектора (`report`): у каждого блока хвоста — `key` (`snapshot`,
  `recalled`, `anchor`, …), а `report["tiers"]` делит ход на уровни для монитора
  токенов: система, память (снимок и факты), окно, текущее сообщение. Мастер-снимок
  новой схемы идёт в блок `[ХРОНИКА И СОСТОЯНИЕ ЧАТА]` как есть, без префикса
  «- заголовок:».
- `messages_to_history(msgs)` — история с **СОХРАНЕНИЕМ вложений**: прошлые сообщения
  пользователя с картинкой/аудио превращаются в мультимодальный контент, чтобы модель
  «видела» присланный ранее файл и на последующих ходах (раньше вложения истории
  терялись). Вложения включаются от свежих к старым до лимита `_MAX_HISTORY_ATT_BYTES`;
  что не влезло — заменяется текстовой пометкой. Используется в `build_context_from_db`
  и в путях regenerate/continue/retry.
- `estimate_content_tokens(content)` — оценка токенов для мультимодального контента
  (base64 картинок/аудио НЕ считается как текст, иначе бюджет выбрасывал бы всю историю).
- `estimate_tokens(text)` — tiktoken `cl100k_base` с кэшем в два яруса: тексты до
  20 000 символов — в большом (история пересчитывается каждый ход), длиннее — в
  маленьком на 16 записей; `count_tokens(text)` — то же без кэша (им сервис
  памяти считает снимки: каждый уникален, и кэш держал бы их тексты).

### `hierarchical_memory.py`
Чистое ядро иерархической пакетной памяти (2.5.0): свёртка старой истории в
мастер-снимок `снимок_N = слияние(снимок_{N−1}, пакет_N)`. Импортирует **только
stdlib** — ни БД, ни FastAPI, ни litellm; модель приходит колбэком, сообщения —
готовыми `MemoryMessage` или через протокол `BatchSource`. Поэтому весь контракт
проверяется тестами без сети и базы (`tests/test_hierarchical_memory.py`).
- Схема снимка: четыре раздела (`SECTIONS`, `GUARDED_SECTIONS`), `SNAPSHOT_SCHEMA =
  "hms-1"`, обёртка ответа `<master_state>…</master_state>`; `parse_sections`,
  `render_snapshot`, `is_structured`.
- `normalize_message` (строка `[#id · время · автор] текст` + `📎 вложения`, чистка
  base64/HTML/`<think>`), `plan_batch`/`plan_batches` — пакеты по числу сообщений и
  символам.
- `validate_snapshot`/`extract_snapshot` — обёртка, разделы, «сдувание»;
  `guard_entries` — возвращает записи разделов 2–4, которые модель потеряла
  (сопоставление — мультимножество, для повторяющихся ключей — расширенный ключ).
- `classify_error` → `MemoryLLMError(kind, message, retryable)`; исключения
  `SnapshotValidationError`, `SourceConflictError`, `MemoryCancelled` (отмена
  прогона посреди паузы).
- `HierarchicalMemoryManager`: `call` (пауза-ограничитель и повторы с бэкоффом для
  любого запроса; отмена прогона обрывает паузы), `merge_block` (слияние +
  корректирующие ходы + страж; обрыв лимитом вывода — сжатие и один повтор;
  снимок больше лимита вывода `MemoryConfig.output_tokens` — сжатие до слияния
  или отказ без вызова), `compact` (сжатие хроники в арки сверх бюджета; из
  ответа берётся только хроника), `scan_and_compress_history` (цикл по пакетам с
  прогрессом `ScanProgress.line()`, отменой и лимитом пакетов). Сигнатура из ТЗ
  `scan_and_compress_history(chat_history, batch_size=20, delay_ms=1500)`
  принимается как есть: `batch_size`/`delay_ms` — переопределения `MemoryConfig`
  на один прогон.
- `window_start`/`WINDOW_STEP`/`DEFAULT_WINDOW = 50` (реэкспортируются из
  `horae_recall`), `SlidingWindow`, `budget_tiers` (монитор токенов),
  `render_export_markdown`; промпты `MASTER_STATE_PROMPT`, `COMPACT_PROMPT`,
  `COMPACT_ARCS_PROMPT`, `CORRECTION_PROMPT`.

### `memory_service.py`
Адаптер ядра к БД и к остальному бэкенду. `main` сюда не импортируется (цикл):
`complete` и `get_connection` приходят через `MemoryDeps` лямбдами, которые берут
глобалы `main` в момент вызова, — поэтому `patch("backend.main.complete")` в
тестах действует и на сервис.
- Запись «📜 Память чата (авто)»: `summary_last_id`, `adopt_summary` (снимок без
  обрезки + `meta.schema/tokens/updated_by/warnings`), `clamp_summary_pointer`.
- `memory_config`/`build_manager` — параметры из `ui` и `.env`, фоновая модель
  `summary_model` в подключении при `params=None`, вывод через
  `llm_gateway.sampling_overrides`, прижатый к лимиту вывода модели
  (`memory_output_limit`, `max_snapshot_tokens`); непустой ответ, оборванный
  лимитом (`finish_reason_probe`), — `MemoryLLMError("length")`; 400 из-за
  `max_tokens` — один повтор с `DEFAULT_MAX_TOKENS`. `load_memory_messages` —
  сообщения чата с настоящими именами авторов и временем в поясе чата.
- `DbBatchSource` — источник пакетов: только то, что старше активного окна; цель
  записи — живой снимок или буфер пересборки `meta["rebuild"]` (остановленный
  буфер, `paused`, — только для «Продолжить»); сверка куска с чатом перед
  записью; факты пакета после записи, с доизвлечением пропуска после сбоя
  (`meta.facts_upto`, `_facts_gap`).
- `run_incremental` — ежеходный проход (его зовёт `main._maybe_update_summary`, не
  больше `MEMORY_MAX_BATCHES_PER_TURN` пакетов); сбой — в `meta.last_error`, и
  следующий проход ждёт паузу `turn_retry_after`; `chat_lock`/`is_busy` — один
  прогон памяти на чат.
- Задания «Пересобрать»/«Догнать»: `MemoryJob` (с `warnings` и `snapshot_tokens`),
  `start_job`, `cancel_job`, `forget_job` (удаление чата), `wait_job`; реестр — в
  памяти процесса, в работе — одно задание на процесс (`_jobs_gate`), остальные
  `queued`.
- `status`, `purge`, `export_markdown` — для эндпоинтов `/api/sessions/{id}/memory*`
  (см. [API.md](API.md)). Тесты — `tests/test_memory_service.py`.

### Horae State Engine (`horae_*.py`)
Перенос плагина Horae: состояние сюжета из служебных тегов ответа. Подробно —
[HORAE_STATE.md](HORAE_STATE.md), дизайн —
[superpowers/specs/2026-09-26-horae-state-engine-design.md](superpowers/specs/2026-09-26-horae-state-engine-design.md).
Чистые модули (stdlib, без БД и сети):
- `horae_state.py` — формат меты, `parse_reply` (теги → текст + мета),
  `strip_tags_text`, `merge_meta`, `normalize_meta`, `replay` (меты + журнал
  правок → состояние), `render_state_block`, `render_timeline`, `timeline`,
  `build_document`, `message_brief`, `to_api`;
- `horae_time.py` — даты сюжета (григорианские, русские, фэнтези, свой
  календарь), относительное время по-русски, возраст NPC;
- `horae_rpg.py` — `<horaerpg>`: разбор, применение, рендер, правила;
- `horae_tables.py` — `<horaetable:…>`: разбор, повтор, структура, рендер;
- `horae_prompts.py` — промпты (`horae_prompts_ru/*.txt`), правила тегов,
  промпты служебных задач и разбор их ответов;
- `horae_settings.py` — настройки по умолчанию и слои глобально → персонаж → чат;
- `horae_import.py` — данные плагина SillyTavern → наш формат.
Сервисы: `horae_engine.py` (БД ↔ ядро: меты, состояние чата, `context_parts`
для хода, `split_reply` для сохранения ответа, журнал правок, события, свёртки,
удаление/ветка/экспорт), `horae_vector.py` (документы и вспоминание),
`horae_tasks.py` (ИИ-анализ, скан, авто-свёртка, сжатие, заполнение NPC,
переписывание запроса, перенос; очередь служебных запросов `aux_complete`),
`horae_api.py` (эндпоинты; `build_router(current_user, can_access_session)`).

### `group_chat.py`
Логика групповых чатов: определяет, кто из персонажей отвечает, и формирует очередь
реплик. Используется раннером генерации (веб) и `_generate_group_reply` (Telegram).

Выбор говорящего — три ступени, гарантирующие, что на реплику пользователя ВСЕГДА
кто-то ответит: (1) **упоминание** — `mentioned_responders` (названный по имени);
(2) **режиссёр** (если включён) — `director_pick` просит модель назвать следующего
говорящего (низкая температура, снятые фильтры через `params`; при пустом/ошибочном
ответе возвращает `[]`, а НЕ падает и не выбирает молча первого); (3) **round-robin** —
`round_robin_next`, фолбэк, если никто не упомянут и режиссёр выключен/промолчал.
`_match_names` сопоставляет имена из ответа режиссёра (длинные — раньше, чтобы «Bot»
не перекрывал «Bot редактор»; порядок — как назвал режиссёр).

**Дубли участников — исправлено в корне.** Настоящая причина «группа пухнет с каждым
пересозданием»: `delete_session` удалял только сообщения, а `group_members` (и canvases /
session_shares / session-horae) оставались «сиротами»; в SQLite id удалённого чата
переиспользуется, и новый чат наследовал чужих участников. Фикс — **каскадное удаление**
в `delete_session` (все дочерние таблицы по `session_id`) + **разовая чистка сирот и
дублей** при старте (`database._cleanup_orphans`, идемпотентно). Плюс защита на входе:
`create_group` и импорт дедупят `character_ids`, а `load_members` возвращает участников
без дублей и в стабильном порядке (по `GroupMember.id`).

## Импорт / экспорт

### `characters.py`
Импорт карточек SillyTavern (PNG с tEXt-чанком или JSON V1/V2) и экспорт в карточку
V2. `extract_horae_entries` достаёт лорбук (`character_book`) → записи Horae;
`build_character_book` — обратно.

### `chat_import.py`
Импорт чатов SillyTavern (.jsonl). `parse_sillytavern_chat` разбирает реплики, выводит
имя персонажа из реплик при заглушке, **вырезает встроенные теги Horae**
(`<horae>`/`<horaeevent>`) из текста и отдаёт структурные данные Horae
(`horae_meta` каждого ответа, мету тегов по свайпам, глобальные данные
`chat[0]`) — `main.import_chat` переводит их в наш формат (`horae_import`).
Текстовые «снимок состояния + хронология» создаются, только если структурных
данных нет. См. [IMPORT_EXPORT.md](IMPORT_EXPORT.md).

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
