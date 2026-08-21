"""Claude Code adapter.

Session format observed on claude 2.1.220 (docs/formats.md):
- transcript: ~/.claude/projects/<munged-cwd>/<sessionId>.jsonl
- entries chained by uuid/parentUuid; types: user, assistant, attachment,
  queue-operation, last-prompt, summary, system
- assistant API messages are split one content block per JSONL line, sharing
  message.id
"""

from __future__ import annotations

import json
import os
import uuid as uuidlib
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
from ..util import append_jsonl_fsync, git_branch, iso_now_ms
from .base import HarnessAdapter, UsageMeter, UsageSnapshot


class _ClaudeUsageMeter(UsageMeter):
    """Claude writes one transcript entry per content block, each repeating
    the whole message's usage with output_tokens still growing — so totals
    keep the *latest* usage per message id (parallel Task sidechains
    interleave ids, which rules out commit-on-id-change). Context fill is
    the latest main-chain prompt side: input + cache read + cache write.
    Sidechains are real spend but a different context window, so they count
    toward the total and never move ctx."""

    def __init__(self):
        self._by_id: dict[str, tuple[int, int]] = {}
        self._input = 0
        self._output = 0
        self._ctx: int | None = None

    def feed(self, raw: Any) -> None:
        if not isinstance(raw, dict) or raw.get("type") != "assistant":
            return
        msg = raw.get("message") or {}
        usage = msg.get("usage")
        if not isinstance(usage, dict):
            return

        def n(key: str) -> int:
            v = usage.get(key)
            return v if isinstance(v, int) else 0

        prompt = (n("input_tokens") + n("cache_read_input_tokens")
                  + n("cache_creation_input_tokens"))
        out = n("output_tokens")
        mid = msg.get("id")
        if isinstance(mid, str):
            seen_prompt, seen_out = self._by_id.get(mid, (0, 0))
            self._input += prompt - seen_prompt
            self._output += out - seen_out
            self._by_id[mid] = (prompt, out)
        else:
            self._input += prompt
            self._output += out
        if not raw.get("isSidechain"):
            self._ctx = prompt

    def snapshot(self) -> UsageSnapshot:
        return UsageSnapshot(ctx_tokens=self._ctx,
                             input_tokens=self._input,
                             output_tokens=self._output)


def _stringify_block_content(content: Any) -> str:
    """tool_result content is either a string or a list of content blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for c in content:
            if isinstance(c, dict) and c.get("type") == "text":
                parts.append(c.get("text", ""))
            elif isinstance(c, dict):
                parts.append(f"<{c.get('type', 'block')}>")
        return "\n".join(parts)
    return "" if content is None else str(content)


def _pid_alive(pid: int) -> bool:
    """A registry entry for a dead pid is a stale leftover (claude
    cleans up on exit, not on crash). PermissionError means alive but
    not ours — still alive; anything else unreadable counts as dead,
    because trusting a stale entry can freeze the flip on a phantom
    "busy". Callers must screen out pid <= 0 first: kill(0)/kill(-N)
    probe process groups and would read as alive."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


_CLAUDE_ENTRY_TYPES = {
    "user", "assistant", "attachment", "system", "summary",
    "queue-operation", "last-prompt", "progress", "file-history-snapshot",
    "mode",  # {"type":"mode","mode":"normal",...} observed on --resume runs
    # uuid-less metadata entries claude 2.1.220 interleaves with conversation
    "permission-mode", "ai-title", "file-history-delta", "pr-link",
    "relocated", "worktree-state",
}


class ClaudeCodeAdapter(HarnessAdapter):
    id = "claude"
    display_name = "Claude Code"
    binary = "claude"
    install_hint = "npm install -g @anthropic-ai/claude-code"
    prompt_hook_capable = True

    # -- environment ---------------------------------------------------------

    def detect_version(self) -> str | None:
        return compat.detect_cli_version(self.binary)

    def version_supported(self, version_text: str) -> bool:
        return compat.version_supported("claude", version_text)

    # -- session files -------------------------------------------------------

    def transcript_path(self, cwd: str, session_id: str) -> Path | None:
        p = paths.claude_transcript_path(cwd, session_id)
        return p if p.exists() else None

    def expected_transcript_path(self, cwd: str, session_id: str) -> Path:
        return paths.claude_transcript_path(cwd, session_id)

    def _validate_entries(self, entries, session_id) -> list[str]:
        problems = []
        seen_uuids: set[str] = set()
        convo = 0
        for i, e in entries:
            etype = e.get("type")
            if etype not in _CLAUDE_ENTRY_TYPES:
                problems.append(f"line {i}: unknown entry type {etype!r}")
                continue
            if etype in ("user", "assistant"):
                convo += 1
                if session_id and e.get("sessionId") not in (None, session_id):
                    problems.append(
                        f"line {i}: sessionId {e.get('sessionId')!r} != {session_id!r}"
                    )
                if not e.get("uuid"):
                    problems.append(f"line {i}: conversation entry missing uuid")
                parent = e.get("parentUuid")
                if parent and parent not in seen_uuids:
                    # claude tolerates forward/dangling parents poorly; flag it
                    problems.append(f"line {i}: parentUuid {parent!r} not seen earlier")
                msg = e.get("message")
                if not isinstance(msg, dict) or "content" not in msg:
                    problems.append(f"line {i}: malformed message")
            if e.get("uuid"):
                seen_uuids.add(e["uuid"])
        if convo == 0:
            problems.append("no conversation entries (user/assistant)")
        return problems

    def mint_session_id(self) -> str:
        return str(uuidlib.uuid4())

    def _base_entry(self, ctx: SessionContext, entry_type: str) -> dict[str, Any]:
        version = "0.0.0"
        raw = self.detect_version()
        if raw:
            parsed = compat.parse_version(raw)
            if parsed:
                version = ".".join(str(x) for x in parsed)
        return {
            "parentUuid": ctx.state_for("claude").get("leaf_uuid"),
            "isSidechain": False,
            "userType": "external",
            "cwd": ctx.cwd,
            "sessionId": ctx.target_session_id,
            "version": version,
            "gitBranch": git_branch(ctx.cwd),
            "type": entry_type,
            "uuid": str(uuidlib.uuid4()),
            "timestamp": iso_now_ms(),
        }

    def create_shadow_transcript(
        self, cwd: str, session_id: str, ctx: SessionContext, note: str
    ) -> Path:
        path = paths.claude_transcript_path(cwd, session_id)
        entry = self._base_entry(ctx, "user")
        entry["message"] = {"role": "user", "content": note}
        append_jsonl_fsync(path, [entry])
        ctx.state_for("claude")["leaf_uuid"] = entry["uuid"]
        return path

    # -- launching -----------------------------------------------------------

    def interactive_argv(self, session_id: str | None, fresh: bool) -> list[str]:
        assert session_id, "claude sessions are always minted up front"
        if fresh:
            return [self.binary, "--session-id", session_id]
        return [self.binary, "--resume", session_id]

    def oneoff_argv(self, session_id: str, prompt: str) -> list[str]:
        return [self.binary, "--resume", session_id, "-p", prompt]

    def model_argv(self, model: str) -> list[str]:
        return ["--model", model]

    def hook_argv_extra(self, sentinel: Path) -> list[str]:
        # Per-invocation settings JSON; Stop fires at each completed turn.
        settings = {
            "hooks": {
                "Stop": [
                    {
                        "hooks": [
                            {"type": "command", "command": f"touch '{sentinel}'"}
                        ]
                    }
                ]
            }
        }
        return ["--settings", json.dumps(settings)]

    def quit_keystrokes(self) -> list[bytes]:
        # claude ≥2.1.229 exits only on a same-key double-press; ^C both
        # clears a drafted composer and arms the confirmation, so three
        # presses cover every prompt state (live-verified 2026-08-13)
        return [b"\x03", b"\x03", b"\x03"]

    def session_status(self, session_id: str) -> str | None:
        """Live turn state from claude's session registry: "busy" while a
        turn runs, "waiting" at the prompt, None when no live entry
        matches. The flip wait treats anything but "busy" as flippable
        (single-tier by spec: eager on schema drift). Matched by
        sessionId — tandem mints claude session ids — never by pid
        filename, which a forking wrapper would break. Dead-pid entries
        are skipped; among several live matches "busy" wins, because
        flipping kills a live turn while waiting only costs a wait."""
        try:
            files = sorted(paths.claude_sessions_dir().glob("*.json"))
        except OSError:
            return None
        statuses: list[str] = []
        for p in files:
            try:
                entry = json.loads(p.read_text())
            except (OSError, ValueError):
                continue
            if not isinstance(entry, dict) or entry.get("sessionId") != session_id:
                continue
            pid = entry.get("pid")
            if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0 \
                or not _pid_alive(pid):
                continue
            status = entry.get("status")
            if isinstance(status, str):
                statuses.append(status)
        if "busy" in statuses:
            return "busy"
        return statuses[0] if statuses else None

    def make_usage_meter(self) -> UsageMeter:
        return _ClaudeUsageMeter()

    # -- parsing -------------------------------------------------------------

    def parse_entry(self, raw: dict[str, Any], ctx: SessionContext) -> list[NormalizedEvent]:
        """One transcript line -> normalized events. Never raises on shape
        surprises; anything unrecognized degrades to a SystemEvent."""
        ts = raw.get("timestamp")
        etype = raw.get("type")

        def sysev(subtype: str, text: str = "") -> SystemEvent:
            return SystemEvent(
                source="claude", timestamp=ts, turn_index=ctx.turn_index,
                subtype=subtype, text=text,
            )

        if raw.get("isSidechain"):
            return [sysev("sidechain")]

        if etype == "user":
            if raw.get("isMeta"):
                return [sysev("meta_user")]
            content = (raw.get("message") or {}).get("content")
            if isinstance(content, str):
                ctx.turn_index += 1
                return [
                    UserMessage(
                        source="user", timestamp=ts,
                        turn_index=ctx.turn_index, text=content,
                    )
                ]
            events: list[NormalizedEvent] = []
            structured = raw.get("toolUseResult")
            for block in content or []:
                btype = block.get("type")
                if btype == "tool_result":
                    events.append(
                        ToolResult(
                            source="claude", timestamp=ts,
                            turn_index=ctx.turn_index,
                            call_id=block.get("tool_use_id"),
                            output=_stringify_block_content(block.get("content")),
                            is_error=bool(block.get("is_error")),
                            structured=structured if isinstance(structured, dict) else None,
                        )
                    )
                elif btype == "text":
                    ctx.turn_index += 1
                    events.append(
                        UserMessage(
                            source="user", timestamp=ts,
                            turn_index=ctx.turn_index,
                            text=block.get("text", ""),
                        )
                    )
                else:
                    events.append(sysev(f"user_block:{btype}"))
            return events or [sysev("user_empty")]

        if etype == "assistant":
            msg = raw.get("message") or {}
            events = []
            for block in msg.get("content") or []:
                btype = block.get("type")
                if btype == "text":
                    events.append(
                        AssistantMessage(
                            source="claude", timestamp=ts,
                            turn_index=ctx.turn_index,
                            text=block.get("text", ""),
                            model=msg.get("model"),
                        )
                    )
                elif btype == "tool_use":
                    events.append(
                        ToolCall(
                            source="claude", timestamp=ts,
                            turn_index=ctx.turn_index,
                            call_id=block.get("id", ""),
                            tool=block.get("name", ""),
                            arguments=block.get("input") or {},
                        )
                    )
                elif btype in ("thinking", "redacted_thinking"):
                    events.append(
                        Thinking(source="claude", timestamp=ts, turn_index=ctx.turn_index)
                    )
                else:
                    events.append(sysev(f"assistant_block:{btype}"))
            return events or [sysev("assistant_empty")]

        if etype == "system" and raw.get("subtype") == "compact_boundary":
            return [sysev("compaction", "conversation was compacted")]

        # attachment, queue-operation, last-prompt, summary, system, unknown
        return [sysev(f"claude:{etype}")]

    # -- rendering (shadow append) -------------------------------------------

    def render_events(
        self, events: list[NormalizedEvent], ctx: SessionContext
    ) -> list[dict[str, Any]]:
        """Normalized events -> native transcript entries, chained onto the
        current leaf. Messages and tool activity both render natively;
        consecutive assistant entries share one message.id."""
        out: list[dict[str, Any]] = []
        st = ctx.state_for("claude")
        for ev in events:
            if ev.kind in ("user_message", "tool_result"):
                st["run_msg_id"] = None
            if ev.kind == "user_message":
                entry = self._base_entry(ctx, "user")
                entry["message"] = {"role": "user", "content": ev.text}
            elif ev.kind == "assistant_message":
                entry = self._base_entry(ctx, "assistant")
                entry["message"] = self._assistant_message(
                    ctx, entry, [{"type": "text", "text": ev.text}], "end_turn"
                )
            elif ev.kind == "tool_call":
                entry = self._base_entry(ctx, "assistant")
                inp = ev.arguments if isinstance(ev.arguments, dict) else {"input": ev.arguments}
                entry["message"] = self._assistant_message(
                    ctx, entry,
                    [{"type": "tool_use", "id": ev.call_id, "name": ev.tool,
                      "input": inp}],
                    "tool_use",
                )
            elif ev.kind == "tool_result":
                entry = self._base_entry(ctx, "user")
                entry["message"] = {
                    "role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": ev.call_id,
                                 "content": ev.output, "is_error": ev.is_error}],
                }
                entry["toolUseResult"] = ev.structured or {"stdout": ev.output}
            else:
                continue
            if ev.timestamp:
                entry["timestamp"] = ev.timestamp
            st["leaf_uuid"] = entry["uuid"]
            out.append(entry)
        return out

    def _assistant_message(
        self, ctx: SessionContext, entry: dict[str, Any],
        content: list[dict[str, Any]], stop_reason: str,
    ) -> dict[str, Any]:
        st = ctx.state_for("claude")
        if st.get("run_msg_id") is None:
            st["run_msg_id"] = f"msg_tandem_{entry['uuid'][:8]}"
        return {
            "role": "assistant",
            "model": st.get("model") or "<synced>",
            "id": st["run_msg_id"],
            "type": "message",
            "content": content,
            "stop_reason": stop_reason,
            "stop_sequence": None,
        }

    def render_placeholder(self, text: str, ctx: SessionContext) -> list[dict[str, Any]]:
        # a rendered user-side entry, so it ends the assistant run like any
        # other: no message.id may straddle it
        st = ctx.state_for("claude")
        st["run_msg_id"] = None
        entry = self._base_entry(ctx, "user")
        entry["message"] = {"role": "user", "content": text}
        st["leaf_uuid"] = entry["uuid"]
        return [entry]

    def _tail_objects(self, transcript: Path) -> list[dict[str, Any]]:
        try:
            with open(transcript, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - 262144))
                tail = f.read().decode("utf-8", errors="replace")
        except OSError:
            return []
        out = []
        for line in tail.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                out.append(obj)
        return out

    def derive_leaf_uuid(self, transcript: Path) -> str | None:
        """Last chainable uuid in an existing transcript (the harness itself
        may have appended since tandem last wrote)."""
        leaf = None
        for obj in self._tail_objects(transcript):
            if obj.get("uuid") and obj.get("type") in (
                "user", "assistant", "attachment", "system",
            ):
                leaf = obj["uuid"]
        return leaf

    def derive_last_model(self, transcript: Path) -> str | None:
        """Model of the last main-thread assistant entry with a real model
        name. Skips the "<synced>" placeholder (pre-fix tandem appends) and
        sidechain entries (subagents may run on a different model)."""
        model = None
        for obj in self._tail_objects(transcript):
            if obj.get("type") != "assistant" or obj.get("isSidechain"):
                continue
            m = (obj.get("message") or {}).get("model")
            if m and m != "<synced>":
                model = m
        return model

    def prepare_shadow(self, ref, ctx) -> None:
        """The claude file may have grown since tandem last wrote (its own
        CLI appends while claude is active): chain onto the real leaf and
        pick up the model claude last used for rendered entries."""
        st = ctx.state_for("claude")
        leaf = self.derive_leaf_uuid(ref)
        if leaf:
            st["leaf_uuid"] = leaf
        model = self.derive_last_model(ref)
        if model:
            st["model"] = model
