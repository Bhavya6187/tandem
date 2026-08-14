"""Normalized internal event model.

This schema is derived from the entries actually written by claude 2.1.220
and codex-cli 0.145.0 (docs/formats.md), not from any published spec. It is
the hub of the sync pipeline: each harness adapter parses its native entries
into these events, and renders these events into its native entries.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator

Agent = str  # harness id, "tandem", or "user"


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
    direction: str  # "<source>-><target>" over registered adapter ids
    turn_index: int = 0
    # call_id -> serialized ToolCall event awaiting its result
    pending_calls: dict[str, dict[str, Any]] = Field(default_factory=dict)
    # per-adapter renderer scratch keyed by harness id (claude: leaf_uuid /
    # run_msg_id / model — see ClaudeCodeAdapter); persisted with the cursor
    harness_state: dict[str, dict[str, Any]] = Field(default_factory=dict)
    source_session_id: str | None = None
    target_session_id: str | None = None

    @field_validator("direction")
    @classmethod
    def _check_direction(cls, v: str) -> str:
        from .harness import ADAPTERS

        parts = v.split("->")
        if (len(parts) != 2 or parts[0] == parts[1]
                or any(p not in ADAPTERS for p in parts)):
            raise ValueError(f"not a harness direction: {v!r}")
        return v

    @property
    def source_id(self) -> str:
        return self.direction.split("->")[0]

    @property
    def target_id(self) -> str:
        return self.direction.split("->")[1]

    def state_for(self, harness_id: str) -> dict[str, Any]:
        return self.harness_state.setdefault(harness_id, {})
