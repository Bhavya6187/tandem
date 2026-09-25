# Chat Navigator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An opt-in second harness reviews each substantive `tandem chat` turn on a private fork of its shadow, prints a receipt or a note in the window, and rides the next prompt when configured.

**Architecture:** The dispatcher's worker already runs each turn end to end and syncs before it emits `Idle`; a new `Navigator` object hooks in there, gates on facts collected from the turn's own live events, and runs one headless review on a background thread through a per-harness `Reviewer` (codex: `ops.fork_shadow` + a fresh `CodexRuntime`; claude: `--fork-session` on a fresh `ClaudeRuntime`). Two new live events carry the review state to the window, which paints a spinner, a bar mark, and rows; the dispatcher appends a pending note to the next prompt as a trailer. Everything is logged to one JSONL file per session.

**Tech Stack:** Python 3.11+, stdlib only (threading, subprocess, json), click for the CLI, pytest with the existing fake CLIs under `tests/fakes/`.

**Spec:** `docs/specs/2026-09-25-chat-navigator-design.md`

## Global Constraints

- No new dependencies: `click`, `pydantic`, `watchdog`, `pexpect` only.
- The feature is off unless `[chat] navigator` names a participant; `""` is the default and stays the default.
- Every bar glyph is one terminal cell wide (no W/F East-Asian-width glyphs on the bar row). `⚑` is only used in the scroll region.
- Every scroll-region newline goes through `Screen.line` / `Screen.print` (CRLF; the tty is raw).
- Config keys are forgiving: a wrong type or value falls back to the default, never raises.
- A navigator note is clipped to 400 characters by tandem.
- Prompts starting with `[tandem` are never reviewed.
- The review fork is never a sync source and is deleted in a `finally`.
- The navigator must never take the window down: every entry point swallows and logs.
- Work happens in the worktree `/Users/bhavya/git/tandem-navigator` on branch `chat-navigator`. Run tests with `uv run pytest -q` from that directory.

## Review Focus

- A cwd that is not a git repository: `compute_diff` returns `""` and the review still runs (Task 4).
- A verdict wrapped in ``` fences or preceded by prose: parsed as JSON, not an error (Task 4).
- A verdict arriving while an approval row is up (`activity.waiting`): deferred, never painted over the answer row (Task 10).
- `/quit` during a review: the reviewer's process is killed and the fork deleted (Tasks 7, 8, 9).
- A pending note when the user types a bare `/codex` (route only, no turn): not consumed; it rides the next real turn (Task 9).

---

### Task 1: Config keys

**Files:**
- Modify: `src/tandem/config.py:204-249` (`ChatConfig`, `load_chat_config`)
- Modify: `docs/configuration.md:209-217` (the `[chat]` TOML block)
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `ChatConfig.navigator: str` (`""` | `"claude"` | `"codex"`), `ChatConfig.navigator_model: str`, `ChatConfig.navigator_deliver: str` (`"bar"` | `"prompt"`), `ChatConfig.navigator_headroom: int`, `ChatConfig.navigator_interval: int`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_config.py`:

```python
def test_navigator_is_off_by_default(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch, '[chat]\nbell = true\n')
    cfg = load_chat_config()
    assert cfg.navigator == "" and cfg.navigator_model == ""
    assert cfg.navigator_deliver == "bar"
    assert cfg.navigator_headroom == 20 and cfg.navigator_interval == 180


def test_navigator_keys_read_and_validate(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch,
                  '[chat]\nnavigator = "codex"\nnavigator_model = "gpt-5.5"\n'
                  'navigator_deliver = "prompt"\nnavigator_headroom = 35\nnavigator_interval = 60\n')
    cfg = load_chat_config()
    assert cfg.navigator == "codex" and cfg.navigator_model == "gpt-5.5"
    assert cfg.navigator_deliver == "prompt"
    assert cfg.navigator_headroom == 35 and cfg.navigator_interval == 60


@pytest.mark.parametrize("value", ['"opencode"', '"gemini"', "true", "3"])
def test_navigator_rejects_unknown_harnesses(tmp_path, monkeypatch, value):
    _write_config(tmp_path, monkeypatch, f'[chat]\nnavigator = {value}\n')
    assert load_chat_config().navigator == ""


def test_navigator_bad_values_fall_back(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch,
                  '[chat]\nnavigator_deliver = "push"\nnavigator_headroom = -5\n'
                  'navigator_interval = "soon"\n')
    cfg = load_chat_config()
    assert cfg.navigator_deliver == "bar"
    assert cfg.navigator_headroom == 0            # clamped, like history_turns
    assert cfg.navigator_interval == 180
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_config.py -q -k navigator`
Expected: FAIL with `AttributeError: 'ChatConfig' object has no attribute 'navigator'`

- [ ] **Step 3: Add the fields and the loader lines**

In `src/tandem/config.py`, add after `_CODEX_SANDBOXES`:

```python
_NAVIGATORS = ("", "claude", "codex")
_NAVIGATOR_DELIVERY = ("bar", "prompt")
```

Add to `ChatConfig` after `skip_permissions`:

```python
    navigator: str = ""                 # "claude" | "codex"; "" = off (the default, kept off)
    navigator_model: str = ""           # model pin for the review turn; "" = the harness default
    navigator_deliver: str = "bar"      # "bar": you see it; "prompt": it also rides your next prompt
    navigator_headroom: int = 20        # skip reviews under this % left in the navigator's 5h window
    navigator_interval: int = 180       # seconds between spoken notes
```

In `load_chat_config`, add to the `ChatConfig(...)` return:

```python
        navigator=pick("navigator", str, d.navigator, _NAVIGATORS),
        navigator_model=pick("navigator_model", str, d.navigator_model),
        navigator_deliver=pick("navigator_deliver", str, d.navigator_deliver, _NAVIGATOR_DELIVERY),
        navigator_headroom=max(0, pick("navigator_headroom", int, d.navigator_headroom)),
        navigator_interval=max(0, pick("navigator_interval", int, d.navigator_interval)),
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_config.py -q`
Expected: PASS

- [ ] **Step 5: Document the keys**

In `docs/configuration.md`, extend the `[chat]` TOML block (after the `codex_sandbox` line):

```toml
# navigator = "codex"          # default "": off. A second harness reviews each substantive turn
# navigator_model = ""         # model pin for the review turn; "" = that harness's default
# navigator_deliver = "bar"    # "bar": the note is shown to you and rides only prompts you route to
#                              # the navigator; "prompt": it also rides your next prompt to any harness
# navigator_headroom = 20      # no reviews when the navigator's 5h window has under this % left
# navigator_interval = 180     # seconds between spoken notes
```

And add this paragraph right after the block:

```markdown
`navigator` names a participant (`claude` or `codex`) that reviews each turn
the other harness runs, on a private fork of its own shadow transcript: one
headless review after every turn that edited files, failed a command, or
claimed completion. A clean review prints a one-line receipt; a concern
prints as a note with file and line evidence. **Enabling it sends every
reviewed turn's conversation and diff to the navigator's vendor after every
turn, on that account's quota.** It is off unless you set it. `/note` shows
the pending note, `/note dismiss` drops it, `/note good` and `/note bad`
record whether it helped (see `tandem navigator log`).
```

- [ ] **Step 6: Commit**

```bash
git add src/tandem/config.py docs/configuration.md tests/test_config.py
git commit -m "Add the [chat] navigator keys, off by default"
```

---

### Task 2: Event vocabulary and file-change paths

**Files:**
- Modify: `src/tandem/chat/events.py`
- Modify: `src/tandem/chat/runtime/claude.py:126-134` (assistant `tool_use` → `ToolStarted`), `:148-156` (`result`)
- Modify: `src/tandem/chat/runtime/codex.py:311-313` (`fileChange` item)
- Modify: `src/tandem/chat/runtime/opencode.py:306-308` (tool part)
- Test: `tests/test_chat_claude.py`, `tests/test_chat_codex.py`, `tests/test_chat_opencode.py`

**Interfaces:**
- Produces: `ToolStarted(call_id, tool, summary, paths: tuple[str, ...] = ())`; `TurnStarted(harness, model, prompt, carried: str = "")`; `TurnOutcome(status, error="", native_id=None, structured=None)`; `LimitsUpdate(harness, text, windows: tuple[tuple[str, int], ...] = ())`; `Evidence(file, line, why="")`; `Verdict(verdict, severity="", note="", evidence=(), elapsed=0.0, error="", navigator="", model="")` with `.spoken`; `ReviewStarted(harness)`; `ReviewFinished(harness, verdict)`; both in `LiveEvent`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_chat_claude.py`:

```python
def test_file_change_tools_carry_their_paths():
    rt = ClaudeRuntime(ChatConfig())
    rec = Recorder()
    for name, inp in [("Edit", {"file_path": "/p/a.py", "old_string": "x"}),
                      ("Write", {"file_path": "/p/b.py", "content": ""}),
                      ("NotebookEdit", {"notebook_path": "/p/c.ipynb"}),
                      ("Bash", {"command": "ls"}),
                      ("Read", {"file_path": "/p/d.py"}),
                      ("Edit", {})]:
        rt.handle_line({"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": f"t-{name}", "name": name, "input": inp}]}}, rec.emit, rec, lambda o: None)
    paths = [e.paths for e in rec.events if isinstance(e, ToolStarted)]
    assert paths == [("/p/a.py",), ("/p/b.py",), ("/p/c.ipynb",), (), (), ()]


def test_result_carries_structured_output():
    rt = ClaudeRuntime(ChatConfig())
    rec = Recorder()
    out = rt.handle_line({"type": "result", "subtype": "success", "is_error": False, "num_turns": 1,
                          "result": "{}", "structured_output": {"verdict": "clean"}},
                         rec.emit, rec, lambda o: None)
    assert out.status == "completed" and out.structured == {"verdict": "clean"}
```

Append to `tests/test_chat_codex.py` (the `Recorder` and `CodexRuntime` are already imported there):

```python
def test_file_change_items_carry_their_paths():
    rt = CodexRuntime(ChatConfig())
    rec = Recorder()
    rt.handle({"jsonrpc": "2.0", "method": "item/started", "params": {
        "threadId": "t", "turnId": "u", "startedAtMs": 1,
        "item": {"type": "fileChange", "id": "fc-1", "status": "inProgress", "changes": [
            {"path": "/p/a.py", "kind": {"type": "update"}, "diff": "-x\n+y\n"},
            {"path": "/p/b.py", "kind": {"type": "add"}, "diff": "+z\n"}]}}},
        lambda o: None, rec.emit, rec)
    started = [e for e in rec.events if isinstance(e, ToolStarted)]
    assert started and started[0].tool == "patch" and started[0].paths == ("/p/a.py", "/p/b.py")
```

`kind` is the discriminated shape from `codex_protocol.PatchChangeKind1/2/3` (`{"type": "add"}`, `{"type": "delete"}`, `{"type": "update", ...}`); if `update` needs extra fields, read `PatchChangeKind3` in `src/tandem/chat/runtime/codex_protocol.py` and add them.

Append to `tests/test_chat_opencode.py` (find its `Recorder`/runtime fixture at the top of the file and use the same names):

```python
def test_edit_and_write_parts_carry_their_paths():
    from tandem.chat.runtime.opencode import OpencodeRuntime, TurnState
    rt = OpencodeRuntime(ChatConfig())
    rec = Recorder()
    st = TurnState(session_id="s-1")
    for tool, inp in [("edit", {"filePath": "/p/a.py"}), ("write", {"filePath": "/p/b.py"}),
                      ("bash", {"command": "ls"})]:
        rt.handle_event({"type": "message.part.updated", "properties": {"part": {
            "sessionID": "s-1", "messageID": "m-1", "id": f"p-{tool}", "type": "tool",
            "callID": f"c-{tool}", "tool": tool, "state": {"status": "running", "input": inp}}}},
            st, rec.emit, rec)
    paths = [e.paths for e in rec.events if isinstance(e, ToolStarted)]
    assert paths == [("/p/a.py",), ("/p/b.py",), ()]
```

`Recorder` and `ChatConfig` are already defined/imported at the top of `tests/test_chat_opencode.py`; if the file's `Recorder` has a different name, use that one.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_chat_claude.py tests/test_chat_codex.py tests/test_chat_opencode.py -q -k "paths or structured"`
Expected: FAIL with `TypeError: ... unexpected keyword argument 'paths'` / `AttributeError: 'TurnOutcome' object has no attribute 'structured'`

- [ ] **Step 3: Extend the events**

In `src/tandem/chat/events.py`:

```python
@dataclass(frozen=True)
class ToolStarted:
    call_id: str
    tool: str
    summary: str        # one line, the renderer prints it after the tool name
    paths: tuple[str, ...] = ()   # files a file-change tool names; empty for commands and reads
```

```python
@dataclass(frozen=True)
class TurnStarted:
    harness: str
    model: str
    prompt: str
    carried: str = ""   # a navigator note's summary when one rode this prompt; "" otherwise
```

```python
@dataclass(frozen=True)
class LimitsUpdate:
    harness: str
    text: str           # bar-ready, e.g. "5h 4% 7d 41%"
    windows: tuple[tuple[str, int], ...] = ()   # (label, used_percent), shortest first
```

Add after `LimitsUpdate`:

```python
@dataclass(frozen=True)
class Evidence:
    file: str
    line: int
    why: str = ""


@dataclass(frozen=True)
class Verdict:
    """A finished review. `verdict` is one of: clean, speak, empty (spoke
    with no note), dup (repeats evidence already spoken), error, off (the
    navigator disabled itself)."""
    verdict: str
    severity: str = ""              # "block" | "warn" | ""
    note: str = ""
    evidence: tuple[Evidence, ...] = ()
    elapsed: float = 0.0
    error: str = ""
    navigator: str = ""
    model: str = ""

    @property
    def spoken(self) -> bool:
        return self.verdict == "speak"


@dataclass(frozen=True)
class ReviewStarted:
    harness: str        # the navigator


@dataclass(frozen=True)
class ReviewFinished:
    harness: str
    verdict: Verdict
```

Extend the union:

```python
LiveEvent = Union[TextDelta, ThinkingDelta, ToolStarted, ToolOutput, ToolFinished,
                  ApprovalRequest, QuestionRequest, TurnStarted, TurnFinished,
                  Failure, LimitsUpdate, ReviewStarted, ReviewFinished, Idle]
```

And `TurnOutcome`:

```python
@dataclass
class TurnOutcome:
    status: str
    error: str = ""
    native_id: str | None = None   # a thread id minted during this turn (fresh codex)
    structured: object | None = None   # claude's structured_output when a schema was requested
```

- [ ] **Step 4: Fill `paths` in the three clients**

`src/tandem/chat/runtime/claude.py` — add near `_COMMAND_TOOLS`:

```python
_FILE_TOOLS = {"Edit": "file_path", "Write": "file_path", "MultiEdit": "file_path",
               "NotebookEdit": "notebook_path"}


def _tool_paths(name: str, inp) -> tuple[str, ...]:
    key = _FILE_TOOLS.get(name)
    if key and isinstance(inp, dict) and isinstance(inp.get(key), str) and inp[key]:
        return (inp[key],)
    return ()
```

In `handle_line`, the `tool_use` branch becomes:

```python
                if b.get("type") == "tool_use":
                    name = b.get("name", "")
                    emit(ToolStarted(b.get("id", ""), f"agent/{name}" if child else name,
                                     summarize_args(b.get("name", ""), b.get("input")),
                                     paths=() if child else _tool_paths(name, b.get("input"))))
```

The `result` branch returns:

```python
            return TurnOutcome(status, error=str(m.get("result", "")) if status == "failed" else "",
                               structured=m.get("structured_output"))
```

`src/tandem/chat/runtime/codex.py` — the `fileChange` branch of `item/started`:

```python
            elif kind == "fileChange":
                names = tuple(c.path for c in (getattr(it, "changes", None) or []) if getattr(c, "path", ""))
                emit(ToolStarted(it.id, "patch", first_line(", ".join(names)), paths=names))
```

`src/tandem/chat/runtime/opencode.py` — the tool part branch:

```python
                if status in ("running", "completed", "error") and call_id not in st.started:
                    st.started.add(call_id)
                    inp = state.get("input")
                    fp = inp.get("filePath") if tool in ("edit", "write") and isinstance(inp, dict) else None
                    emit(ToolStarted(call_id, tool, summarize_args(tool, inp),
                                     paths=(fp,) if isinstance(fp, str) and fp else ()))
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_chat_claude.py tests/test_chat_codex.py tests/test_chat_opencode.py tests/test_chat_render.py -q`
Expected: PASS (the render tests construct `ToolStarted` positionally; the new field has a default)

- [ ] **Step 6: Commit**

```bash
git add src/tandem/chat/events.py src/tandem/chat/runtime/claude.py src/tandem/chat/runtime/codex.py src/tandem/chat/runtime/opencode.py tests/test_chat_claude.py tests/test_chat_codex.py tests/test_chat_opencode.py
git commit -m "Carry file-change paths, structured output and review events in the chat vocabulary"
```

---

### Task 3: Turn facts and the gate

**Files:**
- Create: `src/tandem/chat/navigator.py`
- Test: `tests/test_chat_navigator.py`

**Interfaces:**
- Consumes: `ToolStarted.paths`, `ToolFinished.ok`, `TextDelta` from Task 2.
- Produces: `TurnFacts` dataclass; `FactsCollector(harness, prompt, carried_note, first_turn, emit, clock=time.monotonic)` with `.emit(ev)` and `.finish(status) -> TurnFacts`; `gate(facts, *, navigator, headroom_ok, interval_ok, disabled) -> str` returning `""` (review) or `"skip:<reason>"`; `COMMAND_TOOLS`, `CLAIM_RE`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_chat_navigator.py`:

```python
"""The navigator: facts from a turn's own events, the gate, the worker, and
the log. Reviewers are faked; see test_chat_reviewers.py for the real ones."""

import json
import threading
import time

import pytest

from tandem.chat.events import (Evidence, TextDelta, ToolFinished, ToolStarted, TurnFinished,
                                Verdict)
from tandem.chat.navigator import CLAIM_RE, FactsCollector, TurnFacts, gate


class Clock:
    def __init__(self): self.now = 100.0
    def __call__(self): return self.now


def collect(events, *, harness="claude", prompt="do it", carried=False, first=False, status="completed"):
    sink = []
    clock = Clock()
    c = FactsCollector(harness, prompt, carried, first, sink.append, clock)
    for ev in events:
        c.emit(ev)
    clock.now += 4.5
    facts = c.finish(status)
    assert len(sink) == len(events)            # every event is forwarded untouched
    return facts


def test_facts_union_paths_and_count_commands_and_failures():
    facts = collect([
        ToolStarted("c1", "Edit", "a.py", paths=("a.py",)),
        ToolFinished("c1", True),
        ToolStarted("c2", "Bash", "pytest"),
        ToolFinished("c2", False, "exit 1"),
        ToolStarted("c3", "Read", "b.py"),
        ToolStarted("c4", "Write", "a.py", paths=("a.py",)),
        TextDelta("all "), TextDelta("done"),
        TurnFinished("completed", ""),
    ])
    assert facts.paths == ("a.py",)
    assert facts.commands == 1 and facts.failed_tools == 1
    assert facts.final_text == "all done"
    assert facts.status == "completed" and facts.ended - facts.started == 4.5


def test_final_text_keeps_only_the_last_2000_chars():
    facts = collect([TextDelta("x" * 1500), TextDelta("y" * 1500)])
    assert len(facts.final_text) == 2000 and facts.final_text.endswith("y" * 1500)


def test_command_tool_names_across_harnesses():
    for tool in ("Bash", "exec", "bash"):
        assert collect([ToolStarted("c", tool, "ls")]).commands == 1
    assert collect([ToolStarted("c", "Grep", "x")]).commands == 0


@pytest.mark.parametrize("text, hit", [
    ("All tests are passing now.", True), ("Fixed the bug.", True), ("I implemented it", True),
    ("Here is how it works", False), ("The function is undone", False),
])
def test_claim_pattern(text, hit):
    assert bool(CLAIM_RE.search(text)) is hit


def facts_with(**kw) -> TurnFacts:
    base = dict(harness="claude", prompt="fix it", carried_note=False, first_turn=False,
                status="completed", paths=(), commands=0, failed_tools=0, final_text="",
                started=0.0, ended=1.0)
    base.update(kw)
    return TurnFacts(**base)


def g(facts, **kw):
    opts = dict(navigator="codex", headroom_ok=True, interval_ok=True, disabled=False)
    opts.update(kw)
    return gate(facts, **opts)


def test_gate_reviews_edits_failures_and_claims():
    assert g(facts_with(paths=("a.py",))) == ""
    assert g(facts_with(failed_tools=1)) == ""
    assert g(facts_with(final_text="done, all tests pass")) == ""
    assert g(facts_with()) == "skip:quiet"


def test_gate_skip_reasons_in_order():
    assert g(facts_with(paths=("a",)), disabled=True) == "skip:disabled"
    assert g(facts_with(harness="codex", paths=("a",))) == "skip:own-turn"
    assert g(facts_with(prompt="[tandem] the turn ended", paths=("a",))) == "skip:tandem-prompt"
    assert g(facts_with(first_turn=True, paths=("a",))) == "skip:first-turn"
    assert g(facts_with(status="interrupted", paths=("a",))) == "skip:interrupted"
    assert g(facts_with(status="failed", paths=("a",))) == "skip:failed"
    assert g(facts_with(paths=("a",)), headroom_ok=False) == "skip:headroom"
    assert g(facts_with(paths=("a",)), interval_ok=False) == "skip:interval"


def test_a_claim_does_not_count_when_a_note_rode_the_prompt():
    assert g(facts_with(final_text="fixed", carried_note=True)) == "skip:quiet"
    assert g(facts_with(paths=("a",), carried_note=True)) == ""     # a real change still does
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_chat_navigator.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'tandem.chat.navigator'`

- [ ] **Step 3: Create the module with facts and gate**

Create `src/tandem/chat/navigator.py`:

```python
"""The navigator: a second harness that reviews each substantive chat turn
on a private fork of its own shadow and says nothing unless it would block
a PR. This module holds the parts that need no process: the facts one turn
leaves behind, the gate that decides whether they deserve a review, the
prompt and schema, the verdict parser, the log, and the worker that strings
them together around a Reviewer (chat/reviewers.py)."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Callable

from .events import LiveEvent, TextDelta, ToolFinished, ToolStarted

# the command tool as each client names it (tandem's own labels for codex)
COMMAND_TOOLS = frozenset({"Bash", "exec", "bash", "shell"})
# a completion claim in the final text — a heuristic, stated as one in the spec
CLAIM_RE = re.compile(r"\b(done|fixed|passing|passes|implemented|completed?|works now)\b", re.I)
_FINAL_TEXT_CHARS = 2000
_TANDEM_PREFIX = "[tandem"


@dataclass
class TurnFacts:
    harness: str
    prompt: str                    # the user's text, trailer excluded
    carried_note: bool             # a navigator note rode this prompt
    first_turn: bool               # the session's first turn (shadows seeded just now)
    status: str                    # TurnOutcome.status
    paths: tuple[str, ...]         # union of ToolStarted.paths
    commands: int                  # command tools started
    failed_tools: int              # ToolFinished(ok=False)
    final_text: str                # last 2000 chars of the turn's TextDelta
    started: float
    ended: float


class FactsCollector:
    """Wraps the dispatcher's emit for one turn: every event is forwarded
    untouched and the few the gate needs are counted on the way past."""

    def __init__(self, harness: str, prompt: str, carried_note: bool, first_turn: bool,
                 emit: Callable[[LiveEvent], None], clock: Callable[[], float] = time.monotonic):
        self._forward, self._clock = emit, clock
        self._facts = TurnFacts(harness, prompt, carried_note, first_turn, "", (), 0, 0, "",
                                clock(), clock())
        self._paths: list[str] = []
        self._text = ""

    def emit(self, ev: LiveEvent) -> None:
        if isinstance(ev, ToolStarted):
            if ev.paths:
                self._paths += [p for p in ev.paths if p not in self._paths]
            elif ev.tool in COMMAND_TOOLS:
                self._facts.commands += 1
        elif isinstance(ev, ToolFinished):
            if not ev.ok:
                self._facts.failed_tools += 1
        elif isinstance(ev, TextDelta):
            self._text = (self._text + ev.text)[-_FINAL_TEXT_CHARS:]
        self._forward(ev)

    def finish(self, status: str) -> TurnFacts:
        f = self._facts
        f.status, f.paths, f.final_text, f.ended = status, tuple(self._paths), self._text, self._clock()
        return f


def gate(facts: TurnFacts, *, navigator: str, headroom_ok: bool, interval_ok: bool,
         disabled: bool) -> str:
    """'' when the turn deserves a review, else 'skip:<reason>' — the reason
    is what the log records, so every branch names one."""
    if disabled:
        return "skip:disabled"
    if facts.harness == navigator:
        return "skip:own-turn"
    if facts.prompt.lstrip().startswith(_TANDEM_PREFIX):
        return "skip:tandem-prompt"
    if facts.first_turn:
        return "skip:first-turn"
    if facts.status != "completed":
        return f"skip:{facts.status}"
    if not headroom_ok:
        return "skip:headroom"
    if not interval_ok:
        return "skip:interval"
    if facts.paths or facts.failed_tools:
        return ""
    if not facts.carried_note and CLAIM_RE.search(facts.final_text):
        return ""
    return "skip:quiet"
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_chat_navigator.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/tandem/chat/navigator.py tests/test_chat_navigator.py
git commit -m "Collect turn facts from the live events and gate them for review"
```

---

### Task 4: Prompt, schema, verdict parser, diff

**Files:**
- Modify: `src/tandem/chat/navigator.py`
- Test: `tests/test_chat_navigator.py`

**Interfaces:**
- Produces: `SCHEMA: dict`; `build_prompt(facts: TurnFacts, diff: str) -> str`; `parse_verdict(structured, text, *, navigator, model, elapsed) -> Verdict`; `compute_diff(cwd, paths, commands, *, cap=20_000, run=subprocess.run) -> str`; `NOTE_CHARS = 400`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_chat_navigator.py`:

```python
import subprocess
from tandem.chat.navigator import NOTE_CHARS, SCHEMA, build_prompt, compute_diff, parse_verdict


def test_prompt_names_the_turn_and_carries_the_diff():
    p = build_prompt(facts_with(harness="claude", paths=("a.py", "b.py")), "--- a.py\n+++ a.py\n")
    assert p.startswith("[tandem navigator]")
    assert "ran on claude" in p and "a.py, b.py" in p and "+++ a.py" in p
    assert "block a pull request" in p and "\"clean\"" in p


def test_prompt_without_files_or_diff_says_so():
    p = build_prompt(facts_with(paths=()), "")
    assert "no files" in p and "(no diff)" in p


def test_schema_is_what_the_spec_says():
    assert SCHEMA["required"] == ["verdict"]
    assert SCHEMA["properties"]["verdict"]["enum"] == ["clean", "speak"]
    assert SCHEMA["properties"]["note"]["maxLength"] == NOTE_CHARS == 400


def pv(structured=None, text="", **kw):
    opts = dict(navigator="codex", model="", elapsed=1.5)
    opts.update(kw)
    return parse_verdict(structured, text, **opts)


def test_structured_output_wins_over_text():
    v = pv({"verdict": "speak", "severity": "block", "note": "bad loop",
            "evidence": [{"file": "s.py", "line": 12, "why": "swallows"}]}, text="garbage")
    assert v.spoken and v.severity == "block" and v.note == "bad loop"
    assert v.evidence == (Evidence("s.py", 12, "swallows"),)
    assert v.navigator == "codex" and v.elapsed == 1.5


def test_text_json_is_parsed_even_inside_fences_or_prose():
    v = pv(text="Sure.\n```json\n{\"verdict\": \"speak\", \"note\": \"n\", \"severity\": \"warn\"}\n```\n")
    assert v.spoken and v.severity == "warn"
    assert pv(text='{"verdict": "clean"}').verdict == "clean"


def test_speak_without_a_note_is_empty_and_long_notes_are_clipped():
    assert pv({"verdict": "speak", "note": "  "}).verdict == "empty"
    v = pv({"verdict": "speak", "note": "x" * 900})
    assert len(v.note) == NOTE_CHARS


def test_bad_verdicts_are_errors_not_exceptions():
    for structured, text in [(None, ""), (None, "not json"), ({"verdict": "maybe"}, ""),
                             ({"verdict": "speak", "note": "n", "evidence": "nope"}, ""),
                             ({"verdict": "speak", "note": "n", "evidence": [{"file": "a"}]}, "")]:
        v = pv(structured, text)
        assert v.verdict == "error" and v.error, (structured, text)


def test_evidence_with_a_string_line_is_coerced_or_dropped():
    v = pv({"verdict": "speak", "note": "n", "evidence": [{"file": "a.py", "line": "7"}]})
    assert v.evidence == (Evidence("a.py", 7),)


@pytest.fixture
def repo(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-q", "--allow-empty", "-m", "root"], check=True)
    (tmp_path / "a.py").write_text("x = 1\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "a.py"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-q", "-m", "a"], check=True)
    return tmp_path


def test_diff_of_touched_files_plus_untracked_contents(repo):
    (repo / "a.py").write_text("x = 2\n")
    (repo / "new.py").write_text("print('hi')\n")
    d = compute_diff(str(repo), ("a.py", "new.py"), 0)
    assert "-x = 1" in d and "+x = 2" in d
    assert "new.py (untracked)" in d and "print('hi')" in d


def test_diff_falls_back_to_the_whole_tree_after_a_command(repo):
    (repo / "a.py").write_text("x = 3\n")
    assert "+x = 3" in compute_diff(str(repo), (), 1)
    assert compute_diff(str(repo), (), 0) == ""          # nothing touched, nothing ran


def test_diff_is_capped_with_a_marker(repo):
    (repo / "a.py").write_text("y\n" * 5000)
    d = compute_diff(str(repo), ("a.py",), 0, cap=500)
    assert len(d) <= 500 + 40 and d.endswith("… (truncated)")


def test_diff_outside_a_repo_is_empty_not_an_error(tmp_path):
    (tmp_path / "a.py").write_text("x\n")
    assert compute_diff(str(tmp_path), ("a.py",), 1) == ""
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_chat_navigator.py -q -k "prompt or schema or verdict or diff or evidence"`
Expected: FAIL with `ImportError: cannot import name 'SCHEMA'`

- [ ] **Step 3: Implement prompt, schema, parser, diff**

Append to `src/tandem/chat/navigator.py` (add `import json`, `import os`, `import subprocess`, `from pathlib import Path`, and `from .events import Evidence, Verdict` to the imports):

```python
NOTE_CHARS = 400
_DIFF_CAP = 20_000

SCHEMA: dict = {
    "type": "object",
    "required": ["verdict"],
    "properties": {
        "verdict": {"enum": ["clean", "speak"]},
        "severity": {"enum": ["block", "warn"]},
        "note": {"type": "string", "maxLength": NOTE_CHARS},
        "evidence": {"type": "array", "items": {
            "type": "object", "required": ["file", "line"],
            "properties": {"file": {"type": "string"}, "line": {"type": "integer"},
                           "why": {"type": "string"}}}},
    },
}

_PROMPT = """[tandem navigator] You are reviewing the assistant turn immediately above this message, which ran on {harness}. It touched: {paths}.
Its diff (may include earlier uncommitted changes in this tree):
{diff}
Speak only if you would block a pull request over something in that turn: a bug it introduced, a claim it made that its own output contradicts, a failing command it ignored. Do not restate the turn. Do not raise style.
Reply in the required schema. If nothing rises to that bar, verdict is "clean" and note is empty."""


def build_prompt(facts: TurnFacts, diff: str) -> str:
    return _PROMPT.format(harness=facts.harness,
                          paths=", ".join(facts.paths) if facts.paths else "no files",
                          diff=diff or "(no diff)")


_JSON_RE = re.compile(r"\{.*\}", re.S)


def _json_in(text: str):
    m = _JSON_RE.search(text or "")
    if m is None:
        raise ValueError("no JSON object in the reply")
    return json.loads(m.group(0))


def parse_verdict(structured, text: str, *, navigator: str, model: str, elapsed: float) -> Verdict:
    """The model's reply as a Verdict. Anything that does not fit the schema
    is an `error` verdict with the reason — never an exception."""
    base = dict(navigator=navigator, model=model, elapsed=elapsed)
    try:
        obj = structured if isinstance(structured, dict) else _json_in(text)
        if not isinstance(obj, dict):
            raise ValueError("reply is not an object")
        verdict = obj.get("verdict")
        if verdict not in ("clean", "speak"):
            raise ValueError(f"verdict {verdict!r} is not clean|speak")
        if verdict == "clean":
            return Verdict("clean", **base)
        note = str(obj.get("note") or "").strip()[:NOTE_CHARS]
        if not note:
            return Verdict("empty", **base)
        severity = obj.get("severity") if obj.get("severity") in ("block", "warn") else ""
        raw = obj.get("evidence") or []
        if not isinstance(raw, list):
            raise ValueError("evidence is not a list")
        evidence = []
        for e in raw:
            if not isinstance(e, dict) or "file" not in e or "line" not in e:
                raise ValueError("evidence item needs file and line")
            evidence.append(Evidence(str(e["file"]), int(e["line"]), str(e.get("why") or "")))
        return Verdict("speak", severity=severity, note=note, evidence=tuple(evidence), **base)
    except (ValueError, TypeError) as exc:
        return Verdict("error", error=f"unparsable verdict: {exc}", **base)


def _git(cwd: str, args: list[str], run) -> str | None:
    """stdout of a git command, or None when git is absent, the cwd is not
    a repository, or the command fails — every one of those means 'no diff'."""
    try:
        r = run(["git", "--no-pager", *args], cwd=cwd, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


def compute_diff(cwd: str, paths: tuple[str, ...], commands: int, *,
                 cap: int = _DIFF_CAP, run=subprocess.run) -> str:
    """What the reviewed turn left in the working tree: git's diff for the
    touched files plus the contents of any touched path git does not track;
    the whole tree's diff when no file was named but a command ran (it may
    have written anything). Capped; empty outside a repository."""
    if _git(cwd, ["rev-parse", "--is-inside-work-tree"], run) is None:
        return ""
    parts: list[str] = []
    if paths:
        d = _git(cwd, ["diff", "--no-color", "--", *paths], run)
        if d:
            parts.append(d)
        tracked = _git(cwd, ["ls-files", "--", *paths], run) or ""
        tracked_set = set(tracked.split("\n"))
        for p in paths:
            rel = os.path.relpath(p, cwd) if os.path.isabs(p) else p
            if rel not in tracked_set:
                try:
                    body = Path(cwd, rel).read_text(errors="replace")
                except OSError:
                    continue
                parts.append(f"--- {rel} (untracked)\n{body}")
    elif commands:
        status = _git(cwd, ["status", "--porcelain"], run)
        d = _git(cwd, ["diff", "--no-color"], run)
        if status:
            parts.append(status)
        if d:
            parts.append(d)
    out = "\n".join(parts)
    return out if len(out) <= cap else out[:cap] + "\n… (truncated)"
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_chat_navigator.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/tandem/chat/navigator.py tests/test_chat_navigator.py
git commit -m "Add the navigator prompt, schema, verdict parser and diff"
```

---

### Task 5: The log

**Files:**
- Modify: `src/tandem/chat/navigator.py`
- Test: `tests/test_chat_navigator.py`

**Interfaces:**
- Produces: `log_path(tandem_id) -> Path`; `NavigatorLog(path)` with `.review(facts, gate_reason, verdict) -> str` (the record's `ts`, used as `ref`), `.ridden(ref, to)`, `.feedback(ref, value)`; `NavigatorLog.read(path) -> list[dict]`; `NavigatorLog.stats(records) -> dict` with keys `reviewed`, `spoken`, `skipped`, `good`, `bad`, `helpful` (float or None).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_chat_navigator.py`:

```python
from tandem.chat.navigator import NavigatorLog, log_path


def test_log_path_is_per_session_under_tandem_home(tmp_path, monkeypatch):
    monkeypatch.setenv("TANDEM_HOME", str(tmp_path / ".tandem"))
    assert log_path("tdm-1") == tmp_path / ".tandem" / "navigator" / "tdm-1.jsonl"


def test_log_records_reviews_rides_and_feedback(tmp_path):
    log = NavigatorLog(tmp_path / "n" / "x.jsonl")
    spoken = Verdict("speak", severity="block", note="bad", evidence=(Evidence("a.py", 3, "w"),),
                     elapsed=2.0, navigator="codex", model="m")
    r1 = log.review(facts_with(prompt="p" * 200, paths=("a.py",)), "", spoken)
    r2 = log.review(facts_with(), "skip:quiet", None)
    r3 = log.review(facts_with(paths=("a",)), "", Verdict("clean", navigator="codex"))
    log.ridden(r1, "claude")
    log.feedback(r1, "good")
    recs = NavigatorLog.read(tmp_path / "n" / "x.jsonl")
    assert [r["kind"] for r in recs] == ["review", "review", "review", "ridden", "feedback"]
    assert recs[0]["ts"] == r1 and recs[0]["gate"] == "review" and recs[0]["verdict"] == "speak"
    assert recs[0]["evidence"] == [{"file": "a.py", "line": 3, "why": "w"}]
    assert len(recs[0]["prompt"]) == 120
    assert recs[1]["gate"] == "skip:quiet" and recs[1]["verdict"] == ""
    assert recs[3] == {"ts": recs[3]["ts"], "kind": "ridden", "ref": r1, "to": "claude"}
    assert recs[4]["ref"] == r1 and recs[4]["value"] == "good"
    assert r1 != r2 != r3


def test_log_stats():
    recs = [
        {"kind": "review", "ts": "1", "gate": "review", "verdict": "speak"},
        {"kind": "review", "ts": "2", "gate": "review", "verdict": "clean"},
        {"kind": "review", "ts": "3", "gate": "skip:quiet", "verdict": ""},
        {"kind": "review", "ts": "4", "gate": "review", "verdict": "speak"},
        {"kind": "review", "ts": "5", "gate": "review", "verdict": "speak"},
        {"kind": "feedback", "ref": "1", "value": "good"},
        {"kind": "feedback", "ref": "4", "value": "bad"},
    ]
    assert NavigatorLog.stats(recs) == {"reviewed": 4, "spoken": 3, "skipped": 1,
                                         "good": 1, "bad": 1, "helpful": 0.5}
    assert NavigatorLog.stats([])["helpful"] is None


def test_log_read_skips_a_torn_line(tmp_path):
    p = tmp_path / "x.jsonl"
    p.write_text('{"kind": "review", "ts": "1"}\n{"kind": "rev')
    assert NavigatorLog.read(p) == [{"kind": "review", "ts": "1"}]
    assert NavigatorLog.read(tmp_path / "missing.jsonl") == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_chat_navigator.py -q -k log`
Expected: FAIL with `ImportError: cannot import name 'NavigatorLog'`

- [ ] **Step 3: Implement the log**

Append to `src/tandem/chat/navigator.py` (add `import threading` and `from datetime import datetime, timezone`, `from .. import paths`):

```python
def log_path(tandem_id: str) -> Path:
    return paths.tandem_home() / "navigator" / f"{tandem_id}.jsonl"


class NavigatorLog:
    """One JSON line per gated turn, plus `ridden` and `feedback` lines that
    point back at a review by its `ts`. Append-only; a torn last line is
    skipped on read."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._last_ts = ""

    def _ts(self) -> str:
        ts = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        if ts <= self._last_ts:                     # same microsecond: keep refs unique
            ts = self._last_ts + "0"
        self._last_ts = ts
        return ts

    def _append(self, record: dict) -> str:
        with self._lock:
            record = {"ts": self._ts(), **record}
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with open(self.path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            except OSError:
                pass                                # the log is a courtesy, never a blocker
            return record["ts"]

    def review(self, facts: TurnFacts, gate_reason: str, verdict: Verdict | None) -> str:
        v = verdict or Verdict("")
        return self._append({
            "kind": "review", "turn_harness": facts.harness,
            "prompt": " ".join(facts.prompt.split())[:120],
            "gate": gate_reason or "review", "verdict": v.verdict, "severity": v.severity,
            "note": v.note,
            "evidence": [{"file": e.file, "line": e.line, "why": e.why} for e in v.evidence],
            "elapsed": round(v.elapsed, 2), "navigator": v.navigator, "model": v.model,
            "error": v.error,
        })

    def ridden(self, ref: str, to: str) -> None:
        self._append({"kind": "ridden", "ref": ref, "to": to})

    def feedback(self, ref: str, value: str) -> None:
        self._append({"kind": "feedback", "ref": ref, "value": value})

    @staticmethod
    def read(path: Path) -> list[dict]:
        out: list[dict] = []
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return out
        for line in lines:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict):
                out.append(rec)
        return out

    @staticmethod
    def stats(records: list[dict]) -> dict:
        reviews = [r for r in records if r.get("kind") == "review"]
        reviewed = [r for r in reviews if r.get("gate") == "review"]
        spoken = [r for r in reviewed if r.get("verdict") == "speak"]
        spoken_ts = {r.get("ts") for r in spoken}
        marks = [r.get("value") for r in records
                 if r.get("kind") == "feedback" and r.get("ref") in spoken_ts]
        good, bad = marks.count("good"), marks.count("bad")
        return {"reviewed": len(reviewed), "spoken": len(spoken),
                "skipped": len(reviews) - len(reviewed), "good": good, "bad": bad,
                "helpful": good / (good + bad) if good + bad else None}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_chat_navigator.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/tandem/chat/navigator.py tests/test_chat_navigator.py
git commit -m "Log every gated turn, ride and feedback mark for the navigator"
```

---

### Task 6: The worker

**Files:**
- Modify: `src/tandem/chat/navigator.py`
- Test: `tests/test_chat_navigator.py`

**Interfaces:**
- Consumes: Tasks 3, 4, 5.
- Produces: `ReviewResult(structured, text)`; `Reviewer` protocol (`harness: str`; `review(session, model, prompt, schema, shadow_lock) -> ReviewResult`; `close()`); `Note(ref, navigator, turn_harness, verdict)` with `.summary` and `.trailer()`; `Navigator(harness, cfg, reviewer, post, log, *, headroom=lambda: True, clock=time.monotonic, diff=compute_diff)` with `.shadow_lock`, `.turn_ended(facts, session)`, `.take(harness) -> Note | None`, `.pending() -> Note | None`, `.dismiss(feedback=None) -> bool`, `.mark() -> str`, `.close()`, `.join(timeout)`. `ReviewError` exception class.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_chat_navigator.py`:

```python
from types import SimpleNamespace
from tandem.chat.events import ReviewFinished, ReviewStarted
from tandem.chat.navigator import Navigator, Note, ReviewError, ReviewResult
from tandem.config import ChatConfig

SESSION = SimpleNamespace(cwd="/tmp/nowhere", tandem_id="tdm-nav", participants=["claude", "codex"])


class FakeReviewer:
    """Scripted results, released one at a time so tests can observe the
    in-flight state. `results` items are ReviewResult, or an Exception."""
    harness = "codex"

    def __init__(self, results):
        self.results = list(results)
        self.calls = []
        self.gate = threading.Event()
        self.gate.set()
        self.closed = 0

    def review(self, session, model, prompt, schema, shadow_lock):
        self.calls.append((model, prompt))
        self.gate.wait(5)
        r = self.results.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    def close(self):
        self.closed += 1


def speak(note="bad loop", file="s.py", line=12):
    return ReviewResult({"verdict": "speak", "severity": "block", "note": note,
                         "evidence": [{"file": file, "line": line, "why": "w"}]}, "")


CLEAN = ReviewResult({"verdict": "clean"}, "")


def make_nav(results, tmp_path, cfg=None, **kw):
    posted = []
    reviewer = FakeReviewer(results)
    log = NavigatorLog(tmp_path / "log.jsonl")
    opts = dict(headroom=lambda: True, clock=Clock(), diff=lambda cwd, paths, commands, **k: "DIFF")
    opts.update(kw)
    nav = Navigator("codex", cfg or ChatConfig(navigator="codex"), reviewer, posted.append, log, **opts)
    return nav, reviewer, posted, log


def finished(posted):
    return [e for e in posted if isinstance(e, ReviewFinished)]


def test_a_gated_turn_runs_one_review_and_posts_both_events(tmp_path):
    nav, reviewer, posted, log = make_nav([CLEAN], tmp_path)
    nav.turn_ended(facts_with(paths=("a.py",)), SESSION)
    nav.join(5)
    assert [type(e).__name__ for e in posted] == ["ReviewStarted", "ReviewFinished"]
    assert posted[0].harness == "codex" and posted[1].verdict.verdict == "clean"
    assert reviewer.calls[0][1].startswith("[tandem navigator]") and "DIFF" in reviewer.calls[0][1]
    recs = NavigatorLog.read(log.path)
    assert recs[-1]["gate"] == "review" and recs[-1]["verdict"] == "clean"
    assert nav.mark() == "" and nav.pending() is None


def test_a_skipped_turn_is_logged_and_runs_nothing(tmp_path):
    nav, reviewer, posted, log = make_nav([CLEAN], tmp_path)
    nav.turn_ended(facts_with(), SESSION)
    nav.join(1)
    assert posted == [] and reviewer.calls == []
    assert NavigatorLog.read(log.path)[-1]["gate"] == "skip:quiet"


def test_a_spoken_note_is_pending_marks_the_bar_and_rides_only_the_navigator_in_bar_mode(tmp_path):
    nav, reviewer, posted, log = make_nav([speak()], tmp_path)
    nav.turn_ended(facts_with(paths=("a.py",)), SESSION)
    nav.join(5)
    note = nav.pending()
    assert isinstance(note, Note) and note.verdict.note == "bad loop" and nav.mark() == "note"
    assert note.summary == "bad loop"
    assert nav.take("claude") is None and nav.pending() is note        # bar mode: not for claude
    got = nav.take("codex")
    assert got is note and nav.pending() is None and nav.mark() == ""
    assert "[tandem navigator] codex reviewed the previous claude turn and flagged (block): bad loop" in got.trailer()
    assert got.trailer().startswith("\n\n") and "s.py:12 — w" in got.trailer()
    recs = NavigatorLog.read(log.path)
    assert recs[-1]["kind"] == "ridden" and recs[-1]["to"] == "codex" and recs[-1]["ref"] == note.ref


def test_prompt_mode_rides_any_harness(tmp_path):
    nav, *_ = make_nav([speak()], tmp_path, cfg=ChatConfig(navigator="codex", navigator_deliver="prompt"))
    nav.turn_ended(facts_with(paths=("a.py",)), SESSION)
    nav.join(5)
    assert nav.take("claude") is not None


def test_dismiss_with_feedback_logs_it(tmp_path):
    nav, reviewer, posted, log = make_nav([speak()], tmp_path)
    nav.turn_ended(facts_with(paths=("a.py",)), SESSION)
    nav.join(5)
    ref = nav.pending().ref
    assert nav.dismiss("bad") is True and nav.pending() is None
    assert nav.dismiss() is False
    rec = NavigatorLog.read(log.path)[-1]
    assert rec == {"ts": rec["ts"], "kind": "feedback", "ref": ref, "value": "bad"}


def test_a_newer_turn_replaces_the_pending_one_while_a_review_runs(tmp_path):
    nav, reviewer, posted, log = make_nav([CLEAN, CLEAN], tmp_path)
    reviewer.gate.clear()
    nav.turn_ended(facts_with(prompt="first", paths=("a.py",)), SESSION)
    nav.turn_ended(facts_with(prompt="second", paths=("b.py",)), SESSION)
    nav.turn_ended(facts_with(prompt="third", paths=("c.py",)), SESSION)
    assert nav.mark() == "reviewing"
    reviewer.gate.set()
    nav.join(5)
    assert len(finished(posted)) == 2
    assert [f"{'a' if 'a.py' in p else 'c'}" for _, p in reviewer.calls] == ["a", "c"]
    # the replaced turn is logged the moment it is replaced, before the
    # first review finishes, so compare by prompt rather than by order
    assert {r["prompt"]: r["gate"] for r in NavigatorLog.read(log.path) if r["kind"] == "review"} == {
        "first": "review", "second": "skip:replaced", "third": "review"}


def test_interval_and_dedupe(tmp_path):
    clock = Clock()
    # the second turn is skipped at the gate, so it pops no result
    nav, reviewer, posted, log = make_nav([speak(), speak(file="t.py"), speak(file="t.py")],
                                          tmp_path, cfg=ChatConfig(navigator="codex", navigator_interval=100),
                                          clock=clock)
    nav.turn_ended(facts_with(paths=("a.py",)), SESSION); nav.join(5)
    nav.turn_ended(facts_with(paths=("a.py",)), SESSION); nav.join(5)       # inside the interval
    assert NavigatorLog.read(log.path)[-1]["gate"] == "skip:interval"
    clock.now += 101
    nav.dismiss()
    nav.turn_ended(facts_with(paths=("a.py",)), SESSION); nav.join(5)       # new evidence: spoken
    assert finished(posted)[-1].verdict.spoken
    clock.now += 101
    nav.dismiss()
    nav.turn_ended(facts_with(paths=("a.py",)), SESSION); nav.join(5)       # same evidence: dup
    assert finished(posted)[-1].verdict.verdict == "dup" and nav.pending() is None


def test_three_failures_disable_the_navigator_for_the_window(tmp_path):
    boom = [ReviewError("no fork"), ReviewResult(None, "not json"), RuntimeError("bug")]
    nav, reviewer, posted, log = make_nav(boom + [CLEAN], tmp_path)
    for _ in range(4):
        nav.turn_ended(facts_with(paths=("a.py",)), SESSION); nav.join(5)
    verdicts = [e.verdict.verdict for e in finished(posted)]
    assert verdicts == ["error", "error", "off"]
    assert "bug" in finished(posted)[-1].verdict.error
    assert NavigatorLog.read(log.path)[-1]["gate"] == "skip:disabled"
    assert reviewer.results == [CLEAN]                                     # never ran


def test_a_success_resets_the_failure_count(tmp_path):
    nav, reviewer, posted, log = make_nav([ReviewError("x"), ReviewError("y"), CLEAN, ReviewError("z"), CLEAN],
                                          tmp_path)
    for _ in range(5):
        nav.turn_ended(facts_with(paths=("a.py",)), SESSION); nav.join(5)
    assert [e.verdict.verdict for e in finished(posted)] == ["error", "error", "clean", "error", "clean"]


def test_headroom_is_asked_per_turn(tmp_path):
    ok = [False]
    nav, reviewer, posted, log = make_nav([CLEAN], tmp_path, headroom=lambda: ok[0])
    nav.turn_ended(facts_with(paths=("a.py",)), SESSION); nav.join(1)
    assert NavigatorLog.read(log.path)[-1]["gate"] == "skip:headroom"
    ok[0] = True
    nav.turn_ended(facts_with(paths=("a.py",)), SESSION); nav.join(5)
    assert finished(posted)


def test_close_reaches_the_reviewer_and_stops_new_work(tmp_path):
    nav, reviewer, posted, log = make_nav([CLEAN], tmp_path)
    nav.close()
    assert reviewer.closed == 1
    nav.turn_ended(facts_with(paths=("a.py",)), SESSION); nav.join(1)
    assert reviewer.calls == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_chat_navigator.py -q -k "review or note or pending or interval or failures or headroom or close"`
Expected: FAIL with `ImportError: cannot import name 'Navigator'`

- [ ] **Step 3: Implement the worker**

Append to `src/tandem/chat/navigator.py` (add `from typing import Protocol`, and `ReviewFinished, ReviewStarted` to the events import):

```python
class ReviewError(RuntimeError):
    """A review that could not run: no shadow, the fork failed, the
    process died. Logged as an error verdict, never raised past the worker."""


@dataclass
class ReviewResult:
    structured: object | None   # claude's structured_output, or None
    text: str                   # the final assistant text (codex puts its JSON here)


class Reviewer(Protocol):
    harness: str

    def review(self, session, model: str, prompt: str, schema: dict,
               shadow_lock: threading.Lock) -> ReviewResult: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class Note:
    ref: str                    # the log record it came from
    navigator: str
    turn_harness: str
    verdict: Verdict

    @property
    def summary(self) -> str:
        head = self.verdict.note.split("\n", 1)[0]
        return head if len(head) <= 60 else head[:59] + "…"

    def trailer(self) -> str:
        """Appended after the user's text: their words lead, so titles and
        the `[tandem` skip rules are untouched."""
        lines = [f"[tandem navigator] {self.navigator} reviewed the previous {self.turn_harness} "
                 f"turn and flagged ({self.verdict.severity or 'note'}): {self.verdict.note}"]
        lines += [f"{e.file}:{e.line}" + (f" — {e.why}" if e.why else "") for e in self.verdict.evidence]
        return "\n\n" + "\n".join(lines)


_MAX_FAILURES = 3


class Navigator:
    """One review in flight, one pending slot (newest wins), one pending
    note. `turn_ended` is called on the dispatcher's worker after sync and
    returns at once; the review runs on this object's own thread and posts
    ReviewStarted / ReviewFinished through the window's queue."""

    def __init__(self, harness: str, cfg, reviewer: Reviewer, post: Callable[[LiveEvent], None],
                 log: NavigatorLog, *, headroom: Callable[[], bool] = lambda: True,
                 clock: Callable[[], float] = time.monotonic, diff=compute_diff):
        self.harness, self.cfg, self.reviewer, self.post, self.log = harness, cfg, reviewer, post, log
        self._headroom, self._clock, self._diff = headroom, clock, diff
        self.shadow_lock = threading.Lock()
        self._lock = threading.Lock()
        self._running = False
        self._thread: threading.Thread | None = None
        self._pending: tuple[TurnFacts, object] | None = None
        self._note: Note | None = None
        self._spoken_evidence: set[tuple[str, int]] = set()
        self._last_spoken = float("-inf")
        self._failures = 0
        self._disabled = False
        self._closed = False

    # -- what the dispatcher and the window ask ------------------------------

    def turn_ended(self, facts: TurnFacts, session) -> None:
        try:
            reason = gate(facts, navigator=self.harness, headroom_ok=self._headroom(),
                          interval_ok=self._clock() - self._last_spoken >= self.cfg.navigator_interval,
                          disabled=self._disabled or self._closed)
            if reason:
                self.log.review(facts, reason, None)
                return
            with self._lock:
                if self._running:
                    if self._pending is not None:
                        self.log.review(self._pending[0], "skip:replaced", None)
                    self._pending = (facts, session)
                    return
                self._start(facts, session)
        except Exception:                          # the navigator must never take the window down
            pass

    def take(self, harness: str) -> Note | None:
        with self._lock:
            note = self._note
            if note is None or not (self.cfg.navigator_deliver == "prompt" or harness == self.harness):
                return None
            self._note = None
        self.log.ridden(note.ref, harness)
        return note

    def pending(self) -> Note | None:
        return self._note

    def dismiss(self, feedback: str | None = None) -> bool:
        with self._lock:
            note, self._note = self._note, None
        if note is None:
            return False
        if feedback in ("good", "bad"):
            self.log.feedback(note.ref, feedback)
        return True

    def mark(self) -> str:
        if self._running:
            return "reviewing"
        return "note" if self._note is not None else ""

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._pending = None
        try:
            self.reviewer.close()
        except Exception:
            pass

    def join(self, timeout: float) -> None:
        """Tests: wait for the worker (and whatever it started) to finish."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            t = self._thread
            if t is None or not t.is_alive():
                with self._lock:
                    if not self._running:
                        return
            time.sleep(0.01)

    # -- the worker ----------------------------------------------------------

    def _start(self, facts: TurnFacts, session) -> None:
        """Call with _lock held."""
        self._running = True
        self._thread = threading.Thread(target=self._run, args=(facts, session),
                                        name="tandem-chat-navigator", daemon=True)
        self._thread.start()

    def _run(self, facts: TurnFacts, session) -> None:
        self.post(ReviewStarted(self.harness))
        started = self._clock()
        model = self.cfg.navigator_model
        try:
            diff = self._diff(session.cwd, facts.paths, facts.commands)
            result = self.reviewer.review(session, model, build_prompt(facts, diff), SCHEMA,
                                          self.shadow_lock)
            verdict = parse_verdict(result.structured, result.text, navigator=self.harness,
                                    model=model, elapsed=self._clock() - started)
        except Exception as exc:
            verdict = Verdict("error", error=f"{type(exc).__name__}: {exc}"[:200],
                              navigator=self.harness, model=model, elapsed=self._clock() - started)
        verdict = self._settle(verdict)
        ref = self.log.review(facts, "", verdict)
        if verdict.spoken:
            with self._lock:
                self._note = Note(ref, self.harness, facts.harness, verdict)
        self.post(ReviewFinished(self.harness, verdict))
        with self._lock:
            nxt, self._pending = self._pending, None
            if nxt is not None and not self._closed and not self._disabled:
                self._start(*nxt)
            else:
                self._running = False

    def _settle(self, verdict: Verdict) -> Verdict:
        """Failure counting, the three-strike switch, dedupe and the
        spoken-interval clock — everything that turns a parsed reply into
        the verdict the window paints and the log keeps."""
        if verdict.verdict == "error":
            self._failures += 1
            if self._failures >= _MAX_FAILURES:
                self._disabled = True
                return Verdict("off", error=verdict.error, navigator=verdict.navigator,
                               model=verdict.model, elapsed=verdict.elapsed)
            return verdict
        self._failures = 0
        if verdict.spoken:
            keys = {(e.file, e.line) for e in verdict.evidence}
            if keys and keys <= self._spoken_evidence:
                return Verdict("dup", navigator=verdict.navigator, model=verdict.model,
                               elapsed=verdict.elapsed)
            self._spoken_evidence |= keys
            self._last_spoken = self._clock()
        return verdict
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_chat_navigator.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/tandem/chat/navigator.py tests/test_chat_navigator.py
git commit -m "Run one navigator review at a time and keep its note for the next prompt"
```

---

### Task 7: The codex reviewer

**Files:**
- Create: `src/tandem/chat/reviewers.py`
- Modify: `src/tandem/chat/runtime/codex.py:86` (constructor), `:482-483` (`turn/start` params)
- Test: `tests/test_chat_reviewers.py`, `tests/test_chat_codex.py`

**Interfaces:**
- Consumes: `ops.fork_shadow`, `ops._sub_lock`, `CodexRuntime.run_turn`, `ReviewResult`, `ReviewError` (Task 6).
- Produces: `CodexRuntime(cfg, *, binary=None, output_schema: dict | None = None)`; `DenyAll` answers; `Collector` emit sink with `.text` and `.failures`; `CodexReviewer(cfg, store, *, binary=None)`; `make_reviewer(harness, cfg, store, *, binaries=None) -> Reviewer`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_chat_codex.py`:

```python
def test_an_output_schema_rides_turn_start(env):
    rt = CodexRuntime(ChatConfig(), binary=[sys.executable, str(FAKE)], output_schema={"type": "object"})
    rec = Recorder()
    rt.run_turn(env.session, "thread-1", "review", "", rec.emit, rec)
    assert env.params("turn/start")["outputSchema"] == {"type": "object"}
    plain = CodexRuntime(ChatConfig(), binary=[sys.executable, str(FAKE)])
    (env.tmp / "params.jsonl").unlink()
    plain.run_turn(env.session, "thread-1", "go", "", rec.emit, rec)
    assert "outputSchema" not in env.params("turn/start")
```

Create `tests/test_chat_reviewers.py`:

```python
"""The real reviewers over the fake CLIs: a codex review forks the shadow
and deletes the fork; a claude review forks at spawn and deletes what it
minted. Both run read-only and deny every approval."""

import json
import sys
import threading
from pathlib import Path

import pytest

from tandem import ops, paths
from tandem.chat.navigator import ReviewError
from tandem.chat.reviewers import ClaudeReviewer, CodexReviewer, Collector, DenyAll, make_reviewer
from tandem.config import ChatConfig
from tandem.harness import get_adapter

FAKE_CODEX = Path(__file__).parent / "fakes" / "fake_codex_appserver.py"
FAKE_CLAUDE = Path(__file__).parent / "fakes" / "fake_claude.py"


def test_deny_all_and_collector():
    from tandem.chat.events import ApprovalRequest, Failure, QuestionRequest, TextDelta
    d = DenyAll()
    assert d.approve(ApprovalRequest("command", "rm -rf")) == "deny"
    assert d.answer(QuestionRequest("which?", ("a",))) == ""
    c = Collector()
    c(TextDelta("{\"verdict\""))
    c(TextDelta(": \"clean\"}"))
    c(Failure("hm"))
    assert c.text == '{"verdict": "clean"}' and c.failures == ["hm"]


@pytest.fixture
def codex_env(env_factory, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_ARGV_OUT", str(tmp_path / "argv.json"))
    monkeypatch.setenv("FAKE_PARAMS_OUT", str(tmp_path / "params.jsonl"))
    monkeypatch.setenv("FAKE_REPLY_OUT", str(tmp_path / "reply.json"))
    env = env_factory()

    def params(method):
        for line in (tmp_path / "params.jsonl").read_text().splitlines():
            m = json.loads(line)
            if m["method"] == method:
                return m["params"]
        return None

    env.params = params
    return env


def test_codex_review_runs_on_a_deleted_fork_read_only_with_the_schema(codex_env, monkeypatch):
    env = codex_env
    seen = {}
    real_fork = ops.fork_shadow

    def spy(store, session):
        fid, fpath = real_fork(store, session)
        seen["id"], seen["path"], seen["existed"] = fid, fpath, fpath.exists()
        return fid, fpath

    monkeypatch.setattr(ops, "fork_shadow", spy)
    r = CodexReviewer(ChatConfig(navigator="codex"), env.store, binary=[sys.executable, str(FAKE_CODEX)])
    lock = threading.Lock()
    out = r.review(env.session, "gpt-x", "review please", {"type": "object"}, lock)
    assert out.text == "DONE" and out.structured is None
    assert seen["existed"] and not seen["path"].exists()             # forked, then deleted
    assert seen["id"] != env.session.native_id("codex")
    resume = env.params("thread/resume")
    assert resume["threadId"] == seen["id"]
    assert resume["approvalPolicy"] == "never" and resume["sandbox"] == "read-only"
    turn = env.params("turn/start")
    assert turn["outputSchema"] == {"type": "object"} and turn["model"] == "gpt-x"
    assert not lock.locked()                                         # released after the copy
    assert json.loads((env.tmp / "reply.json").read_text()) != {}    # the fake asked; we denied


def test_codex_review_denies_approvals(codex_env):
    env = codex_env
    r = CodexReviewer(ChatConfig(), env.store, binary=[sys.executable, str(FAKE_CODEX)])
    r.review(env.session, "", "p", {}, threading.Lock())
    assert json.loads((env.tmp / "reply.json").read_text()) == {"decision": "decline"}


def test_codex_review_failure_is_a_review_error_and_still_deletes_the_fork(codex_env, monkeypatch):
    env = codex_env
    monkeypatch.setenv("FAKE_CODEX_SCENARIO", "crash")
    forks = []
    real_fork = ops.fork_shadow
    monkeypatch.setattr(ops, "fork_shadow", lambda s, sess: forks.append(real_fork(s, sess)) or forks[-1])
    r = CodexReviewer(ChatConfig(), env.store, binary=[sys.executable, str(FAKE_CODEX)])
    with pytest.raises(ReviewError):
        r.review(env.session, "", "p", {}, threading.Lock())
    assert forks and not forks[0][1].exists()


def test_codex_review_without_a_shadow_is_a_review_error(env_factory, monkeypatch):
    env = env_factory(active="codex", seed_active=False)       # codex has no id yet
    r = CodexReviewer(ChatConfig(), env.store, binary=[sys.executable, str(FAKE_CODEX)])
    monkeypatch.setattr(ops, "_create_codex_shadow_late", lambda *a, **k: (_ for _ in ()).throw(ops.SyncSetupError("no")))
    with pytest.raises(ReviewError):
        r.review(env.session, "", "p", {}, threading.Lock())


def test_make_reviewer_picks_by_harness(env_factory):
    env = env_factory()
    assert isinstance(make_reviewer("codex", ChatConfig(), env.store), CodexReviewer)
    assert isinstance(make_reviewer("claude", ChatConfig(), env.store), ClaudeReviewer)
    with pytest.raises(ValueError):
        make_reviewer("opencode", ChatConfig(), env.store)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_chat_reviewers.py tests/test_chat_codex.py -q -k "schema or review or deny or collector"`
Expected: FAIL with `ModuleNotFoundError: No module named 'tandem.chat.reviewers'` and `TypeError: __init__() got an unexpected keyword argument 'output_schema'`

- [ ] **Step 3: Add `output_schema` to the codex runtime**

In `src/tandem/chat/runtime/codex.py`:

```python
    def __init__(self, cfg, *, binary: list[str] | None = None, output_schema: dict | None = None):
        self.cfg = cfg
        self.binary = list(binary) if binary else ["codex"]
        self.output_schema = output_schema      # constrains the final message (the navigator's verdict)
```

And in `run_turn`, the `turn/start` params:

```python
            turn = cp.TurnStartParams(threadId=thread_id, input=[{"type": "text", "text": prompt}],
                                      model=model or None, outputSchema=self.output_schema)
```

- [ ] **Step 4: Create the reviewers module**

Create `src/tandem/chat/reviewers.py`:

```python
"""The per-harness half of the navigator: fork the shadow, run one headless
turn on the fork with a fresh runtime client, hand back what the model
said, delete the fork. Every reviewer runs read-only, denies every
approval, and never touches the dispatcher's own runtime instance."""

from __future__ import annotations

import dataclasses
import json
import threading

from .. import ops, paths
from ..harness import get_adapter
from ..sync import SyncSetupError
from .events import ApprovalRequest, Failure, LiveEvent, QuestionRequest, TextDelta
from .navigator import ReviewError, ReviewResult
from .runtime.claude import ClaudeRuntime
from .runtime.codex import CodexRuntime

_CLAUDE_REVIEW_TOOLS = ["Read", "Grep", "Glob", "Bash(git diff *)", "Bash(git log *)"]


class DenyAll:
    """The review's Answers: nobody is at the keyboard for a fork."""

    def approve(self, req: ApprovalRequest) -> str:
        return "deny"

    def answer(self, req: QuestionRequest) -> str:
        return ""


class Collector:
    """The review's emit sink: the final text and any failures, nothing painted."""

    def __init__(self) -> None:
        self._parts: list[str] = []
        self.failures: list[str] = []

    def __call__(self, ev: LiveEvent) -> None:
        if isinstance(ev, TextDelta):
            self._parts.append(ev.text)
        elif isinstance(ev, Failure):
            self.failures.append(ev.message)

    @property
    def text(self) -> str:
        return "".join(self._parts)


class CodexReviewer:
    harness = "codex"

    def __init__(self, cfg, store, *, binary: list[str] | None = None):
        self.cfg, self.store, self.binary = cfg, store, binary
        self._rt: CodexRuntime | None = None
        self._lock = threading.Lock()

    def review(self, session, model: str, prompt: str, schema: dict,
               shadow_lock: threading.Lock) -> ReviewResult:
        try:
            with shadow_lock, ops._sub_lock():
                fork_id, fork_path = ops.fork_shadow(self.store, session)
        except SyncSetupError as exc:
            raise ReviewError(f"codex fork: {exc}") from exc
        cfg = dataclasses.replace(self.cfg, skip_permissions=False,
                                  codex_approval_policy="never", codex_sandbox="read-only")
        rt = CodexRuntime(cfg, binary=self.binary, output_schema=schema)
        col = Collector()
        try:
            with self._lock:
                self._rt = rt
            outcome = rt.run_turn(session, fork_id, prompt, model, col, DenyAll())
        finally:
            with self._lock:
                self._rt = None
            fork_path.unlink(missing_ok=True)
        if outcome.status != "completed":
            raise ReviewError(outcome.error or f"codex review {outcome.status}")
        return ReviewResult(None, col.text)

    def close(self) -> None:
        with self._lock:
            rt = self._rt
        if rt is not None:
            rt.close()


class ClaudeReviewer:
    harness = "claude"

    def __init__(self, cfg, *, binary: list[str] | None = None):
        self.cfg, self.binary = cfg, binary
        self._rt: ClaudeRuntime | None = None
        self._lock = threading.Lock()

    def review(self, session, model: str, prompt: str, schema: dict,
               shadow_lock: threading.Lock) -> ReviewResult:
        sid = session.native_id("claude")
        adapter = get_adapter("claude")
        if not sid or adapter.transcript_path(session.cwd, sid) is None:
            raise ReviewError("claude shadow transcript missing")
        cfg = dataclasses.replace(self.cfg, skip_permissions=False)
        extra = ["--fork-session", "--json-schema", json.dumps(schema),
                 "--allowedTools", *_CLAUDE_REVIEW_TOOLS]
        forked: dict = {}
        released = threading.Event()

        def on_init(new_id: str) -> None:
            # the fork has read the shadow once init arrives; the dispatcher
            # may drain into it again from here
            forked["id"] = new_id
            if not released.is_set():
                released.set()
                shadow_lock.release()

        rt = ClaudeRuntime(cfg, binary=self.binary, extra_args=extra, on_init=on_init)
        col = Collector()
        shadow_lock.acquire()
        try:
            with self._lock:
                self._rt = rt
            outcome = rt.run_turn(session, sid, prompt, model, col, DenyAll())
        finally:
            if not released.is_set():
                released.set()
                shadow_lock.release()
            with self._lock:
                self._rt = None
            new_id = forked.get("id")
            if new_id and new_id != sid:
                paths.claude_transcript_path(session.cwd, new_id).unlink(missing_ok=True)
        if outcome.status != "completed":
            raise ReviewError(outcome.error or f"claude review {outcome.status}")
        return ReviewResult(outcome.structured, col.text)

    def close(self) -> None:
        with self._lock:
            rt = self._rt
        if rt is not None:
            rt.close()


def make_reviewer(harness: str, cfg, store, *, binaries: dict | None = None):
    binaries = binaries or {}
    if harness == "codex":
        return CodexReviewer(cfg, store, binary=binaries.get("codex"))
    if harness == "claude":
        return ClaudeReviewer(cfg, binary=binaries.get("claude"))
    raise ValueError(f"{harness} cannot be the navigator")
```

`ClaudeRuntime(extra_args=..., on_init=...)` does not exist yet; Task 8 adds it. For this task's tests to import, add those two keyword arguments to `ClaudeRuntime.__init__` now with no behaviour: store them as `self.extra_args = list(extra_args or [])` and `self.on_init = on_init`. Task 8 wires them.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_chat_reviewers.py tests/test_chat_codex.py -q`
Expected: PASS for every test except the claude ones (none exist yet in this task)

- [ ] **Step 6: Commit**

```bash
git add src/tandem/chat/reviewers.py src/tandem/chat/runtime/codex.py src/tandem/chat/runtime/claude.py tests/test_chat_reviewers.py tests/test_chat_codex.py
git commit -m "Review on a forked codex rollout, read-only, with the verdict schema"
```

---

### Task 8: The claude reviewer

**Files:**
- Modify: `src/tandem/chat/runtime/claude.py:54-60` (constructor), `:64-76` (`argv`), `:104-108` / `:167` (`system/init`)
- Modify: `tests/fakes/fake_claude.py` (a `review` scenario)
- Test: `tests/test_chat_claude.py`, `tests/test_chat_reviewers.py`

**Interfaces:**
- Consumes: `ClaudeReviewer` from Task 7.
- Produces: `ClaudeRuntime(cfg, *, binary=None, extra_args: list[str] | None = None, on_init: Callable[[str], None] | None = None)`; `argv()` appends `extra_args`; `handle_line` calls `on_init(session_id)` on `system/init`.

- [ ] **Step 1: Add the `review` scenario to the fake**

In `tests/fakes/fake_claude.py`, extend the docstring list with `review    emits init under a forked id, one JSON delta, and a result carrying structured_output`, set `FORK_SID = "fake-claude-fork"` next to `SID`, and add in `main()` right after the `scenario == "text"` branch:

```python
    if scenario == "review":
        verdict = {"verdict": "speak", "severity": "warn", "note": "loop swallows errors",
                   "evidence": [{"file": "s.py", "line": 9, "why": "bare except"}]}
        delta(json.dumps(verdict))
        out({"type": "result", "subtype": "success", "is_error": False, "num_turns": 1,
             "session_id": FORK_SID, "result": json.dumps(verdict), "structured_output": verdict,
             "usage": {"input_tokens": 1, "output_tokens": 1}})
        return
```

And make the init line use the forked id when `--fork-session` is in argv:

```python
    sid = FORK_SID if "--fork-session" in sys.argv else SID
    out({"type": "system", "subtype": "init", "session_id": sid, "model": "fake", "tools": ["Bash"],
         "cwd": os.getcwd(), "slash_commands": [], "claude_code_version": "0.0.0"})
```

(Every other scenario keeps emitting `SID`; only the init line changes.)

- [ ] **Step 2: Write the failing tests**

Append to `tests/test_chat_claude.py`:

```python
def test_extra_args_ride_argv_after_the_standard_flags():
    rt = ClaudeRuntime(ChatConfig(), extra_args=["--fork-session", "--json-schema", "{}"])
    argv = rt.argv("sid-1", fresh=False, model="")
    assert argv[-3:] == ["--fork-session", "--json-schema", "{}"]
    assert "--resume" in argv


def test_on_init_gets_the_session_id_the_child_announces():
    seen = []
    rt = ClaudeRuntime(ChatConfig(), on_init=seen.append)
    rec = Recorder()
    rt.handle_line({"type": "system", "subtype": "init", "session_id": "fresh-id"}, rec.emit, rec, lambda o: None)
    rt.handle_line({"type": "system", "subtype": "status"}, rec.emit, rec, lambda o: None)
    assert seen == ["fresh-id"]
```

Append to `tests/test_chat_reviewers.py`:

```python
@pytest.fixture
def claude_env(env_factory, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_ARGV_OUT", str(tmp_path / "argv.json"))
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", "review")
    env = env_factory()                      # active claude, its shadow seeded on disk
    env.argv = lambda: json.loads((tmp_path / "argv.json").read_text())
    return env


def test_claude_review_forks_at_spawn_deletes_the_fork_and_returns_structured_output(claude_env):
    env = claude_env
    fork_file = paths.claude_transcript_path(env.session.cwd, "fake-claude-fork")
    fork_file.parent.mkdir(parents=True, exist_ok=True)
    fork_file.write_text("{}\n")
    shadow = get_adapter("claude").transcript_path(env.session.cwd, env.session.native_id("claude"))
    before = shadow.read_bytes()
    r = ClaudeReviewer(ChatConfig(navigator="claude"), binary=[sys.executable, str(FAKE_CLAUDE)])
    lock = threading.Lock()
    out = r.review(env.session, "claude-x", "review please", {"type": "object"}, lock)
    assert out.structured["verdict"] == "speak" and "loop swallows" in out.text
    argv = env.argv()
    assert "--fork-session" in argv and argv[argv.index("--json-schema") + 1] == '{"type": "object"}'
    i = argv.index("--allowedTools")
    assert argv[i + 1:i + 6] == ["Read", "Grep", "Glob", "Bash(git diff *)", "Bash(git log *)"]
    assert "--permission-mode" not in argv                          # never bypass on a review
    assert argv[argv.index("--model") + 1] == "claude-x"
    assert not fork_file.exists() and shadow.read_bytes() == before
    assert not lock.locked()


def test_claude_review_releases_the_shadow_lock_when_init_arrives(claude_env):
    """The dispatcher's next drain must not wait for the whole review."""
    env = claude_env
    r = ClaudeReviewer(ChatConfig(), binary=[sys.executable, str(FAKE_CLAUDE)])
    lock = threading.Lock()
    states = []

    from tandem.chat.runtime import claude as claude_mod
    orig = claude_mod.ClaudeRuntime.handle_line

    def spy(self, m, emit, answers, send):
        is_init = m.get("type") == "system" and m.get("subtype") == "init"
        before = lock.locked()
        out = orig(self, m, emit, answers, send)
        if is_init:
            states.append((before, lock.locked()))   # held going in, released by on_init
        return out

    claude_mod.ClaudeRuntime.handle_line = spy
    try:
        r.review(env.session, "", "p", {}, lock)
    finally:
        claude_mod.ClaudeRuntime.handle_line = orig
    assert states == [(True, False)]


def test_claude_review_without_a_shadow_is_a_review_error(env_factory):
    env = env_factory(seed_active=False)     # claude's file does not exist yet
    r = ClaudeReviewer(ChatConfig(), binary=[sys.executable, str(FAKE_CLAUDE)])
    with pytest.raises(ReviewError):
        r.review(env.session, "", "p", {}, threading.Lock())


def test_claude_review_crash_is_a_review_error_and_releases_the_lock(claude_env, monkeypatch):
    env = claude_env
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", "crash")
    r = ClaudeReviewer(ChatConfig(), binary=[sys.executable, str(FAKE_CLAUDE)])
    lock = threading.Lock()
    with pytest.raises(ReviewError):
        r.review(env.session, "", "p", {}, lock)
    assert not lock.locked()
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest tests/test_chat_claude.py tests/test_chat_reviewers.py -q -k "extra_args or on_init or claude_review"`
Expected: FAIL (`argv` lacks the extra flags; `on_init` never called; the reviewer's spawn uses no fork flag)

- [ ] **Step 4: Wire `extra_args` and `on_init` into the claude runtime**

In `src/tandem/chat/runtime/claude.py`:

```python
    def __init__(self, cfg, *, binary: list[str] | None = None,
                 extra_args: list[str] | None = None,
                 on_init: Callable[[str], None] | None = None):
        self.cfg = cfg
        self.binary = list(binary) if binary else ["claude"]
        # the navigator's review flags (--fork-session, --json-schema, --allowedTools)
        self.extra_args = list(extra_args or [])
        # told the session id the child announces at init — a forked review
        # learns the id it has to delete afterwards
        self.on_init = on_init
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._interrupted = False
        self._streamed_text = False   # did the current message stream text deltas?
```

At the end of `argv`, before `return argv`:

```python
        argv += self.extra_args
```

In `handle_line`, before the final `return None`, add a branch (place it after the `rate_limit_event` branch):

```python
        if t == "system" and m.get("subtype") == "init":
            if self.on_init is not None and m.get("session_id"):
                self.on_init(str(m["session_id"]))
            return None
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_chat_claude.py tests/test_chat_reviewers.py -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/tandem/chat/runtime/claude.py tests/fakes/fake_claude.py tests/test_chat_claude.py tests/test_chat_reviewers.py
git commit -m "Review on a forked claude session with a schema and read-only tools"
```

---

### Task 9: Dispatcher integration

**Files:**
- Modify: `src/tandem/chat/dispatch.py` (`__init__`, `_run`, `close`)
- Test: `tests/test_chat_dispatch.py`

**Interfaces:**
- Consumes: `FactsCollector`, `Navigator.shadow_lock / take / turn_ended / close`, `Note.trailer / summary`, `TurnStarted.carried`.
- Produces: `Dispatcher(store, session, runtimes, emit, answers, *, meters=None, add_meters=None, first_turn=None, navigator=None)`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_chat_dispatch.py`:

```python
from tandem.chat.events import Evidence, ToolStarted, Verdict
from tandem.chat.navigator import Note


class StubNavigator:
    """Records what the dispatcher hands it and scripts what it hands back."""

    def __init__(self, note=None):
        self.shadow_lock = threading.Lock()
        self.note = note
        self.takes, self.ended, self.closed = [], [], 0
        self.lock_held_during_sync = []

    def take(self, harness):
        self.takes.append(harness)
        n, self.note = self.note, None
        return n if n is not None and harness == "codex" else None

    def turn_ended(self, facts, session):
        self.ended.append((facts, session))

    def close(self):
        self.closed += 1


def make_note(text="bad loop"):
    return Note("ref-1", "codex", "claude", Verdict("speak", severity="block", note=text,
                                                     evidence=(Evidence("s.py", 12, "w"),)))


def run_one(env, nav, text, harness="claude"):
    events = []
    done = threading.Event()

    def emit(ev):
        events.append(ev)
        if isinstance(ev, Idle):
            done.set()

    runtimes = {"claude": FakeRuntime("claude", env), "codex": FakeRuntime("codex", env)}
    d = Dispatcher(env.store, env.session, runtimes, emit, Answers(), navigator=nav)
    assert d.submit(text) == ""
    assert done.wait(5)
    return d, runtimes, events


def test_facts_reach_the_navigator_after_sync(env_factory):
    env = env_factory()
    nav = StubNavigator()
    d, runtimes, events = run_one(env, nav, "fix it")
    assert len(nav.ended) == 1
    facts, session = nav.ended[0]
    assert facts.harness == "claude" and facts.prompt == "fix it" and facts.status == "completed"
    assert facts.carried_note is False and facts.first_turn is False
    assert facts.final_text == "claude says hi"
    assert session.tandem_id == env.session.tandem_id
    # turn_ended ran before Idle was announced
    assert isinstance(events[-1], Idle)


def test_the_first_turn_is_flagged(env_factory):
    env = env_factory()
    nav = StubNavigator()
    events, done = [], threading.Event()
    emit = lambda ev: (events.append(ev), isinstance(ev, Idle) and done.set())
    runtimes = {"claude": FakeRuntime("claude", env), "codex": FakeRuntime("codex", env)}
    d = Dispatcher(env.store, env.session, runtimes, emit, Answers(), first_turn=lambda: None, navigator=nav)
    d.submit("hello"); assert done.wait(5)
    assert nav.ended[0][0].first_turn is True


def test_a_pending_note_rides_the_prompt_as_a_trailer(env_factory):
    env = env_factory()
    nav = StubNavigator(note=make_note())
    d, runtimes, events = run_one(env, nav, "/codex why?")
    native_id, prompt, model = runtimes["codex"].calls[0]
    assert prompt.startswith("why?\n\n[tandem navigator] codex reviewed the previous claude turn")
    assert "s.py:12 — w" in prompt
    started = [e for e in events if isinstance(e, TurnStarted)][0]
    assert started.prompt == "why?" and started.carried == "bad loop"
    assert nav.ended[0][0].carried_note is True and nav.ended[0][0].prompt == "why?"
    assert nav.takes == ["codex"]


def test_a_bare_route_consumes_no_note(env_factory):
    env = env_factory()
    nav = StubNavigator(note=make_note())
    runtimes = {"claude": FakeRuntime("claude", env), "codex": FakeRuntime("codex", env)}
    d = Dispatcher(env.store, env.session, runtimes, lambda ev: None, Answers(), navigator=nav)
    assert d.submit("/codex").startswith("default → codex")
    assert nav.takes == [] and nav.note is not None


def test_the_shadow_lock_is_held_across_prepare_and_sync(env_factory, monkeypatch):
    env = env_factory()
    nav = StubNavigator()
    seen = []
    real_prepare, real_sync = dispatch.ops.prepare_turn, dispatch.ops.sync_after_turn
    monkeypatch.setattr(dispatch.ops, "prepare_turn",
                        lambda *a, **k: (seen.append(("prepare", nav.shadow_lock.locked())), real_prepare(*a, **k))[1])
    monkeypatch.setattr(dispatch.ops, "sync_after_turn",
                        lambda *a, **k: (seen.append(("sync", nav.shadow_lock.locked())), real_sync(*a, **k))[1])
    run_one(env, nav, "go")
    assert seen == [("prepare", True), ("sync", True)]
    assert not nav.shadow_lock.locked()


def test_without_a_navigator_nothing_changes(env_factory):
    env = env_factory()
    d, runtimes, events = run_one(env, None, "go")
    assert runtimes["claude"].calls[0][1] == "go"
    assert [e for e in events if isinstance(e, TurnStarted)][0].carried == ""


def test_a_navigator_that_raises_does_not_break_the_turn(env_factory):
    env = env_factory()
    nav = StubNavigator()
    nav.turn_ended = lambda facts, session: (_ for _ in ()).throw(RuntimeError("nav bug"))
    d, runtimes, events = run_one(env, nav, "go")
    assert not any(isinstance(e, Failure) for e in events)
    assert isinstance(events[-1], Idle)


def test_close_reaches_the_navigator(env_factory):
    env = env_factory()
    nav = StubNavigator()
    runtimes = {"claude": FakeRuntime("claude", env), "codex": FakeRuntime("codex", env)}
    d = Dispatcher(env.store, env.session, runtimes, lambda ev: None, Answers(), navigator=nav)
    d.close()
    assert nav.closed == 1
```

`Answers` is the stub class already defined in `tests/test_chat_dispatch.py` (used as `Dispatcher(..., Answers())` by the existing tests).

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_chat_dispatch.py -q -k "navigator or note or facts or shadow_lock or first_turn_is_flagged"`
Expected: FAIL with `TypeError: __init__() got an unexpected keyword argument 'navigator'`

- [ ] **Step 3: Wire the navigator into the dispatcher**

In `src/tandem/chat/dispatch.py`, add `import contextlib` and `from .navigator import FactsCollector`. Constructor:

```python
    def __init__(self, store, session, runtimes: dict, emit: Callable[[LiveEvent], None],
                 answers: Answers, *, meters: dict | None = None,
                 add_meters: Callable[[object], None] | None = None,
                 first_turn: Callable[[], None] | None = None,
                 navigator=None):
        ...
        # the second harness that reviews each turn (chat/navigator.py), or
        # None: then nothing here changes — no facts, no trailer, no lock
        self.navigator = navigator
```

Replace `_run` with:

```python
    def _run(self, item: Pending, spoken: int) -> None:
        harness = item.harness
        ran = False
        nav = self.navigator
        # a note the navigator left rides this prompt as a trailer — taken
        # now, not at submit, so a note that lands while a prompt is queued
        # still reaches it
        note = nav.take(harness) if nav is not None else None
        prompt = item.prompt + note.trailer() if note is not None else item.prompt
        first = self._first_turn is not None
        self.emit(TurnStarted(harness, item.model, item.prompt,
                              carried=note.summary if note is not None else ""))
        facts = (FactsCollector(harness, item.prompt, note is not None, first, self.emit)
                 if nav is not None else None)
        emit = facts.emit if facts is not None else self.emit
        lock = nav.shadow_lock if nav is not None else contextlib.nullcontext()
        outcome = None
        try:
            if self._first_turn is not None:
                self._first_turn()
                self._first_turn = None
            problems = self._validate(harness)
            if problems:
                self.emit(Failure(f"{harness} transcript: " + "; ".join(problems)))
                self.emit(TurnFinished("failed", ""))
                return
            session = self.session
            if session.native_id(harness):
                # prepare_turn also seeds any participant whose harness has
                # never run (a fresh session's active claude has no file yet),
                # and hands back the session that knows the ids it minted.
                # Under the navigator's lock: a claude review fork reads the
                # shadow this drains into.
                with lock:
                    session = self.session = ops.prepare_turn(self.store, session, harness)
            # else: nothing to fast-forward and no file to drain into yet — the
            # first turn on a never-run codex starts context-less, as `tandem run
            # --on codex` does, and sync_after_turn translates it outward once
            # its thread id is adopted below. Nothing needs seeding there
            # either: an active codex with no id is the only harness a fresh
            # pairing leaves fileless, and every other side already has one.
            outcome = self.runtimes[harness].run_turn(
                session, session.native_id(harness), prompt, item.model, emit, self.answers)
            ran = True      # from here on the runtime has emitted its own TurnFinished
            if outcome.native_id:
                self.session = ops.adopt_native_id(self.store, session, harness, outcome.native_id)
            # the target becomes the default — its file holds the turn, partial
            # or not — unless the user named another harness since this turn
            # started: a bare `/codex` typed while claude worked is the later word
            if self._spoken == spoken:
                self._set_default(harness)
            else:
                self._reload_session()          # keep the ids this turn minted alongside that word
            # A turn that did not complete can have recorded the prompt and no
            # answer (a model call that 401s does exactly that). Synced
            # outward as-is it leaves every other session ending on a user
            # message, which opencode's dry-resume check rejects — wedging
            # every later turn there. Close it in the shadows as we sync.
            quarantine_pre = self._failed_turns(harness)
            with lock:
                ops.sync_after_turn(self.store, self.session, harness,
                                    close_note=_close_note(harness, outcome))
            self._report_quarantine(harness, quarantine_pre)
            self.store.touch_used(self.session.tandem_id)
            if self._add_meters is not None:
                self._add_meters(self.session)
            meter = self.meters.get(harness)
            if meter is not None:
                meter.poll()
        except SyncSetupError as exc:
            self.emit(Failure(f"sync: {exc}"))
            self._finish_unrun(ran)
        except Exception as exc:                       # a runtime bug must not kill the window
            self.emit(Failure(f"{harness}: {type(exc).__name__}: {exc}"))
            self._finish_unrun(ran)
        finally:
            # free before the announcement: a window that pumps straight out
            # of this Idle — even synchronously, on this thread — must find
            # the dispatcher idle, or the queued turn stalls until the next
            # submit and then runs out of order
            with self._lock:
                self._current = None
                self._running = False
            if facts is not None and outcome is not None:
                try:
                    nav.turn_ended(facts.finish(outcome.status), self.session)
                except Exception:
                    pass                               # the navigator must never take the window down
            self.emit(Idle())
```

In `close`, after `self.interrupt()`:

```python
        if self.navigator is not None:
            try:
                self.navigator.close()
            except Exception:
                pass
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_chat_dispatch.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/tandem/chat/dispatch.py tests/test_chat_dispatch.py
git commit -m "Hand each finished turn to the navigator and ride its note on the next prompt"
```

---

### Task 10: Activity state and review rows

**Files:**
- Modify: `src/tandem/chat/activity.py`
- Modify: `src/tandem/chat/render.py` (`turn_started`, new `review`)
- Test: `tests/test_chat_activity.py`, `tests/test_chat_render.py`

**Interfaces:**
- Consumes: `ReviewStarted`, `ReviewFinished`, `Verdict`, `TurnStarted.carried`.
- Produces: `Activity.reviewing: str`; `Activity.animating: bool`; `Activity.text()` shows `⠋ codex reviewing · 12s` while reviewing and idle; `Screen.review(ev: ReviewFinished)`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_chat_activity.py`:

```python
from tandem.chat.events import ReviewFinished, ReviewStarted, Verdict


def test_a_review_shows_on_the_line_only_while_no_turn_runs(act):
    a, clock = act
    a.on_event(ReviewStarted("codex"))
    assert a.active is False and a.reviewing == "codex" and a.animating is True
    assert a.text() == "⠋ codex reviewing · 0s"
    clock.now += 12
    assert a.text() == "⠋ codex reviewing · 12s"
    a.on_event(TurnStarted("claude", "", "go"))
    assert a.text().startswith("⠋ claude · starting")          # the turn owns the row
    a.on_event(TurnFinished("completed", "")); a.on_event(Idle())
    assert a.text() == "⠋ codex reviewing · 12s"                # back to the review, still counting
    a.on_event(ReviewFinished("codex", Verdict("clean")))
    assert a.reviewing == "" and a.text() == "" and a.animating is False


def test_the_review_clock_starts_at_review_start(act):
    a, clock = act
    a.on_event(TurnStarted("claude", "", "go"))
    clock.now += 30
    a.on_event(TurnFinished("completed", "")); a.on_event(Idle())
    a.on_event(ReviewStarted("codex"))
    clock.now += 3
    assert a.text() == "⠋ codex reviewing · 3s"
```

Append to `tests/test_chat_render.py` (read its top for the `Out` helper and `Screen` construction used by the other tests, and reuse them; the assertions below are against `out.text()` with `color=False`):

```python
from tandem.chat.events import Evidence, ReviewFinished, TurnStarted, Verdict


def test_a_clean_review_is_a_one_line_receipt(screen_factory):
    s, out = screen_factory()
    s.review(ReviewFinished("codex", Verdict("clean", elapsed=18.2)))
    assert out.text().rstrip("\r\n").endswith("  codex reviewed · no concerns · 18s")


@pytest.mark.parametrize("kind", ["dup", "empty"])
def test_dup_and_empty_read_as_clean(screen_factory, kind):
    s, out = screen_factory()
    s.review(ReviewFinished("codex", Verdict(kind, elapsed=2)))
    assert "no concerns" in out.text()


def test_a_spoken_review_prints_the_note_and_evidence(screen_factory):
    s, out = screen_factory(cols=40)
    v = Verdict("speak", severity="block", elapsed=21.0,
                note="The retry loop swallows ShadowBusy, so a busy shadow drops lines.",
                evidence=(Evidence("sync.py", 142, "except ShadowBusy: continue"), Evidence("a.py", 1)))
    s.review(ReviewFinished("codex", v))
    text = out.text()
    assert "codex ⚑ block · 21s" in text
    assert "  The retry loop swallows ShadowBusy, so" in text          # wrapped, indented
    assert "  sync.py:142 — except ShadowBusy: continue" in text and "  a.py:1\r\n" in text
    assert all(len(line) <= 40 for line in text.replace("\r", "").split("\n"))


def test_an_error_review_paints_nothing_and_off_paints_one_line(screen_factory):
    s, out = screen_factory()
    s.review(ReviewFinished("codex", Verdict("error", error="boom")))
    assert out.text() == ""
    s.review(ReviewFinished("codex", Verdict("off", error="claude exited 3")))
    assert "codex navigator off: claude exited 3" in out.text()


def test_a_carried_note_shows_under_the_prompt_row(screen_factory):
    s, out = screen_factory()
    s.turn_started(TurnStarted("codex", "", "why?", carried="bad loop"))
    assert "you → codex  why?" in out.text() and "  + navigator note: bad loop" in out.text()
```

If `tests/test_chat_render.py` has no `screen_factory` fixture, add one at its top matching how its other tests build a `Screen`:

```python
@pytest.fixture
def screen_factory():
    def make(rows=24, cols=60, cfg=None):
        out = Out()
        return Screen(out, rows, cols, cfg or ChatConfig(), color=False), out
    return make
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_chat_activity.py tests/test_chat_render.py -q -k "review or carried"`
Expected: FAIL with `AttributeError: 'Activity' object has no attribute 'reviewing'` / `'Screen' object has no attribute 'review'`

- [ ] **Step 3: Extend the activity state**

In `src/tandem/chat/activity.py`, import `ReviewFinished, ReviewStarted` and change `Activity`:

```python
    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self.active = False
        self.waiting = False            # an approval or a question is up
        self.harness = ""
        self.phase = ""
        self.last_elapsed = 0.0         # the turn that just ended
        self._started = 0.0
        self.reviewing = ""             # the navigator, while its review runs
        self._review_started = 0.0

    @property
    def animating(self) -> bool:
        """Something on the line moves on its own clock: a running turn's
        spinner, or the review spinner while no turn is running."""
        return (self.active and not self.waiting) or (bool(self.reviewing) and not self.active)

    def on_event(self, ev: LiveEvent) -> None:
        if isinstance(ev, ReviewStarted):
            self.reviewing, self._review_started = ev.harness, self._clock()
            return
        if isinstance(ev, ReviewFinished):
            self.reviewing = ""
            return
        if isinstance(ev, TurnStarted):
            ...  # unchanged from here down
```

And in `text()`, replace the first `if not self.active: return ""`:

```python
        if not self.active:
            if not self.reviewing:
                return ""
            elapsed = self._clock() - self._review_started
            frame = _FRAMES[int(elapsed / _FRAME_SECONDS) % len(_FRAMES)]
            return f"{frame} {self.reviewing} reviewing · {elapsed_text(elapsed)}"
```

- [ ] **Step 4: Paint the rows**

In `src/tandem/chat/render.py`, import `ReviewFinished` and add `import textwrap`. In `turn_started`, after the prompt row:

```python
        if ev.carried:
            self.line(self._dim(f"  + navigator note: {_safe(ev.carried)}"))
```

Add after `failure`:

```python
    def review(self, ev: ReviewFinished) -> None:
        """Every finished review gets a row: a receipt when clean (dup and
        empty read as clean — nothing to act on), the note in full when
        spoken, one line when the navigator switched itself off, nothing
        for a failed review (it is in the log)."""
        v = ev.verdict
        if v.verdict == "error":
            return
        if v.verdict == "off":
            self.line(self._dim(f"  {ev.harness} navigator off: {_safe(v.error)}"))
            return
        if not v.spoken:
            self.line(self._dim(f"  {ev.harness} reviewed · no concerns · {elapsed_text(v.elapsed)}"))
            return
        self.line()
        self.line(self._bold(f"{ev.harness} ⚑ {v.severity or 'note'} · {elapsed_text(v.elapsed)}"))
        width = max(10, self.cols - 2)
        for para in _safe(v.note).split("\n"):
            for row in textwrap.wrap(para, width) or [""]:
                self.line("  " + row)
        for e in v.evidence:
            where = f"{_safe(e.file)}:{e.line}" + (f" — {_safe(e.why)}" if e.why else "")
            self.line(self._dim("  " + _clip(where, width)))
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_chat_activity.py tests/test_chat_render.py -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/tandem/chat/activity.py src/tandem/chat/render.py tests/test_chat_activity.py tests/test_chat_render.py
git commit -m "Show a running review on the activity line and paint its verdict"
```

---

### Task 11: Window wiring

**Files:**
- Modify: `src/tandem/chat/window.py` (`WINDOW_COMMANDS`, `Window.__init__`, `bar_line`, `status_line`, `tick_seconds`, `handle_event`, `handle_input`, `run_chat`)
- Test: `tests/test_chat_window.py`

**Interfaces:**
- Consumes: `Navigator` (Task 6), `make_reviewer` (Task 7), `NavigatorLog`, `log_path`, `Screen.review`, `Activity.animating`.
- Produces: `Window(..., navigator=None)`; `/note [dismiss|good|bad]`; the `navigator <h> · <deliver>` fragment in `/status`; `run_chat` builds the navigator when `cfg.navigator` names a participant.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_chat_window.py`:

```python
from tandem.chat.events import Evidence, ReviewFinished, ReviewStarted, Verdict
from tandem.chat.navigator import Note


class StubNavigator:
    harness = "codex"

    def __init__(self):
        self.note, self.running, self.dismissed, self.closed = None, False, [], 0

    def mark(self):
        return "reviewing" if self.running else ("note" if self.note else "")

    def pending(self):
        return self.note

    def dismiss(self, feedback=None):
        had = self.note is not None
        self.dismissed.append(feedback); self.note = None
        return had

    def close(self):
        self.closed += 1


def spoken_note():
    return Note("r1", "codex", "claude", Verdict("speak", severity="block", note="bad loop",
                                                  evidence=(Evidence("s.py", 12, "w"),)))


def make_nav_window(env, **kw):
    w, d, out, answers = make_window(env, **kw)
    w.navigator = StubNavigator()
    return w, d, out, answers


def test_the_bar_marks_the_navigator_slot(env_factory):
    env = env_factory(); w, d, out, _ = make_nav_window(env)
    w.bar.cols = 100
    assert "codex ○ " in w.bar_line() and "reviewing" not in w.bar_line()
    w.navigator.running = True
    assert "codex ○ reviewing" in w.bar_line()
    w.navigator.running = False; w.navigator.note = spoken_note()
    assert "codex ○ note" in w.bar_line()


def test_the_navigator_mark_sits_beside_skip_perms(env_factory):
    env = env_factory()
    w, d, out, _ = make_nav_window(env, cfg=ChatConfig(skip_permissions=True))
    w.bar.cols = 120; w.navigator.running = True
    line = w.bar_line()
    assert "codex ○ skip-perms · reviewing" in line and "claude ● skip-perms" in line


def test_a_review_landing_while_idle_paints_at_once(env_factory):
    env = env_factory(); w, d, out, _ = make_nav_window(env)
    w.handle_event(ReviewStarted("codex"))
    assert last_separator(out).startswith("── ⠋ codex reviewing · 0s ")
    assert w.tick_seconds < 0.2
    w.handle_event(ReviewFinished("codex", Verdict("clean", elapsed=3)))
    assert "codex reviewed · no concerns · 3s" in out.text()
    assert last_separator(out) == "─" * 60 and w.tick_seconds == 1.0


def test_a_review_landing_mid_turn_waits_for_the_closing_row(env_factory):
    env = env_factory(); w, d, out, _ = make_nav_window(env)
    w.handle_event(TurnStarted("claude", "", "go"))
    w.handle_event(TextDelta("half a para"))
    w.handle_event(ReviewFinished("codex", Verdict("clean", elapsed=3)))
    assert "no concerns" not in out.text()
    w.handle_event(TurnFinished("completed", ""))
    text = out.text()
    assert "no concerns" in text and text.index("✓ done") < text.index("no concerns")


def test_a_review_landing_during_an_approval_waits_too(env_factory):
    env = env_factory(); w, d, out, _ = make_nav_window(env)
    w.handle_event(TurnStarted("claude", "", "go"))
    w.handle_event(ApprovalRequest("command", "rm x"))
    w.handle_event(ReviewFinished("codex", Verdict("clean", elapsed=3)))
    assert "no concerns" not in out.text()
    w.handle_input(b"n")
    w.handle_event(TurnFinished("interrupted", ""))
    assert "no concerns" in out.text()


class TestNoteCommand:
    def test_note_prints_the_pending_note_in_full(self, env_factory):
        env = env_factory(); w, d, out, _ = make_nav_window(env)
        w.navigator.note = spoken_note()
        assert w.handle_input(b"/note\r") is True and d.submitted == []
        assert "codex ⚑ block" in out.text() and "s.py:12 — w" in out.text()

    def test_note_without_one_says_so(self, env_factory):
        env = env_factory(); w, d, out, _ = make_nav_window(env)
        w.handle_input(b"/note\r")
        assert "no pending note" in out.text()

    @pytest.mark.parametrize("arg, feedback", [("dismiss", None), ("good", "good"), ("bad", "bad")])
    def test_dismiss_and_feedback(self, env_factory, arg, feedback):
        env = env_factory(); w, d, out, _ = make_nav_window(env)
        w.navigator.note = spoken_note()
        w.handle_input(f"/note {arg}\r".encode())
        assert w.navigator.dismissed == [feedback] and "note dropped" in out.text()

    def test_bad_argument_is_usage(self, env_factory):
        env = env_factory(); w, d, out, _ = make_nav_window(env)
        w.handle_input(b"/note maybe\r")
        assert "usage: /note [dismiss|good|bad]" in out.text() and d.submitted == []

    def test_note_with_the_navigator_off(self, env_factory):
        env = env_factory(); w, d, out, _ = make_window(env)
        w.handle_input(b"/note\r")
        assert "navigator is off" in out.text() and d.submitted == []

    def test_a_word_starting_with_note_is_the_harnesss(self, env_factory):
        env = env_factory(); w, d, out, _ = make_nav_window(env)
        assert w.handle_input(b"/notes\r") is True and d.submitted == ["/notes"]


def test_status_names_the_navigator(env_factory):
    env = env_factory()
    w, d, out, _ = make_nav_window(env, cfg=ChatConfig(navigator="codex", navigator_deliver="prompt"))
    w.handle_input(b"/status\r")
    assert "navigator codex · prompt" in out.text()
    w, d, out, _ = make_window(env)
    w.handle_input(b"/status\r")
    assert "navigator" not in out.text()


def test_run_chat_builds_a_navigator_only_for_a_participant(env_factory, monkeypatch):
    """Drive run_chat over a pty like test_run_chat_on_a_pty does, with the
    config naming codex; assert the dispatcher got a Navigator. Then name a
    harness that is not a participant and assert the dim note and no
    navigator."""
    import pty
    from tandem.chat import window as window_mod
    built = []
    real = window_mod.Navigator

    class Spy(real):
        def __init__(self, *a, **k):
            super().__init__(*a, **k); built.append(self)

    monkeypatch.setattr(window_mod, "Navigator", Spy)
    monkeypatch.setattr(window_mod, "make_reviewer", lambda h, cfg, store, **k: type("R", (), {
        "harness": h, "review": lambda *a, **k: None, "close": lambda self: None})())
    env = env_factory()
    master, slave = pty.openpty()
    os.write(master, b"/quit\r")
    code = run_chat(env.session, env.store, ChatConfig(navigator="codex"), stdin_fd=slave, out_fd=slave,
                    runtimes={"claude": EchoRuntime("claude"), "codex": EchoRuntime("codex")})
    assert code == 0 and len(built) == 1 and built[0].harness == "codex"
    built.clear()
    out2 = bytearray()
    master2, slave2 = pty.openpty()
    os.write(master2, b"/quit\r")
    env.session.participants.remove("codex")
    run_chat(env.session, env.store, ChatConfig(navigator="codex"), stdin_fd=slave2, out_fd=slave2,
             runtimes={"claude": EchoRuntime("claude")})
    assert built == []
    painted = os.read(master2, 65536).decode(errors="replace")
    assert "navigator codex is not a participant" in painted
```

Read `test_run_chat_on_a_pty` in the same file (line ~421) and `EchoRuntime` (line ~354) before writing the last test; copy its pty setup exactly (including how it reads the painted output back) and keep only the navigator assertions above.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_chat_window.py -q -k "navigator or note or review or status_names"`
Expected: FAIL with `AttributeError: 'Window' object has no attribute 'navigator'` and unknown `/note` reaching the dispatcher

- [ ] **Step 3: Wire the window**

In `src/tandem/chat/window.py`:

```python
from .events import (ApprovalRequest, Failure, Idle, LimitsUpdate, LiveEvent, QuestionRequest,
                     ReviewFinished, ReviewStarted, TextDelta, ThinkingDelta, ToolFinished,
                     ToolOutput, ToolStarted, TurnFinished, TurnStarted)
from .navigator import Navigator, NavigatorLog, log_path
from .reviewers import make_reviewer

WINDOW_COMMANDS = ("/quit", "/status", "/skip-permissions", "/note")
```

`Window.__init__` gains `navigator=None` at the end of its keyword list and sets:

```python
        self.navigator = navigator
        self._deferred: list[ReviewFinished] = []   # verdicts that landed mid-turn
```

`bar_line`:

```python
    def bar_line(self) -> str:
        default = self.dispatcher.default
        self.bar.active = default
        self.bar.others = [h for h in self.session.participants if h != default]
        marks = {h: "skip-perms" for h in self._skipping()}
        nav = self.navigator
        if nav is not None:
            word = nav.mark()
            if word:
                marks[nav.harness] = " · ".join(filter(None, [marks.get(nav.harness, ""), word]))
        self.bar.marks = marks
        meter = self.meters.get(default)
        usage = meter.state.get("text", "") if meter is not None else ""
        return self.bar.line(False, usage, self.usage_state.get("limits") or {})
```

`status_line`, before the `return`:

```python
        if self.navigator is not None:
            parts.append(f"navigator {self.navigator.harness} · {self.cfg.navigator_deliver}")
```

`tick_seconds`:

```python
        return _BUSY_TICK if self.activity.animating else 1.0
```

`handle_event`: add two branches before `elif isinstance(ev, TurnFinished)`, and extend that branch:

```python
        elif isinstance(ev, ReviewStarted):
            pass                                   # the activity line and the bar mark carry it
        elif isinstance(ev, ReviewFinished):
            if self.activity.active:
                self._deferred.append(ev)          # never split a streaming paragraph
            else:
                s.review(ev)
        elif isinstance(ev, TurnFinished):
            s.turn_finished(ev, self.activity.last_elapsed if was_active else None)
            for deferred in self._deferred:
                s.review(deferred)
            self._deferred.clear()
            if was_active and self.activity.last_elapsed >= _LONG_TURN_SECONDS:
                self._ring()
```

Add the command handler next to `set_skip_permissions`:

```python
    def note_command(self, arg: str) -> None:
        """`/note` shows the pending note in full; `dismiss` drops it; `good`
        and `bad` drop it and record whether it helped."""
        nav = self.navigator
        if nav is None:
            self.screen.note("navigator is off")
            return
        if arg == "":
            note = nav.pending()
            if note is None:
                self.screen.note("no pending note")
            else:
                self.screen.review(ReviewFinished(note.navigator, note.verdict))
            return
        if arg not in ("dismiss", "good", "bad"):
            self.screen.note("usage: /note [dismiss|good|bad]")
            return
        dropped = nav.dismiss(None if arg == "dismiss" else arg)
        self.screen.note("note dropped" if dropped else "no pending note")
```

In `handle_input`, after the `/skip-permissions` branch:

```python
                if command == "/note":
                    self.note_command(action.text.strip()[len(command):].strip())
                    continue
```

In `run_chat`, after `usage_state`/`poller` are built and before the `Dispatcher(...)` line:

```python
    navigator = None
    nav_note = ""
    if cfg.navigator:
        if cfg.navigator in session.participants:
            navigator = Navigator(
                cfg.navigator, cfg, make_reviewer(cfg.navigator, cfg, store), post,
                NavigatorLog(log_path(session.tandem_id)),
                headroom=lambda: headroom_ok(usage_state, cfg.navigator, cfg.navigator_headroom))
        else:
            nav_note = (f"navigator {cfg.navigator} is not a participant of this session "
                        f"({', '.join(session.participants)}); off")
```

Pass `navigator=navigator` to both `Dispatcher(...)` and `Window(...)`. After `screen.enter(fresh=True)`:

```python
        if nav_note:
            screen.note(nav_note)
```

`headroom_ok` is written in Task 12; for this task add a placeholder in `navigator.py` that Task 12 replaces:

```python
def headroom_ok(usage_state: dict, harness: str, floor: int) -> bool:
    return True
```

and import it in `window.py` from `.navigator`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_chat_window.py -q`
Expected: PASS

- [ ] **Step 5: Run the whole suite**

Run: `uv run pytest -q`
Expected: PASS, count ≥ 1222 + the tests added so far

- [ ] **Step 6: Commit**

```bash
git add src/tandem/chat/window.py src/tandem/chat/navigator.py tests/test_chat_window.py
git commit -m "Build the navigator in the chat window: bar mark, rows, /note"
```

---

### Task 12: Headroom from the rate-limit windows

**Files:**
- Modify: `src/tandem/ratelimit.py` (`_SharedState`, `RateLimitPoller.__init__`, `refresh`, `remember`)
- Modify: `src/tandem/chat/runtime/claude.py` (`rate_limit_event`), `src/tandem/chat/runtime/codex.py` (`account/rateLimits/updated`)
- Modify: `src/tandem/chat/window.py` (`LimitsUpdate` branch)
- Modify: `src/tandem/chat/navigator.py` (`headroom_ok`)
- Test: `tests/test_ratelimit.py` (find the poller tests there), `tests/test_chat_claude.py`, `tests/test_chat_codex.py`, `tests/test_chat_window.py`, `tests/test_chat_navigator.py`

**Interfaces:**
- Produces: `state["windows"]: dict[str, list[tuple[str, int]]]` published by the poller and by `LimitsUpdate.windows`; `remember(harness, text, windows=())`; `headroom_ok(usage_state, harness, floor) -> bool` — the first window listed is the shortest; absent data → True.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_chat_navigator.py`:

```python
from tandem.chat.navigator import headroom_ok


def test_headroom_reads_the_shortest_window():
    state = {"windows": {"codex": [("5h", 85), ("7d", 10)]}}
    assert headroom_ok(state, "codex", 20) is False           # 15 % left < 20
    assert headroom_ok(state, "codex", 15) is True
    assert headroom_ok(state, "codex", 0) is True
    assert headroom_ok({"windows": {"codex": [("5h", 79)]}}, "codex", 20) is True


def test_headroom_without_data_is_not_enforced():
    assert headroom_ok({}, "codex", 20) is True
    assert headroom_ok({"windows": {}}, "codex", 20) is True
    assert headroom_ok({"windows": {"codex": []}}, "codex", 20) is True
    assert headroom_ok({"limits": {"codex": "5h 99%"}}, "codex", 20) is True    # text alone is not data
```

Append to `tests/test_ratelimit.py` (read its existing poller test for how `fetchers` and `state` are set up and mirror it):

```python
def test_poller_publishes_parsed_windows_beside_the_text():
    from tandem.ratelimit import RateLimitPoller, Window
    state = {}
    p = RateLimitPoller(["codex"], state, fetchers={"codex": lambda: [Window("5h", 42), Window("7d", 7)]})
    p.refresh()
    assert state["limits"] == {"codex": "5h 42% 7d 7%"}
    assert state["windows"] == {"codex": [("5h", 42), ("7d", 7)]}


def test_a_remembered_figure_carries_its_windows():
    from tandem import ratelimit
    ratelimit.remember("claude", "5h 9%", (("5h", 9),))
    state = {}
    p = ratelimit.RateLimitPoller(["claude"], state, fetchers={"claude": lambda: None})
    assert state["windows"]["claude"] == [("5h", 9)]
```

Append to `tests/test_chat_claude.py` (next to `test_rate_limit_event_feeds_the_bar`, reuse its event fixture):

```python
def test_rate_limit_event_carries_windows():
    rt = ClaudeRuntime(ChatConfig())
    rec = Recorder()
    rt.handle_line({"type": "rate_limit_event", "rate_limit_info": {
        "status": "allowed", "rateLimitType": "five_hour", "unifiedWindows": {
            "five_hour": {"utilization": 0.25}, "seven_day": {"utilization": 0.5}}}},
        rec.emit, rec, lambda o: None)
    ev = [e for e in rec.events if isinstance(e, LimitsUpdate)][0]
    assert ev.windows == (("5h", 25), ("7d", 50))
```

Append to `tests/test_chat_codex.py` a mirror using the `account/rateLimits/updated` shape from `src/tandem/chat/runtime/codex.py:362-370`:

```python
def test_rate_limits_updated_carries_windows():
    rt = CodexRuntime(ChatConfig())
    rec = Recorder()
    rt.handle({"jsonrpc": "2.0", "method": "account/rateLimits/updated", "params": {"rateLimits": {
        "primary": {"usedPercent": 30, "windowDurationMins": 300},
        "secondary": {"usedPercent": 5, "windowDurationMins": 10080}}}}, lambda o: None, rec.emit, rec)
    ev = [e for e in rec.events if isinstance(e, LimitsUpdate)][0]
    assert ev.windows == (("5h", 30), ("7d", 5))
```

Append to `tests/test_chat_window.py`:

```python
def test_a_streamed_limit_publishes_its_windows(env_factory):
    env = env_factory(); w, d, out, _ = make_window(env)
    w.handle_event(LimitsUpdate("codex", "5h 30%", (("5h", 30),)))
    assert w.usage_state["windows"] == {"codex": [("5h", 30)]}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_chat_navigator.py tests/test_ratelimit.py tests/test_chat_claude.py tests/test_chat_codex.py tests/test_chat_window.py -q -k "headroom or windows"`
Expected: FAIL (`headroom_ok` always True; `state` has no `windows`; `LimitsUpdate.windows` empty)

- [ ] **Step 3: Publish windows everywhere**

`src/tandem/ratelimit.py`:

```python
class _SharedState:
    def __init__(self) -> None:
        self.not_before: dict[str, float] = {}
        self.last_refresh: float = float("-inf")
        self.text: dict[str, str] = {}
        self.windows: dict[str, list[tuple[str, int]]] = {}   # parsed, beside the text
        self.keychain_dead: bool = False


def _pairs(windows: list[Window] | None) -> list[tuple[str, int]]:
    return [(w.label, w.used_percent) for w in windows or []]


def remember(harness: str, text: str, windows: tuple[tuple[str, int], ...] = ()) -> None:
    _shared.text[harness] = text
    _shared.windows[harness] = list(windows)
```

In `RateLimitPoller.__init__`, after `self.state["limits"] = ...`:

```python
        self.state["windows"] = {h: list(_shared.windows.get(h, [])) for h in self.fetchers}
```

In `refresh`, keep a parallel dict:

```python
        out: dict[str, str] = {}
        wins: dict[str, list[tuple[str, int]]] = {}
        for h, fetch in self.fetchers.items():
            if self._halt.is_set():
                out[h] = _shared.text.get(h, ""); wins[h] = list(_shared.windows.get(h, []))
                continue
            if now < _shared.not_before.get(h, 0.0):
                out[h] = _shared.text.get(h, ""); wins[h] = list(_shared.windows.get(h, []))
                continue
            try:
                windows = fetch()
            except Throttled as exc:
                _shared.not_before[h] = now + exc.retry_after
                out[h] = _shared.text.get(h, ""); wins[h] = list(_shared.windows.get(h, []))
                continue
            except Exception:
                windows = None
            out[h] = format_windows(windows) if windows else ""
            wins[h] = _pairs(windows)
        _shared.text.update(out)
        _shared.windows.update(wins)
        self.state["limits"] = out
        self.state["windows"] = wins
```

`src/tandem/chat/runtime/claude.py`, the `rate_limit_event` branch:

```python
                emit(LimitsUpdate("claude", format_windows(windows),
                                  tuple((w.label, w.used_percent) for w in windows)))
```

`src/tandem/chat/runtime/codex.py`, the `account/rateLimits/updated` branch:

```python
            if windows:
                emit(LimitsUpdate("codex", format_windows(windows),
                                  tuple((w.label, w.used_percent) for w in windows)))
```

`src/tandem/chat/window.py`, the `LimitsUpdate` branch:

```python
        elif isinstance(ev, LimitsUpdate):
            limits = dict(self.usage_state.get("limits") or {})
            limits[ev.harness] = ev.text
            self.usage_state["limits"] = limits
            wins = dict(self.usage_state.get("windows") or {})
            wins[ev.harness] = list(ev.windows)
            self.usage_state["windows"] = wins
            remember(ev.harness, ev.text, ev.windows)     # else a throttled poller's next refresh blanks it
```

`src/tandem/chat/navigator.py`, replace the placeholder:

```python
def headroom_ok(usage_state: dict, harness: str, floor: int) -> bool:
    """The navigator's shortest rate-limit window (listed first by both
    endpoints) has at least `floor` percent left. No parsed data — polling
    off, API-key login, nothing fetched yet — means the floor is not
    enforced: the bar text alone is not data."""
    wins = (usage_state.get("windows") or {}).get(harness) or []
    if not wins:
        return True
    _, used = wins[0]
    return 100 - int(used) >= floor
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/tandem/ratelimit.py src/tandem/chat/runtime/claude.py src/tandem/chat/runtime/codex.py src/tandem/chat/window.py src/tandem/chat/navigator.py tests/test_ratelimit.py tests/test_chat_claude.py tests/test_chat_codex.py tests/test_chat_window.py tests/test_chat_navigator.py
git commit -m "Back the navigator off when its rate-limit window is nearly spent"
```

---

### Task 13: `tandem navigator log`

**Files:**
- Modify: `src/tandem/cli.py` (a `navigator` group after the `plugin` group)
- Modify: `docs/configuration.md` (one sentence pointing at the command, already referenced in Task 1's paragraph)
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `NavigatorLog.read / stats`, `log_path`, `_current_session`.
- Produces: `tandem navigator log [-n N] [--all]`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_cli.py`:

```python
def _nav_log(home, tandem_id, records):
    from tandem.chat.navigator import log_path
    p = log_path(tandem_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("".join(json.dumps(r) + "\n" for r in records))
    return p


NAV_RECORDS = [
    {"ts": "2026-09-25T14:01:00.000001+00:00", "kind": "review", "turn_harness": "claude",
     "prompt": "fix the loop", "gate": "review", "verdict": "speak", "severity": "block",
     "note": "The retry loop swallows ShadowBusy", "evidence": [], "elapsed": 21.3,
     "navigator": "codex", "model": "", "error": ""},
    {"ts": "2026-09-25T14:03:00.000001+00:00", "kind": "review", "turn_harness": "claude",
     "prompt": "explain", "gate": "skip:quiet", "verdict": "", "severity": "", "note": "",
     "evidence": [], "elapsed": 0, "navigator": "codex", "model": "", "error": ""},
    {"ts": "2026-09-25T14:05:00.000001+00:00", "kind": "review", "turn_harness": "opencode",
     "prompt": "add tests", "gate": "review", "verdict": "clean", "severity": "", "note": "",
     "evidence": [], "elapsed": 9.0, "navigator": "codex", "model": "", "error": ""},
    {"ts": "2026-09-25T14:05:30.000001+00:00", "kind": "feedback",
     "ref": "2026-09-25T14:01:00.000001+00:00", "value": "good"},
]


def test_navigator_log_prints_the_current_sessions_records_and_a_footer(homes, ok_versions, monkeypatch):
    with StateStore() as store:
        session = cli._pair_session(store, str(homes), "claude", ["claude", "codex"], seed=False)
    _nav_log(homes, session.tandem_id, NAV_RECORDS)
    r = click.testing.CliRunner().invoke(cli.main, ["navigator", "log"])
    assert r.exit_code == 0, r.output
    lines = r.output.rstrip("\n").split("\n")
    assert lines[0].startswith("14:01") and "claude → codex" in lines[0] and "speak block" in lines[0]
    assert "The retry loop swallows ShadowBusy" in lines[0] and lines[0].endswith("[good]")
    assert lines[1].startswith("14:03") and "skip:quiet" in lines[1]
    assert lines[2].startswith("14:05") and "clean" in lines[2]
    assert lines[-1] == "reviewed 2 · spoken 1 · skipped 1 · helpful 1/1 (100%)"


def test_navigator_log_limit_and_all(homes, ok_versions):
    with StateStore() as store:
        s1 = cli._pair_session(store, str(homes), "claude", ["claude", "codex"], seed=False)
    _nav_log(homes, s1.tandem_id, NAV_RECORDS)
    _nav_log(homes, "tdm-other", NAV_RECORDS[:1])
    r = click.testing.CliRunner().invoke(cli.main, ["navigator", "log", "-n", "1"])
    assert r.output.count("\n") == 2                       # one row + footer
    assert "14:05" in r.output                              # the newest
    r = click.testing.CliRunner().invoke(cli.main, ["navigator", "log", "--all"])
    assert r.output.count("14:01") == 2 and "tdm-other" in r.output


def test_navigator_log_without_a_session_or_records(homes, ok_versions):
    r = click.testing.CliRunner().invoke(cli.main, ["navigator", "log"])
    assert r.exit_code == 0 and "no navigator log" in r.output
```

`cli._pair_session(store, cwd, active, participants, *, seed=False)` is the real signature; it echoes what it paired, which the CliRunner does not capture.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_cli.py -q -k navigator_log`
Expected: FAIL with `Error: No such command 'navigator'`

- [ ] **Step 3: Add the command**

In `src/tandem/cli.py`, after the `plugin` group:

```python
@main.group()
def navigator() -> None:
    """The chat navigator's review log (see `[chat] navigator`)."""


def _nav_row(rec: dict, marks: dict[str, str], *, session_id: str | None = None) -> str:
    when = rec.get("ts", "")[11:16]
    who = rec.get("turn_harness", "?")
    if rec.get("gate", "") != "review":
        body = rec.get("gate", "")
    else:
        body = rec.get("verdict", "")
        if rec.get("severity"):
            body += f" {rec['severity']}"
        if rec.get("note"):
            body += f"  {rec['note'][:70]}"
    parts = [when, f"{who} → {rec.get('navigator', '?')}" if rec.get("gate") == "review" else who, body]
    if session_id:
        parts.insert(1, session_id)
    line = "  ".join(parts)
    mark = marks.get(rec.get("ts", ""))
    return f"{line}  [{mark}]" if mark else line


@navigator.command(name="log")
@click.option("-n", "limit", type=int, default=20, show_default=True, help="Rows to show, newest last")
@click.option("--all", "all_sessions", is_flag=True, help="Every session's log, not just this directory's")
def navigator_log(limit: int, all_sessions: bool) -> None:
    """Recent reviews and the helpful rate."""
    from .chat.navigator import NavigatorLog, log_path

    files: list[tuple[str | None, Path]] = []
    if all_sessions:
        root = paths.tandem_home() / "navigator"
        files = [(p.stem, p) for p in sorted(root.glob("*.jsonl"))] if root.is_dir() else []
    else:
        with StateStore() as store:
            session = _current_session(store, _cwd())
        if session is not None:
            files = [(None, log_path(session.tandem_id))]
    records: list[tuple[str | None, dict]] = []
    for sid, p in files:
        records += [(sid, r) for r in NavigatorLog.read(p)]
    if not records:
        click.echo("no navigator log for this session" if not all_sessions else "no navigator log")
        return
    marks = {r["ref"]: r["value"] for _, r in records
             if r.get("kind") == "feedback" and "ref" in r and "value" in r}
    reviews = [(sid, r) for sid, r in records if r.get("kind") == "review"]
    reviews.sort(key=lambda t: t[1].get("ts", ""))
    for sid, r in reviews[-limit:] if limit > 0 else reviews:
        click.echo(_nav_row(r, marks, session_id=sid))
    st = NavigatorLog.stats([r for _, r in records])
    helpful = ("n/a" if st["helpful"] is None
               else f"{st['good']}/{st['good'] + st['bad']} ({round(st['helpful'] * 100)}%)")
    click.echo(f"reviewed {st['reviewed']} · spoken {st['spoken']} · skipped {st['skipped']} · helpful {helpful}")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_cli.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/tandem/cli.py tests/test_cli.py
git commit -m "Add tandem navigator log: recent reviews and the helpful rate"
```

---

### Task 14: Live gate (manual, tmux)

**Files:** none changed. This task produces evidence, recorded in the PR description.

- [ ] **Step 1: Prepare a config**

In a scratch repo with a committed file (`/tmp/navgate`, `git init`, one `s.py` with a loop and a bare `except:` committed), set in `~/.tandem/config.toml`:

```toml
[chat]
navigator = "codex"
```

- [ ] **Step 2: Codex reviewing claude**

```bash
tmux new-session -d -s navgate -x 120 -y 40 -c /tmp/navgate 'tandem --new'
sleep 3
tmux send-keys -t navgate 'edit s.py so the bare except re-raises KeyboardInterrupt, then say done' Enter
```

Watch with `tmux capture-pane -p -t navgate` every few seconds. Expected, in order: the claude turn's closing row; `── ⠹ codex reviewing · Ns ──` on the separator; the bar slot `codex ○ reviewing`; then either `  codex reviewed · no concerns · Ns` or a `codex ⚑ …` block. Record which, and the row text.

- [ ] **Step 3: A planted bug**

```bash
tmux send-keys -t navgate 'in s.py add a function total(xs) that returns sum(xs[1:]) and call it the total; say it is done' Enter
```

Expected: a spoken note naming `s.py` and the off-by-one. If the note is clean, record the verdict from `~/.tandem/navigator/<id>.jsonl` and the prompt sent (the log has the turn), and adjust nothing yet — that is a precision data point.

- [ ] **Step 4: Argue with it**

```bash
tmux send-keys -t navgate '/codex why do you think that is wrong?' Enter
```

Expected: the prompt row shows `you → codex  why do you think that is wrong?` and under it `  + navigator note: …`; codex's answer refers to the note. Check the trailer landed in codex's shadow:

```bash
grep -l "tandem navigator" ~/.codex/sessions/**/*.jsonl | head
```

- [ ] **Step 5: Claude reviewing codex**

Set `navigator = "claude"`, open a new session with `tandem --new --on codex`, run one editing prompt. Expected: receipt or note row; then verify the shadow was untouched and the fork is gone:

```bash
ls -la ~/.claude/projects/-tmp-navgate/        # exactly one transcript for this session
tail -1 ~/.tandem/navigator/<id>.jsonl          # verdict recorded
```

If the shadow transcript's size changed across the review or a second transcript remains, record it as a finding: the claude reviewer must be marked degraded in code (spec § Reviewers) before merge.

- [ ] **Step 6: Quit mid-review**

Start an editing prompt, and the moment `reviewing` appears, `tmux send-keys -t navgate '/quit' Enter`. Expected: the window exits within a few seconds, `ps aux | grep -c "codex app-server"` shows none from this session, and no `tandem-sub` rollout is left under `~/.codex/sessions` newer than the session's own.

- [ ] **Step 7: Record**

Paste the captured rows and the log lines into the PR description under "Live gate", with the claude/codex versions from `claude --version` and `codex --version`.

---

## Self-review notes

- Spec coverage: config (T1); events and paths (T2); facts and gate (T3); prompt, schema, parser, diff (T4); log (T5); worker, queue, dedupe, interval, three-strike, take/dismiss/mark (T6); codex fork reviewer (T7); claude fork reviewer with shadow lock release at init (T8); dispatcher hook, trailer, lock, close (T9); activity row and rows (T10); bar mark, deferred rows, `/note`, `/status`, construction, not-a-participant note (T11); headroom (T12); CLI (T13); live gate incl. shadow-size check and quit-mid-review (T14). The spec's "claude reviewer marked degraded when the shadow changed" is a live-gate finding rather than code in v1; if T14 step 5 shows a change, add a size check around `ClaudeReviewer.review` that raises `ReviewError("shadow changed during review")` and disables via the three-strike path.
- Type consistency: `Navigator.turn_ended(facts, session)` everywhere; `Reviewer.review(session, model, prompt, schema, shadow_lock) -> ReviewResult`; `Note.summary` / `Note.trailer()`; `Verdict.verdict` ∈ {clean, speak, empty, dup, error, off}; `ToolStarted.paths`; `TurnStarted.carried`; `LimitsUpdate.windows` as `(label, used_percent)` tuples; `usage_state["windows"]` lists of those tuples.
- Review Focus mapping: non-repo diff → T4 `test_diff_outside_a_repo_is_empty_not_an_error`; fenced JSON → T4 `test_text_json_is_parsed_even_inside_fences_or_prose`; verdict during approval → T11 `test_a_review_landing_during_an_approval_waits_too`; quit during review → T6 `test_close_reaches_the_reviewer_and_stops_new_work`, T9 `test_close_reaches_the_navigator`, T14 step 6; bare route keeps the note → T9 `test_a_bare_route_consumes_no_note`.
