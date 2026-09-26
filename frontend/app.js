/*
 * TaleEngine — фронтенд одной страницей (без сборки).
 *
 * Тонкий клиент: всё тяжёлое (LLM, память Horae, файлы) делает сервер. Здесь только
 * отображение и тонкая логика: WebSocket-стриминг токенов, рендер markdown, вызовы REST.
 *
 * Связь с сервером — с того же origin: REST по /api/..., стрим по /ws/chat/<id>.
 * Браузер НИКОГДА не ходит в LiteLLM напрямую — только сервер.
 */

// markdown-рендер с экранированием (защита от XSS через DOMPurify).
const md = window.markdownit({ breaks: true, linkify: true });

const { createApp } = Vue;

// Порядок единого списка чатов — тот же ключ, что у сервера (_sort_by_activity):
//   1. закреп — абсолютный приоритет: закреплённый чат никогда не опускается
//      ниже незакреплённого, как бы давно в нём ни писали;
//   2. внутри обеих групп — последняя активность, свежие выше. Активность
//      (activity_at) — последняя реплика либо создание чата, что позже: новый
//      пустой чат стоит наверху, а не под брошенными полгода назад;
//   3. ничья по секундам — id последней реплики (sortKey), затем id чата.
// Раньше клиент сливал личные чаты с группами и пересортировывал всё по одному
// last_id без закрепа вовсе: активный незакреплённый чат перепрыгивал пины.
//
// Это ЕДИНСТВЕННЫЙ компаратор чатов на клиенте: им упорядочены и сайдбар, и
// «Недавние диалоги» первого экрана, и чаты в палитре. Два порядка в двух местах
// рано или поздно разъезжаются, и тогда один и тот же чат стоит первым в одном
// списке и пятым в другом.
//
// Сортируем КОПИЮ: sort() меняет массив на месте, и вызов на реактивном
// источнике (allSessions, groups) из computed переставлял бы сами данные,
// заново будя все зависящие от них вычисления.
function sortChats(rows) {
  const ts = (v) => { const t = v ? Date.parse(v) : NaN; return Number.isFinite(t) ? t : 0; };
  return rows.slice().sort((a, b) =>
    (b.pinned ? 1 : 0) - (a.pinned ? 1 : 0)
    || ts(b.activity_at) - ts(a.activity_at)
    || (b.sortKey || 0) - (a.sortKey || 0)
    || (b.id || 0) - (a.id || 0));
}

// Локальная копия библиотеки вместо CDN. Сервер перечисляет файлы из
// frontend/vendor/ в <meta name="tale-vendor"> (см. serve_index в main.py) и сам
// подменяет адреса в index.html; здесь то же для того, что app.js грузит по
// надобности (KaTeX, lamejs). Раньше ради этого app.js правили прямо на
// сервере, и каждый git pull упирался в правку. Список читается один раз.
let vendorFiles = null;
function vendorUrl(url) {
  if (vendorFiles === null) {
    const meta = document.querySelector('meta[name="tale-vendor"]');
    vendorFiles = new Set(((meta && meta.content) || "").split(",").filter(Boolean));
  }
  const name = url.split("/").pop();
  return vendorFiles.has(name) ? "/vendor/" + name : url;
}

// Событие «список чатов изменился». Шлют его все мутации (создание, удаление,
// переименование, закреп, новая реплика), слушает одно место в mounted, которое
// перечитывает список — сайдбар больше не ждёт F5.
const CHATLIST_EVENT = "taleengine:chatlist-updated";
// Тот же сигнал между вкладками. Без него чат, созданный или закреплённый в
// одной вкладке, в соседней появлялся только после F5 — ровно та болезнь, от
// которой лечит CHATLIST_EVENT, только на уровень выше.
const CHATLIST_CHANNEL = "taleengine";
// Метка своей вкладки: сообщения канала, отправленные нами же, пропускаем.
// BroadcastChannel и так не доставляет их отправителю, но второй экземпляр
// канала в той же вкладке (горячая перезагрузка, расширение) получил бы эхо.
const TAB_ID = Math.random().toString(36).slice(2) + Date.now().toString(36);

// Параметры мастер-памяти, у которых есть умолчание сервера (MEMORY_* в .env):
// [ключ «ui», поле data(), ключ status().settings]. Такой ключ пишется в «ui»,
// только когда человек сам поменял поле (см. setMemPref): иначе первое же
// сохранение интерфейса записало бы умолчание клиента, и .env перестал бы
// действовать навсегда — «ui» на сервере важнее .env.
const MEM_UI_FIELDS = [
  ["memory_batch", "memoryBatch", "batch_size"],
  ["memory_delay_ms", "memoryDelayMs", "delay_ms"],
  ["memory_snapshot_tokens", "memorySnapshotTokens", "snapshot_tokens"],
];

createApp({
  data() {
    return {
      // --- Персонажи и чаты ---
      characters: [],
      selectedCharacterId: null,
      sessions: [],
      sessionId: null,
      messages: [],
      loadingOlder: false,      // идёт подгрузка старых сообщений (скролл вверх)
      noMoreMessages: false,    // старых сообщений больше нет (дошли до начала чата)
      messagePreload: 40,       // сколько сообщений грузить на открытии/за одну подгрузку (настройка админа)
      headerMenu: false,        // мобильное меню шапки (⋯)

      // --- Ввод и стриминг ---
      input: "",
      // Тач-устройство? На нём Enter переносит строку, а не отправляет (отправка — кнопкой).
      isTouch: (typeof window !== "undefined" && window.matchMedia)
        ? window.matchMedia("(pointer: coarse)").matches : false,
      // Какая панель видна на мобильном, когда открыт Канвас: 'chat' | 'canvas'.
      mobilePane: "chat",
      pendingAttachments: [],
      waitingFiles: false,      // отправка ждёт дочитывания вложений
      // Прогресс загрузки сообщения с файлами на сервер: null или
      // { percent (0..100 | null), loaded, total } — полоса над композером.
      uploadProgress: null,
      // Файл уже НА СЕРВЕРЕ, нейросеть получает/обрабатывает его (до первого токена).
      processingNote: false,
      plusMenu: false,          // выпадашка [+]: голос/арт (второстепенные действия)
      dragOver: false,          // подсветка зоны при перетаскивании файла
      lightbox: null,           // data:URI картинки для полноэкранного предпросмотра
      // --- Канвас (как в Gemini): документ/код рядом с чатом ---
      canvasOpen: false,
      canvas: null,             // { id, title, kind, language, content, can_undo }
      canvasInstruction: "",
      canvasBusy: false,
      canvasSel: { start: 0, end: 0 },  // выделение в редакторе (для точечной правки)
      toolbarPos: null,                 // позиция плавающего тулбара ({top,left}) или null
      // Режим композера. РОВНО ОДНО значение: text | art | canvasCmd | canvasGen.
      // Раньше это были три независимых булевых флага, которые включались
      // одновременно, показывали три одинаковые плашки и позволяли надписи на
      // кнопке отправки разойтись с реальным действием.
      composerMode: "text",
      canvasGenerating: false,          // идёт генерация документа в Канвас
      canvasView: "edit",               // 'edit' (редактор) | 'preview' (просмотр результата)
      copied: false,                    // флаг «код скопирован» для кнопки тулбара
      streaming: false,
      currentReply: "",
      currentThought: "",   // live-«размышления» модели (reasoning_content), в ответ не входят
      currentJobId: null,
      connected: false,
      ws: null,
      chatError: "", // последняя ошибка генерации (показываем, не прячем)

      // --- Запись аудио прямо в браузере ---
      recording: false,
      mediaRecorder: null,
      recChunks: [],

      // --- Редактирование сообщений ---
      editingId: null,
      editingText: "",

      // --- Правая панель ---
      drawerTab: null,         // generation | connection | character | memory | persona | null (по умолчанию СКРЫТ)
      // Счётчик для вкладки «Память» (панель Хроники, frontend/horae.js): растёт,
      // когда ход закончился или правили данные Horae под сообщением, — открытая
      // панель по нему перечитывает состояние. Сама панель про ход ничего не знает.
      horaeTick: 0,
      // Вкладка «Память» — панель Хроники из horae.js; свои разделы (сжатие
      // истории, лорбук) app.js рисует в её слотах. memoryTabReq — переход на
      // раздел извне ({ tab, n }, см. openMemory).
      memoryExtraTabs: [["compress", "Сжатие истории"], ["lore", "Лорбук"]],
      memoryTabReq: null,
      // Глобальный summary_enabled Хроники (GET /horae/settings): вместе с
      // autoSummary он задаёт, чем по умолчанию сжимается история (memoryEngine).
      // null — ещё не загружен.
      horaeGlobalSummary: null,
      // Грубый указатель (палец). Раскрытие действий строки по наведению на
      // таком устройстве не срабатывает НИКОГДА, поэтому там нужна не та же
      // разметка с другим оформлением, а другое раскрытие: одна кнопка вместо
      // пяти. Читается один раз при старте: тип указателя за сеанс не меняется.
      coarse: false,
      // Узкое окно (та же граница 768px, что у мобильной вёрстки в CSS). Решение
      // «прятать вторичные действия под ⋯» зависит и от пальца, и от ширины:
      // тип указателя сам по себе не знает, что десктопное окно сужено до
      // телефонного, и тогда девять кнопок уезжали бы в прокрутку мышью.
      narrow: false,
      rowMenu: null,           // ключ строки списка с раскрытыми действиями
      msgMenu: null,           // id сообщения с раскрытой строкой действий

      // --- Параметры генерации (вкладка Generation) ---
      params: {
        model: "",
        temperature: 0.9,
        top_p: 0.95,
        top_k: 40,
        max_tokens: 8192,       // длина ОДНОГО ОТВЕТА (вывод); рассуждения тратят его же
        repetition_penalty: 1.1,
        // Окно контекста («память»). 200к — под границей, за которой Gemini
        // тарифицирует ВЕСЬ вход вдвое дороже. Раньше здесь стоял 1 млн.
        context_tokens: 200000,
        history_files_mb: 8,       // файлы истории: сколько МБ вложений уходит модели за ход
        history_files_turns: 12,   // и из скольких ПОСЛЕДНИХ сообщений (0 = без ограничения)
        knowledge_chars: 60000,    // потолок текста базы знаний в контексте (0 = без лимита)
        // Стандартная фильтрация по умолчанию. Существующие профили не
        // затрагиваются: loadUiPrefs кладёт сохранённые параметры поверх.
        disable_safety: false,
        safety_preset: null,      // общий порог настраиваемых фильтров провайдера
        safety_overrides: {},     // точечные пороги по категориям (важнее пресета)
        send_avatars: false,
        web_access: false,
        assistant_mode: false,  // весь чат без отыгрыша (разовый аналог — ((…)) или /ooc)
        reasoning_effort: "",   // "" авто | disable | low | medium | high
        file_reasoning: true,   // авто-включать рассуждения при файлах
      },
      presets: [],
      presetName: "",

      // --- Подключение к LiteLLM (вкладка Connection) ---
      connection: { use_proxy: true, base_url: "http://localhost:4000", api_key: "", default_model: "gpt-4o", image_model: "", image_via_chat: false, fallback_model: "", auto_fallback: true, summary_model: "", embedding_model: "" },
      models: [],
      connStatus: "",
      connOk: null,

      // --- Редактор персонажа (вкладка Character) ---
      charEdit: null,

      // --- Память: лорбук и сжатие истории (вкладка Memory) ---
      horae: [],
      // Мастер-снимок: каждые summaryEvery сообщений ИИ дописывает в него то,
      // что вышло из окна. Ложь — снимок не обновляется (см. memoryEngine).
      autoSummary: true,
      summaryEvery: 10,         // каждые сколько сообщений обновлять сводку (это платный запрос)
      // Активное окно: столько последних сообщений идут в модель как есть
      // (0 — вся история). Значение совпадает с DEFAULT_WINDOW сервера (50).
      // Прежние 20 одним разом поднимает миграция в loadUiPrefs (флаг
      // memory_defaults_v), выбранное вручную потом не перетирается.
      memoryWindow: 50,
      horaeFacts: true,         // извлекать атомарные факты и подмешивать релевантные
      // Мастер-память чата (иерархическая пакетная): параметры ручной пересборки.
      // Глобальные, как остальные ключи памяти. Здесь — умолчания MEMORY_*
      // сервера до первого ответа статуса: пока человек поле не менял, оно
      // показывает status().settings, то есть то, с чем сервер работает
      // (значение из .env), и в «ui» не пишется (см. MEM_UI_FIELDS).
      memoryBatch: 20,          // сообщений в одном пакете сжатия (1–200)
      memoryDelayMs: 1500,      // пауза между запросами к модели сводки, мс (0–60000)
      memorySnapshotTokens: 12000, // бюджет мастер-снимка, токенов (1000–40000 в поле)
      // Статус памяти ОТКРЫТОГО чата (GET /sessions/{id}/memory) или null: ещё
      // не загружен, старый сервер без эндпоинта, нет доступа. Шаблон на null
      // показывает пустое состояние, а не падает на memStatus.snapshot.
      memStatus: null,
      memBusy: false,           // идёт запрос панели: первая загрузка, запуск, сброс
      // Сбои опроса статуса подряд во время задания (сеть, 5xx). С третьего
      // панель пишет «Нет связи с сервером» и реже опрашивает (_memPollDelay).
      memPollFails: 0,
      // Чат, чью память сейчас сбрасывают (DELETE в полёте), или null. Сброс
      // ждёт конца текущего пакета на сервере — это бывают минуты. Хранится
      // id, а не флаг: ушли в другой чат — там «Сбрасываю…» не к месту.
      memPurgingId: null,
      groupReplyDelay: 3,       // пауза (сек) между ответами персонажей в группе
      groupWaiting: 0,          // идёт пауза перед следующим ответом группы (сек)

      // --- Обход цензуры ---
      // Настраиваемые категории Gemini (совпадают с censorship.SAFETY_CATEGORIES).
      safetyCategories: [
        ["HARM_CATEGORY_HARASSMENT", "Домогательства и травля"],
        ["HARM_CATEGORY_HATE_SPEECH", "Ненависть и вражда"],
        ["HARM_CATEGORY_SEXUALLY_EXPLICIT", "Откровенный контент"],
        ["HARM_CATEGORY_DANGEROUS_CONTENT", "Опасный контент"],
        ["HARM_CATEGORY_CIVIC_INTEGRITY", "Гражданская добропорядочность"],
      ],
      // Общие инструкции перед ответом — один текст на все чаты + библиотека своих
      // пресетов. Сам текст пишет пользователь, готовых промптов тут нет.
      jailbreak: { enabled: false, text: "", presets: [] },
      jailbreakPresetName: "",

      // --- Расход токенов (кнопка 📊) ---
      usageOpen: false,
      usage: null,
      // Готовые наборы «сколько платим за ход». Меняют три настройки разом:
      // окно контекста, пересылку прежних файлов и объём базы знаний.
      economyModes: [
        { id: "eco", label: "🪙 Экономия", hint: "Минимум токенов на ход: короткая память, файлы только из свежих сообщений",
          v: { context_tokens: 64000, history_files_mb: 3, history_files_turns: 6, knowledge_chars: 20000 } },
        { id: "balance", label: "⚖️ Баланс", hint: "Рекомендуется: почти полная память, но без удвоенного тарифа и вечной пересылки файлов",
          v: { context_tokens: 200000, history_files_mb: 8, history_files_turns: 12, knowledge_chars: 60000 } },
        { id: "max", label: "🔥 Максимум", hint: "Прежнее поведение: помнит всё и пересылает все файлы каждый ход. Дорого!",
          v: { context_tokens: 1000000, history_files_mb: 0, history_files_turns: 0, knowledge_chars: 0 } },
      ],
      // Пустая форма записи памяти (тот же объект возвращает метод blankHorae()).
      horaeEdit: { id: null, category: "lore", title: "", content: "", keywords: "", always_on: false, enabled: true, priority: 0, scope: "global" },

      // --- Персоны и заметка автора (вкладка Persona) ---
      personas: [],
      personaNew: { name: "", description: "", avatar_path: null },
      authorNote: "",
      sessionPersonaId: null,
      // Часовой пояс ТЕКУЩЕГО чата (IANA-имя): нейросеть видит время пользователя,
      // а метки времени сообщений показываются в этом поясе.
      sessionTimezone: "",

      // --- Адаптив / мобильный режим ---
      // Сайдбар: на десктопе показан по умолчанию, на мобильном скрыт; ☰ слайдит.
      sidebarOpen: (typeof window !== "undefined" && window.matchMedia)
        ? window.matchMedia("(min-width: 761px)").matches : true,

      // --- Фон чата ---
      sessionBg: "",
      bgPicker: false,
      bgPresets: [
        { name: "Нет", value: "" },
        { name: "Ночь", value: "linear-gradient(160deg,#0f1020,#1a1530)" },
        { name: "Закат", value: "linear-gradient(160deg,#3a1c2b,#7a3b2e)" },
        { name: "Лес", value: "linear-gradient(160deg,#0e2018,#1d3b2a)" },
        { name: "Море", value: "linear-gradient(160deg,#0b2030,#15455c)" },
        { name: "Туман", value: "linear-gradient(160deg,#1c1f26,#2b313d)" },
      ],

      // --- Меню генерации арта ---
      artMenu: false,

      // --- Доступ / администрирование ---
      accessCode: "",
      adminPassword: "",
      authStatus: { access_required: false, admin_set: false },
      needAccess: false, // показывать экран ввода кода
      accessInput: "",
      accessError: "",
      adminOpen: false,
      adminAuthed: false,
      adminPassInput: "",
      adminSec: { access_code: "", admin_password: "", basic_auth: { enabled: false, username: "", password: "" } },
      adminTg: { token: "", enabled: false, open_to_all: false, model: "", default_character_id: null, whitelist: [], requests: [], bot_state: { running: false, error: "" } },
      adminUsers: [],
      newWlId: "",

      // --- Ответ на конкретное сообщение ---
      replyToId: null,

      // --- Звуковое уведомление ---
      soundOn: true,

      // --- Групповые чаты ---
      groups: [],
      groupModal: false,
      groupName: "Групповой чат",
      groupScenario: "",
      groupSelectedIds: [],
      groupInviteSelected: [],  // друзья, приглашаемые в группу при создании
      membersOpen: false,       // модалка «участники группы» (добавить/убрать)
      memberAddSelected: [],    // выбранные для добавления персонажи
      kbOpen: false,            // модалка «база знаний» чата
      kbFiles: [],              // список файлов базы знаний текущего чата
      kbUploading: false,       // идёт загрузка файла в базу знаний
      directorBar: false,       // показывать режиссёрскую панель (группа)
      // Аккордеон сайдбара: какие разделы раскрыты (по умолчанию — персонажи и чаты).
      openSections: { characters: true, chats: true, groups: false, shared: false },
      // Вежливый регион объявлений: скринридер узнаёт о ходе генерации. Объявляем
      // ЗАВЕРШЁННЫЙ ответ, а не каждый токен, иначе речь перезапускается десятки
      // раз в секунду и слушать её невозможно.
      liveStatus: "",
      // Недавние чаты первого экрана — теперь computed (см. recentChats): копия
      // среза allSessions, которую надо было не забыть обновить, отставала от
      // сайдбара и не знала ни о группах, ни о закрепе.

      // --- Приборы: телеметрия хода ---
      // Замеры делаются на КЛИЕНТЕ: токенизатор провайдера браузеру недоступен,
      // поэтому скорость считается по приросту символов и честно подписана оценкой.
      ctxStats: null,          // отчёт инспектора: веса блоков, обрезка, память
      ctxBusy: false,
      inspectorOpen: false,
      genStartAt: 0,           // момент отправки
      genFirstAt: 0,           // момент первого токена
      genElapsed: 0,           // секунд идёт ход
      genTps: 0,               // символов в секунду -> оценка токенов
      // Цена за миллион входных токенов. Ноль означает «не показывать»: выдумывать
      // тарифы за пользователя нельзя, у каждого прокси они свои.
      pricePerMTok: Number(localStorage.getItem("pricePerMTok") || 0),

      // --- Режим ленты ---
      // Одно поле, как composerMode: normal | scene | work.
      // Режим «Работа с документом» намеренно НЕ восстанавливается из хранилища:
      // канвас живёт на сервере, а canvasOpen и canvas на старте всегда false и
      // null. Восстановленный «work» включал бы ровно ту ложь, ради которой
      // переписан setViewMode: режим подсвечен, а экран неотличим от обычного.
      viewMode: localStorage.getItem("viewMode") === "scene" ? "scene" : "normal",
      // Три режима одной комнаты — списком в data, а не таблицей внутри строкового
      // шаблона: по нему идёт v-for сегмента в шапке, и рядом с самим viewMode
      // видно, из каких ровно трёх значений это поле состоит.
      viewModes: [
        { id: "normal", icon: "💬", label: "Обычный", hint: "реплики как переписка" },
        { id: "scene", icon: "🎭", label: "Сцена", hint: "фон истории, имена янтарём" },
        { id: "work", icon: "📄", label: "Работа с документом", hint: "лента 42%, канвас 58%" },
      ],

      // ---------- Раскладка оболочки ----------
      // ВТОРАЯ ось, не путать с viewMode. viewMode это свойство ЛЕНТЫ: как
      // выглядят реплики. shellLayout — свойство ОБОЛОЧКИ: как расставлено
      // вокруг ленты всё остальное.
      //
      // Держим их раздельно намеренно. Продукт задуман как студия, которую
      // ролевик обустраивает под себя, и обустраивать он должен комнату, а не
      // выбирать между девятью безымянными сочетаниями. Поэтому раскладка —
      // редкая настройка «как у меня стоит мебель», а режим ленты остаётся
      // частым переключением по ходу истории.
      shellLayout: localStorage.getItem("shellLayout") || "modes",
      shellLayouts: [
        {
          id: "modes",
          icon: "🎛",
          label: "Комнаты",
          hint: "режим ленты поднят в первичную навигацию: сегмент сверху виден всегда",
        },
        {
          id: "document",
          icon: "📖",
          label: "Страница",
          hint: "контролы уходят на поля во время чтения и возвращаются по намерению",
        },
        {
          id: "name",
          icon: "⌘",
          label: "По имени",
          hint: "постоянного списка нет, всё вызывается строкой поиска и команд",
        },
      ],

      // --- Единый список чатов ---
      // Все чаты пользователя, а не только выбранного персонажа. Раньше раздел
      // «Чаты» существовал лишь при выбранном персонаже, и чтобы найти чат, надо
      // было сначала вспомнить персонажа — то есть вспомнить ответ до вопроса.
      allSessions: [],
      chatFilterChar: null,   // чип фильтра: id персонажа либо null («все»)

      // --- Командная палитра ---
      paletteOpen: false,
      paletteQuery: "",
      paletteIndex: 0,
      searchResults: [],
      searchBusy: false,
      // Команды палитры. Статический список: хранить здесь функции нельзя,
      // Vue обернул бы их в Proxy — диспетчеризация идёт по id в paletteRun.
      paletteCommands: [
        { id: "new", label: "Новый диалог", sub: "создать чат" },
        { id: "model", label: "/model — сменить модель", sub: "настройки генерации" },
        { id: "branch", label: "/branch — ветка от последней реплики", sub: "форк чата" },
        { id: "export", label: "/export — выгрузить чат", sub: "нативный формат" },
        { id: "settings", label: "/settings — настройки", sub: "панель параметров" },
        { id: "clear", label: "/clear — очистить поле ввода", sub: "сбросить режим композера" },
        // Режимы ленты. Палитра объявлена единым входом ко всему, но три главных
        // состояния приложения в неё не входили: добраться до них можно было
        // только мышью и только через безымянное «⋯».
        { id: "view-normal", label: "Режим: обычный", sub: "реплики как переписка" },
        { id: "view-scene", label: "Режим: сцена", sub: "фон истории, имена янтарём" },
        { id: "view-work", label: "Режим: работа с документом", sub: "лента 42%, канвас 58%" },
        // Раскладка — редкая настройка, и в «⋯» она лежала бы мёртвым грузом
        // рядом с двенадцатью другими пунктами. В палитре её находят по имени.
        { id: "shell-modes", label: "Раскладка: комнаты", sub: "сегмент режимов сверху" },
        { id: "shell-document", label: "Раскладка: страница", sub: "контролы на полях, лента как текст" },
        { id: "shell-name", label: "Раскладка: по имени", sub: "без постоянного списка" },
      ],
      groupDirector: false,
      liveBubbles: [], // живые пузыри разных персонажей при стриминге группы

      // --- Аккаунты ---
      userToken: "",
      currentUserObj: null,
      needAuth: false,        // показывать экран входа/регистрации (режим аккаунтов)
      authTab: "login",       // login | register
      authForm: { username: "", password: "" },
      // Подтверждение пароля держим ОТДЕЛЬНО от authForm: authForm уходит на
      // сервер целиком (JSON.stringify), и подтверждению там делать нечего.
      authPassword2: "",
      authPassShown: false,   // показать пароль текстом — обе строки разом
      friends: [],
      friendsIncoming: [],
      newFriendName: "",
      inviteOpen: false,    // модалка приглашения друзей в чат
      inviteSessionId: null,
      inviteSelected: [],   // логины выбранных друзей
      sharedSessions: [],   // чаты, которыми со мной поделились
      sharedView: null,     // открытый сейчас «чужой» чат (из раздела «Доступные мне»)
      notifOpen: false,     // выпадашка уведомлений
      pendingChats: [],     // id чатов, куда пришёл ответ, пока вы были в другом чате
      toasts: [],           // всплывающие уведомления (тосты) в правом нижнем углу
      katexReady: false,    // KaTeX догрузился лениво -> перерисовать формулы
      chatFilter: "",       // строка поиска по чатам (появляется, когда их много)
      charFilter: "",       // то же по персонажам
      awayFromBottom: false, // отъехали от низа -> показать навигацию по чату
      jumpBusy: false,      // идёт дозагрузка истории для прыжка в начало
      composerH: 90,        // высота поля ввода: над ним висит панель навигации
      dialog: null,         // модальный диалог (подтверждение/ввод) вместо браузерных alert/prompt/confirm

      // --- Отладочный лог LLM ---
      debugOpen: false,
      debugEntries: [],
      envLocked: {},          // какие секреты приходят из переменных окружения
      // Панель отладки показывает ТОЛЬКО свои ходы. Раньше буфер был общим на
      // всё приложение, и в режиме аккаунтов туда попадала чужая переписка.
      debugAll: false,        // админ может посмотреть общий системный лог
      debugCanSeeAll: false,  // роль приходит с сервера, клиент её не решает

      // --- Профиль (привязка Telegram) ---
      profileOpen: false,
      linkCode: "",
    };
  },

  computed: {
    selectedCharacter() {
      return this.characters.find((c) => c.id === this.selectedCharacterId) || null;
    },
    // Поиск по списку: ищем и в названии, и в последней реплике — по чату
    // «Новый чат #7» вспомнить нечего, а по обрывку разговора вспоминается сразу.
    filteredSessions() {
      const q = this.chatFilter.trim().toLowerCase();
      if (!q) return this.sessions;
      return this.sessions.filter((s) =>
        (s.title || "").toLowerCase().includes(q) ||
        (s.preview || "").toLowerCase().includes(q));
    },
    filteredCharacters() {
      const q = this.charFilter.trim().toLowerCase();
      if (!q) return this.characters;
      return this.characters.filter((c) => (c.name || "").toLowerCase().includes(q));
    },
    // Ловушка, которую пользователь сам не свяжет: у Gemini режим размышлений
    // включает СВОЮ модерацию поверх safety_settings и душит контент даже при
    // пороге OFF. Авто-включение мы блокируем, но ручной выбор уважаем — значит
    // про конфликт надо предупредить прямо в настройках (см. backend/censorship.py).
    reasoningConflict() {
      const r = (this.params.reasoning_effort || "").toLowerCase();
      return this.params.disable_safety && r !== "" && r !== "auto" && r !== "disable";
    },
    // Открытая сейчас сессия из списка чатов (для заголовка «имя чата · #номер»).
    currentSession() {
      return this.sessions.find((s) => s.id === this.sessionId) || null;
    },
    // Полное имя открытого чата (обычный / группа / расшаренный).
    currentSessionTitle() {
      if (this.sharedView) return this.sharedView.title || "";
      if (this.currentIsGroup) return this.currentGroup.title || "";
      return this.currentSession ? (this.currentSession.title || "") : "";
    },
    // Запасная модель из настроек подключения (для баннера ошибки и ретрая).
    fallbackModel() {
      return ((this.connection && this.connection.fallback_model) || "").trim();
    },
    // Аватар в шапке чата: персонаж; для группы — первый участник с аватаркой.
    headerAvatar() {
      if (this.sharedView) return this.sharedView.character_avatar || "";
      if (this.currentIsGroup) {
        const withAva = (this.currentGroup.members || []).find((m) => m.avatar_path);
        return withAva ? withAva.avatar_path : "";
      }
      return (this.selectedCharacter && this.selectedCharacter.avatar_path) || "";
    },
    // Список часовых поясов для настройки чата (браузер знает полный список IANA).
    tzOptions() {
      try {
        if (Intl.supportedValuesOf) return Intl.supportedValuesOf("timeZone");
      } catch (e) {}
      return ["UTC", "Europe/Moscow", "Europe/Kaliningrad", "Europe/Samara",
              "Asia/Yekaterinburg", "Asia/Omsk", "Asia/Krasnoyarsk", "Asia/Irkutsk",
              "Asia/Yakutsk", "Asia/Vladivostok", "Asia/Magadan", "Asia/Kamchatka",
              "Europe/Kyiv", "Europe/Minsk", "Asia/Almaty", "Asia/Tashkent"];
    },
    lastAssistantId() {
      const a = [...this.messages].reverse().find((m) => m.role === "assistant");
      return a ? a.id : null;
    },
    chatBgStyle() {
      const bg = this.sessionBg;
      if (!bg) return {};
      if (bg.startsWith("data:") || bg.startsWith("http") || bg.startsWith("/")) {
        return { backgroundImage: 'url("' + bg + '")', backgroundSize: "cover", backgroundPosition: "center" };
      }
      return { background: bg }; // CSS-градиент/цвет
    },
    chatImages() {
      // Картинки, уже сгенерированные в этом чате (для установки на фон).
      const re = /!\[[^\]]*\]\(([^)]+)\)/g;
      const urls = [];
      for (const m of this.messages) {
        let match;
        while ((match = re.exec(m.content || "")) !== null) urls.push(match[1]);
      }
      return urls;
    },
    currentGroup() {
      return this.groups.find((g) => g.id === this.sessionId) || null;
    },
    currentIsGroup() {
      return !!this.currentGroup;
    },
    isAdmin() {
      // В режиме аккаунтов админ определяется ролью; в режиме кода доступа админка
      // открыта (гейт по паролю администратора при открытии).
      if (this.authStatus.accounts_enabled) {
        return !!(this.currentUserObj && this.currentUserObj.role === "admin");
      }
      return true;
    },
    replyToMsg() {
      return this.messages.find((m) => m.id === this.replyToId) || null;
    },
    canvasSelText() {
      if (!this.canvas || this.canvasSel.end <= this.canvasSel.start) return "";
      return (this.canvas.content || "").slice(this.canvasSel.start, this.canvasSel.end);
    },
    // Хоть одно вложение ещё читается (спиннер) — отправку задерживаем до готовности.
    attachmentsLoading() {
      return this.pendingAttachments.some((a) => a.loading);
    },
    // Размер порции сообщений (предзагрузка) — из настройки, в разумных пределах.
    msgPageSize() {
      return Math.min(400, Math.max(10, Math.round(Number(this.messagePreload) || 40)));
    },
    // Последний ход остался без ответа (ошибка/обрыв/ручная остановка) — можно повторить.
    canRetry() {
      if (this.streaming || !this.messages.length) return false;
      const last = this.messages[this.messages.length - 1];
      return last.role === "user" && last.id !== "tmp";
    },
    // Веб-приложение (HTML/CSS/JS/React) — для него доступен live-предпросмотр в iframe.
    canvasIsWeb() {
      if (!this.canvas || this.canvas.kind !== "code") return false;
      const c = this.canvas.content || "";
      const low = c.toLowerCase();
      return low.includes("<!doctype html") || low.includes("<html") || low.includes("<body")
        || (low.includes("<div") && (low.includes("<script") || low.includes("<style")))
        || /\b(import\s+react|from\s+['"]react['"]|reactdom|react\.)/i.test(c)
        || (/export\s+default/.test(c) && /<[A-Z][A-Za-z0-9]*[\s/>]/.test(c)); // JSX-компонент
    },
    // HTML для iframe-предпросмотра: полный HTML — как есть; React/JSX — оборачиваем в
    // React+Babel (CDN) и рендерим компонент App; фрагмент/CSS/JS — оборачиваем в страницу.
    previewSrcdoc() {
      const code = this.canvas ? (this.canvas.content || "") : "";
      const low = code.toLowerCase();
      if (low.includes("<!doctype") || low.includes("<html")) return code;
      const isReact = /\b(import\s+react|from\s+['"]react['"]|reactdom|react\.)/i.test(code)
        || (/export\s+default/.test(code) && /<[A-Z][A-Za-z0-9]*[\s/>]/.test(code));
      if (isReact) {
        const cleaned = code
          .replace(/^\s*import[^\n]*\n/gm, "")
          .replace(/export\s+default\s+function/g, "function")
          .replace(/export\s+default\s+/g, "const __default = ");
        return '<!DOCTYPE html><html><head><meta charset="utf-8">'
          + '<script src="https://unpkg.com/react@18/umd/react.production.min.js"></scr' + 'ipt>'
          + '<script src="https://unpkg.com/react-dom@18/umd/react-dom.production.min.js"></scr' + 'ipt>'
          + '<script src="https://unpkg.com/@babel/standalone/babel.min.js"></scr' + 'ipt>'
          + '<style>body{font-family:system-ui;margin:0;padding:14px;color:#111}</style></head>'
          + '<body><div id="root"></div><script type="text/babel">\n' + cleaned
          + '\n;(function(){try{var C=(typeof App!=="undefined"&&App)||(typeof __default!=="undefined"&&__default);'
          + 'if(C){ReactDOM.createRoot(document.getElementById("root")).render(React.createElement(C));}'
          + 'else{document.getElementById("root").innerHTML="<i>Нет компонента App для предпросмотра</i>";}}'
          + 'catch(e){document.body.innerHTML="<pre style=\\"color:#c00;white-space:pre-wrap\\">"+e+"</pre>";}})();'
          + '\n</scr' + 'ipt></body></html>';
      }
      // HTML-фрагмент / CSS / JS — простая страница.
      return '<!DOCTYPE html><html><head><meta charset="utf-8">'
        + '<style>body{font-family:system-ui;margin:0;padding:14px;color:#111}</style></head>'
        + '<body>' + code + '</body></html>';
    },
    composerPlaceholder() {
      if (this.composerMode === "canvasCmd") return "Что сделать с Canvas… (Esc — обычное сообщение)";
      if (this.composerMode === "canvasGen") return "Опишите документ или код… (Esc — обычное сообщение)";
      if (this.composerMode === "art") return "Опишите картинку… (Esc — обычное сообщение)";
      if (this.currentIsGroup) return "Сообщение… Режиссура: +Имя вызвать, -Имя исключить (🎬)";
      return this.isTouch
        ? "Сообщение… (Enter — перенос строки)"
        : "Сообщение… (Enter — отправить, Shift+Enter — перенос)";
    },
    // Открыт ли хоть какой-то модальный слой. Один признак на все окна:
    // по нему работают и удержание фокуса, и его возврат.
    overlayOpen() {
      return !!(this.dialog || this.lightbox || this.adminOpen || this.kbOpen
        || this.membersOpen || this.groupModal || this.inviteOpen || this.profileOpen
        || this.debugOpen || this.usageOpen || this.drawerTab || this.paletteOpen
        || this.inspectorOpen);
    },

    // ---------- Шапка ----------
    headerTitle() {
      if (this.sharedView) return "🔗 " + this.sharedView.title;
      if (!this.sessionId) return "TaleEngine";
      return this.currentSessionTitle || "Чат";
    },
    // Вторая строка: кто в чате и его номер. Раньше это были три отдельные
    // пилюли, две из которых повторяли заголовок.
    headerSubtitle() {
      if (!this.sessionId) return "";
      const who = this.currentIsGroup
        ? (this.currentGroup ? this.currentGroup.members.map((m) => m.name).join(", ") : "группа")
        : (this.selectedCharacter ? this.selectedCharacter.name : "");
      return (who ? who + " · " : "") + "#" + this.sessionId;
    },

    // ---------- Единый список чатов ----------
    // Личные, групповые и расшаренные в одной ленте с признаком вида. Порядок —
    // sortChats: закреп выше всего, затем последняя активность (activity_at
    // приходит с сервера у всех трёх видов). Закреп расшаренного чата — дело его
    // владельца, у меня такой чат всегда среди незакреплённых.
    unifiedChats() {
      const rows = [];
      for (const s of this.allSessions) {
        rows.push({ ...s, kind: "chat", sortKey: s.last_id || 0 });
      }
      for (const g of this.groups) {
        rows.push({
          ...g, kind: "group", sortKey: g.last_id || 0,
          character_name: (g.members || []).map((m) => m.name).join(", "),
        });
      }
      for (const s of this.sharedSessions) {
        rows.push({ ...s, kind: "shared", pinned: false, sortKey: s.last_id || 0 });
      }
      return sortChats(rows);
    },
    // «Недавние диалоги» первого экрана — голова того же единого списка, а не
    // отдельный срез личных чатов: порядок и закреп здесь ровно как в сайдбаре.
    recentChats() {
      return this.unifiedChats.slice(0, 5);
    },
    // Сокращать строку действий под сообщением до четырёх главных и «⋯»: на
    // пальце (наведения нет, каждая цель 44px) и в узком окне (места нет).
    // Широкий планшет — палец, значит сокращаем; узкое окно с мышью — места нет,
    // значит тоже. Остаётся только широкий экран с мышью: там строка видна
    // целиком и проявляется по наведению (см. @media (hover: hover) в CSS).
    compactActions() {
      return this.coarse || this.narrow;
    },
    // Чипы фильтра: только персонажи, у которых чаты реально есть.
    chatFilterChips() {
      const seen = new Map();
      for (const s of this.allSessions) {
        if (!s.character_id) continue;
        if (!seen.has(s.character_id)) {
          seen.set(s.character_id, { id: s.character_id, name: s.character_name || "Без имени", n: 0 });
        }
        seen.get(s.character_id).n += 1;
      }
      return Array.from(seen.values()).sort((a, b) => b.n - a.n);
    },
    visibleChats() {
      const q = (this.chatFilter || "").trim().toLowerCase();
      return this.unifiedChats.filter((s) => {
        if (this.chatFilterChar !== null && s.character_id !== this.chatFilterChar) return false;
        if (!q) return true;
        return (s.title || "").toLowerCase().includes(q)
          || (s.preview || "").toLowerCase().includes(q)
          || (s.character_name || "").toLowerCase().includes(q);
      });
    },

    // ---------- Приборы ----------
    // Доля занятого окна контекста. Считается по отчёту инспектора, то есть по
    // тем же числам, что уйдут в модель, а не по отдельной оценке рядом.
    ctxFill() {
      if (!this.ctxStats || !this.ctxStats.budget) return 0;
      return Math.min(100, Math.round((this.ctxStats.total_tokens / this.ctxStats.budget) * 100));
    },
    ctxLevel() {
      const p = this.ctxFill;
      return p >= 90 ? "crit" : p >= 70 ? "warn" : "ok";
    },
    // Монитор токенов: три яруса хода из отчёта инспектора (ctxStats.tiers).
    // Старый сервер их не присылает — тогда null, и монитор просто не рисуется.
    memTiers() {
      const t = this.ctxStats && this.ctxStats.tiers;
      return t && typeof t === "object" ? t : null;
    },
    // Группа: сервер ярусы не считает (tiers: null, tiers_unavailable: "group")
    // — групповой ход собирается иначе, и окна у него нет. Без этого признака
    // монитор просто пропадал бы, и пустое место читалось бы как сбой.
    memTiersGroup() {
      return !!this.ctxStats && this.ctxStats.tiers_unavailable === "group";
    },
    // Сегменты полосы и подписи к ним одним списком: вкладка «Память» и
    // инспектор рисуют по нему одну и ту же разбивку, и подписи не разъедутся.
    memTierRows() {
      const t = this.memTiers;
      if (!t) return [];
      const k = t.window_messages;
      const win = typeof k === "number"
        ? "Окно (" + this.fmtNum(k) + " " + this.plural(k, "сообщение", "сообщения", "сообщений") + ")"
        : "Окно";
      return [
        { cls: "seg-guides", label: "Системный промпт и якоря", tokens: t.system },
        { cls: "seg-horae", label: "Память: снимок, Хроника, факты", tokens: t.memory },
        { cls: "seg-history", label: win, tokens: t.window },
      ].map((r) => ({ ...r, pct: this.sharePct(r.tokens, t.total) }));
    },
    // Задание памяти идёт или ждёт очереди: показываем прогресс, опрашиваем
    // статус и не даём запустить второе.
    memJobActive() {
      const job = this.memStatus && this.memStatus.job;
      return !!job && (job.status === "running" || job.status === "queued");
    },
    // Строка прогресса: строка сервера «[Обработано 140/800 …]» и рядом — с
    // какого времени задание идёт. У задания в очереди строки сервера ещё нет,
    // и время его постановки ничего не говорит: вместо них — «В очереди».
    memJobLine() {
      const job = this.memStatus && this.memStatus.job;
      if (!job) return "";
      let since = "";
      if (job.status === "queued") since = "в очереди";
      else if (this.fmtClock(job.started_at)) since = "идёт с " + this.fmtClock(job.started_at);
      const s = [job.line, since].filter(Boolean).join(" · ");
      if (s) return s.charAt(0).toUpperCase() + s.slice(1);
      // Ни строки, ни времени (сервер их не прислал): пустая строка под
      // полосой выглядела бы как зависание.
      return job.status === "running" ? "Идёт…" : "";
    },
    // Под строкой прогресса (спека §9): «Пакет 8 · сбой API — повтор через 3 с».
    // batches — сколько пакетов уже ЗАПИСАНО. Пока модель сворачивает пакет
    // (merge, его повтор retry, сжатие хроники compact внутри того же
    // пакета), записано на один меньше: раньше строка всё это время называла
    // предыдущий пакет, а у первого номера не было вовсе. Поэтому в работе —
    // batches + 1. Пауза wait стоит МЕЖДУ запросами — там честно «после
    // пакета N» (записанного); после пауз и в прочих фазах — просто «Пакет N»,
    // но без «Пакета 0»: он читается как сбой. Без фазы и номера строки нет
    // вовсе — лишняя пустая строка в живом регионе ни к чему.
    memJobPhase() {
      const job = this.memStatus && this.memStatus.job;
      if (!job || job.status !== "running") return "";
      const parts = [];
      const n = Number(job.batches) || 0;
      const working = job.phase === "merge" || job.phase === "retry" || job.phase === "compact";
      if (working) parts.push("Пакет " + this.fmtNum(n + 1));
      else if (job.phase === "wait") { if (n > 0) parts.push("после пакета " + this.fmtNum(n)); }
      else if (n > 0) parts.push("Пакет " + this.fmtNum(n));
      if (job.phase === "wait") parts.push("пауза между запросами");
      else if (job.phase === "compact") parts.push("сжатие хроники");
      else if (job.phase === "retry") {
        // retry_in_s — float (2.4817 с): вверх до целых, чтобы не обещать
        // повтор раньше, чем он будет. Числа нет — просто «повторяю».
        const s = Number(job.retry_in_s);
        parts.push(job.retry_in_s != null && s > 0
          ? "сбой API — повтор через " + Math.ceil(s) + " с"
          : "сбой API — повторяю…");
      }
      const s = parts.join(" · ");
      return s ? s.charAt(0).toUpperCase() + s.slice(1) : "";
    },
    // Сброс памяти ОТКРЫТОГО чата ещё ждёт ответа сервера.
    memPurging() {
      return this.memPurgingId != null && this.memPurgingId === this.sessionId;
    },
    // Чем по умолчанию сжимается старая история (выбор в «Память» → «Сжатие
    // истории»): свёртки Хроники, мастер-снимок или ничто. Работает ровно один
    // механизм — сервер решает это для каждого чата (horae_engine.compression_engine).
    memoryEngine() {
      if (this.horaeGlobalSummary) return "horae";
      return this.autoSummary ? "snapshot" : "off";
    },
    // Выбор меняет глобальные настройки Хроники, а их пишет только
    // администратор: не-администратору свёртки Хроники недоступны, а если они
    // уже включены — сменить выбор он не может вовсе.
    memoryEngineLocked() { return !this.isAdmin && !!this.horaeGlobalSummary; },
    // Кто сжимает историю ОТКРЫТОГО чата (статус памяти → compression) или null.
    chatEngine() {
      const c = this.memStatus && this.memStatus.compression;
      return c ? c.engine : null;
    },
    // Чат живёт не по общему выбору (свои настройки Хроники у чата или
    // персонажа, Хроника в чате выключена) — говорим прямо, иначе выбор выше
    // выглядел бы несработавшим.
    chatEngineNote() {
      const c = this.memStatus && this.memStatus.compression;
      if (!c || this.horaeGlobalSummary === null || c.engine === this.memoryEngine) return "";
      const name = { snapshot: "мастер-снимок", horae: "свёртки Хроники", off: "ничто — сжатие выключено" }[c.engine] || c.engine;
      const why = c.summary_layer === "chat" ? " — так выбрано в настройках Хроники этого чата"
        : c.summary_layer === "character" ? " — так выбрано в профиле Хроники персонажа" : "";
      return "В этом чате историю сжимает " + name + why + ".";
    },
    // Предупреждения памяти под строкой статуса: снимка (snapshot.warnings —
    // «превышает бюджет», «лимит вывода модели меньше бюджета») и
    // завершённого задания (job.warnings — «пересборка не нашла сообщений
    // старше окна — прежний снимок оставлен», «модель потеряла N записей»).
    // Раньше панель их выбрасывала, и пустая пересборка выглядела как
    // стёртая память. У идущего задания список ещё растёт — показываем по
    // завершении. Не больше 5 строк: длинный список вытеснил бы кнопки.
    memWarnings() {
      const st = this.memStatus;
      if (!st) return [];
      const list = (x) => (Array.isArray(x) ? x : [])
        .filter((w) => typeof w === "string" && w.trim()).map((w) => w.trim());
      const job = st.job;
      const finished = !!job && job.status !== "running" && job.status !== "queued";
      const all = [...new Set([...list(st.snapshot && st.snapshot.warnings),
        ...(finished ? list(job.warnings) : [])])]
        .map((w) => "⚠ " + w.charAt(0).toUpperCase() + w.slice(1));
      if (all.length <= 5) return all;
      const rest = all.length - 4;
      return [...all.slice(0, 4),
        "…и ещё " + rest + " " + this.plural(rest, "предупреждение", "предупреждения", "предупреждений")];
    },
    // Сбой ежеходного обновления памяти (snapshot.last_error). Сервер после
    // него пропускает проходы до retry_after, чтобы не жечь запросы на каждом
    // ходу, — и без этой строки память молча замерзала бы. Пока идёт ручное
    // задание, строка не к месту: оно само и есть следующая попытка.
    memLastError() {
      const st = this.memStatus;
      const err = st && st.snapshot && st.snapshot.last_error;
      if (!err || typeof err !== "object" || this.memJobActive) return "";
      const msg = typeof err.message === "string" ? err.message.trim().replace(/[.\s]+$/, "") : "";
      // Проход запускает ход чата, а не таймер: после retry_after попытка будет
      // с первым ходом, поэтому «после HH:MM». Время прошло или его нет — пауза
      // кончилась, повторит ближайший ход.
      const at = st.snapshot.retry_after ? new Date(st.snapshot.retry_after) : null;
      const when = at && !isNaN(at) && at.getTime() > Date.now()
        ? "после " + this.fmtClock(st.snapshot.retry_after) : "со следующим ходом";
      return "⚠ Память не обновилась" + (msg ? ": " + msg : "") + ". Следующая попытка — "
        + when + " или кнопкой «Догнать».";
    },
    // Бюджет снимка больше, чем модель памяти может выдать за один ответ
    // (settings.max_snapshot_tokens считает сервер по лимиту вывода модели;
    // null — лимит неизвестен). Снимок переписывается целиком, и такой бюджет
    // обрывал бы каждое обновление посреди текста.
    memSnapCap() {
      const s = this.memStatus && this.memStatus.settings;
      const cap = s ? Number(s.max_snapshot_tokens) : NaN;
      if (!s || s.max_snapshot_tokens == null || !(cap > 0)) return "";
      if (cap >= Number(this.memorySnapshotTokens)) return "";
      return "⚠ Модель памяти выдаёт не больше ≈" + this.fmtNum(cap) + " токенов снимка — уменьшите бюджет";
    },
    // Стоимость показываем ТОЛЬКО если пользователь задал свой тариф: у каждого
    // прокси он свой, и выдуманное число здесь хуже отсутствующего.
    ctxCost() {
      if (!this.pricePerMTok || !this.ctxStats) return "";
      const usd = (this.ctxStats.total_tokens / 1e6) * this.pricePerMTok;
      return usd < 0.01 ? "<0.01" : usd.toFixed(2);
    },
    fmtCtx() {
      if (!this.ctxStats) return "";
      const t = this.ctxStats.total_tokens;
      return t >= 1000 ? Math.round(t / 1000) + "к" : String(t);
    },

    // ---------- Командная палитра ----------
    paletteItems() {
      const raw = this.paletteQuery.trim();
      const q = raw.replace(/^\//, "");
      const out = [];
      const add = (kind, item, hay) => {
        const score = this._fuzzy(hay, q);
        if (score >= 0) out.push({ ...item, kind, score });
      };
      for (const c of this.paletteCommands) {
        add("cmd", { id: c.id, label: c.label, sub: c.sub }, c.label + " " + c.id);
      }
      // Слэш означает «только команды»: иначе выдача тонет в чатах.
      if (!raw.startsWith("/")) {
        for (const s of this.unifiedChats.slice(0, 300)) {
          add("chat", { id: s.id, label: s.title || "Чат", sub: s.character_name || "чат", row: s },
            (s.title || "") + " " + (s.character_name || ""));
        }
        for (const c of this.characters) {
          add("char", { id: c.id, label: c.name, sub: "персонаж" }, c.name);
        }
      }
      out.sort((a, b) => a.score - b.score);
      const head = out.slice(0, 10);
      // Найденные реплики идут отдельным блоком после команд и чатов: они
      // приходят с сервера асинхронно и не участвуют в нечётком ранжировании.
      for (const r of this.searchResults.slice(0, 10)) {
        head.push({
          kind: "msg", id: r.message_id, sid: r.session_id,
          label: r.session_title || "Чат", sub: r.snippet, score: 0,
        });
      }
      return head;
    },
  },

  methods: {
    // ---------- Общий REST-помощник ----------
    async api(path, opts = {}) {
      const headers = { "Content-Type": "application/json", ...(opts.headers || {}) };
      if (this.accessCode) headers["X-Access-Code"] = this.accessCode;
      if (this.adminPassword) headers["X-Admin-Password"] = this.adminPassword;
      if (this.userToken) headers["X-User-Token"] = this.userToken;
      const res = await fetch("/api" + path, { ...opts, headers });
      if (res.status === 401) this.needAccess = true; // код доступа изменился
      if (!res.ok) {
        // Достаём реальный текст ошибки сервера (а не просто код).
        let detail = "HTTP " + res.status;
        try { const j = await res.json(); if (j && j.detail) detail = j.detail; } catch (e) {}
        // Код ответа — рядом с текстом: вызывающему иногда важно отличить
        // «чата больше нет» (403/404) от сбоя сети, а по тексту это ненадёжно.
        const err = new Error(detail);
        err.status = res.status;
        throw err;
      }
      return res.status === 204 ? null : res.json();
    },

    // ---------- Ленивая подгрузка тяжёлых библиотек ----------
    // KaTeX и lamejs вместе весят больше всего остального фронтенда, а нужны
    // редко: формулы встречаются не в каждом чате, запись голоса — тем более.
    // Раньше они висели в <head> и их ждал КАЖДЫЙ заход, включая мобильный.
    _loadOnce(key, urls) {
      this._loading = this._loading || {};
      if (this._loading[key]) return this._loading[key];
      this._loading[key] = Promise.all(urls.map((cdn) => new Promise((resolve, reject) => {
        const url = vendorUrl(cdn);
        let el;
        if (url.endsWith(".css")) {
          el = document.createElement("link");
          el.rel = "stylesheet";
          el.href = url;
        } else {
          el = document.createElement("script");
          el.src = url;
        }
        el.onload = resolve;
        el.onerror = reject;
        document.head.appendChild(el);
      })));
      return this._loading[key];
    },
    ensureKatex() {
      if (window.katex) return Promise.resolve();
      return this._loadOnce("katex", [
        "https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/katex.min.css",
        "https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/katex.min.js",
      ]).then(() => {
        // Флаг реактивный: renderMd на него подписан, поэтому уже показанные
        // сообщения перерисуются сами, и формула проявится без перезагрузки.
        this.katexReady = true;
      }).catch(() => {});
    },
    ensureLame() {
      if (window.lamejs) return Promise.resolve();
      return this._loadOnce("lame", [
        "https://cdn.jsdelivr.net/npm/lamejs@1.2.1/lame.min.js",
      ]).catch(() => {});
    },

    renderMd(text) {
      // Подписка на флаг: когда KaTeX догрузится, Vue перерисует сообщения.
      void this.katexReady;
      // Формула в тексте есть, а библиотеки ещё нет — тянем её сейчас.
      if (!window.katex && /\$\$|\\\[|\\\(|\$[^$\n]+\$/.test(text || "")) {
        this.ensureKatex();
      }
      // LaTeX: формулы вырезаются ДО markdown-it (иначе он «съедает» \( \[ и **),
      // рендерятся KaTeX'ом и подставляются обратно уже готовым HTML.
      const math = [];
      const protectedText = this._extractMath(text || "", math);
      let html = md.render(protectedText);
      if (math.length) {
        html = html.replace(/%%MATH-(\d+)%%/g, (_, i) => math[+i] || "");
      }
      // ADD_DATA_URI_TAGS: разрешаем <img src="data:..."> (сгенерированные арты).
      // Панель с кнопкой копирования навешиваем ПОСЛЕ санитайза — иначе DOMPurify
      // вырезал бы нашу же кнопку.
      return this._withCodeToolbar(DOMPurify.sanitize(html, { ADD_DATA_URI_TAGS: ["img"] }));
    },

    // ---------- Horae: теги в тексте и строка под ответом ----------
    // Выражения шаблона не видят window, поэтому HoraeUI зовём через методы.
    // Нет horae.js (не загрузился) — текст как есть: теги видны, но ответ цел.
    // partial=true — режим стрима: отрезается и незакрытый хвостовой блок.
    horaeStrip(text, partial = false) {
      const H = window.HoraeUI;
      return H ? H.stripTags(text || "", partial) : (text || "");
    },
    // Модель прямо сейчас пишет служебный блок — под пузырём пометка вместо него.
    horaeWriting(text) {
      const H = window.HoraeUI;
      return !!(H && text && H.hasOpenTag(text));
    },
    // Модель забыла теги — сервер доизвлекает данные ответа фоновым ИИ-анализом
    // (настройка auto_analyze). Чтобы строка под ответом и открытая «Хроника»
    // не стояли пустыми до следующего хода, через несколько секунд
    // перепроверяем мету именно этого сообщения — без перечитки всего списка.
    _horaeRecheck() {
      const sid = this.sessionId;
      const last = [...this.messages].reverse().find((m) => m.role === "assistant");
      if (!last || typeof last.id !== "number" || last.horae_brief || last.horae_side) return;
      const mid = last.id;
      [6000, 20000].forEach((delay) => setTimeout(async () => {
        if (this.sessionId !== sid) return;
        const m = this.messages.find((x) => x.id === mid);
        if (!m || m.horae_brief) return;
        try {
          const view = await this.api("/messages/" + mid + "/horae");
          if (view && view.brief && this.sessionId === sid) {
            m.horae_brief = view.brief;
            this.horaeTick += 1;
          }
        } catch (e) { /* сообщение удалили или нет доступа — строка останется как есть */ }
      }, delay));
    },
    // Правка данных Horae под сообщением: сводка в строке берётся из списка
    // сообщений (horae_brief), а открытая «Хроника» пересчитывает состояние.
    onHoraeMsgChanged() {
      this.horaeTick += 1;
      this.loadMessages().catch(() => {});
    },

    // Оборачивает каждый <pre> в блок с шапкой: язык слева, «Копировать» справа.
    // Работает на готовом HTML через DOM, а не регулярками, чтобы не разбирать
    // разметку строками и не сломаться на вложенных тегах.
    _withCodeToolbar(html) {
      if (!html || html.indexOf("<pre") === -1) return html;
      const holder = document.createElement("div");
      holder.innerHTML = html;
      holder.querySelectorAll("pre").forEach((pre) => {
        const code = pre.querySelector("code");
        const cls = (code && code.className) || "";
        const m = cls.match(/language-([\w+#-]+)/);
        const wrap = document.createElement("div");
        wrap.className = "code-block";
        const bar = document.createElement("div");
        bar.className = "code-bar";
        const lang = document.createElement("span");
        lang.className = "code-lang";
        lang.textContent = m ? m[1] : "код";   // textContent: язык приходит из ответа модели
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "code-copy";
        btn.title = "Скопировать код";
        btn.textContent = "Копировать";
        bar.appendChild(lang);
        bar.appendChild(btn);
        pre.parentNode.insertBefore(wrap, pre);
        wrap.appendChild(bar);
        wrap.appendChild(pre);
      });
      return holder.innerHTML;
    },

    async copyText(text) {
      // Clipboard API работает ТОЛЬКО в защищённом контексте (https или localhost).
      // Приложение раздаётся по http, поэтому на сервере этот путь недоступен —
      // без запасного варианта кнопка молча не срабатывала бы именно на проде.
      try {
        if (navigator.clipboard && window.isSecureContext) {
          await navigator.clipboard.writeText(text);
          return true;
        }
      } catch (e) { /* пробуем запасной путь ниже */ }
      try {
        const ta = document.createElement("textarea");
        ta.value = text;
        ta.setAttribute("readonly", "");
        ta.style.cssText = "position:fixed;top:-1000px;left:0;opacity:0";
        document.body.appendChild(ta);
        ta.select();
        ta.setSelectionRange(0, ta.value.length);   // iOS иначе не выделяет
        const ok = document.execCommand("copy");
        document.body.removeChild(ta);
        return ok;
      } catch (e) { return false; }
    },

    // Один делегированный обработчик на документ вместо слушателя в каждом
    // сообщении: разметка приходит из v-html и постоянно перерисовывается.
    async _onDocClick(ev) {
      const btn = ev.target && ev.target.closest && ev.target.closest(".code-copy");
      if (!btn) return;
      const block = btn.closest(".code-block");
      const pre = block && block.querySelector("pre");
      if (!pre) return;
      const ok = await this.copyText(pre.innerText);
      btn.textContent = ok ? "✓ Скопировано" : "✕ Не вышло";
      btn.classList.toggle("done", ok);
      clearTimeout(btn._t);
      btn._t = setTimeout(() => {
        btn.textContent = "Копировать";
        btn.classList.remove("done");
      }, 1600);
    },
    // Вырезает LaTeX-фрагменты ($$..$$, \[..\], \(..\), $..$) вне код-блоков,
    // складывает готовый HTML KaTeX в out и возвращает текст с плейсхолдерами
    // %%MATH-n%% (markdown-it отдаёт их как обычный текст, потом подставляем HTML).
    _extractMath(text, out) {
      if (!window.katex) return text;
      const token = (tex, display) => {
        try {
          out.push(katex.renderToString(tex, { displayMode: display, throwOnError: false, output: "html" }));
          return "%%MATH-" + (out.length - 1) + "%%";
        } catch (e) { return tex; }
      };
      // Код (``` и `…`) не трогаем: внутри него $ и \( — обычные символы.
      const parts = text.split(/(```[\s\S]*?(?:```|$)|`[^`\n]*`)/);
      return parts.map((seg, idx) => {
        if (idx % 2 === 1) return seg;
        return seg
          .replace(/\$\$([\s\S]+?)\$\$/g, (m, tex) => token(tex, true))
          .replace(/\\\[([\s\S]+?)\\\]/g, (m, tex) => token(tex, true))
          .replace(/\\\((.+?)\\\)/g, (m, tex) => token(tex, false))
          // Одинарные $…$: без пробела после открывающего и перед закрывающим,
          // в одну строку — чтобы не срабатывать на цены («$5 и $10»).
          .replace(/\$(\S(?:[^$\n]*\S)?)\$/g, (m, tex) => token(tex, false));
      }).join("");
    },

    // ---------- Метки времени сообщений ----------
    // Короткая метка: сегодня — «14:32», иначе «07.10 14:32» (в часовом поясе чата).
    fmtWhen(iso) {
      if (!iso) return "";
      const d = new Date(iso);
      if (isNaN(d)) return "";
      const tz = this.sessionTimezone || undefined;
      try {
        const time = d.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit", timeZone: tz });
        const today = new Date().toLocaleDateString("ru-RU", { timeZone: tz });
        const day = d.toLocaleDateString("ru-RU", { timeZone: tz });
        return day === today ? time : day.slice(0, 5) + " " + time;
      } catch (e) { // неизвестный пояс — показываем локальное время браузера
        return d.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
      }
    },
    // Полная метка для title-подсказки.
    fmtWhenFull(iso) {
      if (!iso) return "";
      const d = new Date(iso);
      if (isNaN(d)) return "";
      try {
        return d.toLocaleString("ru-RU", { timeZone: this.sessionTimezone || undefined })
          + (this.sessionTimezone ? " (" + this.sessionTimezone + ")" : "");
      } catch (e) { return d.toLocaleString("ru-RU"); }
    },
    // POST с прогрессом загрузки (XMLHttpRequest — fetch не умеет upload.onprogress).
    // Используется для отправки сообщений с файлами: видно, сколько уже ушло на
    // сервер, а сторож стриминга не считает долгую загрузку «зависанием».
    _postWithProgress(path, body) {
      return new Promise((resolve, reject) => {
        const isForm = (typeof FormData !== "undefined") && body instanceof FormData;
        const xhr = new XMLHttpRequest();
        xhr.open("POST", "/api" + path);
        // Для FormData Content-Type ставит браузер (multipart с boundary).
        if (!isForm) xhr.setRequestHeader("Content-Type", "application/json");
        const h = this.authHeaders();
        for (const k in h) xhr.setRequestHeader(k, h[k]);
        xhr.upload.onprogress = (e) => {
          this._lastEvtAt = Date.now(); // загрузка идёт — это не зависший стриминг
          this.uploadProgress = e.lengthComputable
            ? { percent: Math.min(100, Math.round((e.loaded / e.total) * 100)), loaded: e.loaded, total: e.total }
            : { percent: null, loaded: e.loaded || 0, total: 0 };
        };
        // Тело догрузилось на сервер — полосу прячем (дальше отвечает нейросеть).
        xhr.upload.onload = () => { this.uploadProgress = null; };
        xhr.onload = () => {
          this.uploadProgress = null;
          if (xhr.status >= 200 && xhr.status < 300) {
            try { resolve(JSON.parse(xhr.responseText || "null")); }
            catch (e) { resolve(null); }
          } else {
            if (xhr.status === 401) this.needAccess = true;
            let detail = "HTTP " + xhr.status;
            try { const j = JSON.parse(xhr.responseText); if (j && j.detail) detail = j.detail; } catch (e) {}
            reject(new Error(detail));
          }
        };
        xhr.onerror = () => { this.uploadProgress = null; reject(new Error("сеть: не удалось загрузить файл на сервер")); };
        xhr.onabort = () => { this.uploadProgress = null; reject(new Error("загрузка отменена")); };
        xhr.send(isForm ? body : JSON.stringify(body));
      });
    },
    // Заголовки авторизации БЕЗ Content-Type — для загрузки файлов (multipart).
    authHeaders() {
      const h = {};
      if (this.accessCode) h["X-Access-Code"] = this.accessCode;
      if (this.adminPassword) h["X-Admin-Password"] = this.adminPassword;
      if (this.userToken) h["X-User-Token"] = this.userToken;
      return h;
    },

    scrollDown() {
      this.$nextTick(() => {
        const el = this.$refs.messages;
        if (el) el.scrollTop = el.scrollHeight;
      });
    },

    // ---------- Персонажи ----------
    async loadCharacters() {
      this.characters = await this.api("/characters");
    },
    // Создать персонажа И ДОВЕСТИ НАМЕРЕНИЕ ДО КОНЦА.
    //
    // Раньше метод заканчивался на loadCharacters(): запись в базе появлялась,
    // а на экране не менялось НИЧЕГО. Для нового пользователя это был первый
    // осмысленный клик в продукте — «Новый диалог», ввёл имя, вернулся на тот
    // же самый экран. Хуже всего, что путь при этом отработал успешно, и
    // понять, что произошло, было неоткуда.
    //
    // Теперь клик доводится до места, ради которого он делался: персонаж
    // выбран, чат создан и открыт, карточка показана с фокусом в «Описании» —
    // единственном поле, без которого персонаж отвечает обобщённо.
    async createCharacter() {
      const name = await this.askPrompt("Имя нового персонажа", { placeholder: "Например: Алиса" });
      if (!name) return;
      const created = await this.api("/characters", {
        method: "POST",
        body: JSON.stringify({ name, first_message: "", system_prompt: "" }),
      });
      await this.loadCharacters();
      const ch = (created && this.characters.find((c) => c.id === created.id)) || null;
      if (!ch) return;
      await this.selectCharacter(ch);
      await this.newChat();
      this.drawerTab = "character";
      this.$nextTick(() => {
        const el = document.querySelector("[data-first-field]");
        if (el) el.focus();
      });
    },
    async importCharacter(e) {
      const file = e.target.files[0];
      if (!file) return;
      const form = new FormData();
      form.append("file", file);
      const res = await fetch("/api/characters/import", { method: "POST", body: form, headers: this.authHeaders() });
      e.target.value = "";
      if (!res.ok) { this.showToast("Не удалось импортировать персонажа (код " + res.status + ")"); return; }
      await this.loadCharacters();
      // Импорт карточки может принести и чаты (нативный формат), и лор Horae —
      // список перечитываем, а не угадываем, что именно пришло.
      this.notifyChatListChanged();
    },
    // Выбор персонажа теперь ТОЛЬКО меняет контекст: показывает карточку в
    // редакторе и ставит фильтр списка чатов. Раньше он же открывал первый чат
    // персонажа, а если чатов не было — создавал новый, то есть просмотр имел
    // разрушительный побочный эффект и плодил пустые чаты. Открытие чата
    // осталось за строкой списка, где ему и место.
    async selectCharacter(c) {
      this.selectedCharacterId = c.id;
      this.charEdit = { ...c, generation_params: c.generation_params || {} };
      this.chatFilterChar = c.id;
      await this.loadSessions();
    },
    async saveCharacter() {
      const c = this.charEdit;
      await this.api("/characters/" + c.id, {
        method: "PATCH",
        body: JSON.stringify({
          name: c.name, description: c.description, personality: c.personality,
          scenario: c.scenario, first_message: c.first_message,
          system_prompt: c.system_prompt, mes_example: c.mes_example || "",
          post_history_instructions: c.post_history_instructions || "",
          model: c.model, avatar_path: c.avatar_path,
        }),
      });
      await this.loadCharacters();
      // Имя персонажа стоит подписью в каждой строке его чатов.
      this.notifyChatListChanged();
    },
    async deleteCharacter(c) {
      if (!(await this.askConfirm("Удалить персонажа «" + c.name + "»?", { okText: "Удалить" }))) return;
      await this.api("/characters/" + c.id, { method: "DELETE" });
      if (this.selectedCharacterId === c.id) { this.selectedCharacterId = null; this.sessionId = null; this.messages = []; }
      await this.loadCharacters();
      this.notifyChatListChanged(); // чаты удалённого персонажа уходят из сайдбара
    },
    async exportCharacter(c) {
      // Экспорт в карточку SillyTavern V2 (вместе с лорбуком из памяти Horae).
      const data = await this.api("/characters/" + c.id + "/export");
      this.downloadJson(data, (c.name || "character") + ".json");
    },
    // Нативный экспорт чата AiChat (полный: персонаж, персона, сообщения, память).
    async exportSession(s) {
      const data = await this.api("/sessions/" + s.id + "/export");
      const base = (data.session && data.session.title) || s.title || "chat";
      this.downloadJson(data, base.replace(/[^\wа-яёА-ЯЁ\-]+/gi, "_") + ".aichat.json");
    },
    downloadJson(data, filename) {
      const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
      this.downloadBlob(blob, filename);
    },
    downloadBlob(blob, filename) {
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = filename;
      a.click();
      URL.revokeObjectURL(url);
    },

    // ---------- Канвас (просмотрщик/редактор сгенерированных документов и кода) ----------
    // Сгенерировать документ/код: запрос уходит в чат, ответ становится Канвасом, а
    // в чате появляется «плашка документа» (по клику открывается Канвас).
    async canvasGenerate(prompt, attachments) {
      if (!this.sessionId) return;
      // Оптимистично показываем своё сообщение в чате.
      this.messages.push({ id: "tmp", role: "user", content: prompt, swipes: [prompt], active_swipe: 0 });
      this.canvasGenerating = true;
      this.scrollDown();
      try {
        const r = await this._postWithProgress("/sessions/" + this.sessionId + "/canvas_generate",
          { prompt, attachments, params: this.params });
        await this.loadMessages();
        await this.openCanvas(r.canvas_id);   // сразу открываем сгенерированное
      } catch (e) {
        this.chatError = "Не удалось сгенерировать документ: " + e.message;
        await this.loadMessages();
      } finally {
        this.canvasGenerating = false;
        // И при успехе, и при ошибке запрос пользователя уже лёг в чат репликой.
        this.notifyChatListChanged(this.sessionId);
      }
    },
    // ПРАВКА открытого канваса на месте (мутация activeDocument, без нового файла).
    async editOpenCanvas(prompt) {
      if (!this.canvas || !this.canvas.id) return;
      this.messages.push({ id: "tmp", role: "user", content: prompt, swipes: [prompt], active_swipe: 0 });
      this.canvasBusy = true;            // оверлей «ИИ дорабатывает…» поверх канваса
      this.scrollDown();
      try {
        const r = await this.api("/sessions/" + this.sessionId + "/canvas_edit", {
          method: "POST",
          body: JSON.stringify({ canvas_id: this.canvas.id, prompt, params: this.params }),
        });
        this.canvas.content = r.canvas.content;
        this.canvas.kind = r.canvas.kind;
        this.canvas.language = r.canvas.language;
        this.canvas.can_undo = r.canvas.can_undo;
        this.clearSel();
        // Автоматически показываем результат правки (превью веб-кода / рендер документа).
        if (this.canvasIsWeb || this.canvas.kind === "document") this.canvasView = "preview";
        await this.loadMessages();       // в чате: запрос + «✏️ Обновил …», БЕЗ новой плашки
      } catch (e) {
        this.chatError = "Не удалось изменить документ: " + e.message;
        await this.loadMessages();
      } finally {
        this.canvasBusy = false;
        this.notifyChatListChanged(this.sessionId);
      }
    },
    // Интент: «создать НОВЫЙ файл с нуля» (тогда — генерация нового канваса).
    _isNewCanvasIntent(t) {
      return /(нов(ый|ую|ое|ого)|с нуля|заново|ещё один|еще один|другой документ|другой файл|создай (новый|документ|файл|код)|напиши новую|сделай новый|next file|new (doc|document|file|article))/i.test(t || "");
    },
    // Интент: «исправь/измени/допиши …» — правка ОТКРЫТОГО канваса.
    _isEditIntent(t) {
      t = (t || "").trim().toLowerCase();
      return /^(исправь|поправь|почини|измени|поменяй|добавь|вставь|убери|удали|замени|сделай|перепиши|сократи|расшир|укороти|допиши|дополни|доработай|обнови|улучши|оформи|переведи|отформатируй|fix|edit|change|add|remove|refactor|rewrite|update|improve|translate|make)/.test(t)
        || /(в документ|в код|этот документ|этот код|в тексте|в файле|в канвас|в статье|здесь|тут|выше|этот баг|эту функци)/.test(t);
    },
    // Тулбар: обернуть выделение (или вставить шаблон) Markdown-разметкой.
    wrapSelection(before, after) {
      const el = this.$refs.canvasEditor;
      if (!el || !this.canvas) return;
      const s = el.selectionStart, e = el.selectionEnd;
      const text = this.canvas.content || "";
      const sel = text.slice(s, e) || "текст";
      this.canvas.content = text.slice(0, s) + before + sel + after + text.slice(e);
      this.$nextTick(() => {
        el.focus();
        el.selectionStart = s + before.length;
        el.selectionEnd = s + before.length + sel.length;
      });
      this.saveCanvas();
    },
    // Тулбар: скопировать код канваса в буфер обмена.
    async copyCanvas() {
      try {
        await navigator.clipboard.writeText(this.canvas ? (this.canvas.content || "") : "");
        this.copied = true;
        setTimeout(() => { this.copied = false; }, 1500);
      } catch (e) { /* буфер недоступен (нет https) — молча игнорируем */ }
    },
    // Открыть существующий Канвас по id (клик по плашке документа в чате).
    async openCanvas(canvasId) {
      if (!canvasId) return;
      try {
        this.canvas = await this.api("/canvas/" + canvasId);
        this.canvasInstruction = "";
        this.clearSel();
        // Автоматически показываем РЕЗУЛЬТАТ: предпросмотр для веб-кода и документов,
        // редактор — для прочего кода.
        this.canvasView = (this.canvasIsWeb || this.canvas.kind === "document") ? "preview" : "edit";
        this.canvasOpen = true;
        this.mobilePane = "canvas";
      } catch (e) { this.showToast("Не удалось открыть канвас: " + e.message); }
    },
    async saveCanvas() {
      if (!this.canvas || !this.canvas.id) return;
      try {
        await this.api("/canvas/" + this.canvas.id, {
          method: "PATCH",
          body: JSON.stringify({ title: this.canvas.title, kind: this.canvas.kind, content: this.canvas.content }),
        });
      } catch (e) {}
    },
    // Запоминаем выделение в редакторе — для точечной правки фрагмента.
    captureSel(e) {
      const t = e.target;
      this.canvasSel = { start: t.selectionStart || 0, end: t.selectionEnd || 0 };
    },
    clearSel() { this.canvasSel = { start: 0, end: 0 }; this.toolbarPos = null; },
    hideToolbar() { this.toolbarPos = null; },
    // Мышь/тап отпущены: если есть выделение — показываем плавающий тулбар над курсором.
    onEditorPointerUp(e) {
      this.captureSel(e);
      if (!this.canvasSelText) { this.toolbarPos = null; return; }
      const p = e.changedTouches ? e.changedTouches[0] : e;
      const x = Math.max(80, Math.min(p.clientX, window.innerWidth - 80));
      this.toolbarPos = { top: Math.max(8, p.clientY - 52), left: x };
    },
    // Клавиатурное выделение (Shift+стрелки): тулбар ставим над редактором по центру.
    onEditorKeyUp(e) {
      this.captureSel(e);
      if (!this.canvasSelText) { this.toolbarPos = null; return; }
      if (!this.toolbarPos && this.$refs.canvasEditor) {
        const r = this.$refs.canvasEditor.getBoundingClientRect();
        this.toolbarPos = { top: Math.max(8, r.top + 10), left: r.left + r.width / 2 };
      }
    },
    // «Своя команда» из плавающего тулбара: единый нижний инпут переходит в режим
    // команды Канвасу (выделение сохраняется), фокус — на него.
    focusCanvasAi() {
      this.toolbarPos = null;
      this.composerMode = "canvasCmd";
      this.mobilePane = "chat";  // на мобильном единый инпут живёт в панели чата
      this.$nextTick(() => { if (this.$refs.composer) this.$refs.composer.focus(); });
    },
    // Доработка ИИ. Если передана строка-инструкция (быстрое действие) — берём её;
    // иначе из поля ввода. Если есть выделение — правим ТОЛЬКО его, иначе весь документ.
    async reviseCanvas(instructionOverride) {
      const fromButton = typeof instructionOverride === "string";
      const instruction = (fromButton ? instructionOverride : this.canvasInstruction).trim();
      if (!this.canvas || !instruction || this.canvasBusy) return;
      this.canvasBusy = true;
      try {
        await this.saveCanvas();
        const body = { instruction };
        if (this.canvasSel.end > this.canvasSel.start) {
          body.selection_start = this.canvasSel.start;
          body.selection_end = this.canvasSel.end;
        }
        const updated = await this.api("/canvas/" + this.canvas.id + "/revise", {
          method: "POST", body: JSON.stringify(body),
        });
        this.canvas.content = updated.content;
        this.canvas.can_undo = updated.can_undo;
        if (!fromButton) this.canvasInstruction = "";
        this.clearSel();
      } catch (e) { this.showToast("ИИ не смог доработать канвас: " + e.message); }
      finally { this.canvasBusy = false; }
    },
    quickAction(instruction) { return this.reviseCanvas(instruction); },
    async translateCode() {
      const lang = await this.askPrompt("На какой язык перевести код?", { placeholder: "Например: Python, Go, Rust" });
      if (lang && lang.trim()) {
        this.reviseCanvas("Переведи этот код на " + lang.trim() + ". Сохрани логику и поведение. Верни только код.");
      }
    },
    async undoCanvas() {
      if (!this.canvas || !this.canvas.can_undo || this.canvasBusy) return;
      try {
        const updated = await this.api("/canvas/" + this.canvas.id + "/undo", { method: "POST" });
        this.canvas.content = updated.content;
        this.canvas.can_undo = updated.can_undo;
        this.clearSel();
      } catch (e) {}
    },
    async exportCanvas(fmt) {
      if (!this.canvas) return;
      await this.saveCanvas();
      const res = await fetch("/api/canvas/" + this.canvas.id + "/export?fmt=" + fmt, { headers: this.authHeaders() });
      if (!res.ok) { this.showToast("Экспорт не удался (код " + res.status + ")"); return; }
      const blob = await res.blob();
      const name = (this.canvas.title || "document").replace(/[^\wа-яёА-ЯЁ\-. ]+/gi, "_").trim() || "document";
      this.downloadBlob(blob, name + "." + fmt);
    },
    closeCanvas() {
      this.saveCanvas(); this.canvasOpen = false; this.mobilePane = "chat";
      // Закрыли документ, оставшись в режиме «Работа», — и режим тут же снова
      // становился ложью: подсвечен, а раскладки 42/58 нет, потому что нет
      // .with-canvas. Из режима выходим вместе с документом.
      if (this.viewMode === "work") this.setViewMode("normal");
      // Канваса больше нет — команда для него бессмысленна.
      if (this.composerMode === "canvasCmd") this.composerMode = "text";
    },

    // ---------- Сессии (чаты) ----------
    async loadSessions() {
      this.sessions = await this.api("/sessions?character_id=" + this.selectedCharacterId);
    },
    async newChat() {
      const r = await this.api("/sessions?character_id=" + this.selectedCharacterId, { method: "POST" });
      await this.syncChatList(r.session_id);
      this.openSession(this._sessionCard(r.session_id) || { id: r.session_id });
    },
    // Найти полную карточку чата по id в уже загруженных списках.
    // openSession зовут из восьми мест, и половина передаёт голый {id}
    // (импорт, шаринг, превращение чата в группу, восстановление после F5).
    // Без этого дровер показывал бы пустые метаданные вместо настоящих.
    _sessionCard(id) {
      // allSessions тоже: новый чат попадает туда на том же refreshChatList, а
      // sessions перечитывается только при выбранном персонаже — без этой строки
      // только что созданный чат открывался бы как голый {id}.
      return this.sessions.find((x) => x.id === id)
        || this.allSessions.find((x) => x.id === id)
        || this.groups.find((x) => x.id === id)
        || this.sharedSessions.find((x) => x.id === id)
        || null;
    },

    // ---------- Доступность: удержание и возврат фокуса ----------
    // Верхний открытый слой. Порядок слоёв уже задан порядком в шаблоне
    // (дровер -> модалки -> диалог -> лайтбокс), поэтому последний найденный
    // узел и есть верхний. Так не нужен ref на каждом из десяти оверлеев.
    _topOverlay() {
      const nodes = document.querySelectorAll(".drawer, .modal, .lightbox");
      return nodes.length ? nodes[nodes.length - 1] : null;
    },
    // Фокусируемое внутри оверлея. Поля выбора файла скрыты визуально, но
    // остаются в табуляции намеренно (см. .file-input), поэтому их пропускать нельзя.
    _focusables(el) {
      const sel = "a[href], button:not([disabled]), input:not([disabled]), "
        + "select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex='-1'])";
      return Array.from(el.querySelectorAll(sel))
        .filter((n) => n.getClientRects().length > 0 || n.classList.contains("file-input"));
    },

    // Одноразовая подсказка про режим фильтрации. Показывается ровно один раз:
    // навязчивое напоминание о настройке, которую человек уже видел, раздражает
    // сильнее, чем отсутствие подсказки.
    _safetyHint() {
      try {
        if (localStorage.getItem("safetyHintSeen")) return;
        localStorage.setItem("safetyHintSeen", "1");
      } catch (e) { return; }
      if (this.params.disable_safety) return;   // человек уже снял фильтры сам
      this.showToast(
        "Включён стандартный режим фильтрации. Zero-Censorship переключается в настройках, вкладка «Генерация»",
        () => { this.drawerTab = "generation"; }
      );
    },

    // ---------- Приборы и инспектор хода ----------
    // Каждый запрос помечен номером, как у loadMemStatus: отчёт перечитывают
    // конец хода, вкладка «Память», сохранение окна и конец задания памяти, и
    // запросы обгоняют друг друга. Поздний ответ старого запроса (или прежнего
    // чата) вернул бы монитору устаревшие числа — пишем только последний.
    async loadCtxStats() {
      const sid = this.sessionId;
      const seq = (this._ctxSeq = (this._ctxSeq || 0) + 1);
      if (!sid) { this.ctxStats = null; this.ctxBusy = false; return; }
      this.ctxBusy = true;
      let stats = null;
      try {
        stats = await this.api("/sessions/" + sid + "/context");
      } catch (e) {
        stats = null;   // у группы без участников контекст не собирается
      }
      if (seq !== this._ctxSeq) return;
      this.ctxStats = this.sessionId === sid ? stats : null;
      this.ctxBusy = false;
    },
    openInspector() {
      this.inspectorOpen = true;
      this.loadCtxStats();
    },
    closeInspector() { this.inspectorOpen = false; },
    // Как на этом ходу искались факты. facts_mode присылает новый сервер;
    // старый знает только facts_enabled, и тогда честно говорим «включены»,
    // не выдумывая способ поиска.
    memFactsLabel(mem) {
      // Сбой памяти сервер отдаёт как facts_mode "off" — без этой проверки
      // человек с включёнными фактами читал бы «выключены» и шёл искать
      // галочку в настройках, а не причину в логе.
      if (mem && mem.error) return "ошибка";
      if (!mem || mem.facts_enabled === false || mem.facts_mode === "off") return "выключены";
      if (mem.facts_mode === "vector") return "по смыслу (эмбеддинги)";
      if (mem.facts_mode === "lexical") return "по совпадению слов";
      return "включены";
    },
    // Сводка памяти хода для инспектора — несколько строк вместо таблицы.
    // Главное в ней — сколько реплик идёт дословно и почему. Окно — это
    // МИНИМУМ дословных реплик: старше него уходит только уже сжатое (снимком
    // или свёртками Хроники). Не догнало сжатие — остальное идёт дословно до
    // потолка бюджета, и тогда здесь строка-предупреждение с тем, что нажать,
    // а не молчаливые «1 040к из 1 049к».
    insMemoryRows(stats) {
      const mem = stats && stats.memory;
      const h = (stats && stats.history) || {};
      if (!mem) return [];
      if (mem.error) return [{ text: "Память не собралась на этом ходу — история идёт целиком", warn: true }];
      const n = (x) => this.fmtNum(x || 0);
      const all = (mem.dropped || 0) + (h.total || 0);
      const rows = [{
        text: "Дословно " + n(h.included) + " из " + n(all) + " " + this.plural(all, "реплики", "реплик", "реплик")
          + (mem.window ? " · окно " + mem.window : " · окно выключено"),
      }];
      const who = { snapshot: "мастер-снимок", horae: "свёртки Хроники", off: "никто — сжатие выключено" }[mem.engine];
      if (mem.covered_upto) {
        const parts = mem.snapshot_upto && mem.covered_upto > mem.snapshot_upto
          ? " (снимок до #" + mem.snapshot_upto + ", дальше свёртки)" : "";
        rows.push({ text: "Сжато до #" + mem.covered_upto + parts + (who ? " · обновляет " + who : "") });
      } else if (who) {
        rows.push({ text: "Сжатого ещё нет · обновляет " + who });
      }
      // Сверх окна идёт дословно то, что сжатие ещё не учло. Несколько реплик —
      // норма (окно шагает по 4, снимок обновляется раз в N сообщений);
      // десятки — сжатие отстало, и ход оплачивается дословной перепиской.
      const backlog = mem.window ? (h.total || 0) - mem.window : 0;
      if (backlog > 20) {
        const fix = mem.engine === "horae" ? "«Хронология» → «Свернуть сейчас»"
          : mem.engine === "snapshot" ? "«Сжатие истории» → «Догнать»"
          : "включите сжатие в «Сжатие истории»";
        rows.push({ text: "Не сжато " + n(backlog) + " " + this.plural(backlog, "реплика", "реплики", "реплик")
          + " сверх окна — идут дословно. " + fix + ".", warn: true });
      }
      if (h.trimmed) {
        rows.push({ text: "Обрезано бюджетом: " + n(h.trimmed) + " " + this.plural(h.trimmed, "реплика", "реплики", "реплик")
          + " (−" + n(h.tokens_trimmed) + " ток.)", warn: true });
      }
      const found = (stats.recalled || []).length;
      const factsOff = mem.facts_enabled === false || mem.facts_mode === "off";
      const extra = ["Факты: " + (factsOff ? "выключены"
        : found ? found + " (" + this.memFactsLabel(mem) + ")" : "не нашлось")];
      if (stats.horae_recall && stats.horae_recall.length) extra.push("воспоминаний Хроники: " + stats.horae_recall.length);
      rows.push({ text: extra.join(" · "), muted: true });
      return rows;
    },
    // «⋯» под сообщением. На телефоне лента действий уже экрана, а её полоса
    // прокрутки спрятана, и раскрытые вторичные кнопки уходили за правый край
    // без единого намёка, что туда можно пролистать. После переключения
    // держим сам переключатель в поле зрения и заново решаем, нужен ли намёк.
    // Кнопку и ленту берём ДО $nextTick: currentTarget события к тому моменту
    // уже обнулён.
    toggleMsgMenu(m, ev) {
      const btn = ev && ev.currentTarget;
      const strip = btn && btn.closest ? btn.closest(".msg-actions") : null;
      this.msgMenu = this.msgMenu === m.id ? null : m.id;
      this.$nextTick(() => {
        if (!strip || !document.contains(strip)) return;
        if (document.contains(btn)) btn.scrollIntoView({ block: "nearest", inline: "nearest" });
        this.msgStripEdge(strip);
      });
    },
    // Намёк «лента длиннее видимого»: атрибут data-more включает затухание
    // правого края (см. .msg-actions[data-more] в CSS). Одним CSS переполнение
    // не распознать, а затухание на коротком ряду гасило бы последнюю кнопку
    // зря. Долистали до конца — намёк снимаем: показывать там больше нечего.
    msgStripEdge(strip) {
      if (!strip) return;
      const more = strip.scrollWidth - strip.clientWidth - strip.scrollLeft > 2;
      if (more) strip.setAttribute("data-more", "");
      else strip.removeAttribute("data-more");
    },
    // Русское множественное число: 1 факт, 2 факта, 5 фактов, 11 фактов, 21 факт.
    plural(n, one, few, many) {
      const m10 = n % 10, m100 = n % 100;
      if (m10 === 1 && m100 !== 11) return one;
      if (m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14)) return few;
      return many;
    },
    // Число с разделителем тысяч, как в строке прогресса сервера («4 200»).
    // Нет числа — прочерк, а не «NaN» или ложный «0»: поле мог не прислать
    // старый сервер.
    fmtNum(n) {
      const v = Number(n);
      return n == null || n === "" || !Number.isFinite(v) ? "—" : v.toLocaleString("ru-RU");
    },
    // «14:05» из ISO-времени сервера (UTC с «Z») — в поясе браузера, а не чата:
    // fmtWhen берёт пояс, сохранённый за чатом (у общего чата — пояс владельца),
    // а «идёт с …» сверяют с часами того, кто смотрит на экран. Нет даты или
    // она битая — "", а не «Invalid Date».
    fmtClock(iso) {
      if (!iso) return "";
      const d = new Date(iso);
      return isNaN(d) ? "" : d.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
    },
    // Сколько строк в тексте — подпись свёрнутого снимка в списке записей
    // («Показать снимок (42 строки)»). Хвостовые пустые строки не считаем:
    // их не видно. Пусто — 0.
    textLines(s) {
      const t = String(s == null ? "" : s).replace(/\s+$/, "");
      return t ? t.split(/\r?\n/).length : 0;
    },
    // Доля части в процентах — ширина сегмента полосы и строки монитора токенов.
    // Пустой знаменатель (пустой чат, старый сервер) даёт 0, а не «NaN%» в подписи.
    sharePct(part, whole) {
      const w = Number(whole);
      return w > 0 ? ((Number(part) || 0) / w) * 100 : 0;
    },

    // Телеметрия хода. Тикер живёт в обычном поле с префиксом _: реактивный
    // дескриптор таймера Vue обернул бы в Proxy (см. заметку про _bgJobs).
    _startTelemetry() {
      this.genStartAt = performance.now();
      this.genFirstAt = 0;
      this.genElapsed = 0;
      this.genTps = 0;
      clearInterval(this._genTimer);
      this._genTimer = setInterval(() => {
        this.genElapsed = (performance.now() - this.genStartAt) / 1000;
        const chars = (this.currentReply || "").length
          + this.liveBubbles.reduce((n, b) => n + (b.content || "").length, 0);
        if (chars > 0 && !this.genFirstAt) this.genFirstAt = performance.now();
        if (this.genFirstAt) {
          const sec = (performance.now() - this.genFirstAt) / 1000;
          // Делим на 4: грубый перевод символов в токены. Точнее браузер не может,
          // токенизатор провайдера ему недоступен, поэтому подпись говорит «≈».
          if (sec > 0.4) this.genTps = Math.round(chars / 4 / sec);
        }
      }, 250);
    },
    _stopTelemetry() {
      clearInterval(this._genTimer);
      this._genTimer = null;
      // Контекст после хода вырос — пересчитываем полосу заполнения.
      this.loadCtxStats();
    },
    // Задержка до первого токена: главный признак «модель думает или зависла».
    // Обычный метод, а не геттер: Vue перебирает methods и ждёт там функции,
    // геттер вычислился бы один раз при инициализации.
    ttft() { return this.genFirstAt ? (this.genFirstAt - this.genStartAt) / 1000 : 0; },

    // ---------- Раскладка оболочки ----------
    // Смена раскладки обязана быть мгновенной и без перезагрузки: человек
    // выбирает мебель, глядя на свою настоящую переписку, а не на пустой экран.
    // Поэтому всё держится на классе корня и CSS, а не на пересборке дерева.
    setShellLayout(id) {
      if (!this.shellLayouts.some((s) => s.id === id)) return;
      this.shellLayout = id;
      localStorage.setItem("shellLayout", id);
      // Раскладка «по имени» не держит постоянного списка. Если сайдбар остался
      // открытым от прежней раскладки, он повис бы поверх ленты без причины.
      if (id === "name") this.sidebarOpen = false;
      // Смена раскладки переставляет весь экран, а без объявления слепой
      // пользователь узнаёт об этом только наткнувшись на переехавший контрол.
      const s = this.shellLayouts.find((x) => x.id === id);
      if (s) this.liveStatus = "Раскладка: " + s.label;
    },

    // ---------- Режим ленты ----------
    async setViewMode(m) {
      // Прежнее условие открывало канвас только при уже загруженном документе
      // (m === "work" && !this.canvasOpen && this.canvas), а до первой генерации
      // документа не существует ни у кого. Поэтому пункт «Работа с документом»
      // отрисовывался включённым и не делал РОВНО НИЧЕГО: раскладка 42/58 висит
      // на классе .with-canvas, а его без канваса неоткуда взять.
      if (m === "work") {
        // Режим обязан либо открыть документ, либо сказать, почему не может.
        // Переключиться молча и оставить экран прежним — это и есть ложь.
        if (!this.sessionId) {
          this.showToast("Документ живёт внутри чата: сначала откройте или создайте диалог");
          return;
        }
        this.viewMode = m;
        localStorage.setItem("viewMode", m);
        // Зовём БЕЗУСЛОВНО. С проверкой `if (!this.canvasOpen)` защита от чужого
        // документа была мёртвой: она живёт внутри ensureWorkCanvas, а в
        // единственном сценарии, ради которого написана, до неё не доходило
        // управление. Воспроизводилось так: открыть документ в чате A, перейти
        // в чат B (openSession поля canvas и canvasOpen не обнуляет), нажать 📄 —
        // и канвас чата A показывался как документ чата B. Метод сам
        // короткозамыкает, когда документ уже открыт и принадлежит этому чату.
        await this.ensureWorkCanvas();
        return;
      }
      this.viewMode = m;
      localStorage.setItem("viewMode", m);
    },
    // Документ для режима «Работа». Сначала ищем уже существующий в этом чате:
    // иначе каждый вход в режим плодил бы в базе пустой канвас. Если чат ещё
    // ничего не сгенерировал, заводим пустой черновик — пустой редактор честнее
    // экрана, неотличимого от обычного режима.
    async ensureWorkCanvas() {
      // Канвас ДРУГОГО чата переиспользовать нельзя: openSession поле canvas не
      // обнуляет, и документ соседнего диалога открылся бы здесь как свой.
      if (this.canvas && this.canvas.session_id === this.sessionId) {
        this.canvasOpen = true; this.mobilePane = "canvas"; return;
      }
      try {
        const rows = await this.api("/canvas?session_id=" + this.sessionId);
        if (rows && rows.length) return this.openCanvas(rows[0].id);
        this.canvas = await this.api("/canvas", {
          method: "POST",
          body: JSON.stringify({ session_id: this.sessionId, title: "Черновик", kind: "document", content: "" }),
        });
        this.canvasInstruction = "";
        this.clearSel();
        // Пустой документ показываем редактором, а не предпросмотром: рендер
        // пустой строки — это пустой экран, из которого не видно, куда печатать.
        this.canvasView = "edit";
        this.canvasOpen = true;
        this.mobilePane = "canvas";
      } catch (e) {
        // Не вышло — откатываем режим: нажатая кнопка без документа это та же ложь.
        this.viewMode = "normal";
        localStorage.setItem("viewMode", "normal");
        this.showToast("Не удалось открыть документ: " + (e && e.message ? e.message : e));
      }
    },

    // ---------- Нечёткий поиск ----------
    // Совпадение по подпоследовательности символов: «нвчт» находит «Новый чат».
    // Возвращает РАНГ (меньше — лучше) или -1, если совпадения нет. Ранг растёт
    // от позиции первого совпадения и от разрывов между буквами, поэтому точное
    // начало слова всегда обходит совпадение в середине.
    _fuzzy(hay, needle) {
      if (!needle) return 0;
      const h = (hay || "").toLowerCase();
      const n = needle.toLowerCase();
      let from = 0, first = -1, last = -1, gaps = 0;
      for (const ch of n) {
        const idx = h.indexOf(ch, from);
        if (idx < 0) return -1;
        if (first < 0) first = idx;
        if (last >= 0) gaps += idx - last - 1;
        last = idx;
        from = idx + 1;
      }
      return first * 2 + gaps;
    },

    // ---------- Командная палитра ----------
    openPalette(prefill) {
      this.paletteQuery = prefill || "";
      this.paletteIndex = 0;
      this.searchResults = [];
      this.paletteOpen = true;
      this.$nextTick(() => { if (this.$refs.paletteInput) this.$refs.paletteInput.focus(); });
    },
    closePalette() {
      this.paletteOpen = false;
      this.paletteQuery = "";
      this.searchResults = [];
    },
    paletteMove(step) {
      const n = this.paletteItems.length;
      if (!n) return;
      this.paletteIndex = (this.paletteIndex + step + n) % n;
    },
    async paletteRun(item) {
      const it = item || this.paletteItems[this.paletteIndex];
      if (!it) return;
      this.closePalette();
      // Через openChatRow, как в сайдбаре и «Недавних»: строка может быть чужим
      // расшаренным чатом, а openRecent открыл бы его как свой — с активными
      // настройками чата и автоподстановкой пояса прямо в сессию владельца.
      if (it.kind === "chat") return this.openChatRow(it.row);
      if (it.kind === "char") {
        const c = this.characters.find((x) => x.id === it.id);
        if (c) { this.selectCharacter(c); this.sidebarOpen = true; }
        return;
      }
      if (it.kind === "msg") return this.jumpToMessage(it.sid, it.id);
      // Команды.
      // Диспетчеризация режимов: id вида view-<режим>, само значение — хвост
      // после дефиса, чтобы список команд и список viewModes не разъезжались.
      if (it.id === "view-normal" || it.id === "view-scene" || it.id === "view-work") {
        return this.setViewMode(it.id.slice(5));
      }
      if (it.id.indexOf("shell-") === 0) return this.setShellLayout(it.id.slice(6));
      if (it.id === "new") return this.startNewChat();
      if (it.id === "model" || it.id === "settings") { this.drawerTab = "generation"; return; }
      if (it.id === "export") {
        const s = this._sessionCard(this.sessionId);
        if (s) this.exportSession(s);
        return;
      }
      if (it.id === "branch") {
        const last = this.messages[this.messages.length - 1];
        if (last) return this.forkFrom(last);
        this.showToast("Ветвить нечего: в чате нет сообщений");
        return;
      }
      if (it.id === "clear") {
        this.input = "";
        this.composerMode = "text";
        this.resetComposerHeight();
      }
    },

    // ---------- Поиск по репликам ----------
    // Дебаунс держим в обычном поле с префиксом _, а не в data: Vue обернул бы
    // дескриптор таймера в Proxy (см. заметку про _bgJobs).
    scheduleSearch() {
      clearTimeout(this._searchTimer);
      const q = this.paletteQuery.trim();
      if (q.startsWith("/") || q.length < 2) { this.searchResults = []; return; }
      this._searchTimer = setTimeout(() => this.runSearch(q), 220);
    },
    async runSearch(q) {
      this.searchBusy = true;
      try {
        const r = await this.api("/search?q=" + encodeURIComponent(q));
        // Пока ждали ответ, запрос мог смениться — старую выдачу не показываем.
        if (this.paletteQuery.trim() === q) this.searchResults = r.results || [];
      } catch (e) {
        this.searchResults = [];
      } finally {
        this.searchBusy = false;
      }
    },
    // Открыть чат и доехать до конкретной реплики. История грузится окнами,
    // поэтому нужное сообщение может быть ещё не загружено: догружаем порциями,
    // как это делает scrollToChatStart, с тем же предохранителем.
    async jumpToMessage(sessionId, messageId) {
      if (sessionId !== this.sessionId) {
        await this.openSession(this._sessionCard(sessionId) || { id: sessionId });
      }
      this.jumpBusy = true;
      try {
        let guard = 40;
        while (guard-- > 0) {
          if (this.messages.some((m) => m.id === messageId)) break;
          if (this.noMoreMessages) break;
          const before = this.messages.length;
          await this.loadOlder();
          if (this.messages.length === before) break;  // ничего не пришло
        }
        await this.$nextTick();
        this.scrollMessageStart(messageId);
      } finally {
        this.jumpBusy = false;
      }
    },

    // ---------- Ветвление ----------
    async forkFrom(m) {
      if (!this.sessionId || !m) return;
      const ok = await this.askConfirm(
        "Создать ветку от этой реплики? В новый чат скопируется вся история до неё включительно, исходный чат не изменится.",
        { title: "Ветка чата", okText: "Создать ветку", danger: false }
      );
      if (!ok) return;
      try {
        const r = await this.api("/sessions/" + this.sessionId + "/fork", {
          method: "POST",
          body: JSON.stringify({ message_id: m.id }),
        });
        await this.syncChatList(r.session_id);
        await this.openSession(this._sessionCard(r.session_id) || { id: r.session_id });
        this.showToast("Ветка создана: " + r.messages + " реплик");
      } catch (e) {
        this.showToast("Не удалось создать ветку: " + (e && e.message ? e.message : e));
      }
    },

    // ---------- Первый экран ----------
    // Все чаты пользователя одним списком. Это основной источник сайдбара;
    // loadSessions (чаты одного персонажа) остаётся для мест, где нужен
    // именно контекст персонажа.
    //
    // Ошибка НЕ обнуляет список. Раньше здесь стояло allSessions = [], и теперь,
    // когда список перечитывается фоном после каждой реплики, один моргнувший
    // запрос стирал бы весь сайдбар до следующего успешного. Старые строки
    // честнее пустоты: они устарели на один ход, а не исчезли.
    async loadAllSessions() {
      try { this.allSessions = await this.api("/sessions"); } catch (e) { /* оставляем прежние */ }
    },
    // Чаты, которыми со мной поделились. Раньше их перечитывал только таймер
    // друзей раз в 20 секунд, и общий чат после приглашения появлялся с
    // задержкой, а в режиме без аккаунтов запрос и вовсе незачем слать.
    async loadSharedSessions() {
      if (!this.authStatus.accounts_enabled || !this.userToken) return;
      try { this.sharedSessions = await this.api("/sessions/shared"); } catch (e) { /* оставляем прежние */ }
    },
    // Перечитать всё, из чего собран сайдбар. Сайдбар строится из allSessions,
    // groups и sharedSessions, а мутации раньше обновляли только sessions (чаты
    // выбранного персонажа) — новый, переименованный или закреплённый чат
    // появлялся в списке лишь после F5. Параллельные вызовы склеиваются: пока
    // идёт чтение, новый вызов только ставит флаг «ещё раз» и получает тот же
    // промис, поэтому пачка мутаций даёт не больше двух проходов, а не десяток.
    refreshChatList() {
      if (this._chatListLoading) { this._chatListAgain = true; return this._chatListLoading; }
      // Фокус внутри сайдбара. Строки с устойчивым ключом (вид + id) Vue
      // переиспользует, но фокус это не спасает: закреп переставляет строку, и
      // keyed-diff двигает её узел через insertBefore — перенос подключённого
      // узла снимает с него фокус, хотя сам узел остаётся в документе. А если
      // строку убрали (чат удалён в другой вкладке), фокус и вовсе падает на
      // <body>. В обоих случаях клавиатурный путь начинался бы с начала
      // страницы — возвращаем фокус туда, где он был, или хотя бы в список.
      const focused = document.activeElement;
      const inSidebar = !!(focused && focused.closest && focused.closest(".sidebar"));
      const run = async () => {
        try {
          do {
            this._chatListAgain = false;
            // Каждый источник ловит свою ошибку сам: иначе сбой одного запроса
            // ронял бы весь Promise.all необработанным отказом из слушателя
            // события, а два других, уже успевших прийти, всё равно применились бы.
            await Promise.all([
              this.selectedCharacterId ? this.loadSessions().catch(() => {}) : null,
              this.loadAllSessions(),
              this.loadGroups().catch(() => {}),
              this.loadSharedSessions(),
            ]);
          } while (this._chatListAgain);
        } finally { this._chatListLoading = null; }
        if (inSidebar) {
          this.$nextTick(() => {
            // Фокус никуда не делся (строка осталась на месте) — не трогаем.
            if (document.activeElement === focused) return;
            // Фокус ушёл сам (человек за это время кликнул или протабал дальше) —
            // не отбираем. Возвращаем, только если он упал на <body>.
            const act = document.activeElement;
            if (act && act !== document.body) return;
            if (document.contains(focused)) { focused.focus({ preventScroll: true }); return; }
            // Строка чата (row2), а не первая строка сайдбара: та — персонаж.
            const next = document.querySelector(".sidebar .list-item.row2 .row-main")
              || document.querySelector(".sidebar .list-item .row-main");
            if (next) next.focus();
          });
        }
      };
      this._chatListLoading = run();
      return this._chatListLoading;
    },
    // Сообщить всем, что список чатов устарел. Слушатель в mounted перечитывает
    // его с небольшой задержкой, поэтому серия мутаций даёт один запрос.
    // sid — чат, в котором что-то поменялось (если известен): соседняя вкладка,
    // где открыт тот же чат, перечитает и его ленту, а не только список.
    notifyChatListChanged(sid) {
      window.dispatchEvent(new CustomEvent(CHATLIST_EVENT, { detail: { sid: sid || null } }));
      this._broadcastChatList(sid);
    },
    // Для мутаций, которым обновлённые строки нужны СРАЗУ (открыть только что
    // созданный чат, найти его карточку): перечитываем без дебаунса и сообщаем
    // соседним вкладкам. Местное событие здесь не шлём — оно дало бы второй,
    // лишний запрос следом за этим.
    syncChatList(sid) {
      this._broadcastChatList(sid);
      return this.refreshChatList();
    },
    _broadcastChatList(sid) {
      if (!this._chatChannel) return;
      try { this._chatChannel.postMessage({ type: "chatlist", from: TAB_ID, sid: sid || null }); } catch (e) {}
    },
    // Сигнал пришёл из соседней вкладки. Наружу его НЕ пересылаем: иначе две
    // вкладки гоняли бы одно сообщение друг другу бесконечно.
    _onChatChannel(ev) {
      const d = ev && ev.data;
      if (!d || d.type !== "chatlist" || d.from === TAB_ID) return;
      window.dispatchEvent(new CustomEvent(CHATLIST_EVENT, { detail: { sid: d.sid || null, remote: true } }));
    },
    // Единый приёмник CHATLIST_EVENT. Дебаунс склеивает пачку событий (закреп +
    // новая реплика, или «job» и «done» короткого ответа) в один запрос.
    _onChatListEvent(e) {
      // До входа не трогаем API: 401 от фонового запроса взводит needAccess, и
      // человек, вошедший по логину, упирался бы следом в экран кода доступа.
      // Сигнал из соседней вкладки или возвращение к окну приходят и на воротах.
      if (!this._appReady) return;
      const d = (e && e.detail) || {};
      clearTimeout(this._chatListTimer);
      this._chatListTimer = setTimeout(async () => {
        await this.refreshChatList();
        this._checkOpenChatAlive();
      }, 120);
      // Реплика пришла в чат, который открыт и здесь: иначе соседняя вкладка
      // показывала бы новый ответ в превью сайдбара, а в самой ленте — нет.
      // Ошибку глотаем: чат могли удалить в той вкладке, что прислала сигнал,
      // и тогда перечитка падает 403 — такой чат закрывает _checkOpenChatAlive
      // после обновления списка, а не необработанный отказ в консоли.
      if (d.remote && d.sid && d.sid === this.sessionId && this._feedIdle()) {
        this.loadMessages().catch(() => {});
      }
    },
    // Можно ли сейчас перечитать ленту открытого чата фоном. Перечитка ЗАМЕНЯЕТ
    // messages последним окном, поэтому нельзя: во время своей генерации (стёрла
    // бы живой пузырь стрима), правки (правку чинит своё сохранение), генерации
    // и доработки в Канвасе (у них временный пузырь id "tmp" без streaming),
    // подгрузки старых сообщений и прыжка в начало (порция истории пропала бы
    // из-под читателя вместе с позицией прокрутки).
    _feedIdle() {
      return !this.streaming && !this.editingId && !this.canvasGenerating
        && !this.canvasBusy && !this.loadingOlder && !this.jumpBusy;
    },
    // Открытый чат пропал из списка — например, его удалили в соседней вкладке.
    // Раньше он оставался открытым: лента, сокет и поле ввода смотрели в
    // удалённый чат, а каждое возвращение к вкладке снова дёргало его ленту и
    // получало ошибку. Одного отсутствия в списке мало (список мог не
    // дочитаться, чат мог появиться между запросами), поэтому спрашиваем
    // сервер и закрываем только на честный отказ 403/404, а не на сбой сети.
    async _checkOpenChatAlive() {
      const sid = this.sessionId;
      if (!sid || this.sharedView || this.streaming) return;
      if (this.unifiedChats.some((r) => r.id === sid)) return;
      try {
        await this.api("/sessions/" + sid + "/messages?limit=1");
        return;
      } catch (e) {
        if (e.status !== 403 && e.status !== 404) return;
      }
      if (this.sessionId !== sid) return;   // пока спрашивали, человек ушёл в другой чат
      // Поколение сокета сдвигаем ДО закрытия: onclose старого сокета тогда
      // видит чужое поколение и не планирует переподключение к удалённому чату.
      this._wsGen = (this._wsGen || 0) + 1;
      if (this.ws) { try { this.ws.close(); } catch (e) {} }
      this._stopHeartbeat();
      if (this._wsReconnectTimer) { clearTimeout(this._wsReconnectTimer); this._wsReconnectTimer = null; }
      this.connected = false;
      this.sessionId = null;
      this.messages = [];
      this.showToast("Этот чат удалён в другой вкладке или на другом устройстве");
    },
    // Минутный опрос, пока вкладка на виду. Ленту открытого чата перечитываем
    // только если в списке у него появилась реплика новее последней показанной:
    // перечитывать её каждую минуту вслепую — лишний запрос почти всегда.
    async _pollChatList() {
      if (!this._appReady || document.visibilityState !== "visible") return;
      await this.refreshChatList();
      await this._checkOpenChatAlive();
      if (!this.sessionId || !this._feedIdle()) return;
      const row = this.unifiedChats.find((r) => r.id === this.sessionId);
      const shown = this.messages.reduce((mx, m) => (typeof m.id === "number" && m.id > mx ? m.id : mx), 0);
      if (row && row.last_id && row.last_id > shown) this.loadMessages().catch(() => {});
    },
    // Возвращение к вкладке. Реплики, пришедшие через Telegram-бота, сервер
    // никому не объявляет, поэтому единственный честный момент их подобрать —
    // когда человек снова смотрит на приложение. Не чаще раза в 10 секунд:
    // focus и visibilitychange приходят парой на одно и то же возвращение.
    //
    // Идём тем же путём, что и минутный опрос, а не «удалённым» сигналом с sid
    // открытого чата: тот перечитывал ленту на КАЖДОЕ возвращение вслепую. В
    // длинном чате это срезало подгруженную историю до последних 400 реплик
    // прямо под читателем (alt-tab, клик в превью Канваса и обратно), а заодно
    // стирало временный пузырь Канваса. Опрос перечитывает ленту, только если
    // в списке у чата есть реплика новее показанной.
    _onChatListWake() {
      if (document.visibilityState !== "visible") return;
      const now = Date.now();
      if (now - (this._chatListWokeAt || 0) < 10000) return;
      this._chatListWokeAt = now;
      this._pollChatList();
    },
    async startNewChat() {
      if (this.selectedCharacter) { await this.newChat(); return; }
      if (this.characters.length) {
        await this.selectCharacter(this.characters[0]);
        await this.newChat();
        return;
      }
      await this.createCharacter();
    },
    // Открыть чат из блока быстрого старта или из палитры: подтягиваем контекст
    // персонажа (карточка, список его чатов) и открываем именно тот чат, по
    // которому кликнули, а не первый попавшийся.
    // Строка единого списка: личный чат, группа и общий чат открываются
    // по-разному, но для пользователя это одна и та же строка.
    async openChatRow(s) {
      this.rowMenu = null;   // открыли чат — раскрытые действия соседней строки больше не нужны
      if (s.kind === "shared") return this.openSharedSession(s);
      return this.openRecent(s);
    },
    async openRecent(s) {
      if (s.character_id && s.character_id !== this.selectedCharacterId) {
        const c = this.characters.find((x) => x.id === s.character_id);
        if (c) {
          this.selectedCharacterId = c.id;
          this.charEdit = { ...c, generation_params: c.generation_params || {} };
          await this.loadSessions();
        }
      }
      await this.openSession(s);
    },
    async openSession(s) {
      if (s.id === this.sessionId && !this.sharedView) return; // уже открыт
      this._handoffStreaming();  // текущую генерацию (если есть) доигрываем в фоне
      this.sharedView = null;   // это мой собственный чат, а не «чужой»
      this.sessionId = s.id;
      this._clearPending(s.id); // открыли чат — снимаем метку «пришёл ответ»
      // Метаданные берём из переданного объекта, а недостающее дотягиваем из
      // списков: переданный {id} не должен выглядеть как «у чата всё пусто».
      const card = ("author_note" in s) ? s : (this._sessionCard(s.id) || s);
      this.authorNote = card.author_note || "";
      this.sessionPersonaId = card.persona_id || null;
      this.sessionBg = card.background || "";
      this.sessionTimezone = card.timezone || "";
      // Пояс ещё не задан — определяем по браузеру и сохраняем за этим чатом.
      // Пользователь может сменить его во вкладке «Персона» (настройка на чат).
      if (!this.sessionTimezone) this._autoTimezone();
      this.closeSidebarOnMobile(); // на мобильном прячем сайдбар после выбора
      // Запоминаем последний открытый чат — восстановим при перезагрузке страницы.
      this._rememberLastChat();
      await this.loadMessages(true);   // свежее открытие — грузим последнюю порцию
      this.connectWs();
    },
    // Сохранить/восстановить последний открытый чат (чтобы после F5 сразу писать).
    _rememberLastChat() {
      try {
        localStorage.setItem("lastChat", JSON.stringify({
          sessionId: this.sessionId,
          characterId: this.selectedCharacterId,
          isGroup: this.currentIsGroup,
        }));
      } catch (e) {}
    },
    async _restoreLastChat() {
      let saved;
      try { saved = JSON.parse(localStorage.getItem("lastChat") || "null"); } catch (e) {}
      if (!saved || !saved.sessionId) return false;
      // Группа: открываем по id из списка групп.
      if (saved.isGroup) {
        const g = this.groups.find((x) => x.id === saved.sessionId);
        if (g) { await this.openSession(g); return true; }
      }
      // Обычный чат: выбираем персонажа и открываем именно этот чат.
      if (saved.characterId) {
        const c = this.characters.find((x) => x.id === saved.characterId);
        if (c) {
          this.selectedCharacterId = c.id;
          this.charEdit = { ...c, generation_params: c.generation_params || {} };
          await this.loadSessions();
          const s = this.sessions.find((x) => x.id === saved.sessionId);
          if (s) { await this.openSession(s); return true; }
        }
      }
      return false;
    },
    // Определить часовой пояс по браузеру и тихо сохранить его за текущим чатом.
    _autoTimezone() {
      let tz = "";
      try { tz = Intl.DateTimeFormat().resolvedOptions().timeZone || ""; } catch (e) {}
      if (!tz || !this.sessionId) return;
      this.sessionTimezone = tz;
      const sid = this.sessionId;
      this.api("/sessions/" + sid, {
        method: "PATCH", body: JSON.stringify({ timezone: tz }),
      }).then(() => this._patchSessionRow(sid, { timezone: tz })).catch(() => {});
    },
    // Открыть чат, которым со мной поделился друг (только из раздела «Доступные мне»).
    async openSharedSession(s) {
      this._handoffStreaming();
      this.sharedView = s;
      this.sessionId = s.id;
      this._clearPending(s.id);
      this.authorNote = "";
      this.sessionPersonaId = null;
      this.sessionBg = s.background || "";
      this.sessionTimezone = s.timezone || ""; // чужой чат: пояс владельца, не перезаписываем
      this.closeSidebarOnMobile();
      this.notifOpen = false;
      await this.loadMessages(true);   // свежее открытие — грузим последнюю порцию
      this.connectWs();
    },
    // Перевести идущую генерацию текущего чата в фон: она досчитается на сервере,
    // мы поймаем «готово» через SSE и подсветим тот чат (а не свежеоткрытый).
    _handoffStreaming() {
      if (this.streaming && this.currentJobId && this.sessionId) {
        this._trackBackgroundJob(this.sessionId, this.currentJobId);
      }
      this.streaming = false;
      this.currentJobId = null;
      this.currentReply = "";
      this.currentThought = "";
      this.liveBubbles = [];
    },
    _trackBackgroundJob(sid, jobId) {
      // Хэндлы EventSource держим вне реактивных данных, чтобы Vue не оборачивал их в Proxy.
      this._bgJobs = this._bgJobs || {};
      if (this._bgJobs[sid]) { try { this._bgJobs[sid].close(); } catch (e) {} }
      const es = new EventSource("/sse/job/" + jobId);
      this._bgJobs[sid] = es;
      es.onmessage = (e) => {
        let ev; try { ev = JSON.parse(e.data); } catch (_) { return; }
        if (ev.type !== "done" && ev.type !== "error") return; // токены в фоне не нужны
        try { es.close(); } catch (_) {}
        delete this._bgJobs[sid];
        this.notifyChatListChanged(sid);
        if (this.sessionId === sid) { this.loadMessages(); return; } // уже вернулись сюда
        if (!this.pendingChats.includes(sid)) this.pendingChats.push(sid);
        if (this.soundOn) this.playChime();
        this.showToast(
          ev.type === "error" ? "⚠ Ошибка генерации в другом чате" : "💬 Ответ готов в другом чате",
          () => this._openById(sid),
        );
      };
    },
    _clearPending(sid) {
      const i = this.pendingChats.indexOf(sid);
      if (i >= 0) this.pendingChats.splice(i, 1);
    },
    // Открыть чат по id, найдя его среди обычных/групповых/расшаренных.
    _openById(sid) {
      const s = this.sessions.find((x) => x.id === sid) || this.allSessions.find((x) => x.id === sid);
      if (s) return this.openRecent(s);   // openRecent подтянет и контекст персонажа
      const g = this.groups.find((x) => x.id === sid);
      if (g) return this.openSession(g);
      const sh = this.sharedSessions.find((x) => x.id === sid);
      if (sh) return this.openSharedSession(sh);
      return this.openSession({ id: sid }); // запасной вариант: хотя бы покажем сообщения
    },
    showToast(text, onClick) {
      const id = (this._toastSeq = (this._toastSeq || 0) + 1);
      this.toasts.push({ id, text, onClick });
      setTimeout(() => this.dismissToast(id), 8000);
    },
    // Внутренние диалоги вместо браузерных confirm()/prompt() — часть приложения.
    // Возвращают Promise: askConfirm → true/false, askPrompt → строка или null.
    askConfirm(message, opts = {}) {
      return new Promise((resolve) => {
        this.dialog = {
          mode: "confirm", message,
          title: opts.title || "Подтвердите действие",
          okText: opts.okText || "OK", cancelText: opts.cancelText || "Отмена",
          danger: opts.danger !== false, value: "", placeholder: "",
        };
        this._dialogResolve = resolve;
      });
    },
    askPrompt(message, opts = {}) {
      return new Promise((resolve) => {
        this.dialog = {
          mode: "prompt", message: opts.message || "",
          title: message || "Введите значение",
          okText: opts.okText || "OK", cancelText: opts.cancelText || "Отмена",
          danger: false, value: opts.value || "", placeholder: opts.placeholder || "",
        };
        this._dialogResolve = resolve;
        this.$nextTick(() => {
          const el = this.$refs.dialogInput;
          if (el) { el.focus(); el.select(); }
        });
      });
    },
    dialogOk() {
      const d = this.dialog;
      if (!d) return;
      this.dialog = null;
      const r = this._dialogResolve; this._dialogResolve = null;
      if (r) r(d.mode === "prompt" ? d.value : true);
    },
    dialogCancel() {
      const d = this.dialog;
      if (!d) return;
      this.dialog = null;
      const r = this._dialogResolve; this._dialogResolve = null;
      if (r) r(d.mode === "prompt" ? null : false);
    },
    dismissToast(id) {
      const i = this.toasts.findIndex((t) => t.id === id);
      if (i >= 0) this.toasts.splice(i, 1);
    },
    toastClick(t) {
      if (t.onClick) t.onClick();
      this.dismissToast(t.id);
    },
    async deleteSession(s) {
      if (!(await this.askConfirm("Удалить этот чат?", { okText: "Удалить" }))) return;
      await this.api("/sessions/" + s.id, { method: "DELETE" });
      if (this.sessionId === s.id) { this.sessionId = null; this.messages = []; }
      await this.syncChatList(s.id);
    },
    async renameSession(s) {
      const title = await this.askPrompt("Новое название чата", { value: s.title, placeholder: "Название чата" });
      if (!title) return;
      await this.api("/sessions/" + s.id, { method: "PATCH", body: JSON.stringify({ title }) });
      await this.syncChatList(s.id);
    },

    // ---------- Групповые чаты ----------
    async loadGroups() { this.groups = await this.api("/groups"); },
    async openGroupModal() {
      this.groupName = "Групповой чат";
      this.groupScenario = "";
      this.groupSelectedIds = [];
      this.groupInviteSelected = [];
      this.groupDirector = false;
      if (this.authStatus.accounts_enabled) await this.loadFriends();
      this.groupModal = true;
    },
    toggleGroupChar(id) {
      const i = this.groupSelectedIds.indexOf(id);
      if (i >= 0) this.groupSelectedIds.splice(i, 1);
      else this.groupSelectedIds.push(id);
    },
    // Аккордеон сайдбара: раскрыть/свернуть раздел.
    toggleSection(name) { this.openSections[name] = !this.openSections[name]; },
    // На мобильном после выбора чата прячем сайдбар; на десктопе оставляем.
    closeSidebarOnMobile() {
      if (window.matchMedia && window.matchMedia("(max-width: 760px)").matches) this.sidebarOpen = false;
    },
    toggleGroupInvite(username) {
      const i = this.groupInviteSelected.indexOf(username);
      if (i >= 0) this.groupInviteSelected.splice(i, 1);
      else this.groupInviteSelected.push(username);
    },
    async createGroup() {
      if (this.groupSelectedIds.length < 1) { this.showToast("Выберите хотя бы одного персонажа"); return; }
      const r = await this.api("/groups", {
        method: "POST",
        body: JSON.stringify({
          name: this.groupName, character_ids: this.groupSelectedIds,
          director: this.groupDirector, scenario: this.groupScenario,
        }),
      });
      // Приглашаем выбранных друзей в созданную комнату.
      for (const username of this.groupInviteSelected) {
        try {
          await this.api("/sessions/" + r.session_id + "/share", {
            method: "POST", body: JSON.stringify({ username }),
          });
        } catch (e) { /* пропускаем */ }
      }
      this.groupModal = false;
      await this.syncChatList(r.session_id);
      this.openSession(this._sessionCard(r.session_id) || { id: r.session_id });
    },
    async toggleDirector() {
      if (!this.currentGroup) return;
      const val = !this.currentGroup.director;
      await this.api("/sessions/" + this.sessionId, { method: "PATCH", body: JSON.stringify({ director: val }) });
      await this.syncChatList(this.sessionId);
    },
    // ---------- Управление участниками (добавить/убрать, чат→группа) ----------
    openMembers() {
      // Кандидаты — персонажи, которых ещё нет в этом чате.
      const inChat = new Set((this.currentGroup ? this.currentGroup.members : []).map((m) => m.id));
      if (!this.currentIsGroup && this.selectedCharacterId) inChat.add(this.selectedCharacterId);
      this.memberAddSelected = [];
      this.membersOpen = true;
    },
    toggleMemberAdd(id) {
      const i = this.memberAddSelected.indexOf(id);
      if (i >= 0) this.memberAddSelected.splice(i, 1);
      else this.memberAddSelected.push(id);
    },
    // Персонажи, доступные для добавления (кого ещё нет в этом чате).
    availableToAdd() {
      const ids = new Set((this.currentGroup ? this.currentGroup.members : []).map((m) => m.id));
      if (!this.currentIsGroup && this.selectedCharacterId) ids.add(this.selectedCharacterId);
      return this.characters.filter((c) => !ids.has(c.id));
    },
    async addMembers() {
      if (!this.sessionId || !this.memberAddSelected.length) { this.membersOpen = false; return; }
      const wasGroup = this.currentIsGroup;
      try {
        await this.api("/sessions/" + this.sessionId + "/members", {
          method: "POST", body: JSON.stringify({ character_ids: this.memberAddSelected }),
        });
        this.membersOpen = false;
        this.memberAddSelected = [];
        // Чат мог стать группой: из личных он уходит, в группах появляется.
        await this.syncChatList(this.sessionId);
        // Чат стал групповым — переоткрываем как группу, чтобы подхватить участников.
        if (!wasGroup) { this.selectedCharacterId = null; await this.openSession({ id: this.sessionId }); }
        await this.loadMessages();
        this.showToast("Участники добавлены");
      } catch (e) { this.showToast("Не удалось добавить: " + e.message); }
    },
    async removeMember(m) {
      if (!(await this.askConfirm("Убрать «" + m.name + "» из группы? Его прошлые реплики останутся.", { okText: "Убрать" }))) return;
      try {
        await this.api("/sessions/" + this.sessionId + "/members/" + m.id, { method: "DELETE" });
        await this.syncChatList(this.sessionId);
        this.showToast("Персонаж убран из группы");
      } catch (e) { this.showToast(e.message); }
    },

    // ---------- База знаний чата (постоянные справочные файлы) ----------
    async openKnowledge() {
      if (!this.sessionId) return;
      this.kbOpen = true;
      await this.loadKnowledge();
    },
    async loadKnowledge() {
      if (!this.sessionId) { this.kbFiles = []; return; }
      try { this.kbFiles = await this.api("/sessions/" + this.sessionId + "/knowledge"); }
      catch (e) { this.kbFiles = []; }
    },
    // Загрузка файлов в базу знаний (читаем в data:URI и шлём на сервер).
    onKnowledgeFiles(e) {
      const files = [...(e.target.files || [])];
      e.target.value = "";
      for (const file of files) this._uploadKnowledge(file);
    },
    _uploadKnowledge(file) {
      const reader = new FileReader();
      this.kbUploading = true;
      reader.onload = async () => {
        try {
          let type = "document";
          const mime = file.type || "application/octet-stream";
          if (mime.startsWith("image")) type = "image";
          else if (mime.startsWith("audio")) type = "audio";
          else if (mime.startsWith("video")) type = "video";
          await this.api("/sessions/" + this.sessionId + "/knowledge", {
            method: "POST",
            body: JSON.stringify({ type, data: reader.result, mime, name: file.name || "файл" }),
          });
          await this.loadKnowledge();
        } catch (err) { this.showToast("Не удалось добавить в базу знаний: " + err.message); }
        finally { this.kbUploading = false; }
      };
      reader.onerror = () => { this.kbUploading = false; this.showToast("Не удалось прочитать файл: " + (file.name || "")); };
      reader.readAsDataURL(file);
    },
    async deleteKnowledge(f) {
      if (!(await this.askConfirm("Удалить «" + f.name + "» из базы знаний?", { okText: "Удалить" }))) return;
      try {
        await this.api("/knowledge/" + f.id, { method: "DELETE" });
        await this.loadKnowledge();
      } catch (e) { this.showToast(e.message); }
    },
    kbIcon(f) {
      if (f.kind === "image") return "🖼";
      if (f.kind === "audio") return "🎵";
      if (f.kind === "video") return "🎬";
      return "📄";
    },
    // ---------- Режиссёрские команды (группа): вставка +Имя / -Имя ----------
    // sign «+» — вызвать (порядок кликов = порядок ответов), «-» — исключить.
    dirInsert(sign, name) {
      const token = sign + name;
      const cur = this.input;
      // Не дублируем уже добавленную команду.
      const re = new RegExp("(^|\\s)" + sign.replace("-", "\\-") + name + "(?=\\s|$)");
      if (re.test(cur)) return;
      this.input = (cur ? cur.replace(/\s+$/, "") + " " : "") + token + " ";
      this.$nextTick(() => { if (this.$refs.composer) { this.$refs.composer.focus(); this.autoGrow({ target: this.$refs.composer }); } });
    },
    // Загрузка окна сообщений (не всей истории). fresh=true — свежее открытие чата
    // (последние 40 + скролл вниз); иначе обновление текущего окна (после хода/правки).
    async loadMessages(fresh = false) {
      if (!this.sessionId) return;
      // Если пользователь прокрутил вверх и читает историю — НЕ дёргаем его вниз
      // при обновлении (после хода/правки). Прыгаем вниз только у нижней кромки/на открытии.
      const el = this.$refs.messages;
      const wasNearBottom = fresh || !el || (el.scrollHeight - el.scrollTop - el.clientHeight < 160);
      if (fresh) { this.messages = []; this.noMoreMessages = false; }
      // Окно = столько же, сколько уже показано (сохраняем прокрутку вверх), но не всё:
      // на открытии — msgPageSize (настройка), максимум 400.
      const limit = Math.min(400, Math.max(this.msgPageSize, this.messages.length + 2));
      const rows = await this.api("/sessions/" + this.sessionId + "/messages?limit=" + limit);
      this.messages = rows;
      this.noMoreMessages = rows.length < limit; // получили меньше лимита → старых нет
      if (wasNearBottom) this.scrollDown();
    },
    // Подгрузка порции более старых сообщений при скролле вверх (сохраняем позицию).
    async loadOlder() {
      if (!this.sessionId || this.loadingOlder || this.noMoreMessages || !this.messages.length) return;
      const oldest = this.messages[0];
      if (!oldest || oldest.id === "tmp") return;
      this.loadingOlder = true;
      const el = this.$refs.messages;
      const prevH = el ? el.scrollHeight : 0;
      const page = this.msgPageSize;
      try {
        const older = await this.api(
          "/sessions/" + this.sessionId + "/messages?before=" + oldest.id + "&limit=" + page
        );
        if (older.length < page) this.noMoreMessages = true;
        if (older.length) {
          this.messages = older.concat(this.messages);
          // Держим кадр на месте: добавили сверху -> компенсируем прирост высоты.
          this.$nextTick(() => { if (el) el.scrollTop += el.scrollHeight - prevH; });
        }
      } catch (e) { /* тихо: подгрузка не критична */ }
      finally { this.loadingOlder = false; }
    },
    onMessagesScroll(e) {
      if (e.target.scrollTop < 120 && !this.loadingOlder && !this.noMoreMessages) {
        this.loadOlder();
      }
      // Показываем навигацию только когда от низа реально отъехали: у самого низа
      // (обычное чтение свежих реплик) она была бы лишним элементом на экране.
      const el = e.target;
      this.awayFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight > 300;
      this._syncComposerH();
    },
    // Высота поля ввода для отступа панели навигации. Обновляем при прокрутке и
    // при росте textarea (autoGrow) — этого достаточно: панель видна только при
    // прокрутке, а растёт композер только во время набора.
    _syncComposerH() {
      const c = document.querySelector(".composer");
      if (c && c.offsetHeight && c.offsetHeight !== this.composerH) this.composerH = c.offsetHeight;
    },

    // ---------- Перемещение по чату ----------
    // Список сообщений с их положением внутри прокручиваемого контейнера.
    _msgTops() {
      const box = this.$refs.messages;
      if (!box) return [];
      return [...box.querySelectorAll(".msg")].map((el) => ({
        el,
        top: el.offsetTop - box.offsetTop,
      }));
    },
    scrollToBottom(smooth = true) {
      const box = this.$refs.messages;
      if (!box) return;
      box.scrollTo({ top: box.scrollHeight, behavior: smooth ? "smooth" : "auto" });
    },
    // К самому началу чата. История подгружается порциями по мере прокрутки вверх,
    // поэтому сначала дотягиваем недостающее, и только потом прыгаем — иначе
    // «начало» оказалось бы началом загруженного куска, а не разговора.
    async scrollToChatStart() {
      const box = this.$refs.messages;
      if (!box) return;
      this.jumpBusy = true;
      try {
        let guard = 40;   // предохранитель от бесконечного цикла на огромном чате
        while (!this.noMoreMessages && guard-- > 0) {
          const before = this.messages.length;
          await this.loadOlder();
          if (this.messages.length === before) break;   // ничего не пришло — выходим
        }
        await this.$nextTick();
        box.scrollTo({ top: 0, behavior: "smooth" });
      } finally {
        this.jumpBusy = false;
      }
    },
    // Шаг по сообщениям. Ориентируемся на верхнюю кромку экрана: следующим
    // считается ближайшее сообщение, начало которого ниже текущего положения.
    jumpMessage(dir) {
      const box = this.$refs.messages;
      if (!box) return;
      const tops = this._msgTops();
      if (!tops.length) return;
      const cur = box.scrollTop;
      const eps = 8;   // допуск, иначе «вверх» упирается в текущее же сообщение
      const target = dir > 0
        ? tops.find((m) => m.top > cur + eps)
        : [...tops].reverse().find((m) => m.top < cur - eps);
      if (!target) {
        if (dir > 0) this.scrollToBottom();
        else if (!this.noMoreMessages) this.scrollToChatStart();
        else box.scrollTo({ top: 0, behavior: "smooth" });
        return;
      }
      box.scrollTo({ top: target.top, behavior: "smooth" });
      this._flashMessage(target.el);
    },
    // К началу ЭТОГО сообщения: у длинного ответа его верх легко уехал за экран,
    // и вернуться к нему прокруткой на глаз неудобно.
    scrollMessageStart(id) {
      const box = this.$refs.messages;
      const el = box && box.querySelector('[data-mid="' + id + '"]');
      if (!box || !el) return;
      box.scrollTo({ top: el.offsetTop - box.offsetTop, behavior: "smooth" });
      this._flashMessage(el);
    },
    // Короткая подсветка: после прыжка видно, куда именно попал.
    _flashMessage(el) {
      el.classList.remove("jump-flash");
      void el.offsetWidth;              // перезапуск анимации
      el.classList.add("jump-flash");
      setTimeout(() => el.classList.remove("jump-flash"), 900);
    },
    // Клавиши работают, только когда не печатаешь: иначе Home/End в поле ввода
    // прыгали бы по чату вместо перемещения курсора в тексте.
    _onNavKey(ev) {
      const t = ev.target;
      const typing = t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.isContentEditable);
      if (typing || !this.sessionId) return;
      if (ev.altKey && ev.key === "ArrowUp") { ev.preventDefault(); this.jumpMessage(-1); }
      else if (ev.altKey && ev.key === "ArrowDown") { ev.preventDefault(); this.jumpMessage(1); }
      else if (ev.key === "Home" && !ev.ctrlKey) { ev.preventDefault(); this.scrollToChatStart(); }
      else if (ev.key === "End" && !ev.ctrlKey) { ev.preventDefault(); this.scrollToBottom(); }
    },

    // ---------- WebSocket стриминг ----------
    connectWs() {
      // Поколение сокета. Обработчики старого сокета могут сработать уже ПОСЛЕ
      // того, как открыт новый (закрытие приходит асинхронно), и без этой метки
      // они бы гасили connected у живого соединения.
      const gen = (this._wsGen = (this._wsGen || 0) + 1);
      if (this.ws) {
        // Мы сами закрываем сокет (смена чата) — это не обрыв сети, не надо
        // дослушивать старую генерацию в активный (уже другой) чат.
        try { this.ws.close(); } catch (e) {}
      }
      this._stopHeartbeat();
      if (this._wsReconnectTimer) { clearTimeout(this._wsReconnectTimer); this._wsReconnectTimer = null; }
      const sid = this.sessionId; // для реконнекта: переподключаемся только к ЭТОМУ чату
      const proto = location.protocol === "https:" ? "wss" : "ws";
      const qs = [];
      if (this.accessCode) qs.push("code=" + encodeURIComponent(this.accessCode));
      if (this.userToken) qs.push("token=" + encodeURIComponent(this.userToken));
      const q = qs.length ? "?" + qs.join("&") : "";
      this.ws = new WebSocket(proto + "://" + location.host + "/ws/chat/" + this.sessionId + q);
      this.ws.onopen = () => {
        if (gen !== this._wsGen) return;  // открылся уже неактуальный сокет
        this.connected = true;
        this._wsRetry = 0;
        this._startHeartbeat();
      };
      this.ws.onclose = () => {
        // Закрылся СТАРЫЙ сокет, пока новый уже работает — не наше дело.
        //
        // Раньше здесь стоял общий флаг _intentionalClose, и он давал баг,
        // из-за которого связь не восстанавливалась без перезагрузки страницы:
        // при вызове connectWs на УЖЕ закрытом сокете close() не порождает
        // события, флаг оставался true, и следующий НАСТОЯЩИЙ обрыв считался
        // намеренным — реконнект не планировался вообще никогда.
        if (gen !== this._wsGen) return;
        this.connected = false;
        this._stopHeartbeat();
        // Непреднамеренный обрыв: дослушиваем активную генерацию через SSE.
        if (this.streaming && this.currentJobId) this.resumeSSE(this.currentJobId);
        // Автопереподключение с бэкоффом: сеть моргнула или сервер перезапустился.
        // Потолок 8 с, а не 15: дольше человек воспринимает как «зависло».
        const delay = Math.min(8000, 1000 * Math.pow(2, this._wsRetry || 0));
        this._wsRetry = (this._wsRetry || 0) + 1;
        this._wsReconnectTimer = setTimeout(() => {
          this._wsReconnectTimer = null;
          if (this.sessionId === sid) this.connectWs();
        }, delay);
      };
      // Без этого неудачная попытка подключения иногда висит без onclose, и
      // реконнект не планируется: закрываем сами, дальше отработает onclose.
      this.ws.onerror = () => { try { this.ws && this.ws.close(); } catch (e) {} };
      this.ws.onmessage = (e) => {
        this._hbPending = false;   // любое сообщение — признак живого соединения
        const data = JSON.parse(e.data);
        if (data && data.type === "pong") return;  // служебный ответ, в ленту не идёт
        this.onWsEvent(data);
      };
    },

    // ---------- Живучесть соединения ----------
    // Пауза в переписке — норма: люди читают ответ по несколько минут. Но
    // молчащий сокет закрывают промежуточные прокси, мобильные операторы и
    // энергосбережение телефона через 30-60 секунд тишины, и выглядело это как
    // «постоянный реконнект на ровном месте». Пинг раз в 25 секунд держит канал.
    _startHeartbeat() {
      this._stopHeartbeat();
      this._hbPending = false;
      this._hbTimer = setInterval(() => {
        const ws = this.ws;
        if (!ws || ws.readyState !== 1) return;
        // На прошлый пинг не ответили — канал мёртв, хотя браузер этого ещё не
        // заметил (полуоткрытый TCP). Закрываем сами, чтобы сработал реконнект.
        if (this._hbPending) {
          this._hbPending = false;
          try { ws.close(); } catch (e) {}
          return;
        }
        this._hbPending = true;
        try { ws.send(JSON.stringify({ type: "ping" })); } catch (e) {}
      }, 25000);
    },
    _stopHeartbeat() {
      if (this._hbTimer) { clearInterval(this._hbTimer); this._hbTimer = null; }
      this._hbPending = false;
    },
    // Возвращение к вкладке, восстановление сети, фокус окна — это момент, когда
    // человек СМОТРИТ на приложение и ждёт, что оно работает. Ждать бэкофф здесь
    // нельзя: на телефоне таймеры в фоне заморожены, поэтому запланированный
    // реконнект мог не сработать вовсе, и связь не поднималась до перезагрузки.
    _bindWake() {
      if (this._wakeBound) return;
      this._wakeBound = true;
      const wake = () => {
        if (document.visibilityState !== "visible") return;
        if (!this.sessionId) return;
        if (this.ws && this.ws.readyState === 1) return;   // уже живы
        if (this.ws && this.ws.readyState === 0) return;   // подключаемся прямо сейчас
        this._wsRetry = 0;                                  // человек вернулся — не томим
        if (this._wsReconnectTimer) {
          clearTimeout(this._wsReconnectTimer);
          this._wsReconnectTimer = null;
        }
        this.connectWs();
      };
      document.addEventListener("visibilitychange", wake);
      window.addEventListener("online", wake);
      window.addEventListener("focus", wake);
    },
    onWsEvent(ev) {
      this._lastEvtAt = Date.now(); // метка для сторожа зависшего стриминга
      // Нейросеть подала признаки жизни — плашка «обрабатывает файл» больше не нужна.
      if (ev.type === "token" || ev.type === "thought" || ev.type === "done" || ev.type === "error") {
        this.processingNote = false;
      }
      if (ev.type === "job") {
        this.currentJobId = ev.job_id;
        // «job» сервер шлёт ПОСЛЕ того, как сохранил реплику пользователя (или
        // начал перегенерацию). Это первый момент, когда чат честно поднимается
        // в списке: ждать «done» значило бы держать его на старом месте всю
        // генерацию, а длинный ответ идёт минуту.
        this.notifyChatListChanged(this.sessionId);
      }
      else if (ev.type === "waiting") {
        // Пауза между ответами персонажей в группе (защита от 429).
        this.groupWaiting = Math.round(ev.seconds || 0);
      } else if (ev.type === "speaker") {
        // Групповой чат: начинается реплика нового персонажа.
        this.groupWaiting = 0;
        this.liveBubbles.push({ name: ev.name, content: "" });
        this.scrollDown();
      } else if (ev.type === "token") {
        if (this.liveBubbles.length) this.liveBubbles[this.liveBubbles.length - 1].content += ev.content;
        else this.currentReply += ev.content;
        this.scrollDown();
      } else if (ev.type === "thought") {
        // Размышления модели: копятся отдельно от ответа, показываются свёрнуто.
        this.currentThought += ev.content;
      } else if (ev.type === "speaker_done") {
        // ничего: пузырь остаётся на экране до перечитки истории
      } else if (ev.type === "fallback") {
        // Основная модель не ответила — сервер повторяет ход запасной.
        // Частичный текст основной сбрасываем: ответ придёт с чистого листа.
        this.currentReply = "";
        this.currentThought = "";
        this.liveBubbles = [];
        this.showToast("⚠ Основная модель не ответила — пробую запасную: " + (ev.model || ""));
      } else if (ev.type === "done") this.finishStream();
      else if (ev.type === "error") {
        // Ошибку НЕ прячем — показываем баннером, чтобы было видно причину.
        this.chatError = ev.content || "неизвестная ошибка";
        this.finishStream();
      }
    },
    async finishStream() {
      this.streaming = false;
      this.currentJobId = null;
      this.processingNote = false;
      this.groupWaiting = 0;
      // Сервер — источник истины: перечитываем сообщения (там уже новый ответ/свайп).
      await this.loadMessages();
      // Ответ разобран на сервере — открытая «Хроника» перечитает состояние.
      this.horaeTick += 1;
      this._horaeRecheck();
      // Новая реплика двигает чат вверх (среди своих: пины не перепрыгивает)
      // и меняет превью в сайдбаре. Ошибка и остановка приходят сюда же, и
      // список нужен и им: реплика пользователя уже сохранена.
      this.notifyChatListChanged(this.sessionId);
      this.currentReply = "";
      this.currentThought = "";
      this.liveBubbles = [];
      if (this.soundOn) this.playChime();
    },
    playChime() {
      try {
        const ctx = new (window.AudioContext || window.webkitAudioContext)();
        const o = ctx.createOscillator();
        const g = ctx.createGain();
        o.connect(g); g.connect(ctx.destination);
        o.type = "sine"; o.frequency.value = 660;
        g.gain.setValueAtTime(0.0001, ctx.currentTime);
        g.gain.exponentialRampToValueAtTime(0.18, ctx.currentTime + 0.02);
        g.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + 0.4);
        o.start(); o.stop(ctx.currentTime + 0.4);
        o.onended = () => ctx.close();
      } catch (e) {}
    },
    resumeSSE(jobId) {
      if (this._sse) { try { this._sse.close(); } catch (e) {} this._sse = null; }
      // SSE отдаёт НАКОПЛЕННЫЙ буфер целиком — сбрасываем live-текст,
      // иначе уже полученные по WS токены задвоятся на экране.
      this.currentReply = "";
      this.currentThought = "";
      this.liveBubbles = [];
      const es = (this._sse = new EventSource("/sse/job/" + jobId));
      es.onmessage = (e) => {
        const ev = JSON.parse(e.data);
        this.onWsEvent(ev);
        if (ev.type === "done" || ev.type === "error") { es.close(); this._sse = null; }
      };
      es.onerror = () => {
        // Задача уже завершена и очищена (404) или SSE недоступен: ответ, если он
        // родился, давно сохранён в БД — перечитываем её и разблокируем интерфейс,
        // вместо того чтобы вечно крутить «печатает…».
        try { es.close(); } catch (e) {}
        this._sse = null;
        if (this.streaming) this.finishStream();
      };
    },
    // Авторесайз поля ввода под содержимое (до max-height из CSS, дальше — скролл).
    autoGrow(e) {
      const el = (e && e.target) || this.$refs.composer;
      if (!el) return;
      el.style.height = "auto";
      el.style.height = Math.min(el.scrollHeight, 200) + "px";
      this._syncComposerH();   // панель навигации висит над композером
    },
    resetComposerHeight() {
      this.$nextTick(() => { if (this.$refs.composer) this.$refs.composer.style.height = "auto"; });
    },
    // Жёсткий обработчик Enter: десктоп — отправка, Shift+Enter — перенос;
    // тач-устройства — Enter ВСЕГДА перенос (отправка только кнопкой).
    onComposerKeydown(e) {
      if (e.key !== "Enter" || e.isComposing) return;  // не мешаем IME
      if (e.shiftKey || this.isTouch) return;          // перенос строки
      e.preventDefault();
      this.submitComposer();
    },
    // Единственная точка отправки: и кнопка, и Enter зовут её. Пока действие
    // выбиралось в двух местах (цепочка v-else-if в шаблоне и if-цепочка в
    // обработчике Enter), они могли разойтись между собой.
    submitComposer() {
      if (this.streaming) return;
      if (this.composerMode === "canvasCmd") return this.applyCanvasCmd();
      if (this.composerMode === "art") return this.sendArt();
      return this.send();
    },
    // Надпись на кнопке отправки берётся из того же режима, что и действие.
    submitLabel() {
      if (this.waitingFiles) return "⏳ файлы…";
      if (this.composerMode === "canvasCmd") return "✨ Применить";
      if (this.composerMode === "canvasGen") return this.canvasGenerating ? "⏳…" : "📄 Создать";
      if (this.composerMode === "art") return "🎨 Сгенерировать";
      return "Отправить";
    },
    // Единый инпут в режиме команды Канвасу: применяем введённое как инструкцию ИИ
    // (к выделенному фрагменту, если он есть, иначе ко всему документу).
    async applyCanvasCmd() {
      const cmd = this.input.trim();
      if (!cmd || !this.canvas) return;
      this.input = "";
      this.resetComposerHeight();
      this.composerMode = "text";
      await this.reviseCanvas(cmd);
    },
    async send() {
      const content = this.input.trim();
      if ((!content && this.pendingAttachments.length === 0) || !this.connected || this.streaming) return;
      // Дожидаемся дочитывания ВСЕХ файлов сообщения, прежде чем отправлять — иначе
      // сообщение могло уйти без ещё не загруженного вложения (гонка с FileReader).
      if (this.attachmentsLoading) {
        this.waitingFiles = true;
        await this._awaitAttachments();
        this.waitingFiles = false;
        if (!this.connected || this.streaming) return; // состояние изменилось, пока ждали
      }
      this._dropBadAttachments(); // выкидываем не прочитавшиеся вложения
      if (!content && this.pendingAttachments.length === 0) return; // всё отвалилось
      // ===== Умная маршрутизация интентов для Canvas =====
      // Явный триггер «📄 Документ»: новый файл, ИЛИ правка открытого (если не «с нуля»).
      if (this.composerMode === "canvasGen") {
        const atts = this._cleanAtts(this.pendingAttachments);
        this.input = ""; this.pendingAttachments = []; this.composerMode = "text"; this.resetComposerHeight();
        if (this.canvasOpen && this.canvas && !this._isNewCanvasIntent(content)) this.editOpenCanvas(content);
        else this.canvasGenerate(content, atts);
        return;
      }
      // Канвас ОТКРЫТ и запрос контекстный: «новый …» → новый файл; правка → мутируем открытый.
      if (this.canvasOpen && this.canvas && content) {
        if (this._isNewCanvasIntent(content)) {
          const atts = this._cleanAtts(this.pendingAttachments);
          this.input = ""; this.pendingAttachments = []; this.resetComposerHeight();
          this.canvasGenerate(content, atts);
          return;
        }
        if (this._isEditIntent(content)) {
          this.input = ""; this.resetComposerHeight();
          this.editOpenCanvas(content);
          return;
        }
      }
      this.chatError = "";
      const pend = this.pendingAttachments.slice();
      const attFiles = this._attFiles || {};
      const bigList = pend.filter((a) => !a.data && attFiles[a.id]); // файлы для multipart
      const attachments = this._cleanAtts(pend);                     // инлайновые (data:URI)
      const replyTo = this.replyToId;
      // Оптимистично показываем своё сообщение сразу; для больших файлов
      // в пузыре работает лёгкое превью (objectURL), а не base64.
      const displayAtts = pend.map((a) => ({
        type: a.type, mime: a.mime, name: a.name, size: a.size, data: a.data, preview: a.preview,
      }));
      this.messages.push({ id: "tmp", role: "user", content, attachments: displayAtts, swipes: [content], active_swipe: 0, created_at: new Date().toISOString() });
      this.currentReply = "";
      this.currentThought = "";
      this.liveBubbles = [];
      this.streaming = true;
      this._lastEvtAt = Date.now();
      this.input = "";
      this.pendingAttachments = [];
      this.replyToId = null;
      this.resetComposerHeight();
      this.scrollDown();
      if (bigList.length) {
        // БОЛЬШИЕ файлы: multipart — браузер шлёт байты прямо с диска (без
        // base64 в памяти), сервер сам кодирует. Прогресс загрузки — тот же XHR.
        const metas = [];
        let fi = 0;
        for (const a of pend) {
          if (a.data) metas.push({ type: a.type, data: a.data, mime: a.mime, name: a.name });
          else metas.push({ type: a.type, mime: a.mime, name: a.name, file_index: fi++ });
        }
        const fd = new FormData();
        fd.append("payload", JSON.stringify({
          content, attachments: metas, params: this.params, reply_to_message_id: replyTo,
        }));
        for (const a of bigList) fd.append("files", attFiles[a.id], a.name || "file");
        for (const a of bigList) delete attFiles[a.id];
        this._postWithProgress("/sessions/" + this.sessionId + "/send_form", fd).then((r) => {
          this.currentJobId = r.job_id;
          this.processingNote = true; // файл на сервере — дальше работает нейросеть
          // HTTP-ход не получает события «job» по сокету: реплика сохранена,
          // когда сервер вернул job_id, — здесь чат и поднимается в списке.
          this.notifyChatListChanged(this.sessionId);
          this.resumeSSE(r.job_id);
        }).catch((e) => {
          this.chatError = "Не удалось отправить файл: " + e.message;
          this.finishStream();
        });
      } else if (attachments.length) {
        // Вложения (особенно аудио/видео) не влезают в WebSocket-кадр (~16 МБ) —
        // отправляем ход по HTTP с ПРОГРЕССОМ загрузки, ответ слушаем по SSE.
        this._postWithProgress("/sessions/" + this.sessionId + "/send", {
          content, attachments, params: this.params, reply_to_message_id: replyTo,
        }).then((r) => {
          this.currentJobId = r.job_id;
          this.processingNote = true;
          this.notifyChatListChanged(this.sessionId);
          this.resumeSSE(r.job_id);
        }).catch((e) => {
          this.chatError = "Не удалось отправить вложение: " + e.message;
          this.finishStream();
        });
      } else {
        this.ws.send(JSON.stringify({
          type: "user_message", content,
          attachments, params: this.params,
          reply_to_message_id: replyTo,
        }));
      }
    },
    regenerate() {
      if (!this.connected || this.streaming) return;
      this.chatError = "";
      this.currentReply = "";
      this.currentThought = "";
      this.liveBubbles = [];
      this.streaming = true;
      this._lastEvtAt = Date.now();
      this.ws.send(JSON.stringify({ type: "regenerate", params: this.params }));
    },
    // Повторить последний ход: если ответ так и не родился (ошибка/обрыв/остановка) —
    // сервер сгенерирует его заново БЕЗ дублирования реплики пользователя;
    // если ответ есть — добавит новый свайп (как обычная перегенерация).
    // useFallback=true — повторить ход ЗАПАСНОЙ моделью (кнопка в баннере ошибки).
    retryGeneration(useFallback = false) {
      if (!this.connected || this.streaming) return;
      this.chatError = "";
      this.currentReply = "";
      this.currentThought = "";
      this.liveBubbles = [];
      this.streaming = true;
      this._lastEvtAt = Date.now();
      // Строгое сравнение: из шаблона метод зовут как обработчик клика,
      // и первым аргументом прилетает MouseEvent (он truthy).
      const params = (useFallback === true && this.fallbackModel)
        ? { ...this.params, model: this.fallbackModel }
        : this.params;
      this.ws.send(JSON.stringify({ type: "retry", params }));
      this.scrollDown();
    },
    stop() {
      if (!this.streaming) return;
      // Отмена по id задачи (HTTP) работает и для WS-, и для HTTP-хода (SSE);
      // сервер отменит генерацию и пришлёт done. WS-стоп — как запасной путь.
      if (this.currentJobId) {
        this.api("/jobs/" + this.currentJobId + "/cancel", { method: "POST" }).catch(() => {});
      }
      if (this.ws && this.connected) {
        this.ws.send(JSON.stringify({ type: "stop" }));
        // Если done потерялся (сокет умер молча) — разблокируемся сами.
        setTimeout(() => { if (this.streaming) this.finishStream(); }, 4000);
      } else {
        // Соединения нет: просто снимаем блокировку и перечитываем БД
        // (частичный ответ, если был, сервер уже сохранил).
        this.finishStream();
      }
    },

    // ---------- Действия над сообщениями ----------
    async swipe(msg, dir) {
      const total = (msg.swipes || []).length;
      const next = msg.active_swipe + dir;
      if (next < 0) return;
      if (next >= total) {
        // Свайп вправо за последний вариант = сгенерировать новый (как в SillyTavern).
        if (msg.id === this.lastAssistantId) this.regenerate();
        return;
      }
      await this.api("/messages/" + msg.id, { method: "PATCH", body: JSON.stringify({ active_swipe: next }) });
      this.loadMessages();
      // Другой вариант ответа — другое превью последней реплики в сайдбаре.
      this.notifyChatListChanged(this.sessionId);
    },
    startEdit(msg) {
      this.editingId = msg.id;
      this.editingText = msg.content;
      // Авто-фокус + высота под содержимое: правка начинается сразу, без лишних кликов.
      this.$nextTick(() => {
        let el = this.$refs.editArea;
        if (Array.isArray(el)) el = el[0];
        if (el) {
          el.focus();
          el.style.height = "auto";
          el.style.height = Math.min(el.scrollHeight + 2, 340) + "px";
        }
      });
    },
    autoGrowEdit(e) {
      const el = e.target;
      el.style.height = "auto";
      el.style.height = Math.min(el.scrollHeight + 2, 340) + "px";
    },
    onEditKeydown(e) {
      if (e.key === "Escape") { e.preventDefault(); this.editingId = null; return; }
      if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); this.saveEdit(); }
    },
    async saveEdit() {
      await this.api("/messages/" + this.editingId, { method: "PATCH", body: JSON.stringify({ content: this.editingText }) });
      this.editingId = null;
      this.loadMessages();
      this.notifyChatListChanged(this.sessionId);   // правка последней реплики меняет превью
    },
    async deleteMessage(msg) {
      if (!(await this.askConfirm("Удалить сообщение?", { okText: "Удалить" }))) return;
      await this.api("/messages/" + msg.id, { method: "DELETE" });
      this.loadMessages();
      // Удалили последнюю реплику — у чата сменились превью, время и место в списке.
      this.notifyChatListChanged(this.sessionId);
    },
    // Скопировать текст сообщения в буфер обмена одной кнопкой.
    async copyMessage(m) {
      const text = m.content || "";
      try {
        await navigator.clipboard.writeText(text);
      } catch (e) {
        // Резерв для http/старых браузеров, где clipboard API недоступен.
        const ta = document.createElement("textarea");
        ta.value = text; ta.style.position = "fixed"; ta.style.opacity = "0";
        document.body.appendChild(ta); ta.select();
        try { document.execCommand("copy"); } catch (e2) {}
        document.body.removeChild(ta);
      }
      this.showToast("Скопировано");
    },

    // ---------- Вложения (мультимодальность) ----------
    attachLabel(a) {
      if (a.type === "document") return "📄 " + (a.name || "файл");
      if (a.type === "audio") return "🎤 аудио";
      if (a.type === "video") return "🎬 " + (a.name || "видео");
      return "🖼 фото";
    },
    // Иконка по типу вложения (для подписи с именем файла).
    attIcon(a) {
      const t = a.type, mime = a.mime || "";
      if (t === "image" || mime.startsWith("image")) return "🖼";
      if (t === "audio" || mime.startsWith("audio")) return "🎵";
      if (t === "video" || mime.startsWith("video")) return "🎬";
      return "📄";
    },
    // Добавить файлы во вложения текущего сообщения (общий код для 📎, вставки и DnD).
    // Плашка появляется СРАЗУ со спиннером, а data дочитывается асинхронно — так видно,
    // что файл грузится, а send() дожидается готовности всех вложений (см. attachmentsLoading).
    addFiles(files) {
      // Размер НЕ ограничиваем: сколько реально пройдёт — зависит от провайдера.
      // Маленькие файлы читаем в data:URI (нужны для превью и истории «как раньше»),
      // а БОЛЬШИЕ не читаем вовсе: браузер отправит их с диска multipart'ом
      // (см. send) — без base64 в памяти (на телефоне 64-МБ видео в base64 —
      // это ~350 МБ RAM и зависший интерфейс) и на треть меньше трафика.
      const INLINE_MAX = 6 * 1024 * 1024;
      this._attFiles = this._attFiles || {}; // File-объекты вне реактивности Vue
      for (const file of [...files]) {
        const mime = file.type || "application/octet-stream";
        let type = "image";
        if (mime.startsWith("audio")) type = "audio";
        else if (mime.startsWith("video")) type = "video";
        else if (!mime.startsWith("image")) type = "document"; // pdf/docx/txt/...
        const raw = {
          id: (this._attSeq = (this._attSeq || 0) + 1),
          type, mime, name: file.name || "файл", size: file.size || 0,
          data: null, preview: null, loading: true, error: false,
        };
        this.pendingAttachments.push(raw);
        // Берём РЕАКТИВНУЮ ссылку из массива (Vue оборачивает элемент) — иначе
        // мутация полей не вызовет перерисовку спиннера/превью.
        const att = this.pendingAttachments[this.pendingAttachments.length - 1];
        if (file.size > INLINE_MAX) {
          // Большой файл: оставляем на диске, превью — лёгкий objectURL.
          this._attFiles[raw.id] = file;
          if (type === "image" || type === "video") {
            try { att.preview = URL.createObjectURL(file); } catch (e) {}
          }
          att.loading = false;
          continue;
        }
        const reader = new FileReader();
        reader.onload = () => { att.data = reader.result; att.loading = false; };
        reader.onerror = () => {
          att.error = true; att.loading = false;
          this.showToast("Не удалось прочитать файл: " + att.name);
        };
        reader.readAsDataURL(file);
      }
    },
    // Авторизация в query — для <img>/<audio>, которые не умеют слать заголовки.
    _authQuery() {
      if (this.userToken) return "token=" + encodeURIComponent(this.userToken);
      if (this.accessCode) return "access_code=" + encodeURIComponent(this.accessCode);
      return "";
    },
    // URL вложения сохранённого сообщения (данные грузятся лениво, не в списке чата).
    attUrl(m, i) {
      const q = this._authQuery();
      return "/api/messages/" + m.id + "/att/" + i + (q ? "?" + q : "");
    },
    fmtSize(bytes) {
      if (!bytes) return "";
      if (bytes < 1024) return bytes + " Б";
      if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(0) + " КБ";
      return (bytes / 1024 / 1024).toFixed(1) + " МБ";
    },
    removeAttachment(i) {
      const a = this.pendingAttachments[i];
      if (a) {
        if (this._attFiles) delete this._attFiles[a.id];
        if (a.preview) { try { URL.revokeObjectURL(a.preview); } catch (e) {} }
      }
      this.pendingAttachments.splice(i, 1);
    },
    // Промис, который завершается, когда ВСЕ вложения дочитаны (data готова или ошибка).
    _awaitAttachments() {
      return new Promise((resolve) => {
        const check = () => (this.attachmentsLoading ? setTimeout(check, 60) : resolve());
        check();
      });
    },
    // Убрать неудавшиеся/пустые вложения перед отправкой.
    // Валидное вложение: либо дочитанный data:URI, либо File на диске (multipart).
    _dropBadAttachments() {
      const files = this._attFiles || {};
      this.pendingAttachments = this.pendingAttachments.filter(
        (a) => (a.data || files[a.id]) && !a.error
      );
    },
    // Чистый payload для бэкенда: только поля AttachmentIn (без служебных id/size/loading).
    // Файловые (без data) вложения сюда не попадают — их шлёт multipart-путь send().
    _cleanAtts(list) {
      return list.filter((a) => a.data)
        .map((a) => ({ type: a.type, data: a.data, mime: a.mime, name: a.name }));
    },
    onAttach(e) {
      this.addFiles(e.target.files);   // несколько файлов сразу
      e.target.value = "";
    },
    // Вставка из буфера обмена (Ctrl+V): скриншоты и скопированные картинки/файлы
    // прикрепляются как вложения; обычный текст вставляется как всегда.
    onPaste(e) {
      const items = (e.clipboardData && e.clipboardData.items) || [];
      const files = [];
      for (const it of items) {
        if (it.kind === "file") {
          const f = it.getAsFile();
          if (f) files.push(f);
        }
      }
      if (files.length) {
        e.preventDefault(); // не вставляем «мусорный» текст рядом с картинкой
        this.addFiles(files);
      }
    },
    // Drag&drop файла в окно чата — прикрепляем к текущему сообщению (как стейт).
    onDrop(e) {
      this.dragOver = false;
      if (!this.sessionId) return;
      const files = e.dataTransfer && e.dataTransfer.files;
      if (files && files.length) this.addFiles(files);
    },

    // ---------- Запись голоса: конвертация в MP3 (webm нейросеть не понимает) ----------
    // MediaRecorder в Chrome/Edge пишет audio/webm (Opus), а Gemini принимает
    // wav/mp3/ogg/flac/aac. Поэтому запись перекодируем: декодируем в PCM
    // (decodeAudioData умеет webm/ogg) и кодируем в MP3 через lamejs (моно, 128 кбит/с).
    async _decodeToPcm(blob) {
      const AC = window.AudioContext || window.webkitAudioContext;
      const ctx = new AC();
      try {
        return await ctx.decodeAudioData(await blob.arrayBuffer());
      } finally {
        if (ctx.close) try { ctx.close(); } catch (e) {}
      }
    },
    _bufferToInt16Mono(buf) {
      const n = buf.length;
      const c0 = buf.getChannelData(0);
      const c1 = buf.numberOfChannels > 1 ? buf.getChannelData(1) : null;
      const out = new Int16Array(n);
      for (let i = 0; i < n; i++) {
        let s = c1 ? (c0[i] + c1[i]) * 0.5 : c0[i];
        s = Math.max(-1, Math.min(1, s));
        out[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
      }
      return out;
    },
    _encodeMp3(int16, sampleRate) {
      const enc = new lamejs.Mp3Encoder(1, sampleRate, 128);
      const parts = [];
      for (let i = 0; i < int16.length; i += 1152) {
        const b = enc.encodeBuffer(int16.subarray(i, i + 1152));
        if (b.length) parts.push(new Uint8Array(b)); // lamejs отдаёт Int8Array
      }
      const end = enc.flush();
      if (end.length) parts.push(new Uint8Array(end));
      return new Blob(parts, { type: "audio/mp3" });
    },
    _encodeWav(buf) {
      // Запасной вариант, если lamejs недоступен: PCM16 моно WAV (тоже понятен модели).
      const int16 = this._bufferToInt16Mono(buf);
      const sr = buf.sampleRate;
      const dv = new DataView(new ArrayBuffer(44 + int16.length * 2));
      const wr = (o, s) => { for (let i = 0; i < s.length; i++) dv.setUint8(o + i, s.charCodeAt(i)); };
      wr(0, "RIFF"); dv.setUint32(4, 36 + int16.length * 2, true); wr(8, "WAVE");
      wr(12, "fmt "); dv.setUint32(16, 16, true); dv.setUint16(20, 1, true); dv.setUint16(22, 1, true);
      dv.setUint32(24, sr, true); dv.setUint32(28, sr * 2, true); dv.setUint16(32, 2, true); dv.setUint16(34, 16, true);
      wr(36, "data"); dv.setUint32(40, int16.length * 2, true);
      for (let i = 0; i < int16.length; i++) dv.setInt16(44 + i * 2, int16[i], true);
      return new Blob([dv], { type: "audio/wav" });
    },
    async _voiceToCompatible(blob) {
      // webm/ogg -> mp3 (lamejs) -> wav (fallback) -> исходник (крайний случай).
      const buf = await this._decodeToPcm(blob);
      await this.ensureLame();   // библиотека подгружается лениво, дождёмся её
      if (window.lamejs && lamejs.Mp3Encoder) {
        return { blob: this._encodeMp3(this._bufferToInt16Mono(buf), buf.sampleRate), ext: "mp3", mime: "audio/mp3" };
      }
      return { blob: this._encodeWav(buf), ext: "wav", mime: "audio/wav" };
    },
    async toggleRecord() {
      if (this.recording) {
        // Остановка: onstop соберёт чанки, перекодирует и добавит аудио во вложения.
        this.mediaRecorder && this.mediaRecorder.stop();
        return;
      }
      // Кодировщик тянем ПАРАЛЛЕЛЬНО с записью, а не ждём его здесь: пока
      // пользователь говорит, библиотека успевает догрузиться незаметно.
      this.ensureLame();
      try {
        const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
        this.recChunks = [];
        this.mediaRecorder = new MediaRecorder(stream);
        this.mediaRecorder.ondataavailable = (ev) => {
          if (ev.data.size > 0) this.recChunks.push(ev.data);
        };
        this.mediaRecorder.onstop = async () => {
          stream.getTracks().forEach((t) => t.stop()); // отпускаем микрофон
          this.recording = false;
          const src = new Blob(this.recChunks, { type: this.mediaRecorder.mimeType || "audio/webm" });
          // Плашка со спиннером сразу — конвертация в MP3 занимает мгновение.
          const raw = {
            id: (this._attSeq = (this._attSeq || 0) + 1),
            type: "audio", mime: "audio/mp3", name: "Голосовое сообщение.mp3",
            size: 0, data: null, loading: true, error: false,
          };
          this.pendingAttachments.push(raw);
          const att = this.pendingAttachments[this.pendingAttachments.length - 1];
          try {
            const { blob, ext, mime } = await this._voiceToCompatible(src);
            att.mime = mime;
            att.name = "Голосовое сообщение." + ext;
            att.size = blob.size;
            const reader = new FileReader();
            reader.onload = () => { att.data = reader.result; att.loading = false; };
            reader.onerror = () => { att.error = true; att.loading = false; };
            reader.readAsDataURL(blob);
          } catch (e) {
            att.error = true; att.loading = false;
            this.showToast("Не удалось обработать запись: " + e.message);
          }
        };
        this.mediaRecorder.start();
        this.recording = true;
      } catch (e) {
        this.showToast("Не удалось получить доступ к микрофону: " + e.message);
      }
    },

    // ---------- Аватар персонажа: загрузка файла -> data URI ----------
    onAvatarFile(e) {
      const file = e.target.files[0];
      if (!file || !this.charEdit) return;
      const reader = new FileReader();
      reader.onload = () => { this.charEdit.avatar_path = reader.result; };
      reader.readAsDataURL(file);
      e.target.value = "";
    },

    // ---------- Генерация арта (по описанию / последней сцене / общей картине) ----------
    async generateArt(mode) {
      this.artMenu = false;
      if (!this.sessionId) return;
      // «По описанию» — не алерт, а режим: вы пишете описание (+ фото) сообщением.
      if (mode === "prompt") {
        this.composerMode = "art";
        this.chatError = "";
        return;
      }
      this.chatError = "";
      try {
        await this.api("/sessions/" + this.sessionId + "/image", {
          method: "POST",
          body: JSON.stringify({ prompt: "", mode }),
        });
        await this.loadMessages();
        this.notifyChatListChanged(this.sessionId);   // картинка — новая реплика чата
      } catch (e) {
        // Показываем РЕАЛЬНУЮ ошибку сервера/прокси (а не общую фразу).
        this.chatError = "Арт не удался: " + e.message;
      }
    },
    // Отправка описания арта: текст из поля ввода + прикреплённые фото идут в генерацию.
    async sendArt() {
      const desc = this.input.trim();
      if (!desc && this.pendingAttachments.length === 0) return;
      if (this.attachmentsLoading) {  // ждём дочитывания прикреплённых фото-референсов
        this.waitingFiles = true;
        await this._awaitAttachments();
        this.waitingFiles = false;
      }
      this._dropBadAttachments();
      this.chatError = "";
      const attachments = this._cleanAtts(this.pendingAttachments);
      this.input = "";
      this.pendingAttachments = [];
      this.composerMode = "text";
      this.resetComposerHeight();
      try {
        // Фото-референсы могут быть тяжёлыми — грузим с прогрессом.
        await this._postWithProgress("/sessions/" + this.sessionId + "/image",
          { prompt: desc, mode: "prompt", attachments });
        await this.loadMessages();
        this.notifyChatListChanged(this.sessionId);
      } catch (e) {
        this.chatError = "Арт не удался: " + e.message;
      }
    },

    // ---------- Арт по конкретному сообщению чата ----------
    async artFromMessage(m) {
      if (!this.sessionId) return;
      this.chatError = "";
      try {
        await this.api("/sessions/" + this.sessionId + "/image", {
          method: "POST",
          body: JSON.stringify({ mode: "scene", from_message_id: m.id }),
        });
        await this.loadMessages();
        this.notifyChatListChanged(this.sessionId);
      } catch (e) {
        this.chatError = "Арт не удался: " + e.message;
      }
    },

    // ---------- Аватарки в чате ----------
    // Аватар участника группового чата по имени (реплики группы несут speaker_name).
    memberAvatar(name) {
      const g = this.currentGroup;
      const mem = g && (g.members || []).find((x) => x.name === name);
      return (mem && mem.avatar_path) || "";
    },
    // Аватар для пузыря сообщения: ассистент — персонаж/участник группы,
    // пользователь — персона этого чата или аватар профиля.
    msgAvatar(m) {
      if (m.role === "assistant") {
        return (m.speaker_name && this.memberAvatar(m.speaker_name))
          || (this.sharedView && this.sharedView.character_avatar)
          || (this.selectedCharacter && this.selectedCharacter.avatar_path) || "";
      }
      const p = this.personas.find((x) => x.id === this.sessionPersonaId);
      return (p && p.avatar_path) || (this.currentUserObj && this.currentUserObj.avatar_path) || "";
    },
    // Буква-заглушка, когда аватарки нет (первая буква имени).
    msgAvatarLetter(m) {
      let name;
      if (m.role === "assistant") {
        name = m.speaker_name
          || (this.sharedView && this.sharedView.character_name)
          || (this.selectedCharacter && this.selectedCharacter.name) || "ИИ";
      } else {
        const p = this.personas.find((x) => x.id === this.sessionPersonaId);
        name = (p && p.name) || (this.currentUserObj && this.currentUserObj.username) || "Вы";
      }
      return (name || "?").charAt(0).toUpperCase();
    },

    // ---------- Ответ на конкретное сообщение ----------
    replyTo(m) { this.replyToId = m.id; },
    cancelReply() { this.replyToId = null; },
    quoteOf(id) {
      const m = this.messages.find((x) => x.id === id);
      return m ? (m.content || "").slice(0, 90) : "";
    },

    // ---------- Функция «Продолжить» ----------
    continueReply() {
      if (!this.connected || this.streaming) return;
      this.chatError = "";
      this.currentReply = "";
      this.currentThought = "";
      this.liveBubbles = [];
      this.streaming = true;
      this._lastEvtAt = Date.now();
      this.ws.send(JSON.stringify({ type: "continue", params: this.params }));
    },

    // ---------- Фон чата ----------
    async setBackground(value) {
      this.sessionBg = value;
      this.bgPicker = false;
      const sid = this.sessionId;
      if (sid) {
        await this.api("/sessions/" + sid, {
          method: "PATCH",
          body: JSON.stringify({ background: value }),
        });
        this._patchSessionRow(sid, { background: value });
      }
    },
    uploadBackground(e) {
      const file = e.target.files[0];
      if (!file) return;
      const reader = new FileReader();
      reader.onload = () => this.setBackground(reader.result);
      reader.readAsDataURL(file);
      e.target.value = "";
    },

    // ---------- Импорт чата из SillyTavern (.jsonl) ----------
    async importChat(e) {
      const file = e.target.files[0];
      if (!file) return;
      const form = new FormData();
      form.append("file", file);
      const res = await fetch("/api/sessions/import", { method: "POST", body: form, headers: this.authHeaders() });
      e.target.value = "";
      if (!res.ok) {
        let detail = "код " + res.status;
        try { const j = await res.json(); if (j && j.detail) detail = j.detail; } catch (e) {}
        this.showToast("Не удалось импортировать чат: " + detail);
        return;
      }
      const data = await res.json();
      await this.loadCharacters();
      this.selectedCharacterId = data.character_id;
      this.charEdit = this.characters.find((c) => c.id === data.character_id) || null;
      await this.syncChatList(data.session_id);
      await this.openSession({ id: data.session_id });
      await this.loadHorae();
      // Явно сообщаем, что импортировалось, в т.ч. подхватилась ли память Horae.
      let msg = (data.native ? "Чат AiChat импортирован. " : "") + "Сообщений: " + data.count + ".";
      if (data.native) {
        if (data.horae_saved) {
          msg += "\n🧠 Записи памяти восстановлены (записей: " + data.horae_saved + ") — «Память» → «Лорбук».";
          this.openMemory("lore");
        }
      } else if (data.horae_structured > 0) {
        // Разобранные данные Horae (время, место, персонажи…) легли в сами
        // сообщения, и снимок-запись сервер тогда не пишет (horae_saved = false):
        // без этой ветки тост ниже соврал бы «данных Horae не найдено».
        const n = data.horae_structured;
        msg += "\n🕰 Horae: перенесены данные " + n + " " + this.plural(n, "ответа", "ответов", "ответов")
          + " (время, место, персонажи, предметы, события) — вкладка «Память».";
        this.openMemory("state");
      } else if (data.horae_saved) {
        msg += "\n🧠 Память Horae подхвачена: снимок состояния сохранён как always_on-запись этого чата («Память» → «Лорбук»).";
        this.openMemory("lore"); // сразу показываем, что сохранилось
      } else {
        msg += "\nДанных Horae в файле не найдено — снимок состояния не сохранён.";
      }
      this.showToast(msg);
    },

    // ---------- Сохранение настроек интерфейса в системе (БД) ----------
    async loadUiPrefs(applyParams = true) {
      const ui = await this.api("/settings/ui");
      // Сработала ли разовая миграция: тогда её флаг надо записать сразу, а не
      // ждать, пока человек сам что-нибудь поменяет (флаги пишет saveUiPrefs).
      let migrated = false;
      if (applyParams && ui && ui.params) this.params = { ...this.params, ...ui.params };
      // Мягкая миграция старых сохранённых настроек.
      if (applyParams) {
        // Прежний дефолт max_tokens=1024 резал ответы (особенно с рассуждениями).
        if (!this.params.max_tokens || this.params.max_tokens <= 1024) this.params.max_tokens = 8192;
        // Пустое окно — не выбор человека, а поломка профиля: чиним всегда.
        if (!this.params.context_tokens) this.params.context_tokens = 200000;
        // Сохранённое окно в 1 млн — прошлый дефолт «максимум Gemini». Он незаметно
        // уводил КАЖДЫЙ ход в удвоенный тариф (у Gemini вход свыше ~200 тыс. стоит
        // вдвое), поэтому один раз опускаем до 200к. «Один раз» раньше было только
        // в комментарии: проверка шла на каждой загрузке и сбрасывала и 1 млн,
        // выставленный вручную. Теперь её закрывает флаг ctx_budget_v.
        if (ui && ui.ctx_budget_v !== 2) {
          if (this.params.context_tokens === 1000000) this.params.context_tokens = 200000;
          migrated = true;
        }
        // Новые настройки экономии могли не сохраниться в старых профилях.
        if (this.params.history_files_turns == null) this.params.history_files_turns = 12;
        if (this.params.knowledge_chars == null) this.params.knowledge_chars = 60000;
        // Настройки обхода цензуры (появились в 1.13.0). Порог доставляем только
        // тем, у кого Zero-Censorship РЕАЛЬНО включён: иначе эта строка молча
        // возвращала бы «off» и новым профилям, у которых фильтры стандартные.
        if (this.params.disable_safety && !this.params.safety_preset) {
          this.params.safety_preset = "off";
        }
        if (!this.params.safety_overrides) this.params.safety_overrides = {};
      }
      // Что saveUiPrefs запишет в ctx_budget_v. «2» — только если миграцию
      // бюджета выше и правда проверили. С пресетом по умолчанию (applyParams =
      // false) params задаёт пресет, сохранённые ui.params не читаются, и флаг
      // «сделано» закрыл бы миграцию, так её и не проверив: снимут пресет —
      // сохранённый 1 млн останется. Тогда флаг уходит в PUT таким, каким
      // лежал (не было — не будет и в PUT). Поле с _: Vue его не проксирует.
      this._ctxBudgetV = applyParams ? 2 : ui ? ui.ctx_budget_v : undefined;
      if (ui && ui.jailbreak) {
        this.jailbreak = {
          enabled: !!ui.jailbreak.enabled,
          text: ui.jailbreak.text || "",
          presets: Array.isArray(ui.jailbreak.presets) ? ui.jailbreak.presets : [],
        };
      }
      if (ui && Number(ui.message_preload) > 0) this.messagePreload = Number(ui.message_preload);
      if (ui && "auto_summary" in ui) this.autoSummary = ui.auto_summary !== false;
      if (ui && ui.group_reply_delay != null) this.groupReplyDelay = Number(ui.group_reply_delay);
      if (ui && ui.summary_every != null) this.summaryEvery = Number(ui.summary_every);
      if (ui && ui.memory_window != null) this.memoryWindow = Number(ui.memory_window);
      // Окно по умолчанию выросло с 20 до 50 (мастер-память, 2.5.0). Сохранённое
      // 20 почти всегда — прежний дефолт, а не выбор, поэтому один раз поднимаем
      // его до 50. После флага memory_defaults_v 20 снова можно выставить руками.
      if (ui && ui.memory_defaults_v !== 2) {
        if (ui.memory_window == null || Number(ui.memory_window) === 20) this.memoryWindow = 50;
        migrated = true;
      }
      if (ui && "horae_facts" in ui) this.horaeFacts = ui.horae_facts !== false;
      // Параметры пакетов: ключ есть — его выбрал человек, он и главный. Нет —
      // поле ждёт status().settings (умолчание сервера из .env, _applyMemSettings)
      // и в «ui» не пишется. Какие ключи выбраны — в поле с _: Vue его не
      // проксирует, а в шаблоне признак не нужен.
      const mine = {};
      for (const [key, field] of MEM_UI_FIELDS) {
        if (ui && ui[key] != null) { this[field] = Number(ui[key]); mine[key] = true; }
      }
      this._memUiSet = mine;
      if (migrated) this.saveUiPrefs();
    },
    // ---------- Закрепление чатов и персонажей ----------
    // После переключения перечитываем список, а не правим флаг у строки на месте:
    // pinned_at назначает сервер («сейчас» в момент закрепления), и порядок
    // среди закреплённых без него не восстановить. Сам порядок единого списка
    // считает sortChats по тем же правилам, что и сервер для каждого вида.
    async togglePinSession(s) {
      const next = !s.pinned;
      try {
        await this.api("/sessions/" + s.id, {
          method: "PATCH",
          body: JSON.stringify({ pinned: next }),
        });
        await this.syncChatList(s.id);
        this.showToast(next ? "📌 Чат закреплён наверху" : "Чат откреплён");
      } catch (e) {
        this.showToast("Не удалось закрепить: " + e.message);
      }
    },
    async togglePinCharacter(c) {
      const next = !c.pinned;
      try {
        await this.api("/characters/" + c.id, {
          method: "PATCH",
          body: JSON.stringify({ pinned: next }),
        });
        await this.loadCharacters();
        this.showToast(next ? "📌 Персонаж закреплён наверху" : "Персонаж откреплён");
      } catch (e) {
        this.showToast("Не удалось закрепить: " + e.message);
      }
    },
    // Компактная отметка времени для списка: сегодня — часы, эта неделя — день,
    // раньше — дата. Длинные строки в узком сайдбаре не помещаются.
    shortWhen(iso) {
      if (!iso) return "";
      const d = new Date(iso);
      if (isNaN(d)) return "";
      const now = new Date();
      const sameDay = d.toDateString() === now.toDateString();
      if (sameDay) return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
      const days = Math.floor((now - d) / 86400000);
      if (days < 7) return d.toLocaleDateString([], { weekday: "short" });
      return d.toLocaleDateString([], { day: "2-digit", month: "2-digit" });
    },

    toggleAssistantMode() {
      this.params.assistant_mode = !this.params.assistant_mode;
      this.saveUiPrefs();
      this.showToast(this.params.assistant_mode
        ? "🎓 Режим ассистента: персонаж не отыгрывает, а выполняет задачу"
        : "🎭 Отыгрыш вернулся");
    },
    saveUiPrefs() {
      // Дебаунс, чтобы не дёргать сервер на каждое движение ползунка.
      clearTimeout(this._uiSaveTimer);
      this._uiSaveTimer = setTimeout(() => {
        // Параметры пакетов — только выбранные человеком (MEM_UI_FIELDS);
        // undefined JSON.stringify опускает, и сервер берёт умолчание из .env.
        const mine = this._memUiSet || {};
        // Монитор токенов ждёт именно ответа PUT: окно и бюджет снимка сервер
        // читает из «ui», и пересчёт до сохранения показал бы старые числа.
        const refreshCtx = this._ctxAfterSave;
        this._ctxAfterSave = false;
        const refreshMem = this._memAfterSave;
        this._memAfterSave = false;
        this.api("/settings/ui", {
          method: "PUT",
          body: JSON.stringify({
            params: this.params,
            message_preload: this.msgPageSize,
            auto_summary: this.autoSummary,
            group_reply_delay: this.groupReplyDelay,
            summary_every: this.summaryEvery,
            memory_window: this.memoryWindow,
            horae_facts: this.horaeFacts,
            memory_batch: mine.memory_batch ? this.memoryBatch : undefined,
            memory_delay_ms: mine.memory_delay_ms ? this.memoryDelayMs : undefined,
            memory_snapshot_tokens: mine.memory_snapshot_tokens ? this.memorySnapshotTokens : undefined,
            // Флаги разовых миграций (см. loadUiPrefs). PUT заменяет значение
            // целиком: не допиши их сюда — следующее же сохранение стёрло бы
            // флаги, и миграции снова перетёрли бы окно 20 и бюджет 1 млн.
            // ctx_budget_v — из loadUiPrefs: миграцию бюджета проверяют не на
            // каждом пути загрузки. undefined JSON.stringify просто опускает.
            memory_defaults_v: 2,
            ctx_budget_v: this._ctxBudgetV,
            jailbreak: this.jailbreak,
          }),
        }).then(() => {
          if (refreshCtx) this.loadCtxStats();
          if (refreshMem) this.loadMemStatus();
        }).catch(() => {});
      }, 600);
    },
    // Поле «Размер пакета», «Пауза» или «Бюджет снимка» изменил человек: с
    // этого момента ключ его, пишется в «ui» и главнее умолчания сервера.
    // То же значение, что уже стоит (мусор в поле numFromInput возвращает
    // прежним), изменением не считаем — иначе случайный Enter в поле навсегда
    // закрепил бы умолчание и отрезал .env.
    setMemPref(key, value) {
      const f = MEM_UI_FIELDS.find((x) => x[0] === key);
      if (!f || value === this[f[1]]) return;
      this[f[1]] = value;
      this._memUiSet = { ...(this._memUiSet || {}), [key]: true };
      // Бюджет снимка меняет разбивку хода — монитор перечитываем после PUT.
      if (key === "memory_snapshot_tokens") this._ctxAfterSave = true;
      this.saveUiPrefs();
    },
    // Активное окно: поле и пресеты. После сохранения монитор токенов
    // перечитывается — иначе он до следующего хода показывал бы прежнее окно.
    setMemoryWindow(value) {
      if (value === this.memoryWindow) return;
      this.memoryWindow = value;
      this._ctxAfterSave = true;
      this.saveUiPrefs();
    },
    // Поля параметров пакетов, которые человек не трогал, показывают то, с чем
    // сервер работает на самом деле: status().settings — это «ui», а без ключа
    // — MEMORY_* из .env. Раньше поле показывало умолчание клиента, и после
    // MEMORY_SNAPSHOT_TOKENS=6000 в .env панель уверяла, что бюджет 12 000.
    _applyMemSettings(s) {
      if (!s || typeof s !== "object") return;
      const mine = this._memUiSet || {};
      for (const [key, field, skey] of MEM_UI_FIELDS) {
        const v = Number(s[skey]);
        if (!mine[key] && s[skey] != null && Number.isFinite(v)) this[field] = v;
      }
    },

    // ---------- Пресеты параметров ----------
    async loadPresets() { this.presets = await this.api("/presets"); },
    async savePreset() {
      const name = this.presetName.trim() || (await this.askPrompt("Название пресета", { placeholder: "Например: Творческий" }));
      if (!name) return;
      await this.api("/presets", { method: "POST", body: JSON.stringify({ name, params: this.params }) });
      this.presetName = "";
      await this.loadPresets();
    },
    applyPreset(p) { this.params = { ...this.params, ...p.params }; },
    async deletePreset(p) { await this.api("/presets/" + p.id, { method: "DELETE" }); await this.loadPresets(); },
    async setDefaultPreset(p) {
      await this.api("/presets/" + p.id + "/default", { method: "POST" });
      await this.loadPresets();
    },

    // ---------- Подключение к LiteLLM ----------
    async loadConnection() {
      this.connection = await this.api("/settings/connection");
      if (!this.params.model) this.params.model = this.connection.default_model || "";
    },
    async saveConnection() {
      this.connection = await this.api("/settings/connection", { method: "PUT", body: JSON.stringify(this.connection) });
      this.connStatus = "Сохранено";
      this.connOk = true;
    },
    async testConnection() {
      this.connStatus = "Проверяю...";
      this.connOk = null;
      await this.saveConnection();
      const r = await this.api("/models");
      if (r.ok) {
        this.models = r.models;
        this.connStatus = "OK, моделей: " + r.models.length;
        this.connOk = true;
      } else {
        this.connStatus = "Ошибка: " + r.error;
        this.connOk = false;
      }
    },

    // ---------- Лорбук (записи памяти, таблица horae_entries) ----------
    blankHorae() {
      return { id: null, category: "lore", title: "", content: "", keywords: "", always_on: false, enabled: true, priority: 0, scope: "global" };
    },
    async loadHorae() { this.horae = await this.api("/horae"); },
    editHorae(h) {
      this.horaeEdit = {
        id: h.id, category: h.category, title: h.title, content: h.content,
        keywords: (h.keywords || []).join(", "), always_on: h.always_on,
        enabled: h.enabled, priority: h.priority,
        scope: h.session_id ? "session" : "global",
      };
    },
    async saveHorae() {
      const h = this.horaeEdit;
      const payload = {
        category: h.category, title: h.title, content: h.content,
        keywords: h.keywords.split(",").map((s) => s.trim()).filter(Boolean),
        always_on: h.always_on, enabled: h.enabled, priority: Number(h.priority) || 0,
      };
      if (h.id) {
        await this.api("/horae/" + h.id, { method: "PATCH", body: JSON.stringify(payload) });
      } else {
        payload.session_id = h.scope === "session" ? this.sessionId : null;
        await this.api("/horae", { method: "POST", body: JSON.stringify(payload) });
      }
      this.horaeEdit = this.blankHorae();
      await this.loadHorae();
    },
    async deleteHorae(h) { await this.api("/horae/" + h.id, { method: "DELETE" }); await this.loadHorae(); },

    // ---------- Вкладка «Память»: раздел и чем сжимать историю ----------
    // Открыть «Память» на разделе: импорт чата показывает, что легло в Хронику
    // («state») или в лорбук («lore»). n растёт — тот же раздел дважды тоже.
    openMemory(sub) {
      this.drawerTab = "memory";
      this.memoryTabReq = { tab: sub, n: ((this.memoryTabReq && this.memoryTabReq.n) || 0) + 1 };
    },
    async loadHoraeGlobal() {
      try {
        const r = await this.api("/horae/settings");
        this.horaeGlobalSummary = !!(r && r.effective && r.effective.summary_enabled);
      } catch (e) {
        // Старый сервер без Хроники: выбор сводится к снимку и «не сжимать».
        this.horaeGlobalSummary = false;
      }
    },
    // Выбор по умолчанию для всех чатов (memoryEngine). Свёртки Хроники —
    // summary_enabled в глобальном слое её настроек (пишет только
    // администратор); снимок при этом остаётся включённым как запасной — для
    // чатов, где Хроника выключена. «Не сжимать» — снимок не обновляется.
    async setMemoryEngine(engine) {
      if (engine === this.memoryEngine) return;
      const wantHorae = engine === "horae";
      if (wantHorae !== !!this.horaeGlobalSummary) {
        try {
          const r = await this.api("/horae/settings", {
            method: "PUT", body: JSON.stringify({ summary_enabled: wantHorae ? true : null }),
          });
          this.horaeGlobalSummary = !!(r && r.effective && r.effective.summary_enabled);
        } catch (e) {
          this.showToast("⚠ " + e.message);
          return;
        }
      }
      const auto = engine !== "off";
      if (auto !== this.autoSummary) {
        this.autoSummary = auto;
        // Статус чата («кто сжимает») сервер считает по «ui» — перечитываем
        // после сохранения, а не до него.
        this._memAfterSave = true;
        this.saveUiPrefs();
      } else {
        this.loadMemStatus();
      }
      this.horaeTick += 1;
    },

    // ---------- Мастер-память чата (иерархическая пакетная) ----------
    // Статус открытого чата: снимок, буфер пересборки, бэклог, задание.
    // Каждый запрос помечен номером: ↻, опрос и смена чата обгоняют друг друга,
    // и поздний ответ старого запроса вернул бы «running» уже после «done» —
    // опрос ожил бы, а тост о завершении пришёл бы дважды. Пишем только ответ
    // последнего запроса и только для того чата, который всё ещё открыт.
    async loadMemStatus() {
      const sid = this.sessionId;
      const seq = (this._memSeq = (this._memSeq || 0) + 1);
      if (!sid) {
        // Без чата панели нет, и ждать больше нечего. «Загружаю…» снимаем
        // здесь: обогнанный запрос прежнего чата его уже не снимет (см. ниже).
        this._stopMemPoll();
        this.memStatus = null;
        this.memBusy = false;
        this.memPollFails = 0;
        return;
      }
      // «Загружаю…» — только пока показывать нечего: опрос раз в 1,5 с иначе
      // мигал бы выключенными кнопками всю пересборку.
      const first = !this.memStatus;
      if (first) this.memBusy = true;
      let st = null;
      let failed = null;
      try {
        st = await this.api("/sessions/" + sid + "/memory");
      } catch (e) {
        failed = e;
      }
      // Флаг первой загрузки снимает только ПОСЛЕДНИЙ запрос. Обогнанный ответ
      // (открыли вкладку и сразу ↻) иначе включил бы кнопки и убрал
      // «Загружаю…», пока новый запрос ещё идёт. Проверка чата — уже после:
      // сменили чат, а нового запроса нет (вкладка закрыта) — флаг всё равно
      // надо снять, иначе он висел бы до следующей загрузки.
      if (first && seq === this._memSeq) this.memBusy = false;
      if (seq !== this._memSeq || this.sessionId !== sid) return;
      // Сбой сети или 5xx посреди пересборки не гасит опрос: задание идёт на
      // сервере, следующий запрос его покажет. 4xx — честный отказ (чат удалён,
      // старый сервер без эндпоинта, сменился код доступа): пустое состояние.
      // Сбои считаем подряд: с третьего панель говорит «Нет связи с сервером»
      // и опрашивает всё реже (_memPollDelay), а не молчит с замёрзшей полосой.
      if (failed && (!failed.status || failed.status >= 500) && this.memJobActive) {
        this.memPollFails += 1;
        this._pollMem();
        return;
      }
      this.memPollFails = 0;
      // Ответ не того вида приравниваем к «статуса нет»: шаблон читает snapshot
      // и backlog без проверок и на чужом JSON упал бы при рендере.
      if (!st || typeof st !== "object" || !st.snapshot || !st.backlog) st = null;
      const prev = this.memStatus && this.memStatus.job ? this.memStatus.job.status : null;
      this.memStatus = st;
      if (st) this._applyMemSettings(st.settings);
      if (this.memJobActive) { this._pollMem(); return; }
      this._stopMemPoll();
      const job = st && st.job;
      if (!job || (prev !== "running" && prev !== "queued")) return;
      // Итог — тостом и в вежливый живой регион liveStatus: тост скринридер не
      // читает, а регион прогресса с концом задания пустеет молча.
      const out = this.memJobOutcome(job, st);
      if (out) {
        this.showToast(out.toast);
        this.liveStatus = out.spoken;
      }
      // «Остановить» исчез вместе с заданием; стоял на нём фокус — он упал бы
      // на body.
      this._memFocus();
      // Снимок и факты поменялись: бюджет хода и список записей уже другие.
      this.loadCtxStats();
      this.loadHorae().catch(() => {});
    },
    // Текст итога задания → { toast, spoken } или null. Честно по тому, что
    // случилось: раньше любое «done» было «Память пересобрана: N → M токенов»,
    // и пустая пересборка (весь чат в окне, прежний снимок оставлен)
    // рапортовала «0 сообщений → 0 токенов» — читалось как стёртая память.
    // Токены — принятого снимка (job.snapshot_tokens; нет — снимок из статуса),
    // а не state_tokens: это размер буфера по ходу задания.
    memJobOutcome(job, st) {
      const warns = (Array.isArray(job.warnings) ? job.warnings : [])
        .filter((w) => typeof w === "string" && w.trim()).map((w) => w.trim());
      const cap = (s) => s.charAt(0).toUpperCase() + s.slice(1);
      const n = Number(job.processed) || 0;
      // processed нет вовсе (старый сервер) — не «ноль», а неизвестно.
      const nothing = job.processed != null && n === 0;
      const hasSnapshot = !!(st && st.snapshot && st.snapshot.exists);
      const seePanel = " (есть предупреждения — см. панель)";
      if (job.status === "done") {
        let text;
        if (nothing) {
          text = warns.length ? cap(warns[0])
            : "Сжимать нечего — весь чат в окне" + (hasSnapshot ? ", прежний снимок оставлен" : "");
          // Первое предупреждение уже и есть текст тоста — «см. панель» только
          // когда там есть что-то ещё.
          if (warns.length > 1) text += seePanel;
        } else {
          const t = job.snapshot_tokens != null ? job.snapshot_tokens : st && st.snapshot && st.snapshot.tokens;
          const head = job.mode === "catchup" ? "Память догнала чат"
            : job.mode === "rebuild" ? "Память пересобрана" : "Память обновлена";
          text = head + ": " + this.fmtNum(n) + " " + this.plural(n, "сообщение", "сообщения", "сообщений")
            + " → " + this.fmtNum(t) + " " + this.plural(Number(t) || 0, "токен", "токена", "токенов");
          if (warns.length) text += seePanel;
        }
        return { toast: "🧠 " + text, spoken: text };
      }
      if (job.status === "error") {
        const err = job.error || "задание прервано";
        return { toast: "⚠ Память: " + err, spoken: "Ошибка памяти: " + err };
      }
      if (job.status === "cancelled") {
        // Остановили в очереди или до первого пакета — сохранять было нечего.
        const text = nothing ? "Задание памяти остановлено до первого пакета — память не менялась"
          : job.mode === "catchup" ? "«Догнать» остановлено, готовое сохранено"
          : "Пересборка остановлена, готовое сохранено";
        return { toast: text, spoken: text };
      }
      return null;
    },
    // Фокус после старта задания, сброса и конца задания. Кнопка, на которую
    // он вернулся после подтверждения, тут же выключается (memBusy), а
    // «Остановить» в конце задания исчезает — браузер роняет фокус на body, и
    // клавиатурный путь начинался бы с начала страницы. Переводим его на
    // «Остановить», пока задание идёт, иначе на заголовок блока (tabindex -1).
    // Фокус, который человек уже унёс в другое место, не трогаем.
    _memFocus() {
      this.$nextTick(() => {
        const head = this.$refs.memHeading;
        if (!head) return;              // вкладка «Память» закрыта
        const sec = this.$refs.memSection;
        const a = document.activeElement;
        const lost = !a || a === document.body
          || (!!sec && sec.contains(a) && (a.disabled || a === head));
        if (!lost) return;
        const stop = this.$refs.memStopBtn;
        (stop && !stop.disabled ? stop : head).focus();
      });
    },
    // Опрос, пока задание queued/running. Таймер живёт в поле с префиксом _
    // (Vue его не проксирует) и всегда один: перед постановкой прежний
    // снимается, иначе ↻ посреди опроса запускал бы вторую цепочку запросов.
    _pollMem() {
      this._stopMemPoll();
      this._memTimer = setTimeout(() => { this._memTimer = null; this.loadMemStatus(); }, this._memPollDelay());
    },
    // Пауза опроса: 1,5 с. После трёх сбоев подряд — 1,5 → 3 → 6 → 10 с и
    // дальше по 10: сервер лежит или сеть пропала, и долбить его раз в 1,5 с
    // минутами незачем, а 10 с — ещё терпимая задержка, когда связь вернётся.
    // Удачный ответ обнуляет счётчик (loadMemStatus) — пауза снова 1,5 с.
    _memPollDelay() {
      const f = this.memPollFails;
      return f < 3 ? 1500 : Math.min(10000, 1500 * 2 ** (f - 3));
    },
    _stopMemPoll() {
      clearTimeout(this._memTimer);
      this._memTimer = null;
    },
    // Запуск задания: «rebuild» — пересборка с нуля (или продолжение
    // прерванной, resume), «catchup» — сжать только то, что ещё не учтено.
    // Каждый пакет — платный запрос к модели сводки, поэтому пересборка с нуля
    // спрашивает подтверждение с оценкой числа пакетов. Считаем от ВСЕЙ истории
    // старше окна, а не от backlog.pending: pending идёт от текущего указателя,
    // а пересборка с нуля начинает с первого сообщения. «Догнать» спрашивает
    // так же, когда пакетов больше трёх: после «Сбросить память», импорта или
    // у старой сводки pending — это вся история, и один клик ставил бы сотню
    // платных запросов, пока соседняя кнопка той же цены спрашивает.
    async startMemJob(mode, resume = false) {
      const sid = this.sessionId;
      if (!sid || this.memBusy || this.memPurging) return;
      const b = (this.memStatus && this.memStatus.backlog) || {};
      const batch = Math.max(1, Number(this.memoryBatch) || 1);
      if (mode === "rebuild" && !resume) {
        const older = Math.max(0, (Number(b.messages_total) || 0) - (Number(b.window) || 0));
        const k = Math.ceil(older / batch);
        const ok = await this.askConfirm(
          "Пересобрать память с нуля? " + this._memCostText(k) + " Старый снимок работает до конца пересборки.",
          { title: "Пересборка памяти", okText: "Пересобрать", danger: false });
        if (!ok || this.sessionId !== sid) return;
      } else if (mode === "catchup") {
        const k = Math.ceil(Math.max(0, Number(b.pending) || 0) / batch);
        if (k > 3) {
          const ok = await this.askConfirm(
            "Догнать память? " + this._memCostText(k) + " Остановить можно в любой момент — готовое сохранится.",
            { title: "Догнать память", okText: "Догнать", danger: false });
          if (!ok || this.sessionId !== sid) return;
        }
      }
      this.memBusy = true;
      // Кнопка запуска сейчас выключится вместе с фокусом на ней.
      this._memFocus();
      try {
        const r = await this.api("/sessions/" + sid + "/memory/rebuild", {
          method: "POST",
          body: JSON.stringify({ mode, resume: !!resume, batch_size: this.memoryBatch, delay_ms: this.memoryDelayMs }),
        });
        // Задание из ответа кладём в статус сразу: короткая догонялка может
        // закончиться раньше, чем мы перечитаем статус, и тогда «предыдущим»
        // оказался бы «done» прошлого задания — тост о новом не пришёл бы.
        if (r && r.job && this.memStatus && this.sessionId === sid) {
          this.memStatus = { ...this.memStatus, job: r.job };
        }
      } catch (e) {
        // 409 — задание этого чата уже идёт (другая вкладка, двойной клик).
        this.showToast(e.status === 409 ? "Уже идёт" : "⚠ Память: " + e.message);
      } finally {
        this.memBusy = false;
      }
      await this.loadMemStatus();
      // Задание пошло — фокус на «Остановить» (с заголовка, куда его увёл
      // вызов выше), не пошло — остаётся на заголовке.
      this._memFocus();
    },
    // Оценка цены задания для подтверждения. Факты запрашиваются только для
    // сообщений новее уже извлечённых фактов, так что у чата с фактами
    // пересборка их не повторяет — «плюс столько же» было бы неправдой.
    _memCostText(k) {
      return "Примерно " + k + " " + this.plural(k, "пакет", "пакета", "пакетов")
        + " = " + k + " " + this.plural(k, "платный запрос", "платных запроса", "платных запросов")
        + " к модели сводки" + (this.horaeFacts ? " (плюс факты — только там, где их ещё нет)" : "") + ".";
    },
    // Остановка между пакетами: сервер доделывает текущий пакет и сохраняет
    // готовое, поэтому статус ещё какое-то время «running» — опрос дождётся
    // «cancelled» и сам покажет тост.
    async cancelMemJob() {
      const sid = this.sessionId;
      if (!sid) return;
      try {
        await this.api("/sessions/" + sid + "/memory/cancel", { method: "POST" });
      } catch (e) {
        this.showToast("⚠ Память: " + e.message);
      }
      await this.loadMemStatus();
    },
    // Сброс: снимок, буфер пересборки и атомарные факты чата. Сообщения
    // остаются, и окно снова отдаёт модели всю историю, пока память не
    // соберётся заново, — поэтому после сброса пересчитываем и бюджет хода.
    async purgeMemory() {
      const sid = this.sessionId;
      if (!sid || this.memBusy || this.memPurging) return;
      if (!(await this.askConfirm("Сбросить память чата? Сотрутся мастер-снимок, буфер пересборки и атомарные факты. Сообщения останутся.", { okText: "Сбросить" }))) return;
      if (this.sessionId !== sid) return;
      this.memBusy = true;
      // Ответа можно ждать минуты: сервер сперва останавливает задание и ждёт
      // конца текущего пакета (и начатого ежеходного прохода), иначе тот
      // воскресил бы стёртую память. Пока ждём — «Сбрасываю…» и кнопки
      // выключены, чтобы долгий запрос не казался зависшим.
      this.memPurgingId = sid;
      // Все кнопки блока выключаются, и «Сбросить память» роняла бы фокус на
      // body — переводим его на заголовок блока.
      this._memFocus();
      let ok = false;
      try {
        const r = await this.api("/sessions/" + sid + "/memory", { method: "DELETE" });
        const n = (r && Number(r.facts_deleted)) || 0;
        this.showToast("🧠 Память чата сброшена" + (n ? " · удалено фактов: " + this.fmtNum(n) : ""));
        ok = true;
      } catch (e) {
        this.showToast("⚠ Память: " + e.message);
      } finally {
        this.memBusy = false;
        if (this.memPurgingId === sid) this.memPurgingId = null;
      }
      await this.loadMemStatus();
      if (!ok) return;
      this.loadCtxStats();
      this.loadHorae().catch(() => {});
    },
    // Экспорт снимка в .md. Сырой fetch, а не api(): ответ — файл, а не JSON.
    // Имя придумывает сервер (в нём название чата), поэтому берём его из
    // Content-Disposition и только без заголовка — своё.
    async exportMemory() {
      const sid = this.sessionId;
      if (!sid) return;
      let res;
      try {
        res = await fetch("/api/sessions/" + sid + "/memory/export?facts=1", { headers: this.authHeaders() });
      } catch (e) {
        this.showToast("⚠ Память: сеть недоступна, снимок не выгружен");
        return;
      }
      if (res.status === 404) { this.showToast("Снимка ещё нет"); return; }
      if (!res.ok) {
        if (res.status === 401) this.needAccess = true;
        this.showToast("Экспорт не удался (код " + res.status + ")");
        return;
      }
      const blob = await res.blob();
      const name = this._dispositionName(res.headers.get("Content-Disposition"));
      this.downloadBlob(blob, name || "memory-" + sid + ".md");
    },
    // Имя файла из Content-Disposition. Сначала filename*=UTF-8''… (RFC 5987):
    // кириллица названия чата приходит только там, процент-кодированной. Затем
    // простой filename=. Битая кодировка или нет ни того ни другого — "".
    _dispositionName(header) {
      if (!header) return "";
      const ext = /filename\*\s*=\s*UTF-8''([^;]+)/i.exec(header);
      if (ext) {
        try { return decodeURIComponent(ext[1].trim().replace(/^"|"$/g, "")); } catch (e) { /* ниже — простое имя */ }
      }
      const plain = /filename\s*=\s*(?:"([^"]*)"|([^;]+))/i.exec(header);
      return plain ? (plain[1] || plain[2] || "").trim() : "";
    },

    // ---------- Персоны и заметка автора ----------
    async loadPersonas() { this.personas = await this.api("/personas"); },
    async createPersona() {
      if (!this.personaNew.name.trim()) return;
      await this.api("/personas", { method: "POST", body: JSON.stringify(this.personaNew) });
      this.personaNew = { name: "", description: "", avatar_path: null };
      await this.loadPersonas();
    },
    onPersonaAvatar(e) {
      const file = e.target.files[0];
      if (!file) return;
      const reader = new FileReader();
      reader.onload = () => { this.personaNew.avatar_path = reader.result; };
      reader.readAsDataURL(file);
      e.target.value = "";
    },
    async deletePersona(p) { await this.api("/personas/" + p.id, { method: "DELETE" }); await this.loadPersonas(); },
    // Сохранить метаданные чата — ТОЛЬКО те поля, которые пользователь трогал.
    //
    // Раньше метод слал все три поля разом, и это молча стирало данные: группа
    // открывается как {id}, метаданных в объекте нет, поэтому authorNote получал
    // пустую строку, и первое же изменение часового пояса записывало эту пустоту
    // в базу. Сервер делает model_dump(exclude_none=True), то есть null он
    // отбрасывает, а пустую СТРОКУ записывает — страдали author_note и timezone.
    //
    // fields приходит из шаблона как объектный литерал. Явная проверка нужна
    // потому, что @change="applySessionMeta" передал бы сюда Event, и его поля
    // ушли бы в PATCH.
    async applySessionMeta(fields) {
      if (!this.sessionId) return;
      const ok = fields && typeof fields === "object" && !(fields instanceof Event);
      if (!ok) return;
      const payload = {};
      for (const [k, v] of Object.entries(fields)) {
        if (v !== undefined) payload[k] = v;
      }
      if (!Object.keys(payload).length) return;
      const sid = this.sessionId;
      await this.api("/sessions/" + sid, {
        method: "PATCH",
        body: JSON.stringify(payload),
      });
      this._patchSessionRow(sid, payload);
    },
    // Сохранённые метаданные чата — в его строку списка, и сразу. openSession
    // берёт заметку автора, персону, фон и пояс из строки, по которой кликнули,
    // а строка до следующего перечитывания списка хранила значения ДО правки.
    // Итог был хуже, чем «показывает старое»: вернулся в чат без новой реплики,
    // увидел прежнюю заметку, кликнул в поле и вышел — blur записывал старое
    // значение обратно на сервер поверх только что сохранённого. Строку правим
    // на месте во всех списках, где она есть, а соседним вкладкам сообщаем.
    _patchSessionRow(sid, fields) {
      for (const list of [this.sessions, this.allSessions, this.groups]) {
        const row = list.find((x) => x.id === sid);
        if (row) Object.assign(row, fields);
      }
      this.notifyChatListChanged(sid);
    },

    // ---------- Доступ к приложению ----------
    checkCode(code) {
      return fetch("/api/auth/login", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ code }),
      }).then((r) => r.json()).then((d) => d.ok).catch(() => false);
    },
    async submitAccess() {
      const ok = await this.checkCode(this.accessInput);
      if (!ok) { this.accessError = "Неверный код"; return; }
      this.accessCode = this.accessInput;
      localStorage.setItem("accessCode", this.accessCode);
      this.needAccess = false;
      this.accessError = "";
      await this.initApp();
    },

    // ---------- Аккаунты ----------
    async fetchMe() {
      try {
        const r = await fetch("/api/auth/me", { headers: { "X-User-Token": this.userToken } });
        if (!r.ok) return null;
        return await r.json();
      } catch (e) { return null; }
    },
    // Переключение вкладок ворот. Ошибка гасится вместе с вкладкой: иначе
    // «Пароли не совпадают», полученное на регистрации, продолжало висеть над
    // формой входа, где такой проверки нет вовсе. Фокус переносится вручную —
    // в tablist фокусируема только выбранная вкладка, и без переноса фокус
    // остался бы на кнопке, только что ушедшей из табуляции.
    setAuthTab(tab, moveFocus) {
      this.authTab = tab;
      this.accessError = "";
      if (!moveFocus) return;
      this.$nextTick(() => {
        const el = this.$refs[tab === "register" ? "gateTabRegister" : "gateTabLogin"];
        if (el) el.focus();
      });
    },
    async submitAuth() {
      // Регистрация проверяется ДО запроса: сервер принимает пароль любой
      // длины и подтверждение не сверяет, а восстановления пароля в продукте
      // нет — сброс делает только администратор. Опечатка в пароле при
      // регистрации отрезает человека от всех его историй, и заметить её
      // после отправки уже негде.
      if (this.authTab === "register") {
        if (this.authForm.password.length < 8) { this.accessError = "Пароль короче 8 символов"; return; }
        if (this.authForm.password !== this.authPassword2) { this.accessError = "Пароли не совпадают"; return; }
      }
      const path = this.authTab === "register" ? "/api/auth/register" : "/api/auth/login_user";
      const r = await fetch(path, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(this.authForm),
      });
      if (!r.ok) {
        this.accessError = r.status === 409 ? "Логин уже занят" : "Неверный логин или пароль";
        return;
      }
      const d = await r.json();
      this.userToken = d.token;
      localStorage.setItem("userToken", this.userToken);
      this.currentUserObj = d.user;
      this.needAuth = false;
      this.accessError = "";
      await this.initApp();
    },
    logout() {
      localStorage.removeItem("userToken");
      this.userToken = "";
      this.currentUserObj = null;
      location.reload();
    },
    async loadFriends() {
      if (!this.authStatus.accounts_enabled || !this.userToken) return;
      const d = await this.api("/friends");
      this.friends = d.friends || [];
      this.friendsIncoming = d.incoming || [];
      try { this.sharedSessions = await this.api("/sessions/shared"); } catch (e) {}
    },
    async addFriend() {
      if (!this.newFriendName.trim()) return;
      try {
        await this.api("/friends/add", { method: "POST", body: JSON.stringify({ username: this.newFriendName }) });
      } catch (e) { this.showToast("Пользователь не найден"); return; }
      this.newFriendName = "";
      await this.loadFriends();
    },
    async acceptFriend(f) {
      await this.api("/friends/" + f.friendship_id + "/accept", { method: "POST" });
      await this.loadFriends();
    },
    async declineFriend(f) {
      await this.api("/friends/" + f.friendship_id + "/decline", { method: "POST" });
      await this.loadFriends();
    },
    async removeFriend(f) {
      // /decline удаляет дружбу в любую сторону — используем его и для «удалить из друзей».
      if (!(await this.askConfirm(
        "Удалить «" + f.username + "» из друзей? Он также потеряет доступ к чатам, которыми вы делились.",
        { okText: "Удалить" }
      ))) return;
      await this.api("/friends/" + f.friendship_id + "/decline", { method: "POST" });
      await this.loadFriends();
    },
    // Поделиться текущим чатом с другом (он сможет читать и участвовать).
    async shareChat() { await this.openInvite({ id: this.sessionId }); },
    // Поделиться конкретным чатом из списка: открываем доступ другу-ролевику.
    // Открыть модалку приглашения друзей в чат s (вместо ввода логина руками).
    async openInvite(s) {
      if (!s || !s.id) return;
      this.inviteSessionId = s.id;
      this.inviteSelected = [];
      await this.loadFriends();   // подтянуть актуальный список друзей
      this.inviteOpen = true;
    },
    toggleInvite(username) {
      const i = this.inviteSelected.indexOf(username);
      if (i >= 0) this.inviteSelected.splice(i, 1);
      else this.inviteSelected.push(username);
    },
    async submitInvite() {
      if (!this.inviteSessionId || !this.inviteSelected.length) return;
      let ok = 0;
      for (const username of this.inviteSelected) {
        try {
          await this.api("/sessions/" + this.inviteSessionId + "/share", {
            method: "POST", body: JSON.stringify({ username }),
          });
          ok++;
        } catch (e) { /* пропускаем тех, кого не вышло */ }
      }
      this.inviteOpen = false;
      if (ok) this.showToast("Приглашено: " + ok);
    },
    // ---------- Отладочный лог LLM ----------
    async openDebug() {
      this.debugOpen = true;
      await this.loadDebug();
      clearInterval(this._debugTimer);
      this._debugTimer = setInterval(() => { if (this.debugOpen) this.loadDebug(); }, 2000);
    },
    closeDebug() { this.debugOpen = false; clearInterval(this._debugTimer); },
    async loadDebug() {
      try {
        const r = await this.api("/debug/log" + (this.debugAll ? "?all=true" : ""));
        this.debugEntries = r.entries || [];
        this.debugCanSeeAll = !!r.can_see_all;
      } catch (e) {}
    },
    async clearDebug() {
      await this.api("/debug/log" + (this.debugAll ? "?all=true" : ""), { method: "DELETE" });
      this.debugEntries = [];
    },
    async toggleDebugScope() { this.debugAll = !this.debugAll; await this.loadDebug(); },

    // ---------- Расход токенов ----------
    async loadUsage() {
      try { this.usage = await this.api("/usage?days=7"); } catch (e) { this.usage = null; }
    },
    // Красивое число: 1234567 -> «1.23 млн», 45678 -> «45.7 тыс.»
    fmtTokens(n) {
      n = Number(n || 0);
      if (n >= 1e6) return (n / 1e6).toFixed(2) + " млн";
      if (n >= 1e3) return (n / 1e3).toFixed(1) + " тыс.";
      return String(n);
    },
    // Какая доля входа пришла из кэша провайдера (чем больше, тем дешевле ход).
    cacheHitPct(row) {
      const p = Number((row && row.prompt) || 0);
      if (!p) return 0;
      return Math.round((Number(row.cached || 0) / p) * 100);
    },
    // ---------- Обход цензуры ----------
    setSafetyOverride(category, threshold) {
      const next = { ...(this.params.safety_overrides || {}) };
      if (threshold) next[category] = threshold;
      else delete next[category];   // «как общий порог» = убрать переопределение
      this.params.safety_overrides = next;
      this.saveUiPrefs();
    },
    saveJailbreakPreset() {
      const name = (this.jailbreakPresetName || "").trim();
      const text = (this.jailbreak.text || "").trim();
      if (!name || !text) { this.showToast("Нужны имя пресета и текст"); return; }
      const presets = (this.jailbreak.presets || []).filter((p) => p.name !== name);
      presets.push({ name, text });
      this.jailbreak = { ...this.jailbreak, presets };
      this.jailbreakPresetName = "";
      this.saveUiPrefs();
      this.showToast(`Пресет «${name}» сохранён`);
    },
    applyJailbreakPreset(p) {
      this.jailbreak = { ...this.jailbreak, text: p.text };
      this.saveUiPrefs();
      this.showToast(`Применён пресет «${p.name}»`);
    },
    deleteJailbreakPreset(i) {
      const presets = [...(this.jailbreak.presets || [])];
      presets.splice(i, 1);
      this.jailbreak = { ...this.jailbreak, presets };
      this.saveUiPrefs();
    },

    isEconomyMode(m) {
      return Object.keys(m.v).every((k) => Number(this.params[k]) === Number(m.v[k]));
    },
    applyEconomyMode(m) {
      this.params = { ...this.params, ...m.v };
      this.saveUiPrefs();
      this.showToast(`Режим расхода: ${m.label}`);
    },
    // Число из поля лимита: целое в [min, max] (min по умолчанию 0). Пустое поле
    // или мусор оставляют прежнее значение. Сервер проверяет эти лимиты строго
    // (целое 0..1000), и «2,5» или стёртое поле, уйди они в params, ломали бы
    // ошибкой 422 КАЖДЫЙ следующий ход, а не одну настройку. Поле тут же
    // показывает то, что сохранилось, — иначе при совпадении с прежним значением
    // Vue не перерисовал бы его, и в поле осталось бы непринятое «-5». Нижняя
    // граница нужна полям мастер-памяти: пакет из 0 сообщений или снимок в
    // 0 токенов сервер отвергнет так же, как «-5».
    numFromInput(ev, max, current, min = 0) {
      const raw = String((ev && ev.target && ev.target.value) || "").trim().replace(",", ".");
      const n = raw === "" ? NaN : Number(raw);
      const v = Number.isFinite(n) ? Math.min(max, Math.max(min, Math.round(n))) : current;
      if (ev && ev.target) ev.target.value = v;
      return v;
    },

    // ---------- Профиль / привязка Telegram ----------
    openProfile() { this.profileOpen = true; this.linkCode = ""; },
    async linkTelegram() {
      const r = await this.api("/auth/link/telegram", { method: "POST" });
      this.linkCode = r.code;
    },

    // ---------- Администрирование ----------
    async openAdmin() {
      this.adminOpen = true;
      if (this.authStatus.accounts_enabled) {
        // Режим аккаунтов: доступ к админке — по роли (проверяет сервер по токену).
        this.adminAuthed = true;
      } else if (this.authStatus.admin_set && !this.adminAuthed) {
        // Режим кода доступа: спросим пароль администратора.
        const ok = this.adminPassword && (await this.checkAdmin(this.adminPassword));
        if (!ok) return;
        this.adminAuthed = true;
      } else {
        this.adminAuthed = true;
      }
      await this.loadAdmin();
    },
    checkAdmin(password) {
      return fetch("/api/auth/admin", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ password }),
      }).then((r) => r.json()).then((d) => d.ok).catch(() => false);
    },
    async submitAdminPass() {
      const ok = await this.checkAdmin(this.adminPassInput);
      if (!ok) { this.accessError = "Неверный пароль"; return; }
      this.adminPassword = this.adminPassInput;
      localStorage.setItem("adminPassword", this.adminPassword);
      this.adminAuthed = true;
      this.accessError = "";
      await this.loadAdmin();
    },
    async loadAdmin() {
      this.adminSec = await this.api("/admin/security");
      if (!this.adminSec.basic_auth) this.adminSec.basic_auth = { enabled: false, username: "", password: "" };
      // Что задано снаружи — решает сервер, клиент только показывает.
      this.envLocked = this.adminSec.env_locked || {};
      this.adminTg = await this.api("/admin/telegram");
      try { this.adminUsers = await this.api("/admin/users"); } catch (e) { this.adminUsers = []; }
    },
    async setUserRole(u, role) {
      await this.api("/admin/users/" + u.id + "/role", { method: "POST", body: JSON.stringify({ role }) });
      await this.loadAdmin();
    },
    async deleteUser(u) {
      if (!(await this.askConfirm("Удалить пользователя «" + u.username + "»?", { okText: "Удалить" }))) return;
      try {
        await this.api("/admin/users/" + u.id, { method: "DELETE" });
      } catch (e) { this.showToast(e.message); }
      await this.loadAdmin();
    },
    async saveSecurity() {
      const ba = this.adminSec.basic_auth || {};
      const baOn = ba.enabled && ba.username;
      await this.api("/admin/security", { method: "PUT", body: JSON.stringify(this.adminSec) });
      // Если поменяли пароль админа — запомним его, чтобы не разлогиниться.
      if (this.adminSec.admin_password) {
        this.adminPassword = this.adminSec.admin_password;
        localStorage.setItem("adminPassword", this.adminPassword);
      }
      // Если поменяли код доступа — обновим свой.
      if (this.adminSec.access_code) {
        this.accessCode = this.adminSec.access_code;
        localStorage.setItem("accessCode", this.accessCode);
      }
      this.authStatus = await fetch("/api/auth/status").then((r) => r.json());
      // HTTP Basic Auth вступает в силу на уровне браузера — нужна перезагрузка,
      // чтобы появилось системное окно входа и браузер запомнил учётку.
      if (baOn) {
        await this.askConfirm(
          "HTTP Basic Auth включён. Страница перезагрузится — браузер спросит логин и пароль.",
          { okText: "Перезагрузить", cancelText: "Позже", danger: false }
        );
        location.reload();
        return;
      }
      this.showToast("Сохранено");
    },
    async saveTelegram() {
      await this.api("/admin/telegram", {
        method: "PUT",
        body: JSON.stringify({
          token: this.adminTg.token,
          enabled: this.adminTg.enabled,
          open_to_all: this.adminTg.open_to_all,
          model: this.adminTg.model,
          default_character_id: this.adminTg.default_character_id,
        }),
      });
      this.showToast("Сохранено");
    },
    async startBot() {
      try {
        this.adminTg.bot_state = await this.api("/admin/telegram/start", { method: "POST" });
      } catch (e) { this.showToast("Не удалось запустить бота — проверьте токен."); }
    },
    async stopBot() {
      this.adminTg.bot_state = await this.api("/admin/telegram/stop", { method: "POST" });
    },
    async wlAdd(id) {
      if (!id) return;
      this.adminTg = await this.api("/admin/telegram/whitelist/" + parseInt(id), { method: "POST" });
      this.newWlId = "";
    },
    async wlRemove(id) {
      this.adminTg = await this.api("/admin/telegram/whitelist/" + id, { method: "DELETE" });
    },

    // ---------- Инициализация приложения (после прохождения гейта) ----------
    async initApp() {
      // Ворота пройдены: с этого момента фоновые перечитки списка чатов
      // (сигнал соседней вкладки, возвращение к окну, минутный опрос) законны.
      this._appReady = true;
      await this.loadConnection();
      await this.loadPresets();
      const def = this.presets.find((p) => p.is_default);
      // Дефолт-пресет задаёт params; message_preload подтягиваем в любом случае.
      if (def) { this.applyPreset(def); await this.loadUiPrefs(false); }
      else await this.loadUiPrefs();
      await Promise.all([this.loadCharacters(), this.loadPersonas(), this.loadHorae(),
        this.loadGroups(), this.loadFriends(), this.loadAllSessions()]);
      this._safetyHint();
      // Восстанавливаем последний открытый чат (после F5 сразу можно писать);
      // если не вышло — показываем блок быстрого старта, а не открываем первого
      // персонажа молча: раньше это создавало чат как побочный эффект запуска.
      await this._restoreLastChat();
      // Периодически подтягиваем заявки в друзья/общие чаты — чтобы уведомления
      // в колокольчике появлялись без перезагрузки страницы.
      clearInterval(this._friendsTimer);
      if (this.authStatus.accounts_enabled && this.userToken) {
        this._friendsTimer = setInterval(() => this.loadFriends(), 20000);
      }
      // Сторож зависшего стриминга. Полуоткрытый TCP (удалённый сервер, NAT) не даёт
      // onclose: сокет «жив», но события не приходят — «печатает…» висит вечно, хотя
      // ответ давно сохранён на сервере. Если 45с тишины — дослушиваем задачу через
      // SSE (он отдаёт весь накопленный буфер), а без job_id просто перечитываем БД.
      clearInterval(this._streamWatchdog);
      this._streamWatchdog = setInterval(() => {
        if (!this.streaming) return;
        if (Date.now() - (this._lastEvtAt || 0) < 45000) return;
        this._lastEvtAt = Date.now();
        if (this.currentJobId) this.resumeSSE(this.currentJobId);
        else this.finishStream();
      }, 15000);
      // Esc закрывает верхний оверлей — как ожидают от десктопного приложения.
      this._bindWake();
      if (!this._escBound) {
        this._escBound = true;
        // Удержание фокуса внутри верхнего оверлея. Без него Tab уходил гулять
        // по интерфейсу под затемнением: при окне в 40 сообщений и 8-11 кнопках
        // под каждым это больше 300 остановок на невидимых элементах.
        window.addEventListener("keydown", (e) => {
          if (e.key !== "Tab" || e.defaultPrevented) return;
          const el = this._topOverlay();
          if (!el) return;
          const items = this._focusables(el);
          if (!items.length) return;
          const first = items[0];
          const last = items[items.length - 1];
          const cur = document.activeElement;
          if (!el.contains(cur)) { e.preventDefault(); first.focus(); return; }
          if (e.shiftKey && cur === first) { e.preventDefault(); last.focus(); }
          else if (!e.shiftKey && cur === last) { e.preventDefault(); first.focus(); }
        });
        // Палитра: Ctrl+K на Windows и Linux, Cmd+K на маке. Перехватываем до
        // браузера, иначе Ctrl+K уедет в его строку поиска.
        window.addEventListener("keydown", (e) => {
          if (e.key !== "k" && e.key !== "K" && e.key !== "л" && e.key !== "Л") return;
          if (!(e.ctrlKey || e.metaKey) || e.altKey) return;
          e.preventDefault();
          if (this.paletteOpen) this.closePalette();
          else this.openPalette();
        });
        window.addEventListener("keydown", (e) => {
          if (e.key !== "Escape" || e.defaultPrevented) return;
          if (this.paletteOpen) { this.closePalette(); return; }
          if (this.inspectorOpen) { this.closeInspector(); return; }
          if (this.dialog) { this.dialogCancel(); return; }
          if (this.lightbox) { this.lightbox = null; return; }
          if (this.headerMenu) { this.headerMenu = false; return; }
          if (this.plusMenu) { this.plusMenu = false; return; }
          if (this.notifOpen) { this.notifOpen = false; return; }
          if (this.inviteOpen) { this.inviteOpen = false; return; }
          if (this.membersOpen) { this.membersOpen = false; return; }
          if (this.kbOpen) { this.kbOpen = false; return; }
          if (this.groupModal) { this.groupModal = false; return; }
          if (this.profileOpen) { this.profileOpen = false; return; }
          if (this.debugOpen) { this.debugOpen = false; return; }
          if (this.usageOpen) { this.usageOpen = false; return; }
          if (this.adminOpen) { this.adminOpen = false; return; }
          if (this.drawerTab) { this.drawerTab = null; return; }
          // Режим композера — тоже слой, и выходить из него надо той же клавишей.
          // Раньше из режима арта можно было выйти только маленькой ссылкой
          // «отмена» внутри плашки, и обычное сообщение легко уходило в
          // генератор картинок.
          if (this.composerMode !== "text") { this.composerMode = "text"; return; }
          if (this.sidebarOpen && this.isTouch) { this.sidebarOpen = false; }
        });
      }
    },
  },

  watch: {
    // Новые пустые чаты ловим на любом перечитывании списков — откуда бы чат ни
    // взялся: эта вкладка, соседняя, импорт или Telegram-бот.
    // Меню «⋯» закрывают не только его кнопкой: любое вторичное действие,
    // сужение окна, открытие меню у соседней реплики. После каждого такого
    // закрытия намёк data-more у прежней ленты устаревал и гасил её правый край
    // — у реплик пользователя ровно там, где стоит «⋯». Пересчитываем все ленты
    // с намёком, а не только ту, по которой нажали.
    msgMenu() {
      this.$nextTick(() => {
        document.querySelectorAll(".msg-actions[data-more]").forEach((el) => this.msgStripEdge(el));
      });
    },
    // Любое изменение параметров генерации сохраняем в системе (с дебаунсом).
    params: { handler() { this.saveUiPrefs(); }, deep: true },
    // Бюджет хода — знаменатель монитора токенов и полосы в шапке: после его
    // сохранения (PUT уходит из наблюдателя выше) отчёт перечитываем, иначе
    // «из бюджета хода» до следующего хода показывал бы прежнее число.
    "params.context_tokens"() { this._ctxAfterSave = true; },
    soundOn(v) { localStorage.setItem("soundOn", v ? "1" : "0"); },

    // Фокус при открытии оверлея уходит внутрь, при закрытии ВОЗВРАЩАЕТСЯ на
    // вызвавший элемент. Раньше клавиатурный путь после каждого закрытия
    // начинался заново с начала страницы, а до дровера, стоящего в шаблоне
    // после ленты, надо было протабать весь чат.
    overlayOpen(open) {
      if (open) {
        this._focusReturn = document.activeElement;
        this.$nextTick(() => {
          const el = this._topOverlay();
          if (!el) return;
          const first = this._focusables(el)[0];
          if (first) first.focus();
          else { el.setAttribute("tabindex", "-1"); el.focus(); }
        });
        return;
      }
      const back = this._focusReturn;
      this._focusReturn = null;
      // Элемент мог исчезнуть вместе с закрытым окном — тогда возвращать некуда.
      if (back && document.contains(back)) back.focus();
    },

    // Объявляем этапы хода, а не токены.
    streaming(on) {
      this.liveStatus = on ? "Генерация ответа началась" : "Ответ получен";
      if (on) this._startTelemetry();
      else this._stopTelemetry();
    },
    // Полоса заполнения окна должна относиться к ОТКРЫТОМУ чату, иначе она
    // показывала бы вес предыдущего.
    // То же со статусом мастер-памяти: опрос прежнего чата снимаем, иначе его
    // ответ (и тост о завершении) пришёл бы в чужой чат. Статус нужен только
    // открытой вкладке «Память» — без неё запрос не шлём. Счёт сбоев связи
    // тоже чужой: в новом чате «Нет связи» не должно всплыть после первого же.
    sessionId() {
      this.ctxStats = null;
      this.loadCtxStats();
      this._stopMemPoll();
      this.memStatus = null;
      this.memPollFails = 0;
      if (this.drawerTab === "memory") this.loadMemStatus();
    },
    // Вкладку «Память» открывают не только её кнопкой (импорт чата переключает
    // на неё сам), поэтому статус грузим по факту смены вкладки. Монитор
    // токенов — тоже: отчёт снят после прошлого хода, а с тех пор могли
    // смениться окно, бюджет или снимок.
    drawerTab(tab) {
      // «Хроники» отдельной вкладкой больше нет — она внутри «Памяти».
      if (tab === "horae") {
        this.openMemory("state");
        return;
      }
      if (tab === "memory") {
        this.loadMemStatus();
        this.loadCtxStats();
        this.loadHoraeGlobal();
      }
    },
    // Ввод в палитре: поиск по репликам идёт на сервер с дебаунсом, чтобы не
    // слать запрос на каждую букву.
    paletteQuery() {
      this.paletteIndex = 0;
      this.scheduleSearch();
    },
    chatError(text) {
      if (text) this.liveStatus = "Ошибка генерации: " + text;
    },
  },

  async mounted() {
    // Копирование кода: один слушатель на документ (разметка сообщений приходит
    // из v-html и перерисовывается, вешать обработчики на каждый блок бессмысленно).
    document.addEventListener("click", this._onDocClick);
    // Клавиши перемещения по чату: Home/End и Alt+↑/↓ (см. _onNavKey — в полях
    // ввода они не перехватываются).
    document.addEventListener("keydown", this._onNavKey);
    // Реактивный список чатов: мутации шлют CHATLIST_EVENT, здесь его ловим
    // (см. _onChatListEvent). Vue перерисует только изменившиеся строки
    // (key = вид + id): без моргания, без сброса прокрутки сайдбара и без потери
    // фокуса — список при перечитке не очищается, новые строки просто ложатся
    // на место старых.
    window.addEventListener(CHATLIST_EVENT, this._onChatListEvent);
    // Соседние вкладки. Проверка наличия обязательна: BroadcastChannel нет в
    // старых Safari, а конструктор бросает в песочнице с запрещённым хранилищем.
    try {
      if (typeof BroadcastChannel !== "undefined") {
        this._chatChannel = new BroadcastChannel(CHATLIST_CHANNEL);
        this._chatChannel.onmessage = this._onChatChannel;
      }
    } catch (e) { this._chatChannel = null; }
    // Реплики из Telegram-бота сервер не объявляет: подбираем их, когда человек
    // возвращается к вкладке, и раз в минуту, пока вкладка на виду. Скрытую
    // вкладку не опрашиваем — там список всё равно никто не видит.
    document.addEventListener("visibilitychange", this._onChatListWake);
    window.addEventListener("focus", this._onChatListWake);
    this._chatListPoll = setInterval(this._pollChatList, 60000);
    // Ширина окна для строки действий под сообщением. Слушаем изменение, а не
    // читаем один раз: окно десктопа сужают и расширяют на ходу, и строка
    // должна перестраиваться вместе с вёрсткой, а не после F5.
    try {
      const mq = window.matchMedia && window.matchMedia("(max-width: 767.98px)");
      if (mq) {
        this.narrow = mq.matches;
        const onMq = (ev) => { this.narrow = ev.matches; if (!ev.matches) this.msgMenu = null; };
        if (mq.addEventListener) mq.addEventListener("change", onMq);
        else if (mq.addListener) mq.addListener(onMq);   // Safari до 14
      }
    } catch (e) { this.narrow = false; }
    // Тип указателя. Раскрытие действий по наведению существует только для
    // мыши: правило вынесено в @media (hover: hover), и на телефоне оно не
    // срабатывает никогда — там нужна своя механика раскрытия, а не та же
    // разметка в другом оформлении.
    try {
      this.coarse = !!(window.matchMedia && window.matchMedia("(pointer: coarse)").matches);
    } catch (e) { this.coarse = false; }
    this.accessCode = localStorage.getItem("accessCode") || "";
    this.adminPassword = localStorage.getItem("adminPassword") || "";
    this.userToken = localStorage.getItem("userToken") || "";
    this.soundOn = localStorage.getItem("soundOn") !== "0";
    try {
      this.authStatus = await fetch("/api/auth/status").then((r) => r.json());
    } catch (e) {}
    // Режим аккаунтов: нужен вход по логину/паролю.
    if (this.authStatus.accounts_enabled) {
      const me = this.userToken ? await this.fetchMe() : null;
      if (!me) { this.needAuth = true; return; }
      this.currentUserObj = me;
      await this.initApp();
      return;
    }
    // Иначе — код доступа (если задан).
    if (this.authStatus.access_required) {
      const ok = this.accessCode && (await this.checkCode(this.accessCode));
      if (!ok) { this.needAccess = true; return; }
    }
    await this.initApp();
  },

  template: `
  <div v-if="needAuth" class="gate">
    <div class="gate-box">
      <h2>TaleEngine</h2>
      <!-- Вкладки были парой кнопок, у которых состояние несла ОДНА css-заливка:
           в дереве доступности лежали два одинаковых пункта, и какой из них
           открыт — не сообщалось ничем. Теперь это настоящий tablist: роль,
           aria-selected и стрелки влево/вправо. type="button" проставлен явно —
           кнопка без типа внутри формы отправляет её. -->
      <div class="row" style="gap:6px; margin-bottom:8px" role="tablist" aria-label="Вход или регистрация">
        <button type="button" ref="gateTabLogin" id="gate-tab-login" role="tab"
                aria-controls="gate-panel" :aria-selected="authTab==='login' ? 'true' : 'false'"
                :tabindex="authTab==='login' ? 0 : -1"
                :class="authTab==='login'?'btn-primary':''" style="flex:1"
                @click="setAuthTab('login')"
                @keydown.left.prevent="setAuthTab('register', true)"
                @keydown.right.prevent="setAuthTab('register', true)">Вход</button>
        <button type="button" ref="gateTabRegister" id="gate-tab-register" role="tab"
                aria-controls="gate-panel" :aria-selected="authTab==='register' ? 'true' : 'false'"
                :tabindex="authTab==='register' ? 0 : -1"
                :class="authTab==='register'?'btn-primary':''" style="flex:1"
                @click="setAuthTab('register')"
                @keydown.left.prevent="setAuthTab('login', true)"
                @keydown.right.prevent="setAuthTab('login', true)">Регистрация</button>
      </div>
      <div id="gate-panel" role="tabpanel"
           :aria-labelledby="authTab==='register' ? 'gate-tab-register' : 'gate-tab-login'">
        <!-- Постоянный текст, а не подсказка по наведению: восстановления пароля
             в продукте нет, сбросить его может только администратор, и цена
             незнания здесь — все истории, которые человек напишет. О таком
             предупреждают до того, как пароль придуман, а не после. -->
        <p v-if="authTab==='register'" id="gate-pass-warn" class="gate-warn">
          Восстановления пароля нет. Забыли — сбросить сможет только
          администратор, а до этого ваши истории останутся недоступны.
          Сохраните пароль в менеджере паролей.
        </p>
        <!-- Настоящая form: Enter срабатывает из любого поля, а не только из
             пароля, куда он был подвешен вручную. required вернул честную
             реакцию на пустую отправку — раньше пустая форма уходила на сервер
             и возвращалась чужим по смыслу «Неверный логин или пароль». -->
        <form @submit.prevent="submitAuth">
          <!-- Подпись даёт <label>, а не placeholder: placeholder исчезает с
               первым набранным символом, и у поля не остаётся имени ни на
               экране, ни в дереве доступности (WCAG 3.3.2). -->
          <label>Логин
            <input v-model="authForm.username" type="text" required autocomplete="username" />
          </label>
          <!-- autocomplete здесь не украшение: без него менеджер паролей не
               предлагает сохранить учётку, а восстанавливать её потом нечем. -->
          <label>Пароль
            <input v-model="authForm.password" :type="authPassShown ? 'text' : 'password'"
                   required :minlength="authTab==='register' ? 8 : null"
                   :autocomplete="authTab==='register' ? 'new-password' : 'current-password'"
                   :aria-describedby="authTab==='register' ? 'gate-pass-warn' : null" />
            <span v-if="authTab==='register'" class="gate-hint">Не короче 8 символов.</span>
          </label>
          <label v-if="authTab==='register'">Повторите пароль
            <input v-model="authPassword2" :type="authPassShown ? 'text' : 'password'"
                   required autocomplete="new-password" />
          </label>
          <!-- Показ пароля — обычный чекбокс, а не кнопка внутри поля: он
               открывает обе строки разом, и именно так проверяют совпадение. -->
          <label class="check">
            <input type="checkbox" v-model="authPassShown" /> Показать пароль
          </label>
          <p v-if="accessError" class="status-err" role="alert">{{ accessError }}</p>
          <button type="submit" class="btn-primary" style="width:100%; margin-top:8px">
            {{ authTab==='register' ? 'Зарегистрироваться' : 'Войти' }}
          </button>
        </form>
      </div>
    </div>
  </div>

  <div v-else-if="needAccess" class="gate">
    <div class="gate-box">
      <h2>TaleEngine</h2>
      <p class="muted">Приложение защищено кодом доступа.</p>
      <!-- Та же болезнь, что и на входе: поле было подписано только
           placeholder-ом и оставалось безымянным с первого символа, а Enter
           висел на самом поле вместо формы. -->
      <form @submit.prevent="submitAccess">
        <label>Код доступа
          <!-- Код общий на всю установку, а не личный пароль: autocomplete
               выключен, чтобы менеджер паролей не завёл под него учётку. -->
          <input v-model="accessInput" type="password" required autocomplete="off" />
        </label>
        <p v-if="accessError" class="status-err" role="alert">{{ accessError }}</p>
        <button type="submit" class="btn-primary" style="width:100%; margin-top:8px">Войти</button>
      </form>
    </div>
  </div>

  <template v-else>
  <div :class="['app-grid', !sidebarOpen ? 'sb-hidden' : '', canvasOpen ? 'with-canvas' : '', 'pane-' + mobilePane, 'view-' + viewMode, 'shell-' + shellLayout]">

    <!-- Заголовок первого уровня. Во всём приложении не было НИ ОДНОГО h1:
         скринридер открывал страницу без точки входа, а команда «перейти к
         заголовку 1» не находила ничего. Глазами он не нужен — название чата
         и так стоит в шапке, — поэтому .sr-only, а не видимая строка. -->
    <h1 class="sr-only">TaleEngine</h1>

    <!-- Затемнение под мобильным сайдбаром -->
    <div v-if="sidebarOpen" class="backdrop" @click="sidebarOpen=false"></div>

    <!-- ===== Левый сайдбар: разделы-аккордеоны ===== -->
    <!-- Ориентир навигации. В дереве доступности не было ни одного ориентира:
         быстрый переход по регионам (rotor, D в NVDA) приводил в пустоту, и до
         списка чатов приходилось табать через всю шапку. -->
    <div :class="['sidebar', sidebarOpen ? 'open' : '']" role="navigation" aria-label="Персонажи и чаты">

      <!-- Раздел: Персонажи -->
      <div class="acc">
        <button class="acc-head" @click="toggleSection('characters')"
                :aria-expanded="openSections.characters ? 'true' : 'false'">
          <span class="acc-icon">🎭</span><span class="acc-title">Персонажи</span>
          <span class="acc-chevron" :class="{ open: openSections.characters }">▸</span>
        </button>
        <div class="acc-body" :class="{ open: openSections.characters }">
          <div class="row" style="padding: 6px 12px; gap:6px">
            <button class="btn-primary" style="flex:1" @click="createCharacter">+ Новый</button>
            <label class="btn-icon" style="margin:0; cursor:pointer" title="Импорт персонажа PNG/JSON">
              📥<input type="file" accept=".png,.json" class="file-input" @change="importCharacter"
                     aria-label="Импорт персонажа из файла PNG или JSON" />
            </label>
            <label class="btn-icon" style="margin:0; cursor:pointer" title="Импорт чата: нативный AiChat (.aichat.json) или SillyTavern (.jsonl)">
              💬<input type="file" accept=".jsonl,.json" class="file-input" @change="importChat"
                     aria-label="Импорт чата из файла AiChat или SillyTavern" />
            </label>
          </div>
          <div class="list-search" v-if="characters.length > 5">
            <!-- Полю поиска нужно ИМЯ, а не только подсказка: placeholder гаснет
                 на первом же введённом символе и именем поля не считается. -->
            <input v-model="charFilter" placeholder="Поиск по персонажам…" aria-label="Поиск по персонажам" @click.stop />
            <button v-if="charFilter" class="btn-icon" @click.stop="charFilter=''" title="Очистить" aria-label="Очистить">✕</button>
          </div>
          <!-- Строка списка это КНОПКА, а не div с обработчиком: иначе с клавиатуры
               можно удалить чат, но нельзя его открыть. Действия лежат СОСЕДЯМИ
               кнопки, а не внутри неё: интерактивный элемент внутри кнопки
               недопустим и ломает и клавиатуру, и скринридер. -->
          <div v-for="c in filteredCharacters" :key="c.id"
               :class="['list-item', c.id === selectedCharacterId ? 'active' : '', c.pinned ? 'pinned' : '',
                        rowMenu === 'c'+c.id ? 'row-menu-open' : '']">
            <button type="button" class="row-main" @click="rowMenu = null; selectCharacter(c)"
                    :aria-current="c.id === selectedCharacterId ? 'true' : null"
                    :aria-label="'Персонаж ' + c.name + (c.pinned ? ', закреплён' : '')">
              <span class="avatar" aria-hidden="true"><img v-if="c.avatar_path" :src="c.avatar_path" class="avatar" alt="" />{{ c.avatar_path ? '' : c.name.charAt(0) }}</span>
              <span class="grow row-title">
                <span v-if="c.pinned" class="pin-mark" aria-hidden="true">📌</span>
                <span class="row-name">{{ c.name }}</span>
              </span>
            </button>
            <button v-if="coarse && rowMenu !== 'c'+c.id" class="btn-icon row-more"
                    @click.stop="rowMenu = 'c'+c.id"
                    title="Действия с персонажем" aria-label="Действия с персонажем"
                    aria-expanded="false">⋯</button>
            <span class="row-actions" v-if="!coarse || rowMenu === 'c'+c.id">
              <button class="btn-icon" @click.stop="togglePinCharacter(c)"
                      :title="c.pinned ? 'Открепить' : 'Закрепить наверху'" :aria-label="c.pinned ? 'Открепить' : 'Закрепить наверху'">{{ c.pinned ? '📍' : '📌' }}</button>
              <button class="btn-icon" @click.stop="exportCharacter(c)" title="Экспорт (JSON + лор Horae)" aria-label="Экспорт (JSON + лор Horae)">⬇</button>
              <button class="btn-icon" @click.stop="deleteCharacter(c)" title="Удалить" aria-label="Удалить">🗑</button>
              <button v-if="coarse" class="btn-icon" @click.stop="rowMenu = null"
                      title="Свернуть" aria-label="Свернуть действия">✕</button>
            </span>
          </div>
          <div v-if="charFilter && !filteredCharacters.length" class="list-empty">Ничего не найдено</div>
          <!-- Пустое состояние существовало только для поиска: при нуле
               персонажей раздел показывал две кнопки и пустоту под ними, и
               ничто не говорило, что делать дальше и с чего начинается
               хорошая история. -->
          <div v-else-if="!charFilter && !characters.length" class="list-empty">
            Персонажей пока нет. Создайте своего кнопкой «+ Новый» — или
            импортируйте готовую карточку SillyTavern (PNG или JSON).
          </div>
        </div>
      </div>

      <!-- Раздел: ВСЕ чаты. Раньше он существовал только при выбранном
           персонаже, и чтобы найти чат, надо было сначала вспомнить персонажа,
           то есть вспомнить ответ до того, как задал вопрос. Теперь это одна
           лента личных, групповых и общих чатов, а персонаж — фильтр поверх. -->
      <div class="acc">
        <button class="acc-head" @click="toggleSection('chats')"
                :aria-expanded="openSections.chats ? 'true' : 'false'">
          <span class="acc-icon">💬</span>
          <span class="acc-title">Чаты<template v-if="chatFilterChar !== null && selectedCharacter"> — {{ selectedCharacter.name }}</template></span>
          <span class="acc-chevron" :class="{ open: openSections.chats }">▸</span>
        </button>
        <div class="acc-body" :class="{ open: openSections.chats }">
          <div class="row" style="padding: 6px 12px; gap:6px">
            <button @click="newChat" style="flex:1" :disabled="!selectedCharacter"
                    :title="selectedCharacter ? 'Новый чат с ' + selectedCharacter.name : 'Сначала выберите персонажа'">+ Новый чат</button>
            <button class="btn-icon" @click="openGroupModal" title="Собрать группу персонажей"
                    aria-label="Собрать группу персонажей">👥</button>
            <button class="btn-icon" @click="openPalette()" title="Поиск по сообщениям (Ctrl+K)"
                    aria-label="Поиск по сообщениям, Ctrl+K">🔍</button>
          </div>
          <!-- Чипы фильтра. Клик по чипу меняет ТОЛЬКО фильтр: он не открывает
               чат и не закрывает сайдбар, поэтому по спискам можно ходить. -->
          <div class="chip-row" v-if="chatFilterChips.length > 1">
            <button class="filter-chip" :class="{ on: chatFilterChar === null }"
                    @click="chatFilterChar = null"
                    :aria-pressed="chatFilterChar === null ? 'true' : 'false'">Все · {{ unifiedChats.length }}</button>
            <button v-for="ch in chatFilterChips" :key="'fc'+ch.id" class="filter-chip"
                    :class="{ on: chatFilterChar === ch.id }"
                    @click="chatFilterChar = (chatFilterChar === ch.id ? null : ch.id)"
                    :aria-pressed="chatFilterChar === ch.id ? 'true' : 'false'">{{ ch.name }} · {{ ch.n }}</button>
          </div>
          <div class="list-search" v-if="unifiedChats.length > 5">
            <input v-model="chatFilter" placeholder="Поиск по названиям…" aria-label="Поиск по названиям чатов" @click.stop />
            <button v-if="chatFilter" class="btn-icon" @click.stop="chatFilter=''" title="Очистить" aria-label="Очистить">✕</button>
          </div>
          <div v-for="s in visibleChats" :key="s.kind + s.id"
               :class="['list-item', 'row2', s.id === sessionId ? 'active' : '', s.pinned ? 'pinned' : '',
                        rowMenu === 's'+s.id ? 'row-menu-open' : '']">
            <button type="button" class="row-main" @click="openChatRow(s)"
                    :title="s.title + ' — чат #' + s.id"
                    :aria-current="s.id === sessionId ? 'true' : null"
                    :aria-label="(s.kind === 'group' ? 'Группа ' : s.kind === 'shared' ? 'Общий чат ' : 'Чат ') + s.title + (s.character_name ? ', ' + s.character_name : '') + (s.pinned ? ', закреплён' : '') + (pendingChats.includes(s.id) ? ', пришёл новый ответ' : '') + (s.last_at ? ', ' + shortWhen(s.last_at) : '')">
              <span v-if="pendingChats.includes(s.id)" class="reply-dot" aria-hidden="true"></span>
              <span class="grow row-text">
                <span class="row-title">
                  <span v-if="s.pinned" class="pin-mark" aria-hidden="true">📌</span>
                  <span v-if="s.kind !== 'chat'" class="kind-mark" aria-hidden="true">{{ s.kind === 'group' ? '👥' : '🔗' }}</span>
                  <span class="row-name">{{ s.title }}</span>
                </span>
                <span class="row-sub" v-if="s.preview">{{ s.preview }}</span>
                <span class="row-sub muted" v-else-if="s.character_name">{{ s.character_name }}</span>
                <span class="row-sub muted" v-else>пустой чат</span>
              </span>
              <span class="row-time" v-if="s.last_at" aria-hidden="true">{{ shortWhen(s.last_at) }}</span>
            </button>
            <!-- На тач-устройствах пять действий прячутся за одну кнопку.
                 Раньше вынос из потока жил только внутри @media (hover: hover),
                 и на телефоне пять кнопок по 44px занимали 234 из 315 пикселей
                 сайдбара: заголовку и превью оставалось около 58, то есть
                 отличить один чат от другого было нельзя. -->
            <button v-if="coarse && s.kind !== 'shared' && rowMenu !== 's'+s.id"
                    class="btn-icon row-more" @click.stop="rowMenu = 's'+s.id"
                    title="Действия с чатом" aria-label="Действия с чатом"
                    aria-expanded="false">⋯</button>
            <span class="row-actions" v-if="s.kind !== 'shared' && (!coarse || rowMenu === 's'+s.id)">
              <button class="btn-icon" @click.stop="togglePinSession(s)"
                      :title="s.pinned ? 'Открепить' : 'Закрепить наверху'" :aria-label="s.pinned ? 'Открепить' : 'Закрепить наверху'">{{ s.pinned ? '📍' : '📌' }}</button>
              <button class="btn-icon" @click.stop="exportSession(s)" title="Экспорт чата (нативный формат AiChat)" aria-label="Экспорт чата (нативный формат AiChat)">💾</button>
              <button v-if="authStatus.accounts_enabled" class="btn-icon" @click.stop="openInvite(s)" title="Пригласить друга" aria-label="Пригласить друга">🔗</button>
              <button class="btn-icon" @click.stop="renameSession(s)" title="Переименовать" aria-label="Переименовать">✎</button>
              <button class="btn-icon" @click.stop="deleteSession(s)" title="Удалить чат" aria-label="Удалить чат">🗑</button>
              <button v-if="coarse" class="btn-icon" @click.stop="rowMenu = null"
                      title="Свернуть" aria-label="Свернуть действия">✕</button>
            </span>
          </div>
          <div v-if="!visibleChats.length" class="list-empty">
            {{ chatFilter || chatFilterChar !== null ? 'Ничего не найдено' : 'Чатов пока нет' }}
          </div>
        </div>
      </div>

      <!-- Разделы «Группы» и «Доступные мне» слились с общим списком чатов выше:
           три списка одной сущности с тремя разными правилами строки были ровно
           тем, из-за чего пользователь не мог опереться ни на одно из них.
           Кнопка создания группы переехала к кнопке нового чата. -->

    </div>

    <!-- ===== Центр: чат ===== -->
    <!-- Основная область. Без role="main" пропуск к содержимому не работал:
         прыгать было некуда, и каждый вход в приложение начинался с обхода
         сайдбара целиком. -->
    <div class="chat" role="main" @dragover.prevent="dragOver = !!sessionId" @dragleave.prevent="dragOver=false" @drop.prevent="onDrop">
      <div v-if="dragOver" class="drop-overlay">📎 Отпустите файл — он прикрепится к сообщению</div>
      <div class="chat-header">
        <button class="btn-icon hamburger" @click="sidebarOpen=!sidebarOpen" title="Меню" aria-label="Меню">☰</button>
        <button v-if="canvasOpen" class="btn-icon only-mobile" @click="mobilePane='canvas'" title="Открыть Canvas" aria-label="Открыть Canvas">📋</button>
        <!-- Аватарка персонажа (для группы — первого участника с аватаркой) -->
        <span v-if="sessionId" class="header-ava">
          <!-- Пустой alt обязателен: без атрибута скринридер зачитывает вслух
               путь к файлу аватарки, а имя персонажа и так стоит рядом текстом. -->
          <img v-if="headerAvatar" :src="headerAvatar" alt="" />
          <span v-else>{{ currentIsGroup ? '👥' : (currentSessionTitle || 'T').charAt(0).toUpperCase() }}</span>
        </span>
        <!-- Идентичность чата: одна строка вместо заголовка и дублирующей его
             пилюли. Номер чата остаётся, по нему удобно ссылаться. -->
        <span class="header-id">
          <span class="title" :title="headerSubtitle">{{ headerTitle }}</span>
          <span v-if="headerSubtitle" class="header-sub">{{ headerSubtitle }}</span>
        </span>
        <!-- Статус связи виден ТОЛЬКО когда связи нет. Вечно зелёный «online»
             занимал место в самой дорогой по вниманию полосе и не читался. -->
        <!-- Распорника здесь нет намеренно: .header-id уже растягивается, и второй
             flex:1 делил бы полосу пополам, подвешивая плашку обрыва связи ровно
             посередине шапки, вдали и от заголовка, и от кнопок. -->
        <span v-if="!connected" class="pill status-err" title="Связь с сервером потеряна, идёт переподключение">⟳ реконнект</span>
        <!-- Три режима — центральное понятие продукта, но жили они в безымянном
             «⋯» среди двенадцати плоских пунктов: пока меню закрыто, концепции
             на экране не существовало, а «Сцена» стояла в одном ряду со «Звук
             вкл». Здесь они на виду и ровно одной группой, а не тремя лишними
             иконками в общей полосе: у поля viewMode одно значение, и орган
             управления у него обязан быть один. Из «⋯» пункты убраны — дубля нет. -->
        <div class="view-seg" role="group" aria-label="Режим ленты">
          <button v-for="m in viewModes" :key="'vm' + m.id" class="btn-icon"
                  :class="viewMode === m.id ? 'on' : ''"
                  :aria-pressed="viewMode === m.id ? 'true' : 'false'"
                  :title="m.label + ' — ' + m.hint"
                  :aria-label="'Режим ленты: ' + m.label + ', ' + m.hint"
                  @click="setViewMode(m.id)">{{ m.icon }}</button>
        </div>
        <button class="btn-icon" @click="openPalette()"
                title="Поиск по сообщениям и команды (Ctrl+K)"
                aria-label="Поиск по сообщениям и команды, Ctrl+K">🔍</button>
        <!-- Колокольчик уведомлений: входящие заявки в друзья -->
        <div v-if="currentUserObj" class="notif-wrap">
          <button class="btn-icon" @click="notifOpen=!notifOpen" title="Уведомления" aria-label="Уведомления">
            🔔<span v-if="friendsIncoming.length" class="notif-badge">{{ friendsIncoming.length }}</span>
          </button>
          <div v-if="notifOpen" class="notif-backdrop" @click="notifOpen=false"></div>
          <div v-if="notifOpen" class="notif-dropdown">
            <div class="notif-head">Уведомления</div>
            <div v-if="!friendsIncoming.length" class="muted" style="padding:10px">Новых заявок нет.</div>
            <div v-for="f in friendsIncoming" :key="f.friendship_id" class="notif-item">
              <div>👤 <b>{{ f.username }}</b> хочет добавить вас в друзья</div>
              <div class="row" style="gap:6px;margin-top:6px">
                <button class="btn-primary" @click="acceptFriend(f)">Принять</button>
                <button class="btn-danger" @click="declineFriend(f)">Отклонить</button>
              </div>
            </div>
          </div>
        </div>
        <button class="btn-icon" @click="drawerTab = drawerTab ? null : 'generation'" title="Настройки" aria-label="Настройки">⚙</button>
        <!-- Редкие действия живут здесь на ВСЕХ экранах, а не только на телефоне.
             Механизм был написан ещё для мобильной шапки, но к десктопу его тогда
             не применили, и девять кнопок продолжали висеть в полосе. -->
        <div class="header-menu-wrap">
          <button class="btn-icon" @click="headerMenu=!headerMenu" title="Ещё" aria-label="Ещё">⋯</button>
          <div v-if="headerMenu" class="plus-backdrop" @click="headerMenu=false"></div>
          <div v-if="headerMenu" class="plus-menu header-menu">
            <!-- Режимы ленты отсюда убраны: они переехали в сегментированный
                 переключатель шапки. Держать их в двух местах нельзя — у одного
                 состояния один орган управления, иначе человек ищет, какой из
                 двух главнее, вместо того чтобы переключать комнату. -->
            <!-- Обустройство комнаты. Раскладка меняется редко — раз выбрал и
                 живёшь, — поэтому её место здесь и в палитре, а не рядом с
                 режимом ленты в шапке: частое и редкое рядом путают. -->
            <div class="menu-group" role="group" aria-label="Раскладка оболочки">
              <span class="menu-group-title">Раскладка</span>
              <button v-for="s in shellLayouts" :key="'sl' + s.id"
                      :class="{ on: shellLayout === s.id }"
                      :aria-pressed="shellLayout === s.id ? 'true' : 'false'"
                      :title="s.hint"
                      @click="setShellLayout(s.id); headerMenu=false">{{ s.icon }} {{ s.label }}</button>
            </div>
            <button v-if="sessionId" @click="openInspector(); headerMenu=false">🔬 Инспектор хода</button>
            <button v-if="sessionId" @click="openKnowledge(); headerMenu=false">📚 База знаний</button>
            <button v-if="sessionId && !sharedView" @click="openMembers(); headerMenu=false">👥➕ {{ currentIsGroup ? 'Участники группы' : 'Добавить персонажа' }}</button>
            <button v-if="currentIsGroup" @click="toggleDirector(); headerMenu=false">🎬 ИИ-режиссёр: {{ currentGroup.director ? 'вкл' : 'выкл' }}</button>
            <button @click="soundOn=!soundOn; headerMenu=false">{{ soundOn ? '🔊 Звук вкл' : '🔇 Звук выкл' }}</button>
            <button v-if="currentUserObj" @click="openProfile(); headerMenu=false">👤 Профиль {{ currentUserObj.username }}</button>
            <button v-if="sessionId && authStatus.accounts_enabled && !sharedView" @click="openInvite({ id: sessionId }); headerMenu=false">👥 Пригласить в чат</button>
            <button v-if="sessionId" @click="bgPicker=!bgPicker; headerMenu=false">🖼 Фон чата</button>
            <button @click="openDebug(); headerMenu=false">🐞 Отладка LLM</button>
            <button v-if="isAdmin" @click="openAdmin(); headerMenu=false">🛡 Администрирование</button>
          </div>
        </div>
      </div>

      <!-- Выбор фона чата -->
      <div v-if="bgPicker" class="bg-picker">
        <div class="bg-row">
          <button v-for="b in bgPresets" :key="b.name" class="bg-swatch" :style="b.value ? {background:b.value} : {}"
                  @click="setBackground(b.value)" :title="b.name" :aria-label="b.name">{{ b.value ? '' : '∅' }}</button>
          <label class="btn" style="margin:0;cursor:pointer">Загрузить фото
            <input type="file" accept="image/*" class="file-input" @change="uploadBackground"
                   aria-label="Загрузить фотографию как фон чата" />
          </label>
        </div>
        <div v-if="chatImages.length" class="bg-row">
          <span class="muted" style="align-self:center">Из чата:</span>
          <img v-for="(u,i) in chatImages" :key="i" :src="u" class="bg-thumb" @click="setBackground(u)" :alt="'Кадр из чата ' + (i + 1)" />
        </div>
      </div>

      <!-- Вежливый регион: скринридер узнаёт, что ход начался, закончился или упал.
           Раньше незрячий пользователь отправлял сообщение и не получал НИКАКОГО
           сигнала о том, что вообще происходит. -->
      <div class="sr-only" role="status" aria-live="polite">{{ liveStatus }}</div>

      <div class="messages" ref="messages" :style="chatBgStyle" @scroll="onMessagesScroll"
           role="log" aria-label="Переписка">
        <!-- Первый экран. Раньше здесь была одна бледная строка «выберите слева»,
             которая на телефоне указывала туда, где ничего нет: сайдбар за ☰. -->
        <div v-if="!sessionId" class="start">
          <h2 class="start-title">С чего начнём?</h2>
          <p class="start-lead">Диалог с персонажем, разговор нескольких персонажей сразу
            или помощник без отыгрыша — всё это один и тот же чат.</p>
          <div class="start-actions">
            <button class="btn-primary" @click="startNewChat">Новый диалог</button>
            <button @click="createCharacter">Создать персонажа</button>
            <button v-if="characters.length > 1" @click="openGroupModal">Собрать группу</button>
          </div>
          <div class="start-recent" v-if="recentChats.length">
            <span class="start-recent-head">Недавние диалоги</span>
            <!-- Теперь это голова единого списка: здесь бывают и группы, и общие
                 чаты. Ключ тот же, что у строки сайдбара (вид + id), а открываем
                 через openChatRow — общий чат открывается иначе, чем свой. -->
            <button v-for="s in recentChats" :key="'rc' + s.kind + s.id" class="start-chat"
                    @click="openChatRow(s)" :aria-label="'Открыть чат ' + s.title">
              <span class="grow">{{ s.title }}</span>
              <span class="when" v-if="s.last_at">{{ shortWhen(s.last_at) }}</span>
            </button>
          </div>
          <p class="start-hint only-mobile">Персонажи и все чаты — в меню ☰ вверху.</p>
        </div>
        <!-- Индикатор подгрузки истории при скролле вверх -->
        <div v-if="sessionId && loadingOlder" class="load-older">⏳ Загружаю ранние сообщения…</div>
        <div v-else-if="sessionId && messages.length >= msgPageSize && noMoreMessages" class="load-older muted">— начало чата —</div>

        <div v-for="m in messages" :key="m.id" :class="['msg', m.role]" :data-mid="m.id">
          <!-- Аватарка: персонаж/участник группы у ответа, персона/профиль у пользователя -->
          <div v-if="m.role === 'user' || m.role === 'assistant'" class="msg-ava"
               :title="m.role === 'assistant' ? (m.speaker_name || (selectedCharacter && selectedCharacter.name) || '') : ''">
            <img v-if="msgAvatar(m)" :src="msgAvatar(m)" loading="lazy" alt="" />
            <span v-else>{{ msgAvatarLetter(m) }}</span>
          </div>
          <div class="msg-body">
          <div v-if="m.speaker_name" class="speaker">{{ m.speaker_name }}</div>
          <!-- В режиме сцены реплику подписывает КАЖДЫЙ говорящий, а не только
               участник группы: это театральная ремарка, по которой читают, кто
               сейчас на сцене. В обычном режиме подпись осталась как была. -->
          <div v-else-if="viewMode === 'scene' && (m.role === 'user' || m.role === 'assistant')"
               class="speaker">{{ m.role === 'user' ? 'Вы' : (selectedCharacter ? selectedCharacter.name : 'Персонаж') }}</div>
          <div v-if="m.reply_to_id" class="reply-quote">↪ {{ quoteOf(m.reply_to_id) }}</div>
          <div class="bubble">
            <!-- режим редактирования: авто-фокус, авто-высота, Ctrl+Enter / Esc -->
            <div v-if="editingId === m.id" class="edit-box">
              <!-- Поле правки не имело имени вообще: ни label, ни placeholder,
                   ни aria-label — вслух это был просто «редактируемый текст», и
                   в ленте на сорок сообщений было не понять, какое правишь. -->
              <textarea ref="editArea" v-model="editingText" class="edit-area"
                        aria-label="Правка сообщения"
                        @input="autoGrowEdit" @keydown="onEditKeydown"></textarea>
              <div class="row edit-actions">
                <span class="muted edit-hint">Ctrl+Enter — сохранить · Esc — отмена</span>
                <div style="flex:1"></div>
                <button @click="editingId = null">Отмена</button>
                <button class="btn-primary" @click="saveEdit">Сохранить</button>
              </div>
            </div>
            <!-- ответ ИИ + плашка документа (ответ-Канвас) -->
            <div v-else-if="m.canvas_id">
              <div v-if="m.content" v-html="renderMd(horaeStrip(m.content))" style="margin-bottom:8px"></div>
              <!-- Карточка документа была обычным div с @click: с клавиатуры на
                   неё нельзя ни встать, ни нажать — ответ-Канвас открывался
                   только мышью. Настоящей кнопкой её не делаем: пришлось бы
                   гасить кнопочный скин и просадку scale(0.94) на всю карточку,
                   поэтому роль и клавиши добавлены атрибутами. -->
              <div class="doc-card" role="button" tabindex="0"
                   @click="openCanvas(m.canvas_id)"
                   @keydown.enter="openCanvas(m.canvas_id)"
                   @keydown.space.prevent="openCanvas(m.canvas_id)"
                   title="Открыть в Canvas">
                <span class="doc-card-icon">{{ m.canvas_kind === 'code' ? '💻' : '📄' }}</span>
                <span class="doc-card-body">
                  <span class="doc-card-title">{{ m.canvas_title || 'Документ' }}</span>
                  <span class="doc-card-hint">Открыть в Canvas →</span>
                </span>
              </div>
            </div>
            <!-- обычный режим: markdown; двойной клик — быстрое редактирование -->
            <div v-else v-html="renderMd(horaeStrip(m.content))" @dblclick="startEdit(m)"></div>
            <!-- предпросмотр вложений сообщения -->
            <div v-if="m.attachments && m.attachments.length" class="attachments">
              <template v-for="(a, ai) in m.attachments" :key="ai">
                <!-- a.data есть только у своего свежеотправленного (оптимистичного) сообщения;
                     у загруженных из БД — тянем лениво по attUrl (кэшируется браузером). -->
                <div class="att-item">
                  <!-- У вложения alt содержательный, а не пустой: картинка в
                       переписке — это содержимое реплики, и с пустым alt она
                       пропала бы из ответа целиком. -->
                  <img v-if="a.type==='image'" :src="a.data || a.preview || attUrl(m, ai)" loading="lazy" class="att-img" @click="lightbox = a.data || a.preview || attUrl(m, ai)" :alt="a.name ? 'Изображение ' + a.name : 'Изображение в сообщении'" title="Открыть" />
                  <audio v-else-if="a.type==='audio'" :src="a.data || a.preview || attUrl(m, ai)" controls preload="none" class="att-audio"></audio>
                  <video v-else-if="a.type==='video' || ((a.mime || '').startsWith('video'))" :src="a.data || a.preview || attUrl(m, ai)" controls preload="metadata" class="att-video"></video>
                  <a v-else class="att-doc" :href="a.data || a.preview || attUrl(m, ai)" :download="a.name || 'файл'" title="Скачать">{{ attIcon(a) }} {{ a.name || 'документ' }}</a>
                  <!-- Подпись с ОРИГИНАЛЬНЫМ именем файла + размер + скачать (для всех типов) -->
                  <div v-if="a.type!=='document'" class="att-caption">
                    <span class="att-fname" :title="a.name || ''">{{ attIcon(a) }} {{ a.name || 'файл' }}</span>
                    <i v-if="a.size"> · {{ fmtSize(a.size) }}</i>
                    <a class="att-dl" :href="a.data || a.preview || attUrl(m, ai)" :download="a.name || 'файл'" :title="'Скачать «' + (a.name || 'файл') + '»'">⬇ скачать</a>
                  </div>
                </div>
              </template>
            </div>
          </div>

          <!-- Строка Horae: сводка данных ответа, по нажатию — редактор меты.
               Только у настоящих ответов ИИ: у оптимистичной «tmp» и плашки
               Канваса данных нет и быть не может. -->
          <horae-msg v-if="m.role === 'assistant' && !m.canvas_id && typeof m.id === 'number'"
                     :message="m" :session-id="sessionId" @changed="onHoraeMsgChanged"></horae-msg>

          <div class="msg-meta">
            <!-- Две группы: сведения (варианты, время, модель) и действия. На
                 узком экране они встают в две строки: сведения сверху, действия
                 ниже одной лентой с горизонтальной прокруткой. Раньше всё жило в
                 одном flex с переносом и на телефоне рассыпалось в 3-4 неровные
                 строки, где кнопки переезжали между рядами от длины имени модели. -->
            <span class="msg-info">
              <!-- свайпы только у ассистента и если их больше одного / это последний ответ -->
              <span v-if="m.role === 'assistant' && !m.canvas_id" class="swipes">
                <button class="btn-icon" @click="swipe(m, -1)" :disabled="m.active_swipe === 0"
                        aria-label="Предыдущий вариант ответа">◀</button>
                {{ m.active_swipe + 1 }}/{{ (m.swipes || [m.content]).length }}
                <button class="btn-icon" @click="swipe(m, 1)" :title="m.id === lastAssistantId ? 'Ещё вариант' : ''"
                        :aria-label="m.id === lastAssistantId ? 'Сгенерировать ещё вариант ответа' : 'Следующий вариант ответа'">▶</button>
              </span>
              <!-- Время: у user — когда отправил, у assistant — когда пришёл ответ (в поясе чата) -->
              <span v-if="m.created_at" class="tag msg-time" :title="fmtWhenFull(m.created_at)">🕒 {{ fmtWhen(m.created_at) }}</span>
              <span v-if="m.model_used" class="tag msg-model" :title="m.model_used">{{ m.model_used }}</span>
            </span>
            <span class="msg-actions" role="group" aria-label="Действия с сообщением"
                  @scroll.passive="msgStripEdge($event.currentTarget)">
              <!-- Четыре главных действия стоят всегда и всегда в одном порядке:
                   копировать, править, перегенерировать, удалить. Вторичные
                   (ответить, к началу, продолжить, арт, ветка) на пальце и в узком
                   окне уходят за «⋯» — одиннадцать кнопок по 44px занимали под
                   КАЖДОЙ репликой больше места, чем короткий ответ, и на телефоне
                   рассыпались в 3-4 неровных ряда. На широком экране с мышью
                   видно всё сразу: там строку и так проявляет наведение.
                   Порядок в обоих режимах общий, меняется только видимость:
                   иначе привычная кнопка переезжала бы при сужении окна.

                   Удалить и «⋯» стоят ПЕРЕД вторичными, а не после них. Раньше
                   раскрытые вторичные вставали перед ними, и на телефоне (лента
                   ~300px) раскрытие выталкивало за край и «Удалить», и сам «✕»,
                   по которому только что нажали. Теперь главные и переключатель
                   не двигаются никогда, а за край уходят только вторичные. -->
              <button v-if="!m.canvas_id" class="btn-icon" @click="copyMessage(m)" title="Скопировать текст" aria-label="Скопировать текст">📋</button>
              <button v-if="!m.canvas_id" class="btn-icon" @click="startEdit(m)" title="Редактировать" aria-label="Редактировать">✎</button>
              <button v-if="!m.canvas_id && m.role === 'assistant' && m.id === lastAssistantId" class="btn-icon"
                      @click="regenerate" title="Перегенерировать" aria-label="Перегенерировать">↻</button>
              <button class="btn-icon" @click="deleteMessage(m)" title="Удалить" aria-label="Удалить">🗑</button>
              <!-- Одна кнопка-переключатель, а не пара «⋯»/«✕» с разными
                   элементами: у пары aria-expanded навсегда застывал в «false», и
                   о раскрытом состоянии скринридер не узнавал ничем. Кнопки нет,
                   когда прятать нечего (плашка документа без длинного текста). -->
              <button v-if="compactActions && (!m.canvas_id || (m.content || '').length > 800)"
                      class="btn-icon msg-more" :class="msgMenu === m.id ? 'on' : ''"
                      @click="toggleMsgMenu(m, $event)"
                      :aria-expanded="msgMenu === m.id ? 'true' : 'false'"
                      :title="msgMenu === m.id ? 'Свернуть действия' : 'Ещё действия'"
                      :aria-label="msgMenu === m.id ? 'Свернуть действия' : 'Ещё действия с сообщением'">{{ msgMenu === m.id ? '✕' : '⋯' }}</button>
              <!-- Вторичные действия. Раскрытое меню закрывается тем же нажатием,
                   что выполняет действие: открытым оно оставалось бы висеть под
                   репликой, к которой человек уже не вернётся. -->
              <template v-if="!compactActions || msgMenu === m.id">
                <button v-if="!m.canvas_id" class="btn-icon" @click="msgMenu = null; replyTo(m)" title="Ответить на это сообщение" aria-label="Ответить на это сообщение">↩</button>
                <!-- У длинного ответа верх уезжает за экран, и вернуться к нему
                     прокруткой на глаз неудобно — даём точный прыжок. -->
                <button v-if="(m.content || '').length > 800" class="btn-icon"
                        @click="msgMenu = null; scrollMessageStart(m.id)" title="К началу этого сообщения"
                        aria-label="К началу этого сообщения">⇞</button>
                <template v-if="!m.canvas_id">
                  <button v-if="m.role === 'assistant' && m.id === lastAssistantId" class="btn-icon" @click="msgMenu = null; continueReply()" title="Продолжить" aria-label="Продолжить">⏩</button>
                  <button class="btn-icon" @click="msgMenu = null; artFromMessage(m)" title="Нарисовать по этому сообщению" aria-label="Нарисовать по этому сообщению">🎨</button>
                  <!-- Ветка от реплики: развилка сюжета перестаёт эмулироваться
                       откруткой свайпа в середине чата, после которой вся дальнейшая
                       переписка отвечала на вариант, которого уже не видно. -->
                  <button class="btn-icon" @click="msgMenu = null; forkFrom(m)"
                          title="Форкнуть отсюда: новый чат с историей до этой реплики"
                          aria-label="Форкнуть отсюда: новый чат с историей до этой реплики">⑂</button>
                </template>
              </template>
            </span>
          </div>
          </div><!-- /.msg-body -->
        </div>

        <!-- live-размышления модели (thinking): свёрнуты, в ответ не входят -->
        <div v-if="streaming && currentThought" class="msg assistant thought-msg">
          <details class="thought-box">
            <summary>💭 Модель размышляет… <i>{{ (currentThought.length / 1000).toFixed(1) }}к симв.</i></summary>
            <div class="thought-text">{{ currentThought }}</div>
          </details>
        </div>
        <!-- стриминг: группа (несколько персонажей по очереди) -->
        <div v-for="(b, i) in liveBubbles" :key="'live'+i" class="msg assistant">
          <div class="msg-ava">
            <img v-if="memberAvatar(b.name)" :src="memberAvatar(b.name)" alt="" />
            <span v-else>{{ (b.name || '?').charAt(0).toUpperCase() }}</span>
          </div>
          <div class="msg-body">
            <div class="speaker">{{ b.name }}</div>
            <div class="bubble"><div v-html="renderMd(horaeStrip(b.content, true))"></div><span class="typing">▌</span>
              <div v-if="horaeWriting(b.content)" class="horae-writing">🕰 Horae записывает…</div></div>
          </div>
        </div>
        <!-- пауза перед ответом следующего персонажа (защита от лимита провайдера) -->
        <div v-if="streaming && groupWaiting > 0" class="msg assistant">
          <div class="bubble muted">⏳ Пауза {{ groupWaiting }} с перед следующим персонажем (бережём лимит запросов)…</div>
        </div>
        <!-- стриминг: одиночный ответ -->
        <div v-if="streaming && !liveBubbles.length" class="msg assistant">
          <div class="msg-ava">
            <img v-if="msgAvatar({ role: 'assistant' })" :src="msgAvatar({ role: 'assistant' })" alt="" />
            <span v-else>{{ msgAvatarLetter({ role: 'assistant' }) }}</span>
          </div>
          <div class="msg-body">
            <!-- Служебные теги Horae сервер вырезает при сохранении ответа, а
                 в стриме они идут сырыми: их прячем здесь, незакрытый хвост
                 тоже, и вместо «<horae>time:…» показываем тихую пометку. -->
            <div class="bubble">
              <div v-html="renderMd(horaeStrip(currentReply, true))"></div>
              <span class="typing">▌</span>
              <div v-if="horaeWriting(currentReply)" class="horae-writing">🕰 Horae записывает…</div>
            </div>
          </div>
        </div>
        <!-- генерация документа в Canvas (нестриминговая) -->
        <div v-if="canvasGenerating" class="msg assistant">
          <div class="bubble">📄 Генерирую документ для Canvas… <span class="typing">▌</span></div>
        </div>
      </div>

      <!-- Навигация по чату: появляется, когда отъехали от низа. У самого низа
           (обычное чтение свежих реплик) она была бы лишним элементом. -->
      <div class="chat-nav" v-if="sessionId && messages.length > 3 && awayFromBottom"
           :style="{ bottom: (composerH + 12) + 'px' }">
        <button class="btn-icon" @click="scrollToChatStart" :disabled="jumpBusy"
                title="К началу чата (Home). Догрузит раннюю историю, если её ещё нет" aria-label="К началу чата (Home). Догрузит раннюю историю, если её ещё нет">
          {{ jumpBusy ? '⏳' : '⤒' }}</button>
        <button class="btn-icon" @click="jumpMessage(-1)" title="Предыдущее сообщение (Alt+↑)" aria-label="Предыдущее сообщение (Alt+↑)">⌃</button>
        <button class="btn-icon" @click="jumpMessage(1)" title="Следующее сообщение (Alt+↓)" aria-label="Следующее сообщение (Alt+↓)">⌄</button>
        <button class="btn-icon accent" @click="scrollToBottom()" title="К последнему сообщению (End)" aria-label="К последнему сообщению (End)">⤓</button>
      </div>

      <!-- Композер — отдельный ориентир: это единственное место, куда пишут, и
           возвращаться в него из ленты на сорок сообщений надо одним движением,
           а не полусотней нажатий Tab. -->
      <div class="composer" v-if="sessionId" role="region" aria-label="Написать сообщение">
        <!-- Все полосы над полем ввода — в одной прокручиваемой обёртке с потолком
             по высоте экрана. Каждая полоса по отдельности невелика, но в худшем
             случае они встают разом: режим ассистента, режиссёр, ошибка, ответ на
             реплику, десяток вложений, загрузка файлов — и вместе выталкивали
             поле ввода с «Отправить» за нижний край телефона. Теперь сжимается
             и прокручивается только эта стопка, а строка ввода всегда на виду. -->
        <div class="composer-bars">
        <!-- Режим ассистента включён — показываем явно: иначе легко забыть, что
             персонаж сейчас не отыгрывает, и удивиться «сухому» ответу. -->
        <div v-if="params.assistant_mode" class="director-bar">
          <span class="dir-hint">🎓 <b>Режим ассистента</b> — персонаж не отыгрывает, а выполняет задачу.
            <a href="#" @click.prevent="toggleAssistantMode">вернуть отыгрыш</a></span>
        </div>
        <!-- Режиссёрская панель (группа): кнопки вызвать/исключить персонажей -->
        <div v-if="currentIsGroup && directorBar" class="director-bar">
          <span class="dir-hint">🎬 Режиссёр: клик по имени — вызвать (порядок кликов = порядок ответов), «−» — исключить. Можно писать вручную: <code>+Хорхе −Джеми</code></span>
          <div class="dir-chips">
            <span v-for="m in currentGroup.members" :key="'d'+m.id" class="dir-chip">
              <button class="dir-add" @click="dirInsert('+', m.name)" :title="'Вызвать ' + m.name" :aria-label="'Вызвать ' + m.name">+ {{ m.name }}</button>
              <button class="dir-ex" @click="dirInsert('-', m.name)" :title="'Исключить ' + m.name" :aria-label="'Исключить ' + m.name">−</button>
            </span>
          </div>
        </div>
        <!-- Баннер ошибки генерации: НЕ прячем, чтобы было видно причину -->
        <div v-if="chatError" class="error-banner">
          Ошибка генерации: {{ chatError }}
          <a v-if="!streaming" href="#" @click.prevent="retryGeneration">↻ повторить</a>
          <a v-if="!streaming && fallbackModel" href="#" @click.prevent="retryGeneration(true)"
             :title="'Повторить ход запасной моделью ' + fallbackModel">⚡ запасной моделью</a>
          <a href="#" @click.prevent="chatError=''">скрыть</a>
        </div>
        <!-- Ответ не пришёл (ошибка/обрыв/остановка) — предлагаем повторить ход -->
        <div v-else-if="canRetry" class="art-indicator retry-bar">
          ⚠ Ответ на последнее сообщение не получен.
          <a href="#" @click.prevent="retryGeneration">↻ Повторить генерацию</a>
          <a v-if="fallbackModel" href="#" @click.prevent="retryGeneration(true)"
             :title="'Повторить ход запасной моделью ' + fallbackModel">⚡ Запасной моделью</a>
        </div>
        <!-- Индикатор «отвечаю на сообщение» -->
        <div v-if="replyToMsg" class="reply-bar">
          ↪ Ответ на: {{ (replyToMsg.content || '').slice(0, 90) }}
          <a href="#" @click.prevent="cancelReply">✕</a>
        </div>
        <!-- Плашек режима здесь больше нет: режим виден на кнопке отправки и в
             плейсхолдере, а Esc возвращает обычное сообщение. Раньше три режима
             давали три одинаковые синие полосы над полем ввода, и на телефоне
             при нескольких полосах на саму переписку оставалось полторы строки. -->
        <!-- Лента вложений ограничена по высоте и прокручивается сама. Раньше
             десять картинок растягивали её на весь экран: переписку не было
             видно, а поле ввода и «Отправить» уезжали под навигацию телефона. -->
        <div class="chips att-strip" v-if="pendingAttachments.length"
             role="list" :aria-label="'Вложения: ' + pendingAttachments.length">
          <span class="chip att-chip" :class="{ 'att-loading': a.loading, 'att-error': a.error }"
                v-for="(a, i) in pendingAttachments" :key="a.id" role="listitem">
            <span v-if="a.loading" class="att-state">⏳</span>
            <span v-else-if="a.error" class="att-state">⚠</span>
            <template v-else>
              <img v-if="a.type==='image'" :src="a.data || a.preview" class="att-thumb" @click="lightbox=a.data || a.preview" :alt="a.name ? 'Вложение ' + a.name : 'Прикреплённое изображение'" title="Открыть" />
              <audio v-else-if="a.type==='audio'" :src="a.data" controls class="att-audio-sm"></audio>
              <span v-else-if="a.type==='video'" class="att-state">🎬</span>
              <span v-else class="att-state">📄</span>
            </template>
            <span class="att-name" :title="a.name || ''">
              {{ a.error ? 'ошибка' : (a.name || (a.type==='audio' ? 'аудио' : 'файл')) }}<i v-if="a.size"> · {{ fmtSize(a.size) }}</i>
            </span>
            <a href="#" class="att-x" @click.prevent="removeAttachment(i)" title="Убрать" :aria-label="'Убрать вложение ' + (a.name || '')">✕</a>
          </span>
        </div>
        <!-- Пока файлы читаются — предупреждаем, что отправка подождёт их -->
        <div v-if="attachmentsLoading || waitingFiles" class="art-indicator files-bar">
          ⏳ Загрузка вложений… {{ waitingFiles ? 'отправлю, как только дочитаются.' : 'дождитесь готовности перед отправкой.' }}
        </div>
        <!-- Прогресс отправки файлов на сервер (XHR upload.onprogress) -->
        <div v-if="uploadProgress" class="art-indicator files-bar upload-bar">
          ⬆ Отправка на сервер…
          {{ uploadProgress.percent != null ? uploadProgress.percent + '%' : '…' }}
          <i v-if="uploadProgress.total"> ({{ fmtSize(uploadProgress.loaded) }} из {{ fmtSize(uploadProgress.total) }})</i>
          <span class="upload-track"><span class="upload-fill" :style="{ width: (uploadProgress.percent || 0) + '%' }"></span></span>
        </div>
        <!-- Файл уже на сервере — идёт передача нейросети и обработка (до первого токена) -->
        <div v-else-if="processingNote && streaming && !currentReply && !currentThought && !liveBubbles.length"
             class="art-indicator files-bar">
          📡 Файл загружен на сервер — нейросеть получает и обрабатывает его… Большие файлы обрабатываются до нескольких минут.
        </div>
        </div><!-- /.composer-bars -->
        <div class="row">
          <!-- [+] второстепенные действия: документ, арт -->
          <div class="plus-wrap">
            <!-- Выбранный режим составителя — обычное «включено», а не тревога:
                 красным здесь помечалось нормальное состояние, и цвет аварии
                 обесценивался. aria-pressed сюда не годится: нажатие открывает
                 меню, а не включает режим, — состояние меню несёт aria-expanded,
                 сам режим виден на кнопке отправки и в плейсхолдере. -->
            <button class="btn-icon" :class="composerMode !== 'text' ? 'on' : ''"
                    :aria-expanded="plusMenu ? 'true' : 'false'"
                    @click="plusMenu=!plusMenu" title="Ещё: документ, арт" aria-label="Ещё: документ, арт">➕</button>
            <div v-if="plusMenu" class="plus-backdrop" @click="plusMenu=false"></div>
            <div v-if="plusMenu" class="plus-menu">
              <button @click="composerMode='canvasGen'; plusMenu=false">📄 Создать документ/код (Canvas)</button>
              <button @click="generateArt('prompt'); plusMenu=false">🖼 Сгенерировать фото (арт)</button>
              <button @click="generateArt('scene'); plusMenu=false">🎬 Арт по последней сцене</button>
              <button @click="generateArt('overview'); plusMenu=false">🌅 Арт по общей картине</button>
            </div>
          </div>
          <label class="btn-icon" style="margin:0; cursor:pointer" title="Прикрепить файл: фото, аудио, видео или документ (Word/PDF/текст)">
            📎<input type="file" multiple accept="image/*,audio/*,video/*,.pdf,.doc,.docx,.odt,.rtf,.txt,.md,.csv"
                   class="file-input" @change="onAttach"
                   aria-label="Прикрепить файл к сообщению: фото, аудио, видео или документ" />
          </label>
          <!-- Голос — отдельной кнопкой: запись/стоп в один клик -->
          <button class="btn-icon" :class="recording ? 'rec-active' : ''" @click="toggleRecord"
                  :title="recording ? 'Остановить запись' : 'Записать голос'" :aria-label="recording ? 'Остановить запись' : 'Записать голос'">{{ recording ? '⏺ стоп' : '🎤' }}</button>
          <!-- Режиссёр (только в группе): панель кнопок «вызвать/исключить».
               Открытая панель — включённый тумблер, а не авария: красная заливка
               уравнивала её с идущей записью голоса. Кнопка раскрывает панель,
               поэтому состояние несёт aria-expanded: подпись у открытого и
               закрытого состояний одна и та же, и раньше о нём говорил только цвет. -->
          <button v-if="currentIsGroup" class="btn-icon" :class="directorBar ? 'on' : ''"
                  :aria-expanded="directorBar ? 'true' : 'false'"
                  @click="directorBar = !directorBar" title="Режиссёр: кто отвечает и в каком порядке" aria-label="Режиссёр: кто отвечает и в каком порядке">🎬</button>
          <!-- Режим ассистента: держим у поля ввода, а не только в настройках —
               переключать его нужно ровно тогда, когда пишешь прикладную просьбу.
               Состояние — заливкой выбора: красный обещал сбой, которого нет.
               aria-pressed нужен ради ВЫКЛЮЧЕННОГО состояния: там подпись
               описывает, что кнопка сделает, а не то, что режим снят. -->
          <button class="btn-icon" :class="params.assistant_mode ? 'on' : ''"
                  :aria-pressed="params.assistant_mode ? 'true' : 'false'"
                  @click="toggleAssistantMode"
                  :title="params.assistant_mode
                    ? 'Режим ассистента ВКЛЮЧЁН: персонаж не отыгрывает, а выполняет задачу. Нажмите, чтобы вернуть отыгрыш'
                    : 'Режим ассистента: выполнять задачи без отыгрыша. Для одного сообщения можно просто написать ((текст))'" :aria-label="params.assistant_mode
                    ? 'Режим ассистента ВКЛЮЧЁН: персонаж не отыгрывает, а выполняет задачу. Нажмите, чтобы вернуть отыгрыш'
                    : 'Режим ассистента: выполнять задачи без отыгрыша. Для одного сообщения можно просто написать ((текст))'">🎓</button>
          <!-- Имя главному полю приложения давал только placeholder, а он не
               имя: часть скринридеров его не читает, и поле оставалось безымянным.
               Берём тот же текст — так подпись вслух совпадает с видимой, и
               режим композера (Канвас, арт, группа) слышен наравне с видимым. -->
          <textarea ref="composer" v-model="input" rows="1" class="composer-input"
                    :aria-label="composerPlaceholder"
                    :placeholder="composerPlaceholder"
                    @input="autoGrow" @keydown="onComposerKeydown" @paste="onPaste"></textarea>
          <button v-if="streaming" class="btn-danger" @click="stop">■ Стоп</button>
          <!-- ОДНА кнопка на все режимы. Раньше их было четыре в цепочке
               v-else-if, и порядок цепочки решал, что произойдёт: надпись могла
               обещать одно, а нажатие делало другое. Теперь и надпись, и
               действие читают одно и то же поле composerMode. -->
          <button v-else class="btn-primary" @click="submitComposer"
                  :disabled="!connected || waitingFiles || (composerMode === 'canvasCmd' && canvasBusy) || (composerMode === 'canvasGen' && canvasGenerating)">{{ submitLabel() }}</button>
        </div>

        <!-- ПРИБОРЫ. Раньше пользователь платил за вход на каждом ходу и не видел
             стоимости следующего, а о переполнении окна узнавал постфактум, когда
             персонаж «забыл» события: история молча обрезалась на сервере.
             На узком экране полоса схлопывается в строку-бейдж (см. CSS), чтобы
             не отнимать высоту у переписки. -->
        <div class="telemetry" v-if="sessionId && ctxStats">
          <button class="telemetry-main" @click="openInspector"
                  :title="'Окно контекста: ' + ctxStats.total_tokens + ' из ' + ctxStats.budget + ' токенов. Открыть инспектор хода'"
                  :aria-label="'Окно контекста заполнено на ' + ctxFill + ' процентов. Открыть инспектор хода'">
            <span class="tele-gauge" :class="ctxLevel" aria-hidden="true">
              <i :style="{ width: ctxFill + '%' }"></i>
            </span>
            <span class="tele-num">{{ fmtCtx }} / {{ Math.round(ctxStats.budget / 1000) }}к</span>
            <span class="tele-sub hide-narrow">{{ ctxFill }}% окна</span>
            <!-- Знак доллара собираем ВНУТРИ выражения. Шаблон живёт в JS-литерале
                 с обратными кавычками, и последовательность «$» плюс «{» начала бы
                 подстановку самого JavaScript ещё до того, как строку увидит Vue. -->
            <span v-if="ctxCost" class="tele-sub">{{ '≈ $' + ctxCost }}</span>
            <span v-if="ctxStats.history && ctxStats.history.trimmed" class="tele-sub tele-warn">
              обрезано {{ ctxStats.history.trimmed }}</span>
          </button>
          <!-- Скорость и задержка: экран перестаёт выглядеть одинаково на второй
               секунде и на девяностой. Знак «≈» не декоративный: браузеру
               токенизатор провайдера недоступен, считаем по приросту символов. -->
          <span v-if="streaming" class="tele-live" aria-hidden="true">
            <span v-if="!genFirstAt">думает {{ genElapsed.toFixed(1) }}с</span>
            <template v-else>
              <span>{{ genElapsed.toFixed(1) }}с</span>
              <span v-if="genTps">· ≈{{ genTps }} т/с</span>
              <span v-if="ttft()">· первый токен {{ ttft().toFixed(1) }}с</span>
            </template>
          </span>
        </div>
      </div>
    </div>

    <!-- ===== Канвас: документ/код рядом с чатом (side-by-side, как в Gemini) ===== -->
    <div class="canvas-pane" v-if="canvasOpen && canvas">
      <div class="canvas-head">
        <!-- На узком экране Канвас занимает весь экран, поэтому возврат должен быть
             явной подписанной кнопкой, а не иконкой: иконка «💬» рядом с полем
             названия читалась как «обсудить документ», а не «выйти отсюда». -->
        <button class="btn canvas-back only-mobile" @click="mobilePane='chat'"
                aria-label="Назад к чату">← К чату</button>
        <input v-model="canvas.title" class="canvas-title" @blur="saveCanvas" placeholder="Без названия" />
        <select v-model="canvas.kind" @change="saveCanvas" class="canvas-kind" title="Тип канваса">
          <option value="document">📄 документ</option>
          <option value="code">💻 код</option>
        </select>
        <button class="btn-icon" @click="undoCanvas" :disabled="!canvas.can_undo || canvasBusy" title="Откатить к предыдущей версии" aria-label="Откатить к предыдущей версии">↩</button>
        <button class="btn-icon" @click="exportCanvas('docx')" title="Экспорт в Word" aria-label="Экспорт в Word">📄</button>
        <button class="btn-icon" @click="exportCanvas('pdf')" title="Экспорт в PDF" aria-label="Экспорт в PDF">📑</button>
        <button class="btn-icon" @click="closeCanvas" title="Закрыть канвас" aria-label="Закрыть канвас">✕</button>
      </div>

      <!-- Единый тулбар: слева — контекстные действия (форматирование / копировать код),
           справа — переключатель «Редактор / Просмотр» (для веб-кода — live-результат). -->
      <!-- Переключатель «Редактор / Просмотр» — тоже вкладки, и тоже без ролей:
           о включённом режиме говорил один класс active, то есть исключительно
           цвет. Слева в той же полосе живут кнопки форматирования; вкладками
           считаются только те два элемента, у которых стоит role="tab". -->
      <div class="canvas-tabs" role="tablist" aria-label="Режим канваса">
        <template v-if="canvas.kind==='document' && canvasView==='edit'">
          <button class="canvas-fmt" @click="wrapSelection('**','**')" title="Жирный" aria-label="Жирный"><b>B</b></button>
          <button class="canvas-fmt" @click="wrapSelection('*','*')" title="Курсив" aria-label="Курсив"><i>I</i></button>
          <button class="canvas-fmt" @click="wrapSelection('## ','')" title="Заголовок" aria-label="Заголовок">H</button>
          <button class="canvas-fmt" @click="wrapSelection('\`','\`')" title="Моноширинный" aria-label="Моноширинный">&lt;/&gt;</button>
        </template>
        <button v-if="canvas.kind==='code'" class="canvas-fmt" @click="copyCanvas" :title="copied ? 'Скопировано' : 'Скопировать код'" :aria-label="copied ? 'Скопировано' : 'Скопировать код'">{{ copied ? '✓ Скопировано' : '⧉ Скопировать код' }}</button>
        <div style="flex:1"></div>
        <button id="canvas-tab-edit" role="tab" aria-controls="canvas-body"
                :aria-selected="canvasView==='edit' ? 'true' : 'false'"
                :class="{ active: canvasView==='edit' }" @click="canvasView='edit'">✎ {{ canvas.kind==='code' ? 'Код' : 'Редактор' }}</button>
        <button id="canvas-tab-preview" role="tab" aria-controls="canvas-body"
                :aria-selected="canvasView==='preview' ? 'true' : 'false'"
                :class="{ active: canvasView==='preview' }" @click="canvasView='preview'">{{ canvasIsWeb ? '▶ Превью' : '👁 Просмотр' }}</button>
      </div>

      <div class="canvas-body" id="canvas-body" role="tabpanel"
           :aria-labelledby="canvasView==='edit' ? 'canvas-tab-edit' : 'canvas-tab-preview'">
        <!-- Редактор (правят и ИИ, и пользователь) -->
        <textarea v-show="canvasView==='edit'" ref="canvasEditor" v-model="canvas.content"
                  :class="['canvas-editor', canvas.kind==='code' ? 'mono' : '']"
                  @blur="saveCanvas" @mousedown="hideToolbar" @mouseup="onEditorPointerUp"
                  @touchend="onEditorPointerUp" @keyup="onEditorKeyUp" @scroll="hideToolbar"
                  placeholder="Содержимое канваса…"></textarea>
        <!-- Предпросмотр: веб-код -> live в iframe; документ -> рендер; прочий код -> как есть -->
        <iframe v-if="canvasView==='preview' && canvasIsWeb" class="canvas-preview-frame"
                :srcdoc="previewSrcdoc" sandbox="allow-scripts allow-modals allow-forms allow-popups"></iframe>
        <div v-else-if="canvasView==='preview' && canvas.kind==='document'" class="canvas-preview bubble" v-html="renderMd(canvas.content)"></div>
        <pre v-else-if="canvasView==='preview'" class="canvas-preview mono">{{ canvas.content }}</pre>
        <div v-if="canvasBusy" class="canvas-overlay">✨ ИИ дорабатывает…</div>
      </div>

      <div v-if="canvasSelText" class="canvas-selinfo">
        Выделено {{ canvasSelText.length }} симв. — действия и команды применятся только к ним.
        <a href="#" @click.prevent="clearSel">снять</a>
      </div>
    </div>

    <!-- ===== Правый drawer: настройки (выезжающий оверлей) ===== -->
    <div v-if="drawerTab" class="drawer-backdrop" @click="drawerTab=null"></div>
    <!-- Хронике нужна ширина: таблицы, карточки персонажей и редактор
         правил в 380px не помещаются. На узком экране ящик и так во всю ширину. -->
    <div class="drawer" :class="{ 'drawer-wide': drawerTab === 'memory' }" v-if="drawerTab" role="dialog" aria-modal="true" aria-label="Настройки">
      <!-- Полоса вкладок объявлена вкладками. Ролей tab/tablist в файле не было
           вообще: скринридер читал пять обычных кнопок и не сообщал ни какая из
           них открыта, ни сколько их всего — состояние несла только заливка. -->
      <div class="tabs" role="tablist" aria-label="Разделы настроек">
        <button id="drawer-tab-generation" role="tab" aria-controls="drawer-panel-generation"
                :aria-selected="drawerTab==='generation' ? 'true' : 'false'"
                :class="['tab-btn', drawerTab==='generation'?'active':'']" @click="drawerTab='generation'">Генерация</button>
        <button v-if="isAdmin" id="drawer-tab-connection" role="tab" aria-controls="drawer-panel-connection"
                :aria-selected="drawerTab==='connection' ? 'true' : 'false'"
                :class="['tab-btn', drawerTab==='connection'?'active':'']" @click="drawerTab='connection'">Подключение</button>
        <button id="drawer-tab-character" role="tab" aria-controls="drawer-panel-character"
                :aria-selected="drawerTab==='character' ? 'true' : 'false'"
                :class="['tab-btn', drawerTab==='character'?'active':'']" @click="drawerTab='character'">Персонаж</button>
        <button id="drawer-tab-memory" role="tab" aria-controls="drawer-panel-memory"
                :aria-selected="drawerTab==='memory' ? 'true' : 'false'"
                :class="['tab-btn', drawerTab==='memory'?'active':'']" @click="drawerTab='memory'">Память</button>
        <button id="drawer-tab-persona" role="tab" aria-controls="drawer-panel-persona"
                :aria-selected="drawerTab==='persona' ? 'true' : 'false'"
                :class="['tab-btn', drawerTab==='persona'?'active':'']" @click="drawerTab='persona'">Персона</button>
        <div style="flex:1"></div>
        <button class="tab-btn" @click="drawerTab=null" title="Закрыть" aria-label="Закрыть">✕</button>
      </div>
      <div class="body">

        <!-- ВКЛАДКА: Генерация -->
        <div v-if="drawerTab==='generation'" id="drawer-panel-generation" role="tabpanel" aria-labelledby="drawer-tab-generation">
          <h3>Параметры генерации</h3>
          <label>Модель <input v-model="params.model" list="models-list" placeholder="как в прокси" />
            <datalist id="models-list"><option v-for="m in models" :key="m" :value="m"></option></datalist>
          </label>
          <label>Temperature <span class="range-val">{{ params.temperature }}</span>
            <input type="range" min="0" max="2" step="0.05" v-model.number="params.temperature" /></label>
          <label>Top P <span class="range-val">{{ params.top_p }}</span>
            <input type="range" min="0" max="1" step="0.01" v-model.number="params.top_p" /></label>
          <label>Top K <span class="range-val">{{ params.top_k }}</span>
            <input type="number" v-model.number="params.top_k" /></label>
          <label>Max tokens — длина ОТВЕТА <span class="range-val">{{ params.max_tokens }}</span>
            <input type="number" min="256" step="256" v-model.number="params.max_tokens" /></label>
          <p class="muted" style="margin:2px 0 10px">Это лимит ВЫВОДА (одного ответа), не памяти. Рассуждения 💭 тратят этот же лимит — при «высоких» держите 8000+.</p>
          <div class="hr"></div>
          <h3>💰 Расход квоты</h3>
          <p class="muted" style="margin:2px 0 8px">Три настройки ниже определяют, сколько токенов уходит провайдеру на КАЖДОМ ходу. Готовые режимы:</p>
          <div class="row" style="gap:6px; margin:0 0 6px; flex-wrap:wrap">
            <button v-for="m in economyModes" :key="m.id"
                    :class="isEconomyMode(m) ? 'btn-primary' : ''"
                    :title="m.hint" @click="applyEconomyMode(m)" :aria-label="m.hint">{{ m.label }}</button>
            <button @click="usageOpen = true; loadUsage()" title="Сколько токенов реально потрачено" aria-label="Сколько токенов реально потрачено">📊 Расход</button>
          </div>

          <label>🧠 Окно контекста — память диалога (токенов) <span class="range-val">{{ params.context_tokens >= 1000000 ? '1 млн (максимум)' : params.context_tokens }}</span>
            <input type="number" min="4000" max="1000000" step="4000" v-model.number="params.context_tokens" /></label>
          <div class="row" style="gap:6px; margin:-4px 0 6px; flex-wrap:wrap">
            <button v-for="p in [[32000,'32к'],[128000,'128к'],[200000,'200к'],[1000000,'1 млн']]" :key="p[0]"
                    :class="params.context_tokens === p[0] ? 'btn-primary' : ''"
                    @click="params.context_tokens = p[0]">{{ p[1] }}</button>
          </div>
          <p class="muted" style="margin:2px 0 10px">Сколько ИСТОРИИ чата видит модель на каждый ход. <b>Важно про цену:</b> у Gemini вход свыше ~200 тыс. токенов тарифицируется <b>вдвое дороже — целиком</b>, поэтому 200к выгоднее 1 млн почти без потери памяти. Что не влезло — сохранит авто-сводка (вкладка «Память»).</p>

          <!-- Своё число вместо четырёх готовых: у кого чат из десятков фото,
               тому 8 МБ мало, а 20 уже дорого. Поле пишет значение по change,
               а не по вводу: пустое поле посреди набора не должно улетать на
               сервер (см. numFromInput). Кнопки — прежние варианты в один клик. -->
          <label>📎 Файлы в памяти диалога (МБ на ход) <span class="range-val">{{ params.history_files_mb ? 'до ' + params.history_files_mb + ' МБ' : 'все файлы' }}</span>
            <input type="number" min="0" max="1000" step="1" inputmode="numeric"
                   :value="params.history_files_mb"
                   @change="params.history_files_mb = numFromInput($event, 1000, params.history_files_mb)" /></label>
          <div class="row" style="gap:6px; margin:-4px 0 6px; flex-wrap:wrap">
            <button v-for="p in [[3,'3 МБ'],[8,'8 МБ'],[20,'20 МБ'],[0,'все файлы']]" :key="'mb' + p[0]"
                    :class="params.history_files_mb === p[0] ? 'btn-primary' : ''"
                    :aria-pressed="params.history_files_mb === p[0] ? 'true' : 'false'"
                    @click="params.history_files_mb = p[0]">{{ p[1] }}</button>
          </div>
          <label>📎 …и только из последних сообщений <span class="range-val">{{ params.history_files_turns ? params.history_files_turns + ' сообщ.' : 'без ограничения' }}</span>
            <input type="number" min="0" max="1000" step="1" inputmode="numeric"
                   :value="params.history_files_turns"
                   @change="params.history_files_turns = numFromInput($event, 1000, params.history_files_turns)" /></label>
          <div class="row" style="gap:6px; margin:-4px 0 6px; flex-wrap:wrap">
            <button v-for="p in [[6,'6'],[12,'12'],[24,'24'],[0,'все']]" :key="'turns' + p[0]"
                    :class="params.history_files_turns === p[0] ? 'btn-primary' : ''"
                    :aria-pressed="params.history_files_turns === p[0] ? 'true' : 'false'"
                    @click="params.history_files_turns = p[0]">{{ p[1] }}</button>
          </div>
          <p class="muted" style="margin:2px 0 10px">0 в любом из двух полей — без ограничения (дорого). По умолчанию 8 МБ из последних 12 сообщений.</p>
          <p class="muted" style="margin:2px 0 10px"><b>Главная статья расхода в долгих чатах.</b> Прежние фото/аудио/видео пересылаются модели заново на КАЖДОМ ходу — она их «видит», а не вспоминает по пометкам. Одно видео без ограничений = десятки тысяч токенов входа в каждом ходу до конца чата. Файл вне окна модель по-прежнему знает по пометке <code>[видео: имя]</code>.</p>

          <label>📚 База знаний в контексте
            <select v-model.number="params.knowledge_chars">
              <option :value="0">без ограничения (дорого)</option>
              <option :value="200000">до 200 тыс. символов</option>
              <option :value="60000">до 60 тыс. символов (по умолчанию)</option>
              <option :value="20000">до 20 тыс. символов (экономно)</option>
            </select>
          </label>
          <p class="muted" style="margin:2px 0 10px">Справочные файлы чата уходят модели в каждом запросе. 200 тыс. символов ≈ 50 тыс. токенов входа на каждом ходу — за сотню ходов это 5 млн токенов только на справочник.</p>
          <label>👥 Пауза между ответами в группе (сек) <span class="range-val">{{ groupReplyDelay }}</span>
            <input type="range" min="0" max="15" step="1" v-model.number="groupReplyDelay" @change="saveUiPrefs" /></label>
          <p class="muted" style="margin:2px 0 10px">Когда в групповом чате отвечают несколько персонажей подряд (напр. <code>+A +B +C</code>), запросы быстро выбирают квоту провайдера — и прилетает ошибка «429 Resource exhausted». Пауза разносит ответы во времени. 0 — без паузы.</p>
          <label>Repetition penalty <span class="range-val">{{ params.repetition_penalty }}</span>
            <input type="range" min="0.8" max="2" step="0.05" v-model.number="params.repetition_penalty" /></label>
          <div class="hr"></div>
          <h3>🛡 Обход цензуры</h3>
          <div class="row" style="gap:8px; margin-bottom:6px">
            <span class="safety-badge" :class="params.disable_safety ? 'off' : 'on'">
              {{ params.disable_safety ? '⚠ Фильтры сняты' : '🛡 Стандартная фильтрация' }}
            </span>
          </div>
          <label class="check" :class="params.disable_safety ? 'danger-text' : ''"><input type="checkbox" v-model="params.disable_safety" /> Zero-Censorship — снять настраиваемые фильтры провайдера</label>
          <template v-if="params.disable_safety">
            <label>Порог для всех категорий
              <select v-model="params.safety_preset">
                <option value="off">OFF — фильтр выключен полностью (максимум свободы)</option>
                <option value="none">BLOCK_NONE — не блокировать, но оценивать</option>
                <option value="high_only">BLOCK_ONLY_HIGH — только явные нарушения</option>
                <option value="provider">не передавать порог — решает провайдер</option>
              </select>
            </label>
            <p class="muted" style="margin:2px 0 6px">У Gemini 2.5/3 <code>OFF</code> и так дефолт, но мы ставим его явно на все пять категорий — включая «гражданскую добропорядочность», о которой обычно забывают.</p>
            <details style="margin:0 0 10px">
              <summary class="muted" style="cursor:pointer">Пороги по отдельным категориям</summary>
              <p class="muted" style="margin:6px 0">Нужно, когда душит одна конкретная категория, а остальные трогать не хочется.</p>
              <label v-for="c in safetyCategories" :key="c[0]" style="margin:4px 0">{{ c[1] }}
                <select :value="params.safety_overrides && params.safety_overrides[c[0]] || ''"
                        @change="setSafetyOverride(c[0], $event.target.value)">
                  <option value="">как общий порог выше</option>
                  <option value="OFF">OFF — выключен</option>
                  <option value="BLOCK_NONE">BLOCK_NONE</option>
                  <option value="BLOCK_ONLY_HIGH">BLOCK_ONLY_HIGH</option>
                </select>
              </label>
            </details>
          </template>

          <p v-if="reasoningConflict" class="danger-text" style="margin:2px 0 10px; font-size:var(--fs-100)">
            ⚠ <b>Размышления возвращают цензуру.</b> У вас выбран уровень размышлений «{{ params.reasoning_effort }}» вместе со снятыми фильтрами. У Gemini режим размышлений добавляет СВОЮ модерацию поверх <code>safety_settings</code> — она душит контент даже при пороге OFF. Если ловите пустые ответы — поставьте размышления в «выключены».
          </p>

          <label class="check"><input type="checkbox" v-model="jailbreak.enabled" @change="saveUiPrefs" />
            📌 Общие инструкции перед ответом (для ВСЕХ персонажей)</label>
          <template v-if="jailbreak.enabled">
            <textarea rows="5" v-model="jailbreak.text" @change="saveUiPrefs"
                      placeholder="Ваши инструкции. Уходят в САМЫЙ конец контекста — после Post-History персонажа, прямо перед ответом."></textarea>
            <div class="row" style="gap:6px; margin:4px 0; flex-wrap:wrap">
              <input v-model="jailbreakPresetName" placeholder="имя пресета" style="flex:1; min-width:120px" />
              <button class="btn-primary" @click="saveJailbreakPreset">Сохранить</button>
            </div>
            <!-- Тег пресета несёт действие «применить», но был просто span с
                 @click: с клавиатуры пресет применить было нельзя. Настоящей
                 кнопкой тег стать не может — внутри него живёт ссылка удаления,
                 а интерактивное внутри интерактивного ломает и то и другое. -->
            <div class="row" style="gap:6px; flex-wrap:wrap">
              <span v-for="(p, i) in jailbreak.presets" :key="i" class="tag" style="cursor:pointer"
                    role="button" tabindex="0" :aria-label="'Применить пресет ' + p.name"
                    @click="applyJailbreakPreset(p)"
                    @keydown.enter="applyJailbreakPreset(p)"
                    @keydown.space.prevent="applyJailbreakPreset(p)"
                    :title="p.text.slice(0, 200)">
                {{ p.name }} <a href="#" @click.stop.prevent="deleteJailbreakPreset(i)">✕</a>
              </span>
            </div>
            <p class="muted" style="margin:6px 0 10px">Раньше такой текст приходилось дублировать в карточке КАЖДОГО персонажа («Инструкции перед ответом»). Здесь он общий: пишется один раз и применяется во всех чатах — личных, групповых и в Telegram. Инструкции персонажа при этом никуда не деваются, общие идут после них.</p>
          </template>

          <details style="margin:0 0 10px">
            <summary class="muted" style="cursor:pointer">Что снять фильтрами НЕЛЬЗЯ</summary>
            <p class="muted" style="margin:6px 0">У Google два вида фильтров. Настраиваемые (пять категорий выше) снимаются порогами. А <code>PROHIBITED_CONTENT</code>, <code>SPII</code>, <code>BLOCKLIST</code>, <code>RECITATION</code> — <b>неотключаемые</b>: они срабатывают всегда, никакой порог на них не влияет. Если ответ пуст — приложение теперь пишет, какой именно фильтр сработал и можно ли с ним что-то сделать, вместо общего «попробуйте переформулировать».</p>
          </details>
          <label class="check"><input type="checkbox" v-model="params.send_avatars" /> Показывать нейросети аватары (внешность персонажа и ролевика)</label>
          <label class="check"><input type="checkbox" v-model="params.web_access" /> 🌐 Доступ в интернет (веб-поиск на каждый запрос)</label>
          <label class="check"><input type="checkbox" v-model="params.assistant_mode" /> 🎓 Режим ассистента — без отыгрыша</label>
          <p class="muted" style="margin:2px 0 10px">Персонаж перестаёт «оставаться в образе» и просто выполняет задачу: написать пост, разобрать код, перевести текст. Нужно потому, что в обычном режиме прямо перед вашей репликой стоит напоминание держать роль — и прикладная просьба ему проигрывает, тем сильнее, чем длиннее чат. Для одного сообщения включать тумблер не нужно: напишите <code>((текст))</code> или <code>/ooc текст</code>.</p>

          <div class="hr"></div>
          <h3>Рассуждения (thinking) 💭</h3>
          <label>Бюджет размышлений модели
            <select v-model="params.reasoning_effort">
              <option value="">авто (решает модель)</option>
              <option value="disable">выключены</option>
              <option value="low">низкие</option>
              <option value="medium">средние</option>
              <option value="high">высокие</option>
            </select>
          </label>
          <label class="check"><input type="checkbox" v-model="params.file_reasoning" /> 📎 Включать рассуждения при работе с файлами (если выше «авто» — Gemini местами не думает над файлами сам)</label>
          <p class="muted" style="margin:2px 0">Размышления видны live в чате (блок 💭), в ответ не входят. Учтите: при малом Max tokens длинные размышления могут «съесть» лимит ответа. <b>При включённом Zero-Censorship авто-рассуждения над файлами НЕ применяются</b> — у Gemini режим размышлений добавляет свою цензуру. Нужны рассуждения и свобода вместе — выберите уровень вручную выше (осознанно).</p>

          <template v-if="isAdmin">
            <div class="hr"></div>
            <h3>Загрузка чата</h3>
            <label>Сколько сообщений подгружать (на открытии и за раз при скролле вверх)
              <input type="number" min="10" max="400" step="10" v-model.number="messagePreload" @change="saveUiPrefs" />
            </label>
            <p class="muted" style="margin:2px 0">Меньше — быстрее открываются длинные чаты; больше — сразу видно больше истории. Настройка общая (задаёт админ).</p>
          </template>

          <div class="hr"></div>
          <h3>Пресеты</h3>
          <div class="row"><input v-model="presetName" placeholder="имя пресета" />
            <button class="btn-primary" @click="savePreset">Сохранить</button></div>
          <p class="muted" style="margin:4px 0">⭐ — пресет по умолчанию (применяется при запуске).</p>
          <div class="card" v-for="p in presets" :key="p.id">
            <div class="row-between"><b>{{ p.is_default ? '⭐ ' : '' }}{{ p.name }}</b>
              <span>
                <button @click="applyPreset(p)">Применить</button>
                <button @click="setDefaultPreset(p)" :title="'Сделать по умолчанию'" :aria-label="'Сделать по умолчанию'">⭐</button>
                <button class="btn-danger" @click="deletePreset(p)" :aria-label="'Удалить пресет ' + p.name">🗑</button>
              </span></div>
          </div>
        </div>

        <!-- ВКЛАДКА: Подключение -->
        <div v-if="drawerTab==='connection'" id="drawer-panel-connection" role="tabpanel" aria-labelledby="drawer-tab-connection">
          <h3>Подключение к LiteLLM</h3>
          <p class="muted">Обработка идёт на сервере. Браузер в прокси не ходит.</p>
          <label class="check"><input type="checkbox" v-model="connection.use_proxy" /> Использовать LiteLLM-прокси</label>
          <label>Адрес прокси (Base URL)<input v-model="connection.base_url" placeholder="http://localhost:4000" /></label>
          <label>API ключ прокси (master key)<input v-model="connection.api_key" type="password" placeholder="sk-..." /></label>
          <label>Модель по умолчанию<input v-model="connection.default_model" placeholder="gpt-4o" /></label>
          <label>Модель для генерации артов (необязательно)<input v-model="connection.image_model" placeholder="например imagen-4 / nano-banana" /></label>
          <label class="check"><input type="checkbox" v-model="connection.image_via_chat" /> Арт через чат (nano-banana: модель «видит» аватары и фото из чата). Иначе — image_generation (imagen).</label>
          <div class="hr"></div>
          <h3>Запасная модель</h3>
          <p class="muted">Если основная модель не ответила (ошибка провайдера, пустой ответ) — ход можно повторить запасной: автоматически или кнопкой «⚡ запасной моделью» в баннере ошибки.</p>
          <label>Запасная модель (пусто = выключено)
            <input v-model="connection.fallback_model" list="models-list" placeholder="например gemini-2.5-flash" />
          </label>
          <label class="check"><input type="checkbox" v-model="connection.auto_fallback" /> Автоматически отвечать запасной моделью при сбое основной</label>
          <div class="hr"></div>
          <h3>Модели памяти Horae</h3>
          <p class="muted">Сводка сюжета и факты считаются фоном после ходов. Быстрая дешёвая модель справляется с этим не хуже основной.</p>
          <!-- У каждого поля — своя однострочная подсказка прямо под ним, а не
               общий абзац сверху: «что будет, если оставить пустым» решается у
               конкретного поля, и искать ответ выше по панели никто не станет. -->
          <label>Быстрая модель для сводки и фактов
            <input v-model="connection.summary_model" list="models-list" placeholder="например gemini-2.5-flash"
                   aria-describedby="conn-summary-hint" />
            <span id="conn-summary-hint" class="field-hint">Пусто — сводку и факты считает модель по умолчанию.</span>
          </label>
          <label>Модель эмбеддингов (необязательно)
            <input v-model="connection.embedding_model" placeholder="например text-embedding-3-small / gemini-embedding-001"
                   aria-describedby="conn-embed-hint" />
            <span id="conn-embed-hint" class="field-hint">Пусто — факты подбираются по совпадению слов; с моделью — по смыслу.</span>
          </label>
          <div class="row">
            <button class="btn-primary" @click="testConnection">Проверить и загрузить модели</button>
            <button @click="saveConnection">Сохранить</button>
          </div>
          <p v-if="connStatus" :class="connOk === true ? 'status-ok' : (connOk === false ? 'status-err' : 'muted')">{{ connStatus }}</p>
          <div v-if="models.length"><div class="hr"></div><b>Доступные модели:</b>
            <div class="card" style="max-height:180px; overflow:auto">
              <div v-for="m in models" :key="m" class="row-between">
                <span>{{ m }}</span><button @click="params.model = m">выбрать</button></div>
            </div>
          </div>
        </div>

        <!-- ВКЛАДКА: Персонаж -->
        <div v-if="drawerTab==='character'" id="drawer-panel-character" role="tabpanel" aria-labelledby="drawer-tab-character">
          <h3>Редактор персонажа</h3>
          <div v-if="charEdit">
            <p class="muted" style="margin:0 0 8px">Изменения сохраняются автоматически при выходе из поля.</p>
            <label>Имя<input v-model="charEdit.name" placeholder="Имя персонажа" @change="saveCharacter" /></label>
            <label>Аватар</label>
            <div class="row" style="margin-bottom:10px">
              <img v-if="charEdit.avatar_path" :src="charEdit.avatar_path" class="avatar" style="width:48px;height:48px" alt="" />
              <label class="btn" style="margin:0; cursor:pointer">Загрузить файл
                <input type="file" accept="image/*" class="file-input" @change="onAvatarFile"
                       aria-label="Загрузить аватар персонажа" />
              </label>
              <button v-if="charEdit.avatar_path" class="btn-danger" @click="charEdit.avatar_path=''; saveCharacter()">убрать</button>
            </div>
            <label>Описание
              <textarea rows="4" v-model="charEdit.description" @change="saveCharacter" data-first-field
                        placeholder="Кто это: внешность, происхождение, ключевые факты биографии"></textarea></label>
            <label>Характер (personality)
              <textarea rows="3" v-model="charEdit.personality" @change="saveCharacter"
                        placeholder="Черты характера, манера речи, привычки, страхи и желания"></textarea></label>
            <label>Сценарий
              <textarea rows="3" v-model="charEdit.scenario" @change="saveCharacter"
                        placeholder="Сеттинг и текущая ситуация: где происходит действие, что вокруг"></textarea></label>
            <label>Первое сообщение
              <textarea rows="4" v-model="charEdit.first_message" @change="saveCharacter"
                        placeholder="Реплика, с которой персонаж начинает каждый новый чат"></textarea></label>
            <label>Системный промпт
              <textarea rows="4" v-model="charEdit.system_prompt" @change="saveCharacter"
                        placeholder="Прямые инструкции модели: стиль ответов, ограничения, формат"></textarea></label>
            <label>Примеры реплик (держат «голос» персонажа)
              <textarea rows="3" v-model="charEdit.mes_example" @change="saveCharacter"
                        placeholder="Образцы того, как персонаж говорит (диалоги-примеры). Модель подстроит стиль под них."></textarea></label>
            <label>Инструкции перед ответом (Post-History / «джейлбрейк»)
              <textarea rows="3" v-model="charEdit.post_history_instructions" @change="saveCharacter"
                        placeholder="Переинъектируются в САМЫЙ конец контекста, прямо перед ответом — сильнее всего держат характер и правила в длинных чатах."></textarea></label>
            <p class="muted" style="margin:-4px 0 10px">💡 «Инструкции перед ответом» — самая мощная позиция: то, что персонаж обязан соблюдать всегда. Подхватываются и из карточек SillyTavern.</p>
            <label>Модель персонажа (необязательно)
              <input v-model="charEdit.model" placeholder="пусто — модель из настроек генерации" @change="saveCharacter" /></label>
            <button class="btn-primary" @click="saveCharacter">💾 Сохранить сейчас</button>
          </div>
          <p v-else class="muted">Выберите персонажа слева.</p>
        </div>

        <!-- ВКЛАДКА: Память — панель Хроники (frontend/horae.js) и два раздела
             app.js в её слотах: «Сжатие истории» (чем сжимать, окно,
             мастер-снимок) и «Лорбук» (записи памяти). Персонаж — только у
             личного чата: у группы и чужого чата профиля персонажа нет, и
             уровень «Персонаж» в настройках Хроники выключен. -->
        <div v-if="drawerTab==='memory'" id="drawer-panel-memory" role="tabpanel" aria-labelledby="drawer-tab-memory">
          <horae-panel :session-id="sessionId" :tick="horaeTick"
                       :extra-tabs="memoryExtraTabs" :request="memoryTabReq"
                       :character-id="currentIsGroup || sharedView ? null : ((currentSession && currentSession.character_id) || selectedCharacterId)">
            <template #compress>
              <fieldset class="mem-engine">
                <legend>Чем сжимать старую историю</legend>
                <label class="check"><input type="radio" name="mem-engine" value="snapshot"
                       :checked="memoryEngine === 'snapshot'" :disabled="memoryEngineLocked"
                       @change="setMemoryEngine('snapshot')" />
                  <span><b>Мастер-снимок</b> — рекомендуется. Подробный пересказ старой части чата:
                    хроника, персонажи, факты и лор, списки.</span></label>
                <label class="check"><input type="radio" name="mem-engine" value="horae"
                       :checked="memoryEngine === 'horae'" :disabled="!isAdmin"
                       @change="setMemoryEngine('horae')" />
                  <span><b>Свёртки Хроники</b> — короткие свёртки событий в ленте Хроники, как в плагине
                    Horae. Пороги — «Настройки» → «Авто-свёртка хронологии».</span></label>
                <label class="check"><input type="radio" name="mem-engine" value="off"
                       :checked="memoryEngine === 'off'" :disabled="memoryEngineLocked"
                       @change="setMemoryEngine('off')" />
                  <span><b>Не сжимать</b> — собранный снимок остаётся, но больше не обновляется.</span></label>
              </fieldset>
              <p class="muted" style="margin:2px 0 10px">Обновляется что-то одно — за одну переписку вы не
                платите дважды. Что уже сжато, остаётся в ходе без повторов: снимок несёт историю до своей
                отметки, свёртки Хроники — после неё. Обновление снимка или свёртка — <b>отдельный платный
                запрос</b> (📊: <code>summary</code>, <code>horae</code>).</p>
              <p v-if="!isAdmin" class="field-hint">Свёртки Хроники для всех чатов включает администратор;
                для своего чата — «Настройки» → уровень «Чат» → «Авто-свёртка хронологии».</p>
              <p v-if="chatEngineNote" class="field-hint mem-engine-note">{{ chatEngineNote }}</p>
              <label v-if="memoryEngine === 'snapshot'">Как часто обновлять снимок
                <select v-model.number="summaryEvery" @change="saveUiPrefs">
                  <option :value="6">каждые 6 сообщений (точнее, дороже)</option>
                  <option :value="10">каждые 10 сообщений (по умолчанию)</option>
                  <option :value="20">каждые 20 сообщений (экономно)</option>
                </select>
              </label>
              <label v-if="memoryEngine === 'snapshot'" class="check"><input type="checkbox" v-model="horaeFacts" @change="saveUiPrefs" />
                🧩 Факты: вместе со снимком ИИ выписывает из переписки отдельные факты (имена, обещания,
                предметы), а на каждом ходу в контекст попадают только те, что связаны с вашей репликой.</label>
              <label>Активное окно (сколько последних сообщений модель видит дословно) <span class="range-val">{{ memoryWindow ? memoryWindow + ' сообщ.' : 'вся история' }}</span>
                <input type="number" min="0" max="1000" step="1" inputmode="numeric"
                       :value="memoryWindow"
                       @change="setMemoryWindow(numFromInput($event, 1000, memoryWindow))" /></label>
              <div class="row" style="gap:6px; margin:-4px 0 6px; flex-wrap:wrap">
                <button v-for="p in [[20,'20'],[50,'50'],[80,'80'],[150,'150'],[0,'вся история']]" :key="'win' + p[0]"
                        :class="memoryWindow === p[0] ? 'btn-primary' : ''"
                        :aria-pressed="memoryWindow === p[0] ? 'true' : 'false'"
                        @click="setMemoryWindow(p[0])">{{ p[1] }}</button>
              </div>
              <p class="muted" style="margin:2px 0 10px">Всё старше окна модель получает сжатым — снимком
                или свёртками Хроники. Из окна уходит только то, что уже сжато: пока сжатие догоняет
                длинный чат, модель видит историю целиком.</p>

              <section v-if="sessionId" ref="memSection" class="card mem-master" aria-labelledby="mem-master-h">
                <!-- Пока сброс ждёт сервера (memPurging), выключены ВСЕ кнопки блока:
                     запуск, остановка, экспорт и перечитка статуса наперегонки со
                     сбросом показали бы память, которой через миг не станет.
                     tabindex -1 у заголовка — чтобы было куда вернуть фокус, когда
                     кнопка под ним выключилась или исчезла (_memFocus). -->
                <div class="row-between"><h4 id="mem-master-h" ref="memHeading" tabindex="-1">🧠 Мастер-снимок этого чата</h4>
                  <button class="btn-icon" :disabled="memPurging" @click="loadMemStatus" aria-label="Обновить статус памяти">↻</button></div>
                <!-- «Старая схема» — только у непустого снимка: пакет из одних пустых
                     сообщений даёт запись с пустым текстом, is_structured("") на
                     сервере ложно, но пересобирать там нечего. Пробел между метками
                     Vue при сборке шаблона убирает — промежуток даёт CSS (.mem-status). -->
                <p v-if="chatEngine === 'horae'" class="field-hint mem-idle-note">Не обновляется: дальше историю этого
                  чата сжимают свёртки Хроники. Собранное снимком остаётся в ходе и несёт историю до своей отметки.</p>
                <p class="muted mem-status" v-if="memStatus && memStatus.snapshot.exists">
                  Снимок {{ fmtNum(memStatus.snapshot.tokens) }} / {{ fmtNum(memStatus.snapshot.budget) }} ток.
                  · учтено до #{{ memStatus.snapshot.covered_upto }}
                  · ждут сжатия {{ memStatus.backlog.pending }}
                  <span v-if="!memStatus.snapshot.structured && memStatus.snapshot.tokens" class="tag">старая схема — пересоберите</span>
                  <span v-if="memStatus.snapshot.over_budget" class="tag">больше бюджета</span></p>
                <p class="muted" v-else-if="memStatus">Снимка нет: окно пропускает историю целиком.
                  Ждут сжатия {{ memStatus.backlog.pending }}.</p>
                <p class="muted" v-else>{{ memBusy ? 'Загружаю статус памяти…' : 'Статус памяти недоступен.' }}</p>
                <!-- Сбой ежеходного обновления и предупреждения снимка/задания.
                     Сбой — цветом предупреждения: память стоит, пока его не
                     исправят; предупреждения — серым, как подсказки полей. -->
                <p v-if="memLastError" class="muted mem-failed">{{ memLastError }}</p>
                <ul v-if="memWarnings.length" class="mem-warnings">
                  <li v-for="(w, i) in memWarnings" :key="'mw' + i" class="field-hint">{{ w }}</li>
                </ul>
                <!-- Буфер пересборки без задания. manual — ручную «Пересобрать»
                     прервали (остановка, ошибка, перезапуск сервера): продолжаем с
                     места. Не manual — старую сводку (до 2.4.0) сервер переводит в
                     новую схему сам, ежеходными проходами: «прервана» тут неправда.
                     И «Продолжить» тут не к месту: буфера может ещё не быть
                     (указатель 0), и кнопка молча начала бы пересборку всего чата.
                     Доделать сразу умеет «Догнать» — сколько это, видно по «ждут
                     сжатия» в строке выше. -->
                <p class="muted" v-if="memStatus && memStatus.staging && !memJobActive">
                  <template v-if="memStatus.staging.manual">Пересборка прервана на #{{ memStatus.staging.last_message_id }}.
                    <button class="btn-primary" :disabled="memBusy || memPurging" @click="startMemJob('rebuild', true)">Продолжить</button></template>
                  <template v-else>Старая сводка переводится в новую схему<template v-if="memStatus.staging.last_message_id">: готово до #{{ memStatus.staging.last_message_id }}</template>.<template v-if="memStatus.backlog.pending"> «Догнать» доделает это сразу.</template></template></p>
                <!-- Прогресс не только цветом: рядом с полосой строка сервера с
                     числами «Обработано 140/800 | Сжато до 4 200 токенов» и время
                     начала, под ней — номер пакета и фаза (спека §9). Живой регион
                     стоит в DOM всегда, а без задания пуст: регион, вставленный уже
                     с текстом, многие скринридеры пропускают, и первое «В очереди»
                     терялось. «Остановить» — вне региона, иначе подпись кнопки
                     зачитывалась бы с каждой строкой прогресса. -->
                <div class="mem-progress" :class="{ 'mem-idle': !memJobActive }">
                  <span v-if="memJobActive" class="upload-track"><span class="upload-fill"
                    :style="{ width: (memStatus.job.total ? Math.round(memStatus.job.processed / memStatus.job.total * 100) : 0) + '%' }"></span></span>
                  <div role="status" aria-live="polite"><template v-if="memJobActive">
                    <div class="muted">{{ memJobLine }}</div>
                    <div class="muted" v-if="memJobPhase">{{ memJobPhase }}</div>
                    <!-- Опрос третий раз подряд не дозвался сервера: полоса стоит не
                         потому, что пакет долгий, — говорим об этом прямо. -->
                    <div class="muted mem-offline" v-if="memPollFails >= 3">Нет связи с сервером, повторяю…</div>
                  </template></div>
                  <button v-if="memJobActive" ref="memStopBtn" class="btn-danger" :disabled="memPurging" @click="cancelMemJob">Остановить</button>
                </div>
                <p v-if="memStatus && memStatus.job && memStatus.job.status === 'error'" class="danger-text">⚠ {{ memStatus.job.error }}</p>
                <div class="row mem-actions">
                  <button class="btn-primary" :disabled="memBusy || memJobActive || !memStatus || memPurging" @click="startMemJob('rebuild')">Пересобрать с нуля</button>
                  <button :disabled="memBusy || memJobActive || !memStatus || !memStatus.backlog.pending || memPurging" @click="startMemJob('catchup')">Догнать</button>
                  <button :disabled="!memStatus || !memStatus.snapshot.exists || memPurging" @click="exportMemory">Экспорт .md</button>
                  <button class="btn-danger" :disabled="memBusy || memJobActive || !memStatus || memPurging" @click="purgeMemory">{{ memPurging ? 'Сбрасываю…' : 'Сбросить снимок и факты' }}</button>
                </div>
                <!-- Поля пишут значение по change, а не по вводу, через numFromInput:
                     стёртое поле или «2,5» не должны улететь на сервер (422).
                     setMemPref помечает ключ выбранным человеком: пока поле не
                     трогали, оно показывает умолчание сервера и в «ui» не пишется. -->
                <label>Размер пакета
                  <input type="number" min="1" max="200" step="1" inputmode="numeric"
                         :value="memoryBatch" aria-describedby="mem-batch-hint"
                         @change="setMemPref('memory_batch', numFromInput($event, 200, memoryBatch, 1))" />
                  <span id="mem-batch-hint" class="field-hint">Сообщений в одном запросе к модели сводки, 1–200. Больше — меньше запросов, но каждый тяжелее.</span></label>
                <label>Пауза между запросами, мс
                  <input type="number" min="0" max="60000" step="100" inputmode="numeric"
                         :value="memoryDelayMs" aria-describedby="mem-delay-hint"
                         @change="setMemPref('memory_delay_ms', numFromInput($event, 60000, memoryDelayMs))" />
                  <span id="mem-delay-hint" class="field-hint">0–60000. Пауза бережёт лимит запросов провайдера при длинной пересборке.</span></label>
                <!-- Потолок поля 40 000, хотя сервер примет и 200 000: снимок модель
                     переписывает ЦЕЛИКОМ в одном ответе, и бюджет выше лимита вывода
                     модели памяти обрывал бы каждое обновление. Какой лимит у
                     выбранной модели, сервер знает сам (max_snapshot_tokens). -->
                <label>Бюджет снимка, токенов
                  <input type="number" min="1000" max="40000" step="1000" inputmode="numeric"
                         :value="memorySnapshotTokens" aria-describedby="mem-snap-hint mem-snap-cap"
                         @change="setMemPref('memory_snapshot_tokens', numFromInput($event, 40000, memorySnapshotTokens, 1000))" />
                  <span id="mem-snap-hint" class="field-hint">1000–40000, не больше лимита вывода модели памяти. Столько снимок занимает в каждом ходе: больше — подробнее память, но дороже ход.</span>
                  <span v-if="memSnapCap" id="mem-snap-cap" class="field-hint">{{ memSnapCap }}</span></label>
                <!-- Монитор токенов: из чего складывается ход — системный промпт с
                     якорями, мастер-снимок с фактами и дословное окно. Числа стоят
                     в подписях, полоса лишь повторяет их цветом. -->
                <template v-if="memTiers">
                  <div class="ins-bar" aria-hidden="true">
                    <i v-for="r in memTierRows" :key="'mt' + r.cls" :class="r.cls" :style="{ width: r.pct + '%' }"></i>
                  </div>
                  <ul class="mem-tiers">
                    <li v-for="r in memTierRows" :key="'ml' + r.cls">
                      <span class="ins-dot" :class="r.cls" aria-hidden="true"></span>
                      <span>{{ r.label }} — {{ fmtNum(r.tokens) }}</span></li>
                  </ul>
                  <p class="muted mem-tier-note">Итого {{ fmtNum(memTiers.total) }} ток.<br>
                    из бюджета хода {{ fmtNum(memTiers.budget) }} ({{ Math.round(sharePct(memTiers.total, memTiers.budget)) }}%)<br>
                    из лимита модели {{ fmtNum(memTiers.model_limit || 1000000) }} ({{ Math.round(sharePct(memTiers.total, memTiers.model_limit || 1000000)) }}%)</p>
                </template>
                <!-- Блок виден только при открытом чате, поэтому «Откройте чат» тут
                     неправда: отчёт ещё считается или сервер его не собрал (группа
                     без участников, чат без персонажа). -->
                <p v-else-if="memTiersGroup" class="muted">В групповых чатах монитор уровней не считается: память собирается иначе, окна нет.</p>
                <p v-else-if="ctxBusy" class="muted">Считаю бюджет хода…</p>
                <p v-else-if="!ctxStats" class="muted">Бюджет хода недоступен: контекст этого чата не собирается.</p>
              </section>

            </template>
            <template #lore>
              <p class="muted">Лорбук — записи, которые вы ведёте сами. <b>always_on</b> — подмешивается в КАЖДЫЙ запрос (состояние, инвентарь, факты); иначе срабатывает по ключевым словам, как World Info. Области:
                <span class="scope-tag global">🌐 глоб.</span> во всех чатах,
                <span class="scope-tag session">💬 чат</span> только в этом,
                <span class="scope-tag character">🎭 перс.</span> из карточки персонажа.
                Записи сохраняются автоматически в БД. При импорте чата из SillyTavern сюда попадает снимок состояния (💬, always_on).</p>
              <div class="card">
                <input v-model="horaeEdit.title" placeholder="Заголовок" style="margin-bottom:6px" />
                <textarea v-model="horaeEdit.content" rows="3" placeholder="Содержимое" style="margin-bottom:6px"></textarea>
                <input v-model="horaeEdit.keywords" placeholder="ключевые слова через запятую" style="margin-bottom:2px" />
                <p class="muted" style="margin:0 0 6px; font-size:12px">Срабатывают по слову целиком и его склонениям: <code>меч</code> поймает «мечи», «мечом», «мечами», а <code>король</code> — «короля», «королём». На другие слова с тем же началом (<code>кот</code> → «который», «котёл») <b>не</b> срабатывает. Нужно шире — поставьте звёздочку: <code>замк*</code> поймает «замка», «замком», «замковый». Фраза с пробелом (<code>тёмный лес</code>) ищется как есть.</p>
                <div class="row" style="margin-bottom:6px">
                  <!-- Оба выпадающих списка стояли без подписи: ни label, ни
                       aria-label — вслух они читались как «список, lore» и «список,
                       глобально», без единого слова о том, что именно выбирают. -->
                  <select v-model="horaeEdit.category" aria-label="Категория записи памяти"><option>lore</option><option>state</option><option>inventory</option><option>character</option><option>hidden</option></select>
                  <input type="number" v-model.number="horaeEdit.priority" placeholder="приоритет" style="width:90px" />
                </div>
                <label class="check"><input type="checkbox" v-model="horaeEdit.always_on" /> always_on</label>
                <label class="check"><input type="checkbox" v-model="horaeEdit.enabled" /> включено</label>
                <div class="row" v-if="!horaeEdit.id">
                  <select v-model="horaeEdit.scope" aria-label="Область видимости записи"><option value="global">глобально</option><option value="session">только этот чат</option></select>
                </div>
                <div class="row">
                  <button class="btn-primary" @click="saveHorae">{{ horaeEdit.id ? 'Обновить' : 'Добавить' }}</button>
                  <button v-if="horaeEdit.id" @click="horaeEdit = blankHorae()">Отмена</button>
                </div>
              </div>
              <div class="card" v-for="h in horae" :key="h.id">
                <div class="row-between">
                  <b>{{ h.title || h.category }}</b>
                  <span style="display:inline-flex; align-items:center; gap:4px">
                    <span class="scope-tag" :class="h.session_id ? 'session' : (h.character_id ? 'character' : 'global')">{{ h.session_id ? '💬 чат' : (h.character_id ? '🎭 перс.' : '🌐 глоб.') }}</span>
                    <span class="tag">{{ h.always_on ? 'always' : ((h.keywords || []).join(',') || h.category) }}</span>
                    <button class="btn-icon" @click="editHorae(h)" :aria-label="'Изменить запись памяти: ' + (h.title || h.category)">✎</button>
                    <button class="btn-danger" @click="deleteHorae(h)" :aria-label="'Удалить запись памяти: ' + (h.title || h.category)">🗑</button>
                  </span>
                </div>
                <!-- Мастер-снимок (категория summary) — до 12 000 токенов с разделами
                     и списками: одним абзацем он растягивал карточку на десяток
                     экранов телефона под настройками, а структура пропадала.
                     Свёрнут, переводы строк сохранены; полный вид — «Экспорт .md». -->
                <p v-if="h.category === 'summary' && h.session_id" class="field-hint">Мастер-снимок чата: обновляется
                  сам, управление — в «Сжатие истории».</p>
                <details v-if="h.category === 'summary'" class="mem-snapshot">
                  <summary>Показать снимок ({{ textLines(h.content) }} {{ plural(textLines(h.content), 'строка', 'строки', 'строк') }})</summary>
                  <div class="muted mem-snapshot-text">{{ h.content }}</div>
                </details>
                <div v-else class="muted">{{ h.content }}</div>
              </div>
            </template>
          </horae-panel>
        </div>

        <!-- ВКЛАДКА: Персона + Author's Note -->
        <div v-if="drawerTab==='persona'" id="drawer-panel-persona" role="tabpanel" aria-labelledby="drawer-tab-persona">
          <h3>Персона пользователя</h3>
          <label>Активная персона в этом чате
            <select v-model="sessionPersonaId" @change="applySessionMeta({ persona_id: sessionPersonaId })">
              <option :value="null">— нет —</option>
              <option v-for="p in personas" :key="p.id" :value="p.id">{{ p.name }}</option>
            </select>
          </label>
          <div class="card">
            <input v-model="personaNew.name" placeholder="Имя персоны" style="margin-bottom:6px" />
            <textarea v-model="personaNew.description" rows="2" placeholder="Описание (кто я)"></textarea>
            <div class="row" style="margin-top:6px">
              <img v-if="personaNew.avatar_path" :src="personaNew.avatar_path" class="avatar" style="width:40px;height:40px" alt="" />
              <label class="btn" style="margin:0;cursor:pointer">Внешность (фото)
                <input type="file" accept="image/*" class="file-input" @change="onPersonaAvatar"
                       aria-label="Загрузить фотографию персоны" />
              </label>
            </div>
            <button class="btn-primary" @click="createPersona" style="margin-top:6px">Создать персону</button>
          </div>
          <div class="card" v-for="p in personas" :key="p.id">
            <div class="row-between">
              <span class="row" style="gap:8px"><img v-if="p.avatar_path" :src="p.avatar_path" class="avatar" alt="" /><b>{{ p.name }}</b></span>
              <button class="btn-danger" @click="deletePersona(p)" :aria-label="'Удалить персону ' + p.name">🗑</button>
            </div>
            <div class="muted">{{ p.description }}</div>
          </div>

          <div v-if="authStatus.accounts_enabled">
            <div class="hr"></div>
            <h3>Друзья-ролевики 🤝</h3>
            <div class="row">
              <input v-model="newFriendName" placeholder="Логин ролевика" />
              <button class="btn-primary" @click="addFriend">Добавить</button>
            </div>
            <div v-if="friendsIncoming.length" style="margin-top:8px">
              <p class="muted">Заявки в друзья:</p>
              <div class="card" v-for="f in friendsIncoming" :key="f.friendship_id">
                <div class="row-between"><b>{{ f.username }}</b>
                  <div class="row" style="gap:6px">
                    <button class="btn-primary" @click="acceptFriend(f)">Принять</button>
                    <button class="btn-danger" @click="declineFriend(f)">Отклонить</button>
                  </div>
                </div>
              </div>
            </div>
            <p v-if="friends.length" class="muted" style="margin:8px 0 4px">Ваши друзья:</p>
            <div class="card" v-for="f in friends" :key="'fr'+f.id">
              <div class="row-between">
                <b>👥 {{ f.username }}</b>
                <button class="btn-danger" @click="removeFriend(f)" title="Удалить из друзей" aria-label="Удалить из друзей">Удалить</button>
              </div>
            </div>
            <p class="muted" style="margin-top:8px">Делиться чатом: откройте чат в списке слева и нажмите 🔗 — друг увидит его в разделе «Доступные мне». Так же делятся и групповые чаты.</p>
          </div>

          <div class="hr"></div>
          <h3>Часовой пояс этого чата 🕒</h3>
          <p class="muted">Нейросеть видит ваше текущее время (утро/ночь, день недели) и метки времени сообщений показываются в этом поясе. Настройка сохраняется для каждого чата отдельно; по умолчанию берётся из браузера.</p>
          <label>Часовой пояс
            <input v-model="sessionTimezone" list="tz-list" placeholder="например Europe/Moscow" @change="applySessionMeta({ timezone: sessionTimezone })" />
            <datalist id="tz-list"><option v-for="tz in tzOptions" :key="tz" :value="tz"></option></datalist>
          </label>
          <div class="row">
            <button @click="_autoTimezone(); applySessionMeta({ timezone: sessionTimezone })">📍 Определить по браузеру</button>
          </div>

          <div class="hr"></div>
          <h3>Заметка автора (Author's Note)</h3>
          <p class="muted">Подмешивается у самого конца контекста — сильно влияет на ответ.</p>
          <textarea v-model="authorNote" rows="3" @blur="applySessionMeta({ author_note: authorNote })" placeholder="например: Пиши от третьего лица, держи мрачный тон."></textarea>
        </div>

      </div>
    </div>
  </div>

  <!-- ===== Админ-модалка ===== -->
  <div v-if="adminOpen" class="modal-backdrop" @click.self="adminOpen=false">
    <div class="modal" role="dialog" aria-modal="true" aria-label="Администрирование">
      <div class="row-between" style="margin-bottom:10px">
        <h3 style="margin:0">Администрирование</h3>
        <button class="btn-icon" @click="adminOpen=false" aria-label="Закрыть администрирование">✕</button>
      </div>

      <!-- Запрос пароля администратора -->
      <div v-if="authStatus.admin_set && !adminAuthed">
        <label>Пароль администратора
          <input v-model="adminPassInput" type="password" @keyup.enter="submitAdminPass" />
        </label>
        <p v-if="accessError" class="status-err">{{ accessError }}</p>
        <button class="btn-primary" @click="submitAdminPass">Войти</button>
      </div>

      <div v-else>
        <h4>Безопасность</h4>
        <!-- Секрет, заданный переменной окружения, ПЕРЕКРЫВАЕТ базу и не
             редактируется отсюда: иначе сохранение «проходило» бы, а работало
             всё равно значение из .env. -->
        <label>Код доступа к приложению (пусто = открыто всем)
          <span v-if="envLocked.access_code" class="env-locked">🔒 задан в .env</span>
          <input v-model="adminSec.access_code" placeholder="код для входа"
                 :disabled="envLocked.access_code"
                 :title="envLocked.access_code ? 'Значение приходит из переменной ACCESS_CODE' : ''" />
        </label>
        <label>Пароль администратора (пусто = без пароля)
          <span v-if="envLocked.admin_password" class="env-locked">🔒 задан в .env</span>
          <input v-model="adminSec.admin_password" type="password" placeholder="пароль админа"
                 :disabled="envLocked.admin_password"
                 :title="envLocked.admin_password ? 'Значение приходит из переменной ADMIN_PASSWORD' : ''" />
        </label>
        <label class="check"><input type="checkbox" v-model="adminSec.accounts_enabled" /> Режим аккаунтов (вход по логину/паролю, у каждого свои приватные данные)</label>
        <p class="muted">В режиме аккаунтов первый зарегистрированный — администратор. Код доступа не используется.</p>

        <div class="hr"></div>
        <h4>HTTP Basic Auth (защита браузером) 🔒</h4>
        <p class="muted">Браузер спросит логин и пароль ещё ДО загрузки приложения — как на «голом» сервере. Это внешний барьер поверх входа выше. Применяется ко всем, включая мобильный и Telegram WebApp. (WebSocket-чат не затрагивается — у него своя авторизация.)</p>
        <label class="check"><input type="checkbox" v-model="adminSec.basic_auth.enabled" /> Включить HTTP Basic Auth</label>
        <div class="row" style="gap:6px">
          <input v-model="adminSec.basic_auth.username" placeholder="логин" autocomplete="off" />
          <input v-model="adminSec.basic_auth.password" type="password" placeholder="пароль" autocomplete="new-password" />
        </div>
        <p v-if="adminSec.basic_auth.enabled && !adminSec.basic_auth.username" class="status-err">Укажите логин — иначе защита не включится.</p>
        <button class="btn-primary" @click="saveSecurity">Сохранить безопасность</button>

        <div class="hr"></div>
        <h4>Telegram-бот</h4>
        <label>Токен бота (от @BotFather)
          <span v-if="envLocked.telegram_token" class="env-locked">🔒 задан в .env</span>
          <input v-model="adminTg.token" type="password" placeholder="123456:ABC..."
                 :disabled="envLocked.telegram_token"
                 :title="envLocked.telegram_token ? 'Значение приходит из переменной TELEGRAM_BOT_TOKEN' : ''" />
        </label>
        <label>Персонаж по умолчанию
          <select v-model="adminTg.default_character_id">
            <option :value="null">— первый из списка —</option>
            <option v-for="c in characters" :key="c.id" :value="c.id">{{ c.name }}</option>
          </select>
        </label>
        <label>Модель бота (нейросеть из прокси; пусто = по умолчанию)
          <input v-model="adminTg.model" list="models-list" placeholder="как в прокси" />
        </label>
        <label class="check"><input type="checkbox" v-model="adminTg.open_to_all" /> Открыть бота для всех (иначе только белый список)</label>
        <label class="check"><input type="checkbox" v-model="adminTg.enabled" /> Запускать бота при старте сервера</label>
        <div class="row">
          <button class="btn-primary" @click="saveTelegram">Сохранить</button>
          <button @click="startBot">▶ Запустить</button>
          <button class="btn-danger" @click="stopBot">■ Остановить</button>
        </div>
        <p :class="adminTg.bot_state && adminTg.bot_state.running ? 'status-ok' : 'muted'">
          Бот: {{ adminTg.bot_state && adminTg.bot_state.running ? 'работает' : 'остановлен' }}
          <span v-if="adminTg.bot_state && adminTg.bot_state.error" class="status-err">— {{ adminTg.bot_state.error }}</span>
        </p>

        <div class="hr"></div>
        <h4>Белый список (доступ к боту по Telegram ID)</h4>
        <p class="muted">Пусто = бот открыт всем. Иначе пускаем только эти ID.</p>
        <div class="row">
          <input v-model="newWlId" placeholder="Telegram ID" />
          <button class="btn-primary" @click="wlAdd(newWlId)">Добавить</button>
        </div>
        <div class="card" v-for="id in adminTg.whitelist" :key="id">
          <div class="row-between"><b>{{ id }}</b><button class="btn-danger" @click="wlRemove(id)">убрать</button></div>
        </div>

        <h4>Заявки на доступ</h4>
        <p v-if="!adminTg.requests || !adminTg.requests.length" class="muted">Заявок нет. В боте — команда /request.</p>
        <div class="card" v-for="r in adminTg.requests" :key="r.id">
          <div class="row-between">
            <span>{{ r.first_name }} <span class="muted">@{{ r.username }} ({{ r.id }})</span></span>
            <button class="btn-primary" @click="wlAdd(r.id)">Одобрить</button>
          </div>
        </div>

        <div v-if="adminUsers.length">
          <div class="hr"></div>
          <h4>Пользователи</h4>
          <div class="card" v-for="u in adminUsers" :key="u.id">
            <div class="row-between">
              <span>{{ u.username }} <span class="tag">{{ u.role }}</span>
                <span v-if="u.telegram_id" class="muted">tg:{{ u.telegram_id }}</span></span>
              <span>
                <button v-if="u.role!=='admin'" @click="setUserRole(u,'admin')" title="Сделать админом" aria-label="Сделать админом">⬆ админ</button>
                <button v-else @click="setUserRole(u,'user')" title="Снять админа" aria-label="Снять админа">⬇ юзер</button>
                <button class="btn-danger" @click="deleteUser(u)" :aria-label="'Удалить пользователя ' + u.username">🗑</button>
              </span>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>

  <!-- ===== Модалка «База знаний чата» ===== -->
  <div v-if="kbOpen" class="modal-backdrop" @click.self="kbOpen=false">
    <div class="modal" style="width:460px" role="dialog" aria-modal="true" aria-label="База знаний чата">
      <div class="row-between" style="margin-bottom:10px">
        <h3 style="margin:0">📚 База знаний чата</h3>
        <button class="btn-icon" @click="kbOpen=false" aria-label="Закрыть базу знаний">✕</button>
      </div>
      <p class="muted" style="margin-top:0">Файлы, которые персонажи учитывают в КАЖДОМ ответе (в личном и групповом чате). Документы (PDF, Word, txt) читаются как текст; картинки/аудио/видео прикладываются целиком. Добавляйте при создании чата и в любой момент.</p>
      <div class="row" style="margin:8px 0">
        <label class="btn-primary" style="margin:0; cursor:pointer">
          {{ kbUploading ? '⏳ Загрузка…' : '➕ Добавить файлы' }}
          <input type="file" multiple class="file-input" :disabled="kbUploading"
                 aria-label="Добавить файлы в базу знаний чата"
                 accept="image/*,audio/*,video/*,.pdf,.doc,.docx,.odt,.rtf,.txt,.md,.csv" @change="onKnowledgeFiles" />
        </label>
      </div>
      <div v-if="!kbFiles.length" class="muted" style="font-size:var(--fs-100); padding:6px 0">База знаний пуста. Добавьте справочные файлы — лор, документы, картинки, аудио.</div>
      <div class="card" v-for="f in kbFiles" :key="f.id">
        <div class="row-between">
          <span class="row" style="gap:8px; min-width:0">
            <span class="kb-ico">{{ kbIcon(f) }}</span>
            <span class="grow" style="overflow:hidden; text-overflow:ellipsis; white-space:nowrap" :title="f.name">{{ f.name }}</span>
            <span v-if="f.has_text" class="tag" title="Документ прочитан как текст">текст</span>
          </span>
          <button class="btn-danger" @click="deleteKnowledge(f)" title="Удалить из базы знаний" aria-label="Удалить из базы знаний">🗑</button>
        </div>
      </div>
    </div>
  </div>

  <!-- ===== Модалка участников (добавить/убрать, чат→группа) ===== -->
  <div v-if="membersOpen" class="modal-backdrop" @click.self="membersOpen=false">
    <div class="modal" style="width:440px" role="dialog" aria-modal="true" aria-label="Участники группы">
      <div class="row-between" style="margin-bottom:10px">
        <h3 style="margin:0">{{ currentIsGroup ? 'Участники группы' : 'Добавить персонажа' }}</h3>
        <button class="btn-icon" @click="membersOpen=false" aria-label="Закрыть участников">✕</button>
      </div>
      <p v-if="!currentIsGroup" class="muted" style="margin-top:0">Добавив персонажа, вы превратите этот чат в групповой. Ведущий персонаж останется в группе.</p>

      <!-- Текущие участники (у группы) — с кнопкой убрать -->
      <template v-if="currentIsGroup && currentGroup">
        <p class="muted" style="margin:4px 0">В группе сейчас:</p>
        <div class="card" v-for="m in currentGroup.members" :key="'m'+m.id">
          <div class="row-between">
            <span class="row" style="gap:8px">
              <span class="member-ava"><img v-if="m.avatar_path" :src="m.avatar_path" alt="" /><span v-else>{{ (m.name||'?').charAt(0) }}</span></span>
              <b>{{ m.name }}</b>
            </span>
            <button class="btn-danger" :disabled="currentGroup.members.length <= 1" @click="removeMember(m)" title="Убрать из группы" aria-label="Убрать из группы">Убрать</button>
          </div>
        </div>
        <div class="hr"></div>
      </template>

      <!-- Кого добавить -->
      <p class="muted" style="margin:4px 0">Добавить в чат:</p>
      <div v-if="!availableToAdd().length" class="muted" style="font-size:var(--fs-100)">Все персонажи уже в чате — создайте нового во вкладке слева.</div>
      <div class="card" v-for="c in availableToAdd()" :key="'add'+c.id">
        <label class="check"><input type="checkbox" :checked="memberAddSelected.includes(c.id)" @change="toggleMemberAdd(c.id)" />
          <span class="row" style="gap:8px"><span class="member-ava"><img v-if="c.avatar_path" :src="c.avatar_path" alt="" /><span v-else>{{ (c.name||'?').charAt(0) }}</span></span> {{ c.name }}</span>
        </label>
      </div>
      <button class="btn-primary" style="margin-top:12px" :disabled="!memberAddSelected.length" @click="addMembers">
        {{ currentIsGroup ? 'Добавить выбранных' : 'Создать группу с выбранными' }}
      </button>
    </div>
  </div>

  <!-- ===== Модалка создания группового чата ===== -->
  <div v-if="groupModal" class="modal-backdrop" @click.self="groupModal=false">
    <div class="modal" role="dialog" aria-modal="true" aria-label="Создание группы">
      <div class="row-between" style="margin-bottom:10px">
        <h3 style="margin:0">Новый групповой чат</h3>
        <button class="btn-icon" @click="groupModal=false" aria-label="Закрыть создание группы">✕</button>
      </div>
      <label>Название<input v-model="groupName" /></label>
      <label>Сцена / сеттинг (детально влияет на ролевую — общая обстановка для всех)
        <textarea v-model="groupScenario" rows="3" placeholder="Например: тёмное фэнтези, таверна на окраине, ночь, идёт дождь. Отношения между персонажами напряжённые..."></textarea>
      </label>
      <p class="muted">Выберите персонажей (2+):</p>
      <div class="card" v-for="c in characters" :key="c.id">
        <label class="check"><input type="checkbox" :checked="groupSelectedIds.includes(c.id)" @change="toggleGroupChar(c.id)" /> {{ c.name }}</label>
      </div>
      <label class="check"><input type="checkbox" v-model="groupDirector" /> ИИ-режиссёр (решает, кто ответит; иначе по имени / по кругу)</label>

      <!-- Пригласить друзей в комнату (режим аккаунтов) -->
      <template v-if="authStatus.accounts_enabled">
        <div class="hr"></div>
        <p class="muted" style="margin:0 0 6px">Пригласить друзей в комнату (необязательно):</p>
        <div v-if="!friends.length" class="muted" style="font-size:var(--fs-100)">У вас пока нет друзей — добавьте их во вкладке «Персона».</div>
        <div v-else class="invite-list" style="max-height:170px">
          <label v-for="f in friends" :key="'gi'+f.id" :class="['invite-item', groupInviteSelected.includes(f.username) ? 'active' : '']">
            <input type="checkbox" :checked="groupInviteSelected.includes(f.username)" @change="toggleGroupInvite(f.username)" />
            <span class="avatar">{{ (f.username||'?').charAt(0).toUpperCase() }}</span>
            <span class="grow">{{ f.username }}</span>
          </label>
        </div>
      </template>

      <button class="btn-primary" style="margin-top:12px" @click="createGroup">Создать группу</button>
    </div>
  </div>

  <!-- ===== Профиль / привязка Telegram ===== -->
  <!-- ===== Модалка приглашения друзей в чат ===== -->
  <div v-if="inviteOpen" class="modal-backdrop" @click.self="inviteOpen=false">
    <div class="modal" style="width:420px" role="dialog" aria-modal="true" aria-label="Приглашение в чат">
      <div class="row-between" style="margin-bottom:12px">
        <h3 style="margin:0">Пригласить в чат</h3>
        <button class="btn-icon" @click="inviteOpen=false" aria-label="Закрыть приглашение">✕</button>
      </div>
      <div v-if="!friends.length" class="empty-state">
        <div class="empty-icon">🫂</div>
        <p><b>У вас пока нет друзей</b></p>
        <p class="muted">Добавьте друзей во вкладке «Персона» → «Друзья-ролевики», и сможете приглашать их в чаты.</p>
      </div>
      <div v-else>
        <p class="muted" style="margin-top:0">Кого пригласить читать и участвовать в этом чате:</p>
        <div class="invite-list">
          <label v-for="f in friends" :key="f.id" :class="['invite-item', inviteSelected.includes(f.username) ? 'active' : '']">
            <input type="checkbox" :checked="inviteSelected.includes(f.username)" @change="toggleInvite(f.username)" />
            <span class="avatar">{{ (f.username || '?').charAt(0).toUpperCase() }}</span>
            <span class="grow">{{ f.username }}</span>
          </label>
        </div>
        <div class="row" style="justify-content:flex-end; margin-top:14px; gap:8px">
          <button @click="inviteOpen=false">Отмена</button>
          <button class="btn-primary" :disabled="!inviteSelected.length" @click="submitInvite">Пригласить{{ inviteSelected.length ? ' (' + inviteSelected.length + ')' : '' }}</button>
        </div>
      </div>
    </div>
  </div>

  <div v-if="profileOpen" class="modal-backdrop" @click.self="profileOpen=false">
    <div class="modal" role="dialog" aria-modal="true" aria-label="Профиль">
      <div class="row-between" style="margin-bottom:10px">
        <h3 style="margin:0">Профиль</h3>
        <button class="btn-icon" @click="profileOpen=false" aria-label="Закрыть профиль">✕</button>
      </div>
      <p>Аккаунт: <b>{{ currentUserObj && currentUserObj.username }}</b>
        <span class="tag">{{ currentUserObj && currentUserObj.role }}</span></p>
      <div class="hr"></div>
      <h4>Привязка Telegram</h4>
      <p class="muted">Привяжите Telegram, чтобы бот работал с ВАШИМИ персонажами и чатами.</p>
      <p v-if="currentUserObj && currentUserObj.telegram_id" class="status-ok">Привязан Telegram ID: {{ currentUserObj.telegram_id }}</p>
      <button class="btn-primary" @click="linkTelegram">Получить код привязки</button>
      <div v-if="linkCode" class="card" style="margin-top:8px">
        <p>Отправьте боту команду:</p>
        <p><b style="font-size:18px">/link {{ linkCode }}</b></p>
        <p class="muted">Код действует 10 минут.</p>
      </div>
      <div class="hr"></div>
      <button class="btn-danger" style="width:100%" @click="logout">⎋ Выйти из аккаунта</button>
    </div>
  </div>

  <!-- ===== Отладочный лог LLM ===== -->
  <div v-if="debugOpen" class="modal-backdrop" @click.self="closeDebug">
    <div class="modal" style="width:640px" role="dialog" aria-modal="true" aria-label="Отладка LLM">
      <div class="row-between" style="margin-bottom:8px">
        <h3 style="margin:0">🐞 Отладка LLM</h3>
        <span>
          <!-- Общий системный лог доступен только администратору, и решает это
               сервер: клиент лишь показывает то, что ему разрешено. -->
          <button v-if="debugCanSeeAll" @click="toggleDebugScope"
                  :class="debugAll ? 'btn-primary' : ''"
                  :title="debugAll ? 'Показаны ходы ВСЕХ пользователей' : 'Показаны только ваши ходы'"
                  :aria-pressed="debugAll ? 'true' : 'false'">{{ debugAll ? '🌐 Все' : '👤 Мои' }}</button>
          <button @click="clearDebug">Очистить</button>
          <button class="btn-icon" @click="closeDebug" aria-label="Закрыть отладку">✕</button>
        </span>
      </div>
      <p class="muted">Последние запросы к прокси: модель, что отправлено и что вернулось. Обновляется автоматически.</p>
      <p v-if="!debugEntries.length" class="muted">Пока пусто — отправьте сообщение или сгенерируйте арт.</p>
      <div class="card" v-for="(e, i) in debugEntries" :key="i">
        <div class="row-between">
          <b>{{ e.kind === 'image' ? '🖼' : '💬' }} {{ e.model }}</b>
          <span :class="e.status==='ok' ? 'status-ok' : (e.status==='error' ? 'status-err' : 'muted')">{{ e.ts }} · {{ e.status }}</span>
        </div>
        <div class="muted" style="font-size:12px">{{ e.api_base }}</div>
        <div v-if="e.messages" style="font-size:12px; margin-top:4px; display:flex; gap:4px; flex-wrap:wrap">
          <span v-for="(m, j) in e.messages" :key="j" class="tag">{{ m.role }}: {{ m.content }}</span>
        </div>
        <div v-if="e.prompt" class="muted" style="font-size:12px">prompt: {{ e.prompt }}</div>
        <div v-if="e.error" class="danger-text" style="font-size:12px; white-space:pre-wrap; margin-top:4px">{{ e.error }}</div>
        <div v-else-if="e.preview" class="muted" style="font-size:12px; margin-top:4px">→ {{ e.preview }}</div>
        <div v-if="e.usage" class="muted" style="font-size:12px; margin-top:4px">
          🔢 вход {{ fmtTokens(e.usage.prompt) }}
          <span v-if="e.usage.cached">(из кэша {{ cacheHitPct(e.usage) }}%)</span>
          · ответ {{ fmtTokens(e.usage.completion) }}
          <span v-if="e.usage.reasoning">· размышления {{ fmtTokens(e.usage.reasoning) }}</span>
        </div>
      </div>
    </div>
  </div>

  <!-- ===== Расход токенов ===== -->
  <div v-if="usageOpen" class="modal-backdrop" @click.self="usageOpen=false">
    <div class="modal" style="width:640px" role="dialog" aria-modal="true" aria-label="Расход токенов">
      <div class="row-between" style="margin-bottom:8px">
        <h3 style="margin:0">📊 Расход токенов</h3>
        <span><button @click="loadUsage">Обновить</button> <button class="btn-icon" @click="usageOpen=false" aria-label="Закрыть отчёт о расходе">✕</button></span>
      </div>
      <p class="muted">Сколько токенов реально ушло провайдеру за последние 7 дней. <b>Вход</b> — весь контекст, который мы отправили; <b>из кэша</b> — та его часть, что стоила в разы дешевле; <b>ответ</b> и <b>размышления</b> — вывод, самый дорогой вид токенов.</p>
      <p v-if="!usage" class="muted">Загрузка…</p>
      <template v-else-if="!usage.total.requests">
        <p class="muted">Пока пусто. Учёт начинается с этой версии — отправьте сообщение, и цифры появятся. Если их так и нет, значит ваш прокси не возвращает usage в стриме.</p>
      </template>
      <template v-else>
        <div class="card">
          <div class="row-between"><b>Сегодня</b><span>{{ usage.today.requests }} запр.</span></div>
          <div>вход {{ fmtTokens(usage.today.prompt) }} (из кэша {{ cacheHitPct(usage.today) }}%) · ответ {{ fmtTokens(usage.today.completion) }}<span v-if="usage.today.reasoning"> · размышления {{ fmtTokens(usage.today.reasoning) }}</span></div>
          <div class="hr"></div>
          <div class="row-between"><b>За 7 дней</b><span>{{ usage.total.requests }} запр.</span></div>
          <div>вход {{ fmtTokens(usage.total.prompt) }} (из кэша {{ cacheHitPct(usage.total) }}%) · ответ {{ fmtTokens(usage.total.completion) }}<span v-if="usage.total.reasoning"> · размышления {{ fmtTokens(usage.total.reasoning) }}</span></div>
        </div>

        <h4 style="margin:12px 0 4px">На что уходит</h4>
        <p class="muted" style="margin:0 0 6px; font-size:12px">chat — сам чат; summary — авто-сводка сюжета; director — выбор отвечающего в группе; canvas / image-prompt — служебные.</p>
        <div class="card" v-for="k in usage.by_kind" :key="k.kind">
          <div class="row-between"><b>{{ k.kind }}</b><span class="muted">{{ k.requests }} запр.</span></div>
          <div style="font-size:12px">вход {{ fmtTokens(k.prompt) }} · ответ {{ fmtTokens(k.completion) }}</div>
        </div>

        <h4 style="margin:12px 0 4px">По моделям</h4>
        <div class="card" v-for="m in usage.by_model" :key="m.model">
          <div class="row-between"><b>{{ m.model || '—' }}</b><span class="muted">{{ m.requests }} запр.</span></div>
          <div style="font-size:12px">вход {{ fmtTokens(m.prompt) }} (из кэша {{ cacheHitPct(m) }}%) · ответ {{ fmtTokens(m.completion) }}</div>
        </div>

        <h4 style="margin:12px 0 4px">По дням</h4>
        <div class="card" v-for="d in usage.by_day" :key="d.day">
          <div class="row-between"><b>{{ d.day }}</b><span class="muted">{{ d.requests }} запр.</span></div>
          <div style="font-size:12px">вход {{ fmtTokens(d.prompt) }} · ответ {{ fmtTokens(d.completion) }}</div>
        </div>
      </template>
    </div>
  </div>

  <!-- ===== Всплывающие уведомления (тосты) ===== -->
  <div class="toast-wrap">
    <!-- Тост кликабелен: он переводит в чат, где пришёл ответ. Но это был div
         без роли и без табуляции — с клавиатуры уведомление нельзя было ни
         открыть, ни убрать, оно просто исчезало через восемь секунд. -->
    <div v-for="t in toasts" :key="t.id" class="toast" role="button" tabindex="0"
         @click="toastClick(t)" @keydown.enter="toastClick(t)"
         @keydown.space.prevent="toastClick(t)">{{ t.text }}</div>
  </div>

  <!-- ===== Диалог (подтверждение/ввод) вместо браузерных confirm/prompt ===== -->
  <div v-if="dialog" class="modal-backdrop" @click.self="dialogCancel" @keydown.esc="dialogCancel">
    <div class="modal dialog-modal" role="dialog" aria-modal="true">
      <h3>{{ dialog.title }}</h3>
      <p v-if="dialog.message" class="dialog-msg">{{ dialog.message }}</p>
      <input v-if="dialog.mode==='prompt'" ref="dialogInput" v-model="dialog.value"
             :placeholder="dialog.placeholder" class="dialog-input"
             @keyup.enter="dialogOk" @keyup.esc="dialogCancel" />
      <div class="dialog-actions">
        <button class="btn-ghost" @click="dialogCancel">{{ dialog.cancelText }}</button>
        <button :class="dialog.danger ? 'btn-danger' : 'btn-primary'" @click="dialogOk">{{ dialog.okText }}</button>
      </div>
    </div>
  </div>

  <!-- ===== Инспектор хода =====
       На вопрос «почему персонаж сказал именно это, дошёл ли лорбук, сработала ли
       заметка автора, что срезал бюджет» панель отладки отвечала длинами:
       «system весил 4210 символов». Здесь видно сам ход: блоки, их вес и обрезка.
       Разбор собирает сама сборка контекста, поэтому числа те же, что уйдут в модель.
       На десктопе это правая панель, на узком экране — нижняя шторка. -->
  <div v-if="inspectorOpen" class="modal-backdrop sheet-backdrop" @click.self="closeInspector">
    <div class="modal inspector" role="dialog" aria-modal="true" aria-label="Инспектор хода">
      <span class="sheet-grip" aria-hidden="true"></span>
      <div class="inspector-head">
        <h3>Инспектор хода</h3>
        <button class="btn-icon" @click="loadCtxStats" title="Пересчитать" aria-label="Пересчитать">↻</button>
        <button class="btn-icon" @click="closeInspector" aria-label="Закрыть инспектор хода">✕</button>
      </div>
      <div class="inspector-body">
        <p v-if="ctxBusy" class="muted">Собираю контекст…</p>
        <p v-else-if="!ctxStats" class="muted">Контекст не собирается: у чата нет персонажа.</p>
        <template v-else>
          <div class="ins-total">
            <b>{{ ctxStats.total_tokens.toLocaleString('ru') }}</b> токенов из
            {{ ctxStats.budget.toLocaleString('ru') }} · {{ ctxFill }}% окна
            <span v-if="ctxStats.model" class="tag">{{ ctxStats.model }}</span>
          </div>
          <!-- Три яруса хода — та же разбивка, что в мониторе «Сжатия истории»
               (строки из memTierRows). Старый сервер tiers не присылает: тогда
               блока нет. У группы ярусов нет по сути — говорим об этом. -->
          <template v-if="memTiers">
            <div class="ins-bar" aria-hidden="true">
              <i v-for="r in memTierRows" :key="'it' + r.cls" :class="r.cls" :style="{ width: r.pct + '%' }"></i>
            </div>
            <ul class="mem-tiers">
              <li v-for="r in memTierRows" :key="'il' + r.cls">
                <span class="ins-dot" :class="r.cls" aria-hidden="true"></span>
                <span>{{ r.label }} — {{ fmtNum(r.tokens) }}</span></li>
            </ul>
          </template>
          <p v-else-if="memTiersGroup" class="muted ins-note">В групповых чатах уровни не считаются: память собирается иначе, окна нет.</p>

          <!-- Память хода — несколько строк (insMemoryRows): сколько реплик идёт
               дословно и почему, докуда сжато и кем, что отстало. Подробности —
               ниже, в свёрнутых разделах: раньше всё было раскрыто сразу, и
               главное терялось между фактами и блоками. -->
          <div v-if="insMemoryRows(ctxStats).length" class="ins-mem">
            <h4 class="ins-sub">Память хода</h4>
            <p v-for="(r, i) in insMemoryRows(ctxStats)" :key="'im' + i"
               :class="['ins-mem-row', { 'ins-warn': r.warn, muted: r.muted }]">{{ r.warn ? '⚠ ' : '' }}{{ r.text }}</p>
          </div>

          <details v-if="ctxStats.recalled && ctxStats.recalled.length" class="ins-more">
            <summary>Отобранные факты ({{ ctxStats.recalled.length }})</summary>
            <div v-for="(f, i) in ctxStats.recalled" :key="'rf'+i" class="ins-fact">
              <span class="ins-fact-text">{{ f.content }}</span>
              <span v-if="typeof f.similarity === 'number'" class="tag ins-w"
                    :title="'Сходство с репликой: ' + f.similarity.toFixed(2) + (typeof f.score === 'number' ? ', итоговый вес с учётом свежести: ' + f.score.toFixed(2) : '')">
                {{ f.similarity.toFixed(2) }}</span>
            </div>
          </details>

          <details v-if="ctxStats.horae_total" class="ins-more">
            <summary>Лорбук: сработало {{ ctxStats.horae.length }} из {{ ctxStats.horae_total }}</summary>
            <p v-if="!ctxStats.horae.length" class="muted ins-note">Ни одна запись не сработала на этом ходу.</p>
            <div v-for="(h, i) in ctxStats.horae" :key="'h'+i" class="ins-horae">
              <span class="grow">{{ h.title }}</span>
              <span class="tag">{{ h.always_on ? 'always' : (h.keywords.join(', ') || h.category) }}</span>
              <span class="ins-w">{{ h.tokens }}</span>
            </div>
          </details>

          <!-- Из чего собран ход: блоки с текстом, история, обрезка, хвост. -->
          <details class="ins-more">
            <summary>Из чего собран ход</summary>
            <div class="ins-bar" aria-hidden="true">
              <i v-for="b in ctxStats.blocks" :key="'bar'+b.key" :class="'seg-' + b.key"
                 :style="{ width: (b.tokens / ctxStats.total_tokens * 100) + '%' }"></i>
              <i class="seg-history" :style="{ width: (ctxStats.history.tokens / ctxStats.total_tokens * 100) + '%' }"></i>
              <i class="seg-tail" :style="{ width: (ctxStats.tail_tokens / ctxStats.total_tokens * 100) + '%' }"></i>
            </div>
            <details v-for="b in ctxStats.blocks" :key="b.key" class="ins-block">
              <summary>
                <span class="ins-dot" :class="'seg-' + b.key" aria-hidden="true"></span>
                <span class="grow">{{ b.label }}</span>
                <span class="ins-w">{{ b.tokens }}</span>
              </summary>
              <pre class="ins-text">{{ b.text || '— пусто —' }}</pre>
            </details>
            <div class="ins-block ins-static">
              <span class="ins-dot seg-history" aria-hidden="true"></span>
              <span class="grow">История · {{ ctxStats.history.included }} из {{ ctxStats.history.total }}</span>
              <span class="ins-w">{{ ctxStats.history.tokens }}</span>
            </div>
            <div v-if="ctxStats.history.trimmed" class="ins-block ins-cut">
              <span class="ins-dot" aria-hidden="true"></span>
              <span class="grow">Обрезано бюджетом · {{ ctxStats.history.trimmed }} реплик</span>
              <span class="ins-w">−{{ ctxStats.history.tokens_trimmed }}</span>
            </div>
            <div class="ins-block ins-static">
              <span class="ins-dot seg-tail" aria-hidden="true"></span>
              <span class="grow">Хвост: память, заметка автора, якорь характера</span>
              <span class="ins-w">{{ ctxStats.tail_tokens }}</span>
            </div>
          </details>
        </template>
      </div>
    </div>
  </div>

  <!-- ===== Командная палитра (Ctrl+K) =====
       Один вход к чатам, персонажам, командам и поиску по репликам. Класс .modal
       нужен не для вида, а чтобы палитра попала в ловушку фокуса из волны 2 без
       отдельного кода: _topOverlay ищет именно .drawer, .modal и .lightbox. -->
  <div v-if="paletteOpen" class="modal-backdrop palette-backdrop" @click.self="closePalette">
    <div class="modal palette" role="dialog" aria-modal="true" aria-label="Поиск и команды">
      <input ref="paletteInput" v-model="paletteQuery" class="palette-input"
             placeholder="Чат, персонаж, команда со /, или фраза из переписки…"
             aria-label="Поиск по чатам, персонажам, командам и репликам"
             role="combobox" aria-autocomplete="list" aria-haspopup="listbox"
             aria-controls="palette-list"
             :aria-expanded="paletteItems.length ? 'true' : 'false'"
             :aria-activedescendant="paletteItems.length ? 'palette-option-' + paletteIndex : null"
             @keydown.down.prevent="paletteMove(1)"
             @keydown.up.prevent="paletteMove(-1)"
             @keydown.enter.prevent="paletteRun()" />
      <div class="palette-hint">
        <span>↑↓ — выбор · Enter — открыть · Esc — закрыть</span>
        <span v-if="searchBusy">ищу по репликам…</span>
      </div>
      <!-- Список результатов объявлен списком выбора, а строки — вариантами.
           Раньше стрелки ↑↓ переставляли подсветку молча: скринридер не
           произносил ни новую строку, ни её номер, и вести палитру с клавиатуры
           было нельзя — при том, что подсказка внизу поля предлагает ровно это. -->
      <div class="palette-list" id="palette-list" role="listbox" aria-label="Результаты поиска" v-if="paletteItems.length">
        <button v-for="(it, i) in paletteItems" :key="it.kind + '-' + it.id + '-' + i"
                class="palette-item" :class="{ on: i === paletteIndex }"
                role="option" tabindex="-1" :id="'palette-option-' + i"
                :aria-selected="i === paletteIndex ? 'true' : 'false'"
                @click="paletteRun(it)" @mousemove="paletteIndex = i">
          <span class="palette-kind" aria-hidden="true">{{ it.kind === 'cmd' ? '⌘' : it.kind === 'char' ? '🎭' : it.kind === 'msg' ? '🔍' : '💬' }}</span>
          <span class="palette-text">
            <span class="palette-label">{{ it.label }}</span>
            <span class="palette-sub">{{ it.sub }}</span>
          </span>
        </button>
      </div>
      <div class="palette-empty" v-else>
        {{ paletteQuery.trim().length < 2 ? 'Введите хотя бы два символа' : 'Ничего не найдено' }}
      </div>
    </div>
  </div>

  <!-- ===== Лайтбокс: полноэкранный предпросмотр картинки ===== -->
  <!-- Гашение по клику было единственным способом закрыть картинку: ни роли,
       ни табуляции у слоя не было, и для скринридера полноэкранного просмотра
       просто не существовало. Esc закрывал его и раньше — узнать об этом было
       неоткуда. -->
  <div v-if="lightbox" class="lightbox" role="button" tabindex="0"
       aria-label="Закрыть изображение"
       @click="lightbox=null" @keydown.enter="lightbox=null"
       @keydown.space.prevent="lightbox=null"><img :src="lightbox" alt="Изображение во весь экран" /></div>

  <!-- ===== Плавающий тулбар Канваса: появляется при выделении (как в Notion) ===== -->
  <div v-if="canvasOpen && canvas && canvasSelText && toolbarPos" class="canvas-toolbar"
       :style="{ top: toolbarPos.top + 'px', left: toolbarPos.left + 'px' }" @mousedown.prevent>
    <template v-if="canvas.kind==='code'">
      <button @click="quickAction('Добавь подробные комментарии, объясняющие, что делает код.')" :disabled="canvasBusy" title="Комментарии" aria-label="Комментарии">💬</button>
      <button @click="quickAction('Найди и исправь баги в этом фрагменте.')" :disabled="canvasBusy" title="Найти баги" aria-label="Найти баги">🐞</button>
      <button @click="quickAction('Сделай ревью: улучши читаемость и структуру, не меняя поведение.')" :disabled="canvasBusy" title="Ревью" aria-label="Ревью">🔍</button>
      <button @click="translateCode" :disabled="canvasBusy" title="Перевести на другой язык" aria-label="Перевести на другой язык">🔁</button>
    </template>
    <template v-else>
      <button @click="quickAction('Сократи примерно вдвое, сохранив суть.')" :disabled="canvasBusy" title="Короче" aria-label="Короче">↧</button>
      <button @click="quickAction('Расширь, добавь деталей и примеров.')" :disabled="canvasBusy" title="Подробнее" aria-label="Подробнее">↥</button>
      <button @click="quickAction('Перепиши в строгом профессиональном тоне.')" :disabled="canvasBusy" title="Строже" aria-label="Строже">🎩</button>
      <button @click="quickAction('Перепиши простым языком, понятно для новичка.')" :disabled="canvasBusy" title="Проще" aria-label="Проще">🙂</button>
      <button @click="quickAction('Исправь грамматику, орфографию и пунктуацию, не меняя стиль.')" :disabled="canvasBusy" title="Грамматика" aria-label="Грамматика">✓</button>
    </template>
    <span class="ct-sep"></span>
    <button @click="focusCanvasAi" :disabled="canvasBusy" title="Своя команда для выделенного" aria-label="Своя команда для выделенного">✨</button>
  </div>
  </template>
  `,
})
  // Компоненты «Хроники» из horae.js (подключён раньше app.js). Файл не
  // загрузился — заглушки: без них Vue отрисовал бы <horae-panel> пустым
  // неизвестным тегом, и вкладка молча пустовала бы.
  .component("horae-panel", (window.HoraeUI && window.HoraeUI.components.HoraePanel)
    || { template: '<div><p class="muted">Хроника не загрузилась — обновите страницу.</p>'
      + '<slot name="compress"></slot><slot name="lore"></slot></div>' })
  .component("horae-msg", (window.HoraeUI && window.HoraeUI.components.HoraeMsg) || { render: () => null })
  .mount("#app");
