"""
Ядро иерархической памяти (backend/hierarchical_memory.py) без БД и сети.

LLM здесь — подставная корутина, sleep и часы — фиктивные: проверяем контракт
свёртки State_N = merge(State_{N-1}, Block_N), паузы, повторы, валидацию схемы и
стража записей, из-за которых раньше тонули списки и атрибуты.
"""
import asyncio
import base64
import random
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


# Хроника длиннее keep_recent_chronicle (12) — есть что сжимать в арки.
_LONG_CHRON = "\n".join(f"- [#{i}–#{i}] событие {i}" for i in range(1, 80))
_ARC = "- [#1–#79] Арка «Дорога»: всё важное"


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


def test_normalize_keeps_code_and_text_runs():
    # Код не трогаем (`vector<int>` — не тег), а длинная строчная «растяжка» — текст,
    # не base64: у настоящего бинаря в 200 символах всегда есть заглавные/цифры.
    line = hm.normalize_message(_msg(1, "`vector<int>` <b>жирный</b> Nooo" + "o" * 300))
    assert "`vector<int>`" in line and "<b>" not in line and "o" * 300 in line


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


def test_skipped_messages_are_consumed_so_the_pointer_passes_them():
    b = hm.plan_batch([_msg(1, " "), _msg(2, "текст"), _msg(3, "")], batch_size=20, max_chars=10**6)
    assert (b.first_id, b.last_id, len(b.messages)) == (1, 3, 3)
    assert b.transcript.startswith("[#2 ") and "[#1 " not in b.transcript
    only_empty = hm.plan_batch([_msg(4, "")], batch_size=20, max_chars=10**6)
    assert only_empty.last_id == 4 and only_empty.transcript == ""


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


def test_extract_strips_thoughts_and_fences_and_returns_canonical_text():
    raw = "<think>план</think>```markdown\n" + _wrap(_snap().replace("## [", "### [")) + "\n```"
    assert hm.extract_snapshot(raw) == (_snap().replace("## [", "### ["), False)
    text, problems = hm.validate_snapshot(raw, None)
    assert problems == [] and text == _snap()


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


def test_guard_recreates_a_lost_sublist():
    prev = _snap(lists="### Треки\n- «Lacrimosa» — утрата (#2)\n### Книги\n- «Дюна» — книга (#3)")
    new = _snap(lists="### Треки\n- «Lacrimosa» — утрата (#2)")
    fixed, restored = hm.guard_entries(prev, new)
    assert restored == ["дюна"]
    assert hm.parse_sections(fixed)[hm.SEC_LISTS].endswith("### Книги\n- «Дюна» — книга (#3)")


def test_guard_keeps_unindented_attribute_with_its_entry():
    # Модель пишет атрибут персонажа отдельной строкой без отступа. Раньше такая
    # строка сама была записью с ключом «здоровье», совпадала с атрибутом другого
    # персонажа — и при потере персонажа возвращалась только его первая строка.
    prev = _snap(chars="- Эльвира — цела\nздоровье: цела\n- Артур — ранен\nздоровье: ранен в плечо")
    new = _snap(chars="- Эльвира — цела\nздоровье: цела")
    fixed, restored = hm.guard_entries(prev, new)
    assert restored == ["артур"]
    assert hm.parse_sections(fixed)[hm.SEC_CHARACTERS].endswith(
        "- Артур — ранен\nздоровье: ранен в плечо")


def test_guard_does_not_tear_off_merged_continuation():
    # Модель слила продолжение с первой строкой записи: запись та же, возвращать
    # нечего. Раньше продолжение считалось своей записью и дописывалось в конец
    # раздела оторванным фрагментом.
    prev = _snap(chars="- Эльвира — здоровье: цела\nпсихологический вектор: доверяет Артуру\n"
                       "- Артур — ранен")
    new = _snap(chars="- Эльвира — здоровье: цела; психологический вектор: доверяет Артуру\n"
                      "- Артур — ранен")
    fixed, restored = hm.guard_entries(prev, new)
    assert restored == [] and fixed.count("психологический вектор") == 1


def test_guard_keeps_nested_list_under_bold_name_before_any_marker():
    # «Жирное имя + вложенный список»: у строк группы верхнего маркера нет вовсе.
    # Строка с отступом до первого маркера — всё равно продолжение: иначе
    # «  - здоровье: …» становится записью с ключом «здоровье», совпадает с
    # атрибутом другого персонажа, и при потере персонажа атрибут пропадает.
    prev = _snap(chars="**Эльвира**\n  - здоровье: цела\n**Артур**\n  - здоровье: ранен в плечо")
    new = _snap(chars="**Эльвира**\n  - здоровье: цела")
    fixed, restored = hm.guard_entries(prev, new)
    assert restored == ["артур"]
    assert hm.parse_sections(fixed)[hm.SEC_CHARACTERS].endswith(
        "**Артур**\n  - здоровье: ранен в плечо")


def test_guard_splits_markerless_section_by_line():
    # Без единого маркера делить не по чему — каждая строка остаётся записью.
    prev = _snap(chars="Эльвира: цела\nАртур: ранен")
    new = _snap(chars="Эльвира: цела")
    fixed, restored = hm.guard_entries(prev, new)
    assert restored == ["артур"]
    assert hm.parse_sections(fixed)[hm.SEC_CHARACTERS].endswith("Артур: ранен")


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
    # Менеджер подставляет плейсхолдеры через str.format — лишних скобок быть не должно.
    compact = hm.COMPACT_PROMPT.format(budget="12 000", keep=12)
    assert "12 000" in compact and "{" not in compact


# ==================== Менеджер: вызов, слияние, сжатие, свёртка ====================

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


def _echo_llm(record, **sections):
    """Возвращает валидный снимок, чья хроника — диапазон пакета из запроса;
    прочие разделы — из sections (аргументы _snap)."""
    async def llm(messages):
        record.append(messages)
        user = messages[1]["content"]
        rng = user.split("[Новые события ", 1)[1].split("]", 1)[0]
        prev = user.split("[Текущая память]\n", 1)[1].split("\n\n[Новые события", 1)[0]
        chron = "" if prev == hm.EMPTY_STATE else hm.parse_sections(prev)[hm.SEC_CHRONICLE] + "\n"
        return _wrap(_snap(chron=chron + f"- [{rng}] пакет", **sections))
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
        if messages[0]["content"] != hm.MASTER_STATE_PROMPT:
            # Сжатие — арка до последней записи: ответ без свежих записей
            # хроники теперь отвергается (нижняя граница, задача 11).
            return _wrap(_snap(chron=_ARC))
        return _wrap(_snap())

    m, _ = _mgr(llm)
    batch = hm.plan_batch([_msg(1)], batch_size=20, max_chars=10**6)
    # Хроника длинная: при короткой сжимать нечего, и модель не зовут вовсе.
    await m.merge_block(_snap(chron=_LONG_CHRON), batch)
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


async def test_empty_batch_needs_no_model_call():
    # Пакет из одних пустых сообщений: вливать нечего, но указатель обязан их пройти.
    async def llm(messages):
        raise AssertionError("модель не должна вызываться")

    m, _ = _mgr(llm)
    res = await m.scan_and_compress_history([_msg(1, " "), _msg(2, "")], _snap())
    assert res.status == "done" and res.processed == 2 and res.state == _snap()


async def test_retry_wait_is_never_shorter_than_the_delay():
    # Повтор — тоже запрос: бэкофф 2 с не должен обходить паузу 5 с.
    attempts = []

    async def flaky(messages):
        attempts.append(1)
        if len(attempts) == 1:
            raise _Err(503)
        return _wrap(_snap())

    m, sleeps = _mgr(flaky, delay_ms=5000)
    await m.call([{"role": "user", "content": "x"}])
    assert sleeps == [5.0]


def test_classify_prefers_the_most_specific_class_name():
    # Ядро не импортирует litellm: признак — имя класса в MRO, и самый точный класс
    # важнее и родителя, и status_code (у litellm.APIConnectionError он 500).
    class APIConnectionError(Exception):
        status_code = 500

    class Timeout(APIConnectionError):
        pass

    class BadRequestError(Exception):
        status_code = 400

    class ContentPolicyViolationError(BadRequestError):
        pass

    kinds = [hm.classify_error(e).kind for e in (
        APIConnectionError("c"), Timeout("t"), ContentPolicyViolationError("p"), BadRequestError("b"))]
    assert kinds == ["network", "timeout", "blocked", "bad_request"]


async def test_failed_compaction_keeps_snapshot_and_warns_about_budget():
    long_chron = "\n".join(f"- [#{i}–#{i}] событие {i}" for i in range(1, 80))

    async def llm(messages):
        if messages[0]["content"] == hm.MASTER_STATE_PROMPT:
            return _wrap(_snap(chron=long_chron))
        return "ерунда"  # сжатие так и не дало снимка по схеме

    m, _ = _mgr(llm, snapshot_tokens=600)
    batch = hm.plan_batch([_msg(80)], batch_size=20, max_chars=10**6)
    state = await m.merge_block("", batch)
    assert "событие 79" in state
    assert any(w.startswith("снимок превышает бюджет:") for w in m.warnings)


# ==================== Доработки по ревью (задача 3b) ====================

def _nbsp_int(n):
    return f"{n:,}".replace(",", "\u00a0")


async def test_compaction_is_skipped_when_only_guarded_sections_are_over_budget():
    # Бюджет превышают списки, а хроника короче keep_recent_chronicle: сокращать
    # нечего. Раньше модель на КАЖДОМ пакете получала запрос сжатия, отвечала «не
    # короче» — вызовов вдвое больше, и на каждый пакет по паре предупреждений.
    big_list = "### Треки\n" + "\n".join(f"- «Трек {i}» — тема (#{i})" for i in range(1, 40))
    systems = []
    echo = _echo_llm([], lists=big_list)

    async def llm(messages):
        systems.append(messages[0]["content"])
        if messages[0]["content"] != hm.MASTER_STATE_PROMPT:
            return _wrap(messages[1]["content"].split("\n", 1)[1])
        return await echo(messages)

    m, _ = _mgr(llm, batch_size=1, snapshot_tokens=300)
    res = await m.scan_and_compress_history([_msg(i) for i in range(1, 11)])
    assert res.batches == 10
    assert systems == [hm.MASTER_STATE_PROMPT] * 10  # ни одного COMPACT_PROMPT
    # Одно предупреждение о бюджете на прогон — с числами последнего пакета.
    assert res.warnings == [
        f"снимок превышает бюджет: {_nbsp_int(len(res.state))} из 300 токенов "
        "(хроника уже сжата; реестр и списки не сжимаются автоматически)"]


def _over_budget(tokens, budget):
    """Предупреждение о бюджете, когда хронику сворачивать уже нечего."""
    return (f"снимок превышает бюджет: {_nbsp_int(tokens)} из {_nbsp_int(budget)} токенов "
            "(хроника уже сжата; реестр и списки не сжимаются автоматически)")


_BIG_LIST = "### Треки\n" + "\n".join(f"- «Трек {i}» — тема (#{i})" for i in range(1, 40))


async def test_compaction_with_arcs_waits_until_entries_pile_up():
    # Длинный чат: хроника уже «арка + 12 последних», бюджет держат списки.
    # Арка — тоже запись списка, и пропуск «записей ≤ keep» тут не срабатывал
    # никогда, а каждый пакет добавляет запись: сжатие (полный снимок на входе и
    # на выходе) снова шло на КАЖДОМ пакете. Теперь модель зовут, только когда
    # сверх keep набралось max(2, keep // 2) = 6 записей, которые ещё не арки.
    arc = "- [#1–#50] Арка «Дорога»: всё важное"
    recent = [f"- [#{i}–#{i}] событие {i}" for i in range(51, 63)]
    systems = []
    echo = _echo_llm([], lists=_BIG_LIST)

    async def llm(messages):
        if messages[0]["content"] == hm.MASTER_STATE_PROMPT:
            systems.append("merge")
            return await echo(messages)
        systems.append("compact")
        chron = hm.parse_sections(messages[1]["content"].split("\n", 1)[1])[hm.SEC_CHRONICLE]
        # Как велит COMPACT_PROMPT: всё старше последних 12 записей — в арку.
        return _wrap(_snap(chron="\n".join([arc, *chron.splitlines()[-12:]]), lists=_BIG_LIST))

    m, _ = _mgr(llm, batch_size=1, snapshot_tokens=600)
    res = await m.scan_and_compress_history([_msg(i) for i in range(100, 110)],
                                            _snap(chron="\n".join([arc, *recent]), lists=_BIG_LIST))
    assert res.batches == 10
    assert systems.count("compact") <= 10 // 6
    # Сжатие — на 6-м пакете, когда сверх keep набралось 6 записей; потом снова копятся.
    assert systems == ["merge"] * 6 + ["compact"] + ["merge"] * 4
    assert res.warnings == [_over_budget(len(res.state), 600)]


async def test_arcs_in_any_format_are_not_entries_to_fold():
    # Арки модель пишет по-разному: жирным, строчными, с диапазоном в конце.
    # Уже свёрнутое — не повод звать модель: семь арок сверх keep ничего не меняют.
    arcs = ["- [#1–#10] Арка «Дорога»: суть",
            "- [#11–#20] **Арка «Лес»**: суть",
            "- **[#21–#30] Арка «Башня»:** суть",
            "* [#31–#40] арка «Мост» — суть",
            "1. Арка «Море» (#41–#50): суть",
            "- [#51–#60] АРКА «Горы»: суть",
            "- [#61–#70] Арки «Долина» и «Река»: суть"]
    recent = [f"- [#{i}–#{i}] событие {i}" for i in range(71, 83)]
    calls = []

    async def llm(messages):
        calls.append(messages)
        return _wrap(_snap(chron=_ARC))

    m, _ = _mgr(llm, snapshot_tokens=100)
    state = _snap(chron="\n".join(arcs + recent))
    assert await m.compact(state) == state
    assert calls == []
    assert m.warnings == [_over_budget(len(state), 100)]


@pytest.mark.parametrize("extra, called", [(5, False), (6, True)])
async def test_compaction_waits_for_enough_entries_beyond_keep(extra, called):
    chron = "\n".join(f"- [#{i}–#{i}] событие {i}" for i in range(1, 13 + extra))  # 12 + extra
    calls = []

    async def llm(messages):
        calls.append(messages[0]["content"])
        return _wrap(_snap(chron=_ARC))

    m, _ = _mgr(llm, snapshot_tokens=100)
    await m.compact(_snap(chron=chron))
    assert len(calls) == int(called)


async def test_length_error_compacts_even_one_entry_beyond_keep():
    # После length гистерезис по записям не действует: без сжатия повтор почти
    # наверняка снова упрётся в лимит вывода — сворачивается хоть одна запись.
    calls = []

    async def llm(messages):
        calls.append(messages[0]["content"])
        if len(calls) == 1:
            raise RuntimeError("Технически: ПУСТОЙ ответ, finish_reason=LENGTH.")
        return _wrap(_snap(chron=_ARC))

    m, _ = _mgr(llm)
    chron = "\n".join(f"- [#{i}–#{i}] событие {i}" for i in range(1, 14))  # keep + 1
    await m.merge_block(_snap(chron=chron),
                        hm.plan_batch([_msg(14)], batch_size=20, max_chars=10**6))
    assert len(calls) == 3
    assert calls[0] == calls[2] == hm.MASTER_STATE_PROMPT
    assert calls[1].startswith(hm.COMPACT_PROMPT.split("{", 1)[0])


async def test_budget_warning_after_compaction_names_the_cause():
    # Хронику свернули, а снимок всё ещё сверх бюджета — держат его списки.
    # Голое «X из Y» не объясняло, почему и что с этим делать.
    async def llm(messages):
        return _wrap(_snap(chron=_ARC, lists=_BIG_LIST))

    m, _ = _mgr(llm, snapshot_tokens=300)
    out = await m.compact(_snap(chron=_LONG_CHRON, lists=_BIG_LIST))
    assert "Арка «Дорога»" in out
    assert m.warnings == [_over_budget(len(out), 300)]


async def test_compaction_aims_below_the_budget():
    # Гистерезис: цель сжатия — 80 % бюджета, иначе снимок, сжатый ровно до
    # бюджета, снова превысит его на следующем пакете, и сжатие пойдёт каждый раз.
    systems = []
    compacted = _snap(chron=_ARC)

    async def llm(messages):
        systems.append(messages[0]["content"])
        return _wrap(compacted)

    budget = len(compacted) + 10  # итог сжатия: выше цели 80 %, но в бюджете
    m, _ = _mgr(llm, snapshot_tokens=budget)
    out = await m.compact(_snap(chron=_LONG_CHRON))
    assert out == compacted
    assert systems == [hm.COMPACT_PROMPT.format(budget=int(budget * 0.8), keep=12)]
    assert m.warnings == []  # «превышает бюджет» — по полному бюджету, не по цели


async def test_repeated_warnings_are_kept_once_per_run():
    # Модель на каждом пакете роняет одну и ту же запись, а сжатие не сокращает
    # хронику: одинаковый текст — одно предупреждение за прогон, но в каждом прогоне.
    lists = "### Треки\n- «A» — тема (#1)"
    state = _snap(chron=_LONG_CHRON, lists=lists + "\n- «B» — тема (#2)")

    async def llm(messages):
        return _wrap(_snap(chron=_LONG_CHRON, lists=lists))  # «B» потерян, хроника та же

    m, _ = _mgr(llm, batch_size=1, snapshot_tokens=600)
    expected = ["модель потеряла 1 запись — возвращены из предыдущего снимка",
                "сжатие не сократило хронику — оставлен прежний снимок",
                f"снимок превышает бюджет: {_nbsp_int(len(state))} из 600 токенов"]
    first = await m.scan_and_compress_history([_msg(i) for i in range(1, 4)], state)
    assert first.batches == 3 and first.warnings == expected
    second = await m.scan_and_compress_history([_msg(i) for i in range(4, 6)], first.state)
    assert second.warnings == expected
    assert m.warnings == expected * 2


@pytest.mark.parametrize("lost, words", [
    (1, "1 запись"), (2, "2 записи"), (4, "4 записи"), (5, "5 записей"), (11, "11 записей"),
    (12, "12 записей"), (14, "14 записей"), (21, "21 запись"), (22, "22 записи"),
])
async def test_restored_warning_agrees_with_the_number(lost, words):
    prev = _snap(lists="### Треки\n" + "\n".join(f"- т{i}" for i in range(1, lost + 1)))

    async def forgetful(messages):
        return _wrap(_snap(lists="—"))

    m, _ = _mgr(forgetful, shrink_min_tokens=10**6)
    await m.merge_block(prev, hm.plan_batch([_msg(100)], batch_size=20, max_chars=10**6))
    assert m.warnings == [f"модель потеряла {words} — возвращены из предыдущего снимка"]


async def test_memory_error_from_the_callback_still_counts_for_the_delay():
    # Сервис может сам классифицировать ошибку (например, length). Раньше такой
    # исход не двигал «конец предыдущего вызова», и следующий запрос уходил без паузы.
    err = hm.MemoryLLMError("length", "упёрлись в лимит", retryable=False)

    async def llm(messages):
        raise err

    m, sleeps = _mgr(llm, delay_ms=1000)
    for _ in range(2):
        with pytest.raises(hm.MemoryLLMError) as e:
            await m.call([{"role": "user", "content": "x"}])
        assert e.value is err
    assert sleeps == [1.0]


async def test_retryable_memory_error_from_the_callback_is_retried():
    attempts = []

    async def llm(messages):
        attempts.append(1)
        if len(attempts) == 1:
            raise hm.MemoryLLMError("server", "сбой", retryable=True, retry_after=5)
        return "ok"

    m, sleeps = _mgr(llm)
    assert await m.call([{"role": "user", "content": "x"}]) == "ok"
    assert len(attempts) == 2 and sleeps == [5.0]


def test_only_known_html_tags_are_stripped():
    # Угловые скобки в ролевом чате — не только HTML: OOC-ремарки и «x <y and z>»
    # раньше пропадали целиком вместе с «тегом».
    for text in ("<OOC: давай завтра продолжим>", "x <y and z> w", "<bold> и <b-side>",
                 "<i думаю, что он врёт>"):
        assert hm.normalize_message(_msg(1, text)).endswith(f"] {text}")
    for html, plain in (("<b>жирный</b>", "жирный"),
                        ("<SPAN Style=\"color:red\">красный</SPAN>", "красный"),
                        ("<font color=#ff0000 size=+1>цвет</font>", "цвет"),
                        ("<details open><summary>Итог</summary></details>", "Итог"),
                        ("<a href=https://x.test/?a=1&b=2>ссылка</a>", "ссылка"),
                        ("<img src='map.png' alt=\"карта\" /><H3>Глава</H3>", "Глава")):
        assert hm.normalize_message(_msg(1, html)).endswith(f"] {plain}")


def test_extract_takes_the_last_envelope():
    # Модель начала с «Вот снимок в формате <master_state>…</master_state>:» —
    # раньше бралось упоминание, и платный корректирующий ход уходил впустую.
    raw = f"Вот снимок в формате {hm.ENVELOPE_OPEN}…{hm.ENVELOPE_CLOSE}:\n" + _wrap(_snap())
    assert hm.extract_snapshot(raw) == (_snap(), False)


def test_export_counts_only_non_empty_facts():
    md = hm.render_export_markdown(
        title="t", character="c", snapshot=_snap(), covered_upto=1, messages_total=1,
        tokens=1, budget=1, schema="hms-1", exported_at="x", facts=["a", "", " "])
    assert "атомарные факты (1)" in md and md.rstrip().endswith("- a")


def test_header_survives_any_max_chars():
    text = "НАЧАЛО " + "x" * 5000 + " КОНЕЦ"
    tiny = hm.normalize_message(_msg(1, text), max_chars=30)
    assert tiny.startswith("[#1 · 2026-09-20 14:03 · Артур]")
    assert "середина длинного сообщения пропущена" in tiny
    small = hm.normalize_message(_msg(1, text), max_chars=200)
    assert small.startswith("[#1 · 2026-09-20 14:03 · Артур] НАЧАЛО")
    assert small.endswith("КОНЕЦ") and len(small) <= 200


def test_batch_chars_has_a_floor():
    assert hm.MemoryConfig(batch_max_chars=30).batch_max_chars == 2000
    assert hm.MemoryConfig(batch_max_chars=-5).batch_max_chars == 2000
    assert hm.MemoryConfig(batch_max_chars=5000).batch_max_chars == 5000


def test_window_helpers_coerce_settings_the_same_way():
    ids = list(range(1, 101))
    w = hm.SlidingWindow("мусор")  # не число → окно по умолчанию (50)
    assert w.pending_for_summary(ids, covered=None) == ids[:50]
    assert w.pending_for_summary(ids, covered="40") == list(range(41, 51))
    assert hm.window_start(ids, covered="100", window="мусор") == 48
    assert hm.window_start(ids, covered="мусор", window="50") == 0


async def test_parallel_calls_are_serialized_through_the_delay():
    # Задача 5 зовёт факты через call() из commit — «дверь» должна быть одна
    # и при одновременных вызовах: второй ждёт конца первого и паузу.
    order = []

    async def llm(messages):
        order.append(("start", messages[0]["content"]))
        await asyncio.sleep(0)  # отдать управление: второй call() успевает войти
        order.append(("end", messages[0]["content"]))
        return "ok"

    m, sleeps = _mgr(llm, delay_ms=1000)
    await asyncio.gather(m.call([{"role": "user", "content": "a"}]),
                         m.call([{"role": "user", "content": "b"}]))
    assert order == [("start", "a"), ("end", "a"), ("start", "b"), ("end", "b")]
    assert sleeps == [1.0]


async def test_progress_reports_retry_compact_and_wait_phases():
    attempts = []

    async def llm(messages):
        attempts.append(1)
        if len(attempts) == 1:
            raise _Err(503)
        if messages[0]["content"] == hm.MASTER_STATE_PROMPT:
            return _wrap(_snap(chron=_LONG_CHRON))
        return _wrap(_snap(chron=_ARC))

    progress = []
    m, _ = _mgr(llm, delay_ms=1000, snapshot_tokens=600)
    await m.scan_and_compress_history([_msg(80)], on_progress=progress.append)
    phases = [(p.phase, p.retry_in_s) for p in progress]
    # Повтор слияния (бэкофф 2 с не короче паузы 1 с) → сжатие → пауза перед ним.
    assert phases.index(("retry", 2.0)) < phases.index(("compact", None)) \
        < phases.index(("wait", 1.0)) < phases.index(("done", None))


async def test_model_request_is_never_reported_as_wait_or_retry():
    # Ревью задачи 10 (I1): «wait»/«retry» уходили перед сном, а после сна фаза
    # не возвращалась — и всё время платного запроса вкладка «Память» писала
    # «пауза между запросами» или «повтор через 2 с», а «сжатие хроники»
    # перетиралось паузой. В момент запроса к модели последнее событие обязано
    # называть работу этого запроса: слияние или сжатие, без обратного отсчёта.
    progress, seen = [], []

    async def llm(messages):
        kind = "merge" if messages[0]["content"] == hm.MASTER_STATE_PROMPT else "compact"
        seen.append((kind, progress[-1].phase, progress[-1].retry_in_s))
        if len(seen) == 1:
            raise _Err(503)
        return _wrap(_snap(chron=_LONG_CHRON if kind == "merge" else _ARC))

    m, sleeps = _mgr(llm, delay_ms=1000, snapshot_tokens=600, batch_size=1)
    await m.scan_and_compress_history([_msg(80), _msg(81)], on_progress=progress.append)
    # Сценарий действительно прошёл через повтор, паузы и сжатия.
    assert 2.0 in sleeps and 1.0 in sleeps
    assert [k for k, _, _ in seen].count("compact") >= 2
    assert [(phase, retry) for _, phase, retry in seen] == [(k, None) for k, _, _ in seen]


async def test_max_batches_counts_rejected_commits():
    calls = []
    m, _ = _mgr(_echo_llm(calls), batch_size=5)
    res = await m.scan_and_compress_history(_ConflictingSource([_msg(1), _msg(2)], 1),
                                            max_batches=1)
    assert (res.status, res.batches, res.processed, len(calls)) == ("limit", 0, 0, 1)


async def test_repeated_length_error_goes_up():
    systems = []

    async def llm(messages):
        systems.append(messages[0]["content"])
        raise RuntimeError("…\nТехнически: ПУСТОЙ ответ, finish_reason=LENGTH.")

    m, _ = _mgr(llm)
    batch = hm.plan_batch([_msg(80)], batch_size=20, max_chars=10**6)
    with pytest.raises(hm.MemoryLLMError) as e:
        await m.merge_block(_snap(chron=_LONG_CHRON), batch)
    assert e.value.kind == "length" and not e.value.retryable
    # слияние → сжатие (тоже length, снимок прежний) → повтор слияния → наверх
    assert len(systems) == 3 and systems[0] == systems[2] == hm.MASTER_STATE_PROMPT


async def test_compaction_that_does_not_shorten_keeps_the_snapshot():
    state = _snap(chron=_LONG_CHRON)

    async def llm(messages):
        return _wrap(_snap(chron=_LONG_CHRON + "\n- [#80–#80] ещё одно"))

    m, _ = _mgr(llm, snapshot_tokens=600)
    assert await m.compact(state) == state
    assert "сжатие не сократило хронику — оставлен прежний снимок" in m.warnings


async def test_compaction_keeps_entries_the_model_dropped():
    # Раньше выпавшую при сжатии запись возвращал страж (с предупреждением).
    # Теперь (задача 11) разделы 2–4 из ответа сжатия не берутся вовсе —
    # терять и возвращать нечего.
    state = _snap(chron=_LONG_CHRON, lists="### Треки\n- «A» — тема (#1)\n- «B» — тема (#2)")

    async def llm(messages):
        return _wrap(_snap(chron=_ARC, lists="### Треки\n- «A» — тема (#1)"))

    m, _ = _mgr(llm, snapshot_tokens=10**6)
    out = await m.compact(state)
    assert "Арка «Дорога»" in out and "- «B» — тема (#2)" in out
    assert m.warnings == []


async def test_retry_after_is_capped():
    # Суточная квота в Retry-After не должна подвешивать задание без движения.
    attempts = []

    async def llm(messages):
        attempts.append(1)
        if len(attempts) == 1:
            raise _Err(429, retry_after=86400)
        return "ok"

    m, sleeps = _mgr(llm)
    assert await m.call([{"role": "user", "content": "x"}]) == "ok"
    assert sleeps == [300.0]


def test_implausibly_long_and_empty_answers_are_problems():
    huge = _wrap(_snap(chron="- " + "событие " * 30_000))
    _, problems = hm.validate_snapshot(huge, None)
    assert "снимок неправдоподобно длинный" in problems
    for raw in ("", "   ", _wrap(""), "<think>только мысли</think>"):
        assert hm.validate_snapshot(raw, None)[1] == ["пустой ответ"]


def test_small_structured_prev_skips_the_shrink_check():
    # У маленького снимка шум оценки велик: «сдувание» ловим только от shrink_min_tokens.
    prev = _snap(chron="\n".join(f"- [#{i}–#{i}] событие {i}" for i in range(1, 15)))
    tiny = hm.render_snapshot({hm.SEC_CHRONICLE: "- x"})
    assert len(prev) < 800 and len(tiny) < 0.5 * len(prev)
    assert hm.validate_snapshot(_wrap(tiny), prev, estimate_tokens=len)[1] == []
    _, problems = hm.validate_snapshot(_wrap(tiny), prev, estimate_tokens=len,
                                       config=hm.MemoryConfig(shrink_min_tokens=100))
    assert any("полов" in p for p in problems)


def test_unclosed_think_at_the_end_is_cut():
    # Модель оборвалась посреди рассуждений после снимка без обёртки.
    raw = _snap() + "\n<think>а вдруг стоило добавить ещё"
    assert hm.extract_snapshot(raw) == (_snap(), False)


def test_real_base64_is_cut_but_long_text_is_kept():
    # Случайные байты с фиксированным зерном — тот же os.urandom, но тест детерминирован.
    blob = base64.b64encode(random.Random(2026).randbytes(600)).decode()
    line = hm.normalize_message(_msg(1, f"вот файл {blob} конец"))
    assert line.endswith("] вот файл  конец")
    mime = "\n".join(blob[i:i + 76] for i in range(0, len(blob), 76))  # перенос MIME
    assert hm.normalize_message(_msg(2, f"{mime}\nконец")).endswith("] конец")
    for text in ("x" * 5000,
                 "Эльвира долго смотрела на море и вспоминала обещание Артура. " * 40):
        assert hm.normalize_message(_msg(3, text)).endswith(f"] {text.strip()}")


# ==================== Доработки по ревью (задача 9) ====================

def test_english_stage_remarks_in_angle_brackets_are_kept():
    # Атрибут без значения признаётся только из списка булевых атрибутов HTML.
    # Раньше им считалось любое латинское слово, и частые в англоязычном
    # ролевом чате ремарки пропадали целиком — сообщение выпадало из пакета.
    for text in ("<time skip>", "<a few hours later>", "<summary of events>", "<small talk>",
                 "<i думаю, что он врёт>"):
        assert hm.normalize_message(_msg(1, text)).endswith(f"] {text}")
    for html, plain in (("<details open>текст</details>", "текст"),
                        ("<DETAILS OPEN>текст</DETAILS>", "текст"),
                        ('<a href="x">ссылка</a>', "ссылка"),
                        ("<video controls autoplay muted loop playsinline>ролик</video>", "ролик"),
                        ("<input type=checkbox checked disabled>пункт", "пункт")):
        assert hm.normalize_message(_msg(1, html)).endswith(f"] {plain}")


def test_card_and_svg_tags_are_stripped():
    # Оформление карточек и SVG: раньше эти теги оставались в транскрипте текстом.
    for html, plain in (
            ("<strike>старое</strike> новое", "старое новое"),
            ("<dl><dt>Имя</dt><dd>Эльвира</dd></dl>", "ИмяЭльвира"),
            ("<table><caption>Итог</caption><tfoot></tfoot></table>", "Итог"),
            ("<nav>меню</nav> <main>суть</main> <aside>сбоку</aside>", "меню суть сбоку"),
            ('<svg><defs><linearGradient id="g"><stop offset="0"/></linearGradient>'
             '<RadialGradient id="r"></RadialGradient></defs><circle cx="1" r="2"/>'
             '<rect x=1 /><line x1=0 /><polygon points="0,0"/><polyline points="1"/>'
             '<ellipse rx=1 /><text x="1"><tspan>Надпись</tspan></text></svg>', "Надпись")):
        assert hm.normalize_message(_msg(1, html)).endswith(f"] {plain}")


def test_extract_ignores_envelope_mention_after_the_snapshot():
    # Зеркальный случай «последней обёртки»: упоминание тега ПОСЛЕ снимка
    # давало «закрыта.» с флагом «обрезан» и платный корректирующий ход.
    raw = _wrap(_snap()) + f"\nОбёртка {hm.ENVELOPE_OPEN} закрыта."
    assert hm.extract_snapshot(raw) == (_snap(), False)
    # И закрытое упоминание после снимка: последний закрытый — не снимок.
    raw = _wrap(_snap()) + f"\nФормат: {hm.ENVELOPE_OPEN}…{hm.ENVELOPE_CLOSE}."
    assert hm.extract_snapshot(raw) == (_snap(), False)
    # Нет структурированного закрытого кандидата — последний закрытый.
    assert hm.extract_snapshot(f"{hm.ENVELOPE_OPEN}a{hm.ENVELOPE_CLOSE} "
                               f"{hm.ENVELOPE_OPEN}b{hm.ENVELOPE_CLOSE}") == ("b", False)
    # Закрытых нет вовсе — последний открытый, и ответ обрезан.
    assert hm.extract_snapshot(f"{hm.ENVELOPE_OPEN} упоминание\n{hm.ENVELOPE_OPEN}\n"
                               + _snap()) == (_snap(), True)


def test_batch_chars_none_or_zero_means_default():
    # None и 0 — «по умолчанию» (80 000), а не TypeError в конструкторе и не
    # молчаливые пакеты по 2000 символов; мусор — тоже дефолт.
    for value in (None, 0, "", "мусор"):
        assert hm.MemoryConfig(batch_max_chars=value).batch_max_chars == 80_000, value
    assert hm.MemoryConfig(batch_max_chars="5000").batch_max_chars == 5000


async def test_budget_warning_after_partial_compaction_names_the_cause():
    # Модель свернула хронику лишь частично (записей для свёртки ещё на порог),
    # а бюджет превышают списки — сами по себе больше бюджета. Без пояснения
    # «X из Y» не объясняло, что автоматически это не пройдёт.
    partial = "\n".join([_ARC] + [f"- [#{i}–#{i}] событие {i}" for i in range(60, 80)])

    async def llm(messages):
        return _wrap(_snap(chron=partial, lists=_BIG_LIST))

    m, _ = _mgr(llm, snapshot_tokens=300)
    out = await m.compact(_snap(chron=_LONG_CHRON, lists=_BIG_LIST))
    assert "Арка «Дорога»" in out and m._foldable(out) >= hm._fold_threshold(12)
    assert m.warnings == [_over_budget(len(out), 300)]


async def test_failed_compaction_does_not_claim_the_chronicle_is_compacted():
    # Сжатие не удалось — «хроника уже сжата» было бы неправдой, хотя списки
    # и тут больше бюджета.
    async def llm(messages):
        return "ерунда"

    m, _ = _mgr(llm, snapshot_tokens=300, validation_retries=0)
    state = _snap(chron=_LONG_CHRON, lists=_BIG_LIST)
    assert await m.compact(state) == state
    assert m.warnings[-1] == (f"снимок превышает бюджет: {_nbsp_int(len(state))} из 300 токенов")


async def test_direct_merge_after_a_run_warns_again():
    # Отсчёт предупреждений прогона сбрасывается в конце прогона: иначе прямой
    # merge_block/compact молча глотал предупреждение, уже выданное прогоном.
    prev = _snap(lists="### Треки\n- «A» — тема (#1)\n- «B» — тема (#2)")

    async def forgetful(messages):
        return _wrap(_snap(lists="### Треки\n- «A» — тема (#1)"))

    lost = "модель потеряла 1 запись — возвращены из предыдущего снимка"
    m, _ = _mgr(forgetful)
    res = await m.scan_and_compress_history([_msg(1)], prev)
    assert res.warnings == [lost]
    await m.merge_block(prev, hm.plan_batch([_msg(2)], batch_size=20, max_chars=10**6))
    assert m.warnings == [lost, lost]


async def test_length_error_folds_arcs_when_no_plain_entries_are_left():
    # Длинный чат: хроника — «40 арок + 12 последних», обычных записей старше
    # keep нет. Модель упёрлась в лимит вывода: раньше сжатие не звали вовсе
    # (сворачивать «нечего»), и повтор слияния снова упирался в тот же лимит.
    # Теперь на пути length сворачиваются и арки — в арку более высокого уровня.
    arcs = [f"- [#{i * 10 + 1}–#{i * 10 + 10}] Арка «Глава {i}»: суть" for i in range(40)]
    recent = [f"- [#{i}–#{i}] событие {i}" for i in range(401, 413)]
    folded = "\n".join(["- [#1–#400] Арка «Книга»: суть сорока глав", *recent])
    systems = []

    async def llm(messages):
        systems.append(messages[0]["content"])
        if len(systems) == 1:
            raise RuntimeError("Технически: ПУСТОЙ ответ, finish_reason=LENGTH.")
        if messages[0]["content"] == hm.MASTER_STATE_PROMPT:
            return _wrap(_snap(chron=folded + "\n- [#413–#413] пакет"))
        return _wrap(_snap(chron=folded))

    m, _ = _mgr(llm)
    state = await m.merge_block(_snap(chron="\n".join(arcs + recent)),
                                hm.plan_batch([_msg(413)], batch_size=20, max_chars=10**6))
    assert len(systems) == 3
    assert systems[0] == systems[2] == hm.MASTER_STATE_PROMPT
    assert systems[1].startswith(hm.COMPACT_PROMPT.format(budget=int(12_000 * 0.8), keep=12))
    assert hm.COMPACT_ARCS_PROMPT in systems[1]
    assert "Арка «Книга»" in state and "Арка «Глава 0»" not in state


async def test_length_error_does_not_fold_when_chronicle_fits_in_keep():
    # Записей хроники не больше keep — сворачивать нечего и на пути length.
    systems = []

    async def llm(messages):
        systems.append(messages[0]["content"])
        if len(systems) == 1:
            raise RuntimeError("Технически: ПУСТОЙ ответ, finish_reason=LENGTH.")
        return _wrap(_snap())

    m, _ = _mgr(llm)
    chron = "\n".join(f"- [#{i}–#{i}] событие {i}" for i in range(1, 13))  # ровно keep
    await m.merge_block(_snap(chron=chron), hm.plan_batch([_msg(13)], batch_size=20,
                                                          max_chars=10**6))
    assert systems == [hm.MASTER_STATE_PROMPT] * 2


# ==================== Доработки по финальному ревью (задача 11) ====================

def _batch(i=80):
    return hm.plan_batch([_msg(i)], batch_size=20, max_chars=10**6)


async def test_truncated_answer_is_length_and_goes_to_compaction_not_correction():
    # Финальное ревью (I1): НЕпустой ответ, оборванный лимитом вывода, уходил в
    # корректирующие ходы с тем же лимитом — три платных вызова, отказ, и так на
    # каждом ходу; путь «сжать хронику и повторить» не срабатывал никогда.
    # Обрыв (открытая обёртка без закрытия) — это length: сразу сжатие и повтор.
    systems, users = [], []

    async def llm(messages):
        systems.append(messages[0]["content"])
        users.append(messages[-1]["content"])
        if len(systems) == 1:
            return hm.ENVELOPE_OPEN + "\n" + _snap(chron=_LONG_CHRON)[:300]
        if messages[0]["content"] == hm.MASTER_STATE_PROMPT:
            return _wrap(_snap(chron=_ARC + "\n- [#80–#80] пакет"))
        return _wrap(_snap(chron=_ARC))

    m, _ = _mgr(llm)
    state = await m.merge_block(_snap(chron=_LONG_CHRON), _batch())
    assert len(systems) == 3
    assert systems[0] == systems[2] == hm.MASTER_STATE_PROMPT
    assert systems[1].startswith(hm.COMPACT_PROMPT.split("{", 1)[0])
    assert hm.COMPACT_ARCS_PROMPT in systems[1]
    assert not any("Ответ отклонён" in u for u in users)
    assert "- [#80–#80] пакет" in state


async def test_repeated_truncation_goes_up_as_one_length_error():
    systems, users = [], []

    async def llm(messages):
        systems.append(messages[0]["content"])
        users.append(messages[-1]["content"])
        return f"{hm.ENVELOPE_OPEN}\n## [{hm.SEC_CHRONICLE}]\n- [#1–#2] оборвано на полусл"

    m, _ = _mgr(llm)
    with pytest.raises(hm.MemoryLLMError) as e:
        await m.merge_block(_snap(chron=_LONG_CHRON), _batch())
    assert e.value.kind == "length" and not e.value.retryable
    # Слияние, одна попытка сжатия (тоже обрыв — снимок прежний), один повтор.
    assert systems.count(hm.MASTER_STATE_PROMPT) == 2 and len(systems) == 3
    assert not any("Ответ отклонён" in u for u in users)


async def test_registry_and_lists_over_the_output_limit_are_refused_without_a_call():
    # Разделы 2–4 сжатие не трогает: если они сами больше лимита вывода, любой
    # ответ модели будет оборван — звать её бессмысленно и платно.
    calls = []

    async def llm(messages):
        calls.append(messages)
        return _wrap(_snap())

    m, _ = _mgr(llm, output_tokens=len(_BIG_LIST) // 2)  # оценка токенов — len
    with pytest.raises(hm.MemoryLLMError) as e:
        await m.merge_block(_snap(lists=_BIG_LIST), _batch())
    assert e.value.kind == "length" and not e.value.retryable
    assert "реестр и списки больше лимита вывода модели памяти" in e.value.message
    assert calls == []


async def test_snapshot_over_the_output_limit_is_compacted_before_the_merge():
    # Весь снимок больше лимита вывода, а разделы 2–4 в него влезают: слияние
    # заведомо оборвалось бы — сначала сжатие хроники, потом слияние.
    systems = []

    async def llm(messages):
        systems.append(messages[0]["content"])
        if messages[0]["content"] == hm.MASTER_STATE_PROMPT:
            return _wrap(_snap(chron=_ARC + "\n- [#80–#80] пакет"))
        return _wrap(_snap(chron=_ARC))

    state = _snap(chron=_LONG_CHRON)
    m, _ = _mgr(llm, output_tokens=1000)
    assert m._guarded_tokens(state) < 1000 < len(state)
    out = await m.merge_block(state, _batch())
    assert len(systems) == 2 and systems[1] == hm.MASTER_STATE_PROMPT
    assert systems[0].startswith(hm.COMPACT_PROMPT.split("{", 1)[0])
    assert "- [#80–#80] пакет" in out


@pytest.mark.parametrize("section, kept, lost", [
    # Плейлист из ТЗ: ключ обеих записей — «linkin park».
    (hm.SEC_LISTS, "### Плейлист Эльвиры\n- Linkin Park — «Numb» — тема ссоры (#3)",
     "- Linkin Park — «In the End» — тема примирения (#9)"),
    # Варианты через скобки: ключ — «кольцо» и «стражник».
    (hm.SEC_REGISTRY, "- Кольцо (серебряное) — у Эльвиры (#4)", "- Кольцо (золотое) — у Артура (#7)"),
    (hm.SEC_CHARACTERS, "- Стражник (у ворот) — пропустил героев", "- Стражник (в башне) — спит"),
    # Четыре слова, три общих: Жаккар ровно 0,6.
    (hm.SEC_REGISTRY, "- Кинжал из чёрной стали — у Артура (#5)",
     "- Кинжал из белой стали — у Эльвиры (#6)"),
])
def test_guard_restores_an_entry_hidden_by_a_same_key_neighbour(section, kept, lost):
    # Финальное ревью (I2): запись считалась сохранённой, если её ключ (или
    # похожий) был где угодно в разделе, — разные записи с одним ключом
    # прикрывали друг друга, и потеря была тихой. Теперь сопоставление —
    # мультимножество: одна новая запись покрывает одну старую.
    prev = hm.render_snapshot({hm.SEC_CHRONICLE: "- [#1–#2] x", section: kept + "\n" + lost})
    new = hm.render_snapshot({hm.SEC_CHRONICLE: "- [#1–#3] x", section: kept})
    fixed, restored = hm.guard_entries(prev, new)
    body = hm.parse_sections(fixed)[section]
    assert lost.strip() in body and body.count(kept.splitlines()[-1]) == 1
    assert len(restored) == 1


def test_guard_keeps_same_key_entries_that_are_both_present_or_updated():
    prev = _snap(lists="### Плейлист\n- Linkin Park — «Numb» — тема ссоры (#3)\n"
                       "- Linkin Park — «In the End» — тема примирения (#9)",
                 chars="- Стражник (у ворот) — пропустил героев\n- Стражник (в башне) — спит")
    new = _snap(lists="### Плейлист\n- Linkin Park — «In the End» — тема примирения (#9)\n"
                      "- Linkin Park — «Numb» — тема ссоры и прощания (было: тема ссоры, #12)",
                chars="- Стражник (у ворот) — пропустил героев; позже поднял тревогу (#30)\n"
                      "- Стражник (в башне) — спит")
    fixed, restored = hm.guard_entries(prev, new)
    assert restored == [] and fixed.count("Numb") == 1 and fixed.count("у ворот") == 1


async def test_compaction_takes_only_the_chronicle_from_the_model():
    # Финальное ревью (I3): ответ сжатия шёл в снимок целиком, а страж сверяет
    # только ключи — «ужатые» моделью атрибуты персонажа, реестр и списки
    # («ключ №7», «ищет брата») пропадали молча. Сжатию позволено менять только
    # хронику, поэтому из ответа берётся только она.
    chars = ("- Эльвира — здоровье: ранена в плечо; при себе: ключ №7, письмо Артура; "
             "скрытые мотивы: ищет брата")
    reg = "- Таверна «Серый гусь» — место встречи; хозяин должен Артуру 30 золотых (#4)"
    lists = "### Треки\n- «A» — тема; играла в сцене #3 (#1)"
    state = _snap(chron=_LONG_CHRON, chars=chars, reg=reg, lists=lists)

    async def llm(messages):
        return _wrap(_snap(chron=_ARC, chars="- Эльвира — ранена",
                           reg="- Таверна «Серый гусь» — место встречи",
                           lists="### Треки\n- «A» — тема (#1)"))

    m, _ = _mgr(llm, snapshot_tokens=10**6)
    out = await m.compact(state, min_fold=1)
    secs, before = hm.parse_sections(out), hm.parse_sections(state)
    assert secs[hm.SEC_CHRONICLE] == _ARC
    for sec in hm.GUARDED_SECTIONS:
        assert secs[sec] == before[sec]
    assert m.warnings == []


@pytest.mark.parametrize("chron", ["—", "- [#1–#40] Арка «Начало»: половина пути"])
async def test_compaction_that_drops_the_recent_chronicle_is_refused(chron):
    # Нижняя граница ответа сжатия: пустая хроника (или хроника без последних
    # записей) стёрла бы историю, которую потом вернёт только пересборка.
    async def llm(messages):
        return _wrap(_snap(chron=chron))

    state = _snap(chron=_LONG_CHRON)
    m, _ = _mgr(llm, snapshot_tokens=10**6)
    assert await m.compact(state, min_fold=1) == state
    assert any("хроник" in w and "оставлен прежний снимок" in w for w in m.warnings)


async def test_budget_warning_is_dropped_once_compaction_brings_the_snapshot_back():
    # Финальное ревью (M2): «превышает бюджет» с пакета, где сжатие не удалось,
    # оставалось в итогах прогона (и в meta.warnings) после удачного сжатия
    # следующего пакета — неверное дважды.
    merges, compacts = [], []

    async def llm(messages):
        if messages[0]["content"] == hm.MASTER_STATE_PROMPT:
            merges.append(1)
            n = 79 + len(merges)
            return _wrap(_snap(chron=_LONG_CHRON + f"\n- [#{n}–#{n}] пакет"))
        compacts.append(1)
        return "ерунда" if len(compacts) == 1 else _wrap(_snap(chron=_ARC + "\n- [#81–#81] пакет"))

    m, _ = _mgr(llm, batch_size=1, snapshot_tokens=600, validation_retries=0)
    res = await m.scan_and_compress_history([_msg(80), _msg(81)])
    assert len(compacts) == 2 and len(res.state) <= 600
    assert not any(w.startswith("снимок превышает бюджет:") for w in res.warnings)
    assert any(w.startswith("сжатие хроники не удалось") for w in res.warnings)


async def test_cancel_interrupts_a_retry_pause_without_another_paid_call():
    # Финальное ревью (M3): «Остановить» проверялся только между пакетами, а
    # пауза повтора по Retry-After длится до 300 с — после неё шли ещё платные
    # попытки. Отмена будит паузу сразу, новых запросов нет.
    cancel, forever = asyncio.Event(), asyncio.Event()
    attempts = []

    async def llm(messages):
        attempts.append(1)
        raise _Err(429, retry_after=300)

    async def long_sleep(seconds):
        cancel.set()           # «Остановить» нажали посреди паузы
        await forever.wait()   # настоящая пауза длилась бы минуты

    m = hm.HierarchicalMemoryManager(llm, hm.MemoryConfig(delay_ms=0, max_retries=4),
                                     sleep=long_sleep)
    res = await asyncio.wait_for(
        m.scan_and_compress_history([_msg(1), _msg(2)], cancel=cancel), 2)
    assert res.status == "cancelled" and res.processed == 0 and len(attempts) == 1


async def test_cancel_interrupts_the_pause_between_requests_and_keeps_the_batch():
    # Пауза-ограничитель перед сжатием тоже прерывается: слитый (оплаченный)
    # пакет записывается несжатым, а сжатие достанется следующему прогону.
    cancel, forever = asyncio.Event(), asyncio.Event()
    systems = []

    async def llm(messages):
        systems.append(messages[0]["content"])
        return _wrap(_snap(chron=_LONG_CHRON))  # сверх бюджета — следом шло бы сжатие

    async def long_sleep(seconds):
        cancel.set()
        await forever.wait()

    m = hm.HierarchicalMemoryManager(
        llm, hm.MemoryConfig(delay_ms=60_000, snapshot_tokens=600), sleep=long_sleep,
        estimate_tokens=len)
    res = await asyncio.wait_for(
        m.scan_and_compress_history([_msg(1), _msg(2)], cancel=cancel), 2)
    assert res.status == "cancelled" and res.processed == 2
    assert systems == [hm.MASTER_STATE_PROMPT] and "событие 79" in res.state


async def test_scan_accepts_batch_size_and_delay_like_the_spec_signature():
    # Буквальная сигнатура ТЗ: scan_and_compress_history(chat_history,
    # batch_size=20, delay_ms=1500) — переопределения конфигурации на один прогон.
    calls = []
    m, sleeps = _mgr(_echo_llm(calls))
    res = await m.scan_and_compress_history([_msg(i) for i in range(1, 6)],
                                            batch_size=2, delay_ms=1500)
    assert res.batches == 3 and res.processed == 5 and sleeps == [1.5, 1.5]
    assert (m.config.batch_size, m.config.delay_ms) == (20, 0)  # только на этот прогон


def test_implausibly_long_limit_grows_with_the_budget():
    # 200 000 символов — около 106 тыс. токенов русского текста: при бюджете
    # снимка больше этого снимок в пределах бюджета отвергался как «неправдоподобный».
    huge = _wrap(_snap(chron="- " + "событие " * 30_000))
    assert "снимок неправдоподобно длинный" in hm.validate_snapshot(huge, None)[1]
    roomy = hm.MemoryConfig(snapshot_tokens=100_000)
    assert "снимок неправдоподобно длинный" not in hm.validate_snapshot(huge, None, config=roomy)[1]


@pytest.mark.parametrize("unit", ["<think>x", "<think x", "<!--x", "<style>x", "<script a"])
def test_normalization_is_fast_on_unclosed_blocks(unit):
    # Финальное ревью (M5): тысячи незакрытых <think>/<!--/<style> в одном
    # сообщении — квадратичный бэктрекинг, секунды синхронно в цикле событий на
    # каждом ходу, пока пакет не записан.
    import time
    text = unit * (80_000 // len(unit))
    started = time.perf_counter()
    hm.normalize_message(_msg(1, text), max_chars=10**7)
    hm.extract_snapshot(text)
    assert time.perf_counter() - started < 1.0


def test_paired_blocks_are_cut_like_before():
    # Скорость — без смены смысла: блок режется до ПЕРВОГО закрытия после
    # открытия, незакрытый блок остаётся текстом, регистр тегов не важен.
    assert hm.normalize_message(_msg(1, "<think>а <think>б</think> хвост")).endswith("] хвост")
    assert hm.normalize_message(_msg(1, "до <STYLE a=1>x</style > после")).endswith("] до  после")
    assert hm.normalize_message(_msg(1, "a <!-- b --> c <!-- d")).endswith("] a  c <!-- d")
    assert hm.normalize_message(_msg(1, "<style>x <!-- y --> z")).endswith("] <style>x  z")
    assert hm.extract_snapshot("<think>a</think>текст <think>оборвано") == ("текст", False)


def test_extended_key_is_linear_on_many_trailing_notes():
    # Регулярка хвостовых пометок «(#id)»/«(было: …)» перебирала число пометок
    # от каждой позиции строки: 20 000 символов «(#1)(#1)…x» — около секунды
    # синхронно в цикле событий, и так для каждой такой записи на каждом пакете.
    import time
    line = "- Кольцо " + "(#1)" * 5000 + "x"
    started = time.perf_counter()
    for _ in range(3):
        hm._extended_key(line)
        hm._extended_key("- Кольцо " + "(#1)" * 5000)
    assert time.perf_counter() - started < 0.5


@pytest.mark.parametrize("line, key", [
    ("- Linkin Park — «Numb» — тема ссоры (#3)", "linkin park numb тема ссоры"),
    ("- Linkin Park — «Numb» — тема (было: ссора, #5) (#12)  ", "linkin park numb тема"),
    ("1. Кольцо (золотое) — у Артура (#7)", "кольцо золотое у артура"),
    ("- Кольцо (золотое)", "кольцо золотое"),          # не пометка — часть имени
    ("- Письмо (#2) (утеряно)", "письмо 2 утеряно"),    # пометка не в конце — остаётся
    ("- Стражник (БЫЛО: спит) ", "стражник"),
    ("- (#4)", ""),
])
def test_extended_key_drops_only_trailing_notes(line, key):
    assert hm._extended_key(line) == key
