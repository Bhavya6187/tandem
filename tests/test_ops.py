"""Switch + one-off routing: role flips drain the outgoing side, fast-forward
the incoming side, and never echo synced entries back."""

import json

from tandem import ops
from tandem.util import read_jsonl

from conftest import (
    claude_assistant,
    claude_user,
    codex_turn,
    shadow_texts,
    write_line,
)


class TestSwitch:
    def test_switch_drains_then_flips(self, env_factory):
        env = env_factory(active="claude")
        # steady state: everything currently in claude's file is known
        ops.fast_forward(env.store, env.session, "claude")

        # claude (active) writes a turn that was not yet synced (e.g. tandem
        # crashed mid-session)
        write_line(env.claude_shadow, claude_user("last words before switch"))
        write_line(env.claude_shadow, claude_assistant([{"type": "text", "text": "ok!"}]))

        new_active, problems = ops.switch_session(env.store, env.session)
        assert new_active == "codex"
        assert problems == []
        session = env.refresh()
        assert session.active == "codex"

        # the unsynced tail was drained into the codex shadow...
        texts = shadow_texts(env.codex_shadow)
        assert "[via claude-code] last words before switch" in texts
        assert "[via claude-code] ok!" in texts

        # ...and codex's cursor now sits at EOF so nothing echoes back
        cursor = env.store.get_cursor(env.session.tandem_id, "codex")
        assert cursor.byte_offset == env.codex_shadow.stat().st_size
        assert ops.unsynced_lines(session, env.store, "codex") == 0

    def test_turns_flow_after_switch_without_echo(self, env_factory):
        env = env_factory(active="claude")
        ops.fast_forward(env.store, env.session, "claude")
        write_line(env.claude_shadow, claude_user("before switch"))
        ops.switch_session(env.store, env.session)
        session = env.refresh()

        # codex (now active) runs a native turn
        for obj in codex_turn("do a thing", "Thing done."):
            write_line(env.codex_shadow, obj)
        ops.drain_source(env.store, session, "codex")

        entries = read_jsonl(env.claude_shadow)
        contents = [json.dumps(e) for e in entries]
        assert any("[via codex] do a thing" in c for c in contents)
        assert any("[via codex] Thing done." in c for c in contents)
        # the pre-switch claude turn must not have bounced back into claude
        assert sum("before switch" in c for c in contents) == 1  # only the original

    def test_double_switch_round_trip(self, env_factory):
        env = env_factory(active="claude")
        ops.fast_forward(env.store, env.session, "claude")
        ops.switch_session(env.store, env.session)
        session = env.refresh()
        assert session.active == "codex"
        new_active, problems = ops.switch_session(env.store, session)
        assert new_active == "claude"
        assert problems == []
        assert env.refresh().active == "claude"


class TestOneOff:
    def test_oneoff_on_shadow_syncs_into_active(self, env_factory, monkeypatch):
        env = env_factory(active="claude")
        ops.fast_forward(env.store, env.session, "claude")

        # fake `codex exec resume`: appends a native turn to the rollout
        calls = {}

        def fake_run(argv, cwd=None, **kw):
            calls["argv"] = argv
            calls["cwd"] = cwd
            for obj in codex_turn("run the linter", "Linter is clean."):
                write_line(env.codex_shadow, obj)

            class R:
                returncode = 0

            return R()

        monkeypatch.setattr(ops, "_run", fake_run)
        code = ops.run_oneoff(env.store, env.session, "codex", "run the linter")
        assert code == 0
        assert calls["argv"][:3] == ["codex", "exec", "--skip-git-repo-check"]
        assert "resume" in calls["argv"]
        assert calls["cwd"] == env.cwd

        # the one-off turn landed in claude's file with attribution
        entries = read_jsonl(env.claude_shadow)
        contents = [json.dumps(e) for e in entries]
        assert any("[via codex] run the linter" in c for c in contents)
        assert any("[via codex] Linter is clean." in c for c in contents)
        # and the codex seed note did NOT echo into claude
        assert not any("one half of tandem paired session" in c for c in contents)

    def test_oneoff_turn_present_in_both_files(self, env_factory, monkeypatch):
        env = env_factory(active="claude")
        ops.fast_forward(env.store, env.session, "claude")

        def fake_run(argv, cwd=None, **kw):
            for obj in codex_turn("q", "a"):
                write_line(env.codex_shadow, obj)

            class R:
                returncode = 0

            return R()

        monkeypatch.setattr(ops, "_run", fake_run)
        ops.run_oneoff(env.store, env.session, "codex", "q")

        # natively in codex...
        codex_texts = shadow_texts(env.codex_shadow)
        assert "a" in codex_texts
        # ...translated in claude
        claude_dump = json.dumps(read_jsonl(env.claude_shadow))
        assert "[via codex] a" in claude_dump

        # afterwards the active side can keep syncing normally
        write_line(env.claude_shadow, claude_user("next claude prompt"))
        ops.drain_source(env.store, env.session, "claude")
        assert any(
            "[via claude-code] next claude prompt" in t
            for t in shadow_texts(env.codex_shadow)
        )


class TestValidate:
    def test_golden_files_validate(self, env_factory):
        from pathlib import Path

        from tandem.doctor import validate_transcript

        golden = Path(__file__).parent / "golden"
        assert validate_transcript(
            "claude", golden / "claude-probe.jsonl",
            "8efda0e4-15e7-4a20-a8e8-8be898a85ee1",
        ) == []
        assert validate_transcript(
            "codex", golden / "codex-probe.jsonl",
            "019faca1-ad54-7092-bed0-f0b2cc71e164",
        ) == []

    def test_seeded_shadows_validate(self, env_factory):
        from tandem.doctor import validate_transcript

        env = env_factory()
        assert validate_transcript("codex", env.codex_shadow,
                                   env.session.codex_session_id) == []
        assert validate_transcript("claude", env.claude_shadow,
                                   env.session.claude_session_id) == []
