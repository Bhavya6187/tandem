"""tandem sub: shadow forking and the subagent execution op."""

import json
import time

from tandem import ops
from tandem.runner import await_codex_rollout
from tandem.util import read_jsonl

from conftest import claude_user, write_line


class TestForkShadow:
    def test_fork_copies_shadow_with_new_identity(self, env_factory):
        env = env_factory(active="claude")
        ops.fast_forward(env.store, env.session, "claude")
        write_line(env.claude_shadow, claude_user("context before fork"))

        fork_id, fork_path = ops.fork_shadow(env.store, env.session)

        assert fork_id != env.session.codex_session_id
        assert fork_path.name.endswith(f"-{fork_id}.jsonl")
        entries = read_jsonl(fork_path)
        meta = entries[0]
        assert meta["type"] == "session_meta"
        assert meta["payload"]["id"] == fork_id
        assert meta["payload"]["session_id"] == fork_id
        assert meta["payload"]["originator"] == "tandem-sub"
        assert meta["payload"]["model_provider"] == "openai"
        # the pre-fork drain landed the claude turn in the fork's history
        dump = json.dumps(entries)
        assert "context before fork" in dump

    def test_fork_is_structurally_resumable(self, env_factory):
        from tandem.doctor import validate_transcript

        env = env_factory(active="claude")
        ops.fast_forward(env.store, env.session, "claude")
        fork_id, fork_path = ops.fork_shadow(env.store, env.session)
        assert validate_transcript("codex", fork_path, fork_id) == []

    def test_fork_leaves_shadow_and_cursors_alone(self, env_factory):
        env = env_factory(active="claude")
        ops.fast_forward(env.store, env.session, "claude")
        before_bytes = env.codex_shadow.read_bytes()
        before_cursor = env.store.get_cursor(env.session.tandem_id, "codex")

        ops.fork_shadow(env.store, env.session)

        assert env.codex_shadow.read_bytes() == before_bytes
        after = env.store.get_cursor(env.session.tandem_id, "codex")
        assert after.byte_offset == before_cursor.byte_offset
        assert after.line_index == before_cursor.line_index

    def test_fork_is_not_adopted_as_a_freshly_minted_codex_session(self, env_factory):
        """A live fork sits in codex's sessions dir with the session cwd and a
        fresh mtime. If rollout discovery returned it, a concurrent fresh-codex
        launch would bind the pair's codex id to the subagent's throwaway."""
        env = env_factory(active="claude")
        ops.fast_forward(env.store, env.session, "claude")
        started = time.time()
        _, fork_path = ops.fork_shadow(env.store, env.session)

        assert await_codex_rollout(env.cwd, started, timeout=0) is None

        # control: a rollout codex itself wrote, same dir, is still discovered
        real_sid = "019faca1-0000-7000-8000-0000000000ff"
        real = fork_path.with_name(f"rollout-2026-07-31T00-00-00-{real_sid}.jsonl")
        write_line(real, {
            "timestamp": "t", "type": "session_meta",
            "payload": {"id": real_sid, "session_id": real_sid,
                        "cwd": env.cwd, "originator": "codex_cli"},
        })
        assert await_codex_rollout(env.cwd, started, timeout=0) == real
