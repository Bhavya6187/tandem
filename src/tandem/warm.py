"""Process warmup: pre-booted standby harnesses for instant flips.

`LaunchRecipe` is the frozen record of how a harness invocation was (or
will be) launched. It exists because config is read per call
(`hook_argv_extra` re-reads the user's config.toml), so the recipe a
standby was spawned with must travel with it — the adopting runner reuses
the recorded recipe verbatim instead of rebuilding one that could
disagree with what is actually running.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import paths
from .config import load_harness_args
from .harness import get_adapter
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
