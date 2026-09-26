"""
scripts/prestart.py — подготовка к запуску из start.sh / start.bat:
доустановка зависимостей по хэшу requirements.txt, копия базы и миграция
при изменении схемы.
"""
import importlib.util
import sqlite3
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("prestart", ROOT / "scripts" / "prestart.py")
prestart = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(prestart)


class FakePip:
    def __init__(self, code=0):
        self.code = code
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)
        return 0 if args[:3] == ("install", "--upgrade", "pip") else self.code


def _make_db(path: Path, rows=("a",)) -> None:
    con = sqlite3.connect(str(path))
    con.execute("CREATE TABLE t (v TEXT)")
    con.executemany("INSERT INTO t VALUES (?)", [(r,) for r in rows])
    con.commit()
    con.close()


def test_first_install_upgrades_pip_and_writes_hash(tmp_path):
    req = tmp_path / "requirements.txt"
    req.write_text("fastapi\n")
    marker = tmp_path / ".venv" / ".installed"
    pip = FakePip()

    assert prestart.ensure_dependencies(req, marker, pip) is True
    assert pip.calls == [("install", "--upgrade", "pip"), ("install", "-r", str(req))]
    assert marker.read_text().strip() == prestart.file_hash(req)

    # Ничего не менялось — pip не вызывается.
    pip.calls.clear()
    assert prestart.ensure_dependencies(req, marker, pip) is True
    assert pip.calls == []


def test_changed_requirements_reinstall_and_old_marker_counts_as_changed(tmp_path):
    req = tmp_path / "requirements.txt"
    req.write_text("fastapi\n")
    marker = tmp_path / ".installed"
    marker.write_text("installed\n")  # метка старого start.bat — без хэша
    pip = FakePip()

    assert prestart.ensure_dependencies(req, marker, pip) is True
    assert pip.calls == [("install", "-r", str(req))]  # не первая установка — без pip upgrade

    req.write_text("fastapi\nsqlalchemy[asyncio]\n")
    pip.calls.clear()
    assert prestart.ensure_dependencies(req, marker, pip) is True
    assert pip.calls == [("install", "-r", str(req))]
    assert marker.read_text().strip() == prestart.file_hash(req)


def test_failed_update_keeps_old_marker_but_failed_first_install_stops(tmp_path):
    req = tmp_path / "requirements.txt"
    req.write_text("fastapi\n")
    marker = tmp_path / ".installed"

    # Первая установка упала — запускать нечего.
    assert prestart.ensure_dependencies(req, marker, FakePip(code=1)) is False
    assert not marker.exists()

    # Доустановка упала — запуск со старыми библиотеками, метка прежняя (повторим потом).
    marker.write_text("old-hash\n")
    assert prestart.ensure_dependencies(req, marker, FakePip(code=1)) is True
    assert marker.read_text().strip() == "old-hash"


def test_sqlite_path():
    assert prestart.sqlite_path("sqlite+aiosqlite:///./data/aichat.db") == Path("./data/aichat.db")
    assert prestart.sqlite_path("sqlite+aiosqlite:////abs/x.db?mode=rwc") == Path("/abs/x.db")
    assert prestart.sqlite_path("sqlite+aiosqlite:///:memory:") is None
    assert prestart.sqlite_path("postgresql+asyncpg://u@h/db") is None


def test_backup_is_a_full_copy_and_old_ones_rotate(tmp_path):
    db = tmp_path / "aichat.db"
    _make_db(db, rows=("x", "y"))
    backups = tmp_path / "backups"

    made = [
        prestart.backup_sqlite(db, backups, keep=2, now=datetime(2026, 9, 26, 12, 0, i))
        for i in range(3)
    ]
    left = sorted(p.name for p in backups.iterdir())
    assert len(left) == 2 and not any(n.endswith(".part") for n in left)
    assert made[-1].name in left

    con = sqlite3.connect(str(made[-1]))
    assert [r[0] for r in con.execute("SELECT v FROM t ORDER BY v")] == ["x", "y"]
    con.close()


def test_schema_change_backs_up_then_migrates_once(tmp_path):
    db = tmp_path / "aichat.db"
    _make_db(db)
    calls = []

    def migrate():
        # Копия уже должна существовать, когда миграция начинает менять базу.
        calls.append(sorted(p.name for p in (tmp_path / "backups").iterdir()))

    kw = dict(state_dir=tmp_path, now=datetime(2026, 9, 26, 12, 0, 0))
    assert prestart.prepare_database(db, migrate, fingerprint="v1", **kw) is True
    assert calls == [["aichat-2026-09-26_12-00-00.db"]]
    assert (tmp_path / ".schema-fingerprint").read_text().strip() == "v1"

    # Схема та же — ни копии, ни миграции.
    assert prestart.prepare_database(db, migrate, fingerprint="v1", **kw) is True
    assert len(calls) == 1

    # Схема изменилась — снова копия и миграция.
    kw["now"] = datetime(2026, 9, 27, 8, 0, 0)
    assert prestart.prepare_database(db, migrate, fingerprint="v2", **kw) is True
    assert len(calls) == 2 and len(calls[1]) == 2


def test_new_database_is_created_without_backup(tmp_path):
    db = tmp_path / "aichat.db"
    migrated = []
    ok = prestart.prepare_database(
        db, lambda: migrated.append(True), fingerprint="v1", state_dir=tmp_path,
    )
    assert ok and migrated == [True]
    assert not (tmp_path / "backups").exists()


def test_failed_migration_stops_start_and_keeps_fingerprint(tmp_path):
    db = tmp_path / "aichat.db"
    _make_db(db)
    (tmp_path / ".schema-fingerprint").write_text("v1\n")

    def broken():
        raise RuntimeError("boom")

    assert prestart.prepare_database(db, broken, fingerprint="v2", state_dir=tmp_path) is False
    # Отпечаток прежний — следующий запуск попробует обновить снова; копия осталась.
    assert (tmp_path / ".schema-fingerprint").read_text().strip() == "v1"
    assert len(list((tmp_path / "backups").iterdir())) == 1


def test_failed_backup_leaves_database_untouched(tmp_path, monkeypatch):
    db = tmp_path / "aichat.db"
    _make_db(db)
    migrated = []

    def no_space(*a, **k):
        raise OSError("No space left on device")

    monkeypatch.setattr(prestart, "backup_sqlite", no_space)
    ok = prestart.prepare_database(
        db, lambda: migrated.append(True), fingerprint="v1", state_dir=tmp_path,
    )
    assert ok is False and migrated == []

    # С SKIP_DB_BACKUP миграция идёт без копии.
    ok = prestart.prepare_database(
        db, lambda: migrated.append(True), fingerprint="v1", state_dir=tmp_path,
        skip_backup=True,
    )
    assert ok is True and migrated == [True]


def test_real_migration_upgrades_old_database(tmp_path, monkeypatch):
    """init_db() дозаливает недостающие колонки в базу старой версии."""
    import asyncio

    from sqlalchemy import create_engine
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    from backend import database, models  # noqa: F401 — модели регистрируются в Base

    # База «версии 2.5.0»: полная схема, но без полей и таблиц Хроники Horae.
    db = tmp_path / "old.db"
    sync_eng = create_engine(f"sqlite:///{db}")
    database.Base.metadata.create_all(sync_eng)
    sync_eng.dispose()
    con = sqlite3.connect(str(db))
    con.execute("ALTER TABLE messages DROP COLUMN horae")
    con.execute("ALTER TABLE characters DROP COLUMN horae_profile")
    con.execute("DROP TABLE horae_chat_state")
    con.execute("DROP TABLE horae_memory_docs")
    con.commit()
    con.close()

    eng = create_async_engine(f"sqlite+aiosqlite:///{db}", poolclass=NullPool)
    monkeypatch.setattr(database, "engine", eng)
    try:
        ok = prestart.prepare_database(
            db, prestart._migrate, fingerprint="v1", state_dir=tmp_path,
        )
    finally:
        asyncio.run(eng.dispose())
    assert ok is True

    con = sqlite3.connect(str(db))
    cols = {r[1] for r in con.execute("PRAGMA table_info(messages)")}
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    con.close()
    assert "horae" in cols
    assert {"horae_chat_state", "horae_memory_docs"} <= tables
    assert len(list((tmp_path / "backups").iterdir())) == 1
