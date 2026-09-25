# Иерархическая пакетная память (2.5.0) — план реализации

> **Для агентов-исполнителей:** обязательный навык — superpowers:subagent-driven-development
> (или superpowers:executing-plans). Шаги отмечаются чекбоксами (`- [ ]`).

**Цель:** ретроспективное пакетное сжатие всей истории чата в строгий мастер-снимок
(`State_N = merge(State_{N-1}, Block_N)`) с паузами, повторами, прогрессом; команды
Rebuild / Catch-up / Purge / Export; монитор токенов по уровням; окно 50.

**Архитектура:** чистое ядро `backend/hierarchical_memory.py` (stdlib, LLM — колбэк)
+ адаптер к БД `backend/memory_service.py` + тонкие обёртки и эндпоинты в `main.py`
+ блок во вкладке «Память». Снимок остаётся в записи Horae `category="summary"`.

**Стек:** Python 3 · FastAPI · SQLAlchemy async + SQLite · pytest (asyncio_mode=auto)
· Vue 3 без сборки (`frontend/app.js`).

**Спека:** `docs/superpowers/specs/2026-09-26-hierarchical-memory-design.md` —
читать целиком перед любой задачей; номера разделов ниже (§N) — оттуда.

## Глобальные ограничения

- Комментарии и docstring — по-русски, идентификаторы — по-английски; плотность
  комментариев как в соседнем коде (объяснять «почему», с историей бага, если есть).
- Ядро `backend/hierarchical_memory.py` импортирует ТОЛЬКО stdlib.
- `memory_service.py` не импортирует `backend.main` (цикл). `complete` и
  `get_connection` приходят через `MemoryDeps` с поздним связыванием (§6.1).
- Сигнатуры `llm_gateway.complete` / `stream_completion` НЕ меняются.
- Служебные вызовы памяти: `params=None` (фильтры безопасности выключены), модель
  через `connection["default_model"] = summary_model`, `kind="summary"`.
- Метки запроса слияния: `[Текущая память]`, `[Новые события #a–#b]`; пустая память —
  `(пока пусто)`; маркер длинного сообщения — `[…середина длинного сообщения пропущена…]`.
- `meta.v` остаётся `2` (`SUMMARY_FORMAT`); новая схема — `meta.schema = "hms-1"`.
- `DEFAULT_WINDOW = 50`; `WINDOW_STEP = 4`.
- Снимок НЕ режется слайсом; бюджет — 12 000 токенов (`MEMORY_SNAPSHOT_TOKENS`).
- Тесты: `ACCESS_CODE= ADMIN_PASSWORD= TMPDIR=<свой каталог> TEMP=<он же> TMP=<он же>
  .venv/Scripts/python.exe -m pytest -q -p no:warnings`. Известные падения
  окружения на чистом HEAD: `test_api_smoke::test_debug_log_records_chat`,
  `test_api_smoke::test_empty_llm_response_is_explicit_error`,
  `test_document_service` (docx без LibreOffice), `test_llm_routing` ×2
  (safety/reasoning). Всё остальное обязано быть зелёным.
- Каждый параллельный прогон pytest — со своим `TMPDIR` (общая тестовая БД в temp).
- Коммит после каждой задачи; сообщения — conventional commits, в конце
  `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- UI: только токены и классы из `DESIGN.md`/`styles.css`, без новых цветов; кнопки
  не пилюли; текст 12/14 px; `prefers-reduced-motion`; мобильная ширина без
  горизонтальной прокрутки. Новые ключи `ui` — в `data()`, `loadUiPrefs`,
  `saveUiPrefs` (PUT заменяет значение целиком).

## Файлы

| Файл | Ответственность |
|---|---|
| `backend/hierarchical_memory.py` (новый) | ядро: типы, нормализация, пакеты, промпты, валидация, страж, сжатие, вызов с паузой/повтором, цикл свёртки, окно, бюджет, экспорт |
| `backend/memory_service.py` (новый) | источник пакетов из БД, запись снимка/буфера, ежеходный прогон, задания, статус, сброс, экспорт |
| `backend/main.py` | обёртки `_maybe_update_summary`/`_summary_pass`, `MemoryDeps`, эндпоинты `/memory*` |
| `backend/horae_recall.py` | `DEFAULT_WINDOW=50`, реэкспорт `window_start` из ядра |
| `backend/horae_memory.py` | ключи элементов хвоста, `report["tiers"]`, рендер структурированного снимка |
| `backend/llm_gateway.py` | `sampling_overrides()` на ContextVar |
| `backend/config.py`, `.env.example` | `MEMORY_*`, `MODEL_CONTEXT_LIMIT` |
| `frontend/app.js`, `frontend/styles.css` | блок «Мастер-память», монитор токенов, миграции флагами |
| `tests/test_hierarchical_memory.py` (новый) | контракт ядра |
| `tests/test_memory_service.py` (новый) | сервис + API |
| `tests/conftest.py` | `MEMORY_DELAY_MS=0` |
| `tests/test_memory.py`, `tests/test_horae_recall.py`, `tests/test_horae_triggers.py` | подмены `complete` → валидный снимок |
| `docs/HORAE.md`, `docs/API.md`, `CHANGELOG.md` | документация 2.5.0 |

Порядок: Задача 1 ∥ Задачи 2–3 ∥ Задача 7 → Задача 4 → Задача 5 → Задача 6 → Задача 8.

---

### Задача 1: Настройки, переопределение сэмплинга, тестовое окружение

**Files:**
- Modify: `backend/config.py` (после `AUTO_SUMMARY_EVERY`, ~стр. 105)
- Modify: `.env.example`
- Modify: `backend/llm_gateway.py` (`_merge_params` ~стр. 232, `stream_completion` ~стр. 340)
- Modify: `tests/conftest.py`
- Test: `tests/test_llm_routing.py` (добавить тест в конец)

**Interfaces:**
- Produces: `settings.MEMORY_BATCH_SIZE=20`, `MEMORY_BATCH_CHARS=80_000`,
  `MEMORY_DELAY_MS=1500`, `MEMORY_SNAPSHOT_TOKENS=12_000`, `MEMORY_MAX_RETRIES=4`,
  `MEMORY_TEMPERATURE=0.2`, `MEMORY_MAX_BATCHES_PER_TURN=6`,
  `MODEL_CONTEXT_LIMIT=1_000_000`;
  `llm_gateway.sampling_overrides(**kw)` — контекстный менеджер; внутри него
  `stream_completion` кладёт `kw` поверх `_merge_params(...)` (только ключи
  `max_tokens`, `temperature`, `top_p`).

- [ ] **Step 1: тест (падает)** — в `tests/test_llm_routing.py`:

```python
async def test_sampling_overrides_apply_only_inside_context():
    """Память просит низкую температуру и длинный вывод, не трогая сигнатуру complete."""
    from unittest.mock import patch
    from backend import llm_gateway

    seen = []

    async def fake_acompletion(**kw):
        seen.append({k: kw.get(k) for k in ("max_tokens", "temperature")})
        async def gen():
            yield _fake_chunk("ок")
        return gen()

    with patch("backend.llm_gateway.litellm.acompletion", new=fake_acompletion), \
            patch("backend.usage_stats._persist", new=lambda *a, **k: _noop()):
        with llm_gateway.sampling_overrides(max_tokens=17824, temperature=0.2):
            await llm_gateway.complete([{"role": "user", "content": "x"}], None, {})
        await llm_gateway.complete([{"role": "user", "content": "x"}], None, {})
    assert seen[0] == {"max_tokens": 17824, "temperature": 0.2}
    assert seen[1]["max_tokens"] != 17824 and seen[1]["temperature"] != 0.2
```

(`_fake_chunk` в файле уже есть; `_noop` — `async def _noop(): return None`,
добавить рядом с тестом, если его нет. Если сигнатура `_fake_chunk` иная —
использовать существующий в файле способ собрать чанк.)

- [ ] **Step 2:** прогон `pytest tests/test_llm_routing.py -k sampling_overrides -q` → FAIL
  (`AttributeError: sampling_overrides`).
- [ ] **Step 3: реализация.**

```python
# llm_gateway.py — рядом с _merge_params
from contextlib import contextmanager
from contextvars import ContextVar

# Переопределение сэмплинга для служебного вызова без протаскивания params:
# params=None держит фильтры безопасности выключенными (см. main._summary_pass),
# а памяти нужны своя температура и длинный вывод под снимок.
_SAMPLING_OVERRIDES: ContextVar[dict | None] = ContextVar("sampling_overrides", default=None)
_OVERRIDABLE = ("max_tokens", "temperature", "top_p")

@contextmanager
def sampling_overrides(**kw):
    token = _SAMPLING_OVERRIDES.set({k: v for k, v in kw.items() if k in _OVERRIDABLE and v is not None})
    try:
        yield
    finally:
        _SAMPLING_OVERRIDES.reset(token)
```

В `stream_completion` после `**_merge_params(params)`:
`call_kwargs.update(_SAMPLING_OVERRIDES.get() or {})`.

`config.py` — восемь полей с русскими комментариями (зачем каждое), значения из
«Interfaces». `.env.example` — те же имена с дефолтами и короткими комментариями.
`tests/conftest.py` — ДО импорта backend: `os.environ.setdefault("MEMORY_DELAY_MS", "0")`
с комментарием «тесты не ждут паузу между запросами памяти; тесты паузы задают её явно».

- [ ] **Step 4:** тест → PASS; весь `tests/test_llm_routing.py` без новых падений.
- [ ] **Step 5: коммит** `feat(memory): sampling overrides and MEMORY_* settings`.

---

### Задача 2: Ядро — типы, нормализация, пакеты, схема, валидация, страж, окно, бюджет, экспорт

**Files:**
- Create: `backend/hierarchical_memory.py`
- Test: `tests/test_hierarchical_memory.py` (новый)

**Interfaces (Produces, точные имена):**
`SNAPSHOT_SCHEMA`, `SEC_CHRONICLE`, `SEC_CHARACTERS`, `SEC_REGISTRY`, `SEC_LISTS`,
`SECTIONS`, `GUARDED_SECTIONS`, `EMPTY_STATE`, `ENVELOPE_OPEN`, `ENVELOPE_CLOSE`,
`LONG_MESSAGE_MARK = "\n[…середина длинного сообщения пропущена…]\n"`,
`DEFAULT_WINDOW = 50`, `WINDOW_STEP = 4`, `MAX_SNAPSHOT_CHARS = 200_000`;
`MemoryMessage`, `Batch`, `MemoryConfig`, `ScanProgress`, `ScanResult` (поля — §4.1);
`MemoryLLMError(kind, message, *, retryable, retry_after=None)`,
`SnapshotValidationError(problems)`, `SourceConflictError`;
`normalize_message(msg, max_chars=80_000) -> str` (`""` — сообщение пропускается);
`plan_batch(messages, batch_size, max_chars) -> Batch | None`;
`plan_batches(messages, batch_size, max_chars) -> list[Batch]`;
`parse_sections(text) -> dict[str, str]`; `render_snapshot(sections: dict) -> str`;
`is_structured(text) -> bool`; `extract_snapshot(raw) -> tuple[str, bool]`;
`validate_snapshot(raw, prev, *, config=None, estimate_tokens=None, check_shrink=True) -> tuple[str, list[str]]`;
`guard_entries(prev, new) -> tuple[str, list[str]]`;
`window_start(history_ids, covered, window, step=WINDOW_STEP) -> int` (перенос 1:1 из `horae_recall`);
`SlidingWindow(window, step=WINDOW_STEP)` с `.start(ids, covered) -> int`,
`.split(ids, covered) -> tuple[list, list]`, `.pending_for_summary(ids, covered) -> list`;
`budget_tiers(*, system, memory, window, current, budget, model_limit) -> dict`;
`render_export_markdown(*, title, character, snapshot, covered_upto, messages_total, tokens, budget, schema, exported_at, facts=None) -> str`;
`default_estimate_tokens(text) -> int`; `MASTER_STATE_PROMPT: str`;
`COMPACT_PROMPT: str` (поля `{budget}`, `{keep}`).

- [ ] **Step 1: тесты (падают)** — создать `tests/test_hierarchical_memory.py`:

```python
"""
Ядро иерархической памяти (backend/hierarchical_memory.py) без БД и сети.

LLM здесь — подставная корутина, sleep и часы — фиктивные: проверяем контракт
свёртки State_N = merge(State_{N-1}, Block_N), паузы, повторы, валидацию схемы и
стража записей, из-за которых раньше тонули списки и атрибуты.
"""
import asyncio
from datetime import datetime
from types import SimpleNamespace

import pytest

from backend import hierarchical_memory as hm


def _snap(chron="- [#1–#2] Артур встретил Эльвиру в таверне «Серый гусь».",
          chars="- Эльвира — здоровье: цела; локация: таверна",
          reg="- Орден Зари — тайный орден охотников (#1)",
          lists="### Плейлист Эльвиры\n- «Lacrimosa» — тема утраты (#2)"):
    return hm.render_snapshot({hm.SEC_CHRONICLE: chron, hm.SEC_CHARACTERS: chars,
                               hm.SEC_REGISTRY: reg, hm.SEC_LISTS: lists})


def _wrap(text):
    return f"{hm.ENVELOPE_OPEN}\n{text}\n{hm.ENVELOPE_CLOSE}"


def _msg(i, text="реплика", role="user", speaker=None, att=()):
    return hm.MemoryMessage(
        id=i, role=role, speaker=speaker or ("Артур" if role == "user" else "Эльвира"),
        text=text, created_at=datetime(2026, 9, 20, 14, 3), attachments=tuple(att))


# ==================== Нормализация ====================

def test_normalize_keeps_author_id_time_and_attachments():
    line = hm.normalize_message(_msg(7, "Смотри карту", att=({"type": "image", "name": "map.png"},)))
    assert line.startswith("[#7 · 2026-09-20 14:03 · Артур] Смотри карту")
    assert "📎 изображение «map.png»" in line


def test_normalize_strips_binary_markup_and_thoughts():
    b64 = "A" * 400
    text = (f"Привет <span style='x'>мир</span> data:image/png;base64,{b64} "
            f"<think>скрытое</think>{b64}<!-- c --><script>x()</script>конец")
    line = hm.normalize_message(_msg(1, text))
    assert "мир" in line and "конец" in line
    for junk in ("<span", "data:image", "AAAA", "скрытое", "<!--", "x()"):
        assert junk not in line


def test_normalize_skips_empty_but_keeps_attachment_only():
    assert hm.normalize_message(_msg(1, "   ")) == ""
    only = hm.normalize_message(_msg(2, "", att=({"type": "audio", "name": "v.ogg"},)))
    assert "аудио «v.ogg»" in only


def test_long_message_is_cut_to_head_and_tail():
    text = "НАЧАЛО " + "x" * 5000 + " КОНЕЦ"
    line = hm.normalize_message(_msg(1, text), max_chars=1000)
    assert "НАЧАЛО" in line and "КОНЕЦ" in line
    assert "середина длинного сообщения пропущена" in line
    assert len(line) < 1200


# ==================== Пакеты ====================

def test_plan_batch_respects_count_and_chars():
    msgs = [_msg(i, "слово " * 50) for i in range(1, 60)]
    b = hm.plan_batch(msgs, batch_size=20, max_chars=10**6)
    assert len(b.messages) == 20 and (b.first_id, b.last_id) == (1, 20)
    small = hm.plan_batch(msgs, batch_size=20, max_chars=700)
    assert 1 <= len(small.messages) < 20
    one = hm.plan_batch([_msg(1, "x" * 5000)], batch_size=20, max_chars=100)
    assert len(one.messages) == 1  # хотя бы одно сообщение всегда
    assert hm.plan_batch([], batch_size=20, max_chars=100) is None


def test_plan_batches_do_not_overlap():
    msgs = [_msg(i) for i in range(1, 46)]
    batches = hm.plan_batches(msgs, batch_size=20, max_chars=10**6)
    assert [len(b.messages) for b in batches] == [20, 20, 5]
    ids = [m.id for b in batches for m in b.messages]
    assert ids == list(range(1, 46))


# ==================== Схема и валидация ====================

def test_parse_sections_accepts_heading_variants_and_render_is_canonical():
    raw = ("### хроника и событийный каркас\n- a\n[АКТИВНЫЕ ПЕРСОНАЖИ И ИХ СТАТУСЫ]\n- b\n"
           "## [ФАКТОЛОГИЧЕСКИЙ РЕЕСТР И ЛОР]\n- c\n# СПИСКИ И МЕДИА-АНКОРЫ\n- d")
    secs = hm.parse_sections(raw)
    assert set(secs) == set(hm.SECTIONS)
    text = hm.render_snapshot(secs)
    assert text.index(f"## [{hm.SEC_CHRONICLE}]") < text.index(f"## [{hm.SEC_LISTS}]")
    assert hm.is_structured(text) and not hm.is_structured("Герои в пути.")
    assert f"## [{hm.SEC_REGISTRY}]\n—" in hm.render_snapshot({hm.SEC_CHRONICLE: "- x"})


def test_truncated_envelope_and_missing_section_are_problems():
    text, problems = hm.validate_snapshot(hm.ENVELOPE_OPEN + "\n" + _snap(), None)
    assert any("обрез" in p for p in problems)
    broken = hm.render_snapshot({hm.SEC_CHRONICLE: "- x"}).replace(f"## [{hm.SEC_LISTS}]\n—", "")
    _, problems = hm.validate_snapshot(_wrap(broken), None)
    assert any(hm.SEC_LISTS in p for p in problems)
    _, ok = hm.validate_snapshot(_wrap(_snap()), None)
    assert ok == []


def test_shrink_is_caught_only_for_structured_prev():
    big = _snap(chron="\n".join(f"- [#{i}–#{i}] событие номер {i} с подробностями" for i in range(1, 200)))
    tiny = _wrap(_snap(chron="- [#1–#2] всё"))
    _, problems = hm.validate_snapshot(tiny, big, estimate_tokens=len)
    assert any("полов" in p for p in problems)
    _, problems = hm.validate_snapshot(tiny, "Старая сводка " * 500, estimate_tokens=len)
    assert problems == []  # конвертация старой схемы — не «сдувание»


# ==================== Страж записей ====================

def test_guard_restores_lost_list_registry_and_character_entries():
    prev = _snap(chars="- Эльвира — цела\n- Артур — ранен в плечо",
                 lists="### Плейлист Эльвиры\n- «Lacrimosa» — тема утраты (#2)\n- «Nocturne» — тема надежды (#9)")
    new = _snap(chars="- Эльвира — цела", reg="—",
                lists="### Плейлист Эльвиры\n- «Lacrimosa» — тема утраты (#2)")
    fixed, restored = hm.guard_entries(prev, new)
    secs = hm.parse_sections(fixed)
    assert "Артур — ранен в плечо" in secs[hm.SEC_CHARACTERS]
    assert "Орден Зари" in secs[hm.SEC_REGISTRY]
    assert "«Nocturne» — тема надежды" in secs[hm.SEC_LISTS]
    assert len(restored) == 3


def test_guard_does_not_duplicate_updated_or_recased_entries():
    prev = _snap(chars="- Артём — ранен", lists="### Треки\n- «Лёд» — тема (#1)")
    new = _snap(chars="- артем — здоров (было: ранен, #5)", lists="### Треки\n- «Лед» — тема (#1)")
    fixed, restored = hm.guard_entries(prev, new)
    assert restored == []
    assert fixed.count("ранен") == 1


def test_guard_ignores_chronicle():
    prev = _snap(chron="- [#1–#2] старое\n- [#3–#4] ещё")
    new = _snap(chron="- [#1–#4] Арка «Начало»: старое и ещё")
    fixed, restored = hm.guard_entries(prev, new)
    assert restored == [] and "- [#3–#4] ещё" not in fixed


# ==================== Окно, бюджет, экспорт ====================

def test_window_start_moved_verbatim_and_sliding_window_split():
    ids = list(range(1, 101))
    assert hm.window_start(ids, covered=100, window=50) == 48  # 50 → кратно шагу 4
    assert hm.window_start(ids, covered=10, window=50) == 8
    assert hm.window_start(ids, covered=100, window=0) == 0
    w = hm.SlidingWindow(50)
    dropped, raw = w.split(ids, covered=100)
    assert dropped == ids[:48] and raw == ids[48:]
    assert w.pending_for_summary(ids, covered=40) == list(range(41, 51))


def test_budget_tiers_sum_and_percentages():
    t = hm.budget_tiers(system=5000, memory=4000, window=60000, current=1000,
                        budget=200000, model_limit=1000000)
    assert t["total"] == 70000 and t["pct_budget"] == 35.0 and t["pct_limit"] == 7.0


def test_export_markdown_has_header_snapshot_and_optional_facts():
    md = hm.render_export_markdown(
        title="Долгая дорога", character="Эльвира", snapshot=_snap(), covered_upto=812,
        messages_total=900, tokens=4200, budget=12000, schema="hms-1",
        exported_at="2026-09-26 12:00 UTC", facts=["Артур ранен в плечо"])
    assert md.startswith("# Мастер-снимок памяти — «Долгая дорога»")
    assert "Учтено до: #812" in md and f"## [{hm.SEC_LISTS}]" in md
    assert "атомарные факты (1)" in md and "- Артур ранен в плечо" in md
    assert "атомарные факты" not in hm.render_export_markdown(
        title="t", character="c", snapshot=_snap(), covered_upto=1, messages_total=1,
        tokens=1, budget=1, schema="hms-1", exported_at="x")


def test_progress_line_format():
    p = hm.ScanProgress(processed=140, total=800, batches=7, state_tokens=4200, phase="merge")
    assert p.line() == "[Обработано 140/800 сообщений | Сжато до 4 200 токенов]"


def test_prompts_carry_the_schema_and_rules():
    for sec in hm.SECTIONS:
        assert f"[{sec}]" in hm.MASTER_STATE_PROMPT
    assert hm.ENVELOPE_OPEN in hm.MASTER_STATE_PROMPT
    assert "{budget}" in hm.COMPACT_PROMPT and "{keep}" in hm.COMPACT_PROMPT
```

- [ ] **Step 2:** `pytest tests/test_hierarchical_memory.py -q` → FAIL (нет модуля).
- [ ] **Step 3: реализация** по §4.1–4.3, §4.5 (валидация), §4.6, §4.9, §4.10, §5.
  Ключевые детали:
  - Заголовок раздела — регэксп по строке:
    `^\s{0,3}(?:#{1,4}\s*)?\[?\s*(<ИМЯ>)\s*\]?\s*:?\s*$` без учёта регистра, ё≡е
    (сравнивать после нормализации строки: `lower().replace("ё","е")`).
  - `extract_snapshot(raw)`: вырезать `<think>…</think>`, ограждения ```` ``` ````;
    `ENVELOPE_OPEN` без `ENVELOPE_CLOSE` → `(текст_после_открытия, True)`.
  - `validate_snapshot` возвращает текст УЖЕ в каноническом виде
    `render_snapshot(parse_sections(...))`, если проблем нет. Тексты проблем (по-русски):
    «ответ обрезан (нет закрывающего </master_state>)», «пустой ответ»,
    «нет раздела [ИМЯ]», «снимок потерял больше половины содержимого (N → M токенов)»,
    «снимок неправдоподобно длинный». Проверка «сдувания» — только если
    `check_shrink` и `is_structured(prev)` и `est(prev) >= config.shrink_min_tokens`.
  - Страж (§4.6): разбить раздел на записи (маркер верхнего уровня + продолжение),
    для `SEC_LISTS` — с учётом `### подзаголовков`; ключ → нормализация → сравнение
    точное или Жаккар по токенам ≥ 0.6; пропавшие дописать в конец своего
    раздела/подсписка исходным текстом.
  - `ScanProgress.line()`: числа токенов с разделителем тысяч ` `.
  - `default_estimate_tokens`: как `horae_memory._tokens_heuristic`
    (`heavy/2 + rest/4`, минимум 1).
  - `MASTER_STATE_PROMPT` и `COMPACT_PROMPT` — полный текст по §5.1–5.2 (по-русски).
- [ ] **Step 4:** тесты → PASS.
- [ ] **Step 5: коммит** `feat(memory): hierarchical memory core — schema, normalization, guard`.

---

### Задача 3: Ядро — `HierarchicalMemoryManager`: вызов, слияние, сжатие, цикл

**Files:**
- Modify: `backend/hierarchical_memory.py`
- Test: `tests/test_hierarchical_memory.py` (дописать)

**Interfaces:**
- Consumes: всё из Задачи 2.
- Produces:
  `LLMCall = Callable[[list[dict]], Awaitable[str]]`;
  `classify_error(exc) -> MemoryLLMError`;
  `class BatchSource(Protocol)` — `pending()`, `current_state(state)`,
  `next_messages(limit)`, `commit(batch, new_state) -> bool` (все async);
  `class ListSource(messages)`;
  `class HierarchicalMemoryManager(llm, config=None, *, sleep=asyncio.sleep, clock=time.monotonic, estimate_tokens=None)`
  c атрибутом `warnings: list[str]` и методами
  `plan_batch(messages)`, `plan_batches(messages)` (с параметрами из config),
  `async call(messages) -> str`, `async merge_block(state, batch) -> str`,
  `async compact(state) -> str`,
  `async scan_and_compress_history(source, state="", *, on_progress=None, cancel=None, max_batches=None, retry_conflicts=True) -> ScanResult`.
  `ScanResult.status`: `"done" | "cancelled" | "limit" | "conflict"`.

- [ ] **Step 1: тесты (падают)** — дописать:

```python
class _Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def _mgr(llm, **cfg):
    clock, sleeps = _Clock(), []

    async def sleep(s):
        sleeps.append(round(s, 3))
        clock.t += s

    m = hm.HierarchicalMemoryManager(llm, hm.MemoryConfig(**{"delay_ms": 0, **cfg}),
                                     sleep=sleep, clock=clock, estimate_tokens=len)
    return m, sleeps


class _Err(Exception):
    def __init__(self, status, retry_after=None):
        super().__init__(f"HTTP {status}")
        self.status_code = status
        self.response = SimpleNamespace(status_code=status,
                                        headers={"retry-after": str(retry_after)} if retry_after else {})


def _echo_llm(record):
    """Возвращает валидный снимок, чья хроника — диапазон пакета из запроса."""
    async def llm(messages):
        record.append(messages)
        user = messages[1]["content"]
        rng = user.split("[Новые события ", 1)[1].split("]", 1)[0]
        prev = user.split("[Текущая память]\n", 1)[1].split("\n\n[Новые события", 1)[0]
        chron = "" if prev == hm.EMPTY_STATE else hm.parse_sections(prev)[hm.SEC_CHRONICLE] + "\n"
        return _wrap(_snap(chron=chron + f"- [{rng}] пакет"))
    return llm


async def test_fold_carries_previous_state_into_each_batch():
    calls = []
    m, _ = _mgr(_echo_llm(calls), batch_size=2)
    res = await m.scan_and_compress_history([_msg(i) for i in range(1, 6)])
    assert res.status == "done" and res.batches == 3 and res.processed == 5
    assert calls[0][1]["content"].startswith("[Текущая память]\n(пока пусто)")
    assert "[Новые события #1–#2]" in calls[0][1]["content"]
    assert "- [#1–#2] пакет" in calls[1][1]["content"]           # State_1 ушёл в пакет 2
    assert "- [#3–#4] пакет" in calls[2][1]["content"]
    assert hm.parse_sections(res.state)[hm.SEC_CHRONICLE].count("пакет") == 3
    assert calls[0][0]["content"] == hm.MASTER_STATE_PROMPT


async def test_delay_between_every_request():
    m, sleeps = _mgr(_echo_llm([]), batch_size=1, delay_ms=1500)
    await m.scan_and_compress_history([_msg(i) for i in range(1, 4)])
    assert sleeps == [1.5, 1.5]  # перед 2-м и 3-м запросом, не перед первым


async def test_rate_limit_is_retried_with_backoff_and_retry_after():
    attempts = []

    async def flaky(messages):
        attempts.append(1)
        if len(attempts) == 1:
            raise _Err(429)
        if len(attempts) == 2:
            raise _Err(503, retry_after=7)
        return _wrap(_snap())

    m, sleeps = _mgr(flaky)
    out = await m.call([{"role": "user", "content": "x"}])
    assert hm.ENVELOPE_OPEN in out and sleeps == [2.0, 7.0]


async def test_auth_error_is_fatal_and_not_retried():
    attempts = []

    async def denied(messages):
        attempts.append(1)
        raise _Err(401)

    m, sleeps = _mgr(denied)
    with pytest.raises(hm.MemoryLLMError) as e:
        await m.call([{"role": "user", "content": "x"}])
    assert e.value.kind == "auth" and not e.value.retryable
    assert len(attempts) == 1 and sleeps == []


async def test_retries_are_bounded():
    async def down(messages):
        raise _Err(500)

    m, sleeps = _mgr(down, max_retries=2)
    with pytest.raises(hm.MemoryLLMError) as e:
        await m.call([{"role": "user", "content": "x"}])
    assert e.value.kind == "server" and sleeps == [2.0, 4.0]


async def test_cancelled_error_is_never_swallowed():
    async def cancelled(messages):
        raise asyncio.CancelledError()

    m, _ = _mgr(cancelled)
    with pytest.raises(asyncio.CancelledError):
        await m.call([{"role": "user", "content": "x"}])


def test_classify_explain_block_texts():
    length = hm.classify_error(RuntimeError("…\nТехнически: ПУСТОЙ ответ, finish_reason=LENGTH."))
    blocked = hm.classify_error(RuntimeError("…\nТехнически: ПУСТОЙ ответ, finish_reason=SAFETY."))
    empty = hm.classify_error(RuntimeError("…\nТехнически: ПУСТОЙ ответ, finish_reason=не указан."))
    assert (length.kind, blocked.kind, empty.kind) == ("length", "blocked", "empty")
    assert empty.retryable and not blocked.retryable


async def test_malformed_answer_gets_a_correction_turn():
    answers = [_wrap("просто пересказ без разделов"), _wrap(_snap())]
    seen = []

    async def llm(messages):
        seen.append(messages)
        return answers.pop(0)

    m, _ = _mgr(llm)
    batch = hm.plan_batch([_msg(1)], batch_size=20, max_chars=10**6)
    state = await m.merge_block("", batch)
    assert hm.is_structured(state)
    assert seen[1][-1]["role"] == "user" and "Ответ отклонён" in seen[1][-1]["content"]
    assert seen[1][-2] == {"role": "assistant", "content": _wrap("просто пересказ без разделов")}


async def test_persistent_garbage_raises_and_writes_nothing():
    async def llm(messages):
        return "ерунда"

    m, _ = _mgr(llm, validation_retries=2)
    batch = hm.plan_batch([_msg(1)], batch_size=20, max_chars=10**6)
    with pytest.raises(hm.SnapshotValidationError):
        await m.merge_block("", batch)


async def test_guard_runs_after_merge():
    prev = _snap(lists="### Треки\n- «Lacrimosa» — утрата (#2)\n- «Nocturne» — надежда (#9)")

    async def forgetful(messages):
        return _wrap(_snap(lists="### Треки\n- «Lacrimosa» — утрата (#2)"))

    m, _ = _mgr(forgetful)
    batch = hm.plan_batch([_msg(10)], batch_size=20, max_chars=10**6)
    state = await m.merge_block(prev, batch)
    assert "«Nocturne» — надежда (#9)" in state
    assert any("возвращены" in w for w in m.warnings)


async def test_over_budget_snapshot_is_compacted_into_arcs():
    long_chron = "\n".join(f"- [#{i}–#{i}] событие {i}" for i in range(1, 80))
    prompts = []

    async def llm(messages):
        prompts.append(messages[0]["content"])
        if messages[0]["content"] == hm.MASTER_STATE_PROMPT:
            return _wrap(_snap(chron=long_chron))
        return _wrap(_snap(chron="- [#1–#79] Арка «Дорога»: всё важное"))

    m, _ = _mgr(llm, snapshot_tokens=600)
    batch = hm.plan_batch([_msg(80)], batch_size=20, max_chars=10**6)
    state = await m.merge_block("", batch)
    assert "Арка «Дорога»" in state and len(prompts) == 2
    assert prompts[1].startswith(hm.COMPACT_PROMPT.split("{", 1)[0])


async def test_length_error_compacts_state_then_retries_merge():
    calls = []

    async def llm(messages):
        calls.append(messages[0]["content"])
        if len(calls) == 1:
            raise RuntimeError("Технически: ПУСТОЙ ответ, finish_reason=LENGTH.")
        return _wrap(_snap())

    m, _ = _mgr(llm)
    batch = hm.plan_batch([_msg(1)], batch_size=20, max_chars=10**6)
    await m.merge_block(_snap(), batch)
    assert calls[0] == hm.MASTER_STATE_PROMPT and calls[-1] == hm.MASTER_STATE_PROMPT
    assert any(c != hm.MASTER_STATE_PROMPT for c in calls[1:-1])  # было сжатие


async def test_cancel_stops_between_batches_and_keeps_progress():
    cancel = asyncio.Event()
    progress = []

    async def llm(messages):
        cancel.set()  # отмена приходит, пока модель считает первый пакет
        return _wrap(_snap())

    m, _ = _mgr(llm, batch_size=1)
    res = await m.scan_and_compress_history([_msg(i) for i in range(1, 4)], cancel=cancel,
                                            on_progress=progress.append)
    assert res.status == "cancelled" and res.processed == 1
    assert progress[-1].phase == "done"


async def test_max_batches_limits_the_run():
    m, _ = _mgr(_echo_llm([]), batch_size=1)
    res = await m.scan_and_compress_history([_msg(i) for i in range(1, 6)], max_batches=2)
    assert res.status == "limit" and res.batches == 2


async def test_progress_reports_totals_and_line():
    progress = []
    m, _ = _mgr(_echo_llm([]), batch_size=2)
    await m.scan_and_compress_history([_msg(i) for i in range(1, 6)], on_progress=progress.append)
    merges = [p for p in progress if p.phase == "merge"]
    assert [p.processed for p in merges] == [0, 2, 4, 5]
    assert all(p.total == 5 for p in merges)
    assert merges[-1].line().startswith("[Обработано 5/5 сообщений | Сжато до ")


class _ConflictingSource:
    """Источник, чей первый commit отвергается: кусок менялся, пока модель считала."""
    def __init__(self, msgs, conflicts):
        self.msgs, self.conflicts, self.pos = msgs, conflicts, 0

    async def pending(self):
        return len(self.msgs) - self.pos

    async def current_state(self, state):
        return state

    async def next_messages(self, limit):
        return self.msgs[self.pos:self.pos + limit]

    async def commit(self, batch, new_state):
        if self.conflicts:
            self.conflicts -= 1
            return False
        self.pos += len(batch.messages)
        return True


async def test_conflict_stops_incremental_run_but_job_retries():
    m, _ = _mgr(_echo_llm([]), batch_size=5)
    res = await m.scan_and_compress_history(_ConflictingSource([_msg(1)], 1), retry_conflicts=False)
    assert res.status == "conflict" and res.processed == 0
    res = await m.scan_and_compress_history(_ConflictingSource([_msg(1)], 1))
    assert res.status == "done" and res.processed == 1
    with pytest.raises(hm.SourceConflictError):
        await m.scan_and_compress_history(_ConflictingSource([_msg(1)], 10))
```

- [ ] **Step 2:** прогон → FAIL.
- [ ] **Step 3: реализация** (§4.4, §4.5, §4.7, §4.8). Опорный код:

```python
async def call(self, messages: list[dict]) -> str:
    """Один запрос к модели: пауза-ограничитель + повтор с бэкоффом (§4.4)."""
    if self._last_end is not None:
        wait = self.config.delay_ms / 1000 - (self._clock() - self._last_end)
        if wait > 0:
            self._emit(phase="wait", retry_in_s=round(wait, 1))
            await self._sleep(wait)
    attempt = 0
    while True:
        try:
            out = await self._llm(messages)
            self._last_end = self._clock()
            return out
        except asyncio.CancelledError:
            raise
        except MemoryLLMError:
            raise
        except Exception as exc:  # noqa: BLE001 — классифицируем любую ошибку провайдера
            self._last_end = self._clock()
            err = classify_error(exc)
            if not err.retryable or attempt >= self.config.max_retries:
                raise err from exc
            wait = min(self.config.backoff_max_s, self.config.backoff_base_s * 2 ** attempt)
            if err.retry_after:
                wait = min(300.0, max(wait, err.retry_after))
            self._emit(phase="retry", retry_in_s=wait)
            await self._sleep(wait)
            attempt += 1
```

`merge_block`: запрос §4.5 → `validate_snapshot(raw, state)` → при проблемах
корректирующий ход (`assistant` = сырой ответ, `user` = «Ответ отклонён: …»), до
`validation_retries`; затем `SnapshotValidationError`. `MemoryLLMError(kind="length")`
→ один раз `state = await self.compact(state)` и повтор слияния. После валидного
ответа — `guard_entries(state, new)` (если `is_structured(state)`; восстановленные
ключи → `self.warnings.append("модель потеряла N записей — возвращены из предыдущего снимка")`),
затем, если `est(new) > snapshot_tokens`, — `compact(new)`.

`compact`: `COMPACT_PROMPT.format(budget=..., keep=...)` (system) +
`"[Текущая память]\n" + state` (user); валидация с `check_shrink=False`, корректирующие
ходы; неудача или хроника не короче → предупреждение и исходный `state`; успех →
`guard_entries(state, compacted)`; всё ещё больше бюджета → предупреждение
«снимок превышает бюджет: X из Y токенов».

`scan_and_compress_history`: алгоритм §4.8. `max_batches` считает ПОПЫТКИ, дошедшие
до `commit` (и успешные, и отвергнутые). Отвергнутый `commit`: при
`retry_conflicts=False` → `status="conflict"`, выход; иначе счётчик подряд,
> 3 → `SourceConflictError`. `on_progress` вызывается: в начале (`merge`,
processed=0), после каждого принятого пакета, на `wait`/`retry` (через `_emit`) и в
конце (`done`). `self.warnings` копируется в `ScanResult.warnings`.

- [ ] **Step 4:** весь `tests/test_hierarchical_memory.py` → PASS.
- [ ] **Step 5: коммит** `feat(memory): HierarchicalMemoryManager — batch fold with delay, retry, validation`.

---

### Задача 4: Окно 50, реэкспорт окна, уровни токенов в отчёте, рендер снимка

**Files:**
- Modify: `backend/horae_recall.py:54-61, 145-179` — `DEFAULT_WINDOW = hm.DEFAULT_WINDOW`
  (обновить комментарий: «ТЗ: 50–150; 50 — нижняя граница»), `window_start`/`WINDOW_STEP`
  — импорт из `backend.hierarchical_memory` (функцию из файла удалить).
- Modify: `backend/horae_memory.py:827-943` — ключи хвоста, `report["tiers"]`,
  рендер структурированного снимка (§7).
- Test: `tests/test_memory_tiers.py` (новый)

**Interfaces:**
- Consumes: `hm.window_start`, `hm.is_structured`, `hm.budget_tiers`, `settings.MODEL_CONTEXT_LIMIT`.
- Produces: `report["tail"][i]["key"]`; `report["tiers"]` = `budget_tiers(...)` +
  `window_messages`, `dropped_messages`, `trimmed_messages`.

- [ ] **Step 1: тесты (падают)** — `tests/test_memory_tiers.py`:

```python
"""Монитор токенов по уровням памяти (report["tiers"]) и рендер мастер-снимка в хвосте."""
from backend import hierarchical_memory as hm
from backend import horae_recall as hr
from backend.horae_memory import HoraeRecord, assemble_context


def _char():
    return {"name": "Эльвира", "description": "Ведьма", "personality": "язвительная"}


def test_default_window_is_fifty_and_window_start_is_shared():
    assert hr.DEFAULT_WINDOW == 50 and hr.window_start is hm.window_start


def test_tiers_split_system_memory_window_current():
    snap = hm.render_snapshot({hm.SEC_CHRONICLE: "- [#1–#9] Эльвира нашла карту."})
    rec = HoraeRecord(category="summary", title="📜 Память чата (авто)", content=snap,
                      keywords=["__auto__"], always_on=True, enabled=True, priority=50)
    history = [{"role": "user", "content": f"реплика {i}"} for i in range(10)]
    report = {}
    assemble_context(character=_char(), horae_records=[rec], history=history,
                     user_message="Что дальше?", token_budget=200_000, report=report)
    t = report["tiers"]
    assert t["memory"] > 0 and t["window"] > 0 and t["system"] > 0 and t["current"] > 0
    assert t["total"] == t["system"] + t["memory"] + t["window"] + t["current"]
    assert t["budget"] == 200_000 and t["model_limit"] == 1_000_000
    keys = [b["key"] for b in report["tail"]]
    assert keys[0] == "snapshot" and "focus" in keys and "anchor" in keys


def test_structured_snapshot_is_rendered_without_title_prefix():
    snap = hm.render_snapshot({hm.SEC_CHRONICLE: "- [#1–#9] Эльвира нашла карту."})
    rec = HoraeRecord(category="summary", title="📜 Память чата (авто)", content=snap,
                      keywords=["__auto__"], always_on=True, enabled=True, priority=50)
    msgs = assemble_context(character=_char(), horae_records=[rec], history=[],
                            user_message="?", token_budget=200_000)
    block = next(m["content"] for m in msgs if "ХРОНИКА И СОСТОЯНИЕ ЧАТА" in str(m["content"]))
    assert "Что было в истории" in block and f"## [{hm.SEC_CHRONICLE}]" in block
    assert "- 📜 Память чата (авто):" not in block
```

- [ ] **Step 2:** прогон → FAIL.
- [ ] **Step 3: реализация.**
  - Каждый `tail.append(...)` в `assemble_context` помечается ключом: хранить
    параллельный список `tail_keys` (сами сообщения в модель уходят без ключа!),
    `report["tail"] = [{"key": k, "tokens": …, "text": …}]`.
  - Снимок: если `len(summary_recs) == 1 and hm.is_structured(r.content)` → тело
    `"[ХРОНИКА И СОСТОЯНИЕ ЧАТА] Что было в истории — помни это.\n"
     "Мастер-снимок старой части чата; последние сообщения выше идут дословно.\n\n" + content`;
    иначе — как сейчас.
  - `tiers`: `system = report["system_tokens"] + knowledge_tokens + токены аватаров
    (сообщения _avatar_messages через estimate_content_tokens) + сумма хвоста по ключам
    manifest/appearance/web/time/author_note/anchor/post_history/global/focus`;
    `memory = хвост snapshot + recalled`; `window = sum(costs[start:])`;
    `current = estimate_content_tokens(user_attachments_content) если не None, иначе
    estimate_tokens(user_message) при непустом тексте, иначе 0`;
    `hm.budget_tiers(..., budget=token_budget, model_limit=settings.MODEL_CONTEXT_LIMIT)`
    + `window_messages=len(trimmed_history)`, `trimmed_messages=start`,
    `dropped_messages=(report.get("memory") or {}).get("dropped", 0)`.
  - Существующие ключи отчёта не меняются.
- [ ] **Step 4:** `tests/test_memory_tiers.py`, `tests/test_horae_memory.py`,
  `tests/test_horae_recall.py`, `tests/test_ooc_mode.py`, `tests/test_usage_stats.py`,
  `tests/test_horae_triggers.py` → PASS (или только известные падения окружения).
  Тесты, которые жёстко рассчитывали окно 20 вместо `DEFAULT_WINDOW`, — поправить на
  константу.
- [ ] **Step 5: коммит** `feat(memory): window 50, per-tier token report, structured snapshot block`.

---

### Задача 5: Сервис памяти и ежеходный инкремент на новом движке

**Files:**
- Create: `backend/memory_service.py`
- Modify: `backend/main.py:413-845` (сводка) и места вызова (`delete_message` ~1909)
- Modify: `tests/test_memory.py`, `tests/test_horae_recall.py`, `tests/test_horae_triggers.py`
  (только подмены `complete` и проверки текста снимка)
- Test: `tests/test_memory_service.py` (новый, часть 1)

**Interfaces:**
- Consumes: ядро (Задачи 2–3), `llm_gateway.sampling_overrides` (Задача 1),
  `horae_recall.FACTS_PROMPT/parse_facts/store_facts/trusted_pointer/DEFAULT_WINDOW`,
  `horae_memory.estimate_tokens`.
- Produces (в `memory_service`):
  `MemoryDeps(complete, get_connection)`;
  `summary_last_id(entry) -> int`, `int_or_zero(v) -> int`,
  `async clamp_summary_pointer(db, session_id) -> int | None`,
  `adopt_summary(entry, content, last_id, *, tokens=None, updated_by="incremental", warnings=None)`,
  `AUTO_SUMMARY_MARK`, `AUTO_SUMMARY_TITLE`;
  `async load_memory_messages(db, session, after_id, before_id=None) -> list[hm.MemoryMessage]`;
  `class DbBatchSource` (§6.3);
  `def build_manager(connection, ui_value, deps, *, on_progress=None) -> tuple[hm.HierarchicalMemoryManager, dict]`
  (второе — `bg_conn`);
  `def memory_config(ui_value, **overrides) -> hm.MemoryConfig`;
  `session_lock(session_id) -> asyncio.Lock`, `is_busy(session_id) -> bool`;
  `async run_incremental(session_id, deps, *, max_batches) -> hm.ScanResult | None`.
  В `main`: `_summary_last_id`, `_int_or_zero`, `_clamp_summary_pointer`,
  `_adopt_summary`, `_AUTO_SUMMARY_MARK`, `_AUTO_SUMMARY_TITLE`,
  `_SUMMARY_CHUNK_CHARS`, `_SUMMARY_MAX_CHUNKS`, `_summary_running` —
  остаются доступными (реэкспорт/псевдонимы); `_memory_deps()` строит `MemoryDeps`
  с поздним связыванием:
  `MemoryDeps(complete=lambda *a, **k: complete(*a, **k), get_connection=lambda db: get_connection(db))`.

- [ ] **Step 1: обновить подмены в существующих тестах.** Каждая подмена `complete`,
  возвращающая сводку, возвращает валидный снимок:
  `hm.render_snapshot({hm.SEC_CHRONICLE: "- [#1–#12] <прежний текст>"})` (ответ на
  `FACTS_PROMPT` — без изменений). Проверки `content.startswith("X")` / `== "X"` для
  снимка → `"X" in content`. Логику (указатели, пересборка, сверка, факты, модель,
  `params is None`, число вызовов) НЕ менять. Прогон этих файлов на СТАРОМ коде →
  тесты с изменённым текстом могут падать только на проверках текста — это ожидаемо
  до Step 3.
- [ ] **Step 2: новые тесты (падают)** — `tests/test_memory_service.py`, часть 1:

```python
"""
Сервис памяти (backend/memory_service.py): ежеходный инкремент на новом движке,
задания пересборки, статус, сброс и экспорт. LLM подменяется через
backend.main.complete — как во всех тестах памяти.
"""
from unittest.mock import patch

from sqlalchemy import select

from backend import hierarchical_memory as hm
from backend import horae_recall as hr


async def _fresh_db():
    from backend.database import engine, init_db
    await engine.dispose()
    await init_db()


async def _make_chat(n, prefix="событие", persona=None):
    from backend import models
    from backend.database import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        ch = models.Character(name="Эльвира")
        db.add(ch)
        await db.flush()
        pid = None
        if persona:
            p = models.Persona(name=persona)
            db.add(p)
            await db.flush()
            pid = p.id
        sess = models.ChatSession(character_id=ch.id, user_key="test:hms", persona_id=pid)
        db.add(sess)
        await db.flush()
        msgs = [models.Message(session_id=sess.id, role="user" if i % 2 == 0 else "assistant",
                               content=f"{prefix} {i}") for i in range(n)]
        db.add_all(msgs)
        await db.commit()
        return ch.id, sess.id, [m.id for m in msgs]


async def _entry(sid):
    from backend import models
    from backend.database import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        return (await db.execute(select(models.HoraeEntry).where(
            models.HoraeEntry.session_id == sid,
            models.HoraeEntry.category == "summary"))).scalars().first()


def _snapshot_llm(seen):
    async def fake_complete(messages, params=None, connection=None, kind="service"):
        seen.append(messages)
        if messages[0]["content"] == hr.FACTS_PROMPT:
            return ""
        user = messages[-1]["content"] if messages[-1]["role"] == "user" else messages[1]["content"]
        rng = user.split("[Новые события ", 1)[1].split("]", 1)[0] if "[Новые события " in user else "#?"
        return hm.ENVELOPE_OPEN + hm.render_snapshot({hm.SEC_CHRONICLE: f"- [{rng}] сжато"}) + hm.ENVELOPE_CLOSE
    return fake_complete


async def test_incremental_uses_master_prompt_names_and_new_schema():
    from backend import main
    from backend.database import engine
    await _fresh_db()
    _, sid, ids = await _make_chat(12 + hr.DEFAULT_WINDOW, persona="Артур")
    seen = []
    with patch("backend.main.complete", new=_snapshot_llm(seen)):
        await main._maybe_update_summary(sid)
    first = seen[0]
    assert first[0]["content"] == hm.MASTER_STATE_PROMPT
    assert "· Артур] событие 0" in first[1]["content"]    # имя персоны, а не «Пользователь»
    assert "· Эльвира] событие 1" in first[1]["content"]  # имя персонажа, а не «Персонаж»
    entry = await _entry(sid)
    assert entry.meta["schema"] == hm.SNAPSHOT_SCHEMA and entry.meta["v"] == hr.SUMMARY_FORMAT
    assert entry.meta["last_message_id"] == ids[11] and entry.meta["tokens"] > 0
    assert f"## [{hm.SEC_CHRONICLE}]" in entry.content
    await engine.dispose()


async def test_incremental_passes_sampling_overrides_and_keeps_safety_off():
    from backend import llm_gateway, main
    from backend.database import engine
    await _fresh_db()
    _, sid, _ = await _make_chat(12 + hr.DEFAULT_WINDOW)
    seen = []

    async def fake_complete(messages, params=None, connection=None, kind="service"):
        seen.append((params, dict(llm_gateway._SAMPLING_OVERRIDES.get() or {}), kind))
        return await _snapshot_llm([])(messages, params, connection, kind)

    with patch("backend.main.complete", new=fake_complete):
        await main._maybe_update_summary(sid)
    params, overrides, kind = seen[0]
    assert params is None and kind == "summary"
    assert overrides["temperature"] == 0.2 and overrides["max_tokens"] >= 12_000
    await engine.dispose()


async def test_long_snapshot_is_not_cut_to_6000_chars():
    from backend import main
    from backend.database import engine
    await _fresh_db()
    _, sid, _ = await _make_chat(12 + hr.DEFAULT_WINDOW)
    big = "\n".join(f"- [#{i}–#{i}] длинное событие номер {i} с подробностями" for i in range(400))

    async def fake_complete(messages, params=None, connection=None, kind="service"):
        if messages[0]["content"] == hr.FACTS_PROMPT:
            return ""
        return hm.render_snapshot({hm.SEC_CHRONICLE: big})

    with patch("backend.main.complete", new=fake_complete):
        await main._maybe_update_summary(sid)
    entry = await _entry(sid)
    assert len(entry.content) > 6000 and "номер 399" in entry.content
    await engine.dispose()
```

(Бюджет снимка по умолчанию 12 000 токенов; 400 строк ≈ 6–8 тыс. токенов —
сжатия не будет. Если оценка окажется выше бюджета, уменьшить число строк до 300.)

- [ ] **Step 3: реализация `memory_service.py` + обёртки в `main.py`.**
  - Перенести из `main.py` в сервис (с исходными комментариями «почему»):
    `_summary_last_id`, `_int_or_zero`, `_clamp_summary_pointer`, `_adopt_summary`
    (без `[:6000]`; `meta.update(last_message_id, v=SUMMARY_FORMAT, schema=hm.SNAPSHOT_SCHEMA,
    tokens=..., updated_by=..., warnings=(warnings or [])[-5:])`), `_ui_flag`,
    константы метки/заголовка. В `main` — реэкспорт под старыми именами.
  - `load_memory_messages`: имена — §6.3 (персона — `Persona.name` по
    `session.persona_id`; персонаж — `Character.name` по `session.character_id`);
    `created_at` → `ZoneInfo(session.timezone)` (naive считать UTC; неверный
    часовой пояс → без перевода); `attachments` — `{type,name,mime}` из
    `Message.attachments`; сообщения без текста и без вложений не возвращать.
  - `DbBatchSource` — перенос логики `_summary_pass` (стр. 606–826) на протокол
    `BatchSource`: `pending`, `current_state`, `next_messages`, `commit` (§6.3).
    Порог `threshold` и подмена буфера без модели — в `next_messages`. Сверка куска
    — в `commit` (сравнивать хэши `content` ТОЛЬКО по id сообщений пакета и
    непустым сообщениям диапазона `first_id..last_id`, как сейчас). Факты — в
    `commit` после коммита снимка через `manager.call` (ошибка логируется).
    `commit` возвращает `False` и НЕ пишет факты при несовпадении.
  - `build_manager`: `bg_conn` как сейчас (`summary_model` → `default_model`);
    колбэк LLM (§6.1) с `llm_gateway.sampling_overrides(max_tokens=out_tokens,
    temperature=settings.MEMORY_TEMPERATURE)`; `estimate_tokens=horae_memory.estimate_tokens`;
    `memory_config(ui_value)` читает `memory_batch`, `memory_delay_ms`,
    `memory_snapshot_tokens` из `ui` с дефолтами из `settings`
    (`batch_max_chars=settings.MEMORY_BATCH_CHARS`, `max_retries=settings.MEMORY_MAX_RETRIES`).
  - `run_incremental`: если `is_busy` → `None`; под `session_lock`: прочитать `ui`
    (выключатель `auto_summary`), `connection = await deps.get_connection(db)`,
    `scan_and_compress_history(DbBatchSource(mode="incremental", threshold=summary_every),
    max_batches=max_batches, retry_conflicts=False)`.
  - `main._maybe_update_summary(sid)`: `run_incremental(..., max_batches=_SUMMARY_MAX_CHUNKS)`
    в `try/except` (лог), затем `_backfill_fact_vectors` — как сейчас.
    `main._summary_pass(sid) -> bool`: `run_incremental(..., max_batches=1)`; `True`,
    если пакет записан и бэклог остался.
  - `main._summary_running`: псевдоним множества занятых чатов из сервиса
    (`memory_service._busy`), чтобы старый код и тесты видели занятость.
- [ ] **Step 4:** `tests/test_memory_service.py`, `tests/test_memory.py`,
  `tests/test_horae_recall.py`, `tests/test_horae_triggers.py`,
  `tests/test_hierarchical_memory.py`, `tests/test_memory_tiers.py` → PASS.
- [ ] **Step 5: коммит** `feat(memory): memory service — incremental chronicle on the hierarchical engine`.

---

### Задача 6: Задания Rebuild/Catch-up, статус, сброс, экспорт, эндпоинты

**Files:**
- Modify: `backend/memory_service.py`, `backend/main.py` (эндпоинты рядом с `inspect_context`)
- Test: `tests/test_memory_service.py` (часть 2)

**Interfaces:**
- Produces: `MemoryJob` (§6.5); `class JobConflict(Exception)`;
  `async start_job(session_id, deps, *, mode, resume=False, batch_size=None, delay_ms=None) -> MemoryJob`;
  `cancel_job(session_id) -> MemoryJob | None`; `get_job(session_id) -> MemoryJob | None`;
  `async wait_job(session_id)` (для тестов и purge);
  `async status(db, session_id) -> dict` (§6.6); `async purge(db, session_id) -> dict`;
  `async export_markdown(db, session_id, include_facts=False) -> tuple[str, str] | None`;
  `MemoryJob.to_dict() -> dict`.
  Эндпоинты — таблица §7.

- [ ] **Step 1: тесты (падают)** — дописать в `tests/test_memory_service.py`:

```python
async def _run_rebuild(sid, **kw):
    from backend import main, memory_service
    job = await memory_service.start_job(sid, main._memory_deps(), mode=kw.pop("mode", "rebuild"), **kw)
    await memory_service.wait_job(sid)
    return job


async def test_rebuild_keeps_old_snapshot_until_atomic_swap():
    from backend import models
    from backend.database import AsyncSessionLocal, engine
    await _fresh_db()
    _, sid, ids = await _make_chat(40 + hr.DEFAULT_WINDOW)
    async with AsyncSessionLocal() as db:
        db.add(models.HoraeEntry(session_id=sid, category="summary", title="t",
                                 content="СТАРЫЙ СНИМОК", always_on=True, enabled=True,
                                 meta={"last_message_id": ids[39], "v": 2}))
        await db.commit()
    during = []

    async def fake_complete(messages, params=None, connection=None, kind="service"):
        during.append((await _entry(sid)).content)
        return await _snapshot_llm([])(messages, params, connection, kind)

    with patch("backend.main.complete", new=fake_complete):
        job = await _run_rebuild(sid, batch_size=10)
    assert job.status == "done" and job.processed == 40
    assert all(c == "СТАРЫЙ СНИМОК" for c in during)       # до подмены работает старый
    entry = await _entry(sid)
    assert "СТАРЫЙ СНИМОК" not in entry.content and "rebuild" not in entry.meta
    assert entry.meta["updated_by"] == "rebuild" and entry.meta["last_message_id"] == ids[39]
    assert "[Обработано 40/40 сообщений" in job.line
    await engine.dispose()


async def test_rebuild_resumes_from_checkpoint():
    from backend import models
    from backend.database import AsyncSessionLocal, engine
    await _fresh_db()
    _, sid, ids = await _make_chat(30 + hr.DEFAULT_WINDOW)
    async with AsyncSessionLocal() as db:
        db.add(models.HoraeEntry(
            session_id=sid, category="summary", title="t", content="СТАРЫЙ", always_on=True,
            enabled=True, meta={"last_message_id": ids[29], "v": 2, "rebuild": {
                "content": hm.render_snapshot({hm.SEC_CHRONICLE: "- [#a–#b] уже сделано"}),
                "last_message_id": ids[19], "manual": True}}))
        await db.commit()
    seen = []
    with patch("backend.main.complete", new=_snapshot_llm(seen)):
        job = await _run_rebuild(sid, resume=True, batch_size=20)
    merges = [m for m in seen if m[0]["content"] == hm.MASTER_STATE_PROMPT]
    assert len(merges) == 1 and "уже сделано" in merges[0][1]["content"]
    assert f"#{ids[20]}–#{ids[29]}" in merges[0][1]["content"]
    assert job.processed == 10
    await engine.dispose()


async def test_catchup_folds_backlog_into_live_snapshot():
    from backend.database import engine
    await _fresh_db()
    _, sid, ids = await _make_chat(45 + hr.DEFAULT_WINDOW)
    with patch("backend.main.complete", new=_snapshot_llm([])):
        job = await _run_rebuild(sid, mode="catchup", batch_size=20)
    assert job.status == "done" and job.processed == 45 and job.batches == 3
    assert (await _entry(sid)).meta["last_message_id"] == ids[44]
    await engine.dispose()


async def test_api_error_marks_job_failed_and_keeps_pointer():
    from backend.database import engine
    await _fresh_db()
    _, sid, _ = await _make_chat(30 + hr.DEFAULT_WINDOW)

    class Denied(Exception):
        status_code = 401

    async def denied(messages, params=None, connection=None, kind="service"):
        raise Denied("no key")

    with patch("backend.main.complete", new=denied):
        job = await _run_rebuild(sid, mode="catchup")
    assert job.status == "error" and job.error
    assert await _entry(sid) is None
    await engine.dispose()


def test_memory_api_status_conflict_purge_export(client):
    from backend import main, memory_service, models
    from backend.database import AsyncSessionLocal
    cid = client.post("/api/characters", json={"name": "Хранитель"}).json()["id"]
    sid = client.post(f"/api/sessions?character_id={cid}").json()["session_id"]

    async def seed():
        async with AsyncSessionLocal() as db:
            for i in range(80):
                db.add(models.Message(session_id=sid, role="user" if i % 2 == 0 else "assistant",
                                      content=f"реплика {i}"))
            await db.commit()
    client.portal.call(seed)

    st = client.get(f"/api/sessions/{sid}/memory").json()
    assert st["snapshot"]["exists"] is False and st["backlog"]["pending"] == 80 - hr.DEFAULT_WINDOW
    assert st["backlog"]["window"] == hr.DEFAULT_WINDOW and st["job"] is None
    assert client.get(f"/api/sessions/{sid}/memory/export").status_code == 404

    with patch("backend.main.complete", new=_snapshot_llm([])):
        r = client.post(f"/api/sessions/{sid}/memory/rebuild", json={"mode": "rebuild", "batch_size": 10})
        assert r.status_code == 202
        client.portal.call(memory_service.wait_job, sid)
    st = client.get(f"/api/sessions/{sid}/memory").json()
    assert st["job"]["status"] == "done" and st["snapshot"]["exists"] and st["snapshot"]["schema"] == "hms-1"

    exp = client.get(f"/api/sessions/{sid}/memory/export?facts=1")
    assert exp.status_code == 200 and exp.headers["content-type"].startswith("text/markdown")
    assert "attachment" in exp.headers["content-disposition"]
    assert exp.text.startswith("# Мастер-снимок памяти") and f"## [{hm.SEC_CHRONICLE}]" in exp.text

    async def add_fact():
        async with AsyncSessionLocal() as db:
            db.add(models.HoraeFact(session_id=sid, content="факт", source_message_id=1))
            await db.commit()
    client.portal.call(add_fact)
    gone = client.delete(f"/api/sessions/{sid}/memory").json()
    assert gone["snapshot_deleted"] is True and gone["facts_deleted"] >= 1
    st = client.get(f"/api/sessions/{sid}/memory").json()
    assert st["snapshot"]["exists"] is False and st["facts"]["count"] == 0
    assert st["backlog"]["messages_total"] == 80                # сообщения на месте


def test_second_job_is_rejected_with_409(client):
    import asyncio
    from backend import memory_service
    cid = client.post("/api/characters", json={"name": "Очередь"}).json()["id"]
    sid = client.post(f"/api/sessions?character_id={cid}").json()["session_id"]
    lock = memory_service.session_lock(sid)

    async def hold():
        await lock.acquire()
    client.portal.call(hold)
    try:
        assert client.post(f"/api/sessions/{sid}/memory/rebuild", json={"mode": "catchup"}).status_code == 202
        assert client.post(f"/api/sessions/{sid}/memory/rebuild", json={"mode": "catchup"}).status_code == 409
        assert client.get(f"/api/sessions/{sid}/memory").json()["job"]["status"] == "queued"
        assert client.post(f"/api/sessions/{sid}/memory/cancel").json()["ok"] is True
    finally:
        async def release():
            lock.release()  # asyncio.Lock отпускаем в его же цикле событий
        client.portal.call(release)
        client.portal.call(memory_service.wait_job, sid)
    assert memory_service.get_job(sid).status == "cancelled"


def test_memory_endpoints_respect_access(client):
    """Чужой чат в режиме аккаунтов — 403 на всех эндпоинтах памяти."""
    a = client.post("/api/auth/register", json={"username": "hms_a", "password": "pw"}).json()
    b = client.post("/api/auth/register", json={"username": "hms_b", "password": "pw"}).json()
    client.put("/api/admin/security", json={"accounts_enabled": True}, headers={"X-User-Token": a["token"]})
    try:
        ha, hb = {"X-User-Token": a["token"]}, {"X-User-Token": b["token"]}
        cid = client.post("/api/characters", json={"name": "Чужой"}, headers=ha).json()["id"]
        sid = client.post(f"/api/sessions?character_id={cid}", headers=ha).json()["session_id"]
        for method, path in (("get", ""), ("post", "/rebuild"), ("post", "/cancel"),
                             ("delete", ""), ("get", "/export")):
            kw = {"json": {"mode": "catchup"}} if path == "/rebuild" else {}
            r = getattr(client, method)(f"/api/sessions/{sid}/memory{path}", headers=hb, **kw)
            assert r.status_code == 403, (method, path, r.status_code)
    finally:
        client.put("/api/admin/security", json={"accounts_enabled": False}, headers={"X-User-Token": a["token"]})
```

(Регистрацию/режим аккаунтов сверить с `tests/test_accounts.py:17-31` и повторить
его точный порядок вызовов; если первый зарегистрированный — админ, второй —
обычный пользователь, это и нужно.)

- [ ] **Step 2:** прогон → FAIL.
- [ ] **Step 3: реализация** по §6.5–6.6 и §7.
  - `start_job`: `JobConflict`, если у чата есть задание в `queued/running`.
    Задание создаётся в `queued`, запускается `asyncio.create_task` (держать ссылку
    в `MemoryJob.task`), внутри — `async with session_lock(sid)`: `running`; для
    `rebuild` без `resume` — инициализация буфера (§6.5); цикл
    `scan_and_compress_history(DbBatchSource(mode, threshold=1), cancel=job.cancel,
    on_progress=<обновляет поля job и job.line>, retry_conflicts=True)`; успешный
    `rebuild` → `adopt_summary(entry, буфер, указатель, updated_by="rebuild")`.
    Исключения: `MemoryLLMError` → `error = e.message`; `SnapshotValidationError` →
    «модель вернула снимок не по схеме: …»; `SourceConflictError` → «переписка
    менялась во время сжатия, повторите»; прочее → лог + «внутренняя ошибка: …».
    Отмена во время ожидания блокировки → `cancelled` без запуска цикла.
    `finished_at` — ISO UTC.
  - Если чат удалили во время задания — задание завершается `error` без падения.
  - `status`: поля §6.6; `pending` считается запросом (без LLM) тем же способом,
    что `DbBatchSource.pending` (цель — буфер, если идёт пересборка).
  - `purge`: `cancel_job` + `wait_job`, затем `DELETE HoraeEntry(category="summary",
    session_id)` и `DELETE HoraeFact(session_id)`; коммит.
  - `export_markdown`: §6.6; имя файла `memory-<slug>-<id>.md`, slug — буквы/цифры
    (латиница и кириллица), остальное → `_`; в заголовке — RFC 5987
    `filename*=UTF-8''…` + ASCII-запасной `filename=memory-<id>.md`.
  - Эндпоинты (`main.py`): `_can_access_session` → 403; нет чата → 404
    (`_can_access_session(None)` даёт False — проверять существование ДО доступа,
    чтобы 404 отличался от 403); тело rebuild — pydantic-модель в `schemas.py`
    `MemoryRebuildIn(mode: Literal["rebuild","catchup"]="rebuild", resume: bool=False,
    batch_size: int | None = Field(None, ge=1, le=200), delay_ms: int | None = Field(None, ge=0, le=60000))`;
    `JobConflict` → 409 «Память этого чата уже пересобирается»; ответ 202
    `{"job": job.to_dict()}`. Экспорт — `Response(text, media_type="text/markdown; charset=utf-8", headers=...)`.
- [ ] **Step 4:** все тесты памяти (+ `tests/test_accounts.py`) → PASS.
- [ ] **Step 5: коммит** `feat(memory): rebuild/catch-up jobs, status, purge, markdown export API`.

---

### Задача 7: Интерфейс — блок «Мастер-память», монитор токенов, миграции

**Files:**
- Modify: `frontend/app.js` — `data()` (~186-199, 307-316), `loadUiPrefs` (~3324-3362),
  `saveUiPrefs` (~3415-3433), методы памяти (~3476-3505), наблюдатель `sessionId`
  (~4043), шаблон вкладки «Память» (~5306-5372), инспектор (~5838-5943),
  `overlayOpen` не трогать (новых модалок нет).
- Modify: `frontend/styles.css` — только если нужен класс, которого нет (переиспользовать
  `.card`, `.row`, `.row-between`, `.muted`, `.tag`, `.upload-track/.upload-fill`,
  `.ins-bar`, `.seg-*`, `.btn-primary`, `.btn-danger`, `.btn-ghost`, `.field-hint`).

**Interfaces:**
- Consumes: API §7 (`GET/POST/DELETE /sessions/{id}/memory*`), `ctxStats.tiers`.
- Produces: `data`: `memoryBatch: 20`, `memoryDelayMs: 1500`, `memorySnapshotTokens: 12000`,
  `memoryWindow: 50`, `memStatus: null`, `memBusy: false`; non-reactive `_memTimer`;
  методы `loadMemStatus()`, `startMemJob(mode, resume=false)`, `cancelMemJob()`,
  `purgeMemory()`, `exportMemory()`, `_pollMem()`, `_stopMemPoll()`,
  вычисляемые `memTiers`, `memJobActive`.

- [ ] **Step 1: настройки и миграции.**
  - `data()`: `memoryWindow: 50` (комментарий: «совпадает с DEFAULT_WINDOW сервера»),
    новые поля выше.
  - `loadUiPrefs`: читать `memory_batch`, `memory_delay_ms`, `memory_snapshot_tokens`;
    миграция окна: `if (ui && ui.memory_defaults_v !== 2) { if (ui.memory_window == null || Number(ui.memory_window) === 20) this.memoryWindow = 50; this._memMigrate = true; }`;
    миграция бюджета: существующая строка (1M → 200k) выполняется ТОЛЬКО если
    `ui.ctx_budget_v !== 2`, после чего флаг ставится. Если хоть одна миграция
    сработала — `saveUiPrefs()`.
  - `saveUiPrefs`: добавить `memory_batch`, `memory_delay_ms`,
    `memory_snapshot_tokens`, `memory_defaults_v: 2`, `ctx_budget_v: 2`.
  - Пресеты окна: `[[20,'20'],[50,'50'],[80,'80'],[150,'150'],[0,'вся история']]`.
- [ ] **Step 2: методы.**
  - `loadMemStatus()` — `GET /sessions/{sessionId}/memory` → `memStatus`; если
    `memJobActive` — запустить `_pollMem` (setTimeout 1500 мс, `_memTimer`); при
    переходе `running/queued → done/error/cancelled` — тост
    (`done`: «🧠 Память пересобрана: N сообщений → T токенов»; `error`: «⚠ Память:
    <error>»; `cancelled`: «Пересборка остановлена, готовое сохранено»),
    `loadCtxStats()`, `loadHorae()`.
  - `startMemJob(mode, resume)` — для `rebuild` без `resume` `askConfirm` с текстом:
    «Пересобрать память с нуля? Примерно K пакетов = K платных запросов к модели
    сводки (плюс факты). Старый снимок работает до конца пересборки.», где
    `K = Math.ceil(messages_total_старше_окна / memoryBatch)`; POST
    `{mode, resume, batch_size: memoryBatch, delay_ms: memoryDelayMs}`; 409 → тост
    «Уже идёт»; затем `loadMemStatus()`.
  - `cancelMemJob()` — POST cancel. `purgeMemory()` — `askConfirm("Сбросить память
    чата? Сотрутся мастер-снимок, буфер пересборки и атомарные факты. Сообщения
    останутся.", {okText: "Сбросить"})` → DELETE → тост → `loadMemStatus`,
    `loadCtxStats`, `loadHorae`.
  - `exportMemory()` — raw `fetch("/api/sessions/"+id+"/memory/export?facts=1", {headers: this.authHeaders()})`
    → `res.blob()` → `downloadBlob(blob, name)`; имя из `Content-Disposition`
    (`filename*=UTF-8''` декодировать), иначе `memory-<id>.md`; 404 → тост «Снимка ещё нет».
  - `sessionId`-наблюдатель: `_stopMemPoll()`, `memStatus = null`, затем
    `loadMemStatus()`, если открыта вкладка «Память»; открытие вкладки «Память» тоже
    зовёт `loadMemStatus()`.
  - Сохранение трёх новых полей — `@change` → клэмп (`numFromInput`) → `saveUiPrefs()`.
- [ ] **Step 3: шаблон вкладки «Память»** — в начало панели (до «Авто-сводки»),
  только при `sessionId`:

```html
<section class="card mem-master" aria-labelledby="mem-master-h">
  <div class="row-between"><h4 id="mem-master-h">🧠 Мастер-память этого чата</h4>
    <button class="btn-icon" @click="loadMemStatus" aria-label="Обновить статус памяти">↻</button></div>
  <p class="muted" v-if="memStatus && memStatus.snapshot.exists">
    Снимок {{ fmtNum(memStatus.snapshot.tokens) }} / {{ fmtNum(memStatus.snapshot.budget) }} ток.
    · учтено до #{{ memStatus.snapshot.covered_upto }}
    · ждут сжатия {{ memStatus.backlog.pending }}
    <span v-if="!memStatus.snapshot.structured" class="tag">старая схема — пересоберите</span>
    <span v-if="memStatus.snapshot.over_budget" class="tag">больше бюджета</span></p>
  <p class="muted" v-else-if="memStatus">Снимка нет: окно пропускает историю целиком.
    Ждут сжатия {{ memStatus.backlog.pending }}.</p>
  <p class="muted" v-if="memStatus && memStatus.staging && !memJobActive">
    Пересборка прервана на #{{ memStatus.staging.last_message_id }}.
    <button class="btn-primary" @click="startMemJob('rebuild', true)">Продолжить</button></p>
  <div v-if="memJobActive" class="mem-progress" role="status" aria-live="polite">
    <span class="upload-track"><span class="upload-fill"
      :style="{ width: (memStatus.job.total ? Math.round(memStatus.job.processed / memStatus.job.total * 100) : 0) + '%' }"></span></span>
    <div class="muted">{{ memStatus.job.line || 'В очереди…' }}</div>
    <div class="muted" v-if="memStatus.job.phase === 'retry'">Сбой API — повтор через {{ memStatus.job.retry_in_s }} с</div>
    <div class="muted" v-else-if="memStatus.job.phase === 'wait'">Пауза между запросами…</div>
    <button class="btn-danger" @click="cancelMemJob">Остановить</button>
  </div>
  <p v-if="memStatus && memStatus.job && memStatus.job.status === 'error'" class="danger-text">⚠ {{ memStatus.job.error }}</p>
  <div class="row mem-actions">
    <button class="btn-primary" :disabled="memJobActive" @click="startMemJob('rebuild')">Пересобрать с нуля</button>
    <button :disabled="memJobActive || !memStatus || !memStatus.backlog.pending" @click="startMemJob('catchup')">Догнать</button>
    <button :disabled="!memStatus || !memStatus.snapshot.exists" @click="exportMemory">Экспорт .md</button>
    <button class="btn-danger" :disabled="memJobActive" @click="purgeMemory">Сбросить память</button>
  </div>
  <!-- три поля: memoryBatch (1–200), memoryDelayMs (0–60000), memorySnapshotTokens (1000–200000) -->
  <!-- монитор токенов: ins-bar с seg-guides / seg-horae / seg-history по ctxStats.tiers + две строки процентов -->
</section>
```

  Дописать три поля (label + `<input type="number">` + `.field-hint`) и монитор:
  полоса `.ins-bar` из трёх `<i>` (`seg-guides`, `seg-horae`, `seg-history`, ширина —
  доля от `tiers.total`), подписи «Системный промпт и якоря — N», «Мастер-снимок и
  факты — N», «Окно (K сообщений) — N», строки «из бюджета хода B (P%)» и «из лимита
  модели 1 000 000 (Q%)». Если `ctxStats` нет — «Откройте чат, чтобы увидеть бюджет».
  Та же полоса — в инспекторе под `.ins-total`, если `ctxStats.tiers` есть.
  `fmtNum` — если помощника нет, добавить (разделитель тысяч — `toLocaleString("ru-RU")`).
- [ ] **Step 4: проверка.** `node --check frontend/app.js`; затем в браузере
  (preview): вкладка «Память» с открытым чатом — статус, кнопки, поля, монитор;
  ширина 375 px без горизонтальной прокрутки; тёмная и светлая тема.
- [ ] **Step 5: коммит** `feat(ui): master memory panel, token budget monitor, one-time setting migrations`.

---

### Задача 8: Документация и журнал изменений

**Files:**
- Modify: `docs/HORAE.md` — новый раздел «Мастер-снимок и пакетная пересборка (2.5.0)»:
  схема 4 разделов, свёртка, пакеты/пауза/повторы, страж, арки, команды, статус,
  монитор, окно 50, настройки; обновить «Три слоя» (окно 50, снимок без обрезки 6000);
  «Известные ограничения» — актуализировать.
- Modify: `docs/API.md` — в «Память Horae» строки таблицы §7 + `GET /api/sessions/{id}/context` (с `tiers`).
- Modify: `docs/BACKEND.md` — абзацы про `hierarchical_memory.py` и `memory_service.py`.
- Modify: `CHANGELOG.md` — `## [2.5.0] — 2026-09-26`: «Добавлено — иерархическая
  пакетная память» (проблема → что теперь), «Исправлено — бюджет 1M сбрасывался на
  200k при каждой загрузке», «Исправлено — снимок памяти обрезался до 6000 символов»,
  «Изменено — окно по умолчанию 50», `### Обновление` (перезапуск; новые `.env`
  `MEMORY_*`; `summary_model` должна выдавать ≥ 18k токенов вывода; пересборка
  платная — оценка числа запросов).
- [ ] **Step 1–3:** написать, сверить имена с кодом (`grep`), коммит
  `docs: hierarchical memory 2.5.0`.

---

## Самопроверка плана

- Спека §4.1–4.10 → Задачи 2–3; §5 → Задача 2; §6.1–6.4 → Задачи 1, 5; §6.5–6.6, §7 API
  → Задача 6; §7 tiers и рендер → Задача 4; §8 настройки → Задачи 1, 7; §9 UI → Задача 7;
  §10 совместимость → Задачи 4–5; §11 тесты → Задачи 2–6; документация → Задача 8.
- Имена сквозные: `render_snapshot`, `parse_sections`, `is_structured`, `ENVELOPE_*`,
  `MASTER_STATE_PROMPT`, `COMPACT_PROMPT`, `sampling_overrides`, `_SAMPLING_OVERRIDES`,
  `MemoryDeps`, `_memory_deps`, `start_job`, `wait_job`, `session_lock`, `status`,
  `purge`, `export_markdown`.
