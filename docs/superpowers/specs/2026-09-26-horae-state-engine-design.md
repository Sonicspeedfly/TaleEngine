# Horae State Engine — перенос плагина Horae (SillyTavern) — дизайн

Дата: 2026-09-26 · Версия продукта: 2.6.0 · Ветка: `claude/dazzling-brahmagupta-r5mmjo`
Источник: плагин `ceh51453-alt/horae` v1.15.1 (`core/horaeManager.js`,
`core/vectorManager.js`, `utils/timeUtils.js`, `index.js`, `prompts/ru/*`).

## 1. Задача

В TaleEngine «Horae» до сих пор был лорбуком (World Info), мастер-снимком и
атомарными фактами. Главного, ради чего существует плагин Horae, не было:
**структурного учёта состояния сюжета**. Модель в конце каждого ответа пишет
служебные теги `<horae>`/`<horaeevent>` (время, место, атмосфера, присутствующие,
костюмы, предметы, NPC, расположение, планы, настроение, отношения, память сцен,
события), плагин их разбирает, прячет из текста и из истории, копит состояние и
на каждом ходу подмешивает его в промпт компактным блоком. При импорте чатов из
SillyTavern эти данные у нас сплющивались в один текст.

Переносим плагин целиком (кроме оформления, тем, обучения и внешнего API Port),
на сервер, с исправлением его известных ошибок, и делаем настраиваемым:
каждый модуль включается отдельно, настройки — глобально, на персонажа и на чат.

Существующие слои памяти (лорбук, мастер-снимок, факты, окно) остаются как есть
и работают вместе с новым слоем.

## 2. Решения

- **Теги вырезаются при сохранении ответа.** В `Message.content`/`swipes` лежит
  чистый текст, разобранные данные — в `Message.horae.metas[i]` (по свайпу). Так
  теги не показываются, не уходят в историю (плагин делал то же регэкспами
  SillyTavern), а свайп переключает и данные.
- **Состояние — повтор (replay).** Агрегат не хранится: он считается заново
  по метам сообщений (в порядке id) плюс журнал правок пользователя. Правки
  (`ops`) привязаны к моменту — id последнего сообщения на момент правки — и
  встают в повтор в своё место: позже ИИ может обновить поле снова (как в
  плагине, где правка записывалась в последнее сообщение), а откат правки — это
  просто удаление записи журнала.
- **Свёртки хронологии покрывают диапазон id сообщений**, а не отдельные
  события: в плагине ручное сжатие всё равно помечает ВСЕ события диапазона.
  Хранятся в состоянии чата, ни одно событие сообщения не мутируется.
- **«Скрытие» сообщений** плагина (`/hide`) у нас делает активное окно:
  сообщение старше окна выпадает из промпта, если его покрывает мастер-снимок
  ИЛИ (настройка `summary_hides`) активная свёртка хронологии.
- **Правила тегов — в системный промпт** (кэшируемый префикс, ≈3–4 тыс. токенов
  по полной цене иначе на КАЖДОМ ходу); короткое напоминание формата — в хвост,
  перед фокусом. Опция `rules_position: "tail"` переносит правила в хвост.
- **Блок состояния и хронология — в хвост** (меняются каждый ход, иначе ломали
  бы кэш истории), после мастер-снимка. Их вес резервируется в бюджете хода.
- **Вспоминание событий** (порт vectorManager) — по документам мет сообщений,
  векторно при заданной `embedding_model`, иначе по словам (тот же лексический
  движок, что у фактов). Сообщения, видимые модели дословно, не вспоминаются.
- **Служебные запросы** (анализ, пакетный скан, свёртки, сжатие, обогащение
  NPC, переписывание запроса) идут на `aux_model` (пусто → `summary_model` →
  модель чата), последовательно, с паузой — аналог «вспомогательного API».
- **Режим «вне роли» (OOC)** не просит теги (ни правил, ни напоминания);
  блок состояния остаётся.
- **Канвас** не получает правил тегов, а случайные теги из его ответа вырезаются.

## 3. Модули

| Модуль | Что | Зависимости |
|---|---|---|
| `backend/horae_time.py` | даты сюжета, относительное время, календарь, возраст | stdlib |
| `backend/horae_state.py` | формат меты, парсер тегов, вырезание, повтор (агрегация), журнал правок, рендер блока состояния и хронологии, документ для поиска | stdlib + horae_time, horae_rpg, horae_tables |
| `backend/horae_rpg.py` | RPG: разбор `<horaerpg>`, применение изменений, рендер, правила | stdlib |
| `backend/horae_tables.py` | таблицы: разбор `<horaetable:…>`, повтор, правка структуры, рендер | stdlib |
| `backend/horae_prompts.py` | промпты по умолчанию (RU), сборка правил, анализ/скан/свёртки/сжатие, разбор ответов | stdlib |
| `backend/horae_settings.py` | настройки по умолчанию, разрешение глобально → персонаж → чат, очистка | stdlib |
| `backend/horae_engine.py` | сервис: БД, сохранение ответа, правки, свёртки, скан, анализ, перенос, импорт/экспорт | БД, llm_gateway |
| `backend/horae_vector.py` | индекс документов сообщений и вспоминание | БД, llm_gateway, horae_recall |
| `backend/horae_api.py` | эндпоинты (APIRouter, подключается в main.py до статики) | FastAPI |
| `frontend/horae.js` | компоненты Vue: панель Horae, панель под сообщением, вырезание тегов при стриме | Vue (глобальный) |

Чистые модули (`horae_time/state/rpg/tables/prompts/settings`) не импортируют
БД, FastAPI и litellm — тестируются без сети.

## 4. Хранение

### 4.1 Message.horae (новая JSON-колонка, nullable)

```json
{"metas": [META | null, ...], "side": false}
```
`metas[i]` — мета свайпа `i` (выравнено со `swipes`). Активная мета —
`metas[active_swipe]` (нет индекса — `null`). `side` — «побочная сцена»
(`_skipHorae` плагина): сообщение не участвует в состоянии, хронологии,
свёртках, поиске и переносе.

### 4.2 META (формат меты сообщения)

```json
{
  "time": {"date": "2026/2/4", "time": "15:00"},
  "scene": {"location": "Таверна·зал", "atmosphere": "шумно",
            "characters": ["Вольф", "Марина"],
            "desc": [{"location": "Таверна·зал", "desc": "..."}]},
  "costumes": {"Вольф": "кожаная куртка"},
  "mood": {"Марина": "напряжена"},
  "items": {"Старый квас(3 бутылки)": {"icon": "🍾", "importance": "", "holder": "{{user}}",
            "location": "полка кладовой", "description": "кисловатый квас"}},
  "items_removed": ["Старый квас"],
  "events": [{"level": "normal", "text": "..."}],
  "affection": {"Вольф": {"mode": "set", "value": 30}},
  "npcs": {"Вольф": {"appearance": "...", "personality": "...", "relationship": "...",
           "gender": "...", "age": "...", "race": "...", "job": "...",
           "birthday": "...", "note": "..."}},
  "agenda": [{"date": "2026/2/10", "text": "..."}],
  "agenda_done": ["..."],
  "relationships": [{"from": "A", "to": "B", "type": "друзья", "note": ""}],
  "tables": [{"name": "Квесты", "cells": {"1,1": "..."}}],
  "rpg": { RPG_CHANGES },
  "raw": "<horae>…</horae>\n<horaeevent>…</horaeevent>",
  "source": "tags" | "loose" | "ai" | "scan" | "user" | "import",
  "pre_scan": META | null
}
```

- Пустые поля можно опускать; `empty_meta()` даёт все ключи.
- `events[].level`: `normal` | `important` | `critical`. Разбор принимает
  `normal/обычное/一般`, `important/важное/重要`, `critical/key/ключевое/критическое/关键`.
- `npcs[имя]` содержит только поля, пришедшие в строке (частичное обновление).
- `affection.mode`: `set` (абсолютное) | `add` (сдвиг).
- `scene.desc` — пары место→описание (у плагина `_descPairs` не сохранялись —
  исправлено). Строка `scene_desc:` без места выше по блоку привязывается к
  `scene.location` блока.
- `items`: ключ — имя с количеством, как написала модель. Базовое имя —
  без хвостового `(число …)`: `Пиво(3 бутылки)` и `Пиво(2 бутылки)` — один
  предмет (у плагина кириллические единицы не срезались — исправлено).
- `raw` — исходные теги (для панели сообщения и отладки).
- `pre_scan` — мета до пакетного скана (для отмены скана).

### 4.3 HoraeChatState (новая таблица)

`session_id` (PK, FK chat_sessions.id), `data` JSON, `updated_at`.

```json
{
  "v": 1,
  "ops": [OP, ...],
  "summaries": [SUMMARY, ...],
  "tables": [LOCAL_TABLE, ...],
  "table_overlays": {"<template_id>": {"base": {"r,c": "..."}, "base_anchor": 0, "rows": 3, "cols": 3}},
  "rpg_config": { RPG_CONFIG },
  "settings": { ...переопределения настроек для чата... },
  "seed": STATE | null,
  "pinned_npcs": ["..."],
  "favorite_npcs": ["..."],
  "scan": {"mids": [..], "at": "ISO"} | null,
  "summary_error": {"message": "...", "at": "ISO"} | null,
  "seq": 0
}
```

- **OP** — правка пользователя: `{"id": "op_<n>", "seq": n, "at": <mid>, "kind": "...", ...payload, "created_at": ISO}`.
  `at` — id последнего сообщения чата в момент правки (0 — сообщений нет).
- **SUMMARY** — свёртка хронологии:
  `{"id": "s_<n>", "kind": "auto"|"manual"|"compress"|"carry", "range": [from_mid, to_mid],
  "text": "...", "depth": 1, "active": true, "created_at": ISO,
  "date_from": "...", "date_to": "...", "events": <число покрытых событий>,
  "children": [SUMMARY, ...]}`.
  Покрывает все события несторонних сообщений с id в `range` (включительно).
  `kind: "carry"` — пересказ, перенесённый из прошлого чата (`range` = [0, 0]),
  всегда в начале хронологии.
- **seed** — стартовое состояние (перенос в новый чат): повтор начинается с его копии.

### 4.4 Прочее

- `Character.horae_profile` (новая JSON-колонка): `{"settings": {...}, "tables": [TEMPLATE, ...]}`.
- `AppSetting["horae"]` — глобальные настройки (словарь ключей §7).
- `AppSetting["horae_library"]` — `{"global_tables": [TEMPLATE], "prompt_presets": [PRESET], "equipment_templates": [EQ_TEMPLATE]}`.
- `HoraeMemoryDoc` (новая таблица, для поиска): `id`, `session_id` (idx),
  `message_id` (nullable; `null` — перенесённый документ), `origin` (`""` или
  `"carry:<session_id>"`), `doc_hash`, `document`, `content` (для перенесённых —
  текст исходного сообщения для «полного текста»), `brief` JSON (дата, время,
  место, персонажи, костюмы, события, NPC, предметы — для строки воспоминания
  перенесённого документа), `embedding` JSON, `embed_model`, `created_at`.

## 5. Разбор тегов (`horae_state.parse_reply`)

`parse_reply(text, ctx) -> (clean_text, meta | None)`; `ctx` — `ParseContext`
(имя пользователя, режимы RPG «только пользователь», список тегов для удаления
`strip_tags`).

1. Удалить блоки пользовательских тегов `strip_tags` (`<tag …>…</tag>`) — только
   для разбора, не из текста.
2. Теги внутри `<think>/<thinking>` не разбираются и не вырезаются.
3. Блоки: `<horae>…</horae>` (несколько — последний, в котором есть строка поля;
   иначе последний; нет — `<!--horae … -->`), `<horaeevent>…</horaeevent>`
   (последний со строкой `event:`), все `<horaetable:ИМЯ>…</horaetable>`,
   `<horaerpg>…</horaerpg>` (последний непустой). Двоеточие в `horaetable` —
   `:` или `：`.
4. Строки `<horae>` и `<horaeevent>` разбираются одним списком; префиксы:
   `time: location: atmosphere: scene_desc: characters: costume: item-: item!!:
   item!: item: event: affection: npc: agenda-: agenda: rel: mood:` — регистр
   ключа не важен, двоеточие `:` или `：`. Правила — как в плагине (spec_core §2.2)
   с исправлениями: уровни событий (см. 4.2); `~occupation:`/`~профессия:` =
   job, `~пол:`, `~возраст:`, `~раса:`, `~день рождения:`, `~примечание:`;
   U+FE0F срезается из имени предмета; `agenda:… (выполнено|отменено|done…)` →
   `agenda_done`.
5. Нет ни одного блока → свободный разбор строк `ключ: значение` в начале строки
   (`loose`, только если найдено хотя бы `time|location|characters|event`),
   иначе `None`.
6. `clean_text` — текст без блоков `<horae>`, `<horaeevent>`, `<horaetable:…>`,
   `<horaerpg>`, `<!--horae…-->` (вне `<think>`), с обрезанными хвостовыми
   пробелами. Свободный разбор текст не меняет.
7. `strip_tags_text(text, partial=False)` — вырезание без разбора; `partial=True`
   отрезает и незакрытый хвостовой блок (для стрима).

## 6. Повтор (агрегация) — `horae_state.replay`

```python
replay(entries, *, ops=(), seed=None, until=None, settings, names) -> State
```
- `entries` — `[(mid, role, meta, side)]` по возрастанию `mid` (только роль
  `assistant` несёт мету; `user` — может в режиме «без пересказа», если
  пользователь сам правил).
- `until` — учитывать сообщения с `mid <= until` и правки с `at <= until`
  (для «состояния на момент» и перегенерации: `until = target_id - 1`).
- Перед сообщением `m` применяются правки с `at < m.mid`, после всех сообщений —
  оставшиеся (правка с `at == m.mid` идёт сразу после `m`).
- Стороннее сообщение (`side`) пропускается целиком.

Семантика по полям — как у плагина (spec_core §3.1):
время и место — последнее непустое (`prev_location` запоминается), `characters` —
последний непустой список, костюмы и настроение — слияние; предметы — по
базовому имени (регистронезависимо), важность только растёт, `locked` защищает
иконку/важность/описание, количество `(0 …)`, пометка «израсходовано/использовано/
уничтожено/consumed» или держатель «нет/израсходовано/none» удаляет; удаления
после добавлений; расположение `set`/`add`; NPC: обновляемые поля
`appearance/personality/relationship/age/job/note`, защищённые (только если
пусто) `gender/race/birthday`, `age_ref` = дата сюжета при появлении/смене
возраста; планы: добавление (без дублей по тексту, не из «заблокированных»
пользователем), `agenda_done` удаляет совпадения (регистронезависимо, подстрока в
обе стороны) — НЕ разрушительно, повтор без сообщения их вернёт; отношения —
по паре (from, to), правленные пользователем не перезаписываются ИИ; память
сцен — последнее описание места, правленные/удалённые пользователем не
перезаписываются; RPG — `horae_rpg.apply_changes`; таблицы — отдельно
(`horae_tables.replay`).

ID: предметы и NPC получают `001, 002, …` по порядку появления (счётчик только
растёт; удалённый id не переиспользуется).

Псевдонимы: `npc.rename` регистрирует `старое → новое`; все имена из мет
(npcs, affection, costumes, mood, characters, relationships, владельцы RPG,
держатели предметов) проходят через карту псевдонимов.

### 6.1 STATE (результат)

```python
{
  "time": {"date": str, "time": str}, "prev_location": str,
  "scene": {"location": str, "atmosphere": str, "characters": [str]},
  "costumes": {name: str}, "mood": {name: str},
  "items": {name: {"id": "001", "icon", "importance", "holder", "location",
                   "description", "locked": bool, "mid": int}},
  "affection": {name: float},
  "npcs": {name: {"id": "001", appearance, personality, relationship, gender, age,
                  race, job, birthday, note, "aliases": [str], "age_ref": str,
                  "first_mid": int, "last_mid": int, "user": bool}},
  "agenda": [{"date", "text", "source": "ai"|"user", "mid": int}],
  "blocked_agenda": [str],
  "relationships": [{"from","to","type","note","user": bool}],
  "locations": {name: {"desc", "first_mid", "mid", "user": bool, "deleted": bool, "aliases": [str]}},
  "events": [{"mid", "i", "level", "text", "date", "time"}],
  "rpg": RPG_STATE | None,
  "aliases": {old: new},
  "counters": {"item": n, "npc": n},
  "last_mid": int
}
```

### 6.2 OP — виды правок

| kind | payload | действие |
|---|---|---|
| `npc.set` | `name, fields{…}` | поля NPC (пустая строка очищает) |
| `npc.add` | `name, fields{…}, aliases[]` | новый NPC (если есть — как `npc.set`) |
| `npc.rename` | `from, to` | переименование во всём состоянии + псевдоним |
| `npc.delete` | `name` | удалить NPC, его расположение, костюм, настроение, из присутствующих |
| `affection.set` | `name, value` | абсолютное значение |
| `affection.delete` | `name` | удалить |
| `item.set` | `name, fields{icon, importance, description, holder, location}, rename?` | правка |
| `item.add` | `name, fields{…}` | новый предмет |
| `item.delete` | `name` | удалить (по базовому имени) |
| `item.lock` | `name, locked` | защита от правок ИИ |
| `agenda.add` | `date, text` | пункт пользователя |
| `agenda.edit` | `text, new_text, date` | правка |
| `agenda.delete` | `text` | удалить + заблокировать повторное добавление ИИ |
| `rel.set` | `from, to, type, note` | отношение (помечается `user`) |
| `rel.delete` | `from, to` | удалить |
| `location.set` | `name, desc` | описание места (`user`) |
| `location.rename` | `from, to` | переименование + псевдоним |
| `location.delete` | `name` | надгробие (ИИ не пересоздаст) |
| `location.merge` | `from, to` | описание `from` дописывается к `to`, `from` удаляется |
| `scene.set` | `date?, time?, location?, atmosphere?, characters?` | поправить текущую сцену |
| `costume.set` / `mood.set` | `name, value` (пусто — удалить) | поправить |
| `rpg.*` | см. §9 | правки RPG |

## 7. Настройки (`horae_settings.DEFAULTS`)

Разрешение: `DEFAULTS ← AppSetting["horae"] ← Character.horae_profile.settings ←
HoraeChatState.settings`. Неизвестные ключи отбрасываются, числа зажимаются.

| Ключ | По умолчанию | Что |
|---|---|---|
| `enabled` | true | главный выключатель слоя Horae |
| `parse_tags` | true | разбирать теги в ответах |
| `inject_state` | true | блок состояния в промпт |
| `rules_position` | `"system"` | `system` / `tail` |
| `tag_reminder` | true | короткое напоминание формата в хвосте |
| `auto_analyze` | `"gaps"` | нет тегов в ответе → фоновый ИИ-анализ: `off` / `gaps` (только если модель писала теги в одном из последних 10 ответов) / `always` |
| `anti_paraphrase` | false | режим «без пересказа» |
| `aux_model` | `""` | модель служебных запросов (пусто — summary_model, затем модель чата) |
| `aux_delay_ms` | 1000 | пауза между служебными запросами |
| `strip_tags` | `""` | теги, вырезаемые перед разбором/поиском (через запятую) |
| `send_timeline` | true | хронология и справка по времени |
| `context_depth` | 15 | сколько последних обычных событий |
| `send_characters` | true | присутствующие, костюмы, NPC |
| `send_affection` | true | расположение |
| `send_main_personality` | true | характер главных (закреплённых) персонажей |
| `send_items` | true | предметы |
| `send_agenda` | true | планы |
| `send_location_memory` | false | память сцен (+ правило `scene_desc`) |
| `send_relationships` | false | сеть отношений (+ правило `rel`) |
| `send_mood` | false | настроение (+ правило `mood`) |
| `summary_enabled` | false | авто-свёртка хронологии |
| `summary_keep_recent` | 5 | сколько последних ответов ИИ не сворачивать (≥ 3) |
| `summary_source` | `"fulltext"` | `fulltext` / `events` |
| `summary_buffer_mode` | `"messages"` | `messages` / `tokens` |
| `summary_buffer_messages` | 10 | порог ответов ИИ (≥ 5) |
| `summary_buffer_tokens` | 30000 | порог токенов (≥ 1000) |
| `summary_batch_messages` | 50 | потолок пакета, событий (≥ 5) |
| `summary_batch_tokens` | 80000 | потолок пакета, токенов (≥ 10000) |
| `resummary_threshold` | 7 | свёрток одного уровня для свёртки выше (0 — выкл., иначе ≥ 2) |
| `resummary_min_chars` | 800 | минимум текста для свёртки выше |
| `summary_hides` | true | активная свёртка разрешает окну выбросить покрытые сообщения |
| `recall_enabled` | true | вспоминание событий из давней части чата |
| `recall_top_k` | 5 | 1–10 |
| `recall_threshold` | 0.72 | порог сходства векторов 0.3–0.95 |
| `recall_lexical_threshold` | 0.3 | порог лексического сходства 0.1–0.9 |
| `recall_full_text_count` | 3 | сколько лучших воспоминаний дать полным текстом 0–5 |
| `recall_full_text_threshold` | 0.9 | 0.6–1 |
| `recall_full_text_chars` | 3000 | потолок полного текста одного сообщения |
| `recall_pure` | false | только смысловой поиск (без ключевых слов) |
| `recall_rerank` | false | переранжирование (`recall_rerank_model`) |
| `recall_rerank_model` | `""` | |
| `recall_rerank_candidates` | 25 | ≥ 5 |
| `recall_rerank_min_score` | 0.5 | 0–1 |
| `recall_query_rewrite` | false | переписать запрос служебной моделью (INTENT + 5 Q) |
| `rpg_enabled` | false | RPG |
| `rpg_strict_present` | false | RPG только при присутствующих |
| `rpg_bars` / `rpg_skills` / `rpg_attrs` | true | модули RPG |
| `rpg_reputation` / `rpg_equipment` / `rpg_level` / `rpg_currency` / `rpg_stronghold` | false | модули RPG |
| `rpg_user_only` | `[]` | модули только для пользователя: `bars, skills, attrs, reputation, equipment, level, currency` |
| `rpg_bar_config` | hp/mp/sp | `[{key, name, color, max, desc}]` |
| `rpg_attr_config` | str…cha | `[{key, name, desc}]` |
| `calendar` | `{enabled:false, months:[]}` | `months: [{name, days}]` |
| `prompts` | `{}` | свои промпты: ключ → текст (пусто — по умолчанию) |

Ключи промптов: `system, analysis, batch, compress_events, compress_fulltext,
auto_summary, auto_resummary, tables, location, relationship, mood, rpg,
anti_paraphrase, query_rewrite, reminder`.

Экспорт профиля: все ключи §7. Импорт: те же ключи, очистка.

## 8. Промпт хода

Системный промпт (после STYLE_GUIDE, если `rules_position == "system"`):
правила Horae — `system` с подстановкой `${sceneDescLine}/${relLine}/${moodLine}`
и `${systemPromptAddition}` (память сцен, таблицы, отношения, настроение, RPG,
«без пересказа», календарь), `{{user}}` → имя персоны («Пользователь»),
`{{char}}` → имя персонажа. STYLE_GUIDE при включённом Horae уточняет, что
служебные теги Horae в конце ответа — не HTML-разметка и обязательны.

Хвост (после мастер-снимка): `("horae_state", блок состояния)`, после фактов —
`("horae_recall", вспомнившиеся события)`, перед фокусом —
`("horae_reminder", напоминание)`. Монитор относит `horae_state` и
`horae_recall` к памяти (Tier 2), напоминание — к Tier 1.

Блок состояния — порт `generateCompactPrompt` (RU-подписи, spec_core §4):
`[Снимок текущего состояния — …]`, `[Время|…]`, `[Время (справка)|…]`,
`[Сцена|…|…]`, `[Память сцены|…]`, `[Присутствуют|…]`, `[Настроение|…]`,
`[Сеть отношений]`, `[Список предметов]`, `[Расположение|…]`, `[Известные NPC]`,
`[Список дел]`, RPG, `[Сюжетная линия]`, таблицы. День недели — русский.

Перегенерация и «Продолжить»: состояние на `target_id − 1` (без меты
перегенерируемого ответа; `skipLast` плагина).

## 9. RPG (`horae_rpg`)

Разбор строк `<horaerpg>` — spec_core §2.4 с исправлениями (`xp:` не шкала,
«нормально/норма/нет» очищает статусы). RPG_CHANGES:
`{bars:{owner:{key:[cur,max,label?]}}, status:{owner:[..]}, skills:[{owner,name,level,desc}],
skills_removed:[{owner,name}], attrs:{owner:{key:int}}, reputation:{owner:{cat:int}},
equip:[{owner,slot,name,attrs}], unequip:[{owner,slot,name}], levels:{owner:int},
xp:{owner:[cur,max]}, currency:[{owner,name,value,delta}], base:[{path,field,value}]}`.

RPG_STATE: `{bars:{owner:{key:{cur,max,label}}}, status:{owner:[..]},
skills:{owner:[{name,level,desc,user}]}, attrs:{owner:{key:int}},
reputation:{owner:{cat:{value,sub:{}}}}, equipment:{owner:{slot:[{name,attrs,item}]}},
levels:{owner:int}, xp:{owner:[cur,max]}, currency:{owner:{name:int}},
strongholds:[{id,name,level,desc,parent}]}`.

RPG_CONFIG (на чат): `{reputation:[{name,min,max,default,sub:[..]}],
currencies:[{name,rate,emoji}], equipment:{locked, chars:{owner:{slots:[{name,max}],
forms:[{id,name,slots}], form}}}}`.

Правки: `rpg.bar {owner,key,cur,max}`, `rpg.status {owner,effects}`,
`rpg.attr {owner,key,value}`, `rpg.skill.add {owner,name,level,desc}`,
`rpg.skill.delete {owner,name}`, `rpg.level {owner,value}`, `rpg.xp {owner,cur,max}`,
`rpg.currency {owner,name,value}`, `rpg.rep {owner,cat,value}`,
`rpg.equip {owner,slot,name,attrs}`, `rpg.unequip {owner,slot,name}`,
`rpg.base {path,level?,desc?}`, `rpg.base.delete {path}`.

## 10. Таблицы (`horae_tables`)

TEMPLATE (глобальная/персонажа): `{id, name, prompt, rows, cols, locked_rows,
locked_cols, locked_cells, headers: {"0,c"|"r,0": text}}`. LOCAL_TABLE:
то же + `base`, `base_anchor`. Данные = `base` + вклады ИИ из сообщений с
`mid > base_anchor` (ячейка заголовка — только если пуста; заблокированные
строки/столбцы/ячейки не пишутся; таблица растёт). Правка ячейки, очистка и
структура (добавить/удалить строку/столбец) — пересчёт текущих данных, новая
`base`, `base_anchor` = последний id сообщения (как `purgeTableContributions`).

## 11. API

Все эндпоинты чата проверяют `_can_access_session`. Ответы — JSON.

- `GET /api/sessions/{sid}/horae/state?at=<mid>` →
  `{enabled, settings, state:{time:{date,time,display}, scene:{location,atmosphere,characters,desc,parent_desc},
  costumes, mood, items:[{id,name,icon,importance,holder,location,description,locked,mid}],
  affection:[{name,value,level}], npcs:[{id,name,…поля…,age_display,aliases,present,pinned,favorite,first_mid,last_mid}],
  agenda:[{date,text,source,mid}], relationships:[{from,to,type,note,user}],
  locations:[{name,desc,current,user,aliases,mid}], rpg}, timeline:[TL_ITEM],
  tables:[{id,name,scope,prompt,rows,cols,data,locked_rows,locked_cols,locked_cells}],
  rpg_config, ops:[{id,kind,at,label,created_at}],
  stats:{messages, ai_messages, with_meta, without_meta, side, injection_tokens, summaries, scan, summary_error},
  job}`.
  TL_ITEM: `{kind:"event", mid, i, level, text, date, time, rel, covered_by}` |
  `{kind:"summary", id, range, text, depth, active, auto, kind_detail, date_from, date_to, rel, events}`.
- `GET /api/messages/{mid}/horae` → `{message_id, role, meta, side, swipe, brief}`;
  `PUT` `{meta}` — заменить мету активного свайпа;
  `POST …/analyze` — ИИ-анализ сейчас; `POST …/side` `{side}`.
- `POST /api/sessions/{sid}/horae/ops` `{kind, …}` → `{ok, op}`;
  `DELETE /api/sessions/{sid}/horae/ops/{op_id}` — откат правки.
- События: `POST /api/sessions/{sid}/horae/events` `{mid, index?, level, text}`;
  `PATCH` `{mid, i, level?, text?}`; `POST …/events/delete` `{refs:[{mid,i}]}`.
- Свёртки: `POST …/horae/compress` `{refs:[{mid,i}], summary_ids:[], mode:"events"|"fulltext"}`;
  `POST …/horae/summaries` `{from_mid, to_mid, text}`; `PATCH …/summaries/{id}` `{text?, active?}`;
  `DELETE …/summaries/{id}`; `POST …/summaries/run`.
- Скан: `POST …/horae/scan` `{batch_tokens, include:{npc,affection,scene,relationships}}` (202);
  `GET …/horae/job`; `POST …/horae/job/cancel`; `POST …/horae/scan/undo`.
- Таблицы: `POST …/horae/tables` `{name, rows, cols, prompt, scope:"local"|"global"|"character"}`;
  `PATCH …/horae/tables/{tid}` `{name?, prompt?, cell?:{r,c,value}, structure?:{op,index},
  lock?:{type,r,c,locked}, clear?}`; `DELETE …/horae/tables/{tid}`; `POST …/horae/tables/import` `{table}`.
- Настройки: `GET/PUT /api/horae/settings` (`{defaults, global, effective}`);
  `GET/PUT /api/sessions/{sid}/horae/settings` (`{overrides, character, effective}`);
  `GET/PUT /api/characters/{cid}/horae_profile`.
- Промпты: `GET /api/horae/prompts` (`{defaults, presets}`); `POST /api/horae/prompts/presets`
  `{name, prompts}`; `DELETE /api/horae/prompts/presets/{id}`.
- RPG: `GET/PUT /api/sessions/{sid}/horae/rpg_config`; `GET/PUT /api/horae/equipment_templates`.
- `GET /api/sessions/{sid}/horae/export`, `POST …/horae/import` `{data, mode:"by_id"|"initial"}`,
  `DELETE /api/sessions/{sid}/horae` (стереть данные Horae чата).
- `POST /api/sessions/{sid}/horae/carryover` `{keep, vectors}` → `{session_id}`.
- `POST /api/sessions/{sid}/horae/npc_enrich` `{name, aliases}` → `{fields, hits}`.
- `GET /api/sessions/{sid}/horae/recall?q=` (отладка); `POST …/horae/reindex`.
- В `GET /api/sessions/{sid}/messages` у каждого сообщения — `horae_brief`
  (строка или `null`) и `horae_side`.

## 12. Жизненный цикл

- **Ответ сохранён** (новый/свайп/продолжение/группа/Telegram): `parse_reply` →
  чистый текст в `content`/`swipes[i]`, мета в `horae.metas[i]`
  («Продолжить» сливает мету продолжения с прежней). Затем в фоне
  `horae_engine.after_turn`: нет тегов и `auto_analyze` — ИИ-анализ; документ
  для поиска; проверка авто-свёртки.
- **Правка текста**: теги в новом тексте — разобрать в мету свайпа.
- **Переключение свайпа**: мета следует за свайпом; документ переиндексируется.
- **Удаление сообщения**: удалить документ поиска; свёртки с `range[1] >= id`
  снимаются (дети восстанавливаются, если целиком старше); якоря правок и
  `base_anchor` таблиц прижимаются к последнему оставшемуся id.
- **Удаление чата**: `HoraeChatState`, `HoraeMemoryDoc` чата.
- **Ветка (fork)**: метки копируются; состояние чата — с переназначением id
  (правки и свёртки старше развилки, таблицы).
- **Импорт SillyTavern**: `horae_meta` каждого сообщения и встроенные теги → META;
  `chat[0].horae_meta` (свёртки, память сцен, отношения, таблицы, RPG, планы
  пользователя, надгробия) → HoraeChatState с переводом индексов в id.
- **Нативный экспорт/импорт**: `horae` сообщений и `horae_chat`.

## 13. Интерфейс

Вкладка ящика «Хроника» (широкий ящик): подвкладки «Состояние», «Хронология»,
«Персонажи», «Предметы», «Сцены», «Таблицы», «RPG», «Настройки», «Промпты».
Под каждым ответом ИИ — строка Horae (`horae_brief`), по клику — редактор меты
сообщения (время, место, атмосфера, присутствующие, костюмы, настроение,
предметы, события, расположение, планы, NPC, отношения), «ИИ-анализ»,
«Побочная сцена», исходные теги. Во время стрима теги не показываются — вместо
них «🕰 Horae записывает…».
