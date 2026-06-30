"""
Тесты Канваса: CRUD + дедуп по сообщению, доработка нейросетью (мок) и экспорт
в Docx/PDF.
"""
from unittest.mock import patch


def _fake_chunk(text: str):
    class _Delta:
        def __init__(self, c): self.content = c

    class _Choice:
        def __init__(self, c): self.delta = _Delta(c)

    class _Chunk:
        def __init__(self, c): self.choices = [_Choice(c)]

    return _Chunk(text)


async def _fake_acompletion(*args, **kwargs):
    async def gen():
        for t in ["Готово", ": новый текст"]:
            yield _fake_chunk(t)

    return gen()


def _session(client) -> int:
    cid = client.post("/api/characters", json={"name": "КанвасПерс"}).json()["id"]
    return client.post(f"/api/sessions?character_id={cid}").json()["session_id"]


def test_canvas_crud_and_dedup(client):
    sid = _session(client)
    cv = client.post(
        "/api/canvas",
        json={"session_id": sid, "source_message_id": 1, "title": "Док", "kind": "document", "content": "# Док"},
    ).json()
    assert cv["title"] == "Док" and cv["kind"] == "document"

    # Повторное создание для того же сообщения возвращает тот же канвас (без дублей).
    cv2 = client.post(
        "/api/canvas", json={"session_id": sid, "source_message_id": 1, "content": "x"}
    ).json()
    assert cv2["id"] == cv["id"]

    # Ручное редактирование.
    client.patch(f"/api/canvas/{cv['id']}", json={"content": "# Обновлено"})
    lst = client.get(f"/api/canvas?session_id={sid}").json()
    assert len(lst) == 1 and lst[0]["content"] == "# Обновлено"

    # Удаление.
    assert client.delete(f"/api/canvas/{cv['id']}").status_code == 200
    assert client.get(f"/api/canvas?session_id={sid}").json() == []


def test_canvas_revise_uses_llm(client):
    sid = _session(client)
    cv = client.post(
        "/api/canvas", json={"session_id": sid, "title": "Док", "content": "старый текст"}
    ).json()
    with patch("backend.llm_gateway.litellm.acompletion", new=_fake_acompletion):
        r = client.post(f"/api/canvas/{cv['id']}/revise", json={"instruction": "перепиши"})
    assert r.status_code == 200
    assert r.json()["content"] == "Готово: новый текст"
    # Пустая инструкция — 400.
    assert client.post(f"/api/canvas/{cv['id']}/revise", json={"instruction": ""}).status_code == 400


def test_canvas_revise_selection_only(client):
    """Правка выделенного фрагмента меняет ТОЛЬКО его, остальное не трогает."""
    sid = _session(client)
    content = "начало СЕРЕДИНА конец"
    cv = client.post("/api/canvas", json={"session_id": sid, "title": "Док", "content": content}).json()
    start = content.index("СЕРЕДИНА")
    end = start + len("СЕРЕДИНА")
    with patch("backend.llm_gateway.litellm.acompletion", new=_fake_acompletion):
        r = client.post(
            f"/api/canvas/{cv['id']}/revise",
            json={"instruction": "перепиши", "selection_start": start, "selection_end": end},
        )
    assert r.status_code == 200
    assert r.json()["content"] == "начало Готово: новый текст конец"
    assert r.json()["can_undo"] is True


def test_canvas_undo_restores_previous(client):
    sid = _session(client)
    cv = client.post("/api/canvas", json={"session_id": sid, "title": "Док", "content": "версия один"}).json()
    with patch("backend.llm_gateway.litellm.acompletion", new=_fake_acompletion):
        client.post(f"/api/canvas/{cv['id']}/revise", json={"instruction": "перепиши"})
    # После доработки содержимое изменилось и доступен откат.
    cur = client.get(f"/api/canvas?session_id={sid}").json()[0]
    assert cur["content"] == "Готово: новый текст" and cur["can_undo"] is True
    # Откат возвращает прежнюю версию.
    undone = client.post(f"/api/canvas/{cv['id']}/undo").json()
    assert undone["content"] == "версия один" and undone["can_undo"] is False


def test_canvas_generate_creates_card_not_text(client):
    """Генерация документа: запрос в чат, ответ -> Канвас + «плашка» (canvas_id)."""
    sid = _session(client)
    with patch("backend.llm_gateway.litellm.acompletion", new=_fake_acompletion):
        r = client.post(f"/api/sessions/{sid}/canvas_generate", json={"prompt": "Напиши статью про лес"})
    assert r.status_code == 200
    body = r.json()
    assert body["canvas_id"] and body["message_id"]

    msgs = client.get(f"/api/sessions/{sid}/messages").json()
    # Сообщение пользователя ушло в чат как обычно.
    assert any(m["role"] == "user" and m["content"] == "Напиши статью про лес" for m in msgs)
    # Ответ — это плашка (assistant с canvas_id), а НЕ полотно текста.
    cards = [m for m in msgs if m.get("canvas_id")]
    assert len(cards) == 1 and cards[0]["canvas_id"] == body["canvas_id"]
    # Сгенерированный текст лежит в Канвасе (плашка в чате несёт только заголовок).
    cv = client.get(f"/api/canvas/{body['canvas_id']}").json()
    assert cv["content"] == "Готово: новый текст"
    assert cv["source_message_id"] == body["message_id"]


def test_canvas_edit_mutates_open_canvas_no_new_card(client):
    """Правка открытого канваса ПАТЧИТ его (без новой плашки и без второго канваса)."""
    sid = _session(client)
    cv = client.post(
        "/api/canvas", json={"session_id": sid, "title": "Статья", "kind": "document", "content": "старый текст"}
    ).json()
    with patch("backend.llm_gateway.litellm.acompletion", new=_fake_acompletion):
        r = client.post(
            f"/api/sessions/{sid}/canvas_edit",
            json={"canvas_id": cv["id"], "prompt": "сделай текст длиннее"},
        )
    assert r.status_code == 200
    patched = r.json()["canvas"]
    # Тот же канвас, новое содержимое, доступен откат.
    assert patched["id"] == cv["id"]
    assert patched["content"] == "Готово: новый текст"
    assert patched["can_undo"] is True

    # Второй канвас НЕ создан — в сессии по-прежнему один.
    assert len(client.get(f"/api/canvas?session_id={sid}").json()) == 1

    msgs = client.get(f"/api/sessions/{sid}/messages").json()
    # Запрос пользователя ушёл в чат; ответ — подтверждение, БЕЗ canvas_id (не плашка).
    assert any(m["role"] == "user" and m["content"] == "сделай текст длиннее" for m in msgs)
    assert any(m["role"] == "assistant" and "Обновил" in m["content"] for m in msgs)
    assert not any(m.get("canvas_id") for m in msgs)

    # Пустой запрос — 400; чужой session_id — 404.
    assert client.post(
        f"/api/sessions/{sid}/canvas_edit", json={"canvas_id": cv["id"], "prompt": ""}
    ).status_code == 400
    assert client.post(
        f"/api/sessions/{sid}/canvas_edit", json={"canvas_id": 999999, "prompt": "x"}
    ).status_code == 404


def test_canvas_export_docx_and_pdf(client):
    sid = _session(client)
    cv = client.post(
        "/api/canvas", json={"session_id": sid, "title": "Отчёт", "content": "# Отчёт\n\nТекст **жирный**."}
    ).json()
    dx = client.get(f"/api/canvas/{cv['id']}/export?fmt=docx")
    assert dx.status_code == 200 and dx.content[:2] == b"PK"
    # Кириллическое имя файла — через RFC 5987 (filename*).
    assert "filename*" in dx.headers.get("content-disposition", "")
    pf = client.get(f"/api/canvas/{cv['id']}/export?fmt=pdf")
    assert pf.status_code == 200 and pf.content[:4] == b"%PDF"
