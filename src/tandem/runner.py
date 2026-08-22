"""Session runner: PTY passthrough of the active harness plus the tail loop.

The tail loop runs on a background thread while the user works in the native
CLI on the foreground thread. Each new transcript line is parsed into
normalized events and handed to a sink (M2: an event logger; M3 swaps in the
sync engine). The durable cursor advances only after the sink has handled a
line, so a crash resumes from the last confirmed entry.
"""

from __future__ import annotations

import io
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Protocol

from . import paths, plugin_setup, routefile
from .config import load_frame_config
from .events import SessionContext
from .harness import get_adapter
from .ptyrun import FrameIO, PtyControl, _winsize, run_in_pty
from .ratelimit import RateLimitPoller
from .routing import RouteCoordinator
from .state import PairedSession, StateStore, SyncCursor
from .tabs import TabState
from .tailer import TailedLine, TranscriptTruncated, TranscriptWatcher
from .util import json_line
from .warm import WarmChild, _shadow_size, build_launch, spawn_hidden


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


def ctx_from_cursor(session: PairedSession, cursor: SyncCursor) -> SessionContext:
    pending = dict(cursor.pending)
    state = pending.pop("harness_state", {})
    # legacy key from the pre-namespace cursor layout; fold it in
    leaf = pending.pop("claude_leaf_uuid", None)
    if leaf is not None:
        state.setdefault("claude", {})["leaf_uuid"] = leaf
    calls = pending.pop("pending_calls", {})
    return SessionContext(
        tandem_id=session.tandem_id,
        cwd=session.cwd,
        direction=f"{cursor.source}->{cursor.target}",
        turn_index=cursor.turn_index,
        pending_calls=calls,
        harness_state=state,
        source_session_id=session.native_id(cursor.source),
        target_session_id=session.native_id(cursor.target),
    )


def ctx_to_cursor(ctx: SessionContext, cursor: SyncCursor) -> None:
    cursor.turn_index = ctx.turn_index
    # update, not replace: the sync engine keeps its own keys (intent,
    # last_placeholder_turn) in the same dict
    cursor.pending.update(
        {
            "pending_calls": ctx.pending_calls,
            "harness_state": ctx.harness_state,
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
_MISSED_MARKER_VALVE_S = 120.0
"""Safety valve for a marker that was wired but never arrived — a hook that
failed to run, a notify handler that died. NOT turn pacing: real turns go
quiet for minutes at a time (a long tool call appends nothing between the
tool_use line and the tool_result line — a full test suite, a slow install,
a `sleep 90`), and firing there kills the harness mid-turn.

The two error costs are wildly asymmetric, which is what sets the number.
Firing too early costs a live turn: the harness is terminated between the
tool call and its result, and that work is gone. Firing too late costs
nothing but a wait, and only in the case this valve exists for at all — a
dead hook — where a second Ctrl-] cancels the pending flip and the user can
quit the harness by hand. So the valve sits far above any plausible
tool-call silence rather than close to it. When the marker is wired it is
the trigger; this is the last resort."""


def _quiesce_default(marker_wired: bool) -> float:
    return _MISSED_MARKER_VALVE_S if marker_wired else _QUIESCE_S


def _key_label(byte: int) -> str:
    """How the bar spells the flip keybind. `config._parse_flip_key` only ever
    yields 0x01-0x1F, so the caret form always applies; the hex fallback is
    there so a future non-control binding still prints something."""
    if 0x00 < byte < 0x20:
        return "^" + chr(byte + 0x40)
    return f"0x{byte:02x}"


def _stdin_tty() -> bool:
    """Is stdin a real terminal? The one probe the warm gate reads (ptyrun
    keeps its own for the pump). A module-level function so tests can flip
    it: under pytest stdin is never a tty, which would otherwise pin the
    gate closed and make the config half of it untestable."""
    try:
        return os.isatty(sys.stdin.fileno())
    except (ValueError, OSError, io.UnsupportedOperation):
        return False


def _flip_debug(msg: str) -> None:
    """Timestamped line into ~/.tandem/logs/flip-debug.log. Temporary
    diagnostic for the live flip-latency hunt: never raises, and never
    called from the pump thread (flip_pressed records to memory instead;
    the monitor thread writes on its behalf)."""
    try:
        path = paths.log_dir() / "flip-debug.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as fh:
            fh.write(f"{time.time():.3f} pid={os.getpid()} {msg}\n")
    except Exception:
        pass


def wait_until_safe(
    transcript: Path | None,
    sentinel: Path | None,
    cancelled: Callable[[], bool],
    quiesce: float | None = None,
    poll: float = 0.2,
    marker_wired: bool = False,
    provider: Callable[[], Path | None] | None = None,
    status_probe: Callable[[], str | None] | None = None,
) -> bool:
    """Block until the turn boundary. The transcript's last append lands
    before the Stop hook / notify touches the sentinel, so idle means the
    sentinel is at least as new as the transcript. Returns False if
    `cancelled()` turned true first.

    Two modes, because transcript quiescence means different things:

    - `marker_wired=True` (the harness was launched with the turn-complete
      hook — the common case): the marker touch is the only normal trigger.
      Quiescence stays wired as a valve for a marker that never arrives,
      never as a turn-pacing signal.
    - `marker_wired=False` (no hook — e.g. codex with a user-configured
      notify handler tandem refuses to clobber): 2s of transcript quiescence
      is the fallback boundary, because nothing better exists.

    `quiesce=None` takes the mode's default, so the mode alone is enough to
    be safe; pass a number to override (tests scale it down).

    The transcript is re-read every poll through `provider`, because the path
    is not always known when the wait starts: codex mints its own session id
    and the runner's tail thread discovers the rollout seconds later. The
    default provider returns the `transcript` argument forever, which is the
    standalone (already-known path) contract this function shipped with.

    An unknown transcript is *not* evidence of idleness. `_mtime(None)` is
    0.0, so the plain `s >= t` test would call every unknown-transcript
    session idle and fire a flip in the middle of a turn. With the marker
    wired there is a better answer: nothing is known, so hold and let the
    valve decide — anchored to the moment the wait began rather than to a
    file mtime that does not exist. (Marker-less sessions keep the old
    reading: quiescence is all they have, and their transcript is always
    known — only codex's fresh-mint path arrives here with None, and that
    path always wires the marker or falls back to the 2s quiescence over a
    real file once discovery lands.)

    Both clocks here are wall-clock on purpose: the deadline is derived from
    file mtimes, so `time.time()` is the only comparable reading (monotonic
    would be right for a pure timeout, but there is none in this loop).

    With `status_probe` (claude sessions), the probe is the entire
    boundary test and the marker/quiescence rules above never run: the
    probe reads claude's own session registry, which distinguishes a
    running turn ("busy") from an idle prompt ("waiting") directly.
    Anything but "busy" — including no answer at all — flips
    immediately: single-tier by spec, eager on registry schema drift.
    The valve is deliberately absent here; it existed to cap a marker
    that never arrives, and it killed genuinely long turns. The
    transcript-noise problem this solves: modern claude appends
    housekeeping (away_summary, last-prompt) minutes after Stop and
    bumps the transcript mtime on resume, so sentinel >= transcript is
    false while the session sits idle at its prompt."""
    if quiesce is None:
        quiesce = _quiesce_default(marker_wired)
    if provider is None:
        provider = lambda: transcript  # noqa: E731 - back-compat default
    armed_at = time.time()
    while True:
        if cancelled():
            return False
        if status_probe is not None:
            if status_probe() == "busy":
                time.sleep(poll)
                continue
            return True
        path = provider()
        if path is None and marker_wired:
            if time.time() - armed_at >= quiesce:
                return True   # valve, from arm time: no mtime to anchor to
            time.sleep(poll)
            continue
        t, s = _mtime(path), _mtime(sentinel)
        if s >= t:
            return True
        if time.time() - t >= quiesce:
            return True
        time.sleep(poll)


class FlipMonitor:
    """Owns the flip lifecycle: the armed flag (toggle to cancel), the
    turn-boundary wait, and the termination ladder through the PtyControl.
    One background thread; all public methods are thread-safe.

    `transcript` is a live attribute, not a constructor snapshot: codex mints
    its own session id, so the runner's tail thread discovers the rollout
    after launch and publishes it here. The wait loop re-reads it every poll
    through a provider closure — a plain attribute read/write, which the GIL
    makes atomic, so no lock is needed (and none may be added: `armed()` runs
    inside the SIGWINCH handler on the pump thread).

    `marker_wired` says whether this launch actually got a turn-complete
    hook, which decides how the wait reads transcript quiescence (see
    `wait_until_safe`). The caller derives it from the adapter: the argv the
    runner already appended, `adapter.hook_argv_extra(sentinel)`, is empty
    exactly when no hook was wired (codex declining to clobber a
    user-configured notify), so `marker_wired=bool(hook_extra)` — reusing the
    list that went into argv rather than calling the adapter a second time,
    since a second call re-reads config and could disagree with what was
    actually launched.

    `status_probe` (claude only) reads the harness's own session
    registry and, when present, replaces the transcript/sentinel rules
    entirely — see `wait_until_safe`.

    `on_flip_decided` fires once, on this thread, in the gap between the
    decision and the ladder — the runner starts the incoming harness's launch
    worker there, so its boot overlaps the outgoing one's teardown. It is
    called with the flag already set (`flip_requested` is what makes the flip
    inevitable, and a callback must not be able to take it back) and its
    exceptions are swallowed: a failed hook costs a cold flip, never the flip
    itself.

    `on_wait_cancelled` is the other side of that coin: it fires on this
    thread when an armed wait was toggled off instead of released, so
    whatever the arm was standing for can be undone (the runner clears the
    tab state's pending move and any routed prompt riding on it). Assigned
    like `on_flip_decided`, called inside try/except-pass for the same
    reason — a raising listener must not cost the thread. A stopping monitor
    is not a cancel: `stop()` returns above this, so shutdown never fires
    it."""

    def __init__(self, control, quit_bytes: list[bytes],
                 transcript: Path | None, sentinel: Path,
                 marker_wired: bool = False,
                 quiesce: float | None = None, poll: float = 0.2,
                 status_probe: Callable[[], str | None] | None = None,
                 on_flip_decided: Callable[[], None] | None = None):
        self.control = control
        self.quit_bytes = quit_bytes
        self.transcript = transcript
        self.sentinel = sentinel
        self.marker_wired = marker_wired
        self.quiesce = _quiesce_default(marker_wired) if quiesce is None else quiesce
        self.poll = poll
        self.status_probe = status_probe
        self.on_flip_decided = on_flip_decided
        # assigned by the runner after construction, before start()
        self.on_wait_cancelled: Callable[[], None] | None = None
        self.flip_requested = False
        self.how = ""
        self._key_events: list[tuple[float, str]] = []
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
        # 15s outlasts the worst-case termination ladder (attach wait 5s +
        # soft keystrokes + soft/term/kill timeouts ~6.75s), so stop() landing
        # mid-flip normally joins instead of abandoning the monitor. The warm
        # launch worker is deliberately outside this join: the fired-slot lock
        # makes that safe by killing a child that lands after the caller has
        # closed and read the slot (see `InteractiveRunner._run`).
        self._thread.join(timeout=15)

    def flip_pressed(self) -> None:
        """Arm, or toggle off a pending flip. Called from the pty pump's
        stdin branch, so it must not block: Event.set/clear take only the
        Event's own lock and never do I/O. It must also stay off any lock
        that `armed()` needs — `armed()` runs inside the SIGWINCH handler on
        this same thread, and a shared lock would deadlock the pump."""
        if self._armed.is_set():
            # memory only — this thread must never do I/O (see docstring);
            # the monitor thread flushes these to the debug log
            self._key_events.append((time.time(), "cancel"))
            self._armed.clear()  # toggle: cancel a pending flip
        else:
            self._key_events.append((time.time(), "arm"))
            self._armed.set()

    def armed(self) -> bool:
        """Bar state: armed and still pending. Reached from inside the pump's
        SIGWINCH handler (on_winch -> paint -> armed), so it is deliberately
        lock-free, allocation-free and non-raising: Event.is_set() is a bare
        flag read and `flip_requested` a plain attribute. Never add a lock,
        a stat/read, or a raise path here. Goes False once the flip actually
        fires, so the bar stops advertising an arm that is already spent."""
        return self._armed.is_set() and not self.flip_requested

    def _flush_key_events(self) -> None:
        while self._key_events:
            ts, ev = self._key_events.pop(0)
            _flip_debug(f"key {ev} pressed_at={ts:.3f}")

    def _run(self) -> None:
        while not self._stop.is_set():
            self._armed.wait()
            if self._stop.is_set():
                return
            self._flush_key_events()
            _flip_debug("wait-start")
            ok = wait_until_safe(
                self.transcript,
                self.sentinel,
                cancelled=lambda: (
                    not self._armed.is_set() or self._stop.is_set()
                ),
                quiesce=self.quiesce,
                poll=self.poll,
                marker_wired=self.marker_wired,
                provider=lambda: self.transcript,
                status_probe=self.status_probe,
            )
            if self._stop.is_set():
                return
            self._flush_key_events()
            if not ok:
                _flip_debug("wait-cancelled")
                if self.on_wait_cancelled is not None:
                    try:
                        self.on_wait_cancelled()
                    except Exception:
                        pass   # a raising listener must not kill the thread
                continue  # cancelled: back to waiting for the next arm
            _flip_debug("wait-released ladder-start")
            self.flip_requested = True
            if self.on_flip_decided is not None:
                try:
                    self.on_flip_decided()
                except Exception:
                    pass   # a failed fire means a cold flip, never a dead thread
            self.how = self.control.terminate(self.quit_bytes)
            _flip_debug(f"ladder-done how={self.how}")
            return


class TailLoop:
    """Tails one source transcript, parses, feeds the sink, persists the
    cursor. Usable standalone in tests (no PTY required)."""

    def __init__(
        self,
        store: StateStore,
        session: PairedSession,
        source: str,
        target: str,
        transcript: Path,
        sink: EventSink,
    ):
        self.store = store
        self.session = session
        self.source = source
        self.target = target
        self.sink = sink
        self.transcript = transcript
        self.cursor = store.get_cursor(session.tandem_id, source, target)
        self.ctx = ctx_from_cursor(session, self.cursor)
        self.reader = get_adapter(source).make_source_reader(
            session, self.cursor, transcript
        )
        self.errors: list[str] = []

    def drain(self) -> int:
        """Process everything new; returns number of units consumed."""
        from .harness.base import ShadowBusy

        try:
            lines = self.reader.poll()
        except TranscriptTruncated as exc:
            self.errors.append(str(exc))
            return 0
        consumed = 0
        for line in lines:
            try:
                self.sink.handle(line, self.ctx, self.cursor)
            except ShadowBusy:
                # transactional append rolled back; rebuild context from the
                # durable cursor and let the next wake-up retry the unit
                self.cursor = self.store.get_cursor(
                    self.session.tandem_id, self.source, self.target)
                self.ctx = ctx_from_cursor(self.session, self.cursor)
                self.reader = get_adapter(self.source).make_source_reader(
                    self.session, self.cursor, self.transcript)
                break
            line.advance(self.cursor)
            ctx_to_cursor(self.ctx, self.cursor)
            consumed += 1
        if consumed:
            self.store.save_cursor(self.cursor)
            self.store.touch_sync(self.session.tandem_id)
        return consumed


class UsageFeed:
    """Cosmetic tap on the active transcript for the bar's token stats: its
    own reader over a throwaway in-memory cursor, so it works uniformly for
    JSONL and DB-unit sources, never touches durable sync state, and cannot
    double-count off `ShadowBusy` re-polls. Any failure goes quiet and stays
    quiet — stats must never cost the user their sync."""

    def __init__(self, adapter, session, transcript: Path, state: dict):
        self.state = state
        self.meter = adapter.make_usage_meter()
        self._cursor = SyncCursor(
            tandem_id=session.tandem_id, source=adapter.id, target="__usage__"
        )
        try:
            self._reader = (
                adapter.make_source_reader(session, self._cursor, transcript)
                if self.meter is not None
                else None
            )
        except Exception:
            self._reader = None

    def poll(self) -> None:
        if self._reader is None:
            return
        try:
            for line in self._reader.poll():
                if line.raw is not None:
                    self.meter.feed(line.raw)
                line.advance(self._cursor)
            self.state["text"] = self.meter.snapshot().bar_text()
        except Exception:
            self._reader = None


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


SinkFactory = Callable[[StateStore, PairedSession, str, str], EventSink]


class InteractiveRunner:
    """Runs the active harness in PTY passthrough with the tail loop on a
    background thread."""

    def __init__(self, session: PairedSession, sink_factory: SinkFactory,
                 adopt_child: WarmChild | None = None,
                 tabs: TabState | None = None,
                 inject: routefile.RouteRequest | None = None):
        self.session = session
        self.sink_factory = sink_factory
        # A standby warmed by the *previous* run, handed over by the flip
        # loop: adopted when it is alive and was warmed for the side this
        # run is launching.
        self.adopt_child = adopt_child
        # The tab cycle, owned by the flip loop and carried across runs. None
        # is the pre-mixed frame: no mixer thread, no frame file, Ctrl-] goes
        # straight to the monitor — every collaborator below reads `is None`
        # rather than branching on a mode flag.
        self.tabs = tabs
        # A routed prompt this run is expected to deliver into its own
        # harness (the flip loop carries it over from the run that routed it).
        self.inject = inject
        # set here too so the attributes exist even if run() raises early
        self._released = False
        self.flip_requested = False
        # This run's routed-turn lifecycle, built by `_run` once the launch
        # it belongs to is known. Everything about routing lives on it; the
        # two properties below are what the flip loop reads back.
        self.coordinator: RouteCoordinator | None = None
        # The harness this run fired at its flip decision, surrendered to the
        # flip loop's carry. Only a flip can fill it: no flip, no fire.
        self.warm_child: WarmChild | None = None
        # Formatted, ready-to-print report lines for the session that just
        # ran. `run()` prints them itself on a normal exit; on a flip it does
        # not, because the flip's screen clear would wipe them a moment
        # later — the flip loop reprints them onto the fresh screen instead.
        self.reports: list[str] = []

    @property
    def route_request(self) -> routefile.RouteRequest | None:
        """Where the mixer took this run — the flip loop reads it after
        `run()` to learn where to go and what to carry."""
        c = self.coordinator
        return c.route_request if c is not None else None

    @property
    def inject_failed(self) -> bool:
        c = self.coordinator
        return c.inject_failed if c is not None else False

    def run(self) -> int:
        """Run the harness, and own the adoptee's disposal while doing it.

        Between the moment adoption is decided and the handover to
        `run_in_pty`, this run is the *only* thing holding the hidden child:
        the flip loop popped it out of its carry before constructing us, and
        only ever gets `warm_child` back. So anything that raises in between
        — a malformed config, a thread that will not start — has to reap it
        here, or a detached harness outlives the session with nobody left to
        kill it. `_released` marks the handover: past it the child belongs to
        the pty (or was already killed for refusing to release), and killing
        it again would terminate the harness the user is looking at.

        An adoptee we decline to adopt (warmed for the other side, or
        already dead) is reaped by the same guard: nothing downstream ever
        looks at it again, so dropping it silently would strand whatever
        part of it is still alive."""
        adopting = (
            self.adopt_child is not None
            and self.adopt_child.recipe.side == self.session.active
            and self.adopt_child.alive()
        )
        self._released = False
        try:
            return self._run(adopting)
        finally:
            # `_released` is only ever set on the adopting path, so this
            # covers both: a handover that never happened, and an adoptee
            # that was never adoptable in the first place.
            if self.adopt_child is not None and not self._released:
                try:
                    self.adopt_child.kill()
                except Exception:
                    pass   # never mask the failure that got us here

    def _run(self, adopting: bool) -> int:
        session = self.session
        active = session.active
        adapter = get_adapter(active)
        active_sid = session.native_id(active)
        # One recipe, bound once: hook_argv_extra re-reads config per call
        # (codex checks the user's config.toml for a notify handler), so the
        # argv actually launched and marker_wired must come from the same
        # snapshot — which is also what lets a warm standby be adopted with
        # the exact recipe it was spawned under. Bound at spawn time for an
        # adopted child (the recipe it actually booted under), at launch time
        # otherwise: rebuilding for an adopted child could disagree with what
        # is already running.
        #
        # A routed turn's model is pinned here and nowhere else: the pin is
        # argv, so the only moment it can be applied is the launch. Only for
        # the harness the route actually named — a ladder that landed
        # somewhere else must launch that harness the way it would have
        # launched anyway (the injector keeps the prompt instead of typing
        # it in).
        recipe = (self.adopt_child.recipe if adopting
                  else build_launch(session, active,
                                    model=(self.inject.model
                                           if self.inject is not None
                                           and self.inject.target == active
                                           else "")))
        _flip_debug(f"run-start side={active} adopting={adopting}")
        transcript = recipe.transcript
        sentinel = recipe.sentinel
        argv = recipe.argv
        hook_extra = recipe.hook_extra

        frame_cfg = load_frame_config()
        control = PtyControl()
        probe_last: dict = {"status": "<unpolled>"}

        def status_probe_fn() -> str | None:
            # A raising probe would kill the tandem-flip thread and leave
            # the bar advertising an armed flip that can never fire (os.kill
            # raises OverflowError — not OSError — on a pid outside C-long
            # range, so registry garbage can escape session_status's own
            # guards). Single tier reads any non-answer as flippable, so an
            # escape maps to None, never to a dead thread.
            try:
                status = adapter.session_status(active_sid)
            except Exception:
                status = None
            if status != probe_last["status"]:
                _flip_debug(f"probe {probe_last['status']} -> {status}")
                probe_last["status"] = status
            return status

        monitor = FlipMonitor(
            control, adapter.quit_keystrokes(), transcript, sentinel,
            marker_wired=bool(hook_extra),
            # capability check: adapters without a live status registry/probe
            # opt out by absence — a probe answering None would flip eagerly
            # mid-turn.
            status_probe=status_probe_fn if hasattr(adapter, "session_status") else None,
        )
        # Fired at most once, on the monitor thread, between the flip
        # decision and the ladder. It does only the in-memory eligibility
        # checks there and starts a worker for everything else — the shadow
        # stat, the recipe build (config.toml reads, the sentinel mkdir) and
        # the fork/exec are all unbounded filesystem work, and any of it on
        # the monitor thread would hold up teardown of the outgoing harness:
        # the ladder starts only when this returns.
        # The finally below closes the handoff slot after the ladder: a child
        # that has landed by then is adopted, while a worker that lands later
        # sees the closed slot and kills its child instead of stranding it.
        # The non-tty path cannot adopt a fired child, so a spawn there could
        # only leak a hidden harness: the gate is read at fire time, and takes
        # both the [frame] `warm` flag and a real stdin tty.
        fired: dict = {"child": None, "closed": False}
        fired_lock = threading.Lock()

        def fire_warm() -> None:
            if self.route_request is not None:
                return   # routed flips spawn cold in v1: the standby would
                         # be for the wrong side or the wrong model
            if not (frame_cfg.warm and _stdin_tty()):
                return
            shadow = session.next_active(session.active)
            if shadow == "opencode":
                # v1 carve-out (spec: Frame and flip): an opencode TUI booted
                # before the final drain would cache the session pre-drain and
                # never show the last turn. Opencode-bound flips run cold.
                return

            def spawn() -> None:
                try:
                    size = _shadow_size(session, shadow)
                    if size is None:
                        return   # no shadow file yet: switch_session's
                                 # late-create + a cold spawn own that flip;
                                 # never fresh-mint
                    # not `recipe`: that name is taken by the *active* side's
                    # launch, which this closure must never rebuild or shadow
                    shadow_recipe = build_launch(session, shadow)
                    dims = _winsize(sys.stdin.fileno())
                    child = spawn_hidden(shadow_recipe, dims, size)
                except Exception:
                    return   # a failed fire means a cold flip
                _flip_debug(
                    f"warm-spawned side={shadow}"
                    f" child={getattr(getattr(child, 'child', None), 'pid', '?')}"
                    f" shadow_size={size}"
                )
                with fired_lock:
                    if not fired["closed"]:
                        fired["child"] = child
                        return
                try:
                    child.kill()   # the runner already read the slot and left
                except Exception:
                    pass

            threading.Thread(
                target=spawn, name="tandem-warm-fire", daemon=True
            ).start()

        # The monitor exists before the closure does, so the hook is wired by
        # assignment — `monitor.start()` is called far below, which is what
        # makes a plain write safe: the thread has not run yet.
        monitor.on_flip_decided = fire_warm
        # written by the tail thread, read by the pump on every paint/tick;
        # a plain dict-slot assignment is the entire synchronization (GIL),
        # same as monitor.transcript
        usage_state = {"text": "", "limits": {}}
        # Account rate limits for every slot, polled on their own thread
        # (network calls must never sit on the tail thread's sync path).
        # Only with a bar to paint them on — no bar, no calls — and the pump
        # is the one that knows: it starts the poll when it draws the bar
        # and halts it when the bar is never drawn or drops.
        poller = (
            RateLimitPoller([active, *session.targets_for(active)], usage_state)
            if frame_cfg.bar and frame_cfg.rate_limits
            else None
        )

        def on_bar(drawn: bool) -> None:
            if poller is None:
                return
            if drawn:
                poller.ensure_started()
            else:
                poller.halt()

        # One reading for the whole run: whether a prompt typed into this
        # harness can be routed at all. The bar says so through `mode` and
        # the frame file says so to the hook, and they must never disagree —
        # so it is read once here, not re-derived per caller. Both halves
        # must hold: the CLI needs a prompt hook, and tandem's plugin has to
        # be registered there to use it. A run started before `tandem plugin
        # install` therefore shows `(no @-routing)` instead of silently
        # eating `@codex …` prefixes as literal prompt text. Reading it once
        # also means installing mid-run does not change the answer under the
        # user — the hook only reaches new sessions anyway.
        routing_ok = (adapter.prompt_hook_capable
                      and plugin_setup.hook_available(active))
        tabs = self.tabs

        def on_flip() -> None:
            # pump thread: TabState.press is allocation-light and I/O-free
            # by contract; bar moves repaint via mode() on the next tick and
            # the mixer thread persists the frame file
            move = tabs.press(active)
            if move.kind == "bar":
                return
            monitor.flip_pressed()   # arms a flip, or toggles off a pending
                                     # one (move.kind == "cancel")

        frame = FrameIO(
            flip_byte=frame_cfg.flip_byte,
            on_flip=on_flip if tabs is not None else monitor.flip_pressed,
            mode=(lambda: tabs.snapshot(active, routing_ok=routing_ok))
                 if tabs is not None else None,
            armed=monitor.armed,
            bar=frame_cfg.bar,
            active=active,
            others=session.targets_for(active),
            key_label=_key_label(frame_cfg.flip_byte),
            usage=lambda: usage_state["text"],
            limits=(lambda: usage_state["limits"]) if poller is not None else None,
            on_bar=on_bar if poller is not None else None,
        )
        self.flip_requested = False
        self.warm_child = None
        self.reports = []

        stop = threading.Event()
        # the mixer's own stop, set first in the finally: it must stop
        # picking routes up before the monitor is torn down under it
        mixer_stop = threading.Event()
        spawn_time = time.time()
        errors: list[str] = []
        # Two lists, one reporting spot: `errors` are sync failures (the
        # "sync error" prefix is load-bearing — it tells the user their
        # transcripts diverged), `notes` are everything else tandem wants to
        # mention on the way out. A note printed as a sync error sends people
        # hunting a failure that never happened.
        notes: list[str] = []
        if (self.inject is not None and self.inject.model
                and self.inject.target == active and not recipe.model):
            # `recipe.model` is what was launched, never what was asked for:
            # an adapter with no launch-time model flag returns no argv and
            # the pin silently evaporates. Say so — the turn is about to run
            # on a model the user did not pick.
            notes.append(f"{active} cannot pin a model at launch —"
                         " running its default")

        route = self.coordinator = RouteCoordinator(
            session, tabs, active, active_sid, adapter, routing_ok,
            self.inject, notes)
        if tabs is not None:
            # plain assignment, like on_flip_decided: the monitor thread has
            # not started yet (monitor.start() is below)
            monitor.on_wait_cancelled = route.cancelled

        def mixer_thread() -> None:
            route.sweep_leftovers()
            while not mixer_stop.is_set():
                route.tick(monitor)
                mixer_stop.wait(0.25)

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
                            # Publish to the flip monitor: until this lands it
                            # has no transcript to judge a turn boundary by,
                            # and holds any armed flip behind its valve. A bare
                            # attribute write is all the synchronization there
                            # is or should be — the GIL makes it atomic and the
                            # monitor's wait re-reads it every poll.
                            monitor.transcript = path
                            break
                    if path is None:
                        return
                ufeed = UsageFeed(get_adapter(active), current, path, usage_state)
                loops: list[TailLoop] = []
                sinks: list[EventSink] = []
                try:
                    for tgt in current.targets_for(active):
                        sink = self.sink_factory(store, current, active, tgt)
                        sinks.append(sink)
                        loops.append(TailLoop(store, current, active, tgt, path, sink))
                except Exception as exc:
                    errors.append(f"sync disabled: {exc}")
                    for s in sinks:
                        s.close()
                    return
                watcher = TranscriptWatcher()
                for p in get_adapter(active).watch_paths(current, path):
                    watcher.watch(p)
                watcher.watch(sentinel)
                watcher.start()
                # `tandem sub --context full` drains these same cursor rows
                # from a separate process, holding `ops._sub_lock()` across
                # its drain-then-fork. Two concurrent drains of one cursor
                # translate the same lines twice — duplicate turns and call ids
                # in the shadow and in the fork — so the tail thread takes the
                # same flock. Kept tight around the drain itself: the wait
                # between iterations must not hold it. (Imported here: `ops`
                # imports this module.)
                from . import ops
                try:
                    while not stop.is_set():
                        with ops._sub_lock():
                            for loop in loops:
                                loop.drain()
                        before = usage_state["text"]
                        ufeed.poll()
                        if poller is not None and usage_state["text"] != before:
                            # a response just landed: the account figures
                            # moved, refresh them ahead of the interval
                            poller.poke()
                        watcher.wait()
                    # final drain after the CLI exits
                    with ops._sub_lock():
                        for loop in loops:
                            loop.drain()
                    for loop in loops:
                        errors.extend(loop.errors)
                finally:
                    watcher.stop()
                    for s in sinks:
                        s.close()

        thread = threading.Thread(target=tail_thread, name="tandem-tail", daemon=True)
        thread.start()
        if self.inject is not None:
            # daemon and never joined: a blocking pty write must not be able
            # to hold the session's exit (see `RouteCoordinator.deliver`)
            threading.Thread(target=route.deliver, name="tandem-inject",
                             args=(control, stop, monitor),
                             daemon=True).start()
        monitor.start()   # before the try: stop() on an unstarted thread raises
        mixer: threading.Thread | None = None
        if tabs is not None:
            mixer = threading.Thread(target=mixer_thread, name="tandem-mixer",
                                     daemon=True)
            mixer.start()
        try:
            # A release that returns None means the discard reader still owns
            # the fd, so there is nothing safe to hand over: run_in_pty spawns
            # cold on child=None, and the WarmChild must be killed here or the
            # hidden process outlives the run with nothing left to reap it.
            pre_spawned = self.adopt_child.release() if adopting else None
            if adopting:
                # The handover is settled from here on: either run_in_pty
                # takes the raw child, or the kill below reaps the WarmChild
                # that would not give it up. Either way `run`'s guard must
                # not dispose of it a second time.
                self._released = True
                if pre_spawned is None:
                    self.adopt_child.kill()
            code = run_in_pty(argv, cwd=session.cwd, frame=frame,
                              control=control, child=pre_spawned)
        finally:
            stop.set()
            # before monitor.stop(): no new routed arm may be taken against a
            # monitor that is on its way out
            mixer_stop.set()
            if poller is not None:
                poller.stop()
            # stop() first, and only then read the monitor: `flip_requested`
            # and `how` are assigned as the ladder finishes, so a read before
            # the join races the flip thread.
            monitor.stop()
            if mixer is not None:
                # The mixer must be *finished*, not merely told to stop:
                # a tick still in flight could arm state on a run that has
                # already exited — set route_request after the flip decision
                # is read below, refill tabs.pending, or rewrite the frame
                # file the next run is about to own. `mixer_stop.wait`
                # returns the moment the event is set, so this costs at most
                # the in-flight tick's file ops.
                mixer.join(timeout=2)
            thread.join(timeout=10)
            self.flip_requested = monitor.flip_requested
            # after flip_requested settles: only a flip keeps the child.
            with fired_lock:
                child, fired["child"] = fired["child"], None
                fired["closed"] = True
            if child is not None and self.flip_requested:
                self.warm_child = child
            elif child is not None:
                # a fire-spawn only exists because the flip fired, but the
                # guard costs nothing: never leak a hidden harness
                try:
                    child.kill()
                except Exception:
                    pass
            sentinel.unlink(missing_ok=True)
        if frame.bar_dropped:
            marker = paths.tandem_home() / "tmp" / f"{session.tandem_id}-bar-dropped"
            try:
                marker.parent.mkdir(parents=True, exist_ok=True)
                marker.touch()
            except OSError:
                pass  # doctor loses one hint; the session's exit code is not
                      # negotiable, and the note below still reaches the user
            notes.append(
                "status bar disabled for this session (terminal conflict);"
                " set [frame] bar = false to silence"
            )
        route.exit_notes()
        self.reports = [f"tandem: sync error: {err}" for err in errors]
        self.reports += [f"tandem: {note}" for note in notes]
        _flip_debug(
            f"run-exit side={active} code={code} flip={self.flip_requested}"
        )
        if not self.flip_requested:
            for line in self.reports:
                print(line)
        return code
