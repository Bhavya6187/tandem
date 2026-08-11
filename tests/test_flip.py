"""Flip loop tests with a fake harness runner."""

import sqlite3

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

    flip.run_session(sess.tandem_id, None, run_harness=run_harness)
    cap = capsys.readouterr()
    assert "could not run the harness: FileNotFoundError" in cap.err
    assert f"to continue this session: tandem resume {sess.tandem_id}" in cap.out


def test_resume_hint_prints_even_when_the_loop_raises(sess, capsys, monkeypatch):
    """The hint is the only place the id is shown, so it must survive an
    unexpected exception escaping the loop."""

    def boom(tandem_id, run_harness, first, reports=None):
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
    assert code == 0
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

    def __init__(self, session, sink_factory=None):
        self.session = session
        self.reports = []
        self.flip_requested = False

    def run(self):
        FakeInteractiveRunner.seen.append(self.session.active)
        reports, flip_req = FakeInteractiveRunner.script.pop(0)
        self.reports = list(reports)
        self.flip_requested = flip_req
        if not flip_req:  # the real runner prints its own on a non-flip exit
            for line in self.reports:
                print(line)
        return 0


def _fake_runner_session(monkeypatch, sess, script):
    from tandem import runner as runner_mod

    _flipping_switch(monkeypatch)
    monkeypatch.setattr(runner_mod, "InteractiveRunner", FakeInteractiveRunner)
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
