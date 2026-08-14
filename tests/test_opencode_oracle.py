"""Round-trips through the REAL opencode binary (skip-if-missing).

Isolated via OPENCODE_DB -> a temp database that opencode migrates into
existence on first run; the operator's real DB is never touched.
"""

import json
import os
import shutil
import subprocess

import pytest

from tandem import compat
from tandem.harness import opencode

_version = compat.detect_cli_version("opencode")
pytestmark = pytest.mark.skipif(
    shutil.which("opencode") is None
    or _version is None
    or not compat.version_supported("opencode", _version),
    reason="no supported opencode binary on PATH",
)


@pytest.fixture
def oracle_env(tmp_path, monkeypatch):
    db = tmp_path / "oracle.db"
    monkeypatch.setenv("OPENCODE_DB", str(db))
    opencode._reset_db_cache()
    cwd = tmp_path / "proj"
    cwd.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=cwd, check=True)
    env = dict(os.environ, OPENCODE_DB=str(db))
    return db, str(cwd), env


def _export(sid, cwd, env):
    out = subprocess.run(["opencode", "export", sid], cwd=cwd, env=env,
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_import_birth_roundtrips_through_export(oracle_env):
    db, cwd, env = oracle_env
    adapter = opencode.OpencodeAdapter()
    from tandem.events import SessionContext
    sid = adapter.mint_session_id()
    ctx = SessionContext(tandem_id="t", cwd=cwd,
                         direction="claude->opencode",
                         target_session_id=sid)
    path = adapter.create_shadow_transcript(cwd, sid, ctx, "[tandem] oracle seed")
    assert path == db
    data = _export(sid, cwd, env)
    assert data["info"]["id"] == sid
    texts = [p.get("text", "") for m in data["messages"] for p in m["parts"]]
    assert any("oracle seed" in t for t in texts)


def test_synced_turn_survives_export(oracle_env):
    db, cwd, env = oracle_env
    adapter = opencode.OpencodeAdapter()
    from tandem.events import (AssistantMessage, SessionContext, ToolCall,
                               ToolResult, UserMessage)
    sid = adapter.mint_session_id()
    ctx = SessionContext(tandem_id="t", cwd=cwd,
                         direction="claude->opencode",
                         target_session_id=sid)
    adapter.create_shadow_transcript(cwd, sid, ctx, "[tandem] oracle seed")
    events = [
        UserMessage(source="user", turn_index=1, text="[via claude-code] hello"),
        ToolCall(source="claude", turn_index=1, call_id="c1", tool="Bash",
                 arguments={"command": "true"}),
        ToolResult(source="claude", turn_index=1, call_id="c1", output="ok"),
        AssistantMessage(source="claude", turn_index=1,
                         text="[via claude-code] done", model="claude-fable-5"),
    ]
    adapter.shadow_append(db, adapter.render_events(events, ctx))
    data = _export(sid, cwd, env)
    texts = [p.get("text", "") for m in data["messages"] for p in m["parts"]]
    assert any("hello" in t for t in texts)
    assert any("done" in t for t in texts)
    roles = [m["info"]["role"] for m in data["messages"]]
    assert roles[-1] == "assistant"


def test_session_listed(oracle_env):
    db, cwd, env = oracle_env
    adapter = opencode.OpencodeAdapter()
    from tandem.events import SessionContext
    sid = adapter.mint_session_id()
    ctx = SessionContext(tandem_id="t", cwd=cwd,
                         direction="claude->opencode",
                         target_session_id=sid)
    adapter.create_shadow_transcript(cwd, sid, ctx, "[tandem] oracle seed")
    out = subprocess.run(["opencode", "session", "list"], cwd=cwd, env=env,
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    assert sid in out.stdout
