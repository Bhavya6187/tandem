"""LCA bug-localization family — STUB. Task 3 implements this.

This family is doubly unfinished: the module below is a stub, and so are the
two `lca-*` entries in bench/tasks.toml (hub_row_index = -1, empty repo/shas).
The runner refuses to run a task whose pin is still empty and says why, so the
failure mode here is a clear message rather than a stack trace.

Sketch of what the implementation owes:

provision()
    Fetch candidate rows through the HF datasets-server REST API, pick two
    mid-sized ones (clone under ~200MB, >=2 changed files, self-contained
    issue text), PIN them into tasks.toml (hub_row_index, repo, base_sha,
    head_sha, expected_files), clone at base_sha. Prompt = the issue text +
    "list the exact files that must change, one per line, in a fenced block".

verify()
    Parse the fenced list out of the final assistant message, then set
    precision / recall / F1 against task["expected_files"] in plain stdlib
    Python. passed = F1 >= task["f1_threshold"].
"""

from __future__ import annotations

from typing import Any, Mapping

from family_api import PromptSpec


def provision(task: Mapping[str, Any], workdir: str) -> PromptSpec:
    raise NotImplementedError(
        f"lca provisioner is not implemented yet (task {task.get('id')!r}); "
        "it lands in Task 3 of the A/B bench plan"
    )


def verify(task: Mapping[str, Any], rundir: str) -> dict:
    raise NotImplementedError(
        f"lca verifier is not implemented yet (task {task.get('id')!r}); "
        "it lands in Task 3 of the A/B bench plan"
    )
