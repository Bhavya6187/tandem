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
from dataclasses import dataclass, field
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

    def write(self, data: bytes) -> bool:
        """Best-effort write to the attached child from any thread — the
        injector's road into a routed target. No attach wait: a caller with
        nothing attached yet has its own readiness gate, and blocking here
        would put an arbitrary stall on that thread. False for every
        failure; the caller owns retry/report."""
        child = self._child
        if child is None or not _is_alive(child):
            return False
        try:
            child.write(data)
        except Exception:
            return False
        return True

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
    others: list[str] = field(default_factory=list)
    # how `flip_byte` is spelled on the bar; the runner derives it from the
    # configured byte so a rebound key advertises itself correctly
    key_label: str = "^]"
    # live usage text for the active slot ("" hides it); read on every paint
    # and polled on the pump tick, so the tail thread publishes by plain
    # assignment into whatever this closure reads
    usage: Callable[[], str] | None = None
    # per-slot account rate-limit text (harness id → "5h 4% 7d 4%"), any
    # slot; published the same way by the rate-limit poller thread
    limits: Callable[[], dict[str, str]] | None = None
    # the tab-state snapshot for the bar's mixed slot (None = pre-mixed
    # rendering); published by TabState via plain attribute reads, so the
    # callable is cheap and safe on the pump thread
    mode: Callable[[], dict] | None = None
    # the pump's word on whether a bar is actually drawn: True once at
    # setup when it is, False when it never is (no tty, too few rows) or
    # when it drops. Anything that only exists to feed the bar (the
    # rate-limit poll) starts and stops on this.
    on_bar: Callable[[bool], None] | None = None
    bar_dropped: bool = False


def _report_bar(frame: FrameIO | None, on: bool) -> None:
    if frame is not None and frame.on_bar is not None:
        try:
            frame.on_bar(on)
        except Exception:
            pass   # a listener must never take the pump down


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
    child=None,
) -> int:
    """Run argv on a pty, mirroring the controlling terminal. Returns the
    child's exit status. Falls back to a plain subprocess when stdin is not
    a tty (tests, pipes). With `frame`, tandem reserves the bottom row for
    the status bar and watches for the flip keybind; with `control`, the
    child is attached for cross-thread termination.

    With `child` (a live, pre-spawned PtyProcess from process warmup), the
    spawn is skipped and the child is adopted: it was spawned one column
    narrow on purpose, and the attach resize below makes sure the child's
    dims really change — the kernel delivers SIGWINCH and the TUI repaints
    itself, which is the entire hidden-boot handover. A dead `child` falls
    back to a fresh spawn: the flip must land somewhere."""
    try:
        stdin_fd = sys.stdin.fileno()
        is_tty = os.isatty(stdin_fd)
    except (ValueError, OSError, io.UnsupportedOperation):
        is_tty = False
    if not is_tty:
        # A pre-spawned `child` never legitimately reaches here: the flip
        # loop's own `_stdin_tty` gate kills a standby rather than hand it to
        # a path that spawns cold and would leave it unreaped.
        _report_bar(frame, False)
        return subprocess.run(argv, cwd=cwd, env=env).returncode

    rows, cols = _winsize(stdin_fd)
    bar_on = _bar_on(frame, rows)
    detector = (
        FlipDetector(frame.flip_byte, bar_row=rows if bar_on else None)
        if frame
        else None
    )
    guard = OutputGuard(rows) if bar_on else None
    bar = (
        StatusBar(rows, cols, frame.active, frame.others, frame.key_label)
        if bar_on
        else None
    )
    if not bar_on:
        _report_bar(frame, False)   # "on" is reported after the first paint

    adopted = child is not None and _is_alive(child)
    if not adopted:
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
        # Local reads: drop_bar can null these out from under a repaint.
        # While the child owns a benign scroll region (codex pinning its
        # composer rows), only the row is repainted: re-emitting tandem's
        # own region would widen the child's out from under it, and the
        # armed-state repaint fires on every flip press.
        b, g = bar, guard
        if b is None:
            return
        region = b"" if (g is not None and g.child_owns_region) else b.region()
        usage = frame.usage() if frame.usage is not None else ""
        limits = frame.limits() if frame.limits is not None else None
        mode_now = frame.mode() if frame.mode is not None else None
        _write_all(out_fd, region + b.paint(frame.armed(), usage, limits, mode_now))

    def drop_bar(reason: str) -> None:
        """Terminal state: the guard's drop verdict is not latched, so the
        pump latches it here by tearing the bar down for good.

        `reason` decides what is *recorded*, not what is torn down — the
        teardown is identical and equally permanent either way (the bar does
        not come back when the window grows again; the drop is session-
        scoped by design).

        - "conflict": the child asserted a scroll region that covers the
          bar row (a region pinned above the bar is benign and never lands
          here), so tandem and the harness really are fighting over the
          same rows. That is a real, per-terminal incompatibility the user
          may want to settle with `[frame] bar = false`, so it leaves the
          marker `doctor` reads.
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
        _report_bar(frame, False)
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
            g = guard
            if g is not None:
                # keep the DECSTBM judgment honest across resizes: a region
                # that was benign at the old height may cover the bar now
                g.rows = r
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
        if adopted:
            # The warm child was spawned one column narrow so this resize is
            # normally a real change. It is not guaranteed to be: the user can
            # resize the terminal during the warm window and land the child
            # exactly on target. setwinsize to the dims a child already has
            # delivers no SIGWINCH at all, so nudge it off target first — a
            # resize that signals nothing is a flip onto a blank screen.
            target = _child_dims(rows, cols, bar_on)
            try:
                try:
                    on_target = child.getwinsize() == target
                except Exception:
                    # The probe is an optimisation; the resize below is the
                    # handover itself. A probe that raises must not take the
                    # resize down with it, so read it as "not on target" and
                    # go straight to the real setwinsize.
                    on_target = False
                if on_target:
                    child.setwinsize(max(1, target[0] - 1), max(1, target[1] - 1))
                child.setwinsize(*target)
            except Exception:
                pass   # child died in the gap: the liveness loop ends the pump
        paint()
        if bar is not None:
            _report_bar(frame, True)   # drawn for real: bytes are on the terminal
        # seeded from the state just painted, so an already-armed frame does
        # not draw a redundant repaint on the first iteration
        last_armed = frame.armed() if bar is not None else False
        last_usage = (
            frame.usage() if bar is not None and frame.usage is not None else ""
        )
        last_limits = (
            frame.limits() if bar is not None and frame.limits is not None else None
        )
        last_mode = (
            frame.mode() if bar is not None and frame.mode is not None else None
        )
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
            if bar is not None and frame.usage is not None:
                usage_now = frame.usage()
                if usage_now != last_usage:
                    last_usage = usage_now
                    paint()
            if bar is not None and frame.limits is not None:
                # the poller replaces the whole dict, so identity is the
                # cheap change check and equality the correct one
                limits_now = frame.limits()
                if limits_now is not last_limits and limits_now != last_limits:
                    last_limits = limits_now
                    paint()
            if bar is not None and frame.mode is not None:
                # A tab change is invisible to the pump otherwise: entering or
                # leaving the mixed tab moves no bytes and resizes nothing.
                # snapshot() builds a fresh dict per call, so unlike `limits`
                # there is no identity shortcut — equality is the whole check.
                mode_now = frame.mode()
                if mode_now != last_mode:
                    last_mode = mode_now
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
