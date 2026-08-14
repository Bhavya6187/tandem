"""Opencode adapter units against a hermetic mini-DB (no binary needed)."""

import sqlite3

import pytest

from tandem.harness import opencode

MINI_SCHEMA = """
CREATE TABLE project (id TEXT PRIMARY KEY, worktree TEXT NOT NULL);
CREATE TABLE session (
    id TEXT PRIMARY KEY, project_id TEXT NOT NULL, slug TEXT NOT NULL,
    directory TEXT NOT NULL, path TEXT, title TEXT NOT NULL,
    version TEXT NOT NULL, cost REAL NOT NULL DEFAULT 0,
    tokens_input INTEGER NOT NULL DEFAULT 0,
    tokens_output INTEGER NOT NULL DEFAULT 0,
    tokens_reasoning INTEGER NOT NULL DEFAULT 0,
    tokens_cache_read INTEGER NOT NULL DEFAULT 0,
    tokens_cache_write INTEGER NOT NULL DEFAULT 0,
    parent_id TEXT, agent TEXT, model TEXT,
    time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL,
    time_archived INTEGER
);
CREATE TABLE message (
    id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
    time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL,
    data TEXT NOT NULL
);
CREATE TABLE part (
    id TEXT PRIMARY KEY, message_id TEXT NOT NULL, session_id TEXT NOT NULL,
    time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL,
    data TEXT NOT NULL
);
"""


@pytest.fixture
def mini_db(tmp_path, monkeypatch):
    db = tmp_path / "opencode.db"
    conn = sqlite3.connect(db)
    conn.executescript(MINI_SCHEMA)
    conn.commit()
    conn.close()
    monkeypatch.setenv("OPENCODE_DB", str(db))
    opencode._reset_db_cache()
    return db


def test_mint_id_format_and_order(monkeypatch):
    ms = [1786577389138]
    monkeypatch.setattr(opencode, "_now_ms", lambda: ms[0])
    a = opencode.mint_id("msg")
    b = opencode.mint_id("msg")
    assert a[:16] == "msg_ff84f8652001"          # verified against a live id
    assert a < b                                  # ascending within the ms
    assert len(a) == 4 + 12 + 14


def test_mint_id_descending_sessions(monkeypatch):
    monkeypatch.setattr(opencode, "_now_ms", lambda: 1786577389117)
    s = opencode.mint_id("ses", descending=True)
    assert s[:16] == "ses_007b079c2ffe"          # ~(ms*4096+1), live-verified
    monkeypatch.setattr(opencode, "_now_ms", lambda: 1786577389117 + 5000)
    later = opencode.mint_id("ses", descending=True)
    assert later < s                              # newer sorts smaller


def test_db_path_env_override(mini_db):
    assert opencode.db_path() == mini_db


def test_db_path_none_when_undiscoverable(monkeypatch):
    monkeypatch.delenv("OPENCODE_DB", raising=False)
    opencode._reset_db_cache()
    monkeypatch.setattr(opencode, "_run",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("no binary")))
    assert opencode.db_path() is None


def test_runtime_ready_ok(mini_db):
    ok, reason = opencode.OpencodeAdapter().runtime_ready()
    assert ok, reason


def test_runtime_ready_fails_closed_without_tables(tmp_path, monkeypatch):
    db = tmp_path / "empty.db"
    sqlite3.connect(db).close()
    monkeypatch.setenv("OPENCODE_DB", str(db))
    opencode._reset_db_cache()
    ok, reason = opencode.OpencodeAdapter().runtime_ready()
    assert not ok and "table" in reason


def test_registered_in_adapters():
    from tandem.harness import get_adapter
    assert get_adapter("opencode").id == "opencode"
