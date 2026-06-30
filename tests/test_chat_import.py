"""
Тесты импорта чата из SillyTavern (JSONL).
"""
import json

from backend.chat_import import parse_sillytavern_chat


def _jsonl(*objs) -> str:
    return "\n".join(json.dumps(o, ensure_ascii=False) for o in objs)


def test_parse_with_metadata_header():
    raw = _jsonl(
        {"user_name": "Игрок", "character_name": "Aria"},
        {"name": "Aria", "is_user": False, "mes": "Привет!"},
        {"name": "Игрок", "is_user": True, "mes": "Здравствуй"},
    )
    parsed = parse_sillytavern_chat(raw)
    assert parsed["character_name"] == "Aria"
    assert len(parsed["messages"]) == 2
    m0 = parsed["messages"][0]
    assert m0["role"] == "assistant" and m0["content"] == "Привет!"
    assert m0["swipes"] == ["Привет!"] and m0["active_swipe"] == 0
    assert m0["speaker"] == "Aria"  # сохраняем, кто говорил
    assert parsed["messages"][1]["role"] == "user"


def test_parse_keeps_swipes_and_active_index():
    raw = _jsonl(
        {"character_name": "Bot"},
        {"is_user": False, "mes": "B", "swipes": ["A", "B", "C"], "swipe_id": 1},
    )
    msg = parse_sillytavern_chat(raw)["messages"][0]
    assert msg["swipes"] == ["A", "B", "C"]
    assert msg["active_swipe"] == 1


def test_parse_without_metadata_treats_all_as_messages():
    raw = _jsonl(
        {"is_user": True, "mes": "первое"},
        {"is_user": False, "mes": "второе"},
    )
    parsed = parse_sillytavern_chat(raw)
    assert len(parsed["messages"]) == 2


def test_invalid_swipe_id_falls_back_to_zero():
    raw = _jsonl({"character_name": "X"}, {"is_user": False, "mes": "m", "swipe_id": 99})
    assert parse_sillytavern_chat(raw)["messages"][0]["active_swipe"] == 0


def test_unused_charname_derived_from_messages():
    raw = _jsonl(
        {"character_name": "unused", "user_name": "unused"},
        {"name": "Селена", "is_user": False, "mes": "реплика 1"},
        {"name": "Селена", "is_user": False, "mes": "реплика 2"},
        {"name": "Игрок", "is_user": True, "mes": "ответ"},
    )
    assert parse_sillytavern_chat(raw)["character_name"] == "Селена"


def test_system_messages_skipped():
    raw = _jsonl(
        {"character_name": "X"},
        {"is_user": False, "mes": "обычное"},
        {"is_user": False, "is_system": True, "mes": "системный лог"},
    )
    msgs = parse_sillytavern_chat(raw)["messages"]
    assert len(msgs) == 1 and msgs[0]["content"] == "обычное"


def test_horae_meta_extracted_as_state():
    raw = _jsonl(
        {"character_name": "X"},
        {"is_user": False, "mes": "m",
         "horae_meta": {"scene": {"location": "Таверна", "atmosphere": "мрачно"}}},
    )
    state = parse_sillytavern_chat(raw)["horae_state"]
    assert "Таверна" in state and "мрачно" in state


def test_inline_horae_tags_stripped_and_extracted():
    """Встроенные <horae>/<horaeevent> вырезаются из текста, но их данные сохраняются."""
    raw = _jsonl(
        {"character_name": "unused"},
        {"name": "Aria", "is_user": False,
         "mes": "Она улыбнулась.\n<horae>\nlocation:Лес\natmosphere:тихо\n"
                "costume:Aria=плащ\n</horae>\n<horaeevent>\nevent:важное|Они встретились.\n</horaeevent>"},
        {"name": "Hero", "is_user": True, "mes": "Привет."},
        # Системный Horae-лог: не реплика, но состояние (последнее) учитываем.
        {"is_user": False, "is_system": True, "mes": "<horae>\nlocation:Поляна\n</horae>"},
    )
    p = parse_sillytavern_chat(raw)
    # Сырые теги не попадают в текст реплики.
    assert p["messages"][0]["content"] == "Она улыбнулась."
    assert all("<horae" not in m["content"] for m in p["messages"])
    # Системный лог не стал репликой.
    assert len(p["messages"]) == 2
    # Слияние состояний: location обновился из последнего блока, костюм/атмосфера сохранились.
    assert "Поляна" in p["horae_state"]
    assert "плащ" in p["horae_state"] and "тихо" in p["horae_state"]
    # Событие извлечено без префикса 'event:важное|'.
    assert "Они встретились." in p["horae_events"]
    assert p["character_name"] == "Aria"


def test_inline_horae_only_message_is_not_a_bubble():
    """Реплика, состоящая ТОЛЬКО из Horae-тегов, не превращается в пустое сообщение."""
    raw = _jsonl(
        {"character_name": "X"},
        {"is_user": False, "mes": "реальная реплика"},
        {"is_user": False, "mes": "<horae>\nlocation:Двор\n</horae>"},
    )
    p = parse_sillytavern_chat(raw)
    assert len(p["messages"]) == 1 and p["messages"][0]["content"] == "реальная реплика"
    assert "Двор" in p["horae_state"]
