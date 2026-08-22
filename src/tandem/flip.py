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

from . import ops, plugin_setup, routefile
from .config import load_frame_config
from .harness import get_adapter
from .state import StateStore
from .tabs import TabState


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
    # The tab cycle, built once below and carried across every run in this
    # process. None is the pre-mixed frame; the closure reads it at call
    # time, so it is assigned inside the try (a store that will not open must
    # still reach the resume hint).
    tabs = None
    if run_harness is None:

        def run_harness(session):
            from .runner import InteractiveRunner, _stdin_tty

            adopt, carry["standby"] = carry["standby"], None
            if adopt is not None and not _stdin_tty():
                # No terminal to hand it to: the pty path spawns cold and
                # would leave the hidden harness with nothing to reap it.
                _kill(adopt)
                adopt = None
            if tabs is not None:
                # Every flip is a fresh process in the same terminal, so the
                # tab the user is in only survives through the store. Written
                # before the harness takes over the terminal: from here on
                # this process is not coming back until the run ends.
                with StateStore() as s:
                    _persist_tabs(s, session.tandem_id, tabs)
            r = InteractiveRunner(session, sink_factory=sink_factory,
                                  adopt_child=adopt, tabs=tabs,
                                  inject=carry.pop("route", None))
            try:
                code = r.run()
            finally:
                # In the finally so a run that raises mid-flip still hands
                # its kept child back: the carry is the only reference left,
                # and this function's own finally is what reaps it.
                carry["standby"] = r.warm_child
                # Where the run's mixer wants to go, if anywhere. Safe to
                # read here and only here: the runner joins the mixer thread
                # before returning, so nothing can still be writing it. No
                # coordinator means the run raised before it had a launch to
                # route for — and this is a finally, so an AttributeError
                # here would mask whatever actually went wrong.
                c = r.coordinator
                carry["route"] = c.route_request if c is not None else None
                if tabs is not None:
                    # Again, because the run may have moved the tab without
                    # ever flipping: entering the mixed tab from the harness
                    # that already holds its focus is a *bar* move, so
                    # `_switch` never runs and the pre-run write above is
                    # stale by exactly that move. Persisting here is what
                    # makes `tandem resume` land back in the tab the user
                    # was actually looking at. Best-effort on purpose: this
                    # sits in a finally, and a locked store raising here
                    # would turn a clean exit into a failed launch and send
                    # the ladder off to boot a harness nobody asked for. One
                    # lost bar move is the cheaper failure.
                    try:
                        with StateStore() as s:
                            _persist_tabs(s, session.tandem_id, tabs)
                    except Exception:
                        pass
            reports[:] = r.reports
            return code, r.flip_requested

    # The resume hint is the only place the id is shown, so it prints from a
    # finally: no failure inside the loop may cost the user their session.
    code = 1
    try:
        try:
            tabs = _tab_state(tandem_id)
        except Exception:
            # A session that cannot read its tab state degrades to the
            # pre-mixed frame rather than dying. This open is the only one
            # that happens before the loop, so an escape here would be a
            # traceback where every other store failure in this function is a
            # red one-liner — and a locked sqlite (a concurrent `tandem sub`
            # holds its own) must never be the reason a session is lost. The
            # loop's own store opens keep their existing error paths.
            tabs = None
            # Degraded or not, this process runs no mixer, and the hook reads
            # the frame file to decide whether to hold a prompt. A file left
            # saying `mixed` would have it stash a routed prompt with nothing
            # to pick it up: the block tells the user their turn is running
            # in another harness, and nothing ever runs it. The next mixed
            # start does surface it — the sweep quotes both slots — but that
            # is a note about a prompt that already went nowhere, one
            # session too late. Stamping the tab off stops the stash from
            # happening at all. The stamp is right whichever way the unreadable
            # config would have gone: mixed off is exactly what `_tab_state`
            # writes, and mixed on still has no mixer this run. Best-effort by
            # construction (`routefile._write_json` swallows its own errors),
            # so it cannot raise back into this guard.
            routefile.write_frame_state(
                tandem_id, {"tab": "harness", "focus": "",
                            "routing_ok": False})
        code = _flip_loop(
            tandem_id, run_harness, _enter(tandem_id, run_harness), reports,
            carry, tabs=tabs,
        )
    finally:
        # Hint first: state bookkeeping must not be able to swallow it.
        click.echo(f"to continue this session: tandem resume {tandem_id}")
        # The session is over, so no mixer owns that file any more — and a
        # file left saying `mixed` is a trap the user can walk into without
        # tandem at all. The hook's identity gate only asks that the prompt
        # came from the focus harness's native session id, which is exactly
        # what `claude -r <same id>` outside tandem hands it: it would read a
        # live-looking frame, block the prompt and stash it for a frame that
        # no longer exists. Stamping the tab off closes it. Same shape as the
        # two startup stamps (`_tab_state`'s mixed-off branch and the
        # degraded-read guard above), and best-effort by the same
        # construction — `routefile._write_json` swallows its own errors, so
        # this cannot raise into a finally that still has a standby to reap.
        # Accepted residual: a SIGKILL'd frame never runs this, so the trap
        # survives a hard kill. Nothing in-process can cover that; the next
        # `tandem` / `tandem resume` in the directory re-stamps the file.
        routefile.write_frame_state(
            tandem_id, {"tab": "harness", "focus": "", "routing_ok": False})
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


def _tab_state(tandem_id: str) -> TabState | None:
    """This session's tab cycle, or None when the mixed tab is off.

    Built once per process and restored from session meta, because a flip is
    a fresh run of this function in the same terminal: without the meta blob
    every flip would drop the user back into the harness tab.

    The `mixed = false` branch is not a plain "return None". The prompt hook
    reads the *frame file*, not the config, and a file left by an earlier run
    that had the tab on still says `tab: "mixed"` — so the hook would keep
    stashing prompts for a mixer that no longer runs. Stamping it back to the
    harness tab once at startup is what keeps that from swallowing turns.
    (With the tab on, the runner's mixer owns that file and this must not
    write it: two writers, one file.)"""
    with StateStore() as store:
        session = store.get_session(tandem_id)
        meta = store.get_meta(tandem_id) if session is not None else {}
    if session is None:
        return None
    if not load_frame_config().mixed:
        routefile.write_frame_state(
            tandem_id, {"tab": "harness", "focus": "", "routing_ok": False})
        return None
    # Which participants a prompt can be @-routed *from*: the CLI needs a
    # prompt hook and tandem's plugin has to be registered there. Read once
    # per process, exactly like the runner's own `routing_ok` (that one is
    # about the harness on screen; this one is what keeps the mixed tab from
    # adopting a focus no keystroke could ever move off).
    routable = {h for h in session.participants
                if get_adapter(h).prompt_hook_capable
                and plugin_setup.hook_available(h)}
    return TabState(session.participants, tab=meta.get("tab", "harness"),
                    focus=meta.get("mixed_focus", ""), routable=routable)


def _persist_tabs(store: StateStore, tandem_id: str, tabs: TabState) -> None:
    """The tab cycle's durable half. `set_meta` replaces the whole blob, so
    both keys always travel together — writing one alone would drop the
    other."""
    store.set_meta(tandem_id, {"tab": tabs.tab, "mixed_focus": tabs.focus})


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
    carry: dict | None = None, tabs: TabState | None = None,
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
    for callers with no warmup (injected test runners). `tabs` is the tab
    cycle, None when the mixed tab is off."""
    code, flip = first
    while flip:
        _clear_screen()
        if reports:
            for line in reports:
                click.echo(line)
            reports.clear()   # this session's news, reported once
        route = carry.pop("route", None) if carry else None
        # The pending slot is the user's to own — `TabState.routed` is a
        # claim, not an assignment — so it outranks the route request. In the
        # normal routed flow the two say the same thing: the mixer only built
        # a request after claiming the slot for it. They differ only when a
        # stale or stranded request meets a fresh press, and then the press
        # wins: the route still rides along, and the injector's target check
        # turns it into a kept-not-delivered note instead of a prompt typed
        # into a harness the user chose for something else.
        to = tabs.pending_target() if tabs is not None else ""
        to = to or (route.target if route is not None else "")
        code, flip = _switch(tandem_id, run_harness, code, carry=carry,
                             to=to, route=route, tabs=tabs)
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


def _standby_fresh(standby, new_active: str, session, mem,
                   route_model: str = "") -> bool:
    """Attach-time freshness: the standby is only usable if nothing
    material changed since it was spawned. Alive + right side + the model
    the incoming turn asked for + shadow byte size equals the spawn
    snapshot + memory sync did nothing.
    Validation problems deliberately do NOT gate: a live standby already
    resumed successfully, which is stronger evidence than the validator;
    they are still reported.

    `route_model` is the routed turn's pin, "" for an ordinary flip. It is
    compared against what the standby was actually *launched* with
    (`recipe.model` records the launch, never the intent), so a child warmed
    for the same side but the wrong model is stale: it is already running,
    and no pin can be applied after the fact."""
    if standby is None or not standby.alive():
        return False
    if standby.recipe.side != new_active:
        return False
    if route_model and standby.recipe.model != route_model:
        return False   # warmed without the pinned model: wrong launch
    if mem.actions:
        return False
    from . import warm

    return warm._shadow_size(session, new_active) == standby.shadow_size


def _switch(
    tandem_id: str, run_harness, code: int, visited: set[str] | None = None,
    carry: dict | None = None, to: str = "",
    route: routefile.RouteRequest | None = None,
    tabs: TabState | None = None,
) -> tuple[int, bool]:
    """Flip to the next harness in cycle order and re-enter it. Returns the
    exit code to carry forward (unchanged if the flip itself failed, 1 if
    the flip worked but the launch did not) and whether the re-entered
    harness asked for another flip.

    Two failures, two answers. The switch itself failing means roles never
    moved, so ending the session at the OS shell is the right landing. The
    switch succeeding and the *launch* failing is worse: the active harness
    cannot start (`codex` uninstalled, a bad `[codex] args`), which is
    precisely the dead end the spec's ladder exists to avoid. So on a launch
    failure the ladder tries the next unvisited harness in the cycle; when
    every other participant has refused, it falls back to the harness the
    user just left — never strand them.

    `visited` carries the refusals across recursive attempts — the N-ary
    generalization of the old no-ping-pong rule. The pre-ladder active is
    never added until it is attempted as the last resort, so at N=2 the
    sequence is exactly today's: try other → fail → back to old → stop.

    `carry` holds the standby the outgoing run warmed, if any. This is where
    it is judged: the roles have just moved, so the side it was warmed for
    and the state it snapshotted are finally comparable against what the
    next run will actually launch. A stale one is killed here and nowhere
    else — the runner silently ignores an adoptee it cannot use, so a wrong
    side that survived this gate would leak — but killed on a reaper thread,
    since the ladder's own timeouts would otherwise be charged to the flip.

    `to` names the harness to land on instead of the next one in cycle order
    — a tab press that skips ahead, or a routed turn's target. `route` is
    that turn's request: it decides the model the standby gate demands, and
    rides the carry into the next run as its inject. `tabs` is the tab cycle,
    settled here (and persisted) once the flip has actually landed.

    The ladder is deliberately blind to all three. A target that refuses to
    launch falls back through the cycle exactly as a Ctrl-] target does, and
    the recursion carries neither `to` (never re-target a refusal) nor
    `route` (never type a prompt into whoever answers instead) — the route
    survives on disk and the next frame start surfaces it."""
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
        visited = visited or set()
        target = to or session.next_active(old)
        while target in visited and target != old:
            target = session.next_active(target)
        if target in visited:
            click.secho("no harness would start — staying where we were.",
                        fg="red", err=True)
            return code, False
        try:
            new_active, problems, mem = ops.switch_session(store, session, to=target)
        except Exception as exc:
            click.secho(
                f"switch failed: {type(exc).__name__}: {exc}", fg="red", err=True
            )
            return code, False
        if tabs is not None:
            # The flip landed, so the pending move is spent: the tab and
            # focus it settles into are what the next run must start in, and
            # the store is the only thing that carries them there.
            tabs.settle(new_active)
            _persist_tabs(store, tandem_id, tabs)
    _report_switch(old, new_active, problems, mem)
    if carry is not None:
        standby = carry.get("standby")
        if standby is not None:
            from .runner import _flip_debug

            fresh = _standby_fresh(
                standby, new_active, session, mem,
                route_model=route.model if route is not None else "")
            _flip_debug(f"standby-gate side={new_active} fresh={fresh}")
            if not fresh:
                carry["standby"] = None
                _reap(standby, carry)   # off-thread: the flip must not wait
        # The next run's inject, popped by the default `run_harness`. Written
        # even when None: a ladder rung that inherited a leftover would type
        # the prompt into a harness the route never named.
        carry["route"] = route
    code, flip, launched = _try_enter(tandem_id, run_harness)
    if launched:
        return code, flip
    visited.add(new_active)
    remaining = [h for h in session.participants
                 if h not in visited and h != old]
    if remaining:
        click.secho(
            f"{new_active} would not start — trying {remaining[0]}.",
            fg="yellow", err=True,
        )
    elif old not in visited:
        click.secho(
            f"{new_active} would not start — switching back to {old}.",
            fg="yellow", err=True,
        )
    else:
        click.secho("no harness would start — staying where we were.",
                    fg="red", err=True)
        return code, False
    return _switch(tandem_id, run_harness, code, visited=visited, carry=carry,
                   tabs=tabs)


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
