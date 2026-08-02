"""SWE-bench Verified: fix a real issue in a real checkout, graded by docker.

provision(): fetch the dataset row (HF datasets-server REST, cached under
bench/work/cache/) and clone the repo at its `base_commit` into
<workdir>/repo. The agent gets the row's `problem_statement` and nothing else.

Deliberately NOT done here, and it is the part that is easy to get wrong:

  - `test_patch` is never applied. The evaluation harness applies it itself,
    after resetting the test files the model touched, which is what makes an
    agent that edits tests score zero rather than a hundred.
  - `environment_setup_commit` is never checked out. It names the commit whose
    dependency pins the harness built the docker image from; the agent works at
    `base_commit`. Both are recorded in meta.json so a result can be traced
    back to the exact image.
  - nothing is installed and no test is run inside the workdir. The image owns
    the environment. The agent edits source; that is the whole job.

verify(): `git diff` the workdir into a one-line predictions jsonl and hand it
to the real harness, in its own uv-managed environment, never imported:

    uvx --from swebench==4.1.0 python -m swebench.harness.run_evaluation \\
        --dataset_name <dataset> --predictions_path <p> \\
        -i <instance_id> --run_id <id>

Resolution is the harness's own: every FAIL_TO_PASS and PASS_TO_PASS test
passes. We read the flag, we do not recompute it.

Two facts about swebench 4.1.0 that the code below depends on (read off
run_evaluation.py / reporting.py, live-verified 2026-08-01):

  - its output paths are RELATIVE TO THE CWD. `logs/run_evaluation/<run_id>/
    <model>/<instance>/report.json` and the run summary `<model>.<run_id>.json`
    both land wherever the process was started, so it is started inside a
    per-run directory in the result dir. `--report_dir` looks like it would fix
    this; it does not — main() creates the directory and make_run_report()
    ignores it.
  - it needs docker, and it pulls the **x86_64** image whether or not an arm64
    one exists. `make_test_spec()` defaults `arch="x86_64"` and
    `run_evaluation` never overrides it (test_spec.py:180, run_evaluation.py:
    306), and 4.1.0's `USE_X86` set is dead code. So on darwin/arm64 every
    instance runs emulated. It works — a gold-patch run of
    django__django-11885 resolved in 123s on this machine — but it is slower
    than native and it makes `arm64_image = true` in bench/tasks.toml a record
    of the Docker Hub survey rather than of what actually gets pulled.

    A pull failure (the registry timing out mid-matrix is the realistic one)
    surfaces as the harness recording the instance in `error_ids` and writing
    no per-instance report, which parse_report turns into status "error" —
    retryable, and deliberately not a score of zero.

An empty diff is a FAILED run, not a broken one: the agent finishing without
editing anything is a result. It is recorded as passed=false with reason
"no_patch" and never reaches docker.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any, Iterable, Mapping

from family_api import PromptSpec
from family_common import (BenchFamilyError, cache_dir, cached_json, clone_at,
                           error_verdict, github_url, require_workdir, run_cmd,
                           slug, verdict, worktree_patch)

SWEBENCH_PIN = "swebench==4.1.0"
HARNESS_CMD = ("uvx", "--from", SWEBENCH_PIN, "python", "-m",
               "swebench.harness.run_evaluation")

MODEL_NAME = "bench"                 # KEY_MODEL in the predictions row
EVAL_TIMEOUT_S = 5400                # image pull + build + test run
INSTANCE_TIMEOUT_S = 1800            # the harness's own per-instance -t
# The harness's own default. At this level it deletes the ~2GB INSTANCE image
# after every run and keeps only the environment image, so each arm of each
# instance re-pulls — ten pulls for the five-instance matrix, and one more
# chance each for the registry to time out. "instance" trades roughly 10GB of
# disk for half the pulls. Left at the harness default because that is the
# configuration these verdicts were validated in; the README documents the
# knob.
EVAL_CACHE_LEVEL = "env"

ROW_URL = ("https://datasets-server.huggingface.co/filter"
           "?dataset={dataset}&config=default&split={split}"
           "&where=%22instance_id%22%3D%27{instance}%27&limit=1")

INSTRUCTION = """\
This checkout is {repo} at the commit immediately before the following issue
was fixed. Fix it.

=== ISSUE ===

{problem}

=== END ISSUE ===

How this is graded, so you do not waste effort in the wrong place:

- Only your changes to the source tree count. They are collected with `git
  diff` when you stop, and replayed against the project's own test suite in a
  prepared container.
- Do NOT add or modify tests. The graders' tests are added afterwards and any
  test file you touch is reset first, so time spent there is time lost.
- Do not install anything, do not run the project's test suite, and do not
  commit: the environment here is a bare checkout, and the grading environment
  is elsewhere.
- Make the smallest change that actually fixes the issue, and leave the
  existing behaviour of everything else alone — passing tests that already
  pass is half of the score."""


# --- the dataset row ----------------------------------------------------------


def _row_cache_path(task: Mapping[str, Any]) -> Path:
    return (cache_dir() / "swebench" /
            f"{slug(task.get('dataset', 'swebench'))}-{slug(task['instance_id'])}.json")


def fetch_row(task: Mapping[str, Any]) -> dict:
    """The pinned instance's dataset row, cached. The only network call here.

    Uses the datasets-server `filter` endpoint (a where= on instance_id) rather
    than a row offset, so the pin is the instance id itself and cannot be moved
    by the dataset being re-ordered."""
    instance = str(task.get("instance_id") or "")
    if not instance:
        raise BenchFamilyError(
            f"task {task.get('id')!r} has no instance_id; pin it in bench/tasks.toml")
    url = ROW_URL.format(dataset=str(task.get("dataset", "")).replace("/", "%2F"),
                         split=task.get("split", "test"), instance=instance)
    data = cached_json(url, _row_cache_path(task))
    rows = data.get("rows") if isinstance(data, dict) else None
    if not rows:
        raise BenchFamilyError(
            f"{task.get('dataset')} has no instance {instance!r} "
            f"({json.dumps(data)[:200]})")
    row = rows[0].get("row") or {}
    for key in ("base_commit", "problem_statement", "repo"):
        if not row.get(key):
            raise BenchFamilyError(
                f"the dataset row for {instance} has no {key}; the dataset "
                "schema changed and this provisioner needs updating")
    return row


# --- prompt -------------------------------------------------------------------


def build_prompt(row: Mapping[str, Any]) -> str:
    return INSTRUCTION.format(repo=row.get("repo", "this repository"),
                              problem=(row.get("problem_statement") or "").strip())


# --- predictions --------------------------------------------------------------


def prediction_row(instance_id: str, patch: str) -> dict:
    """The three fields the harness reads (KEY_INSTANCE_ID / KEY_MODEL /
    KEY_PREDICTION in swebench.harness.constants)."""
    return {"instance_id": instance_id, "model_name_or_path": MODEL_NAME,
            "model_patch": patch}


def write_predictions(path: Path | str, rows: Iterable[Mapping[str, Any]]) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(dict(r)) + "\n" for r in rows))
    return path


def eval_run_id(rundir: str) -> str:
    """A harness --run_id that identifies this one run and nothing else.

    The harness namespaces its logs, its containers and its report file by run
    id, so two runs sharing one would read each other's report — and worse,
    run_instance() SHORT-CIRCUITS on an existing report.json, so arm B would
    silently inherit arm A's verdict. The last four path segments
    (run/task/arm/repeat) are what make a run unique; the digest keeps ids that
    differ only further up the path apart."""
    parts = [p for p in Path(rundir).parts if p not in ("/", "")]
    tail = "-".join(parts[-4:]) if parts else "run"
    digest = hashlib.sha256(str(rundir).encode()).hexdigest()[:8]
    return f"{slug(tail)}-{digest}"[:120]


# --- report -------------------------------------------------------------------


def _counts(status: Mapping[str, Any] | None) -> dict:
    status = status or {}
    return {"success": len(status.get("success") or []),
            "failure": len(status.get("failure") or [])}


def parse_report(eval_dir: Path | str, run_id: str, instance_id: str) -> dict:
    """The harness's verdict for one instance.

    Prefers the per-instance report.json (the authoritative `resolved` flag
    plus the per-test breakdown) and falls back to the run summary, which is
    the only thing written when the instance errored out before grading."""
    eval_dir = Path(eval_dir)
    report_path = (eval_dir / "logs" / "run_evaluation" / run_id / MODEL_NAME /
                   instance_id / "report.json")
    if report_path.is_file():
        try:
            body = json.loads(report_path.read_text())
        except ValueError as exc:
            raise BenchFamilyError(f"{report_path} is not valid JSON: {exc}") from exc
        entry = body.get(instance_id)
        if not isinstance(entry, dict):
            raise BenchFamilyError(
                f"{report_path} has no entry for {instance_id} "
                f"(keys: {', '.join(map(str, body))})")
        tests = entry.get("tests_status") or {}
        return {
            "resolved": bool(entry.get("resolved")),
            "source": "report.json",
            "report_path": str(report_path),
            "patch_exists": bool(entry.get("patch_exists")),
            "patch_successfully_applied":
                bool(entry.get("patch_successfully_applied")),
            "fail_to_pass": _counts(tests.get("FAIL_TO_PASS")),
            "pass_to_pass": _counts(tests.get("PASS_TO_PASS")),
        }

    summary_path = eval_dir / f"{MODEL_NAME}.{run_id}.json"
    if summary_path.is_file():
        try:
            body = json.loads(summary_path.read_text())
        except ValueError as exc:
            raise BenchFamilyError(f"{summary_path} is not valid JSON: {exc}") from exc
        if instance_id in (body.get("error_ids") or []):
            raise BenchFamilyError(
                f"the harness recorded {instance_id} as errored; see "
                f"{eval_dir / 'logs' / 'run_evaluation' / run_id}")
        return {
            "resolved": instance_id in (body.get("resolved_ids") or []),
            "source": "run_summary",
            "report_path": str(summary_path),
            "patch_exists": instance_id not in (body.get("empty_patch_ids") or []),
            "patch_successfully_applied": None,
            "fail_to_pass": None, "pass_to_pass": None,
        }
    raise BenchFamilyError(
        f"the harness wrote no report for {instance_id} under {eval_dir}. It "
        "did not get as far as grading — check the harness log in that "
        "directory (docker not running, or no arm64 image for this instance?)")


# --- provision / verify -------------------------------------------------------


def provision(task: Mapping[str, Any], workdir: str) -> PromptSpec:
    row = fetch_row(task)
    repo_dir = Path(workdir) / "repo"
    clone_at(github_url(str(row["repo"])), repo_dir, str(row["base_commit"]))
    return PromptSpec(
        prompt=build_prompt(row),
        workdir=str(repo_dir),
        meta={
            "family": "swebench", "dataset": task.get("dataset"),
            "instance_id": row.get("instance_id"), "repo": row.get("repo"),
            "base_commit": row.get("base_commit"),
            # recorded, not used: the harness's image was built from this
            # commit's pins, the agent works at base_commit
            "environment_setup_commit": row.get("environment_setup_commit"),
            "version": row.get("version"), "difficulty": row.get("difficulty"),
            "problem_statement_chars": len(row.get("problem_statement") or ""),
            "test_patch_applied": False,
        },
    )


def _base_commit(task: Mapping[str, Any], rundir: Path | str) -> str:
    """The commit the agent started from, for the verifying diff.

    From the run's own meta.json first: the runner copies PromptSpec.meta there
    at provision time, so the answer is already sitting in the directory being
    verified and scoring needs no network at all. The cached dataset row is the
    fallback for a result dir written before that was recorded, and HEAD is the
    last resort — grading a patch that is probably complete beats refusing to
    grade."""
    try:
        meta = json.loads((Path(rundir) / "meta.json").read_text())
        sha = (meta.get("provision_meta") or {}).get("base_commit")
        if sha:
            return str(sha)
    except (OSError, ValueError, AttributeError):
        pass
    try:
        return str(fetch_row(task).get("base_commit") or "HEAD")
    except BenchFamilyError:
        return "HEAD"


def verify(task: Mapping[str, Any], rundir: str) -> dict:
    eval_dir = Path(rundir) / "swebench-eval"
    try:
        instance = str(task.get("instance_id") or "")
        if not instance:
            raise BenchFamilyError(
                f"task {task.get('id')!r} has no instance_id; pin it in "
                "bench/tasks.toml")
        repo_dir = require_workdir(task)
        if not (repo_dir / ".git").is_dir():
            raise BenchFamilyError(
                f"{repo_dir} is not a git checkout, so there is no patch to "
                "grade. Was the run provisioned?")
        # A retried --run-id lands on the same result path, so eval_run_id()
        # returns the same id, and run_instance() SHORT-CIRCUITS on an existing
        # report.json for that id: it would hand back the previous attempt's
        # `resolved` without ever looking at the new agent's patch. Nothing
        # from a previous attempt may survive into this one.
        shutil.rmtree(eval_dir, ignore_errors=True)
        # against the pinned base commit, not HEAD: an agent that committed
        # its fix (the prompt says not to; that is not a guarantee) would
        # otherwise diff clean and be scored as having done nothing
        patch = worktree_patch(repo_dir, since=_base_commit(task, rundir))
        eval_dir.mkdir(parents=True, exist_ok=True)
        (eval_dir / "model_patch.diff").write_text(patch)
        if not patch.strip():
            return verdict("verified", False, 0.0, reason="no_patch",
                           instance_id=instance, patch_chars=0,
                           note="the agent finished without changing any file")

        preds = write_predictions(eval_dir / "predictions.jsonl",
                                  [prediction_row(instance, patch)])
        run_id = eval_run_id(rundir)
        cmd = [*HARNESS_CMD,
               "--dataset_name", str(task.get("dataset")),
               "--split", str(task.get("split", "test")),
               "--predictions_path", str(preds),
               "-i", instance,
               "--run_id", run_id,
               "--max_workers", "1",
               "--cache_level", EVAL_CACHE_LEVEL,
               "-t", str(INSTANCE_TIMEOUT_S)]
        # cwd, not --report_dir: swebench 4.1.0 writes both the logs tree and
        # the run summary relative to the process cwd (see module docstring)
        p = run_cmd(cmd, cwd=eval_dir, timeout=EVAL_TIMEOUT_S)
        (eval_dir / "run_evaluation.log").write_text(
            f"$ {' '.join(cmd)}\n\n--- stdout ---\n{p.stdout}\n"
            f"--- stderr ---\n{p.stderr}\n")

        got = parse_report(eval_dir, run_id, instance)
        return verdict(
            "verified", got["resolved"], 1.0 if got["resolved"] else 0.0,
            instance_id=instance, patch_chars=len(patch), run_id=run_id,
            harness=SWEBENCH_PIN, harness_exit=p.returncode,
            harness_cmd=cmd, eval_dir=str(eval_dir), **got)
    except Exception as exc:                        # noqa: BLE001 - verdict, not crash
        return error_verdict(exc, task=task.get("id"), eval_dir=str(eval_dir))
