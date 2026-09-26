/*
 * Horae State Engine — интерфейс: вкладка «Хроника» в ящике и строка Horae
 * под каждым ответом ИИ.
 *
 * Отдельным файлом, а не ещё парой тысяч строк в app.js: хроника живёт своей
 * жизнью (свой опрос заданий, свои слои настроек, девять подвкладок), и в
 * общем объекте приложения её данные смешались бы с лентой и генерацией.
 * Устроено так же, как app.js: объекты опций Vue с шаблонами-строками, без
 * сборки. Сеть, тосты и диалоги берутся у корня (this.$root.api, showToast,
 * askConfirm) — вторая обёртка над fetch рано или поздно разошлась бы с
 * первой в заголовках авторизации и в разборе ошибок сервера.
 *
 * Экспорт: window.HoraeUI = { stripTags, hasOpenTag, components }.
 * app.js регистрирует HoraePanel и HoraeMsg; остальные компоненты — части
 * панели и регистрируются в ней локально.
 */
(function () {
  "use strict";

  // ==========================================================================
  // Вырезание служебных тегов
  // ==========================================================================
  // Сервер вырезает теги при сохранении ответа, но во время стрима текст идёт
  // сырым: без этого человек видел бы, как модель в конце ответа печатает
  // «<horae>time:…» — служебную разметку посреди рассказа. Правила те же, что
  // у strip_tags_text на сервере (спека §5): блоки <horae>, <horaeevent>,
  // <horaerpg>, <horaetable:Имя> и <!--horae…-->, внутри <think> не трогаем.
  const BLOCK_SRC = [
    "<horae\\s*>[\\s\\S]*?<\\/horae\\s*>",
    "<horaeevent\\s*>[\\s\\S]*?<\\/horaeevent\\s*>",
    "<horaerpg\\s*>[\\s\\S]*?<\\/horaerpg\\s*>",
    "<horaetable\\s*[:\\uFF1A][^>]*>[\\s\\S]*?<\\/horaetable(?:\\s*[:\\uFF1A][^>]*)?\\s*>",
    "<!--\\s*horae[\\s\\S]*?-->",
  ].join("|");
  // Открывающий тег без закрывающего: модель прямо сейчас пишет блок.
  const OPEN_SRC = "<(?:horae(?:event|rpg)?\\s*>|horaetable\\s*[:\\uFF1A]|!--\\s*horae)";
  // <think>…</think> (или незакрытый до конца текста) — рассуждения модели:
  // сервер теги внутри них не разбирает и не вырезает, клиент тоже.
  const THINK_SRC = "<(think|thinking)\\b[^>]*>[\\s\\S]*?(?:<\\/\\1\\s*>|$)";
  // Начала открывающих тегов для хвоста стрима: токен может оборваться на
  // «<hor», и без этого обрубок мигал бы в пузыре до следующего токена.
  const OPENERS = ["<horae>", "<horaeevent>", "<horaerpg>", "<horaetable:", "<horaetable：", "<!--horae", "<!-- horae"];

  function trailingOpener(text, minLen) {
    const i = text.lastIndexOf("<");
    if (i === -1) return -1;
    const tail = text.slice(i);
    if (tail.indexOf(">") !== -1 || tail.length < (minLen || 1)) return -1;
    const low = tail.toLowerCase();
    return OPENERS.some((o) => o.startsWith(low)) ? i : -1;
  }

  function stripOutside(seg, cutOpen) {
    let out = seg.replace(new RegExp(BLOCK_SRC, "gi"), "");
    if (cutOpen) {
      const m = new RegExp(OPEN_SRC, "i").exec(out);
      if (m) out = out.slice(0, m.index);
      const t = trailingOpener(out, 1);
      if (t !== -1) out = out.slice(0, t);
    }
    return out;
  }

  // stripTags(text, partial): partial=true отрезает и незакрытый хвостовой
  // блок — это режим стрима. Для сохранённых сообщений (страховка на случай
  // старого сервера) partial=false: незакрытое там — уже чей-то текст.
  function stripTags(text, partial) {
    if (typeof text !== "string" || !text) return text || "";
    if (text.indexOf("<") === -1) return text;
    const re = new RegExp(THINK_SRC, "gi");
    const parts = [];
    let last = 0;
    let m;
    while ((m = re.exec(text))) {
      parts.push([false, text.slice(last, m.index)]);
      parts.push([true, m[0]]);
      last = m.index + m[0].length;
      if (!m[0].length) re.lastIndex++;
    }
    parts.push([false, text.slice(last)]);
    let changed = false;
    const out = parts.map(([think, seg], i) => {
      if (think) return seg;
      const cut = stripOutside(seg, !!partial && i === parts.length - 1);
      if (cut !== seg) changed = true;
      return cut;
    }).join("");
    return changed ? out.replace(/\s+$/, "") : out;
  }

  // Модель пишет служебный блок: есть незакрытый тег, обрубок тега в хвосте
  // или после последнего готового блока одни пробелы (между <horae> и
  // <horaeevent> пометка не должна мигать).
  function hasOpenTag(text) {
    if (typeof text !== "string" || text.indexOf("<") === -1) return false;
    const outside = text.replace(new RegExp(THINK_SRC, "gi"), "");
    const rest = outside.replace(new RegExp(BLOCK_SRC, "gi"), "\u0000");
    if (new RegExp(OPEN_SRC, "i").test(rest)) return true;
    if (trailingOpener(rest, 2) !== -1) return true;
    const i = rest.lastIndexOf("\u0000");
    return i !== -1 && !rest.slice(i + 1).trim();
  }

  // ==========================================================================
  // Мелочи
  // ==========================================================================
  const obj = (v) => (v && typeof v === "object" && !Array.isArray(v) ? v : {});
  const arr = (v) => (Array.isArray(v) ? v : []);
  const str = (v) => (v == null ? "" : String(v));
  const own = (o, k) => !!o && Object.prototype.hasOwnProperty.call(o, k);
  const clone = (v) => (v == null ? v : JSON.parse(JSON.stringify(v)));
  const splitList = (s) => str(s).split(/[,，、\n]/).map((x) => x.trim()).filter(Boolean);
  const short = (s, n) => { const t = str(s).replace(/\s+/g, " ").trim(); return t.length > (n || 40) ? t.slice(0, n || 40) + "…" : t; };
  const num = (v, dflt) => { const n = Number(v); return v === "" || v == null || !Number.isFinite(n) ? dflt : n; };
  let seq = 0;
  const uid = (p) => (p || "h") + "-" + (++seq);

  // Файл JSON из <input type=file>. Значение поля сбрасываем сразу: иначе
  // повторный выбор того же файла не вызвал бы change вовсе.
  function readJsonFile(ev) {
    const input = ev && ev.target;
    const f = input && input.files && input.files[0];
    if (input) input.value = "";
    if (!f) return Promise.resolve(null);
    return f.text().then((t) => JSON.parse(t));
  }

  function safeName(s) {
    return str(s || "horae").replace(/[^\wЀ-ӿ\-]+/g, "_").slice(0, 60) || "horae";
  }

  const LEVELS = [
    { id: "critical", mark: "★", label: "ключевое", many: "Ключевые" },
    { id: "important", mark: "●", label: "важное", many: "Важные" },
    { id: "normal", mark: "○", label: "обычное", many: "Обычные" },
  ];
  const LEVEL_BY = LEVELS.reduce((a, l) => { a[l.id] = l; return a; }, {});
  const levelOf = (id) => LEVEL_BY[id] || LEVEL_BY.normal;
  const LEVEL_OPTIONS = LEVELS.map((l) => [l.id, l.mark + " " + l.label]);

  const IMPORTANCE = [["", "обычный"], ["!", "важный"], ["!!", "критичный"]];
  const IMPORTANCE_TAG = { "!": "важно", "!!": "критич." };

  const NPC_FIELDS = [
    ["appearance", "Внешность"], ["personality", "Характер"], ["relationship", "Отношение"],
    ["gender", "Пол"], ["age", "Возраст"], ["race", "Раса"], ["job", "Занятие"],
    ["birthday", "День рождения"], ["note", "Примечание"],
  ];

  // Пол пишут свободным текстом («мужчина», «ж», «male») — фильтр понимает
  // частые формы, остальное уходит в «другие».
  function genderKind(g) {
    const s = str(g).trim().toLowerCase();
    if (!s) return "other";
    // \b в JS видит границу слова только у латиницы, поэтому одиночные «м»/«ж»
    // ловим отрицательным просмотром вперёд: «м», но не «маг».
    if (/^(м(?![а-яё])|муж|мальчик|парень|юноша|male|man\b|m\b|♂|男)/.test(s)) return "male";
    if (/^(ж(?![а-яё])|жен|девушка|девочка|female|woman|f\b|♀|女)/.test(s)) return "female";
    return "other";
  }
  const GENDER_ICON = { male: "♂", female: "♀", other: "◌" };

  const PROMPT_TITLES = [
    ["system", "Правила тегов", "Главный промпт: какие теги писать в конце ответа."],
    ["reminder", "Напоминание формата", "Короткая строка в хвосте каждого хода."],
    ["analysis", "ИИ-анализ сообщения", "Когда в ответе нет тегов — служебная модель извлекает данные."],
    ["batch", "Пакетный скан", "ИИ-скан старой истории пакетами."],
    ["compress_events", "Сжатие событий", "Ручное сжатие выбранных событий в свёртку."],
    ["compress_fulltext", "Сжатие по полному тексту", "То же, но по тексту сообщений."],
    ["auto_summary", "Авто-свёртка", "Свёртка старой части хронологии."],
    ["auto_resummary", "Свёртка свёрток", "Когда свёрток одного уровня много."],
    ["tables", "Правила таблиц", "Как заполнять свои таблицы."],
    ["location", "Память сцен", "Правило scene_desc."],
    ["relationship", "Сеть отношений", "Правило rel."],
    ["mood", "Настроение", "Правило mood."],
    ["rpg", "RPG", "Правила тега <horaerpg>."],
    ["anti_paraphrase", "Без пересказа", "Режим, где модель не пересказывает вашу реплику."],
    ["query_rewrite", "Переписывание запроса", "Запрос для поиска воспоминаний."],
  ];

  const RPG_MODULES = [
    ["bars", "шкалы"], ["skills", "навыки"], ["attrs", "атрибуты"], ["reputation", "репутация"],
    ["equipment", "снаряжение"], ["level", "уровень"], ["currency", "валюта"],
  ];

  // Задание скана/свёртки ещё идёт — опрашиваем и показываем полосу.
  const jobActive = (j) => !!j && (j.status === "queued" || j.status === "running");

  // Ответ GET …/horae/state → форма, которую шаблоны читают без проверок.
  // Старый или недописанный сервер может не прислать половину полей, и
  // шаблон не должен падать на undefined.items посреди отрисовки.
  function normalize(r) {
    r = obj(r);
    const st = obj(r.state);
    const sc = obj(st.scene);
    const rp = st.rpg && typeof st.rpg === "object" ? st.rpg : null;
    const rc = obj(r.rpg_config);
    const eq = obj(rc.equipment);
    const stats = obj(r.stats);
    return {
      enabled: r.enabled !== false,
      settings: obj(r.settings),
      state: {
        time: { date: str(obj(st.time).date), time: str(obj(st.time).time), display: str(obj(st.time).display) },
        scene: {
          location: str(sc.location), atmosphere: str(sc.atmosphere),
          characters: arr(sc.characters).map(str).filter(Boolean),
          desc: str(sc.desc), parent_desc: str(sc.parent_desc),
        },
        costumes: obj(st.costumes), mood: obj(st.mood),
        items: arr(st.items).filter((x) => x && x.name),
        affection: arr(st.affection).filter((x) => x && x.name),
        npcs: arr(st.npcs).filter((x) => x && x.name).map((n) => ({ ...n, aliases: arr(n.aliases) })),
        agenda: arr(st.agenda).filter((x) => x && x.text),
        relationships: arr(st.relationships).filter((x) => x && x.from && x.to),
        locations: arr(st.locations).filter((x) => x && x.name).map((l) => ({ ...l, aliases: arr(l.aliases) })),
        rpg: rp ? {
          bars: obj(rp.bars), status: obj(rp.status), skills: obj(rp.skills), attrs: obj(rp.attrs),
          reputation: obj(rp.reputation), equipment: obj(rp.equipment), levels: obj(rp.levels),
          xp: obj(rp.xp), currency: obj(rp.currency), strongholds: arr(rp.strongholds),
        } : null,
      },
      timeline: arr(r.timeline).filter((x) => x && (x.kind === "event" || x.kind === "summary")),
      tables: arr(r.tables).filter((t) => t && t.id != null),
      rpg_config: {
        reputation: arr(rc.reputation), currencies: arr(rc.currencies),
        equipment: { locked: !!eq.locked, chars: obj(eq.chars) },
      },
      ops: arr(r.ops),
      stats: {
        messages: stats.messages, ai_messages: stats.ai_messages, with_meta: stats.with_meta,
        without_meta: stats.without_meta, side: stats.side, injection_tokens: stats.injection_tokens,
        summaries: stats.summaries, scan: stats.scan || null, summary_error: stats.summary_error || null,
      },
      job: r.job || null,
    };
  }

  // ==========================================================================
  // Общие компоненты: окно, форма, список строк
  // ==========================================================================

  // Окно поверх ящика. Классы .modal-backdrop/.modal — те же, что у окон
  // приложения: по .modal корень находит верхний слой и держит в нём Tab
  // (_topOverlay). Фокус уходит внутрь при открытии и возвращается на кнопку,
  // которая окно открыла. Esc закрывает только это окно, не весь ящик: он
  // останавливается здесь, до слушателя на window.
  const HModal = {
    name: "HModal",
    props: { title: { type: String, default: "" }, wide: { type: Boolean, default: false } },
    emits: ["close"],
    data() { return { hid: uid("hm") }; },
    template: `
      <div class="modal-backdrop h-modal-backdrop" @click.self="$emit('close')">
        <div ref="box" class="modal h-modal" :class="{ 'h-modal-wide': wide }" role="dialog" aria-modal="true"
             :aria-labelledby="hid" @keydown.esc="onEsc">
          <div class="h-modal-head">
            <h3 :id="hid">{{ title }}</h3>
            <button type="button" class="btn-icon" @click="$emit('close')" aria-label="Закрыть окно">✕</button>
          </div>
          <slot></slot>
        </div>
      </div>`,
    mounted() {
      this._ret = document.activeElement;
      this.$nextTick(() => {
        const box = this.$refs.box;
        if (!box) return;
        // Первое поле, а не «✕»: окно открывают, чтобы что-то ввести.
        const el = box.querySelector("[data-autofocus]")
          || box.querySelector(".h-form input:not([type=hidden]):not([disabled]), .h-form textarea, .h-form select")
          || box.querySelector("button");
        if (el) el.focus();
      });
    },
    beforeUnmount() {
      const r = this._ret;
      // Кнопка могла исчезнуть вместе с перерисовкой списка — тогда некуда.
      if (r && r.focus) setTimeout(() => { if (document.contains(r)) r.focus(); }, 0);
    },
    methods: {
      onEsc(e) {
        // Поверх открыт диалог подтверждения приложения — Esc его, не наш.
        if (this.$root && this.$root.dialog) return;
        e.preventDefault();
        e.stopPropagation();
        this.$emit("close");
      },
    },
  };

  // Универсальная форма в окне. Описание полей — массив:
  // { key, label, type: text|textarea|number|select|checkbox|color, value,
  //   options: [[value, label]], required, hint, placeholder, min, max, step,
  //   list: [строки для подсказок] }.
  // Числа возвращаются числами (пустое поле — null), остальное строками.
  const HForm = {
    name: "HForm",
    components: { "h-modal": HModal },
    props: {
      title: { type: String, default: "" },
      fields: { type: Array, default: () => [] },
      submitText: { type: String, default: "Сохранить" },
      busy: { type: Boolean, default: false },
      danger: { type: String, default: "" },
      note: { type: String, default: "" },
      wide: { type: Boolean, default: false },
    },
    emits: ["submit", "close", "delete"],
    data() {
      const vals = {};
      for (const f of this.fields) {
        vals[f.key] = f.type === "checkbox" ? !!f.value : (f.value == null ? "" : f.value);
      }
      return { vals, fid: uid("hf") };
    },
    template: `
      <h-modal :title="title" :wide="wide" @close="$emit('close')">
        <form class="h-form" @submit.prevent="submit">
          <p v-if="note" class="h-form-note">{{ note }}</p>
          <template v-for="f in fields" :key="f.key">
            <label v-if="f.type === 'checkbox'" class="check">
              <input type="checkbox" v-model="vals[f.key]" :aria-describedby="f.hint ? fid + '-' + f.key : null" /> {{ f.label }}</label>
            <label v-else>{{ f.label }}<span v-if="f.required" class="h-req" aria-hidden="true"> *</span>
              <textarea v-if="f.type === 'textarea'" v-model="vals[f.key]" :rows="f.rows || 3"
                        :required="!!f.required" :placeholder="f.placeholder || ''"
                        :aria-describedby="f.hint ? fid + '-' + f.key : null"></textarea>
              <select v-else-if="f.type === 'select'" v-model="vals[f.key]"
                      :aria-describedby="f.hint ? fid + '-' + f.key : null">
                <option v-for="o in f.options" :key="String(o[0])" :value="o[0]">{{ o[1] }}</option>
              </select>
              <input v-else-if="f.type === 'number'" type="number" v-model="vals[f.key]" inputmode="decimal"
                     :min="f.min" :max="f.max" :step="f.step || 'any'" :required="!!f.required"
                     :placeholder="f.placeholder || ''" :aria-describedby="f.hint ? fid + '-' + f.key : null" />
              <input v-else-if="f.type === 'color'" type="color" v-model="vals[f.key]" class="h-color" />
              <input v-else type="text" v-model="vals[f.key]" :required="!!f.required"
                     :placeholder="f.placeholder || ''" :list="f.list && f.list.length ? fid + '-l-' + f.key : null"
                     :aria-describedby="f.hint ? fid + '-' + f.key : null" autocomplete="off" />
              <datalist v-if="f.list && f.list.length" :id="fid + '-l-' + f.key">
                <option v-for="o in f.list" :key="o" :value="o"></option>
              </datalist>
            </label>
            <span v-if="f.hint" :id="fid + '-' + f.key" class="field-hint h-form-hint">{{ f.hint }}</span>
          </template>
          <slot :vals="vals"></slot>
          <div class="h-form-actions">
            <button v-if="danger" type="button" class="btn-danger" :disabled="busy" @click="$emit('delete', vals)">{{ danger }}</button>
            <span class="h-grow"></span>
            <button type="button" class="btn-ghost" @click="$emit('close')">Отмена</button>
            <button type="submit" class="btn-primary" :disabled="busy">{{ busy ? 'Минуту…' : submitText }}</button>
          </div>
        </form>
      </h-modal>`,
    methods: {
      submit() {
        const out = {};
        for (const f of this.fields) {
          const v = this.vals[f.key];
          if (f.type === "number") out[f.key] = num(v, null);
          else if (f.type === "checkbox") out[f.key] = !!v;
          else out[f.key] = typeof v === "string" ? v.trim() : v;
        }
        this.$emit("submit", out);
      },
    },
  };

  // Строки-записи в редакторе меты сообщения: наряды, предметы, события…
  // Массив строк принадлежит форме родителя и правится на месте.
  const HRows = {
    name: "HRows",
    props: {
      rows: { type: Array, required: true },
      cols: { type: Array, required: true },
      legend: { type: String, default: "" },
      addText: { type: String, default: "Добавить" },
      blank: { type: Object, default: () => ({}) },
      itemName: { type: String, default: "запись" },
    },
    template: `
      <fieldset class="h-fs">
        <legend>{{ legend }}</legend>
        <div v-for="(r, i) in rows" :key="i" class="h-rrow">
          <div class="h-rrow-fields">
            <label v-for="c in cols" :key="c.key" class="h-rf" :class="{ 'h-rf-wide': c.wide, 'h-rf-narrow': c.narrow }">
              <span class="h-rf-l">{{ c.label }}</span>
              <select v-if="c.type === 'select'" v-model="r[c.key]">
                <option v-for="o in c.options" :key="String(o[0])" :value="o[0]">{{ o[1] }}</option>
              </select>
              <textarea v-else-if="c.type === 'textarea'" v-model="r[c.key]" rows="2"></textarea>
              <input v-else-if="c.type === 'number'" type="number" step="any" inputmode="decimal" v-model="r[c.key]" />
              <input v-else-if="c.type === 'color'" type="color" v-model="r[c.key]" class="h-color" />
              <input v-else type="text" v-model="r[c.key]" :placeholder="c.placeholder || ''" autocomplete="off" />
            </label>
          </div>
          <button type="button" class="btn-icon h-rrow-del" @click="rows.splice(i, 1)"
                  :aria-label="'Удалить ' + itemName + ' ' + (i + 1)">✕</button>
        </div>
        <p v-if="!rows.length" class="field-hint">Пусто.</p>
        <button type="button" class="h-add" @click="rows.push(Object.assign({}, blank))">＋ {{ addText }}</button>
      </fieldset>`,
  };

  // Всё, что нужно каждой подвкладке: доступ к панели (hp) и форматтеры.
  // Панель отдаёт себя через provide: запросы, перечитка состояния, правки
  // (ops) и слои настроек живут в одном месте, а не копируются в девять вкладок.
  const HBase = {
    inject: { hp: { default: null } },
    props: { view: { type: Object, required: true } },
    computed: {
      st() { return this.view.state; },
      settings() { return this.view.settings; },
    },
    methods: {
      fmtNum(n) { return this.$root.fmtNum ? this.$root.fmtNum(n) : str(n); },
      plural(n, a, b, c) { return this.$root.plural ? this.$root.plural(Math.abs(Number(n) || 0), a, b, c) : c; },
      short(s, n) { return short(s, n); },
    },
  };

  // Открыть форму в окне: описание полей + что делать по «Сохранить».
  // Сама операция — строкой kind, а не функцией: форма лежит в реактивных
  // данных, и хранить там замыкания незачем.
  const FormHost = {
    data() { return { form: null }; },
    methods: {
      openForm(kind, spec, ctx) {
        this.form = Object.assign({ fields: [], title: "", note: "", danger: "", submitText: "Сохранить", wide: false },
          spec, { kind, ctx: ctx || null, id: uid("f") });
      },
      closeForm() { this.form = null; },
    },
  };
  // Разметка формы одинакова во всех вкладках; разбор — в onForm/onFormDelete вкладки.
  const FORM_TAG = `<h-form v-if="form" :key="form.id" :title="form.title" :fields="form.fields" :note="form.note"
      :danger="form.danger" :submit-text="form.submitText" :wide="form.wide" :busy="hp.busy"
      @submit="onForm" @delete="onFormDelete" @close="closeForm"></h-form>`;

  // ==========================================================================
  // «Состояние»
  // ==========================================================================
  const HStateTab = {
    name: "HStateTab",
    mixins: [HBase, FormHost],
    components: { "h-form": HForm },
    computed: {
      timeText() {
        const t = this.st.time;
        return t.display || [t.date, t.time].filter(Boolean).join(" ");
      },
      // Наряды и настроение — только у присутствующих: в плагине так же, и
      // список «кто во что одет» из прошлых сцен здесь только мешал бы.
      present() {
        return this.st.scene.characters.map((name) => ({
          name, costume: str(this.st.costumes[name]), mood: str(this.st.mood[name]),
        }));
      },
      topItems() {
        const rank = (it) => (it.importance === "!!" ? 0 : it.importance === "!" ? 1 : 2);
        return this.st.items.slice().sort((a, b) => rank(a) - rank(b)).slice(0, 12);
      },
    },
    template: `
      <div class="h-tab">
        <div v-if="!view.enabled" class="h-notice" role="note">
          <p><b>Horae выключен для этого чата.</b> Теги не разбираются, блок состояния не уходит в промпт.
            Ниже — то, что накоплено раньше.</p>
          <button type="button" class="btn-primary" @click="hp.goto('settings')">Открыть настройки</button>
        </div>
        <dl class="h-kv">
          <div><dt>Время</dt><dd>{{ timeText || '—' }}</dd></div>
          <div><dt>Место</dt><dd>{{ st.scene.location || '—' }}
            <span v-if="st.scene.atmosphere" class="tag h-atm"><span class="sr-only">атмосфера: </span>{{ st.scene.atmosphere }}</span></dd></div>
        </dl>
        <p v-if="st.scene.desc" class="h-desc">{{ st.scene.desc }}</p>
        <p v-if="st.scene.parent_desc" class="h-desc h-desc-sub"><span class="h-meta">Вокруг: </span>{{ st.scene.parent_desc }}</p>

        <h4 class="h-h">Присутствуют</h4>
        <ul v-if="present.length" class="h-present">
          <li v-for="p in present" :key="p.name">
            <b>{{ p.name }}</b>
            <span v-if="p.costume" class="h-meta"><span aria-hidden="true">👕 </span><span class="sr-only">наряд: </span>{{ p.costume }}</span>
            <span v-if="p.mood" class="h-meta"><span aria-hidden="true">💭 </span><span class="sr-only">настроение: </span>{{ p.mood }}</span>
          </li>
        </ul>
        <p v-else class="muted">Никто не отмечен в сцене.</p>

        <h4 class="h-h">Предметы <span class="h-count">{{ st.items.length }}</span></h4>
        <div v-if="topItems.length" class="h-chips">
          <span v-for="it in topItems" :key="it.id || it.name" class="chip h-item-chip">
            {{ it.icon }} {{ it.name }}<span v-if="it.importance" class="sr-only"> ({{ it.importance === '!!' ? 'критичный' : 'важный' }})</span><span
              v-if="it.holder || it.location" class="h-meta"> · {{ it.holder }}<template v-if="it.location"> @ {{ it.location }}</template></span>
          </span>
          <button v-if="st.items.length > topItems.length" type="button" class="btn-ghost" @click="hp.goto('items')">Все предметы →</button>
        </div>
        <p v-else class="muted">Предметов нет.</p>

        <div class="h-stats">
          <p>≈ {{ fmtNum(view.stats.injection_tokens) }} {{ plural(view.stats.injection_tokens, 'токен', 'токена', 'токенов') }} в каждом ходе</p>
          <p class="h-meta">Ответов с данными: {{ fmtNum(view.stats.with_meta) }} · без данных: {{ fmtNum(view.stats.without_meta) }}<template
            v-if="view.stats.side"> · побочных сцен: {{ fmtNum(view.stats.side) }}</template> · свёрток: {{ fmtNum(view.stats.summaries) }}</p>
        </div>
        <div class="h-actions">
          <button type="button" @click="openScene">✎ Поправить сцену</button>
        </div>
        ${FORM_TAG}
      </div>`,
    methods: {
      openScene() {
        const st = this.st;
        const fields = [
          { key: "date", label: "Дата", value: st.time.date, placeholder: "2026/2/4" },
          { key: "time", label: "Время", value: st.time.time, placeholder: "15:00" },
          { key: "location", label: "Место", value: st.scene.location, hint: "Вложенные места — через «·»: Таверна·Зал." },
          { key: "atmosphere", label: "Атмосфера", value: st.scene.atmosphere },
          { key: "characters", label: "Присутствуют", value: st.scene.characters.join(", "), hint: "Имена через запятую." },
        ];
        this.present.forEach((p, i) => {
          fields.push({ key: "c" + i, label: "Наряд: " + p.name, value: p.costume });
          fields.push({ key: "m" + i, label: "Настроение: " + p.name, value: p.mood });
        });
        this.openForm("scene", {
          title: "Поправить сцену", fields,
          note: "Правка встаёт в журнал: следующие ответы ИИ могут её обновить, откатить её можно в «Настройках».",
        });
      },
      async onForm(v) {
        if (!this.form || this.form.kind !== "scene") return;
        const st = this.st;
        const patch = {};
        if (v.date !== st.time.date) patch.date = v.date;
        if (v.time !== st.time.time) patch.time = v.time;
        if (v.location !== st.scene.location) patch.location = v.location;
        if (v.atmosphere !== st.scene.atmosphere) patch.atmosphere = v.atmosphere;
        const chars = splitList(v.characters);
        if (chars.join("\n") !== st.scene.characters.join("\n")) patch.characters = chars;
        const hp = this.hp;
        let ok = true;
        if (Object.keys(patch).length) ok = await hp.op("scene.set", patch, { reload: false });
        const present = this.present;
        for (let i = 0; ok && i < present.length; i++) {
          if (v["c" + i] !== present[i].costume) ok = await hp.op("costume.set", { name: present[i].name, value: v["c" + i] }, { reload: false });
          if (ok && v["m" + i] !== present[i].mood) ok = await hp.op("mood.set", { name: present[i].name, value: v["m" + i] }, { reload: false });
        }
        await hp.reload();
        if (ok) { this.closeForm(); hp.toast("Сцена поправлена"); }
      },
      onFormDelete() {},
    },
  };

  // ==========================================================================
  // «Хронология»: планы, события, свёртки
  // ==========================================================================
  const SUMMARY_KIND = { auto: "авто", manual: "ручная", compress: "сжатие", carry: "перенос" };

  const HTimelineTab = {
    name: "HTimelineTab",
    mixins: [HBase, FormHost],
    components: { "h-form": HForm },
    data() {
      return { filter: "all", q: "", showCovered: false, selecting: false, sel: {}, selSum: {}, work: "" };
    },
    computed: {
      levels() { return LEVELS; },
      filters() { return [["all", "Все"], ["critical", "Ключевые"], ["important", "Важные"], ["normal", "Обычные"], ["summary", "Свёртки"]]; },
      activeSum() {
        const m = {};
        for (const it of this.view.timeline) if (it.kind === "summary") m[it.id] = it.active !== false;
        return m;
      },
      counts() {
        const c = { all: 0, critical: 0, important: 0, normal: 0, summary: 0 };
        for (const it of this.view.timeline) {
          c.all += 1;
          if (it.kind === "summary") c.summary += 1;
          else c[levelOf(it.level).id] += 1;
        }
        return c;
      },
      hiddenCount() {
        return this.view.timeline.filter((it) => it.kind === "event" && it.covered_by && this.activeSum[it.covered_by]).length;
      },
      // Новые сверху: читают хронологию, чтобы вспомнить, что было только что.
      items() {
        const q = this.q.trim().toLowerCase();
        return this.view.timeline.slice().reverse().filter((it) => {
          if (it.kind === "summary") {
            if (this.filter !== "all" && this.filter !== "summary") return false;
          } else {
            if (this.filter === "summary") return false;
            if (this.filter !== "all" && levelOf(it.level).id !== this.filter) return false;
            if (!this.showCovered && it.covered_by && this.activeSum[it.covered_by]) return false;
          }
          if (!q) return true;
          const hay = [it.text, it.date, it.time, it.date_from, it.date_to, it.rel,
            it.kind === "event" ? levelOf(it.level).label : "свёртка", it.mid != null ? "#" + it.mid : ""].join(" ").toLowerCase();
          return hay.indexOf(q) !== -1;
        });
      },
      selRefs() {
        return Object.keys(this.sel).filter((k) => this.sel[k]).map((k) => {
          const p = k.split(":");
          return { mid: Number(p[0]), i: Number(p[1]) };
        });
      },
      selIds() { return Object.keys(this.selSum).filter((k) => this.selSum[k]); },
      selCount() { return this.selRefs.length + this.selIds.length; },
      summaryError() {
        const e = this.view.stats.summary_error;
        if (!e) return "";
        const when = this.$root.fmtClock ? this.$root.fmtClock(e.at) : "";
        return "Авто-свёртка не удалась" + (e.message ? ": " + str(e.message).replace(/[.\s]+$/, "") : "")
          + (when ? " (" + when + ")" : "") + ". Следующая попытка — со следующим ходом или кнопкой «Свернуть сейчас».";
      },
    },
    template: `
      <div class="h-tab">
        <section class="h-block" aria-labelledby="h-agenda-h">
          <div class="h-block-head">
            <h4 id="h-agenda-h" class="h-h">Планы <span class="h-count">{{ st.agenda.length }}</span></h4>
            <button type="button" @click="addAgenda">＋ План</button>
          </div>
          <ul v-if="st.agenda.length" class="h-list">
            <li v-for="(a, i) in st.agenda" :key="'ag' + i" class="h-row">
              <span class="h-src" aria-hidden="true">{{ a.source === 'user' ? '👤' : '🤖' }}</span>
              <div class="h-row-main">
                <span class="sr-only">{{ a.source === 'user' ? 'Ваш пункт' : 'Пункт от ИИ' }}: </span>
                <span v-if="a.date" class="tag">{{ a.date }}</span> {{ a.text }}
              </div>
              <div class="h-acts">
                <button type="button" class="btn-icon" @click="editAgenda(a)" :aria-label="'Изменить план: ' + short(a.text)">✎</button>
                <button type="button" class="btn-icon" @click="deleteAgenda(a)" :aria-label="'Удалить план: ' + short(a.text)">🗑</button>
              </div>
            </li>
          </ul>
          <p v-else class="muted">Планов нет.</p>
        </section>

        <section class="h-block" aria-labelledby="h-events-h">
          <div class="h-block-head">
            <h4 id="h-events-h" class="h-h">События <span class="h-count">{{ counts.all - counts.summary }}</span></h4>
            <span class="h-grow"></span>
            <button type="button" @click="insertEvent()">＋ Событие</button>
            <button type="button" @click="addSummary">＋ Своя свёртка</button>
            <button type="button" :disabled="!!work" @click="runSummary">⚡ Свернуть сейчас</button>
          </div>
          <p v-if="summaryError" class="h-warn">⚠ {{ summaryError }}</p>
          <div class="h-filters" role="group" aria-label="Какие записи показывать">
            <button v-for="f in filters" :key="f[0]" type="button" class="filter-chip" :class="{ on: filter === f[0] }"
                    :aria-pressed="filter === f[0] ? 'true' : 'false'" @click="filter = f[0]">{{ f[1] }} {{ counts[f[0]] }}</button>
          </div>
          <div class="h-toolbar">
            <input type="search" v-model="q" class="h-search" placeholder="Поиск: текст, дата, #сообщение" aria-label="Поиск по хронологии" />
            <label v-if="hiddenCount" class="check h-inline"><input type="checkbox" v-model="showCovered" />
              показывать свёрнутые ({{ hiddenCount }})</label>
            <button type="button" :class="{ 'btn-primary': selecting }" :aria-pressed="selecting ? 'true' : 'false'"
                    @click="toggleSelecting">{{ selecting ? 'Готово' : 'Выбрать' }}</button>
          </div>
          <div v-if="selecting" class="h-selbar" role="group" aria-label="Действия с выбранными">
            <span>Выбрано: {{ selCount }}</span>
            <button type="button" class="btn-ghost" @click="selectVisible">Все видимые</button>
            <button type="button" :disabled="!selCount || !!work" @click="compressSelected">Сжать в свёртку</button>
            <button type="button" class="btn-danger" :disabled="!selCount || !!work" @click="deleteSelected">Удалить</button>
          </div>
          <p class="h-work" role="status" aria-live="polite">{{ work }}</p>

          <ul v-if="items.length" class="h-list h-tl">
            <template v-for="it in items" :key="it.kind === 'summary' ? 's' + it.id : 'e' + it.mid + ':' + it.i">
              <li v-if="it.kind === 'summary'" class="h-row h-sum" :class="{ 'h-sum-off': it.active === false }">
                <label v-if="selecting" class="h-sel"><input type="checkbox" v-model="selSum[it.id]"
                       :aria-label="'Выбрать свёртку #' + (it.range || [])[0] + '–#' + (it.range || [])[1]" /></label>
                <div class="h-row-main">
                  <div class="h-sum-head">
                    <b>{{ it.kind_detail === 'carry' ? 'Пересказ прошлого чата' : 'Свёртка L' + (it.depth || 1) }}</b>
                    <span class="tag">{{ sumKind(it) }}</span>
                    <span v-if="it.active === false" class="tag">раскрыта</span>
                  </div>
                  <div class="h-sum-text">{{ it.text }}</div>
                  <div class="h-meta">
                    <template v-if="it.range && it.kind_detail !== 'carry'">#{{ it.range[0] }}–#{{ it.range[1] }} · </template>
                    <template v-if="it.date_from || it.date_to">{{ it.date_from }}<template v-if="it.date_to && it.date_to !== it.date_from"> — {{ it.date_to }}</template> · </template>
                    <template v-if="it.events != null">{{ it.events }} {{ plural(it.events, 'событие', 'события', 'событий') }}</template>
                    <template v-if="it.rel"> · {{ it.rel }}</template>
                  </div>
                </div>
                <div v-if="!selecting" class="h-acts">
                  <button type="button" class="btn-icon" @click="editSummary(it)" aria-label="Изменить текст свёртки">✎</button>
                  <button type="button" class="btn-icon" :aria-pressed="it.active === false ? 'true' : 'false'" @click="toggleSummary(it)"
                          :title="it.active === false ? 'Свернуть снова' : 'Показать исходные события'"
                          :aria-label="it.active === false ? 'Свернуть снова' : 'Показать исходные события'">{{ it.active === false ? '⊟' : '⊞' }}</button>
                  <button type="button" class="btn-icon" @click="deleteSummary(it)" aria-label="Удалить свёртку">🗑</button>
                </div>
              </li>
              <li v-else class="h-row h-ev" :class="['h-lv-' + lv(it).id, { 'h-ev-covered': it.covered_by }]">
                <label v-if="selecting" class="h-sel"><input type="checkbox" v-model="sel[it.mid + ':' + it.i]"
                       :aria-label="'Выбрать событие: ' + short(it.text)" /></label>
                <span class="h-lvmark" aria-hidden="true" :title="lv(it).label">{{ lv(it).mark }}</span>
                <div class="h-row-main">
                  <div class="h-ev-text"><span class="sr-only">{{ lv(it).label }}: </span>{{ it.text }}</div>
                  <div class="h-meta">
                    <template v-if="it.date || it.time">{{ [it.date, it.time].filter(Boolean).join(' ') }} · </template>
                    <template v-if="it.rel">{{ it.rel }} · </template>
                    <button type="button" class="h-link" @click="hp.jump(it.mid)" :aria-label="'Перейти к сообщению ' + it.mid">сообщение #{{ it.mid }}</button>
                    <template v-if="it.covered_by"> · в свёртке</template>
                  </div>
                </div>
                <div v-if="!selecting" class="h-acts">
                  <button type="button" class="btn-icon" @click="editEvent(it)" :aria-label="'Изменить событие: ' + short(it.text)">✎</button>
                  <button type="button" class="btn-icon" @click="deleteEvent(it)" :aria-label="'Удалить событие: ' + short(it.text)">🗑</button>
                </div>
              </li>
            </template>
          </ul>
          <p v-else-if="view.timeline.length" class="muted">Под фильтр ничего не подходит.</p>
          <p v-else class="muted">Хронология пуста: события появятся, когда ИИ начнёт писать теги.</p>
        </section>
        ${FORM_TAG}
      </div>`,
    methods: {
      lv(it) { return levelOf(it.level); },
      sumKind(it) { return SUMMARY_KIND[it.kind_detail] || (it.auto ? "авто" : "ручная"); },
      toggleSelecting() {
        this.selecting = !this.selecting;
        if (!this.selecting) { this.sel = {}; this.selSum = {}; }
      },
      selectVisible() {
        for (const it of this.items) {
          if (it.kind === "summary") this.selSum[it.id] = true;
          else this.sel[it.mid + ":" + it.i] = true;
        }
      },
      // --- Планы ---
      addAgenda() {
        this.openForm("agenda.add", {
          title: "Новый план",
          fields: [
            { key: "date", label: "Дата (необязательно)", placeholder: "2026/2/10" },
            { key: "text", label: "Что запланировано", type: "textarea", required: true },
          ],
        });
      },
      editAgenda(a) {
        this.openForm("agenda.edit", {
          title: "План",
          fields: [
            { key: "date", label: "Дата (необязательно)", value: a.date },
            { key: "text", label: "Что запланировано", type: "textarea", value: a.text, required: true },
          ],
          danger: "Удалить",
        }, a);
      },
      async deleteAgenda(a) {
        const ok = await this.hp.confirm("Удалить план «" + short(a.text, 80) + "»? ИИ не сможет добавить его заново.", { okText: "Удалить" });
        if (ok && await this.hp.op("agenda.delete", { text: a.text })) this.closeForm();
      },
      // --- События ---
      insertEvent(near) {
        const mid = near ? near.mid : this.$root.lastAssistantId;
        this.openForm("event.add", {
          title: "Вставить событие",
          fields: [
            { key: "mid", label: "Номер сообщения", type: "number", value: mid == null ? "" : mid, required: true, min: 1, step: 1,
              hint: "К какому ответу привязать событие: оно встанет в хронологию на его место." },
            { key: "index", label: "Позиция внутри сообщения", type: "number", min: 0, step: 1,
              hint: "Пусто — в конец. 0 — первым." },
            { key: "level", label: "Уровень", type: "select", options: LEVEL_OPTIONS, value: "normal" },
            { key: "text", label: "Событие", type: "textarea", required: true },
          ],
          submitText: "Вставить",
        });
      },
      editEvent(it) {
        this.openForm("event.edit", {
          title: "Событие · сообщение #" + it.mid,
          fields: [
            { key: "level", label: "Уровень", type: "select", options: LEVEL_OPTIONS, value: levelOf(it.level).id },
            { key: "text", label: "Событие", type: "textarea", value: it.text, rows: 4,
              hint: "Пустой текст удалит событие." },
          ],
          danger: "Удалить",
        }, it);
      },
      async deleteRefs(refs, what) {
        const ok = await this.hp.confirm("Удалить " + what + "? Событие стирается из данных сообщения — в журнал правок оно не попадает.", { okText: "Удалить" });
        if (!ok) return false;
        const r = await this.hp.send("POST", this.hp.base + "/events/delete", { refs });
        if (r) await this.hp.reload();
        return !!r;
      },
      async deleteEvent(it) {
        if (await this.deleteRefs([{ mid: it.mid, i: it.i }], "событие «" + short(it.text, 60) + "»")) this.closeForm();
      },
      // --- Свёртки ---
      addSummary() {
        this.openForm("summary.add", {
          title: "Своя свёртка",
          note: "Свёртка заменит в промпте события сообщений диапазона. Исходные события остаются — свёртку можно раскрыть.",
          fields: [
            { key: "from_mid", label: "С сообщения #", type: "number", required: true, min: 1, step: 1 },
            { key: "to_mid", label: "По сообщение #", type: "number", required: true, min: 1, step: 1 },
            { key: "text", label: "Текст свёртки", type: "textarea", rows: 5, required: true },
          ],
        });
      },
      editSummary(it) {
        this.openForm("summary.edit", {
          title: "Текст свёртки", wide: true,
          fields: [{ key: "text", label: "Текст", type: "textarea", rows: 8, value: it.text, required: true }],
        }, it);
      },
      async toggleSummary(it) {
        const r = await this.hp.send("PATCH", this.hp.base + "/summaries/" + encodeURIComponent(it.id), { active: it.active === false });
        if (r) await this.hp.reload();
      },
      async deleteSummary(it) {
        const ok = await this.hp.confirm("Удалить свёртку? Её события вернутся в хронологию как были.", { okText: "Удалить" });
        if (!ok) return;
        const r = await this.hp.send("DELETE", this.hp.base + "/summaries/" + encodeURIComponent(it.id));
        if (r) await this.hp.reload();
      },
      async runSummary() {
        this.work = "Сворачиваю старую часть хронологии — это запрос к служебной модели…";
        const r = await this.hp.send("POST", this.hp.base + "/summaries/run", {});
        this.work = "";
        if (!r) return;
        if (r.job) { this.hp.trackJob(r.job); return; }
        this.hp.toast(r.message || (r.created ? "Готово: новых свёрток — " + r.created : "Свёртка выполнена"));
        await this.hp.reload();
      },
      compressSelected() {
        const n = this.selRefs.length;
        const s = this.selIds.length;
        this.openForm("compress", {
          title: "Сжать в свёртку",
          note: "Выбрано: " + n + " " + this.plural(n, "событие", "события", "событий")
            + (s ? " и " + s + " " + this.plural(s, "свёртка", "свёртки", "свёрток") : "")
            + ". Служебная модель напишет одну свёртку — это платный запрос, до минуты.",
          fields: [{ key: "mode", label: "Из чего писать", type: "select", value: "events",
            options: [["events", "Из текста событий — быстрее и дешевле"], ["fulltext", "Из полного текста сообщений — точнее"]] }],
          submitText: "Сжать",
        });
      },
      async deleteSelected() {
        const refs = this.selRefs;
        const ids = this.selIds;
        const what = (refs.length ? refs.length + " " + this.plural(refs.length, "событие", "события", "событий") : "")
          + (refs.length && ids.length ? " и " : "")
          + (ids.length ? ids.length + " " + this.plural(ids.length, "свёртку", "свёртки", "свёрток") : "");
        const ok = await this.hp.confirm("Удалить " + what + "? События стираются из данных сообщений, у свёрток вернутся исходные события.", { okText: "Удалить" });
        if (!ok) return;
        let good = true;
        if (refs.length) good = !!(await this.hp.send("POST", this.hp.base + "/events/delete", { refs }));
        for (const id of ids) {
          if (!good) break;
          good = !!(await this.hp.send("DELETE", this.hp.base + "/summaries/" + encodeURIComponent(id)));
        }
        this.sel = {};
        this.selSum = {};
        await this.hp.reload();
        if (good) this.hp.toast("Удалено: " + what);
      },
      async onForm(v) {
        const f = this.form;
        if (!f) return;
        const hp = this.hp;
        let ok = false;
        if (f.kind === "agenda.add") ok = await hp.op("agenda.add", { date: v.date, text: v.text });
        else if (f.kind === "agenda.edit") ok = await hp.op("agenda.edit", { text: f.ctx.text, new_text: v.text, date: v.date });
        else if (f.kind === "event.add") {
          if (v.mid == null) return;
          const body = { mid: v.mid, level: v.level, text: v.text };
          if (v.index != null) body.index = v.index;
          ok = !!(await hp.send("POST", hp.base + "/events", body));
          if (ok) await hp.reload();
        } else if (f.kind === "event.edit") {
          if (!v.text) { await this.deleteEvent(f.ctx); return; }
          ok = !!(await hp.send("PATCH", hp.base + "/events", { mid: f.ctx.mid, i: f.ctx.i, level: v.level, text: v.text }));
          if (ok) await hp.reload();
        } else if (f.kind === "summary.add") {
          if (v.from_mid > v.to_mid) { hp.toast("Начало диапазона позже конца"); return; }
          ok = !!(await hp.send("POST", hp.base + "/summaries", { from_mid: v.from_mid, to_mid: v.to_mid, text: v.text }));
          if (ok) await hp.reload();
        } else if (f.kind === "summary.edit") {
          ok = !!(await hp.send("PATCH", hp.base + "/summaries/" + encodeURIComponent(f.ctx.id), { text: v.text }));
          if (ok) await hp.reload();
        } else if (f.kind === "compress") {
          const refs = this.selRefs;
          const ids = this.selIds;
          const n = refs.length + ids.length;
          this.closeForm();
          this.work = "Сжимаю " + n + " " + this.plural(n, "запись", "записи", "записей") + " в свёртку…";
          const r = await hp.send("POST", hp.base + "/compress", { refs, summary_ids: ids, mode: v.mode });
          this.work = "";
          if (r) {
            this.sel = {};
            this.selSum = {};
            this.selecting = false;
            hp.toast("Свёртка готова");
            await hp.reload();
          }
          return;
        }
        if (ok) this.closeForm();
      },
      async onFormDelete() {
        const f = this.form;
        if (!f) return;
        if (f.kind === "agenda.edit") await this.deleteAgenda(f.ctx);
        else if (f.kind === "event.edit") await this.deleteEvent(f.ctx);
      },
    },
  };

  // ==========================================================================
  // «Персонажи»: присутствующие, расположение, NPC, отношения
  // ==========================================================================
  const HCharsTab = {
    name: "HCharsTab",
    mixins: [HBase, FormHost],
    components: { "h-form": HForm },
    data() { return { gender: "all", q: "", enriching: false }; },
    computed: {
      npcFields() { return NPC_FIELDS; },
      genders() { return [["all", "Все"], ["male", "Мужчины"], ["female", "Женщины"], ["other", "Другие"]]; },
      npcs() {
        const q = this.q.trim().toLowerCase();
        return this.st.npcs.filter((n) => {
          if (this.gender !== "all" && genderKind(n.gender) !== this.gender) return false;
          if (!q) return true;
          return [n.name, ...n.aliases, n.appearance, n.personality, n.relationship, n.job, n.race, n.note]
            .join(" ").toLowerCase().indexOf(q) !== -1;
        });
      },
      groups() {
        const g = [
          { id: "pinned", title: "Главные", list: [] },
          { id: "fav", title: "Отмеченные", list: [] },
          { id: "rest", title: "Остальные", list: [] },
        ];
        for (const n of this.npcs) (n.pinned ? g[0] : n.favorite ? g[1] : g[2]).list.push(n);
        return g.filter((x) => x.list.length);
      },
      showRel() { return !!this.settings.send_relationships || this.st.relationships.length > 0; },
      names() {
        const s = new Set();
        for (const n of this.st.npcs) s.add(n.name);
        for (const n of this.st.scene.characters) s.add(n);
        for (const a of this.st.affection) s.add(a.name);
        return Array.from(s).filter(Boolean).sort((a, b) => a.localeCompare(b, "ru"));
      },
    },
    template: `
      <div class="h-tab">
        <h4 class="h-h">В сцене</h4>
        <div v-if="st.scene.characters.length" class="h-chips">
          <span v-for="n in st.scene.characters" :key="n" class="chip">{{ n }}</span>
        </div>
        <p v-else class="muted">Никто не отмечен в сцене.</p>

        <section class="h-block" aria-labelledby="h-aff-h">
          <div class="h-block-head">
            <h4 id="h-aff-h" class="h-h">Расположение <span class="h-count">{{ st.affection.length }}</span></h4>
            <button type="button" @click="addAffection">＋ Добавить</button>
          </div>
          <ul v-if="st.affection.length" class="h-list">
            <li v-for="a in st.affection" :key="a.name" class="h-row">
              <div class="h-row-main"><b>{{ a.name }}</b> <span class="h-aff">{{ fmtAff(a.value) }}</span>
                <span v-if="a.level" class="tag">{{ a.level }}</span></div>
              <div class="h-acts">
                <button type="button" class="btn-icon" @click="editAffection(a)" :aria-label="'Изменить расположение: ' + a.name">✎</button>
                <button type="button" class="btn-icon" @click="deleteAffection(a)" :aria-label="'Удалить расположение: ' + a.name">🗑</button>
              </div>
            </li>
          </ul>
          <p v-else class="muted">Расположение ещё не отмечено.</p>
        </section>

        <section class="h-block" aria-labelledby="h-npc-h">
          <div class="h-block-head">
            <h4 id="h-npc-h" class="h-h">Персонажи <span class="h-count">{{ st.npcs.length }}</span></h4>
            <button type="button" @click="addNpc">＋ Персонаж</button>
          </div>
          <div class="h-filters" role="group" aria-label="Пол">
            <button v-for="g in genders" :key="g[0]" type="button" class="filter-chip" :class="{ on: gender === g[0] }"
                    :aria-pressed="gender === g[0] ? 'true' : 'false'" @click="gender = g[0]">{{ g[1] }}</button>
          </div>
          <input v-if="st.npcs.length > 6" type="search" v-model="q" class="h-search" placeholder="Поиск по имени и полям" aria-label="Поиск персонажей" />
          <template v-for="g in groups" :key="g.id">
            <h5 class="h-sub">{{ g.title }} <span class="h-count">{{ g.list.length }}</span></h5>
            <article v-for="n in g.list" :key="n.id || n.name" class="card h-npc" :aria-label="n.name">
              <div class="h-card-head">
                <div class="h-card-title">
                  <span class="h-gicon" aria-hidden="true">{{ gIcon(n) }}</span>
                  <b class="h-npc-name">{{ n.name }}</b>
                  <span v-if="n.id" class="h-meta">#{{ n.id }}</span>
                  <span v-if="n.present" class="tag">в сцене</span>
                </div>
                <div class="h-acts">
                <button type="button" class="btn-icon" :class="{ on: n.pinned }" :aria-pressed="n.pinned ? 'true' : 'false'"
                        @click="togglePin(n)" :title="n.pinned ? 'Главный персонаж' : 'Сделать главным'"
                        :aria-label="(n.pinned ? 'Убрать из главных: ' : 'Сделать главным: ') + n.name">📌</button>
                <button type="button" class="btn-icon" :aria-pressed="n.favorite ? 'true' : 'false'" @click="toggleFav(n)"
                        :title="n.favorite ? 'Отмечен' : 'Отметить'"
                        :aria-label="(n.favorite ? 'Снять отметку: ' : 'Отметить: ') + n.name">{{ n.favorite ? '★' : '☆' }}</button>
                <button type="button" class="btn-icon" @click="editNpc(n)" :aria-label="'Изменить персонажа: ' + n.name">✎</button>
                </div>
              </div>
              <dl class="h-dl">
                <template v-for="f in npcFields" :key="f[0]">
                  <div v-if="fieldVal(n, f[0])"><dt>{{ f[1] }}</dt><dd>{{ fieldVal(n, f[0]) }}</dd></div>
                </template>
              </dl>
              <p v-if="n.aliases.length" class="h-meta">Также: {{ n.aliases.join(', ') }}</p>
              <p v-if="n.first_mid" class="h-meta">Впервые в #{{ n.first_mid }}<template v-if="n.last_mid && n.last_mid !== n.first_mid"> · последний раз в #{{ n.last_mid }}</template></p>
            </article>
          </template>
          <p v-if="!st.npcs.length" class="muted">Персонажей пока нет: они появятся, когда ИИ опишет их в тегах, или добавьте вручную.</p>
          <p v-else-if="!npcs.length" class="muted">Под фильтр никто не подходит.</p>
        </section>

        <section v-if="showRel" class="h-block" aria-labelledby="h-rel-h">
          <div class="h-block-head">
            <h4 id="h-rel-h" class="h-h">Отношения <span class="h-count">{{ st.relationships.length }}</span></h4>
            <button type="button" @click="editRel(null)">＋ Связь</button>
          </div>
          <ul v-if="st.relationships.length" class="h-list">
            <li v-for="r in st.relationships" :key="r.from + '→' + r.to" class="h-row">
              <div class="h-row-main">{{ r.from }} → {{ r.to }}: <b>{{ r.type }}</b>
                <span v-if="r.note" class="h-meta"> ({{ r.note }})</span>
                <span v-if="r.user" class="h-meta" title="Правлено вами"> · 👤<span class="sr-only"> правлено вами</span></span></div>
              <div class="h-acts">
                <button type="button" class="btn-icon" @click="editRel(r)" :aria-label="'Изменить связь ' + r.from + ' → ' + r.to">✎</button>
                <button type="button" class="btn-icon" @click="deleteRel(r)" :aria-label="'Удалить связь ' + r.from + ' → ' + r.to">🗑</button>
              </div>
            </li>
          </ul>
          <p v-else class="muted">Связей нет.</p>
        </section>

        <h-form v-if="form" :key="form.id" :title="form.title" :fields="form.fields" :note="form.note"
                :danger="form.danger" :submit-text="form.submitText" :wide="form.wide" :busy="hp.busy || enriching"
                @submit="onForm" @delete="onFormDelete" @close="closeForm">
          <template #default="{ vals }">
            <div v-if="form.kind === 'npc.add'" class="h-enrich">
              <button type="button" :disabled="enriching || !vals.name" @click="enrich(vals)">{{ enriching ? 'Ищу в истории…' : '✨ Заполнить ИИ' }}</button>
              <span class="field-hint">Служебная модель прочтёт упоминания имени в чате и заполнит пустые поля.</span>
            </div>
          </template>
        </h-form>
      </div>`,
    methods: {
      gIcon(n) { return GENDER_ICON[genderKind(n.gender)]; },
      fieldVal(n, key) { return key === "age" ? str(n.age_display || n.age) : str(n[key]); },
      fmtAff(v) {
        const n = Number(v);
        if (!Number.isFinite(n)) return str(v);
        return (n > 0 ? "+" : "") + (Math.round(n * 10) / 10).toLocaleString("ru-RU");
      },
      npcFormFields(n) {
        n = n || {};
        return [
          { key: "name", label: "Имя", value: n.name || "", required: true },
          { key: "gender", label: "Пол", value: str(n.gender) },
          { key: "age", label: "Возраст", value: str(n.age), hint: n.age_display && n.age_display !== n.age ? "Сейчас по сюжету: " + n.age_display : "" },
          { key: "race", label: "Раса", value: str(n.race) },
          { key: "job", label: "Занятие", value: str(n.job) },
          { key: "birthday", label: "День рождения", value: str(n.birthday), placeholder: "гггг/мм/дд или мм/дд" },
          { key: "appearance", label: "Внешность", type: "textarea", value: str(n.appearance) },
          { key: "personality", label: "Характер", type: "textarea", value: str(n.personality) },
          { key: "relationship", label: "Отношение", value: str(n.relationship), hint: "Как относится к вашему герою." },
          { key: "note", label: "Примечание", type: "textarea", value: str(n.note) },
        ];
      },
      editNpc(n) {
        const fields = this.npcFormFields(n).concat([
          { key: "pinned", label: "Главный персонаж (его характер уходит в промпт)", type: "checkbox", value: !!n.pinned },
          { key: "favorite", label: "Отмечен", type: "checkbox", value: !!n.favorite },
        ]);
        this.openForm("npc.edit", { title: n.name, fields, danger: "Удалить персонажа", wide: true,
          note: "Новое имя переименует персонажа везде: в нарядах, расположении, отношениях и RPG." }, n);
      },
      addNpc() {
        const fields = this.npcFormFields(null);
        fields.splice(1, 0, { key: "aliases", label: "Прежние имена и прозвища", hint: "Через запятую." });
        this.openForm("npc.add", { title: "Новый персонаж", fields, submitText: "Добавить", wide: true });
      },
      async enrich(vals) {
        if (!vals.name || this.enriching) return;
        this.enriching = true;
        const r = await this.hp.send("POST", this.hp.base + "/npc_enrich", { name: vals.name, aliases: splitList(vals.aliases) });
        this.enriching = false;
        if (!r) return;
        const got = obj(r.fields);
        let filled = 0;
        let skipped = 0;
        for (const k of ["appearance", "personality", "relationship", "age", "gender"]) {
          const val = str(got[k]).trim();
          if (!val) continue;
          if (str(vals[k]).trim()) { skipped += 1; continue; }
          vals[k] = val;
          filled += 1;
        }
        const hits = r.hits != null ? " Упоминаний найдено: " + r.hits + "." : "";
        if (filled) this.hp.toast("✨ Заполнено полей: " + filled + "." + hits);
        else if (skipped) this.hp.toast("Поля уже заполнены — очистите те, что заменить." + hits);
        else this.hp.toast("ИИ ничего не нашёл об этом персонаже." + hits);
      },
      async togglePin(n) { await this.hp.op("npc.pin", { name: n.name, pinned: !n.pinned }); },
      async toggleFav(n) { await this.hp.op("npc.favorite", { name: n.name, favorite: !n.favorite }); },
      findNpc(name) {
        const low = str(name).trim().toLowerCase();
        return this.st.npcs.find((n) => n.name.toLowerCase() === low
          || n.aliases.some((a) => str(a).toLowerCase() === low)) || null;
      },
      // --- Расположение ---
      addAffection() {
        this.openForm("aff.add", { title: "Расположение", fields: [
          { key: "name", label: "Кто", required: true, list: this.names },
          { key: "value", label: "Значение", type: "number", step: 0.1, value: 0, required: true,
            hint: "От −100 (враг) до 100 (возлюбленный)." },
        ] });
      },
      editAffection(a) {
        this.openForm("aff.edit", { title: "Расположение: " + a.name, danger: "Удалить", fields: [
          { key: "value", label: "Значение", type: "number", step: 0.1, value: a.value, required: true,
            hint: "От −100 (враг) до 100 (возлюбленный). Сейчас: " + (a.level || "—") + "." },
        ] }, a);
      },
      async deleteAffection(a) {
        const ok = await this.hp.confirm("Удалить расположение «" + a.name + "»?", { okText: "Удалить" });
        if (ok && await this.hp.op("affection.delete", { name: a.name })) this.closeForm();
      },
      // --- Отношения ---
      editRel(r) {
        this.openForm(r ? "rel.edit" : "rel.add", {
          title: r ? "Связь" : "Новая связь", danger: r ? "Удалить" : "",
          fields: [
            { key: "from", label: "Кто", value: r ? r.from : "", required: true, list: this.names },
            { key: "to", label: "К кому", value: r ? r.to : "", required: true, list: this.names },
            { key: "type", label: "Связь", value: r ? r.type : "", required: true, placeholder: "друзья, соперники, брат…" },
            { key: "note", label: "Примечание", value: r ? r.note : "" },
          ],
          note: "Связь, заданная вами, не перезаписывается ИИ.",
        }, r);
      },
      async deleteRel(r) {
        const ok = await this.hp.confirm("Удалить связь «" + r.from + " → " + r.to + "»?", { okText: "Удалить" });
        if (ok && await this.hp.op("rel.delete", { from: r.from, to: r.to })) this.closeForm();
      },
      async onForm(v) {
        const f = this.form;
        if (!f) return;
        const hp = this.hp;
        let ok = false;
        if (f.kind === "npc.edit") {
          const n = f.ctx;
          let name = n.name;
          ok = true;
          if (v.name && v.name !== n.name) {
            ok = await hp.op("npc.rename", { from: n.name, to: v.name }, { reload: false });
            if (ok) name = v.name;
          }
          const fields = {};
          for (const [k] of NPC_FIELDS) if (str(v[k]) !== str(n[k])) fields[k] = v[k];
          if (ok && Object.keys(fields).length) ok = await hp.op("npc.set", { name, fields }, { reload: false });
          if (ok && v.pinned !== !!n.pinned) ok = await hp.op("npc.pin", { name, pinned: v.pinned }, { reload: false });
          if (ok && v.favorite !== !!n.favorite) ok = await hp.op("npc.favorite", { name, favorite: v.favorite }, { reload: false });
          await hp.reload();
        } else if (f.kind === "npc.add") {
          const dup = this.findNpc(v.name) || splitList(v.aliases).map((a) => this.findNpc(a)).find(Boolean);
          if (dup) {
            const open = await hp.confirm("«" + dup.name + "» уже есть в списке. Открыть его карточку вместо нового?",
              { title: "Такой персонаж есть", okText: "Открыть", cancelText: "Добавить всё равно", danger: false });
            if (open) { this.editNpc(dup); return; }
          }
          const fields = {};
          for (const [k] of NPC_FIELDS) if (str(v[k])) fields[k] = v[k];
          ok = await hp.op("npc.add", { name: v.name, fields, aliases: splitList(v.aliases) });
        } else if (f.kind === "aff.add") {
          if (v.value == null) return;
          ok = await hp.op("affection.set", { name: v.name, value: v.value });
        } else if (f.kind === "aff.edit") {
          if (v.value == null) return;
          ok = await hp.op("affection.set", { name: f.ctx.name, value: v.value });
        } else if (f.kind === "rel.add" || f.kind === "rel.edit") {
          const r = f.ctx;
          ok = true;
          if (r && (r.from !== v.from || r.to !== v.to)) ok = await hp.op("rel.delete", { from: r.from, to: r.to }, { reload: false });
          if (ok) ok = await hp.op("rel.set", { from: v.from, to: v.to, type: v.type, note: v.note }, { reload: false });
          await hp.reload();
        }
        if (ok) this.closeForm();
      },
      async onFormDelete() {
        const f = this.form;
        if (!f) return;
        if (f.kind === "npc.edit") {
          const n = f.ctx;
          const ok = await this.hp.confirm("Удалить «" + n.name + "»? Уйдут его наряд, настроение, расположение и место в сцене. Откатить можно в журнале правок («Настройки»).", { okText: "Удалить" });
          if (ok && await this.hp.op("npc.delete", { name: n.name })) this.closeForm();
        } else if (f.kind === "aff.edit") await this.deleteAffection(f.ctx);
        else if (f.kind === "rel.edit") await this.deleteRel(f.ctx);
      },
    },
  };

  // ==========================================================================
  // «Предметы»
  // ==========================================================================
  const HItemsTab = {
    name: "HItemsTab",
    mixins: [HBase, FormHost],
    components: { "h-form": HForm },
    data() { return { q: "", holder: "*", imp: "*", selecting: false, sel: {} }; },
    computed: {
      holders() {
        const s = new Set();
        for (const it of this.st.items) if (it.holder) s.add(it.holder);
        return Array.from(s).sort((a, b) => a.localeCompare(b, "ru"));
      },
      imps() { return [["*", "Все"], ["", "Обычные"], ["!", "Важные"], ["!!", "Критичные"]]; },
      // Фильтр важности сверяет с тем, что реально хранится ("" / "!" / "!!").
      // В плагине значения фильтра были китайскими словами и не совпадали ни с чем.
      list() {
        const q = this.q.trim().toLowerCase();
        return this.st.items.filter((it) => {
          if (this.holder !== "*" && str(it.holder) !== this.holder) return false;
          if (this.imp !== "*" && str(it.importance) !== this.imp) return false;
          if (!q) return true;
          return [it.name, it.holder, it.location, it.description].join(" ").toLowerCase().indexOf(q) !== -1;
        });
      },
      selNames() { return Object.keys(this.sel).filter((k) => this.sel[k]); },
      people() {
        const s = new Set(this.holders);
        for (const n of this.st.npcs) s.add(n.name);
        for (const n of this.st.scene.characters) s.add(n);
        return Array.from(s).filter(Boolean);
      },
    },
    template: `
      <div class="h-tab">
        <div class="h-block-head">
          <h4 class="h-h">Предметы <span class="h-count">{{ st.items.length }}</span></h4>
          <span class="h-grow"></span>
          <button type="button" @click="addItem">＋ Предмет</button>
          <button type="button" :class="{ 'btn-primary': selecting }" :aria-pressed="selecting ? 'true' : 'false'"
                  @click="toggleSelecting">{{ selecting ? 'Готово' : 'Выбрать' }}</button>
        </div>
        <div class="h-toolbar">
          <input type="search" v-model="q" class="h-search" placeholder="Поиск: предмет, владелец, место" aria-label="Поиск предметов" />
          <label class="h-inline-select">Владелец
            <select v-model="holder">
              <option value="*">все</option>
              <option v-for="h in holders" :key="h" :value="h">{{ h }}</option>
            </select></label>
        </div>
        <div class="h-filters" role="group" aria-label="Важность">
          <button v-for="o in imps" :key="o[0]" type="button" class="filter-chip" :class="{ on: imp === o[0] }"
                  :aria-pressed="imp === o[0] ? 'true' : 'false'" @click="imp = o[0]">{{ o[1] }}</button>
        </div>
        <div v-if="selecting" class="h-selbar" role="group" aria-label="Действия с выбранными">
          <span>Выбрано: {{ selNames.length }}</span>
          <button type="button" class="btn-ghost" @click="selectVisible">Все видимые</button>
          <button type="button" class="btn-danger" :disabled="!selNames.length || hp.busy" @click="deleteSelected">Удалить</button>
        </div>
        <ul v-if="list.length" class="h-list">
          <li v-for="it in list" :key="it.id || it.name" class="h-row h-item" :class="{ 'h-item-locked': it.locked }">
            <label v-if="selecting" class="h-sel"><input type="checkbox" v-model="sel[it.name]" :aria-label="'Выбрать: ' + it.name" /></label>
            <span class="h-icon" aria-hidden="true">{{ it.icon || '•' }}</span>
            <div class="h-row-main">
              <div><b>{{ it.name }}</b>
                <span v-if="it.importance" class="tag" :class="it.importance === '!!' ? 'h-imp-crit' : 'h-imp'">{{ impTag(it.importance) }}</span>
                <span v-if="it.locked" class="h-meta"> 🔒<span class="sr-only"> защищён от правок ИИ</span></span></div>
              <div v-if="it.holder || it.location" class="h-meta">{{ it.holder || '—' }}<template v-if="it.location"> · {{ it.location }}</template></div>
              <div v-if="it.description" class="h-item-desc">{{ it.description }}</div>
            </div>
            <div v-if="!selecting" class="h-acts">
              <button type="button" class="btn-icon" :class="{ on: it.locked }" :aria-pressed="it.locked ? 'true' : 'false'" @click="toggleLock(it)"
                      :title="it.locked ? 'Защищён: ИИ не меняет иконку, важность и описание' : 'Защитить от правок ИИ'"
                      :aria-label="(it.locked ? 'Снять защиту: ' : 'Защитить от правок ИИ: ') + it.name">{{ it.locked ? '🔒' : '🔓' }}</button>
              <button type="button" class="btn-icon" @click="editItem(it)" :aria-label="'Изменить: ' + it.name">✎</button>
              <button type="button" class="btn-icon" @click="deleteItem(it)" :aria-label="'Удалить: ' + it.name">🗑</button>
            </div>
          </li>
        </ul>
        <p v-else-if="st.items.length" class="muted">Под фильтр ничего не подходит.</p>
        <p v-else class="muted">Предметов нет: они появятся, когда ИИ отметит их в тегах, или добавьте вручную.</p>
        ${FORM_TAG}
      </div>`,
    methods: {
      impTag(i) { return IMPORTANCE_TAG[i] || ""; },
      toggleSelecting() { this.selecting = !this.selecting; if (!this.selecting) this.sel = {}; },
      selectVisible() { for (const it of this.list) this.sel[it.name] = true; },
      fields(it) {
        it = it || {};
        return [
          { key: "name", label: "Название", value: it.name || "", required: true, hint: "Количество — в скобках: Зелье (3 шт.)." },
          { key: "icon", label: "Иконка", value: it.icon || "", placeholder: "🗡" },
          { key: "importance", label: "Важность", type: "select", options: IMPORTANCE, value: it.importance || "" },
          { key: "holder", label: "У кого", value: it.holder || "", list: this.people },
          { key: "location", label: "Где лежит", value: it.location || "" },
          { key: "description", label: "Описание", type: "textarea", value: it.description || "" },
        ];
      },
      addItem() { this.openForm("item.add", { title: "Новый предмет", fields: this.fields(null), submitText: "Добавить" }); },
      editItem(it) {
        this.openForm("item.edit", { title: it.name, fields: this.fields(it), danger: "Удалить" }, it);
      },
      async toggleLock(it) { await this.hp.op("item.lock", { name: it.name, locked: !it.locked }); },
      async deleteItem(it) {
        const ok = await this.hp.confirm("Удалить «" + it.name + "»? Откатить можно в журнале правок («Настройки»).", { okText: "Удалить" });
        if (ok && await this.hp.op("item.delete", { name: it.name })) this.closeForm();
      },
      async deleteSelected() {
        const names = this.selNames;
        const n = names.length;
        const ok = await this.hp.confirm("Удалить " + n + " " + this.plural(n, "предмет", "предмета", "предметов") + "?", { okText: "Удалить" });
        if (!ok) return;
        let done = 0;
        for (const name of names) {
          if (!(await this.hp.op("item.delete", { name }, { reload: false }))) break;
          done += 1;
        }
        this.sel = {};
        await this.hp.reload();
        this.hp.toast("Удалено предметов: " + done);
      },
      async onForm(v) {
        const f = this.form;
        if (!f) return;
        const fields = { icon: v.icon, importance: v.importance, description: v.description, holder: v.holder, location: v.location };
        let ok = false;
        if (f.kind === "item.add") ok = await this.hp.op("item.add", { name: v.name, fields });
        else if (f.kind === "item.edit") {
          const body = { name: f.ctx.name, fields };
          if (v.name && v.name !== f.ctx.name) body.rename = v.name;
          ok = await this.hp.op("item.set", body);
        }
        if (ok) this.closeForm();
      },
      async onFormDelete() { if (this.form && this.form.kind === "item.edit") await this.deleteItem(this.form.ctx); },
    },
  };

  // ==========================================================================
  // «Сцены»: память мест
  // ==========================================================================
  const PATH_SEP = /\s*[·・]\s*/;

  const HScenesTab = {
    name: "HScenesTab",
    mixins: [HBase, FormHost],
    components: { "h-form": HForm },
    computed: {
      // Места группируются по первому звену пути «Таверна·Зал»: так видно,
      // что зал — часть таверны, а не отдельное место на карте.
      groups() {
        const by = new Map();
        for (const l of this.st.locations) {
          const parts = l.name.split(PATH_SEP).filter(Boolean);
          const root = parts[0] || l.name;
          if (!by.has(root)) by.set(root, { root, self: null, children: [] });
          const g = by.get(root);
          if (parts.length <= 1) g.self = l;
          else g.children.push(Object.assign({}, l, { leaf: parts.slice(1).join(" · ") }));
        }
        const cur = (g) => (g.self && g.self.current) || g.children.some((c) => c.current);
        return Array.from(by.values()).sort((a, b) => (cur(b) ? 1 : 0) - (cur(a) ? 1 : 0) || a.root.localeCompare(b.root, "ru"));
      },
      names() { return this.st.locations.map((l) => l.name); },
    },
    template: `
      <div class="h-tab">
        <div class="h-block-head">
          <h4 class="h-h">Память сцен <span class="h-count">{{ st.locations.length }}</span></h4>
          <span class="h-grow"></span>
          <button type="button" @click="addLoc">＋ Место</button>
          <button type="button" :disabled="st.locations.length < 2" @click="mergeLoc(null)">Объединить</button>
        </div>
        <p v-if="!settings.send_location_memory" class="field-hint">Описания мест сейчас не уходят ИИ — включите «Память сцен» в «Настройках → Что отправлять ИИ».</p>
        <article v-for="g in groups" :key="g.root" class="card h-loc">
          <div class="h-card-head">
            <div class="h-card-title">
              <b>{{ g.root }}</b>
              <span v-if="g.self && g.self.current" class="tag h-cur">здесь сейчас</span>
              <span v-if="g.self && g.self.user" class="h-meta" title="Правлено вами">👤<span class="sr-only"> правлено вами</span></span>
            </div>
            <div v-if="g.self" class="h-acts">
              <button type="button" class="btn-icon" @click="editLoc(g.self)" :aria-label="'Изменить место: ' + g.self.name">✎</button>
              <button type="button" class="btn-icon" @click="mergeLoc(g.self)" :aria-label="'Объединить место ' + g.self.name + ' с другим'">⇄</button>
              <button type="button" class="btn-icon" @click="deleteLoc(g.self)" :aria-label="'Удалить место: ' + g.self.name">🗑</button>
            </div>
          </div>
          <p v-if="g.self && g.self.desc" class="h-desc">{{ g.self.desc }}</p>
          <p v-else-if="g.self" class="h-meta">Описания нет.</p>
          <p v-if="g.self && (g.self.aliases.length || g.self.mid)" class="h-meta">
            <template v-if="g.self.aliases.length">Также: {{ g.self.aliases.join(', ') }}<template v-if="g.self.mid"> · </template></template>
            <template v-if="g.self.mid">обновлено в #{{ g.self.mid }}</template></p>
          <ul v-if="g.children.length" class="h-list h-loc-kids">
            <li v-for="c in g.children" :key="c.name" class="h-row">
              <div class="h-row-main">
                <div><b>· {{ c.leaf }}</b> <span v-if="c.current" class="tag h-cur">здесь сейчас</span>
                  <span v-if="c.user" class="h-meta" title="Правлено вами">👤<span class="sr-only"> правлено вами</span></span></div>
                <div v-if="c.desc" class="h-desc">{{ c.desc }}</div>
                <div v-if="c.aliases.length || c.mid" class="h-meta">
                  <template v-if="c.aliases.length">Также: {{ c.aliases.join(', ') }}<template v-if="c.mid"> · </template></template>
                  <template v-if="c.mid">обновлено в #{{ c.mid }}</template></div>
              </div>
              <div class="h-acts">
                <button type="button" class="btn-icon" @click="editLoc(c)" :aria-label="'Изменить место: ' + c.name">✎</button>
                <button type="button" class="btn-icon" @click="mergeLoc(c)" :aria-label="'Объединить место ' + c.name + ' с другим'">⇄</button>
                <button type="button" class="btn-icon" @click="deleteLoc(c)" :aria-label="'Удалить место: ' + c.name">🗑</button>
              </div>
            </li>
          </ul>
        </article>
        <p v-if="!st.locations.length" class="muted">Мест пока нет: ИИ описывает их тегом scene_desc, когда включена память сцен.</p>
        ${FORM_TAG}
      </div>`,
    methods: {
      addLoc() {
        this.openForm("loc.add", { title: "Новое место", submitText: "Добавить", fields: [
          { key: "name", label: "Название", required: true, hint: "Вложенное место — через «·»: Таверна·Зал." },
          { key: "desc", label: "Описание", type: "textarea", rows: 4, required: true },
        ] });
      },
      editLoc(l) {
        this.openForm("loc.edit", { title: l.name, danger: "Удалить", fields: [
          { key: "name", label: "Название", value: l.name, required: true, hint: "Новое имя переименует и вложенные места; старое станет псевдонимом." },
          { key: "desc", label: "Описание", type: "textarea", rows: 5, value: l.desc || "",
            hint: "Пустым описание не бывает — чтобы место забыть, удалите его." },
        ] }, l);
      },
      mergeLoc(l) {
        const opts = this.names.map((n) => [n, n]);
        const fields = [];
        if (!l) fields.push({ key: "from", label: "Какое место убрать", type: "select", options: opts, value: this.names[0] || "" });
        fields.push({ key: "to", label: l ? "Влить «" + l.name + "» в" : "Во что влить", type: "select",
          options: l ? opts.filter((o) => o[0] !== l.name) : opts, value: "" });
        this.openForm("loc.merge", { title: "Объединить места", fields, submitText: "Объединить",
          note: "Описание первого места допишется ко второму, а само первое место удалится." }, l);
      },
      async deleteLoc(l) {
        const ok = await this.hp.confirm("Удалить «" + l.name + "»? ИИ не создаст это место заново.", { okText: "Удалить" });
        if (ok && await this.hp.op("location.delete", { name: l.name })) this.closeForm();
      },
      async onForm(v) {
        const f = this.form;
        if (!f) return;
        const hp = this.hp;
        let ok = false;
        if (f.kind === "loc.add") ok = await hp.op("location.set", { name: v.name, desc: v.desc });
        else if (f.kind === "loc.edit") {
          const l = f.ctx;
          let name = l.name;
          ok = true;
          if (v.name && v.name !== l.name) {
            ok = await hp.op("location.rename", { from: l.name, to: v.name }, { reload: false });
            if (ok) name = v.name;
          }
          if (ok && v.desc !== str(l.desc)) {
            if (v.desc) ok = await hp.op("location.set", { name, desc: v.desc }, { reload: false });
            else { hp.toast("Описание не может быть пустым — чтобы забыть место, удалите его"); ok = false; }
          }
          await hp.reload();
        } else if (f.kind === "loc.merge") {
          const from = f.ctx ? f.ctx.name : v.from;
          if (!v.to || !from || v.to === from) { hp.toast("Выберите два разных места"); return; }
          ok = await hp.op("location.merge", { from, to: v.to });
        }
        if (ok) this.closeForm();
      },
      async onFormDelete() { if (this.form && this.form.kind === "loc.edit") await this.deleteLoc(this.form.ctx); },
    },
  };

  // ==========================================================================
  // «Таблицы»
  // ==========================================================================
  const SCOPES_TABLE = [["local", "Чат"], ["character", "Персонаж"], ["global", "Глобальная"]];
  const SCOPE_LABEL = { local: "Чат", character: "Персонаж", global: "Глобальная" };
  const SCOPE_CLASS = { local: "session", character: "character", global: "global" };
  const range = (n) => { const out = []; for (let i = 0; i < Math.max(0, Math.min(200, Number(n) || 0)); i++) out.push(i); return out; };

  const HTablesTab = {
    name: "HTablesTab",
    mixins: [HBase, FormHost],
    components: { "h-form": HForm },
    // menu — панель действий строки/столбца: {tid, type: row|col|corner, index}.
    // Не всплывающее меню, а полоса над таблицей: у всплывающего внутри
    // прокручиваемой таблицы пришлось бы считать координаты, а на пальце
    // долгое нажатие плагина не находил никто.
    data() { return { menu: null, lockMode: {} }; },
    computed: {
      scopes() { return SCOPES_TABLE; },
    },
    template: `
      <div class="h-tab">
        <div class="h-block-head">
          <h4 class="h-h">Таблицы <span class="h-count">{{ view.tables.length }}</span></h4>
          <span class="h-grow"></span>
          <button type="button" @click="addTable">＋ Таблица</button>
          <label class="btn">Импорт JSON
            <input type="file" accept="application/json,.json" class="file-input" @change="importFile" aria-label="Импорт таблицы из JSON" /></label>
        </div>
        <p class="field-hint">Строка 0 и столбец 0 — заголовки. ИИ заполняет таблицу тегом &lt;horaetable:имя&gt;; защищённые ячейки он не трогает. Действия со строкой или столбцом — кнопка «⋯» в заголовке.</p>
        <section v-for="t in view.tables" :key="t.id" class="card h-table" :aria-label="'Таблица ' + (t.name || 'без имени')">
          <div class="h-table-head">
            <span class="scope-tag" :class="scopeClass(t.scope)">{{ scopeLabel(t.scope) }}</span>
            <input class="h-table-name" :value="t.name" @change="patch(t, { name: $event.target.value.trim() })"
                   placeholder="Имя таблицы" aria-label="Имя таблицы" />
          </div>
          <div class="h-table-tools">
            <label class="h-inline-select">Где живёт
              <select :value="t.scope" @change="setScope(t, $event)">
                <option v-for="s in scopes" :key="s[0]" :value="s[0]"
                        :disabled="(s[0] === 'character' && !hp.characterId) || (s[0] === 'global' && !hp.isAdmin)">{{ s[1] }}</option>
              </select></label>
            <button type="button" :class="{ 'btn-primary': lockMode[t.id] }" :aria-pressed="lockMode[t.id] ? 'true' : 'false'"
                    @click="lockMode[t.id] = !lockMode[t.id]">🔒 Защита ячеек</button>
            <button type="button" @click="clearTable(t)">Очистить</button>
            <button type="button" @click="exportTable(t)">Экспорт</button>
            <button type="button" class="btn-danger" @click="deleteTable(t)">Удалить</button>
          </div>
          <label>Как заполнять (для ИИ)
            <input :value="t.prompt" @change="patch(t, { prompt: $event.target.value })" placeholder="Например: по строке на каждый квест, столбцы — цель, награда, статус" /></label>
          <p v-if="lockMode[t.id]" class="field-hint">Режим защиты: нажатие на ячейку включает и снимает защиту от ИИ.</p>
          <div v-if="menu && menu.tid === t.id" ref="menuBar" class="h-tmenu" role="group" :aria-label="menuTitle(t)" @keydown.esc.stop.prevent="closeMenu">
            <b>{{ menuTitle(t) }}</b>
            <template v-if="menu.type === 'corner'">
              <button type="button" @click="struct(t, 'add_row_below', 0)">＋ Строка</button>
              <button type="button" @click="struct(t, 'add_col_right', 0)">＋ Столбец</button>
            </template>
            <template v-else-if="menu.type === 'row'">
              <button type="button" @click="struct(t, 'add_row_above', menu.index)">＋ Выше</button>
              <button type="button" @click="struct(t, 'add_row_below', menu.index)">＋ Ниже</button>
              <button type="button" @click="lock(t, 'row', menu.index, null, !rowLocked(t, menu.index))">{{ rowLocked(t, menu.index) ? '🔓 Снять защиту' : '🔒 Защитить' }}</button>
              <button type="button" class="btn-danger" :disabled="t.rows <= 2" @click="struct(t, 'delete_row', menu.index)">Удалить строку</button>
            </template>
            <template v-else>
              <button type="button" @click="struct(t, 'add_col_left', menu.index)">＋ Левее</button>
              <button type="button" @click="struct(t, 'add_col_right', menu.index)">＋ Правее</button>
              <button type="button" @click="lock(t, 'col', null, menu.index, !colLocked(t, menu.index))">{{ colLocked(t, menu.index) ? '🔓 Снять защиту' : '🔒 Защитить' }}</button>
              <button type="button" class="btn-danger" :disabled="t.cols <= 2" @click="struct(t, 'delete_col', menu.index)">Удалить столбец</button>
            </template>
            <button type="button" class="btn-ghost" @click="closeMenu">Закрыть</button>
          </div>
          <div class="h-table-wrap" tabindex="0" role="region" :aria-label="'Ячейки таблицы ' + (t.name || '')">
            <table class="h-grid">
              <tbody>
                <tr v-for="r in rows(t)" :key="r">
                  <template v-for="c in cols(t)" :key="c">
                    <th v-if="r === 0 || c === 0" :scope="r === 0 && c > 0 ? 'col' : (c === 0 && r > 0 ? 'row' : null)"
                        :class="{ 'h-locked': (r === 0 && c > 0 && colLocked(t, c)) || (c === 0 && r > 0 && rowLocked(t, r)) }">
                      <div class="h-th">
                        <input :value="cell(t, r, c)" @change="setCell(t, r, c, $event)" :aria-label="cellLabel(t, r, c)" />
                        <span v-if="(r === 0 && c > 0 && colLocked(t, c)) || (c === 0 && r > 0 && rowLocked(t, r))" class="h-lockmark"
                              title="Защищено от ИИ">🔒<span class="sr-only"> защищено от ИИ</span></span>
                        <button type="button" class="h-th-more" :aria-expanded="isMenu(t, r, c) ? 'true' : 'false'"
                                @click="openMenu(t, r, c, $event)" :aria-label="menuBtnLabel(r, c)">⋯</button>
                      </div>
                    </th>
                    <td v-else :class="{ 'h-locked': anyLocked(t, r, c) }">
                      <button v-if="lockMode[t.id]" type="button" class="h-cell-lock" :aria-pressed="cellLocked(t, r, c) ? 'true' : 'false'"
                              @click="lock(t, 'cell', r, c, !cellLocked(t, r, c))" :aria-label="'Защита ячейки: ' + cellLabel(t, r, c)">
                        <span aria-hidden="true">{{ cellLocked(t, r, c) ? '🔒' : '🔓' }}</span> {{ cell(t, r, c) }}</button>
                      <div v-else class="h-td">
                        <input :value="cell(t, r, c)" @change="setCell(t, r, c, $event)"
                               :aria-label="cellLabel(t, r, c) + (anyLocked(t, r, c) ? ' (защищено от ИИ)' : '')" />
                        <span v-if="anyLocked(t, r, c)" class="h-lockmark" aria-hidden="true" title="Защищено от ИИ">🔒</span>
                      </div>
                    </td>
                  </template>
                </tr>
              </tbody>
            </table>
          </div>
        </section>
        <p v-if="!view.tables.length" class="muted">Таблиц нет. Таблица — это то, что ИИ ведёт сам: квесты, долги, расписание.</p>
        ${FORM_TAG}
      </div>`,
    methods: {
      scopeLabel(s) { return SCOPE_LABEL[s] || s; },
      scopeClass(s) { return SCOPE_CLASS[s] || ""; },
      rows(t) { return range(t.rows); },
      cols(t) { return range(t.cols); },
      cell(t, r, c) { return str(obj(t.data)[r + "," + c]); },
      rowLocked(t, r) { return arr(t.locked_rows).map(Number).indexOf(r) !== -1; },
      colLocked(t, c) { return arr(t.locked_cols).map(Number).indexOf(c) !== -1; },
      cellLocked(t, r, c) { return arr(t.locked_cells).indexOf(r + "," + c) !== -1; },
      anyLocked(t, r, c) { return this.rowLocked(t, r) || this.colLocked(t, c) || this.cellLocked(t, r, c); },
      cellLabel(t, r, c) {
        const rh = this.cell(t, r, 0);
        const ch = this.cell(t, 0, c);
        if (r === 0 && c === 0) return "Угловая ячейка";
        if (r === 0) return "Заголовок столбца " + c;
        if (c === 0) return "Заголовок строки " + r;
        return (rh || "строка " + r) + " / " + (ch || "столбец " + c);
      },
      menuBtnLabel(r, c) {
        if (r === 0 && c === 0) return "Добавить строку или столбец";
        return r === 0 ? "Действия со столбцом " + c : "Действия со строкой " + r;
      },
      menuTitle(t) {
        const m = this.menu;
        if (!m) return "";
        if (m.type === "corner") return "Таблица";
        return m.type === "row" ? "Строка " + m.index + (this.cell(t, m.index, 0) ? " «" + short(this.cell(t, m.index, 0), 20) + "»" : "")
          : "Столбец " + m.index + (this.cell(t, 0, m.index) ? " «" + short(this.cell(t, 0, m.index), 20) + "»" : "");
      },
      isMenu(t, r, c) {
        const m = this.menu;
        if (!m || m.tid !== t.id) return false;
        if (r === 0 && c === 0) return m.type === "corner";
        return r === 0 ? m.type === "col" && m.index === c : m.type === "row" && m.index === r;
      },
      openMenu(t, r, c, ev) {
        if (this.isMenu(t, r, c)) { this.closeMenu(); return; }
        this._menuRet = ev && ev.currentTarget;
        this.menu = { tid: t.id, type: r === 0 && c === 0 ? "corner" : (r === 0 ? "col" : "row"), index: r === 0 ? c : r };
        this.$nextTick(() => {
          const bar = this.$el.querySelector(".h-tmenu");
          const b = bar && bar.querySelector("button");
          if (b) b.focus();
        });
      },
      closeMenu() {
        this.menu = null;
        const r = this._menuRet;
        this._menuRet = null;
        if (r && document.contains(r)) r.focus();
      },
      async patch(t, body, reload) {
        const r = await this.hp.send("PATCH", this.hp.base + "/tables/" + encodeURIComponent(t.id), body);
        if (r && reload !== false) await this.hp.reload();
        return !!r;
      },
      // Ячейка пишется по change (ушли из поля), а не по вводу: иначе каждая
      // буква была бы запросом. Состояние целиком не перечитываем — иначе
      // перерисовка сбивала бы набор в соседней ячейке.
      async setCell(t, r, c, ev) {
        const input = ev.target;
        const value = input.value;
        if (value === this.cell(t, r, c)) return;
        const ok = await this.patch(t, { cell: { r, c, value } }, false);
        if (ok) this.hp.setLocalCell(t.id, r + "," + c, value);
        else input.value = this.cell(t, r, c);
      },
      async struct(t, op, index) {
        if (op === "delete_row" || op === "delete_col") {
          const what = op === "delete_row" ? "строку " + index : "столбец " + index;
          const ok = await this.hp.confirm("Удалить " + what + " вместе с данными?", { okText: "Удалить" });
          if (!ok) return;
        }
        this.menu = null;
        await this.patch(t, { structure: { op, index } });
        this._menuRet = null;
      },
      async lock(t, type, r, c, locked) {
        const lk = { type, locked };
        if (r != null) lk.r = r;
        if (c != null) lk.c = c;
        await this.patch(t, { lock: lk });
      },
      async setScope(t, ev) {
        const scope = ev.target.value;
        if (scope === t.scope) return;
        const warn = scope === "global" ? "Таблица станет общей: её заголовки появятся во всех чатах, данные у каждого чата свои."
          : scope === "character" ? "Таблица перейдёт в профиль персонажа: её заголовки появятся во всех его чатах."
          : "Таблица станет только этого чата.";
        const ok = await this.hp.confirm(warn, { title: "Сменить область таблицы", okText: "Сменить", danger: false });
        if (!ok) { ev.target.value = t.scope; return; }
        if (!(await this.patch(t, { scope }))) ev.target.value = t.scope;
      },
      async clearTable(t) {
        const ok = await this.hp.confirm("Очистить данные таблицы «" + (t.name || "без имени") + "»? Заголовки останутся.", { okText: "Очистить" });
        if (ok) await this.patch(t, { clear: true });
      },
      exportTable(t) {
        if (this.$root.downloadJson) this.$root.downloadJson(t, "horae_table_" + safeName(t.name) + ".json");
      },
      async deleteTable(t) {
        const extra = t.scope === "global" ? " Она исчезнет из всех чатов." : t.scope === "character" ? " Она исчезнет из всех чатов персонажа." : "";
        const ok = await this.hp.confirm("Удалить таблицу «" + (t.name || "без имени") + "»?" + extra, { okText: "Удалить" });
        if (!ok) return;
        const r = await this.hp.send("DELETE", this.hp.base + "/tables/" + encodeURIComponent(t.id));
        if (r) await this.hp.reload();
      },
      addTable() {
        const scopes = SCOPES_TABLE.filter((s) => (s[0] !== "character" || this.hp.characterId) && (s[0] !== "global" || this.hp.isAdmin));
        this.openForm("table.add", { title: "Новая таблица", submitText: "Создать", fields: [
          { key: "name", label: "Имя", required: true, hint: "По нему ИИ пишет тег: <horaetable:Имя>." },
          { key: "rows", label: "Строк (с заголовком)", type: "number", value: 4, min: 2, max: 100, step: 1, required: true },
          { key: "cols", label: "Столбцов (с заголовком)", type: "number", value: 4, min: 2, max: 30, step: 1, required: true },
          { key: "scope", label: "Где живёт", type: "select", options: scopes, value: "local" },
          { key: "prompt", label: "Как заполнять (для ИИ)", type: "textarea", rows: 2 },
        ] });
      },
      async importFile(ev) {
        let data;
        try { data = await readJsonFile(ev); } catch (e) { this.hp.toast("⚠ Это не JSON: " + (e.message || e)); return; }
        if (!data) return;
        const table = data.table && typeof data.table === "object" ? data.table : data;
        const r = await this.hp.send("POST", this.hp.base + "/tables/import", { table });
        if (r) { this.hp.toast("Таблица импортирована"); await this.hp.reload(); }
      },
      async onForm(v) {
        const f = this.form;
        if (!f || f.kind !== "table.add") return;
        const rows = Math.max(2, Math.min(100, Math.round(num(v.rows, 4))));
        const cols = Math.max(2, Math.min(30, Math.round(num(v.cols, 4))));
        const r = await this.hp.send("POST", this.hp.base + "/tables", { name: v.name, rows, cols, prompt: v.prompt, scope: v.scope });
        if (r) { this.closeForm(); await this.hp.reload(); }
      },
      onFormDelete() {},
    },
  };

  // ==========================================================================
  // «RPG»
  // ==========================================================================
  const rand = () => {
    try { const a = new Uint32Array(1); window.crypto.getRandomValues(a); return a[0] / 4294967296; } catch (e) { return Math.random(); }
  };

  // Кубики. «2к6» тоже понимаем: так пишут в русских системах.
  const HDice = {
    name: "HDice",
    data() { return { expr: "1d20", result: "", last: "", did: uid("hd") }; },
    template: `
      <section class="h-block h-dice" :aria-labelledby="did">
        <h4 :id="did" class="h-h">🎲 Кубики</h4>
        <div class="h-chips" role="group" aria-label="Быстрый бросок">
          <button v-for="d in [4, 6, 8, 10, 12, 20, 100]" :key="d" type="button" @click="quick(d)">d{{ d }}</button>
        </div>
        <form class="h-dice-row" @submit.prevent="roll">
          <label class="h-grow">Бросок
            <input v-model="expr" placeholder="2d6+1" autocomplete="off" :aria-describedby="did + '-hint'" /></label>
          <button type="submit" class="btn-primary">Бросить</button>
        </form>
        <span :id="did + '-hint'" class="field-hint">NdX±M: 1–20 кубиков, 2–1000 граней, модификатор до ±99.</span>
        <p class="h-dice-out" role="status" aria-live="polite">{{ result }}</p>
        <button v-if="last" type="button" @click="insert">Вставить в поле ввода</button>
      </section>`,
    methods: {
      quick(d) {
        const m = /^\s*(\d*)\s*[dдк]\s*\d+(.*)$/i.exec(this.expr);
        this.expr = (m && m[1] ? m[1] : "1") + "d" + d + (m ? m[2].trim() : "");
        this.roll();
      },
      roll() {
        const m = /^\s*(\d{0,2})\s*[dдк]\s*(\d{1,4})\s*(?:([+\-−])\s*(\d{1,2}))?\s*$/i.exec(this.expr);
        if (!m) { this.result = "Не понял бросок — пишите как 2d6+1"; this.last = ""; return; }
        const n = Math.min(20, Math.max(1, Number(m[1] || 1)));
        const sides = Math.min(1000, Math.max(2, Number(m[2])));
        const mod = m[3] ? (m[3] === "+" ? 1 : -1) * Number(m[4]) : 0;
        const rolls = [];
        for (let i = 0; i < n; i++) rolls.push(1 + Math.floor(rand() * sides));
        const sum = rolls.reduce((a, b) => a + b, 0) + mod;
        const modText = mod ? (mod > 0 ? "+" : "−") + Math.abs(mod) : "";
        this.last = "🎲 " + n + "d" + sides + modText + " = [" + rolls.join(", ") + "]" + modText + " = " + sum;
        this.result = this.last;
      },
      insert() {
        const root = this.$root;
        const cur = str(root.input).replace(/\s+$/, "");
        root.input = cur ? cur + "\n" + this.last : this.last;
        if (root.showToast) root.showToast("Бросок — в поле ввода");
      },
    },
  };

  const EMPTY_RPG = { bars: {}, status: {}, skills: {}, attrs: {}, reputation: {}, equipment: {}, levels: {}, xp: {}, currency: {}, strongholds: [] };
  const pct = (cur, max, min) => {
    const lo = Number(min) || 0;
    const span = (Number(max) || 0) - lo;
    if (!(span > 0)) return 0;
    return Math.max(0, Math.min(100, ((Number(cur) || 0) - lo) / span * 100));
  };
  // «atk=5, def=-1» → {atk: 5, def: -1}
  const parseAttrs = (s) => {
    const out = {};
    for (const part of str(s).split(/[,;\n]/)) {
      const m = /^\s*([^=:\s]+)\s*[=:]\s*([+-]?\d+)\s*$/.exec(part);
      if (m) out[m[1]] = Number(m[2]);
    }
    return out;
  };
  const fmtAttrs = (a) => Object.entries(obj(a)).map(([k, v]) => k + (Number(v) >= 0 ? "+" : "") + v).join(", ");

  const HRpgTab = {
    name: "HRpgTab",
    mixins: [HBase, FormHost],
    components: { "h-form": HForm, "h-dice": HDice, "h-rows": HRows },
    data() { return { cfg: null, cfgOwner: "", templates: null, tplPick: "" }; },
    computed: {
      on() { return !!this.settings.rpg_enabled; },
      rpg() { return this.st.rpg || EMPTY_RPG; },
      mods() {
        const s = this.settings;
        return {
          bars: s.rpg_bars !== false, skills: s.rpg_skills !== false, attrs: s.rpg_attrs !== false,
          reputation: !!s.rpg_reputation, equipment: !!s.rpg_equipment, level: !!s.rpg_level,
          currency: !!s.rpg_currency, stronghold: !!s.rpg_stronghold,
        };
      },
      barCfg() { return arr(this.settings.rpg_bar_config); },
      attrCfg() { return arr(this.settings.rpg_attr_config); },
      names() {
        const s = new Set(this.owners);
        for (const n of this.st.npcs) s.add(n.name);
        for (const n of this.st.scene.characters) s.add(n);
        return Array.from(s).filter(Boolean);
      },
      owners() {
        const r = this.rpg;
        const s = new Set();
        for (const k of ["bars", "status", "skills", "attrs", "reputation", "equipment", "levels", "xp", "currency"]) {
          for (const o of Object.keys(obj(r[k]))) s.add(o);
        }
        const present = this.st.scene.characters;
        return Array.from(s).sort((a, b) => (present.indexOf(b) !== -1) - (present.indexOf(a) !== -1) || a.localeCompare(b, "ru"));
      },
      cards() { return this.owners.map((o) => this.card(o)); },
      tree() {
        const list = arr(this.rpg.strongholds).filter((s) => s && s.name);
        const byId = new Map(list.map((s) => [String(s.id), s]));
        const byName = new Map(list.map((s) => [s.name, s]));
        const parentOf = (s) => (s.parent == null || s.parent === "" ? null : (byId.get(String(s.parent)) || byName.get(s.parent) || null));
        const kids = new Map();
        const roots = [];
        for (const s of list) {
          const p = parentOf(s);
          if (p && p !== s) { if (!kids.has(p)) kids.set(p, []); kids.get(p).push(s); } else roots.push(s);
        }
        const out = [];
        const seen = new Set();
        const walk = (s, depth, path) => {
          if (seen.has(s) || depth > 12) return;
          seen.add(s);
          const p = path ? path + ">" + s.name : s.name;
          out.push({ id: s.id, name: s.name, level: s.level, desc: s.desc, depth, path: p });
          for (const k of kids.get(s) || []) walk(k, depth + 1, p);
        };
        roots.forEach((r) => walk(r, 0, ""));
        return out;
      },
      repCfg() { return this.view.rpg_config.reputation; },
      curCfg() { return this.view.rpg_config.currencies; },
      ownerSlots() {
        const c = this.cfg && this.cfg.chars[this.cfgOwner.trim()];
        return c && Array.isArray(c.slots) ? c.slots : null;
      },
    },
    watch: { cfgOwner() { this.ensureOwner(); } },
    template: `
      <div class="h-tab">
        <template v-if="!on">
          <div class="h-notice" role="note">
            <p><b>RPG выключен.</b> Когда он включён, ИИ ведёт тегом &lt;horaerpg&gt; шкалы здоровья и маны, статусы,
              навыки, атрибуты, снаряжение, репутацию и деньги — и всё это видно и правится здесь.
              Правила RPG добавляют в каждый ход около 1–2 тысяч токенов.</p>
            <div class="h-actions">
              <button type="button" class="btn-primary" :disabled="hp.busy" @click="hp.enableRpg">Включить для этого чата</button>
              <button type="button" @click="hp.goto('settings', 'rpg')">Выбрать модули…</button>
            </div>
          </div>
        </template>
        <template v-else>
          <div class="h-block-head">
            <h4 class="h-h">Персонажи <span class="h-count">{{ owners.length }}</span></h4>
            <span class="h-grow"></span>
            <button type="button" @click="addOwner">＋ Персонаж</button>
          </div>
          <p v-if="!owners.length" class="muted">RPG-данных пока нет: они появятся, когда ИИ начнёт писать &lt;horaerpg&gt;, или добавьте персонажа.</p>
          <article v-for="c in cards" :key="c.owner" class="card h-rpg-card" :aria-label="c.owner">
            <div class="h-rpg-head">
              <b>{{ c.owner }}</b>
              <span v-if="c.present" class="tag">в сцене</span>
              <span v-if="c.level != null" class="tag">Ур. {{ c.level }}</span>
              <span class="h-grow"></span>
              <button v-if="mods.level" type="button" class="btn-icon" @click="editLevel(c)" :aria-label="'Уровень и опыт: ' + c.owner">✎</button>
            </div>
            <div v-if="mods.level && c.xp" class="h-bar-row">
              <span class="h-bar-l">Опыт</span>
              <span class="h-bar" aria-hidden="true"><span class="h-bar-fill" :style="{ width: pctOf(c.xp.cur, c.xp.max) + '%' }"></span></span>
              <span class="h-bar-v">{{ c.xp.cur }}/{{ c.xp.max }}</span>
            </div>
            <template v-if="mods.bars">
              <div v-for="b in c.bars" :key="b.key" class="h-bar-row">
                <span class="h-bar-l">{{ b.label }}</span>
                <span class="h-bar" aria-hidden="true"><span class="h-bar-fill" :style="{ width: b.pct + '%', background: b.color || null }"></span></span>
                <button type="button" class="h-bar-v h-link" @click="editBar(c.owner, b)" :aria-label="'Изменить ' + b.label + ' (' + c.owner + '): ' + b.cur + ' из ' + b.max">{{ b.cur }}/{{ b.max }}</button>
              </div>
              <div class="h-chips h-status">
                <span class="h-meta">Эффекты:</span>
                <span v-for="(s, i) in c.status" :key="i" class="chip">{{ s }}</span>
                <span v-if="!c.status.length" class="h-meta">нет</span>
                <button type="button" class="btn-icon" @click="editStatus(c)" :aria-label="'Изменить эффекты: ' + c.owner">✎</button>
                <button type="button" class="btn-ghost h-small" @click="addBar(c.owner)">＋ шкала</button>
              </div>
            </template>
            <details v-if="mods.attrs" class="h-det">
              <summary>Атрибуты <span class="h-count">{{ c.attrs.filter(a => a.set).length }}</span></summary>
              <svg v-if="c.radar" class="h-radar" viewBox="0 0 320 210" role="img" :aria-label="'Атрибуты ' + c.owner + ': ' + c.attrs.map(a => a.name + ' ' + a.value).join(', ')">
                <polygon v-for="(g, gi) in c.radar.grid" :key="'g' + gi" :points="g" class="h-radar-grid" />
                <line v-for="(a, ai) in c.radar.axes" :key="'a' + ai" x1="160" y1="105" :x2="a.x" :y2="a.y" class="h-radar-axis" />
                <polygon :points="c.radar.data" class="h-radar-data" />
                <text v-for="(l, li) in c.radar.labels" :key="'l' + li" :x="l.x" :y="l.y" :text-anchor="l.anchor" class="h-radar-label">{{ l.text }}</text>
              </svg>
              <ul class="h-list">
                <li v-for="a in c.attrs" :key="a.key" class="h-row h-attr">
                  <span class="h-bar-l">{{ a.name }}</span>
                  <span class="h-bar" aria-hidden="true"><span class="h-bar-fill" :style="{ width: (a.set ? a.value : 0) + '%' }"></span></span>
                  <button type="button" class="h-bar-v h-link" @click="editAttr(c.owner, a)" :aria-label="'Изменить ' + a.name + ' (' + c.owner + '): ' + (a.set ? a.value : 'не задано')">{{ a.set ? a.value : '—' }}</button>
                </li>
              </ul>
            </details>
            <details v-if="mods.skills" class="h-det">
              <summary>Навыки <span class="h-count">{{ c.skills.length }}</span></summary>
              <ul v-if="c.skills.length" class="h-list">
                <li v-for="s in c.skills" :key="s.name" class="h-row">
                  <div class="h-row-main"><b>{{ s.name }}</b><span v-if="s.level" class="tag">Ур. {{ s.level }}</span>
                    <span v-if="s.user" class="h-meta" title="Добавлен вами"> 👤<span class="sr-only"> добавлен вами</span></span>
                    <div v-if="s.desc" class="h-meta">{{ s.desc }}</div></div>
                  <div class="h-acts"><button type="button" class="btn-icon" @click="deleteSkill(c.owner, s)" :aria-label="'Удалить навык ' + s.name">🗑</button></div>
                </li>
              </ul>
              <button type="button" class="h-add" @click="addSkill(c.owner)">＋ Навык</button>
            </details>
            <details v-if="mods.equipment" class="h-det">
              <summary>Снаряжение <span class="h-count">{{ c.equipCount }}</span></summary>
              <ul v-if="c.equip.length" class="h-list">
                <li v-for="sl in c.equip" :key="sl.slot" class="h-row">
                  <div class="h-row-main"><span class="h-meta">{{ sl.slot }}<template v-if="sl.max > 1"> ×{{ sl.max }}</template>:</span>
                    <span v-if="!sl.items.length" class="h-meta"> пусто</span>
                    <span v-for="it in sl.items" :key="it.name" class="chip h-eq">{{ it.name }}<span v-if="fmtAttrs(it.attrs)" class="h-meta"> {{ fmtAttrs(it.attrs) }}</span>
                      <button type="button" class="btn-icon h-chip-x" @click="unequip(c.owner, sl.slot, it)" :aria-label="'Снять ' + it.name">✕</button></span></div>
                </li>
              </ul>
              <p v-else class="h-meta">Слотов нет — задайте их в «Настройке RPG» ниже или наденьте предмет.</p>
              <button type="button" class="h-add" @click="equip(c)">＋ Надеть</button>
            </details>
            <details v-if="mods.reputation" class="h-det">
              <summary>Репутация <span class="h-count">{{ c.rep.length }}</span></summary>
              <ul v-if="c.rep.length" class="h-list">
                <li v-for="r in c.rep" :key="r.cat" class="h-row h-attr">
                  <span class="h-bar-l">{{ r.cat }}</span>
                  <span class="h-bar" aria-hidden="true"><span class="h-bar-fill" :style="{ width: r.pct + '%' }"></span></span>
                  <button type="button" class="h-bar-v h-link" @click="editRep(c.owner, r)" :aria-label="'Изменить репутацию ' + r.cat + ': ' + r.value">{{ r.value }}</button>
                </li>
              </ul>
              <button type="button" class="h-add" @click="editRep(c.owner, null)">＋ Репутация</button>
            </details>
            <details v-if="mods.currency" class="h-det">
              <summary>Деньги <span class="h-count">{{ c.money.length }}</span></summary>
              <div class="h-chips">
                <button v-for="m in c.money" :key="m.name" type="button" class="chip h-money" @click="editMoney(c.owner, m)"
                        :aria-label="'Изменить ' + m.name + ': ' + m.value">{{ m.emoji }} {{ m.name }} × {{ m.value }}</button>
                <button type="button" class="h-add" @click="editMoney(c.owner, null)">＋ Валюта</button>
              </div>
            </details>
          </article>

          <section v-if="mods.stronghold" class="h-block" aria-labelledby="h-base-h">
            <div class="h-block-head">
              <h4 id="h-base-h" class="h-h">Опорные пункты <span class="h-count">{{ tree.length }}</span></h4>
              <button type="button" @click="editBase(null, null)">＋ Пункт</button>
            </div>
            <ul v-if="tree.length" class="h-list">
              <li v-for="b in tree" :key="b.path" class="h-row" :style="{ paddingLeft: Math.min(b.depth, 6) * 14 + 'px' }">
                <div class="h-row-main"><b>{{ b.depth ? '└ ' : '' }}{{ b.name }}</b> <span v-if="b.level != null && b.level !== ''" class="tag">Ур. {{ b.level }}</span>
                  <div v-if="b.desc" class="h-meta">{{ b.desc }}</div></div>
                <div class="h-acts">
                  <button type="button" class="btn-icon" @click="editBase(null, b)" :aria-label="'Добавить внутрь ' + b.name">＋</button>
                  <button type="button" class="btn-icon" @click="editBase(b, null)" :aria-label="'Изменить ' + b.name">✎</button>
                  <button type="button" class="btn-icon" @click="deleteBase(b)" :aria-label="'Удалить ' + b.name">🗑</button>
                </div>
              </li>
            </ul>
            <p v-else class="muted">Опорных пунктов нет.</p>
          </section>

          <details class="h-det h-cfg" @toggle="onCfgToggle">
            <summary>Настройка RPG этого чата</summary>
            <template v-if="cfg">
              <h-rows :rows="cfg.reputation" legend="Категории репутации" add-text="Категория" item-name="категорию"
                      :cols="[{ key: 'name', label: 'Название' }, { key: 'min', label: 'Мин.', type: 'number', narrow: true }, { key: 'max', label: 'Макс.', type: 'number', narrow: true }, { key: 'default', label: 'Старт', type: 'number', narrow: true }, { key: 'sub', label: 'Подкатегории через запятую', wide: true }]"
                      :blank="{ name: '', min: -100, max: 100, default: 0, sub: '' }"></h-rows>
              <h-rows :rows="cfg.currencies" legend="Валюты" add-text="Валюта" item-name="валюту"
                      :cols="[{ key: 'emoji', label: 'Значок', narrow: true }, { key: 'name', label: 'Название' }, { key: 'rate', label: 'Курс', type: 'number', narrow: true }]"
                      :blank="{ emoji: '💰', name: '', rate: 1 }"></h-rows>
              <fieldset class="h-fs">
                <legend>Слоты снаряжения</legend>
                <label class="check"><input type="checkbox" v-model="cfg.locked" /> Запретить ИИ создавать новые слоты</label>
                <label>Персонаж
                  <input v-model="cfgOwner" :list="'h-rpg-owners'" placeholder="Имя" autocomplete="off" /></label>
                <datalist id="h-rpg-owners"><option v-for="n in names" :key="n" :value="n"></option></datalist>
                <template v-if="ownerSlots">
                  <h-rows :rows="ownerSlots" :legend="'Слоты: ' + cfgOwner.trim()" add-text="Слот" item-name="слот"
                          :cols="[{ key: 'name', label: 'Слот' }, { key: 'max', label: 'Сколько вещей', type: 'number', narrow: true }]"
                          :blank="{ name: '', max: 1 }"></h-rows>
                  <div class="h-actions">
                    <button type="button" @click="loadTemplates">Шаблоны…</button>
                    <template v-if="templates">
                      <select v-model="tplPick" aria-label="Шаблон слотов">
                        <option value="">— шаблон —</option>
                        <option v-for="(t, i) in templates" :key="i" :value="String(i)">{{ t.builtin ? '⭐ ' : '' }}{{ t.name || ('Шаблон ' + (i + 1)) }}</option>
                      </select>
                      <button type="button" :disabled="tplPick === ''" @click="applyTemplate">Применить</button>
                      <button type="button" @click="saveTemplate">Сохранить как шаблон</button>
                    </template>
                  </div>
                </template>
              </fieldset>
              <div class="h-actions">
                <button type="button" class="btn-primary" :disabled="hp.busy" @click="saveCfg">Сохранить настройку</button>
                <button type="button" class="btn-ghost" @click="resetCfg">Сбросить правки</button>
              </div>
              <p class="field-hint">Шкалы (HP/MP) и атрибуты настраиваются в «Настройках → RPG»: они общие для всех чатов выбранного уровня.</p>
            </template>
          </details>
        </template>
        <h-dice></h-dice>
        ${FORM_TAG}
      </div>`,
    methods: {
      pctOf(c, m) { return pct(c, m); },
      fmtAttrs(a) { return fmtAttrs(a); },
      card(o) {
        const r = this.rpg;
        const bars = Object.entries(obj(r.bars[o])).map(([key, b]) => {
          b = obj(b);
          const cfg = this.barCfg.find((x) => x.key === key) || {};
          const max = num(b.max, num(cfg.max, 100));
          const cur = num(b.cur, 0);
          return { key, label: str(b.label || cfg.name || key.toUpperCase()), cur, max, color: str(cfg.color), pct: pct(cur, max) };
        });
        const have = obj(r.attrs[o]);
        const keys = this.attrCfg.map((a) => a.key);
        for (const k of Object.keys(have)) if (keys.indexOf(k) === -1) keys.push(k);
        const attrs = keys.map((key) => {
          const cfg = this.attrCfg.find((a) => a.key === key) || {};
          return { key, name: str(cfg.name || key), value: num(have[key], 0), set: own(have, key) };
        });
        const set = attrs.filter((a) => a.set);
        const xp = arr(r.xp[o]);
        const equip = this.equipOf(o);
        return {
          owner: o, present: this.st.scene.characters.indexOf(o) !== -1,
          level: r.levels[o] != null ? r.levels[o] : null,
          xp: xp.length ? { cur: num(xp[0], 0), max: num(xp[1], 0) } : null,
          bars, status: arr(r.status[o]).map(str), attrs,
          radar: set.length >= 3 ? this.radar(set) : null,
          skills: arr(r.skills[o]).filter((s) => s && s.name),
          equip, equipCount: equip.reduce((n, s) => n + s.items.length, 0),
          rep: this.repOf(o), money: this.moneyOf(o),
        };
      },
      radar(attrs) {
        // Поле 320×210, центр (160, 105): по 90px с боков под подписи вроде
        // «Интеллект 40» — в поле 240 они срезались краем картинки.
        const n = attrs.length;
        const cx = 160;
        const cy = 105;
        const R = 64;
        const pt = (i, f) => {
          const a = -Math.PI / 2 + (i * 2 * Math.PI) / n;
          return [cx + Math.cos(a) * R * f, cy + Math.sin(a) * R * f];
        };
        const poly = (vals) => vals.map((f, i) => pt(i, f).map((x) => x.toFixed(1)).join(",")).join(" ");
        return {
          grid: [poly(attrs.map(() => 1)), poly(attrs.map(() => 0.5))],
          axes: attrs.map((_, i) => { const p = pt(i, 1); return { x: p[0].toFixed(1), y: p[1].toFixed(1) }; }),
          data: poly(attrs.map((a) => Math.max(0, Math.min(100, a.value)) / 100)),
          labels: attrs.map((a, i) => {
            const p = pt(i, 1.16);
            const anchor = Math.abs(p[0] - cx) < 6 ? "middle" : (p[0] > cx ? "start" : "end");
            return { x: p[0].toFixed(1), y: (p[1] + 4).toFixed(1), anchor, text: short(a.name, 10) + " " + a.value };
          }),
        };
      },
      equipOf(o) {
        const conf = obj(this.view.rpg_config.equipment.chars[o]);
        const have = obj(this.rpg.equipment[o]);
        const slots = arr(conf.slots).filter((s) => s && s.name).map((s) => ({ slot: s.name, max: num(s.max, 1), items: [] }));
        for (const [slot, items] of Object.entries(have)) {
          let s = slots.find((x) => x.slot === slot);
          if (!s) { s = { slot, max: 1, items: [] }; slots.push(s); }
          s.items = arr(items).filter((x) => x && x.name).map((x) => ({ name: x.name, attrs: obj(x.attrs) }));
        }
        return slots;
      },
      repOf(o) {
        const have = obj(this.rpg.reputation[o]);
        const cats = this.repCfg.map((c) => c.name).filter(Boolean);
        for (const k of Object.keys(have)) if (cats.indexOf(k) === -1) cats.push(k);
        return cats.filter((cat) => own(have, cat)).map((cat) => {
          const cfg = this.repCfg.find((c) => c.name === cat) || {};
          const min = num(cfg.min, -100);
          const max = num(cfg.max, 100);
          const value = num(obj(have[cat]).value, num(have[cat], 0));
          return { cat, value, min, max, pct: pct(value, max, min) };
        });
      },
      moneyOf(o) {
        return Object.entries(obj(this.rpg.currency[o])).map(([name, value]) => {
          const cfg = this.curCfg.find((c) => c.name === name) || {};
          return { name, value: num(value, 0), emoji: str(cfg.emoji || "💰") };
        });
      },
      // --- Правки ---
      addOwner() {
        const fields = [{ key: "owner", label: "Имя", required: true, list: this.names.filter((n) => this.owners.indexOf(n) === -1) }];
        if (this.mods.level) fields.push({ key: "level", label: "Уровень", type: "number", value: 1, min: 0, step: 1 });
        this.openForm("owner.add", { title: "Персонаж в RPG", fields, submitText: "Добавить",
          note: this.mods.level ? "" : "Персонаж появится со шкалой «" + ((this.barCfg[0] && this.barCfg[0].name) || "HP") + "» на максимуме." });
      },
      editLevel(c) {
        this.openForm("level", { title: "Уровень: " + c.owner, fields: [
          { key: "level", label: "Уровень", type: "number", value: c.level == null ? "" : c.level, min: 0, step: 1 },
          { key: "cur", label: "Опыт сейчас", type: "number", value: c.xp ? c.xp.cur : "", min: 0, step: 1 },
          { key: "max", label: "Опыта до следующего уровня", type: "number", value: c.xp ? c.xp.max : "", min: 0, step: 1,
            hint: "Обычно уровень × 100." },
        ] }, c);
      },
      editBar(owner, b) {
        this.openForm("bar", { title: b.label + ": " + owner, fields: [
          { key: "cur", label: "Сейчас", type: "number", value: b.cur, step: 1, required: true },
          { key: "max", label: "Максимум", type: "number", value: b.max, min: 1, step: 1, required: true },
        ] }, { owner, key: b.key });
      },
      addBar(owner) {
        const opts = this.barCfg.map((b) => [b.key, b.name + " (" + b.key + ")"]);
        if (!opts.length) { this.hp.toast("Шкалы не настроены — добавьте их в «Настройках → RPG»"); return; }
        this.openForm("bar.add", { title: "Шкала: " + owner, fields: [
          { key: "key", label: "Шкала", type: "select", options: opts, value: opts[0][0] },
          { key: "cur", label: "Сейчас", type: "number", step: 1, value: num(this.barCfg[0].max, 100), required: true },
          { key: "max", label: "Максимум", type: "number", min: 1, step: 1, value: num(this.barCfg[0].max, 100), required: true },
        ] }, { owner });
      },
      editStatus(c) {
        this.openForm("status", { title: "Эффекты: " + c.owner, fields: [
          { key: "effects", label: "Эффекты", type: "textarea", value: c.status.join("\n"), hint: "По одному в строке. Пусто — эффектов нет." },
        ] }, c);
      },
      editAttr(owner, a) {
        const cfg = this.attrCfg.find((x) => x.key === a.key) || {};
        this.openForm("attr", { title: a.name + ": " + owner, fields: [
          { key: "value", label: "Значение (0–100)", type: "number", min: 0, max: 100, step: 1, value: a.set ? a.value : "", required: true, hint: str(cfg.desc) },
        ] }, { owner, key: a.key });
      },
      addSkill(owner) {
        this.openForm("skill", { title: "Навык: " + owner, submitText: "Добавить", fields: [
          { key: "name", label: "Название", required: true },
          { key: "level", label: "Уровень", placeholder: "3 или «мастер»" },
          { key: "desc", label: "Что даёт", type: "textarea", rows: 2 },
        ] }, { owner });
      },
      async deleteSkill(owner, s) {
        const ok = await this.hp.confirm("Удалить навык «" + s.name + "» у " + owner + "? ИИ не вернёт его сам.", { okText: "Удалить" });
        if (ok) await this.hp.op("rpg.skill.delete", { owner, name: s.name });
      },
      equip(c) {
        const slots = c.equip.map((s) => s.slot);
        this.openForm("equip", { title: "Надеть: " + c.owner, submitText: "Надеть", fields: [
          { key: "slot", label: "Слот", required: true, list: slots, value: slots[0] || "" },
          { key: "name", label: "Предмет", required: true },
          { key: "attrs", label: "Характеристики", placeholder: "atk=5, def=-1", hint: "Целые числа через запятую." },
        ] }, c);
      },
      async unequip(owner, slot, it) {
        const ok = await this.hp.confirm("Снять «" + it.name + "»? Вещь вернётся в предметы.", { okText: "Снять", danger: false });
        if (ok) await this.hp.op("rpg.unequip", { owner, slot, name: it.name });
      },
      editRep(owner, r) {
        const cats = this.repCfg.map((c) => c.name).filter(Boolean);
        const fields = [];
        if (!r) fields.push({ key: "cat", label: "Категория", required: true, list: cats });
        fields.push({ key: "value", label: "Значение", type: "number", step: 1, value: r ? r.value : 0, required: true,
          hint: r ? "От " + r.min + " до " + r.max + "." : "" });
        this.openForm("rep", { title: "Репутация: " + owner + (r ? " · " + r.cat : ""), fields }, { owner, cat: r ? r.cat : "" });
      },
      editMoney(owner, m) {
        const names = this.curCfg.map((c) => c.name).filter(Boolean);
        const fields = [];
        if (!m) fields.push({ key: "name", label: "Валюта", required: true, list: names });
        fields.push({ key: "value", label: "Сколько", type: "number", step: 1, value: m ? m.value : 0, required: true });
        this.openForm("money", { title: "Деньги: " + owner + (m ? " · " + m.name : ""), fields }, { owner, name: m ? m.name : "" });
      },
      editBase(b, parent) {
        const fields = [];
        if (!b) fields.push({ key: "name", label: "Название", required: true, hint: parent ? "Внутри «" + parent.name + "»." : "" });
        fields.push({ key: "level", label: "Уровень", type: "number", min: 0, step: 1, value: b && b.level != null ? b.level : "" });
        fields.push({ key: "desc", label: "Описание", type: "textarea", value: b ? str(b.desc) : "" });
        this.openForm("base", { title: b ? b.name : "Новый опорный пункт", fields, submitText: b ? "Сохранить" : "Добавить" }, { b, parent });
      },
      async deleteBase(b) {
        const ok = await this.hp.confirm("Удалить «" + b.name + "» со всем, что внутри?", { okText: "Удалить" });
        if (ok) await this.hp.op("rpg.base.delete", { path: b.path });
      },
      async onForm(v) {
        const f = this.form;
        if (!f) return;
        const hp = this.hp;
        const ctx = f.ctx || {};
        let ok = false;
        switch (f.kind) {
          case "owner.add":
            if (this.mods.level) ok = await hp.op("rpg.level", { owner: v.owner, value: num(v.level, 1) });
            else if (this.barCfg.length) {
              const b = this.barCfg[0];
              ok = await hp.op("rpg.bar", { owner: v.owner, key: b.key, cur: num(b.max, 100), max: num(b.max, 100) });
            } else ok = await hp.op("rpg.status", { owner: v.owner, effects: [] });
            break;
          case "level": {
            const owner = ctx.owner;
            ok = true;
            const lv = v.level;
            if (lv != null && lv !== ctx.level) ok = await hp.op("rpg.level", { owner, value: lv }, { reload: false });
            const xp = ctx.xp || {};
            if (ok && v.cur != null && v.max != null && (v.cur !== xp.cur || v.max !== xp.max)) {
              ok = await hp.op("rpg.xp", { owner, cur: v.cur, max: v.max }, { reload: false });
            }
            await hp.reload();
            break;
          }
          case "bar":
          case "bar.add":
            if (v.cur == null || v.max == null) return;
            ok = await hp.op("rpg.bar", { owner: ctx.owner, key: f.kind === "bar" ? ctx.key : v.key, cur: v.cur, max: v.max });
            break;
          case "status":
            ok = await hp.op("rpg.status", { owner: ctx.owner, effects: splitList(str(v.effects).replace(/\//g, "\n")) });
            break;
          case "attr":
            if (v.value == null) return;
            ok = await hp.op("rpg.attr", { owner: ctx.owner, key: ctx.key, value: Math.max(0, Math.min(100, Math.round(v.value))) });
            break;
          case "skill":
            ok = await hp.op("rpg.skill.add", { owner: ctx.owner, name: v.name, level: v.level, desc: v.desc });
            break;
          case "equip":
            ok = await hp.op("rpg.equip", { owner: ctx.owner, slot: v.slot, name: v.name, attrs: parseAttrs(v.attrs) });
            break;
          case "rep":
            if (v.value == null) return;
            ok = await hp.op("rpg.rep", { owner: ctx.owner, cat: ctx.cat || v.cat, value: v.value });
            break;
          case "money":
            if (v.value == null) return;
            ok = await hp.op("rpg.currency", { owner: ctx.owner, name: ctx.name || v.name, value: v.value });
            break;
          case "base": {
            const b = ctx.b;
            const path = b ? b.path : (ctx.parent ? ctx.parent.path + ">" : "") + str(v.name).replace(/>/g, " ");
            const body = { path };
            if (v.level != null) body.level = v.level;
            if (!b || v.desc !== str(b.desc)) body.desc = v.desc;
            ok = await hp.op("rpg.base", body);
            break;
          }
          default:
            return;
        }
        if (ok) this.closeForm();
      },
      onFormDelete() {},
      // --- Настройка RPG чата (rpg_config) ---
      onCfgToggle(ev) { if (ev.target.open && !this.cfg) this.resetCfg(); },
      resetCfg() {
        const rc = this.view.rpg_config;
        const chars = {};
        for (const [o, c] of Object.entries(clone(rc.equipment.chars) || {})) {
          const cc = obj(c);
          chars[o] = Object.assign({}, cc, { slots: arr(cc.slots).map((s) => ({ name: str(obj(s).name), max: num(obj(s).max, 1) })) });
        }
        this.cfg = {
          reputation: rc.reputation.map((r) => ({ name: str(r.name), min: num(r.min, -100), max: num(r.max, 100),
            default: num(r.default, 0), sub: arr(r.sub).join(", ") })),
          currencies: rc.currencies.map((c) => ({ emoji: str(c.emoji || "💰"), name: str(c.name), rate: num(c.rate, 1) })),
          locked: !!rc.equipment.locked,
          chars,
        };
        if (!this.cfgOwner) this.cfgOwner = this.owners[0] || this.st.scene.characters[0] || "";
        this.ensureOwner();
      },
      // Запись персонажа заводится здесь, а не при отрисовке: правка
      // реактивных данных посреди рендера заставила бы Vue рисовать дважды.
      ensureOwner() {
        const o = this.cfgOwner.trim();
        if (!this.cfg || !o) return;
        if (!this.cfg.chars[o]) this.cfg.chars[o] = { slots: [], forms: [], form: "" };
        if (!Array.isArray(this.cfg.chars[o].slots)) this.cfg.chars[o].slots = [];
      },
      async saveCfg() {
        const c = this.cfg;
        const chars = {};
        for (const [o, cc] of Object.entries(c.chars)) {
          const slots = arr(cc.slots).filter((s) => str(s.name).trim()).map((s) => ({ name: str(s.name).trim(), max: Math.max(1, Math.round(num(s.max, 1))) }));
          if (!slots.length && !arr(cc.forms).length) continue;
          chars[o] = Object.assign({}, cc, { slots });
        }
        const body = {
          reputation: c.reputation.filter((r) => str(r.name).trim()).map((r) => ({
            name: str(r.name).trim(), min: num(r.min, -100), max: num(r.max, 100), default: num(r.default, 0), sub: splitList(r.sub) })),
          currencies: c.currencies.filter((x) => str(x.name).trim()).map((x) => ({
            name: str(x.name).trim(), rate: Math.max(1, num(x.rate, 1)), emoji: str(x.emoji).trim() || "💰" })),
          equipment: { locked: !!c.locked, chars },
        };
        // Сервер держит в настройке и служебные списки (deleted_skills и др.):
        // без них в теле PUT удалённые навыки и валюты вернулись бы. Берём
        // их из свежей настройки, а не из копии в состоянии.
        const cur = await this.hp.send("GET", this.hp.base + "/rpg_config");
        if (!cur) return;
        const r = await this.hp.send("PUT", this.hp.base + "/rpg_config", Object.assign({}, obj(cur), body));
        if (!r) return;
        this.hp.toast("Настройка RPG сохранена");
        await this.hp.reload();
        this.resetCfg();
      },
      // Сервер отдаёт {builtin, custom}: встроенные расы (⭐, не меняются) и свои.
      // Слоты шаблона живут в формах: {forms: [{id, name, slots}]} — у кицунэ и
      // оборотня форм несколько, у остальных одна «Обычная».
      async loadTemplates() {
        const r = await this.hp.send("GET", "/horae/equipment_templates");
        if (!r) return;
        const mark = (list, builtin) => arr(list).filter((t) => t && typeof t === "object").map((t) => Object.assign({}, t, { builtin }));
        this._customTpl = mark(r.custom, false);
        this.templates = mark(r.builtin, true).concat(this._customTpl);
        if (!this.templates.length) this.hp.toast("Шаблонов пока нет — сохраните слоты как шаблон");
      },
      applyTemplate() {
        const t = this.templates && this.templates[Number(this.tplPick)];
        const o = this.cfgOwner.trim();
        if (!t || !o) return;
        const forms = arr(t.forms).filter((f) => f && typeof f === "object");
        const first = forms[0] || {};
        const slots = arr(t.slots).length ? arr(t.slots) : arr(first.slots);
        this.cfg.chars[o] = {
          slots: slots.map((x) => ({ name: str(obj(x).name), max: num(obj(x).max, 1) })),
          forms: clone(forms), form: str(first.id || ""), template: str(t.id || t.name || ""),
        };
        this.hp.toast("Шаблон «" + (t.name || "") + "» применён — сохраните настройку");
      },
      async saveTemplate() {
        const o = this.cfgOwner.trim();
        const name = await this.hp.prompt("Имя шаблона", { value: o, okText: "Сохранить" });
        if (!name || !name.trim()) return;
        const cc = obj(this.cfg.chars[o]);
        const slots = arr(cc.slots).filter((x) => str(x.name).trim()).map((x) => ({ name: str(x.name).trim(), max: Math.max(1, Math.round(num(x.max, 1))) }));
        const tpl = { id: "u_" + Date.now().toString(36), name: name.trim(), aliases: [],
          forms: [{ id: "base", name: "Обычная", slots }] };
        const custom = arr(this._customTpl).filter((t) => t.name !== tpl.name).map((t) => { const c = Object.assign({}, t); delete c.builtin; return c; });
        custom.push(tpl);
        const r = await this.hp.send("PUT", "/horae/equipment_templates", { custom });
        if (r) { this.hp.toast("Шаблон сохранён"); await this.loadTemplates(); }
      },
    },
  };

  // ==========================================================================
  // «Настройки»
  // ==========================================================================
  // Описание полей по группам (ключи — спека §7). Подписи — что делает
  // настройка, подсказка — чем она обернётся (цена, побочный эффект).
  const SETTINGS_GROUPS = [
    { id: "main", title: "Основное", fields: [
      { key: "enabled", type: "bool", label: "Horae включён", hint: "Главный выключатель: без него теги не разбираются, а состояние не уходит в промпт." },
      { key: "parse_tags", type: "bool", label: "Разбирать теги в ответах", hint: "Модель пишет <horae> в конце ответа; теги вырезаются из текста и становятся данными." },
      { key: "inject_state", type: "bool", label: "Блок состояния в каждом ходе", hint: "Время, место, присутствующие, предметы и хронология — в хвост промпта." },
      { key: "rules_position", type: "choice", label: "Где правила тегов", options: [["system", "В системном промпте — кэшируется, дешевле"], ["tail", "В хвосте — модель слушается лучше, но платите каждый ход"]] },
      { key: "tag_reminder", type: "bool", label: "Напоминание формата в хвосте", hint: "Одна строка перед ответом: модель реже забывает теги в длинном чате." },
      { key: "auto_analyze", type: "choice", label: "ИИ-анализ ответов без тегов", options: [
        ["off", "выключен"],
        ["gaps", "только пропуски (модель обычно пишет теги, но иногда забывает)"],
        ["always", "всегда (дороже: +1 запрос на каждый ответ без тегов)"]],
        hint: "Служебная модель допишет данные ответа в фоне." },
      { key: "anti_paraphrase", type: "bool", label: "Режим «без пересказа»", hint: "Данные о вашей реплике модель пишет в свой ответ, а не пересказывает её." },
      { key: "aux_model", type: "str", label: "Модель служебных запросов", models: true, placeholder: "например gemini-2.5-flash", hint: "Анализ, скан, свёртки. Пусто — модель сводки, затем модель чата." },
      { key: "aux_delay_ms", type: "int", label: "Пауза между служебными запросами, мс", min: 0, max: 60000, step: 100, hint: "Бережёт лимит запросов провайдера." },
      { key: "strip_tags", type: "str", label: "Вырезать перед разбором", placeholder: "details, summary", hint: "Свои теги через запятую: их содержимое не разбирается и не ищется." },
    ] },
    { id: "send", title: "Что отправлять ИИ", fields: [
      { key: "send_timeline", type: "bool", label: "Хронология и справка по времени" },
      { key: "context_depth", type: "int", label: "Последних обычных событий", min: 0, max: 500, hint: "Ключевые и важные уходят всегда, обычные — только последние N." },
      { key: "send_characters", type: "bool", label: "Присутствующие, наряды, NPC" },
      { key: "send_affection", type: "bool", label: "Расположение" },
      { key: "send_main_personality", type: "bool", label: "Характер главных персонажей" },
      { key: "send_items", type: "bool", label: "Предметы" },
      { key: "send_agenda", type: "bool", label: "Планы" },
      { key: "send_location_memory", type: "bool", label: "Память сцен", hint: "И правило scene_desc: модель описывает новые места." },
      { key: "send_relationships", type: "bool", label: "Сеть отношений", hint: "И правило rel: модель отмечает связи между персонажами." },
      { key: "send_mood", type: "bool", label: "Настроение", hint: "И правило mood." },
    ] },
    { id: "summary", title: "Авто-свёртка хронологии", fields: [
      { key: "summary_enabled", type: "bool", label: "Сворачивать старые события сами", hint: "Каждая свёртка — платный запрос к служебной модели." },
      { key: "summary_keep_recent", type: "int", label: "Не трогать последних ответов ИИ", min: 3, max: 500 },
      { key: "summary_source", type: "choice", label: "Из чего писать свёртку", options: [["fulltext", "Из полного текста сообщений — точнее"], ["events", "Из списка событий — дешевле"]] },
      { key: "summary_buffer_mode", type: "choice", label: "Когда сворачивать", options: [["messages", "Накопилось N ответов"], ["tokens", "Накопилось N токенов"]] },
      { key: "summary_buffer_messages", type: "int", label: "Порог, ответов ИИ", min: 5, max: 1000 },
      { key: "summary_buffer_tokens", type: "int", label: "Порог, токенов", min: 1000, max: 1000000, step: 1000 },
      { key: "summary_batch_messages", type: "int", label: "Потолок пакета, событий", min: 5, max: 1000 },
      { key: "summary_batch_tokens", type: "int", label: "Потолок пакета, токенов", min: 10000, max: 1000000, step: 1000 },
      { key: "resummary_threshold", type: "int", label: "Свёрток одного уровня для свёртки выше", min: 0, max: 100, hint: "0 — свёртки не сворачиваются." },
      { key: "resummary_min_chars", type: "int", label: "Минимум текста для свёртки выше, символов", min: 0, max: 100000, step: 100 },
      { key: "summary_hides", type: "bool", label: "Свёртка убирает покрытые сообщения из окна", hint: "Сообщения под активной свёрткой не уходят модели дословно — ход дешевле." },
    ] },
    { id: "recall", title: "Вспоминание", fields: [
      { key: "recall_enabled", type: "bool", label: "Вспоминать события из давней части чата" },
      { key: "recall_top_k", type: "int", label: "Сколько воспоминаний", min: 1, max: 10 },
      { key: "recall_threshold", type: "float", label: "Порог сходства по смыслу", min: 0.3, max: 0.95, step: 0.01, hint: "С моделью эмбеддингов. Выше — строже." },
      { key: "recall_lexical_threshold", type: "float", label: "Порог сходства по словам", min: 0.1, max: 0.9, step: 0.01, hint: "Когда модели эмбеддингов нет." },
      { key: "recall_full_text_count", type: "int", label: "Лучших — полным текстом", min: 0, max: 5 },
      { key: "recall_full_text_threshold", type: "float", label: "Порог для полного текста", min: 0.6, max: 1, step: 0.01 },
      { key: "recall_full_text_chars", type: "int", label: "Потолок полного текста, символов", min: 200, max: 50000, step: 100 },
      { key: "recall_pure", type: "bool", label: "Только по смыслу, без ключевых слов" },
      { key: "recall_rerank", type: "bool", label: "Переранжирование" },
      { key: "recall_rerank_model", type: "str", label: "Модель переранжирования", models: true },
      { key: "recall_rerank_candidates", type: "int", label: "Кандидатов на переранжирование", min: 5, max: 200 },
      { key: "recall_rerank_min_score", type: "float", label: "Минимальная оценка", min: 0, max: 1, step: 0.01 },
      { key: "recall_query_rewrite", type: "bool", label: "Переписывать запрос служебной моделью", hint: "Находит точнее, но это ещё один запрос на каждом ходу." },
    ] },
    { id: "rpg", title: "RPG", fields: [
      { key: "rpg_enabled", type: "bool", label: "RPG включён", hint: "Правила RPG — около 1–2 тысяч токенов в каждом ходе." },
      { key: "rpg_strict_present", type: "bool", label: "Только для присутствующих", hint: "Никого нет в сцене — RPG-данные не отправляются." },
      { key: "rpg_bars", type: "bool", label: "Шкалы (HP/MP/SP) и эффекты" },
      { key: "rpg_skills", type: "bool", label: "Навыки" },
      { key: "rpg_attrs", type: "bool", label: "Атрибуты" },
      { key: "rpg_reputation", type: "bool", label: "Репутация" },
      { key: "rpg_equipment", type: "bool", label: "Снаряжение" },
      { key: "rpg_level", type: "bool", label: "Уровень и опыт" },
      { key: "rpg_currency", type: "bool", label: "Деньги" },
      { key: "rpg_stronghold", type: "bool", label: "Опорные пункты" },
      { key: "rpg_user_only", type: "userOnly", label: "Только для вашего героя", hint: "Отмеченные модули ИИ ведёт только для вас, не для NPC." },
      { key: "rpg_bar_config", type: "barConfig", label: "Шкалы" },
      { key: "rpg_attr_config", type: "attrConfig", label: "Атрибуты" },
    ] },
    { id: "calendar", title: "Календарь", fields: [
      { key: "calendar", type: "calendar", label: "Свой календарь", hint: "Для вымышленных миров: месяцы со своими названиями и длиной. Дни недели тогда не считаются." },
    ] },
  ];
  const SCOPES = [["global", "Глобально"], ["character", "Персонаж"], ["chat", "Этот чат"]];
  const SCOPE_HINT = {
    global: "Для всех чатов. Профиль персонажа и чат могут переопределить.",
    character: "Для всех чатов этого персонажа. Сильнее глобальных.",
    chat: "Только этот чат. Сильнее всего остального.",
  };
  const SCOPE_NAME = { global: "Глобально", character: "Персонаж", chat: "Этот чат" };
  const LAYER_FROM = { global: "из глобальных", character: "из профиля персонажа", chat: "из этого чата" };

  const HSettingsTab = {
    name: "HSettingsTab",
    mixins: [HBase, FormHost],
    components: { "h-form": HForm, "h-rows": HRows },
    data() {
      const open = {};
      if (this.hp && this.hp.focusGroup) open[this.hp.focusGroup] = true;
      return { open, barRows: null, attrRows: null, cal: null, opsAll: false };
    },
    computed: {
      groups() { return SETTINGS_GROUPS; },
      scopes() { return SCOPES; },
      scopeHint() { return SCOPE_HINT[this.hp.scope]; },
      scopeName() { return SCOPE_NAME[this.hp.scope]; },
      L() { return this.hp.layers; },
      rpgMods() { return RPG_MODULES; },
      userOnly() { return arr(this.hp.valueOf("rpg_user_only")); },
      bars() { return arr(this.hp.valueOf("rpg_bar_config")); },
      attrs() { return arr(this.hp.valueOf("rpg_attr_config")); },
      calValue() { return obj(this.hp.valueOf("calendar")); },
      ops() { const all = this.view.ops.slice().reverse(); return this.opsAll ? all : all.slice(0, 30); },
      jobBusy() { return jobActive(this.hp.job); },
      scanWhen() { const s = this.view.stats.scan; return s && s.at ? this.when(s.at) : ""; },
    },
    watch: {
      // Другой уровень — другие значения: черновики редакторов сбрасываем.
      "hp.scope"() { this.resetDrafts(); },
      "hp.layers"() { this.resetDrafts(); },
    },
    created() {
      this.hp.ensureLayers();
      this.hp.focusGroup = "";
    },
    template: `
      <div class="h-tab">
        <div class="h-scope" role="group" aria-label="Где менять настройки">
          <button v-for="s in scopes" :key="s[0]" type="button" class="filter-chip" :class="{ on: hp.scope === s[0] }"
                  :aria-pressed="hp.scope === s[0] ? 'true' : 'false'" :disabled="s[0] === 'character' && !hp.characterId"
                  @click="hp.setScope(s[0])">{{ s[1] }}</button>
        </div>
        <p class="field-hint">{{ scopeHint }}<template v-if="!hp.characterId"> У группового чата профиля персонажа нет.</template></p>
        <p v-if="!hp.canEdit" class="h-warn">Глобальные настройки меняет только администратор — здесь их можно посмотреть.</p>
        <p v-if="hp.layersError" class="h-warn">⚠ {{ hp.layersError }} <button type="button" @click="hp.loadLayers">Повторить</button></p>
        <p v-else-if="!L" class="muted">Загружаю настройки…</p>
        <!-- Отключённый fieldset гасит все поля внутри разом: уровень, который
             менять нельзя, остаётся читаемым, но не принимает правок. -->
        <fieldset v-else class="h-fs-plain" :disabled="!hp.canEdit">
          <details v-for="g in groups" :key="g.id" class="h-det h-sgroup" :open="!!open[g.id]" @toggle="open[g.id] = $event.target.open">
            <summary>{{ g.title }} <span v-if="setCount(g)" class="tag">задано здесь: {{ setCount(g) }}</span></summary>
            <div v-for="f in g.fields" :key="f.key" class="h-field" :class="{ 'h-field-set': hp.isSet(f.key) }">
              <label v-if="f.type === 'bool'" class="check">
                <input type="checkbox" :checked="!!hp.valueOf(f.key)" :disabled="hp.busy" :aria-describedby="'hs-' + f.key"
                       @change="hp.saveSetting(f.key, $event.target.checked)" /> {{ f.label }}</label>
              <label v-else-if="f.type === 'choice'">{{ f.label }}
                <select :value="hp.valueOf(f.key)" :disabled="hp.busy" :aria-describedby="'hs-' + f.key"
                        @change="hp.saveSetting(f.key, $event.target.value)">
                  <option v-for="o in f.options" :key="o[0]" :value="o[0]">{{ o[1] }}</option>
                </select></label>
              <label v-else-if="f.type === 'int' || f.type === 'float'">{{ f.label }}
                <input type="number" inputmode="decimal" :min="f.min" :max="f.max" :step="f.step || (f.type === 'int' ? 1 : 0.01)"
                       :value="hp.valueOf(f.key)" :aria-describedby="'hs-' + f.key" @change="saveNum(f, $event)" /></label>
              <label v-else-if="f.type === 'str'">{{ f.label }}
                <input type="text" :value="hp.valueOf(f.key)" :placeholder="f.placeholder || ''" autocomplete="off"
                       :list="f.models ? 'horae-models' : null" :aria-describedby="'hs-' + f.key"
                       @change="hp.saveSetting(f.key, $event.target.value.trim())" /></label>
              <fieldset v-else-if="f.type === 'userOnly'" class="h-fs">
                <legend>{{ f.label }}</legend>
                <div class="h-checks">
                  <label v-for="m in rpgMods" :key="m[0]" class="check"><input type="checkbox" :disabled="hp.busy"
                         :checked="userOnly.indexOf(m[0]) !== -1" @change="toggleUserOnly(m[0], $event.target.checked)" /> {{ m[1] }}</label>
                </div>
              </fieldset>
              <div v-else-if="f.type === 'barConfig'" class="h-cfgblock">
                <b>Шкалы</b>
                <template v-if="barRows">
                  <h-rows :rows="barRows" legend="Шкалы" add-text="Шкала" item-name="шкалу" :blank="{ key: '', name: '', color: '#8aa3ff', max: 100, desc: '' }"
                          :cols="[{ key: 'key', label: 'Ключ (латиница)', narrow: true }, { key: 'name', label: 'Название' }, { key: 'color', label: 'Цвет', type: 'color', narrow: true }, { key: 'max', label: 'Макс.', type: 'number', narrow: true }, { key: 'desc', label: 'Что значит (для ИИ)', wide: true }]"></h-rows>
                  <div class="h-actions"><button type="button" class="btn-primary" :disabled="hp.busy" @click="saveBars">Сохранить шкалы</button>
                    <button type="button" class="btn-ghost" @click="barRows = null">Отмена</button></div>
                </template>
                <template v-else>
                  <ul class="h-list">
                    <li v-for="b in bars" :key="b.key" class="h-row"><span class="h-swatch" :style="{ background: b.color }" aria-hidden="true"></span>
                      <div class="h-row-main">{{ b.name }} <span class="h-meta">({{ b.key }}, до {{ b.max }})</span><div v-if="b.desc" class="h-meta">{{ b.desc }}</div></div></li>
                  </ul>
                  <button type="button" @click="editBars">Изменить шкалы</button>
                </template>
              </div>
              <div v-else-if="f.type === 'attrConfig'" class="h-cfgblock">
                <b>Атрибуты</b>
                <template v-if="attrRows">
                  <h-rows :rows="attrRows" legend="Атрибуты" add-text="Атрибут" item-name="атрибут" :blank="{ key: '', name: '', desc: '' }"
                          :cols="[{ key: 'key', label: 'Ключ (латиница)', narrow: true }, { key: 'name', label: 'Название' }, { key: 'desc', label: 'Что значит (для ИИ)', wide: true }]"></h-rows>
                  <div class="h-actions"><button type="button" class="btn-primary" :disabled="hp.busy" @click="saveAttrs">Сохранить атрибуты</button>
                    <button type="button" class="btn-ghost" @click="attrRows = null">Отмена</button></div>
                </template>
                <template v-else>
                  <p class="h-meta">{{ attrs.map(a => a.name + ' (' + a.key + ')').join(', ') || 'Не заданы.' }}</p>
                  <button type="button" @click="editAttrs">Изменить атрибуты</button>
                </template>
              </div>
              <div v-else-if="f.type === 'calendar'" class="h-cfgblock">
                <template v-if="cal">
                  <label class="check"><input type="checkbox" v-model="cal.on" /> {{ f.label }}</label>
                  <label>Месяцы — «Название:дней», по одному в строке
                    <textarea v-model="cal.text" rows="6" placeholder="Месяц снегов:30"></textarea></label>
                  <div class="h-actions"><button type="button" class="btn-primary" :disabled="hp.busy" @click="saveCalendar">Сохранить календарь</button>
                    <button type="button" class="btn-ghost" @click="cal = null">Отмена</button></div>
                </template>
                <template v-else>
                  <p class="h-meta">{{ calValue.enabled ? 'Включён: ' + arrLen(calValue.months) + ' мес.' : 'Выключен — обычный календарь с днями недели.' }}</p>
                  <button type="button" @click="editCalendar">Изменить календарь</button>
                </template>
              </div>
              <div class="h-field-foot">
                <span :id="'hs-' + f.key" class="field-hint">{{ f.hint ? f.hint + ' ' : '' }}<span class="h-origin">{{ hp.originText(f.key) }}</span></span>
                <button v-if="hp.isSet(f.key)" type="button" class="btn-ghost h-reset" :disabled="hp.busy"
                        @click="hp.resetSetting(f.key)" :aria-label="'Сбросить: ' + f.label">сбросить</button>
              </div>
            </div>
          </details>
          <details class="h-det h-sgroup">
            <summary>Профиль настроек</summary>
            <p class="field-hint">Файл с настройками уровня «{{ scopeName }}» — перенести на другой сервер или поделиться. Модели и промпты входят в файл.</p>
            <div class="h-actions">
              <button type="button" @click="exportProfile">Экспорт JSON</button>
              <label class="btn">Импорт JSON<input type="file" accept="application/json,.json" class="file-input"
                     @change="importProfile" aria-label="Импорт настроек Horae из JSON" /></label>
              <button type="button" class="btn-danger" :disabled="hp.busy" @click="restoreDefaults">Вернуть по умолчанию</button>
            </div>
          </details>
        </fieldset>

        <section class="h-block" aria-labelledby="h-data-h">
          <h4 id="h-data-h" class="h-h">Данные этого чата</h4>
          <p class="field-hint">Ответов ИИ без данных Horae: {{ fmtNum(view.stats.without_meta) }}<template v-if="scanWhen"> · последний скан: {{ scanWhen }}</template>.</p>
          <div class="h-actions">
            <button type="button" :disabled="jobBusy" @click="openScan">🔎 ИИ-скан истории</button>
            <button type="button" :disabled="!view.stats.scan || jobBusy" @click="undoScan">Отменить скан</button>
            <button type="button" @click="exportData">Экспорт данных</button>
            <label class="btn">Импорт<input type="file" accept="application/json,.json" class="file-input"
                   @change="importData" aria-label="Импорт данных Horae из JSON" /></label>
            <button type="button" @click="openCarry">Новый чат с памятью</button>
            <button type="button" @click="reindex">Переиндексировать поиск</button>
            <button type="button" class="btn-danger" @click="wipe">Стереть данные Horae</button>
          </div>
        </section>

        <section class="h-block" aria-labelledby="h-ops-h">
          <h4 id="h-ops-h" class="h-h">Журнал правок <span class="h-count">{{ view.ops.length }}</span></h4>
          <p class="field-hint">Ваши правки состояния, новые сверху. Откат убирает правку — всё остальное пересчитается без неё.</p>
          <ul v-if="ops.length" class="h-list">
            <li v-for="o in ops" :key="o.id" class="h-row">
              <div class="h-row-main">{{ o.label || o.kind }}
                <div class="h-meta">{{ o.kind }}<template v-if="o.at != null"> · после #{{ o.at }}</template><template v-if="o.created_at"> · {{ when(o.created_at) }}</template></div></div>
              <div class="h-acts"><button type="button" :disabled="hp.busy" @click="undoOp(o)" :aria-label="'Откатить: ' + (o.label || o.kind)">Откатить</button></div>
            </li>
          </ul>
          <p v-else class="muted">Правок нет.</p>
          <button v-if="view.ops.length > 30" type="button" class="btn-ghost" @click="opsAll = !opsAll">{{ opsAll ? 'Показать последние 30' : 'Показать все ' + view.ops.length }}</button>
        </section>
        ${FORM_TAG}
      </div>`,
    methods: {
      arrLen(a) { return arr(a).length; },
      when(iso) {
        const d = new Date(iso);
        return isNaN(d) ? "" : d.toLocaleString("ru-RU", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
      },
      setCount(g) { return g.fields.filter((f) => this.hp.isSet(f.key)).length; },
      resetDrafts() { this.barRows = null; this.attrRows = null; this.cal = null; },
      saveNum(f, ev) {
        const raw = str(ev.target.value).replace(",", ".").trim();
        const n = Number(raw);
        if (raw === "" || !Number.isFinite(n)) { ev.target.value = this.hp.valueOf(f.key); return; }
        let v = f.type === "int" ? Math.round(n) : n;
        if (f.min != null) v = Math.max(f.min, v);
        if (f.max != null) v = Math.min(f.max, v);
        ev.target.value = v;
        this.hp.saveSetting(f.key, v);
      },
      toggleUserOnly(m, on) {
        const cur = this.userOnly.filter((x) => x !== m);
        if (on) cur.push(m);
        this.hp.saveSetting("rpg_user_only", RPG_MODULES.map((x) => x[0]).filter((x) => cur.indexOf(x) !== -1));
      },
      editBars() { this.barRows = this.bars.map((b) => ({ key: str(b.key), name: str(b.name), color: str(b.color || "#8aa3ff"), max: num(b.max, 100), desc: str(b.desc) })); },
      async saveBars() {
        const rows = this.barRows.filter((b) => str(b.key).trim()).map((b) => ({
          key: str(b.key).trim().toLowerCase(), name: str(b.name).trim(), color: str(b.color), max: Math.max(1, Math.round(num(b.max, 100))), desc: str(b.desc).trim() }));
        if (await this.hp.saveSetting("rpg_bar_config", rows)) this.barRows = null;
      },
      editAttrs() { this.attrRows = this.attrs.map((a) => ({ key: str(a.key), name: str(a.name), desc: str(a.desc) })); },
      async saveAttrs() {
        const rows = this.attrRows.filter((a) => str(a.key).trim()).map((a) => ({ key: str(a.key).trim().toLowerCase(), name: str(a.name).trim(), desc: str(a.desc).trim() }));
        if (await this.hp.saveSetting("rpg_attr_config", rows)) this.attrRows = null;
      },
      editCalendar() {
        const c = this.calValue;
        this.cal = { on: !!c.enabled, text: arr(c.months).map((m) => str(m.name) + ":" + str(m.days)).join("\n") };
      },
      async saveCalendar() {
        const months = [];
        const bad = [];
        for (const line of str(this.cal.text).split("\n")) {
          if (!line.trim()) continue;
          const m = /^(.+?)\s*[:：]\s*(\d+)\s*$/.exec(line.trim());
          if (m) months.push({ name: m[1].trim(), days: Number(m[2]) }); else bad.push(line.trim());
        }
        if (bad.length) { this.hp.toast("Не понял строки: " + bad.slice(0, 3).join("; ") + " — нужно «Название:дней»"); return; }
        if (this.cal.on && !months.length) { this.hp.toast("Добавьте хотя бы один месяц"); return; }
        if (await this.hp.saveSetting("calendar", { enabled: this.cal.on, months })) this.cal = null;
      },
      // --- Профиль ---
      allKeys() { return Object.keys(obj(this.L && this.L.defaults)); },
      exportProfile() {
        const settings = {};
        for (const k of this.allKeys()) { const v = this.hp.valueOf(k); if (v !== undefined) settings[k] = clone(v); }
        const name = "horae-settings_" + this.hp.scope + "_" + new Date().toISOString().slice(0, 10) + ".json";
        this.$root.downloadJson({ type: "horae-settings", version: 1, scope: this.hp.scope, settings }, name);
      },
      async importProfile(ev) {
        let data;
        try { data = await readJsonFile(ev); } catch (e) { this.hp.toast("⚠ Это не JSON: " + (e.message || e)); return; }
        if (!data) return;
        const src = obj(data.type === "horae-settings" ? data.settings : data);
        const keys = this.allKeys();
        const patch = {};
        for (const k of Object.keys(src)) if (keys.indexOf(k) !== -1) patch[k] = src[k];
        const n = Object.keys(patch).length;
        if (!n) { this.hp.toast("В файле нет настроек Horae"); return; }
        const ok = await this.hp.confirm("Импортировать " + n + " " + this.plural(n, "настройку", "настройки", "настроек") + " на уровень «" + this.scopeName + "»?", { okText: "Импортировать", danger: false });
        if (ok && await this.hp.saveLayer(patch)) this.hp.toast("Настройки импортированы");
      },
      async restoreDefaults() {
        const layer = obj(this.L && this.L[this.hp.scope]);
        const keys = Object.keys(layer);
        if (!keys.length) { this.hp.toast("На этом уровне своих настроек нет"); return; }
        const ok = await this.hp.confirm("Убрать все " + keys.length + " своих настроек уровня «" + this.scopeName + "», включая промпты? Останутся значения уровней ниже или умолчания.", { okText: "Сбросить" });
        if (!ok) return;
        const patch = {};
        for (const k of keys) patch[k] = null;
        if (await this.hp.saveLayer(patch)) this.hp.toast("Настройки уровня «" + this.scopeName + "» сброшены");
      },
      // --- Данные чата ---
      openScan() {
        const n = this.view.stats.without_meta;
        this.openForm("scan", {
          title: "ИИ-скан истории", submitText: "Начать скан",
          note: "Ответов без данных: " + this.fmtNum(n) + ". Служебная модель прочтёт их пакетами и допишет данные. Каждый пакет — платный запрос; результат можно отменить кнопкой «Отменить скан».",
          fields: [
            { key: "batch_tokens", label: "Размер пакета, токенов", type: "number", value: 80000, min: 2000, max: 500000, step: 1000,
              hint: "Больше — меньше запросов, но каждый тяжелее." },
            { key: "npc", label: "Искать персонажей (NPC)", type: "checkbox", value: true },
            { key: "affection", label: "Искать расположение", type: "checkbox", value: true },
            { key: "scene", label: "Искать описания мест", type: "checkbox", value: !!this.settings.send_location_memory },
            { key: "relationships", label: "Искать отношения", type: "checkbox", value: !!this.settings.send_relationships },
          ],
        });
      },
      async undoScan() {
        const ok = await this.hp.confirm("Отменить последний скан? Данные, которые он дописал, уберутся; прежние вернутся.", { okText: "Отменить скан" });
        if (!ok) return;
        const r = await this.hp.send("POST", this.hp.base + "/scan/undo", {});
        if (r) { this.hp.toast("Скан отменён"); await this.hp.reload(); }
      },
      async exportData() {
        const r = await this.hp.send("GET", this.hp.base + "/export");
        if (r) this.$root.downloadJson(r, "horae_chat_" + this.hp.sessionId + ".json");
      },
      async importData(ev) {
        let data;
        try { data = await readJsonFile(ev); } catch (e) { this.hp.toast("⚠ Это не JSON: " + (e.message || e)); return; }
        if (!data) return;
        if (data.type !== "horae-chat") {
          this.hp.toast("Это не файл «Экспорт данных Horae» (нужен type: horae-chat)");
          return;
        }
        this._importData = data;
        this.openForm("import", { title: "Импорт данных Horae", submitText: "Импортировать", fields: [
          { key: "mode", label: "Как положить", type: "select", value: "by_id", options: [
            ["by_id", "По номерам сообщений — вернуть данные в тот же чат"],
            ["initial", "Как стартовое состояние — перенести в другой чат"]] },
        ], note: "По номерам — данные встанут к тем же сообщениям. Стартовое состояние — NPC, предметы, планы, отношения и RPG станут началом этого чата." });
      },
      openCarry() {
        const keep = num(this.settings.summary_keep_recent, 10);
        this.openForm("carry", { title: "Новый чат с памятью", submitText: "Создать чат", fields: [
          { key: "keep", label: "Сколько последних ответов ИИ перенести дословно", type: "number", value: keep, min: 0, max: 200, step: 1 },
          { key: "vectors", label: "Перенести и индекс воспоминаний", type: "checkbox", value: true },
        ], note: "В новый чат перейдут состояние (персонажи, предметы, планы, отношения, RPG, таблицы) и пересказ всего, что было раньше. Этот чат не изменится." });
      },
      async reindex() {
        const r = await this.hp.send("POST", this.hp.base + "/reindex", {});
        if (r) this.hp.toast(r.documents != null ? "Поиск переиндексирован: документов " + this.fmtNum(r.documents) : "Поиск переиндексирован");
      },
      async wipe() {
        const ok = await this.hp.confirm("Стереть данные Horae этого чата? Уйдут данные всех сообщений, свёртки, таблицы чата, RPG и журнал правок. Текст сообщений останется. Вернуть нельзя — сначала сделайте экспорт.",
          { title: "Стереть данные Horae", okText: "Стереть" });
        if (!ok) return;
        const r = await this.hp.send("DELETE", this.hp.base);
        if (r) { this.hp.toast("Данные Horae чата стёрты"); await this.hp.reload(); }
      },
      async undoOp(o) {
        const r = await this.hp.send("DELETE", this.hp.base + "/ops/" + encodeURIComponent(o.id));
        if (r) { this.hp.toast("Откачено: " + (o.label || o.kind)); await this.hp.reload(); }
      },
      async onForm(v) {
        const f = this.form;
        if (!f) return;
        const hp = this.hp;
        if (f.kind === "scan") {
          const body = { batch_tokens: Math.max(2000, Math.round(num(v.batch_tokens, 80000))),
            include: { npc: v.npc, affection: v.affection, scene: v.scene, relationships: v.relationships } };
          const r = await hp.send("POST", hp.base + "/scan", body);
          if (!r) return;
          this.closeForm();
          if (r.job) hp.trackJob(r.job); else hp.pollJob();
        } else if (f.kind === "import") {
          const data = this._importData;
          if (!data) { this.closeForm(); return; }
          const r = await hp.send("POST", hp.base + "/import", { data, mode: v.mode });
          if (!r) return;
          this._importData = null;
          this.closeForm();
          hp.toast("Данные Horae импортированы");
          await hp.reload();
        } else if (f.kind === "carry") {
          const r = await hp.send("POST", hp.base + "/carryover", { keep: Math.max(0, Math.round(num(v.keep, 10))), vectors: v.vectors });
          if (!r) return;
          this.closeForm();
          if (r.session_id != null) {
            hp.toast("Новый чат с памятью создан");
            await hp.openChat(r.session_id);
          }
        }
      },
      onFormDelete() {},
    },
  };

  // ==========================================================================
  // «Промпты»
  // ==========================================================================
  const HPromptsTab = {
    name: "HPromptsTab",
    mixins: [HBase],
    data() { return { lib: null, libErr: "", drafts: {}, preset: "" }; },
    computed: {
      keys() { return PROMPT_TITLES; },
      defaults() { return obj(this.lib && this.lib.defaults); },
      presets() { return arr(this.lib && this.lib.presets); },
      presetObj() { return this.presets.find((p) => String(p.id) === this.preset) || null; },
      scopeName() { return SCOPE_NAME[this.hp.scope]; },
      scopes() { return SCOPES; },
    },
    watch: {
      "hp.scope"() { this.drafts = {}; },
      "hp.layers"() { this.drafts = {}; },
    },
    created() { this.hp.ensureLayers(); this.loadLib(); },
    template: `
      <div class="h-tab">
        <div class="h-scope" role="group" aria-label="Где менять промпты">
          <button v-for="s in scopes" :key="s[0]" type="button" class="filter-chip" :class="{ on: hp.scope === s[0] }"
                  :aria-pressed="hp.scope === s[0] ? 'true' : 'false'" :disabled="s[0] === 'character' && !hp.characterId"
                  @click="hp.setScope(s[0])">{{ s[1] }}</button>
        </div>
        <p class="field-hint">Свои промпты уровня «{{ scopeName }}». Пустой или совпадающий с исходным — значит по умолчанию.</p>
        <p v-if="!hp.canEdit" class="h-warn">Глобальные промпты меняет только администратор — здесь их можно посмотреть.</p>
        <p v-if="libErr" class="h-warn">⚠ {{ libErr }} <button type="button" @click="loadLib">Повторить</button></p>
        <p v-if="!lib || !hp.layers" class="muted">Загружаю промпты…</p>
        <fieldset v-else class="h-fs-plain" :disabled="!hp.canEdit">
          <details v-for="k in keys" :key="k[0]" class="h-det h-prompt">
            <summary>{{ k[1] }} <span class="tag" :class="{ 'h-tag-set': state(k[0]).custom }">{{ status(k[0]) }}</span>
              <span v-if="drafts[k[0]] != null" class="tag">не сохранено</span></summary>
            <p class="field-hint">{{ k[2] }}</p>
            <label class="sr-only" :for="'hp-' + k[0]">{{ k[1] }}</label>
            <textarea :id="'hp-' + k[0]" class="h-prompt-text" rows="10" spellcheck="false"
                      :value="textOf(k[0])" @input="drafts[k[0]] = $event.target.value"></textarea>
            <div class="h-prompt-foot">
              <span class="h-meta">{{ fmtNum(textOf(k[0]).length) }} {{ plural(textOf(k[0]).length, 'символ', 'символа', 'символов') }}</span>
              <span class="h-grow"></span>
              <button v-if="drafts[k[0]] != null" type="button" class="btn-ghost" @click="drop(k[0])">Отменить правку</button>
              <button type="button" :disabled="!state(k[0]).custom || hp.busy" @click="resetOne(k[0])">Вернуть по умолчанию</button>
              <button type="button" class="btn-primary" :disabled="drafts[k[0]] == null || hp.busy" @click="saveOne(k[0])">Сохранить</button>
            </div>
          </details>

          <section class="h-block" aria-labelledby="h-presets-h">
            <h4 id="h-presets-h" class="h-h">Пресеты</h4>
            <div class="h-actions">
              <select v-model="preset" aria-label="Пресет промптов" class="h-preset-select">
                <option value="">— выберите пресет —</option>
                <option v-for="p in presets" :key="p.id" :value="String(p.id)">{{ p.builtin ? '⭐ ' : '' }}{{ p.name }}</option>
              </select>
              <button type="button" :disabled="!presetObj || hp.busy" @click="loadPreset">Загрузить</button>
              <button type="button" class="btn-danger" :disabled="!presetObj || presetObj.builtin || hp.busy" @click="deletePreset">Удалить</button>
            </div>
            <p class="field-hint">⭐ — встроенные, их не удалить. Загрузка заменит свои промпты уровня «{{ scopeName }}».</p>
            <div class="h-actions">
              <button type="button" :disabled="hp.busy" @click="savePreset">Сохранить текущие как пресет</button>
              <button type="button" @click="exportAll">Экспорт промптов</button>
              <label class="btn">Импорт<input type="file" accept="application/json,.json" class="file-input"
                     @change="importAll" aria-label="Импорт промптов Horae из JSON" /></label>
            </div>
          </section>
        </fieldset>
      </div>`,
    methods: {
      async loadLib() {
        this.libErr = "";
        try { this.lib = await this.$root.api("/horae/prompts"); } catch (e) { this.libErr = "Промпты не загрузились: " + (e.message || e); }
      },
      state(k) { return this.hp.promptAt(k); },
      status(k) {
        const s = this.state(k);
        if (!s.custom) return "по умолчанию";
        return s.from === this.hp.scope ? "своё здесь" : "своё, " + (LAYER_FROM[s.from] || "");
      },
      textOf(k) {
        if (this.drafts[k] != null) return this.drafts[k];
        const s = this.state(k);
        return s.custom ? s.text : str(this.defaults[k]);
      },
      drop(k) { delete this.drafts[k]; },
      // Текст, совпавший с исходным, храним пустым: иначе обновление промпта
      // по умолчанию на сервере до этого чата не дошло бы никогда.
      norm(k, text) {
        const t = str(text);
        return t.trim() === str(this.defaults[k]).trim() ? "" : t;
      },
      async saveOne(k) {
        if (await this.hp.savePrompts({ [k]: this.norm(k, this.drafts[k]) })) {
          delete this.drafts[k];
          this.hp.toast("Промпт «" + this.title(k) + "» сохранён");
        }
      },
      async resetOne(k) {
        if (await this.hp.savePrompts({ [k]: "" })) { delete this.drafts[k]; this.hp.toast("«" + this.title(k) + "» — по умолчанию"); }
      },
      title(k) { const t = PROMPT_TITLES.find((x) => x[0] === k); return t ? t[1] : k; },
      current() {
        const out = {};
        for (const [k] of PROMPT_TITLES) { const s = this.state(k); out[k] = s.custom ? s.text : ""; }
        return out;
      },
      async loadPreset() {
        const p = this.presetObj;
        if (!p) return;
        const ok = await this.hp.confirm("Загрузить пресет «" + p.name + "» на уровень «" + this.scopeName + "»? Свои промпты этого уровня заменятся.", { okText: "Загрузить", danger: false });
        if (!ok) return;
        const src = obj(p.prompts);
        const map = {};
        for (const [k] of PROMPT_TITLES) map[k] = this.norm(k, src[k]);
        if (await this.hp.savePrompts(map)) { this.drafts = {}; this.hp.toast("Пресет «" + p.name + "» загружен"); }
      },
      async savePreset() {
        const name = await this.hp.prompt("Имя пресета", { okText: "Сохранить", placeholder: "например «Подробные события»" });
        if (!name || !name.trim()) return;
        const r = await this.hp.send("POST", "/horae/prompts/presets", { name: name.trim(), prompts: this.current() });
        if (r) { this.hp.toast("Пресет сохранён"); await this.loadLib(); if (r.id != null) this.preset = String(r.id); }
      },
      async deletePreset() {
        const p = this.presetObj;
        if (!p || p.builtin) return;
        const ok = await this.hp.confirm("Удалить пресет «" + p.name + "»?", { okText: "Удалить" });
        if (!ok) return;
        const r = await this.hp.send("DELETE", "/horae/prompts/presets/" + encodeURIComponent(p.id));
        if (r) { this.preset = ""; await this.loadLib(); }
      },
      exportAll() {
        this.$root.downloadJson({ type: "horae-prompts", version: 1, prompts: this.current() },
          "horae-prompts_" + new Date().toISOString().slice(0, 10) + ".json");
      },
      async importAll(ev) {
        let data;
        try { data = await readJsonFile(ev); } catch (e) { this.hp.toast("⚠ Это не JSON: " + (e.message || e)); return; }
        if (!data) return;
        if (data.type !== "horae-prompts" || !data.prompts || typeof data.prompts !== "object") {
          this.hp.toast("Это не файл промптов Horae (нужен type: horae-prompts)");
          return;
        }
        const map = {};
        for (const [k] of PROMPT_TITLES) if (own(data.prompts, k)) map[k] = this.norm(k, data.prompts[k]);
        const n = Object.keys(map).length;
        const ok = await this.hp.confirm("Импортировать " + n + " " + this.plural(n, "промпт", "промпта", "промптов") + " на уровень «" + this.scopeName + "»?", { okText: "Импортировать", danger: false });
        if (ok && await this.hp.savePrompts(map)) { this.drafts = {}; this.hp.toast("Промпты импортированы"); }
      },
    },
  };

  // ==========================================================================
  // Панель «Хроника»
  // ==========================================================================
  const TABS = [
    ["state", "Состояние"], ["timeline", "Хронология"], ["chars", "Персонажи"], ["items", "Предметы"],
    ["scenes", "Сцены"], ["tables", "Таблицы"], ["rpg", "RPG"], ["settings", "Настройки"], ["prompts", "Промпты"],
  ];

  const HoraePanel = {
    name: "HoraePanel",
    components: {
      "h-state": HStateTab, "h-timeline": HTimelineTab, "h-chars": HCharsTab, "h-items": HItemsTab,
      "h-scenes": HScenesTab, "h-tables": HTablesTab, "h-rpg": HRpgTab, "h-settings": HSettingsTab, "h-prompts": HPromptsTab,
    },
    props: {
      sessionId: { type: [Number, String], default: null },
      characterId: { type: [Number, String], default: null },
      // Растёт после каждого хода (app.js, finishStream): состояние перечитывается.
      tick: { type: Number, default: 0 },
    },
    provide() { return { hp: this }; },
    data() {
      let tab = "state";
      try { const t = localStorage.getItem("horaeTab"); if (TABS.some((x) => x[0] === t)) tab = t; } catch (e) { /* приватный режим */ }
      return {
        tab, raw: null, loading: false, loadError: "", busy: false,
        job: null, jobFails: 0, live: "",
        layers: null, layersError: "", scope: "chat", focusGroup: "",
      };
    },
    computed: {
      tabs() { return TABS; },
      view() { return this.raw ? normalize(this.raw) : null; },
      // Глобальные настройки и глобальные таблицы сервер даёт менять только
      // администратору. Заглушка без isAdmin (стенд, старое приложение) — да.
      isAdmin() { return this.$root.isAdmin !== false; },
      canEdit() { return this.scope !== "global" || this.isAdmin; },
      base() { return "/sessions/" + this.sessionId + "/horae"; },
      models() { const m = this.$root.models; return Array.isArray(m) ? m.filter((x) => typeof x === "string") : []; },
      jobOn() { return jobActive(this.job); },
      jobTitle() { return this.job && this.job.kind === "summary" ? "Авто-свёртка хронологии" : "ИИ-скан истории"; },
      jobPct() {
        const j = this.job;
        return j && j.total ? Math.max(0, Math.min(100, Math.round((Number(j.processed) || 0) / j.total * 100))) : 0;
      },
      jobLine() {
        const j = this.job;
        if (!j) return "";
        if (j.line) return j.line;
        if (j.status === "queued") return "В очереди…";
        if (j.total) return "Обработано " + (j.processed || 0) + " из " + j.total + (j.batches ? " · пакетов: " + j.batches : "");
        return "Идёт…";
      },
    },
    watch: {
      sessionId() {
        this.stopJob();
        this.raw = null;
        this.job = null;
        this.layers = null;
        this.loadError = "";
        this.reload();
        if (this.tab === "settings" || this.tab === "prompts") this.loadLayers();
      },
      characterId() {
        if (!this.characterId && this.scope === "character") this.scope = "chat";
        this.layers = null;
        if (this.tab === "settings" || this.tab === "prompts") this.loadLayers();
      },
      tick() { this.reload(); },
      tab(t) {
        try { localStorage.setItem("horaeTab", t); } catch (e) { /* приватный режим */ }
        if (t === "settings" || t === "prompts") this.ensureLayers();
        this.$nextTick(() => {
          const b = document.getElementById("horae-tab-" + t);
          if (b && b.scrollIntoView) b.scrollIntoView({ block: "nearest", inline: "nearest" });
        });
      },
    },
    mounted() {
      this.reload();
      if (this.tab === "settings" || this.tab === "prompts") this.ensureLayers();
    },
    beforeUnmount() {
      this._dead = true;
      this.stopJob();
    },
    template: `
      <section class="horae" aria-label="Хроника Horae">
        <p v-if="!sessionId" class="muted">Откройте чат — здесь появится его хроника: время, место, персонажи, предметы и события.</p>
        <template v-else>
          <div class="h-tabs" role="tablist" aria-label="Разделы хроники" @keydown="onTabKey">
            <button v-for="t in tabs" :key="t[0]" :id="'horae-tab-' + t[0]" type="button" role="tab" class="tab-btn"
                    :class="{ active: tab === t[0] }" :aria-selected="tab === t[0] ? 'true' : 'false'"
                    :aria-controls="'horae-panel-' + t[0]" :tabindex="tab === t[0] ? 0 : -1" @click="tab = t[0]">{{ t[1] }}</button>
          </div>
          <!-- Задание скана или свёртки — над любой подвкладкой: запускают его
               в «Настройках», а смотреть на полосу можно откуда угодно. Живой
               регион стоит в DOM всегда, «Остановить» — вне его. -->
          <div class="h-job" :class="{ 'h-job-idle': !jobOn }">
            <div v-if="jobOn" class="h-job-head">
              <b>{{ jobTitle }}</b>
              <span class="h-grow"></span>
              <button type="button" class="btn-danger" @click="cancelJob">Остановить</button>
            </div>
            <span v-if="jobOn" class="upload-track"><span class="upload-fill" :style="{ width: jobPct + '%' }"></span></span>
            <div class="h-meta" role="status" aria-live="polite">{{ jobOn ? jobLine : '' }}</div>
            <div v-if="jobOn && jobFails >= 3" class="h-meta h-offline">Нет связи с сервером, повторяю…</div>
            <p v-if="job && job.status === 'error'" class="h-warn">⚠ {{ jobTitle }}: {{ job.error || 'ошибка' }}</p>
          </div>
          <div class="sr-only" role="status" aria-live="polite">{{ live }}</div>
          <div :id="'horae-panel-' + tab" role="tabpanel" :aria-labelledby="'horae-tab-' + tab" class="h-panel">
            <div class="h-panel-bar">
              <span v-if="loading" class="h-meta">Обновляю…</span>
              <span class="h-grow"></span>
              <button type="button" class="btn-icon" :disabled="loading" @click="reload" aria-label="Перечитать хронику" title="Перечитать">↻</button>
            </div>
            <p v-if="loadError" class="h-warn">⚠ Хроника не загрузилась: {{ loadError }}
              <button type="button" @click="reload">Повторить</button></p>
            <p v-if="!view && loading" class="muted">Загружаю хронику…</p>
            <template v-if="view">
              <h-state v-if="tab === 'state'" :view="view"></h-state>
              <h-timeline v-else-if="tab === 'timeline'" :view="view"></h-timeline>
              <h-chars v-else-if="tab === 'chars'" :view="view"></h-chars>
              <h-items v-else-if="tab === 'items'" :view="view"></h-items>
              <h-scenes v-else-if="tab === 'scenes'" :view="view"></h-scenes>
              <h-tables v-else-if="tab === 'tables'" :view="view"></h-tables>
              <h-rpg v-else-if="tab === 'rpg'" :view="view"></h-rpg>
              <h-settings v-else-if="tab === 'settings'" :view="view"></h-settings>
              <h-prompts v-else-if="tab === 'prompts'" :view="view"></h-prompts>
            </template>
          </div>
          <datalist id="horae-models"><option v-for="m in models" :key="m" :value="m"></option></datalist>
        </template>
      </section>`,
    methods: {
      // --- Сеть ---
      toast(t) { if (this.$root.showToast) this.$root.showToast(t); },
      fail(e) { this.toast("⚠ " + ((e && e.message) || String(e))); },
      // Запрос с тостом ошибки. Успех — объект ответа (204 → {}), сбой — null:
      // вызывающему хватает if (!r) return.
      async send(method, path, body) {
        this._busyN = (this._busyN || 0) + 1;
        this.busy = true;
        try {
          const opts = { method };
          if (body !== undefined && method !== "GET") opts.body = JSON.stringify(body);
          const r = await this.$root.api(path, opts);
          return r == null ? {} : r;
        } catch (e) {
          this.fail(e);
          return null;
        } finally {
          this._busyN -= 1;
          this.busy = this._busyN > 0;
        }
      },
      // Правка состояния (журнал ops). reload: false — когда правок несколько
      // подряд, перечитываем один раз в конце.
      async op(kind, payload, opts) {
        const r = await this.send("POST", this.base + "/ops", Object.assign({ kind }, payload || {}));
        if (r && !(opts && opts.reload === false)) await this.reload();
        return !!r;
      },
      // Номер запроса отсекает обогнанные ответы: ↻, ход и смена чата
      // перечитывают состояние наперегонки.
      async reload() {
        const sid = this.sessionId;
        if (!sid) return;
        const seq = (this._seq = (this._seq || 0) + 1);
        this.loading = true;
        try {
          const r = await this.$root.api("/sessions/" + sid + "/horae/state");
          if (seq !== this._seq || sid !== this.sessionId || this._dead) return;
          this.raw = r && typeof r === "object" ? r : {};
          this.loadError = "";
          const job = this.raw.job;
          if (jobActive(job) && !this._jobTimer) this.setJob(job);
          else if (!jobActive(this.job)) this.job = job || null;
        } catch (e) {
          if (seq === this._seq) this.loadError = (e && e.message) || String(e);
        } finally {
          if (seq === this._seq) this.loading = false;
        }
      },
      setLocalCell(tid, key, value) {
        const t = arr(this.raw && this.raw.tables).find((x) => x && x.id === tid);
        if (!t) return;
        if (!t.data || typeof t.data !== "object") t.data = {};
        t.data[key] = value;
      },
      // --- Диалоги приложения ---
      // Диалог подтверждения открывается поверх уже открытого ящика, и
      // корень фокус в него не переводит (для него слой не новый). Переводим
      // сами — на «Отмену»: Enter по ошибке ничего не сотрёт.
      confirm(message, opts) {
        const root = this.$root;
        const back = document.activeElement;
        const p = root.askConfirm(message, opts || {});
        this.$nextTick(() => {
          const b = document.querySelector(".dialog-modal .dialog-actions button");
          if (b) b.focus();
        });
        return p.then((ok) => {
          if (back && back.focus && document.contains(back)) back.focus();
          return ok;
        });
      },
      prompt(title, opts) {
        const back = document.activeElement;
        return this.$root.askPrompt(title, opts || {}).then((v) => {
          if (back && back.focus && document.contains(back)) back.focus();
          return v;
        });
      },
      goto(tab, group) {
        this.focusGroup = group || "";
        this.tab = tab;
        this.$nextTick(() => { const b = document.getElementById("horae-tab-" + tab); if (b) b.focus(); });
      },
      // «сообщение #N» в хронологии: закрываем ящик (он модальный и закрыл бы
      // ленту) и едем к реплике, догружая историю, если надо.
      jump(mid) {
        const root = this.$root;
        if ("drawerTab" in root) root.drawerTab = null;
        if (root.jumpToMessage) root.jumpToMessage(this.sessionId, Number(mid));
      },
      async openChat(sid) {
        const root = this.$root;
        try { if (root.syncChatList) await root.syncChatList(sid); } catch (e) { /* список дочитается сам */ }
        const card = root._sessionCard ? root._sessionCard(sid) : null;
        if (root.openSession) await root.openSession(card || { id: sid });
      },
      async enableRpg() {
        const r = await this.send("PUT", this.base + "/settings", { rpg_enabled: true });
        if (!r) return;
        this.layers = null;
        this.toast("RPG включён для этого чата");
        await this.reload();
      },
      // --- Вкладки ---
      onTabKey(e) {
        const k = e.key;
        if (k !== "ArrowRight" && k !== "ArrowLeft" && k !== "Home" && k !== "End") return;
        const ids = TABS.map((t) => t[0]);
        let i = ids.indexOf(this.tab);
        if (k === "ArrowRight") i = (i + 1) % ids.length;
        else if (k === "ArrowLeft") i = (i - 1 + ids.length) % ids.length;
        else if (k === "Home") i = 0;
        else i = ids.length - 1;
        e.preventDefault();
        this.goto(ids[i]);
      },
      // --- Задание (скан, свёртка) ---
      trackJob(job) { this.setJob(job); },
      setJob(job) {
        const prev = this.job;
        this.job = job || null;
        if (jobActive(this.job)) { this.schedulePoll(); return; }
        this.stopJob();
        if (prev && jobActive(prev) && this.job) this.jobDone(this.job);
      },
      // 1,5 с, как у мастер-памяти; после трёх сбоев подряд — реже, до 10 с.
      schedulePoll() {
        clearTimeout(this._jobTimer);
        const f = this.jobFails;
        const d = f < 3 ? 1500 : Math.min(10000, 1500 * Math.pow(2, f - 3));
        this._jobTimer = setTimeout(() => { this._jobTimer = null; this.pollJob(); }, d);
      },
      stopJob() { clearTimeout(this._jobTimer); this._jobTimer = null; },
      async pollJob() {
        const sid = this.sessionId;
        if (!sid || this._dead) return;
        try {
          const r = await this.$root.api(this.base + "/job");
          if (sid !== this.sessionId || this._dead) return;
          this.jobFails = 0;
          this.setJob(r && own(r, "job") ? r.job : r);
        } catch (e) {
          if (sid !== this.sessionId || this._dead) return;
          if (e && e.status && e.status < 500) {
            // Задания нет (404) — нечего ждать: перечитываем состояние.
            const prev = this.job;
            this.job = null;
            this.stopJob();
            if (prev && jobActive(prev)) this.reload();
            return;
          }
          this.jobFails += 1;
          this.schedulePoll();
        }
      },
      jobDone(job) {
        const summary = job.kind === "summary";
        const what = summary ? "Свёртка" : "Скан истории";
        let text = "";
        if (job.status === "done") {
          text = what + (summary ? " готова" : " готов")
            + (job.processed != null ? ": обработано " + job.processed + (job.total ? " из " + job.total : "") : "");
        } else if (job.status === "error") text = "⚠ " + what + ": " + (job.error || "ошибка");
        else if (job.status === "cancelled") text = what + (summary ? " остановлена" : " остановлен") + ", готовое сохранено";
        if (text) { this.toast(text); this.live = text; }
        this.reload();
        // «Остановить» исчез вместе с заданием; стоял на нём фокус — вернём
        // его на открытую вкладку, а не на body.
        this.$nextTick(() => {
          const a = document.activeElement;
          if (a && a !== document.body) return;
          const b = document.getElementById("horae-tab-" + this.tab);
          if (b) b.focus();
        });
      },
      async cancelJob() {
        const r = await this.send("POST", this.base + "/job/cancel", {});
        if (!r) return;
        if (r.job) this.setJob(r.job); else this.pollJob();
      },
      // --- Слои настроек: умолчания ← глобальные ← персонаж ← чат ---
      async ensureLayers() {
        if (!this.layers && !this._layersLoading) await this.loadLayers();
      },
      async loadLayers() {
        const sid = this.sessionId;
        const cid = this.characterId;
        this._layersLoading = true;
        this.layersError = "";
        try {
          const both = await Promise.all([
            this.$root.api("/horae/settings"),
            sid ? this.$root.api("/sessions/" + sid + "/horae/settings") : Promise.resolve(null),
          ]);
          let prof = null;
          if (cid) {
            try { prof = await this.$root.api("/characters/" + cid + "/horae_profile"); } catch (e) { prof = null; }
          }
          if (sid !== this.sessionId || this._dead) return;
          const g = obj(both[0]);
          const c = obj(both[1]);
          this.layers = {
            defaults: obj(g.defaults), global: obj(g.global),
            character: prof && prof.settings ? obj(prof.settings) : obj(c.character),
            chat: obj(c.overrides), effective: obj(c.effective || g.effective),
          };
        } catch (e) {
          this.layersError = "Настройки не загрузились: " + ((e && e.message) || e);
        } finally {
          this._layersLoading = false;
        }
      },
      setScope(s) {
        if (s === "character" && !this.characterId) return;
        this.scope = s;
      },
      order() {
        const all = ["chat", "character", "global", "defaults"];
        return all.slice(Math.max(0, all.indexOf(this.scope)));
      },
      originOf(key) {
        const L = this.layers;
        if (!L) return "defaults";
        for (const n of this.order()) { const l = L[n]; if (own(l, key) && l[key] != null) return n; }
        return "defaults";
      },
      valueOf(key) {
        const L = this.layers;
        if (!L) return undefined;
        const n = this.originOf(key);
        const l = L[n];
        return own(l, key) ? l[key] : undefined;
      },
      isSet(key) {
        const l = this.layers && this.layers[this.scope];
        return own(l, key) && l[key] != null;
      },
      originText(key) {
        const o = this.originOf(key);
        if (o === this.scope) return "Задано здесь.";
        if (o === "defaults") return "По умолчанию.";
        return "Унаследовано " + LAYER_FROM[o] + ".";
      },
      async saveLayer(patch) {
        let r;
        if (this.scope === "global") r = await this.send("PUT", "/horae/settings", patch);
        else if (this.scope === "chat") r = await this.send("PUT", this.base + "/settings", patch);
        else {
          if (!this.characterId) { this.toast("У этого чата нет персонажа"); return false; }
          // Профиль персонажа пишется целиком: берём текущий слой, убираем
          // сброшенные ключи и кладём новые. null оставляем в теле — сервер,
          // который сливает, а не заменяет, по нему тоже уберёт ключ.
          const cur = Object.assign({}, obj(this.layers && this.layers.character));
          for (const k of Object.keys(patch)) if (patch[k] === null) delete cur[k];
          r = await this.send("PUT", "/characters/" + this.characterId + "/horae_profile", { settings: Object.assign(cur, patch) });
        }
        if (!r) { await this.loadLayers(); return false; }
        await this.loadLayers();
        await this.reload();
        return true;
      },
      saveSetting(key, value) { return this.saveLayer({ [key]: value }); },
      resetSetting(key) { return this.saveLayer({ [key]: null }); },
      // Промпт ключа на выбранном уровне: первый уровень сверху, где ключ есть.
      // Пустая строка там — «вернуть по умолчанию» (так их сливает сервер).
      promptAt(key) {
        const L = this.layers;
        if (L) {
          for (const n of this.order()) {
            const p = obj(L[n] && L[n].prompts);
            if (own(p, key)) {
              const t = str(p[key]);
              return t.trim() ? { text: t, from: n, custom: true } : { text: "", from: n, custom: false };
            }
          }
        }
        return { text: "", from: "defaults", custom: false };
      },
      // Весь словарь промптов уровня, а не один ключ: сервер, который заменяет
      // prompts целиком, иначе потерял бы остальные.
      savePrompts(map) {
        const l = this.layers && this.layers[this.scope];
        return this.saveLayer({ prompts: Object.assign({}, obj(l && l.prompts), map) });
      },
    },
  };

  // ==========================================================================
  // Строка Horae под ответом ИИ и редактор меты сообщения
  // ==========================================================================
  const MSG_COLS = {
    desc: [{ key: "location", label: "Место" }, { key: "desc", label: "Описание", type: "textarea", wide: true }],
    costumes: [{ key: "name", label: "Кто" }, { key: "value", label: "Наряд", wide: true }],
    mood: [{ key: "name", label: "Кто" }, { key: "value", label: "Настроение", wide: true }],
    items: [
      { key: "icon", label: "Иконка", narrow: true }, { key: "name", label: "Название" },
      { key: "importance", label: "Важность", type: "select", options: IMPORTANCE },
      { key: "holder", label: "У кого" }, { key: "location", label: "Где" },
      { key: "description", label: "Описание", wide: true },
    ],
    removed: [{ key: "text", label: "Название", wide: true }],
    events: [{ key: "level", label: "Уровень", type: "select", options: LEVEL_OPTIONS }, { key: "text", label: "Событие", type: "textarea", wide: true }],
    affection: [
      { key: "name", label: "Кто" },
      { key: "mode", label: "Как", type: "select", options: [["set", "= стало"], ["add", "± сдвиг"]] },
      { key: "value", label: "Число", type: "number", narrow: true },
    ],
    agenda: [{ key: "date", label: "Дата", narrow: true }, { key: "text", label: "План", wide: true }],
    done: [{ key: "text", label: "План", wide: true }],
    rel: [{ key: "from", label: "Кто" }, { key: "to", label: "К кому" }, { key: "type", label: "Связь" }, { key: "note", label: "Примечание", wide: true }],
  };

  function mapRows(m, valueKey) {
    return Object.entries(obj(m)).map(([name, value]) => ({ name, [valueKey]: str(value) }));
  }

  // META (спека §4.2) → строки редактора.
  function metaToForm(meta) {
    const m = obj(meta);
    const t = obj(m.time);
    const sc = obj(m.scene);
    return {
      date: str(t.date), time: str(t.time),
      location: str(sc.location), atmosphere: str(sc.atmosphere),
      characters: arr(sc.characters).map(str).join(", "),
      desc: arr(sc.desc).map((d) => ({ location: str(obj(d).location), desc: str(obj(d).desc) })),
      costumes: mapRows(m.costumes, "value"),
      mood: mapRows(m.mood, "value"),
      items: Object.entries(obj(m.items)).map(([name, it]) => {
        it = obj(it);
        return { name, icon: str(it.icon), importance: str(it.importance), holder: str(it.holder), location: str(it.location), description: str(it.description) };
      }),
      items_removed: arr(m.items_removed).map((s) => ({ text: str(s) })),
      events: arr(m.events).map((e) => ({ level: levelOf(obj(e).level).id, text: str(obj(e).text) })),
      affection: Object.entries(obj(m.affection)).map(([name, a]) => {
        const o = a && typeof a === "object" ? a : { mode: "set", value: a };
        return { name, mode: o.mode === "add" ? "add" : "set", value: o.value == null ? "" : o.value };
      }),
      agenda: arr(m.agenda).map((a) => ({ date: str(obj(a).date), text: str(obj(a).text) })),
      agenda_done: arr(m.agenda_done).map((s) => ({ text: str(s) })),
      npcs: Object.entries(obj(m.npcs)).map(([name, f]) => {
        const row = { name };
        for (const [k] of NPC_FIELDS) row[k] = str(obj(f)[k]);
        return row;
      }),
      relationships: arr(m.relationships).map((r) => ({ from: str(obj(r).from), to: str(obj(r).to), type: str(obj(r).type), note: str(obj(r).note) })),
    };
  }

  // Строки редактора → META поверх копии прежней: таблицы, RPG, исходные
  // теги, pre_scan и всё, чего редактор не показывает, остаются как были.
  function formToMeta(f, original) {
    const out = clone(obj(original)) || {};
    const t = (s) => str(s).trim();
    const rowsToMap = (rows, valueKey) => {
      const o = {};
      for (const r of rows) if (t(r.name)) o[t(r.name)] = t(r[valueKey]);
      return o;
    };
    out.time = { date: t(f.date), time: t(f.time) };
    out.scene = Object.assign({}, obj(out.scene), {
      location: t(f.location), atmosphere: t(f.atmosphere), characters: splitList(f.characters),
      desc: f.desc.filter((d) => t(d.location) || t(d.desc)).map((d) => ({ location: t(d.location), desc: t(d.desc) })),
    });
    out.costumes = rowsToMap(f.costumes, "value");
    out.mood = rowsToMap(f.mood, "value");
    const items = {};
    for (const r of f.items) {
      if (!t(r.name)) continue;
      items[t(r.name)] = { icon: t(r.icon), importance: r.importance || "", holder: t(r.holder), location: t(r.location), description: t(r.description) };
    }
    out.items = items;
    out.items_removed = f.items_removed.map((r) => t(r.text)).filter(Boolean);
    out.events = f.events.filter((e) => t(e.text)).map((e) => ({ level: levelOf(e.level).id, text: t(e.text) }));
    const aff = {};
    for (const r of f.affection) {
      const v = Number(str(r.value).replace(",", "."));
      if (t(r.name) && str(r.value) !== "" && Number.isFinite(v)) aff[t(r.name)] = { mode: r.mode === "add" ? "add" : "set", value: v };
    }
    out.affection = aff;
    out.agenda = f.agenda.filter((a) => t(a.text)).map((a) => ({ date: t(a.date), text: t(a.text) }));
    out.agenda_done = f.agenda_done.map((r) => t(r.text)).filter(Boolean);
    const npcs = {};
    for (const r of f.npcs) {
      if (!t(r.name)) continue;
      const fields = {};
      for (const [k] of NPC_FIELDS) if (t(r[k])) fields[k] = t(r[k]);
      npcs[t(r.name)] = fields;
    }
    out.npcs = npcs;
    out.relationships = f.relationships.filter((r) => t(r.from) && t(r.to)).map((r) => ({ from: t(r.from), to: t(r.to), type: t(r.type), note: t(r.note) }));
    return out;
  }

  const HoraeMsg = {
    name: "HoraeMsg",
    components: { "h-rows": HRows },
    props: {
      message: { type: Object, required: true },
      sessionId: { type: [Number, String], default: null },
    },
    emits: ["changed"],
    data() {
      return { open: false, loading: false, err: "", info: null, form: null, dirty: false, busy: "", status: "", eid: uid("hmsg") };
    },
    computed: {
      mid() { return this.message ? this.message.id : null; },
      brief() {
        const b = this.info && this.info.brief != null ? this.info.brief : this.message.horae_brief;
        return str(b);
      },
      side() { return this.info ? !!this.info.side : !!this.message.horae_side; },
      raw() { return str(this.info && this.info.meta && this.info.meta.raw); },
      cols() { return MSG_COLS; },
      npcFields() { return NPC_FIELDS; },
    },
    watch: {
      // Свайп меняет и мету: открытый редактор без правок перечитываем,
      // закрытая строка берёт свежую сводку из списка сообщений.
      "message.active_swipe"() {
        if (this.open && !this.dirty) this.load();
        else if (!this.open) this.info = null;
      },
      "message.horae_brief"() { if (!this.open) this.info = null; },
    },
    template: `
      <div class="h-msg" :class="{ 'h-msg-open': open }">
        <button type="button" class="h-msg-line" :aria-expanded="open ? 'true' : 'false'" :aria-controls="eid"
                :title="brief || null" @click="toggle">
          <span aria-hidden="true">🕰</span><span class="sr-only">Данные Horae:</span>
          <span v-if="brief" class="h-msg-brief">{{ brief }}</span>
          <span v-else class="h-msg-empty">нет данных Horae</span>
          <span v-if="side" class="tag h-msg-side">побочная сцена</span>
        </button>
        <div v-if="open" :id="eid" class="h-msg-ed" role="region" :aria-label="'Данные Horae сообщения ' + mid" @keydown.esc="onEsc">
          <p v-if="loading" class="muted">Загружаю…</p>
          <p v-else-if="err" class="h-warn">⚠ {{ err }} <button type="button" @click="load">Повторить</button></p>
          <form v-else-if="form" class="h-msg-form" @submit.prevent="save" @input="dirty = true" @change="dirty = true">
            <p v-if="side" class="field-hint">Побочная сцена: сообщение не участвует в состоянии, хронологии, свёртках и поиске.</p>
            <fieldset class="h-fs">
              <legend>Время и место</legend>
              <div class="h-grid2">
                <label>Дата<input v-model="form.date" placeholder="2026/2/4" autocomplete="off" /></label>
                <label>Время<input v-model="form.time" placeholder="15:00" autocomplete="off" /></label>
                <label>Место<input v-model="form.location" autocomplete="off" /></label>
                <label>Атмосфера<input v-model="form.atmosphere" autocomplete="off" /></label>
                <label class="h-span-all">Присутствуют (через запятую)<input v-model="form.characters" autocomplete="off" /></label>
              </div>
            </fieldset>
            <h-rows :rows="form.events" legend="События" add-text="Событие" item-name="событие" :cols="cols.events" :blank="{ level: 'normal', text: '' }"></h-rows>
            <h-rows :rows="form.costumes" legend="Наряды" add-text="Наряд" item-name="наряд" :cols="cols.costumes" :blank="{ name: '', value: '' }"></h-rows>
            <h-rows :rows="form.mood" legend="Настроение" add-text="Настроение" item-name="настроение" :cols="cols.mood" :blank="{ name: '', value: '' }"></h-rows>
            <h-rows :rows="form.items" legend="Предметы" add-text="Предмет" item-name="предмет" :cols="cols.items"
                    :blank="{ icon: '', name: '', importance: '', holder: '', location: '', description: '' }"></h-rows>
            <h-rows :rows="form.items_removed" legend="Убранные предметы" add-text="Убранный предмет" item-name="убранный предмет" :cols="cols.removed" :blank="{ text: '' }"></h-rows>
            <h-rows :rows="form.affection" legend="Расположение" add-text="Расположение" item-name="расположение" :cols="cols.affection" :blank="{ name: '', mode: 'add', value: '' }"></h-rows>
            <h-rows :rows="form.agenda" legend="Планы" add-text="План" item-name="план" :cols="cols.agenda" :blank="{ date: '', text: '' }"></h-rows>
            <h-rows :rows="form.agenda_done" legend="Выполненные планы" add-text="Выполненный план" item-name="выполненный план" :cols="cols.done" :blank="{ text: '' }"></h-rows>
            <fieldset class="h-fs">
              <legend>Персонажи (NPC)</legend>
              <div v-for="(n, i) in form.npcs" :key="i" class="h-rrow">
                <div class="h-rrow-fields">
                  <label class="h-rf h-rf-wide"><span class="h-rf-l">Имя</span><input v-model="n.name" autocomplete="off" /></label>
                  <details class="h-npc-det h-rf-wide">
                    <summary>Поля: {{ npcFilled(n) }}</summary>
                    <div class="h-grid2">
                      <label v-for="f in npcFields" :key="f[0]" class="h-rf"><span class="h-rf-l">{{ f[1] }}</span><input v-model="n[f[0]]" autocomplete="off" /></label>
                    </div>
                  </details>
                </div>
                <button type="button" class="btn-icon h-rrow-del" @click="form.npcs.splice(i, 1)" :aria-label="'Удалить персонажа ' + (n.name || i + 1)">✕</button>
              </div>
              <p v-if="!form.npcs.length" class="field-hint">Пусто.</p>
              <button type="button" class="h-add" @click="addNpc">＋ Персонаж</button>
            </fieldset>
            <h-rows :rows="form.relationships" legend="Отношения" add-text="Связь" item-name="связь" :cols="cols.rel" :blank="{ from: '', to: '', type: '', note: '' }"></h-rows>
            <h-rows :rows="form.desc" legend="Описания мест" add-text="Описание" item-name="описание" :cols="cols.desc" :blank="{ location: '', desc: '' }"></h-rows>
            <details v-if="raw" class="h-det">
              <summary>Исходные теги</summary>
              <pre class="h-raw">{{ raw }}</pre>
            </details>
            <div class="h-msg-actions">
              <button type="submit" class="btn-primary" :disabled="!!busy">{{ busy === 'save' ? 'Сохраняю…' : 'Сохранить' }}</button>
              <button type="button" :disabled="!!busy" @click="analyze">{{ busy === 'analyze' ? 'ИИ читает…' : '✨ ИИ-анализ' }}</button>
              <button type="button" :disabled="!!busy" :class="{ 'btn-primary': side }" :aria-pressed="side ? 'true' : 'false'" @click="toggleSide">🎭 Побочная сцена</button>
              <button type="button" class="btn-ghost" @click="cancel">Отмена</button>
            </div>
            <p class="sr-only" role="status" aria-live="polite">{{ status }}</p>
          </form>
        </div>
      </div>`,
    methods: {
      toast(t) { if (this.$root.showToast) this.$root.showToast(t); },
      npcFilled(n) {
        const got = NPC_FIELDS.filter(([k]) => str(n[k]).trim()).map(([, l]) => l.toLowerCase());
        return got.length ? got.join(", ") : "не заданы";
      },
      addNpc() {
        const row = { name: "" };
        for (const [k] of NPC_FIELDS) row[k] = "";
        this.form.npcs.push(row);
        this.dirty = true;
      },
      toggle() {
        if (this.open) { this.cancel(); return; }
        this.open = true;
        this.load();
      },
      async load() {
        if (this.mid == null) return;
        this.loading = true;
        this.err = "";
        try {
          this.apply(await this.$root.api("/messages/" + this.mid + "/horae"));
        } catch (e) {
          this.err = "Данные сообщения не загрузились: " + ((e && e.message) || e);
        } finally {
          this.loading = false;
        }
      },
      apply(r) {
        this.info = r && typeof r === "object" ? r : {};
        this.form = metaToForm(this.info.meta);
        this.dirty = false;
      },
      async call(kind, method, path, body) {
        this.busy = kind;
        try {
          const opts = { method };
          if (body !== undefined) opts.body = JSON.stringify(body);
          return await this.$root.api(path, opts);
        } catch (e) {
          this.toast("⚠ " + ((e && e.message) || e));
          return null;
        } finally {
          this.busy = "";
        }
      },
      async save() {
        const meta = formToMeta(this.form, this.info && this.info.meta);
        const r = await this.call("save", "PUT", "/messages/" + this.mid + "/horae", { meta });
        if (!r) return;
        this.apply(r);
        this.status = "Сохранено";
        this.toast("Данные сообщения сохранены");
        this.$emit("changed", this.mid);
      },
      async analyze() {
        if (this.dirty) {
          const ok = await this.$root.askConfirm("Несохранённые правки пропадут: ИИ перепишет данные сообщения заново.", { title: "ИИ-анализ", okText: "Анализировать" });
          if (!ok) return;
        }
        this.status = "ИИ читает сообщение…";
        const r = await this.call("analyze", "POST", "/messages/" + this.mid + "/horae/analyze", {});
        if (!r) { this.status = ""; return; }
        this.apply(r);
        this.status = "ИИ-анализ готов";
        this.toast("✨ ИИ-анализ готов");
        this.$emit("changed", this.mid);
      },
      async toggleSide() {
        const r = await this.call("side", "POST", "/messages/" + this.mid + "/horae/side", { side: !this.side });
        if (!r) return;
        const keep = this.dirty ? this.form : null;
        this.apply(r);
        if (keep) { this.form = keep; this.dirty = true; }
        this.status = this.side ? "Помечено как побочная сцена" : "Снова основная сцена";
        this.$emit("changed", this.mid);
      },
      async cancel() {
        if (this.dirty) {
          const ok = await this.$root.askConfirm("Закрыть без сохранения? Правки пропадут.", { title: "Несохранённые правки", okText: "Закрыть" });
          if (!ok) return;
        }
        this.open = false;
        this.form = null;
        this.dirty = false;
        this.info = null;
        this.err = "";
        this.$nextTick(() => { const b = this.$el && this.$el.querySelector(".h-msg-line"); if (b) b.focus(); });
      },
      onEsc(e) {
        if (this.$root.dialog) return;
        e.preventDefault();
        e.stopPropagation();
        this.cancel();
      },
    },
  };

  window.HoraeUI = {
    stripTags,
    hasOpenTag,
    components: { HoraePanel, HoraeMsg },
    // Для проверок и отладки: чистые функции без Vue.
    _internals: { normalize, metaToForm, formToMeta, genderKind, parseAttrs },
  };
})();
