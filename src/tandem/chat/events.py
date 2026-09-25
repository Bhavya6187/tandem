"""What a running turn tells the window, in one vocabulary for all three
harnesses. Deliberately narrower than events.NormalizedEvent: these are
paint instructions and prompts, not transcript content — the transcript
is the harness's own file, which sync reads afterwards.

Runtime clients emit these from their worker thread; the window drains
them on the main thread. ApprovalRequest and QuestionRequest are the two
that block: the client calls Answers and waits."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Union


@dataclass(frozen=True)
class TextDelta:
    text: str


@dataclass(frozen=True)
class ThinkingDelta:
    text: str


@dataclass(frozen=True)
class ToolStarted:
    call_id: str
    tool: str
    summary: str        # one line, the renderer prints it after the tool name
    paths: tuple[str, ...] = ()   # files a file-change tool names; empty for commands and reads


@dataclass(frozen=True)
class ToolOutput:
    call_id: str
    text: str


@dataclass(frozen=True)
class ToolFinished:
    call_id: str
    ok: bool
    summary: str = ""


@dataclass(frozen=True)
class ApprovalRequest:
    kind: str           # "command" | "file_change" | "permission"
    detail: str
    choices: tuple[str, ...] = ("allow", "always", "deny")


APPROVAL_LABELS = (("allow", "[y]es"), ("always", "[a]lways"), ("deny", "[n]o"))


def offered_labels(choices: tuple[str, ...] | None) -> str:
    """The keys an approval advertises, in y/a/n order, filtered by what the
    request actually offers — codex drops `always` when the app-server's
    availableDecisions carry no acceptForSession. An empty or missing set
    means the default three (the dataclass's own default)."""
    return " ".join(label for key, label in APPROVAL_LABELS
                    if not choices or key in choices)


@dataclass(frozen=True)
class QuestionRequest:
    prompt: str
    options: tuple[str, ...] = ()   # empty = free text


@dataclass(frozen=True)
class TurnStarted:
    harness: str
    model: str
    prompt: str
    carried: str = ""   # a navigator note's summary when one rode this prompt; "" otherwise


@dataclass(frozen=True)
class TurnFinished:
    status: str         # "completed" | "interrupted" | "failed"
    usage: str = ""     # dim trailer line, "" for none


@dataclass(frozen=True)
class Failure:
    message: str


@dataclass(frozen=True)
class LimitsUpdate:
    harness: str
    text: str           # bar-ready, e.g. "5h 4% 7d 41%"
    windows: tuple[tuple[str, int], ...] = ()   # (label, used_percent), shortest first


@dataclass(frozen=True)
class Evidence:
    file: str
    line: int
    why: str = ""


@dataclass(frozen=True)
class Verdict:
    """A finished review. `verdict` is one of: clean, speak, empty (spoke
    with no note), dup (repeats evidence already spoken), error, off (the
    navigator disabled itself)."""
    verdict: str
    severity: str = ""              # "block" | "warn" | ""
    note: str = ""
    evidence: tuple[Evidence, ...] = ()
    elapsed: float = 0.0
    error: str = ""
    navigator: str = ""
    model: str = ""

    @property
    def spoken(self) -> bool:
        return self.verdict == "speak"


@dataclass(frozen=True)
class ReviewStarted:
    harness: str        # the navigator


@dataclass(frozen=True)
class ReviewFinished:
    harness: str
    verdict: Verdict


@dataclass(frozen=True)
class Idle:
    """The dispatcher finished its post-turn work; the window may pump."""


LiveEvent = Union[TextDelta, ThinkingDelta, ToolStarted, ToolOutput, ToolFinished,
                  ApprovalRequest, QuestionRequest, TurnStarted, TurnFinished,
                  Failure, LimitsUpdate, ReviewStarted, ReviewFinished, Idle]

STATUSES = ("completed", "interrupted", "failed")


class Answers(Protocol):
    def approve(self, req: ApprovalRequest) -> str: ...   # one of req.choices

    def answer(self, req: QuestionRequest) -> str: ...


@dataclass
class TurnOutcome:
    status: str
    error: str = ""
    native_id: str | None = None   # a thread id minted during this turn (fresh codex)
    structured: object | None = None   # claude's structured_output when a schema was requested
