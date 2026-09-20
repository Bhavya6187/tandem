"""The paths the composer's `@` picker offers, and how a query ranks them.

`list_paths` asks git where it can — tracked files plus untracked ones that
are not ignored, so a build tree never floods the list — and walks the
directory, skipping dot-directories, where it cannot. Directories ride
along with a trailing slash: `@src/` is a mention too. `match` is pure."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Sequence

_CAP = 20000


def list_paths(cwd, cap: int = _CAP) -> list[str]:
    files = _git_files(cwd)
    if files is None:
        files = _walk(cwd, cap)
    files = files[:cap]
    dirs = {p[: i + 1] for p in files for i, ch in enumerate(p) if ch == "/"}
    return sorted(dirs.union(files))[:cap]


def _git_files(cwd) -> list[str] | None:
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=cwd, capture_output=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return [p for p in out.stdout.decode(errors="replace").split("\0") if p]


def _walk(cwd, cap: int) -> list[str]:
    found: list[str] = []
    for root, dirs, names in os.walk(cwd):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        rel = os.path.relpath(root, cwd)
        for name in sorted(names):
            found.append(name if rel == "." else f"{rel}/{name}".replace(os.sep, "/"))
            if len(found) >= cap:
                return found
    return found


def match(query: str, paths: Sequence[str], limit: int) -> list[str]:
    """Best first: a basename that starts with the query, a basename that
    contains it, a path that contains it, a path that has its characters in
    order. A query with a slash in it is about the path, not the basename.
    Shorter paths win a tie."""
    q = query.lower()
    ranked = []
    for path in paths:
        low = path.lower()
        base = low.rstrip("/").rpartition("/")[2]
        if "/" not in q and base.startswith(q):
            tier = 0
        elif "/" not in q and q in base:
            tier = 1
        elif q in low:
            tier = 2
        elif _subsequence(q, low):
            tier = 3
        else:
            continue
        ranked.append((tier, len(path), path))
    return [path for _, _, path in sorted(ranked)[:limit]]


def _subsequence(q: str, text: str) -> bool:
    chars = iter(text)
    return all(ch in chars for ch in q)
