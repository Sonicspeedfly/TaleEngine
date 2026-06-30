"""
Импорт карточек персонажей в форматах SillyTavern:
  * JSON-карточка (Tavern Card V1/V2);
  * PNG с зашитой в tEXt-чанк 'chara' base64-строкой того же JSON.

Парсер PNG написан вручную (через struct), чтобы не тянуть тяжёлую Pillow ради
чтения одного текстового чанка.
"""
import base64
import json
import struct

from backend.schemas import CharacterCreate


def parse_character_json(raw: dict) -> CharacterCreate:
    """Нормализует JSON карточки (V1 или V2) в нашу схему CharacterCreate."""
    # В Tavern Card V2 полезные данные лежат в ключе 'data', в V1 — в корне.
    data = raw.get("data", raw)
    return CharacterCreate(
        name=data.get("name", "Unnamed"),
        description=data.get("description", ""),
        personality=data.get("personality", ""),
        scenario=data.get("scenario", ""),
        first_message=data.get("first_mes", ""),
        system_prompt=data.get("system_prompt", ""),
    )


def _read_png_text_chunks(png_bytes: bytes) -> dict[str, str]:
    """Достаёт все tEXt-чанки из PNG в виде словаря {keyword: value}."""
    chunks: dict[str, str] = {}
    # Структура PNG: 8-байтная сигнатура, далее чанки вида
    # [длина данных (4) | тип (4) | данные | CRC (4)].
    offset = 8
    while offset < len(png_bytes):
        length = struct.unpack(">I", png_bytes[offset : offset + 4])[0]
        ctype = png_bytes[offset + 4 : offset + 8].decode("latin-1")
        data = png_bytes[offset + 8 : offset + 8 + length]
        if ctype == "tEXt":
            # tEXt: keyword \x00 text
            key, _, value = data.partition(b"\x00")
            chunks[key.decode("latin-1")] = value.decode("latin-1")
        offset += 12 + length  # 4 (len) + 4 (type) + length + 4 (crc)
        if ctype == "IEND":
            break
    return chunks


def decode_png_card(png_bytes: bytes) -> dict:
    """Возвращает СЫРОЙ JSON карточки из PNG (чанк 'chara' с base64-JSON)."""
    chunks = _read_png_text_chunks(png_bytes)
    if "chara" not in chunks:
        raise ValueError("В PNG нет данных персонажа (отсутствует чанк 'chara')")
    return json.loads(base64.b64decode(chunks["chara"]).decode("utf-8"))


def parse_character_png(png_bytes: bytes) -> CharacterCreate:
    """Извлекает карточку из PNG."""
    return parse_character_json(decode_png_card(png_bytes))


def extract_horae_entries(raw: dict) -> list[dict]:
    """
    Извлекает лорбук / World Info из карточки SillyTavern (data.character_book.entries)
    и нормализует в записи Horae (детали памяти учитываются при импорте).
    """
    data = raw.get("data", raw)
    book = data.get("character_book") or {}
    out: list[dict] = []
    for e in book.get("entries") or []:
        if not isinstance(e, dict):
            continue
        content = (e.get("content") or "").strip()
        if not content:
            continue
        keys = e.get("keys") or e.get("key") or []
        if isinstance(keys, str):
            keys = [keys]
        try:
            priority = int(e.get("insertion_order", e.get("priority", 0)) or 0)
        except (ValueError, TypeError):
            priority = 0
        out.append({
            "title": (e.get("comment") or e.get("name") or "")[:300],
            "content": content,
            "keywords": [str(k) for k in keys if str(k).strip()],
            "always_on": bool(e.get("constant", False)),
            "enabled": bool(e.get("enabled", True)),
            "priority": priority,
            "category": "lore",
        })
    return out


def build_character_book(entries: list[dict]) -> dict:
    """Обратное: записи Horae -> character_book для экспорта в формат SillyTavern."""
    book_entries = []
    for i, e in enumerate(entries):
        book_entries.append({
            "keys": e.get("keywords") or [],
            "content": e.get("content") or "",
            "enabled": bool(e.get("enabled", True)),
            "constant": bool(e.get("always_on", False)),
            "insertion_order": int(e.get("priority", 0) or 0),
            "comment": e.get("title") or "",
            "name": e.get("title") or "",
            "selective": bool(e.get("keywords")),
            "extensions": {},
            "id": i,
        })
    return {"entries": book_entries, "name": "AiChat SSF lorebook"}
