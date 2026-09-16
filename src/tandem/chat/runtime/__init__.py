"""One client per harness, one calling convention. Each client owns its
process(es) and protocol; the dispatcher only ever calls run_turn,
interrupt and close. Shared here: the child environment rule, the kill
ladder for plain subprocesses, and the one-line summaries the renderer
prints for tool calls."""

from __future__ import annotations

import json
import os
import signal
import subprocess
from typing import Callable, Protocol

from ..events import Answers, LiveEvent, TurnOutcome


class RuntimeClient(Protocol):
    harness: str

    def run_turn(self, session, native_id: str | None, prompt: str, model: str,
                 emit: Callable[[LiveEvent], None], answers: Answers) -> TurnOutcome: ...

    def interrupt(self) -> None: ...

    def close(self) -> None: ...


# What claude exports to the processes it spawns so a child knows it is
# nested and where its parent's plumbing lives. Inherited by a headless
# claude that tandem starts from inside a claude session, they change how
# that child behaves (the frame's pty probes found it swallowing quit keys).
# Every other CLAUDE* variable is the user's own configuration —
# CLAUDE_CONFIG_DIR names the very store tandem syncs, CLAUDE_CODE_USE_BEDROCK
# picks the provider — and has to reach the child untouched.
_NESTING_MARKERS = frozenset({
    "CLAUDECODE", "CLAUDE_PID", "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_EXECPATH", "CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_MESSAGING_TOKEN",
    "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_SSE_PORT",
})


def child_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """The user's environment minus the markers claude sets for its own
    children (_NESTING_MARKERS): codex and opencode never read them, and a
    headless claude must not take itself for a nested one."""
    env = dict(os.environ if base is None else base)
    for key in _NESTING_MARKERS:
        env.pop(key, None)
    return env


def _wait(proc: subprocess.Popen, timeout: float) -> bool:
    try:
        proc.wait(timeout=timeout)
        return True
    except subprocess.TimeoutExpired:
        return False


def _signal_group(proc: subprocess.Popen, sig: int) -> None:
    # every runtime spawns with start_new_session=True, so the child's pid
    # is its group id and its tool children go down with it
    try:
        os.killpg(proc.pid, sig)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.send_signal(sig)
        except OSError:
            pass


def terminate(proc: subprocess.Popen, *, soft: Callable[[], None] | None = None,
              soft_timeout: float = 5.0, term_timeout: float = 2.0) -> str:
    """The ladder: `soft` (close stdin, send a quit request), SIGTERM to the
    group, SIGKILL. Returns the rung that worked: dead|soft|term|kill.

    Every rung tandem climbs signals the whole group, not just its leader:
    a harness that goes quietly on EOF does not wait for the tool command
    it was running, and that command sits in the group tandem created. Only
    `dead` leaves the group alone — a harness that ended on its own owns its
    children's fate, as it does under its native CLI."""
    if proc.poll() is not None:
        return "dead"
    if soft is not None:
        try:
            soft()
        except Exception:
            pass
        if _wait(proc, soft_timeout):
            # the group outlives its leader while any member remains; an
            # empty one raises, which _signal_group swallows
            _signal_group(proc, signal.SIGTERM)
            return "soft"
    _signal_group(proc, signal.SIGTERM)
    if _wait(proc, term_timeout):
        return "term"
    _signal_group(proc, signal.SIGKILL)
    _wait(proc, 1.0)
    return "kill"


def first_line(text: str, limit: int = 80) -> str:
    stripped = (text or "").strip()
    if not stripped:
        return ""
    line = stripped.splitlines()[0]
    return line if len(line) <= limit else line[: limit - 1] + "…"


_SUMMARY_KEYS = ("command", "cmd", "file_path", "path", "pattern", "query",
                 "url", "prompt", "description")


def summarize_args(tool: str, args) -> str:
    """One line for a tool row: the argument a human would look for first."""
    if isinstance(args, str):
        return first_line(args)
    if isinstance(args, dict):
        for key in _SUMMARY_KEYS:
            v = args.get(key)
            if isinstance(v, str) and v.strip():
                return first_line(v)
        if args:
            return first_line(json.dumps(args, ensure_ascii=False))
    return ""
