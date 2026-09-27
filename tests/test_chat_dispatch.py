"""One turn at a time: routing, pins, the queue, and the one-off run's
bookkeeping wrapped around a streaming runtime."""

import json
import threading
import time
from collections import deque

import pytest

from tandem.chat import dispatch
from tandem.chat.dispatch import Dispatcher
from tandem.chat.events import (Failure, Idle, Notice, TextDelta, ToolStarted, TurnFinished, TurnOutcome,
                               TurnStarted)
from tandem.harness import get_adapter
from tandem.sync import SyncSetupError
from tandem.util import read_jsonl

from conftest import claude_assistant, claude_user, codex_turn, shadow_texts, write_line


def claude_texts(path):
    """Plain text of every claude conversation entry, in file order."""
    out = []
    for e in read_jsonl(path):
        content = (e.get("message") or {}).get("content")
        if isinstance(content, str):
            out.append(content)
        elif isinstance(content, list):
            out += [b.get("text", "") for b in content
                    if isinstance(b, dict) and b.get("type") == "text"]
    return out


class FakeRuntime:
    """Emits one delta, appends a native turn to the harness's own file (so
    sync has something to translate), and returns the scripted outcome.

    half_turn=True records only the user half and fails the turn — the shape
    a model call that errors out (a 401, say) leaves in the transcript: the
    prompt is there, no reply ever follows it."""

    def __init__(self, harness, env, *, block=None, fresh_id=None, half_turn=False, fail_models=False,
                 review_reply='{"verdict": "clean"}', paths=()):
        self.harness = harness
        self.env = env
        self.calls = []
        self.block = block
        self.fresh_id = fresh_id
        self.half_turn = half_turn
        self.interrupts = 0
        self.models = None if fail_models else [f"{harness}-a  first", f"{harness}-b  second"]
        self.harness_commands = []
        self.review_reply = review_reply
        self.last_answers = None
        self.paths = paths      # files a non-review turn reports editing (what a review is told about)

    def list_models(self, session):
        if self.models is None:
            raise RuntimeError("no catalog")
        return list(self.models)

    def run_turn(self, session, native_id, prompt, model, emit, answers, command="", review=None):
        call = (native_id, prompt, model)
        if command:
            call += (command,)
        if review is not None:
            call += (review,)
        self.calls.append(call)
        self.last_answers = answers
        if self.block is not None:
            self.block.wait(5)
        reply = self.review_reply if review is not None else f"{self.harness} did {prompt}"
        if self.paths and review is None:
            emit(ToolStarted(f"t-{len(self.calls)}", "Edit", ", ".join(self.paths), paths=tuple(self.paths)))
        emit(TextDelta(reply if review is not None else f"{self.harness} says hi"))
        if self.harness == "claude":
            write_line(self.env.claude_shadow, claude_user(prompt, uuid=f"u-{len(self.calls)}"))
            if not self.half_turn:
                write_line(
                    self.env.claude_shadow,
                    claude_assistant([{"type": "text", "text": reply}], uuid=f"a-{len(self.calls)}"),
                )
        elif self.harness == "codex" and native_id:
            entries = codex_turn(prompt, reply)
            for obj in entries[:2] if self.half_turn else entries:
                write_line(self.env.codex_shadow, obj)
        if self.half_turn:
            emit(TurnFinished("failed", ""))
            return TurnOutcome("failed", error="boom")
        emit(TurnFinished("completed", "1 turn"))
        return TurnOutcome("completed", native_id=self.fresh_id if native_id is None else None)

    def interrupt(self):
        self.interrupts += 1
        if self.block is not None:
            self.block.set()

    def close(self):
        pass


class CountingRuntime:
    """Records the prompts it was handed, and how many turns were ever live
    at once."""

    def __init__(self, harness, hold=0.0):
        self.harness = harness
        self.hold = hold
        self.calls = []
        self.live = 0
        self.peak = 0
        self._lock = threading.Lock()

    def run_turn(self, session, native_id, prompt, model, emit, answers, command=""):
        with self._lock:
            self.calls.append(prompt)
            self.live += 1
            self.peak = max(self.peak, self.live)
        time.sleep(self.hold)
        with self._lock:
            self.live -= 1
        emit(TurnFinished("completed"))
        return TurnOutcome("completed")

    def interrupt(self):
        pass

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


def test_a_bare_route_typed_during_a_turn_outlives_that_turn(setup):
    """`/codex` while claude is still working is the user's later word: the
    turn's completion makes its harness the default only when nothing was
    said since it started."""
    env, d, rts, events = setup
    gate = threading.Event()
    rts["claude"].block = gate
    assert d.submit("first") == ""
    deadline = time.monotonic() + 5.0
    while not rts["claude"].calls and time.monotonic() < deadline:
        time.sleep(0.01)
    assert d.submit("/codex") == "default → codex"
    gate.set()
    wait_idle(events)
    assert d.default == "codex"
    assert env.store.get_session(env.session.tandem_id).active == "codex"
    d.submit("second")
    wait_idle(events, 2)
    assert rts["codex"].calls[-1][1] == "second"
    assert [c[1] for c in rts["claude"].calls] == ["first"]


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


def no_turn_is_left_unfinished(events):
    """Every TurnStarted owes the renderer exactly one terminal TurnFinished."""
    return (sum(isinstance(e, TurnStarted) for e in events)
            == sum(isinstance(e, TurnFinished) for e in events))


def test_a_wedged_drain_before_the_runtime_still_finishes_the_turn(setup, monkeypatch):
    """prepare_turn can raise (SyncEngine rejects a participant with no shadow
    file). The runtime never runs, so nobody else will emit the terminal
    TurnFinished the renderer is waiting for."""
    def boom(store, session, target):
        raise SyncSetupError("shadow transcript missing for opencode")

    monkeypatch.setattr(dispatch.ops, "prepare_turn", boom)
    env, d, rts, events = setup
    d.submit("hello")
    wait_idle(events)
    assert rts["claude"].calls == []
    assert [type(e).__name__ for e in events] == ["TurnStarted", "Failure", "TurnFinished", "Idle"]
    assert events[1] == Failure("sync: shadow transcript missing for opencode")
    assert events[2] == TurnFinished("failed", "")
    assert no_turn_is_left_unfinished(events)


def test_a_failure_after_the_runtime_ran_adds_no_second_turn_finished(setup, monkeypatch):
    """The outward sync can fail once the turn itself is over. The runtime has
    already emitted its terminal event; a second one would double-close it."""
    def boom(store, session, target, **kw):
        raise SyncSetupError("cursor is wedged")

    monkeypatch.setattr(dispatch.ops, "sync_after_turn", boom)
    env, d, rts, events = setup
    d.submit("hello")
    wait_idle(events)
    assert rts["claude"].calls != []
    assert [type(e).__name__ for e in events] == [
        "TurnStarted", "TextDelta", "TurnFinished", "Failure", "Idle"]
    assert [e for e in events if isinstance(e, TurnFinished)] == [TurnFinished("completed", "1 turn")]
    assert events[3] == Failure("sync: cursor is wedged")


def test_submit_and_pump_from_two_threads_start_one_turn_each(env_factory):
    """The start-or-queue decision is the one place two threads meet: the
    window submits on the main thread while a finishing turn pumps on its own.
    Unguarded, both can start a turn in the same instant (two workers on one
    pair of cursors) or both popleft an empty queue."""
    env = env_factory(active="claude")
    events = []
    runtime = CountingRuntime("claude")
    holder = {}
    errors = []

    def emit(event):
        events.append(event)
        if isinstance(event, Idle):
            holder["dispatcher"].pump()          # the window's own pump, on the worker

    d = Dispatcher(env.store, env.session, {"claude": runtime, "codex": runtime},
                   emit, Answers())
    holder["dispatcher"] = d
    prompts = [f"p{i}" for i in range(200)]

    def submitter():
        for prompt in prompts:
            try:
                d.submit(prompt)
            except Exception as exc:
                errors.append(f"submit: {exc!r}")
            time.sleep(0)                        # yield, so submits land mid-turn

    def pumper():
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            try:
                d.pump()
            except Exception as exc:
                errors.append(f"pump: {exc!r}")
                return
            if len(runtime.calls) == len(prompts) and not d.busy and not d.queue:
                return
            time.sleep(0.001)                    # hammer, but do not starve the worker

    threads = [threading.Thread(target=submitter), threading.Thread(target=pumper)]
    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        assert not any(t.is_alive() for t in threads)
    finally:
        d.close()
    assert errors == []
    assert runtime.calls == prompts               # every one, once, in order
    assert runtime.peak == 1                      # never two turns at once
    assert no_turn_is_left_unfinished(events)

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and any(
        t.name == "tandem-chat-turn" for t in threading.enumerate()
    ):
        time.sleep(0.01)
    assert [t.name for t in threading.enumerate() if t.name == "tandem-chat-turn"] == []


def test_a_submit_inside_the_start_window_cannot_double_start(env_factory):
    """The race the hammer above can only stumble on, made deterministic: a
    pump has taken the next turn off the queue but has not marked itself
    running yet, and a submit arrives in exactly that gap. Held open by a
    sleep inside _start, it is the width of that sleep instead of two
    bytecodes — so an unguarded decision starts the third prompt on a second
    worker thread, ahead of the second."""
    env = env_factory(active="claude")
    arm = threading.Event()
    starts = []

    class SlowStartDispatcher(Dispatcher):
        def _start(self, item):
            starts.append(item.prompt)
            if len(starts) == 2:                 # the pump's start, mid-gap
                arm.set()                        # the third submit may go now
                time.sleep(0.05)
            super()._start(item)

    events = []
    runtime = CountingRuntime("claude", hold=0.1)
    holder = {}

    def emit(event):
        events.append(event)
        if isinstance(event, Idle):
            holder["dispatcher"].pump()

    d = SlowStartDispatcher(env.store, env.session, {"claude": runtime, "codex": runtime},
                            emit, Answers())
    holder["dispatcher"] = d

    def latecomer():
        arm.wait(5)
        d.submit("third")

    thread = threading.Thread(target=latecomer)
    try:
        thread.start()
        assert d.submit("first") == ""
        assert d.submit("second") == "queued → claude"
        wait_idle(events, 3, timeout=10.0)
        thread.join(5)
        assert not thread.is_alive()
    finally:
        d.close()
    assert runtime.calls == ["first", "second", "third"]
    assert runtime.peak == 1


def test_a_submit_between_a_turn_and_its_pump_waits_its_turn(env_factory):
    """The other gap: the window renders the finished turn before it pumps, so
    there is a moment with nothing running and a prompt still queued. A submit
    landing there must join the back of the queue — starting it would run it
    ahead of the prompt typed before it."""
    env = env_factory(active="claude")
    events = []
    runtime = CountingRuntime("claude", hold=0.05)
    holder = {}
    arm = threading.Event()

    def emit(event):
        events.append(event)
        if isinstance(event, Idle):
            arm.set()                            # turn over, pump not run yet
            time.sleep(0.05)                     # the window, repainting
            holder["dispatcher"].pump()

    d = Dispatcher(env.store, env.session, {"claude": runtime, "codex": runtime},
                   emit, Answers())
    holder["dispatcher"] = d

    def latecomer():
        arm.wait(5)
        d.submit("third")

    thread = threading.Thread(target=latecomer)
    try:
        thread.start()
        assert d.submit("first") == ""
        assert d.submit("second") == "queued → claude"
        wait_idle(events, 3, timeout=10.0)
        thread.join(5)
        assert not thread.is_alive()
    finally:
        d.close()
    assert runtime.calls == ["first", "second", "third"]
    assert runtime.peak == 1


def test_set_cfg_reaches_every_runtime(setup):
    """The window's `/skip-permissions`: each runtime reads its cfg as a turn
    starts, so the next turn — on any harness — runs under the new one."""
    env, d, runtimes, events = setup
    cfg = object()
    d.set_cfg(cfg)
    assert all(rt.cfg is cfg for rt in runtimes.values())


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


def test_a_failed_codex_turn_closes_its_user_message_in_the_claude_shadow(env_factory):
    """A model call that dies mid-turn (a 401) leaves the prompt in the
    harness's own transcript with no reply. Synced outward as-is it leaves
    every shadow ending on a user message — which opencode's dry-resume
    check rejects outright, wedging every later turn in the session."""
    env = env_factory(active="claude")
    events = []
    rts = {"claude": FakeRuntime("claude", env),
           "codex": FakeRuntime("codex", env, half_turn=True)}
    d = Dispatcher(env.store, env.session, rts, events.append, Answers())
    try:
        assert d.submit("/codex break it") == ""
        wait_idle(events)

        texts = claude_texts(env.claude_shadow)
        assert texts[-2] == "[via codex] break it"
        assert texts[-1] == "[tandem] the turn on codex ended: failed: boom"
        sid = env.session.native_id("claude")
        assert get_adapter("claude").validate_transcript(env.claude_shadow, sid) == []
        # the note went in through the engine, so echo suppression covers it:
        # claude's outgoing cursor sits past it and it never bounces back
        cursor = env.store.get_cursor(env.session.tandem_id, "claude", "codex")
        assert cursor.byte_offset == env.claude_shadow.stat().st_size

        # and the session is not wedged: the next turn's pre-turn validation
        # passes and it runs
        rts["codex"].half_turn = False
        assert d.submit("/codex again") == ""
        wait_idle(events, 2)
    finally:
        d.close()
    assert rts["codex"].calls[-1][1] == "again"
    assert [e for e in events if isinstance(e, Failure)] == []
    assert "[via codex] codex did again" in claude_texts(env.claude_shadow)


def test_a_failed_claude_turn_closes_its_user_message_in_the_codex_shadow(env_factory):
    env = env_factory(active="claude")
    events = []
    rts = {"claude": FakeRuntime("claude", env, half_turn=True),
           "codex": FakeRuntime("codex", env)}
    d = Dispatcher(env.store, env.session, rts, events.append, Answers())
    try:
        assert d.submit("break it") == ""
        wait_idle(events)
    finally:
        d.close()
    texts = shadow_texts(env.codex_shadow)
    assert texts[-2] == "[via claude-code] break it"
    assert texts[-1] == "[tandem] the turn on claude ended: failed: boom"
    sid = env.session.native_id("codex")
    assert get_adapter("codex").validate_transcript(env.codex_shadow, sid) == []


def test_a_completed_turn_gets_no_closing_note(setup):
    env, d, rts, events = setup
    d.submit("hello there")
    wait_idle(events)
    assert not any("the turn on" in t for t in shadow_texts(env.codex_shadow))


def test_a_quarantined_entry_is_reported(setup, monkeypatch):
    """An entry the converter cannot translate is quarantined and replaced by a
    placeholder — the drain does not fail, so nothing else says it happened."""
    real = dispatch.ops.sync_after_turn

    def bumping(store, session, target, **kw):
        cursor = store.get_cursor(session.tandem_id, target, "codex")
        cursor.failed_turns += 2
        store.save_cursor(cursor)
        return real(store, session, target, **kw)

    monkeypatch.setattr(dispatch.ops, "sync_after_turn", bumping)
    env, d, rts, events = setup
    d.submit("hello")
    wait_idle(events)
    notes = [e.message for e in events if isinstance(e, Failure)]
    assert notes == ["2 entries quarantined while syncing claude → codex; "
                     "see `tandem doctor`"]


def test_a_clean_drain_reports_no_quarantine(setup):
    env, d, rts, events = setup
    d.submit("hello")
    wait_idle(events)
    assert [e for e in events if isinstance(e, Failure)] == []


class TestClose:
    """`close()` is the window's last act before the state store closes under
    it: a worker still inside sync_after_turn writes to a closed sqlite
    connection, and its events post to a wake pipe whose fds have been reused."""

    def _wait_for(self, predicate, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not predicate():
            time.sleep(0.01)
        assert predicate()

    def test_close_interrupts_the_turn_and_joins_its_worker(self, env_factory):
        env = env_factory(active="claude")
        events = []
        gate = threading.Event()
        rts = {"claude": FakeRuntime("claude", env, block=gate),
               "codex": FakeRuntime("codex", env)}
        d = Dispatcher(env.store, env.session, rts, events.append, Answers())
        d.submit("slow")
        self._wait_for(lambda: rts["claude"].calls != [])
        started = time.monotonic()
        d.close()                                   # interrupt releases the gate
        assert time.monotonic() - started < 9.0
        assert rts["claude"].interrupts == 1
        assert d._thread is not None and not d._thread.is_alive()

    def test_a_closed_dispatcher_starts_nothing_more(self, env_factory):
        env = env_factory(active="claude")
        events = []
        rts = {"claude": FakeRuntime("claude", env), "codex": FakeRuntime("codex", env)}
        d = Dispatcher(env.store, env.session, rts, events.append, Answers())
        d.close()
        assert d.submit("late") == "closed"
        d.pump()
        assert rts["claude"].calls == [] and d.queue == deque()

    def test_close_wakes_a_worker_parked_on_an_approval(self, env_factory):
        """Quitting with an approval on screen leaves the runtime parked in the
        answers queue, where an interrupt cannot reach it — close() would then
        join for its full timeout. It answers deny first, as Esc does."""
        from tandem.chat.events import ApprovalRequest
        from tandem.chat.window import WindowAnswers

        env = env_factory(active="claude")
        answered = []

        class AsksRuntime:
            harness = "claude"

            def run_turn(self, session, native_id, prompt, model, emit, answers, command=""):
                answered.append(answers.approve(ApprovalRequest("command", "rm -rf ~/")))
                emit(TurnFinished("interrupted", ""))
                return TurnOutcome("interrupted")

            def interrupt(self): pass

            def close(self): pass

        events = []
        rt = AsksRuntime()
        posted = []
        d = Dispatcher(env.store, env.session, {"claude": rt, "codex": rt},
                       events.append, WindowAnswers(posted.append))
        d.submit("ask me")
        # the request is on screen: approve() has dropped its stale values and
        # is waiting on the queue, exactly as it is when the user quits
        self._wait_for(lambda: posted != [])
        started = time.monotonic()
        d.close()
        assert time.monotonic() - started < 9.0
        assert answered == ["deny"]
        assert not d._thread.is_alive()

    def test_close_releases_every_question_of_a_multi_question_request(self, env_factory):
        """claude's AskUserQuestion asks its questions one after another on
        the worker. A close() that answers only the first leaves the worker
        parked on the second for the whole join timeout, and the store then
        closes under it."""
        from tandem.chat.events import QuestionRequest
        from tandem.chat.window import WindowAnswers

        env = env_factory(active="claude")
        answered = []

        class AsksTwice:
            harness = "claude"

            def run_turn(self, session, native_id, prompt, model, emit, answers, command=""):
                for q in ("one?", "two?"):
                    answered.append(answers.answer(QuestionRequest(q, ())))
                emit(TurnFinished("interrupted", ""))
                return TurnOutcome("interrupted")

            def interrupt(self): pass

            def close(self): pass

        events, posted = [], []
        rt = AsksTwice()
        d = Dispatcher(env.store, env.session, {"claude": rt, "codex": rt},
                       events.append, WindowAnswers(posted.append))
        d.submit("ask me twice")
        self._wait_for(lambda: posted != [])
        started = time.monotonic()
        d.close()
        assert time.monotonic() - started < 5.0
        assert answered == ["", ""]
        assert not d._thread.is_alive()


class TestFreshlyPairedSession:
    """A freshly paired session has no file for its ACTIVE harness: claude's
    transcript is written by claude on its first turn, and an active codex has
    no id at all until it runs (cli._pair_session). `switch_session` seeds both
    late; the chat dispatcher goes through prepare_turn, which must do the
    same — or the first prompt routed away from the active harness bricks the
    session."""

    def test_a_routed_first_prompt_seeds_the_fileless_active_claude(self, env_factory):
        env = env_factory(active="claude", seed_active=False)
        assert not env.claude_shadow.exists()
        events = []
        rts = {"claude": FakeRuntime("claude", env), "codex": FakeRuntime("codex", env)}
        d = Dispatcher(env.store, env.session, rts, events.append, Answers())
        try:
            assert d.submit("/codex hello") == ""
            wait_idle(events)
            assert [e for e in events if isinstance(e, Failure)] == []
            assert env.claude_shadow.exists()
            assert "[via codex] codex did hello" in claude_texts(env.claude_shadow)
            # and the window is not wedged: the next turn still runs
            assert d.submit("/claude and now you") == ""
            wait_idle(events, 2)
        finally:
            d.close()
        assert [e for e in events if isinstance(e, Failure)] == []
        assert rts["claude"].calls[-1][1] == "and now you"
        # the seed is tandem's own marker, not a turn: it must not echo outward
        assert not any("half of tandem paired session" in t
                       for t in shadow_texts(env.codex_shadow))

    def test_a_first_prompt_routed_off_a_never_run_codex_seeds_codex(self, env_factory):
        env = env_factory(active="codex", seed_active=False)
        assert env.session.native_id("codex") is None
        events = []
        rts = {"claude": FakeRuntime("claude", env), "codex": FakeRuntime("codex", env)}
        d = Dispatcher(env.store, env.session, rts, events.append, Answers())
        try:
            assert d.submit("/claude go") == ""
            wait_idle(events)
        finally:
            d.close()
        assert [e for e in events if isinstance(e, Failure)] == []
        sid = env.store.get_session(env.session.tandem_id).native_id("codex")
        assert sid
        rollout = get_adapter("codex").transcript_path(env.cwd, sid)
        assert rollout is not None
        texts = shadow_texts(rollout)
        assert "[via claude-code] go" in texts
        assert "[via claude-code] claude did go" in texts


# -- first_turn: what a fresh session defers until it is actually used ---------

def test_first_turn_hook_runs_once_before_the_first_runtime_call(env_factory):
    env = env_factory(active="claude")
    events, order = [], []
    rt = CountingRuntime("claude")
    real = rt.run_turn
    rt.run_turn = lambda *a, **kw: (order.append("turn"), real(*a, **kw))[1]
    d = Dispatcher(env.store, env.session, {"claude": rt, "codex": rt}, events.append, Answers(),
                   first_turn=lambda: order.append("seed"))
    try:
        assert d.first_turn_pending
        d.submit("one"); wait_idle(events)
        assert not d.first_turn_pending
        d.submit("two"); wait_idle(events, 2)
    finally:
        d.close()
    assert order == ["seed", "turn", "turn"]


def test_first_turn_hook_that_fails_fails_the_turn_and_is_retried(env_factory):
    env = env_factory(active="claude")
    events, attempts = [], []
    rt = CountingRuntime("claude")

    def seed():
        attempts.append(1)
        if len(attempts) == 1:
            raise OSError("disk full")

    d = Dispatcher(env.store, env.session, {"claude": rt, "codex": rt}, events.append, Answers(),
                   first_turn=seed)
    try:
        d.submit("one"); wait_idle(events)
        assert rt.calls == [] and d.first_turn_pending
        assert any(isinstance(e, Failure) and "disk full" in e.message for e in events)
        assert TurnFinished("failed", "") in events
        d.submit("two"); wait_idle(events, 2)
    finally:
        d.close()
    assert rt.calls == ["two"] and len(attempts) == 2 and not d.first_turn_pending


def test_without_a_hook_nothing_is_pending(setup):
    _, d, _, _ = setup
    assert not d.first_turn_pending


from tandem.chat.events import Evidence, ToolStarted, Verdict
from tandem.chat.navigator import Note


class StubNavigator:
    """Records what the dispatcher hands it and scripts what it hands back."""

    def __init__(self, note=None):
        self.shadow_lock = threading.Lock()
        self.note = note
        self.takes, self.ended, self.closed = [], [], 0
        self.lock_held_during_sync = []
        self.given_back = []

    def take(self, harness):
        self.takes.append(harness)
        n, self.note = self.note, None
        return n if n is not None and harness == "codex" else None

    def turn_ended(self, facts, session):
        self.ended.append((facts, session))

    def close(self):
        self.closed += 1

    def give_back(self, note):
        self.given_back.append(note)


def make_note(text="bad loop"):
    return Note("ref-1", "codex", "claude", Verdict("speak", severity="block", note=text,
                                                     evidence=(Evidence("s.py", 12, "w"),)))


def run_one(env, nav, text, harness="claude"):
    events = []
    done = threading.Event()

    def emit(ev):
        events.append(ev)
        if isinstance(ev, Idle):
            done.set()

    runtimes = {"claude": FakeRuntime("claude", env), "codex": FakeRuntime("codex", env)}
    d = Dispatcher(env.store, env.session, runtimes, emit, Answers(), navigator=nav)
    assert d.submit(text) == ""
    assert done.wait(5)
    return d, runtimes, events


def test_facts_reach_the_navigator_after_sync(env_factory):
    env = env_factory()
    nav = StubNavigator()
    d, runtimes, events = run_one(env, nav, "fix it")
    assert len(nav.ended) == 1
    facts, session = nav.ended[0]
    assert facts.harness == "claude" and facts.prompt == "fix it" and facts.status == "completed"
    assert facts.carried_note is False and facts.first_turn is False
    assert facts.final_text == "claude says hi"
    assert session.tandem_id == env.session.tandem_id
    # turn_ended ran before Idle was announced
    assert isinstance(events[-1], Idle)


def test_a_seeded_first_turn_is_reviewed(env_factory):
    """Seeding runs before the facts are taken, so a turn that completed —
    and needed the seeding to — is not skipped as a first turn."""
    env = env_factory()
    nav = StubNavigator()
    events, done = [], threading.Event()
    emit = lambda ev: (events.append(ev), isinstance(ev, Idle) and done.set())
    runtimes = {"claude": FakeRuntime("claude", env), "codex": FakeRuntime("codex", env)}
    d = Dispatcher(env.store, env.session, runtimes, emit, Answers(), first_turn=lambda: None, navigator=nav)
    d.submit("hello"); assert done.wait(5)
    assert nav.ended[0][0].first_turn is False


def test_a_pending_note_rides_the_prompt_as_a_trailer(env_factory):
    env = env_factory()
    nav = StubNavigator(note=make_note())
    d, runtimes, events = run_one(env, nav, "/codex why?")
    native_id, prompt, model = runtimes["codex"].calls[0]
    assert prompt.startswith("why?\n\n[tandem navigator] codex reviewed the previous claude turn")
    assert "s.py:12 — w" in prompt
    started = [e for e in events if isinstance(e, TurnStarted)][0]
    assert started.prompt == "why?" and started.carried == "bad loop"
    assert nav.ended[0][0].carried_note is True and nav.ended[0][0].prompt == "why?"
    assert nav.takes == ["codex"]


def test_a_bare_route_consumes_no_note(env_factory):
    env = env_factory()
    nav = StubNavigator(note=make_note())
    runtimes = {"claude": FakeRuntime("claude", env), "codex": FakeRuntime("codex", env)}
    d = Dispatcher(env.store, env.session, runtimes, lambda ev: None, Answers(), navigator=nav)
    assert d.submit("/codex").startswith("default → codex")
    assert nav.takes == [] and nav.note is not None


def test_the_shadow_lock_is_held_across_prepare_and_sync(env_factory, monkeypatch):
    env = env_factory()
    nav = StubNavigator()
    seen = []
    real_prepare, real_sync = dispatch.ops.prepare_turn, dispatch.ops.sync_after_turn
    monkeypatch.setattr(dispatch.ops, "prepare_turn",
                        lambda *a, **k: (seen.append(("prepare", nav.shadow_lock.locked())), real_prepare(*a, **k))[1])
    monkeypatch.setattr(dispatch.ops, "sync_after_turn",
                        lambda *a, **k: (seen.append(("sync", nav.shadow_lock.locked())), real_sync(*a, **k))[1])
    run_one(env, nav, "go")
    assert seen == [("prepare", True), ("sync", True)]
    assert not nav.shadow_lock.locked()


def test_without_a_navigator_nothing_changes(env_factory):
    env = env_factory()
    d, runtimes, events = run_one(env, None, "go")
    assert runtimes["claude"].calls[0][1] == "go"
    assert [e for e in events if isinstance(e, TurnStarted)][0].carried == ""


def test_a_navigator_that_raises_does_not_break_the_turn(env_factory):
    env = env_factory()
    nav = StubNavigator()
    nav.turn_ended = lambda facts, session: (_ for _ in ()).throw(RuntimeError("nav bug"))
    d, runtimes, events = run_one(env, nav, "go")
    assert not any(isinstance(e, Failure) for e in events)
    assert isinstance(events[-1], Idle)


def test_close_reaches_the_navigator(env_factory):
    env = env_factory()
    nav = StubNavigator()
    runtimes = {"claude": FakeRuntime("claude", env), "codex": FakeRuntime("codex", env)}
    d = Dispatcher(env.store, env.session, runtimes, lambda ev: None, Answers(), navigator=nav)
    d.close()
    assert nav.closed == 1


def test_a_note_taken_for_a_turn_that_never_ran_goes_back(env_factory):
    env = env_factory()
    note = make_note()
    nav = StubNavigator(note=note)
    env.codex_shadow.write_text("{not json\n")        # the routed target's transcript fails validation
    d, runtimes, events = run_one(env, nav, "/codex why?")
    assert runtimes["codex"].calls == []
    assert [e for e in events if isinstance(e, TurnStarted)][0].carried == "bad loop"
    assert any(isinstance(e, Failure) for e in events)
    assert nav.given_back == [note] and nav.ended == []


def test_a_turn_whose_sync_failed_is_not_reviewed(env_factory, monkeypatch):
    env = env_factory()
    nav = StubNavigator()

    def boom(*a, **k):
        raise dispatch.SyncSetupError("boom")

    monkeypatch.setattr(dispatch.ops, "sync_after_turn", boom)
    d, runtimes, events = run_one(env, nav, "go")
    assert runtimes["claude"].calls
    assert [e.message for e in events if isinstance(e, Failure)][0].startswith("sync:")
    assert nav.ended == [] and nav.given_back == []


# -- dispatcher commands: /model and /compact -----------------------------------


def collect(env, runtimes=None, **kw):
    events, done = [], threading.Event()

    def emit(ev):
        events.append(ev)
        if isinstance(ev, Idle):
            done.set()

    runtimes = runtimes or {"claude": FakeRuntime("claude", env), "codex": FakeRuntime("codex", env)}
    return Dispatcher(env.store, env.session, runtimes, emit, Answers(), **kw), runtimes, events, done


def test_model_alone_lists_the_default_harnesss_models_as_a_notice(env_factory):
    env = env_factory()
    d, runtimes, events, done = collect(env)
    assert d.submit("/model") == ""
    assert done.wait(5)
    notices = [e for e in events if isinstance(e, Notice)]
    assert len(notices) == 1 and "claude-a  first" in notices[0].text
    assert not any(isinstance(e, TurnStarted) for e in events)
    assert runtimes["claude"].calls == []                      # no turn ran


def test_model_marks_the_pinned_one(env_factory):
    env = env_factory()
    env.store.set_pin(env.session.tandem_id, "claude", "claude-b")
    d, runtimes, events, done = collect(env)
    d.submit("/model"); assert done.wait(5)
    text = next(e for e in events if isinstance(e, Notice)).text
    assert "* claude-b" in text and "  claude-a" in text


def test_model_with_a_name_is_the_pin_route(env_factory):
    env = env_factory()
    d, runtimes, events, done = collect(env)
    assert d.submit("/model haiku") == "default → claude · haiku"
    assert d.pin("claude") == "haiku"


def test_model_listing_failure_is_a_failure_event(env_factory):
    env = env_factory()
    rts = {"claude": FakeRuntime("claude", env, fail_models=True), "codex": FakeRuntime("codex", env)}
    d, runtimes, events, done = collect(env, rts)
    d.submit("/model"); assert done.wait(5)
    assert any(isinstance(e, Failure) and "no catalog" in e.message for e in events)


def test_compact_runs_as_a_command_turn(env_factory):
    env = env_factory()
    d, runtimes, events, done = collect(env)
    assert d.submit("/compact") == ""
    assert done.wait(5)
    assert runtimes["claude"].calls[-1][1:] == ("/compact", "", "compact")
    started = next(e for e in events if isinstance(e, TurnStarted))
    assert started.prompt == "/compact" and started.harness == "claude"


def test_a_routed_compact_runs_on_the_routed_harness(env_factory):
    env = env_factory()
    d, runtimes, events, done = collect(env)
    d.submit("/codex /compact"); assert done.wait(5)
    assert runtimes["codex"].calls[-1][3] == "compact"
    assert runtimes["claude"].calls == []


def test_compact_with_arguments_is_ordinary_pass_through(env_factory):
    env = env_factory()
    d, runtimes, events, done = collect(env)
    d.submit("/compact keep the API notes"); assert done.wait(5)
    assert runtimes["claude"].calls[-1] == (env.session.native_id("claude"), "/compact keep the API notes", "")


def test_compact_takes_no_navigator_note_and_reports_no_facts(env_factory):
    env = env_factory()
    nav = StubNavigator(make_note())
    d, runtimes, events, done = collect(env, navigator=nav)
    d.submit("/codex /compact"); assert done.wait(5)
    assert nav.takes == [] and nav.ended == []
    assert nav.note is not None                                 # still waiting for a real prompt


def test_model_with_a_spaced_name_is_an_error_not_a_turn(env_factory):
    env = env_factory()
    d, runtimes, events, done = collect(env)
    got = d.submit("/model gpt 5.5")
    assert got.startswith("error:") and "one model name" in got
    assert d.pin("claude") == "" and runtimes["claude"].calls == []


# -- turn mode: the review round ---------------------------------------------------

from types import SimpleNamespace

from tandem.chat.events import FileDiff, ReviewFinished, ToolFinished, ToolOutput
from tandem.chat.navigator import SCHEMA, DenyAll, TurnFacts
from tandem.config import ChatConfig

SPEAK = json.dumps({"verdict": "speak", "severity": "block", "note": "bad loop",
                    "evidence": [{"file": "s.py", "line": 12, "why": "w"}]})


def facts_for(harness="claude", prompt="fix it"):
    return TurnFacts(harness, prompt, False, False, "completed", ("s.py",), 0, 0, "", 0.0, 1.0)


class RoundNavigator:
    """A turn-mode navigator with no gate of its own: the first `rounds`
    completed non-tandem turns go to the dispatcher's queue (the real gate
    would skip the quiet ones), and a settled verdict comes back as the
    note to act on. Records everything; posts nothing."""

    def __init__(self, harness="codex", rounds=1):
        self.harness = harness
        self.cfg = ChatConfig(navigator=harness, navigator_deliver="turn", navigator_model="rev-model")
        self.shadow_lock = threading.Lock()
        self.dispatch = None
        self.rounds = rounds
        self.ended, self.settled, self.rides, self.closed = [], [], [], 0
        self.log = SimpleNamespace(ridden=lambda ref, to: self.rides.append((ref, to)))

    def take(self, harness):
        return None

    def give_back(self, note):
        pass

    def turn_ended(self, facts, session):
        self.ended.append(facts)
        if facts.status == "completed" and not facts.prompt.startswith("[tandem") and self.rounds > 0:
            self.rounds -= 1
            self.dispatch(facts)

    def settle_round(self, facts, verdict):
        self.settled.append(verdict)
        return Note("ref-9", self.harness, facts.harness, verdict) if verdict.spoken else None

    def close(self):
        self.closed += 1


def round_setup(env, nav, runtimes):
    """A dispatcher wired the way run_chat wires it, whose emit pumps on
    Idle as the window does. Returns (dispatcher, events)."""
    events, holder = [], {}

    def emit(ev):
        events.append(ev)
        if isinstance(ev, Idle):
            holder["d"].pump()

    d = holder["d"] = Dispatcher(env.store, env.session, runtimes, emit, Answers(), navigator=nav)
    nav.dispatch = d.start_round
    return d, events


def starts(events):
    return [(e.harness, e.kind, e.peer, e.carried) for e in events if isinstance(e, TurnStarted)]


def test_a_round_runs_review_then_followup_ahead_of_what_was_typed(env_factory):
    env = env_factory(active="claude")
    env.store.set_pin(env.session.tandem_id, "claude", "opus")
    nav = RoundNavigator()
    gate = threading.Event()
    rts = {"claude": FakeRuntime("claude", env, block=gate, review_reply=SPEAK, paths=("s.py",)),
           "codex": FakeRuntime("codex", env, review_reply=SPEAK)}
    d, events = round_setup(env, nav, rts)
    try:
        assert d.submit("fix it") == ""
        assert d.submit("next") == "queued → claude"
        gate.set()
        wait_idle(events, 4)
        # order: the prompt, its review, the follow-up, then what was typed
        assert starts(events) == [("claude", "", "", ""), ("codex", "review", "claude", ""),
                                  ("claude", "followup", "codex", "bad loop"), ("claude", "", "", "")]
        review = rts["codex"].calls[0]
        assert review[1].startswith("[tandem navigator] You are reviewing") and "s.py" in review[1]
        assert review[2] == "rev-model" and review[3] == SCHEMA
        assert isinstance(rts["codex"].last_answers, DenyAll)
        assert isinstance(rts["claude"].last_answers, Answers)
        prompts = [c[1] for c in rts["claude"].calls]
        assert prompts[0] == "fix it" and prompts[2] == "next"
        assert prompts[1].startswith("[tandem navigator] codex reviewed your previous turn and flagged (block): bad loop")
        assert prompts[1].endswith("disagree with and why.") and "s.py:12 — w" in prompts[1]
        assert rts["claude"].calls[1][2] == "opus"            # the executor's pin
        assert nav.rides == [("ref-9", "claude")]
        assert [v.verdict for v in nav.settled] == ["speak"]
        # the review's JSON was collected, not painted
        assert not any(isinstance(e, TextDelta) and "verdict" in e.text for e in events)
        # the follow-up was handed to the gate (and would be skipped there), the review was not
        assert [f.prompt[:18] for f in nav.ended] == ["fix it", "[tandem navigator]", "next"]
        assert nav.ended[1].carried_note is True
        # the review never became the default
        assert env.store.get_session(env.session.tandem_id).active == "claude"
        # both transcripts hold the round
        codex = json.dumps(list(read_jsonl(env.codex_shadow)))
        assert "[tandem navigator] You are reviewing" in codex and "bad loop" in codex
        claude = "\n".join(claude_texts(env.claude_shadow))
        assert "bad loop" in claude and "[tandem navigator] codex reviewed your previous turn" in claude
    finally:
        d.close()


def test_a_clean_review_ends_the_round_without_a_followup(env_factory):
    env = env_factory()
    nav = RoundNavigator()
    rts = {"claude": FakeRuntime("claude", env), "codex": FakeRuntime("codex", env)}
    d, events = round_setup(env, nav, rts)
    try:
        d.submit("fix it"); d.submit("next")
        wait_idle(events, 3)
        assert starts(events) == [("claude", "", "", ""), ("codex", "review", "claude", ""), ("claude", "", "", "")]
        assert [v.verdict for v in nav.settled] == ["clean"] and nav.rides == []
        assert [c[1] for c in rts["claude"].calls] == ["fix it", "next"]
    finally:
        d.close()


def test_a_failed_review_is_an_error_verdict_and_no_followup(env_factory):
    env = env_factory()
    nav = RoundNavigator()
    rts = {"claude": FakeRuntime("claude", env), "codex": FakeRuntime("codex", env, half_turn=True)}
    d, events = round_setup(env, nav, rts)
    try:
        d.submit("fix it")
        wait_idle(events, 2)
        assert [(v.verdict, v.error) for v in nav.settled] == [("error", "boom")]
        assert nav.rides == [] and len(rts["claude"].calls) == 1
        assert isinstance(events[-1], Idle)
    finally:
        d.close()


class InterruptedReview(FakeRuntime):
    def run_turn(self, session, native_id, prompt, model, emit, answers, command="", review=None):
        if review is None:
            return super().run_turn(session, native_id, prompt, model, emit, answers, command=command)
        self.calls.append((native_id, prompt, model, review))
        emit(TurnFinished("interrupted", ""))
        return TurnOutcome("interrupted")


def test_an_interrupted_review_ends_the_round_without_a_strike(env_factory):
    env = env_factory()
    nav = RoundNavigator()
    rts = {"claude": FakeRuntime("claude", env), "codex": InterruptedReview("codex", env)}
    d, events = round_setup(env, nav, rts)
    try:
        d.submit("fix it")
        wait_idle(events, 2)
        assert [(v.verdict, v.error) for v in nav.settled] == [("error", "interrupted")]
        assert nav.rides == [] and len(rts["claude"].calls) == 1
    finally:
        d.close()


def test_an_unparsable_review_reply_is_an_error_and_no_followup(env_factory):
    env = env_factory()
    nav = RoundNavigator()
    rts = {"claude": FakeRuntime("claude", env), "codex": FakeRuntime("codex", env, review_reply="no json here")}
    d, events = round_setup(env, nav, rts)
    try:
        d.submit("fix it")
        wait_idle(events, 2)
        assert nav.settled[0].verdict == "error" and "unparsable" in nav.settled[0].error
        assert nav.rides == [] and len(rts["claude"].calls) == 1
    finally:
        d.close()


def test_a_review_whose_sync_fails_is_settled_as_an_error(env_factory, monkeypatch):
    env = env_factory()
    nav = RoundNavigator()
    real = dispatch.ops.sync_after_turn

    def flaky(store, session, target, **kw):
        if target == "codex":
            raise dispatch.SyncSetupError("codex boom")
        return real(store, session, target, **kw)

    monkeypatch.setattr(dispatch.ops, "sync_after_turn", flaky)
    rts = {"claude": FakeRuntime("claude", env, review_reply=SPEAK), "codex": FakeRuntime("codex", env, review_reply=SPEAK)}
    d, events = round_setup(env, nav, rts)
    try:
        d.submit("fix it")
        wait_idle(events, 2)
        assert nav.settled[0].verdict == "error" and nav.settled[0].error == "sync: codex boom"
        assert nav.rides == [] and any(isinstance(e, Failure) and e.message.startswith("sync:") for e in events)
        assert isinstance(events[-1], Idle)
    finally:
        d.close()


def test_start_round_after_close_queues_nothing(env_factory):
    env = env_factory()
    nav = RoundNavigator()
    rts = {"claude": FakeRuntime("claude", env), "codex": FakeRuntime("codex", env)}
    d = Dispatcher(env.store, env.session, rts, lambda ev: None, Answers(), navigator=nav)
    d.close()
    d.start_round(facts_for())
    assert not d.queue


def test_start_round_puts_the_review_ahead_of_the_queue(env_factory):
    env = env_factory()
    nav = RoundNavigator()
    rts = {"claude": FakeRuntime("claude", env), "codex": FakeRuntime("codex", env)}
    d = Dispatcher(env.store, env.session, rts, lambda ev: None, Answers(), navigator=nav)
    try:
        d.queue.append(dispatch.Pending("claude", "", "typed"))
        d.start_round(facts_for())
        first = d.queue[0]
        assert first.kind == "review" and first.harness == "codex" and first.peer == "claude"
        assert first.model == "rev-model" and first.facts.prompt == "fix it"
        assert d.queue[1].prompt == "typed"
    finally:
        d.close()


def test_a_route_typed_during_the_review_outlives_the_followup(env_factory):
    """`/codex` sent while codex reviews claude moves the default at once.
    The follow-up that runs next was queued by tandem, not typed, so it
    must not move the default back — even though it starts after that
    route and so carries a fresh spoken snapshot."""
    env = env_factory(active="claude")
    nav = RoundNavigator()
    gate = threading.Event()
    rts = {"claude": FakeRuntime("claude", env, review_reply=SPEAK),
           "codex": FakeRuntime("codex", env, block=gate, review_reply=SPEAK)}
    d, events = round_setup(env, nav, rts)
    try:
        d.submit("fix it")
        wait_idle(events, 1)                            # claude ran; the review is now blocked in codex
        deadline = time.monotonic() + 5
        while not rts["codex"].calls and time.monotonic() < deadline:
            time.sleep(0.01)
        assert rts["codex"].calls, "the review turn never started"
        assert d.submit("/codex").startswith("default → codex")
        gate.set()
        wait_idle(events, 3)
        assert starts(events)[1:] == [("codex", "review", "claude", ""), ("claude", "followup", "codex", "bad loop")]
        assert env.store.get_session(env.session.tandem_id).active == "codex"
    finally:
        d.close()


def test_the_mirror_round_reviews_on_claude_and_follows_up_on_codex(env_factory):
    env = env_factory(active="codex")
    nav = RoundNavigator(harness="claude")
    rts = {"claude": FakeRuntime("claude", env, review_reply=SPEAK), "codex": FakeRuntime("codex", env, review_reply=SPEAK)}
    d, events = round_setup(env, nav, rts)
    try:
        d.submit("fix it")
        wait_idle(events, 3)
        assert starts(events) == [("codex", "", "", ""), ("claude", "review", "codex", ""),
                                  ("codex", "followup", "claude", "bad loop")]
        assert rts["claude"].calls[0][3] == SCHEMA and isinstance(rts["claude"].last_answers, DenyAll)
        assert rts["codex"].calls[1][1].startswith("[tandem navigator] claude reviewed your previous turn")
        assert env.store.get_session(env.session.tandem_id).active == "codex"
    finally:
        d.close()


def settle_quietly(events, d, count):
    """Wait for `count` Idles, give a stray extra one time to show, and
    return how many arrived."""
    wait_idle(events, count)
    time.sleep(0.1)
    return sum(isinstance(e, Idle) for e in events)


def test_a_review_whose_prompt_cannot_be_built_still_settles_and_idles(env_factory, monkeypatch):
    env = env_factory()
    nav = RoundNavigator()

    def explode(*a, **kw):
        raise RuntimeError("git exploded")

    monkeypatch.setattr(dispatch, "compute_diff", explode)
    rts = {"claude": FakeRuntime("claude", env, review_reply=SPEAK), "codex": FakeRuntime("codex", env, review_reply=SPEAK)}
    d, events = round_setup(env, nav, rts)
    try:
        d.submit("fix it")
        assert settle_quietly(events, d, 2) == 2
        assert len(nav.settled) == 1 and nav.settled[0].verdict == "error"
        assert "git exploded" in nav.settled[0].error
        assert nav.rides == [] and rts["codex"].calls == []
        assert not d.busy
    finally:
        d.close()


class RaisingSettle(RoundNavigator):
    def settle_round(self, facts, verdict):
        self.settled.append(verdict)
        raise RuntimeError("nav bug")


def test_a_navigator_that_raises_in_settle_round_is_settled_once_and_paints_no_failure(env_factory):
    env = env_factory()
    nav = RaisingSettle()
    rts = {"claude": FakeRuntime("claude", env, review_reply=SPEAK), "codex": FakeRuntime("codex", env, review_reply=SPEAK)}
    d, events = round_setup(env, nav, rts)
    try:
        d.submit("fix it")
        assert settle_quietly(events, d, 2) == 2
        assert len(nav.settled) == 1
        assert not any(isinstance(e, Failure) for e in events)
        assert nav.rides == []
    finally:
        d.close()


def test_a_ridden_log_that_raises_does_not_end_the_round_in_error(env_factory):
    env = env_factory()
    nav = RoundNavigator()
    nav.log = SimpleNamespace(ridden=lambda ref, to: (_ for _ in ()).throw(OSError("disk")))
    rts = {"claude": FakeRuntime("claude", env, review_reply=SPEAK), "codex": FakeRuntime("codex", env, review_reply=SPEAK)}
    d, events = round_setup(env, nav, rts)
    try:
        d.submit("fix it")
        assert settle_quietly(events, d, 3) == 3
        assert starts(events)[2][1] == "followup"
        assert [v.verdict for v in nav.settled] == ["speak"]
        assert not any(isinstance(e, Failure) for e in events)
    finally:
        d.close()


class CorruptsCodex(FakeRuntime):
    """A claude turn that, once its own turn is written, leaves the codex
    shadow unreadable — so the review that follows fails validation."""

    def run_turn(self, session, native_id, prompt, model, emit, answers, command="", review=None):
        outcome = super().run_turn(session, native_id, prompt, model, emit, answers, command=command, review=review)
        self.env.codex_shadow.write_text("{not json\n")
        return outcome


def test_a_review_that_fails_validation_is_settled_as_an_error(env_factory):
    env = env_factory()
    nav = RoundNavigator()
    rts = {"claude": CorruptsCodex("claude", env, review_reply=SPEAK), "codex": FakeRuntime("codex", env, review_reply=SPEAK)}
    d, events = round_setup(env, nav, rts)
    try:
        d.submit("fix it")
        assert settle_quietly(events, d, 2) == 2
        assert len(nav.settled) == 1 and nav.settled[0].verdict == "error"
        assert nav.settled[0].error.startswith("codex transcript:")
        assert nav.rides == [] and rts["codex"].calls == []
    finally:
        d.close()


class RaisesOnReview(FakeRuntime):
    def run_turn(self, session, native_id, prompt, model, emit, answers, command="", review=None):
        if review is not None:
            raise RuntimeError("boom")
        return super().run_turn(session, native_id, prompt, model, emit, answers, command=command)


def test_a_runtime_that_raises_on_the_review_is_settled_as_an_error(env_factory):
    env = env_factory()
    nav = RoundNavigator()
    rts = {"claude": FakeRuntime("claude", env, review_reply=SPEAK), "codex": RaisesOnReview("codex", env, review_reply=SPEAK)}
    d, events = round_setup(env, nav, rts)
    try:
        d.submit("fix it")
        assert settle_quietly(events, d, 2) == 2
        assert len(nav.settled) == 1 and nav.settled[0].verdict == "error"
        assert "boom" in nav.settled[0].error
        assert len(rts["claude"].calls) == 1
        assert not d.busy
    finally:
        d.close()


class StructuredOutputRuntime(FakeRuntime):
    """A reviewer whose verdict arrives the way claude's `--json-schema`
    delivers it: as a StructuredOutput tool call, beside a real Read."""

    def run_turn(self, session, native_id, prompt, model, emit, answers, command="", review=None):
        if review is not None:
            emit(ToolStarted("r1", "Read", "s.py"))
            emit(ToolStarted("s1", "StructuredOutput", SPEAK))
            emit(ToolOutput("s1", "Structured output provided successfully"))
            emit(ToolFinished("s1", True, ""))
            emit(FileDiff("s1", "s.py", "@@"))
            emit(ToolFinished("r1", True, ""))
        return super().run_turn(session, native_id, prompt, model, emit, answers, command, review)


def test_a_structured_output_tool_call_is_the_verdict_and_never_painted(env_factory):
    env = env_factory(active="claude")
    nav = RoundNavigator()
    rts = {"claude": FakeRuntime("claude", env),
           "codex": StructuredOutputRuntime("codex", env, review_reply=SPEAK)}
    d, events = round_setup(env, nav, rts)
    try:
        d.submit("fix it")
        wait_idle(events, 3)
        assert not [e for e in events if getattr(e, "call_id", None) == "s1"]
        assert [(type(e).__name__, e.call_id) for e in events if getattr(e, "call_id", None) == "r1"] == \
            [("ToolStarted", "r1"), ("ToolFinished", "r1")]
        assert [v.verdict for v in nav.settled] == ["speak"]
    finally:
        d.close()
