"""Parser tests against golden transcripts captured from the real CLIs
(claude 2.1.220, codex-cli 0.145.0) on 2026-07-28."""

import json
from pathlib import Path

import pytest

from tandem.events import SessionContext
from tandem.harness import get_adapter

GOLDEN = Path(__file__).parent / "golden"


def load(name):
    return [json.loads(l) for l in (GOLDEN / name).read_text().splitlines() if l.strip()]


def make_ctx(direction):
    return SessionContext(
        tandem_id="t1",
        cwd="/probe",
        direction=direction,
        claude_session_id="8efda0e4-15e7-4a20-a8e8-8be898a85ee1",
        codex_session_id="019faca1-ad54-7092-bed0-f0b2cc71e164",
    )


def parse_all(adapter, raws, ctx):
    events = []
    for raw in raws:
        got = adapter.parse_entry(raw, ctx)
        assert got, f"parse_entry returned nothing for {raw.get('type')}"
        events.extend(got)
    return events


class TestClaudeParse:
    def test_golden_transcript(self):
        adapter = get_adapter("claude")
        ctx = make_ctx("claude->codex")
        events = parse_all(adapter, load("claude-probe.jsonl"), ctx)

        users = [e for e in events if e.kind == "user_message"]
        assert len(users) == 1
        assert users[0].text.startswith("Create a file named hello.txt")
        assert users[0].source == "user"
        assert users[0].turn_index == 1

        calls = [e for e in events if e.kind == "tool_call"]
        assert [c.tool for c in calls] == ["Write", "Bash"]
        assert calls[0].arguments["content"] == "hi tandem"

        results = [e for e in events if e.kind == "tool_result"]
        assert len(results) == 2
        assert results[0].call_id == calls[0].call_id
        assert results[0].structured["type"] == "create"
        assert results[1].output == "hi tandem"
        assert results[1].structured["stdout"] == "hi tandem"

        finals = [e for e in events if e.kind == "assistant_message"]
        assert finals[-1].text.startswith("Created hello.txt")
        assert finals[-1].model == "claude-fable-5"

        thinking = [e for e in events if e.kind == "thinking"]
        assert len(thinking) == 1  # signature-bound, content dropped

    def test_unknown_entry_degrades_to_system(self):
        adapter = get_adapter("claude")
        ctx = make_ctx("claude->codex")
        (ev,) = adapter.parse_entry({"type": "brand-new-thing"}, ctx)
        assert ev.kind == "system"
        assert "brand-new-thing" in ev.subtype

    def test_sidechain_skipped(self):
        adapter = get_adapter("claude")
        ctx = make_ctx("claude->codex")
        (ev,) = adapter.parse_entry(
            {"type": "assistant", "isSidechain": True, "message": {"content": []}}, ctx
        )
        assert ev.kind == "system" and ev.subtype == "sidechain"


class TestDeriveLastModel:
    """claude --resume restores the session model from the transcript, so
    rendered entries must carry the model claude itself last used."""

    def write(self, tmp_path, entries):
        p = tmp_path / "transcript.jsonl"
        p.write_text("".join(json.dumps(e) + "\n" for e in entries))
        return p

    def test_last_real_model_wins(self, tmp_path):
        p = self.write(tmp_path, [
            {"type": "assistant", "uuid": "a1",
             "message": {"role": "assistant", "model": "claude-opus-5",
                         "content": []}},
            {"type": "assistant", "uuid": "a2",
             "message": {"role": "assistant", "model": "claude-fable-5",
                         "content": []}},
        ])
        assert get_adapter("claude").derive_last_model(p) == "claude-fable-5"

    def test_synced_and_sidechain_models_skipped(self, tmp_path):
        p = self.write(tmp_path, [
            {"type": "assistant", "uuid": "a1",
             "message": {"role": "assistant", "model": "claude-fable-5",
                         "content": []}},
            {"type": "assistant", "uuid": "side", "isSidechain": True,
             "message": {"role": "assistant", "model": "claude-haiku-4-5",
                         "content": []}},
            {"type": "assistant", "uuid": "a2",
             "message": {"role": "assistant", "model": "<synced>",
                         "content": []}},
        ])
        assert get_adapter("claude").derive_last_model(p) == "claude-fable-5"

    def test_no_real_model_returns_none(self, tmp_path):
        p = self.write(tmp_path, [
            {"type": "user", "uuid": "u1",
             "message": {"role": "user", "content": "hi"}},
        ])
        assert get_adapter("claude").derive_last_model(p) is None


class TestCodexParse:
    def test_golden_rollout(self):
        adapter = get_adapter("codex")
        ctx = make_ctx("codex->claude")
        raws = load("codex-probe.jsonl")

        events = []
        for raw in raws:
            got = adapter.parse_entry(raw, ctx)
            events.extend(got)
            # emulate the sync engine stashing pending calls so
            # patch_apply_end enrichment has somewhere to land
            for ev in got:
                if ev.kind == "tool_call":
                    ctx.pending_calls[ev.call_id] = {"tool": ev.tool}
                if ev.kind == "tool_result" and ev.call_id in ctx.pending_calls:
                    del ctx.pending_calls[ev.call_id]

        users = [e for e in events if e.kind == "user_message"]
        assert len(users) == 1
        assert users[0].text.startswith("Create a file named hello.txt")
        assert users[0].turn_index == 1

        calls = [e for e in events if e.kind == "tool_call"]
        assert [c.tool for c in calls] == [
            "exec_command", "exec_command", "apply_patch", "exec_command",
        ]
        assert "Begin Patch" in calls[2].arguments

        results = [e for e in events if e.kind == "tool_result"]
        assert len(results) == 4
        patch_result = results[2]
        assert patch_result.structured is not None
        assert patch_result.structured["success"] is True
        assert any(
            c.get("type") == "add" and c.get("content") == "hi tandem\n"
            for c in patch_result.structured["changes"].values()
        )

        assistants = [e for e in events if e.kind == "assistant_message"]
        finals = [a for a in assistants if a.phase == "final"]
        assert len(finals) == 1
        assert finals[0].text.startswith("Created and verified")
        commentary = [a for a in assistants if a.phase == "commentary"]
        assert len(commentary) == 2

        # duplicated event_msg agent_message must NOT become assistant events
        agent_msgs = [
            r for r in raws
            if r["type"] == "event_msg" and r["payload"].get("type") == "agent_message"
        ]
        assert len(agent_msgs) == 3  # exist in the file...
        assert len(assistants) == 3  # ...but only response_items were parsed

        # injected env/context user response_items are system, not prompts
        ctx_msgs = [e for e in events if e.kind == "system" and e.subtype.startswith("context_message")]
        assert len(ctx_msgs) >= 4

    def test_unknown_payload_degrades_to_system(self):
        adapter = get_adapter("codex")
        ctx = make_ctx("codex->claude")
        (ev,) = adapter.parse_entry(
            {"type": "event_msg", "payload": {"type": "novel_event"}}, ctx
        )
        assert ev.kind == "system" and "novel_event" in ev.subtype
