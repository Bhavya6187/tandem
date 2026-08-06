# In-Session Switch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Type `/tandem:switch` inside a live Claude Code or codex session and have the paired other harness take over the terminal seconds later; `/tandem:status` and `/tandem:doctor` work in-session too.

**Architecture:** The wrapper's tail loop (which already reads every live transcript line for sync) detects the *user-typed* switch command via a new per-adapter `switch_signal` classifier and arms an in-memory `SwitchMonitor`; the existing launch-injected turn marker (claude `--settings` Stop hook / codex `-c notify`, both touching a file) says when the turn is done; the runner then terminates the harness through a new `PtyControl` handle and `run_shell` reuses the existing `_switch` flip-and-resume path. The plugin gains three command files and codex packaging (commands only — no plugin hooks). Spec: `docs/superpowers/specs/2026-08-04-in-session-switch-design.md`.

**Tech Stack:** Python 3.11+, click, ptyprocess, pydantic events, pytest. Plugin: Claude Code plugin format, consumed by both Claude Code and codex ≥0.145.

## Global Constraints

- Switch command string: exactly `/tandem:switch`; trigger token: exactly `[tandem-switch-request]` (both defined once in `constants.py`).
- The switch trigger must key off **user-message entries only** — never assistant output, tool results, or replayed history from before this run (entry timestamp < runner spawn time).
- `switch_signal` must never mutate translation state (`parse_entry` owns that, exactly once, inside the sink).
- Quiescence window: 2.0 s; SIGTERM grace before SIGKILL: 3.0 s.
- Codex plugin-support version floor constant: `(0, 145, 0)` (verified locally on codex-cli 0.145.0).
- All work on branch `in-session-switch`. Run tests with `uv run pytest`. `docs/superpowers/` is in `.git/info/exclude`; spec and this plan were committed with `git add -f` — implementation commits never need `-f`.
- Do not touch `src/tandem/modelcat.py`, `tests/test_modelcat.py`, `tests/test_sub.py` — they carry unrelated pre-existing local changes.
- Plugin manifests keep version lockstep with `pyproject.toml` (existing `test_plugin.py` convention; the codex manifest joins it).

---

### Task 1: Switch-signal classifier (constants + adapters)

**Files:**
- Modify: `src/tandem/constants.py` (add two constants)
- Modify: `src/tandem/harness/base.py` (new abstract method after `hook_argv_extra`, ~line 65)
- Modify: `src/tandem/harness/claude_code.py`, `src/tandem/harness/codex.py`
- Test: `tests/test_switch_signal.py` (new)

**Interfaces:**
- Consumes: nothing new.
- Produces: `constants.SWITCH_COMMAND: str`, `constants.SWITCH_TOKEN: str`; `HarnessAdapter.switch_signal(raw: dict) -> tuple[str, str | None] | None` returning `("arm", ts)`, `("cancel", ts)`, or `None`, where `ts` is the entry's own ISO-8601 timestamp or None. Task 3's `SwitchMonitor` calls this.

- [ ] **Step 1: Write the failing tests**

```python
"""tests/test_switch_signal.py"""
from tandem.constants import SWITCH_COMMAND, SWITCH_TOKEN
from tandem.harness import get_adapter


def claude_user(text, ts="2026-08-05T10:00:00.000Z", **extra):
    return {"type": "user", "timestamp": ts,
            "message": {"role": "user", "content": text}, **extra}


def codex_user(text, ts="2026-08-05T10:00:00.000Z"):
    return {"type": "event_msg", "timestamp": ts,
            "payload": {"type": "user_message", "message": text}}


class TestClaudeSwitchSignal:
    def setup_method(self):
        self.a = get_adapter("claude")

    def test_raw_typed_command_arms(self):
        assert self.a.switch_signal(claude_user(SWITCH_COMMAND))[0] == "arm"

    def test_command_name_tag_arms(self):
        text = (f"<command-message>switch</command-message>"
                f"<command-name>{SWITCH_COMMAND}</command-name>")
        assert self.a.switch_signal(claude_user(text))[0] == "arm"

    def test_trigger_token_in_expanded_body_arms(self):
        assert self.a.switch_signal(
            claude_user(f"{SWITCH_TOKEN}\n\nThe user asked to switch."))[0] == "arm"

    def test_other_user_prompt_cancels(self):
        assert self.a.switch_signal(claude_user("fix the tests"))[0] == "cancel"

    def test_mentioning_the_command_midsentence_cancels(self):
        assert self.a.switch_signal(
            claude_user(f"what does {SWITCH_COMMAND} do?"))[0] == "cancel"

    def test_content_block_list_with_text_arms(self):
        raw = claude_user("x")
        raw["message"]["content"] = [{"type": "text", "text": SWITCH_COMMAND}]
        assert self.a.switch_signal(raw)[0] == "arm"

    def test_tool_result_user_entry_is_ignored(self):
        raw = claude_user("x")
        raw["message"]["content"] = [
            {"type": "tool_result", "tool_use_id": "toolu_1", "content": "ok"}]
        assert self.a.switch_signal(raw) is None

    def test_meta_entry_is_ignored(self):
        assert self.a.switch_signal(claude_user(SWITCH_COMMAND, isMeta=True)) is None

    def test_assistant_entry_is_ignored(self):
        raw = {"type": "assistant", "timestamp": "2026-08-05T10:00:00.000Z",
               "message": {"role": "assistant",
                           "content": [{"type": "text", "text": SWITCH_COMMAND}]}}
        assert self.a.switch_signal(raw) is None

    def test_timestamp_is_returned(self):
        assert self.a.switch_signal(claude_user(SWITCH_COMMAND))[1] == \
            "2026-08-05T10:00:00.000Z"


class TestCodexSwitchSignal:
    def setup_method(self):
        self.a = get_adapter("codex")

    def test_raw_typed_command_arms(self):
        assert self.a.switch_signal(codex_user(SWITCH_COMMAND))[0] == "arm"

    def test_trigger_token_arms(self):
        assert self.a.switch_signal(
            codex_user(f"{SWITCH_TOKEN}\nThe user asked to switch."))[0] == "arm"

    def test_other_user_message_cancels(self):
        assert self.a.switch_signal(codex_user("refactor this"))[0] == "cancel"

    def test_response_item_user_message_is_ignored(self):
        raw = {"type": "response_item", "timestamp": "2026-08-05T10:00:00.000Z",
               "payload": {"type": "message", "role": "user",
                           "content": [{"type": "input_text",
                                        "text": SWITCH_COMMAND}]}}
        assert self.a.switch_signal(raw) is None

    def test_agent_message_is_ignored(self):
        raw = {"type": "event_msg", "timestamp": "2026-08-05T10:00:00.000Z",
               "payload": {"type": "agent_message", "message": SWITCH_COMMAND}}
        assert self.a.switch_signal(raw) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_switch_signal.py -v`
Expected: FAIL — `ImportError: cannot import name 'SWITCH_COMMAND'` (then `AttributeError: switch_signal` once constants exist).

- [ ] **Step 3: Implement**

In `src/tandem/constants.py` add:

```python
# In-session switch (see docs/superpowers/specs/2026-08-04-in-session-switch-design.md)
SWITCH_COMMAND = "/tandem:switch"
SWITCH_TOKEN = "[tandem-switch-request]"
```

In `src/tandem/harness/base.py`, after `hook_argv_extra` (~line 69), add:

```python
    @abstractmethod
    def switch_signal(self, raw: dict[str, Any]) -> tuple[str, str | None] | None:
        """Classify one raw transcript entry for the in-session switch.

        ("arm", ts) when the entry is the user invoking /tandem:switch,
        ("cancel", ts) for any other user-typed prompt, None for
        everything else (assistant output, tool results, meta/system
        entries). ts is the entry's own ISO-8601 timestamp when present.
        Must not mutate translation state — parse_entry owns that."""
```

In `src/tandem/harness/claude_code.py` (import `SWITCH_COMMAND, SWITCH_TOKEN` from `..constants`):

```python
    def switch_signal(self, raw: dict[str, Any]) -> tuple[str, str | None] | None:
        if raw.get("type") != "user" or raw.get("isMeta"):
            return None
        content = (raw.get("message") or {}).get("content")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            if any(isinstance(b, dict) and b.get("type") == "tool_result"
                   for b in content):
                return None
            text = "\n".join(b.get("text", "") for b in content
                             if isinstance(b, dict) and b.get("type") == "text")
        else:
            return None
        if not text.strip():
            return None
        ts = raw.get("timestamp")
        return (_switch_kind(text), ts)
```

In `src/tandem/harness/codex.py` (same imports):

```python
    def switch_signal(self, raw: dict[str, Any]) -> tuple[str, str | None] | None:
        if raw.get("type") != "event_msg":
            return None
        payload = raw.get("payload") or {}
        if payload.get("type") != "user_message":
            return None
        text = payload.get("message") or ""
        if not text.strip():
            return None
        return (_switch_kind(text), raw.get("timestamp"))
```

The shared kind check lives in `src/tandem/harness/base.py` as a module-level function (both adapters import it from `.base`):

```python
def _switch_kind(text: str) -> str:
    from ..constants import SWITCH_COMMAND, SWITCH_TOKEN

    if (text.strip() == SWITCH_COMMAND
            or SWITCH_TOKEN in text
            or f"<command-name>{SWITCH_COMMAND}</command-name>" in text):
        return "arm"
    return "cancel"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_switch_signal.py -v`
Expected: PASS (all).

- [ ] **Step 5: Run the full suite, then commit**

Run: `uv run pytest`
Expected: no new failures (pre-existing failures in modelcat/sub tests, if any, are out of scope).

```bash
git add src/tandem/constants.py src/tandem/harness/base.py \
    src/tandem/harness/claude_code.py src/tandem/harness/codex.py \
    tests/test_switch_signal.py
git commit -m "feat: per-adapter switch_signal classifier for /tandem:switch"
```

---

### Task 2: PtyControl — cross-thread termination handle

**Files:**
- Modify: `src/tandem/ptyrun.py`
- Test: `tests/test_ptyrun.py` (extend)

**Interfaces:**
- Consumes: nothing new.
- Produces: `ptyrun.PtyControl` with `.terminate(grace: float = 3.0) -> None` and `.forced: bool`; `run_in_pty(argv, cwd=None, env=None, control: PtyControl | None = None) -> int`. Task 3's runner passes a control and calls `terminate()` from the tail thread.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_ptyrun.py`:

```python
import threading
import time

from tandem.ptyrun import PtyControl, run_in_pty


def test_control_terminates_a_running_child():
    # pytest has no tty, so this exercises the subprocess fallback path.
    control = PtyControl()
    result = {}

    def target():
        result["code"] = run_in_pty(["sleep", "30"], control=control)

    t = threading.Thread(target=target)
    t.start()
    deadline = time.time() + 5
    while not control.attached() and time.time() < deadline:
        time.sleep(0.02)
    assert control.attached()
    start = time.time()
    control.terminate(grace=2.0)
    t.join(timeout=5)
    assert not t.is_alive()
    assert time.time() - start < 5
    assert result["code"] != 0  # killed, not clean exit
    assert control.forced is False  # sleep dies on SIGTERM


def test_terminate_before_attach_is_a_noop():
    control = PtyControl()
    control.terminate(grace=0.1)  # nothing attached: must not raise
    assert control.forced is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_ptyrun.py -v`
Expected: FAIL — `ImportError: cannot import name 'PtyControl'`.

- [ ] **Step 3: Implement**

In `src/tandem/ptyrun.py` add (top-level, after `_winsize`; add `import time` to imports):

```python
class PtyControl:
    """Cross-thread handle to terminate the child run_in_pty is running.

    `forced` records whether SIGKILL escalation was needed (surfaced so
    the switch path can report a non-graceful handoff)."""

    def __init__(self):
        self._child = None   # PtyProcess (tty path)
        self._proc = None    # subprocess.Popen (fallback path)
        self.forced = False

    def attached(self) -> bool:
        return self._child is not None or self._proc is not None

    def terminate(self, grace: float = 3.0) -> None:
        if not self.attached():
            return
        self._signal(signal.SIGTERM)
        deadline = time.time() + grace
        while time.time() < deadline:
            if not self._alive():
                return
            time.sleep(0.05)
        self.forced = True
        self._signal(signal.SIGKILL)

    def _alive(self) -> bool:
        try:
            if self._child is not None:
                return self._child.isalive()
            if self._proc is not None:
                return self._proc.poll() is None
        except Exception:
            pass
        return False

    def _signal(self, sig) -> None:
        try:
            if self._child is not None:
                # PtyProcess.spawn setsids the child: pid == pgid, so the
                # whole harness process group (tool children too) gets it.
                os.killpg(self._child.pid, sig)
            elif self._proc is not None:
                self._proc.send_signal(sig)
        except (ProcessLookupError, PermissionError):
            pass
        except Exception:
            pass
```

Change `run_in_pty`'s signature and wire the control in both paths:

```python
def run_in_pty(
    argv: list[str],
    cwd: str | None = None,
    env: dict | None = None,
    control: "PtyControl | None" = None,
) -> int:
```

Fallback path (replace the `subprocess.run` line — Popen so the control can signal it):

```python
    if not is_tty:
        proc = subprocess.Popen(argv, cwd=cwd, env=env)
        if control is not None:
            control._proc = proc
        return proc.wait()
```

TTY path, right after `child = PtyProcess.spawn(...)`:

```python
    if control is not None:
        control._child = child
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_ptyrun.py -v`
Expected: PASS (new and pre-existing).

- [ ] **Step 5: Commit**

```bash
git add src/tandem/ptyrun.py tests/test_ptyrun.py
git commit -m "feat: PtyControl terminate handle for run_in_pty"
```

---

### Task 3: SwitchMonitor + runner integration

**Files:**
- Modify: `src/tandem/runner.py` (SwitchMonitor class; TailLoop `on_line` param; InteractiveRunner wiring)
- Test: `tests/test_switch_monitor.py` (new)

**Interfaces:**
- Consumes: `adapter.switch_signal(raw)` (Task 1), `PtyControl` (Task 2).
- Produces: `runner.SwitchMonitor(adapter, spawn_time)` with `.note_line(raw: dict | None) -> None` and `.should_fire(marker_mtime: float | None) -> bool`; `TailLoop(..., on_line: Callable[[dict | None], None] | None = None)`; `InteractiveRunner.switch_requested: bool` (set before termination). Task 4 reads `switch_requested`.

- [ ] **Step 1: Write the failing tests**

```python
"""tests/test_switch_monitor.py"""
import time

from tandem.harness import get_adapter
from tandem.runner import SwitchMonitor


def user(text, ts=None):
    return {"type": "user", "timestamp": ts,
            "message": {"role": "user", "content": text}}


def make(spawn_offset=-5.0):
    return SwitchMonitor(get_adapter("claude"), time.time() + spawn_offset)


def test_unarmed_never_fires():
    m = make()
    assert m.should_fire(marker_mtime=time.time()) is False


def test_arm_then_marker_after_arm_fires():
    m = make()
    m.note_line(user("/tandem:switch"))
    assert m.should_fire(marker_mtime=time.time() + 1) is True


def test_arm_with_stale_marker_does_not_fire_yet():
    m = make()
    m.note_line(user("/tandem:switch"))
    assert m.should_fire(marker_mtime=time.time() - 60) is False


def test_missing_marker_fires_after_quiescence():
    m = make()
    m.note_line(user("/tandem:switch"))
    m.armed_at -= 3.0       # simulate 3s having passed since arming
    m.last_line_at -= 3.0   # and no transcript lines since
    assert m.should_fire(marker_mtime=None) is True


def test_recent_lines_hold_off_quiescence_fire():
    m = make()
    m.note_line(user("/tandem:switch"))
    m.armed_at -= 3.0
    m.note_line(None)  # a fresh line just drained
    assert m.should_fire(marker_mtime=None) is False


def test_later_user_prompt_cancels():
    m = make()
    m.note_line(user("/tandem:switch"))
    m.note_line(user("actually wait, one more thing"))
    assert m.should_fire(marker_mtime=time.time() + 1) is False


def test_replayed_history_before_spawn_never_arms():
    m = SwitchMonitor(get_adapter("claude"), time.time())
    m.note_line(user("/tandem:switch", ts="2020-01-01T00:00:00.000Z"))
    assert m.should_fire(marker_mtime=time.time() + 1) is False


def test_non_user_lines_do_not_change_armed_state():
    m = make()
    m.note_line(user("/tandem:switch"))
    m.note_line({"type": "assistant", "message": {
        "role": "assistant", "content": [{"type": "text", "text": "ok"}]}})
    assert m.armed_at is not None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_switch_monitor.py -v`
Expected: FAIL — `ImportError: cannot import name 'SwitchMonitor'`.

- [ ] **Step 3: Implement SwitchMonitor**

In `src/tandem/runner.py` (add `from datetime import datetime` to imports), before `InteractiveRunner`:

```python
def _iso_to_epoch(ts: str | None) -> float | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


class SwitchMonitor:
    """In-memory arm/cancel state for the in-session switch.

    Arms on the user's /tandem:switch transcript entry (never on model
    output), cancels on any later user prompt, and ignores entries whose
    own timestamp predates this runner's spawn (replayed history). Fires
    when the launch-injected turn marker was touched after arming, or —
    marker unavailable — after QUIESCENCE seconds with no new transcript
    lines. Single-threaded: only the tail thread calls it."""

    QUIESCENCE = 2.0

    def __init__(self, adapter, spawn_time: float):
        self.adapter = adapter
        self.spawn_time = spawn_time
        self.armed_at: float | None = None
        self.last_line_at: float = spawn_time

    def note_line(self, raw: dict | None) -> None:
        self.last_line_at = time.time()
        if raw is None:
            return
        sig = self.adapter.switch_signal(raw)
        if sig is None:
            return
        kind, ts = sig
        epoch = _iso_to_epoch(ts)
        if epoch is not None and epoch < self.spawn_time:
            return
        self.armed_at = time.time() if kind == "arm" else None

    def should_fire(self, marker_mtime: float | None) -> bool:
        if self.armed_at is None:
            return False
        if marker_mtime is not None and marker_mtime >= self.armed_at:
            return True
        now = time.time()
        return (now - self.armed_at >= self.QUIESCENCE
                and now - self.last_line_at >= self.QUIESCENCE)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_switch_monitor.py -v`
Expected: PASS.

- [ ] **Step 5: Thread `on_line` through TailLoop**

In `TailLoop.__init__` (runner.py:90) add a final keyword parameter `on_line=None`, stored as `self.on_line = on_line`. In `TailLoop.drain` (runner.py:110), inside the per-line loop, immediately after the sink has handled a line and the cursor advanced, add:

```python
            if self.on_line is not None:
                self.on_line(line.raw)
```

(Read the loop body first; place the call so it runs once per tailed line, after `sink.handle`.)

- [ ] **Step 6: Wire the monitor into InteractiveRunner**

In `InteractiveRunner`:

- `__init__`: add `self.switch_requested = False`.
- In `run()`, after `spawn_time = time.time()` add:

```python
        monitor = SwitchMonitor(adapter, spawn_time)
        control = PtyControl()
```

(`PtyControl` imported at top: extend `from .ptyrun import run_in_pty` to `from .ptyrun import PtyControl, run_in_pty`.)

- Environment pinning: build the child env and pass control (replaces the bare `run_in_pty(argv, cwd=session.cwd)` call):

```python
        env = dict(os.environ)
        env["TANDEM_SESSION_ID"] = session.tandem_id
        ...
            code = run_in_pty(argv, cwd=session.cwd, env=env, control=control)
```

(`import os` at module top.)

- In `tail_thread`, construct the loop with the callback: `loop = TailLoop(store, current, active, path, sink, on_line=monitor.note_line)`, and change the drain loop to check the monitor each cycle:

```python
                    while not stop.is_set():
                        with ops._sub_lock():
                            loop.drain()
                        if monitor.should_fire(_marker_mtime(sentinel)):
                            self.switch_requested = True
                            control.terminate()
                            break
                        watcher.wait()
```

with a module-level helper:

```python
def _marker_mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None
```

The code after the loop (final drain, watcher.stop, sink.close) is unchanged — after `terminate()` the child dies, `run_in_pty` returns, the foreground `finally` sets `stop` and joins, and the final drain picks up any last lines. Note for the reviewer: `codex` sessions launched fresh take the `await_codex_rollout` path before `loop` exists — the monitor wiring lives where `TailLoop` is constructed, which both paths share.

- [ ] **Step 7: Run the full suite**

Run: `uv run pytest`
Expected: no new failures (sync/runner tests construct TailLoop positionally; the new param is keyword-only-by-position at the end, so they pass unchanged).

- [ ] **Step 8: Commit**

```bash
git add src/tandem/runner.py tests/test_switch_monitor.py
git commit -m "feat: SwitchMonitor arms on typed /tandem:switch and terminates at turn end"
```

---

### Task 4: Surface the switch through shell.py

**Files:**
- Modify: `src/tandem/shell.py`
- Modify: `src/tandem/cli.py:629-632` (`_enter_session` default `run_harness` unchanged — it lives in shell; verify only)
- Test: `tests/test_shell.py` (extend + adapt seams)

**Interfaces:**
- Consumes: `InteractiveRunner.switch_requested` (Task 3).
- Produces: `run_harness(session) -> tuple[int, bool]` contract (code, switch_requested); `_enter` and `_switch` return `tuple[int, bool]`; new `_settle(first, tandem_id, run_harness, code) -> int` loops flips while sessions exit requesting a switch.

- [ ] **Step 1: Adapt the test seams and write the failing test**

In `tests/test_shell.py`, existing injected `run_harness` fakes return `int`; update every fake to return `(code, False)` (mechanical: wrap existing return values). Then add:

```python
def test_switch_requested_by_runner_flips_without_prompt_input(sess, monkeypatch):
    """A harness exit carrying switch_requested=True must flip roles and
    re-enter immediately — no prompt interaction in between."""
    calls = []

    def run_harness(session):
        calls.append(session.active)
        # first entry requests a switch; the re-entered session exits plain
        return (0, len(calls) == 1)

    def input_fn(prompt):
        raise EOFError  # reached only after the auto-switch settles

    from tandem.shell import run_shell
    code = run_shell(sess.tandem_id, None, input_fn=input_fn,
                     run_harness=run_harness)
    assert calls == ["claude", "codex"]  # entered, auto-flipped, re-entered
    assert code == 0
```

(Match the existing tests' fixture idioms — `sess` pairs a session with `claude` active; see `test_switch_flips_and_reenters` for the pattern.)

- [ ] **Step 2: Run tests to verify the new one fails**

Run: `uv run pytest tests/test_shell.py -v`
Expected: the new test FAILS (run_shell treats the tuple as an int today); adapted old tests may error until Step 3.

- [ ] **Step 3: Implement**

In `src/tandem/shell.py`:

- `_enter` returns a tuple — change its tail to `return run_harness(session)` and both failure paths to `return code, False`; docstring: "Returns (exit code, switch_requested)."
- `_switch` returns `_enter(tandem_id, run_harness, code)` on success and `return code, False` on both failure paths.
- Add:

```python
def _settle(first, tandem_id, run_harness, code) -> int:
    """Run one session entry via `first` (_enter or _switch), then keep
    flipping for as long as each session exits asking to switch."""
    code, want_switch = first(tandem_id, run_harness, code)
    while want_switch:
        code, want_switch = _switch(tandem_id, run_harness, code)
    return code
```

- Update the three call sites in `run_shell`:
  - initial entry: `code = _settle(_enter, tandem_id, run_harness, code)`
  - `("", "resume")` branch: `code = _settle(_enter, tandem_id, run_harness, code)`
  - `["switch"]` branch: `code = _settle(_switch, tandem_id, run_harness, code)`
- The default `run_harness` closure becomes:

```python
        def run_harness(session):
            from .runner import InteractiveRunner

            runner = InteractiveRunner(session, sink_factory=sink_factory)
            code = runner.run()
            return code, runner.switch_requested
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_shell.py -v`
Expected: PASS (all, including adapted seams).

- [ ] **Step 5: Full suite, then commit**

Run: `uv run pytest`

```bash
git add src/tandem/shell.py tests/test_shell.py
git commit -m "feat: auto-flip when a harness session exits requesting a switch"
```

---

### Task 5: Session pinning via TANDEM_SESSION_ID

**Files:**
- Modify: `src/tandem/cli.py:31-35` (`_resolve_session`)
- Test: `tests/test_cli.py` (extend)

**Interfaces:**
- Consumes: the env var Task 3's runner exports (`TANDEM_SESSION_ID=<tandem_id>`).
- Produces: `_resolve_session` preference order: `_SESSION_ID` (shell dispatch) → `TANDEM_SESSION_ID` env (in-harness calls) → cwd-MRU fallback.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_cli.py` (follow its existing store/session fixture idioms):

```python
def test_resolve_session_prefers_env_pin_over_cwd_mru(tmp_path, monkeypatch):
    """Two sessions in one directory: a command run inside harness A must
    resolve to A even when B is the cwd-MRU."""
    from tandem import cli
    from tandem.state import StateStore

    monkeypatch.setattr(cli, "_cwd", lambda: str(tmp_path))
    with StateStore() as store:
        a = store.create_session(str(tmp_path), active="claude")
        b = store.create_session(str(tmp_path), active="claude")
        store.touch_used(b.tandem_id)  # b is now the cwd-MRU
        monkeypatch.setenv("TANDEM_SESSION_ID", a.tandem_id)
        assert cli._resolve_session(store).tandem_id == a.tandem_id


def test_resolve_session_ignores_env_pin_for_unknown_id(tmp_path, monkeypatch):
    from tandem import cli
    from tandem.state import StateStore

    monkeypatch.setattr(cli, "_cwd", lambda: str(tmp_path))
    with StateStore() as store:
        a = store.create_session(str(tmp_path), active="claude")
        monkeypatch.setenv("TANDEM_SESSION_ID", "no-such-session")
        assert cli._resolve_session(store).tandem_id == a.tandem_id
```

(Adjust `create_session` to the actual `StateStore` API — read `tests/test_state.py` for the canonical construction; the state tests show how sessions are created in tests. Keep the assertions as written.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_cli.py -k resolve_session -v`
Expected: FAIL — env pin ignored, first test resolves to `b`.

- [ ] **Step 3: Implement**

In `src/tandem/cli.py` (add `import os` at top if not present):

```python
def _resolve_session(store: StateStore) -> PairedSession | None:
    if _SESSION_ID is not None:
        return store.get_session(_SESSION_ID)
    env_id = os.environ.get("TANDEM_SESSION_ID")
    if env_id:
        session = store.get_session(env_id)
        if session is not None:
            return session
    return store.latest_session_for_cwd(_cwd())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_cli.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/tandem/cli.py tests/test_cli.py
git commit -m "feat: pin in-harness tandem commands to their session via TANDEM_SESSION_ID"
```

---

### Task 6: Plugin command files + codex packaging

**Files:**
- Create: `plugin/commands/switch.md`, `plugin/commands/status.md`, `plugin/commands/doctor.md`
- Create: `plugin/.codex-plugin/plugin.json`, `.codex-plugin/marketplace.json` (repo root)
- Test: `tests/test_plugin.py` (extend)

**Interfaces:**
- Consumes: `SWITCH_TOKEN` literal (must byte-match `constants.SWITCH_TOKEN`).
- Produces: `/tandem:switch`, `/tandem:status`, `/tandem:doctor` as plugin commands in both harnesses; codex manifests that `plugin_setup` (Task 7) and `doctor` (Task 8) reference.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_plugin.py` (reuse its existing `PLUGIN`/repo-root path helpers):

```python
def test_switch_command_carries_the_trigger_token():
    from tandem.constants import SWITCH_TOKEN

    body = (PLUGIN / "commands" / "switch.md").read_text()
    assert SWITCH_TOKEN in body


def test_status_and_doctor_commands_allowlist_exactly_their_command():
    for name in ("status", "doctor"):
        text = (PLUGIN / "commands" / f"{name}.md").read_text()
        assert f"allowed-tools: Bash(tandem {name}:*)" in text


def test_switch_command_does_not_allowlist_bash():
    text = (PLUGIN / "commands" / "switch.md").read_text()
    assert "allowed-tools" not in text


def test_codex_plugin_manifest_mirrors_the_claude_one():
    import json

    claude = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text())
    codex = json.loads((PLUGIN / ".codex-plugin" / "plugin.json").read_text())
    assert codex["name"] == claude["name"] == "tandem"
    assert codex["version"] == claude["version"]


def test_codex_marketplace_points_at_the_plugin_directory():
    import json

    root = PLUGIN.parent
    market = json.loads((root / ".codex-plugin" / "marketplace.json").read_text())
    entry = market["plugins"][0]
    assert entry["name"] == "tandem"
    assert (root / entry["source"]).resolve() == PLUGIN.resolve()
```

(If `test_plugin.py` names its plugin-dir constant differently, use its name; do not invent a second helper.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_plugin.py -v`
Expected: new tests FAIL — files missing.

- [ ] **Step 3: Create the command files**

`plugin/commands/switch.md`:

```markdown
---
description: Switch this paired tandem session to the other harness
---
[tandem-switch-request]

The user asked to switch this tandem session to the other harness
(claude ⇄ codex). The line above is a machine marker: when this session
runs under the `tandem` wrapper, tandem detects it in the transcript and
performs the switch automatically when this turn ends. Do not run any
command — the switch is not yours to execute.

Reply with exactly one short line confirming the switch, e.g.
"Switching — the other harness will pick up right here." Then add one
brief note: if nothing happens within a few seconds, this session is not
running under tandem; launch one with `tandem resume`.
```

`plugin/commands/status.md`:

```markdown
---
description: Show the tandem pairing status for this directory
allowed-tools: Bash(tandem status:*)
---
Run `tandem status` with Bash and relay its output verbatim in a fenced
code block. If it reports there is no session for this directory, tell
the user this directory has no tandem pairing and that running `tandem`
starts one.
```

`plugin/commands/doctor.md`:

```markdown
---
description: Run tandem's health checks for this paired session
allowed-tools: Bash(tandem doctor:*)
---
Run `tandem doctor` with Bash and relay its output verbatim in a fenced
code block. Summarize in one line whether anything needs attention. If
it reports there is no session for this directory, tell the user this
directory has no tandem pairing and that running `tandem` starts one.
```

- [ ] **Step 4: Create the codex manifests**

`plugin/.codex-plugin/plugin.json` (version must equal `plugin/.claude-plugin/plugin.json`'s, currently `0.1.10`):

```json
{
  "name": "tandem",
  "description": "Drive a paired tandem session from inside codex: /tandem:switch, /tandem:status, /tandem:doctor.",
  "version": "0.1.10",
  "author": {"name": "tandem"}
}
```

`.codex-plugin/marketplace.json` (repo root):

```json
{
  "name": "tandem",
  "description": "tandem's plugin marketplace: one paired session across Claude Code and codex.",
  "owner": {
    "name": "Bhavya Agarwal",
    "url": "https://github.com/Bhavya6187"
  },
  "plugins": [
    {
      "name": "tandem",
      "source": "./plugin",
      "description": "In-session tandem commands (switch/status/doctor) and codex-model subagent routing."
    }
  ]
}
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_plugin.py -v`
Expected: PASS, including the pre-existing version-lockstep test still passing.

- [ ] **Step 6: Commit**

```bash
git add plugin/commands plugin/.codex-plugin .codex-plugin tests/test_plugin.py
git commit -m "feat: in-session command files and codex plugin packaging"
```

---

### Task 7: `tandem plugin install` learns codex

**Files:**
- Modify: `src/tandem/plugin_setup.py`
- Modify: the `plugin install` CLI command (find it: `grep -n "plugin" src/tandem/cli.py src/tandem/plugin_setup.py` — the command that currently shells out to `claude plugin marketplace add` / `claude plugin install`)
- Test: `tests/test_plugin_setup.py` (extend)

**Interfaces:**
- Consumes: `_run(cmd)` helper and `MARKETPLACE_REPO`/`PLUGIN_ID` constants already in `plugin_setup.py:23-24`.
- Produces: `plugin_setup.is_codex_plugin_installed() -> bool`, `plugin_setup.install_codex_plugin() -> bool`. Task 8's doctor calls `is_codex_plugin_installed`.

- [ ] **Step 1: Pin the exact codex CLI syntax**

Run: `codex plugin marketplace --help` and `codex plugin add --help`.
Expected shape (from codex-cli 0.145.0): `codex plugin marketplace add <source>` and `codex plugin add <plugin>[@<marketplace>]`. Record the exact argv forms; if they differ from the code below, adjust the code and tests to the real forms before writing them.

- [ ] **Step 2: Write the failing tests**

Add to `tests/test_plugin_setup.py` (mirror its existing mocking style for `_run` and config paths — read the file first; it already fakes `claude` invocations, follow the same pattern):

```python
def test_is_codex_plugin_installed_reads_config_toml(tmp_path, monkeypatch):
    from tandem import plugin_setup

    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    monkeypatch.setattr(plugin_setup.paths, "codex_home", lambda: codex_home)

    (codex_home / "config.toml").write_text("")
    assert plugin_setup.is_codex_plugin_installed() is False

    (codex_home / "config.toml").write_text(
        '[plugins."tandem@tandem"]\nenabled = true\n')
    assert plugin_setup.is_codex_plugin_installed() is True


def test_install_codex_plugin_runs_marketplace_add_then_plugin_add(monkeypatch):
    from tandem import plugin_setup

    calls = []

    def fake_run(cmd):
        calls.append(cmd)
        class P:  # matches _run's CompletedProcess contract
            returncode = 0
        return P()

    monkeypatch.setattr(plugin_setup, "_run", fake_run)
    assert plugin_setup.install_codex_plugin() is True
    assert calls == [
        ["codex", "plugin", "marketplace", "add", plugin_setup.MARKETPLACE_REPO],
        ["codex", "plugin", "add", plugin_setup.CODEX_PLUGIN_ID],
    ]


def test_install_codex_plugin_reports_failure(monkeypatch):
    from tandem import plugin_setup

    monkeypatch.setattr(plugin_setup, "_run", lambda cmd: None)  # codex missing
    assert plugin_setup.install_codex_plugin() is False
```

(`plugin_setup` must import `paths` for the first test's monkeypatch target — the implementation below does. If `is_plugin_installed` reads claude config via a different seam, keep the codex reader symmetric with it.)

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_plugin_setup.py -v`
Expected: FAIL — missing attributes.

- [ ] **Step 4: Implement**

In `src/tandem/plugin_setup.py` (add `from . import paths` and `import tomllib` to imports; keep `CODEX_PLUGIN_ID = "tandem@tandem"` next to `PLUGIN_ID`):

```python
CODEX_PLUGIN_ID = "tandem@tandem"


def is_codex_plugin_installed() -> bool:
    """True when codex's config.toml registers the tandem plugin."""
    try:
        with open(paths.codex_home() / "config.toml", "rb") as f:
            return CODEX_PLUGIN_ID in tomllib.load(f).get("plugins", {})
    except (OSError, ValueError):
        return False


def install_codex_plugin() -> bool:
    """Register tandem's marketplace with codex and install the plugin.
    Returns True on success; False (never raises) when codex is missing
    or either step fails."""
    add = _run(["codex", "plugin", "marketplace", "add", MARKETPLACE_REPO])
    if add is None or add.returncode != 0:
        return False
    ins = _run(["codex", "plugin", "add", CODEX_PLUGIN_ID])
    return ins is not None and ins.returncode == 0
```

Then extend the `tandem plugin install` CLI command: after the existing claude steps, call `install_codex_plugin()`; echo `codex plugin: installed` on success and a yellow warning `codex plugin: skipped (codex missing or install failed) — in-session commands unavailable in codex` on failure. Codex failure must NOT change the command's exit code (claude result decides, preserving today's behavior).

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_plugin_setup.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/tandem/plugin_setup.py src/tandem/cli.py tests/test_plugin_setup.py
git commit -m "feat: tandem plugin install covers the codex plugin"
```

---

### Task 8: Doctor checks — plugin presence and codex floor

**Files:**
- Modify: `src/tandem/doctor.py`
- Test: `tests/test_memory_doctor.py` or the file where `run_doctor` report tests live (locate with `grep -rn "run_doctor" tests/`); extend there. If none exists, create `tests/test_doctor_plugin_checks.py`.

**Interfaces:**
- Consumes: `plugin_setup.is_plugin_installed()`, `plugin_setup.is_codex_plugin_installed()` (Task 7), `DoctorReport.ok/warn` (doctor.py:135-141).
- Produces: `doctor.CODEX_PLUGIN_FLOOR = (0, 145, 0)`; `_plugin_checks(report)` wired into `run_doctor`; `_codex_version() -> tuple[int, ...] | None`.

- [ ] **Step 1: Write the failing tests**

```python
"""tests/test_doctor_plugin_checks.py"""
from tandem import doctor


class Report:
    def __init__(self):
        self.oks, self.warns = [], []

    def ok(self, msg):
        self.oks.append(msg)

    def warn(self, msg):
        self.warns.append(msg)


def test_codex_version_parses_the_cli_banner(monkeypatch):
    monkeypatch.setattr(doctor, "_codex_version_output", lambda: "codex-cli 0.145.0")
    assert doctor._codex_version() == (0, 145, 0)


def test_codex_version_none_when_codex_missing(monkeypatch):
    monkeypatch.setattr(doctor, "_codex_version_output", lambda: None)
    assert doctor._codex_version() is None


def test_plugin_checks_warn_when_missing_and_old(monkeypatch):
    from tandem import plugin_setup

    monkeypatch.setattr(plugin_setup, "is_plugin_installed", lambda: False)
    monkeypatch.setattr(plugin_setup, "is_codex_plugin_installed", lambda: False)
    monkeypatch.setattr(doctor, "_codex_version", lambda: (0, 140, 0))
    report = Report()
    doctor._plugin_checks(report)
    assert len(report.warns) == 3  # claude plugin, codex plugin, version floor
    assert any("0.145" in w for w in report.warns)


def test_plugin_checks_ok_when_present_and_new(monkeypatch):
    from tandem import plugin_setup

    monkeypatch.setattr(plugin_setup, "is_plugin_installed", lambda: True)
    monkeypatch.setattr(plugin_setup, "is_codex_plugin_installed", lambda: True)
    monkeypatch.setattr(doctor, "_codex_version", lambda: (0, 150, 2))
    report = Report()
    doctor._plugin_checks(report)
    assert report.warns == []
    assert len(report.oks) == 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_doctor_plugin_checks.py -v`
Expected: FAIL — missing attributes.

- [ ] **Step 3: Implement**

In `src/tandem/doctor.py` (add `import re`, `import subprocess` as needed):

```python
CODEX_PLUGIN_FLOOR = (0, 145, 0)


def _codex_version_output() -> str | None:
    try:
        out = subprocess.run(["codex", "--version"], capture_output=True,
                             text=True, timeout=10)
        return out.stdout.strip() if out.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def _codex_version() -> tuple[int, ...] | None:
    banner = _codex_version_output()
    if not banner:
        return None
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", banner)
    return tuple(int(g) for g in m.groups()) if m else None


def _plugin_checks(report) -> None:
    from . import plugin_setup

    if plugin_setup.is_plugin_installed():
        report.ok("claude plugin installed (/tandem:switch available)")
    else:
        report.warn("claude plugin not installed — in-session commands "
                    "unavailable in Claude Code; run `tandem plugin install`")
    if plugin_setup.is_codex_plugin_installed():
        report.ok("codex plugin installed (/tandem:switch available)")
    else:
        report.warn("codex plugin not installed — in-session commands "
                    "unavailable in codex; run `tandem plugin install`")
    v = _codex_version()
    if v is not None and v < CODEX_PLUGIN_FLOOR:
        floor = ".".join(map(str, CODEX_PLUGIN_FLOOR))
        report.warn(f"codex {'.'.join(map(str, v))} predates plugin support; "
                    f"in-session switch from codex needs >= {floor}")
```

Wire `_plugin_checks(report)` into `run_doctor` (doctor.py:149) alongside the existing check groups (after `_subagent_checks` is a natural spot).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_doctor_plugin_checks.py -v` then `uv run pytest`
Expected: PASS; no new failures elsewhere.

- [ ] **Step 5: Commit**

```bash
git add src/tandem/doctor.py tests/test_doctor_plugin_checks.py
git commit -m "feat: doctor checks plugin presence and codex plugin-support floor"
```

---

### Task 9: Live validation (manual, operator-run)

**Files:**
- Modify: `docs/superpowers/plans/2026-08-05-in-session-switch.md` (check the boxes with observed results)
- Possibly modify: `src/tandem/harness/claude_code.py` / `codex.py` `switch_signal` + `tests/test_switch_signal.py` fixtures, if observed transcript forms differ

**Interfaces:** none — this validates Tasks 1-8 end to end.

This task needs an interactive terminal and both CLIs logged in; it is run by the operator (or a subagent with explicit interactive access), not asserted in pytest.

- [ ] **Step 1: Install the dev plugin into both harnesses**

```bash
claude plugin marketplace add /Users/bhavya/git/tandem && claude plugin install tandem@tandem
codex plugin marketplace add /Users/bhavya/git/tandem && codex plugin add tandem@tandem
```

(If already installed from GitHub, `claude plugin marketplace update tandem` after pushing the branch instead. Note which route was used.)

- [ ] **Step 2: claude → codex round trip**

In a scratch repo: `tandem`, ask claude something trivial, then type `/tandem:switch`.
Expected: claude replies one line, exits within ~3 s, codex resumes showing the same conversation. Then `/tandem:switch` in codex flips back (codex → claude). Record both.

- [ ] **Step 3: Pin the real transcript forms**

Open the claude session jsonl (`~/.claude/projects/<cwd-slug>/<session-id>.jsonl`) and the codex rollout (`~/.codex/sessions/**/rollout-*.jsonl`) from Step 2. Find the user entry for the typed `/tandem:switch`. Verify `switch_signal`'s matcher hit the actual form (raw command, `<command-name>` tag, or `[tandem-switch-request]` token). If the real form differs, update the adapter matcher and the fixtures in `tests/test_switch_signal.py` to the observed shape, rerun `uv run pytest`, and commit `fix: match observed transcript form for /tandem:switch`.

- [ ] **Step 4: Cancel rule**

`/tandem:switch`, then immediately type another prompt before the switch lands.
Expected: if the second prompt was submitted before termination, the switch cancels and the session stays; a re-issued `/tandem:switch` then works. (Timing-dependent — if the switch already fired, that's the fast path, not a failure; retry to observe the cancel.)

- [ ] **Step 5: Edge cases**

- Plain `claude` (no wrapper) in the scratch repo: `/tandem:switch` → model's one-liner says nothing will happen without the wrapper; nothing else occurs.
- `/tandem:status` and `/tandem:doctor` inside the wrapper session: output relayed, no permission prompt on claude; on codex, note whether the state-store read triggered an approval (spec's open verification — record the answer in the spec's plan-time note).
- Two `tandem` sessions in one directory: `/tandem:status` in each reports its own session id (env pinning).

- [ ] **Step 6: Commit the record**

Check the boxes above with one-line observed results appended, then:

```bash
git add -f docs/superpowers/plans/2026-08-05-in-session-switch.md
git commit -m "docs: live-validation record for in-session switch"
```

---

## Self-Review (completed)

- **Spec coverage:** trigger/detector → Task 1+3; turn marker + quiescence + termination hardening → Tasks 2-3; flip/re-enter chain → Task 4; session pinning → Task 5; plugin commands + codex packaging → Task 6; install flow → Task 7; doctor floor/presence → Task 8; the spec's three plan-time verifications → Task 9 Steps 3 and 5 (transcript forms, codex sandbox behavior) and the floor constant in Task 8. Escape-hatch behavior needs no task (unchanged paths).
- **Placeholder scan:** none; the two "read the file first" notes (TailLoop drain placement, test fixture idioms) point at exact locations with the code to insert given.
- **Type consistency:** `switch_signal` tuple contract (Task 1) matches `SwitchMonitor.note_line` consumption (Task 3); `run_harness -> tuple[int, bool]` (Task 4) matches the runner attribute (Task 3); `CODEX_PLUGIN_ID` (Task 7) matches doctor usage (Task 8); `SWITCH_TOKEN` literal in switch.md (Task 6) is asserted equal to the constant by test.
