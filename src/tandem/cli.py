"""tandem — run Claude Code and Codex CLI as one paired session."""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import click

from . import __version__, compat, paths
from .constants import ATTRIBUTION, SEED_NOTE, SESSION_ENV
from .events import SessionContext, UserMessage
from .harness import get_adapter
from .state import PairedSession, StateStore, SyncCursor


def _cwd() -> str:
    return str(Path.cwd())


def _current_session(store: StateStore, cwd: str) -> PairedSession | None:
    """The session a command acts on. Inside a chat window's harness the
    window names it (SESSION_ENV): a directory holds many chat sessions, and
    the newest is not necessarily the one asking. Anywhere else — and for an
    id that no longer exists — it is the directory's most recent."""
    named = os.environ.get(SESSION_ENV)
    session = store.get_session(named) if named else None
    return session or store.latest_session_for_cwd(cwd)


def _require_session(store: StateStore) -> PairedSession:
    session = _current_session(store, _cwd())
    if session is None:
        click.echo(
            "No tandem session for this directory. Run `tandem` to start one.",
            err=True,
        )
        sys.exit(1)
    store.touch_used(session.tandem_id)
    return _narrow_participants(store, session)


def _resolve_participants(warn_only: bool = False) -> tuple[list[str], dict[str, str | None]]:
    """participants = configured ∩ installed-and-version-supported-and-ready.

    Not-installed is a normal state: silent skip, zero further probes.
    Installed but unusable (version below the compat floor, runtime not
    ready) warns and skips — fail closed. Above-ceiling versions warn but
    stay usable (drift, not proven breakage). Fewer than two usable is an
    error naming what's missing (warn-only mode reports instead, for
    status/resume)."""
    from .config import load_harnesses
    from .harness import ADAPTERS

    versions: dict[str, str | None] = {}
    usable: list[str] = []
    for hid in load_harnesses():
        if hid not in ADAPTERS:
            versions[hid] = None
            continue   # named in config but no adapter in this build (PR 1
                       # ships no opencode adapter): same silence as
                       # not-installed
        adapter = get_adapter(hid)
        v = adapter.detect_version()
        versions[hid] = v
        if v is None:
            continue                      # silent: the invariant
        if not adapter.version_supported(v):
            tested = compat.COMPAT[hid].tested
            parsed = compat.parse_version(v)
            if parsed is not None and parsed < compat.COMPAT[hid].min_version:
                # Below the floor the session format predates what tandem was
                # built on and genuinely cannot work: fail closed. Above the
                # ceiling is unproven drift — warn and proceed (a hard drop
                # would brick tandem on every new release until a compat
                # bump), which is also the pre-N-harness behavior.
                click.secho(
                    f"warning: {adapter.display_name} version {v!r} is below "
                    f"the oldest supported version (tested: {tested}) — "
                    f"excluded from this session.",
                    fg="yellow", err=True,
                )
                continue
            click.secho(
                f"warning: {adapter.display_name} version {v!r} is outside the "
                f"range tandem was built against (tested: {tested}). "
                f"Run `tandem doctor` before trusting sync.",
                fg="yellow", err=True,
            )
        ok, reason = adapter.runtime_ready()
        if not ok:
            click.secho(
                f"warning: {adapter.display_name} installed but unusable "
                f"({reason}) — excluded from this session.",
                fg="yellow", err=True,
            )
            continue
        usable.append(hid)
    if len(usable) < 2:
        missing = [h for h in load_harnesses() if h not in usable]
        msg = (f"tandem needs at least two usable harnesses; usable: "
               f"{usable or 'none'}, unavailable: {missing}.")
        if warn_only:
            click.secho(f"warning: {msg}", fg="yellow", err=True)
        else:
            click.secho(f"error: {msg}", fg="red", err=True)
            # Absent CLIs get an install line; installed-but-unusable ones
            # already explained themselves in the warnings above.
            for hid in missing:
                if hid in ADAPTERS and versions.get(hid) is None:
                    adapter = get_adapter(hid)
                    click.echo(
                        f"  install {adapter.display_name}: "
                        f"{adapter.install_hint}", err=True)
            sys.exit(1)
    return usable, versions


def _narrow_participants(store: StateStore, session: PairedSession) -> PairedSession:
    """Resume rule (spec: Participants/Resume): members gone missing are
    dropped from the session for good; narrowed list persisted; active moves
    to the first survivor if it was dropped; <2 survivors is fatal. No
    dynamic rejoin."""
    usable, _ = _resolve_participants(warn_only=True)
    survivors = [h for h in session.participants if h in usable]
    if survivors == session.participants:
        return session
    if len(survivors) < 2:
        click.secho(
            f"error: session {session.tandem_id} needs two usable harnesses; "
            f"surviving: {survivors or 'none'}.", fg="red", err=True)
        sys.exit(1)
    dropped = [h for h in session.participants if h not in survivors]
    click.secho(f"note: dropped {', '.join(dropped)} from this session "
                f"(not usable here); it will not rejoin.", fg="yellow", err=True)
    store.set_participants(session.tandem_id, survivors)
    if session.active not in survivors:
        store.set_active(session.tandem_id, survivors[0])
    return store.get_session(session.tandem_id)


_HARNESS_CHOICE = click.Choice(["claude", "codex", "opencode"])
_ON_HELP = "Harness for the first prompt [default: first usable, or last-used on resume]."
_NEW_HELP = "Pair a fresh session (the default)."
_CONTINUE_HELP = "Continue the most recently used session across all directories."
_skip_permissions_option = click.option(
    "--skip-permissions/--no-skip-permissions", "skip_permissions", default=None,
    help="Run claude and codex without their permission prompts for this "
         "launch [default: the skip_permissions config key].")


def _apply_skip_permissions(value: bool | None) -> None:
    """One launch's `--[no-]skip-permissions`. A subcommand's callback runs
    after the group's, so the nearer spelling wins, as it does for `--on`."""
    if value is not None:
        from .config import set_skip_permissions

        set_skip_permissions(value)


@click.group(invoke_without_command=True)
@click.version_option(version=__version__, prog_name="tandem")
@click.option("--on", "harness", type=_HARNESS_CHOICE, default=None, help=_ON_HELP)
@click.option("--new", "fresh", is_flag=True, help=_NEW_HELP)
@click.option("--continue", "-c", "continue_last", is_flag=True, help=_CONTINUE_HELP)
@click.option("--active", type=_HARNESS_CHOICE, default=None, hidden=True)
@_skip_permissions_option
@click.pass_context
def main(ctx: click.Context, harness: str | None, fresh: bool,
         continue_last: bool, active: str | None,
         skip_permissions: bool | None) -> None:
    """Run Claude Code and Codex as one paired session.

    With no subcommand, starts a fresh chat. Use `tandem resume [ID]` to
    reopen a chat, or --continue for the latest one across all directories.
    `tandem native` pairs a fresh session inside the CLIs' own TUIs and
    `tandem native resume [ID]` re-enters one.
    """
    if active is not None:
        # pre-chat-default spelling of `tandem native --active X`
        raise click.UsageError(
            f'--active moved: use "tandem native --active {active}" '
            f'(or "tandem --on {active}" for the chat window).')
    if ctx.invoked_subcommand == "resume" and (fresh or continue_last):
        raise click.UsageError("resume, --new and --continue cannot be combined.")
    if ctx.invoked_subcommand not in (None, "resume") and (
            harness is not None or fresh or continue_last):
        raise click.UsageError(
            "--on, --new and --continue only apply to chat sessions; "
            "use bare tandem or tandem resume.")
    if skip_permissions is not None and ctx.invoked_subcommand not in (None, "resume", "native"):
        # one-off relays, subagent dispatch and doctor probes never bypass:
        # taking the flag there would promise something it does not do
        raise click.UsageError(
            "--skip-permissions only applies to the sessions tandem opens: "
            "tandem, tandem resume, tandem native and tandem native resume.")
    _apply_skip_permissions(skip_permissions)
    if ctx.invoked_subcommand is None:
        _chat(harness, fresh, continue_last=continue_last)


def _pair_session(store: StateStore, cwd: str, active: str,
                  participants: list[str], *, seed: bool = True) -> PairedSession:
    """Create a fresh N-way session: state row, seeded shadow transcripts,
    memory sync. Echoes what it did.

    `seed=False` leaves the shadow transcripts to the caller (`_seed_shadows`,
    which the chat window runs on its first turn): they are the part of a
    pairing that lands in the harnesses' own session lists, and a window
    opened and closed must not leave one there per launch."""
    native: dict[str, str | None] = {}
    for hid in participants:
        if hid == "codex" and hid == active:
            native[hid] = None   # codex mints its own id on first run
        else:
            native[hid] = get_adapter(hid).mint_session_id()
    session = store.create_session(cwd, active, participants, native)
    if seed:
        _seed_shadows(store, session)

    from .memory_sync import sync_memory_files

    mem = sync_memory_files(cwd)
    shadows = [h for h in participants if h != active]
    click.echo(f"paired {session.tandem_id} ({active} active, "
               f"{', '.join(shadows)} shadow)")
    for a in mem.actions:
        click.echo(f"  memory: {a}")
    for w in mem.warnings:
        click.secho(f"  memory: {w}", fg="yellow", err=True)
    if "codex" in participants and native.get("codex") is None:
        click.echo("  note: codex session id will be captured on first run")
    return session


def _seed_shadows(store: StateStore, session: PairedSession) -> None:
    """The shadow transcripts of a fresh pairing, from the session as it was
    paired. Silent — the chat window runs it mid-turn, under its own screen —
    and a participant whose file is already there is skipped, so a run that
    failed halfway can be run again."""
    cwd, active = session.cwd, session.active
    participants = session.participants
    native = {hid: session.native_id(hid) for hid in participants}
    note = SEED_NOTE.format(
        tandem_id=session.tandem_id,
        other=get_adapter(active).display_name,
    )
    # Shadow transcripts are created now so they are resume-ready from the
    # first turn. The active side's file is created by the harness itself
    # at first launch (claude is pinned via --session-id; codex mints its
    # own id which tandem captures on first run) — except opencode, whose
    # `opencode -s <id>` requires the session to already exist, so an
    # opencode participant is pre-created whether or not it is active.
    for hid in participants:
        needs_create = (hid != active) or (hid == "opencode")
        if not needs_create or native[hid] is None:
            continue
        existing = get_adapter(hid).transcript_path(cwd, native[hid])
        if existing is not None and existing.exists():
            continue
        if hid == active:
            # opencode active: its session must exist before `opencode -s`
            direction = f"{session.next_active(active)}->{hid}"
        else:
            direction = f"{active}->{hid}"
        ctx = SessionContext(
            tandem_id=session.tandem_id, cwd=cwd, direction=direction,
            source_session_id=native.get(direction.split("->")[0]),
            target_session_id=native[hid],
        )
        get_adapter(hid).create_shadow_transcript(cwd, native[hid], ctx, note)
        cursor = store.get_cursor(session.tandem_id, active,
                                  hid if hid != active else session.next_active(active))
        cursor.pending["harness_state"] = ctx.harness_state
        store.save_cursor(cursor)


@main.command()
def status() -> None:
    """Show the paired session for this directory."""
    from . import modelcat

    with StateStore() as store:
        session = _require_session(store)
        versions = _resolve_participants(warn_only=True)[1]
        click.echo(f"tandem session {session.tandem_id}  ({session.cwd})")
        click.echo(f"  created:   {session.created_at}")
        click.echo(f"  last sync: {session.last_sync_at or 'never'}")
        for hid in session.participants:
            adapter = get_adapter(hid)
            sid = session.native_id(hid)
            role = "ACTIVE" if session.active == hid else "shadow"
            path = adapter.transcript_path(session.cwd, sid) if sid else None
            click.echo(f"  {adapter.display_name:<12} {role}")
            click.echo(f"    version: {versions.get(hid) or 'not installed'}")
            click.echo(f"    session: {sid or '(pending first run)'}")
            click.echo(f"    file:    {path or '(not created yet)'}")
        from . import ops

        for source in session.participants:
            # any direction shows the same source-side lag unless one target
            # crashed mid-append — good enough for status
            target = session.targets_for(source)[0]
            cursor = store.get_cursor(session.tandem_id, source, target)
            behind = ops.unsynced_lines(session, store, source, target)
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
        sub_root = paths.tandem_home() / "subagents" / session.tandem_id
        run_dir = sub_root / "running"
        if run_dir.is_dir():
            for m in sorted(run_dir.glob("*.json")):
                try:
                    d = json.loads(m.read_text())
                except (OSError, ValueError):
                    continue
                if not isinstance(d, dict):
                    continue  # non-object marker: skip it, never traceback
                # One wording owns "nobody picked a model" — the same phrase
                # the sub trailer and announcement use, so status and a
                # relayed reply describe the same worker the same way.
                click.echo(
                    f"  subagent running: {modelcat.model_label(d.get('model') or '')} "
                    f"({d.get('context')}) {d.get('task_preview', '')}"
                )
        kept = sorted(sub_root.glob("rollout-*.jsonl")) if sub_root.is_dir() else []
        if kept:
            click.echo(f"  retained forks: {len(kept)} under {sub_root}")


@click.command(name="resume")
@click.argument("tandem_id", required=False)
@_skip_permissions_option
def native_resume(tandem_id: str | None, skip_permissions: bool | None) -> None:
    """Resume a paired session (most recent for this directory by default).

    The id is printed when you leave a session, and shown by `tandem status`
    and `tandem sessions`.
    """
    _apply_skip_permissions(skip_permissions)
    cwd = _cwd()
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
                    "run `tandem native resume` from there.",
                    fg="red",
                    err=True,
                )
                sys.exit(1)
        store.touch_used(session.tandem_id)
        session = _narrow_participants(store, session)
    sys.exit(_enter_session(session))


def _ago(stamp: str | None, now: datetime | None = None) -> str:
    """Coarse relative age of an ISO timestamp: `just now`, `Nm ago`,
    `Nh ago`, `Nd ago`. Unparseable or missing input renders as `?` —
    a listing must never traceback on one bad row."""
    if not stamp:
        return "?"
    try:
        then = datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return "?"
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    secs = max(0, int((now - then).total_seconds()))
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


def _short_dir(cwd: str) -> str:
    """`~`-collapse the home prefix; tag directories that no longer exist."""
    path = Path(cwd)
    home = Path.home()
    try:
        shown = "~/" + str(path.relative_to(home)) if path != home else "~"
    except ValueError:
        shown = cwd
    if not path.is_dir():
        shown += " (missing)"
    return shown


_TAGS = tuple(t for t in ATTRIBUTION.values() if t and t != ATTRIBUTION["tandem"])
_NO_TITLE = "(no turns yet)"


def _iter_entries(adapter, session: PairedSession, harness: str, path: Path):
    """Raw transcript entries, lazily. A JSONL file streams line by line so a
    consumer that stops early never reads the multi-megabyte tail; anything
    else (opencode's DB) goes through the adapter's own reader."""
    if path.suffix == ".jsonl":
        with open(path, encoding="utf-8", errors="replace") as f:
            for text in f:
                try:
                    yield json.loads(text)
                except ValueError:
                    continue
        return
    cursor = SyncCursor(tandem_id=session.tandem_id, source=harness, target="__title__")
    for line in adapter.make_source_reader(session, cursor, path).poll():
        if line.raw is not None:
            yield line.raw


def _first_prompt(session: PairedSession) -> str | None:
    """The first thing a human typed into this session, or None if nobody
    has yet. The active harness's transcript is asked first: a turn that
    was mirrored into a shadow carries a `[via …]` tag, which is stripped
    when it is all there is. Tandem's own seed and close notes are not
    prompts. A harness whose transcript is missing (a zero-turn shadow)
    defers to the next participant."""
    order = [session.active] + [h for h in session.participants if h != session.active]
    for harness in order:
        sid = session.native_id(harness)
        if not sid:
            continue
        adapter = get_adapter(harness)
        path = adapter.transcript_path(session.cwd, sid)
        if path is None:
            continue
        # parse_entry only reads the context; the direction is a formality
        # (it must name two different adapters), so any other one will do
        other = next((h for h in session.participants if h != harness),
                     next(h for h in ("claude", "codex") if h != harness))
        ctx = SessionContext(tandem_id=session.tandem_id, cwd=session.cwd,
                             direction=f"{harness}->{other}")
        for raw in _iter_entries(adapter, session, harness, path):
            for ev in adapter.parse_entry(raw, ctx):
                if not isinstance(ev, UserMessage):
                    continue
                text = ev.text.strip()
                if text.startswith(ATTRIBUTION["tandem"]):
                    continue
                for tag in _TAGS:
                    if text.startswith(tag):
                        text = text[len(tag):].strip()
                        break
                if text:
                    return text
    return None


def _title_width(fixed: int) -> int:
    """Whatever the terminal has left after `fixed` columns of table and
    room for a directory, clamped so a title is never useless or endless."""
    cols = shutil.get_terminal_size((100, 24)).columns
    return max(16, min(60, cols - fixed - 2 - 16))


def _title_cell(session: PairedSession, width: int) -> str:
    """One padded cell: the first line of the first prompt that says
    anything (a pasted report often opens with a dashed rule), whitespace
    collapsed, cut with an ellipsis to `width`. A listing must never
    traceback on one unreadable transcript, so any failure is a `?` (same
    contract as `_ago`)."""
    try:
        text = _first_prompt(session)
    except Exception:
        return "?".ljust(width)
    if text is None:
        return _NO_TITLE.ljust(width)
    lines = text.splitlines()
    line = next((ln for ln in lines if any(c.isalnum() for c in ln)),
                next((ln for ln in lines if ln.strip()), ""))
    line = " ".join(line.split())
    if len(line) > width:
        line = line[:width - 1] + "…"
    return line.ljust(width)


@main.command()
@click.option("-n", "--limit", default=10, show_default=True, type=click.IntRange(min=1),
              help="How many sessions to show.")
def sessions(limit: int) -> None:
    """List your most recent paired sessions, newest first.

    Sessions in the current directory are marked with `*`; resume any of
    them with `tandem resume <id>` from anywhere.
    """
    cwd = _cwd()
    with StateStore() as store:
        _drop_abandoned(store)
        rows = store.list_sessions(limit=limit)
    if not rows:
        click.echo("No tandem sessions yet. Run `tandem` to start one.")
        return
    width = _title_width(fixed=2 + 12 + 2 + 9 + 2 + 8 + 2 + 22 + 2)
    click.echo(f"  {'ID':<12}  {'LAST USED':<9}  {'ACTIVE':<8}  "
               f"{'PARTICIPANTS':<22}  {'TITLE':<{width}}  DIRECTORY")
    for s in rows:
        mark = "*" if s.cwd == cwd else " "
        click.echo(
            f"{mark} {s.tandem_id:<12}  {_ago(s.last_used_at or s.created_at):<9}  "
            f"{s.active:<8}  {'+'.join(s.participants):<22}  {_title_cell(s, width)}  "
            f"{_short_dir(s.cwd)}"
        )
    click.echo()
    click.echo("Rows marked * are in this directory. Continue one with "
               "`tandem resume <id>` from anywhere, or "
               "`tandem native resume <id>` from its directory for the native frame.")


def _default_sink_factory(store, session, source, target):
    """Sync engine by default; TANDEM_LOG_EVENTS=1 switches to the debug
    event logger (no shadow writes)."""
    from .runner import EventLogger
    from .sync import SyncEngine

    if os.environ.get("TANDEM_LOG_EVENTS"):
        return EventLogger(session.tandem_id, source)
    return SyncEngine(store, session, source, target)


@main.command(name="run")
@click.option(
    "--on",
    "target",
    type=click.Choice(["claude", "codex", "opencode"]),
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
        if target not in session.participants:
            # Click admits every supported name; this session may hold fewer
            # (dropped member, or a build with no adapter for the name).
            click.secho(
                f"error: {target} is not a participant in this session "
                f"(participants: {', '.join(session.participants)}).",
                fg="red", err=True,
            )
            sys.exit(1)
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
@click.argument("tandem_id", required=False)
@click.option("--on", "harness", type=_HARNESS_CHOICE, default=None, help=_ON_HELP)
@_skip_permissions_option
@click.pass_context
def resume(ctx: click.Context, tandem_id: str | None, harness: str | None,
           skip_permissions: bool | None) -> None:
    """Resume a chat by ID, or choose from sessions across all directories.

    Restores the conversation, last-used harness and model pins. The session
    uses its saved working directory, regardless of where you launch tandem.
    Use `tandem native resume [ID]` for the CLIs' own interfaces."""
    _apply_skip_permissions(skip_permissions)
    parent_options = ctx.parent.params if ctx.parent is not None else {}
    _chat(harness if harness is not None else parent_options.get("harness"),
          False, tandem_id or "")


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True         # it exists; it is just not ours to signal
    return True


def _drop_abandoned(store: StateStore) -> None:
    """Forget chat sessions whose window died before its first turn. A closed
    terminal tab is a SIGHUP: no cleanup runs, and what is left is a row with
    no shadow transcripts — resumable in name only. The marker names the
    owning window, so one that is merely still open is left alone."""
    try:
        markers = list((paths.tandem_home() / "unused").iterdir())
    except OSError:
        return
    for marker in markers:
        try:
            pid = int(marker.read_text().strip())
        except (OSError, ValueError):
            pid = None
        if pid is not None and _pid_alive(pid):
            continue
        store.delete_session(marker.name)
        marker.unlink(missing_ok=True)


def _select_chat_session(store: StateStore, resume_id: str | None,
                         continue_last: bool) -> PairedSession | None:
    """Resolve explicit resume intent; cwd is session metadata, not a key.

    None means a fresh launch. Cancellation exits here so it cannot fall
    through into pairing, and a failed resume never creates a replacement.
    """
    if resume_id is None and not continue_last:
        return None
    if resume_id:
        session = store.get_session(resume_id)
        if session is None:
            raise click.ClickException(f"No tandem session {resume_id!r}. See `tandem sessions`.")
    else:
        rows = store.list_sessions(limit=1 if continue_last else None)
        if not rows:
            raise click.ClickException("No tandem sessions yet. Run `tandem` to start one.")
        if continue_last:
            session = rows[0]
        else:
            click.echo("Resume a session (all directories):")
            width = _title_width(fixed=5 + 12 + 2 + 9 + 2 + 8 + 2)
            click.echo(f"     {'ID':<12}  {'LAST USED':<9}  {'ACTIVE':<8}  "
                       f"{'TITLE':<{width}}  DIRECTORY")
            for i, row in enumerate(rows, 1):
                click.echo(
                    f"{i:>3}. {row.tandem_id:<12}  {_ago(row.last_used_at or row.created_at):<9}  "
                    f"{row.active:<8}  {_title_cell(row, width)}  {_short_dir(row.cwd)}")
            choice = click.prompt("Session number (0 to cancel)", type=click.IntRange(0, len(rows)))
            if choice == 0:
                raise click.exceptions.Exit(0)
            session = rows[choice - 1]
    if not Path(session.cwd).is_dir():
        raise click.ClickException(
            f"Session {session.tandem_id}'s working directory is missing: {session.cwd}")
    return session


def _chat(harness: str | None, fresh: bool, resume_id: str | None = None,
          continue_last: bool = False) -> None:
    from .chat.window import run_chat
    from .config import load_chat_config

    if sum((fresh, resume_id is not None, continue_last)) > 1:
        raise click.UsageError("--new, resume and --continue cannot be combined.")
    with StateStore() as store:
        _drop_abandoned(store)
        session = _select_chat_session(store, resume_id, continue_last)
        paired = session is None
        if paired:
            usable, _ = _resolve_participants()
            # a --on naming a harness this machine cannot run must never
            # become the fresh session's active slot: an active harness
            # outside the participants can never run a turn. Pair on the
            # default and let the participant check below report it.
            active = harness if harness in usable else usable[0]
            session = _pair_session(store, _cwd(), active, usable, seed=False)
        else:
            session = _narrow_participants(store, session)
        if harness is not None:
            if harness not in session.participants:
                click.secho(
                    f"error: {harness} is not a participant in this session "
                    f"(participants: {', '.join(session.participants)}).",
                    fg="red", err=True,
                )
                sys.exit(1)
            store.set_active(session.tandem_id, harness)
            session = store.get_session(session.tandem_id) or session
        store.touch_used(session.tandem_id)
        if not paired:
            click.echo(f"resuming {session.tandem_id} ({session.active} active, "
                       f"{_short_dir(session.cwd)})")
        if paired:
            from .plugin_setup import offer_install

            offer_install()
        if not paired:
            code = run_chat(session, store, load_chat_config())
        else:
            # The shadows wait for the first prompt, seeded from the session
            # as it was paired (a bare `/codex` typed first moves the active
            # slot, not what the pairing owes). A window that never got one —
            # opened and closed, or refused for want of a terminal — has
            # written nothing outside the state db, and is dropped whole.
            # A window killed outright runs none of this; the marker is how
            # the next launch knows to (_drop_abandoned).
            fresh, used = session, []
            marker = paths.unused_marker(session.tandem_id)
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(str(os.getpid()))

            def first_turn() -> None:
                _seed_shadows(store, fresh)
                used.append(1)
                marker.unlink(missing_ok=True)

            try:
                code = run_chat(session, store, load_chat_config(), first_turn=first_turn)
            finally:
                if not used:
                    store.delete_session(session.tandem_id)
                    marker.unlink(missing_ok=True)
    sys.exit(code)


@main.command()
@click.option("-m", "--model", default=None,
              help="Codex model name or shorthand (config default otherwise).")
@click.option("--context", "context_mode",
              type=click.Choice(["task", "full"]), default=None,
              help="Worker context: cold task-only, or a full fork of the "
                   "paired session (config policy decides by default).")
@click.option("-q", "--quiet", is_flag=True,
              help="Print only the worker's final message (raw codex output "
                   "goes to a log under ~/.tandem/subagents/<id>/logs). Used "
                   "by the bridge agent, which relays stdout verbatim.")
@click.option("--sandbox", "sandbox",
              type=click.Choice(["read-only", "workspace-write"]),
              default=None,
              help="Codex sandbox for this worker. Default: the consent the "
                   "dispatching claude session stamped via its permission "
                   "mode, else codex's configured default.")
@click.argument("task", required=False)
def sub(model: str | None, context_mode: str | None, quiet: bool,
        sandbox: str | None, task: str | None) -> None:
    """Run one delegated subagent task on codex (task argument or stdin).

    Used by the tandem plugin's codex-worker bridge; also works manually.
    A brief whose first line is `tandem-model: <name>` picks the codex
    model for this worker: the name is resolved against codex's own model
    catalog (~/.codex/models_cache.json), and a malformed or unresolvable
    name fails here, before codex is invoked, with the valid slugs listed.
    A generic name (`gpt`, `codex`) asks for no particular model and runs
    the configured default."""
    from . import modelcat, ops, pinstash
    from .config import load_subagents_config

    if task is None or task == "-":
        task = sys.stdin.read()
    task = task.strip()
    try:
        requested, task = modelcat.split_model_header(task)
    except modelcat.MalformedHeader as e:
        click.secho(f"error: {e}", fg="red", err=True)
        sys.exit(1)
    task = task.strip()
    if not task:
        click.secho("error: empty task brief.", fg="red", err=True)
        sys.exit(1)
    if not requested and model is None:
        # A headerless brief may be a relay echo that dropped the pin — the
        # known lossy hop — so consult the hook's out-of-band copy, keyed by
        # this exact body. A miss changes nothing; a hit rejoins the normal
        # header path below (resolution, announcement, trailer). -m callers
        # never rode the header protocol, so they skip the lookup.
        requested = pinstash.lookup(task)
    selected = model if model is not None else requested
    resolved = ""
    if selected:
        try:
            resolved = modelcat.resolve(selected, modelcat.load_catalog())
        except modelcat.UnknownModel as e:
            click.secho(f"error: {e}", fg="red", err=True)
            sys.exit(1)
    cfg = load_subagents_config()
    # A standin header ("gpt") resolves to "" and falls through to the config
    # default here, exactly like no header at all; the flag still outranks both.
    worker_model = resolved if model is not None else (resolved or cfg.model)
    if selected and not quiet:
        click.secho(f"worker model: {modelcat.model_label(worker_model)}",
                    err=True)
    with StateStore() as store:
        session = _require_session(store)
        code = ops.run_sub(
            store, session, task,
            model=worker_model,
            context=context_mode or ("full" if cfg.context == "full" else "task"),
            fanout_feature=cfg.fanout_feature,
            keep_forks=cfg.keep_forks,
            quiet=quiet,
            sandbox=sandbox if sandbox is not None
                    else _read_sandbox_stamp(session.tandem_id),
        )
    if requested and quiet:
        sys.stdout.write("\n" + modelcat.model_footer(worker_model) + "\n")
        sys.stdout.flush()
    sys.exit(code)


# Stamps are per claude session id and worthless once that session is gone;
# a week is long enough that a resumed session stays quiet.
_WARN_STAMP_TTL = 7 * 24 * 3600
_SESSION_ID_RE = re.compile(r"[A-Za-z0-9._-]{1,128}")


def _warn_stamp(payload: dict) -> Path | None:
    """Where this claude session's 'already warned' stamp lives, or None when
    the payload carries no usable `session_id`. The id is untrusted text, so
    anything that is not a plain filename component (traversal, separators)
    counts as absent — the notice then repeats rather than tandem writing
    outside TANDEM_HOME."""
    sid = payload.get("session_id")
    if not isinstance(sid, str) or not _SESSION_ID_RE.fullmatch(sid):
        return None
    if not sid.strip("."):        # "." and ".." match the pattern
        return None
    return paths.tandem_home() / "warned" / sid


def _already_warned(stamp: Path | None) -> bool:
    """True only when a notice is *provably* already out for this session.
    Every doubt resolves to False — repeating the one message that explains
    the silence is cheaper than swallowing it."""
    if stamp is None:
        return False
    try:
        return stamp.exists()
    except OSError:
        return False


def _mark_warned(stamp: Path | None) -> None:
    """Record the notice and opportunistically prune week-old stamps. All
    best-effort: the message is already printed, and no bookkeeping failure
    may reach the dispatch."""
    if stamp is None:
        return
    try:
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.touch()
        cutoff = time.time() - _WARN_STAMP_TTL
        entries = list(stamp.parent.iterdir())
    except OSError:
        return
    for p in entries:
        # per entry: one unreadable stamp (dangling symlink, vanished
        # mid-pass) must not abort the prune and strand every other one.
        # lstat, since a dangling link has no stat to follow.
        try:
            if p.lstat().st_mtime < cutoff:
                p.unlink()
        except OSError:
            pass


def _sandbox_stamp_path(tandem_id: str) -> Path:
    # tandem_id comes from our own state store (hex), never from payload
    # text, so it is safe as a filename component without filtering.
    return paths.tandem_home() / "sandbox" / tandem_id


def _stamp_sandbox(tandem_id: str, value: str) -> None:
    """Record the dispatching session's current write-consent for this pair.
    Rewritten on every dispatch so a mode change (including back to default)
    always wins; best-effort, because no stamp failure may reach the
    dispatch. Known race: two claude sessions dispatching on the same pair
    interleave last-write-wins; the window is the relay's spawn time."""
    try:
        p = _sandbox_stamp_path(tandem_id)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(value)
    except OSError:
        pass


def _read_sandbox_stamp(tandem_id: str) -> str:
    """The stamped consent, filtered to the one value we ever act on —
    anything unexpected (corrupt file, hand-edited) degrades to no flag."""
    try:
        text = _sandbox_stamp_path(tandem_id).read_text().strip()
    # ValueError covers the UnicodeDecodeError read_text() raises when the
    # file is not UTF-8: our caller has no blanket except, so an escaping
    # read failure would crash the dispatch instead of dropping the flag.
    except (OSError, ValueError):
        return ""
    return text if text == "workspace-write" else ""


@main.command(name="hook-route")
def hook_route_cmd() -> None:
    """Claude Code PreToolUse hook: reroute subagent dispatches to codex.

    Reads hook JSON on stdin; prints a decision or nothing. This function
    ALWAYS exits 0 — exit 2 would block the dispatch, and any failure here
    must degrade to native behavior.

    It also has one side effect beyond its output: every Agent/Task dispatch
    in a paired session writes the permission mode's sandbox consent to
    `$TANDEM_HOME/sandbox/<tandem_id>`, which the relay's `tandem sub` reads
    when it is given no `--sandbox` flag. That is how write consent reaches
    codex at all — it cannot ride the dispatch itself, since the relay's only
    channel to the worker is the untrusted brief.

    When nothing is rerouted because tandem is not usable here — no paired
    session for the cwd, or codex missing/unsupported — it prints a bare
    top-level `{"systemMessage": …}` instead: a user-visible line with NO
    permission decision, so claude still runs the dispatch natively. That
    fires once per claude session, stamped under `$TANDEM_HOME/warned/`.
    Stamp I/O is best-effort and failures warn again rather than go silent,
    since the notice exists precisely to explain otherwise-invisible
    behavior.

    The function body is not the whole story: click's usage-error path exits
    2 before this ever runs (version skew — plugin installed, an older
    tandem on PATH without this subcommand — or a stray argument). So the
    hook MUST be registered as `tandem hook-route || true`; that shell guard
    is what makes exit 2 unreachable in practice."""
    try:
        from . import pinstash
        from .config import load_subagents_config
        from .hookroute import (missed_reroute_notice, relay_pin, route,
                                sandbox_for_mode)

        payload = json.loads(sys.stdin.read() or "{}")
        cwd = payload.get("cwd") or _cwd()
        cfg = load_subagents_config()
        with StateStore() as store:
            session = _current_session(store, cwd)
        # Consent travels out-of-band: the relay's `tandem sub` reads this
        # stamp, so it must be current before the dispatch spawns the relay.
        # Stamped regardless of route config — a manual tandem:gpt dispatch
        # (never rewritten below) consents via permission mode all the same.
        if session is not None and payload.get("tool_name") in ("Agent", "Task"):
            _stamp_sandbox(session.tandem_id,
                           sandbox_for_mode(payload.get("permission_mode")))
        # The model pin travels the same out-of-band road as the consent
        # stamp, for the same reason: it rides the brief, and the relay's
        # echo of the brief is lossy (live transcripts show the
        # `tandem-model:` line dropped from the heredoc). Stash it while
        # the prompt is still pristine; `tandem sub` recovers it when its
        # stdin arrives headerless.
        pin = relay_pin(payload)
        if pin is not None:
            pinstash.stash(body=pin[1], requested=pin[0])
        codex_ok = False
        if session is not None:
            adapter = get_adapter("codex")
            v = adapter.detect_version()
            codex_ok = v is not None and adapter.version_supported(v)
        decision = route(payload, cfg, cwd, paths.claude_home(),
                         has_session=session is not None, codex_ok=codex_ok)
        if decision is not None:
            click.echo(json.dumps(decision))
        else:
            stamp = _warn_stamp(payload)
            notice = missed_reroute_notice(
                payload, cfg,
                has_session=session is not None, codex_ok=codex_ok,
                already_warned=_already_warned(stamp))
            # Accepted race: check and stamp are not atomic, so concurrent
            # first dispatches in one session can each print. Claiming the
            # stamp first (O_EXCL) would instead spend the session's single
            # notice on a caller that never got to print it.
            if notice is not None:
                click.echo(json.dumps(notice))
                _mark_warned(stamp)     # only ever stamp what we printed
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
        session = _current_session(store, _cwd())
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


@main.group()
def plugin() -> None:
    """Manage tandem's Claude Code plugin."""


@plugin.command(name="install")
def plugin_install_cmd() -> None:
    """Install the plugin via claude (marketplace add + plugin install)."""
    from .plugin_setup import install_plugin

    sys.exit(0 if install_plugin() else 1)


@main.group(invoke_without_command=True)
@click.option(
    "--active",
    type=_HARNESS_CHOICE,
    default=None,
    help="Initially active harness for the fresh session "
         "[default: first usable harness]",
)
@_skip_permissions_option
@click.pass_context
def native(ctx: click.Context, active: str | None, skip_permissions: bool | None) -> None:
    """Pair a fresh session and enter the active CLI's own TUI.

    Flip between the CLIs from the bar; `tandem native resume` continues an
    earlier session."""
    _apply_skip_permissions(skip_permissions)
    if ctx.invoked_subcommand is not None:
        if active is not None:
            raise click.UsageError("--active only applies to a fresh `tandem native` session.")
        return
    cwd = _cwd()
    usable, _ = _resolve_participants()
    if active is None:
        # No preference stated: first usable in configured order, so claude
        # users see no change and a claude-less machine still just works.
        active = usable[0]
    elif active not in usable:
        click.secho(f"error: --active {active} is not usable here "
                    f"(usable: {', '.join(usable)}).", fg="red", err=True)
        sys.exit(1)
    with StateStore() as store:
        session = _pair_session(store, cwd, active, usable)
    from .plugin_setup import offer_install

    offer_install()
    sys.exit(_enter_session(session))


native.add_command(native_resume)


def _enter_session(session: PairedSession) -> int:
    from .flip import run_session

    return run_session(session.tandem_id, _default_sink_factory)


if __name__ == "__main__":
    main()
