"""The navigator: a second harness that reviews each substantive chat turn
on a private fork of its own shadow and says nothing unless it would block
a PR. This module holds the parts that need no process: the facts one turn
leaves behind, the gate that decides whether they deserve a review, the
prompt and schema, the verdict parser, the log, and the worker that strings
them together around a Reviewer (chat/reviewers.py)."""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .. import paths
from .events import Evidence, LiveEvent, TextDelta, ToolFinished, ToolStarted, Verdict

# the command tool as each client names it (tandem's own labels for codex)
COMMAND_TOOLS = frozenset({"Bash", "exec", "bash", "shell"})
# a completion claim in the final text — a heuristic, stated as one in the spec
CLAIM_RE = re.compile(r"\b(done|fixed|passing|passes|implemented|completed?|works now)\b", re.I)
_FINAL_TEXT_CHARS = 2000
_TANDEM_PREFIX = "[tandem"


@dataclass
class TurnFacts:
    harness: str
    prompt: str                    # the user's text, trailer excluded
    carried_note: bool             # a navigator note rode this prompt
    first_turn: bool               # the session's first turn (shadows seeded just now)
    status: str                    # TurnOutcome.status
    paths: tuple[str, ...]         # union of ToolStarted.paths
    commands: int                  # command tools started
    failed_tools: int              # ToolFinished(ok=False)
    final_text: str                # last 2000 chars of the turn's TextDelta
    started: float
    ended: float


class FactsCollector:
    """Wraps the dispatcher's emit for one turn: every event is forwarded
    untouched and the few the gate needs are counted on the way past."""

    def __init__(self, harness: str, prompt: str, carried_note: bool, first_turn: bool,
                 emit: Callable[[LiveEvent], None], clock: Callable[[], float] = time.monotonic):
        self._forward, self._clock = emit, clock
        self._facts = TurnFacts(harness, prompt, carried_note, first_turn, "", (), 0, 0, "",
                                clock(), clock())
        self._paths: list[str] = []
        self._text = ""

    def emit(self, ev: LiveEvent) -> None:
        if isinstance(ev, ToolStarted):
            if ev.paths:
                self._paths += [p for p in ev.paths if p not in self._paths]
            elif ev.tool in COMMAND_TOOLS:
                self._facts.commands += 1
        elif isinstance(ev, ToolFinished):
            if not ev.ok:
                self._facts.failed_tools += 1
        elif isinstance(ev, TextDelta):
            self._text = (self._text + ev.text)[-_FINAL_TEXT_CHARS:]
        self._forward(ev)

    def finish(self, status: str) -> TurnFacts:
        f = self._facts
        f.status, f.paths, f.final_text, f.ended = status, tuple(self._paths), self._text, self._clock()
        return f


def gate(facts: TurnFacts, *, navigator: str, headroom_ok: bool, interval_ok: bool,
         disabled: bool) -> str:
    """'' when the turn deserves a review, else 'skip:<reason>' — the reason
    is what the log records, so every branch names one."""
    if disabled:
        return "skip:disabled"
    if facts.harness == navigator:
        return "skip:own-turn"
    if facts.prompt.lstrip().startswith(_TANDEM_PREFIX):
        return "skip:tandem-prompt"
    if facts.first_turn:
        return "skip:first-turn"
    if facts.status != "completed":
        return f"skip:{facts.status}"
    if not headroom_ok:
        return "skip:headroom"
    if not interval_ok:
        return "skip:interval"
    if facts.paths or facts.failed_tools:
        return ""
    if not facts.carried_note and CLAIM_RE.search(facts.final_text):
        return ""
    return "skip:quiet"


NOTE_CHARS = 400
_DIFF_CAP = 20_000

SCHEMA: dict = {
    "type": "object",
    "required": ["verdict"],
    "properties": {
        "verdict": {"enum": ["clean", "speak"]},
        "severity": {"enum": ["block", "warn"]},
        "note": {"type": "string", "maxLength": NOTE_CHARS},
        "evidence": {"type": "array", "items": {
            "type": "object", "required": ["file", "line"],
            "properties": {"file": {"type": "string"}, "line": {"type": "integer"},
                           "why": {"type": "string"}}}},
    },
}

_PROMPT = """[tandem navigator] You are reviewing the assistant turn immediately above this message, which ran on {harness}. It touched: {paths}.
Its diff (may include earlier uncommitted changes in this tree):
{diff}
Speak only if you would block a pull request over something in that turn: a bug it introduced, a claim it made that its own output contradicts, a failing command it ignored. Do not restate the turn. Do not raise style.
Reply in the required schema. If nothing rises to that bar, verdict is "clean" and note is empty."""


def build_prompt(facts: TurnFacts, diff: str) -> str:
    return _PROMPT.format(harness=facts.harness,
                          paths=", ".join(facts.paths) if facts.paths else "no files",
                          diff=diff or "(no diff)")


_JSON_RE = re.compile(r"\{.*\}", re.S)


def _json_in(text: str):
    m = _JSON_RE.search(text or "")
    if m is None:
        raise ValueError("no JSON object in the reply")
    return json.loads(m.group(0))


def parse_verdict(structured, text: str, *, navigator: str, model: str, elapsed: float) -> Verdict:
    """The model's reply as a Verdict. Anything that does not fit the schema
    is an `error` verdict with the reason — never an exception."""
    base = dict(navigator=navigator, model=model, elapsed=elapsed)
    try:
        obj = structured if isinstance(structured, dict) else _json_in(text)
        if not isinstance(obj, dict):
            raise ValueError("reply is not an object")
        verdict = obj.get("verdict")
        if verdict not in ("clean", "speak"):
            raise ValueError(f"verdict {verdict!r} is not clean|speak")
        if verdict == "clean":
            return Verdict("clean", **base)
        note = str(obj.get("note") or "").strip()[:NOTE_CHARS]
        if not note:
            return Verdict("empty", **base)
        severity = obj.get("severity") if obj.get("severity") in ("block", "warn") else ""
        raw = obj.get("evidence") or []
        if not isinstance(raw, list):
            raise ValueError("evidence is not a list")
        evidence = []
        for e in raw:
            if not isinstance(e, dict) or "file" not in e or "line" not in e:
                raise ValueError("evidence item needs file and line")
            evidence.append(Evidence(str(e["file"]), int(e["line"]), str(e.get("why") or "")))
        return Verdict("speak", severity=severity, note=note, evidence=tuple(evidence), **base)
    except Exception as exc:  # the contract is "never raises": overflow, recursion, anything
        return Verdict("error", error=f"unparsable verdict: {exc}", **base)


def _git(cwd: str, args: list[str], run) -> str | None:
    """stdout of a git command, or None when git is absent, the cwd is not
    a repository, or the command fails — every one of those means 'no diff'."""
    try:
        r = run(["git", "--no-pager", *args], cwd=cwd, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


def compute_diff(cwd: str, paths: tuple[str, ...], commands: int, *,
                 cap: int = _DIFF_CAP, run=subprocess.run) -> str:
    """What the reviewed turn left in the working tree: git's diff for the
    touched files plus the contents of any touched path git does not track;
    the whole tree's diff when no file was named but a command ran (it may
    have written anything). Capped; empty outside a repository."""
    top = _git(cwd, ["rev-parse", "--show-toplevel"], run)
    if not top:
        return ""
    parts: list[str] = []
    if paths:
        # git refuses the whole pathspec if one path lies outside the work
        # tree, so keep only the paths inside it (relative to cwd)
        root, here = Path(top.strip()).resolve(), Path(cwd).resolve()
        inside = []
        for p in paths:
            try:
                full = (here / p).resolve()
            except (OSError, RuntimeError):   # a symlink loop, say
                continue
            if full == root or root in full.parents:
                inside.append(os.path.relpath(full, here))
        d = _git(cwd, ["diff", "--no-color", "--", *inside], run) if inside else None
        if d:
            parts.append(d)
        tracked = (_git(cwd, ["ls-files", "--", *inside], run) or "") if inside else ""
        tracked_set = set(tracked.split("\n"))
        for rel in inside:
            if rel not in tracked_set:
                try:
                    body = Path(cwd, rel).read_text(errors="replace")
                except OSError:
                    continue
                parts.append(f"--- {rel} (untracked)\n{body}")
    elif commands:
        status = _git(cwd, ["status", "--porcelain"], run)
        d = _git(cwd, ["diff", "--no-color"], run)
        if status:
            parts.append(status)
        if d:
            parts.append(d)
    out = "\n".join(parts)
    return out if len(out) <= cap else out[:cap] + "\n… (truncated)"


def log_path(tandem_id: str) -> Path:
    return paths.tandem_home() / "navigator" / f"{tandem_id}.jsonl"


class NavigatorLog:
    """One JSON line per gated turn, plus `ridden` and `feedback` lines that
    point back at a review by its `ts`. Append-only; a torn last line is
    skipped on read."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._last_ts = ""

    def _ts(self) -> str:
        ts = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        if ts <= self._last_ts:                     # same microsecond: keep refs unique
            ts = self._last_ts + "0"
        self._last_ts = ts
        return ts

    def _append(self, record: dict) -> str:
        with self._lock:
            record = {"ts": self._ts(), **record}
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with open(self.path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            except OSError:
                pass                                # the log is a courtesy, never a blocker
            return record["ts"]

    def review(self, facts: TurnFacts, gate_reason: str, verdict: Verdict | None) -> str:
        v = verdict or Verdict("")
        return self._append({
            "kind": "review", "turn_harness": facts.harness,
            "prompt": " ".join(facts.prompt.split())[:120],
            "gate": gate_reason or "review", "verdict": v.verdict, "severity": v.severity,
            "note": v.note,
            "evidence": [{"file": e.file, "line": e.line, "why": e.why} for e in v.evidence],
            "elapsed": round(v.elapsed, 2), "navigator": v.navigator, "model": v.model,
            "error": v.error,
        })

    def ridden(self, ref: str, to: str) -> None:
        self._append({"kind": "ridden", "ref": ref, "to": to})

    def feedback(self, ref: str, value: str) -> None:
        self._append({"kind": "feedback", "ref": ref, "value": value})

    @staticmethod
    def read(path: Path) -> list[dict]:
        out: list[dict] = []
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return out
        for line in lines:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict):
                out.append(rec)
        return out

    @staticmethod
    def stats(records: list[dict]) -> dict:
        reviews = [r for r in records if r.get("kind") == "review"]
        reviewed = [r for r in reviews if r.get("gate") == "review"]
        spoken = [r for r in reviewed if r.get("verdict") == "speak"]
        spoken_ts = {r.get("ts") for r in spoken}
        marks = [r.get("value") for r in records
                 if r.get("kind") == "feedback" and r.get("ref") in spoken_ts]
        good, bad = marks.count("good"), marks.count("bad")
        return {"reviewed": len(reviewed), "spoken": len(spoken),
                "skipped": len(reviews) - len(reviewed), "good": good, "bad": bad,
                "helpful": good / (good + bad) if good + bad else None}
