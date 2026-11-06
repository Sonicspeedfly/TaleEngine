"""
Черновики сообщений, привязанные к чату (2.14.0).

Поле ввода больше не «абстракция для отправки», а черновик конкретного чата у
конкретного пользователя: текст, ответ на сообщение и файлы лежат на сервере.
Отсюда:
  * переключился в другой чат — там свой черновик, недописанное не переезжает;
  * начал на телефоне — продолжил на компьютере (и докинул файл);
  * отправил — черновик очищается сервером, на всех устройствах.

Открытые вкладки узнают о чужой правке по шине уведомлений (событие "draft",
backend/turn_events.py) и перечитывают черновик. Конфликт двух устройств решает
version: сохранение со старой версией не затирает более свежее (409 + текущий
черновик в ответе).
"""
import base64
import logging

from sqlalchemy import delete as sql_delete, select, update

from backend import models, turn_events
from backend.config import settings

log = logging.getLogger("aichat.drafts")

TEXT_MAX = 200_000


def owner_of(user) -> int:
    """Владелец черновика: id пользователя, 0 — режим без аккаунтов."""
    return int(getattr(user, "id", 0) or 0)


async def _row(db, session_id: int, owner: int):
    return (await db.execute(select(models.ChatDraft).where(
        models.ChatDraft.session_id == session_id, models.ChatDraft.owner_id == owner,
    ))).scalar_one_or_none()


async def _files(db, session_id: int, owner: int, ids: list[int] | None = None) -> list:
    q = select(models.DraftFile.id, models.DraftFile.type, models.DraftFile.mime,
               models.DraftFile.name, models.DraftFile.size).where(
        models.DraftFile.session_id == session_id, models.DraftFile.owner_id == owner)
    if ids is not None:
        q = q.where(models.DraftFile.id.in_(ids or [-1]))
    return (await db.execute(q)).all()


class ChatGone(LookupError):
    """Чат удалили, пока писали его черновик."""


async def _ensure_chat(db, session_id: int) -> None:
    """
    Проверка ПОСЛЕ записи: соединение уже держит замок на запись, так что
    удаление чата либо закоммичено (видим — откатываемся), либо ещё впереди
    (тогда оно само снесёт этот черновик). Иначе черновик, записанный в миг
    удаления, достался бы новому чату с тем же id.
    """
    await db.flush()
    gone = (await db.execute(select(models.ChatSession.id).where(
        models.ChatSession.id == session_id))).scalar_one_or_none() is None
    if gone:
        await db.rollback()
        raise ChatGone(session_id)


async def _bump(db, session_id: int, owner: int, max_base: int | None = None):
    """
    Атомарно занять черновик под запись: версия +1 одним UPDATE (с условием
    «на сервере не новее max_base»). Проверка «прочитать версию → записать»
    пропускала два одновременных сохранения с одной базой — оба получали 200,
    и правка одного устройства молча терялась. Теперь второе получает 409.
    Возвращает свежую строку (уже с новой версией) или None — версия ушла вперёд.
    После успеха соединение держит замок на запись до commit.
    """
    dialect = db.get_bind().dialect.name
    if dialect in ("sqlite", "postgresql"):
        if dialect == "sqlite":
            from sqlalchemy.dialects.sqlite import insert as d_insert
        else:
            from sqlalchemy.dialects.postgresql import insert as d_insert
        # «Создать, если нет» одним оператором: два первых сохранения разом не
        # падают на уникальном ключе (session_id, owner_id).
        await db.execute(d_insert(models.ChatDraft).values(
            session_id=session_id, owner_id=owner, text="", files=[], version=0,
        ).on_conflict_do_nothing(index_elements=["session_id", "owner_id"]))
    elif await _row(db, session_id, owner) is None:
        db.add(models.ChatDraft(session_id=session_id, owner_id=owner, text="", files=[], version=0))
        await db.flush()
    cond = [models.ChatDraft.session_id == session_id, models.ChatDraft.owner_id == owner]
    if max_base is not None:
        cond.append(models.ChatDraft.version <= max_base)
    res = await db.execute(update(models.ChatDraft).where(*cond)
                           .values(version=models.ChatDraft.version + 1)
                           .execution_options(synchronize_session=False))
    if not res.rowcount:
        return None
    return (await db.execute(select(models.ChatDraft).where(
        models.ChatDraft.session_id == session_id, models.ChatDraft.owner_id == owner,
    ).execution_options(populate_existing=True))).scalar_one()


def _file_meta(f) -> dict:
    return {"fid": f.id, "type": f.type, "mime": f.mime, "name": f.name, "size": f.size}


async def get(db, session_id: int, owner: int) -> dict:
    """Черновик для клиента: текст, ответ, файлы (мета, без данных), версия."""
    row = await _row(db, session_id, owner)
    if row is None:
        return {"text": "", "reply_to_id": None, "files": [], "version": 0}
    order = [int(x) for x in (row.files or []) if isinstance(x, int)]
    by_id = {f.id: f for f in await _files(db, session_id, owner, order)}
    return {
        "text": row.text or "", "reply_to_id": row.reply_to_id,
        "files": [_file_meta(by_id[i]) for i in order if i in by_id],
        "version": row.version or 0,
    }


def _announce(session_id: int, owner: int, version: int, origin: str = "") -> None:
    turn_events.publish({"type": "draft", "session_id": session_id, "owner_id": owner,
                         "version": version, "origin": origin})


async def save(db, session_id: int, owner: int, *, text: str, reply_to_id, files: list,
               base_version: int | None, origin: str = "") -> tuple[bool, dict]:
    """
    Сохранить черновик. files — id файлов черновика в нужном порядке; файлы,
    которых в списке нет, удаляются. base_version — версия, на которой основана
    правка: если на сервере уже новее (правили с другого устройства), ничего не
    пишем и возвращаем (False, текущий черновик).
    """
    row = await _bump(db, session_id, owner, base_version)
    if row is None:
        await db.rollback()
        return False, await get(db, session_id, owner)
    known = {f.id for f in await _files(db, session_id, owner)}
    keep = []
    for fid in files or []:
        try:
            fid = int(fid)
        except (TypeError, ValueError):
            continue
        if fid in known and fid not in keep:
            keep.append(fid)
    doomed = known - set(keep)
    if doomed:
        await db.execute(sql_delete(models.DraftFile).where(models.DraftFile.id.in_(doomed)))
    row.text = (text or "")[:TEXT_MAX]
    row.reply_to_id = reply_to_id if isinstance(reply_to_id, int) else None
    row.files = keep
    version = row.version
    await _ensure_chat(db, session_id)
    await db.commit()
    _announce(session_id, owner, version, origin)
    return True, await get(db, session_id, owner)


async def add_file(db, session_id: int, owner: int, *, name: str, mime: str, kind: str,
                   raw: bytes, origin: str = "") -> dict:
    """Прикрепить файл к черновику. Слишком большой — ValueError (остаётся на устройстве)."""
    limit = settings.DRAFT_FILE_MAX_MB * 1024 * 1024
    if len(raw) > limit:
        raise ValueError(f"Файл больше {settings.DRAFT_FILE_MAX_MB} МБ — в черновике не синхронизируется")
    row = await _bump(db, session_id, owner)   # замок: параллельные загрузки не обойдут общий лимит
    used = sum(f.size or 0 for f in await _files(db, session_id, owner))
    if used + len(raw) > settings.DRAFT_TOTAL_MAX_MB * 1024 * 1024:
        await db.rollback()
        raise ValueError(f"Файлы черновика больше {settings.DRAFT_TOTAL_MAX_MB} МБ — этот не синхронизируется")
    mime = (mime or "application/octet-stream")[:200]
    f = models.DraftFile(
        session_id=session_id, owner_id=owner, type=(kind or "document")[:20], mime=mime,
        name=(name or "файл")[:300], size=len(raw),
        data=f"data:{mime};base64," + base64.b64encode(raw).decode(),
    )
    db.add(f)
    await db.flush()
    row.files = [*(row.files or []), f.id]
    version = row.version
    meta = _file_meta(f)
    await _ensure_chat(db, session_id)
    await db.commit()
    _announce(session_id, owner, version, origin)
    return {**meta, "version": version}


async def file_data(db, session_id: int, owner: int, fid: int) -> tuple[bytes, str, str] | None:
    f = (await db.execute(select(models.DraftFile).where(
        models.DraftFile.id == fid, models.DraftFile.session_id == session_id,
        models.DraftFile.owner_id == owner))).scalar_one_or_none()
    if f is None:
        return None
    data = f.data or ""
    b64 = data.split(",", 1)[1] if data.startswith("data:") and "," in data else data
    return base64.b64decode(b64), f.mime, f.name


def _ints(xs) -> set[int]:
    out = set()
    for x in xs or []:
        try:
            out.add(int(x))
        except (TypeError, ValueError):
            continue
    return out


async def clear(db, session_id: int, owner: int, *, sent_text: str | None = None,
                version: int | None = None, fids=None, origin: str = "") -> None:
    """
    Сообщение ушло — убираем из черновика ОТПРАВЛЕННОЕ, на всех устройствах.

    version — версия черновика, когда нажали «Отправить», fids — его файлы,
    ушедшие с сообщением. Ход стартует не мгновенно (загрузка большого файла —
    минуты), и человек мог уже начать следующее сообщение: если черновик с тех
    пор менялся, его новый текст и новые файлы не трогаем, удаляем только
    отправленные файлы. Текст очищаем, если версия та же или в черновике лежит
    ровно отправленный текст (сохранение, ушедшее за миг до отправки).
    version=None — старый клиент без этих сведений: очищаем всё, как раньше.
    """
    sent = _ints(fids)
    if await _row(db, session_id, owner) is None:
        return   # черновика не было — убирать нечего
    row = await _bump(db, session_id, owner)   # замок до чтения: не разминуться с сохранением
    current = (row.version or 1) - 1           # версия до этой записи
    untouched = version is None or current <= version
    same_text = row is not None and sent_text is not None \
        and (row.text or "").strip() == (sent_text or "").strip()
    if untouched:
        await db.execute(sql_delete(models.DraftFile).where(
            models.DraftFile.session_id == session_id, models.DraftFile.owner_id == owner))
    elif sent:
        await db.execute(sql_delete(models.DraftFile).where(
            models.DraftFile.session_id == session_id, models.DraftFile.owner_id == owner,
            models.DraftFile.id.in_(sent)))
    files = [] if untouched else [f for f in (row.files or []) if f not in sent]
    clear_text = untouched or same_text
    if not clear_text and files == list(row.files or []):
        await db.rollback()
        return   # отправленного в черновике уже нет — нечего менять
    if clear_text:
        row.text, row.reply_to_id = "", None
    row.files = files
    new_version = row.version
    await db.commit()
    _announce(session_id, owner, new_version, origin)


async def drop_session(db, session_id: int) -> None:
    """Чат удалён — черновики и их файлы всех пользователей (коммит — у вызывающего)."""
    for model in (models.ChatDraft, models.DraftFile):
        await db.execute(sql_delete(model).where(model.session_id == session_id))
