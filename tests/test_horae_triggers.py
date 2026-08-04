"""
Срабатывание памяти Horae: ключевые слова, окно сканирования, лимит записей.

Эти тесты закрывают то, из-за чего память «работала криво»: лор подмешивался
невпопад (ключ «кот» срабатывал на «который»), переставал срабатывать в репликах
с вложениями, а при большом лорбуке в промпт уезжало всё разом.
"""
from unittest.mock import patch

import pytest
from sqlalchemy import select

from backend.horae_memory import (
    HoraeRecord,
    _scan_text_for_triggers,
    assemble_context,
    keyword_hits,
)


def _rec(keywords, title="Запись", priority=0, always_on=False):
    return HoraeRecord(category="lore", title=title, content=f"ФАКТ-{title}",
                       keywords=keywords, always_on=always_on, enabled=True,
                       priority=priority)


def _fires(keywords, text) -> bool:
    return bool(_scan_text_for_triggers(text, [_rec(keywords)]))


def _char():
    return {"name": "Тест", "description": "", "personality": "",
            "scenario": "", "system_prompt": ""}


# ---------- Ложные срабатывания (главная причина «кривой» памяти) ----------
@pytest.mark.parametrize("kw,text", [
    ("кот", "который час?"),          # всё это раньше срабатывало
    ("кот", "поставил котёл"),
    ("мир", "подтверждает мирный договор"),
    ("сон", "мы обсуждали Сонечку"),
    ("Рим", "он всё время римейкует"),
    ("лес", "она вышла на лестницу"),
    ("маг", "зашёл в магазин"),
    ("рука", "порвал рукав"),          # «-ав» не падежное окончание
    ("король", "род Корольковых"),     # «-ьков» тоже
    ("стол", "приехал в столицу"),
    ("нож", "взял ножницы"),
    ("дом", "она домохозяйка"),
])
def test_keyword_does_not_fire_on_a_different_word(kw, text):
    assert not _fires([kw], text), f"«{kw}» не должно срабатывать на «{text}»"


# ---------- Нормальные срабатывания ----------
@pytest.mark.parametrize("kw,text", [
    ("кот", "смотри, кот!"),               # слово целиком
    ("кот", "погладил кота"),              # окончание приросло
    ("кот", "следил за котом"),            # творительный падеж
    ("меч", "он взял мечи"),
    ("меч", "ударил мечом"),
    ("меч", "звенели мечами"),
    ("король", "мы говорили о короле"),    # мягкий знак сменился окончанием
    ("король", "стоял за королём"),
    ("конь", "поехал на коне"),
    ("медведь", "видели медведя"),
    ("башня", "сигнал с башнями"),
    ("вода", "набрал воды"),
    ("КОТ", "тут КоТ"),                    # регистр не важен
])
def test_keyword_fires_on_the_word_and_its_declensions(kw, text):
    assert _fires([kw], text)


def test_phrase_keyword_matches_as_substring():
    """Пробел в ключе = намеренная фраза, ищем как есть."""
    assert _fires(["тёмный лес"], "они вошли в тёмный лес на закате")
    assert not _fires(["тёмный лес"], "лес был тёмный")


def test_wildcard_keyword_covers_hard_declensions():
    """
    Для слов с беглой гласной («замок» → «замка») угадывать окончание нельзя без
    настоящего стеммера, поэтому есть явный шаблон «замк*» — предсказуемо и
    управляется пользователем.
    """
    assert not _fires(["замок"], "мы стояли у замка")   # честно: не угадываем
    assert _fires(["замк*"], "мы стояли у замка")
    assert _fires(["замк*"], "владел замком")
    assert not _fires(["замк*"], "он замер")


def test_empty_and_blank_keywords_never_fire():
    """Пустой ключ не должен активировать запись на любом тексте."""
    assert not _fires(["", "   "], "любой текст")
    assert not _fires(["*"], "любой текст")


# ---------- Окно сканирования ----------
def test_triggers_scan_text_inside_multimodal_messages():
    """
    Реплика с вложением раньше пропускалась ЦЕЛИКОМ вместе со своим текстом —
    стоило приложить фото, и ключевые слова из этой реплики переставали
    активировать память. Со стороны — «Horae срабатывает через раз».
    """
    history = [
        {"role": "user", "content": [
            {"type": "text", "text": "вот фото моего кота"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}},
        ]},
        {"role": "assistant", "content": "милый"},
    ]
    msgs = assemble_context(character=_char(), horae_records=[_rec(["кот"], "Кот")],
                            history=history, user_message="расскажи о нём")
    system = " ".join(m["content"] for m in msgs if isinstance(m.get("content"), str))
    assert "ФАКТ-Кот" in system


def test_trigger_window_covers_recent_turns_not_only_current():
    """Ключ, названный пару реплик назад, ещё работает (окно последних ходов)."""
    history = [{"role": "user", "content": "нашли артефакт"},
               {"role": "assistant", "content": "и что дальше?"},
               {"role": "user", "content": "думаем"}]
    msgs = assemble_context(character=_char(), horae_records=[_rec(["артефакт"], "Арт")],
                            history=history, user_message="ну?")
    system = " ".join(m["content"] for m in msgs if isinstance(m.get("content"), str))
    assert "ФАКТ-Арт" in system


def test_very_old_message_does_not_keep_triggering():
    """Но давняя реплика не должна держать лор активным вечно."""
    history = [{"role": "user", "content": "нашли артефакт"}] + [
        {"role": "assistant", "content": f"реплика {i}"} for i in range(20)
    ]
    msgs = assemble_context(character=_char(), horae_records=[_rec(["артефакт"], "Арт")],
                            history=history, user_message="ну?")
    system = " ".join(m["content"] for m in msgs if isinstance(m.get("content"), str))
    assert "ФАКТ-Арт" not in system


# ---------- Лимит и приоритеты ----------
def test_keyword_records_are_capped_by_priority():
    """
    Большой лорбук не должен уезжать в промпт целиком: берём самые приоритетные.
    Раньше лимита не было — полсотни сработавших записей раздували каждый ход.
    """
    records = [_rec(["тест"], title=f"R{i}", priority=i) for i in range(100)]
    got = _scan_text_for_triggers("тест", records, max_keyword_records=5)
    assert len(got) == 5
    assert [r.priority for r in got] == [99, 98, 97, 96, 95]


def test_always_on_records_bypass_the_cap():
    """always_on — явное решение пользователя «показывать всегда», лимит не режет."""
    records = [_rec(["тест"], title=f"R{i}", priority=0) for i in range(30)]
    records += [_rec([], title=f"A{i}", always_on=True) for i in range(10)]
    got = _scan_text_for_triggers("тест", records, max_keyword_records=3)
    assert sum(1 for r in got if r.always_on) == 10
    assert sum(1 for r in got if not r.always_on) == 3


def test_disabled_records_never_activate():
    rec = _rec(["тест"], "Выключенная")
    rec.enabled = False
    assert not _scan_text_for_triggers("тест", [rec])


def test_keyword_hits_is_usable_standalone():
    """Публичная функция — на неё опирается и UI-подсказка, и тесты."""
    from backend.horae_memory import _text_tokens

    text = "погладил кота"
    assert keyword_hits("кот", text, _text_tokens(text))
    assert not keyword_hits("который", text, _text_tokens(text))


# ---------- Авто-сводка: устойчивость указателя ----------
@pytest.mark.asyncio
async def test_summary_recovers_after_messages_are_deleted():
    """
    Указатель «до какого сообщения учтено» мог уехать в будущее: пользователь
    удалял последние сообщения (а SQLite переиспользует id), и условие
    «id > указателя» не выполнялось НИКОГДА — сводка молча умирала навсегда.
    """
    from backend import main, models
    from backend.database import AsyncSessionLocal, engine, init_db

    await engine.dispose()
    await init_db()

    async with AsyncSessionLocal() as db:
        ch = models.Character(name="Забывчивый")
        db.add(ch)
        await db.commit()
        await db.refresh(ch)
        sess = models.ChatSession(character_id=ch.id, user_key="test:horae-ptr")
        db.add(sess)
        await db.commit()
        await db.refresh(sess)
        sid = sess.id
        for i in range(12):
            db.add(models.Message(session_id=sid, role="user", content=f"событие {i}"))
        # Указатель «из будущего» — как после удаления свежих сообщений.
        db.add(models.HoraeEntry(
            session_id=sid, category="summary", title="📜 Память чата (авто)",
            content="старая память", always_on=True, enabled=True,
            meta={"last_message_id": 10_000_000},
        ))
        await db.commit()

    called = {}

    async def fake_complete(messages, params=None, connection=None, kind="service"):
        called["yes"] = True
        return "Память пересобрана после удаления сообщений."

    with patch("backend.main.complete", new=fake_complete):
        await main._maybe_update_summary(sid)

    assert called.get("yes"), "сводка обязана ожить, а не молчать навсегда"
    async with AsyncSessionLocal() as db:
        entry = (await db.execute(select(models.HoraeEntry).where(
            models.HoraeEntry.session_id == sid,
            models.HoraeEntry.category == "summary",
        ))).scalars().first()
    assert "пересобрана" in entry.content
    assert 0 < (entry.meta or {}).get("last_message_id", 0) < 10_000_000
    await engine.dispose()


def test_legacy_last_marker_is_still_understood():
    """Записи из старых версий (метка last: в keywords) не должны потерять прогресс."""
    from types import SimpleNamespace

    from backend.main import _summary_last_id

    legacy = SimpleNamespace(meta=None, keywords=["__auto__", "last:777"])
    assert _summary_last_id(legacy) == 777
    modern = SimpleNamespace(meta={"last_message_id": 42}, keywords=["__auto__"])
    assert _summary_last_id(modern) == 42
    assert _summary_last_id(None) == 0
    broken = SimpleNamespace(meta={"last_message_id": "мусор"}, keywords=[])
    assert _summary_last_id(broken) == 0
