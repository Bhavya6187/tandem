"""The window loop: composer actions reaching the dispatcher, live events
reaching the screen, prompts answered from the keyboard, and the real
select loop driven over a pty."""

import os
import select
import threading
import time
import tty

from conftest import claude_assistant, claude_user, write_line

from tandem.chat.composer import Composer
from tandem.chat.events import (ApprovalRequest, Idle, LimitsUpdate, QuestionRequest, TextDelta,
                                TurnFinished, TurnOutcome, TurnStarted)
from tandem.chat.render import Screen
from tandem.chat.window import Window, WindowAnswers, run_chat
from tandem.config import ChatConfig
from tandem.frame import StatusBar


class Out:
    def __init__(self): self.buf = bytearray()
    def __call__(self, b): self.buf += b
    def text(self): return self.buf.decode(errors="replace")


class StubDispatcher:
    def __init__(self):
        self.submitted, self.pumps, self.interrupts = [], 0, 0
        self.busy, self.default, self.note = False, "claude", ""
        self.pins = {}
    def submit(self, text): self.submitted.append(text); return self.note
    def pump(self): self.pumps += 1
    def interrupt(self): self.interrupts += 1
    def pin(self, harness): return self.pins.get(harness, "")
    def close(self): pass


def make_window(env, cfg=None, stdin_fd=None):
    cfg = cfg or ChatConfig()
    out = Out()
    screen = Screen(out, 24, 60, cfg, color=False)
    answers = WindowAnswers(lambda ev: None)
    d = StubDispatcher()
    bar = StatusBar(24, 60, "claude", ["codex"], hint="/claude /codex route")
    w = Window(env.session, env.store, cfg, screen, Composer(), d, answers, bar, {"limits": {}}, {},
               stdin_fd=stdin_fd)
    return w, d, out, answers


def readable(fd, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        if select.select([fd], [], [], 0.02)[0]:
            return True
        if time.monotonic() >= deadline:
            return False


def blocks_until_answered(answers, req, sink):
    """Start an approve() on a worker and assert it is still waiting: no
    leftover value may answer a request the user has not seen."""
    t = threading.Thread(target=lambda: sink.append(answers.approve(req)), daemon=True)
    t.start(); t.join(0.2)
    assert sink == []
    return t


def test_submit_and_notes(env_factory):
    env = env_factory(); w, d, out, _ = make_window(env)
    assert w.handle_input(b"hello\r") is True
    assert d.submitted == ["hello"]
    d.note = "queued → codex"; w.handle_input(b"more\r")
    assert "queued → codex" in out.text()
    d.note = "error: nope"; w.handle_input(b"/x\r")
    assert "error: nope" in out.text()


class TestWindowCommands:
    """`/quit` and `/status` are tandem's own and never reach a harness; every
    other leading `/word` is the harness's own slash command."""

    def test_quit_exits_the_loop(self, env_factory):
        env = env_factory(); w, d, out, _ = make_window(env)
        assert w.handle_input(b"/quit\r") is False
        assert d.submitted == []

    def test_quit_with_trailing_words_still_quits(self, env_factory):
        env = env_factory(); w, d, out, _ = make_window(env)
        assert w.handle_input(b"/quit now\r") is False
        assert d.submitted == []

    def test_a_word_starting_with_quit_is_the_harnesss(self, env_factory):
        env = env_factory(); w, d, out, _ = make_window(env)
        assert w.handle_input(b"/quitter\r") is True
        assert d.submitted == ["/quitter"]

    def test_status_prints_a_note_and_runs_nothing(self, env_factory):
        env = env_factory(); w, d, out, _ = make_window(env)
        assert w.handle_input(b"/status\r") is True
        assert d.submitted == []
        line = out.text()
        assert f"session {env.session.tandem_id}" in line
        assert "default claude" in line and "participants claude, codex" in line
        assert "pins:" not in line                       # none set

    def test_status_lists_the_model_pins(self, env_factory):
        env = env_factory(); w, d, out, _ = make_window(env)
        d.pins = {"codex": "gpt-5.5"}
        w.handle_input(b"/status\r")
        assert "pins: codex=gpt-5.5" in out.text()


def test_approval_round_trip(env_factory):
    env = env_factory(); w, d, out, answers = make_window(env)
    got = {}
    t = threading.Thread(target=lambda: got.__setitem__("choice", answers.approve(ApprovalRequest("command", "rm x"))))
    t.start(); time.sleep(0.05)
    w.handle_event(ApprovalRequest("command", "rm x"))          # the window sees the posted request
    assert w.composer.mode == "approval" and "[y]es [a]lways [n]o" in out.text()
    w.handle_input(b"y"); t.join(2)
    assert got["choice"] == "allow" and w.composer.mode == "prompt"


def test_esc_during_approval_denies_and_interrupts(env_factory):
    env = env_factory(); w, d, out, answers = make_window(env)
    got = {}
    t = threading.Thread(target=lambda: got.__setitem__("c", answers.approve(ApprovalRequest("command", "x")))); t.start()
    time.sleep(0.05); w.handle_event(ApprovalRequest("command", "x")); d.busy = True
    w.handle_input(b"\x1b"); t.join(2)
    assert got["c"] == "deny" and d.interrupts == 1


def test_ctrl_c_during_approval_denies_and_interrupts(env_factory):
    """A worker waiting on an approval is parked in the answers queue, not in
    a turn: interrupting it has to answer first — deny, exactly as Esc does —
    or the worker never wakes and every later prompt queues behind it."""
    env = env_factory(); w, d, out, answers = make_window(env)
    got = []
    # daemon: a regression parks this worker forever, and that must fail the
    # assertion below rather than wedge the interpreter at exit
    t = threading.Thread(target=lambda: got.append(answers.approve(ApprovalRequest("command", "rm x"))), daemon=True)
    t.start(); time.sleep(0.05)
    w.handle_event(ApprovalRequest("command", "rm x")); d.busy = True
    assert w.handle_input(b"\x03") is True                        # first press: interrupt, not quit
    t.join(2)
    assert got == ["deny"] and w.composer.mode == "prompt" and d.interrupts == 1

    second = []                                                   # the window is usable again
    t2 = blocks_until_answered(answers, ApprovalRequest("command", "rm y"), second)
    w.handle_event(ApprovalRequest("command", "rm y")); w.handle_input(b"n"); t2.join(2)
    assert second == ["deny"]


def test_esc_then_a_key_resolves_exactly_once(env_factory):
    """Esc denies and the window leaves answer mode; the key that lands right
    behind it — the user's, arriving before the runtime has posted anything
    new — must not resolve a second time. A stranded "allow" would silently
    approve the NEXT request before the user ever sees it."""
    env = env_factory(); w, d, out, answers = make_window(env)
    got = []
    t = threading.Thread(target=lambda: got.append(answers.approve(ApprovalRequest("command", "rm x"))))
    t.start(); time.sleep(0.05)
    w.handle_event(ApprovalRequest("command", "rm x")); d.busy = True
    w.handle_input(b"\x1b"); w.handle_input(b"y"); t.join(2)       # two reads, as a keyboard sends them
    assert got == ["deny"] and d.interrupts == 1
    assert w.composer.mode == "prompt" and w.composer.text == "y"  # ordinary text now, not an answer

    second = []
    t2 = blocks_until_answered(answers, ApprovalRequest("command", "rm y"), second)
    w.handle_event(ApprovalRequest("command", "rm y")); w.handle_input(b"n"); t2.join(2)
    assert second == ["deny"]


def test_a_repeated_approval_key_resolves_exactly_once(env_factory):
    env = env_factory(); w, d, out, answers = make_window(env)
    got = []
    t = threading.Thread(target=lambda: got.append(answers.approve(ApprovalRequest("command", "x")))); t.start()
    time.sleep(0.05); w.handle_event(ApprovalRequest("command", "x"))
    w.handle_input(b"y"); w.handle_input(b"y"); t.join(2)          # answered, then pressed again
    assert got == ["allow"] and w.composer.mode == "prompt"

    second = []
    t2 = blocks_until_answered(answers, ApprovalRequest("command", "z"), second)
    w.handle_event(ApprovalRequest("command", "z")); w.handle_input(b"n"); t2.join(2)
    assert second == ["deny"]


def test_prose_typed_before_the_row_answers_nothing(env_factory):
    """The loop drains live events — painting the approval row and entering
    answer mode — before it reads stdin in the same pass. Whatever was typed
    while the model worked is still in the tty buffer and arrives as the first
    chunk after the row: it must not answer a request the user never saw."""
    env = env_factory(); w, d, out, answers = make_window(env)
    got = []
    t = blocks_until_answered(answers, ApprovalRequest("command", "rm -rf ~/"), got)
    w.handle_event(ApprovalRequest("command", "rm -rf ~/"))
    assert w.handle_input(b"and then fix the tests") is True
    t.join(0.2)
    assert got == [] and w.composer.mode == "approval"
    w.handle_input(b"n"); t.join(2)                            # a real keypress does answer
    assert got == ["deny"]


def test_entering_answer_mode_drops_what_was_typed_before_the_row(env_factory):
    """Belt to the first-character rule: the bytes never reach the composer at
    all. Flushed before the row is painted, so nothing typed after it is lost."""
    env = env_factory()
    master, slave = os.openpty()
    try:
        tty.setraw(slave)                                      # as run_chat does
        w, d, out, _ = make_window(env, stdin_fd=slave)
        os.write(master, b"and then fix the tests")
        assert readable(slave, 2.0)                            # in the tty buffer
        w.handle_event(ApprovalRequest("command", "rm -rf ~/"))
        assert not readable(slave, 0.1)                        # dropped with the row
        assert w.composer.mode == "approval"
    finally:
        os.close(master); os.close(slave)


def test_a_window_without_a_tty_still_enters_answer_mode(env_factory):
    env = env_factory(); w, d, out, _ = make_window(env)       # stdin_fd None
    w.handle_event(ApprovalRequest("command", "rm x"))
    assert w.composer.mode == "approval"


def test_sigwinch_repaints_the_bottom_block(env_factory):
    """A resize recomputes the scroll region; without a repaint the bar and
    composer stay wherever the old geometry left them until the next event."""
    env = env_factory(); w, d, out, _ = make_window(env)
    out.buf.clear()
    w.resize(30, 100)
    assert w.screen.rows == 30 and w.bar.rows == 30 and w.bar.cols == 100
    assert "\x1b[29;1H" in out.text()                          # the bar, at its new row


def test_window_answers_never_hands_over_a_leftover_value():
    """Belt and braces to the window's drop: whatever the cause, a value that
    predates the request must not answer it."""
    posted = []
    answers = WindowAnswers(posted.append)
    answers.resolve("allow")                                       # left over from nobody
    got = []
    req = ApprovalRequest("command", "rm -rf /")
    t = blocks_until_answered(answers, req, got)
    assert posted == [req]                                         # the user does see the request
    answers.resolve("deny"); t.join(2)
    assert got == ["deny"]


def test_question_by_digit(env_factory):
    env = env_factory(); w, d, out, answers = make_window(env)
    got = {}
    t = threading.Thread(target=lambda: got.__setitem__("a", answers.answer(QuestionRequest("Which?", ("red", "blue"))))); t.start()
    time.sleep(0.05); w.handle_event(QuestionRequest("Which?", ("red", "blue")))
    w.handle_input(b"2"); t.join(2)
    assert got["a"] == "blue"


def test_ctrl_c_ladder(env_factory):
    env = env_factory(); w, d, out, _ = make_window(env)
    d.busy = True
    assert w.handle_input(b"\x03") is True and d.interrupts == 1
    assert w.handle_input(b"\x03") is False                     # second within 2s quits
    w._ctrlc_at = 0.0
    assert w.handle_input(b"\x03") is True                      # a stale first press does not quit


def test_events_paint_and_idle_pumps(env_factory):
    env = env_factory(); w, d, out, _ = make_window(env)
    w.handle_event(TurnStarted("codex", "", "go")); w.handle_event(TextDelta("hi")); w.handle_event(TurnFinished("completed", "u"))
    w.handle_event(LimitsUpdate("codex", "5h 3%")); w.handle_event(Idle())
    # the screen runs a raw tty, so every newline it writes is CRLF
    assert "you → codex  go" in out.text() and "codex\r\nhi" in out.text()
    assert w.usage_state["limits"]["codex"] == "5h 3%" and d.pumps == 1
    assert "5h 3%" in w.bar_line()


def test_history_paints_the_default_harness_transcript(env_factory):
    env = env_factory(active="claude")
    write_line(env.claude_shadow, claude_user("fix the tests", uuid="u9"))
    write_line(env.claude_shadow, claude_assistant([{"type": "text", "text": "All green."}], uuid="a9"))
    w, d, out, _ = make_window(env)
    w.paint_history()
    t = out.text()
    assert "fix the tests" in t and "All green." in t


def test_history_turns_zero_paints_nothing(env_factory):
    """`history_turns = 0` means none — the trim indexes starts[-N], and
    starts[-0] is starts[0], i.e. the whole transcript."""
    env = env_factory(active="claude")
    write_line(env.claude_shadow, claude_user("fix the tests", uuid="u9"))
    write_line(env.claude_shadow, claude_assistant([{"type": "text", "text": "All green."}], uuid="a9"))
    w, d, out, _ = make_window(env, cfg=ChatConfig(history_turns=0))
    w.paint_history()
    assert out.text() == ""


class EchoRuntime:
    harness = "claude"
    def run_turn(self, session, native_id, prompt, model, emit, answers):
        emit(TextDelta(f"echo:{prompt}")); emit(TurnFinished("completed", "")); return TurnOutcome("completed")
    def interrupt(self): pass
    def close(self): pass


def hermetic_frame():
    """No rate-limit poller: the suite makes no network or keychain calls."""
    with open(os.path.join(os.environ["TANDEM_HOME"], "config.toml"), "w") as f:
        f.write("[frame]\nrate_limits = false\n")


def drive_chat(env) -> tuple[int, str]:
    """Run the real loop over a pty: wait for the composer, submit `ping`,
    wait for the echo, then quit with two Ctrl-Cs. Returns (exit code,
    everything the window painted). The quit is sent even when the echo never
    arrives, so a broken drain fails the assertion instead of hanging."""
    master, slave = os.openpty()
    captured = bytearray()

    def pull() -> bool:
        """One blocking read; False once the window's end of the pty is gone."""
        try:
            chunk = os.read(master, 4096)
        except OSError:
            return False
        if not chunk:
            return False
        captured.extend(chunk)
        return True

    def driver():
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and b"> " not in captured:
            if not pull():
                return
        os.write(master, b"ping\r")
        while time.monotonic() < deadline and b"echo:ping" not in captured:
            if not pull():
                return
        os.write(master, b"\x03\x03")
        # a fresh deadline: the window still has its teardown to write, and a
        # driver that stopped reading would wedge it on a full pty buffer
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if not pull():
                return

    t = threading.Thread(target=driver, daemon=True); t.start()
    try:
        code = run_chat(env.session, env.store, ChatConfig(), stdin_fd=slave, out_fd=slave,
                        runtimes={"claude": EchoRuntime(), "codex": EchoRuntime()})
    finally:
        os.close(slave)                               # the driver's read then fails and ends
        t.join(5)
        os.close(master)
    assert not t.is_alive()
    return code, captured.decode(errors="replace")


def test_run_chat_on_a_pty(env_factory):
    """The real loop: raw mode, a submitted prompt reaching a fake runtime, Ctrl-C twice to quit."""
    env = env_factory()
    hermetic_frame()
    code, text = drive_chat(env)
    assert code == 0
    assert "echo:ping" in text and "\x1b[r" in text


def test_a_flush_does_not_leave_the_loop_blocked_on_a_dead_read(env_factory, monkeypatch):
    """The loop decides stdin is readable from the select at the top of the
    pass, then the event drain below it flushes the tty — an approval row
    discards whatever was typed before it existed. The fd is blocking with
    VMIN=1 (raw mode), so an unconditional read then waits for a keypress: no
    1 s repaint, and every event queued after the approval sits unpainted
    until the user touches the keyboard.

    Made deterministic the way the lost-wake-byte test is: the approval's wake
    byte is dropped, so the pass that drains it is woken by the typed-ahead
    bytes alone — exactly the interleaving the flush was added for."""
    env = env_factory()
    hermetic_frame()
    master, slave = os.openpty()
    captured = bytearray()
    pipes: list[tuple[int, int]] = []
    real_pipe, real_write = os.pipe, os.write
    swallow_wake = {"on": True}

    def spy_pipe():
        fds = real_pipe()
        pipes.append(fds)
        return fds

    def lossy_write(fd, data):
        if swallow_wake["on"] and pipes and fd == pipes[0][1] and bytes(data) == b"E":
            return len(data)                  # the window never learns of this event
        return real_write(fd, data)

    monkeypatch.setattr(os, "pipe", spy_pipe)
    monkeypatch.setattr(os, "write", lossy_write)

    class AsksThenStreams:
        harness = "claude"

        def run_turn(self, session, native_id, prompt, model, emit, answers):
            emit(ApprovalRequest("command", "rm -rf ~/"))   # queued, no wake byte
            real_write(master, b"and then fix the tests")   # …and now stdin is readable
            time.sleep(0.5)                                 # the pass above has run
            swallow_wake["on"] = False
            emit(TextDelta("late-event"))                   # owed a paint, with no keypress
            emit(TurnFinished("completed", ""))
            return TurnOutcome("completed")

        def interrupt(self): pass

        def close(self): pass

    seen = {}

    def pull() -> bool:
        """One read, but never a blocking one: this driver's deadlines have to
        stay enforceable while the window is painting nothing at all."""
        try:
            if not select.select([master], [], [], 0.05)[0]:
                return True
            chunk = os.read(master, 4096)
        except OSError:
            return False
        if not chunk:
            return False
        captured.extend(chunk)
        return True

    def driver():
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and b"> " not in captured:
            if not pull():
                return
        real_write(master, b"ping\r")
        deadline = time.monotonic() + 3                     # the 1 s tick, with room
        while time.monotonic() < deadline and b"late-event" not in captured:
            if not pull():
                return
        seen["late"] = b"late-event" in captured
        real_write(master, b"\x03\x03")                     # also unwedges a dead read
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if not pull():
                return

    t = threading.Thread(target=driver, daemon=True); t.start()
    try:
        code = run_chat(env.session, env.store, ChatConfig(), stdin_fd=slave, out_fd=slave,
                        runtimes={"claude": AsksThenStreams(), "codex": EchoRuntime()})
    finally:
        os.close(slave)
        t.join(5)
        os.close(master)
    assert code == 0
    assert seen.get("late") is True, "the loop was blocked in os.read after the flush"


def test_run_chat_drains_events_whose_wake_byte_was_lost(env_factory, monkeypatch):
    """`post` swallows a failed wake-pipe write, so the select timeout has to
    drain the queue too — otherwise that event sits unpainted until some later
    event's byte gets through."""
    env = env_factory()
    hermetic_frame()
    pipes: list[tuple[int, int]] = []
    real_pipe, real_write = os.pipe, os.write

    def spy_pipe():
        fds = real_pipe()
        pipes.append(fds)
        return fds

    def lossy_write(fd, data):
        # only the window's own wake byte; every other write goes through
        if pipes and fd == pipes[0][1] and bytes(data) == b"E":
            raise OSError("wake byte lost")
        return real_write(fd, data)

    monkeypatch.setattr(os, "pipe", spy_pipe)
    monkeypatch.setattr(os, "write", lossy_write)
    code, text = drive_chat(env)
    assert code == 0
    assert "echo:ping" in text
