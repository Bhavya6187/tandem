import json

from tandem.state import StateStore, SyncCursor


def make_store(tmp_path):
    return StateStore(db_path=tmp_path / "state.db")


def test_create_and_lookup_session(tmp_path):
    with make_store(tmp_path) as store:
        s = store.create_session("/proj", "claude", "c-id", "x-id")
        assert s.shadow == "codex"
        found = store.session_for_cwd("/proj")
        assert found is not None
        assert found.tandem_id == s.tandem_id
        assert found.active == "claude"
        assert found.claude_session_id == "c-id"
        assert store.session_for_cwd("/other") is None


def test_set_active_and_archive(tmp_path):
    with make_store(tmp_path) as store:
        s = store.create_session("/proj", "claude", "c-id", None)
        store.set_active(s.tandem_id, "codex")
        assert store.session_for_cwd("/proj").active == "codex"
        store.set_native_session_id(s.tandem_id, "codex", "new-x")
        assert store.session_for_cwd("/proj").codex_session_id == "new-x"
        store.archive_session(s.tandem_id)
        assert store.session_for_cwd("/proj") is None


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
