"""
Вспоминание событий Horae — порт vectorManager плагина.

Что индексируется. Одна запись на ответ ИИ: не сырой текст, а документ из его
меты — события, «место · персонажи · дата» и RPG (horae_state.build_document).
Абзац ролевой прозы «про всё сразу» похож на всё понемногу, а короткий
перечень событий хода — ровно на то, о чём он.

Как ищется (на каждом ходу, если в чате есть документы):
  1. Структурный слой — без векторов, по намерению реплики и русским ключевым
     словам плагина: «впервые», «в прошлый раз», подарки, важные предметы и
     события, темы (клятвы, утраты, тайны…) — и соседние ответы ±3.
  2. Смысловой слой — косинус векторов (если задана embedding_model и векторы
     посчитаны у ≥ 90 % документов), иначе лексическое сходство (тот же
     движок, что у фактов, horae_recall.lexical_similarities).
  3. Необязательно — переписывание запроса служебной моделью (INTENT + до 5 Q).
Результаты сливаются RRF (k = 60) с бонусом за общих с текущей сценой
персонажей; можно переранжировать моделью rerank. Лучшие воспоминания с
высоким сходством идут полным текстом исходного ответа.

Сообщения, которые модель видит дословно (активное окно), не вспоминаются:
отсев — здесь по окну и в assemble_context по реальной обрезке бюджетом.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from functools import lru_cache
from pathlib import Path

from sqlalchemy import delete as sql_delete, select

from backend import horae_recall, horae_state as hs, horae_time
from backend import models

log = logging.getLogger("aichat.horae")

RRF_K = 60
CONTEXT_RADIUS = 3            # соседние ответы вокруг структурного попадания
DEDUP_COSINE = 0.92           # почти одинаковые документы — один
VECTOR_COVERAGE = 0.9         # доля документов с векторами для смыслового режима
EMBED_BATCH = 32
MAX_EMBED_PER_PASS = 256
CANDIDATE_FACTOR = 3          # кандидатов больше top_k: часть отсеет обрезка бюджетом
USER_QUERY_CHARS = 300
PREV_USER_CHARS = 800

HEADER = ("[Воспоминание — фрагменты из прошлого, связанные с текущей сценой. Только для справки, "
          "не часть текущего контекста]")


@lru_cache(maxsize=1)
def keywords() -> dict:
    path = Path(__file__).with_name("horae_prompts_ru") / "recall_keywords.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def doc_hash(text: str) -> str:
    return hashlib.md5((text or "").encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Индекс
# ---------------------------------------------------------------------------
def _embedding_model(connection) -> str:
    return ((connection or {}).get("embedding_model") or "").strip()


async def _embed(texts: list[str], connection) -> list[list[float]] | None:
    from backend import llm_gateway

    vectors = await llm_gateway.embed(texts, connection)
    if not vectors:
        return None
    return [horae_recall._unit(v) for v in vectors]


async def sync_documents(db, session_id: int, connection: dict | None = None, *,
                         only_ids: list[int] | None = None, embed: bool = True) -> int:
    """
    Привести документы чата в соответствие метам сообщений: новые — добавить,
    изменившиеся (правка, свайп) — обновить, без меты или побочные — удалить,
    недостающие векторы — досчитать (не больше MAX_EMBED_PER_PASS за раз).
    Возвращает, сколько документов изменено.

    Векторы здесь не читаются (у тысяч документов это десятки мегабайт JSON
    на каждый ход) — только id, сообщение и хэш текста.
    """
    from sqlalchemy import update

    from backend.horae_engine import active_meta, is_side

    q = select(models.Message.id, models.Message.role, models.Message.horae, models.Message.active_swipe) \
        .where(models.Message.session_id == session_id)
    if only_ids:
        q = q.where(models.Message.id.in_(only_ids))
    rows = (await db.execute(q)).all()
    docs_q = select(models.HoraeMemoryDoc.id, models.HoraeMemoryDoc.message_id,
                    models.HoraeMemoryDoc.doc_hash).where(
        models.HoraeMemoryDoc.session_id == session_id, models.HoraeMemoryDoc.message_id.is_not(None))
    if only_ids:
        docs_q = docs_q.where(models.HoraeMemoryDoc.message_id.in_(only_ids))
    existing = {d.message_id: d for d in (await db.execute(docs_q)).all()}
    changed = 0
    doomed: list[int] = []
    for r in rows:
        meta = active_meta(r.horae, r.active_swipe)
        text = hs.build_document(meta) if (r.role == "assistant" and not is_side(r.horae)) else ""
        doc = existing.pop(r.id, None)
        if not text:
            if doc is not None:
                doomed.append(doc.id)
            continue
        h = doc_hash(text)
        if doc is None:
            db.add(models.HoraeMemoryDoc(session_id=session_id, message_id=r.id, document=text, doc_hash=h))
            changed += 1
        elif doc.doc_hash != h:
            await db.execute(update(models.HoraeMemoryDoc).where(models.HoraeMemoryDoc.id == doc.id).values(
                document=text, doc_hash=h, embedding=None, embed_model=""))
            changed += 1
    if not only_ids:
        doomed.extend(d.id for d in existing.values())   # сообщение удалено мимо хуков
    if doomed:
        await db.execute(sql_delete(models.HoraeMemoryDoc).where(models.HoraeMemoryDoc.id.in_(doomed)))
        changed += len(doomed)
    await db.flush()
    if embed and _embedding_model(connection):
        await embed_missing(db, session_id, connection)
    return changed


async def embed_missing(db, session_id: int, connection) -> int:
    """Досчитать векторы документов без векторов текущей модели (не больше MAX_EMBED_PER_PASS)."""
    from sqlalchemy import or_, update

    model = _embedding_model(connection)
    if not model:
        return 0
    todo = (await db.execute(
        select(models.HoraeMemoryDoc.id, models.HoraeMemoryDoc.document).where(
            models.HoraeMemoryDoc.session_id == session_id,
            or_(models.HoraeMemoryDoc.embedding.is_(None), models.HoraeMemoryDoc.embed_model != model),
        ).limit(MAX_EMBED_PER_PASS)
    )).all()
    todo = [d for d in todo if d.document]
    done = 0
    for i in range(0, len(todo), EMBED_BATCH):
        batch = todo[i:i + EMBED_BATCH]
        vectors = await _embed([d.document for d in batch], connection)
        if not vectors or len(vectors) != len(batch):
            break  # провайдер недоступен — поиск по словам, досчитаем позже
        for d, v in zip(batch, vectors):
            await db.execute(update(models.HoraeMemoryDoc).where(models.HoraeMemoryDoc.id == d.id)
                             .values(embedding=v, embed_model=model))
            done += 1
        await db.flush()
    return done


async def reindex(db, session_id: int, connection) -> int:
    await db.execute(sql_delete(models.HoraeMemoryDoc).where(
        models.HoraeMemoryDoc.session_id == session_id, models.HoraeMemoryDoc.message_id.is_not(None)))
    await db.flush()
    return await sync_documents(db, session_id, connection)


# ---------------------------------------------------------------------------
# Запрос
# ---------------------------------------------------------------------------
_TAG_RE = re.compile(r"<[^>]*>")


def clean_user_query(text: str) -> str:
    return _TAG_RE.sub("", text or "").replace("[", "").replace("]", "").strip()[:USER_QUERY_CHARS]


def state_query(state: dict, last_meta: dict | None) -> str:
    """Текущая ситуация одной строкой: время, место, персонажи в костюмах, события последнего ответа."""
    parts = []
    t = (last_meta or {}).get("time") or {}
    date = t.get("date") or state["time"].get("date") or ""
    time = t.get("time") or state["time"].get("time") or ""
    if date or time:
        parts.append(" ".join(x for x in ("время", date, time) if x))
    if state["scene"].get("location"):
        parts.append(state["scene"]["location"])
    for name in state["scene"].get("characters") or []:
        parts.append(name)
        if state["costumes"].get(name):
            parts.append(state["costumes"][name])
    for ev in (last_meta or {}).get("events") or []:
        if ev.get("text"):
            parts.append(ev["text"])
    return " ".join(parts)


def merged_query(state_q: str, user_q: str) -> str:
    lines = []
    if state_q:
        lines.append(f"[Текущая ситуация] {state_q}")
    if user_q:
        lines.append(f"[Ввод игрока] {user_q}")
    return "\n".join(lines)


def dynamic_threshold(base: float, n_docs: int) -> float:
    """Чем больше документов, тем выше порог: в большом чате случайные совпадения чаще."""
    if n_docs <= 50:
        return base
    return min(0.95, base + min(0.05, math.log10(n_docs / 50) * 0.04))


# ---------------------------------------------------------------------------
# Структурный слой
# ---------------------------------------------------------------------------
def _has_any(text: str, words) -> list[str]:
    return [w for w in words or [] if w and w.lower() in text]


def _name_hit(name: str, text_low: str) -> bool:
    """
    Имя упомянуто с учётом падежа: «Кая» ↔ «Кай», «Мариной» ↔ «Марина».
    Основы — стеммер фактов (horae_recall.terms); подстрока тут не годится:
    у «Кай» в родительном «й» меняется на «я». Имя, от которого основы не
    осталось (очень короткое), ищется целым словом с коротким окончанием.
    """
    name_terms = horae_recall.terms(name or "")
    if name_terms:
        return bool(name_terms & horae_recall.terms(text_low))
    low = (name or "").strip().lower().replace("ё", "е")
    if not low:
        return False
    return bool(re.search(rf"(?<!\w){re.escape(low)}\w{{0,3}}(?!\w)", text_low))


def _brief_of(meta: dict) -> dict:
    sc = meta.get("scene") or {}
    t = meta.get("time") or {}
    return {
        "date": t.get("date") or "", "time": t.get("time") or "",
        "location": sc.get("location") or "",
        "characters": [(c, (meta.get("costumes") or {}).get(c, "")) for c in sc.get("characters") or []],
        "events": [(e.get("level") or "normal", e.get("text")) for e in meta.get("events") or [] if e.get("text")],
        "npcs": [(n, (f or {}).get("relationship") or "") for n, f in (meta.get("npcs") or {}).items()],
        "items": [(i.get("icon") or "", n, i.get("holder") or "") for n, i in (meta.get("items") or {}).items()],
    }


def structured_hits(metas: list[tuple[int, dict]], user_q: str, known_chars: list[str], *,
                    top_k: int, pure: bool) -> list[dict]:
    """Слой 1 (порт _structuredQuery): попадания по намерению и ключевым словам."""
    q = (user_q or "").lower().replace("ё", "е")
    if not q or not metas:
        return []
    kw = keywords()
    intent = kw.get("intent") or {}
    pat = kw.get("patterns") or {}
    mentioned = [c for c in known_chars if _name_hit(c, q)]
    is_first = bool(_has_any(q, intent.get("first")))
    is_last = bool(_has_any(q, intent.get("last")))
    hits: dict[int, dict] = {}

    def add(mid: int, sim: float, why: str):
        if mid not in hits or hits[mid]["similarity"] < sim:
            hits[mid] = {"mid": mid, "similarity": sim, "source": "structured:" + why}

    by_new = list(reversed(metas))
    if is_first and mentioned:
        for name in mentioned:
            for mid, meta in metas:
                sc = meta.get("scene") or {}
                if name in (meta.get("npcs") or {}) or name in (sc.get("characters") or []):
                    add(mid, 1.0, "first")
                    break
    if is_last and mentioned and _has_any(q, pat.get("costume")):
        for name in mentioned:
            for mid, meta in by_new:
                if (meta.get("costumes") or {}).get(name):
                    add(mid, 1.0, "costume")
                    break
    if is_last and _has_any(q, pat.get("mood")):
        words = _has_any(q, kw.get("moodWords"))
        for mid, meta in by_new:
            moods = meta.get("mood") or {}
            pool = [moods.get(n, "") for n in mentioned] if mentioned else list(moods.values())
            if any(v and (not words or any(w in v.lower() for w in words)) for v in pool):
                add(mid, 1.0, "mood")
                break
    if _has_any(q, pat.get("gift")):
        found = 0
        for mid, meta in by_new:
            items = meta.get("items") or {}
            if any(i.get("importance") in ("!", "!!") and (not mentioned or any(
                    m in (i.get("holder") or "") for m in mentioned)) for i in items.values()):
                add(mid, 0.95, "gift")
                found += 1
            elif any(any(g in (e.get("text") or "").lower() for g in kw.get("giftKws") or [])
                     and (not mentioned or any(_name_hit(m, (e.get("text") or "").lower()) for m in mentioned))
                     for e in meta.get("events") or []):
                add(mid, 0.95, "gift")
                found += 1
            if found >= top_k:
                break
    if _has_any(q, pat.get("importantItem")):
        found = 0
        for mid, meta in by_new:
            if any(i.get("importance") in ("!", "!!") for i in (meta.get("items") or {}).values()):
                add(mid, 0.95, "item")
                found += 1
                if found >= top_k:
                    break
    if _has_any(q, pat.get("importantEvent")):
        found = 0
        for mid, meta in by_new:
            levels = [e.get("level") for e in meta.get("events") or []]
            if "critical" in levels or "important" in levels:
                add(mid, 1.0 if "critical" in levels else 0.95, "event")
                found += 1
                if found >= top_k:
                    break
    if not pure:
        cats = kw.get("categories") or {}
        themes = [t for t in ("ceremony", "promise", "loss", "revelation", "power") if _has_any(q, pat.get(t))]
        if themes:
            terms = {w.lower() for t in themes for w in cats.get(t, [])}
            for mid, meta in by_new:
                text = " ".join((e.get("text") or "").lower() for e in meta.get("events") or [])
                n = sum(1 for w in terms if w in text)
                if n:
                    add(mid, 0.90 + min(n, 5) * 0.02, "theme")
                    break
        detected = {w.lower() for words in cats.values() for w in words if w.lower() in q}
        if detected:
            expanded = {w.lower() for words in cats.values() if detected & {x.lower() for x in words}
                        for w in words}
            scored = []
            for mid, meta in by_new:
                parts = [e.get("text") or "" for e in meta.get("events") or []]
                parts.append((meta.get("scene") or {}).get("location") or "")
                parts += [f"{n} {(f or {}).get('relationship') or ''}" for n, f in (meta.get("npcs") or {}).items()]
                parts += [f"{n} {(i or {}).get('location') or ''}" for n, i in (meta.get("items") or {}).items()]
                text = " ".join(parts).lower()
                n = sum(1 for w in expanded if w in text)
                if n >= 2 or (n >= 1 and any(_name_hit(m, text) for m in mentioned)):
                    scored.append((n, mid))
            scored.sort(reverse=True)
            for n, mid in scored[:top_k]:
                if mid not in hits:
                    add(mid, 0.85 + n * 0.02, "keywords")
    # Соседние ответы: событие-попадание часто — середина сцены.
    order = [mid for mid, _ in metas]
    with_events = {mid for mid, meta in metas if meta.get("events")}
    for hit in list(hits.values()):
        try:
            pos = order.index(hit["mid"])
        except ValueError:
            continue
        for step in (-1, 1):
            for k in range(1, CONTEXT_RADIUS + 1):
                j = pos + step * k
                if 0 <= j < len(order) and order[j] in with_events:
                    if order[j] not in hits:
                        hits[order[j]] = {"mid": order[j], "similarity": hit["similarity"] * 0.85,
                                          "source": "context"}
                    break
    ranked = sorted(hits.values(), key=lambda h: -h["similarity"])
    return ranked[:top_k]


# ---------------------------------------------------------------------------
# Смысловой слой
# ---------------------------------------------------------------------------
def _idf_noise_ok(doc_text: str, sim: float, threshold: float, df: dict, n: int) -> bool:
    """Документ из одних частых слов («таверна», «Вольф») требует чуть большего сходства."""
    terms = [t for t in re.split(r"[\s|,.!?:;()\[\]\n«»\"]+", doc_text.lower()) if 2 <= len(t) <= 20]
    if not terms or n < 10:
        return True
    avg = sum(math.log((n + 1) / (df.get(t, 0) + 1)) for t in terms) / len(terms)
    if avg >= 0.5:
        return True
    return sim >= threshold + (0.5 - avg) * 0.05


def vector_search(query_vec, docs: list, threshold: float, top_k: int, *, pure: bool) -> list[dict]:
    scored = []
    for d in docs:
        if not d["vec"]:
            continue
        sim = sum(a * b for a, b in zip(query_vec, d["vec"]))
        if sim >= threshold:
            scored.append((sim, d))
    scored.sort(key=lambda x: -x[0])
    if not pure and len(scored) >= 2:
        n = len(docs)
        df: dict[str, int] = {}
        for d in docs:
            for t in set(re.split(r"[\s|,.!?:;()\[\]\n«»\"]+", d["text"].lower())):
                if 2 <= len(t) <= 20:
                    df[t] = df.get(t, 0) + 1
        scored = [(s, d) for s, d in scored if d.get("carried") or _idf_noise_ok(d["text"], s, threshold, df, n)]
    kept: list = []
    for sim, d in scored:
        if any(sum(a * b for a, b in zip(d["vec"], k["vec"])) > DEDUP_COSINE for _, k in kept):
            continue
        kept.append((sim, d))
        if len(kept) >= top_k:
            break
    return [{"key": d["key"], "mid": d["mid"], "similarity": round(sim, 4), "source": "vector"} for sim, d in kept]


def lexical_search(query: str, context: str, docs: list, threshold: float, top_k: int) -> list[dict]:
    if not docs:
        return []
    sims = horae_recall.lexical_similarities(query, [{"content": d["text"]} for d in docs], context)
    scored = sorted(((s, d) for s, d in zip(sims, docs) if s >= threshold), key=lambda x: -x[0])
    return [{"key": d["key"], "mid": d["mid"], "similarity": round(s, 4), "source": "lexical"}
            for s, d in scored[:top_k]]


def fuse(layers: list[tuple[list[dict], float]], relevant_chars: set, doc_chars: dict) -> list[dict]:
    """RRF: score += w / (60 + ранг) по каждому слою + 1/60 за общих персонажей."""
    merged: dict = {}
    for results, weight in layers:
        for rank, r in enumerate(results):
            key = r.get("key") or r["mid"]
            entry = merged.setdefault(key, {**r, "score": 0.0})
            entry["score"] += weight / (RRF_K + rank)
            entry["similarity"] = max(entry.get("similarity") or 0, r.get("similarity") or 0)
            if r["source"] not in entry["source"]:
                entry["source"] += "+" + r["source"]
    for key, entry in merged.items():
        if relevant_chars & doc_chars.get(key, set()):
            entry["score"] += 1 / RRF_K
            entry["source"] += "+char"
    return sorted(merged.values(), key=lambda e: (-e["score"], -(e.get("similarity") or 0)))


async def _rerank(query: str, candidates: list[dict], texts: dict, settings: dict, connection) -> list[dict]:
    """Переранжирование моделью rerank (LiteLLM). Сбой — порядок RRF остаётся."""
    model = (settings.get("recall_rerank_model") or "").strip()
    if not model or len(candidates) < 2:
        return candidates
    try:
        import litellm

        from backend.llm_gateway import _route_kwargs

        kwargs = _route_kwargs(connection, model)
        docs = [texts.get(c.get("key") or c["mid"], "") for c in candidates]
        resp = await litellm.arerank(query=query, documents=docs, top_n=len(docs), **kwargs)
        results = getattr(resp, "results", None) or (resp.get("results") if isinstance(resp, dict) else None) or []
        scores = {}
        for r in results:
            idx = r.get("index") if isinstance(r, dict) else getattr(r, "index", None)
            score = r.get("relevance_score") if isinstance(r, dict) else getattr(r, "relevance_score", None)
            if isinstance(idx, int) and score is not None:
                scores[idx] = float(score)
        if not scores:
            return candidates
        min_score = float(settings.get("recall_rerank_min_score") or 0)
        out = []
        for i, c in enumerate(candidates):
            if i in scores and scores[i] >= min_score:
                out.append({**c, "similarity": round(scores[i], 4), "source": c["source"] + "+rerank"})
        out.sort(key=lambda c: -c["similarity"])
        return out
    except Exception as exc:  # noqa: BLE001
        log.warning("Rerank Horae не сработал: %s", exc)
        return candidates


# ---------------------------------------------------------------------------
# Вспоминание
# ---------------------------------------------------------------------------
async def recall(db, session, comp, *, user_message: str, connection: dict,
                 history_ids: list | None = None) -> list[dict]:
    """
    Кандидаты воспоминаний для хода, лучшие первыми (до top_k × 3 — часть
    отсеет обрезка истории по бюджету). Каждый — {mid|None, similarity,
    score, source, carried, brief, full, content}.
    """
    s = comp.settings
    top_k = int(s.get("recall_top_k") or 5)
    docs_rows = (await db.execute(
        select(models.HoraeMemoryDoc.id, models.HoraeMemoryDoc.message_id, models.HoraeMemoryDoc.document,
               models.HoraeMemoryDoc.doc_hash, models.HoraeMemoryDoc.embed_model,
               models.HoraeMemoryDoc.brief, models.HoraeMemoryDoc.content)
        .where(models.HoraeMemoryDoc.session_id == session.id)
    )).all()
    if not docs_rows:
        return []
    # Что модель видит дословно (окно) и что позже момента until — не вспоминаем.
    first_visible = next((i for i in (history_ids or []) if i), None)
    until = comp.until

    def excluded(mid) -> bool:
        if mid is None:
            return False
        if first_visible is not None and mid >= first_visible:
            return True
        return until is not None and mid > until

    metas = {mid: meta for mid, role, meta, side in comp.entries
             if role == "assistant" and not side and isinstance(meta, dict)}
    docs = []
    doc_chars: dict = {}
    model = _embedding_model(connection)
    usable = [d for d in docs_rows
              if d.message_id is None or (d.message_id in metas and not excluded(d.message_id))]
    vectors = await _cached_vectors(db, session.id, model, usable) if model else {}
    own_total = own_vec = 0
    for d in usable:
        carried = d.message_id is None
        vec = vectors.get(d.id)
        if not carried:
            own_total += 1
            if vec:
                own_vec += 1
        key = d.message_id if not carried else f"c{d.id}"
        meta = metas.get(d.message_id) if not carried else None
        brief = _brief_of(meta) if meta else (d.brief or {})
        doc_chars[key] = {c for c, _ in brief.get("characters") or []} | {n for n, _ in brief.get("npcs") or []}
        docs.append({"key": key, "mid": d.message_id, "text": d.document or "", "carried": carried,
                     "vec": vec, "brief": brief, "content": d.content if carried else ""})
    if not docs:
        return []
    state = comp.state
    # Последний ответ ИИ (он же может быть в окне — для запроса это и нужно:
    # «что происходит сейчас»), но не позже момента until (перегенерация).
    recent = [mid for mid in metas if until is None or mid <= until]
    last_meta = metas[max(recent)] if recent else None
    user_q = clean_user_query(user_message)
    sq = state_query(state, last_meta)
    query = merged_query(sq, user_q)
    known = sorted({*state["npcs"], *state["scene"].get("characters", [])} | {
        c for d in docs for c in doc_chars.get(d["key"], set())})
    ordered_metas = [(mid, metas[mid]) for mid in sorted(metas) if not excluded(mid)]

    layers: list[tuple[list[dict], float]] = []
    structured = structured_hits(ordered_metas, user_q, known, top_k=top_k, pure=bool(s.get("recall_pure")))
    if structured:
        layers.append(([{**h, "key": h["mid"]} for h in structured], 1.0))

    use_vectors = bool(model) and own_total and own_vec / own_total >= VECTOR_COVERAGE
    query_vec = None
    if use_vectors:
        vecs = await _embed([query], connection)
        query_vec = vecs[0] if vecs else None
    pool = top_k * CANDIDATE_FACTOR
    if query_vec:
        threshold = dynamic_threshold(float(s.get("recall_threshold") or 0.72), len(docs))
        layers.append((vector_search(query_vec, docs, threshold, pool, pure=bool(s.get("recall_pure"))), 1.0))
    else:
        layers.append((lexical_search(user_q, sq, docs, float(s.get("recall_lexical_threshold") or 0.3), pool), 1.0))

    intent = ""
    if s.get("recall_query_rewrite"):
        try:
            from backend import horae_tasks

            intent, queries = await horae_tasks.rewrite_query(db, session, s, connection)
            for q in queries:
                if query_vec is not None:
                    qv = await _embed([q], connection)
                    if qv:
                        threshold = dynamic_threshold(float(s.get("recall_threshold") or 0.72), len(docs))
                        layers.append((vector_search(qv[0], docs, threshold, pool, pure=True), 1 / max(1, len(queries))))
                else:
                    layers.append((lexical_search(q, "", docs, float(s.get("recall_lexical_threshold") or 0.3), pool),
                                   1 / max(1, len(queries))))
        except Exception as exc:  # noqa: BLE001
            log.warning("Переписывание запроса Horae не сработало: %s", exc)

    relevant = set(state["scene"].get("characters") or []) | {c for c in known if _name_hit(c, user_q.lower())}
    fused = fuse(layers, relevant, doc_chars)[:max(pool, s.get("recall_rerank_candidates") or 25)]
    if s.get("recall_rerank") and fused:
        texts = {d["key"]: d["text"] for d in docs}
        fused = await _rerank(intent or query or user_q, fused, texts, s, connection)
    fused = fused[:pool]

    by_key = {d["key"]: d for d in docs}
    full_n = int(s.get("recall_full_text_count") or 0)
    full_thr = float(s.get("recall_full_text_threshold") or 0.9)
    full_chars = int(s.get("recall_full_text_chars") or 3000)
    out = []
    for rank, c in enumerate(fused):
        d = by_key.get(c.get("key") or c["mid"])
        if d is None:
            continue
        out.append({
            "mid": d["mid"], "carried": d["carried"], "similarity": c.get("similarity"),
            "score": round(c.get("score", 0), 5), "source": c.get("source"), "brief": d["brief"],
            "full": rank < full_n and (c.get("similarity") or 0) >= full_thr,
            "content": d["content"][:full_chars] if d["carried"] else "",
        })
    # Полный текст — только тем, кто дошёл до верхушки; сам текст из сообщений.
    need = [c["mid"] for c in out if c["full"] and not c["carried"]]
    if need:
        rows = (await db.execute(
            select(models.Message.id, models.Message.content).where(models.Message.id.in_(need))
        )).all()
        texts = {r.id: (r.content or "") for r in rows}
        prev = {}
        if s.get("anti_paraphrase"):
            prev = await _preceding_user(db, session.id, need)
        for c in out:
            if c["full"] and not c["carried"]:
                body = hs.strip_tags_text(texts.get(c["mid"], "")).strip()[:full_chars]
                if prev.get(c["mid"]):
                    body = f"[ПОЛЬЗОВАТЕЛЬ]\n{prev[c['mid']]}\n[ОТВЕТ]\n{body}"
                c["content"] = body
                c["full"] = bool(body)
    return out


# Векторы документов в памяти процесса: (чат, модель) → {id документа: (хэш, вектор)}.
# Без кэша каждый ход разбирал бы JSON всех векторов чата (тысячи × 1536 чисел).
# Держим несколько последних чатов; изменённый документ узнаётся по хэшу.
_VEC_CACHE: dict = {}
_VEC_CACHE_CHATS = 4


async def _cached_vectors(db, session_id: int, model: str, rows) -> dict[int, list]:
    key = (session_id, model)
    cache = _VEC_CACHE.pop(key, None) or {}
    _VEC_CACHE[key] = cache                      # в конец — самый свежий
    while len(_VEC_CACHE) > _VEC_CACHE_CHATS:
        _VEC_CACHE.pop(next(iter(_VEC_CACHE)))
    want = [r.id for r in rows if r.embed_model == model
            and (r.id not in cache or cache[r.id][0] != r.doc_hash)]
    for i in range(0, len(want), 500):
        chunk = want[i:i + 500]
        got = (await db.execute(
            select(models.HoraeMemoryDoc.id, models.HoraeMemoryDoc.doc_hash, models.HoraeMemoryDoc.embedding)
            .where(models.HoraeMemoryDoc.id.in_(chunk))
        )).all()
        for g in got:
            if g.embedding:
                cache[g.id] = (g.doc_hash, g.embedding)
    return {r.id: cache[r.id][1] for r in rows
            if r.embed_model == model and r.id in cache and cache[r.id][0] == r.doc_hash}


async def _preceding_user(db, session_id: int, mids: list[int]) -> dict[int, str]:
    rows = (await db.execute(
        select(models.Message.id, models.Message.role, models.Message.content)
        .where(models.Message.session_id == session_id, models.Message.id <= max(mids))
        .order_by(models.Message.id)
    )).all()
    out, last_user = {}, ""
    wanted = set(mids)
    for r in rows:
        if r.role == "user":
            last_user = (r.content or "")[:PREV_USER_CHARS]
        elif r.id in wanted:
            out[r.id] = last_user
    return out


def visible(candidates: list[dict], history_ids, total: int, start: int, top_k: int) -> list[dict]:
    """
    Отсев после обрезки истории по бюджету: воспоминание о сообщении, которое
    модель и так видит дословно, — лишнее. Перенесённые — всегда.
    """
    cands = list(candidates or [])
    if cands and history_ids is not None and len(history_ids) == total and start < total:
        first = history_ids[start]
        if first is not None:
            cands = [c for c in cands if c.get("mid") is None or c["mid"] < first]
    return cands[:top_k]


def render(candidates: list[dict], *, current_date: str = "", calendar=None) -> str:
    """Блок «[Воспоминание — …]» (порт текста плагина). Пусто — ""."""
    lines = [HEADER]
    for c in candidates or []:
        b = c.get("brief") or {}
        prefix = "[Прошлая память] " if c.get("carried") else ""
        ident = f"#{c['mid']}" if c.get("mid") else "#?"
        rel = horae_time.relative_label(b.get("date") or "", current_date, calendar) \
            if (current_date and b.get("date")) else ""
        tag_parts = [x for x in (rel, b.get("date"), b.get("time")) if x]
        time_tag = f"({' '.join(tag_parts)})" if tag_parts else ""
        if c.get("full") and (c.get("content") or "").strip():
            head = " ".join(x for x in (f"{prefix}{ident}", time_tag, "[Полный текст]") if x)
            lines.append(f"{head}\n{c['content'].strip()}")
            continue
        parts = [time_tag] if time_tag else []
        if b.get("location"):
            parts.append(f"Сцена:{b['location']}")
        for name, costume in b.get("characters") or []:
            parts.append(f"{name}({costume})" if costume else name)
        for level, text in b.get("events") or []:
            parts.append(f"{hs.LEVEL_MARK.get(level, '○')}{text}")
        for name, rel_txt in b.get("npcs") or []:
            parts.append(f"NPC:{name}({rel_txt})" if rel_txt else f"NPC:{name}")
        for icon, name, holder in b.get("items") or []:
            parts.append(f"{icon}{name}={holder}")
        if parts:
            lines.append(f"{prefix}{ident} " + " | ".join(parts))
    return "\n".join(lines) if len(lines) > 1 else ""


async def carry_documents(db, src_id: int, dst_id: int) -> int:
    """
    Перенести память поиска в новый чат: документы своих сообщений (с текстом
    ответа и краткой метой) и уже перенесённые раньше — цепочкой, как
    «снимки памяти» плагина v1.15.
    """
    from backend.horae_engine import active_meta

    rows = (await db.execute(
        select(models.HoraeMemoryDoc).where(models.HoraeMemoryDoc.session_id == src_id)
    )).scalars().all()
    mids = [d.message_id for d in rows if d.message_id]
    msgs = {}
    if mids:
        for m in (await db.execute(select(models.Message).where(models.Message.id.in_(mids)))).scalars().all():
            msgs[m.id] = m
    count = 0
    for d in rows:
        if d.message_id is None:
            brief, content, origin = d.brief, d.content, d.origin
        else:
            m = msgs.get(d.message_id)
            if m is None:
                continue
            meta = active_meta(m.horae, m.active_swipe) or {}
            brief = _brief_of(meta)
            content = hs.strip_tags_text(m.content or "")
            origin = f"carry:{src_id}"
        db.add(models.HoraeMemoryDoc(
            session_id=dst_id, message_id=None, origin=origin or f"carry:{src_id}", doc_hash=d.doc_hash,
            document=d.document, content=content or "", brief=brief, embedding=d.embedding,
            embed_model=d.embed_model or ""))
        count += 1
    await db.flush()
    return count
