"""
Логика групповых чатов (несколько персонажей в одном чате).

Гибридная очередь ответов (выбор пользователя):
  * если в реплике УПОМЯНУТО имя персонажа — отвечает он;
  * иначе, если включён «режиссёр» — модель решает, кто уместно ответит;
  * иначе — по кругу (round-robin) следующий после последнего говорившего.

Контекст для каждого персонажа собирается так, чтобы он отвечал ТОЛЬКО за себя,
видя реплики остальных как обычный диалог с подписями имён.
"""
from sqlalchemy import select

from backend.horae_memory import (
    _load_horae_records,
    _load_persona_and_note,
    _render_character_block,
    _render_horae_block,
    _render_persona_block,
    _scan_text_for_triggers,
)
from backend.llm_gateway import complete
from backend.models import Character, GroupMember, Message


async def load_members(db, session_id: int) -> list:
    """Список персонажей группового чата."""
    rows = (
        await db.execute(
            select(Character)
            .join(GroupMember, GroupMember.character_id == Character.id)
            .where(GroupMember.session_id == session_id)
        )
    ).scalars().all()
    return rows


def mentioned_responders(user_text: str, members: list) -> list:
    """Персонажи, чьё имя встретилось в реплике пользователя."""
    low = (user_text or "").lower()
    return [m for m in members if m.name and m.name.lower() in low]


def round_robin_next(members: list, last_speaker_name: str | None) -> list:
    """Следующий персонаж по кругу после последнего говорившего."""
    if not members:
        return []
    names = [m.name for m in members]
    if last_speaker_name in names:
        idx = (names.index(last_speaker_name) + 1) % len(members)
        return [members[idx]]
    return [members[0]]


async def director_pick(members: list, transcript: str, connection: dict) -> list:
    """ИИ-режиссёр решает, кто ответит следующим (1-2 персонажа или никто)."""
    names = [m.name for m in members]
    system = (
        "Ты — режиссёр ролевой сцены. По диалогу реши, КТО из персонажей логично "
        "ответит следующим (можно 1-2). Верни ТОЛЬКО имена через запятую строго из "
        "списка: " + ", ".join(names) + ". Если сейчас никто не должен отвечать, верни 'никто'."
    )
    out = (await complete(
        [{"role": "system", "content": system}, {"role": "user", "content": transcript}],
        None,
        connection,
    )).strip().lower()
    if "никто" in out or "none" in out:
        return []
    picked = [m for m in members if m.name.lower() in out]
    return picked[:2] or [members[0]]


async def build_group_messages(
    db, session, target_character, token_budget: int, send_avatars: bool = False
) -> list[dict]:
    """Собирает messages, чтобы target_character ответил как он сам, видя весь диалог."""
    members = await load_members(db, session.id)
    member_names = [c.name for c in members] or [target_character.name]

    msgs = (
        await db.execute(
            select(Message).where(Message.session_id == session.id).order_by(Message.id)
        )
    ).scalars().all()

    records = await _load_horae_records(db, session.id, target_character.id)
    persona, author_note = await _load_persona_and_note(db, session)
    persona_name = persona["name"] if persona and persona.get("name") else "Пользователь"

    recent_text = " ".join(m.content for m in msgs[-6:] if m.content)
    activated = _scan_text_for_triggers(recent_text, records)

    char_block = _render_character_block(
        {
            "name": target_character.name,
            "description": target_character.description,
            "personality": target_character.personality,
            "scenario": target_character.scenario,
            "system_prompt": target_character.system_prompt,
        }
    )
    others = [n for n in member_names if n != target_character.name]
    group_instr = (
        "[Групповой чат] Участники: " + ", ".join(member_names) + ". Ты — "
        + target_character.name + ". Отвечай ТОЛЬКО как " + target_character.name
        + ", одной репликой и в характере. Не пиши реплики за других персонажей ("
        + ", ".join(others) + ")."
    )
    # Общая «сцена» группы (сеттинг ролевой) — влияет на всех участников.
    scene = (session.scenario or "").strip()
    scene_block = f"[Сцена] {scene}" if scene else ""
    system = "\n\n".join(
        p for p in [char_block, _render_persona_block(persona), scene_block, group_instr, _render_horae_block(activated)] if p
    )

    lines = []
    for m in msgs:
        if m.role == "user":
            lines.append(f"{persona_name}: {m.content}")
        else:
            lines.append(f"{m.speaker_name or target_character.name}: {m.content}")
    transcript = "\n".join(lines)

    messages: list[dict] = [{"role": "system", "content": system}]
    if send_avatars:
        from backend.horae_memory import _avatar_messages
        messages.extend(
            _avatar_messages(
                {"name": target_character.name}, target_character.avatar_path, (persona or {}).get("avatar")
            )
        )
    if author_note and author_note.strip():
        messages.append({"role": "system", "content": f"[Author's Note]\n{author_note.strip()}"})
    messages.append({"role": "user", "content": transcript + f"\n\n{target_character.name}:"})
    return messages
