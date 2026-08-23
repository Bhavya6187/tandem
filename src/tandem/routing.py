"""One run's routed-turn lifecycle, on the frame's side of the route files.

The runner owns threads, the pty and the launch; everything a routed turn
needs between them lives here: the startup sweep, the frame file the hook
reads, taking a request and arming the flip for it, typing the prompt into
the harness it landed in, and the notes that make every failure visible.
Keeping it in one object is what makes the ownership legible — the hook
process creates the pending file and nothing else on this side touches
either file.

Threading, because the four entry points run on four different threads:
`tick` on `tandem-mixer`, `deliver` on the `tandem-inject` daemon,
`cancelled` on the monitor thread, and `sweep_leftovers`/`exit_notes` on
the mixer and the runner respectively. The state they share is two plain
attributes (`route_request`, `inject_failed`) plus `notes`; the GIL makes
each write atomic, no reader needs a consistent multi-field view, and the
runner joins the mixer before reading anything.
"""

from __future__ import annotations

import threading
import time

from . import routefile
from .state import PairedSession
from .tabs import MIXED, TabState


# Seconds of child-output silence after the first draw that count as "the
# composer is up"; TUIs repaint in bursts well under this while booting.
OUTPUT_QUIET_S = 1.0
# How long a listening TUI gets to echo a paste (redraw its composer). No
# output at all after a paste means the TUI was not taking input yet — the
# bytes went nowhere — so the paste is retried after the next quiet period.
ECHO_WAIT_S = 1.5
# Paste attempts per delivery; each one waits out a fresh quiet period.
PASTE_ATTEMPTS = 3


class RouteCoordinator:
    """The routed-turn state of one run: at most one request taken, at most
    one prompt to deliver."""

    def __init__(self, session: PairedSession, tabs: TabState | None,
                 active: str, active_sid: str | None, adapter,
                 routing_ok: bool,
                 inject: routefile.RouteRequest | None,
                 notes: list[str]):
        self.session = session
        # The tab cycle, or None for the pre-mixed frame: no frame file, no
        # pickup, and Ctrl-] goes straight to the monitor.
        self.tabs = tabs
        self.active = active
        self.active_sid = active_sid
        self.adapter = adapter
        # Whether a prompt typed into this harness can be routed at all —
        # published to the hook and painted on the bar, so it is read once
        # by the runner and never re-derived.
        self.routing_ok = routing_ok
        # A routed prompt this run is expected to deliver into its own
        # harness (the flip loop carried it over from the run that took it).
        self.inject = inject
        # The run's shared exit-report list; `list.append` is atomic and the
        # runner reads it only after the threads are joined.
        self.notes = notes
        # Filled by `pickup` when a request armed this run's flip; the flip
        # loop reads it after run() to learn where to go and what to carry.
        self.route_request: routefile.RouteRequest | None = None
        # The injector could not put `inject` into the harness; the exit
        # notes then tell the user the prompt was kept rather than lost.
        self.inject_failed = False
        self._published = -1

    def sweep_leftovers(self) -> None:
        """Clear both route files at startup, saying what they held.

        A leftover from an earlier run must not replay a stale prompt into
        this one, so the files go — but never silently. A claimed one never
        landed; a pending one was never even picked up, which a user can
        reach without crashing anything (route while a flip is already
        armed: the mixer refuses the claim, the flip proceeds, and this
        sweep is what eats the request). Either way it is a typed prompt
        about to be deleted, and the note is the only copy the user gets.

        Not while this run is the one delivering: that request is the
        injector's, and only the injector releases it."""
        if self.inject is not None:
            return
        pending, claimed = routefile.sweep(self.session.tandem_id)
        for left, what in ((pending, "never picked up"),
                           (claimed, "never delivered")):
            if left is not None:
                # a leftover the sweep could only read loosely may have no
                # target left to name; the prompt is the part that matters
                where = f" (target {left.target})" if left.target else ""
                self.notes.append(
                    f"a routed prompt was {what} and was kept: "
                    f"{left.prompt!r}{where}")

    def tick(self, monitor) -> None:
        """One mixer tick: republish the frame file the hook reads if the
        tabs moved, then try to pick a route up.

        The `tabs is None` gate lives here because this is where every
        tabs-touching path starts — the pre-mixed frame has no mixer thread
        to call this, and nothing below has to ask again.

        `tabs.version` is the whole change feed — every TabState mutation
        bumps it, so one integer compare keeps the hook's view in step
        without rewriting the file four times a second."""
        tabs = self.tabs
        if tabs is None:
            return
        if tabs.version != self._published:
            self._published = tabs.version
            routefile.write_frame_state(
                self.session.tandem_id,
                tabs.snapshot(self.active, routing_ok=self.routing_ok))
        self.pickup(monitor)

    def pickup(self, monitor) -> None:
        """Turn a pending route request into an armed flip toward its target.

        Two claims, in this order, and it is load-bearing. `tabs.routed` is
        a claim, not an assignment: a user press records its pending move on
        the pump thread and arms the monitor a beat later, so a tick landing
        inside that window sees an unarmed monitor over a slot that is
        already taken. Taking the request before the slot would burn it on a
        flip that goes somewhere else. On a refused claim nothing moves and
        the request stays pending for the next tick.

        Everything from the claim to `flip_pressed` is exposed the same way:
        the rename between them is not instant, and a press landing in there
        wins the monitor. `cancelled` is the recovery — it releases both the
        request and the file when the arm is toggled off."""
        tabs = self.tabs
        if tabs.tab != MIXED:
            return
        if self.route_request is not None:
            return               # this run already has its routed flip
        if monitor.armed() or monitor.flip_requested:
            return               # a flip is already in flight; not ours to take
        tandem_id = self.session.tandem_id
        req = routefile.read_pending(tandem_id)
        if req is None:
            return
        if req.target == self.active or \
                req.target not in self.session.participants:
            # nowhere to go: routing to the harness the user is already in is
            # the hook's job to prevent, and a target that left the session
            # cannot be launched. Drop it rather than arm a flip to nowhere.
            routefile.release(tandem_id, req.id)
        elif tabs.routed(req.target):
            if not routefile.claim(tandem_id, req.id):
                tabs.cancelled()   # a second prompt overwrote the slot in
                return             # between: give the tab move back
            # plain attribute assignment, GIL-atomic: read by fire_warm on the
            # monitor thread and by the flip loop after run() returns
            self.route_request = req
            monitor.flip_pressed()

    def deliver(self, control, stop: threading.Event, monitor) -> None:
        """Type this run's routed prompt into its harness once the harness is
        ready to take one, then release the request.

        Runs on the `tandem-inject` daemon thread. `PtyControl.write` flushes
        to the pty master, which is a BLOCKING write: a harness that stopped
        draining its input would park this thread for as long as it sulks.
        Daemon is the containment — a wedged write can cost the injection,
        never tandem's exit — and nothing else waits on this thread.

        Readiness has two shapes, and the split is claude-or-not rather than
        has-a-probe-or-not. Claude keeps a per-live-session registry whose
        idle answer ("waiting" on 2.1.226, "idle" on 2.1.241) really does
        mean an idle composer, so it is asked directly and the prompt goes in
        the moment it answers anything but "busy". Everyone else takes a
        fixed settle delay from spawn: opencode *has* a `session_status`,
        but it reads the transcript sqlite — an unknown session id and a
        resumed session's last row both answer "waiting" — which says
        nothing about whether the TUI has drawn and can take a paste.
        Trusting it would either fail every routed opencode turn (write
        before attach) or, worse, land the paste in a TUI that is not
        listening and then release the request, destroying the prompt. (The
        `hasattr(adapter, "session_status")` idiom stays right for the flip
        gate in the runner, where a stale "waiting" only means flipping a
        beat early — a safe answer there, not here.)

        Two liveness questions, not one. `stop` says the run is over, but it
        is set in the runner's finally — *after* `run_in_pty` returns, which
        is after the quit ladder has already killed the child. The ladder
        itself runs while the harness is still alive and draining input, so
        `stop` alone would let a paste land in a harness that is on its way
        out and then release the request believing it delivered. `monitor`
        answers the other half (`flip_requested` = the ladder is running or
        about to, `armed()` = a press is waiting on the turn boundary), and
        it is asked twice: once before the paste, and again before the
        release, since a flip can fire in between. It is passed in rather
        than held as state because it is the runner's local — one monitor
        per run, and an attribute would outlive the run it belongs to.

        Failure never destroys the prompt: the claimed file stays on disk and
        `inject_failed` turns it into an exit note, so the user can see what
        was meant to land and re-type it. The guards fail in that direction on
        purpose — the worst case is a delivered prompt that also earns a
        "kept" note, which costs a duplicate line, not a turn."""
        req = self.inject
        if req is None:
            return
        if req.target != self.active:
            self.inject_failed = True   # ladder landed elsewhere; keep
            return                      # the request for the user to see

        def flipping() -> bool:
            # plain reads of a flag and an Event, in the monitor's own
            # lock-free contract: never blocks this thread, never raises
            return bool(monitor.flip_requested) or bool(monitor.armed())

        deadline = time.time() + 30
        ready = False
        while time.time() < deadline and not stop.is_set():
            # claude only, by name: see the docstring — opencode's probe
            # answers "waiting" off transcript state and would be believed
            if self.active == "claude" and self.active_sid:
                try:
                    # Any positive non-busy answer is an idle composer:
                    # claude 2.1.226 spelled it "waiting", 2.1.241 spells it
                    # "idle" (live gate 2026-08-23). None means no live
                    # registry entry yet — still booting — so keep asking.
                    status = self.adapter.session_status(self.active_sid)
                    if status is not None and status != "busy":
                        ready = True
                        break
                except Exception:
                    pass    # a raising probe is not an answer; keep asking
                time.sleep(0.3)
            else:
                # No registry to ask (codex/opencode): the TUI is ready once
                # it has drawn something and then gone quiet. A fixed delay
                # was not enough — live gate 2026-08-23: codex took longer
                # to boot (MCP startup), a 2.5 s settle pasted into a TUI
                # that was not listening, the request was released as
                # delivered, and the prompt was gone. `control.last_output`
                # is stamped by the pump on every child read.
                last = control.last_output
                if last and time.monotonic() - last >= OUTPUT_QUIET_S:
                    ready = True
                    break
                time.sleep(0.3)
        if not ready or stop.is_set() or flipping():
            self.inject_failed = True
            return
        body = req.prompt.encode()
        # Bracketed paste (multi-line prompts must not submit per line), and
        # a write only counts once the TUI has echoed it. Live gate
        # 2026-08-23: opencode draws its frame and goes quiet while it is
        # still loading the session, and input in that window is discarded
        # — a paste that nothing echoes went nowhere, so it is safe (and
        # necessary) to wait for the next quiet period and paste again.
        paste = b"\x1b[200~" + body + b"\x1b[201~"
        delivered = False
        for _ in range(PASTE_ATTEMPTS):
            if flipping() or stop.is_set():
                break
            if self._write_echoed(control, paste):
                delivered = True
                break
            if not self._quiet_again(control, stop, flipping):
                break
        if not delivered or flipping():
            self.inject_failed = True
            return
        time.sleep(0.15)   # let the composer ingest the paste
        # The submit must be acknowledged too: a TUI that echoed the paste
        # and then produced nothing for the Enter has a prompt sitting in
        # its composer, not a turn running — keep the request so the user
        # is told, rather than release on faith.
        if not self._write_echoed(control, b"\r") or flipping():
            self.inject_failed = True
            return
        routefile.release(self.session.tandem_id, req.id)

    @staticmethod
    def _write_echoed(control: PtyControl, data: bytes) -> bool:
        """Write, then wait up to ECHO_WAIT_S for the child to produce any
        output — the only evidence available from outside that the bytes
        reached a TUI that was listening."""
        before = control.last_output
        if not control.write(data):
            return False
        deadline = time.monotonic() + ECHO_WAIT_S
        while time.monotonic() < deadline:
            if control.last_output > before:
                return True
            time.sleep(0.05)
        return False

    @staticmethod
    def _quiet_again(control: PtyControl, stop, flipping) -> bool:
        """Wait for the next output-then-quiet period (the TUI finishing
        whatever it was still doing), bounded by the delivery deadline."""
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not stop.is_set() and not flipping():
            last = control.last_output
            if last and time.monotonic() - last >= OUTPUT_QUIET_S:
                return True
            time.sleep(0.3)
        return False

    def cancelled(self) -> None:
        """Undo a routed arm: the monitor thread, after an armed wait was
        toggled off. The pending tab move is gone either way; a routed arm
        riding on it has to be undone here too, including the file — it is
        already claimed, and left alone it would be surfaced at the next
        frame start as a prompt that never landed."""
        self.tabs.cancelled()
        req = self.route_request
        if req is not None:
            self.route_request = None
            routefile.release(self.session.tandem_id, req.id)
            # Quoted whole, like every other route note: the file is gone by
            # the time this prints, so the note IS the prompt — a long exit
            # line beats a prompt the user cannot get back.
            self.notes.append("routed turn cancelled — the prompt was "
                              f"discarded: {req.prompt!r}")

    def exit_notes(self) -> None:
        """The report line a run owes the user when its injection failed.

        The note carries the prompt itself rather than promising the file:
        usually the claimed request is still on disk for the next start to
        surface, but a second routed prompt claimed during this run would
        have renamed over it. Quoting it here is true either way."""
        req = self.inject
        if req is not None and self.inject_failed:
            self.notes.append(
                "routed prompt was not delivered — the prompt is below;"
                f" re-type it in {req.target} ({req.prompt!r})"
            )
