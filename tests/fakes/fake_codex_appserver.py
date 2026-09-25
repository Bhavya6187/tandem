"""A scripted stand-in for `codex app-server` (JSON-RPC 2.0, one message per line).

Records argv to $FAKE_ARGV_OUT and every request's params (one JSON object
per line: {"method":…, "params":…}) to $FAKE_PARAMS_OUT; the approval reply
goes to $FAKE_REPLY_OUT. Scenario from $FAKE_CODEX_SCENARIO:
  approve    (default) resume -> turn -> command approval -> DONE
  fresh      like approve but expects thread/start (no threadId)
  freshfail  thread/start succeeds, then turn/start fails
  lock       thread/resume fails with "already has an active writer"
  crash      exits 2 after turn/start with "kaboom" on stderr
  interrupt  streams a delta, waits for turn/interrupt, completes as interrupted
  question   asks a requestUserInput question and echoes the answer
  thinturn   like approve but the turn/start result carries only {"turn": {"id"}}
  filechange asks item/fileChange/requestApproval (reply id 8), then DONE
  permission asks item/permissions/requestApproval (reply id 9), then DONE
"""
import json
import os
import sys

T = "01a0774c-fdd8-7937-a3dd-ba15c718b01a"
TURN = "turn-1"


def out(obj):
    sys.stdout.write(json.dumps(obj) + "\n"); sys.stdout.flush()


def notify(method, params):
    out({"jsonrpc": "2.0", "method": method, "params": params})


def read():
    line = sys.stdin.readline()
    return json.loads(line) if line else None


def record(m):
    p = os.environ.get("FAKE_PARAMS_OUT")
    if p:
        with open(p, "a") as f:
            f.write(json.dumps({"method": m.get("method"), "params": m.get("params")}) + "\n")


def item(item_type, **fields):
    return {"type": item_type, "id": fields.pop("id", "call-1"), **fields}


def reply_out(m):
    with open(os.environ.get("FAKE_REPLY_OUT", os.devnull), "w") as f:
        json.dump(m.get("result") or {"error": m.get("error")}, f)


def finish(thread_id, text="DONE"):
    notify("item/started", {"threadId": thread_id, "turnId": TURN, "startedAtMs": 3,
                            "item": item("agentMessage", id="msg-1", text="", phase="final_answer")})
    notify("item/agentMessage/delta", {"threadId": thread_id, "turnId": TURN, "itemId": "msg-1", "delta": text})
    notify("item/completed", {"threadId": thread_id, "turnId": TURN, "completedAtMs": 4,
                              "item": item("agentMessage", id="msg-1", text=text, phase="final_answer")})
    notify("turn/completed", {"threadId": thread_id, "turn": {
        "id": TURN, "items": [], "itemsView": "summary", "status": "completed",
        "error": None, "startedAt": 1, "completedAt": 5, "durationMs": 4}})


def main():
    if os.environ.get("FAKE_ARGV_OUT"):
        with open(os.environ["FAKE_ARGV_OUT"], "w") as f:
            json.dump(sys.argv[1:], f)
    scenario = os.environ.get("FAKE_CODEX_SCENARIO", "approve")
    thread_id = T
    while True:
        m = read()
        if m is None:
            return
        record(m)
        meth, rid = m.get("method"), m.get("id")
        if meth == "initialize":
            out({"jsonrpc": "2.0", "id": rid, "result": {"userAgent": "fake", "codexHome": "/x", "platformFamily": "unix", "platformOs": "macos"}})
        elif meth == "initialized":
            pass
        elif meth == "thread/resume":
            if scenario == "lock":
                out({"jsonrpc": "2.0", "id": rid, "error": {"code": -32600, "message": f"thread {m['params']['threadId']} already has an active writer"}})
                continue
            thread_id = m["params"]["threadId"]
            out({"jsonrpc": "2.0", "id": rid, "result": {"thread": {"id": thread_id, "cwd": m["params"].get("cwd"), "turns": []}, "model": "gpt-fake"}})
        elif meth == "thread/start":
            thread_id = "thread-new"
            out({"jsonrpc": "2.0", "id": rid, "result": {"thread": {"id": thread_id, "cwd": m["params"].get("cwd"), "turns": []}, "model": "gpt-fake"}})
        elif meth == "turn/start":
            if scenario == "freshfail":
                out({"jsonrpc": "2.0", "id": rid,
                     "error": {"code": -32000, "message": "model unavailable"}})
                continue
            if scenario == "thinturn":
                out({"jsonrpc": "2.0", "id": rid, "result": {"turn": {"id": TURN}}})
            else:
                out({"jsonrpc": "2.0", "id": rid, "result": {"turn": {"id": TURN, "items": [], "itemsView": "notLoaded", "status": "inProgress", "error": None, "startedAt": None, "completedAt": None, "durationMs": None}}})
            if scenario == "crash":
                sys.stderr.write("kaboom\n"); sys.exit(2)
            notify("turn/started", {"threadId": thread_id, "turn": {"id": TURN, "items": [], "itemsView": "notLoaded", "status": "inProgress", "error": None, "startedAt": 1, "completedAt": None, "durationMs": None}})
            if scenario in ("childfirst", "parentfirst", "draininterrupt"):
                notify("item/started", {"threadId": thread_id, "turnId": TURN, "startedAtMs": 1,
                    "item": item("subAgentActivity", kind="started", agentThreadId="child", agentPath="/root/worker")})
                notify("turn/started", {"threadId": "child", "turn": {"id": TURN}})
                if scenario == "childfirst":
                    finish("child", "CHILD_DONE")
                    finish(thread_id, "PARENT_DONE")
                else:
                    finish(thread_id, "PARENT_DONE")
                    if scenario == "parentfirst":
                        finish("child", "CHILD_DONE")
                continue
            if scenario == "interrupt":
                notify("item/started", {"threadId": thread_id, "turnId": TURN, "startedAtMs": 1, "item": item("agentMessage", id="msg-1", text="", phase="final_answer")})
                notify("item/agentMessage/delta", {"threadId": thread_id, "turnId": TURN, "itemId": "msg-1", "delta": "partial"})
                continue
            if scenario == "filechange":
                notify("item/started", {"threadId": thread_id, "turnId": TURN, "startedAtMs": 1, "item": item(
                    "fileChange", id="call-1", status="inProgress",
                    changes=[{"path": "/p/x.py", "kind": {"type": "update"}, "diff": "@@"}])})
                out({"jsonrpc": "2.0", "id": 8, "method": "item/fileChange/requestApproval", "params": {
                    "threadId": thread_id, "turnId": TURN, "itemId": "call-1", "startedAtMs": 1,
                    "reason": "write outside the workspace",
                    "availableDecisions": ["accept", "acceptForSession", "decline", "cancel"]}})
                continue
            if scenario == "permission":
                out({"jsonrpc": "2.0", "id": 9, "method": "item/permissions/requestApproval", "params": {
                    "threadId": thread_id, "turnId": TURN, "itemId": "call-1", "startedAtMs": 1,
                    "cwd": "/p", "reason": "network access",
                    "permissions": {"network": {"allowAll": True}}}})
                continue
            if scenario == "question":
                out({"jsonrpc": "2.0", "id": 7, "method": "item/tool/requestUserInput", "params": {
                    "threadId": thread_id, "turnId": TURN, "itemId": "tool-q", "isBlocking": True,
                    "questions": [{"id": "q1", "header": "Color", "question": "Which color?", "isOther": False, "isSecret": False,
                                   "options": [{"label": "red", "description": ""}, {"label": "blue", "description": ""}]}]}})
                continue
            notify("item/started", {"threadId": thread_id, "turnId": TURN, "startedAtMs": 1, "item": item(
                "commandExecution", command="/bin/zsh -lc 'touch x.txt'", cwd="/p", status="inProgress",
                commandActions=[{"type": "unknown", "command": "touch x.txt"}], aggregatedOutput=None, exitCode=None, durationMs=None,
                pluginId=None, scriptPath=None, processId=None, source="agent")})
            out({"jsonrpc": "2.0", "id": 0, "method": "item/commandExecution/requestApproval", "params": {
                "kind": "command", "threadId": thread_id, "turnId": TURN, "itemId": "call-1", "startedAtMs": 1,
                "command": "/bin/zsh -lc 'touch x.txt'", "cwd": "/p", "commandActions": [{"type": "unknown", "command": "touch x.txt"}],
                "availableDecisions": ["accept", "acceptForSession", "decline", "cancel"]}})
        elif meth == "turn/interrupt":
            out({"jsonrpc": "2.0", "id": rid, "result": {}})
            notify("turn/completed", {"threadId": thread_id, "turn": {"id": TURN, "items": [], "itemsView": "summary", "status": "interrupted", "error": None, "startedAt": 1, "completedAt": 2, "durationMs": 1}})
        elif rid == 7 and "result" in m:      # the user-input answer
            answer = m["result"]["answers"]["q1"]["answers"][0]
            notify("item/started", {"threadId": thread_id, "turnId": TURN, "startedAtMs": 1, "item": item("agentMessage", id="msg-1", text="", phase="final_answer")})
            notify("item/agentMessage/delta", {"threadId": thread_id, "turnId": TURN, "itemId": "msg-1", "delta": f"you chose {answer}"})
            notify("turn/completed", {"threadId": thread_id, "turn": {"id": TURN, "items": [], "itemsView": "summary", "status": "completed", "error": None, "startedAt": 1, "completedAt": 2, "durationMs": 1}})
        elif rid == 8 and ("result" in m or "error" in m):    # the file-change reply
            reply_out(m)
            decision = (m.get("result") or {}).get("decision")
            notify("item/completed", {"threadId": thread_id, "turnId": TURN, "completedAtMs": 2, "item": item(
                "fileChange", id="call-1",
                status="completed" if decision in ("accept", "acceptForSession") else "declined",
                changes=[{"path": "/p/x.py", "kind": {"type": "update"}, "diff": "@@"}])})
            finish(thread_id)
        elif rid == 9 and ("result" in m or "error" in m):    # the permissions reply
            reply_out(m)
            finish(thread_id)
        elif rid == 0 and ("result" in m or "error" in m):   # the approval reply
            reply_out(m)
            decision = (m.get("result") or {}).get("decision")
            if decision in ("accept", "acceptForSession"):
                notify("item/commandExecution/outputDelta", {"threadId": thread_id, "turnId": TURN, "itemId": "call-1", "delta": "hello\n"})
                notify("item/completed", {"threadId": thread_id, "turnId": TURN, "completedAtMs": 2, "item": item(
                    "commandExecution", command="/bin/zsh -lc 'touch x.txt'", cwd="/p", status="completed",
                    commandActions=[{"type": "unknown", "command": "touch x.txt"}], aggregatedOutput="hello\n", exitCode=0, durationMs=1,
                    pluginId=None, scriptPath=None, processId="1", source="agent")})
            else:
                notify("item/completed", {"threadId": thread_id, "turnId": TURN, "completedAtMs": 2, "item": item(
                    "commandExecution", command="/bin/zsh -lc 'touch x.txt'", cwd="/p", status="declined",
                    commandActions=[{"type": "unknown", "command": "touch x.txt"}], aggregatedOutput=None, exitCode=None, durationMs=None,
                    pluginId=None, scriptPath=None, processId=None, source="agent")})
            notify("item/started", {"threadId": thread_id, "turnId": TURN, "startedAtMs": 3, "item": item("agentMessage", id="msg-1", text="", phase="final_answer")})
            notify("item/agentMessage/delta", {"threadId": thread_id, "turnId": TURN, "itemId": "msg-1", "delta": "DONE"})
            notify("item/completed", {"threadId": thread_id, "turnId": TURN, "completedAtMs": 4, "item": item("agentMessage", id="msg-1", text="DONE", phase="final_answer")})
            notify("thread/tokenUsage/updated", {"threadId": thread_id, "turnId": TURN, "tokenUsage": {
                "total": {"totalTokens": 2400, "inputTokens": 2200, "cachedInputTokens": 0, "cacheWriteInputTokens": 0, "outputTokens": 200, "reasoningOutputTokens": 0},
                "last": {"totalTokens": 2400, "inputTokens": 2200, "cachedInputTokens": 0, "cacheWriteInputTokens": 0, "outputTokens": 200, "reasoningOutputTokens": 0},
                "modelContextWindow": 240000}})
            notify("account/rateLimits/updated", {"rateLimits": {"limitId": "codex", "primary": {"usedPercent": 3, "windowDurationMins": 300, "resetsAt": 1},
                                                                "secondary": {"usedPercent": 12, "windowDurationMins": 10080, "resetsAt": 2}}})
            notify("turn/completed", {"threadId": thread_id, "turn": {"id": TURN, "items": [], "itemsView": "summary", "status": "completed", "error": None, "startedAt": 1, "completedAt": 5, "durationMs": 4}})


if __name__ == "__main__":
    main()
