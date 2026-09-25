"""The per-harness half of the navigator: fork the shadow, run one headless
turn on the fork with a fresh runtime client, hand back what the model
said, delete the fork. Every reviewer runs read-only, denies every
approval, and never touches the dispatcher's own runtime instance."""

from __future__ import annotations

import dataclasses
import json
import threading

from .. import ops, paths
from ..harness import get_adapter
from ..sync import SyncSetupError
from .events import ApprovalRequest, Failure, LiveEvent, QuestionRequest, TextDelta
from .navigator import ReviewError, ReviewResult
from .runtime.claude import ClaudeRuntime
from .runtime.codex import CodexRuntime

# read-only by allowlist, by denylist and by permission mode: a fork must never write,
# and the diff it would reach for with git is already in the prompt
_CLAUDE_REVIEW_TOOLS = ["Read", "Grep", "Glob"]
_CLAUDE_DENIED_TOOLS = ["Edit", "Write", "MultiEdit", "NotebookEdit", "Agent", "Task"]


class DenyAll:
    """The review's Answers: nobody is at the keyboard for a fork."""

    def approve(self, req: ApprovalRequest) -> str:
        return "deny"

    def answer(self, req: QuestionRequest) -> str:
        return ""


class Collector:
    """The review's emit sink: the final text and any failures, nothing painted."""

    def __init__(self) -> None:
        self._parts: list[str] = []
        self.failures: list[str] = []

    def __call__(self, ev: LiveEvent) -> None:
        if isinstance(ev, TextDelta):
            self._parts.append(ev.text)
        elif isinstance(ev, Failure):
            self.failures.append(ev.message)

    @property
    def text(self) -> str:
        return "".join(self._parts)


class CodexReviewer:
    harness = "codex"

    def __init__(self, cfg, store, *, binary: list[str] | None = None):
        self.cfg, self.store, self.binary = cfg, store, binary
        self._rt: CodexRuntime | None = None
        self._lock = threading.Lock()

    def review(self, session, model: str, prompt: str, schema: dict,
               shadow_lock: threading.Lock) -> ReviewResult:
        try:
            with shadow_lock, ops._sub_lock():
                fork_id, fork_path = ops.fork_shadow(self.store, session)
        except SyncSetupError as exc:
            raise ReviewError(f"codex fork: {exc}") from exc
        cfg = dataclasses.replace(self.cfg, skip_permissions=False,
                                  codex_approval_policy="never", codex_sandbox="read-only")
        rt = CodexRuntime(cfg, binary=self.binary, output_schema=schema)
        col = Collector()
        try:
            with self._lock:
                self._rt = rt
            outcome = rt.run_turn(session, fork_id, prompt, model, col, DenyAll())
        finally:
            with self._lock:
                self._rt = None
            fork_path.unlink(missing_ok=True)
        if outcome.status != "completed":
            raise ReviewError(outcome.error or f"codex review {outcome.status}")
        return ReviewResult(None, col.text)

    def close(self) -> None:
        with self._lock:
            rt = self._rt
        if rt is not None:
            rt.close()


class ClaudeReviewer:
    harness = "claude"

    def __init__(self, cfg, *, binary: list[str] | None = None):
        self.cfg, self.binary = cfg, binary
        self._rt: ClaudeRuntime | None = None
        self._lock = threading.Lock()

    def review(self, session, model: str, prompt: str, schema: dict,
               shadow_lock: threading.Lock) -> ReviewResult:
        sid = session.native_id("claude")
        adapter = get_adapter("claude")
        if not sid or adapter.transcript_path(session.cwd, sid) is None:
            raise ReviewError("claude shadow transcript missing")
        cfg = dataclasses.replace(self.cfg, skip_permissions=False)
        extra = ["--fork-session", "--json-schema", json.dumps(schema),
                 "--permission-mode", "default",
                 "--allowedTools", *_CLAUDE_REVIEW_TOOLS,
                 "--disallowedTools", *_CLAUDE_DENIED_TOOLS]
        forked: dict = {}
        released = threading.Event()

        def on_init(new_id: str) -> None:
            # the fork has read the shadow once init arrives; the dispatcher
            # may drain into it again from here
            forked["id"] = new_id
            if not released.is_set():
                released.set()
                shadow_lock.release()

        rt = ClaudeRuntime(cfg, binary=self.binary, extra_args=extra, on_init=on_init)
        col = Collector()
        shadow_lock.acquire()
        try:
            with self._lock:
                self._rt = rt
            outcome = rt.run_turn(session, sid, prompt, model, col, DenyAll())
        finally:
            if not released.is_set():
                released.set()
                shadow_lock.release()
            with self._lock:
                self._rt = None
            new_id = forked.get("id")
            if new_id and new_id != sid:
                paths.claude_transcript_path(session.cwd, new_id).unlink(missing_ok=True)
        if outcome.status != "completed":
            raise ReviewError(outcome.error or f"claude review {outcome.status}")
        return ReviewResult(outcome.structured, col.text)

    def close(self) -> None:
        with self._lock:
            rt = self._rt
        if rt is not None:
            rt.close()


def make_reviewer(harness: str, cfg, store, *, binaries: dict | None = None):
    binaries = binaries or {}
    if harness == "codex":
        return CodexReviewer(cfg, store, binary=binaries.get("codex"))
    if harness == "claude":
        return ClaudeReviewer(cfg, binary=binaries.get("claude"))
    raise ValueError(f"{harness} cannot be the navigator")
