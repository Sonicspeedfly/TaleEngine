"""
Сделать второй аккаунт Google основным в config.yaml LiteLLM-прокси (2.30.5).

    python3 scripts/promote_account.py /путь/к/config.yaml acc2/

Что делает:
  * берёт проект, ключ и регион из записи-шаблона «acc2/*»;
  * всем моделям Vertex без префикса (vertex_ai/…) ставит этот проект и ключ —
    регион у каждой свой остаётся;
  * то же — в litellm_settings (там проект по умолчанию для картинок);
  * записи «acc2/…» удаляет; псевдоним «acc2/имя», которого нет среди моделей
    без префикса, не теряется — становится просто «имя».
Старый файл сохраняется рядом: config.yaml.bak-ГГГГММДД-ЧЧММСС. Комментарии
YAML при перезаписи пропадают — они остаются в копии.
"""
from __future__ import annotations

import shutil
import sys
import time

KEYS = ("vertex_project", "vertex_credentials")


def promote(cfg: dict, prefix: str) -> tuple[dict, list[str]]:
    """Возвращает новый конфиг и список того, что сделано (для печати)."""
    prefix = prefix if prefix.endswith("/") else prefix + "/"
    models = list(cfg.get("model_list") or [])
    mine = [m for m in models if str(m.get("model_name") or "").startswith(prefix)]
    if not mine:
        raise SystemExit(f"В model_list нет записей «{prefix}…» — нечего переносить.")
    tpl = next((m for m in mine if m.get("model_name") == prefix + "*"), mine[0])
    src = tpl.get("litellm_params") or {}
    acc = {k: src[k] for k in KEYS if src.get(k)}
    if "vertex_project" not in acc:
        raise SystemExit(f"У записи «{tpl.get('model_name')}» нет vertex_project — откуда брать проект?")
    log = [f"Проект и ключ берутся из «{tpl.get('model_name')}»: " + ", ".join(f"{k}={v}" for k, v in acc.items())]

    main = [m for m in models if not str(m.get("model_name") or "").startswith(prefix)]
    names = {m.get("model_name") for m in main}
    for m in main:
        p = m.get("litellm_params") or {}
        if str(p.get("model") or "").startswith("vertex_ai/"):
            p.update(acc)
            m["litellm_params"] = p
            log.append(f"  {m.get('model_name')}: переведена на новый проект")
    for m in mine:
        name = str(m.get("model_name"))
        short = name[len(prefix):]
        if short == "*" or short in names:
            log.append(f"  {name}: удалена")
            continue
        m = dict(m, model_name=short)
        main.append(m)
        names.add(short)
        log.append(f"  {name}: стала «{short}»")
    out = dict(cfg, model_list=main)
    ls = dict(out.get("litellm_settings") or {})
    ls.update(acc)
    if src.get("vertex_location") and not ls.get("vertex_location"):
        ls["vertex_location"] = src["vertex_location"]
    out["litellm_settings"] = ls
    log.append("  litellm_settings: проект по умолчанию — новый")
    return out, log


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    path, prefix = argv[1], (argv[2] if len(argv) > 2 else "acc2/")
    try:
        import yaml
    except ImportError:
        print("Нужен PyYAML: запустите тем же python, что и LiteLLM, или pip install pyyaml.")
        return 1
    with open(path, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}
    new, log = promote(cfg, prefix)
    backup = f"{path}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
    shutil.copy2(path, backup)
    with open(path, "w", encoding="utf-8") as fh:
        yaml.safe_dump(new, fh, allow_unicode=True, sort_keys=False, default_flow_style=False)
    print("\n".join(log))
    print(f"Готово. Старый файл: {backup}")
    print("Дальше: systemctl restart litellm")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
