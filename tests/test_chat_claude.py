import json
import os
import signal
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from tandem.chat.events import (ApprovalRequest, QuestionRequest, TextDelta, ThinkingDelta,
                                ToolFinished, ToolOutput, ToolStarted, TurnFinished)
from tandem.chat.runtime.claude import ClaudeRuntime
from tandem.config import ChatConfig

FAKE = Path(__file__).parent / "fakes" / "fake_claude.py"
GOLDEN = Path(__file__).parent / "golden" / "chat" / "claude_stream.jsonl"


class Recorder:
    def __init__(self, approve="allow", answer="red"):
        self.events = []
        self.approvals = []
        self.questions = []
        self._approve = approve
        self._answer = answer

    def emit(self, ev):
        self.events.append(ev)

    def approve(self, req):
        self.approvals.append(req)
        return self._approve

    def answer(self, req):
        self.questions.append(req)
        return self._answer

    def kinds(self):
        return [type(e).__name__ for e in self.events]


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / ".claude"))
    monkeypatch.setenv("FAKE_ARGV_OUT", str(tmp_path / "argv.json"))
    monkeypatch.setenv("FAKE_REPLY_OUT", str(tmp_path / "reply.json"))
    monkeypatch.setenv("FAKE_PROMPT_OUT", str(tmp_path / "prompt.txt"))
    proj = tmp_path / "proj"; proj.mkdir()
    return SimpleNamespace(tmp=tmp_path, session=SimpleNamespace(cwd=str(proj), tandem_id="tdm-claude"),
                           runtime=ClaudeRuntime(ChatConfig(), binary=[sys.executable, str(FAKE)]))


def test_argv_skip_permissions_bypasses_but_keeps_the_prompt_tool():
    argv = ClaudeRuntime(ChatConfig(skip_permissions=True)).argv("sid-1", fresh=False, model="")
    i = argv.index("--permission-mode")
    assert argv[i + 1] == "bypassPermissions"
    # AskUserQuestion still arrives as a can_use_tool request over stdio
    assert argv[argv.index("--permission-prompt-tool") + 1] == "stdio"


def test_argv_has_no_permission_mode_by_default():
    assert "--permission-mode" not in ClaudeRuntime(ChatConfig()).argv("sid-1", fresh=False, model="")


def test_argv_fresh_vs_resume():
    # the default one-word binary, so the flags sit at the indices below
    rt = ClaudeRuntime(ChatConfig())
    fresh = rt.argv("sid-1", fresh=True, model="")
    assert fresh[:2] == ["claude", "-p"]
    assert fresh[2:4] == ["--session-id", "sid-1"]
    assert "--resume" not in fresh
    resumed = rt.argv("sid-1", fresh=False, model="haiku")
    assert resumed[2:4] == ["--resume", "sid-1"]
    assert resumed[-2:] == ["--model", "haiku"]
    for flag in ("--input-format", "--output-format", "--verbose", "--include-partial-messages",
                 "--permission-prompt-tool", "--setting-sources"):
        assert flag in resumed
    i = resumed.index("--setting-sources")
    assert resumed[i + 1] == "user,project,local"


def test_approve_flow_allow(env):
    rec = Recorder("allow")
    out = env.runtime.run_turn(env.session, "sid-1", "make x", "", rec.emit, rec)
    assert out.status == "completed"
    assert rec.approvals == [ApprovalRequest("command", "Bash touch x.txt")]
    assert rec.kinds() == ["ToolStarted", "ToolOutput", "ToolFinished", "TextDelta", "TurnFinished"]
    started = rec.events[0]
    assert started == ToolStarted("toolu_1", "Bash", "touch x.txt")
    assert rec.events[1] == ToolOutput("toolu_1", "ok\nline2\nline3")
    assert rec.events[2] == ToolFinished("toolu_1", True, "")
    assert rec.events[3] == TextDelta("DONE")
    assert rec.events[4] == TurnFinished("completed", "2 turns")
    reply = json.loads((env.tmp / "reply.json").read_text())
    assert reply == {"behavior": "allow", "updatedInput": {"command": "touch x.txt", "description": "make x"}}
    assert (env.tmp / "prompt.txt").read_text() == "make x"
    argv = json.loads((env.tmp / "argv.json").read_text())
    assert argv[:3] == ["-p", "--session-id", "sid-1"]     # no transcript yet -> fresh


def test_resume_when_transcript_exists(env):
    from tandem import paths
    p = paths.claude_transcript_path(env.session.cwd, "sid-2"); p.parent.mkdir(parents=True); p.write_text("{}\n")
    rec = Recorder("allow")
    env.runtime.run_turn(env.session, "sid-2", "go", "", rec.emit, rec)
    argv = json.loads((env.tmp / "argv.json").read_text())
    assert argv[:3] == ["-p", "--resume", "sid-2"]


def test_approve_flow_always_adds_session_rule(env):
    rec = Recorder("always")
    env.runtime.run_turn(env.session, "sid-1", "make x", "", rec.emit, rec)
    reply = json.loads((env.tmp / "reply.json").read_text())
    assert reply["behavior"] == "allow"
    assert reply["updatedPermissions"] == [{"type": "addRules", "rules": [{"toolName": "Bash", "ruleContent": "touch x.txt"}],
                                            "behavior": "allow", "destination": "session"}]


def test_approve_flow_deny(env):
    rec = Recorder("deny")
    out = env.runtime.run_turn(env.session, "sid-1", "make x", "", rec.emit, rec)
    assert out.status == "completed"       # the model carried on after the denial
    reply = json.loads((env.tmp / "reply.json").read_text())
    assert reply == {"behavior": "deny", "message": "denied in tandem chat"}
    assert ToolFinished("toolu_1", False, "denied in tandem chat") in rec.events


def test_text_only_turn(env, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", "text")
    rec = Recorder()
    out = env.runtime.run_turn(env.session, "sid-1", "hi", "", rec.emit, rec)
    assert out.status == "completed"
    assert [e for e in rec.events if isinstance(e, TextDelta)] == [TextDelta("hello "), TextDelta("world")]


def test_question_round_trip(env, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", "question")
    rec = Recorder(answer="blue")
    out = env.runtime.run_turn(env.session, "sid-1", "ask me", "", rec.emit, rec)
    assert out.status == "completed"
    assert rec.questions == [QuestionRequest("Which color?", ("red", "blue"))]
    assert TextDelta("you chose blue") in rec.events


def test_crash_is_failed_with_stderr(env, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", "crash")
    rec = Recorder()
    out = env.runtime.run_turn(env.session, "sid-1", "hi", "", rec.emit, rec)
    assert out.status == "failed"
    assert "boom" in out.error
    assert rec.events[-1] == TurnFinished("failed", "")


def test_child_death_during_approval_returns_an_outcome(env):
    """`_approval` blocks for however long the human takes, and claude can die in
    that window (rate-limited out, OOM-killed, killed by hand). The write of the
    control_response then breaks, which must not escape as an exception: the
    caller is owed a TurnOutcome and the window is owed a terminal event."""
    rt = env.runtime

    class KillsTheChild(Recorder):
        def approve(self, req):
            proc = rt._proc                     # deliberate: stands in for an external kill
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=5)                # dead read end, so the write is deterministic
            return super().approve(req)

    rec = KillsTheChild("allow")
    out = rt.run_turn(env.session, "sid-1", "make x", "", rec.emit, rec)
    assert out.status == "failed"
    assert "claude exited" in out.error
    assert rec.events[-1] == TurnFinished("failed", "")


def test_interrupt_marks_turn_interrupted(env, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", "interrupt")
    rec = Recorder()
    rt = env.runtime
    holder = {}

    def run():
        holder["out"] = rt.run_turn(env.session, "sid-1", "hi", "", rec.emit, rec)

    t = threading.Thread(target=run); t.start()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not any(isinstance(e, TextDelta) for e in rec.events):
        time.sleep(0.02)
    rt.interrupt()
    t.join(10)
    assert holder["out"].status == "interrupted"


def test_golden_lines_drive_the_parser():
    rec = Recorder("allow")
    sent = []
    rt = ClaudeRuntime(ChatConfig(show_thinking=True))
    outcome = None
    for line in GOLDEN.read_text().splitlines():
        got = rt.handle_line(json.loads(line), rec.emit, rec, sent.append)
        outcome = got or outcome
    assert outcome is not None and outcome.status == "completed"
    assert rec.kinds() == ["ThinkingDelta", "ToolStarted", "ToolOutput", "ToolFinished", "TextDelta", "TurnFinished"]
    assert rec.events[0] == ThinkingDelta("I should run it.")
    assert rec.events[1] == ToolStarted("toolu_01EdBuo5aF5Cjkj7NbwFrLeC", "Bash", "touch fixture-claude.txt")
    assert sent[0]["type"] == "control_response" and sent[0]["response"]["request_id"] == "16c61d1a-b82a-4c4b-8a21-1f3298f9eaaf"


def test_child_is_told_which_session_it_belongs_to(env, monkeypatch):
    from tandem.chat.runtime import claude as mod

    seen = []
    real = mod.subprocess.Popen
    monkeypatch.setattr(mod.subprocess, "Popen",
                        lambda *a, **kw: (seen.append(kw["env"]), real(*a, **kw))[1])
    rec = Recorder("allow")
    env.runtime.run_turn(env.session, "sid-1", "make x", "", rec.emit, rec)
    assert seen[0]["TANDEM_SESSION_ID"] == "tdm-claude"
