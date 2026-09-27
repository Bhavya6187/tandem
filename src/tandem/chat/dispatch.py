"""One turn at a time, on whichever harness the prompt names or the last
turn ran on. The pipeline is the one-off run's bookkeeping with a streaming
runner in the middle:

  parse route → (bare route: set default, done) → queue if busy →
  validate target transcript → ops.prepare_turn → runtime.run_turn →
  adopt a freshly minted id → target becomes the default → ops.sync_after_turn
  → feed the usage meter → hand the turn to the navigator, if any → Idle

The worker thread emits every event through `emit`; the window drains
them on the main thread and calls pump() on Idle."""

from __future__ import annotations

import contextlib
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable

from .. import ops
from ..constants import TURN_ENDED_NOTE
from ..harness import get_adapter
from ..promptroute import RouteError, parse_route
from ..sync import SyncSetupError
from .events import (Answers, Failure, FileDiff, Idle, LiveEvent, Notice, TextDelta, ToolFinished,
                     ToolOutput, ToolStarted, TurnFinished, TurnOutcome, TurnStarted, Verdict)
from .navigator import SCHEMA, DenyAll, FactsCollector, build_prompt, compute_diff, parse_verdict


@dataclass
class Pending:
    harness: str
    model: str
    prompt: str
    command: str = ""      # "" for a prompt; "compact" | "models" for a window command
    kind: str = ""         # "" for a prompt; "review" | "followup": the two turns of a review round
    peer: str = ""         # the round's other harness (TurnStarted.peer)
    carried: str = ""      # a follow-up: the note summary the header shows
    facts: object = None   # a review: the TurnFacts of the turn it reviews


def parse_command(prompt: str) -> tuple[str, str] | None:
    """(command, argument) when the prompt is one of tandem's dispatcher
    commands, else None. `/compact` is the whole prompt or nothing —
    `/compact focus on X` is claude's own form and goes through as text."""
    head = prompt.split(maxsplit=1)
    if not head:
        return None
    if head[0] == "/compact" and len(head) == 1:
        return "compact", ""
    if head[0] == "/model":
        return "models", head[1].strip() if len(head) > 1 else ""
    return None


def _close_note(harness: str, outcome: TurnOutcome) -> str | None:
    """The note that closes an unanswered turn in the other sessions, or None
    when the turn completed and owes them nothing."""
    if outcome.status == "completed":
        return None
    note = TURN_ENDED_NOTE.format(harness=harness, status=outcome.status)
    return f"{note}: {outcome.error}" if outcome.error else note


def _mute_review(forward: Callable[[LiveEvent], None], sink: list[str]) -> Callable[[LiveEvent], None]:
    """A review turn's emit: its reply is the JSON verdict, collected for
    the parser and never painted; every other event goes through — except
    claude's StructuredOutput call and everything under it."""
    verdict_calls: set[str] = set()

    def emit(ev: LiveEvent) -> None:
        if isinstance(ev, TextDelta):
            sink.append(ev.text)
        elif isinstance(ev, ToolStarted) and ev.tool == "StructuredOutput":
            # claude's --json-schema reply is a tool call: the verdict, not a tool
            verdict_calls.add(ev.call_id)
        elif isinstance(ev, (ToolOutput, ToolFinished, FileDiff)) and ev.call_id in verdict_calls:
            pass
        else:
            forward(ev)
    return emit


class Dispatcher:
    def __init__(self, store, session, runtimes: dict, emit: Callable[[LiveEvent], None],
                 answers: Answers, *, meters: dict | None = None,
                 add_meters: Callable[[object], None] | None = None,
                 first_turn: Callable[[], None] | None = None,
                 navigator=None):
        self.store = store
        self.session = session
        self.runtimes = runtimes
        self.emit = emit
        self.answers = answers
        self.meters = meters if meters is not None else {}
        # a harness has no transcript to meter until its first turn has
        # written one (and a codex or opencode no id before that), so every
        # turn end is a chance to bring in the meters the window could not
        # build when it opened
        self._add_meters = add_meters
        # what a fresh session puts off until it is used (seeding the other
        # harnesses' session files): a window opened and closed leaves nothing
        # behind. Kept until it succeeds, so a failed attempt is retried.
        self._first_turn = first_turn
        # the second harness that reviews each turn (chat/navigator.py), or
        # None: then nothing here changes — no facts, no trailer, no lock
        self.navigator = navigator
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
        """The window's `/mode` (and its `/skip-permissions` alias). A runtime reads its cfg as a turn
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
        command = ""
        parsed = parse_command(prompt)
        if parsed is not None:
            command, arg = parsed
            if command == "models" and arg:
                if any(ch.isspace() for ch in arg):
                    # `/{harness}:gpt 5.5` would pin `gpt` and run `5.5` as a prompt
                    return "error: /model takes one model name"
                return self.submit(f"/{harness}:{arg}")     # `/model NAME` is the pin route
            if command == "compact":
                prompt = "/compact"
        item = Pending(harness, self.pin(harness), prompt, command)
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

    def start_round(self, facts) -> None:
        """The navigator's turn mode: queue a review of the turn `facts`
        describe ahead of anything typed. Called from the worker that ran
        that turn, before it declares itself idle, so nothing typed can
        start between the turn and its review."""
        nav = self.navigator
        with self._lock:
            if self._closed or nav is None:
                return
            self.queue.appendleft(Pending(nav.harness, nav.cfg.navigator_model, "",
                                          kind="review", peer=facts.harness, facts=facts))

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
        if self.navigator is not None:
            try:
                self.navigator.close()
            except Exception:
                pass
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

    def _list_models(self, item: Pending) -> None:
        """`/model` with no name: ask the runtime, on this worker, and hand
        the rows back as one Notice. No transcript is touched, so none of
        the turn pipeline runs."""
        try:
            pin = self.pin(item.harness)
            rows = self.runtimes[item.harness].list_models(self.session)
            marked = [("* " if pin and row.split()[0] == pin else "  ") + row for row in rows]
            self.emit(Notice("\n".join(marked) if marked else f"{item.harness}: no models listed"))
        except Exception as exc:                       # a listing must never take the window down
            self.emit(Failure(f"{item.harness} models: {exc}"))
        finally:
            with self._lock:
                self._current = None
                self._running = False
            self.emit(Idle())

    def _run(self, item: Pending, spoken: int) -> None:
        harness = item.harness
        if item.command == "models":
            self._list_models(item)
            return
        ran = False
        nav = self.navigator
        review = item.kind == "review"
        started = time.monotonic()
        # a note the navigator left rides this prompt as a trailer — taken
        # now, not at submit, so a note that lands while a prompt is queued
        # still reaches it. A command turn (a compact) carries none, and a
        # round's own turns carry none: the follow-up IS the note.
        note = nav.take(harness) if nav is not None and not item.command and not item.kind else None
        prompt = item.prompt + note.trailer() if note is not None else item.prompt
        carried = note.summary if note is not None else item.carried
        # a review's prompt is built inside the try below, so a diff that
        # cannot be read still settles the round and idles; its painter
        # never echoes the prompt
        self.emit(TurnStarted(harness, item.model, "" if review else item.prompt,
                              carried=carried, kind=item.kind, peer=item.peer))
        facts = None
        emit = self.emit
        text: list[str] = []
        lock = nav.shadow_lock if nav is not None else contextlib.nullcontext()
        outcome = None
        synced = False
        settled = False
        err = ""
        try:
            if self._first_turn is not None:
                self._first_turn()
                self._first_turn = None
            # read after the seeding: a turn that gets this far has its
            # shadows, so its review is not skipped as a first turn
            first = self._first_turn is not None
            if review:
                # the diff is read here, on the worker, after the reviewed turn synced
                prompt = build_prompt(item.facts, compute_diff(self.session.cwd, item.facts.paths,
                                                               item.facts.commands))
            if nav is not None and not item.command and not review:
                facts = FactsCollector(harness, item.prompt, note is not None or item.kind == "followup",
                                       first, self.emit)
                emit = facts.emit
            elif review:
                emit = _mute_review(self.emit, text)
            problems = self._validate(harness)
            if problems:
                err = f"{harness} transcript: " + "; ".join(problems)
                self.emit(Failure(err))
                self.emit(TurnFinished("failed", ""))
                self._give_back(note)
                return
            session = self.session
            if session.native_id(harness):
                # prepare_turn also seeds any participant whose harness has
                # never run (a fresh session's active claude has no file yet),
                # and hands back the session that knows the ids it minted.
                # Under the navigator's lock: a claude review fork reads the
                # shadow this drains into.
                with lock:
                    session = self.session = ops.prepare_turn(self.store, session, harness)
            # else: nothing to fast-forward and no file to drain into yet — the
            # first turn on a never-run codex starts context-less, as `tandem run
            # --on codex` does, and sync_after_turn translates it outward once
            # its thread id is adopted below. Nothing needs seeding there
            # either: an active codex with no id is the only harness a fresh
            # pairing leaves fileless, and every other side already has one.
            outcome = self.runtimes[harness].run_turn(
                session, session.native_id(harness), prompt, item.model, emit,
                DenyAll() if review else self.answers, command=item.command,
                **({"review": SCHEMA} if review else {}))
            ran = True      # from here on the runtime has emitted its own TurnFinished
            if outcome.native_id:
                self.session = ops.adopt_native_id(self.store, session, harness, outcome.native_id)
            # the target becomes the default — its file holds the turn, partial
            # or not — unless the user named another harness since this turn
            # started: a bare `/codex` typed while claude worked is the later word.
            # A round's turns never take it: the review is an aside, and the
            # follow-up was queued by tandem, not typed — a `/codex` sent while
            # the review ran predates it and must outlive it
            if self._spoken == spoken and not item.kind:
                self._set_default(harness)
            else:
                self._reload_session()          # keep the ids this turn minted alongside that word
            # A turn that did not complete can have recorded the prompt and no
            # answer (a model call that 401s does exactly that). Synced
            # outward as-is it leaves every other session ending on a user
            # message, which opencode's dry-resume check rejects — wedging
            # every later turn there. Close it in the shadows as we sync.
            quarantine_pre = self._failed_turns(harness)
            with lock:
                ops.sync_after_turn(self.store, self.session, harness,
                                    close_note=_close_note(harness, outcome))
            synced = True       # only a turn the shadows received is worth reviewing
            self._report_quarantine(harness, quarantine_pre)
            self.store.touch_used(self.session.tandem_id)
            if self._add_meters is not None:
                self._add_meters(self.session)
            meter = self.meters.get(harness)
            if meter is not None:
                meter.poll()
            if review:
                # claimed before the call: a raise inside it must not settle the round twice
                settled = True
                self._settle_review(item, outcome, "".join(text), started)
        except SyncSetupError as exc:
            err = f"sync: {exc}"
            self.emit(Failure(err))
            self._finish_unrun(ran)
            if not ran:
                self._give_back(note)
        except Exception as exc:                       # a runtime bug must not kill the window
            err = f"{harness}: {type(exc).__name__}: {exc}"
            self.emit(Failure(err))
            self._finish_unrun(ran)
            if not ran:
                self._give_back(note)
        finally:
            # the navigator hears the turn before the dispatcher frees itself:
            # in turn mode it queues the review from inside turn_ended, and a
            # prompt submitted in between must land behind it
            if facts is not None and outcome is not None and synced:
                try:
                    nav.turn_ended(facts.finish(outcome.status), self.session)
                except Exception:
                    pass                               # the navigator must never take the window down
            if review and not settled:
                self._fail_review(item, err or "review ended without a verdict", started)
            # free before the announcement: a window that pumps straight out
            # of this Idle — even synchronously, on this thread — must find
            # the dispatcher idle, or the queued turn stalls until the next
            # submit and then runs out of order
            with self._lock:
                self._current = None
                self._running = False
            self.emit(Idle())

    def _give_back(self, note) -> None:
        """A note taken for a turn no model ever saw goes back to the
        navigator, to ride the next prompt instead."""
        if note is None:
            return
        try:
            self.navigator.give_back(note)
        except Exception:
            pass                                       # the navigator must never take the window down

    def _settle_review(self, item: Pending, outcome: TurnOutcome, text: str, started: float) -> None:
        """A review turn ended and synced: parse the reply, let the navigator
        settle and paint it, and queue the follow-up when it spoke."""
        nav = self.navigator
        base = dict(navigator=nav.harness, model=item.model, elapsed=time.monotonic() - started)
        if outcome.status == "interrupted":
            verdict = Verdict("error", error="interrupted", **base)
        elif outcome.status != "completed":
            verdict = Verdict("error", error=(outcome.error or f"{item.harness} review {outcome.status}")[:200],
                              **base)
        else:
            verdict = parse_verdict(outcome.structured, text, **base)
        try:
            note = nav.settle_round(item.facts, verdict)
            if note is None:
                return
            executor = item.facts.harness
            followup = Pending(executor, self.pin(executor), note.followup_prompt(),
                               kind="followup", peer=nav.harness, carried=note.summary)
            with self._lock:
                if self._closed:
                    return
                self.queue.appendleft(followup)
            nav.log.ridden(note.ref, executor)
        except Exception:
            pass                                       # the navigator must never take the window down

    def _fail_review(self, item: Pending, error: str, started: float) -> None:
        """A review turn that never reached the parser: the round ends on an
        error verdict the navigator logs and counts."""
        nav = self.navigator
        try:
            nav.settle_round(item.facts, Verdict("error", error=error[:200], navigator=nav.harness,
                                                 model=item.model, elapsed=time.monotonic() - started))
        except Exception:
            pass                                       # the navigator must never take the window down

    def _finish_unrun(self, ran: bool) -> None:
        """Every TurnStarted owes the renderer one terminal TurnFinished. The
        runtime emits its own on every path, so this is only for the failures
        that happen before it ran (a wedged drain in prepare_turn) — one
        arriving after would be a second."""
        if not ran:
            self.emit(TurnFinished("failed", ""))
