"""The mixed tab's @-prefix grammar: what routes, and what passes through."""

import json

from tandem import promptroute
from tandem.promptroute import RouteDecision, parse_prefix, route_prompt

PARTS = ["claude", "codex", "opencode"]


def test_harness_prefix_routes():
    d, body = parse_prefix("@codex fix the flaky test", PARTS)
    assert d == RouteDecision(harness="codex", model="", reason="→ codex")
    assert body == "fix the flaky test"


def test_prefix_is_first_token_only():
    assert parse_prefix("please ask @codex to fix it", PARTS) is None


def test_unknown_at_token_is_passthrough(monkeypatch):
    # claude file mentions must survive the mixed tab
    monkeypatch.setattr(promptroute.modelcat, "load_catalog",
                        lambda: [{"slug": "gpt-5.3-codex", "visibility": "show"}])
    assert parse_prefix("@src/foo.py explain this", PARTS) is None


def test_bare_prefix_with_no_body_is_passthrough():
    assert parse_prefix("@codex", PARTS) is None
    assert parse_prefix("@codex   ", PARTS) is None


def test_non_participant_harness_is_passthrough():
    assert parse_prefix("@codex do it", ["claude", "opencode"]) is None


def test_harness_colon_model():
    d, body = parse_prefix("@opencode:anthropic/claude-sonnet-5 go", PARTS)
    assert d.harness == "opencode"
    assert d.model == "anthropic/claude-sonnet-5"
    assert body == "go"


def test_codex_colon_model_resolves_via_catalog(monkeypatch):
    monkeypatch.setattr(promptroute.modelcat, "load_catalog",
                        lambda: [{"slug": "gpt-5.3-codex", "visibility": "show"}])
    d, _ = parse_prefix("@codex:5.3 go", PARTS)
    assert d.model == "gpt-5.3-codex"


def test_codex_colon_unresolvable_model_is_passthrough(monkeypatch):
    monkeypatch.setattr(promptroute.modelcat, "load_catalog",
                        lambda: [{"slug": "gpt-5.3-codex", "visibility": "show"}])
    assert parse_prefix("@codex:no-such-model go", PARTS) is None


def test_codex_colon_model_verbatim_without_catalog(monkeypatch):
    # mirrors `tandem sub`: no catalog = pass the name through
    monkeypatch.setattr(promptroute.modelcat, "load_catalog", lambda: None)
    d, _ = parse_prefix("@codex:gpt-5.3-codex go", PARTS)
    assert d.model == "gpt-5.3-codex"


def test_newline_after_token_still_routes():
    d, body = parse_prefix("@codex\nfix the flaky test", PARTS)
    assert d.harness == "codex" and body == "fix the flaky test"


def test_bare_claude_alias_routes_to_claude():
    d, body = parse_prefix("@haiku summarize the diff", PARTS)
    assert d == RouteDecision(harness="claude", model="haiku",
                              reason="→ claude · haiku")
    assert body == "summarize the diff"


def test_claude_file_mentions_pass_through(monkeypatch):
    # the killer case: every one of these normalizes to a "claude" prefix,
    # and routing one would run `claude --model CLAUDE.md` on a typed prompt
    monkeypatch.setattr(promptroute.modelcat, "load_catalog",
                        lambda: [{"slug": "gpt-5.3-codex", "visibility": "show"}])
    for prompt in ("@CLAUDE.md summarize this",
                   "@.claude/settings.json what does this do",
                   "@claude/agents/foo.md explain"):
        assert parse_prefix(prompt, PARTS) is None, prompt


def test_full_claude_slug_still_routes():
    d, body = parse_prefix("@claude-sonnet-5 rewrite it", PARTS)
    assert d == RouteDecision(harness="claude", model="claude-sonnet-5",
                              reason="→ claude · claude-sonnet-5")
    assert body == "rewrite it"


def test_bare_codex_model_needs_catalog(monkeypatch):
    monkeypatch.setattr(promptroute.modelcat, "load_catalog", lambda: None)
    assert parse_prefix("@gpt-5.3-codex go", PARTS) is None
    monkeypatch.setattr(promptroute.modelcat, "load_catalog",
                        lambda: [{"slug": "gpt-5.3-codex", "visibility": "show"}])
    d, _ = parse_prefix("@gpt-5.3-codex go", PARTS)
    assert d == RouteDecision(harness="codex", model="gpt-5.3-codex",
                              reason="→ codex · gpt-5.3-codex")


def test_route_prompt_stay_is_none():
    assert route_prompt("@codex go", focus="codex", participants=PARTS) is None


def test_route_prompt_move():
    got = route_prompt("@codex go", focus="claude", participants=PARTS)
    assert got is not None and got[0].harness == "codex"


class TestCli:
    """`tandem hook-prompt`: the UserPromptSubmit wrapper around the grammar
    above — stdin, the route stash and the block decision. Same discipline as
    hook-route's TestCli: every failure is a silent exit 0 (native turn)."""

    def _run(self, payload):
        import click.testing

        from tandem import cli
        return click.testing.CliRunner().invoke(
            cli.main, ["hook-prompt"], input=json.dumps(payload))

    def _mixed(self, env, focus="claude"):
        from tandem import routefile
        routefile.write_frame_state(env.session.tandem_id,
                                    {"tab": "mixed", "focus": focus,
                                     "routing_ok": True})

    def test_silent_without_session(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TANDEM_HOME", str(tmp_path / ".tandem"))
        r = self._run({"cwd": str(tmp_path), "prompt": "@codex hi there"})
        assert r.exit_code == 0 and r.output == ""

    def test_silent_outside_the_mixed_tab(self, env_factory):
        # paired, routable prompt, but no frame state: not the mixed tab
        env = env_factory(active="claude")
        r = self._run({"cwd": env.cwd, "prompt": "@codex hi there"})
        assert r.exit_code == 0 and r.output == ""

    def test_blocks_and_stashes_in_the_mixed_tab(self, env_factory):
        from tandem import routefile
        env = env_factory(active="claude")
        self._mixed(env)
        r = self._run({"cwd": env.cwd, "prompt": "@codex fix the test"})
        assert r.exit_code == 0
        decision = json.loads(r.output)
        assert decision["decision"] == "block"
        assert "codex" in decision["reason"]
        assert decision["reason"].endswith("running there")
        req = routefile.read_route(env.session.tandem_id)
        assert req is not None and req.target == "codex"
        assert req.prompt == "fix the test" and req.state == "pending"
        assert req.source == "claude"

    def test_frame_focus_beats_session_active(self, env_factory):
        # mid-flip the DB's `active` lags the frame; the frame file is the
        # authority, so @codex from a codex-focused tab must NOT route
        from tandem import routefile
        env = env_factory(active="claude")
        self._mixed(env, focus="codex")
        r = self._run({"cwd": env.cwd, "prompt": "@codex keep going"})
        assert r.exit_code == 0 and r.output == ""
        assert routefile.read_route(env.session.tandem_id) is None

    def test_stay_on_focus_is_silent(self, env_factory):
        from tandem import routefile
        env = env_factory(active="claude")
        self._mixed(env)
        r = self._run({"cwd": env.cwd, "prompt": "@claude hi there"})
        assert r.exit_code == 0 and r.output == ""
        assert routefile.read_route(env.session.tandem_id) is None

    def test_unstashable_route_allows_the_native_turn(self, env_factory,
                                                      monkeypatch):
        # the unlosable-prompt invariant: a stash that did not land must not
        # block, or the typed prompt disappears with nothing to replay it
        from tandem import routefile
        env = env_factory(active="claude")
        self._mixed(env)
        monkeypatch.setattr(routefile, "write_route", lambda *a, **kw: None)
        r = self._run({"cwd": env.cwd, "prompt": "@codex fix the test"})
        assert r.exit_code == 0 and r.output == ""

    def test_empty_prompt_is_silent(self, env_factory):
        env = env_factory(active="claude")
        self._mixed(env)
        r = self._run({"cwd": env.cwd, "prompt": "   "})
        assert r.exit_code == 0 and r.output == ""

    def test_any_crash_exits_zero_silent(self, env_factory, monkeypatch):
        env = env_factory(active="claude")
        self._mixed(env)
        monkeypatch.setattr(promptroute, "route_prompt",
                            lambda *a, **kw: 1 / 0)
        r = self._run({"cwd": env.cwd, "prompt": "@codex fix the test"})
        assert r.exit_code == 0 and r.output == ""

    def test_garbage_stdin_exits_zero_silent(self, tmp_path, monkeypatch):
        import click.testing

        from tandem import cli
        monkeypatch.setenv("TANDEM_HOME", str(tmp_path / ".tandem"))
        r = click.testing.CliRunner().invoke(
            cli.main, ["hook-prompt"], input="{not json")
        assert r.exit_code == 0 and r.output == ""
