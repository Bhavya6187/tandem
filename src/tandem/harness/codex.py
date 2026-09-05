"""Codex CLI adapter.

Session format observed on codex-cli 0.145.0, rechecked on 0.153.4
(docs/formats.md):
- rollout: ~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<uuidv7>.jsonl
- each line {timestamp, type, payload}; model-facing history is the
  response_item lines, UI history is the event_msg lines
- first line is session_meta; ~/.codex/session_index.jsonl indexes threads
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .. import compat, paths
from ..events import (
    AssistantMessage,
    NormalizedEvent,
    SessionContext,
    SystemEvent,
    Thinking,
    ToolCall,
    ToolResult,
    UserMessage,
)
from ..util import append_jsonl_fsync, iso_now_ms, uuid7
from .base import HarnessAdapter, UsageMeter, UsageSnapshot


class _CodexUsageMeter(UsageMeter):
    """Codex precomputes both numbers in event_msg/token_count: cumulative
    (total_token_usage), the last request (≈ current context), and — unlike
    claude — the model's context window, so the bar can show an exact
    percent. `info` is null on rate-limit-only events. input_tokens already
    includes cached_input_tokens (live: total = input + output exactly) and
    reasoning is a subset of output_tokens, so both sides read straight."""

    def __init__(self):
        self._input = 0
        self._output = 0
        self._ctx: int | None = None
        self._pct: int | None = None

    def feed(self, raw: Any) -> None:
        if not isinstance(raw, dict) or raw.get("type") != "event_msg":
            return
        payload = raw.get("payload") or {}
        if payload.get("type") != "token_count":
            return
        info = payload.get("info") or {}
        total = info.get("total_token_usage") or {}
        inp, out = total.get("input_tokens"), total.get("output_tokens")
        if isinstance(inp, int):
            self._input = inp
        if isinstance(out, int):
            self._output = out
        last = (info.get("last_token_usage") or {}).get("total_tokens")
        if isinstance(last, int):
            self._ctx = last
            window = info.get("model_context_window")
            self._pct = (
                round(100 * last / window)
                if isinstance(window, int) and window > 0
                else None
            )

    def snapshot(self) -> UsageSnapshot:
        return UsageSnapshot(
            ctx_tokens=self._ctx, ctx_percent=self._pct,
            input_tokens=self._input, output_tokens=self._output,
        )


_CODEX_LINE_TYPES = {
    "session_meta", "response_item", "event_msg", "turn_context",
    "world_state", "compacted",
    "token_usage_record",  # 0.153.4 per-response accounting (not model history)
}


def output_text(output: Any) -> str:
    """Flatten a `*_output` payload to the text the model saw. Codex writes
    either a plain string or (0.153: about half the time) a list of
    `{"type": "input_text", "text": ...}` blocks — consecutive chunks of one
    output, so they concatenate without a separator."""
    if isinstance(output, str):
        return output
    if isinstance(output, list):
        return "".join(
            b["text"] for b in output
            if isinstance(b, dict) and isinstance(b.get("text"), str)
        )
    return "" if output is None else str(output)


class CodexAdapter(HarnessAdapter):
    id = "codex"
    display_name = "Codex CLI"
    binary = "codex"
    install_hint = "npm install -g @openai/codex"

    # -- environment ---------------------------------------------------------

    def detect_version(self) -> str | None:
        return compat.detect_cli_version(self.binary)

    def version_supported(self, version_text: str) -> bool:
        return compat.version_supported("codex", version_text)

    # -- session files -------------------------------------------------------

    def transcript_path(self, cwd: str, session_id: str) -> Path | None:
        return paths.find_codex_rollout(session_id)

    def mint_session_id(self) -> str:
        return uuid7()

    def _validate_entries(self, entries, session_id) -> list[str]:
        problems = []
        first = entries[0][1]
        if first.get("type") != "session_meta":
            problems.append("first line is not session_meta")
        else:
            payload = first.get("payload") or {}
            meta_id = payload.get("id") or payload.get("session_id")
            if session_id and meta_id != session_id:
                problems.append(f"session_meta id {meta_id!r} != {session_id!r}")
        for i, e in entries:
            etype = e.get("type")
            if etype not in _CODEX_LINE_TYPES:
                problems.append(f"line {i}: unknown line type {etype!r}")
                continue
            if "payload" not in e:
                problems.append(f"line {i}: missing payload")
            if etype == "response_item":
                p = e.get("payload") or {}
                if p.get("type") == "message" and not isinstance(p.get("content"), list):
                    problems.append(f"line {i}: message content is not a list")
        if not any(e.get("type") == "response_item" for _, e in entries):
            problems.append("no response_item entries (model context would be empty)")
        return problems

    def session_meta(
        self, cwd: str, session_id: str, originator: str = "tandem"
    ) -> dict[str, Any]:
        """The session_meta first line codex expects for a rollout tandem
        authored. `originator` must stay one of runner._TANDEM_ORIGINATORS:
        rollout discovery skips exactly those, which is what keeps a
        tandem-written rollout from being adopted as a freshly minted codex
        session ("tandem-sub" marks throwaway subagent rollouts)."""
        version = "0.0.0"
        raw = self.detect_version()
        if raw:
            parsed = compat.parse_version(raw)
            if parsed:
                version = ".".join(str(x) for x in parsed)
        ts = iso_now_ms()
        return {
            "timestamp": ts,
            "type": "session_meta",
            "payload": {
                "session_id": session_id,
                "id": session_id,
                "timestamp": ts,
                "cwd": cwd,
                "originator": originator,
                "cli_version": version,
                "source": "exec",
                "thread_source": "user",
                # codex >= 0.145 interactive thread/resume rejects rollouts
                # without a provider id; "openai" is codex's built-in default
                "model_provider": "openai",
                "history_mode": "legacy",
            },
        }

    def rollout_path(self, session_id: str) -> Path:
        """Where a rollout tandem writes for `session_id` lives — codex's own
        naming (day directory + timestamped filename embedding the id)."""
        now = datetime.now(timezone.utc)
        day_dir = paths.codex_sessions_dir() / now.strftime("%Y/%m/%d")
        return day_dir / f"rollout-{now.strftime('%Y-%m-%dT%H-%M-%S')}-{session_id}.jsonl"

    def create_shadow_transcript(
        self, cwd: str, session_id: str, ctx: SessionContext, note: str
    ) -> Path:
        """Create a rollout file shaped like codex's own, headed by a
        session_meta whose originator marks it as tandem-created."""
        path = self.rollout_path(session_id)
        entries = [self.session_meta(cwd, session_id)] + self.render_note(note)
        append_jsonl_fsync(path, entries)
        return path

    def render_note(self, note: str) -> list[dict[str, Any]]:
        """A tandem-authored informational line, as both the model-facing
        response_item and the UI-facing event_msg."""
        ts = iso_now_ms()
        return [
            {
                "timestamp": ts,
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": note}],
                },
            },
            {
                "timestamp": ts,
                "type": "event_msg",
                "payload": {
                    "type": "user_message",
                    "message": note,
                    "images": [],
                    "local_images": [],
                    "audio": [],
                    "local_audio": [],
                    "text_elements": [],
                },
            },
        ]

    # -- launching -----------------------------------------------------------

    def interactive_argv(self, session_id: str | None, fresh: bool) -> list[str]:
        if fresh and session_id is None:
            # codex mints its own id; tandem captures the new rollout file.
            return [self.binary]
        assert session_id
        return [self.binary, "resume", session_id]

    def oneoff_argv(self, session_id: str, prompt: str) -> list[str]:
        return [self.binary, "exec", "--skip-git-repo-check", "resume", session_id, prompt]

    def hook_argv_extra(self, sentinel: Path) -> list[str]:
        # A -c notify=... override would silently replace a notify handler
        # the user already configured (observed in the wild: Codex Computer
        # Use registers one). In that case rely on fs-watching alone.
        try:
            import tomllib

            with open(paths.codex_home() / "config.toml", "rb") as f:
                if tomllib.load(f).get("notify"):
                    return []
        except (OSError, ValueError):
            pass
        # notify appends a JSON payload argument; route through sh -c so it
        # lands in $0 and is ignored.
        return ["-c", f'notify=["/bin/sh","-c","touch \'{sentinel}\'"]']

    def quit_keystrokes(self) -> list[bytes]:
        return [b"\x03", b"\x03", b"\x04"]

    def make_usage_meter(self) -> UsageMeter:
        return _CodexUsageMeter()

    # -- parsing -------------------------------------------------------------

    def parse_entry(self, raw: dict[str, Any], ctx: SessionContext) -> list[NormalizedEvent]:
        """One rollout line -> normalized events.

        Sources of truth per event class (to avoid the duplication built into
        the rollout format):
        - user prompts:      event_msg/user_message (response_item role=user
                             also carries injected env context, so it's skipped)
        - assistant text:    response_item/message role=assistant
        - tool activity:     response_item function_call / custom_tool_call and
                             their *_output lines; event_msg/patch_apply_end
                             only enriches the pending call with structured
                             change data.
        """
        ts = raw.get("timestamp")
        etype = raw.get("type")
        payload = raw.get("payload") or {}
        ptype = payload.get("type")

        def sysev(subtype: str, text: str = "") -> SystemEvent:
            return SystemEvent(
                source="codex", timestamp=ts, turn_index=ctx.turn_index,
                subtype=subtype, text=text,
            )

        if etype == "response_item":
            if ptype == "message":
                role = payload.get("role")
                if role != "assistant":
                    return [sysev(f"context_message:{role}")]
                text = "\n".join(
                    b.get("text", "")
                    for b in payload.get("content") or []
                    if isinstance(b, dict) and b.get("type") == "output_text"
                )
                phase = "final" if payload.get("phase") == "final_answer" else "commentary"
                return [
                    AssistantMessage(
                        source="codex", timestamp=ts, turn_index=ctx.turn_index,
                        text=text, phase=phase,
                    )
                ]
            if ptype == "reasoning":
                return [Thinking(source="codex", timestamp=ts, turn_index=ctx.turn_index)]
            if ptype in ("function_call", "custom_tool_call"):
                return [
                    ToolCall(
                        source="codex", timestamp=ts, turn_index=ctx.turn_index,
                        call_id=payload.get("call_id", ""),
                        tool=payload.get("name", ""),
                        arguments=payload.get("arguments", payload.get("input", "")),
                    )
                ]
            if ptype in ("function_call_output", "custom_tool_call_output"):
                call_id = payload.get("call_id")
                pending = ctx.pending_calls.get(call_id or "", {})
                structured = pending.pop("_structured", None) if pending else None
                return [
                    ToolResult(
                        source="codex", timestamp=ts, turn_index=ctx.turn_index,
                        call_id=call_id, output=output_text(payload.get("output")),
                        structured=structured,
                    )
                ]
            return [sysev(f"response_item:{ptype}")]

        if etype == "event_msg":
            if ptype == "task_started":
                ctx.turn_index += 1
                return [sysev("turn_start")]
            if ptype == "user_message":
                return [
                    UserMessage(
                        source="user", timestamp=ts, turn_index=ctx.turn_index,
                        text=payload.get("message", ""),
                    )
                ]
            if ptype == "patch_apply_end":
                call_id = payload.get("call_id")
                if call_id and call_id in ctx.pending_calls:
                    ctx.pending_calls[call_id]["_structured"] = {
                        "success": payload.get("success"),
                        "changes": payload.get("changes"),
                    }
                return [sysev("patch_apply_end")]
            if ptype == "task_complete":
                return [sysev("turn_end", payload.get("last_agent_message") or "")]
            # agent_message duplicates response_item/message; token_count etc.
            return [sysev(f"event_msg:{ptype}")]

        # session_meta, turn_context, world_state, unknown
        return [sysev(f"codex:{etype}")]

    # -- rendering (shadow append) -------------------------------------------

    def render_events(
        self, events: list[NormalizedEvent], ctx: SessionContext
    ) -> list[dict[str, Any]]:
        """Normalized events -> rollout lines. User prompts get both the
        model-facing response_item and the UI-facing event_msg (codex's own
        writer does the same); assistant text gets a response_item so it
        lands in the model's context on resume.

        Tool activity is rendered as native pairs (response_item only, no
        event_msg): dict arguments -> function_call with JSON-stringified
        arguments, str arguments -> custom_tool_call with input; the matching
        result follows as function_call_output / custom_tool_call_output."""
        out: list[dict[str, Any]] = []
        custom_ids: set[str] = set()  # calls rendered as custom_tool_call
        for ev in events:
            ts = ev.timestamp or iso_now_ms()
            if ev.kind == "user_message":
                out.append(
                    {
                        "timestamp": ts,
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": [{"type": "input_text", "text": ev.text}],
                        },
                    }
                )
                out.append(
                    {
                        "timestamp": ts,
                        "type": "event_msg",
                        "payload": {
                            "type": "user_message",
                            "message": ev.text,
                            "images": [],
                            "local_images": [],
                            "audio": [],
                            "local_audio": [],
                            "text_elements": [],
                        },
                    }
                )
            elif ev.kind == "assistant_message":
                phase = "final_answer" if ev.phase == "final" else "commentary"
                out.append(
                    {
                        "timestamp": ts,
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": ev.text}],
                            "phase": phase,
                        },
                    }
                )
                out.append(
                    {
                        "timestamp": ts,
                        "type": "event_msg",
                        "payload": {
                            "type": "agent_message",
                            "message": ev.text,
                            "phase": phase,
                            "memory_citation": None,
                        },
                    }
                )
            elif ev.kind == "tool_call":
                if isinstance(ev.arguments, str):
                    custom_ids.add(ev.call_id)
                    payload: dict[str, Any] = {
                        "type": "custom_tool_call", "name": ev.tool,
                        "input": ev.arguments, "call_id": ev.call_id,
                    }
                else:
                    payload = {
                        "type": "function_call", "name": ev.tool,
                        "arguments": json.dumps(ev.arguments, ensure_ascii=False),
                        "call_id": ev.call_id,
                    }
                out.append({"timestamp": ts, "type": "response_item", "payload": payload})
            elif ev.kind == "tool_result":
                ptype = ("custom_tool_call_output" if ev.call_id in custom_ids
                         else "function_call_output")
                out.append({
                    "timestamp": ts, "type": "response_item",
                    "payload": {"type": ptype, "call_id": ev.call_id,
                                "output": ev.output},
                })
        return out

    def render_placeholder(self, text: str, ctx: SessionContext) -> list[dict[str, Any]]:
        return self.render_note(text)
