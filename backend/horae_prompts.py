"""
Промпты Horae: тексты по умолчанию, сборка правил тегов и служебных запросов,
разбор ответов служебных запросов.

Тексты по умолчанию лежат файлами в backend/horae_prompts_ru/ — это русские
промпты плагина Horae почти дословно (их выверял автор плагина, а правило
«только реальные изменения» и примеры форматов на них держатся). Свои промпты
пользователь хранит в настройках (`settings["prompts"][ключ]`), пустой —
значит по умолчанию.

Подстановки те же, что в плагине, чтобы свои промпты из SillyTavern
переносились как есть: `{{user}}`, `{{char}}`, `${sceneDescLine}`,
`${relLine}`, `${moodLine}`, `${systemPromptAddition}`, `{{context}}`,
`{{previousUserMessage}}`, `{{content}}`, `{{messages}}`, `{{events}}`,
`{{fulltext}}`, `{{count}}`.

Исправлены две ошибки плагина с русскими промптами по умолчанию:
  * пакетный скан просил у модели разделитель `===сообщение#N===`, а ответ
    резался только по `===消息#N===`/`===Message#N===` — каждый пакет кончался
    «ошибкой формата» (split_batch_response принимает все три);
  * промпт сжатия делился на «события» и «полный текст» по маркеру, которого в
    русском файле не было, и модель получала обе инструкции разом — здесь это
    два отдельных промпта (compress_events / compress_fulltext).
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

from backend.horae_settings import PROMPT_KEYS

_DIR = Path(__file__).with_name("horae_prompts_ru")


@lru_cache(maxsize=None)
def _read(name: str) -> str:
    try:
        return (_DIR / name).read_text(encoding="utf-8").replace("\r\n", "\n")
    except OSError:
        return ""


def default(key: str) -> str:
    """Промпт по умолчанию по ключу (см. horae_settings.PROMPT_KEYS)."""
    if key not in PROMPT_KEYS:
        return ""
    return _read(f"{key}.txt")


def defaults() -> dict[str, str]:
    """Все промпты по умолчанию — для вкладки «Промпты» (кнопка «вернуть»)."""
    return {key: default(key) for key in PROMPT_KEYS}


# Встроенные наборы промптов. «Расширенные события» — пресет плагина
# vector-summary: события по 150–280 символов с полным «кто/что/кому/что
# изменилось». Такие события богаче для свёрток и поиска, но дороже по выводу.
def builtin_presets() -> list[dict]:
    extended = {k: _read(f"presets/extended/{k}.txt") for k in ("system", "analysis", "batch")}
    return [
        {"id": "default", "name": "По умолчанию (компактные события)", "builtin": True,
         "prompts": {}},
        {"id": "extended", "name": "Расширенные события (для свёрток и поиска)", "builtin": True,
         "prompts": {k: v for k, v in extended.items() if v}},
    ]


def get(settings: dict | None, key: str) -> str:
    """Действующий текст промпта: свой из настроек или по умолчанию."""
    custom = ((settings or {}).get("prompts") or {}).get(key)
    if isinstance(custom, str) and custom.strip():
        return custom.replace("\r\n", "\n")
    return default(key)


_USER_RE = re.compile(r"\{\{user\}\}", re.IGNORECASE)
_CHAR_RE = re.compile(r"\{\{char\}\}", re.IGNORECASE)


def fill(template: str, *, user: str = "", char: str = "", **values) -> str:
    """
    Подставить значения в шаблон. `values` — ключи без скобок: подставляются и
    как `{{ключ}}`, и как `${ключ}` (в плагине встречаются оба вида).
    Замена — одним проходом по каждому ключу, текст значения повторно не
    просматривается: иначе `{{user}}` внутри реплики игрока подменился бы тоже.
    """
    text = template or ""
    for key, value in values.items():
        text = text.replace("{{" + key + "}}", str(value)).replace("${" + key + "}", str(value))
    text = _USER_RE.sub(lambda _m: user or "Пользователь", text)
    text = _CHAR_RE.sub(lambda _m: char or "Персонаж", text)
    return text


def field_lines(settings: dict) -> dict[str, str]:
    """
    Дополнительные строки формата <horae> — только для включённых модулей:
    незачем учить модель полю, которое никуда не пойдёт (и тратить на него
    вывод каждый ход).
    """
    s = settings or {}
    return {
        "sceneDescLine": (
            "\nscene_desc:постоянные физические особенности места (писать только при "
            "первом посещении или необратимом изменении)"
        ) if s.get("send_location_memory") else "",
        "relLine": (
            "\nrel:Персонаж A>Персонаж B=тип отношений|примечание (писать только при "
            "появлении или изменении отношений)"
        ) if s.get("send_relationships") else "",
        "moodLine": (
            "\nmood:имя персонажа=эмоция/состояние (писать только при явных эмоциональных "
            "изменениях присутствующих)"
        ) if s.get("send_mood") else "",
    }


def rules_prompt(settings: dict, *, user: str, char: str, tables_suffix: str | None = None,
                 rpg_prompt: str = "", calendar_line: str = "") -> str:
    """
    Правила тегов Horae для системного промпта (порт generateSystemPromptAddition).

    :param tables_suffix: None — таблиц нет (правило таблиц не нужно); строка —
        есть таблицы, и это размер первой из них с примером (horae_tables.rules_suffix).
    :param rpg_prompt: готовый текст правил RPG (horae_rpg.fill_rpg_prompt) или "".
    :param calendar_line: строка про свой календарь (horae_time.calendar_prompt) или "".
    """
    s = settings or {}
    subs = ""
    if s.get("send_location_memory"):
        subs += "\n" + get(s, "location")
    if tables_suffix is not None:
        subs += "\n" + get(s, "tables") + tables_suffix
    if s.get("send_relationships"):
        subs += "\n" + get(s, "relationship")
    if s.get("send_mood"):
        subs += "\n" + get(s, "mood")
    if s.get("rpg_enabled") and rpg_prompt.strip():
        subs += "\n" + rpg_prompt.strip()
    if s.get("anti_paraphrase"):
        subs += "\n" + get(s, "anti_paraphrase")
    if calendar_line:
        subs += "\n" + calendar_line
    template = get(s, "system")
    lines = field_lines(s)
    if "${systemPromptAddition}" in template:
        text = fill(template, user=user, char=char, systemPromptAddition=subs, **lines)
    else:
        # Свой промпт без места для дополнений — дописываем их в конец, как плагин.
        text = fill(template, user=user, char=char, **lines) + fill(subs, user=user, char=char)
    return re.sub(r"\n{4,}", "\n\n\n", text).strip()


def reminder(settings: dict, *, user: str = "", char: str = "") -> str:
    """Короткое напоминание формата для хвоста промпта (см. дизайн §8)."""
    return fill(get(settings, "reminder"), user=user, char=char).strip()


# ---------------------------------------------------------------------------
# Служебные запросы
# ---------------------------------------------------------------------------

# Системная роль служебных запросов. Без неё модель отвечала «в образе»
# персонажа из истории или продолжала сюжет вместо разбора.
EXTRACTOR_SYSTEM = (
    "Ты — строгий движок извлечения сведений из текста ролевой истории. Ты не пишешь "
    "художественный текст и не продолжаешь сюжет: только разбираешь данный фрагмент "
    "и отвечаешь строго в запрошенном формате. Не выдумывай того, чего нет в тексте."
)

SUMMARIZER_SYSTEM = (
    "Ты — редактор хроники ролевой истории. Ты сжимаешь уже случившиеся события в "
    "плотный объективный пересказ, сохраняя имена, даты, числа, предметы, обещания и "
    "изменения отношений. Сюжет не продолжаешь, оценок не добавляешь."
)


def analysis_prompt(settings: dict, *, context: str, prev_user: str, content: str,
                    user: str, char: str) -> str:
    """Промпт ИИ-анализа одного ответа (волшебная палочка плагина)."""
    text = fill(
        get(settings, "analysis"), user=user, char=char,
        context=context or "(нет данных)", previousUserMessage=prev_user or "(нет)",
        content=content, **field_lines(settings),
    )
    if (settings or {}).get("anti_paraphrase"):
        text += (
            "\n\n【Обязательно】Действия пользователя описаны в его сообщении выше и в ответ "
            "не пересказываются: изменения предметов, мест, событий и NPC из действий "
            "пользователя тоже включите в <horae>/<horaeevent>."
        )
    return text


# Необязательные поля пакетного скана (галочки диалога скана). Промпт по
# умолчанию просит только время, предметы и события — остальное дорого и
# шумно на старой истории; включённые поля дописываются отдельным разделом.
_BATCH_EXTRAS = {
    "scene": "location:текущее место\ncharacters:присутствующие через запятую",
    "npc": "npc:имя|внешность=характер@отношение к {{user}}~gender:…~age:…~race:…~job:…",
    "affection": "affection:имя=число расположения к {{user}} (0-100)",
    "relationships": "rel:Персонаж A>Персонаж B=тип отношений|примечание",
}


def batch_prompt(settings: dict, messages: list[tuple[int, str]], *, include: dict | None,
                 user: str, char: str) -> str:
    """
    Промпт пакетного скана. Разделитель сообщения — `===сообщение#<id>===`,
    где id — настоящий id сообщения в БД: по нему ответ раскладывается обратно
    без сопоставления позиций.
    """
    block = "\n\n".join(f"===сообщение#{mid}===\n{text.strip()}" for mid, text in messages)
    text = fill(get(settings, "batch"), user=user, char=char, messages=block)
    if "{{messages}}" not in get(settings, "batch"):
        text += "\n\n" + block
    extras = [line for key, line in _BATCH_EXTRAS.items() if (include or {}).get(key)]
    if extras:
        text += (
            "\n\n【Дополнительно】Внутри <horae> каждого сообщения также укажите, если есть "
            "в тексте (иначе не пишите):\n" + fill("\n".join(extras), user=user, char=char)
        )
    if (settings or {}).get("anti_paraphrase"):
        text += (
            "\n\n【Режим без пересказа】Блок сообщения может содержать действие пользователя "
            "([ДЕЙСТВИЕ ПОЛЬЗОВАТЕЛЯ]) и ответ ([ОТВЕТ]): учитывайте изменения из обоих."
        )
    return text


_BATCH_SPLIT_RE = re.compile(
    r"={2,}\s*(?:сообщение|message|msg|消息)\s*#\s*(\d+)\s*={2,}", re.IGNORECASE
)
_THINK_RE = re.compile(r"<think(?:ing)?\b[\s\S]*?</think(?:ing)?>", re.IGNORECASE)


def split_batch_response(text: str) -> dict[int, str]:
    """Ответ пакетного скана → {id сообщения: кусок ответа с его тегами}."""
    body = _THINK_RE.sub("", text or "")
    parts = _BATCH_SPLIT_RE.split(body)
    out: dict[int, str] = {}
    # split с группой: [до, id1, кусок1, id2, кусок2, …]
    for i in range(1, len(parts) - 1, 2):
        try:
            mid = int(parts[i])
        except ValueError:
            continue
        chunk = parts[i + 1].strip()
        if chunk:
            out[mid] = chunk
    return out


def summary_prompt(settings: dict, *, events: str, fulltext: str, count: int, user: str,
                   source: str = "fulltext") -> str:
    """Промпт авто-свёртки хронологии."""
    template = get(settings, "auto_summary")
    text = fill(template, user=user, events=events, fulltext=fulltext, count=count)
    if source == "fulltext" and fulltext and "{{fulltext}}" not in template:
        # У плагина здесь был китайский заголовок «【全文对话记录】» в русском промпте.
        text += "\n\n【Полный текст фрагмента】\n" + fulltext
    return text


def resummary_prompt(settings: dict, *, records: str, count: int, user: str) -> str:
    """Промпт свёртки свёрток (уровень выше)."""
    return fill(get(settings, "auto_resummary"), user=user, events=records, fulltext="", count=count)


def compress_prompt(settings: dict, mode: str, *, events: str, fulltext: str, count: int,
                    user: str) -> str:
    """Промпт ручного сжатия выбранных событий: mode = events | fulltext."""
    key = "compress_fulltext" if mode == "fulltext" else "compress_events"
    template = get(settings, key)
    text = fill(template, user=user, events=events, fulltext=fulltext, count=count)
    if mode == "fulltext" and fulltext and "{{fulltext}}" not in template:
        text += "\n\n" + fulltext
    if mode != "fulltext" and events and "{{events}}" not in template:
        text += "\n\n" + events
    return text


class TruncatedSummary(ValueError):
    """Ответ оборван: тег свёртки открыт, но не закрыт — сохранять нечего."""


_SUMMARY_RE = re.compile(r"<horaesummary>([\s\S]*?)</horaesummary>", re.IGNORECASE)
_SUMMARY_OPEN_RE = re.compile(r"<horaesummary>", re.IGNORECASE)


def extract_summary(text: str) -> str | None:
    """
    Текст свёртки из ответа: содержимое последнего <horaesummary>…</horaesummary>.

    Открыт и не закрыт — TruncatedSummary (ответ упёрся в лимит вывода;
    обрывок свёртки в память писать нельзя — он молча потерял бы конец).
    Тега нет — None (ошибка формата), свободный текст не принимается: так в
    хронику не попадёт отказ модели или её рассуждение вслух.
    """
    body = _THINK_RE.sub("", text or "")
    found = _SUMMARY_RE.findall(body)
    if found:
        summary = found[-1].strip()
        return summary or None
    if _SUMMARY_OPEN_RE.search(body):
        raise TruncatedSummary("ответ модели оборван — свёртка не записана")
    return None


def npc_enrich_messages(name: str, aliases: list[str], snippets: list[str], user: str) -> list[dict]:
    """Запрос «ИИ-заполнение» профиля NPC по упоминаниям в истории."""
    who = name + (f" (также: {', '.join(aliases)})" if aliases else "")
    prompt = (
        f"Ниже — фрагменты ролевой истории, где упоминается персонаж {who}. "
        f"Собеседник пользователя — {user or 'Пользователь'}.\n\n"
        + "\n\n".join(snippets)
        + "\n\nЗаполни профиль этого персонажа ТОЛЬКО по фактам из фрагментов. Ответь строго "
        "одним JSON-объектом без пояснений:\n"
        '{"appearance": "", "personality": "", "relationship": "", "age": "", "gender": ""}\n'
        "Каждое поле — 1–2 коротких предложения по-русски; relationship — отношение к "
        f"{user or 'пользователю'} с объектом («подруга детства {user or 'пользователя'}»); "
        "неизвестное — пустая строка. Ничего не выдумывай."
    )
    return [
        {"role": "system", "content": EXTRACTOR_SYSTEM},
        {"role": "user", "content": prompt},
    ]


_JSON_OBJ_RE = re.compile(r"\{[\s\S]*\}")


def parse_npc_enrich(text: str) -> dict:
    """Поля профиля из ответа «ИИ-заполнение» (пустой словарь — не разобралось)."""
    body = _THINK_RE.sub("", text or "")
    body = re.sub(r"```(?:json)?", "", body)
    match = _JSON_OBJ_RE.search(body)
    if not match:
        return {}
    try:
        data = json.loads(match.group(0))
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    out = {}
    for key in ("appearance", "personality", "relationship", "age", "gender"):
        value = data.get(key)
        if isinstance(value, (str, int, float)) and str(value).strip():
            out[key] = str(value).strip()[:500]
    return out


def query_rewrite_messages(settings: dict, dialogue: str) -> list[dict]:
    """Запрос переписывания поискового запроса (INTENT + до 5 × Q)."""
    return [
        {"role": "system", "content": get(settings, "query_rewrite")},
        {"role": "user", "content": "Фрагмент диалога:\n\n" + dialogue},
    ]


_INTENT_RE = re.compile(r"^\s*INTENT\s*[:：]\s*(.+)$", re.IGNORECASE | re.MULTILINE)
_Q_RE = re.compile(r"^\s*Q\s*[:：]\s*(.+)$", re.IGNORECASE | re.MULTILINE)


def parse_query_rewrite(text: str) -> tuple[str, list[str]]:
    """Ответ переписывания → (замысел сцены, до пяти запросов без повторов)."""
    body = _THINK_RE.sub("", text or "")
    intent_m = _INTENT_RE.search(body)
    intent = intent_m.group(1).strip() if intent_m else ""
    queries: list[str] = []
    seen = set()
    for q in _Q_RE.findall(body):
        key = re.sub(r"\s+", "", q).lower()
        if q.strip() and key not in seen:
            seen.add(key)
            queries.append(q.strip())
        if len(queries) >= 5:
            break
    return intent, queries
