"""A scripted stand-in for `claude -p --input-format stream-json`.

Records its argv to $FAKE_ARGV_OUT, reads one user message from stdin, then
plays the scenario named by $FAKE_CLAUDE_SCENARIO:
  approve   (default) tool_use Bash -> can_use_tool -> tool_result -> DONE
  text      a text delta and a result, no tools
  question  AskUserQuestion -> can_use_tool with questions -> echoes the answer
  interrupt streams one delta, then waits for an interrupt control_request
  crash     prints init, writes "boom" to stderr, exits 3
"""
import json
import os
import sys

SID = "fake-claude-session"


def out(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def read():
    line = sys.stdin.readline()
    return json.loads(line) if line else None


def delta(text):
    out({"type": "stream_event", "event": {"type": "content_block_delta", "index": 0,
         "delta": {"type": "text_delta", "text": text}}, "session_id": SID})


def result(is_error=False, subtype="success", turns=1):
    out({"type": "result", "subtype": subtype, "is_error": is_error, "num_turns": turns,
         "total_cost_usd": 0.001, "session_id": SID, "result": "DONE" if not is_error else "error",
         "usage": {"input_tokens": 1, "output_tokens": 1}})


def main():
    if os.environ.get("FAKE_ARGV_OUT"):
        with open(os.environ["FAKE_ARGV_OUT"], "w") as f:
            json.dump(sys.argv[1:], f)
    scenario = os.environ.get("FAKE_CLAUDE_SCENARIO", "approve")
    first = read()
    assert first and first["type"] == "user", first
    prompt = first["message"]["content"][0]["text"]
    out({"type": "system", "subtype": "init", "session_id": SID, "model": "fake", "tools": ["Bash"],
         "cwd": os.getcwd(), "slash_commands": [], "claude_code_version": "0.0.0"})
    if scenario == "crash":
        sys.stderr.write("boom\n")
        sys.exit(3)
    if scenario == "text":
        delta("hello "); delta("world"); result(); return
    if scenario == "interrupt":
        delta("partial")
        while True:
            m = read()
            if m is None:
                return
            if m.get("type") == "control_request" and m["request"].get("subtype") == "interrupt":
                out({"type": "control_response", "response": {"subtype": "success",
                     "request_id": m["request_id"], "response": {}}})
                result(is_error=True, subtype="error_during_execution"); return
    if scenario == "question":
        inp = {"questions": [{"question": "Which color?", "header": "Color",
                              "options": [{"label": "red"}, {"label": "blue"}]}]}
        out({"type": "assistant", "message": {"role": "assistant", "content": [
             {"type": "tool_use", "id": "toolu_q", "name": "AskUserQuestion", "input": inp}]}, "session_id": SID})
        out({"type": "control_request", "request_id": "req-q", "request": {
             "subtype": "can_use_tool", "tool_name": "AskUserQuestion", "input": inp, "permission_suggestions": []}})
        m = read()
        answer = m["response"]["response"]["updatedInput"]["answers"]["Which color?"]
        out({"type": "user", "message": {"role": "user", "content": [
             {"tool_use_id": "toolu_q", "type": "tool_result", "content": json.dumps({"answers": {"Which color?": answer}}), "is_error": False}]}, "session_id": SID})
        delta(f"you chose {answer}"); result(turns=2); return
    # approve
    inp = {"command": "touch x.txt", "description": "make x"}
    out({"type": "assistant", "message": {"role": "assistant", "content": [
         {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": inp}]}, "session_id": SID})
    out({"type": "control_request", "request_id": "req-1", "request": {
         "subtype": "can_use_tool", "tool_name": "Bash", "input": inp,
         "permission_suggestions": [{"type": "addRules", "rules": [{"toolName": "Bash", "ruleContent": "touch x.txt"}],
                                     "behavior": "allow", "destination": "localSettings"}]}})
    m = read()
    resp = m["response"]["response"]
    with open(os.environ.get("FAKE_REPLY_OUT", os.devnull), "w") as f:
        json.dump(resp, f)
    if resp.get("behavior") == "allow":
        out({"type": "user", "message": {"role": "user", "content": [
             {"tool_use_id": "toolu_1", "type": "tool_result", "content": "ok\nline2\nline3", "is_error": False}]}, "session_id": SID})
        delta("DONE"); result(turns=2)
    else:
        out({"type": "user", "message": {"role": "user", "content": [
             {"tool_use_id": "toolu_1", "type": "tool_result", "content": "denied in tandem chat", "is_error": True}]}, "session_id": SID})
        delta("I could not run it."); result(turns=2)
    # a trailing prompt echo proves the client passed the text through verbatim
    with open(os.environ.get("FAKE_PROMPT_OUT", os.devnull), "w") as f:
        f.write(prompt)


if __name__ == "__main__":
    main()
