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
from .config import load_frame_config, load_harness_args
from .events import SessionContext
from .harness import get_adapter
from .ptyrun import FrameIO, PtyControl, run_in_pty
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


def _mtime(path: Path | None) -> float:
    if path is None:
        return 0.0
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


# Quiescence means two different things depending on whether the
# turn-complete marker was wired at launch, so it gets two constants.
_QUIESCE_S = 2.0            # marker-less: the only turn-boundary signal there is
_MISSED_MARKER_VALVE_S = 30.0
"""Safety valve for a marker that was wired but never arrived — a hook that
failed to run, a notify handler that died. NOT turn pacing: real turns go
quiet for seconds at a time (a long tool call appends nothing between the
tool_use line and the tool_result line), so anything near the marker-less 2s
would fire the flip 58s into a 60s `npm test` and kill the harness mid-turn.
When the marker is wired it is the trigger; this is the last resort."""


def _quiesce_default(marker_wired: bool) -> float:
    return _MISSED_MARKER_VALVE_S if marker_wired else _QUIESCE_S


def wait_until_safe(
    transcript: Path | None,
    sentinel: Path | None,
    cancelled: Callable[[], bool],
    quiesce: float | None = None,
    poll: float = 0.2,
    marker_wired: bool = False,
) -> bool:
    """Block until the turn boundary. The transcript's last append lands
    before the Stop hook / notify touches the sentinel, so idle means the
    sentinel is at least as new as the transcript. Returns False if
    `cancelled()` turned true first.

    Two modes, because transcript quiescence means different things:

    - `marker_wired=True` (the harness was launched with the turn-complete
      hook — the common case): the marker touch is the only normal trigger.
      Quiescence stays wired as a 30s valve for a marker that never arrives,
      never as a turn-pacing signal.
    - `marker_wired=False` (no hook — e.g. codex with a user-configured
      notify handler tandem refuses to clobber): 2s of transcript quiescence
      is the fallback boundary, because nothing better exists.

    `quiesce=None` takes the mode's default, so the mode alone is enough to
    be safe; pass a number to override (tests scale it down).

    Both clocks here are wall-clock on purpose: the deadline is derived from
    file mtimes, so `time.time()` is the only comparable reading (monotonic
    would be right for a pure timeout, but there is none in this loop).
    A missing file reads as mtime 0, which makes a fresh session (no files
    yet) idle in either mode, and leaves a marker-less session on the
    quiescence path for as long as it runs."""
    if quiesce is None:
        quiesce = _quiesce_default(marker_wired)
    while True:
        if cancelled():
            return False
        t, s = _mtime(transcript), _mtime(sentinel)
        if s >= t:
            return True
        if time.time() - t >= quiesce:
            return True
        time.sleep(poll)


class FlipMonitor:
    """Owns the flip lifecycle: the armed flag (toggle to cancel), the
    turn-boundary wait, and the termination ladder through the PtyControl.
    One background thread; all public methods are thread-safe.

    `marker_wired` says whether this launch actually got a turn-complete
    hook, which decides how the wait reads transcript quiescence (see
    `wait_until_safe`). The caller derives it from the adapter: the argv the
    runner already appended, `adapter.hook_argv_extra(sentinel)`, is empty
    exactly when no hook was wired (codex declining to clobber a
    user-configured notify), so `marker_wired=bool(hook_extra)` — reusing the
    list that went into argv rather than calling the adapter a second time,
    since a second call re-reads config and could disagree with what was
    actually launched."""

    def __init__(self, control, quit_bytes: list[bytes],
                 transcript: Path | None, sentinel: Path,
                 marker_wired: bool = False,
                 quiesce: float | None = None, poll: float = 0.2):
        self.control = control
        self.quit_bytes = quit_bytes
        self.transcript = transcript
        self.sentinel = sentinel
        self.marker_wired = marker_wired
        self.quiesce = _quiesce_default(marker_wired) if quiesce is None else quiesce
        self.poll = poll
        self.flip_requested = False
        self.how = ""
        self._armed = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="tandem-flip", daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._armed.set()  # unblock the wait
        # 15s outlasts the worst-case ladder (attach wait 5s + soft
        # keystrokes + soft/term/kill timeouts ~6.75s), so a stop() landing
        # mid-ladder still joins instead of abandoning a live thread.
        self._thread.join(timeout=15)

    def flip_pressed(self) -> None:
        """Arm, or toggle off a pending flip. Called from the pty pump's
        stdin branch, so it must not block: Event.set/clear take only the
        Event's own lock and never do I/O. It must also stay off any lock
        that `armed()` needs — `armed()` runs inside the SIGWINCH handler on
        this same thread, and a shared lock would deadlock the pump."""
        if self._armed.is_set():
            self._armed.clear()  # toggle: cancel a pending flip
        else:
            self._armed.set()

    def armed(self) -> bool:
        """Bar state: armed and still pending. Reached from inside the pump's
        SIGWINCH handler (on_winch -> paint -> armed), so it is deliberately
        lock-free, allocation-free and non-raising: Event.is_set() is a bare
        flag read and `flip_requested` a plain attribute. Never add a lock,
        a stat/read, or a raise path here. Goes False once the flip actually
        fires, so the bar stops advertising an arm that is already spent."""
        return self._armed.is_set() and not self.flip_requested

    def _run(self) -> None:
        while not self._stop.is_set():
            self._armed.wait()
            if self._stop.is_set():
                return
            ok = wait_until_safe(
                self.transcript,
                self.sentinel,
                cancelled=lambda: (
                    not self._armed.is_set() or self._stop.is_set()
                ),
                quiesce=self.quiesce,
                poll=self.poll,
                marker_wired=self.marker_wired,
            )
            if self._stop.is_set():
                return
            if not ok:
                continue  # cancelled: back to waiting for the next arm
            self.flip_requested = True
            self.how = self.control.terminate(self.quit_bytes)
            return


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
        # set here too so the attribute exists even if run() raises early
        self.flip_requested = False

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
        argv += load_harness_args(active)
        # bound, not re-called: hook_argv_extra re-reads config per call
        # (codex checks the user's config.toml for a notify handler), so a
        # second call could disagree with the argv actually launched.
        hook_extra = adapter.hook_argv_extra(sentinel)
        argv += hook_extra

        frame_cfg = load_frame_config()
        control = PtyControl()
        monitor = FlipMonitor(
            control, adapter.quit_keystrokes(), transcript, sentinel,
            marker_wired=bool(hook_extra),
        )
        frame = FrameIO(
            flip_byte=frame_cfg.flip_byte,
            on_flip=monitor.flip_pressed,
            armed=monitor.armed,
            bar=frame_cfg.bar,
            active=active,
            other=session.shadow,
        )
        self.flip_requested = False

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
        monitor.start()   # before the try: stop() on an unstarted thread raises
        try:
            code = run_in_pty(argv, cwd=session.cwd, frame=frame, control=control)
        finally:
            stop.set()
            # stop() first, and only then read the monitor: `flip_requested`
            # and `how` are assigned as the ladder finishes, so a read before
            # the join races the flip thread.
            monitor.stop()
            thread.join(timeout=10)
            sentinel.unlink(missing_ok=True)
            self.flip_requested = monitor.flip_requested
        if frame.bar_dropped:
            marker = paths.tandem_home() / "tmp" / f"{session.tandem_id}-bar-dropped"
            try:
                marker.parent.mkdir(parents=True, exist_ok=True)
                marker.touch()
            except OSError:
                pass  # doctor loses one hint; the session's exit code is not
                      # negotiable, and the note below still reaches the user
            errors.append(
                "status bar disabled for this session (terminal conflict);"
                " set [frame] bar = false to silence"
            )
        for err in errors:
            print(f"tandem: sync error: {err}")
        return code
