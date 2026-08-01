#!/usr/bin/env python3
"""Score a finished run: verdict.json -> verified.

    python bench/verify.py --run-id 20260801T191700Z
    python bench/verify.py --run-id X --tasks repoqa-python-black --arms a
    python bench/verify.py --run-id X --force        # re-score verified runs

The runner deliberately never calls verify(): scoring shells out to docker and
uvx, takes minutes to an hour, and must not sit inside the window the bench is
timing. So a run ends `"status": "unverified"` and this walks back over the
result directories afterwards, calls the family's verify(), and merges
status/passed/score/detail into each verdict.json in place. Everything the
runner wrote — validity, warnings, metrics — is left exactly as it was; the
aggregator reads only verdict.json and must never see the two disagree.

A verifier that raises is recorded as `"status": "error"` on that one run and
the walk continues. One instance with no arm64 image, or a docker daemon that
died halfway, should not cost a whole matrix its scores.

`--force` re-runs a family's verify() on runs already marked verified. For
repoqa that genuinely re-scores; for swebench the harness short-circuits on an
existing report.json for the same run id, so a forced re-verify replays the
previous grading rather than repeating an hour of docker. Delete the run's
swebench-eval/ directory if you want the eval done again for real.

stdlib only.
"""

from __future__ import annotations

import sys

if sys.version_info < (3, 11):                      # tomllib, and the `X | Y` types
    raise SystemExit(
        "bench/verify.py needs Python 3.11+ (got "
        f"{sys.version_info.major}.{sys.version_info.minor}). Use the repo's "
        "venv: .venv/bin/python bench/verify.py ..."
    )

import argparse
import json
import traceback
from pathlib import Path
from typing import Any, Iterable, Mapping

BENCH_DIR = Path(__file__).resolve().parent
if str(BENCH_DIR) not in sys.path:
    sys.path.insert(0, str(BENCH_DIR))

import runner                                        # noqa: E402  (after sys.path)

DEFAULT_RESULTS_DIR = BENCH_DIR / "work" / "results"


class VerifyError(Exception):
    """The message is user-facing."""


def _split(value: str | None) -> list[str] | None:
    if value is None:
        return None
    return [v.strip() for v in value.split(",") if v.strip()]


def find_verdicts(run_dir: Path | str) -> list[Path]:
    """Every verdict.json under <results>/<run id>/<task>/<arm>/<repeat>/."""
    run_dir = Path(run_dir)
    if not run_dir.is_dir():
        raise VerifyError(
            f"no such run directory: {run_dir}. Has this run id been run yet?")
    return sorted(run_dir.glob("*/*/*/verdict.json"))


def _load(path: Path) -> dict:
    try:
        obj = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise VerifyError(f"cannot read {path}: {exc}") from exc
    if not isinstance(obj, dict):
        raise VerifyError(f"{path} is not a verdict object")
    return obj


def selected(verdicts: Iterable[Path], tasks: list[str] | None,
             arms: list[str] | None) -> list[Path]:
    out = []
    for p in verdicts:
        # <results>/<run>/<task>/<arm>/<repeat>/verdict.json
        task, arm = p.parts[-4], p.parts[-3]
        if tasks and task not in tasks:
            continue
        if arms and arm not in arms:
            continue
        out.append(p)
    return out


def merge_verdict(verdict: dict, result: Mapping[str, Any]) -> dict:
    """Overwrite exactly the four fields verify() owns."""
    verdict["status"] = result.get("status") or "error"
    verdict["passed"] = result.get("passed")
    verdict["score"] = result.get("score")
    verdict["detail"] = dict(result.get("detail") or {})
    return verdict


def verify_one(path: Path, task: Mapping[str, Any], family_dir: Path) -> dict:
    """Score the run at `path`'s directory and rewrite its verdict.json."""
    verdict = _load(path)
    rundir = str(path.parent)
    try:
        mod = runner.load_family(str(verdict.get("family") or task.get("family")),
                                 family_dir)
        result = mod.verify(dict(task, _workdir=verdict.get("workdir") or ""), rundir)
        if not isinstance(result, Mapping) or "status" not in result:
            raise VerifyError(
                f"{verdict.get('family')}.verify() returned {type(result).__name__}, "
                "not the dict bench/family_api.py documents")
    except Exception as exc:                        # noqa: BLE001 - user code
        result = {"status": "error", "passed": None, "score": None,
                  "detail": {"error": f"{type(exc).__name__}: {exc}",
                             "traceback": traceback.format_exc()[-2000:]}}
    merge_verdict(verdict, result)
    path.write_text(json.dumps(verdict, indent=2))
    return verdict


def _line(verdict: Mapping[str, Any]) -> str:
    status = verdict.get("status")
    mark = {"verified": "ok  ", "error": "ERR "}.get(status, "??? ")
    if status == "verified":
        outcome = "PASS" if verdict.get("passed") else "fail"
    else:
        outcome = (verdict.get("detail") or {}).get("error", status) or status
    score = verdict.get("score")
    return (f"{mark}{verdict.get('task')} arm {verdict.get('arm')} "
            f"repeat {verdict.get('repeat')}: {outcome}"
            + (f" (score {score:.3f})" if isinstance(score, (int, float)) else ""))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="verify.py",
                                 description=__doc__.split("\n")[0])
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--results-dir", default=str(DEFAULT_RESULTS_DIR))
    ap.add_argument("--tasks-file", default=str(runner.DEFAULT_TASKS_FILE))
    ap.add_argument("--family-dir", default=str(runner.DEFAULT_FAMILY_DIR))
    ap.add_argument("--tasks", default=None, help="comma-separated task ids")
    ap.add_argument("--arms", default=None, help="comma-separated: a,b")
    ap.add_argument("--force", action="store_true",
                    help="re-score runs that are already verified")
    args = ap.parse_args(argv)

    try:
        cfg = runner.load_tasks(args.tasks_file, args.family_dir)
        paths = selected(find_verdicts(Path(args.results_dir) / args.run_id),
                         _split(args.tasks), _split(args.arms))
    except (VerifyError, runner.BenchError) as exc:
        print(f"verify.py: {exc}", file=sys.stderr)
        return 1
    if not paths:
        print(f"verify.py: no runs to verify under "
              f"{Path(args.results_dir) / args.run_id}", file=sys.stderr)
        return 1

    by_id = {t["id"]: t for t in cfg.tasks}
    family_dir = Path(args.family_dir)
    errors = 0
    for p in paths:
        try:
            verdict = _load(p)
        except VerifyError as exc:
            print(f"ERR {p}: {exc}")
            errors += 1
            continue
        if verdict.get("status") == "verified" and not args.force:
            print(f"skip {verdict.get('task')} arm {verdict.get('arm')} "
                  f"repeat {verdict.get('repeat')}: already verified "
                  "(--force to re-score)")
            continue
        task = by_id.get(verdict.get("task"))
        if task is None:
            merge_verdict(verdict, {
                "status": "error", "passed": None, "score": None,
                "detail": {"error": f"task {verdict.get('task')!r} is not in "
                                    f"{args.tasks_file} any more; it cannot be "
                                    "scored"}})
            p.write_text(json.dumps(verdict, indent=2))
            print(_line(verdict))
            errors += 1
            continue
        out = verify_one(p, task, family_dir)
        print(_line(out))
        if out.get("status") != "verified":
            errors += 1

    run_id = args.run_id
    print(f"\n{len(paths)} run(s) considered, {errors} error(s). "
          f"Now: python bench/aggregate.py --run-id {run_id}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
