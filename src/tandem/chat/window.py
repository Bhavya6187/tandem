"""Wiring: the composer feeds the dispatcher, the dispatcher's worker posts
live events, the main thread paints them and answers prompts.

Threads: the main loop selects on stdin and a wake pipe; the dispatcher
runs each turn on a worker; runtimes block on WindowAnswers until the user
types an answer. Every event crosses to the main thread through a queue
plus one byte on the wake pipe, so the loop never polls."""

from __future__ import annotations

import dataclasses
import os
import queue
import select
import signal
import sys
import termios
import time
import tty
from typing import Callable

from ..config import load_frame_config
from ..events import SessionContext, UserMessage
from ..frame import StatusBar
from ..harness import get_adapter
from ..ptyrun import _winsize
from ..ratelimit import RateLimitPoller, remember
from ..runner import UsageFeed
from ..state import SyncCursor
from .activity import Activity
from .composer import Answer, Cancel, Composer, CtrlC, Interrupt, Repaint, Submit
from .dispatch import Dispatcher
from .events import (ApprovalRequest, Failure, Idle, LimitsUpdate, LiveEvent, QuestionRequest,
                     ReviewFinished, ReviewStarted, TextDelta, ThinkingDelta, ToolFinished,
                     ToolOutput, ToolStarted, TurnFinished, TurnStarted)
from .files import list_paths
from .navigator import Navigator, NavigatorLog, headroom_ok, log_path
from .render import Screen
from .reviewers import make_reviewer
from .runtime.factory import make_runtimes

WINDOW_COMMANDS = ("/quit", "/status", "/skip-permissions", "/note")
_BUSY_TICK = 0.12            # the spinner's frame is 0.1 s; slower and it visibly skips
_LONG_TURN_SECONDS = 15.0    # a turn this long ends with the bell


def route_hint(participants: list[str]) -> str:
    """The bar's trailer: only the harnesses `/` can route to in this session."""
    return " ".join(f"/{h}" for h in participants) + " route"


def window_command(text: str) -> str:
    """Tandem's own few, recognized as a whole leading word — `/quitter` is
    somebody else's. Everything else the composer submits belongs to the
    harness, slash commands included."""
    head = text.strip().split(maxsplit=1)
    return head[0] if head and head[0] in WINDOW_COMMANDS else ""


_CLOSED = object()      # what close() leaves in the answers queue for whoever is waiting


class WindowAnswers:
    def __init__(self, post: Callable[[LiveEvent], None]):
        self._post = post
        self._q: queue.Queue = queue.Queue()
        self._closed = False

    def approve(self, req: ApprovalRequest) -> str:
        return self._ask(req, "deny")

    def answer(self, req: QuestionRequest) -> str:
        return self._ask(req, "")

    def _ask(self, req, fallback: str) -> str:
        """Put the request on screen and wait for the key. After close()
        nobody is at the keyboard: the request waiting now gets the fallback,
        and every later one is answered at once without reaching the screen —
        a runtime that asks its questions one after another (claude's
        AskUserQuestion) would otherwise park the worker on the second."""
        if self._closed:
            return fallback
        self._drop_stale()
        self._post(req)
        got = self._q.get()
        return fallback if got is _CLOSED else got

    def resolve(self, text: str) -> None:
        self._q.put(text)

    def close(self) -> None:
        self._closed = True
        self._q.put(_CLOSED)

    def _drop_stale(self) -> None:
        """Every answer belongs to the request that was on screen when the
        user pressed the key. A value still sitting here answers nothing the
        user has seen, so it must never be handed to the next request — an
        inherited "allow" would approve an unseen command. The window drops
        the duplicate keypress that would leave one (see handle_input); this
        is the belt to that pair of braces."""
        while True:
            try:
                got = self._q.get_nowait()
            except queue.Empty:
                return
            if got is _CLOSED:
                self._q.put(got)             # close() spoke; the wait below must hear it
                return


class Window:
    def __init__(self, session, store, cfg, screen: Screen, composer: Composer,
                 dispatcher, answers: WindowAnswers, bar: StatusBar, usage_state: dict,
                 meters: dict, poller: RateLimitPoller | None = None,
                 stdin_fd: int | None = None, clock: Callable[[], float] = time.monotonic,
                 navigator=None):
        self.session, self.store, self.cfg = session, store, cfg
        self.screen, self.composer, self.dispatcher = screen, composer, dispatcher
        self.answers, self.bar, self.usage_state, self.meters, self.poller = answers, bar, usage_state, meters, poller
        self.stdin_fd = stdin_fd
        self.activity = Activity(clock)
        self._ctrlc_at = 0.0
        self.navigator = navigator
        self._deferred: list[ReviewFinished] = []   # verdicts that landed mid-turn

    # -- painting ------------------------------------------------------------

    def bar_line(self) -> str:
        default = self.dispatcher.default
        self.bar.active = default
        self.bar.others = [h for h in self.session.participants if h != default]
        marks = {h: "skip-perms" for h in self._skipping()}
        nav = self.navigator
        if nav is not None:
            word = nav.mark()
            if word:
                marks[nav.harness] = " · ".join(filter(None, [marks.get(nav.harness, ""), word]))
        self.bar.marks = marks
        meter = self.meters.get(default)
        usage = meter.state.get("text", "") if meter is not None else ""
        return self.bar.line(False, usage, self.usage_state.get("limits") or {})

    def _skipping(self) -> list[str]:
        """The harnesses whose next turn asks nothing. opencode never: the
        setting does not reach it. codex only while no explicit
        `[chat] codex_approval_policy` overrules the default it sets."""
        if not self.cfg.skip_permissions:
            return []
        skipping = ["claude"]
        if self.cfg.codex_approval_policy in ("", "never"):
            skipping.append("codex")
        return skipping

    def set_skip_permissions(self, arg: str) -> None:
        """`/skip-permissions [on|off]`, bare = the other way. This window
        only, like the launch flag: nothing is written to the config."""
        if arg not in ("", "on", "off"):
            self.screen.note("usage: /skip-permissions [on|off]")
            return
        skip = arg == "on" if arg else not self.cfg.skip_permissions
        self.cfg = dataclasses.replace(self.cfg, skip_permissions=skip)
        self.dispatcher.set_cfg(self.cfg)
        self.screen.note(f"permissions {'skipped' if skip else 'asked'} from the next turn")

    def note_command(self, arg: str) -> None:
        """`/note` shows the pending note in full; `dismiss` drops it; `good`
        and `bad` drop it and record whether it helped."""
        nav = self.navigator
        if nav is None:
            self.screen.note("navigator is off")
            return
        if arg == "":
            note = nav.pending()
            if note is None:
                self.screen.note("no pending note")
            else:
                self.screen.review(ReviewFinished(note.navigator, note.verdict))
            return
        if arg not in ("dismiss", "good", "bad"):
            self.screen.note("usage: /note [dismiss|good|bad]")
            return
        had = nav.pending() is not None
        done = nav.dismiss(None if arg == "dismiss" else arg)
        if not done:
            self.screen.note("no pending note")
        else:
            self.screen.note("note dropped" if had else "feedback recorded")

    def status_line(self) -> str:
        """What `/status` prints: the session this window is driving, where
        the next bare prompt goes, and the model pins that would ride with it."""
        parts = [f"session {self.session.tandem_id}",
                 f"default {self.dispatcher.default}",
                 "participants " + ", ".join(self.session.participants)]
        pins = [f"{h}={self.dispatcher.pin(h)}" for h in self.session.participants
                if self.dispatcher.pin(h)]
        if pins:
            parts.append("pins: " + ", ".join(pins))
        if self.cfg.skip_permissions:
            parts.append("permissions skipped")
        if self.navigator is not None:
            parts.append(f"navigator {self.navigator.harness} · {self.cfg.navigator_deliver}")
        return " · ".join(parts)

    def paint(self) -> None:
        rows, row, col = self.composer.rows(self.screen.cols, self.screen.composer_max_rows)
        queued = len(getattr(self.dispatcher, "queue", ()))
        self.screen.paint_bottom(self.bar_line(), rows, row, col, focus_composer=True,
                                 activity=self.activity.text(queued, self.screen.cols - 4),
                                 urgent=self.activity.waiting)

    @property
    def tick_seconds(self) -> float:
        """How long the loop may sleep before a repaint. A running turn's
        spinner and timer move on their own clock — a thinking model posts
        nothing for them to ride on; a line waiting on the user is static,
        like the idle rule."""
        return _BUSY_TICK if self.activity.animating else 1.0

    def _ring(self) -> None:
        if self.cfg.bell:
            self.screen.bell()

    def resize(self, rows: int, cols: int) -> None:
        """SIGWINCH: the scroll region and the bar move together, and the
        bottom block is repainted at once — otherwise it stays wherever the
        old geometry left it until the next event or the select timeout."""
        self.screen.resize(rows, cols)
        self.bar.resize(rows, cols)
        self.paint()

    def paint_history(self) -> None:
        if self.cfg.history_turns <= 0:
            # no history means none: the trim below indexes starts[-N], and
            # starts[-0] is starts[0] — the whole transcript
            return
        harness = self.dispatcher.default
        sid = self.session.native_id(harness)
        if not sid:
            return
        try:
            adapter = get_adapter(harness)
            path = adapter.transcript_path(self.session.cwd, sid)
            if path is None:
                return
            others = self.session.targets_for(harness)
            ctx = SessionContext(tandem_id=self.session.tandem_id, cwd=self.session.cwd,
                                 direction=f"{harness}->{others[0] if others else harness}",
                                 source_session_id=sid,
                                 target_session_id=self.session.native_id(others[0]) if others else None)
            cursor = SyncCursor(tandem_id=self.session.tandem_id, source=harness, target="__chat__")
            reader = adapter.make_source_reader(self.session, cursor, path)
            events = []
            for line in reader.poll():
                if line.raw is not None:
                    events.extend(adapter.parse_entry(line.raw, ctx))
            starts = [i for i, e in enumerate(events) if isinstance(e, UserMessage)]
            if len(starts) > self.cfg.history_turns:
                events = events[starts[-self.cfg.history_turns]:]
            self.screen.history(events, harness)
        except Exception as exc:                       # history is a courtesy, never a blocker
            self.screen.note(f"history unavailable: {type(exc).__name__}: {exc}")

    # -- events (main thread) --------------------------------------------------

    def handle_event(self, ev: LiveEvent) -> None:
        s = self.screen
        was_active = self.activity.active
        self.activity.on_event(ev)
        if isinstance(ev, TurnStarted):
            s.turn_started(ev)
        elif isinstance(ev, TextDelta):
            s.text_delta(ev)
        elif isinstance(ev, ThinkingDelta):
            s.thinking_delta(ev)
        elif isinstance(ev, ToolStarted):
            s.tool_started(ev)
        elif isinstance(ev, ToolOutput):
            s.tool_output(ev)
        elif isinstance(ev, ToolFinished):
            s.tool_finished(ev)
        elif isinstance(ev, ApprovalRequest):
            self._flush_input()
            s.approval(ev)
            self.composer.begin_approval(ev)
            self._ring()
        elif isinstance(ev, QuestionRequest):
            self._flush_input()
            s.question(ev)
            self.composer.begin_question(ev)
            self._ring()
        elif isinstance(ev, ReviewStarted):
            pass                                   # the activity line and the bar mark carry it
        elif isinstance(ev, ReviewFinished):
            if self.activity.active:
                self._deferred.append(ev)          # never split a streaming paragraph
            else:
                s.review(ev)
        elif isinstance(ev, TurnFinished):
            s.turn_finished(ev, self.activity.last_elapsed if was_active else None)
            for deferred in self._deferred:
                s.review(deferred)
            self._deferred.clear()
            # whoever sat through a short turn saw it end; a long one is
            # the one they left for another window
            if was_active and self.activity.last_elapsed >= _LONG_TURN_SECONDS:
                self._ring()
        elif isinstance(ev, Failure):
            s.failure(ev)
        elif isinstance(ev, LimitsUpdate):
            limits = dict(self.usage_state.get("limits") or {})
            limits[ev.harness] = ev.text
            self.usage_state["limits"] = limits
            wins = dict(self.usage_state.get("windows") or {})
            wins[ev.harness] = list(ev.windows)
            self.usage_state["windows"] = wins
            remember(ev.harness, ev.text, ev.windows)     # else a throttled poller's next refresh blanks it
        elif isinstance(ev, Idle):
            self.session = getattr(self.dispatcher, "session", self.session)
            self.dispatcher.pump()
            if self.poller is not None:
                self.poller.poke()
        self.paint()

    # -- input (main thread) ---------------------------------------------------

    def _flush_input(self) -> None:
        """Drop whatever is still unread in the tty before an answer row goes
        up. The loop drains live events before it reads stdin in the same
        pass, so bytes typed while the model was working would arrive as the
        first chunk after the row and their first character would be read as
        the answer to a request the user has not seen. Anything typed after
        the row is untouched."""
        if self.stdin_fd is None:
            return
        try:
            termios.tcflush(self.stdin_fd, termios.TCIFLUSH)
        except (termios.error, OSError, ValueError):
            pass                                  # not a tty (tests, a pipe)

    def _deny_pending(self) -> bool:
        """Answer an approval or question the user is walking away from, and
        say whether there was one. A runtime waiting on an answer is parked in
        the answers queue, not inside an interruptible turn: interrupting one
        without answering it leaves the worker asleep forever and every later
        prompt queued behind it. Deny first, then interrupt."""
        if self.composer.mode == "prompt":
            return False
        self.composer.end_answer()
        self.answers.resolve("deny")
        self.activity.answered()
        return True

    def handle_input(self, data: bytes) -> bool:
        for action in self.composer.feed(data):
            if isinstance(action, Submit):
                command = window_command(action.text)
                if command == "/quit":
                    return False
                if command == "/status":
                    self.screen.note(self.status_line())
                    continue
                if command == "/skip-permissions":
                    self.set_skip_permissions(action.text.strip()[len(command):].strip())
                    continue
                if command == "/note":
                    self.note_command(action.text.strip()[len(command):].strip())
                    continue
                note = self.dispatcher.submit(action.text)
                if note.startswith("error: "):
                    self.screen.failure(Failure(note[7:]))
                elif note:
                    self.screen.note(note)
            # One request, one answer. The composer leaves answer mode only
            # when the window says so, so a second answer can still arrive
            # behind the first — another action in the same read (a question's
            # `2\x7f3`), or the next read before the runtime has posted
            # anything new. The mode check makes it a no-op: resolving twice
            # strands a value that silently answers the NEXT request, and the
            # runtime that asked this one is already gone.
            elif isinstance(action, Answer):
                if self.composer.mode != "prompt":
                    self.composer.end_answer()
                    self.answers.resolve(action.text)
                    self.activity.answered()
            elif isinstance(action, Cancel):
                if self._deny_pending():
                    self.dispatcher.interrupt()
                    self.screen.note("denied · interrupting…")
            elif isinstance(action, Interrupt):
                denied = self._deny_pending()
                if denied or self.dispatcher.busy:
                    self.dispatcher.interrupt()
                    self.screen.note(("denied · " if denied else "") + "interrupting…")
            elif isinstance(action, CtrlC):
                now = time.monotonic()
                if now - self._ctrlc_at < 2.0:
                    return False
                self._ctrlc_at = now
                denied = self._deny_pending()
                if denied or self.dispatcher.busy:
                    self.dispatcher.interrupt()
                    self.screen.note(("denied · " if denied else "")
                                     + "interrupting… (Ctrl-C again to quit)")
                else:
                    self.screen.note("Ctrl-C again to quit")
            elif isinstance(action, Repaint):
                self.screen.enter()
        self.paint()
        return True


def run_chat(session, store, cfg, *, stdin_fd: int | None = None, out_fd: int | None = None,
             runtimes: dict | None = None, first_turn: Callable[[], None] | None = None) -> int:
    """`first_turn` is what a freshly paired session defers until its first
    prompt (cli._chat: seeding the other harnesses' session files). A window
    left before then has run nothing, and the launcher drops the session."""
    stdin_fd = sys.stdin.fileno() if stdin_fd is None else stdin_fd
    out_fd = sys.stdout.fileno() if out_fd is None else out_fd
    if not os.isatty(stdin_fd):
        sys.stderr.write("tandem needs a terminal\n")
        return 1
    rows, cols = _winsize(stdin_fd)
    events: queue.Queue = queue.Queue()
    wake_r, wake_w = os.pipe()
    closing = False

    def post(ev: LiveEvent) -> None:
        # nothing is painted after the loop ends, and the wake fds are closed
        # (and reused by whatever opens next) once close() returns
        if closing:
            return
        events.put(ev)
        try:
            os.write(wake_w, b"E")
        except OSError:
            pass

    def write(b: bytes) -> None:
        view = memoryview(b)
        while view:
            n = os.write(out_fd, view)
            view = view[n:]

    screen = Screen(write, rows, cols, cfg, color="NO_COLOR" not in os.environ)
    composer = Composer(paths=lambda: list_paths(session.cwd))
    answers = WindowAnswers(post)
    runtimes = runtimes if runtimes is not None else make_runtimes(session, cfg)
    meters: dict = {}

    def add_meters(sess) -> None:
        """A meter for every participant that has a transcript by now and no
        meter yet. Runs at open and after every turn: the active harness's
        file is written by its own first turn, and a seeded opencode has no
        transcript path until its session exists."""
        for h in sess.participants:
            sid = sess.native_id(h)
            path = get_adapter(h).transcript_path(sess.cwd, sid) if sid and h not in meters else None
            if path is not None:
                meter = UsageFeed(get_adapter(h), sess, path, {"text": ""})
                meter.poll()
                meters[h] = meter

    add_meters(session)
    usage_state: dict = {"limits": {}}
    poller = RateLimitPoller(list(session.participants), usage_state) if load_frame_config().rate_limits else None
    navigator = None
    nav_note = ""
    if cfg.navigator:
        if cfg.navigator in session.participants:
            navigator = Navigator(
                cfg.navigator, cfg, make_reviewer(cfg.navigator, cfg, store), post,
                NavigatorLog(log_path(session.tandem_id)),
                headroom=lambda: headroom_ok(usage_state, cfg.navigator, cfg.navigator_headroom))
        else:
            nav_note = (f"navigator {cfg.navigator} is not a participant of this session "
                        f"({', '.join(session.participants)}); off")
    dispatcher = Dispatcher(store, session, runtimes, post, answers, meters=meters,
                            add_meters=add_meters, first_turn=first_turn, navigator=navigator)
    bar = StatusBar(rows, cols, session.active, session.targets_for(session.active),
                    hint=route_hint(session.participants))
    win = Window(session, store, cfg, screen, composer, dispatcher, answers, bar, usage_state,
                 meters, poller, stdin_fd=stdin_fd, navigator=navigator)

    old_attrs = termios.tcgetattr(stdin_fd)
    old_winch = signal.signal(signal.SIGWINCH, lambda *_: os.write(wake_w, b"W"))
    try:
        tty.setraw(stdin_fd)
        screen.enter(fresh=True)
        if nav_note:
            screen.note(nav_note)
        win.paint_history()
        if poller is not None:
            poller.ensure_started()
        win.paint()
        while True:
            ready, _, _ = select.select([stdin_fd, wake_r], [], [], win.tick_seconds)
            if wake_r in ready:
                kinds = os.read(wake_r, 4096)
                if b"W" in kinds:
                    win.resize(*_winsize(stdin_fd))
            # drained on every pass, not only when a wake byte arrived: `post`
            # swallows a failed write, and a queued event must not sit unseen
            # until some later event's byte gets through
            while True:
                try:
                    win.handle_event(events.get_nowait())
                except queue.Empty:
                    break
            # re-checked after the drain, not trusted from the select above: an
            # answer row flushes the tty input queue as it goes up, and the fd
            # is blocking with VMIN=1 — reading it now would wait for a
            # keypress, with the 1 s repaint and every queued event behind it
            if stdin_fd in ready and select.select([stdin_fd], [], [], 0)[0]:
                data = os.read(stdin_fd, 4096)
                if not data or not win.handle_input(data):
                    break
            if not ready:
                win.paint()         # the rate-limit figures and the activity line move on their own clock
    finally:
        closing = True                               # post() is a no-op from here
        dispatcher.close()                           # returns with the worker joined
        if poller is not None:
            poller.stop()
        screen.leave()
        termios.tcsetattr(stdin_fd, termios.TCSADRAIN, old_attrs)
        signal.signal(signal.SIGWINCH, old_winch)
        os.close(wake_r)
        os.close(wake_w)
    if not dispatcher.first_turn_pending:            # else nothing ran and the session is dropped
        write(f"tandem: session {session.tandem_id} · resume with "
              f"`tandem resume {session.tandem_id}`\r\n".encode())
    return 0
