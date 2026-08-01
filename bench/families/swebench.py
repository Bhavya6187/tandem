"""SWE-bench Verified family — STUB. Task 3 implements this.

Sketch of what the implementation owes, recorded here so the contract is not
re-derived later:

provision()
    Fetch (and cache under bench/work/cache/) the dataset row for
    task["instance_id"] from task["dataset"]; clone task["repo"] into
    `workdir` deep enough that `git diff` works; check out the row's
    `base_commit`. The row's `test_patch` is NOT applied — the evaluation
    harness owns it. The prompt is the row's `problem_statement` plus the
    instruction to fix it in this checkout.

verify()
    `git diff` in the workdir -> a predictions jsonl row
    {instance_id, model_name_or_path, model_patch}; run
    `python -m swebench.harness.run_evaluation --dataset_name <dataset>
    --predictions_path <p> -i <instance_id> --run_id <id>` in an isolated uv
    environment (docker, linux/arm64 images); parse the report json and map
    `resolved` to passed.
"""

from __future__ import annotations

from typing import Any, Mapping

from family_api import PromptSpec


def provision(task: Mapping[str, Any], workdir: str) -> PromptSpec:
    raise NotImplementedError(
        f"swebench provisioner is not implemented yet (task {task.get('id')!r}); "
        "it lands in Task 3 of the A/B bench plan"
    )


def verify(task: Mapping[str, Any], rundir: str) -> dict:
    raise NotImplementedError(
        f"swebench verifier is not implemented yet (task {task.get('id')!r}); "
        "it lands in Task 3 of the A/B bench plan"
    )
