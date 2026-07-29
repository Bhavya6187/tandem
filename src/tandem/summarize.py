"""Tool-call → plain-text action summary policy.

Tool calls are never replayed into the shadow as real tool calls (the two
harnesses have different tool vocabularies). Each completed call is rendered
as a compact plain-text summary the shadow model can read as context:

    ran `pytest` -> exit 1; output: ... (head/tail sampled)
    edited src/auth.py: <unified diff, truncated>
    created hello.txt (1 line)

Full diffs are preserved for edits when under DIFF_MAX_LINES; command output
is head/tail sampled.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .events import ToolCall, ToolResult

DIFF_MAX_LINES = 80
OUTPUT_HEAD_LINES = 25
OUTPUT_TAIL_LINES = 10
OUTPUT_MAX_CHARS = 4000
ARGS_MAX_CHARS = 160


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


def _clip_diff(diff: str) -> str:
    lines = diff.rstrip("\n").splitlines()
    if len(lines) > DIFF_MAX_LINES:
        lines = lines[:DIFF_MAX_LINES] + [f"… (diff truncated at {DIFF_MAX_LINES} lines)"]
    return "\n".join(lines)


def _compact_args(arguments: Any) -> str:
    if isinstance(arguments, str):
        text = arguments
    else:
        text = json.dumps(arguments, ensure_ascii=False)
    text = re.sub(r"\s+", " ", text)
    return text[:ARGS_MAX_CHARS] + ("…" if len(text) > ARGS_MAX_CHARS else "")


def summarize_pair(call: ToolCall, result: ToolResult) -> str:
    """One completed tool call -> one plain-text action summary."""
    if call.source == "claude":
        return _summarize_claude(call, result)
    return _summarize_codex(call, result)


def summarize_orphan_result(result: ToolResult) -> str:
    """Result whose call was never seen (e.g. restart lost the pairing)."""
    status = "error" if result.is_error else "ok"
    return f"tool result ({status}): {_clip_lines(result.output)}"


# -- Claude Code tools -------------------------------------------------------

def _summarize_claude(call: ToolCall, result: ToolResult) -> str:
    args = call.arguments if isinstance(call.arguments, dict) else {}
    s = result.structured or {}
    tool = call.tool

    if tool == "Bash":
        cmd = args.get("command", "")
        if result.is_error or s.get("interrupted"):
            status = "interrupted" if s.get("interrupted") else "failed"
        else:
            status = "ok"
        body = s.get("stdout") if isinstance(s.get("stdout"), str) else result.output
        err = s.get("stderr") or ""
        text = f"ran `{cmd}` -> {status}"
        if body:
            text += f"\noutput:\n{_clip_lines(body)}"
        if err.strip():
            text += f"\nstderr:\n{_clip_lines(err)}"
        return text

    if tool == "Write":
        path = args.get("file_path", "?")
        content = s.get("content") if isinstance(s.get("content"), str) else args.get("content", "")
        n = content.count("\n") + 1 if content else 0
        diff = "\n".join("+" + l for l in content.splitlines())
        return f"created {path} ({n} lines):\n{_clip_diff(diff)}"

    if tool in ("Edit", "MultiEdit", "NotebookEdit"):
        path = args.get("file_path", "?")
        hunks = s.get("structuredPatch") or []
        parts = []
        for h in hunks:
            parts.append(
                f"@@ -{h.get('oldStart')},{h.get('oldLines')} "
                f"+{h.get('newStart')},{h.get('newLines')} @@"
            )
            parts.extend(h.get("lines") or [])
        if not parts:
            parts = [f"-{_compact_args(args.get('old_string', ''))}",
                     f"+{_compact_args(args.get('new_string', ''))}"]
        body = _clip_diff("\n".join(parts))
        return f"edited {path}:\n{body}"

    if tool == "Read":
        return f"read {args.get('file_path', '?')}"

    if tool in ("Glob", "Grep"):
        return (
            f"searched {tool.lower()}({_compact_args(args)}) -> "
            f"{_clip_lines(result.output, head=10, tail=0)}"
        )

    if tool == "TodoWrite":
        return "updated todo list"

    if tool in ("Task", "Agent"):
        desc = args.get("description") or args.get("prompt", "")[:80]
        return f"ran subagent ({desc}) -> {_clip_lines(result.output, head=15, tail=5)}"

    status = "error" if result.is_error else "ok"
    return (
        f"used {tool}({_compact_args(args)}) -> {status}: "
        f"{_clip_lines(result.output, head=10, tail=3)}"
    )


# -- Codex tools -------------------------------------------------------------

_CODEX_EXIT_RE = re.compile(r"^(?:Process exited|Exit code)[^\d-]*(-?\d+)", re.M)
_CODEX_OUTPUT_RE = re.compile(r"^Output:\n", re.M)


def _codex_exec_output(raw: str) -> tuple[str | None, str]:
    """codex exec_command output has a header (chunk id, wall time, exit
    code, token count) followed by 'Output:\\n<text>'."""
    exit_code = None
    m = _CODEX_EXIT_RE.search(raw)
    if m:
        exit_code = m.group(1)
    m = _CODEX_OUTPUT_RE.search(raw)
    body = raw[m.end():] if m else raw
    return exit_code, body


def _summarize_codex(call: ToolCall, result: ToolResult) -> str:
    tool = call.tool

    if tool == "exec_command":
        cmd = ""
        if isinstance(call.arguments, str):
            try:
                cmd = json.loads(call.arguments).get("cmd", "")
            except (json.JSONDecodeError, AttributeError):
                cmd = call.arguments
        elif isinstance(call.arguments, dict):
            cmd = call.arguments.get("cmd", "")
        exit_code, body = _codex_exec_output(result.output)
        status = f"exit {exit_code}" if exit_code is not None else "done"
        text = f"ran `{cmd}` -> {status}"
        if body.strip():
            text += f"\noutput:\n{_clip_lines(body)}"
        return text

    if tool == "apply_patch":
        patch = call.arguments if isinstance(call.arguments, str) else json.dumps(call.arguments)
        s = result.structured or {}
        status = "applied" if s.get("success", True) else "FAILED"
        changed = ""
        changes = s.get("changes") or {}
        if changes:
            kinds = ", ".join(
                f"{v.get('type', 'update')} {k.rsplit('/', 1)[-1]}" for k, v in changes.items()
            )
            changed = f" ({kinds})"
        return f"{status} patch{changed}:\n{_clip_diff(patch)}"

    status = "error" if result.is_error else "ok"
    return (
        f"used {tool}({_compact_args(call.arguments)}) -> {status}: "
        f"{_clip_lines(result.output, head=10, tail=3)}"
    )
