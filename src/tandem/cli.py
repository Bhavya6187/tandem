"""tandem — run Claude Code and Codex CLI as one paired session."""

from __future__ import annotations

import sys
from pathlib import Path

import click

from . import compat, paths
from .constants import SEED_NOTE
from .events import SessionContext
from .harness import get_adapter, other
from .state import PairedSession, StateStore


def _cwd() -> str:
    return str(Path.cwd())


def _require_session(store: StateStore) -> PairedSession:
    session = store.session_for_cwd(_cwd())
    if session is None:
        click.echo("No tandem session for this directory. Run `tandem start` first.", err=True)
        sys.exit(1)
    return session


def _check_versions(warn_only: bool = False) -> dict[str, str | None]:
    versions: dict[str, str | None] = {}
    for hid in ("claude", "codex"):
        adapter = get_adapter(hid)
        v = adapter.detect_version()
        versions[hid] = v
        if v is None:
            msg = f"{adapter.display_name} ({adapter.binary}) not found on PATH."
            if warn_only:
                click.secho(f"warning: {msg}", fg="yellow", err=True)
            else:
                click.secho(f"error: {msg}", fg="red", err=True)
                sys.exit(1)
        elif not adapter.version_supported(v):
            tested = compat.COMPAT[hid].tested
            click.secho(
                f"warning: {adapter.display_name} version {v!r} is outside the "
                f"range tandem was built against (tested: {tested}). "
                f"Run `tandem doctor` before trusting sync.",
                fg="yellow",
                err=True,
            )
    return versions


@click.group(invoke_without_command=True)
@click.version_option(package_name="tandem")
@click.pass_context
def main(ctx: click.Context) -> None:
    """Work in Claude Code or Codex and switch at any time.

    Run with no arguments to enter the active harness interactively.
    """
    if ctx.invoked_subcommand is None:
        _interactive()


@main.command()
@click.option(
    "--active",
    type=click.Choice(["claude", "codex"]),
    default=None,
    help="Initially active harness (prompted if omitted).",
)
def start(active: str | None) -> None:
    """Initialize a paired session for the current directory."""
    cwd = _cwd()
    _check_versions()
    with StateStore() as store:
        existing = store.session_for_cwd(cwd)
        if existing is not None:
            click.echo(
                f"A tandem session already exists here ({existing.tandem_id}, "
                f"active: {existing.active})."
            )
            if not click.confirm("Archive it and start a new one?", default=False):
                sys.exit(1)
            store.archive_session(existing.tandem_id)

        if active is None:
            active = click.prompt(
                "Initially active harness",
                type=click.Choice(["claude", "codex"]),
                default="claude",
            )
        shadow = other(active)

        claude_sid = get_adapter("claude").mint_session_id()
        codex_sid = None if active == "codex" else get_adapter("codex").mint_session_id()

        session = store.create_session(cwd, active, claude_sid, codex_sid)

        ctx = SessionContext(
            tandem_id=session.tandem_id,
            cwd=cwd,
            direction="claude->codex" if active == "claude" else "codex->claude",
            claude_session_id=claude_sid,
            codex_session_id=codex_sid,
        )
        note = SEED_NOTE.format(
            tandem_id=session.tandem_id,
            other=get_adapter(active).display_name,
        )
        # The shadow transcript is created now so it is resume-ready from the
        # first turn. The active side's file is created by the harness itself
        # at first launch (claude is pinned via --session-id; codex mints its
        # own id which tandem captures on first run).
        shadow_adapter = get_adapter(shadow)
        if shadow == "claude":
            shadow_path = shadow_adapter.create_shadow_transcript(cwd, claude_sid, ctx, note)
            cursor_updates = {"claude_leaf_uuid": ctx.claude_leaf_uuid}
        else:
            shadow_path = shadow_adapter.create_shadow_transcript(cwd, codex_sid, ctx, note)
            cursor_updates = {}
        cursor = store.get_cursor(session.tandem_id, active)
        cursor.pending.update(cursor_updates)
        store.save_cursor(cursor)

        click.echo(f"Paired session {session.tandem_id} created in {cwd}")
        click.echo(f"  active:  {get_adapter(active).display_name}")
        click.echo(f"  shadow:  {shadow_adapter.display_name} -> {shadow_path}")
        if active == "codex":
            click.echo(
                "  note: codex session id will be captured on first `tandem` run"
            )
        click.echo("Run `tandem` to begin.")


@main.command()
def status() -> None:
    """Show the paired session for this directory."""
    with StateStore() as store:
        session = _require_session(store)
        versions = _check_versions(warn_only=True)
        click.echo(f"tandem session {session.tandem_id}  ({session.cwd})")
        click.echo(f"  created:   {session.created_at}")
        click.echo(f"  last sync: {session.last_sync_at or 'never'}")
        for hid in ("claude", "codex"):
            adapter = get_adapter(hid)
            sid = getattr(session, f"{hid}_session_id")
            role = "ACTIVE" if session.active == hid else "shadow"
            path = adapter.transcript_path(session.cwd, sid) if sid else None
            click.echo(f"  {adapter.display_name:<12} {role}")
            click.echo(f"    version: {versions.get(hid) or 'not installed'}")
            click.echo(f"    session: {sid or '(pending first run)'}")
            click.echo(f"    file:    {path or '(not created yet)'}")
        for source in ("claude", "codex"):
            cursor = store.get_cursor(session.tandem_id, source)
            if cursor.updated_at or cursor.failed_turns:
                click.echo(
                    f"  sync from {source}: line {cursor.line_index}, "
                    f"turn {cursor.turn_index}, failed turns: {cursor.failed_turns} "
                    f"(updated {cursor.updated_at})"
                )
        qdir = paths.quarantine_dir(session.tandem_id)
        if qdir.is_dir() and any(qdir.iterdir()):
            click.echo(f"  quarantine: {qdir} (has entries)")


def _interactive() -> None:
    from .runner import EventLogger, InteractiveRunner

    with StateStore() as store:
        session = _require_session(store)
    _check_versions(warn_only=True)
    runner = InteractiveRunner(
        session,
        sink_factory=lambda source: EventLogger(session.tandem_id, source),
    )
    sys.exit(runner.run())


if __name__ == "__main__":
    main()
