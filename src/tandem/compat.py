"""Pinned CLI compatibility table and installed-version detection.

Both session formats are internal to their CLIs and drift between releases.
Tandem pins the ranges it was built against; outside a range we warn and
require `tandem doctor` before enabling sync (see cli.py).
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass


@dataclass(frozen=True)
class CompatRange:
    tested: str          # exact version this code was developed against
    min_version: tuple[int, ...]
    max_exclusive: tuple[int, ...]


# Format observations in docs/formats.md correspond to these versions.
COMPAT: dict[str, CompatRange] = {
    "claude": CompatRange(tested="2.1.220", min_version=(2, 0), max_exclusive=(3,)),
    "codex": CompatRange(tested="0.145.0", min_version=(0, 140), max_exclusive=(0, 150)),
}

_VERSION_RE = re.compile(r"(\d+(?:\.\d+)+)")


def parse_version(text: str) -> tuple[int, ...] | None:
    m = _VERSION_RE.search(text)
    if not m:
        return None
    return tuple(int(x) for x in m.group(1).split("."))


def detect_cli_version(binary: str) -> str | None:
    """Return the raw version string of an installed CLI, or None if absent."""
    if shutil.which(binary) is None:
        return None
    try:
        out = subprocess.run(
            [binary, "--version"], capture_output=True, text=True, timeout=20
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() or out.stderr.strip() or None


def version_supported(harness: str, version_text: str) -> bool:
    rng = COMPAT[harness]
    v = parse_version(version_text)
    if v is None:
        return False
    return rng.min_version <= v < rng.max_exclusive
