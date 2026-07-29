import json
import sqlite3

from tandem.state import StateStore, SyncCursor


def make_store(tmp_path):
    return StateStore(db_path=tmp_path / "state.db")


def test_create_and_lookup_session(tmp_path):
    with make_store(tmp_path) as store:
        s = store.create_session("/proj", "claude", "c-id", "x-id")
        assert s.shadow == "codex"
        found = store.latest_session_for_cwd("/proj")
        assert found is not None
        assert found.tandem_id == s.tandem_id
        assert found.active == "claude"
        assert found.claude_session_id == "c-id"
        assert store.latest_session_for_cwd("/other") is None


def test_set_active(tmp_path):
    with make_store(tmp_path) as store:
        s = store.create_session("/proj", "claude", "c-id", None)
        store.set_active(s.tandem_id, "codex")
        assert store.latest_session_for_cwd("/proj").active == "codex"
        store.set_native_session_id(s.tandem_id, "codex", "new-x")
        assert store.latest_session_for_cwd("/proj").codex_session_id == "new-x"


def test_archive_session_is_a_harmless_noop(tmp_path):
    # Archiving is vestigial: the `archived` column is gone from the schema and
    # no query filters on it. It must not crash on a fresh DB (Task 2 removes it
    # along with `start`).
    with make_store(tmp_path) as store:
        s = store.create_session("/proj", "claude", "c-id", "x-id")
        store.archive_session(s.tandem_id)
        assert store.latest_session_for_cwd("/proj").tandem_id == s.tandem_id


def test_cursor_roundtrip_and_upsert(tmp_path):
    with make_store(tmp_path) as store:
        s = store.create_session("/proj", "claude", "c-id", "x-id")
        fresh = store.get_cursor(s.tandem_id, "claude")
        assert fresh.byte_offset == 0 and fresh.pending == {}

        fresh.byte_offset = 1024
        fresh.line_index = 7
        fresh.turn_index = 2
        fresh.pending = {"call_1": {"tool": "Bash"}}
        store.save_cursor(fresh)

        again = store.get_cursor(s.tandem_id, "claude")
        assert again.byte_offset == 1024
        assert again.line_index == 7
        assert again.pending["call_1"]["tool"] == "Bash"

        again.failed_turns = 3
        store.save_cursor(again)
        assert store.get_cursor(s.tandem_id, "claude").failed_turns == 3


def test_cursor_survives_reopen(tmp_path):
    with make_store(tmp_path) as store:
        s = store.create_session("/proj", "codex", "c-id", "x-id")
        cur = store.get_cursor(s.tandem_id, "codex")
        cur.line_index = 42
        store.save_cursor(cur)
        tid = s.tandem_id
    with make_store(tmp_path) as store:
        assert store.get_cursor(tid, "codex").line_index == 42


def test_create_sets_last_used(tmp_path):
    with make_store(tmp_path) as store:
        s = store.create_session("/proj", "claude", "c-id", "x-id")
        assert s.last_used_at == s.created_at


def test_latest_prefers_most_recently_used(tmp_path):
    with make_store(tmp_path) as store:
        s1 = store.create_session("/proj", "claude", "c-1", "x-1")
        s2 = store.create_session("/proj", "claude", "c-2", "x-2")
        assert store.latest_session_for_cwd("/proj").tandem_id == s2.tandem_id
        store.touch_used(s1.tandem_id)
        assert store.latest_session_for_cwd("/proj").tandem_id == s1.tandem_id
        # both sessions coexist for one cwd
        assert store.get_session(s2.tandem_id) is not None


V1_SCHEMA = """
CREATE TABLE sessions (
    tandem_id TEXT PRIMARY KEY,
    cwd TEXT NOT NULL,
    active TEXT NOT NULL CHECK (active IN ('claude', 'codex')),
    claude_session_id TEXT,
    codex_session_id TEXT,
    created_at TEXT NOT NULL,
    last_sync_at TEXT,
    archived INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE sync_cursors (
    tandem_id TEXT NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('claude', 'codex')),
    byte_offset INTEGER NOT NULL DEFAULT 0,
    line_index INTEGER NOT NULL DEFAULT 0,
    turn_index INTEGER NOT NULL DEFAULT 0,
    pending TEXT NOT NULL DEFAULT '{}',
    failed_turns INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT,
    PRIMARY KEY (tandem_id, source)
);
"""


def test_v1_db_migrates_in_place(tmp_path):
    db = tmp_path / "state.db"
    conn = sqlite3.connect(db)
    conn.executescript(V1_SCHEMA)
    conn.execute(
        "INSERT INTO sessions (tandem_id, cwd, active, claude_session_id,"
        " codex_session_id, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        ("oldid0000001", "/proj", "claude", "c-id", "x-id",
         "2026-01-01T00:00:00+00:00"),
    )
    conn.commit()
    conn.close()
    with StateStore(db_path=db) as store:
        s = store.latest_session_for_cwd("/proj")
        assert s is not None and s.tandem_id == "oldid0000001"
        # last_used_at backfilled from created_at
        assert s.last_used_at == "2026-01-01T00:00:00+00:00"
        store.touch_used(s.tandem_id)
        assert store.get_session(s.tandem_id).last_used_at != s.last_used_at
