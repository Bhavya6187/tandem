#!/usr/bin/env python3
"""Turn a run's verdict.json files into the results table.

    python bench/aggregate.py --run-id 20260801T191700Z

Reads ONLY verdict.json — the runner and the verifier both write everything a
reader needs into it, so the table can never disagree with the per-run record
it is summarising.

Invalid runs are excluded from every mean and from the pass rate, and counted
separately. That is the whole point of the validity flag: an arm-A run where
nothing was rerouted measured native subagents a second time, and an arm-B run
where something WAS rerouted is contaminated. Averaging them in would quietly
pull the two arms towards each other — which is exactly the effect the bench
exists to measure.

stdlib only.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Mapping

DEFAULT_RESULTS_DIR = Path(__file__).resolve().parent / "work" / "results"


class AggregateError(Exception):
    """The message is user-facing."""


def load_verdicts(run_dir: Path | str) -> list[dict]:
    """Every verdict.json under a run directory, unreadable ones skipped.

    A missing or malformed verdict is a normal mid-experiment state (a run
    still going, a crashed verifier); it must not stop the table from being
    printed for everything else."""
    run_dir = Path(run_dir)
    if not run_dir.is_dir():
        raise AggregateError(f"no such run directory: {run_dir}")
    out = []
    for p in sorted(run_dir.glob("*/*/*/verdict.json")):
        try:
            obj = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _tally(verdicts: list[dict], task: str | None, arm: str) -> dict:
    total = len(verdicts)
    valid = [v for v in verdicts if v.get("validity") == "valid"]
    invalid = [v for v in verdicts if v.get("validity") != "valid"]
    verified = [v for v in valid
                if v.get("status") == "verified" and v.get("passed") is not None]
    passes = sum(1 for v in verified if v.get("passed"))
    row = {
        "task": task, "arm": arm,
        "runs": total, "valid": len(valid), "invalid": len(invalid),
        "verified": len(verified), "unverified": len(valid) - len(verified),
        "passes": passes,
        "pass_rate": (passes / len(verified)) if verified else None,
        "mean_wall_s": _mean([float((v.get("run") or {}).get("wall_clock_s") or 0)
                              for v in valid]),
        "mean_tokens": _mean([float((v.get("metrics") or {}).get("tokens_total") or 0)
                              for v in valid]),
        "mean_dispatches": _mean([float((v.get("metrics") or {}).get("dispatches") or 0)
                                  for v in valid]),
        "mean_reroutes": _mean([float((v.get("metrics") or {}).get("reroutes") or 0)
                                for v in valid]),
        "mean_cost_usd": _mean([float((v.get("metrics") or {}).get("cost_usd") or 0)
                                for v in valid]),
        "timeouts": sum(1 for v in verdicts
                        if (v.get("run") or {}).get("timed_out")),
        "invalid_reasons": dict(Counter(v.get("validity") for v in invalid)),
        # A warning is not an exclusion — a killed run whose token totals fell
        # back to per-message usage is still a measurement — but it IS the
        # reason a mean can be quietly degenerate, so the table has to say the
        # runs exist rather than leaving them to whoever opens verdict.json.
        "warned": sum(1 for v in verdicts if v.get("warnings")),
    }
    return row


def summarize(verdicts: Iterable[Mapping[str, Any]]) -> dict:
    verdicts = [dict(v) for v in verdicts]
    groups: dict[tuple[str, str], list[dict]] = {}
    for v in verdicts:
        groups.setdefault((v.get("task", ""), v.get("arm", "")), []).append(v)
    rows = [_tally(vs, task, arm) for (task, arm), vs in sorted(groups.items())]

    by_arm = []
    for arm in sorted({v.get("arm", "") for v in verdicts}):
        by_arm.append(_tally([v for v in verdicts if v.get("arm") == arm],
                             None, arm))
    return {"rows": rows, "by_arm": by_arm, "runs": len(verdicts)}


def _num(v, digits=1):
    return "-" if v is None else f"{v:.{digits}f}"


def _pct(v):
    return "-" if v is None else f"{v * 100:.0f}%"


HEADER = ("| task | arm | runs | valid | invalid | pass rate | mean wall (s) "
          "| mean tokens | mean dispatches | mean reroutes |")
DIVIDER = "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"


def _line(row, task_label):
    return (f"| {task_label} | {row['arm']} | {row['runs']} | {row['valid']} "
            f"| {row['invalid']} | {_pct(row['pass_rate'])} "
            f"| {_num(row['mean_wall_s'])} | {_num(row['mean_tokens'], 0)} "
            f"| {_num(row['mean_dispatches'], 2)} "
            f"| {_num(row['mean_reroutes'], 2)} |")


def render(summary: Mapping[str, Any]) -> str:
    rows = summary.get("rows") or []
    if not rows:
        return "no verdicts found — has anything been run and verified yet?"
    out = [HEADER, DIVIDER]
    out += [_line(r, r["task"]) for r in rows]
    for r in summary.get("by_arm") or []:
        out.append(_line(r, "**all tasks**"))

    reasons = Counter()
    timeouts = 0
    warned = []
    for r in rows:
        reasons.update(r["invalid_reasons"])
        timeouts += r["timeouts"]
        if r["warned"]:
            warned.append(f"{r['task']}/{r['arm']} × {r['warned']}")
    notes = []
    if reasons:
        notes.append("Excluded as invalid: "
                     + ", ".join(f"{k} × {v}" for k, v in sorted(reasons.items())))
    if warned:
        notes.append("Runs with warnings (read their verdict.json before "
                     "trusting the row): " + ", ".join(warned))
    if timeouts:
        notes.append(f"Timed out (counted, not excluded): {timeouts}")
    unverified = sum(r["unverified"] for r in rows)
    if unverified:
        notes.append(f"Valid but not yet verified: {unverified}")
    if notes:
        out.append("")
        out += [f"- {n}" for n in notes]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="aggregate.py", description=__doc__.split("\n")[0])
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--results-dir", default=str(DEFAULT_RESULTS_DIR))
    ap.add_argument("--json", action="store_true", help="dump the summary as JSON")
    args = ap.parse_args(argv)
    try:
        verdicts = load_verdicts(Path(args.results_dir) / args.run_id)
    except AggregateError as exc:
        print(f"aggregate.py: {exc}", file=sys.stderr)
        return 1
    summary = summarize(verdicts)
    print(json.dumps(summary, indent=2) if args.json else render(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
