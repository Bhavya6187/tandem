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


_AFTER_POS_SQL = (
    "SELECT id, time_created, data FROM message WHERE session_id = ?"
    " AND (time_created > ? OR (time_created = ? AND id > ?))"
    " ORDER BY time_created, id"
)


def _parts_for(conn: sqlite3.Connection, message_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT id, data FROM part WHERE message_id = ? ORDER BY id",
        (message_id,),
    ).fetchall()
    return [{**json.loads(r["data"]), "id": r["id"]} for r in rows]


def _is_closed(msg_data: dict) -> bool:
    time_completed = (msg_data.get("time") or {}).get("completed")
    return bool(time_completed) and msg_data.get("finish") not in (None, "tool-calls")


class OpencodeTurnReader:
    """Yields whole completed turns as single native objects. Tool parts
    mutate in place while a turn runs, so raw rows never cross this
    boundary; an incomplete turn is simply not yielded and re-read whole
    on a later poll."""

    def __init__(self, session_id: str, db: Path, cursor):
        self.session_id = session_id
        self.db = db
        self.cursor = cursor

    def poll(self):
        from ..tailer import TailedLine

        pos = self.cursor.pending.get("source_pos") or {"time": 0, "id": ""}
        out = []
        with connect(self.db) as conn:
            rows = conn.execute(
                _AFTER_POS_SQL,
                (self.session_id, pos["time"], pos["time"], pos["id"]),
            ).fetchall()
            msgs = [{**json.loads(r["data"]), "id": r["id"],
                     "_time_created": r["time_created"]} for r in rows]
            # partition into user-headed turns, in stored order
            turns: list[dict] = []
            for m in msgs:
                if m.get("role") == "user":
                    turns.append({"user": m, "assistants": []})
                elif turns:
                    turns[-1]["assistants"].append(m)
                # assistants before any user row: tail of a turn whose user
                # half was already consumed — cannot happen, because the
                # cursor only ever advances to a terminal assistant.
            index = self.cursor.line_index
            for i, t in enumerate(turns):
                followed_by_user = i + 1 < len(turns)
                a = t["assistants"]
                closed = bool(a) and _is_closed(a[-1])
                if not (followed_by_user or closed):
                    break   # incomplete turn; nothing after it is ready either
                turn_obj = {
                    "user": {"message": t["user"],
                             "parts": _parts_for(conn, t["user"]["id"])},
                    "assistants": [
                        {"message": m, "parts": _parts_for(conn, m["id"])}
                        for m in a
                    ],
                }
                last = a[-1] if a else t["user"]
                out.append(TailedLine(
                    line_index=index, end_offset=0, raw=turn_obj,
                    text=f"opencode turn {index}",
                    pos={"time": last["_time_created"], "id": last["id"]},
                ))
                index += 1
        return out


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

    # -- storage capabilities -------------------------------------------------

    def make_source_reader(self, session, cursor, transcript):
        return OpencodeTurnReader(session.native_id("opencode"), db_path(), cursor)

    def watch_paths(self, session, transcript) -> list[Path]:
        db = db_path()
        if db is None:
            return []
        return [db, db.with_name(db.name + "-wal")]

    def fast_forward_cursor(self, session, cursor) -> None:
        db = db_path()
        sid = session.native_id("opencode")
        if db is None or not sid:
            return
        with connect(db) as conn:
            row = conn.execute(
                "SELECT id, time_created FROM message WHERE session_id = ?"
                " ORDER BY time_created DESC, id DESC LIMIT 1", (sid,)
            ).fetchone()
        if row is not None:
            cursor.pending["source_pos"] = {"time": row["time_created"],
                                            "id": row["id"]}

    def pending_units(self, session, cursor) -> int:
        """Approximation: message rows past the cursor (a live turn's rows
        count until it closes) — good enough for status/doctor reporting."""
        db = db_path()
        sid = session.native_id("opencode")
        if db is None or not sid:
            return 0
        pos = cursor.pending.get("source_pos") or {"time": 0, "id": ""}
        with connect(db) as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM message WHERE session_id = ?"
                " AND (time_created > ? OR (time_created = ? AND id > ?))",
                (sid, pos["time"], pos["time"], pos["id"])).fetchone()
        return int(row[0])

    # -- flip gate ------------------------------------------------------------

    def session_status(self, session_id: str) -> str | None:
        """Busy iff the last message is a user prompt (turn starting) or an
        assistant still streaming (no time.completed / tool-calls finish)."""
        db = db_path()
        if db is None or not session_id:
            return None
        try:
            with connect(db) as conn:
                row = conn.execute(
                    "SELECT data FROM message WHERE session_id = ?"
                    " ORDER BY time_created DESC, id DESC LIMIT 1",
                    (session_id,)).fetchone()
        except sqlite3.Error:
            return None
        if row is None:
            return "waiting"
        data = json.loads(row["data"])
        if data.get("role") == "user":
            return "busy"
        return "waiting" if _is_closed(data) else "busy"

    # -- parsing --------------------------------------------------------------

    def parse_entry(self, raw: dict[str, Any], ctx: SessionContext) -> list[NormalizedEvent]:
        """One completed-turn object -> normalized events. Never raises on
        shape surprises; anything unrecognized degrades to a SystemEvent."""
        def sysev(subtype: str, text: str = "") -> SystemEvent:
            return SystemEvent(source="opencode", turn_index=ctx.turn_index,
                               subtype=subtype, text=text)

        if "user" not in raw or "assistants" not in raw:
            return [sysev(f"opencode:{raw.get('type', '?')}")]

        user_model = (raw["user"].get("message") or {}).get("model") or {}
        if user_model.get("providerID") == SENTINEL_PROVIDER:
            return [sysev("tandem_echo")]

        events: list[NormalizedEvent] = []
        ctx.turn_index += 1
        text = "\n".join(p.get("text", "") for p in raw["user"].get("parts", [])
                         if p.get("type") == "text")
        events.append(UserMessage(source="user", turn_index=ctx.turn_index,
                                  text=text))
        for a in raw["assistants"]:
            model = (a.get("message") or {}).get("modelID")
            for p in a.get("parts", []):
                ptype = p.get("type")
                if ptype == "text":
                    events.append(AssistantMessage(
                        source="opencode", turn_index=ctx.turn_index,
                        text=p.get("text", ""), model=model))
                elif ptype == "reasoning":
                    events.append(Thinking(source="opencode",
                                           turn_index=ctx.turn_index))
                elif ptype == "tool":
                    state = p.get("state") or {}
                    call_id = p.get("callID", "")
                    args = state.get("input")
                    events.append(ToolCall(
                        source="opencode", turn_index=ctx.turn_index,
                        call_id=call_id, tool=p.get("tool", ""),
                        arguments=args if isinstance(args, (dict, str)) else {}))
                    output = state.get("output", "")
                    if state.get("status") == "error":
                        output = str(state.get("error", output))
                    events.append(ToolResult(
                        source="opencode", turn_index=ctx.turn_index,
                        call_id=call_id,
                        output=output if isinstance(output, str) else str(output),
                        is_error=state.get("status") == "error"))
                else:
                    # step-start, step-finish, snapshot, patch, file, unknown
                    events.append(sysev(f"part:{ptype}"))
        return events

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

    def render_events(
        self, events: list[NormalizedEvent], ctx: SessionContext
    ) -> list[dict[str, Any]]:
        raise NotImplementedError

    def render_placeholder(self, text: str, ctx: SessionContext) -> list[dict[str, Any]]:
        raise NotImplementedError
