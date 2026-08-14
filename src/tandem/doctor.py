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

def validate_transcript(harness: str, path: Path, session_id: str | None) -> list[str]:
    """Return a list of problems (empty = looks resumable). Thin shim: the
    structural check lives on the adapter (each harness owns its own entry
    shapes; storage-backed adapters override the whole method)."""
    from .harness import get_adapter

    return get_adapter(harness).validate_transcript(path, session_id)


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
    from .config import load_harnesses
    from .harness import ADAPTERS, get_adapter

    report = DoctorReport()

    for hid in load_harnesses():
        if hid not in ADAPTERS:
            continue   # no adapter in this build: not even an info line
        adapter = get_adapter(hid)
        v = adapter.detect_version()
        if v is None:
            # doctor is the single place absence is mentioned (spec:
            # not-installed is a normal state, not a degraded one)
            report.ok(f"{adapter.display_name}: not installed (not a participant)")
        elif not adapter.version_supported(v):
            report.warn(
                f"{adapter.display_name}: version {v!r} outside supported range "
                f"(tested against {compat.COMPAT[hid].tested}); session formats "
                f"may have drifted — treat sync results with suspicion"
            )
        else:
            report.ok(f"{adapter.display_name}: {v}")
            ok, reason = adapter.runtime_ready()
            if not ok:
                report.warn(f"{adapter.display_name}: installed but unusable — {reason}")

    if session is None:
        report.fail("no tandem session for this directory (run `tandem` to start one)")
        return report

    report.ok(
        f"paired session {session.tandem_id} (active: "
        f"{get_adapter(session.active).display_name})"
    )

    transcripts: dict[str, Path | None] = {}
    for hid in session.participants:
        adapter = get_adapter(hid)
        sid = session.native_id(hid)
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

    for source in session.participants:
        for target in session.targets_for(source):
            cursor = store.get_cursor(session.tandem_id, source, target)
            if cursor.pending.get("intent"):
                report.warn(
                    f"sync {source}->{target}: unresolved write intent (crash "
                    f"during append); it will be resolved automatically on "
                    f"next sync"
                )
            if cursor.failed_turns:
                report.warn(
                    f"sync {source}->{target}: {cursor.failed_turns} turn(s) "
                    f"fell back to placeholders"
                )
        # any direction shows the same source-side lag — good enough here
        target = session.targets_for(source)[0]
        behind = ops.unsynced_lines(session, store, source, target)
        if behind and source == session.active:
            report.warn(
                f"sync from {source}: {behind} line(s) awaiting translation "
                f"(run `tandem sync`)"
            )

    # Written best-effort by the runner when the frame gave up on the status
    # bar; nothing deletes it, so the warning repeats until the user does.
    bar_marker = paths.tandem_home() / "tmp" / f"{session.tandem_id}-bar-dropped"
    if bar_marker.exists():
        report.warn(
            "the status bar was auto-disabled in a previous session"
            " (terminal conflict) — set [frame] bar = false to keep it off,"
            " or delete the marker to retry: " + str(bar_marker)
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
    """Real resumes: one tiny model call per participant, run in the cwd."""
    from .harness import get_adapter

    prompt = "tandem doctor live check - reply with exactly: ok"
    for hid in session.participants:
        sid = session.native_id(hid)
        if sid and transcripts.get(hid):
            argv = get_adapter(hid).oneoff_argv(sid, prompt)
            _live_one(report, hid, argv, session.cwd)
        else:
            report.warn(f"{hid}: skipping live resume (no transcript yet)")


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
