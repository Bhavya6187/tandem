"""PTY passthrough: run the real harness CLI with its untouched native UX.

The child gets a real pty (raw-mode stdin, terminal resize via SIGWINCH
forwarding, control bytes delivered through the pty line discipline). Tandem
never reads meaning from the terminal stream — transcript files are the
source of truth — except the frame's enumerated sequences (flip byte, paste
markers, mouse, and the output guard's reset set) — so this module just pumps
bytes.
"""

from __future__ import annotations

import io
import os
import select
import signal
import struct
import subprocess
import sys
import termios
import threading
import time
import tty
from dataclasses import dataclass
from typing import Callable

from ptyprocess import PtyProcess

from .frame import FlipDetector, OutputGuard, StatusBar


def _winsize(fd: int) -> tuple[int, int]:
    try:
        import fcntl

        packed = fcntl.ioctl(fd, termios.TIOCGWINSZ, struct.pack("HHHH", 0, 0, 0, 0))
        rows, cols = struct.unpack("HHHH", packed)[:2]
        return (rows or 24, cols or 80)
    except OSError:
        return (24, 80)


# seconds of keyboard quiet after which the detector's carried escape
# fragment is released to the child (a lone ESC must not wait for a key)
_IDLE_FLUSH_S = 0.2


def _write_all(fd: int, data: bytes) -> None:
    """Write every byte. os.write comes back short when a signal lands
    mid-write — PEP 475 only retries a write that transferred nothing — and
    this module writes under SIGWINCH by design, so a plain os.write can
    strand half a repaint (raw escape bytes) on the user's screen."""
    view = memoryview(data)
    while view:
        try:
            n = os.write(fd, view)
        except InterruptedError:
            continue
        if n <= 0:
            break
        view = view[n:]


def _is_alive(child) -> bool:
    """isalive() is not thread-safe: it reaps via waitpid, so whichever of the
    pump thread and the terminating thread loses the race gets ECHILD back as a
    PtyProcessError. A child we can no longer ask about is a child that is
    gone."""
    try:
        return child.isalive()
    except Exception:
        return False


def _wait_dead(child, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _is_alive(child):
            return True
        time.sleep(0.05)
    return not _is_alive(child)


class PtyControl:
    """Cross-thread termination handle. run_in_pty attaches the live child;
    terminate() runs the escalation ladder from any other thread: soft quit
    keystrokes (the CLI finalizes its own transcript), SIGTERM to the
    process group, SIGKILL. Every rung tolerates a child that is already
    gone."""

    def __init__(self):
        self._child = None
        self._attached = threading.Event()

    def attach(self, child) -> None:
        self._child = child
        self._attached.set()

    def terminate(
        self,
        soft: list[bytes],
        soft_timeout: float = 3.0,
        term_timeout: float = 2.0,
        attach_timeout: float = 5.0,
    ) -> str:
        self._attached.wait(timeout=attach_timeout)
        child = self._child
        if child is None or not _is_alive(child):
            return "dead"
        for chunk in soft:
            try:
                child.write(chunk)
            except Exception:
                break
            time.sleep(0.25)
        if _wait_dead(child, soft_timeout):
            return "soft"
        # ptyprocess spawns the child with setsid, so the child's pid is its
        # process-group id — killpg(child.pid, …) takes the harness's whole
        # tool-child tree down with it.
        try:
            os.killpg(child.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        if _wait_dead(child, term_timeout):
            return "term"
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        _wait_dead(child, 1.0)
        return "kill"


@dataclass
class FrameIO:
    """Frame wiring for run_in_pty, built by the runner. The pump
    constructs the detector/guard/bar internally from these fields;
    `bar_dropped` reports back that the bar was disabled over a *terminal
    conflict* — the one drop cause worth remembering across sessions. A bar
    dropped because the window got short is not recorded: the user can see
    their own window."""

    flip_byte: int
    on_flip: Callable[[], None]
    armed: Callable[[], bool]
    bar: bool = True
    active: str = ""
    other: str = ""
    bar_dropped: bool = False


def _child_dims(rows: int, cols: int, bar_on: bool) -> tuple[int, int]:
    """The winsize lie: with the bar on, the child gets one row fewer and
    tandem owns the real bottom row."""
    return (rows - 1 if bar_on else rows, cols)


def _bar_on(frame: FrameIO | None, rows: int) -> bool:
    """Bar policy, re-run on every resize: below the row floor the reserved
    row costs the child more than the bar is worth (and at rows<2 the lie
    would leave it no rows at all)."""
    return frame is not None and frame.bar and rows >= 5


def run_in_pty(
    argv: list[str],
    cwd: str | None = None,
    env: dict | None = None,
    frame: FrameIO | None = None,
    control: PtyControl | None = None,
) -> int:
    """Run argv on a pty, mirroring the controlling terminal. Returns the
    child's exit status. Falls back to a plain subprocess when stdin is not
    a tty (tests, pipes). With `frame`, tandem reserves the bottom row for
    the status bar and watches for the flip keybind; with `control`, the
    child is attached for cross-thread termination."""
    try:
        stdin_fd = sys.stdin.fileno()
        is_tty = os.isatty(stdin_fd)
    except (ValueError, OSError, io.UnsupportedOperation):
        is_tty = False
    if not is_tty:
        return subprocess.run(argv, cwd=cwd, env=env).returncode

    rows, cols = _winsize(stdin_fd)
    bar_on = _bar_on(frame, rows)
    detector = (
        FlipDetector(frame.flip_byte, bar_row=rows if bar_on else None)
        if frame
        else None
    )
    guard = OutputGuard() if bar_on else None
    bar = StatusBar(rows, cols, frame.active, frame.other) if bar_on else None

    child = PtyProcess.spawn(
        argv,
        cwd=cwd,
        env=env or dict(os.environ),
        dimensions=_child_dims(rows, cols, bar_on),
    )
    # attach before anything can block: terminate() reads a missing child as
    # "dead", so a late attach would report death on a live harness.
    if control is not None:
        control.attach(child)

    out_fd = sys.stdout.fileno()

    def paint() -> None:
        if bar is not None:
            _write_all(out_fd, bar.region() + bar.paint(frame.armed()))

    def drop_bar(reason: str) -> None:
        """Terminal state: the guard's drop verdict is not latched, so the
        pump latches it here by tearing the bar down for good.

        `reason` decides what is *recorded*, not what is torn down — the
        teardown is identical and equally permanent either way (the bar does
        not come back when the window grows again; the drop is session-
        scoped by design).

        - "conflict": the child asserted its own scroll region, so tandem and
          the harness are fighting over the same rows. That is a real,
          per-terminal incompatibility the user may want to settle with
          `[frame] bar = false`, so it leaves the marker `doctor` reads.
        - "shrunk": tandem's own row-floor policy fired because the window is
          below the height the reserved row is worth. Nothing is wrong,
          nothing is worth fixing, and the cause is visible on screen —
          recording it would make `doctor` nag about a resize."""
        nonlocal bar_on, guard, bar
        # Claim the bar first, ask questions second. Drops and resizes are
        # causally correlated (a child sets DECSTBM *because* the terminal
        # resized, and a drag-resize bursts SIGWINCH), so a SIGWINCH landing
        # anywhere below must already see bar is None. Reading the guard
        # before the swap leaves a one-statement window where a reentrant
        # call passes the guard, swaps the bar out from under this one, and
        # leaves `dying` None here — an AttributeError escaping the pump and
        # orphaning the child. It also has to be None before the writes
        # below, or the handler repaints region and bar *after* the clear
        # with nothing left to remove them, corrupting the terminal past
        # tandem's exit.
        dying, bar = bar, None
        if dying is None:
            return
        bar_on, guard = False, None
        if reason == "conflict":
            frame.bar_dropped = True
        if detector is not None:
            detector.bar_row = None
        _write_all(out_fd, dying.clear())
        r, c = _winsize(stdin_fd)
        try:
            child.setwinsize(r, c)
        except Exception:
            pass

    def on_winch(signum, frm):
        # runs as a signal handler in the pump thread: an escaping exception
        # would surface at whatever bytecode it interrupted, so swallow.
        try:
            r, c = _winsize(stdin_fd)
            if bar is not None and not _bar_on(frame, r):
                # too short now; drop_bar restores the full winsize
                drop_bar("shrunk")
                return
            if bar is not None:
                bar.resize(r, c)
                if detector is not None:
                    detector.bar_row = r
            try:
                child.setwinsize(*_child_dims(r, c, bar_on))
            except Exception:
                pass
            paint()
        except Exception:
            pass

    def on_term(signum, frm):
        try:
            child.kill(signal.SIGTERM)
        except Exception:
            pass

    old_winch = signal.signal(signal.SIGWINCH, on_winch)
    old_term = signal.signal(signal.SIGTERM, on_term)
    old_attrs = termios.tcgetattr(stdin_fd)
    try:
        tty.setraw(stdin_fd)
        paint()
        # seeded from the state just painted, so an already-armed frame does
        # not draw a redundant repaint on the first iteration
        last_armed = frame.armed() if bar is not None else False
        last_stdin = time.monotonic()
        stdin_open = True
        # _is_alive, not isalive(): a PtyControl on another thread polls
        # liveness too, and the loser of the waitpid race gets ECHILD as a
        # PtyProcessError — which must not escape the pump.
        while _is_alive(child):
            rlist = [child.fd] + ([stdin_fd] if stdin_open else [])
            try:
                ready, _, _ = select.select(rlist, [], [], 0.2)
            except InterruptedError:
                continue  # signal (e.g. SIGWINCH) — loop again
            if (
                detector is not None
                and stdin_fd not in ready
                and time.monotonic() - last_stdin >= _IDLE_FLUSH_S
            ):
                # Idle *keyboard*, not idle loop: gating on an empty select
                # would never fire against a child that streams (a spinner is
                # enough), and streaming output is exactly when the user
                # reaches for ESC. Release the fragment the detector is
                # holding so a lone ESC lands without a second keypress.
                stranded = detector.flush()
                if stranded:
                    try:
                        child.write(stranded)
                    except Exception:
                        pass  # child gone; the liveness check ends the pump
            if child.fd in ready:
                try:
                    data = child.read(65536)
                except EOFError:
                    break
                if not data:
                    break
                _write_all(out_fd, data)
                if guard is not None:
                    verdict = guard.feed(data)
                    if verdict == "drop":
                        drop_bar("conflict")
                    elif verdict == "reassert":
                        paint()
            if stdin_open and stdin_fd in ready:
                data = os.read(stdin_fd, 65536)
                last_stdin = time.monotonic()
                if data:
                    if detector is not None:
                        data, flips = detector.feed(data)
                        for _ in range(flips):
                            frame.on_flip()
                    if data:
                        child.write(data)
                else:
                    if detector is not None:
                        # anything still carried belongs before the EOF
                        stranded = detector.flush()
                        if stranded:
                            try:
                                child.write(stranded)
                            except Exception:
                                pass
                    child.sendeof()
                    stdin_open = False
            if bar is not None and frame.armed() != last_armed:
                last_armed = frame.armed()
                paint()
    finally:
        # SIGWINCH goes back first: its handler is the one that paints, and
        # both the clear and tcsetattr (which blocks until the tty drains)
        # are long windows for it to land in — a repaint after the final
        # clear would outlive tandem with nothing left to remove it.
        # Restoring the handler shuts that window, where clearing `bar`
        # afterwards would still race the write itself. SIGTERM's handler
        # only signals the child, never the terminal, so it stays installed
        # until the terminal is whole again.
        signal.signal(signal.SIGWINCH, old_winch)
        if bar is not None:
            _write_all(out_fd, bar.clear())
        termios.tcsetattr(stdin_fd, termios.TCSADRAIN, old_attrs)
        signal.signal(signal.SIGTERM, old_term)
    try:
        child.wait()
    except Exception:
        pass
    return child.exitstatus if child.exitstatus is not None else 1
