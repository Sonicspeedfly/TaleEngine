"""
Теги и релизы GitHub из CHANGELOG.md.

Каждая версия «## [X.Y.Z] — дата» получает тег vX.Y.Z на мерж-коммите main,
который её принёс (первый коммит основной ветки, где появился её заголовок), и
релиз «TaleEngine vX.Y.Z Beta» с текстом её раздела.

  * обычный запуск (push в main) — только верхняя, самая новая версия;
  * --all или BACKFILL=true — все версии без релиза (догнать пропущенные);
  * --dry-run — только показать, что было бы сделано (gh не нужен).

Версия, чей мерж уже помечен тегом другой версии (вышла в составе неё), не
получает отдельного тега: её текст и так в том релизе. Нужен gh с GH_TOKEN
(в GitHub Actions — github.token с правом contents: write).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys

HEADING = re.compile(r"^## \[(\d+\.\d+\.\d+)\][^\n]*$", re.M)


def sh(*args: str) -> str:
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout.strip()


def vkey(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in v.split("."))


def sections(text: str) -> list[tuple[str, str]]:
    """[(версия, текст раздела без заголовка)] в порядке файла (новые сверху)."""
    marks = list(HEADING.finditer(text))
    out = []
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        out.append((m.group(1), text[m.end():end].strip()))
    return out


def target_commit(version: str, mainline: list[str]) -> str | None:
    """Мерж-коммит main, который принёс версию: первый, где есть её заголовок."""
    first = sh("git", "log", "--reverse", "--format=%H", "-S", f"## [{version}]", "HEAD", "--", "CHANGELOG.md")
    first = first.split("\n")[0] if first else ""
    if not first:
        return None
    for sha in mainline:
        if subprocess.run(["git", "merge-base", "--is-ancestor", first, sha]).returncode == 0:
            return sha
    return None


def main() -> int:
    dry = "--dry-run" in sys.argv
    backfill = "--all" in sys.argv or os.environ.get("BACKFILL", "").lower() in ("1", "true", "yes")
    secs = sections(open("CHANGELOG.md", encoding="utf-8").read())
    if not secs:
        print("В CHANGELOG.md нет версий")
        return 0
    newest = max((v for v, _ in secs), key=vkey)
    todo = secs if backfill else secs[:1]

    tags = {t: sh("git", "rev-list", "-n1", t) for t in sh("git", "tag", "-l", "v*").split()}
    tagged = {sha: t for t, sha in tags.items()}
    released: set[str] = set()
    if not dry:
        released = {r["tagName"] for r in json.loads(
            sh("gh", "release", "list", "--limit", "1000", "--json", "tagName") or "[]")}
    mainline = sh("git", "rev-list", "--first-parent", "--reverse", "HEAD").split()

    made = 0
    for version, body in sorted(todo, key=lambda s: vkey(s[0])):
        tag = f"v{version}"
        if tag in released:
            continue
        sha = tags.get(tag) or target_commit(version, mainline)
        if not sha:
            print(f"{tag}: не нашёл мерж в основной ветке — пропускаю")
            continue
        if tag not in tags and sha in tagged:
            print(f"{tag}: вышла в составе {tagged[sha]} — отдельного тега нет")
            continue
        cmd = ["gh", "release", "create", tag, "--title", f"TaleEngine {tag} Beta",
               "--notes-file", "-", f"--latest={'true' if version == newest else 'false'}"]
        if tag not in tags:
            cmd += ["--target", sha]
        print(f"{tag}: {'был бы создан' if dry else 'создаю'} релиз на {sha[:7]}"
              f"{' (тег уже есть)' if tag in tags else ''}")
        if not dry:
            subprocess.run(cmd, input=body or f"TaleEngine {tag}", text=True, check=True)
        tagged[sha] = tag
        made += 1
    print(f"Готово: {made}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
