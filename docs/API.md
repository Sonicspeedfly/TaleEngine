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
| PATCH | `/api/sessions/{id}` | Переименование/мета (scenario, author_note, фон, …) |
| DELETE | `/api/sessions/{id}` | Удалить чат |
| GET | `/api/sessions/{id}/messages` | Сообщения чата. Пагинация: `?limit=N` — последние N; `?before=<id>&limit=N` — порция старше id (ленивая подгрузка при скролле вверх); без параметров — вся история |
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

## Персоны, пресеты, настройки

| Метод | Путь | Назначение |
|------|------|-----------|
| GET/POST | `/api/personas` · DELETE `/api/personas/{id}` | Персоны пользователя |
| GET/POST | `/api/presets` · DELETE `/api/presets/{id}` | Пресеты параметров |
| POST | `/api/presets/{id}/default` | Сделать пресет дефолтным |
| GET/PUT | `/api/settings/connection` | Подключение к LiteLLM (ключ маскируется не-админам) |
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
| POST | `/api/jobs/{job_id}/cancel` | Остановить генерацию по id задачи (для WS- и HTTP-хода) |

Фронт: есть вложения → `POST /send` + `EventSource /sse/job/{job_id}`; только текст →
быстрый путь по WebSocket. События SSE и WS идентичны (общий обработчик).

### SSE `/sse/job/{job_id}`
`EventSource`-поток задачи генерации — для дослушивания хода (обрыв WebSocket ИЛИ
HTTP-отправка с вложениями). Отдаёт накопленный буфер и финальный `done`/`error`.

### `/api/health`
Проверка здоровья без авторизации (опрашивается `start.bat` при запуске). Открыт даже
при включённом Basic Auth.
