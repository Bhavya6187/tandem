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


def final_answer(events: Iterable[Mapping[str, Any]]) -> str:
    """The agent's final message — the text every family verifies against.

    The LAST `result` event wins. That is claude's own final answer string, and
    it is the last one for a reason: an async Agent dispatch makes claude emit
    one `result` per turn, and the first of them is only "Agent dispatched.
    Waiting for it to complete." (live-verified in the Task 2 fixtures). A run
    that was killed before any result event still has assistant messages, so
    the fallback is the last assistant message that carried text — tool_use and
    thinking blocks are not an answer."""
    events = list(events)
    for ev in reversed(events):
        if ev.get("type") == "result":
            text = ev.get("result")
            if isinstance(text, str) and text.strip():
                return text.strip()
            break                      # a blank final result: fall through
    for ev in reversed(events):
        if ev.get("type") == "assistant":
            text = _assistant_text(ev)
            if text:
                return text
    return ""


# --- fenced code blocks -------------------------------------------------------

_OPEN_FENCE = re.compile(r"^(?P<indent>\s{0,3})(?P<fence>`{3,}|~{3,})[ \t]*"
                         r"(?P<lang>[^\s`~]*)[ \t]*$")


def fenced_blocks(text: str) -> list[tuple[str, str]]:
    """Every ```-fenced block in `text`, as (language tag, body).

    Line-based rather than one big regex because the cases that matter are
    structural: a longer opening fence legally contains shorter ones, and a
    model that runs out of tokens mid-answer leaves the last block unterminated
    — dropping that block would silently score a truncated-but-correct answer
    as "no code block"."""
    lines = (text or "").splitlines()
    out: list[tuple[str, str]] = []
    i = 0
    while i < len(lines):
        m = _OPEN_FENCE.match(lines[i])
        if not m:
            i += 1
            continue
        fence, lang = m.group("fence"), m.group("lang")
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
            body.append(lines[i])
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


def worktree_patch(repo: Path | str) -> str:
    """Everything the agent changed, as a patch git can apply.

    `add -A -N` records new files as intent-to-add so the diff includes them
    without staging their content; .gitignore still applies, which keeps
    __pycache__ and stray logs out of a SWE-bench prediction. Returns "" for an
    untouched tree — an empty patch is a legitimate (failing) answer, not an
    error."""
    git(["add", "-A", "-N"], cwd=repo)
    p = git(["diff", "--no-color"], cwd=repo)
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
    return dest


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
    except BenchFamilyError:
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
