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
from ..harness import get_adapter
from ..promptroute import RouteError, parse_route
from ..sync import SyncSetupError
from .events import Answers, Failure, Idle, LiveEvent, TurnFinished, TurnStarted


@dataclass(frozen=True)
class Pending:
    harness: str
    model: str
    prompt: str


class Dispatcher:
    def __init__(self, store, session, runtimes: dict, emit: Callable[[LiveEvent], None],
                 answers: Answers, *, meters: dict | None = None):
        self.store = store
        self.session = session
        self.runtimes = runtimes
        self.emit = emit
        self.answers = answers
        self.meters = meters or {}
        self.queue: deque[Pending] = deque()
        self._thread: threading.Thread | None = None
        self._current: str | None = None
        self._running = False

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

    # -- input ---------------------------------------------------------------

    def submit(self, text: str) -> str:
        text = text.strip()
        if not text:
            return ""
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
                self._set_default(harness)
                model = self.pin(harness)
                return f"default → {harness}" + (f" · {model}" if model else "")
        item = Pending(harness, self.pin(harness), prompt)
        if self.busy:
            self.queue.append(item)
            return f"queued → {harness}"
        self._start(item)
        return ""

    def pump(self) -> None:
        if not self.busy and self.queue:
            self._start(self.queue.popleft())

    def interrupt(self) -> None:
        current = self._current
        if current is not None:
            self.runtimes[current].interrupt()

    def close(self) -> None:
        self.queue.clear()
        for rt in self.runtimes.values():
            try:
                rt.close()
            except Exception:
                pass

    # -- the turn ------------------------------------------------------------

    def _set_default(self, harness: str) -> None:
        if harness != self.session.active:
            self.store.set_active(self.session.tandem_id, harness)
        self.session = self.store.get_session(self.session.tandem_id) or self.session

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

    def _start(self, item: Pending) -> None:
        self._current = item.harness
        self._running = True
        self._thread = threading.Thread(target=self._run, args=(item,),
                                        name="tandem-chat-turn", daemon=True)
        self._thread.start()

    def _run(self, item: Pending) -> None:
        harness = item.harness
        self.emit(TurnStarted(harness, item.model, item.prompt))
        try:
            problems = self._validate(harness)
            if problems:
                self.emit(Failure(f"{harness} transcript: " + "; ".join(problems)))
                self.emit(TurnFinished("failed", ""))
                return
            session = self.session
            if session.native_id(harness):
                ops.prepare_turn(self.store, session, harness)
            # else: nothing to fast-forward and no file to drain into yet — the
            # first turn on a never-run codex starts context-less, as `tandem run
            # --on codex` does, and sync_after_turn translates it outward once
            # its thread id is adopted below
            outcome = self.runtimes[harness].run_turn(
                session, session.native_id(harness), item.prompt, item.model, self.emit, self.answers)
            if outcome.native_id:
                self.session = ops.adopt_native_id(self.store, session, harness, outcome.native_id)
            # the target stays the default even after a failure: its file holds the partial turn
            self._set_default(harness)
            ops.sync_after_turn(self.store, self.session, harness)
            self.store.touch_used(self.session.tandem_id)
            meter = self.meters.get(harness)
            if meter is not None:
                meter.poll()
        except SyncSetupError as exc:
            self.emit(Failure(f"sync: {exc}"))
        except Exception as exc:                       # a runtime bug must not kill the window
            self.emit(Failure(f"{harness}: {type(exc).__name__}: {exc}"))
        finally:
            # free before the announcement: a window that pumps straight out
            # of this Idle — even synchronously, on this thread — must find
            # the dispatcher idle, or the queued turn stalls until the next
            # submit and then runs out of order
            self._current = None
            self._running = False
            self.emit(Idle())
