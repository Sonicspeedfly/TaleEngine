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
      canvasCmdMode: false,             // единый инпут пишет команду открытому Канвасу
      canvasGenMode: false,             // триггер: следующий ответ ИИ уйдёт в Канвас-документ
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

      // --- Параметры генерации (вкладка Generation) ---
      params: {
        model: "",
        temperature: 0.9,
        top_p: 0.95,
        top_k: 40,
        max_tokens: 8192,       // длина ОДНОГО ОТВЕТА (вывод); рассуждения тратят его же
        repetition_penalty: 1.1,
        context_tokens: 1000000, // окно контекста («память»): по умолчанию максимум Gemini
        history_files_mb: 0,     // файлы истории: 0 = ВСЕ пересылаются модели (полная память)
        disable_safety: true,
        send_avatars: false,
        web_access: false,
        reasoning_effort: "",   // "" авто | disable | low | medium | high
        file_reasoning: true,   // авто-включать рассуждения при файлах
      },
      presets: [],
      presetName: "",

      // --- Подключение к LiteLLM (вкладка Connection) ---
      connection: { use_proxy: true, base_url: "http://localhost:4000", api_key: "", default_model: "gpt-4o", image_model: "", image_via_chat: false, fallback_model: "", auto_fallback: true },
      models: [],
      connStatus: "",
      connOk: null,

      // --- Редактор персонажа (вкладка Character) ---
      charEdit: null,

      // --- Память Horae (вкладка Memory) ---
      horae: [],
      // Авто-сводка сюжета: каждые ~12 сообщений ИИ обновляет запись
      // «Сводка сюжета (авто)» — старые события не выпадают из памяти.
      autoSummary: true,
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
      artMode: false, // режим «опиши арт в сообщении» (вместо алерта)

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
      // Аккордеон сайдбара: какие разделы раскрыты (по умолчанию — персонажи и чаты).
      openSections: { characters: true, chats: true, groups: false, shared: false },
      groupDirector: false,
      liveBubbles: [], // живые пузыри разных персонажей при стриминге группы

      // --- Аккаунты ---
      userToken: "",
      currentUserObj: null,
      needAuth: false,        // показывать экран входа/регистрации (режим аккаунтов)
      authTab: "login",       // login | register
      authForm: { username: "", password: "" },
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
      dialog: null,         // модальный диалог (подтверждение/ввод) вместо браузерных alert/prompt/confirm

      // --- Отладочный лог LLM ---
      debugOpen: false,
      debugEntries: [],

      // --- Профиль (привязка Telegram) ---
      profileOpen: false,
      linkCode: "",
    };
  },

  computed: {
    selectedCharacter() {
      return this.characters.find((c) => c.id === this.selectedCharacterId) || null;
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
      if (this.canvasCmdMode) return "Что сделать с Canvas…";
      if (this.canvasGenMode) return "Опишите документ или код для генерации…";
      if (this.artMode) return "Опишите картинку для генерации…";
      return this.isTouch
        ? "Сообщение… (Enter — перенос строки)"
        : "Сообщение… (Enter — отправить, Shift+Enter — перенос)";
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
        throw new Error(detail);
      }
      return res.status === 204 ? null : res.json();
    },

    renderMd(text) {
      // LaTeX: формулы вырезаются ДО markdown-it (иначе он «съедает» \( \[ и **),
      // рендерятся KaTeX'ом и подставляются обратно уже готовым HTML.
      const math = [];
      const protectedText = this._extractMath(text || "", math);
      let html = md.render(protectedText);
      if (math.length) {
        html = html.replace(/%%MATH-(\d+)%%/g, (_, i) => math[+i] || "");
      }
      // ADD_DATA_URI_TAGS: разрешаем <img src="data:..."> (сгенерированные арты).
      return DOMPurify.sanitize(html, { ADD_DATA_URI_TAGS: ["img"] });
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
    async createCharacter() {
      const name = await this.askPrompt("Имя нового персонажа", { placeholder: "Например: Алиса" });
      if (!name) return;
      await this.api("/characters", {
        method: "POST",
        body: JSON.stringify({ name, first_message: "", system_prompt: "" }),
      });
      await this.loadCharacters();
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
    },
    async selectCharacter(c) {
      this.selectedCharacterId = c.id;
      this.charEdit = { ...c, generation_params: c.generation_params || {} };
      await this.loadSessions();
      // Автоматически открываем последний чат или создаём новый.
      if (this.sessions.length) this.openSession(this.sessions[0]);
      else await this.newChat();
    },
    async saveCharacter() {
      const c = this.charEdit;
      await this.api("/characters/" + c.id, {
        method: "PATCH",
        body: JSON.stringify({
          name: c.name, description: c.description, personality: c.personality,
          scenario: c.scenario, first_message: c.first_message,
          system_prompt: c.system_prompt, model: c.model, avatar_path: c.avatar_path,
        }),
      });
      await this.loadCharacters();
    },
    async deleteCharacter(c) {
      if (!(await this.askConfirm("Удалить персонажа «" + c.name + "»?", { okText: "Удалить" }))) return;
      await this.api("/characters/" + c.id, { method: "DELETE" });
      if (this.selectedCharacterId === c.id) { this.selectedCharacterId = null; this.sessionId = null; this.messages = []; }
      await this.loadCharacters();
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
      } finally { this.canvasGenerating = false; }
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
      } finally { this.canvasBusy = false; }
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
      this.canvasCmdMode = true;
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
    closeCanvas() { this.saveCanvas(); this.canvasOpen = false; this.mobilePane = "chat"; this.canvasCmdMode = false; },

    // ---------- Сессии (чаты) ----------
    async loadSessions() {
      this.sessions = await this.api("/sessions?character_id=" + this.selectedCharacterId);
    },
    async newChat() {
      const r = await this.api("/sessions?character_id=" + this.selectedCharacterId, { method: "POST" });
      await this.loadSessions();
      this.openSession({ id: r.session_id });
    },
    async openSession(s) {
      if (s.id === this.sessionId && !this.sharedView) return; // уже открыт
      this._handoffStreaming();  // текущую генерацию (если есть) доигрываем в фоне
      this.sharedView = null;   // это мой собственный чат, а не «чужой»
      this.sessionId = s.id;
      this._clearPending(s.id); // открыли чат — снимаем метку «пришёл ответ»
      this.authorNote = s.author_note || "";
      this.sessionPersonaId = s.persona_id || null;
      this.sessionBg = s.background || "";
      // Часовой пояс чата: из сессии; для групп (открываются как {id}) — из списка групп.
      this.sessionTimezone = s.timezone || "";
      if (!this.sessionTimezone) {
        const g = this.groups.find((x) => x.id === s.id);
        this.sessionTimezone = (g && g.timezone) || "";
      }
      // Пояс ещё не задан — определяем по браузеру и сохраняем за этим чатом.
      // Пользователь может сменить его во вкладке «Персона» (настройка на чат).
      if (!this.sessionTimezone) this._autoTimezone();
      this.closeSidebarOnMobile(); // на мобильном прячем сайдбар после выбора
      await this.loadMessages(true);   // свежее открытие — грузим последнюю порцию
      this.connectWs();
    },
    // Определить часовой пояс по браузеру и тихо сохранить его за текущим чатом.
    _autoTimezone() {
      let tz = "";
      try { tz = Intl.DateTimeFormat().resolvedOptions().timeZone || ""; } catch (e) {}
      if (!tz || !this.sessionId) return;
      this.sessionTimezone = tz;
      this.api("/sessions/" + this.sessionId, {
        method: "PATCH", body: JSON.stringify({ timezone: tz }),
      }).catch(() => {});
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
      const s = this.sessions.find((x) => x.id === sid);
      if (s) return this.openSession(s);
      const g = this.groups.find((x) => x.id === sid);
      if (g) return this.openSession({ id: g.id });
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
      await this.loadSessions();
      await this.loadGroups();
    },
    async renameSession(s) {
      const title = await this.askPrompt("Новое название чата", { value: s.title, placeholder: "Название чата" });
      if (!title) return;
      await this.api("/sessions/" + s.id, { method: "PATCH", body: JSON.stringify({ title }) });
      await this.loadSessions();
      await this.loadGroups();
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
      await this.loadGroups();
      this.openSession({ id: r.session_id });
    },
    async toggleDirector() {
      if (!this.currentGroup) return;
      const val = !this.currentGroup.director;
      await this.api("/sessions/" + this.sessionId, { method: "PATCH", body: JSON.stringify({ director: val }) });
      await this.loadGroups();
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
    },

    // ---------- WebSocket стриминг ----------
    connectWs() {
      if (this.ws) {
        // Мы сами закрываем сокет (смена чата) — это не обрыв сети, не надо
        // дослушивать старую генерацию в активный (уже другой) чат.
        this._intentionalClose = true;
        try { this.ws.close(); } catch (e) {}
      }
      if (this._wsReconnectTimer) { clearTimeout(this._wsReconnectTimer); this._wsReconnectTimer = null; }
      const sid = this.sessionId; // для реконнекта: переподключаемся только к ЭТОМУ чату
      const proto = location.protocol === "https:" ? "wss" : "ws";
      const qs = [];
      if (this.accessCode) qs.push("code=" + encodeURIComponent(this.accessCode));
      if (this.userToken) qs.push("token=" + encodeURIComponent(this.userToken));
      const q = qs.length ? "?" + qs.join("&") : "";
      this.ws = new WebSocket(proto + "://" + location.host + "/ws/chat/" + this.sessionId + q);
      this.ws.onopen = () => { this.connected = true; this._wsRetry = 0; };
      this.ws.onclose = () => {
        this.connected = false;
        if (this._intentionalClose) { this._intentionalClose = false; return; }
        // Непреднамеренный обрыв: дослушиваем активную генерацию через SSE.
        if (this.streaming && this.currentJobId) this.resumeSSE(this.currentJobId);
        // Автопереподключение с бэкоффом: сеть моргнула или сервер перезапустился.
        // Без этого после обрыва connected=false навсегда и отправка блокируется.
        const delay = Math.min(15000, 1500 * Math.pow(2, this._wsRetry || 0));
        this._wsRetry = (this._wsRetry || 0) + 1;
        this._wsReconnectTimer = setTimeout(() => {
          this._wsReconnectTimer = null;
          if (this.sessionId === sid) this.connectWs();
        }, delay);
      };
      this.ws.onmessage = (e) => this.onWsEvent(JSON.parse(e.data));
    },
    onWsEvent(ev) {
      this._lastEvtAt = Date.now(); // метка для сторожа зависшего стриминга
      // Нейросеть подала признаки жизни — плашка «обрабатывает файл» больше не нужна.
      if (ev.type === "token" || ev.type === "thought" || ev.type === "done" || ev.type === "error") {
        this.processingNote = false;
      }
      if (ev.type === "job") this.currentJobId = ev.job_id;
      else if (ev.type === "speaker") {
        // Групповой чат: начинается реплика нового персонажа.
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
      // Сервер — источник истины: перечитываем сообщения (там уже новый ответ/свайп).
      await this.loadMessages();
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
      if (this.canvasCmdMode) this.applyCanvasCmd();
      else if (this.artMode) this.sendArt();
      else this.send();
    },
    // Единый инпут в режиме команды Канвасу: применяем введённое как инструкцию ИИ
    // (к выделенному фрагменту, если он есть, иначе ко всему документу).
    async applyCanvasCmd() {
      const cmd = this.input.trim();
      if (!cmd || !this.canvas) return;
      this.input = "";
      this.resetComposerHeight();
      this.canvasCmdMode = false;
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
      if (this.canvasGenMode) {
        const atts = this._cleanAtts(this.pendingAttachments);
        this.input = ""; this.pendingAttachments = []; this.canvasGenMode = false; this.resetComposerHeight();
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
    },
    async deleteMessage(msg) {
      if (!(await this.askConfirm("Удалить сообщение?", { okText: "Удалить" }))) return;
      await this.api("/messages/" + msg.id, { method: "DELETE" });
      this.loadMessages();
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
        this.artMode = true;
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
      this.artMode = false;
      this.resetComposerHeight();
      try {
        // Фото-референсы могут быть тяжёлыми — грузим с прогрессом.
        await this._postWithProgress("/sessions/" + this.sessionId + "/image",
          { prompt: desc, mode: "prompt", attachments });
        await this.loadMessages();
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
      if (this.sessionId) {
        await this.api("/sessions/" + this.sessionId, {
          method: "PATCH",
          body: JSON.stringify({ background: value }),
        });
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
      await this.loadSessions();
      await this.openSession({ id: data.session_id });
      await this.loadHorae();
      // Явно сообщаем, что импортировалось, в т.ч. подхватилась ли память Horae.
      let msg = (data.native ? "Чат AiChat импортирован. " : "") + "Сообщений: " + data.count + ".";
      if (data.native) {
        if (data.horae_saved) {
          msg += "\n🧠 Память Horae восстановлена (записей: " + data.horae_saved + ").";
          this.drawerTab = "memory";
        }
      } else if (data.horae_saved) {
        msg += "\n🧠 Память Horae подхвачена: снимок состояния сохранён как always_on-запись этого чата (вкладка «Память Horae»).";
        this.drawerTab = "memory"; // сразу показываем, что сохранилось
      } else {
        msg += "\nДанных Horae в файле не найдено — снимок состояния не сохранён.";
      }
      this.showToast(msg);
    },

    // ---------- Сохранение настроек интерфейса в системе (БД) ----------
    async loadUiPrefs(applyParams = true) {
      const ui = await this.api("/settings/ui");
      if (applyParams && ui && ui.params) this.params = { ...this.params, ...ui.params };
      // Мягкая миграция старых сохранённых настроек: прежний дефолт max_tokens=1024
      // резал ответы (особенно с рассуждениями), а окно контекста было 64000
      // (прошлый дефолт) — теперь по умолчанию максимум Gemini (1 млн).
      if (applyParams) {
        if (!this.params.max_tokens || this.params.max_tokens <= 1024) this.params.max_tokens = 8192;
        if (!this.params.context_tokens || this.params.context_tokens === 64000) this.params.context_tokens = 1000000;
      }
      if (ui && Number(ui.message_preload) > 0) this.messagePreload = Number(ui.message_preload);
      if (ui && "auto_summary" in ui) this.autoSummary = ui.auto_summary !== false;
    },
    saveUiPrefs() {
      // Дебаунс, чтобы не дёргать сервер на каждое движение ползунка.
      clearTimeout(this._uiSaveTimer);
      this._uiSaveTimer = setTimeout(() => {
        this.api("/settings/ui", {
          method: "PUT",
          body: JSON.stringify({
            params: this.params,
            message_preload: this.msgPageSize,
            auto_summary: this.autoSummary,
          }),
        }).catch(() => {});
      }, 600);
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

    // ---------- Память Horae ----------
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
    async applySessionMeta() {
      if (!this.sessionId) return;
      await this.api("/sessions/" + this.sessionId, {
        method: "PATCH",
        body: JSON.stringify({
          author_note: this.authorNote,
          persona_id: this.sessionPersonaId,
          timezone: this.sessionTimezone || "",
        }),
      });
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
    async submitAuth() {
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
      try { this.debugEntries = await this.api("/debug/log"); } catch (e) {}
    },
    async clearDebug() { await this.api("/debug/log", { method: "DELETE" }); this.debugEntries = []; },

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
      await this.loadConnection();
      await this.loadPresets();
      const def = this.presets.find((p) => p.is_default);
      // Дефолт-пресет задаёт params; message_preload подтягиваем в любом случае.
      if (def) { this.applyPreset(def); await this.loadUiPrefs(false); }
      else await this.loadUiPrefs();
      await Promise.all([this.loadCharacters(), this.loadPersonas(), this.loadHorae(), this.loadGroups(), this.loadFriends()]);
      if (this.characters.length) this.selectCharacter(this.characters[0]);
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
      if (!this._escBound) {
        this._escBound = true;
        window.addEventListener("keydown", (e) => {
          if (e.key !== "Escape" || e.defaultPrevented) return;
          if (this.dialog) { this.dialogCancel(); return; }
          if (this.lightbox) { this.lightbox = null; return; }
          if (this.headerMenu) { this.headerMenu = false; return; }
          if (this.plusMenu) { this.plusMenu = false; return; }
          if (this.notifOpen) { this.notifOpen = false; return; }
          if (this.inviteOpen) { this.inviteOpen = false; return; }
          if (this.groupModal) { this.groupModal = false; return; }
          if (this.profileOpen) { this.profileOpen = false; return; }
          if (this.debugOpen) { this.debugOpen = false; return; }
          if (this.adminOpen) { this.adminOpen = false; return; }
          if (this.drawerTab) { this.drawerTab = null; }
        });
      }
    },
  },

  watch: {
    // Любое изменение параметров генерации сохраняем в системе (с дебаунсом).
    params: { handler() { this.saveUiPrefs(); }, deep: true },
    soundOn(v) { localStorage.setItem("soundOn", v ? "1" : "0"); },
  },

  async mounted() {
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
      <div class="row" style="gap:6px; margin-bottom:8px">
        <button :class="authTab==='login'?'btn-primary':''" @click="authTab='login'" style="flex:1">Вход</button>
        <button :class="authTab==='register'?'btn-primary':''" @click="authTab='register'" style="flex:1">Регистрация</button>
      </div>
      <input v-model="authForm.username" placeholder="Логин" />
      <input v-model="authForm.password" type="password" placeholder="Пароль" @keyup.enter="submitAuth" style="margin-top:6px" />
      <p v-if="accessError" class="status-err">{{ accessError }}</p>
      <button class="btn-primary" style="width:100%; margin-top:8px" @click="submitAuth">
        {{ authTab==='register' ? 'Зарегистрироваться' : 'Войти' }}
      </button>
    </div>
  </div>

  <div v-else-if="needAccess" class="gate">
    <div class="gate-box">
      <h2>TaleEngine</h2>
      <p class="muted">Приложение защищено кодом доступа.</p>
      <input v-model="accessInput" type="password" placeholder="Код доступа" @keyup.enter="submitAccess" />
      <p v-if="accessError" class="status-err">{{ accessError }}</p>
      <button class="btn-primary" style="width:100%; margin-top:8px" @click="submitAccess">Войти</button>
    </div>
  </div>

  <template v-else>
  <div :class="['app-grid', !sidebarOpen ? 'sb-hidden' : '', canvasOpen ? 'with-canvas' : '', 'pane-' + mobilePane]">

    <!-- Затемнение под мобильным сайдбаром -->
    <div v-if="sidebarOpen" class="backdrop" @click="sidebarOpen=false"></div>

    <!-- ===== Левый сайдбар: разделы-аккордеоны ===== -->
    <div :class="['sidebar', sidebarOpen ? 'open' : '']">

      <!-- Раздел: Персонажи -->
      <div class="acc">
        <button class="acc-head" @click="toggleSection('characters')">
          <span class="acc-icon">🎭</span><span class="acc-title">Персонажи</span>
          <span class="acc-chevron" :class="{ open: openSections.characters }">▸</span>
        </button>
        <div class="acc-body" :class="{ open: openSections.characters }">
          <div class="row" style="padding: 6px 12px; gap:6px">
            <button class="btn-primary" style="flex:1" @click="createCharacter">+ Новый</button>
            <label class="btn-icon" style="margin:0; cursor:pointer" title="Импорт персонажа PNG/JSON">
              📥<input type="file" accept=".png,.json" style="display:none" @change="importCharacter" />
            </label>
            <label class="btn-icon" style="margin:0; cursor:pointer" title="Импорт чата: нативный AiChat (.aichat.json) или SillyTavern (.jsonl)">
              💬<input type="file" accept=".jsonl,.json" style="display:none" @change="importChat" />
            </label>
          </div>
          <div v-for="c in characters" :key="c.id"
               :class="['list-item', c.id === selectedCharacterId ? 'active' : '']"
               @click="selectCharacter(c)">
            <div class="avatar"><img v-if="c.avatar_path" :src="c.avatar_path" class="avatar" />{{ c.avatar_path ? '' : c.name.charAt(0) }}</div>
            <div class="grow">{{ c.name }}</div>
            <button class="btn-icon" @click.stop="exportCharacter(c)" title="Экспорт (JSON + лор Horae)">⬇</button>
            <button class="btn-icon" @click.stop="deleteCharacter(c)" title="Удалить">🗑</button>
          </div>
        </div>
      </div>

      <!-- Раздел: Чаты выбранного персонажа -->
      <div class="acc" v-if="selectedCharacter">
        <button class="acc-head" @click="toggleSection('chats')">
          <span class="acc-icon">💬</span><span class="acc-title">Чаты — {{ selectedCharacter.name }}</span>
          <span class="acc-chevron" :class="{ open: openSections.chats }">▸</span>
        </button>
        <div class="acc-body" :class="{ open: openSections.chats }">
          <div style="padding: 6px 12px"><button @click="newChat" style="width:100%">+ Новый чат</button></div>
          <div v-for="s in sessions" :key="s.id"
               :class="['list-item', s.id === sessionId ? 'active' : '']"
               :title="s.title + ' — чат #' + s.id"
               @click="openSession(s)">
            <span v-if="pendingChats.includes(s.id)" class="reply-dot" title="Пришёл новый ответ"></span>
            <div class="grow">{{ s.title }} <span class="muted">#{{ s.id }}</span></div>
            <button class="btn-icon" @click.stop="exportSession(s)" title="Экспорт чата (нативный формат AiChat)">💾</button>
            <button v-if="authStatus.accounts_enabled" class="btn-icon" @click.stop="openInvite(s)" title="Пригласить друга">🔗</button>
            <button class="btn-icon" @click.stop="renameSession(s)" title="Переименовать">✎</button>
            <button class="btn-icon" @click.stop="deleteSession(s)" title="Удалить чат">🗑</button>
          </div>
        </div>
      </div>

      <!-- Раздел: Группы -->
      <div class="acc">
        <button class="acc-head" @click="toggleSection('groups')">
          <span class="acc-icon">👥</span><span class="acc-title">Группы</span>
          <span class="acc-chevron" :class="{ open: openSections.groups }">▸</span>
        </button>
        <div class="acc-body" :class="{ open: openSections.groups }">
          <div style="padding: 6px 12px"><button @click="openGroupModal" style="width:100%">+ Группа</button></div>
          <div v-for="g in groups" :key="g.id"
               :class="['list-item', g.id === sessionId ? 'active' : '']"
               :title="g.title + ' — чат #' + g.id"
               @click="openSession({ id: g.id })">
            <span v-if="pendingChats.includes(g.id)" class="reply-dot" title="Пришёл новый ответ"></span>
            <div class="grow">{{ g.title }}
              <span class="muted">{{ g.members.map(m => m.name).join(', ') }}</span>
            </div>
            <button class="btn-icon" @click.stop="exportSession(g)" title="Экспорт чата (нативный формат AiChat)">💾</button>
            <button v-if="authStatus.accounts_enabled" class="btn-icon" @click.stop="openInvite(g)" title="Пригласить друга в группу">🔗</button>
            <button class="btn-icon" @click.stop="renameSession(g)" title="Переименовать">✎</button>
            <button class="btn-icon" @click.stop="deleteSession(g)" title="Удалить группу">🗑</button>
          </div>
        </div>
      </div>

      <!-- Раздел: Доступные мне (расшаренные чаты) -->
      <div class="acc" v-if="authStatus.accounts_enabled && sharedSessions.length">
        <button class="acc-head" @click="toggleSection('shared')">
          <span class="acc-icon">👁</span><span class="acc-title">Доступные мне</span>
          <span class="acc-chevron" :class="{ open: openSections.shared }">▸</span>
        </button>
        <div class="acc-body" :class="{ open: openSections.shared }">
          <div v-for="s in sharedSessions" :key="'sh'+s.id"
               :class="['list-item', s.id === sessionId ? 'active' : '']"
               @click="openSharedSession(s)">
            <div class="grow">{{ s.title }}
              <span class="muted">{{ s.character_name }} · от {{ s.owner }}</span>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- ===== Центр: чат ===== -->
    <div class="chat" @dragover.prevent="dragOver = !!sessionId" @dragleave.prevent="dragOver=false" @drop.prevent="onDrop">
      <div v-if="dragOver" class="drop-overlay">📎 Отпустите файл — он прикрепится к сообщению</div>
      <div class="chat-header">
        <button class="btn-icon hamburger" @click="sidebarOpen=!sidebarOpen" title="Меню">☰</button>
        <button v-if="canvasOpen" class="btn-icon only-mobile" @click="mobilePane='canvas'" title="Открыть Canvas">📋</button>
        <!-- Аватарка персонажа (для группы — первого участника с аватаркой) -->
        <span v-if="sessionId" class="header-ava">
          <img v-if="headerAvatar" :src="headerAvatar" />
          <span v-else>{{ currentIsGroup ? '👥' : (currentSessionTitle || 'T').charAt(0).toUpperCase() }}</span>
        </span>
        <span class="title" :title="sessionId ? (currentSessionTitle + ' — чат #' + sessionId) : ''">{{ sharedView ? ('🔗 ' + sharedView.title) : (currentIsGroup ? currentGroup.title : (selectedCharacter ? selectedCharacter.name : 'TaleEngine')) }}</span>
        <!-- Полное имя чата и его номер: по нему удобно ссылаться на конкретный чат -->
        <span v-if="sessionId" class="pill chat-id-pill" :title="currentSessionTitle + ' — чат #' + sessionId">💬 {{ currentSessionTitle || 'чат' }} · #{{ sessionId }}</span>
        <span v-if="currentIsGroup" class="pill hide-mobile" title="Участники группы">👥 {{ currentGroup.members.map(m => m.name).join(', ') }}</span>
        <span :class="['pill', connected ? 'status-ok' : 'status-err']"
              :title="connected ? 'Соединение с сервером активно' : 'Переподключение…'">{{ connected ? 'online' : '⟳ реконнект' }}</span>
        <span class="pill hide-mobile">{{ params.model || connection.default_model }}</span>
        <span v-if="connection.use_proxy" class="pill hide-mobile" title="Запросы идут в LiteLLM-прокси">proxy {{ connection.base_url }}</span>
        <div style="flex:1"></div>
        <button v-if="currentIsGroup" class="btn-icon hide-mobile" :class="currentGroup.director ? 'rec-active' : ''"
                @click="toggleDirector" title="ИИ-режиссёр: решает, кто ответит">🎬</button>
        <!-- Колокольчик уведомлений: входящие заявки в друзья -->
        <div v-if="currentUserObj" class="notif-wrap">
          <button class="btn-icon" @click="notifOpen=!notifOpen" title="Уведомления">
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
        <button class="btn-icon hide-mobile" @click="soundOn=!soundOn" :title="soundOn ? 'Звук вкл' : 'Звук выкл'">{{ soundOn ? '🔊' : '🔇' }}</button>
        <button v-if="currentUserObj" class="btn-icon hide-mobile" @click="openProfile" title="Профиль / привязка Telegram">👤<span class="hide-mobile"> {{ currentUserObj.username }}</span></button>
        <button v-if="sessionId && authStatus.accounts_enabled && !sharedView" class="btn-icon hide-mobile" @click="openInvite({ id: sessionId })" title="Пригласить друга в этот чат">👥</button>
        <button v-if="sessionId" class="btn-icon hide-mobile" @click="bgPicker=!bgPicker" title="Фон чата">🖼</button>
        <button class="btn-icon hide-mobile" @click="openDebug" title="Отладка LLM (что уходит в прокси)">🐞</button>
        <button v-if="isAdmin" class="btn-icon hide-mobile" @click="openAdmin" title="Администрирование">🛡</button>
        <button class="btn-icon" @click="drawerTab = drawerTab ? null : 'generation'" title="Настройки">⚙</button>
        <!-- На мобильном вторичные действия шапки прячем в это меню (⋯) -->
        <div class="header-menu-wrap only-mobile">
          <button class="btn-icon" @click="headerMenu=!headerMenu" title="Ещё">⋯</button>
          <div v-if="headerMenu" class="plus-backdrop" @click="headerMenu=false"></div>
          <div v-if="headerMenu" class="plus-menu header-menu">
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
                  @click="setBackground(b.value)" :title="b.name">{{ b.value ? '' : '∅' }}</button>
          <label class="btn" style="margin:0;cursor:pointer">Загрузить фото
            <input type="file" accept="image/*" style="display:none" @change="uploadBackground" />
          </label>
        </div>
        <div v-if="chatImages.length" class="bg-row">
          <span class="muted" style="align-self:center">Из чата:</span>
          <img v-for="(u,i) in chatImages" :key="i" :src="u" class="bg-thumb" @click="setBackground(u)" />
        </div>
      </div>

      <div class="messages" ref="messages" :style="chatBgStyle" @scroll="onMessagesScroll">
        <div v-if="!sessionId" class="empty">Выберите или создайте персонажа и чат слева.</div>
        <!-- Индикатор подгрузки истории при скролле вверх -->
        <div v-if="sessionId && loadingOlder" class="load-older">⏳ Загружаю ранние сообщения…</div>
        <div v-else-if="sessionId && messages.length >= msgPageSize && noMoreMessages" class="load-older muted">— начало чата —</div>

        <div v-for="m in messages" :key="m.id" :class="['msg', m.role]">
          <!-- Аватарка: персонаж/участник группы у ответа, персона/профиль у пользователя -->
          <div v-if="m.role === 'user' || m.role === 'assistant'" class="msg-ava"
               :title="m.role === 'assistant' ? (m.speaker_name || (selectedCharacter && selectedCharacter.name) || '') : ''">
            <img v-if="msgAvatar(m)" :src="msgAvatar(m)" loading="lazy" />
            <span v-else>{{ msgAvatarLetter(m) }}</span>
          </div>
          <div class="msg-body">
          <div v-if="m.speaker_name" class="speaker">{{ m.speaker_name }}</div>
          <div v-if="m.reply_to_id" class="reply-quote">↪ {{ quoteOf(m.reply_to_id) }}</div>
          <div class="bubble">
            <!-- режим редактирования: авто-фокус, авто-высота, Ctrl+Enter / Esc -->
            <div v-if="editingId === m.id" class="edit-box">
              <textarea ref="editArea" v-model="editingText" class="edit-area"
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
              <div v-if="m.content" v-html="renderMd(m.content)" style="margin-bottom:8px"></div>
              <div class="doc-card" @click="openCanvas(m.canvas_id)" title="Открыть в Canvas">
                <span class="doc-card-icon">{{ m.canvas_kind === 'code' ? '💻' : '📄' }}</span>
                <span class="doc-card-body">
                  <span class="doc-card-title">{{ m.canvas_title || 'Документ' }}</span>
                  <span class="doc-card-hint">Открыть в Canvas →</span>
                </span>
              </div>
            </div>
            <!-- обычный режим: markdown; двойной клик — быстрое редактирование -->
            <div v-else v-html="renderMd(m.content)" @dblclick="startEdit(m)"></div>
            <!-- предпросмотр вложений сообщения -->
            <div v-if="m.attachments && m.attachments.length" class="attachments">
              <template v-for="(a, ai) in m.attachments" :key="ai">
                <!-- a.data есть только у своего свежеотправленного (оптимистичного) сообщения;
                     у загруженных из БД — тянем лениво по attUrl (кэшируется браузером). -->
                <img v-if="a.type==='image'" :src="a.data || a.preview || attUrl(m, ai)" loading="lazy" class="att-img" @click="lightbox = a.data || a.preview || attUrl(m, ai)" title="Открыть" />
                <audio v-else-if="a.type==='audio'" :src="a.data || a.preview || attUrl(m, ai)" controls preload="none" class="att-audio"></audio>
                <video v-else-if="a.type==='video' || ((a.mime || '').startsWith('video'))" :src="a.data || a.preview || attUrl(m, ai)" controls preload="metadata" class="att-video"></video>
                <a v-else class="att-doc" :href="a.data || a.preview || attUrl(m, ai)" :download="a.name || 'файл'" title="Скачать">📄 {{ a.name || 'документ' }}</a>
              </template>
            </div>
          </div>

          <div class="msg-meta">
            <!-- свайпы только у ассистента и если их больше одного / это последний ответ -->
            <span v-if="m.role === 'assistant' && !m.canvas_id" class="swipes">
              <button class="btn-icon" @click="swipe(m, -1)" :disabled="m.active_swipe === 0">◀</button>
              {{ m.active_swipe + 1 }}/{{ (m.swipes || [m.content]).length }}
              <button class="btn-icon" @click="swipe(m, 1)" :title="m.id === lastAssistantId ? 'Ещё вариант' : ''">▶</button>
            </span>
            <!-- Время: у user — когда отправил, у assistant — когда пришёл ответ (в поясе чата) -->
            <span v-if="m.created_at" class="tag msg-time" :title="fmtWhenFull(m.created_at)">🕒 {{ fmtWhen(m.created_at) }}</span>
            <span v-if="m.model_used" class="tag">{{ m.model_used }}</span>
            <button v-if="!m.canvas_id" class="btn-icon" @click="copyMessage(m)" title="Скопировать текст">📋</button>
            <template v-if="!m.canvas_id">
              <button class="btn-icon" @click="replyTo(m)" title="Ответить на это сообщение">↩</button>
              <button class="btn-icon" @click="startEdit(m)" title="Редактировать">✎</button>
              <button v-if="m.role === 'assistant' && m.id === lastAssistantId" class="btn-icon" @click="regenerate" title="Перегенерировать">↻</button>
              <button v-if="m.role === 'assistant' && m.id === lastAssistantId" class="btn-icon" @click="continueReply" title="Продолжить">⏩</button>
              <button class="btn-icon" @click="artFromMessage(m)" title="Нарисовать по этому сообщению">🎨</button>
            </template>
            <button class="btn-icon" @click="deleteMessage(m)" title="Удалить">🗑</button>
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
            <img v-if="memberAvatar(b.name)" :src="memberAvatar(b.name)" />
            <span v-else>{{ (b.name || '?').charAt(0).toUpperCase() }}</span>
          </div>
          <div class="msg-body">
            <div class="speaker">{{ b.name }}</div>
            <div class="bubble"><div v-html="renderMd(b.content)"></div><span class="typing">▌</span></div>
          </div>
        </div>
        <!-- стриминг: одиночный ответ -->
        <div v-if="streaming && !liveBubbles.length" class="msg assistant">
          <div class="msg-ava">
            <img v-if="msgAvatar({ role: 'assistant' })" :src="msgAvatar({ role: 'assistant' })" />
            <span v-else>{{ msgAvatarLetter({ role: 'assistant' }) }}</span>
          </div>
          <div class="msg-body">
            <div class="bubble">
              <div v-html="renderMd(currentReply)"></div>
              <span class="typing">▌</span>
            </div>
          </div>
        </div>
        <!-- генерация документа в Canvas (нестриминговая) -->
        <div v-if="canvasGenerating" class="msg assistant">
          <div class="bubble">📄 Генерирую документ для Canvas… <span class="typing">▌</span></div>
        </div>
      </div>

      <div class="composer" v-if="sessionId">
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
        <!-- Индикатор режима арта (вместо алерта): пишите описание сообщением -->
        <div v-if="artMode" class="art-indicator">
          🎨 Опишите картинку сообщением (можно прикрепить фото 📎), затем «Сгенерировать».
          <a href="#" @click.prevent="artMode=false">отмена</a>
        </div>
        <!-- Индикатор: команда уходит в открытый Канвас (а не сообщением в чат) -->
        <div v-if="canvasCmdMode" class="art-indicator">
          ✨ Команда для Canvas {{ canvasSelText ? '(выделенный фрагмент)' : '(весь документ)' }}
          <a href="#" @click.prevent="canvasCmdMode=false">отмена</a>
        </div>
        <!-- Индикатор: триггер «Документ» — ответ ИИ откроется в Canvas -->
        <div v-if="canvasGenMode" class="art-indicator">
          📄 Опишите документ/код — ответ придёт плашкой и откроется в Canvas.
          <a href="#" @click.prevent="canvasGenMode=false">отмена</a>
        </div>
        <!-- Подсказка о маршрутизации, когда Канвас открыт (умный редактор, а не генератор файлов) -->
        <div v-if="canvasOpen && canvas && !canvasGenMode && !canvasCmdMode && !artMode" class="art-indicator route-hint">
          📝 Открыт «{{ canvas.title || 'без названия' }}»: правки («исправь…», «сделай длиннее») меняют его; «напиши новый…» создаст отдельный.
        </div>
        <div class="chips" v-if="pendingAttachments.length">
          <span class="chip att-chip" :class="{ 'att-loading': a.loading, 'att-error': a.error }"
                v-for="(a, i) in pendingAttachments" :key="a.id">
            <span v-if="a.loading" class="att-state">⏳</span>
            <span v-else-if="a.error" class="att-state">⚠</span>
            <template v-else>
              <img v-if="a.type==='image'" :src="a.data || a.preview" class="att-thumb" @click="lightbox=a.data || a.preview" title="Открыть" />
              <audio v-else-if="a.type==='audio'" :src="a.data" controls class="att-audio-sm"></audio>
              <span v-else-if="a.type==='video'" class="att-state">🎬</span>
              <span v-else class="att-state">📄</span>
            </template>
            <span class="att-name" v-if="a.loading || a.error || a.type!=='image'">
              {{ a.error ? 'ошибка' : (a.type==='audio' ? '🎤 голос' : a.name) }}<i v-if="a.size"> · {{ fmtSize(a.size) }}</i>
            </span>
            <a href="#" class="att-x" @click.prevent="removeAttachment(i)" title="Убрать">✕</a>
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
        <div class="row">
          <!-- [+] второстепенные действия: документ, арт -->
          <div class="plus-wrap">
            <button class="btn-icon" :class="(artMode || canvasGenMode) ? 'rec-active' : ''" @click="plusMenu=!plusMenu" title="Ещё: документ, арт">➕</button>
            <div v-if="plusMenu" class="plus-backdrop" @click="plusMenu=false"></div>
            <div v-if="plusMenu" class="plus-menu">
              <button @click="canvasGenMode=true; artMode=false; plusMenu=false">📄 Создать документ/код (Canvas)</button>
              <button @click="generateArt('prompt'); plusMenu=false">🖼 Сгенерировать фото (арт)</button>
              <button @click="generateArt('scene'); plusMenu=false">🎬 Арт по последней сцене</button>
              <button @click="generateArt('overview'); plusMenu=false">🌅 Арт по общей картине</button>
            </div>
          </div>
          <label class="btn-icon" style="margin:0; cursor:pointer" title="Прикрепить файл: фото, аудио, видео или документ (Word/PDF/текст)">
            📎<input type="file" multiple accept="image/*,audio/*,video/*,.pdf,.doc,.docx,.odt,.rtf,.txt,.md,.csv" style="display:none" @change="onAttach" />
          </label>
          <!-- Голос — отдельной кнопкой: запись/стоп в один клик -->
          <button class="btn-icon" :class="recording ? 'rec-active' : ''" @click="toggleRecord"
                  :title="recording ? 'Остановить запись' : 'Записать голос'">{{ recording ? '⏺ стоп' : '🎤' }}</button>
          <textarea ref="composer" v-model="input" rows="1" class="composer-input"
                    :placeholder="composerPlaceholder"
                    @input="autoGrow" @keydown="onComposerKeydown" @paste="onPaste"></textarea>
          <button v-if="streaming" class="btn-danger" @click="stop">■ Стоп</button>
          <button v-else-if="canvasCmdMode" class="btn-primary" @click="applyCanvasCmd" :disabled="canvasBusy">✨ Применить</button>
          <button v-else-if="canvasGenMode" class="btn-primary" @click="send" :disabled="!connected || canvasGenerating">{{ canvasGenerating ? '⏳…' : '📄 Создать' }}</button>
          <button v-else-if="artMode" class="btn-primary" @click="sendArt" :disabled="!connected">🎨 Сгенерировать</button>
          <button v-else class="btn-primary" @click="send" :disabled="!connected || waitingFiles">{{ waitingFiles ? '⏳ файлы…' : 'Отправить' }}</button>
        </div>
      </div>
    </div>

    <!-- ===== Канвас: документ/код рядом с чатом (side-by-side, как в Gemini) ===== -->
    <div class="canvas-pane" v-if="canvasOpen && canvas">
      <div class="canvas-head">
        <button class="btn-icon only-mobile" @click="mobilePane='chat'" title="К чату">💬</button>
        <input v-model="canvas.title" class="canvas-title" @blur="saveCanvas" placeholder="Без названия" />
        <select v-model="canvas.kind" @change="saveCanvas" class="canvas-kind" title="Тип канваса">
          <option value="document">📄 документ</option>
          <option value="code">💻 код</option>
        </select>
        <button class="btn-icon" @click="undoCanvas" :disabled="!canvas.can_undo || canvasBusy" title="Откатить к предыдущей версии">↩</button>
        <button class="btn-icon" @click="exportCanvas('docx')" title="Экспорт в Word">📄</button>
        <button class="btn-icon" @click="exportCanvas('pdf')" title="Экспорт в PDF">📑</button>
        <button class="btn-icon" @click="closeCanvas" title="Закрыть канвас">✕</button>
      </div>

      <!-- Единый тулбар: слева — контекстные действия (форматирование / копировать код),
           справа — переключатель «Редактор / Просмотр» (для веб-кода — live-результат). -->
      <div class="canvas-tabs">
        <template v-if="canvas.kind==='document' && canvasView==='edit'">
          <button class="canvas-fmt" @click="wrapSelection('**','**')" title="Жирный"><b>B</b></button>
          <button class="canvas-fmt" @click="wrapSelection('*','*')" title="Курсив"><i>I</i></button>
          <button class="canvas-fmt" @click="wrapSelection('## ','')" title="Заголовок">H</button>
          <button class="canvas-fmt" @click="wrapSelection('\`','\`')" title="Моноширинный">&lt;/&gt;</button>
        </template>
        <button v-if="canvas.kind==='code'" class="canvas-fmt" @click="copyCanvas" :title="copied ? 'Скопировано' : 'Скопировать код'">{{ copied ? '✓ Скопировано' : '⧉ Скопировать код' }}</button>
        <div style="flex:1"></div>
        <button :class="{ active: canvasView==='edit' }" @click="canvasView='edit'">✎ {{ canvas.kind==='code' ? 'Код' : 'Редактор' }}</button>
        <button :class="{ active: canvasView==='preview' }" @click="canvasView='preview'">{{ canvasIsWeb ? '▶ Превью' : '👁 Просмотр' }}</button>
      </div>

      <div class="canvas-body">
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
    <div class="drawer" v-if="drawerTab">
      <div class="tabs">
        <button :class="['tab-btn', drawerTab==='generation'?'active':'']" @click="drawerTab='generation'">Генерация</button>
        <button v-if="isAdmin" :class="['tab-btn', drawerTab==='connection'?'active':'']" @click="drawerTab='connection'">Подключение</button>
        <button :class="['tab-btn', drawerTab==='character'?'active':'']" @click="drawerTab='character'">Персонаж</button>
        <button :class="['tab-btn', drawerTab==='memory'?'active':'']" @click="drawerTab='memory'">Память</button>
        <button :class="['tab-btn', drawerTab==='persona'?'active':'']" @click="drawerTab='persona'">Персона</button>
        <div style="flex:1"></div>
        <button class="tab-btn" @click="drawerTab=null" title="Закрыть">✕</button>
      </div>
      <div class="body">

        <!-- ВКЛАДКА: Генерация -->
        <div v-if="drawerTab==='generation'">
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
          <label>🧠 Окно контекста — память диалога (токенов) <span class="range-val">{{ params.context_tokens >= 1000000 ? '1 млн (максимум)' : params.context_tokens }}</span>
            <input type="number" min="4000" max="1000000" step="4000" v-model.number="params.context_tokens" /></label>
          <div class="row" style="gap:6px; margin:-4px 0 6px; flex-wrap:wrap">
            <button v-for="p in [[32000,'32к'],[128000,'128к'],[200000,'200к'],[1000000,'1 млн']]" :key="p[0]"
                    :class="params.context_tokens === p[0] ? 'btn-primary' : ''"
                    @click="params.context_tokens = p[0]">{{ p[1] }}</button>
          </div>
          <p class="muted" style="margin:2px 0 10px">Сколько ИСТОРИИ чата видит модель на каждый ход. По умолчанию — 1 млн (максимум Gemini): модель помнит весь чат. Уменьшите, если ходы станут дорогими или медленными (свыше ~200 тыс. Gemini тарифицирует дороже). Что не влезло — сохранит авто-сводка (вкладка «Память»).</p>
          <label>📎 Файлы в памяти диалога
            <select v-model.number="params.history_files_mb">
              <option :value="0">все файлы — полная память (по умолчанию)</option>
              <option :value="20">до ~20 МБ на ход</option>
              <option :value="5">до ~5 МБ на ход (экономно)</option>
            </select>
          </label>
          <p class="muted" style="margin:2px 0 10px">Прежние фото/аудио/видео пересылаются модели заново на каждом ходу — она их «видит», а не вспоминает по пометкам. «Все файлы»: в чате с тяжёлыми видео каждый ход несёт их целиком — дольше и дороже; лимиты шлют свежие файлы до N МБ, старые заменяются пометкой [видео: имя].</p>
          <label>Repetition penalty <span class="range-val">{{ params.repetition_penalty }}</span>
            <input type="range" min="0.8" max="2" step="0.05" v-model.number="params.repetition_penalty" /></label>
          <label class="check danger-text"><input type="checkbox" v-model="params.disable_safety" /> Zero-Censorship — снять фильтры (вкл. по умолчанию; порог OFF)</label>
          <label class="check"><input type="checkbox" v-model="params.send_avatars" /> Показывать нейросети аватары (внешность персонажа и ролевика)</label>
          <label class="check"><input type="checkbox" v-model="params.web_access" /> 🌐 Доступ в интернет (веб-поиск на каждый запрос)</label>

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
                <button @click="setDefaultPreset(p)" :title="'Сделать по умолчанию'">⭐</button>
                <button class="btn-danger" @click="deletePreset(p)">🗑</button>
              </span></div>
          </div>
        </div>

        <!-- ВКЛАДКА: Подключение -->
        <div v-if="drawerTab==='connection'">
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
        <div v-if="drawerTab==='character'">
          <h3>Редактор персонажа</h3>
          <div v-if="charEdit">
            <p class="muted" style="margin:0 0 8px">Изменения сохраняются автоматически при выходе из поля.</p>
            <label>Имя<input v-model="charEdit.name" placeholder="Имя персонажа" @change="saveCharacter" /></label>
            <label>Аватар</label>
            <div class="row" style="margin-bottom:10px">
              <img v-if="charEdit.avatar_path" :src="charEdit.avatar_path" class="avatar" style="width:48px;height:48px" />
              <label class="btn" style="margin:0; cursor:pointer">Загрузить файл
                <input type="file" accept="image/*" style="display:none" @change="onAvatarFile" />
              </label>
              <button v-if="charEdit.avatar_path" class="btn-danger" @click="charEdit.avatar_path=''; saveCharacter()">убрать</button>
            </div>
            <label>Описание
              <textarea rows="4" v-model="charEdit.description" @change="saveCharacter"
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
            <label>Модель персонажа (необязательно)
              <input v-model="charEdit.model" placeholder="пусто — модель из настроек генерации" @change="saveCharacter" /></label>
            <button class="btn-primary" @click="saveCharacter">💾 Сохранить сейчас</button>
          </div>
          <p v-else class="muted">Выберите персонажа слева.</p>
        </div>

        <!-- ВКЛАДКА: Память Horae -->
        <div v-if="drawerTab==='memory'">
          <h3>Память Horae 🧠</h3>
          <label class="check"><input type="checkbox" v-model="autoSummary" @change="saveUiPrefs" />
            📜 Авто-сводка сюжета: каждые ~12 сообщений ИИ обновляет запись «Сводка сюжета (авто)» этого чата — события, выпавшие из окна контекста, остаются в памяти модели.</label>
          <div class="hr"></div>
          <p class="muted">Долговременная память ролей. <b>always_on</b> — подмешивается в КАЖДЫЙ запрос (состояние, инвентарь, факты); иначе срабатывает по ключевым словам, как World Info. Области:
            <span class="scope-tag global">🌐 глоб.</span> во всех чатах,
            <span class="scope-tag session">💬 чат</span> только в этом,
            <span class="scope-tag character">🎭 перс.</span> из карточки персонажа.
            Записи сохраняются автоматически в БД. При импорте чата из SillyTavern сюда попадает снимок состояния (💬, always_on).</p>
          <div class="card">
            <input v-model="horaeEdit.title" placeholder="Заголовок" style="margin-bottom:6px" />
            <textarea v-model="horaeEdit.content" rows="3" placeholder="Содержимое" style="margin-bottom:6px"></textarea>
            <input v-model="horaeEdit.keywords" placeholder="ключевые слова через запятую" style="margin-bottom:6px" />
            <div class="row" style="margin-bottom:6px">
              <select v-model="horaeEdit.category"><option>lore</option><option>state</option><option>inventory</option><option>character</option><option>hidden</option></select>
              <input type="number" v-model.number="horaeEdit.priority" placeholder="приоритет" style="width:90px" />
            </div>
            <label class="check"><input type="checkbox" v-model="horaeEdit.always_on" /> always_on</label>
            <label class="check"><input type="checkbox" v-model="horaeEdit.enabled" /> включено</label>
            <div class="row" v-if="!horaeEdit.id">
              <select v-model="horaeEdit.scope"><option value="global">глобально</option><option value="session">только этот чат</option></select>
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
                <button class="btn-icon" @click="editHorae(h)">✎</button>
                <button class="btn-danger" @click="deleteHorae(h)">🗑</button>
              </span>
            </div>
            <div class="muted">{{ h.content }}</div>
          </div>
        </div>

        <!-- ВКЛАДКА: Персона + Author's Note -->
        <div v-if="drawerTab==='persona'">
          <h3>Персона пользователя</h3>
          <label>Активная персона в этом чате
            <select v-model="sessionPersonaId" @change="applySessionMeta">
              <option :value="null">— нет —</option>
              <option v-for="p in personas" :key="p.id" :value="p.id">{{ p.name }}</option>
            </select>
          </label>
          <div class="card">
            <input v-model="personaNew.name" placeholder="Имя персоны" style="margin-bottom:6px" />
            <textarea v-model="personaNew.description" rows="2" placeholder="Описание (кто я)"></textarea>
            <div class="row" style="margin-top:6px">
              <img v-if="personaNew.avatar_path" :src="personaNew.avatar_path" class="avatar" style="width:40px;height:40px" />
              <label class="btn" style="margin:0;cursor:pointer">Внешность (фото)
                <input type="file" accept="image/*" style="display:none" @change="onPersonaAvatar" />
              </label>
            </div>
            <button class="btn-primary" @click="createPersona" style="margin-top:6px">Создать персону</button>
          </div>
          <div class="card" v-for="p in personas" :key="p.id">
            <div class="row-between">
              <span class="row" style="gap:8px"><img v-if="p.avatar_path" :src="p.avatar_path" class="avatar" /><b>{{ p.name }}</b></span>
              <button class="btn-danger" @click="deletePersona(p)">🗑</button>
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
                <button class="btn-danger" @click="removeFriend(f)" title="Удалить из друзей">Удалить</button>
              </div>
            </div>
            <p class="muted" style="margin-top:8px">Делиться чатом: откройте чат в списке слева и нажмите 🔗 — друг увидит его в разделе «Доступные мне». Так же делятся и групповые чаты.</p>
          </div>

          <div class="hr"></div>
          <h3>Часовой пояс этого чата 🕒</h3>
          <p class="muted">Нейросеть видит ваше текущее время (утро/ночь, день недели) и метки времени сообщений показываются в этом поясе. Настройка сохраняется для каждого чата отдельно; по умолчанию берётся из браузера.</p>
          <label>Часовой пояс
            <input v-model="sessionTimezone" list="tz-list" placeholder="например Europe/Moscow" @change="applySessionMeta" />
            <datalist id="tz-list"><option v-for="tz in tzOptions" :key="tz" :value="tz"></option></datalist>
          </label>
          <div class="row">
            <button @click="_autoTimezone(); applySessionMeta()">📍 Определить по браузеру</button>
          </div>

          <div class="hr"></div>
          <h3>Заметка автора (Author's Note)</h3>
          <p class="muted">Подмешивается у самого конца контекста — сильно влияет на ответ.</p>
          <textarea v-model="authorNote" rows="3" @blur="applySessionMeta" placeholder="например: Пиши от третьего лица, держи мрачный тон."></textarea>
        </div>

      </div>
    </div>
  </div>

  <!-- ===== Админ-модалка ===== -->
  <div v-if="adminOpen" class="modal-backdrop" @click.self="adminOpen=false">
    <div class="modal">
      <div class="row-between" style="margin-bottom:10px">
        <h3 style="margin:0">Администрирование</h3>
        <button class="btn-icon" @click="adminOpen=false">✕</button>
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
        <label>Код доступа к приложению (пусто = открыто всем)
          <input v-model="adminSec.access_code" placeholder="код для входа" />
        </label>
        <label>Пароль администратора (пусто = без пароля)
          <input v-model="adminSec.admin_password" type="password" placeholder="пароль админа" />
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
          <input v-model="adminTg.token" type="password" placeholder="123456:ABC..." />
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
                <button v-if="u.role!=='admin'" @click="setUserRole(u,'admin')" title="Сделать админом">⬆ админ</button>
                <button v-else @click="setUserRole(u,'user')" title="Снять админа">⬇ юзер</button>
                <button class="btn-danger" @click="deleteUser(u)">🗑</button>
              </span>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>

  <!-- ===== Модалка создания группового чата ===== -->
  <div v-if="groupModal" class="modal-backdrop" @click.self="groupModal=false">
    <div class="modal">
      <div class="row-between" style="margin-bottom:10px">
        <h3 style="margin:0">Новый групповой чат</h3>
        <button class="btn-icon" @click="groupModal=false">✕</button>
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
        <div v-if="!friends.length" class="muted" style="font-size:13px">У вас пока нет друзей — добавьте их во вкладке «Персона».</div>
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
    <div class="modal" style="width:420px">
      <div class="row-between" style="margin-bottom:12px">
        <h3 style="margin:0">Пригласить в чат</h3>
        <button class="btn-icon" @click="inviteOpen=false">✕</button>
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
    <div class="modal">
      <div class="row-between" style="margin-bottom:10px">
        <h3 style="margin:0">Профиль</h3>
        <button class="btn-icon" @click="profileOpen=false">✕</button>
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
    <div class="modal" style="width:640px">
      <div class="row-between" style="margin-bottom:8px">
        <h3 style="margin:0">🐞 Отладка LLM</h3>
        <span><button @click="clearDebug">Очистить</button> <button class="btn-icon" @click="closeDebug">✕</button></span>
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
      </div>
    </div>
  </div>

  <!-- ===== Всплывающие уведомления (тосты) ===== -->
  <div class="toast-wrap">
    <div v-for="t in toasts" :key="t.id" class="toast" @click="toastClick(t)">{{ t.text }}</div>
  </div>

  <!-- ===== Диалог (подтверждение/ввод) вместо браузерных confirm/prompt ===== -->
  <div v-if="dialog" class="modal-backdrop" @click.self="dialogCancel" @keydown.esc="dialogCancel">
    <div class="modal dialog-modal">
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

  <!-- ===== Лайтбокс: полноэкранный предпросмотр картинки ===== -->
  <div v-if="lightbox" class="lightbox" @click="lightbox=null"><img :src="lightbox" /></div>

  <!-- ===== Плавающий тулбар Канваса: появляется при выделении (как в Notion) ===== -->
  <div v-if="canvasOpen && canvas && canvasSelText && toolbarPos" class="canvas-toolbar"
       :style="{ top: toolbarPos.top + 'px', left: toolbarPos.left + 'px' }" @mousedown.prevent>
    <template v-if="canvas.kind==='code'">
      <button @click="quickAction('Добавь подробные комментарии, объясняющие, что делает код.')" :disabled="canvasBusy" title="Комментарии">💬</button>
      <button @click="quickAction('Найди и исправь баги в этом фрагменте.')" :disabled="canvasBusy" title="Найти баги">🐞</button>
      <button @click="quickAction('Сделай ревью: улучши читаемость и структуру, не меняя поведение.')" :disabled="canvasBusy" title="Ревью">🔍</button>
      <button @click="translateCode" :disabled="canvasBusy" title="Перевести на другой язык">🔁</button>
    </template>
    <template v-else>
      <button @click="quickAction('Сократи примерно вдвое, сохранив суть.')" :disabled="canvasBusy" title="Короче">↧</button>
      <button @click="quickAction('Расширь, добавь деталей и примеров.')" :disabled="canvasBusy" title="Подробнее">↥</button>
      <button @click="quickAction('Перепиши в строгом профессиональном тоне.')" :disabled="canvasBusy" title="Строже">🎩</button>
      <button @click="quickAction('Перепиши простым языком, понятно для новичка.')" :disabled="canvasBusy" title="Проще">🙂</button>
      <button @click="quickAction('Исправь грамматику, орфографию и пунктуацию, не меняя стиль.')" :disabled="canvasBusy" title="Грамматика">✓</button>
    </template>
    <span class="ct-sep"></span>
    <button @click="focusCanvasAi" :disabled="canvasBusy" title="Своя команда для выделенного">✨</button>
  </div>
  </template>
  `,
}).mount("#app");
