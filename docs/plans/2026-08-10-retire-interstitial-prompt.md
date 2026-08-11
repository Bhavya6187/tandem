# Retire the Interstitial Prompt Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the `tandem (claude)> ` prompt loop and the one-shot `tandem switch` subcommand; exiting a harness prints the resume hint and returns to the OS shell, with Ctrl-] as the only flip.

**Architecture:** `src/tandem/shell.py` is renamed to `src/tandem/flip.py` and keeps only the flip machinery (`_flip_loop`, `_switch` with its flip-back ladder, held-back report reprinting); the `input()` REPL and its command dispatch are deleted. `cli.py` loses the `switch` command and the `_SESSION_ID` thread-through, and `_report_switch` moves into `flip.py` (its only remaining caller).

**Tech Stack:** Python 3.11+, click, pytest. Tests run with `uv run pytest` from the repo root. No linter is configured.

**Spec:** `docs/specs/2026-08-10-remove-interstitial-prompt-design.md`

## Global Constraints

- The resume hint (`to continue this session: tandem resume <id>`) MUST print from a `finally` — no failure inside the loop may cost the user their session id. This invariant is unchanged from today and guarded by tests.
- The flip-back ladder is one retry only — never ping-pong between two harnesses that both refuse to launch.
- All work happens on branch `retire-interstitial-prompt` (already exists, spec committed).
- Release/version bump is OUT OF SCOPE (spec: next release is 0.3.0, handled separately).

---

### Task 1: `flip.py` — the prompt goes, the flip loop stays

**Files:**
- Rename: `src/tandem/shell.py` → `src/tandem/flip.py` (rewrite: delete `HELP`, `_split_run_line`, `_dispatch`, the `while True` input loop; rename `run_shell` → `run_session`, drop its `input_fn` parameter)
- Modify: `src/tandem/cli.py:647-650` (`_enter_session`)
- Rename: `tests/test_shell.py` → `tests/test_flip.py` (rewrite: keep/adapt the flip, ladder, report, and resume-hint tests; delete all prompt tests and the `scripted`/`Rival`/`_capture_oneoff` helpers)

**Interfaces:**
- Consumes: `ops.switch_session(store, session) -> (new_active, problems, mem)`, `StateStore`, `InteractiveRunner` — all unchanged.
- Produces: `flip.run_session(tandem_id: str, sink_factory, run_harness=None) -> int`. Task 2 relies on this exact name and on `_switch` still late-importing `_report_switch` from `cli` (Task 2 moves it).

- [ ] **Step 1: Rename both files with git mv**

```bash
cd /Users/bhavya/git/tandem
git mv src/tandem/shell.py src/tandem/flip.py
git mv tests/test_shell.py tests/test_flip.py
```

- [ ] **Step 2: Rewrite `tests/test_flip.py` (the failing tests)**

Replace the entire file content with:

```python
"""Flip loop tests with a fake harness runner."""

import sqlite3

import pytest

from tandem import flip, paths
from tandem.state import StateStore


class FakeMem:
    actions: list = []
    warnings: list = []


@pytest.fixture
def sess(tmp_path, monkeypatch):
    monkeypatch.setenv("TANDEM_HOME", str(tmp_path / ".tandem"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / ".codex"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / ".claude"))
    with StateStore() as store:
        return store.create_session(str(tmp_path / "proj"), "claude", "c-id", "x-id")


def fake_runner(log, codes=None):
    codes = list(codes or [])

    def run_harness(session):
        log.append(session.active)
        return codes.pop(0) if codes else 0

    return run_harness


def _flipping_switch(monkeypatch):
    def fake_switch(store, session):
        new = "codex" if session.active == "claude" else "claude"
        store.set_active(session.tandem_id, new)
        return new, [], FakeMem()

    monkeypatch.setattr(flip.ops, "switch_session", fake_switch)


def test_exit_prints_resume_hint_and_last_code(sess, capsys):
    log = []
    code = flip.run_session(
        sess.tandem_id, None, run_harness=fake_runner(log, codes=[7])
    )
    assert code == 7
    assert log == ["claude"]
    assert f"to continue this session: tandem resume {sess.tandem_id}" in (
        capsys.readouterr().out
    )


def test_failed_entry_reports_and_exits(sess, capsys):
    """The harness binary vanishing must not kill the session."""

    def run_harness(session):
        raise FileNotFoundError("claude: command not found")

    flip.run_session(sess.tandem_id, None, run_harness=run_harness)
    cap = capsys.readouterr()
    assert "could not run the harness: FileNotFoundError" in cap.err
    assert f"to continue this session: tandem resume {sess.tandem_id}" in cap.out


def test_resume_hint_prints_even_when_the_loop_raises(sess, capsys, monkeypatch):
    """The hint is the only place the id is shown, so it must survive an
    unexpected exception escaping the loop."""

    def boom(tandem_id, run_harness, first, reports=None):
        raise RuntimeError("terminal went away")

    monkeypatch.setattr(flip, "_flip_loop", boom)
    with pytest.raises(RuntimeError):
        flip.run_session(sess.tandem_id, None, run_harness=fake_runner([]))
    assert f"to continue this session: tandem resume {sess.tandem_id}" in (
        capsys.readouterr().out
    )


def test_flip_reenters_other_harness(sess, monkeypatch):
    """Ctrl-] inside the harness flips and re-enters with no stop."""
    _flipping_switch(monkeypatch)
    calls = []

    def run_harness(session):
        calls.append(session.active)
        if len(calls) == 1:
            return 0, True   # user pressed Ctrl-]
        return 0, False      # then exited normally

    code = flip.run_session(sess.tandem_id, None, run_harness=run_harness)
    assert code == 0
    assert calls == ["claude", "codex"]  # flip switched roles


def test_flip_loop_keeps_flipping_until_a_plain_exit(sess, monkeypatch):
    """Successive flips chain; a plain exit ends the session."""
    _flipping_switch(monkeypatch)
    calls = []

    def run_harness(session):
        calls.append(session.active)
        return 0, len(calls) < 4

    flip.run_session(sess.tandem_id, None, run_harness=run_harness)
    assert calls == ["claude", "codex", "claude", "codex"]


def test_flip_failure_exits_with_the_session_intact(sess, capsys, monkeypatch):
    """ops.switch_session raising must not lose the session or spin."""

    def run_harness(session):
        return 0, True

    def boom(store, session):
        raise RuntimeError("no flip for you")

    monkeypatch.setattr(flip.ops, "switch_session", boom)
    code = flip.run_session(sess.tandem_id, None, run_harness=run_harness)
    cap = capsys.readouterr()
    assert code == 0  # carried through; session intact
    assert "switch failed: RuntimeError: no flip for you" in cap.err
    assert f"to continue this session: tandem resume {sess.tandem_id}" in cap.out


def test_failed_launch_after_a_flip_returns_to_the_harness_we_left(
    sess, capsys, monkeypatch
):
    """The ladder's first rung: never strand the user facing a harness that
    cannot launch. Flip the roles back and re-enter the one they were in a
    second ago."""
    _flipping_switch(monkeypatch)
    calls = []

    def run_harness(session):
        calls.append(session.active)
        if session.active == "codex":
            raise FileNotFoundError("codex: command not found")
        return 0, len(calls) == 1   # the first claude session asks for a flip

    flip.run_session(sess.tandem_id, None, run_harness=run_harness)
    cap = capsys.readouterr()
    assert calls == ["claude", "codex", "claude"]  # flipped, failed, flipped back
    assert "codex would not start — switching back to claude." in cap.err
    with StateStore() as store:
        assert store.get_session(sess.tandem_id).active == "claude"  # roles restored


def test_both_harnesses_failing_exits_with_the_session_intact(
    sess, capsys, monkeypatch
):
    """Second rung: the flip-back cannot launch either, so land at the OS
    shell with the errors shown — and the resume hint still prints."""
    _flipping_switch(monkeypatch)
    calls = []

    def run_harness(session):
        calls.append(session.active)
        if len(calls) == 1:
            return 0, True          # flip requested
        raise FileNotFoundError(f"{session.active}: command not found")

    code = flip.run_session(sess.tandem_id, None, run_harness=run_harness)
    cap = capsys.readouterr()
    assert calls == ["claude", "codex", "claude"]  # one retry only, no ping-pong
    assert cap.err.count("could not run the harness: FileNotFoundError") == 2
    assert code == 0
    assert f"to continue this session: tandem resume {sess.tandem_id}" in cap.out


def test_flip_back_does_not_run_when_the_switch_itself_fails(sess, capsys, monkeypatch):
    """`ops.switch_session` raising means roles never moved: there is nothing
    to flip back from."""

    def boom(store, session):
        raise RuntimeError("no flip for you")

    monkeypatch.setattr(flip.ops, "switch_session", boom)
    calls = []

    def run_harness(session):
        calls.append(session.active)
        return 0, len(calls) == 1

    flip.run_session(sess.tandem_id, None, run_harness=run_harness)
    cap = capsys.readouterr()
    assert calls == ["claude"]
    assert "switch failed: RuntimeError: no flip for you" in cap.err
    assert "would not start" not in cap.err


def test_plain_entry_failure_has_no_flip_back(sess, capsys):
    """A failed launch with no flip involved never moved roles, so it must
    not drag the session into the other harness."""
    calls = []

    def run_harness(session):
        calls.append(session.active)
        raise FileNotFoundError("claude: command not found")

    flip.run_session(sess.tandem_id, None, run_harness=run_harness)
    assert calls == ["claude"]
    with StateStore() as store:
        assert store.get_session(sess.tandem_id).active == "claude"
    assert "would not start" not in capsys.readouterr().err


def test_flip_with_a_vanished_session_row_stops_the_loop(sess, capsys):
    """A session deleted mid-flight breaks the loop instead of spinning."""

    def run_harness(session):
        conn = sqlite3.connect(paths.state_db_path())
        with conn:
            conn.execute(
                "DELETE FROM sessions WHERE tandem_id = ?", (sess.tandem_id,)
            )
        conn.close()
        return 0, True

    code = flip.run_session(sess.tandem_id, None, run_harness=run_harness)
    assert code == 0
    assert "switch failed" in capsys.readouterr().err


class FakeInteractiveRunner:
    """Stands in for the real runner behind `run_session`'s own closure — the
    seam the reports plumbing actually lives in, so the injected `run_harness`
    used by every other test would skip it entirely."""

    script: list = []
    seen: list = []

    def __init__(self, session, sink_factory=None):
        self.session = session
        self.reports = []
        self.flip_requested = False

    def run(self):
        FakeInteractiveRunner.seen.append(self.session.active)
        reports, flip_req = FakeInteractiveRunner.script.pop(0)
        self.reports = list(reports)
        self.flip_requested = flip_req
        if not flip_req:  # the real runner prints its own on a non-flip exit
            for line in self.reports:
                print(line)
        return 0


def _fake_runner_session(monkeypatch, sess, script):
    from tandem import runner as runner_mod

    _flipping_switch(monkeypatch)
    monkeypatch.setattr(runner_mod, "InteractiveRunner", FakeInteractiveRunner)
    FakeInteractiveRunner.script = list(script)
    FakeInteractiveRunner.seen = []
    flip.run_session(sess.tandem_id, None)
    return FakeInteractiveRunner.seen


def test_flip_reprints_the_runners_reports_after_the_clear(sess, capsys, monkeypatch):
    """The flip clears the screen; a sync error the user never gets to read is
    the same as no sync error at all, so the held-back lines print after it."""
    lines = [
        "tandem: sync error: transcript shrank",
        "tandem: status bar disabled for this session (terminal conflict)",
    ]
    seen = _fake_runner_session(
        monkeypatch, sess, [(lines, True), ([], False)]
    )
    out = capsys.readouterr().out
    assert seen == ["claude", "codex"]        # the flip really happened
    for line in lines:
        assert out.count(line) == 1           # shown once, on the fresh screen


def test_non_flip_exit_prints_its_reports_once(sess, capsys, monkeypatch):
    """No flip, no clear: the runner prints them itself and the flip loop must
    not print a second copy."""
    lines = ["tandem: sync error: transcript shrank"]
    seen = _fake_runner_session(monkeypatch, sess, [(lines, False)])
    out = capsys.readouterr().out
    assert seen == ["claude"]
    assert out.count(lines[0]) == 1


def test_int_returning_run_harness_still_works(sess):
    """Legacy seam: a plain int means no flip."""

    def run_harness(session):
        return 7

    code = flip.run_session(sess.tandem_id, None, run_harness=run_harness)
    assert code == 7


def test_flip_reports_switch_outcome(sess, capsys, monkeypatch):
    """Display names, memory actions and the doctor advisory — the flip is
    now the only switch path, so it must not report less than the old
    one-shot `tandem switch` did."""

    class Mem:
        actions = ["wrote shared block into AGENTS.md"]
        warnings = ["CLAUDE.md has no tandem markers; read-only"]

    problems = ["transcript for newly active harness does not exist yet"]

    def fake_switch(store, session):
        store.set_active(session.tandem_id, "codex")
        return "codex", problems, Mem()

    monkeypatch.setattr(flip.ops, "switch_session", fake_switch)
    calls = []

    def run_harness(session):
        calls.append(session.active)
        return 0, len(calls) == 1

    flip.run_session(sess.tandem_id, None, run_harness=run_harness)
    cap = capsys.readouterr()
    assert "active harness: Claude Code -> Codex CLI" in cap.out
    assert "memory: wrote shared block into AGENTS.md" in cap.out
    assert "memory: CLAUDE.md has no tandem markers" in cap.err
    assert "transcript for newly active harness does not exist yet" in cap.err
    assert "run `tandem doctor` for details." in cap.err
```

Deleted relative to the old `test_shell.py` (all prompt-only behavior): `test_switch_flips_and_reenters`, `test_enter_and_resume_reenter_without_flip`, `test_resume_with_id_rejected_at_prompt`, `test_eof_exits_and_ctrl_c_does_not`, `test_unknown_input_prints_command_list`, `test_unbalanced_quote_does_not_kill_the_prompt`, `test_launch_options_neither_pair_nor_nest`, `test_non_click_exception_keeps_the_prompt_alive`, `test_status_dispatches_through_cli`, `test_prompt_shows_active_harness`, `test_dispatch_targets_this_shell_not_the_cwd_mru`, `test_doctor_at_the_prompt_targets_this_shell`, `test_failed_reentry_returns_to_the_prompt`, `test_vanished_session_row_leaves_the_loop_cleanly` (prompt-loop variant; the flip variant survives), the four `run --on` parsing tests, `test_malformed_run_still_gets_click_usage_error`, `test_flip_from_the_typed_switch_command`, and the `scripted`/`Rival`/`_capture_oneoff` helpers.

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest tests/test_flip.py -x -q`
Expected: FAIL at import — `tandem.flip` still has `run_shell` (not `run_session`) and still expects `input_fn`.

- [ ] **Step 4: Rewrite `src/tandem/flip.py`**

Replace the entire file content with (this is the old `shell.py` minus `HELP`, `_split_run_line`, `_dispatch`, the `while True` input loop, and the `shlex` import; `run_shell(tandem_id, sink_factory, input_fn, run_harness)` becomes `run_session(tandem_id, sink_factory, run_harness)`; `_flip_loop`, `_switch`, `_try_enter`, `_enter`, `_norm`, `_clear_screen` are carried over verbatim except the two docstring updates shown):

```python
"""Run the active harness, re-entering on Ctrl-] flips until a plain exit.

Bare `tandem` / `tandem resume` enter here: run the active harness on its
PTY; a flip asked for from inside it (Ctrl-]) re-enters the other one with
no stop in between. When the harness exits without a flip pending, print
the resume hint and return to the OS shell.
"""

from __future__ import annotations

import sys

import click

from . import ops
from .state import StateStore


def run_session(tandem_id: str, sink_factory, run_harness=None) -> int:
    """Run the active harness, flipping (Ctrl-]) until a session ends
    without requesting one. Returns the last harness exit code.
    `run_harness` is an injection point for tests (real: an
    InteractiveRunner)."""
    # Report lines the runner held back because the flip about to happen would
    # clear the screen out from under them; `_flip_loop` prints them onto the
    # fresh screen. Refilled by every harness run. Injected test runners never
    # touch it, which leaves it empty — harmless.
    reports: list[str] = []
    if run_harness is None:  # pragma: no cover - interactive default

        def run_harness(session):
            from .runner import InteractiveRunner

            r = InteractiveRunner(session, sink_factory=sink_factory)
            code = r.run()
            reports[:] = r.reports
            return code, r.flip_requested

    # The resume hint is the only place the id is shown, so it prints from a
    # finally: no failure inside the loop may cost the user their session.
    code = 1
    try:
        code = _flip_loop(
            tandem_id, run_harness, _enter(tandem_id, run_harness, code), reports
        )
    finally:
        # Hint first: state bookkeeping must not be able to swallow it.
        click.echo(f"to continue this session: tandem resume {tandem_id}")
        with StateStore() as store:
            store.touch_used(tandem_id)
    return code


def _norm(res) -> tuple[int, bool]:
    """run_harness returns `(code, flip)`; a bare int (legacy callers and
    test seams) means "exited, no flip"."""
    return res if isinstance(res, tuple) else (res, False)


def _clear_screen() -> None:
    if sys.stdout.isatty():  # pragma: no cover - interactive only
        sys.stdout.write("\x1b[2J\x1b[H")
        sys.stdout.flush()


def _flip_loop(
    tandem_id: str, run_harness, first: tuple[int, bool],
    reports: list[str] | None = None,
) -> int:
    """Keep flipping (Ctrl-]) until a session ends without requesting one.
    No stop between flips — this is the frame's tab feel. A failed flip
    reports itself and returns no-flip, which ends the loop and returns
    the user to the OS shell with the session intact.

    `reports` is the outgoing session's held-back report lines (sync errors,
    notes). They print right after the clear, never before it: the clear is
    what would otherwise erase them, and the whole point is that a flip must
    not cost the user a sync-failure warning."""
    code, flip = first
    while flip:
        _clear_screen()
        if reports:
            for line in reports:
                click.echo(line)
            reports.clear()   # this session's news, reported once
        code, flip = _switch(tandem_id, run_harness, code)
    return code


def _switch(
    tandem_id: str, run_harness, code: int, fall_back: bool = True
) -> tuple[int, bool]:
    """Flip roles and re-enter the newly active harness. Returns the exit
    code to carry forward (unchanged if the flip failed) and whether the
    re-entered harness asked for another flip.

    Two failures, two answers. The switch itself failing means roles never
    moved, so ending the session at the OS shell is the right landing. The
    switch succeeding and the *launch* failing is worse: the active harness
    cannot start (`codex` uninstalled, a bad `[codex] args`), which is
    precisely the dead end the spec's ladder exists to avoid. So the first
    rung is to flip straight back and re-enter the harness the user just
    left — never strand them.

    `fall_back=False` marks that flip-back attempt: one retry, no ping-pong
    between two harnesses that both refuse to launch. If it also fails, the
    loop ends with the error shown — and the session itself is never at
    risk, since `run_session`'s finally always prints the resume hint."""
    from .cli import _report_switch

    with StateStore() as store:
        session = store.get_session(tandem_id)
        if session is None:
            click.secho(
                f"switch failed: session {tandem_id} is no longer in the"
                " state store.",
                fg="red",
                err=True,
            )
            return code, False
        old = session.active
        try:
            new_active, problems, mem = ops.switch_session(store, session)
        except Exception as exc:
            click.secho(
                f"switch failed: {type(exc).__name__}: {exc}", fg="red", err=True
            )
            return code, False
    _report_switch(old, new_active, problems, mem)
    code, flip, launched = _try_enter(tandem_id, run_harness, code)
    if launched or not fall_back:
        return code, flip
    click.secho(
        f"{new_active} would not start — switching back to {old}.",
        fg="yellow",
        err=True,
    )
    return _switch(tandem_id, run_harness, code, fall_back=False)


def _try_enter(tandem_id: str, run_harness, code: int) -> tuple[int, bool, bool]:
    """Run the active harness; returns (exit code, flip requested, launched).
    `launched` is False when the harness never got off the ground (a missing
    binary, a vanished session row) as opposed to running and exiting — the
    distinction `_switch` needs to decide whether to flip back."""
    try:
        with StateStore() as store:
            session = store.get_session(tandem_id)
            if session is None:
                raise LookupError(f"session {tandem_id} is not in the state store")
            store.touch_used(tandem_id)
        return (*_norm(run_harness(session)), True)
    except Exception as exc:
        click.secho(
            f"could not run the harness: {type(exc).__name__}: {exc}",
            fg="red",
            err=True,
        )
        return code, False, False


def _enter(tandem_id: str, run_harness, code: int) -> tuple[int, bool]:
    """Run the active harness; returns (exit code, flip requested). A failed
    launch (or a session row that vanished) is reported and `code` is carried
    forward with no flip, so the caller ends the session instead of losing
    it. No flip-back ladder here: roles never moved, so the harness the user
    just failed to launch *is* the one they were in."""
    c, flip, _ = _try_enter(tandem_id, run_harness, code)
    return c, flip
```

Note: `_switch` still late-imports `_report_switch` from `cli` in this task; Task 2 moves the function here and removes that import.

- [ ] **Step 5: Point `cli._enter_session` at the new module**

In `src/tandem/cli.py`, replace:

```python
def _enter_session(session: PairedSession) -> int:
    from .shell import run_shell

    return run_shell(session.tandem_id, _default_sink_factory)
```

with:

```python
def _enter_session(session: PairedSession) -> int:
    from .flip import run_session

    return run_session(session.tandem_id, _default_sink_factory)
```

- [ ] **Step 6: Run the flip tests, then the full suite**

Run: `uv run pytest tests/test_flip.py -q`
Expected: all pass.

Run: `uv run pytest -q`
Expected: all pass (`test_cli.py` still passes — the `switch` command is untouched until Task 2).

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "refactor: shell.py becomes flip.py — the prompt goes, the flip loop stays

Exiting the harness with no flip pending prints the resume hint and
returns to the OS shell; the tandem prompt loop is gone. Ctrl-]'s flip
machinery (ladder, held-back reports) is unchanged.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: `cli.py` — delete `tandem switch` and `_SESSION_ID`, move `_report_switch`

**Files:**
- Modify: `src/tandem/cli.py` (delete the `switch` command at lines 270-284, `_report_switch` at lines 247-267, `_SESSION_ID` at lines 24-28, `_resolve_session` at lines 31-34; inline the session lookup into `_require_session`)
- Modify: `src/tandem/flip.py` (add `_report_switch` + the `get_adapter` import; drop the late `from .cli import _report_switch`)
- Test: `tests/test_cli.py` (delete `test_one_shot_switch_hints_resume`, lines 164-179)

**Interfaces:**
- Consumes: `flip.py` exactly as Task 1 left it (late import of `cli._report_switch` in `_switch`).
- Produces: `cli.py` with no `switch` command, no `_SESSION_ID`, no `_report_switch`, no `_resolve_session`; `flip._report_switch(old, new_active, problems, mem)` module-local. Task 3 relies on nothing from this task.

- [ ] **Step 1: Delete the one-shot switch test**

In `tests/test_cli.py`, delete the whole `test_one_shot_switch_hints_resume` function (lines 164-179, from `def test_one_shot_switch_hints_resume(...)` through `assert "Run `tandem` to continue" not in r.output`).

- [ ] **Step 2: Move `_report_switch` into `flip.py`**

In `src/tandem/flip.py`:

1. Change the import block at the top from:

```python
from . import ops
from .state import StateStore
```

to:

```python
from . import ops
from .harness import get_adapter
from .state import StateStore
```

2. Add the function (place it directly above `_switch`), moved verbatim from `cli.py` except the docstring's first sentence, which loses the dead reference to the one-shot command:

```python
def _report_switch(old: str, new_active: str, problems, mem) -> None:
    """Report the outcome of a role flip: memory-sync actions and the
    may-not-resume advisory."""
    click.echo(
        f"active harness: {get_adapter(old).display_name} -> "
        f"{get_adapter(new_active).display_name}"
    )
    for a in mem.actions:
        click.echo(f"  memory: {a}")
    for w in mem.warnings:
        click.secho(f"  memory: {w}", fg="yellow", err=True)
    for p in problems:
        click.secho(f"  warning: {p}", fg="yellow", err=True)
    if problems:
        click.secho(
            "  the newly active session may not resume cleanly; "
            "run `tandem doctor` for details.",
            fg="yellow",
            err=True,
        )
```

3. In `_switch`, delete the line `from .cli import _report_switch` (the call site stays; the function is now module-local).

- [ ] **Step 3: Delete the `switch` command and `_report_switch` from `cli.py`**

In `src/tandem/cli.py`, delete both blocks entirely:

- `def _report_switch(old: str, new_active: str, problems, mem) -> None:` through the end of its `if problems:` body (lines 247-267)
- `@main.command()` / `def switch() -> None:` through `click.echo("Run \`tandem resume\` to continue in the new harness.")` (lines 270-284)

- [ ] **Step 4: Delete `_SESSION_ID` and `_resolve_session` from `cli.py`**

Delete the comment block and assignment (lines 24-28):

```python
# Set by the tandem prompt (shell.py) around each dispatched command so it
# acts on that shell's own session. Without it, a second `tandem` in the same
# directory becomes the cwd-MRU and silently steals `status`/`sync`/`run --on`
# typed in the first shell.
_SESSION_ID: str | None = None
```

Delete `_resolve_session` (lines 31-34) and inline it at BOTH call sites:

1. `_require_session` (lines 37-46) becomes:

```python
def _require_session(store: StateStore) -> PairedSession:
    session = store.latest_session_for_cwd(_cwd())
    if session is None:
        click.echo(
            "No tandem session for this directory. Run `tandem` to start one.",
            err=True,
        )
        sys.exit(1)
    store.touch_used(session.tandem_id)
    return session
```

2. In the `doctor` command (line 583), replace:

```python
        session = _resolve_session(store)
```

with:

```python
        session = store.latest_session_for_cwd(_cwd())
```

(`doctor` deliberately accepts a None session — `run_doctor` reports it — so it does not use `_require_session`.)

CAUTION: `_SESSION_ID_RE` at `cli.py:402` is an unrelated session-id validation regex — do NOT touch it. Then verify nothing else references the deleted names (`-w` keeps `_SESSION_ID_RE` out of the matches):

Run: `grep -rnw "_resolve_session\|_SESSION_ID" src/ tests/`
Expected: no output.

- [ ] **Step 5: Run the full suite**

Run: `uv run pytest -q`
Expected: all pass. Also confirm the command is really gone:

Run: `uv run tandem switch 2>&1 | head -3 || true`
Expected: click usage error — `No such command 'switch'`.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "feat!: remove the one-shot tandem switch command

Ctrl-] is the only flip. _report_switch moves to flip.py (its only
caller); the _SESSION_ID prompt-dispatch thread-through is dead code
without the prompt and goes with it.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: Docs — README and how-it-works describe exit-to-shell

**Files:**
- Modify: `README.md` (lines 52-59 exit block; line 115 "no prompt in between"; lines 130-131 cheat-sheet rows)
- Modify: `docs/how-it-works.md` (lines 12-20 prompt pillar; line 26 "no stop at the prompt in between")

**Interfaces:**
- Consumes: nothing from earlier tasks (docs-only).
- Produces: nothing later tasks rely on.

- [ ] **Step 1: Rewrite the README exit block**

Replace (lines 52-59):

```markdown
Exit the harness the usual way and you land at tandem's prompt instead of
your shell:

```
tandem (claude)> switch      # the same flip, from the prompt
tandem (codex)> exit
to continue this session: tandem resume a1b2c3d4e5f6
```
```

with:

```markdown
Exit the harness the usual way and you're back at your shell, with the
session saved and a hint for picking it up again:

```
to continue this session: tandem resume a1b2c3d4e5f6
```
```

- [ ] **Step 2: Fix the README flip bullet and cheat sheet**

Line 115, replace:

```markdown
  lets the sync settle, and resumes the other side — no prompt in
  between.
```

with:

```markdown
  lets the sync settle, and resumes the other side — no stop in
  between.
```

Line 130, the Ctrl-] row: replace

```markdown
| `Ctrl-]` | Flip to the other harness from inside a running session, without stopping at a prompt (rebindable in `[frame]`) |
```

with:

```markdown
| `Ctrl-]` | Flip to the other harness from inside a running session (rebindable in `[frame]`) |
```

Line 131: delete the `switch` row entirely:

```markdown
| `switch` | Flip active/shadow and enter the other agent — at the tandem prompt, or one-shot from your shell (one-shot only flips, it doesn't enter) |
```

- [ ] **Step 3: Rewrite the how-it-works pillar**

In `docs/how-it-works.md`, replace the bullet (lines 12-20):

```markdown
- **A persistent prompt, not the OS shell.** Exiting the harness — as
  opposed to flipping out of it — lands you at `tandem (claude)>`. There,
  `switch` flips roles and drops you straight into the other tool, Enter
  re-enters the current one, and
  `status` / `sync` / `doctor` / `run --on` / `sync-mcp` all run against
  this session. `exit` (or Ctrl-D) returns to your shell and prints the
  resume hint. Every command also works one-shot from your shell,
  targeting the directory's most recently used session.
```

with:

```markdown
- **Exit means exit.** Leaving the harness — as opposed to flipping out
  of it — prints the resume hint and returns you to your OS shell; the
  paired session is saved and `tandem resume` re-enters it.
  `status` / `sync` / `doctor` / `run --on` / `sync-mcp` run one-shot
  from your shell, targeting the directory's most recently used session.
```

And on line 26, replace `— with no stop at the prompt in between.` with `— with no stop in between.`

- [ ] **Step 4: Sweep for stragglers**

Run: `grep -rn "tandem (claude)>\|the prompt\|tandem switch\|run_shell\|shell.py" README.md docs/*.md plugin/ 2>/dev/null | grep -v docs/specs | grep -v docs/plans | grep -v docs/superpowers`
Expected: no hits describing the removed prompt or command (permission-prompt/UserPromptSubmit mentions are unrelated and fine). Fix anything that slips through the specific edits above.

- [ ] **Step 5: Run the full suite one last time and commit**

Run: `uv run pytest -q`
Expected: all pass.

```bash
git add -A
git commit -m "docs: exiting the harness means exiting tandem

README and how-it-works drop the prompt walkthrough and the switch
cheat-sheet row; exit prints the resume hint and lands at the OS shell.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```
