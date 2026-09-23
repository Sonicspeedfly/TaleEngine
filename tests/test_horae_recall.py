"""
Долгая память Horae: активное окно, факты и их отбор (backend/horae_recall.py).

Чистые функции (окно, ранжирование, разбор ответа модели) проверяются без БД и
сети. Проводка через БД — на настоящей сборке контекста: именно там память раньше
либо не работала вовсе (модуля не было, и КАЖДЫЙ ход падал на импорте), либо
тащила в контекст нерелевантный мусор.
"""
import math
from unittest.mock import patch

import pytest
from sqlalchemy import select

from backend import horae_recall as hr
from backend.horae_memory import assemble_context


def _char():
    return {"name": "Тест", "description": "", "personality": "",
            "scenario": "", "system_prompt": ""}


def _fact(content, src=1, emb=None, **kw):
    f = {"content": content, "source_message_id": src}
    if emb is not None:
        f["embedding"] = emb
    f.update(kw)
    return f


# ==================== Слой 1: активное окно ====================

def test_window_short_history_is_kept_whole():
    assert hr.window_start([1, 2, 3], covered=3, window=20) == 0


def test_window_zero_or_negative_means_whole_history():
    ids = list(range(1, 101))
    assert hr.window_start(ids, covered=100, window=0) == 0
    assert hr.window_start(ids, covered=100, window=-5) == 0


def test_window_without_summary_drops_nothing():
    """Сводки ещё нет — выбрасывать нечего: иначе середина чата пропала бы."""
    assert hr.window_start(list(range(1, 101)), covered=0, window=20) == 0


def test_window_drops_only_summarised_messages():
    ids = list(range(1, 101))
    # Сводка учла только первые 30 — выбросить можно не больше 30, хотя окно
    # позволило бы выбросить 80.
    assert hr.window_start(ids, covered=30, window=20, step=1) == 30
    # Сводка учла всё — остаётся ровно окно.
    assert hr.window_start(ids, covered=100, window=20, step=1) == 80


def test_window_stops_at_unknown_id():
    """Неизвестный id считается неучтённым: на нём отбрасывание останавливается."""
    ids = [1, 2, 3, None, 5, 6, 7, 8, 9, 10]
    assert hr.window_start(ids, covered=10, window=2, step=1) == 3
    assert hr.window_start([None] * 50, covered=10**9, window=5) == 0


def test_window_moves_in_steps_for_prompt_cache():
    """Граница двигается ступенями — начало истории не меняется каждый ход."""
    starts = {hr.window_start(list(range(1, n + 1)), covered=n, window=20, step=4)
              for n in range(40, 44)}
    assert len(starts) == 1
    for n in range(21, 120):
        start = hr.window_start(list(range(1, n + 1)), covered=n, window=20, step=4)
        assert start % 4 == 0
        assert 20 <= n - start < 24  # дословно всегда окно, не меньше


def test_window_bad_input_does_not_raise():
    assert hr.window_start([], covered=5, window=20) == 0
    assert hr.window_start(list(range(1, 50)), covered="мусор", window="мусор") == 0


# ==================== Слой 3: ранжирование (векторы) ====================

def test_vector_threshold_boundary_is_inclusive():
    q = [1.0, 0.0]
    exact = _fact("ровно на пороге", emb=[3.0, math.sqrt(7.0)])   # cos = 0.75
    below = _fact("чуть ниже порога", emb=[0.74, math.sqrt(1 - 0.74 ** 2)])
    got = hr.rank_facts([exact, below], 10, query_vec=q)
    assert [r["content"] for r in got] == ["ровно на пороге"]
    assert got[0]["similarity"] == pytest.approx(0.75, abs=1e-4)


def test_vector_threshold_uses_raw_similarity_not_recency():
    """Свежий, но посторонний факт в контекст не пролезает."""
    q = [1.0, 0.0]
    fresh_offtopic = _fact("свежий посторонний", src=1000, emb=[0.6, 0.8])  # cos 0.6
    old_exact = _fact("старый точный", src=1, emb=[1.0, 0.0])
    got = hr.rank_facts([fresh_offtopic, old_exact], 1000, query_vec=q)
    assert [r["content"] for r in got] == ["старый точный"]


def test_recency_orders_equal_similarity_but_never_zeroes_old_facts():
    q = [1.0, 0.0]
    old = _fact("обещание с 50-го сообщения", src=50, emb=[1.0, 0.0])
    new = _fact("свежий факт", src=4990, emb=[1.0, 0.0])
    got = hr.rank_facts([old, new], 5000, query_vec=q)
    assert [r["content"] for r in got] == ["свежий факт", "обещание с 50-го сообщения"]
    assert got[1]["score"] >= hr.RECENCY_FLOOR - 1e-6  # старое не обнуляется
    assert got[0]["score"] > got[1]["score"]


def test_age_field_overrides_id_distance():
    """Возраст в сообщениях чата важнее разницы id (id общие на все чаты)."""
    q = [1.0, 0.0]
    a = _fact("a", src=10, emb=[1.0, 0.0], age=0)
    b = _fact("b", src=900, emb=[1.0, 0.0], age=5000)
    got = hr.rank_facts([a, b], 1000, query_vec=q)
    assert got[0]["content"] == "a"


def test_top_k_cap_and_result_shape():
    q = [1.0, 0.0]
    facts = [_fact(f"факт {i}", src=i, emb=[1.0, 0.01 * i]) for i in range(12)]
    got = hr.rank_facts(facts, 12, query_vec=q)
    assert len(got) == hr.TOP_K == 5
    assert set(got[0]) == {"content", "similarity", "score", "source_message_id"}
    scores = [r["score"] for r in got]
    assert scores == sorted(scores, reverse=True)


def test_empty_and_incomparable_inputs():
    assert hr.rank_facts([], 10, query_vec=[1.0, 0.0]) == []
    assert hr.rank_facts([], 10, query_text="что угодно") == []
    # Факт без вектора или с вектором другой размерности в режиме векторов пропускается.
    facts = [_fact("без вектора"), _fact("другая модель", emb=[1.0, 0.0, 0.0])]
    assert hr.rank_facts(facts, 10, query_vec=[1.0, 0.0]) == []


# ==================== Слой 3: поиск по словам (режим по умолчанию) ====================

_FACTS = [
    _fact("Эльвира пообещала вернуть Артуру серебряный кинжал до полнолуния", 10),
    _fact("Таверна «Сломанный рог» стоит у северных ворот Дольна", 20),
    _fact("Артур ранен в левое плечо стрелой орков", 30),
    _fact("Гильдия воров должна Артуру двести золотых", 40),
    _fact("Мельник Густав — отец Эльвиры", 50),
]


def test_lexical_unrelated_query_recalls_nothing():
    """Нет относящихся фактов — нет и блока (не засорять контекст)."""
    assert hr.rank_facts(_FACTS, 100, query_text="Какая сегодня погода? Пойдём купаться на озеро.") == []


def test_lexical_common_word_alone_is_not_enough():
    """Одно общее слово факта («вернуть») — не повод его вспоминать."""
    assert hr.rank_facts(_FACTS, 100, query_text="Хочу вернуться домой") == []


@pytest.mark.parametrize("query", ["Где Эльвира?", "Ты не видел Эльвиру?", "Что с Эльвирой?"])
def test_lexical_entity_survives_russian_inflection(query):
    got = [r["content"] for r in hr.rank_facts(_FACTS, 100, query_text=query)]
    assert any("Эльвира пообещала" in c for c in got)
    assert not any("Гильдия" in c for c in got)


def test_lexical_picks_the_right_fact():
    got = hr.rank_facts(_FACTS, 100, query_text="Сколько денег нам должна гильдия?")
    assert [r["content"] for r in got] == [_FACTS[3]["content"]]
    got = hr.rank_facts(_FACTS, 100, query_text="Артур, как твоё плечо?")
    assert got[0]["content"] == _FACTS[2]["content"]


def test_lexical_scores_are_normalised():
    sims = hr.lexical_similarities("Эльвира Артур кинжал серебряный полнолуние вернуть пообещала", _FACTS)
    assert all(0.0 <= s <= 1.0 for s in sims)
    assert sims[0] == pytest.approx(1.0)


def test_lexical_threshold_is_separate_from_vector_threshold():
    assert hr.LEXICAL_THRESHOLD != hr.SIM_THRESHOLD
    assert hr.SIM_THRESHOLD == 0.75


# ==================== Разбор ответа модели ====================

def test_parse_facts_bullets_numbering_and_blank_lines():
    text = (
        "Вот факты:\n\n"
        "- Эльвира — дочь мельника Густава\n"
        "* Артур ранен в плечо\n"
        "1. Гильдия должна Артуру двести золотых\n"
        "2) Таверна стоит у северных ворот\n"
        "• **Кинжал** остался у Эльвиры\n"
        "\n"
        "### Персонажи\n"
    )
    assert hr.parse_facts(text) == [
        "Эльвира — дочь мельника Густава",
        "Артур ранен в плечо",
        "Гильдия должна Артуру двести золотых",
        "Таверна стоит у северных ворот",
        "Кинжал остался у Эльвиры",
    ]


def test_parse_facts_json_array_in_code_fence():
    text = '```json\n["Эльвира — дочь мельника", {"fact": "Артур ранен в плечо"}]\n```'
    assert hr.parse_facts(text) == ["Эльвира — дочь мельника", "Артур ранен в плечо"]


def test_parse_facts_json_object_with_facts_key():
    assert hr.parse_facts('{"facts": ["Артур ранен в плечо"]}') == ["Артур ранен в плечо"]


def test_parse_facts_dedupes_case_insensitively_and_caps():
    text = "Артур ранен в плечо\nартур ранен в плечо.\nАРТУР РАНЕН В ПЛЕЧО"
    assert hr.parse_facts(text) == ["Артур ранен в плечо"]
    long = "Артур " + "очень " * 100 + "устал"
    got = hr.parse_facts(long)
    assert len(got) == 1 and len(got[0]) <= hr.MAX_FACT_CHARS + 1
    many = "\n".join(f"Факт номер {i} про Артура" for i in range(50))
    assert len(hr.parse_facts(many)) == hr.MAX_FACTS_PER_CHUNK


def test_parse_facts_empty_and_no_facts_answers():
    assert hr.parse_facts("") == []
    assert hr.parse_facts(None) == []
    assert hr.parse_facts("Нет фактов.") == []
    assert hr.parse_facts("нет") == []


def test_facts_prompt_asks_for_atomic_self_contained_facts():
    p = hr.FACTS_PROMPT
    assert "АТОМАРНЫЕ" in p and "по именам" in p and "по одному на строку" in p


# ==================== Блок в контексте ====================

def test_render_recalled_block_shape_and_chronology():
    block = hr.render_recalled([
        {"content": "поздний факт", "source_message_id": 90, "score": 0.9},
        {"content": "ранний факт", "source_message_id": 10, "score": 0.5},
    ])
    assert block.startswith("[HORAE RECALLED MEMORY:")
    assert "ЭТОГО чата" in block
    assert block.index("ранний факт") < block.index("поздний факт")


def test_render_recalled_empty_is_empty_string():
    assert hr.render_recalled([]) == ""
    assert hr.render_recalled(None) == ""
    assert hr.render_recalled([{"content": "  "}]) == ""


def test_assemble_context_adds_no_recalled_block_without_facts():
    for facts in (None, [], [{"content": ""}]):
        messages = assemble_context(character=_char(), horae_records=[], history=[],
                                    user_message="привет", recalled_facts=facts)
        assert "HORAE RECALLED MEMORY" not in str(messages)


def test_assemble_context_adds_recalled_block_and_reports_it():
    report: dict = {}
    messages = assemble_context(
        character=_char(), horae_records=[], history=[], user_message="где кинжал?",
        recalled_facts=[{"content": "Кинжал у Эльвиры", "score": 0.8, "similarity": 0.8}],
        report=report,
    )
    system = [m["content"] for m in messages if m["role"] == "system"]
    assert any(s.startswith("[HORAE RECALLED MEMORY:") and "Кинжал у Эльвиры" in s for s in system)
    assert report["recalled"] == [{"content": "Кинжал у Эльвиры", "score": 0.8, "similarity": 0.8}]


# ==================== Эмбеддинги (llm_gateway.embed) ====================

async def test_embed_disabled_without_model():
    from backend import llm_gateway

    async def boom(**kw):  # noqa: ANN003
        raise AssertionError("без модели эмбеддингов запроса быть не должно")

    with patch("backend.llm_gateway.litellm.aembedding", new=boom):
        assert await llm_gateway.embed(["текст"], {"embedding_model": ""}) is None
        assert await llm_gateway.embed(["текст"], None) is None


async def test_embed_error_returns_none_instead_of_raising():
    from backend import llm_gateway

    async def fail(**kw):  # noqa: ANN003
        raise RuntimeError("провайдер лежит")

    with patch("backend.llm_gateway.litellm.aembedding", new=fail):
        assert await llm_gateway.embed(["текст"], {"embedding_model": "emb"}) is None


async def test_embed_orders_vectors_by_index_and_routes_like_chat():
    from backend import llm_gateway

    seen = {}

    async def fake(**kw):  # noqa: ANN003
        seen.update(kw)
        return {"data": [{"index": 1, "embedding": [0.0, 1.0]},
                         {"index": 0, "embedding": [1.0, 0.0]}],
                "usage": {"prompt_tokens": 4}}

    conn = {"embedding_model": "text-emb", "use_proxy": True, "base_url": "http://proxy:4000"}
    with patch("backend.llm_gateway.litellm.aembedding", new=fake):
        got = await llm_gateway.embed(["первый", "второй"], conn)
    assert got == [[1.0, 0.0], [0.0, 1.0]]
    assert seen["model"] == "litellm_proxy/text-emb" and seen["api_base"] == "http://proxy:4000"


# ==================== Проводка через БД ====================

async def _fresh_db():
    from backend.database import engine, init_db

    # Пул соединений мог быть создан в чужом event loop (TestClient) — сбрасываем.
    await engine.dispose()
    await init_db()


async def _make_chat(n_messages: int = 0, prefix: str = "реплика") -> tuple[int, int, list[int]]:
    from backend import models
    from backend.database import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        ch = models.Character(name="Память")
        db.add(ch)
        await db.commit()
        await db.refresh(ch)
        sess = models.ChatSession(character_id=ch.id, user_key="test:recall")
        db.add(sess)
        await db.commit()
        await db.refresh(sess)
        msgs = []
        for i in range(n_messages):
            m = models.Message(session_id=sess.id, role="user" if i % 2 == 0 else "assistant",
                               content=f"{prefix} {i}")
            db.add(m)
            msgs.append(m)
        await db.commit()
        return ch.id, sess.id, [m.id for m in msgs]


async def _add_facts(sid: int, facts: list[tuple[str, int]]) -> None:
    from backend import models
    from backend.database import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        for content, src in facts:
            db.add(models.HoraeFact(session_id=sid, content=content, source_message_id=src))
        await db.commit()


def _keyword_vectors(texts):
    """Детерминированные «эмбеддинги»: ось на каждое ключевое слово."""
    axes = ["эльвир", "кинжал", "таверн", "гильди", "погод"]
    out = []
    for t in texts:
        low = (t or "").lower()
        vec = [1.0 if a in low else 0.0 for a in axes] + [0.1]
        out.append(vec)
    return out


async def test_summary_pass_extracts_and_stores_facts():
    from backend import main, models
    from backend.database import AsyncSessionLocal, engine

    await _fresh_db()
    # 12 сообщений старше активного окна: само окно сводка не трогает.
    _, sid, ids = await _make_chat(12 + hr.DEFAULT_WINDOW,
                                   prefix="Эльвира пообещала вернуть кинжал, событие")

    calls = []

    async def fake_complete(messages, params=None, connection=None, kind="service"):
        calls.append(messages[0]["content"])
        if messages[0]["content"] == hr.FACTS_PROMPT:
            return "- Эльвира пообещала вернуть Артуру кинжал\n- Артур ранен в плечо\n"
        return "### Текущее состояние сюжета\nГерои в пути."

    with patch("backend.main.complete", new=fake_complete):
        await main._maybe_update_summary(sid)

    assert hr.FACTS_PROMPT in calls, "факты извлекаются тем же фоновым проходом"
    async with AsyncSessionLocal() as db:
        facts = (await db.execute(select(models.HoraeFact).where(
            models.HoraeFact.session_id == sid))).scalars().all()
        entry = (await db.execute(select(models.HoraeEntry).where(
            models.HoraeEntry.session_id == sid,
            models.HoraeEntry.category == "summary"))).scalars().first()
    assert sorted(f.content for f in facts) == [
        "Артур ранен в плечо", "Эльвира пообещала вернуть Артуру кинжал",
    ]
    # Источник факта — последнее сообщение куска: по нему считается свежесть.
    # Кусок кончается перед активным окном.
    assert all(f.source_message_id == ids[11] for f in facts)
    assert all(not f.embed_model for f in facts)  # эмбеддинги не настроены
    assert entry is not None and "Герои в пути" in entry.content
    await engine.dispose()


async def test_summary_survives_fact_failure():
    """Сбой извлечения фактов не должен стоить уже посчитанной сводки."""
    from backend import main, models
    from backend.database import AsyncSessionLocal, engine

    await _fresh_db()
    _, sid, _ = await _make_chat(12 + hr.DEFAULT_WINDOW)

    async def fake_complete(messages, params=None, connection=None, kind="service"):
        if messages[0]["content"] == hr.FACTS_PROMPT:
            raise RuntimeError("быстрая модель упала")
        return "Сводка на месте."

    with patch("backend.main.complete", new=fake_complete):
        await main._maybe_update_summary(sid)

    async with AsyncSessionLocal() as db:
        entry = (await db.execute(select(models.HoraeEntry).where(
            models.HoraeEntry.session_id == sid,
            models.HoraeEntry.category == "summary"))).scalars().first()
    assert entry is not None and entry.content == "Сводка на месте."
    await engine.dispose()


async def test_store_facts_skips_duplicates():
    from backend import models
    from backend.database import AsyncSessionLocal, engine

    await _fresh_db()
    _, sid, _ = await _make_chat(0)
    async with AsyncSessionLocal() as db:
        n1 = await hr.store_facts(db, sid, ["Артур ранен в плечо"], 5, {})
        n2 = await hr.store_facts(db, sid, ["артур ранен в плечо!", "Эльвира ушла в лес"], 6, {})
        rows = (await db.execute(select(models.HoraeFact.content).where(
            models.HoraeFact.session_id == sid))).scalars().all()
    assert (n1, n2) == (1, 1)
    assert sorted(rows) == ["Артур ранен в плечо", "Эльвира ушла в лес"]
    await engine.dispose()


async def test_store_facts_embeds_and_skips_near_duplicates():
    from backend import models
    from backend.database import AsyncSessionLocal, engine

    await _fresh_db()
    _, sid, _ = await _make_chat(0)

    async def fake_embed(texts, connection=None):
        return _keyword_vectors(texts)

    conn = {"embedding_model": "fake-emb"}
    with patch("backend.llm_gateway.embed", new=fake_embed):
        async with AsyncSessionLocal() as db:
            await hr.store_facts(db, sid, ["Эльвира хранит кинжал"], 5, conn)
            # Другие слова, тот же смысл по вектору (косинус 1.0) — почти-дубль.
            added = await hr.store_facts(db, sid, ["Кинжал хранится у Эльвиры"], 6, conn)
            rows = (await db.execute(select(models.HoraeFact).where(
                models.HoraeFact.session_id == sid))).scalars().all()
    assert added == 0 and len(rows) == 1
    assert rows[0].embed_model == "fake-emb"
    assert abs(sum(x * x for x in rows[0].embedding) - 1.0) < 1e-3  # хранится единичным
    await engine.dispose()


async def test_recall_excludes_facts_from_active_window():
    from backend.database import AsyncSessionLocal, engine

    await _fresh_db()
    _, sid, ids = await _make_chat(30)
    await _add_facts(sid, [
        ("Эльвира пообещала вернуть кинжал", ids[5]),     # давно — вспоминаем
        ("Эльвира смеялась у костра", ids[25]),          # уже в окне дословно
    ])
    async with AsyncSessionLocal() as db:
        stats: dict = {}
        got = await hr.recall(db, sid, "Где Эльвира?", {}, newest_id=ids[-1],
                              exclude_from_id=ids[20], stats=stats)
        everything = await hr.recall(db, sid, "Где Эльвира?", {}, newest_id=ids[-1])
    assert [r["content"] for r in got] == ["Эльвира пообещала вернуть кинжал"]
    assert stats == {"mode": "lexical", "candidates": 1}
    assert len(everything) == 2
    await engine.dispose()


async def test_recall_uses_vectors_when_embeddings_work():
    from backend.database import AsyncSessionLocal, engine

    await _fresh_db()
    _, sid, ids = await _make_chat(4)

    async def fake_embed(texts, connection=None):
        return _keyword_vectors(texts)

    conn = {"embedding_model": "fake-emb"}
    with patch("backend.llm_gateway.embed", new=fake_embed):
        async with AsyncSessionLocal() as db:
            await hr.store_facts(db, sid, ["Эльвира хранит кинжал", "Гильдия ждёт долг"], ids[1], conn)
            stats: dict = {}
            got = await hr.recall(db, sid, "где эльвира и кинжал", conn, stats=stats)
    assert stats["mode"] == "vector"
    assert [r["content"] for r in got] == ["Эльвира хранит кинжал"]
    await engine.dispose()


async def test_recall_falls_back_to_words_when_embedding_fails():
    from backend.database import AsyncSessionLocal, engine

    await _fresh_db()
    _, sid, ids = await _make_chat(4)
    await _add_facts(sid, [("Эльвира пообещала вернуть кинжал", ids[1])])

    async def dead_embed(texts, connection=None):
        return None  # так embed() сообщает о сбое или таймауте

    with patch("backend.llm_gateway.embed", new=dead_embed):
        async with AsyncSessionLocal() as db:
            stats: dict = {}
            got = await hr.recall(db, sid, "Где Эльвира?", {"embedding_model": "emb"}, stats=stats)
    assert stats["mode"] == "lexical"
    assert [r["content"] for r in got] == ["Эльвира пообещала вернуть кинжал"]
    await engine.dispose()


async def test_backfill_gives_old_facts_vectors_of_new_model():
    from backend import models
    from backend.database import AsyncSessionLocal, engine

    await _fresh_db()
    _, sid, ids = await _make_chat(2)
    await _add_facts(sid, [("Эльвира хранит кинжал", ids[0]), ("Таверна у ворот", ids[1])])

    async def fake_embed(texts, connection=None):
        return _keyword_vectors(texts)

    with patch("backend.llm_gateway.embed", new=fake_embed):
        async with AsyncSessionLocal() as db:
            done = await hr.backfill_embeddings(db, sid, {"embedding_model": "fake-emb"})
            rows = (await db.execute(select(models.HoraeFact).where(
                models.HoraeFact.session_id == sid))).scalars().all()
    assert done == 2 and all(r.embed_model == "fake-emb" and r.embedding for r in rows)
    await engine.dispose()


async def _build(sid, char_id, **kw):
    from backend import models
    from backend.database import AsyncSessionLocal
    from backend.horae_memory import build_context_from_db

    report: dict = {}
    async with AsyncSessionLocal() as db:
        sess = await db.get(models.ChatSession, sid)
        character = await db.get(models.Character, char_id)
        messages = await build_context_from_db(
            db, sess, character, kw.pop("user_message", "дальше"), None, 200_000,
            report=report, **kw,
        )
    return messages, report


async def _set_summary(sid: int, last_id: int) -> None:
    from backend import models
    from backend.database import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        db.add(models.HoraeEntry(
            session_id=sid, category="summary", title="Память чата (авто)",
            content="Герои пришли в Дольн.", always_on=True, enabled=True,
            meta={"last_message_id": last_id, "v": 2},
        ))
        await db.commit()


async def test_active_window_drops_only_summarised_history():
    from backend.database import engine

    await _fresh_db()
    char_id, sid, ids = await _make_chat(60, prefix="старая реплика")
    await _set_summary(sid, ids[39])  # сводка учла первые 40 сообщений

    messages, report = await _build(sid, char_id)
    mem = report["memory"]
    assert mem["dropped"] == 40 and mem["covered_upto"] == ids[39]
    assert mem["window"] == hr.DEFAULT_WINDOW and mem["facts_mode"] == "lexical"
    text = str(messages)
    assert "старая реплика 39'" not in text and "старая реплика 40" in text
    assert "ХРОНИКА И СОСТОЯНИЕ ЧАТА" in text  # выброшенное живёт в сводке
    await engine.dispose()


async def test_passed_history_without_ids_is_never_trimmed():
    """
    Переданная история без id не сопоставляется с чатом «на глаз»: иначе история,
    собранная не с начала чата, выбросила бы сообщения, которых нет в сводке.
    """
    from backend.database import engine

    await _fresh_db()
    char_id, sid, ids = await _make_chat(60)
    await _set_summary(sid, ids[-1])
    history = [{"role": "user", "content": f"чужая {i}"} for i in range(50)]

    _, report = await _build(sid, char_id, history=history)
    assert report["memory"]["dropped"] == 0

    _, report = await _build(sid, char_id, history=history, history_ids=ids[:50])
    assert report["memory"]["dropped"] > 0
    await engine.dispose()


async def test_recalled_facts_reach_the_model_and_the_inspector():
    from backend.database import engine

    await _fresh_db()
    char_id, sid, ids = await _make_chat(60)
    await _set_summary(sid, ids[-1])
    await _add_facts(sid, [("Эльвира пообещала вернуть Артуру кинжал", ids[3])])

    messages, report = await _build(sid, char_id, user_message="А где сейчас Эльвира?")
    assert "HORAE RECALLED MEMORY" in str(messages)
    assert report["recalled"][0]["content"].startswith("Эльвира")
    assert set(report["recalled"][0]) == {"content", "score", "similarity"}

    messages, _ = await _build(sid, char_id, user_message="Какая сегодня погода?")
    assert "HORAE RECALLED MEMORY" not in str(messages)
    await engine.dispose()


async def test_long_memory_failure_does_not_break_the_turn():
    """Сломанная память выключается, а не роняет ход: история идёт целиком."""
    from backend.database import engine

    await _fresh_db()
    char_id, sid, ids = await _make_chat(60, prefix="реплика")
    await _set_summary(sid, ids[-1])

    with patch("backend.horae_recall.window_start", side_effect=RuntimeError("сломалось")):
        messages, report = await _build(sid, char_id)
    assert "реплика 0" in str(messages)
    assert report["memory"]["dropped"] == 0 and report["memory"]["facts_mode"] == "off"

    async def broken_recall(*a, **kw):  # noqa: ANN002, ANN003
        raise RuntimeError("и это сломалось")

    with patch("backend.horae_recall.recall", new=broken_recall):
        messages, _ = await _build(sid, char_id)
    assert messages[-1]["content"] == "дальше"
    await engine.dispose()


async def test_facts_can_be_switched_off():
    from backend import models
    from backend.database import AsyncSessionLocal, engine

    await _fresh_db()
    char_id, sid, ids = await _make_chat(10)
    await _add_facts(sid, [("Эльвира пообещала вернуть кинжал", ids[0])])
    async with AsyncSessionLocal() as db:
        row = await db.get(models.AppSetting, "ui")
        before = dict(row.value or {}) if row else None
        if row is None:
            db.add(models.AppSetting(key="ui", value={"horae_facts": False}))
        else:
            row.value = {**(row.value or {}), "horae_facts": False}
        await db.commit()
    try:
        messages, report = await _build(sid, char_id, user_message="Где Эльвира?")
        assert "HORAE RECALLED MEMORY" not in str(messages)
        assert report["memory"]["facts_enabled"] is False
        assert report["memory"]["facts_mode"] == "off"
    finally:
        async with AsyncSessionLocal() as db:
            row = await db.get(models.AppSetting, "ui")
            if before is None:
                await db.delete(row)
            else:
                row.value = before
            await db.commit()
        await engine.dispose()


def test_inspector_endpoint_returns_memory_report(client):
    cid = client.post("/api/characters", json={"name": "Инспектор"}).json()["id"]
    sid = client.post(f"/api/sessions?character_id={cid}").json()["session_id"]
    rep = client.get(f"/api/sessions/{sid}/context").json()
    assert {"window", "dropped", "covered_upto", "facts_enabled", "facts_mode"} <= set(rep["memory"])
    assert rep["recalled"] == []


def test_deleting_chat_deletes_its_facts(client):
    from backend import models
    from backend.database import AsyncSessionLocal

    cid = client.post("/api/characters", json={"name": "Удаляемый"}).json()["id"]
    sid = client.post(f"/api/sessions?character_id={cid}").json()["session_id"]

    async def add():
        async with AsyncSessionLocal() as db:
            db.add(models.HoraeFact(session_id=sid, content="Факт на удаление", source_message_id=1))
            await db.commit()

    async def count():
        async with AsyncSessionLocal() as db:
            return len((await db.execute(select(models.HoraeFact).where(
                models.HoraeFact.session_id == sid))).scalars().all())

    client.portal.call(add)
    assert client.portal.call(count) == 1
    client.delete(f"/api/sessions/{sid}")
    assert client.portal.call(count) == 0


def test_connection_keeps_memory_models(client):
    """Поля памяти в подключении сохраняются, а не отбрасываются схемой запроса."""
    before = client.get("/api/settings/connection").json()
    try:
        got = client.put("/api/settings/connection", json={
            **before, "summary_model": "fast-model", "embedding_model": "text-emb",
        }).json()
        assert got["summary_model"] == "fast-model" and got["embedding_model"] == "text-emb"
        again = client.get("/api/settings/connection").json()
        assert again["embedding_model"] == "text-emb"
    finally:
        client.put("/api/settings/connection", json={
            **before, "summary_model": "", "embedding_model": "",
        })


# ==================== Поиск по словам: точность на ролевом чате ====================

# Двенадцать фактов «настоящего» чата: Артур и Эльвира — главные герои, их имена
# стоят в трети фактов и больше.
_RP_FACTS = [
    _fact("Артур носит серебряный медальон матери", 10),
    _fact("Артур ранен в левое плечо стрелой", 20),
    _fact("У Артура в рюкзаке три факела", 30),
    _fact("Эльвира боится глубокой воды", 40),
    _fact("Эльвира пообещала Артуру вернуть кинжал до полнолуния", 50),
    _fact("Мельник Густав — отец Эльвиры", 60),
    _fact("Гильдия воров должна Артуру двести золотых", 70),
    _fact("Таверна «Сломанный рог» стоит у северных ворот Дольна", 80),
    _fact("Старый маг Ирвен знает тайну медальона", 90),
    _fact("Эльвира и Артур поссорились из-за карты", 100),
    _fact("Орки сожгли мост через реку Вельду", 110),
    _fact("Капитан стражи Борен подкуплен гильдией", 120),
]
_TAIL = "Артур поднялся и посмотрел на Эльвиру."


@pytest.mark.parametrize("reply", ["ок", "Иду дальше", "Да"])
def test_lexical_names_from_the_tail_alone_recall_nothing(reply):
    """
    Прошлая реплика персонажа почти всегда называет героев. Раньше совпадения по
    одним их именам хватало, чтобы на «ок» заполнить блок памяти пятью
    посторонними фактами — и так почти на каждом ходу.
    """
    assert hr.rank_facts(_RP_FACTS, 200, query_text=reply, context_text=_TAIL) == []


def test_lexical_ubiquitous_names_do_not_fade_in_with_chat_size():
    """Чем длиннее чат, тем больше фактов про героев; шум от этого не должен расти."""
    import random

    rnd = random.Random(7)

    def word():
        return "".join(rnd.choice("бвгджзклмнпрстфхцчшщ") + rnd.choice("аеиоуы") for _ in range(4))

    for n in (30, 300, 1000):
        facts = []
        for i in range(n):
            names = (["Артур"] if rnd.random() < 0.5 else []) + (["Эльвира"] if rnd.random() < 0.35 else [])
            facts.append(_fact(" ".join(names + [word() for _ in range(4)]), i + 1))
        assert hr.rank_facts(facts, n + 10, query_text="Иду дальше", context_text=_TAIL) == []
        assert hr.rank_facts(facts, n + 10, query_text="ок", context_text=_TAIL) == []


def test_lexical_tail_resolves_a_reply_without_own_words():
    """«А что с ним?» — своих слов нет, и тогда факт ищется по хвосту."""
    got = hr.rank_facts(_RP_FACTS, 200, query_text="А что с ним?",
                        context_text="В дверях стоял старый маг Ирвен.")
    assert [r["content"] for r in got] == ["Старый маг Ирвен знает тайну медальона"]


def test_lexical_rare_name_and_topic_still_found():
    got = hr.rank_facts(_RP_FACTS, 200, query_text="Что знает Ирвен?", context_text=_TAIL)
    assert got and got[0]["content"] == "Старый маг Ирвен знает тайну медальона"
    got = hr.rank_facts(_RP_FACTS, 200, query_text="Помнишь, что ты мне обещала?",
                        context_text=_TAIL)
    assert [r["content"] for r in got] == [
        "Эльвира пообещала Артуру вернуть кинжал до полнолуния",
    ]


@pytest.mark.parametrize("query, fact", [
    ("Спроси Иру про амулет", "Ира отдала Лису свой амулет."),
    ("Что с Аней?", "Аня прячет ключ от башни под половицей."),
    ("Позови Аню", "Аня прячет ключ от башни под половицей."),
    ("А что с Каем?", "Кай поклялся отомстить Ордену."),
    ("Помнишь клятву Кая?", "Кай поклялся отомстить Ордену."),
])
def test_lexical_short_names_decline(query, fact):
    """Короткие имена склоняются так же, как длинные: «Ира/Иру», «Аня/Аней», «Кай/Каем»."""
    got = hr.rank_facts([_fact(fact, 5)], 10, query_text=query)
    assert [r["content"] for r in got] == [fact]


@pytest.mark.parametrize("query, fact", [
    ("обещала", "Эльвира пообещала вернуть кинжал"),   # приставка
    ("магом", "Ирвен — старый маг"),                   # окончание приросло
    ("дочери", "Эльвира — дочь мельника"),              # беглое «ер»
    ("кинжалом", "Эльвира вернула кинжал"),
    ("амулета", "Ира отдала амулет"),
])
def test_lexical_word_forms_meet(query, fact):
    assert hr.lexical_similarities(query, [_fact(fact)])[0] > 0


@pytest.mark.parametrize("query, fact", [
    ("рукав", "Артур поранил руку"),    # другое слово с тем же началом
    ("страж", "Артура охватил страх"),
])
def test_lexical_different_words_do_not_meet(query, fact):
    assert hr.lexical_similarities(query, [_fact(fact)])[0] == 0


def test_lexical_capital_at_sentence_start_is_not_a_name():
    """«Старый» в начале факта — не имя: «Он старался не шуметь» его не вспоминает."""
    facts = [_fact("Старый маг Ирвен знает тайну медальона.", 5)]
    assert hr.rank_facts(facts, 10, query_text="Он старался не шуметь") == []
    tower = _RP_FACTS + [_fact("Башня Ирвена стоит на краю болота.", 130)]
    assert hr.rank_facts(tower, 200, query_text="Мне нужна башня? нет, просто отдохнуть") == []
    # А имя в начале факта узнаётся по заглавной в разговоре.
    got = hr.rank_facts([_fact("Эльвира пообещала вернуть Артуру кинжал", 5)], 10,
                        query_text="А где сейчас Эльвира?")
    assert got


# ==================== Векторы: включение посреди жизни чата ====================

async def test_recall_stays_lexical_until_vectors_are_backfilled():
    """
    Эмбеддинги включили в чате с сотней старых фактов. Раньше поиск сразу шёл
    по векторам — то есть по горстке свежих фактов, у которых вектор уже есть, —
    и вся старая память была недоступна сотни сообщений.
    """
    from backend.database import AsyncSessionLocal, engine

    await _fresh_db()
    _, sid, ids = await _make_chat(4)
    old = [(f"Старый факт номер {i} про погоду", ids[0]) for i in range(99)]
    await _add_facts(sid, old + [("Эльвира пообещала вернуть кинжал", ids[0])])

    async def fake_embed(texts, connection=None):
        return _keyword_vectors(texts)

    conn = {"embedding_model": "fake-emb"}
    with patch("backend.llm_gateway.embed", new=fake_embed):
        async with AsyncSessionLocal() as db:
            # Свежий факт уже с вектором, сто старых — ещё нет.
            await hr.store_facts(db, sid, ["Гильдия ждёт долг"], ids[3], conn)
            stats: dict = {}
            got = await hr.recall(db, sid, "Где Эльвира?", conn, stats=stats)
            assert stats["mode"] == "lexical" and stats.get("backfilling") is True
            assert [r["content"] for r in got] == ["Эльвира пообещала вернуть кинжал"]

            done = await hr.backfill_all(db, sid, conn)
            assert done == 100  # две порции за один запуск, а не одна за проход сводки
            stats = {}
            got = await hr.recall(db, sid, "где эльвира и кинжал", conn, stats=stats)
    assert stats["mode"] == "vector" and "backfilling" not in stats
    assert [r["content"] for r in got][0] == "Эльвира пообещала вернуть кинжал"
    await engine.dispose()


# ==================== Отсев фактов по тому, что модель видит ====================

def test_recalled_facts_filtered_after_budget_trim():
    """
    Отсев фактов «уже в контексте» идёт по первому сообщению, пережившему обрезку
    бюджетом, а не по началу окна: иначе факты сообщений, срезанных бюджетом,
    не попадали ни в контекст, ни в память.
    """
    history = [{"role": "user" if i % 2 == 0 else "assistant", "content": "слово " * 200}
               for i in range(40)]
    ids = list(range(101, 141))
    report: dict = {}
    facts = [
        {"content": "Факт из срезанной части", "source_message_id": 105, "score": 0.5},
        {"content": "Факт из видимой части", "source_message_id": 139, "score": 0.9},
    ]
    messages = assemble_context(character=_char(), horae_records=[], history=history,
                                user_message="дальше", token_budget=3000,
                                recalled_facts=facts, history_ids=ids, report=report)
    assert report["history"]["trimmed"] > 5  # бюджет срезал начало
    assert [f["content"] for f in report["recalled"]] == ["Факт из срезанной части"]
    assert "Факт из видимой части" not in str(messages)


def test_recalled_facts_capped_to_top_k():
    facts = [{"content": f"факт {i}", "source_message_id": i, "score": 1 - i / 100} for i in range(20)]
    report: dict = {}
    assemble_context(character=_char(), horae_records=[], history=[], user_message="x",
                     recalled_facts=facts, report=report)
    assert len(report["recalled"]) == hr.TOP_K


async def test_window_zero_still_recalls_facts_from_trimmed_history():
    """memory_window = 0 — окна нет, но слой фактов обязан работать."""
    from backend import models
    from backend.database import AsyncSessionLocal, engine
    from backend.horae_memory import build_context_from_db

    await _fresh_db()
    char_id, sid, ids = await _make_chat(60, prefix="длинная реплика " * 40)
    await _add_facts(sid, [("Эльвира пообещала вернуть Артуру кинжал", ids[3])])
    async with AsyncSessionLocal() as db:
        row = await db.get(models.AppSetting, "ui")
        before = dict(row.value or {}) if row else None
        if row is None:
            db.add(models.AppSetting(key="ui", value={"memory_window": 0}))
        else:
            row.value = {**(row.value or {}), "memory_window": 0}
        await db.commit()
    try:
        report: dict = {}
        async with AsyncSessionLocal() as db:
            sess = await db.get(models.ChatSession, sid)
            character = await db.get(models.Character, char_id)
            messages = await build_context_from_db(
                db, sess, character, "А где сейчас Эльвира?", None, 4000, report=report,
            )
        assert report["memory"]["dropped"] == 0 and report["history"]["trimmed"] > 3
        assert "HORAE RECALLED MEMORY" in str(messages)
    finally:
        async with AsyncSessionLocal() as db:
            row = await db.get(models.AppSetting, "ui")
            if before is None:
                await db.delete(row)
            else:
                row.value = before
            await db.commit()
        await engine.dispose()


async def test_window_ignores_summary_that_is_not_injected():
    """Сняли «всегда» с авто-сводки — её нет в контексте, и окно ничего не выбрасывает."""
    from backend import models
    from backend.database import AsyncSessionLocal, engine

    await _fresh_db()
    char_id, sid, ids = await _make_chat(60, prefix="старая реплика")
    await _set_summary(sid, ids[-1])
    async with AsyncSessionLocal() as db:
        entry = (await db.execute(select(models.HoraeEntry).where(
            models.HoraeEntry.session_id == sid))).scalars().first()
        entry.always_on = False
        await db.commit()

    messages, report = await _build(sid, char_id)
    assert report["memory"]["dropped"] == 0
    assert "ХРОНИКА И СОСТОЯНИЕ ЧАТА" not in str(messages)
    assert "старая реплика 0'" in str(messages)
    await engine.dispose()


# ==================== Фоновая сводка ====================

async def test_summary_skips_active_window_and_takes_long_messages_whole():
    """
    Сводка сжимает только то, что старше активного окна: свежий ответ ещё могут
    перегенерировать, продолжить или поправить, и в память должна попасть
    окончательная версия. И сообщение идёт в сводку целиком — раньше оно
    резалось до 1500 символов, а указатель всё равно считал его учтённым.
    """
    from backend import main, models
    from backend.database import AsyncSessionLocal, engine

    await _fresh_db()
    _, sid, ids = await _make_chat(12 + hr.DEFAULT_WINDOW, prefix="событие")
    async with AsyncSessionLocal() as db:
        m = await db.get(models.Message, ids[0])
        m.content = "Долгая сцена. " * 300 + "И в конце Артур поклялся вернуться за сестрой."
        await db.commit()

    seen: list[str] = []

    async def fake_complete(messages, params=None, connection=None, kind="service"):
        seen.append(messages[-1]["content"])
        return "Сводка."

    with patch("backend.main.complete", new=fake_complete):
        await main._maybe_update_summary(sid)

    assert seen and "поклялся вернуться за сестрой" in seen[0]
    assert "событие 11" in seen[0]
    assert "событие 12" not in seen[0]  # первое сообщение окна ждёт
    async with AsyncSessionLocal() as db:
        entry = (await db.execute(select(models.HoraeEntry).where(
            models.HoraeEntry.session_id == sid,
            models.HoraeEntry.category == "summary"))).scalars().first()
    assert entry.meta["last_message_id"] == ids[11]
    await engine.dispose()


async def test_summary_fast_model_keeps_safety_off():
    """
    Быстрая модель сводки подставляется в подключение, а params остаются None.
    Через GenerationParams(model=…) вызов терял «служебный» статус: фильтры
    безопасности включались, провайдер блокировал сводку ролевого чата, и
    память замирала.
    """
    from backend import main
    from backend.database import engine

    await _fresh_db()
    _, sid, _ = await _make_chat(12 + hr.DEFAULT_WINDOW)
    calls = []

    async def fake_complete(messages, params=None, connection=None, kind="service"):
        calls.append((params, dict(connection or {})))
        return "Сводка."

    async def fake_connection(db):
        return {"default_model": "chat-model", "summary_model": "fast-model"}

    with patch("backend.main.complete", new=fake_complete), \
            patch("backend.main.get_connection", new=fake_connection):
        await main._maybe_update_summary(sid)

    assert len(calls) == 2  # сводка и факты
    assert all(p is None for p, _ in calls)
    assert all(c["default_model"] == "fast-model" for _, c in calls)
    await engine.dispose()


def test_deleting_fresh_messages_clamps_summary_pointer(client):
    """
    id в SQLite переиспользуются: удалили два последних сообщения — следующие два
    получат те же id. Раньше указатель сводки стоял на месте, и переписанный
    заново ответ считался уже учтённым: в память он не попадал, а окно потом
    выбрасывало его из контекста. В памяти же оставался удалённый текст.
    """
    from backend import models
    from backend.database import AsyncSessionLocal

    cid = client.post("/api/characters", json={"name": "Переписчик"}).json()["id"]
    sid = client.post(f"/api/sessions?character_id={cid}").json()["session_id"]

    async def setup():
        async with AsyncSessionLocal() as db:
            msgs = [models.Message(session_id=sid, role="user" if i % 2 == 0 else "assistant",
                                   content=f"ход {i}") for i in range(6)]
            db.add_all(msgs)
            await db.commit()
            ids = [m.id for m in msgs]
            db.add(models.HoraeEntry(
                session_id=sid, category="summary", title="📜 Память чата (авто)",
                content="Сводка", always_on=True, enabled=True,
                meta={"last_message_id": ids[-1], "v": 2},
            ))
            db.add(models.HoraeFact(session_id=sid, content="Факт из удалённого ответа",
                                    source_message_id=ids[-1]))
            db.add(models.HoraeFact(session_id=sid, content="Старый факт",
                                    source_message_id=ids[1]))
            await db.commit()
            return ids

    async def state():
        async with AsyncSessionLocal() as db:
            entry = (await db.execute(select(models.HoraeEntry).where(
                models.HoraeEntry.session_id == sid,
                models.HoraeEntry.category == "summary"))).scalars().first()
            facts = (await db.execute(select(models.HoraeFact.content).where(
                models.HoraeFact.session_id == sid))).scalars().all()
            return entry.meta["last_message_id"], sorted(facts)

    async def add_message():
        async with AsyncSessionLocal() as db:
            m = models.Message(session_id=sid, role="user", content="переписанный ход")
            db.add(m)
            await db.commit()
            return m.id

    ids = client.portal.call(setup)
    # Удаление сообщения старше указателя его не трогает: чат не пересобирается.
    assert client.delete(f"/api/messages/{ids[2]}").status_code == 200
    assert client.portal.call(state) == (ids[-1], ["Старый факт", "Факт из удалённого ответа"])

    client.delete(f"/api/messages/{ids[-1]}")
    client.delete(f"/api/messages/{ids[-2]}")
    pointer, facts = client.portal.call(state)
    assert pointer == ids[-3]
    assert facts == ["Старый факт"]
    new_id = client.portal.call(add_message)
    assert new_id > pointer  # новое сообщение снова «свежее» для сводки


# ==================== Сводка старого формата (до 2.4.0) ====================

async def _set_legacy_summary(sid: int, last_id: int, content: str = "СТАРАЯ СВОДКА") -> None:
    """Сводка, как её оставлял сводчик до 2.4.0: указатель без версии формата."""
    from backend import models
    from backend.database import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        db.add(models.HoraeEntry(
            session_id=sid, category="summary", title="📜 Память чата (авто)",
            content=content, always_on=True, enabled=True,
            keywords=[f"last:{last_id}", "__auto__"], meta={"last_message_id": last_id},
        ))
        await db.commit()


async def _summary_entry(sid: int):
    from backend import models
    from backend.database import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        return (await db.execute(select(models.HoraeEntry).where(
            models.HoraeEntry.session_id == sid,
            models.HoraeEntry.category == "summary"))).scalars().first()


def test_trusted_pointer_only_for_current_format():
    assert hr.trusted_pointer({"last_message_id": 50, "v": hr.SUMMARY_FORMAT}) == 50
    assert hr.trusted_pointer({"last_message_id": 50}) == 0          # до 2.4.0
    assert hr.trusted_pointer({"last_message_id": "мусор", "v": 2}) == 0
    assert hr.trusted_pointer(None) == 0 and hr.trusted_pointer([]) == 0


async def test_legacy_summary_pointer_does_not_trim_history():
    """
    Сводчик до 2.4.0 резал реплики до 1500 символов и всё равно ставил указатель
    на последнее сообщение. Поверь ему окно — и сразу после обновления из
    контекста ушли бы хвосты длинных ответов, которых в сводке нет.
    """
    from backend.database import engine

    await _fresh_db()
    char_id, sid, ids = await _make_chat(60, prefix="давняя реплика")
    await _set_legacy_summary(sid, ids[-1])

    messages, report = await _build(sid, char_id)
    assert report["memory"]["dropped"] == 0 and report["memory"]["covered_upto"] == 0
    assert "давняя реплика 0" in str(messages)       # история целиком, как до 2.4.0
    assert "СТАРАЯ СВОДКА" in str(messages)          # и старая сводка по-прежнему в хвосте
    await engine.dispose()


async def test_legacy_summary_is_rebuilt_before_it_is_replaced():
    """
    Старая сводка пересобирается новым сводчиком с нуля, но в контекст до конца
    пересборки идёт СТАРАЯ: подмени её сразу — и середина чата на время догонки
    не была бы ни в пересказе, ни дословно. Подмена — когда новая догнала.
    """
    from backend import main
    from backend.database import AsyncSessionLocal, engine

    await _fresh_db()
    _, sid, ids = await _make_chat(40 + hr.DEFAULT_WINDOW)
    async with AsyncSessionLocal() as db:  # по 5 сообщений на кусок сводки: 8 кусков
        from backend import models
        for mid in ids:
            (await db.get(models.Message, mid)).content = f"сцена #{mid} " + "текст " * 780
        await db.commit()
    await _set_legacy_summary(sid, ids[-1])

    prompts: list[str] = []

    async def fake_complete(messages, params=None, connection=None, kind="service"):
        prompts.append(messages[-1]["content"])
        return f"НОВАЯ СВОДКА {len(prompts)}"

    with patch("backend.main.complete", new=fake_complete):
        await main._maybe_update_summary(sid)
    entry = await _summary_entry(sid)
    # Пересборка идёт с нуля, а не дописывает старый пересказ.
    assert prompts[0].startswith("[Текущая память]\n(пока пусто)")
    assert f"сцена #{ids[0]} " in prompts[0]
    # Первый запуск бэклог не догнал: в контексте по-прежнему старая сводка.
    assert entry.content == "СТАРАЯ СВОДКА" and entry.meta.get("v") is None
    assert entry.meta["rebuild"]["last_message_id"] < ids[39]
    assert hr.trusted_pointer(entry.meta) == 0

    with patch("backend.main.complete", new=fake_complete):
        await main._maybe_update_summary(sid)
    entry = await _summary_entry(sid)
    assert entry.content.startswith("НОВАЯ СВОДКА") and "rebuild" not in entry.meta
    assert entry.meta["v"] == hr.SUMMARY_FORMAT
    # 35 из 40 сообщений старше окна пересказаны; остаток меньше summary_every
    # ждёт, как и у обычного сводчика, и пока лежит в контексте дословно.
    assert hr.trusted_pointer(entry.meta) == ids[34]
    await engine.dispose()


async def test_oversized_message_is_cut_to_head_and_tail():
    """
    Сообщение длиннее куска сводки шло целиком отдельным запросом. Больше окна
    быстрой модели — запрос падал, указатель стоял, и каждый ход повторял ту же
    платную неудачу. Теперь в кусок идут начало и конец сообщения.
    """
    from backend import main, models
    from backend.database import AsyncSessionLocal, engine

    await _fresh_db()
    _, sid, ids = await _make_chat(12 + hr.DEFAULT_WINDOW, prefix="событие")
    async with AsyncSessionLocal() as db:
        m = await db.get(models.Message, ids[0])
        m.content = "НАЧАЛО документа. " + "строка " * 20000 + " КОНЕЦ: Артур поклялся вернуться."
        await db.commit()

    seen: list[str] = []

    async def fake_complete(messages, params=None, connection=None, kind="service"):
        seen.append(messages[-1]["content"])
        return "Сводка."

    with patch("backend.main.complete", new=fake_complete):
        await main._maybe_update_summary(sid)
    first = seen[0]
    assert "НАЧАЛО документа" in first and "Артур поклялся вернуться" in first
    assert "середина длинного сообщения пропущена" in first
    assert len(first) < main._SUMMARY_CHUNK_CHARS + 7000  # кусок + прежняя память
    await engine.dispose()


async def test_rebuild_that_caught_up_replaces_summary_without_llm_call():
    """
    Остаток бэклога меньше порога summary_every ждёт и у обычного сводчика.
    Пересборка, догнавшая бэклог до этого остатка, подменяет старую сводку
    сразу — не дожидаясь, пока в чате наберётся ещё summary_every сообщений.
    """
    from backend import main, models
    from backend.database import AsyncSessionLocal, engine

    await _fresh_db()
    _, sid, ids = await _make_chat(40 + hr.DEFAULT_WINDOW)
    async with AsyncSessionLocal() as db:
        db.add(models.HoraeEntry(
            session_id=sid, category="summary", title="📜 Память чата (авто)",
            content="СТАРАЯ СВОДКА", always_on=True, enabled=True,
            meta={"last_message_id": ids[-1],
                  "rebuild": {"content": "ПЕРЕСОБРАННАЯ", "last_message_id": ids[35]}},
        ))
        await db.commit()

    calls = []

    async def fake_complete(messages, params=None, connection=None, kind="service"):
        calls.append(kind)
        return "не должна вызываться"

    with patch("backend.main.complete", new=fake_complete):
        await main._maybe_update_summary(sid)
    entry = await _summary_entry(sid)
    assert calls == []
    assert entry.content == "ПЕРЕСОБРАННАЯ" and "rebuild" not in entry.meta
    assert hr.trusted_pointer(entry.meta) == ids[35]
    await engine.dispose()


async def test_imported_summary_without_pointer_is_not_replaced_early():
    """
    У сводки из импорта (нативный экспорт meta не несёт) указатель неизвестен.
    Подмена «по указателю» срабатывала после первого же куска, и хроника всего
    чата менялась на пересказ его первых сообщений.
    """
    from backend import main, models
    from backend.database import AsyncSessionLocal, engine

    await _fresh_db()
    _, sid, ids = await _make_chat(40 + hr.DEFAULT_WINDOW)
    async with AsyncSessionLocal() as db:
        for mid in ids:
            (await db.get(models.Message, mid)).content = f"сцена #{mid} " + "текст " * 780
        db.add(models.HoraeEntry(
            session_id=sid, category="summary", title="📜 Память чата (авто)",
            content="ХРОНИКА ИЗ ИМПОРТА", always_on=True, enabled=True,
            keywords=["__auto__"], meta={},
        ))
        await db.commit()

    async def fake_complete(messages, params=None, connection=None, kind="service"):
        return "частичный пересказ"

    with patch("backend.main.complete", new=fake_complete):
        await main._summary_pass(sid)  # один кусок из восьми
    entry = await _summary_entry(sid)
    assert entry.content == "ХРОНИКА ИЗ ИМПОРТА"
    assert entry.meta["rebuild"]["last_message_id"] == ids[4]
    assert hr.trusted_pointer(entry.meta) == 0
    await engine.dispose()


async def test_restarted_summary_does_not_re_extract_facts():
    """
    Удалили запись «Память чата (авто)» — сводка начинается заново, а факты
    остаются. Повторный разбор того же куска давал пересказанные другими
    словами дубли, и они вытесняли другие факты из отбора.
    """
    from backend import main, models
    from backend.database import AsyncSessionLocal, engine

    await _fresh_db()
    _, sid, ids = await _make_chat(12 + hr.DEFAULT_WINDOW, prefix="событие")
    kinds: list[str] = []

    async def fake_complete(messages, params=None, connection=None, kind="service"):
        is_facts = messages[0]["content"] == hr.FACTS_PROMPT
        kinds.append("facts" if is_facts else "summary")
        return f"Факт номер {len(kinds)} про Артура" if is_facts else "Сводка."

    with patch("backend.main.complete", new=fake_complete):
        await main._maybe_update_summary(sid)
    assert kinds == ["summary", "facts"]
    async with AsyncSessionLocal() as db:
        entry = await _summary_entry(sid)
        await db.delete(await db.get(models.HoraeEntry, entry.id))
        await db.commit()

    kinds.clear()
    with patch("backend.main.complete", new=fake_complete):
        await main._maybe_update_summary(sid)
    assert kinds == ["summary"]  # тот же кусок: факты из него уже есть
    async with AsyncSessionLocal() as db:
        n = len((await db.execute(select(models.HoraeFact.id).where(
            models.HoraeFact.session_id == sid))).scalars().all())
    assert n == 1
    await engine.dispose()


async def test_chunk_changed_during_model_call_is_not_recorded():
    """
    Пока модель считала, сообщение куска переписали (или удалили, а id занял
    новый ответ). Указатель лёг бы на текст, которого сводка не видела, и окно
    выбросило бы его навсегда. Такой проход не записывается и повторяется.
    """
    from backend import main, models
    from backend.database import AsyncSessionLocal, engine

    await _fresh_db()
    _, sid, ids = await _make_chat(12 + hr.DEFAULT_WINDOW, prefix="событие")

    async def meddling_complete(messages, params=None, connection=None, kind="service"):
        async with AsyncSessionLocal() as db:
            (await db.get(models.Message, ids[5])).content = "ПЕРЕПИСАНО во время сводки"
            await db.commit()
        return "Сводка по старому тексту."

    with patch("backend.main.complete", new=meddling_complete):
        await main._maybe_update_summary(sid)
    assert await _summary_entry(sid) is None
    async with AsyncSessionLocal() as db:
        assert not (await db.execute(select(models.HoraeFact.id).where(
            models.HoraeFact.session_id == sid))).scalars().all()

    seen: list[str] = []

    async def fake_complete(messages, params=None, connection=None, kind="service"):
        seen.append(messages[-1]["content"])
        return "Сводка."

    with patch("backend.main.complete", new=fake_complete):
        await main._maybe_update_summary(sid)
    assert "ПЕРЕПИСАНО во время сводки" in seen[0]
    assert hr.trusted_pointer((await _summary_entry(sid)).meta) == ids[11]
    await engine.dispose()
