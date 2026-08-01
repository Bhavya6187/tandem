"""PreToolUse routing: should this native Agent dispatch run on codex?

Pure decision logic — the CLI wrapper owns process concerns (stdin, exit
codes). Returning None means 'emit nothing': the dispatch proceeds
natively. That is the failure mode for everything unexpected."""

from __future__ import annotations

from pathlib import Path

from .config import SubagentsConfig

BRIDGE_AGENT = "codex-worker"
BRIDGE_MODEL = "haiku"


def route(
    payload: dict,
    cfg: SubagentsConfig,
    cwd: str,
    claude_home: Path,
    *,
    has_session: bool,
    codex_ok: bool,
) -> dict | None:
    if cfg.route != "all" or not has_session or not codex_ok:
        return None
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    subagent_type = tool_input.get("subagent_type") or ""
    # forks keep claude's native full-context contract; bridge = loop guard
    if subagent_type in ("fork", BRIDGE_AGENT):
        return None
    prompt = tool_input.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return None
    body = find_agent_body(subagent_type, cwd, claude_home)
    if body:
        prompt = (
            "Instructions for this task (from the dispatching session's "
            f"{subagent_type!r} agent definition):\n\n{body}\n\n---\n\n"
            + prompt
        )
    updated = dict(tool_input)
    updated["subagent_type"] = BRIDGE_AGENT
    # per-invocation model overrides agent frontmatter; without this rewrite
    # the bridge would run on the dispatch's model (observed: opus)
    updated["model"] = BRIDGE_MODEL
    updated["prompt"] = prompt
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "permissionDecisionReason": "tandem: rerouted to codex-worker",
            "updatedInput": updated,
        }
    }


def find_agent_body(subagent_type: str, cwd: str, claude_home: Path) -> str:
    """The definition body a named claude agent would have received as its
    system prompt: every .claude/agents/ from cwd up to the filesystem
    root, then <claude_home>/agents/, searched recursively, first `name`
    match wins. Built-in and plugin-scoped types have no local file."""
    if not subagent_type or ":" in subagent_type:
        return ""
    bases: list[Path] = []
    d = Path(cwd)
    while True:
        bases.append(d / ".claude" / "agents")
        if d.parent == d:
            break
        d = d.parent
    bases.append(claude_home / "agents")
    for base in bases:
        if not base.is_dir():
            continue
        for f in sorted(base.rglob("*.md")):
            name, body = _parse_agent_file(f)
            if name == subagent_type:
                return body
    return ""


def _parse_agent_file(path: Path) -> tuple[str, str]:
    try:
        text = path.read_text()
    except OSError:
        return "", ""
    name, body = path.stem, text
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            for line in text[3:end].splitlines():
                if line.strip().startswith("name:"):
                    name = line.split(":", 1)[1].strip()
            body = text[end + 4:]
    return name, body.strip()
