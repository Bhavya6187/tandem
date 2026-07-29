"""Transcript validation ("dry resume"): structural checks that a session
file is something its CLI can load, without spending a model call.

`tandem doctor` (M5) layers version checks, pairing checks and an optional
--live mode (real one-word resume, costs one model call per harness) on top
of validate_transcript.
"""

from __future__ import annotations

import json
from pathlib import Path

_CLAUDE_ENTRY_TYPES = {
    "user", "assistant", "attachment", "system", "summary",
    "queue-operation", "last-prompt", "progress", "file-history-snapshot",
    "mode",  # {"type":"mode","mode":"normal",...} observed on --resume runs
}
_CODEX_LINE_TYPES = {
    "session_meta", "response_item", "event_msg", "turn_context",
    "world_state", "compacted",
}


def validate_transcript(harness: str, path: Path, session_id: str | None) -> list[str]:
    """Return a list of problems (empty = looks resumable)."""
    problems: list[str] = []
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return [f"cannot read transcript: {exc}"]
    if not raw.strip():
        return ["transcript is empty"]

    entries: list[tuple[int, dict]] = []
    for i, line in enumerate(raw.splitlines()):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            problems.append(f"line {i}: not valid JSON")
            continue
        if not isinstance(obj, dict):
            problems.append(f"line {i}: not a JSON object")
            continue
        entries.append((i, obj))
    if not entries:
        return problems + ["no parseable entries"]

    if harness == "claude":
        problems += _validate_claude(entries, session_id)
    else:
        problems += _validate_codex(entries, session_id)
    return problems


def _validate_claude(entries: list[tuple[int, dict]], session_id: str | None) -> list[str]:
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


def _validate_codex(entries: list[tuple[int, dict]], session_id: str | None) -> list[str]:
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
