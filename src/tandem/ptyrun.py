"""PTY passthrough: run the real harness CLI with its untouched native UX.

The child gets a real pty (raw-mode stdin, terminal resize via SIGWINCH
forwarding, control bytes delivered through the pty line discipline). Tandem
never reads meaning from the terminal stream — transcript files are the
source of truth — so this module just pumps bytes.
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

from ptyprocess import PtyProcess


def _winsize(fd: int) -> tuple[int, int]:
    try:
        import fcntl

        packed = fcntl.ioctl(fd, termios.TIOCGWINSZ, struct.pack("HHHH", 0, 0, 0, 0))
        rows, cols = struct.unpack("HHHH", packed)[:2]
        return (rows or 24, cols or 80)
    except OSError:
        return (24, 80)


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


def run_in_pty(argv: list[str], cwd: str | None = None, env: dict | None = None) -> int:
    """Run argv on a pty, mirroring the controlling terminal. Returns the
    child's exit status. Falls back to a plain subprocess when stdin is not a
    tty (tests, pipes)."""
    try:
        stdin_fd = sys.stdin.fileno()
        is_tty = os.isatty(stdin_fd)
    except (ValueError, OSError, io.UnsupportedOperation):
        is_tty = False
    if not is_tty:
        return subprocess.run(argv, cwd=cwd, env=env).returncode

    child = PtyProcess.spawn(
        argv, cwd=cwd, env=env or dict(os.environ), dimensions=_winsize(stdin_fd)
    )

    def on_winch(signum, frame):
        rows, cols = _winsize(stdin_fd)
        try:
            child.setwinsize(rows, cols)
        except Exception:
            pass

    def on_term(signum, frame):
        try:
            child.kill(signal.SIGTERM)
        except Exception:
            pass

    old_winch = signal.signal(signal.SIGWINCH, on_winch)
    old_term = signal.signal(signal.SIGTERM, on_term)
    old_attrs = termios.tcgetattr(stdin_fd)
    try:
        tty.setraw(stdin_fd)
        stdin_open = True
        while child.isalive():
            rlist = [child.fd] + ([stdin_fd] if stdin_open else [])
            try:
                ready, _, _ = select.select(rlist, [], [], 0.2)
            except InterruptedError:
                continue  # signal (e.g. SIGWINCH) — loop again
            if child.fd in ready:
                try:
                    data = child.read(65536)
                except EOFError:
                    break
                if not data:
                    break
                os.write(sys.stdout.fileno(), data)
            if stdin_open and stdin_fd in ready:
                data = os.read(stdin_fd, 65536)
                if data:
                    child.write(data)
                else:
                    child.sendeof()
                    stdin_open = False
    finally:
        termios.tcsetattr(stdin_fd, termios.TCSADRAIN, old_attrs)
        signal.signal(signal.SIGWINCH, old_winch)
        signal.signal(signal.SIGTERM, old_term)
    try:
        child.wait()
    except Exception:
        pass
    return child.exitstatus if child.exitstatus is not None else 1
