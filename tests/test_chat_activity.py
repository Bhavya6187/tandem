"""The activity line's state: what the running turn is doing right now, for
how long, and whether it is the user it is waiting on."""

import pytest

from tandem.chat.activity import Activity, elapsed_text
from tandem.chat.events import (ApprovalRequest, Idle, QuestionRequest, TextDelta, ThinkingDelta,
                                ToolFinished, ToolStarted, TurnFinished, TurnStarted)


class Clock:
    def __init__(self): self.now = 100.0
    def __call__(self): return self.now


@pytest.fixture
def act():
    clock = Clock()
    return Activity(clock), clock


def test_idle_until_a_turn_starts(act):
    a, _ = act
    assert a.active is False and a.text() == ""


def test_a_started_turn_names_the_harness_and_counts_up(act):
    a, clock = act
    a.on_event(TurnStarted("claude", "", "go"))
    assert a.active is True
    assert a.text() == "⠋ claude · starting · 0s · esc interrupts"
    clock.now += 12.0
    assert a.text() == "⠋ claude · starting · 12s · esc interrupts"


@pytest.mark.parametrize("event, phase", [
    (ThinkingDelta("hm"), "thinking"),
    (TextDelta("hi"), "writing"),
    (ToolStarted("c1", "Bash", "pytest -q"), "running Bash"),
])
def test_the_phase_follows_the_stream(act, event, phase):
    a, _ = act
    a.on_event(TurnStarted("codex", "", "go"))
    a.on_event(event)
    assert a.text() == f"⠋ codex · {phase} · 0s · esc interrupts"


def test_a_finished_tool_is_no_longer_running(act):
    """Between a tool's result and the model's next token the turn is still
    working, but not inside that tool."""
    a, _ = act
    a.on_event(TurnStarted("codex", "", "go"))
    a.on_event(ToolStarted("c1", "Bash", "ls"))
    a.on_event(ToolFinished("c1", True, "exit 0"))
    assert a.text() == "⠋ codex · working · 0s · esc interrupts"


def test_the_spinner_advances_with_the_clock(act):
    a, clock = act
    a.on_event(TurnStarted("claude", "", "go"))
    first = a.text()[0]
    clock.now += 0.15            # a frame is 0.1 s; 100.1 - 100.0 is a hair under one
    assert a.text()[0] != first


@pytest.mark.parametrize("request_event", [
    ApprovalRequest("command", "rm -rf build", ("allow", "deny")),
    QuestionRequest("which one?", ("a", "b")),
])
def test_a_request_says_the_turn_waits_on_the_user(act, request_event):
    """No spinner and no timer: nothing is running, and the clock that
    matters is the user's."""
    a, clock = act
    a.on_event(TurnStarted("claude", "", "go"))
    a.on_event(request_event)
    clock.now += 30
    assert a.waiting is True
    assert a.text() == "● claude is waiting for your answer"


def test_an_answer_puts_the_turn_back_to_work(act):
    a, _ = act
    a.on_event(TurnStarted("claude", "", "go"))
    a.on_event(ApprovalRequest("command", "ls", ("allow", "deny")))
    a.answered()
    assert a.waiting is False
    assert a.text() == "⠋ claude · working · 0s · esc interrupts"


def test_a_finished_turn_reports_its_length_and_goes_idle(act):
    a, clock = act
    a.on_event(TurnStarted("claude", "", "go"))
    clock.now += 42.4
    a.on_event(TurnFinished("completed", ""))
    assert a.active is False and a.text() == ""
    assert a.last_elapsed == pytest.approx(42.4)


def test_idle_clears_a_turn_that_never_reported_its_end(act):
    a, _ = act
    a.on_event(TurnStarted("claude", "", "go"))
    a.on_event(Idle())
    assert a.active is False


def test_queued_prompts_are_counted(act):
    a, _ = act
    a.on_event(TurnStarted("claude", "", "go"))
    assert a.text(queued=2) == "⠋ claude · starting · 0s · +2 queued · esc interrupts"


def test_a_narrow_row_drops_the_hint_then_the_queue(act):
    """The harness, the phase and the timer are the line's reason to exist;
    the key hint is documented elsewhere and the queue count was already
    said once when the prompt was queued."""
    a, _ = act
    a.on_event(TurnStarted("claude", "", "go"))
    assert a.text(queued=1, width=40) == "⠋ claude · starting · 0s · +1 queued"
    assert a.text(queued=1, width=30) == "⠋ claude · starting · 0s"


@pytest.mark.parametrize("seconds, want", [
    (0, "0s"), (59.9, "59s"), (60, "1m 00s"), (125, "2m 05s"), (3725, "1h 02m"),
])
def test_elapsed_text(seconds, want):
    assert elapsed_text(seconds) == want


from tandem.chat.events import ReviewFinished, ReviewStarted, Verdict


def test_a_review_shows_on_the_line_only_while_no_turn_runs(act):
    a, clock = act
    a.on_event(ReviewStarted("codex"))
    assert a.active is False and a.reviewing == "codex" and a.animating is True
    assert a.text() == "⠋ codex reviewing · 0s"
    clock.now += 12
    assert a.text() == "⠋ codex reviewing · 12s"
    a.on_event(TurnStarted("claude", "", "go"))
    assert a.text().startswith("⠋ claude · starting")          # the turn owns the row
    a.on_event(TurnFinished("completed", "")); a.on_event(Idle())
    assert a.text() == "⠋ codex reviewing · 12s"                # back to the review, still counting
    a.on_event(ReviewFinished("codex", Verdict("clean")))
    assert a.reviewing == "" and a.text() == "" and a.animating is False


def test_the_review_clock_starts_at_review_start(act):
    a, clock = act
    a.on_event(TurnStarted("claude", "", "go"))
    clock.now += 30
    a.on_event(TurnFinished("completed", "")); a.on_event(Idle())
    a.on_event(ReviewStarted("codex"))
    clock.now += 3
    assert a.text() == "⠋ codex reviewing · 3s"
