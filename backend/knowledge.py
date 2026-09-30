"""
База знаний чата: постоянные справочные файлы, доступные модели В КАЖДОМ ходе.

В отличие от разовых вложений сообщения, файлы базы знаний привязаны к чату и
подмешиваются в контекст всегда — модель/персонажи «знают» их содержимое и
отвечают по нему. Документы храним извлечённым ТЕКСТОМ (дёшево пересылать
каждый ход), медиа/PDF — данными в attachment_blobs.
"""
from backend import models
from backend.document_service import is_document, prepare_document

# Сколько текста ХРАНИМ у одного файла базы знаний. Хранение дёшево, поэтому
# потолок щедрый: обрезаем уже на сборке контекста (см. build_knowledge).
_MAX_KB_TEXT = 200_000


def _kind_of(mime: str | None, name: str | None) -> str:
    m = (mime or "").lower()
    if m.startswith("image/"):
        return "image"
    if m.startswith("audio/"):
        return "audio"
    if m.startswith("video/"):
        return "video"
    return "document" if is_document(mime, name) else "document"


async def add_file(db, session_id: int, owner_id, name: str, mime: str | None, data: str) -> models.KnowledgeFile:
    """
    Добавить файл в базу знаний чата. Документы -> извлекаем текст (кешируем),
    медиа/PDF -> сохраняем данные в blob. Возвращает созданную запись.
    """
    from backend.attachments import store_attachments
    from backend.schemas import AttachmentIn

    kind = _kind_of(mime, name)
    content = ""
    blob_id = None

    if kind == "document":
        # Один раз извлекаем текст: docx/txt/csv/md — через prepare_document;
        # PDF — через pypdf. Текст ДЁШЕВО слать каждый ход (вместо тяжёлого файла).
        block = prepare_document(data, mime, name)
        if block.get("type") == "text":
            content = block.get("text", "")
        else:
            # PDF-блок: пробуем вытащить текст, иначе храним файл (скан без текста).
            from backend.document_service import _decode, extract_pdf_text

            pdf_text = extract_pdf_text(_decode(data))
            if pdf_text:
                content = f"[Документ «{name}» — содержимое ниже]\n\n{pdf_text}"
            else:
                metas = await store_attachments(db, None, [AttachmentIn(type="document", data=data, mime=mime, name=name)])
                blob_id = (metas[0].get("blob_id") if metas else None)
    else:
        metas = await store_attachments(db, None, [AttachmentIn(type=kind, data=data, mime=mime, name=name)])
        blob_id = (metas[0].get("blob_id") if metas else None)

    kf = models.KnowledgeFile(
        session_id=session_id, owner_id=owner_id, name=name or "файл",
        mime=mime, kind=kind, content=content[:_MAX_KB_TEXT], blob_id=blob_id,
    )
    db.add(kf)
    await db.commit()
    await db.refresh(kf)
    return kf


async def list_files(db, session_id: int) -> list[models.KnowledgeFile]:
    from sqlalchemy import select

    return list((await db.execute(
        select(models.KnowledgeFile)
        .where(models.KnowledgeFile.session_id == session_id)
        .order_by(models.KnowledgeFile.id)
    )).scalars().all())


async def build_knowledge(
    db, session_id: int, max_chars: int | None = None
) -> tuple[str, list[dict]]:
    """
    Собирает базу знаний чата для контекста:
      * knowledge_text — склеенный текст документов (один system-блок);
      * media_msgs — user-сообщения с медиа/PDF (каждый помечен как база знаний).
    Пусто, если базы нет.

    :param max_chars: потолок текста справочника В КОНТЕКСТЕ. База уходит в
        КАЖДЫЙ запрос, поэтому её размер — постоянная статья расхода: 200 тыс.
        символов ≈ 50 тыс. токенов входа на каждом ходу. None — дефолт из
        настроек (KNOWLEDGE_TEXT_CHARS); 0 — без ограничения.
    """
    from backend.config import settings
    from backend.media_refs import placeholder

    files = await list_files(db, session_id)
    if not files:
        return "", []

    if max_chars is None:
        max_chars = settings.KNOWLEDGE_TEXT_CHARS
    budget = max_chars if max_chars and max_chars > 0 else _MAX_KB_TEXT

    text_parts: list[str] = []
    media_msgs: list[dict] = []
    used = 0
    # Размер медиа — для оценки их веса в токенах и лимита «целиком»; у файла
    # базы знаний своей колонки размера нет, поэтому — длина base64 в blob.
    sizes = await _blob_sizes(db, [f.blob_id for f in files if not f.content and f.blob_id])
    for f in files:
        if f.content:
            chunk = f.content
            if used + len(chunk) > budget:
                chunk = chunk[: max(0, budget - used)]
            if chunk:
                text_parts.append(f"[Файл «{f.name}»]\n{chunk}")
                used += len(chunk)
        elif f.blob_id:
            # Заготовка, а не данные: ссылкой на копию в хранилище модели (или
            # целиком, пока влезает) файл сделает stream_completion — см.
            # backend/media_refs.py. Раньше медиа базы знаний уходили base64-ом
            # в КАЖДОМ запросе без всякого лимита.
            block = placeholder({"type": f.kind if f.kind in ("image", "audio", "video") else "document",
                                 "mime": f.mime, "name": f.name, "size": sizes.get(f.blob_id, 0),
                                 "blob_id": f.blob_id}, priority=1)
            if block is None:
                continue
            media_msgs.append({
                "role": "user",
                "content": [
                    {"type": "text", "text": f"[База знаний — файл «{f.name}»]"},
                    block,
                ],
            })

    knowledge_text = ""
    if text_parts:
        knowledge_text = "\n\n".join(text_parts)
    return knowledge_text, media_msgs


async def _blob_sizes(db, blob_ids) -> dict[int, int]:
    """{blob_id: размер файла в байтах} по длине base64 (данные в Python не читаются)."""
    from sqlalchemy import func, select

    ids = sorted({int(i) for i in blob_ids if i})
    if not ids:
        return {}
    rows = (await db.execute(
        select(models.AttachmentBlob.id, func.length(models.AttachmentBlob.data))
        .where(models.AttachmentBlob.id.in_(ids))
    )).all()
    return {i: int((n or 0) * 3 / 4) for i, n in rows}
