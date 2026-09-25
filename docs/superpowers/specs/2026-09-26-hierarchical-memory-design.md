# Иерархическая пакетная память (HierarchicalMemoryManager) — дизайн

Дата: 2026-09-26 · Версия продукта: 2.5.0 · Ветка: `feat/hierarchical-memory`
Статус: утверждён (подход A — развитие Horae; окно 50; снимок 12 000 токенов;
сброс = снимок + буфер + факты).

## 1. Задача

ТЗ пользователя: трёхуровневый контекст для чатов на 200k–1M+ токенов.

| Уровень | Содержимое | Где в TaleEngine |
|---|---|---|
| Tier 1 | системный промпт, персонаж, правила, якоря | `assemble_context`: системный промпт + статичный хвост |
| Tier 2 | мастер-снимок — структурированный экстракт всей старой истории | запись Horae чата `category="summary"` |
| Tier 3 | активное окно — последние K сырых сообщений (мультимодальные) | `horae_recall.window_start` |

Что требуется и чего нет сейчас (разбор upstream Horae — в §12):

1. **Ретроспективное пакетное сжатие** всей накопленной истории: `State_N =
   merge(State_{N-1}, Block_N)` блоками по `batch_size`, с паузой `delay_ms`,
   повтором при ошибках API и прогресс-баром `[Обработано 140/800 сообщений |
   Сжато до 4 200 токенов]`. Сейчас фоновая сводка делает ≤ 6 кусков за ход и
   только после нового ответа ассистента; пауз, повторов и прогресса нет.
2. **Строгая схема** снимка из четырёх разделов вместо свободного пересказа;
   снимок не должен терять списки и атрибуты при повторных сжатиях. Сейчас
   промпт другой (5 разделов), а `_adopt_summary` режет снимок слайсом
   `[:6000]` символов — хвостовые разделы теряются молча.
3. **Ручное управление жизненным циклом**: Rebuild Full Memory, Purge Memory
   Cache, Export Master Snapshot (.md), Token Budget Monitor.
4. **Строгое скользящее окно (FIFO)**: сообщения `0..M-K` идут в модель только
   через снимок, `M-K..M` — дословно.

## 2. Решения

- **Подход A.** Не параллельная подсистема, а развитие Horae: один движок и один
  промпт и для ежеходного инкремента, и для полной пересборки. Снимок живёт в
  той же записи `summary`, поэтому окно (`trusted_pointer`), инспектор, ручное
  редактирование во вкладке «Память», приватность (`_can_access_horae`) и
  удаление чата работают без изменений.
- **Окно по умолчанию 50** (`horae_recall.DEFAULT_WINDOW = 50`, клиент тоже).
  Настройка в UI остаётся (0–1000, кнопки 20/50/80/150/«вся»). Одноразовая
  миграция: сохранённое старое значение по умолчанию 20 становится 50 (флаг
  `memory_defaults_v: 2` в настройках `ui`, повторно не срабатывает).
- **Бюджет снимка 12 000 токенов** (`memory_snapshot_tokens`, настраивается).
- **Purge** стирает снимок, буфер пересборки и атомарные факты чата. Сообщения
  не трогает.
- **Схема снимка — Markdown** с фиксированными заголовками (не JSON): LLM
  надёжно пишет Markdown, его читает человек во вкладке «Память» и в экспорте,
  он идёт в контекст как есть.
- **Физическое место снимка в промпте не меняется**: блок стоит в хвосте
  (после окна, перед репликой пользователя). Логический порядок уровней — как в
  ТЗ, но если поставить снимок между системным промптом и окном, каждое его
  обновление сбрасывало бы кэш провайдера для всей истории.
- **Формат указателя не меняется** (`meta.v = 2`, `SUMMARY_FORMAT = 2`): новый
  сводчик видит всё, что учитывает, поэтому указатель v2 остаётся правдой. Новая
  схема помечается отдельно: `meta.schema = "hms-1"`. Сводки v2 старой схемы
  конвертируются в новую на ближайшем проходе бесплатно (слияние принимает любое
  предыдущее состояние); полная чистая конвертация — кнопкой «Пересобрать».

## 3. Модули

```
backend/hierarchical_memory.py   НОВЫЙ. Чистое ядро: только stdlib (без БД,
                                 FastAPI, SQLAlchemy, litellm). LLM — колбэк.
backend/memory_service.py        НОВЫЙ. Адаптер к БД: источник пакетов, запись
                                 снимка/буфера, задания, статус, сброс, экспорт.
backend/main.py                  _summary_pass/_maybe_update_summary — тонкие
                                 обёртки над сервисом; новые эндпоинты.
backend/horae_recall.py          DEFAULT_WINDOW = 50; window_start переезжает в
                                 ядро и реэкспортируется отсюда.
backend/horae_memory.py          рендер снимка новой схемы; отчёт report["tiers"].
backend/llm_gateway.py           контекстная переменная переопределения
                                 сэмплинга (max_tokens, temperature).
backend/config.py                настройки MEMORY_* и MODEL_CONTEXT_LIMIT.
frontend/app.js, styles.css      блок «Мастер-память этого чата», монитор токенов,
                                 фикс сброса бюджета 1M.
```

Зависимости: `hierarchical_memory` ← `memory_service` ← `main`. Ядро ничего не
импортирует из проекта. Сервис не импортирует `main` (цикл) — зависимости,
которые тесты подменяют (`backend.main.complete`, `backend.main.get_connection`),
main передаёт сервису поздно связанными колбэками (см. §6.1).

## 4. Ядро `backend/hierarchical_memory.py`

### 4.1 Константы и данные

```python
SNAPSHOT_SCHEMA = "hms-1"
SEC_CHRONICLE  = "ХРОНИКА И СОБЫТИЙНЫЙ КАРКАС"
SEC_CHARACTERS = "АКТИВНЫЕ ПЕРСОНАЖИ И ИХ СТАТУСЫ"
SEC_REGISTRY   = "ФАКТОЛОГИЧЕСКИЙ РЕЕСТР И ЛОР"
SEC_LISTS      = "СПИСКИ И МЕДИА-АНКОРЫ"
SECTIONS = (SEC_CHRONICLE, SEC_CHARACTERS, SEC_REGISTRY, SEC_LISTS)
GUARDED_SECTIONS = (SEC_CHARACTERS, SEC_REGISTRY, SEC_LISTS)
EMPTY_STATE = "(пока пусто)"
ENVELOPE_OPEN, ENVELOPE_CLOSE = "<master_state>", "</master_state>"

@dataclass(frozen=True)
class MemoryMessage:
    id: int
    role: str                      # user | assistant | system
    speaker: str                   # уже разрешённое имя
    text: str                      # активный свайп (Message.content)
    created_at: datetime | None = None   # уже в часовом поясе чата (или naive UTC)
    attachments: tuple[dict, ...] = ()   # лёгкая мета {type, name, mime}

@dataclass(frozen=True)
class Batch:
    messages: tuple[MemoryMessage, ...]
    transcript: str
    first_id: int
    last_id: int

@dataclass
class MemoryConfig:
    batch_size: int = 20
    batch_max_chars: int = 80_000
    delay_ms: int = 1500
    max_retries: int = 4
    backoff_base_s: float = 2.0
    backoff_max_s: float = 60.0
    snapshot_tokens: int = 12_000
    validation_retries: int = 2
    shrink_ratio: float = 0.5      # новый снимок < 50% старого — брак
    shrink_min_tokens: int = 800   # проверку «сдувания» включаем от этого размера
    keep_recent_chronicle: int = 12  # сколько последних записей хроники не сжимать в арки

@dataclass
class ScanProgress:
    processed: int; total: int; batches: int; state_tokens: int
    phase: str                  # "merge" | "compact" | "wait" | "retry" | "done"
    retry_in_s: float | None = None
    last_range: tuple[int, int] | None = None
    def line(self) -> str       # "[Обработано 140/800 сообщений | Сжато до 4 200 токенов]"
                                # (разделитель тысяч — неразрывный пробел)

@dataclass
class ScanResult:
    state: str; processed: int; batches: int
    status: str                 # "done" | "cancelled" | "limit"
    state_tokens: int
    warnings: list[str]
```

Исключения:

```python
class MemoryLLMError(Exception):
    kind: str        # rate_limit | timeout | server | network | auth | bad_request
                     # | blocked | length | empty | unknown
    retryable: bool
    message: str     # по-русски, для UI
class SnapshotValidationError(Exception): problems: list[str]
class SourceConflictError(Exception)   # кусок трижды подряд менялся под моделью
```

### 4.2 Нормализация сообщения

`normalize_message(msg) -> str`:

```
[#1234 · 2026-09-20 14:03 · Эльвира] текст реплики
📎 изображение «map.png»; аудио «voice.ogg»
```

- Время `YYYY-MM-DD HH:MM`; нет `created_at` — поле опускается.
- Из текста вырезаются: `data:`-URI; сплошные base64-последовательности ≥ 200
  символов (`[A-Za-z0-9+/=\s]{200,}` без пробелов внутри считаем бинарём);
  `<think>…</think>`, `<thinking>…</thinking>`, `<style>…</style>`,
  `<script>…</script>`, HTML-комментарии; остальные HTML-теги снимаются с
  сохранением внутреннего текста (`<br>`, `</p>`, `</div>` → перевод строки);
  zero-width символы; 3+ пустые строки схлопываются до одной.
- Markdown и код не трогаются.
- Вложения: подписи по типу (`image`→«изображение», `audio`→«аудио»,
  `video`→«видео», `document`→«документ», иначе «вложение») и имя файла. Что
  внутри медиа, модель узнаёт из текста реплик (ответ ассистента, анализирующего
  файл, идёт в тот же пакет).
- Сообщение без текста и без вложений пропускается; только с вложениями —
  попадает (строка с 📎), чтобы следующий анализ был понятен.
- Строка длиннее `batch_max_chars` сокращается до начала и конца по половине с
  маркером `\n[…середина длинного сообщения пропущена…]\n` (текст маркера
  сохраняется — на него опираются тесты).

### 4.3 Пакеты

`plan_batch(messages) -> Batch | None` берёт подряд до `batch_size` сообщений,
пока сумма длин строк ≤ `batch_max_chars`; хотя бы одно сообщение всегда.
Пакеты не пересекаются. `plan_batches(messages) -> list[Batch]` — все подряд.
`Batch.transcript` — нормализованные строки через `\n\n`.

### 4.4 Вызов модели: пауза, повтор, классификация

`LLMCall = Callable[[list[dict]], Awaitable[str]]` передаётся в конструктор.

`async call(messages) -> str`:

1. **Пауза-ограничитель**: если с конца предыдущего вызова этого менеджера
   прошло меньше `delay_ms`, спим остаток (`sleep`, `clock` — внедряемые).
   Так пауза стоит между ЛЮБЫМИ запросами прогона: слияние, факты, сжатие.
2. Вызов. Ошибка → `classify_error(exc) -> MemoryLLMError`:

   | Признак | kind | повтор |
   |---|---|---|
   | `status_code == 429`, `RateLimitError` | rate_limit | да |
   | 408, `Timeout`, `asyncio.TimeoutError`, `TimeoutError` | timeout | да |
   | 5xx, `InternalServerError`, `ServiceUnavailableError`, `BadGatewayError` | server | да |
   | `APIConnectionError`, `ConnectionError`, `OSError` | network | да |
   | 401/403, `AuthenticationError`, `PermissionDeniedError` | auth | нет |
   | 400/404/422, `BadRequestError`, `NotFoundError`, `ContextWindowExceededError` | bad_request | нет |
   | `ContentPolicyViolationError`; `RuntimeError` с `finish_reason=SAFETY/PROHIBITED_CONTENT/BLOCKLIST/SPII/RECITATION/CONTENT_FILTER` | blocked | нет |
   | `RuntimeError` с `finish_reason=LENGTH` / `MAX_TOKENS` | length | нет (обрабатывает `merge_block`) |
   | прочий `RuntimeError` с «ПУСТОЙ ответ» | empty | да |
   | иное | unknown | да |

   Признаки берутся по имени класса и атрибутам (`status_code`,
   `response.status_code`, `response.headers["retry-after"]`) — ядро не
   импортирует litellm. `asyncio.CancelledError` не перехватывается никогда.
3. Повтор: ожидание `min(backoff_max_s, backoff_base_s * 2**attempt)`, но не
   меньше `Retry-After`, если он есть; до `max_retries` повторов (итого
   `max_retries + 1` попыток), затем `MemoryLLMError` наверх. Перед ожиданием —
   событие прогресса `phase="retry", retry_in_s=…`.

LiteLLM внутри тоже повторяет (`LLM_NUM_RETRIES`); наш повтор — внешний контур
с длинными паузами поверх него.

### 4.5 Слияние одного пакета: `merge_block(state, batch) -> str`

Запрос:

```
system: MASTER_STATE_PROMPT
user:   "[Текущая память]\n{state или EMPTY_STATE}\n\n[Новые события #{first}–#{last}]\n{transcript}"
```

(Метки `[Текущая память]` и `[Новые события` сохранены — на них опираются
тесты.)

Разбор и проверка ответа (`validate_snapshot(raw, prev) -> (text, problems)`):

1. Вырезать `<think>…</think>` и ограждения ```` ``` ````.
2. Обёртка: если есть `<master_state>` без `</master_state>` → проблема
   «ответ обрезан»; если обёртка есть — берём содержимое; если её нет вовсе,
   берём весь текст (мягкий режим).
3. Пусто → «пустой ответ».
4. Разобрать разделы (`parse_sections`): заголовок — строка вида
   `## [ИМЯ]`, `### ИМЯ`, `[ИМЯ]` (1–4 решётки, скобки необязательны, регистр
   и ё/е не важны). Отсутствующий раздел → «нет раздела ИМЯ».
5. «Сдувание»: если предыдущий снимок структурирован (`is_structured(prev)`) и
   весит ≥ `shrink_min_tokens`, а новый < `shrink_ratio` от него → «снимок
   потерял больше половины содержимого». Для неструктурированного `prev`
   (сводка старой схемы или пусто) проверка не делается — это конвертация.
6. Длиннее 200 000 символов → «снимок неправдоподобно длинный».

Проблемы → повтор с корректирующим ходом (до `validation_retries` раз):

```
assistant: <сырой ответ>
user: "Ответ отклонён: <проблемы через «; »>. Верни ПОЛНЫЙ снимок заново строго по
       схеме, в обёртке <master_state>…</master_state>, без пояснений."
```

После исчерпания — `SnapshotValidationError(problems)`: в память ничего не
пишется, указатель не двигается.

Ошибка `kind="length"` (модель упёрлась в лимит вывода): один раз сжать `state`
(`compact`, §4.7) и повторить слияние; повторная `length` → ошибка наверх.

После валидного ответа:

- `guard_entries(prev, new)` (§4.6), если `prev` структурирован;
- если `estimate_tokens(new) > snapshot_tokens` → `compact(new)`; неудача
  сжатия → оставить несжатый снимок и добавить предупреждение
  «снимок превышает бюджет: X из Y токенов».

Результат нормализуется `render_snapshot(parse_sections(text))`: заголовки
`## [ИМЯ]` в каноническом порядке, пустой раздел → строка `—`, без обёртки.

### 4.6 Страж записей (детерминированная гарантия фактов)

`guard_entries(prev, new) -> (text, restored: list[str])` для разделов
`GUARDED_SECTIONS`:

- Запись — строка маркированного списка верхнего уровня (`- `, `* `, `• `,
  `N. `, отступ ≤ 1 пробела) вместе с продолжением (строки с отступом, не
  маркер и не заголовок). В `SEC_LISTS` учитываются подзаголовки `### Название`:
  запись принадлежит своему подсписку.
- Ключ записи — текст до первого из `" — "`, `" – "`, `" - "`, `":"`, `"|"`,
  `"("`; нижний регистр, ё→е, без пунктуации, пробелы схлопнуты, ≤ 80 символов.
  Пустой ключ — запись не охраняется.
- Запись считается сохранённой, если в том же разделе нового снимка есть ключ
  с совпадением точно или по Жаккару токенов ≥ 0.6.
- Пропавшая запись дописывается в конец своего раздела (для списков — в свой
  подсписок; нет подзаголовка — создать) исходным текстом; ключ попадает в
  `restored`, а в прогресс — предупреждение «модель потеряла N записей —
  возвращены из предыдущего снимка».

Хроника не охраняется: её сжимает §4.7.

### 4.7 Иерархическое сжатие: `compact(state) -> str`

Запрос `COMPACT_PROMPT` (system) + снимок (user): сократить ТОЛЬКО хронику —
объединить старые записи в арки
`- [#a–#b] Арка «название»: суть, ключевые решения, последствия`,
последние `keep_recent_chronicle` записей оставить без изменений, разделы 2–4
перенести дословно. Ответ проходит ту же валидацию (без проверки «сдувания»),
затем `guard_entries(state, compacted)`. Если хроника не стала короче —
предупреждение, снимок остаётся прежним.

### 4.8 Цикл: `scan_and_compress_history`

```python
async def scan_and_compress_history(
    self, source: Sequence[MemoryMessage] | BatchSource, state: str = "", *,
    on_progress: Callable[[ScanProgress], None] | None = None,
    cancel: asyncio.Event | None = None,
    max_batches: int | None = None,
) -> ScanResult
```

```python
class BatchSource(Protocol):
    async def pending(self) -> int                          # сколько сообщений ждут
    async def current_state(self, state: str) -> str        # свежий снимок перед пакетом
    async def next_messages(self, limit: int) -> list[MemoryMessage]  # [] — нечего
    async def commit(self, batch: Batch, new_state: str) -> bool      # False — кусок изменился
```

Список сообщений оборачивается во внутренний `ListSource` (`current_state`
возвращает переданное, `commit` всегда `True`).

Алгоритм:

```
processed = batches = conflicts = 0
emit(phase="merge", total=processed + pending)
loop:
    cancel установлен            -> status="cancelled"; break
    max_batches достигнут         -> status="limit"; break
    state = await source.current_state(state)
    msgs  = await source.next_messages(batch_size)
    batch = plan_batch(msgs); нет -> status="done"; break
    new   = await merge_block(state, batch)          # паузы/повторы внутри
    if not await source.commit(batch, new):
        conflicts += 1; >3 подряд -> SourceConflictError; continue
    conflicts = 0; state = new; processed += len(batch); batches += 1
    emit(phase="merge", processed, total=processed + pending, state_tokens, last_range)
emit(phase="done")
```

Исключения из `merge_block` (`MemoryLLMError`, `SnapshotValidationError`)
пробрасываются: вызывающий решает, что это значит (задание → статус `error`,
ежеходный проход → лог и повтор на следующем ходу). Уже закоммиченные пакеты
остаются.

### 4.9 Скользящее окно

`window_start(history_ids, covered, window, step=WINDOW_STEP)` переезжает в
ядро без изменений; `horae_recall.window_start` — реэкспорт (тесты подменяют
именно `backend.horae_recall.window_start`, а `horae_memory` вызывает его через
модуль `horae_recall`).

`class SlidingWindow` — удобная обёртка для встраивания вне TaleEngine:
`split(messages_ids, covered) -> (dropped_ids, raw_ids)`,
`pending_for_summary(ids, covered) -> ids` (что старше окна и ещё не учтено).

### 4.10 Прочие чистые помощники

- `parse_sections(text) -> dict[str, str]`, `render_snapshot(sections | **kw) -> str`,
  `is_structured(text) -> bool` (есть ≥ 3 из 4 заголовков).
- `budget_tiers(*, system, memory, window, current, budget, model_limit) -> dict`
  — суммы и доли (для монитора; §7).
- `render_export_markdown(*, title, character, snapshot, covered_upto,
  messages_total, tokens, budget, schema, exported_at, facts=None) -> str`.
- `default_estimate_tokens(text)` — эвристика (кириллица/CJK ≈ 2 символа на
  токен, прочее ≈ 4); сервис передаёт `horae_memory.estimate_tokens` (tiktoken).

## 5. Промпты

### 5.1 `MASTER_STATE_PROMPT` (строгий системный промпт компрессора)

Содержание (итоговый текст — в коде, на русском):

- Роль: «компрессор долговременной памяти ролевого чата; не пересказ, а
  МАСТЕР-СНИМОК по строгой схеме: СНИМОК_N = слияние(СНИМОК_{N-1}, НОВЫЙ БЛОК)».
- Вход: `[Текущая память]` — проверенные данные, только для чтения: не
  перефразировать, не сокращать, не «улучшать» то, чего новый блок не касается,
  — переносить дословно. `[Новые события #a–#b]` — сообщения по порядку,
  формат строки `[#id · время · автор] текст`, `📎` — вложения; что в медиа,
  известно только из текста реплик.
- Жёсткие правила:
  1. Только то, что прямо сказано в блоке или уже есть в снимке. Не
     додумывать чувства, мотивы, даты, числа, имена. Неясное — «(?)»; время не
     указано — «время не указано». Никогда не выдумывать конкретное время.
  2. Имена, названия, числа, цитаты, названия произведений/треков — символ в
     символ, без синонимов и переводов; цитаты — в «кавычках» с автором.
  3. Ни одна запись разделов 2–4 не исчезает. Изменилось — обнови на месте
     (новое значение; в скобках — прежнее и с какого сообщения). Утратило
     силу — «(неактуально с #id: причина)», но не удалять.
  4. Хроника дополняется в конец в хронологическом порядке; каждая запись
     начинается с диапазона `[#a–#b]`; старые записи и арки не переписывать.
  5. Противоречия: для текущих статусов новое важнее старого; для фактов
     прошлого — более конкретная запись, расхождение отметить.
  6. Конкретика вместо «воды»: кто кому что сказал/дал/пообещал и что решили.
     ❌ «договорились о встрече» ✅ «Эльвира пообещала Артуру встретиться у
     старого маяка в полночь 12 мая (#1234)».
  7. Бытовые реплики без последствий не записывать; необратимые решения,
     разоблачения, смерть, предательство, договоры, передача важных
     предметов — записывать всегда.
  8. Скрытые мотивы — только если прямо показаны в тексте, иначе «—».
  9. Пустой раздел — заголовок и строка «—».
- Схема ответа (строго, без вступлений и комментариев):

```
<master_state>
## [ХРОНИКА И СОБЫТИЙНЫЙ КАРКАС]
- [#a–#b] (время сюжета, если известно) кто → что → результат/решение
## [АКТИВНЫЕ ПЕРСОНАЖИ И ИХ СТАТУСЫ]
- Имя — здоровье: …; локация: …; психологический вектор: …; скрытые мотивы: …; при себе: …; отношения: …
## [ФАКТОЛОГИЧЕСКИЙ РЕЕСТР И ЛОР]
- Сущность — что это / правило / решение (#id)
## [СПИСКИ И МЕДИА-АНКОРЫ]
### Название списка
- элемент — значение/роль (#id)
</master_state>
```

- Самопроверка перед ответом: все четыре раздела на месте; ни одна запись
  разделов 2–4 из текущей памяти не пропала; списки не сокращены и не
  переупорядочены; нет выдуманных деталей; обёртка закрыта.

### 5.2 `COMPACT_PROMPT`

«Снимок превышает бюджет {budget} токенов. Сократи ТОЛЬКО раздел ХРОНИКА:
объедини старые записи в арки `- [#a–#b] Арка «название»: суть, ключевые
решения, последствия`; последние {keep} записей оставь без изменений. Разделы
2–4 перенеси ДОСЛОВНО, символ в символ. Ответ — полный снимок в обёртке
<master_state>.» Плейсхолдеры подставляет ядро.

### 5.3 Факты

`horae_recall.FACTS_PROMPT` и формат `[Новые события]\n…` не меняются; их
вызов идёт через тот же `manager.call` (пауза и повторы общие).

## 6. Сервис `backend/memory_service.py`

### 6.1 Зависимости от main

```python
@dataclass
class MemoryDeps:
    complete: Callable[..., Awaitable[str]]        # late-bound: lambda *a, **k: main.complete(*a, **k)
    get_connection: Callable[[AsyncSession], Awaitable[dict]]  # late-bound на main.get_connection
```

main создаёт `MemoryDeps` с функциями, которые обращаются к глобалам main в
момент вызова, поэтому `patch("backend.main.complete")` и
`patch("backend.main.get_connection")` в тестах действуют на новый код.

LLM-колбэк менеджера:

```python
async def call(messages):
    with llm_gateway.sampling_overrides(max_tokens=out_tokens, temperature=MEMORY_TEMPERATURE):
        return await deps.complete(messages, None, bg_conn, kind="summary")
```

- `bg_conn` — как сейчас: `summary_model` подставляется в `default_model`,
  `params=None` (фильтры безопасности выключены; тест
  `test_summary_fast_model_keeps_safety_off`).
- `out_tokens = max(DEFAULT_MAX_TOKENS, round(snapshot_tokens * 1.4) + 1024)`
  (12 000 → 17 824). `summary_model` должна поддерживать такой вывод.
- `llm_gateway.sampling_overrides(**kw)` — контекстный менеджер на
  `ContextVar`; `stream_completion` применяет его значения поверх
  `_merge_params`. Сигнатуры `complete/stream_completion` не меняются (подмены в
  тестах имеют фиксированную сигнатуру).

### 6.2 Хранение

Запись `HoraeEntry(session_id, category="summary")` — как сейчас
(`title="📜 Память чата (авто)"`, `keywords=["__auto__"]`, `always_on`,
`enabled`, `priority=50`).

`meta`:

```json
{
  "last_message_id": 812, "v": 2, "schema": "hms-1",
  "tokens": 4200, "updated_by": "incremental|rebuild|catchup",
  "warnings": ["…последние ≤ 5…"],
  "rebuild": {"content": "…", "last_message_id": 400, "manual": true,
              "started_at": "2026-09-26T10:00:00Z", "batch_size": 20}
}
```

- `content` больше не режется до 6000 символов (защита от мусора — лимит
  200 000 символов в валидации ядра).
- `_adopt_summary(entry, content, last_id, *, tokens=None, updated_by=...)` —
  как сейчас + `schema/tokens/updated_by`; удаляет `rebuild`.
- Буфер `meta.rebuild`: и пересборка старой сводки (legacy, как в 2.4.0), и
  ручная пересборка (`manual: true`). Пока буфер есть, ежеходные проходы пишут
  в него (это и есть возобновление после перезапуска сервера), в контекст идёт
  старый снимок. Подмена — когда буфер догнал бэклог.

### 6.3 Источник пакетов `DbBatchSource`

```python
DbBatchSource(session_id, *, mode: "incremental" | "rebuild" | "catchup",
              threshold: int, manager, deps, want_facts: bool, connection: dict,
              window: int, estimate_tokens)
```

Логика переносится из нынешнего `_summary_pass` без потери поведения:

- **Цель записи**: `rebuilding = legacy (meta.v != 2) or bool(meta.rebuild)`
  (для `mode="rebuild"` буфер создаётся заранее). При `rebuilding` указатель и
  предыдущее состояние берутся из буфера, иначе — из самой записи.
- **Прижатие указателя** `_clamp_summary_pointer` перед чтением (как сейчас).
- **Что сжимается**: `id > pointer` и `id < window_from` (id самого старого
  сообщения окна `memory_window`; чат короче окна → ничего). Окно читается
  одинаково с `horae_memory._long_memory`.
- **Порог**: `pending < threshold` → `[]`; если при этом идёт пересборка, в
  буфере есть текст и указатель > 0 — подменить снимок буфером без вызова
  модели (как сейчас). `incremental`: threshold = `summary_every` (≥ 2);
  задания: threshold = 1.
- **Сообщения**: `load_memory_messages(db, session, after_id, before_id)` —
  имена: user → имя персоны чата (`ChatSession.persona_id → Persona.name`), иначе
  «Пользователь»; assistant → `speaker_name`, иначе имя персонажа чата, иначе
  «Персонаж»; system → «Система». Время: `created_at` (naive UTC) →
  `ZoneInfo(session.timezone)`, если он задан и валиден.
- **commit(batch, new_state)**:
  1. Сверка куска с чатом (как сейчас: хэши `content` по id от `first_id` до
     `last_id`, только непустые) — не совпало → `False`.
  2. Запись: `rebuilding and not created and more` → буфер
     `meta.rebuild.content/last_message_id`; иначе `_adopt_summary`. `more` =
     после этого пакета остались сообщения до окна. Для `mode="rebuild"` —
     всегда буфер, подмена в конце задания.
  3. Коммит; затем факты: если `want_facts` и в пакете есть сообщения новее
     `max(HoraeFact.source_message_id)` — `manager.call([FACTS_PROMPT, "[Новые
     события]\n…"])` → `parse_facts` → `store_facts(…, newest_id=batch.last_id)`.
     Ошибка фактов логируется и не откатывает снимок.
  4. Повторное прижатие указателя.
- **current_state(state)** — перечитывает запись (ручная правка снимка во время
  задания не затирается).

### 6.4 Ежеходный инкремент

`main._maybe_update_summary(session_id)` — как сейчас по контракту: если чат
занят (идёт задание или другой прогон) — выход; иначе
`scan_and_compress_history(DbBatchSource(mode="incremental"),
max_batches=MEMORY_MAX_BATCHES_PER_TURN=6)`; исключения логируются; затем
`_backfill_fact_vectors`. `main._summary_pass(session_id) -> bool` остаётся
(тесты зовут его напрямую): один пакет, `True` — пакет записан и бэклог
остался.

### 6.5 Задания

```python
@dataclass
class MemoryJob:
    session_id: int; mode: str          # rebuild | catchup
    status: str                         # queued | running | done | error | cancelled
    processed: int = 0; total: int = 0; batches: int = 0; state_tokens: int = 0
    phase: str = "merge"; retry_in_s: float | None = None
    line: str = ""; error: str | None = None; warnings: list[str]
    started_at: str; finished_at: str | None = None
    batch_size: int; delay_ms: int
    cancel: asyncio.Event; task: asyncio.Task | None
```

- Реестр в памяти процесса: не больше одного задания на чат; блокировка чата —
  `asyncio.Lock` на `session_id`, общая с ежеходным инкрементом
  (`_summary_running` в main остаётся как совместимый псевдоним занятости).
  Ежеходный проход при занятом чате пропускается; задание ждёт освобождения
  (`queued`).
- `start_job(session_id, mode, batch_size=None, delay_ms=None, resume=False)`:
  - `rebuild` без `resume`: создать буфер `meta.rebuild = {content:"",
    last_message_id:0, manual:true, started_at, batch_size}` (старый снимок
    продолжает работать).
  - `rebuild` с `resume`: продолжить существующий буфер.
  - `catchup`: цель — живой снимок (если буфера нет).
  - Цикл `scan_and_compress_history(DbBatchSource(threshold=1))`; по
    завершении `rebuild` — подмена снимка буфером (`_adopt_summary`,
    `updated_by="rebuild"`), даже если хвост меньше `summary_every`.
  - Ошибка → `status="error"`, `error` — русский текст (`MemoryLLMError.message`,
    проблемы валидации). Буфер и указатель остаются — можно продолжить.
  - Отмена → `cancelled`, готовые пакеты сохранены (буфер для rebuild).
- `cancel_job(session_id)`.
- Завершённое задание остаётся в реестре до следующего старта (статус видит UI).
- Выключатель «Авто-сводка сюжета» на задания не влияет (это ручная команда);
  на ежеходный инкремент — как сейчас.

### 6.6 Статус, сброс, экспорт

`async status(db, session_id) -> dict`:

```json
{
  "job": {"mode": "rebuild", "status": "running", "processed": 140, "total": 800,
          "batches": 7, "state_tokens": 4200, "phase": "merge", "retry_in_s": null,
          "line": "[Обработано 140/800 сообщений | Сжато до 4 200 токенов]",
          "error": null, "warnings": [], "started_at": "…", "finished_at": null},
  "snapshot": {"exists": true, "tokens": 4200, "budget": 12000, "covered_upto": 812,
               "schema": "hms-1", "structured": true, "updated_at": "…",
               "over_budget": false, "warnings": []},
  "staging": {"last_message_id": 400, "tokens": 3000, "manual": true,
              "started_at": "…"},
  "backlog": {"pending": 37, "window": 50, "messages_total": 900},
  "facts": {"count": 120},
  "settings": {"batch_size": 20, "delay_ms": 1500, "snapshot_tokens": 12000}
}
```

`job`/`staging` — `null`, если нет. `pending` — сообщения старше окна, ещё не
учтённые целью (буфером при пересборке).

`async purge(db, session_id) -> dict`: отменить задание и дождаться его конца;
удалить записи `summary` чата и все `HoraeFact` чата; вернуть
`{"snapshot_deleted": bool, "facts_deleted": n}`. Сообщения не трогаются.
После сброса указатель = 0 → окно ничего не выбрасывает, пока память не
соберётся заново.

`async export_markdown(db, session_id, include_facts=False) -> (filename, text)`:

```markdown
# Мастер-снимок памяти — «Название чата»

- Персонаж: Эльвира · Сообщений в чате: 900 · Учтено до: #812
- Размер: 4 200 из 12 000 токенов · Схема: hms-1 · Выгружено: 2026-09-26 12:00 UTC

---

## [ХРОНИКА И СОБЫТИЙНЫЙ КАРКАС]
…

---

## Приложение: атомарные факты (120)      ← только при include_facts
- …
```

Имя файла: `memory-<очищенное название>-<id>.md`. Нет снимка → 404.

## 7. API

| Метод | Путь | Назначение |
|---|---|---|
| GET | `/api/sessions/{id}/memory` | статус (§6.6) |
| POST | `/api/sessions/{id}/memory/rebuild` | `{mode: "rebuild" \| "catchup", resume?: bool, batch_size?: 1–200, delay_ms?: 0–60000}` → 202 `{job}`; 409, если задание уже идёт |
| POST | `/api/sessions/{id}/memory/cancel` | остановить между пакетами → `{ok, job}` |
| DELETE | `/api/sessions/{id}/memory` | Purge (§6.6) |
| GET | `/api/sessions/{id}/memory/export?facts=1` | `text/markdown; charset=utf-8`, `Content-Disposition: attachment` |

Доступ — `_can_access_session` (как у инспектора); нет доступа → 403, нет чата
→ 404.

**Token Budget Monitor**: `GET /api/sessions/{id}/context` получает
`report["tiers"]`:

```json
{"system": 5200, "memory": 4300, "window": 61000, "current": 0,
 "total": 70500, "budget": 200000, "model_limit": 1000000,
 "window_messages": 52, "dropped_messages": 848, "trimmed_messages": 0}
```

- `system` = системный промпт + база знаний (текст) + аватары + статичные
  элементы хвоста (манифест, внешность, веб, время, Author's Note, якорь,
  post-history, глобальные инструкции, фокус).
- `memory` = блок снимка + блок вспомненных фактов.
- `window` = история после окна и обрезки; `current` = текущее сообщение
  (текст + вложения по `estimate_content_tokens`).
- Для этого элементы `tail` в `assemble_context` получают ключ (`snapshot`,
  `recalled`, `manifest`, `appearance`, `web`, `time`, `author_note`, `anchor`,
  `post_history`, `global`, `focus`); `report["tail"][i]["key"]` появляется в
  отчёте (UI игнорирует неизвестные поля).
- `model_limit` — `settings.MODEL_CONTEXT_LIMIT` (1 000 000).

Рендер снимка в хвосте: если запись структурирована (`is_structured`), тело
блока — сам снимок без префикса `- 📜 …:`; заголовок блока сохраняется
(`[ХРОНИКА И СОСТОЯНИЕ ЧАТА] Что было в истории — помни это.` + строка
«Мастер-снимок старой части чата; последние сообщения выше идут дословно.»).
Неструктурированные сводки — как сейчас.

## 8. Настройки

`backend/config.py` (+ `.env.example`):

| Имя | По умолчанию |
|---|---|
| `MEMORY_BATCH_SIZE` | 20 |
| `MEMORY_BATCH_CHARS` | 80 000 |
| `MEMORY_DELAY_MS` | 1500 |
| `MEMORY_SNAPSHOT_TOKENS` | 12 000 |
| `MEMORY_MAX_RETRIES` | 4 |
| `MEMORY_TEMPERATURE` | 0.2 |
| `MEMORY_MAX_BATCHES_PER_TURN` | 6 |
| `MODEL_CONTEXT_LIMIT` | 1 000 000 |

Настройки `ui` (глобальные, как остальные ключи памяти): `memory_window`
(по умолчанию 50), `memory_batch`, `memory_delay_ms`, `memory_snapshot_tokens`,
`memory_defaults_v`, `ctx_budget_v`. Все новые ключи добавляются в
`saveUiPrefs` (PUT заменяет значение целиком), `loadUiPrefs` и `data()`.

`main._SUMMARY_CHUNK_CHARS` остаётся псевдонимом `MEMORY_BATCH_CHARS`,
`_SUMMARY_MAX_CHUNKS` — `MEMORY_MAX_BATCHES_PER_TURN` (на них ссылаются тесты).

`tests/conftest.py`: `os.environ.setdefault("MEMORY_DELAY_MS", "0")` до импорта
backend — существующие тесты не ждут паузы; тесты паузы задают её явно и
подменяют `sleep`/`clock`.

## 9. Интерфейс (вкладка «Память»)

Новый блок «Мастер-память этого чата» (виден, когда открыт чат), над общими
настройками памяти:

- **Статус**: «Снимок 4 200 / 12 000 ток. · учтено до #812 · ждут сжатия 37».
  Нет снимка — «Снимка нет: окно пропускает историю целиком». Схема старая —
  пометка «старая схема, пересоберите». Буфер пересборки есть и задания нет —
  «Пересборка прервана на #400 — [Продолжить]».
- **Прогресс** (задание идёт): полоса `upload-track/upload-fill`, строка
  `job.line`, под ней «Пакет 7 · пауза/повтор через N с», кнопка «Остановить».
  Прогресс не только цветом: число и текст рядом с полосой.
- **Кнопки**: «Пересобрать с нуля» (подтверждение с оценкой числа пакетов =
  ⌈сообщений / размер пакета⌉ и напоминанием о платных запросах), «Догнать»
  (только если `pending > 0`), «Экспорт .md», «Сбросить память» (`btn-danger`,
  подтверждение: «стирает снимок, буфер и факты; сообщения останутся»).
- **Параметры**: «Размер пакета» (1–200), «Пауза между запросами, мс» (0–60000),
  «Бюджет снимка, токенов» (1000–200000). Существующее поле «Активное окно»
  остаётся, пресеты `[20, 50, 80, 150, 0]`.
- **Монитор токенов**: трёхсегментная полоса `ins-bar` (Tier 1 — `seg-guides`,
  Tier 2 — `seg-horae`, Tier 3 — `seg-history`) + подписи с числами; строки
  «из бюджета хода 200 000 (35%)» и «из лимита модели 1 000 000 (7%)». Данные —
  `ctxStats.tiers` (тот же `loadCtxStats`). Та же разбивка — в инспекторе хода.
- **Опрос**: `GET /sessions/{id}/memory` при открытии вкладки/чата; пока
  задание `queued/running` — каждые 1,5 с (таймер в `_`-поле, снимается при
  смене чата и по завершении). Завершение → тост («Память пересобрана: 800
  сообщений → 11 900 токенов» / ошибка) и `loadCtxStats`, `loadHorae`.
- **Фикс**: миграция `context_tokens` 1 000 000 → 200 000 выполняется один раз
  (флаг `ctx_budget_v: 2`); выбранный вручную 1M больше не сбрасывается.
- Стиль — только существующие токены и классы DESIGN.md: без новых цветов,
  пилюля — только у полос состояния, кнопки не пилюли, текст 12/14 px,
  `prefers-reduced-motion` уважается, мобильная ширина без горизонтальной
  прокрутки.

## 10. Совместимость и миграция

- Сводки v2 старой схемы: указателю по-прежнему верят; ближайший проход сливает
  их в новую схему (проверка «сдувания» для неструктурированного `prev`
  отключена).
- Сводки до 2.4.0 (без `v`): пересборка в буфере — как сейчас.
- Существующие тесты: поведение, которое они проверяют (указатель, пересборка,
  сверка куска, прижатие, факты, модель сводки, фильтры безопасности), не
  меняется. Подмены `complete` в них, возвращающие текст без разделов,
  обновляются на валидный снимок (`hierarchical_memory.render_snapshot(...)`);
  проверки вида `content.startswith("X")` — на `"X" in content`.
- Telegram-чаты сводку по-прежнему не запускают; групповые — запускают, окна у
  них нет (известные ограничения, не входят в задачу).

## 11. Тестирование

`tests/test_hierarchical_memory.py` (чистое ядро, без БД):

- нормализация: base64/`data:`/HTML/`<think>` вырезаны; автор, `#id`, время и
  подписи вложений на месте; длинное сообщение — начало и конец с маркером;
- пакеты: не больше `batch_size`, не больше `batch_max_chars`, хотя бы одно
  сообщение, без пересечений;
- свёртка: запрос пакета N содержит `State_{N-1}` (ответ пакета N-1);
- пауза между всеми вызовами (шпион `sleep` + фиктивные часы);
- 429 → повтор с бэкоффом 2/4/8 с и успех; `Retry-After` учитывается;
  401 → сразу `MemoryLLMError(kind="auth")`; `CancelledError` не глотается;
- обрезанная обёртка / нет раздела → корректирующий повтор → успех; брак после
  всех попыток → `SnapshotValidationError`;
- «сдувание» ловится для структурированного `prev` и не ловится для легаси;
- страж возвращает потерянную запись списка/реестра/персонажа, не дублирует
  переименованную по регистру/ё;
- снимок сверх бюджета → вызов `COMPACT_PROMPT`; `length` → сжатие и повтор;
- отмена между пакетами → `cancelled`, готовое сохранено; `max_batches` → `limit`;
- формат `ScanProgress.line()`;
- `window_start`/`SlidingWindow`, `budget_tiers`, `render_export_markdown`.

`tests/test_memory_service.py` (БД + API, LLM подменён через `backend.main.complete`):

- задание `rebuild`: старый снимок в контексте до конца, затем атомарная подмена;
  `meta.schema == "hms-1"`, `updated_by == "rebuild"`;
- прогресс в статусе растёт; строка `line`;
- возобновление: буфер из «упавшего» задания продолжается с указателя;
- `catchup` сворачивает хвост в живой снимок;
- 409 на второе задание; отмена;
- purge: снимок и факты удалены, сообщения на месте, окно ничего не режет;
- экспорт: `text/markdown`, заголовок, снимок, факты по `?facts=1`, 404 без снимка;
- ежеходный инкремент использует новый промпт и паузу;
- ошибка API в задании → `status="error"`, указатель не сдвинут;
- `report["tiers"]` в `/context`; окно по умолчанию 50;
- доступ: чужой чат в режиме аккаунтов → 403.

Прогон всего набора — по памяти проекта: пустые `ACCESS_CODE/ADMIN_PASSWORD`,
свой `TMPDIR`; сравнение с известными падениями окружения.

## 12. Разбор upstream Horae (SillyTavern) — что взято и что нет

- **AI Batch Scan** — независимые пакеты без предыдущего состояния; удаления
  предметов выбрасываются; вывод ограничен 4096 токенами (хвост большого пакета
  теряется молча); нет повторов/бэкоффа; пауза 2 с жёстко. **Не берём** модель
  независимых пакетов; **берём** чекпоинт после каждого пакета и прогресс.
- **Auto Summary / Resummary** — один пакет на срабатывание; промпты требуют
  «один абзац, без списков» (списки и цитаты гибнут); пересводка — дерево
  абзацев; инъекция без бюджета. **Берём** обёртку ответа с детектом обрезки,
  правила против выдумок, ❌/✅-конкретику, приоритеты «критичное всегда»,
  датировку хроники; **не берём** прозу вместо схемы и неограниченную инъекцию.
- **Вспомогательный API** — последовательная очередь, минимальный зазор,
  классификация ошибок — **берём** в виде паузы-ограничителя и повторов §4.4.

## 13. Вне задачи

- Пересчёт снимка при правке/удалении сообщения в уже учтённой части (решается
  кнопкой «Пересобрать»).
- Окно для групповых чатов, сводка в Telegram.
- Персистентная очередь заданий между перезапусками (возобновление — через
  буфер и кнопку «Продолжить» / ежеходные проходы).
