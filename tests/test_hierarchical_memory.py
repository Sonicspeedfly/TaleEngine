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
