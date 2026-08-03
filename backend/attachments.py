"""
Хранение ДАННЫХ вложений отдельно от сообщений (таблица attachment_blobs).

Раньше base64 картинок/аудио/видео лежал прямо в JSON-колонке
messages.attachments — и каждый ход/открытие чата поднимал в память СОТНИ
мегабайт (чат с парой видео = 170+ МБ на каждое чтение истории). Теперь в
messages.attachments живёт только лёгкая мета {type, mime, name, size, blob_id},
а тяжёлый base64 достаётся ТОЧЕЧНО и только когда реально нужен:

  * показ вложения в браузере — /api/messages/{id}/att/{idx};
  * сборка контекста — только вложения, влезающие в лимит истории
    (решение «влезает/нет» принимается ПО МЕТЕ, большие blobs даже не читаются);
  * retry хода / референсы артов / полный экспорт чата — по одному сообщению.

Старые записи с инлайн-`data` поддерживаются везде (и мигрируются на старте —
см. database._migrate_attachment_blobs).
"""
from backend import models
from backend.schemas import AttachmentIn


def meta_size(a: dict) -> int:
    """~Объём данных вложения в символах base64 (для лимитов истории)."""
    d = a.get("data")
    if d:
        return len(d)
    return int((a.get("size") or 0) * 4 / 3)  # size хранится «сырым», base64 длиннее


async def store_attachments(db, message_id: int, attachments) -> list[dict]:
    """
    Сохраняет вложения сообщения: data -> attachment_blobs, возвращает список
    мет для messages.attachments. Принимает AttachmentIn или сырые dict'ы
    (импорт); элементы без data (уже мета) проходят как есть.
    """
    metas: list[dict] = []
    for a in attachments or []:
        d = a if isinstance(a, dict) else a.model_dump()
        data = d.get("data") or ""
        if not data:
            metas.append({k: v for k, v in d.items() if k != "data"})
            continue
        blob = models.AttachmentBlob(message_id=message_id, data=data)
        db.add(blob)
        await db.flush()  # получаем blob.id, не закрывая транзакцию
        metas.append({
            "type": d.get("type"),
            "mime": d.get("mime"),
            "name": d.get("name"),
            "size": d.get("size") or int(len(data) * 0.75),
            "blob_id": blob.id,
        })
    return metas


async def load_blob(db, blob_id) -> str:
    if not blob_id:
        return ""
    blob = await db.get(models.AttachmentBlob, int(blob_id))
    return (blob.data if blob else "") or ""


async def attachment_data(db, att: dict) -> str:
    """data вложения: инлайн (легаси) или из blob-таблицы."""
    return (att.get("data") or "") or await load_blob(db, att.get("blob_id"))


async def message_attachments_in(db, msg) -> list[AttachmentIn]:
    """ПОЛНЫЕ вложения одного сообщения (retry хода, повторная генерация)."""
    out: list[AttachmentIn] = []
    for a in (msg.attachments or []):
        if not isinstance(a, dict):
            continue
        data = await attachment_data(db, a)
        if not data:
            continue
        out.append(AttachmentIn(
            type=a.get("type") or "document", data=data,
            mime=a.get("mime"), name=a.get("name"),
        ))
    return out


async def load_history_attachments(
    db, msgs, budget_chars: int | None, turns_window: int | None = None
) -> dict:
    """
    Вложения для ИСТОРИИ контекста: возвращает {message_id: [att dict С data]}.

    Отбор идёт от СВЕЖИХ сообщений к старым по двум независимым ограничениям —
    срабатывает то, которое жёстче:

    :param budget_chars: суммарный объём данных (символы base64). None — без
        лимита по объёму. Не влезшие сообщения отбрасываются ПО МЕТЕ, их данные
        вообще не читаются из БД (иначе чат с видео поднимал бы сотни МБ).
    :param turns_window: сколько ПОСЛЕДНИХ сообщений вообще имеют право нести
        файлы. None/0 — без ограничения по возрасту.

    Зачем возрастное окно: лимит в МБ не спасает от «долгих» чатов, где десятки
    мелких картинок по отдельности дёшевы, но все вместе висят в КАЖДОМ запросе до
    конца жизни чата. Файл вне окна модель не видит целиком, но знает о нём из
    текстовой пометки «[изображение] / [видео: имя]» (см. messages_to_history).
    """
    msgs = list(msgs)
    # Граница возрастного окна: сообщения старше неё файлы не несут.
    oldest_allowed_idx = 0
    if turns_window and turns_window > 0:
        oldest_allowed_idx = max(0, len(msgs) - turns_window)

    out: dict = {}
    used = 0
    for idx in range(len(msgs) - 1, oldest_allowed_idx - 1, -1):
        m = msgs[idx]
        atts = [
            a for a in (m.attachments or [])
            if isinstance(a, dict) and (a.get("data") or a.get("blob_id"))
        ]
        if not atts:
            continue
        if budget_chars is not None:
            size = sum(meta_size(a) for a in atts)
            if used + size > budget_chars:
                continue  # это сообщение не влезло — более старые могут быть меньше
            used += size
        hydrated = []
        for a in atts:
            data = await attachment_data(db, a)
            if data:
                hydrated.append({**a, "data": data})
        if hydrated:
            out[m.id] = hydrated
    return out


async def delete_message_blobs(db, message_ids) -> None:
    """Удаляет данные вложений для перечисленных сообщений (или подзапроса id)."""
    from sqlalchemy import delete as sql_delete

    await db.execute(
        sql_delete(models.AttachmentBlob).where(
            models.AttachmentBlob.message_id.in_(message_ids)
        )
    )


async def hydrate_export_attachments(db, export_dict: dict, messages) -> None:
    """
    Полный экспорт чата: подставляет data вложений из blobs в уже собранный
    словарь экспорта (blob_id вырезается — он бессмыслен в другой БД).
    """
    for m_dict, m in zip(export_dict.get("messages") or [], messages):
        atts = m.attachments or []
        if not atts:
            continue
        full = []
        for a in atts:
            if not isinstance(a, dict):
                continue
            data = await attachment_data(db, a)
            meta = {k: v for k, v in a.items() if k != "blob_id"}
            if data:
                meta["data"] = data
            full.append(meta)
        m_dict["attachments"] = full
