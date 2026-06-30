"""
Импорт истории чата из SillyTavern.

SillyTavern хранит чат в формате JSONL (по одному JSON-объекту на строку):
  * первая строка — метаданные {user_name, character_name, ...};
  * остальные строки — сообщения {name, is_user, mes, swipes, swipe_id, ...}.

Парсер устойчив: если первой строки-метаданных нет, считаем все строки сообщениями.

Память Horae в экспортах встречается в ДВУХ видах, и мы поддерживаем оба:
  * JSON-поле `horae_meta` у сообщения (структурированный снимок состояния);
  * ВСТРОЕННЫЕ теги прямо в тексте реплики:
        <horae> time:..  location:..  atmosphere:..  characters:..
                costume:Имя=описание ... </horae>
        <horaeevent> event:важное|текст события </horaeevent>
    Эти теги мы ВЫРЕЗАЕМ из текста (чтобы они не висели сырым мусором в чате) и
    превращаем в записи памяти: последний <horae> — текущее состояние (always_on),
    все <horaeevent> — хронология событий.
"""
import json
import re

# Имена-заглушки SillyTavern, которые не стоит брать как имя персонажа.
_PLACEHOLDER_NAMES = {"", "unused", "system", "user"}

# Встроенные блоки Horae в тексте реплики.
_HORAE_BLOCK_RE = re.compile(r"<horae>(.*?)</horae>", re.DOTALL | re.IGNORECASE)
_HORAE_EVENT_RE = re.compile(r"<horaeevent>(.*?)</horaeevent>", re.DOTALL | re.IGNORECASE)


def _meta_nonempty(meta) -> bool:
    """Есть ли в horae_meta осмысленные данные (а не пустой шаблон)."""
    if not isinstance(meta, dict):
        return False
    for key, val in meta.items():
        if key == "timestamp":
            continue
        if isinstance(val, dict) and any(val.values()):
            return True
        if isinstance(val, list) and val:
            return True
        if isinstance(val, str) and val.strip():
            return True
    return False


def summarize_horae_meta(meta: dict) -> str:
    """Краткий снимок состояния из horae_meta (расширение Horae в SillyTavern)."""
    parts: list[str] = []
    scene = meta.get("scene") or {}
    if scene.get("location"):
        parts.append("Место: " + str(scene["location"]))
    if scene.get("atmosphere"):
        parts.append("Атмосфера: " + str(scene["atmosphere"]))
    if scene.get("characters_present"):
        parts.append("Присутствуют: " + ", ".join(map(str, scene["characters_present"])))
    ts = meta.get("timestamp") or {}
    when = " ".join(str(ts.get(k, "")) for k in ("story_date", "story_time")).strip()
    if when:
        parts.append("Время: " + when)
    for label, key in [("Одежда/внешность", "costumes"), ("Инвентарь", "items"),
                       ("Настроение", "mood"), ("Привязанность", "affection"),
                       ("Персонажи", "npcs")]:
        val = meta.get(key)
        if isinstance(val, dict) and val:
            kv = "; ".join(f"{k}: {v}" for k, v in val.items() if v)
            if kv:
                parts.append(f"{label}: {kv}")
    rels = meta.get("relationships") or []
    rtxt = "; ".join(
        f"{r.get('from', '?')}->{r.get('to', '?')}: {r.get('status', r.get('value', ''))}"
        for r in rels if isinstance(r, dict)
    ).strip("; ")
    if rtxt:
        parts.append("Отношения: " + rtxt)
    for label, key in [("События", "events"), ("Цели", "agenda")]:
        val = meta.get(key)
        if isinstance(val, list) and val:
            parts.append(f"{label}: " + "; ".join(map(str, val)))
    return "\n".join(parts)


# ---------- Встроенные теги <horae> / <horaeevent> ----------
def _parse_horae_block(body: str) -> dict:
    """Разобрать тело <horae> (строки key:value, costume:Имя=описание) в словарь."""
    state: dict = {"costumes": {}}
    for ln in body.splitlines():
        ln = ln.strip()
        if not ln or ":" not in ln:
            continue
        key, _, val = ln.partition(":")
        key = key.strip().lower()
        val = val.strip()
        if not val:
            continue
        if key == "costume":
            name, sep, desc = val.partition("=")
            if sep:
                state["costumes"][name.strip()] = desc.strip()
            else:
                state["costumes"][name.strip()] = ""
        else:
            state[key] = val
    return state


def _merge_inline_state(base: dict | None, new: dict) -> dict:
    """
    Слить снимки состояния: более новые значения важнее, но отсутствующие поля
    берём из прежнего снимка (костюмы аккумулируем по персонажам). Так даже частичный
    последний <horae>-блок не затирает подробности из предыдущих.
    """
    if base is None:
        return {**new, "costumes": dict(new.get("costumes") or {})}
    merged = dict(base)
    costumes = dict(merged.get("costumes") or {})
    for key, val in new.items():
        if key == "costumes":
            costumes.update({k: v for k, v in (val or {}).items() if v or k not in costumes})
        elif val:
            merged[key] = val
    merged["costumes"] = costumes
    return merged


def _summarize_inline_state(state: dict) -> str:
    """Снимок состояния из встроенного <horae>-блока -> человекочитаемый текст."""
    parts: list[str] = []
    labels = [("location", "Место"), ("atmosphere", "Атмосфера"),
              ("characters", "Присутствуют"), ("time", "Время")]
    for key, label in labels:
        if state.get(key):
            parts.append(f"{label}: {state[key]}")
    costumes = state.get("costumes") or {}
    if costumes:
        parts.append("Одежда/внешность: " + "; ".join(
            f"{k}: {v}" if v else k for k, v in costumes.items()
        ))
    known = {"location", "atmosphere", "characters", "time", "costumes"}
    for key, val in state.items():
        if key not in known and isinstance(val, str) and val:
            parts.append(f"{key}: {val}")
    return "\n".join(parts)


def _parse_event_block(body: str) -> list[str]:
    """Разобрать тело <horaeevent> в список текстов событий (без префикса event:важное|)."""
    out: list[str] = []
    for ln in body.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        if ln.lower().startswith("event:"):
            ln = ln[len("event:"):]
        if "|" in ln:  # 'важное|текст' -> 'текст'
            ln = ln.split("|", 1)[1]
        ln = ln.strip()
        if ln:
            out.append(ln)
    return out


def _extract_inline_horae(text: str):
    """
    Вырезать встроенные Horae-теги из текста.
    Возвращает (state|None, events[list], clean_text): state — из ПОСЛЕДНЕГО <horae>
    блока (актуальное состояние), events — из всех <horaeevent>, clean_text — текст
    реплики без этих тегов.
    """
    if not text or "<horae" not in text.lower():
        return None, [], text or ""
    state = None
    blocks = _HORAE_BLOCK_RE.findall(text)
    if blocks:
        state = _parse_horae_block(blocks[-1])
    events: list[str] = []
    for body in _HORAE_EVENT_RE.findall(text):
        events.extend(_parse_event_block(body))
    clean = _HORAE_EVENT_RE.sub("", _HORAE_BLOCK_RE.sub("", text))
    clean = re.sub(r"\n{3,}", "\n\n", clean).strip()
    return state, events, clean


def parse_sillytavern_chat(raw_text: str) -> dict:
    """
    Парсит JSONL-чат SillyTavern -> словарь с реплики/именами/данными Horae.

    Возвращает:
      character_name, user_name, messages[],
      horae_state  — текущий снимок состояния (always_on),
      horae_events — хронология событий (по тексту, по одному в строке).

    Имя персонажа берём из метаданных, а если там заглушка ('unused') — выводим из
    самих реплик. Horae собираем И из встроенных тегов в тексте, И из поля horae_meta.
    Системные Horae-логи в чат как реплики НЕ тащим, но их данные извлекаем.
    """
    lines = [ln for ln in raw_text.splitlines() if ln.strip()]
    if not lines:
        raise ValueError("Пустой файл чата")

    character_name = ""
    user_name = "User"
    start = 0

    # Первая строка — метаданные (без поля 'mes').
    try:
        first = json.loads(lines[0])
        if isinstance(first, dict) and "mes" not in first:
            character_name = first.get("character_name") or ""
            user_name = first.get("user_name") or user_name
            start = 1
    except json.JSONDecodeError:
        pass

    messages: list[dict] = []
    name_counts: dict[str, int] = {}
    latest_meta = None
    latest_inline_state = None
    all_events: list[str] = []

    for line in lines[start:]:
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict) or "mes" not in obj:
            continue

        # Horae: и из встроенных тегов, и из JSON-поля — собираем всегда, даже у
        # системных сообщений (чтобы ничего не потерять).
        st, evs, clean = _extract_inline_horae(obj.get("mes", ""))
        if st:
            latest_inline_state = _merge_inline_state(latest_inline_state, st)
        if evs:
            all_events.extend(evs)
        if _meta_nonempty(obj.get("horae_meta")):
            latest_meta = obj["horae_meta"]

        # Системные пометки SillyTavern — это служебные Horae-логи, не реплики.
        if obj.get("is_system"):
            continue

        role = "user" if obj.get("is_user") else "assistant"
        name = (obj.get("name") or "").strip()

        # Чистим все свайпы от встроенных тегов Horae.
        raw_swipes = obj.get("swipes") or [obj.get("mes", "")]
        swipes = []
        for s in raw_swipes:
            _, _, cs = _extract_inline_horae(s if isinstance(s, str) else "")
            swipes.append(cs)
        if not swipes:
            swipes = [clean]

        # Сообщение, в котором кроме Horae-тегов ничего не было, — пропускаем как реплику.
        if not clean.strip() and not any(s.strip() for s in swipes):
            continue

        if role == "assistant" and name:
            name_counts[name] = name_counts.get(name, 0) + 1

        swipe_id = obj.get("swipe_id", 0)
        if not isinstance(swipe_id, int) or swipe_id < 0 or swipe_id >= len(swipes):
            swipe_id = 0
        messages.append(
            {
                "role": role,
                "content": swipes[swipe_id] if swipes else clean,
                "swipes": swipes,
                "active_swipe": swipe_id,
                "speaker": name if role == "assistant" else None,
            }
        )

    # Если имя персонажа — заглушка, берём самое частое имя из реплик.
    if character_name.strip().lower() in _PLACEHOLDER_NAMES and name_counts:
        character_name = max(name_counts, key=name_counts.get)
    if not character_name.strip():
        character_name = "Импортированный чат"

    # Текущее состояние: встроенный <horae> приоритетнее (он и подробнее, и свежее).
    if latest_inline_state:
        horae_state = _summarize_inline_state(latest_inline_state)
    elif latest_meta:
        horae_state = summarize_horae_meta(latest_meta)
    else:
        horae_state = ""

    # Хронология событий: дедуп с сохранением порядка.
    seen: set[str] = set()
    uniq_events: list[str] = []
    for ev in all_events:
        if ev and ev not in seen:
            seen.add(ev)
            uniq_events.append(ev)
    horae_events = "\n".join("• " + e for e in uniq_events)

    return {
        "character_name": character_name,
        "user_name": user_name,
        "messages": messages,
        "horae_state": horae_state,
        "horae_events": horae_events,
    }
