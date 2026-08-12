"""Flip loop tests with a fake harness runner."""

import sqlite3
import threading

import pytest

from tandem import flip, paths
from tandem.state import StateStore


class FakeMem:
    actions: list = []
    warnings: list = []


@pytest.fixture
def sess(tmp_path, monkeypatch):
    monkeypatch.setenv("TANDEM_HOME", str(tmp_path / ".tandem"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / ".codex"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / ".claude"))
    with StateStore() as store:
        return store.create_session(str(tmp_path / "proj"), "claude", "c-id", "x-id")


def fake_runner(log, codes=None):
    codes = list(codes or [])

    def run_harness(session):
        log.append(session.active)
        return codes.pop(0) if codes else 0

    return run_harness


def _flipping_switch(monkeypatch):
    def fake_switch(store, session):
        new = "codex" if session.active == "claude" else "claude"
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

    def boom(tandem_id, run_harness, first, reports, carry):
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

    def boom(store, session):
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

    def boom(store, session):
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

    def __init__(self, session, sink_factory=None, adopt_child=None):
        self.session = session
        self.reports = []
        self.flip_requested = False
        self.adopt_child = adopt_child
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

    def fake_switch(store, session):
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
        def __init__(self, session, sink_factory=None, adopt_child=None):
            super().__init__(session, sink_factory)
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
