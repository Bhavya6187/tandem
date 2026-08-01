"""Pure logic of the three family provisioners/verifiers.

Nothing here touches the network, docker, uvx or the real HF datasets-server:
the dataset rows are hand-built dicts, the scorer/report outputs are fixtures
(two of them captured from a real `repoqa.compute_score` run), and the only
subprocess used is `git` against a repo created in tmp_path."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from conftest import BENCH_FIXTURES, load_bench_family, load_bench_shared

common = load_bench_shared("family_common")
lca = load_bench_family("lca")
repoqa = load_bench_family("repoqa")
swebench = load_bench_family("swebench")

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


# --- the agent's answer -------------------------------------------------------


def _events(*objs):
    return list(objs)


def test_final_answer_prefers_the_last_result_event():
    evs = _events(
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "mid"}]}},
        {"type": "result", "subtype": "success", "result": "first turn"},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "last"}]}},
        {"type": "result", "subtype": "success", "result": "the answer"},
    )
    assert common.final_answer(evs) == "the answer"


def test_final_answer_falls_back_to_the_last_assistant_text():
    evs = _events(
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "one"}]}},
        {"type": "assistant", "message": {"content": [
            {"type": "thinking", "thinking": "hmm"},
            {"type": "tool_use", "name": "Read", "input": {}}]}},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "two"}, {"type": "text", "text": "three"}]}},
    )
    assert common.final_answer(evs) == "two\nthree"


def test_final_answer_ignores_a_blank_result_event():
    evs = _events(
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "real"}]}},
        {"type": "result", "subtype": "error_during_execution", "result": ""},
    )
    assert common.final_answer(evs) == "real"


def test_final_answer_of_an_empty_transcript_is_empty():
    assert common.final_answer([]) == ""


def test_final_answer_on_the_live_fixture_transcript():
    evs = [json.loads(ln) for ln in
           (BENCH_FIXTURES / "stream-native.jsonl").read_text().splitlines() if ln.strip()]
    assert common.final_answer(evs) == "**First line of README.md:** Widget Toolkit"


def test_final_answer_fallback_never_returns_a_subagents_message():
    """A subagent's report is not the agent's answer.

    Subagent output is streamed into the same transcript, tagged with
    parent_tool_use_id. A run killed before any result event would otherwise be
    scored on whatever a worker happened to say last."""
    evs = _events(
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "mine"}]}},
        {"type": "assistant", "parent_tool_use_id": "toolu_1",
         "message": {"content": [{"type": "text", "text": "the worker's report"}]}},
    )
    assert common.final_answer(evs) == "mine"


def _async_tail(*texts):
    """Result events shaped like a real async-dispatch tail: the answer, then
    one turn per background agent that finished afterwards."""
    evs = [{"type": "result", "subtype": "success", "result": texts[0]}]
    evs += [{"type": "result", "subtype": "success", "result": t,
             "origin": {"kind": "task-notification"}} for t in texts[1:]]
    return evs


def test_final_answer_with_block_skips_a_post_completion_summary():
    evs = _async_tail("here it is:\n```\ndef f(): pass\n```",
                      "Task complete. The investigation confirmed my finding.",
                      "Both agents have now completed.")
    assert common.final_answer(evs) == "Both agents have now completed."
    assert common.final_answer_with_block(evs) == "here it is:\n```\ndef f(): pass\n```"


def test_final_answer_with_block_takes_the_newest_block_of_several():
    evs = _async_tail("draft:\n```\nwrong\n```", "correction:\n```\nright\n```")
    assert common.final_answer_with_block(evs) == "correction:\n```\nright\n```"


def test_final_answer_with_block_falls_back_when_no_turn_had_one():
    evs = _async_tail("no code here", "still none")
    assert common.final_answer_with_block(evs) == "still none"


def test_final_answer_with_block_never_returns_a_subagents_block():
    evs = [
        {"type": "assistant", "parent_tool_use_id": "toolu_1",
         "message": {"content": [{"type": "text", "text": "```\nworker code\n```"}]}},
        *_async_tail("I dispatched the agents.", "They are done."),
    ]
    assert "worker code" not in common.final_answer_with_block(evs)
    assert common.final_answer_with_block(evs) == "They are done."


def test_final_answer_with_block_on_the_live_async_arm_a_transcript():
    """The bug the smoke found, held down.

    Captured from bench run SMOKE1, arm A (rerouted subagents dispatch
    ASYNC, so each completion adds a turn): the answer — the whole
    `_merge_string_group` function in a fenced block — is in the FIRST result
    event, and the last two results are notification-driven summaries with no
    code in them. Scoring the last one is how a correct answer became a
    0.010."""
    evs = [json.loads(ln) for ln in
           (BENCH_FIXTURES / "stream-async-tail.jsonl").read_text().splitlines()
           if ln.strip()]
    assert "```" not in common.final_answer(evs)          # the trap
    answer = common.final_answer_with_block(evs)
    assert "def _merge_string_group(" in answer
    # ... and it is the MAIN agent's block, not either worker's report
    assert "All relevant matches are in" not in answer
    assert "Found two matching functions" not in answer


# --- fenced blocks ------------------------------------------------------------


def test_fenced_blocks_none():
    assert common.fenced_blocks("just prose, no fences at all") == []


def test_fenced_blocks_one_with_language_tag():
    text = "here it is:\n```python\ndef f():\n    return 1\n```\nthat's it"
    assert common.fenced_blocks(text) == [("python", "def f():\n    return 1")]


def test_fenced_blocks_multiple_keeps_order_and_langs():
    text = "```\na\n```\nprose\n```go\nb\n```\n"
    assert common.fenced_blocks(text) == [("", "a"), ("go", "b")]


def test_fenced_blocks_unterminated_block_runs_to_end_of_text():
    text = "```ts\nfunction sendfile() {}\n"
    assert common.fenced_blocks(text) == [("ts", "function sendfile() {}")]


def test_fenced_blocks_tolerates_longer_and_indented_fences():
    text = "  ````python\n```\nnested\n```\n  ````\n"
    assert common.fenced_blocks(text) == [("python", "```\nnested\n```")]


def test_fenced_blocks_empty_block():
    assert common.fenced_blocks("```\n```") == [("", "")]


def test_fenced_blocks_accepts_a_multi_token_info_string():
    """```python title=x is a legal opener and models emit it.

    Unrecognised, the opener is skipped, the CLOSING fence is read as an opener
    instead, and whatever prose followed the block gets submitted to the scorer
    as the agent's answer — a correct answer recorded as a wrong one."""
    text = "```python title=trans.py\ndef f(): pass\n```\nand that is it"
    assert common.fenced_blocks(text) == [("python", "def f(): pass")]
    text = "```js {highlight: [1,2]}\nlet a = 1\n```"
    assert common.fenced_blocks(text) == [("js", "let a = 1")]


def test_fenced_blocks_accepts_a_deeply_indented_fence():
    """Four spaces or more used to yield [] -> a false "no_code_block"."""
    text = "1. like so:\n\n    ```go\n    func f() {}\n    ```\n"
    assert common.fenced_blocks(text) == [("go", "func f() {}")]


def test_fenced_blocks_dedents_the_body_by_the_fence_indent_only():
    text = "  ```python\n  def f():\n      return 1\n  ```"
    assert common.fenced_blocks(text) == [("python", "def f():\n    return 1")]


def test_fenced_blocks_never_eats_a_tab_that_is_the_codes_own_indent():
    """RepoQA's go needles are tab-indented, and a model that indents the
    fence by a space or two used to have every tab of the function's own
    indentation stripped — a flattened body, scored as a wrong answer."""
    text = "  ```go\n\tfunc f() {\n\t\treturn\n\t}\n  ```"
    assert common.fenced_blocks(text) == [("go", "\tfunc f() {\n\t\treturn\n\t}")]


def test_fenced_blocks_dedents_a_tab_indented_fence_by_its_own_tab():
    text = "\t```go\n\t\tfunc f() {}\n\t```"
    assert common.fenced_blocks(text) == [("go", "\tfunc f() {}")]


def test_refence_round_trips_blocks_the_scorer_can_read_back():
    text = "prose\n```python\ndef f(): pass\n```\nmore prose\n```\nx\n```"
    refenced = common.refence(common.fenced_blocks(text))
    assert "prose" not in refenced
    assert common.fenced_blocks(refenced) == [("python", "def f(): pass"), ("", "x")]


# --- set F1 -------------------------------------------------------------------


def test_set_scores_exact_match():
    s = common.set_scores(["a.py", "b.py"], ["b.py", "a.py"])
    assert (s["precision"], s["recall"], s["f1"]) == (1.0, 1.0, 1.0)
    assert s["true_positives"] == 2


def test_set_scores_empty_prediction_is_zero_not_a_crash():
    s = common.set_scores([], ["a.py"])
    assert (s["precision"], s["recall"], s["f1"]) == (0.0, 0.0, 0.0)


def test_set_scores_empty_expected_is_zero_not_a_crash():
    s = common.set_scores(["a.py"], [])
    assert s["f1"] == 0.0


def test_set_scores_superset_prediction_loses_precision_only():
    s = common.set_scores(["a.py", "b.py", "c.py", "d.py"], ["a.py", "b.py"])
    assert s["recall"] == 1.0
    assert s["precision"] == 0.5
    assert s["f1"] == pytest.approx(2 / 3)


def test_set_scores_deduplicates_the_prediction():
    s = common.set_scores(["a.py", "a.py", "b.py"], ["a.py", "b.py"])
    assert s["f1"] == 1.0
    assert s["predicted_count"] == 2


def test_set_scores_threshold_edge_is_inclusive():
    # 1 correct out of 1 predicted against 3 expected: f1 = 2*1/(1+3) = 0.5 exactly
    s = common.set_scores(["a.py"], ["a.py", "b.py", "c.py"])
    assert s["f1"] == 0.5
    assert s["f1"] >= 0.5          # the lca pass rule must not miss the edge


def test_set_scores_disjoint():
    s = common.set_scores(["x.py"], ["a.py"])
    assert s["f1"] == 0.0


# --- git helpers (local repos only, no network) --------------------------------


def _make_repo(path: Path) -> str:
    path.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@e",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@e")
    run = lambda *a: subprocess.run(["git", "-C", str(path), *a], check=True,
                                    capture_output=True, env=env)
    run("init", "-q", "-b", "main")
    (path / "a.py").write_text("print(1)\n")
    (path / ".gitignore").write_text("*.log\n")
    run("add", "-A")
    run("commit", "-qm", "one")
    base = subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"],
                          capture_output=True, text=True, check=True).stdout.strip()
    (path / "b.py").write_text("print(2)\n")
    run("add", "-A")
    run("commit", "-qm", "two")
    return base


@needs_git
def test_worktree_patch_sees_edits_and_new_files_but_not_ignored_ones(tmp_path):
    repo = tmp_path / "r"
    _make_repo(repo)
    (repo / "a.py").write_text("print(99)\n")
    (repo / "new.py").write_text("print('new')\n")
    (repo / "noise.log").write_text("garbage\n")
    patch = common.worktree_patch(repo)
    assert "print(99)" in patch and "new.py" in patch
    assert "noise.log" not in patch


@needs_git
def test_worktree_patch_includes_deleted_files(tmp_path):
    """`git add -A -N` puts a deletion in the INDEX, so a plain `git diff`
    (worktree vs index) no longer shows it. A fix that removes a file would
    have been silently dropped from the SWE-bench prediction."""
    repo = tmp_path / "r"
    _make_repo(repo)
    (repo / "b.py").unlink()
    patch = common.worktree_patch(repo)
    assert "b.py" in patch
    assert "deleted file" in patch or "-print(2)" in patch


@needs_git
def test_worktree_patch_survives_an_agent_that_staged_or_committed(tmp_path):
    """An agent told not to commit may still stage, or commit anyway.

    Neither is visible to `git diff` alone, and the result would be an empty
    patch scored as "no_patch" — the agent's work reported as no work."""
    repo = tmp_path / "r"
    base = _make_repo(repo)
    (repo / "a.py").write_text("print(42)\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    assert "print(42)" in common.worktree_patch(repo)

    subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@e",
                    "-c", "user.name=t", "commit", "-qm", "oops"], check=True)
    assert common.worktree_patch(repo) == ""              # HEAD moved with it
    assert "print(42)" in common.worktree_patch(repo, since=base)


@needs_git
def test_worktree_patch_of_an_untouched_clone_is_empty(tmp_path):
    repo = tmp_path / "r"
    _make_repo(repo)
    assert common.worktree_patch(repo) == ""


@needs_git
def test_clone_at_is_idempotent_and_resets_a_dirty_workdir(tmp_path):
    origin = tmp_path / "origin"
    base = _make_repo(origin)
    dest = tmp_path / "work" / "repo"
    common.clone_at(origin.as_uri(), dest, base)
    assert (dest / "a.py").exists() and not (dest / "b.py").exists()

    # the agent made a mess; a retry must hand the next run a pristine tree
    (dest / "a.py").write_text("vandalised\n")
    (dest / "junk.py").write_text("junk\n")
    subprocess.run(["git", "-C", str(dest), "checkout", "-qb", "scratch"], check=True)
    common.clone_at(origin.as_uri(), dest, base)
    assert (dest / "a.py").read_text() == "print(1)\n"
    assert not (dest / "junk.py").exists()
    assert common.worktree_patch(dest) == ""


@needs_git
def test_clone_at_leaves_no_ref_that_reaches_the_answer(tmp_path):
    """The provisioned tree must not contain the fix.

    A clone brings the whole default branch, and detaching at base_sha hides
    nothing: `git log origin/master`, `git branch -a`, `git log --all
    --grep=<issue>` all still reach the fixing commit. For lca that commit IS
    the answer (`git show <head_sha> --name-only` is the expected file list);
    for swebench it is the gold patch. One `git log` and the benchmark is
    void."""
    origin = tmp_path / "origin"
    base = _make_repo(origin)
    # the "fix" — everything after base that the agent must not be able to read
    (origin / "fix.py").write_text("the answer\n")
    subprocess.run(["git", "-C", str(origin), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(origin), "-c", "user.email=t@e",
                    "-c", "user.name=t", "commit", "-qm", "fix the bug"],
                   check=True)
    subprocess.run(["git", "-C", str(origin), "tag", "v2"], check=True)
    head = subprocess.run(["git", "-C", str(origin), "rev-parse", "HEAD"],
                          capture_output=True, text=True, check=True).stdout.strip()

    dest = tmp_path / "work" / "repo"
    common.clone_at(origin.as_uri(), dest, base)

    def g(*args):
        return subprocess.run(["git", "-C", str(dest), *args],
                              capture_output=True, text=True).stdout

    assert head not in g("rev-list", "--all")
    assert "fix the bug" not in g("log", "--all", "--oneline")
    assert g("log", "--all", "--grep=fix", "--oneline").strip() == ""
    # only the detached-HEAD pseudo entry; no branch, local or remote, survives
    assert g("branch", "-a").split() == ["*", "(no", "branch)"]
    assert g("for-each-ref").strip() == ""
    assert g("tag").strip() == ""
    assert g("log", "-g", "--oneline").strip() == ""      # nor the reflog
    assert not (dest / "fix.py").exists()
    # the base history the agent legitimately needs is still there
    assert base in g("rev-list", "HEAD")
    assert "one" in g("log", "--oneline")
    # ... and the remote is kept: the blobless clone's promisor fetch and the
    # `git fetch origin <sha>` fallback both need it
    assert g("remote", "get-url", "origin").strip() == origin.as_uri()


@needs_git
def test_clone_at_rejects_a_sha_that_does_not_exist(tmp_path):
    origin = tmp_path / "origin"
    _make_repo(origin)
    with pytest.raises(common.BenchFamilyError):
        common.clone_at(origin.as_uri(), tmp_path / "w", "0" * 40)


# --- the cache ----------------------------------------------------------------


def test_cached_fetch_writes_once_and_reads_back(tmp_path):
    dest = tmp_path / "cache" / "row.json"
    calls = []

    def fake(url):
        calls.append(url)
        return b'{"instance_id": "x"}'

    p = common.cached_fetch("https://example.invalid/x", dest, fetcher=fake)
    assert json.loads(p.read_text())["instance_id"] == "x"
    common.cached_fetch("https://example.invalid/x", dest, fetcher=fake)
    assert calls == ["https://example.invalid/x"]          # second call is a hit


def test_cached_fetch_does_not_leave_a_truncated_file_behind(tmp_path):
    dest = tmp_path / "cache" / "row.json"

    def boom(url):
        raise OSError("connection reset")

    with pytest.raises(common.BenchFamilyError):
        common.cached_fetch("https://example.invalid/x", dest, fetcher=boom)
    assert not dest.exists()
    assert not list(dest.parent.glob("*"))


# --- lca ----------------------------------------------------------------------


def test_lca_parse_file_list_plain():
    text = "The files are:\n```\nsanic/response.py\nsanic/http.py\n```\n"
    assert lca.parse_file_list(text) == ["sanic/response.py", "sanic/http.py"]


def test_lca_parse_file_list_strips_bullets_numbering_backticks_and_dot_slash():
    text = "```\n- `a/b.py`\n2. ./c/d.py\n* /e/f.py\n```"
    assert lca.parse_file_list(text) == ["a/b.py", "c/d.py", "e/f.py"]


def test_lca_parse_file_list_dedupes_and_drops_blank_and_prose_lines():
    text = "```\na.py\n\na.py\nHere are the files:\nb.py\n```"
    assert lca.parse_file_list(text) == ["a.py", "b.py"]


def test_lca_parse_file_list_uses_the_last_fenced_block_when_there_are_several():
    text = "```\nscratch.py\n```\nactually:\n```\nreal.py\n```"
    assert lca.parse_file_list(text) == ["real.py"]


def test_lca_parse_file_list_without_a_fence_falls_back_to_the_whole_message():
    assert lca.parse_file_list("a/b.py\nc/d.py") == ["a/b.py", "c/d.py"]


def test_lca_parse_file_list_without_a_fence_drops_bare_prose_words():
    # "Analysis" is path-shaped; only the fenced form is taken at its word
    assert lca.parse_file_list("Analysis\na/b.py\nDone") == ["a/b.py"]
    assert lca.parse_file_list("```\nAnalysis\na/b.py\n```") == ["Analysis", "a/b.py"]


def test_lca_parse_file_list_of_an_empty_answer():
    assert lca.parse_file_list("") == []


def _rundir(tmp_path, answer, workdir=None):
    rd = tmp_path / "run"
    rd.mkdir(parents=True, exist_ok=True)
    (rd / "transcript.jsonl").write_text(
        json.dumps({"type": "result", "subtype": "success", "result": answer}) + "\n")
    return str(rd)


def test_lca_verify_passes_on_a_perfect_answer(tmp_path):
    task = {"id": "lca-1", "family": "lca", "expected_files": ["a.py", "b.py"],
            "f1_threshold": 0.5, "_workdir": str(tmp_path)}
    v = lca.verify(task, _rundir(tmp_path, "```\na.py\nb.py\n```"))
    assert v["status"] == "verified" and v["passed"] is True
    assert v["score"] == 1.0
    assert v["detail"]["predicted_files"] == ["a.py", "b.py"]


def test_lca_verify_fails_below_the_threshold(tmp_path):
    task = {"id": "lca-1", "family": "lca", "expected_files": ["a.py", "b.py", "c.py"],
            "f1_threshold": 0.5, "_workdir": str(tmp_path)}
    v = lca.verify(task, _rundir(tmp_path, "```\nz.py\n```"))
    assert v["status"] == "verified" and v["passed"] is False
    assert v["score"] == 0.0


def test_lca_verify_records_no_answer_rather_than_erroring(tmp_path):
    task = {"id": "lca-1", "family": "lca", "expected_files": ["a.py"],
            "f1_threshold": 0.5, "_workdir": str(tmp_path)}
    v = lca.verify(task, _rundir(tmp_path, ""))
    assert v["status"] == "verified" and v["passed"] is False
    assert v["detail"]["reason"] == "no_answer"


def test_lca_verify_scores_the_turn_that_carried_the_answer(tmp_path):
    """An async dispatch's trailing summary must not erase the file list."""
    rd = tmp_path / "run"
    rd.mkdir(parents=True, exist_ok=True)
    (rd / "transcript.jsonl").write_text("\n".join(json.dumps(e) for e in _async_tail(
        "The fix has to touch:\n```\na.py\nb.py\n```",
        "Both agents have finished; the analysis above stands.")) + "\n")
    task = {"id": "lca-1", "family": "lca", "expected_files": ["a.py", "b.py"],
            "f1_threshold": 0.5, "_workdir": str(tmp_path)}
    v = lca.verify(task, str(rd))
    assert v["status"] == "verified" and v["passed"] is True
    assert v["detail"]["predicted_files"] == ["a.py", "b.py"]


def test_lca_verify_errors_when_the_pin_is_empty(tmp_path):
    task = {"id": "lca-1", "family": "lca", "expected_files": [],
            "f1_threshold": 0.5, "_workdir": str(tmp_path)}
    v = lca.verify(task, _rundir(tmp_path, "```\na.py\n```"))
    assert v["status"] == "error" and v["passed"] is None


def test_lca_prompt_asks_for_one_path_per_line_in_a_fenced_block():
    row = {"issue_title": "Boom", "issue_body": "it crashes",
           "repo_owner": "o", "repo_name": "n"}
    prompt = lca.build_prompt(row)
    assert "Boom" in prompt and "it crashes" in prompt
    assert "one per line" in prompt and "```" in prompt


# --- repoqa -------------------------------------------------------------------


def _dataset(desc_black="a description with no giveaway",
             desc_other="another description"):
    return {
        "python": [{
            "repo": "psf/black", "commit_sha": "deadbeef" * 5,
            "needles": [
                {"name": "_merge_string_group", "path": "src/black/trans.py",
                 "start_line": 585, "end_line": 639, "description": desc_black},
                {"name": "run_transformer", "path": "src/black/linegen.py",
                 "start_line": 1, "end_line": 9, "description": desc_other},
            ],
        }],
        "rust": [{
            "repo": "seanmonstar/warp", "commit_sha": "cafe" * 10,
            "needles": [{"name": "reject", "path": "src/reject.rs",
                         "start_line": 73, "end_line": 76,
                         "description": "stops a request"}],
        }],
    }


BLACK_TASK = {
    "id": "repoqa-python-black", "family": "repoqa", "language": "python",
    "repo": "psf/black", "needle_name": "_merge_string_group",
    "alternate_language": "rust", "alternate_repo": "seanmonstar/warp",
    "alternate_needle_name": "reject",
}


def test_repoqa_leak_screen_flags_the_name_in_the_description():
    assert repoqa.leaks_name("sendfile", "the sendfile function streams a file")
    assert repoqa.leaks_name("Pool", "returns a pool of workers")     # case-insensitive
    assert not repoqa.leaks_name("_merge_string_group",
                                 "combines adjacent strings in a line")


def test_repoqa_select_needle_keeps_a_clean_pin():
    sel = repoqa.select_needle(_dataset(), BLACK_TASK)
    assert sel["needle"]["name"] == "_merge_string_group"
    assert sel["swapped"] is False
    assert sel["position_ratio"] == 0.25          # (0 + 0.5) / 2 needles


def test_repoqa_select_needle_prefers_a_clean_needle_from_the_same_repo():
    ds = _dataset(desc_black="_merge_string_group merges strings")
    sel = repoqa.select_needle(ds, BLACK_TASK)
    assert sel["needle"]["name"] == "run_transformer"
    assert sel["repo"] == "psf/black" and sel["language"] == "python"
    assert sel["swapped"] is True
    assert "same repo" in sel["swap_reason"]
    assert sel["position_ratio"] == 0.75


def test_repoqa_select_needle_falls_back_to_the_pinned_alternate():
    ds = _dataset(desc_black="_merge_string_group merges strings",
                  desc_other="run_transformer runs it")
    sel = repoqa.select_needle(ds, BLACK_TASK)
    assert (sel["language"], sel["repo"], sel["needle"]["name"]) == \
        ("rust", "seanmonstar/warp", "reject")
    assert sel["swapped"] is True
    assert "alternate" in sel["swap_reason"]


def test_repoqa_select_needle_errors_on_an_unknown_pin():
    with pytest.raises(common.BenchFamilyError):
        repoqa.select_needle(_dataset(), dict(BLACK_TASK, needle_name="nope",
                                              alternate_needle_name="also-nope"))


def test_repoqa_scorer_row_carries_the_fields_compute_score_reads():
    sel = repoqa.select_needle(_dataset(), BLACK_TASK)
    row = repoqa.scorer_row(sel, "sure:\n```python\ndef f(): pass\n```\nhope that helps")
    assert set(row) >= {"language", "repo", "name", "output", "position_ratio",
                        "needle_token_start", "needle_token_end"}
    assert row["name"] == "_merge_string_group"
    assert row["position_ratio"] == 0.25
    assert isinstance(row["output"], list) and len(row["output"]) == 1
    # prose is dropped; the fence survives so the scorer's tree-sitter pass works
    assert "hope that helps" not in row["output"][0]
    assert "```python" in row["output"][0]


def test_repoqa_scorer_row_without_a_fence_sends_the_raw_answer():
    sel = repoqa.select_needle(_dataset(), BLACK_TASK)
    row = repoqa.scorer_row(sel, "def f(): pass")
    assert row["output"] == ["def f(): pass"]


def _scores(name):
    return json.loads((BENCH_FIXTURES / f"repoqa-scores-{name}.json").read_text())


def test_repoqa_parse_scores_pass_from_a_real_compute_score_output():
    got = repoqa.parse_scores(_scores("pass"), "_merge_string_group")
    assert got["passed"] is True
    assert got["score"] == 1.0
    assert got["best_target"] == "_merge_string_group"


def test_repoqa_parse_scores_fails_when_another_needle_wins():
    got = repoqa.parse_scores(_scores("mismatch"), "_merge_string_group")
    assert got["passed"] is False
    assert got["best_target"] == "run_transformer"


def test_repoqa_parse_scores_fails_below_the_bleu_threshold():
    got = repoqa.parse_scores(_scores("lowbleu"), "_merge_string_group")
    assert got["passed"] is False
    assert got["score"] == pytest.approx(0.42)


def test_repoqa_parse_scores_errors_on_an_empty_result_set():
    with pytest.raises(common.BenchFamilyError):
        repoqa.parse_scores({"bench": {"results": {}}}, "_merge_string_group")


def test_repoqa_verify_scores_the_turn_that_carried_the_block(tmp_path, monkeypatch):
    """The whole chain, minus the network and uvx: an async tail must not cost
    the run the answer it already gave."""
    monkeypatch.setattr(repoqa, "load_release", _dataset)
    monkeypatch.setattr(repoqa, "release_paths",
                        lambda: (tmp_path / "r.json.gz", tmp_path / "r.json"))
    seen = {}

    def fake_run_cmd(cmd, cwd=None, timeout=None):
        results = Path(cmd[cmd.index("--model-output-path") + 1])
        seen["row"] = json.loads(results.read_text())
        (Path(cwd) / "bench-SCORES.json").write_text(json.dumps({"bench": {
            "results": {"python": [{"name": "_merge_string_group",
                                    "is_best_similar": True,
                                    "best_similar_score": 1.0,
                                    "best_target": "_merge_string_group"}]}}}))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(repoqa, "run_cmd", fake_run_cmd)
    rd = tmp_path / "run"
    rd.mkdir(parents=True, exist_ok=True)
    (rd / "transcript.jsonl").write_text("\n".join(json.dumps(e) for e in _async_tail(
        "found it:\n```python\ndef _merge_string_group(self): pass\n```",
        "Task complete. The agents confirmed the finding above.")) + "\n")

    v = repoqa.verify(BLACK_TASK, str(rd))
    assert v["status"] == "verified" and v["passed"] is True
    assert v["detail"]["answer_had_code_block"] is True
    assert "def _merge_string_group" in seen["row"]["output"][0]


def test_repoqa_prompt_asks_for_a_fenced_code_block():
    sel = repoqa.select_needle(_dataset(), BLACK_TASK)
    prompt = repoqa.build_prompt(sel)
    assert "a description with no giveaway" in prompt
    assert "```" in prompt
    assert "_merge_string_group" not in prompt        # never name the needle


# --- swebench -----------------------------------------------------------------


def test_swebench_prediction_row_shape():
    row = swebench.prediction_row("django__django-11885", "diff --git a/x b/x\n")
    assert row == {"instance_id": "django__django-11885",
                   "model_name_or_path": "bench",
                   "model_patch": "diff --git a/x b/x\n"}


@needs_git
def test_swebench_prediction_row_from_a_real_worktree_diff(tmp_path):
    repo = tmp_path / "r"
    _make_repo(repo)
    (repo / "a.py").write_text("print(42)\n")
    row = swebench.prediction_row("x__y-1", common.worktree_patch(repo))
    assert row["model_patch"].startswith("diff --git")
    assert "print(42)" in row["model_patch"]
    # a predictions file is one json object per line
    p = tmp_path / "predictions.jsonl"
    swebench.write_predictions(p, [row])
    assert [json.loads(ln) for ln in p.read_text().splitlines()] == [row]


def _report(tmp_path, run_id, instance, body):
    d = (tmp_path / "logs" / "run_evaluation" / run_id / "bench" / instance)
    d.mkdir(parents=True)
    (d / "report.json").write_text(json.dumps(body))
    return tmp_path


def test_swebench_parse_report_resolved(tmp_path):
    inst = "django__django-11885"
    body = {inst: {"patch_is_None": False, "patch_exists": True,
                   "patch_successfully_applied": True, "resolved": True,
                   "tests_status": {"FAIL_TO_PASS": {"success": ["t1"], "failure": []},
                                    "PASS_TO_PASS": {"success": ["t2"], "failure": []}}}}
    _report(tmp_path, "r1", inst, body)
    got = swebench.parse_report(tmp_path, "r1", inst)
    assert got["resolved"] is True
    assert got["fail_to_pass"] == {"success": 1, "failure": 0}
    assert got["report_path"].endswith("report.json")


def test_swebench_parse_report_unresolved(tmp_path):
    inst = "sympy__sympy-16597"
    body = {inst: {"patch_exists": True, "patch_successfully_applied": True,
                   "resolved": False,
                   "tests_status": {"FAIL_TO_PASS": {"success": [], "failure": ["t1"]},
                                    "PASS_TO_PASS": {"success": ["t2"], "failure": []}}}}
    _report(tmp_path, "r1", inst, body)
    got = swebench.parse_report(tmp_path, "r1", inst)
    assert got["resolved"] is False
    assert got["fail_to_pass"] == {"success": 0, "failure": 1}


def test_swebench_parse_report_falls_back_to_the_run_summary(tmp_path):
    inst = "astropy__astropy-13398"
    (tmp_path / "bench.r1.json").write_text(json.dumps({
        "resolved_ids": [inst], "unresolved_ids": [], "error_ids": [],
        "completed_ids": [inst], "empty_patch_ids": []}))
    got = swebench.parse_report(tmp_path, "r1", inst)
    assert got["resolved"] is True
    assert got["source"] == "run_summary"


def test_swebench_parse_report_errors_when_nothing_was_written(tmp_path):
    with pytest.raises(common.BenchFamilyError):
        swebench.parse_report(tmp_path, "r1", "nope__nope-1")


def _swebench_rundir(tmp_path, base_commit):
    """A result dir shaped like the runner's, meta.json included.

    meta.json is where the verifier gets the base commit from, which is what
    keeps scoring off the network: the runner copies PromptSpec.meta there when
    the run is provisioned."""
    rd = tmp_path / "run"
    rd.mkdir()
    (rd / "meta.json").write_text(json.dumps({
        "run_id": "t1", "task": "django__django-11885", "family": "swebench",
        "provision_meta": {"base_commit": base_commit,
                           "instance_id": "django__django-11885"}}))
    return rd


@needs_git
def test_swebench_verify_reports_no_patch_as_a_fail_not_an_error(tmp_path):
    """An untouched checkout AT the base commit: the agent edited nothing.

    Nothing here reaches docker — verify() returns before building a
    predictions file, which is the point: an empty patch is not worth an
    hour of eval."""
    repo = tmp_path / "clone"
    _make_repo(repo)
    head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                          capture_output=True, text=True, check=True).stdout.strip()
    task = {"id": "django__django-11885", "family": "swebench",
            "instance_id": "django__django-11885",
            "dataset": "princeton-nlp/SWE-bench_Verified", "_workdir": str(repo)}
    v = swebench.verify(task, str(_swebench_rundir(tmp_path, head)))
    assert v["status"] == "verified" and v["passed"] is False
    assert v["detail"]["reason"] == "no_patch"
    assert not (tmp_path / "run" / "swebench-eval" / "predictions.jsonl").exists()


@needs_git
def test_swebench_base_commit_comes_from_the_runs_own_meta_json(tmp_path):
    """No network in the normal verify path: the runner already recorded it."""
    repo = tmp_path / "clone"
    base = _make_repo(repo)
    rd = _swebench_rundir(tmp_path, base)
    assert swebench._base_commit({"id": "x", "instance_id": "x"}, str(rd)) == base


@needs_git
def test_swebench_verify_discards_a_previous_attempts_eval(tmp_path):
    """A retried --run-id must not inherit the last attempt's verdict.

    eval_run_id() is derived from the result path, so a retry reuses it — and
    the harness SHORT-CIRCUITS on an existing report.json for that run id,
    returning the old `resolved` and never looking at the new patch. The
    eval directory has to go before anything is written into it."""
    repo = tmp_path / "clone"
    _make_repo(repo)
    head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                          capture_output=True, text=True, check=True).stdout.strip()
    rd = _swebench_rundir(tmp_path, head)
    inst = "django__django-11885"
    stale = _report(rd / "swebench-eval", swebench.eval_run_id(str(rd)), inst,
                    {inst: {"resolved": True, "patch_exists": True,
                            "patch_successfully_applied": True}})
    stale_report = (stale / "logs" / "run_evaluation" /
                    swebench.eval_run_id(str(rd)) / "bench" / inst / "report.json")
    assert stale_report.is_file()

    task = {"id": inst, "family": "swebench", "instance_id": inst,
            "dataset": "princeton-nlp/SWE-bench_Verified", "_workdir": str(repo)}
    v = swebench.verify(task, str(rd))
    assert not stale_report.exists()
    # the new attempt's own answer, not the stale resolved=true
    assert v["status"] == "verified" and v["passed"] is False
    assert v["detail"]["reason"] == "no_patch"


@pytest.mark.parametrize("workdir", ["", None])
def test_swebench_verify_refuses_an_empty_workdir(tmp_path, workdir, monkeypatch):
    """Path("") is Path("."), which passes a `.git` check when the verifier
    runs from a git repo — and `git add -A -N` would then stage the USER'S
    OWN checkout, outside bench/work/ entirely."""
    monkeypatch.chdir(Path(__file__).resolve().parent.parent)   # the tandem repo
    task = {"id": "django__django-11885", "family": "swebench",
            "instance_id": "django__django-11885", "dataset": "d",
            "_workdir": workdir}
    v = swebench.verify(task, str(tmp_path))
    assert v["status"] == "error" and v["passed"] is None
    assert "workdir" in json.dumps(v["detail"]).lower()


@pytest.mark.parametrize("workdir", ["", None, "/nonexistent/nope"])
def test_require_workdir_rejects_anything_that_is_not_a_real_tree(workdir):
    with pytest.raises(common.BenchFamilyError) as exc:
        common.require_workdir({"id": "t", "_workdir": workdir})
    assert "workdir" in str(exc.value).lower()


@needs_git
def test_require_workdir_returns_a_real_tree(tmp_path):
    _make_repo(tmp_path / "r")
    assert common.require_workdir({"_workdir": str(tmp_path / "r")}) == tmp_path / "r"


def test_swebench_base_commit_falls_back_to_head_without_meta_json(tmp_path):
    """The fallback, and — since fetch_row is tried in between — proof that
    the fallback survives having no network to fetch the row with."""
    rd = tmp_path / "run"
    rd.mkdir()
    assert swebench._base_commit({"id": "x"}, str(rd)) == "HEAD"


def test_swebench_eval_run_id_is_a_stable_filesystem_safe_slug():
    a = swebench.eval_run_id("/x/work/results/20260801T1200Z/django__django-1/a/0")
    b = swebench.eval_run_id("/x/work/results/20260801T1200Z/django__django-1/b/0")
    assert a != b
    assert a == swebench.eval_run_id("/x/work/results/20260801T1200Z/django__django-1/a/0")
    assert all(c.isalnum() or c in "-_." for c in a), a


def test_swebench_prompt_carries_the_problem_statement_and_bans_test_edits():
    row = {"problem_statement": "Something is broken in django.",
           "instance_id": "django__django-11885", "repo": "django/django",
           "base_commit": "abc123"}
    prompt = swebench.build_prompt(row)
    assert "Something is broken in django." in prompt
    assert "test" in prompt.lower()


# --- the family contract ------------------------------------------------------


@pytest.mark.parametrize("mod", [lca, repoqa, swebench])
def test_every_family_exposes_the_contract(mod):
    assert callable(mod.provision) and callable(mod.verify)


@pytest.mark.parametrize("mod", [lca, repoqa, swebench])
def test_no_family_imports_tandem(mod):
    src = Path(mod.__file__).read_text()
    assert "import tandem" not in src and "from tandem" not in src
