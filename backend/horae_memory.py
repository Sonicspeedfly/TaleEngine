"""
Подсистема памяти Horae — «State & Context Manager».

Назначение: перед КАЖДЫМ запросом к LLM собрать ИТОГОВЫЙ контекст:

    1. Системный промпт персонажа (кто он, его характер, сценарий, правила).
    2. Релевантные записи памяти Horae:
         * always_on  -> «снимки состояния»: инвентарь, скрытые характеристики
                         персонажей, текущее положение сюжета — подмешиваются ВСЕГДА;
         * по ключевым словам (стиль World Info) -> подмешиваются только если в
                         последних сообщениях встретилось ключевое слово.
    3. История диалога (обрезается под бюджет токенов — свежие сообщения важнее).
    4. Текущее сообщение пользователя (с мультимодальными вложениями, если есть).

Архитектурно модуль разделён на две части:
    * assemble_context()      — ЧИСТАЯ функция (без БД и сети). Принимает обычные
                                dict/list и возвращает готовый список messages для
                                LiteLLM. Её удобно и быстро покрывать юнит-тестами.
    * build_context_from_db() — тонкая обёртка: тянет данные из БД и зовёт чистую
                                функцию выше.
"""
from dataclasses import dataclass


def _is_image(src) -> bool:
    """Похоже ли значение аватара на картинку (data:image / http-URL)."""
    return isinstance(src, str) and (
        src.startswith("data:image") or src.startswith("http") or src.startswith("/")
    )


def _avatar_messages(character: dict, character_avatar, persona_avatar) -> list[dict]:
    """Сообщения с картинками-аватарами, чтобы нейросеть «видела» внешность."""
    msgs: list[dict] = []
    name = character.get("name", "персонаж")
    for label, av in [
        (f"Так выглядит {name} (твоя внешность)", character_avatar),
        ("Так выглядит собеседник (пользователь)", persona_avatar),
    ]:
        if _is_image(av):
            msgs.append(
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": f"[{label}]"},
                        {"type": "image_url", "image_url": {"url": av}},
                    ],
                }
            )
    return msgs


def estimate_tokens(text: str) -> int:
    """
    Грубая оценка количества токенов: ~4 символа на токен.
    Для точного подсчёта можно подключить tiktoken, но для бюджетирования контекста
    этой оценки достаточно, и она не тянет тяжёлых зависимостей.
    """
    return max(1, len(text) // 4)


def estimate_content_tokens(content) -> int:
    """
    Оценка токенов для контента, который может быть мультимодальным (список блоков).
    Для картинок/аудио НЕ считаем длину base64 как текст (это дало бы гигантскую
    оценку и выбросило всю историю) — берём грубую фиксированную стоимость блока.
    """
    if isinstance(content, list):
        total = 0
        for b in content:
            if not isinstance(b, dict):
                continue
            t = b.get("type")
            if t == "text":
                total += estimate_tokens(b.get("text", ""))
            elif t == "input_audio":
                total += 1500   # аудио заметно дороже картинки
            else:                # image_url / document / прочее
                total += 400
        return total
    return estimate_tokens(str(content or ""))


# Сколько байт base64-вложений из ИСТОРИИ разрешаем включить в один запрос. Сверх
# этого — вложение заменяется текстовой пометкой. Держим НЕБОЛЬШИМ: тяжёлое аудио
# (14 МБ → ~19 МБ base64) не должно гоняться в контексте КАЖДЫЙ ход — иначе запросы
# к Vertex раздуваются и подвисают. Картинки (сотни КБ) при этом спокойно остаются
# видимыми модели и дальше, а крупное аудио — только на своём ходу (потом пометка).
_MAX_HISTORY_ATT_BYTES = 5 * 1024 * 1024


def _att_label(a: dict) -> str:
    t = a.get("type")
    if t == "image":
        return "изображение"
    if t == "audio":
        return "аудио"
    if t == "video":
        return "видео: " + (a.get("name") or "файл")
    if t == "document":
        return "документ: " + (a.get("name") or "файл")
    return "вложение"


def messages_to_history(msgs, att_map: dict | None = None) -> list[dict]:
    """
    Превращает ORM-сообщения в историю для контекста, СОХРАНЯЯ вложения (картинки,
    аудио, документы) — чтобы модель «видела» присланный ранее файл и на последующих
    ходах (раньше вложения из истории терялись, и файл был виден только на своём ходу).

    Вложения включаем от свежих к старым, пока суммарный объём не превысит лимит; что
    не влезло — заменяем текстовой пометкой «[изображение]/[аудио]/…», чтобы модель хотя
    бы знала о факте вложения. Мультимодальный контент собираем только для реплик
    пользователя (у ассистента вложений в норме нет, а image в assistant часть
    провайдеров не принимает).

    :param att_map: {message_id: [att dict С data]} — вложения, уже отобранные под
        лимит и гидратированные из blob-таблицы (см. attachments.load_history_attachments).
        None — легаси-режим: данные берутся прямо из сообщений (инлайн base64).
    """
    from backend.llm_gateway import build_user_content
    from backend.schemas import AttachmentIn

    if att_map is None:
        # Легаси: инлайн-данные в самих сообщениях (старые БД, юнит-тесты).
        att_map = {}
        used = 0
        for m in reversed(msgs):
            atts = [a for a in (m.attachments or []) if isinstance(a, dict) and a.get("data")]
            size = sum(len(a.get("data") or "") for a in atts)
            if atts and used + size <= _MAX_HISTORY_ATT_BYTES:
                att_map[m.id] = atts
                used += size

    out: list[dict] = []
    for m in msgs:
        all_atts = [a for a in (m.attachments or []) if isinstance(a, dict)]
        kept = att_map.get(m.id)
        if kept and m.role == "user":
            try:
                content = build_user_content(m.content or "", [
                    AttachmentIn(
                        type=a.get("type") or "document", data=a.get("data") or "",
                        mime=a.get("mime"), name=a.get("name"),
                    )
                    for a in kept
                ])
            except Exception:  # noqa: BLE001 — битое вложение не должно рушить контекст
                content = m.content or ""
        elif all_atts:
            note = " ".join(f"[{_att_label(a)}]" for a in all_atts)
            content = f"{m.content} {note}".strip() if m.content else note
        else:
            content = m.content or ""
        out.append({"role": m.role, "content": content})
    return out


async def messages_to_history_db(db, msgs, files_limit_chars: int | None = None) -> list[dict]:
    """
    То же, что messages_to_history, но данные вложений подтягиваются из
    blob-таблицы ТОЧЕЧНО и только когда нужны.

    :param files_limit_chars: лимит файлов истории в символах base64;
        None — БЕЗ лимита (модель заново видит все прежние файлы, дефолт).
    """
    from backend.attachments import load_history_attachments

    att_map = await load_history_attachments(db, msgs, files_limit_chars)
    return messages_to_history(msgs, att_map)


@dataclass
class HoraeRecord:
    """
    Лёгкое представление записи памяти, НЕ зависящее от ORM.
    Именно поэтому ядро сборки контекста легко тестировать без базы данных.
    """
    category: str
    title: str
    content: str
    keywords: list[str]
    always_on: bool
    enabled: bool
    priority: int


def _scan_text_for_triggers(
    haystack: str, records: list[HoraeRecord]
) -> list[HoraeRecord]:
    """
    Возвращает записи, которые нужно активировать:
      * always_on (если enabled) — всегда;
      * keyword-записи — если хотя бы одно ключевое слово встретилось в тексте.
    Результат сортируется по priority (по убыванию): важное идёт первым.
    """
    haystack_low = haystack.lower()
    activated: list[HoraeRecord] = []

    for rec in records:
        if not rec.enabled:
            continue
        if rec.always_on:
            activated.append(rec)
            continue
        # Стиль World Info: ищем любое ключевое слово как подстроку (регистр игнорируем).
        if any(kw.strip().lower() in haystack_low for kw in rec.keywords if kw.strip()):
            activated.append(rec)

    activated.sort(key=lambda r: r.priority, reverse=True)
    return activated


def _render_character_block(character: dict) -> str:
    """Собирает «паспорт» персонажа в текстовый блок системного промпта."""
    parts: list[str] = []
    if character.get("system_prompt"):
        parts.append(character["system_prompt"].strip())
    if character.get("name"):
        parts.append(f"You are {character['name']}.")
    if character.get("description"):
        parts.append(f"Description: {character['description'].strip()}")
    if character.get("personality"):
        parts.append(f"Personality: {character['personality'].strip()}")
    if character.get("scenario"):
        parts.append(f"Scenario: {character['scenario'].strip()}")
    # Примеры реплик (mes_example) — образец «голоса»/стиля персонажа.
    if character.get("mes_example"):
        parts.append("[Example dialogue — match this voice and style]\n" + character["mes_example"].strip())
    return "\n\n".join(p for p in parts if p)


def _render_char_anchor(character: dict) -> str:
    """
    Компактный «якорь» характера для ПЕРЕинъекции в конец контекста. В длинном
    окне модель хуже помнит далёкий системный промпт (recency bias), поэтому прямо
    перед ответом напоминаем, кто она и как себя ведёт — так характер не «плывёт».
    """
    name = character.get("name") or "персонаж"
    bits = [f"Ты — {name}. Оставайся полностью в образе и отвечай от его лица."]
    pers = (character.get("personality") or "").strip()
    if pers:
        bits.append(f"Характер: {pers[:600]}")
    return "[Напоминание о роли] " + " ".join(bits)


def _render_horae_block(records: list[HoraeRecord]) -> str:
    """Складывает активированные записи памяти в единый блок для системного промпта."""
    if not records:
        return ""
    lines = ["[Memory & World State]"]
    for rec in records:
        header = rec.title or rec.category
        lines.append(f"- {header}: {rec.content.strip()}")
    return "\n".join(lines)


def _render_persona_block(persona: dict | None) -> str:
    """Описывает, кем отыгрывает пользователь (его персона)."""
    if not persona or not (persona.get("name") or persona.get("description")):
        return ""
    parts = ["[User Persona]"]
    if persona.get("name"):
        parts.append(f"The user is {persona['name']}.")
    if persona.get("description"):
        parts.append(persona["description"].strip())
    return " ".join(parts)


# Базовые правила поведения. Держим модель в образе, гоним прочь галлюцинации и
# заставляем реально СМОТРЕТЬ в приложенные файлы, а не выдумывать.
# ВАЖНО: конкретные мессенджеры здесь НЕ называем — если в системном промпте написано
# «Telegram», модель временами начинает считать, что общается именно там.
BEHAVIOR_GUIDE = (
    "[Как отвечать] Ты полностью вживаешься в свою роль и остаёшься в образе на "
    "протяжении всего диалога: сохраняй характер, манеру речи и мотивацию персонажа, "
    "не ломай роль и не добавляй мета-комментариев от «нейросети», если тебя об этом "
    "прямо не просят.\n"
    "[Работа с материалами] Если к сообщению приложены файлы (изображение, видео, "
    "аудио, документ) или пользователь ссылается на ранее присланный файл — сначала "
    "ВНИМАТЕЛЬНО изучи его содержимое и опирайся на факты из него. НЕ выдумывай того, "
    "чего в материале нет; если чего-то в файле не хватает или он нечитаем — честно "
    "скажи об этом, а не сочиняй.\n"
    "[Точность] Не придумывай факты, имена и события. Если не уверен — так и скажи "
    "или уточни у пользователя, вместо того чтобы фантазировать."
)

STYLE_GUIDE = (
    "[Оформление ответа] Пиши естественной прозой. Лёгкую разметку используй "
    "умеренно: *курсив* для действий и мыслей, **жирный** для акцентов, "
    "`моноширинный` и блоки кода в тройных кавычках для технического текста, "
    "«> » для цитат, «- » для списков. Не используй таблицы и HTML-разметку."
)


def _attachment_manifest(history: list[dict], current_content) -> str:
    """
    Манифест приложенных файлов: короткий список того, что физически есть в
    контексте (по типам). Модель видит, что «файлы реально приложены», и понимает,
    что к ним можно обращаться — а не отвечать «файла не вижу».
    """
    counts = {"image": 0, "video": 0, "audio": 0, "document": 0}
    def _scan(content):
        if not isinstance(content, list):
            return
        for b in content:
            if not isinstance(b, dict):
                continue
            t = b.get("type")
            if t == "image_url":
                url = ((b.get("image_url") or {}).get("url")) or ""
                if url.startswith("data:application/pdf") or "pdf" in url[:40]:
                    counts["document"] += 1
                elif url.startswith("data:video"):
                    counts["video"] += 1
                else:
                    counts["image"] += 1
            elif t == "input_audio":
                counts["audio"] += 1
    for m in history:
        _scan(m.get("content"))
    _scan(current_content)
    total = sum(counts.values())
    if not total:
        return ""
    ru = {"image": "изображений", "video": "видео", "audio": "аудио", "document": "документов"}
    parts = [f"{ru[k]}: {v}" for k, v in counts.items() if v]
    return (
        "[Приложенные материалы] В этом диалоге модели доступны файлы (" + ", ".join(parts)
        + "). Они реально приложены к сообщениям — изучай их и отвечай по их содержимому."
    )


def assemble_context(
    *,
    character: dict,
    horae_records: list[HoraeRecord],
    history: list[dict],
    user_message: str,
    user_attachments_content=None,
    persona: dict | None = None,
    author_note: str = "",
    token_budget: int = 8000,
    character_avatar=None,
    persona_avatar=None,
    send_avatars: bool = False,
    user_time: str = "",
    post_history_instructions: str = "",
    web_access: bool = False,
) -> list[dict]:
    """
    ЧИСТАЯ функция сборки контекста. Возвращает messages для LiteLLM:

        [{"role": "system",    "content": "..."},
         {"role": "user",      "content": "..."},
         {"role": "assistant", "content": "..."},
         ...]

    Аргументы намеренно простые (dict/list/str), чтобы покрывать юнит-тестами без
    поднятия БД и без обращения к сети.

    :param history: предыдущие сообщения БЕЗ текущего (его добавим последним сами).
    :param user_attachments_content: если у текущего сообщения есть картинки/аудио —
        сюда передаётся уже собранный мультимодальный контент (см. build_user_content).
    """
    # 1. Текст, по которому ищем триггеры памяти: текущее сообщение + хвост истории.
    recent_text = user_message + "\n" + "\n".join(
        m.get("content", "")
        for m in history[-4:]
        if isinstance(m.get("content"), str)
    )
    activated = _scan_text_for_triggers(recent_text, horae_records)
    # Авто-сводку сюжета (category=summary) вынимаем из общего блока — она пойдёт
    # ОТДЕЛЬНЫМ recency-блоком в конец, где влияет сильнее (иначе тонула в начале).
    summary_recs = [r for r in activated if r.category == "summary"]
    lore_recs = [r for r in activated if r.category != "summary"]

    # 2. Системный промпт = паспорт персонажа + персона + лор Horae + правила поведения.
    system_parts = [
        _render_character_block(character),
        _render_persona_block(persona),
        _render_horae_block(lore_recs),
        BEHAVIOR_GUIDE,
        STYLE_GUIDE,
    ]
    system_prompt = "\n\n".join(p for p in system_parts if p)

    # 3. «Несжимаемый» бюджет: системный промпт + текущее сообщение пользователя.
    used = estimate_tokens(system_prompt) + estimate_tokens(user_message)

    # 4. Добавляем историю с конца (свежие сообщения важнее), пока хватает бюджета.
    trimmed_history: list[dict] = []
    for msg in reversed(history):
        cost = estimate_content_tokens(msg.get("content"))  # учитывает мультимодальные блоки
        if used + cost > token_budget:
            break
        trimmed_history.insert(0, {"role": msg["role"], "content": msg["content"]})
        used += cost

    # 5. Финальная сборка messages.
    messages: list[dict] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    # Аватары: показываем нейросети внешность персонажа и пользователя (если включено).
    if send_avatars:
        messages.extend(_avatar_messages(character, character_avatar, persona_avatar))
    messages.extend(trimmed_history)

    # ===== Переинъекция в КОНЕЦ (сильнейшая позиция — recency bias) =====
    # Здесь всё, что должно «весить» на ответ несмотря на длину истории: сводка
    # сюжета, якорь характера, post-history инструкции, заметка автора, файлы.
    tail: list[dict] = []

    # Что было в диалоге раньше (авто-сводка Horae) — как отдельный свежий блок.
    if summary_recs:
        body = "\n".join(f"- {(r.title or 'Сводка')}: {r.content.strip()}" for r in summary_recs)
        tail.append({"role": "system", "content": "[Что было в истории — помни это]\n" + body})

    # Манифест приложенных файлов + напоминание изучать их.
    manifest = _attachment_manifest(trimmed_history, user_attachments_content)
    if manifest:
        tail.append({"role": "system", "content": manifest})

    # Напоминание об аватарах (внешности) — картинки приложены в начале, в длинном
    # контексте про них легко забыть, поэтому освежаем ссылку на них у конца.
    if send_avatars and (_is_image(character_avatar) or _is_image(persona_avatar)):
        who = []
        if _is_image(character_avatar):
            who.append("персонажа")
        if _is_image(persona_avatar):
            who.append("собеседника")
        tail.append({"role": "system", "content": (
            "[Внешность] Выше в диалоге приложены изображения-аватары " + " и ".join(who)
            + ". Учитывай эту внешность, когда описываешь их вид."
        )})

    # Веб-поиск включён — прямо просим искать факты, а не выдумывать.
    if web_access:
        tail.append({"role": "system", "content": (
            "[Доступ в интернет включён] Если для ответа нужны актуальные или точные "
            "факты, которых нет в контексте, — ВОСПОЛЬЗУЙСЯ веб-поиском и опирайся на "
            "найденное, а не придумывай."
        )})

    # Текущее время пользователя (часовой пояс — настройка чата).
    if user_time:
        tail.append({"role": "system", "content": f"[Время пользователя] Сейчас у пользователя {user_time}."})

    # Author's Note (заметка автора) — у самого конца.
    if author_note and author_note.strip():
        tail.append({"role": "system", "content": f"[Author's Note]\n{author_note.strip()}"})

    # Якорь характера — чтобы личность не «плыла» в длинном окне.
    tail.append({"role": "system", "content": _render_char_anchor(character)})

    # Post-History Instructions (jailbreak/UJB) — САМЫЙ конец: максимальное влияние.
    if post_history_instructions and post_history_instructions.strip():
        tail.append({"role": "system", "content": post_history_instructions.strip()})

    # Фокус на текущем ходе: в огромном контексте (вся история + все файлы) модель
    # может «утопить» свежую реплику и начать выдумывать то, что уже прислано
    # (например, сочинять текст песни, которая ЕСТЬ в сообщении). Явно велим
    # опираться на само сообщение и приложенные к нему материалы.
    has_current_media = isinstance(user_attachments_content, list)
    focus = (
        "[Отвечай на это сообщение] Ниже — АКТУАЛЬНАЯ реплика пользователя. Внимательно "
        "прочитай её и"
        + (" приложенные к ней материалы" if has_current_media else " весь её текст")
        + " и отвечай именно по ним. Если нужный текст, данные или файл УЖЕ есть в "
        "сообщении — используй их дословно, НЕ придумывай и не заменяй своей выдумкой."
    )
    tail.append({"role": "system", "content": focus})

    messages.extend(tail)

    # Текущее сообщение: либо мультимодальный контент, либо просто текст.
    messages.append(
        {
            "role": "user",
            "content": user_attachments_content
            if user_attachments_content is not None
            else user_message,
        }
    )
    return messages


# Русские названия дней недели для блока «время пользователя».
_RU_WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]


def session_user_time(session) -> str:
    """
    Текущее время пользователя по часовому поясу чата (session.timezone).
    Поддерживаются IANA-имена (Europe/Moscow) и смещения ("+03:00", "UTC+3").
    Пустая настройка или неизвестный пояс -> "" (блок времени не добавляется).
    """
    import re as _re
    from datetime import datetime, timedelta, timezone as _tz

    tz_name = (getattr(session, "timezone", "") or "").strip()
    if not tz_name:
        return ""
    tzinfo = None
    m = _re.fullmatch(r"(?:UTC|GMT)?\s*([+-])\s*(\d{1,2})(?::?(\d{2}))?", tz_name)
    if m:
        sign = -1 if m.group(1) == "-" else 1
        tzinfo = _tz(sign * timedelta(hours=int(m.group(2)), minutes=int(m.group(3) or 0)))
    else:
        try:
            from zoneinfo import ZoneInfo

            tzinfo = ZoneInfo(tz_name)
        except Exception:  # noqa: BLE001 — опечатка в имени пояса не должна ронять ход
            return ""
    now = datetime.now(tzinfo)
    return f"{now.strftime('%H:%M')}, {_RU_WEEKDAYS[now.weekday()]} {now.strftime('%d.%m.%Y')} ({tz_name})"


# ----------------------------------------------------------------------------
# DB-обёртка: тянет персонажа, записи Horae и историю из БД, затем зовёт
# чистую assemble_context(). Используется и веб-сервером, и Telegram-ботом.
# ----------------------------------------------------------------------------
async def _load_horae_records(session_db, session_id: int, character_id=None) -> list[HoraeRecord]:
    """
    Активные записи памяти:
      * привязанные к этой сессии (session_id);
      * лорбук персонажа (character_id) — из карточки SillyTavern;
      * глобальные (session_id и character_id оба NULL).
    """
    from sqlalchemy import and_, or_, select

    from backend.models import HoraeEntry

    conds = [
        HoraeEntry.session_id == session_id,
        and_(HoraeEntry.session_id.is_(None), HoraeEntry.character_id.is_(None)),
    ]
    if character_id is not None:
        conds.append(HoraeEntry.character_id == character_id)

    q = select(HoraeEntry).where(
        HoraeEntry.enabled == True,  # noqa: E712
        or_(*conds),
    )
    rows = (await session_db.execute(q)).scalars().all()
    return [
        HoraeRecord(
            category=r.category,
            title=r.title,
            content=r.content,
            keywords=r.keywords or [],
            always_on=r.always_on,
            enabled=r.enabled,
            priority=r.priority,
        )
        for r in rows
    ]


async def _load_persona_and_note(session_db, session) -> tuple[dict | None, str]:
    """Достаёт персону пользователя и заметку автора для сессии.

    Аватар персоны: сначала свой (Persona.avatar_path), иначе — аватар аккаунта
    владельца чата (чтобы модель «видела» пользователя, даже если у персоны своей
    картинки нет). Так аватарка персоны реально доходит до нейросети.
    """
    from backend.models import Persona, User

    owner_avatar = None
    if getattr(session, "owner_id", None):
        u = await session_db.get(User, session.owner_id)
        if u and _is_image(u.avatar_path):
            owner_avatar = u.avatar_path

    persona = None
    if session.persona_id:
        p = await session_db.get(Persona, session.persona_id)
        if p:
            persona = {
                "name": p.name,
                "description": p.description,
                "avatar": p.avatar_path if _is_image(p.avatar_path) else owner_avatar,
            }
    elif owner_avatar:
        # Персона не выбрана, но у пользователя есть аватар — покажем хотя бы его.
        persona = {"name": "", "description": "", "avatar": owner_avatar}
    return persona, session.author_note or ""


async def build_context_from_db(
    session_db,
    session,
    character,
    user_message: str,
    attachments_content,
    token_budget: int,
    history: list[dict] | None = None,
    send_avatars: bool = False,
    history_files_limit: int | None = None,
    web_access: bool = False,
) -> list[dict]:
    """
    Достаёт из БД память Horae, персону, заметку автора и историю сообщений,
    после чего вызывает чистую assemble_context().

    :param session: ORM-объект ChatSession (нужны его id, persona_id, author_note).
    :param history: если None — берём всю историю сессии из БД. Можно передать свою
        (например, для «регенерации» — историю БЕЗ последнего ответа ассистента).

    ВАЖНО: при обычном ходе вызывать ДО сохранения нового сообщения пользователя,
    иначе оно задвоится в истории.
    """
    from sqlalchemy import select

    from backend.models import Message

    records = await _load_horae_records(
        session_db, session.id, getattr(character, "id", None)
    )
    persona, author_note = await _load_persona_and_note(session_db, session)

    if history is None:
        hq = (
            select(Message)
            .where(Message.session_id == session.id)
            .order_by(Message.id)
        )
        msgs = (await session_db.execute(hq)).scalars().all()
        # СОХРАНЯЕМ вложения истории — данные тянутся из blob-таблицы точечно.
        # history_files_limit=None — без лимита (полная память по файлам).
        history = await messages_to_history_db(session_db, msgs, history_files_limit)

    char_dict = {
        "name": character.name,
        "description": character.description,
        "personality": character.personality,
        "scenario": character.scenario,
        "system_prompt": character.system_prompt,
        "mes_example": getattr(character, "mes_example", "") or "",
    }

    return assemble_context(
        character=char_dict,
        horae_records=records,
        history=history,
        user_message=user_message,
        user_attachments_content=attachments_content,
        persona=persona,
        author_note=author_note,
        token_budget=token_budget,
        character_avatar=character.avatar_path,
        persona_avatar=(persona or {}).get("avatar"),
        send_avatars=send_avatars,
        user_time=session_user_time(session),
        post_history_instructions=getattr(character, "post_history_instructions", "") or "",
        web_access=web_access,
    )
