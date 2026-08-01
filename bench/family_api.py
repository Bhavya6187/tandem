"""The contract every bench family module must satisfy.

A "family" is one benchmark (swebench, repoqa, lca). Each lives in a single
stdlib-only module under bench/families/<family>.py and exposes exactly two
functions:

    provision(task: Mapping, workdir: str) -> PromptSpec
        Put the task's world on disk under `workdir` (clone at the pinned sha,
        check out the base commit, drop in whatever files the task needs) and
        return the task-specific part of the prompt. `workdir` already exists
        and is the realpath the agent will run in; the provisioner owns
        everything inside it and must not touch anything outside bench/work/.
        It must be idempotent: the runner may re-provision an existing workdir.

    verify(task: Mapping, rundir: str) -> dict
        Score one finished run. `rundir` is the result directory the runner
        wrote (transcript.jsonl, meta.json, extraction.json, verdict.json) and
        `task["_workdir"]` names the tree the agent worked in. Returns the
        fields to merge into verdict.json:

            {"status": "verified"|"error", "passed": bool|None,
             "score": float|None, "detail": {...}}

        Verifiers may shell out (docker, uvx) — they run outside the timed
        window, after the agent is done. The runner itself never calls
        verify(); the Task 3 verifier CLI does.

`task` is the raw [[tasks]] table from bench/tasks.toml as a dict, plus the
`_workdir` key the runner adds. Family modules are loaded by file path, so
they must import only the stdlib and this module.

Returning a plain dict with the same keys as PromptSpec is also accepted — the
runner duck-types it — but PromptSpec is the documented form.
"""

from __future__ import annotations

from typing import Any, NamedTuple


class PromptSpec(NamedTuple):
    """What provision() hands back to the runner.

    `prompt` is only the task itself. The runner wraps it in the shared
    subagent scaffold, which is byte-identical in both arms — that identity is
    what makes the A/B comparison fair, so families must not restate or
    override the scaffold's instructions.

    `workdir` is where claude should actually be launched. Normally that is
    the directory you were handed; return a subdirectory of it instead when
    the provisioner clones into one (`<workdir>/repo`, say). The runner pairs
    and runs in exactly the path returned here, so it must exist.

    `meta` is copied into meta.json for the record (dataset row ids, the
    resolved commit sha, whether a name-leaking needle was swapped, ...).
    """

    prompt: str
    workdir: str
    meta: dict[str, Any] | None = None      # None, not {}: NamedTuple defaults
                                            # are shared between instances
