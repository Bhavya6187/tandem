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
from ..events import (
    AssistantMessage,
    NormalizedEvent,
    SessionContext,
    SystemEvent,
    Thinking,
    ToolCall,
    ToolResult,
    UserMessage,
)
from ..util import append_jsonl_fsync, git_branch, iso_now_ms
from .base import HarnessAdapter


def _stringify_block_content(content: Any) -> str:
    """tool_result content is either a string or a list of content blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for c in content:
            if isinstance(c, dict) and c.get("type") == "text":
                parts.append(c.get("text", ""))
            elif isinstance(c, dict):
                parts.append(f"<{c.get('type', 'block')}>")
        return "\n".join(parts)
    return "" if content is None else str(content)


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

    # -- parsing -------------------------------------------------------------

    def parse_entry(self, raw: dict[str, Any], ctx: SessionContext) -> list[NormalizedEvent]:
        """One transcript line -> normalized events. Never raises on shape
        surprises; anything unrecognized degrades to a SystemEvent."""
        ts = raw.get("timestamp")
        etype = raw.get("type")

        def sysev(subtype: str, text: str = "") -> SystemEvent:
            return SystemEvent(
                source="claude", timestamp=ts, turn_index=ctx.turn_index,
                subtype=subtype, text=text,
            )

        if raw.get("isSidechain"):
            return [sysev("sidechain")]

        if etype == "user":
            if raw.get("isMeta"):
                return [sysev("meta_user")]
            content = (raw.get("message") or {}).get("content")
            if isinstance(content, str):
                ctx.turn_index += 1
                return [
                    UserMessage(
                        source="user", timestamp=ts,
                        turn_index=ctx.turn_index, text=content,
                    )
                ]
            events: list[NormalizedEvent] = []
            structured = raw.get("toolUseResult")
            for block in content or []:
                btype = block.get("type")
                if btype == "tool_result":
                    events.append(
                        ToolResult(
                            source="claude", timestamp=ts,
                            turn_index=ctx.turn_index,
                            call_id=block.get("tool_use_id"),
                            output=_stringify_block_content(block.get("content")),
                            is_error=bool(block.get("is_error")),
                            structured=structured if isinstance(structured, dict) else None,
                        )
                    )
                elif btype == "text":
                    ctx.turn_index += 1
                    events.append(
                        UserMessage(
                            source="user", timestamp=ts,
                            turn_index=ctx.turn_index,
                            text=block.get("text", ""),
                        )
                    )
                else:
                    events.append(sysev(f"user_block:{btype}"))
            return events or [sysev("user_empty")]

        if etype == "assistant":
            msg = raw.get("message") or {}
            events = []
            for block in msg.get("content") or []:
                btype = block.get("type")
                if btype == "text":
                    events.append(
                        AssistantMessage(
                            source="claude", timestamp=ts,
                            turn_index=ctx.turn_index,
                            text=block.get("text", ""),
                            model=msg.get("model"),
                        )
                    )
                elif btype == "tool_use":
                    events.append(
                        ToolCall(
                            source="claude", timestamp=ts,
                            turn_index=ctx.turn_index,
                            call_id=block.get("id", ""),
                            tool=block.get("name", ""),
                            arguments=block.get("input") or {},
                        )
                    )
                elif btype in ("thinking", "redacted_thinking"):
                    events.append(
                        Thinking(source="claude", timestamp=ts, turn_index=ctx.turn_index)
                    )
                else:
                    events.append(sysev(f"assistant_block:{btype}"))
            return events or [sysev("assistant_empty")]

        if etype == "system" and raw.get("subtype") == "compact_boundary":
            return [sysev("compaction", "conversation was compacted")]

        # attachment, queue-operation, last-prompt, summary, system, unknown
        return [sysev(f"claude:{etype}")]

    def render_events(
        self, events: list[NormalizedEvent], ctx: SessionContext
    ) -> list[dict[str, Any]]:
        raise NotImplementedError

    def render_placeholder(self, text: str, ctx: SessionContext) -> list[dict[str, Any]]:
        raise NotImplementedError
