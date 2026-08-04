"""
Тесты режима «вне роли» (OOC / ассистент) и честного подсчёта токенов.

Оба механизма лечат один и тот же симптом: в длинном чате модель переставала
слышать прикладную просьбу. Причин было две — контекст раздувался вдвое против
показанного бюджета, а прямо перед репликой пользователя стояло требование
«оставайся в образе», которое перебивало саму задачу.
"""
from backend.horae_memory import (
    ASSISTANT_GUIDE,
    BEHAVIOR_GUIDE,
    _replace_first_text,
    assemble_context,
    detect_ooc,
    estimate_tokens,
)


def _char():
    return {"name": "Джеми", "description": "бариста", "personality": "дерзкая"}


def _system_text(messages):
    return "\n".join(m["content"] for m in messages if m["role"] == "system")


# ==================== Подсчёт токенов ====================

def test_cyrillic_is_not_counted_as_four_chars_per_token():
    """
    Главная регрессия: прежняя оценка len/4 занижала русский текст почти вдвое.
    Из-за неё окно контекста показывало не то, что реально уходит провайдеру,
    история почти никогда не обрезалась, а расход был вдвое больше ожидаемого.
    """
    text = "Привет, как у тебя дела сегодня вечером?" * 20
    assert estimate_tokens(text) > len(text) // 4 * 1.5


def test_latin_still_close_to_four_chars_per_token():
    """На латинице правило «4 символа на токен» и было верным — не ломаем его."""
    text = "The quick brown fox jumps over the lazy dog. " * 20
    naive = len(text) // 4
    assert naive * 0.6 < estimate_tokens(text) < naive * 1.4


def test_estimate_tokens_survives_special_sequences():
    """Пользователь может написать служебную последовательность — не должно падать."""
    assert estimate_tokens("<|endoftext|> обычный текст") > 1


def test_history_is_trimmed_once_budget_is_honest():
    """Считая честно, сборщик обязан уложиться в бюджет и обрезать лишнее."""
    history = [{"role": "user", "content": "Длинная русская реплика про всё на свете. " * 20}
               for _ in range(60)]
    messages = assemble_context(character=_char(), horae_records=[], history=history,
                                user_message="и что дальше?", token_budget=3000)
    kept = [m for m in messages if m["role"] != "system"]
    assert len(kept) - 1 < len(history)


# ==================== Распознавание пометки «вне роли» ====================

def test_double_parens_marks_ooc():
    ooc, text = detect_ooc("((напиши пост для подруги))")
    assert ooc and text == "напиши пост для подруги"


def test_slash_ooc_marks_ooc():
    ooc, text = detect_ooc("/ooc разбери этот код")
    assert ooc and text == "разбери этот код"


def test_double_slash_marks_ooc():
    ooc, text = detect_ooc("//переведи на английский")
    assert ooc and text == "переведи на английский"


def test_plain_message_is_not_ooc():
    ooc, text = detect_ooc("Привет, как дела?")
    assert not ooc and text == "Привет, как дела?"


def test_parens_inside_message_are_not_ooc():
    """Скобки в СЕРЕДИНЕ — обычная ролевая ремарка, роль ломать нельзя."""
    ooc, _ = detect_ooc("Джеми улыбнулась ((кажется, она рада)) и налила кофе")
    assert not ooc


def test_empty_marker_is_not_ooc():
    """Пустые скобки — не команда, а просто текст."""
    assert detect_ooc("(())")[0] is False


# ==================== Влияние режима на контекст ====================

def test_ooc_swaps_behavior_guide():
    messages = assemble_context(character=_char(), horae_records=[], history=[],
                                user_message="напиши пост", ooc=True)
    system = _system_text(messages)
    assert ASSISTANT_GUIDE in system
    assert BEHAVIOR_GUIDE not in system


def test_ooc_drops_character_reminder_from_tail():
    """
    В обычном режиме персонажа освежаем в конце (в длинном окне личность «плывёт»),
    а в режиме ассистента не напоминаем о нём вовсе — задача важнее.
    """
    normal = assemble_context(character=_char(), horae_records=[], history=[],
                              user_message="напиши пост")
    ooc = assemble_context(character=_char(), horae_records=[], history=[],
                           user_message="напиши пост", ooc=True)
    assert "[Напоминание]" in _system_text(normal)
    assert "[Напоминание]" not in _system_text(ooc)


def test_ooc_focus_block_demands_the_task():
    messages = assemble_context(character=_char(), horae_records=[], history=[],
                                user_message="напиши пост", ooc=True)
    last_system = [m for m in messages if m["role"] == "system"][-1]["content"]
    assert "[Выполни эту задачу]" in last_system


def test_user_message_stays_last_in_ooc():
    """Реплика пользователя обязана остаться последней — иначе теряется её вес."""
    messages = assemble_context(character=_char(), horae_records=[], history=[],
                                user_message="напиши пост", ooc=True)
    assert messages[-1]["role"] == "user"
    assert messages[-1]["content"] == "напиши пост"


def test_character_block_survives_in_ooc():
    """Кто такой персонаж — знать всё ещё нужно, про него могут спросить."""
    messages = assemble_context(character=_char(), horae_records=[], history=[],
                                user_message="кто ты?", ooc=True)
    assert "Джеми" in _system_text(messages)


# ==================== Снятие пометки в мультимодальном контенте ====================

def test_replace_first_text_touches_only_text_block():
    content = [
        {"type": "text", "text": "((разбери фото))"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
    ]
    out = _replace_first_text(content, "((разбери фото))", "разбери фото")
    assert out[0]["text"] == "разбери фото"
    assert out[1] == content[1]


def test_replace_first_text_ignores_plain_string():
    assert _replace_first_text("просто текст", "просто текст", "другое") == "просто текст"


def test_replace_first_text_replaces_only_first_match():
    content = [{"type": "text", "text": "дубль"}, {"type": "text", "text": "дубль"}]
    out = _replace_first_text(content, "дубль", "новое")
    assert out[0]["text"] == "новое" and out[1]["text"] == "дубль"


# ==================== Проводка через БД (веб, Telegram, регенерация) ====================

async def test_ooc_marker_works_through_db_builder(client):
    """
    Разбор пометки живёт в build_context_from_db, а не в обработчике веб-запроса —
    иначе режим работал бы только в чате и молча игнорировался в Telegram, при
    регенерации и retry. Проверяем на настоящей сборке из БД.
    """
    from backend import models
    from backend.database import AsyncSessionLocal
    from backend.horae_memory import build_context_from_db

    char = client.post("/api/characters", json={"name": "Джеми", "personality": "дерзкая"}).json()
    sid = client.post(f"/api/sessions?character_id={char['id']}").json()["session_id"]

    async with AsyncSessionLocal() as db:
        sess = await db.get(models.ChatSession, sid)
        character = await db.get(models.Character, char["id"])
        messages = await build_context_from_db(
            db, sess, character, "((напиши пост подруге))", None, 8000,
        )

    system = "\n".join(m["content"] for m in messages if m["role"] == "system")
    assert "[Напоминание]" not in system             # о персонаже не напоминаем
    assert "[Выполни эту задачу]" in system          # фокус переключён на задачу
    assert messages[-1]["content"] == "напиши пост подруге"  # пометка снята с текста


async def test_assistant_mode_flag_works_without_marker(client):
    """Тумблер из интерфейса включает тот же режим без пометки в сообщении."""
    from backend import models
    from backend.database import AsyncSessionLocal
    from backend.horae_memory import build_context_from_db

    char = client.post("/api/characters", json={"name": "Джеми"}).json()
    sid = client.post(f"/api/sessions?character_id={char['id']}").json()["session_id"]

    async with AsyncSessionLocal() as db:
        sess = await db.get(models.ChatSession, sid)
        character = await db.get(models.Character, char["id"])
        on = await build_context_from_db(
            db, sess, character, "напиши пост", None, 8000, assistant_mode=True)
        off = await build_context_from_db(
            db, sess, character, "напиши пост", None, 8000, assistant_mode=False)

    assert "[Напоминание]"not in "\n".join(m["content"] for m in on if m["role"] == "system")
    assert "[Напоминание]"in "\n".join(m["content"] for m in off if m["role"] == "system")
