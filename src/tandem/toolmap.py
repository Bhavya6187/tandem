"""Tool vocabulary mapping between harnesses.

Spec: docs/specs/2026-07-29-native-tool-call-translation-design.md.

Tier 1 re-expresses common tools in the target harness's own vocabulary so
shadow history reads as the shadow's own work; Tier 2 passes name and
arguments through verbatim (spike-validated safe on both sides, 2026-07).
Honesty rule: Tier 1 only when the native rendering truthfully describes
what happened; anything that doesn't fit degrades to Tier 2. map_pair never
raises on shape surprises.

Rendering conventions consumed by the adapters:
- codex target: ToolCall with dict arguments renders as function_call, str
  arguments as custom_tool_call input.
- claude target: ToolResult.structured becomes the toolUseResult sibling,
  ToolResult.is_error the tool_result is_error flag.
"""

from __future__ import annotations

import json
import re
import shlex

from .events import ToolCall, ToolResult

PLACEHOLDER_OUTPUT = "(tool result not recorded)"

_EXIT_RE = re.compile(r"^(?:Process exited|Exit code)[^\d-]*(-?\d+)", re.M)
_OUTPUT_RE = re.compile(r"^Output:\n", re.M)


def parse_exec_output(raw: str) -> tuple[str | None, str]:
    """codex exec_command output has a header (chunk id, wall time, exit
    code, token count) followed by 'Output:\\n<text>'."""
    exit_code = None
    m = _EXIT_RE.search(raw)
    if m:
        exit_code = m.group(1)
    m = _OUTPUT_RE.search(raw)
    return exit_code, raw[m.end():] if m else raw


def map_pair(
    call: ToolCall, result: ToolResult, target: str
) -> tuple[ToolCall, ToolResult]:
    """One completed source-native pair -> one target-native pair."""
    try:
        mapper = _TO_CODEX if target == "codex" else _TO_CLAUDE
        fn = mapper.get(call.tool)
        if fn is not None:
            mapped = fn(call, result)
            if mapped is not None:
                return mapped
    except Exception:
        pass  # honesty rule: surprises fall through to pass-through
    return _passthrough(call, result, target)


def _passthrough(
    call: ToolCall, result: ToolResult, target: str
) -> tuple[ToolCall, ToolResult]:
    if target == "claude":
        args = call.arguments
        if isinstance(args, str):
            try:
                parsed = json.loads(args)
                args = parsed if isinstance(parsed, dict) else {"input": args}
            except json.JSONDecodeError:
                args = {"input": args}
        call = call.model_copy(update={"arguments": args})
        if result.structured is None:
            result = result.model_copy(update={"structured": {"stdout": result.output}})
    return call, result


def _retool(call, result, tool, arguments, output=None, structured=None,
            is_error=None):
    new_call = call.model_copy(update={"tool": tool, "arguments": arguments})
    updates = {"tool": tool}
    if output is not None:
        updates["output"] = output
    if structured is not None:
        updates["structured"] = structured
    if is_error is not None:
        updates["is_error"] = is_error
    return new_call, result.model_copy(update=updates)


# -- claude -> codex ---------------------------------------------------------

def _bash_to_exec(call, result):
    args = call.arguments
    if not isinstance(args, dict):
        return None
    return _retool(call, result, "exec_command", {"cmd": str(args.get("command", ""))})


def _sh_quote(path: str) -> str:
    q = shlex.quote(path)
    return q


def _read_to_cat(call, result):
    args = call.arguments
    if not isinstance(args, dict) or not args.get("file_path"):
        return None
    cmd = f"cat -n {_sh_quote(args['file_path'])}"
    offset, limit = args.get("offset"), args.get("limit")
    if offset or limit:
        start = int(offset or 1)
        end = f"{start + int(limit) - 1}" if limit else "$"
        cmd += f" | sed -n '{start},{end}p'"
    return _retool(call, result, "exec_command", {"cmd": cmd})


def _patch(op: str, path: str, body: str) -> str:
    return f"*** Begin Patch\n*** {op} File: {path}\n{body}\n*** End Patch"


def _write_to_patch(call, result):
    args = call.arguments
    s = result.structured or {}
    if not isinstance(args, dict) or s.get("type") != "create":
        return None  # overwrite or unknown: honesty rule, Tier 2
    content = s.get("content") if isinstance(s.get("content"), str) else args.get("content", "")
    body = "\n".join("+" + line for line in content.splitlines())
    return _retool(call, result, "apply_patch",
                   _patch("Add", args.get("file_path", "?"), body))


def _edit_to_patch(call, result):
    args = call.arguments
    if not isinstance(args, dict) or args.get("replace_all"):
        return None
    hunks = (result.structured or {}).get("structuredPatch") or []
    if hunks:
        lines: list[str] = []
        for h in hunks:
            lines.append("@@")
            lines.extend(h.get("lines") or [])
        body = "\n".join(lines)
    else:
        old = [f"-{l}" for l in str(args.get("old_string", "")).splitlines()]
        new = [f"+{l}" for l in str(args.get("new_string", "")).splitlines()]
        body = "\n".join(old + new)
    return _retool(call, result, "apply_patch",
                   _patch("Update", args.get("file_path", "?"), body))


def _grep_to_rg(call, result):
    args = call.arguments
    if not isinstance(args, dict) or not args.get("pattern"):
        return None
    flag = "-l" if args.get("output_mode") == "files_with_matches" else "-n"
    cmd = f"rg {flag}"
    if args.get("-i"):
        cmd += " -i"
    if args.get("glob"):
        cmd += f" -g {shlex.quote(args['glob'])}"
    cmd += f" {shlex.quote(args['pattern'])}"
    if args.get("path"):
        cmd += f" {_sh_quote(args['path'])}"
    return _retool(call, result, "exec_command", {"cmd": cmd})


def _glob_to_rg(call, result):
    args = call.arguments
    if not isinstance(args, dict) or not args.get("pattern"):
        return None
    cmd = f"rg --files -g {shlex.quote(args['pattern'])}"
    if args.get("path"):
        cmd += f" {_sh_quote(args['path'])}"
    return _retool(call, result, "exec_command", {"cmd": cmd})


def _todos_to_plan(call, result):
    args = call.arguments
    if not isinstance(args, dict) or not isinstance(args.get("todos"), list):
        return None
    plan = [{"step": t.get("content", ""), "status": t.get("status", "pending")}
            for t in args["todos"] if isinstance(t, dict)]
    return _retool(call, result, "update_plan", {"plan": plan})


# -- codex -> claude ---------------------------------------------------------

def _exec_to_bash(call, result):
    args = call.arguments
    if isinstance(args, str):
        args = json.loads(args)
    if not isinstance(args, dict):
        return None
    exit_code, body = parse_exec_output(result.output)
    structured: dict = {"stdout": body}
    is_error = False
    if exit_code is not None:
        structured["exitCode"] = int(exit_code)
        is_error = exit_code != "0"
    return _retool(call, result, "Bash", {"command": str(args.get("cmd", ""))},
                   output=body, structured=structured, is_error=is_error)


_TO_CODEX = {
    "Bash": _bash_to_exec,
    "Read": _read_to_cat,
    "Write": _write_to_patch,
    "Edit": _edit_to_patch,
    "Grep": _grep_to_rg,
    "Glob": _glob_to_rg,
    "TodoWrite": _todos_to_plan,
}

_TO_CLAUDE = {
    "exec_command": _exec_to_bash,
}
