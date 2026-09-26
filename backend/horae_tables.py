"""
Пользовательские таблицы Horae: разбор <horaetable:…>, повтор, правка структуры, рендер (только stdlib).

Зачем модуль. В плагине Horae пользователь заводит таблицы (квесты, связи,
инвентарь отряда…), модель заполняет их строками `r,c:значение` в теге
<horaetable:Имя>, а плагин держит данные в chat[0] и при каждой правке,
свайпе или удалении пересобирает их (rebuildTableData). У нас данные таблиц,
как и всё состояние Horae, считаются повтором: `base` (снимок, сделанный
последней правкой пользователя) + вклады ИИ из сообщений с id больше
`base_anchor`. Правка пользователя пересчитывает текущие данные и делает их
новой `base` с якорем на последнем сообщении — это аналог purgeTableContributions
плагина: старые вклады ИИ больше не применяются, а свежие ответы продолжают
дописывать таблицу.

Ключи ячеек — строки "r,c" (в плагине "r-c"; при разборе и импорте
принимаются оба). Строка 0 и столбец 0 — заголовки; rows/cols считают и их,
минимум 2×2.

Три области: global (шаблон в библиотеке), character (шаблон в профиле
персонажа) и local (таблица чата). У шаблона — только структура, блокировки,
промпт и заголовки; данные живут в «оверлее» чата
(`HoraeChatState.table_overlays[template_id]`). resolve() склеивает шаблон и
оверлей в эффективную таблицу, split_effective() раскладывает обратно.

Чистые функции — никакой БД; всё проверяется tests/test_horae_tables.py.
"""
from __future__ import annotations

import copy
import re
import secrets

# Блок таблицы. Двоеточие — `:` или `：`; закрывающий тег модель иногда
# повторяет с именем (`</horaetable:Квесты>`) — принимаем и так.
_BLOCK_RE = re.compile(r"<horaetable[:：]\s*(.+?)>([\s\S]*?)</horaetable(?:[:：][^>]*)?>", re.I)
_SEGMENT_SPLIT_RE = re.compile(r"\s*[|｜]\s*")
_CELL_RE = re.compile(r"^(\d+)\s*[,，\-]\s*(\d+)\s*[:：]\s*(.*)$", re.S)
# Заглушки «пусто». Их модель пишет вопреки правилам; записанные как значение,
# они затёрли бы реальные данные ячейки. Очистить ячейку ИИ не может — только
# пользователь (как в плагине).
_EMPTY_VALUE_RE = re.compile(r"^(?:[(（]?\s*(?:пусто|空)\s*[)）]?|[-—–]+)$", re.I)
_KEY_RE = re.compile(r"^\s*(\d+)\s*[,，\-]\s*(\d+)\s*$")

_SCOPES = ("global", "character", "local")
# Порядок поиска таблицы по имени для вклада ИИ: локальная таблица чата
# перекрывает одноимённый шаблон персонажа, а тот — глобальный (как в плагине).
_LOOKUP_ORDER = ("local", "character", "global")


# ============================================================================
# Ключи и ячейки
# ============================================================================

def _key(r: int, c: int) -> str:
    return f"{r},{c}"


def _parse_key(key) -> tuple[int, int] | None:
    """"r,c" / "r-c" / (r, c) → (r, c) или None."""
    if isinstance(key, (list, tuple)) and len(key) == 2:
        try:
            return int(key[0]), int(key[1])
        except (TypeError, ValueError):
            return None
    m = _KEY_RE.match(str(key or ""))
    return (int(m.group(1)), int(m.group(2))) if m else None


def _is_header(r: int, c: int) -> bool:
    return r == 0 or c == 0


def _norm_cells(cells) -> dict[str, str]:
    """Словарь ячеек → {"r,c": непустая строка}; мусорные ключи отбрасываются."""
    out: dict[str, str] = {}
    for k, v in (cells or {}).items() if isinstance(cells, dict) else ():
        rc = _parse_key(k)
        if rc is None or v is None:
            continue
        text = str(v)
        if text.strip():
            out[_key(*rc)] = text
    return out


def _norm_int_list(values) -> list[int]:
    out = set()
    for v in values or []:
        try:
            out.add(int(v))
        except (TypeError, ValueError):
            continue
    return sorted(out)


def _norm_cell_list(values) -> list[str]:
    out = set()
    for v in values or []:
        rc = _parse_key(v)
        if rc is not None:
            out.add(_key(*rc))
    return sorted(out, key=lambda k: _parse_key(k))


def _dims(rows, cols, cells: dict[str, str]) -> tuple[int, int]:
    """Размер с учётом ячеек за пределами (таблица растёт), минимум 2×2."""
    r = max(2, _to_int(rows, 2))
    c = max(2, _to_int(cols, 2))
    for k in cells:
        rc = _parse_key(k)
        if rc:
            r, c = max(r, rc[0] + 1), max(c, rc[1] + 1)
    return r, c


def _to_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# ============================================================================
# Разбор ответа модели
# ============================================================================

def _parse_cells(text: str) -> dict[str, str]:
    cells: dict[str, str] = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        # Несколько ячеек в строке через «|». Поэтому значение ячейки не может
        # содержать «|» — ограничение формата плагина.
        for seg in _SEGMENT_SPLIT_RE.split(line):
            m = _CELL_RE.match(seg.strip())
            if not m:
                continue
            value = m.group(3).strip()
            if not value or _EMPTY_VALUE_RE.match(value):
                continue
            cells[_key(int(m.group(1)), int(m.group(2)))] = value
    return cells


def parse_blocks(text: str) -> list[dict]:
    """Все блоки <horaetable:ИМЯ> ответа → [{"name", "cells": {"r,c": str}}] (только непустые)."""
    out = []
    for m in _BLOCK_RE.finditer(text or ""):
        name = m.group(1).strip()
        cells = _parse_cells(m.group(2))
        if name and cells:
            out.append({"name": name, "cells": cells})
    return out


def strip_blocks(text: str) -> str:
    """Текст без блоков <horaetable:…>."""
    return _BLOCK_RE.sub("", text or "")


# ============================================================================
# Таблицы, шаблоны, разрешение областей
# ============================================================================

def new_table(name: str, rows: int = 3, cols: int = 3, prompt: str = "",
              scope: str = "local") -> dict:
    return {
        "id": "t_" + secrets.token_hex(4),
        "name": str(name or "").strip(),
        "scope": scope if scope in _SCOPES else "local",
        "prompt": str(prompt or ""),
        "rows": max(2, _to_int(rows, 3)),
        "cols": max(2, _to_int(cols, 3)),
        "locked_rows": [],
        "locked_cols": [],
        "locked_cells": [],
        "base": {},
        "base_anchor": 0,
    }


def _headers_of(cells: dict) -> dict[str, str]:
    return {k: v for k, v in _norm_cells(cells).items() if _is_header(*_parse_key(k))}


def make_template(table: dict) -> dict:
    """
    Шаблон из таблицы: только заголовки (строка 0 / столбец 0), структура,
    блокировки и промпт. Данные остаются в чате — шаблон общий для многих чатов.
    Заголовки берутся из `headers`, `base` и `data` (текущие данные таблицы) —
    в этом порядке, более свежие перекрывают.
    """
    t = table or {}
    headers: dict[str, str] = {}
    for src in (t.get("headers"), t.get("base"), t.get("data")):
        headers.update(_headers_of(src or {}))
    rows, cols = _dims(t.get("rows"), t.get("cols"), headers)
    return {
        "id": t.get("id") or "t_" + secrets.token_hex(4),
        "name": str(t.get("name") or "").strip(),
        "scope": t.get("scope") if t.get("scope") in _SCOPES else "global",
        "prompt": str(t.get("prompt") or ""),
        "rows": rows,
        "cols": cols,
        "locked_rows": _norm_int_list(t.get("locked_rows")),
        "locked_cols": _norm_int_list(t.get("locked_cols")),
        "locked_cells": _norm_cell_list(t.get("locked_cells")),
        "headers": headers,
    }


def _effective_from_template(tpl: dict, overlay: dict | None, scope: str) -> dict:
    headers = _headers_of(tpl.get("headers") or {})
    if not headers:
        headers = _headers_of(tpl.get("base") or tpl.get("data") or {})
    if overlay:
        base = _norm_cells(overlay.get("base"))
        # Заголовки шаблона главнее оверлея: их правят в шаблоне, и правка
        # должна дойти до всех чатов (в плагине — на каждом разрешении).
        base.update(headers)
        anchor = _to_int(overlay.get("base_anchor"), 0)
        rows = max(_to_int(tpl.get("rows"), 2), _to_int(overlay.get("rows"), 2))
        cols = max(_to_int(tpl.get("cols"), 2), _to_int(overlay.get("cols"), 2))
    else:
        base, anchor = dict(headers), 0
        rows, cols = _to_int(tpl.get("rows"), 2), _to_int(tpl.get("cols"), 2)
    rows, cols = _dims(rows, cols, base)
    return {
        # Без id шаблон адресуется по имени — тем же ключом, что и его оверлей.
        "id": tpl.get("id") or str(tpl.get("name") or "").strip(),
        "name": str(tpl.get("name") or "").strip(),
        "scope": scope,
        "prompt": str(tpl.get("prompt") or ""),
        "rows": rows,
        "cols": cols,
        "locked_rows": _norm_int_list(tpl.get("locked_rows")),
        "locked_cols": _norm_int_list(tpl.get("locked_cols")),
        "locked_cells": _norm_cell_list(tpl.get("locked_cells")),
        "base": base,
        "base_anchor": anchor,
    }


def _effective_local(table: dict) -> dict:
    base = _norm_cells(table.get("base"))
    rows, cols = _dims(table.get("rows"), table.get("cols"), base)
    out = copy.deepcopy(table)
    out.update({
        "id": table.get("id") or "t_" + secrets.token_hex(4),
        "name": str(table.get("name") or "").strip(),
        "scope": "local",
        "prompt": str(table.get("prompt") or ""),
        "rows": rows,
        "cols": cols,
        "locked_rows": _norm_int_list(table.get("locked_rows")),
        "locked_cols": _norm_int_list(table.get("locked_cols")),
        "locked_cells": _norm_cell_list(table.get("locked_cells")),
        "base": base,
        "base_anchor": _to_int(table.get("base_anchor"), 0),
    })
    return out


def resolve(global_templates: list, char_templates: list, local_tables: list,
            overlays: dict) -> list[dict]:
    """
    Эффективные таблицы чата в порядке global → character → local.

    Таблицы без имени пропускаются: модель адресует таблицу только по имени,
    безымянную она заполнить не может, а в промпте она была бы шумом.
    """
    overlays = overlays if isinstance(overlays, dict) else {}
    out: list[dict] = []
    for scope, templates in (("global", global_templates), ("character", char_templates)):
        for tpl in templates or []:
            if not isinstance(tpl, dict) or not str(tpl.get("name") or "").strip():
                continue
            key = tpl.get("id") or str(tpl.get("name")).strip()
            out.append(_effective_from_template(tpl, overlays.get(key), scope))
    for table in local_tables or []:
        if isinstance(table, dict) and str(table.get("name") or "").strip():
            out.append(_effective_local(table))
    return out


# ============================================================================
# Повтор
# ============================================================================

def _locks(table: dict) -> tuple[set[int], set[int], set[str]]:
    return (set(_norm_int_list(table.get("locked_rows"))),
            set(_norm_int_list(table.get("locked_cols"))),
            set(_norm_cell_list(table.get("locked_cells"))))


def replay(tables: list[dict], contributions: list[tuple[int, list[dict]]]) -> dict[str, dict]:
    """
    Данные таблиц = base + вклады ИИ из сообщений с id > base_anchor.

    contributions — [(mid, [{"name", "cells"}]), …] по возрастанию mid.
    Результат — {table_id: {"data": {"r,c": str}, "rows", "cols"}}.

    Вклад ищет таблицу по имени (без учёта регистра и крайних пробелов):
    сначала локальные, потом персонажа, потом глобальные — первая найденная.
    Ячейка заголовка пишется, только если пуста (заголовки задаёт человек);
    заблокированные строки/столбцы/ячейки не пишутся; таблица растёт под
    координаты вклада.
    """
    results: dict[str, dict] = {}
    by_name: dict[str, dict] = {}
    for table in tables or []:
        tid = table.get("id")
        base = _norm_cells(table.get("base"))
        rows, cols = _dims(table.get("rows"), table.get("cols"), base)
        results[tid] = {"data": base, "rows": rows, "cols": cols}
    for scope in _LOOKUP_ORDER:
        for table in tables or []:
            if (table.get("scope") or "local") != scope:
                continue
            name = str(table.get("name") or "").strip().casefold()
            if name and name not in by_name:
                by_name[name] = table
    locks = {t.get("id"): _locks(t) for t in tables or []}

    for mid, contribs in contributions or []:
        for contrib in contribs or []:
            if not isinstance(contrib, dict):
                continue
            table = by_name.get(str(contrib.get("name") or "").strip().casefold())
            if table is None:
                continue
            # Вклады до якоря уже «впитаны» в base правкой пользователя.
            if _to_int(mid, 0) <= _to_int(table.get("base_anchor"), 0):
                continue
            res = results[table.get("id")]
            locked_rows, locked_cols, locked_cells = locks[table.get("id")]
            for k, value in _norm_cells(contrib.get("cells")).items():
                r, c = _parse_key(k)
                if _is_header(r, c) and str(res["data"].get(k) or "").strip():
                    continue
                if r in locked_rows or c in locked_cols or k in locked_cells:
                    continue
                res["data"][k] = value
                res["rows"] = max(res["rows"], r + 1)
                res["cols"] = max(res["cols"], c + 1)
    return results


# ============================================================================
# Правки пользователя: ячейка, очистка, структура, блокировки
# ============================================================================

def _with_base(table: dict, data: dict, anchor: int, rows: int | None = None,
               cols: int | None = None) -> dict:
    out = copy.deepcopy(table)
    base = _norm_cells(data)
    r, c = _dims(rows if rows is not None else table.get("rows"),
                 cols if cols is not None else table.get("cols"), base)
    out.update({"base": base, "base_anchor": _to_int(anchor, 0), "rows": r, "cols": c})
    return out


def set_cell(table: dict, data: dict, r: int, c: int, value: str, anchor: int) -> dict:
    """
    Правка ячейки пользователем: новая base = текущие данные с этой ячейкой,
    base_anchor = anchor (id последнего сообщения). Пустое значение очищает
    ячейку; блокировки на человека не действуют; таблица растёт.
    """
    r, c = int(r), int(c)
    if r < 0 or c < 0:
        raise ValueError("координаты ячейки не могут быть отрицательными")
    cells = _norm_cells(data)
    text = "" if value is None else str(value)
    if text.strip():
        cells[_key(r, c)] = text
    else:
        cells.pop(_key(r, c), None)
    return _with_base(table, cells, anchor,
                      max(_to_int(table.get("rows"), 2), r + 1),
                      max(_to_int(table.get("cols"), 2), c + 1))


def clear_data(table: dict, data: dict, anchor: int) -> dict:
    """«Очистить данные (оставить заголовки)»: base — только строка 0 и столбец 0."""
    headers = {k: v for k, v in _norm_cells(data).items() if _is_header(*_parse_key(k))}
    return _with_base(table, headers, anchor)


_STRUCTURE_OPS = ("add_row_above", "add_row_below", "add_col_left", "add_col_right",
                  "delete_row", "delete_col")


def _shift(cells: dict[str, str], axis: int, pos: int, delta: int, drop: int | None) -> dict[str, str]:
    """Сдвиг ячеек по оси (0 — строки, 1 — столбцы) начиная с pos; drop — удаляемый индекс."""
    out: dict[str, str] = {}
    for k, v in cells.items():
        rc = list(_parse_key(k))
        if drop is not None and rc[axis] == drop:
            continue
        if rc[axis] >= pos:
            rc[axis] += delta
        out[_key(*rc)] = v
    return out


def _shift_index_list(values: list[int], pos: int, delta: int, drop: int | None) -> list[int]:
    out = []
    for v in values:
        if drop is not None and v == drop:
            continue
        out.append(v + delta if v >= pos else v)
    return sorted(set(out))


def structure(table: dict, data: dict, op: str, index: int, anchor: int) -> dict:
    """
    Добавить/удалить строку или столбец. Ячейки и блокировки сдвигаются вместе
    с данными; строку/столбец заголовков (0) удалить нельзя, вставка «выше»
    строки 0 кладёт новую строку первой строкой данных. Минимум 2×2.
    Неверная операция или индекс — ValueError.
    """
    if op not in _STRUCTURE_OPS:
        raise ValueError(f"неизвестная операция структуры таблицы: {op}")
    index = int(index)
    cells = _norm_cells(data)
    rows, cols = _dims(table.get("rows"), table.get("cols"), cells)
    locked_rows, locked_cols, locked_cells = (sorted(x) for x in _locks(table))
    axis = 0 if "row" in op else 1
    size = rows if axis == 0 else cols

    if op.startswith("add"):
        pos = index if op in ("add_row_above", "add_col_left") else index + 1
        pos = min(max(1, pos), size)
        cells = _shift(cells, axis, pos, 1, None)
        cell_locks = _shift({k: "1" for k in locked_cells}, axis, pos, 1, None)
        if axis == 0:
            locked_rows = _shift_index_list(locked_rows, pos, 1, None)
            rows += 1
        else:
            locked_cols = _shift_index_list(locked_cols, pos, 1, None)
            cols += 1
    else:
        if index < 1 or index >= size:
            raise ValueError("нельзя удалить заголовок или несуществующую строку/столбец")
        if size <= 2:
            raise ValueError("таблица не может быть меньше 2×2")
        cells = _shift(cells, axis, index + 1, -1, index)
        cell_locks = _shift({k: "1" for k in locked_cells}, axis, index + 1, -1, index)
        if axis == 0:
            locked_rows = _shift_index_list(locked_rows, index + 1, -1, index)
            rows -= 1
        else:
            locked_cols = _shift_index_list(locked_cols, index + 1, -1, index)
            cols -= 1

    out = _with_base(table, cells, anchor, rows, cols)
    out["locked_rows"] = locked_rows
    out["locked_cols"] = locked_cols
    out["locked_cells"] = _norm_cell_list(cell_locks)
    return out


def set_lock(table: dict, kind: str, r: int, c: int, locked: bool) -> dict:
    """Блокировка строки (kind="row", по r), столбца ("col", по c) или ячейки ("cell")."""
    out = copy.deepcopy(table)
    if kind == "row":
        vals = set(_norm_int_list(table.get("locked_rows")))
        (vals.add if locked else vals.discard)(int(r))
        out["locked_rows"] = sorted(vals)
    elif kind == "col":
        vals = set(_norm_int_list(table.get("locked_cols")))
        (vals.add if locked else vals.discard)(int(c))
        out["locked_cols"] = sorted(vals)
    elif kind == "cell":
        vals = set(_norm_cell_list(table.get("locked_cells")))
        (vals.add if locked else vals.discard)(_key(int(r), int(c)))
        out["locked_cells"] = _norm_cell_list(vals)
    else:
        raise ValueError(f"неизвестный вид блокировки: {kind}")
    return out


def split_effective(table: dict) -> tuple[dict | None, dict]:
    """
    Эффективная таблица → (шаблон, оверлей) для global/character или
    (None, вся таблица) для local. Так правка структуры/заголовков доходит до
    шаблона, а данные остаются в чате.
    """
    if (table or {}).get("scope") not in ("global", "character"):
        return None, copy.deepcopy(table)
    template = make_template({k: v for k, v in table.items() if k != "data"})
    template["id"] = table.get("id") or template["id"]
    template["scope"] = table["scope"]
    overlay = {
        "base": _norm_cells(table.get("base")),
        "base_anchor": _to_int(table.get("base_anchor"), 0),
        "rows": _to_int(table.get("rows"), 2),
        "cols": _to_int(table.get("cols"), 2),
    }
    return template, overlay


# ============================================================================
# Рендер для промпта (spec_core §7.3)
# ============================================================================

def _result_for(table: dict, results: dict) -> tuple[dict[str, str], int, int]:
    res = (results or {}).get(table.get("id"))
    if res is None:
        data = _norm_cells(table.get("base"))
        rows, cols = _dims(table.get("rows"), table.get("cols"), data)
        return data, rows, cols
    data = _norm_cells(res.get("data"))
    rows, cols = _dims(res.get("rows"), res.get("cols"), data)
    return data, rows, cols


def _is_rendered(table: dict, data: dict[str, str]) -> bool:
    return any(v.strip() for v in data.values()) or bool(str(table.get("prompt") or "").strip())


def render_block(tables: list[dict], results: dict[str, dict]) -> str:
    """
    Таблицы для блока состояния, по-русски. Каждая таблица начинается с
    "\\n[Имя](Nстрок×Mстолбцов)". Хвостовые пустые строки не выводятся —
    вместо них одна пометка: иначе пустая таблица на 30 строк съедала бы
    сотни токенов на каждом ходу. Пустые столбцы перечисляются, чтобы модель
    знала, что их ждут заполнить.
    """
    lines: list[str] = []
    for table in tables or []:
        data, rows, cols = _result_for(table, results)
        if not _is_rendered(table, data):
            continue
        locked_rows, locked_cols, locked_cells = _locks(table)
        name = str(table.get("name") or "").strip() or "Пользовательская таблица"
        lines.append(f"\n[{name}]({rows - 1}строк×{cols - 1}столбцов)")
        prompt = str(table.get("prompt") or "").strip()
        if prompt:
            lines.append(f"(Инструкции: {prompt})")

        last = 0
        for r in range(rows - 1, 0, -1):
            if any(data.get(_key(r, c), "").strip() for c in range(cols)):
                last = r
                break
        last = last or 1

        header = []
        for c in range(cols):
            label = data.get(_key(0, c)) or ("Заголовок" if c == 0 else f"Столбец{c}")
            header.append(f"[0,{c}]{label}" + ("🔒" if c in locked_cols else ""))
        lines.append(" | ".join(header))
        for r in range(1, last + 1):
            row = []
            for c in range(cols):
                if c == 0:
                    label = data.get(_key(r, 0)) or str(r)
                    row.append(f"[{r},0]{label}" + ("🔒" if r in locked_rows else ""))
                else:
                    val = data.get(_key(r, c), "")
                    row.append(f"[{r},{c}]{val}" + ("🔒" if _key(r, c) in locked_cells else ""))
            lines.append(" | ".join(row))
        if last < rows - 1:
            lines.append(f"(всего {rows - 1} строк, строки {last + 1}-{rows - 1} пусты)")

        empty_cols = [c for c in range(1, cols)
                      if not any(data.get(_key(r, c), "").strip() for r in range(1, rows))]
        if empty_cols:
            names = [data.get(_key(0, c)) or f"Столбец{c}" for c in empty_cols]
            lines.append(f"({', '.join(names)}: нет данных, заполните, если в сюжете есть "
                         f"соответствующая информация)")
    return "\n".join(lines)


def rules_suffix(tables: list[dict], results: dict[str, dict]) -> str:
    """
    Размер и пример формата — для ПЕРВОЙ выводимой таблицы (в плагине — первой
    вообще, из-за `break`). Если ни одна таблица не выводится (все пусты и без
    промпта), берём первую: иначе модель не узнала бы о таблице совсем.
    """
    tables = [t for t in tables or [] if str(t.get("name") or "").strip()]
    if not tables:
        return ""
    chosen = None
    for t in tables:
        data, _, _ = _result_for(t, results)
        if _is_rendered(t, data):
            chosen = t
            break
    chosen = chosen or tables[0]
    _, rows, cols = _result_for(chosen, results)
    name = str(chosen.get("name") or "").strip()
    return (f"\n★ Таблица «{name}» размер: {rows - 1} строк × {cols - 1} столбцов "
            f"(данные: строки 1-{rows - 1}, столбцы 1-{cols - 1})"
            f"\nПример (заполните пустые ячейки или обновите изменённые):"
            f"\n<horaetable:{name}>\n1,1:Содержимое A\n1,2:Содержимое B\n2,1:Содержимое C\n</horaetable>")


# ============================================================================
# Импорт / экспорт одной таблицы
# ============================================================================

EXPORT_FORMAT = "taleengine-horae-table"


def to_export(table: dict, data: dict) -> dict:
    """Таблица с текущими данными → JSON для файла (ключи ячеек "r,c")."""
    cells = _norm_cells(data)
    rows, cols = _dims(table.get("rows"), table.get("cols"), cells)
    return {
        "format": EXPORT_FORMAT,
        "v": 1,
        "name": str(table.get("name") or "").strip(),
        "prompt": str(table.get("prompt") or ""),
        "rows": rows,
        "cols": cols,
        "data": cells,
        "locked_rows": _norm_int_list(table.get("locked_rows")),
        "locked_cols": _norm_int_list(table.get("locked_cols")),
        "locked_cells": _norm_cell_list(table.get("locked_cells")),
    }


def from_import(obj) -> dict:
    """
    JSON таблицы → новая локальная таблица (новый id, base = данные файла).

    Понимает и наш экспорт, и файл плагина (`data` с ключами "r-c",
    `lockedRows/lockedCols/lockedCells`). base_anchor = 0: вызывающий ставит
    якорь на последнее сообщение, чтобы старые вклады одноимённой таблицы не
    легли поверх импортированных данных (в плагине — purge вкладов).
    """
    if isinstance(obj, dict) and isinstance(obj.get("table"), dict):
        obj = obj["table"]
    if not isinstance(obj, dict):
        raise ValueError("файл таблицы должен быть JSON-объектом")
    cells = _norm_cells(obj.get("data") if obj.get("data") is not None else obj.get("base"))
    rows, cols = _dims(obj.get("rows"), obj.get("cols"), cells)
    table = new_table(str(obj.get("name") or "").strip() or "Импортированная таблица",
                      rows, cols, str(obj.get("prompt") or ""), "local")
    table["base"] = cells
    table["locked_rows"] = _norm_int_list(obj.get("locked_rows", obj.get("lockedRows")))
    table["locked_cols"] = _norm_int_list(obj.get("locked_cols", obj.get("lockedCols")))
    table["locked_cells"] = _norm_cell_list(obj.get("locked_cells", obj.get("lockedCells")))
    return table
