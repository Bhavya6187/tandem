"""Filesystem locations for tandem and both wrapped harnesses.

Everything here was derived by inspecting the files written by the installed
CLIs (claude 2.1.220, codex-cli 0.145.0) — see docs/formats.md.
"""

from __future__ import annotations

import os
import re
from pathlib import Path


def tandem_home() -> Path:
    return Path(os.environ.get("TANDEM_HOME", Path.home() / ".tandem"))


def state_db_path() -> Path:
    return tandem_home() / "state.db"


def quarantine_dir(tandem_id: str) -> Path:
    return tandem_home() / "quarantine" / tandem_id


def log_dir() -> Path:
    return tandem_home() / "logs"


# --- Claude Code ------------------------------------------------------------

def claude_home() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))


def claude_munge_cwd(cwd: str | Path) -> str:
    """Claude Code names each project dir after the cwd with every character
    outside [A-Za-z0-9] replaced by '-'. Observed: /private/tmp/claude-501/x
    -> -private-tmp-claude-501-x (leading '/' becomes a leading '-',
    pre-existing '-' survive unchanged)."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(cwd))


def claude_project_dir(cwd: str | Path) -> Path:
    return claude_home() / "projects" / claude_munge_cwd(cwd)


def claude_transcript_path(cwd: str | Path, session_id: str) -> Path:
    return claude_project_dir(cwd) / f"{session_id}.jsonl"


# --- Codex ------------------------------------------------------------------

def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))


def codex_sessions_dir() -> Path:
    return codex_home() / "sessions"


_ROLLOUT_RE = re.compile(
    r"rollout-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-([0-9a-f-]{36})\.jsonl$"
)


def codex_rollout_session_id(path: Path) -> str | None:
    """The session UUID is embedded in the rollout filename."""
    m = _ROLLOUT_RE.search(path.name)
    return m.group(1) if m else None


def find_codex_rollout(session_id: str) -> Path | None:
    """Locate a rollout file by session id. Filenames embed the id, so this
    never needs to open files."""
    root = codex_sessions_dir()
    if not root.is_dir():
        return None
    for p in root.glob(f"**/rollout-*-{session_id}.jsonl"):
        return p
    return None


def iter_codex_rollouts_newest_first() -> list[Path]:
    root = codex_sessions_dir()
    if not root.is_dir():
        return []
    return sorted(
        root.glob("**/rollout-*.jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
