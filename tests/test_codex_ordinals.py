"""codex >= 0.155 paginated rollouts: every record carries a contiguous
top-level `ordinal` (session_meta is 0), and codex refuses to resume a
thread whose final record lacks one (`final paginated rollout record at
... is missing an ordinal`). Records tandem appends into such a rollout
must continue the sequence; legacy rollouts (tandem's own seeded shadows)
carry no ordinals and must stay that way."""

import json

from tandem.harness import get_adapter
from tandem.util import read_jsonl

from conftest import claude_assistant, claude_user, write_line


def _meta(history_mode, ordinal=None):
    rec = {"timestamp": "t", "type": "session_meta",
           "payload": {"id": "s", "session_id": "s", "cwd": "/p",
                       "originator": "codex", "cli_version": "0.155.1",
                       "history_mode": history_mode}}
    if ordinal is not None:
        rec["ordinal"] = ordinal
    return rec


def _user(text, ordinal=None):
    rec = {"timestamp": "t", "type": "response_item",
           "payload": {"type": "message", "role": "user",
                       "content": [{"type": "input_text", "text": text}]}}
    if ordinal is not None:
        rec["ordinal"] = ordinal
    return rec


def _note(text):
    return {"timestamp": "t", "type": "response_item",
            "payload": {"type": "message", "role": "user",
                        "content": [{"type": "input_text", "text": text}]}}


def _paginate(path):
    """Rewrite a legacy fixture rollout the way codex 0.155 writes its own:
    history_mode paginated, ordinals 0..n-1 on every record."""
    entries = read_jsonl(path)
    entries[0]["payload"]["history_mode"] = "paginated"
    with open(path, "w") as f:
        for i, e in enumerate(entries):
            e["ordinal"] = i
            f.write(json.dumps(e) + "\n")


class TestShadowAppend:
    def test_paginated_rollout_gets_contiguous_ordinals(self, tmp_path):
        path = tmp_path / "rollout.jsonl"
        for rec in (_meta("paginated", 0), _user("hi", 1), _user("again", 2)):
            write_line(path, rec)
        get_adapter("codex").shadow_append(path, [_note("a"), _note("b")])
        assert [e["ordinal"] for e in read_jsonl(path)] == [0, 1, 2, 3, 4]

    def test_ordinal_is_top_level_not_in_payload(self, tmp_path):
        path = tmp_path / "rollout.jsonl"
        write_line(path, _meta("paginated", 0))
        get_adapter("codex").shadow_append(path, [_note("a")])
        last = read_jsonl(path)[-1]
        assert last["ordinal"] == 1
        assert "ordinal" not in last["payload"]

    def test_legacy_rollout_stays_ordinal_free(self, tmp_path):
        path = tmp_path / "rollout.jsonl"
        for rec in (_meta("legacy"), _user("hi")):
            write_line(path, rec)
        get_adapter("codex").shadow_append(path, [_note("a")])
        assert all("ordinal" not in e for e in read_jsonl(path))

    def test_sequence_continues_across_separate_appends(self, tmp_path):
        path = tmp_path / "rollout.jsonl"
        write_line(path, _meta("paginated", 0))
        adapter = get_adapter("codex")
        adapter.shadow_append(path, [_note("a")])
        adapter.shadow_append(path, [_note("b"), _note("c")])
        assert [e["ordinal"] for e in read_jsonl(path)] == [0, 1, 2, 3]

    def test_caller_entries_are_not_mutated(self, tmp_path):
        path = tmp_path / "rollout.jsonl"
        write_line(path, _meta("paginated", 0))
        entries = [_note("a")]
        get_adapter("codex").shadow_append(path, entries)
        assert "ordinal" not in entries[0]

    def test_last_record_longer_than_tail_window(self, tmp_path):
        """The record carrying the last ordinal may be a huge tool output;
        the lookup must find it however long it is."""
        path = tmp_path / "rollout.jsonl"
        write_line(path, _meta("paginated", 0))
        write_line(path, _user("x" * 600_000, 1))
        get_adapter("codex").shadow_append(path, [_note("a")])
        assert read_jsonl(path)[-1]["ordinal"] == 2


class TestSyncIntoPaginatedRollout:
    def test_synced_claude_turn_continues_codex_ordinals(self, env_factory):
        env = env_factory()
        _paginate(env.codex_shadow)
        loop, _ = env.loop()
        write_line(env.source_file, claude_user("please fix the bug"))
        write_line(env.source_file, claude_assistant(
            [{"type": "tool_use", "id": "c1", "name": "Bash",
              "input": {"command": "pytest -q"}}]))
        write_line(env.source_file, claude_assistant([{"type": "text", "text": "Fixed."}]))
        loop.drain()
        entries = read_jsonl(env.codex_shadow)
        assert len(entries) > 3
        assert [e["ordinal"] for e in entries] == list(range(len(entries)))

    def test_restart_keeps_ordinals_contiguous(self, env_factory):
        env = env_factory()
        _paginate(env.codex_shadow)
        loop, _ = env.loop()
        write_line(env.source_file, claude_user("first"))
        loop.drain()
        write_line(env.source_file, claude_user("second"))
        loop2, _ = env.loop()
        loop2.drain()
        entries = read_jsonl(env.codex_shadow)
        assert [e["ordinal"] for e in entries] == list(range(len(entries)))

    def test_legacy_shadow_sync_adds_no_ordinals(self, env_factory):
        env = env_factory()
        loop, _ = env.loop()
        write_line(env.source_file, claude_user("first"))
        loop.drain()
        assert all("ordinal" not in e for e in read_jsonl(env.codex_shadow))
