# API: REST / WebSocket / SSE

Все REST-эндпоинты — под префиксом `/api`. Авторизация зависит от режима доступа
(см. [SECURITY.md](SECURITY.md)): заголовок `X-User-Token` (аккаунты),
`X-Access-Code` / `X-Admin-Password` (код доступа), либо открыто. При включённом
HTTP Basic Auth поверх всего нужен ещё заголовок `Authorization: Basic …` (кроме
`/ws*` и `/api/health`).

## Аутентификация и доступ

| Метод | Путь | Назначение |
|------|------|-----------|
| GET | `/api/auth/status` | Режим доступа: нужен ли код, заданы ли аккаунты/админ |
| POST | `/api/auth/login` | Проверка кода доступа (режим кода) |
| POST | `/api/auth/admin` | Проверка пароля администратора |
| POST | `/api/auth/register` | Регистрация (первый зарегистрированный — админ) |
| POST | `/api/auth/login_user` | Вход по логину/паролю → токен |
| GET | `/api/auth/me` | Текущий пользователь по токену |
| PATCH | `/api/auth/me` | Обновить профиль (например аватар) |
| POST | `/api/auth/link/telegram` | Получить одноразовый код привязки Telegram |

## Персонажи

| Метод | Путь | Назначение |
|------|------|-----------|
| GET | `/api/characters` | Список (scope по владельцу в режиме аккаунтов) |
| POST | `/api/characters` | Создать |
| PATCH | `/api/characters/{id}` | Обновить поля |
| DELETE | `/api/characters/{id}` | Удалить |
| POST | `/api/characters/import` | Импорт карточки SillyTavern (PNG/JSON) |
| GET | `/api/characters/{id}/export` | Экспорт в карточку V2 (с лорбуком) |

## Чаты (сессии) и сообщения

| Метод | Путь | Назначение |
|------|------|-----------|
| GET | `/api/sessions?character_id=` | Чаты персонажа |
| POST | `/api/sessions?character_id=` | Новый чат |
| GET | `/api/sessions/shared` | Чаты, которыми со мной поделились |
| PATCH | `/api/sessions/{id}` | Переименование/мета (scenario, author_note, фон, `timezone` — часовой пояс чата, …) |
| DELETE | `/api/sessions/{id}` | Удалить чат |
| GET | `/api/sessions/{id}/messages` | Сообщения чата. Пагинация: `?limit=N` — последние N; `?before=<id>&limit=N` — порция старше id (ленивая подгрузка при скролле вверх); без параметров — вся история. Вложения — только МЕТА (type/mime/name/size), БЕЗ base64 (иначе чат с фото/аудио весит десятки МБ). Каждое сообщение содержит `created_at` (ISO, UTC) — время отправки (user) / готовности ответа (assistant) |
| GET | `/api/messages/{id}/att/{idx}` | Байты одного вложения сообщения (лениво грузят `<img>`/`<audio>`, кэшируется). Авторизация — заголовком или `?token=`/`?access_code=` (теги не шлют заголовки) |
| PATCH | `/api/messages/{id}` | Редактировать сообщение/свайп |
| DELETE | `/api/messages/{id}` | Удалить сообщение |
| GET | `/api/sessions/{id}/export` | **Нативный экспорт чата AiChat** |
| POST | `/api/sessions/import` | Импорт чата (нативный AiChat **или** SillyTavern — автоопределение) |
| POST | `/api/sessions/{id}/image` | Генерация арта (по описанию/сцене/обзору) |

## Группы

| Метод | Путь | Назначение |
|------|------|-----------|
| GET | `/api/groups` | Групповые чаты |
| POST | `/api/groups` | Создать групповой чат из нескольких персонажей |
| POST | `/api/sessions/{id}/members` | Добавить персонажей `{character_ids:[…]}`; обычный чат при этом ПРЕВРАЩАЕТСЯ в групповой |
| DELETE | `/api/sessions/{id}/members/{character_id}` | Убрать персонажа из группы (нельзя последнего; его реплики остаются) |

## База знаний чата

Постоянные справочные файлы чата, доступные модели/персонажам В КАЖДОМ ходе
(в отличие от разовых вложений сообщения). Документы читаются как текст (кешируется),
медиа/PDF прикладываются целиком. Работает в личных и групповых чатах.

| Метод | Путь | Назначение |
|------|------|-----------|
| GET | `/api/sessions/{id}/knowledge` | Список файлов базы знаний (мета) |
| POST | `/api/sessions/{id}/knowledge` | Добавить файл `{type, data, mime, name}` (как вложение) |
| DELETE | `/api/knowledge/{kid}` | Удалить файл базы знаний (с данными) |

## Канвас (документ/код рядом с чатом)

| Метод | Путь | Назначение |
|------|------|-----------|
| POST | `/api/sessions/{id}/canvas_generate` | Сгенерировать НОВЫЙ документ/код: ответ ИИ → Канвас + «плашка» в чате |
| POST | `/api/sessions/{id}/canvas_edit` | Правка ОТКРЫТОГО канваса: `{canvas_id, prompt}` → PATCH того же документа (мутация, без новой плашки) |
| GET | `/api/canvas?session_id=` | Канвасы сессии |
| GET | `/api/canvas/{id}` | Один канвас (открытие по клику на плашку) |
| POST | `/api/canvas` | Создать вручную |
| PATCH | `/api/canvas/{id}` | Ручное редактирование (title/kind/language/content) |
| POST | `/api/canvas/{id}/revise` | Доработка ИИ: `{instruction}`; опц. `selection_start/end` — правит только выделенный фрагмент |
| POST | `/api/canvas/{id}/undo` | Откат к предыдущей версии (история правок) |
| DELETE | `/api/canvas/{id}` | Удалить |
| GET | `/api/canvas/{id}/export?fmt=docx\|pdf` | Экспорт в Word или PDF |

Подробнее о Канвасе (двухоконный режим, inline-редактирование, быстрые действия,
версионирование) — в [CANVAS.md](CANVAS.md).

## Память Horae

| Метод | Путь | Назначение |
|------|------|-----------|
| GET | `/api/horae?session_id=` | Записи (scope по доступу; глобальный лор — всем) |
| POST | `/api/horae` | Создать запись (session_id / character_id / глобально) |
| PATCH | `/api/horae/{id}` | Обновить |
| DELETE | `/api/horae/{id}` | Удалить |
| GET | `/api/sessions/{id}/memory` | Статус мастер-памяти чата: `job` (последнее задание или `null`), `snapshot` (`exists`, `tokens`, `budget`, `covered_upto`, `schema`, `structured`, `updated_at`, `over_budget`, `warnings`, `last_error`, `retry_after`), `staging` (буфер пересборки или `null`: `last_message_id`, `tokens`, `manual`, `started_at`, `paused`), `backlog` (`pending`, `window`, `messages_total`), `facts.count`, `settings` (`batch_size`, `delay_ms`, `snapshot_tokens`, `max_snapshot_tokens`) |
| POST | `/api/sessions/{id}/memory/rebuild` | Задание памяти `{mode: "rebuild" \| "catchup", resume?, batch_size? 1–200, delay_ms? 0–60000}` → 202 `{job}`. `rebuild` — собрать снимок с нуля в буфер (`resume: true` — продолжить прерванную или остановленную пересборку), `catchup` — свернуть бэклог в цель записи: живой снимок, а при незаконченной (не остановленной) пересборке или сводке до 2.4.0 — её буфер с подменой в конце. 409 — у чата уже есть задание в очереди или в работе. Заданий в работе на процесс — одно, остальные ждут в `queued` |
| POST | `/api/sessions/{id}/memory/cancel` | Остановить задание: начатый запрос к модели доводится, паузы (между запросами и перед повтором) обрываются сразу, слитый пакет сохраняется; из очереди — сразу. Буфер остановленной пересборки помечается `paused` и ждёт `resume: true` → `{ok, job}`; `ok: false` — останавливать нечего |
| DELETE | `/api/sessions/{id}/memory` | Сброс: остановить задание и идущий ежеходный проход (после текущего запроса к модели), удалить снимок (с буфером) и все атомарные факты чата; сообщения остаются → `{snapshot_deleted, facts_deleted}` |
| GET | `/api/sessions/{id}/memory/export?facts=1` | Снимок `.md`-файлом (`text/markdown; charset=utf-8`, `Content-Disposition: attachment`, имя `memory-<название>-<id>.md`); `facts=1` — с приложением атомарных фактов. Снимка нет → 404 |
| GET | `/api/sessions/{id}/context` | Инспектор хода: что уйдёт в модель на следующем ходу (ход не выполняется). В отчёте — блоки, `tail` (у каждого блока хвоста `key`), `memory`, `recalled` и `tiers` — монитор токенов; у группового чата `tiers: null` и `tiers_unavailable: "group"` |

Эндпоинты `/memory*` проверяют доступ как у чата (`_can_access_session`): нет
чата → 404, чужой → 403. Задание идёт в фоне; ответ `rebuild` приходит сразу, а
прогресс (`job.processed/total`, `job.line` вида `[Обработано 140/800 сообщений |
Сжато до 4 200 токенов]`, `job.phase`: `merge`/`compact`/`wait`/`retry`/`done`)
интерфейс опрашивает через `GET …/memory`. Статусы задания: `queued`, `running`,
`done`, `error` (текст причины — в `job.error`), `cancelled`. Реестр заданий живёт
в памяти процесса, поэтому после перезапуска сервера `job` — `null`, а буфер
пересборки остаётся в `staging`.

Поля статуса, которые объясняют итог и сбои:

- `job.warnings` — предупреждения прогона (список строк); `job.snapshot_tokens` —
  размер живого снимка после задания (для итога «N сообщений → M токенов»;
  `null`, пока задание идёт или если оно упало). `job.state_tokens` — размер
  цели прогона (у пересборки — буфера). Задание, которому нечего было сжимать,
  завершается `done` с `processed: 0` и предупреждением: «пересборка не нашла
  сообщений старше окна — прежний снимок оставлен», «сжимать нечего — весь чат
  в окне» или «догонять нечего — всё, что старше окна, уже в снимке».
- `snapshot.last_error` — сбой ежеходного прохода `{message, kind, at,
  failures}` или `null`; `snapshot.retry_after` — ISO UTC, не раньше которого
  ежеходный проход попробует снова (`at` + `min(6 ч, 10 мин · 2^(failures−1))`),
  или `null`. Задания паузу не ждут; удачная запись снимка ошибку убирает.
- `snapshot.warnings` — предупреждения последней записи снимка (до пяти) и,
  если бюджет снимка больше, чем успевает написать модель памяти, строка об этом.
- `settings.max_snapshot_tokens` — наибольший бюджет снимка, который модель
  памяти успевает написать за ответ (⌊(max_output_tokens − 1024) / 1,4⌋ по
  карте LiteLLM), или `null` — модель LiteLLM не знает.
- `staging.paused` — пересборку остановили кнопкой: буфер ждёт `resume: true`, а
  ежеходные проходы и «Догнать» пишут в живой снимок.

`tiers` в отчёте `/context`:

```json
{"system": 5200, "memory": 4300, "window": 61000, "current": 0,
 "total": 70500, "budget": 200000, "model_limit": 1000000,
 "pct_budget": 35.2, "pct_limit": 7.0,
 "window_messages": 52, "dropped_messages": 848, "trimmed_messages": 0}
```

`system` — системный промпт, база знаний, аватары и статичные блоки хвоста;
`memory` — блок мастер-снимка и вспомненные факты; `window` — дословная история
после окна и обрезки по бюджету; `current` — текущее сообщение; `pct_*` — доли
`total` от бюджета хода и от `MODEL_CONTEXT_LIMIT`. Снимок, база знаний, аватары
и статичный хвост резервируются в бюджете до обрезки истории, поэтому `total` не
больше `budget` — кроме блока вспомненных фактов (до ≈ 800 токенов), который от
обрезки зависит. У группового чата `tiers` — `null`, а `tiers_unavailable:
"group"`: ход группы собирается иначе (окна нет, запрос на каждого отвечающего).
Подробно — в [HORAE.md](HORAE.md).

## Horae State Engine (состояние сюжета)

Подробно — [HORAE_STATE.md](HORAE_STATE.md). Все эндпоинты чата проверяют
доступ к чату (как `/messages`). Роутер — `backend/horae_api.py`.

| Метод и путь | Что |
|---|---|
| `GET /api/sessions/{id}/horae/state?at=<mid>` | состояние сюжета, хронология, таблицы, настройки RPG, журнал правок, статистика (`injection_tokens`…), задание |
| `GET/PUT /api/messages/{id}/horae` | мета активного свайпа ответа (`{meta}`; `null` — стереть) |
| `POST /api/messages/{id}/horae/analyze` | ИИ-анализ ответа сейчас |
| `POST /api/messages/{id}/horae/side` | `{side}` — побочная сцена |
| `POST /api/sessions/{id}/horae/ops` | правка пользователя `{kind, …}` (виды — дизайн §6.2 и §9; `npc.pin`, `npc.favorite`) |
| `DELETE /api/sessions/{id}/horae/ops/{op_id}` | откатить правку |
| `POST /api/sessions/{id}/horae/events` | вставить событие `{mid, index?, level, text}` |
| `PATCH /api/sessions/{id}/horae/events` | `{mid, i, level?, text?}` (пустой текст — удалить) |
| `POST /api/sessions/{id}/horae/events/delete` | `{refs:[{mid, i}]}` |
| `POST /api/sessions/{id}/horae/compress` | сжать события/свёртки в свёртку `{refs, summary_ids, mode: events\|fulltext}` |
| `POST /api/sessions/{id}/horae/summaries` | своя свёртка `{from_mid, to_mid, text}` |
| `PATCH/DELETE /api/sessions/{id}/horae/summaries/{sid}` | `{text?, active?}` / удалить (дети возвращаются) |
| `POST /api/sessions/{id}/horae/summaries/run` | 202 — авто-свёртка сейчас (задание) |
| `POST /api/sessions/{id}/horae/scan` | 202 — ИИ-скан истории `{batch_tokens, include:{npc, affection, scene, relationships}}` |
| `GET /api/sessions/{id}/horae/job`, `POST …/job/cancel` | статус / остановка задания (`kind`, `status`, `processed/total`, `line`, `error`, `warnings`) |
| `POST /api/sessions/{id}/horae/scan/undo` | отменить скан |
| `POST /api/sessions/{id}/horae/tables` | таблица `{name, rows, cols, prompt, scope: local\|character\|global}` |
| `PATCH /api/sessions/{id}/horae/tables/{tid}` | одно из `{name}`, `{prompt}`, `{cell:{r,c,value}}`, `{structure:{op,index}}`, `{lock:{type,r,c,locked}}`, `{clear:true}`, `{scope}` |
| `DELETE …/horae/tables/{tid}`, `POST …/horae/tables/import` | удалить / импорт JSON |
| `GET/PUT /api/horae/settings` | глобальные настройки (`{defaults, global, effective}`; менять — админ); `null` снимает ключ |
| `GET/PUT /api/sessions/{id}/horae/settings` | переопределения чата (`{overrides, character, effective}`) |
| `GET/PUT /api/characters/{id}/horae_profile` | профиль персонажа `{settings, tables}` |
| `GET /api/horae/prompts`, `POST/DELETE …/prompts/presets` | промпты по умолчанию и наборы |
| `GET/PUT /api/sessions/{id}/horae/rpg_config`, `GET/PUT /api/horae/equipment_templates` | настройки RPG чата / шаблоны снаряжения |
| `GET /api/sessions/{id}/horae/export`, `POST …/horae/import` | данные Horae чата (`{data, mode: by_id\|initial}`) |
| `DELETE /api/sessions/{id}/horae` | стереть данные Horae чата |
| `POST /api/sessions/{id}/horae/carryover` | новый чат с памятью `{keep, vectors}` → `{session_id}` |
| `POST /api/sessions/{id}/horae/npc_enrich` | ИИ-заполнение профиля NPC `{name, aliases}` → `{fields, hits}` |
| `POST /api/sessions/{id}/horae/reindex`, `GET …/horae/recall?q=` | пересобрать документы поиска / отладка вспоминания |

`GET /api/sessions/{id}/messages` отдаёт у каждого сообщения `horae_brief`
(строка под ответом или `null`) и `horae_side`. Импорт SillyTavern отвечает
ещё `horae_structured` (сколько ответов со структурными данными Horae) и
`horae_chat_saved`. В инспекторе хода (`/context`) — `report.horae_state`,
`report.horae_recall`, блоки хвоста `horae_state`, `horae_recall`,
`horae_reminder` и блок системного промпта `horae_rules`; `horae_state` и
`horae_recall` монитор относит к памяти (Tier 2).

## Персоны, пресеты, настройки

| Метод | Путь | Назначение |
|------|------|-----------|
| GET/POST | `/api/personas` · DELETE `/api/personas/{id}` | Персоны пользователя |
| GET/POST | `/api/presets` · DELETE `/api/presets/{id}` | Пресеты параметров |
| POST | `/api/presets/{id}/default` | Сделать пресет дефолтным |
| GET/PUT | `/api/settings/connection` | Подключение к LiteLLM (ключ маскируется не-админам). Поля `fallback_model`/`auto_fallback` — запасная модель на случай сбоя основной |
| GET | `/api/models` | Прокси `/v1/models` LiteLLM |
| GET/PUT | `/api/settings/ui` | Серверные UI-предпочтения (параметры по умолчанию) |

## Друзья и шаринг (режим аккаунтов)

| Метод | Путь | Назначение |
|------|------|-----------|
| GET | `/api/friends` | Друзья + входящие заявки (с `friendship_id`) |
| POST | `/api/friends/add` | Заявка по логину (дедуп повторов) |
| POST | `/api/friends/{id}/accept` | Принять заявку |
| POST | `/api/friends/{id}/decline` | Отклонить заявку / удалить из друзей |
| GET | `/api/sessions/{id}/shares` | Кому открыт чат |
| POST | `/api/sessions/{id}/share` | Открыть доступ другу (нужно быть друзьями) |
| DELETE | `/api/sessions/{id}/share/{uid}` | Закрыть доступ |

## Администрирование (роль admin / пароль админа)

| Метод | Путь | Назначение |
|------|------|-----------|
| GET/PUT | `/api/admin/security` | Код доступа, пароль админа, режим аккаунтов, Basic Auth |
| GET/PUT | `/api/admin/telegram` | Токен бота, модель, `open_to_all` |
| POST/DELETE | `/api/admin/telegram/whitelist/{tg_id}` | Белый список Telegram-ID |
| POST | `/api/admin/telegram/start` · `/stop` | Запуск/остановка бота |
| GET | `/api/admin/users` | Список пользователей |
| POST | `/api/admin/users/{id}/role` | Сменить роль |
| DELETE | `/api/admin/users/{id}` | Удалить (нельзя последнего админа) |
| GET/DELETE | `/api/debug/log` | Лог запросов к LLM |

## Реальное время

### WebSocket `/ws/chat/{session_id}`
Авторизация — query-параметром `?token=` (аккаунты) или `?code=` (код доступа).
Клиент → сервер:
- `{"type":"user_message", content, attachments, params, reply_to_message_id}`
- `{"type":"regenerate", params}` — новый свайп к последнему ответу
- `{"type":"continue", params}` — дописать последний ответ
- `{"type":"retry", params}` — повторить ход после сбоя/обрыва/остановки: если чат
  кончается репликой пользователя (ответ не родился) — ответить на неё заново БЕЗ
  дублирования реплики; иначе — новый свайп (эквивалент regenerate)
- `{"type":"stop"}` — отменить генерацию (частичный текст сохраняется)

Сервер → клиент:
- `{"type":"job", job_id}` — id задачи (для SSE-дослушивания)
- `{"type":"speaker", name}` / `{"type":"token", content}` / `{"type":"speaker_done"}`
- `{"type":"thought", content}` — «размышления» модели (thinking) live; в ответ
  и в БД они не входят, клиент показывает их свёрнутым блоком 💭
- `{"type":"fallback", model, reason}` — основная модель не ответила, ход повторяется
  ЗАПАСНОЙ моделью (настройка «Подключение»); клиент сбрасывает live-текст —
  дальше токены придут с чистого листа
- `{"type":"done"}` — генерация завершена (клиент перечитывает историю)
- `{"type":"error", content}` — ошибка (показывается баннером, не «проглатывается»)

> **Лимит кадра WebSocket снят.** Сервер запускается через `run.py`
> (`ws_max_size=None` — без ограничения), поэтому большие сообщения по WS не
> обрываются. Обычный `uvicorn backend.main:app` вернул бы лимит в 16 МБ (14-МБ аудио
> в base64 ≈ 19 МБ → close 1009), поэтому запускать нужно **`python run.py`**, а НЕ
> голый uvicorn. Вложения дополнительно уходят по HTTP (см. ниже) — двойная страховка.

### HTTP-отправка хода (для больших вложений)
| Метод | Путь | Назначение |
|------|------|-----------|
| POST | `/api/sessions/{id}/send` | Отправить ход `{content, attachments, params, reply_to_message_id}` по HTTP (у тела нет 16-МБ лимита WS); возвращает `{job_id}`. Ответ слушается по SSE |
| POST | `/api/sessions/{id}/send_form` | То же multipart'ом — для БОЛЬШИХ файлов: поле `payload` (тот же JSON, но вложение может ссылаться на файл формы через `file_index`) + файлы `files` БИНАРНО. Браузер не кодирует base64 (экономия памяти и трафика) — кодирует сервер |
| POST | `/api/jobs/{job_id}/cancel` | Остановить генерацию по id задачи (для WS- и HTTP-хода) |

Фронт: есть вложения → `POST /send` + `EventSource /sse/job/{job_id}`; только текст →
быстрый путь по WebSocket. События SSE и WS идентичны (общий обработчик).

### SSE `/sse/job/{job_id}`
`EventSource`-поток задачи генерации — для дослушивания хода (обрыв WebSocket ИЛИ
HTTP-отправка с вложениями). Отдаёт накопленный буфер и финальный `done`/`error`.

### `/api/health`
Проверка здоровья без авторизации (опрашивается `start.bat` при запуске). Открыт даже
при включённом Basic Auth.
