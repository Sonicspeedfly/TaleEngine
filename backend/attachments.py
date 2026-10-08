"""
Хранение ДАННЫХ вложений отдельно от сообщений (таблица attachment_blobs).

Раньше base64 картинок/аудио/видео лежал прямо в JSON-колонке
messages.attachments — и каждый ход/открытие чата поднимал в память СОТНИ
мегабайт (чат с парой видео = 170+ МБ на каждое чтение истории). Теперь в
messages.attachments живёт только лёгкая мета {type, mime, name, size, blob_id},
а тяжёлый base64 достаётся ТОЧЕЧНО и только когда реально нужен:

  * показ вложения в браузере — /api/messages/{id}/att/{idx};
  * запрос к модели — только файлы, которые уходят целиком (остальные идут
    ссылкой на копию в хранилище провайдера, см. backend/media_refs.py);
  * retry хода / референсы артов / полный экспорт чата — по одному сообщению.

Старые записи с инлайн-`data` поддерживаются везде (и мигрируются на старте —
см. database._migrate_attachment_blobs).
"""
from backend import models
from backend.schemas import AttachmentIn


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
            # Мета без данных приходит только из импорта. Чужой blob_id в ней —
            # ссылка на ЧУЖОЙ файл этой базы (его отдали бы и в браузер, и модели
            # ссылкой): такой id не принимаем, остаётся пометка о файле.
            metas.append({k: v for k, v in d.items() if k not in ("data", "blob_id")})
            continue
        blob = models.AttachmentBlob(message_id=message_id, data=data)
        db.add(blob)
        await db.flush()  # получаем blob.id, не закрывая транзакцию
        metas.append({
            "type": d.get("type"),
            "mime": d.get("mime"),
            "name": d.get("name"),
            # Размер — всегда по самим данным: по нему считается вес файла в
            # запросе, и присланному снаружи числу (импорт чата) верить нельзя.
            "size": _decoded_size(data),
            "blob_id": blob.id,
        })
    return metas


def _decoded_size(data: str) -> int:
    """Размер файла в байтах по его base64 (или data:URI) — без декодирования."""
    head = data.find(";base64,", 0, 512)
    payload = len(data) - (head + 8 if head >= 0 else (data.find(",", 0, 512) + 1 if data[:5] == "data:" else 0))
    return max(0, int(payload * 3 / 4) - (data[-2:].count("=") if data else 0))


async def load_blob(db, blob_id, owner: int | None = None) -> str:
    """
    Данные файла по blob_id. owner — id сообщения, которому файл должен
    принадлежать: id в SQLite переиспользуются, и ссылка, пережившая удаление
    своего файла, иначе отдала бы файл ДРУГОГО чата.
    """
    if not blob_id:
        return ""
    blob = await db.get(models.AttachmentBlob, int(blob_id))
    if blob is None or (owner is not None and blob.message_id != owner):
        return ""
    return blob.data or ""


async def attachment_data(db, att: dict, owner: int | None = None) -> str:
    """data вложения: инлайн (легаси) или из blob-таблицы (см. load_blob про owner)."""
    return (att.get("data") or "") or await load_blob(db, att.get("blob_id"), owner)


async def copy_attachments(db, message_id: int, source) -> list[dict]:
    """
    Вложения сообщения-копии (ветка, продолжение): свои blob'ы, а не ссылки на
    файлы исходного сообщения. Общая ссылка жила до удаления исходного чата —
    дальше файл пропадал, а освободившийся id получал файл другого чата.
    """
    out = []
    for a in source.attachments or []:
        if not isinstance(a, dict):
            continue
        data = await attachment_data(db, a, owner=source.id)
        if data:
            out.extend(await store_attachments(db, message_id, [{**a, "data": data}]))
        else:
            out.append({k: v for k, v in a.items() if k not in ("data", "blob_id")})
    return out


async def message_attachments_in(db, msg) -> list[AttachmentIn]:
    """ПОЛНЫЕ вложения одного сообщения (retry хода, повторная генерация)."""
    out: list[AttachmentIn] = []
    for a in (msg.attachments or []):
        if not isinstance(a, dict):
            continue
        data = await attachment_data(db, a, owner=msg.id)
        if not data:
            continue
        out.append(AttachmentIn(
            type=a.get("type") or "document", data=data,
            mime=a.get("mime"), name=a.get("name"),
        ))
    return out


# Последние раскодированные аудио и видео (см. main.get_attachment): перемотка —
# это серия Range-запросов к одному файлу, и без кэша каждый раскодировал бы весь
# base64 заново. Чистится при удалении вложений: id в SQLite переиспользуются.
_att_cache: dict = {}
_ATT_CACHE_BYTES = 96 * 1024 * 1024


def att_cache_get(key):
    return _att_cache.get(key)


def att_cache_put(key, raw: bytes) -> None:
    if len(raw) > _ATT_CACHE_BYTES // 2:
        return
    _att_cache.pop(key, None)
    _att_cache[key] = raw
    while sum(len(v) for v in _att_cache.values()) > _ATT_CACHE_BYTES:
        _att_cache.pop(next(iter(_att_cache)))


async def delete_message_blobs(db, message_ids) -> None:
    """Удаляет данные вложений для перечисленных сообщений (или подзапроса id)."""
    _att_cache.clear()
    from sqlalchemy import delete as sql_delete

    await db.execute(
        sql_delete(models.AttachmentBlob).where(
            models.AttachmentBlob.message_id.in_(message_ids)
        )
    )
    # Ссылки на копии этих файлов в хранилище модели — убрать (фоном, после commit).
    from backend.media_refs import uploader

    uploader.sweep_soon()


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
            data = await attachment_data(db, a, owner=m.id)
            meta = {k: v for k, v in a.items() if k != "blob_id"}
            if data:
                meta["data"] = data
            full.append(meta)
        m_dict["attachments"] = full
