"""Codex CLI adapter.

Session format observed on codex-cli 0.145.0 (docs/formats.md):
- rollout: ~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<uuidv7>.jsonl
- each line {timestamp, type, payload}; model-facing history is the
  response_item lines, UI history is the event_msg lines
- first line is session_meta; ~/.codex/session_index.jsonl indexes threads
"""

from __future__ import annotations

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
from .base import HarnessAdapter


class CodexAdapter(HarnessAdapter):
    id = "codex"
    display_name = "Codex CLI"
    binary = "codex"

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

    def create_shadow_transcript(
        self, cwd: str, session_id: str, ctx: SessionContext, note: str
    ) -> Path:
        """Create a rollout file shaped like codex's own, headed by a
        session_meta whose originator marks it as tandem-created."""
        now = datetime.now(timezone.utc)
        day_dir = paths.codex_sessions_dir() / now.strftime("%Y/%m/%d")
        fname = f"rollout-{now.strftime('%Y-%m-%dT%H-%M-%S')}-{session_id}.jsonl"
        path = day_dir / fname
        version = "0.0.0"
        raw = self.detect_version()
        if raw:
            parsed = compat.parse_version(raw)
            if parsed:
                version = ".".join(str(x) for x in parsed)
        ts = iso_now_ms()
        meta = {
            "timestamp": ts,
            "type": "session_meta",
            "payload": {
                "session_id": session_id,
                "id": session_id,
                "timestamp": ts,
                "cwd": cwd,
                "originator": "tandem",
                "cli_version": version,
                "source": "exec",
                "thread_source": "user",
                "history_mode": "legacy",
            },
        }
        entries = [meta] + self.render_note(note)
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
                output = payload.get("output", "")
                if not isinstance(output, str):
                    output = str(output)
                return [
                    ToolResult(
                        source="codex", timestamp=ts, turn_index=ctx.turn_index,
                        call_id=call_id, output=output, structured=structured,
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
        writer does the same); assistant text and action summaries get
        response_items so they land in the model's context on resume."""
        out: list[dict[str, Any]] = []
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
        return out

    def render_placeholder(self, text: str, ctx: SessionContext) -> list[dict[str, Any]]:
        return self.render_note(text)
