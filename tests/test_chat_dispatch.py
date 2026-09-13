"""One turn at a time: routing, pins, the queue, and the one-off run's
bookkeeping wrapped around a streaming runtime."""

import json
import threading
import time

import pytest

from tandem.chat.dispatch import Dispatcher
from tandem.chat.events import Failure, Idle, TextDelta, TurnFinished, TurnOutcome, TurnStarted
from tandem.util import read_jsonl

from conftest import claude_assistant, claude_user, codex_turn, write_line


class FakeRuntime:
    """Emits one delta, appends a native turn to the harness's own file (so
    sync has something to translate), and returns the scripted outcome."""

    def __init__(self, harness, env, *, block=None, fresh_id=None):
        self.harness = harness
        self.env = env
        self.calls = []
        self.block = block
        self.fresh_id = fresh_id
        self.interrupts = 0

    def run_turn(self, session, native_id, prompt, model, emit, answers):
        self.calls.append((native_id, prompt, model))
        if self.block is not None:
            self.block.wait(5)
        emit(TextDelta(f"{self.harness} says hi"))
        if self.harness == "claude":
            write_line(self.env.claude_shadow, claude_user(prompt, uuid=f"u-{len(self.calls)}"))
            write_line(
                self.env.claude_shadow,
                claude_assistant(
                    [{"type": "text", "text": f"claude did {prompt}"}],
                    uuid=f"a-{len(self.calls)}",
                ),
            )
        elif self.harness == "codex" and native_id:
            for obj in codex_turn(prompt, f"codex did {prompt}"):
                write_line(self.env.codex_shadow, obj)
        emit(TurnFinished("completed", "1 turn"))
        return TurnOutcome("completed", native_id=self.fresh_id if native_id is None else None)

    def interrupt(self):
        self.interrupts += 1
        if self.block is not None:
            self.block.set()

    def close(self):
        pass


class Answers:
    def approve(self, req):
        return "allow"

    def answer(self, req):
        return ""


def wait_idle(events, count=1, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if sum(isinstance(e, Idle) for e in events) >= count:
            return
        time.sleep(0.01)
    raise AssertionError(f"no Idle #{count} within {timeout}s: {[type(e).__name__ for e in events]}")


@pytest.fixture
def setup(env_factory):
    env = env_factory(active="claude")
    events = []
    runtimes = {"claude": FakeRuntime("claude", env), "codex": FakeRuntime("codex", env)}
    d = Dispatcher(env.store, env.session, runtimes, events.append, Answers())
    try:
        yield env, d, runtimes, events
    finally:
        d.close()


def test_plain_prompt_runs_on_the_default_and_syncs_outward(setup):
    env, d, rts, events = setup
    assert d.submit("hello there") == ""
    wait_idle(events)
    assert rts["claude"].calls == [(env.session.native_id("claude"), "hello there", "")]
    assert events[0] == TurnStarted("claude", "", "hello there")
    assert [type(e).__name__ for e in events] == ["TurnStarted", "TextDelta", "TurnFinished", "Idle"]
    contents = [json.dumps(e) for e in read_jsonl(env.codex_shadow)]
    assert any("[via claude-code] claude did hello there" in c for c in contents)
    assert env.store.get_session(env.session.tandem_id).active == "claude"


def test_route_runs_there_and_becomes_the_default(setup):
    env, d, rts, events = setup
    assert d.submit("/codex review it") == ""
    wait_idle(events)
    assert rts["codex"].calls == [(env.session.native_id("codex"), "review it", "")]
    assert env.store.get_session(env.session.tandem_id).active == "codex"
    contents = [json.dumps(e) for e in read_jsonl(env.claude_shadow)]
    assert any("[via codex] codex did review it" in c for c in contents)
    d.submit("and again")
    wait_idle(events, 2)
    assert rts["codex"].calls[-1] == (env.session.native_id("codex"), "and again", "")
    assert rts["claude"].calls == []


def test_bare_route_switches_the_default_without_a_turn(setup):
    env, d, rts, events = setup
    assert d.submit("/codex") == "default → codex"
    assert d.default == "codex" and not d.busy
    assert rts["codex"].calls == [] and events == []


def test_model_pin_is_sticky_per_harness(setup, monkeypatch):
    from tandem import promptroute

    monkeypatch.setattr(promptroute.modelcat, "load_catalog", lambda: None)
    env, d, rts, events = setup
    d.submit("/codex:gpt-5.5 go")
    wait_idle(events, 1)
    d.submit("/codex again")
    wait_idle(events, 2)
    d.submit("/claude:haiku hi")
    wait_idle(events, 3)
    d.submit("/codex:default last")
    wait_idle(events, 4)
    assert [c[2] for c in rts["codex"].calls] == ["gpt-5.5", "gpt-5.5", ""]
    assert rts["claude"].calls[0][2] == "haiku"
    assert d.submit("/codex") == "default → codex"
    assert d.submit("/claude") == "default → claude · haiku"


def test_prompts_queue_while_busy(setup):
    env, d, rts, events = setup
    gate = threading.Event()
    rts["claude"].block = gate
    assert d.submit("first") == ""
    assert d.submit("/codex second") == "queued → codex"
    assert rts["codex"].calls == []
    gate.set()
    wait_idle(events, 1)
    d.pump()
    wait_idle(events, 2)
    assert rts["codex"].calls == [(env.session.native_id("codex"), "second", "")]
    assert d.default == "codex"


def test_pump_from_inside_the_idle_emit_starts_the_queued_turn(env_factory):
    """The window pumps on Idle, and Idle is emitted from the worker thread
    before it returns. Even at its most synchronous — pump() called straight
    out of the emit callback — the queue must move, so `busy` cannot be the
    worker's liveness."""
    env = env_factory(active="claude")
    events = []
    gate = threading.Event()
    rts = {"claude": FakeRuntime("claude", env, block=gate),
           "codex": FakeRuntime("codex", env)}
    holder = {}

    def emit(event):
        events.append(event)
        if isinstance(event, Idle):
            holder["dispatcher"].pump()

    d = Dispatcher(env.store, env.session, rts, emit, Answers())
    holder["dispatcher"] = d
    try:
        assert d.submit("first") == ""
        assert d.submit("/codex second") == "queued → codex"
        gate.set()
        wait_idle(events, 2)                       # no pump() from the test
    finally:
        d.close()
    assert rts["claude"].calls == [(env.session.native_id("claude"), "first", "")]
    assert rts["codex"].calls == [(env.session.native_id("codex"), "second", "")]
    assert [e.harness for e in events if isinstance(e, TurnStarted)] == ["claude", "codex"]
    assert not d.busy and not d.queue

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and any(
        t.name == "tandem-chat-turn" for t in threading.enumerate()
    ):
        time.sleep(0.01)
    assert [t.name for t in threading.enumerate() if t.name == "tandem-chat-turn"] == []


def test_route_error_is_a_note_and_runs_nothing(setup):
    env, d, rts, events = setup
    note = d.submit("/opencode do it")
    assert note.startswith("error: opencode is not a participant")
    assert not d.busy and events == []


def test_invalid_transcript_fails_before_the_runtime(setup):
    env, d, rts, events = setup
    env.claude_shadow.write_text("{not json\n")
    d.submit("hello")
    wait_idle(events)
    assert rts["claude"].calls == []
    kinds = [type(e).__name__ for e in events]
    assert kinds == ["TurnStarted", "Failure", "TurnFinished", "Idle"]
    assert events[2] == TurnFinished("failed", "")


def test_fresh_codex_id_is_adopted(env_factory):
    env = env_factory(active="claude")
    session = env.store.create_session(
        env.cwd, "claude", ["claude", "codex"],
        {"claude": env.session.native_id("claude"), "codex": None},
    )
    events = []
    rts = {"claude": FakeRuntime("claude", env),
           "codex": FakeRuntime("codex", env, fresh_id="thread-new")}
    d = Dispatcher(env.store, session, rts, events.append, Answers())
    try:
        d.submit("/codex start")
        wait_idle(events)
    finally:
        d.close()
    assert rts["codex"].calls == [(None, "start", "")]
    assert env.store.get_session(session.tandem_id).native_id("codex") == "thread-new"
    assert env.store.get_cursor(session.tandem_id, "codex", "claude").byte_offset == 0


def test_interrupt_reaches_the_running_runtime(setup):
    env, d, rts, events = setup
    gate = threading.Event()
    rts["claude"].block = gate
    d.submit("slow")
    time.sleep(0.05)
    d.interrupt()
    wait_idle(events)
    assert rts["claude"].interrupts == 1
