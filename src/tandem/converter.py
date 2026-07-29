"""TraceConverter adapter interface + the reference implementation.

The interface mirrors the spec so an external bidirectional converter can be
swapped in without touching the sync engine:

    translate_entry(entry, direction, ctx) -> list[target entries] | TranslationError

The reference implementation goes through the normalized event model:
source adapter parses the native entry, this module applies the sync policy
(attribution tagging, tool-call pairing into action summaries, dropping
non-portable content), and the target adapter renders native entries.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable

from .constants import ATTRIBUTION
from .events import (
    AssistantMessage,
    NormalizedEvent,
    SessionContext,
    ToolCall,
    ToolResult,
    UserMessage,
)
from .harness import get_adapter
from .summarize import summarize_orphan_result, summarize_pair

Direction = Literal["claude->codex", "codex->claude"]


@dataclass
class TranslationError:
    reason: str
    entry_summary: str = ""


@runtime_checkable
class TraceConverter(Protocol):
    def translate_entry(
        self, entry: dict[str, Any], direction: Direction, ctx: SessionContext
    ) -> list[dict[str, Any]] | TranslationError: ...


class ReferenceConverter:
    """parse -> normalize -> policy -> render. Handles user messages,
    assistant text, tool calls (as action summaries), and system/compaction
    events (skip or one-line note)."""

    def translate_entry(
        self, entry: dict[str, Any], direction: Direction, ctx: SessionContext
    ) -> list[dict[str, Any]] | TranslationError:
        source_id, target_id = direction.split("->")
        source = get_adapter(source_id)
        target = get_adapter(target_id)
        try:
            events = source.parse_entry(entry, ctx)
            out_events = self._apply_policy(events, source_id, ctx)
            if not out_events:
                return []
            return target.render_events(out_events, ctx)
        except Exception as exc:  # localize any surprise to this one entry
            return TranslationError(
                reason=f"{type(exc).__name__}: {exc}",
                entry_summary=str(entry.get("type", "?")),
            )

    def _apply_policy(
        self, events: list[NormalizedEvent], source_id: str, ctx: SessionContext
    ) -> list[NormalizedEvent]:
        tag = ATTRIBUTION[source_id]
        out: list[NormalizedEvent] = []
        for ev in events:
            if isinstance(ev, UserMessage):
                out.append(ev.model_copy(update={"text": f"{tag} {ev.text}".strip()}))
            elif isinstance(ev, AssistantMessage):
                if ev.text.strip():
                    out.append(ev.model_copy(update={"text": f"{tag} {ev.text}"}))
            elif isinstance(ev, ToolCall):
                ctx.pending_calls[ev.call_id] = ev.model_dump(exclude_none=True)
            elif isinstance(ev, ToolResult):
                stored = ctx.pending_calls.pop(ev.call_id or "", None)
                if stored:
                    stored.pop("_structured", None)
                    call = ToolCall.model_validate(stored)
                    summary = summarize_pair(call, ev)
                else:
                    summary = summarize_orphan_result(ev)
                out.append(
                    AssistantMessage(
                        source=ev.source,
                        timestamp=ev.timestamp,
                        turn_index=ev.turn_index,
                        text=f"{tag} {summary}",
                        phase="commentary",
                    )
                )
            elif ev.kind == "system" and ev.subtype == "compaction":
                out.append(
                    AssistantMessage(
                        source="tandem",
                        turn_index=ev.turn_index,
                        text=f"{ATTRIBUTION['tandem']} (the {source_id} side compacted "
                        "its conversation history here)",
                        phase="commentary",
                    )
                )
            # thinking and other system events: dropped
        return out
