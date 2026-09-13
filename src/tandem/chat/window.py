"""Wiring: the composer feeds the dispatcher, the dispatcher's worker posts
live events, the main thread paints them and answers prompts.

Threads: the main loop selects on stdin and a wake pipe; the dispatcher
runs each turn on a worker; runtimes block on WindowAnswers until the user
types an answer. Every event crosses to the main thread through a queue
plus one byte on the wake pipe, so the loop never polls."""

from __future__ import annotations

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
from ..ratelimit import RateLimitPoller
from ..runner import UsageFeed
from ..state import SyncCursor
from .composer import Answer, Cancel, Composer, CtrlC, Interrupt, Repaint, Submit
from .dispatch import Dispatcher
from .events import (ApprovalRequest, Failure, Idle, LimitsUpdate, LiveEvent, QuestionRequest,
                     TextDelta, ThinkingDelta, ToolFinished, ToolOutput, ToolStarted,
                     TurnFinished, TurnStarted)
from .render import Screen
from .runtime.factory import make_runtimes

HINT = "/claude /codex /opencode route"


class WindowAnswers:
    def __init__(self, post: Callable[[LiveEvent], None]):
        self._post = post
        self._q: queue.Queue = queue.Queue()

    def approve(self, req: ApprovalRequest) -> str:
        self._drop_stale()
        self._post(req)
        return self._q.get()

    def answer(self, req: QuestionRequest) -> str:
        self._drop_stale()
        self._post(req)
        return self._q.get()

    def resolve(self, text: str) -> None:
        self._q.put(text)

    def _drop_stale(self) -> None:
        """Every answer belongs to the request that was on screen when the
        user pressed the key. A value still sitting here answers nothing the
        user has seen, so it must never be handed to the next request — an
        inherited "allow" would approve an unseen command. The window drops
        the duplicate keypress that would leave one (see handle_input); this
        is the belt to that pair of braces."""
        while True:
            try:
                self._q.get_nowait()
            except queue.Empty:
                return


class Window:
    def __init__(self, session, store, cfg, screen: Screen, composer: Composer,
                 dispatcher, answers: WindowAnswers, bar: StatusBar, usage_state: dict,
                 meters: dict, poller: RateLimitPoller | None = None,
                 stdin_fd: int | None = None):
        self.session, self.store, self.cfg = session, store, cfg
        self.screen, self.composer, self.dispatcher = screen, composer, dispatcher
        self.answers, self.bar, self.usage_state, self.meters, self.poller = answers, bar, usage_state, meters, poller
        self.stdin_fd = stdin_fd
        self._ctrlc_at = 0.0

    # -- painting ------------------------------------------------------------

    def bar_line(self) -> str:
        default = self.dispatcher.default
        self.bar.active = default
        self.bar.others = [h for h in self.session.participants if h != default]
        meter = self.meters.get(default)
        usage = meter.state.get("text", "") if meter is not None else ""
        return self.bar.line(False, usage, self.usage_state.get("limits") or {})

    def paint(self) -> None:
        text, col = self.composer.line(self.screen.cols)
        self.screen.paint_bottom(self.bar_line(), text, col, focus_composer=True)

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
        elif isinstance(ev, QuestionRequest):
            self._flush_input()
            s.question(ev)
            self.composer.begin_question(ev)
        elif isinstance(ev, TurnFinished):
            s.turn_finished(ev)
        elif isinstance(ev, Failure):
            s.failure(ev)
        elif isinstance(ev, LimitsUpdate):
            limits = dict(self.usage_state.get("limits") or {})
            limits[ev.harness] = ev.text
            self.usage_state["limits"] = limits
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
        return True

    def handle_input(self, data: bytes) -> bool:
        for action in self.composer.feed(data):
            if isinstance(action, Submit):
                note = self.dispatcher.submit(action.text)
                if note.startswith("error: "):
                    self.screen.failure(Failure(note[7:]))
                elif note:
                    self.screen.note(note)
            # One request, one answer. The composer stays in answer mode for
            # the whole read, so a single 4096-byte chunk can carry two answer
            # actions (`\x1by` → Cancel + Answer, `y\x7fy` → two Answers).
            # Leaving answer mode before resolving makes the second a no-op:
            # resolving twice would strand a value that silently answers the
            # next request — the runtime is already gone by then.
            elif isinstance(action, Answer):
                if self.composer.mode != "prompt":
                    self.composer.end_answer()
                    self.answers.resolve(action.text)
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
             runtimes: dict | None = None) -> int:
    stdin_fd = sys.stdin.fileno() if stdin_fd is None else stdin_fd
    out_fd = sys.stdout.fileno() if out_fd is None else out_fd
    if not os.isatty(stdin_fd):
        sys.stderr.write("tandem chat needs a terminal\n")
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
    composer = Composer()
    answers = WindowAnswers(post)
    runtimes = runtimes if runtimes is not None else make_runtimes(session, cfg)
    meters: dict = {}
    for h in session.participants:
        sid = session.native_id(h)
        path = get_adapter(h).transcript_path(session.cwd, sid) if sid else None
        if path is not None:
            meters[h] = UsageFeed(get_adapter(h), session, path, {"text": ""})
    usage_state: dict = {"limits": {}}
    poller = RateLimitPoller(list(session.participants), usage_state) if load_frame_config().rate_limits else None
    dispatcher = Dispatcher(store, session, runtimes, post, answers, meters=meters)
    bar = StatusBar(rows, cols, session.active, session.targets_for(session.active), hint=HINT)
    win = Window(session, store, cfg, screen, composer, dispatcher, answers, bar, usage_state,
                 meters, poller, stdin_fd=stdin_fd)

    old_attrs = termios.tcgetattr(stdin_fd)
    old_winch = signal.signal(signal.SIGWINCH, lambda *_: os.write(wake_w, b"W"))
    try:
        tty.setraw(stdin_fd)
        screen.enter()
        win.paint_history()
        for m in meters.values():
            m.poll()
        if poller is not None:
            poller.ensure_started()
        win.paint()
        while True:
            ready, _, _ = select.select([stdin_fd, wake_r], [], [], 1.0)
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
            if stdin_fd in ready:
                data = os.read(stdin_fd, 4096)
                if not data or not win.handle_input(data):
                    break
            if not ready:
                win.paint()                              # the bar's rate-limit figures refresh on their own clock
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
    write(f"tandem chat: session {session.tandem_id} · continue with `tandem chat`\r\n".encode())
    return 0
