import fcntl
import os
import pty
import signal
import struct
import sys
import termios
import threading
import time

import tandem.ptyrun as ptyrun
from tandem.ptyrun import FrameIO, PtyControl, _bar_on, _child_dims, run_in_pty


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


def test_child_dims_reserves_bottom_row_when_bar_on():
    assert _child_dims(40, 120, bar_on=True) == (39, 120)
    assert _child_dims(40, 120, bar_on=False) == (40, 120)


def test_bar_activation_policy():
    frame = FrameIO(flip_byte=0x1D, on_flip=lambda: None, armed=lambda: False)
    assert _bar_on(frame, rows=40) is True
    assert _bar_on(frame, rows=4) is False      # too small
    assert _bar_on(None, rows=40) is False      # no frame wiring
    frame_off = FrameIO(
        flip_byte=0x1D, on_flip=lambda: None, armed=lambda: False, bar=False
    )
    assert _bar_on(frame_off, rows=40) is False  # [frame] bar = false


def test_bar_policy_leaves_the_child_at_least_one_row_at_every_size():
    # on_winch re-runs _bar_on precisely so the winsize lie can never hand the
    # child a 0-row terminal: wherever the policy allows the bar, the reserved
    # row still leaves the child rows to live on; where it doesn't, the child
    # gets the terminal whole.
    frame = FrameIO(flip_byte=0x1D, on_flip=lambda: None, armed=lambda: False)
    for rows in range(0, 12):
        if _bar_on(frame, rows):
            assert _child_dims(rows, 80, bar_on=True)[0] >= 1
        else:
            assert _child_dims(rows, 80, bar_on=False) == (rows, 80)


def test_frame_io_defaults():
    frame = FrameIO(flip_byte=0x1D, on_flip=lambda: None, armed=lambda: False)
    assert frame.bar is True
    assert (frame.active, frame.other) == ("", "")
    assert frame.key_label == "^]"
    assert frame.bar_dropped is False


def test_non_tty_fallback_ignores_frame_and_control(capfd):
    from tandem.ptyrun import PtyControl

    frame = FrameIO(flip_byte=0x1D, on_flip=lambda: None, armed=lambda: False)
    code = run_in_pty(
        [sys.executable, "-c", "print('fallback-ok')"],
        frame=frame,
        control=PtyControl(),
    )
    assert code == 0
    assert "fallback-ok" in capfd.readouterr().out
    assert frame.bar_dropped is False        # nothing to drop off-tty


def test_write_all_finishes_a_short_write(monkeypatch):
    # os.write returns a short count when a signal lands mid-write (PEP 475
    # only retries a write that moved nothing), so a repaint interrupted by
    # SIGWINCH would otherwise leave half an escape sequence on screen
    import tandem.ptyrun as ptyrun

    seen = []

    def fake_write(fd, data):
        b = bytes(data)
        seen.append(b[:3])
        return min(3, len(b))

    monkeypatch.setattr(ptyrun.os, "write", fake_write)
    ptyrun._write_all(7, b"abcdefgh")
    assert b"".join(seen) == b"abcdefgh"


def test_write_all_retries_after_interrupted_error(monkeypatch):
    import tandem.ptyrun as ptyrun

    calls = []

    def fake_write(fd, data):
        calls.append(bytes(data))
        if len(calls) == 1:
            raise InterruptedError
        return len(data)

    monkeypatch.setattr(ptyrun.os, "write", fake_write)
    ptyrun._write_all(7, b"xy")
    assert calls == [b"xy", b"xy"]


def test_write_all_gives_up_on_a_zero_byte_write(monkeypatch):
    # a fd that accepts nothing must not spin the pump forever
    import tandem.ptyrun as ptyrun

    monkeypatch.setattr(ptyrun.os, "write", lambda fd, data: 0)
    ptyrun._write_all(7, b"abc")   # returns instead of hanging


# -- driving the real pump ------------------------------------------------
#
# The bar teardown paths only exist inside run_in_pty's closure, and they
# only run against a tty. These give the pump a real pty for stdin (so
# tcgetattr/setraw/ioctl are honest) and a stub child whose "fd" is a pipe
# the test writes into, which is enough to exercise the output guard, the
# SIGWINCH handler, and drop_bar end to end.


class _FakeStdin:
    def __init__(self, fd):
        self._fd = fd

    def fileno(self):
        return self._fd


class _PumpChild:
    """Stub PtyProcess whose `fd` is a real pipe the test feeds. Closing the
    write end is how a test ends the pump (read -> EOF -> loop breaks)."""

    def __init__(self):
        self.rd, self.wr = os.pipe()
        self.pid = os.getpid()
        self.exitstatus = 0
        self.writes = []
        self.winsizes = []

    @property
    def fd(self):
        return self.rd

    def read(self, n):
        data = os.read(self.rd, n)
        if not data:
            raise EOFError
        return data

    def write(self, data):
        self.writes.append(bytes(data))

    def isalive(self):
        return True

    def setwinsize(self, rows, cols):
        self.winsizes.append((rows, cols))

    def sendeof(self):
        pass

    def wait(self):
        return 0


class _Pump:
    def __init__(self, monkeypatch, frame=None, rows=24, cols=80):
        self.frame = frame or FrameIO(
            flip_byte=0x1D, on_flip=lambda: None, armed=lambda: False
        )
        self.master, self.slave = pty.openpty()
        self.child = _PumpChild()
        self.writes: list[bytes] = []
        self.painted = threading.Event()
        self.on_write = None
        self.set_rows(rows, cols)
        monkeypatch.setattr(ptyrun, "_write_all", self._record)
        monkeypatch.setattr(ptyrun.sys, "stdin", _FakeStdin(self.slave))
        monkeypatch.setattr(
            ptyrun, "PtyProcess",
            type("_Spawner", (), {"spawn": staticmethod(lambda *a, **kw: self.child)}),
        )

    def _record(self, fd, data):
        data = bytes(data)
        self.writes.append(data)
        self.painted.set()      # the first paint proves the pump is running
        if self.on_write is not None:
            self.on_write(data)

    def set_rows(self, rows, cols=80):
        fcntl.ioctl(
            self.slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0)
        )

    def feed(self, data: bytes) -> None:
        os.write(self.child.wr, data)

    def winch(self, rows, cols=80):
        self.set_rows(rows, cols)
        os.kill(os.getpid(), signal.SIGWINCH)   # delivered to the pump thread

    def await_clear(self, timeout=3.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if any(b"\x1b[2K" in w for w in self.writes):
                return True
            time.sleep(0.02)
        return False

    def run(self, drive) -> int:
        def driver():
            try:
                assert self.painted.wait(timeout=5)
                drive()
            finally:
                os.close(self.child.wr)     # EOF: ends the pump
        thread = threading.Thread(target=driver, daemon=True)
        thread.start()
        try:
            return run_in_pty(["fake-harness"], frame=self.frame)
        finally:
            thread.join(timeout=5)
            for fd in (self.master, self.slave, self.child.rd):
                try:
                    os.close(fd)
                except OSError:
                    pass


def test_pump_drops_the_bar_when_the_child_region_covers_the_bar_row(monkeypatch):
    pump = _Pump(monkeypatch)

    def drive():
        pump.feed(b"\x1b[1;24r")            # DECSTBM down to the real bottom row
        assert pump.await_clear()

    assert pump.run(drive) == 0
    assert pump.frame.bar_dropped is True
    # the bar's row is handed back to the child at the full terminal height
    assert pump.child.winsizes[-1] == (24, 80)


def test_pump_keeps_the_bar_when_the_child_region_stays_above_it(monkeypatch):
    """codex asserts \\x1b[1;<its rows-2>r at startup for its composer. With
    the winsize lie the region bottom is below the real bottom row, so the
    bar is untouched: no drop, and the repaint that follows must not stomp
    the child's region with tandem's own (\\x1b[1;23r at 24 rows)."""
    pump = _Pump(monkeypatch)

    def drive():
        before = len(pump.writes)
        pump.feed(b"\x1b[1;22r")
        deadline = time.time() + 3
        while time.time() < deadline:
            if any(b"\x1b[7m" in w for w in pump.writes[before:]):
                break
            time.sleep(0.02)
        assert any(b"\x1b[7m" in w for w in pump.writes[before:])   # repainted
        assert not any(b"\x1b[2K" in w for w in pump.writes)        # never torn down
        assert not any(b"\x1b[1;23r" in w for w in pump.writes[before:])

    assert pump.run(drive) == 0
    assert pump.frame.bar_dropped is False


def test_a_window_shrunk_below_the_row_floor_drops_the_bar_but_records_nothing(
    monkeypatch,
):
    """tandem's own row-floor policy is not a terminal conflict: the bar goes
    away (permanently, for this session) but nothing is persisted, so the next
    `doctor` does not warn about a window the user simply made small."""
    pump = _Pump(monkeypatch)

    def drive():
        pump.winch(3)                       # below the 5-row floor
        assert pump.await_clear()

    assert pump.run(drive) == 0
    assert pump.frame.bar_dropped is False
    assert pump.child.winsizes[-1] == (3, 80)   # child gets the whole window


def test_a_bar_dropped_by_the_row_floor_does_not_come_back_on_grow(monkeypatch):
    """Session-scoped drop semantics are unchanged by the reason: growing the
    window back does not re-enable the bar."""
    pump = _Pump(monkeypatch)

    def drive():
        pump.winch(3)
        assert pump.await_clear()
        before = len(pump.writes)
        pump.winch(40)
        time.sleep(0.2)
        assert not any(b"\x1b[7m" in w for w in pump.writes[before:])

    assert pump.run(drive) == 0
    assert pump.frame.bar_dropped is False


def test_drop_bar_survives_a_sigwinch_landing_inside_its_own_teardown(monkeypatch):
    """Drops and resizes are causally correlated, so the SIGWINCH handler
    lands inside drop_bar's writes all the time. The bar must already be gone
    by then: a reentrant drop that still saw it would write a second clear (or
    repaint the region *after* the clear) and outlive tandem's exit."""
    pump = _Pump(monkeypatch)
    reentered = []

    def on_write(data):
        if b"\x1b[2K" in data and not reentered:
            reentered.append(True)
            pump.set_rows(3)                # a resize the drop raced
            os.kill(os.getpid(), signal.SIGWINCH)
            time.sleep(0.05)                # let the handler run to completion

    pump.on_write = on_write

    def drive():
        pump.feed(b"\x1b[1;24r")
        assert pump.await_clear()

    assert pump.run(drive) == 0             # no AttributeError escaped the pump
    assert reentered == [True]              # the reentrant path really ran
    clears = [w for w in pump.writes if b"\x1b[2K" in w]
    assert len(clears) == 1                 # torn down exactly once
    assert not any(b"\x1b[7m" in w for w in pump.writes[pump.writes.index(clears[0]):])
