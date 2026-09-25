import json
import os
import signal
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from tandem.chat.events import (ApprovalRequest, Failure, LimitsUpdate, QuestionRequest,
                                TextDelta, ToolFinished, ToolOutput, ToolStarted, TurnFinished)
from tandem.chat.runtime.codex import CodexRuntime, _choices, _decision, strip_shell
from tandem.config import ChatConfig

FAKE = Path(__file__).parent / "fakes" / "fake_codex_appserver.py"
GOLDEN = Path(__file__).parent / "golden" / "chat" / "codex_appserver.jsonl"


class Recorder:
    def __init__(self, approve="allow", answer="red"):
        self.events, self.approvals, self.questions = [], [], []
        self._approve, self._answer = approve, answer

    def emit(self, ev): self.events.append(ev)
    def approve(self, req): self.approvals.append(req); return self._approve
    def answer(self, req): self.questions.append(req); return self._answer
    def kinds(self): return [type(e).__name__ for e in self.events]


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_ARGV_OUT", str(tmp_path / "argv.json"))
    monkeypatch.setenv("FAKE_PARAMS_OUT", str(tmp_path / "params.jsonl"))
    monkeypatch.setenv("FAKE_REPLY_OUT", str(tmp_path / "reply.json"))
    proj = tmp_path / "proj"; proj.mkdir()

    def params(method):
        for line in (tmp_path / "params.jsonl").read_text().splitlines():
            m = json.loads(line)
            if m["method"] == method:
                return m["params"]
        return None

    return SimpleNamespace(tmp=tmp_path, session=SimpleNamespace(cwd=str(proj), tandem_id="tdm-codex"), params=params,
                           runtime=CodexRuntime(ChatConfig(), binary=[sys.executable, str(FAKE)]))


def test_strip_shell_wrapper():
    assert strip_shell("/bin/zsh -lc 'echo hi'") == "echo hi"
    assert strip_shell("bash -lc \"ls -la\"") == "ls -la"
    assert strip_shell("ls -la") == "ls -la"


def test_approve_flow(env):
    rec = Recorder("allow")
    out = env.runtime.run_turn(env.session, "thread-1", "make x", "", rec.emit, rec)
    assert out.status == "completed" and out.native_id is None
    assert json.loads((env.tmp / "argv.json").read_text()) == ["app-server"]
    assert env.params("initialize")["clientInfo"]["name"] == "tandem"
    assert env.params("thread/resume") == {"threadId": "thread-1", "cwd": env.session.cwd}
    assert env.params("turn/start") == {"threadId": "thread-1", "input": [{"type": "text", "text": "make x", "text_elements": []}]}
    assert rec.approvals == [ApprovalRequest("command", "touch x.txt")]
    assert json.loads((env.tmp / "reply.json").read_text()) == {"decision": "accept"}
    assert rec.kinds() == ["ToolStarted", "ToolOutput", "ToolFinished", "TextDelta", "LimitsUpdate", "TurnFinished"]
    assert rec.events[0] == ToolStarted("call-1", "exec", "touch x.txt")
    assert rec.events[1] == ToolOutput("call-1", "hello\n")
    assert rec.events[2] == ToolFinished("call-1", True, "exit 0")
    assert rec.events[3] == TextDelta("DONE")
    assert rec.events[4] == LimitsUpdate("codex", "5h 3% 7d 12%")
    assert rec.events[5].status == "completed" and rec.events[5].usage == "1% ctx · 2200↑ 200↓"


def test_model_and_config_overrides(env):
    rt = CodexRuntime(ChatConfig(codex_approval_policy="never", codex_sandbox="read-only"),
                      binary=[sys.executable, str(FAKE)])
    rec = Recorder("allow")
    rt.run_turn(env.session, "thread-1", "go", "gpt-5.5", rec.emit, rec)
    assert env.params("thread/resume") == {"threadId": "thread-1", "cwd": env.session.cwd,
                                           "approvalPolicy": "never", "sandbox": "read-only"}
    assert env.params("turn/start")["model"] == "gpt-5.5"


def test_skip_permissions_overrides_approval_and_sandbox(env):
    rt = CodexRuntime(ChatConfig(skip_permissions=True), binary=[sys.executable, str(FAKE)])
    rec = Recorder("allow")
    rt.run_turn(env.session, "thread-1", "go", "", rec.emit, rec)
    assert env.params("thread/resume") == {"threadId": "thread-1", "cwd": env.session.cwd,
                                           "approvalPolicy": "never",
                                           "sandbox": "danger-full-access"}


def test_skip_permissions_yields_to_an_explicit_codex_key(env):
    rt = CodexRuntime(ChatConfig(skip_permissions=True, codex_sandbox="workspace-write"),
                      binary=[sys.executable, str(FAKE)])
    rec = Recorder("allow")
    rt.run_turn(env.session, "thread-1", "go", "", rec.emit, rec)
    assert env.params("thread/resume") == {"threadId": "thread-1", "cwd": env.session.cwd,
                                           "approvalPolicy": "never",
                                           "sandbox": "workspace-write"}


def test_always_and_deny_decisions(env, monkeypatch):
    rec = Recorder("always")
    env.runtime.run_turn(env.session, "t", "go", "", rec.emit, rec)
    assert json.loads((env.tmp / "reply.json").read_text()) == {"decision": "acceptForSession"}
    rec = Recorder("deny")
    out = env.runtime.run_turn(env.session, "t", "go", "", rec.emit, rec)
    assert json.loads((env.tmp / "reply.json").read_text()) == {"decision": "decline"}
    assert out.status == "completed"
    assert ToolFinished("call-1", False, "declined") in rec.events


def test_the_decision_table():
    """What each answer means on the wire, against what the app-server says it
    will accept. `acceptForSession` and `decline` are not always on offer."""
    assert _decision("allow", ["accept", "acceptForSession", "decline", "cancel"]) == "accept"
    assert _decision("always", ["accept", "acceptForSession", "decline"]) == "acceptForSession"
    assert _decision("always", ["accept", "cancel"]) == "accept"        # not offered
    assert _decision("deny", ["accept", "decline", "cancel"]) == "decline"
    assert _decision("deny", ["accept", "cancel"]) == "cancel"          # decline not offered
    assert _decision("deny", None) == "decline"                         # nothing declared
    # and what the row may offer: `always` only where it means acceptForSession
    assert _choices(["accept", "acceptForSession", "decline"]) == ("allow", "always", "deny")
    assert _choices(["accept", "cancel"]) == ("allow", "deny")
    assert _choices(None) == ("allow", "deny")   # undeclared: `always` would be a one-shot accept


@pytest.mark.parametrize("choice,decision,status", [
    ("allow", "accept", True), ("deny", "decline", False)])
def test_file_change_approval(env, monkeypatch, choice, decision, status):
    monkeypatch.setenv("FAKE_CODEX_SCENARIO", "filechange")
    rec = Recorder(choice)
    out = env.runtime.run_turn(env.session, "t", "patch it", "", rec.emit, rec)
    assert out.status == "completed"
    assert rec.approvals == [ApprovalRequest("file_change", "write outside the workspace")]
    assert json.loads((env.tmp / "reply.json").read_text()) == {"decision": decision}
    assert ToolFinished("call-1", status, "") in rec.events
    assert TextDelta("DONE") in rec.events


def test_permissions_approval_allows_for_the_turn(env, monkeypatch):
    monkeypatch.setenv("FAKE_CODEX_SCENARIO", "permission")
    rec = Recorder("allow")
    out = env.runtime.run_turn(env.session, "t", "go", "", rec.emit, rec)
    assert out.status == "completed"
    assert rec.approvals == [ApprovalRequest("permission", "network access")]
    assert json.loads((env.tmp / "reply.json").read_text()) == {
        "permissions": {"network": {"allowAll": True}}, "scope": "turn"}


def test_permissions_approval_always_is_session_scoped(env, monkeypatch):
    monkeypatch.setenv("FAKE_CODEX_SCENARIO", "permission")
    rec = Recorder("always")
    env.runtime.run_turn(env.session, "t", "go", "", rec.emit, rec)
    assert json.loads((env.tmp / "reply.json").read_text())["scope"] == "session"


def test_permissions_denial_is_a_json_rpc_error(env, monkeypatch):
    """A permissions request has no `decision` field to say no with: codex
    reads the JSON-RPC error as the refusal."""
    monkeypatch.setenv("FAKE_CODEX_SCENARIO", "permission")
    rec = Recorder("deny")
    out = env.runtime.run_turn(env.session, "t", "go", "", rec.emit, rec)
    assert out.status == "completed"
    assert json.loads((env.tmp / "reply.json").read_text()) == {
        "error": {"code": -32001, "message": "denied in tandem chat"}}


def test_fresh_thread_start_returns_the_new_id(env, monkeypatch):
    monkeypatch.setenv("FAKE_CODEX_SCENARIO", "fresh")
    rec = Recorder("allow")
    out = env.runtime.run_turn(env.session, None, "go", "", rec.emit, rec)
    assert out.status == "completed" and out.native_id == "thread-new"
    assert env.params("thread/start") == {"cwd": env.session.cwd}
    assert env.params("thread/resume") is None
    assert env.params("turn/start")["threadId"] == "thread-new"


def test_a_turn_that_fails_keeps_the_thread_it_just_started(env, monkeypatch):
    """thread/start minted a real thread before turn/start failed. Dropping
    its id orphans the thread and mints another on every retry — and the
    rollout codex wrote for it is never adopted as the session's own."""
    monkeypatch.setenv("FAKE_CODEX_SCENARIO", "freshfail")
    rec = Recorder()
    out = env.runtime.run_turn(env.session, None, "go", "", rec.emit, rec)
    assert out.status == "failed" and "model unavailable" in out.error
    assert out.native_id == "thread-new"
    assert rec.kinds() == ["Failure", "TurnFinished"]


def test_writer_lock_is_reported_not_retried(env, monkeypatch):
    monkeypatch.setenv("FAKE_CODEX_SCENARIO", "lock")
    rec = Recorder()
    out = env.runtime.run_turn(env.session, "t", "go", "", rec.emit, rec)
    assert out.status == "failed" and "open in another process" in out.error
    assert rec.kinds() == ["Failure", "TurnFinished"]
    assert env.params("turn/start") is None


def test_crash_is_failed_with_stderr(env, monkeypatch):
    monkeypatch.setenv("FAKE_CODEX_SCENARIO", "crash")
    rec = Recorder()
    out = env.runtime.run_turn(env.session, "t", "go", "", rec.emit, rec)
    assert out.status == "failed" and "kaboom" in out.error
    assert rec.events[-1] == TurnFinished("failed", "")


def test_child_death_during_approval_returns_an_outcome(env):
    """`answers.approve` blocks for however long the human takes, and the
    app-server can die in that window. The write of the approval response then
    breaks, which must not escape as an exception: the caller is owed a
    TurnOutcome and the window is owed a terminal event."""
    rt = env.runtime

    class KillsTheChild(Recorder):
        def approve(self, req):
            proc = rt._proc                     # deliberate: stands in for an external kill
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=5)                # dead read end, so the write is deterministic
            return super().approve(req)

    rec = KillsTheChild("allow")
    out = rt.run_turn(env.session, "t", "make x", "", rec.emit, rec)
    assert out.status == "failed"
    assert isinstance(rec.events[-1], TurnFinished)


def test_interrupt(env, monkeypatch):
    monkeypatch.setenv("FAKE_CODEX_SCENARIO", "interrupt")
    rec = Recorder(); rt = env.runtime; holder = {}
    t = threading.Thread(target=lambda: holder.__setitem__("out", rt.run_turn(env.session, "t", "go", "", rec.emit, rec)))
    t.start()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not any(isinstance(e, TextDelta) for e in rec.events):
        time.sleep(0.02)
    rt.interrupt(); t.join(10)
    assert holder["out"].status == "interrupted"
    assert env.params("turn/interrupt") == {"threadId": "t", "turnId": "turn-1"}


def test_question_round_trip(env, monkeypatch):
    monkeypatch.setenv("FAKE_CODEX_SCENARIO", "question")
    rec = Recorder(answer="blue")
    out = env.runtime.run_turn(env.session, "t", "go", "", rec.emit, rec)
    assert out.status == "completed"
    assert rec.questions == [QuestionRequest("Which color?", ("red", "blue"))]
    assert TextDelta("you chose blue") in rec.events


def test_unknown_item_type_is_reported_not_raised():
    """Every ThreadItem discriminator is a closed Literal over the variants the
    pinned schema knows, so a codex release that adds one must cost the window
    a line, not the turn: no ValidationError may escape `handle`."""
    rec = Recorder(); rt = CodexRuntime(ChatConfig()); sent = []
    got = rt.handle({"method": "item/started",
                     "params": {"threadId": "t", "turnId": "turn-1", "startedAtMs": 1,
                                "item": {"type": "quantumThing", "id": "x-1"}}},
                    sent.append, rec.emit, rec)
    assert got is None and sent == []
    assert rec.kinds() == ["Failure"]
    assert rec.events[0].message.startswith("codex sent a message tandem cannot parse: item/started: ")


def test_unparsable_turn_completed_still_ends_the_turn():
    """A required field that drifts out of `turn/completed` must not strand the
    turn: the status is read off the dict so run_turn's loop still terminates."""
    rec = Recorder(); rt = CodexRuntime(ChatConfig()); sent = []
    got = rt.handle({"method": "turn/completed", "params": {"turn": {"status": "completed"}}},
                    sent.append, rec.emit, rec)
    assert got is not None and got.status == "completed" and got.error == ""
    assert rec.kinds() == ["Failure", "TurnFinished"]
    assert rec.events[-1] == TurnFinished("completed", "")
    assert isinstance(rec.events[0], Failure)


def test_unparsable_approval_request_is_declined_not_dropped():
    """A request is never dropped the way a notification is: the app-server
    blocks until it is answered, and codex reads a JSON-RPC error as a decline.
    The human is not asked to rule on a request tandem cannot show them."""
    rec = Recorder("allow"); rt = CodexRuntime(ChatConfig()); sent = []
    got = rt.handle({"method": "item/commandExecution/requestApproval", "id": 5,
                     "params": {"threadId": "t", "turnId": "turn-1", "command": "ls"}},
                    sent.append, rec.emit, rec)
    assert got is None
    assert sent == [{"jsonrpc": "2.0", "id": 5,
                     "error": {"code": -32001, "message": "tandem cannot parse this request"}}]
    assert rec.approvals == []
    assert rec.kinds() == ["Failure"]
    assert rec.events[0].message.startswith(
        "codex sent a request tandem cannot parse: item/commandExecution/requestApproval: ")


def test_unparsable_user_input_request_answers_nothing():
    rec = Recorder(answer="blue"); rt = CodexRuntime(ChatConfig()); sent = []
    got = rt.handle({"method": "item/tool/requestUserInput", "id": 6,
                     "params": {"threadId": "t", "turnId": "turn-1"}},
                    sent.append, rec.emit, rec)
    assert got is None
    assert sent == [{"jsonrpc": "2.0", "id": 6, "result": {"answers": {}}}]
    assert rec.questions == []
    assert rec.kinds() == ["Failure"]
    assert rec.events[0].message.startswith(
        "codex sent a request tandem cannot parse: item/tool/requestUserInput: ")


def test_thin_turn_start_response_falls_back_to_the_dict(env, monkeypatch):
    """Only turn.id is load-bearing in the turn/start response, so a result
    that drifts elsewhere still starts a usable turn."""
    monkeypatch.setenv("FAKE_CODEX_SCENARIO", "thinturn")
    rec = Recorder("allow")
    out = env.runtime.run_turn(env.session, "t", "go", "", rec.emit, rec)
    assert out.status == "completed"
    assert isinstance(rec.events[0], Failure)
    assert rec.events[0].message.startswith("codex sent a response tandem cannot parse: turn/start: ")
    assert rec.kinds()[1:] == ["ToolStarted", "ToolOutput", "ToolFinished", "TextDelta",
                               "LimitsUpdate", "TurnFinished"]


def test_golden_lines_drive_the_handler():
    rec = Recorder("allow"); sent = []
    rt = CodexRuntime(ChatConfig())
    outcome = None
    for line in GOLDEN.read_text().splitlines():
        m = json.loads(line)
        if "method" not in m:
            continue                                   # responses are consumed by _call, not handle
        got = rt.handle(m, sent.append, rec.emit, rec)
        outcome = got or outcome
    assert outcome is not None and outcome.status == "completed"
    # the captured availableDecisions are ["accept", {amendment…}, "cancel"]:
    # no acceptForSession, so the row must not offer `always` — the key would
    # mean plain accept
    assert rec.approvals == [ApprovalRequest("command", "echo fixture —", ("allow", "deny"))]
    assert sent == [{"jsonrpc": "2.0", "id": 0, "result": {"decision": "accept"}}]
    assert rec.kinds() == ["ToolStarted", "ToolOutput", "ToolFinished", "LimitsUpdate", "TextDelta", "TurnFinished"]
    assert rec.events[0] == ToolStarted("call_vDe2mWf2XYJ1BjLDXSDWgLSo", "exec", "echo fixture —")
    assert rec.events[1] == ToolOutput("call_vDe2mWf2XYJ1BjLDXSDWgLSo", "fixture —\n")   # from aggregatedOutput, no delta streamed
    assert rec.events[3] == LimitsUpdate("codex", "5h 0% 7d 0%")


def test_child_is_told_which_session_it_belongs_to(env, monkeypatch):
    from tandem.chat.runtime import codex as mod

    seen = []
    real = mod.subprocess.Popen
    monkeypatch.setattr(mod.subprocess, "Popen",
                        lambda *a, **kw: (seen.append(kw["env"]), real(*a, **kw))[1])
    rec = Recorder()
    env.runtime.run_turn(env.session, None, "hi", "", rec.emit, rec)
    assert seen[0]["TANDEM_SESSION_ID"] == "tdm-codex"


def subagent_notice(method, thread="parent", **params):
    return {"method": method, "params": {"threadId": thread, **params}}


def subagent_done(thread, turn="turn-1", status="completed"):
    return subagent_notice("turn/completed", thread, turn={
        "id": turn, "items": [], "itemsView": "summary", "status": status,
        "error": None, "startedAt": 1, "completedAt": 2, "durationMs": 1})


def subagent_runtime():
    rt = CodexRuntime(ChatConfig())
    rt._thread_id, rt._turn_id = "parent", "turn-1"
    rec = Recorder()
    return rt, rec, lambda m: rt.handle(m, lambda _: None, rec.emit, rec)


def test_child_completion_does_not_end_parent_or_leak_child_text():
    rt, rec, handle = subagent_runtime()
    handle(subagent_notice("turn/started", "child", turn={"id": "child-turn"}))
    handle(subagent_notice("item/agentMessage/delta", "child", turnId="child-turn",
                          itemId="child-msg", delta="CHILD_DONE"))
    assert handle(subagent_done("child", "child-turn")) is None
    assert not any(isinstance(e, (TextDelta, TurnFinished)) for e in rec.events)
    assert any(isinstance(e, ToolOutput) and e.text == "CHILD_DONE" for e in rec.events)
    handle(subagent_notice("item/agentMessage/delta", turnId="turn-1", itemId="parent-msg", delta="PARENT_DONE"))
    assert handle(subagent_done("parent")).status == "completed"
    assert [e.text for e in rec.events if isinstance(e, TextDelta)] == ["PARENT_DONE"]
    assert len([e for e in rec.events if isinstance(e, TurnFinished)]) == 1


def test_parent_completion_waits_for_running_children():
    rt, rec, handle = subagent_runtime()
    for child in ("child-a", "child-b"):
        handle(subagent_notice("turn/started", child, turn={"id": "child-turn"}))
    assert handle(subagent_done("parent")) is None
    assert handle(subagent_done("child-a", "child-turn")) is None
    assert not any(isinstance(e, TurnFinished) for e in rec.events)
    assert handle(subagent_done("child-b", "child-turn")).status == "completed"
    assert len([e for e in rec.events if isinstance(e, TurnFinished)]) == 1


def test_stale_parent_completion_cannot_end_current_turn():
    rt, rec, handle = subagent_runtime()
    assert handle(subagent_done("parent", "old-turn")) is None
    assert rec.events == []


def test_subagent_spawn_is_visible_and_keeps_server_alive_before_child_starts():
    rt, rec, handle = subagent_runtime()
    item = {"type": "subAgentActivity", "id": "spawn-1", "kind": "started",
            "agentThreadId": "child", "agentPath": "/root/worker"}
    handle(subagent_notice("item/started", turnId="turn-1", startedAtMs=1, item=item))
    handle(subagent_notice("item/completed", turnId="turn-1", completedAtMs=2, item=item))
    assert any(isinstance(e, ToolStarted) and "worker" in e.summary for e in rec.events)
    assert handle(subagent_done("parent")) is None
    handle(subagent_notice("turn/started", "child", turn={"id": "child-turn"}))
    assert handle(subagent_done("child", "child-turn")).status == "completed"


def test_child_approval_is_answered_while_parent_waits():
    rt, rec, handle = subagent_runtime()
    sent = []
    rt.handle({"id": 99, "method": "item/tool/requestUserInput", "params": {
        "threadId": "child", "turnId": "child-turn", "itemId": "question", "isBlocking": True,
        "questions": [{"id": "q", "header": "Color", "question": "Color?", "isOther": False,
                       "isSecret": False, "options": None}]}}, sent.append, rec.emit, rec)
    assert sent[0]["result"]["answers"]["q"]["answers"] == ["red"]
    assert len(rec.questions) == 1


def test_parent_interruption_does_not_wait_for_children():
    rt, rec, handle = subagent_runtime()
    handle(subagent_notice("turn/started", "child", turn={"id": "child-turn"}))
    assert handle(subagent_done("parent", status="interrupted")).status == "interrupted"


@pytest.mark.parametrize("scenario", ["childfirst", "parentfirst"])
def test_process_waits_for_parent_and_children(env, monkeypatch, scenario):
    monkeypatch.setenv("FAKE_CODEX_SCENARIO", scenario)
    rec = Recorder()
    out = env.runtime.run_turn(env.session, "parent", "go", "", rec.emit, rec)
    assert out.status == "completed"
    assert [e.text for e in rec.events if isinstance(e, TextDelta)] == ["PARENT_DONE"]
    assert ToolOutput("agent:child", "CHILD_DONE") in rec.events
    assert len([e for e in rec.events if isinstance(e, TurnFinished)]) == 1
    assert isinstance(rec.events[-1], TurnFinished)


def test_interrupt_while_draining_workers(env, monkeypatch):
    monkeypatch.setenv("FAKE_CODEX_SCENARIO", "draininterrupt")
    rec = Recorder(); rt = env.runtime; holder = {}
    t = threading.Thread(target=lambda: holder.setdefault("out", rt.run_turn(
        env.session, "parent", "go", "", rec.emit, rec)))
    t.start()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and rt._parent_outcome is None and t.is_alive():
        time.sleep(0.01)
    try:
        assert rt._parent_outcome is not None
        rt.interrupt()
        t.join(5)
        assert not t.is_alive()
        assert holder["out"].status == "interrupted"
        assert rec.events[-1].status == "interrupted"
    finally:
        rt.close(); t.join(5)


def test_cancel_wins_over_last_child_finishing():
    rt, rec, handle = subagent_runtime()
    handle(subagent_notice("turn/started", "child", turn={"id": "child-turn"}))
    assert handle(subagent_done("parent")) is None
    rt._interrupted = True
    assert handle(subagent_done("child", "child-turn")).status == "interrupted"


def test_child_failure_is_reported_without_failing_successful_parent():
    rt, rec, handle = subagent_runtime()
    handle(subagent_notice("turn/started", "child", turn={"id": "child-turn"}))
    assert handle(subagent_done("child", "child-turn", "failed")) is None
    assert ToolFinished("agent:child", False, "failed") in rec.events
    assert handle(subagent_done("parent")).status == "completed"


def test_stale_child_completion_does_not_finish_new_child_turn():
    rt, rec, handle = subagent_runtime()
    handle(subagent_notice("turn/started", "child", turn={"id": "new-turn"}))
    assert handle(subagent_done("parent")) is None
    assert handle(subagent_done("child", "old-turn")) is None
    assert handle(subagent_done("child", "new-turn")).status == "completed"


def subagent_status(thread, kind):
    return subagent_notice("thread/status/changed", thread, status={"type": kind})


def test_worker_that_went_idle_without_a_turn_completed_does_not_hang_the_turn():
    """A closed or crashed worker may never report turn/completed; its thread
    leaving `active` is the other sign that nothing is running there."""
    rt, rec, handle = subagent_runtime()
    handle(subagent_notice("turn/started", "child", turn={"id": "child-turn"}))
    handle(subagent_status("child", "active"))
    assert handle(subagent_status("child", "systemError")) is None
    assert handle(subagent_done("parent")).status == "completed"
    assert ToolFinished("agent:child", False, "systemError") in rec.events
    assert isinstance(rec.events[-1], TurnFinished)


def test_idle_at_spawn_does_not_release_a_worker_that_has_not_run_yet():
    """Captured order on codex 0.155.1: the child thread reports idle, then the
    parent's subAgentActivity item, then the child goes active."""
    rt, rec, handle = subagent_runtime()
    handle(subagent_status("child", "idle"))
    item = {"type": "subAgentActivity", "id": "spawn-1", "kind": "started",
            "agentThreadId": "child", "agentPath": "/root/worker"}
    handle(subagent_notice("item/started", turnId="turn-1", startedAtMs=1, item=item))
    handle(subagent_status("child", "active"))
    handle(subagent_notice("turn/started", "child", turn={"id": "child-turn"}))
    assert handle(subagent_done("parent")) is None
    assert handle(subagent_status("child", "idle")).status == "completed"
    assert ToolFinished("agent:child", True, "") in rec.events


def test_worker_reactivated_after_idle_is_waited_for_again():
    rt, rec, handle = subagent_runtime()
    handle(subagent_notice("turn/started", "child", turn={"id": "t1"}))
    handle(subagent_status("child", "idle"))
    handle(subagent_status("child", "active"))
    assert handle(subagent_done("parent")) is None
    assert handle(subagent_done("child", "t1")).status == "completed"
