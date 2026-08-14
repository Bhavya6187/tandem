from tandem.state import StateStore


def make_store(tmp_path):
    return StateStore(db_path=tmp_path / "state.db")


def test_create_session_with_participants(tmp_path):
    with StateStore(db_path=tmp_path / "s.db") as store:
        s = store.create_session(
            "/proj", "claude", ["claude", "codex"],
            {"claude": "c-id", "codex": "x-id"},
        )
        assert s.participants == ["claude", "codex"]
        assert s.native_id("claude") == "c-id"
        assert s.next_active("claude") == "codex"
        assert s.targets_for("claude") == ["codex"]
        found = store.get_session(s.tandem_id)
        assert found.native_id("codex") == "x-id"


def test_three_way_cycle(tmp_path):
    with StateStore(db_path=tmp_path / "s.db") as store:
        s = store.create_session(
            "/proj", "claude", ["claude", "codex", "opencode"],
            {"claude": "c", "codex": None, "opencode": "ses_x"},
        )
        assert s.next_active("claude") == "codex"
        assert s.next_active("codex") == "opencode"
        assert s.next_active("opencode") == "claude"
        assert s.targets_for("codex") == ["claude", "opencode"]


def test_set_participants_persists(tmp_path):
    with StateStore(db_path=tmp_path / "s.db") as store:
        s = store.create_session(
            "/proj", "claude", ["claude", "codex", "opencode"],
            {"claude": "c", "codex": "x", "opencode": "o"},
        )
        store.set_participants(s.tandem_id, ["claude", "codex"])
        assert store.get_session(s.tandem_id).participants == ["claude", "codex"]


def test_cursor_keyed_by_direction(tmp_path):
    with StateStore(db_path=tmp_path / "s.db") as store:
        s = store.create_session("/proj", "claude", ["claude", "codex"],
                                 {"claude": "c", "codex": "x"})
        a = store.get_cursor(s.tandem_id, "claude", "codex")
        a.byte_offset = 10
        store.save_cursor(a)
        b = store.get_cursor(s.tandem_id, "claude", "opencode")
        assert b.byte_offset == 0          # independent direction
        assert store.get_cursor(s.tandem_id, "claude", "codex").byte_offset == 10


def test_old_schema_recreated(tmp_path):
    import sqlite3
    db = tmp_path / "s.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE sessions (tandem_id TEXT PRIMARY KEY, cwd TEXT,"
                 " active TEXT, claude_session_id TEXT, codex_session_id TEXT,"
                 " created_at TEXT)")
    conn.execute("INSERT INTO sessions VALUES ('old', '/p', 'claude', 'c', 'x', 't')")
    conn.commit()
    conn.close()
    with StateStore(db_path=db) as store:
        assert store.get_session("old") is None      # fresh DB
    assert (tmp_path / "s.db.old").exists()          # moved aside, not deleted


def test_create_and_lookup_session(tmp_path):
    with make_store(tmp_path) as store:
        s = store.create_session("/proj", "claude", ["claude", "codex"],
                                 {"claude": "c-id", "codex": "x-id"})
        found = store.latest_session_for_cwd("/proj")
        assert found is not None
        assert found.tandem_id == s.tandem_id
        assert found.active == "claude"
        assert found.native_id("claude") == "c-id"
        assert store.latest_session_for_cwd("/other") is None


def test_set_active(tmp_path):
    with make_store(tmp_path) as store:
        s = store.create_session("/proj", "claude", ["claude", "codex"],
                                 {"claude": "c-id", "codex": None})
        store.set_active(s.tandem_id, "codex")
        assert store.latest_session_for_cwd("/proj").active == "codex"
        store.set_native_session_id(s.tandem_id, "codex", "new-x")
        assert store.latest_session_for_cwd("/proj").native_id("codex") == "new-x"


def test_cursor_roundtrip_and_upsert(tmp_path):
    with make_store(tmp_path) as store:
        s = store.create_session("/proj", "claude", ["claude", "codex"],
                                 {"claude": "c-id", "codex": "x-id"})
        fresh = store.get_cursor(s.tandem_id, "claude", "codex")
        assert fresh.byte_offset == 0 and fresh.pending == {}

        fresh.byte_offset = 1024
        fresh.line_index = 7
        fresh.turn_index = 2
        fresh.pending = {"call_1": {"tool": "Bash"}}
        store.save_cursor(fresh)

        again = store.get_cursor(s.tandem_id, "claude", "codex")
        assert again.byte_offset == 1024
        assert again.line_index == 7
        assert again.pending["call_1"]["tool"] == "Bash"

        again.failed_turns = 3
        store.save_cursor(again)
        assert store.get_cursor(s.tandem_id, "claude", "codex").failed_turns == 3


def test_cursor_survives_reopen(tmp_path):
    with make_store(tmp_path) as store:
        s = store.create_session("/proj", "codex", ["claude", "codex"],
                                 {"claude": "c-id", "codex": "x-id"})
        cur = store.get_cursor(s.tandem_id, "codex", "claude")
        cur.line_index = 42
        store.save_cursor(cur)
        tid = s.tandem_id
    with make_store(tmp_path) as store:
        assert store.get_cursor(tid, "codex", "claude").line_index == 42


def test_create_sets_last_used(tmp_path):
    with make_store(tmp_path) as store:
        s = store.create_session("/proj", "claude", ["claude", "codex"],
                                 {"claude": "c-id", "codex": "x-id"})
        assert s.last_used_at == s.created_at


def test_latest_prefers_most_recently_used(tmp_path):
    with make_store(tmp_path) as store:
        s1 = store.create_session("/proj", "claude", ["claude", "codex"],
                                  {"claude": "c-1", "codex": "x-1"})
        s2 = store.create_session("/proj", "claude", ["claude", "codex"],
                                  {"claude": "c-2", "codex": "x-2"})
        assert store.latest_session_for_cwd("/proj").tandem_id == s2.tandem_id
        store.touch_used(s1.tandem_id)
        assert store.latest_session_for_cwd("/proj").tandem_id == s1.tandem_id
        # both sessions coexist for one cwd
        assert store.get_session(s2.tandem_id) is not None


def test_latest_is_immune_to_null_last_used(tmp_path):
    """A row whose last_used_at is NULL (e.g. written by a crashed process)
    must fall back to created_at, not sort behind every older session."""
    with make_store(tmp_path) as store:
        older = store.create_session("/proj", "claude", ["claude", "codex"],
                                     {"claude": "c-1", "codex": "x-1"})
        newer = store.create_session("/proj", "claude", ["claude", "codex"],
                                     {"claude": "c-2", "codex": "x-2"})
        store._conn.execute(
            "UPDATE sessions SET last_used_at = ?, created_at = ?"
            " WHERE tandem_id = ?",
            ("2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00",
             older.tandem_id),
        )
        store._conn.execute(
            "UPDATE sessions SET last_used_at = NULL, created_at = ?"
            " WHERE tandem_id = ?",
            ("2026-06-01T00:00:00+00:00", newer.tandem_id),
        )
        store._conn.commit()
        assert store.latest_session_for_cwd("/proj").tandem_id == newer.tandem_id
