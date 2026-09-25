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


import subprocess
from tandem.chat.navigator import NOTE_CHARS, SCHEMA, build_prompt, compute_diff, parse_verdict


def test_prompt_names_the_turn_and_carries_the_diff():
    p = build_prompt(facts_with(harness="claude", paths=("a.py", "b.py")), "--- a.py\n+++ a.py\n")
    assert p.startswith("[tandem navigator]")
    assert "ran on claude" in p and "a.py, b.py" in p and "+++ a.py" in p
    assert "block a pull request" in p and "\"clean\"" in p


def test_prompt_without_files_or_diff_says_so():
    p = build_prompt(facts_with(paths=()), "")
    assert "no files" in p and "(no diff)" in p


def test_schema_is_what_the_spec_says():
    assert SCHEMA["required"] == ["verdict"]
    assert SCHEMA["properties"]["verdict"]["enum"] == ["clean", "speak"]
    assert SCHEMA["properties"]["note"]["maxLength"] == NOTE_CHARS == 400


def pv(structured=None, text="", **kw):
    opts = dict(navigator="codex", model="", elapsed=1.5)
    opts.update(kw)
    return parse_verdict(structured, text, **opts)


def test_structured_output_wins_over_text():
    v = pv({"verdict": "speak", "severity": "block", "note": "bad loop",
            "evidence": [{"file": "s.py", "line": 12, "why": "swallows"}]}, text="garbage")
    assert v.spoken and v.severity == "block" and v.note == "bad loop"
    assert v.evidence == (Evidence("s.py", 12, "swallows"),)
    assert v.navigator == "codex" and v.elapsed == 1.5


def test_text_json_is_parsed_even_inside_fences_or_prose():
    v = pv(text="Sure.\n```json\n{\"verdict\": \"speak\", \"note\": \"n\", \"severity\": \"warn\"}\n```\n")
    assert v.spoken and v.severity == "warn"
    assert pv(text='{"verdict": "clean"}').verdict == "clean"


def test_speak_without_a_note_is_empty_and_long_notes_are_clipped():
    assert pv({"verdict": "speak", "note": "  "}).verdict == "empty"
    v = pv({"verdict": "speak", "note": "x" * 900})
    assert len(v.note) == NOTE_CHARS


def test_bad_verdicts_are_errors_not_exceptions():
    for structured, text in [(None, ""), (None, "not json"), ({"verdict": "maybe"}, ""),
                             ({"verdict": "speak", "note": "n", "evidence": "nope"}, ""),
                             ({"verdict": "speak", "note": "n", "evidence": [{"file": "a"}]}, "")]:
        v = pv(structured, text)
        assert v.verdict == "error" and v.error, (structured, text)


def test_evidence_with_a_string_line_is_coerced_or_dropped():
    v = pv({"verdict": "speak", "note": "n", "evidence": [{"file": "a.py", "line": "7"}]})
    assert v.evidence == (Evidence("a.py", 7),)


@pytest.fixture
def repo(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-q", "--allow-empty", "-m", "root"], check=True)
    (tmp_path / "a.py").write_text("x = 1\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "a.py"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-q", "-m", "a"], check=True)
    return tmp_path


def test_diff_of_touched_files_plus_untracked_contents(repo):
    (repo / "a.py").write_text("x = 2\n")
    (repo / "new.py").write_text("print('hi')\n")
    d = compute_diff(str(repo), ("a.py", "new.py"), 0)
    assert "-x = 1" in d and "+x = 2" in d
    assert "new.py (untracked)" in d and "print('hi')" in d


def test_diff_falls_back_to_the_whole_tree_after_a_command(repo):
    (repo / "a.py").write_text("x = 3\n")
    assert "+x = 3" in compute_diff(str(repo), (), 1)
    assert compute_diff(str(repo), (), 0) == ""          # nothing touched, nothing ran


def test_diff_is_capped_with_a_marker(repo):
    (repo / "a.py").write_text("y\n" * 5000)
    d = compute_diff(str(repo), ("a.py",), 0, cap=500)
    assert len(d) <= 500 + 40 and d.endswith("… (truncated)")


def test_diff_outside_a_repo_is_empty_not_an_error(tmp_path):
    (tmp_path / "a.py").write_text("x\n")
    assert compute_diff(str(tmp_path), ("a.py",), 1) == ""
