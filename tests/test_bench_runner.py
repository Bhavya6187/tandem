"""The runner's process loop, exercised end-to-end against fake `claude`,
`tandem` and `pair.py` executables and a fake family provisioner.

Nothing here touches the network, docker, the real claude CLI or the user's
~/.tandem: the fakes are stdlib python scripts written into tmp_path, and the
canned stream-json they replay is the sanitized live fixture pair."""

import json
import os
import signal
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import (BENCH_DIR, BENCH_FIXTURES, bench_mixed_stream,
                      load_bench_module)

runner = load_bench_module("runner")

FAKE_CLAUDE = '''\
import json, os, sys, time
args = sys.argv[1:]
if args[:1] == ["--version"]:
    print("9.9.9 (Claude Code)"); sys.exit(0)
if args[:2] == ["plugin", "list"]:
    print(json.dumps([{"id": "tandem@tandem", "version": "0.1.5",
                      "scope": "user", "enabled": True}])); sys.exit(0)
rec = os.environ["FAKE_RECORD_DIR"]
os.makedirs(rec, exist_ok=True)
n = len(os.listdir(rec))
with open(os.path.join(rec, "invocation-%d.json" % n), "w") as fh:
    json.dump({"argv": args, "cwd": os.getcwd(),
               "tandem_home": os.environ.get("TANDEM_HOME"),
               "path0": os.environ.get("PATH", "").split(os.pathsep)[0]}, fh)
time.sleep(float(os.environ.get("FAKE_SLEEP", "0")))
route = ""
home = os.environ.get("TANDEM_HOME", "")
try:
    route = open(os.path.join(home, "config.toml")).read()
except OSError:
    pass
stream = os.environ["FAKE_STREAM_A"] if 'route = "all"' in route \\
    else os.environ["FAKE_STREAM_B"]
sys.stdout.write(open(stream).read())
sys.exit(int(os.environ.get("FAKE_EXIT", "0")))
'''

FAKE_CODEX = '''\
import sys
print("codex-cli 0.145.0"); sys.exit(0)
'''

FAKE_TANDEM = '''\
import json, sys
if sys.argv[1:2] == ["--version"]:
    print("tandem, version 9.9.9"); sys.exit(0)
if sys.argv[1:2] == ["hook-route"]:
    sys.stdin.read()
    print(json.dumps({"systemMessage": "tandem: subagent plugin is active but "
                      "this directory has no paired tandem session"}))
    sys.exit(0)
sys.exit(2)
'''

FAKE_PAIR = '''\
import json, os, sys
rec = os.environ["FAKE_RECORD_DIR"]
os.makedirs(rec, exist_ok=True)
with open(os.path.join(rec, "pair.log"), "a") as fh:
    fh.write(json.dumps(sys.argv[1:]) + "\\n")
sys.exit(int(os.environ.get("FAKE_PAIR_EXIT", "0")))
'''

FAKE_FAMILY = '''\
from pathlib import Path


def provision(task, workdir):
    Path(workdir, "README.md").write_text("Widget Toolkit\\n")
    return {"prompt": "Find the widget in %s." % task["id"],
            "workdir": workdir, "meta": {"provisioned": True}}


def verify(task, rundir):
    return {"status": "verified", "passed": True}
'''

TASKS_TOML = '''\
[run]
repeats = 1
model = ""
arms = ["a", "b"]
claude_flags = []
smoke_task = "fake-1"

[run.timeout_s]
fake = 60

[[tasks]]
id = "fake-1"
family = "fake"
'''


def write_script(path, body):
    path.write_text("#!" + sys.executable + "\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IRWXU)
    return path


@pytest.fixture
def rig(tmp_path, monkeypatch):
    """A complete fake environment: binaries, family dir, tasks file, work dir."""
    binder = tmp_path / "bin"
    binder.mkdir()
    fam = tmp_path / "families"
    fam.mkdir()
    (fam / "fake.py").write_text(FAKE_FAMILY)
    tasks = tmp_path / "tasks.toml"
    tasks.write_text(TASKS_TOML)
    rec = tmp_path / "rec"
    monkeypatch.setenv("FAKE_RECORD_DIR", str(rec))
    monkeypatch.setenv("FAKE_STREAM_A", str(BENCH_FIXTURES / "stream-reroute.jsonl"))
    monkeypatch.setenv("FAKE_STREAM_B", str(BENCH_FIXTURES / "stream-native.jsonl"))

    class Rig:
        work = tmp_path / "work"
        claude = write_script(binder / "claude", FAKE_CLAUDE)
        tandem = write_script(binder / "tandem", FAKE_TANDEM)
        codex = write_script(binder / "codex", FAKE_CODEX)
        pair = write_script(binder / "pair.py", FAKE_PAIR)
        family_dir = fam
        tasks_file = tasks
        record = rec

        def argv(self, *extra):
            return [
                "--tasks-file", str(self.tasks_file),
                "--work-dir", str(self.work),
                "--family-dir", str(self.family_dir),
                "--claude-bin", str(self.claude),
                "--codex-bin", str(self.codex),
                "--tandem-bin", str(self.tandem),
                "--pair-script", str(self.pair),
                *extra,
            ]

        def invocations(self):
            return [json.loads(p.read_text())
                    for p in sorted(self.record.glob("invocation-*.json"))]

        def pair_calls(self):
            p = self.record / "pair.log"
            return [json.loads(ln) for ln in p.read_text().splitlines()] \
                if p.exists() else []

    return Rig()


def rundir(rig, task="fake-1", arm="a", repeat=0, run_id="t1"):
    return rig.work / "results" / run_id / task / arm / str(repeat)


def rp(p):
    """tmp_path is under a symlinked temp root on macOS, and the runner
    realpaths everything it hands to pair.py and to claude."""
    return os.path.realpath(str(p))


def both(capsys):
    cap = capsys.readouterr()
    return cap.out + cap.err


# --- check -------------------------------------------------------------------


def test_check_passes_with_everything_present(rig, capsys):
    assert runner.main(["check", *rig.argv()]) == 0
    out = capsys.readouterr().out
    assert "claude" in out and "codex" not in out.split("FAIL")[0].split("\n")[0]


def test_check_creates_both_tandem_homes_with_the_right_route(rig):
    runner.main(["check", *rig.argv()])
    a = (rig.work / "tandem-home-a" / "config.toml").read_text()
    b = (rig.work / "tandem-home-b" / "config.toml").read_text()
    assert 'route = "all"' in a
    assert 'route = "off"' in b


def test_check_fails_clearly_when_claude_is_missing(rig, capsys):
    rc = runner.main(["check", *rig.argv("--claude-bin", "/nonexistent/claude")])
    assert rc != 0
    assert "claude" in capsys.readouterr().out.lower()


def test_check_fails_when_the_plugin_is_not_installed(rig, tmp_path, capsys):
    write_script(tmp_path / "bin" / "claude", FAKE_CLAUDE.replace(
        '"id": "tandem@tandem"', '"id": "other@somewhere"'))
    rc = runner.main(["check", *rig.argv()])
    assert rc != 0
    out = capsys.readouterr().out
    assert "plugin" in out.lower() and "tandem" in out


def test_check_fails_when_the_tandem_binary_has_no_hook_route(rig, tmp_path, capsys):
    write_script(tmp_path / "bin" / "tandem", 'import sys; sys.exit(2)')
    rc = runner.main(["check", *rig.argv()])
    assert rc != 0
    assert "hook-route" in capsys.readouterr().out


def test_check_does_not_need_docker_without_swebench_tasks(rig, monkeypatch):
    monkeypatch.setattr(runner, "docker_ok", lambda: (False, "docker is down"))
    assert runner.main(["check", *rig.argv("--tasks", "fake-1")]) == 0


def test_check_needs_docker_for_swebench_tasks(rig, monkeypatch, capsys):
    monkeypatch.setattr(runner, "docker_ok", lambda: (False, "docker is down"))
    rc = runner.main(["check", *rig.argv(
        "--tasks-file", str(BENCH_DIR / "tasks.toml"),
        "--tasks", "django__django-16263", "--family-dir", str(runner.DEFAULT_FAMILY_DIR))])
    assert rc != 0
    assert "docker" in capsys.readouterr().out.lower()


def test_check_reports_unrunnable_lca_tasks(rig, capsys):
    rc = runner.main(["check", *rig.argv(
        "--tasks-file", str(BENCH_DIR / "tasks.toml"),
        "--tasks", "lca-1", "--family-dir", str(runner.DEFAULT_FAMILY_DIR))])
    assert rc != 0
    assert "lca-1" in capsys.readouterr().out


# --- run ---------------------------------------------------------------------


def test_run_writes_the_full_result_layout(rig):
    assert runner.main(["run", *rig.argv("--run-id", "t1", "--arms", "a")]) == 0
    d = rundir(rig)
    for name in ("transcript.jsonl", "meta.json", "extraction.json",
                 "verdict.json", "prompt.txt"):
        assert (d / name).is_file(), name


def test_run_arm_a_is_valid_and_arm_b_is_valid(rig):
    runner.main(["run", *rig.argv("--run-id", "t1", "--arms", "a,b")])
    a = json.loads((rundir(rig, arm="a") / "extraction.json").read_text())
    b = json.loads((rundir(rig, arm="b") / "extraction.json").read_text())
    assert a["validity"] == "valid" and a["reroutes"] == 1
    assert b["validity"] == "valid" and b["reroutes"] == 0


def test_run_marks_a_leak_in_arm_b(rig, monkeypatch):
    # arm B replaying the rerouted stream = a reroute that should not exist
    monkeypatch.setenv("FAKE_STREAM_B", str(BENCH_FIXTURES / "stream-reroute.jsonl"))
    runner.main(["run", *rig.argv("--run-id", "t1", "--arms", "b")])
    b = json.loads((rundir(rig, arm="b") / "extraction.json").read_text())
    assert b["validity"] == "invalid_leak"


def test_run_marks_a_partial_reroute_in_arm_a(rig, monkeypatch, tmp_path):
    mixed = tmp_path / "mixed.jsonl"
    mixed.write_text("".join(json.dumps(e) + "\n" for e in bench_mixed_stream()))
    monkeypatch.setenv("FAKE_STREAM_A", str(mixed))
    runner.main(["run", *rig.argv("--run-id", "t1", "--arms", "a")])
    a = json.loads((rundir(rig, arm="a") / "extraction.json").read_text())
    v = json.loads((rundir(rig, arm="a") / "verdict.json").read_text())
    assert a["reroutes"] == 1 and a["native_dispatches"] == 1
    assert a["validity"] == v["validity"] == "invalid_partial_reroute"
    assert any("1 of 2" in w for w in v["warnings"])


def test_run_marks_arm_a_without_reroute(rig, monkeypatch):
    monkeypatch.setenv("FAKE_STREAM_A", str(BENCH_FIXTURES / "stream-notice.jsonl"))
    runner.main(["run", *rig.argv("--run-id", "t1", "--arms", "a")])
    a = json.loads((rundir(rig, arm="a") / "extraction.json").read_text())
    assert a["validity"] == "invalid_no_reroute"
    assert a["notices"] == 1


def test_run_pairs_the_workdir_and_verifies_before_launch(rig):
    runner.main(["run", *rig.argv("--run-id", "t1", "--arms", "a,b")])
    calls = rig.pair_calls()
    assert len(calls) == 2, calls
    # exactly one workdir, paired then verified, and it is arm A's
    workdir = rp(rig.work / "workdirs" / "t1" / "fake-1" / "a" / "0")
    assert all(workdir in c for c in calls)
    assert "--verify-only" not in calls[0]
    assert "--verify-only" in calls[1]
    assert rp(rig.work / "tandem-home-a") in calls[0]


def test_run_aborts_the_arm_a_launch_when_pairing_fails(rig, monkeypatch, capsys):
    monkeypatch.setenv("FAKE_PAIR_EXIT", "1")
    rc = runner.main(["run", *rig.argv("--run-id", "t1", "--arms", "a")])
    assert rc != 0
    assert rig.invocations() == []
    assert "pair" in both(capsys).lower()


def test_arm_b_is_never_paired(rig):
    runner.main(["run", *rig.argv("--run-id", "t1", "--arms", "b")])
    assert rig.pair_calls() == []


def test_run_gives_claude_the_arm_env(rig):
    runner.main(["run", *rig.argv("--run-id", "t1", "--arms", "a,b")])
    inv = {Path(i["tandem_home"]).name: i for i in rig.invocations()}
    assert set(inv) == {"tandem-home-a", "tandem-home-b"}
    for i in inv.values():
        # the tandem the hook will resolve is the one the runner chose
        assert i["path0"] == rp(rig.tandem.parent)
        assert i["cwd"].endswith(os.path.join("fake-1", i["tandem_home"][-1], "0"))


def test_prompt_is_identical_across_arms_and_mandates_investigate_only(rig):
    runner.main(["run", *rig.argv("--run-id", "t1", "--arms", "a,b")])
    a = (rundir(rig, arm="a") / "prompt.txt").read_text()
    b = (rundir(rig, arm="b") / "prompt.txt").read_text()
    assert a == b
    low = a.lower()
    assert "subagent" in low
    assert "never" in low and ("edit" in low or "modif" in low)
    assert "Find the widget in fake-1." in a


def test_claude_argv_carries_the_stream_flags(rig):
    runner.main(["run", *rig.argv("--run-id", "t1", "--arms", "b",
                                  "--model", "haiku")])
    argv = rig.invocations()[0]["argv"]
    assert argv[0] == "-p"
    assert "--output-format" in argv and "stream-json" in argv
    assert "--verbose" in argv
    assert "--include-hook-events" in argv
    assert argv[argv.index("--model") + 1] == "haiku"


def test_repeats_produce_separate_result_dirs(rig):
    runner.main(["run", *rig.argv("--run-id", "t1", "--arms", "b",
                                  "--repeats", "2")])
    assert rundir(rig, arm="b", repeat=0).is_dir()
    assert rundir(rig, arm="b", repeat=1).is_dir()
    assert len(rig.invocations()) == 2


def test_meta_records_timing_exit_and_versions(rig):
    runner.main(["run", *rig.argv("--run-id", "t1", "--arms", "b")])
    meta = json.loads((rundir(rig, arm="b") / "meta.json").read_text())
    assert meta["exit_code"] == 0
    assert meta["timed_out"] is False
    assert meta["wall_clock_s"] >= 0
    assert meta["claude_version"] == "9.9.9"
    assert meta["task"] == "fake-1" and meta["arm"] == "b"
    assert meta["family"] == "fake"


def test_verdict_skeleton_is_unverified_but_carries_the_metrics(rig):
    runner.main(["run", *rig.argv("--run-id", "t1", "--arms", "a")])
    v = json.loads((rundir(rig) / "verdict.json").read_text())
    assert v["status"] == "unverified"
    assert v["passed"] is None
    assert v["validity"] == "valid"
    assert v["metrics"]["reroutes"] == 1
    assert v["metrics"]["tokens_total"] > 0
    assert v["run"]["wall_clock_s"] >= 0


def test_provisioner_ran_in_the_workdir(rig):
    runner.main(["run", *rig.argv("--run-id", "t1", "--arms", "b")])
    assert (rig.work / "workdirs" / "t1" / "fake-1" / "b" / "0"
            / "README.md").is_file()


def test_a_provisioner_may_redirect_the_run_into_a_subdirectory(rig):
    (rig.family_dir / "fake.py").write_text(
        'from pathlib import Path\n'
        'def provision(task, workdir):\n'
        '    d = Path(workdir, "repo"); d.mkdir(exist_ok=True)\n'
        '    return {"prompt": "p", "workdir": str(d)}\n'
        'def verify(task, rundir):\n'
        '    return {}\n')
    runner.main(["run", *rig.argv("--run-id", "t1", "--arms", "a")])
    assert rig.invocations()[0]["cwd"].endswith(os.path.join("0", "repo"))
    assert all(c[c.index("--cwd") + 1].endswith(os.path.join("0", "repo"))
               for c in rig.pair_calls())


def test_a_provisioner_returning_a_bad_workdir_is_rejected(rig, capsys):
    (rig.family_dir / "fake.py").write_text(
        'def provision(task, workdir):\n'
        '    return {"prompt": "p", "workdir": workdir + "/ghost"}\n'
        'def verify(task, rundir):\n'
        '    return {}\n')
    assert runner.main(["run", *rig.argv("--run-id", "t1", "--arms", "b")]) != 0
    assert "not a directory" in both(capsys)


def test_tandem_home_config_keeps_operator_keys_but_not_a_wrong_route(tmp_path):
    work = tmp_path / "w"
    (work / "tandem-home-a").mkdir(parents=True)
    cfg = work / "tandem-home-a" / "config.toml"
    cfg.write_text('[subagents]\nroute = "all"\nmodel = "gpt-5-codex-mini"\n')
    runner.ensure_tandem_homes(work)
    assert "gpt-5-codex-mini" in cfg.read_text()

    cfg.write_text('[subagents]\nroute = "off"\nmodel = "gpt-5-codex-mini"\n')
    runner.ensure_tandem_homes(work)
    assert 'route = "all"' in cfg.read_text()
    assert "gpt-5-codex-mini" not in cfg.read_text()


def test_nonzero_claude_exit_is_recorded_not_fatal(rig, monkeypatch):
    monkeypatch.setenv("FAKE_EXIT", "1")
    rc = runner.main(["run", *rig.argv("--run-id", "t1", "--arms", "b")])
    meta = json.loads((rundir(rig, arm="b") / "meta.json").read_text())
    assert meta["exit_code"] == 1
    assert rc == 0  # the run happened; the verifier decides pass/fail


def test_timeout_kills_and_marks_the_run_failed_not_invalid(rig, monkeypatch):
    monkeypatch.setenv("FAKE_SLEEP", "30")
    rig.tasks_file.write_text(TASKS_TOML.replace("fake = 60", "fake = 1"))
    runner.main(["run", *rig.argv("--run-id", "t1", "--arms", "b")])
    meta = json.loads((rundir(rig, arm="b") / "meta.json").read_text())
    v = json.loads((rundir(rig, arm="b") / "verdict.json").read_text())
    assert meta["timed_out"] is True
    assert v["run"]["timed_out"] is True
    assert v["status"] == "unverified"
    assert v["validity"] != "invalid_leak"


def test_dry_run_plans_without_launching(rig, capsys):
    assert runner.main(["run", *rig.argv("--run-id", "t1", "--arms", "a,b",
                                         "--dry-run")]) == 0
    assert rig.invocations() == []
    assert "fake-1" in capsys.readouterr().out


def test_run_refuses_an_unrunnable_task(rig, capsys):
    rc = runner.main(["run", *rig.argv(
        "--run-id", "t1", "--arms", "b",
        "--tasks-file", str(BENCH_DIR / "tasks.toml"),
        "--family-dir", str(runner.DEFAULT_FAMILY_DIR), "--tasks", "lca-2")])
    assert rc != 0
    assert "lca-2" in both(capsys)


def test_run_id_defaults_to_a_timestamp(rig):
    runner.main(["run", *rig.argv("--arms", "b")])
    ids = [p.name for p in (rig.work / "results").iterdir()]
    assert len(ids) == 1 and ids[0][:2] == "20"


def test_smoke_runs_the_pinned_smoke_task_on_both_arms(rig):
    assert runner.main(["smoke", *rig.argv("--run-id", "s1")]) == 0
    assert len(rig.invocations()) == 2
    for arm in ("a", "b"):
        assert rundir(rig, arm=arm, run_id="s1").is_dir()


# --- process control ---------------------------------------------------------


def test_ctrl_c_kills_the_claude_process_group_before_propagating(
        tmp_path, monkeypatch):
    """SIGINT reaches the runner but not claude: _launch opens the child in its
    own session, so nothing else would ever kill it."""
    killed = []
    monkeypatch.setattr(runner, "_kill_group", lambda p: killed.append(p.pid))
    real_wait = subprocess.Popen.wait
    seen = {"n": 0}

    def fake_wait(self, timeout=None):
        seen["n"] += 1
        if seen["n"] == 1:
            raise KeyboardInterrupt
        return real_wait(self, timeout=timeout)

    monkeypatch.setattr(subprocess.Popen, "wait", fake_wait)
    with pytest.raises(KeyboardInterrupt):
        runner._launch([sys.executable, "-c", "import time; time.sleep(0.3)"],
                       str(tmp_path), dict(os.environ),
                       tmp_path / "o", tmp_path / "e", 60)
    assert len(killed) == 1


def test_kill_group_kills_a_real_child(tmp_path):
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                            start_new_session=True)
    runner._kill_group(proc)
    assert proc.poll() is not None


def test_kill_group_still_tries_sigkill_after_a_failed_sigterm(monkeypatch):
    sent = []

    def fake_killpg(pgid, sig):
        sent.append(sig)
        if sig == signal.SIGTERM:
            raise PermissionError("not permitted")

    monkeypatch.setattr(os, "killpg", fake_killpg)
    monkeypatch.setattr(os, "getpgid", lambda pid: pid)

    class Proc:
        pid = 424242
        returncode = None

        def __init__(self):
            self.waits = 0

        def wait(self, timeout=None):
            self.waits += 1
            if self.waits == 1:
                raise subprocess.TimeoutExpired("claude", timeout)
            return -9

    runner._kill_group(Proc())
    assert sent == [signal.SIGTERM, signal.SIGKILL]


# --- family contract ---------------------------------------------------------


@pytest.mark.parametrize("family", ["swebench", "repoqa", "lca"])
def test_real_family_stubs_are_declared_but_not_implemented(family):
    mod = runner.load_family(family, runner.DEFAULT_FAMILY_DIR)
    with pytest.raises(NotImplementedError):
        mod.provision({"id": "x", "family": family}, "/tmp/nope")
    with pytest.raises(NotImplementedError):
        mod.verify({"id": "x", "family": family}, "/tmp/nope")


def test_known_families_matches_the_canonical_tuple():
    assert set(runner.known_families(runner.DEFAULT_FAMILY_DIR)) == set(runner.FAMILIES)


def test_load_family_missing_module_is_a_bench_error(tmp_path):
    with pytest.raises(runner.BenchError):
        runner.load_family("ghost", tmp_path)
