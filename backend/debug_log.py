"""
Лёгкий отладочный лог последних обращений к LLM (кольцевой буфер в памяти).

Виден в интерфейсе (кнопка 🐞) и помогает следить, ЧТО ушло в прокси (модель,
адрес, краткая сводка сообщений и параметры) и что вернулось (превью ответа или
текст ошибки). Содержимое сообщений не храним целиком — только размеры/типы,
чтобы не раздувать память и не светить весь контекст.
"""
import time
from collections import deque

_entries: deque = deque(maxlen=100)


def summarize_messages(messages: list[dict]) -> list[dict]:
    """Короткая сводка по сообщениям: роль + что внутри (без полного текста)."""
    out = []
    for m in messages:
        content = m.get("content")
        if isinstance(content, list):
            parts = []
            for b in content:
                t = b.get("type")
                if t == "text":
                    parts.append(f"text:{len(b.get('text', ''))}")
                elif t == "image_url":
                    # Различаем видео/pdf/картинку — иначе непонятно, что реально
                    # уходит в Gemini (частая причина «слабого анализа видео»).
                    iu = b.get("image_url") or {}
                    fmt = (iu.get("format") or "").lower()
                    url = (iu.get("url") or "")[:40].lower()
                    if "video" in fmt or url.startswith("data:video"):
                        parts.append("🎬 video")
                    elif "pdf" in fmt or "pdf" in url:
                        parts.append("📄 pdf")
                    else:
                        parts.append("🖼 image")
                elif t == "input_audio":
                    fmt = (b.get("input_audio") or {}).get("format") or ""
                    parts.append(f"🎤 audio/{fmt}" if fmt else "🎤 audio")
                else:
                    parts.append(t or "?")
            desc = " + ".join(parts)
        else:
            desc = f"{len(str(content))} симв."
        out.append({"role": m.get("role"), "content": desc})
    return out


def log_request(kind: str, model: str, api_base, detail: dict) -> dict:
    """Создаёт запись лога (status='...'), которую потом закрывают через finish()."""
    entry = {
        "ts": time.strftime("%H:%M:%S"),
        "kind": kind,  # chat | image
        "model": model,
        "api_base": api_base or "",
        "status": "...",  # ... | ok | error
        **detail,
    }
    _entries.appendleft(entry)  # новые сверху
    return entry


def finish(entry: dict, status: str, error: str = "", preview: str = "") -> None:
    entry["status"] = status
    if error:
        entry["error"] = error
    if preview:
        entry["preview"] = preview


def entries() -> list:
    return list(_entries)


def clear() -> None:
    _entries.clear()
