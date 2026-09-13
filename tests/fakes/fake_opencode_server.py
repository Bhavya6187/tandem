"""An in-process stand-in for `opencode serve`: health, the SSE event
stream, message posting, permission and question replies, abort.

Scenario (constructor arg or $FAKE_OPENCODE_SCENARIO):
  tool        (default) reasoning delta, bash tool pending/running/completed, text "DONE"
  permission  like tool but asks permission first; reject -> tool error, no text
  question    asks one question, echoes the answer as text
  abort       streams "partial" then waits for POST .../abort
  error       emits session.error and answers the POST with 500
  malformed   pushes an event tandem cannot read, then finishes the turn normally
  sse_drop    like tool, but the first /event stream is chunked HTTP/1.1 and is cut
              after one event (an IncompleteRead client-side, like a serve restart)
"""
from __future__ import annotations

import json
import os
import queue
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SID = "ses_fake"


class FakeOpencode:
    def __init__(self, scenario: str | None = None, port: int = 0):
        self.scenario = scenario or os.environ.get("FAKE_OPENCODE_SCENARIO", "tool")
        self.clients: list[queue.Queue] = []
        self.replies: list[dict] = []          # permission replies received
        self.question_replies: list[dict] = []
        self.posts: list[dict] = []
        self.aborted = threading.Event()
        self.dropped = threading.Event()       # the sse_drop stream was cut
        self.drop_once = self.scenario == "sse_drop"
        self._reply = threading.Event()
        self._answered = threading.Event()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _json(self, code, obj):
                body = json.dumps(obj).encode()
                self.send_response(code); self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body))); self.end_headers(); self.wfile.write(body)

            def do_GET(self):
                if self.path == "/global/health":
                    return self._json(200, {"healthy": True, "version": "fake"})
                if self.path == "/event":
                    q: queue.Queue = queue.Queue(); fake.clients.append(q)
                    drop, fake.drop_once = fake.drop_once, False
                    if drop:                    # the real serve streams chunked HTTP/1.1
                        self.protocol_version = "HTTP/1.1"
                        self.send_response(200); self.send_header("content-type", "text/event-stream")
                        self.send_header("transfer-encoding", "chunked"); self.end_headers()
                    else:
                        self.send_response(200); self.send_header("content-type", "text/event-stream")
                        self.end_headers()
                    try:
                        while True:
                            ev = q.get()
                            if ev is None:
                                return
                            payload = f"data: {json.dumps(ev)}\n\n".encode()
                            if drop:            # one chunk, then the body stops mid-stream
                                self.wfile.write(b"%x\r\n" % len(payload) + payload + b"\r\n")
                                self.wfile.flush(); self.close_connection = True
                                fake.clients.remove(q); fake.dropped.set()
                                return
                            self.wfile.write(payload); self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        return
                self._json(404, {"error": "no"})

            def do_POST(self):
                n = int(self.headers.get("content-length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                parts = self.path.strip("/").split("/")
                if parts[0] == "session" and parts[-1] == "message":
                    fake.posts.append(body)
                    return self._json(*fake.run_message(parts[1], body))
                if parts[0] == "permission" and parts[-1] == "reply":
                    fake.replies.append({"id": parts[1], **body}); fake._reply.set()
                    return self._json(200, True)
                if parts[0] == "session" and parts[-1] == "abort":
                    fake.aborted.set(); return self._json(200, True)
                if parts[0] == "session" and "question" in parts and parts[-1] == "reply":
                    fake.question_replies.append({"id": parts[3], **body}); fake._answered.set()
                    return self._json(200, True)
                self._json(404, {"error": "no"})

        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def push(self, typ: str, props: dict) -> None:
        ev = {"id": f"evt_{time.monotonic_ns()}", "type": typ, "properties": props}
        for q in list(self.clients):
            q.put(ev)

    def part(self, part: dict) -> None:
        self.push("message.part.updated", {"sessionID": SID, "part": {"sessionID": SID, "messageID": "msg_a", **part}})

    def run_message(self, sid: str, body: dict) -> tuple[int, dict]:
        self.push("message.updated", {"sessionID": SID, "info": {"id": "msg_u", "role": "user", "sessionID": SID}})
        self.push("session.status", {"sessionID": SID, "status": {"type": "busy"}})
        self.push("message.updated", {"sessionID": SID, "info": {"id": "msg_a", "role": "assistant", "sessionID": SID,
                                                                    "modelID": body.get("model", {}).get("modelID", "big-pickle")}})
        info = {"id": "msg_a", "role": "assistant", "sessionID": SID, "tokens": {"input": 120, "output": 7}, "cost": 0.001}
        if self.scenario == "error":
            self.push("session.error", {"sessionID": SID, "error": {"name": "ProviderError", "message": "provider down"}})
            return 500, {"error": "provider down"}
        if self.scenario == "abort":
            self.part({"id": "prt_t", "type": "text", "text": ""})
            self.push("message.part.delta", {"sessionID": SID, "messageID": "msg_a", "partID": "prt_t", "field": "text", "delta": "partial"})
            self.aborted.wait(10)
            self.push("session.status", {"sessionID": SID, "status": {"type": "idle"}})
            return 200, {"info": info, "parts": [{"type": "text", "text": "partial"}]}
        if self.scenario == "malformed":
            self.push("permission.asked", "junk")       # properties is not an object
            self.part({"id": "prt_t", "type": "text", "text": ""})
            self.push("message.part.delta", {"sessionID": SID, "messageID": "msg_a", "partID": "prt_t", "field": "text", "delta": "DONE"})
            self.push("session.idle", {"sessionID": SID})
            return 200, {"info": info, "parts": [{"type": "text", "text": "DONE"}]}
        if self.scenario == "question":
            self.push("question.asked", {"id": "que_1", "sessionID": SID, "questions": [
                {"question": "Which color?", "header": "Color", "options": [{"label": "red"}, {"label": "blue"}]}]})
            self._answered.wait(10)
            answer = self.question_replies[-1]["answers"][0][0]
            self.part({"id": "prt_t", "type": "text", "text": ""})
            self.push("message.part.delta", {"sessionID": SID, "messageID": "msg_a", "partID": "prt_t", "field": "text", "delta": f"you chose {answer}"})
            return 200, {"info": info, "parts": [{"type": "text", "text": f"you chose {answer}"}]}
        self.part({"id": "prt_r", "type": "reasoning", "text": ""})
        self.push("message.part.delta", {"sessionID": SID, "messageID": "msg_a", "partID": "prt_r", "field": "text", "delta": "thinking…"})
        self.part({"id": "prt_tool", "type": "tool", "tool": "bash", "callID": "call_1", "state": {"status": "pending", "input": {}, "raw": ""}})
        if self.scenario == "permission":
            self.push("permission.asked", {"id": "per_1", "sessionID": SID, "permission": "bash", "patterns": ["touch x.txt"],
                                           "metadata": {"command": "touch x.txt"}, "always": ["*"], "tool": {"messageID": "msg_a", "callID": "call_1"}})
            self._reply.wait(10)
            self.push("permission.replied", {"sessionID": SID, "requestID": "per_1", "reply": self.replies[-1]["reply"]})
            if self.replies[-1]["reply"] == "reject":
                self.part({"id": "prt_tool", "type": "tool", "tool": "bash", "callID": "call_1",
                           "state": {"status": "error", "input": {"command": "touch x.txt"}, "error": "The user rejected permission", "time": {"start": 1, "end": 2}}})
                return 200, {"info": info, "parts": []}
        self.part({"id": "prt_tool", "type": "tool", "tool": "bash", "callID": "call_1",
                   "state": {"status": "running", "input": {"command": "touch x.txt"}, "time": {"start": 1}}})
        self.part({"id": "prt_tool", "type": "tool", "tool": "bash", "callID": "call_1",
                   "state": {"status": "completed", "input": {"command": "touch x.txt"}, "output": "ok\n", "title": "touch x.txt",
                             "metadata": {"exit": 0}, "time": {"start": 1, "end": 2}}})
        self.part({"id": "prt_t", "type": "text", "text": ""})
        self.push("message.part.delta", {"sessionID": SID, "messageID": "msg_a", "partID": "prt_t", "field": "text", "delta": "DONE"})
        self.push("session.status", {"sessionID": SID, "status": {"type": "idle"}})
        self.push("session.idle", {"sessionID": SID})
        return 200, {"info": info, "parts": [{"type": "text", "text": "DONE"}]}

    def stop(self) -> None:
        for q in list(self.clients):
            q.put(None)
        self.server.shutdown()
        self.server.server_close()      # the listening socket goes with the loop
