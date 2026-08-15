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
from .base import HarnessAdapter, UsageMeter, UsageSnapshot


class _OpencodeUsageMeter(UsageMeter):
    """Fed whole turn units (OpencodeTurnReader yields each exactly once):
    every assistant row carries its request's tokens block. Rows tandem
    seeded are all zeros — skipped whole, so a synced shadow turn never
    blanks a real context reading. Reasoning is its own field here but a
    subset of output in codex's — folded into output so ↓ means the same
    thing on both slots."""

    def __init__(self):
        self._input = 0
        self._output = 0
        self._ctx: int | None = None

    def feed(self, raw: Any) -> None:
        if not isinstance(raw, dict):
            return
        for a in raw.get("assistants") or []:
            tokens = (a.get("message") or {}).get("tokens")
            if not isinstance(tokens, dict):
                continue

            def n(key: str) -> int:
                v = tokens.get(key)
                return v if isinstance(v, int) else 0

            cache = tokens.get("cache") or {}
            read = cache.get("read") if isinstance(cache.get("read"), int) else 0
            write = cache.get("write") if isinstance(cache.get("write"), int) else 0
            row_input = n("input") + read + write
            row_output = n("output") + n("reasoning")
            if row_input + row_output == 0:
                continue
            self._input += row_input
            self._output += row_output
            self._ctx = n("input") + read

    def snapshot(self) -> UsageSnapshot:
        return UsageSnapshot(ctx_tokens=self._ctx,
                             input_tokens=self._input,
                             output_tokens=self._output)

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
    install_hint = "npm install -g opencode-ai"

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
                missing = {"session", "message", "part"} - names
                if missing:
                    return False, f"expected table(s) missing: {sorted(missing)}"
                mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
                if str(mode).lower() != "wal":
                    return False, f"journal_mode is {mode!r}, expected wal"
                # sync writes rows directly; a read-only DB must fail closed
                # here, not one append at a time later
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    conn.execute("ROLLBACK")
                except sqlite3.OperationalError as exc:
                    return False, f"database not writable: {exc}"
        except sqlite3.Error as exc:
            return False, f"database unreadable: {exc}"
        return True, ""

    def mint_session_id(self) -> str:
        return mint_id("ses", descending=True)

    # -- doctor ---------------------------------------------------------------

    def validate_transcript(self, path: Path, session_id: str | None) -> list[str]:
        """Structural dry-resume for a DB-backed session. `path` is the DB."""
        problems: list[str] = []
        try:
            with connect(path) as conn:
                if session_id is not None:
                    row = conn.execute("SELECT 1 FROM session WHERE id = ?",
                                       (session_id,)).fetchone()
                    if row is None:
                        return [f"session {session_id} not in the opencode database"]
                mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
                if str(mode).lower() != "wal":
                    problems.append(f"journal_mode is {mode!r}, expected wal")
                last = conn.execute(
                    "SELECT data FROM message WHERE session_id = ?"
                    " ORDER BY time_created DESC, id DESC LIMIT 1",
                    (session_id,)).fetchone()
        except sqlite3.Error as exc:
            return [f"cannot read opencode database: {exc}"]
        if last is None:
            problems.append("no messages (session would render empty)")
            return problems
        data = json.loads(last["data"])
        if data.get("role") != "assistant" or not _is_closed(data):
            problems.append(
                "last message is not a completed assistant — the opencode TUI"
                " would show this session as perpetually working")
        return problems

    # -- storage capabilities -------------------------------------------------

    def make_source_reader(self, session, cursor, transcript):
        return OpencodeTurnReader(session.native_id("opencode"), db_path(), cursor)

    def make_usage_meter(self) -> UsageMeter:
        return _OpencodeUsageMeter()

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

    # -- session files -------------------------------------------------------

    def transcript_path(self, cwd: str, session_id: str) -> Path | None:
        """Opencode has no per-session file; the "transcript" is the DB, and
        it exists for a session exactly when the session row does."""
        db = db_path()
        if db is None or not session_id:
            return None
        try:
            with connect(db) as conn:
                row = conn.execute("SELECT 1 FROM session WHERE id = ?",
                                   (session_id,)).fetchone()
        except sqlite3.Error:
            return None
        return db if row is not None else None

    def prepare_shadow(self, ref, ctx) -> None:
        """Re-anchor the renderer's turn state to the rows actually in the
        DB. Crash-skip replay re-renders a unit for its ctx side effects,
        minting fresh ids the skipped append never inserted; without this
        the next unit would chain onto a phantom parent (an FK failure
        under foreign_keys=ON)."""
        sid = ctx.target_session_id
        if not sid:
            return
        try:
            with connect(ref) as conn:
                row = conn.execute(
                    "SELECT id, data FROM message WHERE session_id = ?"
                    " ORDER BY time_created DESC, id DESC LIMIT 1",
                    (sid,)).fetchone()
        except sqlite3.Error:
            return   # unreadable store: leave state; the append fails loudly
        st = ctx.state_for("opencode")
        if row is None:
            st["turn_user_id"] = None
            st["assistant_id"] = None
            return
        data = json.loads(row["data"])
        if data.get("role") == "assistant":
            st["assistant_id"] = row["id"]
            st["turn_user_id"] = data.get("parentID")
        else:
            st["turn_user_id"] = row["id"]
            st["assistant_id"] = None

    # -- rendering (shadow append) -------------------------------------------

    def _message_entry(self, sid: str, msg_id: str, data: dict, ms: int) -> dict:
        return {"table": "message", "id": msg_id, "session_id": sid,
                "time": ms, "data": data}

    def _part_entry(self, sid: str, msg_id: str, data: dict, ms: int) -> dict:
        return {"table": "part", "id": mint_id("prt"), "session_id": sid,
                "message_id": msg_id, "time": ms, "data": data}

    def _open_user(self, out: list, st: dict, sid: str, text: str, ms: int) -> None:
        uid = mint_id("msg")
        st["turn_user_id"] = uid
        st["assistant_id"] = None
        out.append(self._message_entry(sid, uid, {
            "role": "user", "time": {"created": ms}, "agent": "build",
            "model": {"providerID": SENTINEL_PROVIDER,
                      "modelID": SENTINEL_MODEL},
        }, ms))
        out.append(self._part_entry(sid, uid, {"type": "text", "text": text}, ms))

    def _ensure_assistant(self, out: list, st: dict, sid: str, ctx,
                          ms: int, model: str | None) -> str:
        if st.get("assistant_id"):
            if model:
                # the turn's assistant row was opened by a tool pair (no
                # model); a later text event carries the real one — upgrade
                # the still-in-flight row so display shows the source model
                for e in reversed(out):
                    if e["table"] == "message" and e["id"] == st["assistant_id"]:
                        if e["data"].get("modelID") == SENTINEL_MODEL:
                            e["data"]["modelID"] = model
                        break
                else:
                    # the row landed in an earlier synced unit: upgrade it in
                    # place (strictly sentinel -> real, tandem rows only, so
                    # replay is idempotent and native rows are untouchable)
                    out.append({"table": "message_model",
                                "id": st["assistant_id"], "session_id": sid,
                                "time": ms, "data": {"modelID": model}})
            return st["assistant_id"]
        if not st.get("turn_user_id"):
            # assistant content with no turn open (first sync after seed):
            # a context row keeps parentID valid
            self._open_user(out, st, sid, "[tandem] (context sync)", ms)
        aid = mint_id("msg")
        st["assistant_id"] = aid
        out.append(self._message_entry(sid, aid, {
            "parentID": st["turn_user_id"], "role": "assistant",
            "mode": "build", "agent": "build",
            "path": {"cwd": ctx.cwd, "root": ctx.cwd},
            "cost": 0,
            "tokens": {"input": 0, "output": 0, "reasoning": 0,
                       "cache": {"read": 0, "write": 0}},
            "modelID": model or SENTINEL_MODEL,
            "providerID": SENTINEL_PROVIDER,
            "time": {"created": ms, "completed": ms},
            "finish": "stop",
        }, ms))
        return aid

    def render_events(
        self, events: list[NormalizedEvent], ctx: SessionContext
    ) -> list[dict[str, Any]]:
        """Normalized events -> row-op entries. One user row per turn; one
        (closed) assistant row per turn carrying text and tool parts. The
        converter emits ToolCall/ToolResult adjacent, so pairing is local;
        a call that somehow arrives without its result closes as an error
        part rather than dangling (opencode's replay rejects dangles)."""
        sid = ctx.target_session_id
        st = ctx.state_for("opencode")
        out: list[dict[str, Any]] = []
        pending_call: ToolCall | None = None

        def flush_dangle(ms: int) -> None:
            nonlocal pending_call
            if pending_call is None:
                return
            aid = self._ensure_assistant(out, st, sid, ctx, ms, None)
            out.append(self._part_entry(sid, aid, self._tool_part(
                pending_call, output="(tool result not recorded)",
                is_error=True), ms))
            pending_call = None

        for ev in events:
            ms = _now_ms()
            if ev.kind == "user_message":
                flush_dangle(ms)
                self._open_user(out, st, sid, ev.text, ms)
            elif ev.kind == "assistant_message":
                flush_dangle(ms)
                aid = self._ensure_assistant(out, st, sid, ctx, ms, ev.model)
                out.append(self._part_entry(
                    sid, aid, {"type": "text", "text": ev.text}, ms))
            elif ev.kind == "tool_call":
                flush_dangle(ms)
                pending_call = ev
            elif ev.kind == "tool_result":
                if pending_call is None:
                    continue   # converter never emits an orphan result natively
                aid = self._ensure_assistant(out, st, sid, ctx, ms, None)
                out.append(self._part_entry(sid, aid, self._tool_part(
                    pending_call, output=ev.output, is_error=ev.is_error), ms))
                pending_call = None
            # thinking/system: dropped (no reasoning parts by spec)
        flush_dangle(_now_ms())
        return out

    def _tool_part(self, call: ToolCall, output: str, is_error: bool) -> dict:
        ms = _now_ms()
        return {
            "type": "tool", "tool": call.tool, "callID": call.call_id,
            "state": {
                "status": "error" if is_error else "completed",
                "input": call.arguments if isinstance(call.arguments, dict)
                         else {"input": call.arguments},
                **({"error": output} if is_error else {"output": output}),
                "title": "", "metadata": {},
                "time": {"start": ms, "end": ms},
            },
        }

    def render_placeholder(self, text: str, ctx: SessionContext) -> list[dict[str, Any]]:
        sid = ctx.target_session_id
        st = ctx.state_for("opencode")
        out: list[dict[str, Any]] = []
        ms = _now_ms()
        self._open_user(out, st, sid, text, ms)
        self._ensure_assistant(out, st, sid, ctx, ms, None)
        aid = st["assistant_id"]
        out.append(self._part_entry(
            sid, aid, {"type": "text", "text": "[tandem] (turn recorded as a"
                       " placeholder; see the note above)"}, ms))
        return out

    # -- shadow append (transactional, idempotent) ----------------------------

    def shadow_append(self, ref: Path, entries: list[dict]) -> None:
        """One transaction per synced unit. A lock outlasting busy_timeout
        raises ShadowBusy — the transaction rolled back, nothing landed, and
        the tailer retries the whole unit next tick (spec: error handling)."""
        from .base import ShadowBusy

        if not entries:
            return
        conn = connect(ref)
        try:
            try:
                conn.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as exc:
                # only contention is retryable; a readonly/corrupt store must
                # surface, not spin as ShadowBusy forever
                if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                    raise ShadowBusy(str(exc)) from exc
                raise
            for e in entries:
                if e["table"] == "message":
                    conn.execute(
                        "INSERT OR IGNORE INTO message"
                        " (id, session_id, time_created, time_updated, data)"
                        " VALUES (?, ?, ?, ?, ?)",
                        (e["id"], e["session_id"], e["time"], e["time"],
                         json.dumps(e["data"])))
                elif e["table"] == "message_model":
                    conn.execute(
                        "UPDATE message SET data = json_set(data,"
                        " '$.modelID', ?) WHERE id = ?"
                        " AND json_extract(data, '$.providerID') = ?"
                        " AND json_extract(data, '$.modelID') = ?",
                        (e["data"]["modelID"], e["id"],
                         SENTINEL_PROVIDER, SENTINEL_MODEL))
                else:
                    conn.execute(
                        "INSERT OR IGNORE INTO part"
                        " (id, message_id, session_id, time_created,"
                        " time_updated, data) VALUES (?, ?, ?, ?, ?, ?)",
                        (e["id"], e["message_id"], e["session_id"], e["time"],
                         e["time"], json.dumps(e["data"])))
            conn.execute("UPDATE session SET time_updated = ? WHERE id = ?",
                         (entries[-1]["time"], entries[0]["session_id"]))
            conn.commit()
        finally:
            conn.close()

    def shadow_intent(self, ref: Path, entries: list[dict]) -> dict:
        return {"ids": [(e["table"], e["id"]) for e in entries]}

    def intent_landed(self, ref: Path, intent: dict) -> bool:
        # anchor on the first INSERT op: update ops (message_model) target
        # rows that pre-exist the append, so their presence proves nothing
        ids = intent.get("ids") or []
        first_insert = next(
            ((t, i) for t, i in ids if t in ("message", "part")), None)
        if first_insert is None:
            return False
        table, first = first_insert
        try:
            with connect(ref) as conn:
                row = conn.execute(
                    f"SELECT 1 FROM {table} WHERE id = ?", (first,)).fetchone()
        except sqlite3.Error:
            return False
        return row is not None

    # -- launching -----------------------------------------------------------

    def interactive_argv(self, session_id: str | None, fresh: bool) -> list[str]:
        assert session_id, "opencode sessions are created at pair time"
        return [self.binary, "-s", session_id]

    def oneoff_argv(self, session_id: str, prompt: str) -> list[str]:
        return [self.binary, "run", "-s", session_id, prompt]

    def hook_argv_extra(self, sentinel: Path) -> list[str]:
        # No per-invocation hook flag exists; the tailer's fs-watch on the
        # -wal file is the wake-up signal (watch_paths).
        return []

    def quit_keystrokes(self) -> list[bytes]:
        # LIVE-VERIFY before release (spec item 2): pinned per version like
        # the claude [^C,^C,^C] recipe; the SIGTERM ladder backstops.
        return [b"\x03", b"\x03"]

    # -- shadow birth (delegated to `opencode import`) ------------------------

    def create_shadow_transcript(
        self, cwd: str, session_id: str, ctx: SessionContext, note: str
    ) -> Path:
        """Tandem never hand-writes opencode session rows. Mint the ids,
        write a minimal export-format file, and let `opencode import` run
        its own schema decoders, project bootstrap, and directory re-homing.
        One subprocess, once per session, off the sync path."""
        import tempfile

        now = _now_ms()
        version = self.detect_version() or "0.0.0"
        user_id = mint_id("msg")
        asst_id = mint_id("msg")
        sentinel_model = {"providerID": SENTINEL_PROVIDER,
                          "modelID": SENTINEL_MODEL}
        payload = {
            "info": {
                # projectID/directory/path are placeholders: import re-homes
                # them from its own instance context. agent/model omitted
                # (optional session-level; opencode's default applies).
                # slug is schema-required (live-verified: import rejects the
                # payload `at ["slug"]` without it) but not unique — opencode
                # mints adjective-noun pairs; tandem's marks provenance.
                "id": session_id,
                "slug": "tandem-pair",
                "projectID": "tandem-import",
                "directory": cwd,
                "title": "tandem paired session",
                "version": version,
                "time": {"created": now, "updated": now},
                "cost": 0,
                "tokens": {"input": 0, "output": 0, "reasoning": 0,
                           "cache": {"read": 0, "write": 0}},
            },
            "messages": [
                {"info": {"id": user_id, "sessionID": session_id,
                          "role": "user", "time": {"created": now},
                          "agent": "build", "model": sentinel_model},
                 "parts": [{"id": mint_id("prt"), "sessionID": session_id,
                            "messageID": user_id, "type": "text",
                            "text": note}]},
                {"info": {"id": asst_id, "sessionID": session_id,
                          "role": "assistant", "parentID": user_id,
                          "time": {"created": now, "completed": now},
                          "modelID": SENTINEL_MODEL,
                          "providerID": SENTINEL_PROVIDER,
                          "mode": "build", "agent": "build",
                          "path": {"cwd": cwd, "root": cwd},
                          "cost": 0, "finish": "stop",
                          "tokens": {"input": 0, "output": 0, "reasoning": 0,
                                     "cache": {"read": 0, "write": 0}}},
                 "parts": [{"id": mint_id("prt"), "sessionID": session_id,
                            "messageID": asst_id, "type": "text",
                            "text": "[tandem] Session created; context syncs"
                                    " in from the paired session."}]},
            ],
        }
        with tempfile.NamedTemporaryFile(
            "w", suffix=".json", prefix="tandem-oc-seed-", delete=False
        ) as f:
            json.dump(payload, f)
            seed_file = Path(f.name)
        try:
            out = _run([self.binary, "import", str(seed_file)], cwd=cwd,
                       capture_output=True, text=True, timeout=120)
            if out.returncode != 0:
                tail = (out.stderr or out.stdout or "").strip().splitlines()
                raise RuntimeError(
                    f"opencode import failed ({out.returncode})"
                    + (f": {tail[-1][:200]}" if tail else ""))
        finally:
            seed_file.unlink(missing_ok=True)
        db = self.transcript_path(cwd, session_id)
        if db is None:
            raise RuntimeError(
                f"opencode import reported success but session {session_id}"
                " is not in the database")
        ctx.state_for("opencode")["turn_user_id"] = user_id
        ctx.state_for("opencode")["assistant_id"] = None
        return db
