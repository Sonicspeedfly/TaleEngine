"""
Личные сообщения между людьми (2.29.0): раздел «Люди».

Общий чат — это чат с персонажем, куда позвали друзей. А поговорить с другом
напрямую или переслать ему понравившуюся реплику было негде. Здесь — простая
переписка двоих без ИИ: только между друзьями (принятая дружба), только в
режиме аккаунтов. Новое сообщение сразу уходит обоим по шине событий
(turn_events, тип "dm" со списком users — SSE раздаёт его только им).

Пересылка: реплика чата сохраняется снимком (кто сказал, из какого чата, текст
до FORWARD_CHARS) — получатель видит её, даже если доступа к чату у него нет;
ссылка «Открыть в чате» сработает, если доступ есть.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, or_, select, update

from backend import models, turn_events

MAX_CHARS = 8000
FORWARD_CHARS = 4000
PAGE = 50


def pair_of(a: int, b: int) -> str:
    lo, hi = sorted((int(a), int(b)))
    return f"{lo}:{hi}"


def _iso(dt) -> str | None:
    if not dt:
        return None
    return dt.isoformat() + ("" if dt.tzinfo else "Z")


def serialize(m) -> dict:
    return {"id": m.id, "sender_id": m.sender_id, "recipient_id": m.recipient_id,
            "content": m.content or "", "forward": m.forward, "read": m.read_at is not None,
            "created_at": _iso(m.created_at)}


def build_router(current_user, *, are_friends, can_access_session):
    from fastapi import APIRouter, Body, Depends, HTTPException
    from sqlalchemy.ext.asyncio import AsyncSession

    from backend import images
    from backend.database import get_session

    router = APIRouter(prefix="/api/dm")

    def _need_user(user):
        if user is None:
            raise HTTPException(409, "Личные сообщения есть только в режиме аккаунтов")
        return user

    async def _friend_ids(db, uid: int) -> list[int]:
        rows = (await db.execute(select(models.Friendship).where(
            models.Friendship.status == "accepted",
            or_(models.Friendship.user_id == uid, models.Friendship.friend_id == uid)))).scalars().all()
        return [f.friend_id if f.user_id == uid else f.user_id for f in rows]

    @router.get("")
    async def threads(user=Depends(current_user), db: AsyncSession = Depends(get_session)):
        """Друзья и переписка с ними: последнее сообщение и непрочитанные."""
        if user is None:
            return {"enabled": False, "threads": [], "unread": 0}
        ids = await _friend_ids(db, user.id)
        if not ids:
            return {"enabled": True, "threads": [], "unread": 0}
        users = {u.id: u for u in (await db.execute(
            select(models.User).where(models.User.id.in_(ids)))).scalars().all()}
        avatars = await images.public_values(db, "user", list(users))
        pairs = {pair_of(user.id, i): i for i in users}
        last_ids = dict((await db.execute(
            select(models.DirectMessage.pair, func.max(models.DirectMessage.id))
            .where(models.DirectMessage.pair.in_(list(pairs))).group_by(models.DirectMessage.pair))).all())
        lasts = {}
        if last_ids:
            lasts = {m.pair: m for m in (await db.execute(
                select(models.DirectMessage).where(models.DirectMessage.id.in_(list(last_ids.values())))
            )).scalars().all()}
        unread = dict((await db.execute(
            select(models.DirectMessage.sender_id, func.count(models.DirectMessage.id))
            .where(models.DirectMessage.recipient_id == user.id, models.DirectMessage.read_at.is_(None))
            .group_by(models.DirectMessage.sender_id))).all())
        out = []
        for pair, uid in pairs.items():
            u = users[uid]
            last = lasts.get(pair)
            out.append({
                "user": {"id": u.id, "name": u.username, "avatar": avatars.get(u.id)},
                "last": ({"content": (last.content or (last.forward or {}).get("content") or "")[:120],
                          "forward": bool(last.forward), "mine": last.sender_id == user.id,
                          "at": _iso(last.created_at), "id": last.id} if last else None),
                "unread": int(unread.get(uid, 0)),
            })
        out.sort(key=lambda r: (-(r["last"]["id"] if r["last"] else 0), r["user"]["name"].lower()))
        return {"enabled": True, "threads": out, "unread": sum(r["unread"] for r in out)}

    @router.get("/{other_id}")
    async def messages(other_id: int, before: int | None = None, user=Depends(current_user),
                       db: AsyncSession = Depends(get_session)):
        """Переписка с другом (последние PAGE, before — постранично вверх); входящие — прочитаны."""
        user = _need_user(user)
        if not await are_friends(db, user.id, other_id):
            raise HTTPException(403, "Переписка — только с друзьями")
        q = select(models.DirectMessage).where(models.DirectMessage.pair == pair_of(user.id, other_id))
        if before:
            q = q.where(models.DirectMessage.id < before)
        rows = list(reversed((await db.execute(
            q.order_by(models.DirectMessage.id.desc()).limit(PAGE))).scalars().all()))
        if not before and rows:
            # Открыл переписку — прочитано ВСЁ входящее, а не только последняя страница.
            await _mark_read(db, user.id, other_id, rows[-1].id)
            for m in rows:
                if m.recipient_id == user.id and m.read_at is None:
                    m.read_at = datetime.utcnow()
        return {"messages": [serialize(m) for m in rows], "more": len(rows) == PAGE}

    async def _mark_read(db, uid: int, other_id: int, upto: int) -> int:
        res = await db.execute(update(models.DirectMessage).where(
            models.DirectMessage.pair == pair_of(uid, other_id), models.DirectMessage.recipient_id == uid,
            models.DirectMessage.read_at.is_(None), models.DirectMessage.id <= upto,
        ).values(read_at=datetime.utcnow()))
        await db.commit()
        if res.rowcount:
            turn_events.publish({"type": "dm_read", "users": [uid, other_id], "reader_id": uid, "upto": upto})
        return res.rowcount or 0

    @router.post("/{other_id}/read")
    async def mark_read(other_id: int, user=Depends(current_user), db: AsyncSession = Depends(get_session)):
        """Прочитал пришедшее, пока переписка открыта (без перечитывания страницы)."""
        user = _need_user(user)
        if not await are_friends(db, user.id, other_id):
            raise HTTPException(403, "Переписка — только с друзьями")
        top = (await db.execute(select(func.max(models.DirectMessage.id)).where(
            models.DirectMessage.pair == pair_of(user.id, other_id)))).scalar() or 0
        return {"read": await _mark_read(db, user.id, other_id, top)}

    @router.post("/{other_id}")
    async def send(other_id: int, payload: dict = Body(...), user=Depends(current_user),
                   db: AsyncSession = Depends(get_session)):
        """Написать другу. forward_message_id — переслать реплику чата (с комментарием или без)."""
        user = _need_user(user)
        if other_id == user.id or not await are_friends(db, user.id, other_id):
            raise HTTPException(403, "Писать можно только друзьям")
        content = str(payload.get("content") or "").strip()[:MAX_CHARS]
        forward = None
        fid = payload.get("forward_message_id")
        if fid:
            try:
                src = await db.get(models.Message, int(fid))
            except (TypeError, ValueError):
                src = None
            sess = await db.get(models.ChatSession, src.session_id) if src is not None else None
            if src is None or not await can_access_session(db, sess, user):
                raise HTTPException(404, "Сообщение не найдено")
            if src.role == "assistant":
                speaker = src.speaker_name
                if not speaker and sess.character_id:
                    ch = await db.get(models.Character, sess.character_id)
                    speaker = ch.name if ch else ""
            else:
                # Старые реплики без автора — владельца чата.
                author = await db.get(models.User, src.author_id or sess.owner_id) \
                    if (src.author_id or sess.owner_id) else None
                speaker = author.username if author else ""
            forward = {"session_id": sess.id, "message_id": src.id, "chat_title": sess.title or "",
                       "speaker": speaker or "", "role": src.role,
                       "content": (src.content or "")[:FORWARD_CHARS],
                       "truncated": len(src.content or "") > FORWARD_CHARS}
        if not content and not forward:
            raise HTTPException(422, "Пустое сообщение")
        m = models.DirectMessage(pair=pair_of(user.id, other_id), sender_id=user.id,
                                 recipient_id=other_id, content=content, forward=forward)
        db.add(m)
        await db.commit()
        data = serialize(m)
        turn_events.publish({"type": "dm", "users": [user.id, other_id], "message": data,
                             "sender_name": user.username})
        return data

    return router


def visible_to(event: dict, user) -> bool | None:
    """Событие личной переписки — только её двоим. None — это не событие «dm»."""
    if event.get("type") not in ("dm", "dm_read"):
        return None
    return user is not None and user.id in (event.get("users") or [])
