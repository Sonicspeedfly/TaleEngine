/*
 * Плавающий плеер: аудио и видео из чата играют в маленьком окне поверх
 * страницы, пока человек листает переписку, открывает другой чат или
 * настройки.
 *
 * Зачем. Вложение играло прямо в ленте: стоило прокрутить чат дальше —
 * плеер уезжал, и чтобы поставить паузу или перемотать, надо было искать его
 * глазами. Браузеры умеют выносить в отдельное окошко только видео (и не
 * все), голосовые и музыку — нет. Здесь одно окно для всего: его можно
 * перетащить куда удобно, свернуть в пилюлю и вернуться к сообщению, из
 * которого запущен файл.
 *
 * Устроено как horae.js: объект опций Vue с шаблоном-строкой, без сборки.
 * Состояние очереди держит корень (app.js): что играет, из какого чата и
 * что следующее; плеер владеет элементом <audio>/<video> и своим видом.
 *
 * Экспорт: window.TalePlayer = { MediaDock, fmtTime }.
 */
(function () {
  "use strict";

  const STORE = "mediaDock";   // localStorage: положение, размер, скорость, «подряд»
  const RATES = [1, 1.25, 1.5, 2, 0.75];
  const SIZES = { s: 240, m: 320, l: 480 };
  const EDGE = 8;              // плеер не уезжает за край окна ближе, чем на столько

  function fmtTime(sec) {
    if (!isFinite(sec) || sec < 0) return "0:00";
    const s = Math.floor(sec % 60), m = Math.floor(sec / 60) % 60, h = Math.floor(sec / 3600);
    const ss = String(s).padStart(2, "0");
    return h ? h + ":" + String(m).padStart(2, "0") + ":" + ss : m + ":" + ss;
  }

  function loadPrefs() {
    try { return JSON.parse(localStorage.getItem(STORE) || "{}") || {}; } catch (e) { return {}; }
  }

  function savePrefs(patch) {
    try { localStorage.setItem(STORE, JSON.stringify({ ...loadPrefs(), ...patch })); } catch (e) { /* приватный режим */ }
  }

  const MediaDock = {
    name: "MediaDock",
    props: {
      // Очередь медиа чата, из которого открыт плеер: [{src, kind, name, msgId, idx, sessionId, who}].
      queue: { type: Array, default: () => [] },
      // С какого файла очереди начать (дальше плеер листает очередь сам).
      pos: { type: Number, default: 0 },
      // С какой секунды начать текущий файл (перенос из ленты продолжает с того же места).
      startAt: { type: Number, default: 0 },
      // Растёт при каждом открытии — даже того же файла: начать заново.
      seq: { type: Number, default: 0 },
      chatTitle: { type: String, default: "" },
      // Пока плеер не передвигали — столько пикселей от низа окна (над полем ввода).
      bottomGap: { type: Number, default: 0 },
    },
    // playing(bool) — играет ли; track(item) — какой файл сейчас в плеере.
    emits: ["close", "goto", "playing", "track"],
    data() {
      const p = loadPrefs();
      return {
        i: this.pos,
        playing: false,
        time: 0,
        duration: 0,
        buffering: false,
        error: "",
        rate: RATES.includes(p.rate) ? p.rate : 1,
        muted: false,
        autoNext: p.autoNext !== false,
        collapsed: !!p.collapsed,
        size: SIZES[p.size] ? p.size : "m",
        showList: false,
        x: typeof p.x === "number" ? p.x : null,
        y: typeof p.y === "number" ? p.y : null,
        drag: null,
        seeking: false,
      };
    },
    computed: {
      cur() { return this.queue[this.i] || null; },
      isVideo() { return !!this.cur && this.cur.kind === "video"; },
      hasPrev() { return this.i > 0; },
      hasNext() { return this.i < this.queue.length - 1; },
      width() { return this.isVideo && !this.collapsed ? SIZES[this.size] : 300; },
      boxStyle() {
        const st = { width: this.collapsed ? "auto" : this.width + "px" };
        if (this.x !== null && this.y !== null) {
          st.left = this.x + "px";
          st.top = this.y + "px";
        } else if (this.bottomGap > 0) {
          st.bottom = this.bottomGap + "px";
        }
        return st;
      },
      progress() { return this.duration ? Math.min(100, (this.time / this.duration) * 100) : 0; },
      title() { return (this.cur && this.cur.name) || (this.isVideo ? "Видео" : "Аудио"); },
      subtitle() {
        const c = this.cur;
        if (!c) return "";
        return [c.who, this.chatTitle].filter(Boolean).join(" · ");
      },
    },
    watch: {
      // Новое открытие (из ленты, кнопкой «поверх»): файл pos с секунды startAt.
      seq() { this.i = this.pos; this.showList = false; this.load(this.startAt, true); },
      collapsed(v) { savePrefs({ collapsed: v }); this.$nextTick(this.clamp); },
      size(v) { savePrefs({ size: v }); this.$nextTick(this.clamp); },
      autoNext(v) { savePrefs({ autoNext: v }); },
    },
    mounted() {
      window.addEventListener("resize", this.clamp);
      this.load(this.startAt, true);
      this.$nextTick(this.clamp);
    },
    beforeUnmount() {
      window.removeEventListener("resize", this.clamp);
      this.clearSession();
    },
    methods: {
      media() { return this.$refs.media || null; },
      // Загрузить текущий файл очереди и (если autoplay) начать с секунды at.
      load(at, autoplay) {
        this.error = "";
        this.time = at || 0;
        this.duration = 0;
        this.$emit("track", this.cur);
        this.$nextTick(() => {
          const el = this.media();
          if (!el || !this.cur) return;
          if (el.getAttribute("src") !== this.cur.src) el.setAttribute("src", this.cur.src);
          el.playbackRate = this.rate;
          // rewind — вернуть позицию после прыжка «в бесконечность» (см. ниже).
          const go = (rewind) => {
            if (at || rewind) { try { el.currentTime = at || 0; } catch (e) { /* ещё не готов */ } }
            if (autoplay) el.play().catch(() => { this.playing = false; });
          };
          const start = () => {
            if (isFinite(el.duration)) { go(); return; }
            // Запись из браузера (webm MediaRecorder) не знает своей длины,
            // пока её не дочитать: прыжок «в бесконечность» заставляет браузер
            // её посчитать — иначе полоса перемотки стояла бы мёртвой.
            let done = false;
            const fix = () => {
              if (done || !isFinite(el.duration)) return;
              done = true;
              el.removeEventListener("durationchange", fix);
              this.onMeta();
              go(true);
            };
            el.addEventListener("durationchange", fix);
            setTimeout(() => { if (!done) { done = true; el.removeEventListener("durationchange", fix); go(true); } }, 2500);
            try { el.currentTime = 1e101; } catch (e) { fix(); }
          };
          if (el.readyState >= 1) start();
          else el.addEventListener("loadedmetadata", start, { once: true });
          this.updateSession();
        });
      },
      toggle() {
        const el = this.media();
        if (!el) return;
        if (el.paused) el.play().catch((e) => { this.error = "Не удалось включить: " + (e && e.message || e); });
        else el.pause();
      },
      // Пауза снаружи (в ленте запустили другой файл — звук должен быть один).
      pause() { const el = this.media(); if (el && !el.paused) el.pause(); },
      skip(sec) {
        const el = this.media();
        if (!el) return;
        el.currentTime = Math.max(0, Math.min((el.duration || Infinity), el.currentTime + sec));
      },
      seekTo(ev) {
        const el = this.media();
        if (!el || !this.duration) return;
        el.currentTime = (Number(ev.target.value) / 100) * this.duration;
      },
      cycleRate() {
        const i = RATES.indexOf(this.rate);
        this.rate = RATES[(i + 1) % RATES.length];
        const el = this.media();
        if (el) el.playbackRate = this.rate;
        savePrefs({ rate: this.rate });
      },
      toggleMute() {
        const el = this.media();
        if (!el) return;
        el.muted = !el.muted;
        this.muted = el.muted;
      },
      go(i) {
        if (i < 0 || i >= this.queue.length) return;
        this.i = i;
        this.load(0, true);
      },
      prev() {
        // Как у плееров: в начале трека — к предыдущему, иначе — в начало этого.
        if (this.time > 3 || !this.hasPrev) { this.skip(-Infinity); return; }
        this.go(this.i - 1);
      },
      next() { if (this.hasNext) this.go(this.i + 1); },
      pick(i) {
        this.showList = false;
        if (i === this.i) { this.skip(-Infinity); const el = this.media(); if (el && el.paused) this.toggle(); }
        else this.go(i);
      },
      cycleSize() { this.size = { s: "m", m: "l", l: "s" }[this.size]; },
      fullscreen() {
        const el = this.media();
        if (el && el.requestFullscreen) el.requestFullscreen().catch(() => {});
      },
      systemPip() {
        const el = this.media();
        if (el && document.pictureInPictureEnabled && el.requestPictureInPicture) {
          el.requestPictureInPicture().catch(() => {});
        }
      },
      pipSupported() { return !!(document.pictureInPictureEnabled && this.isVideo); },

      // ----- события элемента -----
      onTime() { if (!this.seeking) this.time = this.media() ? this.media().currentTime : 0; this.updatePosition(); },
      onMeta() { const el = this.media(); this.duration = el && isFinite(el.duration) ? el.duration : 0; this.updatePosition(); },
      onPlay() { this.playing = true; this.buffering = false; this.$emit("playing", true); this.updateSession(); },
      onPause() { this.playing = false; this.$emit("playing", false); this.updateSession(); },
      onEnded() {
        this.playing = false;
        this.$emit("playing", false);
        if (this.autoNext && this.hasNext) this.go(this.i + 1);
      },
      onError() {
        const el = this.media();
        if (!el || !el.getAttribute("src")) return;
        this.playing = false;
        this.error = "Файл не воспроизводится (нет доступа или формат не поддерживается).";
      },

      // ----- перетаскивание за шапку -----
      dragStart(ev) {
        if (ev.button !== undefined && ev.button !== 0) return;
        if (ev.target.closest("button, input, select, a")) return;
        const box = this.$refs.box.getBoundingClientRect();
        this.drag = { dx: ev.clientX - box.left, dy: ev.clientY - box.top, moved: false, id: ev.pointerId };
        try { ev.currentTarget.setPointerCapture(ev.pointerId); } catch (e) { /* старый браузер */ }
      },
      dragMove(ev) {
        if (!this.drag || ev.pointerId !== this.drag.id) return;
        this.drag.moved = true;
        this.x = ev.clientX - this.drag.dx;
        this.y = ev.clientY - this.drag.dy;
        this.clamp();
      },
      dragEnd(ev) {
        if (!this.drag || (ev && ev.pointerId !== this.drag.id)) return;
        const moved = this.drag.moved;
        this.drag = null;
        if (moved) savePrefs({ x: this.x, y: this.y });
        return moved;
      },
      // Свёрнутая пилюля: нажатие разворачивает, перетаскивание — двигает.
      pillUp(ev) { if (!this.dragEnd(ev)) this.collapsed = false; },
      // Стрелки на шапке двигают плеер с клавиатуры (перетаскивание мышью — не единственный путь).
      nudge(dx, dy) {
        const box = this.$refs.box.getBoundingClientRect();
        this.x = (this.x === null ? box.left : this.x) + dx;
        this.y = (this.y === null ? box.top : this.y) + dy;
        this.clamp();
        savePrefs({ x: this.x, y: this.y });
      },
      clamp() {
        const box = this.$refs.box;
        if (!box || this.x === null || this.y === null) return;
        const r = box.getBoundingClientRect();
        const vw = window.innerWidth, vh = window.innerHeight;
        this.x = Math.max(EDGE, Math.min(this.x, vw - r.width - EDGE));
        this.y = Math.max(EDGE, Math.min(this.y, vh - r.height - EDGE));
      },
      resetPlace() {
        this.x = null; this.y = null;
        savePrefs({ x: null, y: null });
      },
      onKey(ev) {
        if (ev.target.closest("input, select")) return;
        // Shift+стрелки — передвинуть плеер с клавиатуры (не только мышью).
        if (ev.shiftKey && ev.key.startsWith("Arrow")) {
          ev.preventDefault();
          const d = { ArrowLeft: [-40, 0], ArrowRight: [40, 0], ArrowUp: [0, -40], ArrowDown: [0, 40] }[ev.key];
          if (d) this.nudge(d[0], d[1]);
          return;
        }
        if (ev.target.closest("button") && (ev.key === " " || ev.key === "Enter")) return;
        if (ev.key === " " || ev.key === "k") { ev.preventDefault(); this.toggle(); }
        else if (ev.key === "ArrowLeft") { ev.preventDefault(); this.skip(-5); }
        else if (ev.key === "ArrowRight") { ev.preventDefault(); this.skip(5); }
        else if (ev.key === "Escape") { this.collapsed = true; }
      },

      // ----- кнопки системы (шторка телефона, клавиши медиа, экран блокировки) -----
      updateSession() {
        const ms = navigator.mediaSession;
        if (!ms || !this.cur) return;
        try {
          ms.metadata = new window.MediaMetadata({ title: this.title, artist: this.subtitle || "TaleEngine", album: "TaleEngine" });
          ms.playbackState = this.playing ? "playing" : "paused";
          const set = (a, fn) => { try { ms.setActionHandler(a, fn); } catch (e) { /* действие не поддерживается */ } };
          set("play", () => this.toggle());
          set("pause", () => this.pause());
          set("seekbackward", () => this.skip(-10));
          set("seekforward", () => this.skip(10));
          set("previoustrack", this.hasPrev ? () => this.prev() : null);
          set("nexttrack", this.hasNext ? () => this.next() : null);
          set("seekto", (d) => { const el = this.media(); if (el && d && isFinite(d.seekTime)) el.currentTime = d.seekTime; });
        } catch (e) { /* без Media Session — просто нет кнопок в системе */ }
      },
      updatePosition() {
        const ms = navigator.mediaSession;
        if (!ms || !ms.setPositionState || !this.duration) return;
        try { ms.setPositionState({ duration: this.duration, position: Math.min(this.time, this.duration), playbackRate: this.rate }); } catch (e) { /* */ }
      },
      clearSession() {
        const ms = navigator.mediaSession;
        if (!ms) return;
        try {
          ms.metadata = null;
          ms.playbackState = "none";
          for (const a of ["play", "pause", "seekbackward", "seekforward", "previoustrack", "nexttrack", "seekto"]) {
            try { ms.setActionHandler(a, null); } catch (e) { /* */ }
          }
        } catch (e) { /* */ }
      },
      fmt: fmtTime,
    },
    template: `
  <div ref="box" class="media-dock" :class="{ collapsed, video: isVideo && !collapsed, placed: x !== null, dragging: !!drag }"
       :style="boxStyle" role="region" aria-label="Плавающий плеер" @keydown="onKey">
    <!-- Свёрнутый вид: пилюля с кнопкой и названием. -->
    <div v-if="collapsed" class="md-pill" @pointerdown="dragStart" @pointermove="dragMove" @pointerup="pillUp"
         @pointercancel="dragEnd">
      <button class="md-btn md-play" @click.stop="toggle" :aria-label="playing ? 'Пауза' : 'Играть'">{{ playing ? '⏸' : '▶' }}</button>
      <span class="md-pill-title" :title="title">{{ title }}</span>
      <span class="md-pill-bar" aria-hidden="true"><i :style="{ width: progress + '%' }"></i></span>
      <button class="md-btn" @click.stop="collapsed = false" aria-label="Развернуть плеер" title="Развернуть">▴</button>
      <button class="md-btn" @click.stop="$emit('close')" aria-label="Закрыть плеер" title="Закрыть">✕</button>
    </div>

    <template v-else>
      <div class="md-head" tabindex="0" @pointerdown="dragStart" @pointermove="dragMove" @pointerup="dragEnd"
           @pointercancel="dragEnd" @dblclick="resetPlace"
           aria-label="Плеер. Пробел — пауза, стрелки — перемотка, Shift со стрелками — передвинуть"
           title="Перетащите, чтобы передвинуть; двойной щелчок — на место">
        <span class="md-grip" aria-hidden="true">⋮⋮</span>
        <span class="md-titles">
          <span class="md-title" :title="title">{{ title }}</span>
          <span v-if="subtitle" class="md-sub" :title="subtitle">{{ subtitle }}</span>
        </span>
        <button class="md-btn" @click="$emit('goto', cur)" aria-label="Перейти к сообщению" title="К сообщению">↩</button>
        <button v-if="queue.length > 1" class="md-btn" :class="{ on: showList }" @click="showList = !showList"
                :aria-expanded="showList ? 'true' : 'false'" aria-label="Список файлов чата" title="Файлы чата">☰</button>
        <button v-if="isVideo" class="md-btn" @click="cycleSize" :aria-label="'Размер видео: ' + size.toUpperCase()" title="Размер">⤢</button>
        <button class="md-btn" @click="collapsed = true" aria-label="Свернуть плеер" title="Свернуть">▾</button>
        <button class="md-btn" @click="$emit('close')" aria-label="Закрыть плеер" title="Закрыть">✕</button>
      </div>

      <ol v-if="showList" class="md-list" aria-label="Файлы чата">
        <li v-for="(q, n) in queue" :key="q.msgId + ':' + q.idx">
          <button :class="{ cur: n === i }" @click="pick(n)" :aria-current="n === i ? 'true' : null">
            <span aria-hidden="true">{{ q.kind === 'video' ? '🎬' : '🎵' }}</span>
            <span class="md-li-name">{{ q.name || (q.kind === 'video' ? 'Видео' : 'Аудио') }}</span>
            <span class="md-li-who">{{ q.who }}</span>
          </button>
        </li>
      </ol>
    </template>

    <!-- Сам элемент живёт всегда (и в свёрнутом виде): иначе сворачивание
         останавливало бы звук. Видео в пилюле просто не показывается. -->
    <div v-if="isVideo" v-show="!collapsed" class="md-stage">
      <video ref="media" class="md-video" playsinline preload="metadata"
             @click="toggle" @timeupdate="onTime" @loadedmetadata="onMeta" @durationchange="onMeta"
             @play="onPlay" @pause="onPause" @ended="onEnded" @error="onError"
             @waiting="buffering = true" @playing="buffering = false"></video>
      <span class="md-stage-tools">
        <button v-if="pipSupported()" class="md-btn" @click="systemPip" aria-label="Отдельное окно системы"
                title="Окно поверх всех программ">⧉</button>
        <button class="md-btn" @click="fullscreen" aria-label="Во весь экран" title="Во весь экран">⛶</button>
      </span>
    </div>
    <audio v-else ref="media" preload="metadata"
           @timeupdate="onTime" @loadedmetadata="onMeta" @durationchange="onMeta"
           @play="onPlay" @pause="onPause" @ended="onEnded" @error="onError"
           @waiting="buffering = true" @playing="buffering = false"></audio>

    <div v-if="!collapsed" class="md-body">
      <div class="md-seek">
        <span class="md-time">{{ fmt(time) }}</span>
        <input type="range" min="0" max="100" step="0.1" :value="progress"
               @pointerdown="seeking = true" @pointerup="seeking = false" @input="seekTo" @change="seeking = false"
               :aria-valuetext="fmt(time) + ' из ' + fmt(duration)" aria-label="Перемотка" />
        <span class="md-time">{{ fmt(duration) }}</span>
      </div>
      <div class="md-controls">
        <button class="md-btn" @click="prev" aria-label="Предыдущий файл или в начало" title="Назад">⏮</button>
        <button class="md-btn" @click="skip(-10)" aria-label="Назад на 10 секунд" title="−10 с">↺10</button>
        <button class="md-btn md-play" @click="toggle" :aria-label="playing ? 'Пауза' : 'Играть'">
          {{ buffering && playing ? '…' : (playing ? '⏸' : '▶') }}</button>
        <button class="md-btn" @click="skip(10)" aria-label="Вперёд на 10 секунд" title="+10 с">10↻</button>
        <button class="md-btn" @click="next" :disabled="!hasNext" aria-label="Следующий файл" title="Дальше">⏭</button>
        <span class="md-spacer"></span>
        <button class="md-btn md-rate" @click="cycleRate" :aria-label="'Скорость ' + rate + '×'" title="Скорость">{{ rate }}×</button>
        <button class="md-btn" @click="toggleMute" :aria-label="muted ? 'Включить звук' : 'Выключить звук'"
                :title="muted ? 'Включить звук' : 'Без звука'">{{ muted ? '🔇' : '🔊' }}</button>
        <button class="md-btn" :class="{ on: autoNext }" @click="autoNext = !autoNext"
                :aria-pressed="autoNext ? 'true' : 'false'" aria-label="Играть файлы подряд" title="Подряд">⇉</button>
      </div>
      <p v-if="error" class="md-error" role="alert">{{ error }}</p>
    </div>
  </div>
  `,
  };

  window.TalePlayer = { MediaDock, fmtTime };
})();
