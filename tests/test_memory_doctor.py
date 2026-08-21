"""M5: CLAUDE.md<->AGENTS.md mapping, MCP copy, and doctor on healthy and
deliberately corrupted shadow files."""

import json
from pathlib import Path

from tandem.doctor import run_doctor, validate_transcript
from tandem.memory_sync import BEGIN, END, copy_mcp, sync_memory_files

from conftest import write_line


class TestMemorySync:
    def test_creates_agents_md_from_claude_md(self, tmp_path):
        (tmp_path / "CLAUDE.md").write_text("Use uv. Never commit secrets.\n")
        report = sync_memory_files(str(tmp_path))
        assert report.actions and not report.warnings
        agents = (tmp_path / "AGENTS.md").read_text()
        assert BEGIN in agents and END in agents
        assert "Use uv. Never commit secrets." in agents
        # source file untouched (no markers forced on it)
        assert BEGIN not in (tmp_path / "CLAUDE.md").read_text()

    def test_refresh_preserves_tool_specific_sections(self, tmp_path):
        (tmp_path / "AGENTS.md").write_text(
            f"# codex-only notes\nkeep me\n\n{BEGIN}\nold shared\n{END}\n"
        )
        claude = tmp_path / "CLAUDE.md"
        claude.write_text(f"{BEGIN}\nnew shared rules\n{END}\n")
        # make CLAUDE.md the newer file
        import os, time

        now = time.time()
        os.utime(tmp_path / "AGENTS.md", (now - 60, now - 60))
        os.utime(claude, (now, now))

        report = sync_memory_files(str(tmp_path))
        agents = (tmp_path / "AGENTS.md").read_text()
        assert "keep me" in agents
        assert "new shared rules" in agents
        assert "old shared" not in agents
        assert report.actions == ["updated shared block in AGENTS.md from CLAUDE.md"]

    def test_both_without_markers_refuses(self, tmp_path):
        (tmp_path / "CLAUDE.md").write_text("a\n")
        (tmp_path / "AGENTS.md").write_text("b\n")
        report = sync_memory_files(str(tmp_path))
        assert report.warnings and not report.actions
        assert (tmp_path / "CLAUDE.md").read_text() == "a\n"
        assert (tmp_path / "AGENTS.md").read_text() == "b\n"

    def test_neither_exists_is_noop(self, tmp_path):
        report = sync_memory_files(str(tmp_path))
        assert not report.actions and not report.warnings


class TestMcpCopy:
    def test_copy_both_directions_additive(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("CODEX_HOME", str(tmp_path / ".codex"))
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
        (tmp_path / ".codex").mkdir()

        (home / ".claude.json").write_text(json.dumps({
            "mcpServers": {
                "linear": {"command": "npx", "args": ["-y", "linear-mcp"],
                           "env": {"TOKEN": "t"}},
                "shared": {"command": "shared-bin"},
            }
        }))
        (tmp_path / ".codex" / "config.toml").write_text(
            'model = "gpt-5.6-sol"\n\n[mcp_servers.shared]\ncommand = "shared-bin"\n'
            '\n[mcp_servers.repl]\ncommand = "repl-bin"\nargs = []\n'
        )

        report = copy_mcp()
        assert any("linear" in a and "codex" in a for a in report.actions)
        assert any("repl" in a and "claude" in a for a in report.actions)

        # codex got linear, kept its own config intact
        toml_text = (tmp_path / ".codex" / "config.toml").read_text()
        assert "[mcp_servers.linear]" in toml_text
        assert 'model = "gpt-5.6-sol"' in toml_text
        assert toml_text.count("[mcp_servers.shared]") == 1
        import tomllib

        parsed = tomllib.loads(toml_text)
        assert parsed["mcp_servers"]["linear"]["command"] == "npx"
        assert parsed["mcp_servers"]["linear"]["env"]["TOKEN"] == "t"

        # claude got repl, kept linear untouched
        cfg = json.loads((home / ".claude.json").read_text())
        assert cfg["mcpServers"]["repl"]["command"] == "repl-bin"
        assert cfg["mcpServers"]["linear"]["args"] == ["-y", "linear-mcp"]

        # second run: nothing new
        report2 = copy_mcp()
        assert report2.actions == ["MCP configs already in sync (nothing copied)"]


class TestDoctor:
    def test_healthy_pairing_passes(self, env_factory):
        env = env_factory()
        report = run_doctor(env.store, env.session)
        assert not report.failed
        messages = " | ".join(c.message for c in report.checks)
        assert "resumable by structure" in messages

    def test_corrupted_shadow_fails(self, env_factory):
        env = env_factory()
        # deliberately corrupt the codex shadow: torn JSON + wrong-type line
        write_line(env.codex_shadow, text='{"type": "response_item", "payl')
        write_line(env.codex_shadow, obj={"type": "not_a_rollout_type", "payload": {}})
        report = run_doctor(env.store, env.session)
        assert report.failed
        fails = [c.message for c in report.checks if c.status == "fail"]
        assert any("not valid JSON" in m for m in fails)
        assert any("not_a_rollout_type" in m for m in fails)

    def test_missing_session_fails(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TANDEM_HOME", str(tmp_path / ".tandem"))
        from tandem.state import StateStore

        with StateStore(db_path=tmp_path / ".tandem" / "db") as store:
            report = run_doctor(store, None)
        assert report.failed

    def test_validate_flags_wrong_session_id(self, env_factory):
        env = env_factory()
        problems = validate_transcript("codex", env.codex_shadow, "someone-else")
        assert any("session_meta id" in p for p in problems)

    def test_stale_intent_warns(self, env_factory):
        env = env_factory()
        cursor = env.store.get_cursor(env.session.tandem_id, "claude", "codex")
        cursor.pending["intent"] = {"line": 3, "pre_size": 10}
        env.store.save_cursor(cursor)
        report = run_doctor(env.store, env.session)
        assert any(
            "unresolved write intent" in c.message
            for c in report.checks
            if c.status == "warn"
        )

    def test_warns_on_bar_drop_marker(self, env_factory):
        from tandem import paths

        env = env_factory()
        marker = paths.tandem_home() / "tmp" / f"{env.session.tandem_id}-bar-dropped"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()
        report = run_doctor(env.store, env.session)
        assert any(
            c.status == "warn" and "status bar" in c.message for c in report.checks
        )

    def test_quiet_without_bar_drop_marker(self, env_factory):
        env = env_factory()
        report = run_doctor(env.store, env.session)
        assert not any("status bar" in c.message for c in report.checks)

    def test_warns_when_a_participants_prompt_hook_is_not_installed(
            self, env_factory):
        # tmp homes: neither harness has the plugin registered, so neither
        # can route an @-mention typed into it. A warn, not a fail — the
        # pairing works, one direction of the mixed tab does not.
        env = env_factory()
        report = run_doctor(env.store, env.session)
        warns = [c.message for c in report.checks if c.status == "warn"]
        assert any("mixed-tab routing: claude plugin not installed" in m
                   and "tandem plugin install" in m for m in warns)
        assert any("mixed-tab routing: codex plugin not installed" in m
                   for m in warns)
        assert not report.failed

    def test_ok_when_both_prompt_hooks_are_installed(self, env_factory):
        from tandem import paths

        env = env_factory()
        reg = paths.claude_installed_plugins_path()
        reg.parent.mkdir(parents=True, exist_ok=True)
        reg.write_text(json.dumps(
            {"version": 2, "plugins": {"tandem@tandem": [{"scope": "user"}]}}))
        (paths.codex_home() / "config.toml").write_text(
            '[plugins."tandem@tandem"]\nenabled = true\n')
        report = run_doctor(env.store, env.session)
        oks = [c.message for c in report.checks if c.status == "ok"]
        assert "mixed-tab routing: claude hook installed" in oks
        assert "mixed-tab routing: codex hook installed" in oks
        assert not any("mixed-tab routing" in c.message
                       for c in report.checks if c.status == "warn")

    def test_no_routing_line_for_a_harness_without_a_prompt_hook(
            self, tmp_path, monkeypatch):
        # opencode has no UserPromptSubmit hook: a "plugin not installed"
        # warning there would name a remedy that changes nothing.
        from conftest import Env3

        env = Env3(tmp_path, monkeypatch)
        report = run_doctor(env.store, env.session)
        assert not any("mixed-tab routing: opencode" in c.message
                       for c in report.checks)


def test_doctor_iterates_participants(tmp_path, monkeypatch):
    from conftest import Env3

    env = Env3(tmp_path, monkeypatch)
    report = run_doctor(env.store, env.session, live=False)
    text = " ".join(c.message for c in report.checks)
    assert "opencode" in text


def test_adapter_validate_transcript_dispatch(tmp_path, monkeypatch):
    from conftest import Env
    from tandem.harness import get_adapter

    env = Env(tmp_path, monkeypatch)
    problems = get_adapter("claude").validate_transcript(
        env.claude_shadow, env.session.native_id("claude"))
    assert problems == []
