"""
Дневник персонажа и дела человека (2.28.0).

Зачем. Память чата живёт в своём чате, память о пользователе («Обо мне»)
нарочно не берёт ни планов, ни событий — только то, что верно о человеке
надолго. Не хватало третьего: персонаж помнит, КАК прошли ваши дни, и знает
дела человека с датами — во всех чатах с ним, а не только в том, где о них
сказали. Отсюда два вида записей:

  * DiaryDay — запись об одном дне ОТ ПЕРВОГО ЛИЦА персонажа: что человек
    рассказывал и делал, что было важно, что персонаж почувствовал. Одна на
    (человек, персонаж, день) — общая для всех чатов человека с персонажем.
    Человек может её поправить: тогда модель дописывает, не переписывая.
  * DiaryPlan — дело или план с датой («20.11 в 18:00 — к врачу»). По
    умолчанию видны всем персонажам человека: напоминание дойдёт в том чате,
    куда он зайдёт, даже если сказал о деле в другом.

В промпт (хвост после истории, рядом со временем — блок меняется каждый день
и не должен сбивать кэш начала запроса) идут сегодняшняя дата, несколько
последних дней дневника и ближайшие дела. Персонаж напоминает о деле один
раз и к месту: после ответа, где дело упомянуто, оно помечается reminded_on,
и блок говорит «уже напомнил сегодня».

Чего не трогаем: общие чаты (там не только владелец — дневник владельца не
для друга) и групповые (у реплики нет одного персонажа-автора дневника).
Разбор — фоном (kind "diary"), после хода, пакетами; запрос к модели — вне
сессии БД, как у памяти о пользователе.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import or_, select

from backend import llm_gateway, models
from backend.config import settings
from backend.database import AsyncSessionLocal
from backend.horae_recall import _norm_key, terms
from backend.user_memory import is_shared, key_for_session, key_for_user

logger = logging.getLogger("aichat.diary")

DEFAULT_SETTINGS = {"enabled": True, "auto": True, "plans_shared": True}

# Когда разбирать: столько новых реплик или символов — или старшая неразобранная
# реплика старше FLUSH_AFTER (короткий разговор тоже попадёт в дневник).
MIN_MESSAGES = 6
MIN_CHARS = 2500
FLUSH_AFTER = 2 * 3600
BATCH_MESSAGES = 40
BATCH_CHARS = 9000
LINE_CHARS = 500
RETRY_AFTER = 600

ENTRY_CHARS = 1500      # запись одного дня
PLAN_CHARS = 200
BLOCK_DAYS = 3          # сколько последних дней дневника видит модель
BLOCK_DAY_CHARS = 700
PLANS_AHEAD = 14        # дела на столько дней вперёд
PLANS_MAX = 8
MAX_PLANS_LISTED = 30   # текущих дел в запросе разбора

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
_RU_WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
_RU_WD_SHORT = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]

_running: set[int] = set()
_failed_at: dict[int, float] = {}
_locks: dict[str, asyncio.Lock] = {}

DIARY_PROMPT = (
    "Ты — {char}. Ты ведёшь ЛИЧНЫЙ ДНЕВНИК о днях, проведённых с {user}. Его читаешь только ты: "
    "пиши честно, от первого лица, своим голосом и характером, с чувствами.\n\n"
    "Тебе дают твою карточку, прежние записи этих дней, список текущих дел {user} и новую "
    "переписку по дням. Ответ — ТОЛЬКО JSON без пояснений:\n"
    '{{"days": [{{"day": "YYYY-MM-DD", "mood": "<одно эмодзи>", "entry": "<запись дня целиком>"}}], '
    '"plans": [{{"due": "YYYY-MM-DD", "time": "HH:MM или пусто", "text": "<дело>", "quote": "<цитата>"}}], '
    '"done": [<номер дела>], "cancel": [<номер дела>]}}\n\n'
    "ДНЕВНИК (days):\n"
    "- день — только из дней переписки; запись — 3–7 предложений (до 1200 символов: насыщенный день — сожми прежнее, а не новое) о дне: что {user} рассказывал "
    "и делал, что между вами происходило, что было важным, как ты это восприняла(-л), что "
    "почувствовала(-л), о чём думаешь и чего ждёшь;\n"
    "- это дневник, а не протокол: без цитат реплик целиком, без перечисления по порядку, без "
    "служебных пометок; события ролевой истории описывай как пережитое вами вместе;\n"
    "- у дня уже есть запись — верни её ЦЕЛИКОМ, сохранив прежнее (особенно то, что поправил "
    "{user}), и допиши новое; ничего нового за день — этот день не возвращай;\n"
    "- mood — одно эмодзи твоего настроения за день.\n\n"
    "ДЕЛА (plans) — только РЕАЛЬНАЯ жизнь {user}, не сюжет игры: встреча, врач, экзамен, работа "
    "к сроку, поездка, покупка, звонок, день рождения близкого. Бери, только если {user} сам "
    "сказал о деле, и у него есть дата или срок. Относительные даты («завтра», «в пятницу», "
    "«через неделю») переводи в точную дату ОТ ДНЯ РЕПЛИКИ. text — коротко, в третьем лице "
    "(«сходить к стоматологу», «сдать отчёт»). quote — дословная короткая цитата из реплики "
    "{user}. Уже записанные дела не повторяй.\n"
    "done / cancel — номера из списка текущих дел, если {user} сказал, что сделал или отменил.\n"
    "Строки с пометкой [вне роли] {user} пишет от себя, а не от героя.\n"
    "Нечего записать — верни {{\"days\": [], \"plans\": [], \"done\": [], \"cancel\": []}}."
)

BLOCK_HEADER = (
    "[Твой дневник — личные записи о прошлых днях с собеседником; их видишь только ты. "
    "Опирайся на них, чтобы помнить, что было и что он чувствовал, но не пересказывай их и "
    "не говори о дневнике, если не спросят.]"
)
PLANS_HEADER = "[Дела собеседника в реальной жизни]"
PLANS_HINT = (
    "Если дело на сегодня или завтра, а ты о нём ещё не напоминал(-а) — к месту, естественно "
    "и один раз напомни (без списка и без «по моим записям»). Отмеченные «уже напомнил» "
    "не повторяй."
)


# ============================================================================
# Настройки и время
# ============================================================================
async def get_settings(db, profile_key: str) -> dict:
    row = await db.get(models.AppSetting, "diary:" + profile_key)
    value = row.value if row is not None and isinstance(row.value, dict) else {}
    return {k: bool(value.get(k, d)) for k, d in DEFAULT_SETTINGS.items()}


async def set_settings(db, profile_key: str, patch: dict) -> dict:
    current = await get_settings(db, profile_key)
    current.update({k: bool(v) for k, v in (patch or {}).items() if k in DEFAULT_SETTINGS})
    row = await db.get(models.AppSetting, "diary:" + profile_key)
    if row is None:
        db.add(models.AppSetting(key="diary:" + profile_key, value=dict(current)))
    else:
        row.value = dict(current)
    return current


def tz_of(sess):
    from backend.horae_memory import chat_tzinfo

    return chat_tzinfo(getattr(sess, "timezone", "") or "") or timezone.utc


def today_of(sess) -> date:
    return datetime.now(tz_of(sess)).date()


def local_day(created_at, tzinfo) -> date:
    """Местная дата реплики (created_at в базе — наивное UTC)."""
    if created_at is None:
        return datetime.now(tzinfo).date()
    return created_at.replace(tzinfo=timezone.utc).astimezone(tzinfo).date()


def fmt_day(d: date) -> str:
    return f"{d.strftime('%d.%m')} ({_RU_WEEKDAYS[d.weekday()]})"


def when_label(due: str, time_s: str, today: date) -> str:
    """«сегодня в 18:00», «завтра», «пт 21.11», «вчера (просрочено?)»."""
    try:
        d = date.fromisoformat(due)
    except ValueError:
        return due
    delta = (d - today).days
    if delta == 0:
        s = "сегодня"
    elif delta == 1:
        s = "завтра"
    elif delta == 2:
        s = "послезавтра"
    elif delta < 0:
        s = f"{d.strftime('%d.%m')} (прошло)"
    else:
        s = f"{_RU_WD_SHORT[d.weekday()]} {d.strftime('%d.%m')}"
    if delta >= 0 and 1 <= delta <= 2:
        s += f", {_RU_WD_SHORT[d.weekday()]} {d.strftime('%d.%m')}"
    return s + (f" в {time_s}" if time_s else "")


def _clean(text, n: int) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text[:n].rstrip()


def _clip_middle(text: str, n: int, head: int) -> str:
    """Длинный текст — начало и КОНЕЦ (новое дописывается в конец), середина «…»."""
    if len(text) <= n:
        return text
    a = text[:head]
    cut = max(a.rfind(". "), a.rfind("! "), a.rfind("? "), a.rfind("\n"))
    a = a[:cut + 1] if cut > head // 2 else a
    b = text[-(n - len(a) - 3):]
    m = re.search(r"(?<=[.!?…])\s+", b)
    b = b[m.end():] if m and m.start() < len(b) // 3 else b
    return a.rstrip() + " … " + b.lstrip()


def _clean_entry(text) -> str:
    text = str(text or "").replace("\r", "").strip()
    text = re.sub(r"\n{3,}", "\n\n", text)
    return _clip_middle(text, ENTRY_CHARS, ENTRY_CHARS // 3)


# ============================================================================
# Кому дневник
# ============================================================================
async def owner_of(db, sess) -> tuple[str, int] | None:
    """(владелец, персонаж) для чата — или None, если дневник здесь не ведётся."""
    if sess is None or sess.is_group or not sess.character_id:
        return None
    if await is_shared(db, sess.id):
        return None
    return key_for_session(sess), sess.character_id


async def user_name(db, sess) -> str:
    """Как зовут собеседника в дневнике: персона чата, логин, иначе «собеседник»."""
    if sess.persona_id:
        p = await db.get(models.Persona, sess.persona_id)
        if p is not None and (p.name or "").strip():
            return p.name.strip()
    if sess.owner_id:
        u = await db.get(models.User, sess.owner_id)
        if u is not None and u.username:
            return u.username
    return "собеседник"


# ============================================================================
# Разбор чата в дневник
# ============================================================================
def schedule(session_id: int) -> bool:
    if not settings.DIARY or session_id in _running:
        return False
    return time.monotonic() - _failed_at.get(session_id, -1e9) >= RETRY_AFTER


async def after_turn(session_id: int) -> None:
    """После хода: отметить напоминания в ответе и, если пора, разобрать чат."""
    if not settings.DIARY:
        return
    try:
        await note_reminders(session_id)
    except Exception:  # noqa: BLE001 — дневник не мешает чату
        logger.exception("Дневник: не удалось отметить напоминания (чат %s)", session_id)
    if schedule(session_id):
        await maybe_update(session_id)


async def maybe_update(session_id: int, *, force: bool = False) -> dict | None:
    """Разобрать новые реплики чата в дневник. Не бросает исключений."""
    if not force and not schedule(session_id):
        return None
    if session_id in _running:
        if not force:
            return None
        # «Записать сейчас», а фоновый разбор этого чата ещё идёт — дождёмся
        # его и разберём то, что осталось (раньше кнопка молча отвечала «нечего»).
        for _ in range(300):
            await asyncio.sleep(0.1)
            if session_id not in _running:
                break
        else:
            return None
    _running.add(session_id)
    try:
        return await _update(session_id, force)
    except Exception:  # noqa: BLE001
        _failed_at[session_id] = time.monotonic()
        logger.exception("Дневник: разбор чата %s не удался", session_id)
        return None
    finally:
        _running.discard(session_id)


async def skip_to_end(db, session_id: int) -> None:
    from sqlalchemy import func

    last = (await db.execute(
        select(func.max(models.Message.id)).where(models.Message.session_id == session_id)
    )).scalar()
    sess = await db.get(models.ChatSession, session_id)
    if sess is not None:
        sess.diary_upto = max(int(sess.diary_upto or 0), int(last or 0))


def _lines(msgs, tzinfo, uname: str, cname: str) -> tuple[list[str], list[str], str]:
    """Переписка по дням для модели, дни "YYYY-MM-DD" и текст реплик человека."""
    from backend.horae_memory import detect_ooc

    out, days, user_text = [], [], []
    current = None
    for m in msgs:
        when = m.created_at.replace(tzinfo=timezone.utc).astimezone(tzinfo) if m.created_at else None
        d = when.date() if when else None
        if d is not None and d != current:
            current = d
            days.append(d.isoformat())
            out.append(f"\n== {d.isoformat()}, {_RU_WEEKDAYS[d.weekday()]} ==")
        if m.role == "user":
            marked, text = detect_ooc(m.content or "")
            who = ("[вне роли] " if marked else "") + uname
            user_text.append(text or "")
        else:
            text, who = m.content or "", cname
        text = _clean(text, LINE_CHARS)
        if not text:
            continue
        stamp = when.strftime("%H:%M") + " " if when else ""
        out.append(f"{stamp}{who}: {text}")
    return out, days, "\n".join(user_text)


def parse_reply(text: str) -> dict:
    out = {"days": [], "plans": [], "done": [], "cancel": []}
    raw = (text or "").strip()
    dec = json.JSONDecoder()
    data = None
    for m in re.finditer(r"\{", raw):
        try:
            cand, _ = dec.raw_decode(raw, m.start())
        except ValueError:
            continue
        if isinstance(cand, dict) and any(k in cand for k in out):
            data = cand
            break
    if not isinstance(data, dict):
        return out
    for k in out:
        v = data.get(k)
        if isinstance(v, list):
            out[k] = v
    return out


def _as_index(n) -> int | None:
    if isinstance(n, bool):
        return None
    try:
        return int(n)
    except (TypeError, ValueError):
        return None


def screen_plan(item, today: date, user_norm: str, user_terms: set[str]) -> dict | None:
    """Дело из ответа модели — или None: без даты, в прошлом, без цитаты из реплик человека."""
    if not isinstance(item, dict):
        return None
    due = str(item.get("due") or "").strip()
    if not _DATE_RE.match(due):
        return None
    try:
        d = date.fromisoformat(due)
    except ValueError:
        return None
    if d < today or d > today + timedelta(days=366):
        return None
    text = _clean(item.get("text"), PLAN_CHARS)
    if len(text) < 3:
        return None
    quote = _clean(item.get("quote"), 300)
    qn = _norm_key(quote)
    if not qn:
        return None
    if qn not in user_norm:
        qt = terms(quote)
        if not qt or len(qt & user_terms) < max(1, (len(qt) * 2 + 2) // 3):
            return None
    tm = str(item.get("time") or "").strip()
    tmatch = _TIME_RE.match(tm)
    tm = f"{int(tmatch.group(1)):02d}:{tmatch.group(2)}" if tmatch else ""
    return {"due": due, "time": tm, "text": text, "quote": quote}


async def _update(session_id: int, force: bool) -> dict | None:
    from backend.memory_service import _background_connection
    from backend.settings_service import get_connection

    async with AsyncSessionLocal() as db:
        sess = await db.get(models.ChatSession, session_id)
        if sess is None:
            return None
        who = await owner_of(db, sess)
        if who is None:
            await skip_to_end(db, session_id)
            await db.commit()
            return None
        pkey, char_id = who
        conf = await get_settings(db, pkey)
        if not conf["enabled"]:
            await skip_to_end(db, session_id)
            await db.commit()
            return None
        if not (conf["auto"] or force):
            return None   # копится до «✍️ Записать сейчас»
        upto = int(sess.diary_upto or 0)
        msgs = (await db.execute(
            select(models.Message).where(models.Message.session_id == session_id,
                                         models.Message.role.in_(("user", "assistant")),
                                         models.Message.id > upto)
            .order_by(models.Message.id)
        )).scalars().all()
        if not msgs or not any(m.role == "user" for m in msgs):
            return None
        chars = sum(len(m.content or "") for m in msgs)
        oldest = msgs[0].created_at
        stale = oldest is not None and (datetime.utcnow() - oldest).total_seconds() >= FLUSH_AFTER
        if not force and len(msgs) < MIN_MESSAGES and chars < MIN_CHARS and not stale:
            return None
        batch, used = [], 0
        # Первый разбор давнего чата — только свежий хвост (старое не листаем);
        # дальше — по порядку от указателя, чтобы ничего не пропустить.
        order = list(reversed(msgs)) if upto == 0 else msgs
        for m in order:
            size = min(len(m.content or ""), LINE_CHARS)
            if batch and (len(batch) >= BATCH_MESSAGES or used + size > BATCH_CHARS):
                break
            batch.append(m)
            used += size
        if upto == 0:
            batch.reverse()
            last_id = msgs[-1].id
        else:
            last_id = batch[-1].id
        tzinfo = tz_of(sess)
        today = today_of(sess)
        character = await db.get(models.Character, char_id)
        cname = (character.name if character else "") or "Персонаж"
        uname = await user_name(db, sess)
        lines, days, user_text = _lines(batch, tzinfo, uname, cname)
        existing = {r.day: r for r in (await db.execute(
            select(models.DiaryDay).where(models.DiaryDay.profile_key == pkey,
                                          models.DiaryDay.character_id == char_id,
                                          models.DiaryDay.day.in_(days))
        )).scalars().all()}
        # Какими записи были до запроса: пока модель думала, человек мог их
        # поправить (или другой чат — дописать) — тогда не затираем.
        seen_text = {d: r.text for d, r in existing.items()}
        pq = select(models.DiaryPlan).where(
            models.DiaryPlan.profile_key == pkey, models.DiaryPlan.status == "planned",
            # Давно прошедшие незакрытые дела не вытесняют нынешние из списка.
            models.DiaryPlan.due >= (today - timedelta(days=3)).isoformat())
        if not conf["plans_shared"]:
            pq = pq.where(or_(models.DiaryPlan.character_id == char_id, models.DiaryPlan.character_id.is_(None)))
        plans = (await db.execute(
            pq.order_by(models.DiaryPlan.due, models.DiaryPlan.id).limit(MAX_PLANS_LISTED)
        )).scalars().all()
        plan_ids = [p.id for p in plans]
        card = ""
        if character is not None:
            card = "\n".join(_clean(getattr(character, f, ""), 600) for f in ("description", "personality")
                             if getattr(character, f, "")).strip()
        parts = [f"[Ты — {cname}]", card or "(карточки нет)", "",
                 f"[Сегодня у {uname}] {today.isoformat()}, {_RU_WEEKDAYS[today.weekday()]}", ""]
        parts.append("[Прежние записи этих дней]")
        if existing:
            for d in days:
                r = existing.get(d)
                if r is not None:
                    mark = " (поправлено человеком — сохрани)" if r.edited else ""
                    parts.append(f"{d}{mark}: {r.text}")
        else:
            parts.append("(записей нет)")
        parts += ["", f"[Текущие дела {uname}]"]
        parts += [f"{i}. {p.due}{(' ' + p.time) if p.time else ''} — {p.text}"
                  for i, p in enumerate(plans, 1)] or ["(дел нет)"]
        parts += ["", "[Переписка]", *lines]
        system = DIARY_PROMPT.format(char=cname, user=uname)
        connection = await get_connection(db)
    with llm_gateway.sampling_overrides(max_tokens=3000, temperature=0.6):
        reply = await llm_gateway.complete(
            [{"role": "system", "content": system}, {"role": "user", "content": "\n".join(parts)}],
            None, _background_connection(connection), kind="diary")
    parsed = parse_reply(reply)
    user_norm, user_terms = _norm_key(user_text), terms(user_text)
    found = [p for p in (screen_plan(i, today, user_norm, user_terms) for i in parsed["plans"]) if p]
    stats = {"days": 0, "plans": 0, "done": 0, "cancelled": 0}
    conflict = False
    lock = _locks.setdefault(pkey, asyncio.Lock())
    async with lock, AsyncSessionLocal() as db:
        sess = await db.get(models.ChatSession, session_id)
        if sess is None:
            return None
        conf = await get_settings(db, pkey)
        if conf["enabled"] and (conf["auto"] or force):
            for item in parsed["days"]:
                if not isinstance(item, dict):
                    continue
                day = str(item.get("day") or "").strip()
                entry = _clean_entry(item.get("entry"))
                if day not in days or len(entry) < 10:
                    continue
                row = (await db.execute(select(models.DiaryDay).where(
                    models.DiaryDay.profile_key == pkey, models.DiaryDay.character_id == char_id,
                    models.DiaryDay.day == day))).scalars().first()
                mood = _clean(item.get("mood"), 8)
                if (row.text if row is not None else None) != seen_text.get(day):
                    conflict = True   # запись изменилась — разберём пакет ещё раз, уже с ней
                    continue
                if row is None:
                    db.add(models.DiaryDay(profile_key=pkey, character_id=char_id, day=day,
                                           mood=mood, text=entry))
                else:
                    row.text, row.updated_at = entry, datetime.utcnow()
                    if mood:
                        row.mood = mood
                stats["days"] += 1
            known = {(p.due, _norm_key(p.text)) for p in (await db.execute(
                select(models.DiaryPlan).where(models.DiaryPlan.profile_key == pkey,
                                               models.DiaryPlan.status != "cancelled")
            )).scalars().all()}
            for p in found:
                key = (p["due"], _norm_key(p["text"]))
                if key in known:
                    continue
                known.add(key)
                db.add(models.DiaryPlan(profile_key=pkey, character_id=char_id, session_id=session_id,
                                        due=p["due"], time=p["time"], text=p["text"], quote=p["quote"]))
                stats["plans"] += 1
            for field, status in (("done", "done"), ("cancel", "cancelled")):
                for n in (_as_index(x) for x in parsed[field]):
                    if n is not None and 1 <= n <= len(plan_ids):
                        plan = await db.get(models.DiaryPlan, plan_ids[n - 1])
                        if plan is not None and plan.status == "planned":
                            plan.status, plan.updated_at = status, datetime.utcnow()
                            stats["done" if status == "done" else "cancelled"] += 1
        if not conflict:
            sess.diary_upto = max(int(sess.diary_upto or 0), last_id)
        await db.commit()
    _failed_at.pop(session_id, None)
    if any(stats.values()):
        logger.info("Дневник (%s, персонаж %s, чат %s): %s", pkey, char_id, session_id, stats)
    return stats


# ============================================================================
# Напоминания
# ============================================================================
def mentions(reply: str, plan_text: str) -> bool:
    """Упомянул ли ответ дело: большая часть значимых слов дела есть в ответе."""
    pt = terms(plan_text)
    if not pt:
        return False
    rt = terms(reply)
    hit = len(pt & rt)
    if hit >= max(2, (len(pt) * 2 + 2) // 3) or (len(pt) == 1 and hit):
        return True
    # Одно общее слово («сходим погулять» и «сходить к врачу») — не напоминание,
    # а самое длинное (обычно суть дела: «стоматолог», «отчёт») — да.
    key = max(pt, key=len)
    return len(key) >= 5 and key in rt


def _near(p, today: date) -> bool:
    """Дело, о котором уместно напоминать: на сегодня, завтра или вчерашнее."""
    try:
        return abs((date.fromisoformat(p.due) - today).days) <= 1
    except ValueError:
        return False


async def note_reminders(session_id: int) -> int:
    """Последний ответ персонажа упомянул дело на ближайшие дни — помечаем «напомнил»."""
    async with AsyncSessionLocal() as db:
        sess = await db.get(models.ChatSession, session_id)
        who = await owner_of(db, sess)
        if who is None:
            return 0
        pkey, char_id = who
        last = (await db.execute(
            select(models.Message).where(models.Message.session_id == session_id)
            .order_by(models.Message.id.desc()).limit(1)
        )).scalars().first()
        if last is None or last.role != "assistant" or not (last.content or "").strip():
            return 0
        today = today_of(sess)
        plans = await _visible_plans(db, pkey, char_id, today, await get_settings(db, pkey))
        n = 0
        for p in plans:
            if p.reminded_on != today.isoformat() and _near(p, today) and mentions(last.content, p.text):
                p.reminded_on = today.isoformat()
                n += 1
        if n:
            await db.commit()
        return n


# ============================================================================
# Блок промпта
# ============================================================================
async def _visible_plans(db, pkey: str, char_id: int | None, today: date, conf: dict) -> list:
    q = select(models.DiaryPlan).where(
        models.DiaryPlan.profile_key == pkey, models.DiaryPlan.status == "planned",
        models.DiaryPlan.due >= (today - timedelta(days=1)).isoformat(),
        models.DiaryPlan.due <= (today + timedelta(days=PLANS_AHEAD)).isoformat(),
    )
    if not conf.get("plans_shared", True) and char_id is not None:
        q = q.where(or_(models.DiaryPlan.character_id == char_id, models.DiaryPlan.character_id.is_(None)))
    return list((await db.execute(
        q.order_by(models.DiaryPlan.due, models.DiaryPlan.time, models.DiaryPlan.id).limit(PLANS_MAX)
    )).scalars().all())


def render(days: list, plans: list, today: date, reminded: set | frozenset = frozenset()) -> str:
    parts = []
    if days:
        parts.append(BLOCK_HEADER)
        for r in days:
            try:
                d = date.fromisoformat(r.day)
                head = "Сегодня" if d == today else ("Вчера" if (today - d).days == 1 else fmt_day(d))
            except ValueError:
                head = r.day
            text = _clip_middle(r.text, BLOCK_DAY_CHARS, BLOCK_DAY_CHARS // 3)
            parts.append(f"{head}{(' ' + r.mood) if r.mood else ''}: {text}")
    if plans:
        if parts:
            parts.append("")
        parts.append(PLANS_HEADER + f" Сегодня {_RU_WEEKDAYS[today.weekday()]}, {today.strftime('%d.%m.%Y')}.")
        for p in plans:
            mark = " (уже напомнил сегодня)" if p.reminded_on == today.isoformat() or p.id in reminded else ""
            parts.append(f"- {when_label(p.due, p.time, today)} — {p.text}{mark}")
        parts.append(PLANS_HINT)
    return "\n".join(parts)


async def prompt_block(db, sess) -> str:
    """Блок дневника для хвоста промпта ("" — дневника здесь нет)."""
    if not settings.DIARY:
        return ""
    try:
        who = await owner_of(db, sess)
        if who is None:
            return ""
        pkey, char_id = who
        conf = await get_settings(db, pkey)
        if not conf["enabled"]:
            return ""
        today = today_of(sess)
        days = list((await db.execute(
            select(models.DiaryDay).where(models.DiaryDay.profile_key == pkey,
                                          models.DiaryDay.character_id == char_id,
                                          models.DiaryDay.day <= today.isoformat())
            .order_by(models.DiaryDay.day.desc()).limit(BLOCK_DAYS)
        )).scalars().all())
        days.reverse()
        plans = await _visible_plans(db, pkey, char_id, today, conf)
        if any(_near(p, today) and p.reminded_on != today.isoformat() for p in plans):
            # Ход не отметил напоминание сам (Telegram не зовёт after_turn): если
            # последний ответ в этом чате уже упомянул дело — «уже напомнил».
            last = (await db.execute(
                select(models.Message.role, models.Message.content)
                .where(models.Message.session_id == sess.id)
                .order_by(models.Message.id.desc()).limit(2)
            )).all()
            said = " ".join(c or "" for r, c in last if r == "assistant")
            if said:
                reminded = {p.id for p in plans if _near(p, today) and mentions(said, p.text)}
                return render(days, plans, today, reminded)
        return render(days, plans, today)
    except Exception:  # noqa: BLE001 — сбой дневника не роняет ход
        logger.exception("Дневник: не удалось собрать блок (чат %s)", getattr(sess, "id", None))
        return ""


# ============================================================================
# REST: /api/diary
# ============================================================================
def _day(r) -> dict:
    try:
        wd = _RU_WEEKDAYS[date.fromisoformat(r.day).weekday()]
    except ValueError:
        wd = ""
    return {"id": r.id, "character_id": r.character_id, "day": r.day, "weekday": wd,
            "mood": r.mood, "text": r.text, "edited": bool(r.edited)}


def _plan(p) -> dict:
    return {"id": p.id, "character_id": p.character_id, "session_id": p.session_id, "due": p.due,
            "time": p.time, "text": p.text, "status": p.status, "source": p.source,
            "quote": p.quote, "reminded_on": p.reminded_on}


def build_router(current_user):
    from fastapi import APIRouter, Body, Depends, HTTPException
    from sqlalchemy.ext.asyncio import AsyncSession

    from backend.database import get_session

    router = APIRouter(prefix="/api/diary")

    def _pkey(user) -> str:
        from backend import admin_service

        if user is None and admin_service.security_cache().get("accounts_enabled"):
            raise HTTPException(401, "Требуется вход")
        return key_for_user(user)

    async def _check_character(db, user, cid: int) -> None:
        """Чужого приватного персонажа к своему дневнику не привязать (и имени его не узнать)."""
        ch = await db.get(models.Character, cid)
        if ch is None or not (user is None or user.role == "admin" or ch.owner_id in (None, user.id)):
            raise HTTPException(404, "Персонаж не найден")

    async def _own_day(db, pkey, day_id):
        r = await db.get(models.DiaryDay, day_id)
        if r is None or r.profile_key != pkey:
            raise HTTPException(404, "Запись не найдена")
        return r

    async def _own_plan(db, pkey, plan_id):
        p = await db.get(models.DiaryPlan, plan_id)
        if p is None or p.profile_key != pkey:
            raise HTTPException(404, "Дело не найдено")
        return p

    @router.get("")
    async def get_diary(character_id: int | None = None, session_id: int | None = None,
                        user=Depends(current_user), db: AsyncSession = Depends(get_session)):
        pkey = _pkey(user)
        conf = await get_settings(db, pkey)
        rows = (await db.execute(select(models.DiaryDay.character_id).where(
            models.DiaryDay.profile_key == pkey))).scalars().all()
        counts: dict[int, int] = {}
        for c in rows:
            counts[c] = counts.get(c, 0) + 1
        chars = {}
        if counts:
            chars = dict((await db.execute(select(models.Character.id, models.Character.name).where(
                models.Character.id.in_(list(counts))))).all())
        days = []
        if character_id:
            days = [_day(r) for r in (await db.execute(
                select(models.DiaryDay).where(models.DiaryDay.profile_key == pkey,
                                              models.DiaryDay.character_id == character_id)
                .order_by(models.DiaryDay.day.desc()).limit(120))).scalars().all()]
        plans = [_plan(p) for p in (await db.execute(
            select(models.DiaryPlan).where(models.DiaryPlan.profile_key == pkey)
            .order_by(models.DiaryPlan.status != "planned", models.DiaryPlan.due, models.DiaryPlan.id)
            .limit(300))).scalars().all()]
        block, active = "", False
        if session_id:
            sess = await db.get(models.ChatSession, session_id)
            if sess is not None and key_for_session(sess) == pkey:
                active = await owner_of(db, sess) is not None
                block = await prompt_block(db, sess)
        return {"settings": conf, "enabled": settings.DIARY, "chat_active": active and settings.DIARY,
                "characters": [{"id": c, "name": chars.get(c, "?"), "days": n} for c, n in counts.items()],
                "days": days, "plans": plans, "block": block}

    @router.put("/settings")
    async def put_settings(patch: dict = Body(...), user=Depends(current_user),
                           db: AsyncSession = Depends(get_session)):
        out = await set_settings(db, _pkey(user), patch)
        await db.commit()
        return out

    @router.post("/days")
    async def add_day(payload: dict = Body(...), user=Depends(current_user),
                      db: AsyncSession = Depends(get_session)):
        pkey = _pkey(user)
        day = str(payload.get("day") or "").strip()
        text = _clean_entry(payload.get("text"))
        try:
            cid = int(payload.get("character_id") or 0)
        except (TypeError, ValueError):
            cid = 0
        if not _DATE_RE.match(day) or not text or not cid:
            raise HTTPException(422, "Нужны персонаж, дата и текст")
        await _check_character(db, user, cid)
        row = (await db.execute(select(models.DiaryDay).where(
            models.DiaryDay.profile_key == pkey, models.DiaryDay.character_id == cid,
            models.DiaryDay.day == day))).scalars().first()
        if row is None:
            row = models.DiaryDay(profile_key=pkey, character_id=cid, day=day, text=text, edited=True,
                                  mood=_clean(payload.get("mood"), 8))
            db.add(row)
        else:
            row.text, row.edited, row.updated_at = text, True, datetime.utcnow()
        await db.commit()
        return _day(row)

    @router.patch("/days/{day_id}")
    async def edit_day(day_id: int, payload: dict = Body(...), user=Depends(current_user),
                       db: AsyncSession = Depends(get_session)):
        r = await _own_day(db, _pkey(user), day_id)
        if "text" in payload:
            text = _clean_entry(payload.get("text"))
            if not text:
                raise HTTPException(422, "Пустая запись — удалите её")
            r.text, r.edited = text, True
        if "mood" in payload:
            r.mood = _clean(payload.get("mood"), 8)
        r.updated_at = datetime.utcnow()
        await db.commit()
        return _day(r)

    @router.delete("/days/{day_id}")
    async def delete_day(day_id: int, user=Depends(current_user), db: AsyncSession = Depends(get_session)):
        r = await _own_day(db, _pkey(user), day_id)
        await db.delete(r)
        await db.commit()
        return {"ok": True}

    def _plan_fields(payload: dict, partial: bool) -> dict:
        out = {}
        if "due" in payload or not partial:
            due = str(payload.get("due") or "").strip()
            if not _DATE_RE.match(due):
                raise HTTPException(422, "Дата — ГГГГ-ММ-ДД")
            out["due"] = due
        if "time" in payload:
            tm = str(payload.get("time") or "").strip()
            m = _TIME_RE.match(tm)
            if tm and not m:
                raise HTTPException(422, "Время — ЧЧ:ММ")
            out["time"] = f"{int(m.group(1)):02d}:{m.group(2)}" if m else ""
        if "text" in payload or not partial:
            text = _clean(payload.get("text"), PLAN_CHARS)
            if not text:
                raise HTTPException(422, "Что за дело?")
            out["text"] = text
        if "status" in payload:
            if payload["status"] not in ("planned", "done", "cancelled"):
                raise HTTPException(422, "Неизвестный статус")
            out["status"] = payload["status"]
        return out

    @router.post("/plans")
    async def add_plan(payload: dict = Body(...), user=Depends(current_user),
                       db: AsyncSession = Depends(get_session)):
        fields = _plan_fields(payload, partial=False)
        cid = payload.get("character_id")
        if isinstance(cid, int):
            await _check_character(db, user, cid)
        p = models.DiaryPlan(profile_key=_pkey(user), source="manual",
                             character_id=int(cid) if isinstance(cid, int) else None, **fields)
        db.add(p)
        await db.commit()
        return _plan(p)

    @router.patch("/plans/{plan_id}")
    async def edit_plan(plan_id: int, payload: dict = Body(...), user=Depends(current_user),
                        db: AsyncSession = Depends(get_session)):
        p = await _own_plan(db, _pkey(user), plan_id)
        for k, v in _plan_fields(payload, partial=True).items():
            setattr(p, k, v)
        if "due" in payload or "time" in payload:
            p.reminded_on = ""   # перенесли — напомнить заново
        p.updated_at = datetime.utcnow()
        await db.commit()
        return _plan(p)

    @router.delete("/plans/{plan_id}")
    async def delete_plan(plan_id: int, user=Depends(current_user), db: AsyncSession = Depends(get_session)):
        p = await _own_plan(db, _pkey(user), plan_id)
        await db.delete(p)
        await db.commit()
        return {"ok": True}

    @router.post("/run")
    async def run_now(session_id: int, user=Depends(current_user), db: AsyncSession = Depends(get_session)):
        """«Записать сейчас»: разобрать новые реплики этого чата без порогов."""
        pkey = _pkey(user)
        sess = await db.get(models.ChatSession, session_id)
        if sess is None or key_for_session(sess) != pkey:
            raise HTTPException(404, "Чат не найден")
        if not settings.DIARY:
            raise HTTPException(409, "Дневник выключен на сервере")
        if await owner_of(db, sess) is None:
            raise HTTPException(409, "В общих и групповых чатах дневник не ведётся")
        await db.close()
        with llm_gateway.foreground():
            stats = await maybe_update(session_id, force=True)
        return {"ok": stats is not None, "stats": stats or {}}

    return router
