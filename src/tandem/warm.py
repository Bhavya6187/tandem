"""Process warmup: the incoming harness boots while the outgoing one dies.

Nothing here runs on its own schedule. The runner fires `spawn_hidden`
from the monitor thread the moment a flip is decided, so the child boots
through the outgoing harness's teardown; whether that child is still
usable by the time the flip lands is flip.py's freshness gate to decide,
and this module only hands it the evidence (the recipe, the shadow-size
snapshot).

`LaunchRecipe` is the frozen record of how a harness invocation was (or
will be) launched. It exists because config is read per call
(`hook_argv_extra` re-reads the user's config.toml), so the recipe a
child was spawned with must travel with it — the adopting runner reuses
the recorded recipe verbatim instead of rebuilding one that could
disagree with what is actually running.
"""

from __future__ import annotations

import os
import select
import threading
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
    Extracted from InteractiveRunner.run() so the warm fire and the runner
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
    means content synced in behind the warm child's back."""
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
    attach path guarantees the resize to true dims is a real change (it
    nudges the child first when the dims already match), so the kernel
    delivers SIGWINCH and the TUI repaints itself — the whole handover
    repaint story."""
    rows, cols = dims
    if spawn is None:
        # `spawn` is the test seam (ptyprocess is already imported by ptyrun,
        # so this costs nothing and saves nothing); the import sits here so
        # the seam is the only thing a test has to substitute.
        from ptyprocess import PtyProcess
        spawn = PtyProcess.spawn
    child = spawn(
        recipe.argv,
        cwd=recipe.cwd,
        env=dict(os.environ),
        dimensions=(max(1, rows - 1), max(1, cols - 1)),
    )
    return WarmChild(recipe, child, shadow_size)
