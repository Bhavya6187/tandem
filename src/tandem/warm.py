"""Process warmup: pre-booted standby harnesses for instant flips.

`LaunchRecipe` is the frozen record of how a harness invocation was (or
will be) launched. It exists because config is read per call
(`hook_argv_extra` re-reads the user's config.toml), so the recipe a
standby was spawned with must travel with it — the adopting runner reuses
the recorded recipe verbatim instead of rebuilding one that could
disagree with what is actually running.
"""

from __future__ import annotations

import os
import select
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import paths
from .config import load_harness_args
from .harness import get_adapter
from .ptyrun import PtyControl, _is_alive
from .state import PairedSession


@dataclass(frozen=True)
class LaunchRecipe:
    side: str                 # 'claude' | 'codex'
    argv: list[str]
    sentinel: Path
    hook_extra: list[str]     # tail of argv; marker_wired = bool(hook_extra)
    transcript: Path | None   # known transcript at build time (claude fresh:
                              # the expected path; codex fresh: None)
    fresh: bool
    cwd: str


def build_launch(session: PairedSession, side: str) -> LaunchRecipe:
    """The argv/sentinel/transcript for launching `side` of this session.
    Extracted from InteractiveRunner.run() so the standby and the runner
    build launches identically; argv order is interactive + user [args] +
    hook extras (tests pin the order)."""
    adapter = get_adapter(side)
    sid = getattr(session, f"{side}_session_id")
    transcript: Path | None = None
    fresh = True
    if sid:
        transcript = adapter.transcript_path(session.cwd, sid)
        fresh = transcript is None
        if fresh and side == "claude":
            transcript = adapter.expected_transcript_path(session.cwd, sid)
    sentinel = paths.tandem_home() / "tmp" / f"{session.tandem_id}-{side}.turn"
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    argv = adapter.interactive_argv(sid, fresh)
    argv += load_harness_args(side)
    hook_extra = adapter.hook_argv_extra(sentinel)
    argv += hook_extra
    return LaunchRecipe(
        side=side, argv=argv, sentinel=sentinel, hook_extra=hook_extra,
        transcript=transcript, fresh=fresh, cwd=session.cwd,
    )


def _shadow_size(session: PairedSession, side: str) -> int | None:
    """Byte size of `side`'s transcript right now; None when the side has
    no id or no file yet. The freshness contract's one number: any growth
    means content synced in behind a standby's back."""
    sid = getattr(session, f"{side}_session_id")
    if not sid:
        return None
    path = get_adapter(side).transcript_path(session.cwd, sid)
    if path is None:
        return None
    try:
        return path.stat().st_size
    except OSError:
        return None


class WarmChild:
    """One spawned-but-hidden harness process. Makes no decisions; carries
    the recipe it was launched under, the shadow-size snapshot the
    freshness gate compares against, and a discard reader that keeps the
    hidden PTY drained so the child never blocks on a full buffer."""

    def __init__(self, recipe: LaunchRecipe, child, shadow_size: int):
        self.recipe = recipe
        self.child = child
        self.shadow_size = shadow_size
        self._stop = threading.Event()
        self._reader = threading.Thread(
            target=self._discard, name="tandem-warm-read", daemon=True
        )
        self._reader.start()

    def _discard(self) -> None:
        while not self._stop.is_set():
            try:
                ready, _, _ = select.select([self.child.fd], [], [], 0.2)
            except InterruptedError:
                continue  # signal (e.g. SIGWINCH) — loop again, like the pump
            except (OSError, ValueError):
                return   # fd gone: child died or was released oddly
            if not ready:
                continue
            try:
                if not self.child.read(65536):
                    return
            except (EOFError, OSError):
                return

    def alive(self) -> bool:
        return _is_alive(self.child)

    def release(self) -> Any | None:
        """Stop and join the discard reader, hand the raw child over.
        Exactly one reader may own the fd — the pump takes over next — so a
        reader that refuses to join means there is nothing safe to hand
        over: returns None, and the caller falls back to a cold spawn."""
        self._stop.set()
        self._reader.join(timeout=2)
        if self._reader.is_alive():
            return None   # never hand over an fd we still own
        return self.child

    def kill(self) -> None:
        """Short version of the ladder: soft quit keys first so the CLI
        cleans its own state (claude removes its session-registry file),
        then TERM/KILL to the process group."""
        self._stop.set()
        self._reader.join(timeout=2)
        control = PtyControl()
        control.attach(self.child)
        control.terminate(
            get_adapter(self.recipe.side).quit_keystrokes(),
            soft_timeout=1.5, term_timeout=1.0,
        )


def spawn_hidden(
    recipe: LaunchRecipe, dims: tuple[int, int], shadow_size: int, *, spawn=None
) -> WarmChild:
    """Spawn the recipe on a hidden PTY, one column narrow on purpose: the
    attach-time setwinsize to true dims is then always a real change, so
    the kernel delivers SIGWINCH and the TUI repaints itself — the whole
    handover repaint story."""
    rows, cols = dims
    if spawn is None:   # deferred: ptyprocess import stays off the hot path
        from ptyprocess import PtyProcess
        spawn = PtyProcess.spawn
    child = spawn(
        recipe.argv,
        cwd=recipe.cwd,
        env=dict(os.environ),
        dimensions=(max(1, rows - 1), max(1, cols - 1)),
    )
    return WarmChild(recipe, child, shadow_size)


def _ensure_shadow_default(session: PairedSession) -> PairedSession | None:
    """Create the shadow transcript if it legitimately does not exist yet,
    via the same late-create helpers switch_session uses (same
    orientation: shadow side, pre-flip; their guards make the at-fire
    calls no-ops afterwards). Returns a refreshed session, or None when
    the state store has lost the row."""
    from . import ops
    from .state import StateStore

    try:
        with StateStore() as store:
            fresh = store.get_session(session.tandem_id)
            if fresh is None:
                return None
            if fresh.shadow == "codex" and not fresh.codex_session_id:
                ops._create_codex_shadow_late(store, fresh)
                fresh = store.get_session(session.tandem_id) or fresh
            elif fresh.shadow == "claude" and fresh.claude_session_id:
                expected = get_adapter("claude").expected_transcript_path(
                    fresh.cwd, fresh.claude_session_id
                )
                never_ran = (
                    store.get_cursor(fresh.tandem_id, "claude").byte_offset == 0
                )
                if not expected.exists() and never_ran:
                    ops._create_claude_shadow_late(store, fresh)
            return fresh
    except Exception:
        return None


class WarmStandby:
    """Holds at most one WarmChild. Spawns when the active side is idle,
    the shadow transcript has been byte-stable for the debounce window,
    and memory files are synced; kills+respawns when the shadow grows
    (a turn synced in behind the standby's back). Reads only three
    inputs — the idle probe, the shadow file stat, memory-sync state —
    and shares no protocol with the tail loop."""

    def __init__(self, session: PairedSession, is_idle, *, enabled: bool = True,
                 winsize=None, spawner=spawn_hidden, clock=time.monotonic,
                 sync_memory=None, ensure_shadow=None,
                 poll_s: float = 1.0, debounce_s: float = 1.5,
                 max_retries: int = 2):
        self.session = session
        self.is_idle = is_idle
        self.enabled = enabled
        self.winsize = winsize or (lambda: (24, 80))
        self.spawner = spawner
        self.clock = clock
        self.sync_memory = sync_memory
        self.ensure_shadow = ensure_shadow or _ensure_shadow_default
        self.poll_s = poll_s
        self.debounce_s = debounce_s
        self.max_retries = max_retries
        self.child: WarmChild | None = None
        self._last_size: int | None = None
        self._stable_since: float | None = None
        self._retries = 0
        self._ensure_refused = False
        # guards every transition of the `child` slot (and nothing else — the
        # kill ladder takes seconds and must never run under it)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="tandem-warm", daemon=True
        )

    def start(self) -> None:
        if self.enabled:
            self._thread.start()

    def shutdown(self, keep_child: bool) -> WarmChild | None:
        """Stop warming. Returns the live child for adoption when
        `keep_child`, else kills it. The runner calls this from its
        finally, after the monitor settles flip_requested."""
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=5)
        with self._lock:
            child, self.child = self.child, None
        if child is None:
            return None
        if keep_child and child.alive():
            return child
        child.kill()
        return None

    def _run(self) -> None:
        while not self._stop.wait(self.poll_s):
            try:
                self._tick()
            except Exception:
                pass   # opportunistic: the warm thread must never die noisily

    def _tick(self) -> None:
        size = _shadow_size(self.session, self.session.shadow)
        doomed: WarmChild | None = None
        note: str | None = None
        with self._lock:
            child = self.child
            if child is not None:
                if not child.alive():
                    # the process is gone, but its discard reader still owns
                    # the fd and, through the thread, the WarmChild itself
                    doomed, self.child = child, None   # consumes a retry
                    self._retries += 1
                    note = "standby exited before attach"
                elif size != child.shadow_size:
                    doomed, self.child = child, None   # a turn synced in
                    self._invalidated()
                    self._stable_since = None
                else:
                    return                  # fresh and waiting: nothing to do
        # empty the slot first, kill outside the lock: a shutdown racing us
        # must never adopt a child that is already going down the ladder
        if doomed is not None:
            doomed.kill()
        if note is not None:
            self._note_failure(note)
        if not self.is_idle():
            self._stable_since = None
            return
        now = self.clock()
        if size != self._last_size:
            self._last_size = size
            self._stable_since = now
            self._invalidated()     # new content with no child held: also an
            return                  # invalidation, so the budget re-arms
        if self._stable_since is None:
            self._stable_since = now
            return
        if now - self._stable_since < self.debounce_s:
            return
        if self._retries > self.max_retries:
            return                      # gave up until the next invalidation
        self._spawn(size)

    def _invalidated(self) -> None:
        """A real content change starts a new idle period: the retry budget
        re-arms (giving up is only ever 'until the next invalidation') and
        so does the ensure-shadow latch, since the file may exist now."""
        self._retries = 0
        self._ensure_refused = False

    def _spawn(self, size: int | None) -> None:
        session = self.session
        if size is None:
            if self._ensure_refused:
                return   # already asked once and got nothing; asking again
                         # every poll just churns the state store forever
            session = self.ensure_shadow(session)
            if session is None:
                self._retries += 1
                self._note_failure("session row vanished from the state store")
                return
            self.session = session
            size = _shadow_size(session, session.shadow)
            if size is None:
                # e.g. claude's consumed-then-missing transcript: that must
                # keep hard-failing at flip time — never boot a blank session
                self._ensure_refused = True
                return
        try:
            if self.sync_memory is not None:
                self.sync_memory()
            else:
                from .memory_sync import sync_memory_files
                sync_memory_files(session.cwd)
        except Exception:
            pass   # memory-sync failures surface at flip time as today
        try:
            # build_launch is inside the budget too: its sentinel mkdir can
            # fail (read-only home, full disk), and an uncounted failure
            # would retry at poll_s forever without ever telling the doctor
            recipe = build_launch(session, session.shadow)
            child = self.spawner(recipe, self.winsize(), size)
        except Exception as exc:
            self._retries += 1
            self._note_failure(f"spawn failed: {type(exc).__name__}: {exc}")
            return
        with self._lock:
            # shutdown sets _stop before it takes the lock, so one of us wins
            # cleanly: either it finds this child in the slot and disposes of
            # it, or we see the flag and kill it ourselves. Checking the flag
            # and filling the slot must be one step — a shutdown landing
            # between them would strand a setsid-detached hidden harness.
            if not self._stop.is_set():
                self.child = child
                return
        child.kill()

    def _note_failure(self, reason: str) -> None:
        """Leave the doctor a trail only once the budget is exhausted — a
        silently broken warmup otherwise just reads as 'flip feels slow'."""
        if self._retries <= self.max_retries:
            return
        marker = paths.tandem_home() / "tmp" / \
            f"{self.session.tandem_id}-warm-failed"
        try:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(reason + "\n")
        except OSError:
            pass
