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

    # The resume hint is the only place the id is shown, so it prints from a
    # finally: no failure inside the loop may cost the user their session.
    code = 1
    try:
        code = _enter(tandem_id, run_harness, code)
        while True:
            with StateStore() as store:
                session = store.get_session(tandem_id)
            if session is None:
                click.secho(
                    f"session {tandem_id} is no longer in the state store.",
                    fg="red",
                    err=True,
                )
                break
            try:
                line = input_fn(f"tandem ({session.active})> ").strip()
            except EOFError:
                break
            except KeyboardInterrupt:
                click.echo()
                continue
            if line in ("exit", "quit"):
                break
            if line in ("", "resume"):
                code = _enter(tandem_id, run_harness, code)
                continue
            if line.split(maxsplit=1)[0] == "run":
                parsed = _split_run_line(line)
                if parsed is not None:
                    target, prompt = parsed
                    # `--` keeps a prompt that starts with `-` an argument
                    _dispatch(["run", "--on", target, "--", prompt], tandem_id)
                    continue
                # Malformed: fall through and let click report the usage error.
            try:
                argv = shlex.split(line)
            except ValueError as exc:  # e.g. an unbalanced quote
                click.secho(f"could not parse: {exc}", fg="yellow")
                continue
            if argv[0].startswith("-") and argv[0] not in ("--help", "--version"):
                # Options with no subcommand would hit the group's
                # invoke_without_command path: a fresh pairing plus a shell
                # nested inside this one. Launching is an OS-shell concern.
                click.secho(
                    f"{argv[0]} is a `tandem` launch option, not a prompt command"
                    " — `exit` first, then re-run tandem.",
                    fg="yellow",
                )
                click.echo(HELP)
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
            _dispatch(argv, tandem_id)
    finally:
        # Hint first: state bookkeeping must not be able to swallow it.
        click.echo(f"to continue this session: tandem resume {tandem_id}")
        with StateStore() as store:
            store.touch_used(tandem_id)
    return code


def _split_run_line(line: str) -> tuple[str, str] | None:
    """Peel `--on <harness>` off a `run` line typed at the prompt and return
    (harness, prompt). None means the line does not have that shape — dispatch
    it normally so click reports the usage error.

    The prompt is taken verbatim from the raw line instead of being
    shlex-split, so an apostrophe ("what's wrong") is literal text rather
    than an unbalanced quote. A prompt wrapped in matching quotes keeps its
    one-shot meaning: the outer quotes are stripped.
    """
    rest = line[len("run") :].strip()
    for prefix in ("--on=", "--on "):
        if rest.startswith(prefix):
            target, _, prompt = rest[len(prefix) :].strip().partition(" ")
            break
    else:
        return None
    target, prompt = target.strip(), prompt.strip()
    if not target or not prompt:
        return None
    if len(prompt) > 1 and prompt[0] == prompt[-1] and prompt[0] in "\"'":
        inner = prompt[1:-1]
        if prompt[0] not in inner:
            prompt = inner
    return target, prompt


def _switch(tandem_id: str, run_harness, code: int) -> int:
    """Flip roles and re-enter the newly active harness. Returns the exit
    code to carry forward (unchanged if the flip failed)."""
    from .cli import _report_switch

    with StateStore() as store:
        session = store.get_session(tandem_id)
        if session is None:
            click.secho(
                f"switch failed: session {tandem_id} is no longer in the"
                " state store.",
                fg="red",
                err=True,
            )
            return code
        old = session.active
        try:
            new_active, problems, mem = ops.switch_session(store, session)
        except Exception as exc:
            click.secho(
                f"switch failed: {type(exc).__name__}: {exc}", fg="red", err=True
            )
            return code
    _report_switch(old, new_active, problems, mem)
    return _enter(tandem_id, run_harness, code)


def _enter(tandem_id: str, run_harness, code: int) -> int:
    """Run the active harness and return its exit code. A failed launch (or a
    session row that vanished) is reported and `code` is carried forward, so
    the caller returns to the prompt instead of losing the session."""
    try:
        with StateStore() as store:
            session = store.get_session(tandem_id)
            if session is None:
                raise LookupError(f"session {tandem_id} is not in the state store")
            store.touch_used(tandem_id)
        return run_harness(session)
    except Exception as exc:
        click.secho(
            f"could not run the harness: {type(exc).__name__}: {exc}",
            fg="red",
            err=True,
        )
        return code


def _dispatch(argv: list[str], tandem_id: str) -> None:
    # Late import: cli imports shell (via _enter_session), so importing cli
    # at module level would be circular.
    from . import cli

    previous = cli._SESSION_ID
    # Commands typed here act on THIS session, not on whatever is the
    # most-recently-used session for the cwd.
    cli._SESSION_ID = tandem_id
    try:
        cli.main.main(args=argv, prog_name="tandem", standalone_mode=False)
    except click.exceptions.UsageError as exc:
        exc.show()
        click.echo(HELP)
    except click.ClickException as exc:
        exc.show()
    except click.exceptions.Abort:
        click.echo()
    except SystemExit:
        pass  # one-shot commands sys.exit(); the prompt continues
    except Exception as exc:
        # A failing command must never take the live session down with it.
        # The type name matters: str(KeyError()) and friends are empty.
        click.secho(f"command failed: {type(exc).__name__}: {exc}", fg="red", err=True)
    finally:
        cli._SESSION_ID = previous
