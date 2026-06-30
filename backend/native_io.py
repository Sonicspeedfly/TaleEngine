"""
Внутренний (нативный) формат AiChat SSF для импорта/экспорта чатов.

Зачем свой формат: экспорт SillyTavern слишком специфичен (его inline-теги Horae,
служебные сообщения, нюансы свайпов) — под него трудно подстроиться без потерь.
Нативный формат сохраняет ВСЁ, что есть в приложении, и даёт точный круговой
импорт↔экспорт: персонаж, персона, сцена, заметка автора, фон, сообщения (со
свайпами, автором реплики, ответами-на-сообщение, вложениями) и память Horae
(и сессии, и лорбук персонажа).

Файл — обычный JSON с маркером `"format": "aichat.chat"`. По нему импорт сам
отличает нативный формат от SillyTavern.

Здесь — ЧИСТАЯ (без БД) сборка экспортного словаря и проверка формата; создание
строк в БД при импорте делает эндпоинт в main.py (`_import_native_chat`).
"""
from datetime import datetime, timezone

CHAT_FORMAT = "aichat.chat"
CHAT_VERSION = 1


def character_to_dict(ch) -> dict:
    """Полные данные персонажа — чтобы экспорт был самодостаточным."""
    if ch is None:
        return {}
    return {
        "name": ch.name,
        "description": ch.description or "",
        "personality": ch.personality or "",
        "scenario": ch.scenario or "",
        "first_message": ch.first_message or "",
        "system_prompt": ch.system_prompt or "",
        "avatar_path": ch.avatar_path,
        "generation_params": ch.generation_params or {},
        "model": ch.model,
    }


def _persona_to_dict(p) -> dict | None:
    if p is None:
        return None
    return {
        "name": p.name,
        "description": p.description or "",
        "avatar_path": p.avatar_path,
    }


def _horae_to_dict(e, character_id) -> dict:
    # scope: 'character' — лорбук персонажа; иначе 'session' — память этого чата.
    scope = "character" if e.character_id and e.character_id == character_id else "session"
    return {
        "scope": scope,
        "category": e.category,
        "title": e.title,
        "content": e.content,
        "keywords": e.keywords or [],
        "always_on": e.always_on,
        "enabled": e.enabled,
        "priority": e.priority,
    }


def build_chat_export(session, character, persona, messages, horae_entries, members) -> dict:
    """
    Собрать нативный экспорт чата из ORM-объектов в сериализуемый словарь.

    Ссылки «ответ на сообщение» переводим из id в ИНДЕКС внутри списка messages
    (idx), чтобы файл не зависел от id в БД и корректно переносился между базами.
    """
    id_to_idx = {m.id: i for i, m in enumerate(messages)}
    char_id = character.id if character is not None else None

    msg_list = []
    for i, m in enumerate(messages):
        msg_list.append({
            "idx": i,
            "role": m.role,
            "content": m.content or "",
            "swipes": m.swipes or [],
            "active_swipe": m.active_swipe or 0,
            "speaker_name": m.speaker_name,
            "reply_to_idx": id_to_idx.get(m.reply_to_id) if m.reply_to_id else None,
            "attachments": m.attachments or [],
            "model_used": m.model_used,
        })

    return {
        "format": CHAT_FORMAT,
        "version": CHAT_VERSION,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "character": character_to_dict(character),
        "persona": _persona_to_dict(persona),
        "session": {
            "title": session.title,
            "scenario": session.scenario or "",
            "author_note": session.author_note or "",
            "background": session.background or "",
            "is_group": session.is_group,
            "director": session.director,
        },
        "group_members": [character_to_dict(c) for c in (members or [])],
        "messages": msg_list,
        "horae": [_horae_to_dict(e, char_id) for e in horae_entries],
    }


def is_native_chat(data) -> bool:
    """Это нативный экспорт AiChat? (по маркеру format)."""
    return isinstance(data, dict) and data.get("format") == CHAT_FORMAT
