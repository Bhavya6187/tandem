"""Tool mapping layer + related shadow-file invariants."""

import json

from tandem import toolmap
from tandem.events import ToolCall, ToolResult


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
