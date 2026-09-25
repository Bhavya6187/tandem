"""The navigator: facts from a turn's own events, the gate, the worker, and
the log. Reviewers are faked; see test_chat_reviewers.py for the real ones."""

import json
import threading
import time

import pytest

from tandem.chat.events import (Evidence, TextDelta, ToolFinished, ToolStarted, TurnFinished,
                                Verdict)
from tandem.chat.navigator import CLAIM_RE, FactsCollector, TurnFacts, gate


class Clock:
    def __init__(self): self.now = 100.0
    def __call__(self): return self.now


def collect(events, *, harness="claude", prompt="do it", carried=False, first=False, status="completed"):
    sink = []
    clock = Clock()
    c = FactsCollector(harness, prompt, carried, first, sink.append, clock)
    for ev in events:
        c.emit(ev)
    clock.now += 4.5
    facts = c.finish(status)
    assert len(sink) == len(events)            # every event is forwarded untouched
    return facts


def test_facts_union_paths_and_count_commands_and_failures():
    facts = collect([
        ToolStarted("c1", "Edit", "a.py", paths=("a.py",)),
        ToolFinished("c1", True),
        ToolStarted("c2", "Bash", "pytest"),
        ToolFinished("c2", False, "exit 1"),
        ToolStarted("c3", "Read", "b.py"),
        ToolStarted("c4", "Write", "a.py", paths=("a.py",)),
        TextDelta("all "), TextDelta("done"),
        TurnFinished("completed", ""),
    ])
    assert facts.paths == ("a.py",)
    assert facts.commands == 1 and facts.failed_tools == 1
    assert facts.final_text == "all done"
    assert facts.status == "completed" and facts.ended - facts.started == 4.5


def test_final_text_keeps_only_the_last_2000_chars():
    facts = collect([TextDelta("x" * 1500), TextDelta("y" * 1500)])
    assert len(facts.final_text) == 2000 and facts.final_text.endswith("y" * 1500)


def test_command_tool_names_across_harnesses():
    for tool in ("Bash", "exec", "bash"):
        assert collect([ToolStarted("c", tool, "ls")]).commands == 1
    assert collect([ToolStarted("c", "Grep", "x")]).commands == 0


@pytest.mark.parametrize("text, hit", [
    ("All tests are passing now.", True), ("Fixed the bug.", True), ("I implemented it", True),
    ("Here is how it works", False), ("The function is undone", False),
])
def test_claim_pattern(text, hit):
    assert bool(CLAIM_RE.search(text)) is hit


def facts_with(**kw) -> TurnFacts:
    base = dict(harness="claude", prompt="fix it", carried_note=False, first_turn=False,
                status="completed", paths=(), commands=0, failed_tools=0, final_text="",
                started=0.0, ended=1.0)
    base.update(kw)
    return TurnFacts(**base)


def g(facts, **kw):
    opts = dict(navigator="codex", headroom_ok=True, interval_ok=True, disabled=False)
    opts.update(kw)
    return gate(facts, **opts)


def test_gate_reviews_edits_failures_and_claims():
    assert g(facts_with(paths=("a.py",))) == ""
    assert g(facts_with(failed_tools=1)) == ""
    assert g(facts_with(final_text="done, all tests pass")) == ""
    assert g(facts_with()) == "skip:quiet"


def test_gate_skip_reasons_in_order():
    assert g(facts_with(paths=("a",)), disabled=True) == "skip:disabled"
    assert g(facts_with(harness="codex", paths=("a",))) == "skip:own-turn"
    assert g(facts_with(prompt="[tandem] the turn ended", paths=("a",))) == "skip:tandem-prompt"
    assert g(facts_with(first_turn=True, paths=("a",))) == "skip:first-turn"
    assert g(facts_with(status="interrupted", paths=("a",))) == "skip:interrupted"
    assert g(facts_with(status="failed", paths=("a",))) == "skip:failed"
    assert g(facts_with(paths=("a",)), headroom_ok=False) == "skip:headroom"
    assert g(facts_with(paths=("a",)), interval_ok=False) == "skip:interval"


def test_a_claim_does_not_count_when_a_note_rode_the_prompt():
    assert g(facts_with(final_text="fixed", carried_note=True)) == "skip:quiet"
    assert g(facts_with(paths=("a",), carried_note=True)) == ""     # a real change still does
