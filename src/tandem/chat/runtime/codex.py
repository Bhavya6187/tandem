"""Codex app-server, one process per turn: JSON-RPC 2.0, one message per
line, over stdio (the same transport the codex Python SDK's client.py uses).

  initialize {clientInfo} -> initialized (notification)
  thread/resume {threadId, cwd[, approvalPolicy, sandbox]}   or   thread/start {cwd[, …]}
  turn/start {threadId, input:[{type:"text", text}][, model]}
  … notifications (item/*, turn/*, thread/tokenUsage/updated,
    account/rateLimits/updated) and server requests (*/requestApproval,
    item/tool/requestUserInput) … until the parent turn and its workers finish.

The process holds the thread's writer lock until it exits, which is why a
new process is spawned per turn and why a lock error is reported, never
retried. Requests are built from the generated models; notifications and
server requests are validated by them (unknown fields ignored); the two
thread responses are read as dicts because their models carry dozens of
unrelated fields that drift between releases."""

from __future__ import annotations

import json
import queue
import re
import subprocess
import threading
import time
import tomllib
from collections import deque
from pathlib import Path
from typing import Callable

from pydantic import ValidationError

from ... import paths
from ...harness import get_adapter
from ...ratelimit import Window, format_windows, window_label
from ..events import (Answers, ApprovalRequest, Failure, FileDiff, LimitsUpdate, LiveEvent,
                      QuestionRequest, TextDelta, ThinkingDelta, ToolFinished, ToolOutput,
                      ToolStarted, TurnFinished, TurnOutcome)
from ..navigator import REVIEW_PROMPT_PREFIX
from . import child_env, first_line, terminate
from . import codex_protocol as cp

try:
    from ... import __version__ as _VERSION
except ImportError:      # pragma: no cover
    _VERSION = "0"

_SHELL_RE = re.compile(r"""^\S*(?:zsh|bash|sh)\s+-l?c\s+(['"])(.*)\1\s*$""", re.S)

class _Declining:
    """The answers a thread-less call (`model/list`) hands to `handle`: no
    turn is running, so nobody is at the keyboard for a request the server
    should not be sending — every approval is declined, every question
    unanswered, and nothing raises."""

    def approve(self, req) -> str:
        return "deny"

    def answer(self, req) -> str:
        return ""


_DECLINE = _Declining()

# `/mode` → codex's /approvals presets: edits = "auto", plan = "read-only",
# skip = "full access". `ask` sends nothing and inherits ~/.codex/config.toml.
CODEX_MODES = {"edits": ("on-request", "workspace-write"),
               "plan": ("on-request", "read-only"),
               "skip": ("never", "danger-full-access")}

# the policy a review turn pins for itself
_REVIEW_POLICY = ("never", "read-only")
# the values the pinned protocol models accept: anything else (a deprecated
# `on-failure`, a sandbox type from another release) would fail validation
_APPROVAL_POLICIES = ("untrusted", "on-request", "never")
_SANDBOX_MODES = ("read-only", "workspace-write", "danger-full-access")


def _user_text(rec: dict) -> str | None:
    """The text of a user prompt record: legacy `event_msg`/`user_message`
    or paginated `response_item`/`message` with `input_text` blocks."""
    payload = rec.get("payload")
    if not isinstance(payload, dict):
        return None
    if rec.get("type") == "event_msg" and payload.get("type") == "user_message":
        text = payload.get("message")
        return text if isinstance(text, str) else None
    if rec.get("type") == "response_item" and payload.get("type") == "message" \
            and payload.get("role") == "user":
        content = payload.get("content")
        if not isinstance(content, list):
            return None
        return "".join(b.get("text", "") for b in content
                       if isinstance(b, dict) and b.get("type") == "input_text"
                       and isinstance(b.get("text"), str))
    return None


def _turns(path: Path) -> list[tuple[str, str, bool]]:
    """`(approval_policy, sandbox type, is_review)` of every turn_context
    record in a rollout, in file order. Only codex writes turn_context, one
    per turn it ran; a turn is a review when a user record after it (before
    the next turn_context) starts, untagged, with the review prompt. Synced
    records are tagged (`[via …]`, `[tandem]`) and never match; a user record
    before any turn_context attaches to nothing. A turn_context with no
    `task_started` since the previous one is a mid-turn compaction
    continuation and keeps the previous turn's review mark. Unparsable lines
    are skipped; an unreadable file has none."""
    found: list[tuple[str, str, bool]] = []
    started = False
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if '"turn_context"' not in line and '"user_message"' not in line \
                        and '"task_started"' not in line \
                        and '"role": "user"' not in line and '"role":"user"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(rec, dict):
                    continue
                payload = rec.get("payload")
                if rec.get("type") == "event_msg" and isinstance(payload, dict) \
                        and payload.get("type") == "task_started":
                    started = True
                    continue
                if rec.get("type") == "turn_context":
                    if not isinstance(payload, dict):
                        continue
                    sandbox = payload.get("sandbox_policy")
                    sandbox = sandbox.get("type") if isinstance(sandbox, dict) else sandbox
                    approval = payload.get("approval_policy")
                    if isinstance(approval, str) and isinstance(sandbox, str):
                        # a continuation (no task_started) is the same turn
                        found.append((approval, sandbox,
                                      False if started or not found else found[-1][2]))
                        started = False
                    continue
                if not found:
                    continue
                text = _user_text(rec)
                # sticky: the other harness's follow-up syncs in after a
                # review with no turn_context and must not unmark it
                if text is not None and text.startswith(REVIEW_PROMPT_PREFIX) and not found[-1][2]:
                    found[-1] = (found[-1][0], found[-1][1], True)
    except OSError:
        return []
    return found


def codex_default_policy() -> dict:
    """The policy codex runs a turn under when nobody overrides it:
    `approval_policy` / `sandbox_mode` from ~/.codex/config.toml when they
    are values the protocol knows, else codex's built-in on-request /
    read-only."""
    try:
        with open(paths.codex_home() / "config.toml", "rb") as f:
            conf = tomllib.load(f)
    except (OSError, ValueError):
        conf = {}
    approval, sandbox = conf.get("approval_policy"), conf.get("sandbox_mode")
    return {"approvalPolicy": approval if approval in _APPROVAL_POLICIES else "on-request",
            "sandbox": sandbox if sandbox in _SANDBOX_MODES else "read-only"}


def policy_after_review(path: Path | None) -> dict | None:
    """The policy to put back when the thread's last codex-run turn was a
    review.

    codex resumes a thread with its last turn_context's policy, so after a
    review an ask-mode turn, which sends no overrides of its own, would
    inherit the review's `never` / `read-only`. `None` unless that last turn
    actually was a review (by its prompt, not its policy); otherwise the
    policy of the last non-review turn — exactly what codex would have
    persisted had the review not run, so a user's own `never` / `read-only`
    is put back as itself — or codex's default when no earlier turn has one.
    A recorded policy the protocol does not know is treated as absent:
    sending it would fail thread/resume, and with no new turn_context, every
    turn after it."""
    if path is None:
        return None
    turns = _turns(path)
    if not turns or not turns[-1][2]:
        return None
    for approval, sandbox, is_review in reversed(turns):
        if is_review or approval not in _APPROVAL_POLICIES or sandbox not in _SANDBOX_MODES:
            continue
        return {"approvalPolicy": approval, "sandbox": sandbox}
    return codex_default_policy()


_REQUEST_MODELS = {
    "item/commandExecution/requestApproval": cp.CommandExecutionRequestApprovalParams,
    "item/fileChange/requestApproval": cp.FileChangeRequestApprovalParams,
    "item/permissions/requestApproval": cp.PermissionsRequestApprovalParams,
    "item/tool/requestUserInput": cp.ToolRequestUserInputParams,
}


def change_diff(change) -> str:
    """A fileChange entry as a diff: `update` carries a unified diff, but
    `add` and `delete` carry the file's content (codex's item builder), so
    those are written out as all-added or all-removed lines."""
    diff = getattr(change, "diff", "") or ""
    kind = getattr(change, "kind", None)
    kind = getattr(kind, "root", kind)
    kind = getattr(kind, "type", None) or (kind.get("type") if isinstance(kind, dict) else None)
    if kind == "add":
        return "\n".join(["@@ new file @@", *("+" + l for l in diff.splitlines())])
    if kind == "delete":
        return "\n".join(["@@ deleted @@", *("-" + l for l in diff.splitlines())])
    return diff


def strip_shell(command: str) -> str:
    """`/bin/zsh -lc 'echo hi'` -> `echo hi`: the TUI shows the inner command."""
    m = _SHELL_RE.match(command or "")
    return m.group(2) if m else (command or "")


def item_of(notification):
    """The concrete ThreadItem variant behind a notification's `item`."""
    item = notification.item
    return getattr(item, "root", item)


def _decision(choice: str, available) -> str:
    listed = {d for d in (available or []) if isinstance(d, str)}
    if choice == "deny":
        return "decline" if "decline" in listed or not listed else "cancel"
    if choice == "always" and "acceptForSession" in listed:
        return "acceptForSession"
    return "accept"


def _choices(available) -> tuple[str, ...]:
    """What the row may offer: `always` needs the app-server to list
    acceptForSession — undeclared included, since _decision falls back to a
    one-shot accept there and the key would silently mean `yes`."""
    listed = {d for d in (available or []) if isinstance(d, str)}
    if "acceptForSession" not in listed:
        return ("allow", "deny")
    return ("allow", "always", "deny")


class CodexRuntime:
    harness = "codex"

    def __init__(self, cfg, *, binary: list[str] | None = None, output_schema: dict | None = None):
        self.cfg = cfg
        self.binary = list(binary) if binary else ["codex"]
        self.output_schema = output_schema      # constrains the final message (the navigator's verdict)
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._n = 0
        self._interrupted = False
        self._interrupt_pending = False     # Ctrl-C before turn/start returned a turn id
        self._thread_id: str | None = None
        self._turn_id: str | None = None
        self._usage = ""
        self._streamed_output: set[str] = set()
        self._streamed_text: set[str] = set()
        self._agents: dict[str, str | None] = {}  # running thread -> turn (None while spawning)
        self._agent_names: dict[str, str] = {}
        # a worker whose thread left `active` after its turn began: a closed or
        # crashed one may never send turn/completed, and must not hold the turn
        self._quiet: dict[str, str] = {}          # thread -> the status it went to
        self._parent_outcome: TurnOutcome | None = None
        self.harness_commands: list = []      # the app-server has no text-level skill invocation
        self._compacting = False
        # how long a compact may go without thread/compacted or turn/completed:
        # `_call`'s timeout covers only the `{}` acknowledgement, and an
        # unbounded q.get() after it would park the worker on a server that
        # never reports. One model call; tests shrink it.
        self.compact_timeout = 300.0

    def _agent_started(self, thread: str, emit, name: str = "") -> None:
        if thread not in self._agents:
            self._agents[thread] = None
            self._agent_names[thread] = name or self._agent_names.get(thread, thread)
            emit(ToolStarted(f"agent:{thread}", "agent", self._agent_names[thread]))

    def _finish_if_ready(self, emit) -> TurnOutcome | None:
        outcome = self._parent_outcome
        if outcome is not None and self._interrupted:
            outcome = TurnOutcome("interrupted")
        if outcome is not None and (outcome.status != "completed"
                                    or all(t in self._quiet for t in self._agents)):
            self._parent_outcome = None
            for thread in [t for t in self._agents if t in self._quiet]:
                kind = self._quiet.pop(thread)
                self._agents.pop(thread)
                emit(ToolFinished(f"agent:{thread}", kind == "idle", "" if kind == "idle" else kind))
            emit(TurnFinished(outcome.status, self._usage))
            return outcome
        return None

    # -- wire ----------------------------------------------------------------

    def _write(self, proc: subprocess.Popen, obj: dict) -> None:
        # a child that died while we were blocked on an approval leaves a broken
        # pipe (or a stdin closed under us by close()); either way the stdout
        # reader is about to hit EOF and the turn ends through run_turn's
        # `outcome is None` path, which owes the window a TurnFinished
        with self._lock:
            if proc.stdin and not proc.stdin.closed:
                try:
                    proc.stdin.write(json.dumps(obj) + "\n")
                    proc.stdin.flush()
                except (OSError, ValueError):
                    pass

    def _request(self, proc, method: str, params: dict | None) -> int:
        self._n += 1
        msg = {"jsonrpc": "2.0", "id": self._n, "method": method}
        if params is not None:
            msg["params"] = params
        self._write(proc, msg)
        return self._n

    def _call(self, proc, q: "queue.Queue", method: str, params: dict | None,
              emit, answers, timeout: float = 60.0) -> dict:
        """Send a request and wait for its response, handling everything
        else that arrives meanwhile."""
        rid = self._request(proc, method, params)
        send = lambda obj: self._write(proc, obj)
        while True:
            try:
                m = q.get(timeout=timeout)
            except queue.Empty:
                return {"error": {"message": f"{method}: no response within {timeout:.0f}s"}}
            if m is None:
                return {"error": {"message": "codex app-server exited"}}
            if m.get("id") == rid and ("result" in m or "error" in m):
                return m
            self.handle(m, send, emit, answers)

    # -- protocol ------------------------------------------------------------

    def _server_request(self, m: dict, send, emit: Callable[[LiveEvent], None],
                        answers: Answers) -> None:
        method, rid = m["method"], m["id"]
        params = m.get("params") or {}
        if answers is None:
            answers = _DECLINE
        model = _REQUEST_MODELS.get(method)
        if model is None:
            send({"jsonrpc": "2.0", "id": rid, "result": {}})
            return
        try:
            p = model.model_validate(params)
        except ValidationError as exc:
            # unlike a notification, a request can never be dropped: the
            # app-server blocks on it until it is answered. codex reads an
            # error as a decline, and an empty answer set is the only honest
            # reply to a question we could not read. The human is not asked
            # to rule on a request we cannot show them.
            emit(Failure(f"codex sent a request tandem cannot parse: "
                         f"{method}: {first_line(str(exc))}"))
            if method == "item/tool/requestUserInput":
                send({"jsonrpc": "2.0", "id": rid, "result": {"answers": {}}})
            else:
                send({"jsonrpc": "2.0", "id": rid,
                      "error": {"code": -32001, "message": "tandem cannot parse this request"}})
            return
        if method == "item/commandExecution/requestApproval":
            available = params.get("availableDecisions")
            choice = answers.approve(ApprovalRequest(
                "command", first_line(strip_shell(p.command or "")), _choices(available)))
            send({"jsonrpc": "2.0", "id": rid,
                  "result": {"decision": _decision(choice, available)}})
        elif method == "item/fileChange/requestApproval":
            available = params.get("availableDecisions")
            detail = getattr(p, "reason", None) or "apply file changes"
            choice = answers.approve(ApprovalRequest("file_change", first_line(detail),
                                                    _choices(available)))
            send({"jsonrpc": "2.0", "id": rid,
                  "result": {"decision": _decision(choice, available)}})
        elif method == "item/permissions/requestApproval":
            detail = getattr(p, "reason", None) or "additional permissions"
            choice = answers.approve(ApprovalRequest("permission", first_line(detail)))
            if choice == "deny":
                send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32001, "message": "denied in tandem chat"}})
            else:
                send({"jsonrpc": "2.0", "id": rid,
                      "result": {"permissions": params.get("permissions") or {},
                                 "scope": "session" if choice == "always" else "turn"}})
        else:                       # item/tool/requestUserInput
            answered = {}
            for q in p.questions or []:
                options = tuple(str(getattr(o, "label", None) or o) for o in (q.options or []))
                answered[q.id] = {"answers": [answers.answer(QuestionRequest(q.question, options))]}
            send({"jsonrpc": "2.0", "id": rid, "result": {"answers": answered}})

    def _parse(self, model, params: dict, method: str, emit: Callable[[LiveEvent], None]):
        """Validate a notification against the pinned models, or report the
        drift and drop the line. Every ThreadItem discriminator is a closed
        Literal over the 19 variants codex 0.153.4 knows, so a release that
        adds a twentieth — or drops a required field — must cost the window
        one line, never the whole turn."""
        try:
            return model.model_validate(params)
        except ValidationError as exc:
            emit(Failure(f"codex sent a message tandem cannot parse: "
                         f"{method}: {first_line(str(exc))}"))
            return None

    def handle(self, m: dict, send: Callable[[dict], None], emit: Callable[[LiveEvent], None],
               answers: Answers) -> TurnOutcome | None:
        """One server line that is not the response being waited for."""
        method = m.get("method")
        if method is None:
            return None
        if "id" in m:
            self._server_request(m, send, emit, answers)
            return None
        params = m.get("params") or {}
        thread = params.get("threadId")
        child = bool(self._thread_id and thread and thread != self._thread_id)
        if method == "thread/compacted":
            # terminal only for a compact this runtime started; an ordinary
            # turn that compacts on its own reports it as a contextCompaction
            # item and ends with turn/completed as ever
            if self._compacting and not child:
                emit(TurnFinished("completed", self._usage))
                return TurnOutcome("completed")
            return None
        turn = params.get("turn") or {}
        turn_id = params.get("turnId") or turn.get("id")
        # Notifications from the app-server multiplex parent and child threads.
        # Server requests above must still be answered, including child approvals.
        if not child and self._turn_id and turn_id and turn_id != self._turn_id:
            return None
        if child:
            if method == "turn/started":
                self._agent_started(thread, emit)
                self._agents[thread] = turn_id
                self._quiet.pop(thread, None)
                return None
            if method == "thread/status/changed":
                kind = str((params.get("status") or {}).get("type") or "")
                if kind == "active":
                    self._quiet.pop(thread, None)
                elif self._agents.get(thread):      # idle at spawn precedes its first turn
                    self._quiet[thread] = kind
                return self._finish_if_ready(emit)
            if method == "turn/completed":
                if thread in self._agents and self._agents[thread] in (None, turn_id):
                    self._agents.pop(thread)
                    self._quiet.pop(thread, None)
                    status = turn.get("status", "failed")
                    emit(ToolFinished(f"agent:{thread}", status == "completed", status))
                return self._finish_if_ready(emit)
            if method in ("thread/tokenUsage/updated", "item/reasoning/summaryTextDelta"):
                return None
            parent_emit = emit

            def emit(ev):
                if isinstance(ev, TextDelta):
                    parent_emit(ToolOutput(f"agent:{thread}", ev.text))
                elif isinstance(ev, ToolStarted):
                    name = self._agent_names.get(thread, thread)
                    parent_emit(ToolStarted(ev.call_id, f"agent[{name}]/{ev.tool}", ev.summary))
                else:
                    parent_emit(ev)

        it = params.get("item") or {}
        if method == "item/started" and it.get("type") == "subAgentActivity":
            if it.get("kind") == "started" and it.get("agentThreadId"):
                self._agent_started(it["agentThreadId"], emit, it.get("agentPath", ""))
            return None
        if method == "item/agentMessage/delta":
            n = self._parse(cp.AgentMessageDeltaNotification, params, method, emit)
            if n is None:
                return None
            self._streamed_text.add(n.itemId)
            emit(TextDelta(n.delta))
        elif method == "item/reasoning/summaryTextDelta":
            if params.get("delta"):
                emit(ThinkingDelta(params["delta"]))
        elif method == "item/commandExecution/outputDelta":
            item_id, delta = params.get("itemId", ""), params.get("delta", "")
            self._streamed_output.add(item_id)
            emit(ToolOutput(item_id, delta))
        elif method == "item/fileChange/outputDelta":
            return None                          # the structured diff at item/completed is authoritative
        elif method == "item/started":
            n = self._parse(cp.ItemStartedNotification, params, method, emit)
            if n is None:
                return None
            it = item_of(n)
            kind = it.type
            if kind == "commandExecution":
                emit(ToolStarted(it.id, "exec", first_line(strip_shell(it.command or ""))))
            elif kind == "fileChange":
                names = tuple(c.path for c in (getattr(it, "changes", None) or []) if getattr(c, "path", ""))
                emit(ToolStarted(it.id, "patch", first_line(", ".join(names)), paths=names))
            elif kind == "mcpToolCall":
                emit(ToolStarted(it.id, f"mcp:{getattr(it, 'server', '?')}.{getattr(it, 'tool', '?')}", ""))
            elif kind == "webSearch":
                emit(ToolStarted(it.id, "web_search", first_line(getattr(it, "query", "") or "")))
            elif kind == "contextCompaction":
                emit(ToolStarted(it.id, "compaction", "context compaction"))
            elif kind == "collabAgentToolCall":
                emit(ToolStarted(it.id, f"agent/{it.tool}", first_line(it.prompt or "")))
        elif method == "item/completed":
            n = self._parse(cp.ItemCompletedNotification, params, method, emit)
            if n is None:
                return None
            it = item_of(n)
            kind = it.type
            if kind == "commandExecution":
                if it.id not in self._streamed_output and it.aggregatedOutput:
                    emit(ToolOutput(it.id, it.aggregatedOutput))
                ok = it.status == "completed"
                summary = f"exit {it.exitCode}" if it.exitCode is not None else str(it.status)
                emit(ToolFinished(it.id, ok, summary))
            elif kind == "fileChange":
                ok = getattr(it, "status", "completed") == "completed"
                emit(ToolFinished(it.id, ok, ""))
                if ok:
                    for c in getattr(it, "changes", None) or []:
                        if getattr(c, "diff", ""):
                            emit(FileDiff(it.id, getattr(c, "path", "") or "", change_diff(c)))
            elif kind in ("mcpToolCall", "webSearch", "contextCompaction"):
                emit(ToolFinished(it.id, getattr(it, "status", "completed") != "failed", ""))
            elif kind == "agentMessage":
                if it.id not in self._streamed_text and it.text:
                    emit(TextDelta(it.text))
            elif kind == "collabAgentToolCall":
                if it.tool in ("spawnAgent", "resumeAgent") and it.status == "completed":
                    for agent in it.receiverThreadIds:
                        # A child may already have completed before the spawn reply.
                        if agent not in self._agent_names:
                            self._agent_started(agent, emit)
                emit(ToolFinished(it.id, it.status == "completed", it.status))
        elif method == "thread/tokenUsage/updated":
            n = self._parse(cp.ThreadTokenUsageUpdatedNotification, params, method, emit)
            if n is None:
                return None
            total = n.tokenUsage.total
            parts = []
            window = getattr(n.tokenUsage, "modelContextWindow", None)
            if window:
                parts.append(f"{round(total.totalTokens * 100 / window)}% ctx")
            parts.append(f"{total.inputTokens}↑ {total.outputTokens}↓")
            self._usage = " · ".join(parts)
        elif method == "account/rateLimits/updated":
            rl = params.get("rateLimits") or {}
            windows = []
            for key in ("primary", "secondary"):
                w = rl.get(key)
                if isinstance(w, dict) and isinstance(w.get("usedPercent"), (int, float)) \
                        and isinstance(w.get("windowDurationMins"), (int, float)) and w["windowDurationMins"] > 0:
                    windows.append(Window(window_label(int(w["windowDurationMins"]) * 60), int(w["usedPercent"])))
            if windows:
                emit(LimitsUpdate("codex", format_windows(windows),
                                  tuple((w.label, w.used_percent) for w in windows)))
        elif method == "error":
            err = params.get("error") or {}
            emit(Failure(str(err.get("message") or err)))
        elif method == "turn/completed":
            n = self._parse(cp.TurnCompletedNotification, params, method, emit)
            if n is not None:
                raw, err = str(n.turn.status), n.turn.error
                detail = str(getattr(err, "message", err)) if err is not None else ""
            else:
                # the turn is over either way, so this one line is read off the
                # dict: dropping it would strand run_turn's loop on a dead queue
                turn = params.get("turn")
                turn = turn if isinstance(turn, dict) else {}
                err = turn.get("error")
                raw = str(turn.get("status") or "")
                detail = str(err.get("message") or err) if isinstance(err, dict) else ""
            status = "interrupted" if self._interrupted or raw == "interrupted" else (
                "failed" if raw == "failed" else "completed")
            self._parent_outcome = TurnOutcome(status, detail if status == "failed" else "")
            return self._finish_if_ready(emit)
        return None

    # -- process -------------------------------------------------------------

    def _spawn(self, cwd: str, tandem_id: str | None):
        """One app-server child with its stderr tail, and a queue its stdout
        lines land on. `None` on the queue is EOF."""
        proc = subprocess.Popen(
            [*self.binary, "app-server"], cwd=cwd,
            env=child_env(tandem_id=tandem_id),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1, start_new_session=True,
        )
        with self._lock:
            self._proc = proc
        tail: deque[str] = deque(maxlen=20)
        drain = threading.Thread(target=lambda: [tail.append(l.rstrip()) for l in proc.stderr],
                                 name="tandem-chat-codex-stderr", daemon=True)
        drain.start()
        q: queue.Queue = queue.Queue()

        def reader() -> None:
            for line in proc.stdout:
                try:
                    m = json.loads(line)
                except ValueError:
                    continue
                if isinstance(m, dict):
                    q.put(m)
            q.put(None)

        pump = threading.Thread(target=reader, name="tandem-chat-codex-reader", daemon=True)
        pump.start()
        return proc, q, tail, drain, pump

    def _teardown(self, proc, drain, pump, soft_timeout: float = 5.0) -> None:
        terminate(proc, soft=lambda: proc.stdin.close(), soft_timeout=soft_timeout)
        drain.join(2.0)         # the tail below must be the whole of stderr
        pump.join(2.0)          # neither reader outlives the turn it reads
        with self._lock:
            self._proc = None

    def _init_params(self) -> dict:
        return cp.InitializeParams(clientInfo=cp.ClientInfo(name="tandem", version=_VERSION)) \
            .model_dump(by_alias=True, exclude_none=True)

    def list_models(self, session) -> list[str]:
        """`model/list` on a thread-less app-server: one line per visible
        model, the slug first so the pin can be matched against it."""
        proc, q, tail, drain, pump = self._spawn(session.cwd, session.tandem_id)
        quiet = lambda ev: None
        try:
            r = self._call(proc, q, "initialize", self._init_params(), quiet, _DECLINE)
            if "error" in r:
                raise RuntimeError(f"initialize failed: {r['error'].get('message', r['error'])}")
            self._write(proc, {"jsonrpc": "2.0", "method": "initialized"})
            r = self._call(proc, q, "model/list", {}, quiet, _DECLINE)
            if "error" in r:
                raise RuntimeError(str(r["error"].get("message", r["error"])))
            data = (r.get("result") or {}).get("data") or []
            return [f"{m.get('model') or m.get('id')}  {m.get('displayName') or ''}".rstrip()
                    for m in data if isinstance(m, dict) and not m.get("hidden")]
        finally:
            self._teardown(proc, drain, pump, soft_timeout=2.0)

    def run_turn(self, session, native_id: str | None, prompt: str, model: str,
                 emit: Callable[[LiveEvent], None], answers: Answers,
                 command: str = "", review: dict | None = None) -> TurnOutcome:
        cfg = self.cfg                 # the mode this turn runs under, whatever /mode says later
        with self._lock:
            self._interrupted = self._interrupt_pending = False
        self._compacting = command == "compact"
        self._usage = ""
        self._thread_id = self._turn_id = None
        self._streamed_output.clear(); self._streamed_text.clear()
        self._agents.clear(); self._agent_names.clear(); self._quiet.clear()
        self._parent_outcome = None
        if self._compacting and not native_id:
            msg = "nothing to compact: codex has never run in this session"
            emit(Failure(msg)); emit(TurnFinished("failed", ""))
            return TurnOutcome("failed", msg)
        proc, q, tail, drain, pump = self._spawn(session.cwd, session.tandem_id)
        send = lambda obj: self._write(proc, obj)
        new_id: str | None = None
        outcome: TurnOutcome | None = None

        def fail(message: str) -> TurnOutcome:
            emit(Failure(message))
            emit(TurnFinished("failed", ""))
            # new_id is set once thread/start minted a thread: a failure after
            # that still owes the dispatcher the id, or the thread (and the
            # rollout codex wrote for it) is orphaned and the next turn mints
            # another one
            return TurnOutcome("failed", message, native_id=new_id)

        try:
            r = self._call(proc, q, "initialize", self._init_params(), emit, answers)
            if "error" in r:
                return fail(f"initialize failed: {r['error'].get('message', r['error'])}")
            self._write(proc, {"jsonrpc": "2.0", "method": "initialized"})
            overrides: dict = {}
            preset = CODEX_MODES.get(cfg.effective_mode)
            if preset:
                # a default only — an explicit codex_* key below still wins
                overrides = {"approvalPolicy": preset[0], "sandbox": preset[1]}
            if cfg.codex_approval_policy:
                overrides["approvalPolicy"] = cfg.codex_approval_policy
            if cfg.codex_sandbox:
                overrides["sandbox"] = cfg.codex_sandbox
            if review is not None:
                # a review never inherits the window's mode or its codex_* keys
                overrides = {"approvalPolicy": _REVIEW_POLICY[0], "sandbox": _REVIEW_POLICY[1]}
            if review is None and native_id:
                # the only place ask mode sends a policy, and only to undo a
                # review's: codex keeps the review's never/read-only on the
                # thread. setdefault, so a mode preset or codex_* key still wins
                restore = policy_after_review(get_adapter("codex").transcript_path(session.cwd, native_id))
                for k, v in (restore or {}).items():
                    overrides.setdefault(k, v)
            if native_id:
                params = cp.ThreadResumeParams(threadId=native_id, cwd=session.cwd, **overrides)
                r = self._call(proc, q, "thread/resume", params.model_dump(by_alias=True, exclude_none=True), emit, answers)
                if "error" in r:
                    msg = str(r["error"].get("message", r["error"]))
                    if "active writer" in msg:
                        msg = "this codex thread is open in another process: " + msg
                    return fail(msg)
                thread_id = ((r.get("result") or {}).get("thread") or {}).get("id") or native_id
            else:
                params = cp.ThreadStartParams(cwd=session.cwd, **overrides)
                r = self._call(proc, q, "thread/start", params.model_dump(by_alias=True, exclude_none=True), emit, answers)
                if "error" in r:
                    return fail(str(r["error"].get("message", r["error"])))
                thread_id = ((r.get("result") or {}).get("thread") or {}).get("id")
                if not thread_id:
                    return fail("thread/start returned no thread id")
                new_id = thread_id
            self._thread_id = thread_id
            if self._compacting:
                # the response is an empty object; completion is the
                # thread/compacted notification (or turn/completed when the
                # server frames the compaction as a turn), handled below
                r = self._call(proc, q, "thread/compact/start", {"threadId": thread_id}, emit, answers)
                if "error" in r:
                    return fail(str(r["error"].get("message", r["error"])))
            else:
                turn = cp.TurnStartParams(threadId=thread_id, input=[{"type": "text", "text": prompt}],
                                          model=model or None,
                                          outputSchema=review if review is not None else self.output_schema)
                r = self._call(proc, q, "turn/start", turn.model_dump(by_alias=True, exclude_none=True), emit, answers)
                if "error" in r:
                    return fail(str(r["error"].get("message", r["error"])))
                drift = None
                try:
                    turn_id = cp.TurnStartResponse.model_validate(r.get("result")).turn.id
                except ValidationError as exc:
                    # only turn.id is load-bearing here (interrupt needs it), so a
                    # response that drifts elsewhere still starts a usable turn
                    turn_id = ((r.get("result") or {}).get("turn") or {}).get("id")
                    drift = exc
                # the id and the pending flag change hands under one lock: an
                # interrupt() between the two would otherwise see no turn id,
                # park itself as pending, and never be read again
                with self._lock:
                    self._turn_id = turn_id
                    pending, self._interrupt_pending = self._interrupt_pending, False
                if not turn_id:
                    return fail("turn/start returned no turn id")
                if drift is not None:
                    emit(Failure(f"codex sent a response tandem cannot parse: "
                                 f"turn/start: {first_line(str(drift))}"))
                if pending:
                    # _interrupted is already set, so a turn that completes
                    # before this lands still reports interrupted
                    self._request(proc, "turn/interrupt",
                                  cp.TurnInterruptParams(threadId=thread_id, turnId=turn_id)
                                  .model_dump(by_alias=True, exclude_none=True))
            # one deadline for the whole compact, not a wait per message: a
            # server that keeps sending usage updates but never completes
            # must still end
            deadline = time.monotonic() + self.compact_timeout
            while True:
                try:
                    m = q.get(timeout=max(0.0, deadline - time.monotonic()) if self._compacting else None)
                except queue.Empty:
                    return fail(f"codex did not report the compaction within {self.compact_timeout:.0f}s")
                if m is None:
                    break
                outcome = self.handle(m, send, emit, answers)
                if outcome is not None:
                    break
        finally:
            self._teardown(proc, drain, pump)
            self._compacting = False        # a late thread/compacted must not end the next turn
        if outcome is None:
            status = "interrupted" if self._interrupted else "failed"
            outcome = TurnOutcome(status, error="\n".join(tail) or f"codex app-server exited {proc.returncode}")
            emit(TurnFinished(status, ""))
        outcome.native_id = new_id
        return outcome

    def interrupt(self) -> None:
        with self._lock:
            proc = self._proc
            thread_id, turn_id = self._thread_id, self._turn_id
            if proc is not None and proc.poll() is None and not (thread_id and turn_id) \
                    and not self._compacting:
                # the turn has not been acknowledged yet (spawn, initialize,
                # thread/resume): run_turn sends the interrupt the moment
                # turn/start returns a turn id. Set under the lock run_turn
                # reads it under, so the two cannot pass each other.
                self._interrupted = self._interrupt_pending = True
                return
        if proc is None or proc.poll() is not None:
            return
        if not (thread_id and turn_id):
            # a compact has no turn to interrupt: it never got a turn/start
            # response. Ending the process is the only way to stop it; the
            # read loop sees EOF and reports the turn interrupted.
            if self._compacting:
                self._interrupted = True
                self.close()
            return
        self._interrupted = True
        self._request(proc, "turn/interrupt",
                      cp.TurnInterruptParams(threadId=thread_id, turnId=turn_id)
                      .model_dump(by_alias=True, exclude_none=True))
        # Once the parent is done it cannot acknowledge an interrupt. Closing
        # the server interrupts its remaining workers and unblocks the reader.
        if self._parent_outcome is not None:
            self.close()

    def close(self) -> None:
        with self._lock:
            proc = self._proc
        if proc is not None:
            terminate(proc, soft=lambda: proc.stdin.close(), soft_timeout=2.0)
