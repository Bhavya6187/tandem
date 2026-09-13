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


def child_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """The user's environment minus every CLAUDE* marker: an inherited one
    changes how claude behaves (the frame's pty probes found it swallowing
    quit keys), and codex/opencode never need them."""
    env = dict(os.environ if base is None else base)
    for key in list(env):
        if key.startswith("CLAUDE"):
            del env[key]
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
    group, SIGKILL. Returns the rung that worked: dead|soft|term|kill."""
    if proc.poll() is not None:
        return "dead"
    if soft is not None:
        try:
            soft()
        except Exception:
            pass
        if _wait(proc, soft_timeout):
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
