"""Tool mapping layer + related shadow-file invariants."""

import json

from tandem import toolmap
from tandem.converter import ReferenceConverter, TranslationError
from tandem.events import (
    AssistantMessage,
    SessionContext,
    ToolCall,
    ToolResult,
    UserMessage,
)
from tandem.harness import get_adapter
from tandem.util import read_jsonl

from conftest import claude_assistant, claude_tool_result, claude_user, write_line


class TestShadowRolloutMeta:
    def test_model_provider_written(self, env_factory):
        env = env_factory()
        meta = json.loads(env.codex_shadow.read_text().splitlines()[0])
        assert meta["type"] == "session_meta"
        # codex >= 0.145 interactive thread/resume rejects rollouts without
        # this ("Model provider `` not found", -32600)
        assert meta["payload"]["model_provider"] == "openai"


def call(tool, arguments, source="claude", call_id="c1"):
    return ToolCall(source=source, call_id=call_id, tool=tool, arguments=arguments)


def result(output, source="claude", call_id="c1", structured=None, is_error=False):
    return ToolResult(source=source, call_id=call_id, output=output,
                      structured=structured, is_error=is_error)


def _ctx(direction="claude->codex"):
    return SessionContext(
        tandem_id="t1", cwd="/p", direction=direction,
        claude_session_id="11111111-1111-4111-8111-111111111111",
        codex_session_id="019faca1-0000-7000-8000-000000000001",
        claude_leaf_uuid="seed",
    )


class TestBashExecCommand:
    def test_bash_to_exec_command(self):
        c, r = toolmap.map_pair(
            call("Bash", {"command": "pytest -q"}),
            result("3 failed", structured={"stdout": "3 failed", "stderr": ""}),
            "codex",
        )
        assert c.tool == "exec_command"
        assert c.arguments == {"cmd": "pytest -q"}
        assert c.call_id == "c1"          # call ids ride verbatim
        assert r.output == "3 failed"     # no fake codex exit-code header

    def test_exec_command_to_bash_strips_header(self):
        raw = ("Chunk ID: 0\nProcess exited with code 1\nWall time: 0.1 s\n"
               "Original token count: 5\nOutput:\n3 failed")
        c, r = toolmap.map_pair(
            call("exec_command", '{"cmd": "pytest -q"}', source="codex"),
            result(raw, source="codex"),
            "claude",
        )
        assert c.tool == "Bash"
        assert c.arguments == {"command": "pytest -q"}
        assert r.output == "3 failed"
        assert r.is_error is True
        assert r.structured == {"stdout": "3 failed", "exitCode": 1}

    def test_exec_command_exit_zero_not_error(self):
        raw = "Process exited with code 0\nOutput:\nok"
        _, r = toolmap.map_pair(
            call("exec_command", '{"cmd": "true"}', source="codex"),
            result(raw, source="codex"), "claude",
        )
        assert r.is_error is False
        assert r.structured == {"stdout": "ok", "exitCode": 0}


class TestPassThrough:
    def test_unknown_claude_tool_rides_verbatim_to_codex(self):
        c, r = toolmap.map_pair(
            call("AskUserQuestion", {"questions": [{"q": "a?"}]}),
            result("answer: a"), "codex",
        )
        assert c.tool == "AskUserQuestion"
        assert c.arguments == {"questions": [{"q": "a?"}]}
        assert r.output == "answer: a"

    def test_unknown_codex_tool_rides_verbatim_to_claude(self):
        c, r = toolmap.map_pair(
            call("write_stdin", '{"chars": "y\\n"}', source="codex"),
            result("ok", source="codex"), "claude",
        )
        assert c.tool == "write_stdin"
        assert c.arguments == {"chars": "y\n"}   # json args parsed for tool_use.input
        assert r.structured == {"stdout": "ok"}  # default toolUseResult

    def test_unparseable_str_args_wrapped_for_claude(self):
        c, _ = toolmap.map_pair(
            call("mystery", "not json", source="codex"),
            result("ok", source="codex"), "claude",
        )
        assert c.arguments == {"input": "not json"}

    def test_mapping_never_raises(self):
        # Bash with pathological arguments degrades to pass-through
        c, _ = toolmap.map_pair(call("Bash", "i am not a dict"), result("x"), "codex")
        assert c.tool == "Bash"


class TestClaudeToCodexTier1:
    def test_read_plain(self):
        c, r = toolmap.map_pair(
            call("Read", {"file_path": "/a/b.py"}),
            result("1\timport os"), "codex",
        )
        assert c.tool == "exec_command"
        assert c.arguments == {"cmd": "cat -n /a/b.py"}
        assert r.output == "1\timport os"

    def test_read_ranged(self):
        c, _ = toolmap.map_pair(
            call("Read", {"file_path": "/a/b.py", "offset": 10, "limit": 5}),
            result("10\tx"), "codex",
        )
        assert c.arguments == {"cmd": "cat -n /a/b.py | sed -n '10,14p'"}

    def test_write_create_becomes_add_file_patch(self):
        c, _ = toolmap.map_pair(
            call("Write", {"file_path": "/p/hello.txt", "content": "hi\nthere"}),
            result("ok", structured={"type": "create", "filePath": "/p/hello.txt",
                                     "content": "hi\nthere"}),
            "codex",
        )
        assert c.tool == "apply_patch"
        assert isinstance(c.arguments, str)   # str => custom_tool_call
        assert c.arguments == (
            "*** Begin Patch\n*** Add File: /p/hello.txt\n+hi\n+there\n*** End Patch"
        )

    def test_write_overwrite_falls_back(self):
        c, _ = toolmap.map_pair(
            call("Write", {"file_path": "/p/x.txt", "content": "new"}),
            result("ok", structured={"type": "update", "filePath": "/p/x.txt"}),
            "codex",
        )
        assert c.tool == "Write"   # Tier 2: honesty rule

    def test_edit_becomes_update_file_patch(self):
        c, _ = toolmap.map_pair(
            call("Edit", {"file_path": "/p/a.py", "old_string": "x = 1",
                          "new_string": "x = 2"}),
            result("ok"), "codex",
        )
        assert c.tool == "apply_patch"
        assert c.arguments == (
            "*** Begin Patch\n*** Update File: /p/a.py\n-x = 1\n+x = 2\n*** End Patch"
        )

    def test_edit_replace_all_falls_back(self):
        c, _ = toolmap.map_pair(
            call("Edit", {"file_path": "/p/a.py", "old_string": "x",
                          "new_string": "y", "replace_all": True}),
            result("ok"), "codex",
        )
        assert c.tool == "Edit"

    def test_grep_and_glob(self):
        # Grep's schema defaults output_mode to files_with_matches, and a
        # transcript records only the args actually passed: omitted => -l
        c, _ = toolmap.map_pair(
            call("Grep", {"pattern": "def main", "path": "src"}),
            result("src/a.py"), "codex",
        )
        assert c.arguments == {"cmd": "rg -l 'def main' src"}
        c, _ = toolmap.map_pair(
            call("Glob", {"pattern": "**/*.py"}), result("a.py"), "codex",
        )
        assert c.arguments == {"cmd": "rg --files -g '**/*.py'"}

    def test_grep_content_mode_uses_line_numbers(self):
        c, _ = toolmap.map_pair(
            call("Grep", {"pattern": "def main", "path": "src",
                          "output_mode": "content"}),
            result("src/a.py:1:def main"), "codex",
        )
        assert c.arguments == {"cmd": "rg -n 'def main' src"}

    def test_grep_count_mode_falls_back(self):
        # 'src/a.py:3' means "3 matches", which `rg -n` output would read as
        # line 3: honesty rule, Tier 2
        c, _ = toolmap.map_pair(
            call("Grep", {"pattern": "def main", "path": "src",
                          "output_mode": "count"}),
            result("src/a.py:3"), "codex",
        )
        assert c.tool == "Grep"

    def test_grep_head_limit_falls_back(self):
        # truncated output under a command implying the full result
        c, _ = toolmap.map_pair(
            call("Grep", {"pattern": "def main", "output_mode": "content",
                          "head_limit": 10}),
            result("src/a.py:1:def main"), "codex",
        )
        assert c.tool == "Grep"

    def test_todowrite_becomes_update_plan(self):
        c, _ = toolmap.map_pair(
            call("TodoWrite", {"todos": [
                {"content": "fix bug", "status": "in_progress", "activeForm": "Fixing"},
                {"content": "add test", "status": "pending", "activeForm": "Adding"},
            ]}),
            result("Todos have been modified successfully"), "codex",
        )
        assert c.tool == "update_plan"
        assert c.arguments == {"plan": [
            {"step": "fix bug", "status": "in_progress"},
            {"step": "add test", "status": "pending"},
        ]}


ADD_PATCH = "*** Begin Patch\n*** Add File: /p/hello.txt\n+hi\n+there\n*** End Patch"
UPDATE_PATCH = ("*** Begin Patch\n*** Update File: /p/a.py\n@@\n ctx\n-x = 1\n+x = 2\n"
                "*** End Patch")
MULTI_PATCH = ("*** Begin Patch\n*** Add File: /p/a\n+1\n*** Add File: /p/b\n+2\n"
               "*** End Patch")
TWO_HUNK_PATCH = ("*** Begin Patch\n*** Update File: /p/a.py\n@@\n ctx\n-x = 1\n+x = 2\n"
                  "@@\n far\n-y = 1\n+y = 2\n*** End Patch")
MOVE_PATCH = ("*** Begin Patch\n*** Update File: /p/old.py\n*** Move to: /p/new.py\n"
              "@@\n ctx\n-x = 1\n+x = 2\n*** End Patch")


class TestCodexToClaudeTier1:
    def test_apply_patch_add_becomes_write(self):
        c, r = toolmap.map_pair(
            call("apply_patch", ADD_PATCH, source="codex"),
            result("Done!", source="codex"), "claude",
        )
        assert c.tool == "Write"
        assert c.arguments == {"file_path": "/p/hello.txt", "content": "hi\nthere"}
        assert r.structured == {"type": "create", "filePath": "/p/hello.txt",
                                "content": "hi\nthere"}

    def test_apply_patch_update_becomes_edit(self):
        c, r = toolmap.map_pair(
            call("apply_patch", UPDATE_PATCH, source="codex"),
            result("Done!", source="codex"), "claude",
        )
        assert c.tool == "Edit"
        assert c.arguments == {"file_path": "/p/a.py", "old_string": "ctx\nx = 1",
                               "new_string": "ctx\nx = 2"}
        assert r.structured == {"filePath": "/p/a.py", "oldString": "ctx\nx = 1",
                                "newString": "ctx\nx = 2"}

    def test_failed_apply_patch_maps_to_an_errored_edit(self):
        # the codex adapter enriches the result with patch_apply_end's
        # {success, changes}; dropping it would render a failed apply as a
        # clean Edit
        c, r = toolmap.map_pair(
            call("apply_patch", UPDATE_PATCH, source="codex"),
            result("Failed to apply patch", source="codex",
                   structured={"success": False, "changes": {}}),
            "claude",
        )
        assert c.tool == "Edit"
        assert r.is_error is True
        assert r.structured["success"] is False
        assert r.structured["changes"] == {}
        assert r.structured["filePath"] == "/p/a.py"  # synthesized keys survive

    def test_successful_apply_patch_enrichment_rides_along(self):
        changes = {"/p/a.py": {"type": "update"}}
        c, r = toolmap.map_pair(
            call("apply_patch", UPDATE_PATCH, source="codex"),
            result("Done!", source="codex",
                   structured={"success": True, "changes": changes}),
            "claude",
        )
        assert c.tool == "Edit"
        assert r.is_error is False
        assert r.structured["success"] is True
        assert r.structured["changes"] == changes

    def test_failed_add_patch_maps_to_an_errored_write(self):
        c, r = toolmap.map_pair(
            call("apply_patch", ADD_PATCH, source="codex"),
            result("nope", source="codex",
                   structured={"success": False, "changes": {}}),
            "claude",
        )
        assert c.tool == "Write"
        assert r.is_error is True
        assert r.structured["success"] is False
        assert r.structured["type"] == "create"

    def test_multi_file_patch_falls_back(self):
        c, _ = toolmap.map_pair(
            call("apply_patch", MULTI_PATCH, source="codex"),
            result("Done!", source="codex"), "claude",
        )
        assert c.tool == "apply_patch"
        assert c.arguments == {"input": MULTI_PATCH}

    def test_multi_hunk_update_falls_back(self):
        # two hunks touch non-adjacent regions; splicing them into one
        # old_string would assert text that appears nowhere in the file
        c, _ = toolmap.map_pair(
            call("apply_patch", TWO_HUNK_PATCH, source="codex"),
            result("Done!", source="codex"), "claude",
        )
        assert c.tool == "apply_patch"
        assert c.arguments == {"input": TWO_HUNK_PATCH}

    def test_move_to_update_falls_back(self):
        # an Edit record would swallow the rename and name a path that no
        # longer exists: honesty rule, Tier 2
        c, _ = toolmap.map_pair(
            call("apply_patch", MOVE_PATCH, source="codex"),
            result("Done!", source="codex"), "claude",
        )
        assert c.tool == "apply_patch"
        assert c.arguments == {"input": MOVE_PATCH}

    def test_update_plan_becomes_todowrite(self):
        c, r = toolmap.map_pair(
            call("update_plan",
                 '{"plan": [{"step": "fix bug", "status": "in_progress"}]}',
                 source="codex"),
            result("Plan updated", source="codex"), "claude",
        )
        assert c.tool == "TodoWrite"
        assert c.arguments == {"todos": [
            {"content": "fix bug", "status": "in_progress", "activeForm": "fix bug"},
        ]}
        assert r.output == "Plan updated"


class TestCodexToolRendering:
    def test_function_call_pair(self):
        ctx = _ctx()
        entries = get_adapter("codex").render_events([
            call("exec_command", {"cmd": "ls"}, call_id="c9"),
            result("a.py", call_id="c9"),
        ], ctx)
        assert [e["payload"]["type"] for e in entries] == [
            "function_call", "function_call_output"]
        assert entries[0]["payload"]["name"] == "exec_command"
        assert entries[0]["payload"]["arguments"] == '{"cmd": "ls"}'
        assert entries[0]["payload"]["call_id"] == "c9"
        assert entries[1]["payload"] == {
            "type": "function_call_output", "call_id": "c9", "output": "a.py"}
        assert all(e["type"] == "response_item" for e in entries)

    def test_custom_tool_call_pair(self):
        ctx = _ctx()
        entries = get_adapter("codex").render_events([
            call("apply_patch", "*** Begin Patch\n*** End Patch", call_id="c2"),
            result("Done!", call_id="c2"),
        ], ctx)
        assert [e["payload"]["type"] for e in entries] == [
            "custom_tool_call", "custom_tool_call_output"]
        assert entries[0]["payload"]["input"].startswith("*** Begin Patch")

    def test_mixed_pairs_interleave_through_the_converter(self):
        """The renderer decides output type from a batch-local custom_ids set,
        so a function_call and a custom_tool_call outstanding at once must
        never cross-label their outputs. Pair-at-result emission is what makes
        that safe: each mapped pair is rendered in its own batch, in the order
        the results arrived."""
        conv = ReferenceConverter()
        ctx = _ctx()
        entries: list = []
        for raw in [
            claude_assistant([
                {"type": "tool_use", "id": "c1", "name": "Bash",
                 "input": {"command": "ls"}},
                {"type": "tool_use", "id": "c2", "name": "Write",
                 "input": {"file_path": "/p/x.txt", "content": "hi"}},
            ]),
            # results come back out of call order, Write first
            claude_tool_result("c2", "File created", structured={
                "type": "create", "filePath": "/p/x.txt", "content": "hi"}),
            claude_tool_result("c1", "x.txt"),
        ]:
            got = conv.translate_entry(raw, "claude->codex", ctx)
            assert not isinstance(got, TranslationError), got
            entries.extend(got)

        # the two tool_use blocks alone emit nothing: both calls are stashed
        # until their results arrive
        assert [e["payload"]["type"] for e in entries] == [
            "custom_tool_call", "custom_tool_call_output",
            "function_call", "function_call_output",
        ]
        patch_call, patch_out, exec_call, exec_out = (e["payload"] for e in entries)
        assert patch_call["name"] == "apply_patch"
        assert patch_call["call_id"] == "c2"
        assert patch_out["call_id"] == "c2"
        assert exec_call["name"] == "exec_command"
        assert exec_call["call_id"] == "c1"
        assert exec_out["call_id"] == "c1"
        assert ctx.pending_calls == {}


class TestClaudeToolRendering:
    def test_tool_pair_and_message_id_runs(self):
        ctx = _ctx("codex->claude")
        adapter = get_adapter("claude")
        entries = adapter.render_events([
            AssistantMessage(source="codex", text="Looking around."),
            call("Bash", {"command": "ls"}, source="codex", call_id="c1"),
            result("a.py", source="codex", call_id="c1",
                   structured={"stdout": "a.py", "exitCode": 0}),
            call("Bash", {"command": "cat a.py"}, source="codex", call_id="c2"),
            result("print(1)", source="codex", call_id="c2",
                   structured={"stdout": "print(1)", "exitCode": 0}),
        ], ctx)

        types = [e["type"] for e in entries]
        assert types == ["assistant", "assistant", "user", "assistant", "user"]

        text_msg, tool1, res1, tool2, res2 = entries
        # text and the first tool_use share one API message id; the
        # tool_result (a user entry) ends the run, so the next call gets a
        # fresh id
        assert tool1["message"]["id"] == text_msg["message"]["id"]
        assert tool2["message"]["id"] != tool1["message"]["id"]

        block = tool1["message"]["content"][0]
        assert block == {"type": "tool_use", "id": "c1", "name": "Bash",
                         "input": {"command": "ls"}}
        assert tool1["message"]["stop_reason"] == "tool_use"

        rblock = res1["message"]["content"][0]
        assert rblock == {"type": "tool_result", "tool_use_id": "c1",
                          "content": "a.py", "is_error": False}
        assert res1["toolUseResult"] == {"stdout": "a.py", "exitCode": 0}

        # uuid/parentUuid chain is intact across the mixed entries
        for prev, cur in zip(entries, entries[1:]):
            assert cur["parentUuid"] == prev["uuid"]
        assert ctx.claude_leaf_uuid == entries[-1]["uuid"]

    def test_render_uses_last_real_claude_model(self):
        # claude --resume rejects "<synced>" as a session model; rendered
        # entries carry the model claude last used when one is known
        ctx = _ctx("codex->claude")
        ctx.claude_model = "claude-fable-5"
        entries = get_adapter("claude").render_events([
            AssistantMessage(source="codex", text="hi"),
            call("Bash", {"command": "ls"}, source="codex", call_id="c9"),
        ], ctx)
        assert all(e["message"]["model"] == "claude-fable-5" for e in entries)

    def test_is_error_flag_rides(self):
        ctx = _ctx("codex->claude")
        entries = get_adapter("claude").render_events([
            call("Bash", {"command": "false"}, source="codex", call_id="c3"),
            result("", source="codex", call_id="c3", is_error=True,
                   structured={"stdout": "", "exitCode": 1}),
        ], ctx)
        assert entries[1]["message"]["content"][0]["is_error"] is True

    def test_user_message_also_ends_the_run(self):
        ctx = _ctx("codex->claude")
        entries = get_adapter("claude").render_events([
            AssistantMessage(source="codex", text="one"),
            AssistantMessage(source="codex", text="two"),
            UserMessage(source="user", text="go on"),
            AssistantMessage(source="codex", text="three"),
        ], ctx)
        a1, a2, _user, a3 = entries
        assert a1["message"]["id"] == a2["message"]["id"]
        assert a3["message"]["id"] != a1["message"]["id"]

    def test_placeholder_also_ends_the_run(self):
        # a quarantine placeholder is a rendered user-side entry: an id must
        # never straddle it, or regrouping strands a tool_use from its result
        ctx = _ctx("codex->claude")
        adapter = get_adapter("claude")
        first = adapter.render_events([AssistantMessage(source="codex", text="one")], ctx)
        adapter.render_placeholder("[tandem: could not translate turn 3]", ctx)
        second = adapter.render_events([AssistantMessage(source="codex", text="two")], ctx)
        assert second[0]["message"]["id"] != first[0]["message"]["id"]


class TestDangleFlush:
    def _pendings(self, env):
        write_line(env.source_file, claude_user("do the thing"))
        write_line(env.source_file, claude_assistant(
            [{"type": "tool_use", "id": "dangling-1", "name": "Bash",
              "input": {"command": "sleep 999"}}]
        ))

    def test_flush_closes_pending_call(self, env_factory):
        env = env_factory()
        loop, engine = env.loop()
        self._pendings(env)
        loop.drain()
        assert loop.ctx.pending_calls  # the call is stashed, unpaired

        n = engine.flush_dangling(loop.ctx, loop.cursor)
        assert n == 2
        assert loop.ctx.pending_calls == {}

        rollout = read_jsonl(env.codex_shadow)
        pl = [e["payload"] for e in rollout if e.get("type") == "response_item"]
        fc = [p for p in pl if p.get("type") == "function_call"]
        out = [p for p in pl if p.get("type") == "function_call_output"]
        assert fc[-1]["name"] == "exec_command"
        assert fc[-1]["call_id"] == "dangling-1"
        assert out[-1] == {"type": "function_call_output",
                           "call_id": "dangling-1",
                           "output": "(tool result not recorded)"}

    def test_flush_noop_when_nothing_pending(self, env_factory):
        env = env_factory()
        loop, engine = env.loop()
        write_line(env.source_file, claude_user("hi"))
        loop.drain()
        before = env.codex_shadow.read_text()
        assert engine.flush_dangling(loop.ctx, loop.cursor) == 0
        assert env.codex_shadow.read_text() == before

    def test_drain_source_flag_flushes(self, env_factory, monkeypatch):
        from tandem import ops
        env = env_factory()
        self._pendings(env)
        # route drain_source at the stand-in transcript
        monkeypatch.setattr(
            ops, "source_transcript", lambda session, source: env.source_file
        )
        ops.drain_source(env.store, env.session, "claude", flush_dangling=True)
        rollout = read_jsonl(env.codex_shadow)
        outs = [e["payload"]["output"] for e in rollout
                if e.get("type") == "response_item"
                and e["payload"].get("type") == "function_call_output"]
        assert "(tool result not recorded)" in outs

    def test_drain_source_without_flag_leaves_the_call_open(
        self, env_factory, monkeypatch
    ):
        # `tandem sync` drains without flush_dangling: a call whose result has
        # not landed yet is still live, so it must stay pending in the
        # persisted cursor and get no placeholder. Flushing here would close a
        # call the source is about to answer itself.
        from tandem import ops
        env = env_factory()
        self._pendings(env)
        monkeypatch.setattr(
            ops, "source_transcript", lambda session, source: env.source_file
        )
        ops.drain_source(env.store, env.session, "claude")  # default: no flush

        cur = env.store.get_cursor(env.session.tandem_id, "claude", "codex")
        assert "dangling-1" in cur.pending["pending_calls"]
        rollout = read_jsonl(env.codex_shadow)
        payloads = [e["payload"] for e in rollout if e.get("type") == "response_item"]
        assert not [p for p in payloads if p.get("call_id") == "dangling-1"]
        assert toolmap.PLACEHOLDER_OUTPUT not in env.codex_shadow.read_text()

    def test_landed_flush_intent_is_not_replayed(self, env_factory):
        # crash after the flush append but before the cursor cleared: the
        # pending calls it closed must not be flushed a second time
        env = env_factory()
        loop, engine = env.loop()
        self._pendings(env)
        loop.drain()
        cur = env.store.get_cursor(env.session.tandem_id, "claude", "codex")
        assert cur.pending["pending_calls"]
        cur.pending["intent"] = {"line": -1, "pre_size": 0}  # file grew => landed
        env.store.save_cursor(cur)

        before = env.codex_shadow.read_text()
        loop2, engine2 = env.loop()
        assert loop2.ctx.pending_calls  # restored from the cursor
        assert engine2.flush_dangling(loop2.ctx, loop2.cursor) == 0
        assert env.codex_shadow.read_text() == before
        reread = env.store.get_cursor(env.session.tandem_id, "claude", "codex")
        assert reread.pending["pending_calls"] == {}
        assert "intent" not in reread.pending

    def test_unlanded_flush_intent_is_replayed(self, env_factory):
        # crash before the append: the calls are still open, so flush again
        env = env_factory()
        loop, engine = env.loop()
        self._pendings(env)
        loop.drain()
        size = env.codex_shadow.stat().st_size
        cur = env.store.get_cursor(env.session.tandem_id, "claude", "codex")
        cur.pending["intent"] = {"line": -1, "pre_size": size}  # did not grow
        env.store.save_cursor(cur)

        loop2, engine2 = env.loop()
        assert engine2.flush_dangling(loop2.ctx, loop2.cursor) == 2
        assert env.codex_shadow.stat().st_size > size
        reread = env.store.get_cursor(env.session.tandem_id, "claude", "codex")
        assert reread.pending["pending_calls"] == {}
        assert "intent" not in reread.pending
