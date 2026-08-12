"""Shared fixtures: a paired session under tmp homes with both shadow files
seeded the way a fresh `tandem` launch does, plus fake native entry builders."""

import json
from pathlib import Path

import pytest

from tandem.events import SessionContext
from tandem.state import StateStore
from tandem.util import read_jsonl


def write_line(path, obj=None, text=None):
    with open(path, "a") as f:
        f.write((text if text is not None else json.dumps(obj)) + "\n")


def claude_user(text, uuid=None):
    return {
        "type": "user",
        "uuid": uuid or f"u-{abs(hash(text)) % 10**8}",
        "timestamp": "2026-07-29T00:00:00.000Z",
        "message": {"role": "user", "content": text},
    }


def claude_assistant(blocks, uuid="a-1"):
    return {
        "type": "assistant",
        "uuid": uuid,
        "timestamp": "2026-07-29T00:00:01.000Z",
        "message": {"role": "assistant", "model": "claude-fable-5", "content": blocks},
    }


def claude_tool_result(call_id, output, structured=None):
    return {
        "type": "user",
        "uuid": "tr-1",
        "timestamp": "2026-07-29T00:00:02.000Z",
        "message": {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": call_id, "content": output}
            ],
        },
        "toolUseResult": structured or {"stdout": output, "stderr": ""},
    }


def codex_turn(user_text, assistant_text):
    return [
        {"timestamp": "t", "type": "event_msg",
         "payload": {"type": "task_started", "turn_id": "x"}},
        {"timestamp": "t", "type": "event_msg",
         "payload": {"type": "user_message", "message": user_text}},
        {"timestamp": "t", "type": "response_item",
         "payload": {"type": "message", "role": "assistant", "phase": "final_answer",
                     "content": [{"type": "output_text", "text": assistant_text}]}},
        {"timestamp": "t", "type": "event_msg",
         "payload": {"type": "task_complete", "turn_id": "x",
                     "last_agent_message": assistant_text}},
    ]


def shadow_texts(rollout_path):
    return [
        e["payload"]["content"][0]["text"]
        for e in read_jsonl(rollout_path)
        if e.get("type") == "response_item" and e["payload"].get("type") == "message"
    ]


class Env:
    """Paired session with seeded shadows under tmp homes."""

    def __init__(self, tmp_path, monkeypatch, active="claude"):
        monkeypatch.setenv("TANDEM_HOME", str(tmp_path / ".tandem"))
        monkeypatch.setenv("CODEX_HOME", str(tmp_path / ".codex"))
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / ".claude"))
        # hermetic version checks: run_doctor must not shell out to real CLIs
        from tandem.compat import COMPAT
        from tandem.harness.claude_code import ClaudeCodeAdapter
        from tandem.harness.codex import CodexAdapter

        monkeypatch.setattr(
            ClaudeCodeAdapter, "detect_version", lambda self: COMPAT["claude"].tested
        )
        monkeypatch.setattr(
            CodexAdapter, "detect_version", lambda self: COMPAT["codex"].tested
        )
        self.cwd = str(tmp_path / "proj")
        Path(self.cwd).mkdir()
        self.store = StateStore(db_path=tmp_path / ".tandem" / "state.db")
        from tandem.harness import get_adapter

        claude_sid = "11111111-1111-4111-8111-111111111111"
        codex_sid = "019faca1-0000-7000-8000-000000000001"
        self.session = self.store.create_session(self.cwd, active, claude_sid, codex_sid)
        ctx = SessionContext(
            tandem_id=self.session.tandem_id,
            cwd=self.cwd,
            direction="claude->codex",
            claude_session_id=claude_sid,
            codex_session_id=codex_sid,
        )
        self.codex_shadow = get_adapter("codex").create_shadow_transcript(
            self.cwd, codex_sid, ctx, "[tandem] seed"
        )
        self.claude_shadow = get_adapter("claude").create_shadow_transcript(
            self.cwd, claude_sid, ctx, "[tandem] seed"
        )
        cur = self.store.get_cursor(self.session.tandem_id, "codex")
        cur.pending["claude_leaf_uuid"] = ctx.claude_leaf_uuid
        self.store.save_cursor(cur)
        # a stand-in transcript some tests tail directly
        self.source_file = tmp_path / "active.jsonl"
        self.source_file.touch()

    def refresh(self):
        self.session = self.store.get_session(self.session.tandem_id)
        return self.session

    def loop(self, source="claude", transcript=None):
        from tandem.runner import TailLoop
        from tandem.sync import SyncEngine

        engine = SyncEngine(self.store, self.session, source)
        return (
            TailLoop(self.store, self.session, source,
                     transcript or self.source_file, engine),
            engine,
        )


@pytest.fixture(autouse=True)
def _warm_gate_closed(monkeypatch):
    """No test may boot a hidden harness for real. Under plain `pytest` stdin
    is not a terminal and the warm gate is shut anyway, but `pytest -s` on a
    real terminal leaves it open — so pin it suite-wide, and let the handful
    of tests that exercise the gate reopen it themselves."""
    from tandem import runner

    monkeypatch.setattr(runner, "_stdin_tty", lambda: False)


@pytest.fixture
def env_factory(tmp_path, monkeypatch):
    def make(active="claude"):
        return Env(tmp_path, monkeypatch, active=active)

    return make
