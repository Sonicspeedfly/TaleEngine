"""
Подготовка к запуску: зависимости и база данных.

Вызывается из start.sh и start.bat (уже интерпретатором из .venv) ПЕРЕД стартом
сервера и бота, чтобы после `git pull` ничего не приходилось делать руками:

  1. Зависимости. Метка `.venv/.installed` хранит хэш `backend/requirements.txt`.
     Метки нет — первая установка (с обновлением pip). Хэш не совпал (файл изменился,
     или метка от старого скрипта без хэша) — `pip install -r` ещё раз.
  2. База данных. Рядом с SQLite-файлом лежит `.schema-fingerprint` — хэш
     `backend/models.py` и `backend/database.py` (описание схемы и миграции).
     Не совпал — сначала копия базы в `data/backups/` (хранятся последние
     BACKUP_KEEP), потом миграция `init_db()`. Сервер и бот зовут `init_db()` и сами,
     но тут это происходит один раз, до их старта и ПОСЛЕ копии.

Код выхода 0 — можно запускать сервер, 1 — нельзя (сообщение уже напечатано).
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

ROOT = Path(__file__).resolve().parent.parent
REQUIREMENTS = ROOT / "backend" / "requirements.txt"
DEPS_MARKER = ROOT / ".venv" / ".installed"
SCHEMA_FILES = (ROOT / "backend" / "models.py", ROOT / "backend" / "database.py")
FINGERPRINT_NAME = ".schema-fingerprint"
BACKUP_DIR_NAME = "backups"
BACKUP_KEEP = 5


def log(msg: str) -> None:
    print(msg, flush=True)


def file_hash(*paths: Path) -> str:
    """sha256 содержимого файлов по порядку (отсутствующий файл = пустой)."""
    h = hashlib.sha256()
    for p in paths:
        h.update(p.name.encode())
        h.update(b"\0")
        if p.is_file():
            h.update(p.read_bytes())
        h.update(b"\0")
    return h.hexdigest()


def _read(path: Path) -> Optional[str]:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return None


def _in_venv() -> bool:
    return sys.prefix != getattr(sys, "base_prefix", sys.prefix)


# --------------------------------------------------------------------------- #
# 1. Зависимости
# --------------------------------------------------------------------------- #

def _pip(*args: str) -> int:
    return subprocess.call([sys.executable, "-m", "pip", *args])


def ensure_dependencies(
    requirements: Path = REQUIREMENTS,
    marker: Path = DEPS_MARKER,
    pip: Callable[..., int] = _pip,
) -> bool:
    """
    Ставит зависимости, если requirements.txt изменился с прошлой установки.
    False — только если не удалась ПЕРВАЯ установка: без библиотек запускать нечего.
    Сбой доустановки (нет интернета) — предупреждение, запуск со старыми библиотеками;
    метка не обновляется, поэтому следующий запуск попробует снова.
    """
    wanted = file_hash(requirements)
    have = _read(marker)
    if have == wanted:
        return True
    first = have is None
    if first:
        log("[setup] Ставлю зависимости (первый раз — пара минут) ...")
        pip("install", "--upgrade", "pip")
    else:
        log("[setup] requirements.txt изменился — доустанавливаю зависимости ...")
    if pip("install", "-r", str(requirements)) != 0:
        if first:
            log("[error] Не удалось установить зависимости. Проверьте интернет и запустите снова.")
            return False
        log("[warn] Не удалось обновить зависимости — запускаю со старыми, "
            "повторю при следующем запуске.")
        return True
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(wanted + "\n", encoding="utf-8")
    return True


# --------------------------------------------------------------------------- #
# 2. База данных
# --------------------------------------------------------------------------- #

def sqlite_path(url: str) -> Optional[Path]:
    """Путь к файлу SQLite из DATABASE_URL; None — не SQLite или база в памяти."""
    if not url.startswith("sqlite"):
        return None
    raw = url.split("///", 1)[-1] if "///" in url else ""
    raw = raw.split("?", 1)[0]
    if not raw or raw == ":memory:":
        return None
    return Path(raw)


def backup_sqlite(db: Path, backups: Path, *, keep: int = BACKUP_KEEP,
                  now: Optional[datetime] = None) -> Path:
    """
    Копия базы через sqlite3 backup API — корректна и при WAL, и если файл кто-то
    читает. Пишется во временный файл и переименовывается: оборванная копия не
    выдаёт себя за целую. Старые копии сверх `keep` удаляются.
    """
    backups.mkdir(parents=True, exist_ok=True)
    stamp = (now or datetime.now()).strftime("%Y-%m-%d_%H-%M-%S")
    target = backups / f"{db.stem}-{stamp}{db.suffix}"
    n = 1
    while target.exists():
        target = backups / f"{db.stem}-{stamp}-{n}{db.suffix}"
        n += 1
    tmp = target.with_name(target.name + ".part")
    src = sqlite3.connect(str(db), timeout=30)
    try:
        dst = sqlite3.connect(str(tmp))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    tmp.replace(target)

    old = sorted(backups.glob(f"{db.stem}-*{db.suffix}"),
                 key=lambda p: p.stat().st_mtime, reverse=True)
    for extra in old[keep:]:
        try:
            extra.unlink()
        except OSError:
            pass
    return target


def prepare_database(
    db: Optional[Path],
    migrate: Callable[[], None],
    *,
    fingerprint: str,
    state_dir: Path,
    keep: int = BACKUP_KEEP,
    skip_backup: bool = False,
    now: Optional[datetime] = None,
) -> bool:
    """
    Схема в коде изменилась с прошлого запуска → копия базы (если файл уже есть),
    затем миграция. Отпечаток пишется только после успешной миграции.
    False — запускать сервер нельзя.
    """
    stamp_file = state_dir / FINGERPRINT_NAME
    if _read(stamp_file) == fingerprint:
        return True

    exists = db is not None and db.is_file() and db.stat().st_size > 0
    if exists and not skip_backup:
        try:
            copy = backup_sqlite(db, state_dir / BACKUP_DIR_NAME, keep=keep, now=now)
        except Exception as exc:  # noqa: BLE001 — любой сбой копии останавливает запуск
            log(f"[error] Не удалось сделать копию базы: {exc}")
            log("        Базу не трогаю. Освободите место на диске и запустите снова "
                "(или SKIP_DB_BACKUP=1, чтобы обновить без копии).")
            return False
        log(f"[db] Схема базы обновилась — копия сохранена: {copy}")

    if exists:
        log("[db] Обновляю базу данных ...")
    try:
        migrate()
    except Exception as exc:  # noqa: BLE001 — показываем причину и не запускаем сервер
        log(f"[error] Не удалось обновить базу данных: {exc!r}")
        if exists and not skip_backup:
            log("        Копия базы до обновления лежит в "
                f"{state_dir / BACKUP_DIR_NAME}.")
        return False

    state_dir.mkdir(parents=True, exist_ok=True)
    stamp_file.write_text(fingerprint + "\n", encoding="utf-8")
    log("[db] База данных готова" if exists else "[db] База данных создана")
    return True


def _migrate() -> None:
    """init_db() из бэкенда — та же миграция, что делает сервер при старте."""
    import asyncio

    from backend.database import engine, init_db

    async def run() -> None:
        try:
            await init_db()
        finally:
            await engine.dispose()

    asyncio.run(run())


def main() -> int:
    os.chdir(ROOT)
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    # Кириллица в консоли Windows / при перенаправлении вывода в файл.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass

    if _in_venv():
        if not ensure_dependencies():
            return 1
    else:
        log("[warn] Запуск не из .venv — зависимости не проверяю.")

    try:
        from backend.config import settings
    except Exception as exc:  # noqa: BLE001
        log(f"[error] Не удалось прочитать настройки (.env): {exc!r}")
        return 1

    db = sqlite_path(settings.DATABASE_URL)
    if db is not None and not db.is_absolute():
        db = ROOT / db
    state_dir = db.parent if db is not None else ROOT / "data"
    ok = prepare_database(
        db,
        _migrate,
        fingerprint=file_hash(*SCHEMA_FILES),
        state_dir=state_dir,
        skip_backup=os.environ.get("SKIP_DB_BACKUP", "").strip() in ("1", "true", "yes"),
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
