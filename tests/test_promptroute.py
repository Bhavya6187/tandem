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

    def _typed(self, env, prompt, focus="claude", **extra):
        """A prompt typed in the focus harness's own window: the hook only
        routes when the payload's session_id is that harness's native id."""
        payload = {"cwd": env.cwd, "prompt": prompt,
                   "session_id": env.session.native_id(focus)}
        payload.update(extra)
        return self._run(payload)

    def _mixed(self, env, focus="claude"):
        from tandem import routefile
        routefile.write_frame_state(env.session.tandem_id,
                                    {"tab": "mixed", "focus": focus,
                                     "routing_ok": True})

    def test_silent_without_session(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TANDEM_HOME", str(tmp_path / ".tandem"))
        r = self._run({"cwd": str(tmp_path), "prompt": "@codex hi there",
                       "session_id": "whatever"})
        assert r.exit_code == 0 and r.output == ""

    def test_silent_outside_the_mixed_tab(self, env_factory):
        # paired, routable prompt, but no frame state: not the mixed tab
        env = env_factory(active="claude")
        r = self._typed(env, "@codex hi there")
        assert r.exit_code == 0 and r.output == ""

    def test_blocks_and_stashes_in_the_mixed_tab(self, env_factory):
        from tandem import routefile
        env = env_factory(active="claude")
        self._mixed(env)
        r = self._typed(env, "@codex fix the test")
        assert r.exit_code == 0
        decision = json.loads(r.output)
        assert decision["decision"] == "block"
        assert "codex" in decision["reason"]
        assert decision["reason"].endswith("running there")
        req = routefile.read_pending(env.session.tandem_id)
        assert req is not None and req.target == "codex"
        assert req.prompt == "fix the test" and req.source == "claude"

    def test_stashes_the_resolved_model_pin(self, env_factory):
        # the model half of the decision has to survive the CLI layer: the
        # relaunch pins it on the target's argv, so a dropped pin silently
        # runs the prompt on the wrong model
        from tandem import routefile
        env = env_factory(active="claude")
        self._mixed(env, focus="codex")
        r = self._typed(env, "@haiku summarize the diff", focus="codex")
        assert r.exit_code == 0
        assert "haiku" in json.loads(r.output)["reason"]
        req = routefile.read_pending(env.session.tandem_id)
        assert req is not None and req.target == "claude"
        assert req.model == "haiku" and req.source == "codex"
        assert req.prompt == "summarize the diff"

    def test_frame_focus_beats_session_active(self, env_factory):
        # mid-flip the DB's `active` lags the frame; the frame file is the
        # authority, so @codex from a codex-focused tab must NOT route
        from tandem import routefile
        env = env_factory(active="claude")
        self._mixed(env, focus="codex")
        r = self._typed(env, "@codex keep going", focus="codex")
        assert r.exit_code == 0 and r.output == ""
        assert routefile.read_pending(env.session.tandem_id) is None

    def test_stay_on_focus_is_silent(self, env_factory):
        from tandem import routefile
        env = env_factory(active="claude")
        self._mixed(env)
        r = self._typed(env, "@claude hi there")
        assert r.exit_code == 0 and r.output == ""
        assert routefile.read_pending(env.session.tandem_id) is None

    def test_foreign_window_is_silent(self, env_factory):
        # a second claude window in the same directory runs this same hook
        # against this same paired session: blocking there would inject the
        # prompt into a terminal the user is not looking at
        from tandem import routefile
        env = env_factory(active="claude")
        self._mixed(env)
        r = self._typed(env, "@codex fix the test",
                        session_id="22222222-2222-4222-8222-222222222222")
        assert r.exit_code == 0 and r.output == ""
        assert routefile.read_pending(env.session.tandem_id) is None

    def test_payload_without_session_id_is_silent(self, env_factory):
        from tandem import routefile
        env = env_factory(active="claude")
        self._mixed(env)
        r = self._run({"cwd": env.cwd, "prompt": "@codex fix the test"})
        assert r.exit_code == 0 and r.output == ""
        assert routefile.read_pending(env.session.tandem_id) is None

    def test_unrecorded_native_id_is_silent(self, env_factory):
        # a harness whose minted id tandem has not captured yet: there is
        # nothing to identify the window by, so doubt allows the native turn
        from tandem import routefile
        env = env_factory(active="claude")
        blind = env.store.create_session(
            env.cwd, "claude", ["claude", "codex"],
            {"claude": None, "codex": "019faca1-0000-7000-8000-000000000002"})
        routefile.write_frame_state(blind.tandem_id,
                                    {"tab": "mixed", "focus": "claude",
                                     "routing_ok": True})
        r = self._run({"cwd": env.cwd, "prompt": "@codex fix the test",
                       "session_id": "11111111-1111-4111-8111-111111111111"})
        assert r.exit_code == 0 and r.output == ""
        assert routefile.read_pending(blind.tandem_id) is None

    def test_unstashable_route_allows_the_native_turn(self, env_factory,
                                                      monkeypatch):
        # the unlosable-prompt invariant: a stash that did not land must not
        # block, or the typed prompt disappears with nothing to replay it
        from tandem import routefile
        env = env_factory(active="claude")
        self._mixed(env)
        monkeypatch.setattr(routefile, "write_route", lambda *a, **kw: None)
        r = self._typed(env, "@codex fix the test")
        assert r.exit_code == 0 and r.output == ""

    def test_a_leftover_route_file_cannot_vouch_for_a_failed_stash(
            self, env_factory, monkeypatch):
        # an earlier request sitting in the slot is exactly what makes a
        # content check dangerous: re-type the same prompt after a failed
        # pickup and prompt+target match, so the leftover would certify a
        # write that never landed and the block would destroy this prompt.
        # Only the id can tell the two apart.
        from tandem import routefile
        env = env_factory(active="claude")
        self._mixed(env)
        leftover = routefile.RouteRequest(
            target="codex", model="", prompt="fix the test",
            source="claude", reason="→ codex")
        routefile.write_route(env.session.tandem_id, leftover)
        monkeypatch.setattr(routefile, "write_route", lambda *a, **kw: None)
        r = self._typed(env, "@codex fix the test")
        assert r.exit_code == 0 and r.output == ""
        assert routefile.read_pending(env.session.tandem_id) == leftover

    def test_a_stash_claimed_mid_hook_still_blocks(self, env_factory,
                                                   monkeypatch):
        # the frame can claim the request — renaming it out of the pending
        # slot — between the write and the verifying read; treating that as
        # a failed stash would run the prompt natively AND on the target,
        # which is worse than either
        from tandem import routefile
        env = env_factory(active="claude")
        self._mixed(env)
        real_write = routefile.write_route

        def write_then_claim(tandem_id, req):
            real_write(tandem_id, req)
            assert routefile.claim(tandem_id, req.id) is True

        monkeypatch.setattr(routefile, "write_route", write_then_claim)
        r = self._typed(env, "@codex fix the test")
        assert json.loads(r.output)["decision"] == "block"

    def test_a_claimed_stash_behind_a_newer_prompt_still_blocks(
            self, env_factory, monkeypatch):
        # the frame claimed this request before verification. Reading only
        # pending state would miss the id and allow a turn that is
        # already on its way to the target — it would then run twice.
        from tandem import routefile
        env = env_factory(active="claude")
        self._mixed(env)
        real_write = routefile.write_route

        def write_claim_then_overwrite(tandem_id, req):
            real_write(tandem_id, req)
            assert routefile.claim(tandem_id, req.id) is True
            real_write(tandem_id, routefile.RouteRequest(
                target="codex", model="", prompt="a second prompt",
                source="claude", reason="→ codex"))

        monkeypatch.setattr(routefile, "write_route",
                            write_claim_then_overwrite)
        r = self._typed(env, "@codex fix the test")
        assert json.loads(r.output)["decision"] == "block"

    def test_empty_prompt_is_silent(self, env_factory):
        env = env_factory(active="claude")
        self._mixed(env)
        r = self._typed(env, "   ")
        assert r.exit_code == 0 and r.output == ""

    def test_any_crash_exits_zero_silent(self, env_factory, monkeypatch):
        env = env_factory(active="claude")
        self._mixed(env)
        monkeypatch.setattr(promptroute, "route_prompt",
                            lambda *a, **kw: 1 / 0)
        r = self._typed(env, "@codex fix the test")
        assert r.exit_code == 0 and r.output == ""

    def test_garbage_stdin_exits_zero_silent(self, tmp_path, monkeypatch):
        import click.testing

        from tandem import cli
        monkeypatch.setenv("TANDEM_HOME", str(tmp_path / ".tandem"))
        r = click.testing.CliRunner().invoke(
            cli.main, ["hook-prompt"], input="{not json")
        assert r.exit_code == 0 and r.output == ""
