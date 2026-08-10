# Idle Flip via Claude Session-Status Probe — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Pressing the flip key while claude sits idle flips immediately, by reading claude's own per-session status registry instead of inferring turn state from file mtimes.

**Architecture:** Claude 2.1.226 maintains `~/.claude/sessions/<pid>.json` — one file per live session with `sessionId` and `status: "busy" | "waiting"`, rewritten on transitions (it is the data `claude agents --json` prints). A new adapter method scans that registry by session id (tandem mints claude session ids, so it always knows the key), skipping entries whose pid is dead. When claude is the active harness, `wait_until_safe` consults this probe as the **entire** turn-boundary test — replacing both the sentinel-vs-transcript mtime comparison and the 120s valve, which are broken for claude because modern claude appends housekeeping to the transcript between turns (`away_summary` minutes after Stop, mtime bumps on resume). Codex keeps the existing logic untouched.

**Tech Stack:** Python 3.11+ stdlib only (json, os, pathlib). Tests: pytest via the repo venv.

**Spec:** `docs/superpowers/specs/2026-08-10-idle-flip-status-probe.md` (untracked — `docs/superpowers/` is gitignored).

## Global Constraints

- Work directly on branch `meta-harness-frame` (the PR #36 live-validation fixes are committed as `cc0321a`/`1623e35`; the tree starts clean). Stage only the explicit paths each commit step lists — no `git add -A`/`git add .`/`git commit -a`.
- Single tier (operator decision): when the probe is wired, its verdict is final. `"busy"` waits; **anything else — `"waiting"`, no matching entry, unreadable file, unknown status — flips immediately.** No fallback to mtime logic, no valve. Eager failure on registry schema drift is accepted ("if it breaks we'll fix it later").
- Codex flip behavior must be byte-identical: every existing test in `tests/test_runner.py` stays green unmodified.
- The Stop hook / sentinel wiring is untouched — the transcript tailer uses the sentinel as its wake signal.
- Run tests with `.venv/bin/python -m pytest` (system python lacks the project deps).

## File Structure

- `src/tandem/paths.py` — add `claude_sessions_dir()` (registry location, `CLAUDE_CONFIG_DIR`-aware like every other helper here).
- `src/tandem/harness/claude_code.py` — add module-level `_pid_alive()` and `ClaudeCodeAdapter.session_status()`. Adapter owns format knowledge; the runner never parses registry JSON.
- `src/tandem/runner.py` — `wait_until_safe` and `FlipMonitor` gain an optional `status_probe`; `InteractiveRunner.run()` wires it for claude only.
- `tests/test_runner.py` — all new tests live here, beside the existing wait/monitor/wiring tests they imitate.
- `docs/how-it-works.md` — the frame bullet describes the new claude boundary test.

---

### Task 1: Registry probe on the claude adapter

**Files:**
- Modify: `src/tandem/paths.py` (after `claude_installed_plugins_path`, ~line 56)
- Modify: `src/tandem/harness/claude_code.py` (imports ~line 13; new code after `quit_keystrokes`, ~line 130)
- Test: `tests/test_runner.py` (append at end)

**Interfaces:**
- Produces: `paths.claude_sessions_dir() -> Path`; `ClaudeCodeAdapter.session_status(session_id: str) -> str | None`; module function `claude_code._pid_alive(pid: int) -> bool` (monkeypatch seam for tests).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_runner.py`:

```python
# ---- claude session-status probe -------------------------------------------


def _registry(tmp_path, monkeypatch, entries):
    """Fake ~/.claude/sessions with the given {filename: dict-or-raw} files."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / ".claude"))
    d = tmp_path / ".claude" / "sessions"
    d.mkdir(parents=True)
    for name, entry in entries.items():
        text = entry if isinstance(entry, str) else json.dumps(entry)
        (d / name).write_text(text)
    return d


_SID = "11111111-1111-4111-8111-111111111111"


def _claude_adapter():
    from tandem.harness import get_adapter
    return get_adapter("claude")


def test_pid_alive_own_pid():
    from tandem.harness.claude_code import _pid_alive
    assert _pid_alive(os.getpid()) is True


def test_session_status_reads_busy_and_waiting(tmp_path, monkeypatch):
    me = os.getpid()
    _registry(tmp_path, monkeypatch, {
        f"{me}.json": {"pid": me, "sessionId": _SID, "status": "busy"},
    })
    assert _claude_adapter().session_status(_SID) == "busy"
    _registry(tmp_path.joinpath("b"), monkeypatch, {
        f"{me}.json": {"pid": me, "sessionId": _SID, "status": "waiting",
                       "waitingFor": "input needed"},
    })
    assert _claude_adapter().session_status(_SID) == "waiting"


def test_session_status_none_when_no_match(tmp_path, monkeypatch):
    me = os.getpid()
    _registry(tmp_path, monkeypatch, {
        f"{me}.json": {"pid": me, "sessionId": "someone-else", "status": "busy"},
    })
    assert _claude_adapter().session_status(_SID) is None


def test_session_status_none_when_dir_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / ".claude"))
    assert _claude_adapter().session_status(_SID) is None


def test_session_status_skips_stale_dead_pid_entry(tmp_path, monkeypatch):
    # A crashed run of this same resumed session leaves a dead-pid file
    # frozen at "busy"; the live entry must win.
    from tandem.harness import claude_code
    me = os.getpid()
    _registry(tmp_path, monkeypatch, {
        "99999.json": {"pid": 99999, "sessionId": _SID, "status": "busy"},
        f"{me}.json": {"pid": me, "sessionId": _SID, "status": "waiting"},
    })
    monkeypatch.setattr(claude_code, "_pid_alive", lambda pid: pid == me)
    assert claude_code.ClaudeCodeAdapter().session_status(_SID) == "waiting"
    # dead-only: no live entry at all reads as no answer
    monkeypatch.setattr(claude_code, "_pid_alive", lambda pid: False)
    assert claude_code.ClaudeCodeAdapter().session_status(_SID) is None


def test_session_status_busy_wins_among_live_matches(tmp_path, monkeypatch):
    from tandem.harness import claude_code
    me = os.getpid()
    _registry(tmp_path, monkeypatch, {
        "11.json": {"pid": 11, "sessionId": _SID, "status": "waiting"},
        "22.json": {"pid": 22, "sessionId": _SID, "status": "busy"},
    })
    monkeypatch.setattr(claude_code, "_pid_alive", lambda pid: True)
    assert claude_code.ClaudeCodeAdapter().session_status(_SID) == "busy"


def test_session_status_tolerates_garbage_files(tmp_path, monkeypatch):
    me = os.getpid()
    _registry(tmp_path, monkeypatch, {
        "junk.json": "not json{",
        "list.json": '["not", "a", "dict"]',
        "nopid.json": {"sessionId": _SID, "status": "busy"},
        f"{me}.json": {"pid": me, "sessionId": _SID, "status": "waiting"},
    })
    assert _claude_adapter().session_status(_SID) == "waiting"
```

`tests/test_runner.py` already imports `os`, `time`, `threading`, `runner`, `paths`; add `import json` to its imports if not present.

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_runner.py -k "session_status or pid_alive" -v`
Expected: FAIL/ERROR with `AttributeError: ... no attribute 'session_status'` (and an ImportError on `_pid_alive`).

- [ ] **Step 3: Implement**

In `src/tandem/paths.py`, after `claude_installed_plugins_path()`:

```python
def claude_sessions_dir() -> Path:
    """Per-live-session registry: <pid>.json with sessionId and
    status "busy"|"waiting" — the data `claude agents --json` prints
    (observed: claude 2.1.226). Rewritten by claude on state
    transitions; stale files linger after a crash."""
    return claude_home() / "sessions"
```

In `src/tandem/harness/claude_code.py`: add `import os` to the stdlib imports (alphabetical, before `import uuid as uuidlib`). Then, module-level, above the adapter class:

```python
def _pid_alive(pid: int) -> bool:
    """A registry entry for a dead pid is a stale leftover (claude
    cleans up on exit, not on crash). PermissionError means alive but
    not ours — still alive; anything else unreadable counts as dead,
    because trusting a stale entry can freeze the flip on a phantom
    "busy"."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True
```

On `ClaudeCodeAdapter`, after `quit_keystrokes`:

```python
def session_status(self, session_id: str) -> str | None:
    """Live turn state from claude's session registry: "busy" while a
    turn runs, "waiting" at the prompt, None when no live entry
    matches. The flip wait treats anything but "busy" as flippable
    (single-tier by spec: eager on schema drift). Matched by
    sessionId — tandem mints claude session ids — never by pid
    filename, which a forking wrapper would break. Dead-pid entries
    are skipped; among several live matches "busy" wins, because
    flipping kills a live turn while waiting only costs a wait."""
    try:
        files = sorted(paths.claude_sessions_dir().glob("*.json"))
    except OSError:
        return None
    statuses: list[str] = []
    for p in files:
        try:
            entry = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(entry, dict) or entry.get("sessionId") != session_id:
            continue
        pid = entry.get("pid")
        if not isinstance(pid, int) or not _pid_alive(pid):
            continue
        status = entry.get("status")
        if isinstance(status, str):
            statuses.append(status)
    if "busy" in statuses:
        return "busy"
    return statuses[0] if statuses else None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_runner.py -k "session_status or pid_alive" -v`
Expected: 7 PASS.

- [ ] **Step 5: Commit**

```bash
git add src/tandem/paths.py src/tandem/harness/claude_code.py tests/test_runner.py
git commit -m "feat: claude session-status probe reads the live registry"
```

---

### Task 2: The probe replaces the mtime test in the flip wait

**Files:**
- Modify: `src/tandem/runner.py` (`wait_until_safe` ~line 129; `FlipMonitor.__init__` ~line 221 and `_run` ~line 270)
- Test: `tests/test_runner.py` (append)

**Interfaces:**
- Consumes: nothing from Task 1 (the probe arrives as a plain callable).
- Produces: `wait_until_safe(..., status_probe: Callable[[], str | None] | None = None)` — keyword-only position at the end of the signature; `FlipMonitor(..., status_probe=None)` stored as `self.status_probe` and forwarded by `_run`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_runner.py`:

```python
# ---- status probe replaces the mtime rules ---------------------------------


def test_wait_probe_waiting_overrides_busy_mtimes(tmp_path):
    # transcript newer than sentinel: the mtime rules read mid-turn and
    # would hold for the 120s valve. The probe says the session is at its
    # prompt, and the probe is the whole test now.
    t, s = tmp_path / "t.jsonl", tmp_path / "s.turn"
    _touch(t, time.time())
    _touch(s, time.time() - 30)
    assert wait_until_safe(t, s, cancelled=lambda: False, marker_wired=True,
                           status_probe=lambda: "waiting") is True


def test_wait_probe_no_answer_flips_eagerly(tmp_path):
    # single tier by spec: registry missing/unreadable -> flip now.
    t, s = tmp_path / "t.jsonl", tmp_path / "s.turn"
    _touch(t, time.time())
    _touch(s, time.time() - 30)
    assert wait_until_safe(t, s, cancelled=lambda: False, marker_wired=True,
                           status_probe=lambda: None) is True


def test_wait_probe_busy_blocks_then_releases(tmp_path):
    t, s = tmp_path / "t.jsonl", tmp_path / "s.turn"
    _touch(s, time.time())  # mtime rules would say idle (sentinel newest)
    _touch(t, time.time() - 30)
    state = {"status": "busy"}
    result = {}
    done = threading.Event()

    def wait():
        result["ok"] = wait_until_safe(t, s, cancelled=lambda: False,
                                       marker_wired=True, poll=0.05,
                                       status_probe=lambda: state["status"])
        done.set()

    threading.Thread(target=wait, daemon=True).start()
    assert not done.wait(timeout=0.4)   # busy verdict outranks idle mtimes
    state["status"] = "waiting"
    assert done.wait(timeout=3)
    assert result["ok"] is True


def test_wait_probe_busy_suppresses_valve(tmp_path):
    # A long-silent tool call: transcript ancient, quiesce tiny — the old
    # valve would fire and kill the live turn. The probe's "busy" holds.
    t, s = tmp_path / "t.jsonl", tmp_path / "s.turn"
    _touch(t, time.time() - 3600)
    _touch(s, time.time() - 7200)
    state = {"status": "busy"}
    result = {}
    done = threading.Event()

    def wait():
        result["ok"] = wait_until_safe(t, s, cancelled=lambda: False,
                                       marker_wired=True, quiesce=0.1,
                                       poll=0.05,
                                       status_probe=lambda: state["status"])
        done.set()

    threading.Thread(target=wait, daemon=True).start()
    assert not done.wait(timeout=0.5)   # outlives quiesce: no valve
    state["status"] = "waiting"
    assert done.wait(timeout=3)
    assert result["ok"] is True


def test_wait_probe_busy_cancel_honored(tmp_path):
    t, s = tmp_path / "t.jsonl", tmp_path / "s.turn"
    _touch(t, time.time())
    cancel = threading.Event()
    result = {}
    done = threading.Event()

    def wait():
        result["ok"] = wait_until_safe(t, s, cancelled=cancel.is_set,
                                       marker_wired=True, poll=0.05,
                                       status_probe=lambda: "busy")
        done.set()

    threading.Thread(target=wait, daemon=True).start()
    assert not done.wait(timeout=0.3)
    cancel.set()
    assert done.wait(timeout=3)
    assert result["ok"] is False


def test_monitor_probe_waiting_fires_immediately(tmp_path):
    # End-to-end through FlipMonitor: mtimes scream mid-turn, probe says
    # waiting -> the ladder runs. Mirrors test_monitor_arm_wait_terminate.
    t, s = tmp_path / "t.jsonl", tmp_path / "s.turn"
    _touch(t, time.time())
    control = _StubControl()
    m = FlipMonitor(control, [b"\x04"], transcript=t, sentinel=s,
                    marker_wired=True, poll=0.05,
                    status_probe=lambda: "waiting")
    m.start()
    m.flip_pressed()
    deadline = time.time() + 3
    while not m.flip_requested and time.time() < deadline:
        time.sleep(0.05)
    m.stop()
    assert m.flip_requested is True
    assert m.how == "soft"
    assert control.calls == [[b"\x04"]]
```

(`_touch` and `_StubControl` already exist in this file — `_StubControl.terminate` records `soft` in `self.calls` and returns `"soft"`.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_runner.py -k probe -v`
Expected: FAIL with `TypeError: wait_until_safe() got an unexpected keyword argument 'status_probe'`.

- [ ] **Step 3: Implement**

In `src/tandem/runner.py`, `wait_until_safe`: add the parameter after `provider`:

```python
def wait_until_safe(
    transcript: Path | None,
    sentinel: Path | None,
    cancelled: Callable[[], bool],
    quiesce: float | None = None,
    poll: float = 0.2,
    marker_wired: bool = False,
    provider: Callable[[], Path | None] | None = None,
    status_probe: Callable[[], str | None] | None = None,
) -> bool:
```

Append to its docstring (after the wall-clock paragraph):

```
    With `status_probe` (claude sessions), the probe is the entire
    boundary test and the marker/quiescence rules above never run: the
    probe reads claude's own session registry, which distinguishes a
    running turn ("busy") from an idle prompt ("waiting") directly.
    Anything but "busy" — including no answer at all — flips
    immediately: single-tier by spec, eager on registry schema drift.
    The valve is deliberately absent here; it existed to cap a marker
    that never arrives, and it killed genuinely long turns. The
    transcript-noise problem this solves: modern claude appends
    housekeeping (away_summary, last-prompt) minutes after Stop and
    bumps the transcript mtime on resume, so sentinel >= transcript is
    false while the session sits idle at its prompt.
```

Then, at the top of the `while True:` loop, immediately after the `cancelled()` check:

```python
        if status_probe is not None:
            if status_probe() == "busy":
                time.sleep(poll)
                continue
            return True
```

`FlipMonitor.__init__`: add `status_probe=None` after `poll` and store it:

```python
    def __init__(self, control, quit_bytes: list[bytes],
                 transcript: Path | None, sentinel: Path,
                 marker_wired: bool = False,
                 quiesce: float | None = None, poll: float = 0.2,
                 status_probe: Callable[[], str | None] | None = None):
```

with `self.status_probe = status_probe` beside the other assignments, and in `_run`, pass it through:

```python
            ok = wait_until_safe(
                self.transcript,
                self.sentinel,
                cancelled=lambda: (
                    not self._armed.is_set() or self._stop.is_set()
                ),
                quiesce=self.quiesce,
                poll=self.poll,
                marker_wired=self.marker_wired,
                provider=lambda: self.transcript,
                status_probe=self.status_probe,
            )
```

Append one line to the `FlipMonitor` class docstring:

```
    `status_probe` (claude only) reads the harness's own session
    registry and, when present, replaces the transcript/sentinel rules
    entirely — see `wait_until_safe`.
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_runner.py -v`
Expected: all new `probe` tests PASS **and every pre-existing test still passes** (codex path untouched).

- [ ] **Step 5: Commit**

```bash
git add src/tandem/runner.py tests/test_runner.py
git commit -m "feat: status probe replaces the mtime idle test in the flip wait"
```

---

### Task 3: Runner wiring and docs

**Files:**
- Modify: `src/tandem/runner.py` (`InteractiveRunner.run`, the `FlipMonitor(...)` construction, ~line 417)
- Modify: `docs/how-it-works.md` (frame bullet, ~lines 20–33)
- Test: `tests/test_runner.py` (append)

**Interfaces:**
- Consumes: `adapter.session_status(session_id)` (Task 1), `FlipMonitor(..., status_probe=...)` (Task 2).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_runner.py` (mirrors `test_runner_publishes_the_discovered_codex_rollout_to_the_monitor`; `_Sink` already exists in this file):

```python
def test_runner_wires_status_probe_for_claude(env_factory, monkeypatch):
    env = env_factory(active="claude")
    made = {}
    real = runner.FlipMonitor

    def capture(*a, **kw):
        made["kw"] = kw
        return real(*a, **kw)

    monkeypatch.setattr(runner, "FlipMonitor", capture)
    monkeypatch.setattr(
        runner, "run_in_pty",
        lambda argv, cwd=None, frame=None, control=None: 0,
    )
    runner.InteractiveRunner(env.session, lambda st, se, so: _Sink()).run()
    probe = made["kw"]["status_probe"]
    assert probe is not None
    # the probe closes over the claude sid: feed the registry and ask it
    me = os.getpid()
    d = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{me}.json").write_text(json.dumps(
        {"pid": me, "sessionId": env.session.claude_session_id,
         "status": "busy"}))
    assert probe() == "busy"


def test_runner_wires_no_probe_for_codex(env_factory, monkeypatch):
    env = env_factory(active="codex")
    made = {}
    real = runner.FlipMonitor

    def capture(*a, **kw):
        made["kw"] = kw
        return real(*a, **kw)

    monkeypatch.setattr(runner, "FlipMonitor", capture)
    monkeypatch.setattr(
        runner, "run_in_pty",
        lambda argv, cwd=None, frame=None, control=None: 0,
    )
    runner.InteractiveRunner(env.session, lambda st, se, so: _Sink()).run()
    assert made["kw"]["status_probe"] is None
```

`tests/test_runner.py` must import `Path` (`from pathlib import Path`) — add if absent.

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_runner.py -k wires -v`
Expected: FAIL with `KeyError: 'status_probe'` (runner never passes it yet).

- [ ] **Step 3: Implement the wiring**

In `InteractiveRunner.run()`, replace the `FlipMonitor(...)` construction:

```python
        monitor = FlipMonitor(
            control, adapter.quit_keystrokes(), transcript, sentinel,
            marker_wired=bool(hook_extra),
            # claude only: codex has no session registry, and a probe that
            # answers None would flip eagerly mid-turn — absence, not None
            # answers, is how codex opts out.
            status_probe=(
                (lambda: adapter.session_status(active_sid))
                if active == "claude" else None
            ),
        )
```

- [ ] **Step 4: Run the full suite**

Run: `.venv/bin/python -m pytest`
Expected: everything passes (the dirty PR #36 working-tree files pair with their own updated tests; do not touch them).

- [ ] **Step 5: Update docs/how-it-works.md**

Two edits in the frame bullet. First:

old:
```
  other harness: pressed mid-turn it arms and fires at the turn-complete
  marker (press again to cancel — the bar shows the armed state), then
```
new:
```
  other harness: pressed mid-turn it arms and fires at the turn boundary
  (press again to cancel — the bar shows the armed state), then
```

Second:

old:
```
  Where no marker could be wired (codex with a `notify` handler of your
  own, which tandem won't clobber) ~2s of transcript quiescence stands in;
  where one was wired, a 120s valve covers a marker that never arrives —
  far above any plausible tool-call silence, because firing early kills a
  live turn while firing late only costs a wait (and Ctrl-] cancels). The
```
new:
```
  With claude fronted, the boundary comes from claude's own session
  registry (`~/.claude/sessions/<pid>.json`, the data `claude agents
  --json` prints): `busy` holds the flip, anything else fires it — so an
  idle prompt flips instantly even though claude keeps appending
  housekeeping to its transcript between turns. With codex fronted the
  transcript-marker rules stand: where no marker could be wired (codex
  with a `notify` handler of your own, which tandem won't clobber) ~2s of
  transcript quiescence stands in; where one was wired, a 120s valve
  covers a marker that never arrives — far above any plausible tool-call
  silence, because firing early kills a live turn while firing late only
  costs a wait (and Ctrl-] cancels). The
```

- [ ] **Step 6: Commit**

```bash
git add src/tandem/runner.py tests/test_runner.py docs/how-it-works.md
git commit -m "feat: runner wires the claude status probe into the flip"
```

---

## Live validation checklist (operator, real terminal — after implementation)

- [ ] Flip while typing mid-session, minutes after a completed turn (the `away_summary` window): flips instantly.
- [ ] Flip immediately after launch/resume with nothing submitted: flips instantly.
- [ ] Flip mid-turn (model generating): bar shows armed state, flip fires at turn end.
- [ ] Flip while a permission dialog is open: record observed `status`/`waitingFor` in the registry file and decide whether abandoning the in-flight tool call is acceptable.
- [ ] Second flip-key press while armed still cancels.
