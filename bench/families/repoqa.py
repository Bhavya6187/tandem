"""RepoQA searching-needle-in-a-haystack family — STUB. Task 3 implements this.

Sketch of what the implementation owes:

provision()
    Download and cache the 2024-06-23 release json.gz; find the row for
    (task["language"], task["repo"]) and the needle named
    task["needle_name"]. Screen the needle's description for the needle's own
    name — 28 of the 600 descriptions leak it — and if it leaks, fall back to
    the task's recorded alternate_* fields and report the swap in
    PromptSpec.meta so tasks.toml can be updated. Clone the repo at the row's
    commit_sha into `workdir`. Prompt = the needle description + "reply with
    the exact function in a fenced code block".

verify()
    Pull the fenced block(s) out of the final assistant message in
    transcript.jsonl, write the scorer jsonl row (language, repo, name,
    output, position_ratio, needle_token_start, needle_token_end — the three
    numeric fields come from the dataset row, not from us), run
    `repoqa.compute_score` in its own environment and map its pass flag.
"""

from __future__ import annotations

from typing import Any, Mapping

from family_api import PromptSpec


def provision(task: Mapping[str, Any], workdir: str) -> PromptSpec:
    raise NotImplementedError(
        f"repoqa provisioner is not implemented yet (task {task.get('id')!r}); "
        "it lands in Task 3 of the A/B bench plan"
    )


def verify(task: Mapping[str, Any], rundir: str) -> dict:
    raise NotImplementedError(
        f"repoqa verifier is not implemented yet (task {task.get('id')!r}); "
        "it lands in Task 3 of the A/B bench plan"
    )
