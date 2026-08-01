"""LCA bug localization: find the files a GitHub issue's fix must touch.

Dataset: JetBrains-Research/lca-bug-localization, config `py`, split `test`
(50 curated rows). The two rows this bench runs are PINNED in bench/tasks.toml
— hub_row_index, repo, base_sha, head_sha and the ground-truth file list — so
a run reproduces exactly and scoring never depends on the dataset server being
up. Only the issue TEXT is fetched at provision time (and cached under
bench/work/cache/), because pasting a 500-word issue body into a TOML file
would be worse to maintain than one cached GET.

Selection (done once, live, when the pins were written — see the Task 3
report): mid-sized repos (clone well under 200MB), >= 2 changed files, an issue
body self-contained enough to localize from, and — the check that actually
eliminated candidates — `git diff --name-only base...head` on a real clone
equal to the dataset's own `changed_files`.

    NOTE, three dots. changed_files is the PULL REQUEST's file list, i.e. the
    merge-base diff. Two-dot `base..head` also drags in whatever landed on
    master between the two commits: for tweepy that is 13 files instead of 3.
    Rows where the two disagree beyond that — sanic-org/sanic #1327, whose fix
    renames a module, so `--name-only` reports the new name and the dataset
    reports the old one — were rejected rather than papered over.

provision(): clone at base_sha (blobless — the agent reads the tree and nothing
here needs file history). The agent never sees head_sha.
verify(): parse the fenced list out of the final assistant message, score set
F1 against the pinned expected_files, pass at f1 >= f1_threshold. No external
tool and no docker: the metric is twenty lines of stdlib.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Mapping

from family_api import PromptSpec
from family_common import (BenchFamilyError, cache_dir, cached_json, clone_at,
                           error_verdict, fenced_blocks,
                           final_answer_selection,
                           github_url, read_events, set_scores, slug, verdict)

ROWS_URL = ("https://datasets-server.huggingface.co/rows"
            "?dataset={dataset}&config={config}&split={split}"
            "&offset={offset}&length=1")

DEFAULT_CONFIG = "py"
DEFAULT_SPLIT = "test"

INSTRUCTION = """\
Work out which files in this repository must be changed to fix the issue. Do
not change anything: this is a localization task, so read and reason only.

Finish your final message with the exact list of repository-relative file
paths that must change, one per line, inside a fenced code block:

```
path/to/first.py
path/to/second.py
```

List only the files the fix itself has to touch. Nothing but paths goes inside
the block."""


# --- prompt -------------------------------------------------------------------


def build_prompt(row: Mapping[str, Any]) -> str:
    title = (row.get("issue_title") or "").strip()
    body = (row.get("issue_body") or "").strip()
    repo = f"{row.get('repo_owner')}/{row.get('repo_name')}"
    return (f"This checkout is {repo}, at the commit just before the following "
            f"issue was fixed.\n\n=== ISSUE ===\n\n{title}\n\n{body}\n\n"
            f"=== END ISSUE ===\n\n{INSTRUCTION}")


# --- answer parsing -----------------------------------------------------------

_PATH_RE = re.compile(r"^[\w.@+-]+(?:/[\w.@+-]+)*$")
_LEADING_JUNK = re.compile(r"^\s*(?:[-*+•]\s+|\d+[.)]\s+)?")


def parse_file_list(answer: str) -> list[str]:
    """Repo-relative paths from the agent's final message.

    The LAST fenced block wins: a model that shows its working and then states
    its answer puts the answer second, and taking the first block would score
    its scratch notes. With no fence at all the whole message is scanned rather
    than failing — an agent that just listed paths in prose has still answered,
    and the path-shaped line filter is what keeps the prose out."""
    blocks = fenced_blocks(answer or "")
    fenced = bool(blocks)
    text = blocks[-1][1] if fenced else (answer or "")
    out: list[str] = []
    for raw in text.splitlines():
        line = _LEADING_JUNK.sub("", raw).strip()
        line = line.strip("`\"',").strip()
        line = line.lstrip("/")
        if line.startswith("./"):
            line = line[2:]
        if not line or not _PATH_RE.match(line):
            continue
        # Outside a fence, every prose line is a candidate, and a one-word
        # sentence ("Analysis", "Summary") is path-shaped. Requiring a
        # separator or an extension there costs us an extensionless top-level
        # file (Makefile, LICENSE) in the no-fence case only, which is a much
        # rarer answer than a stray word. Inside a fence the agent was told to
        # put nothing but paths, so it is taken at its word.
        if not fenced and "/" not in line and "." not in line:
            continue
        if line not in out:
            out.append(line)
    return out


# --- provision / verify -------------------------------------------------------


def _row_cache_path(task: Mapping[str, Any]) -> Path:
    return (cache_dir() / "lca" /
            f"{slug(task.get('dataset', 'lca'))}"
            f"-{task.get('hub_config', DEFAULT_CONFIG)}"
            f"-{task.get('hub_split', DEFAULT_SPLIT)}"
            f"-{int(task['hub_row_index'])}.json")


def fetch_row(task: Mapping[str, Any]) -> dict:
    """The pinned dataset row, cached. The only network call in this family.

    Cross-checks repo and base_sha against tasks.toml: `offset` is a position,
    not an identity, so a re-ordered dataset would otherwise hand the agent one
    issue and score it against another's files."""
    idx = task.get("hub_row_index")
    if not isinstance(idx, int) or isinstance(idx, bool) or idx < 0:
        raise BenchFamilyError(
            f"task {task.get('id')!r} has no hub_row_index; pin it in "
            "bench/tasks.toml before running the lca family")
    url = ROWS_URL.format(
        dataset=str(task.get("dataset", "")).replace("/", "%2F"),
        config=task.get("hub_config", DEFAULT_CONFIG),
        split=task.get("hub_split", DEFAULT_SPLIT), offset=idx)
    data = cached_json(url, _row_cache_path(task))
    rows = data.get("rows") if isinstance(data, dict) else None
    if not rows:
        raise BenchFamilyError(
            f"the datasets-server returned no row {idx} for {task.get('dataset')} "
            f"({json.dumps(data)[:200]})")
    row = rows[0].get("row") or {}
    got = f"{row.get('repo_owner')}/{row.get('repo_name')}"
    if got != task.get("repo") or row.get("base_sha") != task.get("base_sha"):
        raise BenchFamilyError(
            f"row {idx} of {task.get('dataset')} is now {got}@"
            f"{str(row.get('base_sha'))[:10]}, but bench/tasks.toml pins "
            f"{task.get('repo')}@{str(task.get('base_sha'))[:10]}. The dataset "
            "was re-ordered; re-pin the task before trusting a result.")
    return row


def provision(task: Mapping[str, Any], workdir: str) -> PromptSpec:
    row = fetch_row(task)
    repo_dir = Path(workdir) / "repo"
    clone_at(github_url(str(task["repo"])), repo_dir, str(task["base_sha"]))
    return PromptSpec(
        prompt=build_prompt(row),
        workdir=str(repo_dir),
        meta={
            "family": "lca", "dataset": task.get("dataset"),
            "hub_config": task.get("hub_config", DEFAULT_CONFIG),
            "hub_split": task.get("hub_split", DEFAULT_SPLIT),
            "hub_row_index": task.get("hub_row_index"),
            "repo": task.get("repo"), "base_sha": task.get("base_sha"),
            "head_sha": task.get("head_sha"),
            "issue_url": row.get("issue_url"), "pull_url": row.get("pull_url"),
            "expected_files": list(task.get("expected_files") or []),
            "f1_threshold": task.get("f1_threshold"),
        },
    )


def verify(task: Mapping[str, Any], rundir: str) -> dict:
    try:
        expected = [str(f) for f in (task.get("expected_files") or [])]
        if not expected:
            raise BenchFamilyError(
                f"task {task.get('id')!r} has no expected_files, so there is "
                "nothing to score against. Pin it in bench/tasks.toml.")
        threshold = float(task.get("f1_threshold", 0.5))
        # turn index and count travel with the score: the newest-fence rule can
        # score a trailing summary that re-quotes one path, and in this family
        # that would otherwise surface only as a low recall (README caveat 11)
        answer, turn_index, turns_total = final_answer_selection(
            read_events(Path(rundir) / "transcript.jsonl"))
        predicted = parse_file_list(answer)
        scores = set_scores(predicted, expected)
        detail = dict(scores, expected_files=expected, predicted_files=predicted,
                      threshold=threshold, answer_turn_index=turn_index,
                      answer_turns_total=turns_total)
        if not predicted:
            return verdict("verified", False, 0.0, reason="no_answer", **detail)
        return verdict("verified", scores["f1"] >= threshold, scores["f1"], **detail)
    except Exception as exc:                        # noqa: BLE001 - verdict, not crash
        return error_verdict(exc, task=task.get("id"))
