"""The plugin is static registration only — validate its manifests (plugin,
marketplace, hooks) and its two agent definitions."""

import json
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).parent.parent
PLUGIN = ROOT / "plugin"
MARKETPLACE = ROOT / ".claude-plugin" / "marketplace.json"


def test_manifest_parses():
    m = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text())
    assert m["name"] == "tandem"
    assert m["version"]


def test_marketplace_manifest_declares_the_tandem_marketplace():
    """The top-level `name` is the `@<marketplace>` half of
    `claude plugin install tandem@tandem` — it is NOT derived from the
    GitHub owner/repo the user passes to `marketplace add`, so it has to be
    exactly "tandem" for the documented install command to work."""
    m = json.loads(MARKETPLACE.read_text())
    assert m["name"] == "tandem"
    assert m["owner"] == {
        "name": "Bhavya Agarwal",
        "url": "https://github.com/Bhavya6187",
    }
    assert len(m["plugins"]) == 1


def test_marketplace_entry_source_resolves_to_the_plugin_directory():
    """A relative source is resolved against the marketplace root (the dir
    holding `.claude-plugin/`), i.e. the repo root."""
    entry = json.loads(MARKETPLACE.read_text())["plugins"][0]
    assert entry["source"] == "./plugin"
    assert (ROOT / entry["source"]).resolve() == PLUGIN.resolve()
    assert (PLUGIN / ".claude-plugin" / "plugin.json").is_file()


def test_marketplace_entry_name_matches_the_plugin_manifest():
    """Claude installs the plugin under the entry's `name`, and every
    plugin-scoped reference (`tandem:codex-worker`, hook rewrite target)
    is built from it — the two manifests must not drift apart."""
    entry = json.loads(MARKETPLACE.read_text())["plugins"][0]
    plugin = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text())
    assert entry["name"] == plugin["name"]
    assert entry["description"] == plugin["description"]


def test_marketplace_entry_leaves_version_and_strict_to_plugin_json():
    """Version resolution is plugin.json → entry → git SHA, so an entry
    `version` is silently shadowed by plugin.json and only invites drift.
    `strict` defaults to true (plugin.json authoritative); `strict: false`
    against a component-declaring plugin.json is a documented load failure,
    so it stays unset too."""
    entry = json.loads(MARKETPLACE.read_text())["plugins"][0]
    assert set(entry) == {"name", "source", "description"}


def test_plugin_version_matches_pyproject_version():
    """Hand-maintained twin of pyproject's version. It gates update
    delivery: `plugin marketplace update` skips a plugin whose resolved
    version already matches the installed one, so a stale plugin.json
    version means shipped fixes never reach users."""
    plugin = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text())
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert plugin["version"] == pyproject["project"]["version"]


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
    # live transcripts (2026-08-08 audit): with no explicit rule, the haiku
    # relay dropped a `tandem-model:` first line from the heredoc in 5 of 6
    # dispatches — it reads the header as envelope metadata already consumed,
    # not as task content. The explicit first-line rule is the fix.
    assert re.search(r"tandem-model.*\n.*part of the task message", body)
    assert re.search(r"FIRST line inside\s+the heredoc", body)
    assert re.search(r"unless .*appears|appears .*in the task", body)
    assert "[tandem-sub blocked: write]" in body
    assert "--sandbox workspace-write" in body
    assert re.search(r"[Nn]ever add that flag", body)


def test_gpt_alias_agent_definition():
    text = (PLUGIN / "agents" / "gpt.md").read_text()
    front = text.split("---")[1]
    assert re.search(r"^name:\s*gpt\s*$", front, re.M)
    assert re.search(r"^model:\s*haiku\s*$", front, re.M)
    assert re.search(r"^tools:\s*Bash\(tandem sub:\*\)\s*$", front, re.M)
    # user-facing: the description must invite selection (unlike
    # codex-worker's "not meant for manual selection")
    desc = re.search(r"^description:\s*(.+)$", front, re.M).group(1)
    assert "not meant for manual selection" not in desc
    assert re.search(r"GPT", desc)
    # same relay contract as codex-worker
    body = text.split("---", 2)[2]
    for marker in ("tandem sub -q", "TANDEM_TASK_EOF_", "verbatim",
                   "tandem-model", "[tandem-sub failed]",
                   "[tandem-sub blocked: write]",
                   "--sandbox workspace-write"):
        assert marker in body, marker
    assert re.search(r"never do the task yourself", body, re.I)
    # The alias differs ONLY in frontmatter: it is the same relay, offered for
    # manual selection. Byte equality is the real pin — the markers above only
    # sample the contract, so an edit to one body that the other misses (a
    # reworded rule, a new guard) would otherwise slip through as two agents
    # that behave differently under the same name.
    assert body == (PLUGIN / "agents" / "codex-worker.md").read_text(
    ).split("---", 2)[2]


def test_loop_guard_covers_both_relay_agents():
    """Every agent under plugin/agents/ must be in hookroute's guard set:
    a plugin agent missing from RELAY_NAMES gets rewritten away from
    itself on dispatch (or worse, loops)."""
    from tandem.hookroute import RELAY_NAMES
    names = set()
    for f in (PLUGIN / "agents").glob("*.md"):
        front = f.read_text().split("---")[1]
        names.add(re.search(r"^name:\s*(\S+)\s*$", front, re.M).group(1))
    assert names == set(RELAY_NAMES)
