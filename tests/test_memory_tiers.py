"""Монитор токенов по уровням памяти (report["tiers"]) и рендер мастер-снимка в хвосте."""
from backend import hierarchical_memory as hm
from backend import horae_recall as hr
from backend.config import Settings, settings
from backend.horae_memory import (HoraeRecord, assemble_context, estimate_content_tokens,
                                  knowledge_block)


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
    msgs = assemble_context(character=_char(), horae_records=[rec], history=history,
                            user_message="Что дальше?", token_budget=200_000, report=report)
    t = report["tiers"]
    assert t["memory"] > 0 and t["window"] > 0 and t["system"] > 0 and t["current"] > 0
    assert t["total"] == t["system"] + t["memory"] + t["window"] + t["current"]
    # Лимит — из настроек: MODEL_CONTEXT_LIMIT в .env разработчика не должен
    # ронять тест без регрессии. Дефолт проверяется отдельно, по полю Settings.
    assert t["budget"] == 200_000 and t["model_limit"] == settings.MODEL_CONTEXT_LIMIT
    assert Settings.model_fields["MODEL_CONTEXT_LIMIT"].default == 1_000_000
    keys = [b["key"] for b in report["tail"]]
    assert keys[0] == "snapshot" and "focus" in keys and "anchor" in keys
    # Ключ блока хвоста нужен только отчёту: в модель сообщения уходят без него.
    assert all(set(m) == {"role", "content"} for m in msgs)


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


def test_snapshot_with_manual_summary_keeps_markup_and_puts_the_note_after_it():
    """
    Снимок и ручная запись категории summary срабатывают вместе. Раньше весь
    блок уходил в старую ветку: префикс «- 📜 …:» прилипал к первому заголовку
    снимка, а заметка оказывалась внутри раздела списков — модель читала её
    как часть реестра. Теперь снимок идёт телом блока как есть (он первый,
    хотя ручная запись с priority 100 сработала раньше), заметки — после него.
    """
    snap = hm.render_snapshot({hm.SEC_CHRONICLE: "- [#1–#9] Эльвира нашла карту.",
                               hm.SEC_LISTS: "### Треки\n- «Lacrimosa» — тема утраты (#2)"})
    auto = HoraeRecord(category="summary", title="📜 Память чата (авто)", content=snap,
                       keywords=["__auto__"], always_on=True, enabled=True, priority=50)
    note = HoraeRecord(category="summary", title="Моя заметка", content="Артур боится воды.",
                       keywords=["заметка"], always_on=True, enabled=True, priority=100)
    report = {}
    msgs = assemble_context(character=_char(), horae_records=[note, auto], history=[],
                            user_message="?", token_budget=200_000, report=report)
    block = next(m["content"] for m in msgs if "ХРОНИКА И СОСТОЯНИЕ ЧАТА" in str(m["content"]))
    assert "Мастер-снимок старой части чата" in block and "- 📜 Память чата (авто):" not in block
    assert block.index("Мастер-снимок") < block.index(snap)       # разметка снимка цела
    assert block.endswith(snap + "\n\nДополнительные записи памяти:\n- Моя заметка: Артур боится воды.")
    assert report["tail"][0]["key"] == "snapshot"
    assert report["tiers"]["memory"] == report["tail"][0]["tokens"]


def test_two_free_summaries_keep_the_old_list():
    """Без снимка новой схемы — прежний список «- заголовок: текст» без подзаголовка."""
    recs = [HoraeRecord(category="summary", title=t, content=c, keywords=["__auto__"],
                        always_on=True, enabled=True, priority=p)
            for t, c, p in (("Сводка", "Герои в Дольне.", 50), ("Заметка", "Артур ранен.", 100))]
    msgs = assemble_context(character=_char(), horae_records=recs, history=[],
                            user_message="?", token_budget=200_000)
    block = next(m["content"] for m in msgs if "ХРОНИКА И СОСТОЯНИЕ ЧАТА" in str(m["content"]))
    assert block.endswith("\n- Заметка: Артур ранен.\n- Сводка: Герои в Дольне.")
    assert "Дополнительные записи памяти" not in block


def test_knowledge_media_and_wrappers_count_as_system():
    """
    База знаний в Tier 1 — целиком: обе служебные обёртки knowledge_block и
    медиа (картинка ≈ 400 токенов), как аватары. Раньше считался только её
    текст, и монитор занижал вес хода на сотни токенов за каждый файл.
    """
    media = [{"role": "user", "content": [
        {"type": "text", "text": "[База знаний — файл «map.png»]"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]}]
    kw = dict(character=_char(), horae_records=[], history=[], user_message="?",
              token_budget=200_000, knowledge_text="Ключ от башни — у стража.")
    plain, rich = {}, {}
    assemble_context(**kw, report=plain)
    assemble_context(**kw, report=rich, knowledge_media=media)
    assert rich["tiers"]["system"] - plain["tiers"]["system"] >= 400
    assert rich["tiers"]["system"] - plain["tiers"]["system"] == estimate_content_tokens(media[0]["content"])
    whole = sum(estimate_content_tokens(m["content"]) for m in knowledge_block(kw["knowledge_text"], media))
    tail_system = sum(b["tokens"] for b in rich["tail"] if b["key"] not in ("snapshot", "recalled"))
    assert rich["tiers"]["system"] == rich["system_tokens"] + whole + tail_system
