"""
Настройки слоя Horae (перенос плагина Horae из SillyTavern).

Три уровня, каждый следующий важнее:

    DEFAULTS  ←  глобальные (AppSetting["horae"])
              ←  профиль персонажа (Character.horae_profile["settings"])
              ←  переопределения чата (HoraeChatState.data["settings"])

Зачем уровни: в плагине настройки были одни на всё, а «профиль карточки»
приходилось вручную подгружать при смене персонажа. Здесь один чат может
отыгрывать фэнтези с RPG и календарём, а другой — рабочую переписку без
тегов, и ничего не нужно переключать руками.

Модуль чистый (stdlib): ни БД, ни FastAPI. Любой уровень может прийти
битым (руками правленый JSON, импорт профиля) — `sanitize` молча выбрасывает
неизвестные ключи и зажимает числа, а не роняет ход.
"""
from __future__ import annotations

import copy

# Ключи своих промптов (settings["prompts"]). Пустой или отсутствующий —
# промпт по умолчанию (horae_prompts.DEFAULTS).
PROMPT_KEYS = (
    "system", "analysis", "batch", "compress_events", "compress_fulltext",
    "auto_summary", "auto_resummary", "tables", "location", "relationship",
    "mood", "rpg", "anti_paraphrase", "query_rewrite", "reminder",
)

RPG_USER_ONLY_MODULES = ("bars", "skills", "attrs", "reputation", "equipment", "level", "currency")

_BOOL = {
    "enabled": True,
    "parse_tags": True,
    "inject_state": True,
    "tag_reminder": True,
    "anti_paraphrase": False,
    "send_timeline": True,
    "send_characters": True,
    "send_affection": True,
    "send_main_personality": True,
    "send_items": True,
    "send_agenda": True,
    "send_location_memory": False,
    "send_relationships": False,
    "send_mood": False,
    "summary_enabled": False,
    "summary_hides": True,
    "recall_enabled": True,
    "recall_pure": False,
    "recall_rerank": False,
    "recall_query_rewrite": False,
    "rpg_enabled": False,
    "rpg_strict_present": False,
    "rpg_bars": True,
    "rpg_skills": True,
    "rpg_attrs": True,
    "rpg_reputation": False,
    "rpg_equipment": False,
    "rpg_level": False,
    "rpg_currency": False,
    "rpg_stronghold": False,
}

# (по умолчанию, минимум, максимум). Нижние границы — из плагина: там, где он
# зажимал значение в интерфейсе (≥3 сохраняемых ответа, ≥5 сообщений порога),
# меньшее значение ломало логику свёртки, а не просто было «экономнее».
_INT = {
    "aux_delay_ms": (1000, 0, 60000),
    "context_depth": (15, 0, 500),
    "summary_keep_recent": (5, 3, 500),
    "summary_buffer_messages": (10, 5, 1000),
    "summary_buffer_tokens": (30000, 1000, 1_000_000),
    "summary_batch_messages": (50, 5, 1000),
    "summary_batch_tokens": (80000, 10000, 1_000_000),
    "resummary_threshold": (7, 0, 100),
    "resummary_min_chars": (800, 0, 100000),
    "recall_top_k": (5, 1, 10),
    "recall_full_text_count": (3, 0, 5),
    "recall_full_text_chars": (3000, 200, 50000),
    "recall_rerank_candidates": (25, 5, 200),
}

_FLOAT = {
    "recall_threshold": (0.72, 0.3, 0.95),
    "recall_lexical_threshold": (0.3, 0.1, 0.9),
    "recall_full_text_threshold": (0.9, 0.6, 1.0),
    "recall_rerank_min_score": (0.5, 0.0, 1.0),
}

_CHOICE = {
    "rules_position": ("system", ("system", "tail")),
    # Фоновый ИИ-анализ ответа без тегов. «gaps» — только если модель вообще
    # умеет писать теги (они есть хотя бы в одном из последних ответов) и
    # просто забыла: модель, которая формат не держит, иначе удваивала бы
    # число платных запросов молча. «always» — анализировать всегда.
    "auto_analyze": ("gaps", ("off", "gaps", "always")),
    "summary_source": ("fulltext", ("fulltext", "events")),
    "summary_buffer_mode": ("messages", ("messages", "tokens")),
}

# Строки: (по умолчанию, максимальная длина).
_STR = {
    "aux_model": ("", 200),
    "strip_tags": ("", 500),
    "recall_rerank_model": ("", 200),
}

_MAX_PROMPT_CHARS = 60000
_MAX_CONFIG_ITEMS = 40


def defaults() -> dict:
    """Полный словарь настроек по умолчанию (новая копия на каждый вызов)."""
    from backend import horae_rpg

    out: dict = {}
    out.update(_BOOL)
    out.update({k: v[0] for k, v in _INT.items()})
    out.update({k: v[0] for k, v in _FLOAT.items()})
    out.update({k: v[0] for k, v in _CHOICE.items()})
    out.update({k: v[0] for k, v in _STR.items()})
    out["rpg_user_only"] = []
    out["rpg_bar_config"] = horae_rpg.default_bar_config()
    out["rpg_attr_config"] = horae_rpg.default_attr_config()
    out["calendar"] = {"enabled": False, "months": []}
    out["prompts"] = {}
    return out


ALL_KEYS = frozenset(
    list(_BOOL) + list(_INT) + list(_FLOAT) + list(_CHOICE) + list(_STR)
    + ["rpg_user_only", "rpg_bar_config", "rpg_attr_config", "calendar", "prompts"]
)


def _as_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("true", "1", "yes", "да", "on"):
            return True
        if low in ("false", "0", "no", "нет", "off"):
            return False
    return None


def _as_number(value, cast):
    if isinstance(value, bool):
        return None
    try:
        return cast(value)
    except (TypeError, ValueError):
        try:
            return cast(float(str(value).replace(",", ".")))
        except (TypeError, ValueError):
            return None


def _clean_str(value, limit: int) -> str | None:
    if value is None:
        return None
    return str(value).strip()[:limit]


def _clean_bar_config(value) -> list | None:
    if not isinstance(value, list):
        return None
    out = []
    seen = set()
    for raw in value[:_MAX_CONFIG_ITEMS]:
        if not isinstance(raw, dict):
            continue
        key = "".join(ch for ch in str(raw.get("key") or "").strip().lower() if ch.isalnum() or ch == "_")
        # Ключ шкалы уходит в формат тега «hp:Имя=80/100», поэтому только
        # латиница: так его и разбирает парсер (как в плагине).
        if not key or not key[0].isascii() or not key[0].isalpha() or key in seen:
            continue
        if key in ("status", "skill", "xp", "level", "attr", "rep", "equip", "unequip", "currency", "base"):
            continue  # служебные префиксы RPG — шкалой быть не могут
        seen.add(key)
        mx = _as_number(raw.get("max"), int)
        out.append({
            "key": key,
            "name": _clean_str(raw.get("name"), 40) or key.upper(),
            "color": _clean_str(raw.get("color"), 20) or "#8aa3ff",
            "max": max(1, min(999999, mx)) if mx else 100,
            "desc": _clean_str(raw.get("desc"), 300) or "",
        })
    return out


def _clean_attr_config(value) -> list | None:
    if not isinstance(value, list):
        return None
    out = []
    seen = set()
    for raw in value[:_MAX_CONFIG_ITEMS]:
        if not isinstance(raw, dict):
            continue
        key = "".join(ch for ch in str(raw.get("key") or "").strip().lower() if ch.isalnum() or ch == "_")
        if not key or not key.isascii() or key in seen:
            continue
        seen.add(key)
        out.append({
            "key": key,
            "name": _clean_str(raw.get("name"), 40) or key,
            "desc": _clean_str(raw.get("desc"), 300) or "",
        })
    return out


def _clean_calendar(value) -> dict | None:
    if not isinstance(value, dict):
        return None
    months = []
    for raw in (value.get("months") or [])[:60]:
        if not isinstance(raw, dict):
            continue
        name = _clean_str(raw.get("name"), 40)
        days = _as_number(raw.get("days"), int)
        if name and days and days > 0:
            months.append({"name": name, "days": min(days, 1000)})
    enabled = _as_bool(value.get("enabled"))
    return {"enabled": bool(enabled) and bool(months), "months": months}


def _clean_prompts(value) -> dict | None:
    if not isinstance(value, dict):
        return None
    out = {}
    for key, text in value.items():
        if key not in PROMPT_KEYS or text is None:
            continue
        out[key] = str(text)[:_MAX_PROMPT_CHARS]
    return out


def sanitize(layer) -> dict:
    """
    Чистит один уровень настроек: только известные ключи, правильные типы,
    числа в границах. Значение None значит «не переопределять» и выбрасывается.
    """
    if not isinstance(layer, dict):
        return {}
    out: dict = {}
    for key, value in layer.items():
        if value is None or key not in ALL_KEYS:
            continue
        if key in _BOOL:
            b = _as_bool(value)
            if b is not None:
                out[key] = b
        elif key in _INT:
            _, lo, hi = _INT[key]
            n = _as_number(value, int)
            if n is not None:
                out[key] = max(lo, min(hi, n))
        elif key in _FLOAT:
            _, lo, hi = _FLOAT[key]
            n = _as_number(value, float)
            if n is not None:
                out[key] = round(max(lo, min(hi, n)), 4)
        elif key in _CHOICE:
            if key == "auto_analyze" and isinstance(value, bool):
                value = "gaps" if value else "off"   # старый формат — флаг
            if value in _CHOICE[key][1]:
                out[key] = value
        elif key in _STR:
            s = _clean_str(value, _STR[key][1])
            if s is not None:
                out[key] = s
        elif key == "rpg_user_only":
            if isinstance(value, (list, tuple)):
                out[key] = [m for m in RPG_USER_ONLY_MODULES if m in value]
        elif key == "rpg_bar_config":
            v = _clean_bar_config(value)
            if v is not None:
                out[key] = v
        elif key == "rpg_attr_config":
            v = _clean_attr_config(value)
            if v is not None:
                out[key] = v
        elif key == "calendar":
            v = _clean_calendar(value)
            if v is not None:
                out[key] = v
        elif key == "prompts":
            v = _clean_prompts(value)
            if v is not None:
                out[key] = v
    # resummary_threshold: 0 — выключено, 1 не имеет смысла (свернуть одну
    # свёртку в одну) — плагин зажимал до 2.
    if out.get("resummary_threshold") == 1:
        out["resummary_threshold"] = 2
    return out


def resolve(global_layer=None, character_layer=None, chat_layer=None) -> dict:
    """
    Действующие настройки чата: умолчания, поверх — глобальные, профиль
    персонажа и переопределения чата.

    Промпты сливаются по ключам: чат может заменить один промпт, не копируя
    остальные. Пустая строка на уровне означает «вернуть промпт по умолчанию»
    (иначе переопределить наследованный свой промпт умолчанием было бы нельзя).
    """
    out = defaults()
    prompts: dict = {}
    for layer in (global_layer, character_layer, chat_layer):
        clean = sanitize(layer)
        layer_prompts = clean.pop("prompts", None)
        out.update(clean)
        if layer_prompts:
            for key, text in layer_prompts.items():
                if text.strip():
                    prompts[key] = text
                else:
                    prompts.pop(key, None)
    out["prompts"] = prompts
    return out


def diff_from(base: dict, values: dict) -> dict:
    """Ключи, где values отличается от base (для записи переопределений)."""
    clean = sanitize(values)
    return {k: v for k, v in clean.items() if base.get(k) != v}


def merge_overrides(current, patch) -> dict:
    """
    Применить к переопределениям уровня частичную правку из интерфейса:
    значение None удаляет переопределение ключа, остальное чистится и пишется.
    """
    result = sanitize(current)
    if not isinstance(patch, dict):
        return result
    for key, value in patch.items():
        if key not in ALL_KEYS:
            continue
        if value is None:
            result.pop(key, None)
            continue
        clean = sanitize({key: value})
        if key in clean:
            if key == "prompts":
                merged = dict(result.get("prompts") or {})
                for pk, text in clean["prompts"].items():
                    merged[pk] = text
                result["prompts"] = merged
            else:
                result[key] = clean[key]
    return result


def export_profile(settings: dict) -> dict:
    """Профиль для файла: все известные ключи, без служебного."""
    clean = sanitize(settings)
    return {"type": "horae-settings", "version": 1, "settings": copy.deepcopy(clean)}


def import_profile(data) -> dict:
    """Настройки из файла профиля (своего формата или «голого» словаря)."""
    if isinstance(data, dict) and data.get("type") == "horae-settings":
        data = data.get("settings")
    return sanitize(data)
