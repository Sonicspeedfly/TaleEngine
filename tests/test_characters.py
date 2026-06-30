"""
Тесты обработки файлов — импорт карточек персонажей (PNG / JSON формата SillyTavern).
Покрывает требование ТЗ «тесты должны проверять обработку файлов».
"""
import base64
import json
import struct

from backend.characters import parse_character_json, parse_character_png

PNG_SIGNATURE = bytes([137, 80, 78, 71, 13, 10, 26, 10])


def _make_png_with_chara(card: dict) -> bytes:
    """Собирает минимальный валидный для нашего парсера PNG с чанком 'chara'."""
    chara_b64 = base64.b64encode(json.dumps(card).encode()).decode()
    text_data = b"chara\x00" + chara_b64.encode("latin-1")

    def chunk(ctype: bytes, data: bytes) -> bytes:
        # [длина(4) | тип(4) | данные | CRC(4)]. CRC наш парсер не проверяет -> нули.
        return struct.pack(">I", len(data)) + ctype + data + b"\x00\x00\x00\x00"

    return PNG_SIGNATURE + chunk(b"tEXt", text_data) + chunk(b"IEND", b"")


def test_parse_character_json_v2():
    raw = {
        "spec": "chara_card_v2",
        "data": {
            "name": "Lyra",
            "description": "Странствующий бард.",
            "personality": "Весёлая.",
            "first_mes": "О, новый слушатель!",
        },
    }
    card = parse_character_json(raw)
    assert card.name == "Lyra"
    assert card.description == "Странствующий бард."
    assert card.first_message == "О, новый слушатель!"


def test_parse_character_json_v1_root_level():
    raw = {"name": "Old", "description": "Карточка старого формата."}
    card = parse_character_json(raw)
    assert card.name == "Old"
    assert card.description == "Карточка старого формата."


def test_parse_character_png_roundtrip():
    png = _make_png_with_chara(
        {"name": "Aria", "description": "Хранительница библиотеки."}
    )
    card = parse_character_png(png)
    assert card.name == "Aria"
    assert card.description == "Хранительница библиотеки."


def test_extract_horae_from_character_book():
    from backend.characters import extract_horae_entries

    raw = {"data": {"name": "X", "character_book": {"entries": [
        {"keys": ["дракон"], "content": "Дракон спит в горах", "enabled": True,
         "constant": False, "insertion_order": 5, "comment": "Дракон"},
        {"keys": [], "content": "", "enabled": True},          # пустой -> пропуск
        {"keys": "король", "content": "Король мудр", "constant": True},
    ]}}}
    out = extract_horae_entries(raw)
    assert len(out) == 2
    assert out[0]["keywords"] == ["дракон"] and out[0]["priority"] == 5
    assert out[1]["always_on"] is True and out[1]["keywords"] == ["король"]


def test_import_character_creates_horae_and_export(client):
    import json

    card = {"spec": "chara_card_v2", "data": {
        "name": "Lore", "character_book": {"entries": [
            {"keys": ["меч"], "content": "Древний меч", "constant": False}
        ]}}}
    r = client.post(
        "/api/characters/import",
        files={"file": ("c.json", json.dumps(card), "application/json")},
    )
    cid = r.json()["id"]
    horae = client.get("/api/horae").json()
    assert any(
        h.get("character_id") == cid and "меч" in (h.get("keywords") or []) for h in horae
    )
    exp = client.get(f"/api/characters/{cid}/export").json()
    assert exp["data"]["name"] == "Lore"
    assert any("Древний меч" in e["content"] for e in exp["data"]["character_book"]["entries"])
