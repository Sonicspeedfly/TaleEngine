"""
Тесты подготовки документов к отправке в нейросеть (Word/PDF/текст).

Word без LibreOffice конвертируется в ТЕКСТ (надёжно, в один ход доходит до
модели); PDF отдаётся как документ; .txt — как текст.
"""
import base64
import io

import pytest

from backend.document_service import is_document, prepare_document


def test_is_document_by_mime_and_name():
    assert is_document("application/pdf", None)
    assert is_document(None, "лор.docx")
    assert is_document(None, "notes.TXT")
    assert not is_document("image/png", "pic.png")
    assert not is_document(None, "song.mp3")


def _docx_bytes() -> bytes:
    docx = pytest.importorskip("docx")  # python-docx
    d = docx.Document()
    d.add_heading("Лор мира", level=1)
    d.add_paragraph("Город Вестенхолл стоит на реке.")
    table = d.add_table(rows=1, cols=2)
    table.rows[0].cells[0].text = "Ария"
    table.rows[0].cells[1].text = "маг"
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def test_docx_extracted_as_text_when_no_libreoffice():
    raw = _docx_bytes()
    data = "data:application/vnd.openxmlformats-officedocument.wordprocessingml.document;base64," + base64.b64encode(raw).decode()
    block = prepare_document(
        data,
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "лор.docx",
    )
    # Без LibreOffice — отдаём текст с пометкой имени файла и кириллицей.
    assert block["type"] == "text"
    assert "лор.docx" in block["text"]
    assert "Вестенхолл" in block["text"]
    assert "Ария | маг" in block["text"]


def test_pdf_passthrough_as_image_url_block():
    data = "data:application/pdf;base64," + base64.b64encode(b"%PDF-1.4 fake").decode()
    block = prepare_document(data, "application/pdf", "doc.pdf")
    assert block["type"] == "image_url"
    assert block["image_url"]["url"].startswith("data:application/pdf;base64,")


def test_plain_text_document():
    data = "data:text/plain;base64," + base64.b64encode("привет мир".encode()).decode()
    block = prepare_document(data, "text/plain", "note.txt")
    assert block["type"] == "text"
    assert "привет мир" in block["text"]


def test_binary_file_not_dumped_as_text():
    """
    Регресс бага «отправка видео ломает чат»: бинарник (видео/архив) раньше
    декодировался latin-1 и в контекст уходили мегабайты мусора. Теперь — короткая
    пометка о неподдерживаемом формате.
    """
    fake_binary = bytes(range(256)) * 64  # 16 КБ псевдо-видео с NUL-байтами
    data = base64.b64encode(fake_binary).decode()
    block = prepare_document(data, "application/octet-stream", "clip.bin")
    assert block["type"] == "text"
    assert "не поддерживается" in block["text"]
    assert len(block["text"]) < 300  # мусор в текст не попал
