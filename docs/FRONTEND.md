# Фронтенд

Тонкий клиент **без сборки**. `frontend/index.html` подключает с CDN Vue 3,
markdown-it и DOMPurify, затем `app.js`. Сервер раздаёт всё это как статику.
Node.js, npm и шаг компиляции не нужны.

## Файлы

- **`index.html`** — точка входа: подключает зависимости (CDN), монтирует приложение,
  содержит контейнер `#app`.
- **`app.js`** — всё приложение: один объект Vue с `data()`, `computed`, `methods` и
  **шаблоном-строкой** (HTML-вёрстка лежит как template прямо в этом файле).
- **`styles.css`** — тёмная тема (в духе SillyTavern): переменные `--bg/--accent/…`,
  сайдбар, пузыри сообщений, Markdown-стили под Telegram-вид, скроллбары, тосты.

## Устройство `app.js`

Это большой объект Vue. Условно делится на блоки:

### Состояние (`data()`)
Поля сгруппированы комментариями: персонажи/чаты (`characters`, `sessions`,
`sessionId`, `messages`), генерация (`streaming`, `currentReply`, `liveBubbles`),
вложения (`pendingAttachments`), память (`horae`, `horaeEdit`), аккаунты (`userToken`,
`currentUserObj`, `friends`, `friendsIncoming`, `sharedSessions`, `notifOpen`),
кросс-чат уведомления (`pendingChats`, `_bgJobs`), настройки (`connection`, `params`,
`presets`), админка (`adminSec`, `adminTg`, `adminUsers`), модалки (`profileOpen`,
`debugOpen`, `adminOpen`).

### Вычисляемые (`computed`)
`currentIsGroup`, `isAdmin` (роль в режиме аккаунтов / открыто в режиме кода),
`selectedCharacter`, `currentGroup`, `replyToMsg` и др.

### Методы (`methods`) — основные группы
- **Персонажи:** `loadCharacters`, `createCharacter`, `selectCharacter`,
  `importCharacter`, `exportCharacter`.
- **Чаты:** `loadSessions`, `newChat`, `openSession`, `openSharedSession`,
  `renameSession`, `deleteSession`, `exportSession` (нативный экспорт), `importChat`
  (автоопределение нативный/SillyTavern), `downloadJson`.
- **WebSocket/генерация:** `connectWs`, `onWsEvent`, `send`, `regenerate`, `stop`,
  `continueReply`, `finishStream`, `resumeSSE`; кросс-чат: `_handoffStreaming`,
  `_trackBackgroundJob`, `showToast`.
- **Вложения:** `onAttach`/`onPaste`/`onDrop`→`addFiles` (📎, Ctrl+V, drag&drop),
  `toggleRecord` (запись голоса). **Загрузка с обратной связью:** плашка вложения
  появляется СРАЗУ со спиннером (`loading:true`), а `data` дочитывается `FileReader`
  асинхронно (мутируем реактивную ссылку из массива, чтобы Vue перерисовал состояние).
  Готово → миниатюра/иконка; ошибка → ⚠ + красная рамка + тост. Computed
  `attachmentsLoading` + `_awaitAttachments()`: `send`/`sendArt` — async и **ждут
  дочитывания всех файлов** перед отправкой (иначе сообщение уходило без ещё не
  загруженного вложения — гонка с FileReader). `_dropBadAttachments` убирает битые,
  `_cleanAtts` шлёт на бэкенд только поля `AttachmentIn`. Пока грузятся — жёлтая плашка
  `.files-bar` и кнопка «⏳ файлы…».
- **Память Horae:** `loadHorae`, `saveHorae`, `editHorae`, `deleteHorae`.
- **Персоны/Author's Note:** `loadPersonas`, `createPersona`, `applySessionMeta`.
- **Аккаунты/друзья:** `submitAuth`, `logout`, `loadFriends`, `addFriend`,
  `acceptFriend`, `declineFriend`, `removeFriend`, `shareSession`.
- **Арты:** `sendArt`, `artFromMessage`, фон чата (`setBackground`, `uploadBackground`).
- **Вложения:** `onAttach`, `attachLabel`; предпросмотр в пузырях и композере (миниатюры,
  `<audio>`, чипы документов), лайтбокс по клику на картинку (`lightbox`).
- **Канвас:** `openCanvasFromMessage` (создаёт/открывает канвас из сообщения, авто-тип
  документ/код), `saveCanvas`, `reviseCanvas` (доработка ИИ), `exportCanvas` (Docx/PDF),
  `closeCanvas`.
- **Админка/профиль:** `openAdmin`, `loadAdmin`, `saveSecurity`, `saveTelegram`,
  `startBot/stopBot`, `linkTelegram`, `openDebug`.
- **Инициализация:** `initApp` (после прохождения гейта: грузит данные, ставит опрос
  заявок в друзья), `mounted` (проверка авторизации).

### Надёжность доставки ответа (WS → SSE → сторож)

Три уровня защиты от «ответ сгенерирован, но в чате не появился» (актуально для
удалённого сервера — обрывы и полуоткрытый TCP неизбежны):
1. **Автопереподключение WS** — `connectWs().onclose` планирует реконнект с бэкоффом
   (1.5с → 3с → … максимум 15с, сброс при onopen); при смене чата реконнект к старому
   чату отменяется. Пилюля в шапке показывает `online / ⟳ реконнект`.
2. **SSE-дослушивание** — при обрыве во время генерации `resumeSSE(jobId)` подключается
   к `/sse/job/{id}`; live-текст перед этим СБРАСЫВАЕТСЯ (SSE отдаёт накопленный буфер
   целиком — иначе токены задвоятся). `onerror` (задача уже очищена/404) закрывает ES и
   зовёт `finishStream()` — ответ, если родился, давно в БД.
3. **Сторож стриминга** — интервал в `initApp`: если `streaming` и 45с не было событий
   (полуоткрытый TCP не даёт onclose!) — `resumeSSE` по job_id, иначе `finishStream()`.

`stop()` работает и без WS: при живом соединении шлёт `stop` (+failsafe-разблокировка
через 4с, если done потерялся), при мёртвом — сразу `finishStream()`.

**Повтор хода:** computed `canRetry` (чат кончается репликой пользователя, не стримим) →
жёлтый баннер «⚠ Ответ не получен — ↻ Повторить» над композером; та же ссылка в баннере
ошибки. `retryGeneration()` шлёт WS `{"type":"retry"}` — бэкенд отвечает на повисшую
реплику БЕЗ её дублирования или делает новый свайп (см. API.md).

### Диалоги и уведомления (без браузерных alert/prompt/confirm)

Все браузерные `alert()`/`prompt()`/`confirm()` заменены на внутренние UI — это часть
приложения, а не системные окна браузера:
- `showToast(text, onClick)` — всплывающее уведомление (успех/ошибка), авто-скрытие ~8с
  (`.toast-wrap`). Замена информационных `alert()`.
- `askConfirm(message, { title, okText, cancelText, danger })` → `Promise<bool>` —
  модалка подтверждения (замена `confirm()`; по умолчанию `danger:true` — красная кнопка).
- `askPrompt(title, { message, value, placeholder, okText })` → `Promise<string|null>` —
  модалка ввода (замена `prompt()`; Enter — подтвердить, Esc/клик-вне/Отмена — `null`;
  поле авто-фокусится и выделяется).

Реализация — одно реактивное поле `dialog` + `_dialogResolve` (не в data, чтобы Vue не
оборачивал функцию в Proxy); один шаблон `.dialog-modal`. Вызовы `await this.askConfirm(…)`
внутри `async`-методов (`deleteSession`, `deleteCharacter`, `deleteMessage`, `removeFriend`,
`deleteUser`), `await this.askPrompt(…)` — `createCharacter`, `renameSession`, `savePreset`,
`translateCode`.

### Хелперы
- `api(path, opts)` — обёртка над `fetch` с авто-заголовками авторизации; вытаскивает
  реальный `detail` ошибки сервера (а не «HTTP 500»).
- `authHeaders()` — заголовки авторизации **без** `Content-Type` (для multipart-загрузок
  importChat/importCharacter).
- `renderMd(text)` — markdown-it + DOMPurify (защита от XSS).

## Layout и адаптивность

Сетка `.app-grid` держит только **чат | [канвас]** (`.with-canvas` → `1fr 1fr`). Обе
боковые панели — **выезжающие оверлеи**, не колонки:
- **Сайдбар** (список чатов) — `position: fixed`, открывается ☰ (`sidebarOpen`),
  закрывается кликом по `.backdrop` или после выбора чата. По умолчанию скрыт.
- **Настройки** (Drawer) — `position: fixed` справа, открывается ⚙. По умолчанию
  СКРЫТ (`drawerTab: null`).

На мобильном (≤760px) канвас даёт **вкладки**: на экране либо чат, либо канвас
(`mobilePane`=`chat|canvas`, класс `pane-*` прячет неактивную панель; кнопки 📋/💬).

**Единый инпут** (один на всё приложение, по центру внизу): авторесайз до
`max-height:200px` (`autoGrow`), Enter — отправка (десктоп) / перенос (тач,
`isTouch`=`pointer: coarse`); см. `onComposerKeydown`. Второстепенное (голос, арт)
спрятано в меню `[+]` (`plusMenu`) — на виду только 📎 и «Отправить». Команды Канвасу
идут через ЭТОТ ЖЕ инпут в режиме `canvasCmdMode` (как `artMode`), у канваса своего
поля ввода нет. **Drag&drop**: файл бросают в `.chat` → `onDrop`→`addFiles` (общий код
с 📎), подсветка `.drop-overlay`.

> Заметка по отладке в preview: вкладка превью троттлит рендер — CSS-переходы не
> анимируются, `requestAnimationFrame` зависает. Поведение, завязанное на transition
> (слайд сайдбара), в превью «не доезжает»; проверяйте с `transition: none`.

## Рендеринг и стилизация

Ответы модели — Markdown, рендерятся через `renderMd`. CSS пузырей оформлен «как в
Telegram»: жирный/курсив, инлайн-код и блоки кода, цитаты с левой полосой, списки,
ссылки акцентным цветом. Это согласуется с `STYLE_GUIDE` в системном промпте и с
`telegram_format.markdown_to_html`, чтобы один и тот же ответ хорошо смотрелся и в
приложении, и в Telegram.

## Реактивность: подводные камни

- Дескрипторы EventSource/WebSocket держим в обычных полях с префиксом `_`
  (`_bgJobs`, `_friendsTimer`), **не** в реактивном `data` — иначе Vue обернёт их в
  Proxy и сломает.
- После генерации клиент **перечитывает** историю с сервера (`loadMessages`) — он
  источник истины, локальное оптимистичное состояние затирается.
