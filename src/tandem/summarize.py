"""Orphan tool-result fallback.

Completed tool calls are translated natively (see toolmap.py / the
converter policy). The only tool activity still rendered as prose is a
result whose call was never seen (e.g. a restart lost the pairing): a bare
native tool_result without its call would break Claude resume, so it
becomes a one-line commentary instead.
"""

from __future__ import annotations

from .events import ToolResult

OUTPUT_HEAD_LINES = 25
OUTPUT_TAIL_LINES = 10
OUTPUT_MAX_CHARS = 4000


def _clip_lines(text: str, head: int = OUTPUT_HEAD_LINES, tail: int = OUTPUT_TAIL_LINES) -> str:
    text = text.rstrip("\n")
    if len(text) > OUTPUT_MAX_CHARS * 2:
        text = text[: OUTPUT_MAX_CHARS] + "\n…\n" + text[-OUTPUT_MAX_CHARS // 2 :]
    lines = text.splitlines()
    if len(lines) <= head + tail + 2:
        out = text
    else:
        omitted = len(lines) - head - tail
        out = "\n".join(lines[:head] + [f"… (+{omitted} lines omitted) …"] + lines[-tail:])
    if len(out) > OUTPUT_MAX_CHARS:
        out = out[:OUTPUT_MAX_CHARS] + "…"
    return out


def summarize_orphan_result(result: ToolResult) -> str:
    """Result whose call was never seen (e.g. restart lost the pairing)."""
    status = "error" if result.is_error else "ok"
    return f"tool result ({status}): {_clip_lines(result.output)}"
