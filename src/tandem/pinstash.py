"""Out-of-band model-pin stash: hook-route writes, `tandem sub` recovers.

A `tandem-model:` first line pins the codex model for one dispatch, but it
travels inside the brief, and the haiku relay that echoes the brief into
`tandem sub` drops that line from its heredoc in a substantial fraction of
dispatches (live transcripts, 2026-08-08 audit) — the worker then silently
runs the config default. At PreToolUse time the parent prompt is still
pristine, so hook-route stashes the requested name here, keyed by a hash of
the brief body, and `tandem sub` consults the stash when its stdin arrives
with no header.

Contract: best-effort on both sides, mirroring the sandbox stamp. A stash
write failure must never reach the dispatch, and a lookup returns the pinned
name only on an exact (edge-whitespace-normalized) body match — anything
else is "" and the dispatch behaves exactly as if this module did not exist.
Entries are not consumed on read, because the relay's one sanctioned retry
re-sends the same brief with --sandbox workspace-write; they expire by
mtime instead."""

from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path

from . import paths

# Long enough to cover a slow codex run plus the relay's sanctioned retry;
# short enough that an identical brief re-dispatched much later does not
# inherit a stale pin.
PIN_TTL = 3600


def _pins_dir() -> Path:
    return paths.tandem_home() / "model-pins"


def _key(body: str) -> str:
    # both writers normalize edge whitespace: the hook hashes the body split
    # off the parent prompt, sub hashes its stripped stdin
    return hashlib.sha256(
        body.strip().encode("utf-8", errors="replace")).hexdigest()


def stash(body: str, requested: str) -> None:
    """Record the requested model for a brief with exactly this body."""
    try:
        d = _pins_dir()
        d.mkdir(parents=True, exist_ok=True)
        _prune(d)
        # write-then-rename so a concurrent lookup never reads a torn entry
        tmp = d / f".{_key(body)}.tmp"
        tmp.write_text(requested)
        os.replace(tmp, d / _key(body))
    except OSError:
        pass


def lookup(body: str) -> str:
    """The pinned name for this exact brief body, or "" for any miss —
    absent, expired, unreadable, or implausible as a header name."""
    try:
        p = _pins_dir() / _key(body)
        if time.time() - p.stat().st_mtime > PIN_TTL:
            return ""
        name = p.read_text().strip()
    # ValueError covers the UnicodeDecodeError of a corrupt entry
    except (OSError, ValueError):
        return ""
    return name if _plausible(name) else ""


def _plausible(name: str) -> bool:
    """Would this name have survived the header parser? A corrupt entry that
    reached resolve() would abort the dispatch loudly, which is worse than
    the silent default this stash exists to improve on."""
    from . import modelcat
    try:
        parsed, _ = modelcat.split_model_header(f"tandem-model: {name}\nx")
    except modelcat.MalformedHeader:
        return False
    return parsed == name


def _prune(d: Path) -> None:
    cutoff = time.time() - PIN_TTL
    try:
        entries = list(d.iterdir())
    except OSError:
        return
    for p in entries:
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
        except OSError:
            pass
