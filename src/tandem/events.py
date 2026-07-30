"""Normalized internal event model.

This schema is derived from the entries actually written by claude 2.1.220
and codex-cli 0.145.0 (docs/formats.md), not from any published spec. It is
the hub of the sync pipeline: each harness adapter parses its native entries
into these events, and renders these events into its native entries.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field

Agent = Literal["claude", "codex", "tandem", "user"]


class _EventBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Agent
    timestamp: str | None = None
    turn_index: int = 0
    raw_line_index: int | None = None


class UserMessage(_EventBase):
    kind: Literal["user_message"] = "user_message"
    text: str


class AssistantMessage(_EventBase):
    kind: Literal["assistant_message"] = "assistant_message"
    text: str
    phase: Literal["final", "commentary"] = "final"
    model: str | None = None


class ToolCall(_EventBase):
    """A tool invocation by the active model. Paired with its ToolResult and
    re-expressed in the shadow harness's own tool vocabulary, then rendered
    as a native call/result pair (see converter policy and toolmap)."""

    kind: Literal["tool_call"] = "tool_call"
    call_id: str
    tool: str
    arguments: dict[str, Any] | str


class ToolResult(_EventBase):
    kind: Literal["tool_result"] = "tool_result"
    call_id: str | None = None
    tool: str | None = None
    output: str = ""
    is_error: bool = False
    # Harness-specific structured result when available (e.g. Claude's
    # toolUseResult with structuredPatch, or Codex patch_apply_end changes).
    structured: dict[str, Any] | None = None


class Thinking(_EventBase):
    """Reasoning blocks. Claude's are signature-bound, Codex's encrypted;
    neither is portable, so converters drop these."""

    kind: Literal["thinking"] = "thinking"


class SystemEvent(_EventBase):
    """Session meta, hooks, attachments, compaction markers, token counts —
    anything that is not conversation content. Converters skip these (or
    summarize compaction)."""

    kind: Literal["system"] = "system"
    subtype: str = ""
    text: str = ""


NormalizedEvent = Annotated[
    Union[UserMessage, AssistantMessage, ToolCall, ToolResult, Thinking, SystemEvent],
    Field(discriminator="kind"),
]


class SessionContext(BaseModel):
    """Mutable per-direction translation context, persisted in the sync
    cursor so a restart resumes mid-turn without losing tool-call pairings."""

    model_config = ConfigDict(extra="forbid")

    tandem_id: str
    cwd: str
    direction: Literal["claude->codex", "codex->claude"]
    turn_index: int = 0
    # call_id -> serialized ToolCall event awaiting its result
    pending_calls: dict[str, dict[str, Any]] = Field(default_factory=dict)
    # uuid of the last entry in the Claude shadow file (parentUuid chain)
    claude_leaf_uuid: str | None = None
    # message.id shared by the current contiguous run of rendered assistant
    # entries; any rendered user-side entry resets it (a real transcript has
    # one id per API response, and regrouping by id must not strand a
    # tool_use away from its tool_result)
    claude_run_msg_id: str | None = None
    claude_session_id: str | None = None
    codex_session_id: str | None = None
    # model claude itself last used; rendered assistant entries carry it so
    # `claude --resume` can restore the session model (it rejects "<synced>")
    claude_model: str | None = None
