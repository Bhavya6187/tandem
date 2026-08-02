# Plugin Auto-Install Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bare `tandem` offers (once, Y/n) to install the Claude Code plugin, and `tandem plugin install` wraps Claude's two marketplace commands into one.

**Architecture:** One new module `src/tandem/plugin_setup.py` holds detection
(read-only peek at Claude's `installed_plugins.json`), install (subprocess to
`claude plugin …`), and the one-time offer (TTY-gated prompt + stamp file
under `$TANDEM_HOME`). `cli.py` grows a `tandem plugin` group and one call
site in `_interactive()`. Spec: `docs/specs/2026-08-02-plugin-auto-install-design.md`.

**Tech Stack:** Python ≥3.11, click, pytest (`click.testing.CliRunner`,
`monkeypatch`, `capsys`). No new dependencies.

## Global Constraints

- Dependencies stay exactly `click`, `pydantic`, `watchdog`, `pexpect` — add nothing.
- Never write to any file under `~/.claude/` — Claude's CLI is the sole writer of its own state; tandem only reads `installed_plugins.json`.
- All Claude interaction is `subprocess.run` of the `claude` binary; tandem itself makes no network calls.
- Exact copy (verbatim in code):
  - prompt: `Install the tandem Claude Code plugin for codex-model subagents?` (click.confirm, `default=True` → `[Y/n]`)
  - decline hint: `You can install it later with: tandem plugin install`
  - success note: `Plugin installed. It takes effect in new Claude sessions (running sessions are unaffected).`
  - marketplace repo: `Bhavya6187/tandem`; plugin id: `tandem@tandem`
  - stamp path: `$TANDEM_HOME/plugin-offer`
- Verified against claude 2.1.220 on 2026-08-02: `claude plugin marketplace add` on an existing marketplace and `claude plugin install` on an installed plugin both exit 0 with an "already …" notice — the commands are naturally idempotent.
- Commit messages end with `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.

---

### Task 1: Detection — `is_plugin_installed()`

**Files:**
- Create: `src/tandem/plugin_setup.py`
- Create: `tests/test_plugin_setup.py`
- Modify: `src/tandem/paths.py` (one helper, after `claude_transcript_path`)
- Modify: `docs/specs/2026-08-02-plugin-auto-install-design.md` (detection nuance, below)

**Interfaces:**
- Consumes: `paths.claude_home()` (respects `CLAUDE_CONFIG_DIR`), `paths.tandem_home()` (respects `TANDEM_HOME`).
- Produces: `paths.claude_installed_plugins_path() -> Path`; `plugin_setup.is_plugin_installed() -> bool`; module constants `MARKETPLACE_REPO = "Bhavya6187/tandem"`, `PLUGIN_ID = "tandem@tandem"`.

**Spec nuance locked in here** (amend the spec's §1 second sentence to match):
a *missing* state file or an absent/empty `tandem@tandem` entry is
definitively not-installed → `False` (a fresh Claude install has no plugins
yet — that user must see the offer). Only *ambiguity* — unreadable file, bad
JSON, unexpected shape — returns `True` (stay silent, never nag).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_plugin_setup.py`:

```python
"""plugin_setup: detection reads claude state; install shells out; the
offer is TTY-gated and stamps once per machine."""

import json

import pytest

from tandem import paths, plugin_setup


@pytest.fixture
def homes(tmp_path, monkeypatch):
    monkeypatch.setenv("TANDEM_HOME", str(tmp_path / ".tandem"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / ".claude"))
    return tmp_path


def write_state(tmp_path, payload) -> None:
    p = tmp_path / ".claude" / "plugins" / "installed_plugins.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(payload if isinstance(payload, str) else json.dumps(payload))


# -- detection ---------------------------------------------------------------

def test_missing_state_file_means_not_installed(homes):
    assert plugin_setup.is_plugin_installed() is False


def test_entry_with_record_means_installed(homes):
    write_state(homes, {"version": 2, "plugins": {
        "tandem@tandem": [{"scope": "user", "version": "0.1.5"}]}})
    assert plugin_setup.is_plugin_installed() is True


def test_absent_entry_means_not_installed(homes):
    write_state(homes, {"version": 2, "plugins": {
        "other@mkt": [{"scope": "user"}]}})
    assert plugin_setup.is_plugin_installed() is False


def test_empty_record_list_means_not_installed(homes):
    write_state(homes, {"version": 2, "plugins": {"tandem@tandem": []}})
    assert plugin_setup.is_plugin_installed() is False


def test_malformed_json_reads_as_installed(homes):
    # Ambiguity must resolve to silence (True = never nag).
    write_state(homes, "{not json")
    assert plugin_setup.is_plugin_installed() is True


def test_unexpected_shape_reads_as_installed(homes):
    write_state(homes, {"version": 3, "plugins": "moved-elsewhere"})
    assert plugin_setup.is_plugin_installed() is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_plugin_setup.py -v`
Expected: FAIL — `ImportError: cannot import name 'plugin_setup'` (module doesn't exist yet).

- [ ] **Step 3: Add the paths helper**

In `src/tandem/paths.py`, directly after `claude_transcript_path` (still in the "Claude Code" section):

```python
def claude_installed_plugins_path() -> Path:
    """Claude's installed-plugin registry (observed: claude 2.1.220)."""
    return claude_home() / "plugins" / "installed_plugins.json"
```

- [ ] **Step 4: Write the minimal module**

Create `src/tandem/plugin_setup.py`:

```python
"""Install the tandem Claude Code plugin through claude's own CLI.

Detection reads claude's installed-plugin registry (read-only; claude
stays the sole writer of its own state). Install shells out to
`claude plugin …` — verified idempotent on claude 2.1.220: re-adding the
marketplace and re-installing the plugin both exit 0 with an "already"
notice. The one-time offer lives here too so both entry points (bare
`tandem` and `tandem plugin install`) share a single routine.
"""

from __future__ import annotations

import json

from . import paths

MARKETPLACE_REPO = "Bhavya6187/tandem"
PLUGIN_ID = "tandem@tandem"


def is_plugin_installed() -> bool:
    """True when claude's registry records a tandem@tandem install.

    Missing file or absent/empty entry is definitively False — a fresh
    claude install has no registry, and that user must get the offer.
    Everything ambiguous (unreadable, unparseable, unexpected shape)
    is True: the caller only decides whether to nag, and doubt must
    stay silent.
    """
    try:
        raw = paths.claude_installed_plugins_path().read_text()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    try:
        plugins = json.loads(raw)["plugins"]
        if not isinstance(plugins, dict):
            return True
        return bool(plugins.get(PLUGIN_ID))
    except Exception:
        return True
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_plugin_setup.py -v`
Expected: 6 passed.

- [ ] **Step 6: Amend the spec's detection sentence**

In `docs/specs/2026-08-02-plugin-auto-install-design.md` §1, replace the
sentence

> Any IO or parse surprise returns `True`: ambiguity means stay silent, never nag.

with

> A missing file or an absent/empty `tandem@tandem` entry is definitively
> not installed (`False` — a fresh Claude install has no registry, and that
> user must see the offer). An unreadable or unparseable file, or an
> unexpected shape, returns `True`: ambiguity means stay silent, never nag.

- [ ] **Step 7: Commit**

```bash
git add src/tandem/plugin_setup.py src/tandem/paths.py tests/test_plugin_setup.py docs/specs/2026-08-02-plugin-auto-install-design.md
git commit -m "feat: detect the tandem plugin in claude's registry

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: Install routine — `install_plugin()`

**Files:**
- Modify: `src/tandem/plugin_setup.py`
- Modify: `tests/test_plugin_setup.py` (append)

**Interfaces:**
- Consumes: `MARKETPLACE_REPO`, `PLUGIN_ID` from Task 1.
- Produces: `plugin_setup.install_plugin() -> bool`; `plugin_setup.MANUAL_COMMANDS: str` (the two claude commands, indented, newline-joined).

**Behavior:** echo each command before running it; a failed `marketplace add`
is a warning only (the install step decides success — covers both the
already-added case and transient add noise); a failed or impossible
`plugin install` prints the manual commands and returns False; success prints
the new-sessions note. `claude` missing from PATH → error + manual commands +
False, no subprocess calls.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_plugin_setup.py`:

```python
# -- install -----------------------------------------------------------------

class FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.fixture
def claude_on_path(monkeypatch):
    monkeypatch.setattr(plugin_setup.shutil, "which",
                        lambda name: "/usr/local/bin/claude")


@pytest.fixture
def recorded_runs(monkeypatch):
    """Record subprocess invocations; per-command results set via dict."""
    calls, results = [], {}

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return results.get(tuple(cmd), FakeProc())

    monkeypatch.setattr(plugin_setup.subprocess, "run", fake_run)
    return calls, results


ADD_CMD = ("claude", "plugin", "marketplace", "add", "Bhavya6187/tandem")
INSTALL_CMD = ("claude", "plugin", "install", "tandem@tandem")


def test_install_runs_add_then_install(claude_on_path, recorded_runs, capsys):
    calls, _ = recorded_runs
    assert plugin_setup.install_plugin() is True
    assert [tuple(c) for c in calls] == [ADD_CMD, INSTALL_CMD]
    out = capsys.readouterr().out
    assert "new Claude sessions" in out


def test_add_failure_is_nonfatal_when_install_succeeds(
        claude_on_path, recorded_runs, capsys):
    calls, results = recorded_runs
    results[ADD_CMD] = FakeProc(returncode=1, stderr="some marketplace noise")
    assert plugin_setup.install_plugin() is True
    assert [tuple(c) for c in calls] == [ADD_CMD, INSTALL_CMD]


def test_install_failure_prints_manual_commands(
        claude_on_path, recorded_runs, capsys):
    _, results = recorded_runs
    results[INSTALL_CMD] = FakeProc(returncode=1, stderr="boom")
    assert plugin_setup.install_plugin() is False
    err = capsys.readouterr().err
    assert "claude plugin marketplace add Bhavya6187/tandem" in err
    assert "claude plugin install tandem@tandem" in err


def test_missing_claude_binary_fails_without_running_anything(
        recorded_runs, monkeypatch, capsys):
    calls, _ = recorded_runs
    monkeypatch.setattr(plugin_setup.shutil, "which", lambda name: None)
    assert plugin_setup.install_plugin() is False
    assert calls == []
    assert "claude plugin install tandem@tandem" in capsys.readouterr().err
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_plugin_setup.py -v`
Expected: 4 new FAIL with `AttributeError: … has no attribute 'shutil'` / `'install_plugin'`; the 6 detection tests still pass.

- [ ] **Step 3: Implement `install_plugin`**

In `src/tandem/plugin_setup.py`, extend the imports and append:

```python
import shutil
import subprocess

import click
```

(top of file, merged into the existing import block: stdlib `json, shutil,
subprocess`, then `click`, then `from . import paths`)

```python
MANUAL_COMMANDS = (
    "    claude plugin marketplace add Bhavya6187/tandem\n"
    "    claude plugin install tandem@tandem"
)


def _run(cmd: list[str]) -> subprocess.CompletedProcess | None:
    """Echo-and-run one claude command; None when it cannot run at all."""
    click.echo("  $ " + " ".join(cmd))
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return None


def install_plugin() -> bool:
    """Marketplace add + plugin install through claude's CLI.

    The add step is advisory — claude 2.1.220 exits 0 when the
    marketplace is already declared, and if the add genuinely failed the
    install step fails right after and reports. Only the install step
    decides the return value.
    """
    if shutil.which("claude") is None:
        click.secho("error: claude not found on PATH.", fg="red", err=True)
        click.echo("Once it is installed, run:\n" + MANUAL_COMMANDS, err=True)
        return False
    add = _run(["claude", "plugin", "marketplace", "add", MARKETPLACE_REPO])
    if add is not None and add.returncode != 0:
        detail = (add.stderr or add.stdout).strip()
        if detail:
            click.secho(f"  marketplace add failed: {detail}",
                        fg="yellow", err=True)
    ins = _run(["claude", "plugin", "install", PLUGIN_ID])
    if ins is None or ins.returncode != 0:
        detail = "" if ins is None else (ins.stderr or ins.stdout).strip()
        if detail:
            click.secho(f"  {detail}", fg="red", err=True)
        click.secho("Plugin install failed. Manual commands:",
                    fg="red", err=True)
        click.echo(MANUAL_COMMANDS, err=True)
        return False
    click.echo(
        "Plugin installed. It takes effect in new Claude sessions "
        "(running sessions are unaffected)."
    )
    return True
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_plugin_setup.py -v`
Expected: 10 passed.

- [ ] **Step 5: Commit**

```bash
git add src/tandem/plugin_setup.py tests/test_plugin_setup.py
git commit -m "feat: one-shot plugin install through claude's CLI

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: One-time offer — `offer_install()`

**Files:**
- Modify: `src/tandem/plugin_setup.py`
- Modify: `tests/test_plugin_setup.py` (append)

**Interfaces:**
- Consumes: `is_plugin_installed()` (Task 1), `install_plugin()` (Task 2), `paths.tandem_home()`.
- Produces: `plugin_setup.offer_install() -> None`; `plugin_setup.LATER_HINT: str`; internals `_offer_stamp() -> Path`, `_stdin_is_tty() -> bool` (module-level so tests can monkeypatch).

**Behavior:** four gates, all resolving to silence — TTY, `claude` on PATH,
no stamp, not installed. A *shown* offer always stamps (`$TANDEM_HOME/plugin-offer`),
whatever the answer or install outcome; gated-out runs never stamp. Ctrl-C/EOF
at the prompt counts as decline. Stamp I/O is best-effort like the existing
`warned/` stamps in `cli.py`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_plugin_setup.py`:

```python
# -- offer -------------------------------------------------------------------

@pytest.fixture
def offerable(homes, claude_on_path, monkeypatch):
    """All four gates open: TTY, claude on PATH, no stamp, not installed."""
    monkeypatch.setattr(plugin_setup, "_stdin_is_tty", lambda: True)
    return homes


def stamp_path(tmp_path):
    return tmp_path / ".tandem" / "plugin-offer"


def test_offer_silent_when_not_tty(homes, claude_on_path, monkeypatch, capsys):
    monkeypatch.setattr(plugin_setup, "_stdin_is_tty", lambda: False)
    plugin_setup.offer_install()
    assert capsys.readouterr().out == ""
    assert not stamp_path(homes).exists()


def test_offer_silent_when_no_claude(homes, monkeypatch, capsys):
    monkeypatch.setattr(plugin_setup, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(plugin_setup.shutil, "which", lambda name: None)
    plugin_setup.offer_install()
    assert capsys.readouterr().out == ""
    assert not stamp_path(homes).exists()


def test_offer_silent_when_already_installed(offerable, capsys):
    write_state(offerable, {"version": 2, "plugins": {
        "tandem@tandem": [{"scope": "user"}]}})
    plugin_setup.offer_install()
    assert capsys.readouterr().out == ""
    assert not stamp_path(offerable).exists()


def test_offer_silent_when_stamped(offerable, monkeypatch, capsys):
    stamp_path(offerable).parent.mkdir(parents=True, exist_ok=True)
    stamp_path(offerable).touch()
    monkeypatch.setattr(plugin_setup.click, "confirm",
                        lambda *a, **k: pytest.fail("prompted despite stamp"))
    plugin_setup.offer_install()
    assert capsys.readouterr().out == ""


def test_decline_prints_hint_and_stamps(offerable, monkeypatch, capsys):
    monkeypatch.setattr(plugin_setup.click, "confirm", lambda *a, **k: False)
    plugin_setup.offer_install()
    assert plugin_setup.LATER_HINT in capsys.readouterr().out
    assert stamp_path(offerable).exists()


def test_accept_installs_and_stamps(offerable, monkeypatch):
    installed = []
    monkeypatch.setattr(plugin_setup.click, "confirm", lambda *a, **k: True)
    monkeypatch.setattr(plugin_setup, "install_plugin",
                        lambda: (installed.append(True), True)[1])
    plugin_setup.offer_install()
    assert installed == [True]
    assert stamp_path(offerable).exists()


def test_failed_install_still_stamps(offerable, monkeypatch):
    monkeypatch.setattr(plugin_setup.click, "confirm", lambda *a, **k: True)
    monkeypatch.setattr(plugin_setup, "install_plugin", lambda: False)
    plugin_setup.offer_install()
    assert stamp_path(offerable).exists()


def test_abort_at_prompt_counts_as_decline(offerable, monkeypatch, capsys):
    def raise_abort(*a, **k):
        raise plugin_setup.click.Abort()

    monkeypatch.setattr(plugin_setup.click, "confirm", raise_abort)
    plugin_setup.offer_install()
    assert plugin_setup.LATER_HINT in capsys.readouterr().out
    assert stamp_path(offerable).exists()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_plugin_setup.py -v`
Expected: 8 new FAIL with `AttributeError: … no attribute '_stdin_is_tty'`; the 10 earlier tests still pass.

- [ ] **Step 3: Implement the offer**

Append to `src/tandem/plugin_setup.py` (add `import sys` to the stdlib imports):

```python
LATER_HINT = "You can install it later with: tandem plugin install"


def _offer_stamp() -> Path:
    return paths.tandem_home() / "plugin-offer"
```

(add `from pathlib import Path` to the imports)

```python
def _stdin_is_tty() -> bool:
    try:
        return sys.stdin.isatty()
    except (ValueError, OSError):    # closed/replaced stdin
        return False


def offer_install() -> None:
    """One-time [Y/n] offer before the interactive shell.

    Every gate resolves to silence; only a *shown* offer stamps, and it
    stamps whatever the answer or install outcome — the hint names the
    retry path, so re-asking would just be nagging.
    """
    if not _stdin_is_tty():
        return
    if shutil.which("claude") is None:
        return
    stamp = _offer_stamp()
    try:
        if stamp.exists():
            return
    except OSError:
        return
    if is_plugin_installed():
        return
    try:
        accepted = click.confirm(
            "Install the tandem Claude Code plugin for codex-model "
            "subagents?",
            default=True,
        )
    except (click.Abort, EOFError):     # ctrl-C / ctrl-D = not now
        click.echo()                    # newline after the aborted prompt
        accepted = False
    if accepted:
        install_plugin()
    else:
        click.echo(LATER_HINT)
    try:
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.touch()
    except OSError:
        pass                            # best-effort, like warned/ stamps
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_plugin_setup.py -v`
Expected: 18 passed.

- [ ] **Step 5: Commit**

```bash
git add src/tandem/plugin_setup.py tests/test_plugin_setup.py
git commit -m "feat: one-time first-run offer to install the plugin

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: CLI wiring — `tandem plugin install` + the offer in bare `tandem`

**Files:**
- Modify: `src/tandem/cli.py` (new group after the `sync` command, ~line 570; one call in `_interactive`)
- Modify: `tests/test_cli.py` (append; reuse the existing `homes`, `ok_versions`, `entered` fixtures)

**Interfaces:**
- Consumes: `plugin_setup.install_plugin() -> bool` (Task 2), `plugin_setup.offer_install() -> None` (Task 3).
- Produces: `tandem plugin install` exit 0/1; the offer fires in `_interactive()` after pairing echoes, before `_enter_session`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_cli.py`:

```python
def test_plugin_install_cmd_exit_codes(monkeypatch):
    from tandem import plugin_setup

    monkeypatch.setattr(plugin_setup, "install_plugin", lambda: True)
    r = click.testing.CliRunner().invoke(cli.main, ["plugin", "install"])
    assert r.exit_code == 0

    monkeypatch.setattr(plugin_setup, "install_plugin", lambda: False)
    r = click.testing.CliRunner().invoke(cli.main, ["plugin", "install"])
    assert r.exit_code == 1


def test_bare_tandem_offers_plugin_after_pairing(
        homes, ok_versions, entered, monkeypatch):
    from tandem import plugin_setup

    calls = []
    monkeypatch.setattr(plugin_setup, "offer_install",
                        lambda: calls.append(len(entered)))
    r = click.testing.CliRunner().invoke(cli.main, [])
    assert r.exit_code == 0
    # offered exactly once, after pairing but before entering the shell
    assert calls == [0]
    assert len(entered) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_cli.py -v`
Expected: the two new tests FAIL (`plugin` is an unknown command → exit 2; offer never called); all pre-existing tests still pass.

- [ ] **Step 3: Wire the CLI**

In `src/tandem/cli.py`, after the `sync` command (before `_interactive`):

```python
@main.group()
def plugin() -> None:
    """Manage tandem's Claude Code plugin."""


@plugin.command(name="install")
def plugin_install_cmd() -> None:
    """Install the plugin via claude (marketplace add + plugin install)."""
    from .plugin_setup import install_plugin

    sys.exit(0 if install_plugin() else 1)
```

And change `_interactive` to:

```python
def _interactive(active: str) -> None:
    cwd = _cwd()
    _check_versions()  # hard: pairing needs both binaries on PATH
    with StateStore() as store:
        session = _pair_session(store, cwd, active)
    from .plugin_setup import offer_install

    offer_install()
    sys.exit(_enter_session(session))
```

(Deferred imports match the file's existing style — see `sync` and
`hook_route_cmd`. Note for the reviewer: `offer_install` must be looked up
on the module at call time via a deferred `from .plugin_setup import …`
inside the function; a top-level import would still work with the tests as
written, but the deferred style keeps `tandem hook-route`'s import cost
down, which that command's docstring cares about.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_cli.py -v`
Expected: all pass, including the two new tests.

- [ ] **Step 5: Commit**

```bash
git add src/tandem/cli.py tests/test_cli.py
git commit -m "feat: tandem plugin install + first-run offer wiring

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: README + full-suite verification

**Files:**
- Modify: `README.md` (lines 72–80: the install code block and the paragraph after it)

**Interfaces:**
- Consumes: the CLI surface from Task 4 (`tandem`, `tandem plugin install`).
- Produces: user-facing quickstart matching the shipped behavior.

- [ ] **Step 1: Update the quickstart**

In `README.md`, replace this block (currently lines 72–80):

```bash
uv tool install tandem-cli                        # the binary the hook drives
claude plugin marketplace add Bhavya6187/tandem   # this repo, as a marketplace
claude plugin install tandem@tandem
```

The marketplace tracks this repo's default branch, but nothing updates
behind your back: you pull new versions when you run `claude plugin
marketplace update` (or `/plugin marketplace update` inside Claude).

with:

```bash
uv tool install tandem-cli   # the binary the hook drives
tandem                       # first launch offers the plugin install — hit enter
```

Said no at the prompt, or running non-interactively? One command performs
both marketplace steps whenever you're ready:

```bash
tandem plugin install   # = claude plugin marketplace add Bhavya6187/tandem
                        #   + claude plugin install tandem@tandem
```

The marketplace tracks this repo's default branch, but nothing updates
behind your back: the first-launch offer asks before touching anything, and
you pull new versions when you run `claude plugin marketplace update` (or
`/plugin marketplace update` inside Claude).

- [ ] **Step 2: Run the full test suite**

Run: `uv run pytest`
Expected: everything passes (the suite had no failures before this work; any failure here is a regression to fix before committing).

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs: quickstart via first-run offer / tandem plugin install

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Self-Review Notes

- Spec §1 → Tasks 1–3 (detection / install / offer); §2 → Task 4 group; §3 → Task 3 + Task 4 wiring; §4 needs no code (constraint recorded in Global Constraints: read-only, no bundling); §5 → Task 5; §6 → each task's tests. Detection nuance (missing file → False) is a deliberate spec amendment, folded into Task 1 Step 6.
- Names used across tasks: `is_plugin_installed`, `install_plugin`, `offer_install`, `MANUAL_COMMANDS`, `LATER_HINT`, `_offer_stamp`, `_stdin_is_tty`, `MARKETPLACE_REPO`, `PLUGIN_ID`, `paths.claude_installed_plugins_path` — consistent in every task above.
- Test count checkpoints: 6 after Task 1, 10 after Task 2, 18 after Task 3.
