"""opencode headless: one `opencode serve` per window, driven over HTTP.

  GET  /global/health                          readiness poll after spawn
  GET  /event                                  SSE: {"type","properties"} per `data:` line
  POST /session/<sid>/message {model?, parts}  blocks until the turn ends; the response
                                               is the assistant message (info.tokens, cost)
  POST /permission/<id>/reply {"reply": once|always|reject}
  POST /session/<sid>/question/<id>/reply {"answers": [[text], …]}
  POST /session/<sid>/abort

Events consumed (opencode 1.18.20, tests/golden/chat/opencode_events.jsonl):
message.updated (learn assistant/user message ids), message.part.updated
(tool parts: pending → running → completed | error; text/reasoning parts:
learn part types), message.part.delta (field "text"), permission.asked,
question.asked, session.error. The POST returning is the turn boundary.
Default opencode config auto-allows bash; a permission event only appears
when the user's opencode config asks — opencode's rule, not tandem's."""

from __future__ import annotations

import http.client
import json
import queue
import socket
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import urlparse

from ..events import (Answers, ApprovalRequest, Failure, LiveEvent, QuestionRequest,
                      TextDelta, ThinkingDelta, ToolFinished, ToolOutput, ToolStarted,
                      TurnFinished, TurnOutcome)
from . import child_env, first_line, summarize_args, terminate

_REPLY = {"allow": "once", "always": "always", "deny": "reject"}


@dataclass
class TurnState:
    session_id: str
    assistant_msgs: set = field(default_factory=set)
    user_msgs: set = field(default_factory=set)
    part_types: dict = field(default_factory=dict)     # part id -> "text" | "reasoning" | "tool"
    started: set = field(default_factory=set)           # tool call ids already announced
    failed: str = ""


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class OpencodeRuntime:
    harness = "opencode"

    def __init__(self, cfg, *, binary: list[str] | None = None, base_url: str | None = None):
        self.cfg = cfg
        self.binary = list(binary) if binary else ["opencode"]
        self.base_url = base_url
        self._injected = base_url is not None
        self._proc: subprocess.Popen | None = None
        self._drain: threading.Thread | None = None
        self._stderr: deque[str] = deque(maxlen=20)
        self._events: queue.Queue = queue.Queue()
        self._sse_started = False
        self._sse_token = 0                      # which reader owns the registration
        self._sse_ready = threading.Event()      # /event is subscribed, events cannot be missed
        self._turn_active = threading.Event()
        self._session_id: str | None = None
        self._interrupted = False
        self._lock = threading.Lock()

    # -- server lifecycle ------------------------------------------------------

    def _healthy(self) -> bool:
        try:
            return bool((self._http("GET", "/global/health", timeout=2) or {}).get("healthy"))
        except Exception:
            return False

    def _forget(self, proc: subprocess.Popen) -> None:
        """A server that never answered must not be remembered: left alive
        with base_url set, every later ensure_server short-circuits on it and
        each opencode turn costs another health poll plus a failed POST for
        the life of the window. Dropped here, the next turn spawns a fresh
        one — restarted once, then reported, as the spec says."""
        terminate(proc)
        with self._lock:
            if self._proc is proc:
                self._proc = None
                self.base_url = None
                self._sse_started = False

    def ensure_server(self, cwd: str, health_timeout: float = 10.0) -> str:
        if self._injected:
            return self.base_url
        with self._lock:
            if self._proc is not None and self._proc.poll() is None and self.base_url:
                return self.base_url
            port = _free_port()
            self.base_url = f"http://127.0.0.1:{port}"
            self._proc = subprocess.Popen(
                [*self.binary, "serve", "--port", str(port), "--hostname", "127.0.0.1"],
                cwd=cwd, env=child_env(), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE, text=True, start_new_session=True,
            )
            proc = self._proc
            self._sse_started = False

        def drain() -> None:
            try:
                for line in proc.stderr:
                    self._stderr.append(line.rstrip())
            except (OSError, ValueError):
                pass
            finally:
                try:
                    proc.stderr.close()     # the pipe is closed by the thread that reads it
                except OSError:
                    pass

        self._drain = threading.Thread(target=drain, name="tandem-chat-opencode-stderr", daemon=True)
        self._drain.start()
        deadline = time.monotonic() + health_timeout
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                self._forget(proc)
                raise RuntimeError("opencode serve exited: " + "\n".join(self._stderr))
            if self._healthy():
                return self.base_url
            time.sleep(0.1)
        self._forget(proc)
        raise RuntimeError(
            f"opencode serve did not become healthy within {health_timeout:g}s")

    def _http(self, method: str, path: str, body=None, timeout: float = 600.0):
        u = urlparse(self.base_url)
        conn = http.client.HTTPConnection(u.hostname, u.port, timeout=timeout)
        try:
            data = json.dumps(body).encode() if body is not None else None
            conn.request(method, path, body=data, headers={"content-type": "application/json"})
            resp = conn.getresponse()
            raw = resp.read()
            if resp.status >= 400:
                raise RuntimeError(f"{method} {path} -> {resp.status}: {raw.decode(errors='replace')[:200]}")
            return json.loads(raw) if raw else None
        finally:
            conn.close()

    def _start_sse(self) -> None:
        with self._lock:
            if self._sse_started:
                return
            self._sse_started = True
            self._sse_ready.clear()
            self._sse_token += 1
            base, token = self.base_url, self._sse_token
        threading.Thread(target=self._sse_reader, args=(base, token),
                         name="tandem-chat-opencode-sse", daemon=True).start()

    def _unregister(self, token: int) -> None:
        """Caller holds the lock. Hand the registration back unless a newer
        reader already took it, so the next turn opens its own stream."""
        if self._sse_token == token:
            self._sse_started = False
            self._sse_ready.clear()

    def _sse_reader(self, base: str, token: int) -> None:
        u = urlparse(base)
        try:
            while True:
                conn = None
                try:
                    conn = http.client.HTTPConnection(u.hostname, u.port, timeout=None)
                    conn.request("GET", "/event")
                    resp = conn.getresponse()
                    self._sse_ready.set()   # the subscriber is registered before the headers
                    for raw in resp:
                        line = raw.decode(errors="replace").rstrip("\n")
                        if line.startswith("data:"):
                            try:
                                self._events.put(json.loads(line[5:].strip()))
                            except ValueError:
                                continue
                except Exception:
                    # a server restarting or gone (OSError), a chunked body cut
                    # mid-stream (http.client's IncompleteRead): either way this
                    # stream is over and the reconnect is ruled on below
                    pass
                finally:
                    self._sse_ready.clear()     # nothing is subscribed until the reconnect
                    if conn is not None:
                        try:
                            conn.close()
                        except Exception:
                            pass
                with self._lock:
                    # a stream that ended is worth reconnecting only while a turn
                    # is still listening to it; idle, this thread hands the
                    # registration back and ends
                    if not (self._turn_active.is_set() and self.base_url == base):
                        self._unregister(token)
                        return
                time.sleep(0.5)
        finally:
            with self._lock:        # no reader may die still holding the registration
                self._unregister(token)

    # -- replies ---------------------------------------------------------------

    def _reply_permission(self, pid: str, reply: str) -> None:
        self._http("POST", f"/permission/{pid}/reply", {"reply": reply}, timeout=30)

    def _reply_question(self, sid: str, qid: str, answers: list[list[str]]) -> None:
        self._http("POST", f"/session/{sid}/question/{qid}/reply", {"answers": answers}, timeout=30)

    def _deliver(self, emit: Callable[[LiveEvent], None], send: Callable[[], None]) -> None:
        """A reply opencode refused — or one that never reached it — costs the
        window a line, not the turn: the POST that drives the turn is still
        running and still owes the caller its outcome."""
        try:
            send()
        except Exception as exc:
            emit(Failure(f"opencode reply failed: {type(exc).__name__}: {first_line(str(exc))}"))

    # -- protocol --------------------------------------------------------------

    def handle_event(self, ev: dict, st: TurnState, emit: Callable[[LiveEvent], None],
                     answers: Answers) -> None:
        typ = ev.get("type")
        pr = ev.get("properties") or {}
        sid = st.session_id
        if typ == "message.updated":
            info = pr.get("info") or {}
            if info.get("sessionID") != sid:
                return
            (st.assistant_msgs if info.get("role") == "assistant" else st.user_msgs).add(info.get("id"))
        elif typ == "message.part.updated":
            part = pr.get("part") or {}
            if part.get("sessionID") != sid or part.get("messageID") in st.user_msgs:
                return
            ptype = part.get("type")
            if ptype in ("text", "reasoning"):
                st.part_types[part.get("id")] = ptype
            elif ptype == "tool":
                call_id, tool = part.get("callID", ""), part.get("tool", "")
                state = part.get("state") or {}
                status = state.get("status")
                if status in ("running", "completed", "error") and call_id not in st.started:
                    st.started.add(call_id)
                    emit(ToolStarted(call_id, tool, summarize_args(tool, state.get("input"))))
                if status == "completed":
                    if state.get("output"):
                        emit(ToolOutput(call_id, state["output"]))
                    emit(ToolFinished(call_id, True, first_line(state.get("title") or "")))
                elif status == "error":
                    emit(ToolFinished(call_id, False, first_line(state.get("error") or "error")))
        elif typ == "message.part.delta":
            if pr.get("sessionID") != sid or pr.get("field") != "text" or pr.get("messageID") in st.user_msgs:
                return
            delta = pr.get("delta", "")
            if st.part_types.get(pr.get("partID")) == "reasoning":
                emit(ThinkingDelta(delta))
            else:
                emit(TextDelta(delta))
        elif typ == "permission.asked":
            if pr.get("sessionID") != sid:
                return
            detail = f"{pr.get('permission', '')}: {', '.join(pr.get('patterns') or [])}".strip(": ")
            choice = answers.approve(ApprovalRequest("permission", first_line(detail)))
            pid, reply = pr.get("id", ""), _REPLY.get(choice, "reject")
            self._deliver(emit, lambda: self._reply_permission(pid, reply))
        elif typ == "question.asked":
            if pr.get("sessionID") != sid:
                return
            replies = []
            for q in pr.get("questions") or []:
                options = tuple(o.get("label", "") for o in (q.get("options") or []) if isinstance(o, dict))
                replies.append([answers.answer(QuestionRequest(q.get("question", ""), options))])
            qid = pr.get("id", "")
            self._deliver(emit, lambda: self._reply_question(sid, qid, replies))
        elif typ == "session.error":
            if pr.get("sessionID") != sid:
                return
            err = pr.get("error") or {}
            st.failed = str(err.get("message") or err)
            emit(Failure(st.failed))

    def _handle(self, ev, st: TurnState, emit: Callable[[LiveEvent], None],
                answers: Answers) -> None:
        """One streamed event. A line tandem cannot read — a non-object
        `properties`, a pattern that is not a string, a release that reshapes a
        field — costs the window that line, never the turn."""
        try:
            self.handle_event(ev, st, emit, answers)
        except Exception as exc:
            emit(Failure(f"opencode event tandem cannot handle: "
                         f"{type(exc).__name__}: {first_line(str(exc))}"))

    # -- turn ------------------------------------------------------------------

    def run_turn(self, session, native_id: str | None, prompt: str, model: str,
                 emit: Callable[[LiveEvent], None], answers: Answers) -> TurnOutcome:
        assert native_id, "opencode sessions are created at pair time"
        body: dict = {"parts": [{"type": "text", "text": prompt}]}
        if model:
            if "/" not in model:
                msg = f"opencode models are spelled provider/model, got {model!r}"
                emit(Failure(msg)); emit(TurnFinished("failed", ""))
                return TurnOutcome("failed", msg)
            provider, model_id = model.split("/", 1)
            body["model"] = {"providerID": provider, "modelID": model_id}
        try:
            self.ensure_server(session.cwd)
        except Exception as exc:        # an unhealthy server, a missing binary: both end the turn
            msg = str(exc) or type(exc).__name__
            emit(Failure(msg)); emit(TurnFinished("failed", ""))
            return TurnOutcome("failed", msg)
        self._interrupted = False
        self._session_id = native_id
        st = TurnState(session_id=native_id)
        done: dict = {}
        self._turn_active.set()
        try:
            self._start_sse()
            self._sse_ready.wait(10.0)          # never post before /event is subscribed
            while not self._events.empty():     # stale events from an earlier turn
                try:
                    self._events.get_nowait()
                except queue.Empty:
                    break

            def post() -> None:
                try:
                    done["response"] = self._http("POST", f"/session/{native_id}/message", body, timeout=3600)
                except Exception as exc:
                    done["error"] = str(exc) or type(exc).__name__

            threading.Thread(target=post, name="tandem-chat-opencode-post", daemon=True).start()
            while "response" not in done and "error" not in done:
                try:
                    ev = self._events.get(timeout=0.25)
                except queue.Empty:
                    continue
                self._handle(ev, st, emit, answers)
            deadline = time.monotonic() + 0.3     # trailing events still in flight on the SSE thread
            while time.monotonic() < deadline:
                try:
                    ev = self._events.get(timeout=0.05)
                except queue.Empty:
                    continue
                self._handle(ev, st, emit, answers)
        finally:
            self._turn_active.clear()
        usage = ""
        resp = done.get("response")
        if isinstance(resp, dict):
            info = resp.get("info") or {}
            tokens = info.get("tokens") or {}
            if isinstance(tokens.get("input"), int) and isinstance(tokens.get("output"), int):
                usage = f"{tokens['input']}↑ {tokens['output']}↓"
            if isinstance(info.get("cost"), (int, float)):
                usage += f" · ${info['cost']:.4f}"
        if self._interrupted:
            status, error = "interrupted", ""
        elif "error" in done or st.failed:
            status, error = "failed", st.failed or done.get("error", "")
        else:
            status, error = "completed", ""
        emit(TurnFinished(status, usage))
        return TurnOutcome(status, error)

    def interrupt(self) -> None:
        sid = self._session_id
        if not sid or not self.base_url:
            return
        self._interrupted = True
        try:
            self._http("POST", f"/session/{sid}/abort", {}, timeout=10)
        except Exception:
            pass

    def close(self) -> None:
        with self._lock:
            proc, self._proc = self._proc, None
            drain, self._drain = self._drain, None
            self.base_url = None if not self._injected else self.base_url
        if proc is not None:
            terminate(proc, soft_timeout=0.0, term_timeout=3.0)
        if drain is not None:
            drain.join(2.0)     # the dead child's stderr reaches EOF, the pipe closes
