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
        # 2 claude text messages + 2 tool-call action summaries
        assert len(texts) == 4
        assert all(t.startswith("[via claude-code]") for t in texts)

        summaries = [t for t in texts if "hello.txt" in t and ("created" in t or "ran" in t)]
        assert any("created" in t and "+hi tandem" in t for t in texts)
        assert any("ran `cat" in t and "hi tandem" in t for t in texts)

        # tool pairing consumed all pending calls
        assert ctx.pending_calls == {}

        # thinking, attachments, queue ops produce nothing
        kinds = {e["payload"].get("type") for e in entries}
        assert kinds == {"message", "user_message", "agent_message"}

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

        users = [e for e in entries if e["type"] == "user"]
        assert len(users) == 1
        assert users[0]["message"]["content"].startswith(
            "[via codex] Create a file named hello.txt"
        )

        assistants = [e for e in entries if e["type"] == "assistant"]
        texts = [a["message"]["content"][0]["text"] for a in assistants]
        assert all(t.startswith("[via codex]") for t in texts)
        # 3 codex messages (2 commentary + 1 final) + 4 action summaries
        assert len(texts) == 7

        patch_summaries = [t for t in texts if "applied patch" in t]
        assert len(patch_summaries) == 1
        assert "add hello.txt" in patch_summaries[0]
        assert "*** Begin Patch" in patch_summaries[0]

        exec_summaries = [t for t in texts if t.startswith("[via codex] ran `")]
        assert len(exec_summaries) == 3
        assert any("`cat hello.txt` -> exit 0" in t for t in exec_summaries)
        assert ctx.pending_calls == {}

    def test_exec_output_header_stripped(self):
        entries, _ = translate_file("codex-probe.jsonl", "codex->claude")
        texts = [
            e["message"]["content"][0]["text"]
            for e in entries
            if e["type"] == "assistant"
        ]
        cat = next(t for t in texts if "`cat hello.txt`" in t)
        assert "Chunk ID" not in cat
        assert "Original token count" not in cat
        assert "hi tandem" in cat
