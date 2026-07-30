"""TraceConverter adapter interface + the reference implementation.

The interface mirrors the spec so an external bidirectional converter can be
swapped in without touching the sync engine:

    translate_entry(entry, direction, ctx) -> list[target entries] | TranslationError

The reference implementation goes through the normalized event model:
source adapter parses the native entry, this module applies the sync policy
(attribution tagging, tool-call pairing into native target-vocabulary pairs
via `toolmap`, dropping non-portable content), and the target adapter renders
native entries.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable

from . import toolmap
from .constants import ATTRIBUTION
from .events import (
    AssistantMessage,
    NormalizedEvent,
    SessionContext,
    ToolCall,
    ToolResult,
    UserMessage,
)
from .harness import get_adapter, other
from .summarize import summarize_orphan_result

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
    assistant text, tool calls and system/compaction events (skip or one-line
    note).

    Tool activity emits at the result: the call is stashed on arrival, and
    when its result lands the pair is mapped into the target harness's own
    tool vocabulary (`toolmap.map_pair`) and emitted as two adjacent events,
    so the shadow reads as the shadow's own work. A result with no stashed
    call falls back to a prose note — never a lone native tool_result, which
    both replay APIs reject."""

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
                stored = ctx.pending_calls.pop(ev.call_id, None) if ev.call_id else None
                if stored:
                    # _structured is the codex adapter's out-of-band enrichment
                    # channel, not a ToolCall field (which forbids extras)
                    stored.pop("_structured", None)
                    call = ToolCall.model_validate(stored)
                    out.extend(toolmap.map_pair(call, ev, other(source_id)))
                else:
                    # no stashed call to pair with, so a native tool_result
                    # would dangle and break resume: prose instead
                    out.append(
                        AssistantMessage(
                            source=ev.source,
                            timestamp=ev.timestamp,
                            turn_index=ev.turn_index,
                            text=f"{tag} {summarize_orphan_result(ev)}",
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

    def flush_dangling(self, ctx: SessionContext) -> list[NormalizedEvent]:
        """Mapped call + placeholder-result pairs for every pending call.
        Both replay APIs reject a call without a result, so a drained source
        must never leave one behind. Clears ctx.pending_calls."""
        target_id = ctx.direction.split("->")[1]
        out: list[NormalizedEvent] = []
        for call_id, stored in list(ctx.pending_calls.items()):
            stored.pop("_structured", None)
            call = ToolCall.model_validate(stored)
            placeholder = ToolResult(
                source=call.source, turn_index=call.turn_index,
                call_id=call_id, output=toolmap.PLACEHOLDER_OUTPUT,
            )
            out.extend(toolmap.map_pair(call, placeholder, target_id))
        ctx.pending_calls.clear()
        return out
