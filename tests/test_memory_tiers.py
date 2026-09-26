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


# ==================== Доработки по финальному ревью (задача 11) ====================
def _big_snapshot_record(n_list: int) -> HoraeRecord:
    """Снимок с разросшимися списками — разделы 2–4 сжатие не трогает."""
    lists = "\n".join(f"- «Трек {i}» — тема утраты, звучал в сцене у реки, связан с Артуром (#{i})"
                      for i in range(n_list))
    snap = hm.render_snapshot({hm.SEC_CHRONICLE: "- [#1–#9] Эльвира нашла карту.",
                               hm.SEC_LISTS: "### Треки\n" + lists})
    return HoraeRecord(category="summary", title="📜 Память чата (авто)", content=snap,
                       keywords=["__auto__"], always_on=True, enabled=True, priority=50)


def _long_history(n: int) -> list[dict]:
    return [{"role": "user" if i % 2 == 0 else "assistant",
             "content": f"Реплика {i}: длинный русский текст сцены. " * 25} for i in range(n)]


def test_snapshot_knowledge_and_avatars_are_reserved_in_the_turn_budget():
    """
    Финальное ревью (контекст, I-1): резерв обрезки истории считал только
    системный промпт и реплику, а снимок (до 12 000 токенов и больше — разделы
    2–4 не сжимаются), база знаний с медиа, аватары и хвост шли СВЕРХ бюджета:
    при бюджете 30 000 в модель уходило 34 559 токенов (115 %). Теперь всё,
    что не зависит от обрезки, собирается до неё и входит в резерв; история
    режется под остаток — не больше бюджета и с запасом меньше одной ступени.
    """
    from backend.horae_memory import _TRIM_STEP

    rec = _big_snapshot_record(400)
    history = _long_history(300)
    media = [{"role": "user", "content": [
        {"type": "text", "text": "[База знаний — файл «map.png»]"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]}]
    step = _TRIM_STEP * max(estimate_content_tokens(m["content"]) for m in history)
    for budget in (30_000, 60_000):
        report = {}
        msgs = assemble_context(
            character=_char(), horae_records=[rec], history=history, user_message="Что дальше?",
            token_budget=budget, knowledge_text="Ключ от башни — у стража.",
            knowledge_media=media, send_avatars=True,
            character_avatar="data:image/png;base64,AAAA", author_note="Держи мрачный тон.",
            report=report)
        t = report["tiers"]
        assert t["memory"] > 10_000 and t["trimmed_messages"] > 0
        assert t["total"] == sum(estimate_content_tokens(m["content"]) for m in msgs)
        assert t["total"] <= budget, (budget, t)
        assert budget - t["total"] < step, (budget, t)   # запас — не больше одной ступени


def test_reserve_keeps_the_tail_order_and_the_trim_stable_between_turns():
    """
    Резерв меняет только ГДЕ режется история: порядок хвоста прежний (снимок
    первым, фокус последним), а граница обрезки на следующем ходу с новой
    репликой той же длины не двигается — кэш промпта провайдера не сбивается.
    """
    rec = _big_snapshot_record(200)
    history = _long_history(120)
    first, second = {}, {}
    assemble_context(character=_char(), horae_records=[rec], history=history,
                     user_message="Что дальше?", token_budget=30_000, report=first)
    assemble_context(character=_char(), horae_records=[rec], history=history + _long_history(1),
                     user_message="А потом?", token_budget=30_000, report=second)
    keys = [b["key"] for b in first["tail"]]
    assert keys[0] == "snapshot" and keys[-1] == "focus" and keys.index("anchor") < keys.index("focus")
    assert first["history"]["trimmed"] == second["history"]["trimmed"] > 0


async def test_tiers_through_the_real_window():
    """
    Финальное ревью (контекст, M-2): связку _long_memory → report.memory.dropped
    → tiers.dropped_messages и равенство tiers.total сумме отправленного не
    проверял ни один тест — только подложенный отчёт. Здесь — настоящий чат из
    123 сообщений, снимок учёл до #70: окно (50, шаг 4) выбрасывает 68, дословно
    идут 55, первое дословное — реплика 68.
    """
    from backend import models
    from backend.database import AsyncSessionLocal, engine, init_db
    from backend.horae_memory import build_context_from_db

    await engine.dispose()
    await init_db()
    async with AsyncSessionLocal() as db:
        ui = await db.get(models.AppSetting, "ui")
        saved = dict(ui.value) if ui is not None and isinstance(ui.value, dict) else None
        # Окно по умолчанию и без фактов: вспоминание не зовёт эмбеддинги из .env.
        pinned = {**(saved or {}), "memory_window": 50, "horae_facts": False}
        if ui is None:
            db.add(models.AppSetting(key="ui", value=pinned))
        else:
            ui.value = pinned
        ch = models.Character(name="Эльвира", personality="язвительная")
        db.add(ch)
        await db.flush()
        sess = models.ChatSession(character_id=ch.id, user_key="test:tiers")
        db.add(sess)
        await db.flush()
        msgs = [models.Message(session_id=sess.id, role="user" if i % 2 == 0 else "assistant",
                               content=f"реплика номер {i} про кинжал и Артура") for i in range(123)]
        db.add_all(msgs)
        await db.flush()
        ids = [m.id for m in msgs]
        snap = hm.render_snapshot({hm.SEC_CHRONICLE: "- [#1–#70] Эльвира нашла кинжал."})
        db.add(models.HoraeEntry(session_id=sess.id, category="summary",
                                 title="📜 Память чата (авто)", content=snap,
                                 keywords=["__auto__"], always_on=True, enabled=True, priority=50,
                                 meta={"last_message_id": ids[69], "v": 2, "schema": "hms-1"}))
        await db.commit()
        sid, cid = sess.id, ch.id
    try:
        report = {}
        async with AsyncSessionLocal() as db:
            sent = await build_context_from_db(
                db, await db.get(models.ChatSession, sid), await db.get(models.Character, cid),
                "Что дальше?", None, 200_000, report=report)
        t = report["tiers"]
        assert (t["dropped_messages"], t["window_messages"], t["trimmed_messages"]) == (68, 55, 0)
        assert report["memory"]["dropped"] == 68 and report["memory"]["covered_upto"] == ids[69]
        assert t["total"] == sum(estimate_content_tokens(m["content"]) for m in sent)
        assert t["memory"] == report["tail"][0]["tokens"] > 0
        verbatim = [m["content"] for m in sent if m["role"] in ("user", "assistant")]
        # 55 дословных сообщений и текущая реплика последней.
        assert verbatim[0] == "реплика номер 68 про кинжал и Артура" and len(verbatim) == 56
    finally:
        async with AsyncSessionLocal() as db:
            ui = await db.get(models.AppSetting, "ui")
            if saved is None:
                await db.delete(ui)
            else:
                ui.value = saved
            await db.commit()
        await engine.dispose()


def test_token_cache_does_not_keep_every_snapshot():
    """
    Финальное ревью (сервис, M5): lru_cache оценки токенов на 4096 записей
    держал каждый снимок — каждый пакет добавлял строку размером со снимок, и
    после большой пересборки процесс держал сотни мегабайт. Длинные тексты
    (> 20 000 символов) идут в отдельный маленький кэш.
    """
    from backend import horae_memory

    long_text = "Эльвира пообещала Артуру встретиться у старого маяка. " * 600
    assert len(long_text) > horae_memory._TOKEN_CACHE_MAX_CHARS
    short_before = horae_memory._estimate_short.cache_info().currsize
    for i in range(40):
        horae_memory.estimate_tokens(f"{i} {long_text}")
    assert horae_memory._estimate_short.cache_info().currsize == short_before
    assert horae_memory._estimate_long.cache_info().currsize <= horae_memory._LONG_CACHE_SIZE
    assert horae_memory.estimate_tokens(long_text) == horae_memory.count_tokens(long_text)
