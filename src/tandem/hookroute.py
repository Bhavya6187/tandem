"""PreToolUse routing: should this native Agent dispatch run on codex?

Pure decision logic — the CLI wrapper owns process concerns (stdin, exit
codes, the once-per-session stamp file). Returning None means 'emit
nothing': the dispatch proceeds natively. That is the failure mode for
everything unexpected. The second decision here — missed_reroute_notice —
is what keeps two of those silences explainable instead of merely quiet."""

from __future__ import annotations

from pathlib import Path

from .config import SubagentsConfig

# Claude registers a plugin's agents under `<plugin-name>:<agent-name>`, and
# the bare name does NOT resolve. Live E2E on claude 2.1.220 (2026-07-31):
# rewriting to "codex-worker" made every dispatch fail with
#   Agent type 'codex-worker' not found. Available agents: …, tandem:codex-worker
# so the rewrite must carry the plugin scope.
BRIDGE_NAME = "codex-worker"
BRIDGE_AGENT = f"tandem:{BRIDGE_NAME}"
BRIDGE_MODEL = "haiku"

# The plugin is installed globally in claude, so its silence is ambiguous:
# "no tandem session here" and "tandem is broken" look identical from the
# UI. These two lines are the only thing that distinguishes them, and each
# names the one command that fixes its own cause.
NOTICE_NO_SESSION = (
    "tandem: subagent plugin is active but this directory has no paired "
    "tandem session — dispatches stay on claude. Run `tandem` here to "
    "enable codex subagents."
)
NOTICE_CODEX = (
    "tandem: subagent plugin is active but codex is missing or its version "
    "is unsupported — dispatches stay on claude. Run `tandem doctor` to see "
    "what is wrong."
)


def route(
    payload: dict,
    cfg: SubagentsConfig,
    cwd: str,
    claude_home: Path,
    *,
    has_session: bool,
    codex_ok: bool,
) -> dict | None:
    # defense in depth: the plugin's matcher is `Agent|Task`, but a matcher is
    # config we do not control at call time — never rewrite another tool's input
    if payload.get("tool_name") not in ("Agent", "Task"):
        return None
    if cfg.route != "all" or not has_session or not codex_ok:
        return None
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    subagent_type = tool_input.get("subagent_type") or ""
    # forks keep claude's native full-context contract; bridge = loop guard.
    # The guard is scope-insensitive: the model can (and in live traces does)
    # ask for the bridge by either the plugin-scoped id or the bare name, and
    # rewriting either one again would re-enter this hook forever.
    if subagent_type == "fork" or subagent_type.rsplit(":", 1)[-1] == BRIDGE_NAME:
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


def missed_reroute_notice(
    payload: dict,
    cfg: SubagentsConfig,
    *,
    has_session: bool,
    codex_ok: bool,
    already_warned: bool,
) -> dict | None:
    """The 'plugin fired, nothing was rerouted' notice, or None for silence.

    A bare top-level `systemMessage` — no permission decision, so the
    dispatch still falls through claude's normal permission flow and runs
    natively. Mutually exclusive with route()'s rewrite by construction: a
    rewrite requires has_session and codex_ok, and those two silence this.
    `route = "off"` is an explicit user choice, so it stays silent even
    though nothing reroutes. Whether this session was warned already is the
    caller's business (it owns the stamp file), as is printing the result."""
    if payload.get("tool_name") not in ("Agent", "Task"):
        return None
    if cfg.route != "all" or already_warned:
        return None
    if has_session and codex_ok:
        return None
    # deliberately not narrowed to reroutable dispatches: with no session or
    # no codex, a fork/bridge/malformed dispatch is just as unrerouted, and
    # the notice explains the environment rather than the call
    return {"systemMessage": NOTICE_NO_SESSION if not has_session
            else NOTICE_CODEX}


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
