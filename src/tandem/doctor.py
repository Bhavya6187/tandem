"""tandem doctor: is this pairing healthy, and would both sessions resume?

validate_transcript() is the structural "dry resume" — it checks that a
session file is something its CLI can load without spending a model call.
run_doctor() layers version checks, pairing checks, sync-state checks and an
optional live mode (a real one-word resume per harness — costs two model
calls) on top.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

_CLAUDE_ENTRY_TYPES = {
    "user", "assistant", "attachment", "system", "summary",
    "queue-operation", "last-prompt", "progress", "file-history-snapshot",
    "mode",  # {"type":"mode","mode":"normal",...} observed on --resume runs
    # uuid-less metadata entries claude 2.1.220 interleaves with conversation
    "permission-mode", "ai-title", "file-history-delta", "pr-link",
    "relocated", "worktree-state",
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


# -- full doctor -------------------------------------------------------------


@dataclass
class Check:
    status: str  # 'ok' | 'warn' | 'fail'
    message: str


@dataclass
class DoctorReport:
    checks: list[Check] = field(default_factory=list)

    def ok(self, msg: str) -> None:
        self.checks.append(Check("ok", msg))

    def warn(self, msg: str) -> None:
        self.checks.append(Check("warn", msg))

    def fail(self, msg: str) -> None:
        self.checks.append(Check("fail", msg))

    @property
    def failed(self) -> bool:
        return any(c.status == "fail" for c in self.checks)


def run_doctor(store, session, live: bool = False) -> DoctorReport:
    from . import compat, ops, paths
    from .harness import get_adapter

    report = DoctorReport()

    for hid in ("claude", "codex"):
        adapter = get_adapter(hid)
        v = adapter.detect_version()
        if v is None:
            report.fail(f"{adapter.display_name}: not found on PATH")
        elif not adapter.version_supported(v):
            report.warn(
                f"{adapter.display_name}: version {v!r} outside supported range "
                f"(tested against {compat.COMPAT[hid].tested}); session formats "
                f"may have drifted — treat sync results with suspicion"
            )
        else:
            report.ok(f"{adapter.display_name}: {v}")

    if session is None:
        report.fail("no tandem session for this directory (run `tandem` to start one)")
        return report

    report.ok(
        f"paired session {session.tandem_id} (active: "
        f"{get_adapter(session.active).display_name})"
    )

    transcripts: dict[str, Path | None] = {}
    for hid in ("claude", "codex"):
        adapter = get_adapter(hid)
        sid = getattr(session, f"{hid}_session_id")
        if not sid:
            report.warn(f"{hid}: session id pending (harness has not run yet)")
            transcripts[hid] = None
            continue
        path = adapter.transcript_path(session.cwd, sid)
        transcripts[hid] = path
        if path is None:
            if hid == session.active:
                report.warn(f"{hid}: transcript not created yet (first run pending)")
            else:
                report.fail(f"{hid}: shadow transcript missing for session {sid}")
            continue
        problems = validate_transcript(hid, path, sid)
        if problems:
            for p in problems[:5]:
                report.fail(f"{hid}: {p}")
            if len(problems) > 5:
                report.fail(f"{hid}: ... {len(problems) - 5} more problems")
        else:
            report.ok(f"{hid}: transcript resumable by structure ({path.name})")

    for source in ("claude", "codex"):
        cursor = store.get_cursor(session.tandem_id, source)
        if cursor.pending.get("intent"):
            report.warn(
                f"sync from {source}: unresolved write intent (crash during "
                f"append); it will be resolved automatically on next sync"
            )
        if cursor.failed_turns:
            report.warn(
                f"sync from {source}: {cursor.failed_turns} turn(s) fell back "
                f"to placeholders"
            )
        behind = ops.unsynced_lines(session, store, source)
        if behind and source == session.active:
            report.warn(
                f"sync from {source}: {behind} line(s) awaiting translation "
                f"(run `tandem sync`)"
            )

    qdir = paths.quarantine_dir(session.tandem_id)
    qfiles = sorted(qdir.iterdir()) if qdir.is_dir() else []
    if qfiles:
        report.warn(
            f"{len(qfiles)} quarantined raw entr(y/ies) under {qdir}"
        )

    _subagent_checks(report, session)

    if live:
        _live_resume_checks(report, session, transcripts)
    return report


def _subagent_checks(report: DoctorReport, session) -> None:
    """Subagent hygiene for everyone who has not turned routing off. Under
    the manual default nothing reaches codex unless the user picks a bridge
    agent, but every dispatch they do make still runs on a model nobody
    chose for them, still bills whatever codex is authed as, and still sees
    only the CLAUDE.md content inside the tandem:shared block."""
    from . import paths
    from .config import load_subagents_config

    cfg = load_subagents_config()
    if cfg.route == "off":
        return
    # model="" means no `-m`, i.e. the codex account default — the frontier
    # tier on every plan seen so far. Picking `tandem:gpt` is a choice about
    # which harness runs the task, never a choice to pay top tier for it, so
    # a manual user with [subagents] model unset is billed the most
    # expensive option on every worker without having asked for it: a
    # warning, not a note. The dataclass default stays empty on purpose: a
    # baked-in id would 400 on accounts that lack it.
    if not cfg.model:
        report.warn(
            "subagents: no model configured — workers will use your codex "
            "account's default (set [subagents] model in "
            "~/.tandem/config.toml to a cheap tier)"
        )
    if os.environ.get("OPENAI_API_KEY"):
        report.warn(
            "subagents: OPENAI_API_KEY is set — codex may bill the API "
            "instead of your ChatGPT subscription"
        )
    auth_path = paths.codex_home() / "auth.json"
    try:
        auth = json.loads(auth_path.read_text())
    except (OSError, ValueError):
        auth = None
    # valid JSON that is not an object would make .get raise AttributeError,
    # which no doctor check may do: an unreadable auth.json is a skipped
    # check, never a traceback.
    if isinstance(auth, dict) and auth.get("OPENAI_API_KEY") and not auth.get("tokens"):
        report.warn(
            "subagents: codex auth is API-key based — subagent runs "
            "will bill the API, not the subscription"
        )
    claude_md = Path(session.cwd) / "CLAUDE.md"
    try:
        if "tandem:shared:begin" not in claude_md.read_text():
            report.warn(
                "subagents: CLAUDE.md has no tandem:shared block — project "
                "rules will not reach codex workers (move subagent-relevant "
                "rules into the shared block)"
            )
    except OSError:
        pass


def _live_resume_checks(report: DoctorReport, session, transcripts) -> None:
    """Real resumes: one tiny model call per harness, run in the session cwd."""
    from .harness import get_adapter

    prompt = "tandem doctor live check - reply with exactly: ok"
    if session.claude_session_id and transcripts.get("claude"):
        argv = get_adapter("claude").oneoff_argv(session.claude_session_id, prompt)
        _live_one(report, "claude", argv, session.cwd)
    else:
        report.warn("claude: skipping live resume (no transcript yet)")
    if session.codex_session_id and transcripts.get("codex"):
        argv = get_adapter("codex").oneoff_argv(session.codex_session_id, prompt)
        _live_one(report, "codex", argv, session.cwd)
    else:
        report.warn("codex: skipping live resume (no transcript yet)")


def _live_one(report: DoctorReport, hid: str, argv: list[str], cwd: str) -> None:
    try:
        out = subprocess.run(
            argv, cwd=cwd, capture_output=True, text=True, timeout=180
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        report.fail(f"{hid}: live resume errored: {exc}")
        return
    if out.returncode == 0:
        report.ok(f"{hid}: live resume succeeded")
    else:
        tail = (out.stderr or out.stdout or "").strip().splitlines()
        report.fail(
            f"{hid}: live resume exited {out.returncode}"
            + (f" - {tail[-1][:160]}" if tail else "")
        )
