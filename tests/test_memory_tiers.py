"""Монитор токенов по уровням памяти (report["tiers"]) и рендер мастер-снимка в хвосте."""
from backend import hierarchical_memory as hm
from backend import horae_recall as hr
from backend.horae_memory import HoraeRecord, assemble_context, estimate_content_tokens


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


def test_free_text_summary_keeps_old_rendering_and_counts_as_memory():
    """Старая свободная сводка (не снимок) идёт списком «- заголовок: текст», как до 2.5.0."""
    rec = HoraeRecord(category="summary", title="Память чата (авто)", content="Герои пришли в Дольн.",
                      keywords=["__auto__"], always_on=True, enabled=True, priority=50)
    report = {}
    msgs = assemble_context(character=_char(), horae_records=[rec], history=[],
                            user_message="?", token_budget=200_000, report=report)
    block = next(m["content"] for m in msgs if "ХРОНИКА И СОСТОЯНИЕ ЧАТА" in str(m["content"]))
    assert "- Память чата (авто): Герои пришли в Дольн." in block and "Мастер-снимок" not in block
    assert report["tail"][0]["key"] == "snapshot"
    assert report["tiers"]["memory"] == report["tail"][0]["tokens"]


def test_tiers_window_counts_match_history_report():
    """Окно — то, что пережило и окно памяти, и бюджет; выброшенное окном берём из отчёта памяти."""
    history = [{"role": "user", "content": "Длинная русская реплика про всё на свете. " * 20}
               for _ in range(60)]
    report = {"memory": {"dropped": 8}}  # так его заранее заполняет build_context_from_db
    assemble_context(character=_char(), horae_records=[], history=history,
                     user_message="и что дальше?", token_budget=3000, report=report)
    t, h = report["tiers"], report["history"]
    assert h["trimmed"] > 0
    assert t["window"] == h["tokens"] and t["window_messages"] == h["included"]
    assert t["trimmed_messages"] == h["trimmed"] and t["dropped_messages"] == 8
    assert t["memory"] == 0


def test_current_counts_attachments_and_avatars_count_as_system():
    """Текущее сообщение — вместе с вложениями; аватары и напоминание о них — Tier 1."""
    content = [{"type": "text", "text": "Смотри"},
               {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]
    kw = dict(character=_char(), horae_records=[], history=[], user_message="Смотри",
              token_budget=200_000)
    plain, rich = {}, {}
    assemble_context(**kw, report=plain)
    msgs = assemble_context(**kw, report=rich, user_attachments_content=content,
                            send_avatars=True, character_avatar="data:image/png;base64,AAAA")
    assert rich["tiers"]["current"] == estimate_content_tokens(content) > plain["tiers"]["current"]
    # Картинка-аватар — единственное мультимодальное сообщение до текущего.
    avatar = sum(estimate_content_tokens(m["content"]) for m in msgs[:-1]
                 if isinstance(m["content"], list))
    extra_tail = sum(b["tokens"] for b in rich["tail"] if b["key"] in ("appearance", "manifest"))
    focus_delta = (next(b["tokens"] for b in rich["tail"] if b["key"] == "focus")
                   - next(b["tokens"] for b in plain["tail"] if b["key"] == "focus"))
    assert avatar >= 400 and extra_tail > 0
    assert rich["tiers"]["system"] == plain["tiers"]["system"] + avatar + extra_tail + focus_delta

    empty = {}
    assemble_context(character=_char(), horae_records=[], history=[], user_message="",
                     token_budget=200_000, report=empty)
    assert empty["tiers"]["current"] == 0
