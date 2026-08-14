"""Opencode adapter units against a hermetic mini-DB (no binary needed)."""

import json
import sqlite3
from pathlib import Path

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


def _insert_session(db, sid="ses_00test0000000000000000000", cwd="/proj"):
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO project VALUES ('p1', ?)", (cwd,))
        conn.execute(
            "INSERT INTO session (id, project_id, slug, directory, path, title,"
            " version, time_created, time_updated) VALUES (?, 'p1', 's', ?, '',"
            " 't', '1.18.15', 1, 1)", (sid, cwd))
    return sid


def _insert_message(db, sid, msg_id, data, t):
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO message VALUES (?, ?, ?, ?, ?)",
                     (msg_id, sid, t, t, json.dumps(data)))


def _insert_part(db, sid, msg_id, part_id, data, t=1):
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO part VALUES (?, ?, ?, ?, ?, ?)",
                     (part_id, msg_id, sid, t, t, json.dumps(data)))


def _seed_turn(db, sid, complete=True, provider="openai"):
    """user('is the readme updated?') + assistant(reasoning, text, tool,
    step parts) — shapes lifted from the operator's real 1.18.15 session."""
    _insert_message(db, sid, "msg_aa0000000001x", {
        "role": "user", "time": {"created": 1000}, "agent": "build",
        "model": {"providerID": provider, "modelID": "gpt-5.6-sol"}}, 1000)
    _insert_part(db, sid, "msg_aa0000000001x", "prt_aa0000000001x",
                 {"type": "text", "text": "is the readme updated?"})
    adata = {
        "parentID": "msg_aa0000000001x", "role": "assistant", "mode": "build",
        "agent": "build", "path": {"cwd": "/proj", "root": "/proj"}, "cost": 0,
        "tokens": {"total": 1, "input": 1, "output": 1, "reasoning": 0,
                   "cache": {"write": 0, "read": 0}},
        "modelID": "gpt-5.6-sol", "providerID": provider,
        "time": {"created": 2000},
    }
    if complete:
        adata["time"]["completed"] = 2500
        adata["finish"] = "stop"
    _insert_message(db, sid, "msg_ab0000000001x", adata, 2000)
    for pid, pdata in [
        ("prt_ab0000000001x", {"type": "step-start", "snapshot": "abc"}),
        ("prt_ab0000000002x", {"type": "reasoning",
                               "text": "**Inspecting git state**"}),
        ("prt_ab0000000003x", {"type": "tool", "tool": "bash",
                               "callID": "call_1",
                               "state": {"status": "completed",
                                         "input": {"command": "git status"},
                                         "output": "(no output)",
                                         "metadata": {}, "title": "git status",
                                         "time": {"start": 1, "end": 2}}}),
        ("prt_ab0000000004x", {"type": "text", "text": "Yes, it is current."}),
        ("prt_ab0000000005x", {"type": "step-finish", "reason": "stop",
                               "tokens": {}, "cost": 0}),
    ]:
        _insert_part(db, sid, "msg_ab0000000001x", pid, pdata)


def _cursor():
    from tandem.state import SyncCursor
    return SyncCursor(tandem_id="t", source="opencode", target="claude")


def test_reader_yields_completed_turn(mini_db):
    sid = _insert_session(mini_db)
    _seed_turn(mini_db, sid, complete=True)
    cur = _cursor()
    reader = opencode.OpencodeTurnReader(sid, mini_db, cur)
    lines = reader.poll()
    assert len(lines) == 1
    turn = lines[0].raw
    assert turn["user"]["parts"][0]["text"] == "is the readme updated?"
    assert len(turn["assistants"]) == 1
    assert lines[0].pos == {"time": 2000, "id": "msg_ab0000000001x"}
    # committing the pos means a second poll yields nothing
    lines[0].advance(cur)
    assert opencode.OpencodeTurnReader(sid, mini_db, cur).poll() == []


def test_reader_holds_incomplete_turn(mini_db):
    sid = _insert_session(mini_db)
    _seed_turn(mini_db, sid, complete=False)
    reader = opencode.OpencodeTurnReader(sid, mini_db, _cursor())
    assert reader.poll() == []


def test_parse_entry_maps_part_types(mini_db):
    sid = _insert_session(mini_db)
    _seed_turn(mini_db, sid, complete=True)
    reader = opencode.OpencodeTurnReader(sid, mini_db, _cursor())
    turn = reader.poll()[0].raw
    from tandem.events import SessionContext
    ctx = SessionContext(tandem_id="t", cwd="/proj",
                         direction="opencode->claude")
    events = opencode.OpencodeAdapter().parse_entry(turn, ctx)
    kinds = [e.kind for e in events]
    assert kinds == ["user_message", "system", "thinking", "tool_call",
                     "tool_result", "assistant_message", "system"]
    call = events[3]
    result = events[4]
    assert call.tool == "bash" and call.call_id == "call_1"
    assert result.output == "(no output)" and result.call_id == "call_1"
    assert events[0].turn_index == 1        # user message bumps the turn


def test_parse_entry_skips_tandem_echo(mini_db):
    sid = _insert_session(mini_db)
    _seed_turn(mini_db, sid, complete=True, provider="tandem")
    reader = opencode.OpencodeTurnReader(sid, mini_db, _cursor())
    turn = reader.poll()[0].raw
    from tandem.events import SessionContext
    ctx = SessionContext(tandem_id="t", cwd="/proj",
                         direction="opencode->claude")
    events = opencode.OpencodeAdapter().parse_entry(turn, ctx)
    assert [e.kind for e in events] == ["system"]
    assert events[0].subtype == "tandem_echo"


def test_session_status_probe(mini_db):
    sid = _insert_session(mini_db)
    adapter = opencode.OpencodeAdapter()
    _seed_turn(mini_db, sid, complete=False)
    assert adapter.session_status(sid) == "busy"
    with sqlite3.connect(mini_db) as conn:
        conn.execute(
            "UPDATE message SET data = json_set(data,"
            " '$.time.completed', 2500, '$.finish', 'stop')"
            " WHERE id = 'msg_ab0000000001x'")
    assert adapter.session_status(sid) == "waiting"


def _render_ctx(sid="ses_00test0000000000000000000"):
    from tandem.events import SessionContext
    return SessionContext(tandem_id="t", cwd="/proj",
                          direction="claude->opencode",
                          target_session_id=sid)


def _events_turn():
    from tandem.events import AssistantMessage, ToolCall, ToolResult, UserMessage
    return [
        UserMessage(source="user", turn_index=1, text="[via claude-code] hi"),
        ToolCall(source="claude", turn_index=1, call_id="c1", tool="Bash",
                 arguments={"command": "ls"}),
        ToolResult(source="claude", turn_index=1, call_id="c1", output="file.txt"),
        AssistantMessage(source="claude", turn_index=1,
                         text="[via claude-code] done", model="claude-fable-5"),
    ]


def test_render_and_append_turn(mini_db):
    sid = _insert_session(mini_db)
    adapter = opencode.OpencodeAdapter()
    ctx = _render_ctx(sid)
    entries = adapter.render_events(_events_turn(), ctx)
    adapter.shadow_append(mini_db, entries)
    with sqlite3.connect(mini_db) as conn:
        conn.row_factory = sqlite3.Row
        msgs = conn.execute(
            "SELECT id, data FROM message WHERE session_id = ?"
            " ORDER BY time_created, id", (sid,)).fetchall()
    datas = [json.loads(m["data"]) for m in msgs]
    assert [d["role"] for d in datas] == ["user", "assistant"]
    assert datas[0]["agent"] and datas[0]["model"]          # schema-required
    assert datas[1]["providerID"] == "tandem"               # sentinel
    assert datas[1]["modelID"] == "claude-fable-5"
    assert datas[1]["time"]["completed"] and datas[1]["finish"] == "stop"
    with sqlite3.connect(mini_db) as conn:
        parts = conn.execute(
            "SELECT data FROM part WHERE message_id = ? ORDER BY id",
            (msgs[1]["id"],)).fetchall()
    pdatas = [json.loads(p[0]) for p in parts]
    assert [p["type"] for p in pdatas] == ["tool", "text"]
    assert pdatas[0]["state"]["status"] == "completed"
    assert pdatas[0]["callID"] == "c1"


def test_append_is_idempotent_by_ids(mini_db):
    sid = _insert_session(mini_db)
    adapter = opencode.OpencodeAdapter()
    entries = adapter.render_events(_events_turn(), _render_ctx(sid))
    adapter.shadow_append(mini_db, entries)
    adapter.shadow_append(mini_db, entries)      # crash replay
    with sqlite3.connect(mini_db) as conn:
        n = conn.execute("SELECT COUNT(*) FROM message WHERE session_id = ?",
                         (sid,)).fetchone()[0]
    assert n == 2                                # not 4


def test_intent_landed_checks_ids(mini_db):
    sid = _insert_session(mini_db)
    adapter = opencode.OpencodeAdapter()
    entries = adapter.render_events(_events_turn(), _render_ctx(sid))
    intent = adapter.shadow_intent(mini_db, entries)
    assert not adapter.intent_landed(mini_db, intent)
    adapter.shadow_append(mini_db, entries)
    assert adapter.intent_landed(mini_db, intent)


def test_written_turn_reads_back_as_echo(mini_db):
    """The sentinel closes the loop: what tandem writes, the reader skips."""
    sid = _insert_session(mini_db)
    adapter = opencode.OpencodeAdapter()
    adapter.shadow_append(
        mini_db, adapter.render_events(_events_turn(), _render_ctx(sid)))
    reader = opencode.OpencodeTurnReader(sid, mini_db, _cursor())
    lines = reader.poll()
    assert len(lines) == 1
    from tandem.events import SessionContext
    ctx = SessionContext(tandem_id="t", cwd="/proj",
                         direction="opencode->claude")
    events = adapter.parse_entry(lines[0].raw, ctx)
    assert [e.kind for e in events] == ["system"]
    assert events[0].subtype == "tandem_echo"


def test_placeholder_renders_closed(mini_db):
    sid = _insert_session(mini_db)
    adapter = opencode.OpencodeAdapter()
    entries = adapter.render_placeholder("[tandem: turn 3 could not be"
                                         " translated]", _render_ctx(sid))
    adapter.shadow_append(mini_db, entries)
    assert adapter.session_status(sid) == "waiting"   # never stuck "working"


def test_transcript_path_requires_session_row(mini_db):
    adapter = opencode.OpencodeAdapter()
    assert adapter.transcript_path("/proj", "ses_missing") is None
    sid = _insert_session(mini_db)
    assert adapter.transcript_path("/proj", sid) == mini_db


def test_create_shadow_via_import(mini_db, tmp_path, monkeypatch):
    """The import subprocess is simulated: assert the payload shape, then
    perform the inserts import would do."""
    adapter = opencode.OpencodeAdapter()
    captured = {}

    def fake_run(argv, **kwargs):
        assert argv[:3] == ["opencode", "import", argv[2]]
        payload = json.loads(Path(argv[2]).read_text())
        captured["payload"] = payload
        info = payload["info"]
        with sqlite3.connect(mini_db) as conn:
            conn.execute("INSERT OR IGNORE INTO project VALUES ('p1', ?)",
                         (kwargs.get("cwd"),))
            conn.execute(
                "INSERT INTO session (id, project_id, slug, directory, path,"
                " title, version, time_created, time_updated)"
                " VALUES (?, 'p1', 'slug', ?, '', ?, ?, ?, ?)",
                (info["id"], kwargs.get("cwd"), info["title"],
                 info["version"], info["time"]["created"],
                 info["time"]["updated"]))
            for m in payload["messages"]:
                mi = m["info"]
                conn.execute(
                    "INSERT INTO message VALUES (?, ?, ?, ?, ?)",
                    (mi["id"], info["id"], mi["time"]["created"],
                     mi["time"]["created"],
                     json.dumps({k: v for k, v in mi.items()
                                 if k not in ("id", "sessionID")})))
                for p in m["parts"]:
                    conn.execute(
                        "INSERT INTO part VALUES (?, ?, ?, ?, ?, ?)",
                        (p["id"], mi["id"], info["id"], 1, 1,
                         json.dumps({k: v for k, v in p.items()
                                     if k not in ("id", "sessionID",
                                                  "messageID")})))
        import subprocess
        return subprocess.CompletedProcess(argv, 0, stdout="Imported", stderr="")

    monkeypatch.setattr(opencode, "_run", fake_run)
    sid = adapter.mint_session_id()
    ctx = _render_ctx(sid)
    path = adapter.create_shadow_transcript(str(tmp_path), sid, ctx,
                                            "[tandem] seed note")
    assert path == mini_db
    payload = captured["payload"]
    info = payload["info"]
    assert info["id"] == sid
    assert "agent" not in info and "model" not in info     # session-level omitted
    user, assistant = (m["info"] for m in payload["messages"])
    assert user["agent"] == "build"                        # message-level REQUIRED
    assert user["model"] == {"providerID": "tandem", "modelID": "<synced>"}
    assert assistant["providerID"] == "tandem"
    assert assistant["time"]["completed"] and assistant["finish"] == "stop"
    assert ctx.state_for("opencode")["turn_user_id"] == user["id"]
    assert adapter.session_status(sid) == "waiting"        # lists as idle


def test_create_shadow_raises_on_import_failure(mini_db, tmp_path, monkeypatch):
    import subprocess
    monkeypatch.setattr(
        opencode, "_run",
        lambda argv, **k: subprocess.CompletedProcess(argv, 1, stdout="",
                                                      stderr="boom"))
    adapter = opencode.OpencodeAdapter()
    with pytest.raises(RuntimeError, match="boom"):
        adapter.create_shadow_transcript(str(tmp_path), adapter.mint_session_id(),
                                         _render_ctx(), "[tandem] seed")


def test_launch_surface():
    adapter = opencode.OpencodeAdapter()
    assert adapter.interactive_argv("ses_x", fresh=False) == \
        ["opencode", "-s", "ses_x"]
    assert adapter.oneoff_argv("ses_x", "hi") == \
        ["opencode", "run", "-s", "ses_x", "hi"]
    assert adapter.hook_argv_extra(Path("/tmp/s")) == []
    assert adapter.quit_keystrokes() == [b"\x03", b"\x03"]
