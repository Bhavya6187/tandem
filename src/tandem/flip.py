"""Run the active harness, re-entering on Ctrl-] flips until a plain exit.

Bare `tandem` / `tandem resume` enter here: run the active harness on its
PTY; a flip asked for from inside it (Ctrl-]) re-enters the other one with
no stop in between. When the harness exits without a flip pending, print
the resume hint and return to the OS shell.
"""

from __future__ import annotations

import sys
import threading

import click

from . import ops
from .harness import get_adapter
from .state import StateStore


def run_session(tandem_id: str, sink_factory, run_harness=None) -> int:
    """Run the active harness, flipping (Ctrl-]) until a session ends
    without requesting one. Returns the last harness exit code.
    `run_harness` is an injection point for tests (real: an
    InteractiveRunner)."""
    # Report lines the runner held back because the flip about to happen would
    # clear the screen out from under them; `_flip_loop` prints them onto the
    # fresh screen. Refilled by every harness run. Injected test runners never
    # touch it, which leaves it empty — harmless.
    reports: list[str] = []
    # The standby travels across flips here: popped for adoption by the
    # next run, refilled from the runner that just ended. Injected test
    # runners never touch it. `reapers` collects the threads tearing down
    # standbys the gate rejected — the flip does not wait for them, this
    # function's finally does.
    carry: dict = {"standby": None, "reapers": []}
    if run_harness is None:

        def run_harness(session):
            from .runner import InteractiveRunner, _stdin_tty

            adopt, carry["standby"] = carry["standby"], None
            if adopt is not None and not _stdin_tty():
                # No terminal to hand it to: the pty path spawns cold and
                # would leave the hidden harness with nothing to reap it.
                _kill(adopt)
                adopt = None
            r = InteractiveRunner(session, sink_factory=sink_factory,
                                  adopt_child=adopt)
            try:
                code = r.run()
            finally:
                # In the finally so a run that raises mid-flip still hands
                # its kept child back: the carry is the only reference left,
                # and this function's own finally is what reaps it.
                carry["standby"] = r.warm_child
            reports[:] = r.reports
            return code, r.flip_requested

    # The resume hint is the only place the id is shown, so it prints from a
    # finally: no failure inside the loop may cost the user their session.
    code = 1
    try:
        code = _flip_loop(
            tandem_id, run_harness, _enter(tandem_id, run_harness), reports,
            carry,
        )
    finally:
        # Hint first: state bookkeeping must not be able to swallow it.
        click.echo(f"to continue this session: tandem resume {tandem_id}")
        # Nested so the reap survives a raising store — a locked sqlite (a
        # concurrent `tandem sub` holds its own) must not be the reason a
        # hidden harness outlives the session with nothing left to reap it.
        try:
            with StateStore() as store:
                store.touch_used(tandem_id)
        finally:
            # Nothing will ever adopt it now — the session is over. Killed
            # inline: the user is on their way back to the OS shell, and a
            # hidden harness must be dead before tandem's own process is.
            leftover = carry.get("standby")
            if leftover is not None:
                carry["standby"] = None
                _kill(leftover)
            # Then the stale ones the gate handed to reaper threads mid-flip.
            # Bounded: a wedged ladder may cost the exit 10s per standby, but
            # never the session — and there is at most one per flip.
            for thread in carry.get("reapers", ()):
                thread.join(timeout=10)
    return code


def _kill(standby) -> None:
    """Reap a standby nobody will adopt. Never raises: a hung or already
    dead child must not cost the user their exit or their flip."""
    try:
        standby.kill()
    except Exception:
        pass


def _reap(standby, carry: dict) -> None:
    """Kill a standby on a background thread and record the thread so the
    session's exit can join it.

    The kill ladder is slow by construction — quit keystrokes, then the
    unconditional soft/term waits — so killing a stale standby inline would
    put seconds between the user's Ctrl-] and the next harness starting,
    on precisely the flips warmup exists to speed up. The child is
    already out of the carry, so nothing can adopt it while it dies."""
    thread = threading.Thread(
        target=_kill, args=(standby,), name="tandem-warm-reap", daemon=True
    )
    reapers = carry.setdefault("reapers", [])
    # Only the ones still running are worth carrying: a long session flips
    # many times, and a finished reaper has nothing left to join.
    reapers[:] = [t for t in reapers if t.is_alive()]
    reapers.append(thread)
    thread.start()


def _norm(res) -> tuple[int, bool]:
    """run_harness returns `(code, flip)`; a bare int (legacy callers and
    test seams) means "exited, no flip"."""
    return res if isinstance(res, tuple) else (res, False)


def _clear_screen() -> None:
    if sys.stdout.isatty():  # pragma: no cover - interactive only
        sys.stdout.write("\x1b[2J\x1b[H")
        sys.stdout.flush()


def _flip_loop(
    tandem_id: str, run_harness, first: tuple[int, bool], reports: list[str],
    carry: dict | None = None,
) -> int:
    """Keep flipping (Ctrl-]) until a session ends without requesting one.
    No stop between flips — this is the frame's tab feel. A failed flip
    reports itself and returns no-flip, which ends the loop and returns
    the user to the OS shell with the session intact.

    `reports` is the outgoing session's held-back report lines (sync errors,
    notes). They print right after the clear, never before it: the clear is
    what would otherwise erase them, and the whole point is that a flip must
    not cost the user a sync-failure warning.

    `carry` is the pre-warmed standby's ride from one run to the next; None
    for callers with no warmup (injected test runners)."""
    code, flip = first
    while flip:
        _clear_screen()
        if reports:
            for line in reports:
                click.echo(line)
            reports.clear()   # this session's news, reported once
        code, flip = _switch(tandem_id, run_harness, code, carry=carry)
    return code


def _report_switch(old: str, new_active: str, problems, mem) -> None:
    """Report the outcome of a role flip: memory-sync actions and the
    may-not-resume advisory."""
    click.echo(
        f"active harness: {get_adapter(old).display_name} -> "
        f"{get_adapter(new_active).display_name}"
    )
    for a in mem.actions:
        click.echo(f"  memory: {a}")
    for w in mem.warnings:
        click.secho(f"  memory: {w}", fg="yellow", err=True)
    for p in problems:
        click.secho(f"  warning: {p}", fg="yellow", err=True)
    if problems:
        click.secho(
            "  the newly active session may not resume cleanly; "
            "run `tandem doctor` for details.",
            fg="yellow",
            err=True,
        )


def _standby_fresh(standby, new_active: str, session, mem) -> bool:
    """Attach-time freshness: the standby is only usable if nothing
    material changed since it was spawned. Alive + right side + shadow
    byte size equals the spawn snapshot + memory sync did nothing.
    Validation problems deliberately do NOT gate: a live standby already
    resumed successfully, which is stronger evidence than the validator;
    they are still reported."""
    if standby is None or not standby.alive():
        return False
    if standby.recipe.side != new_active:
        return False
    if mem.actions:
        return False
    from . import warm

    return warm._shadow_size(session, new_active) == standby.shadow_size


def _switch(
    tandem_id: str, run_harness, code: int, fall_back: bool = True,
    carry: dict | None = None,
) -> tuple[int, bool]:
    """Flip roles and re-enter the newly active harness. Returns the exit
    code to carry forward (unchanged if the flip itself failed, 1 if the
    flip worked but the launch did not) and whether the re-entered harness
    asked for another flip.

    Two failures, two answers. The switch itself failing means roles never
    moved, so ending the session at the OS shell is the right landing. The
    switch succeeding and the *launch* failing is worse: the active harness
    cannot start (`codex` uninstalled, a bad `[codex] args`), which is
    precisely the dead end the spec's ladder exists to avoid. So the first
    rung is to flip straight back and re-enter the harness the user just
    left — never strand them.

    `fall_back=False` marks that flip-back attempt: one retry, no ping-pong
    between two harnesses that both refuse to launch. If it also fails, the
    loop ends with the error shown — and the session itself is never at
    risk, since `run_session`'s finally always prints the resume hint.

    `carry` holds the standby the outgoing run warmed, if any. This is where
    it is judged: the roles have just moved, so the side it was warmed for
    and the state it snapshotted are finally comparable against what the
    next run will actually launch. A stale one is killed here and nowhere
    else — the runner silently ignores an adoptee it cannot use, so a wrong
    side that survived this gate would leak — but killed on a reaper thread,
    since the ladder's own timeouts would otherwise be charged to the flip."""
    with StateStore() as store:
        session = store.get_session(tandem_id)
        if session is None:
            click.secho(
                f"switch failed: session {tandem_id} is no longer in the"
                " state store.",
                fg="red",
                err=True,
            )
            return code, False
        old = session.active
        try:
            new_active, problems, mem = ops.switch_session(store, session)
        except Exception as exc:
            click.secho(
                f"switch failed: {type(exc).__name__}: {exc}", fg="red", err=True
            )
            return code, False
    _report_switch(old, new_active, problems, mem)
    if carry is not None:
        standby = carry.get("standby")
        if standby is not None:
            from .runner import _flip_debug

            fresh = _standby_fresh(standby, new_active, session, mem)
            _flip_debug(f"standby-gate side={new_active} fresh={fresh}")
            if not fresh:
                carry["standby"] = None
                _reap(standby, carry)   # off-thread: the flip must not wait
    code, flip, launched = _try_enter(tandem_id, run_harness)
    if launched or not fall_back:
        return code, flip
    click.secho(
        f"{new_active} would not start — switching back to {old}.",
        fg="yellow",
        err=True,
    )
    return _switch(tandem_id, run_harness, code, fall_back=False, carry=carry)


def _try_enter(tandem_id: str, run_harness) -> tuple[int, bool, bool]:
    """Run the active harness; returns (exit code, flip requested, launched).
    `launched` is False when the harness never got off the ground (a missing
    binary, a vanished session row) as opposed to running and exiting — the
    distinction `_switch` needs to decide whether to flip back.

    A failed launch exits 1 wherever it happens: a launch that never ran is a
    failure whether it was the first one or the one after a flip, and carrying
    the previous harness's code forward would report the last thing that *did*
    run as this session's outcome. A later successful launch overwrites it with
    that harness's real exit code."""
    try:
        with StateStore() as store:
            session = store.get_session(tandem_id)
            if session is None:
                raise LookupError(f"session {tandem_id} is not in the state store")
            store.touch_used(tandem_id)
        return (*_norm(run_harness(session)), True)
    except Exception as exc:
        click.secho(
            f"could not run the harness: {type(exc).__name__}: {exc}",
            fg="red",
            err=True,
        )
        return 1, False, False


def _enter(tandem_id: str, run_harness) -> tuple[int, bool]:
    """Run the active harness; returns (exit code, flip requested). A failed
    launch (or a session row that vanished) is reported and exits 1 with no
    flip, so the caller ends the session instead of losing it. No flip-back
    ladder here: roles never moved, so the harness the user just failed to
    launch *is* the one they were in."""
    c, flip, _ = _try_enter(tandem_id, run_harness)
    return c, flip
