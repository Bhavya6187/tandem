"""Opencode adapter.

Session storage observed on opencode 1.18.15 (docs/formats.md and the spec's
reference index into the checkout at ~/git/opencode):
- everything lives in one WAL-mode SQLite DB (`opencode db path`), tables
  session / message / part with JSON `data` payloads
- ids: <prefix>_ + 12 hex chars (48-bit ms*4096+counter; sessions bitwise-NOT
  it so they sort descending) + 14 random base62
- messages order by (time_created, id); parts by part id alone
- tool parts mutate IN PLACE (pending -> running -> completed), so reads
  consume whole completed turns, never raw rows
- external writes are the `opencode import` recipe: plain INSERTs; a session
  whose last message is not a completed assistant renders as "working"
- tandem-authored rows carry providerID "tandem": degrades reasoning replay
  and marks echoes
"""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Any

from .. import compat
from ..events import (
    AssistantMessage,
    NormalizedEvent,
    SessionContext,
    SystemEvent,
    Thinking,
    ToolCall,
    ToolResult,
    UserMessage,
)
from .base import HarnessAdapter

SENTINEL_PROVIDER = "tandem"
SENTINEL_MODEL = "<synced>"

# seam for tests (matches ops.py's convention)
_run = subprocess.run

_ID_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
_id_state = {"ms": 0, "counter": 0}


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def mint_id(prefix: str, descending: bool = False) -> str:
    """Port of opencode's identifier scheme (packages/schema/src/identifier.ts):
    48-bit big-endian hex of ms*4096+counter (counter resets each ms), NOT'd
    for descending ids, then 14 random base62 chars."""
    ms = _now_ms()
    if ms != _id_state["ms"]:
        _id_state["ms"] = ms
        _id_state["counter"] = 0
    _id_state["counter"] += 1
    value = (ms * 0x1000 + _id_state["counter"]) & 0xFFFFFFFFFFFF
    if descending:
        value = ~value & 0xFFFFFFFFFFFF
    suffix = "".join(secrets.choice(_ID_ALPHABET) for _ in range(14))
    return f"{prefix}_{value:012x}{suffix}"


_db_cache: dict = {}


def _reset_db_cache() -> None:
    _db_cache.clear()


def db_path() -> Path | None:
    """Opencode's DB. $OPENCODE_DB (opencode's own override flag) wins; else
    `opencode db path` once per process. None on ANY failure — discovery
    fails closed (the adapter is dropped from the usable set), never a
    guessed default path (the filename is channel-suffixed)."""
    env = os.environ.get("OPENCODE_DB")
    if env:
        return Path(env)
    if "path" in _db_cache:
        return _db_cache["path"]
    try:
        out = _run(["opencode", "db", "path"], capture_output=True, text=True,
                   timeout=20)
        line = out.stdout.strip().splitlines()[-1] if out.returncode == 0 and \
            out.stdout.strip() else ""
        p = Path(line) if line else None
        _db_cache["path"] = p if p is not None and p.is_file() else None
    except (OSError, subprocess.TimeoutExpired):
        _db_cache["path"] = None
    return _db_cache["path"]


def connect(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


class OpencodeAdapter(HarnessAdapter):
    id = "opencode"
    display_name = "opencode"
    binary = "opencode"

    # -- environment ---------------------------------------------------------

    def detect_version(self) -> str | None:
        return compat.detect_cli_version(self.binary)

    def version_supported(self, version_text: str) -> bool:
        return compat.version_supported("opencode", version_text)

    def runtime_ready(self) -> tuple[bool, str]:
        db = db_path()
        if db is None:
            return False, "database not discoverable (`opencode db path` failed)"
        try:
            with connect(db) as conn:
                names = {r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
        except sqlite3.Error as exc:
            return False, f"database unreadable: {exc}"
        missing = {"session", "message", "part"} - names
        if missing:
            return False, f"expected table(s) missing: {sorted(missing)}"
        return True, ""

    def mint_session_id(self) -> str:
        return mint_id("ses", descending=True)

    # -- temporary stubs (replaced by Tasks 11-13) ---------------------------

    def transcript_path(self, cwd: str, session_id: str) -> Path | None:
        raise NotImplementedError

    def create_shadow_transcript(
        self, cwd: str, session_id: str, ctx: SessionContext, note: str
    ) -> Path:
        raise NotImplementedError

    def interactive_argv(self, session_id: str | None, fresh: bool) -> list[str]:
        raise NotImplementedError

    def oneoff_argv(self, session_id: str, prompt: str) -> list[str]:
        raise NotImplementedError

    def hook_argv_extra(self, sentinel: Path) -> list[str]:
        raise NotImplementedError

    def parse_entry(self, raw: dict[str, Any], ctx: SessionContext) -> list[NormalizedEvent]:
        raise NotImplementedError

    def render_events(
        self, events: list[NormalizedEvent], ctx: SessionContext
    ) -> list[dict[str, Any]]:
        raise NotImplementedError

    def render_placeholder(self, text: str, ctx: SessionContext) -> list[dict[str, Any]]:
        raise NotImplementedError
