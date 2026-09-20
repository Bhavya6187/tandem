"""The chat window's routing grammar: what routes, what passes through."""
import pytest

from tandem import promptroute
from tandem.promptroute import Route, RouteError, is_passthrough_command, parse_route

PARTS = ["claude", "codex", "opencode"]


def test_harness_route_with_body():
    assert parse_route("/codex fix the flaky test", PARTS) == (Route("codex", None), "fix the flaky test")


def test_bare_route_has_empty_body():
    assert parse_route("/codex", PARTS) == (Route("codex", None), "")
    assert parse_route("/codex   ", PARTS) == (Route("codex", None), "")


def test_route_only_at_start():
    assert parse_route("please ask /codex to fix it", PARTS) is None


def test_path_after_harness_name_is_not_a_route():
    assert parse_route("/codex/README.md explain this", PARTS) is None
    assert parse_route("/claude:haiku/x go", PARTS) is None


def test_file_mentions_are_never_interpreted():
    assert parse_route("@src/foo.py explain this", PARTS) is None
    assert parse_route("@CLAUDE.md summarize", PARTS) is None


def test_other_slash_commands_pass_through():
    assert parse_route("/compact", PARTS) is None
    assert parse_route("/superpowers:brainstorming go", PARTS) is None
    assert is_passthrough_command("/compact") is True
    assert is_passthrough_command("/codex go") is True   # the caller checks parse_route first
    assert is_passthrough_command("hello") is False


def test_non_participant_harness_is_an_error():
    with pytest.raises(RouteError):
        parse_route("/codex do it", ["claude", "opencode"])


def test_claude_model_passes_through():
    assert parse_route("/claude:haiku summarize", PARTS) == (Route("claude", "haiku"), "summarize")


def test_opencode_provider_model():
    assert parse_route("/opencode:openai/gpt-5.4-mini go", PARTS) == (
        Route("opencode", "openai/gpt-5.4-mini"), "go")


def test_opencode_model_id_may_carry_more_slashes():
    # opencode splits `provider/modelID` on the first slash only, so
    # openrouter ids keep theirs; nobody else's model name may.
    assert parse_route("/opencode:openrouter/anthropic/claude-sonnet-4 go", PARTS) == (
        Route("opencode", "openrouter/anthropic/claude-sonnet-4"), "go")
    assert parse_route("/claude:a/b go", PARTS) is None
    assert parse_route("/codex:a/b go", PARTS) is None


def test_default_clears_pin():
    assert parse_route("/codex:default go", PARTS) == (Route("codex", ""), "go")


def test_codex_model_resolves_via_catalog(monkeypatch):
    monkeypatch.setattr(promptroute.modelcat, "load_catalog",
                        lambda: [{"slug": "gpt-5.5", "visibility": "show"}])
    assert parse_route("/codex:5.5 go", PARTS) == (Route("codex", "gpt-5.5"), "go")


def test_codex_unknown_model_is_an_error(monkeypatch):
    monkeypatch.setattr(promptroute.modelcat, "load_catalog",
                        lambda: [{"slug": "gpt-5.5", "visibility": "show"}])
    with pytest.raises(RouteError):
        parse_route("/codex:no-such-model go", PARTS)


def test_codex_model_verbatim_without_catalog(monkeypatch):
    monkeypatch.setattr(promptroute.modelcat, "load_catalog", lambda: None)
    assert parse_route("/codex:gpt-5.5 go", PARTS) == (Route("codex", "gpt-5.5"), "go")


def test_newline_after_route_still_routes():
    assert parse_route("/codex\nfix it", PARTS) == (Route("codex", None), "fix it")


def test_model_routes_resolve_by_harness(monkeypatch):
    monkeypatch.setattr(promptroute.modelcat, "load_catalog",
                        lambda: [{"slug": "gpt-6-astra"}])
    assert parse_route("/codex:astra go", PARTS) == (
        Route("codex", "gpt-6-astra"), "go")
    assert parse_route("/claude:fable go", PARTS) == (
        Route("claude", "fable"), "go")
    assert parse_route("/opencode:openai/gpt-6-astra go", PARTS) == (
        Route("opencode", "openai/gpt-6-astra"), "go")


def test_at_is_never_a_route():
    # `@` belongs to the file picker and the harnesses' own mentions: even a
    # well-formed harness:model behind it goes out as ordinary text, and a
    # non-participant or an unknown model behind it is nobody's error
    for prompt in ("@codex explain", "@codex:astra go", "@claude:fable-5.1 go",
                   "@codex:default", "@opencode:openai/gpt-6-astra go",
                   "@codex/README.md explain", "@claude:no-such-model go"):
        assert parse_route(prompt, PARTS) is None
    assert parse_route("@codex:astra go", ["claude"]) is None


def test_claude_versioned_name_becomes_the_full_id():
    assert parse_route("/claude:fable-5.1 go", PARTS) == (
        Route("claude", "claude-fable-5-1"), "go")


def test_claude_unknown_model_is_an_error():
    with pytest.raises(RouteError):
        parse_route("/claude:fabel go", PARTS)
