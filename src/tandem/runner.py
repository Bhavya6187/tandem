"""Session runner: PTY passthrough of the active harness plus the tail loop.

The tail loop runs on a background thread while the user works in the native
CLI on the foreground thread. Each new transcript line is parsed into
normalized events and handed to a sink (M2: an event logger; M3 swaps in the
sync engine). The durable cursor advances only after the sink has handled a
line, so a crash resumes from the last confirmed entry.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Callable, Protocol

from . import paths
from .events import SessionContext
from .harness import get_adapter
from .ptyrun import run_in_pty
from .state import PairedSession, StateStore, SyncCursor
from .tailer import JsonlTailer, TailedLine, TranscriptTruncated, TranscriptWatcher
from .util import json_line


class EventSink(Protocol):
    """Sinks own translation of raw lines (parsing mutates the translation
    context, so it must happen exactly once, inside the sink)."""

    def handle(self, line: TailedLine, ctx: SessionContext, cursor: "SyncCursor") -> None: ...

    def close(self) -> None: ...


class EventLogger:
    """Debug sink: parse and append normalized events to ~/.tandem/logs/."""

    def __init__(self, tandem_id: str, source: str):
        self.adapter = get_adapter(source)
        self.path = paths.log_dir() / f"{tandem_id}-{source}.events.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "ab")

    def handle(self, line: TailedLine, ctx: SessionContext, cursor: "SyncCursor") -> None:
        events = [] if line.raw is None else self.adapter.parse_entry(line.raw, ctx)
        for ev in events:
            record = ev.model_dump(exclude_none=True)
            record["raw_line_index"] = line.line_index
            self._fh.write(json_line(record))
        self._fh.flush()

    def close(self) -> None:
        self._fh.close()


def ctx_from_cursor(session: PairedSession, source: str, cursor: SyncCursor) -> SessionContext:
    direction = "claude->codex" if source == "claude" else "codex->claude"
    pending = dict(cursor.pending)
    leaf = pending.pop("claude_leaf_uuid", None)
    calls = pending.pop("pending_calls", {})
    return SessionContext(
        tandem_id=session.tandem_id,
        cwd=session.cwd,
        direction=direction,
        turn_index=cursor.turn_index,
        pending_calls=calls,
        claude_leaf_uuid=leaf,
        claude_session_id=session.claude_session_id,
        codex_session_id=session.codex_session_id,
    )


def ctx_to_cursor(ctx: SessionContext, cursor: SyncCursor) -> None:
    cursor.turn_index = ctx.turn_index
    # update, not replace: the sync engine keeps its own keys (intent,
    # last_placeholder_turn) in the same dict
    cursor.pending.update(
        {
            "pending_calls": ctx.pending_calls,
            "claude_leaf_uuid": ctx.claude_leaf_uuid,
        }
    )


class TailLoop:
    """Tails one source transcript, parses, feeds the sink, persists the
    cursor. Usable standalone in tests (no PTY required)."""

    def __init__(
        self,
        store: StateStore,
        session: PairedSession,
        source: str,
        transcript: Path,
        sink: EventSink,
    ):
        self.store = store
        self.session = session
        self.source = source
        self.sink = sink
        self.cursor = store.get_cursor(session.tandem_id, source)
        self.ctx = ctx_from_cursor(session, source, self.cursor)
        self.tailer = JsonlTailer(
            transcript, start_offset=self.cursor.byte_offset,
            start_line=self.cursor.line_index,
        )
        self.errors: list[str] = []

    def drain(self) -> int:
        """Process everything new; returns number of lines consumed."""
        try:
            lines = self.tailer.poll()
        except TranscriptTruncated as exc:
            self.errors.append(str(exc))
            return 0
        for line in lines:
            self.sink.handle(line, self.ctx, self.cursor)
            self.cursor.byte_offset = line.end_offset
            self.cursor.line_index = line.line_index + 1
            ctx_to_cursor(self.ctx, self.cursor)
        if lines:
            self.store.save_cursor(self.cursor)
            self.store.touch_sync(self.session.tandem_id)
        return len(lines)


# Rollouts tandem wrote itself: "tandem" heads a seeded shadow (codex
# adapter), "tandem-sub" heads a subagent rollout (ops.fork_shadow for
# --context full, ops.seed_sub_rollout for the cold path). Both live in
# codex's sessions dir with a fresh mtime and the session cwd, so discovery
# must skip them or a live worker gets adopted as the pair's real codex
# session.
_TANDEM_ORIGINATORS = ("tandem", "tandem-sub")


def await_codex_rollout(cwd: str, after: float, timeout: float | None = None) -> Path | None:
    """Find the rollout file codex just created for this cwd (codex mints its
    own session id; tandem discovers it from the filesystem). Rollouts tandem
    authored are never candidates."""
    deadline = None if timeout is None else time.time() + timeout
    while True:
        for p in paths.iter_codex_rollouts_newest_first():
            try:
                if p.stat().st_mtime < after - 1:
                    break  # newest-first: everything after this is older
                with open(p, "rb") as f:
                    first = f.readline()
                meta = json.loads(first) if first.strip() else {}
                if (
                    meta.get("type") == "session_meta"
                    and meta.get("payload", {}).get("cwd") == cwd
                    and meta.get("payload", {}).get("originator")
                    not in _TANDEM_ORIGINATORS
                ):
                    return p
            except (OSError, json.JSONDecodeError):
                continue
        if deadline is None or time.time() >= deadline:
            return None
        time.sleep(0.3)


SinkFactory = Callable[[StateStore, PairedSession, str], EventSink]


class InteractiveRunner:
    """Runs the active harness in PTY passthrough with the tail loop on a
    background thread."""

    def __init__(self, session: PairedSession, sink_factory: SinkFactory):
        self.session = session
        self.sink_factory = sink_factory

    def run(self) -> int:
        session = self.session
        active = session.active
        adapter = get_adapter(active)
        active_sid = getattr(session, f"{active}_session_id")

        transcript: Path | None = None
        fresh = True
        if active_sid:
            transcript = adapter.transcript_path(session.cwd, active_sid)
            fresh = transcript is None
            if fresh and active == "claude":
                transcript = adapter.expected_transcript_path(session.cwd, active_sid)

        sentinel = paths.tandem_home() / "tmp" / f"{session.tandem_id}-{active}.turn"
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        argv = adapter.interactive_argv(active_sid, fresh)
        argv += adapter.hook_argv_extra(sentinel)

        stop = threading.Event()
        spawn_time = time.time()
        errors: list[str] = []

        def tail_thread() -> None:
            # Own store/connection: sqlite handles are thread-bound, and the
            # sink (sync engine) is built here so it shares this store.
            with StateStore() as store:
                current = store.get_session(session.tandem_id) or session
                path = transcript
                if path is None:
                    # codex minting its own session: wait for the rollout.
                    while not stop.is_set():
                        found = await_codex_rollout(session.cwd, spawn_time, timeout=0.5)
                        if found:
                            sid = paths.codex_rollout_session_id(found)
                            if sid:
                                store.set_native_session_id(session.tandem_id, "codex", sid)
                                current = store.get_session(session.tandem_id) or current
                            path = found
                            break
                    if path is None:
                        return
                try:
                    sink = self.sink_factory(store, current, active)
                except Exception as exc:
                    errors.append(f"sync disabled: {exc}")
                    return
                watcher = TranscriptWatcher()
                watcher.watch(path)
                watcher.watch(sentinel)
                watcher.start()
                loop = TailLoop(store, current, active, path, sink)
                # `tandem sub --context full` drains this same cursor row from
                # a separate process, holding `ops._sub_lock()` across its
                # drain-then-fork. Two concurrent drains of one cursor
                # translate the same lines twice — duplicate turns and call ids
                # in the shadow and in the fork — so the tail thread takes the
                # same flock. Kept tight around the drain itself: the wait
                # between iterations must not hold it. (Imported here: `ops`
                # imports this module.)
                from . import ops
                try:
                    while not stop.is_set():
                        with ops._sub_lock():
                            loop.drain()
                        watcher.wait()
                    # final drain after the CLI exits
                    with ops._sub_lock():
                        loop.drain()
                    errors.extend(loop.errors)
                finally:
                    watcher.stop()
                    sink.close()

        thread = threading.Thread(target=tail_thread, name="tandem-tail", daemon=True)
        thread.start()
        try:
            code = run_in_pty(argv, cwd=session.cwd)
        finally:
            stop.set()
            thread.join(timeout=10)
            sentinel.unlink(missing_ok=True)
        for err in errors:
            print(f"tandem: sync error: {err}")
        return code
