# Модель данных

ORM-модели в `backend/models.py` — **единственный источник правды о структуре БД**.
Одна SQLite-база (`data/aichat.db`) обслуживает и веб, и Telegram-бота. Поле
`owner_id` (nullable) появляется у пользовательских сущностей для режима аккаунтов:
`NULL` = общая/легаси-запись (видна всем/админу), иначе — приватная запись владельца.

## Таблицы

### `characters` — `Character`
Карточка персонажа (поля совместимы с SillyTavern): `name`, `description`,
`personality`, `scenario`, `first_message`, `system_prompt`, `mes_example`
(примеры реплик — держат «голос»), `post_history_instructions` (SillyTavern
«jailbreak»/UJB — переинъектируются в самый конец контекста), `avatar_path`
(data:URI или URL), `generation_params` (JSON, переопределяет дефолты), `model`,
`owner_id`, `horae_profile` (JSON: `{"settings": {...}, "tables": [...]}` —
настройки Horae персонажа поверх глобальных и шаблоны таблиц персонажа).

### `chat_sessions` — `ChatSession`
Сессия чата. `user_key` различает ведущего диалог: `web:<uuid>` или `tg:<telegram_id>`.
Поля: `character_id` (ведущий персонаж), `title`, `scenario` (общая сцена группы),
`author_note` (Author's Note), `background` (фон: градиент/URL/data:URI),
`is_group`, `director` (ИИ-режиссёр), `persona_id` (активная персона), `owner_id`,
`timezone` (часовой пояс пользователя ДЛЯ ЭТОГО чата: IANA-имя `Europe/Moscow` или
смещение `+03:00`; нейросеть видит по нему текущее время собеседника, метки времени
в UI показываются в нём же; настраивается во вкладке «Персона», по умолчанию —
автоматически из браузера).

### `messages` — `Message`
Сообщение. `role` = `user|assistant|system`, `content` (зеркалит активный свайп),
`attachments` (JSON — только МЕТА вложений: `type` = `image|audio|video|document`,
`mime`, `name`, `size`, `blob_id`; сами base64-данные — в `attachment_blobs`),
`swipes` (JSON — варианты ответа) + `active_swipe`, `model_used` (какая модель
ответила; при срабатывании запасной — она), `speaker_name` (кто сказал в группе),
`reply_to_id` (ответ на конкретное сообщение), `created_at` (UTC; в API отдаётся
ISO-строкой с «Z» — у user-сообщения это время отправки, у assistant — время
готовности ответа), `horae` (JSON или NULL: `{"metas": [мета свайпа 0, …],
"side": bool}` — данные Horae State Engine по свайпам, см.
[HORAE_STATE.md](HORAE_STATE.md); служебные теги из `content` вырезаны).

### `attachment_blobs` — `AttachmentBlob`
Данные (base64) вложений — отдельно от сообщений: `message_id`, `data`.
Раньше base64 лежал в JSON-колонке `messages.attachments`, и каждый ход/открытие
чата поднимал в память сотни МБ; теперь данные достаются точечно
(`backend/attachments.py`). Легаси-строки мигрируются на старте
(`database._migrate_attachment_blobs`).

### `group_members` — `GroupMember`
Связь сессия ↔ персонаж для групповых чатов.

### `knowledge_files` — `KnowledgeFile`
База знаний чата: постоянные справочные файлы, доступные модели в КАЖДОМ ходе.
`session_id`, `owner_id`, `name`, `mime`, `kind` (`image|audio|video|document`),
`content` (извлечённый текст документа — дёшево слать каждый ход; у медиа/PDF пусто),
`blob_id` (данные медиа/PDF в `attachment_blobs`). См. `backend/knowledge.py`.

### `horae_entries` — `HoraeEntry`
Запись памяти Horae. `session_id` (NULL = глобальный лор) и/или `character_id`
(лорбук из карточки). Поля: `category`, `title`, `content`, `keywords` (JSON),
`always_on` (подмешивать всегда), `enabled`, `priority`, `meta` (JSON, служебное).
У авто-сводки «📜 Память чата (авто)» (`category="summary"`) в `meta`:
`last_message_id` (указатель «учтено до»), `v` (формат указателя, `2` — окну можно
верить), `schema` (`"hms-1"` — мастер-снимок строгой схемы), `tokens`,
`updated_by` (`incremental`/`catchup`/`rebuild`), `warnings` (последние пять),
`facts_upto` (до какого сообщения разобраны факты), `last_error` (сбой
ежеходного прохода `{message, kind, at, failures}`) и `rebuild` — буфер
пересборки `{content, last_message_id, manual, started_at, batch_size, paused}`.
См. [HORAE.md](HORAE.md).

### `horae_facts` — `HoraeFact`
Атомарный факт долговременной памяти чата (слой 3 Horae, `backend/horae_recall.py`).
Извлекается фоном из переписки старше активного окна; на каждом ходу в контекст
попадают только факты, похожие на текущую реплику. Поля: `session_id`, `content`
(одно утверждение, до 300 символов), `embedding` (JSON — единичный вектор,
округлённый до 6 знаков; NULL, пока эмбеддинги не настроены или не досчитаны),
`embed_model` (какой моделью посчитан вектор: векторы разных моделей несопоставимы,
при смене модели факты пересчитываются), `source_message_id` (последнее сообщение
фрагмента, из которого извлечён факт: по нему считается свежесть и отсекаются
факты, чей источник модель и так видит дословно), `created_at`. Удаляются вместе
с чатом (`database._cleanup_orphans`) и при удалении сообщений, из которых взяты.

### `horae_chat_state` — `HoraeChatState`
Данные Horae State Engine уровня чата: `session_id` (PK), `data` (JSON:
журнал правок `ops`, свёртки хронологии `summaries`, таблицы чата `tables`,
данные глобальных/персонажных таблиц `table_overlays`, `rpg_config`,
переопределения настроек `settings`, стартовое состояние переноса `seed`,
`pinned_npcs`, `favorite_npcs`, последний скан `scan`, ошибка свёртки
`summary_error`, счётчик `seq`), `updated_at`. Само состояние сюжета не
хранится — пересчитывается из мет сообщений и журнала (`horae_state.replay`).

### `horae_memory_docs` — `HoraeMemoryDoc`
Документы вспоминания событий: `session_id`, `message_id` (NULL — перенесён
из прошлого чата), `origin`, `doc_hash`, `document` (события, место,
персонажи, дата ответа), `content` и `brief` (для перенесённых — текст ответа
и краткая мета), `embedding` + `embed_model`.

### `personas` — `Persona`
Персона пользователя (кем он отыгрывает): `name`, `description`, `avatar_path`, `owner_id`.

### `users` — `User`
Аккаунт (режим аккаунтов). Первый зарегистрированный — `role=admin`. `username`
(уникальный), `password_hash` (pbkdf2), `telegram_id` (привязка Telegram),
`avatar_path`.

### `user_tokens` — `UserToken`
Токен сессии пользователя (заголовок `X-User-Token`). Ключ — сам `token`, плюс `user_id`.

### `friendships` — `Friendship`
Дружба между ролевиками. `user_id` (инициатор), `friend_id`, `status` =
`pending|accepted`.

### `session_shares` — `SessionShare`
Доступ друга к чату: `session_id` + `user_id` (кому открыт). Даёт чтение и участие.

### `canvases` — `Canvas`
Канвас (документ/код рядом с чатом, как в Gemini): `session_id`, `source_message_id`
(из какого сообщения создан), `title`, `kind` = `document|code`, `language` (для кода),
`content`, `owner_id`, `created_at`/`updated_at`. Правится вручную и нейросетью,
экспортируется в Docx/PDF.

### `sampling_presets` — `SamplingPreset`
Сохранённый набор параметров генерации: `name` (уникальный), `params` (JSON),
`is_default` (применяется автоматически при загрузке UI), `owner_id`.

### `app_settings` — `AppSetting`
Универсальное key-value (JSON) хранилище настроек приложения. Ключи:
- `connection` — подключение к LiteLLM (base_url, api_key, default_model, image_model,
  `summary_model` — быстрая модель для фоновой сводки и фактов, `embedding_model` —
  модель эмбеддингов фактов; пустые = модель чата / поиск фактов по словам);
- `ui` — общие настройки интерфейса, в том числе памяти: `auto_summary`,
  `summary_every`, `memory_window` (активное окно, по умолчанию 50; 0 = вся
  история), `horae_facts` (факты выключены только явным `false`),
  `memory_batch` (размер пакета, 1–200), `memory_delay_ms` (пауза между
  запросами памяти, 0–60000), `memory_snapshot_tokens` (бюджет снимка,
  1000–200000; пока ключа нет — умолчания из `.env`), флаги разовых миграций
  интерфейса `memory_defaults_v` (окно 20 → 50) и `ctx_budget_v` (бюджет хода
  1 000 000 → 200 000 один раз);
- `security` — `access_code`, `admin_password`, `accounts_enabled`, `basic_auth`;
- `telegram` — токен, `enabled`, `default_character_id`, `model`, `open_to_all`,
  `whitelist[]`, `requests[]`;
- `horae` — глобальные настройки Horae State Engine (ключи — дизайн §7);
- `horae_library` — `global_tables` (шаблоны глобальных таблиц),
  `prompt_presets` (свои наборы промптов), `equipment_templates` (свои шаблоны
  слотов снаряжения).

## Миграции

Полноценного Alembic нет — используется лёгкий механизм в `database.py`:
- **Новые таблицы** создаются сами через `Base.metadata.create_all` при старте.
- **Новые колонки** в существующих таблицах добавляет `_sqlite_add_missing_columns`
  (сравнивает модель с фактической схемой и делает `ALTER TABLE ADD COLUMN`).

Поэтому добавление поля в модель обычно не требует ручной миграции — достаточно
перезапустить сервер. Для несовместимых изменений (переименование/удаление колонок,
смена типов) механизм не подходит — там нужна ручная миграция или пересоздание БД
(`reset_db.bat`).

> ⚠️ `session_shares` и подобные новые таблицы появляются в боевой БД только **после
> перезапуска** сервера (когда отработает `create_all`).
