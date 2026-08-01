"""The verifier CLI: it walks a run's result dirs, calls the family's
verify(), and merges the outcome into each verdict.json.

No network, no docker: the family module is a fake written into tmp_path and
loaded exactly the way the runner loads a real one."""

import json
from pathlib import Path

import pytest
from conftest import load_bench_module

verify_cli = load_bench_module("verify")

FAKE_FAMILY = '''\
import json
from pathlib import Path


def provision(task, workdir):
    raise NotImplementedError


def verify(task, rundir):
    marker = Path(rundir) / "answer.txt"
    if marker.read_text().strip() == "boom":
        raise RuntimeError("verifier exploded")
    passed = marker.read_text().strip() == "good"
    return {"status": "verified", "passed": passed,
            "score": 1.0 if passed else 0.0,
            "detail": {"workdir": task["_workdir"], "task": task["id"]}}
'''

TASKS = """\
[run]
repeats = 1

[[tasks]]
id = "t1"
family = "fake"
"""


@pytest.fixture
def rig(tmp_path):
    fam = tmp_path / "families"
    fam.mkdir()
    (fam / "fake.py").write_text(FAKE_FAMILY)
    tasks = tmp_path / "tasks.toml"
    tasks.write_text(TASKS)
    results = tmp_path / "results"

    def add_run(arm="a", repeat=0, answer="good", status="unverified", task="t1"):
        rd = results / "RUN1" / task / arm / str(repeat)
        rd.mkdir(parents=True)
        wd = tmp_path / "workdirs" / task / arm / str(repeat)
        wd.mkdir(parents=True)
        (rd / "answer.txt").write_text(answer)
        (rd / "verdict.json").write_text(json.dumps({
            "schema": 1, "run_id": "RUN1", "task": task, "family": "fake",
            "arm": arm, "repeat": repeat, "status": status, "passed": None,
            "score": None, "detail": {}, "validity": "valid", "warnings": [],
            "run": {"exit_code": 0}, "metrics": {"dispatches": 2},
            "result_dir": str(rd), "workdir": str(wd),
        }))
        return rd

    def run(*extra):
        return verify_cli.main([
            "--run-id", "RUN1", "--results-dir", str(results),
            "--tasks-file", str(tasks), "--family-dir", str(fam), *extra])

    return type("Rig", (), {"add_run": staticmethod(add_run),
                            "run": staticmethod(run),
                            "results": results, "tmp": tmp_path})


def _verdict(rd):
    return json.loads((Path(rd) / "verdict.json").read_text())


def test_verify_merges_a_pass_into_the_verdict(rig, capsys):
    rd = rig.add_run(answer="good")
    assert rig.run() == 0
    v = _verdict(rd)
    assert v["status"] == "verified" and v["passed"] is True and v["score"] == 1.0
    assert v["detail"]["task"] == "t1"
    # the runner's own fields survive untouched
    assert v["metrics"]["dispatches"] == 2 and v["validity"] == "valid"


def test_verify_merges_a_fail(rig):
    rd = rig.add_run(answer="bad")
    assert rig.run() == 0
    assert _verdict(rd)["passed"] is False


def test_verify_passes_the_workdir_to_the_family(rig):
    rd = rig.add_run()
    rig.run()
    assert _verdict(rd)["detail"]["workdir"] == _verdict(rd)["workdir"]


def test_verify_records_an_exploding_verifier_as_an_error_and_keeps_going(rig, capsys):
    bad = rig.add_run(arm="a", answer="boom")
    good = rig.add_run(arm="b", answer="good")
    rc = rig.run()
    assert rc == 1
    assert _verdict(bad)["status"] == "error"
    assert _verdict(bad)["passed"] is None
    assert "verifier exploded" in json.dumps(_verdict(bad)["detail"])
    assert _verdict(good)["status"] == "verified"     # the other run still ran


def test_verify_skips_already_verified_runs_unless_forced(rig):
    rd = rig.add_run(answer="good")
    rig.run()
    (Path(rd) / "answer.txt").write_text("bad")
    rig.run()
    assert _verdict(rd)["passed"] is True             # skipped: unchanged
    rig.run("--force")
    assert _verdict(rd)["passed"] is False


def test_verify_can_be_narrowed_to_one_arm(rig):
    a = rig.add_run(arm="a")
    b = rig.add_run(arm="b")
    assert rig.run("--arms", "b") == 0
    assert _verdict(a)["status"] == "unverified"
    assert _verdict(b)["status"] == "verified"


def test_verify_can_be_narrowed_to_one_task(rig):
    t1 = rig.add_run(arm="a", task="t1")
    other = rig.add_run(arm="a", task="ghost")
    assert rig.run("--tasks", "t1") == 0
    assert _verdict(t1)["status"] == "verified"
    assert _verdict(other)["status"] == "unverified"


def test_verify_reports_an_unknown_run_id(rig, capsys):
    assert verify_cli.main(["--run-id", "NOPE", "--results-dir", str(rig.results),
                            "--tasks-file", str(rig.tmp / "tasks.toml"),
                            "--family-dir", str(rig.tmp / "families")]) == 1


def test_verify_reports_a_verdict_whose_task_is_not_in_tasks_toml(rig):
    rd = rig.add_run(task="ghost")
    rc = rig.run()
    assert rc == 1
    assert _verdict(rd)["status"] == "error"
    assert "ghost" in json.dumps(_verdict(rd)["detail"])
