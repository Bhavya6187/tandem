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

import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

from . import paths
from .harness import get_adapter
from .harness.codex import output_text
from .runner import TailLoop, await_codex_rollout
from .state import PairedSession, StateStore
from .sync import SyncEngine, SyncSetupError
from .util import append_jsonl_fsync, read_jsonl, uuid7

# seam for tests (patching subprocess.run itself would also intercept the
# CLI version probes)
_run = subprocess.run


def source_transcript(session: PairedSession, source: str) -> Path | None:
    sid = session.native_id(source)
    if not sid:
        return None
    adapter = get_adapter(source)
    return adapter.transcript_path(session.cwd, sid)


def drain_source(
    store: StateStore, session: PairedSession, source: str,
    *, flush_dangling: bool = False,
) -> int:
    """Translate any unsynced tail of `source` into EVERY other participant.
    One engine+loop per (source, target) direction; per-direction cursors
    keep progress independent. Returns total lines consumed across targets.
    With flush_dangling=True, close any still-unpaired tool calls with
    placeholder results afterwards (required when the source is being handed
    off: both replay APIs reject a dangling call)."""
    transcript = source_transcript(session, source)
    if transcript is None:
        return 0
    total = 0
    for target in session.targets_for(source):
        engine = SyncEngine(store, session, source, target)
        loop = TailLoop(store, session, source, target, transcript, engine)
        while True:
            n = loop.drain()
            total += n
            if n == 0:
                break
        if loop.errors:
            raise SyncSetupError("; ".join(loop.errors))
        if flush_dangling:
            engine.flush_dangling(loop.ctx, loop.cursor)
    return total


def fast_forward(store: StateStore, session: PairedSession, source: str,
                 target: str) -> None:
    """Mark everything currently in `source`'s store as already-synced for
    the (source -> target) direction."""
    cursor = store.get_cursor(session.tandem_id, source, target)
    get_adapter(source).fast_forward_cursor(session, cursor)
    cursor.pending.pop("intent", None)
    store.save_cursor(cursor)


def fast_forward_all(store: StateStore, session: PairedSession, source: str) -> None:
    """Every outgoing direction of `source` — the flip's anti-echo step.
    Only runtime-participant directions exist; cursors toward a dropped
    member are never created, advanced, or inspected."""
    for target in session.targets_for(source):
        fast_forward(store, session, source, target)


def unsynced_lines(session: PairedSession, store: StateStore, source: str,
                   target: str) -> int:
    cursor = store.get_cursor(session.tandem_id, source, target)
    return get_adapter(source).pending_units(session, cursor)


def switch_session(store: StateStore, session: PairedSession,
                   to: str | None = None):
    """Flip active role to `to` (default: next in cycle). Drains the old
    active into every target, fast-forwards ALL outgoing directions of the
    new active (anti-echo), then records the switch. Returns (new_active,
    problems-with-new-active-file, memory-sync report). Instant: catch-up
    drain of the old source + cursor fast-forward, no bulk re-conversion."""
    from .doctor import validate_transcript

    old_active = session.active
    new_active = to or session.next_active(old_active)
    if new_active not in session.participants:
        raise SyncSetupError(f"{new_active} is not a participant")

    # If codex never ran (id pending), its shadow file does not exist yet;
    # create it now so the flip has something to resume.
    if new_active == "codex" and not session.native_id("codex"):
        _create_codex_shadow_late(store, session)
        session = store.get_session(session.tandem_id) or session

    # If claude never ran, its file does not exist either: claude's CLI
    # creates the transcript on the first turn, not at launch, so a flip
    # away from a zero-turn claude leaves its recorded id with no file. That
    # bites whichever way this flip goes: leaving claude makes it a fileless
    # TARGET of every other side's drain (the runner's ->claude engine and
    # every later flip's drain refuse to start on a missing shadow), and
    # flipping back into it leaves the drain below no file to append to.
    # Seed it now — but only when tandem has never consumed a byte of it; a
    # consumed-then-missing file is data loss, and the drain's hard error is
    # the right answer there.
    if session.native_id("claude"):
        expected = get_adapter("claude").expected_transcript_path(
            session.cwd, session.native_id("claude")
        )
        never_ran = all(
            store.get_cursor(session.tandem_id, "claude", t).byte_offset == 0
            for t in session.targets_for("claude")
        )
        if not expected.exists() and never_ran:
            other = new_active if old_active == "claude" else old_active
            _create_claude_shadow_late(store, session, other)

    drain_source(store, session, old_active, flush_dangling=True)
    fast_forward_all(store, session, new_active)
    store.set_active(session.tandem_id, new_active)

    from .memory_sync import sync_memory_files

    memory_report = sync_memory_files(session.cwd)

    problems: list[str] = []
    transcript = source_transcript(session, new_active)
    if transcript is None:
        if new_active == "claude" and session.native_id("claude"):
            # claude never launched; it will be created fresh on next run
            problems = []
        else:
            problems = ["transcript for newly active harness does not exist yet"]
    else:
        problems = validate_transcript(new_active, transcript,
                                       session.native_id(new_active))
    return new_active, problems, memory_report


def _create_claude_shadow_late(store: StateStore, session: PairedSession,
                               other: str) -> None:
    """Seed claude's transcript after the fact. `other` is the harness whose
    turns will flow into it next (the side being left when claude is the
    incoming active, the side being entered when claude is the outgoing
    one): the ctx belongs to the (other -> claude) direction."""
    from .constants import SEED_NOTE
    from .runner import ctx_from_cursor

    adapter = get_adapter("claude")
    cursor = store.get_cursor(session.tandem_id, other, "claude")
    ctx = ctx_from_cursor(session, cursor)
    # Any leaf uuid the cursor still holds points into the missing file;
    # the seed is the new file's root, so it must not chain onto it.
    ctx.state_for("claude")["leaf_uuid"] = None
    note = SEED_NOTE.format(
        tandem_id=session.tandem_id, other=get_adapter(other).display_name
    )
    adapter.create_shadow_transcript(session.cwd, session.native_id("claude"), ctx, note)
    cursor.pending.setdefault("harness_state", {}).setdefault("claude", {})[
        "leaf_uuid"
    ] = ctx.state_for("claude").get("leaf_uuid")
    store.save_cursor(cursor)


def _create_codex_shadow_late(store: StateStore, session: PairedSession) -> None:
    from .constants import SEED_NOTE
    from .runner import ctx_from_cursor

    adapter = get_adapter("codex")
    sid = adapter.mint_session_id()
    # only ever called with new_active == "codex": the (active -> codex) ctx
    cursor = store.get_cursor(session.tandem_id, session.active, "codex")
    ctx = ctx_from_cursor(session, cursor)
    ctx.target_session_id = sid
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
    sid = session.native_id(target)

    # Catch up the active side first, then mark the target's whole file as
    # known so only the new turn flows back afterwards. (When target IS the
    # active side there is nothing to fast-forward — its cursor is live.)
    drain_source(store, session, session.active, flush_dangling=True)
    if target != session.active and sid and source_transcript(session, target) is not None:
        fast_forward_all(store, session, target)

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
                fast_forward_to_zero = store.get_cursor(session.tandem_id, "codex", "claude")
                fast_forward_to_zero.byte_offset = 0
                fast_forward_to_zero.line_index = 0
                store.save_cursor(fast_forward_to_zero)

    # Echo suppression per direction (see the module docstring): the drain
    # below appends tandem's translation of the target's turn into every
    # other participant's file. Those appends are by construction already
    # represented in the target's file, so each recipient that was fully
    # synced before the drain fast-forwards its own outgoing cursors past
    # the copy — otherwise its next drain translates them straight back,
    # duplicating call ids and text.
    echo_pre: dict[str, tuple[int | None, dict[str, int]]] = {}
    for side in session.targets_for(target):
        size = _file_size(source_transcript(session, side))
        offsets = {
            t: store.get_cursor(session.tandem_id, side, t).byte_offset
            for t in session.targets_for(side)
        }
        echo_pre[side] = (size, offsets)

    drain_source(store, session, target, flush_dangling=True)

    # Only when a recipient was fully synced before the drain is everything
    # now in its file known to be ours. If it had an unsynced tail (a
    # concurrent writer), fast-forwarding would swallow a live turn: leave
    # its cursors alone and let the normal drain pick both up.
    for side, (pre_size, offsets) in echo_pre.items():
        if pre_size is None:
            continue
        if all(off == pre_size for off in offsets.values()):
            fast_forward_all(store, session, side)
    return code


def _file_size(path: Path | None) -> int | None:
    if path is None:
        return None
    try:
        return path.stat().st_size
    except OSError:
        return None


@contextmanager
def _sub_lock():
    """Serializes drain-then-fork across parallel `tandem sub` processes.
    The cold task path never takes this lock — seeding its own empty rollout
    touches no cursor and no shadow."""
    lock_path = paths.tandem_home() / "sub.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def fork_shadow(store: StateStore, session: PairedSession) -> tuple[str, Path]:
    """Copy the codex shadow rollout into a fresh ephemeral rollout for one
    subagent worker: same history, new uuid7 identity, originator
    'tandem-sub'. The fork is NEVER registered as a sync source. Callers MUST
    hold `_sub_lock()` across this call: it drains the active source first,
    and concurrent drains against the same cursor row corrupt sync state.
    Returns (fork_session_id, fork_path)."""
    if not session.native_id("codex"):
        _create_codex_shadow_late(store, session)
        session = store.get_session(session.tandem_id) or session
    drain_source(store, session, session.active, flush_dangling=True)
    src = source_transcript(session, "codex")
    if src is None:
        raise SyncSetupError("codex shadow rollout not found")
    entries = read_jsonl(src)
    if not entries or entries[0].get("type") != "session_meta":
        raise SyncSetupError("shadow rollout has no session_meta first line")
    fork_id = uuid7()
    meta = json.loads(json.dumps(entries[0]))  # deep copy
    meta["payload"]["id"] = fork_id
    meta["payload"]["session_id"] = fork_id
    meta["payload"]["originator"] = "tandem-sub"
    fork_path = get_adapter("codex").rollout_path(fork_id)
    append_jsonl_fsync(fork_path, [meta] + entries[1:])
    return fork_id, fork_path


def seed_sub_rollout(session: PairedSession) -> tuple[str, Path]:
    """Author the minimal rollout a cold (`context='task'`) worker resumes:
    fresh uuid7 identity, originator 'tandem-sub', one seed note and no
    shared history.

    Cold runs resume this instead of `codex exec`-ing fresh because a plain
    exec writes an ordinary rollout — non-tandem originator, session cwd,
    fresh mtime — which `await_codex_rollout` would hand to the next
    codex-id capture, binding the pair's codex_session_id to a throwaway
    worker transcript tandem then deletes. Seeding keeps every sub rollout
    behind the same discovery guard as forks.

    Touches no cursor and no shadow: no drain, no `_sub_lock` needed.
    Returns (seed_session_id, seed_path)."""
    from .constants import SUB_SEED_NOTE

    adapter = get_adapter("codex")
    seed_id = adapter.mint_session_id()
    seed_path = adapter.rollout_path(seed_id)
    entries = [adapter.session_meta(session.cwd, seed_id, originator="tandem-sub")]
    entries += adapter.render_note(SUB_SEED_NOTE)
    append_jsonl_fsync(seed_path, entries)
    return seed_id, seed_path


def run_sub(
    store: StateStore,
    session: PairedSession,
    task: str,
    *,
    model: str = "",
    context: str = "task",
    fanout_feature: str = "",
    sandbox: str = "",
    keep_forks: bool = False,
    quiet: bool = False,
) -> int:
    """Execute one delegated subagent task on codex. context='task' resumes
    a freshly seeded empty rollout in the session cwd (claude wrote a
    self-contained brief for a cold worker); context='full' forks the shadow
    and resumes that instead. Either way the worker runs in a tandem-authored
    'tandem-sub' rollout that is never a sync source and never adoptable as
    the pair's own codex session, and is disposed of on exit. The brief is
    passed through verbatim, on codex's stdin (`resume <id> -`), never as
    argv. Exit code mirrors codex.

    `sandbox` is codex's `--sandbox` value ("read-only"/"workspace-write"),
    empty for codex's own configured default. It is caller-validated — the CLI
    accepts it only from a click.Choice flag or the dispatching session's
    consent stamp — and can never come from the brief, which reaches codex on
    stdin and never touches argv.

    quiet=True is the bridge-agent mode: codex's raw transcript goes to a log
    file and this command's ENTIRE stdout becomes the worker's final message
    (via codex's own `-o/--output-last-message`). With inherited stdio the
    caller would instead get the whole exec log — header, actions, token
    counts — and asking a cheap relay model to extract "the final message"
    from that is unreliable. quiet=False is byte-for-byte unchanged: stdio is
    inherited so manual runs still stream live."""
    adapter = get_adapter("codex")
    argv = [adapter.binary, "exec", "--skip-git-repo-check"]
    if model:
        argv += ["-m", model]
    if fanout_feature:
        argv += ["--enable", fanout_feature]
    if sandbox:
        # exec-level flag: must precede the `resume` subcommand, like -m.
        # Value is caller-validated ("read-only"/"workspace-write"); the
        # brief can never influence it (stdin-only transport).
        argv += ["--sandbox", sandbox]
    if context == "full":
        with _sub_lock():
            sub_id, sub_path = fork_shadow(store, session)
    else:
        sub_id, sub_path = seed_sub_rollout(session)

    # Everything already in the rollout predates this run (a fork inherits the
    # pair's whole codex history); only patches attempted below are this
    # worker's. Newline count, as in fast_forward: whole-line JSONL, so it is
    # the entry index — and an unreadable file just means "count from zero".
    try:
        sub_pre_entries = sub_path.read_bytes().count(b"\n")
    except OSError:
        sub_pre_entries = 0

    sub_root = paths.tandem_home() / "subagents" / session.tandem_id
    last_path = log_path = None
    if quiet:
        logs = sub_root / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        last_path, log_path = logs / f"{sub_id}.last", logs / f"{sub_id}.log"
        # exec-level flag: must precede the `resume` subcommand, like -m
        argv += ["-o", str(last_path)]
    # `-` is codex's documented "read the prompt from stdin" marker. The brief
    # must never be a trailing positional: it is untrusted text, so codex's own
    # parser reads a leading `-` as a flag — "- review …" dies with `unexpected
    # argument` before the model runs, and "-m gpt-9 …" is silently honored as
    # a flag. stdin keeps it opaque, and drops the argv length limit too.
    argv += ["resume", sub_id, "-"]

    marker = sub_root / "running" / f"{sub_id}.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({
        "model": model, "context": context, "pid": os.getpid(),
        # collapsed: `tandem status` prints this on one line, and briefs are
        # multi-paragraph
        "task_preview": " ".join(task.split())[:120],
    }))
    brief = task.encode()
    try:
        if quiet:
            with open(log_path, "wb") as fh:
                code = _run(argv, cwd=session.cwd, input=brief, stdout=fh,
                            stderr=subprocess.STDOUT).returncode
        else:
            code = _run(argv, cwd=session.cwd, input=brief).returncode
    finally:
        # The worker's answer is what this run exists to produce; disposal is
        # bookkeeping. Relay first so a raising cleanup cannot swallow a result
        # codex already produced and billed for.
        if quiet:
            _relay_last_message(last_path, log_path)
            # Bridge protocol: turn "sandbox rejected the writes" from prose
            # buried in the answer into a fixed trailer the orchestrating
            # session can match on. Must read sub_path before disposal below.
            rejected = blocked_write_paths(sub_path, since=sub_pre_entries)
            if rejected:
                sys.stdout.write(blocked_footer(
                    rejected, retry_hint=sandbox != "workspace-write"))
                sys.stdout.flush()
        marker.unlink(missing_ok=True)
        if keep_forks:
            sub_root.mkdir(parents=True, exist_ok=True)
            # move, not rename: codex's sessions dir and TANDEM_HOME can sit on
            # different filesystems, and rename() across devices raises
            shutil.move(sub_path, sub_root / sub_path.name)
        else:
            sub_path.unlink(missing_ok=True)
    return code


# The bridge-protocol marker for "codex finished but the sandbox rejected
# its writes". The orchestrating session matches on this exact line, so it
# is public API: change it and every dispatching model's instructions rot.
BLOCKED_HEADER = "[tandem-sub blocked: write]"


# Codex's literal marker in a failed exec's output when the sandbox (or an
# approval policy) refuses a patch. Observed text: "patch rejected: writing
# is blocked by read-only sandbox; rejected by user approval settings".
#
# Deliberately never on the same physical line as the patch tool's name: a
# worker that greps this repo for that name would otherwise get a hit line
# carrying both literals, which is exactly the shape the join below reads as
# a real rejection.
PATCH_REJECTED = "patch rejected"

# Anchored, because "somewhere in the output" is not a rejection. Codex prints
# the marker as its own line ("Script error:\npatch rejected: …"), while a
# worker grepping this repo gets it back inside an `rg` hit line, prefixed by
# `path:lineno:`. Anchoring is what separates the two — and it is the only
# thing that does once the grep pattern also names the patch tool, since then
# the call itself looks like a patch to the gate below.
_PATCH_REJECTED_RE = re.compile("^" + re.escape(PATCH_REJECTED), re.M)

# `*** Add File: <path>` out of the patch text. That text usually reaches us
# embedded in a JS string literal (`tools.apply_patch("*** Begin Patch\n…")`),
# so a patch line ends at a literal two-char `\n` ESCAPE at least as often as
# at a real newline — and the last one ends at the closing quote. Stop at all
# three, or the path swallows the rest of the script.
_PATCH_TARGET_RE = re.compile(
    r"""\*\*\* (?:Add|Update|Delete) File: (.+?)(?=\\n|[\n"']|$)""")


def blocked_write_paths(sub_path: Path, *, since: int = 0) -> list[str]:
    """Paths whose apply_patch the sandbox rejected during this run, in event
    order. Anything unreadable or unexpected yields [] — detection is
    advisory, never load-bearing.

    Two shapes, because the obvious one is not the one that fires. Live probe
    (2026-08-01, `codex exec --sandbox read-only`): a refused write emits NO
    `patch_apply_end` at all — codex writes that event when a patch APPLIES
    (every such event on that machine carried success:true). The rejection
    survives only as the `custom_tool_call` running apply_patch plus its
    call_id-matched `custom_tool_call_output` carrying `patch rejected`. The
    `patch_apply_end` success:false branch is kept for other codex flows that
    may still produce it.

    `since` is the entry count the rollout had before the run: a
    `--context full` fork carries the pair's real codex history, rejections
    from earlier interactive turns included, and those are not this worker's
    doing."""
    try:
        entries = read_jsonl(sub_path)[since:]
    except Exception:
        return []
    rejected: list[str] = []
    scripts: dict[str, str] = {}   # call_id -> the apply_patch invocation

    def add(found: list[str]) -> None:
        paths_ = [p.strip() for p in found if isinstance(p, str) and p.strip()]
        rejected.extend(
            p for p in (paths_ or ["(unknown path)"]) if p not in rejected)

    for e in entries:
        if not isinstance(e, dict):
            continue
        p = e.get("payload")
        if not isinstance(p, dict):
            continue
        ptype = p.get("type")
        if e.get("type") == "event_msg" and ptype == "patch_apply_end":
            if p.get("success"):
                continue
            changes = p.get("changes")
            add(list(changes) if isinstance(changes, dict) else [])
        elif ptype == "custom_tool_call":
            # the call always precedes its output, so a forward pass resolves
            # the join without a second scan (and never reaches back past
            # `since` for a script this run did not run)
            call_id, src = p.get("call_id"), p.get("input")
            if isinstance(call_id, str) and isinstance(src, str):
                scripts[call_id] = src
        elif ptype == "custom_tool_call_output":
            call_id = p.get("call_id")
            src = scripts.get(call_id, "") if isinstance(call_id, str) else ""
            # Only a call that actually tried to patch can be a blocked write.
            # Matching the marker in ANY tool output false-positives on a
            # worker that merely grepped for the string — this very file
            # contains it, so `rg 'patch rejected' src/` was enough to fake a
            # rejection and push the orchestrator into a needless escalation.
            if "*** Begin Patch" not in src and "apply_patch" not in src:
                continue
            # ... and one grep for both literals passes that gate on its own,
            # so the marker must also sit where codex puts it: line-anchored.
            if not _PATCH_REJECTED_RE.search(output_text(p.get("output"))):
                continue
            add(_PATCH_TARGET_RE.findall(src))
    return rejected


def blocked_footer(rejected: list[str], *, retry_hint: bool) -> str:
    lines = [
        BLOCKED_HEADER,
        # NOT "no files were modified": under workspace-write a run can apply
        # several patches and have a later one refused (a path outside the
        # workspace, say), so the only claim this footer can make is about the
        # patches it actually saw rejected.
        "The codex worker's file changes were rejected by its sandbox; "
        "the listed changes were not applied. Rejected: "
        + ", ".join(rejected[:10]),
    ]
    if retry_hint:
        lines.append(
            "To grant writes: message this worker to rerun the same task "
            "with `tandem sub -q --sandbox workspace-write`, or ask it to "
            "return the content and apply the changes yourself.")
    return "\n".join(lines) + "\n"


def _relay_last_message(last_path: Path, log_path: Path, tail: int = 50) -> None:
    """Print exactly what the bridge should return as its final message: the
    worker's last message, or — when codex died before writing one — the tail
    of its log, so a failed relay still carries the error text instead of
    reporting an empty failure. Never prefixes or wraps: the caller's whole
    stdout is the payload. Both files are retained as the debugging trail."""
    text = ""
    try:
        text = last_path.read_text(errors="replace")
    except OSError:
        pass
    if not text.strip():
        try:
            lines = log_path.read_text(errors="replace").splitlines()
        except OSError:
            lines = []
        text = "\n".join(lines[-tail:])
    if not text:
        return
    sys.stdout.write(text if text.endswith("\n") else text + "\n")
    sys.stdout.flush()
