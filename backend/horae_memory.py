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
from functools import lru_cache


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


_ENCODER = None
_ENCODER_TRIED = False


def _encoder():
    """Токенизатор tiktoken (приезжает зависимостью litellm). None, если недоступен."""
    global _ENCODER, _ENCODER_TRIED
    if not _ENCODER_TRIED:
        _ENCODER_TRIED = True
        try:
            import tiktoken
            _ENCODER = tiktoken.get_encoding("cl100k_base")
        except Exception:  # noqa: BLE001 — без токенизатора просто считаем грубее
            _ENCODER = None
    return _ENCODER


def _tokens_heuristic(text: str) -> int:
    """
    Запасная оценка, если tiktoken недоступен.

    Правило «4 символа на токен» верно только для латиницы. Кириллица, греческий,
    CJK и эмодзи в BPE-словарях режутся мелко — там ближе к 2 символам на токен.
    Поэтому считаем «тяжёлые» символы отдельно от «лёгких».
    """
    heavy = sum(1 for ch in text if ord(ch) > 0x02FF)
    return max(1, int(heavy / 2 + (len(text) - heavy) / 4))


@lru_cache(maxsize=4096)
def estimate_tokens(text: str) -> int:
    """
    Сколько токенов займёт текст.

    ПОЧЕМУ НЕ len/4: прежняя оценка «4 символа на токен» — правило для английского.
    На русском она занижает объём ПОЧТИ ВДВОЕ (замеры на реальных чатах: 47 706
    против 93 912 и 22 016 против 42 511 — коэффициент 1.93–1.97). Последствия были
    неприятные: окно контекста показывало не то, что уходит на самом деле, история
    почти никогда не обрезалась (чат на 174 сообщения влезал «целиком»), а расход
    оказывался вдвое больше ожидаемого. Длинный контекст ещё и топит инструкцию
    пользователя — модель переставала слышать, чего от неё хотят.

    Результат кэшируется: история пересчитывается на КАЖДОМ ходу, а сообщения в ней
    не меняются, поэтому второй и последующие разы обходятся бесплатно.
    """
    enc = _encoder()
    if enc is None:
        return _tokens_heuristic(text)
    # disallowed_special=() — иначе tiktoken падает, если пользователь напишет
    # в чате служебную последовательность вида <|endoftext|>.
    return max(1, len(enc.encode(text, disallowed_special=())))


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


# Историю обрезаем «ступенями» по столько сообщений за раз.
#
# Зачем: провайдер даёт скидку 75–90% за НАЧАЛО запроса, совпадающее с прошлым
# запросом байт в байт (кэш промпта). Если резать историю ровно по границе
# бюджета, то каждый новый ход выталкивает самое старое сообщение — начало
# контекста сдвигается, и кэш промахивается КАЖДЫЙ раз. Поэтому граница обрезки
# «залипает» на индексе, кратном ступени, и стоит на месте целую ступень ходов.
_TRIM_STEP = 16


def stable_trim_start(costs: list[int], budget: int, reserved: int = 0) -> int:
    """
    С какого индекса истории начинать, чтобы уложиться в бюджет токенов.

    :param costs: стоимость каждого сообщения истории (в порядке от старых к новым).
    :param reserved: «несжимаемая» часть бюджета (системный промпт, текущая реплика).
    :return: 0, если влезает всё; иначе индекс начала, ОКРУГЛЁННЫЙ ВВЕРХ до
        кратного _TRIM_STEP.

    Что это даёт: индекс привязан к абсолютной нумерации сообщений, поэтому новые
    реплики в конце его не двигают — он «прыгает» только раз в _TRIM_STEP ходов,
    когда история дорастает до следующей ступени. То есть кэш промпта промахивается
    примерно на одном ходу из шестнадцати вместо КАЖДОГО хода.
    """
    if not budget or budget <= 0:
        return 0
    remaining = reserved + sum(costs)
    if remaining <= budget:
        return 0
    start = 0
    while start < len(costs) and remaining > budget:
        remaining -= costs[start]
        start += 1
    stepped = ((start + _TRIM_STEP - 1) // _TRIM_STEP) * _TRIM_STEP
    # Если округление вверх съедает вообще всё (даже хвост не влезает в бюджет) —
    # ступень не применяем, иначе контекст остался бы пустым.
    return stepped if stepped < len(costs) else start


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


async def messages_to_history_db(
    db, msgs, files_limit_chars: int | None = None, files_turns: int | None = None
) -> list[dict]:
    """
    То же, что messages_to_history, но данные вложений подтягиваются из
    blob-таблицы ТОЧЕЧНО и только когда нужны.

    :param files_limit_chars: лимит файлов истории в символах base64;
        None — БЕЗ лимита по объёму.
    :param files_turns: возрастное окно — файлы несут только N последних
        сообщений; None/0 — без ограничения по возрасту.
    """
    from backend.attachments import load_history_attachments

    att_map = await load_history_attachments(db, msgs, files_limit_chars, files_turns)
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


import re as _re

# Слова текста (юникод, кириллица тоже); цифры и подчёркивания не считаем словами.
_WORD_RE = _re.compile(r"[^\W\d_]+", _re.UNICODE)


def _text_tokens(text: str) -> list[str]:
    return _WORD_RE.findall((text or "").lower())


# Окончания, которые отпадают при склонении: «король» → «королЕ», «конь» → «конЯ».
# Срезаем РОВНО ОДНУ такую букву — этого хватает для большинства падежей и почти
# не даёт ложных склеек. Более агрессивная обрезка начала бы путать разные слова.
_FLEXION_TAIL = "аеёиоуыэюяьйъ"


def _stem(word: str) -> str:
    """Грубая основа слова: слово без одного окончания-гласной или мягкого знака."""
    if len(word) >= 4 and word[-1] in _FLEXION_TAIL:
        return word[:-1]
    return word


# Падежные окончания, которые могут ПРИРАСТИ к ключу: «меч» → «мечом», «мечами».
# Именно список окончаний, а не «любой хвост до N букв»: иначе «рука» цепляла бы
# «рукав», «король» — «корольков», а «кот» — «котёл». Слово с посторонним хвостом
# («-ов», «-ниц», «-азин») теперь не проходит.
_CASE_ENDINGS = frozenset({
    "а", "я", "у", "ю", "ы", "и", "е", "ё", "о", "й", "ь",
    "ой", "ей", "ом", "ем", "ём", "ов", "ев", "ах", "ях", "ам", "ям", "ью",
    "ии", "ие", "ия", "ый", "ая", "ое", "ые", "ем", "ух",
    "ами", "ями", "ому", "ему", "ого", "его", "ыми", "ими", "ов", "ей",
})


def keyword_hits(keyword: str, text_low: str, tokens: list[str]) -> bool:
    """
    Сработало ли ключевое слово World Info по тексту.

    Раньше здесь была голая проверка подстроки — и запись с ключом «кот»
    активировалась на слове «который», «мир» — на «мирный», «сон» — на «Сонечку».
    Лор подмешивался невпопад, и память выглядела сломанной. Теперь:

      * «фраза из слов»  — ищется как подстрока (пробел = явное намерение);
      * «ключ*»          — любое слово, начинающееся на «ключ» (для сложных
                           склонений: «замк*» поймает и «замка», и «замком»);
      * «ключ»           — слово целиком, его склонение («меч» → «мечи»,
                           «король» → «короле») — но НЕ другое слово с тем же
                           началом («кот» → «который», «мир» → «мирный»).
    """
    kw = (keyword or "").strip().lower()
    if not kw:
        return False
    # Фраза — намеренная подстрока: «тёмный лес», «Джон Смит».
    if " " in kw:
        return kw in text_low
    # Явный шаблон: пользователь сам разрешил широкое совпадение.
    if kw.endswith("*"):
        prefix = kw[:-1]
        return bool(prefix) and any(t.startswith(prefix) for t in tokens)
    kw_stem = _stem(kw)
    for t in tokens:
        if t == kw:
            return True
        # Окончание отпало у слова и/или у ключа: «король» ↔ «короле».
        if _stem(t) == kw_stem:
            return True
        # Окончание приросло к основе: «меч» → «мечом», «король» → «королём».
        # Сравниваем именно с ОСНОВОЙ ключа (у «король» это «корол»), иначе
        # варианты со сменой последней буквы не ловятся. Принимаем только
        # НАСТОЯЩИЕ падежные окончания, иначе «рука» снова зацепит «рукав».
        if t.startswith(kw_stem) and t[len(kw_stem):] in _CASE_ENDINGS:
            return True
    return False


# Сколько ПОСЛЕДНИХ сообщений просматриваем в поисках ключевых слов.
_TRIGGER_WINDOW = 6


def _plain_text(content) -> str:
    """
    Текст сообщения для поиска триггеров — включая МУЛЬТИМОДАЛЬНЫЕ реплики.

    Раньше окно сканирования брало только сообщения со строковым content, а
    реплика с вложением (content = список блоков) пропускалась ЦЕЛИКОМ вместе со
    своим текстом. То есть стоило приложить фото — и ключевые слова из этой
    реплики переставали активировать память. Со стороны это выглядело как
    «Horae срабатывает через раз».
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            b.get("text", "") for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


# Сколько активированных по ключевым словам записей пускаем в один запрос.
# Без потолка полсотни сработавших записей уезжали в системный промпт целиком —
# и раздували каждый ход, и топили важное в неважном. always_on-записи под лимит
# НЕ попадают: пользователь пометил их как «всегда», это его явное решение.
_MAX_KEYWORD_RECORDS = 24


def _scan_text_for_triggers(
    haystack: str, records: list[HoraeRecord], max_keyword_records: int = _MAX_KEYWORD_RECORDS
) -> list[HoraeRecord]:
    """
    Возвращает записи, которые нужно активировать:
      * always_on (если enabled) — всегда;
      * keyword-записи — если сработало хотя бы одно ключевое слово (см.
        keyword_hits), но не больше max_keyword_records штук — самые
        приоритетные.
    Результат сортируется по priority (по убыванию): важное идёт первым.
    """
    haystack_low = (haystack or "").lower()
    tokens = _text_tokens(haystack)
    always: list[HoraeRecord] = []
    by_keyword: list[HoraeRecord] = []

    for rec in records:
        if not rec.enabled:
            continue
        if rec.always_on:
            always.append(rec)
            continue
        if any(keyword_hits(kw, haystack_low, tokens) for kw in rec.keywords):
            by_keyword.append(rec)

    by_keyword.sort(key=lambda r: r.priority, reverse=True)
    if max_keyword_records and max_keyword_records > 0:
        by_keyword = by_keyword[:max_keyword_records]

    activated = always + by_keyword
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


def knowledge_block(knowledge_text: str, knowledge_media: list | None) -> list[dict]:
    """
    Блок базы знаний для контекста, ЯВНО отделённый от диалога.

    Кладётся В НАЧАЛО (после системного промпта, до истории) — НИЗКАЯ «свежесть»:
    иначе справочник перетягивает внимание, и модель «сканирует» его вместо того,
    чтобы помнить сам диалог и последнюю реплику пользователя. База знаний — это
    ПОДСОБНЫЙ материал (загляни, если по делу), а не то, что происходит в чате.
    """
    if not (knowledge_text or knowledge_media):
        return []
    out: list[dict] = [{"role": "system", "content": (
        "===== СПРАВОЧНАЯ БАЗА ЗНАНИЙ (НЕ часть диалога) =====\n"
        "Ниже — подсобные материалы чата. Это СПРАВОЧНИК: заглядывай в него ТОЛЬКО "
        "если вопрос напрямую касается его содержимого. Он НЕ описывает то, что "
        "происходит в ролевой прямо сейчас, и НЕ заменяет диалог."
    )}]
    if knowledge_text:
        out.append({"role": "system", "content": knowledge_text})
    if knowledge_media:
        out.extend(knowledge_media)
    out.append({"role": "system", "content": (
        "===== КОНЕЦ БАЗЫ ЗНАНИЙ =====\n"
        "Дальше идёт САМ ДИАЛОГ: то, что реально писали пользователь и персонажи. "
        "Держи в голове именно его и последнюю реплику пользователя; базу знаний "
        "используй лишь как справку, не позволяй ей вытеснять факты из чата."
    )})
    return out


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


# ==================== РЕЖИМ БЕЗ ОТЫГРЫША (OOC / ассистент) ====================
# Зачем: обычный BEHAVIOR_GUIDE требует «оставайся в образе и не давай мета-
# комментариев», а якорь роли переинъектируется в САМУЮ сильную позицию — прямо
# перед репликой пользователя. Из-за этого прикладная просьба («напиши пост»,
# «разбери этот код») тонула: модель отвечала В ОБРАЗЕ вместо выполнения задачи,
# и чем длиннее ролевая история, тем сильнее она перевешивала одну инструкцию.
ASSISTANT_GUIDE = (
    "[Как отвечать] ОТЫГРЫШ СЕЙЧАС ВЫКЛЮЧЕН. Пользователь обращается не к персонажу, "
    "а к тебе напрямую, как к ассистенту, и ждёт выполнения конкретной задачи. "
    "НЕ говори от лица персонажа, не описывай его действия и эмоции, не веди сцену. "
    "Просто сделай то, о чём просят, и дай результат.\n"
    "[Точность] Не придумывай факты. Если данных не хватает — спроси или скажи прямо, "
    "чего не хватает, вместо того чтобы фантазировать.\n"
    "[Контекст] Диалог выше — справочная информация: обращайся к нему, если задача "
    "касается его содержимого, но отвечать в его стиле не нужно."
)

ASSISTANT_STYLE_GUIDE = (
    "[Оформление ответа] Отвечай по существу, без ролевой прозы. Разметку используй "
    "по делу: **жирный** для акцентов, `моноширинный` и блоки кода в тройных кавычках "
    "для технического текста, «- » для списков. Не используй HTML-разметку."
)

# Пометки, которыми пользователь помечает реплику «вне роли». Скобки — конвенция
# SillyTavern, косая черта — привычный вид команды.
_OOC_PREFIXES = ("/ooc ", "/ooc\n", "//")


def _replace_first_text(content, old: str, new: str):
    """
    Меняет текст в мультимодальном контенте (список блоков), не трогая вложения.

    Нужно потому, что при сообщении с файлами текст лежит ПЕРВЫМ блоком списка, и
    снять пометку «вне роли» только в user_message было бы мало — до модели она
    доехала бы вторым путём, внутри блоков.
    """
    if not isinstance(content, list):
        return content
    out = []
    done = False
    for b in content:
        if not done and isinstance(b, dict) and b.get("type") == "text" and b.get("text") == old:
            out.append({**b, "text": new})
            done = True
        else:
            out.append(b)
    return out


def detect_ooc(text: str) -> tuple[bool, str]:
    """
    Помечена ли реплика как «вне роли», и текст без пометки.

    Понимаем два вида:
        ((текст))     — обёрнуто ЦЕЛИКОМ (внутри сообщения такие скобки не трогаем,
                        иначе сломали бы обычную ролевую ремарку в середине фразы);
        /ooc текст    — команда в начале, а также сокращение //текст.

    :return: (это_вне_роли, текст_без_пометки). Если пометки нет — (False, исходный).
    """
    stripped = (text or "").strip()
    if not stripped:
        return False, text
    if stripped.startswith("((") and stripped.endswith("))") and len(stripped) > 4:
        inner = stripped[2:-2].strip()
        if inner:
            return True, inner
    low = stripped.lower()
    for pref in _OOC_PREFIXES:
        if low.startswith(pref):
            inner = stripped[len(pref):].strip()
            if inner:
                return True, inner
    return False, text


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
    knowledge_text: str = "",
    knowledge_media: list | None = None,
    global_instructions: str = "",
    ooc: bool = False,
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
    :param ooc: реплика «вне роли» — пользователь обращается к ассистенту, а не к
        персонажу. Снимает требование держать образ и убирает якорь роли из конца,
        иначе прикладная просьба проигрывает ролевой инструкции (см. ASSISTANT_GUIDE).
    """
    # 1. Текст, по которому ищем триггеры памяти: текущее сообщение + хвост истории.
    recent_text = user_message + "\n" + "\n".join(
        _plain_text(m.get("content")) for m in history[-_TRIGGER_WINDOW:]
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
        ASSISTANT_GUIDE if ooc else BEHAVIOR_GUIDE,
        ASSISTANT_STYLE_GUIDE if ooc else STYLE_GUIDE,
    ]
    system_prompt = "\n\n".join(p for p in system_parts if p)

    # 3. «Несжимаемый» бюджет: системный промпт + текущее сообщение пользователя.
    used = estimate_tokens(system_prompt) + estimate_tokens(user_message)

    # 4. Обрезаем историю под бюджет: свежие сообщения важнее старых.
    #
    # ЭКОНОМИЯ: граница обрезки квантуется по ступеням (см. stable_trim_start) —
    # тогда начало запроса не меняется от хода к ходу и попадает в кэш промпта
    # провайдера со скидкой 75–90%. Что выпало — держит авто-сводка сюжета.
    costs = [estimate_content_tokens(m.get("content")) for m in history]
    start = stable_trim_start(costs, token_budget, reserved=used)
    trimmed_history: list[dict] = [
        {"role": m["role"], "content": m["content"]} for m in history[start:]
    ]
    used += sum(costs[start:])

    # 5. Финальная сборка messages.
    messages: list[dict] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    # Аватары: показываем нейросети внешность персонажа и пользователя (если включено).
    if send_avatars:
        messages.extend(_avatar_messages(character, character_avatar, persona_avatar))

    # База знаний — ДО истории и явно ОТДЕЛЕНА от диалога (см. knowledge_block).
    messages.extend(knowledge_block(knowledge_text, knowledge_media))

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

    # Якорь характера — чтобы личность не «плыла» в длинном окне. В режиме «вне роли»
    # его НЕ добавляем: он стоит в сильнейшей позиции и перебивал бы прямую задачу.
    if not ooc:
        tail.append({"role": "system", "content": _render_char_anchor(character)})

    # Post-History Instructions (jailbreak/UJB) — САМЫЙ конец: максимальное влияние.
    if post_history_instructions and post_history_instructions.strip():
        tail.append({"role": "system", "content": post_history_instructions.strip()})

    # Глобальные инструкции обхода: то же место, но общее для ВСЕХ персонажей —
    # чтобы один и тот же текст не приходилось дублировать в каждой карточке.
    # Идут ПОСЛЕ инструкций персонажа: общее правило важнее частного.
    if global_instructions and global_instructions.strip():
        tail.append({"role": "system", "content": global_instructions.strip()})

    # Фокус на текущем ходе: в огромном контексте (вся история + все файлы) модель
    # может «утопить» свежую реплику и начать выдумывать то, что уже прислано
    # (например, сочинять текст песни, которая ЕСТЬ в сообщении). Явно велим
    # опираться на само сообщение и приложенные к нему материалы.
    has_current_media = isinstance(user_attachments_content, list) and any(
        isinstance(b, dict) and b.get("type") in ("image_url", "input_audio")
        for b in user_attachments_content
    )
    if ooc:
        focus = (
            "[Выполни эту задачу] Ниже — прямая просьба пользователя, обращённая к тебе "
            "как к ассистенту, а НЕ реплика в ролевой сцене. Прочитай её целиком и сделай "
            "ровно то, о чём просят. Не отвечай от лица персонажа и не переводи разговор "
            "в отыгрыш. Если в просьбе уже есть нужный текст или данные — используй их "
            "дословно, не подменяя выдумкой."
        )
    elif has_current_media:
        focus = (
            "[Отвечай на это сообщение] Ниже — АКТУАЛЬНАЯ реплика пользователя и "
            "ПРИЛОЖЕННЫЕ ИМЕННО К НЕЙ файлы. Анализируй их напрямую и целиком: смотри, "
            "что/кто РЕАЛЬНО в этом видео/на фото и что звучит в этом аудио. НЕ переноси "
            "сюда персонажей, имена или выводы из ранее присланных файлов и не выдумывай — "
            "опирайся только на то, что действительно видишь и слышишь в этих свежих файлах."
        )
    else:
        focus = (
            "[Отвечай на это сообщение] Ниже — АКТУАЛЬНАЯ реплика пользователя. Внимательно "
            "прочитай весь её текст и отвечай именно по нему. Если нужный текст или данные "
            "УЖЕ есть в сообщении — используй их дословно, НЕ придумывай и не заменяй выдумкой."
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
    history_files_turns: int | None = None,
    knowledge_chars: int | None = None,
    global_instructions: str = "",
    assistant_mode: bool = False,
) -> list[dict]:
    """
    Достаёт из БД память Horae, персону, заметку автора и историю сообщений,
    после чего вызывает чистую assemble_context().

    :param assistant_mode: постоянный тумблер «без отыгрыша» из интерфейса. Работает
        вместе с разовой пометкой ((…)) / /ooc в самом сообщении — сработает любое
        из двух. Разбор пометки живёт ЗДЕСЬ, а не в обработчике веб-запроса, чтобы
        режим одинаково действовал во всех путях: чат, Telegram, регенерация, retry.

    :param session: ORM-объект ChatSession (нужны его id, persona_id, author_note).
    :param history: если None — берём всю историю сессии из БД. Можно передать свою
        (например, для «регенерации» — историю БЕЗ последнего ответа ассистента).

    ВАЖНО: при обычном ходе вызывать ДО сохранения нового сообщения пользователя,
    иначе оно задвоится в истории.
    """
    from sqlalchemy import select

    from backend.models import Message

    # Пометка «вне роли» в самой реплике. Из текста её убираем: модели она ничего
    # не говорит, а вот в сохранённом сообщении остаётся — пользователь видит в
    # истории, что этот ход шёл без отыгрыша.
    marked_ooc, clean_message = detect_ooc(user_message)
    ooc = bool(assistant_mode or marked_ooc)
    if marked_ooc and clean_message != user_message:
        attachments_content = _replace_first_text(
            attachments_content, user_message, clean_message
        )
        user_message = clean_message

    records = await _load_horae_records(
        session_db, session.id, getattr(character, "id", None)
    )
    persona, author_note = await _load_persona_and_note(session_db, session)
    # База знаний чата (справочные файлы) — доступна модели в каждом ходе.
    from backend.knowledge import build_knowledge

    knowledge_text, knowledge_media = await build_knowledge(
        session_db, session.id, knowledge_chars
    )
    # Глобальные инструкции обхода (общие для всех персонажей) — грузим здесь,
    # чтобы они применялись во ВСЕХ режимах: веб, Telegram, регенерация, retry.
    if not global_instructions:
        from backend.censorship import load_global_instructions

        global_instructions = await load_global_instructions(session_db)

    if history is None:
        hq = (
            select(Message)
            .where(Message.session_id == session.id)
            .order_by(Message.id)
        )
        msgs = (await session_db.execute(hq)).scalars().all()
        # СОХРАНЯЕМ вложения истории — данные тянутся из blob-таблицы точечно,
        # в пределах лимита по объёму И возрастного окна (см. load_history_attachments).
        history = await messages_to_history_db(
            session_db, msgs, history_files_limit, history_files_turns
        )

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
        knowledge_text=knowledge_text,
        knowledge_media=knowledge_media,
        global_instructions=global_instructions,
        ooc=ooc,
    )
