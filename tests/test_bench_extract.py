"""Post-run extraction from claude's stream-json, and the validity rules the
aggregator trusts.

Fixture provenance — all three come from live claude 2.1.220 / codex-cli
0.145.0 runs on 2026-08-01 (`claude -p "…one Explore subagent…" --model haiku
--output-format stream-json --verbose --include-hook-events`), sanitized only
for paths and for the operator's own SessionStart plugin context:

  stream-native.jsonl   plain scratch dir, no tandem plugin loaded
  stream-reroute.jsonl  paired dir under a scratch TANDEM_HOME, plugin loaded
                        via --plugin-dir; hook rewrote the dispatch
  stream-notice.jsonl   stream-native with a real `tandem hook-route` notice
                        spliced in inside a PreToolUse hook_response envelope
                        copied field-for-field from the rerouted run — the one
                        shape the two live probes could not produce together
"""

import json

import pytest
from conftest import BENCH_FIXTURES, bench_mixed_stream, load_bench_module

runner = load_bench_module("runner")


def events(name):
    return runner.read_stream(BENCH_FIXTURES / f"stream-{name}.jsonl")


@pytest.fixture
def native():
    return runner.extract_transcript(events("native"))


@pytest.fixture
def reroute():
    return runner.extract_transcript(events("reroute"))


@pytest.fixture
def notice():
    return runner.extract_transcript(events("notice"))


@pytest.fixture
def mixed():
    return runner.extract_transcript(bench_mixed_stream())


# --- session facts -----------------------------------------------------------


def test_session_facts_from_init(native):
    assert native["session_id"] == "fe266b0f-3b1a-4db6-a4cf-d42c60830e92"
    assert native["claude_version"] == "2.1.220"
    assert native["model"] == "claude-haiku-4-5-20251001"


def test_result_is_the_last_one_not_the_first(native):
    # an async Agent dispatch produces one `result` per turn; only the last
    # carries the run's final answer
    assert native["result_events"] == 2
    assert native["result_subtype"] == "success"
    assert native["result_is_error"] is False


# --- dispatch counting -------------------------------------------------------


def test_native_dispatch(native):
    assert native["dispatches"] == 1
    assert native["reroutes"] == 0
    assert native["notices"] == 0
    assert native["agent_types"] == {"Explore": 1}
    assert native["requested_agent_types"] == {"Explore": 1}


def test_rerouted_dispatch(reroute):
    assert reroute["dispatches"] == 1
    assert reroute["reroutes"] == 1
    assert reroute["notices"] == 0
    # the model asked for Explore; the hook made it the codex bridge
    assert reroute["requested_agent_types"] == {"Explore": 1}
    assert reroute["agent_types"] == {"tandem:codex-worker": 1}


def test_background_bash_is_not_a_dispatch(reroute):
    # the bridge agent runs `tandem sub` via Bash, which emits its own
    # task_started with task_type="local_bash"
    kinds = {e.get("task_type") for e in events("reroute")
             if e.get("subtype") == "task_started"}
    assert kinds == {"local_agent", "local_bash"}
    assert reroute["dispatches"] == 1


def test_mixed_run_counts_both_kinds(mixed):
    assert mixed["dispatches"] == 2
    assert mixed["reroutes"] == 1
    assert mixed["native_dispatches"] == 1
    assert mixed["notices"] == 0          # the silent case: no notice at all
    assert mixed["agent_types"] == {"tandem:codex-worker": 1, "Explore": 1}


def test_hook_decisions_are_counted(reroute, native):
    assert reroute["hook_reroute_decisions"] == 1
    assert native["hook_reroute_decisions"] == 0


def test_notice_is_seen_in_the_hook_response(notice):
    assert notice["notices"] == 1
    assert notice["reroutes"] == 0
    assert notice["dispatches"] == 1


# --- tokens ------------------------------------------------------------------


def test_token_totals_from_last_result(native):
    t = native["tokens"]
    assert t == {
        "input": 46, "output": 698, "cache_read": 78469,
        "cache_creation": 18439, "total": 46 + 698 + 78469 + 18439,
    }
    assert native["cost_usd"] == pytest.approx(0.04164665)


def test_token_totals_reroute(reroute):
    assert reroute["tokens"]["output"] == 818
    assert reroute["cost_usd"] == pytest.approx(0.0374655)


def test_tokens_fall_back_to_usage_when_model_usage_is_absent():
    ev = [{"type": "result", "subtype": "success", "is_error": False,
           "usage": {"input_tokens": 3, "output_tokens": 5,
                     "cache_read_input_tokens": 7,
                     "cache_creation_input_tokens": 11}}]
    assert runner.extract_transcript(ev)["tokens"] == {
        "input": 3, "output": 5, "cache_read": 7, "cache_creation": 11,
        "total": 26,
    }


def test_killed_run_with_no_result_event_still_reports_tokens(native):
    # a run killed on timeout never emits a `result`; the per-message usage on
    # every assistant event is what is left, and folding a 0 into the token
    # means would quietly flatter whichever arm timed out
    killed = [e for e in events("native") if e.get("type") != "result"]
    ex = runner.extract_transcript(killed)
    assert ex["result_events"] == 0
    assert ex["tokens"]["output"] > 0
    assert ex["tokens"]["total"] > 0
    # the fallback is per-message usage, so it is a different (smaller) number
    # than the session-wide modelUsage the result event carries
    assert ex["tokens"]["total"] != native["tokens"]["total"]


def test_a_missing_result_event_is_warned_about():
    ex = runner.mark_validity("b", runner.extract_transcript(
        [e for e in events("native") if e.get("type") != "result"]))
    assert ex["validity"] == "valid"           # a killed run is still a run
    assert any("result event" in w for w in ex["warnings"])


def test_empty_transcript_extracts_zeros():
    e = runner.extract_transcript([])
    assert e["dispatches"] == 0 and e["reroutes"] == 0 and e["notices"] == 0
    assert e["tokens"]["total"] == 0
    assert e["result_events"] == 0


# --- tolerant reading --------------------------------------------------------


def test_read_stream_skips_junk_lines(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text('{"type":"a"}\nnot json\n\n[1,2]\n{"type":"b"}\n')
    assert [e["type"] for e in runner.read_stream(p)] == ["a", "b"]


def test_read_stream_missing_file_is_empty(tmp_path):
    assert runner.read_stream(tmp_path / "absent.jsonl") == []


# --- validity ----------------------------------------------------------------


def make(reroutes=0, notices=0, dispatches=1):
    return {"dispatches": dispatches, "reroutes": reroutes, "notices": notices,
            "native_dispatches": dispatches - reroutes}


@pytest.mark.parametrize("arm,ex,expected", [
    ("a", make(reroutes=1), "valid"),
    ("a", make(reroutes=0), "invalid_no_reroute"),
    ("a", make(reroutes=1, notices=1), "invalid_no_reroute"),
    ("a", make(reroutes=0, dispatches=0), "invalid_no_reroute"),
    # one rerouted, one not — no notice, so nothing else would have caught it
    ("a", make(reroutes=1, dispatches=2), "invalid_partial_reroute"),
    ("a", make(reroutes=2, dispatches=5), "invalid_partial_reroute"),
    # nothing rerouted at all stays the stronger, more specific verdict
    ("a", make(reroutes=0, dispatches=3), "invalid_no_reroute"),
    ("b", make(reroutes=0), "valid"),
    ("b", make(reroutes=1), "invalid_leak"),
    ("b", make(reroutes=0, notices=1), "valid"),
    # arm B is native by definition; unrerouted dispatches are the point
    ("b", make(reroutes=0, dispatches=4), "valid"),
])
def test_validity_rules(arm, ex, expected):
    assert runner.validity_for(arm, ex) == expected


def test_partial_reroute_is_derived_when_native_dispatches_is_absent():
    # older extraction.json files, and synthetic dicts, only have the counts
    assert runner.validity_for("a", {"dispatches": 3, "reroutes": 1}) == \
        "invalid_partial_reroute"


def test_partial_reroute_warning_names_the_counts(mixed):
    ex = runner.mark_validity("a", mixed)
    assert ex["validity"] == "invalid_partial_reroute"
    assert any("1 of 2" in w for w in ex["warnings"]), ex["warnings"]


def test_warn_stamp_alone_invalidates_arm_a():
    # --include-hook-events missing, or claude stopped emitting hook output:
    # the TANDEM_HOME stamp still says the hook declined
    ex = make(reroutes=1)
    ex["side_effects"] = {"new_subagent_logs": 0, "warn_stamp": True}
    assert runner.hook_declined(ex) is True
    assert runner.validity_for("a", ex) == "invalid_no_reroute"
    assert runner.validity_for("b", make(reroutes=0)) == "valid"


def test_zero_dispatches_in_arm_b_is_valid_but_warned():
    ex = runner.mark_validity("b", make(dispatches=0))
    assert ex["validity"] == "valid"
    assert any("dispatch" in w for w in ex["warnings"])


def test_mark_validity_on_the_real_fixtures(native, reroute):
    assert runner.mark_validity("a", reroute)["validity"] == "valid"
    assert runner.mark_validity("b", native)["validity"] == "valid"
    assert runner.mark_validity("a", dict(native))["validity"] == "invalid_no_reroute"
    assert runner.mark_validity("b", dict(reroute))["validity"] == "invalid_leak"


# --- TANDEM_HOME side-effect corroboration -----------------------------------


def test_side_effects_count_new_subagent_logs(tmp_path):
    home = tmp_path / "home"
    logs = home / "subagents" / "abc" / "logs"
    logs.mkdir(parents=True)
    (logs / "old.log").write_text("x")
    before = runner.snapshot_side_effects(home)
    (logs / "new.log").write_text("y")
    after = runner.side_effects_since(home, before, session_id="s1")
    assert after["new_subagent_logs"] == 1
    assert after["warn_stamp"] is False


def test_side_effects_see_the_warn_stamp_for_this_session(tmp_path):
    home = tmp_path / "home"
    before = runner.snapshot_side_effects(home)
    (home / "warned").mkdir(parents=True)
    (home / "warned" / "s1").touch()
    after = runner.side_effects_since(home, before, session_id="s1")
    assert after["warn_stamp"] is True
    assert runner.side_effects_since(home, before, session_id="other")["warn_stamp"] is False


def test_side_effects_on_a_missing_home_are_empty(tmp_path):
    before = runner.snapshot_side_effects(tmp_path / "nope")
    got = runner.side_effects_since(tmp_path / "nope", before, session_id="s")
    assert got == {"new_subagent_logs": 0, "warn_stamp": False}


def test_extraction_json_round_trips(reroute):
    # the extraction dict is written verbatim to extraction.json
    assert json.loads(json.dumps(runner.mark_validity("a", reroute)))["validity"] \
        == "valid"
