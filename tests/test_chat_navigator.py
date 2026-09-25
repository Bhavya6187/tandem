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
    # codex's strict output mode: every property required, no extras, at every object level
    assert SCHEMA["required"] == ["verdict", "severity", "note", "evidence"]
    assert SCHEMA["additionalProperties"] is False
    props = SCHEMA["properties"]
    assert props["verdict"]["enum"] == ["clean", "speak"]
    assert props["severity"]["enum"] == ["block", "warn", ""]
    assert props["note"]["maxLength"] == NOTE_CHARS == 400
    item = props["evidence"]["items"]
    assert item["required"] == ["file", "line", "why"]
    assert item["additionalProperties"] is False


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


def test_strict_shape_replies_parse():
    clean = pv({"verdict": "clean", "severity": "", "note": "", "evidence": []})
    assert clean.verdict == "clean"
    v = pv({"verdict": "speak", "severity": "", "note": "n",
            "evidence": [{"file": "s.py", "line": 3, "why": ""}]})
    assert v.spoken and v.severity == ""
    assert v.evidence == (Evidence("s.py", 3, ""),)


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


def test_out_of_range_or_deeply_nested_replies_are_errors_not_exceptions():
    huge = '{"verdict": "speak", "note": "n", "evidence": [{"file": "a", "line": 1e999}]}'
    assert pv(text=huge).verdict == "error"
    nested = '{"a":' * 50000 + '1' + '}' * 50000
    assert pv(text=nested).verdict == "error"


def test_diff_of_a_non_utf8_file_is_decoded_not_raised(repo):
    (repo / "l.txt").write_bytes(b"caf\xe9\n")
    subprocess.run(["git", "-C", str(repo), "add", "l.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-q", "-m", "l"], check=True)
    (repo / "l.txt").write_bytes(b"caf\xe9s\n")
    assert "caf" in compute_diff(str(repo), ("l.txt",), 0)


def test_a_touched_path_outside_the_repo_is_omitted(tmp_path):
    root = tmp_path / "r"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / "a.py").write_text("x = 1\n")
    subprocess.run(["git", "-C", str(root), "add", "a.py"], check=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-q", "-m", "a"], check=True)
    (root / "a.py").write_text("x = 2\n")
    outside = tmp_path / "elsewhere" / "scratch.txt"
    outside.parent.mkdir()
    outside.write_text("scratch\n")
    d = compute_diff(str(root), (str(root / "a.py"), str(outside)), 0)
    assert "-x = 1" in d and "+x = 2" in d
    assert "(untracked)" not in d


from tandem.chat.navigator import NavigatorLog, log_path


def test_log_path_is_per_session_under_tandem_home(tmp_path, monkeypatch):
    monkeypatch.setenv("TANDEM_HOME", str(tmp_path / ".tandem"))
    assert log_path("tdm-1") == tmp_path / ".tandem" / "navigator" / "tdm-1.jsonl"


def test_log_records_reviews_rides_and_feedback(tmp_path):
    log = NavigatorLog(tmp_path / "n" / "x.jsonl")
    spoken = Verdict("speak", severity="block", note="bad", evidence=(Evidence("a.py", 3, "w"),),
                     elapsed=2.0, navigator="codex", model="m")
    r1 = log.review(facts_with(prompt="p" * 200, paths=("a.py",)), "", spoken)
    r2 = log.review(facts_with(), "skip:quiet", None)
    r3 = log.review(facts_with(paths=("a",)), "", Verdict("clean", navigator="codex"))
    log.ridden(r1, "claude")
    log.feedback(r1, "good")
    recs = NavigatorLog.read(tmp_path / "n" / "x.jsonl")
    assert [r["kind"] for r in recs] == ["review", "review", "review", "ridden", "feedback"]
    assert recs[0]["ts"] == r1 and recs[0]["gate"] == "review" and recs[0]["verdict"] == "speak"
    assert recs[0]["evidence"] == [{"file": "a.py", "line": 3, "why": "w"}]
    assert len(recs[0]["prompt"]) == 120
    assert recs[1]["gate"] == "skip:quiet" and recs[1]["verdict"] == ""
    assert recs[3] == {"ts": recs[3]["ts"], "kind": "ridden", "ref": r1, "to": "claude"}
    assert recs[4]["ref"] == r1 and recs[4]["value"] == "good"
    assert r1 != r2 != r3


def test_log_stats():
    recs = [
        {"kind": "review", "ts": "1", "gate": "review", "verdict": "speak"},
        {"kind": "review", "ts": "2", "gate": "review", "verdict": "clean"},
        {"kind": "review", "ts": "3", "gate": "skip:quiet", "verdict": ""},
        {"kind": "review", "ts": "4", "gate": "review", "verdict": "speak"},
        {"kind": "review", "ts": "5", "gate": "review", "verdict": "speak"},
        {"kind": "feedback", "ref": "1", "value": "good"},
        {"kind": "feedback", "ref": "4", "value": "bad"},
    ]
    assert NavigatorLog.stats(recs) == {"reviewed": 4, "spoken": 3, "skipped": 1,
                                         "good": 1, "bad": 1, "helpful": 0.5}
    assert NavigatorLog.stats([])["helpful"] is None


def test_log_read_skips_a_torn_line(tmp_path):
    p = tmp_path / "x.jsonl"
    p.write_text('{"kind": "review", "ts": "1"}\n{"kind": "rev')
    assert NavigatorLog.read(p) == [{"kind": "review", "ts": "1"}]
    assert NavigatorLog.read(tmp_path / "missing.jsonl") == []


def test_log_read_skips_a_line_torn_mid_utf8_character(tmp_path):
    p = tmp_path / "x.jsonl"
    p.write_bytes('{"kind":"review","ts":"1"}\n{"kind":"review","prompt":"é'.encode("utf-8")[:-1])
    assert NavigatorLog.read(p) == [{"kind": "review", "ts": "1"}]


from types import SimpleNamespace
from tandem.chat.events import ReviewFinished, ReviewStarted
from tandem.chat.navigator import Navigator, Note, ReviewError, ReviewResult
from tandem.config import ChatConfig

SESSION = SimpleNamespace(cwd="/tmp/nowhere", tandem_id="tdm-nav", participants=["claude", "codex"])


class FakeReviewer:
    """Scripted results, released one at a time so tests can observe the
    in-flight state. `results` items are ReviewResult, or an Exception."""
    harness = "codex"

    def __init__(self, results):
        self.results = list(results)
        self.calls = []
        self.gate = threading.Event()
        self.gate.set()
        self.closed = 0

    def review(self, session, model, prompt, schema, shadow_lock):
        self.calls.append((model, prompt))
        self.gate.wait(5)
        r = self.results.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    def close(self):
        self.closed += 1


def speak(note="bad loop", file="s.py", line=12):
    return ReviewResult({"verdict": "speak", "severity": "block", "note": note,
                         "evidence": [{"file": file, "line": line, "why": "w"}]}, "")


CLEAN = ReviewResult({"verdict": "clean"}, "")


def make_nav(results, tmp_path, cfg=None, **kw):
    posted = []
    reviewer = FakeReviewer(results)
    log = NavigatorLog(tmp_path / "log.jsonl")
    opts = dict(headroom=lambda: True, clock=Clock(), diff=lambda cwd, paths, commands, **k: "DIFF")
    opts.update(kw)
    nav = Navigator("codex", cfg or ChatConfig(navigator="codex"), reviewer, posted.append, log, **opts)
    return nav, reviewer, posted, log


def finished(posted):
    return [e for e in posted if isinstance(e, ReviewFinished)]


def test_a_gated_turn_runs_one_review_and_posts_both_events(tmp_path):
    nav, reviewer, posted, log = make_nav([CLEAN], tmp_path)
    nav.turn_ended(facts_with(paths=("a.py",)), SESSION)
    nav.join(5)
    assert [type(e).__name__ for e in posted] == ["ReviewStarted", "ReviewFinished"]
    assert posted[0].harness == "codex" and posted[1].verdict.verdict == "clean"
    assert reviewer.calls[0][1].startswith("[tandem navigator]") and "DIFF" in reviewer.calls[0][1]
    recs = NavigatorLog.read(log.path)
    assert recs[-1]["gate"] == "review" and recs[-1]["verdict"] == "clean"
    assert nav.mark() == "" and nav.pending() is None


def test_a_skipped_turn_is_logged_and_runs_nothing(tmp_path):
    nav, reviewer, posted, log = make_nav([CLEAN], tmp_path)
    nav.turn_ended(facts_with(), SESSION)
    nav.join(1)
    assert posted == [] and reviewer.calls == []
    assert NavigatorLog.read(log.path)[-1]["gate"] == "skip:quiet"


def test_a_spoken_note_is_pending_marks_the_bar_and_rides_only_the_navigator_in_bar_mode(tmp_path):
    nav, reviewer, posted, log = make_nav([speak()], tmp_path)
    nav.turn_ended(facts_with(paths=("a.py",)), SESSION)
    nav.join(5)
    note = nav.pending()
    assert isinstance(note, Note) and note.verdict.note == "bad loop" and nav.mark() == "note"
    assert note.summary == "bad loop"
    assert nav.take("claude") is None and nav.pending() is note        # bar mode: not for claude
    got = nav.take("codex")
    assert got is note and nav.pending() is None and nav.mark() == ""
    assert "[tandem navigator] codex reviewed the previous claude turn and flagged (block): bad loop" in got.trailer()
    assert got.trailer().startswith("\n\n") and "s.py:12 — w" in got.trailer()
    recs = NavigatorLog.read(log.path)
    assert recs[-1]["kind"] == "ridden" and recs[-1]["to"] == "codex" and recs[-1]["ref"] == note.ref


def test_prompt_mode_rides_any_harness(tmp_path):
    nav, *_ = make_nav([speak()], tmp_path, cfg=ChatConfig(navigator="codex", navigator_deliver="prompt"))
    nav.turn_ended(facts_with(paths=("a.py",)), SESSION)
    nav.join(5)
    assert nav.take("claude") is not None


def test_dismiss_with_feedback_logs_it(tmp_path):
    nav, reviewer, posted, log = make_nav([speak()], tmp_path)
    nav.turn_ended(facts_with(paths=("a.py",)), SESSION)
    nav.join(5)
    ref = nav.pending().ref
    assert nav.dismiss("bad") is True and nav.pending() is None
    assert nav.dismiss() is False
    rec = NavigatorLog.read(log.path)[-1]
    assert rec == {"ts": rec["ts"], "kind": "feedback", "ref": ref, "value": "bad"}


def test_feedback_after_the_note_rode_a_prompt_is_logged_against_it(tmp_path):
    nav, reviewer, posted, log = make_nav([speak()], tmp_path)
    nav.turn_ended(facts_with(paths=("a.py",)), SESSION)
    nav.join(5)
    ridden = nav.take("codex")
    assert ridden is not None and nav.pending() is None
    assert nav.dismiss("good") is True
    rec = NavigatorLog.read(log.path)[-1]
    assert rec == {"ts": rec["ts"], "kind": "feedback", "ref": ridden.ref, "value": "good"}
    assert nav.dismiss() is False                     # a bare dismiss still needs a pending note


def test_feedback_with_nothing_ever_spoken_is_refused(tmp_path):
    nav, reviewer, posted, log = make_nav([], tmp_path)
    assert nav.dismiss("bad") is False
    assert NavigatorLog.read(log.path) == []


def test_a_newer_turn_replaces_the_pending_one_while_a_review_runs(tmp_path):
    nav, reviewer, posted, log = make_nav([CLEAN, CLEAN], tmp_path)
    reviewer.gate.clear()
    nav.turn_ended(facts_with(prompt="first", paths=("a.py",)), SESSION)
    nav.turn_ended(facts_with(prompt="second", paths=("b.py",)), SESSION)
    nav.turn_ended(facts_with(prompt="third", paths=("c.py",)), SESSION)
    assert nav.mark() == "reviewing"
    reviewer.gate.set()
    nav.join(5)
    assert len(finished(posted)) == 2
    assert [f"{'a' if 'a.py' in p else 'c'}" for _, p in reviewer.calls] == ["a", "c"]
    # the replaced turn is logged the moment it is replaced, before the
    # first review finishes, so compare by prompt rather than by order
    assert {r["prompt"]: r["gate"] for r in NavigatorLog.read(log.path) if r["kind"] == "review"} == {
        "first": "review", "second": "skip:replaced", "third": "review"}


def test_interval_and_dedupe(tmp_path):
    clock = Clock()
    # the second turn is skipped at the gate, so it pops no result
    nav, reviewer, posted, log = make_nav([speak(), speak(file="t.py"), speak(file="t.py")],
                                          tmp_path, cfg=ChatConfig(navigator="codex", navigator_interval=100),
                                          clock=clock)
    nav.turn_ended(facts_with(paths=("a.py",)), SESSION); nav.join(5)
    nav.turn_ended(facts_with(paths=("a.py",)), SESSION); nav.join(5)       # inside the interval
    assert NavigatorLog.read(log.path)[-1]["gate"] == "skip:interval"
    clock.now += 101
    nav.dismiss()
    nav.turn_ended(facts_with(paths=("a.py",)), SESSION); nav.join(5)       # new evidence: spoken
    assert finished(posted)[-1].verdict.spoken
    clock.now += 101
    nav.dismiss()
    nav.turn_ended(facts_with(paths=("a.py",)), SESSION); nav.join(5)       # same evidence: dup
    assert finished(posted)[-1].verdict.verdict == "dup" and nav.pending() is None


def test_three_failures_disable_the_navigator_for_the_window(tmp_path):
    boom = [ReviewError("no fork"), ReviewResult(None, "not json"), RuntimeError("bug")]
    nav, reviewer, posted, log = make_nav(boom + [CLEAN], tmp_path)
    for _ in range(4):
        nav.turn_ended(facts_with(paths=("a.py",)), SESSION); nav.join(5)
    verdicts = [e.verdict.verdict for e in finished(posted)]
    assert verdicts == ["error", "error", "off"]
    assert "bug" in finished(posted)[-1].verdict.error
    assert NavigatorLog.read(log.path)[-1]["gate"] == "skip:disabled"
    assert reviewer.results == [CLEAN]                                     # never ran


def test_a_success_resets_the_failure_count(tmp_path):
    nav, reviewer, posted, log = make_nav([ReviewError("x"), ReviewError("y"), CLEAN, ReviewError("z"), CLEAN],
                                          tmp_path)
    for _ in range(5):
        nav.turn_ended(facts_with(paths=("a.py",)), SESSION); nav.join(5)
    assert [e.verdict.verdict for e in finished(posted)] == ["error", "error", "clean", "error", "clean"]


def test_headroom_is_asked_per_turn(tmp_path):
    ok = [False]
    nav, reviewer, posted, log = make_nav([CLEAN], tmp_path, headroom=lambda: ok[0])
    nav.turn_ended(facts_with(paths=("a.py",)), SESSION); nav.join(1)
    assert NavigatorLog.read(log.path)[-1]["gate"] == "skip:headroom"
    ok[0] = True
    nav.turn_ended(facts_with(paths=("a.py",)), SESSION); nav.join(5)
    assert finished(posted)


def test_close_reaches_the_reviewer_and_stops_new_work(tmp_path):
    nav, reviewer, posted, log = make_nav([CLEAN], tmp_path)
    nav.close()
    assert reviewer.closed == 1
    nav.turn_ended(facts_with(paths=("a.py",)), SESSION); nav.join(1)
    assert reviewer.calls == []


def test_close_waits_for_the_review_in_flight(tmp_path):
    nav, reviewer, posted, log = make_nav([CLEAN], tmp_path)
    reviewer.gate.clear()
    nav.turn_ended(facts_with(paths=("a.py",)), SESSION)
    assert nav.mark() == "reviewing"
    threading.Timer(0.1, reviewer.gate.set).start()
    nav.close()                                     # returns once the worker is gone
    assert reviewer.closed == 1 and nav._running is False
    assert not nav._thread.is_alive() and len(finished(posted)) == 1


def test_a_reply_the_log_cannot_encode_still_finishes_the_review(tmp_path):
    bad = ReviewResult({"verdict": "speak", "severity": "block", "note": "\ud800 bad",
                        "evidence": [{"file": "s.py", "line": 1, "why": "w"}]}, "")
    nav, reviewer, posted, log = make_nav([bad, CLEAN], tmp_path,
                                          cfg=ChatConfig(navigator="codex", navigator_interval=0))
    nav.turn_ended(facts_with(paths=("a.py",)), SESSION); nav.join(5)
    assert len(finished(posted)) == 1 and nav.mark() in ("", "note")
    nav.dismiss()
    assert nav.mark() == ""
    nav.turn_ended(facts_with(paths=("b.py",)), SESSION); nav.join(5)
    assert len(finished(posted)) == 2 and reviewer.results == []


def test_a_post_that_raises_on_finish_does_not_wedge_the_worker(tmp_path):
    posted = []
    raised = []

    def post(ev):
        if isinstance(ev, ReviewFinished) and not raised:
            raised.append(ev)
            raise RuntimeError("queue gone")
        posted.append(ev)

    nav, reviewer, _, log = make_nav([CLEAN, CLEAN], tmp_path)
    nav.post = post
    nav.turn_ended(facts_with(paths=("a.py",)), SESSION); nav.join(5)
    assert raised and nav.mark() == ""
    nav.turn_ended(facts_with(paths=("b.py",)), SESSION); nav.join(5)
    assert len(finished(posted)) == 1 and reviewer.results == []


def test_a_close_during_the_gate_starts_no_review(tmp_path, monkeypatch):
    # gate() reads `disabled` after headroom/clock, so a close inside the
    # headroom callable would be seen by the gate itself; land it after the
    # gate returns and before turn_ended takes the lock instead
    import tandem.chat.navigator as navmod
    real_gate = navmod.gate
    holder = []

    def gate_then_close(*a, **kw):
        reason = real_gate(*a, **kw)
        holder[0].close()
        return reason

    monkeypatch.setattr(navmod, "gate", gate_then_close)
    nav, reviewer, posted, log = make_nav([CLEAN], tmp_path)
    holder.append(nav)
    nav.turn_ended(facts_with(paths=("a.py",)), SESSION); nav.join(1)
    assert reviewer.calls == [] and posted == []
    assert NavigatorLog.read(log.path)[-1]["gate"] == "skip:disabled"


def test_a_note_given_back_is_pending_again_but_never_displaces_a_newer_one(tmp_path):
    clock = Clock()
    nav, *_ = make_nav([speak(), speak("newer", file="t.py")], tmp_path, clock=clock)
    nav.turn_ended(facts_with(paths=("a.py",)), SESSION); nav.join(5)
    old = nav.take("codex")
    assert old is not None and nav.pending() is None
    nav.give_back(old)
    assert nav.pending() is old and nav.mark() == "note"
    assert nav.take("codex") is old
    clock.now += 1000
    nav.turn_ended(facts_with(paths=("a.py",)), SESSION); nav.join(5)
    newer = nav.pending()
    assert newer is not None and newer.summary == "newer"
    nav.give_back(old)
    assert nav.pending() is newer


from tandem.chat.navigator import headroom_ok


def test_headroom_reads_the_shortest_window():
    state = {"windows": {"codex": [("5h", 85), ("7d", 10)]}}
    assert headroom_ok(state, "codex", 20) is False           # 15 % left < 20
    assert headroom_ok(state, "codex", 15) is True
    assert headroom_ok(state, "codex", 0) is True
    assert headroom_ok({"windows": {"codex": [("5h", 79)]}}, "codex", 20) is True


def test_headroom_without_data_is_not_enforced():
    assert headroom_ok({}, "codex", 20) is True
    assert headroom_ok({"windows": {}}, "codex", 20) is True
    assert headroom_ok({"windows": {"codex": []}}, "codex", 20) is True
    assert headroom_ok({"limits": {"codex": "5h 99%"}}, "codex", 20) is True    # text alone is not data
