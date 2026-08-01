"""The plugin is static registration only — validate the three files."""

import json
import re
from pathlib import Path

PLUGIN = Path(__file__).parent.parent / "plugin"


def test_manifest_parses():
    m = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text())
    assert m["name"] == "tandem"
    assert m["version"]


def test_hooks_register_hook_route():
    h = json.loads((PLUGIN / "hooks" / "hooks.json").read_text())
    entries = h["hooks"]["PreToolUse"]
    assert entries[0]["matcher"] == "Agent|Task"
    cmds = [hk["command"] for hk in entries[0]["hooks"]]
    # `|| true` is load-bearing: click exits 2 on a usage error (older
    # tandem on PATH without the subcommand), and exit 2 blocks dispatches
    assert cmds == ["tandem hook-route || true"]


def test_hook_rewrite_target_matches_the_plugin_scoped_agent_id():
    """Claude resolves a plugin's agents as `<plugin-name>:<agent-name>` and
    rejects the bare name (live, 2.1.220: "Agent type 'codex-worker' not
    found. Available agents: …, tandem:codex-worker"). If the manifest name
    or the agent's `name:` ever moves, the hook's rewrite target must move
    with it — otherwise every rerouted dispatch fails."""
    from tandem.hookroute import BRIDGE_AGENT

    plugin_name = json.loads(
        (PLUGIN / ".claude-plugin" / "plugin.json").read_text())["name"]
    front = (PLUGIN / "agents" / "codex-worker.md").read_text().split("---")[1]
    agent_name = re.search(r"^name:\s*(\S+)\s*$", front, re.M).group(1)
    assert BRIDGE_AGENT == f"{plugin_name}:{agent_name}"


def test_bridge_agent_definition():
    text = (PLUGIN / "agents" / "codex-worker.md").read_text()
    front = text.split("---")[1]
    assert re.search(r"^name:\s*codex-worker\s*$", front, re.M)
    assert re.search(r"^model:\s*haiku\s*$", front, re.M)
    assert re.search(r"^tools:\s*Bash\(tandem sub:\*\)\s*$", front, re.M)
    body = text.split("---", 2)[2]
    assert "tandem sub" in body
    assert "verbatim" in body
    assert "[tandem-sub failed]" in body
    # live E2E (haiku 4.5, 2026-07-31): given a one-command task and a Bash
    # tool, the relay answered it itself with `find` instead of delegating.
    # The unconditional "never do the task yourself" rule is the fix.
    assert re.search(r"never do the task yourself", body, re.I)
    # quiet mode: the command's whole output IS the final message, so the
    # relay has nothing to extract (inherited stdio would hand it codex's
    # entire exec log instead)
    assert "tandem sub -q" in body
    # a fixed heredoc delimiter is a shell-injection hazard: a task message
    # containing that line truncates the brief and runs the rest as shell
    assert "TANDEM_TASK_EOF_" in body
    assert re.search(r"unless .*appears|appears .*in the task", body)
