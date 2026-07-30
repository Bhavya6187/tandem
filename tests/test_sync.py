"""Sync engine integration: fake active harness writes JSONL, engine appends
to a real shadow file with quarantine, placeholders and crash recovery."""

from tandem import paths
from tandem.util import read_jsonl

from conftest import (
    claude_assistant,
    claude_tool_result,
    claude_user,
    shadow_texts,
    write_line,
)


class TestSyncClaudeToCodex:
    def test_full_turn_synced(self, env_factory):
        env = env_factory()
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
        assert texts[-1] == "[via claude-code] Fixed."

        # the Bash call rides as a native function_call pair, not prose
        rollout = read_jsonl(env.codex_shadow)
        fcalls = [e["payload"] for e in rollout
                  if e.get("type") == "response_item"
                  and e["payload"].get("type") == "function_call"]
        assert any('pytest -q' in f["arguments"] for f in fcalls)
        outs = [e["payload"] for e in rollout
                if e.get("type") == "response_item"
                and e["payload"].get("type") == "function_call_output"]
        assert any("3 failed" in o["output"] for o in outs)

    def test_corrupt_line_quarantined_with_placeholder(self, env_factory):
        env = env_factory()
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

    def test_restart_resumes_without_duplicates(self, env_factory):
        env = env_factory()
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

    def test_crash_after_append_before_cursor_save(self, env_factory):
        """Intent recorded + shadow appended, then crash: restart must not
        double-append."""
        env = env_factory()
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

    def test_chain_appends_to_claude_shadow(self, env_factory):
        env = env_factory(active="codex")
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

    def test_leaf_rederived_from_file(self, env_factory):
        """If claude itself appended entries (it was active earlier), new
        synced entries chain onto claude's real leaf, not a stale one."""
        env = env_factory(active="codex")
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
