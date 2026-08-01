#!/usr/bin/env python3
"""A/B bench runner: the same task, twice, with subagent reroute ON and OFF.

    python bench/runner.py check [--tasks ...]
    python bench/runner.py run   --tasks t1,t2 --arms a,b --repeats N --run-id X
    python bench/runner.py smoke

Arm A runs claude with a bench-owned TANDEM_HOME whose `[subagents] route` is
"all", and a tandem pairing for the task's working directory, so the plugin's
PreToolUse hook rewrites every Agent/Task dispatch to `tandem:codex-worker`.
Arm B runs the identical prompt in an identical tree with route = "off", so the
hook returns nothing and subagents stay native. Everything else — model,
prompt, flags, permission mode — is byte-identical between the arms; that
identity is the experiment.

Nothing here imports tandem: the pairing goes through bench/pair.py, and the
routing is observed from claude's own stream-json. Nothing here writes to
~/.tandem or mutates the user's claude config (`claude plugin list` is read
only). All state lives under --work-dir (default bench/work/, git-ignored).

stdlib only.
"""

from __future__ import annotations

import sys

if sys.version_info < (3, 11):      # tomllib, and the type syntax below
    raise SystemExit(
        "bench/runner.py needs Python 3.11+ (got "
        f"{sys.version_info.major}.{sys.version_info.minor}). Use the repo's "
        "venv: .venv/bin/python bench/runner.py ..."
    )

import argparse
import hashlib
import json
import os
import shutil
import signal
import subprocess
import time
import tomllib
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

BENCH_DIR = Path(__file__).resolve().parent
REPO_ROOT = BENCH_DIR.parent
DEFAULT_TASKS_FILE = BENCH_DIR / "tasks.toml"
DEFAULT_WORK_DIR = BENCH_DIR / "work"
DEFAULT_FAMILY_DIR = BENCH_DIR / "families"
DEFAULT_PAIR_SCRIPT = BENCH_DIR / "pair.py"

# The canonical families. `known_families()` reads the family directory, so a
# test can point the runner at a directory of fakes; this tuple is what the
# shipped bench/families/ must contain.
FAMILIES = ("swebench", "repoqa", "lca")

ARMS = ("a", "b")
ARM_ROUTE = {"a": "all", "b": "off"}

# The `[[tasks]]` keys a task of each family needs before it can run at all.
# lca's are empty in the shipped tasks.toml on purpose — Task 3 pins them.
REQUIRED_FIELDS = {
    "swebench": ("instance_id", "repo", "dataset"),
    "repoqa": ("language", "repo", "needle_name"),
    "lca": ("hub_row_index", "repo", "base_sha", "head_sha", "expected_files"),
}
LCA_SCHEMA = ("hub_row_index", "repo", "base_sha", "head_sha",
              "expected_files", "f1_threshold")

# Where the provisioners clone. The leading dot is load-bearing, not style:
# bench/work/ sits under the repo root and fills up with checkouts of other
# people's repositories, tests and conftest.py files included, so a plain
# `uv run pytest` walks straight into them. Live, after the first smoke run,
# two checkouts of psf/black gave pytest two files both claiming to be
# `tests.conftest` and collection died before a single tandem test ran.
# pytest's default norecursedirs skips `.*`, which fixes it with no config
# change (bench/ must not touch pyproject.toml) and no extra conftest.py —
# and an extra conftest.py is itself a trap here, because tests/ has no
# __init__.py and a second one would win the module name `conftest`.
WORKDIRS_DIR = ".workdirs"

DEFAULT_TIMEOUT_S = 900
# claude flags the extraction depends on; not configurable from tasks.toml.
STREAM_FLAGS = ("--output-format", "stream-json", "--verbose",
                "--include-hook-events")
# `tandem hook-route` rewrites a dispatch to this agent. Claude scopes plugin
# agents as `<plugin>:<agent>`, and the hook itself compares on the bare name,
# so we do too.
BRIDGE_NAME = "codex-worker"

SCHEMA_VERSION = 1


class BenchError(Exception):
    """Anything that makes a run impossible. The message is user-facing."""


# --- the prompt scaffold -----------------------------------------------------

# Wrapped around every family's task text, identically in both arms.
#
# "Investigate only" is not politeness: rerouted subagents are codex workers
# running under codex's own config, which in practice means `sandbox:
# read-only` — they physically cannot edit files (bench/PAIRING.md caveat 7).
# Native Explore subagents are read-only too, so restricting both arms the same
# way is the fair comparison rather than a handicap. If a family ever needs
# writing subagents, the arms stop being comparable and this scaffold has to be
# revisited, not quietly relaxed.
SCAFFOLD = """\
How to work on this (mandatory, and it applies to the whole task):

1. Investigate before you act. Dispatch subagents with the Agent tool — at
   least two, in parallel, before your first edit or your final answer — and
   give each a specific question about this repository.
2. Subagents INVESTIGATE ONLY. They may read, search and analyse files. They
   must never create, edit, move or delete a file, never run a command that
   changes the working tree, and never run the project's build or tests. Say
   this explicitly in every brief you send them.
3. You make every change yourself, in this session, using what they report
   back. Do not ask a subagent to apply a patch for you.
4. You are running non-interactively: there is nobody to answer questions.
   Finish the task, state the answer in your final message, and stop.

=== TASK ===

{task}
"""


def build_prompt(task_prompt: str) -> str:
    return SCAFFOLD.format(task=task_prompt.strip())


# --- config ------------------------------------------------------------------


@dataclass
class BenchConfig:
    tasks: list[dict] = field(default_factory=list)
    run: dict = field(default_factory=dict)
    path: Path | None = None


def known_families(family_dir: Path) -> list[str]:
    """The families that actually have a provisioner module on disk."""
    try:
        return sorted(p.stem for p in Path(family_dir).glob("*.py")
                      if not p.stem.startswith("_"))
    except OSError:
        return []


def load_tasks(path: Path | str, family_dir: Path | str = DEFAULT_FAMILY_DIR
               ) -> BenchConfig:
    path = Path(path)
    try:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    except OSError as exc:
        raise BenchError(f"cannot read {path}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise BenchError(f"{path} is not valid TOML: {exc}") from exc

    allowed = set(known_families(family_dir)) or set(FAMILIES)
    tasks = data.get("tasks") or []
    if not isinstance(tasks, list):
        raise BenchError(f"{path}: [[tasks]] must be an array of tables")
    seen: set[str] = set()
    for t in tasks:
        if not isinstance(t, dict) or not t.get("id"):
            raise BenchError(f"{path}: every [[tasks]] entry needs an id")
        tid = t["id"]
        if tid in seen:
            raise BenchError(f"{path}: duplicate task id {tid!r}")
        seen.add(tid)
        if "/" in tid or ".." in tid or tid != tid.strip():
            raise BenchError(
                f"{path}: task id {tid!r} is used as a directory name; it must "
                "not contain '/' or '..' or leading/trailing space"
            )
        fam = t.get("family")
        if fam not in allowed:
            raise BenchError(
                f"{path}: task {tid!r} has family {fam!r}, which has no "
                f"provisioner in {family_dir} (known: {', '.join(sorted(allowed))})"
            )

    run = dict(data.get("run") or {})
    run.setdefault("repeats", 1)
    run.setdefault("model", "")
    run.setdefault("arms", list(ARMS))
    run.setdefault("claude_flags", [])
    run.setdefault("timeout_s", {})
    run.setdefault("smoke_task", tasks[0]["id"] if tasks else "")
    return BenchConfig(tasks=tasks, run=run, path=path)


def select_tasks(cfg: BenchConfig, ids: Iterable[str] | None) -> list[dict]:
    """Tasks in the order the caller asked for them; all of them for None."""
    if ids is None:
        return list(cfg.tasks)
    by_id = {t["id"]: t for t in cfg.tasks}
    out = []
    for i in ids:
        if i not in by_id:
            raise BenchError(
                f"unknown task {i!r}. Known: {', '.join(sorted(by_id))}"
            )
        out.append(by_id[i])
    return out


def timeout_for(cfg: BenchConfig, family: str) -> int:
    t = (cfg.run.get("timeout_s") or {}).get(family)
    return int(t) if t else DEFAULT_TIMEOUT_S


def task_runnable(task: Mapping[str, Any]) -> tuple[bool, str]:
    """(True, "") or (False, why). A pin that Task 3 has not filled in yet is
    the expected 'no' — it must read as a to-do, not as a crash."""
    missing = [k for k in REQUIRED_FIELDS.get(task.get("family", ""), ())
               if _is_blank(task.get(k))]
    if not missing:
        return True, ""
    return False, (
        f"task {task.get('id')!r} ({task.get('family')}) is not pinned yet: "
        f"{', '.join(missing)} still empty. Task 3's {task.get('family')} "
        "provisioner fills these into bench/tasks.toml."
    )


def _is_blank(v: Any) -> bool:
    if v is None or v == "" or v == [] or v == {}:
        return True
    # hub_row_index = -1 is the shipped 'unset' marker for lca
    return isinstance(v, int) and not isinstance(v, bool) and v < 0


def load_family(name: str, family_dir: Path | str):
    """Import bench/families/<name>.py by path.

    Family modules are loaded by path rather than as a package so that bench/
    stays a plain directory of scripts. BENCH_DIR goes on sys.path first so
    they can `from family_api import PromptSpec`."""
    import importlib.util

    p = Path(family_dir) / f"{name}.py"
    if not p.is_file():
        raise BenchError(f"no provisioner module for family {name!r} at {p}")
    if str(BENCH_DIR) not in sys.path:
        sys.path.insert(0, str(BENCH_DIR))
    alias = f"bench_family_{name}"
    # cached on content, not just on name: the runner loads a family once per
    # run, and a module cached by name alone would go stale the moment anyone
    # edits a provisioner in a long-lived process
    try:
        st = p.stat()
        stamp = (st.st_mtime_ns, st.st_size)
    except OSError:
        stamp = (0, 0)
    hit = _FAMILY_CACHE.get(str(p))
    if hit and hit[0] == stamp:
        return hit[1]
    spec = importlib.util.spec_from_file_location(alias, p)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[alias] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception as exc:                       # noqa: BLE001 - user code
        sys.modules.pop(alias, None)
        raise BenchError(f"family module {p} failed to import: {exc}") from exc
    for fn in ("provision", "verify"):
        if not callable(getattr(mod, fn, None)):
            raise BenchError(f"family module {p} has no {fn}() — see "
                             "bench/family_api.py for the contract")
    _FAMILY_CACHE[str(p)] = (stamp, mod)
    return mod


_FAMILY_CACHE: dict[str, tuple[tuple[int, int], Any]] = {}


# --- stream-json extraction --------------------------------------------------
#
# Which signal says "this dispatch was rerouted"? Three candidates show up in a
# real transcript, and they do NOT agree:
#
#   assistant / tool_use(Agent).input.subagent_type
#       the type the MODEL asked for. Live-verified 2026-08-01: on a rerouted
#       dispatch this still reads "Explore" — the hook's updatedInput is
#       applied after the assistant message has already been streamed. Counting
#       reroutes here would report zero every time.
#   system / hook_response (needs --include-hook-events)
#       tandem's raw hook stdout: the rewrite decision, or the bare
#       {"systemMessage": ...} notice. Proves the hook FIRED and what it said,
#       but carries no tool_use_id, so it cannot be attributed to a dispatch.
#   system / task_started, task_type == "local_agent"      <-- AUTHORITATIVE
#       the type claude actually SPAWNED, after hooks: "tandem:codex-worker"
#       on a rerouted dispatch, "Explore" on a native one. Carries the
#       tool_use_id, so dispatches and reroutes are counted from one place.
#
# So: task_started decides, hook_response corroborates (and is the only place
# a notice is visible at all — a decision-free systemMessage produces no
# user-facing stream event), and the TANDEM_HOME side effects recorded
# alongside are the third, out-of-band witness.


def read_stream(path: Path | str) -> list[dict]:
    """Parse a stream-json transcript, skipping anything unparseable.

    A truncated last line is normal when a run is killed on timeout, and a
    partial transcript is still worth extracting from."""
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


def _bare_agent(name: str) -> str:
    return (name or "").rsplit(":", 1)[-1]


def _hook_payload(event: dict) -> dict | None:
    raw = event.get("stdout") or event.get("output")
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        obj = json.loads(raw)
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def extract_transcript(events: Iterable[dict]) -> dict:
    events = list(events)
    requested: dict[str, str] = {}
    effective: dict[str, str] = {}
    notices = 0
    hook_reroutes = 0
    session_id = ""
    claude_version = ""
    model = ""
    results: list[dict] = []

    for ev in events:
        if not session_id and isinstance(ev.get("session_id"), str):
            session_id = ev["session_id"]
        etype, subtype = ev.get("type"), ev.get("subtype")

        if etype == "system" and subtype == "init":
            claude_version = ev.get("claude_code_version") or claude_version
            model = ev.get("model") or model

        elif etype == "system" and subtype == "task_started":
            # local_bash task_started events exist too (the bridge agent runs
            # `tandem sub` as a background Bash task); only local_agent is a
            # subagent dispatch.
            if ev.get("task_type") == "local_agent" and ev.get("tool_use_id"):
                effective[ev["tool_use_id"]] = ev.get("subagent_type") or ""

        elif etype == "system" and subtype == "hook_response":
            if str(ev.get("hook_event") or ev.get("hook_name") or "") \
                    .startswith("PreToolUse"):
                payload = _hook_payload(ev) or {}
                hso = payload.get("hookSpecificOutput") or {}
                updated = hso.get("updatedInput") or {}
                if _bare_agent(updated.get("subagent_type", "")) == BRIDGE_NAME:
                    hook_reroutes += 1
                msg = payload.get("systemMessage")
                # other plugins may also emit a PreToolUse systemMessage; only
                # tandem's counts as "the reroute hook declined"
                if isinstance(msg, str) and "tandem" in msg.lower():
                    notices += 1

        elif etype == "assistant":
            content = (ev.get("message") or {}).get("content")
            for block in content if isinstance(content, list) else []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use" and \
                        block.get("name") in ("Agent", "Task"):
                    tid = block.get("id") or ""
                    inp = block.get("input") or {}
                    if tid:
                        requested[tid] = inp.get("subagent_type") or ""

        elif etype == "result":
            results.append(ev)

    ids = list(dict.fromkeys([*requested, *effective]))
    resolved = {i: effective.get(i, requested.get(i, "")) for i in ids}
    reroutes = sum(1 for v in resolved.values() if _bare_agent(v) == BRIDGE_NAME)
    last = results[-1] if results else {}

    return {
        "schema": SCHEMA_VERSION,
        "session_id": session_id,
        "claude_version": claude_version,
        "model": model,
        "dispatches": len(ids),
        "reroutes": reroutes,
        "native_dispatches": len(ids) - reroutes,
        "notices": notices,
        "hook_reroute_decisions": hook_reroutes,
        "agent_types": dict(Counter(resolved.values())),
        "requested_agent_types": dict(Counter(requested.values())),
        "tokens": _tokens(last, events),
        "cost_usd": float(last.get("total_cost_usd") or 0.0),
        "duration_ms": int(last.get("duration_ms") or 0),
        # SUMMED, unlike cost and modelUsage on the same event: claude reports
        # num_turns per result event, and an async dispatch emits one result
        # per turn. Live in SMOKE1 arm A that was 11, 1, 1 — reading the last
        # one filed a 13-turn session as a 1-turn one.
        "num_turns": sum(int(r.get("num_turns") or 0) for r in results),
        "result_events": len(results),
        "result_subtype": last.get("subtype") or "",
        "result_is_error": bool(last.get("is_error")),
    }


def _tokens(result: Mapping[str, Any], events: list[dict]) -> dict:
    """Token totals, from the best source this transcript has.

    1. The LAST result event's `modelUsage` — an async Agent dispatch makes
       claude emit one `result` per turn, and only the last one's modelUsage is
       the whole session, subagents included.
    2. That result's per-turn `usage`.
    3. No result event at all: sum `message.usage` over the assistant events.
       A run killed on the timeout never reaches a result, and reporting 0
       tokens for it would fold a free run into the mean for whichever arm
       times out — flattering exactly the arm that failed."""
    tot = {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0}

    def add_snake(u: Mapping[str, Any]) -> None:
        tot["input"] += int(u.get("input_tokens") or 0)
        tot["output"] += int(u.get("output_tokens") or 0)
        tot["cache_read"] += int(u.get("cache_read_input_tokens") or 0)
        tot["cache_creation"] += int(u.get("cache_creation_input_tokens") or 0)

    mu = result.get("modelUsage")
    usage = result.get("usage")
    if isinstance(mu, dict) and mu:
        for per in mu.values():
            if not isinstance(per, dict):
                continue
            tot["input"] += int(per.get("inputTokens") or 0)
            tot["output"] += int(per.get("outputTokens") or 0)
            tot["cache_read"] += int(per.get("cacheReadInputTokens") or 0)
            tot["cache_creation"] += int(per.get("cacheCreationInputTokens") or 0)
    elif isinstance(usage, dict):
        add_snake(usage)
    else:
        for ev in events:
            if ev.get("type") != "assistant":
                continue
            u = (ev.get("message") or {}).get("usage")
            if isinstance(u, dict):
                add_snake(u)
    tot["total"] = sum(tot.values())
    return tot


# --- validity ----------------------------------------------------------------


def hook_declined(ex: Mapping[str, Any]) -> bool:
    """Did tandem's hook fire and decline to reroute?

    Two independent witnesses, either sufficient: the notice in the stream
    (which needs --include-hook-events, since a decision-free systemMessage
    produces no user-facing stream event at all) and the stamp tandem writes at
    $TANDEM_HOME/warned/<claude session id> when it prints one. The stamp is
    the one that survives a missing flag."""
    return bool(ex.get("notices")) or \
        bool((ex.get("side_effects") or {}).get("warn_stamp"))


def native_dispatches(ex: Mapping[str, Any]) -> int:
    """Dispatches that ran on claude. Derived when the field is absent so that
    an extraction.json written by an older runner still classifies."""
    if ex.get("native_dispatches") is not None:
        return int(ex["native_dispatches"])
    return max(0, int(ex.get("dispatches") or 0) - int(ex.get("reroutes") or 0))


def validity_for(arm: str, ex: Mapping[str, Any]) -> str:
    """Was this run a measurement of what we think it measured?

    Arm A exists to measure rerouted subagents; if nothing rerouted it measured
    arm B a second time. Two ways that goes wrong, and only one of them is
    noisy:

    - the hook could not reroute at all (no session, no usable codex). It says
      so, once, via the notice — `hook_declined`.
    - the hook fired, was healthy, and still returned None for SOME dispatches:
      `route()` passes through `fork` subagents, dispatches already aimed at
      the bridge, and blank prompts, and `missed_reroute_notice()` is silent
      for exactly those (has_session and codex_ok short-circuit it). Nothing
      announces it. The scaffold asks for >=2 parallel dispatches, so a run
      with one rerouted and one native Agent is entirely reachable — and
      averaging it in would blend the two arms, which is the one error this
      bench cannot afford.

    Hence a distinct flag rather than a warning: the aggregator excludes it.
    Arm B is native by definition, so unrerouted dispatches are the point
    there, not a defect."""
    reroutes = int(ex.get("reroutes") or 0)
    if arm == "a":
        if reroutes == 0 or hook_declined(ex):
            return "invalid_no_reroute"
        return "invalid_partial_reroute" if native_dispatches(ex) else "valid"
    if arm == "b":
        return "invalid_leak" if reroutes > 0 else "valid"
    return "valid"


def mark_validity(arm: str, ex: dict) -> dict:
    ex["arm"] = arm
    ex["validity"] = validity_for(arm, ex)
    warnings: list[str] = []
    if not ex.get("dispatches"):
        warnings.append("no subagent dispatches in the transcript — the "
                        "scaffold did not take")
    if arm == "a" and hook_declined(ex):
        warnings.append("tandem hook-route emitted its 'nothing was rerouted' "
                        "notice: the hook fired and declined")
    if arm == "a" and int(ex.get("reroutes") or 0) and native_dispatches(ex):
        warnings.append(
            f"only {ex.get('reroutes')} of {ex.get('dispatches')} dispatches "
            f"rerouted; {native_dispatches(ex)} ran natively with no notice "
            f"(spawned agent types: {ex.get('agent_types')})")
    hook_reroutes = int(ex.get("hook_reroute_decisions") or 0)
    if hook_reroutes > int(ex.get("reroutes") or 0):
        warnings.append(f"hook rewrote {hook_reroutes} dispatch(es) but only "
                        f"{ex.get('reroutes')} spawned as the bridge")
    if ex.get("result_events") == 0:        # absent key != counted zero
        warnings.append("claude produced no result event — it was killed or "
                        "died mid-run; token totals fall back to per-message "
                        "usage and are not session-wide")
    if ex.get("result_is_error"):
        warnings.append("claude's final result event is an error")
    ex["warnings"] = warnings
    return ex


# --- TANDEM_HOME side effects (the out-of-band witness) ----------------------


def snapshot_side_effects(tandem_home: Path | str) -> set[str]:
    home = Path(tandem_home)
    try:
        return {str(p) for p in home.glob("subagents/*/logs/*.log")}
    except OSError:
        return set()


def side_effects_since(tandem_home: Path | str, before: set[str],
                       session_id: str) -> dict:
    """What arm A left behind: one `tandem sub` log per rerouted dispatch, and
    — only if something did NOT reroute — a warn stamp for this claude
    session."""
    home = Path(tandem_home)
    after = snapshot_side_effects(home)
    stamp = False
    if session_id:
        try:
            stamp = (home / "warned" / session_id).exists()
        except OSError:
            stamp = False
    return {"new_subagent_logs": len(after - before), "warn_stamp": stamp}


# --- preconditions -----------------------------------------------------------


def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def claude_version(claude_bin: str) -> tuple[bool, str]:
    try:
        p = _run([claude_bin, "--version"], timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"cannot run {claude_bin}: {exc}"
    if p.returncode != 0:
        return False, f"{claude_bin} --version exited {p.returncode}"
    return True, (p.stdout.strip().split() or [""])[0]


def plugin_installed(claude_bin: str, name: str = "tandem") -> tuple[bool, str]:
    """Read-only. The bench never installs or enables anything for the user."""
    try:
        p = _run([claude_bin, "plugin", "list", "--json"], timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"cannot run `{claude_bin} plugin list`: {exc}"
    if p.returncode != 0:
        return False, f"`claude plugin list` exited {p.returncode}"
    try:
        rows = json.loads(p.stdout)
    except ValueError:
        rows = None
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, dict) and str(row.get("id", "")).split("@")[0] == name:
                if not row.get("enabled", True):
                    return False, f"plugin {row['id']} is installed but disabled"
                return True, str(row.get("id"))
        return False, (
            f"the {name} plugin is not installed. Install it with:\n"
            "      claude plugin marketplace add Bhavya6187/tandem\n"
            "      claude plugin install tandem@tandem"
        )
    # older claude without --json: fall back to the human listing
    if f"{name}@" in p.stdout:
        return True, "(from text listing)"
    return False, f"the {name} plugin is not installed (`claude plugin list`)"


def hook_route_smoke(tandem_bin: str) -> tuple[bool, str]:
    """Prove the tandem binary claude's hook will invoke is new enough.

    Feeding it an Agent dispatch under an empty TANDEM_HOME must produce the
    'no paired session' notice: that exercises the whole hook-route path
    (config -> StateStore -> decision) and is exactly the shape that a stale
    build — hook-route present but predating the notice — fails."""
    import tempfile

    payload = json.dumps({
        "session_id": "bench-runner-check",
        "cwd": str(BENCH_DIR),
        "hook_event_name": "PreToolUse",
        "tool_name": "Task",
        "tool_input": {"description": "bench precondition probe",
                       "prompt": "probe", "subagent_type": "general-purpose"},
    })
    with tempfile.TemporaryDirectory(prefix="bench-hookcheck-") as tmp:
        env = dict(os.environ, TANDEM_HOME=tmp)
        try:
            p = subprocess.run([tandem_bin, "hook-route"], input=payload,
                               env=env, capture_output=True, text=True,
                               timeout=120)
        except (OSError, subprocess.SubprocessError) as exc:
            return False, f"cannot run `{tandem_bin} hook-route`: {exc}"
    if p.returncode != 0:
        return False, (
            f"`{tandem_bin} hook-route` exited {p.returncode} — this build has "
            "no hook-route subcommand. Install the repo's own tandem "
            "(`uv sync` in the repo, or `uv tool install --from . tandem-cli`); "
            "the PyPI release is stale."
        )
    try:
        out = json.loads(p.stdout or "{}")
    except ValueError:
        return False, f"`hook-route` printed non-JSON: {p.stdout[:200]!r}"
    if not isinstance(out, dict) or "systemMessage" not in out:
        return False, (
            "`hook-route` did not emit the 'no paired session' notice for an "
            f"empty TANDEM_HOME (got {json.dumps(out)[:200]}). This build "
            "predates the notice; arm A's invalidity check needs it."
        )
    return True, "hook-route responds"


def codex_version(codex_bin: str) -> tuple[bool, str]:
    try:
        p = _run([codex_bin, "--version"], timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"cannot run {codex_bin}: {exc}"
    if p.returncode != 0:
        return False, f"{codex_bin} --version exited {p.returncode}"
    return True, p.stdout.strip()


def docker_ok() -> tuple[bool, str]:
    try:
        p = _run(["docker", "info", "--format", "{{.ServerVersion}}"], timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"docker is not usable: {exc}"
    if p.returncode != 0:
        return False, "`docker info` failed — is the daemon running?"
    return True, f"docker {p.stdout.strip()}"


def ensure_work_dir(work: Path | str) -> Path:
    """Create --work-dir, and clear a seal an older runner used to write.

    bench/work/ used to be sealed against pytest with a `collect_ignore_glob`
    conftest.py. That cure was worse than the disease: tests/ has no
    __init__.py, so `from conftest import ...` resolves through sys.path, and a
    second conftest.py anywhere under the rootdir wins the name `conftest` and
    breaks every bench test's import. The clones now live in a dot-directory
    instead (WORKDIRS_DIR), which pytest's default norecursedirs skips without
    any config at all."""
    work = Path(work)
    work.mkdir(parents=True, exist_ok=True)
    stale = work / "conftest.py"
    try:
        if stale.is_file() and "collect_ignore_glob" in stale.read_text():
            stale.unlink()
    except OSError:
        pass
    return work


def ensure_tandem_homes(work: Path) -> dict[str, Path]:
    """Create (and re-assert) the two bench-owned TANDEM_HOMEs.

    These directories belong to the bench, so their config.toml is rewritten
    on every check/run: the whole experiment turns on arm A saying route =
    "all" and arm B saying route = "off", and a hand-edit there would silently
    turn the A/B into an A/A."""
    homes = {}
    for arm in ARMS:
        home = work / f"tandem-home-{arm}"
        home.mkdir(parents=True, exist_ok=True)
        want = (
            f"# bench arm {arm.upper()} — created by bench/runner.py.\n"
            f'# route = "{ARM_ROUTE[arm]}" is the experiment\'s independent\n'
            "# variable; the runner rewrites this whole file (losing any other\n"
            "# keys) if it ever finds a different route here. Other [subagents]\n"
            "# settings you add are left alone.\n"
            "[subagents]\n"
            f'route = "{ARM_ROUTE[arm]}"\n'
        )
        cfg = home / "config.toml"
        if cfg.is_file() and _config_route(cfg) == ARM_ROUTE[arm]:
            homes[arm] = home
            continue
        cfg.write_text(want)
        homes[arm] = home
    return homes


def _config_route(path: Path) -> str | None:
    try:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    except (OSError, ValueError):
        return None
    sub = data.get("subagents")
    return sub.get("route") if isinstance(sub, dict) else None


@dataclass
class Checks:
    rows: list[tuple[bool, str, str]] = field(default_factory=list)

    def add(self, ok: bool, name: str, detail: str) -> None:
        self.rows.append((ok, name, detail))

    @property
    def failed(self) -> bool:
        return any(not ok for ok, _, _ in self.rows)

    def render(self) -> str:
        out = []
        for ok, name, detail in self.rows:
            out.append(f"{'ok  ' if ok else 'FAIL'}  {name}: {detail}")
        out.append("all preconditions met" if not self.failed
                   else "preconditions NOT met — fix the FAIL lines above")
        return "\n".join(out)


def preconditions(args, cfg: BenchConfig, tasks: list[dict],
                  arms: list[str]) -> Checks:
    c = Checks()
    ok, detail = claude_version(args.claude_bin)
    c.add(ok, f"claude ({args.claude_bin})", detail)

    ok, detail = plugin_installed(args.claude_bin)
    c.add(ok, "claude plugin `tandem`", detail)

    tandem_bin = args.tandem_bin
    if not Path(tandem_bin).is_file() and shutil.which(tandem_bin) is None:
        c.add(False, "tandem binary", f"{tandem_bin} not found — pass --tandem-bin")
    else:
        ok, detail = hook_route_smoke(tandem_bin)
        c.add(ok, f"tandem hook-route ({tandem_bin})", detail)
        # the hook line in plugin/hooks/hooks.json is literally `tandem
        # hook-route`, so what matters is which tandem PATH resolves to inside
        # the claude child, not which one we can find here
        arm_path = os.pathsep.join(
            [str(Path(tandem_bin).resolve().parent), os.environ.get("PATH", "")])
        found = shutil.which("tandem", path=arm_path)
        same = found and os.path.realpath(found) == \
            os.path.realpath(shutil.which(tandem_bin) or tandem_bin)
        c.add(bool(same), "tandem on the run PATH",
              f"{found}" if same else
              f"`tandem` on the run PATH resolves to {found!r}, not {tandem_bin!r}")

    ok, detail = codex_version(args.codex_bin)
    c.add(ok, f"codex ({args.codex_bin})", detail +
          ("" if not ok else " (tandem's compat range is enforced by "
                             "bench/pair.py before every arm-A launch)"))

    if "a" in arms:
        p = Path(args.pair_script)
        c.add(p.is_file(), "pairing helper", str(p) if p.is_file()
              else f"{p} is missing")

    if any(t.get("family") == "swebench" for t in tasks):
        ok, detail = docker_ok()
        c.add(ok, "docker (swebench eval)", detail)

    homes = ensure_tandem_homes(Path(args.work_dir))
    c.add(True, "bench TANDEM_HOMEs",
          ", ".join(f"{a}={homes[a]} route={ARM_ROUTE[a]}" for a in ARMS))

    for t in tasks:
        ok, why = task_runnable(t)
        if not ok:
            c.add(False, f"task {t['id']}", why)
    runnable = [t["id"] for t in tasks if task_runnable(t)[0]]
    c.add(True, "tasks selected",
          f"{len(runnable)}/{len(tasks)} runnable: {', '.join(runnable) or '(none)'}")
    return c


# --- running -----------------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _spec_fields(spec: Any, workdir: str) -> tuple[str, str, dict]:
    if isinstance(spec, Mapping):
        prompt, wd, meta = spec.get("prompt"), spec.get("workdir"), spec.get("meta")
    else:
        prompt = getattr(spec, "prompt", None)
        wd = getattr(spec, "workdir", None)
        meta = getattr(spec, "meta", None)
    if not isinstance(prompt, str) or not prompt.strip():
        raise BenchError("provision() returned no prompt — see bench/family_api.py")
    return prompt, str(wd or workdir), dict(meta or {})


def _pair(pair_script: str, tandem_home: Path, workdir: str,
          verify_only: bool) -> None:
    cmd = [sys.executable, str(pair_script), "--tandem-home", str(tandem_home),
           "--cwd", workdir]
    if verify_only:
        cmd.append("--verify-only")
    p = _run(cmd, timeout=600)
    if p.returncode != 0:
        what = "verify" if verify_only else "create"
        raise BenchError(
            f"could not {what} the tandem pairing for {workdir}\n"
            f"  {' '.join(cmd)}\n"
            f"  {(p.stderr or p.stdout).strip()[:800]}"
        )


def _launch(cmd: list[str], cwd: str, env: dict, out: Path, err: Path,
            timeout_s: int) -> tuple[int, bool, float]:
    """Run claude, streaming to files. Returns (exit_code, timed_out, seconds).

    start_new_session + killpg because claude spawns subagent and hook
    processes; killing only the direct child would leave them running."""
    started = time.monotonic()
    with open(out, "wb") as fo, open(err, "wb") as fe:
        proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=fo, stderr=fe,
                                stdin=subprocess.DEVNULL, start_new_session=True)
        timed_out = False
        try:
            rc = proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_group(proc)
            rc = proc.returncode if proc.returncode is not None else -signal.SIGKILL
        except KeyboardInterrupt:
            # start_new_session means the Ctrl-C that reached us did NOT reach
            # claude. Left alone it would keep spending budget, keep dispatching
            # codex workers, and keep writing under the shared arm-A
            # TANDEM_HOME — where its late `tandem sub` logs would land inside
            # the NEXT run's side_effects_since() window and look like reroutes
            # that run never made.
            _kill_group(proc)
            raise
    return rc, timed_out, time.monotonic() - started


def _kill_group(proc: subprocess.Popen) -> None:
    """SIGTERM the child's whole process group, then SIGKILL what survives.

    A failed killpg is never a reason to stop: SIGTERM can fail (EPERM, or the
    leader already reaped while a grandchild lives on) while SIGKILL would
    still land, and giving up after the first error is how an escalation path
    silently becomes a single polite request."""
    for sig, grace in ((signal.SIGTERM, 10), (signal.SIGKILL, 5)):
        try:
            os.killpg(os.getpgid(proc.pid), sig)
        except (OSError, ProcessLookupError):
            pass
        try:
            proc.wait(timeout=grace)
            return
        except subprocess.TimeoutExpired:
            continue


@dataclass
class RunContext:
    args: Any
    cfg: BenchConfig
    run_id: str
    work: Path
    homes: dict[str, Path]
    claude_version: str = ""


def run_one(ctx: RunContext, task: dict, arm: str, repeat: int) -> dict:
    a = ctx.args
    rel = Path(ctx.run_id) / task["id"] / arm / str(repeat)
    workdir = ctx.work / WORKDIRS_DIR / rel
    resultdir = ctx.work / "results" / rel
    workdir.mkdir(parents=True, exist_ok=True)
    resultdir.mkdir(parents=True, exist_ok=True)
    # realpath: tandem matches a session's cwd as an exact string and stores the
    # symlink-resolved form, so a /tmp -> /private/tmp difference silently
    # un-pairs the directory (bench/PAIRING.md caveat 1).
    workdir_s = os.path.realpath(workdir)

    family = load_family(task["family"], a.family_dir)
    try:
        spec = family.provision(dict(task, _workdir=workdir_s), workdir_s)
    except NotImplementedError as exc:
        raise BenchError(f"{task['id']}: {exc}") from exc
    task_prompt, effective_dir, provision_meta = _spec_fields(spec, workdir_s)
    # a provisioner that clones into a subdirectory says so by returning it;
    # everything downstream (pairing, cwd, verdict) then uses that one path
    workdir_s = os.path.realpath(effective_dir)
    if not os.path.isdir(workdir_s):
        raise BenchError(
            f"{task['id']}: provision() returned workdir {workdir_s!r}, "
            "which is not a directory")
    prompt = build_prompt(task_prompt)
    (resultdir / "prompt.txt").write_text(prompt)

    home = ctx.homes[arm]
    if arm == "a":
        # create-then-verify, every time: pairing is per-directory and never
        # inherited by subdirectories, and --verify-only is the only thing that
        # actually proves hook-route will rewrite here.
        _pair(a.pair_script, home, workdir_s, verify_only=False)
        _pair(a.pair_script, home, workdir_s, verify_only=True)

    env = dict(os.environ)
    env["TANDEM_HOME"] = str(home)
    env["PATH"] = os.pathsep.join(
        [str(Path(a.tandem_bin).resolve().parent), env.get("PATH", "")])

    model = a.model or ctx.cfg.run.get("model") or ""
    cmd = [a.claude_bin, "-p", prompt, *STREAM_FLAGS]
    if model:
        cmd += ["--model", model]
    cmd += [str(f) for f in (ctx.cfg.run.get("claude_flags") or [])]

    timeout_s = timeout_for(ctx.cfg, task["family"])
    before = snapshot_side_effects(home)
    started_at = _now()
    rc, timed_out, wall = _launch(cmd, workdir_s, env,
                                  resultdir / "transcript.jsonl",
                                  resultdir / "stderr.log", timeout_s)

    ex = extract_transcript(read_stream(resultdir / "transcript.jsonl"))
    # before mark_validity: the warn stamp is a second witness for "the hook
    # declined", and arm A's validity turns on that
    ex["side_effects"] = side_effects_since(home, before, ex.get("session_id", ""))
    mark_validity(arm, ex)
    (resultdir / "extraction.json").write_text(json.dumps(ex, indent=2))

    meta = {
        "schema": SCHEMA_VERSION,
        "run_id": ctx.run_id, "task": task["id"], "family": task["family"],
        "arm": arm, "repeat": repeat,
        "model": model or "(session default)",
        "claude_bin": a.claude_bin, "claude_version": ctx.claude_version,
        "tandem_bin": str(Path(a.tandem_bin).resolve()),
        "tandem_home": str(home), "route": ARM_ROUTE[arm],
        "cwd": workdir_s, "cmd": cmd,
        "started_at": started_at, "finished_at": _now(),
        "wall_clock_s": round(wall, 3), "timeout_s": timeout_s,
        "exit_code": rc, "timed_out": timed_out,
        "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
        "prompt_chars": len(prompt),
        "provision_meta": provision_meta,
    }
    (resultdir / "meta.json").write_text(json.dumps(meta, indent=2))

    verdict = {
        "schema": SCHEMA_VERSION,
        "run_id": ctx.run_id, "task": task["id"], "family": task["family"],
        "arm": arm, "repeat": repeat,
        # the verifier (Task 3) overwrites status/passed/score/detail
        "status": "unverified", "passed": None, "score": None, "detail": {},
        "validity": ex["validity"], "warnings": ex["warnings"],
        "run": {"exit_code": rc, "timed_out": timed_out,
                "wall_clock_s": round(wall, 3),
                "model": meta["model"], "claude_version": ctx.claude_version},
        "metrics": {
            "dispatches": ex["dispatches"], "reroutes": ex["reroutes"],
            "notices": ex["notices"], "tokens_total": ex["tokens"]["total"],
            "tokens_input": ex["tokens"]["input"],
            "tokens_output": ex["tokens"]["output"],
            "cost_usd": ex["cost_usd"],
        },
        "result_dir": str(resultdir), "workdir": workdir_s,
    }
    (resultdir / "verdict.json").write_text(json.dumps(verdict, indent=2))
    return verdict


def do_run(args, tasks: list[dict], arms: list[str], cfg: BenchConfig) -> int:
    for t in tasks:
        ok, why = task_runnable(t)
        if not ok:
            raise BenchError(why)

    work = ensure_work_dir(os.path.realpath(args.work_dir))
    homes = ensure_tandem_homes(work)
    run_id = args.run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    repeats = int(args.repeats or cfg.run.get("repeats") or 1)

    plan = [(t, arm, r) for t in tasks for arm in arms for r in range(repeats)]
    if args.dry_run:
        print(f"run {run_id}: {len(plan)} runs")
        for t, arm, r in plan:
            print(f"  {t['id']}  arm {arm}  repeat {r}  "
                  f"timeout {timeout_for(cfg, t['family'])}s")
        return 0

    if not args.no_check:
        checks = preconditions(args, cfg, tasks, arms)
        if checks.failed:
            print(checks.render())
            raise BenchError("preconditions not met; nothing was run")

    ok, ver = claude_version(args.claude_bin)
    ctx = RunContext(args=args, cfg=cfg, run_id=run_id, work=work, homes=homes,
                     claude_version=ver if ok else "")

    print(f"run {run_id}: {len(plan)} runs -> {work / 'results' / run_id}")
    # A failure inside run_one (pairing, provisioning) aborts the whole matrix
    # rather than being recorded and skipped: every cause is an environment
    # problem that would repeat on the next run, and half a matrix of
    # arm-B-only results is worse than none. Results already written stay on
    # disk; re-running with the same --run-id overwrites them in place, so
    # narrow the retry with --tasks/--arms rather than repeating the matrix.
    for t, arm, r in plan:
        print(f"  {t['id']} arm {arm} repeat {r} ... ", end="", flush=True)
        v = run_one(ctx, t, arm, r)
        m = v["metrics"]
        print(f"{v['validity']}  exit={v['run']['exit_code']} "
              f"{v['run']['wall_clock_s']}s dispatches={m['dispatches']} "
              f"reroutes={m['reroutes']} notices={m['notices']}"
              f"{' TIMEOUT' if v['run']['timed_out'] else ''}")
        for w in v["warnings"]:
            print(f"      ! {w}")
    print(f"\nnow verify, then: python bench/aggregate.py --run-id {run_id}")
    return 0


# --- cli ---------------------------------------------------------------------


def _split(value: str | None) -> list[str] | None:
    if value is None:
        return None
    return [v.strip() for v in value.split(",") if v.strip()]


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--tasks-file", default=str(DEFAULT_TASKS_FILE))
    p.add_argument("--work-dir", default=str(DEFAULT_WORK_DIR),
                   help="all bench state lives here (default: %(default)s)")
    p.add_argument("--family-dir", default=str(DEFAULT_FAMILY_DIR),
                   help="directory of per-family provisioner modules")
    p.add_argument("--claude-bin", default="claude")
    p.add_argument("--codex-bin", default="codex")
    p.add_argument("--tandem-bin", default=None,
                   help="the tandem the hook will run (default: the repo's "
                        ".venv/bin/tandem if present, else PATH). Its directory "
                        "is prepended to the claude child's PATH.")
    p.add_argument("--pair-script", default=str(DEFAULT_PAIR_SCRIPT))
    p.add_argument("--tasks", default=None,
                   help="comma-separated task ids (default: all)")


def _add_run_opts(p: argparse.ArgumentParser) -> None:
    p.add_argument("--arms", default=None, help="comma-separated: a,b")
    p.add_argument("--repeats", default=None, type=int)
    p.add_argument("--run-id", default=None)
    p.add_argument("--model", default=None,
                   help="main-agent model, same in both arms")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--no-check", action="store_true",
                   help="skip the precondition check before running")


def _default_tandem_bin() -> str:
    venv = REPO_ROOT / ".venv" / "bin" / "tandem"
    if venv.is_file():
        return str(venv)
    return shutil.which("tandem") or "tandem"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="runner.py", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, helptext in (
        ("check", "verify every precondition and create the bench TANDEM_HOMEs"),
        ("run", "run the selected tasks in the selected arms"),
        ("smoke", "run the pinned smoke task on both arms"),
    ):
        p = sub.add_parser(name, help=helptext)
        _add_common(p)
        if name != "check":
            _add_run_opts(p)
    args = ap.parse_args(argv)
    if args.tandem_bin is None:
        args.tandem_bin = _default_tandem_bin()
    # absolute from here on: its parent directory is prepended to the claude
    # child's PATH, and `Path("tandem").resolve().parent` would be the cwd
    if not Path(args.tandem_bin).is_file():
        args.tandem_bin = shutil.which(args.tandem_bin) or args.tandem_bin

    try:
        # realpath once, here, so the pairing, the TANDEM_HOME the hook reads
        # and the cwd claude is handed are all the same string (PAIRING.md #1)
        try:
            ensure_work_dir(args.work_dir)
        except OSError as exc:
            raise BenchError(f"cannot create --work-dir {args.work_dir}: {exc}")
        args.work_dir = os.path.realpath(args.work_dir)
        cfg = load_tasks(args.tasks_file, args.family_dir)
        if args.cmd == "smoke":
            args.tasks = cfg.run.get("smoke_task") or ""
            args.arms = args.arms or ",".join(ARMS)
            args.repeats = args.repeats or 1
            args.model = args.model or "haiku"
        tasks = select_tasks(cfg, _split(args.tasks))
        arms = _split(getattr(args, "arms", None)) or \
            list(cfg.run.get("arms") or ARMS)
        bad = [a for a in arms if a not in ARMS]
        if bad:
            raise BenchError(f"unknown arm(s) {bad}; known: {', '.join(ARMS)}")

        if args.cmd == "check":
            checks = preconditions(args, cfg, tasks, arms)
            print(checks.render())
            return 1 if checks.failed else 0
        return do_run(args, tasks, arms, cfg)
    except BenchError as exc:
        print(f"runner.py: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
