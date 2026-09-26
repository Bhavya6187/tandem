"""The `/` catalog: tandem's window commands, the routes, then the default
harness's own commands. Pure: no terminal, no runtime."""

from tandem.chat.commands import WINDOW, Command, catalog, help_lines


def test_window_commands_come_first_with_descriptions():
    got = catalog(["claude", "codex"], "claude", {})
    names = [c.name for c in got[: len(WINDOW)]]
    assert names == [c.name for c in WINDOW]
    assert all(c.description and c.origin == "tandem" for c in got[: len(WINDOW)])
    assert {"help", "status", "compact", "model", "skip-permissions", "note", "quit"} <= set(names)


def test_routes_follow_one_per_participant():
    got = catalog(["claude", "codex"], "claude", {})
    routes = [c for c in got if c.origin == "route"]
    assert [c.name for c in routes] == ["claude", "codex"]
    assert "pins" in routes[0].description         # says `:model` pins


def test_only_the_default_harnesss_commands_are_listed():
    hc = {"claude": [Command("deep-research", "claude command", "claude")],
          "opencode": [Command("init", "guided AGENTS.md setup", "opencode")]}
    got = catalog(["claude", "opencode"], "opencode", hc)
    tail = [c for c in got if c.origin not in ("tandem", "route")]
    assert tail == hc["opencode"]


def test_a_harness_command_that_shadows_a_window_command_is_dropped():
    hc = {"claude": [Command("compact", "x", "claude"), Command("review", "y", "claude")]}
    got = catalog(["claude"], "claude", hc)
    assert [c.name for c in got if c.origin == "claude"] == ["review"]


def test_help_lines_group_by_origin():
    got = catalog(["claude", "codex"], "codex", {"codex": []})
    lines = help_lines(got)
    assert lines[0] == "tandem:"
    assert any(l.startswith("  /help") for l in lines)
    assert "routes:" in lines
    assert lines[-1].startswith("  /codex")        # the last route; codex has no commands
