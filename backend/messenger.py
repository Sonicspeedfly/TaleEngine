"""
Общий чат как мессенджер (2.27.0): несколько людей и персонаж в одном чате.

Раньше у сообщения не было автора: в общем чате модель видела одного
«Пользователя», интерфейс рисовал всем лицо смотрящего, а каждая реплика —
даже «ты тут?» другу — запускала ответ модели. Теперь:

  * у реплики человека есть автор (Message.author_id), в ленте — его имя и
    аватар, в истории для модели — подпись «Имя: текст»;
  * прежде чем звать модель, быстрая модель решает, КОМУ реплика: персонажу
    или людям (decide). Людям — сообщение просто публикуется, без хода;
  * реплика, пришедшая, пока персонаж отвечает другому, не отбрасывается
    («в чате уже идёт ответ»), а публикуется; если она персонажу — он ответит
    следом (очередь в main._queue_answer).

Режим включается только в чатах, где людей больше одного (есть SessionShare).
Личный чат ведёт себя как раньше: каждая реплика — ход модели.

Адресат (Message.address): "ai" — персонажу, "people" — людям. Как решаем:
  1. явный выбор в окне ввода (address из клиента): «персонажу» / «людям»;
  2. команда режиссёра «+Имя» или имя персонажа в тексте — персонажу;
  3. имя (логин) другого участника без имени персонажа — людям;
  4. иначе — быстрая модель по хвосту разговора; сбой или неясный ответ —
     персонажу (как раньше: лучше ответить лишний раз, чем промолчать).
Режим чата reply_mode="always" отключает проверку: персонаж отвечает всегда.
"""
from __future__ import annotations

import asyncio
import logging
import re

from sqlalchemy import select

from backend import llm_gateway, models
from backend.group_chat import name_in_text

logger = logging.getLogger("aichat.messenger")

AI, PEOPLE = "ai", "people"
ADDRESSES = (AI, PEOPLE)
REPLY_MODES = ("auto", "always")

# Хвост разговора для классификатора: сколько реплик и символов.
TAIL_MESSAGES = 12
TAIL_CHARS = 3000
LINE_CHARS = 400
# Сколько ждём быструю модель; дольше — решаем «персонажу» и не держим ход.
DECIDE_TIMEOUT = 12.0

DECIDE_PROMPT = (
    "Ты — диспетчер общего чата. В чате переписываются несколько ЛЮДЕЙ и ИИ-персонаж(и). "
    "Реши, кому адресована ПОСЛЕДНЯЯ реплика: персонажу (он должен ответить) или только людям "
    "(персонаж молчит).\n\n"
    "Персонажу — если реплика: обращается к нему по имени или по смыслу; отвечает на его "
    "последнюю реплику; продолжает сцену/ролевую игру с ним (действия в *звёздочках*, реплики "
    "героя); задаёт вопрос всем, включая персонажа; просит что-то сделать, рассказать, решить.\n"
    "Людям — если реплика: обращается к другому человеку по имени; отвечает на реплику другого "
    "человека, а не персонажа; это болтовня людей между собой («ты тут?», «я на минуту», "
    "«ахаха», «согласен с тобой»); обсуждение вне игры, которое персонажа не касается.\n"
    "Сомневаешься — выбирай персонажа, если разговор с ним шёл последним, иначе людей.\n\n"
    "Ответь ОДНИМ словом: AI — персонажу, PEOPLE — людям."
)


def _clip(text: str, n: int) -> str:
    text = (text or "").replace("\n", " ").strip()
    return text if len(text) <= n else text[: n - 1] + "…"


async def has_shares(db, session_id: int) -> bool:
    row = (await db.execute(
        select(models.SessionShare.id).where(models.SessionShare.session_id == session_id).limit(1)
    )).first()
    return row is not None


async def participants(db, sess) -> list[dict]:
    """Люди чата: владелец и те, с кем им поделились — [{id, name, owner}]."""
    if sess is None:
        return []
    ids = []
    if sess.owner_id:
        ids.append(sess.owner_id)
    shared = (await db.execute(
        select(models.SessionShare.user_id).where(models.SessionShare.session_id == sess.id)
        .order_by(models.SessionShare.id)
    )).scalars().all()
    ids += [u for u in shared if u not in ids]
    if not ids:
        return []
    users = {u.id: u for u in (await db.execute(
        select(models.User).where(models.User.id.in_(ids))
    )).scalars().all()}
    return [{"id": i, "name": users[i].username, "owner": i == sess.owner_id}
            for i in ids if i in users]


async def author_labels(db, sess) -> dict[int, str]:
    """
    Подписи авторов для истории модели: {user_id: имя}. Владелец подписан именем
    своей персоны чата (кем он играет), если она задана, остальные — логином.
    Пусто, если людей в чате один: подписи нужны только общему чату.
    """
    if sess is None or not await has_shares(db, sess.id):
        return {}
    people = await participants(db, sess)
    labels = {p["id"]: p["name"] for p in people}
    if sess.owner_id in labels and sess.persona_id:
        persona = await db.get(models.Persona, sess.persona_id)
        if persona is not None and (persona.name or "").strip():
            labels[sess.owner_id] = persona.name.strip()
    return labels


def label(text: str, name: str | None) -> str:
    """«Имя: текст» — реплика человека в общем чате для модели."""
    return f"{name}: {text}" if name else text


async def reply_mode(db, sess) -> str:
    mode = (getattr(sess, "reply_mode", None) or "auto")
    return mode if mode in REPLY_MODES else "auto"


async def is_messenger(db, sess) -> bool:
    """Чат с несколькими людьми, где персонаж отвечает только на обращения."""
    if sess is None or not await has_shares(db, sess.id):
        return False
    return await reply_mode(db, sess) == "auto"


async def character_names(db, sess) -> list[str]:
    if sess is None:
        return []
    if sess.is_group:
        rows = (await db.execute(
            select(models.Character.name).join(
                models.GroupMember, models.GroupMember.character_id == models.Character.id)
            .where(models.GroupMember.session_id == sess.id)
        )).scalars().all()
        return [n for n in rows if n]
    ch = await db.get(models.Character, sess.character_id) if sess.character_id else None
    return [ch.name] if ch is not None and ch.name else []


_PLUS_RE = re.compile(r"(?:^|\s)\+[^\s+\-]")


def quick_decide(text: str, author_id: int | None, chars: list[str], people: list[dict]) -> str | None:
    """Решение без модели — по явным признакам; None — нужна модель."""
    if _PLUS_RE.search(text or ""):
        return AI   # «+Имя» — режиссёр зовёт персонажа
    if any(name_in_text(c, text) for c in chars):
        return AI
    others = [p["name"] for p in people if p["id"] != author_id and p["name"]]
    low = (text or "").lower()
    if any(("@" + o.lower()) in low or name_in_text(o, text) for o in others):
        return PEOPLE
    return None


def parse_answer(text: str) -> str | None:
    t = (text or "").strip().upper()
    has_ai = re.search(r"\bAI\b|ПЕРСОНАЖ", t) is not None
    has_people = re.search(r"\bPEOPLE\b|ЛЮД", t) is not None
    if has_ai and not has_people:
        return AI
    if has_people and not has_ai:
        return PEOPLE
    return None


async def _transcript(db, sess, upto_id: int, labels: dict[int, str]) -> list[str]:
    rows = (await db.execute(
        select(models.Message).where(models.Message.session_id == sess.id, models.Message.id <= upto_id)
        .order_by(models.Message.id.desc()).limit(TAIL_MESSAGES)
    )).scalars().all()
    lines, total = [], 0
    for m in rows:
        if m.role == "assistant":
            who = f"{m.speaker_name or 'Персонаж'} (персонаж)"
        elif m.role == "user":
            who = labels.get(m.author_id) or "Человек"
        else:
            continue
        line = f"{who}: {_clip(m.content, LINE_CHARS)}"
        total += len(line)
        if lines and total > TAIL_CHARS:
            break
        lines.append(line)
    return list(reversed(lines))


async def decide(db, sess, msg, connection, *, explicit: str | None = None) -> tuple[str, str]:
    """
    Кому реплика msg: (AI|PEOPLE, как решили: "explicit"|"rule"|"model"|"fallback").
    msg уже сохранена (её id — граница хвоста разговора).
    """
    if explicit in ADDRESSES:
        return explicit, "explicit"
    people = await participants(db, sess)
    chars = await character_names(db, sess)
    quick = quick_decide(msg.content or "", msg.author_id, chars, people)
    if quick:
        return quick, "rule"
    labels = await author_labels(db, sess)
    lines = await _transcript(db, sess, msg.id, labels)
    names = ", ".join(chars) or "Персонаж"
    humans = ", ".join(p["name"] for p in people) or "—"
    user = (f"Персонаж(и): {names}\nЛюди в чате: {humans}\n\nПереписка (последняя строка — реплика, "
            f"которую надо отнести):\n" + "\n".join(lines) + "\n\nКому последняя реплика? AI или PEOPLE?")
    from backend.memory_service import _background_connection
    try:
        with llm_gateway.sampling_overrides(max_tokens=400, temperature=0):
            out = await asyncio.wait_for(llm_gateway.complete(
                [{"role": "system", "content": DECIDE_PROMPT}, {"role": "user", "content": user}],
                None, _background_connection(connection), kind="address"), DECIDE_TIMEOUT)
    except Exception as exc:  # noqa: BLE001 — сбой диспетчера не мешает чату
        logger.warning("Адресат реплики %s не определён: %s", msg.id, exc)
        return AI, "fallback"
    got = parse_answer(out)
    return (got, "model") if got else (AI, "fallback")
