# Unified Chat Window Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `tandem chat` — one composer and one conversation view over the existing paired session, where every prompt runs headless inside the harness that ran the last one unless it starts with `/claude`, `/codex`, or `/opencode`.

**Architecture:** Four units on top of the unchanged sync engine: a prefix grammar with a sticky default (`sessions.active`), one hand-rolled protocol client per harness (claude `-p` stream-json, codex app-server JSON-RPC over stdio, opencode `serve` HTTP + SSE) that all emit one small live-event vocabulary, a dispatcher that is the one-off run's bookkeeping with a streaming runner, and a raw-ANSI renderer (scroll region above a status bar and composer). Codex protocol models are generated from the schema the installed binary dumps.

**Tech Stack:** Python 3.11+, stdlib (`subprocess`, `select`, `termios`, `http.client`, `sqlite3`), pydantic v2 (already a dependency), pytest. `datamodel-code-generator` as a dev-only tool for the codex models.

**Spec:** `docs/specs/2026-09-08-unified-chat-window-design.md` — read it first; this plan argues from it.

## Global Constraints

- Runtime dependencies stay exactly `click>=8.1`, `pydantic>=2.7`, `watchdog>=4.0`, `pexpect>=4.9`. The only addition is `datamodel-code-generator>=0.26` under `[project.optional-dependencies] dev`. CI installs with `uv sync --locked`, so the commit that touches `pyproject.toml` must also run `uv lock` and commit `uv.lock`.
- Route grammar: `/(claude|codex|opencode)(:[A-Za-z0-9._/-]+)?` at the start of the prompt, followed by whitespace or end of input. `@path` is never interpreted. Any other leading `/word` passes through to the current harness unchanged.
- Every child process gets the user's environment minus every variable starting with `CLAUDE`, and is spawned with `start_new_session=True` so the kill ladder can signal its whole group.
- One turn in flight at a time across all harnesses. Every turn resumes its native session at dispatch time; no resident claude or codex process ever exists between turns. One `opencode serve` lives for the life of the window.
- Every glyph on the status bar row is one terminal cell wide (`frame.StatusBar.line`'s rule).
- Tested versions recorded in `compat.py`: claude `2.1.265`, codex `0.153.4`, opencode `1.18.20`.
- Tests are hermetic: no real harness binaries. Fake CLIs live under `tests/fakes/` and are launched with `sys.executable`.
- Commit messages end with the two trailer lines `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>` and `Claude-Session: https://claude.ai/code/session_01WZ7WV9iKDBigenNSsJaRdy`.
- Work on branch `unified-chat-window` (the spec's branch). `main` is PR-only.

---

## File Structure

New package `src/tandem/chat/`; one responsibility per file.

| File | Responsibility |
|---|---|
| `src/tandem/promptroute.py` | The `/harness[:model]` grammar and the slash pass-through rule. Pure functions. |
| `src/tandem/chat/__init__.py` | Package marker. |
| `src/tandem/chat/events.py` | The live-event vocabulary, `Answers` protocol, `TurnOutcome`. |
| `src/tandem/chat/runtime/__init__.py` | `RuntimeClient` protocol, `child_env`, `terminate`, `summarize_args`, `first_line`. |
| `src/tandem/chat/runtime/codex_protocol.py` | GENERATED pydantic models (do not edit by hand). |
| `src/tandem/chat/runtime/claude.py` | `ClaudeRuntime`: `claude -p` over stream-json. |
| `src/tandem/chat/runtime/codex.py` | `CodexRuntime`: `codex app-server` JSON-RPC over stdio. |
| `src/tandem/chat/runtime/opencode.py` | `OpencodeRuntime`: `opencode serve` HTTP + SSE, server lifecycle. |
| `src/tandem/chat/runtime/factory.py` | `make_runtimes(session, cfg)`. |
| `src/tandem/chat/dispatch.py` | `Dispatcher`: queue, pre-turn sync, worker thread, post-turn sync. |
| `src/tandem/chat/render.py` | `Screen`: scroll region, tool rows, prompts, history, bottom block. |
| `src/tandem/chat/composer.py` | `Composer`: line editor, key parsing, approval/question modes. |
| `src/tandem/chat/window.py` | `Window` + `run_chat`: raw mode, select loop, wiring, bar. |
| `src/tandem/ops.py` | Extract `prepare_turn` / `sync_after_turn` from `run_oneoff`. |
| `src/tandem/state.py` | `chat_pins` table + `get_pin` / `set_pin`. |
| `src/tandem/config.py` | `ChatConfig` + `load_chat_config`. |
| `src/tandem/frame.py` | `StatusBar` gains a `hint` parameter. |
| `src/tandem/cli.py` | `tandem chat [--on harness]`. |
| `src/tandem/compat.py`, `src/tandem/doctor.py` | Tested-version bumps; a doctor line for the generated codex models. |
| `tools/gen_codex_protocol.py` | The generator. |
| `tests/fakes/fake_claude.py`, `fake_codex_appserver.py` | Scripted stand-ins launched as subprocesses. |
| `tests/fakes/fake_opencode_server.py` | In-process `http.server` stand-in. |
| `tests/golden/chat/*.jsonl` | Live-captured protocol lines (2026-09-13). |
| `tests/test_promptroute.py`, `test_chat_*.py`, `test_codex_protocol.py` | Task tests. |
| `tools/live_gate_chat.py` | tmux-driven live gate. |
| `docs/configuration.md`, `docs/development.md`, `docs/formats.md`, `README.md` | Docs. |

---

### Task 1: Prefix grammar (`promptroute.py`)

**Files:**
- Create: `src/tandem/promptroute.py`
- Test: `tests/test_promptroute.py`

**Interfaces:**
- Produces: `Route(harness: str, model: str | None)` — `model=None` keeps the harness's pin, `model=""` clears it; `parse_route(prompt, participants) -> tuple[Route, str] | None` (route + body, or `None` = not a route); `RouteError(ValueError)` for a route tandem cannot honor; `is_passthrough_command(prompt) -> bool`.
- Consumes: `modelcat.resolve(name, models)` and `modelcat.load_catalog()` (existing).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_promptroute.py
"""The chat window's routing grammar: what routes, what passes through."""
import pytest

from tandem import promptroute
from tandem.promptroute import Route, RouteError, is_passthrough_command, parse_route

PARTS = ["claude", "codex", "opencode"]


def test_harness_route_with_body():
    assert parse_route("/codex fix the flaky test", PARTS) == (Route("codex", None), "fix the flaky test")


def test_bare_route_has_empty_body():
    assert parse_route("/codex", PARTS) == (Route("codex", None), "")
    assert parse_route("/codex   ", PARTS) == (Route("codex", None), "")


def test_route_only_at_start():
    assert parse_route("please ask /codex to fix it", PARTS) is None


def test_path_after_harness_name_is_not_a_route():
    assert parse_route("/codex/README.md explain this", PARTS) is None
    assert parse_route("/claude:haiku/x go", PARTS) is None


def test_file_mentions_are_never_interpreted():
    assert parse_route("@src/foo.py explain this", PARTS) is None
    assert parse_route("@CLAUDE.md summarize", PARTS) is None


def test_other_slash_commands_pass_through():
    assert parse_route("/compact", PARTS) is None
    assert parse_route("/superpowers:brainstorming go", PARTS) is None
    assert is_passthrough_command("/compact") is True
    assert is_passthrough_command("/codex go") is True   # the caller checks parse_route first
    assert is_passthrough_command("hello") is False


def test_non_participant_harness_is_an_error():
    with pytest.raises(RouteError):
        parse_route("/codex do it", ["claude", "opencode"])


def test_claude_model_passes_through():
    assert parse_route("/claude:haiku summarize", PARTS) == (Route("claude", "haiku"), "summarize")


def test_opencode_provider_model():
    assert parse_route("/opencode:openai/gpt-5.4-mini go", PARTS) == (
        Route("opencode", "openai/gpt-5.4-mini"), "go")


def test_default_clears_pin():
    assert parse_route("/codex:default go", PARTS) == (Route("codex", ""), "go")


def test_codex_model_resolves_via_catalog(monkeypatch):
    monkeypatch.setattr(promptroute.modelcat, "load_catalog",
                        lambda: [{"slug": "gpt-5.5", "visibility": "show"}])
    assert parse_route("/codex:5.5 go", PARTS) == (Route("codex", "gpt-5.5"), "go")


def test_codex_unknown_model_is_an_error(monkeypatch):
    monkeypatch.setattr(promptroute.modelcat, "load_catalog",
                        lambda: [{"slug": "gpt-5.5", "visibility": "show"}])
    with pytest.raises(RouteError):
        parse_route("/codex:no-such-model go", PARTS)


def test_codex_model_verbatim_without_catalog(monkeypatch):
    monkeypatch.setattr(promptroute.modelcat, "load_catalog", lambda: None)
    assert parse_route("/codex:gpt-5.5 go", PARTS) == (Route("codex", "gpt-5.5"), "go")


def test_newline_after_route_still_routes():
    assert parse_route("/codex\nfix it", PARTS) == (Route("codex", None), "fix it")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_promptroute.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'tandem.promptroute'`

- [ ] **Step 3: Write the module**

```python
# src/tandem/promptroute.py
"""The chat window's routing grammar: a leading `/harness[:model]`.

`/` is the command sigil in all three TUIs and `@` is their file-mention
sigil (headless claude still expands `@file`), so routes are spelled with
`/` and `@` is never touched. Everything that is not one of the three
harness names at the very start of the prompt is not tandem's: it goes to
the current harness verbatim, slash commands included.

Returning None means "not a route". Raising RouteError means the user
clearly wrote a route that tandem cannot honor (a non-participant, or a
codex model the catalog rejects) — the composer shows the message and
keeps the prompt for editing rather than sending it anywhere.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from . import modelcat

HARNESSES = ("claude", "codex", "opencode")

# The name must end the token: `/codex/README.md` and `/claude:haiku/x` are
# paths, not routes, because a slash follows where whitespace or the end of
# input must be.
_ROUTE_RE = re.compile(r"/(claude|codex|opencode)(?::([A-Za-z0-9._/-]+))?(?=\s|$)")


@dataclass(frozen=True)
class Route:
    harness: str
    model: str | None = None   # None = keep the harness's pin; "" = clear it


class RouteError(ValueError):
    """A route the user meant, that tandem cannot honor."""


def parse_route(prompt: str, participants) -> tuple[Route, str] | None:
    """(route, body) for a prompt that starts with a harness route; None
    when the prompt is ordinary text or someone else's slash command."""
    text = prompt.lstrip()
    m = _ROUTE_RE.match(text)
    if m is None:
        return None
    harness, model = m.group(1), m.group(2)
    if harness not in participants:
        raise RouteError(
            f"{harness} is not a participant in this session "
            f"(participants: {', '.join(participants)})")
    body = text[m.end():].strip()
    if model is None:
        return Route(harness), body
    if model == "default":
        return Route(harness, ""), body
    if harness == "codex":
        try:
            model = modelcat.resolve(model, modelcat.load_catalog())
        except modelcat.UnknownModel as exc:
            raise RouteError(str(exc)) from exc
    return Route(harness, model), body


def is_passthrough_command(prompt: str) -> bool:
    """A leading slash that is not a route belongs to the harness."""
    return prompt.lstrip().startswith("/")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_promptroute.py -q`
Expected: 14 passed

- [ ] **Step 5: Commit**

```bash
git add src/tandem/promptroute.py tests/test_promptroute.py
git commit -m "feat(chat): /harness[:model] route grammar with slash pass-through" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>" -m "Claude-Session: https://claude.ai/code/session_01WZ7WV9iKDBigenNSsJaRdy"
```

---

### Task 2: Model pins in the state store and `[chat]` config

**Files:**
- Modify: `src/tandem/state.py` (the `_SCHEMA` string and a new section after the sync-cursor methods)
- Modify: `src/tandem/config.py` (append)
- Test: `tests/test_state.py` (append), `tests/test_config.py` (append)

**Interfaces:**
- Produces: `StateStore.get_pin(tandem_id, harness) -> str` (`""` when none), `StateStore.set_pin(tandem_id, harness, model)` (`""` deletes); `ChatConfig` dataclass with fields `tool_output_lines: int = 8`, `history_turns: int = 50`, `show_thinking: bool = False`, `claude_setting_sources: tuple[str, ...] = ("user", "project", "local")`, `codex_approval_policy: str = ""`, `codex_sandbox: str = ""`; `load_chat_config() -> ChatConfig`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_state.py`:

```python
def test_chat_pins_round_trip(tmp_path):
    from tandem.state import StateStore

    store = StateStore(db_path=tmp_path / "state.db")
    s = store.create_session(str(tmp_path), "claude", ["claude", "codex"],
                             {"claude": "c", "codex": "x"})
    assert store.get_pin(s.tandem_id, "codex") == ""
    store.set_pin(s.tandem_id, "codex", "gpt-5.5")
    assert store.get_pin(s.tandem_id, "codex") == "gpt-5.5"
    store.set_pin(s.tandem_id, "codex", "gpt-5.4")          # overwrite
    assert store.get_pin(s.tandem_id, "codex") == "gpt-5.4"
    assert store.get_pin(s.tandem_id, "claude") == ""       # per harness
    store.set_pin(s.tandem_id, "codex", "")                  # clear
    assert store.get_pin(s.tandem_id, "codex") == ""
    store.close()


def test_chat_pins_table_added_to_existing_db(tmp_path):
    """An older state.db without chat_pins is extended in place, not moved aside."""
    import sqlite3

    from tandem.state import StateStore

    db = tmp_path / "state.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE sessions (tandem_id TEXT PRIMARY KEY, cwd TEXT NOT NULL,"
        " active TEXT NOT NULL, participants TEXT NOT NULL,"
        " native_session_ids TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL,"
        " last_sync_at TEXT, last_used_at TEXT);"
    )
    conn.execute("INSERT INTO sessions VALUES ('abc', '/p', 'claude', '[\"claude\"]', '{}', 'now', NULL, NULL)")
    conn.commit(); conn.close()
    store = StateStore(db_path=db)
    assert store.get_session("abc") is not None           # not moved aside
    store.set_pin("abc", "claude", "haiku")
    assert store.get_pin("abc", "claude") == "haiku"
    store.close()
```

Append to `tests/test_config.py`:

```python
def test_chat_config_defaults_when_absent(tmp_path, monkeypatch):
    from tandem.config import ChatConfig, load_chat_config

    monkeypatch.setenv("TANDEM_HOME", str(tmp_path))
    assert load_chat_config() == ChatConfig()


def test_chat_config_reads_and_validates(tmp_path, monkeypatch):
    from tandem.config import load_chat_config

    monkeypatch.setenv("TANDEM_HOME", str(tmp_path))
    (tmp_path / "config.toml").write_text(
        '[chat]\ntool_output_lines = 3\nhistory_turns = "lots"\nshow_thinking = true\n'
        'claude_setting_sources = ["user", "bogus"]\ncodex_approval_policy = "never"\n'
        'codex_sandbox = 7\n'
    )
    cfg = load_chat_config()
    assert cfg.tool_output_lines == 3
    assert cfg.history_turns == 50            # wrong type -> default
    assert cfg.show_thinking is True
    assert cfg.claude_setting_sources == ("user",)   # unknown names dropped
    assert cfg.codex_approval_policy == "never"
    assert cfg.codex_sandbox == ""            # wrong type -> default
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_state.py tests/test_config.py -q -k "chat"`
Expected: FAIL with `AttributeError: 'StateStore' object has no attribute 'get_pin'` and `ImportError: cannot import name 'ChatConfig'`

- [ ] **Step 3: Extend the schema and the store**

In `src/tandem/state.py`, append to `_SCHEMA` (inside the triple-quoted string, after the `sync_cursors` table):

```python
CREATE TABLE IF NOT EXISTS chat_pins (
    tandem_id TEXT NOT NULL,
    harness TEXT NOT NULL,
    model TEXT NOT NULL,
    PRIMARY KEY (tandem_id, harness)
);
```

`_SCHEMA` runs with `CREATE TABLE IF NOT EXISTS` on every open, so an older DB gains the table without a move-aside (`_schema_stale` only keys on the `participants` column). Add after `save_cursor`:

```python
    # -- chat model pins -----------------------------------------------------

    def get_pin(self, tandem_id: str, harness: str) -> str:
        """The model pinned for `harness` in the chat window, "" for none."""
        row = self._conn.execute(
            "SELECT model FROM chat_pins WHERE tandem_id = ? AND harness = ?",
            (tandem_id, harness),
        ).fetchone()
        return row["model"] if row else ""

    def set_pin(self, tandem_id: str, harness: str, model: str) -> None:
        """Pin `model` for `harness`; an empty model clears the pin."""
        with self._conn:
            if model:
                self._conn.execute(
                    "INSERT INTO chat_pins (tandem_id, harness, model) VALUES (?, ?, ?)"
                    " ON CONFLICT (tandem_id, harness) DO UPDATE SET model = excluded.model",
                    (tandem_id, harness, model),
                )
            else:
                self._conn.execute(
                    "DELETE FROM chat_pins WHERE tandem_id = ? AND harness = ?",
                    (tandem_id, harness),
                )
```

- [ ] **Step 4: Add the config section**

Append to `src/tandem/config.py`:

```python
_SETTING_SOURCES = ("user", "project", "local")


@dataclass(frozen=True)
class ChatConfig:
    """[chat]: the unified window. Every field forgiving, like the rest."""
    tool_output_lines: int = 8          # tail printed per tool call
    history_turns: int = 50             # turns painted at startup
    show_thinking: bool = False
    claude_setting_sources: tuple[str, ...] = _SETTING_SOURCES
    codex_approval_policy: str = ""     # "" = inherit ~/.codex/config.toml
    codex_sandbox: str = ""             # "" = inherit


def load_chat_config() -> ChatConfig:
    raw = _read_config().get("chat")
    if not isinstance(raw, dict):
        return ChatConfig()
    d = ChatConfig()

    def pick(key: str, kind: type, default):
        v = raw.get(key, default)
        # bool is an int subclass: a `true` must not pass as an int
        if not isinstance(v, kind) or (kind is int and isinstance(v, bool)):
            return default
        return v

    sources = raw.get("claude_setting_sources")
    if isinstance(sources, list):
        kept = tuple(s for s in sources if isinstance(s, str) and s in _SETTING_SOURCES)
        sources = kept or d.claude_setting_sources
    else:
        sources = d.claude_setting_sources
    return ChatConfig(
        tool_output_lines=max(0, pick("tool_output_lines", int, d.tool_output_lines)),
        history_turns=max(0, pick("history_turns", int, d.history_turns)),
        show_thinking=pick("show_thinking", bool, d.show_thinking),
        claude_setting_sources=sources,
        codex_approval_policy=pick("codex_approval_policy", str, d.codex_approval_policy),
        codex_sandbox=pick("codex_sandbox", str, d.codex_sandbox),
    )
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/test_state.py tests/test_config.py -q`
Expected: all pass (the two new state tests and two new config tests included)

- [ ] **Step 6: Commit**

```bash
git add src/tandem/state.py src/tandem/config.py tests/test_state.py tests/test_config.py
git commit -m "feat(chat): chat_pins table and [chat] config" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>" -m "Claude-Session: https://claude.ai/code/session_01WZ7WV9iKDBigenNSsJaRdy"
```

---
### Task 3: Live-event vocabulary and runtime base

**Files:**
- Create: `src/tandem/chat/__init__.py`, `src/tandem/chat/events.py`, `src/tandem/chat/runtime/__init__.py`
- Test: `tests/test_chat_runtime_base.py`

**Interfaces:**
- Produces (`chat/events.py`): frozen dataclasses `TextDelta(text)`, `ThinkingDelta(text)`, `ToolStarted(call_id, tool, summary)`, `ToolOutput(call_id, text)`, `ToolFinished(call_id, ok, summary="")`, `ApprovalRequest(kind, detail, choices=("allow","always","deny"))`, `QuestionRequest(prompt, options=())`, `TurnStarted(harness, model, prompt)`, `TurnFinished(status, usage="")`, `Failure(message)`, `LimitsUpdate(harness, text)`, `Idle()`; `LiveEvent` union; `Answers` protocol with `approve(req) -> str` and `answer(req) -> str`; `TurnOutcome(status, error="", native_id=None)`.
- Produces (`chat/runtime/__init__.py`): `RuntimeClient` protocol (`harness: str`, `run_turn(session, native_id, prompt, model, emit, answers) -> TurnOutcome`, `interrupt()`, `close()`), `child_env(base=None) -> dict`, `terminate(proc, *, soft=None, soft_timeout=5.0, term_timeout=2.0) -> str`, `first_line(text, limit=80) -> str`, `summarize_args(tool, args) -> str`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_chat_runtime_base.py
import subprocess
import sys

from tandem.chat.events import ApprovalRequest, TurnOutcome
from tandem.chat.runtime import child_env, first_line, summarize_args, terminate


def test_child_env_strips_claude_markers():
    env = child_env({"CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "cli", "PATH": "/bin", "HOME": "/h"})
    assert env == {"PATH": "/bin", "HOME": "/h"}


def test_first_line_truncates_with_ellipsis():
    assert first_line("  hello\nworld ") == "hello"
    assert first_line("x" * 100, limit=10) == "x" * 9 + "…"
    assert first_line("") == ""


def test_summarize_args_prefers_the_meaningful_key():
    assert summarize_args("Bash", {"command": "pytest -q", "description": "run tests"}) == "pytest -q"
    assert summarize_args("Edit", {"file_path": "/a/b.py", "old_string": "x"}) == "/a/b.py"
    assert summarize_args("exec", "touch x") == "touch x"
    assert summarize_args("mystery", {"k": 1}) == '{"k": 1}'
    assert summarize_args("none", {}) == ""


def test_terminate_soft_then_term_then_kill():
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
    assert terminate(proc, soft_timeout=0.2, term_timeout=1.0) in ("term", "kill")
    assert proc.poll() is not None
    dead = subprocess.Popen([sys.executable, "-c", "pass"]); dead.wait()
    assert terminate(dead) == "dead"


def test_terminate_honors_a_soft_hook():
    proc = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.readline()"],
                            stdin=subprocess.PIPE, start_new_session=True)
    assert terminate(proc, soft=lambda: proc.stdin.close(), soft_timeout=3.0) == "soft"


def test_defaults():
    assert ApprovalRequest("command", "rm -rf x").choices == ("allow", "always", "deny")
    assert TurnOutcome("completed").native_id is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_chat_runtime_base.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'tandem.chat'`

- [ ] **Step 3: Write the events module**

`src/tandem/chat/__init__.py`:

```python
"""The unified chat window: one composer, every harness headless."""
```

`src/tandem/chat/events.py`:

```python
"""What a running turn tells the window, in one vocabulary for all three
harnesses. Deliberately narrower than events.NormalizedEvent: these are
paint instructions and prompts, not transcript content — the transcript
is the harness's own file, which sync reads afterwards.

Runtime clients emit these from their worker thread; the window drains
them on the main thread. ApprovalRequest and QuestionRequest are the two
that block: the client calls Answers and waits."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Union


@dataclass(frozen=True)
class TextDelta:
    text: str


@dataclass(frozen=True)
class ThinkingDelta:
    text: str


@dataclass(frozen=True)
class ToolStarted:
    call_id: str
    tool: str
    summary: str        # one line, the renderer prints it after the tool name


@dataclass(frozen=True)
class ToolOutput:
    call_id: str
    text: str


@dataclass(frozen=True)
class ToolFinished:
    call_id: str
    ok: bool
    summary: str = ""


@dataclass(frozen=True)
class ApprovalRequest:
    kind: str           # "command" | "file_change" | "permission"
    detail: str
    choices: tuple[str, ...] = ("allow", "always", "deny")


@dataclass(frozen=True)
class QuestionRequest:
    prompt: str
    options: tuple[str, ...] = ()   # empty = free text


@dataclass(frozen=True)
class TurnStarted:
    harness: str
    model: str
    prompt: str


@dataclass(frozen=True)
class TurnFinished:
    status: str         # "completed" | "interrupted" | "failed"
    usage: str = ""     # dim trailer line, "" for none


@dataclass(frozen=True)
class Failure:
    message: str


@dataclass(frozen=True)
class LimitsUpdate:
    harness: str
    text: str           # bar-ready, e.g. "5h 4% 7d 41%"


@dataclass(frozen=True)
class Idle:
    """The dispatcher finished its post-turn work; the window may pump."""


LiveEvent = Union[TextDelta, ThinkingDelta, ToolStarted, ToolOutput, ToolFinished,
                  ApprovalRequest, QuestionRequest, TurnStarted, TurnFinished,
                  Failure, LimitsUpdate, Idle]

STATUSES = ("completed", "interrupted", "failed")


class Answers(Protocol):
    def approve(self, req: ApprovalRequest) -> str: ...   # one of req.choices
    def answer(self, req: QuestionRequest) -> str: ...


@dataclass
class TurnOutcome:
    status: str
    error: str = ""
    native_id: str | None = None   # a thread id minted during this turn (fresh codex)
```

- [ ] **Step 4: Write the runtime base**

`src/tandem/chat/runtime/__init__.py`:

```python
"""One client per harness, one calling convention. Each client owns its
process(es) and protocol; the dispatcher only ever calls run_turn,
interrupt and close. Shared here: the child environment rule, the kill
ladder for plain subprocesses, and the one-line summaries the renderer
prints for tool calls."""

from __future__ import annotations

import json
import os
import signal
import subprocess
from typing import Callable, Protocol

from ..events import Answers, LiveEvent, TurnOutcome


class RuntimeClient(Protocol):
    harness: str

    def run_turn(self, session, native_id: str | None, prompt: str, model: str,
                 emit: Callable[[LiveEvent], None], answers: Answers) -> TurnOutcome: ...

    def interrupt(self) -> None: ...

    def close(self) -> None: ...


def child_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """The user's environment minus every CLAUDE* marker: an inherited one
    changes how claude behaves (the frame's pty probes found it swallowing
    quit keys), and codex/opencode never need them."""
    env = dict(os.environ if base is None else base)
    for key in list(env):
        if key.startswith("CLAUDE"):
            del env[key]
    return env


def _wait(proc: subprocess.Popen, timeout: float) -> bool:
    try:
        proc.wait(timeout=timeout)
        return True
    except subprocess.TimeoutExpired:
        return False


def _signal_group(proc: subprocess.Popen, sig: int) -> None:
    # every runtime spawns with start_new_session=True, so the child's pid
    # is its group id and its tool children go down with it
    try:
        os.killpg(proc.pid, sig)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.send_signal(sig)
        except OSError:
            pass


def terminate(proc: subprocess.Popen, *, soft: Callable[[], None] | None = None,
              soft_timeout: float = 5.0, term_timeout: float = 2.0) -> str:
    """The ladder: `soft` (close stdin, send a quit request), SIGTERM to the
    group, SIGKILL. Returns the rung that worked: dead|soft|term|kill."""
    if proc.poll() is not None:
        return "dead"
    if soft is not None:
        try:
            soft()
        except Exception:
            pass
        if _wait(proc, soft_timeout):
            return "soft"
    _signal_group(proc, signal.SIGTERM)
    if _wait(proc, term_timeout):
        return "term"
    _signal_group(proc, signal.SIGKILL)
    _wait(proc, 1.0)
    return "kill"


def first_line(text: str, limit: int = 80) -> str:
    stripped = (text or "").strip()
    if not stripped:
        return ""
    line = stripped.splitlines()[0]
    return line if len(line) <= limit else line[: limit - 1] + "…"


_SUMMARY_KEYS = ("command", "cmd", "file_path", "path", "pattern", "query",
                 "url", "prompt", "description")


def summarize_args(tool: str, args) -> str:
    """One line for a tool row: the argument a human would look for first."""
    if isinstance(args, str):
        return first_line(args)
    if isinstance(args, dict):
        for key in _SUMMARY_KEYS:
            v = args.get(key)
            if isinstance(v, str) and v.strip():
                return first_line(v)
        if args:
            return first_line(json.dumps(args, ensure_ascii=False))
    return ""
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/test_chat_runtime_base.py -q`
Expected: 6 passed

- [ ] **Step 6: Commit**

```bash
git add src/tandem/chat tests/test_chat_runtime_base.py
git commit -m "feat(chat): live-event vocabulary and runtime base helpers" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>" -m "Claude-Session: https://claude.ai/code/session_01WZ7WV9iKDBigenNSsJaRdy"
```

---

### Task 4: Generated codex protocol models

**Files:**
- Create: `tools/gen_codex_protocol.py`, `src/tandem/chat/runtime/codex_protocol.py` (generated), `tests/golden/chat/codex_appserver.jsonl`, `tests/test_codex_protocol.py`
- Modify: `pyproject.toml` (dev extra), `uv.lock` (via `uv lock`), `src/tandem/compat.py` (codex `tested` stays `0.153.4`; the header pin test reads it)

**Interfaces:**
- Produces: module `tandem.chat.runtime.codex_protocol` with pydantic classes named after the schema definitions: `InitializeParams`, `ClientInfo`, `ThreadStartParams`, `ThreadStartResponse`, `ThreadResumeParams`, `ThreadResumeResponse`, `TurnStartParams`, `TurnStartResponse`, `TurnInterruptParams`, `Turn`, `ItemStartedNotification`, `ItemCompletedNotification`, `AgentMessageDeltaNotification`, `CommandExecutionOutputDeltaNotification`, `FileChangeOutputDeltaNotification`, `ReasoningSummaryTextDeltaNotification`, `TurnStartedNotification`, `TurnCompletedNotification`, `ThreadTokenUsageUpdatedNotification`, `AccountRateLimitsUpdatedNotification`, `ErrorNotification`, `CommandExecutionRequestApprovalParams`, `CommandExecutionRequestApprovalResponse`, `FileChangeRequestApprovalParams`, `FileChangeRequestApprovalResponse`, `PermissionsRequestApprovalParams`, `PermissionsRequestApprovalResponse`, `ToolRequestUserInputParams`, `ToolRequestUserInputResponse`. Fields keep their camelCase wire names (`threadId`, `itemId`). Union members of `ThreadItem` come out as `ThreadItem1`… classes; read the discriminator via `.type` on the unwrapped item (Task 6 defines `item_of`). Extra fields are allowed everywhere. First line of the file: `# generated by tools/gen_codex_protocol.py from codex <version> (schema sha256 <12 hex>) — do not edit`.
- Consumes: `compat.COMPAT["codex"].tested`.

Dry-run facts (2026-09-13): `datamodel-codegen 0.80.0` against codex 0.153.4's schema emits 13,093 lines; the schema's `definitions` are namespaced (`#/definitions/v2/Name`) and two files have dotted stems, both of which the script below flattens and skips — without that the tool aborts with "Modular references require an output directory".

- [ ] **Step 1: Add the dev dependency and lock**

In `pyproject.toml`, change the dev extra line to:

```toml
dev = ["pytest>=8", "datamodel-code-generator>=0.26"]
```

Run: `uv lock && uv sync --extra dev`
Expected: `uv.lock` updated; `uv run datamodel-codegen --version` prints a version.

- [ ] **Step 2: Write the golden fixture**

`tests/golden/chat/codex_appserver.jsonl` — one JSON object per line, captured live from `codex app-server` 0.153.4 on 2026-09-13 (strings trimmed, hook/mcp/status chatter dropped). The `id: 2` line is the `thread/resume` response with the thread's turns emptied:

```json
{"id": 1, "result": {"userAgent": "tandem-spike/0.153.4 (Mac OS 26.6.2; arm64) vscode/3.16.29 (tandem-spike; 0)", "codexHome": "/Users/bhavya/.codex", "platformFamily": "unix", "platformOs": "macos"}}
{"id": 2, "result": {"thread": {"id": "01a0774c-fdd8-7937-a3dd-ba15c718b01a", "extra": null, "sessionId": "01a0774c-fdd8-7937-a3dd-ba15c718b01a", "forkedFromId": null, "parentThreadId": null, "preview": "[tandem] This session is one half of tandem paired session 73b2cb165105.", "ephemeral": false, "section": null, "sectionEnteredAt": null, "projectId": null, "historyMode": "legacy", "modelProvider": "openai", "model": "gpt-5.5", "reasoningEffort": null, "createdAt": 1788707929, "updatedAt": 1788707929, "recencyAt": 1788708104, "status": {"type": "idle"}, "path": "/Users/bhavya/.codex/sessions/2026/09/06/rollout-2026-09-06T15-18-49-01a0774c-fdd8-7937-a3dd-ba15c718b01a.jsonl", "cwd": "/tmp/spike/proj", "cliVersion": "0.153.4", "source": "exec", "canAcceptDirectInput": true, "threadSource": "user", "agentNickname": null, "agentRole": null, "gitInfo": null, "name": null, "turns": []}, "model": "gpt-5.5", "modelProvider": "openai", "serviceTier": null, "cwd": "/tmp/spike/proj", "runtimeWorkspaceRoots": ["/tmp/spike/proj"], "instructionSources": [], "approvalPolicy": "untrusted", "approvalsReviewer": "user", "sandbox": {"type": "workspaceWrite", "writableRoots": [], "networkAccess": false, "excludeTmpdirEnvVar": false, "excludeSlashTmp": false}, "activePermissionProfile": {"id": ":workspace", "extends": null}, "reasoningEffort": null, "multiAgentMode": "explicitRequestOnly", "initialTurnsPage": null, "turnsBackwardsCursor": null, "itemsBackwardsCursor": null}}
{"method": "thread/tokenUsage/updated", "params": {"threadId": "01a0774c-fdd8-7937-a3dd-ba15c718b01a", "turnId": "01a0774f-a8e7-7631-8528-539aca001f46", "tokenUsage": {"total": {"totalTokens": 53759, "inputTokens": 53677, "cachedInputTokens": 28800, "cacheWriteInputTokens": 0, "outputTokens": 82, "reasoningOutputTokens": 14}, "last": {"totalTokens": 20708, "inputTokens": 20685, "cachedInputTokens": 5504, "cacheWriteInputTokens": 0, "outputTokens": 23, "reasoningOutputTokens": 14}, "modelContextWindow": 258400}}, "emittedAtMs": 1789310565395}
{"id": 3, "result": {"turn": {"id": "01a09b38-7c17-7111-9005-3fe234bc87ef", "items": [], "itemsView": "notLoaded", "status": "inProgress", "error": null, "startedAt": null, "completedAt": null, "durationMs": null}}}
{"method": "turn/started", "params": {"threadId": "01a0774c-fdd8-7937-a3dd-ba15c718b01a", "turn": {"id": "01a09b38-7c17-7111-9005-3fe234bc87ef", "items": [], "itemsView": "notLoaded", "status": "inProgress", "error": null, "startedAt": 1789310565, "completedAt": null, "durationMs": null}}, "emittedAtMs": 1789310565407}
{"method": "item/started", "params": {"item": {"type": "userMessage", "id": "01a09b38-8595-7f83-ac7c-ea9b8ec71019", "clientId": null, "content": [{"type": "text", "text": "Run this shell command: echo fixture — then reply with exactly DONE.", "text_elements": []}]}, "threadId": "01a0774c-fdd8-7937-a3dd-ba15c718b01a", "turnId": "01a09b38-7c17-7111-9005-3fe234bc87ef", "startedAtMs": 1789310567829}, "emittedAtMs": 1789310567830}
{"method": "item/completed", "params": {"item": {"type": "userMessage", "id": "01a09b38-8595-7f83-ac7c-ea9b8ec71019", "clientId": null, "content": [{"type": "text", "text": "Run this shell command: echo fixture — then reply with exactly DONE.", "text_elements": []}]}, "threadId": "01a0774c-fdd8-7937-a3dd-ba15c718b01a", "turnId": "01a09b38-7c17-7111-9005-3fe234bc87ef", "completedAtMs": 1789310567829}, "emittedAtMs": 1789310567830}
{"method": "item/started", "params": {"item": {"type": "reasoning", "id": "rs_00a69d94b0b0b34f016aa6b66933e887d0a04ed4ed214054e2", "summary": [], "content": []}, "threadId": "01a0774c-fdd8-7937-a3dd-ba15c718b01a", "turnId": "01a09b38-7c17-7111-9005-3fe234bc87ef", "startedAtMs": 1789310569244}, "emittedAtMs": 1789310569244}
{"method": "item/completed", "params": {"item": {"type": "reasoning", "id": "rs_00a69d94b0b0b34f016aa6b66933e887d0a04ed4ed214054e2", "summary": [], "content": []}, "threadId": "01a0774c-fdd8-7937-a3dd-ba15c718b01a", "turnId": "01a09b38-7c17-7111-9005-3fe234bc87ef", "completedAtMs": 1789310570406}, "emittedAtMs": 1789310570406}
{"method": "item/started", "params": {"item": {"type": "commandExecution", "id": "call_vDe2mWf2XYJ1BjLDXSDWgLSo", "pluginId": null, "scriptPath": null, "command": "/bin/zsh -lc 'echo fixture —'", "cwd": "/tmp/spike/proj", "processId": null, "source": "agent", "status": "inProgress", "commandActions": [{"type": "unknown", "command": "echo fixture —"}], "aggregatedOutput": null, "exitCode": null, "durationMs": null}, "threadId": "01a0774c-fdd8-7937-a3dd-ba15c718b01a", "turnId": "01a09b38-7c17-7111-9005-3fe234bc87ef", "startedAtMs": 1789310570845}, "emittedAtMs": 1789310570845}
{"method": "item/commandExecution/requestApproval", "id": 0, "params": {"kind": "command", "threadId": "01a0774c-fdd8-7937-a3dd-ba15c718b01a", "turnId": "01a09b38-7c17-7111-9005-3fe234bc87ef", "itemId": "call_vDe2mWf2XYJ1BjLDXSDWgLSo", "startedAtMs": 1789310570843, "environmentId": "local", "command": "/bin/zsh -lc 'echo fixture —'", "cwd": "/tmp/spike/proj", "commandActions": [{"type": "unknown", "command": "echo fixture —"}], "proposedExecpolicyAmendment": ["echo", "fixture", "—"], "availableDecisions": ["accept", {"acceptWithExecpolicyAmendment": {"execpolicy_amendment": ["echo", "fixture", "—"]}}, "cancel"]}}
{"method": "item/completed", "params": {"item": {"type": "commandExecution", "id": "call_vDe2mWf2XYJ1BjLDXSDWgLSo", "pluginId": null, "scriptPath": null, "command": "/bin/zsh -lc 'echo fixture —'", "cwd": "/tmp/spike/proj", "processId": "10305", "source": "unifiedExecStartup", "status": "completed", "commandActions": [{"type": "unknown", "command": "echo fixture —"}], "aggregatedOutput": "fixture —\n", "exitCode": 0, "durationMs": 0}, "threadId": "01a0774c-fdd8-7937-a3dd-ba15c718b01a", "turnId": "01a09b38-7c17-7111-9005-3fe234bc87ef", "completedAtMs": 1789310570909}, "emittedAtMs": 1789310570909}
{"method": "account/rateLimits/updated", "params": {"rateLimits": {"limitId": "codex", "limitName": null, "primary": {"usedPercent": 0, "windowDurationMins": 300, "resetsAt": 1789328567}, "secondary": {"usedPercent": 0, "windowDurationMins": 10080, "resetsAt": 1789915367}, "credits": {"hasCredits": false, "unlimited": false, "balance": "0"}, "individualLimit": null, "spendControlReached": null, "planType": "plus", "rateLimitReachedType": null}}, "emittedAtMs": 1789310571341}
{"method": "item/started", "params": {"item": {"type": "agentMessage", "id": "msg_00a69d94b0b0b34f016aa6b66decb487d0998b43928c84053a", "text": "", "phase": "final_answer", "memoryCitation": null, "delivery": null, "questions": null}, "threadId": "01a0774c-fdd8-7937-a3dd-ba15c718b01a", "turnId": "01a09b38-7c17-7111-9005-3fe234bc87ef", "startedAtMs": 1789310573958}, "emittedAtMs": 1789310573958}
{"method": "item/agentMessage/delta", "params": {"threadId": "01a0774c-fdd8-7937-a3dd-ba15c718b01a", "turnId": "01a09b38-7c17-7111-9005-3fe234bc87ef", "itemId": "msg_00a69d94b0b0b34f016aa6b66decb487d0998b43928c84053a", "delta": "DONE"}, "emittedAtMs": 1789310573974}
{"method": "item/completed", "params": {"item": {"type": "agentMessage", "id": "msg_00a69d94b0b0b34f016aa6b66decb487d0998b43928c84053a", "text": "DONE", "phase": "final_answer", "memoryCitation": null, "delivery": null, "questions": null}, "threadId": "01a0774c-fdd8-7937-a3dd-ba15c718b01a", "turnId": "01a09b38-7c17-7111-9005-3fe234bc87ef", "completedAtMs": 1789310574084}, "emittedAtMs": 1789310574084}
{"method": "turn/completed", "params": {"threadId": "01a0774c-fdd8-7937-a3dd-ba15c718b01a", "turn": {"id": "01a09b38-7c17-7111-9005-3fe234bc87ef", "items": [{"type": "agentMessage", "id": "msg_00a69d94b0b0b34f016aa6b66decb487d0998b43928c84053a", "text": "DONE", "phase": "final_answer", "memoryCitation": null, "delivery": null, "questions": null}], "itemsView": "summary", "status": "completed", "error": null, "startedAt": 1789310565, "completedAt": 1789310574, "durationMs": 8833}}, "emittedAtMs": 1789310574237}
```

- [ ] **Step 3: Write the failing pin test**

```python
# tests/test_codex_protocol.py
"""The generated codex models: pinned to the tested codex version and
proven against live-captured app-server lines."""
import json
from pathlib import Path

import pytest

from tandem.compat import COMPAT

GOLDEN = Path(__file__).parent / "golden" / "chat" / "codex_appserver.jsonl"

NOTIFICATIONS = {
    "item/started": "ItemStartedNotification",
    "item/completed": "ItemCompletedNotification",
    "item/agentMessage/delta": "AgentMessageDeltaNotification",
    "turn/started": "TurnStartedNotification",
    "turn/completed": "TurnCompletedNotification",
    "thread/tokenUsage/updated": "ThreadTokenUsageUpdatedNotification",
    "account/rateLimits/updated": "AccountRateLimitsUpdatedNotification",
    "item/commandExecution/requestApproval": "CommandExecutionRequestApprovalParams",
}


@pytest.fixture(scope="module")
def cp():
    from tandem.chat.runtime import codex_protocol
    return codex_protocol


def test_header_pins_the_tested_codex_version(cp):
    first = Path(cp.__file__).read_text().splitlines()[0]
    assert first.startswith("# generated by tools/gen_codex_protocol.py from codex ")
    assert f"from codex {COMPAT['codex'].tested} " in first


def test_every_golden_line_validates(cp):
    seen = set()
    for line in GOLDEN.read_text().splitlines():
        m = json.loads(line)
        method = m.get("method")
        if method in NOTIFICATIONS:
            getattr(cp, NOTIFICATIONS[method]).model_validate(m["params"])
            seen.add(method)
        elif m.get("id") == 1:
            cp.InitializeResponse.model_validate(m["result"])
        elif m.get("id") == 2:
            r = cp.ThreadResumeResponse.model_validate(m["result"])
            assert r.thread.id == "01a0774c-fdd8-7937-a3dd-ba15c718b01a"
        elif m.get("id") == 3:
            cp.TurnStartResponse.model_validate(m["result"])
    assert seen == set(NOTIFICATIONS)


def test_item_discriminator_is_readable(cp):
    types = []
    for line in GOLDEN.read_text().splitlines():
        m = json.loads(line)
        if m.get("method") == "item/started":
            n = cp.ItemStartedNotification.model_validate(m["params"])
            item = getattr(n.item, "root", n.item)
            types.append(item.type)
    assert types == ["userMessage", "reasoning", "commandExecution", "agentMessage"]


def test_requests_dump_on_the_wire_shape(cp):
    p = cp.TurnStartParams(threadId="t", input=[{"type": "text", "text": "hi"}])
    assert p.model_dump(by_alias=True, exclude_none=True) == {
        "threadId": "t", "input": [{"type": "text", "text": "hi", "text_elements": []}]}
    assert cp.CommandExecutionRequestApprovalResponse(decision="accept").model_dump() == {"decision": "accept"}
    assert cp.ThreadStartParams(cwd="/x").model_dump(by_alias=True, exclude_none=True) == {"cwd": "/x"}


def test_unknown_fields_are_ignored(cp):
    cp.AgentMessageDeltaNotification.model_validate(
        {"threadId": "t", "turnId": "u", "itemId": "i", "delta": "x", "futureField": 1})
```

Run: `uv run pytest tests/test_codex_protocol.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'tandem.chat.runtime.codex_protocol'`

- [ ] **Step 4: Write the generator**

```python
#!/usr/bin/env python3
# tools/gen_codex_protocol.py
"""Generate src/tandem/chat/runtime/codex_protocol.py from codex's app-server
JSON schema.

Default input is the schema the installed binary dumps
(`codex app-server generate-json-schema --out DIR`), so the models match
the codex on this machine exactly; `--schema-dir` points at a checkout's
`codex-rs/app-server-protocol/schema/json` instead (then pass
`--codex-version`). Only WANTED and what it references are emitted, via
datamodel-code-generator (dev-only dependency).

Schema facts the merge handles: definitions are namespaced under
`v2`/`v1` (`#/definitions/v2/Name`), which the generator would treat as
modules; two files have dotted stems; approval/user-input params are
standalone top-level schemas named by file.

Usage: uv run python tools/gen_codex_protocol.py [--schema-dir DIR --codex-version 0.153.4]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "src" / "tandem" / "chat" / "runtime" / "codex_protocol.py"

WANTED = [
    "InitializeParams", "InitializeResponse", "ClientInfo",
    "ThreadStartParams", "ThreadStartResponse", "ThreadResumeParams", "ThreadResumeResponse",
    "TurnStartParams", "TurnStartResponse", "TurnInterruptParams",
    "Thread", "Turn", "TurnStatus", "ThreadItem",
    "ItemStartedNotification", "ItemCompletedNotification", "AgentMessageDeltaNotification",
    "CommandExecutionOutputDeltaNotification", "FileChangeOutputDeltaNotification",
    "ReasoningSummaryTextDeltaNotification", "TurnStartedNotification", "TurnCompletedNotification",
    "ThreadTokenUsageUpdatedNotification", "AccountRateLimitsUpdatedNotification", "ErrorNotification",
    "CommandExecutionRequestApprovalParams", "CommandExecutionRequestApprovalResponse",
    "FileChangeRequestApprovalParams", "FileChangeRequestApprovalResponse",
    "PermissionsRequestApprovalParams", "PermissionsRequestApprovalResponse",
    "ToolRequestUserInputParams", "ToolRequestUserInputResponse",
]


def dump_schema_from_binary(tmp: Path) -> Path:
    subprocess.run(["codex", "app-server", "generate-json-schema", "--out", str(tmp)],
                   check=True, capture_output=True, text=True)
    hits = list(tmp.rglob("ClientRequest.json"))
    if not hits:
        sys.exit("codex app-server generate-json-schema produced no ClientRequest.json")
    return hits[0].parent


def codex_version() -> str:
    out = subprocess.run(["codex", "--version"], check=True, capture_output=True, text=True).stdout
    m = re.search(r"(\d+(?:\.\d+)+)", out)
    return m.group(1) if m else out.strip()


def normalize_refs(o) -> None:
    """`#/definitions/v2/Name` -> `#/definitions/Name`, in place."""
    if isinstance(o, dict):
        ref = o.get("$ref")
        if isinstance(ref, str):
            name = ref.split("#/definitions/")[-1].split("/")[-1]
            o["$ref"] = "#/definitions/" + (name[:-5] if name.endswith(".json") else name)
        for v in o.values():
            normalize_refs(v)
    elif isinstance(o, list):
        for v in o:
            normalize_refs(v)


def merge(schema_dir: Path) -> tuple[dict, str]:
    defs: dict = {}
    digest = hashlib.sha256()
    for f in sorted(schema_dir.rglob("*.json")):
        raw = f.read_bytes()
        digest.update(f.name.encode()); digest.update(raw)
        d = json.loads(raw)
        raw_defs = d.get("definitions", {})
        for ns in ("v2", "v1"):                     # v2 wins on a name clash
            for k, v in (raw_defs.get(ns) or {}).items():
                defs.setdefault(k, v)
        for k, v in raw_defs.items():
            if k not in ("v1", "v2"):
                defs.setdefault(k, v)
        top = {k: v for k, v in d.items() if k not in ("definitions", "$schema")}
        if f.stem.isidentifier() and any(k in top for k in ("properties", "oneOf", "enum", "type")):
            defs.setdefault(f.stem, top)
    normalize_refs(defs)
    missing = [w for w in WANTED if w not in defs]
    if missing:
        sys.exit(f"schema is missing wanted definitions: {missing}")
    root = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "title": "CodexProtocolRoot", "type": "object",
        "properties": {w: {"$ref": f"#/definitions/{w}"} for w in WANTED},
        "definitions": defs,
    }
    return root, digest.hexdigest()[:12]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--schema-dir", type=Path)
    ap.add_argument("--codex-version")
    ap.add_argument("--out", type=Path, default=OUT)
    a = ap.parse_args()
    tmp = Path(tempfile.mkdtemp(prefix="codex-schema-"))
    try:
        schema_dir = a.schema_dir or dump_schema_from_binary(tmp)
        version = a.codex_version or codex_version()
        root, sha = merge(schema_dir)
        merged = tmp / "merged.json"
        merged.write_text(json.dumps(root))
        gen = tmp / "generated.py"
        codegen = shutil.which("datamodel-codegen") or str(Path(sys.executable).parent / "datamodel-codegen")
        subprocess.run([
            codegen, "--input", str(merged), "--input-file-type", "jsonschema",
            "--output", str(gen), "--output-model-type", "pydantic_v2.BaseModel",
            "--target-python-version", "3.11", "--use-annotated",
            "--use-standard-collections", "--use-union-operator", "--disable-timestamp",
            "--allow-extra-fields", "--collapse-root-models", "--enum-field-as-literal", "all",
        ], check=True)
        header = (f"# generated by tools/gen_codex_protocol.py from codex {version} "
                  f"(schema sha256 {sha}) — do not edit\n# ruff: noqa\n")
        a.out.write_text(header + gen.read_text())
        print(f"wrote {a.out} ({len(a.out.read_text().splitlines())} lines) from codex {version}, schema {sha}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
```

- [ ] **Step 5: Generate and run the tests**

Run: `uv run python tools/gen_codex_protocol.py && uv run pytest tests/test_codex_protocol.py -q`
Expected: `wrote .../codex_protocol.py (~13100 lines) from codex 0.153.4, schema ...`; then 5 passed. If the installed codex is not 0.153.4, the header test fails on purpose: regenerate against a 0.153.4 checkout with `--schema-dir ~/git/codex/codex-rs/app-server-protocol/schema/json --codex-version 0.153.4` after `git -C ~/git/codex checkout rust-v0.153.4` (or update `COMPAT["codex"].tested` deliberately, with the live gate).

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml uv.lock tools/gen_codex_protocol.py src/tandem/chat/runtime/codex_protocol.py tests/golden/chat/codex_appserver.jsonl tests/test_codex_protocol.py
git commit -m "feat(chat): generate codex app-server protocol models from the installed schema" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>" -m "Claude-Session: https://claude.ai/code/session_01WZ7WV9iKDBigenNSsJaRdy"
```

---
### Task 5: Claude runtime (`claude -p` over stream-json)

**Files:**
- Create: `src/tandem/chat/runtime/claude.py`, `tests/fakes/__init__.py` (empty), `tests/fakes/fake_claude.py`, `tests/golden/chat/claude_stream.jsonl`
- Test: `tests/test_chat_claude.py`

**Interfaces:**
- Consumes: Task 3's events and helpers; `ChatConfig.claude_setting_sources`, `ChatConfig.show_thinking`; `paths.claude_transcript_path(cwd, sid)`.
- Produces: `ClaudeRuntime(cfg, *, binary: list[str] | None = None)` with `harness = "claude"`, `argv(native_id, fresh, model) -> list[str]`, `handle_line(m: dict, emit, answers, send) -> TurnOutcome | None` (pure per-line protocol step; returns the outcome on `result`), `run_turn(...)`, `interrupt()`, `close()`. Approval mapping: allow → `{"behavior":"allow","updatedInput":input}`; always → the same plus `updatedPermissions` built from the first `addRules` suggestion with `destination` rewritten to `"session"`; deny → `{"behavior":"deny","message":"denied in tandem chat"}`. `AskUserQuestion` becomes one `QuestionRequest` per question and answers go back as `updatedInput.answers[question] = answer`.

- [ ] **Step 1: Write the golden fixture**

`tests/golden/chat/claude_stream.jsonl` (claude 2.1.265, 2026-09-13; one line each of the shapes the client reads, long fields trimmed):

```json
{"type": "system", "subtype": "init", "cwd": "/tmp/spike/proj", "session_id": "d768d411-2c2b-416a-8481-44dd5164f268", "tools": ["Task", "AskUserQuestion", "Bash"], "model": "claude-haiku-4-5-20251001", "permissionMode": "default", "slash_commands": ["deep-research", "claude-md-management:revise-claude-md"], "claude_code_version": "2.1.265", "uuid": "ea768405-a161-40e1-b0be-c8f0e81f66d0"}
{"type": "stream_event", "event": {"type": "message_start", "message": {"model": "claude-haiku-4-5-20251001", "id": "msg_011Cf1d5LkZwFBrgD8CJXw72", "type": "message", "role": "assistant", "content": [], "stop_reason": null, "usage": {"input_tokens": 10, "output_tokens": 3}}}, "session_id": "d768d411-2c2b-416a-8481-44dd5164f268", "parent_tool_use_id": null, "uuid": "917eb98e-982d-4290-b853-cf3d6d28a23e"}
{"type": "stream_event", "event": {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "I should run it.", "estimated_tokens": 50}}, "session_id": "d768d411-2c2b-416a-8481-44dd5164f268", "parent_tool_use_id": null, "uuid": "79e304f9-5a6d-44c8-894e-ffd60d55db58"}
{"type": "assistant", "message": {"model": "claude-haiku-4-5-20251001", "id": "msg_011Cf1d5LkZwFBrgD8CJXw72", "type": "message", "role": "assistant", "content": [{"type": "tool_use", "id": "toolu_01EdBuo5aF5Cjkj7NbwFrLeC", "name": "Bash", "input": {"command": "touch fixture-claude.txt", "description": "Create fixture-claude.txt file"}, "caller": {"type": "direct"}}], "stop_reason": null, "usage": {"input_tokens": 10, "output_tokens": 3}}, "parent_tool_use_id": null, "session_id": "d768d411-2c2b-416a-8481-44dd5164f268", "uuid": "7b84053e-4dc8-456a-aca4-5ab7186e84db", "timestamp": "2026-09-13T14:48:31.777Z"}
{"type": "control_request", "request_id": "16c61d1a-b82a-4c4b-8a21-1f3298f9eaaf", "request": {"subtype": "can_use_tool", "tool_name": "Bash", "display_name": "Bash", "input": {"command": "touch fixture-claude.txt", "description": "Create fixture-claude.txt file"}, "description": "Create fixture-claude.txt file", "permission_suggestions": [{"type": "addRules", "rules": [{"toolName": "Bash", "ruleContent": "touch fixture-claude.txt"}], "behavior": "allow", "destination": "localSettings"}, {"type": "addDirectories", "directories": ["/tmp/spike/proj"], "destination": "session"}, {"type": "setMode", "mode": "acceptEdits", "destination": "session"}], "blocked_path": "/tmp/spike/proj/fixture-claude.txt", "tool_use_id": "toolu_01EdBuo5aF5Cjkj7NbwFrLeC"}}
{"type": "user", "message": {"role": "user", "content": [{"tool_use_id": "toolu_01EdBuo5aF5Cjkj7NbwFrLeC", "type": "tool_result", "content": "(Bash completed with no output)", "is_error": false}]}, "parent_tool_use_id": null, "session_id": "d768d411-2c2b-416a-8481-44dd5164f268", "uuid": "741527d2-3ee3-4252-be34-5f40ec6da075", "timestamp": "2026-09-13T14:48:32.302Z", "tool_use_result": {"stdout": "", "stderr": "", "interrupted": false, "isImage": false, "noOutputExpected": true}}
{"type": "stream_event", "event": {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "DONE"}}, "session_id": "d768d411-2c2b-416a-8481-44dd5164f268", "parent_tool_use_id": null, "uuid": "8d5d5551-1c03-428d-8fcd-8f1f799e55ab"}
{"type": "assistant", "message": {"model": "claude-haiku-4-5-20251001", "id": "msg_011Cf1d5Xo6YKPwB3Gd4fvF9", "type": "message", "role": "assistant", "content": [{"type": "text", "text": "DONE"}], "stop_reason": null, "usage": {"input_tokens": 8, "output_tokens": 1}}, "parent_tool_use_id": null, "session_id": "d768d411-2c2b-416a-8481-44dd5164f268", "uuid": "e0939dac-e12d-4f6c-aaf7-e368a7ad54b4", "timestamp": "2026-09-13T14:48:33.598Z"}
{"type": "rate_limit_event", "rate_limit_info": {"status": "allowed", "resetsAt": 1789322400, "rateLimitType": "five_hour", "unifiedWindows": {"five_hour": {"utilization": 0.09, "resetsAt": 1789322400}, "seven_day": {"utilization": 0.04, "resetsAt": 1789840800}}}, "uuid": "c920b6b3-0000-4000-8000-000000000000", "session_id": "d768d411-2c2b-416a-8481-44dd5164f268"}
{"type": "result", "subtype": "success", "is_error": false, "duration_ms": 4142, "duration_api_ms": 3654, "num_turns": 2, "result": "DONE", "session_id": "d768d411-2c2b-416a-8481-44dd5164f268", "total_cost_usd": 0.0158657, "usage": {"input_tokens": 18, "cache_creation_input_tokens": 5361, "cache_read_input_tokens": 42357, "output_tokens": 178}, "permission_denials": [], "uuid": "087d098a-17e5-4f25-8462-2a2dffa6672b"}
```

- [ ] **Step 2: Write the fake CLI**

`tests/fakes/__init__.py` is empty. `tests/fakes/fake_claude.py`:

```python
"""A scripted stand-in for `claude -p --input-format stream-json`.

Records its argv to $FAKE_ARGV_OUT, reads one user message from stdin, then
plays the scenario named by $FAKE_CLAUDE_SCENARIO:
  approve   (default) tool_use Bash -> can_use_tool -> tool_result -> DONE
  text      a text delta and a result, no tools
  question  AskUserQuestion -> can_use_tool with questions -> echoes the answer
  interrupt streams one delta, then waits for an interrupt control_request
  crash     prints init, writes "boom" to stderr, exits 3
"""
import json
import os
import sys

SID = "fake-claude-session"


def out(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def read():
    line = sys.stdin.readline()
    return json.loads(line) if line else None


def delta(text):
    out({"type": "stream_event", "event": {"type": "content_block_delta", "index": 0,
         "delta": {"type": "text_delta", "text": text}}, "session_id": SID})


def result(is_error=False, subtype="success", turns=1):
    out({"type": "result", "subtype": subtype, "is_error": is_error, "num_turns": turns,
         "total_cost_usd": 0.001, "session_id": SID, "result": "DONE" if not is_error else "error",
         "usage": {"input_tokens": 1, "output_tokens": 1}})


def main():
    if os.environ.get("FAKE_ARGV_OUT"):
        with open(os.environ["FAKE_ARGV_OUT"], "w") as f:
            json.dump(sys.argv[1:], f)
    scenario = os.environ.get("FAKE_CLAUDE_SCENARIO", "approve")
    first = read()
    assert first and first["type"] == "user", first
    prompt = first["message"]["content"][0]["text"]
    out({"type": "system", "subtype": "init", "session_id": SID, "model": "fake", "tools": ["Bash"],
         "cwd": os.getcwd(), "slash_commands": [], "claude_code_version": "0.0.0"})
    if scenario == "crash":
        sys.stderr.write("boom\n")
        sys.exit(3)
    if scenario == "text":
        delta("hello "); delta("world"); result(); return
    if scenario == "interrupt":
        delta("partial")
        while True:
            m = read()
            if m is None:
                return
            if m.get("type") == "control_request" and m["request"].get("subtype") == "interrupt":
                out({"type": "control_response", "response": {"subtype": "success",
                     "request_id": m["request_id"], "response": {}}})
                result(is_error=True, subtype="error_during_execution"); return
    if scenario == "question":
        inp = {"questions": [{"question": "Which color?", "header": "Color",
                              "options": [{"label": "red"}, {"label": "blue"}]}]}
        out({"type": "assistant", "message": {"role": "assistant", "content": [
             {"type": "tool_use", "id": "toolu_q", "name": "AskUserQuestion", "input": inp}]}, "session_id": SID})
        out({"type": "control_request", "request_id": "req-q", "request": {
             "subtype": "can_use_tool", "tool_name": "AskUserQuestion", "input": inp, "permission_suggestions": []}})
        m = read()
        answer = m["response"]["response"]["updatedInput"]["answers"]["Which color?"]
        out({"type": "user", "message": {"role": "user", "content": [
             {"tool_use_id": "toolu_q", "type": "tool_result", "content": json.dumps({"answers": {"Which color?": answer}}), "is_error": False}]}, "session_id": SID})
        delta(f"you chose {answer}"); result(turns=2); return
    # approve
    inp = {"command": "touch x.txt", "description": "make x"}
    out({"type": "assistant", "message": {"role": "assistant", "content": [
         {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": inp}]}, "session_id": SID})
    out({"type": "control_request", "request_id": "req-1", "request": {
         "subtype": "can_use_tool", "tool_name": "Bash", "input": inp,
         "permission_suggestions": [{"type": "addRules", "rules": [{"toolName": "Bash", "ruleContent": "touch x.txt"}],
                                     "behavior": "allow", "destination": "localSettings"}]}})
    m = read()
    resp = m["response"]["response"]
    with open(os.environ.get("FAKE_REPLY_OUT", os.devnull), "w") as f:
        json.dump(resp, f)
    if resp.get("behavior") == "allow":
        out({"type": "user", "message": {"role": "user", "content": [
             {"tool_use_id": "toolu_1", "type": "tool_result", "content": "ok\nline2\nline3", "is_error": False}]}, "session_id": SID})
        delta("DONE"); result(turns=2)
    else:
        out({"type": "user", "message": {"role": "user", "content": [
             {"tool_use_id": "toolu_1", "type": "tool_result", "content": "denied in tandem chat", "is_error": True}]}, "session_id": SID})
        delta("I could not run it."); result(turns=2)
    # a trailing prompt echo proves the client passed the text through verbatim
    with open(os.environ.get("FAKE_PROMPT_OUT", os.devnull), "w") as f:
        f.write(prompt)


if __name__ == "__main__":
    main()
```

- [ ] **Step 3: Write the failing tests**

```python
# tests/test_chat_claude.py
import json
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from tandem.chat.events import (ApprovalRequest, QuestionRequest, TextDelta, ThinkingDelta,
                                ToolFinished, ToolOutput, ToolStarted, TurnFinished)
from tandem.chat.runtime.claude import ClaudeRuntime
from tandem.config import ChatConfig

FAKE = Path(__file__).parent / "fakes" / "fake_claude.py"
GOLDEN = Path(__file__).parent / "golden" / "chat" / "claude_stream.jsonl"


class Recorder:
    def __init__(self, approve="allow", answer="red"):
        self.events = []
        self.approvals = []
        self.questions = []
        self._approve = approve
        self._answer = answer

    def emit(self, ev):
        self.events.append(ev)

    def approve(self, req):
        self.approvals.append(req)
        return self._approve

    def answer(self, req):
        self.questions.append(req)
        return self._answer

    def kinds(self):
        return [type(e).__name__ for e in self.events]


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / ".claude"))
    monkeypatch.setenv("FAKE_ARGV_OUT", str(tmp_path / "argv.json"))
    monkeypatch.setenv("FAKE_REPLY_OUT", str(tmp_path / "reply.json"))
    monkeypatch.setenv("FAKE_PROMPT_OUT", str(tmp_path / "prompt.txt"))
    proj = tmp_path / "proj"; proj.mkdir()
    return SimpleNamespace(tmp=tmp_path, session=SimpleNamespace(cwd=str(proj)),
                           runtime=ClaudeRuntime(ChatConfig(), binary=[sys.executable, str(FAKE)]))


def test_argv_fresh_vs_resume(env):
    rt = env.runtime
    fresh = rt.argv("sid-1", fresh=True, model="")
    assert fresh[2:4] == ["--session-id", "sid-1"]
    assert "--resume" not in fresh
    resumed = rt.argv("sid-1", fresh=False, model="haiku")
    assert resumed[2:4] == ["--resume", "sid-1"]
    assert resumed[-2:] == ["--model", "haiku"]
    for flag in ("--input-format", "--output-format", "--verbose", "--include-partial-messages",
                 "--permission-prompt-tool", "--setting-sources"):
        assert flag in resumed
    i = resumed.index("--setting-sources")
    assert resumed[i + 1] == "user,project,local"


def test_approve_flow_allow(env):
    rec = Recorder("allow")
    out = env.runtime.run_turn(env.session, "sid-1", "make x", "", rec.emit, rec)
    assert out.status == "completed"
    assert rec.approvals == [ApprovalRequest("command", "Bash touch x.txt")]
    assert rec.kinds() == ["ToolStarted", "ToolOutput", "ToolFinished", "TextDelta", "TurnFinished"]
    started = rec.events[0]
    assert started == ToolStarted("toolu_1", "Bash", "touch x.txt")
    assert rec.events[1] == ToolOutput("toolu_1", "ok\nline2\nline3")
    assert rec.events[2] == ToolFinished("toolu_1", True, "")
    assert rec.events[3] == TextDelta("DONE")
    assert rec.events[4].status == "completed" and "2 turns" in rec.events[4].usage
    reply = json.loads((env.tmp / "reply.json").read_text())
    assert reply == {"behavior": "allow", "updatedInput": {"command": "touch x.txt", "description": "make x"}}
    assert (env.tmp / "prompt.txt").read_text() == "make x"
    argv = json.loads((env.tmp / "argv.json").read_text())
    assert argv[:3] == ["-p", "--session-id", "sid-1"]     # no transcript yet -> fresh


def test_resume_when_transcript_exists(env):
    from tandem import paths
    p = paths.claude_transcript_path(env.session.cwd, "sid-2"); p.parent.mkdir(parents=True); p.write_text("{}\n")
    rec = Recorder("allow")
    env.runtime.run_turn(env.session, "sid-2", "go", "", rec.emit, rec)
    argv = json.loads((env.tmp / "argv.json").read_text())
    assert argv[:3] == ["-p", "--resume", "sid-2"]


def test_approve_flow_always_adds_session_rule(env):
    rec = Recorder("always")
    env.runtime.run_turn(env.session, "sid-1", "make x", "", rec.emit, rec)
    reply = json.loads((env.tmp / "reply.json").read_text())
    assert reply["behavior"] == "allow"
    assert reply["updatedPermissions"] == [{"type": "addRules", "rules": [{"toolName": "Bash", "ruleContent": "touch x.txt"}],
                                            "behavior": "allow", "destination": "session"}]


def test_approve_flow_deny(env):
    rec = Recorder("deny")
    out = env.runtime.run_turn(env.session, "sid-1", "make x", "", rec.emit, rec)
    assert out.status == "completed"       # the model carried on after the denial
    reply = json.loads((env.tmp / "reply.json").read_text())
    assert reply == {"behavior": "deny", "message": "denied in tandem chat"}
    assert ToolFinished("toolu_1", False, "denied in tandem chat") in rec.events


def test_text_only_turn(env, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", "text")
    rec = Recorder()
    out = env.runtime.run_turn(env.session, "sid-1", "hi", "", rec.emit, rec)
    assert out.status == "completed"
    assert [e for e in rec.events if isinstance(e, TextDelta)] == [TextDelta("hello "), TextDelta("world")]


def test_question_round_trip(env, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", "question")
    rec = Recorder(answer="blue")
    out = env.runtime.run_turn(env.session, "sid-1", "ask me", "", rec.emit, rec)
    assert out.status == "completed"
    assert rec.questions == [QuestionRequest("Which color?", ("red", "blue"))]
    assert TextDelta("you chose blue") in rec.events


def test_crash_is_failed_with_stderr(env, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", "crash")
    rec = Recorder()
    out = env.runtime.run_turn(env.session, "sid-1", "hi", "", rec.emit, rec)
    assert out.status == "failed"
    assert "boom" in out.error
    assert rec.events[-1] == TurnFinished("failed", "")


def test_interrupt_marks_turn_interrupted(env, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", "interrupt")
    rec = Recorder()
    rt = env.runtime
    holder = {}

    def run():
        holder["out"] = rt.run_turn(env.session, "sid-1", "hi", "", rec.emit, rec)

    t = threading.Thread(target=run); t.start()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not any(isinstance(e, TextDelta) for e in rec.events):
        time.sleep(0.02)
    rt.interrupt()
    t.join(10)
    assert holder["out"].status == "interrupted"


def test_golden_lines_drive_the_parser():
    rec = Recorder("allow")
    sent = []
    rt = ClaudeRuntime(ChatConfig(show_thinking=True))
    outcome = None
    for line in GOLDEN.read_text().splitlines():
        got = rt.handle_line(json.loads(line), rec.emit, rec, sent.append)
        outcome = got or outcome
    assert outcome is not None and outcome.status == "completed"
    assert rec.kinds() == ["ThinkingDelta", "ToolStarted", "ToolOutput", "ToolFinished", "TextDelta", "TurnFinished"]
    assert rec.events[0] == ThinkingDelta("I should run it.")
    assert rec.events[1] == ToolStarted("toolu_01EdBuo5aF5Cjkj7NbwFrLeC", "Bash", "touch fixture-claude.txt")
    assert sent[0]["type"] == "control_response" and sent[0]["response"]["request_id"] == "16c61d1a-b82a-4c4b-8a21-1f3298f9eaaf"
```

Run: `uv run pytest tests/test_chat_claude.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'tandem.chat.runtime.claude'`

- [ ] **Step 4: Write the runtime**

```python
# src/tandem/chat/runtime/claude.py
"""Claude Code headless: one `claude -p` process per turn over stream-json.

Wire shapes (captured live 2026-09-13 on claude 2.1.265, see
tests/golden/chat/claude_stream.jsonl; the open-source Agent SDK is the
reference for anything not captured):

  client -> cli
    {"type":"user","message":{"role":"user","content":[{"type":"text","text":…}]},
     "parent_tool_use_id":null,"session_id":SID}
    {"type":"control_request","request_id":ID,"request":{"subtype":"interrupt"}}
    {"type":"control_response","response":{"subtype":"success","request_id":ID,"response":{…}}}
  cli -> client
    system/init · stream_event (content_block_delta: text_delta | thinking_delta)
    assistant (content blocks incl. tool_use) · user (tool_result blocks)
    control_request (subtype can_use_tool: tool_name, input, permission_suggestions)
    result (subtype, is_error, num_turns, total_cost_usd)

The process exits after `result`; that exit is the turn boundary, so no
Stop-hook sentinel is wired. `handle_line` is the whole protocol as a pure
function of one parsed line so the golden fixture can drive it."""

from __future__ import annotations

import json
import subprocess
import threading
import uuid
from collections import deque
from typing import Callable

from ... import paths
from ..events import (Answers, ApprovalRequest, LiveEvent, QuestionRequest, TextDelta,
                      ThinkingDelta, ToolFinished, ToolOutput, ToolStarted, TurnFinished,
                      TurnOutcome)
from . import child_env, first_line, summarize_args, terminate

_COMMAND_TOOLS = ("Bash",)


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(b.get("text", "") for b in content
                       if isinstance(b, dict) and b.get("type") == "text")
    return ""


class ClaudeRuntime:
    harness = "claude"

    def __init__(self, cfg, *, binary: list[str] | None = None):
        self.cfg = cfg
        self.binary = list(binary) if binary else ["claude"]
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._interrupted = False
        self._streamed_text = False   # did the current message stream text deltas?

    # -- argv ----------------------------------------------------------------

    def argv(self, native_id: str, fresh: bool, model: str) -> list[str]:
        argv = [*self.binary, "-p", "--session-id" if fresh else "--resume", native_id,
                "--input-format", "stream-json", "--output-format", "stream-json",
                "--verbose", "--include-partial-messages",
                "--permission-prompt-tool", "stdio",
                "--setting-sources", ",".join(self.cfg.claude_setting_sources)]
        if model:
            argv += ["--model", model]
        return argv

    # -- protocol ------------------------------------------------------------

    def _approval(self, req: dict, answers: Answers) -> dict:
        tool = req.get("tool_name", "")
        inp = req.get("input") or {}
        if tool == "AskUserQuestion":
            answered = {}
            for q in inp.get("questions") or []:
                prompt = q.get("question", "")
                options = tuple(o.get("label", "") for o in q.get("options") or []
                                if isinstance(o, dict))
                answered[prompt] = answers.answer(QuestionRequest(prompt, options))
            return {"behavior": "allow", "updatedInput": {**inp, "answers": answered}}
        kind = "command" if tool in _COMMAND_TOOLS else "permission"
        detail = f"{tool} {summarize_args(tool, inp)}".strip()
        choice = answers.approve(ApprovalRequest(kind, detail))
        if choice == "deny":
            return {"behavior": "deny", "message": "denied in tandem chat"}
        resp: dict = {"behavior": "allow", "updatedInput": inp}
        if choice == "always":
            for s in req.get("permission_suggestions") or []:
                if isinstance(s, dict) and s.get("type") == "addRules":
                    resp["updatedPermissions"] = [{"type": "addRules", "rules": s.get("rules", []),
                                                   "behavior": "allow", "destination": "session"}]
                    break
        return resp

    def handle_line(self, m: dict, emit: Callable[[LiveEvent], None], answers: Answers,
                    send: Callable[[dict], None]) -> TurnOutcome | None:
        """One parsed stdout line. Returns the outcome on `result`, else None."""
        t = m.get("type")
        if t == "stream_event":
            ev = m.get("event") or {}
            if ev.get("type") == "message_start":
                self._streamed_text = False
            elif ev.get("type") == "content_block_delta":
                d = ev.get("delta") or {}
                if d.get("type") == "text_delta":
                    self._streamed_text = True
                    emit(TextDelta(d.get("text", "")))
                elif d.get("type") == "thinking_delta" and d.get("thinking"):
                    emit(ThinkingDelta(d["thinking"]))
            return None
        if t == "assistant":
            for b in (m.get("message") or {}).get("content") or []:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "tool_use":
                    emit(ToolStarted(b.get("id", ""), b.get("name", ""),
                                     summarize_args(b.get("name", ""), b.get("input"))))
                elif b.get("type") == "text" and not self._streamed_text and b.get("text"):
                    emit(TextDelta(b["text"]))       # partial messages off: paint the block
            return None
        if t == "user":
            for b in (m.get("message") or {}).get("content") or []:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    text = _text_of(b.get("content"))
                    err = bool(b.get("is_error"))
                    emit(ToolOutput(b.get("tool_use_id", ""), text))
                    emit(ToolFinished(b.get("tool_use_id", ""), not err, first_line(text) if err else ""))
            return None
        if t == "control_request":
            req = m.get("request") or {}
            rid = m.get("request_id")
            response = self._approval(req, answers) if req.get("subtype") == "can_use_tool" else {}
            send({"type": "control_response",
                  "response": {"subtype": "success", "request_id": rid, "response": response}})
            return None
        if t == "result":
            if self._interrupted:
                status = "interrupted"
            else:
                status = "failed" if m.get("is_error") else "completed"
            cost = m.get("total_cost_usd")
            usage = f"{m.get('num_turns', 0)} turns"
            if isinstance(cost, (int, float)):
                usage += f" · ${cost:.4f}"
            emit(TurnFinished(status, usage))
            return TurnOutcome(status, error=str(m.get("result", "")) if status == "failed" else "")
        return None     # system/init, rate_limit_event, control_response: nothing to paint

    # -- process -------------------------------------------------------------

    def run_turn(self, session, native_id: str | None, prompt: str, model: str,
                 emit: Callable[[LiveEvent], None], answers: Answers) -> TurnOutcome:
        assert native_id, "claude session ids are minted at pair time"
        fresh = not paths.claude_transcript_path(session.cwd, native_id).exists()
        self._interrupted = False
        self._streamed_text = False
        proc = subprocess.Popen(
            self.argv(native_id, fresh, model), cwd=session.cwd, env=child_env(),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1, start_new_session=True,
        )
        with self._lock:
            self._proc = proc
        tail: deque[str] = deque(maxlen=20)
        threading.Thread(target=lambda: [tail.append(l.rstrip()) for l in proc.stderr],
                         name="tandem-chat-claude-stderr", daemon=True).start()

        def send(obj: dict) -> None:
            with self._lock:
                if proc.stdin and not proc.stdin.closed:
                    proc.stdin.write(json.dumps(obj) + "\n")
                    proc.stdin.flush()

        send({"type": "user",
              "message": {"role": "user", "content": [{"type": "text", "text": prompt}]},
              "parent_tool_use_id": None, "session_id": native_id})
        outcome: TurnOutcome | None = None
        try:
            for line in proc.stdout:
                try:
                    m = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(m, dict):
                    continue
                outcome = self.handle_line(m, emit, answers, send)
                if outcome is not None:
                    break
        finally:
            terminate(proc, soft=lambda: proc.stdin.close(), soft_timeout=3.0)
            with self._lock:
                self._proc = None
        if outcome is None:
            status = "interrupted" if self._interrupted else "failed"
            outcome = TurnOutcome(status, error="\n".join(tail) or f"claude exited {proc.returncode}")
            emit(TurnFinished(status, ""))
        return outcome

    def interrupt(self) -> None:
        with self._lock:
            proc = self._proc
            if proc is None or proc.poll() is not None:
                return
            self._interrupted = True
            try:
                proc.stdin.write(json.dumps({"type": "control_request", "request_id": f"int-{uuid.uuid4().hex[:8]}",
                                             "request": {"subtype": "interrupt"}}) + "\n")
                proc.stdin.flush()
            except (OSError, ValueError):
                pass

    def close(self) -> None:
        with self._lock:
            proc = self._proc
        if proc is not None:
            terminate(proc, soft=lambda: proc.stdin.close(), soft_timeout=2.0)
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/test_chat_claude.py -q`
Expected: 10 passed

- [ ] **Step 6: Commit**

```bash
git add src/tandem/chat/runtime/claude.py tests/fakes tests/golden/chat/claude_stream.jsonl tests/test_chat_claude.py
git commit -m "feat(chat): claude runtime over stream-json with stdio permissions" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>" -m "Claude-Session: https://claude.ai/code/session_01WZ7WV9iKDBigenNSsJaRdy"
```

---
### Task 6: Codex runtime (app-server JSON-RPC over stdio)

**Files:**
- Create: `src/tandem/chat/runtime/codex.py`, `tests/fakes/fake_codex_appserver.py`
- Test: `tests/test_chat_codex.py`

**Interfaces:**
- Consumes: Task 3 helpers, Task 4 models (`tandem.chat.runtime.codex_protocol as cp`), `ChatConfig.codex_approval_policy` / `codex_sandbox`, `ratelimit.Window`, `ratelimit.window_label`, `ratelimit.format_windows`, `tandem.__version__` (falls back to `"0"` if absent).
- Produces: `CodexRuntime(cfg, *, binary: list[str] | None = None)` with `harness = "codex"`, `handle(m, proc_send, emit, answers) -> TurnOutcome | None` (one parsed server line; `proc_send(obj)` writes a line back), `run_turn(...)` (returns `native_id` set when it had to `thread/start`), `interrupt()`, `close()`; module helpers `item_of(notification)` and `strip_shell(command)`.
- Decision mapping: allow → `accept`; always → `acceptForSession` when the request's `availableDecisions` lists it, else `accept`; deny → `decline` when listed, else `cancel`. Permissions requests: allow/always → `{"permissions": <requested>, "scope": "turn"|"session"}`, deny → JSON-RPC error `-32001 "denied in tandem chat"`. User input: `{"answers": {question.id: {"answers": [text]}}}`.

- [ ] **Step 1: Write the fake app-server**

`tests/fakes/fake_codex_appserver.py`:

```python
"""A scripted stand-in for `codex app-server` (JSON-RPC 2.0, one message per line).

Records argv to $FAKE_ARGV_OUT and every request's params (one JSON object
per line: {"method":…, "params":…}) to $FAKE_PARAMS_OUT; the approval reply
goes to $FAKE_REPLY_OUT. Scenario from $FAKE_CODEX_SCENARIO:
  approve   (default) resume -> turn -> command approval -> DONE
  fresh     like approve but expects thread/start (no threadId)
  lock      thread/resume fails with "already has an active writer"
  crash     exits 2 after turn/start with "kaboom" on stderr
  interrupt streams a delta, waits for turn/interrupt, completes as interrupted
  question  asks a requestUserInput question and echoes the answer
"""
import json
import os
import sys

T = "01a0774c-fdd8-7937-a3dd-ba15c718b01a"
TURN = "turn-1"


def out(obj):
    sys.stdout.write(json.dumps(obj) + "\n"); sys.stdout.flush()


def notify(method, params):
    out({"jsonrpc": "2.0", "method": method, "params": params})


def read():
    line = sys.stdin.readline()
    return json.loads(line) if line else None


def record(m):
    p = os.environ.get("FAKE_PARAMS_OUT")
    if p:
        with open(p, "a") as f:
            f.write(json.dumps({"method": m.get("method"), "params": m.get("params")}) + "\n")


def item(kind, **fields):
    return {"type": kind, "id": fields.pop("id", "call-1"), **fields}


def main():
    if os.environ.get("FAKE_ARGV_OUT"):
        with open(os.environ["FAKE_ARGV_OUT"], "w") as f:
            json.dump(sys.argv[1:], f)
    scenario = os.environ.get("FAKE_CODEX_SCENARIO", "approve")
    thread_id = T
    while True:
        m = read()
        if m is None:
            return
        record(m)
        meth, rid = m.get("method"), m.get("id")
        if meth == "initialize":
            out({"jsonrpc": "2.0", "id": rid, "result": {"userAgent": "fake", "codexHome": "/x", "platformFamily": "unix", "platformOs": "macos"}})
        elif meth == "initialized":
            pass
        elif meth == "thread/resume":
            if scenario == "lock":
                out({"jsonrpc": "2.0", "id": rid, "error": {"code": -32600, "message": f"thread {m['params']['threadId']} already has an active writer"}})
                continue
            thread_id = m["params"]["threadId"]
            out({"jsonrpc": "2.0", "id": rid, "result": {"thread": {"id": thread_id, "cwd": m["params"].get("cwd"), "turns": []}, "model": "gpt-fake"}})
        elif meth == "thread/start":
            thread_id = "thread-new"
            out({"jsonrpc": "2.0", "id": rid, "result": {"thread": {"id": thread_id, "cwd": m["params"].get("cwd"), "turns": []}, "model": "gpt-fake"}})
        elif meth == "turn/start":
            out({"jsonrpc": "2.0", "id": rid, "result": {"turn": {"id": TURN, "items": [], "itemsView": "notLoaded", "status": "inProgress", "error": None, "startedAt": None, "completedAt": None, "durationMs": None}}})
            if scenario == "crash":
                sys.stderr.write("kaboom\n"); sys.exit(2)
            notify("turn/started", {"threadId": thread_id, "turn": {"id": TURN, "items": [], "itemsView": "notLoaded", "status": "inProgress", "error": None, "startedAt": 1, "completedAt": None, "durationMs": None}})
            if scenario == "interrupt":
                notify("item/started", {"threadId": thread_id, "turnId": TURN, "startedAtMs": 1, "item": item("agentMessage", id="msg-1", text="", phase="final_answer")})
                notify("item/agentMessage/delta", {"threadId": thread_id, "turnId": TURN, "itemId": "msg-1", "delta": "partial"})
                continue
            if scenario == "question":
                out({"jsonrpc": "2.0", "id": 7, "method": "item/tool/requestUserInput", "params": {
                    "threadId": thread_id, "turnId": TURN, "itemId": "tool-q", "isBlocking": True,
                    "questions": [{"id": "q1", "header": "Color", "question": "Which color?", "isOther": False, "isSecret": False,
                                   "options": [{"label": "red", "description": ""}, {"label": "blue", "description": ""}]}]}})
                continue
            notify("item/started", {"threadId": thread_id, "turnId": TURN, "startedAtMs": 1, "item": item(
                "commandExecution", command="/bin/zsh -lc 'touch x.txt'", cwd="/p", status="inProgress",
                commandActions=[{"type": "unknown", "command": "touch x.txt"}], aggregatedOutput=None, exitCode=None, durationMs=None,
                pluginId=None, scriptPath=None, processId=None, source="agent")})
            out({"jsonrpc": "2.0", "id": 0, "method": "item/commandExecution/requestApproval", "params": {
                "kind": "command", "threadId": thread_id, "turnId": TURN, "itemId": "call-1", "startedAtMs": 1,
                "command": "/bin/zsh -lc 'touch x.txt'", "cwd": "/p", "commandActions": [{"type": "unknown", "command": "touch x.txt"}],
                "availableDecisions": ["accept", "acceptForSession", "decline", "cancel"]}})
        elif meth == "turn/interrupt":
            out({"jsonrpc": "2.0", "id": rid, "result": {}})
            notify("turn/completed", {"threadId": thread_id, "turn": {"id": TURN, "items": [], "itemsView": "summary", "status": "interrupted", "error": None, "startedAt": 1, "completedAt": 2, "durationMs": 1}})
        elif rid == 7 and "result" in m:      # the user-input answer
            answer = m["result"]["answers"]["q1"]["answers"][0]
            notify("item/started", {"threadId": thread_id, "turnId": TURN, "startedAtMs": 1, "item": item("agentMessage", id="msg-1", text="", phase="final_answer")})
            notify("item/agentMessage/delta", {"threadId": thread_id, "turnId": TURN, "itemId": "msg-1", "delta": f"you chose {answer}"})
            notify("turn/completed", {"threadId": thread_id, "turn": {"id": TURN, "items": [], "itemsView": "summary", "status": "completed", "error": None, "startedAt": 1, "completedAt": 2, "durationMs": 1}})
        elif rid == 0 and ("result" in m or "error" in m):   # the approval reply
            with open(os.environ.get("FAKE_REPLY_OUT", os.devnull), "w") as f:
                json.dump(m.get("result") or {"error": m.get("error")}, f)
            decision = (m.get("result") or {}).get("decision")
            if decision in ("accept", "acceptForSession"):
                notify("item/commandExecution/outputDelta", {"threadId": thread_id, "turnId": TURN, "itemId": "call-1", "delta": "hello\n"})
                notify("item/completed", {"threadId": thread_id, "turnId": TURN, "completedAtMs": 2, "item": item(
                    "commandExecution", command="/bin/zsh -lc 'touch x.txt'", cwd="/p", status="completed",
                    commandActions=[{"type": "unknown", "command": "touch x.txt"}], aggregatedOutput="hello\n", exitCode=0, durationMs=1,
                    pluginId=None, scriptPath=None, processId="1", source="agent")})
            else:
                notify("item/completed", {"threadId": thread_id, "turnId": TURN, "completedAtMs": 2, "item": item(
                    "commandExecution", command="/bin/zsh -lc 'touch x.txt'", cwd="/p", status="declined",
                    commandActions=[{"type": "unknown", "command": "touch x.txt"}], aggregatedOutput=None, exitCode=None, durationMs=None,
                    pluginId=None, scriptPath=None, processId=None, source="agent")})
            notify("item/started", {"threadId": thread_id, "turnId": TURN, "startedAtMs": 3, "item": item("agentMessage", id="msg-1", text="", phase="final_answer")})
            notify("item/agentMessage/delta", {"threadId": thread_id, "turnId": TURN, "itemId": "msg-1", "delta": "DONE"})
            notify("item/completed", {"threadId": thread_id, "turnId": TURN, "completedAtMs": 4, "item": item("agentMessage", id="msg-1", text="DONE", phase="final_answer")})
            notify("thread/tokenUsage/updated", {"threadId": thread_id, "turnId": TURN, "tokenUsage": {
                "total": {"totalTokens": 2400, "inputTokens": 2200, "cachedInputTokens": 0, "cacheWriteInputTokens": 0, "outputTokens": 200, "reasoningOutputTokens": 0},
                "last": {"totalTokens": 2400, "inputTokens": 2200, "cachedInputTokens": 0, "cacheWriteInputTokens": 0, "outputTokens": 200, "reasoningOutputTokens": 0},
                "modelContextWindow": 240000}})
            notify("account/rateLimits/updated", {"rateLimits": {"limitId": "codex", "primary": {"usedPercent": 3, "windowDurationMins": 300, "resetsAt": 1},
                                                                  "secondary": {"usedPercent": 12, "windowDurationMins": 10080, "resetsAt": 2}}})
            notify("turn/completed", {"threadId": thread_id, "turn": {"id": TURN, "items": [], "itemsView": "summary", "status": "completed", "error": None, "startedAt": 1, "completedAt": 5, "durationMs": 4}})


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Write the failing tests**

```python
# tests/test_chat_codex.py
import json
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from tandem.chat.events import (ApprovalRequest, LimitsUpdate, QuestionRequest, TextDelta,
                                ToolFinished, ToolOutput, ToolStarted, TurnFinished)
from tandem.chat.runtime.codex import CodexRuntime, strip_shell
from tandem.config import ChatConfig

FAKE = Path(__file__).parent / "fakes" / "fake_codex_appserver.py"
GOLDEN = Path(__file__).parent / "golden" / "chat" / "codex_appserver.jsonl"


class Recorder:
    def __init__(self, approve="allow", answer="red"):
        self.events, self.approvals, self.questions = [], [], []
        self._approve, self._answer = approve, answer

    def emit(self, ev): self.events.append(ev)
    def approve(self, req): self.approvals.append(req); return self._approve
    def answer(self, req): self.questions.append(req); return self._answer
    def kinds(self): return [type(e).__name__ for e in self.events]


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_ARGV_OUT", str(tmp_path / "argv.json"))
    monkeypatch.setenv("FAKE_PARAMS_OUT", str(tmp_path / "params.jsonl"))
    monkeypatch.setenv("FAKE_REPLY_OUT", str(tmp_path / "reply.json"))
    proj = tmp_path / "proj"; proj.mkdir()

    def params(method):
        for line in (tmp_path / "params.jsonl").read_text().splitlines():
            m = json.loads(line)
            if m["method"] == method:
                return m["params"]
        return None

    return SimpleNamespace(tmp=tmp_path, session=SimpleNamespace(cwd=str(proj)), params=params,
                           runtime=CodexRuntime(ChatConfig(), binary=[sys.executable, str(FAKE)]))


def test_strip_shell_wrapper():
    assert strip_shell("/bin/zsh -lc 'echo hi'") == "echo hi"
    assert strip_shell("bash -lc \"ls -la\"") == "ls -la"
    assert strip_shell("ls -la") == "ls -la"


def test_approve_flow(env):
    rec = Recorder("allow")
    out = env.runtime.run_turn(env.session, "thread-1", "make x", "", rec.emit, rec)
    assert out.status == "completed" and out.native_id is None
    assert json.loads((env.tmp / "argv.json").read_text()) == ["app-server"]
    assert env.params("initialize")["clientInfo"]["name"] == "tandem"
    assert env.params("thread/resume") == {"threadId": "thread-1", "cwd": env.session.cwd}
    assert env.params("turn/start") == {"threadId": "thread-1", "input": [{"type": "text", "text": "make x", "text_elements": []}]}
    assert rec.approvals == [ApprovalRequest("command", "touch x.txt")]
    assert json.loads((env.tmp / "reply.json").read_text()) == {"decision": "accept"}
    assert rec.kinds() == ["ToolStarted", "ToolOutput", "ToolFinished", "TextDelta", "LimitsUpdate", "TurnFinished"]
    assert rec.events[0] == ToolStarted("call-1", "exec", "touch x.txt")
    assert rec.events[1] == ToolOutput("call-1", "hello\n")
    assert rec.events[2] == ToolFinished("call-1", True, "exit 0")
    assert rec.events[3] == TextDelta("DONE")
    assert rec.events[4] == LimitsUpdate("codex", "5h 3% 7d 12%")
    assert rec.events[5].status == "completed" and rec.events[5].usage == "1% ctx · 2200↑ 200↓"


def test_model_and_config_overrides(env):
    rt = CodexRuntime(ChatConfig(codex_approval_policy="never", codex_sandbox="read-only"),
                      binary=[sys.executable, str(FAKE)])
    rec = Recorder("allow")
    rt.run_turn(env.session, "thread-1", "go", "gpt-5.5", rec.emit, rec)
    assert env.params("thread/resume") == {"threadId": "thread-1", "cwd": env.session.cwd,
                                           "approvalPolicy": "never", "sandbox": "read-only"}
    assert env.params("turn/start")["model"] == "gpt-5.5"


def test_always_and_deny_decisions(env, monkeypatch):
    rec = Recorder("always")
    env.runtime.run_turn(env.session, "t", "go", "", rec.emit, rec)
    assert json.loads((env.tmp / "reply.json").read_text()) == {"decision": "acceptForSession"}
    rec = Recorder("deny")
    out = env.runtime.run_turn(env.session, "t", "go", "", rec.emit, rec)
    assert json.loads((env.tmp / "reply.json").read_text()) == {"decision": "decline"}
    assert out.status == "completed"
    assert ToolFinished("call-1", False, "declined") in rec.events


def test_fresh_thread_start_returns_the_new_id(env, monkeypatch):
    monkeypatch.setenv("FAKE_CODEX_SCENARIO", "fresh")
    rec = Recorder("allow")
    out = env.runtime.run_turn(env.session, None, "go", "", rec.emit, rec)
    assert out.status == "completed" and out.native_id == "thread-new"
    assert env.params("thread/start") == {"cwd": env.session.cwd}
    assert env.params("thread/resume") is None
    assert env.params("turn/start")["threadId"] == "thread-new"


def test_writer_lock_is_reported_not_retried(env, monkeypatch):
    monkeypatch.setenv("FAKE_CODEX_SCENARIO", "lock")
    rec = Recorder()
    out = env.runtime.run_turn(env.session, "t", "go", "", rec.emit, rec)
    assert out.status == "failed" and "open in another process" in out.error
    assert rec.kinds() == ["Failure", "TurnFinished"]
    assert env.params("turn/start") is None


def test_crash_is_failed_with_stderr(env, monkeypatch):
    monkeypatch.setenv("FAKE_CODEX_SCENARIO", "crash")
    rec = Recorder()
    out = env.runtime.run_turn(env.session, "t", "go", "", rec.emit, rec)
    assert out.status == "failed" and "kaboom" in out.error
    assert rec.events[-1] == TurnFinished("failed", "")


def test_interrupt(env, monkeypatch):
    monkeypatch.setenv("FAKE_CODEX_SCENARIO", "interrupt")
    rec = Recorder(); rt = env.runtime; holder = {}
    t = threading.Thread(target=lambda: holder.__setitem__("out", rt.run_turn(env.session, "t", "go", "", rec.emit, rec)))
    t.start()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not any(isinstance(e, TextDelta) for e in rec.events):
        time.sleep(0.02)
    rt.interrupt(); t.join(10)
    assert holder["out"].status == "interrupted"
    assert env.params("turn/interrupt") == {"threadId": "t", "turnId": "turn-1"}


def test_question_round_trip(env, monkeypatch):
    monkeypatch.setenv("FAKE_CODEX_SCENARIO", "question")
    rec = Recorder(answer="blue")
    out = env.runtime.run_turn(env.session, "t", "go", "", rec.emit, rec)
    assert out.status == "completed"
    assert rec.questions == [QuestionRequest("Which color?", ("red", "blue"))]
    assert TextDelta("you chose blue") in rec.events


def test_golden_lines_drive_the_handler():
    rec = Recorder("allow"); sent = []
    rt = CodexRuntime(ChatConfig())
    outcome = None
    for line in GOLDEN.read_text().splitlines():
        m = json.loads(line)
        if "method" not in m:
            continue                                   # responses are consumed by _call, not handle
        got = rt.handle(m, sent.append, rec.emit, rec)
        outcome = got or outcome
    assert outcome is not None and outcome.status == "completed"
    assert rec.approvals == [ApprovalRequest("command", "echo fixture —")]
    assert sent == [{"jsonrpc": "2.0", "id": 0, "result": {"decision": "accept"}}]
    assert rec.kinds() == ["ToolStarted", "ToolOutput", "ToolFinished", "LimitsUpdate", "TextDelta", "TurnFinished"]
    assert rec.events[0] == ToolStarted("call_vDe2mWf2XYJ1BjLDXSDWgLSo", "exec", "echo fixture —")
    assert rec.events[1] == ToolOutput("call_vDe2mWf2XYJ1BjLDXSDWgLSo", "fixture —\n")   # from aggregatedOutput, no delta streamed
    assert rec.events[3] == LimitsUpdate("codex", "5h 0% 7d 0%")
```

Run: `uv run pytest tests/test_chat_codex.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'tandem.chat.runtime.codex'`

- [ ] **Step 3: Write the runtime**

```python
# src/tandem/chat/runtime/codex.py
"""Codex app-server, one process per turn: JSON-RPC 2.0, one message per
line, over stdio (the same transport the codex Python SDK's client.py uses).

  initialize {clientInfo} -> initialized (notification)
  thread/resume {threadId, cwd[, approvalPolicy, sandbox]}   or   thread/start {cwd[, …]}
  turn/start {threadId, input:[{type:"text", text}][, model]}
  … notifications (item/*, turn/*, thread/tokenUsage/updated,
    account/rateLimits/updated) and server requests (*/requestApproval,
    item/tool/requestUserInput) … until turn/completed, then stdin closes.

The process holds the thread's writer lock until it exits, which is why a
new process is spawned per turn and why a lock error is reported, never
retried. Requests are built from the generated models; notifications and
server requests are validated by them (unknown fields ignored); the two
thread responses are read as dicts because their models carry dozens of
unrelated fields that drift between releases."""

from __future__ import annotations

import json
import queue
import re
import subprocess
import threading
from collections import deque
from typing import Callable

from ...ratelimit import Window, format_windows, window_label
from ..events import (Answers, ApprovalRequest, Failure, LimitsUpdate, LiveEvent,
                      QuestionRequest, TextDelta, ThinkingDelta, ToolFinished, ToolOutput,
                      ToolStarted, TurnFinished, TurnOutcome)
from . import child_env, first_line, terminate
from . import codex_protocol as cp

try:
    from ... import __version__ as _VERSION
except ImportError:      # pragma: no cover
    _VERSION = "0"

_SHELL_RE = re.compile(r"""^\S*(?:zsh|bash|sh)\s+-l?c\s+(['"])(.*)\1\s*$""", re.S)


def strip_shell(command: str) -> str:
    """`/bin/zsh -lc 'echo hi'` -> `echo hi`: the TUI shows the inner command."""
    m = _SHELL_RE.match(command or "")
    return m.group(2) if m else (command or "")


def item_of(notification):
    """The concrete ThreadItem variant behind a notification's `item`."""
    item = notification.item
    return getattr(item, "root", item)


def _decision(choice: str, available) -> str:
    listed = {d for d in (available or []) if isinstance(d, str)}
    if choice == "deny":
        return "decline" if "decline" in listed or not listed else "cancel"
    if choice == "always" and "acceptForSession" in listed:
        return "acceptForSession"
    return "accept"


class CodexRuntime:
    harness = "codex"

    def __init__(self, cfg, *, binary: list[str] | None = None):
        self.cfg = cfg
        self.binary = list(binary) if binary else ["codex"]
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._n = 0
        self._interrupted = False
        self._thread_id: str | None = None
        self._turn_id: str | None = None
        self._usage = ""
        self._streamed_output: set[str] = set()
        self._streamed_text: set[str] = set()

    # -- wire ----------------------------------------------------------------

    def _write(self, proc: subprocess.Popen, obj: dict) -> None:
        with self._lock:
            if proc.stdin and not proc.stdin.closed:
                proc.stdin.write(json.dumps(obj) + "\n")
                proc.stdin.flush()

    def _request(self, proc, method: str, params: dict | None) -> int:
        self._n += 1
        msg = {"jsonrpc": "2.0", "id": self._n, "method": method}
        if params is not None:
            msg["params"] = params
        self._write(proc, msg)
        return self._n

    def _call(self, proc, q: "queue.Queue", method: str, params: dict | None,
              emit, answers, timeout: float = 60.0) -> dict:
        """Send a request and wait for its response, handling everything
        else that arrives meanwhile."""
        rid = self._request(proc, method, params)
        send = lambda obj: self._write(proc, obj)
        while True:
            try:
                m = q.get(timeout=timeout)
            except queue.Empty:
                return {"error": {"message": f"{method}: no response within {timeout:.0f}s"}}
            if m is None:
                return {"error": {"message": "codex app-server exited"}}
            if m.get("id") == rid and ("result" in m or "error" in m):
                return m
            self.handle(m, send, emit, answers)

    # -- protocol ------------------------------------------------------------

    def _server_request(self, m: dict, send, answers: Answers) -> None:
        method, rid = m["method"], m["id"]
        params = m.get("params") or {}
        if method == "item/commandExecution/requestApproval":
            p = cp.CommandExecutionRequestApprovalParams.model_validate(params)
            choice = answers.approve(ApprovalRequest("command", first_line(strip_shell(p.command or ""))))
            send({"jsonrpc": "2.0", "id": rid,
                  "result": {"decision": _decision(choice, params.get("availableDecisions"))}})
        elif method == "item/fileChange/requestApproval":
            p = cp.FileChangeRequestApprovalParams.model_validate(params)
            detail = getattr(p, "reason", None) or "apply file changes"
            choice = answers.approve(ApprovalRequest("file_change", first_line(detail)))
            send({"jsonrpc": "2.0", "id": rid,
                  "result": {"decision": _decision(choice, params.get("availableDecisions"))}})
        elif method == "item/permissions/requestApproval":
            p = cp.PermissionsRequestApprovalParams.model_validate(params)
            detail = getattr(p, "reason", None) or "additional permissions"
            choice = answers.approve(ApprovalRequest("permission", first_line(detail)))
            if choice == "deny":
                send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32001, "message": "denied in tandem chat"}})
            else:
                send({"jsonrpc": "2.0", "id": rid,
                      "result": {"permissions": params.get("permissions") or {},
                                 "scope": "session" if choice == "always" else "turn"}})
        elif method == "item/tool/requestUserInput":
            p = cp.ToolRequestUserInputParams.model_validate(params)
            answered = {}
            for q in p.questions or []:
                options = tuple(str(getattr(o, "label", None) or o) for o in (q.options or []))
                answered[q.id] = {"answers": [answers.answer(QuestionRequest(q.question, options))]}
            send({"jsonrpc": "2.0", "id": rid, "result": {"answers": answered}})
        else:
            send({"jsonrpc": "2.0", "id": rid, "result": {}})

    def handle(self, m: dict, send: Callable[[dict], None], emit: Callable[[LiveEvent], None],
               answers: Answers) -> TurnOutcome | None:
        """One server line that is not the response being waited for."""
        method = m.get("method")
        if method is None:
            return None
        if "id" in m:
            self._server_request(m, send, answers)
            return None
        params = m.get("params") or {}
        if method == "item/agentMessage/delta":
            n = cp.AgentMessageDeltaNotification.model_validate(params)
            self._streamed_text.add(n.itemId)
            emit(TextDelta(n.delta))
        elif method == "item/reasoning/summaryTextDelta":
            if params.get("delta"):
                emit(ThinkingDelta(params["delta"]))
        elif method in ("item/commandExecution/outputDelta", "item/fileChange/outputDelta"):
            item_id, delta = params.get("itemId", ""), params.get("delta", "")
            self._streamed_output.add(item_id)
            emit(ToolOutput(item_id, delta))
        elif method == "item/started":
            it = item_of(cp.ItemStartedNotification.model_validate(params))
            kind = it.type
            if kind == "commandExecution":
                emit(ToolStarted(it.id, "exec", first_line(strip_shell(it.command or ""))))
            elif kind == "fileChange":
                paths = ", ".join(c.path for c in (getattr(it, "changes", None) or []))
                emit(ToolStarted(it.id, "patch", first_line(paths)))
            elif kind == "mcpToolCall":
                emit(ToolStarted(it.id, f"mcp:{getattr(it, 'server', '?')}.{getattr(it, 'tool', '?')}", ""))
            elif kind == "webSearch":
                emit(ToolStarted(it.id, "web_search", first_line(getattr(it, "query", "") or "")))
            elif kind == "contextCompaction":
                emit(ToolStarted(it.id, "compaction", "context compaction"))
        elif method == "item/completed":
            it = item_of(cp.ItemCompletedNotification.model_validate(params))
            kind = it.type
            if kind == "commandExecution":
                if it.id not in self._streamed_output and it.aggregatedOutput:
                    emit(ToolOutput(it.id, it.aggregatedOutput))
                ok = it.status == "completed"
                summary = f"exit {it.exitCode}" if it.exitCode is not None else str(it.status)
                emit(ToolFinished(it.id, ok, summary))
            elif kind == "fileChange":
                if it.id not in self._streamed_output:
                    diff = "\n".join(c.diff for c in (getattr(it, "changes", None) or []) if getattr(c, "diff", ""))
                    if diff:
                        emit(ToolOutput(it.id, diff))
                emit(ToolFinished(it.id, getattr(it, "status", "completed") == "completed", ""))
            elif kind in ("mcpToolCall", "webSearch", "contextCompaction"):
                emit(ToolFinished(it.id, getattr(it, "status", "completed") != "failed", ""))
            elif kind == "agentMessage":
                if it.id not in self._streamed_text and it.text:
                    emit(TextDelta(it.text))
        elif method == "thread/tokenUsage/updated":
            n = cp.ThreadTokenUsageUpdatedNotification.model_validate(params)
            total = n.tokenUsage.total
            parts = []
            window = getattr(n.tokenUsage, "modelContextWindow", None)
            if window:
                parts.append(f"{round(total.totalTokens * 100 / window)}% ctx")
            parts.append(f"{total.inputTokens}↑ {total.outputTokens}↓")
            self._usage = " · ".join(parts)
        elif method == "account/rateLimits/updated":
            rl = params.get("rateLimits") or {}
            windows = []
            for key in ("primary", "secondary"):
                w = rl.get(key)
                if isinstance(w, dict) and isinstance(w.get("usedPercent"), (int, float)) \
                        and isinstance(w.get("windowDurationMins"), (int, float)) and w["windowDurationMins"] > 0:
                    windows.append(Window(window_label(int(w["windowDurationMins"]) * 60), int(w["usedPercent"])))
            if windows:
                emit(LimitsUpdate("codex", format_windows(windows)))
        elif method == "error":
            err = params.get("error") or {}
            emit(Failure(str(err.get("message") or err)))
        elif method == "turn/completed":
            n = cp.TurnCompletedNotification.model_validate(params)
            raw = str(n.turn.status)
            status = "interrupted" if self._interrupted or raw == "interrupted" else (
                "failed" if raw == "failed" else "completed")
            error = ""
            if status == "failed" and n.turn.error is not None:
                error = str(getattr(n.turn.error, "message", n.turn.error))
            emit(TurnFinished(status, self._usage))
            return TurnOutcome(status, error)
        return None

    # -- process -------------------------------------------------------------

    def run_turn(self, session, native_id: str | None, prompt: str, model: str,
                 emit: Callable[[LiveEvent], None], answers: Answers) -> TurnOutcome:
        self._interrupted = False
        self._usage = ""
        self._thread_id = self._turn_id = None
        self._streamed_output.clear(); self._streamed_text.clear()
        proc = subprocess.Popen(
            [*self.binary, "app-server"], cwd=session.cwd, env=child_env(),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1, start_new_session=True,
        )
        with self._lock:
            self._proc = proc
        tail: deque[str] = deque(maxlen=20)
        threading.Thread(target=lambda: [tail.append(l.rstrip()) for l in proc.stderr],
                         name="tandem-chat-codex-stderr", daemon=True).start()
        q: queue.Queue = queue.Queue()

        def reader() -> None:
            for line in proc.stdout:
                try:
                    m = json.loads(line)
                except ValueError:
                    continue
                if isinstance(m, dict):
                    q.put(m)
            q.put(None)

        threading.Thread(target=reader, name="tandem-chat-codex-reader", daemon=True).start()
        send = lambda obj: self._write(proc, obj)
        new_id: str | None = None
        outcome: TurnOutcome | None = None

        def fail(message: str) -> TurnOutcome:
            emit(Failure(message))
            emit(TurnFinished("failed", ""))
            return TurnOutcome("failed", message)

        try:
            r = self._call(proc, q, "initialize",
                           cp.InitializeParams(clientInfo=cp.ClientInfo(name="tandem", version=_VERSION))
                           .model_dump(by_alias=True, exclude_none=True), emit, answers)
            if "error" in r:
                return fail(f"initialize failed: {r['error'].get('message', r['error'])}")
            self._write(proc, {"jsonrpc": "2.0", "method": "initialized"})
            overrides: dict = {}
            if self.cfg.codex_approval_policy:
                overrides["approvalPolicy"] = self.cfg.codex_approval_policy
            if self.cfg.codex_sandbox:
                overrides["sandbox"] = self.cfg.codex_sandbox
            if native_id:
                params = cp.ThreadResumeParams(threadId=native_id, cwd=session.cwd, **overrides)
                r = self._call(proc, q, "thread/resume", params.model_dump(by_alias=True, exclude_none=True), emit, answers)
                if "error" in r:
                    msg = str(r["error"].get("message", r["error"]))
                    if "active writer" in msg:
                        msg = "this codex thread is open in another process: " + msg
                    return fail(msg)
                thread_id = ((r.get("result") or {}).get("thread") or {}).get("id") or native_id
            else:
                params = cp.ThreadStartParams(cwd=session.cwd, **overrides)
                r = self._call(proc, q, "thread/start", params.model_dump(by_alias=True, exclude_none=True), emit, answers)
                if "error" in r:
                    return fail(str(r["error"].get("message", r["error"])))
                thread_id = ((r.get("result") or {}).get("thread") or {}).get("id")
                if not thread_id:
                    return fail("thread/start returned no thread id")
                new_id = thread_id
            self._thread_id = thread_id
            turn = cp.TurnStartParams(threadId=thread_id, input=[{"type": "text", "text": prompt}],
                                      model=model or None)
            r = self._call(proc, q, "turn/start", turn.model_dump(by_alias=True, exclude_none=True), emit, answers)
            if "error" in r:
                return fail(str(r["error"].get("message", r["error"])))
            self._turn_id = cp.TurnStartResponse.model_validate(r["result"]).turn.id
            while True:
                m = q.get()
                if m is None:
                    break
                outcome = self.handle(m, send, emit, answers)
                if outcome is not None:
                    break
        finally:
            terminate(proc, soft=lambda: proc.stdin.close(), soft_timeout=5.0)
            with self._lock:
                self._proc = None
        if outcome is None:
            status = "interrupted" if self._interrupted else "failed"
            outcome = TurnOutcome(status, error="\n".join(tail) or f"codex app-server exited {proc.returncode}")
            emit(TurnFinished(status, ""))
        outcome.native_id = new_id
        return outcome

    def interrupt(self) -> None:
        with self._lock:
            proc = self._proc
            thread_id, turn_id = self._thread_id, self._turn_id
        if proc is None or proc.poll() is not None or not (thread_id and turn_id):
            return
        self._interrupted = True
        self._request(proc, "turn/interrupt",
                      cp.TurnInterruptParams(threadId=thread_id, turnId=turn_id)
                      .model_dump(by_alias=True, exclude_none=True))

    def close(self) -> None:
        with self._lock:
            proc = self._proc
        if proc is not None:
            terminate(proc, soft=lambda: proc.stdin.close(), soft_timeout=2.0)
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_chat_codex.py -q`
Expected: 10 passed. If `TurnStartResponse.model_validate` rejects the fake's turn object, the fake is missing a field the generated model requires — copy the missing field from the golden `id: 3` line into the fake rather than loosening the model.

- [ ] **Step 5: Commit**

```bash
git add src/tandem/chat/runtime/codex.py tests/fakes/fake_codex_appserver.py tests/test_chat_codex.py
git commit -m "feat(chat): codex runtime over app-server with approvals and user input" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>" -m "Claude-Session: https://claude.ai/code/session_01WZ7WV9iKDBigenNSsJaRdy"
```

---
### Task 7: Opencode runtime (`opencode serve` over HTTP + SSE) and the runtime factory

**Files:**
- Create: `src/tandem/chat/runtime/opencode.py`, `src/tandem/chat/runtime/factory.py`, `tests/fakes/fake_opencode_server.py`, `tests/fakes/fake_opencode_serve.py`, `tests/golden/chat/opencode_events.jsonl`
- Test: `tests/test_chat_opencode.py`

**Interfaces:**
- Consumes: Task 3 helpers; `ChatConfig`; `PairedSession.participants`.
- Produces: `OpencodeRuntime(cfg, *, binary=None, base_url=None)` with `harness = "opencode"`, `ensure_server(cwd) -> str` (base URL; spawns `opencode serve` once per window unless `base_url` was injected), `handle_event(ev: dict, st: TurnState, emit, answers) -> None`, `run_turn(...)`, `interrupt()`, `close()`; `TurnState` dataclass (`session_id`, `assistant_msgs: set`, `user_msgs: set`, `part_types: dict`, `started: set`, `failed: str`); `factory.make_runtimes(session, cfg) -> dict[str, RuntimeClient]` (one instance per participant).
- Model strings are `provider/model`; anything without a slash fails the turn with a `Failure` before any request is made.
- Reply mapping: allow → `once`, always → `always`, deny → `reject` via `POST /permission/<id>/reply {"reply": …}`; questions via `POST /session/<sid>/question/<id>/reply {"answers": [[text], …]}`; interrupt via `POST /session/<sid>/abort`.

- [ ] **Step 1: Write the golden fixture**

`tests/golden/chat/opencode_events.jsonl` (opencode 1.18.20, 2026-09-13; SSE `data:` payloads, plus the permission pair observed with `permission.bash = "ask"`):

```json
{"id": "evt_1", "type": "message.updated", "properties": {"sessionID": "ses_f88b30227ffeMqqajMYXp3Bmqs", "info": {"id": "msg_user1", "role": "user", "sessionID": "ses_f88b30227ffeMqqajMYXp3Bmqs", "time": {"created": 1789310958796}, "agent": "build", "model": {"providerID": "opencode", "modelID": "big-pickle"}}}}
{"id": "evt_2", "type": "message.part.updated", "properties": {"sessionID": "ses_f88b30227ffeMqqajMYXp3Bmqs", "part": {"type": "text", "text": "Run this shell command: touch fixture-oc.txt — then reply with exactly DONE.", "messageID": "msg_user1", "sessionID": "ses_f88b30227ffeMqqajMYXp3Bmqs", "id": "prt_user1"}, "time": 1789310959028}}
{"id": "evt_3", "type": "session.status", "properties": {"sessionID": "ses_f88b30227ffeMqqajMYXp3Bmqs", "status": {"type": "busy"}}}
{"id": "evt_4", "type": "message.updated", "properties": {"sessionID": "ses_f88b30227ffeMqqajMYXp3Bmqs", "info": {"id": "msg_asst1", "parentID": "msg_user1", "role": "assistant", "mode": "build", "agent": "build", "cost": 0, "tokens": {"input": 0, "output": 0, "reasoning": 0, "cache": {"read": 0, "write": 0}}, "modelID": "big-pickle", "providerID": "opencode", "time": {"created": 1789310959041}, "sessionID": "ses_f88b30227ffeMqqajMYXp3Bmqs"}}}
{"id": "evt_5", "type": "message.part.updated", "properties": {"sessionID": "ses_f88b30227ffeMqqajMYXp3Bmqs", "part": {"id": "prt_step1", "messageID": "msg_asst1", "sessionID": "ses_f88b30227ffeMqqajMYXp3Bmqs", "type": "step-start"}, "time": 1789310963798}}
{"id": "evt_6", "type": "message.part.updated", "properties": {"sessionID": "ses_f88b30227ffeMqqajMYXp3Bmqs", "part": {"id": "prt_reason1", "messageID": "msg_asst1", "sessionID": "ses_f88b30227ffeMqqajMYXp3Bmqs", "type": "reasoning", "text": "", "time": {"start": 1789310963902}}, "time": 1789310963902}}
{"id": "evt_7", "type": "message.part.delta", "properties": {"sessionID": "ses_f88b30227ffeMqqajMYXp3Bmqs", "messageID": "msg_asst1", "partID": "prt_reason1", "field": "text", "delta": "The"}}
{"id": "evt_8", "type": "message.part.updated", "properties": {"sessionID": "ses_f88b30227ffeMqqajMYXp3Bmqs", "part": {"id": "prt_tool1", "messageID": "msg_asst1", "sessionID": "ses_f88b30227ffeMqqajMYXp3Bmqs", "type": "tool", "tool": "bash", "callID": "call_bb6afd06e9414ec8bb76f7ca", "state": {"status": "pending", "input": {}, "raw": ""}}, "time": 1789310963970}}
{"id": "evt_9", "type": "permission.asked", "properties": {"id": "per_07751394f001ki7iX2q5s6tq9A", "sessionID": "ses_f88b30227ffeMqqajMYXp3Bmqs", "permission": "bash", "patterns": ["touch fixture-oc.txt"], "metadata": {"command": "touch fixture-oc.txt"}, "always": ["*"], "tool": {"messageID": "msg_asst1", "callID": "call_bb6afd06e9414ec8bb76f7ca"}}}
{"id": "evt_10", "type": "permission.replied", "properties": {"sessionID": "ses_f88b30227ffeMqqajMYXp3Bmqs", "requestID": "per_07751394f001ki7iX2q5s6tq9A", "reply": "once"}}
{"id": "evt_11", "type": "message.part.updated", "properties": {"sessionID": "ses_f88b30227ffeMqqajMYXp3Bmqs", "part": {"type": "tool", "tool": "bash", "callID": "call_bb6afd06e9414ec8bb76f7ca", "state": {"status": "running", "input": {"command": "touch fixture-oc.txt"}, "time": {"start": 1789310964153}}, "id": "prt_tool1", "sessionID": "ses_f88b30227ffeMqqajMYXp3Bmqs", "messageID": "msg_asst1"}, "time": 1789310964153}}
{"id": "evt_12", "type": "message.part.updated", "properties": {"sessionID": "ses_f88b30227ffeMqqajMYXp3Bmqs", "part": {"type": "tool", "tool": "bash", "callID": "call_bb6afd06e9414ec8bb76f7ca", "state": {"status": "completed", "input": {"command": "touch fixture-oc.txt"}, "output": "(no output)", "metadata": {"output": "(no output)", "exit": 0, "truncated": false}, "title": "touch fixture-oc.txt", "time": {"start": 1789310964178, "end": 1789310964196}}, "id": "prt_tool1", "sessionID": "ses_f88b30227ffeMqqajMYXp3Bmqs", "messageID": "msg_asst1"}, "time": 1789310964196}}
{"id": "evt_13", "type": "message.part.updated", "properties": {"sessionID": "ses_f88b30227ffeMqqajMYXp3Bmqs", "part": {"id": "prt_text1", "messageID": "msg_asst1", "sessionID": "ses_f88b30227ffeMqqajMYXp3Bmqs", "type": "text", "text": ""}, "time": 1789310964300}}
{"id": "evt_14", "type": "message.part.delta", "properties": {"sessionID": "ses_f88b30227ffeMqqajMYXp3Bmqs", "messageID": "msg_asst1", "partID": "prt_text1", "field": "text", "delta": "DONE"}}
{"id": "evt_15", "type": "message.part.updated", "properties": {"sessionID": "ses_f88b30227ffeMqqajMYXp3Bmqs", "part": {"id": "prt_fin1", "reason": "stop", "messageID": "msg_asst1", "sessionID": "ses_f88b30227ffeMqqajMYXp3Bmqs", "type": "step-finish", "tokens": {"total": 9278, "input": 7443, "output": 43, "reasoning": 0, "cache": {"write": 0, "read": 1792}}, "cost": 0}, "time": 1789310964200}}
{"id": "evt_16", "type": "session.status", "properties": {"sessionID": "ses_f88b30227ffeMqqajMYXp3Bmqs", "status": {"type": "idle"}}}
{"id": "evt_17", "type": "session.idle", "properties": {"sessionID": "ses_f88b30227ffeMqqajMYXp3Bmqs"}}
```

- [ ] **Step 2: Write the fake server**

`tests/fakes/fake_opencode_server.py`:

```python
"""An in-process stand-in for `opencode serve`: health, the SSE event
stream, message posting, permission and question replies, abort.

Scenario (constructor arg or $FAKE_OPENCODE_SCENARIO):
  tool        (default) reasoning delta, bash tool pending/running/completed, text "DONE"
  permission  like tool but asks permission first; reject -> tool error, no text
  question    asks one question, echoes the answer as text
  abort       streams "partial" then waits for POST .../abort
  error       emits session.error and answers the POST with 500
"""
from __future__ import annotations

import json
import os
import queue
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SID = "ses_fake"


class FakeOpencode:
    def __init__(self, scenario: str | None = None, port: int = 0):
        self.scenario = scenario or os.environ.get("FAKE_OPENCODE_SCENARIO", "tool")
        self.clients: list[queue.Queue] = []
        self.replies: list[dict] = []          # permission replies received
        self.question_replies: list[dict] = []
        self.posts: list[dict] = []
        self.aborted = threading.Event()
        self._reply = threading.Event()
        self._answered = threading.Event()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _json(self, code, obj):
                body = json.dumps(obj).encode()
                self.send_response(code); self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body))); self.end_headers(); self.wfile.write(body)

            def do_GET(self):
                if self.path == "/global/health":
                    return self._json(200, {"healthy": True, "version": "fake"})
                if self.path == "/event":
                    q: queue.Queue = queue.Queue(); fake.clients.append(q)
                    self.send_response(200); self.send_header("content-type", "text/event-stream"); self.end_headers()
                    try:
                        while True:
                            ev = q.get()
                            if ev is None:
                                return
                            self.wfile.write(f"data: {json.dumps(ev)}\n\n".encode()); self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        return
                self._json(404, {"error": "no"})

            def do_POST(self):
                n = int(self.headers.get("content-length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                parts = self.path.strip("/").split("/")
                if parts[0] == "session" and parts[-1] == "message":
                    fake.posts.append(body)
                    return self._json(*fake.run_message(parts[1], body))
                if parts[0] == "permission" and parts[-1] == "reply":
                    fake.replies.append({"id": parts[1], **body}); fake._reply.set()
                    return self._json(200, True)
                if parts[0] == "session" and parts[-1] == "abort":
                    fake.aborted.set(); return self._json(200, True)
                if parts[0] == "session" and "question" in parts and parts[-1] == "reply":
                    fake.question_replies.append({"id": parts[3], **body}); fake._answered.set()
                    return self._json(200, True)
                self._json(404, {"error": "no"})

        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def push(self, typ: str, props: dict) -> None:
        ev = {"id": f"evt_{time.monotonic_ns()}", "type": typ, "properties": props}
        for q in list(self.clients):
            q.put(ev)

    def part(self, part: dict) -> None:
        self.push("message.part.updated", {"sessionID": SID, "part": {"sessionID": SID, "messageID": "msg_a", **part}})

    def run_message(self, sid: str, body: dict) -> tuple[int, dict]:
        self.push("message.updated", {"sessionID": SID, "info": {"id": "msg_u", "role": "user", "sessionID": SID}})
        self.push("session.status", {"sessionID": SID, "status": {"type": "busy"}})
        self.push("message.updated", {"sessionID": SID, "info": {"id": "msg_a", "role": "assistant", "sessionID": SID,
                                                                    "modelID": body.get("model", {}).get("modelID", "big-pickle")}})
        info = {"id": "msg_a", "role": "assistant", "sessionID": SID, "tokens": {"input": 120, "output": 7}, "cost": 0.001}
        if self.scenario == "error":
            self.push("session.error", {"sessionID": SID, "error": {"name": "ProviderError", "message": "provider down"}})
            return 500, {"error": "provider down"}
        if self.scenario == "abort":
            self.part({"id": "prt_t", "type": "text", "text": ""})
            self.push("message.part.delta", {"sessionID": SID, "messageID": "msg_a", "partID": "prt_t", "field": "text", "delta": "partial"})
            self.aborted.wait(10)
            self.push("session.status", {"sessionID": SID, "status": {"type": "idle"}})
            return 200, {"info": info, "parts": [{"type": "text", "text": "partial"}]}
        if self.scenario == "question":
            self.push("question.asked", {"id": "que_1", "sessionID": SID, "questions": [
                {"question": "Which color?", "header": "Color", "options": [{"label": "red"}, {"label": "blue"}]}]})
            self._answered.wait(10)
            answer = self.question_replies[-1]["answers"][0][0]
            self.part({"id": "prt_t", "type": "text", "text": ""})
            self.push("message.part.delta", {"sessionID": SID, "messageID": "msg_a", "partID": "prt_t", "field": "text", "delta": f"you chose {answer}"})
            return 200, {"info": info, "parts": [{"type": "text", "text": f"you chose {answer}"}]}
        self.part({"id": "prt_r", "type": "reasoning", "text": ""})
        self.push("message.part.delta", {"sessionID": SID, "messageID": "msg_a", "partID": "prt_r", "field": "text", "delta": "thinking…"})
        self.part({"id": "prt_tool", "type": "tool", "tool": "bash", "callID": "call_1", "state": {"status": "pending", "input": {}, "raw": ""}})
        if self.scenario == "permission":
            self.push("permission.asked", {"id": "per_1", "sessionID": SID, "permission": "bash", "patterns": ["touch x.txt"],
                                           "metadata": {"command": "touch x.txt"}, "always": ["*"], "tool": {"messageID": "msg_a", "callID": "call_1"}})
            self._reply.wait(10)
            self.push("permission.replied", {"sessionID": SID, "requestID": "per_1", "reply": self.replies[-1]["reply"]})
            if self.replies[-1]["reply"] == "reject":
                self.part({"id": "prt_tool", "type": "tool", "tool": "bash", "callID": "call_1",
                           "state": {"status": "error", "input": {"command": "touch x.txt"}, "error": "The user rejected permission", "time": {"start": 1, "end": 2}}})
                return 200, {"info": info, "parts": []}
        self.part({"id": "prt_tool", "type": "tool", "tool": "bash", "callID": "call_1",
                   "state": {"status": "running", "input": {"command": "touch x.txt"}, "time": {"start": 1}}})
        self.part({"id": "prt_tool", "type": "tool", "tool": "bash", "callID": "call_1",
                   "state": {"status": "completed", "input": {"command": "touch x.txt"}, "output": "ok\n", "title": "touch x.txt",
                             "metadata": {"exit": 0}, "time": {"start": 1, "end": 2}}})
        self.part({"id": "prt_t", "type": "text", "text": ""})
        self.push("message.part.delta", {"sessionID": SID, "messageID": "msg_a", "partID": "prt_t", "field": "text", "delta": "DONE"})
        self.push("session.status", {"sessionID": SID, "status": {"type": "idle"}})
        self.push("session.idle", {"sessionID": SID})
        return 200, {"info": info, "parts": [{"type": "text", "text": "DONE"}]}

    def stop(self) -> None:
        for q in list(self.clients):
            q.put(None)
        self.server.shutdown()
```

`tests/fakes/fake_opencode_serve.py` (what the spawn path launches instead of `opencode serve`):

```python
"""`opencode serve --port N --hostname H` stand-in: serves FakeOpencode on the given port until killed."""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from fake_opencode_server import FakeOpencode  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("serve")
ap.add_argument("--port", type=int, required=True)
ap.add_argument("--hostname", default="127.0.0.1")
a = ap.parse_args()
fake = FakeOpencode(port=a.port)                 # binds and serves on a daemon thread
while True:
    time.sleep(1)
```

- [ ] **Step 3: Write the failing tests**

```python
# tests/test_chat_opencode.py
import json
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from tandem.chat.events import (ApprovalRequest, Failure, QuestionRequest, TextDelta, ThinkingDelta,
                                ToolFinished, ToolOutput, ToolStarted, TurnFinished)
from tandem.chat.runtime.opencode import OpencodeRuntime, TurnState
from tandem.config import ChatConfig

sys.path.insert(0, str(Path(__file__).parent / "fakes"))
from fake_opencode_server import SID, FakeOpencode  # noqa: E402

GOLDEN = Path(__file__).parent / "golden" / "chat" / "opencode_events.jsonl"
FAKE_SERVE = Path(__file__).parent / "fakes" / "fake_opencode_serve.py"


class Recorder:
    def __init__(self, approve="allow", answer="red"):
        self.events, self.approvals, self.questions = [], [], []
        self._approve, self._answer = approve, answer

    def emit(self, ev): self.events.append(ev)
    def approve(self, req): self.approvals.append(req); return self._approve
    def answer(self, req): self.questions.append(req); return self._answer
    def kinds(self): return [type(e).__name__ for e in self.events]


@pytest.fixture
def fake():
    def make(scenario="tool"):
        f = FakeOpencode(scenario); made.append(f); return f
    made = []
    yield make
    for f in made:
        f.stop()


SESSION = SimpleNamespace(cwd="/tmp/proj")


def test_tool_turn_streams_events(fake):
    f = fake("tool"); rec = Recorder()
    rt = OpencodeRuntime(ChatConfig(), base_url=f.base_url)
    out = rt.run_turn(SESSION, SID, "make x", "opencode/big-pickle", rec.emit, rec)
    assert out.status == "completed"
    assert f.posts == [{"model": {"providerID": "opencode", "modelID": "big-pickle"}, "parts": [{"type": "text", "text": "make x"}]}]
    assert rec.kinds() == ["ThinkingDelta", "ToolStarted", "ToolOutput", "ToolFinished", "TextDelta", "TurnFinished"]
    assert rec.events[0] == ThinkingDelta("thinking…")
    assert rec.events[1] == ToolStarted("call_1", "bash", "touch x.txt")
    assert rec.events[2] == ToolOutput("call_1", "ok\n")
    assert rec.events[3] == ToolFinished("call_1", True, "touch x.txt")
    assert rec.events[4] == TextDelta("DONE")
    assert rec.events[5] == TurnFinished("completed", "120↑ 7↓ · $0.0010")
    rt.close()


def test_no_model_pin_posts_without_model(fake):
    f = fake("tool"); rec = Recorder()
    OpencodeRuntime(ChatConfig(), base_url=f.base_url).run_turn(SESSION, SID, "hi", "", rec.emit, rec)
    assert "model" not in f.posts[0]


def test_model_without_provider_fails_before_posting(fake):
    f = fake("tool"); rec = Recorder()
    out = OpencodeRuntime(ChatConfig(), base_url=f.base_url).run_turn(SESSION, SID, "hi", "big-pickle", rec.emit, rec)
    assert out.status == "failed" and f.posts == []
    assert rec.kinds() == ["Failure", "TurnFinished"]


@pytest.mark.parametrize("choice,reply", [("allow", "once"), ("always", "always")])
def test_permission_allow_variants(fake, choice, reply):
    f = fake("permission"); rec = Recorder(choice)
    out = OpencodeRuntime(ChatConfig(), base_url=f.base_url).run_turn(SESSION, SID, "make x", "", rec.emit, rec)
    assert out.status == "completed"
    assert rec.approvals == [ApprovalRequest("permission", "bash: touch x.txt")]
    assert f.replies == [{"id": "per_1", "reply": reply}]
    assert TextDelta("DONE") in rec.events


def test_permission_deny(fake):
    f = fake("permission"); rec = Recorder("deny")
    out = OpencodeRuntime(ChatConfig(), base_url=f.base_url).run_turn(SESSION, SID, "make x", "", rec.emit, rec)
    assert out.status == "completed"
    assert f.replies == [{"id": "per_1", "reply": "reject"}]
    assert ToolFinished("call_1", False, "The user rejected permission") in rec.events


def test_question_round_trip(fake):
    f = fake("question"); rec = Recorder(answer="blue")
    out = OpencodeRuntime(ChatConfig(), base_url=f.base_url).run_turn(SESSION, SID, "ask", "", rec.emit, rec)
    assert out.status == "completed"
    assert rec.questions == [QuestionRequest("Which color?", ("red", "blue"))]
    assert f.question_replies == [{"id": "que_1", "answers": [["blue"]]}]
    assert TextDelta("you chose blue") in rec.events


def test_interrupt_aborts(fake):
    f = fake("abort"); rec = Recorder(); rt = OpencodeRuntime(ChatConfig(), base_url=f.base_url); holder = {}
    t = threading.Thread(target=lambda: holder.__setitem__("out", rt.run_turn(SESSION, SID, "go", "", rec.emit, rec))); t.start()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not any(isinstance(e, TextDelta) for e in rec.events):
        time.sleep(0.02)
    rt.interrupt(); t.join(10)
    assert f.aborted.is_set() and holder["out"].status == "interrupted"


def test_session_error_fails_the_turn(fake):
    f = fake("error"); rec = Recorder()
    out = OpencodeRuntime(ChatConfig(), base_url=f.base_url).run_turn(SESSION, SID, "go", "", rec.emit, rec)
    assert out.status == "failed" and "provider down" in out.error
    assert any(isinstance(e, Failure) for e in rec.events)


def test_events_for_other_sessions_are_ignored(fake):
    rec = Recorder(); rt = OpencodeRuntime(ChatConfig(), base_url="http://127.0.0.1:1")
    st = TurnState(session_id="ses_mine")
    rt.handle_event({"type": "message.part.delta", "properties": {"sessionID": "ses_other", "messageID": "m", "partID": "p", "field": "text", "delta": "x"}}, st, rec.emit, rec)
    assert rec.events == []


def test_golden_events_drive_the_handler(fake):
    rec = Recorder("allow"); rt = OpencodeRuntime(ChatConfig(), base_url="http://127.0.0.1:1")
    st = TurnState(session_id="ses_f88b30227ffeMqqajMYXp3Bmqs")
    rt._reply_permission = lambda pid, reply: rec.approvals.append(("reply", pid, reply))   # no server behind the golden lines
    for line in GOLDEN.read_text().splitlines():
        rt.handle_event(json.loads(line), st, rec.emit, rec)
    assert rec.kinds() == ["ThinkingDelta", "ToolStarted", "ToolOutput", "ToolFinished", "TextDelta"]
    assert rec.events[0] == ThinkingDelta("The")
    assert rec.events[1] == ToolStarted("call_bb6afd06e9414ec8bb76f7ca", "bash", "touch fixture-oc.txt")
    assert rec.events[3] == ToolFinished("call_bb6afd06e9414ec8bb76f7ca", True, "touch fixture-oc.txt")
    assert rec.approvals[0] == ApprovalRequest("permission", "bash: touch fixture-oc.txt")
    assert rec.approvals[1] == ("reply", "per_07751394f001ki7iX2q5s6tq9A", "once")


def test_ensure_server_spawns_and_polls_health(tmp_path):
    rt = OpencodeRuntime(ChatConfig(), binary=[sys.executable, str(FAKE_SERVE)])
    base = rt.ensure_server(str(tmp_path))
    assert base.startswith("http://127.0.0.1:")
    assert rt.ensure_server(str(tmp_path)) == base       # idempotent while alive
    rec = Recorder()
    out = rt.run_turn(SESSION, SID, "make x", "", rec.emit, rec)
    assert out.status == "completed"
    rt.close()
    assert rt._proc is None or rt._proc.poll() is not None


def test_factory_builds_one_runtime_per_participant():
    from tandem.chat.runtime.factory import make_runtimes
    session = SimpleNamespace(participants=["claude", "codex", "opencode"], cwd="/p")
    rts = make_runtimes(session, ChatConfig())
    assert sorted(rts) == ["claude", "codex", "opencode"]
    assert all(rts[h].harness == h for h in rts)
```

Run: `uv run pytest tests/test_chat_opencode.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'tandem.chat.runtime.opencode'`

- [ ] **Step 4: Write the runtime and the factory**

```python
# src/tandem/chat/runtime/opencode.py
"""opencode headless: one `opencode serve` per window, driven over HTTP.

  GET  /global/health                          readiness poll after spawn
  GET  /event                                  SSE: {"type","properties"} per `data:` line
  POST /session/<sid>/message {model?, parts}  blocks until the turn ends; the response
                                               is the assistant message (info.tokens, cost)
  POST /permission/<id>/reply {"reply": once|always|reject}
  POST /session/<sid>/question/<id>/reply {"answers": [[text], …]}
  POST /session/<sid>/abort

Events consumed (opencode 1.18.20, tests/golden/chat/opencode_events.jsonl):
message.updated (learn assistant/user message ids), message.part.updated
(tool parts: pending → running → completed | error; text/reasoning parts:
learn part types), message.part.delta (field "text"), permission.asked,
question.asked, session.error. The POST returning is the turn boundary.
Default opencode config auto-allows bash; a permission event only appears
when the user's opencode config asks — opencode's rule, not tandem's."""

from __future__ import annotations

import http.client
import json
import queue
import socket
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import urlparse

from ..events import (Answers, ApprovalRequest, Failure, LiveEvent, QuestionRequest,
                      TextDelta, ThinkingDelta, ToolFinished, ToolOutput, ToolStarted,
                      TurnFinished, TurnOutcome)
from . import child_env, first_line, summarize_args, terminate

_REPLY = {"allow": "once", "always": "always", "deny": "reject"}


@dataclass
class TurnState:
    session_id: str
    assistant_msgs: set = field(default_factory=set)
    user_msgs: set = field(default_factory=set)
    part_types: dict = field(default_factory=dict)     # part id -> "text" | "reasoning" | "tool"
    started: set = field(default_factory=set)           # tool call ids already announced
    failed: str = ""


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class OpencodeRuntime:
    harness = "opencode"

    def __init__(self, cfg, *, binary: list[str] | None = None, base_url: str | None = None):
        self.cfg = cfg
        self.binary = list(binary) if binary else ["opencode"]
        self.base_url = base_url
        self._injected = base_url is not None
        self._proc: subprocess.Popen | None = None
        self._stderr: deque[str] = deque(maxlen=20)
        self._events: queue.Queue = queue.Queue()
        self._sse_started = False
        self._session_id: str | None = None
        self._interrupted = False
        self._lock = threading.Lock()

    # -- server lifecycle ------------------------------------------------------

    def _healthy(self) -> bool:
        try:
            return bool((self._http("GET", "/global/health", timeout=2) or {}).get("healthy"))
        except Exception:
            return False

    def ensure_server(self, cwd: str) -> str:
        if self._injected:
            return self.base_url
        with self._lock:
            if self._proc is not None and self._proc.poll() is None and self.base_url:
                return self.base_url
            port = _free_port()
            self.base_url = f"http://127.0.0.1:{port}"
            self._proc = subprocess.Popen(
                [*self.binary, "serve", "--port", str(port), "--hostname", "127.0.0.1"],
                cwd=cwd, env=child_env(), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE, text=True, start_new_session=True,
            )
            proc = self._proc
            self._sse_started = False
        threading.Thread(target=lambda: [self._stderr.append(l.rstrip()) for l in proc.stderr],
                         name="tandem-chat-opencode-stderr", daemon=True).start()
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise RuntimeError("opencode serve exited: " + "\n".join(self._stderr))
            if self._healthy():
                return self.base_url
            time.sleep(0.1)
        raise RuntimeError("opencode serve did not become healthy within 10s")

    def _http(self, method: str, path: str, body=None, timeout: float = 600.0):
        u = urlparse(self.base_url)
        conn = http.client.HTTPConnection(u.hostname, u.port, timeout=timeout)
        try:
            data = json.dumps(body).encode() if body is not None else None
            conn.request(method, path, body=data, headers={"content-type": "application/json"})
            resp = conn.getresponse()
            raw = resp.read()
            if resp.status >= 400:
                raise RuntimeError(f"{method} {path} -> {resp.status}: {raw.decode(errors='replace')[:200]}")
            return json.loads(raw) if raw else None
        finally:
            conn.close()

    def _start_sse(self) -> None:
        if self._sse_started:
            return
        self._sse_started = True
        base = self.base_url

        def reader() -> None:
            u = urlparse(base)
            while self.base_url == base:
                try:
                    conn = http.client.HTTPConnection(u.hostname, u.port, timeout=None)
                    conn.request("GET", "/event")
                    resp = conn.getresponse()
                    for raw in resp:
                        line = raw.decode(errors="replace").rstrip("\n")
                        if line.startswith("data:"):
                            try:
                                self._events.put(json.loads(line[5:].strip()))
                            except ValueError:
                                continue
                except OSError:
                    time.sleep(0.5)      # server restarting or gone: retry while this base is current

        threading.Thread(target=reader, name="tandem-chat-opencode-sse", daemon=True).start()

    # -- replies ---------------------------------------------------------------

    def _reply_permission(self, pid: str, reply: str) -> None:
        self._http("POST", f"/permission/{pid}/reply", {"reply": reply}, timeout=30)

    def _reply_question(self, sid: str, qid: str, answers: list[list[str]]) -> None:
        self._http("POST", f"/session/{sid}/question/{qid}/reply", {"answers": answers}, timeout=30)

    # -- protocol --------------------------------------------------------------

    def handle_event(self, ev: dict, st: TurnState, emit: Callable[[LiveEvent], None],
                     answers: Answers) -> None:
        typ = ev.get("type")
        pr = ev.get("properties") or {}
        sid = st.session_id
        if typ == "message.updated":
            info = pr.get("info") or {}
            if info.get("sessionID") != sid:
                return
            (st.assistant_msgs if info.get("role") == "assistant" else st.user_msgs).add(info.get("id"))
        elif typ == "message.part.updated":
            part = pr.get("part") or {}
            if part.get("sessionID") != sid or part.get("messageID") in st.user_msgs:
                return
            ptype = part.get("type")
            if ptype in ("text", "reasoning"):
                st.part_types[part.get("id")] = ptype
            elif ptype == "tool":
                call_id, tool = part.get("callID", ""), part.get("tool", "")
                state = part.get("state") or {}
                status = state.get("status")
                if status in ("running", "completed", "error") and call_id not in st.started:
                    st.started.add(call_id)
                    emit(ToolStarted(call_id, tool, summarize_args(tool, state.get("input"))))
                if status == "completed":
                    if state.get("output"):
                        emit(ToolOutput(call_id, state["output"]))
                    emit(ToolFinished(call_id, True, first_line(state.get("title") or "")))
                elif status == "error":
                    emit(ToolFinished(call_id, False, first_line(state.get("error") or "error")))
        elif typ == "message.part.delta":
            if pr.get("sessionID") != sid or pr.get("field") != "text" or pr.get("messageID") in st.user_msgs:
                return
            delta = pr.get("delta", "")
            if st.part_types.get(pr.get("partID")) == "reasoning":
                emit(ThinkingDelta(delta))
            else:
                emit(TextDelta(delta))
        elif typ == "permission.asked":
            if pr.get("sessionID") != sid:
                return
            detail = f"{pr.get('permission', '')}: {', '.join(pr.get('patterns') or [])}".strip(": ")
            choice = answers.approve(ApprovalRequest("permission", first_line(detail)))
            self._reply_permission(pr.get("id", ""), _REPLY.get(choice, "reject"))
        elif typ == "question.asked":
            if pr.get("sessionID") != sid:
                return
            replies = []
            for q in pr.get("questions") or []:
                options = tuple(o.get("label", "") for o in (q.get("options") or []) if isinstance(o, dict))
                replies.append([answers.answer(QuestionRequest(q.get("question", ""), options))])
            self._reply_question(sid, pr.get("id", ""), replies)
        elif typ == "session.error":
            if pr.get("sessionID") != sid:
                return
            err = pr.get("error") or {}
            st.failed = str(err.get("message") or err)
            emit(Failure(st.failed))

    # -- turn ------------------------------------------------------------------

    def run_turn(self, session, native_id: str | None, prompt: str, model: str,
                 emit: Callable[[LiveEvent], None], answers: Answers) -> TurnOutcome:
        assert native_id, "opencode sessions are created at pair time"
        body: dict = {"parts": [{"type": "text", "text": prompt}]}
        if model:
            if "/" not in model:
                msg = f"opencode models are spelled provider/model, got {model!r}"
                emit(Failure(msg)); emit(TurnFinished("failed", ""))
                return TurnOutcome("failed", msg)
            provider, model_id = model.split("/", 1)
            body["model"] = {"providerID": provider, "modelID": model_id}
        try:
            self.ensure_server(session.cwd)
        except RuntimeError as exc:
            emit(Failure(str(exc))); emit(TurnFinished("failed", ""))
            return TurnOutcome("failed", str(exc))
        self._start_sse()
        while not self._events.empty():         # stale events from an earlier turn
            self._events.get_nowait()
        self._interrupted = False
        self._session_id = native_id
        st = TurnState(session_id=native_id)
        done: dict = {}

        def post() -> None:
            try:
                done["response"] = self._http("POST", f"/session/{native_id}/message", body, timeout=3600)
            except Exception as exc:
                done["error"] = str(exc)

        threading.Thread(target=post, name="tandem-chat-opencode-post", daemon=True).start()
        while "response" not in done and "error" not in done:
            try:
                ev = self._events.get(timeout=0.25)
            except queue.Empty:
                continue
            self.handle_event(ev, st, emit, answers)
        deadline = time.monotonic() + 0.3         # trailing events still in flight on the SSE thread
        while time.monotonic() < deadline:
            try:
                self.handle_event(self._events.get(timeout=0.05), st, emit, answers)
            except queue.Empty:
                continue
        usage = ""
        resp = done.get("response")
        if isinstance(resp, dict):
            info = resp.get("info") or {}
            tokens = info.get("tokens") or {}
            if isinstance(tokens.get("input"), int) and isinstance(tokens.get("output"), int):
                usage = f"{tokens['input']}↑ {tokens['output']}↓"
            if isinstance(info.get("cost"), (int, float)):
                usage += f" · ${info['cost']:.4f}"
        if self._interrupted:
            status, error = "interrupted", ""
        elif "error" in done or st.failed:
            status, error = "failed", st.failed or done.get("error", "")
        else:
            status, error = "completed", ""
        emit(TurnFinished(status, usage))
        return TurnOutcome(status, error)

    def interrupt(self) -> None:
        sid = self._session_id
        if not sid or not self.base_url:
            return
        self._interrupted = True
        try:
            self._http("POST", f"/session/{sid}/abort", {}, timeout=10)
        except Exception:
            pass

    def close(self) -> None:
        with self._lock:
            proc, self._proc = self._proc, None
            self.base_url = None if not self._injected else self.base_url
        if proc is not None:
            terminate(proc, soft_timeout=0.0, term_timeout=3.0)
```

`src/tandem/chat/runtime/factory.py`:

```python
"""Build the window's runtime clients, one per participant."""

from __future__ import annotations

from .claude import ClaudeRuntime
from .codex import CodexRuntime
from .opencode import OpencodeRuntime

_CLASSES = {"claude": ClaudeRuntime, "codex": CodexRuntime, "opencode": OpencodeRuntime}


def make_runtimes(session, cfg) -> dict:
    return {h: _CLASSES[h](cfg) for h in session.participants if h in _CLASSES}
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/test_chat_opencode.py -q`
Expected: 13 passed

- [ ] **Step 6: Commit**

```bash
git add src/tandem/chat/runtime/opencode.py src/tandem/chat/runtime/factory.py tests/fakes/fake_opencode_server.py tests/fakes/fake_opencode_serve.py tests/golden/chat/opencode_events.jsonl tests/test_chat_opencode.py
git commit -m "feat(chat): opencode runtime over serve with SSE, permissions, questions" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>" -m "Claude-Session: https://claude.ai/code/session_01WZ7WV9iKDBigenNSsJaRdy"
```

---
### Task 8: Turn bookkeeping in `ops` and the dispatcher

**Files:**
- Modify: `src/tandem/ops.py` (`run_oneoff` at lines 205-269 splits into `prepare_turn`, `adopt_native_id`, `sync_after_turn`)
- Create: `src/tandem/chat/dispatch.py`
- Test: `tests/test_chat_dispatch.py`; the existing `tests/test_ops.py::TestOneOff` must keep passing

**Interfaces:**
- Produces (`ops`): `prepare_turn(store, session, target) -> None` (drain the active side with `flush_dangling`, then fast-forward the target's outgoing cursors when it is a shadow with a transcript); `adopt_native_id(store, session, harness, native_id) -> PairedSession` (record a freshly minted id and zero the harness's outgoing cursors; returns the refreshed session); `sync_after_turn(store, session, target) -> None` (the echo-suppressed outward drain that `run_oneoff` did after its subprocess).
- Produces (`chat/dispatch.py`): `Pending(harness, model, prompt)`; `Dispatcher(store, session, runtimes, emit, answers, *, meters=None)` with `default` property (`session.active`), `busy` property, `submit(text) -> str` (a note for the renderer: `""` when the turn started, `"queued → codex"`, `"default → codex[ · model]"`, or `"error: …"`), `pump()` (start the next queued turn; the window calls it on `Idle`), `interrupt()`, `close()`. Every turn: `TurnStarted` → runtime events → (`TurnFinished` from the runtime) → post-turn sync → `Idle`. On a validation failure the dispatcher itself emits `Failure` and `TurnFinished("failed")` and never calls the runtime.
- Consumes: Task 1 grammar, Task 2 pins, Task 3 events, `RuntimeClient.run_turn`, `harness.get_adapter(...).validate_transcript`, `sync.SyncSetupError`, `runner.UsageFeed.poll` for `meters`.
- The spec's same-thread guard holds by construction: every runtime's `run_turn` returns only after `terminate()` ran on its process, so no codex process the dispatcher started can still hold the thread when the next turn resumes it; a lock held by a process tandem did not start surfaces as the runtime's "open in another process" failure.

- [ ] **Step 1: Refactor `run_oneoff` into three named steps (no behavior change)**

Replace the body of `run_oneoff` in `src/tandem/ops.py` so it reads:

```python
def prepare_turn(store: StateStore, session: PairedSession, target: str) -> None:
    """Before a turn on `target`: catch the active side up, then mark the
    target's whole file as known so only the new turn flows back afterwards.
    (When target IS the active side there is nothing to fast-forward — its
    cursor is live.)"""
    drain_source(store, session, session.active, flush_dangling=True)
    if (target != session.active and session.native_id(target)
            and source_transcript(session, target) is not None):
        fast_forward_all(store, session, target)


def adopt_native_id(store: StateStore, session: PairedSession, harness: str,
                    native_id: str) -> PairedSession:
    """A harness minted its own session id during a turn (codex on its first
    run). Record it and start every outgoing cursor of that harness at zero
    so the whole new file is translated on the next drain."""
    store.set_native_session_id(session.tandem_id, harness, native_id)
    session = store.get_session(session.tandem_id) or session
    for other in session.targets_for(harness):
        cursor = store.get_cursor(session.tandem_id, harness, other)
        cursor.byte_offset = 0
        cursor.line_index = 0
        store.save_cursor(cursor)
    return session


def sync_after_turn(store: StateStore, session: PairedSession, target: str) -> None:
    """After a turn on `target`: translate it into every other participant
    with echo suppression (see the module docstring). Each recipient that was
    fully synced before the drain fast-forwards its own outgoing cursors past
    the copy — otherwise its next drain translates them straight back,
    duplicating call ids and text. A recipient with an unsynced tail (a
    concurrent writer) is left alone so a live turn is never swallowed."""
    echo_pre: dict[str, tuple[int | None, dict[str, int]]] = {}
    for side in session.targets_for(target):
        size = _file_size(source_transcript(session, side))
        offsets = {
            t: store.get_cursor(session.tandem_id, side, t).byte_offset
            for t in session.targets_for(side)
        }
        echo_pre[side] = (size, offsets)

    drain_source(store, session, target, flush_dangling=True)

    for side, (pre_size, offsets) in echo_pre.items():
        if pre_size is None:
            continue
        if all(off == pre_size for off in offsets.values()):
            fast_forward_all(store, session, side)


def run_oneoff(
    store: StateStore, session: PairedSession, target: str, prompt: str
) -> int:
    """Route one prompt to `target` as a single native non-interactive turn,
    then sync that turn into the other file. Exactly one model (target's) is
    invoked."""
    adapter = get_adapter(target)
    sid = session.native_id(target)

    prepare_turn(store, session, target)

    started = time.time()
    if target == "codex" and not sid:
        # codex never ran: no session to resume; a fresh exec creates one.
        argv = [adapter.binary, "exec", "--skip-git-repo-check", prompt]
    else:
        argv = adapter.oneoff_argv(sid, prompt)
    code = _run(argv, cwd=session.cwd).returncode

    if target == "codex" and not sid:
        rollout = await_codex_rollout(session.cwd, started, timeout=10)
        if rollout:
            new_sid = paths.codex_rollout_session_id(rollout)
            if new_sid:
                session = adopt_native_id(store, session, "codex", new_sid)

    sync_after_turn(store, session, target)
    return code
```

Run: `uv run pytest tests/test_ops.py -q`
Expected: all pass (the one-off tests exercise both halves unchanged).

- [ ] **Step 2: Write the failing dispatcher tests**

```python
# tests/test_chat_dispatch.py
import json
import threading
import time

import pytest
from conftest import claude_assistant, claude_user, codex_turn, write_line

from tandem.chat.dispatch import Dispatcher
from tandem.chat.events import Failure, Idle, TextDelta, TurnFinished, TurnOutcome, TurnStarted
from tandem.util import read_jsonl


class FakeRuntime:
    """Emits one delta, appends a native turn to the harness's own file (so
    sync has something to translate), and returns the scripted outcome."""

    def __init__(self, harness, env, *, block=None, fresh_id=None):
        self.harness = harness
        self.env = env
        self.calls = []
        self.block = block
        self.fresh_id = fresh_id
        self.interrupts = 0

    def run_turn(self, session, native_id, prompt, model, emit, answers):
        self.calls.append((native_id, prompt, model))
        if self.block is not None:
            self.block.wait(5)
        emit(TextDelta(f"{self.harness} says hi"))
        if self.harness == "claude":
            write_line(self.env.claude_shadow, claude_user(prompt, uuid=f"u-{len(self.calls)}"))
            write_line(self.env.claude_shadow, claude_assistant([{"type": "text", "text": f"claude did {prompt}"}], uuid=f"a-{len(self.calls)}"))
        elif self.harness == "codex" and native_id:
            for obj in codex_turn(prompt, f"codex did {prompt}"):
                write_line(self.env.codex_shadow, obj)
        emit(TurnFinished("completed", "1 turn"))
        return TurnOutcome("completed", native_id=self.fresh_id if native_id is None else None)

    def interrupt(self):
        self.interrupts += 1
        if self.block is not None:
            self.block.set()

    def close(self):
        pass


class Answers:
    def approve(self, req): return "allow"
    def answer(self, req): return ""


def wait_idle(events, count=1, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if sum(isinstance(e, Idle) for e in events) >= count:
            return
        time.sleep(0.01)
    raise AssertionError(f"no Idle #{count} within {timeout}s: {[type(e).__name__ for e in events]}")


@pytest.fixture
def setup(env_factory):
    env = env_factory(active="claude")
    events = []
    runtimes = {"claude": FakeRuntime("claude", env), "codex": FakeRuntime("codex", env)}
    d = Dispatcher(env.store, env.session, runtimes, events.append, Answers())
    return env, d, runtimes, events


def test_plain_prompt_runs_on_the_default_and_syncs_outward(setup):
    env, d, rts, events = setup
    assert d.submit("hello there") == ""
    wait_idle(events)
    assert rts["claude"].calls == [(env.session.native_id("claude"), "hello there", "")]
    assert events[0] == TurnStarted("claude", "", "hello there")
    assert [type(e).__name__ for e in events] == ["TurnStarted", "TextDelta", "TurnFinished", "Idle"]
    contents = [json.dumps(e) for e in read_jsonl(env.codex_shadow)]
    assert any("[via claude-code] claude did hello there" in c for c in contents)
    assert env.store.get_session(env.session.tandem_id).active == "claude"


def test_route_runs_there_and_becomes_the_default(setup):
    env, d, rts, events = setup
    assert d.submit("/codex review it") == ""
    wait_idle(events)
    assert rts["codex"].calls == [(env.session.native_id("codex"), "review it", "")]
    assert env.store.get_session(env.session.tandem_id).active == "codex"
    contents = [json.dumps(e) for e in read_jsonl(env.claude_shadow)]
    assert any("[via codex] codex did review it" in c for c in contents)
    d.submit("and again")
    wait_idle(events, 2)
    assert rts["codex"].calls[-1] == (env.session.native_id("codex"), "and again", "")
    assert rts["claude"].calls == []


def test_bare_route_switches_the_default_without_a_turn(setup):
    env, d, rts, events = setup
    assert d.submit("/codex") == "default → codex"
    assert d.default == "codex" and not d.busy
    assert rts["codex"].calls == [] and events == []


def test_model_pin_is_sticky_per_harness(setup, monkeypatch):
    from tandem import promptroute
    monkeypatch.setattr(promptroute.modelcat, "load_catalog", lambda: None)
    env, d, rts, events = setup
    d.submit("/codex:gpt-5.5 go"); wait_idle(events, 1)
    d.submit("/codex again"); wait_idle(events, 2)
    d.submit("/claude:haiku hi"); wait_idle(events, 3)
    d.submit("/codex:default last"); wait_idle(events, 4)
    assert [c[2] for c in rts["codex"].calls] == ["gpt-5.5", "gpt-5.5", ""]
    assert rts["claude"].calls[0][2] == "haiku"
    assert d.submit("/codex") == "default → codex"
    assert d.submit("/claude") == "default → claude · haiku"


def test_prompts_queue_while_busy(setup):
    env, d, rts, events = setup
    gate = threading.Event()
    rts["claude"].block = gate
    assert d.submit("first") == ""
    assert d.submit("/codex second") == "queued → codex"
    assert rts["codex"].calls == []
    gate.set()
    wait_idle(events, 1)
    d.pump()
    wait_idle(events, 2)
    assert rts["codex"].calls == [(env.session.native_id("codex"), "second", "")]
    assert d.default == "codex"


def test_route_error_is_a_note_and_runs_nothing(setup):
    env, d, rts, events = setup
    note = d.submit("/opencode do it")
    assert note.startswith("error: opencode is not a participant")
    assert not d.busy and events == []


def test_invalid_transcript_fails_before_the_runtime(setup):
    env, d, rts, events = setup
    env.claude_shadow.write_text("{not json\n")
    d.submit("hello")
    wait_idle(events)
    assert rts["claude"].calls == []
    kinds = [type(e).__name__ for e in events]
    assert kinds == ["TurnStarted", "Failure", "TurnFinished", "Idle"]
    assert events[2] == TurnFinished("failed", "")


def test_fresh_codex_id_is_adopted(env_factory):
    env = env_factory(active="claude")
    session = env.store.create_session(env.cwd, "claude", ["claude", "codex"],
                                       {"claude": env.session.native_id("claude"), "codex": None})
    events = []
    rts = {"claude": FakeRuntime("claude", env), "codex": FakeRuntime("codex", env, fresh_id="thread-new")}
    d = Dispatcher(env.store, session, rts, events.append, Answers())
    d.submit("/codex start")
    wait_idle(events)
    assert rts["codex"].calls == [(None, "start", "")]
    assert env.store.get_session(session.tandem_id).native_id("codex") == "thread-new"
    assert env.store.get_cursor(session.tandem_id, "codex", "claude").byte_offset == 0


def test_interrupt_reaches_the_running_runtime(setup):
    env, d, rts, events = setup
    gate = threading.Event()
    rts["claude"].block = gate
    d.submit("slow")
    time.sleep(0.05)
    d.interrupt()
    wait_idle(events)
    assert rts["claude"].interrupts == 1
```

Run: `uv run pytest tests/test_chat_dispatch.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'tandem.chat.dispatch'`

- [ ] **Step 3: Write the dispatcher**

```python
# src/tandem/chat/dispatch.py
"""One turn at a time, on whichever harness the prompt names or the last
turn ran on. The pipeline is the one-off run's bookkeeping with a streaming
runner in the middle:

  parse route → (bare route: set default, done) → queue if busy →
  validate target transcript → ops.prepare_turn → runtime.run_turn →
  adopt a freshly minted id → target becomes the default → ops.sync_after_turn
  → feed the usage meter → Idle

The worker thread emits every event through `emit`; the window drains
them on the main thread and calls pump() on Idle."""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass
from typing import Callable

from .. import ops
from ..harness import get_adapter
from ..promptroute import RouteError, parse_route
from ..sync import SyncSetupError
from .events import Answers, Failure, Idle, LiveEvent, TurnFinished, TurnStarted


@dataclass(frozen=True)
class Pending:
    harness: str
    model: str
    prompt: str


class Dispatcher:
    def __init__(self, store, session, runtimes: dict, emit: Callable[[LiveEvent], None],
                 answers: Answers, *, meters: dict | None = None):
        self.store = store
        self.session = session
        self.runtimes = runtimes
        self.emit = emit
        self.answers = answers
        self.meters = meters or {}
        self.queue: deque[Pending] = deque()
        self._thread: threading.Thread | None = None
        self._current: str | None = None

    @property
    def default(self) -> str:
        return self.session.active

    @property
    def busy(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def pin(self, harness: str) -> str:
        return self.store.get_pin(self.session.tandem_id, harness)

    # -- input ---------------------------------------------------------------

    def submit(self, text: str) -> str:
        text = text.strip()
        if not text:
            return ""
        try:
            got = parse_route(text, self.session.participants)
        except RouteError as exc:
            return f"error: {exc}"
        if got is None:
            harness, prompt = self.default, text
        else:
            route, prompt = got
            harness = route.harness
            if route.model is not None:
                self.store.set_pin(self.session.tandem_id, harness, route.model)
            if not prompt:
                self._set_default(harness)
                model = self.pin(harness)
                return f"default → {harness}" + (f" · {model}" if model else "")
        item = Pending(harness, self.pin(harness), prompt)
        if self.busy:
            self.queue.append(item)
            return f"queued → {harness}"
        self._start(item)
        return ""

    def pump(self) -> None:
        if not self.busy and self.queue:
            self._start(self.queue.popleft())

    def interrupt(self) -> None:
        current = self._current
        if current is not None:
            self.runtimes[current].interrupt()

    def close(self) -> None:
        self.queue.clear()
        for rt in self.runtimes.values():
            try:
                rt.close()
            except Exception:
                pass

    # -- the turn ------------------------------------------------------------

    def _set_default(self, harness: str) -> None:
        if harness != self.session.active:
            self.store.set_active(self.session.tandem_id, harness)
        self.session = self.store.get_session(self.session.tandem_id) or self.session

    def _validate(self, harness: str) -> list[str]:
        sid = self.session.native_id(harness)
        if not sid:
            return []
        adapter = get_adapter(harness)
        path = adapter.transcript_path(self.session.cwd, sid)
        if path is None:
            return []
        try:
            return adapter.validate_transcript(path, sid)
        except Exception as exc:                       # validation must never take the window down
            return [f"validation error: {exc}"]

    def _start(self, item: Pending) -> None:
        self._current = item.harness
        self._thread = threading.Thread(target=self._run, args=(item,),
                                        name="tandem-chat-turn", daemon=True)
        self._thread.start()

    def _run(self, item: Pending) -> None:
        harness = item.harness
        self.emit(TurnStarted(harness, item.model, item.prompt))
        try:
            problems = self._validate(harness)
            if problems:
                self.emit(Failure(f"{harness} transcript: " + "; ".join(problems)))
                self.emit(TurnFinished("failed", ""))
                return
            session = self.session
            if session.native_id(harness):
                ops.prepare_turn(self.store, session, harness)
            # else: nothing to fast-forward and no file to drain into yet — the
            # first turn on a never-run codex starts context-less, as `tandem run
            # --on codex` does, and sync_after_turn translates it outward once
            # its thread id is adopted below
            outcome = self.runtimes[harness].run_turn(
                session, session.native_id(harness), item.prompt, item.model, self.emit, self.answers)
            if outcome.native_id:
                self.session = ops.adopt_native_id(self.store, session, harness, outcome.native_id)
            # the target stays the default even after a failure: its file holds the partial turn
            self._set_default(harness)
            ops.sync_after_turn(self.store, self.session, harness)
            self.store.touch_used(self.session.tandem_id)
            meter = self.meters.get(harness)
            if meter is not None:
                meter.poll()
        except SyncSetupError as exc:
            self.emit(Failure(f"sync: {exc}"))
        except Exception as exc:                       # a runtime bug must not kill the window
            self.emit(Failure(f"{harness}: {type(exc).__name__}: {exc}"))
        finally:
            self._current = None
            self.emit(Idle())
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_chat_dispatch.py tests/test_ops.py -q`
Expected: all pass (9 new + the existing ops suite)

- [ ] **Step 5: Commit**

```bash
git add src/tandem/ops.py src/tandem/chat/dispatch.py tests/test_chat_dispatch.py
git commit -m "feat(chat): dispatcher on the one-off run's bookkeeping (prepare/adopt/sync_after_turn)" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>" -m "Claude-Session: https://claude.ai/code/session_01WZ7WV9iKDBigenNSsJaRdy"
```

---
### Task 9: Screen renderer and the status bar hint

**Files:**
- Create: `src/tandem/chat/render.py`
- Modify: `src/tandem/frame.py` (`StatusBar.__init__` and `StatusBar.line`, lines 228-287)
- Test: `tests/test_chat_render.py`, `tests/test_frame.py` (append one test)

**Interfaces:**
- Produces: `Screen(write: Callable[[bytes], None], rows: int, cols: int, cfg: ChatConfig, *, color: bool = True)` with `region_rows` property (`rows - 3`, floor 1), `enter()`, `leave()`, `resize(rows, cols)`, `print(text)`, `line(text="")`, semantic painters `turn_started(ev)`, `text_delta(ev)`, `thinking_delta(ev)`, `tool_started(ev)`, `tool_output(ev)`, `tool_finished(ev)`, `approval(ev)`, `question(ev)`, `turn_finished(ev)`, `failure(ev)`, `note(text)`, `history(events, source)`, and `paint_bottom(bar_line, composer_text, cursor_col, focus_composer)`. Layout: scroll region rows `1..rows-3`, separator on `rows-2`, bar on `rows-1`, composer on `rows`.
- Produces: `frame.StatusBar(..., hint: str | None = None)` — when given, replaces the `"{key_label} flips"` trailer.
- Consumes: Task 3 events, `events.NormalizedEvent` kinds (`user_message`, `assistant_message`, `tool_call`, `tool_result`), `runtime.first_line` / `summarize_args`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_frame.py`:

```python
def test_status_bar_hint_replaces_the_flip_trailer():
    from tandem.frame import StatusBar

    bar = StatusBar(rows=24, cols=80, active="codex", others=["claude"], hint="/claude routes")
    text = bar.line(False)
    assert text.rstrip().endswith("/claude routes")
    assert "flips" not in text
    assert StatusBar(rows=24, cols=80, active="codex", others=[]).line(False).rstrip().endswith("^] flips")
```

`tests/test_chat_render.py`:

```python
import pytest

from tandem.chat.events import (ApprovalRequest, Failure, QuestionRequest, TextDelta, ThinkingDelta,
                                ToolFinished, ToolOutput, ToolStarted, TurnFinished, TurnStarted)
from tandem.chat.render import Screen
from tandem.config import ChatConfig
from tandem.events import AssistantMessage, ToolCall, ToolResult, UserMessage


class Out:
    def __init__(self):
        self.chunks = []

    def __call__(self, b: bytes):
        self.chunks.append(b)

    def text(self, clear=False):
        s = b"".join(self.chunks).decode()
        if clear:
            self.chunks.clear()
        return s


@pytest.fixture
def screen():
    out = Out()
    return Screen(out, rows=24, cols=40, cfg=ChatConfig(tool_output_lines=2), color=False), out


def test_enter_sets_the_region_and_bracketed_paste(screen):
    s, out = screen
    s.enter()
    t = out.text()
    assert "\x1b[?2004h" in t and "\x1b[1;21r" in t and "\x1b[21;1H" in t
    assert s.region_rows == 21


def test_leave_resets_everything(screen):
    s, out = screen
    s.enter(); out.text(clear=True); s.leave()
    t = out.text()
    assert "\x1b[r" in t and "\x1b[?2004l" in t


def test_print_tracks_the_column_and_returns_to_the_region_after_the_bottom_paint(screen):
    s, out = screen
    s.enter(); s.print("hello")
    assert s._col == 5
    s.paint_bottom("bar", "> hi", 4, focus_composer=True)
    out.text(clear=True)
    s.print(" world")
    assert out.text().startswith("\x1b[21;6H world")
    assert s._col == 11
    s.print("\n"); assert s._col == 0


def test_exactly_full_line_breaks_before_the_next_chunk(screen):
    s, out = screen
    s.enter(); s.print("x" * 40)
    assert s._col == 0 and out.text().endswith("x" * 40 + "\n")


def test_turn_and_tool_rows(screen):
    s, out = screen
    s.enter(); out.text(clear=True)
    s.turn_started(TurnStarted("codex", "gpt-5.5", "review it"))
    s.text_delta(TextDelta("Looks "))
    s.text_delta(TextDelta("fine."))
    s.tool_started(ToolStarted("c1", "exec", "pytest -q"))
    s.tool_output(ToolOutput("c1", "l1\nl2\nl3\nl4\n"))
    s.tool_finished(ToolFinished("c1", True, "exit 0"))
    s.turn_finished(TurnFinished("completed", "1000↑ 200↓"))
    t = out.text()
    assert "you → codex · gpt-5.5  review it\n" in t
    assert "codex\nLooks fine." in t
    assert "  ▸ exec pytest -q\n    l1\n    l2\n" in t
    assert "l3" not in t
    assert "    … +2 lines\n    ok · exit 0\n" in t
    assert "\n  completed · 1000↑ 200↓\n" in t


def test_speaker_label_is_printed_once_per_turn(screen):
    s, out = screen
    s.enter(); out.text(clear=True)
    s.turn_started(TurnStarted("claude", "", "hi"))
    s.text_delta(TextDelta("a")); s.text_delta(TextDelta("b"))
    s.tool_started(ToolStarted("c", "Bash", "ls"))
    assert out.text().count("\nclaude\n") == 1


def test_thinking_hidden_unless_configured():
    out = Out()
    Screen(out, 24, 40, ChatConfig(show_thinking=False), color=False).thinking_delta(ThinkingDelta("hmm"))
    assert "hmm" not in out.text()
    out2 = Out()
    Screen(out2, 24, 40, ChatConfig(show_thinking=True), color=False).thinking_delta(ThinkingDelta("hmm"))
    assert "hmm" in out2.text()


def test_prompts_and_failures(screen):
    s, out = screen
    s.enter(); out.text(clear=True)
    s.approval(ApprovalRequest("command", "rm -rf build"))
    s.question(QuestionRequest("Which color?", ("red", "blue")))
    s.failure(Failure("codex thread is open in another process"))
    t = out.text()
    assert "  ▸ Allow command: rm -rf build   [y]es [a]lways [n]o\n" in t
    assert "  ? Which color?\n    1. red\n    2. blue\n" in t
    assert "error: codex thread is open in another process\n" in t


def test_bottom_block_rows_and_cursor(screen):
    s, out = screen
    s.enter(); out.text(clear=True)
    s.paint_bottom("claude ● │ codex ○", "> hello", 7, focus_composer=True)
    t = out.text()
    assert "\x1b[22;1H" in t and "─" * 40 in t          # separator row
    assert "\x1b[23;1H\x1b[7m" in t and "claude ● │ codex ○" in t   # bar, reverse video
    assert "\x1b[24;1H\x1b[2K> hello" in t               # composer row
    assert t.endswith("\x1b[24;8H")                       # cursor after the text
    out.text(clear=True)
    s.paint_bottom("bar", "> x", 3, focus_composer=False)
    assert out.text().endswith("\x1b8")                   # cursor restored to the region


def test_resize_reissues_the_region(screen):
    s, out = screen
    s.enter(); out.text(clear=True); s.resize(30, 100)
    assert "\x1b[1;27r" in out.text() and s.region_rows == 27 and s.cols == 100


def test_color_off_emits_no_sgr_bold():
    out = Out()
    s = Screen(out, 24, 40, ChatConfig(), color=False)
    s.turn_started(TurnStarted("codex", "", "x"))
    assert "\x1b[1m" not in out.text()
    out2 = Out()
    Screen(out2, 24, 40, ChatConfig(), color=True).turn_started(TurnStarted("codex", "", "x"))
    assert "\x1b[1m" in out2.text()


def test_history_paints_normalized_events(screen):
    s, out = screen
    s.enter(); out.text(clear=True)
    s.history([
        UserMessage(source="claude", text="fix it"),
        AssistantMessage(source="claude", text="Done."),
        ToolCall(source="claude", call_id="1", tool="Bash", arguments={"command": "pytest"}),
        ToolResult(source="claude", call_id="1", output="12 passed\nmore"),
    ], source="claude")
    t = out.text()
    assert "you → claude  fix it\n" in t and "claude\nDone.\n" in t
    assert "  ▸ Bash pytest\n    12 passed\n" in t
```

Run: `uv run pytest tests/test_chat_render.py tests/test_frame.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'tandem.chat.render'` and `TypeError: ... unexpected keyword argument 'hint'`

- [ ] **Step 2: Add the hint to `StatusBar`**

In `src/tandem/frame.py`, change the constructor signature and body:

```python
    def __init__(self, rows: int, cols: int, active: str, others: list[str],
                 key_label: str = "^]", hint: str | None = None):
        self.rows = rows
        self.cols = cols
        self.active = active
        self.others = list(others)
        # The bar is the only place the keybind is advertised, so it has to
        # name the key actually bound — `[frame] flip_key` rebinds it, and a
        # bar still saying ^] would send the user pressing a key that goes
        # straight through to the harness. The chat window has no flip key
        # and passes its own trailer as `hint`.
        self.key_label = key_label
        self.hint = hint
```

and in `line()`, replace the `compose` return line:

```python
            trailer = self.hint if self.hint is not None else f"{self.key_label} flips"
            return f" {' │ '.join(slots)}   {trailer}"
```

- [ ] **Step 3: Write the renderer**

```python
# src/tandem/chat/render.py
"""The conversation view, in raw ANSI on the main screen.

Rows 1..rows-3 are a DECSTBM scroll region the conversation prints into, so
the terminal's own scrollback and mouse wheel keep working; rows-2 is a
separator, rows-1 the status bar (reverse video, like the frame's), rows the
composer. Output is append-only: a tool call is a row when it starts, its
output tail, and a status row when it ends — nothing is redrawn later.

The cursor lives in the region while a turn streams and in the composer
while the window waits for input. `_col` tracks where the region's bottom
row was left so a return from the composer resumes mid-line. Widths are
counted in characters (the bar's one-cell-per-glyph rule applies to what
tandem prints; model text is whatever it is and the terminal wraps it)."""

from __future__ import annotations

from typing import Callable

from ..events import AssistantMessage, ToolCall, ToolResult, UserMessage
from .events import (ApprovalRequest, Failure, QuestionRequest, TextDelta, ThinkingDelta,
                     ToolFinished, ToolOutput, ToolStarted, TurnFinished, TurnStarted)
from .runtime import first_line, summarize_args

_CSI = "\x1b["


class Screen:
    def __init__(self, write: Callable[[bytes], None], rows: int, cols: int, cfg, *,
                 color: bool = True):
        self.write = write
        self.rows = max(4, rows)
        self.cols = max(10, cols)
        self.cfg = cfg
        self.color = color
        self._col = 0
        self._focus = "region"          # "region" | "composer"
        self._speaker_shown = False
        self._turn_harness = ""
        self._tool_lines: dict[str, int] = {}
        self._tool_dropped: dict[str, int] = {}

    @property
    def region_rows(self) -> int:
        return max(1, self.rows - 3)

    # -- plumbing ------------------------------------------------------------

    def _w(self, s: str) -> None:
        self.write(s.encode())

    def _region_cmd(self) -> str:
        return f"{_CSI}1;{self.region_rows}r"

    def enter(self) -> None:
        self._w(f"{_CSI}?2004h" + self._region_cmd() + f"{_CSI}{self.region_rows};1H")
        self._col, self._focus = 0, "region"

    def leave(self) -> None:
        self._w(f"{_CSI}r{_CSI}?2004l{_CSI}{self.rows};1H\n")

    def resize(self, rows: int, cols: int) -> None:
        self.rows, self.cols = max(4, rows), max(10, cols)
        self._col = min(self._col, self.cols - 1)
        self._w("\x1b7" + self._region_cmd() + "\x1b8")
        self._focus = "composer"        # force a reposition before the next region write

    def _goto_region(self) -> None:
        if self._focus != "region":
            self._w(f"{_CSI}{self.region_rows};{self._col + 1}H")
            self._focus = "region"

    def print(self, text: str) -> None:
        """Append to the region, tracking the bottom-row column."""
        if not text:
            return
        self._goto_region()
        segments = text.split("\n")
        for i, seg in enumerate(segments):
            if i:
                self._w("\n")
                self._col = 0
            if seg:
                self._w(seg)
                self._col += len(seg)
                if self._col >= self.cols:
                    # the terminal's pending-wrap state does not survive a
                    # cursor move, so end the line here rather than guess
                    self._w("\n")
                    self._col = 0

    def line(self, text: str = "") -> None:
        if self._col:
            self.print("\n")
        self.print(text + "\n")

    def _bold(self, s: str) -> str:
        return f"{_CSI}1m{s}{_CSI}0m" if self.color else s

    def _dim(self, s: str) -> str:
        return f"{_CSI}2m{s}{_CSI}0m" if self.color else s

    # -- conversation ----------------------------------------------------------

    def _ensure_speaker(self) -> None:
        if not self._speaker_shown:
            self.line(self._bold(self._turn_harness))
            self._speaker_shown = True

    def turn_started(self, ev: TurnStarted) -> None:
        self._turn_harness = ev.harness
        self._speaker_shown = False
        self._tool_lines.clear(); self._tool_dropped.clear()
        label = f"you → {ev.harness}" + (f" · {ev.model}" if ev.model else "")
        self.line()
        if "\n" in ev.prompt:
            self.line(self._bold(label))
            self.print(ev.prompt + "\n")
        else:
            self.line(self._bold(label) + "  " + ev.prompt)

    def text_delta(self, ev: TextDelta) -> None:
        self._ensure_speaker()
        self.print(ev.text)

    def thinking_delta(self, ev: ThinkingDelta) -> None:
        if self.cfg.show_thinking:
            self._ensure_speaker()
            self.print(self._dim(ev.text))

    def tool_started(self, ev: ToolStarted) -> None:
        self._ensure_speaker()
        self._tool_lines[ev.call_id] = 0
        self._tool_dropped[ev.call_id] = 0
        self.line(self._dim(f"  ▸ {ev.tool} {ev.summary}".rstrip()))

    def tool_output(self, ev: ToolOutput) -> None:
        cap = self.cfg.tool_output_lines
        for raw in ev.text.splitlines():
            if self._tool_lines.get(ev.call_id, 0) < cap:
                self._tool_lines[ev.call_id] = self._tool_lines.get(ev.call_id, 0) + 1
                self.line(self._dim("    " + raw))
            else:
                self._tool_dropped[ev.call_id] = self._tool_dropped.get(ev.call_id, 0) + 1

    def tool_finished(self, ev: ToolFinished) -> None:
        dropped = self._tool_dropped.pop(ev.call_id, 0)
        self._tool_lines.pop(ev.call_id, None)
        if dropped:
            self.line(self._dim(f"    … +{dropped} lines"))
        status = "ok" if ev.ok else "error"
        self.line(self._dim(f"    {status}" + (f" · {ev.summary}" if ev.summary else "")))

    def approval(self, ev: ApprovalRequest) -> None:
        self.line(self._bold(f"  ▸ Allow {ev.kind}: {ev.detail}") + "   [y]es [a]lways [n]o")

    def question(self, ev: QuestionRequest) -> None:
        self.line(self._bold(f"  ? {ev.prompt}"))
        for i, option in enumerate(ev.options, 1):
            self.line(f"    {i}. {option}")
        if not ev.options:
            self.line(self._dim("    (type an answer)"))

    def turn_finished(self, ev: TurnFinished) -> None:
        if self._col:
            self.print("\n")
        if ev.usage:
            self.line(self._dim(f"  {ev.status} · {ev.usage}"))
        elif ev.status != "completed":
            self.line(self._dim(f"  {ev.status}"))

    def failure(self, ev: Failure) -> None:
        self.line(self._bold("error: ") + ev.message)

    def note(self, text: str) -> None:
        self.line(self._dim(text))

    def history(self, events, source: str) -> None:
        """Paint transcript events the adapters already parsed, tagged by the
        harness whose file they came from (translated turns carry their own
        `[via …]` marker in the text)."""
        for ev in events:
            if isinstance(ev, UserMessage):
                self.turn_started(TurnStarted(source, "", ev.text))
            elif isinstance(ev, AssistantMessage):
                self._turn_harness = source
                self._ensure_speaker()
                self.line(ev.text)
            elif isinstance(ev, ToolCall):
                self.line(self._dim(f"  ▸ {ev.tool} {summarize_args(ev.tool, ev.arguments)}".rstrip()))
            elif isinstance(ev, ToolResult):
                text = first_line(ev.output)
                if text:
                    self.line(self._dim("    " + text))

    # -- bottom block ------------------------------------------------------------

    def paint_bottom(self, bar_line: str, composer_text: str, cursor_col: int,
                     focus_composer: bool) -> None:
        r = self.rows
        out = ["\x1b7",
               f"{_CSI}{r - 2};1H" + self._dim("─" * self.cols),
               f"{_CSI}{r - 1};1H{_CSI}7m" + bar_line[: self.cols].ljust(self.cols) + f"{_CSI}0m",
               f"{_CSI}{r};1H{_CSI}2K" + composer_text[: self.cols]]
        if focus_composer:
            out.append(f"{_CSI}{r};{min(cursor_col, self.cols - 1) + 1}H")
            self._focus = "composer"
        else:
            out.append("\x1b8")
        self._w("".join(out))
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_chat_render.py tests/test_frame.py -q`
Expected: all pass (12 new + the frame suite)

- [ ] **Step 5: Commit**

```bash
git add src/tandem/chat/render.py src/tandem/frame.py tests/test_chat_render.py tests/test_frame.py
git commit -m "feat(chat): raw-ANSI conversation screen; StatusBar hint" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>" -m "Claude-Session: https://claude.ai/code/session_01WZ7WV9iKDBigenNSsJaRdy"
```

---
### Task 10: Composer (line editor and key parsing)

**Files:**
- Create: `src/tandem/chat/composer.py`
- Test: `tests/test_chat_composer.py`

**Interfaces:**
- Produces: actions `Submit(text)`, `Answer(text)`, `Interrupt()`, `CtrlC()`, `Repaint()`, `Cancel()`; `Composer(history_limit=200)` with `mode` (`"prompt" | "approval" | "question"`), `pending` (the request being answered), `text` property, `feed(data: bytes) -> list[Action]`, `begin_approval(req)`, `begin_question(req)`, `end_answer()`, `line(cols) -> tuple[str, int]` (row text and cursor column). In approval mode `y`/`a`/`n` answer `allow`/`always`/`deny` immediately and Esc is `Cancel`; in question mode a lone digit picks that option, otherwise typed text + Enter answers; in prompt mode Esc is `Interrupt`.
- Consumes: Task 3 `ApprovalRequest` / `QuestionRequest`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_chat_composer.py
from tandem.chat.composer import Answer, Cancel, Composer, CtrlC, Interrupt, Repaint, Submit
from tandem.chat.events import ApprovalRequest, QuestionRequest


def feed(c, s):
    return c.feed(s if isinstance(s, bytes) else s.encode())


def test_typing_and_submit():
    c = Composer()
    assert feed(c, "hello") == []
    assert c.text == "hello" and c.cur == 5
    assert feed(c, "\r") == [Submit("hello")]
    assert c.text == "" and c.history == ["hello"]


def test_empty_enter_is_a_noop():
    c = Composer()
    assert feed(c, "   \r") == [] and c.text == "   "


def test_editing_keys():
    c = Composer()
    feed(c, "abd")
    feed(c, b"\x1b[D")            # left
    feed(c, "c")
    assert c.text == "abcd" and c.cur == 3
    feed(c, b"\x1b[H"); feed(c, "X")              # home
    feed(c, b"\x1b[F"); feed(c, "Y")              # end
    assert c.text == "XabcdY"
    feed(c, b"\x7f")                              # backspace
    assert c.text == "Xabcd"
    feed(c, b"\x01"); feed(c, b"\x1b[3~")         # ctrl-a, delete
    assert c.text == "abcd"
    feed(c, b"\x05"); feed(c, b"\x15")            # ctrl-e, ctrl-u kills to start
    assert c.text == ""


def test_history_with_draft():
    c = Composer()
    feed(c, "one\r"); feed(c, "two\r"); feed(c, "dra")
    feed(c, b"\x1b[A"); assert c.text == "two"
    feed(c, b"\x1b[A"); assert c.text == "one"
    feed(c, b"\x1b[A"); assert c.text == "one"    # top stays
    feed(c, b"\x1b[B"); feed(c, b"\x1b[B"); assert c.text == "dra"   # back to the draft


def test_control_keys():
    c = Composer()
    assert feed(c, b"\x03") == [CtrlC()]
    assert feed(c, b"\x0c") == [Repaint()]
    assert feed(c, b"\x1b") == [Interrupt()]      # lone Esc in prompt mode


def test_escape_sequence_split_across_reads():
    c = Composer()
    feed(c, "ab")
    assert feed(c, b"\x1b[") == []
    assert feed(c, b"D") == [] and c.cur == 1


def test_bracketed_paste_keeps_newlines():
    c = Composer()
    feed(c, b"\x1b[200~line one\nline two\x1b[201~")
    assert c.text == "line one\nline two"
    row, col = c.line(40)
    assert row == "> line one (+1 lines)"
    assert feed(c, "\r") == [Submit("line one\nline two")]


def test_utf8_split_across_reads():
    c = Composer()
    feed(c, "é".encode()[:1]); feed(c, "é".encode()[1:])
    assert c.text == "é"


def test_line_scrolls_around_the_cursor():
    c = Composer()
    feed(c, "x" * 50)
    row, col = c.line(20)
    assert len(row) == 19 and row.startswith("> ") and col == 19   # 17 visible chars, cursor after the last
    feed(c, b"\x01")
    row, col = c.line(20)
    assert row == "> " + "x" * 18 and col == 2


def test_approval_mode_keys():
    c = Composer()
    c.begin_approval(ApprovalRequest("command", "rm x"))
    assert c.line(60)[0].startswith(" [y]es [a]lways [n]o")
    assert feed(c, "q") == []                     # not a choice
    assert feed(c, "A") == [Answer("always")]
    assert feed(c, b"\x1b") == [Cancel()]
    assert feed(c, "\r") == []                    # Enter does not pick a default
    c.end_answer()
    assert c.mode == "prompt"


def test_question_mode_digit_and_free_text():
    c = Composer()
    c.begin_question(QuestionRequest("Which?", ("red", "blue")))
    assert c.line(60)[0] == "? "
    assert feed(c, "2") == [Answer("blue")]
    c.begin_question(QuestionRequest("Name?", ()))
    assert feed(c, "9") == [] and c.text == "9"    # no option 9: it is text
    feed(c, "x")
    assert feed(c, "\r") == [Answer("9x")]
```

Run: `uv run pytest tests/test_chat_composer.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'tandem.chat.composer'`

- [ ] **Step 2: Write the composer**

```python
# src/tandem/chat/composer.py
"""The single-line editor at the bottom of the window, and the key parser
that drives it. Pure: bytes in, actions out, no terminal access.

Three modes. `prompt` edits text and submits on Enter; `approval` answers
a permission request with one key; `question` picks a numbered option or
takes free text. Bracketed paste keeps its newlines (the prompt becomes
multi-line; the row shows the first line and a `(+N lines)` marker). A
partial escape sequence at the end of a read is carried to the next one;
a lone Esc is a key."""

from __future__ import annotations

import codecs
from dataclasses import dataclass
from typing import Union

from .events import ApprovalRequest, QuestionRequest


@dataclass(frozen=True)
class Submit:
    text: str


@dataclass(frozen=True)
class Answer:
    text: str


@dataclass(frozen=True)
class Interrupt:
    pass


@dataclass(frozen=True)
class CtrlC:
    pass


@dataclass(frozen=True)
class Repaint:
    pass


@dataclass(frozen=True)
class Cancel:
    pass


Action = Union[Submit, Answer, Interrupt, CtrlC, Repaint, Cancel]

_APPROVAL_KEYS = {"y": "allow", "a": "always", "n": "deny"}
_CSI_FINAL = {ord("A"): "up", ord("B"): "down", ord("C"): "right", ord("D"): "left",
              ord("H"): "home", ord("F"): "end"}
_TILDE = {"200": "paste_start", "201": "paste_end", "3": "delete",
          "1": "home", "7": "home", "4": "end", "8": "end"}
APPROVAL_ROW = " [y]es [a]lways [n]o  (Esc denies and interrupts)"


class Composer:
    def __init__(self, history_limit: int = 200):
        self.buf: list[str] = []
        self.cur = 0
        self.history: list[str] = []
        self.history_limit = history_limit
        self._hidx: int | None = None
        self._draft = ""
        self.mode = "prompt"
        self.pending: ApprovalRequest | QuestionRequest | None = None
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._paste = False
        self._carry = b""

    # -- modes ---------------------------------------------------------------

    def begin_approval(self, req: ApprovalRequest) -> None:
        self.mode, self.pending = "approval", req

    def begin_question(self, req: QuestionRequest) -> None:
        self.mode, self.pending = "question", req
        self.buf, self.cur = [], 0

    def end_answer(self) -> None:
        self.mode, self.pending = "prompt", None
        self.buf, self.cur = [], 0

    @property
    def text(self) -> str:
        return "".join(self.buf)

    # -- input ---------------------------------------------------------------

    def feed(self, data: bytes) -> list[Action]:
        actions: list[Action] = []
        data = self._carry + data
        self._carry = b""
        i = 0
        while i < len(data):
            if self._paste:
                end = data.find(b"\x1b[201~", i)
                if end == -1:
                    self._insert(self._decoder.decode(data[i:]))
                    break
                self._insert(self._decoder.decode(data[i:end]))
                self._paste = False
                i = end + 6
                continue
            b = data[i]
            if b == 0x1B:
                name, n = self._escape(data[i:])
                if n == 0:                        # incomplete sequence: wait for more bytes
                    self._carry = data[i:]
                    break
                i += n
                if name == "paste_start":
                    self._paste = True
                elif name == "esc":
                    actions.append(Cancel() if self.mode != "prompt" else Interrupt())
                elif name:
                    self._key(name)
                continue
            i += 1
            if b == 0x03:
                actions.append(CtrlC())
            elif b == 0x0C:
                actions.append(Repaint())
            elif b in (0x0D, 0x0A):
                self._enter(actions)
            elif b in (0x7F, 0x08):
                self._backspace()
            elif b == 0x01:
                self.cur = 0
            elif b == 0x05:
                self.cur = len(self.buf)
            elif b == 0x15:
                del self.buf[: self.cur]
                self.cur = 0
            elif b == 0x0B:
                del self.buf[self.cur:]
            elif b < 0x20:
                pass                              # other control bytes: ignored
            else:
                j = i - 1
                while i < len(data) and data[i] >= 0x20 and data[i] not in (0x7F, 0x1B):
                    i += 1
                self._typed(self._decoder.decode(data[j:i]), actions)
        return actions

    def _escape(self, data: bytes) -> tuple[str, int]:
        """(key name, bytes consumed); ("", n) swallows an unknown sequence;
        ("esc", 1) is the Esc key; n == 0 means incomplete."""
        if len(data) == 1:
            return "esc", 1
        if data[1:2] == b"[":
            j = 2
            while j < len(data) and not (0x40 <= data[j] <= 0x7E):
                j += 1
            if j >= len(data):
                return "", 0
            final, params = data[j], data[2:j].decode(errors="replace")
            if final == ord("~"):
                return _TILDE.get(params, ""), j + 1
            return _CSI_FINAL.get(final, ""), j + 1
        if data[1:2] == b"O":
            if len(data) < 3:
                return "", 0
            return _CSI_FINAL.get(data[2], ""), 3
        return "esc", 1                           # Esc then an ordinary key

    def _typed(self, text: str, actions: list[Action]) -> None:
        if not text:
            return
        if self.mode == "approval":
            for ch in text:
                choice = _APPROVAL_KEYS.get(ch.lower())
                if choice:
                    actions.append(Answer(choice))
                    return
            return
        if (self.mode == "question" and self.pending is not None and self.pending.options
                and not self.buf and text.strip().isdigit()):
            n = int(text.strip())
            if 1 <= n <= len(self.pending.options):
                actions.append(Answer(self.pending.options[n - 1]))
                return
        self._insert(text)

    def _key(self, name: str) -> None:
        if name == "left":
            self.cur = max(0, self.cur - 1)
        elif name == "right":
            self.cur = min(len(self.buf), self.cur + 1)
        elif name == "home":
            self.cur = 0
        elif name == "end":
            self.cur = len(self.buf)
        elif name == "delete":
            if self.cur < len(self.buf):
                del self.buf[self.cur]
        elif name == "up":
            self._history_step(-1)
        elif name == "down":
            self._history_step(1)

    def _history_step(self, step: int) -> None:
        if self.mode != "prompt" or not self.history:
            return
        if self._hidx is None:
            if step > 0:
                return
            self._draft = self.text
            self._hidx = len(self.history)
        idx = max(0, self._hidx + step)
        if idx >= len(self.history):
            self._hidx = None
            self._set(self._draft)
            return
        self._hidx = idx
        self._set(self.history[idx])

    def _set(self, text: str) -> None:
        self.buf, self.cur = list(text), len(text)

    def _insert(self, text: str) -> None:
        if text:
            self.buf[self.cur:self.cur] = list(text)
            self.cur += len(text)

    def _backspace(self) -> None:
        if self.cur > 0:
            del self.buf[self.cur - 1]
            self.cur -= 1

    def _enter(self, actions: list[Action]) -> None:
        if self.mode == "approval":
            return
        text = self.text
        if self.mode == "question":
            if text.strip():
                actions.append(Answer(text.strip()))
                self.buf, self.cur = [], 0
            return
        if not text.strip():
            return
        if not self.history or self.history[-1] != text:
            self.history.append(text)
            del self.history[: -self.history_limit]
        self._hidx = None
        self.buf, self.cur = [], 0
        actions.append(Submit(text))

    # -- row -------------------------------------------------------------------

    def line(self, cols: int) -> tuple[str, int]:
        if self.mode == "approval":
            return APPROVAL_ROW[:cols], 0
        prompt = "? " if self.mode == "question" else "> "
        text, cur = self.text, self.cur
        nl = text.find("\n")
        if nl != -1:
            text = text[:nl] + f" (+{text.count(chr(10))} lines)"
            cur = min(cur, nl)
        avail = max(1, cols - len(prompt))
        start = cur - avail + 1 if cur >= avail else 0
        return prompt + text[start:start + avail], len(prompt) + (cur - start)
```

- [ ] **Step 3: Run the tests**

Run: `uv run pytest tests/test_chat_composer.py -q`
Expected: 11 passed

- [ ] **Step 4: Commit**

```bash
git add src/tandem/chat/composer.py tests/test_chat_composer.py
git commit -m "feat(chat): composer with key parsing, history, paste, approval and question modes" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>" -m "Claude-Session: https://claude.ai/code/session_01WZ7WV9iKDBigenNSsJaRdy"
```

---
### Task 11: The window loop and `tandem chat`

**Files:**
- Create: `src/tandem/chat/window.py`
- Modify: `src/tandem/cli.py` (add the `chat` command after `run_cmd`)
- Test: `tests/test_chat_window.py`, `tests/test_cli.py` (append)

**Interfaces:**
- Produces: `WindowAnswers(post)` (an `Answers` that posts the request as an event and blocks on `resolve(text)`), `Window(session, store, cfg, screen, composer, dispatcher, answers, bar, usage_state, meters, poller=None)` with `handle_event(ev)`, `handle_input(data) -> bool` (False = quit), `paint()`, `paint_history()`, `bar_line()`; `run_chat(session, store, cfg, *, stdin_fd=None, out_fd=None, runtimes=None) -> int`; CLI `tandem chat [--on HARNESS]`.
- Consumes: Tasks 2-10; `frame.StatusBar`, `ratelimit.RateLimitPoller`, `runner.UsageFeed`, `ptyrun._winsize`, `config.load_frame_config`, `events.SessionContext`, `state.SyncCursor`, `cli._pair_session`, `cli._resolve_participants`, `cli._narrow_participants`.
- Key rules: Enter submits; `y`/`a`/`n` answer an approval; digits or text answer a question; Esc interrupts (or denies + interrupts inside a prompt); Ctrl-C once interrupts, twice within 2 s quits; Ctrl-L repaints. The bar's marked slot is the current default; its trailer reads `/claude /codex /opencode route`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_chat_window.py
import os
import threading
import time

from conftest import claude_assistant, claude_user, write_line

from tandem.chat.composer import Composer
from tandem.chat.events import (ApprovalRequest, Idle, LimitsUpdate, QuestionRequest, TextDelta,
                                TurnFinished, TurnOutcome, TurnStarted)
from tandem.chat.render import Screen
from tandem.chat.window import Window, WindowAnswers, run_chat
from tandem.config import ChatConfig
from tandem.frame import StatusBar


class Out:
    def __init__(self): self.buf = bytearray()
    def __call__(self, b): self.buf += b
    def text(self): return self.buf.decode(errors="replace")


class StubDispatcher:
    def __init__(self):
        self.submitted, self.pumps, self.interrupts = [], 0, 0
        self.busy, self.default, self.note = False, "claude", ""
    def submit(self, text): self.submitted.append(text); return self.note
    def pump(self): self.pumps += 1
    def interrupt(self): self.interrupts += 1
    def close(self): pass


def make_window(env):
    out = Out()
    screen = Screen(out, 24, 60, ChatConfig(), color=False)
    answers = WindowAnswers(lambda ev: None)
    d = StubDispatcher()
    bar = StatusBar(24, 60, "claude", ["codex"], hint="/claude /codex route")
    w = Window(env.session, env.store, ChatConfig(), screen, Composer(), d, answers, bar, {"limits": {}}, {})
    return w, d, out, answers


def test_submit_and_notes(env_factory):
    env = env_factory(); w, d, out, _ = make_window(env)
    assert w.handle_input(b"hello\r") is True
    assert d.submitted == ["hello"]
    d.note = "queued → codex"; w.handle_input(b"more\r")
    assert "queued → codex" in out.text()
    d.note = "error: nope"; w.handle_input(b"/x\r")
    assert "error: nope" in out.text()


def test_approval_round_trip(env_factory):
    env = env_factory(); w, d, out, answers = make_window(env)
    got = {}
    t = threading.Thread(target=lambda: got.__setitem__("choice", answers.approve(ApprovalRequest("command", "rm x"))))
    t.start(); time.sleep(0.05)
    w.handle_event(ApprovalRequest("command", "rm x"))          # the window sees the posted request
    assert w.composer.mode == "approval" and "[y]es [a]lways [n]o" in out.text()
    w.handle_input(b"y"); t.join(2)
    assert got["choice"] == "allow" and w.composer.mode == "prompt"


def test_esc_during_approval_denies_and_interrupts(env_factory):
    env = env_factory(); w, d, out, answers = make_window(env)
    got = {}
    t = threading.Thread(target=lambda: got.__setitem__("c", answers.approve(ApprovalRequest("command", "x")))); t.start()
    time.sleep(0.05); w.handle_event(ApprovalRequest("command", "x")); d.busy = True
    w.handle_input(b"\x1b"); t.join(2)
    assert got["c"] == "deny" and d.interrupts == 1


def test_question_by_digit(env_factory):
    env = env_factory(); w, d, out, answers = make_window(env)
    got = {}
    t = threading.Thread(target=lambda: got.__setitem__("a", answers.answer(QuestionRequest("Which?", ("red", "blue"))))); t.start()
    time.sleep(0.05); w.handle_event(QuestionRequest("Which?", ("red", "blue")))
    w.handle_input(b"2"); t.join(2)
    assert got["a"] == "blue"


def test_ctrl_c_ladder(env_factory):
    env = env_factory(); w, d, out, _ = make_window(env)
    d.busy = True
    assert w.handle_input(b"\x03") is True and d.interrupts == 1
    assert w.handle_input(b"\x03") is False                     # second within 2s quits
    w._ctrlc_at = 0.0
    assert w.handle_input(b"\x03") is True                      # a stale first press does not quit


def test_events_paint_and_idle_pumps(env_factory):
    env = env_factory(); w, d, out, _ = make_window(env)
    w.handle_event(TurnStarted("codex", "", "go")); w.handle_event(TextDelta("hi")); w.handle_event(TurnFinished("completed", "u"))
    w.handle_event(LimitsUpdate("codex", "5h 3%")); w.handle_event(Idle())
    assert "you → codex  go" in out.text() and "codex\nhi" in out.text()
    assert w.usage_state["limits"]["codex"] == "5h 3%" and d.pumps == 1
    assert "5h 3%" in w.bar_line()


def test_history_paints_the_default_harness_transcript(env_factory):
    env = env_factory(active="claude")
    write_line(env.claude_shadow, claude_user("fix the tests", uuid="u9"))
    write_line(env.claude_shadow, claude_assistant([{"type": "text", "text": "All green."}], uuid="a9"))
    w, d, out, _ = make_window(env)
    w.paint_history()
    t = out.text()
    assert "fix the tests" in t and "All green." in t


def test_run_chat_on_a_pty(env_factory):
    """The real loop: raw mode, a submitted prompt reaching a fake runtime, Ctrl-C twice to quit."""
    env = env_factory()

    class FakeRuntime:
        harness = "claude"
        def run_turn(self, session, native_id, prompt, model, emit, answers):
            emit(TextDelta(f"echo:{prompt}")); emit(TurnFinished("completed", "")); return TurnOutcome("completed")
        def interrupt(self): pass
        def close(self): pass

    master, slave = os.openpty()
    captured = bytearray()

    def driver():
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and b"> " not in captured:
            captured.extend(os.read(master, 4096)); time.sleep(0.01)
        os.write(master, b"ping\r")
        while time.monotonic() < deadline and b"echo:ping" not in captured:
            captured.extend(os.read(master, 4096)); time.sleep(0.01)
        os.write(master, b"\x03\x03")
        while time.monotonic() < deadline:
            try: captured.extend(os.read(master, 4096))
            except OSError: break

    t = threading.Thread(target=driver, daemon=True); t.start()
    code = run_chat(env.session, env.store, ChatConfig(), stdin_fd=slave, out_fd=slave,
                    runtimes={"claude": FakeRuntime(), "codex": FakeRuntime()})
    os.close(slave)                                   # the driver's read then fails and ends
    t.join(5)
    assert code == 0
    text = captured.decode(errors="replace")
    assert "echo:ping" in text and "\x1b[r" in text
```

Append to `tests/test_cli.py`:

```python
def test_chat_command_uses_the_latest_session_and_honors_on(env_factory, monkeypatch):
    from click.testing import CliRunner

    from tandem import cli

    env = env_factory(active="claude")
    seen = {}
    monkeypatch.setattr(cli, "_cwd", lambda: env.cwd)
    monkeypatch.setattr("tandem.chat.window.run_chat", lambda session, store, cfg, **kw: seen.setdefault("session", session) and 0)
    result = CliRunner().invoke(cli.main, ["chat", "--on", "codex"])
    assert result.exit_code == 0, result.output
    assert seen["session"].active == "codex"
    assert env.store.get_session(env.session.tandem_id).active == "codex"


def test_chat_rejects_a_non_participant(env_factory, monkeypatch):
    from click.testing import CliRunner

    from tandem import cli

    env = env_factory(active="claude")
    monkeypatch.setattr(cli, "_cwd", lambda: env.cwd)
    result = CliRunner().invoke(cli.main, ["chat", "--on", "opencode"])
    assert result.exit_code == 1 and "not a participant" in result.output
```

Run: `uv run pytest tests/test_chat_window.py tests/test_cli.py -q -k "chat"`
Expected: FAIL with `ModuleNotFoundError: No module named 'tandem.chat.window'`

- [ ] **Step 2: Write the window**

```python
# src/tandem/chat/window.py
"""Wiring: the composer feeds the dispatcher, the dispatcher's worker posts
live events, the main thread paints them and answers prompts.

Threads: the main loop selects on stdin and a wake pipe; the dispatcher
runs each turn on a worker; runtimes block on WindowAnswers until the user
types an answer. Every event crosses to the main thread through a queue
plus one byte on the wake pipe, so the loop never polls."""

from __future__ import annotations

import os
import queue
import select
import signal
import sys
import termios
import time
import tty
from typing import Callable

from ..config import load_frame_config
from ..events import SessionContext, UserMessage
from ..frame import StatusBar
from ..harness import get_adapter
from ..ptyrun import _winsize
from ..ratelimit import RateLimitPoller
from ..runner import UsageFeed
from ..state import SyncCursor
from .composer import Answer, Cancel, Composer, CtrlC, Interrupt, Repaint, Submit
from .dispatch import Dispatcher
from .events import (ApprovalRequest, Failure, Idle, LimitsUpdate, LiveEvent, QuestionRequest,
                     TextDelta, ThinkingDelta, ToolFinished, ToolOutput, ToolStarted,
                     TurnFinished, TurnStarted)
from .render import Screen
from .runtime.factory import make_runtimes

HINT = "/claude /codex /opencode route"


class WindowAnswers:
    def __init__(self, post: Callable[[LiveEvent], None]):
        self._post = post
        self._q: queue.Queue = queue.Queue()

    def approve(self, req: ApprovalRequest) -> str:
        self._post(req)
        return self._q.get()

    def answer(self, req: QuestionRequest) -> str:
        self._post(req)
        return self._q.get()

    def resolve(self, text: str) -> None:
        self._q.put(text)


class Window:
    def __init__(self, session, store, cfg, screen: Screen, composer: Composer,
                 dispatcher, answers: WindowAnswers, bar: StatusBar, usage_state: dict,
                 meters: dict, poller: RateLimitPoller | None = None):
        self.session, self.store, self.cfg = session, store, cfg
        self.screen, self.composer, self.dispatcher = screen, composer, dispatcher
        self.answers, self.bar, self.usage_state, self.meters, self.poller = answers, bar, usage_state, meters, poller
        self._ctrlc_at = 0.0

    # -- painting ------------------------------------------------------------

    def bar_line(self) -> str:
        default = self.dispatcher.default
        self.bar.active = default
        self.bar.others = [h for h in self.session.participants if h != default]
        meter = self.meters.get(default)
        usage = meter.state.get("text", "") if meter is not None else ""
        return self.bar.line(False, usage, self.usage_state.get("limits") or {})

    def paint(self) -> None:
        text, col = self.composer.line(self.screen.cols)
        self.screen.paint_bottom(self.bar_line(), text, col, focus_composer=True)

    def paint_history(self) -> None:
        harness = self.dispatcher.default
        sid = self.session.native_id(harness)
        if not sid:
            return
        try:
            adapter = get_adapter(harness)
            path = adapter.transcript_path(self.session.cwd, sid)
            if path is None:
                return
            others = self.session.targets_for(harness)
            ctx = SessionContext(tandem_id=self.session.tandem_id, cwd=self.session.cwd,
                                 direction=f"{harness}->{others[0] if others else harness}",
                                 source_session_id=sid,
                                 target_session_id=self.session.native_id(others[0]) if others else None)
            cursor = SyncCursor(tandem_id=self.session.tandem_id, source=harness, target="__chat__")
            reader = adapter.make_source_reader(self.session, cursor, path)
            events = []
            for line in reader.poll():
                if line.raw is not None:
                    events.extend(adapter.parse_entry(line.raw, ctx))
            starts = [i for i, e in enumerate(events) if isinstance(e, UserMessage)]
            if len(starts) > self.cfg.history_turns:
                events = events[starts[-self.cfg.history_turns]:]
            self.screen.history(events, harness)
        except Exception as exc:                       # history is a courtesy, never a blocker
            self.screen.note(f"history unavailable: {type(exc).__name__}: {exc}")

    # -- events (main thread) --------------------------------------------------

    def handle_event(self, ev: LiveEvent) -> None:
        s = self.screen
        if isinstance(ev, TurnStarted):
            s.turn_started(ev)
        elif isinstance(ev, TextDelta):
            s.text_delta(ev)
        elif isinstance(ev, ThinkingDelta):
            s.thinking_delta(ev)
        elif isinstance(ev, ToolStarted):
            s.tool_started(ev)
        elif isinstance(ev, ToolOutput):
            s.tool_output(ev)
        elif isinstance(ev, ToolFinished):
            s.tool_finished(ev)
        elif isinstance(ev, ApprovalRequest):
            s.approval(ev)
            self.composer.begin_approval(ev)
        elif isinstance(ev, QuestionRequest):
            s.question(ev)
            self.composer.begin_question(ev)
        elif isinstance(ev, TurnFinished):
            s.turn_finished(ev)
        elif isinstance(ev, Failure):
            s.failure(ev)
        elif isinstance(ev, LimitsUpdate):
            limits = dict(self.usage_state.get("limits") or {})
            limits[ev.harness] = ev.text
            self.usage_state["limits"] = limits
        elif isinstance(ev, Idle):
            self.session = getattr(self.dispatcher, "session", self.session)
            self.dispatcher.pump()
            if self.poller is not None:
                self.poller.poke()
        self.paint()

    # -- input (main thread) ---------------------------------------------------

    def handle_input(self, data: bytes) -> bool:
        for action in self.composer.feed(data):
            if isinstance(action, Submit):
                note = self.dispatcher.submit(action.text)
                if note.startswith("error: "):
                    self.screen.failure(Failure(note[7:]))
                elif note:
                    self.screen.note(note)
            elif isinstance(action, Answer):
                self.answers.resolve(action.text)
                self.composer.end_answer()
            elif isinstance(action, Cancel):
                self.answers.resolve("deny")
                self.composer.end_answer()
                self.dispatcher.interrupt()
                self.screen.note("denied · interrupting…")
            elif isinstance(action, Interrupt):
                if self.dispatcher.busy:
                    self.dispatcher.interrupt()
                    self.screen.note("interrupting…")
            elif isinstance(action, CtrlC):
                now = time.monotonic()
                if now - self._ctrlc_at < 2.0:
                    return False
                self._ctrlc_at = now
                if self.dispatcher.busy:
                    self.dispatcher.interrupt()
                    self.screen.note("interrupting… (Ctrl-C again to quit)")
                else:
                    self.screen.note("Ctrl-C again to quit")
            elif isinstance(action, Repaint):
                self.screen.enter()
        self.paint()
        return True


def run_chat(session, store, cfg, *, stdin_fd: int | None = None, out_fd: int | None = None,
             runtimes: dict | None = None) -> int:
    stdin_fd = sys.stdin.fileno() if stdin_fd is None else stdin_fd
    out_fd = sys.stdout.fileno() if out_fd is None else out_fd
    if not os.isatty(stdin_fd):
        sys.stderr.write("tandem chat needs a terminal\n")
        return 1
    rows, cols = _winsize(stdin_fd)
    events: queue.Queue = queue.Queue()
    wake_r, wake_w = os.pipe()

    def post(ev: LiveEvent) -> None:
        events.put(ev)
        try:
            os.write(wake_w, b"E")
        except OSError:
            pass

    def write(b: bytes) -> None:
        view = memoryview(b)
        while view:
            n = os.write(out_fd, view)
            view = view[n:]

    screen = Screen(write, rows, cols, cfg, color="NO_COLOR" not in os.environ)
    composer = Composer()
    answers = WindowAnswers(post)
    runtimes = runtimes if runtimes is not None else make_runtimes(session, cfg)
    meters: dict = {}
    for h in session.participants:
        sid = session.native_id(h)
        path = get_adapter(h).transcript_path(session.cwd, sid) if sid else None
        if path is not None:
            meters[h] = UsageFeed(get_adapter(h), session, path, {"text": ""})
    usage_state: dict = {"limits": {}}
    poller = RateLimitPoller(list(session.participants), usage_state) if load_frame_config().rate_limits else None
    dispatcher = Dispatcher(store, session, runtimes, post, answers, meters=meters)
    bar = StatusBar(rows, cols, session.active, session.targets_for(session.active), hint=HINT)
    win = Window(session, store, cfg, screen, composer, dispatcher, answers, bar, usage_state, meters, poller)

    old_attrs = termios.tcgetattr(stdin_fd)
    old_winch = signal.signal(signal.SIGWINCH, lambda *_: os.write(wake_w, b"W"))
    try:
        tty.setraw(stdin_fd)
        screen.enter()
        win.paint_history()
        for m in meters.values():
            m.poll()
        if poller is not None:
            poller.ensure_started()
        win.paint()
        while True:
            ready, _, _ = select.select([stdin_fd, wake_r], [], [], 1.0)
            if wake_r in ready:
                kinds = os.read(wake_r, 4096)
                if b"W" in kinds:
                    rows, cols = _winsize(stdin_fd)
                    screen.resize(rows, cols)
                    bar.resize(rows, cols)
                while True:
                    try:
                        win.handle_event(events.get_nowait())
                    except queue.Empty:
                        break
            if stdin_fd in ready:
                data = os.read(stdin_fd, 4096)
                if not data or not win.handle_input(data):
                    break
            if not ready:
                win.paint()                              # the bar's rate-limit figures refresh on their own clock
    finally:
        dispatcher.close()
        if poller is not None:
            poller.stop()
        screen.leave()
        termios.tcsetattr(stdin_fd, termios.TCSADRAIN, old_attrs)
        signal.signal(signal.SIGWINCH, old_winch)
        os.close(wake_r)
        os.close(wake_w)
    write(f"tandem chat: session {session.tandem_id} · continue with `tandem chat`\r\n".encode())
    return 0
```

- [ ] **Step 3: Add the CLI command**

In `src/tandem/cli.py`, after `run_cmd`:

```python
@main.command()
@click.option(
    "--on", "harness",
    type=click.Choice(["claude", "codex", "opencode"]),
    default=None,
    help="Harness for the first prompt [default: the session's last-used harness].",
)
def chat(harness: str | None) -> None:
    """One composer for every harness.

    Prompts run headless on the last-used CLI; a leading /claude, /codex or
    /opencode runs the prompt there and makes it the default. Pairs a fresh
    session when this directory has none."""
    from .chat.window import run_chat
    from .config import load_chat_config

    cwd = _cwd()
    with StateStore() as store:
        session = store.latest_session_for_cwd(cwd)
        if session is None:
            usable, _ = _resolve_participants()
            session = _pair_session(store, cwd, harness or usable[0], usable)
        else:
            store.touch_used(session.tandem_id)
            session = _narrow_participants(store, session)
        if harness is not None:
            if harness not in session.participants:
                click.secho(
                    f"error: {harness} is not a participant in this session "
                    f"(participants: {', '.join(session.participants)}).",
                    fg="red", err=True,
                )
                sys.exit(1)
            store.set_active(session.tandem_id, harness)
            session = store.get_session(session.tandem_id) or session
        code = run_chat(session, store, load_chat_config())
    sys.exit(code)
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_chat_window.py tests/test_cli.py -q`
Expected: all pass (8 window tests including the pty loop, 2 CLI tests, plus the existing CLI suite). If `test_chat_rejects_a_non_participant` sees the message on stderr only, assert on `result.stderr` or use `CliRunner(mix_stderr=True)` depending on the installed click.

- [ ] **Step 5: Run the whole suite**

Run: `uv run pytest -q`
Expected: everything green.

- [ ] **Step 6: Commit**

```bash
git add src/tandem/chat/window.py src/tandem/cli.py tests/test_chat_window.py tests/test_cli.py
git commit -m "feat(chat): tandem chat — window loop, answers, bar, history" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>" -m "Claude-Session: https://claude.ai/code/session_01WZ7WV9iKDBigenNSsJaRdy"
```

---
### Task 12: Docs, version pins, the doctor line, and the live gate

**Files:**
- Modify: `src/tandem/compat.py` (the `COMPAT` table), `src/tandem/doctor.py` (`run_doctor`), `README.md` (Everyday commands table, line ~140), `docs/configuration.md` (new section before "Environment variables"), `docs/development.md` (Layout list and a new "Rechecking the codex protocol" section), `docs/formats.md` (new section at the end)
- Create: `tools/live_gate_chat.py`
- Test: `tests/test_paths_compat.py` or `tests/test_memory_doctor.py` (append one doctor test)

**Interfaces:**
- Consumes: everything above; `doctor.DoctorReport.ok/warn`, `compat.COMPAT`, `compat.detect_cli_version`.
- Produces: `COMPAT["claude"].tested == "2.1.265"`, `COMPAT["opencode"].tested == "1.18.20"`; a doctor check `chat: codex protocol models generated from <v> (installed <v>)` that warns when the generated version differs from the pinned one or from the installed codex; the gate script.

- [ ] **Step 1: Write the failing doctor test**

Append to `tests/test_memory_doctor.py`:

```python
def test_doctor_reports_the_codex_protocol_pin(env_factory, monkeypatch):
    from tandem import doctor
    from tandem.compat import COMPAT

    env = env_factory()
    report = doctor.run_doctor(env.store, env.session)
    msgs = [c.message for c in report.checks]
    line = next(m for m in msgs if m.startswith("chat: codex protocol models"))
    assert f"generated from {COMPAT['codex'].tested}" in line
    assert next(c for c in report.checks if c.message == line).status == "ok"
    monkeypatch.setattr(doctor, "_codex_protocol_version", lambda: "0.1.0")
    report = doctor.run_doctor(env.store, env.session)
    warn = next(c for c in report.checks if c.message.startswith("chat: codex protocol models"))
    assert warn.status == "warn" and "0.1.0" in warn.message
```

Run: `uv run pytest tests/test_memory_doctor.py -q -k protocol`
Expected: FAIL (`StopIteration`: no such line yet)

- [ ] **Step 2: Bump the pins and add the doctor line**

In `src/tandem/compat.py`:

```python
COMPAT: dict[str, CompatRange] = {
    "claude": CompatRange(tested="2.1.265", min_version=(2, 0), max_exclusive=(3,)),
    "codex": CompatRange(tested="0.153.4", min_version=(0, 140), max_exclusive=(0, 160)),
    # Floor-only by operator decision (spec: Compat gate). Pre-1.18 opencode
    # predates SQLite session storage and genuinely cannot work.
    "opencode": CompatRange(tested="1.18.20", min_version=(1, 18)),
}
```

In `src/tandem/doctor.py`, add near the top:

```python
def _codex_protocol_version() -> str | None:
    """The codex version the generated app-server models were built from
    (first line of codex_protocol.py), or None when unreadable."""
    import re
    from pathlib import Path

    try:
        from .chat.runtime import codex_protocol
        first = Path(codex_protocol.__file__).read_text().splitlines()[0]
    except Exception:
        return None
    m = re.search(r"from codex (\S+)", first)
    return m.group(1) if m else None
```

and at the end of `run_doctor` (before `return report`), after the existing per-harness checks:

```python
    generated = _codex_protocol_version()
    pinned = compat.COMPAT["codex"].tested
    installed = compat.detect_cli_version("codex")
    installed_v = compat.parse_version(installed) if installed else None
    installed_s = ".".join(str(x) for x in installed_v) if installed_v else "not installed"
    if generated is None:
        report.warn("chat: codex protocol models missing — run tools/gen_codex_protocol.py")
    elif generated != pinned or (installed_v and installed_s != generated):
        report.warn(f"chat: codex protocol models generated from {generated} (pinned {pinned}, installed {installed_s});"
                    " regenerate with tools/gen_codex_protocol.py and re-run the live gate")
    else:
        report.ok(f"chat: codex protocol models generated from {generated} (installed {installed_s})")
```

(`doctor.py` already imports `compat`; if it imports names individually, add `from . import compat`.) `Env` pins `detect_version` on the adapters, not `compat.detect_cli_version`; in the test environment the real `codex --version` may run once — it is cached and `Env` tolerates it — or, if the CI runner has no codex, `installed_s` is `not installed` and the line is still `ok`.

Run: `uv run pytest tests/test_memory_doctor.py tests/test_paths_compat.py tests/test_current_formats.py -q`
Expected: pass. If a test pins the old `tested` strings (grep `2.1.261` / `1.18.15` under `tests/`), update those assertions to the new values in the same commit.

- [ ] **Step 3: Documentation**

`README.md`, Everyday commands table — add after the `tandem run --on codex "…"` row:

```markdown
| `tandem chat` | One composer for every CLI: prompts run headless on the last-used harness; `/claude`, `/codex`, `/opencode` route and stick |
```

`docs/configuration.md` — insert before `## Environment variables`:

```markdown
## [chat] — the unified window

`tandem chat` runs every prompt headless inside the harness that ran the
last one, unless the prompt starts with `/claude`, `/codex`, or
`/opencode` (optionally `/codex:gpt-5.5` to pin a model for that harness,
`/codex:default` to clear it). A bare route switches the default without a
turn. Any other leading `/word` is passed to the current harness as its
own slash command, and `@path` mentions reach it untouched.

```toml
[chat]
tool_output_lines = 8        # lines of tool output shown per call (rest elided)
history_turns = 50           # turns painted from the transcript at startup
show_thinking = false        # reasoning summaries, dimmed
claude_setting_sources = ["user", "project", "local"]   # what headless claude loads
# codex_approval_policy = "on-request"   # default: inherit ~/.codex/config.toml
# codex_sandbox = "workspace-write"      # default: inherit
```

Keys: Enter sends; Esc interrupts the running turn; Ctrl-C once interrupts,
twice within two seconds quits; Ctrl-L repaints. An approval prompt takes
`y`, `a` (allow for the rest of the session), or `n`; a question takes its
option number or typed text.

Headless claude and codex app-server skip the folder-trust prompts their
TUIs show; opencode's default config auto-allows `bash` and only asks when
your opencode config says so. The bar's rate-limit polling follows
`[frame] rate_limits`.
```

`docs/development.md` — add to the Layout list:

```markdown
- `src/tandem/chat/` — the unified window: `promptroute.py` (the `/`
  grammar), `runtime/` (one headless client per harness; `codex_protocol.py`
  is generated), `dispatch.py`, `render.py`, `composer.py`, `window.py`.
```

and a new section after "Extending tandem":

```markdown
## Rechecking the codex protocol

`src/tandem/chat/runtime/codex_protocol.py` is generated from the schema
the installed `codex` dumps (`codex app-server generate-json-schema`), and
`tests/test_codex_protocol.py` pins its header to `COMPAT["codex"].tested`.
When codex moves:

1. `git -C ~/git/codex fetch --tags` and read the diff between the old and
   new tags for `codex-rs/app-server-protocol/schema/json`,
   `codex-rs/tui/src/history_cell` and `codex-rs/tui/src/exec_cell` — the
   protocol and the rendering rules tandem copies.
2. Install the new codex, run `uv run python tools/gen_codex_protocol.py`,
   read the generated diff (that diff is the drift report), and update
   `runtime/codex.py` / `render.py` for anything the diff changed.
3. Bump `COMPAT["codex"]` (tested and ceiling), run the suite, then
   `tools/live_gate_chat.py` (below), and record the result in
   `docs/formats.md`.

The opencode and claude clients are hand-written against
`~/git/opencode/packages/sdk/openapi.json` and the Agent SDK source; their
golden lines live in `tests/golden/chat/`.
```

`docs/formats.md` — append:

```markdown
## Chat window live gate (claude 2.1.265 / codex 0.153.4 / opencode 1.18.20)

`tools/live_gate_chat.py` drives `tandem chat` in tmux on a private socket:
a three-harness relay (claude → codex → opencode → claude) with one command
approval per harness, then checks each native session file for the other
harnesses' turns. Record each run here as `date · versions · PASS/FAIL ·
notes`.

- 2026-09-13 · spike relay, headless via the raw protocols (pre-window) · PASS ·
  see `docs/specs/2026-09-08-unified-chat-window-design.md`, Decisions.
```

- [ ] **Step 4: Write the live gate script**

```python
#!/usr/bin/env python3
# tools/live_gate_chat.py
"""Live gate for `tandem chat`: a three-harness relay with approvals, driven
in tmux on a private socket. Needs real, signed-in claude/codex/opencode.

usage: tools/live_gate_chat.py [--bin /path/to/tandem] [--proj DIR]

Steps (each waits on pane text, 60 s max):
  1. launch `tandem chat` in an empty, trusted project directory
  2. `/claude Reply with exactly MARLIN.`                    → pane shows MARLIN
  3. `/codex What word did the previous assistant say? Then run: touch gate-codex.txt`
       → approval prompt → `y` → pane shows MARLIN, file exists
  4. `/opencode:opencode/big-pickle Which words so far? Then run: touch gate-oc.txt`
       → pane shows MARLIN, file exists (permission prompt answered if shown)
  5. `Use the tool named exactly Bash to run: touch gate-claude.txt` (default is opencode → route back)
       `/claude …`                                              → approval → `y` → file exists
  6. Ctrl-C twice → exits; then every native session file is checked for `[via …]` turns.
"""
import argparse
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("--bin", default="tandem")
ap.add_argument("--proj", default=None)
a = ap.parse_args()
PROJ = Path(a.proj or tempfile.mkdtemp(prefix="tandem-chat-gate-"))
PROJ.mkdir(parents=True, exist_ok=True)
subprocess.run(["git", "init", "-q"], cwd=PROJ)
S = "chatgate"


def t(*args):
    return subprocess.run(["tmux", "-L", "chatgate", *args], capture_output=True, text=True)


def pane():
    return t("capture-pane", "-p", "-t", S).stdout


def wait(pattern, secs=60, what=""):
    t0 = time.time()
    while time.time() - t0 < secs:
        if re.search(pattern, pane()):
            print(f"  ok: {what or pattern} ({time.time() - t0:.1f}s)")
            return True
        time.sleep(0.5)
    print(f"  !! timeout waiting for {what or pattern}\n{pane()[-1500:]}")
    return False


def send(text, enter=True):
    t("send-keys", "-t", S, "-l", text)
    if enter:
        time.sleep(0.3)
        t("send-keys", "-t", S, "Enter")


failures = 0


def check(cond, what):
    global failures
    print(("  ok: " if cond else "  FAIL: ") + what)
    failures += 0 if cond else 1


t("kill-session", "-t", S)
t("new-session", "-d", "-s", S, "-x", "160", "-y", "45", "-c", str(PROJ),
  f"env -u CLAUDECODE -u CLAUDE_CODE_ENTRYPOINT {a.bin} chat; echo EXIT=$?; sleep 300")
check(wait(r"^> ", 60, "composer"), "window up")

send("/claude Reply with exactly the single word MARLIN and nothing else.")
check(wait(r"MARLIN", 120, "claude reply"), "claude turn")

send("/codex What single word did the previous assistant reply with? Then run the shell command: touch gate-codex.txt")
if wait(r"\[y\]es \[a\]lways \[n\]o", 90, "codex approval"):
    send("y", enter=False)
check(wait(r"completed", 120, "codex turn end"), "codex turn")
check((PROJ / "gate-codex.txt").exists(), "codex ran the command")

send("/opencode:opencode/big-pickle Which single words did the previous assistants reply with? Then run: touch gate-oc.txt")
for _ in range(2):
    if re.search(r"\[y\]es \[a\]lways \[n\]o", pane()):
        send("y", enter=False)
    time.sleep(2)
check(wait(r"completed", 180, "opencode turn end"), "opencode turn")
check((PROJ / "gate-oc.txt").exists(), "opencode ran the command")

send("/claude Use the tool named exactly Bash to run: touch gate-claude.txt — then reply DONE.")
if wait(r"\[y\]es \[a\]lways \[n\]o", 90, "claude approval"):
    send("y", enter=False)
check(wait(r"DONE", 120, "claude second turn"), "claude turn after opencode")
check((PROJ / "gate-claude.txt").exists(), "claude ran the command")

t("send-keys", "-t", S, "C-c"); time.sleep(0.3); t("send-keys", "-t", S, "C-c")
check(wait(r"EXIT=0", 30, "clean exit"), "exit code 0")

status = subprocess.run([a.bin, "status"], cwd=PROJ, capture_output=True, text=True).stdout
print(status)
t("kill-server")
print("PASS" if failures == 0 else f"FAIL ({failures})")
sys.exit(1 if failures else 0)
```

- [ ] **Step 5: Run the gate and record it**

Run: `uv run python tools/live_gate_chat.py --bin "$(pwd)/.venv/bin/tandem"`
Expected: `PASS`. Append the result line to the new section in `docs/formats.md`. If a step fails, that is a real finding: fix the client or renderer, rerun. The trust dialogs are not a factor (headless modes skip them), but a project directory the CLIs have never seen still gets their per-project state created.

- [ ] **Step 6: Commit**

```bash
git add src/tandem/compat.py src/tandem/doctor.py README.md docs/configuration.md docs/development.md docs/formats.md tools/live_gate_chat.py tests/test_memory_doctor.py
git commit -m "docs(chat): tandem chat docs, version pins, doctor line, live gate" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>" -m "Claude-Session: https://claude.ai/code/session_01WZ7WV9iKDBigenNSsJaRdy"
```

---

## Done when

- `uv run pytest -q` is green, `tools/live_gate_chat.py` prints `PASS`, and `docs/formats.md` carries the run.
- Runtime dependencies in `pyproject.toml` are unchanged; `uv.lock` only added the dev tool.
- The branch is ready for a PR against `main` (PR-only); the release bump (pyproject + both plugin manifests + `uv lock` in one commit) happens separately, per `docs/development.md`.
