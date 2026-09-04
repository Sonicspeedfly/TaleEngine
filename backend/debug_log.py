"""
Лёгкий отладочный лог последних обращений к LLM (кольцевой буфер в памяти).

Виден в интерфейсе (кнопка 🐞) и помогает следить, ЧТО ушло в прокси (модель,
адрес, краткая сводка сообщений и параметры) и что вернулось (превью ответа или
текст ошибки). Содержимое сообщений не храним целиком — только размеры/типы,
чтобы не раздувать память и не светить весь контекст.
"""
import time
from collections import deque
from contextvars import ContextVar

_entries: deque = deque(maxlen=400)

# Владелец текущего хода. Заполняется там, где ход НАЧИНАЕТСЯ (веб-сокет, REST),
# и читается здесь, в момент записи.
#
# Почему контекстная переменная, а не параметр: log_request зовут из llm_gateway,
# который про пользователя ничего не знает и знать не должен. Протаскивать
# owner_id через stream_completion, complete и три функции картинок значило бы
# менять их сигнатуры ради служебного поля. ContextVar копируется в задачу при
# asyncio.create_task, поэтому фоновая генерация наследует владельца сама.
_owner: ContextVar = ContextVar("debug_owner", default=None)


def set_owner(user_id) -> None:
    """Пометить текущий контекст владельцем. None — режим без аккаунтов."""
    _owner.set(user_id)


def current_owner():
    return _owner.get()


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
        # Владелец хода. Наружу это поле не отдаётся (см. _public), оно нужно
        # только для фильтрации: сводка сообщений — это чужая переписка.
        "owner_id": _owner.get(),
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


def _public(entry: dict) -> dict:
    """Запись без служебных полей: владелец наружу не уходит."""
    return {k: v for k, v in entry.items() if k != "owner_id"}


def entries(owner_id, include_all: bool = False) -> list:
    """
    Записи ОДНОГО владельца.

    Раньше буфер был общим, а эндпоинт не спрашивал, кто пришёл: в режиме
    аккаунтов любой залогиненный видел сводку чужих сообщений, то есть чужую
    переписку. Право на include_all проверяет ВЫЗЫВАЮЩИЙ (эндпоинт знает роль),
    здесь мы только исполняем: модуль лога не должен решать вопросы доступа.
    """
    if include_all:
        return [_public(e) for e in _entries]
    return [_public(e) for e in _entries if e.get("owner_id") == owner_id]


def clear(owner_id, include_all: bool = False) -> int:
    """Очистить свои записи (или все, если разрешено). Возвращает число удалённых."""
    global _entries
    if include_all:
        n = len(_entries)
        _entries.clear()
        return n
    keep = [e for e in _entries if e.get("owner_id") != owner_id]
    n = len(_entries) - len(keep)
    _entries.clear()
    _entries.extend(keep)   # deque сохраняет maxlen, порядок «новые сверху» цел
    return n
