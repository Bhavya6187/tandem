"""The navigator: a second harness that reviews each substantive chat turn
on a private fork of its own shadow and says nothing unless it would block
a PR. This module holds the parts that need no process: the facts one turn
leaves behind, the gate that decides whether they deserve a review, the
prompt and schema, the verdict parser, the log, and the worker that strings
them together around a Reviewer (chat/reviewers.py)."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Callable

from .events import LiveEvent, TextDelta, ToolFinished, ToolStarted

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
