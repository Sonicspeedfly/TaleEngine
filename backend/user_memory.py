"""
Память о пользователе — общие сведения о реальном человеке для ВСЕХ его чатов.

Зачем отдельно от памяти чата. Факты, снимок и Хроника живут в своём чате и
нужны сюжету этого чата: кто кому что обещал, что обсуждали, что будет дальше.
В другом чате они чужие — модель «вспоминала» бы разговор, которого здесь не
было («ты же собирался скинуть порты в JSON…»). Сюда же идёт только то, что
верно о самом человеке и через месяц, в любом разговоре: как его зовут и как к
нему обращаться, на каком языке писать, чем он занимается, что умеет и чем
давно увлекается, какие ответы ему удобны и чего избегать.

Как сведения не тянут за собой темы чатов:
  * разбираются ТОЛЬКО реплики пользователя (не ответы персонажа — иначе его
    «пришли мне файл» стало бы фактом о пользователе);
  * модель получает строгий список категорий и примеры того, что НЕ брать
    (темы, планы, обещания, просьбы, настроение, отыгрыш);
  * каждое сведение — с дословной цитатой, которую проверяем в репликах;
    планы и время («скину», «завтра»), код, ссылки, числа и имена персонажей
    отсеиваются кодом, а не только просьбой в промпте;
  * новое сведение сначала «кандидат»: в промпт оно попадает, только когда
    его подтвердит человек, когда оно сказано вне роли ((…), /ooc) или когда
    другой фразой повторится в ДРУГОМ чате — общее отличается от темы одного
    разговора тем, что встречается не в одном разговоре;
  * чаты, открытые другу (SessionShare), не разбираются и память в них не
    подмешивается: реплики там не только владельца, а друг увидел бы чужое.

Владелец (profile_key): "u:<id>" — аккаунт (веб и привязанный Telegram
вместе), "tg:<id>" — Telegram без аккаунтов, "local" — один пользователь.
Настройки владельца — AppSetting "user_memory:<key>" (не в общем "ui": его
PUT заменяет целиком, и сохранение интерфейса стирало бы их).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import datetime

from sqlalchemy import func, select

from backend import llm_gateway, models
from backend.config import settings
from backend.database import AsyncSessionLocal
from backend.horae_recall import _STOPWORDS, _norm_key, terms

logger = logging.getLogger("aichat.user_memory")

# Категории: ключ → (подпись, одно значение?, потолок записей).
CATEGORIES: dict[str, tuple[str, bool, int]] = {
    "name": ("Имя", True, 1),
    "address": ("Обращение", True, 1),
    "lang": ("Язык общения", True, 1),
    "work": ("Занятие", False, 3),
    "skills": ("Умеет", False, 6),
    "interests": ("Интересы", False, 6),
    "style": ("Как отвечать", False, 6),
    "limits": ("Чего избегать", False, 6),
}
MAX_ACTIVE = 30
MAX_CANDIDATES = 40
MAX_TEXT = 160
BLOCK_CHARS = 1500

# Когда разбирать: накопилось столько новых реплик или символов — или самая
# старая неразобранная реплика старше FLUSH_AFTER (короткий чат тоже учтётся).
MIN_MESSAGES = 5
MIN_CHARS = 2000
FLUSH_AFTER = 12 * 3600
# Сколько реплик берём за раз: свежие, не больше BATCH_CHARS символов. Старый
# хвост (первый разбор давнего чата) пропускается — указатель уходит в конец.
BATCH_MESSAGES = 30
BATCH_CHARS = 6000
LINE_CHARS = 400
# После сбоя модели этот чат не разбираем столько секунд.
RETRY_AFTER = 600

PROFILE_PROMPT = (
    "Ты ведёшь короткую «память о пользователе» — сведения о реальном человеке, который пишет "
    "в чат. Они будут подсказкой во ВСЕХ его будущих разговорах, с любыми персонажами. Поэтому "
    "сюда идёт только то, что верно о самом человеке и надолго, а не то, о чём он говорит сейчас.\n\n"
    "Тебе дают текущие записи (с номерами) и новые реплики пользователя из одного чата. "
    "Ответ — ТОЛЬКО JSON без пояснений:\n"
    '{"add": [{"cat": "<категория>", "text": "<сведение>", "quote": "<цитата>"}], '
    '"drop": [{"n": <номер записи>, "quote": "<цитата>"}]}\n\n'
    "Категории (cat):\n"
    "- name — как зовут пользователя (назвал себя сам);\n"
    "- address — как к нему обращаться: на «ты» или «вы», по имени, по нику;\n"
    "- lang — на каком языке он хочет общаться;\n"
    "- work — профессия, род занятий, учёба;\n"
    "- skills — что он умеет: языки программирования, инструменты, ремёсла;\n"
    "- interests — устойчивые увлечения («давно играю», «люблю», «увлекаюсь»);\n"
    "- style — как ему отвечать: длина, тон, формат, что раздражает;\n"
    "- limits — чего он просит избегать, его границы.\n\n"
    "Бери сведение, только если пользователь прямо сказал это О СЕБЕ и это будет верно через месяц "
    "в совсем другом разговоре. Пиши коротко, в третьем лице, без имён персонажей: "
    "«Python-разработчик», «Просит отвечать коротко, без вступлений», «Давно играет в D&D мастером».\n\n"
    "НЕ записывай — это остаётся в своём чате:\n"
    "- темы, задачи и проекты разговора: «обсуждает парсер», «делает бота», «пишет мод»;\n"
    "- планы и обещания: «скинет порты в JSON», «пришлёт файл», «продолжит завтра»;\n"
    "- просьбы и вопросы к ассистенту, содержимое файлов, код, ссылки, числа;\n"
    "- настроение и обстоятельства сегодняшнего дня: «устал», «болеет», «сейчас в дороге»;\n"
    "- роль и сюжет: кого пользователь отыгрывает, действия его героя (текст в *звёздочках*, "
    "реплики от лица героя), события истории, отношения с персонажами;\n"
    "- догадки и выводы, которых он не говорил.\n\n"
    "Строки с пометкой [вне роли] пользователь пишет от себя, а не от героя.\n"
    "quote — точная короткая цитата (3–15 слов) из реплики пользователя, где он это говорит; "
    "без цитаты сведение не примут.\n"
    "drop — только если пользователь прямо сказал, что запись неверна или устарела "
    "(«я больше не…», «на самом деле меня зовут…»), с цитатой.\n"
    "Уже записанное не повторяй. Подходящего нет — верни {\"add\": [], \"drop\": []}."
)

BLOCK_HEADER = (
    "[Память о пользователе — общие сведения о самом собеседнике, собранные в разных разговорах. "
    "Это НЕ события и НЕ темы этого чата. Учитывай их в обращении, языке и формате ответа; "
    "не упоминай другие разговоры и не ссылайся на эти сведения без повода.]"
)

# Планы, время, обсуждения и разовые просьбы — признак темы чата, а не человека.
_TRANSIENT_RE = re.compile(
    r"\b(?:буд(?:у|ет|ем|ете|ут)|собира(?:юсь|ется|емся|етесь|ются|лся|лась|лись)|"
    r"планиру(?:ю|ет|ем|ете|ют)|пришл\w*|прислат\w*|скин\w*|кин(?:у|ет|ем|ете|ут)|вылож\w*|"
    r"отправ\w*|покаж(?:у|ет|ем|ете|ут)|поделит\w*|обеща\w*|сегодня|завтра|вчера|на днях|скоро|"
    r"в этом чате|обсужда\w*|спросил\w*|хотел\w* бы получить|today|tomorrow|yesterday|tonight)\b",
    re.IGNORECASE,
)
# Код, ссылки и файлы. Имена технологий вида Node.js — не файлы.
_CODE_RE = re.compile(
    r"(?:://|www\.|```|[{}<>\[\]`]|\b(?!(?:node|vue|next|nuxt|react|three|express|d3|angular|svelte|"
    r"solid|ember|backbone|deno|bun|chart|socket)\.js\b)\w+\.(?:json|txt|py|js|csv|md|pdf|docx?|xlsx?|png|jpe?g)\b)",
    re.IGNORECASE)
# Отрицание и «ты/вы» меняют смысл при тех же словах: «на ты» ≠ «на вы».
_POLARITY_RE = re.compile(r"\b(?:не|ни|нет|без|no|not|never|ты|вы)\b", re.IGNORECASE)
_NAME_ENDINGS = r"(?:а|я|ы|и|е|у|ю|о|ой|ей|ою|ею|ам|ям|ах|ях)?"
_LONG_TERM_RE = re.compile(
    r"\b(?:давно|много лет|с детства|всю жизнь|годами|всегда|постоянно|обожаю|увлекаюсь|"
    r"профессионально|по профессии|years|always|for a long time)\b", re.IGNORECASE)

_locks: dict[str, asyncio.Lock] = {}
_running: set[int] = set()
_failed_at: dict[int, float] = {}


# ============================================================================
# Владелец и настройки
# ============================================================================
def key_for_user(user) -> str:
    """Владелец памяти для веб-запроса: аккаунт или единственный пользователь."""
    return f"u:{user.id}" if user is not None else "local"


def key_for_session(sess) -> str:
    """
    Владелец памяти для чата — тот же ключ, что key_for_user у его хозяина в
    вебе. Без режима аккаунтов веб один ("local"), даже если у старых чатов
    остался owner_id от прежнего режима; Telegram-пользователи — каждый свой.
    """
    from backend import admin_service

    user_key = getattr(sess, "user_key", "") or ""
    if admin_service.security_cache().get("accounts_enabled") and getattr(sess, "owner_id", None):
        return f"u:{sess.owner_id}"
    return user_key if user_key.startswith("tg:") else "local"


DEFAULT_SETTINGS = {"enabled": True, "auto": True, "instant": False}


async def get_settings(db, profile_key: str) -> dict:
    row = await db.get(models.AppSetting, "user_memory:" + profile_key)
    value = row.value if row is not None and isinstance(row.value, dict) else {}
    return {k: bool(value.get(k, d)) for k, d in DEFAULT_SETTINGS.items()}


async def set_settings(db, profile_key: str, patch: dict) -> dict:
    current = await get_settings(db, profile_key)
    current.update({k: bool(v) for k, v in (patch or {}).items() if k in DEFAULT_SETTINGS})
    row = await db.get(models.AppSetting, "user_memory:" + profile_key)
    if row is None:
        db.add(models.AppSetting(key="user_memory:" + profile_key, value=dict(current)))
    else:
        row.value = dict(current)
    return current


async def is_shared(db, session_id: int) -> bool:
    """Чат открыт другу: реплики там не только владельца, а память владельца — не для друга."""
    return bool((await db.execute(
        select(models.SessionShare.id).where(models.SessionShare.session_id == session_id).limit(1)
    )).first())


# ============================================================================
# Текст: ключи, проверка цитат, фильтры
# ============================================================================
def norm_key(category: str, text: str) -> str:
    return (category + ":" + _norm_key(text))[:400]


def _clean(text) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip().strip("«»\"'").strip()


def _quote_found(quote: str, source_norm: str, source_terms: set[str]) -> bool:
    """Цитата есть в репликах: дословно (без регистра и пунктуации) или почти всеми словами."""
    q = _norm_key(quote)
    if len(q) < 3:
        return False
    if q in source_norm:
        return True
    qt = terms(quote)
    return len(qt) >= 2 and len(qt & source_terms) >= 0.8 * len(qt)


def _names_pattern(names) -> re.Pattern | None:
    """
    Имена персонажей и персоны с падежными окончаниями: «Артура», «Лиру»,
    «Катей», «Еву». Служебные слова из имён («The Narrator») не берём.
    """
    words = {w for n in names or [] for w in _norm_key(n).split() if len(w) >= 3 and w not in _STOPWORDS}
    if not words:
        return None
    alts = []
    for w in sorted(words, key=len, reverse=True):
        if len(w) >= 5:
            alts.append(re.escape(w[:-1]) + r"\w{0,3}")
        elif w[-1] in "аяоеиыуюьй":
            alts.append(re.escape(w[:-1]) + _NAME_ENDINGS)   # «Лира» → «Лиру», «Катя» → «Катей»
        else:
            alts.append(re.escape(w) + _NAME_ENDINGS)        # «Ян» → «Яна», «Олег» → «Олегу»
    return re.compile(r"\b(?:" + "|".join(alts) + r")\b", re.IGNORECASE)


def screen(item, source_norm: str, source_terms: set[str], names_re) -> dict | None:
    """
    Одно предложенное моделью сведение → {cat, text, quote} или None.
    Код здесь — последний рубеж: модель могла не послушаться промпта.
    """
    if not isinstance(item, dict):
        return None
    cat = str(item.get("cat") or item.get("category") or "").strip().lower()
    text = _clean(item.get("text") or item.get("content"))
    quote = _clean(item.get("quote"))
    if cat not in CATEGORIES or not text or not quote:
        return None
    if len(text) > MAX_TEXT or len(text) < (2 if cat == "name" else 3):
        return None
    if _TRANSIENT_RE.search(text) or _CODE_RE.search(text):
        return None
    if sum(ch.isdigit() for ch in text) >= 4:
        return None
    if names_re is not None and names_re.search(_norm_key(text)):
        return None   # имя персонажа или персоны — это роль, а не человек
    if not _quote_found(quote, source_norm, source_terms):
        return None
    return {"cat": cat, "text": text[0].upper() + text[1:], "quote": quote}


def parse_reply(text: str) -> dict:
    """{"add": [...], "drop": [...]} из ответа модели; мусор → пусто."""
    raw = (text or "").strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw)
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return {"add": [], "drop": []}
    try:
        data = json.loads(raw[start:end + 1])
    except (ValueError, TypeError):
        return {"add": [], "drop": []}
    if not isinstance(data, dict):
        return {"add": [], "drop": []}
    add = data.get("add") if isinstance(data.get("add"), list) else []
    drop = data.get("drop") if isinstance(data.get("drop"), list) else []
    return {"add": add[:12], "drop": drop[:6]}


def _polarity(text: str) -> set[str]:
    return {m.lower() for m in _POLARITY_RE.findall(_norm_key(text))}


def _similar(a: str, b: str) -> bool:
    """Одно ли это сведение другими словами (для категорий с несколькими значениями)."""
    if _polarity(a) != _polarity(b):
        return False
    ta, tb = terms(a), terms(b)
    if not ta or not tb:
        return _norm_key(a) == _norm_key(b)
    return len(ta & tb) / len(ta | tb) >= 0.6


# ============================================================================
# Хранение
# ============================================================================
def _seen(rec) -> list[int]:
    return [int(x) for x in (rec.sessions_seen or []) if isinstance(x, int) or str(x).isdigit()]


def _quotes(rec) -> list[str]:
    return list((rec.meta or {}).get("quotes") or [])


def _confirmed_elsewhere(rec) -> bool:
    """
    Повтор в другом разговоре другой фразой — сведение общее, а не тема.
    sessions_seen хранит корни разговоров (ветка и продолжение — тот же).
    """
    return len(set(_seen(rec))) >= 2 and len(set(_quotes(rec))) >= 2


async def _activate(db, rec, profile_key: str) -> list:
    """
    Сделать запись действующей; в категории с одним значением — заменить
    прежнюю. Возвращает удалённые записи (вызывающий убирает их из своих списков).
    """
    rec.status = "active"
    single = CATEGORIES.get(rec.category, ("", False, 6))[1]
    if not single:
        return []
    olds = (await db.execute(select(models.UserMemory).where(
        models.UserMemory.profile_key == profile_key, models.UserMemory.category == rec.category,
        models.UserMemory.status == "active", models.UserMemory.id != rec.id,
    ))).scalars().all()
    if any(old.locked for old in olds):
        rec.status = "candidate"   # человек сам задал значение — авто его не меняет
        return []
    for old in olds:
        await db.delete(old)
    await db.flush()
    return list(olds)


async def _enforce_caps(db, profile_key: str, keep: set[int] = frozenset()) -> None:
    """
    Потолки записей. Вытесняются самые слабые (реже встречались, давно не
    подтверждались) — но не закреплённые человеком и не включённые только что
    (keep): иначе явная реплика вне роли исчезала бы тут же.
    """
    rows = (await db.execute(select(models.UserMemory).where(
        models.UserMemory.profile_key == profile_key))).scalars().all()

    def weak_first(r):
        return (r.hits or 0, r.updated_at or datetime.min, r.id)

    def evictable(r):
        return not r.locked and r.id not in keep

    active = [r for r in rows if r.status == "active"]
    for cat, (_label, _single, cap) in CATEGORIES.items():
        mine = sorted([r for r in active if r.category == cat and evictable(r)], key=weak_first)
        extra = len([r for r in active if r.category == cat]) - cap
        for r in mine[:max(0, extra)]:
            await db.delete(r)
            active.remove(r)
    extra = len(active) - MAX_ACTIVE
    for r in sorted([r for r in active if evictable(r)], key=weak_first)[:max(0, extra)]:
        await db.delete(r)
    cands = sorted([r for r in rows if r.status == "candidate" and r.id not in keep], key=weak_first)
    extra = len([r for r in rows if r.status == "candidate"]) - MAX_CANDIDATES
    for r in cands[:max(0, extra)]:
        await db.delete(r)


async def apply(db, profile_key: str, conversation: int, found: list[dict], drops: list[int],
                *, out_of_role: set[str] | None = None, instant: bool = False) -> dict:
    """
    Записать отобранные сведения. conversation — корень разговора (см.
    conversation_of): повтор считается «в другом чате», только если корни
    разные. found — результат screen(); drops — id записей, которые
    пользователь опроверг (цитата уже проверена). out_of_role —
    нормализованные реплики вне роли: сведения из них действуют сразу.
    Возвращает счётчики для журнала и тестов.
    """
    stats = {"added": 0, "bumped": 0, "activated": 0, "dropped": 0}
    rows = list((await db.execute(select(models.UserMemory).where(
        models.UserMemory.profile_key == profile_key))).scalars().all())
    for rid in drops:
        rec = next((r for r in rows if r.id == rid), None)
        if rec is not None and not rec.locked and rec.source == "auto":
            await db.delete(rec)
            rows.remove(rec)
            stats["dropped"] += 1
    # Удаление — до вставок: иначе INSERT записи с тем же ключом ушёл бы раньше
    # DELETE и нарушил уникальность.
    await db.flush()
    now = datetime.utcnow()
    touched: set[int] = set()
    for item in found:
        cat, text, qkey = item["cat"], item["text"], _norm_key(item["quote"])
        key = norm_key(cat, text)
        rec = next((r for r in rows if r.norm_key == key), None)
        if rec is None and not CATEGORIES[cat][1]:
            # Другие слова того же сведения. В категориях с одним значением —
            # только точное совпадение: «на ты» и «на вы» — разные значения.
            rec = next((r for r in rows if r.category == cat and _similar(r.content, text)), None)
        ooc = len(qkey) >= 5 and any(qkey in o for o in out_of_role or ())
        if rec is not None:
            seen, quotes = _seen(rec), _quotes(rec)
            if conversation in seen and qkey in quotes:
                continue
            rec.sessions_seen = (seen + ([conversation] if conversation not in seen else []))[-20:]
            rec.meta = {**(rec.meta or {}), "quotes": (quotes + ([qkey] if qkey not in quotes else []))[-10:]}
            rec.hits = (rec.hits or 0) + 1
            rec.updated_at = now
            stats["bumped"] += 1
            if rec.status == "candidate" and (ooc or instant or _confirmed_elsewhere(rec)):
                for gone in await _activate(db, rec, profile_key):
                    if gone in rows:
                        rows.remove(gone)
                if rec.status == "active":
                    touched.add(rec.id)
            continue
        rec = models.UserMemory(
            profile_key=profile_key, category=cat, content=text, norm_key=key, status="candidate",
            enabled=True, source="auto", locked=False, hits=1, sessions_seen=[conversation],
            meta={"quotes": [qkey], "quote": item["quote"][:200]}, updated_at=now,
        )
        db.add(rec)
        await db.flush()
        rows.append(rec)
        stats["added"] += 1
        if ooc or instant:
            for gone in await _activate(db, rec, profile_key):
                if gone in rows:
                    rows.remove(gone)
            if rec.status == "active":
                touched.add(rec.id)
    await db.flush()
    await _enforce_caps(db, profile_key, keep=touched)
    await db.flush()
    stats["activated"] = len([r for r in rows if r.id in touched and r.status == "active"])
    return stats


# ============================================================================
# Разбор чата
# ============================================================================
def _user_lines(messages) -> tuple[list[str], set[str]]:
    """Реплики пользователя для модели и нормализованные тексты реплик вне роли."""
    from backend.horae_memory import detect_ooc

    lines, ooc = [], set()
    for m in messages:
        marked, text = detect_ooc(m.content or "")
        text = re.sub(r"\s+", " ", text or "").strip()
        if not text:
            continue
        text = text[:LINE_CHARS]
        lines.append(("[вне роли] " if marked else "") + text)
        if marked:
            ooc.add(_norm_key(text))
    return lines, ooc


def _profile_lines(rows) -> list[str]:
    out = []
    for i, r in enumerate(rows, 1):
        out.append(f"{i}. ({r.category}) {r.content}")
    return out


async def _context_names(db, sess) -> tuple[list[str], str]:
    """Имена персонажей чата и персоны (не сведения о человеке) + описание персоны."""
    names, persona_text = [], ""
    char = await db.get(models.Character, sess.character_id) if sess.character_id else None
    if char is not None:
        names.append(char.name or "")
    if sess.is_group:
        from backend import group_chat

        names += [c.name or "" for c in await group_chat.load_members(db, sess.id)]
    if sess.persona_id:
        p = await db.get(models.Persona, sess.persona_id)
        if p is not None:
            names.append(p.name or "")
            persona_text = f"{p.name or ''}: {(p.description or '').strip()[:400]}".strip(": ")
    return [n for n in names if n.strip()], persona_text


async def _ask(lines: list[str], profile: list[str], names: list[str], persona: str, connection) -> str:
    from backend.memory_service import _background_connection

    parts = ["[Текущие записи о пользователе]", *(profile or ["(записей нет)"])]
    if persona:
        parts += ["", "[Роль пользователя в этом чате — это герой, а НЕ сведения о человеке]", persona]
    if names:
        parts += ["", "[Персонажи этого чата — их имена в записи не попадают]", ", ".join(names)]
    parts += ["", "[Реплики пользователя]", *("- " + ln for ln in lines)]
    messages = [{"role": "system", "content": PROFILE_PROMPT}, {"role": "user", "content": "\n".join(parts)}]
    with llm_gateway.sampling_overrides(max_tokens=800, temperature=0.1):
        return await llm_gateway.complete(messages, None, _background_connection(connection), kind="profile")


def schedule(session_id: int) -> bool:
    """Нужно ли запускать разбор после хода (дёшево, без БД)."""
    if not settings.USER_MEMORY or session_id in _running:
        return False
    return time.monotonic() - _failed_at.get(session_id, -1e9) >= RETRY_AFTER


async def maybe_update(session_id: int, *, force: bool = False) -> dict | None:
    """
    Разобрать новые реплики пользователя этого чата, если их накопилось
    достаточно. Никогда не бросает исключений: память не должна мешать чату.
    force — разобрать сейчас, без порогов (кнопка и тесты).
    """
    if not force and not schedule(session_id):
        return None
    if session_id in _running:
        return None
    _running.add(session_id)
    try:
        return await _update(session_id, force)
    except Exception:  # noqa: BLE001
        _failed_at[session_id] = time.monotonic()
        logger.exception("Память о пользователе: разбор чата %s не удался", session_id)
        return None
    finally:
        _running.discard(session_id)


async def _update(session_id: int, force: bool) -> dict | None:
    from backend.settings_service import get_connection

    async with AsyncSessionLocal() as db:
        sess = await db.get(models.ChatSession, session_id)
        if sess is None:
            return None
        pkey = key_for_session(sess)
        conf = await get_settings(db, pkey)
        if not conf["auto"] or await is_shared(db, session_id):
            # Сбор выключен или чат открыт другу: эти реплики не читаем и
            # потом — указатель уходит в конец, иначе после «включить» или
            # закрытия доступа они (и реплики друга) ушли бы в разбор.
            await skip_to_end(db, session_id)
            await db.commit()
            return None
        conversation = conversation_of(sess)
        upto = int(sess.profile_upto or 0)
        msgs = (await db.execute(
            select(models.Message).where(models.Message.session_id == session_id,
                                         models.Message.role == "user", models.Message.id > upto)
            .order_by(models.Message.id)
        )).scalars().all()
        if not msgs:
            return None
        chars = sum(len(m.content or "") for m in msgs)
        oldest = msgs[0].created_at
        stale = oldest is not None and (datetime.utcnow() - oldest).total_seconds() >= FLUSH_AFTER
        if not force and len(msgs) < MIN_MESSAGES and chars < MIN_CHARS and not stale:
            return None
        last_id = msgs[-1].id
        batch, used = [], 0
        for m in reversed(msgs):
            size = min(len(m.content or ""), LINE_CHARS)
            if batch and (len(batch) >= BATCH_MESSAGES or used + size > BATCH_CHARS):
                break
            batch.append(m)
            used += size
        batch.reverse()
        lines, ooc = _user_lines(batch)
        if not lines:
            sess.profile_upto = last_id
            await db.commit()
            return {"added": 0, "bumped": 0, "activated": 0, "dropped": 0}
        rows = (await db.execute(
            select(models.UserMemory).where(models.UserMemory.profile_key == pkey)
            .order_by(models.UserMemory.status, models.UserMemory.category, models.UserMemory.id)
        )).scalars().all()
        listed = rows[:60]
        profile = _profile_lines(listed)
        listed_ids = [r.id for r in listed]
        names, persona = await _context_names(db, sess)
        connection = await get_connection(db)
    # Запрос к модели — вне сессии БД: он идёт секунды, а SQLite не должен ждать.
    reply = await _ask(lines, profile, names, persona, connection)
    parsed = parse_reply(reply)
    source = "\n".join(lines)
    source_norm, source_terms = _norm_key(source), terms(source)
    names_re = _names_pattern(names)
    found = [f for f in (screen(i, source_norm, source_terms, names_re) for i in parsed["add"]) if f]
    drops = []
    for d in parsed["drop"]:
        n = d.get("n") if isinstance(d, dict) else None
        if isinstance(n, int) and 1 <= n <= len(listed_ids) and isinstance(d, dict) \
                and _quote_found(_clean(d.get("quote")), source_norm, source_terms):
            drops.append(listed_ids[n - 1])
    lock = _locks.setdefault(pkey, asyncio.Lock())
    async with lock, AsyncSessionLocal() as db:
        sess = await db.get(models.ChatSession, session_id)
        if sess is None:
            return None
        conf = await get_settings(db, pkey)
        if conf["auto"]:   # сбор выключили, пока шёл запрос, — разобранное не пишем
            stats = await apply(db, pkey, conversation, found, drops, out_of_role=ooc, instant=conf["instant"])
        else:
            stats = {"added": 0, "bumped": 0, "activated": 0, "dropped": 0}
        sess.profile_upto = max(int(sess.profile_upto or 0), last_id)
        await db.commit()
    _failed_at.pop(session_id, None)
    if any(stats.values()):
        logger.info("Память о пользователе (%s, чат %s): %s", pkey, session_id, stats)
    return stats


def conversation_of(sess) -> int:
    """Корень разговора: ветка и продолжение — тот же разговор, что и исходный чат."""
    return int(getattr(sess, "profile_root", 0) or 0) or sess.id


async def skip_to_end(db, session_id: int) -> None:
    """Не разбирать то, что уже есть в чате: указатель — на последнее сообщение."""
    sess = await db.get(models.ChatSession, session_id)
    if sess is not None:
        last = (await db.execute(select(func.max(models.Message.id)).where(
            models.Message.session_id == session_id))).scalar()
        sess.profile_upto = max(int(sess.profile_upto or 0), int(last or 0))


async def mark_copied(db, session_id: int, source=None) -> None:
    """
    Чат начат с копии (ветка, продолжение, импорт): скопированные реплики —
    не новые слова человека. source — исходный чат: новое в копии считается
    тем же разговором и повтором «в другом чате» не будет.
    """
    await skip_to_end(db, session_id)
    sess = await db.get(models.ChatSession, session_id)
    if sess is not None and source is not None:
        sess.profile_root = conversation_of(source)


async def clamp_pointer(db, session_id: int) -> None:
    """
    Удалили свежие сообщения — указатель прижимается к оставшимся: SQLite
    отдаёт их id новым сообщениям, и те сошли бы за уже разобранные.
    """
    sess = await db.get(models.ChatSession, session_id)
    if sess is None or not sess.profile_upto:
        return
    top = (await db.execute(select(func.max(models.Message.id)).where(
        models.Message.session_id == session_id))).scalar() or 0
    if sess.profile_upto > top:
        sess.profile_upto = int(top)


# ============================================================================
# Блок для промпта
# ============================================================================
def render(rows) -> str:
    order = list(CATEGORIES)
    rows = sorted(rows, key=lambda r: (order.index(r.category) if r.category in order else 99, r.id))
    lines, used = [], len(BLOCK_HEADER)
    for r in rows:
        label = CATEGORIES.get(r.category, (r.category, False, 0))[0]
        line = f"- {label}: {r.content.strip()}"
        if used + len(line) + 1 > BLOCK_CHARS:
            break
        lines.append(line)
        used += len(line) + 1
    return BLOCK_HEADER + "\n" + "\n".join(lines) if lines else ""


async def prompt_block(db, sess) -> str:
    """Текст «Памяти о пользователе» для системного промпта этого чата или ""."""
    if sess is None:
        return ""
    try:
        pkey = key_for_session(sess)
        if not (await get_settings(db, pkey))["enabled"] or await is_shared(db, sess.id):
            return ""
        rows = (await db.execute(select(models.UserMemory).where(
            models.UserMemory.profile_key == pkey, models.UserMemory.status == "active",
            models.UserMemory.enabled == True,  # noqa: E712
        ))).scalars().all()
        return render(rows)
    except Exception:  # noqa: BLE001 — память не роняет ход
        logger.exception("Память о пользователе не загрузилась для чата %s", getattr(sess, "id", None))
        return ""


# ============================================================================
# API
# ============================================================================
def _item(r) -> dict:
    return {
        "id": r.id, "category": r.category, "content": r.content, "status": r.status,
        "enabled": bool(r.enabled), "source": r.source, "locked": bool(r.locked), "hits": r.hits or 0,
        "chats": len(set(_seen(r))), "quote": (r.meta or {}).get("quote") or "",
        "updated_at": r.updated_at.isoformat() if r.updated_at else None,
    }


def build_router(current_user):
    from fastapi import APIRouter, Body, Depends, HTTPException
    from sqlalchemy.ext.asyncio import AsyncSession

    from backend.database import get_session

    router = APIRouter(prefix="/api/user-memory")

    def _manager(user) -> bool:
        """Видит чужие безхозные профили: админ или единственный пользователь."""
        return user is None or getattr(user, "role", "") == "admin"

    def _pkey(user, profile: str = "") -> str:
        """
        Чей профиль. По умолчанию свой. Админ (и единственный пользователь без
        аккаунтов) может открыть профили, которыми больше некому управлять:
        "local" и Telegram без аккаунтов ("tg:<id>"). Чужой "u:<id>" — никогда.
        """
        from backend import admin_service

        if user is None and admin_service.security_cache().get("accounts_enabled"):
            raise HTTPException(401, "Требуется вход")
        own = key_for_user(user)
        profile = (profile or "").strip()
        if not profile or profile == own:
            return own
        if _manager(user) and (profile == "local" or re.fullmatch(r"tg:\d+", profile)):
            return profile
        raise HTTPException(403, "Это не ваша память")

    async def _rows(db, pkey):
        return (await db.execute(select(models.UserMemory).where(
            models.UserMemory.profile_key == pkey).order_by(models.UserMemory.id))).scalars().all()

    async def _own(db, item_id: int, pkey: str):
        rec = await db.get(models.UserMemory, item_id)
        if rec is None or rec.profile_key != pkey:
            raise HTTPException(404, "Запись не найдена")
        return rec

    async def _state(db, pkey):
        rows = await _rows(db, pkey)
        return {
            "profile": pkey,
            "settings": await get_settings(db, pkey),
            "categories": [{"key": k, "label": v[0], "single": v[1]} for k, v in CATEGORIES.items()],
            "items": [_item(r) for r in rows],
            "block": render([r for r in rows if r.status == "active" and r.enabled]),
        }

    @router.get("")
    async def get_memory(profile: str = "", user=Depends(current_user),
                         db: AsyncSession = Depends(get_session)):
        return await _state(db, _pkey(user, profile))

    @router.get("/profiles")
    async def list_profiles(user=Depends(current_user), db: AsyncSession = Depends(get_session)):
        """Профили, которые можно открыть: свой и (для админа) безхозные "local" / "tg:<id>"."""
        own = _pkey(user)
        counts = dict((await db.execute(
            select(models.UserMemory.profile_key, func.count(models.UserMemory.id))
            .group_by(models.UserMemory.profile_key))).all())
        keys = [own]
        if _manager(user):
            keys += sorted(k for k in counts if k != own and (k == "local" or k.startswith("tg:")))
        return [{"key": k, "own": k == own, "count": int(counts.get(k, 0)),
                 "label": "Вы" if k == own else ("Веб без аккаунта" if k == "local" else "Telegram " + k[3:])}
                for k in keys]

    @router.put("/settings")
    async def put_settings(payload: dict = Body(...), profile: str = "", user=Depends(current_user),
                           db: AsyncSession = Depends(get_session)):
        conf = await set_settings(db, _pkey(user, profile), payload)
        await db.commit()
        return conf

    @router.post("")
    async def add_item(payload: dict = Body(...), profile: str = "", user=Depends(current_user),
                       db: AsyncSession = Depends(get_session)):
        pkey = _pkey(user, profile)
        cat = str(payload.get("category") or "").strip()
        text = _clean(payload.get("content"))[:300]
        if cat not in CATEGORIES or not text:
            raise HTTPException(400, "Нужны категория и текст")
        async with _locks.setdefault(pkey, asyncio.Lock()):
            rows = await _rows(db, pkey)
            key = norm_key(cat, text)
            rec = next((r for r in rows if r.norm_key == key), None)
            if rec is None:
                rec = models.UserMemory(profile_key=pkey, category=cat, content=text, norm_key=key,
                                        enabled=True, source="manual", hits=1, sessions_seen=[],
                                        meta={}, updated_at=datetime.utcnow())
                db.add(rec)
                await db.flush()
            # Записал человек — действует сразу и главнее прежнего значения.
            rec.locked, rec.status, rec.enabled = True, "active", True
            await _replace_single(db, rec, pkey)
            await db.commit()
        return _item(rec)

    async def _replace_single(db, rec, pkey):
        if not CATEGORIES[rec.category][1]:
            return
        for old in await _rows(db, pkey):
            if old.id != rec.id and old.category == rec.category and old.status == "active":
                await db.delete(old)

    @router.patch("/{item_id}")
    async def patch_item(item_id: int, payload: dict = Body(...), profile: str = "", user=Depends(current_user),
                         db: AsyncSession = Depends(get_session)):
        pkey = _pkey(user, profile)
        async with _locks.setdefault(pkey, asyncio.Lock()):
            rec = await _own(db, item_id, pkey)
            if "content" in payload or "category" in payload:
                cat = str(payload.get("category") or rec.category).strip()
                text = _clean(payload.get("content") if "content" in payload else rec.content)[:300]
                if cat not in CATEGORIES or not text:
                    raise HTTPException(400, "Нужны категория и текст")
                key = norm_key(cat, text)
                dup = next((r for r in await _rows(db, pkey) if r.norm_key == key and r.id != rec.id), None)
                if dup is not None:
                    raise HTTPException(409, "Такая запись уже есть")
                rec.category, rec.content, rec.norm_key, rec.locked = cat, text, key, True
                if rec.status == "active":
                    await _replace_single(db, rec, pkey)
            if "enabled" in payload:
                rec.enabled = bool(payload["enabled"])
            if payload.get("status") == "active" and rec.status != "active":
                rec.locked = True     # подтвердил человек — авто её больше не снимет
                rec.status = "active"
                await _replace_single(db, rec, pkey)
            rec.updated_at = datetime.utcnow()
            await db.commit()
        return _item(rec)

    @router.delete("/{item_id}")
    async def delete_item(item_id: int, profile: str = "", user=Depends(current_user),
                          db: AsyncSession = Depends(get_session)):
        pkey = _pkey(user, profile)
        rec = await _own(db, item_id, pkey)
        await db.delete(rec)
        await db.commit()
        return {"ok": True}

    @router.delete("")
    async def clear(profile: str = "", user=Depends(current_user), db: AsyncSession = Depends(get_session)):
        pkey = _pkey(user, profile)
        for rec in await _rows(db, pkey):
            await db.delete(rec)
        await db.commit()
        return {"ok": True}

    return router
