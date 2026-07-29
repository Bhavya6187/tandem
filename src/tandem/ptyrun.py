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
