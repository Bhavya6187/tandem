"""bench/aggregate.py reads verdict.json files and nothing else, so these
tests build synthetic verdicts and check the arithmetic and the exclusions."""

import json

import pytest
from conftest import load_bench_module

aggregate = load_bench_module("aggregate")


def verdict(task="t1", arm="a", repeat=0, validity="valid", status="verified",
            passed=True, wall=10.0, tokens=1000, dispatches=2):
    return {
        "schema": 1, "run_id": "r1", "task": task, "family": "repoqa",
        "arm": arm, "repeat": repeat, "status": status, "passed": passed,
        "score": None, "validity": validity,
        "run": {"exit_code": 0, "timed_out": False, "wall_clock_s": wall},
        "metrics": {"tokens_total": tokens, "dispatches": dispatches,
                    "reroutes": 1 if arm == "a" else 0, "notices": 0},
    }


def write_run(tmp_path, verdicts, run_id="r1"):
    root = tmp_path / "results" / run_id
    for v in verdicts:
        d = root / v["task"] / v["arm"] / str(v["repeat"])
        d.mkdir(parents=True, exist_ok=True)
        (d / "verdict.json").write_text(json.dumps(v))
    return root


def test_load_verdicts_finds_every_nested_file(tmp_path):
    root = write_run(tmp_path, [verdict(repeat=0), verdict(repeat=1),
                                verdict(arm="b")])
    got = aggregate.load_verdicts(root)
    assert len(got) == 3


def test_load_verdicts_skips_unreadable_and_reports_it(tmp_path):
    root = write_run(tmp_path, [verdict()])
    bad = root / "t1" / "a" / "9"
    bad.mkdir(parents=True)
    (bad / "verdict.json").write_text("{oops")
    got = aggregate.load_verdicts(root)
    assert len(got) == 1


def test_load_verdicts_missing_dir_is_an_error(tmp_path):
    with pytest.raises(aggregate.AggregateError):
        aggregate.load_verdicts(tmp_path / "absent")


# --- math --------------------------------------------------------------------


def test_means_over_valid_runs_only():
    vs = [
        verdict(repeat=0, wall=10, tokens=100, dispatches=2),
        verdict(repeat=1, wall=20, tokens=300, dispatches=4),
        verdict(repeat=2, wall=999, tokens=999, dispatches=99,
                validity="invalid_no_reroute", passed=False),
    ]
    row = aggregate.summarize(vs)["rows"][0]
    assert row["runs"] == 3
    assert row["valid"] == 2
    assert row["invalid"] == 1
    assert row["mean_wall_s"] == pytest.approx(15.0)
    assert row["mean_tokens"] == pytest.approx(200.0)
    assert row["mean_dispatches"] == pytest.approx(3.0)


def test_pass_rate_counts_only_verified_valid_runs():
    vs = [
        verdict(repeat=0, passed=True),
        verdict(repeat=1, passed=False),
        verdict(repeat=2, passed=True, validity="invalid_leak"),
        verdict(repeat=3, status="unverified", passed=None),
    ]
    row = aggregate.summarize(vs)["rows"][0]
    assert row["verified"] == 2
    assert row["passes"] == 1
    assert row["pass_rate"] == pytest.approx(0.5)
    assert row["unverified"] == 1


def test_pass_rate_is_none_when_nothing_is_verified():
    row = aggregate.summarize([verdict(status="unverified", passed=None)])["rows"][0]
    assert row["pass_rate"] is None
    assert row["valid"] == 1


def test_all_invalid_group_has_no_means():
    row = aggregate.summarize(
        [verdict(validity="invalid_no_reroute", passed=None)])["rows"][0]
    assert row["valid"] == 0
    assert row["mean_wall_s"] is None
    assert row["mean_tokens"] is None
    assert row["pass_rate"] is None


def test_rows_are_grouped_by_task_and_arm_and_sorted():
    vs = [verdict(task="t2", arm="b"), verdict(task="t1", arm="b"),
          verdict(task="t1", arm="a")]
    rows = aggregate.summarize(vs)["rows"]
    assert [(r["task"], r["arm"]) for r in rows] == [
        ("t1", "a"), ("t1", "b"), ("t2", "b")]


def test_arm_totals_pool_across_tasks():
    vs = [verdict(task="t1", arm="a", wall=10, tokens=100),
          verdict(task="t2", arm="a", wall=30, tokens=300),
          verdict(task="t1", arm="b", wall=5, tokens=50,
                  validity="invalid_leak", passed=None)]
    totals = {t["arm"]: t for t in aggregate.summarize(vs)["by_arm"]}
    assert totals["a"]["mean_wall_s"] == pytest.approx(20.0)
    assert totals["a"]["mean_tokens"] == pytest.approx(200.0)
    assert totals["b"]["valid"] == 0
    assert totals["b"]["invalid"] == 1


def test_invalid_reasons_are_broken_out():
    vs = [verdict(validity="invalid_no_reroute", passed=None),
          verdict(repeat=1, validity="invalid_leak", passed=None),
          verdict(repeat=2)]
    row = aggregate.summarize(vs)["rows"][0]
    assert row["invalid_reasons"] == {"invalid_no_reroute": 1, "invalid_leak": 1}


def test_timeouts_are_counted_as_runs_not_as_invalid():
    v = verdict(passed=False)
    v["run"]["timed_out"] = True
    row = aggregate.summarize([v])["rows"][0]
    assert row["valid"] == 1 and row["invalid"] == 0 and row["timeouts"] == 1


# --- rendering ---------------------------------------------------------------


def test_render_is_a_markdown_table_with_a_row_per_task_arm():
    out = aggregate.render(aggregate.summarize(
        [verdict(task="t1", arm="a"), verdict(task="t1", arm="b", passed=False)]))
    lines = [ln for ln in out.splitlines() if ln.startswith("|")]
    assert lines[1].startswith("| ---")
    assert any("| t1 | a |" in ln for ln in lines)
    assert any("| t1 | b |" in ln for ln in lines)
    for col in ("task", "arm", "pass rate", "wall", "tokens", "dispatch", "invalid"):
        assert col in lines[0]


def test_render_shows_a_dash_for_unknown_pass_rate():
    out = aggregate.render(aggregate.summarize(
        [verdict(status="unverified", passed=None)]))
    assert "| - |" in out


def test_render_of_nothing_says_so():
    assert "no verdicts" in aggregate.render(aggregate.summarize([])).lower()
