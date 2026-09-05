"""Shapes observed with Claude 2.1.261 / Codex 0.153.4 (September 2026)."""

import pytest

from conftest import claude_assistant, claude_user, write_line
from tandem.cli import _resolve_participants
from tandem.converter import ReferenceConverter
from tandem.doctor import validate_transcript
from tandem.events import SessionContext
from tandem.harness import ADAPTERS
from tandem.ops import drain_source, switch_session
from tandem.util import read_jsonl

# uuid-less session metadata claude 2.1.26x interleaves with the conversation
_CLAUDE_METADATA = [
    {"type": "atis-latch", "atis": ""},
    {"type": "bridge-session", "bridgeSessionId": "cse_probe",
     "lastSequenceNum": 0, "ownerAccountUuid": "account",
     "ownerOrganizationUuid": "organization"},
    {"type": "cost-state", "totalCostUSD": 0.01, "totalAPIDuration": 100,
     "totalAPIDurationWithoutRetries": 100, "totalToolDuration": 0,
     "totalLinesAdded": 0, "totalLinesRemoved": 0, "totalDuration": 150,
     "startTime": 1788624000000, "modelUsage": {}, "hasUnknownModelCost": False},
    {"type": "custom-title", "customTitle": "Renamed by the user"},
]


@pytest.mark.parametrize("record", _CLAUDE_METADATA, ids=lambda r: r["type"])
def test_claude_session_metadata_is_valid_and_not_conversation(env_factory, record):
    env = env_factory()
    sid = env.session.native_id("claude")
    record = dict(record, sessionId=sid)  # never mutate the parametrize table
    write_line(env.claude_shadow, record)
    assert validate_transcript("claude", env.claude_shadow, sid) == []
    ctx = SessionContext(tandem_id="probe", cwd=env.cwd, direction="claude->codex")
    assert ReferenceConverter().translate_entry(record, ctx.direction, ctx) == []
    assert ctx.turn_index == 0


def test_claude_model_fallback_block_is_dropped_not_text(env_factory):
    """2.1.26x records a mid-session model switch as an assistant content
    block; it carries no prose, so it must translate to nothing rather than
    to an assistant message or a translation failure."""
    env = env_factory()
    entry = claude_assistant(
        [{"type": "fallback", "from": {"model": "claude-fable-5"},
          "to": {"model": "claude-opus-4-8"}}], uuid="a-fallback")
    ctx = SessionContext(tandem_id="probe", cwd=env.cwd, direction="claude->codex")
    assert ReferenceConverter().translate_entry(entry, ctx.direction, ctx) == []


def test_unknown_uuid_carrying_entry_does_not_cascade_into_parent_problems(env_factory):
    """A future chain participant claude has not told us about yet is ONE
    problem, not one plus a dangling-parent complaint for every entry chained
    after it (which would bury the real cause under doctor's `... N more`)."""
    env = env_factory()
    sid = env.session.native_id("claude")
    write_line(env.claude_shadow, {"type": "novel-chain-entry", "uuid": "novel-1",
                                   "sessionId": sid})
    write_line(env.claude_shadow, {**claude_user("after the novelty", uuid="u-after"),
                                   "parentUuid": "novel-1"})
    problems = validate_transcript("claude", env.claude_shadow, sid)
    assert problems == ["line 1: unknown entry type 'novel-chain-entry'"]


def usage_record():
    usage = {"input_tokens": 100, "cached_input_tokens": 64,
             "cache_write_input_tokens": 0, "output_tokens": 10,
             "reasoning_output_tokens": 2, "total_tokens": 110}
    return {"timestamp": "2026-09-05T16:00:00.000Z", "type": "token_usage_record",
            "payload": {"thread_id": "thread", "turn_id": "turn",
                        "session_id": "thread", "root_turn_id": "turn",
                        "response_id": "response", "usage": usage,
                        "turn_token_usage": usage, "thread_token_usage": usage}}


def test_codex_accounting_record_is_valid_and_not_conversation(env_factory):
    env = env_factory()
    record = usage_record()
    write_line(env.codex_shadow, record)
    assert validate_transcript("codex", env.codex_shadow,
                               env.session.native_id("codex")) == []
    ctx = SessionContext(tandem_id="probe", cwd=env.cwd, direction="codex->claude")
    assert ReferenceConverter().translate_entry(record, ctx.direction, ctx) == []
    assert ctx.turn_index == 0


def _blocks(text_parts):
    """codex 0.153 writes roughly half of its tool outputs as a list of
    input_text blocks rather than one string (531 of ~5000 local outputs)."""
    return [{"type": "input_text", "text": t} for t in text_parts]


def test_codex_list_shaped_tool_output_parses_to_joined_text():
    adapter = ADAPTERS["codex"]
    ctx = SessionContext(tandem_id="probe", cwd="/tmp", direction="codex->claude")
    (ev,) = adapter.parse_entry(
        {"type": "response_item", "payload": {
            "type": "custom_tool_call_output", "call_id": "c1",
            "output": _blocks(["Script completed\nOutput:\n", "cobalt\n"])}},
        ctx)
    assert ev.kind == "tool_result"
    assert ev.output == "Script completed\nOutput:\ncobalt\n"
    assert "input_text" not in ev.output


def _codex_turn_with_tools(user_text, assistant_text):
    """One 0.153 turn: a code-mode `exec` call (list-shaped output), an
    exec_command whose list-shaped output reports a failure, the new
    accounting record, then the final answer."""
    return [
        {"timestamp": "t", "type": "event_msg",
         "payload": {"type": "task_started", "turn_id": "x"}},
        {"timestamp": "t", "type": "event_msg",
         "payload": {"type": "user_message", "message": user_text}},
        {"timestamp": "t", "type": "response_item", "payload": {
            "type": "custom_tool_call", "id": "tool-id", "status": "completed",
            "call_id": "call-exec", "name": "exec",
            "input": 'text(await tools.exec_command({cmd:"printf cobalt"}));',
            "internal_chat_message_metadata_passthrough": {}}},
        {"timestamp": "t", "type": "response_item", "payload": {
            "type": "custom_tool_call_output", "call_id": "call-exec",
            "output": _blocks(["Script completed\nOutput:\n", "cobalt"])}},
        {"timestamp": "t", "type": "response_item", "payload": {
            "type": "function_call", "name": "exec_command",
            "arguments": '{"cmd": "false"}', "call_id": "call-shell"}},
        {"timestamp": "t", "type": "response_item", "payload": {
            "type": "function_call_output", "call_id": "call-shell",
            "output": _blocks(["Process exited with code 1\nOutput:\n", "boom"])}},
        usage_record(),
        {"timestamp": "t", "type": "response_item",
         "payload": {"type": "message", "role": "assistant", "phase": "final_answer",
                     "content": [{"type": "output_text", "text": assistant_text}]}},
        {"timestamp": "t", "type": "event_msg",
         "payload": {"type": "task_complete", "turn_id": "x",
                     "last_agent_message": assistant_text}},
    ]


def _claude_tool_blocks(transcript):
    blocks = [b for e in read_jsonl(transcript)
              for b in (e.get("message") or {}).get("content") or []
              if isinstance(b, dict)]
    return ([b for b in blocks if b["type"] == "tool_use"],
            [b for b in blocks if b["type"] == "tool_result"])


def test_current_formats_survive_repeated_switches_without_echo(env_factory):
    env = env_factory()
    write_line(env.claude_shadow, claude_user("remember apricot", uuid="u-probe"))
    write_line(env.claude_shadow, {"type": "atis-latch", "atis": ""})
    write_line(env.claude_shadow, claude_assistant(
        [{"type": "text", "text": "apricot remembered"}], uuid="a-probe"))
    assert switch_session(env.store, env.session, "codex")[1] == []
    env.refresh()
    records = _codex_turn_with_tools("remember cobalt", "cobalt remembered")
    for record in records:
        write_line(env.codex_shadow, record)
    assert switch_session(env.store, env.session, "claude")[1] == []
    env.refresh()

    calls, results = _claude_tool_blocks(env.claude_shadow)
    assert [c["name"] for c in calls] == ["exec", "Bash"]
    assert [r["tool_use_id"] for r in results] == [c["id"] for c in calls]
    # code-mode call: JavaScript kept verbatim, list output flattened to text
    assert calls[0]["input"]["input"] == records[2]["payload"]["input"]
    assert results[0]["content"] == "Script completed\nOutput:\ncobalt"
    assert results[0]["is_error"] is False
    # exec_command with a list-shaped failure still maps to a failed Bash
    assert calls[1]["input"] == {"command": "false"}
    assert results[1]["content"] == "boom"
    assert results[1]["is_error"] is True

    before = env.codex_shadow.read_bytes(), env.claude_shadow.read_bytes()
    drain_source(env.store, env.session, "claude")
    assert switch_session(env.store, env.session, "codex")[1] == []
    assert before == (env.codex_shadow.read_bytes(), env.claude_shadow.read_bytes())
    for source, target in [("claude", "codex"), ("codex", "claude")]:
        assert env.store.get_cursor(env.session.tandem_id, source, target).failed_turns == 0


def test_installed_versions_start_without_format_warning(monkeypatch, capsys):
    monkeypatch.setattr("tandem.config.load_harnesses", lambda: ["claude", "codex"])
    for hid, version in [("claude", "2.1.261 (Claude Code)"),
                         ("codex", "codex-cli 0.153.4")]:
        monkeypatch.setattr(type(ADAPTERS[hid]), "detect_version",
                            lambda self, _v=version: _v)
    assert _resolve_participants()[0] == ["claude", "codex"]
    assert capsys.readouterr().err == ""
