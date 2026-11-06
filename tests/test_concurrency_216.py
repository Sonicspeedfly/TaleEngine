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
