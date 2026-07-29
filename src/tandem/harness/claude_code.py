"""Claude Code adapter.

Session format observed on claude 2.1.220 (docs/formats.md):
- transcript: ~/.claude/projects/<munged-cwd>/<sessionId>.jsonl
- entries chained by uuid/parentUuid; types: user, assistant, attachment,
  queue-operation, last-prompt, summary, system
- assistant API messages are split one content block per JSONL line, sharing
  message.id
"""

from __future__ import annotations

import json
import uuid as uuidlib
from pathlib import Path
from typing import Any

from .. import compat, paths
from ..events import NormalizedEvent, SessionContext
from ..util import append_jsonl_fsync, git_branch, iso_now_ms
from .base import HarnessAdapter


class ClaudeCodeAdapter(HarnessAdapter):
    id = "claude"
    display_name = "Claude Code"
    binary = "claude"

    # -- environment ---------------------------------------------------------

    def detect_version(self) -> str | None:
        return compat.detect_cli_version(self.binary)

    def version_supported(self, version_text: str) -> bool:
        return compat.version_supported("claude", version_text)

    # -- session files -------------------------------------------------------

    def transcript_path(self, cwd: str, session_id: str) -> Path | None:
        p = paths.claude_transcript_path(cwd, session_id)
        return p if p.exists() else None

    def expected_transcript_path(self, cwd: str, session_id: str) -> Path:
        return paths.claude_transcript_path(cwd, session_id)

    def mint_session_id(self) -> str:
        return str(uuidlib.uuid4())

    def _base_entry(self, ctx: SessionContext, entry_type: str) -> dict[str, Any]:
        version = "0.0.0"
        raw = self.detect_version()
        if raw:
            parsed = compat.parse_version(raw)
            if parsed:
                version = ".".join(str(x) for x in parsed)
        return {
            "parentUuid": ctx.claude_leaf_uuid,
            "isSidechain": False,
            "userType": "external",
            "cwd": ctx.cwd,
            "sessionId": ctx.claude_session_id,
            "version": version,
            "gitBranch": git_branch(ctx.cwd),
            "type": entry_type,
            "uuid": str(uuidlib.uuid4()),
            "timestamp": iso_now_ms(),
        }

    def create_shadow_transcript(
        self, cwd: str, session_id: str, ctx: SessionContext, note: str
    ) -> Path:
        path = paths.claude_transcript_path(cwd, session_id)
        entry = self._base_entry(ctx, "user")
        entry["message"] = {"role": "user", "content": note}
        append_jsonl_fsync(path, [entry])
        ctx.claude_leaf_uuid = entry["uuid"]
        return path

    # -- launching -----------------------------------------------------------

    def interactive_argv(self, session_id: str | None, fresh: bool) -> list[str]:
        assert session_id, "claude sessions are always minted up front"
        if fresh:
            return [self.binary, "--session-id", session_id]
        return [self.binary, "--resume", session_id]

    def oneoff_argv(self, session_id: str, prompt: str) -> list[str]:
        return [self.binary, "--resume", session_id, "-p", prompt]

    def hook_argv_extra(self, sentinel: Path) -> list[str]:
        # Per-invocation settings JSON; Stop fires at each completed turn.
        settings = {
            "hooks": {
                "Stop": [
                    {
                        "hooks": [
                            {"type": "command", "command": f"touch '{sentinel}'"}
                        ]
                    }
                ]
            }
        }
        return ["--settings", json.dumps(settings)]

    # -- parse / render (M2/M3) ----------------------------------------------

    def parse_entry(self, raw: dict[str, Any], ctx: SessionContext) -> list[NormalizedEvent]:
        raise NotImplementedError  # lands with the sync engine milestone

    def render_events(
        self, events: list[NormalizedEvent], ctx: SessionContext
    ) -> list[dict[str, Any]]:
        raise NotImplementedError

    def render_placeholder(self, text: str, ctx: SessionContext) -> list[dict[str, Any]]:
        raise NotImplementedError
