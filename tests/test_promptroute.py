"""The mixed tab's @-prefix grammar: what routes, and what passes through."""

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
