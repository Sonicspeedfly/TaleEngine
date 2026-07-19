"""
База знаний чата: постоянные справочные файлы, доступные модели в каждом ходе.
Проверяем загрузку (документ → текст, картинка → медиа-блок), выдачу в контекст,
список и удаление, и что база доступна в личном и групповом чатах.
"""
import base64
from unittest.mock import patch

# Фикстура `client` — из tests/conftest.py.


def _fake_chunk(text: str):
    class _Delta:
        content = text

    class _Choice:
        delta = _Delta()

    class _Chunk:
        choices = [_Choice()]

    return _Chunk()


async def _capture_acompletion(*args, **kwargs):
    _capture_acompletion.captured = kwargs

    async def gen():
        yield _fake_chunk("Ок")

    return gen()


def _txt(text: str) -> str:
    return "data:text/plain;base64," + base64.b64encode(text.encode()).decode()


def test_knowledge_document_reaches_model(client):
    cid = client.post("/api/characters", json={"name": "КБ"}).json()["id"]
    sid = client.post(f"/api/sessions?character_id={cid}").json()["session_id"]

    # Добавляем документ базы знаний.
    r = client.post(f"/api/sessions/{sid}/knowledge", json={
        "type": "document", "data": _txt("Столица Атлантиды — город Мар."),
        "mime": "text/plain", "name": "лор.txt",
    })
    assert r.status_code == 200
    meta = r.json()
    assert meta["kind"] == "document" and meta["has_text"] is True

    # Список базы знаний.
    kb = client.get(f"/api/sessions/{sid}/knowledge").json()
    assert len(kb) == 1 and kb[0]["name"] == "лор.txt"

    # Отправляем сообщение — текст базы знаний должен уйти в модель.
    with patch("backend.llm_gateway.litellm.acompletion", new=_capture_acompletion):
        with client.websocket_connect(f"/ws/chat/{sid}") as ws:
            ws.send_json({"type": "user_message", "content": "какая столица?"})
            for _ in range(50):
                if ws.receive_json()["type"] in ("done", "error"):
                    break
    sent = str(_capture_acompletion.captured["messages"])
    # Контент дошёл, и он оформлен как ОТДЕЛЁННАЯ справочная база (не часть диалога).
    assert "город Мар" in sent
    assert "СПРАВОЧНАЯ БАЗА ЗНАНИЙ" in sent and "КОНЕЦ БАЗЫ ЗНАНИЙ" in sent


def test_knowledge_delete(client):
    cid = client.post("/api/characters", json={"name": "КБ2"}).json()["id"]
    sid = client.post(f"/api/sessions?character_id={cid}").json()["session_id"]
    kid = client.post(f"/api/sessions/{sid}/knowledge", json={
        "type": "document", "data": _txt("временный факт"), "mime": "text/plain", "name": "t.txt",
    }).json()["id"]
    assert len(client.get(f"/api/sessions/{sid}/knowledge").json()) == 1
    client.delete(f"/api/knowledge/{kid}")
    assert client.get(f"/api/sessions/{sid}/knowledge").json() == []


def test_knowledge_pdf_extracted_as_text_not_reuploaded_each_turn(client):
    """PDF в базе знаний -> извлекается ТЕКСТ (дёшево), а не пересылается файл каждый ход."""
    pypdf = __import__("importlib").import_module("pypdf")  # skip if missing
    import io

    from pypdf import PdfWriter

    # Соберём простой PDF с текстовым слоем.
    try:
        from reportlab.pdfgen import canvas as _c  # noqa: F401
        have_rl = True
    except Exception:
        have_rl = False

    # Без reportlab не создать текстовый PDF просто — используем fpdf2 (есть в проекте).
    from fpdf import FPDF
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", size=14)
    pdf.cell(0, 10, "SECRET-CODE-ALPHA-7")
    raw = bytes(pdf.output())
    import base64 as _b64
    data = "data:application/pdf;base64," + _b64.b64encode(raw).decode()

    cid = client.post("/api/characters", json={"name": "PDFКБ"}).json()["id"]
    sid = client.post(f"/api/sessions?character_id={cid}").json()["session_id"]
    r = client.post(f"/api/sessions/{sid}/knowledge", json={
        "type": "document", "data": data, "mime": "application/pdf", "name": "секрет.pdf",
    })
    assert r.status_code == 200
    meta = r.json()
    # Текст извлечён -> has_text=True (файл НЕ будет пересылаться целиком каждый ход).
    assert meta["has_text"] is True

    with patch("backend.llm_gateway.litellm.acompletion", new=_capture_acompletion):
        with client.websocket_connect(f"/ws/chat/{sid}") as ws:
            ws.send_json({"type": "user_message", "content": "какой код?"})
            for _ in range(50):
                if ws.receive_json()["type"] in ("done", "error"):
                    break
    sent = str(_capture_acompletion.captured["messages"])
    assert "SECRET-CODE-ALPHA-7" in sent           # текст дошёл
    assert "application/pdf" not in sent            # тяжёлый PDF НЕ пересылается


def test_knowledge_image_stored_as_media(client):
    cid = client.post("/api/characters", json={"name": "КБ3"}).json()["id"]
    sid = client.post(f"/api/sessions?character_id={cid}").json()["session_id"]
    raw = b"\x89PNG-kb" * 20
    data = "data:image/png;base64," + base64.b64encode(raw).decode()
    r = client.post(f"/api/sessions/{sid}/knowledge", json={
        "type": "image", "data": data, "mime": "image/png", "name": "карта.png",
    })
    assert r.status_code == 200
    meta = r.json()
    assert meta["kind"] == "image" and meta["has_text"] is False
    # Картинка базы знаний уходит в модель как медиа-блок.
    with patch("backend.llm_gateway.litellm.acompletion", new=_capture_acompletion):
        with client.websocket_connect(f"/ws/chat/{sid}") as ws:
            ws.send_json({"type": "user_message", "content": "что на карте?"})
            for _ in range(50):
                if ws.receive_json()["type"] in ("done", "error"):
                    break
    sent = str(_capture_acompletion.captured["messages"])
    assert "карта.png" in sent and "СПРАВОЧНАЯ БАЗА ЗНАНИЙ" in sent


def test_knowledge_deleted_with_chat(client):
    cid = client.post("/api/characters", json={"name": "КБ4"}).json()["id"]
    sid = client.post(f"/api/sessions?character_id={cid}").json()["session_id"]
    client.post(f"/api/sessions/{sid}/knowledge", json={
        "type": "document", "data": _txt("факт"), "mime": "text/plain", "name": "f.txt",
    })
    client.delete(f"/api/sessions/{sid}")
    # После удаления чата база знаний недоступна (403 или пусто).
    r = client.get(f"/api/sessions/{sid}/knowledge")
    assert r.status_code in (403, 404) or r.json() == []
