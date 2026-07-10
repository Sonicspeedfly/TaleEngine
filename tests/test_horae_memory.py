"""
Тесты подсистемы памяти Horae.

Проверяют сборку контекста БЕЗ БД и БЕЗ сети — именно для этого ядро (assemble_context)
сделано чистой функцией. Покрываем главное по ТЗ: подмешивание памяти в системный
промпт, срабатывание по ключевым словам и обрезку истории под бюджет токенов.
"""
from types import SimpleNamespace

from backend.horae_memory import (
    HoraeRecord,
    assemble_context,
    estimate_content_tokens,
    estimate_tokens,
    messages_to_history,
)


def _msg(mid, role, content, attachments=None):
    return SimpleNamespace(id=mid, role=role, content=content, attachments=attachments or [])


def _char() -> dict:
    return {
        "name": "Aria",
        "description": "Хранительница древней библиотеки.",
        "personality": "Спокойная, мудрая.",
        "scenario": "Древняя библиотека на краю мира.",
        "system_prompt": "Отыгрывай роль живо и в характере.",
    }


def test_system_prompt_contains_character_fields():
    messages = assemble_context(
        character=_char(), horae_records=[], history=[], user_message="Привет"
    )
    system = messages[0]
    assert system["role"] == "system"
    assert "Aria" in system["content"]
    assert "Хранительница древней библиотеки." in system["content"]


def test_always_on_record_is_always_injected():
    rec = HoraeRecord(
        category="state",
        title="Инвентарь",
        content="У игрока есть старый меч.",
        keywords=[],
        always_on=True,
        enabled=True,
        priority=10,
    )
    messages = assemble_context(
        character=_char(), horae_records=[rec], history=[], user_message="Осмотрюсь"
    )
    # always_on подмешивается даже без ключевых слов.
    assert "У игрока есть старый меч." in messages[0]["content"]


def test_keyword_record_triggers_only_on_keyword():
    rec = HoraeRecord(
        category="lore",
        title="Дракон",
        content="Дракон спит в северных горах.",
        keywords=["дракон"],
        always_on=False,
        enabled=True,
        priority=0,
    )
    # Нет ключевого слова -> запись НЕ подмешивается.
    m1 = assemble_context(
        character=_char(), horae_records=[rec], history=[], user_message="Иду в лес"
    )
    assert "Дракон спит в северных горах." not in m1[0]["content"]

    # Есть ключевое слово -> запись подмешивается.
    m2 = assemble_context(
        character=_char(),
        horae_records=[rec],
        history=[],
        user_message="А где живёт дракон?",
    )
    assert "Дракон спит в северных горах." in m2[0]["content"]


def test_disabled_record_never_injected():
    rec = HoraeRecord(
        category="lore",
        title="Секрет",
        content="секретная информация",
        keywords=["секрет"],
        always_on=True,
        enabled=False,  # выключено -> не должно попасть в контекст
        priority=0,
    )
    messages = assemble_context(
        character=_char(), horae_records=[rec], history=[], user_message="секрет"
    )
    assert "секретная информация" not in messages[0]["content"]


def test_priority_orders_records():
    low = HoraeRecord("state", "Low", "low-content", [], True, True, priority=1)
    high = HoraeRecord("state", "High", "high-content", [], True, True, priority=99)
    messages = assemble_context(
        character=_char(), horae_records=[low, high], history=[], user_message="hi"
    )
    content = messages[0]["content"]
    # Запись с большим приоритетом должна идти раньше в блоке памяти.
    assert content.index("high-content") < content.index("low-content")


def test_history_trimmed_to_budget():
    # Большая история должна обрезаться под маленький бюджет токенов.
    history = [{"role": "user", "content": "word " * 100} for _ in range(50)]
    messages = assemble_context(
        character=_char(),
        horae_records=[],
        history=history,
        user_message="последнее сообщение",
        token_budget=300,
    )
    # В итог попали не все 50 сообщений истории.
    assert len(messages) < 52
    # Текущее сообщение всегда последнее.
    assert messages[-1]["content"] == "последнее сообщение"


def test_attachments_content_passed_through():
    # Если есть вложения, текущее сообщение должно нести мультимодальный контент.
    multimodal = [
        {"type": "text", "text": "что на фото?"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
    ]
    messages = assemble_context(
        character=_char(),
        horae_records=[],
        history=[],
        user_message="что на фото?",
        user_attachments_content=multimodal,
    )
    assert messages[-1]["content"] == multimodal


def test_estimate_tokens_is_positive():
    assert estimate_tokens("") == 1
    assert estimate_tokens("abcd" * 10) > 1


def _image_blocks(messages):
    out = []
    for m in messages:
        if isinstance(m["content"], list):
            out += [b for b in m["content"] if b.get("type") == "image_url"]
    return out


def test_send_avatars_injects_two_images():
    messages = assemble_context(
        character=_char(), horae_records=[], history=[], user_message="привет",
        character_avatar="data:image/png;base64,AAAA",
        persona_avatar="https://example.com/p.png",
        send_avatars=True,
    )
    assert len(_image_blocks(messages)) == 2  # внешность персонажа + пользователя


def test_no_avatars_when_disabled():
    messages = assemble_context(
        character=_char(), horae_records=[], history=[], user_message="привет",
        character_avatar="data:image/png;base64,AAAA", send_avatars=False,
    )
    assert _image_blocks(messages) == []


def test_persona_injected_into_system_prompt():
    persona = {"name": "Кай", "description": "Молодой картограф."}
    messages = assemble_context(
        character=_char(), horae_records=[], history=[],
        user_message="привет", persona=persona,
    )
    assert "Кай" in messages[0]["content"]
    assert "Молодой картограф." in messages[0]["content"]


def test_history_keeps_attachments_so_model_sees_earlier_files():
    """
    Баг: вложения из истории терялись — модель «видела» файл только на своём ходу.
    Теперь прошлое сообщение с картинкой попадает в историю мультимодальным блоком.
    """
    img = {"type": "image", "data": "data:image/png;base64,AAAABBBB", "mime": "image/png", "name": "p.png"}
    msgs = [
        _msg(1, "user", "посмотри на это фото", [img]),
        _msg(2, "assistant", "вижу картинку"),
    ]
    hist = messages_to_history(msgs)
    # Реплика пользователя стала мультимодальной: текст + картинка.
    first = hist[0]["content"]
    assert isinstance(first, list)
    assert any(b.get("type") == "image_url" for b in first)
    assert any(b.get("type") == "text" and "фото" in b["text"] for b in first)
    # Ответ ассистента — обычный текст.
    assert hist[1] == {"role": "assistant", "content": "вижу картинку"}


def test_history_attachment_over_limit_becomes_note():
    """Слишком объёмное вложение из истории заменяется пометкой, а не тянется целиком."""
    from backend.horae_memory import _MAX_HISTORY_ATT_BYTES
    huge = {"type": "audio", "data": "Q" * (_MAX_HISTORY_ATT_BYTES + 10), "mime": "audio/mp3", "name": "v.mp3"}
    msgs = [_msg(1, "user", "послушай", [huge])]
    hist = messages_to_history(msgs)
    assert isinstance(hist[0]["content"], str)      # не мультимодальный список
    assert "[аудио]" in hist[0]["content"]           # но пометка о факте вложения есть
    assert "послушай" in hist[0]["content"]


def test_estimate_content_tokens_ignores_base64_size():
    """Оценка токенов не должна считать base64 как текст (иначе история выбрасывается)."""
    big_b64 = "A" * 5_000_000
    multimodal = [
        {"type": "text", "text": "hi"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + big_b64}},
    ]
    # Мультимодальный блок стоит десятки токенов, а не миллион (как длина base64).
    assert estimate_content_tokens(multimodal) < 1000
    assert estimate_content_tokens("просто текст") == estimate_tokens("просто текст")


def test_multimodal_history_survives_budget_trim():
    """Картинка из недавней истории не должна выбрасываться бюджетом из-за размера base64."""
    img_hist = [{
        "role": "user",
        "content": [
            {"type": "text", "text": "фото"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "A" * 2_000_000}},
        ],
    }]
    messages = assemble_context(
        character=_char(), horae_records=[], history=img_hist,
        user_message="и что?", token_budget=2000,
    )
    # Картинка сохранилась в контексте (в истории есть image_url блок).
    assert _image_blocks(messages)


def test_author_note_injected_before_user_message():
    messages = assemble_context(
        character=_char(), horae_records=[], history=[],
        user_message="что дальше?", author_note="Держи мрачный тон.",
    )
    # Заметка автора — отдельным системным сообщением прямо перед репликой пользователя.
    note = messages[-2]
    assert note["role"] == "system"
    assert "Author's Note" in note["content"]
    assert "Держи мрачный тон." in note["content"]
    assert messages[-1]["content"] == "что дальше?"


def test_user_time_block_injected_before_user_message():
    """Блок «[Время пользователя]» добавляется системным сообщением у конца контекста."""
    messages = assemble_context(
        character=_char(), horae_records=[], history=[],
        user_message="Доброе утро!", user_time="09:15, четверг 10.07.2026 (Europe/Moscow)",
    )
    time_msgs = [m for m in messages if m["role"] == "system" and "Время пользователя" in m["content"]]
    assert len(time_msgs) == 1
    assert "09:15" in time_msgs[0]["content"]
    # Без user_time блока нет.
    messages2 = assemble_context(character=_char(), horae_records=[], history=[], user_message="привет")
    assert not any("Время пользователя" in m["content"] for m in messages2 if m["role"] == "system")


def test_session_user_time_offset_iana_and_bad():
    """session_user_time: смещения '+03:00', IANA-имена, пусто/опечатка -> ''. """
    from backend.horae_memory import session_user_time

    out = session_user_time(SimpleNamespace(timezone="+03:00"))
    assert "(+03:00)" in out and ":" in out
    assert session_user_time(SimpleNamespace(timezone="")) == ""
    assert session_user_time(SimpleNamespace(timezone="Nope/Nowhere")) == ""
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo("Europe/Moscow")
        has_tzdb = True
    except Exception:
        has_tzdb = False
    if has_tzdb:
        assert "Europe/Moscow" in session_user_time(SimpleNamespace(timezone="Europe/Moscow"))


def test_video_attachment_label_in_history_note():
    """Видео, не влезшее в лимит вложений истории, помечается как [видео: имя]."""
    big = "data:video/mp4;base64," + "A" * 6_000_000  # больше _MAX_HISTORY_ATT_BYTES
    msgs = [
        _msg(1, "user", "смотри", [{"type": "video", "data": big, "mime": "video/mp4", "name": "clip.mp4"}]),
        _msg(2, "assistant", "вижу"),
    ]
    history = messages_to_history(msgs)
    assert "[видео: clip.mp4]" in history[0]["content"]
