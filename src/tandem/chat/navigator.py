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
from typing import Callable, Protocol

from .. import paths
from .events import (Evidence, LiveEvent, ReviewFinished, ReviewStarted, TextDelta, ToolFinished,
                     ToolStarted, Verdict)

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

# codex's strict output mode needs every property required and additionalProperties: false,
# so `severity` carries "" for a clean verdict and `why` is "" when there is nothing to say
SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["verdict", "severity", "note", "evidence"],
    "properties": {
        "verdict": {"type": "string", "enum": ["clean", "speak"]},
        "severity": {"type": "string", "enum": ["block", "warn", ""]},
        "note": {"type": "string", "maxLength": NOTE_CHARS},
        "evidence": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["file", "line", "why"],
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


def headroom_ok(usage_state: dict, harness: str, floor: int) -> bool:
    """The navigator's five-hour rate-limit window (the one labelled "5h",
    wherever the source lists it; the first window when none is) has at
    least `floor` percent left. No parsed data — polling off, API-key login,
    nothing fetched yet — means the floor is not enforced: the bar text
    alone is not data."""
    wins = (usage_state.get("windows") or {}).get(harness) or []
    if not wins:
        return True
    _, used = next((w for w in wins if w[0] == "5h"), wins[0])
    return 100 - int(used) >= floor


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
            except (OSError, ValueError, TypeError):  # ValueError: a lone surrogate the file can't encode
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
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
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


class ReviewError(RuntimeError):
    """A review that could not run: no shadow, the fork failed, the
    process died. Logged as an error verdict, never raised past the worker."""


@dataclass
class ReviewResult:
    structured: object | None   # claude's structured_output, or None
    text: str                   # the final assistant text (codex puts its JSON here)


class Reviewer(Protocol):
    harness: str

    def review(self, session, model: str, prompt: str, schema: dict,
               shadow_lock: threading.Lock) -> ReviewResult: ...

    def cancel(self) -> None: ...     # kill the running review; stay usable

    def close(self) -> None: ...      # the window quit: kill and start nothing more


@dataclass(frozen=True)
class Note:
    ref: str                    # the log record it came from
    navigator: str
    turn_harness: str
    verdict: Verdict

    @property
    def summary(self) -> str:
        head = self.verdict.note.split("\n", 1)[0]
        return head if len(head) <= 60 else head[:59] + "…"

    def trailer(self) -> str:
        """Appended after the user's text: their words lead, so titles and
        the `[tandem` skip rules are untouched."""
        lines = [f"[tandem navigator] {self.navigator} reviewed the previous {self.turn_harness} "
                 f"turn and flagged ({self.verdict.severity or 'note'}): {self.verdict.note}"]
        lines += [f"{e.file}:{e.line}" + (f" — {e.why}" if e.why else "") for e in self.verdict.evidence]
        return "\n\n" + "\n".join(lines)


_MAX_FAILURES = 3
# a review still running after this many seconds is cancelled (its process
# killed, the reviewer kept): the error verdict it ends in counts as one failure
REVIEW_TIMEOUT = 120.0


class Navigator:
    """One review in flight, one pending slot (newest wins), one pending
    note. `turn_ended` is called on the dispatcher's worker after sync and
    returns at once; the review runs on this object's own thread and posts
    ReviewStarted / ReviewFinished through the window's queue."""

    def __init__(self, harness: str, cfg, reviewer: Reviewer, post: Callable[[LiveEvent], None],
                 log: NavigatorLog, *, headroom: Callable[[], bool] = lambda: True,
                 clock: Callable[[], float] = time.monotonic, diff=compute_diff):
        self.harness, self.cfg, self.reviewer, self.post, self.log = harness, cfg, reviewer, post, log
        self._headroom, self._clock, self._diff = headroom, clock, diff
        self.shadow_lock = threading.Lock()
        self._lock = threading.Lock()
        self._running = False
        self._thread: threading.Thread | None = None
        self._pending: tuple[TurnFacts, object] | None = None
        self._note: Note | None = None
        # the ref of the last note spoken, kept after the note rides a prompt
        # so `/note good|bad` can still mark it
        self._last_spoken_ref: str | None = None
        self._spoken_evidence: set[tuple[str, int]] = set()
        self._last_spoken = float("-inf")
        self._failures = 0
        self._disabled = False
        self._closed = False

    # -- what the dispatcher and the window ask ------------------------------

    def turn_ended(self, facts: TurnFacts, session) -> None:
        try:
            reason = gate(facts, navigator=self.harness, headroom_ok=self._headroom(),
                          interval_ok=self._clock() - self._last_spoken >= self.cfg.navigator_interval,
                          disabled=self._disabled or self._closed)
            if reason:
                self.log.review(facts, reason, None)
                return
            with self._lock:
                if self._running:
                    if self._pending is not None:
                        self.log.review(self._pending[0], "skip:replaced", None)
                    self._pending = (facts, session)
                    return
                if self._closed or self._disabled:  # close() or the third strike landed after the gate
                    self.log.review(facts, "skip:disabled", None)
                    return
                self._start(facts, session)
        except Exception:                          # the navigator must never take the window down
            pass

    def take(self, harness: str) -> Note | None:
        with self._lock:
            note = self._note
            if note is None or not (self.cfg.navigator_deliver == "prompt" or harness == self.harness):
                return None
            self._note = None
        self.log.ridden(note.ref, harness)
        return note

    def give_back(self, note: Note) -> None:
        """Return a note taken for a turn that failed before any model saw
        it. A note that landed since is newer and wins."""
        with self._lock:
            if self._note is None:
                self._note = note

    def pending(self) -> Note | None:
        return self._note

    def dismiss(self, feedback: str | None = None) -> bool:
        """Drop the pending note; with `good`/`bad`, also mark the note —
        the pending one, else the last one spoken, which may already have
        ridden a prompt. False only when there was nothing to drop or mark."""
        with self._lock:
            note, self._note = self._note, None
            ref = note.ref if note is not None else self._last_spoken_ref
        if feedback in ("good", "bad") and ref is not None:
            self.log.feedback(ref, feedback)
            return True
        return note is not None

    def mark(self) -> str:
        if self._running:
            return "reviewing"
        return "note" if self._note is not None else ""

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._pending = None
        try:
            self.reviewer.close()
        except Exception:
            pass
        # bounded: the killed reviewer ends the worker at once; a wedged one
        # must not hold the window's quit for longer than this
        t = self._thread
        if t is not None and t.is_alive() and t is not threading.current_thread():
            t.join(5.0)

    def join(self, timeout: float) -> None:
        """Tests: wait for the worker (and whatever it started) to finish."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            t = self._thread
            if t is None or not t.is_alive():
                with self._lock:
                    if not self._running:
                        return
            time.sleep(0.01)

    # -- the worker ----------------------------------------------------------

    def _start(self, facts: TurnFacts, session) -> None:
        """Call with _lock held."""
        self._running = True
        self._thread = threading.Thread(target=self._run, args=(facts, session),
                                        name="tandem-chat-navigator", daemon=True)
        self._thread.start()

    def _run(self, facts: TurnFacts, session) -> None:
        started = self._clock()
        model = self.cfg.navigator_model
        verdict: Verdict | None = None
        timer: threading.Timer | None = None
        try:
            self.post(ReviewStarted(self.harness))
            try:
                diff = self._diff(session.cwd, facts.paths, facts.commands)
                timer = threading.Timer(REVIEW_TIMEOUT, self.reviewer.cancel)
                timer.daemon = True
                timer.start()
                result = self.reviewer.review(session, model, build_prompt(facts, diff), SCHEMA,
                                              self.shadow_lock)
                verdict = parse_verdict(result.structured, result.text, navigator=self.harness,
                                        model=model, elapsed=self._clock() - started)
            except Exception as exc:
                verdict = Verdict("error", error=f"{type(exc).__name__}: {exc}"[:200],
                                  navigator=self.harness, model=model, elapsed=self._clock() - started)
            verdict = self._settle(verdict)
            ref = self.log.review(facts, "", verdict)
            if verdict.spoken:
                with self._lock:
                    self._note = Note(ref, self.harness, facts.harness, verdict)
                    self._last_spoken_ref = ref
        except Exception as exc:                   # a traceback here would paint over the screen
            if verdict is None:
                verdict = Verdict("error", error=f"{type(exc).__name__}: {exc}"[:200],
                                  navigator=self.harness, model=model, elapsed=self._clock() - started)
        finally:
            if timer is not None:
                timer.cancel()
            # whatever happened above, the window hears the review end and
            # the pending slot moves on — otherwise the worker wedges
            try:
                self.post(ReviewFinished(self.harness, verdict or Verdict(
                    "error", error="review ended without a verdict", navigator=self.harness,
                    model=model)))
            except Exception:
                pass
            with self._lock:
                nxt, self._pending = self._pending, None
                self._running = False
                if nxt is not None and not self._closed and not self._disabled:
                    try:
                        self._start(*nxt)
                    except Exception:
                        self._running = False

    def _settle(self, verdict: Verdict) -> Verdict:
        """Failure counting, the three-strike switch, dedupe and the
        spoken-interval clock — everything that turns a parsed reply into
        the verdict the window paints and the log keeps."""
        if verdict.verdict == "error":
            self._failures += 1
            if self._failures >= _MAX_FAILURES:
                self._disabled = True
                return Verdict("off", error=verdict.error, navigator=verdict.navigator,
                               model=verdict.model, elapsed=verdict.elapsed)
            return verdict
        self._failures = 0
        if verdict.spoken:
            keys = {(e.file, e.line) for e in verdict.evidence}
            if keys and keys <= self._spoken_evidence:
                return Verdict("dup", navigator=verdict.navigator, model=verdict.model,
                               elapsed=verdict.elapsed)
            self._spoken_evidence |= keys
            self._last_spoken = self._clock()
        return verdict
