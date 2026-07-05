# Разработка и сопровождение

## Окружение

- **Python 3.12** (через лаунчер `py`). PATH-овый `python` может быть MSYS2/без pip —
  используйте `.venv`.
- Виртуальное окружение со всеми зависимостями — в `.venv`.

```
py -m venv .venv
.venv\Scripts\python -m pip install -r backend/requirements.txt
```

Зависимости — в `backend/requirements.txt` (FastAPI, uvicorn, litellm, sqlalchemy,
aiosqlite, pydantic(-settings), aiogram, **python-docx**, pytest, httpx). Для полной
конвертации Word→PDF опционально нужен **LibreOffice** (`soffice` в PATH); без него
Word отдаётся как извлечённый текст.

## Запуск

- **Windows:** `start.bat` — создаёт `.venv`, ставит зависимости, выбирает свободный
  порт, ждёт `/api/health`, открывает браузер. Сервер — в окне `cmd /k` (не закроется
  при ошибке).
- **Linux:** `./start.sh` (слушает `0.0.0.0:8000`; `HOST`/`PORT` переопределяемы).
- **Docker:** `docker compose up --build`. Из контейнера хостовый LiteLLM виден как
  `host.docker.internal:4000`.
- **Вручную:** `.venv\Scripts\python run.py --host 0.0.0.0 --port 8042` — запуск через
  `run.py`, а НЕ голый `uvicorn backend.main:app`. `run.py` ставит `ws_max_size=None`
  (снимает лимит 16 МБ на размер WebSocket-кадра), иначе большие вложения (аудио 14 МБ →
  base64 ≈ 19 МБ) по WS обрываются с close 1009. `HOST`/`PORT` — из аргументов или env.

Настройки подключения к LiteLLM задаются в UI (⚙ → «Подключение») и хранятся в БД.

## Тесты

```
.venv\Scripts\python.exe -m pytest -q
```

Все вызовы к LLM замоканы. `tests/conftest.py` подменяет `DATABASE_URL` на временный
файл (`aichat_test.db`) **до** импорта бэкенда — тесты не трогают рабочую БД. Фикстура
`client` поднимает `TestClient` с lifespan (init_db + загрузка кэшей). Покрытие:
роутинг LLM, сборка контекста Horae и приватность, импорт/экспорт (SillyTavern +
нативный), документы, форматирование для Telegram, аккаунты/друзья/шаринг, конфиг,
сквозной WebSocket.

> Тесты, меняющие глобальные настройки (режим аккаунтов, код доступа, Basic Auth),
> **обязаны откатывать их в `finally`** — кэш и БД общие в рамках прогона.

## Соглашения

- **Комментарии — на русском**, идентификаторы — английские.
- **`models.py`** — единственный источник правды о БД; новые поля добавляются туда,
  колонки доезжают авто-миграцией (`database.py`), новые таблицы — через `create_all`.
- **`.bat`-файлы только ASCII.** `cmd.exe` читает `.bat` в OEM-кодировке — кириллица в
  `echo`/`REM` ломает выполнение. Русский можно в `.sh`, `.py`, web-UI. Проверка: в
  `start.bat` нет байтов > 127.
- **Всегда передавайте api_key в LiteLLM** — даже для прокси без авторизации
  (`DUMMY_PROXY_KEY`), иначе AuthenticationError ещё до запроса.
- **Vertex image-модели** требуют явный `vertex_project` в конфиге прокси (текстовые
  читают его из ADC, image — нет).
- Дескрипторы WebSocket/EventSource на фронте держите в `_`-полях, не в реактивном
  `data` (иначе Vue обернёт их в Proxy).

## Как добавить...

### …параметр генерации
1. Поле в `GenerationParams` (`schemas.py`).
2. (Опц.) дефолт в `config.py` + подстановка в `_merge_params` (`llm_gateway.py`).
3. Поле в `params` и `<input>` в шаблоне (`frontend/app.js`).

`_merge_params` распаковывает все заданные поля в `litellm.acompletion(**kwargs)`;
неподдерживаемое LiteLLM отбрасывает (`drop_params=True`).

### …REST-эндпоинт
Добавьте функцию с декоратором `@app.<метод>` в `main.py`, при необходимости
`user=Depends(current_user)` и проверку доступа (`_can_access_*`, `accounts.scope_query`).
Опишите его в [API.md](API.md).

### …поле в БД
Добавьте в модель (`models.py`) — авто-миграция доедет колонку при следующем старте.
Для несовместимых изменений — ручная миграция или `reset_db.bat`.

### …возможность бота
Хэндлер в `_register(dp)` (`telegram_runtime.py`). Длинные ответы — через `send_long`,
вложения — через `_attachment_from_message`/`_process_messages`.

### …тип вложения
Расширьте `AttachmentIn.type` (`schemas.py`) и `_content_from_attachment`
(`llm_gateway.py`); на фронте — `onAttach`/`attachLabel` (`app.js`).

## Полезные команды

```
.venv\Scripts\python -m compileall -q backend     # быстрая проверка синтаксиса
node --check frontend/app.js                       # синтаксис JS (не валидирует Vue-шаблон!)
```

> `node --check` НЕ ловит ошибки внутри шаблона-строки Vue — проверяйте монтирование в
> браузере (страница должна отрисоваться без ошибок в консоли).

## Известные подводные камни

- **Кэш статики.** Раньше браузер кэшировал старый `app.js`; теперь
  `NoCacheStaticMiddleware` ставит `no-cache`. При обновлениях во время разработки —
  хард-рефреш (Ctrl+F5).
- **Новые таблицы в боевой БД** появляются только после перезапуска сервера.
- **Basic Auth + preview/отладка:** относительный `fetch` падает, если в URL есть
  `user:pass@`. Откройте чистый URL — браузер уже закэшировал учётку после первого входа.
