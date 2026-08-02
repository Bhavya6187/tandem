# Sandbox Consent & Manual Routing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Codex subagent dispatches inherit write-consent from the dispatching Claude session's permission mode, report sandbox-blocked writes in a structured way, and become explicitly selectable via a user-facing `tandem:gpt` agent and a `route = "manual"` config mode.

**Architecture:** Four additive features on the existing hook→relay→`tandem sub`→`codex exec` pipeline. (A) `tandem hook-route` maps the hook payload's `permission_mode` to a codex sandbox choice and stamps it under `$TANDEM_HOME/sandbox/<tandem_id>`; `tandem sub` reads the stamp (or an explicit `--sandbox` flag) and passes `--sandbox` to `codex exec`. (B) After a quiet run, `run_sub` scans the sub-rollout for `event_msg/patch_apply_end` events with `success: false` and appends a structured `[tandem-sub blocked: write]` footer to stdout so the orchestrating session gets a machine-recognizable signal instead of prose. (C) A `plugin/agents/gpt.md` alias agent makes GPT dispatch user-selectable; the hook's loop guard learns to skip it. (D) `route = "manual"` disables auto-reroute (and the missed-reroute notice) while explicit bridge dispatches keep working.

**Tech Stack:** Python 3.12, click, pytest (run via `uv run pytest`), Claude Code plugin (markdown agents + hooks.json — no hooks.json changes needed).

## Global Constraints

- Failure discipline (repo-wide, load-bearing): `tandem hook-route` ALWAYS exits 0; any internal error degrades to native dispatch. Config errors yield defaults (`config.py` module docstring). New sandbox plumbing must never be the reason a dispatch or `tandem sub` run fails: all stamp I/O is best-effort `try/except OSError`.
- The task brief never reaches argv (untrusted text; stdin only). The `--sandbox` value is NEVER taken from the brief.
- Only `"workspace-write"` and `"read-only"` are ever passed to codex. `danger-full-access` is deliberately unsupported.
- Permission modes that map to write access: exactly `acceptEdits` and `bypassPermissions`. All other modes (`default`, `plan`, unknown/missing) map to no flag (codex's configured default, read-only in practice). Unknown modes MUST degrade to no-write.
- `plugin/.claude-plugin/plugin.json` version must equal `pyproject.toml` version (`test_plugin_version_matches_pyproject_version`); both bump 0.1.5 → 0.1.6 in the final task.
- Comment style: comments state constraints the code can't show (see existing `hookroute.py`/`ops.py`); match it.
- Commit style: `feat:`/`fix:`/`docs:` prefixes, imperative mood (see `git log`).

## File Structure

- `src/tandem/hookroute.py` — add `sandbox_for_mode()` (pure mapping), `WRITE_MODES`, `ALIAS_NAME`/`RELAY_NAMES` loop-guard set.
- `src/tandem/cli.py` — `hook_route_cmd` stamps the sandbox; `sub` grows `--sandbox` and stamp-read fallback; new `_sandbox_stamp_path`/`_stamp_sandbox`/`_read_sandbox_stamp` helpers next to the warn-stamp helpers.
- `src/tandem/ops.py` — `run_sub(sandbox=...)` argv plumbing; `blocked_write_paths()` detector; `blocked_footer()` + quiet-mode footer printing.
- `src/tandem/config.py` — `_ROUTES` grows `"manual"`.
- `plugin/agents/codex-worker.md` — blocked-footer relay rule + sanctioned `--sandbox workspace-write` retry form.
- `plugin/agents/gpt.md` — new user-facing alias agent (same relay protocol).
- `tests/test_hookroute.py`, `tests/test_sub.py`, `tests/test_config.py`, `tests/test_plugin.py` — extended.
- `README.md`, `pyproject.toml`, `plugin/.claude-plugin/plugin.json` — docs + version bump.

---

### Task 1: `sandbox_for_mode()` — pure permission-mode → sandbox mapping

**Files:**
- Modify: `src/tandem/hookroute.py` (add after `BRIDGE_MODEL` constant, line ~22)
- Test: `tests/test_hookroute.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `WRITE_MODES: frozenset[str]`, `sandbox_for_mode(permission_mode) -> str` returning `"workspace-write"` or `""`. Task 2 calls `sandbox_for_mode(payload.get("permission_mode"))`.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_hookroute.py`:

```python
class TestSandboxForMode:
    def test_edit_consenting_modes_map_to_workspace_write(self):
        from tandem.hookroute import sandbox_for_mode
        assert sandbox_for_mode("acceptEdits") == "workspace-write"
        assert sandbox_for_mode("bypassPermissions") == "workspace-write"

    def test_everything_else_maps_to_no_flag(self):
        # unknown/future modes MUST degrade to no-write: an unrecognized
        # string is not consent
        from tandem.hookroute import sandbox_for_mode
        for mode in ("default", "plan", "dontAsk", "auto", "", None, 7):
            assert sandbox_for_mode(mode) == ""
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_hookroute.py::TestSandboxForMode -v`
Expected: FAIL with `ImportError: cannot import name 'sandbox_for_mode'`

- [ ] **Step 3: Implement** — in `src/tandem/hookroute.py`, after `BRIDGE_MODEL = "haiku"`:

```python
# Write-consent propagation: these are the two claude permission modes in
# which the user has already said "apply edits without asking". Every other
# value — default, plan, and any mode added after this list was written —
# maps to "" (codex keeps its configured default, read-only in practice):
# an unrecognized mode is not consent.
WRITE_MODES = frozenset({"acceptEdits", "bypassPermissions"})


def sandbox_for_mode(permission_mode) -> str:
    """The codex --sandbox value a dispatch from this claude permission
    mode has consented to, or "" for 'pass no flag'."""
    return "workspace-write" if permission_mode in WRITE_MODES else ""
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_hookroute.py::TestSandboxForMode -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add src/tandem/hookroute.py tests/test_hookroute.py
git commit -m "feat: map claude permission modes to a codex sandbox choice"
```

---

### Task 2: hook-route stamps the sandbox choice per pair

**Files:**
- Modify: `src/tandem/cli.py` (`hook_route_cmd` body at line ~429, new helpers after `_mark_warned` at line ~404)
- Test: `tests/test_hookroute.py`

**Interfaces:**
- Consumes: `sandbox_for_mode` from Task 1; existing `paths.tandem_home()`, `store.latest_session_for_cwd(cwd)`.
- Produces: stamp file `$TANDEM_HOME/sandbox/<tandem_id>` whose content is `"workspace-write"` or `""`, rewritten on EVERY Agent/Task hook firing (so it always reflects the dispatching session's current mode). Task 3 reads it via `_read_sandbox_stamp(tandem_id)` (also added here so the pair of helpers lands together).

- [ ] **Step 1: Write the failing tests** — append to `tests/test_hookroute.py` (uses the existing `env_factory` conftest fixture, which sets `TANDEM_HOME` and creates a paired session for `env.cwd`):

```python
class TestSandboxStamp:
    def _hook(self, env, mode):
        payload = _payload()
        payload["cwd"] = env.cwd
        payload["session_id"] = "s-1"
        if mode is not None:
            payload["permission_mode"] = mode
        return _run_hook(payload)

    def _stamp(self, env):
        return (paths.tandem_home() / "sandbox" / env.session.tandem_id)

    def test_accept_edits_stamps_workspace_write(self, env_factory):
        env = env_factory(active="claude")
        r = self._hook(env, "acceptEdits")
        assert r.exit_code == 0
        assert self._stamp(env).read_text() == "workspace-write"
        # the rewrite decision itself is unchanged by stamping
        out = json.loads(r.output)
        assert out["hookSpecificOutput"]["updatedInput"]["subagent_type"] \
            == BRIDGE_AGENT

    def test_default_mode_restamps_empty(self, env_factory):
        # a later default-mode dispatch must revoke an earlier consent
        env = env_factory(active="claude")
        self._hook(env, "acceptEdits")
        self._hook(env, "default")
        assert self._stamp(env).read_text() == ""

    def test_missing_mode_stamps_empty(self, env_factory):
        env = env_factory(active="claude")
        self._hook(env, None)
        assert self._stamp(env).read_text() == ""

    def test_no_session_writes_no_stamp(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TANDEM_HOME", str(tmp_path / ".tandem"))
        payload = _payload()
        payload["cwd"] = str(tmp_path)          # no paired session here
        payload["permission_mode"] = "acceptEdits"
        assert _run_hook(payload).exit_code == 0
        assert not (tmp_path / ".tandem" / "sandbox").exists()

    def test_unwritable_stamp_dir_does_not_break_the_dispatch(
            self, env_factory):
        # failure discipline: stamp I/O must never block a reroute
        env = env_factory(active="claude")
        (paths.tandem_home() / "sandbox").write_text("not a directory")
        r = self._hook(env, "acceptEdits")
        assert r.exit_code == 0
        assert json.loads(r.output)["hookSpecificOutput"]["updatedInput"][
            "subagent_type"] == BRIDGE_AGENT
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_hookroute.py::TestSandboxStamp -v`
Expected: FAIL — stamp file never created (`FileNotFoundError` on `read_text`)

- [ ] **Step 3: Implement** — in `src/tandem/cli.py`.

Add helpers after `_mark_warned` (line ~404):

```python
def _sandbox_stamp_path(tandem_id: str) -> Path:
    # tandem_id comes from our own state store (hex), never from payload
    # text, so it is safe as a filename component without filtering.
    return paths.tandem_home() / "sandbox" / tandem_id


def _stamp_sandbox(tandem_id: str, value: str) -> None:
    """Record the dispatching session's current write-consent for this pair.
    Rewritten on every dispatch so a mode change (including back to default)
    always wins; best-effort, because no stamp failure may reach the
    dispatch. Known race: two claude sessions dispatching on the same pair
    interleave last-write-wins; the window is the relay's spawn time."""
    try:
        p = _sandbox_stamp_path(tandem_id)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(value)
    except OSError:
        pass


def _read_sandbox_stamp(tandem_id: str) -> str:
    """The stamped consent, filtered to the one value we ever act on —
    anything unexpected (corrupt file, hand-edited) degrades to no flag."""
    try:
        text = _sandbox_stamp_path(tandem_id).read_text().strip()
    except OSError:
        return ""
    return text if text == "workspace-write" else ""
```

In `hook_route_cmd`, extend the import line and stamp after session resolution:

```python
        from .config import load_subagents_config
        from .hookroute import missed_reroute_notice, route, sandbox_for_mode

        payload = json.loads(sys.stdin.read() or "{}")
        cwd = payload.get("cwd") or _cwd()
        cfg = load_subagents_config()
        with StateStore() as store:
            session = store.latest_session_for_cwd(cwd)
        # Consent travels out-of-band: the relay's `tandem sub` reads this
        # stamp, so it must be current before the dispatch spawns the relay.
        # Stamped regardless of route config — a manual tandem:gpt dispatch
        # (never rewritten below) consents via permission mode all the same.
        if session is not None and payload.get("tool_name") in ("Agent", "Task"):
            _stamp_sandbox(session.tandem_id,
                           sandbox_for_mode(payload.get("permission_mode")))
```

(The rest of the function body is unchanged.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_hookroute.py -v`
Expected: PASS (all, including pre-existing)

- [ ] **Step 5: Commit**

```bash
git add src/tandem/cli.py tests/test_hookroute.py
git commit -m "feat: hook-route stamps write-consent per pair from permission_mode"
```

---

### Task 3: `tandem sub --sandbox` + stamp fallback + codex argv plumbing

**Files:**
- Modify: `src/tandem/ops.py` (`run_sub`, line ~284), `src/tandem/cli.py` (`sub` command, line ~311)
- Test: `tests/test_sub.py`

**Interfaces:**
- Consumes: `_read_sandbox_stamp(tandem_id)` from Task 2.
- Produces: `run_sub(..., sandbox: str = "")` — when truthy, appends `["--sandbox", sandbox]` to the exec-level argv (before `resume`). `tandem sub` option `--sandbox` with choices `read-only`/`workspace-write`; precedence: explicit flag > stamp > nothing. Task 4 reads `run_sub`'s `sandbox` parameter to decide the retry hint.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_sub.py`:

```python
class TestSandboxPlumbing:
    def test_run_sub_passes_sandbox_before_resume(self, env_factory,
                                                  monkeypatch):
        env = env_factory(active="claude")
        calls = {}
        monkeypatch.setattr(
            ops, "_run",
            lambda argv, cwd=None, **kw: calls.update(argv=argv) or _R(0),
        )
        ops.run_sub(env.store, env.session, "t", sandbox="workspace-write")
        argv = calls["argv"]
        i = argv.index("--sandbox")
        assert argv[i + 1] == "workspace-write"
        assert i < argv.index("resume")   # exec-level flag, like -m and -o

    def test_run_sub_default_omits_sandbox(self, env_factory, monkeypatch):
        env = env_factory(active="claude")
        calls = {}
        monkeypatch.setattr(
            ops, "_run",
            lambda argv, cwd=None, **kw: calls.update(argv=argv) or _R(0),
        )
        ops.run_sub(env.store, env.session, "t")
        assert "--sandbox" not in calls["argv"]

    def test_sub_cli_flag_forwards(self, env_factory, monkeypatch):
        import click.testing
        env = env_factory(active="claude")
        cli = TestSubCli()._cli_env(env, monkeypatch)
        calls = {}
        monkeypatch.setattr(
            ops, "run_sub",
            lambda store, session, task, **kw: calls.update(kw=kw) or 0,
        )
        r = click.testing.CliRunner().invoke(
            cli.main, ["sub", "--sandbox", "workspace-write", "brief"])
        assert r.exit_code == 0
        assert calls["kw"]["sandbox"] == "workspace-write"

    def test_sub_cli_reads_stamp_when_no_flag(self, env_factory, monkeypatch):
        import click.testing
        env = env_factory(active="claude")
        cli = TestSubCli()._cli_env(env, monkeypatch)
        stamp = paths.tandem_home() / "sandbox" / env.session.tandem_id
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.write_text("workspace-write")
        calls = {}
        monkeypatch.setattr(
            ops, "run_sub",
            lambda store, session, task, **kw: calls.update(kw=kw) or 0,
        )
        r = click.testing.CliRunner().invoke(cli.main, ["sub", "brief"])
        assert r.exit_code == 0
        assert calls["kw"]["sandbox"] == "workspace-write"

    def test_sub_cli_flag_beats_stamp(self, env_factory, monkeypatch):
        import click.testing
        env = env_factory(active="claude")
        cli = TestSubCli()._cli_env(env, monkeypatch)
        stamp = paths.tandem_home() / "sandbox" / env.session.tandem_id
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.write_text("workspace-write")
        calls = {}
        monkeypatch.setattr(
            ops, "run_sub",
            lambda store, session, task, **kw: calls.update(kw=kw) or 0,
        )
        r = click.testing.CliRunner().invoke(
            cli.main, ["sub", "--sandbox", "read-only", "brief"])
        assert r.exit_code == 0
        assert calls["kw"]["sandbox"] == "read-only"

    def test_sub_cli_garbage_stamp_degrades_to_no_flag(self, env_factory,
                                                       monkeypatch):
        import click.testing
        env = env_factory(active="claude")
        cli = TestSubCli()._cli_env(env, monkeypatch)
        stamp = paths.tandem_home() / "sandbox" / env.session.tandem_id
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.write_text("danger-full-access")   # never act on this
        calls = {}
        monkeypatch.setattr(
            ops, "run_sub",
            lambda store, session, task, **kw: calls.update(kw=kw) or 0,
        )
        r = click.testing.CliRunner().invoke(cli.main, ["sub", "brief"])
        assert r.exit_code == 0
        assert calls["kw"]["sandbox"] == ""
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_sub.py::TestSandboxPlumbing -v`
Expected: FAIL — `run_sub() got an unexpected keyword argument 'sandbox'` / `no such option: --sandbox`

- [ ] **Step 3: Implement.**

In `src/tandem/ops.py`, `run_sub` signature gains `sandbox: str = ""` (after `fanout_feature`), and after the `fanout_feature` argv block (line ~316):

```python
    if fanout_feature:
        argv += ["--enable", fanout_feature]
    if sandbox:
        # exec-level flag: must precede the `resume` subcommand, like -m.
        # Value is caller-validated ("read-only"/"workspace-write"); the
        # brief can never influence it (stdin-only transport).
        argv += ["--sandbox", sandbox]
```

In `src/tandem/cli.py`, `sub` command — add the option after `-q/--quiet` and thread it through:

```python
@click.option("--sandbox", "sandbox",
              type=click.Choice(["read-only", "workspace-write"]),
              default=None,
              help="Codex sandbox for this worker. Default: the consent the "
                   "dispatching claude session stamped via its permission "
                   "mode, else codex's configured default.")
```

```python
def sub(model: str | None, context_mode: str | None, quiet: bool,
        sandbox: str | None, task: str | None) -> None:
```

and inside, after `session = _require_session(store)`:

```python
        code = ops.run_sub(
            store, session, task,
            model=model if model is not None else cfg.model,
            context=context_mode or ("full" if cfg.context == "full" else "task"),
            fanout_feature=cfg.fanout_feature,
            keep_forks=cfg.keep_forks,
            quiet=quiet,
            sandbox=sandbox if sandbox is not None
                    else _read_sandbox_stamp(session.tandem_id),
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_sub.py -v`
Expected: PASS (all, including pre-existing)

- [ ] **Step 5: Commit**

```bash
git add src/tandem/ops.py src/tandem/cli.py tests/test_sub.py
git commit -m "feat: tandem sub --sandbox flag with stamped-consent fallback"
```

---

### Task 4: blocked-write detection + structured escalation footer

**Files:**
- Modify: `src/tandem/ops.py` (`run_sub` finally block line ~354, `_relay_last_message` neighborhood), `plugin/agents/codex-worker.md`
- Test: `tests/test_sub.py`, `tests/test_plugin.py`

**Interfaces:**
- Consumes: `run_sub`'s `sandbox` param (Task 3); `read_jsonl` from `tandem.util`; sub rollout path `sub_path` (already in scope in `run_sub`).
- Produces: `BLOCKED_HEADER = "[tandem-sub blocked: write]"`; `blocked_write_paths(sub_path) -> list[str]`; `blocked_footer(rejected: list[str], *, retry_hint: bool) -> str`. Footer appended to stdout ONLY in quiet mode, after the relayed message, before disposal. Exit code stays codex's.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_sub.py`:

```python
class TestBlockedWriteFooter:
    def _fake_run_blocked(self, last_text="Could not write the file."):
        def fake_run(argv, cwd=None, **kw):
            Path(argv[argv.index("-o") + 1]).write_text(last_text)
            sub_path = paths.find_codex_rollout(
                argv[argv.index("resume") + 1])
            write_line(sub_path, {
                "timestamp": "t", "type": "event_msg",
                "payload": {"type": "patch_apply_end", "stdout": "",
                            "success": False,
                            "changes": {"plugin/README.md": {"type": "add"}}},
            })
            return _R(0)
        return fake_run

    def test_quiet_blocked_write_appends_structured_footer(
            self, env_factory, monkeypatch, capsys):
        env = env_factory(active="claude")
        monkeypatch.setattr(ops, "_run", self._fake_run_blocked())
        code = ops.run_sub(env.store, env.session, "t", quiet=True)
        assert code == 0                       # exit code stays codex's
        out = capsys.readouterr().out
        assert "Could not write the file." in out
        assert out.index("Could not write") < out.index(ops.BLOCKED_HEADER)
        assert "plugin/README.md" in out
        assert "--sandbox workspace-write" in out   # the retry hint

    def test_retry_hint_suppressed_when_already_workspace_write(
            self, env_factory, monkeypatch, capsys):
        # blocked even with write access => the hint would be a lie
        env = env_factory(active="claude")
        monkeypatch.setattr(ops, "_run", self._fake_run_blocked())
        ops.run_sub(env.store, env.session, "t", quiet=True,
                    sandbox="workspace-write")
        out = capsys.readouterr().out
        assert ops.BLOCKED_HEADER in out
        assert "--sandbox workspace-write`" not in out.split(
            ops.BLOCKED_HEADER)[1]

    def test_successful_patches_emit_no_footer(self, env_factory,
                                               monkeypatch, capsys):
        env = env_factory(active="claude")

        def fake_run(argv, cwd=None, **kw):
            Path(argv[argv.index("-o") + 1]).write_text("done")
            sub_path = paths.find_codex_rollout(
                argv[argv.index("resume") + 1])
            write_line(sub_path, {
                "timestamp": "t", "type": "event_msg",
                "payload": {"type": "patch_apply_end", "stdout": "ok",
                            "success": True,
                            "changes": {"a.py": {"type": "update"}}},
            })
            return _R(0)

        monkeypatch.setattr(ops, "_run", fake_run)
        ops.run_sub(env.store, env.session, "t", quiet=True)
        assert ops.BLOCKED_HEADER not in capsys.readouterr().out

    def test_non_quiet_never_prints_footer(self, env_factory, monkeypatch,
                                           capsys):
        # manual runs stream codex's own output; the footer is bridge
        # protocol, not user chrome
        env = env_factory(active="claude")
        monkeypatch.setattr(ops, "_run", self._fake_run_blocked())
        ops.run_sub(env.store, env.session, "t")
        assert ops.BLOCKED_HEADER not in capsys.readouterr().out

    def test_unreadable_rollout_degrades_to_no_footer(self, env_factory,
                                                      monkeypatch, capsys):
        env = env_factory(active="claude")

        def fake_run(argv, cwd=None, **kw):
            Path(argv[argv.index("-o") + 1]).write_text("fine")
            sub_path = paths.find_codex_rollout(
                argv[argv.index("resume") + 1])
            sub_path.write_text("not json {{{\n")
            return _R(0)

        monkeypatch.setattr(ops, "_run", fake_run)
        code = ops.run_sub(env.store, env.session, "t", quiet=True)
        assert code == 0
        out = capsys.readouterr().out
        assert "fine" in out
        assert ops.BLOCKED_HEADER not in out
```

Note: if `tandem.util.read_jsonl` raises on malformed lines rather than skipping them, wrap the detector's read in `try/except` (see Step 3) — the last test pins that contract.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_sub.py::TestBlockedWriteFooter -v`
Expected: FAIL with `AttributeError: module 'tandem.ops' has no attribute 'BLOCKED_HEADER'`

- [ ] **Step 3: Implement** — in `src/tandem/ops.py`.

Constants + helpers near `_relay_last_message` (line ~371):

```python
# The bridge-protocol marker for "codex finished but the sandbox rejected
# its writes". The orchestrating session matches on this exact line, so it
# is public API: change it and every dispatching model's instructions rot.
BLOCKED_HEADER = "[tandem-sub blocked: write]"


def blocked_write_paths(sub_path: Path) -> list[str]:
    """Paths whose apply_patch the sandbox rejected during this run, in
    event order. Codex records each attempt as event_msg/patch_apply_end
    with a success flag (docs/formats.md); anything unreadable or
    unexpected yields [] — detection is advisory, never load-bearing."""
    try:
        entries = read_jsonl(sub_path)
    except Exception:
        return []
    rejected: list[str] = []
    for e in entries:
        if not isinstance(e, dict) or e.get("type") != "event_msg":
            continue
        p = e.get("payload")
        if not isinstance(p, dict) or p.get("type") != "patch_apply_end":
            continue
        if p.get("success"):
            continue
        changes = p.get("changes")
        if isinstance(changes, dict) and changes:
            rejected += [c for c in changes if c not in rejected]
        elif "(unknown path)" not in rejected:
            rejected.append("(unknown path)")
    return rejected


def blocked_footer(rejected: list[str], *, retry_hint: bool) -> str:
    lines = [
        BLOCKED_HEADER,
        "The codex worker's file changes were rejected by its sandbox; "
        "no files were modified. Rejected: " + ", ".join(rejected[:10]),
    ]
    if retry_hint:
        lines.append(
            "To grant writes: message this worker to rerun the same task "
            "with `tandem sub -q --sandbox workspace-write`, or ask it to "
            "return the content and apply the changes yourself.")
    return "\n".join(lines) + "\n"
```

Add the `read_jsonl` import to the existing `tandem.util` import line in `ops.py` (check the top of the file; if `ops.py` has no util import yet, add `from .util import read_jsonl` alongside the existing relative imports).

In `run_sub`'s `finally` block, after `_relay_last_message(...)` and before `marker.unlink(...)`:

```python
    finally:
        # The worker's answer is what this run exists to produce; disposal is
        # bookkeeping. Relay first so a raising cleanup cannot swallow a result
        # codex already produced and billed for.
        if quiet:
            _relay_last_message(last_path, log_path)
            # Bridge protocol: turn "sandbox rejected the writes" from prose
            # buried in the answer into a fixed trailer the orchestrating
            # session can match on. Must read sub_path before disposal below.
            rejected = blocked_write_paths(sub_path)
            if rejected:
                sys.stdout.write(blocked_footer(
                    rejected, retry_hint=sandbox != "workspace-write"))
                sys.stdout.flush()
        marker.unlink(missing_ok=True)
```

In `plugin/agents/codex-worker.md`, append after step 3:

```markdown
4. The output may end with a `[tandem-sub blocked: write]` trailer: codex
   finished, but its sandbox rejected the file changes. Return the whole
   output verbatim as usual — the trailer is for the session that
   dispatched you, not for you to act on.

5. The ONE exception to the fixed command form: if the session that
   dispatched you sends a follow-up message telling you to retry with
   write access, run the SAME heredoc command again with the flag added:
   `tandem sub -q --sandbox workspace-write <<'TANDEM_TASK_EOF' ...`.
   Never add that flag on your own initiative.
```

Append to `tests/test_plugin.py::test_bridge_agent_definition`:

```python
    assert "[tandem-sub blocked: write]" in body
    assert "--sandbox workspace-write" in body
    assert re.search(r"[Nn]ever add that flag", body)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_sub.py tests/test_plugin.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/tandem/ops.py plugin/agents/codex-worker.md tests/test_sub.py tests/test_plugin.py
git commit -m "feat: structured [tandem-sub blocked: write] trailer for sandbox-rejected writes"
```

---

### Task 5: `route = "manual"` config value

**Files:**
- Modify: `src/tandem/config.py` (lines 17, 24)
- Test: `tests/test_config.py`, `tests/test_hookroute.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `"manual"` accepted by `_ROUTES`. Semantics (all already emergent from `route()`/`missed_reroute_notice()` checking `cfg.route != "all"`): no auto-rewrite, no missed-reroute notice; explicit bridge dispatches pass through; sandbox stamping still happens (Task 2 stamps regardless of route).

- [ ] **Step 1: Write the failing tests.**

Append to `tests/test_config.py` (match the file's existing style — it tests `load_subagents_config` with a `TANDEM_HOME` tmp dir; follow the pattern already used there for the `route` key, e.g.):

```python
def test_route_manual_is_accepted(tmp_path, monkeypatch):
    monkeypatch.setenv("TANDEM_HOME", str(tmp_path))
    (tmp_path / "config.toml").write_text('[subagents]\nroute = "manual"\n')
    from tandem.config import load_subagents_config
    assert load_subagents_config().route == "manual"
```

Append to `tests/test_hookroute.py`:

```python
class TestManualRoute:
    def test_manual_never_rewrites(self):
        assert _route(_payload(),
                      cfg=SubagentsConfig(route="manual")) is None

    def test_manual_never_warns(self):
        # like "off": an explicit user choice, so silence is the requested
        # behavior even when nothing could reroute anyway
        cfg = SubagentsConfig(route="manual")
        assert _notice(_payload(), cfg=cfg) is None
        assert _notice(_payload(), cfg=cfg,
                       has_session=True, codex_ok=False) is None

    def test_manual_still_stamps_sandbox(self, env_factory):
        env = env_factory(active="claude")
        (paths.tandem_home() / "config.toml").write_text(
            '[subagents]\nroute = "manual"\n')
        payload = _payload()
        payload["cwd"] = env.cwd
        payload["permission_mode"] = "acceptEdits"
        r = _run_hook(payload)
        assert r.exit_code == 0
        assert r.output == ""            # no rewrite, no notice
        assert (paths.tandem_home() / "sandbox"
                / env.session.tandem_id).read_text() == "workspace-write"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_config.py::test_route_manual_is_accepted tests/test_hookroute.py::TestManualRoute -v`
Expected: `test_route_manual_is_accepted` FAILS (unlisted value degrades to default `"all"`); the hookroute tests may already pass — that's fine, they pin the semantics.

- [ ] **Step 3: Implement** — in `src/tandem/config.py`:

```python
    route: str = "all"          # "all" | "manual" | "off"
```

```python
_ROUTES = ("all", "manual", "off")
```

(`"manual"`: no auto-reroute and no missed-reroute notice — dispatch to codex only when the model/user explicitly picks a bridge agent (`tandem:gpt`, `tandem:codex-worker`). Distinct from `"off"` only in intent today; it exists so docs can point users at a supported name instead of overloading `"off"`. Add this as a comment on `_ROUTES`.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_config.py tests/test_hookroute.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/tandem/config.py tests/test_config.py tests/test_hookroute.py
git commit -m "feat: route=manual config value for explicit-only codex dispatch"
```

---

### Task 6: `tandem:gpt` user-facing alias agent + loop-guard extension

**Files:**
- Create: `plugin/agents/gpt.md`
- Modify: `src/tandem/hookroute.py` (constants line ~20, guard line ~63)
- Test: `tests/test_hookroute.py`, `tests/test_plugin.py`

**Interfaces:**
- Consumes: relay protocol text from `plugin/agents/codex-worker.md` (Task 4 version, including the blocked-trailer steps).
- Produces: `ALIAS_NAME = "gpt"`, `RELAY_NAMES = frozenset({BRIDGE_NAME, ALIAS_NAME})`; guard skips any `subagent_type` whose last `:`-segment is in `RELAY_NAMES`. Agent `tandem:gpt`, selectable by the orchestrating model when the user asks for GPT subagents.

- [ ] **Step 1: Write the failing tests.**

Append to `tests/test_hookroute.py` `TestPassthrough`:

```python
    def test_gpt_alias_loop_guard(self):
        # tandem:gpt is the user-facing door to the same relay; rewriting it
        # to codex-worker would work but erase the explicit choice from the
        # UI and transcript — and the bare name must behave identically
        assert _route(_payload(subagent_type="tandem:gpt")) is None
        assert _route(_payload(subagent_type="gpt")) is None
```

Append to `tests/test_plugin.py`:

```python
def test_gpt_alias_agent_definition():
    text = (PLUGIN / "agents" / "gpt.md").read_text()
    front = text.split("---")[1]
    assert re.search(r"^name:\s*gpt\s*$", front, re.M)
    assert re.search(r"^model:\s*haiku\s*$", front, re.M)
    assert re.search(r"^tools:\s*Bash\(tandem sub:\*\)\s*$", front, re.M)
    # user-facing: the description must invite selection (unlike
    # codex-worker's "not meant for manual selection")
    desc = re.search(r"^description:\s*(.+)$", front, re.M).group(1)
    assert "not meant for manual selection" not in desc
    assert re.search(r"GPT", desc)
    # same relay contract as codex-worker
    body = text.split("---", 2)[2]
    for marker in ("tandem sub -q", "TANDEM_TASK_EOF_", "verbatim",
                   "[tandem-sub failed]", "[tandem-sub blocked: write]",
                   "--sandbox workspace-write"):
        assert marker in body, marker
    assert re.search(r"never do the task yourself", body, re.I)


def test_loop_guard_covers_both_relay_agents():
    """Every agent under plugin/agents/ must be in hookroute's guard set:
    a plugin agent missing from RELAY_NAMES gets rewritten away from
    itself on dispatch (or worse, loops)."""
    from tandem.hookroute import RELAY_NAMES
    names = set()
    for f in (PLUGIN / "agents").glob("*.md"):
        front = f.read_text().split("---")[1]
        names.add(re.search(r"^name:\s*(\S+)\s*$", front, re.M).group(1))
    assert names == set(RELAY_NAMES)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_hookroute.py::TestPassthrough::test_gpt_alias_loop_guard tests/test_plugin.py::test_gpt_alias_agent_definition tests/test_plugin.py::test_loop_guard_covers_both_relay_agents -v`
Expected: FAIL — guard rewrites `tandem:gpt`; `gpt.md` missing; `RELAY_NAMES` missing

- [ ] **Step 3: Implement.**

`src/tandem/hookroute.py` — after `BRIDGE_AGENT`:

```python
# The user-facing door to the same relay: selectable by the orchestrating
# model when the user asks for GPT subagents (route="manual" makes that the
# only path). Same body contract as the bridge; only the description invites
# selection.
ALIAS_NAME = "gpt"
RELAY_NAMES = frozenset({BRIDGE_NAME, ALIAS_NAME})
```

and change the guard (line ~63):

```python
    if subagent_type == "fork" or \
            subagent_type.rsplit(":", 1)[-1] in RELAY_NAMES:
        return None
```

Create `plugin/agents/gpt.md` — frontmatter as below; body copied verbatim from the Task-4 version of `plugin/agents/codex-worker.md` (the two bodies are intentionally identical; `test_gpt_alias_agent_definition` pins the shared markers):

```markdown
---
name: gpt
description: Runs the task on a GPT model via tandem's codex pairing. Select this when the user asks for GPT subagents or to run something on GPT/codex.
model: haiku
tools: Bash(tandem sub:*)
---

<body: byte-for-byte copy of codex-worker.md's body, starting at "You are a relay between this session and a codex worker.">
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_hookroute.py tests/test_plugin.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add plugin/agents/gpt.md src/tandem/hookroute.py tests/test_hookroute.py tests/test_plugin.py
git commit -m "feat: user-selectable tandem:gpt alias agent"
```

---

### Task 7: docs, version bump, full-suite verification

**Files:**
- Modify: `README.md` (config block at line ~82-96, commands table line ~164), `plugin/README.md`, `pyproject.toml`, `plugin/.claude-plugin/plugin.json`

**Interfaces:**
- Consumes: everything above.
- Produces: version 0.1.6 in both manifests (equality is test-enforced); user docs for all four features.

- [ ] **Step 1: Update `README.md`.**

In the config block (line ~86-88), change the route comment and add the new pieces:

```toml
[subagents]
route = "all"           # all | manual | off
```

Below the block, add prose (match the README's existing voice):

```markdown
- `route = "manual"` turns off automatic rerouting; dispatch to codex only
  when you (or Claude) explicitly pick the `tandem:gpt` agent — e.g. say
  "use gpt subagents for this".
- Write access follows your Claude permission mode: in `acceptEdits` or
  `bypassPermissions` the codex worker runs with `--sandbox
  workspace-write`; otherwise it is read-only and file-writing tasks come
  back with a `[tandem-sub blocked: write]` trailer explaining how to
  grant access (`tandem sub -q --sandbox workspace-write`).
```

In the commands table (line ~164), update the `tandem sub` row's description to mention `--sandbox`.

- [ ] **Step 2: Update `plugin/README.md`** — add `agents/gpt.md` to the directory walk-through (one short paragraph: user-facing alias, same relay, when to select it). Verify every claim against the files as committed.

- [ ] **Step 3: Bump versions** — `pyproject.toml` `version = "0.1.6"`; `plugin/.claude-plugin/plugin.json` `"version": "0.1.6"`. (The plugin content changed; `plugin marketplace update` skips same-version plugins, so shipped fixes never reach users without this — see `test_plugin_version_matches_pyproject_version`.)

- [ ] **Step 4: Run the full suite**

Run: `uv run pytest -q`
Expected: all tests pass, no skips introduced by this work

- [ ] **Step 5: Commit**

```bash
git add README.md plugin/README.md pyproject.toml plugin/.claude-plugin/plugin.json
git commit -m "docs: sandbox consent, manual routing, tandem:gpt; release 0.1.6"
```

---

## Post-plan notes (not tasks)

- **Live E2E after merge:** plugin changes only take effect in Claude sessions started after `claude plugin update tandem@tandem` (hooks and agents register at session startup — the exact failure mode that motivated this work). Verify with one fresh-session dispatch in `acceptEdits` mode: expect reroute, codex writes a file, no blocked trailer.
- **Known accepted race (documented in `_stamp_sandbox`):** two Claude sessions dispatching on the same pair interleave last-write-wins on the stamp. Degradation is at worst one dispatch running with the other session's consent level on the same repo, same user.
- **`dontAsk`/`auto` modes deliberately unmapped** — revisit only with documented semantics in hand.
