# Модель данных

ORM-модели в `backend/models.py` — **единственный источник правды о структуре БД**.
Одна SQLite-база (`data/aichat.db`) обслуживает и веб, и Telegram-бота. Поле
`owner_id` (nullable) появляется у пользовательских сущностей для режима аккаунтов:
`NULL` = общая/легаси-запись (видна всем/админу), иначе — приватная запись владельца.

## Таблицы

### `characters` — `Character`
Карточка персонажа (поля совместимы с SillyTavern): `name`, `description`,
`personality`, `scenario`, `first_message`, `system_prompt`, `avatar_path` (data:URI
или URL), `generation_params` (JSON, переопределяет дефолты), `model`, `owner_id`.

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
готовности ответа).

### `attachment_blobs` — `AttachmentBlob`
Данные (base64) вложений — отдельно от сообщений: `message_id`, `data`.
Раньше base64 лежал в JSON-колонке `messages.attachments`, и каждый ход/открытие
чата поднимал в память сотни МБ; теперь данные достаются точечно
(`backend/attachments.py`). Легаси-строки мигрируются на старте
(`database._migrate_attachment_blobs`).

### `group_members` — `GroupMember`
Связь сессия ↔ персонаж для групповых чатов.

### `horae_entries` — `HoraeEntry`
Запись памяти Horae. `session_id` (NULL = глобальный лор) и/или `character_id`
(лорбук из карточки). Поля: `category`, `title`, `content`, `keywords` (JSON),
`always_on` (подмешивать всегда), `enabled`, `priority`. См. [HORAE.md](HORAE.md).

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
- `connection` — подключение к LiteLLM (base_url, api_key, default_model, image_model);
- `security` — `access_code`, `admin_password`, `accounts_enabled`, `basic_auth`;
- `telegram` — токен, `enabled`, `default_character_id`, `model`, `open_to_all`,
  `whitelist[]`, `requests[]`.

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
