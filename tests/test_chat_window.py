"""The window loop: composer actions reaching the dispatcher, live events
reaching the screen, prompts answered from the keyboard, and the real
select loop driven over a pty."""

import os
import threading
import time

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
    def submit(self, text): self.submitted.append(text); return self.note
    def pump(self): self.pumps += 1
    def interrupt(self): self.interrupts += 1
    def close(self): pass


def make_window(env, cfg=None):
    cfg = cfg or ChatConfig()
    out = Out()
    screen = Screen(out, 24, 60, cfg, color=False)
    answers = WindowAnswers(lambda ev: None)
    d = StubDispatcher()
    bar = StatusBar(24, 60, "claude", ["codex"], hint="/claude /codex route")
    w = Window(env.session, env.store, cfg, screen, Composer(), d, answers, bar, {"limits": {}}, {})
    return w, d, out, answers


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


def test_esc_then_a_key_in_one_chunk_resolves_exactly_once(env_factory):
    """One read can carry two answer keys: the composer only leaves answer
    mode when the window says so, so `\\x1by` yields Cancel + Answer. The
    second must be dropped — resolving twice strands an "allow" that would
    silently approve the NEXT request before the user ever sees it."""
    env = env_factory(); w, d, out, answers = make_window(env)
    got = []
    t = threading.Thread(target=lambda: got.append(answers.approve(ApprovalRequest("command", "rm x"))))
    t.start(); time.sleep(0.05)
    w.handle_event(ApprovalRequest("command", "rm x")); d.busy = True
    w.handle_input(b"\x1by"); t.join(2)
    assert got == ["deny"] and d.interrupts == 1
    assert w.composer.mode == "prompt" and w.composer.text == ""   # the `y` is not typed into the prompt

    second = []
    t2 = blocks_until_answered(answers, ApprovalRequest("command", "rm y"), second)
    w.handle_event(ApprovalRequest("command", "rm y")); w.handle_input(b"n"); t2.join(2)
    assert second == ["deny"]


def test_a_repeated_approval_key_in_one_chunk_resolves_exactly_once(env_factory):
    env = env_factory(); w, d, out, answers = make_window(env)
    got = []
    t = threading.Thread(target=lambda: got.append(answers.approve(ApprovalRequest("command", "x")))); t.start()
    time.sleep(0.05); w.handle_event(ApprovalRequest("command", "x"))
    w.handle_input(b"y\x7fy"); t.join(2)                           # Answer, backspace, Answer
    assert got == ["allow"] and w.composer.mode == "prompt"

    second = []
    t2 = blocks_until_answered(answers, ApprovalRequest("command", "z"), second)
    w.handle_event(ApprovalRequest("command", "z")); w.handle_input(b"n"); t2.join(2)
    assert second == ["deny"]


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
