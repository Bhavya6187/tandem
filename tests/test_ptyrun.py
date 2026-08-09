import sys

from tandem.ptyrun import PtyControl, run_in_pty


def test_non_tty_fallback_runs_subprocess(capfd):
    # pytest runs with a non-tty stdin, exercising the subprocess fallback.
    code = run_in_pty([sys.executable, "-c", "print('pty-ok')"])
    assert code == 0
    assert "pty-ok" in capfd.readouterr().out


def test_exit_code_propagates():
    assert run_in_pty([sys.executable, "-c", "raise SystemExit(3)"]) == 3


class _StubChild:
    """Stands in for a PtyProcess: records writes, dies on command."""

    def __init__(self, dies_after_writes=None, dies_on_signal=None):
        self.pid = 99999999  # killpg will fail -> ladder must survive that
        self.writes = []
        self._alive = True
        self._dies_after_writes = dies_after_writes
        self._dies_on_signal = dies_on_signal

    def write(self, data):
        self.writes.append(data)
        if self._dies_after_writes and len(self.writes) >= self._dies_after_writes:
            self._alive = False

    def isalive(self):
        return self._alive

    def kill_externally(self):
        self._alive = False


def test_terminate_soft_exit():
    c = PtyControl()
    child = _StubChild(dies_after_writes=2)
    c.attach(child)
    how = c.terminate([b"\x03", b"\x04"], soft_timeout=1.0, term_timeout=0.2)
    assert how == "soft"
    assert child.writes == [b"\x03", b"\x04"]


def test_terminate_already_dead():
    c = PtyControl()
    child = _StubChild()
    child.kill_externally()
    c.attach(child)
    assert c.terminate([b"\x04"], soft_timeout=0.2) == "dead"


def test_terminate_never_attached_returns_dead():
    c = PtyControl()
    assert c.terminate([b"\x04"], soft_timeout=0.1, attach_timeout=0.1) == "dead"


def test_terminate_escalates_past_failing_killpg(monkeypatch):
    # killpg raising (fake pid) must not break the ladder; the child dying
    # during the term wait is still detected
    c = PtyControl()
    child = _StubChild()
    c.attach(child)

    import tandem.ptyrun as ptyrun

    def fake_killpg(pgid, sig):
        child.kill_externally()

    monkeypatch.setattr(ptyrun.os, "killpg", fake_killpg)
    how = c.terminate([b"\x03"], soft_timeout=0.3, term_timeout=1.0)
    assert how == "term"


class _ReapedChild(_StubChild):
    """A child the pump thread already reaped: ptyprocess raises
    PtyProcessError from isalive() when waitpid comes back ECHILD."""

    def __init__(self, alive_checks_before_raise=0):
        super().__init__()
        self._checks = alive_checks_before_raise

    def isalive(self):
        if self._checks <= 0:
            raise RuntimeError("no child process — someone else called waitpid()")
        self._checks -= 1
        return True


def test_terminate_treats_unqueryable_child_as_dead():
    # the other thread reaped the child before terminate even looked
    c = PtyControl()
    c.attach(_ReapedChild())
    assert c.terminate([b"\x04"], soft_timeout=0.2, term_timeout=0.2) == "dead"


def test_terminate_survives_reap_race_mid_ladder():
    # child looks alive at the gate, then the pump thread reaps it while the
    # ladder is waiting — that must read as death, not blow up the ladder
    c = PtyControl()
    c.attach(_ReapedChild(alive_checks_before_raise=1))
    assert c.terminate([b"\x04"], soft_timeout=0.2, term_timeout=0.2) == "soft"
