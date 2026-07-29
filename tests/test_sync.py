"""Sync engine integration: fake active harness writes JSONL, engine appends
to a real shadow file with quarantine, placeholders and crash recovery."""

import json
from pathlib import Path

from tandem import paths
from tandem.events import SessionContext
from tandem.runner import TailLoop
from tandem.state import StateStore
from tandem.sync import SyncEngine
from tandem.util import read_jsonl


def write_line(path, obj=None, text=None):
    with open(path, "a") as f:
        f.write((text if text is not None else json.dumps(obj)) + "\n")


def claude_user(text):
    return {
        "type": "user",
        "uuid": f"u-{abs(hash(text)) % 10**8}",
        "timestamp": "2026-07-29T00:00:00.000Z",
        "message": {"role": "user", "content": text},
    }


def claude_assistant(blocks):
    return {
        "type": "assistant",
        "uuid": "a-1",
        "timestamp": "2026-07-29T00:00:01.000Z",
        "message": {"role": "assistant", "model": "claude-fable-5", "content": blocks},
    }


def claude_tool_result(call_id, output, structured=None):
    return {
        "type": "user",
        "uuid": "tr-1",
        "timestamp": "2026-07-29T00:00:02.000Z",
        "message": {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": call_id, "content": output}
            ],
        },
        "toolUseResult": structured or {"stdout": output, "stderr": ""},
    }


class Env:
    """Paired session with claude as the fake active source and a seeded
    codex shadow rollout, all under tmp homes."""

    def __init__(self, tmp_path, monkeypatch, active="claude"):
        monkeypatch.setenv("TANDEM_HOME", str(tmp_path / ".tandem"))
        monkeypatch.setenv("CODEX_HOME", str(tmp_path / ".codex"))
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / ".claude"))
        self.cwd = str(tmp_path / "proj")
        Path(self.cwd).mkdir()
        self.store = StateStore(db_path=tmp_path / ".tandem" / "state.db")
        from tandem.harness import get_adapter

        claude_sid = "11111111-1111-4111-8111-111111111111"
        codex_sid = "019faca1-0000-7000-8000-000000000001"
        self.session = self.store.create_session(self.cwd, active, claude_sid, codex_sid)
        ctx = SessionContext(
            tandem_id=self.session.tandem_id,
            cwd=self.cwd,
            direction="claude->codex",
            claude_session_id=claude_sid,
            codex_session_id=codex_sid,
        )
        # seed both files the way `tandem start` does
        self.codex_shadow = get_adapter("codex").create_shadow_transcript(
            self.cwd, codex_sid, ctx, "[tandem] seed"
        )
        self.claude_shadow = get_adapter("claude").create_shadow_transcript(
            self.cwd, claude_sid, ctx, "[tandem] seed"
        )
        cur = self.store.get_cursor(self.session.tandem_id, "codex")
        cur.pending["claude_leaf_uuid"] = ctx.claude_leaf_uuid
        self.store.save_cursor(cur)
        # the fake active transcript we tail (stands in for the real one)
        self.source_file = tmp_path / "active.jsonl"
        self.source_file.touch()

    def loop(self, source="claude"):
        engine = SyncEngine(self.store, self.session, source)
        return TailLoop(self.store, self.session, source, self.source_file, engine), engine


def shadow_texts(rollout_path):
    return [
        e["payload"]["content"][0]["text"]
        for e in read_jsonl(rollout_path)
        if e.get("type") == "response_item" and e["payload"].get("type") == "message"
    ]


class TestSyncClaudeToCodex:
    def test_full_turn_synced(self, tmp_path, monkeypatch):
        env = Env(tmp_path, monkeypatch)
        loop, _ = env.loop()

        write_line(env.source_file, claude_user("please fix the bug"))
        write_line(
            env.source_file,
            claude_assistant(
                [{"type": "tool_use", "id": "c1", "name": "Bash",
                  "input": {"command": "pytest -q"}}]
            ),
        )
        write_line(env.source_file, claude_tool_result("c1", "3 failed"))
        write_line(env.source_file, claude_assistant([{"type": "text", "text": "Fixed."}]))
        loop.drain()

        texts = shadow_texts(env.codex_shadow)
        assert texts[0] == "[tandem] seed"
        assert "[via claude-code] please fix the bug" in texts[1]
        assert any("ran `pytest -q`" in t and "3 failed" in t for t in texts)
        assert texts[-1] == "[via claude-code] Fixed."

    def test_corrupt_line_quarantined_with_placeholder(self, tmp_path, monkeypatch):
        env = Env(tmp_path, monkeypatch)
        loop, _ = env.loop()

        write_line(env.source_file, claude_user("turn one"))
        write_line(env.source_file, text="{this is not json")
        write_line(env.source_file, text='{"type": "user", "message": 42}')
        write_line(env.source_file, claude_assistant([{"type": "text", "text": "done"}]))
        loop.drain()

        texts = shadow_texts(env.codex_shadow)
        placeholders = [t for t in texts if t.startswith("[tandem: turn")]
        # two failures in the same turn -> ONE placeholder
        assert len(placeholders) == 1
        assert "could not be translated" in placeholders[0]
        # sync continued past the failure
        assert texts[-1] == "[via claude-code] done"

        qdir = paths.quarantine_dir(env.session.tandem_id)
        qfiles = sorted(p.name for p in qdir.iterdir())
        assert len(qfiles) == 2
        assert (qdir / qfiles[0]).read_text().startswith("{this is not json")

        cursor = env.store.get_cursor(env.session.tandem_id, "claude")
        assert cursor.failed_turns == 1

    def test_restart_resumes_without_duplicates(self, tmp_path, monkeypatch):
        env = Env(tmp_path, monkeypatch)
        loop, _ = env.loop()
        write_line(env.source_file, claude_user("first"))
        loop.drain()

        # simulate restart: fresh loop + engine from persisted cursor
        write_line(env.source_file, claude_user("second"))
        loop2, _ = env.loop()
        loop2.drain()

        texts = shadow_texts(env.codex_shadow)
        assert texts.count("[via claude-code] first") == 1
        assert texts.count("[via claude-code] second") == 1

    def test_crash_after_append_before_cursor_save(self, tmp_path, monkeypatch):
        """Intent recorded + shadow appended, then crash: restart must not
        double-append."""
        env = Env(tmp_path, monkeypatch)
        loop, engine = env.loop()
        write_line(env.source_file, claude_user("only once"))
        loop.drain()

        # forge the crash window: rewind the cursor to before the line and
        # re-add the intent, as if we died right after append_jsonl_fsync
        cursor = env.store.get_cursor(env.session.tandem_id, "claude")
        shadow_size = env.codex_shadow.stat().st_size
        cursor.byte_offset = 0
        cursor.line_index = 0
        cursor.turn_index = 0
        cursor.pending = {
            "pending_calls": {},
            "claude_leaf_uuid": None,
            "intent": {"line": 0, "pre_size": shadow_size - 1},
        }
        env.store.save_cursor(cursor)

        loop3, _ = env.loop()
        loop3.drain()
        texts = shadow_texts(env.codex_shadow)
        assert texts.count("[via claude-code] only once") == 1


class TestSyncCodexToClaude:
    def codex_lines(self):
        return [
            {"timestamp": "t", "type": "event_msg",
             "payload": {"type": "task_started", "turn_id": "x"}},
            {"timestamp": "t", "type": "event_msg",
             "payload": {"type": "user_message", "message": "hello from codex side"}},
            {"timestamp": "t", "type": "response_item",
             "payload": {"type": "message", "role": "assistant", "phase": "final_answer",
                         "content": [{"type": "output_text", "text": "All done."}]}},
        ]

    def test_chain_appends_to_claude_shadow(self, tmp_path, monkeypatch):
        env = Env(tmp_path, monkeypatch, active="codex")
        for obj in self.codex_lines():
            write_line(env.source_file, obj)
        loop, _ = env.loop(source="codex")
        loop.drain()

        entries = read_jsonl(env.claude_shadow)
        # seed + user + assistant
        assert len(entries) == 3
        assert entries[1]["message"]["content"] == "[via codex] hello from codex side"
        assert entries[2]["message"]["content"][0]["text"] == "[via codex] All done."
        # parentUuid chain is intact from the seed onward
        assert entries[1]["parentUuid"] == entries[0]["uuid"]
        assert entries[2]["parentUuid"] == entries[1]["uuid"]

    def test_leaf_rederived_from_file(self, tmp_path, monkeypatch):
        """If claude itself appended entries (it was active earlier), new
        synced entries chain onto claude's real leaf, not a stale one."""
        env = Env(tmp_path, monkeypatch, active="codex")
        # claude appended its own entry after the seed
        write_line(
            env.claude_shadow,
            {"type": "assistant", "uuid": "claude-own-leaf", "parentUuid": "seed",
             "message": {"role": "assistant", "content": [{"type": "text", "text": "hi"}]}},
        )
        for obj in self.codex_lines():
            write_line(env.source_file, obj)
        loop, _ = env.loop(source="codex")
        loop.drain()
        entries = read_jsonl(env.claude_shadow)
        synced_user = next(e for e in entries if e.get("type") == "user" and "codex" in str(e.get("message")))
        assert synced_user["parentUuid"] == "claude-own-leaf"
