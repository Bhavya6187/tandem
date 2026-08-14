"""Tailer + TailLoop integration: a fake harness appends JSONL while the
loop drains, persists its cursor, and survives restarts mid-stream."""

import json

import pytest

from tandem.events import SessionContext
from tandem.runner import TailLoop
from tandem.state import StateStore
from tandem.tailer import JsonlTailer, TranscriptTruncated


def write_line(path, obj):
    with open(path, "a") as f:
        f.write(json.dumps(obj) + "\n")


class TestJsonlTailer:
    def test_incremental_and_partial_lines(self, tmp_path):
        p = tmp_path / "t.jsonl"
        p.write_text('{"a":1}\n{"b":2}\n')
        tailer = JsonlTailer(p)
        lines = tailer.poll()
        assert [l.raw for l in lines] == [{"a": 1}, {"b": 2}]
        assert tailer.poll() == []

        # partial line: not consumed until the newline arrives
        with open(p, "a") as f:
            f.write('{"c":')
        assert tailer.poll() == []
        with open(p, "a") as f:
            f.write("3}\n")
        (line,) = tailer.poll()
        assert line.raw == {"c": 3}
        assert line.line_index == 2

    def test_invalid_json_line_yields_raw_none(self, tmp_path):
        p = tmp_path / "t.jsonl"
        p.write_text("not json at all\n")
        (line,) = JsonlTailer(p).poll()
        assert line.raw is None
        assert line.text == "not json at all"

    def test_truncation_detected(self, tmp_path):
        p = tmp_path / "t.jsonl"
        p.write_text('{"a":1}\n{"b":2}\n')
        tailer = JsonlTailer(p)
        tailer.poll()
        p.write_text('{"a":1}\n')
        with pytest.raises(TranscriptTruncated):
            tailer.poll()

    def test_missing_file_is_quiet(self, tmp_path):
        assert JsonlTailer(tmp_path / "nope.jsonl").poll() == []


class CollectSink:
    """Parses like the real sinks do (sinks own translation)."""

    def __init__(self, source="claude"):
        from tandem.harness import get_adapter

        self.adapter = get_adapter(source)
        self.events = []
        self.lines = []

    def handle(self, line, ctx, cursor):
        self.lines.append(line.line_index)
        if line.raw is not None:
            self.events.extend(self.adapter.parse_entry(line.raw, ctx))

    def close(self):
        pass


def claude_user(text, sid="s1"):
    return {
        "type": "user",
        "sessionId": sid,
        "uuid": "u-%s" % text[:8],
        "parentUuid": None,
        "timestamp": "2026-07-29T00:00:00.000Z",
        "message": {"role": "user", "content": text},
    }


def claude_assistant_text(text, sid="s1"):
    return {
        "type": "assistant",
        "sessionId": sid,
        "uuid": "a-%s" % text[:8],
        "timestamp": "2026-07-29T00:00:01.000Z",
        "message": {
            "role": "assistant",
            "model": "claude-fable-5",
            "content": [{"type": "text", "text": text}],
        },
    }


class TestTailLoop:
    def test_fake_harness_session_with_restart(self, tmp_path):
        transcript = tmp_path / "fake.jsonl"
        transcript.touch()
        db = tmp_path / "state.db"

        with StateStore(db_path=db) as store:
            session = store.create_session(str(tmp_path), "claude",
                                           ["claude", "codex"],
                                           {"claude": "s1", "codex": "x1"})
            sink = CollectSink()
            loop = TailLoop(store, session, "claude", "codex", transcript, sink)
            assert loop.drain() == 0

            write_line(transcript, claude_user("hello there"))
            write_line(transcript, claude_assistant_text("hi!"))
            assert loop.drain() == 2
            assert [e.kind for e in sink.events] == ["user_message", "assistant_message"]
            assert sink.events[0].turn_index == 1

        # restart: new store + loop resume from the persisted cursor
        with StateStore(db_path=db) as store:
            session = store.latest_session_for_cwd(str(tmp_path))
            write_line(transcript, claude_user("second prompt"))
            sink2 = CollectSink()
            loop2 = TailLoop(store, session, "claude", "codex", transcript, sink2)
            assert loop2.drain() == 1
            assert sink2.events[0].kind == "user_message"
            assert sink2.events[0].text == "second prompt"
            # turn index continued from the persisted context
            assert sink2.events[0].turn_index == 2
            assert sink2.lines == [2]

    def test_cursor_not_advanced_past_partial(self, tmp_path):
        transcript = tmp_path / "fake.jsonl"
        transcript.touch()
        db = tmp_path / "state.db"
        with StateStore(db_path=db) as store:
            session = store.create_session(str(tmp_path), "claude",
                                           ["claude", "codex"],
                                           {"claude": "s1", "codex": "x1"})
            sink = CollectSink()
            loop = TailLoop(store, session, "claude", "codex", transcript, sink)
            with open(transcript, "a") as f:
                f.write(json.dumps(claude_user("full")) + "\n")
                f.write('{"type": "user", "mess')  # torn write
            assert loop.drain() == 1
            saved = store.get_cursor(session.tandem_id, "claude", "codex")
            assert saved.line_index == 1
            with open(transcript, "a") as f:
                f.write('age": {"role": "user", "content": "later"}}\n')
            assert loop.drain() == 1
            assert sink.events[-1].text == "later"
