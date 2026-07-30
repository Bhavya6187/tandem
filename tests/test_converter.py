"""Reference converter: golden transcripts in, native shadow entries out."""

import json
from pathlib import Path

from tandem.converter import ReferenceConverter, TranslationError
from tandem.events import SessionContext

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
        claude_leaf_uuid="seed-leaf",
    )


def translate_file(name, direction):
    conv = ReferenceConverter()
    ctx = make_ctx(direction)
    out = []
    for raw in load(name):
        got = conv.translate_entry(raw, direction, ctx)
        assert not isinstance(got, TranslationError), got
        out.extend(got)
    return out, ctx


class TestClaudeToCodex:
    def test_golden(self):
        entries, ctx = translate_file("claude-probe.jsonl", "claude->codex")

        # every rollout line is shaped {timestamp, type, payload}
        assert all(set(e) == {"timestamp", "type", "payload"} for e in entries)

        user_items = [
            e for e in entries
            if e["type"] == "response_item" and e["payload"].get("role") == "user"
        ]
        assert len(user_items) == 1
        text = user_items[0]["payload"]["content"][0]["text"]
        assert text.startswith("[via claude-code] Create a file named hello.txt")

        assistant_items = [
            e for e in entries
            if e["type"] == "response_item" and e["payload"].get("role") == "assistant"
        ]
        texts = [a["payload"]["content"][0]["text"] for a in assistant_items]
        # 2 claude text messages, still attribution-tagged
        assert len(texts) == 2
        assert all(t.startswith("[via claude-code]") for t in texts)

        # the Write became a native custom_tool_call apply_patch pair
        customs = [e["payload"] for e in entries
                   if e["payload"].get("type") == "custom_tool_call"]
        assert len(customs) == 1
        assert customs[0]["name"] == "apply_patch"
        assert "*** Add File:" in customs[0]["input"]
        assert "+hi tandem" in customs[0]["input"]

        # the Bash became a native exec_command pair
        fcalls = [e["payload"] for e in entries
                  if e["payload"].get("type") == "function_call"]
        assert len(fcalls) == 1
        assert fcalls[0]["name"] == "exec_command"
        cmd = json.loads(fcalls[0]["arguments"])["cmd"]
        assert cmd.startswith("cat ") and cmd.endswith("hello.txt")
        outs = [e["payload"] for e in entries
                if e["payload"].get("type", "").endswith("_output")]
        assert len(outs) == 2
        assert any("hi tandem" in o["output"] for o in outs)

        # tool pairing consumed all pending calls
        assert ctx.pending_calls == {}

        # thinking, attachments, queue ops produce nothing
        kinds = {e["payload"].get("type") for e in entries}
        assert kinds == {
            "message", "user_message", "agent_message",
            "custom_tool_call", "custom_tool_call_output",
            "function_call", "function_call_output",
        }

    def test_orphan_result_falls_back_to_prose(self):
        # a result whose call was never seen (e.g. a restart lost the
        # pairing): a lone native tool_result would break replay, so the
        # fallback stays prose
        conv = ReferenceConverter()
        ctx = make_ctx("claude->codex")
        got = conv.translate_entry(
            {"type": "user", "uuid": "u1", "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "gone", "content": "3 failed"}]}},
            "claude->codex", ctx,
        )
        assert [e["payload"]["type"] for e in got] == ["message", "agent_message"]
        assert got[0]["payload"]["content"][0]["text"] == (
            "[via claude-code] tool result (ok): 3 failed"
        )

    def test_error_localized_to_entry(self):
        conv = ReferenceConverter()
        ctx = make_ctx("claude->codex")
        # a user entry whose message is a number triggers a parse crash
        bad = {"type": "user", "message": 42}
        got = conv.translate_entry(bad, "claude->codex", ctx)
        assert isinstance(got, TranslationError)
        # converter keeps working afterwards
        ok = conv.translate_entry(
            {"type": "user", "message": {"role": "user", "content": "hi"}},
            "claude->codex",
            ctx,
        )
        assert isinstance(ok, list) and ok


class TestCodexToClaude:
    def test_golden(self):
        entries, ctx = translate_file("codex-probe.jsonl", "codex->claude")

        # chain integrity: first parent is the seed leaf, then each entry
        # parents the previous one; ctx tracks the final leaf
        assert entries[0]["parentUuid"] == "seed-leaf"
        for prev, cur in zip(entries, entries[1:]):
            assert cur["parentUuid"] == prev["uuid"]
        assert ctx.claude_leaf_uuid == entries[-1]["uuid"]
        assert all(e["sessionId"] == ctx.claude_session_id for e in entries)

        prompts = [e for e in entries if e["type"] == "user"
                   and isinstance(e["message"]["content"], str)]
        assert len(prompts) == 1
        assert prompts[0]["message"]["content"].startswith(
            "[via codex] Create a file named hello.txt"
        )

        texts = [
            e["message"]["content"][0]["text"] for e in entries
            if e["type"] == "assistant"
            and e["message"]["content"][0]["type"] == "text"
        ]
        # 3 codex messages (2 commentary + 1 final), still tagged; no summaries
        assert len(texts) == 3
        assert all(t.startswith("[via codex]") for t in texts)

        tool_uses = [
            e["message"]["content"][0] for e in entries
            if e["type"] == "assistant"
            and e["message"]["content"][0]["type"] == "tool_use"
        ]
        # apply_patch add hello.txt -> Write; 3 exec_command -> Bash
        # (count, not order: the probe's call order is not pinned here)
        assert sorted(t["name"] for t in tool_uses) == ["Bash", "Bash", "Bash", "Write"]
        write = next(t for t in tool_uses if t["name"] == "Write")
        assert write["input"]["file_path"].endswith("hello.txt")
        assert "hi tandem" in write["input"]["content"]
        assert any(t["name"] == "Bash" and t["input"]["command"] == "cat hello.txt"
                   for t in tool_uses)

        results = [e for e in entries if e["type"] == "user"
                   and isinstance(e["message"]["content"], list)]
        assert len(results) == 4
        cat = next(e for e in results
                   if "hi tandem" in e["message"]["content"][0]["content"])
        # exec header stripped from content, exit code in toolUseResult
        assert "Original token count" not in cat["message"]["content"][0]["content"]
        assert cat["toolUseResult"]["exitCode"] == 0
        assert ctx.pending_calls == {}
