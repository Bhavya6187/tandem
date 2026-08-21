"""Flip loop tests with a fake harness runner."""

import sqlite3
import threading

import pytest

from tandem import flip, paths, routefile
from tandem.routefile import RouteRequest
from tandem.state import StateStore
from tandem.tabs import TabState


class FakeMem:
    actions: list = []
    warnings: list = []


@pytest.fixture
def sess(tmp_path, monkeypatch):
    monkeypatch.setenv("TANDEM_HOME", str(tmp_path / ".tandem"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / ".codex"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / ".claude"))
    with StateStore() as store:
        return store.create_session(str(tmp_path / "proj"), "claude",
                                    ["claude", "codex"],
                                    {"claude": "c-id", "codex": "x-id"})


@pytest.fixture
def sess3(tmp_path, monkeypatch):
    """`sess` widened to three participants: the tab cycle and the routed
    switch both need somewhere to go that is not simply "the other one"."""
    monkeypatch.setenv("TANDEM_HOME", str(tmp_path / ".tandem"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / ".codex"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / ".claude"))
    with StateStore() as store:
        return store.create_session(str(tmp_path / "proj"), "claude",
                                    ["claude", "codex", "opencode"],
                                    {"claude": "c-id", "codex": "x-id",
                                     "opencode": "o-id"})


def fake_runner(log, codes=None):
    codes = list(codes or [])

    def run_harness(session):
        log.append(session.active)
        return codes.pop(0) if codes else 0

    return run_harness


def _flipping_switch(monkeypatch):
    def fake_switch(store, session, to=None):
        new = to or session.next_active(session.active)
        store.set_active(session.tandem_id, new)
        return new, [], FakeMem()

    monkeypatch.setattr(flip.ops, "switch_session", fake_switch)


def test_exit_prints_resume_hint_and_last_code(sess, capsys):
    log = []
    code = flip.run_session(
        sess.tandem_id, None, run_harness=fake_runner(log, codes=[7])
    )
    assert code == 7
    assert log == ["claude"]
    assert f"to continue this session: tandem resume {sess.tandem_id}" in (
        capsys.readouterr().out
    )


def test_failed_entry_reports_and_exits(sess, capsys):
    """The harness binary vanishing must not kill the session."""

    def run_harness(session):
        raise FileNotFoundError("claude: command not found")

    code = flip.run_session(sess.tandem_id, None, run_harness=run_harness)
    cap = capsys.readouterr()
    assert code == 1  # a launch that never ran is a failure, not a 0
    assert "could not run the harness: FileNotFoundError" in cap.err
    assert f"to continue this session: tandem resume {sess.tandem_id}" in cap.out


def test_resume_hint_prints_even_when_the_loop_raises(sess, capsys, monkeypatch):
    """The hint is the only place the id is shown, so it must survive an
    unexpected exception escaping the loop."""

    def boom(tandem_id, run_harness, first, reports, carry, tabs=None):
        raise RuntimeError("terminal went away")

    monkeypatch.setattr(flip, "_flip_loop", boom)
    with pytest.raises(RuntimeError):
        flip.run_session(sess.tandem_id, None, run_harness=fake_runner([]))
    assert f"to continue this session: tandem resume {sess.tandem_id}" in (
        capsys.readouterr().out
    )


def test_flip_reenters_other_harness(sess, monkeypatch):
    """Ctrl-] inside the harness flips and re-enters with no stop."""
    _flipping_switch(monkeypatch)
    calls = []

    def run_harness(session):
        calls.append(session.active)
        if len(calls) == 1:
            return 0, True   # user pressed Ctrl-]
        return 0, False      # then exited normally

    code = flip.run_session(sess.tandem_id, None, run_harness=run_harness)
    assert code == 0
    assert calls == ["claude", "codex"]  # flip switched roles


def test_flip_loop_keeps_flipping_until_a_plain_exit(sess, monkeypatch):
    """Successive flips chain; a plain exit ends the session."""
    _flipping_switch(monkeypatch)
    calls = []

    def run_harness(session):
        calls.append(session.active)
        return 0, len(calls) < 4

    flip.run_session(sess.tandem_id, None, run_harness=run_harness)
    assert calls == ["claude", "codex", "claude", "codex"]


def test_flip_failure_exits_with_the_session_intact(sess, capsys, monkeypatch):
    """ops.switch_session raising must not lose the session or spin."""

    def run_harness(session):
        return 0, True

    def boom(store, session, to=None):
        raise RuntimeError("no flip for you")

    monkeypatch.setattr(flip.ops, "switch_session", boom)
    code = flip.run_session(sess.tandem_id, None, run_harness=run_harness)
    cap = capsys.readouterr()
    assert code == 0  # carried through; session intact
    assert "switch failed: RuntimeError: no flip for you" in cap.err
    assert f"to continue this session: tandem resume {sess.tandem_id}" in cap.out


def test_failed_launch_after_a_flip_returns_to_the_harness_we_left(
    sess, capsys, monkeypatch
):
    """The ladder's first rung: never strand the user facing a harness that
    cannot launch. Flip the roles back and re-enter the one they were in a
    second ago."""
    _flipping_switch(monkeypatch)
    calls = []

    def run_harness(session):
        calls.append(session.active)
        if session.active == "codex":
            raise FileNotFoundError("codex: command not found")
        return 0, len(calls) == 1   # the first claude session asks for a flip

    flip.run_session(sess.tandem_id, None, run_harness=run_harness)
    cap = capsys.readouterr()
    assert calls == ["claude", "codex", "claude"]  # flipped, failed, flipped back
    assert "codex would not start — switching back to claude." in cap.err
    with StateStore() as store:
        assert store.get_session(sess.tandem_id).active == "claude"  # roles restored


def test_both_harnesses_failing_exits_with_the_session_intact(
    sess, capsys, monkeypatch
):
    """Second rung: the flip-back cannot launch either, so land at the OS
    shell with the errors shown — and the resume hint still prints."""
    _flipping_switch(monkeypatch)
    calls = []

    def run_harness(session):
        calls.append(session.active)
        if len(calls) == 1:
            return 0, True          # flip requested
        raise FileNotFoundError(f"{session.active}: command not found")

    code = flip.run_session(sess.tandem_id, None, run_harness=run_harness)
    cap = capsys.readouterr()
    assert calls == ["claude", "codex", "claude"]  # one retry only, no ping-pong
    assert cap.err.count("could not run the harness: FileNotFoundError") == 2
    assert code == 1  # neither harness ran: a failure, not the flipping run's 0
    assert f"to continue this session: tandem resume {sess.tandem_id}" in cap.out


def test_flip_back_does_not_run_when_the_switch_itself_fails(sess, capsys, monkeypatch):
    """`ops.switch_session` raising means roles never moved: there is nothing
    to flip back from."""

    def boom(store, session, to=None):
        raise RuntimeError("no flip for you")

    monkeypatch.setattr(flip.ops, "switch_session", boom)
    calls = []

    def run_harness(session):
        calls.append(session.active)
        return 0, len(calls) == 1

    flip.run_session(sess.tandem_id, None, run_harness=run_harness)
    cap = capsys.readouterr()
    assert calls == ["claude"]
    assert "switch failed: RuntimeError: no flip for you" in cap.err
    assert "would not start" not in cap.err


def test_plain_entry_failure_has_no_flip_back(sess, capsys):
    """A failed launch with no flip involved never moved roles, so it must
    not drag the session into the other harness."""
    calls = []

    def run_harness(session):
        calls.append(session.active)
        raise FileNotFoundError("claude: command not found")

    flip.run_session(sess.tandem_id, None, run_harness=run_harness)
    assert calls == ["claude"]
    with StateStore() as store:
        assert store.get_session(sess.tandem_id).active == "claude"
    assert "would not start" not in capsys.readouterr().err


def test_flip_with_a_vanished_session_row_stops_the_loop(sess, capsys):
    """A session deleted mid-flight breaks the loop instead of spinning."""

    def run_harness(session):
        conn = sqlite3.connect(paths.state_db_path())
        with conn:
            conn.execute(
                "DELETE FROM sessions WHERE tandem_id = ?", (sess.tandem_id,)
            )
        conn.close()
        return 0, True

    code = flip.run_session(sess.tandem_id, None, run_harness=run_harness)
    assert code == 0
    assert "switch failed" in capsys.readouterr().err


class FakeInteractiveRunner:
    """Stands in for the real runner behind `run_session`'s own closure — the
    seam the reports plumbing actually lives in, so the injected `run_harness`
    used by every other test would skip it entirely."""

    script: list = []
    seen: list = []

    def __init__(self, session, sink_factory=None, adopt_child=None,
                 tabs=None, inject=None):
        self.session = session
        self.reports = []
        self.flip_requested = False
        self.adopt_child = adopt_child
        self.tabs = tabs
        self.inject = inject
        self.route_request = None   # the real runner's routed-flip out-attr
        self.warm_child = None   # the real runner's standby-out attribute

    def run(self):
        FakeInteractiveRunner.seen.append(self.session.active)
        reports, flip_req = FakeInteractiveRunner.script.pop(0)
        self.reports = list(reports)
        self.flip_requested = flip_req
        if not flip_req:  # the real runner prints its own on a non-flip exit
            for line in self.reports:
                print(line)
        return 0


def _fake_runner_session(monkeypatch, sess, script,
                         runner=FakeInteractiveRunner, tty=True):
    """Run a whole session through `run_session`'s own closure with `runner`
    standing in for the real InteractiveRunner. `tty` drives the closure's
    handover gate: under pytest stdin is never a terminal, and these fakes
    are all standing in for interactive runs."""
    from tandem import runner as runner_mod

    _flipping_switch(monkeypatch)
    monkeypatch.setattr(runner_mod, "InteractiveRunner", runner)
    monkeypatch.setattr(runner_mod, "_stdin_tty", lambda: tty)
    FakeInteractiveRunner.script = list(script)
    FakeInteractiveRunner.seen = []
    flip.run_session(sess.tandem_id, None)
    return FakeInteractiveRunner.seen


def test_flip_reprints_the_runners_reports_after_the_clear(sess, capsys, monkeypatch):
    """The flip clears the screen; a sync error the user never gets to read is
    the same as no sync error at all, so the held-back lines print after it."""
    lines = [
        "tandem: sync error: transcript shrank",
        "tandem: status bar disabled for this session (terminal conflict)",
    ]
    seen = _fake_runner_session(
        monkeypatch, sess, [(lines, True), ([], False)]
    )
    out = capsys.readouterr().out
    assert seen == ["claude", "codex"]        # the flip really happened
    for line in lines:
        assert out.count(line) == 1           # shown once, on the fresh screen


def test_non_flip_exit_prints_its_reports_once(sess, capsys, monkeypatch):
    """No flip, no clear: the runner prints them itself and the flip loop must
    not print a second copy."""
    lines = ["tandem: sync error: transcript shrank"]
    seen = _fake_runner_session(monkeypatch, sess, [(lines, False)])
    out = capsys.readouterr().out
    assert seen == ["claude"]
    assert out.count(lines[0]) == 1


def test_int_returning_run_harness_still_works(sess):
    """Legacy seam: a plain int means no flip."""

    def run_harness(session):
        return 7

    code = flip.run_session(sess.tandem_id, None, run_harness=run_harness)
    assert code == 7


def test_flip_reports_switch_outcome(sess, capsys, monkeypatch):
    """Display names, memory actions and the doctor advisory — the flip is
    now the only switch path, so it must not report less than the old
    one-shot `tandem switch` did."""

    class Mem:
        actions = ["wrote shared block into AGENTS.md"]
        warnings = ["CLAUDE.md has no tandem markers; read-only"]

    problems = ["transcript for newly active harness does not exist yet"]

    def fake_switch(store, session, to=None):
        store.set_active(session.tandem_id, "codex")
        return "codex", problems, Mem()

    monkeypatch.setattr(flip.ops, "switch_session", fake_switch)
    calls = []

    def run_harness(session):
        calls.append(session.active)
        return 0, len(calls) == 1

    flip.run_session(sess.tandem_id, None, run_harness=run_harness)
    cap = capsys.readouterr()
    assert "active harness: Claude Code -> Codex CLI" in cap.out
    assert "memory: wrote shared block into AGENTS.md" in cap.out
    assert "memory: CLAUDE.md has no tandem markers" in cap.err
    assert "transcript for newly active harness does not exist yet" in cap.err
    assert "run `tandem doctor` for details." in cap.err


class FakeStandby:
    def __init__(self, side="codex", size=10, alive=True):
        class _R:
            pass
        self.recipe = _R()
        self.recipe.side = side
        self.shadow_size = size
        self._alive = alive
        self.killed = False

    def alive(self):
        return self._alive

    def kill(self):
        self.killed = True
        self._alive = False


def _gate(monkeypatch, standby, new_active="codex", size=10, actions=None):
    from tandem import flip
    import tandem.warm as warm
    monkeypatch.setattr(warm, "_shadow_size", lambda session, side: size)
    mem = FakeMem()
    if actions:
        mem.actions = list(actions)
    return flip._standby_fresh(standby, new_active, object(), mem)


def test_gate_accepts_a_fresh_standby(monkeypatch):
    assert _gate(monkeypatch, FakeStandby(side="codex", size=10)) is True


def test_gate_rejects_dead_wrong_side_grown_or_memory(monkeypatch):
    assert _gate(monkeypatch, None) is False
    assert _gate(monkeypatch, FakeStandby(alive=False)) is False
    assert _gate(monkeypatch, FakeStandby(side="claude")) is False
    assert _gate(monkeypatch, FakeStandby(size=10), size=11) is False
    assert _gate(monkeypatch, FakeStandby(size=10),
                 actions=["wrote AGENTS.md"]) is False


class BlockingStandby(FakeStandby):
    """A standby whose kill ladder hangs, standing in for the real one: quit
    keystrokes plus TERM/KILL timeouts put 0.5-5s between `kill()` and a dead
    process."""

    def __init__(self, release, **kw):
        super().__init__(**kw)
        self.release = release
        self.kill_entered = threading.Event()

    def kill(self):
        self.kill_entered.set()
        self.release.wait(10)
        super().kill()


def test_stale_standby_is_killed_at_the_gate(sess, monkeypatch, capsys):
    from tandem import flip
    import tandem.warm as warm
    _flipping_switch(monkeypatch)
    monkeypatch.setattr(warm, "_shadow_size", lambda session, side: 999)
    stale = FakeStandby(side="codex", size=10)   # snapshot != 999 -> stale
    runs = []

    def run_harness(session):
        runs.append(session.active)
        return 0, False

    carry = {"standby": stale}
    flip._switch(sess.tandem_id, run_harness, 0, carry=carry)
    for t in carry["reapers"]:   # the reap is off-thread; the exit joins it
        t.join(timeout=10)
    assert stale.killed
    assert carry["standby"] is None
    assert runs   # the flip still landed, on a cold spawn


def test_the_gates_kill_does_not_delay_the_flip(sess, monkeypatch, capsys):
    """The teardown of a stale standby costs seconds of quit-key ladder, and
    it lands on exactly the flips warmup exists to make instant. So the gate
    hands the kill to a reaper thread and re-enters immediately; the session's
    exit is what joins it."""
    import tandem.warm as warm
    monkeypatch.setattr(warm, "_shadow_size", lambda session, side: 999)
    release = threading.Event()
    stale = BlockingStandby(release, side="codex", size=10)
    adopted = []

    class Runner(_adopting_runner(adopted, stale)):
        def run(self):
            if self.session.active == "codex":
                # the post-flip run: the harness is already launching while
                # the stale standby is still going down the ladder
                assert stale.kill_entered.wait(10)
                assert not stale.killed
                release.set()
            return super().run()

    _fake_runner_session(monkeypatch, sess, [([], True), ([], False)],
                         runner=Runner)
    assert adopted == [None, None]   # stale: never handed to the next run
    assert stale.killed              # run_session's exit joined the reaper


def test_plain_exit_kills_the_leftover_standby(sess, monkeypatch, capsys):
    """No flip pending means nothing will ever adopt it: the exit path is the
    last chance to reap a hidden harness."""
    leftover = FakeStandby()

    class Runner(FakeInteractiveRunner):
        def run(self):
            self.warm_child = leftover
            return super().run()

    _fake_runner_session(monkeypatch, sess, [([], False)], runner=Runner)
    assert leftover.killed


def _adopting_runner(adopted, standby):
    """A fake runner that records what it was handed and warms `standby` on
    its first run — the flip loop must carry that into the next run."""

    class Runner(FakeInteractiveRunner):
        def __init__(self, session, sink_factory=None, adopt_child=None,
                     **kw):
            super().__init__(session, sink_factory, **kw)
            adopted.append(adopt_child)

        def run(self):
            code = super().run()
            if len(adopted) == 1:
                self.warm_child = standby
            return code

    return Runner


def test_fresh_standby_is_adopted_on_flip(sess, monkeypatch, capsys):
    import tandem.warm as warm
    monkeypatch.setattr(warm, "_shadow_size", lambda session, side: 10)
    fresh = FakeStandby(side="codex", size=10)
    adopted = []
    _fake_runner_session(monkeypatch, sess, [([], True), ([], False)],
                         runner=_adopting_runner(adopted, fresh))
    # first run adopts nothing; the post-flip run adopts the fresh standby
    assert adopted[0] is None
    assert adopted[1] is fresh
    assert not fresh.killed


def test_adopted_standby_is_not_reused_by_the_flip_back(sess, monkeypatch, capsys):
    """A consumed standby leaves the carry, so a launch failure that flips
    back cannot hand the same child to the other side."""
    import tandem.warm as warm
    monkeypatch.setattr(warm, "_shadow_size", lambda session, side: 10)
    fresh = FakeStandby(side="codex", size=10)
    adopted = []

    class Runner(_adopting_runner(adopted, fresh)):
        def run(self):
            if self.session.active == "codex":
                raise FileNotFoundError("codex: command not found")
            return super().run()

    _fake_runner_session(monkeypatch, sess, [([], True), ([], False)],
                         runner=Runner)
    assert adopted == [None, fresh, None]   # flipped, failed, flipped back cold


def test_a_non_tty_run_never_adopts_the_standby(sess, monkeypatch, capsys):
    """`run_in_pty` ignores a pre-spawned child when stdin is not a terminal,
    so handing one over would strand a hidden harness: kill it instead."""
    import tandem.warm as warm
    monkeypatch.setattr(warm, "_shadow_size", lambda session, side: 10)
    fresh = FakeStandby(side="codex", size=10)
    adopted = []
    _fake_runner_session(monkeypatch, sess, [([], True), ([], False)],
                         runner=_adopting_runner(adopted, fresh), tty=False)
    assert adopted == [None, None]
    assert fresh.killed


def test_a_run_that_raises_still_hands_its_standby_to_the_carry(
    sess, monkeypatch, capsys
):
    """The runner publishes `warm_child` from its own finally, so a run that
    raises with a flip already fired still has one. The closure must store it
    back or the last reference to a live hidden harness is dropped."""
    kept = FakeStandby()

    class Runner(FakeInteractiveRunner):
        def run(self):
            self.warm_child = kept        # the runner's finally got this far
            raise RuntimeError("terminal went away")

    _fake_runner_session(monkeypatch, sess, [([], False)], runner=Runner)
    assert kept.killed   # reached the carry, then the exit reaped it


def test_a_raising_store_still_reaps_the_leftover_standby(sess, monkeypatch, capsys):
    """The exit's bookkeeping is the last thing standing between a hidden
    harness and nobody at all: a locked sqlite (a concurrent `tandem sub`
    holds its own store) must not be the reason one survives the session."""
    leftover = FakeStandby()
    armed = []

    class Runner(FakeInteractiveRunner):
        def run(self):
            self.warm_child = leftover
            armed.append(True)   # from here the exit's store call blows up
            return super().run()

    real_store = flip.StateStore

    def guarded(*a, **kw):
        if armed:
            raise sqlite3.OperationalError("database is locked")
        return real_store(*a, **kw)

    monkeypatch.setattr(flip, "StateStore", guarded)
    with pytest.raises(sqlite3.OperationalError):
        _fake_runner_session(monkeypatch, sess, [([], False)], runner=Runner)
    assert leftover.killed
    assert f"to continue this session: tandem resume {sess.tandem_id}" in (
        capsys.readouterr().out          # the hint still came first
    )


def test_switch_ladder_tries_next_unvisited_then_falls_back(tmp_path, monkeypatch):
    """3-way cycle: codex won't launch -> try opencode; opencode won't
    launch -> land back on claude (the old active). Never ping-pongs."""
    from conftest import Env3

    env = Env3(tmp_path, monkeypatch)   # active=claude
    attempts = []

    def run_harness(session):
        attempts.append(session.active)
        raise OSError("won't start")

    from tandem.flip import _switch
    code, flipped = _switch(env.session.tandem_id, run_harness, 0, carry=None)
    # ladder: codex (next), opencode (next unvisited), back to claude
    assert attempts == ["codex", "opencode", "claude"]
    assert flipped is False


# -- mixed tab: routed switch, tab persistence, inject carry ------------------


def _carry():
    return {"standby": None, "reapers": []}


def test_switch_honors_target_override(sess3, monkeypatch):
    """`to` beats the cycle: from claude, next_active is codex, but a tab
    press (or a routed request) aimed at opencode lands on opencode."""
    _flipping_switch(monkeypatch)
    log = []
    code, flipped = flip._switch(sess3.tandem_id, fake_runner(log), 0,
                                 to="opencode")
    assert log == ["opencode"]
    assert (code, flipped) == (0, False)
    with StateStore() as store:
        assert store.get_session(sess3.tandem_id).active == "opencode"


def test_switch_settles_tabs_and_persists_meta(sess3, monkeypatch):
    """The flip landed, so the pending move is spent and the tab/focus it
    settled into is what the next process must start in."""
    _flipping_switch(monkeypatch)
    tabs = TabState(sess3.participants, tab="mixed", focus="claude")
    assert tabs.routed("codex") is True
    flip._switch(sess3.tandem_id, fake_runner([]), 0, to="codex", tabs=tabs)
    assert tabs.tab == "mixed" and tabs.focus == "codex"
    assert tabs.pending_target() == ""       # the move was consumed
    with StateStore() as store:
        assert store.get_meta(sess3.tandem_id) == {"tab": "mixed",
                                                   "mixed_focus": "codex"}


def test_switch_without_tabs_writes_no_meta(sess, monkeypatch):
    """The pre-mixed frame is untouched: no tab state, no meta blob."""
    _flipping_switch(monkeypatch)
    flip._switch(sess.tandem_id, fake_runner([]), 0)
    with StateStore() as store:
        assert store.get_meta(sess.tandem_id) == {}


def _model_standby(model, size=10, side="codex", cwd=""):
    from tandem.warm import LaunchRecipe

    class Standby:
        recipe = LaunchRecipe(side=side, argv=[side],
                              sentinel=paths.tandem_home() / "s",
                              hook_extra=[], transcript=None, fresh=False,
                              cwd=cwd, model=model)
        shadow_size = size

        def alive(self):
            return True

    return Standby()


def test_standby_stale_when_route_model_differs(sess):
    """A standby warmed without the routed turn's model pin would launch the
    wrong thing — reject it before the shadow-size compare even runs."""
    assert flip._standby_fresh(_model_standby("", cwd=sess.cwd), "codex",
                               sess, FakeMem(),
                               route_model="gpt-5.3-codex") is False


def test_standby_fresh_when_route_model_matches(sess, monkeypatch):
    import tandem.warm as warm

    monkeypatch.setattr(warm, "_shadow_size", lambda session, side: 10)
    standby = _model_standby("gpt-5.3-codex", cwd=sess.cwd)
    assert flip._standby_fresh(standby, "codex", sess, FakeMem(),
                               route_model="gpt-5.3-codex") is True
    # and an unpinned flip never asks about the model at all
    assert flip._standby_fresh(_model_standby("", cwd=sess.cwd), "codex",
                               sess, FakeMem()) is True


def test_routed_switch_reaps_a_standby_pinned_to_another_model(sess, monkeypatch):
    """End to end through the gate: the route's model reaches `_standby_fresh`
    from the `_switch` call site, so the wrong-model child is killed rather
    than adopted."""
    import tandem.warm as warm

    _flipping_switch(monkeypatch)
    monkeypatch.setattr(warm, "_shadow_size", lambda session, side: 10)
    stale = FakeStandby(side="codex", size=10)
    stale.recipe.model = ""
    carry = dict(_carry(), standby=stale)
    req = RouteRequest("codex", "gpt-5.3-codex", "do it", "claude", "→ codex")
    flip._switch(sess.tandem_id, fake_runner([]), 0, carry=carry,
                 to="codex", route=req)
    for t in carry["reapers"]:
        t.join(timeout=10)
    assert stale.killed and carry["standby"] is None


def test_route_carry_reaches_next_run(sess, monkeypatch):
    """`_switch` puts the route back into the carry before `_try_enter`, so
    the default `run_harness` pops it as the next run's inject."""
    _flipping_switch(monkeypatch)
    seen = []

    def run_harness(session):
        seen.append(carry.pop("route", None))
        return 0, False

    carry = _carry()
    req = RouteRequest("codex", "", "do it", "claude", "→ codex")
    flip._switch(sess.tandem_id, run_harness, 0, carry=carry,
                 to="codex", route=req)
    assert seen == [req]


def test_the_ladder_neither_retargets_nor_reinjects(sess3, monkeypatch):
    """A refused target must not be re-targeted, and the prompt meant for it
    must not be typed into whoever answers instead — the injector's target
    check is the second line of defence, this is the first."""
    _flipping_switch(monkeypatch)
    injected, attempts = [], []
    req = RouteRequest("codex", "", "do it", "claude", "→ codex")

    def run_harness(session):
        attempts.append(session.active)
        injected.append(carry.pop("route", None))
        if session.active == "codex":
            raise OSError("won't start")
        return 0, False

    carry = _carry()
    flip._switch(sess3.tandem_id, run_harness, 0, carry=carry, to="codex",
                 route=req)
    assert attempts == ["codex", "opencode"]
    assert injected == [req, None]


def test_the_ladder_settles_the_tab_on_whoever_launched(sess3, monkeypatch):
    """Every rung of the ladder is a real switch, so every rung settles: the
    tab must end up pointing at the harness that actually came up, not at the
    target that refused."""
    _flipping_switch(monkeypatch)
    tabs = TabState(sess3.participants, tab="mixed", focus="claude")
    assert tabs.routed("codex") is True
    settled = []
    real_settle = tabs.settle
    tabs.settle = lambda new: settled.append(new) or real_settle(new)

    def run_harness(session):
        if session.active == "codex":
            raise OSError("won't start")
        return 0, False

    flip._switch(sess3.tandem_id, run_harness, 0, carry=_carry(), to="codex",
                 route=RouteRequest("codex", "", "do it", "claude", "→ codex"),
                 tabs=tabs)
    assert settled == ["codex", "opencode"]   # once per successful switch
    assert tabs.tab == "mixed" and tabs.focus == "opencode"
    with StateStore() as store:
        assert store.get_meta(sess3.tandem_id) == {"tab": "mixed",
                                                   "mixed_focus": "opencode"}


def test_ladder_exhaustion_leaves_the_routed_prompt_on_disk(sess3, monkeypatch):
    """Nothing would start: the route file is still there (`dispatched`), so
    the next frame start surfaces the prompt instead of losing it."""
    _flipping_switch(monkeypatch)
    req = RouteRequest("codex", "", "do it", "claude", "→ codex",
                       state="dispatched")
    routefile.write_route(sess3.tandem_id, req)

    def run_harness(session):
        raise OSError("won't start")

    code, flipped = flip._switch(sess3.tandem_id, run_harness, 0,
                                 carry=_carry(), to="codex", route=req)
    assert flipped is False
    assert routefile.read_route(sess3.tandem_id) == req


def test_flip_loop_prefers_the_press_over_a_stale_route(sess3, monkeypatch):
    """The press owns the pending slot (`TabState.routed` is a claim), so a
    stranded route request cannot retarget it. The route still rides along —
    the injector's target check is what keeps the prompt off the wrong
    harness."""
    _flipping_switch(monkeypatch)
    tabs = TabState(sess3.participants)
    assert tabs.press("claude").target == "codex"     # the user's own press
    stale = RouteRequest("opencode", "", "stale", "claude", "→ opencode")
    carry = dict(_carry(), route=stale)
    log = []
    flip._flip_loop(sess3.tandem_id, fake_runner(log), (0, True), [], carry,
                    tabs=tabs)
    assert log == ["codex"]
    assert carry["route"] is stale     # carried, for the injector to refuse


def test_flip_loop_falls_back_to_the_routes_target(sess3, monkeypatch):
    """With no pending move of its own (the mixer's claim was cleared, or the
    tab cycle is off) the route names the target."""
    _flipping_switch(monkeypatch)
    req = RouteRequest("opencode", "", "do it", "claude", "→ opencode")
    log = []
    flip._flip_loop(sess3.tandem_id, fake_runner(log), (0, True), [],
                    dict(_carry(), route=req))
    assert log == ["opencode"]


class _MixedRunner(FakeInteractiveRunner):
    """FakeInteractiveRunner that routes on its first run, the way the mixer
    thread does: claim the tab slot, then publish the request."""

    route = None
    seen_inject: list = []

    def __init__(self, session, sink_factory=None, adopt_child=None,
                 tabs=None, inject=None):
        super().__init__(session, sink_factory, adopt_child, tabs, inject)
        _MixedRunner.seen_inject.append(inject)

    def run(self):
        route = _MixedRunner.route
        if route is not None and self.tabs is not None \
                and self.tabs.routed(route.target):
            self.route_request = route
            _MixedRunner.route = None
        return super().run()


def test_run_session_threads_the_tab_state_and_the_route(sess, monkeypatch):
    """The whole loop through `run_session`'s own closure: restore the tab
    from meta, hand it and the route to the runner, persist what it settled
    into."""
    with StateStore() as store:
        store.set_meta(sess.tandem_id, {"tab": "mixed",
                                        "mixed_focus": "claude"})
    req = RouteRequest("codex", "", "do it", "claude", "→ codex")
    _MixedRunner.route, _MixedRunner.seen_inject = req, []
    seen = _fake_runner_session(monkeypatch, sess, [([], True), ([], False)],
                                runner=_MixedRunner)
    assert seen == ["claude", "codex"]
    assert _MixedRunner.seen_inject == [None, req]   # delivered to its target
    with StateStore() as store:
        assert store.get_meta(sess.tandem_id) == {"tab": "mixed",
                                                  "mixed_focus": "codex"}


def test_a_bar_move_survives_the_session_exit(sess, monkeypatch):
    """Entering the mixed tab is a *bar* move: no flip, so `_switch` never
    runs and nothing settles. Only the post-run persist can carry it to the
    next `tandem resume` — without it the user comes back to the harness tab
    they explicitly left."""

    class Runner(FakeInteractiveRunner):
        def run(self):
            if self.session.active == "codex":
                # what the pump does on the press: codex is last in the cycle
                # and the focus is already here, so only the tab changes
                assert self.tabs.press("codex").kind == "bar"
            return super().run()

    _fake_runner_session(monkeypatch, sess, [([], True), ([], False)],
                         runner=Runner)
    with StateStore() as store:
        assert store.get_meta(sess.tandem_id) == {"tab": "mixed",
                                                  "mixed_focus": "codex"}


def test_a_locked_store_at_startup_degrades_to_the_pre_mixed_frame(
    sess, capsys, monkeypatch
):
    """A concurrent `tandem sub` holds its own store. Reading the tab state
    must never be the reason a session dies: no tab cycle beats no session."""
    real_store, calls = flip.StateStore, []

    def guarded(*a, **kw):
        calls.append(1)
        if len(calls) == 1:            # `_tab_state`'s open, and only it
            raise sqlite3.OperationalError("database is locked")
        return real_store(*a, **kw)

    monkeypatch.setattr(flip, "StateStore", guarded)
    log = []
    code = flip.run_session(sess.tandem_id, None, run_harness=fake_runner(log))
    assert (code, log) == (0, ["claude"])
    assert f"to continue this session: tandem resume {sess.tandem_id}" in (
        capsys.readouterr().out
    )


def test_a_locked_store_at_startup_still_stamps_the_frame_file(sess, monkeypatch):
    """The degraded start runs no mixer either, so the hook has to be told.
    Left saying `mixed`, the frame file makes it block and stash a routed
    prompt nothing will ever pick up — and a `pending` leftover is cleared
    *silently* at the next mixed start (only `dispatched` earns the preserved
    note), so the prompt is destroyed after the user was told it went to
    another harness."""
    routefile.write_frame_state(sess.tandem_id, {"tab": "mixed",
                                                 "focus": "claude",
                                                 "routing_ok": True})
    real_store, calls = flip.StateStore, []

    def guarded(*a, **kw):
        calls.append(1)
        if len(calls) == 1:            # `_tab_state`'s open, and only it
            raise sqlite3.OperationalError("database is locked")
        return real_store(*a, **kw)

    monkeypatch.setattr(flip, "StateStore", guarded)
    log = []
    flip.run_session(sess.tandem_id, None, run_harness=fake_runner(log))
    assert log == ["claude"]           # the session still ran
    assert routefile.read_frame_state(sess.tandem_id) == {
        "tab": "harness", "focus": "", "routing_ok": False}


def test_run_session_stamps_a_stale_frame_file_when_mixed_is_off(sess):
    """`[frame] mixed = false` with a leftover `tab: mixed` file on disk: the
    hook reads the file, not the config, and would keep stashing prompts for
    a mixer that no longer runs."""
    routefile.write_frame_state(sess.tandem_id, {"tab": "mixed",
                                                 "focus": "claude",
                                                 "routing_ok": True})
    (paths.tandem_home() / "config.toml").write_text("[frame]\nmixed = false\n")
    flip.run_session(sess.tandem_id, None, run_harness=fake_runner([]))
    assert routefile.read_frame_state(sess.tandem_id) == {
        "tab": "harness", "focus": "", "routing_ok": False}


def test_run_session_leaves_the_frame_file_to_the_mixer_when_mixed_is_on(sess):
    """With the tab cycle live the runner's mixer owns that file *while a run
    is up*; a second writer in the flip loop would fight it. Only the exit
    stamp writes it from here, and only once every mixer is gone."""
    seeded = {"tab": "mixed", "focus": "claude", "routing_ok": True}
    routefile.write_frame_state(sess.tandem_id, seeded)
    seen = []

    def run_harness(session):
        seen.append(routefile.read_frame_state(sess.tandem_id))
        return 0

    flip.run_session(sess.tandem_id, None, run_harness=run_harness)
    assert seen == [seeded]        # untouched for as long as the run owns it


def test_run_session_stamps_the_frame_file_on_exit(sess):
    """Exiting from the mixed tab leaves the frame file saying `mixed`, and
    the hook's identity gate does not care that tandem is gone: resuming the
    same native session outside tandem (`claude -r`, same session id) makes
    it block and stash prompts with no frame alive to pick them up."""
    routefile.write_frame_state(sess.tandem_id, {"tab": "mixed",
                                                 "focus": "claude",
                                                 "routing_ok": True})
    flip.run_session(sess.tandem_id, None, run_harness=fake_runner([]))
    assert routefile.read_frame_state(sess.tandem_id) == {
        "tab": "harness", "focus": "", "routing_ok": False}


def test_the_exit_stamp_lands_after_a_flip_too(sess, monkeypatch):
    """Not only on the one-run path: a session that flipped its way to the
    mixed tab is exactly the one that leaves a `mixed` file behind."""
    _flipping_switch(monkeypatch)
    routefile.write_frame_state(sess.tandem_id, {"tab": "mixed",
                                                 "focus": "claude",
                                                 "routing_ok": True})
    log = []
    flip.run_session(sess.tandem_id, None,
                     run_harness=fake_runner(log, codes=[(0, True), (0, False)]))
    assert log == ["claude", "codex"]
    assert routefile.read_frame_state(sess.tandem_id) == {
        "tab": "harness", "focus": "", "routing_ok": False}
