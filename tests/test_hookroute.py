"""hook-route: reroute native Agent dispatches to the codex-worker bridge.
Failure discipline: any problem -> None (native dispatch), CLI always exit 0."""

import json
from pathlib import Path

import pytest

from tandem import paths
from tandem.config import SubagentsConfig
from tandem.hookroute import (
    BRIDGE_AGENT,
    BRIDGE_MODEL,
    BRIDGE_NAME,
    NOTICE_CODEX,
    NOTICE_NO_SESSION,
    find_agent_body,
    missed_reroute_notice,
    route,
)


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


def _notice(payload, cfg=CFG, has_session=False, codex_ok=True,
            already_warned=False):
    return missed_reroute_notice(payload, cfg, has_session=has_session,
                                 codex_ok=codex_ok,
                                 already_warned=already_warned)


def _run_hook(payload):
    import click.testing

    from tandem import cli
    return click.testing.CliRunner().invoke(
        cli.main, ["hook-route"], input=json.dumps(payload))


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

    def test_task_alias_is_rerouted(self):
        payload = _payload()
        payload["tool_name"] = "Task"  # the documented alias of Agent
        ui = _route(payload)["hookSpecificOutput"]["updatedInput"]
        assert ui["subagent_type"] == BRIDGE_AGENT

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

    def test_bridge_loop_guard_is_scope_insensitive(self):
        # the model asks for the bridge by either id (both observed live);
        # rewriting the bare name would re-enter this hook forever
        assert _route(_payload(subagent_type=BRIDGE_NAME)) is None
        assert _route(_payload(subagent_type=f"other:{BRIDGE_NAME}")) is None

    def test_route_off(self):
        assert _route(_payload(), cfg=SubagentsConfig(route="off")) is None

    def test_no_session_or_unhealthy_codex(self):
        assert _route(_payload(), has_session=False) is None
        assert _route(_payload(), codex_ok=False) is None

    def test_other_tool_names_pass_through(self):
        # the plugin matcher should never send these here; if it ever does,
        # rewriting an unrelated tool's input would corrupt the call
        payload = _payload()          # otherwise fully rewritable
        payload["tool_name"] = "Bash"
        assert _route(payload) is None
        del payload["tool_name"]
        assert _route(payload) is None

    def test_malformed_input(self):
        assert _route({"tool_name": "Agent", "tool_input": "not a dict"}) is None
        assert _route(_payload(prompt="")) is None


class TestNotice:
    def test_no_session_notice_carries_no_permission_decision(self):
        out = _notice(_payload())
        assert out == {"systemMessage": NOTICE_NO_SESSION}
        assert "hookSpecificOutput" not in out and "decision" not in out
        assert "Run `tandem` here" in NOTICE_NO_SESSION

    def test_codex_variant_names_its_own_cause(self):
        out = _notice(_payload(), has_session=True, codex_ok=False)
        assert out == {"systemMessage": NOTICE_CODEX}
        assert NOTICE_CODEX != NOTICE_NO_SESSION
        assert "codex" in NOTICE_CODEX

    def test_silent_when_the_reroute_works(self):
        # a rewrite is emitted here; a notice must never accompany one
        assert _notice(_payload(), has_session=True, codex_ok=True) is None

    def test_route_off_never_warns(self):
        # explicit user choice — silence is the requested behavior
        assert _notice(_payload(), cfg=SubagentsConfig(route="off")) is None
        assert _notice(_payload(), cfg=SubagentsConfig(route="off"),
                       has_session=True, codex_ok=False) is None

    def test_already_warned_suppresses(self):
        assert _notice(_payload(), already_warned=True) is None

    def test_only_agent_dispatches_warn(self):
        payload = _payload()
        payload["tool_name"] = "Bash"
        assert _notice(payload) is None
        del payload["tool_name"]
        assert _notice(payload) is None

    def test_fork_and_bridge_still_warn_without_a_session(self):
        # deliberately not special-cased: with no paired session nothing can
        # reroute, so the explanation is due whatever the dispatch asked for
        assert _notice(_payload(subagent_type="fork")) is not None
        assert _notice(_payload(subagent_type=BRIDGE_AGENT)) is not None

    def test_malformed_input_still_warns(self):
        # the notice explains the environment, not the dispatch's shape
        assert _notice({"tool_name": "Agent"}) == {
            "systemMessage": NOTICE_NO_SESSION}


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
        # the working path is untouched by the notice: no extra key, no stamp
        assert "systemMessage" not in out
        assert not (paths.tandem_home() / "warned").exists()

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


class TestCliNotice:
    """The wrapper half: stamp bookkeeping and what reaches stdout."""

    def _unpaired(self, tmp_path, monkeypatch, session_id="s-1"):
        monkeypatch.setenv("TANDEM_HOME", str(tmp_path / ".tandem"))
        payload = _payload()
        payload["cwd"] = str(tmp_path)      # no paired session here
        if session_id is not None:
            payload["session_id"] = session_id
        return payload

    def test_first_dispatch_warns_without_deciding_and_stamps(
            self, tmp_path, monkeypatch):
        payload = self._unpaired(tmp_path, monkeypatch)
        r = _run_hook(payload)
        assert r.exit_code == 0
        out = json.loads(r.output)
        assert out == {"systemMessage": NOTICE_NO_SESSION}
        assert "hookSpecificOutput" not in out and "decision" not in out
        assert (tmp_path / ".tandem" / "warned" / "s-1").exists()

    def test_second_dispatch_same_session_is_silent(
            self, tmp_path, monkeypatch):
        payload = self._unpaired(tmp_path, monkeypatch)
        assert _run_hook(payload).output != ""
        again = _run_hook(payload)
        assert again.exit_code == 0
        assert again.output == ""
        # a different claude session warns again
        payload["session_id"] = "s-2"
        assert json.loads(_run_hook(payload).output) == {
            "systemMessage": NOTICE_NO_SESSION}

    def test_route_off_is_silent(self, tmp_path, monkeypatch):
        payload = self._unpaired(tmp_path, monkeypatch)
        (tmp_path / ".tandem").mkdir(parents=True, exist_ok=True)
        (tmp_path / ".tandem" / "config.toml").write_text(
            '[subagents]\nroute = "off"\n')
        r = _run_hook(payload)
        assert r.exit_code == 0
        assert r.output == ""
        assert not (tmp_path / ".tandem" / "warned").exists()

    def test_paired_session_with_broken_codex_names_codex(
            self, env_factory, monkeypatch):
        from tandem.harness.codex import CodexAdapter
        env = env_factory(active="claude")
        monkeypatch.setattr(CodexAdapter, "detect_version", lambda self: None)
        payload = _payload()
        payload["cwd"] = env.cwd
        payload["session_id"] = "s-codex"
        r = _run_hook(payload)
        assert r.exit_code == 0
        assert json.loads(r.output) == {"systemMessage": NOTICE_CODEX}

    def test_unusable_stamp_dir_warns_anyway_and_exits_zero(
            self, tmp_path, monkeypatch):
        payload = self._unpaired(tmp_path, monkeypatch)
        (tmp_path / ".tandem").mkdir(parents=True, exist_ok=True)
        (tmp_path / ".tandem" / "warned").write_text("not a directory")
        for _ in range(2):  # unstampable => warns every time, never blocks
            r = _run_hook(payload)
            assert r.exit_code == 0
            assert json.loads(r.output) == {"systemMessage": NOTICE_NO_SESSION}

    def test_missing_session_id_warns_anyway(self, tmp_path, monkeypatch):
        payload = self._unpaired(tmp_path, monkeypatch, session_id=None)
        for _ in range(2):  # nothing to stamp => no suppression, still exit 0
            r = _run_hook(payload)
            assert r.exit_code == 0
            assert json.loads(r.output) == {"systemMessage": NOTICE_NO_SESSION}
        assert not (tmp_path / ".tandem" / "warned").exists()

    @pytest.mark.parametrize("bad_id", ["../../evil", "..", "a/b"])
    def test_hostile_session_id_never_becomes_a_path(
            self, tmp_path, monkeypatch, bad_id):
        """`session_id` is untrusted payload text that names a file, so the
        id filter is the whole security boundary here. A rejected id must
        degrade like a missing one: warn (every time — nothing suppresses
        it), exit 0, and write nothing inside or outside TANDEM_HOME."""
        home = tmp_path / "home"            # `warned/../../x` escapes to here
        monkeypatch.setenv("TANDEM_HOME", str(home / ".tandem"))
        payload = _payload()
        payload["cwd"] = str(tmp_path)      # no paired session here
        payload["session_id"] = bad_id
        for _ in range(2):  # unstampable => no suppression, warns again
            r = _run_hook(payload)
            assert r.exit_code == 0
            assert json.loads(r.output) == {"systemMessage": NOTICE_NO_SESSION}
        warned = home / ".tandem" / "warned"
        assert not warned.exists() or not list(warned.iterdir())
        # nothing was created at the traversal target, or anywhere else
        # outside the tandem home the stamp path was supposed to stay in
        assert not (home / "evil").exists()
        assert [p.name for p in home.iterdir()] == [".tandem"]

    def test_stamps_are_pruned_after_a_week(self, tmp_path, monkeypatch):
        import os
        payload = self._unpaired(tmp_path, monkeypatch)
        warned = tmp_path / ".tandem" / "warned"
        warned.mkdir(parents=True)
        stale = warned / "s-old"
        stale.touch()
        os.utime(stale, (0, 0))
        _run_hook(payload)
        assert not stale.exists()
        assert (warned / "s-1").exists()

    def test_one_broken_stamp_does_not_stop_the_prune(
            self, tmp_path, monkeypatch):
        # a dangling symlink has no stat() to follow; pruning it must stay a
        # per-entry failure rather than stranding every other stale stamp
        import os
        payload = self._unpaired(tmp_path, monkeypatch)
        warned = tmp_path / ".tandem" / "warned"
        warned.mkdir(parents=True)
        (warned / "dangling").symlink_to(tmp_path / "does-not-exist")
        stale = warned / "s-old"
        stale.touch()
        os.utime(stale, (0, 0))
        r = _run_hook(payload)
        assert r.exit_code == 0
        assert not stale.exists()
