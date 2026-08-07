# Per-Harness Startup Args Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let `~/.tandem/config.toml` declare per-harness `args` lists (e.g. `[claude] args = ["--dangerously-skip-permissions"]`) that tandem appends to every interactive launch of that harness.

**Architecture:** A new `load_harness_args()` reader in `config.py` (same lenient never-break-a-launch philosophy as the existing `[subagents]` loader), wired into `InteractiveRunner.run()` between the adapter's own launch args and tandem's hook extras. Adapters stay config-unaware. Spec: `docs/specs/2026-08-06-harness-startup-args-design.md`.

**Tech Stack:** Python 3.11+ (`tomllib`), pytest via `uv run pytest`. No new dependencies.

## Global Constraints

- Config parsing must never raise: missing file, unreadable TOML, absent table, non-list `args`, or any non-string element all yield `[]`.
- Args apply only to interactive launches (`InteractiveRunner`). `tandem run` (`oneoff_argv`), `tandem sub`, and doctor probes are untouched.
- Argv order: adapter launch args, then user args, then `hook_argv_extra` output.
- Commit messages end with `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.

---

### Task 1: `load_harness_args` in config.py

**Files:**
- Modify: `src/tandem/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: `paths.tandem_home()` (already imported in `config.py`).
- Produces: `load_harness_args(harness: str) -> list[str]` — reads `[<harness>] args` from `$TANDEM_HOME/config.toml`; returns `[]` on any problem. Task 2 imports it from `tandem.config`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_config.py` (existing tests there show the pattern: point `TANDEM_HOME` at a tmp dir, write `config.toml`, assert):

```python
def test_harness_args_reads_lists(tmp_path, monkeypatch):
    home = tmp_path / ".tandem"
    home.mkdir()
    (home / "config.toml").write_text(
        '[claude]\nargs = ["--dangerously-skip-permissions"]\n\n'
        '[codex]\nargs = ["--dangerously-bypass-approvals-and-sandbox"]\n'
    )
    monkeypatch.setenv("TANDEM_HOME", str(home))
    assert load_harness_args("claude") == ["--dangerously-skip-permissions"]
    assert load_harness_args("codex") == [
        "--dangerously-bypass-approvals-and-sandbox"
    ]


def test_harness_args_empty_when_file_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("TANDEM_HOME", str(tmp_path / ".tandem"))
    assert load_harness_args("claude") == []


def test_harness_args_invalid_shapes_fall_back(tmp_path, monkeypatch):
    home = tmp_path / ".tandem"
    home.mkdir()
    monkeypatch.setenv("TANDEM_HOME", str(home))
    cases = (
        '[claude]\nargs = "--not-a-list"\n',      # scalar, not a list
        '[claude]\nargs = ["--ok", 7]\n',         # non-string element
        '[subagents]\nroute = "manual"\n',        # table absent entirely
        "[claude\nnot toml",                      # broken TOML
    )
    for body in cases:
        (home / "config.toml").write_text(body)
        assert load_harness_args("claude") == [], body
```

Update the import at the top of the file:

```python
from tandem.config import SubagentsConfig, load_harness_args, load_subagents_config
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_config.py -v`
Expected: FAIL at import time with `ImportError: cannot import name 'load_harness_args'`

- [ ] **Step 3: Implement `load_harness_args`**

In `src/tandem/config.py`, append at the end of the file:

```python
def load_harness_args(harness: str) -> list[str]:
    """`args` from the [claude] / [codex] table: extra argv appended to
    every interactive launch of that harness. Anything malformed -> []."""
    try:
        with open(paths.tandem_home() / "config.toml", "rb") as f:
            data = tomllib.load(f)
    except (OSError, ValueError):
        return []
    table = data.get(harness)
    args = table.get("args") if isinstance(table, dict) else None
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        return []
    return args
```

Replace the module docstring (it currently claims `[subagents]` is the only table):

```python
"""User configuration: $TANDEM_HOME/config.toml.

[subagents] controls codex subagent routing; [claude] / [codex] hold an
`args` list appended to every interactive launch of that harness.

Unknown keys are ignored and every error yields defaults — configuration
must never be the reason a launch breaks or subagent routing stops (the
hook's failure mode is 'dispatch natively', and this module upholds it)."""
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_config.py -v`
Expected: all PASS (new tests plus the six pre-existing ones)

- [ ] **Step 5: Commit**

```bash
git add src/tandem/config.py tests/test_config.py
git commit -m "feat: read per-harness startup args from config.toml

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: Wire args into interactive launches

**Files:**
- Modify: `src/tandem/runner.py` (import block at ~line 18; argv build at ~line 191 in `InteractiveRunner.run()`)
- Create: `tests/test_runner.py`

**Interfaces:**
- Consumes: `load_harness_args(harness: str) -> list[str]` from `tandem.config` (Task 1); the `env_factory` fixture from `tests/conftest.py` (paired session under tmp homes, `TANDEM_HOME` already pointed at `tmp_path/.tandem`).
- Produces: interactive argv shape `[binary, <session flags>, <user args>, <hook extras>]`. No new exports.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_runner.py`:

```python
"""InteractiveRunner: user-configured [harness] args land in the spawned argv."""

from tandem import paths, runner


class _Sink:
    def handle(self, line, ctx, cursor): ...

    def close(self): ...


def _run_capturing_argv(env, monkeypatch):
    calls = {}
    monkeypatch.setattr(
        runner, "run_in_pty",
        lambda argv, cwd=None: calls.update(argv=argv) or 0,
    )
    code = runner.InteractiveRunner(
        env.session, lambda store, session, source: _Sink()).run()
    assert code == 0
    return calls["argv"]


def test_claude_resume_gets_args_before_hook_extras(env_factory, monkeypatch):
    env = env_factory(active="claude")
    (paths.tandem_home() / "config.toml").write_text(
        '[claude]\nargs = ["--dangerously-skip-permissions"]\n'
    )
    argv = _run_capturing_argv(env, monkeypatch)
    i = argv.index("--resume")
    assert argv[i + 1] == env.session.claude_session_id
    assert argv[i + 2] == "--dangerously-skip-permissions"
    assert "--settings" in argv[i + 3:]  # hook extras still last


def test_claude_fresh_launch_gets_args(env_factory, monkeypatch):
    env = env_factory(active="claude")
    env.claude_shadow.unlink()  # no transcript -> fresh --session-id launch
    (paths.tandem_home() / "config.toml").write_text(
        '[claude]\nargs = ["--dangerously-skip-permissions"]\n'
    )
    argv = _run_capturing_argv(env, monkeypatch)
    i = argv.index("--session-id")
    assert argv[i + 2] == "--dangerously-skip-permissions"


def test_codex_gets_its_own_args(env_factory, monkeypatch):
    env = env_factory(active="codex")
    (paths.tandem_home() / "config.toml").write_text(
        '[codex]\nargs = ["--dangerously-bypass-approvals-and-sandbox"]\n\n'
        '[claude]\nargs = ["--should-not-appear"]\n'
    )
    argv = _run_capturing_argv(env, monkeypatch)
    i = argv.index("resume")
    assert argv[i + 1] == env.session.codex_session_id
    assert argv[i + 2] == "--dangerously-bypass-approvals-and-sandbox"
    assert "--should-not-appear" not in argv


def test_no_config_leaves_argv_unchanged(env_factory, monkeypatch):
    env = env_factory(active="claude")
    argv = _run_capturing_argv(env, monkeypatch)
    assert argv[:3] == ["claude", "--resume", env.session.claude_session_id]
    assert argv[3] == "--settings"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_runner.py -v`
Expected: the three args tests FAIL (configured flag absent from argv, e.g. `assert argv[i + 2] == "--dangerously-skip-permissions"` sees `--settings`); `test_no_config_leaves_argv_unchanged` already PASSES.

- [ ] **Step 3: Wire in the args**

In `src/tandem/runner.py`, add to the intra-package import block:

```python
from .config import load_harness_args
```

In `InteractiveRunner.run()`, the argv build currently reads:

```python
        argv = adapter.interactive_argv(active_sid, fresh)
        argv += adapter.hook_argv_extra(sentinel)
```

Insert the user args between the two lines:

```python
        argv = adapter.interactive_argv(active_sid, fresh)
        argv += load_harness_args(active)
        argv += adapter.hook_argv_extra(sentinel)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_runner.py -v`
Expected: all 4 PASS

- [ ] **Step 5: Run the full suite**

Run: `uv run pytest`
Expected: PASS (no pre-existing test asserts the old argv shape end-to-end; this confirms it)

- [ ] **Step 6: Commit**

```bash
git add src/tandem/runner.py tests/test_runner.py
git commit -m "feat: append configured per-harness args to interactive launches

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: Document the tables in the README

**Files:**
- Modify: `README.md` (the `~/.tandem/config.toml` section, after the `[subagents]` code block and its trailing paragraph)

**Interfaces:**
- Consumes: behavior fixed by Tasks 1–2 (interactive-only, lenient parsing).
- Produces: user-facing docs only.

- [ ] **Step 1: Add the per-harness section**

After the paragraph that ends "...so `tandem doctor` warns until you set it." add:

````markdown
Optional per-harness tables add flags to every interactive session tandem
opens (`tandem`, `tandem resume`) — one-off relays (`tandem run`),
subagent dispatch, and doctor probes are unaffected:

```toml
[claude]
args = ["--dangerously-skip-permissions"]

[codex]
args = ["--dangerously-bypass-approvals-and-sandbox"]
```

The flags shown disable the harnesses' own permission prompts for
sessions tandem launches — set them only if that is what you want.
````

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "docs: per-harness startup args in the README config section

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```
