# Codex-Model Subagents Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Native Claude Code subagent dispatches transparently execute on a cheap Codex model, via a PreToolUse reroute hook + a `codex-worker` bridge agent + a `tandem sub` execution engine.

**Architecture:** A Claude plugin (static files, never a process) registers a PreToolUse hook running `tandem hook-route`, which rewrites `Agent` dispatches to a Bash-only bridge agent; the bridge runs `tandem sub`, which executes the task on codex — cold `codex exec` by default, or a fork of the shadow rollout for full context. Results return through Claude's native tool-result/task-notification machinery; tandem's existing sync mirrors everything. Spec: `docs/specs/2026-07-31-codex-subagents-design.md`.

**Tech Stack:** Python 3.11+, click, stdlib `tomllib`/`fcntl`/`sqlite3`, pytest (+ `click.testing.CliRunner`), existing tandem modules (`ops`, `paths`, `state`, `harness`, `doctor`).

## Global Constraints

- Python ≥3.11; **no new dependencies** (config uses stdlib `tomllib`, locking uses `fcntl`).
- `tandem hook-route` must **never exit 2** (exit 2 blocks the dispatch); every failure path exits 0 — failure mode is "no savings", never "broken subagents".
- The task brief is forwarded **verbatim — no truncation, no summarization** at any step (stdin transport, not argv).
- Fork rollouts are **never registered as sync sources**: no cursors created, no echo-suppression changes, shadow untouched.
- `updatedInput` replaces the **entire** input object: carry every original field, rewrite exactly `subagent_type`, `model`, `prompt`.
- Run tests with `uv run pytest`; commit after every task; follow existing patterns (`Check`/`DoctorReport`, `_run` seam, `env_factory` fixture, `append_jsonl_fsync`).
- **Documented deviation from the spec:** the spec's `allow_fanout = true` boolean is implemented as a `fanout_feature` string (the `--enable <name>` feature name; empty = don't pass the flag) because spike S2 has not yet pinned the name. Task 7 runs S2 and updates the spec.

## File Structure

- Create `src/tandem/config.py` — read `[subagents]` from `$TANDEM_HOME/config.toml`; defaults on any error.
- Create `src/tandem/hookroute.py` — pure reroute decision logic + agent-definition lookup (no I/O beyond reading agent files).
- Modify `src/tandem/ops.py` — add `fork_shadow()`, `run_sub()`, `_sub_lock()`.
- Modify `src/tandem/cli.py` — add `sub` and `hook-route` commands; extend `status`.
- Modify `src/tandem/doctor.py` — subagent auth/shared-block checks.
- Create `plugin/` — `.claude-plugin/plugin.json`, `hooks/hooks.json`, `agents/codex-worker.md`.
- Create tests: `tests/test_config.py`, `tests/test_hookroute.py`, `tests/test_sub.py`, `tests/test_plugin.py`.

---

### Task 1: Subagents config module

**Files:**
- Create: `src/tandem/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `SubagentsConfig` frozen dataclass with fields `route: str = "all"`, `model: str = ""`, `context: str = "match"`, `fanout_feature: str = ""`, `keep_forks: bool = False`; and `load_subagents_config() -> SubagentsConfig`. Later tasks import both from `tandem.config`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_config.py
"""[subagents] config: defaults on missing/broken file, validated values."""

from tandem.config import SubagentsConfig, load_subagents_config


def test_defaults_when_file_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("TANDEM_HOME", str(tmp_path / ".tandem"))
    cfg = load_subagents_config()
    assert cfg == SubagentsConfig()
    assert (cfg.route, cfg.model, cfg.context) == ("all", "", "match")
    assert (cfg.fanout_feature, cfg.keep_forks) == ("", False)


def test_reads_values(tmp_path, monkeypatch):
    home = tmp_path / ".tandem"
    home.mkdir()
    (home / "config.toml").write_text(
        '[subagents]\nroute = "off"\nmodel = "gpt-x-mini"\n'
        'context = "full"\nfanout_feature = "collab"\nkeep_forks = true\n'
    )
    monkeypatch.setenv("TANDEM_HOME", str(home))
    cfg = load_subagents_config()
    assert cfg.route == "off"
    assert cfg.model == "gpt-x-mini"
    assert cfg.context == "full"
    assert cfg.fanout_feature == "collab"
    assert cfg.keep_forks is True


def test_invalid_values_fall_back(tmp_path, monkeypatch):
    home = tmp_path / ".tandem"
    home.mkdir()
    (home / "config.toml").write_text(
        '[subagents]\nroute = "sometimes"\ncontext = 7\nkeep_forks = "yes"\n'
    )
    monkeypatch.setenv("TANDEM_HOME", str(home))
    cfg = load_subagents_config()
    assert cfg == SubagentsConfig()  # every bad value -> default


def test_broken_toml_falls_back(tmp_path, monkeypatch):
    home = tmp_path / ".tandem"
    home.mkdir()
    (home / "config.toml").write_text("[subagents\nnot toml")
    monkeypatch.setenv("TANDEM_HOME", str(home))
    assert load_subagents_config() == SubagentsConfig()


def test_non_utf8_falls_back(tmp_path, monkeypatch):
    home = tmp_path / ".tandem"
    home.mkdir()
    (home / "config.toml").write_bytes(b'[subagents]\nmodel = "caf\xe9"\n')
    monkeypatch.setenv("TANDEM_HOME", str(home))
    assert load_subagents_config() == SubagentsConfig()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_config.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'tandem.config'`

- [ ] **Step 3: Write the implementation**

```python
# src/tandem/config.py
"""User configuration: $TANDEM_HOME/config.toml, [subagents] table only.

Unknown keys are ignored and every error yields defaults — configuration
must never be the reason subagent routing breaks (the hook's failure mode
is 'dispatch natively', and this module upholds it)."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass

from . import paths


@dataclass(frozen=True)
class SubagentsConfig:
    route: str = "all"          # "all" | "off"
    model: str = ""             # "" -> omit -m; codex's configured default
    context: str = "match"      # "match" | "task" | "full"
    fanout_feature: str = ""    # --enable <name>; "" -> flag not passed
    keep_forks: bool = False


_ROUTES = ("all", "off")
_CONTEXTS = ("match", "task", "full")


def load_subagents_config() -> SubagentsConfig:
    try:
        with open(paths.tandem_home() / "config.toml", "rb") as f:
            data = tomllib.load(f)
    # ValueError covers TOMLDecodeError (a subclass), the UnicodeDecodeError
    # tomllib raises when the file is not UTF-8, and open()'s embedded-NUL path.
    except (OSError, ValueError):
        return SubagentsConfig()
    raw = data.get("subagents")
    if not isinstance(raw, dict):
        return SubagentsConfig()
    d = SubagentsConfig()

    def pick(key: str, kind: type, default, allowed=None):
        v = raw.get(key, default)
        if not isinstance(v, kind) or (allowed and v not in allowed):
            return default
        return v

    return SubagentsConfig(
        route=pick("route", str, d.route, _ROUTES),
        model=pick("model", str, d.model),
        context=pick("context", str, d.context, _CONTEXTS),
        fanout_feature=pick("fanout_feature", str, d.fanout_feature),
        keep_forks=pick("keep_forks", bool, d.keep_forks),
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_config.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add src/tandem/config.py tests/test_config.py
git commit -m "feat: subagents config table in ~/.tandem/config.toml"
```

---

### Task 2: Shadow fork op

**Files:**
- Modify: `src/tandem/ops.py` (append after `run_oneoff`; extend module imports)
- Test: `tests/test_sub.py` (new file)

**Interfaces:**
- Consumes: `drain_source(store, session, source, flush_dangling=True)`, `_create_codex_shadow_late(store, session)`, `source_transcript(session, "codex")` — all already in `ops.py`.
- Produces: `fork_shadow(store: StateStore, session: PairedSession) -> tuple[str, Path]` returning `(fork_session_id, fork_rollout_path)`, and `_sub_lock()` context manager. Task 3 calls both.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_sub.py
"""tandem sub: shadow forking and the subagent execution op."""

import json

from tandem import ops
from tandem.util import read_jsonl

from conftest import claude_user, write_line


class TestForkShadow:
    def test_fork_copies_shadow_with_new_identity(self, env_factory):
        env = env_factory(active="claude")
        ops.fast_forward(env.store, env.session, "claude")
        write_line(env.claude_shadow, claude_user("context before fork"))

        fork_id, fork_path = ops.fork_shadow(env.store, env.session)

        assert fork_id != env.session.codex_session_id
        assert fork_path.name.endswith(f"-{fork_id}.jsonl")
        entries = read_jsonl(fork_path)
        meta = entries[0]
        assert meta["type"] == "session_meta"
        assert meta["payload"]["id"] == fork_id
        assert meta["payload"]["session_id"] == fork_id
        assert meta["payload"]["originator"] == "tandem-sub"
        assert meta["payload"]["model_provider"] == "openai"
        # the pre-fork drain landed the claude turn in the fork's history
        dump = json.dumps(entries)
        assert "context before fork" in dump

    def test_fork_is_structurally_resumable(self, env_factory):
        from tandem.doctor import validate_transcript

        env = env_factory(active="claude")
        ops.fast_forward(env.store, env.session, "claude")
        fork_id, fork_path = ops.fork_shadow(env.store, env.session)
        assert validate_transcript("codex", fork_path, fork_id) == []

    def test_fork_leaves_shadow_and_cursors_alone(self, env_factory):
        env = env_factory(active="claude")
        ops.fast_forward(env.store, env.session, "claude")
        before_bytes = env.codex_shadow.read_bytes()
        before_cursor = env.store.get_cursor(env.session.tandem_id, "codex")

        ops.fork_shadow(env.store, env.session)

        assert env.codex_shadow.read_bytes() == before_bytes
        after = env.store.get_cursor(env.session.tandem_id, "codex")
        assert after.byte_offset == before_cursor.byte_offset
        assert after.line_index == before_cursor.line_index
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_sub.py -v`
Expected: FAIL with `AttributeError: module 'tandem.ops' has no attribute 'fork_shadow'`

- [ ] **Step 3: Write the implementation**

Add to `src/tandem/ops.py` module imports: `import fcntl`, `import json`, `from contextlib import contextmanager`, `from datetime import datetime, timezone`, and extend the `from .util import` needs by adding `from .util import append_jsonl_fsync, read_jsonl, uuid7`. Then append after `_file_size`:

```python
@contextmanager
def _sub_lock():
    """Serializes drain-then-fork across parallel `tandem sub` processes.
    The cold task path never takes this lock — it touches no shared state."""
    lock_path = paths.tandem_home() / "sub.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def fork_shadow(store: StateStore, session: PairedSession) -> tuple[str, Path]:
    """Copy the codex shadow rollout into a fresh ephemeral rollout for one
    subagent worker: same history, new uuid7 identity, originator
    'tandem-sub'. The fork is NEVER registered as a sync source. Returns
    (fork_session_id, fork_path)."""
    if not session.codex_session_id:
        _create_codex_shadow_late(store, session)
        session = store.get_session(session.tandem_id) or session
    drain_source(store, session, session.active, flush_dangling=True)
    src = source_transcript(session, "codex")
    if src is None:
        raise SyncSetupError("codex shadow rollout not found")
    entries = read_jsonl(src)
    if not entries or entries[0].get("type") != "session_meta":
        raise SyncSetupError("shadow rollout has no session_meta first line")
    fork_id = uuid7()
    meta = json.loads(json.dumps(entries[0]))  # deep copy
    meta["payload"]["id"] = fork_id
    meta["payload"]["session_id"] = fork_id
    meta["payload"]["originator"] = "tandem-sub"
    now = datetime.now(timezone.utc)
    day_dir = paths.codex_sessions_dir() / now.strftime("%Y/%m/%d")
    fname = f"rollout-{now.strftime('%Y-%m-%dT%H-%M-%S')}-{fork_id}.jsonl"
    fork_path = day_dir / fname
    append_jsonl_fsync(fork_path, [meta] + entries[1:])
    return fork_id, fork_path
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_sub.py tests/test_ops.py -v`
Expected: all pass (existing ops tests must stay green)

- [ ] **Step 5: Commit**

```bash
git add src/tandem/ops.py tests/test_sub.py
git commit -m "feat: fork_shadow — ephemeral codex rollout copies for subagents"
```

---

### Task 3: `run_sub` op and the `tandem sub` CLI command

**Files:**
- Modify: `src/tandem/ops.py` (append after `fork_shadow`)
- Modify: `src/tandem/cli.py` (new command after `run_cmd`)
- Modify: `src/tandem/harness/codex.py` (extract `session_meta` / `rollout_path` so a rollout can be authored with a different originator)
- Modify: `src/tandem/constants.py` (`SUB_SEED_NOTE`)
- Test: `tests/test_sub.py` (extend)

**Interfaces:**
- Consumes: `fork_shadow`, `_sub_lock`, `_run` seam (Task 2); `load_subagents_config`, `SubagentsConfig` (Task 1); `get_adapter`, `paths.tandem_home`.
- Produces: `run_sub(store, session, task, *, model: str = "", context: str = "task", fanout_feature: str = "", keep_forks: bool = False, quiet: bool = False) -> int`, `seed_sub_rollout(session) -> tuple[str, Path]`, and the `tandem sub` command (`-m/--model`, `--context task|full`, `-q/--quiet`, task from argument or stdin). The bridge agent (Task 5) and status (Task 6) rely on: exit code mirrors codex exec; running-marker files under `$TANDEM_HOME/subagents/<tandem-id>/running/<run-id>.json` with keys `model`, `context`, `task_preview`, `pid`; retained sub rollouts moved to `$TANDEM_HOME/subagents/<tandem-id>/`.

**Both contexts resume a tandem-authored rollout.** A cold `codex exec` would write an ordinary rollout in the session cwd (non-tandem originator, fresh mtime) — exactly what `await_codex_rollout` looks for — so the next codex-id capture would bind the pair's `codex_session_id` to a throwaway worker transcript that `run_sub` then deletes. `context='task'` therefore seeds its own minimal `originator: "tandem-sub"` rollout and resumes that; Task 2's discovery guard then covers every sub rollout, fork or seed.

- [ ] **Step 1: Write the failing op tests**

Append to `tests/test_sub.py`:

```python
class TestSeedSubRollout:
    def test_seed_is_a_resumable_tandem_sub_rollout(self, env_factory):
        from tandem.doctor import validate_transcript

        env = env_factory(active="claude")
        seed_id, seed_path = ops.seed_sub_rollout(env.session)

        assert validate_transcript("codex", seed_path, seed_id) == []
        entries = read_jsonl(seed_path)
        meta = entries[0]
        assert meta["payload"]["id"] == seed_id
        assert meta["payload"]["session_id"] == seed_id
        assert meta["payload"]["cwd"] == env.cwd
        assert meta["payload"]["originator"] == "tandem-sub"
        assert meta["payload"]["model_provider"] == "openai"
        # minimal: the meta plus one note (response_item + event_msg)
        assert len(entries) == 3

    def test_seed_carries_no_history_and_drains_nothing(self, env_factory):
        env = env_factory(active="claude")
        write_line(env.claude_shadow, claude_user("pair-only context"))
        before_bytes = env.codex_shadow.read_bytes()
        before = env.store.get_cursor(env.session.tandem_id, "claude")

        _, seed_path = ops.seed_sub_rollout(env.session)

        assert "pair-only context" not in seed_path.read_text()
        assert env.codex_shadow.read_bytes() == before_bytes
        after = env.store.get_cursor(env.session.tandem_id, "claude")
        assert after.byte_offset == before.byte_offset
        assert after.line_index == before.line_index

    def test_seed_is_not_adopted_as_a_freshly_minted_codex_session(self, env_factory):
        env = env_factory(active="claude")
        started = time.time()
        ops.seed_sub_rollout(env.session)
        assert await_codex_rollout(env.cwd, started, timeout=0) is None


class _R:
    def __init__(self, code=0):
        self.returncode = code


class TestRunSub:
    def test_task_context_resumes_a_tandem_sub_seed(self, env_factory, monkeypatch):
        """A cold worker resumes its own seeded rollout rather than exec-ing
        fresh: a plain `codex exec` writes an ordinary rollout in the session
        cwd, and rollout discovery would then bind the pair's codex id to that
        throwaway (which run_sub deletes on the way out)."""
        env = env_factory(active="claude")
        calls = {}
        started = time.time()

        def fake_run(argv, cwd=None, **kw):
            calls["argv"], calls["cwd"] = argv, cwd
            seed = paths.find_codex_rollout(argv[argv.index("resume") + 1])
            calls["meta"] = read_jsonl(seed)[0]
            # while the worker runs, the seed must not look like a fresh codex
            calls["adopted"] = await_codex_rollout(env.cwd, started, timeout=0)
            return _R(0)

        monkeypatch.setattr(ops, "_run", fake_run)
        code = ops.run_sub(env.store, env.session, "audit the README",
                           model="gpt-x-mini", context="task")
        assert code == 0
        argv = calls["argv"]
        assert argv[:5] == ["codex", "exec", "--skip-git-repo-check",
                            "-m", "gpt-x-mini"]
        assert argv[5] == "resume"
        seed_id = argv[6]
        assert seed_id != env.session.codex_session_id
        assert argv[7:] == ["audit the README"]
        assert calls["cwd"] == env.cwd
        assert calls["meta"]["payload"]["originator"] == "tandem-sub"
        assert calls["meta"]["payload"]["cwd"] == env.cwd
        assert calls["adopted"] is None
        assert paths.find_codex_rollout(seed_id) is None  # cleaned up after

    def test_task_context_keep_forks_retains_seed(self, env_factory, monkeypatch):
        env = env_factory(active="claude")
        monkeypatch.setattr(ops, "_run", lambda *a, **kw: _R(0))
        ops.run_sub(env.store, env.session, "t", keep_forks=True)
        kept = list((paths.tandem_home() / "subagents"
                     / env.session.tandem_id).glob("rollout-*.jsonl"))
        assert len(kept) == 1

    def test_no_model_omits_flag_and_fanout_adds_enable(self, env_factory,
                                                        monkeypatch):
        env = env_factory(active="claude")
        calls = {}
        monkeypatch.setattr(
            ops, "_run",
            lambda argv, cwd=None, **kw: calls.update(argv=argv) or _R(0),
        )
        ops.run_sub(env.store, env.session, "t", fanout_feature="collab")
        assert "-m" not in calls["argv"]
        assert ["--enable", "collab"] == calls["argv"][3:5]

    def test_full_context_forks_resumes_and_deletes(self, env_factory,
                                                    monkeypatch):
        env = env_factory(active="claude")
        ops.fast_forward(env.store, env.session, "claude")
        calls = {}
        monkeypatch.setattr(
            ops, "_run",
            lambda argv, cwd=None, **kw: calls.update(argv=argv) or _R(0),
        )
        ops.run_sub(env.store, env.session, "deep task", context="full")
        argv = calls["argv"]
        assert "resume" in argv
        fork_id = argv[argv.index("resume") + 1]
        assert fork_id != env.session.codex_session_id
        # the fork was cleaned up (keep_forks=False)
        assert paths.find_codex_rollout(fork_id) is None
        kept = list((paths.tandem_home() / "subagents").rglob("*.jsonl"))
        assert kept == []

    def test_full_context_keep_forks_retains(self, env_factory, monkeypatch):
        env = env_factory(active="claude")
        ops.fast_forward(env.store, env.session, "claude")
        monkeypatch.setattr(ops, "_run", lambda *a, **kw: _R(0))
        ops.run_sub(env.store, env.session, "deep task", context="full",
                    keep_forks=True)
        kept = list((paths.tandem_home() / "subagents"
                     / env.session.tandem_id).glob("rollout-*.jsonl"))
        assert len(kept) == 1

    def test_exit_code_mirrors_codex(self, env_factory, monkeypatch):
        env = env_factory(active="claude")
        monkeypatch.setattr(ops, "_run", lambda *a, **kw: _R(3))
        assert ops.run_sub(env.store, env.session, "t") == 3

    def test_running_marker_lifecycle(self, env_factory, monkeypatch):
        env = env_factory(active="claude")
        seen = {}

        def fake_run(argv, cwd=None, **kw):
            run_dir = (paths.tandem_home() / "subagents"
                       / env.session.tandem_id / "running")
            seen["during"] = list(run_dir.glob("*.json"))
            # read the marker while the run is in flight; it is gone after
            seen["payload"] = [json.loads(p.read_text()) for p in seen["during"]]
            return _R(0)

        monkeypatch.setattr(ops, "_run", fake_run)
        ops.run_sub(env.store, env.session, "watch me")
        assert len(seen["during"]) == 1
        assert seen["payload"][0]["task_preview"] == "watch me"
        assert seen["during"][0].exists() is False  # removed on completion


class TestQuietRelay:
    """Quiet mode exists for the bridge agent: with inherited stdio the Bash
    output is codex's ENTIRE exec log (header, actions, token counts), and
    "return the final message verbatim" is not something a haiku relay can do
    reliably. Quiet mode makes the command's whole output BE the final
    message, via codex's own `-o/--output-last-message`."""

    def test_quiet_relays_only_the_last_message(self, env_factory, monkeypatch,
                                                capsys):
        env = env_factory(active="claude")
        calls = {}

        def fake_run(argv, cwd=None, **kw):
            calls["argv"], calls["kw"] = argv, kw
            i = argv.index("-o")
            calls["o_before_resume"] = i < argv.index("resume")
            calls["last"] = Path(argv[i + 1])
            calls["last"].write_text("FINAL ANSWER")
            return _R(0)

        monkeypatch.setattr(ops, "_run", fake_run)
        code = ops.run_sub(env.store, env.session, "t", quiet=True)

        assert code == 0
        # -o is an exec-level flag: it must precede the `resume` subcommand
        assert calls["o_before_resume"]
        # stdout carries the worker's answer and nothing else
        assert capsys.readouterr().out == "FINAL ANSWER\n"
        # the raw transcript is redirected to a retained log for debugging
        logs = (paths.tandem_home() / "subagents" / env.session.tandem_id
                / "logs")
        assert calls["last"].parent == logs
        assert calls["kw"]["stderr"] == subprocess.STDOUT
        assert list(logs.glob("*.log")) != []

    def test_quiet_falls_back_to_the_log_tail(self, env_factory, monkeypatch,
                                              capsys):
        """A codex run that dies before writing a last message must still
        relay something — otherwise the bridge reports an empty failure."""
        env = env_factory(active="claude")

        def fake_run(argv, cwd=None, stdout=None, **kw):
            stdout.write(b"boom: auth failed\nstack line\n")
            stdout.flush()
            return _R(1)

        monkeypatch.setattr(ops, "_run", fake_run)
        code = ops.run_sub(env.store, env.session, "t", quiet=True)

        assert code == 1
        out = capsys.readouterr().out
        assert "boom: auth failed" in out
        assert "stack line" in out

    def test_non_quiet_streams_with_inherited_stdio(self, env_factory,
                                                    monkeypatch, capsys):
        """Manual `tandem sub` keeps streaming: no -o, no redirection."""
        env = env_factory(active="claude")
        calls = {}
        monkeypatch.setattr(
            ops, "_run",
            lambda argv, cwd=None, **kw: calls.update(argv=argv, kw=kw) or _R(0),
        )
        ops.run_sub(env.store, env.session, "t")
        assert "-o" not in calls["argv"]
        assert calls["kw"] == {}
        assert capsys.readouterr().out == ""
```

(`tests/test_sub.py` imports `paths` alongside `ops` at module level for these,
plus `subprocess` and `pathlib.Path` for the quiet-mode tests.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_sub.py -v`
Expected: `TestRunSub` FAILs with `AttributeError: ... no attribute 'run_sub'`; `TestForkShadow` stays green.

- [ ] **Step 3: Implement `run_sub`**

Append to `src/tandem/ops.py` (add `import os` to module imports). `seed_sub_rollout` builds its rollout from two small pieces factored out of `CodexAdapter.create_shadow_transcript` — `session_meta(cwd, session_id, originator="tandem")` and `rollout_path(session_id)` — so the seed differs from a shadow only in its originator (`fork_shadow` uses `rollout_path` too):

```python
def seed_sub_rollout(session: PairedSession) -> tuple[str, Path]:
    """Author the minimal rollout a cold (`context='task'`) worker resumes:
    fresh uuid7 identity, originator 'tandem-sub', one seed note and no
    shared history.

    Cold runs resume this instead of `codex exec`-ing fresh because a plain
    exec writes an ordinary rollout — non-tandem originator, session cwd,
    fresh mtime — which `await_codex_rollout` would hand to the next
    codex-id capture, binding the pair's codex_session_id to a throwaway
    worker transcript tandem then deletes. Seeding keeps every sub rollout
    behind the same discovery guard as forks.

    Touches no cursor and no shadow: no drain, no `_sub_lock` needed.
    Returns (seed_session_id, seed_path)."""
    from .constants import SUB_SEED_NOTE

    adapter = get_adapter("codex")
    seed_id = adapter.mint_session_id()
    seed_path = adapter.rollout_path(seed_id)
    entries = [adapter.session_meta(session.cwd, seed_id, originator="tandem-sub")]
    entries += adapter.render_note(SUB_SEED_NOTE)
    append_jsonl_fsync(seed_path, entries)
    return seed_id, seed_path


def run_sub(
    store: StateStore,
    session: PairedSession,
    task: str,
    *,
    model: str = "",
    context: str = "task",
    fanout_feature: str = "",
    keep_forks: bool = False,
    quiet: bool = False,
) -> int:
    """Execute one delegated subagent task on codex. context='task' resumes
    a freshly seeded empty rollout in the session cwd (claude wrote a
    self-contained brief for a cold worker); context='full' forks the shadow
    and resumes that instead. Either way the worker runs in a tandem-authored
    'tandem-sub' rollout that is never a sync source and never adoptable as
    the pair's own codex session, and is disposed of on exit. The brief is
    passed through verbatim. Exit code mirrors codex.

    quiet=True is the bridge-agent mode: codex's raw transcript goes to a log
    file and this command's ENTIRE stdout becomes the worker's final message
    (via codex's own `-o/--output-last-message`). With inherited stdio the
    caller would instead get the whole exec log — header, actions, token
    counts — and asking a cheap relay model to extract "the final message"
    from that is unreliable. quiet=False is byte-for-byte unchanged: stdio is
    inherited so manual runs still stream live."""
    adapter = get_adapter("codex")
    argv = [adapter.binary, "exec", "--skip-git-repo-check"]
    if model:
        argv += ["-m", model]
    if fanout_feature:
        argv += ["--enable", fanout_feature]
    if context == "full":
        with _sub_lock():
            sub_id, sub_path = fork_shadow(store, session)
    else:
        sub_id, sub_path = seed_sub_rollout(session)

    sub_root = paths.tandem_home() / "subagents" / session.tandem_id
    last_path = log_path = None
    if quiet:
        logs = sub_root / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        last_path, log_path = logs / f"{sub_id}.last", logs / f"{sub_id}.log"
        # exec-level flag: must precede the `resume` subcommand, like -m
        argv += ["-o", str(last_path)]
    argv += ["resume", sub_id, task]

    marker = sub_root / "running" / f"{sub_id}.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({
        "model": model, "context": context, "pid": os.getpid(),
        "task_preview": task[:120],
    }))
    try:
        if quiet:
            with open(log_path, "wb") as fh:
                code = _run(argv, cwd=session.cwd, stdout=fh,
                            stderr=subprocess.STDOUT).returncode
        else:
            code = _run(argv, cwd=session.cwd).returncode
    finally:
        marker.unlink(missing_ok=True)
        if keep_forks:
            sub_root.mkdir(parents=True, exist_ok=True)
            sub_path.rename(sub_root / sub_path.name)
        else:
            sub_path.unlink(missing_ok=True)
        if quiet:
            _relay_last_message(last_path, log_path)
    return code


def _relay_last_message(last_path: Path, log_path: Path, tail: int = 50) -> None:
    """Print exactly what the bridge should return as its final message: the
    worker's last message, or — when codex died before writing one — the tail
    of its log, so a failed relay still carries the error text instead of
    reporting an empty failure. Never prefixes or wraps: the caller's whole
    stdout is the payload. Both files are retained as the debugging trail."""
    text = ""
    try:
        text = last_path.read_text(errors="replace")
    except OSError:
        pass
    if not text.strip():
        try:
            lines = log_path.read_text(errors="replace").splitlines()
        except OSError:
            lines = []
        text = "\n".join(lines[-tail:])
    if not text:
        return
    sys.stdout.write(text if text.endswith("\n") else text + "\n")
    sys.stdout.flush()
```

Add `SUB_SEED_NOTE` to `src/tandem/constants.py`:

```python
SUB_SEED_NOTE = "[tandem] Delegated subagent worker session (no shared history)."
```

- [ ] **Step 4: Run op tests to verify they pass**

Run: `uv run pytest tests/test_sub.py -v`
Expected: all pass

- [ ] **Step 5: Write the failing CLI tests**

Append to `tests/test_sub.py`:

```python
class TestSubCli:
    def _cli_env(self, env, monkeypatch):
        from tandem import cli
        monkeypatch.setattr(cli, "_cwd", lambda: env.cwd)
        monkeypatch.setattr(
            cli, "_check_versions",
            lambda warn_only=False: {"claude": "2.1.220", "codex": "0.145.0"},
        )
        return cli

    def test_sub_reads_task_from_stdin(self, env_factory, monkeypatch):
        import click.testing
        env = env_factory(active="claude")
        cli = self._cli_env(env, monkeypatch)
        calls = {}
        monkeypatch.setattr(
            ops, "run_sub",
            lambda store, session, task, **kw: calls.update(
                task=task, kw=kw) or 0,
        )
        r = click.testing.CliRunner().invoke(
            cli.main, ["sub"], input="line one\nline two\n")
        assert r.exit_code == 0
        assert calls["task"] == "line one\nline two"
        assert calls["kw"]["context"] == "task"   # config default: match->task

    def test_sub_flags_override_config(self, env_factory, monkeypatch):
        import click.testing
        env = env_factory(active="claude")
        cli = self._cli_env(env, monkeypatch)
        calls = {}
        monkeypatch.setattr(
            ops, "run_sub",
            lambda store, session, task, **kw: calls.update(kw=kw) or 0,
        )
        r = click.testing.CliRunner().invoke(
            cli.main, ["sub", "-m", "gpt-x-mini", "--context", "full", "brief"])
        assert r.exit_code == 0
        assert calls["kw"]["model"] == "gpt-x-mini"
        assert calls["kw"]["context"] == "full"
        assert calls["kw"]["quiet"] is False

    def test_sub_quiet_flag_forwards(self, env_factory, monkeypatch):
        import click.testing
        env = env_factory(active="claude")
        cli = self._cli_env(env, monkeypatch)
        calls = {}
        monkeypatch.setattr(
            ops, "run_sub",
            lambda store, session, task, **kw: calls.update(kw=kw) or 0,
        )
        r = click.testing.CliRunner().invoke(cli.main, ["sub", "-q", "brief"])
        assert r.exit_code == 0
        assert calls["kw"]["quiet"] is True

    def test_sub_empty_task_errors(self, env_factory, monkeypatch):
        import click.testing
        env = env_factory(active="claude")
        cli = self._cli_env(env, monkeypatch)
        r = click.testing.CliRunner().invoke(cli.main, ["sub"], input="  \n")
        assert r.exit_code == 1
        assert "empty task" in r.output

    def test_sub_empty_task_errors(self, env_factory, monkeypatch):
        import click.testing
        env = env_factory(active="claude")
        cli = self._cli_env(env, monkeypatch)
        r = click.testing.CliRunner().invoke(cli.main, ["sub"], input="  \n")
        assert r.exit_code == 1
        assert "empty task" in r.output
```

Run: `uv run pytest tests/test_sub.py::TestSubCli -v`
Expected: FAIL with `Error: No such command 'sub'`

- [ ] **Step 6: Implement the CLI command**

In `src/tandem/cli.py`, add after `run_cmd`:

```python
@main.command()
@click.option("-m", "--model", default=None,
              help="Codex model for this worker (config default otherwise).")
@click.option("--context", "context_mode",
              type=click.Choice(["task", "full"]), default=None,
              help="Worker context: cold task-only, or a full fork of the "
                   "paired session (config policy decides by default).")
@click.option("-q", "--quiet", is_flag=True,
              help="Print only the worker's final message (raw codex output "
                   "goes to a log under ~/.tandem/subagents/<id>/logs). Used "
                   "by the bridge agent, which relays stdout verbatim.")
@click.argument("task", required=False)
def sub(model: str | None, context_mode: str | None, quiet: bool,
        task: str | None) -> None:
    """Run one delegated subagent task on codex (task argument or stdin).

    Used by the tandem plugin's codex-worker bridge; also works manually."""
    from . import ops
    from .config import load_subagents_config

    if task is None or task == "-":
        task = sys.stdin.read()
    task = task.strip()
    if not task:
        click.secho("error: empty task brief.", fg="red", err=True)
        sys.exit(1)
    cfg = load_subagents_config()
    with StateStore() as store:
        session = _require_session(store)
        code = ops.run_sub(
            store, session, task,
            model=model if model is not None else cfg.model,
            context=context_mode or ("full" if cfg.context == "full" else "task"),
            fanout_feature=cfg.fanout_feature,
            keep_forks=cfg.keep_forks,
            quiet=quiet,
        )
    sys.exit(code)
```

- [ ] **Step 7: Run the full suite**

Run: `uv run pytest`
Expected: all pass

- [ ] **Step 8: Commit**

```bash
git add src/tandem/ops.py src/tandem/cli.py src/tandem/harness/codex.py \
        src/tandem/constants.py tests/test_sub.py
git commit -m "feat: tandem sub — execute delegated subagent tasks on codex"
```

---

### Task 4: Hook routing — `hookroute.py` and `tandem hook-route`

**Files:**
- Create: `src/tandem/hookroute.py`
- Modify: `src/tandem/cli.py` (new command after `sub`)
- Test: `tests/test_hookroute.py`

**Interfaces:**
- Consumes: `SubagentsConfig` (Task 1).
- Produces: `route(payload: dict, cfg: SubagentsConfig, cwd: str, claude_home: Path, has_session: bool, codex_ok: bool) -> dict | None`, `find_agent_body(subagent_type: str, cwd: str, claude_home: Path) -> str`, constants `BRIDGE_AGENT = "codex-worker"` and `BRIDGE_MODEL = "haiku"`. The plugin (Task 5) invokes the CLI command `tandem hook-route`.

The emitted JSON shape (verify field names against
https://code.claude.com/docs/en/hooks#pretooluse-decision-control before
implementing):

```json
{"hookSpecificOutput": {"hookEventName": "PreToolUse",
  "permissionDecision": "allow",
  "permissionDecisionReason": "tandem: rerouted to codex-worker",
  "updatedInput": {"...": "every original field, three rewritten"}}}
```

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_hookroute.py
"""hook-route: reroute native Agent dispatches to the codex-worker bridge.
Failure discipline: any problem -> None (native dispatch), CLI always exit 0."""

import json
from pathlib import Path

from tandem.config import SubagentsConfig
from tandem.hookroute import BRIDGE_AGENT, BRIDGE_MODEL, find_agent_body, route


def _payload(subagent_type="Explore", prompt="find the tests", **extra):
    ti = {"subagent_type": subagent_type, "prompt": prompt,
          "description": "short label", "model": "opus"}
    ti.update(extra)
    return {"hook_event_name": "PreToolUse", "tool_name": "Agent",
            "cwd": "/tmp/x", "tool_input": ti}


CFG = SubagentsConfig()


def _route(payload, cfg=CFG, has_session=True, codex_ok=True,
           cwd="/tmp/x", claude_home=Path("/nonexistent")):
    return route(payload, cfg, cwd, claude_home,
                 has_session=has_session, codex_ok=codex_ok)


class TestRewrite:
    def test_reroutes_and_rewrites_exactly_three_fields(self):
        out = _route(_payload(run_in_background=True))
        hso = out["hookSpecificOutput"]
        assert hso["hookEventName"] == "PreToolUse"
        assert hso["permissionDecision"] == "allow"
        ui = hso["updatedInput"]
        assert ui["subagent_type"] == BRIDGE_AGENT
        assert ui["model"] == BRIDGE_MODEL          # opus would override haiku
        assert ui["prompt"] == "find the tests"     # verbatim, built-in type
        assert ui["description"] == "short label"   # untouched
        assert ui["run_in_background"] is True      # unknown fields carried

    def test_task_alias_is_rerouted(self):
        payload = _payload()
        payload["tool_name"] = "Task"  # the documented alias of Agent
        ui = _route(payload)["hookSpecificOutput"]["updatedInput"]
        assert ui["subagent_type"] == BRIDGE_AGENT

    def test_named_agent_body_is_inlined(self, tmp_path):
        agents = tmp_path / "proj" / ".claude" / "agents"
        agents.mkdir(parents=True)
        (agents / "reviewer.md").write_text(
            "---\nname: code-reviewer\ndescription: reviews\n---\n"
            "Always check for X."
        )
        out = _route(_payload(subagent_type="code-reviewer"),
                     cwd=str(tmp_path / "proj"))
        p = out["hookSpecificOutput"]["updatedInput"]["prompt"]
        assert "Always check for X." in p
        assert p.endswith("find the tests")  # original brief last, verbatim


class TestPassthrough:
    def test_fork_passes_through(self):
        assert _route(_payload(subagent_type="fork")) is None

    def test_bridge_loop_guard(self):
        assert _route(_payload(subagent_type=BRIDGE_AGENT)) is None

    def test_route_off(self):
        assert _route(_payload(), cfg=SubagentsConfig(route="off")) is None

    def test_no_session_or_unhealthy_codex(self):
        assert _route(_payload(), has_session=False) is None
        assert _route(_payload(), codex_ok=False) is None

    def test_other_tool_names_pass_through(self):
        payload = _payload()          # otherwise fully rewritable
        payload["tool_name"] = "Bash"
        assert _route(payload) is None
        del payload["tool_name"]
        assert _route(payload) is None

    def test_malformed_input(self):
        assert _route({"tool_name": "Agent", "tool_input": "not a dict"}) is None
        assert _route(_payload(prompt="")) is None


class TestFindAgentBody:
    def test_builtin_and_plugin_scoped_have_no_body(self, tmp_path):
        assert find_agent_body("Explore", str(tmp_path), tmp_path) == ""
        assert find_agent_body("my-plugin:reviewer", str(tmp_path),
                               tmp_path) == ""

    def test_walks_up_and_falls_back_to_user_home(self, tmp_path):
        (tmp_path / ".claude" / "agents").mkdir(parents=True)
        (tmp_path / ".claude" / "agents" / "helper.md").write_text(
            "---\nname: helper\n---\nBody from repo root."
        )
        nested = tmp_path / "a" / "b"
        nested.mkdir(parents=True)
        body = find_agent_body("helper", str(nested), tmp_path / "nohome")
        assert body == "Body from repo root."


class TestCli:
    def test_prints_decision_and_exits_zero(self, env_factory, monkeypatch):
        import click.testing
        from tandem import cli
        env = env_factory(active="claude")
        payload = _payload()
        payload["cwd"] = env.cwd
        r = click.testing.CliRunner().invoke(
            cli.main, ["hook-route"], input=json.dumps(payload))
        assert r.exit_code == 0
        out = json.loads(r.output)
        assert out["hookSpecificOutput"]["updatedInput"]["subagent_type"] \
            == BRIDGE_AGENT

    def test_any_crash_exits_zero_silent(self, monkeypatch, tmp_path):
        import click.testing
        from tandem import cli, hookroute
        monkeypatch.setenv("TANDEM_HOME", str(tmp_path / ".tandem"))
        monkeypatch.setattr(hookroute, "route",
                            lambda *a, **kw: 1 / 0)
        r = click.testing.CliRunner().invoke(
            cli.main, ["hook-route"], input=json.dumps(_payload()))
        assert r.exit_code == 0
        assert r.output == ""

    def test_garbage_stdin_exits_zero_silent(self, tmp_path, monkeypatch):
        import click.testing
        from tandem import cli
        monkeypatch.setenv("TANDEM_HOME", str(tmp_path / ".tandem"))
        r = click.testing.CliRunner().invoke(
            cli.main, ["hook-route"], input="not json {{{")
        assert r.exit_code == 0
        assert r.output == ""
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_hookroute.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'tandem.hookroute'`

- [ ] **Step 3: Write `hookroute.py`**

```python
# src/tandem/hookroute.py
"""PreToolUse routing: should this native Agent dispatch run on codex?

Pure decision logic — the CLI wrapper owns process concerns (stdin, exit
codes). Returning None means 'emit nothing': the dispatch proceeds
natively. That is the failure mode for everything unexpected."""

from __future__ import annotations

from pathlib import Path

from .config import SubagentsConfig

BRIDGE_AGENT = "codex-worker"
BRIDGE_MODEL = "haiku"


def route(
    payload: dict,
    cfg: SubagentsConfig,
    cwd: str,
    claude_home: Path,
    *,
    has_session: bool,
    codex_ok: bool,
) -> dict | None:
    # defense in depth: the plugin's matcher is `Agent|Task`, but a matcher is
    # config we do not control at call time — never rewrite another tool's input
    if payload.get("tool_name") not in ("Agent", "Task"):
        return None
    if cfg.route != "all" or not has_session or not codex_ok:
        return None
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    subagent_type = tool_input.get("subagent_type") or ""
    # forks keep claude's native full-context contract; bridge = loop guard
    if subagent_type in ("fork", BRIDGE_AGENT):
        return None
    prompt = tool_input.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return None
    body = find_agent_body(subagent_type, cwd, claude_home)
    if body:
        prompt = (
            "Instructions for this task (from the dispatching session's "
            f"{subagent_type!r} agent definition):\n\n{body}\n\n---\n\n"
            + prompt
        )
    updated = dict(tool_input)
    updated["subagent_type"] = BRIDGE_AGENT
    # per-invocation model overrides agent frontmatter; without this rewrite
    # the bridge would run on the dispatch's model (observed: opus)
    updated["model"] = BRIDGE_MODEL
    updated["prompt"] = prompt
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "permissionDecisionReason": "tandem: rerouted to codex-worker",
            "updatedInput": updated,
        }
    }


def find_agent_body(subagent_type: str, cwd: str, claude_home: Path) -> str:
    """The definition body a named claude agent would have received as its
    system prompt: every .claude/agents/ from cwd up to the filesystem
    root, then <claude_home>/agents/, searched recursively, first `name`
    match wins. Built-in and plugin-scoped types have no local file."""
    if not subagent_type or ":" in subagent_type:
        return ""
    bases: list[Path] = []
    d = Path(cwd)
    while True:
        bases.append(d / ".claude" / "agents")
        if d.parent == d:
            break
        d = d.parent
    bases.append(claude_home / "agents")
    for base in bases:
        if not base.is_dir():
            continue
        for f in sorted(base.rglob("*.md")):
            name, body = _parse_agent_file(f)
            if name == subagent_type:
                return body
    return ""


def _parse_agent_file(path: Path) -> tuple[str, str]:
    try:
        text = path.read_text()
    except OSError:
        return "", ""
    name, body = path.stem, text
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            for line in text[3:end].splitlines():
                if line.strip().startswith("name:"):
                    name = line.split(":", 1)[1].strip()
            body = text[end + 4:]
    return name, body.strip()
```

- [ ] **Step 4: Add the CLI command**

In `src/tandem/cli.py`, after `sub`:

```python
@main.command(name="hook-route")
def hook_route_cmd() -> None:
    """Claude Code PreToolUse hook: reroute subagent dispatches to codex.

    Reads hook JSON on stdin; prints a decision or nothing. This function
    ALWAYS exits 0 — exit 2 would block the dispatch, and any failure here
    must degrade to native behavior.

    The function body is not the whole story: click's usage-error path exits
    2 before this ever runs (version skew — plugin installed, an older
    tandem on PATH without this subcommand — or a stray argument). So the
    hook MUST be registered as `tandem hook-route || true`; that shell guard
    is what makes exit 2 unreachable in practice."""
    try:
        from .config import load_subagents_config
        from .hookroute import route

        payload = json.loads(sys.stdin.read() or "{}")
        cwd = payload.get("cwd") or _cwd()
        cfg = load_subagents_config()
        with StateStore() as store:
            session = store.latest_session_for_cwd(cwd)
        codex_ok = False
        if session is not None:
            adapter = get_adapter("codex")
            v = adapter.detect_version()
            codex_ok = v is not None and adapter.version_supported(v)
        decision = route(payload, cfg, cwd, paths.claude_home(),
                         has_session=session is not None, codex_ok=codex_ok)
        if decision is not None:
            click.echo(json.dumps(decision))
    except Exception:
        pass
    sys.exit(0)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_hookroute.py -v && uv run pytest`
Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add src/tandem/hookroute.py src/tandem/cli.py tests/test_hookroute.py
git commit -m "feat: tandem hook-route — PreToolUse reroute of Agent dispatches"
```

---

### Task 5: The plugin — bridge agent, hook registration, manifest

**Files:**
- Create: `plugin/.claude-plugin/plugin.json`
- Create: `plugin/hooks/hooks.json`
- Create: `plugin/agents/codex-worker.md`
- Test: `tests/test_plugin.py`

**Interfaces:**
- Consumes: the CLI commands `tandem hook-route` (Task 4) and `tandem sub -q` (Task 3) — the bridge depends on quiet mode, where the command's entire stdout is the worker's final message.
- Produces: an installable local plugin. Loaded for development/E2E with `claude --plugin-dir /Users/bhavya/git/tandem/plugin` (verify the flag spelling with `claude --help`; the plugins doc also documents marketplace installs for distribution later).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_plugin.py
"""The plugin is static registration only — validate the three files."""

import json
import re
from pathlib import Path

PLUGIN = Path(__file__).parent.parent / "plugin"


def test_manifest_parses():
    m = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text())
    assert m["name"] == "tandem"
    assert m["version"]


def test_hooks_register_hook_route():
    h = json.loads((PLUGIN / "hooks" / "hooks.json").read_text())
    entries = h["hooks"]["PreToolUse"]
    assert entries[0]["matcher"] == "Agent|Task"
    cmds = [hk["command"] for hk in entries[0]["hooks"]]
    # `|| true` is load-bearing: click exits 2 on a usage error (older
    # tandem on PATH without the subcommand), and exit 2 blocks dispatches
    assert cmds == ["tandem hook-route || true"]


def test_bridge_agent_definition():
    text = (PLUGIN / "agents" / "codex-worker.md").read_text()
    front = text.split("---")[1]
    assert re.search(r"^name:\s*codex-worker\s*$", front, re.M)
    assert re.search(r"^model:\s*haiku\s*$", front, re.M)
    assert re.search(r"^tools:\s*Bash\(tandem sub:\*\)\s*$", front, re.M)
    body = text.split("---", 2)[2]
    assert "tandem sub" in body
    assert "verbatim" in body
    assert "[tandem-sub failed]" in body
    # quiet mode: the command's whole output IS the final message, so the
    # relay has nothing to extract (inherited stdio would hand it codex's
    # entire exec log instead)
    assert "tandem sub -q" in body
    # a fixed heredoc delimiter is a shell-injection hazard: a task message
    # containing that line truncates the brief and runs the rest as shell
    assert "TANDEM_TASK_EOF_" in body
    assert re.search(r"unless .*appears|appears .*in the task", body)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_plugin.py -v`
Expected: FAIL with `FileNotFoundError`

- [ ] **Step 3: Create the plugin files**

`plugin/.claude-plugin/plugin.json`:

```json
{
  "name": "tandem",
  "description": "Route Claude Code subagent dispatches to cheap codex models via tandem.",
  "version": "0.1.0",
  "author": {"name": "tandem"}
}
```

`plugin/hooks/hooks.json`:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Agent|Task",
        "hooks": [
          {"type": "command", "command": "tandem hook-route || true"}
        ]
      }
    ]
  }
}
```

`plugin/agents/codex-worker.md`:

```markdown
---
name: codex-worker
description: Executes delegated tasks on a codex model via tandem. Dispatched automatically by tandem's reroute hook; not meant for manual selection.
model: haiku
tools: Bash(tandem sub:*)
---

You are a relay between this session and a codex worker. Do exactly this:

1. Run ONE command: `tandem sub -q` with your ENTIRE task message — every
   line, byte-for-byte, nothing added or removed — on stdin, via heredoc:

   ```
   tandem sub -q <<'TANDEM_TASK_EOF'
   <your entire task message here>
   TANDEM_TASK_EOF
   ```

   Choose the delimiter BEFORE writing the command: use `TANDEM_TASK_EOF`
   unless that string appears anywhere in the task message; in that case
   append random digits (e.g. `TANDEM_TASK_EOF_84613`) and check again,
   until the delimiter appears nowhere in the message. A delimiter that
   collides with a line of the task ends the heredoc early — the brief is
   truncated and the rest of it runs as shell commands.

   Set the Bash tool's timeout parameter to 600000 (codex runs are long).

2. If the command exits 0: its entire output IS the worker's final message.
   Return that output as your final message, verbatim — no summary, no
   commentary, no added headers, nothing trimmed.

3. If it exits nonzero: return its output prefixed with
   `[tandem-sub failed]` and stop. Do not attempt the task yourself.
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_plugin.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add plugin tests/test_plugin.py
git commit -m "feat: tandem claude plugin — reroute hook + codex-worker bridge"
```

---

### Task 6: Doctor and status surfaces

**Files:**
- Modify: `src/tandem/doctor.py` (new function + one call in `run_doctor` before the `if live:` line)
- Modify: `src/tandem/cli.py` (`status` command, after the quarantine block)
- Test: `tests/test_sub.py` (extend)

**Interfaces:**
- Consumes: `load_subagents_config` (Task 1); marker/retention layout from Task 3 (`$TANDEM_HOME/subagents/<tandem-id>/running/*.json`, retained `rollout-*.jsonl`); `DoctorReport.warn` (existing).
- Produces: `_subagent_checks(report: DoctorReport, session) -> None` in `doctor.py`; status output lines starting `  subagent running:` and `  retained forks:`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_sub.py`:

```python
class TestDoctorAndStatus:
    def test_doctor_warns_when_no_model_is_configured(self, env_factory,
                                                      monkeypatch):
        """The shipped defaults are route="all" + model="" — everything is
        rerouted, but with no `-m` the worker runs on the codex account's
        default, which is the frontier tier (S1: gpt-5.6-sol)."""
        from tandem import paths
        from tandem.doctor import run_doctor
        env = env_factory(active="claude")
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        report = run_doctor(env.store, env.session, live=False)
        assert any("no model configured" in c.message for c in report.checks
                   if c.status == "warn")
        (paths.tandem_home() / "config.toml").write_text(
            '[subagents]\nmodel = "gpt-5.6-luna"\n')
        report = run_doctor(env.store, env.session, live=False)
        assert not any("no model configured" in c.message
                       for c in report.checks)

    def test_doctor_warns_on_api_key_env(self, env_factory, monkeypatch):
        from tandem.doctor import run_doctor
        env = env_factory(active="claude")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        report = run_doctor(env.store, env.session, live=False)
        assert any("OPENAI_API_KEY" in c.message for c in report.checks
                   if c.status == "warn")

    def test_doctor_nudges_shared_block(self, env_factory, monkeypatch):
        from pathlib import Path
        from tandem.doctor import run_doctor
        env = env_factory(active="claude")
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        Path(env.cwd, "CLAUDE.md").write_text("# rules, no markers\n")
        report = run_doctor(env.store, env.session, live=False)
        assert any("tandem:shared" in c.message for c in report.checks)

    def test_status_lists_running_and_retained(self, env_factory, monkeypatch):
        import click.testing
        from tandem import cli, paths
        env = env_factory(active="claude")
        monkeypatch.setattr(cli, "_cwd", lambda: env.cwd)
        monkeypatch.setattr(
            cli, "_check_versions",
            lambda warn_only=False: {"claude": "2.1.220", "codex": "0.145.0"},
        )
        sub_root = paths.tandem_home() / "subagents" / env.session.tandem_id
        (sub_root / "running").mkdir(parents=True)
        (sub_root / "running" / "r1.json").write_text(json.dumps(
            {"model": "gpt-x-mini", "context": "task",
             "task_preview": "audit the README", "pid": 1}))
        (sub_root / "rollout-x-1.jsonl").write_text("{}\n")
        r = click.testing.CliRunner().invoke(cli.main, ["status"])
        assert r.exit_code == 0
        assert "subagent running: gpt-x-mini (task) audit the README" in r.output
        assert "retained forks: 1" in r.output

    def test_doctor_survives_non_object_auth_json(self, env_factory, monkeypatch):
        """auth.json that is valid JSON but not an object must not crash
        doctor: `.get` on a list raises AttributeError, not ValueError."""
        from tandem import paths
        from tandem.doctor import run_doctor
        env = env_factory(active="claude")
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        auth = paths.codex_home() / "auth.json"
        auth.parent.mkdir(parents=True, exist_ok=True)
        auth.write_text("[]")
        report = run_doctor(env.store, env.session, live=False)
        assert not any("API-key" in c.message for c in report.checks)
        assert not any("OPENAI_API_KEY" in c.message for c in report.checks)

    def test_status_skips_non_object_marker(self, env_factory, monkeypatch):
        """A marker holding valid-but-non-object JSON is skipped, not fatal."""
        import click.testing
        from tandem import cli, paths
        env = env_factory(active="claude")
        monkeypatch.setattr(cli, "_cwd", lambda: env.cwd)
        monkeypatch.setattr(
            cli, "_check_versions",
            lambda warn_only=False: {"claude": "2.1.220", "codex": "0.145.0"},
        )
        run_dir = (paths.tandem_home() / "subagents" / env.session.tandem_id
                   / "running")
        run_dir.mkdir(parents=True)
        # sorts first, so it would crash before the good marker is reached
        (run_dir / "bad.json").write_text('"3"')
        (run_dir / "good.json").write_text(json.dumps(
            {"model": "gpt-x-mini", "context": "task",
             "task_preview": "audit the README", "pid": 1}))
        r = click.testing.CliRunner().invoke(cli.main, ["status"])
        assert r.exit_code == 0
        assert "subagent running: gpt-x-mini (task) audit the README" in r.output
        assert r.output.count("subagent running:") == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_sub.py::TestDoctorAndStatus -v`
Expected: FAIL (no warn lines / no status lines)

- [ ] **Step 3: Implement the doctor checks**

Append to `src/tandem/doctor.py` (add `import os` to module imports), and call `_subagent_checks(report, session)` in `run_doctor` immediately before the `if live:` line:

```python
def _subagent_checks(report: DoctorReport, session) -> None:
    """Subagent routing hygiene: routing is on by default but the model is
    not chosen for you, billing follows codex auth, and codex workers only
    see CLAUDE.md content inside the tandem:shared block."""
    from . import paths
    from .config import load_subagents_config

    cfg = load_subagents_config()
    if cfg.route == "off":
        return
    # model="" means no `-m`, i.e. the codex account default — the frontier
    # tier on every plan seen so far. Combined with the route="all" default
    # that silently sends every dispatch to the most expensive model, so it
    # is a warning, not a note. The dataclass default stays empty on
    # purpose: a baked-in id would 400 on accounts that lack it.
    if not cfg.model:
        report.warn(
            "subagents: no model configured — workers will use your codex "
            "account's default (set [subagents] model in "
            "~/.tandem/config.toml to a cheap tier)"
        )
    if os.environ.get("OPENAI_API_KEY"):
        report.warn(
            "subagents: OPENAI_API_KEY is set — codex may bill the API "
            "instead of your ChatGPT subscription"
        )
    auth_path = paths.codex_home() / "auth.json"
    try:
        auth = json.loads(auth_path.read_text())
    except (OSError, ValueError):
        auth = None
    # valid JSON that is not an object would make .get raise AttributeError,
    # which no doctor check may do: an unreadable auth.json is a skipped
    # check, never a traceback.
    if isinstance(auth, dict) and auth.get("OPENAI_API_KEY") and not auth.get("tokens"):
        report.warn(
            "subagents: codex auth is API-key based — subagent runs "
            "will bill the API, not the subscription"
        )
    claude_md = Path(session.cwd) / "CLAUDE.md"
    try:
        if "tandem:shared:begin" not in claude_md.read_text():
            report.warn(
                "subagents: CLAUDE.md has no tandem:shared block — project "
                "rules will not reach codex workers (move subagent-relevant "
                "rules into the shared block)"
            )
    except OSError:
        pass
```

- [ ] **Step 4: Implement the status lines**

In `src/tandem/cli.py` `status()`, after the quarantine block:

```python
        sub_root = paths.tandem_home() / "subagents" / session.tandem_id
        run_dir = sub_root / "running"
        if run_dir.is_dir():
            for m in sorted(run_dir.glob("*.json")):
                try:
                    d = json.loads(m.read_text())
                except (OSError, ValueError):
                    continue
                if not isinstance(d, dict):
                    continue  # non-object marker: skip it, never traceback
                click.echo(
                    f"  subagent running: {d.get('model') or 'default-model'} "
                    f"({d.get('context')}) {d.get('task_preview', '')}"
                )
        kept = sorted(sub_root.glob("rollout-*.jsonl")) if sub_root.is_dir() else []
        if kept:
            click.echo(f"  retained forks: {len(kept)} under {sub_root}")
```

- [ ] **Step 5: Run the full suite**

Run: `uv run pytest`
Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add src/tandem/doctor.py src/tandem/cli.py tests/test_sub.py
git commit -m "feat: doctor auth/shared-block checks + status subagent listing"
```

---

### Task 7: Docs, spikes S1–S4, live E2E

**Files:**
- Modify: `README.md` (new "Codex subagents" section after "Two model families on one problem"; new cheat-sheet row)
- Modify: `docs/specs/2026-07-31-codex-subagents-design.md` (record spike results; replace `allow_fanout` with `fanout_feature` in the config block)
- Test: manual live checklist (this machine has both CLIs + subscriptions)

**Interfaces:**
- Consumes: everything shipped in Tasks 1–6.
- Produces: user-facing docs; validated spike facts recorded in the spec's Spikes section (edit each S1–S4 bullet in place, replacing its open question with the observed result).

- [ ] **Step 1: README section + cheat-sheet row**

Add to the cheat-sheet table: `| \`tandem sub "…"\` | Run one delegated task on a codex model (used by the plugin's reroute hook) |`

New section after "Two model families on one problem":

```markdown
### 🐣 Subagents on the cheap model.

Install the tandem plugin and Claude's subagent dispatches run on a cheap
codex model instead — automatically, with the task brief forwarded
verbatim and results returned through Claude's own machinery. Claude
orchestrates; codex does the legwork; your Claude quota stays for the
main thread. Routing lives in `~/.tandem/config.toml` (`[subagents]`),
never in prompts:

    claude --plugin-dir /path/to/tandem/plugin   # try it locally

Forks and specialized flows pass through untouched; remove the plugin and
Claude is byte-for-byte stock again.
```

(Verify the `--plugin-dir` flag spelling with `claude --help` and adjust the README line to whatever the installed CLI accepts.)

- [ ] **Step 2: Spike S1 — cheap model ids and `-m` on resume**

Run (in a scratch dir with a tandem session):

```bash
codex exec --skip-git-repo-check -m gpt-5.6-sol-mini "reply with exactly: ok" ; echo "exit=$?"
# try candidate mini ids until one succeeds; `codex /model` in the TUI lists them
tandem sub --context full -m <working-mini-id> "reply with exactly: ok"
```

Then confirm the fork's rollout (`keep_forks = true` in config for this test) records the requested model in its `turn_context` line, not the account default. Record the working id(s) and the resume-`-m` verdict in the spec's S1 bullet; set the shipped default `model` guidance in README accordingly.

- [ ] **Step 3: Spike S2 — fanout feature name**

Run `codex --help | grep -A2 enable` and `codex features 2>/dev/null || codex config 2>/dev/null` to enumerate feature names; look for the multi-agent/collab feature (spawn_agent tools were observed in rollouts on this machine). Test: `codex exec --enable <name> "spawn a trivial agent that replies ok, then report"`. Record the name in the spec's S2 bullet and document setting `fanout_feature = "<name>"` in the README config example. If no flag exists on codex 0.145, record that and leave `fanout_feature` empty (native default behavior).

- [ ] **Step 4: Spikes S3+S4 — live E2E through the plugin**

```bash
cd ~/git/tandem && uv tool install . --force   # tandem on PATH for the hook
cd <scratch project> && tandem                  # pair a session, exit claude
claude --plugin-dir ~/git/tandem/plugin
# in claude: "dispatch a subagent to count the .py files here and report"
```

Verify, in order: (1) the transcript's Agent dispatch carries `subagent_type: "codex-worker"` and `model: "haiku"` (S3: matcher + updatedInput fired on 2.1.220); (2) a codex rollout for the worker exists and did the work; (3) the bridge's final message is codex's output verbatim, unembellished (S4 — if haiku editorializes, tighten `plugin/agents/codex-worker.md` wording and repeat); (4) `tandem status` during the run shows `subagent running:`; (5) after the turn, `tandem switch` + `codex resume` shows the dispatch/result synced into the shadow; (6) `tandem doctor` is green. Record S3/S4 results in the spec.

- [ ] **Step 5: Commit**

```bash
git add README.md docs/specs/2026-07-31-codex-subagents-design.md
git commit -m "docs: codex subagents — README section, spike results in spec"
```

---

## Self-Review Notes

- **Spec coverage:** config (T1), fork engine + `sub` + markers (T2/T3), hook + rewrite rules + exit discipline (T4), plugin trio (T5), doctor auth/shared-block + status (T6), docs + S1–S4 + E2E (T7). Deferred spec items (Monitor, matchers, `route_forks`, batch, MCP) are "Later" in the spec and intentionally absent here.
- **Deviation** (`allow_fanout` → `fanout_feature`) is declared in Global Constraints and reconciled with the spec in Task 7.
- **Type consistency:** `fork_shadow -> tuple[str, Path]`; `run_sub(..., model="", context="task", fanout_feature="", keep_forks=False) -> int`; `route(payload, cfg, cwd, claude_home, *, has_session, codex_ok) -> dict | None`; `SubagentsConfig(route, model, context, fanout_feature, keep_forks)` — used identically across Tasks 1–6.
