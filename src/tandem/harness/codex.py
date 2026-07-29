"""Codex CLI adapter.

Session format observed on codex-cli 0.145.0 (docs/formats.md):
- rollout: ~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<uuidv7>.jsonl
- each line {timestamp, type, payload}; model-facing history is the
  response_item lines, UI history is the event_msg lines
- first line is session_meta; ~/.codex/session_index.jsonl indexes threads
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .. import compat, paths
from ..events import NormalizedEvent, SessionContext
from ..util import append_jsonl_fsync, iso_now_ms, uuid7
from .base import HarnessAdapter


class CodexAdapter(HarnessAdapter):
    id = "codex"
    display_name = "Codex CLI"
    binary = "codex"

    # -- environment ---------------------------------------------------------

    def detect_version(self) -> str | None:
        return compat.detect_cli_version(self.binary)

    def version_supported(self, version_text: str) -> bool:
        return compat.version_supported("codex", version_text)

    # -- session files -------------------------------------------------------

    def transcript_path(self, cwd: str, session_id: str) -> Path | None:
        return paths.find_codex_rollout(session_id)

    def mint_session_id(self) -> str:
        return uuid7()

    def create_shadow_transcript(
        self, cwd: str, session_id: str, ctx: SessionContext, note: str
    ) -> Path:
        """Create a rollout file shaped like codex's own, headed by a
        session_meta whose originator marks it as tandem-created."""
        now = datetime.now(timezone.utc)
        day_dir = paths.codex_sessions_dir() / now.strftime("%Y/%m/%d")
        fname = f"rollout-{now.strftime('%Y-%m-%dT%H-%M-%S')}-{session_id}.jsonl"
        path = day_dir / fname
        version = "0.0.0"
        raw = self.detect_version()
        if raw:
            parsed = compat.parse_version(raw)
            if parsed:
                version = ".".join(str(x) for x in parsed)
        ts = iso_now_ms()
        meta = {
            "timestamp": ts,
            "type": "session_meta",
            "payload": {
                "session_id": session_id,
                "id": session_id,
                "timestamp": ts,
                "cwd": cwd,
                "originator": "tandem",
                "cli_version": version,
                "source": "exec",
                "thread_source": "user",
                "history_mode": "legacy",
            },
        }
        entries = [meta] + self.render_note(note)
        append_jsonl_fsync(path, entries)
        return path

    def render_note(self, note: str) -> list[dict[str, Any]]:
        """A tandem-authored informational line, as both the model-facing
        response_item and the UI-facing event_msg."""
        ts = iso_now_ms()
        return [
            {
                "timestamp": ts,
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": note}],
                },
            },
            {
                "timestamp": ts,
                "type": "event_msg",
                "payload": {
                    "type": "user_message",
                    "message": note,
                    "images": [],
                    "local_images": [],
                    "audio": [],
                    "local_audio": [],
                    "text_elements": [],
                },
            },
        ]

    # -- launching -----------------------------------------------------------

    def interactive_argv(self, session_id: str | None, fresh: bool) -> list[str]:
        if fresh and session_id is None:
            # codex mints its own id; tandem captures the new rollout file.
            return [self.binary]
        assert session_id
        return [self.binary, "resume", session_id]

    def oneoff_argv(self, session_id: str, prompt: str) -> list[str]:
        return [self.binary, "exec", "--skip-git-repo-check", "resume", session_id, prompt]

    def hook_argv_extra(self, sentinel: Path) -> list[str]:
        # notify appends a JSON payload argument; route through sh -c so it
        # lands in $0 and is ignored.
        return ["-c", f'notify=["/bin/sh","-c","touch \'{sentinel}\'"]']

    # -- parse / render (M2/M3) ----------------------------------------------

    def parse_entry(self, raw: dict[str, Any], ctx: SessionContext) -> list[NormalizedEvent]:
        raise NotImplementedError

    def render_events(
        self, events: list[NormalizedEvent], ctx: SessionContext
    ) -> list[dict[str, Any]]:
        raise NotImplementedError

    def render_placeholder(self, text: str, ctx: SessionContext) -> list[dict[str, Any]]:
        raise NotImplementedError
