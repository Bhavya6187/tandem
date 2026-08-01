"""Helpers shared by the three bench family modules.

Lives in bench/ rather than bench/families/ on purpose: `runner.known_families()`
globs bench/families/*.py and treats every hit as a family, so anything that is
not a family must live one directory up. `runner.load_family()` puts bench/ on
sys.path before importing a family, which is what makes `from family_common
import ...` resolve there — the same mechanism families already use for
`family_api`.

stdlib only (urllib, gzip, json, subprocess, pathlib, re). Network and
subprocess work is deliberately confined to a handful of small functions —
`http_get`, `cached_fetch`, `clone_at`, `run_cmd` — so that everything a unit
test cares about (parsing an answer, scoring a set, building a predictions row)
is pure and testable without touching the world.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

BENCH_DIR = Path(__file__).resolve().parent
WORK_DIR = BENCH_DIR / "work"

USER_AGENT = "tandem-bench/1 (+https://github.com/Bhavya6187/tandem)"
HTTP_TIMEOUT_S = 120
HTTP_RETRIES = 3
GIT_TIMEOUT_S = 1800


class BenchFamilyError(Exception):
    """Anything that makes provisioning or verifying impossible.

    The message is user-facing: it lands in verdict.json's detail or in the
    runner's abort message, so it has to say what to do about it."""


# --- paths --------------------------------------------------------------------


def cache_dir() -> Path:
    """Where downloaded datasets and dataset rows live.

    Everything the bench downloads goes under bench/work/ (git-ignored) so a
    clone of this repo never carries somebody else's dataset. BENCH_CACHE_DIR
    overrides it, which is how a test gets a scratch cache."""
    d = Path(os.environ.get("BENCH_CACHE_DIR") or (WORK_DIR / "cache"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def slug(text: str, keep: str = "-_.") -> str:
    """A filesystem- and swebench-run-id-safe rendering of `text`."""
    out = "".join(c if (c.isalnum() or c in keep) else "-" for c in str(text))
    return re.sub(r"-{2,}", "-", out).strip("-") or "x"


# --- the agent's answer -------------------------------------------------------


def read_events(path: Path | str) -> list[dict]:
    """Parse a stream-json transcript, skipping unparseable lines.

    Same tolerance as runner.read_stream: a run killed on the timeout leaves a
    truncated last line, and a partial transcript is still worth reading."""
    out: list[dict] = []
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if isinstance(obj, dict):
                    out.append(obj)
    except OSError:
        return []
    return out


def _assistant_text(event: Mapping[str, Any]) -> str:
    content = (event.get("message") or {}).get("content")
    if not isinstance(content, list):
        return ""
    parts = [b.get("text") or "" for b in content
             if isinstance(b, dict) and b.get("type") == "text"]
    return "\n".join(p for p in parts if p).strip()


def _is_main_agent(event: Mapping[str, Any]) -> bool:
    """False for anything a SUBAGENT said.

    Subagent output is streamed into the same transcript, tagged with
    `parent_tool_use_id`. A worker's report is evidence the main agent used;
    it is not the main agent's answer, and scoring it would credit (or blame)
    the wrong process — in arm A, literally a different model."""
    return not event.get("parent_tool_use_id")


def answer_turns(events: Iterable[Mapping[str, Any]]) -> list[str]:
    """Everything the MAIN agent offered as a finished answer, oldest first.

    One string per `result` event: claude emits one per turn, and with an
    async Agent dispatch there are several — see `final_answer_with_block` for
    what that does to scoring. A run killed before any result event has none,
    so the fallback is the last main-agent assistant message that carried text
    (tool_use and thinking blocks are not an answer)."""
    events = list(events)
    out = [ev["result"].strip() for ev in events
           if ev.get("type") == "result" and _is_main_agent(ev)
           and isinstance(ev.get("result"), str) and ev["result"].strip()]
    if out:
        return out
    for ev in reversed(events):
        if ev.get("type") == "assistant" and _is_main_agent(ev):
            text = _assistant_text(ev)
            if text:
                return [text]
    return []


def final_answer(events: Iterable[Mapping[str, Any]]) -> str:
    """The agent's last word. Families that want a specific answer SHAPE
    should use `final_answer_with_block` instead — see why there."""
    turns = answer_turns(events)
    return turns[-1] if turns else ""


def final_answer_with_block(events: Iterable[Mapping[str, Any]]) -> str:
    """The newest turn that actually contains a fenced block; the newest turn
    of any kind if none does.

    "The last result event is the answer" is wrong whenever subagents run
    ASYNC, and that is not a corner case — it is arm A's normal shape. A
    rerouted `tandem:codex-worker` dispatch returns "Async agent launched
    successfully" immediately, so the main agent answers in full, and THEN
    each worker's completion notification drives another turn whose result is
    a bare acknowledgement ("Task complete. The investigation confirmed my
    finding."). Live, in bench run SMOKE1: arm A's answer — the complete
    `_merge_string_group` function, correctly found, correctly fenced — sat in
    result #1 while results #2 and #3 were summaries, and scoring the last one
    gave BLEU 0.010 against arm B's 1.000 on the same task. That is the A/B
    comparison being decided by transcript shape rather than by routing, which
    is the one failure this bench cannot afford.

    So: the answer is the most recent turn that answered in the requested
    form. Not "the turn with the best block" — the LAST one, so a model that
    corrects itself is still taken at its latest word — and never a
    subagent's block (`answer_turns` filters those out), which would score a
    worker's report as the agent's answer.

    THE RESIDUAL, and it is the same bias in miniature. This only rescues an
    answer from a trailing turn with NO fence: "newest turn with ANY fence" is
    not "newest turn with the answer". A task-notification summary that
    re-quotes one line of the function — or any fenced fragment at all —
    outranks the complete earlier answer and gets scored instead, and
    `answer_had_code_block` is true for the fragment exactly as it is for the
    answer, so nothing downstream separates the re-quoting summary from the
    answer it displaced. lca does not even carry that field, so there a
    displaced answer surfaces only as a low recall. Arm A produces more of
    those trailing turns than arm B (SMOKE1 arm A: 3 result events; SMOKE2
    arm A: 1 — the tail is not even deterministic), so what is left is a
    smaller, same-signed version of the shape-bias this function exists to
    remove. Nothing here detects it; a suspiciously partial arm-A answer is a
    reason to open transcript.jsonl, not a reason to trust the number."""
    turns = answer_turns(events)
    for text in reversed(turns):
        if fenced_blocks(text):
            return text
    return turns[-1] if turns else ""


# --- fenced code blocks -------------------------------------------------------

# The whole info string is allowed and all but its first token discarded.
# ```python title=trans.py and ```js {highlight:[1,2]} are both legal openers
# that models really emit, and a stricter pattern does not merely miss them: the
# opener is skipped, the CLOSING fence is then read as an opener, and the prose
# after the block becomes "the answer". A correct answer scored as a wrong one.
#
# This cannot misfire on a closing fence, because closings are matched by the
# state machine below and never by this pattern. Backticks are excluded from the
# info string (CommonMark forbids them there for backtick fences), which is what
# keeps a prose line containing `inline code` from opening a block.
#
# Indentation is unbounded rather than CommonMark's 3 spaces: a fence nested in
# a numbered list is indented 4, and reading that as "not a fence" produced an
# empty extraction and a false no_code_block.
_OPEN_FENCE = re.compile(r"^(?P<indent>[ \t]*)(?P<fence>`{3,}|~{3,})[ \t]*"
                         r"(?P<info>[^`]*)$")


def _strip_indent(line: str, indent: str) -> str:
    """Remove the opening fence's indentation from one body line.

    A tab is never split. CommonMark expands tabs to four-column stops and
    will happily consume part of one, but doing that here means rewriting a
    tab as spaces — and for a tab-indented language (every Go needle in
    RepoQA) that is the difference between BLEU 1.0 and a fail. Worse, the
    naive "drop up to N leading blanks" version counted a tab as ONE blank, so
    a fence indented two spaces around tab-indented Go ate the code's own
    indentation entirely and flattened the body.

    So: strip the fence's own indent when the line actually starts with it,
    and otherwise strip leading SPACES only, stopping at the first tab."""
    if indent and line.startswith(indent):
        return line[len(indent):]
    n = 0
    while n < len(indent) and n < len(line) and line[n] == " ":
        n += 1
    return line[n:]


def fenced_blocks(text: str) -> list[tuple[str, str]]:
    """Every ```-fenced block in `text`, as (language tag, body).

    Line-based rather than one big regex because the cases that matter are
    structural: a longer opening fence legally contains shorter ones, and a
    model that runs out of tokens mid-answer leaves the last block unterminated
    — dropping that block would silently score a truncated-but-correct answer
    as "no code block".

    The body is de-indented by the opening fence's own indent, so a block
    written inside a list item yields the code as it is in the file rather than
    the code plus four spaces on every line — which for RepoQA is the
    difference between a BLEU of 1.0 and a fail."""
    lines = (text or "").splitlines()
    out: list[tuple[str, str]] = []
    i = 0
    while i < len(lines):
        m = _OPEN_FENCE.match(lines[i])
        if not m:
            i += 1
            continue
        fence, info = m.group("fence"), m.group("info")
        indent = m.group("indent")
        lang = (info.split() or [""])[0]
        char, size = fence[0], len(fence)
        body: list[str] = []
        i += 1
        closed = False
        while i < len(lines):
            stripped = lines[i].strip()
            if stripped and set(stripped) == {char} and len(stripped) >= size:
                closed = True
                i += 1
                break
            body.append(_strip_indent(lines[i], indent))
            i += 1
        out.append((lang, "\n".join(body).strip("\n")))
        if not closed:
            break
    return out


def refence(blocks: Sequence[tuple[str, str]]) -> str:
    """Re-emit extracted blocks as fenced markdown.

    RepoQA's own `sanitize_output` re-parses fences and then runs tree-sitter
    over each block, so handing it bare code would lose that path. Re-fencing
    keeps the scorer on its documented behaviour while dropping the prose the
    agent wrapped around the answer."""
    return "\n\n".join(f"```{lang}\n{body}\n```" for lang, body in blocks)


# --- set metrics --------------------------------------------------------------


def set_scores(predicted: Iterable[str], expected: Iterable[str]) -> dict:
    """Set precision / recall / F1 — the LCA bug-localization metric.

    F1 is computed as 2·tp/(|P|+|E|) rather than from precision and recall.
    They are the same number, but this form is an exact ratio of small integers,
    so the pass rule `f1 >= threshold` lands exactly on the documented edge (one
    of three files found scores exactly 0.5, not 0.49999999999999994)."""
    p = list(dict.fromkeys(str(x) for x in predicted))
    e = list(dict.fromkeys(str(x) for x in expected))
    ps, es = set(p), set(e)
    tp = len(ps & es)
    precision = tp / len(ps) if ps else 0.0
    recall = tp / len(es) if es else 0.0
    f1 = (2 * tp) / (len(ps) + len(es)) if (ps or es) else 0.0
    return {
        "precision": precision, "recall": recall, "f1": f1,
        "true_positives": tp,
        "predicted_count": len(ps), "expected_count": len(es),
        "missing": sorted(es - ps), "spurious": sorted(ps - es),
    }


# --- subprocesses -------------------------------------------------------------


def run_cmd(cmd: Sequence[str], cwd: Path | str | None = None,
            timeout: int = GIT_TIMEOUT_S,
            env: Mapping[str, str] | None = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run([str(c) for c in cmd], cwd=str(cwd) if cwd else None,
                              capture_output=True, text=True, timeout=timeout,
                              env=dict(env) if env else None)
    except subprocess.TimeoutExpired as exc:
        raise BenchFamilyError(
            f"`{' '.join(str(c) for c in cmd)}` timed out after {timeout}s") from exc
    except OSError as exc:
        raise BenchFamilyError(
            f"cannot run `{cmd[0]}`: {exc}. Is it installed and on PATH?") from exc


def git(args: Sequence[str], cwd: Path | str | None = None,
        check: bool = True) -> subprocess.CompletedProcess:
    p = run_cmd(["git", *args], cwd=cwd)
    if check and p.returncode != 0:
        raise BenchFamilyError(
            f"git {' '.join(str(a) for a in args)} failed in {cwd}: "
            f"{(p.stderr or p.stdout).strip()[:500]}")
    return p


def require_workdir(task: Mapping[str, Any]) -> Path:
    """`task["_workdir"]`, or an error — never a relative path.

    `Path("")` is `Path(".")`. A verdict.json with no workdir (an aborted run,
    a hand-edited file, an older schema) therefore used to resolve to the
    verifier's OWN CURRENT DIRECTORY — which, when bench/verify.py is run from
    the repo root, is a git checkout that passes every "is this a clone?" test.
    `git add -A -N` then runs in the user's own repository, staging their
    working tree, from a bench that is not supposed to write anything outside
    bench/work/.

    An unusable workdir is a broken run record, so it is an error verdict, not
    a best effort."""
    raw = task.get("_workdir")
    if not raw or not str(raw).strip():
        raise BenchFamilyError(
            f"run {task.get('id')!r} has no _workdir; there is no tree to grade. "
            "Its verdict.json has no `workdir` — re-run the task rather than "
            "verifying against whatever directory this happens to be.")
    path = Path(str(raw))
    if not path.is_absolute():
        raise BenchFamilyError(
            f"run {task.get('id')!r} has a relative _workdir {str(raw)!r}; the "
            "runner records an absolute realpath, so this record is corrupt.")
    if not path.is_dir():
        raise BenchFamilyError(
            f"the workdir for run {task.get('id')!r} is gone: {path}. "
            "bench/work/.workdirs/ was probably cleaned between the run and "
            "the verify.")
    return path


def worktree_patch(repo: Path | str, since: str = "HEAD") -> str:
    """Everything the agent changed since `since`, as a patch git can apply.

    `add -A -N` records new files as intent-to-add so the diff includes them
    without staging their content; .gitignore still applies, which keeps
    __pycache__ and stray logs out of a SWE-bench prediction.

    The diff is against a COMMIT, not the index, and each of the three ways
    that matters is a way an agent's work would otherwise vanish into an empty
    patch scored as "did nothing":

      - `add -A` records a deletion in the index, so worktree-vs-index no
        longer shows it. A fix that removes a file would be dropped.
      - an agent that stages its edits (`git add`) moves them out of
        worktree-vs-index entirely.
      - an agent that commits — the prompt says not to, which is not the same
        as it not happening — moves them past HEAD too. Pass the pinned base
        commit as `since` and they are still collected.

    Returns "" for an untouched tree: an empty patch is a legitimate failing
    answer, not an error."""
    git(["add", "-A", "-N"], cwd=repo)
    p = git(["diff", "--no-color", since], cwd=repo)
    return p.stdout


# --- cloning ------------------------------------------------------------------


def clone_at(url: str, dest: Path | str, sha: str, blobless: bool = True) -> Path:
    """Put `url` at `sha` in `dest`, whatever state `dest` is in.

    Idempotent by contract (bench/family_api.py): the runner reuses a workdir
    when a run id is retried, so this may be handed a tree the previous agent
    edited, left on a scratch branch, or littered with untracked files. Reset
    covers all three; a directory that is not a usable clone of `url` is thrown
    away and re-cloned rather than repaired.

    `blobless` asks for a `--filter=blob:none` partial clone: full commit and
    tree history (so any pinned sha is reachable and `git diff` works offline
    against the checkout) with file contents fetched on demand. Repos here run
    to hundreds of MB and every task/arm/repeat gets its own copy."""
    dest = Path(dest)
    if not _is_clone_of(dest, url):
        if dest.exists():
            shutil.rmtree(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        cmd = ["clone", "--quiet"]
        if blobless:
            cmd.append("--filter=blob:none")
        cmd += [url, str(dest)]
        git(cmd)
    else:
        git(["reset", "--hard", "--quiet"], cwd=dest, check=False)
        git(["clean", "-qfdx"], cwd=dest, check=False)

    if git(["cat-file", "-e", f"{sha}^{{commit}}"], cwd=dest, check=False).returncode:
        # a sha that is not on any fetched branch (a PR head, a force-pushed
        # tip) is still fetchable by name on GitHub
        git(["fetch", "--quiet", "origin", sha], cwd=dest, check=False)
    if git(["cat-file", "-e", f"{sha}^{{commit}}"], cwd=dest, check=False).returncode:
        raise BenchFamilyError(
            f"commit {sha} is not in {url}. Has the pin gone stale, or was the "
            "commit force-pushed away?")
    git(["checkout", "--quiet", "--force", "--detach", sha], cwd=dest)
    git(["reset", "--hard", "--quiet", sha], cwd=dest)
    git(["clean", "-qfdx"], cwd=dest)
    _hide_the_answer(dest)
    return dest


def _hide_the_answer(repo: Path) -> None:
    """Delete every ref, so nothing in the clone reaches past the pinned sha.

    Detaching at the base commit hides the future from `git status` and from
    nobody else. `git log origin/master`, `git branch -a`, `git log --all
    --grep=<issue number>`, `git show v2.1` — all still walk the fixing commit,
    and for these benchmarks that commit IS the answer: lca's expected_files is
    literally `git show <head_sha> --name-only`, and swebench's gold patch is
    one `git log -p` away. An agent with a Bash tool does not have to be
    devious to find it; `git log` is the first thing you run in an unfamiliar
    repository.

    So: no refs at all. HEAD stays detached at the pinned commit, `git log`
    still shows the history the agent legitimately needs, and `--all` degrades
    to exactly that. Reflogs go too — a fresh clone's HEAD reflog names the
    branch tip it was cloned from.

    What is deliberately KEPT is `remote.origin.url`: a blobless clone fetches
    file contents from the promisor on demand, and clone_at's `git fetch origin
    <sha>` fallback needs it too. The objects for later commits may still sit
    in the pack — this makes them unreachable by name, not unguessable by sha.
    The shas are never shown to the agent (head_sha stays in tasks.toml and is
    not in the prompt or the workdir), which is what makes that the right line
    to draw: a full `git gc --prune=now` on a partial clone is slow and buys
    only defence against a sha nobody has."""
    listed = git(["for-each-ref", "--format=%(refname)"], cwd=repo, check=False)
    for ref in listed.stdout.split():
        git(["update-ref", "-d", ref], cwd=repo, check=False)
    git(["reflog", "expire", "--expire=now", "--all"], cwd=repo, check=False)
    try:
        (repo / ".git" / "FETCH_HEAD").unlink(missing_ok=True)
    except OSError:
        pass


def _is_clone_of(dest: Path, url: str) -> bool:
    if not (dest / ".git").exists():
        return False
    p = git(["remote", "get-url", "origin"], cwd=dest, check=False)
    if p.returncode != 0:
        return False
    return _same_remote(p.stdout.strip(), url)


def _same_remote(a: str, b: str) -> bool:
    def norm(u: str) -> str:
        u = u.strip().rstrip("/")
        return u[:-4] if u.endswith(".git") else u
    return norm(a) == norm(b)


def github_url(repo: str) -> str:
    return f"https://github.com/{repo.strip('/')}.git"


# --- HTTP + cache -------------------------------------------------------------


def http_get(url: str, timeout: int = HTTP_TIMEOUT_S) -> bytes:
    """One GET, retried. The only place in bench/families that opens a socket."""
    last: Exception | None = None
    for attempt in range(HTTP_RETRIES):
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except (urllib.error.URLError, OSError) as exc:      # incl. HTTPError
            last = exc
            if attempt == HTTP_RETRIES - 1:
                break
    raise BenchFamilyError(f"GET {url} failed after {HTTP_RETRIES} tries: {last}")


def cached_fetch(url: str, dest: Path | str,
                 fetcher: Callable[[str], bytes] | None = None) -> Path:
    """`dest`, downloading it from `url` first if it is not already there.

    Written to a sibling .part and renamed, so an interrupted download can
    never be mistaken for a cache hit on the next run — a half-written dataset
    that parses as valid JSON is the kind of thing that produces a wrong
    verdict rather than an error."""
    dest = Path(dest)
    if dest.is_file() and dest.stat().st_size > 0:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    try:
        data = (fetcher or http_get)(url)
        tmp.write_bytes(data)
        tmp.replace(dest)
    except (BenchFamilyError, AssertionError):
        # AssertionError passes through unwrapped: it is what tests/conftest.py's
        # network guard raises, and wrapping it in a BenchFamilyError would let
        # a test that went online land quietly on some caller's offline
        # fallback instead of failing.
        tmp.unlink(missing_ok=True)
        raise
    except Exception as exc:                        # noqa: BLE001 - fetcher is caller's
        tmp.unlink(missing_ok=True)
        raise BenchFamilyError(f"could not fetch {url}: {exc}") from exc
    return dest


def cached_json(url: str, dest: Path | str,
                fetcher: Callable[[str], bytes] | None = None) -> Any:
    path = cached_fetch(url, dest, fetcher=fetcher)
    try:
        return json.loads(path.read_text())
    except ValueError as exc:
        path.unlink(missing_ok=True)                # never keep a poisoned cache
        raise BenchFamilyError(
            f"cached {path} is not valid JSON ({exc}); it has been deleted, "
            "re-run to fetch it again") from exc


# --- verdicts -----------------------------------------------------------------


def verdict(status: str, passed: bool | None, score: float | None,
            **detail: Any) -> dict:
    """The dict shape verify() owes the runner (bench/family_api.py)."""
    return {"status": status, "passed": passed, "score": score, "detail": detail}


def error_verdict(exc: BaseException, **detail: Any) -> dict:
    return verdict("error", None, None, error=f"{type(exc).__name__}: {exc}", **detail)
