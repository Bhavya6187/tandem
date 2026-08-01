"""hook-route: reroute native Agent dispatches to the codex-worker bridge.
Failure discipline: any problem -> None (native dispatch), CLI always exit 0."""

import json
from pathlib import Path

from tandem.config import SubagentsConfig
from tandem.hookroute import BRIDGE_AGENT, BRIDGE_MODEL, find_agent_body, route


def _payload(subagent_type="Explore", prompt="find the tests", **extra):
    ti = {"subagent_type": subagent_type, "prompt": prompt,
          "description": "short label", "model": "opus"}
    ti.update(extra)
    return {"hook_event_name": "PreToolUse", "tool_name": "Agent",
            "cwd": "/tmp/x", "tool_input": ti}


CFG = SubagentsConfig()


def _route(payload, cfg=CFG, has_session=True, codex_ok=True,
           cwd="/tmp/x", claude_home=Path("/nonexistent")):
    return route(payload, cfg, cwd, claude_home,
                 has_session=has_session, codex_ok=codex_ok)


class TestRewrite:
    def test_reroutes_and_rewrites_exactly_three_fields(self):
        out = _route(_payload(run_in_background=True))
        hso = out["hookSpecificOutput"]
        assert hso["hookEventName"] == "PreToolUse"
        assert hso["permissionDecision"] == "allow"
        ui = hso["updatedInput"]
        assert ui["subagent_type"] == BRIDGE_AGENT
        assert ui["model"] == BRIDGE_MODEL          # opus would override haiku
        assert ui["prompt"] == "find the tests"     # verbatim, built-in type
        assert ui["description"] == "short label"   # untouched
        assert ui["run_in_background"] is True      # unknown fields carried

    def test_named_agent_body_is_inlined(self, tmp_path):
        agents = tmp_path / "proj" / ".claude" / "agents"
        agents.mkdir(parents=True)
        (agents / "reviewer.md").write_text(
            "---\nname: code-reviewer\ndescription: reviews\n---\n"
            "Always check for X."
        )
        out = _route(_payload(subagent_type="code-reviewer"),
                     cwd=str(tmp_path / "proj"))
        p = out["hookSpecificOutput"]["updatedInput"]["prompt"]
        assert "Always check for X." in p
        assert p.endswith("find the tests")  # original brief last, verbatim


class TestPassthrough:
    def test_fork_passes_through(self):
        assert _route(_payload(subagent_type="fork")) is None

    def test_bridge_loop_guard(self):
        assert _route(_payload(subagent_type=BRIDGE_AGENT)) is None

    def test_route_off(self):
        assert _route(_payload(), cfg=SubagentsConfig(route="off")) is None

    def test_no_session_or_unhealthy_codex(self):
        assert _route(_payload(), has_session=False) is None
        assert _route(_payload(), codex_ok=False) is None

    def test_malformed_input(self):
        assert _route({"tool_input": "not a dict"}) is None
        assert _route(_payload(prompt="")) is None


class TestFindAgentBody:
    def test_builtin_and_plugin_scoped_have_no_body(self, tmp_path):
        assert find_agent_body("Explore", str(tmp_path), tmp_path) == ""
        assert find_agent_body("my-plugin:reviewer", str(tmp_path),
                               tmp_path) == ""

    def test_walks_up_and_falls_back_to_user_home(self, tmp_path):
        (tmp_path / ".claude" / "agents").mkdir(parents=True)
        (tmp_path / ".claude" / "agents" / "helper.md").write_text(
            "---\nname: helper\n---\nBody from repo root."
        )
        nested = tmp_path / "a" / "b"
        nested.mkdir(parents=True)
        body = find_agent_body("helper", str(nested), tmp_path / "nohome")
        assert body == "Body from repo root."


class TestCli:
    def test_prints_decision_and_exits_zero(self, env_factory, monkeypatch):
        import click.testing
        from tandem import cli
        env = env_factory(active="claude")
        payload = _payload()
        payload["cwd"] = env.cwd
        r = click.testing.CliRunner().invoke(
            cli.main, ["hook-route"], input=json.dumps(payload))
        assert r.exit_code == 0
        out = json.loads(r.output)
        assert out["hookSpecificOutput"]["updatedInput"]["subagent_type"] \
            == BRIDGE_AGENT

    def test_any_crash_exits_zero_silent(self, monkeypatch, tmp_path):
        import click.testing
        from tandem import cli, hookroute
        monkeypatch.setenv("TANDEM_HOME", str(tmp_path / ".tandem"))
        monkeypatch.setattr(hookroute, "route",
                            lambda *a, **kw: 1 / 0)
        r = click.testing.CliRunner().invoke(
            cli.main, ["hook-route"], input=json.dumps(_payload()))
        assert r.exit_code == 0
        assert r.output == ""

    def test_garbage_stdin_exits_zero_silent(self, tmp_path, monkeypatch):
        import click.testing
        from tandem import cli
        monkeypatch.setenv("TANDEM_HOME", str(tmp_path / ".tandem"))
        r = click.testing.CliRunner().invoke(
            cli.main, ["hook-route"], input="not json {{{")
        assert r.exit_code == 0
        assert r.output == ""
