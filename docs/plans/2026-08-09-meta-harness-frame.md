# Meta-Harness Frame Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Launch `tandem` once and never leave it: Ctrl-] flips the screen between the native Claude Code and Codex TUIs (~1–3 s, history intact via the existing sync engine), with a one-row status bar on the terminal's bottom line.

**Architecture:** Three layers, none of which render conversation UI. `frame.py` (new) holds pure bytes-in/bytes-out state machines: flip-key detection on the input stream, a reset-watching guard on the output stream, and the bar's byte composition. `ptyrun.py` grows optional frame wiring (reserved bottom row via a `rows-1` winsize lie, scroll-region protection) and a `PtyControl` cross-thread termination handle. `runner.py` adds a `FlipMonitor` thread (armed flag → turn-boundary wait → termination ladder) and `shell.py` re-enters the other harness on flip without stopping at the tandem prompt. Spec: `docs/specs/2026-08-09-meta-harness-frame-design.md`.

**Tech Stack:** Python 3.11+, ptyprocess, click, pytest. No new dependencies.

## Global Constraints

- No new dependencies; stdlib + existing deps only.
- Config errors never break a launch: any malformed `[frame]` value falls back to its default (house rule from `src/tandem/config.py` module docstring).
- tandem never parses meaning from terminal bytes except the exact sequences enumerated in `frame.py` (flip byte, bracketed-paste markers, SGR mouse, RIS/DECSTBM/alt-screen/ED). Everything else relays verbatim.
- The bar activates only when: stdin is a tty AND `[frame] bar` is true AND terminal rows ≥ 5. The flip works with or without the bar.
- The session must never be lost: every failure path lands at the tandem prompt with the resume hint (existing `shell.run_shell` finally-guarantee — do not disturb it).
- Test seams: `shell.run_shell(input_fn=, run_harness=)` and monkeypatched `runner.run_in_pty` are the established patterns — reuse them, don't invent new ones.
- Existing behavior is regression-scope: plain exits still reach the tandem prompt, `switch` typed at the prompt still works, non-tty `run_in_pty` fallback unchanged.
- Commit messages follow the repo style (`feat:` / `docs:` prefixes, imperative, lowercase) and end with `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.
- Run tests with `uv run pytest <path> -v` from the repo root (`/Users/bhavya/git/tandem`).

---

### Task 1: `[frame]` config table

**Files:**
- Modify: `src/tandem/config.py` (append after `load_harness_args`, ~line 81)
- Test: `tests/test_config.py` (append)

**Interfaces:**
- Produces: `FrameConfig` frozen dataclass with `flip_byte: int = 0x1D`, `bar: bool = True`; `load_frame_config() -> FrameConfig`. Task 9 imports both from `tandem.config`.
- Consumes: existing `_read_config()` in the same file.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_config.py` (it already has `paths`/`tmp` fixtures in scope via `monkeypatch.setenv("TANDEM_HOME", ...)` — follow the file's existing pattern for writing `config.toml`; look at its first test for the exact fixture idiom and reuse it):

```python
from tandem.config import FrameConfig, load_frame_config


def _write_config(tmp_path, monkeypatch, text):
    monkeypatch.setenv("TANDEM_HOME", str(tmp_path))
    (tmp_path / "config.toml").write_text(text)


def test_frame_defaults_without_config(tmp_path, monkeypatch):
    monkeypatch.setenv("TANDEM_HOME", str(tmp_path))
    cfg = load_frame_config()
    assert cfg == FrameConfig(flip_byte=0x1D, bar=True)


def test_frame_flip_key_ctrl_name(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch, '[frame]\nflip_key = "ctrl-t"\n')
    assert load_frame_config().flip_byte == 0x14


def test_frame_flip_key_hex(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch, '[frame]\nflip_key = "0x1e"\n')
    assert load_frame_config().flip_byte == 0x1E


def test_frame_flip_key_printable_rejected(tmp_path, monkeypatch):
    # a printable key would swallow real typing — fall back to default
    _write_config(tmp_path, monkeypatch, '[frame]\nflip_key = "a"\n')
    assert load_frame_config().flip_byte == 0x1D


def test_frame_bar_off(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch, '[frame]\nbar = false\n')
    assert load_frame_config().bar is False


def test_frame_malformed_values_fall_back(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch, '[frame]\nflip_key = 29\nbar = "yes"\n')
    assert load_frame_config() == FrameConfig()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_config.py -v -k frame`
Expected: FAIL — `ImportError: cannot import name 'FrameConfig'`

- [ ] **Step 3: Implement**

Append to `src/tandem/config.py`:

```python
@dataclass(frozen=True)
class FrameConfig:
    flip_byte: int = 0x1D   # Ctrl-]
    bar: bool = True


def _parse_flip_key(value: str) -> int | None:
    """'ctrl-]' / 'ctrl-t' / '0x1d' -> byte value. None when unparseable or
    not a control byte (a printable key would swallow real typing)."""
    v = value.strip().lower()
    if v.startswith("ctrl-") and len(v) == 6:
        code = ord(v[5].upper()) & 0x1F
    elif v.startswith("0x"):
        try:
            code = int(v, 16)
        except ValueError:
            return None
    else:
        return None
    return code if 0 < code < 0x20 else None


def load_frame_config() -> FrameConfig:
    """[frame] table: the flip keybind and the status bar toggle."""
    raw = _read_config().get("frame")
    if not isinstance(raw, dict):
        return FrameConfig()
    d = FrameConfig()
    key = raw.get("flip_key")
    byte = _parse_flip_key(key) if isinstance(key, str) else None
    bar = raw.get("bar")
    return FrameConfig(
        flip_byte=byte if byte is not None else d.flip_byte,
        bar=bar if isinstance(bar, bool) else d.bar,
    )
```

Also extend the module docstring's first paragraph to mention `[frame]`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_config.py -v`
Expected: all PASS (new and pre-existing).

- [ ] **Step 5: Commit**

```bash
git add src/tandem/config.py tests/test_config.py
git commit -m "feat: [frame] config table — flip_key and bar toggle"
```

---

### Task 2: FlipDetector (input-side state machine)

**Files:**
- Create: `src/tandem/frame.py`
- Test: `tests/test_frame.py` (create)

**Interfaces:**
- Produces: `FlipDetector(flip_byte: int, bar_row: int | None = None)` with `feed(data: bytes) -> tuple[bytes, int]` returning (bytes to forward to the child, count of flip presses). Task 6 consumes it in `ptyrun`.
- Consumes: nothing tandem-internal (pure module — keep it that way; `ptyrun` imports `frame`, never the reverse).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_frame.py`:

```python
"""Frame state machines: pure bytes-in/bytes-out, no PTY, no threads."""

from tandem.frame import FlipDetector

FLIP = 0x1D  # Ctrl-]


def test_flip_byte_consumed_and_counted():
    d = FlipDetector(FLIP)
    out, flips = d.feed(b"hello\x1dworld")
    assert out == b"helloworld"
    assert flips == 1


def test_plain_bytes_pass_through_untouched():
    d = FlipDetector(FLIP)
    out, flips = d.feed(b"ls -la\r")
    assert out == b"ls -la\r"
    assert flips == 0


def test_flip_byte_inside_bracketed_paste_passes_through():
    d = FlipDetector(FLIP)
    out, flips = d.feed(b"\x1b[200~abc\x1ddef\x1b[201~")
    assert out == b"\x1b[200~abc\x1ddef\x1b[201~"
    assert flips == 0


def test_flip_after_paste_end_fires():
    d = FlipDetector(FLIP)
    out, flips = d.feed(b"\x1b[200~x\x1b[201~\x1d")
    assert out == b"\x1b[200~x\x1b[201~"
    assert flips == 1


def test_paste_marker_split_across_feeds():
    d = FlipDetector(FLIP)
    out1, f1 = d.feed(b"\x1b[20")          # partial paste-begin marker
    out2, f2 = d.feed(b"0~\x1d\x1b[201~")  # completes it; 0x1D is pasted
    assert out1 + out2 == b"\x1b[200~\x1d\x1b[201~"
    assert f1 == 0 and f2 == 0


def test_paste_state_persists_across_feeds():
    d = FlipDetector(FLIP)
    d.feed(b"\x1b[200~abc")
    out, flips = d.feed(b"\x1ddef")        # still inside the paste
    assert out == b"\x1ddef"
    assert flips == 0


def test_mouse_click_on_bar_row_swallowed():
    d = FlipDetector(FLIP, bar_row=40)
    out, flips = d.feed(b"\x1b[<0;12;40M")
    assert out == b""
    assert flips == 0


def test_mouse_click_elsewhere_passes_through():
    d = FlipDetector(FLIP, bar_row=40)
    out, _ = d.feed(b"\x1b[<0;12;39M")
    assert out == b"\x1b[<0;12;39M"


def test_mouse_ignored_when_no_bar():
    d = FlipDetector(FLIP, bar_row=None)
    out, _ = d.feed(b"\x1b[<0;12;40M")
    assert out == b"\x1b[<0;12;40M"


def test_lone_esc_not_swallowed():
    # a bare ESC keypress must reach the child promptly-ish: it is carried
    # only while it could still become a tracked sequence, and flushed as
    # soon as following bytes rule that out
    d = FlipDetector(FLIP)
    out1, _ = d.feed(b"\x1b")
    out2, _ = d.feed(b"q")
    assert out1 + out2 == b"\x1bq"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_frame.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tandem.frame'`

- [ ] **Step 3: Implement**

Create `src/tandem/frame.py`:

```python
"""Frame machinery: flip-key detection, output guard, and the status bar.

The frame is tandem's one visible surface — a reserved bottom row and one
reserved keybind. Everything here is a pure bytes-in/bytes-out state
machine so `ptyrun` stays a dumb pump: nothing in this module touches the
terminal, the PTY, or threads, and `ptyrun` imports `frame`, never the
reverse.
"""

from __future__ import annotations

import re

PASTE_BEGIN = b"\x1b[200~"
PASTE_END = b"\x1b[201~"

_MOUSE_RE = re.compile(rb"\x1b\[<(\d{1,4});(\d{1,4});(\d{1,4})([Mm])")
# a trailing fragment that could still become a paste marker or mouse event
_PARTIAL_RE = re.compile(rb"\x1b(\[(<(\d{0,4}(;\d{0,4}){0,2};?)?|20[01]?)?)?\Z")


def _split_partial(buf: bytes) -> tuple[bytes, bytes]:
    """Split off a trailing fragment of a tracked escape sequence, to be
    retried on the next feed. Anything that cannot become one is not held
    back (a lone ESC keypress must reach the child)."""
    i = buf.rfind(b"\x1b", max(0, len(buf) - 11))
    if i == -1:
        return buf, b""
    m = _PARTIAL_RE.match(buf, i)
    if m and m.group(0) != b"":
        return buf[:i], buf[i:]
    return buf, b""


class FlipDetector:
    """Input-side filter: consume the flip byte (outside bracketed paste),
    swallow SGR mouse events aimed at the bar row, forward everything else
    byte-for-byte."""

    def __init__(self, flip_byte: int, bar_row: int | None = None):
        self.flip_byte = flip_byte
        self.bar_row = bar_row
        self._in_paste = False
        self._carry = b""

    def feed(self, data: bytes) -> tuple[bytes, int]:
        buf = self._carry + data
        buf, self._carry = _split_partial(buf)
        out = bytearray()
        flips = 0
        i = 0
        while i < len(buf):
            if not self._in_paste and buf.startswith(PASTE_BEGIN, i):
                self._in_paste = True
                out += PASTE_BEGIN
                i += len(PASTE_BEGIN)
                continue
            if self._in_paste and buf.startswith(PASTE_END, i):
                self._in_paste = False
                out += PASTE_END
                i += len(PASTE_END)
                continue
            if not self._in_paste:
                m = _MOUSE_RE.match(buf, i)
                if m:
                    if self.bar_row is not None and int(m.group(3)) == self.bar_row:
                        i = m.end()  # click landed on tandem's row
                        continue
                    out += m.group(0)
                    i = m.end()
                    continue
                if buf[i] == self.flip_byte:
                    flips += 1
                    i += 1
                    continue
            out.append(buf[i])
            i += 1
        return bytes(out), flips
```

Note on `test_lone_esc_not_swallowed`: `_PARTIAL_RE` matches a bare `\x1b` at end-of-buffer, so `\x1b` alone is carried; the next feed makes the buffer `\x1bq`, which no longer matches, so both bytes flush. If the regex needs adjusting to satisfy the test, adjust the regex — the test states the contract.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_frame.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/tandem/frame.py tests/test_frame.py
git commit -m "feat: FlipDetector — flip keybind on the PTY input stream, paste-safe"
```

---

### Task 3: OutputGuard (output-side watcher)

**Files:**
- Modify: `src/tandem/frame.py` (append)
- Test: `tests/test_frame.py` (append)

**Interfaces:**
- Produces: `OutputGuard()` with `feed(data: bytes) -> str` returning `""` (nothing), `"reassert"` (repaint bar + scroll region), or `"drop"` (child manages its own scroll regions — bar cannot coexist). Never alters bytes. Task 6 consumes it.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_frame.py`:

```python
from tandem.frame import OutputGuard


def test_guard_plain_output_no_verdict():
    assert OutputGuard().feed(b"hello \x1b[31mred\x1b[0m") == ""


def test_guard_clear_screen_triggers_reassert():
    assert OutputGuard().feed(b"\x1b[2J") == "reassert"


def test_guard_alt_screen_enter_and_leave_trigger_reassert():
    g = OutputGuard()
    assert g.feed(b"\x1b[?1049h") == "reassert"
    assert g.feed(b"\x1b[?1049l") == "reassert"


def test_guard_ris_triggers_reassert():
    assert OutputGuard().feed(b"\x1bc") == "reassert"


def test_guard_bare_region_reset_triggers_reassert():
    assert OutputGuard().feed(b"\x1b[r") == "reassert"


def test_guard_parameterized_region_triggers_drop():
    # the child drives its own scroll regions: the bar cannot coexist
    assert OutputGuard().feed(b"\x1b[5;40r") == "drop"


def test_guard_drop_wins_over_reassert():
    assert OutputGuard().feed(b"\x1b[2J\x1b[5;40r") == "drop"


def test_guard_sequence_split_across_feeds():
    g = OutputGuard()
    assert g.feed(b"text\x1b[?10") == ""
    assert g.feed(b"49h more") == "reassert"


def test_guard_does_not_double_count_carry():
    g = OutputGuard()
    assert g.feed(b"\x1b[2J") == "reassert"
    # the sequence sits inside the carry window now; a new feed with no
    # fresh trigger must not re-fire it
    assert g.feed(b"quiet") == ""
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_frame.py -v -k guard`
Expected: FAIL — `ImportError: cannot import name 'OutputGuard'`

- [ ] **Step 3: Implement**

Append to `src/tandem/frame.py`:

```python
_DROP_RE = re.compile(rb"\x1b\[\d{1,4}(;\d{1,4})?r")     # child's own DECSTBM
_REASSERT_RE = re.compile(rb"\x1bc|\x1b\[\?1049[hl]|\x1b\[[23]J|\x1b\[r(?!\d)")


class OutputGuard:
    """Output-side watcher for the handful of sequences that clobber the
    reserved row or scroll region. Returns a verdict; never alters bytes.
    A small carry window handles sequences split across read chunks; only
    matches ending beyond the carry count (earlier ones fired last feed)."""

    CARRY = 15

    def __init__(self):
        self._carry = b""

    def feed(self, data: bytes) -> str:
        buf = self._carry + data
        base = len(self._carry)
        self._carry = buf[max(0, len(buf) - self.CARRY):]
        for m in _DROP_RE.finditer(buf):
            if m.end() > base:
                return "drop"
        for m in _REASSERT_RE.finditer(buf):
            if m.end() > base:
                return "reassert"
        return ""
```

Note: `\x1b\[r(?!\d)` keeps the bare-reset pattern from half-matching the front of a parameterized one; `_DROP_RE` is checked first regardless.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_frame.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/tandem/frame.py tests/test_frame.py
git commit -m "feat: OutputGuard — watch child output for bar-clobbering resets"
```

---

### Task 4: StatusBar (byte composition)

**Files:**
- Modify: `src/tandem/frame.py` (append)
- Test: `tests/test_frame.py` (append)

**Interfaces:**
- Produces: `StatusBar(rows: int, cols: int, active: str, other: str)` with `resize(rows, cols)`, `line(armed: bool) -> str`, `paint(armed: bool) -> bytes`, `region() -> bytes`, `clear() -> bytes`. Pure composition — the pump writes what this returns. Task 6 consumes it.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_frame.py`:

```python
from tandem.frame import StatusBar


def test_bar_line_shows_active_and_other():
    bar = StatusBar(rows=40, cols=60, active="claude", other="codex")
    line = bar.line(armed=False)
    assert "claude ●" in line and "codex ○" in line and "^] flips" in line
    assert len(line) == 60


def test_bar_line_armed_state():
    bar = StatusBar(rows=40, cols=60, active="claude", other="codex")
    line = bar.line(armed=True)
    assert "flipping at turn end" in line and "^] cancels" in line


def test_bar_line_truncates_to_width():
    bar = StatusBar(rows=40, cols=12, active="claude", other="codex")
    assert len(bar.line(armed=False)) == 12


def test_bar_paint_targets_real_bottom_row_and_restores_cursor():
    bar = StatusBar(rows=40, cols=60, active="claude", other="codex")
    b = bar.paint(armed=False)
    assert b.startswith(b"\x1b7")        # DECSC save cursor
    assert b"\x1b[40;1H" in b            # jump to the real bottom row
    assert b"\x1b[7m" in b               # inverse video
    assert b.endswith(b"\x1b[0m\x1b8")   # reset attrs, DECRC restore


def test_bar_region_reserves_all_but_last_row():
    bar = StatusBar(rows=40, cols=60, active="claude", other="codex")
    b = bar.region()
    assert b == b"\x1b7\x1b[1;39r\x1b8"  # DECSTBM homes the cursor: save/restore


def test_bar_clear_restores_full_region_and_wipes_row():
    bar = StatusBar(rows=40, cols=60, active="claude", other="codex")
    b = bar.clear()
    assert b"\x1b[r" in b                # full-screen scroll region back
    assert b"\x1b[40;1H" in b and b"\x1b[2K" in b


def test_bar_resize_recomputes():
    bar = StatusBar(rows=40, cols=60, active="claude", other="codex")
    bar.resize(rows=30, cols=50)
    assert b"\x1b[30;1H" in bar.paint(armed=False)
    assert len(bar.line(armed=False)) == 50
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_frame.py -v -k bar`
Expected: FAIL — `ImportError: cannot import name 'StatusBar'`

- [ ] **Step 3: Implement**

Append to `src/tandem/frame.py`:

```python
class StatusBar:
    """Composes the bar's paint/region/clear byte strings for the real
    bottom terminal row. The child is told the terminal is one row shorter
    (identity row mapping for everything it can address); DECSTBM keeps
    normal-buffer scrolling above the bar."""

    def __init__(self, rows: int, cols: int, active: str, other: str):
        self.rows = rows
        self.cols = cols
        self.active = active
        self.other = other

    def resize(self, rows: int, cols: int) -> None:
        self.rows = rows
        self.cols = cols

    def line(self, armed: bool) -> str:
        if armed:
            text = f" {self.active} ⏳ flipping at turn end…  ^] cancels"
        else:
            text = f" {self.active} ● │ {self.other} ○   ^] flips"
        return text[: self.cols].ljust(self.cols)

    def paint(self, armed: bool) -> bytes:
        return (
            b"\x1b7"
            + f"\x1b[{self.rows};1H".encode()
            + b"\x1b[7m"
            + self.line(armed).encode()
            + b"\x1b[0m\x1b8"
        )

    def region(self) -> bytes:
        # DECSTBM homes the cursor, so save/restore around it
        return b"\x1b7" + f"\x1b[1;{self.rows - 1}r".encode() + b"\x1b8"

    def clear(self) -> bytes:
        return (
            b"\x1b7\x1b[r"
            + f"\x1b[{self.rows};1H".encode()
            + b"\x1b[2K\x1b8"
        )
```

(`line()` counts characters, not display cells — `●`/`│`/`⏳` are one cell each in practice; exact-width invariants are on `len(str)`, which is what the tests pin.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_frame.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/tandem/frame.py tests/test_frame.py
git commit -m "feat: StatusBar — bottom-row tab bar byte composition"
```

---

### Task 5: PtyControl and the termination ladder

**Files:**
- Modify: `src/tandem/ptyrun.py` (add imports `threading`, `time`, `dataclasses`; add classes above `run_in_pty`)
- Test: `tests/test_ptyrun.py` (append)

**Interfaces:**
- Produces: `PtyControl()` with `attach(child) -> None` (called by `run_in_pty` once the child exists) and `terminate(soft: list[bytes], soft_timeout: float = 3.0, term_timeout: float = 2.0) -> str` returning `"dead" | "soft" | "term" | "kill"` — safe to call from another thread. Tasks 6 and 9 consume it.
- Consumes: `os.killpg`, `signal`, ptyprocess child API (`.write`, `.isalive()`, `.pid`).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_ptyrun.py`:

```python
import time

from tandem.ptyrun import PtyControl


class _StubChild:
    """Stands in for a PtyProcess: records writes, dies on command."""

    def __init__(self, dies_after_writes=None, dies_on_signal=None):
        self.pid = 99999999  # killpg will fail -> ladder must survive that
        self.writes = []
        self._alive = True
        self._dies_after_writes = dies_after_writes
        self._dies_on_signal = dies_on_signal

    def write(self, data):
        self.writes.append(data)
        if self._dies_after_writes and len(self.writes) >= self._dies_after_writes:
            self._alive = False

    def isalive(self):
        return self._alive

    def kill_externally(self):
        self._alive = False


def test_terminate_soft_exit():
    c = PtyControl()
    child = _StubChild(dies_after_writes=2)
    c.attach(child)
    how = c.terminate([b"\x03", b"\x04"], soft_timeout=1.0, term_timeout=0.2)
    assert how == "soft"
    assert child.writes == [b"\x03", b"\x04"]


def test_terminate_already_dead():
    c = PtyControl()
    child = _StubChild()
    child.kill_externally()
    c.attach(child)
    assert c.terminate([b"\x04"], soft_timeout=0.2) == "dead"


def test_terminate_never_attached_returns_dead():
    c = PtyControl()
    assert c.terminate([b"\x04"], soft_timeout=0.1, attach_timeout=0.1) == "dead"


def test_terminate_escalates_past_failing_killpg(monkeypatch):
    # killpg raising (fake pid) must not break the ladder; the child dying
    # during the term wait is still detected
    c = PtyControl()
    child = _StubChild()
    c.attach(child)

    import tandem.ptyrun as ptyrun

    def fake_killpg(pgid, sig):
        child.kill_externally()

    monkeypatch.setattr(ptyrun.os, "killpg", fake_killpg)
    how = c.terminate([b"\x03"], soft_timeout=0.3, term_timeout=1.0)
    assert how == "term"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_ptyrun.py -v`
Expected: FAIL — `ImportError: cannot import name 'PtyControl'`

- [ ] **Step 3: Implement**

Add to `src/tandem/ptyrun.py` (new imports at top: `import threading`, `import time`):

```python
def _wait_dead(child, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not child.isalive():
            return True
        time.sleep(0.05)
    return not child.isalive()


class PtyControl:
    """Cross-thread termination handle. run_in_pty attaches the live child;
    terminate() runs the escalation ladder from any other thread: soft quit
    keystrokes (the CLI finalizes its own transcript), SIGTERM to the
    process group, SIGKILL. Every rung tolerates a child that is already
    gone."""

    def __init__(self):
        self._child = None
        self._attached = threading.Event()

    def attach(self, child) -> None:
        self._child = child
        self._attached.set()

    def terminate(
        self,
        soft: list[bytes],
        soft_timeout: float = 3.0,
        term_timeout: float = 2.0,
        attach_timeout: float = 5.0,
    ) -> str:
        self._attached.wait(timeout=attach_timeout)
        child = self._child
        if child is None or not child.isalive():
            return "dead"
        for chunk in soft:
            try:
                child.write(chunk)
            except Exception:
                break
            time.sleep(0.25)
        if _wait_dead(child, soft_timeout):
            return "soft"
        try:
            os.killpg(child.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        if _wait_dead(child, term_timeout):
            return "term"
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        _wait_dead(child, 1.0)
        return "kill"
```

(ptyprocess spawns the child with `setsid`, so the child's pid is its process-group id — `killpg(child.pid, …)` takes the harness's tool children down with it.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_ptyrun.py -v`
Expected: all PASS. The soft test writes both chunks with 0.25 s pauses — runtime under a second.

- [ ] **Step 5: Commit**

```bash
git add src/tandem/ptyrun.py tests/test_ptyrun.py
git commit -m "feat: PtyControl — cross-thread termination ladder for the pty child"
```

---

### Task 6: Frame wiring in run_in_pty

**Files:**
- Modify: `src/tandem/ptyrun.py` (extend `run_in_pty`, add `FrameIO`; new import `from .frame import FlipDetector, OutputGuard, StatusBar`)
- Test: `tests/test_ptyrun.py` (append)

**Interfaces:**
- Produces: `FrameIO` dataclass — fields `flip_byte: int`, `on_flip: Callable[[], None]`, `armed: Callable[[], bool]`, `bar: bool = True`, `active: str = ""`, `other: str = ""`, `bar_dropped: bool = False` (set by the pump when the bar had to go). New `run_in_pty(argv, cwd=None, env=None, frame: FrameIO | None = None, control: PtyControl | None = None) -> int`; both new params default to None = today's behavior, byte-for-byte. Also produces `_child_dims(rows: int, cols: int, bar_on: bool) -> tuple[int, int]` (unit-testable winsize lie). Task 9 constructs `FrameIO` and passes both.
- Consumes: Task 2 `FlipDetector`, Task 3 `OutputGuard`, Task 4 `StatusBar`, Task 5 `PtyControl`.

- [ ] **Step 1: Write the failing tests**

The interactive pump needs a real tty, which pytest doesn't have — the integrated path is covered by live validation (Task 11). What is unit-testable: the winsize lie, bar-activation policy, and that the non-tty fallback still ignores frame/control safely. Append to `tests/test_ptyrun.py`:

```python
import sys

from tandem.ptyrun import FrameIO, _bar_on, _child_dims, run_in_pty


def test_child_dims_reserves_bottom_row_when_bar_on():
    assert _child_dims(40, 120, bar_on=True) == (39, 120)
    assert _child_dims(40, 120, bar_on=False) == (40, 120)


def test_bar_activation_policy():
    frame = FrameIO(flip_byte=0x1D, on_flip=lambda: None, armed=lambda: False)
    assert _bar_on(frame, rows=40) is True
    assert _bar_on(frame, rows=4) is False      # too small
    assert _bar_on(None, rows=40) is False      # no frame wiring
    frame_off = FrameIO(
        flip_byte=0x1D, on_flip=lambda: None, armed=lambda: False, bar=False
    )
    assert _bar_on(frame_off, rows=40) is False  # [frame] bar = false


def test_non_tty_fallback_ignores_frame_and_control(capfd):
    from tandem.ptyrun import PtyControl

    frame = FrameIO(flip_byte=0x1D, on_flip=lambda: None, armed=lambda: False)
    code = run_in_pty(
        [sys.executable, "-c", "print('fallback-ok')"],
        frame=frame,
        control=PtyControl(),
    )
    assert code == 0
    assert "fallback-ok" in capfd.readouterr().out
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_ptyrun.py -v`
Expected: FAIL — `ImportError: cannot import name 'FrameIO'`

- [ ] **Step 3: Implement**

In `src/tandem/ptyrun.py`, add near the top (after `PtyControl`):

```python
from dataclasses import dataclass
from typing import Callable

from .frame import FlipDetector, OutputGuard, StatusBar


@dataclass
class FrameIO:
    """Frame wiring for run_in_pty, built by the runner. The pump
    constructs the detector/guard/bar internally from these fields;
    `bar_dropped` reports back that the bar had to be disabled."""

    flip_byte: int
    on_flip: Callable[[], None]
    armed: Callable[[], bool]
    bar: bool = True
    active: str = ""
    other: str = ""
    bar_dropped: bool = False


def _child_dims(rows: int, cols: int, bar_on: bool) -> tuple[int, int]:
    """The winsize lie: with the bar on, the child gets one row fewer and
    tandem owns the real bottom row."""
    return (rows - 1 if bar_on else rows, cols)


def _bar_on(frame: FrameIO | None, rows: int) -> bool:
    return frame is not None and frame.bar and rows >= 5
```

Then rewrite `run_in_pty` — same skeleton, frame-aware. Full replacement body:

```python
def run_in_pty(
    argv: list[str],
    cwd: str | None = None,
    env: dict | None = None,
    frame: FrameIO | None = None,
    control: PtyControl | None = None,
) -> int:
    """Run argv on a pty, mirroring the controlling terminal. Returns the
    child's exit status. Falls back to a plain subprocess when stdin is not
    a tty (tests, pipes). With `frame`, tandem reserves the bottom row for
    the status bar and watches for the flip keybind; with `control`, the
    child is attached for cross-thread termination."""
    try:
        stdin_fd = sys.stdin.fileno()
        is_tty = os.isatty(stdin_fd)
    except (ValueError, OSError, io.UnsupportedOperation):
        is_tty = False
    if not is_tty:
        return subprocess.run(argv, cwd=cwd, env=env).returncode

    rows, cols = _winsize(stdin_fd)
    bar_on = _bar_on(frame, rows)
    detector = FlipDetector(
        frame.flip_byte, bar_row=rows if bar_on else None
    ) if frame else None
    guard = OutputGuard() if bar_on else None
    bar = StatusBar(rows, cols, frame.active, frame.other) if bar_on else None

    child = PtyProcess.spawn(
        argv, cwd=cwd, env=env or dict(os.environ),
        dimensions=_child_dims(rows, cols, bar_on),
    )
    if control is not None:
        control.attach(child)

    out_fd = sys.stdout.fileno()

    def paint() -> None:
        if bar is not None:
            os.write(out_fd, bar.region() + bar.paint(frame.armed()))

    def drop_bar() -> None:
        nonlocal bar_on, guard, bar
        if bar is None:
            return
        os.write(out_fd, bar.clear())
        r, c = _winsize(stdin_fd)
        try:
            child.setwinsize(r, c)
        except Exception:
            pass
        if detector is not None:
            detector.bar_row = None
        bar_on, guard, bar = False, None, None
        frame.bar_dropped = True

    def on_winch(signum, frm):
        r, c = _winsize(stdin_fd)
        if bar is not None:
            bar.resize(r, c)
            detector.bar_row = r
        try:
            child.setwinsize(*_child_dims(r, c, bar_on))
        except Exception:
            pass
        paint()

    def on_term(signum, frm):
        try:
            child.kill(signal.SIGTERM)
        except Exception:
            pass

    old_winch = signal.signal(signal.SIGWINCH, on_winch)
    old_term = signal.signal(signal.SIGTERM, on_term)
    old_attrs = termios.tcgetattr(stdin_fd)
    last_armed = False
    try:
        tty.setraw(stdin_fd)
        paint()
        stdin_open = True
        while child.isalive():
            rlist = [child.fd] + ([stdin_fd] if stdin_open else [])
            try:
                ready, _, _ = select.select(rlist, [], [], 0.2)
            except InterruptedError:
                continue  # signal (e.g. SIGWINCH) — loop again
            if child.fd in ready:
                try:
                    data = child.read(65536)
                except EOFError:
                    break
                if not data:
                    break
                os.write(out_fd, data)
                if guard is not None:
                    verdict = guard.feed(data)
                    if verdict == "drop":
                        drop_bar()
                    elif verdict == "reassert":
                        paint()
            if stdin_open and stdin_fd in ready:
                data = os.read(stdin_fd, 65536)
                if data:
                    if detector is not None:
                        data, flips = detector.feed(data)
                        for _ in range(flips):
                            frame.on_flip()
                    if data:
                        child.write(data)
                else:
                    child.sendeof()
                    stdin_open = False
            if bar is not None and frame.armed() != last_armed:
                last_armed = frame.armed()
                paint()
    finally:
        if bar is not None:
            os.write(out_fd, bar.clear())
        termios.tcsetattr(stdin_fd, termios.TCSADRAIN, old_attrs)
        signal.signal(signal.SIGWINCH, old_winch)
        signal.signal(signal.SIGTERM, old_term)
    try:
        child.wait()
    except Exception:
        pass
    return child.exitstatus if child.exitstatus is not None else 1
```

Also update the module docstring: the "never reads meaning from the terminal stream" sentence gains "— except the frame's enumerated sequences (flip byte, paste markers, mouse, and the output guard's reset set)".

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_ptyrun.py tests/test_frame.py -v`
Expected: all PASS (including the two pre-existing fallback tests).

- [ ] **Step 5: Commit**

```bash
git add src/tandem/ptyrun.py tests/test_ptyrun.py
git commit -m "feat: run_in_pty frame wiring — reserved bar row, flip detection, output guard"
```

---

### Task 7: Adapter quit recipes

**Files:**
- Modify: `src/tandem/harness/base.py` (add method to `HarnessAdapter`)
- Modify: `src/tandem/harness/claude_code.py`, `src/tandem/harness/codex.py`
- Test: `tests/test_frame.py` (append — small, keeps frame concerns together)

**Interfaces:**
- Produces: `HarnessAdapter.quit_keystrokes(self) -> list[bytes]` — byte chunks sent in order (0.25 s apart, by `PtyControl.terminate`) to cleanly exit the interactive CLI: clear the composer, then quit. Task 9 consumes via `adapter.quit_keystrokes()`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_frame.py`:

```python
from tandem.harness import get_adapter


def test_claude_quit_recipe():
    # Ctrl-C clears the composer / interrupts, Ctrl-D on the empty
    # composer exits
    assert get_adapter("claude").quit_keystrokes() == [b"\x03", b"\x04"]


def test_codex_quit_recipe():
    # Ctrl-C clears/interrupts, second Ctrl-C quits ("press again"),
    # Ctrl-D backstops older builds
    assert get_adapter("codex").quit_keystrokes() == [b"\x03", b"\x03", b"\x04"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_frame.py -v -k quit`
Expected: FAIL — `AttributeError: ... has no attribute 'quit_keystrokes'`

- [ ] **Step 3: Implement**

In `src/tandem/harness/base.py`, add to `HarnessAdapter` (near `hook_argv_extra`, ~line 65):

```python
    def quit_keystrokes(self) -> list[bytes]:
        """Byte chunks that cleanly exit the interactive CLI — clear the
        composer, then quit — sent in order with a short pause between.
        Pinned per harness version; live-verified at release (the ladder's
        SIGTERM rung backstops a recipe the CLI stops honoring)."""
        return []
```

In `ClaudeCodeAdapter`:

```python
    def quit_keystrokes(self) -> list[bytes]:
        return [b"\x03", b"\x04"]
```

In `CodexAdapter`:

```python
    def quit_keystrokes(self) -> list[bytes]:
        return [b"\x03", b"\x03", b"\x04"]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_frame.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/tandem/harness/base.py src/tandem/harness/claude_code.py src/tandem/harness/codex.py tests/test_frame.py
git commit -m "feat: per-harness quit keystroke recipes for the soft-exit rung"
```

---

### Task 8: wait_until_safe + FlipMonitor

**Files:**
- Modify: `src/tandem/runner.py` (add after `ctx_to_cursor`, before `TailLoop`)
- Test: `tests/test_runner.py` (append)

**Interfaces:**
- Produces: `wait_until_safe(transcript: Path | None, sentinel: Path | None, cancelled: Callable[[], bool], quiesce: float = 2.0, poll: float = 0.2) -> bool` (False = cancelled). `FlipMonitor(control, quit_bytes: list[bytes], transcript: Path | None, sentinel: Path)` with `start()`, `stop()`, `flip_pressed()` (thread-safe arm/cancel toggle), `armed() -> bool`, and attributes `flip_requested: bool`, `how: str`. Task 9 wires it into `InteractiveRunner`.
- Consumes: Task 5 `PtyControl.terminate(soft=...)` (called with `quit_bytes`).

The turn-boundary rule (from the spec): the transcript's last append happens before the Stop hook / notify touches the sentinel, so **idle ⇔ sentinel mtime ≥ transcript mtime**. Mid-turn, wait for the marker touch or ~2 s of transcript quiescence (the fallback where the marker is unavailable — e.g. codex with a user-configured notify handler tandem refuses to clobber). A missing file reads as mtime 0, which makes a fresh session (no files) idle and a marker-less mid-turn session take the quiescence path.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_runner.py`:

```python
import os
import threading
import time

from tandem.runner import FlipMonitor, wait_until_safe


def _touch(path, mtime):
    path.write_text("x")
    os.utime(path, (mtime, mtime))


def test_wait_idle_when_sentinel_newer_than_transcript(tmp_path):
    t, s = tmp_path / "t.jsonl", tmp_path / "s.turn"
    now = time.time()
    _touch(t, now - 10)
    _touch(s, now - 5)   # marker closed the last turn
    assert wait_until_safe(t, s, cancelled=lambda: False) is True


def test_wait_idle_when_no_files(tmp_path):
    assert (
        wait_until_safe(tmp_path / "none", tmp_path / "none2",
                        cancelled=lambda: False)
        is True
    )


def test_wait_quiescence_fallback(tmp_path):
    t, s = tmp_path / "t.jsonl", tmp_path / "s.turn"
    now = time.time()
    _touch(t, now - 3)   # transcript quiet for 3s, no marker since
    _touch(s, now - 10)
    assert (
        wait_until_safe(t, s, cancelled=lambda: False, quiesce=2.0) is True
    )


def test_wait_blocks_midturn_then_marker_releases(tmp_path):
    t, s = tmp_path / "t.jsonl", tmp_path / "s.turn"
    _touch(t, time.time())          # a line just landed: turn in flight
    _touch(s, time.time() - 30)
    done = threading.Event()
    result = {}

    def waiter():
        result["ok"] = wait_until_safe(t, s, cancelled=lambda: False,
                                       quiesce=30.0, poll=0.05)
        done.set()

    threading.Thread(target=waiter, daemon=True).start()
    time.sleep(0.2)
    assert not done.is_set()        # still waiting
    _touch(s, time.time() + 1)      # marker fires
    assert done.wait(timeout=2)
    assert result["ok"] is True


def test_wait_cancelled(tmp_path):
    t, s = tmp_path / "t.jsonl", tmp_path / "s.turn"
    _touch(t, time.time())
    _touch(s, time.time() - 30)
    assert (
        wait_until_safe(t, s, cancelled=lambda: True, quiesce=30.0) is False
    )


class _StubControl:
    def __init__(self):
        self.calls = []

    def terminate(self, soft, **kw):
        self.calls.append(soft)
        return "soft"


def test_monitor_arm_wait_terminate(tmp_path):
    control = _StubControl()
    m = FlipMonitor(control, [b"\x04"], transcript=None,
                    sentinel=tmp_path / "s.turn")
    m.start()
    assert m.armed() is False
    m.flip_pressed()                 # idle (no files) -> fires immediately
    deadline = time.time() + 3
    while not m.flip_requested and time.time() < deadline:
        time.sleep(0.05)
    m.stop()
    assert m.flip_requested is True
    assert m.how == "soft"
    assert control.calls == [[b"\x04"]]


def test_monitor_toggle_cancels(tmp_path):
    t, s = tmp_path / "t.jsonl", tmp_path / "s.turn"
    _touch(t, time.time())           # mid-turn: monitor will block
    _touch(s, time.time() - 30)
    control = _StubControl()
    m = FlipMonitor(control, [b"\x04"], transcript=t, sentinel=s)
    m.start()
    m.flip_pressed()
    time.sleep(0.1)
    assert m.armed() is True
    m.flip_pressed()                 # toggle: cancel
    time.sleep(0.3)
    assert m.armed() is False
    assert m.flip_requested is False
    m.stop()
    assert control.calls == []


def test_monitor_stop_unblocks_cleanly(tmp_path):
    m = FlipMonitor(_StubControl(), [b"\x04"], transcript=None,
                    sentinel=tmp_path / "s.turn")
    m.start()
    m.stop()                         # never armed: must not hang or fire
    assert m.flip_requested is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_runner.py -v -k "wait or monitor"`
Expected: FAIL — `ImportError: cannot import name 'FlipMonitor'`

- [ ] **Step 3: Implement**

Add to `src/tandem/runner.py` (after `ctx_to_cursor`; `time`, `threading`, `Path` already imported):

```python
def _mtime(path: Path | None) -> float:
    if path is None:
        return 0.0
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def wait_until_safe(
    transcript: Path | None,
    sentinel: Path | None,
    cancelled,
    quiesce: float = 2.0,
    poll: float = 0.2,
) -> bool:
    """Block until the turn boundary. The transcript's last append lands
    before the Stop hook / notify touches the sentinel, so idle means the
    sentinel is at least as new as the transcript; otherwise wait for the
    marker touch, with transcript quiescence as the marker-less fallback.
    Returns False if `cancelled()` turned true first."""
    while True:
        if cancelled():
            return False
        t, s = _mtime(transcript), _mtime(sentinel)
        if s >= t:
            return True
        if time.time() - t >= quiesce:
            return True
        time.sleep(poll)


class FlipMonitor:
    """Owns the flip lifecycle: the armed flag (toggle to cancel), the
    turn-boundary wait, and the termination ladder through the PtyControl.
    One background thread; all public methods are thread-safe."""

    def __init__(self, control, quit_bytes: list[bytes],
                 transcript: Path | None, sentinel: Path):
        self.control = control
        self.quit_bytes = quit_bytes
        self.transcript = transcript
        self.sentinel = sentinel
        self.flip_requested = False
        self.how = ""
        self._armed = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="tandem-flip", daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._armed.set()  # unblock the wait
        self._thread.join(timeout=15)

    def flip_pressed(self) -> None:
        if self._armed.is_set():
            self._armed.clear()  # toggle: cancel a pending flip
        else:
            self._armed.set()

    def armed(self) -> bool:
        return self._armed.is_set() and not self.flip_requested

    def _run(self) -> None:
        while not self._stop.is_set():
            self._armed.wait()
            if self._stop.is_set():
                return
            ok = wait_until_safe(
                self.transcript,
                self.sentinel,
                cancelled=lambda: (
                    not self._armed.is_set() or self._stop.is_set()
                ),
            )
            if self._stop.is_set():
                return
            if not ok:
                continue  # cancelled: back to waiting for the next arm
            self.flip_requested = True
            self.how = self.control.terminate(self.quit_bytes)
            return
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_runner.py -v`
Expected: all PASS (the mid-turn test takes ~1 s by design).

- [ ] **Step 5: Commit**

```bash
git add src/tandem/runner.py tests/test_runner.py
git commit -m "feat: FlipMonitor — armed flag, turn-boundary wait, termination ladder"
```

---

### Task 9: Wire the frame into InteractiveRunner

**Files:**
- Modify: `src/tandem/runner.py` (`InteractiveRunner.run`, ~lines 176–261; new imports `load_frame_config` from `.config`, `FrameIO`, `PtyControl` from `.ptyrun`)
- Test: `tests/test_runner.py` (append)

**Interfaces:**
- Consumes: Task 1 `load_frame_config()`, Task 6 `FrameIO`/`run_in_pty(frame=, control=)`, Task 5 `PtyControl`, Task 7 `adapter.quit_keystrokes()`, Task 8 `FlipMonitor`.
- Produces: `InteractiveRunner.run() -> int` unchanged in return type, plus a new attribute set before returning: `self.flip_requested: bool`. On bar drop, a marker file `paths.tandem_home() / "tmp" / f"{tandem_id}-bar-dropped"` and a stderr-style note after exit. Task 10 consumes `flip_requested`; Task 11 (doctor) consumes the marker file.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_runner.py` (reusing the file's `_Sink` and `env_factory`; note the existing `_run_capturing_argv` monkeypatches `run_in_pty` with `lambda argv, cwd=None: ...` — those tests keep passing because the new params have defaults and `InteractiveRunner` passes them by keyword; update that lambda to `lambda argv, cwd=None, **kw: ...` only if the run below shows otherwise):

```python
def test_runner_passes_frame_and_control(env_factory, monkeypatch):
    env = env_factory(active="claude")
    seen = {}

    def fake_run_in_pty(argv, cwd=None, frame=None, control=None):
        seen.update(frame=frame, control=control)
        return 0

    monkeypatch.setattr(runner, "run_in_pty", fake_run_in_pty)
    r = runner.InteractiveRunner(env.session, lambda st, se, so: _Sink())
    code = r.run()
    assert code == 0
    assert seen["control"] is not None
    frame = seen["frame"]
    assert frame is not None
    assert frame.flip_byte == 0x1D
    assert frame.active == "claude" and frame.other == "codex"
    assert r.flip_requested is False


def test_runner_reports_flip_requested(env_factory, monkeypatch):
    env = env_factory(active="claude")

    def fake_run_in_pty(argv, cwd=None, frame=None, control=None):
        frame.on_flip()  # user pressed the keybind; idle -> fires
        deadline = time.time() + 3
        while not frame.armed() and time.time() < deadline:
            time.sleep(0.02)
        # the monitor's ladder finds no real child: control.terminate
        # returns "dead", flip_requested still set
        deadline = time.time() + 3
        while frame.armed() and time.time() < deadline:
            time.sleep(0.02)
        return 0

    monkeypatch.setattr(runner, "run_in_pty", fake_run_in_pty)
    r = runner.InteractiveRunner(env.session, lambda st, se, so: _Sink())
    r.run()
    assert r.flip_requested is True


def test_runner_writes_bar_drop_marker(env_factory, monkeypatch):
    env = env_factory(active="claude")

    def fake_run_in_pty(argv, cwd=None, frame=None, control=None):
        frame.bar_dropped = True
        return 0

    monkeypatch.setattr(runner, "run_in_pty", fake_run_in_pty)
    runner.InteractiveRunner(env.session, lambda st, se, so: _Sink()).run()
    marker = paths.tandem_home() / "tmp" / f"{env.session.tandem_id}-bar-dropped"
    assert marker.exists()
```

Add `import time` to the test file's imports if Task 8 didn't already.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_runner.py -v`
Expected: the three new tests FAIL (`run_in_pty` called without `frame=`/`control=` kwargs → `seen` empty / no `flip_requested` attribute). Pre-existing tests must still pass; if any fail on the changed call signature, fix them per the note in Step 1.

- [ ] **Step 3: Implement**

In `InteractiveRunner.run()`:

Imports at module top: `from .config import load_frame_config, load_harness_args` (extend the existing import), `from .ptyrun import FrameIO, PtyControl, run_in_pty` (extend).

After the `argv` lines (`argv += adapter.hook_argv_extra(sentinel)`) and before `stop = threading.Event()`:

```python
        frame_cfg = load_frame_config()
        control = PtyControl()
        monitor = FlipMonitor(
            control, adapter.quit_keystrokes(), transcript, sentinel
        )
        frame = FrameIO(
            flip_byte=frame_cfg.flip_byte,
            on_flip=monitor.flip_pressed,
            armed=monitor.armed,
            bar=frame_cfg.bar,
            active=active,
            other=session.shadow,
        )
        self.flip_requested = False
```

Change the `run_in_pty` call and the `finally`:

```python
        thread = threading.Thread(target=tail_thread, name="tandem-tail", daemon=True)
        thread.start()
        monitor.start()
        try:
            code = run_in_pty(argv, cwd=session.cwd, frame=frame, control=control)
        finally:
            stop.set()
            monitor.stop()
            thread.join(timeout=10)
            sentinel.unlink(missing_ok=True)
            self.flip_requested = monitor.flip_requested
        if frame.bar_dropped:
            marker = paths.tandem_home() / "tmp" / f"{session.tandem_id}-bar-dropped"
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.touch()
            errors.append(
                "status bar disabled for this session (terminal conflict);"
                " set [frame] bar = false to silence"
            )
        for err in errors:
            print(f"tandem: sync error: {err}")
        return code
```

(The existing `for err in errors:` block moves after the bar-drop check; the message prefix stays as-is — one shared reporting path.)

Also set `self.flip_requested = False` in `__init__` so the attribute exists even if `run()` raises early.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_runner.py -v`
Expected: all PASS, including the pre-existing argv tests.

- [ ] **Step 5: Commit**

```bash
git add src/tandem/runner.py tests/test_runner.py
git commit -m "feat: InteractiveRunner wires the frame — flip monitor, control, bar-drop marker"
```

---

### Task 10: Auto-flip in shell.run_shell

**Files:**
- Modify: `src/tandem/shell.py` (`run_shell`, `_switch`, `_enter`; add `_flip_loop`, `_norm`, `_clear_screen`; add `import sys`)
- Test: `tests/test_shell.py` (append)

**Interfaces:**
- Consumes: `run_harness(session)` now returns `int` **or** `(code: int, flip: bool)`. The real closure (built inside `run_shell` when `run_harness is None`) returns `(runner.run(), runner.flip_requested)`. Test seams passing plain ints keep working via `_norm`.
- Produces: `_enter(...) -> tuple[int, bool]` and `_switch(...) -> tuple[int, bool]` (both previously returned `int`); `_flip_loop(tandem_id, run_harness, first: tuple[int, bool]) -> int` — flips repeatedly, no prompt stop, screen cleared between; `run_shell` signature unchanged.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_shell.py` (mirror the file's existing fake-`run_harness`/`input_fn` idiom — read its first test and reuse the same session/store setup helper it uses):

```python
from tandem import shell


def test_flip_reenters_other_harness_without_prompt(shell_env):
    # shell_env: use the same fixture/helper the file's existing tests use
    # to get (tandem_id, store) with a paired session; adapt the name.
    calls = []

    def run_harness(session):
        calls.append(session.active)
        if len(calls) == 1:
            return (0, True)     # user pressed Ctrl-]
        return (0, False)        # then exited normally

    prompts = []

    def input_fn(prompt):
        prompts.append(prompt)
        raise EOFError           # leave the shell at the first prompt

    code = shell.run_shell(
        shell_env.tandem_id, sink_factory=None,
        input_fn=input_fn, run_harness=run_harness,
    )
    assert code == 0
    assert calls == ["claude", "codex"]      # flip switched roles, no prompt between
    assert len(prompts) == 1                 # prompt only after the plain exit


def test_flip_failure_falls_back_to_prompt(shell_env, monkeypatch):
    # ops.switch_session raising must not lose the session: back to prompt
    def run_harness(session):
        return (0, True)

    def boom(store, session):
        raise RuntimeError("no flip for you")

    monkeypatch.setattr(shell.ops, "switch_session", boom)

    def input_fn(prompt):
        raise EOFError

    code = shell.run_shell(
        shell_env.tandem_id, sink_factory=None,
        input_fn=input_fn, run_harness=run_harness,
    )
    assert code == 0             # carried through; session intact at prompt


def test_int_returning_run_harness_still_works(shell_env):
    # legacy seam: plain int means no flip
    def run_harness(session):
        return 7

    def input_fn(prompt):
        raise EOFError

    code = shell.run_shell(
        shell_env.tandem_id, sink_factory=None,
        input_fn=input_fn, run_harness=run_harness,
    )
    assert code == 7
```

`shell_env` above is a stand-in name: `tests/test_shell.py` already constructs paired sessions for `run_shell` tests — use its existing fixture or setup helper verbatim. Do not invent a new fixture if one exists.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_shell.py -v -k flip`
Expected: FAIL — with a tuple return, current `run_shell` treats `(0, True)` as the exit code (type error or wrong assertion), and no re-enter happens.

- [ ] **Step 3: Implement**

In `src/tandem/shell.py` (add `import sys` at top):

```python
def _norm(res) -> tuple[int, bool]:
    """run_harness returns int (legacy and test seams) or (code, flip)."""
    return res if isinstance(res, tuple) else (res, False)


def _clear_screen() -> None:
    if sys.stdout.isatty():  # pragma: no cover - interactive only
        sys.stdout.write("\x1b[2J\x1b[H")
        sys.stdout.flush()


def _flip_loop(tandem_id: str, run_harness, first: tuple[int, bool]) -> int:
    """Keep flipping (Ctrl-]) until a session ends without requesting one.
    No prompt stop between flips — this is the frame's tab feel."""
    code, flip = first
    while flip:
        _clear_screen()
        code, flip = _switch(tandem_id, run_harness, code)
    return code
```

Change `_enter` to return `(code, flip)`:

```python
def _enter(tandem_id: str, run_harness, code: int) -> tuple[int, bool]:
    """Run the active harness; returns (exit code, flip requested). A
    failed launch (or a vanished session row) is reported and `code` is
    carried forward with no flip, so the caller returns to the prompt
    instead of losing the session."""
    try:
        with StateStore() as store:
            session = store.get_session(tandem_id)
            if session is None:
                raise LookupError(f"session {tandem_id} is not in the state store")
            store.touch_used(tandem_id)
        return _norm(run_harness(session))
    except Exception as exc:
        click.secho(
            f"could not run the harness: {type(exc).__name__}: {exc}",
            fg="red",
            err=True,
        )
        return code, False
```

Change `_switch` the same way — its failure paths become `return code, False`, and its success tail becomes `return _enter(tandem_id, run_harness, code)` (unchanged text, now propagating the tuple).

In `run_shell`, update the three call sites:

- initial entry: `code = _flip_loop(tandem_id, run_harness, _enter(tandem_id, run_harness, code))`
- the `"" | "resume"` branch: same `_flip_loop(...)` form
- the `["switch"]` branch: `code = _flip_loop(tandem_id, run_harness, _switch(tandem_id, run_harness, code))`

Update the default `run_harness` closure at the top of `run_shell`:

```python
        def run_harness(session):
            from .runner import InteractiveRunner

            r = InteractiveRunner(session, sink_factory=sink_factory)
            return r.run(), r.flip_requested
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_shell.py -v`
Expected: all PASS — the new flip tests and every pre-existing `run_shell` test (they pass ints through the seam; `_norm` keeps their meaning).

- [ ] **Step 5: Commit**

```bash
git add src/tandem/shell.py tests/test_shell.py
git commit -m "feat: shell auto-flip — Ctrl-] re-enters the other harness without a prompt stop"
```

---

### Task 11: Doctor surfaces bar drops

**Files:**
- Modify: `src/tandem/doctor.py` (inside `run_doctor`, with the other per-session checks)
- Test: `tests/test_memory_doctor.py` (append — doctor tests live here)

**Interfaces:**
- Consumes: the marker file Task 9 writes: `paths.tandem_home() / "tmp" / f"{session.tandem_id}-bar-dropped"`.
- Produces: a `report.warn(...)` when the marker exists.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_memory_doctor.py` (reuse the file's existing doctor-report fixture/setup idiom for a paired session):

```python
def test_doctor_warns_on_bar_drop_marker(doctor_env):
    # doctor_env: whatever existing fixture yields (store, session) for
    # run_doctor tests in this file — reuse it verbatim.
    from tandem import paths
    from tandem.doctor import run_doctor

    store, session = doctor_env
    marker = paths.tandem_home() / "tmp" / f"{session.tandem_id}-bar-dropped"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.touch()
    report = run_doctor(store, session)
    assert any(
        c.status == "warn" and "status bar" in c.msg for c in report.checks
    )


def test_doctor_quiet_without_bar_drop_marker(doctor_env):
    from tandem.doctor import run_doctor

    store, session = doctor_env
    report = run_doctor(store, session)
    assert not any("status bar" in c.msg for c in report.checks)
```

(Check the actual `Check` field name for the message — `msg` vs `message` — in `doctor.py` ~line 130 and match it.)

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_memory_doctor.py -v -k bar_drop`
Expected: FAIL — no such warning is produced.

- [ ] **Step 3: Implement**

In `run_doctor` (with the other session-scoped checks), add:

```python
    marker = paths.tandem_home() / "tmp" / f"{session.tandem_id}-bar-dropped"
    if marker.exists():
        report.warn(
            "the status bar was auto-disabled in a previous session"
            " (terminal conflict) — set [frame] bar = false to keep it off,"
            " or delete the marker to retry: " + str(marker)
        )
```

Import `paths` if `doctor.py` doesn't already.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_memory_doctor.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/tandem/doctor.py tests/test_memory_doctor.py
git commit -m "feat: doctor warns when the status bar was auto-disabled"
```

---

### Task 12: Docs — the inverted story

**Files:**
- Modify: `README.md` (Why tandem / quick-start area)
- Modify: `docs/how-it-works.md` (new "The frame" bullet in the mechanics list)
- Modify: `docs/configuration.md` (new `[frame]` section)

**Interfaces:** none — prose only. Match each file's existing voice (short bullets, bold leads, no marketing fluff beyond the README's established tone).

- [ ] **Step 1: README**

Add a frame-led bullet to the "Why tandem?" section (place it first) and update the quick-start to lead with the frame experience:

```markdown
### 🖥️ One CLI, two harnesses, zero ceremony.

`tandem` is the terminal you live in. It fronts the real Claude Code or
Codex TUI — pixel-for-pixel native — and **Ctrl-]** flips to the other
one in a couple of seconds, same conversation, same files, same history.
A one-line tab bar on the bottom row shows which model you're facing;
everything above it is the untouched native UI.
```

And in the how-you-use-it flow, replace the exit-then-`switch` framing with: work → `Ctrl-]` → keep working, noting that the tandem prompt still exists on plain exit and `switch` still works there.

- [ ] **Step 2: docs/how-it-works.md**

Add one bullet to the top mechanics list, after "A persistent prompt, not the OS shell":

```markdown
- **The frame: flip without leaving.** Ctrl-] (configurable, consumed at
  the PTY layer, ignored inside bracketed paste) flips the screen to the
  other harness: pressed mid-turn it arms and fires at the turn-complete
  marker (press again to cancel — the bar shows the armed state), then
  tandem exits the fronted CLI gracefully (quit keystrokes, then SIGTERM,
  then a bounded SIGKILL), lets the incremental sync settle, flips roles,
  and resumes the other side. The bottom terminal row is tandem's one
  drawn pixel: the child is told the terminal is a row shorter, a scroll
  region keeps output above the bar, and a targeted watcher reasserts it
  after child screen resets. If a terminal can't sustain the bar it drops
  for the session (the flip keeps working) and `tandem doctor` says so.
```

- [ ] **Step 3: docs/configuration.md**

Add:

```markdown
## [frame]

| key | default | meaning |
| --- | --- | --- |
| `flip_key` | `"ctrl-]"` | The flip keybind, consumed by tandem (never forwarded). Accepts `ctrl-<char>` or a hex byte like `"0x1d"`; printable keys are rejected (they would swallow typing). |
| `bar` | `true` | The one-line tab bar on the bottom terminal row. `false` hides it; the flip still works. |
```

- [ ] **Step 4: Proofread render**

Run: `uv run python -c "print(open('README.md').read()[:2000])"` — or just re-read the three diffs. Check: no stale exit-then-switch instructions contradicting the frame story anywhere in the three files (`grep -n "switch" README.md docs/how-it-works.md docs/configuration.md`).

- [ ] **Step 5: Commit**

```bash
git add README.md docs/how-it-works.md docs/configuration.md
git commit -m "docs: the frame — tandem as the CLI you launch, Ctrl-] flips models"
```

---

### Task 13: Live validation (operator-run)

**Files:** none (manual checklist; fixes discovered here become new commits on this branch)

This is the release gate. Run in a scratch repo with a real paired session (`tandem` in a fresh directory), on at least two of: Terminal.app, iTerm2, VS Code terminal.

- [ ] Flip claude → codex while idle: lands in codex within ~3 s, history present, bar shows `codex ●`.
- [ ] Flip codex → claude while idle: same, reversed.
- [ ] Press Ctrl-] mid-turn (while the model is generating): bar shows `⏳ flipping at turn end…`; flip fires only after the turn completes; the completed turn is present on the other side.
- [ ] Press Ctrl-] mid-turn, then Ctrl-] again: armed indicator clears, no flip happens.
- [ ] Paste a text blob containing a literal Ctrl-] byte (e.g. `printf 'a\x1db' | pbcopy`, then paste in the composer): no flip, the paste arrives intact.
- [ ] Quit-recipe verification (spec: pinned per harness version): confirm the soft rung actually exits each CLI — flip and check the runner reports/behaves as a graceful exit (no SIGTERM needed). If a recipe fails, fix `quit_keystrokes()` for that adapter and note the CLI version in the commit message.
- [ ] Bar under stress: resize the window (bar repaints, no child misrender), scroll a long output (bar stays pinned), open something alt-screen from inside the harness if available (bar survives or drops gracefully).
- [ ] `[frame] bar = false`: no bar, flip still works.
- [ ] `[frame] flip_key = "ctrl-t"`: Ctrl-T flips, Ctrl-] types through.
- [ ] Resume-failure fallback: temporarily break the shadow CLI (`PATH` without codex), flip — tandem relaunches the harness you left and reports the problem; session intact.
- [ ] Plain exit (Ctrl-D at empty composer): still lands at the `tandem (active)>` prompt; `switch`, `status`, `exit` all behave as before.
- [ ] Non-frame surfaces unaffected: `tandem run --on codex "..."` one-shot works; a plain `claude` launched outside tandem is untouched by any of this.

Record results (terminal app + CLI versions tested) in the PR description.

---

## Self-Review (completed)

- **Spec coverage:** frame behavior (Tasks 6, 8–10), detector edges incl. paste + config override (Tasks 1, 2), bar + reserved row + guard + auto-drop (Tasks 3, 4, 6), termination ladder (Tasks 5, 7), turn-boundary + cancel toggle (Task 8), never-lose-the-session (Task 10 failure test), doctor note (Task 11), README/docs reframe (Task 12), live validation incl. emulator matrix and quit-recipe pinning (Task 13). Unpaired/plain sessions need no code: the detector only exists under the wrapper.
- **Placeholder scan:** the two named-fixture stand-ins (`shell_env`, `doctor_env`) are explicit instructions to reuse the file's existing setup idiom, not TBDs; all code blocks are complete.
- **Type consistency:** `FrameIO` fields match between Tasks 6 and 9; `terminate(soft=list[bytes])` matches `quit_keystrokes() -> list[bytes]`; `_enter`/`_switch` tuple returns consistent across Task 10's call sites; marker-file path identical in Tasks 9 and 11.

---

## Task 13 addendum — items added by the final whole-branch review

Run these in addition to the checklist above:

- [ ] **Does the bar survive first contact? (MERGE BLOCKER if not)** Launch, wait 60s, use both TUIs normally. If the bar vanishes immediately, capture output (`script`/asciinema) and check for a startup full-height DECSTBM — that decides whether the "parameterized DECSTBM ⇒ drop" rule survives.
- [ ] **DECSC/DECRC collision:** watch for cursor misplacement during heavy streaming and drag-resize (one save slot per screen buffer; tandem and the child share it).
- [ ] **Mid-turn flip during a >60s silent tool call** (arm at t+5s of a long test run) — must wait for turn completion, not fire at the 120s valve unless the hook actually died.
- [ ] **Ctrl-] mid-turn in a fresh `tandem --active codex` session** — must show the armed indicator and wait (rollout-discovery publishing path).
- [ ] **Sync-error visibility across a flip** — force a truncated transcript, flip, confirm the `sync error` line is readable after the screen clear.
- [ ] **Quit-recipe verification:** confirm a normal flip reports a graceful exit (`soft`, not SIGTERM) in both directions, per pinned CLI versions.
- [ ] **Shrink below 5 rows, then restore:** bar drops silently (no doctor marker — that's the shrink path), child gets full winsize back.
- [ ] **Terminal hangup mid-session** (close window / drop SSH): no orphaned harness child.
- [ ] **ESC pressed while the model is streaming** reaches the child (~200ms keyboard-idle flush) — test while streaming, not while idle.
- [ ] Marker-less fresh codex (user-owned `notify`): flip armed in the first second of the session may fire early until the rollout is discovered — confirm the window is imperceptible in practice.
- [ ] Note: the pump loop below the tty check has no automated coverage by design (pytest has no tty) — live validation is this code's only integration test.

## Post-merge follow-ups (from review triage; none block merge)

flip_key denylist for Tab/Enter/Esc collisions; pin the `bar_row` writable contract and the config→FrameIO seam with tests; carry-truncation test filler byte; `\x1b[0m` prefix before the bar's SGR; `time.monotonic()` in `_wait_dead`; EIO guard on the pump's stdin read; post-wait armed re-check (cancel TOCTOU); bound the flip-loop test fakes; extract `paths.bar_drop_marker()`; tail-join timeout vs `switch_session` drain lock (pre-existing); `docs/formats.md` cross-check next release.
