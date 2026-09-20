"""One turn at a time, on whichever harness the prompt names or the last
turn ran on. The pipeline is the one-off run's bookkeeping with a streaming
runner in the middle:

  parse route → (bare route: set default, done) → queue if busy →
  validate target transcript → ops.prepare_turn → runtime.run_turn →
  adopt a freshly minted id → target becomes the default → ops.sync_after_turn
  → feed the usage meter → Idle

The worker thread emits every event through `emit`; the window drains
them on the main thread and calls pump() on Idle."""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass
from typing import Callable

from .. import ops
from ..constants import TURN_ENDED_NOTE
from ..harness import get_adapter
from ..promptroute import RouteError, parse_route
from ..sync import SyncSetupError
from .events import (Answers, Failure, Idle, LiveEvent, TurnFinished, TurnOutcome,
                     TurnStarted)


@dataclass(frozen=True)
class Pending:
    harness: str
    model: str
    prompt: str


def _close_note(harness: str, outcome: TurnOutcome) -> str | None:
    """The note that closes an unanswered turn in the other sessions, or None
    when the turn completed and owes them nothing."""
    if outcome.status == "completed":
        return None
    note = TURN_ENDED_NOTE.format(harness=harness, status=outcome.status)
    return f"{note}: {outcome.error}" if outcome.error else note


class Dispatcher:
    def __init__(self, store, session, runtimes: dict, emit: Callable[[LiveEvent], None],
                 answers: Answers, *, meters: dict | None = None,
                 first_turn: Callable[[], None] | None = None):
        self.store = store
        self.session = session
        self.runtimes = runtimes
        self.emit = emit
        self.answers = answers
        self.meters = meters if meters is not None else {}
        # what a fresh session puts off until it is used (seeding the other
        # harnesses' session files): a window opened and closed leaves nothing
        # behind. Kept until it succeeds, so a failed attempt is retried.
        self._first_turn = first_turn
        self.queue: deque[Pending] = deque()
        # guards the start-or-queue decision and the flags it sets, nothing
        # more: submit() runs on the main thread while pump() can run on the
        # finishing turn's worker thread (the window pumps on Idle), and
        # without it both can start a turn in the same instant. Never held
        # across a turn.
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._current: str | None = None
        self._running = False
        self._closed = False
        self._spoken = 0        # bumped by every bare route; a turn started before one must not undo it

    @property
    def first_turn_pending(self) -> bool:
        return self._first_turn is not None

    @property
    def default(self) -> str:
        return self.session.active

    @property
    def busy(self) -> bool:
        # an explicit flag, not the worker's liveness: the turn clears it
        # before emitting Idle, so a pump() driven by that Idle always sees
        # the dispatcher free and starts the next queued turn
        return self._running

    def pin(self, harness: str) -> str:
        return self.store.get_pin(self.session.tandem_id, harness)

    def set_cfg(self, cfg) -> None:
        """The window's `/skip-permissions`. A runtime reads its cfg as a turn
        starts, so a turn already running keeps the one it started under."""
        for rt in self.runtimes.values():
            rt.cfg = cfg

    # -- input ---------------------------------------------------------------

    def submit(self, text: str) -> str:
        text = text.strip()
        if not text:
            return ""
        if self._closed:
            # ahead of the route parse: a bare `/codex` or a `:model` would
            # otherwise write a pin and an active harness the window is in the
            # middle of shutting down
            return "closed"
        try:
            got = parse_route(text, self.session.participants)
        except RouteError as exc:
            return f"error: {exc}"
        if got is None:
            harness, prompt = self.default, text
        else:
            route, prompt = got
            harness = route.harness
            if route.model is not None:
                self.store.set_pin(self.session.tandem_id, harness, route.model)
            if not prompt:
                with self._lock:
                    self._spoken += 1
                self._set_default(harness)
                model = self.pin(harness)
                return f"default → {harness}" + (f" · {model}" if model else "")
        item = Pending(harness, self.pin(harness), prompt)
        with self._lock:
            if self._closed:
                return "closed"
            # a waiting queue means a pump is still owed: jumping it would
            # run the prompts out of the order they were typed
            if self._running or self.queue:
                self.queue.append(item)
                return f"queued → {harness}"
            self._start(item)
        return ""

    def pump(self) -> None:
        with self._lock:
            if not self._closed and not self._running and self.queue:
                self._start(self.queue.popleft())

    def interrupt(self) -> None:
        current = self._current
        if current is not None:
            self.runtimes[current].interrupt()

    def close(self) -> None:
        """Stop taking work, end the running turn, and do not return until its
        worker is gone. The window closes the state store and the wake pipe
        the moment this returns: a worker still inside sync_after_turn would
        write to a closed sqlite connection, and its events would post to
        reused fds. Never holds the lock across the join — the worker's own
        finally takes it."""
        with self._lock:
            self._closed = True
            self.queue.clear()
            thread = self._thread
        self.interrupt()
        # A runtime parked on an unanswered approval is asleep in the answers
        # queue, where interrupt cannot reach it; the window's Ctrl-C ladder
        # denies first for the same reason. Closing the answers denies that
        # one and everything the runtime asks after it — claude asks a
        # multi-question request one question at a time, and a single deny
        # would leave the worker parked on the second.
        close_answers = getattr(self.answers, "close", None)
        if close_answers is not None:
            try:
                close_answers()
            except Exception:
                pass
        for rt in self.runtimes.values():
            try:
                rt.close()
            except Exception:
                pass
        if thread is not None:
            thread.join(timeout=10.0)

    # -- the turn ------------------------------------------------------------

    def _reload_session(self) -> None:
        self.session = self.store.get_session(self.session.tandem_id) or self.session

    def _set_default(self, harness: str) -> None:
        if harness != self.session.active:
            self.store.set_active(self.session.tandem_id, harness)
        self._reload_session()

    def _validate(self, harness: str) -> list[str]:
        sid = self.session.native_id(harness)
        if not sid:
            return []
        adapter = get_adapter(harness)
        path = adapter.transcript_path(self.session.cwd, sid)
        if path is None:
            return []
        try:
            return adapter.validate_transcript(path, sid)
        except Exception as exc:                       # validation must never take the window down
            return [f"validation error: {exc}"]

    def _failed_turns(self, harness: str) -> dict[str, int]:
        return {t: self.store.get_cursor(self.session.tandem_id, harness, t).failed_turns
                for t in self.session.targets_for(harness)}

    def _report_quarantine(self, harness: str, before: dict[str, int]) -> None:
        """An entry the converter cannot translate is quarantined and replaced
        by a placeholder: the drain succeeds, so the window is the only place
        that can say a turn arrived incomplete on the other side."""
        for target, after in self._failed_turns(harness).items():
            grew = after - before.get(target, 0)
            if grew > 0:
                self.emit(Failure(
                    f"{grew} entr{'y' if grew == 1 else 'ies'} quarantined while "
                    f"syncing {harness} → {target}; see `tandem doctor`"))

    def _start(self, item: Pending) -> None:
        """Call with _lock held: the flags and the thread it hands them to
        must be claimed by one caller only."""
        self._current = item.harness
        self._running = True
        self._thread = threading.Thread(target=self._run, args=(item, self._spoken),
                                        name="tandem-chat-turn", daemon=True)
        self._thread.start()

    def _run(self, item: Pending, spoken: int) -> None:
        harness = item.harness
        ran = False
        self.emit(TurnStarted(harness, item.model, item.prompt))
        try:
            if self._first_turn is not None:
                self._first_turn()
                self._first_turn = None
            problems = self._validate(harness)
            if problems:
                self.emit(Failure(f"{harness} transcript: " + "; ".join(problems)))
                self.emit(TurnFinished("failed", ""))
                return
            session = self.session
            if session.native_id(harness):
                # prepare_turn also seeds any participant whose harness has
                # never run (a fresh session's active claude has no file yet),
                # and hands back the session that knows the ids it minted
                session = self.session = ops.prepare_turn(self.store, session, harness)
            # else: nothing to fast-forward and no file to drain into yet — the
            # first turn on a never-run codex starts context-less, as `tandem run
            # --on codex` does, and sync_after_turn translates it outward once
            # its thread id is adopted below. Nothing needs seeding there
            # either: an active codex with no id is the only harness a fresh
            # pairing leaves fileless, and every other side already has one.
            outcome = self.runtimes[harness].run_turn(
                session, session.native_id(harness), item.prompt, item.model, self.emit, self.answers)
            ran = True      # from here on the runtime has emitted its own TurnFinished
            if outcome.native_id:
                self.session = ops.adopt_native_id(self.store, session, harness, outcome.native_id)
            # the target becomes the default — its file holds the turn, partial
            # or not — unless the user named another harness since this turn
            # started: a bare `/codex` typed while claude worked is the later word
            if self._spoken == spoken:
                self._set_default(harness)
            else:
                self._reload_session()          # keep the ids this turn minted alongside that word
            # A turn that did not complete can have recorded the prompt and no
            # answer (a model call that 401s does exactly that). Synced
            # outward as-is it leaves every other session ending on a user
            # message, which opencode's dry-resume check rejects — wedging
            # every later turn there. Close it in the shadows as we sync.
            quarantine_pre = self._failed_turns(harness)
            ops.sync_after_turn(self.store, self.session, harness,
                                close_note=_close_note(harness, outcome))
            self._report_quarantine(harness, quarantine_pre)
            self.store.touch_used(self.session.tandem_id)
            meter = self.meters.get(harness)
            if meter is not None:
                meter.poll()
        except SyncSetupError as exc:
            self.emit(Failure(f"sync: {exc}"))
            self._finish_unrun(ran)
        except Exception as exc:                       # a runtime bug must not kill the window
            self.emit(Failure(f"{harness}: {type(exc).__name__}: {exc}"))
            self._finish_unrun(ran)
        finally:
            # free before the announcement: a window that pumps straight out
            # of this Idle — even synchronously, on this thread — must find
            # the dispatcher idle, or the queued turn stalls until the next
            # submit and then runs out of order
            with self._lock:
                self._current = None
                self._running = False
            self.emit(Idle())

    def _finish_unrun(self, ran: bool) -> None:
        """Every TurnStarted owes the renderer one terminal TurnFinished. The
        runtime emits its own on every path, so this is only for the failures
        that happen before it ran (a wedged drain in prepare_turn) — one
        arriving after would be a second."""
        if not ran:
            self.emit(TurnFinished("failed", ""))
