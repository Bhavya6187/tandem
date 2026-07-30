"""Tool mapping layer + related shadow-file invariants."""

import json

from tandem import toolmap
from tandem.events import SessionContext, ToolCall, ToolResult
from tandem.harness import get_adapter


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
