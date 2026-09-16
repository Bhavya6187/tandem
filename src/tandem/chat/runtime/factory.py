"""Build the window's runtime clients, one per participant."""

from __future__ import annotations

from .claude import ClaudeRuntime
from .codex import CodexRuntime
from .opencode import OpencodeRuntime

_CLASSES = {"claude": ClaudeRuntime, "codex": CodexRuntime, "opencode": OpencodeRuntime}


def make_runtimes(session, cfg) -> dict:
    return {h: _CLASSES[h](cfg) for h in session.participants if h in _CLASSES}
