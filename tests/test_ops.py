"""Switch + one-off routing: role flips drain the outgoing side, fast-forward
the incoming side, and never echo synced entries back."""

import json

import pytest

from tandem import ops
from tandem.sync import SyncSetupError
from tandem.util import read_jsonl

from conftest import (
    claude_assistant,
    claude_tool_result,
    claude_user,
    codex_turn,
    shadow_texts,
    write_line,
)


def codex_tool_turn(user_text, assistant_text, call_id="call-echo-1"):
    """A codex turn that includes a completed native tool call."""
    return [
        {"timestamp": "t", "type": "event_msg",
         "payload": {"type": "task_started", "turn_id": "x"}},
        {"timestamp": "t", "type": "event_msg",
         "payload": {"type": "user_message", "message": user_text}},
        {"timestamp": "t", "type": "response_item",
         "payload": {"type": "function_call", "name": "exec_command",
                     "arguments": '{"cmd": "ls"}', "call_id": call_id}},
        {"timestamp": "t", "type": "response_item",
         "payload": {"type": "function_call_output", "call_id": call_id,
                     "output": "a.py"}},
        {"timestamp": "t", "type": "response_item",
         "payload": {"type": "message", "role": "assistant", "phase": "final_answer",
                     "content": [{"type": "output_text", "text": assistant_text}]}},
        {"timestamp": "t", "type": "event_msg",
         "payload": {"type": "task_complete", "turn_id": "x",
                     "last_agent_message": assistant_text}},
    ]


def codex_call_ids(rollout_path):
    return [
        e["payload"].get("call_id")
        for e in read_jsonl(rollout_path)
        if e.get("type") == "response_item"
        and e["payload"].get("type") in ("function_call", "custom_tool_call")
    ]


def claude_tool_use_ids(transcript_path):
    return [
        b.get("id")
        for e in read_jsonl(transcript_path)
        if e.get("type") == "assistant"
        for b in (e.get("message") or {}).get("content") or []
        if isinstance(b, dict) and b.get("type") == "tool_use"
    ]


class TestSwitch:
    def test_switch_drains_then_flips(self, env_factory):
        env = env_factory(active="claude")
        # steady state: everything currently in claude's file is known
        ops.fast_forward(env.store, env.session, "claude")

        # claude (active) writes a turn that was not yet synced (e.g. tandem
        # crashed mid-session)
        write_line(env.claude_shadow, claude_user("last words before switch"))
        write_line(env.claude_shadow, claude_assistant([{"type": "text", "text": "ok!"}]))

        new_active, problems, _mem = ops.switch_session(env.store, env.session)
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

    def test_switch_back_to_never_run_claude_seeds_shadow(self, env_factory):
        # Pairing seeds only the shadow side's file; claude's CLI creates its
        # own transcript on the first *turn*, not at launch. Flipping away
        # from a zero-turn claude therefore leaves the recorded session id
        # with no file (the test env pre-creates it, so remove it), and the
        # flip back must seed one rather than die in the codex->claude drain.
        env = env_factory(active="claude")
        env.claude_shadow.unlink()

        ops.switch_session(env.store, env.session)
        session = env.refresh()
        assert session.active == "codex"

        for obj in codex_turn("hello from codex", "Hi!"):
            write_line(env.codex_shadow, obj)

        new_active, problems, _mem = ops.switch_session(env.store, session)
        assert new_active == "claude"
        assert problems == []
        contents = [json.dumps(e) for e in read_jsonl(env.claude_shadow)]
        assert any("[via codex] hello from codex" in c for c in contents)
        assert any("[via codex] Hi!" in c for c in contents)

    def test_switch_back_after_claude_file_loss_still_errors(self, env_factory):
        # A claude cursor that has consumed bytes means the transcript
        # existed; its file going missing mid-session is data loss, not the
        # never-ran case, and must keep the hard error.
        env = env_factory(active="claude")
        ops.fast_forward(env.store, env.session, "claude")
        ops.switch_session(env.store, env.session)
        session = env.refresh()

        env.claude_shadow.unlink()
        with pytest.raises(SyncSetupError, match="shadow transcript missing"):
            ops.switch_session(env.store, session)

    def test_double_switch_round_trip(self, env_factory):
        env = env_factory(active="claude")
        ops.fast_forward(env.store, env.session, "claude")
        ops.switch_session(env.store, env.session)
        session = env.refresh()
        assert session.active == "codex"
        new_active, problems, _mem = ops.switch_session(env.store, session)
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

    def test_oneoff_appends_do_not_echo_back(self, env_factory, monkeypatch):
        """run_oneoff's closing drain writes tandem's translation of the
        target's turn into the OTHER file; that side's cursor must move past
        those appends, or the next drain bounces them back — duplicate
        call_ids, which both replay APIs reject."""
        env = env_factory(active="claude")
        ops.fast_forward(env.store, env.session, "claude")

        def fake_run(argv, cwd=None, **kw):
            for obj in codex_tool_turn("patch it", "Patched."):
                write_line(env.codex_shadow, obj)

            class R:
                returncode = 0

            return R()

        monkeypatch.setattr(ops, "_run", fake_run)
        ops.run_oneoff(env.store, env.session, "codex", "patch it")

        # the echo side (claude) is drained again, as the next switch/sync does
        ops.drain_source(env.store, env.refresh(), "claude")

        ids = codex_call_ids(env.codex_shadow)
        assert ids == ["call-echo-1"], f"echoed call ids: {ids}"
        texts = shadow_texts(env.codex_shadow)
        assert sum("Patched." in t for t in texts) == 1, texts

    def test_oneoff_on_claude_does_not_echo_back(self, env_factory, monkeypatch):
        """Mirror direction: codex active, one-off routed to claude."""
        env = env_factory(active="codex")
        ops.fast_forward(env.store, env.session, "codex")

        def fake_run(argv, cwd=None, **kw):
            write_line(env.claude_shadow, claude_user("patch it"))
            write_line(env.claude_shadow, claude_assistant(
                [{"type": "tool_use", "id": "tu-echo-1", "name": "Bash",
                  "input": {"command": "ls"}}], uuid="a-echo-1"))
            write_line(env.claude_shadow, claude_tool_result("tu-echo-1", "a.py"))
            write_line(env.claude_shadow, claude_assistant(
                [{"type": "text", "text": "Patched."}], uuid="a-echo-2"))

            class R:
                returncode = 0

            return R()

        monkeypatch.setattr(ops, "_run", fake_run)
        ops.run_oneoff(env.store, env.session, "claude", "patch it")

        ops.drain_source(env.store, env.refresh(), "codex")

        ids = claude_tool_use_ids(env.claude_shadow)
        assert ids == ["tu-echo-1"], f"echoed tool_use ids: {ids}"
        dump = json.dumps(read_jsonl(env.claude_shadow))
        assert dump.count("Patched.") == 1, dump


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

    def test_claude_metadata_entry_types_validate(self, env_factory):
        """claude 2.1.220 interleaves uuid-less metadata entries with the
        conversation; a healthy transcript containing them must validate."""
        from tandem.doctor import validate_transcript

        env = env_factory()
        sid = env.session.claude_session_id
        for meta in [
            {"type": "permission-mode", "permissionMode": "bypassPermissions",
             "sessionId": sid},
            {"type": "ai-title", "aiTitle": "Fix switch warnings",
             "sessionId": sid},
            {"type": "file-history-delta", "messageId": "m-1",
             "snapshotMessageId": "s-1", "trackingPath": "pyproject.toml"},
            {"type": "pr-link", "sessionId": sid, "prNumber": 3,
             "prUrl": "https://github.com/x/y/pull/3", "prRepository": "x/y"},
            {"type": "relocated", "sessionId": sid, "relocatedCwd": "/tmp/wt"},
            {"type": "worktree-state", "worktreeSession": {"worktreeName": "wt"}},
        ]:
            write_line(env.claude_shadow, obj=meta)
        assert validate_transcript("claude", env.claude_shadow, sid) == []
