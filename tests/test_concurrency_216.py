"""
2.16.0: два пользователя разом не тормозят друг друга — сборка контекста в
потоках, индекс слов для лорбука, SQLite в режиме WAL.
"""
import random

from backend import horae_memory as hm


def test_keyword_index_matches_old_token_loop():
    """Поиск по множествам (_TokenIndex) — ровно то же, что прежний перебор слов."""
    random.seed(3)
    words = ("король короля королём меч мечом мечами рука рукав кот который котёл мир мирный "
             "замок замка замком сон сонечку ёж ежа лес лесом река реки дракон драконы тень").split()
    keys = words + ["замк*", "кот*", "тёмный лес", "Король", " меч ", "ёж", "ежи", "", "*", "ко"]
    for _ in range(300):
        text = " ".join(random.choice(words) for _ in range(random.randint(0, 30)))
        low = text.lower()
        tokens = hm._text_tokens(text)
        index = hm._TokenIndex(tokens)
        for kw in keys:
            assert hm.keyword_hits(kw, low, tokens, index) == hm.keyword_hits(kw, low, tokens), (kw, text)


def test_sqlite_runs_in_wal_mode(client):
    import sqlite3

    from tests.test_attachment_blobs import _db_path

    client.get("/api/health")
    con = sqlite3.connect(_db_path())
    assert con.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"


def test_entries_accept_already_decoded_json():
    """PostgreSQL (asyncpg) отдаёт JSON уже разобранным — мета не должна пропадать."""
    from backend import horae_engine as he

    meta = {"scene": {"location": "Таверна"}}
    out = he._entries_from_raw([
        (1, "assistant", {"metas": [meta], "side": True}, 0),
        (2, "assistant", '{"metas": [{"a": 1}]}', 0),
        (3, "user", None, 0),
        (4, "assistant", "null", 0),
        (5, "assistant", "{битый", 0),
    ])
    assert out[0] == (1, "assistant", meta, True)
    assert out[1] == (2, "assistant", {"a": 1}, False)
    assert [o[2] for o in out[2:]] == [None, None, None]


def test_soffice_runs_one_at_a_time_with_own_profile(monkeypatch):
    import threading
    import time

    from backend import document_service as ds

    calls, active, peak = [], [0], [0]
    lock = threading.Lock()

    def fake_run(args, **kw):
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        calls.append(args)
        time.sleep(0.05)
        with lock:
            active[0] -= 1

    monkeypatch.setattr(ds, "find_libreoffice", lambda: "/usr/bin/soffice")
    monkeypatch.setattr(ds.subprocess, "run", fake_run)
    ts = [threading.Thread(target=ds.convert_to_pdf, args=(b"x", ".docx")) for _ in range(3)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert peak[0] == 1
    assert all(any(a.startswith("-env:UserInstallation=file:") for a in c) for c in calls)
