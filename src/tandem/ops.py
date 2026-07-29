"""Session operations shared by the CLI: catch-up drain, cursor
fast-forward, switch, and one-off routing.

Echo suppression: every entry tandem appends to a shadow is, by
construction, already represented in the other file. So whenever a harness
changes role (switch, or a one-off turn routed to it), we first drain the
outgoing source's unsynced tail, then fast-forward the incoming source's
cursor to end-of-file — from that point only genuinely new turns flow, and
nothing synced ever bounces back.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

from . import paths
from .harness import get_adapter
from .runner import TailLoop, await_codex_rollout
from .state import PairedSession, StateStore
from .sync import SyncEngine, SyncSetupError

# seam for tests (patching subprocess.run itself would also intercept the
# CLI version probes)
_run = subprocess.run


def source_transcript(session: PairedSession, source: str) -> Path | None:
    sid = getattr(session, f"{source}_session_id")
    if not sid:
        return None
    adapter = get_adapter(source)
    return adapter.transcript_path(session.cwd, sid)


def drain_source(store: StateStore, session: PairedSession, source: str) -> int:
    """Translate any unsynced tail of `source`'s file into the other file.
    Pure local file I/O. Returns lines consumed."""
    transcript = source_transcript(session, source)
    if transcript is None:
        return 0
    engine = SyncEngine(store, session, source)
    loop = TailLoop(store, session, source, transcript, engine)
    total = 0
    while True:
        n = loop.drain()
        total += n
        if n == 0:
            break
    if loop.errors:
        raise SyncSetupError("; ".join(loop.errors))
    return total


def fast_forward(store: StateStore, session: PairedSession, source: str) -> None:
    """Mark everything currently in `source`'s file as already-synced."""
    transcript = source_transcript(session, source)
    cursor = store.get_cursor(session.tandem_id, source)
    if transcript is None:
        cursor.byte_offset = 0
        cursor.line_index = 0
    else:
        data = transcript.read_bytes()
        cursor.byte_offset = len(data)
        cursor.line_index = data.count(b"\n")
    cursor.pending.pop("intent", None)
    store.save_cursor(cursor)


def unsynced_lines(session: PairedSession, store: StateStore, source: str) -> int:
    transcript = source_transcript(session, source)
    if transcript is None:
        return 0
    cursor = store.get_cursor(session.tandem_id, source)
    try:
        data = transcript.read_bytes()
    except OSError:
        return 0
    if len(data) <= cursor.byte_offset:
        return 0
    return data[cursor.byte_offset :].count(b"\n")


def switch_session(store: StateStore, session: PairedSession):
    """Flip active/shadow. Returns (new_active, problems-with-new-active-file,
    memory-sync report). Instant: catch-up drain of the old source + cursor
    fast-forward, no bulk re-conversion."""
    from .doctor import validate_transcript

    old_active, new_active = session.active, session.shadow

    # If codex never ran (id pending), its shadow file does not exist yet;
    # create it now so the flip has something to resume.
    if new_active == "codex" and not session.codex_session_id:
        _create_codex_shadow_late(store, session)
        session = store.get_session(session.tandem_id) or session

    drain_source(store, session, old_active)
    fast_forward(store, session, new_active)
    store.set_active(session.tandem_id, new_active)

    from .memory_sync import sync_memory_files

    memory_report = sync_memory_files(session.cwd)

    problems: list[str] = []
    transcript = source_transcript(session, new_active)
    if transcript is None:
        if new_active == "claude" and session.claude_session_id:
            # claude never launched; it will be created fresh on next run
            problems = []
        else:
            problems = ["transcript for newly active harness does not exist yet"]
    else:
        problems = validate_transcript(new_active, transcript,
                                       getattr(session, f"{new_active}_session_id"))
    return new_active, problems, memory_report


def _create_codex_shadow_late(store: StateStore, session: PairedSession) -> None:
    from .constants import SEED_NOTE
    from .runner import ctx_from_cursor

    adapter = get_adapter("codex")
    sid = adapter.mint_session_id()
    cursor = store.get_cursor(session.tandem_id, session.active)
    ctx = ctx_from_cursor(session, session.active, cursor)
    ctx.codex_session_id = sid
    note = SEED_NOTE.format(
        tandem_id=session.tandem_id, other=get_adapter(session.active).display_name
    )
    adapter.create_shadow_transcript(session.cwd, sid, ctx, note)
    store.set_native_session_id(session.tandem_id, "codex", sid)


def run_oneoff(
    store: StateStore, session: PairedSession, target: str, prompt: str
) -> int:
    """Route one prompt to `target` as a single native non-interactive turn,
    then sync that turn into the other file. Exactly one model (target's) is
    invoked."""
    adapter = get_adapter(target)
    sid = getattr(session, f"{target}_session_id")

    # Catch up the active side first, then mark the target's whole file as
    # known so only the new turn flows back afterwards. (When target IS the
    # active side there is nothing to fast-forward — its cursor is live.)
    drain_source(store, session, session.active)
    if target != session.active and sid and source_transcript(session, target) is not None:
        fast_forward(store, session, target)

    started = time.time()
    if target == "codex" and not sid:
        # codex never ran: no session to resume; a fresh exec creates one.
        argv = [adapter.binary, "exec", "--skip-git-repo-check", prompt]
    else:
        argv = adapter.oneoff_argv(sid, prompt)
    code = _run(argv, cwd=session.cwd).returncode

    if target == "codex" and not sid:
        rollout = await_codex_rollout(session.cwd, started, timeout=10)
        if rollout:
            new_sid = paths.codex_rollout_session_id(rollout)
            if new_sid:
                store.set_native_session_id(session.tandem_id, "codex", new_sid)
                session = store.get_session(session.tandem_id) or session
                fast_forward_to_zero = store.get_cursor(session.tandem_id, "codex")
                fast_forward_to_zero.byte_offset = 0
                fast_forward_to_zero.line_index = 0
                store.save_cursor(fast_forward_to_zero)

    drain_source(store, session, target)
    return code
