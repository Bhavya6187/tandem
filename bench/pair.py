#!/usr/bin/env python3
"""Create (or verify) a headless tandem pairing for one working directory.

Arm A of the A/B bench needs `tandem hook-route` to answer a Task dispatch
with a REWRITE decision for the task's working directory, under a bench-owned
TANDEM_HOME. That requires exactly two things at hook time
(src/tandem/cli.py::hook_route_cmd):

    1. StateStore().latest_session_for_cwd(cwd) returns a row
    2. codex is installed and its version is inside tandem's compat range

There is no non-interactive `tandem` subcommand that creates the row: bare
`tandem` pairs and then immediately enters the harness on a PTY. So this
helper calls tandem's OWN pairing function, `tandem.cli._pair_session`, in
tandem's own interpreter — the same code path bare `tandem` uses, minus the
TUI. Nothing about the session state is hand-rolled here. See bench/PAIRING.md.

Usage:
    python bench/pair.py --tandem-home <dir> --cwd <dir>
    python bench/pair.py --tandem-home <dir> --cwd <dir> --verify-only
    python bench/pair.py --tandem-home <dir> --cwd <dir> --clean

Idempotent: a second run verifies the existing pairing instead of creating a
second one. Exits 0 only when the pairing is present AND hook-route actually
returns the rewrite decision for that cwd.

Scope of that green light: it proves `tandem hook-route` reroutes when it is
invoked. It proves NOTHING about whether arm A's claude session will invoke
it — that needs the tandem plugin registered in whatever CLAUDE_CONFIG_DIR the
runner launches claude with, which is the runner's job to verify.

stdlib only. Shells out to the `tandem` console script and its interpreter.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TANDEM_HOME = REPO_ROOT / "bench" / "work" / "tandem-home"
DEFAULT_CWD = REPO_ROOT / "bench" / "work" / "proj"

# Probe payload for the hook-route verification. Shaped like a real claude
# PreToolUse Task dispatch. The session_id is deliberately unique-ish and
# bench-owned: hook-route burns a once-per-claude-session "nothing was
# rerouted" stamp under $TANDEM_HOME/warned/<session_id>, and a probe must
# never spend the real run's single notice.
PROBE_SESSION_ID = "bench-pair-probe"
BRIDGE_AGENT = "tandem:codex-worker"


# --- the two snippets we run inside tandem's interpreter ---------------------

# Reads state through tandem's own StateStore/adapters so this helper never
# encodes the schema or the rollout naming rules itself.
INSPECT_SNIPPET = r"""
import json, sys
from tandem import paths
from tandem.doctor import validate_transcript
from tandem.harness import get_adapter
from tandem.state import StateStore

cwd = sys.argv[1]
codex = get_adapter("codex")
cv = codex.detect_version()
claude_v = get_adapter("claude").detect_version()
out = {
    "cwd": cwd,
    "state_db": str(paths.state_db_path()),
    "codex_home": str(paths.codex_home()),
    "codex_version": cv,
    "codex_ok": bool(cv) and codex.version_supported(cv),
    "claude_version": claude_v,
    "paired": False,
}
with StateStore() as store:
    s = store.latest_session_for_cwd(cwd)
if s is not None:
    rollout = (paths.find_codex_rollout(s.codex_session_id)
               if s.codex_session_id else None)
    out.update({
        "paired": True,
        "tandem_id": s.tandem_id,
        "active": s.active,
        "claude_session_id": s.claude_session_id,
        "codex_session_id": s.codex_session_id,
        "codex_rollout": str(rollout) if rollout else None,
        "created_at": s.created_at,
    })
    out["rollout_problems"] = (
        validate_transcript("codex", rollout, s.codex_session_id)
        if rollout else ["codex shadow rollout file not found"]
    )
print("###JSON###" + json.dumps(out))
"""

# _pair_session is tandem's real pairing routine: state row + seeded codex
# shadow rollout + write-ahead cursor (+ memory sync, suppressed by default
# here — see --memory-sync).
#
# `active` is hard-coded to "claude" and NOT exposed as an option: with
# active="codex", _pair_session sets codex_session_id=None and seeds a CLAUDE
# shadow transcript under CLAUDE_CONFIG_DIR (i.e. the user's ~/.claude by
# default). That both violates the bench's "never write to ~/.claude" rule and
# produces a pairing with no codex rollout, which verify() would reject anyway.
PAIR_SNIPPET = r"""
import contextlib, io, json, sys
cwd, do_memory_sync = sys.argv[1], sys.argv[2] == "1"
# The suppression is the one failure mode that could silently corrupt the
# experiment (an AGENTS.md appearing in the task tree makes arm A's tree
# differ from arm B's), so the stub COUNTS its calls: _pair_session imports
# sync_memory_files lazily inside the function body, and if that import ever
# moves to module scope this patch would no-op unnoticed. The caller asserts
# on the count.
calls = {"n": 0}
if not do_memory_sync:
    import tandem.memory_sync as _m

    def _stub(_cwd):
        calls["n"] += 1
        return _m.MemorySyncReport()

    _m.sync_memory_files = _stub
from tandem.cli import _pair_session
from tandem.state import StateStore

buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    with StateStore() as store:
        s = _pair_session(store, cwd, "claude")
print("###JSON###" + json.dumps({
    "tandem_id": s.tandem_id,
    "echo": buf.getvalue(),
    "memory_sync_stub_calls": calls["n"],
}))
"""

# The two files tandem's memory sync can create or rewrite in the task tree.
MEMORY_FILES = ("CLAUDE.md", "AGENTS.md")


class PairError(Exception):
    """Anything that makes the pairing unusable. Message is user-facing."""


def fail(msg: str) -> None:
    raise PairError(msg)


# --- locating the binaries ---------------------------------------------------


def resolve_tandem_bin(explicit: str | None) -> Path:
    if explicit:
        p = Path(os.path.abspath(os.path.expanduser(explicit)))
        if not p.is_file():
            fail(f"--tandem-bin {p} does not exist")
        return p
    venv = REPO_ROOT / ".venv" / "bin" / "tandem"
    if venv.is_file():
        return venv
    found = shutil.which("tandem")
    if not found:
        fail(
            "no tandem binary: expected the worktree venv at "
            f"{venv} (run `uv sync` in {REPO_ROOT}) or `tandem` on PATH"
        )
    return Path(os.path.abspath(found))


def resolve_tandem_python(tandem_bin: Path, explicit: str | None) -> Path:
    """The interpreter that can `import tandem`.

    Console scripts installed by pip/uv are python files whose shebang names
    exactly that interpreter, so the shebang is the authoritative answer.

    Symlinks are deliberately NOT resolved: a venv's bin/python is a symlink
    to the base interpreter, and resolving it lands outside the venv, where
    `import tandem` fails.
    """
    if explicit:
        p = Path(os.path.abspath(os.path.expanduser(explicit)))
        if not p.is_file():
            fail(f"--tandem-python {p} does not exist")
        return p
    try:
        with open(tandem_bin, "rb") as fh:
            first = fh.readline(512).decode("utf-8", "replace").strip()
    except OSError as exc:
        fail(f"cannot read {tandem_bin}: {exc}")
    if first.startswith("#!"):
        parts = first[2:].split()
        # "#!/usr/bin/env python3" as well as a direct interpreter path
        cand = parts[1] if parts and parts[0].endswith("env") and len(parts) > 1 else (
            parts[0] if parts else ""
        )
        if cand and "python" in Path(cand).name:
            found = shutil.which(cand) if not Path(cand).is_absolute() else cand
            if found and Path(found).is_file():
                return Path(os.path.abspath(found))
    sibling = tandem_bin.parent / "python3"
    if sibling.is_file():
        return sibling
    fail(
        f"could not work out which interpreter runs {tandem_bin} "
        "(no python shebang, no sibling python3). Pass --tandem-python."
    )


def check_importable(python: Path, tandem_bin: Path) -> None:
    proc = subprocess.run(
        [str(python), "-c", "import tandem; print(tandem.__version__)"],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        fail(
            f"{python} cannot `import tandem` (derived from {tandem_bin}).\n"
            f"  {proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else ''}\n"
            "  run `uv sync` in the repo, or pass --tandem-python."
        )


# --- subprocess plumbing -----------------------------------------------------


def child_env(tandem_home: Path, codex_home: str | None,
              claude_home: str | None) -> dict[str, str]:
    env = dict(os.environ)
    env["TANDEM_HOME"] = str(tandem_home)
    if codex_home:
        env["CODEX_HOME"] = codex_home
    if claude_home:
        env["CLAUDE_CONFIG_DIR"] = claude_home
    return env


def run_snippet(python: Path, snippet: str, args: list[str], env: dict,
                cwd: Path) -> dict:
    proc = subprocess.run(
        [str(python), "-c", snippet, *args],
        env=env, cwd=str(cwd), capture_output=True, text=True,
    )
    marker = "###JSON###"
    for line in proc.stdout.splitlines():
        if line.startswith(marker):
            return json.loads(line[len(marker):])
    detail = (proc.stderr.strip() or proc.stdout.strip() or "no output")
    fail(f"tandem internals call failed (exit {proc.returncode}):\n{detail}")


def hook_route_probe(tandem_bin: Path, cwd: Path, env: dict) -> dict:
    """Feed hook-route a realistic Task dispatch and return its decision."""
    payload = {
        "session_id": PROBE_SESSION_ID,
        "transcript_path": str(cwd / ".bench-probe.jsonl"),
        "cwd": str(cwd),
        "hook_event_name": "PreToolUse",
        "permission_mode": "default",
        "tool_name": "Task",
        "tool_input": {
            "description": "bench pairing probe",
            "prompt": "Probe dispatch used by bench/pair.py to verify routing.",
            "subagent_type": "general-purpose",
        },
    }
    proc = subprocess.run(
        [str(tandem_bin), "hook-route"], input=json.dumps(payload),
        env=env, cwd=str(cwd), capture_output=True, text=True,
    )
    out = proc.stdout.strip()
    if not out:
        fail(
            "hook-route printed nothing for a Task dispatch in this cwd "
            "(expected either a rewrite decision or a systemMessage notice). "
            f"stderr: {proc.stderr.strip() or '(empty)'}"
        )
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        fail(f"hook-route printed non-JSON: {out[:400]}")


# --- the operations ----------------------------------------------------------


def verify(python: Path, tandem_bin: Path, cwd: Path, env: dict) -> dict:
    info = run_snippet(python, INSPECT_SNIPPET, [str(cwd)], env, cwd)
    if not info["paired"]:
        fail(
            f"no tandem session for {cwd}\n"
            f"  state.db: {info['state_db']}\n"
            "  (cwd is matched as an exact string; it must be the realpath)"
        )
    if not info["codex_ok"]:
        fail(
            "codex is missing or its version is outside tandem's compat range "
            f"(detected: {info['codex_version']!r}). hook-route will refuse to "
            "reroute. Run `tandem doctor`."
        )
    problems = info.get("rollout_problems") or []
    if problems:
        fail(
            "the codex shadow rollout is not resumable:\n  "
            + "\n  ".join(problems)
            + "\n  re-run with --clean and then pair again"
        )
    decision = hook_route_probe(tandem_bin, cwd, env)
    hso = decision.get("hookSpecificOutput") or {}
    updated = hso.get("updatedInput") or {}
    if hso.get("permissionDecision") != "allow" or \
            updated.get("subagent_type") != BRIDGE_AGENT:
        fail(
            "hook-route did NOT return the reroute decision for this cwd.\n"
            f"  it returned: {json.dumps(decision)[:600]}\n"
            "  (a bare systemMessage means no session or no usable codex)"
        )
    info["hook_decision"] = decision
    return info


def memory_file_state(cwd: Path) -> dict[str, str | None]:
    """Fingerprint of the memory files tandem's sync could touch."""
    state: dict[str, str | None] = {}
    for name in MEMORY_FILES:
        p = cwd / name
        try:
            data = p.read_bytes()
        except OSError:
            state[name] = None
        else:
            state[name] = f"{len(data)}:{hash(data)}"
    return state


def pair(python: Path, cwd: Path, memory_sync: bool, env: dict,
         tandem_home: Path) -> str:
    """Create the pairing. Always active='claude' (see PAIR_SNIPPET).

    With memory_sync off, this must leave the task working tree byte-identical.
    That is checked three ways — the stub's own call count, the echoed
    "memory: …" action lines, and the files on disk — and any surprise rolls
    the half-made pairing back before raising, so a failure never leaves a
    state row that would make a later `--verify-only` look healthy.
    """
    before = memory_file_state(cwd)
    res = run_snippet(
        python, PAIR_SNIPPET, [str(cwd), "1" if memory_sync else "0"], env, cwd,
    )
    problem: str | None = None
    if not memory_sync:
        calls = res.get("memory_sync_stub_calls")
        actions = [ln.strip() for ln in (res.get("echo") or "").splitlines()
                   if ln.strip().startswith("memory:")]
        if calls != 1:
            problem = (
                "memory-sync suppression did not take effect "
                f"(stub called {calls} times, expected 1).\n"
                "  tandem.cli._pair_session no longer imports sync_memory_files "
                "lazily, so bench/pair.py can no longer suppress it. Until this "
                "is fixed, pairing may write AGENTS.md into the task tree."
            )
        elif actions:
            problem = ("memory sync ran despite suppression: "
                       + "; ".join(actions))
    after = memory_file_state(cwd)
    if problem is None and not memory_sync and after != before:
        changed = [n for n in MEMORY_FILES if before[n] != after[n]]
        problem = (
            f"pairing modified {', '.join(changed)} in the task working tree; "
            "arm A's tree would no longer match arm B's"
        )
    if problem is not None:
        try:
            clean(python, cwd, env, tandem_home)
        except PairError:
            pass
        fail(problem + "\n  (the half-made pairing was rolled back)")
    return res["tandem_id"]


def clean(python: Path, cwd: Path, env: dict, tandem_home: Path) -> list[str]:
    """Remove pairings for this cwd: the tandem-authored codex rollouts first
    (they live in the user's CODEX_HOME, so they are the part a bench teardown
    would otherwise leak), then the rows in the bench-owned state.db."""
    removed: list[str] = []
    db = tandem_home / "state.db"
    if not db.is_file():
        return removed
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT tandem_id, codex_session_id FROM sessions WHERE cwd = ?",
            (str(cwd),),
        ).fetchall()
    except sqlite3.DatabaseError as exc:
        conn.close()
        fail(f"cannot read {db}: {exc}")
    for row in rows:
        sid = row["codex_session_id"]
        if sid:
            info = run_snippet(
                python,
                "import json,sys\n"
                "from tandem import paths\n"
                "p = paths.find_codex_rollout(sys.argv[1])\n"
                "print('###JSON###' + json.dumps({'path': str(p) if p else None}))",
                [sid], env, cwd,
            )
            path = info["path"]
            if path:
                # Only ever delete a rollout tandem authored for THIS cwd.
                try:
                    with open(path, "rb") as fh:
                        meta = json.loads(fh.readline() or "{}")
                except (OSError, ValueError):
                    meta = {}
                p = meta.get("payload") or {}
                if (meta.get("type") == "session_meta"
                        and p.get("originator") in ("tandem", "tandem-sub")
                        and p.get("cwd") == str(cwd)):
                    Path(path).unlink(missing_ok=True)
                    removed.append(path)
                else:
                    removed.append(f"(kept, not tandem-authored) {path}")
        with conn:
            conn.execute("DELETE FROM sync_cursors WHERE tandem_id = ?",
                         (row["tandem_id"],))
            conn.execute("DELETE FROM sessions WHERE tandem_id = ?",
                         (row["tandem_id"],))
        removed.append(f"state row {row['tandem_id']}")
        subs = tandem_home / "subagents" / row["tandem_id"]
        if subs.is_dir():
            shutil.rmtree(subs, ignore_errors=True)
            removed.append(str(subs))
    conn.close()
    return removed


def probe_sub(tandem_bin: Path, cwd: Path, env: dict) -> None:
    """Real end-to-end smoke test. Costs one small codex model call."""
    proc = subprocess.run(
        [str(tandem_bin), "sub", "-q", "reply with ok"],
        env=env, cwd=str(cwd), capture_output=True, text=True,
    )
    out = (proc.stdout or "").strip()
    if proc.returncode != 0 or not out:
        fail(
            f"`tandem sub` failed (exit {proc.returncode}).\n"
            f"  stdout: {out[:600] or '(empty)'}\n"
            f"  stderr: {(proc.stderr or '').strip()[:600] or '(empty)'}"
        )
    print(f"  sub probe: codex replied {out.splitlines()[-1][:80]!r}")


# --- cli ---------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="pair.py",
        description="Create or verify a headless tandem pairing for one cwd.",
    )
    ap.add_argument("--tandem-home", default=str(DEFAULT_TANDEM_HOME),
                    help="bench-owned TANDEM_HOME (default: %(default)s)")
    ap.add_argument("--cwd", default=str(DEFAULT_CWD),
                    help="working directory to pair (default: %(default)s)")
    ap.add_argument("--codex-home", default=None,
                    help="override CODEX_HOME. Default: inherit, so codex "
                         "finds the user's existing auth; rollouts then land "
                         "in ~/.codex/sessions (this is what tandem does).")
    ap.add_argument("--claude-home", default=None,
                    help="override CLAUDE_CONFIG_DIR for the hook-route probe "
                         "(it reads <claude-home>/agents/ when inlining an "
                         "agent definition). Pairing itself never writes "
                         "there. Default: inherit.")
    ap.add_argument("--tandem-bin", default=None,
                    help="tandem console script (default: <repo>/.venv/bin/"
                         "tandem, else PATH)")
    ap.add_argument("--tandem-python", default=None,
                    help="interpreter that can import tandem (default: read "
                         "from the tandem shebang)")
    ap.add_argument("--memory-sync", action="store_true",
                    help="also run tandem's CLAUDE.md<->AGENTS.md sync. OFF by "
                         "default: it writes into the task working tree, which "
                         "would make arm A's tree differ from arm B's.")
    ap.add_argument("--verify-only", action="store_true",
                    help="fail if the pairing does not already exist")
    ap.add_argument("--clean", action="store_true",
                    help="remove pairings for this cwd (state rows + the "
                         "tandem-authored codex rollouts) and exit")
    ap.add_argument("--probe-sub", action="store_true",
                    help="also run a real `tandem sub` (costs one codex call)")
    ap.add_argument("--json", action="store_true",
                    help="print the result as JSON on stdout")
    ap.add_argument("-q", "--quiet", action="store_true")
    args = ap.parse_args(argv)

    try:
        # realpath: latest_session_for_cwd matches cwd as an exact string, and
        # tandem stores str(Path.cwd()), which is always symlink-resolved
        # (/tmp -> /private/tmp on macOS).
        cwd = Path(os.path.realpath(os.path.expanduser(args.cwd)))
        tandem_home = Path(os.path.realpath(os.path.expanduser(args.tandem_home)))
        if not cwd.is_dir():
            fail(f"--cwd {cwd} is not an existing directory")
        tandem_home.mkdir(parents=True, exist_ok=True)

        tandem_bin = resolve_tandem_bin(args.tandem_bin)
        python = resolve_tandem_python(tandem_bin, args.tandem_python)
        check_importable(python, tandem_bin)
        env = child_env(tandem_home, args.codex_home, args.claude_home)

        if args.clean:
            removed = clean(python, cwd, env, tandem_home)
            if not args.quiet:
                for r in removed:
                    print(f"  removed {r}")
                print(f"cleaned {cwd}" if removed else f"nothing to clean for {cwd}")
            return 0

        info = run_snippet(python, INSPECT_SNIPPET, [str(cwd)], env, cwd)
        created = False
        if not info["paired"]:
            if args.verify_only:
                fail(f"--verify-only: no tandem session for {cwd}")
            if not info["codex_ok"]:
                fail(
                    "refusing to pair: codex is missing or unsupported "
                    f"(detected {info['codex_version']!r}); hook-route would "
                    "never reroute here anyway."
                )
            pair(python, cwd, args.memory_sync, env, tandem_home)
            created = True

        info = verify(python, tandem_bin, cwd, env)
        info["created"] = created
        info["tandem_home"] = str(tandem_home)
        info["tandem_bin"] = str(tandem_bin)

        if args.probe_sub:
            probe_sub(tandem_bin, cwd, env)

        if args.json:
            print(json.dumps(info, indent=2))
        elif not args.quiet:
            print(f"{'paired' if created else 'already paired'} "
                  f"{info['tandem_id']}  ({cwd})")
            print(f"  TANDEM_HOME:   {tandem_home}")
            print(f"  state.db:      {info['state_db']}")
            print(f"  codex shadow:  {info['codex_rollout']}")
            print(f"  codex version: {info['codex_version']}")
            print("  hook-route:    REWRITE -> "
                  f"{info['hook_decision']['hookSpecificOutput']['updatedInput']['subagent_type']}")
        return 0
    except PairError as exc:
        print(f"pair.py: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
