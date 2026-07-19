"""
База знаний чата: постоянные справочные файлы, доступные модели В КАЖДОМ ходе.

В отличие от разовых вложений сообщения, файлы базы знаний привязаны к чату и
подмешиваются в контекст всегда — модель/персонажи «знают» их содержимое и
отвечают по нему. Документы храним извлечённым ТЕКСТОМ (дёшево пересылать
каждый ход), медиа/PDF — данными в attachment_blobs.
"""
from backend import models
from backend.document_service import is_document, prepare_document

# Ограничение объёма текста базы знаний в контексте (символы) — чтобы очень
# большая база не раздувала каждый запрос до бесконечности.
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


async def build_knowledge(db, session_id: int) -> tuple[str, list[dict]]:
    """
    Собирает базу знаний чата для контекста:
      * knowledge_text — склеенный текст документов (один system-блок);
      * media_msgs — user-сообщения с медиа/PDF (каждый помечен как база знаний).
    Пусто, если базы нет.
    """
    from backend.attachments import load_blob
    from backend.llm_gateway import _content_from_attachment
    from backend.schemas import AttachmentIn

    files = await list_files(db, session_id)
    if not files:
        return "", []

    text_parts: list[str] = []
    media_msgs: list[dict] = []
    used = 0
    for f in files:
        if f.content:
            chunk = f.content
            if used + len(chunk) > _MAX_KB_TEXT:
                chunk = chunk[: max(0, _MAX_KB_TEXT - used)]
            if chunk:
                text_parts.append(f"[Файл «{f.name}»]\n{chunk}")
                used += len(chunk)
        elif f.blob_id:
            data = await load_blob(db, f.blob_id)
            if not data:
                continue
            try:
                block = _content_from_attachment(
                    AttachmentIn(type=(f.kind if f.kind != "document" else "document"),
                                 data=data, mime=f.mime, name=f.name)
                )
            except Exception:  # noqa: BLE001 — битый файл не должен рушить контекст
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
