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
    """
    Список персонажей группового чата — БЕЗ дублей и в стабильном порядке добавления
    (по GroupMember.id). Дедуп на чтении лечит уже испорченные данные (повторные
    строки group_members), чтобы участники не двоились в шапке и в очереди ответов.
    """
    rows = (
        await db.execute(
            select(Character)
            .join(GroupMember, GroupMember.character_id == Character.id)
            .where(GroupMember.session_id == session_id)
            .order_by(GroupMember.id)
        )
    ).scalars().all()
    seen: set[int] = set()
    members: list = []
    for c in rows:
        if c.id not in seen:
            seen.add(c.id)
            members.append(c)
    return members


async def dedupe_members(db, session_id: int) -> int:
    """
    Самолечение данных: удалить повторяющиеся строки group_members (оставить по одной
    на персонажа, самую раннюю). Возвращает число удалённых. Вызывается при показе
    списка групп, поэтому испорченные группы чинятся при первом открытии приложения.
    """
    rows = (
        await db.execute(
            select(GroupMember)
            .where(GroupMember.session_id == session_id)
            .order_by(GroupMember.id)
        )
    ).scalars().all()
    seen: set[int] = set()
    removed = 0
    for gm in rows:
        if gm.character_id in seen:
            await db.delete(gm)
            removed += 1
        else:
            seen.add(gm.character_id)
    if removed:
        await db.commit()
    return removed


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


def _match_names(members: list, text: str) -> list:
    """Найти персонажей, чьи имена встретились в тексте (ответе режиссёра).

    Длинные имена проверяем раньше, чтобы «Bot редактор» не перекрывался «Bot»;
    результат упорядочиваем по позиции имени в тексте (кого режиссёр назвал первым).
    """
    low = (text or "").lower()
    picked: list = []
    for m in sorted(members, key=lambda x: -len(x.name or "")):
        name = (m.name or "").lower()
        if name and name in low and m not in picked:
            picked.append(m)
    picked.sort(key=lambda m: low.find((m.name or "").lower()))
    return picked


async def director_pick(
    members: list, transcript: str, connection: dict, params=None, last_user: str = ""
) -> list:
    """
    ИИ-режиссёр решает, кто ответит следующим (1-2 персонажа). Возвращает [] если
    выбрать не удалось (модель промолчала/ошиблась/заблокирована) — тогда вызывающий
    делает round-robin, чтобы КТО-ТО всегда ответил (без этого чат «зависал»).

    Важно: раньше был `complete(..., None, ...)` без params — служебный вызов шёл
    БЕЗ снятия фильтров, и на «остром» контексте Gemini возвращал пустоту, а режиссёр
    молча выбирал первого. Теперь передаём params (фильтры сняты) + низкую температуру.
    """
    if not members:
        return []
    numbered = "\n".join(f"{i + 1}. {m.name}" for i, m in enumerate(members))
    system = (
        "Ты — РЕЖИССЁР групповой ролевой сцены. Реши, кто из персонажей заговорит "
        "СЛЕДУЮЩИМ, чтобы сцена шла живо и естественно.\n"
        "Персонажи:\n" + numbered + "\n\n"
        "Правила:\n"
        "— Отвечает тот, к кому обратились/кого назвали, или кому логичнее реагировать "
        "на последнюю реплику.\n"
        "— Обычно ОДИН персонаж; двоих (через запятую) — только если реплика явно к обоим.\n"
        "— Не выбирай того, кто только что говорил, если в этом нет смысла.\n"
        "— Ответь ТОЛЬКО именем персонажа из списка. Без пояснений, кавычек и лишних слов."
    )
    user = transcript
    if last_user:
        user += f"\n\n[Последняя реплика пользователя]: {last_user}\nКто ответит следующим?"
    # Низкая температура и умеренный лимит — решение должно быть коротким и стабильным.
    dparams = params.model_copy(update={"temperature": 0.2, "max_tokens": 256}) if params else None
    try:
        out = await complete(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            dparams,
            connection,
        )
    except Exception:  # noqa: BLE001 — пустой/ошибочный ответ режиссёра не должен ронять ход
        return []
    if "никто" in out.lower() or "none" in out.lower():
        return []
    return _match_names(members, out)[:2]


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
