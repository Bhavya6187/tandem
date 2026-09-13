"""Claude Code headless: one `claude -p` process per turn over stream-json.

Wire shapes (captured live 2026-09-13 on claude 2.1.265, see
tests/golden/chat/claude_stream.jsonl; the open-source Agent SDK is the
reference for anything not captured):

  client -> cli
    {"type":"user","message":{"role":"user","content":[{"type":"text","text":…}]},
     "parent_tool_use_id":null,"session_id":SID}
    {"type":"control_request","request_id":ID,"request":{"subtype":"interrupt"}}
    {"type":"control_response","response":{"subtype":"success","request_id":ID,"response":{…}}}
  cli -> client
    system/init · stream_event (content_block_delta: text_delta | thinking_delta)
    assistant (content blocks incl. tool_use) · user (tool_result blocks)
    control_request (subtype can_use_tool: tool_name, input, permission_suggestions)
    result (subtype, is_error, num_turns, total_cost_usd)

The process exits after `result`; that exit is the turn boundary, so no
Stop-hook sentinel is wired. `handle_line` is the whole protocol as a pure
function of one parsed line so the golden fixture can drive it."""

from __future__ import annotations

import json
import subprocess
import threading
import uuid
from collections import deque
from typing import Callable

from ... import paths
from ..events import (Answers, ApprovalRequest, LiveEvent, QuestionRequest, TextDelta,
                      ThinkingDelta, ToolFinished, ToolOutput, ToolStarted, TurnFinished,
                      TurnOutcome)
from . import child_env, first_line, summarize_args, terminate

_COMMAND_TOOLS = ("Bash",)


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(b.get("text", "") for b in content
                       if isinstance(b, dict) and b.get("type") == "text")
    return ""


class ClaudeRuntime:
    harness = "claude"

    def __init__(self, cfg, *, binary: list[str] | None = None):
        self.cfg = cfg
        self.binary = list(binary) if binary else ["claude"]
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._interrupted = False
        self._streamed_text = False   # did the current message stream text deltas?

    # -- argv ----------------------------------------------------------------

    def argv(self, native_id: str, fresh: bool, model: str) -> list[str]:
        argv = [*self.binary, "-p", "--session-id" if fresh else "--resume", native_id,
                "--input-format", "stream-json", "--output-format", "stream-json",
                "--verbose", "--include-partial-messages",
                "--permission-prompt-tool", "stdio",
                "--setting-sources", ",".join(self.cfg.claude_setting_sources)]
        if model:
            argv += ["--model", model]
        return argv

    # -- protocol ------------------------------------------------------------

    def _approval(self, req: dict, answers: Answers) -> dict:
        tool = req.get("tool_name", "")
        inp = req.get("input") or {}
        if tool == "AskUserQuestion":
            answered = {}
            for q in inp.get("questions") or []:
                prompt = q.get("question", "")
                options = tuple(o.get("label", "") for o in q.get("options") or []
                                if isinstance(o, dict))
                answered[prompt] = answers.answer(QuestionRequest(prompt, options))
            return {"behavior": "allow", "updatedInput": {**inp, "answers": answered}}
        kind = "command" if tool in _COMMAND_TOOLS else "permission"
        detail = f"{tool} {summarize_args(tool, inp)}".strip()
        choice = answers.approve(ApprovalRequest(kind, detail))
        if choice == "deny":
            return {"behavior": "deny", "message": "denied in tandem chat"}
        resp: dict = {"behavior": "allow", "updatedInput": inp}
        if choice == "always":
            for s in req.get("permission_suggestions") or []:
                if isinstance(s, dict) and s.get("type") == "addRules":
                    resp["updatedPermissions"] = [{"type": "addRules", "rules": s.get("rules", []),
                                                   "behavior": "allow", "destination": "session"}]
                    break
        return resp

    def handle_line(self, m: dict, emit: Callable[[LiveEvent], None], answers: Answers,
                    send: Callable[[dict], None]) -> TurnOutcome | None:
        """One parsed stdout line. Returns the outcome on `result`, else None."""
        t = m.get("type")
        if t == "stream_event":
            ev = m.get("event") or {}
            if ev.get("type") == "message_start":
                self._streamed_text = False
            elif ev.get("type") == "content_block_delta":
                d = ev.get("delta") or {}
                if d.get("type") == "text_delta":
                    self._streamed_text = True
                    emit(TextDelta(d.get("text", "")))
                elif d.get("type") == "thinking_delta" and d.get("thinking"):
                    emit(ThinkingDelta(d["thinking"]))
            return None
        if t == "assistant":
            for b in (m.get("message") or {}).get("content") or []:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "tool_use":
                    emit(ToolStarted(b.get("id", ""), b.get("name", ""),
                                     summarize_args(b.get("name", ""), b.get("input"))))
                elif b.get("type") == "text" and not self._streamed_text and b.get("text"):
                    emit(TextDelta(b["text"]))       # partial messages off: paint the block
            return None
        if t == "user":
            for b in (m.get("message") or {}).get("content") or []:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    text = _text_of(b.get("content"))
                    err = bool(b.get("is_error"))
                    emit(ToolOutput(b.get("tool_use_id", ""), text))
                    emit(ToolFinished(b.get("tool_use_id", ""), not err, first_line(text) if err else ""))
            return None
        if t == "control_request":
            req = m.get("request") or {}
            rid = m.get("request_id")
            response = self._approval(req, answers) if req.get("subtype") == "can_use_tool" else {}
            send({"type": "control_response",
                  "response": {"subtype": "success", "request_id": rid, "response": response}})
            return None
        if t == "result":
            if self._interrupted:
                status = "interrupted"
            else:
                status = "failed" if m.get("is_error") else "completed"
            cost = m.get("total_cost_usd")
            usage = f"{m.get('num_turns', 0)} turns"
            if isinstance(cost, (int, float)):
                usage += f" · ${cost:.4f}"
            emit(TurnFinished(status, usage))
            return TurnOutcome(status, error=str(m.get("result", "")) if status == "failed" else "")
        return None     # system/init, rate_limit_event, control_response: nothing to paint

    # -- process -------------------------------------------------------------

    def run_turn(self, session, native_id: str | None, prompt: str, model: str,
                 emit: Callable[[LiveEvent], None], answers: Answers) -> TurnOutcome:
        assert native_id, "claude session ids are minted at pair time"
        fresh = not paths.claude_transcript_path(session.cwd, native_id).exists()
        self._interrupted = False
        self._streamed_text = False
        proc = subprocess.Popen(
            self.argv(native_id, fresh, model), cwd=session.cwd, env=child_env(),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1, start_new_session=True,
        )
        with self._lock:
            self._proc = proc
        tail: deque[str] = deque(maxlen=20)

        def drain_stderr() -> None:
            for line in proc.stderr:
                tail.append(line.rstrip())

        reader = threading.Thread(target=drain_stderr, name="tandem-chat-claude-stderr",
                                  daemon=True)
        reader.start()

        def send(obj: dict) -> None:
            # a child that died while we were blocked on an approval leaves a
            # broken pipe (or a stdin closed under us by close()); either way the
            # stdout loop is about to hit EOF and the turn ends through the
            # `outcome is None` path below, which owes the window a TurnFinished
            with self._lock:
                if proc.stdin and not proc.stdin.closed:
                    try:
                        proc.stdin.write(json.dumps(obj) + "\n")
                        proc.stdin.flush()
                    except (OSError, ValueError):
                        pass

        outcome: TurnOutcome | None = None
        try:
            send({"type": "user",
                  "message": {"role": "user", "content": [{"type": "text", "text": prompt}]},
                  "parent_tool_use_id": None, "session_id": native_id})
            for line in proc.stdout:
                try:
                    m = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(m, dict):
                    continue
                outcome = self.handle_line(m, emit, answers, send)
                if outcome is not None:
                    break
        finally:
            terminate(proc, soft=lambda: proc.stdin.close(), soft_timeout=3.0)
            reader.join(2.0)        # the tail below must be the whole of stderr
            with self._lock:
                self._proc = None
            for pipe in (proc.stdin, proc.stdout, proc.stderr):
                if pipe is not None:
                    try:
                        pipe.close()
                    except OSError:
                        pass
        if outcome is None:
            status = "interrupted" if self._interrupted else "failed"
            outcome = TurnOutcome(status, error="\n".join(tail) or f"claude exited {proc.returncode}")
            emit(TurnFinished(status, ""))
        return outcome

    def interrupt(self) -> None:
        with self._lock:
            proc = self._proc
            if proc is None or proc.poll() is not None:
                return
            self._interrupted = True
            try:
                proc.stdin.write(json.dumps({"type": "control_request", "request_id": f"int-{uuid.uuid4().hex[:8]}",
                                             "request": {"subtype": "interrupt"}}) + "\n")
                proc.stdin.flush()
            except (OSError, ValueError):
                pass

    def close(self) -> None:
        with self._lock:
            proc = self._proc
        if proc is not None:
            terminate(proc, soft=lambda: proc.stdin.close(), soft_timeout=2.0)
