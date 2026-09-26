"""
Служебные задачи Horae (backend/horae_tasks.py) и вспоминание событий
(backend/horae_vector.py): пакетный скан и его отмена, ИИ-анализ ответа,
авто-анализ после хода, авто-свёртка и свёртка свёрток, ручное сжатие,
«ИИ-заполнение» NPC, поиск по старым событиям. Модель подменяется через
backend.llm_gateway.complete — дверь служебных запросов Horae.
"""
import re
from unittest.mock import patch

import pytest

from backend import horae_engine as he
from backend import horae_tasks, horae_vector


@pytest.fixture(autouse=True)
def _no_pause():
    """Пауза между служебными запросами (aux_delay_ms) тестам не нужна."""
    horae_tasks._last_call = -1e9
    horae_tasks._jobs.clear()
    yield
    horae_tasks._jobs.clear()


async def _make_chat(rows, *, settings=None, name="Марина"):
    """rows: [(role, content, meta|None)] → (session_id, [ids])."""
    from backend import models
    from backend.database import AsyncSessionLocal, init_db

    await init_db()
    async with AsyncSessionLocal() as db:
        ch = models.Character(name=name)
        db.add(ch)
        await db.flush()
        sess = models.ChatSession(character_id=ch.id, user_key="test:horae")
        db.add(sess)
        await db.flush()
        ids = []
        for role, content, meta in rows:
            m = models.Message(session_id=sess.id, role=role, content=content, swipes=[content],
                               horae=he.with_meta(None, 0, meta) if meta else None)
            db.add(m)
            await db.flush()
            ids.append(m.id)
        if settings:
            data = he.blank_chat_data()
            data["settings"] = settings
            await he.save_chat_data(db, sess.id, data)
        await db.commit()
        return sess.id, ids


async def _metas(ids):
    from backend import models
    from backend.database import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        out = {}
        for mid in ids:
            m = await db.get(models.Message, mid)
            out[mid] = he.active_meta(m.horae, m.active_swipe)
        return out


def _ev_meta(date, text, level="normal", location="Таверна"):
    from backend import horae_state as hs

    return hs.parse_reply(f"<horae>\ntime:{date} 10:00\nlocation:{location}\ncharacters:Вольф\n</horae>"
                          f"<horaeevent>\nevent:{level}|{text}\n</horaeevent>")[1]


LONG = "Вольф долго смотрел в окно таверны, потом повернулся к Марине и заговорил о брате."


async def test_scan_fills_missing_events_and_undo_restores():
    sid, ids = await _make_chat([
        ("user", "Идём в таверну", None),
        ("assistant", LONG, None),
        ("user", "Что дальше?", None),
        ("assistant", LONG + " Он достал старую карту.", _ev_meta("2026/1/1", "")) ,
    ])
    seen = []

    async def fake(messages, params=None, connection=None, kind="service"):
        prompt = messages[-1]["content"]
        seen.append(prompt)
        mids = re.findall(r"===сообщение#(\d+)===", prompt)
        return "\n".join(
            f"===сообщение#{m}===\n<horae>\ntime:2026/1/{i + 1} 10:00\ncostume:Вольф=плащ\n</horae>\n"
            f"<horaeevent>\nevent:important|Событие сообщения {m}\n</horaeevent>"
            for i, m in enumerate(mids))

    with patch("backend.llm_gateway.complete", new=fake):
        job = horae_tasks.start_scan(sid)
        await job.task
    assert job.status == "done", job.error
    assert job.total == 2 and job.processed == 2
    metas = await _metas([ids[1], ids[3]])
    assert metas[ids[1]]["events"] == [{"level": "important", "text": f"Событие сообщения {ids[1]}"}]
    assert metas[ids[1]]["scanned"] is True and metas[ids[1]]["source"] == "scan"
    # Скан не пишет костюмы (по одному сообщению их не восстановить честно).
    assert metas[ids[1]]["costumes"] == {}
    assert "===сообщение#" in seen[0]
    restored = await horae_tasks.undo_scan(sid)
    assert restored == 2
    metas = await _metas([ids[1], ids[3]])
    assert metas[ids[1]] is None
    assert metas[ids[3]]["time"]["date"] == "2026/1/1" and not metas[ids[3]].get("scanned")


async def test_scan_without_markup_is_an_error():
    sid, _ = await _make_chat([("user", "x", None), ("assistant", LONG, None)])

    async def fake(messages, params=None, connection=None, kind="service"):
        return "Извините, не могу."

    with patch("backend.llm_gateway.complete", new=fake):
        job = horae_tasks.start_scan(sid)
        await job.task
    assert job.status == "error" and "разметку" in job.error


async def test_analyze_merges_into_message_meta():
    sid, ids = await _make_chat([
        ("user", "Пойдём на рынок", None),
        ("assistant", "Они шли по рынку, Лея купила яблоки.", None),
    ])
    prompts = []

    async def fake(messages, params=None, connection=None, kind="service"):
        prompts.append(messages)
        return ("<horae>\ntime:2026/5/1 12:00\nlocation:Рынок\ncharacters:Лея\n"
                "item:🍎Яблоки(5 шт)=Лея@корзина\n</horae>\n"
                "<horaeevent>\nevent:normal|Лея купила яблоки на рынке\n</horaeevent>")

    with patch("backend.llm_gateway.complete", new=fake):
        meta = await horae_tasks.analyze_message(sid, ids[1])
    assert meta["scene"]["location"] == "Рынок" and meta["source"] == "ai"
    assert "Пойдём на рынок" in prompts[0][-1]["content"]      # предыдущая реплика в промпте
    assert (await _metas([ids[1]]))[ids[1]]["items"]["Яблоки(5 шт)"]["holder"] == "Лея"


async def _analysis_calls(rows, settings=None):
    sid, ids = await _make_chat(rows, settings=settings)
    calls = []

    async def fake(messages, params=None, connection=None, kind="service"):
        calls.append(messages)
        return "<horae>\nlocation:Таверна\n</horae><horaeevent>\nevent:normal|Разговор о брате\n</horaeevent>"

    with patch("backend.llm_gateway.complete", new=fake):
        await horae_tasks.after_turn(sid, [ids[-1]])
    return calls, (await _metas([ids[-1]]))[ids[-1]]


async def test_after_turn_fills_gap_when_model_usually_writes_tags():
    tagged = _ev_meta("2026/1/1", "Пришли")
    calls, meta = await _analysis_calls([("user", "Привет", None), ("assistant", "a", tagged),
                                         ("user", "Дальше", None), ("assistant", LONG, None)])
    assert len(calls) == 1 and meta["scene"]["location"] == "Таверна" and meta["source"] == "ai"


async def test_after_turn_does_not_double_cost_when_model_never_writes_tags():
    calls, meta = await _analysis_calls([("user", "Привет", None), ("assistant", LONG, None)])
    assert calls == [] and meta is None
    calls, meta = await _analysis_calls([("user", "Привет", None), ("assistant", LONG, None)],
                                        settings={"auto_analyze": "always"})
    assert len(calls) == 1 and meta["scene"]["location"] == "Таверна"


async def _summary_chat(n_ai=12, settings=None):
    rows = []
    for i in range(n_ai):
        rows.append(("user", f"реплика {i}", None))
        rows.append(("assistant", f"ответ {i} " + LONG, _ev_meta(f"2026/3/{i + 1}", f"Событие {i}")))
    return await _make_chat(rows, settings=settings or {
        "summary_enabled": True, "summary_keep_recent": 3, "summary_buffer_messages": 5,
        "summary_batch_messages": 5, "resummary_threshold": 0})


async def test_auto_summary_starts_after_the_master_snapshot():
    """Историю до отметки снимка несёт он — свёртки не пересказывают её заново."""
    from backend import models
    from backend.database import AsyncSessionLocal

    sid, ids = await _summary_chat()
    async with AsyncSessionLocal() as db:
        db.add(models.HoraeEntry(session_id=sid, category="summary", title="Память чата (авто)",
                                 content="Снимок первых пяти ответов.", always_on=True, enabled=True,
                                 meta={"last_message_id": ids[9], "v": 2}))
        await db.commit()
    prompts = []

    async def fake(messages, params=None, connection=None, kind="service"):
        prompts.append(messages[-1]["content"])
        return "<horaesummary>Ответы 5–9.</horaesummary>"

    # После снимка в буфере 4 ответа (5–8) — меньше порога, поэтому «Свернуть сейчас».
    with patch("backend.llm_gateway.complete", new=fake):
        result = await horae_tasks.run_auto_summary(sid, max_batches=1, force=True)
    assert result["created"] == 1
    async with AsyncSessionLocal() as db:
        data = await he.load_chat_data(db, sid)
    assert data["summaries"][0]["range"] == [ids[10], ids[17]]
    assert "Событие 5" in prompts[0] and "Событие 4" not in prompts[0]


async def test_auto_summary_folds_oldest_buffer_batch():
    sid, ids = await _summary_chat()
    prompts = []

    async def fake(messages, params=None, connection=None, kind="service"):
        prompts.append(messages[-1]["content"])
        return "<horaesummary>Первые пять ответов: события 0–4.</horaesummary>"

    with patch("backend.llm_gateway.complete", new=fake):
        result = await horae_tasks.run_auto_summary(sid, max_batches=1)
    assert result["created"] == 1
    from backend.database import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        data = await he.load_chat_data(db, sid)
    s = data["summaries"][0]
    # Пакет — пять самых старых ответов; диапазон начинается с первого сообщения чата.
    assert s["range"] == [ids[0], ids[9]] and s["kind"] == "auto" and s["date_from"] == "2026/3/1"
    assert "Событие 0" in prompts[0] and "Событие 5" not in prompts[0]
    assert "【Полный текст фрагмента】" in prompts[0]


async def test_auto_summary_waits_for_buffer():
    sid, _ = await _summary_chat(n_ai=6)
    with patch("backend.llm_gateway.complete", new=None):
        result = await horae_tasks.run_auto_summary(sid)
    assert result["created"] == 0 and "буфер" in result["reason"]


async def test_resummary_merges_level_one_summaries():
    sid, ids = await _summary_chat(n_ai=14, settings={
        "summary_enabled": True, "summary_keep_recent": 3, "summary_buffer_messages": 5,
        "summary_batch_messages": 5, "resummary_threshold": 2, "resummary_min_chars": 0})
    from backend.database import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        comp = await he.compute(db, await db.get(__import__("backend.models", fromlist=["x"]).ChatSession, sid))
    for lo, hi, text in ((ids[0], ids[3], "первая"), (ids[4], ids[7], "вторая")):
        async with AsyncSessionLocal() as db:
            await he.add_summary(db, sid, lo=lo, hi=hi, text=text, kind="auto", state=comp.state)
    calls = []

    async def fake(messages, params=None, connection=None, kind="service"):
        calls.append(messages[-1]["content"])
        return "<horaesummary>Итог уровня два</horaesummary>" if "Многоуровневая" in calls[-1] \
            else "<horaesummary>Новая свёртка</horaesummary>"

    with patch("backend.llm_gateway.complete", new=fake):
        result = await horae_tasks.run_auto_summary(sid, max_batches=1)
    assert result["resummaries"] == 1
    async with AsyncSessionLocal() as db:
        data = await he.load_chat_data(db, sid)
    top = data["summaries"][0]
    assert top["depth"] == 2 and top["range"] == [ids[0], ids[7]]
    assert [c["text"] for c in top["children"]] == ["первая", "вторая"]
    # Удаление свёртки уровня 2 возвращает детей.
    async with AsyncSessionLocal() as db:
        await he.delete_summary(db, sid, top["id"])
        data = await he.load_chat_data(db, sid)
    assert [s["text"] for s in data["summaries"] if s["depth"] == 1][:2] == ["первая", "вторая"]


async def test_compress_selected_events():
    sid, ids = await _summary_chat(n_ai=4)

    async def fake(messages, params=None, connection=None, kind="service"):
        assert "Объедините следующие 2 событий" in messages[-1]["content"]
        return "<horaesummary>Сжатые события</horaesummary>"

    with patch("backend.llm_gateway.complete", new=fake):
        s = await horae_tasks.compress(sid, [{"mid": ids[1], "i": 0}, {"mid": ids[3], "i": 0}], [], "events")
    assert s["kind"] == "compress" and s["range"] == [ids[1], ids[3]] and s["text"] == "Сжатые события"
    with pytest.raises(horae_tasks.HoraeTaskError):
        await horae_tasks.compress(sid, [{"mid": ids[5], "i": 0}], [], "events")


async def test_truncated_summary_is_recorded_not_saved():
    sid, _ = await _summary_chat()

    async def fake(messages, params=None, connection=None, kind="service"):
        return "<horaesummary>Обрыв на полусл"

    with patch("backend.llm_gateway.complete", new=fake):
        with pytest.raises(horae_tasks.HoraeTaskError):
            await horae_tasks.run_auto_summary(sid)
    from backend.database import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        data = await he.load_chat_data(db, sid)
    assert data["summaries"] == [] and "оборван" in data["summary_error"]["message"]


async def test_npc_enrich_reads_mentions():
    sid, _ = await _make_chat([
        ("assistant", "Кай — высокий моряк с седой бородой.", None),
        ("user", "Кай, ты мне поможешь?", None),
        ("assistant", "Никто не ответил.", None),
    ])
    seen = []

    async def fake(messages, params=None, connection=None, kind="service"):
        seen.append(messages[-1]["content"])
        return '{"appearance": "высокий, седая борода", "gender": "мужской"}'

    with patch("backend.llm_gateway.complete", new=fake):
        out = await horae_tasks.npc_enrich(sid, "Кай")
    assert out["hits"] == 2 and out["fields"]["appearance"] == "высокий, седая борода"
    assert "Никто не ответил" not in seen[0]


async def test_recall_finds_old_event_outside_window_lexically():
    rows = []
    for i in range(30):
        rows.append(("user", f"реплика {i}", None))
        text = "Вольф спрятал золотой ключ под половицей в подвале" if i == 2 else f"Обычный разговор номер {i}"
        rows.append(("assistant", f"ответ {i}", _ev_meta(f"2026/4/{i % 28 + 1}", text,
                                                         level="important" if i == 2 else "normal")))
    sid, ids = await _make_chat(rows)
    from backend import models
    from backend.database import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        await horae_vector.sync_documents(db, sid, {})
        await db.commit()
        sess = await db.get(models.ChatSession, sid)
        comp = await he.compute(db, sess)
        window = ids[-10:]
        found = await horae_vector.recall(db, sess, comp, user_message="Где золотой ключ?",
                                          connection={}, history_ids=window)
    assert found and found[0]["mid"] == ids[5]
    assert all(c["mid"] < window[0] for c in found)
    text = horae_vector.render(found[:3], current_date="2026/4/30")
    assert text.startswith("[Воспоминание") and f"#{ids[5]}" in text and "золотой ключ" in text


async def test_recall_structured_first_meeting():
    rows = [("user", "x", None),
            ("assistant", "a", _ev_meta("2026/1/1", "Лея впервые встретила Кая на пристани")),
            ("user", "y", None),
            ("assistant", "b", _ev_meta("2026/1/2", "Шторм"))]
    from backend import horae_state as hs
    rows[1] = ("assistant", "a", hs.parse_reply(
        "<horae>\ncharacters:Кай, Лея\nnpc:Кай|высокий=@проводник\n</horae>"
        "<horaeevent>\nevent:normal|Лея впервые встретила Кая на пристани\n</horaeevent>")[1])
    sid, ids = await _make_chat(rows)
    from backend import models
    from backend.database import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        await horae_vector.sync_documents(db, sid, {})
        await db.commit()
        sess = await db.get(models.ChatSession, sid)
        comp = await he.compute(db, sess)
        found = await horae_vector.recall(db, sess, comp, user_message="Где я впервые встретила Кая?",
                                          connection={}, history_ids=[ids[3]])
    assert found[0]["mid"] == ids[1] and "structured" in found[0]["source"]


def test_visible_filters_after_budget_trim():
    cands = [{"mid": 5}, {"mid": 40}, {"mid": None, "carried": True}]
    out = horae_vector.visible(cands, [30, 31, 40, 41], 4, 2, 5)
    assert out == [{"mid": 5}, {"mid": None, "carried": True}]


async def test_after_turn_indexes_whole_chat_once_then_incrementally():
    rows = [("user", "a", None), ("assistant", "x", _ev_meta("2026/1/1", "Первое")),
            ("user", "b", None), ("assistant", "y", _ev_meta("2026/1/2", "Второе"))]
    sid, ids = await _make_chat(rows)
    from sqlalchemy import func, select as sel

    from backend import models
    from backend.database import AsyncSessionLocal
    await horae_tasks.after_turn(sid, [ids[3]])
    async with AsyncSessionLocal() as db:
        n = (await db.execute(sel(func.count()).select_from(models.HoraeMemoryDoc)
                              .where(models.HoraeMemoryDoc.session_id == sid))).scalar()
        data = await he.load_chat_data(db, sid)
    # Старый ответ без документа тоже проиндексирован — первым полным проходом.
    assert n == 2 and data["docs_synced"] is True


def _bow(texts):
    vocab = ["ключ", "подвал", "шторм", "корабль", "таверна", "вольф", "золот"]
    out = []
    for t in texts:
        low = t.lower()
        v = [1.0 if w in low else 0.0 for w in vocab] + [0.1]
        out.append(v)
    return out


async def test_recall_uses_vectors_when_embeddings_configured():
    rows = []
    for i in range(12):
        rows.append(("user", f"u{i}", None))
        text = "Вольф спрятал золотой ключ в подвале" if i == 1 else f"Шторм и корабль номер {i}"
        rows.append(("assistant", f"a{i}", _ev_meta(f"2026/6/{i + 1}", text)))
    sid, ids = await _make_chat(rows)
    conn = {"embedding_model": "fake-embed"}

    async def fake_embed(texts, connection=None):
        return _bow(texts)

    from backend import models
    from backend.database import AsyncSessionLocal
    with patch("backend.llm_gateway.embed", new=fake_embed):
        async with AsyncSessionLocal() as db:
            await horae_vector.sync_documents(db, sid, conn)
            await db.commit()
            sess = await db.get(models.ChatSession, sid)
            comp = await he.compute(db, sess, settings={**comp_settings(), "recall_threshold": 0.5})
            found = await horae_vector.recall(db, sess, comp, user_message="где золотой ключ от подвала?",
                                              connection=conn, history_ids=ids[-4:])
            again = await horae_vector.recall(db, sess, comp, user_message="где золотой ключ от подвала?",
                                              connection=conn, history_ids=ids[-4:])
    assert found and found[0]["mid"] == ids[3] and "vector" in found[0]["source"]
    assert [c["mid"] for c in again] == [c["mid"] for c in found]   # второй раз — из кэша векторов


def comp_settings():
    from backend import horae_settings

    return horae_settings.resolve()
