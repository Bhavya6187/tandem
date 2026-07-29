"""Persistent tandem prompt between harness sessions.

Bare `tandem` / `tandem resume` enter here: run the active harness on its
PTY; when the user exits it, offer a prompt instead of returning to the OS
shell. `switch` flips roles and re-enters immediately; other lines are
dispatched through the click group so one-shot and prompt behavior never
drift apart.
"""

from __future__ import annotations

import shlex

import click

from . import ops
from .state import StateStore

HELP = (
    "commands: switch | resume (or Enter) | status | sync | doctor |"
    " run --on <harness> <prompt> | sync-mcp | exit"
)


def run_shell(tandem_id: str, sink_factory, input_fn=None, run_harness=None) -> int:
    """Loop: harness session -> prompt -> harness session. Returns the last
    harness exit code. `input_fn`/`run_harness` are injection points for
    tests (real: `input` and an InteractiveRunner)."""
    if input_fn is None:  # pragma: no cover - interactive default
        input_fn = input
    if run_harness is None:  # pragma: no cover - interactive default

        def run_harness(session):
            from .runner import InteractiveRunner

            return InteractiveRunner(session, sink_factory=sink_factory).run()

    code = _enter(tandem_id, run_harness)
    while True:
        with StateStore() as store:
            active = store.get_session(tandem_id).active
        try:
            line = input_fn(f"tandem ({active})> ").strip()
        except EOFError:
            break
        except KeyboardInterrupt:
            click.echo()
            continue
        if line in ("exit", "quit"):
            break
        if line in ("", "resume"):
            code = _enter(tandem_id, run_harness)
            continue
        try:
            argv = shlex.split(line)
        except ValueError as exc:  # e.g. an unbalanced quote
            click.secho(f"could not parse: {exc}", fg="yellow")
            continue
        if argv[0] == "resume":
            click.secho(
                "resume takes no id at this prompt — `exit` first, then"
                " `tandem resume <id>`.",
                fg="yellow",
            )
            continue
        if argv == ["switch"]:
            code = _switch(tandem_id, run_harness, code)
            continue
        _dispatch(argv)

    with StateStore() as store:
        store.touch_used(tandem_id)
    click.echo(f"to continue this session: tandem resume {tandem_id}")
    return code


def _switch(tandem_id: str, run_harness, code: int) -> int:
    """Flip roles and re-enter the newly active harness. Returns the exit
    code to carry forward (unchanged if the flip failed)."""
    with StateStore() as store:
        session = store.get_session(tandem_id)
        old = session.active
        try:
            new_active, problems, mem = ops.switch_session(store, session)
        except Exception as exc:
            click.secho(f"switch failed: {exc}", fg="red", err=True)
            return code
    click.echo(f"active harness: {old} -> {new_active}")
    for w in list(mem.warnings) + list(problems):
        click.secho(f"  warning: {w}", fg="yellow", err=True)
    return _enter(tandem_id, run_harness)


def _enter(tandem_id: str, run_harness) -> int:
    with StateStore() as store:
        session = store.get_session(tandem_id)
        store.touch_used(tandem_id)
    return run_harness(session)


def _dispatch(argv: list[str]) -> None:
    # Late import: cli imports shell (via _enter_session), so importing cli
    # at module level would be circular.
    from .cli import main as cli_group

    try:
        cli_group.main(args=argv, prog_name="tandem", standalone_mode=False)
    except click.exceptions.UsageError as exc:
        exc.show()
        click.echo(HELP)
    except click.ClickException as exc:
        exc.show()
    except click.exceptions.Abort:
        click.echo()
    except SystemExit:
        pass  # one-shot commands sys.exit(); the prompt continues
