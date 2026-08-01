"""RepoQA searching-needle-in-a-haystack, adapted to an agent with tools.

Upstream RepoQA pastes a ~16k-token synthetic code context into the prompt and
asks a model to repeat one function from it. This bench gives the agent the
REAL repository at the release's commit_sha instead and asks the same question:
here is a description, find that function and reproduce it exactly. The task is
identical in kind — locate a needle by behavioural description — but it is
solved by searching a checkout, which is what makes it a subagent workload.

Scoring stays upstream's, exactly, so the numbers mean what RepoQA says they
mean: `repoqa.compute_score` reads a jsonl of results, extracts the function
from the model's fenced block with tree-sitter, computes smoothed sentence-BLEU
against every needle in that repo, and passes when the ARGMAX needle is the one
that was asked for AND its BLEU >= 0.8.

External tool, never imported (bench/ is stdlib-only):

    uvx --from repoqa --with tree-sitter==0.21.3 python -m repoqa.compute_score

The tree-sitter pin is not decoration. repoqa 0.1.2 depends on
tree_sitter_languages 1.10.2, which is a C extension built against the
tree-sitter 0.21 API; resolving tree-sitter freely gets 0.26 and every scoring
run dies in `get_parser()` with "TypeError: __init__() takes exactly 1
argument (2 given)". Live-verified 2026-08-01.

Two more things the scorer does that shape the code below:

  - `save_json` PROMPTS on stdin if its output file already exists. A verifier
    that re-scored a run would hang forever, so the stale -SCORES.json is
    deleted before every invocation.
  - the model name in the report is the jsonl's filename stem, so the results
    file is always `bench.jsonl` and the report always `bench-SCORES.json`.

`needle_token_start` / `needle_token_end` are required keys in the results
jsonl but are only echoed into the report — compute_score never scores with
them (verified against repoqa/compute_score.py:272-290). They describe where
the needle sat inside upstream's synthesized token window, which this bench
does not build: the agent gets a real checkout, so there is no such window and
no honest number to put there. They are sent as 0 and the verdict records that
they are placeholders. `position_ratio` IS reproduced exactly — upstream
computes it as (index + 0.5) / len(needles).

Name-leak screen: 28 of the 600 release descriptions contain their own
function's name, which would turn "find it" into "grep for it". This module
screens case-insensitively (33/600, a superset of upstream's 28) and, if the
pinned needle leaks, prefers another clean needle FROM THE SAME REPO AND
LANGUAGE before falling back to the task's recorded alternate — the three
pinned tasks are one python, one typescript and one go, and two of them share
a single rust alternate, so leaning on the alternate first could collapse two
languages into one. All three pins are clean as of the 2024-06-23 release
(live-screened 2026-08-01), so no swap is active.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Any, Mapping

from family_api import PromptSpec
from family_common import (BenchFamilyError, cache_dir, cached_fetch, clone_at,
                           error_verdict, fenced_blocks,
                           final_answer_with_block,
                           github_url, read_events, refence, run_cmd, verdict)

RELEASE_VERSION = "2024-06-23"
RELEASE_URL = ("https://github.com/evalplus/repoqa_release/releases/download/"
               f"{RELEASE_VERSION}/repoqa-{RELEASE_VERSION}.json.gz")

# Pinned so a re-run scores the same way a year from now. See the module
# docstring for why tree-sitter is pinned alongside repoqa itself.
REPOQA_PIN = "repoqa"
TREE_SITTER_PIN = "tree-sitter==0.21.3"
SCORER_CMD = ("uvx", "--from", REPOQA_PIN, "--with", TREE_SITTER_PIN,
              "python", "-m", "repoqa.compute_score")

# upstream's pass bar: the argmax needle must be the right one AND beat this
BLEU_THRESHOLD = 0.8
MODEL_NAME = "bench"
SCORER_TIMEOUT_S = 1800

INSTRUCTION = """\
Somewhere in this repository is one function that does exactly what the
description below says. Find it.

Finish your final message with that function — the complete, exact source as it
appears in this checkout, character for character, with nothing renamed,
reformatted or summarised — inside a single code block wrapped by ```:

```
<the function, exactly as it appears in the repository>
```

Put nothing else in the block: not the surrounding class, not the whole file,
no commentary.

=== FUNCTION DESCRIPTION ==={description}
=== END DESCRIPTION ==="""


# --- the release --------------------------------------------------------------


def release_paths() -> tuple[Path, Path]:
    """(the .json.gz we download, the .json the scorer reads).

    compute_score's --dataset-path wants plain json, and passing it explicitly
    is what keeps the scorer off the network AND on the right release: repoqa's
    own default is REPOQA_DATA_VERSION=2024-04-20, a different set of needles
    from the one these tasks are pinned against."""
    d = cache_dir() / "repoqa"
    return (d / f"repoqa-{RELEASE_VERSION}.json.gz",
            d / f"repoqa-{RELEASE_VERSION}.json")


def load_release() -> dict:
    """The release json, downloading and decompressing it once."""
    gz, plain = release_paths()
    cached_fetch(RELEASE_URL, gz)
    if not (plain.is_file() and plain.stat().st_size > 0):
        tmp = plain.with_name(plain.name + ".part")
        try:
            with gzip.open(gz, "rb") as fh:
                tmp.write_bytes(fh.read())
            tmp.replace(plain)
        except (OSError, EOFError, gzip.BadGzipFile) as exc:
            tmp.unlink(missing_ok=True)
            gz.unlink(missing_ok=True)          # a bad download, not a bad disk
            raise BenchFamilyError(
                f"could not decompress {gz} ({exc}); it has been deleted, "
                "re-run to fetch it again") from exc
    try:
        return json.loads(plain.read_text())
    except ValueError as exc:
        plain.unlink(missing_ok=True)
        raise BenchFamilyError(f"{plain} is not valid JSON ({exc}); deleted, "
                               "re-run to rebuild it") from exc


# --- needle selection ---------------------------------------------------------


def leaks_name(name: str, description: str) -> bool:
    """Does the description give the function's own name away?

    Case-insensitive substring, which flags 33 of the 600 needles where
    upstream's case-sensitive count is 28. Over-screening costs at most a swap
    to an equally valid needle; under-screening silently turns a search task
    into a grep."""
    return bool(name) and name.lower() in (description or "").lower()


def _find_repo(dataset: Mapping[str, Any], language: str, repo: str) -> dict | None:
    for row in dataset.get(language) or []:
        if row.get("repo") == repo:
            return row
    return None


def _needle(row: Mapping[str, Any], name: str) -> tuple[int, dict] | None:
    for i, n in enumerate(row.get("needles") or []):
        if n.get("name") == name:
            return i, n
    return None


def _select(dataset, language, repo, name, swapped, reason) -> dict:
    row = _find_repo(dataset, language, repo)
    if row is None:
        raise BenchFamilyError(
            f"the {RELEASE_VERSION} RepoQA release has no {language} repo "
            f"{repo!r}; re-pin the task in bench/tasks.toml")
    hit = _needle(row, name)
    if hit is None:
        raise BenchFamilyError(
            f"{repo} ({language}) has no needle named {name!r} in the "
            f"{RELEASE_VERSION} release (it has: "
            f"{', '.join(n['name'] for n in row.get('needles') or [])})")
    index, needle = hit
    return {
        "language": language, "repo": repo, "commit_sha": row.get("commit_sha"),
        "index": index, "needle": needle,
        "needle_count": len(row.get("needles") or []),
        # upstream: repoqa/search_needle_function.py — (i + 0.5) / len(needles)
        "position_ratio": (index + 0.5) / max(1, len(row.get("needles") or [])),
        "swapped": swapped, "swap_reason": reason,
    }


def require_pins(task: Mapping[str, Any]) -> None:
    """Fail on an unpinned task BEFORE the 12MB release download.

    runner.task_runnable() already refuses to launch one, but provision() and
    verify() are also reachable directly (bench/verify.py, a retry, a test), and
    "download 12MB, then discover the task is empty" is a bad way to find out."""
    missing = [k for k in ("language", "repo", "needle_name")
               if not str(task.get(k) or "").strip()]
    if missing:
        raise BenchFamilyError(
            f"task {task.get('id')!r} is not pinned: {', '.join(missing)} "
            "empty in bench/tasks.toml")


def select_needle(dataset: Mapping[str, Any], task: Mapping[str, Any]) -> dict:
    """The needle this task actually runs, after the name-leak screen.

    Order matters (see the module docstring): a clean needle from the same
    repo and language first, the task's recorded alternate only if the whole
    repo leaks."""
    language, repo = str(task.get("language")), str(task.get("repo"))
    name = str(task.get("needle_name"))
    sel = _select(dataset, language, repo, name, False, "")
    if not leaks_name(name, sel["needle"].get("description", "")):
        return sel

    row = _find_repo(dataset, language, repo) or {}
    for n in row.get("needles") or []:
        if not leaks_name(n.get("name", ""), n.get("description", "")):
            return _select(dataset, language, repo, n["name"], True,
                           f"pinned needle {name!r} leaks its own name in the "
                           f"description; swapped to {n['name']!r} from the "
                           "same repo and language")
    alt_lang = str(task.get("alternate_language") or language)
    alt_repo = str(task.get("alternate_repo") or "")
    alt_name = str(task.get("alternate_needle_name") or "")
    if not (alt_repo and alt_name):
        raise BenchFamilyError(
            f"every needle in {repo} ({language}) leaks its name and task "
            f"{task.get('id')!r} has no alternate_* pin to fall back to")
    return _select(dataset, alt_lang, alt_repo, alt_name, True,
                   f"every needle in {repo} ({language}) leaks its own name; "
                   f"fell back to the pinned alternate {alt_repo} "
                   f"({alt_lang}) {alt_name!r} — LANGUAGE COVERAGE CHANGED, "
                   "record this in bench/tasks.toml")


# --- prompt -------------------------------------------------------------------


def build_prompt(sel: Mapping[str, Any]) -> str:
    return INSTRUCTION.format(description=sel["needle"].get("description", ""))


# --- scoring ------------------------------------------------------------------


def scorer_row(sel: Mapping[str, Any], answer: str) -> dict:
    """One line of the jsonl compute_score reads.

    The answer is reduced to its fenced blocks and re-fenced, which drops the
    prose without taking the scorer off its own documented path (sanitize_output
    re-parses the fences and runs tree-sitter over each block). An answer with
    no fence at all is passed through verbatim — that is a real, failing answer,
    and rewriting it here would hide it."""
    blocks = fenced_blocks(answer or "")
    output = refence(blocks) if blocks else (answer or "")
    return {
        "language": sel["language"],
        "repo": sel["repo"],
        "name": sel["needle"]["name"],
        "output": [output],
        "position_ratio": sel["position_ratio"],
        # placeholders: required by the reader, never used in scoring — see
        # the module docstring
        "needle_token_start": 0,
        "needle_token_end": 0,
    }


def parse_scores(scores: Mapping[str, Any], needle_name: str) -> dict:
    """pass/fail out of compute_score's <model>-SCORES.json.

    Reads `results`, not `scores`: the pass@1 table is a mean over thresholds,
    while results carries the two facts a single run turns on — which needle
    won the BLEU argmax, and by how much."""
    if not isinstance(scores, Mapping) or not scores:
        raise BenchFamilyError("compute_score wrote an empty report")
    model = MODEL_NAME if MODEL_NAME in scores else next(iter(scores))
    results = (scores.get(model) or {}).get("results") or {}
    rows = [r for per_lang in results.values() for r in per_lang]
    if not rows:
        raise BenchFamilyError(
            f"compute_score scored nothing for model {model!r} — the results "
            "jsonl was empty or its language/repo did not match the dataset")
    row = next((r for r in rows if r.get("name") == needle_name), rows[0])
    score = float(row.get("best_similar_score") or 0.0)
    return {
        "passed": bool(row.get("is_best_similar")) and score >= BLEU_THRESHOLD,
        "score": score,
        "best_target": row.get("best_target"),
        "is_best_similar": bool(row.get("is_best_similar")),
        "bleu_threshold": BLEU_THRESHOLD,
        "needle_position": row.get("needle_position"),
        "model": model,
    }


# --- provision / verify -------------------------------------------------------


def provision(task: Mapping[str, Any], workdir: str) -> PromptSpec:
    require_pins(task)
    sel = select_needle(load_release(), task)
    repo_dir = Path(workdir) / "repo"
    clone_at(github_url(sel["repo"]), repo_dir, str(sel["commit_sha"]))
    return PromptSpec(
        prompt=build_prompt(sel),
        workdir=str(repo_dir),
        meta={
            "family": "repoqa", "release": RELEASE_VERSION,
            "language": sel["language"], "repo": sel["repo"],
            "commit_sha": sel["commit_sha"],
            "needle_name": sel["needle"]["name"],
            "needle_path": sel["needle"].get("path"),
            "needle_lines": [sel["needle"].get("start_line"),
                             sel["needle"].get("end_line")],
            "position_ratio": sel["position_ratio"],
            "needle_swapped": sel["swapped"],
            "swap_reason": sel["swap_reason"],
            "pinned_needle_name": task.get("needle_name"),
        },
    )


def verify(task: Mapping[str, Any], rundir: str) -> dict:
    """Score the run with the real repoqa scorer.

    The needle is re-selected from the cached release rather than read back out
    of meta.json: selection is a pure function of (release, task), so this
    cannot drift from what provision() asked, and a verifier that works on a
    result directory alone is one less thing to keep in sync."""
    eval_dir = Path(rundir) / "repoqa-eval"
    try:
        require_pins(task)
        sel = select_needle(load_release(), task)
        answer = final_answer_with_block(
            read_events(Path(rundir) / "transcript.jsonl"))
        row = scorer_row(sel, answer)
        blocks = fenced_blocks(answer)

        eval_dir.mkdir(parents=True, exist_ok=True)
        results = eval_dir / f"{MODEL_NAME}.jsonl"
        report = eval_dir / f"{MODEL_NAME}-SCORES.json"
        results.write_text(json.dumps(row) + "\n")
        # compute_score's save_json() blocks on stdin when its output already
        # exists; a re-verify would hang forever otherwise
        report.unlink(missing_ok=True)

        _, dataset_json = release_paths()
        cmd = [*SCORER_CMD, "--model-output-path", str(results),
               "--dataset-path", str(dataset_json)]
        p = run_cmd(cmd, cwd=eval_dir, timeout=SCORER_TIMEOUT_S)
        (eval_dir / "compute_score.log").write_text(
            f"$ {' '.join(cmd)}\n\n--- stdout ---\n{p.stdout}\n"
            f"--- stderr ---\n{p.stderr}\n")
        if not report.is_file():
            raise BenchFamilyError(
                f"compute_score wrote no {report.name} (exit {p.returncode}). "
                f"See {eval_dir / 'compute_score.log'}.")
        got = parse_scores(json.loads(report.read_text()), row["name"])
        return verdict(
            "verified", got["passed"], got["score"],
            needle_name=row["name"], language=row["language"], repo=row["repo"],
            best_target=got["best_target"], is_best_similar=got["is_best_similar"],
            bleu=got["score"], bleu_threshold=BLEU_THRESHOLD,
            answer_had_code_block=bool(blocks), answer_chars=len(answer),
            needle_swapped=sel["swapped"], swap_reason=sel["swap_reason"],
            scorer_cmd=cmd, report_path=str(report),
            token_positions_are_placeholders=True,
            reason=None if blocks else "no_code_block",
        )
    except Exception as exc:                        # noqa: BLE001 - verdict, not crash
        return error_verdict(exc, task=task.get("id"), eval_dir=str(eval_dir))
