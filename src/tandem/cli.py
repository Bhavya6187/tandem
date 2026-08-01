"""tandem — run Claude Code and Codex CLI as one paired session."""

from __future__ import annotations

import json
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


# Set by the tandem prompt (shell.py) around each dispatched command so it
# acts on that shell's own session. Without it, a second `tandem` in the same
# directory becomes the cwd-MRU and silently steals `status`/`sync`/`run --on`
# typed in the first shell.
_SESSION_ID: str | None = None


def _resolve_session(store: StateStore) -> PairedSession | None:
    if _SESSION_ID is not None:
        return store.get_session(_SESSION_ID)
    return store.latest_session_for_cwd(_cwd())


def _require_session(store: StateStore) -> PairedSession:
    session = _resolve_session(store)
    if session is None:
        click.echo(
            "No tandem session for this directory. Run `tandem` to start one.",
            err=True,
        )
        sys.exit(1)
    store.touch_used(session.tandem_id)
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
@click.option(
    "--active",
    type=click.Choice(["claude", "codex"]),
    default="claude",
    show_default=True,
    help="Initially active harness for the fresh session.",
)
@click.pass_context
def main(ctx: click.Context, active: str) -> None:
    """Run Claude Code and Codex as one paired session.

    With no subcommand, pairs a fresh session and enters the active
    harness; `tandem resume` continues an earlier one.
    """
    if ctx.invoked_subcommand is None:
        _interactive(active)


def _pair_session(store: StateStore, cwd: str, active: str) -> PairedSession:
    """Create a fresh paired session: state row, seeded shadow transcript,
    write-ahead cursor, memory sync. Echoes what it did."""
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
        shadow_adapter.create_shadow_transcript(cwd, claude_sid, ctx, note)
        cursor_updates = {"claude_leaf_uuid": ctx.claude_leaf_uuid}
    else:
        shadow_adapter.create_shadow_transcript(cwd, codex_sid, ctx, note)
        cursor_updates = {}
    cursor = store.get_cursor(session.tandem_id, active)
    cursor.pending.update(cursor_updates)
    store.save_cursor(cursor)

    from .memory_sync import sync_memory_files

    mem = sync_memory_files(cwd)
    click.echo(f"paired {session.tandem_id} ({active} active, {shadow} shadow)")
    for a in mem.actions:
        click.echo(f"  memory: {a}")
    for w in mem.warnings:
        click.secho(f"  memory: {w}", fg="yellow", err=True)
    if active == "codex":
        click.echo("  note: codex session id will be captured on first run")
    return session


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
        from . import ops

        for source in ("claude", "codex"):
            cursor = store.get_cursor(session.tandem_id, source)
            behind = ops.unsynced_lines(session, store, source)
            if cursor.updated_at or cursor.failed_turns or behind:
                line = (
                    f"  sync from {source}: line {cursor.line_index}, "
                    f"turn {cursor.turn_index}, failed turns: {cursor.failed_turns}"
                )
                if behind and source == session.active:
                    line += f", {behind} lines awaiting translation"
                click.echo(line)
        qdir = paths.quarantine_dir(session.tandem_id)
        if qdir.is_dir() and any(qdir.iterdir()):
            click.echo(f"  quarantine: {qdir} (has entries)")


@main.command()
@click.argument("tandem_id", required=False)
def resume(tandem_id: str | None) -> None:
    """Resume a paired session (most recent for this directory by default).

    The id is printed when you leave a session, and shown by `tandem status`.
    """
    cwd = _cwd()
    _check_versions(warn_only=True)
    with StateStore() as store:
        if tandem_id is None:
            session = store.latest_session_for_cwd(cwd)
            if session is None:
                click.echo(
                    "No tandem session for this directory. Run `tandem` to start one.",
                    err=True,
                )
                sys.exit(1)
        else:
            session = store.get_session(tandem_id)
            if session is None:
                click.secho(f"error: no tandem session {tandem_id!r}.", fg="red", err=True)
                sys.exit(1)
            if session.cwd != cwd:
                click.secho(
                    f"error: session {tandem_id} belongs to {session.cwd}; "
                    "run `tandem resume` from there.",
                    fg="red",
                    err=True,
                )
                sys.exit(1)
        store.touch_used(session.tandem_id)
    sys.exit(_enter_session(session))


def _default_sink_factory(store, session, source):
    """Sync engine by default; TANDEM_LOG_EVENTS=1 switches to the debug
    event logger (no shadow writes)."""
    import os

    from .runner import EventLogger
    from .sync import SyncEngine

    if os.environ.get("TANDEM_LOG_EVENTS"):
        return EventLogger(session.tandem_id, source)
    return SyncEngine(store, session, source)


def _report_switch(old: str, new_active: str, problems, mem) -> None:
    """Report the outcome of a role flip. Shared by the one-shot `switch`
    command and the tandem prompt's `switch`, so neither path drops
    memory-sync actions or the may-not-resume advisory."""
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


@main.command()
def switch() -> None:
    """Make the shadow harness active (instant; no re-conversion)."""
    from . import ops

    with StateStore() as store:
        session = _require_session(store)
        old = session.active
        try:
            new_active, problems, mem = ops.switch_session(store, session)
        except Exception as exc:
            click.secho(f"switch failed: {exc}", fg="red", err=True)
            sys.exit(1)
        _report_switch(old, new_active, problems, mem)
        click.echo("Run `tandem resume` to continue in the new harness.")


@main.command(name="run")
@click.option(
    "--on",
    "target",
    type=click.Choice(["claude", "codex"]),
    required=True,
    help="Harness to route this one prompt to.",
)
@click.argument("prompt", nargs=-1, required=True)
def run_cmd(target: str, prompt: tuple[str, ...]) -> None:
    """Run one prompt on the other harness, then return control.

    The resulting turn lands in both session files with attribution."""
    from . import ops

    text = " ".join(prompt)
    with StateStore() as store:
        session = _require_session(store)
        if target == session.active:
            click.secho(
                f"note: {target} is already the active harness; running the "
                "turn there anyway.",
                fg="yellow",
                err=True,
            )
        code = ops.run_oneoff(store, session, target, text)
    sys.exit(code)


@main.command()
@click.option("-m", "--model", default=None,
              help="Codex model for this worker (config default otherwise).")
@click.option("--context", "context_mode",
              type=click.Choice(["task", "full"]), default=None,
              help="Worker context: cold task-only, or a full fork of the "
                   "paired session (config policy decides by default).")
@click.argument("task", required=False)
def sub(model: str | None, context_mode: str | None, task: str | None) -> None:
    """Run one delegated subagent task on codex (task argument or stdin).

    Used by the tandem plugin's codex-worker bridge; also works manually."""
    from . import ops
    from .config import load_subagents_config

    if task is None or task == "-":
        task = sys.stdin.read()
    task = task.strip()
    if not task:
        click.secho("error: empty task brief.", fg="red", err=True)
        sys.exit(1)
    cfg = load_subagents_config()
    with StateStore() as store:
        session = _require_session(store)
        code = ops.run_sub(
            store, session, task,
            model=model if model is not None else cfg.model,
            context=context_mode or ("full" if cfg.context == "full" else "task"),
            fanout_feature=cfg.fanout_feature,
            keep_forks=cfg.keep_forks,
        )
    sys.exit(code)


@main.command(name="hook-route")
def hook_route_cmd() -> None:
    """Claude Code PreToolUse hook: reroute subagent dispatches to codex.

    Reads hook JSON on stdin; prints a decision or nothing. This function
    ALWAYS exits 0 — exit 2 would block the dispatch, and any failure here
    must degrade to native behavior.

    The function body is not the whole story: click's usage-error path exits
    2 before this ever runs (version skew — plugin installed, an older
    tandem on PATH without this subcommand — or a stray argument). So the
    hook MUST be registered as `tandem hook-route || true`; that shell guard
    is what makes exit 2 unreachable in practice."""
    try:
        from .config import load_subagents_config
        from .hookroute import route

        payload = json.loads(sys.stdin.read() or "{}")
        cwd = payload.get("cwd") or _cwd()
        cfg = load_subagents_config()
        with StateStore() as store:
            session = store.latest_session_for_cwd(cwd)
        codex_ok = False
        if session is not None:
            adapter = get_adapter("codex")
            v = adapter.detect_version()
            codex_ok = v is not None and adapter.version_supported(v)
        decision = route(payload, cfg, cwd, paths.claude_home(),
                         has_session=session is not None, codex_ok=codex_ok)
        if decision is not None:
            click.echo(json.dumps(decision))
    except Exception:
        pass
    sys.exit(0)


@main.command()
@click.option(
    "--live",
    is_flag=True,
    help="Also perform a real resume on both sessions (costs one small model "
    "call per harness).",
)
def doctor(live: bool) -> None:
    """Validate that both session files are resumable; report drift."""
    from .doctor import run_doctor

    with StateStore() as store:
        session = _resolve_session(store)
        report = run_doctor(store, session, live=live)
    icons = {"ok": ("✓", "green"), "warn": ("!", "yellow"), "fail": ("✗", "red")}
    for check in report.checks:
        icon, color = icons[check.status]
        click.secho(f" {icon} {check.message}", fg=color if check.status != "ok" else None)
    if report.failed:
        sys.exit(1)
    click.echo("all checks passed" if not any(
        c.status == "warn" for c in report.checks
    ) else "passed with warnings")


@main.command(name="sync-mcp")
@click.confirmation_option(
    prompt="Copy MCP server definitions between ~/.claude.json and "
    "~/.codex/config.toml (additive, never overwrites existing entries)?"
)
def sync_mcp() -> None:
    """Copy MCP server configs between the two harnesses (opt-in)."""
    from .memory_sync import copy_mcp

    report = copy_mcp()
    for a in report.actions:
        click.echo(f"  {a}")
    for w in report.warnings:
        click.secho(f"  warning: {w}", fg="yellow", err=True)


@main.command()
def sync() -> None:
    """Catch up shadow translation manually (pure local file I/O)."""
    from . import ops

    with StateStore() as store:
        session = _require_session(store)
        n = ops.drain_source(store, session, session.active)
        click.echo(f"synced {n} new transcript lines from {session.active}.")


def _interactive(active: str) -> None:
    cwd = _cwd()
    _check_versions()  # hard: pairing needs both binaries on PATH
    with StateStore() as store:
        session = _pair_session(store, cwd, active)
    sys.exit(_enter_session(session))


def _enter_session(session: PairedSession) -> int:
    from .shell import run_shell

    return run_shell(session.tandem_id, _default_sink_factory)


if __name__ == "__main__":
    main()
