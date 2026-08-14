"""Per-harness adapter interface.

Each supported CLI gets exactly one module implementing this interface, so a
session-format change in one tool touches one file. The interface is generic
over harnesses (a future Gemini CLI adapter would slot in here) but v1 ships
only claude_code and codex.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from ..events import NormalizedEvent, SessionContext


class JsonlSourceReader:
    """Default source reader: the existing byte-offset JSONL tailer."""

    def __init__(self, path: Path, cursor):
        from ..tailer import JsonlTailer

        self.tailer = JsonlTailer(
            path, start_offset=cursor.byte_offset, start_line=cursor.line_index
        )

    def poll(self):
        return self.tailer.poll()


class ShadowBusy(RuntimeError):
    """Shadow store locked beyond busy_timeout. The append transaction
    rolled back — nothing landed — so the tailer retries next tick."""


class HarnessAdapter(ABC):
    id: str          # 'claude' | 'codex'
    display_name: str
    binary: str      # executable name on PATH
    install_hint: str  # one-line install command, shown when the CLI is absent

    # -- environment ---------------------------------------------------------

    @abstractmethod
    def detect_version(self) -> str | None:
        """Raw --version output of the installed CLI, or None if missing."""

    @abstractmethod
    def version_supported(self, version_text: str) -> bool: ...

    def runtime_ready(self) -> tuple[bool, str]:
        """Beyond version support: is this installed harness actually usable
        (storage reachable, schema sane)? Returns (ok, reason). Default
        True — only adapters with external runtime state override."""
        return True, ""

    def validate_transcript(self, path: Path, session_id: str | None) -> list[str]:
        """Structural dry-resume check. Default: parse JSONL, then the
        adapter-specific entry validation. Storage-backed adapters override
        the whole method."""
        problems, entries = _parse_jsonl_entries(path)
        if entries is None:
            return problems
        return problems + self._validate_entries(entries, session_id)

    def _validate_entries(self, entries, session_id) -> list[str]:
        return []

    # -- session files -------------------------------------------------------

    @abstractmethod
    def transcript_path(self, cwd: str, session_id: str) -> Path | None:
        """Expected transcript location for (cwd, session id). For harnesses
        whose filename embeds a creation timestamp (codex) this returns the
        discovered existing file, or None if not created yet."""

    @abstractmethod
    def mint_session_id(self) -> str:
        """Mint a new session id in this harness's native id format."""

    @abstractmethod
    def create_shadow_transcript(
        self, cwd: str, session_id: str, ctx: SessionContext, note: str
    ) -> Path:
        """Create a resume-ready transcript file containing only tandem-seeded
        content (no model was involved). Used when this harness starts life as
        the shadow."""

    # -- launching the real CLI ----------------------------------------------

    @abstractmethod
    def interactive_argv(self, session_id: str | None, fresh: bool) -> list[str]:
        """argv to run the harness interactively, pinned/resumed to session_id.
        fresh=True means the session file does not exist yet."""

    @abstractmethod
    def oneoff_argv(self, session_id: str, prompt: str) -> list[str]:
        """argv for a single non-interactive turn appended to session_id's
        native session (claude -p / codex exec resume)."""

    @abstractmethod
    def hook_argv_extra(self, sentinel: Path) -> list[str]:
        """Extra per-invocation argv that wires a turn-complete hook touching
        `sentinel`, without mutating the user's persistent config. The tailer
        uses the sentinel as a flush signal; fs-watching remains the
        fallback."""

    def quit_keystrokes(self) -> list[bytes]:
        """Byte chunks that cleanly exit the interactive CLI — clear the
        composer, then quit — sent in order with a short pause between.
        Pinned per harness version; live-verified at release (the ladder's
        SIGTERM rung backstops a recipe the CLI stops honoring)."""
        return []

    # -- transcript parsing / rendering (the converter halves) ---------------

    @abstractmethod
    def parse_entry(self, raw: dict[str, Any], ctx: SessionContext) -> list[NormalizedEvent]:
        """Translate one native transcript entry into normalized events.
        Unknown/irrelevant entry shapes yield SystemEvents (never raise)."""

    @abstractmethod
    def render_events(
        self, events: list[NormalizedEvent], ctx: SessionContext
    ) -> list[dict[str, Any]]:
        """Render normalized events into native transcript entries ready to
        append to this harness's session file (the shadow direction)."""

    @abstractmethod
    def render_placeholder(self, text: str, ctx: SessionContext) -> list[dict[str, Any]]:
        """Native entries for an untranslatable-turn placeholder."""

    # -- storage capabilities (file-backed defaults; opencode overrides) -----

    def make_source_reader(self, session, cursor, transcript: Path):
        return JsonlSourceReader(transcript, cursor)

    def watch_paths(self, session, transcript: Path) -> list[Path]:
        return [transcript]

    def shadow_append(self, ref: Path, entries: list[dict]) -> None:
        from ..util import append_jsonl_fsync

        append_jsonl_fsync(ref, entries)

    def shadow_intent(self, ref: Path, entries: list[dict]) -> dict:
        return {"pre_size": ref.stat().st_size}

    def intent_landed(self, ref: Path, intent: dict) -> bool:
        try:
            return ref.stat().st_size > int(intent["pre_size"])
        except (OSError, KeyError, ValueError):
            return False

    def fast_forward_cursor(self, session, cursor) -> None:
        """Mark everything currently in this SOURCE's store as consumed."""
        sid = session.native_id(self.id)
        path = self.transcript_path(session.cwd, sid) if sid else None
        if path is None:
            cursor.byte_offset = 0
            cursor.line_index = 0
            return
        data = path.read_bytes()
        cursor.byte_offset = len(data)
        cursor.line_index = data.count(b"\n")

    def pending_units(self, session, cursor) -> int:
        sid = session.native_id(self.id)
        path = self.transcript_path(session.cwd, sid) if sid else None
        if path is None:
            return 0
        try:
            data = path.read_bytes()
        except OSError:
            return 0
        if len(data) <= cursor.byte_offset:
            return 0
        return data[cursor.byte_offset:].count(b"\n")

    def prepare_shadow(self, ref, ctx) -> None:
        """Re-anchor renderer state to what is actually in the shadow store.
        Called before the first append of a sync run and after a crash-skip
        re-append. Default: nothing."""


def _parse_jsonl_entries(path: Path):
    """(problems, entries|None) — the shared JSONL prelude of the structural
    dry-resume check, split from the per-adapter entry validation."""
    import json

    problems: list[str] = []
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return [f"cannot read transcript: {exc}"], None
    if not raw.strip():
        return ["transcript is empty"], None
    entries: list[tuple[int, dict]] = []
    for i, line in enumerate(raw.splitlines()):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            problems.append(f"line {i}: not valid JSON")
            continue
        if not isinstance(obj, dict):
            problems.append(f"line {i}: not a JSON object")
            continue
        entries.append((i, obj))
    if not entries:
        return problems + ["no parseable entries"], None
    return problems, entries
