# Chat slash typeahead, `/help`, `/compact`, `/model` — Implementation Plan (PR 1 of 4)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Typing `/` in the chat composer lists what can be typed, `/help` prints the same list, and `/compact` and `/model` work on every harness.

**Architecture:** A new pure module `chat/commands.py` owns the command catalog. The composer's `@` picker gains an explicit kind so a `/` at the start of the draft lists commands through the same list/select/accept machinery. `/compact` and `/model` are dispatcher commands, not window intercepts: the dispatcher resolves the target harness first, runs them on its worker thread, and the runtimes gain a `command` parameter plus two small methods (`list_models`, and a `harness_commands` list fed from claude's init line and opencode's `GET /command`).

**Tech Stack:** Python 3.12, pytest, the existing fake codex app-server and fake opencode server under `tests/fakes/`.

**Spec:** `docs/specs/2026-09-26-chat-parity-design.md`, section 2 ("Slash typeahead, `/help`, `/compact`, `/model`") plus "Verified facts" and "Non-goals". Sections 1, 3 and 4 are later PRs and are out of scope here.

## Global Constraints

- No new runtime dependency in this PR (`rich` arrives with PR 4).
- The window's scroll region is append-only: nothing already printed is redrawn.
- Nothing blocking (a child process, an HTTP call) runs on the main thread; every such call goes through the dispatcher worker.
- Any `/word` that is not a route and not in the catalog still reaches the current harness verbatim — nothing that works today stops working.
- Routes keep their exact grammar: `/codex:gpt-5.5`, `/codex:default`, `/opencode:openrouter/anthropic/claude-sonnet-4` submit untouched; `/codex/README.md` is not a route.
- Codex `AskForApproval` on the wire is `untrusted | on-request | never`; `thread/compact/start` returns `{}` and completes via the `thread/compacted` notification.
- Commit messages follow the repo's imperative style and end with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.
- Run the suite with `uv run pytest -q` from the worktree root; the baseline is 1400 passed on `main` at `1636ab0`.

## Review Focus

Inputs the spec implies but no test would otherwise exercise. Each has a test pinned to the owning task below.

1. `/mode` typed in full while `/model` is also in the catalog: Enter must submit `/mode`, not accept `/model`. (Task 2: `test_a_name_typed_in_full_closes_the_picker_even_with_a_longer_sibling`.)
2. `/codex /compact` — a routed compact — must run the compact on codex, not send `/compact` as text to the default harness. (Task 3: `test_a_routed_compact_runs_on_the_routed_harness`.)
3. `/compact` with trailing words (`/compact keep the API notes`) must go to the harness verbatim, never as the command. (Task 3: `test_compact_with_arguments_is_ordinary_pass_through`.)
4. `/compact` on a codex participant that has never run (no thread id) must fail with a message, not hang waiting for `thread/compacted`. (Task 5: `test_compact_without_a_thread_fails_at_once`.)
5. A `thread/compacted` notification arriving during an ordinary turn must be ignored, not end the turn. (Task 5: `test_compacted_outside_a_compact_is_ignored`.)

---

### Task 1: The command catalog and `/help`

**Files:**
- Create: `src/tandem/chat/commands.py`
- Create: `tests/test_chat_commands.py`
- Modify: `src/tandem/chat/window.py:42` (`WINDOW_COMMANDS`), `Window.__init__`, `handle_input`
- Test: `tests/test_chat_window.py` (`TestWindowCommands`)

**Interfaces:**
- Produces: `Command(name: str, description: str, origin: str)` frozen dataclass; `WINDOW: tuple[Command, ...]`; `catalog(participants, default, harness_commands: dict[str, list[Command]]) -> list[Command]`; `help_lines(cmds: list[Command]) -> list[str]`; `Window.catalog() -> list[Command]`; `Window.__init__(..., harness_commands: Callable[[], dict[str, list[Command]]] | None = None)`.
- Consumes: nothing new.

- [ ] **Step 1: Write the failing tests for the catalog**

```python
# tests/test_chat_commands.py
"""The `/` catalog: tandem's window commands, the routes, then the default
harness's own commands. Pure: no terminal, no runtime."""

from tandem.chat.commands import WINDOW, Command, catalog, help_lines


def test_window_commands_come_first_with_descriptions():
    got = catalog(["claude", "codex"], "claude", {})
    names = [c.name for c in got[: len(WINDOW)]]
    assert names == [c.name for c in WINDOW]
    assert all(c.description and c.origin == "tandem" for c in got[: len(WINDOW)])
    assert {"help", "status", "compact", "model", "skip-permissions", "note", "quit"} <= set(names)


def test_routes_follow_one_per_participant():
    got = catalog(["claude", "codex"], "claude", {})
    routes = [c for c in got if c.origin == "route"]
    assert [c.name for c in routes] == ["claude", "codex"]
    assert "pins" in routes[0].description         # says `:model` pins


def test_only_the_default_harnesss_commands_are_listed():
    hc = {"claude": [Command("deep-research", "claude command", "claude")],
          "opencode": [Command("init", "guided AGENTS.md setup", "opencode")]}
    got = catalog(["claude", "opencode"], "opencode", hc)
    tail = [c for c in got if c.origin not in ("tandem", "route")]
    assert tail == hc["opencode"]


def test_a_harness_command_that_shadows_a_window_command_is_dropped():
    hc = {"claude": [Command("compact", "x", "claude"), Command("review", "y", "claude")]}
    got = catalog(["claude"], "claude", hc)
    assert [c.name for c in got if c.origin == "claude"] == ["review"]


def test_help_lines_group_by_origin():
    got = catalog(["claude", "codex"], "codex", {"codex": []})
    lines = help_lines(got)
    assert lines[0] == "tandem:"
    assert any(l.startswith("  /help") for l in lines)
    assert "routes:" in lines
    assert lines[-1].startswith("  /codex")        # the last route; codex has no commands
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_chat_commands.py -q`
Expected: `ModuleNotFoundError: No module named 'tandem.chat.commands'`

- [ ] **Step 3: Write `chat/commands.py`**

```python
"""What a leading `/` can be: tandem's own window commands, a route per
participant, and the default harness's own commands. One list, in that
order, for the composer's picker and for `/help`. Pure — the runtimes hand
in the harness lists; nothing here reads a file or a socket."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class Command:
    name: str          # without the slash
    description: str
    origin: str        # "tandem" | "route" | a harness name


WINDOW: tuple[Command, ...] = (
    Command("help", "list what a leading / can be", "tandem"),
    Command("status", "session id, default harness, participants, model pins", "tandem"),
    Command("compact", "compact the default harness's conversation", "tandem"),
    Command("model", "list the default harness's models; /model NAME pins one", "tandem"),
    Command("skip-permissions", "[on|off] skip claude's and codex's permission prompts", "tandem"),
    Command("note", "[dismiss|good|bad] the navigator's pending note", "tandem"),
    Command("quit", "leave the window (two Ctrl-Cs do the same)", "tandem"),
)

_ROUTE_DESCRIPTION = "run the prompt here and make it the default; :model pins"


def catalog(participants: Sequence[str], default: str,
            harness_commands: dict[str, list[Command]]) -> list[Command]:
    """Window commands, then a route per participant, then the default
    harness's own commands. A harness command named like a window command
    is dropped: the window's wins, and one row per name keeps Enter
    unambiguous."""
    out = list(WINDOW)
    out += [Command(h, _ROUTE_DESCRIPTION, "route") for h in participants]
    taken = {c.name for c in out}
    out += [c for c in harness_commands.get(default, []) if c.name not in taken]
    return out


def help_lines(cmds: list[Command]) -> list[str]:
    """`/help`: one row per command under a heading per origin."""
    width = max((len(c.name) for c in cmds), default=0) + 1
    lines: list[str] = []
    for origin in dict.fromkeys(c.origin for c in cmds):
        lines.append("tandem:" if origin == "tandem" else "routes:" if origin == "route" else f"{origin}:")
        lines += [f"  /{c.name}".ljust(width + 3) + " " + c.description
                  for c in cmds if c.origin == origin]
    return lines
```

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/test_chat_commands.py -q`
Expected: 5 passed

- [ ] **Step 5: Write the failing window test for `/help`**

Add to `TestWindowCommands` in `tests/test_chat_window.py`:

```python
    def test_help_prints_the_catalog_and_runs_nothing(self, env_factory):
        env = env_factory(); w, d, out, _ = make_window(env)
        assert w.handle_input(b"/help\r") is True
        assert d.submitted == []
        text = out.text()
        assert "tandem:" in text and "/help" in text and "/compact" in text
        assert "routes:" in text and "/claude" in text and "/codex" in text

    def test_help_lists_the_default_harnesss_own_commands(self, env_factory):
        env = env_factory()
        w, d, out, _ = make_window(env, harness_commands=lambda: {
            "claude": [Command("deep-research", "claude command", "claude")]})
        w.handle_input(b"/help\r")
        assert "claude:" in out.text() and "/deep-research" in out.text()
```

Add the import at the top of the test file: `from tandem.chat.commands import Command`. Extend `make_window` to accept and pass `harness_commands`:

```python
def make_window(env, cfg=None, stdin_fd=None, clock=None, harness_commands=None):
    ...
    w = Window(env.session, env.store, cfg, screen, Composer(), d, answers, bar, {"limits": {}}, {},
               stdin_fd=stdin_fd, harness_commands=harness_commands,
               **({"clock": clock} if clock else {}))
```

- [ ] **Step 6: Run to verify it fails**

Run: `uv run pytest tests/test_chat_window.py -k help -q`
Expected: FAIL — `TypeError: Window.__init__() got an unexpected keyword argument 'harness_commands'`

- [ ] **Step 7: Wire `/help` into the window**

In `src/tandem/chat/window.py`:

```python
from .commands import Command, catalog, help_lines          # new import

WINDOW_COMMANDS = ("/quit", "/status", "/skip-permissions", "/note", "/help")
```

In `Window.__init__`, add the keyword parameter and store it:

```python
    def __init__(self, session, store, cfg, screen: Screen, composer: Composer,
                 dispatcher, answers: WindowAnswers, bar: StatusBar, usage_state: dict,
                 meters: dict, poller: RateLimitPoller | None = None,
                 stdin_fd: int | None = None, clock: Callable[[], float] = time.monotonic,
                 navigator=None,
                 harness_commands: Callable[[], dict[str, list[Command]]] | None = None):
        ...
        self._harness_commands = harness_commands or (lambda: {})
```

Add the method next to `status_line`:

```python
    def catalog(self) -> list[Command]:
        """What `/` can be right now: tandem's, the routes, and the default
        harness's own commands as its runtime last reported them."""
        return catalog(list(self.session.participants), self.dispatcher.default,
                       self._harness_commands())
```

In `handle_input`, after the `/status` branch:

```python
                if command == "/help":
                    for line in help_lines(self.catalog()):
                        self.screen.note(line)
                    continue
```

- [ ] **Step 8: Run the window and command tests**

Run: `uv run pytest tests/test_chat_window.py tests/test_chat_commands.py -q`
Expected: all passed

- [ ] **Step 9: Commit**

```bash
git add src/tandem/chat/commands.py tests/test_chat_commands.py src/tandem/chat/window.py tests/test_chat_window.py
git commit -m "Add the chat command catalog and /help

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: The `/` picker in the composer

**Files:**
- Modify: `src/tandem/chat/composer.py` (docstring, `__init__`, `_escape`/Esc handling in `feed`, `_mention` → `_locate`, `candidates`, `_sync_picker`, `_accept`, `_set`, `rows`)
- Test: `tests/test_chat_composer.py`

**Interfaces:**
- Consumes: `Command` from Task 1.
- Produces: `Composer(history_limit=200, paths=None, commands: Callable[[], list[Command]] | None = None)`; `Composer.candidates -> list[str] | list[Command]`; `Composer.picker_kind -> str` (`""`, `"path"`, `"command"`).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_chat_composer.py`:

```python
from tandem.chat.commands import Command

CMDS = [Command("help", "list what / can be", "tandem"), Command("mode", "set the mode", "tandem"),
        Command("model", "list models", "tandem"), Command("codex", "route", "route"),
        Command("deep-research", "claude command", "claude")]


def commanding(paths=PATHS, cmds=CMDS):
    return Composer(paths=lambda: list(paths), commands=lambda: list(cmds))


def test_a_leading_slash_opens_the_command_picker():
    c = commanding()
    feed(c, "/")
    assert c.picker_kind == "command"
    assert [x.name for x in c.candidates] == [x.name for x in CMDS]
    feed(c, "mo")
    assert [x.name for x in c.candidates] == ["mode", "model"]


def test_command_matching_is_prefix_and_case_insensitive():
    c = commanding()
    feed(c, "/DEEP")
    assert [x.name for x in c.candidates] == ["deep-research"]


def test_a_slash_not_at_the_start_is_text():
    c = commanding()
    feed(c, "see /help")
    assert c.candidates == [] and c.picker_kind == ""


def test_without_a_command_source_a_slash_is_just_text():
    c = picking()
    feed(c, "/he")
    assert c.candidates == []
    assert feed(c, "\r") == [Submit("/he")]


def test_accepting_a_command_inserts_it_with_a_space_and_closes():
    c = commanding()
    feed(c, "/he")
    assert feed(c, "\t") == []
    assert c.text == "/help " and c.cur == 6 and c.candidates == []
    assert feed(c, "\r") == [Submit("/help ")]


def test_enter_with_the_picker_open_accepts_the_selection():
    c = commanding()
    feed(c, "/mo")
    feed(c, b"\x1b[B")                                # down → model
    assert feed(c, "\r") == []
    assert c.text == "/model "


def test_a_name_typed_in_full_closes_the_picker_even_with_a_longer_sibling():
    c = commanding()
    feed(c, "/mode")
    assert c.candidates == []                          # not ["model"]
    assert feed(c, "\r") == [Submit("/mode")]


def test_a_route_with_a_model_closes_the_picker_and_submits_raw():
    c = commanding()
    feed(c, "/codex:gpt-5.5 go")
    assert c.candidates == []
    assert feed(c, "\r") == [Submit("/codex:gpt-5.5 go")]


def test_a_path_like_slash_word_is_not_a_command():
    c = commanding()
    feed(c, "/codex/README.md")
    assert c.candidates == []


def test_after_the_first_word_the_at_picker_still_works():
    c = commanding()
    feed(c, "/codex look at @ren")
    assert c.picker_kind == "path"
    assert c.candidates == ["docs/render-notes.md", "src/tandem/chat/render.py",
                            "src/tandem/chat/window.py"]


def test_esc_closes_the_command_picker_without_interrupting():
    c = commanding()
    feed(c, "/he")
    assert feed(c, b"\x1b") == []
    assert c.candidates == [] and c.text == "/he"


def test_command_rows_show_the_name_padded_and_the_description():
    c = commanding()
    feed(c, "/mo")
    rows, r, col = c.rows(60, 8)
    assert rows[0] == "> /mo"
    assert rows[1].startswith("  ❯ /mode ") and "set the mode" in rows[1]
    assert rows[2].startswith("    /model") and "list models" in rows[2]


def test_a_recalled_command_does_not_reopen_the_picker():
    c = commanding()
    feed(c, "/help\r")
    feed(c, b"\x1b[A")                                 # up: recall
    assert c.text == "/help" and c.candidates == []
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_chat_composer.py -q -k "command or slash or recalled_command or first_word"`
Expected: FAIL — `TypeError: Composer.__init__() got an unexpected keyword argument 'commands'` (and `AttributeError: picker_kind`)

- [ ] **Step 3: Generalize the picker**

In `src/tandem/chat/composer.py`:

Docstring: replace the final paragraph ("A word that starts with `@` …") with:

```
Two pickers share one list under the draft. A word that starts with `@`
is a file mention: while the cursor is on one, the paths that match it are
listed, Up/Down choose, Tab or Enter puts the choice in the draft, Esc
closes the list. A `/` at the very start of the draft, with the cursor
still inside that first word, lists commands the same way — tandem's, the
routes, the harness's own — matched by name prefix; a first word that is
not a bare name (`/codex:gpt-5.5`, `/codex/README.md`) is not a command
and closes the list, so a route submits untouched. A namespaced claude
command (`plugin:name`) is listed while the query is still before its
colon; accepting it inserts the whole name. Both lists come from callables
the window hands in; nothing here reads the filesystem, and the mention or
command is sent as written.
```

Imports and constants:

```python
from .commands import Command
...
_CMD_NAME = re.compile(r"[A-Za-z0-9_-]*")
```

`__init__` — replace the picker fields:

```python
    def __init__(self, history_limit: int = 200,
                 paths: Callable[[], list[str]] | None = None,
                 commands: Callable[[], list[Command]] | None = None):
        ...
        self._list_paths = paths
        self._list_commands = commands
        self._items: list = []                       # what the open picker was listed from
        self._listed_at: tuple[str, int] | None = None   # (kind, start) the items were listed for
        self._dismissed: int | None = None          # where the word Esc closed the picker on starts
        self._matched: tuple[str, int, str] | None = None
        self._matches: list = []
        self._kind = ""
        self.selected = 0
        self._pick_top = 0
```

Replace `_mention` with `_locate`:

```python
    def _locate(self) -> tuple[str, int, str] | None:
        """(picker kind, where its word starts, the query up to the cursor),
        in prompt mode. A `/` at index 0 with the cursor inside that first
        word is a command when the word is a bare name; an `@` word under
        the cursor anywhere is a path."""
        if self.mode != "prompt":
            return None
        if self.buf and self.buf[0] == "/" and self._list_commands is not None:
            end = 1
            while end < len(self.buf) and not self.buf[end].isspace():
                end += 1
            if 0 < self.cur <= end:
                word = "".join(self.buf[1:end])
                if _CMD_NAME.fullmatch(word):
                    return "command", 0, "".join(self.buf[1:self.cur])
                return None
        if self._list_paths is None:
            return None
        start = self.cur
        while start > 0 and not self.buf[start - 1].isspace():
            start -= 1
        if start == self.cur or self.buf[start] != "@":
            return None
        return "path", start, "".join(self.buf[start + 1:self.cur])

    @property
    def picker_kind(self) -> str:
        self._sync_picker()
        return self._kind
```

Replace `_sync_picker`:

```python
    def _sync_picker(self) -> list:
        """Bring the picker in line with the draft. Items are listed once per
        word, not once per keystroke; the selection starts over when the
        query changes. A command typed out in full is not offered back —
        Enter has to submit it — and that includes a full name with a longer
        sibling (`/mode` beside `/model`)."""
        loc = self._locate()
        if loc is None:
            self._listed_at = self._dismissed = None
            self._kind = ""
            return []
        kind, start, query = loc
        if start == self._dismissed:
            self._kind = ""
            return []
        self._dismissed = None
        if (kind, start) != self._listed_at:
            self._items = list(self._list_paths() if kind == "path" else self._list_commands())
            self._listed_at, self._matched = (kind, start), None
        if loc != self._matched:
            if kind == "path":
                self._matches = [p for p in match(query, self._items, _PICKER_MATCHES + 1)
                                 if p != query][:_PICKER_MATCHES]
            else:
                q = query.lower()
                self._matches = ([] if any(c.name == query for c in self._items)
                                 else [c for c in self._items if c.name.lower().startswith(q)])
            self._matched, self.selected, self._pick_top = loc, 0, 0
        self._kind = kind
        return self._matches
```

Replace `_accept`:

```python
    def _accept(self) -> None:
        """The chosen item takes the word's place. A directory leaves the
        cursor on it, so the picker goes on into it; a command gets a
        trailing space, so the picker closes and the prompt can follow."""
        kind, start, _ = self._locate()
        pick = self.candidates[self.selected]
        end = self.cur
        while end < len(self.buf) and not self.buf[end].isspace():
            end += 1
        if kind == "command":
            text = f"/{pick.name} "
        else:
            text = f'@"{pick}"' if any(ch.isspace() for ch in pick) else "@" + pick
            if not pick.endswith("/"):
                text += " "
        self.buf[start:end] = list(text)
        self.cur = start + len(text)
```

In `feed`, the Esc branch uses `self._mention()[0]` — change to `self._locate()[1]`. In `_set`, replace the two-line mention check with:

```python
        loc = self._locate()
        self._dismissed = loc[1] if loc else None
```

In `rows`, replace the picker-row comprehension:

```python
        self._pick_top = min(max(self._pick_top, self.selected - n + 1), self.selected)
        if self._kind == "command":
            width = max((len(c.name) for c in picks), default=0) + 2
            shown += [("  ❯ " if i == self.selected else "    ")
                      + f"/{picks[i].name}".ljust(width) + " " + _printable(picks[i].description)
                      for i in range(self._pick_top, self._pick_top + n)]
        else:
            shown += [("  ❯ " if i == self.selected else "    ") + _printable(picks[i])
                      for i in range(self._pick_top, self._pick_top + n)]
```

- [ ] **Step 4: Run the whole composer file**

Run: `uv run pytest tests/test_chat_composer.py -q`
Expected: all passed — the existing `@` tests included

- [ ] **Step 5: Commit**

```bash
git add src/tandem/chat/composer.py tests/test_chat_composer.py
git commit -m "Open a command picker on a leading slash in the chat composer

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3: Dispatcher commands — `/compact`, `/model`, and the `Notice` event

**Files:**
- Modify: `src/tandem/chat/events.py` (new `Notice`, union), `src/tandem/chat/dispatch.py` (`Pending`, `submit`, `_run`), `src/tandem/chat/runtime/__init__.py` (`RuntimeClient`), `src/tandem/chat/window.py` (`handle_event`)
- Test: `tests/test_chat_dispatch.py`, `tests/test_chat_window.py`

**Interfaces:**
- Produces: `Notice(text: str)` live event; `Pending(harness, model, prompt, command: str = "")`; `RuntimeClient.run_turn(session, native_id, prompt, model, emit, answers, command: str = "")`; `RuntimeClient.list_models(session) -> list[str]`; `RuntimeClient.harness_commands: list[Command]`; `Dispatcher.submit` recognizes `/compact` (exact) and `/model [NAME]` after the route.
- Consumes: `Command` (Task 1).

- [ ] **Step 1: Write the failing dispatcher tests**

Extend `FakeRuntime` in `tests/test_chat_dispatch.py`:

```python
    def __init__(self, harness, env, *, block=None, fresh_id=None, half_turn=False, models=None):
        ...
        self.models = models if models is not None else [f"{harness}-a  first", f"{harness}-b  second"]
        self.harness_commands = []

    def run_turn(self, session, native_id, prompt, model, emit, answers, command=""):
        self.calls.append((native_id, prompt, model) if not command else (native_id, prompt, model, command))
        ...   # unchanged body

    def list_models(self, session):
        if self.models is None:
            raise RuntimeError("no catalog")
        return list(self.models)
```

Add the tests:

```python
from tandem.chat.events import Notice


def collect(env, runtimes=None, **kw):
    events, done = [], threading.Event()

    def emit(ev):
        events.append(ev)
        if isinstance(ev, Idle):
            done.set()

    runtimes = runtimes or {"claude": FakeRuntime("claude", env), "codex": FakeRuntime("codex", env)}
    return Dispatcher(env.store, env.session, runtimes, emit, Answers(), **kw), runtimes, events, done


def test_model_alone_lists_the_default_harnesss_models_as_a_notice(env_factory):
    env = env_factory()
    d, runtimes, events, done = collect(env)
    assert d.submit("/model") == ""
    assert done.wait(5)
    notices = [e for e in events if isinstance(e, Notice)]
    assert len(notices) == 1 and "claude-a  first" in notices[0].text
    assert not any(isinstance(e, TurnStarted) for e in events)
    assert runtimes["claude"].calls == []                      # no turn ran


def test_model_marks_the_pinned_one(env_factory):
    env = env_factory()
    env.store.set_pin(env.session.tandem_id, "claude", "claude-b")
    d, runtimes, events, done = collect(env)
    d.submit("/model"); assert done.wait(5)
    text = next(e for e in events if isinstance(e, Notice)).text
    assert "* claude-b" in text and "  claude-a" in text


def test_model_with_a_name_is_the_pin_route(env_factory):
    env = env_factory()
    d, runtimes, events, done = collect(env)
    assert d.submit("/model haiku") == "default → claude · haiku"
    assert d.pin("claude") == "haiku"


def test_model_listing_failure_is_a_failure_event(env_factory):
    env = env_factory()
    rts = {"claude": FakeRuntime("claude", env, models=None), "codex": FakeRuntime("codex", env)}
    d, runtimes, events, done = collect(env, rts)
    d.submit("/model"); assert done.wait(5)
    assert any(isinstance(e, Failure) and "no catalog" in e.message for e in events)


def test_compact_runs_as_a_command_turn(env_factory):
    env = env_factory()
    d, runtimes, events, done = collect(env)
    assert d.submit("/compact") == ""
    assert done.wait(5)
    assert runtimes["claude"].calls[-1][1:] == ("/compact", "", "compact")
    started = next(e for e in events if isinstance(e, TurnStarted))
    assert started.prompt == "/compact" and started.harness == "claude"


def test_a_routed_compact_runs_on_the_routed_harness(env_factory):
    env = env_factory()
    d, runtimes, events, done = collect(env)
    d.submit("/codex /compact"); assert done.wait(5)
    assert runtimes["codex"].calls[-1][3] == "compact"
    assert runtimes["claude"].calls == []


def test_compact_with_arguments_is_ordinary_pass_through(env_factory):
    env = env_factory()
    d, runtimes, events, done = collect(env)
    d.submit("/compact keep the API notes"); assert done.wait(5)
    assert runtimes["claude"].calls[-1] == (env.session.native_id("claude"), "/compact keep the API notes", "")


def test_compact_takes_no_navigator_note_and_reports_no_facts(env_factory):
    env = env_factory()
    nav = StubNavigator(make_note())
    d, runtimes, events, done = collect(env, navigator=nav)
    d.submit("/codex /compact"); assert done.wait(5)
    assert nav.takes == [] and nav.ended == []
    assert nav.note is not None                                 # still waiting for a real prompt
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_chat_dispatch.py -q -k "model or compact"`
Expected: FAIL — `ImportError: cannot import name 'Notice'`

- [ ] **Step 3: Add `Notice` and the protocol changes**

`src/tandem/chat/events.py`, after `Failure`:

```python
@dataclass(frozen=True)
class Notice:
    """Text the window prints dim, outside any turn: a `/model` listing, a
    command's one-line result. Never transcript content."""
    text: str
```

and add `Notice` to the `LiveEvent` union.

`src/tandem/chat/runtime/__init__.py`:

```python
from ..commands import Command      # new import, below the events import


class RuntimeClient(Protocol):
    harness: str
    harness_commands: list[Command]     # the harness's own slash commands, as last reported

    def run_turn(self, session, native_id: str | None, prompt: str, model: str,
                 emit: Callable[[LiveEvent], None], answers: Answers,
                 command: str = "") -> TurnOutcome: ...

    def list_models(self, session) -> list[str]: ...    # one display line per model, name first

    def interrupt(self) -> None: ...

    def close(self) -> None: ...
```

- [ ] **Step 4: Teach the dispatcher the two commands**

`src/tandem/chat/dispatch.py`:

```python
from .events import (Answers, Failure, Idle, LiveEvent, Notice, TurnFinished, TurnOutcome,
                     TurnStarted)


@dataclass(frozen=True)
class Pending:
    harness: str
    model: str
    prompt: str
    command: str = ""      # "" for a prompt; "compact" | "models" for a window command


def parse_command(prompt: str) -> tuple[str, str] | None:
    """(command, argument) when the prompt is one of tandem's dispatcher
    commands, else None. `/compact` is the whole prompt or nothing —
    `/compact focus on X` is claude's own form and goes through as text."""
    head = prompt.split(maxsplit=1)
    if not head:
        return None
    if head[0] == "/compact" and len(head) == 1:
        return "compact", ""
    if head[0] == "/model":
        return "models", head[1].strip() if len(head) > 1 else ""
    return None
```

In `submit`, after the `if got is None … else …` block and before `item = Pending(...)`:

```python
        command = ""
        parsed = parse_command(prompt)
        if parsed is not None:
            command, arg = parsed
            if command == "models" and arg:
                return self.submit(f"/{harness}:{arg}")     # `/model NAME` is the pin route
            if command == "compact":
                prompt = "/compact"
        item = Pending(harness, self.pin(harness), prompt, command)
```

In `_run`, at the very top, before `note = …`:

```python
        if item.command == "models":
            self._list_models(item)
            return
        note = nav.take(harness) if nav is not None and not item.command else None
```

and keep the `FactsCollector` line guarded: `if nav is not None and not item.command:`. Pass the command to the runtime:

```python
            outcome = self.runtimes[harness].run_turn(
                session, session.native_id(harness), prompt, item.model, emit, self.answers,
                command=item.command)
```

Add the worker-side listing:

```python
    def _list_models(self, item: Pending) -> None:
        """`/model` with no name: ask the runtime, on this worker, and hand
        the rows back as one Notice. No transcript is touched, so none of
        the turn pipeline runs."""
        try:
            pin = self.pin(item.harness)
            rows = self.runtimes[item.harness].list_models(self.session)
            marked = [("* " if pin and row.split()[0] == pin else "  ") + row for row in rows]
            self.emit(Notice("\n".join(marked) if marked else f"{item.harness}: no models listed"))
        except Exception as exc:                       # a listing must never take the window down
            self.emit(Failure(f"{item.harness} models: {exc}"))
        finally:
            with self._lock:
                self._current = None
                self._running = False
            self.emit(Idle())
```

- [ ] **Step 5: Paint the notice in the window**

`src/tandem/chat/window.py` — import `Notice` and in `handle_event`, after the `Failure` branch:

```python
        elif isinstance(ev, Notice):
            for line in ev.text.split("\n"):
                s.note(line)
```

Add to `tests/test_chat_window.py`:

```python
def test_a_notice_paints_each_line_dim(env_factory):
    env = env_factory(); w, d, out, _ = make_window(env)
    w.handle_event(Notice("one\ntwo"))
    assert "one" in out.text() and "two" in out.text()
```

(import `Notice` alongside the other events.)

- [ ] **Step 6: Run dispatch and window tests**

Run: `uv run pytest tests/test_chat_dispatch.py tests/test_chat_window.py -q`
Expected: all passed

- [ ] **Step 7: Commit**

```bash
git add src/tandem/chat/events.py src/tandem/chat/dispatch.py src/tandem/chat/runtime/__init__.py src/tandem/chat/window.py tests/test_chat_dispatch.py tests/test_chat_window.py
git commit -m "Run /compact and /model through the chat dispatcher

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 4: Claude runtime — commands from init, `/compact` as text, model families

**Files:**
- Modify: `src/tandem/chat/runtime/claude.py`
- Test: `tests/test_chat_claude.py`

**Interfaces:**
- Produces: `ClaudeRuntime.harness_commands: list[Command]` (built-ins `compact`, `context` first, then every name from the last `system/init` `slash_commands`); `run_turn(..., command="compact")` sends the text `/compact`; `list_models(session)` returns one line per `modelcat.CLAUDE_FAMILIES` entry.
- Consumes: `Command`, protocol from Task 3.

- [ ] **Step 1: Write the failing tests**

```python
from tandem.chat.commands import Command


def test_builtins_are_listed_before_any_turn():
    rt = ClaudeRuntime(ChatConfig())
    assert [c.name for c in rt.harness_commands] == ["compact", "context"]
    assert all(c.origin == "claude" for c in rt.harness_commands)


def test_init_line_adds_the_sessions_slash_commands():
    rt = ClaudeRuntime(ChatConfig()); rec = Recorder()
    rt.handle_line({"type": "system", "subtype": "init", "session_id": "s",
                    "slash_commands": ["deep-research", "compact", "tandem:switch"]},
                   rec.emit, rec, lambda _: None)
    assert [c.name for c in rt.harness_commands] == ["compact", "context", "deep-research", "tandem:switch"]


def test_a_child_init_does_not_replace_the_list():
    rt = ClaudeRuntime(ChatConfig()); rec = Recorder()
    rt.handle_line({"type": "system", "subtype": "init", "session_id": "s", "slash_commands": ["x"]},
                   rec.emit, rec, lambda _: None)
    rt.handle_line({"type": "system", "subtype": "init", "session_id": "c", "parent_tool_use_id": "t",
                    "slash_commands": []}, rec.emit, rec, lambda _: None)
    assert [c.name for c in rt.harness_commands][-1] == "x"


def test_golden_init_populates_the_commands():
    rt = ClaudeRuntime(ChatConfig()); rec = Recorder()
    for line in GOLDEN.read_text().splitlines():
        rt.handle_line(json.loads(line), rec.emit, rec, lambda _: None)
    assert "deep-research" in [c.name for c in rt.harness_commands]


def test_compact_command_sends_the_slash_text(env, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", "text")
    rec = Recorder()
    env.runtime.run_turn(env.session, env.sid, "ignored", "", rec.emit, rec, command="compact")
    sent = json.loads(Path(os.environ["FAKE_STDIN_OUT"]).read_text().splitlines()[0])
    assert sent["message"]["content"][0]["text"] == "/compact"


def test_list_models_is_the_family_aliases():
    rows = ClaudeRuntime(ChatConfig()).list_models(None)
    assert [r.split()[0] for r in rows] == ["fable", "opus", "sonnet", "haiku"]
```

Check the `env` fixture and `tests/fakes/fake_claude.py` for the exact env-var name that records what the fake read on stdin (the `text` scenario and a stdin capture already exist for `test_text_only_turn`); if the fake records under another name, use that name in `test_compact_command_sends_the_slash_text`. If it records nothing, add to the fake, right where it reads the first stdin line:

```python
if os.environ.get("FAKE_STDIN_OUT"):
    with open(os.environ["FAKE_STDIN_OUT"], "a") as f:
        f.write(line)
```

and set `FAKE_STDIN_OUT` in the `env` fixture like `FAKE_ARGV_OUT`.

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_chat_claude.py -q -k "builtins or init_line or child_init or golden_init or compact_command or list_models"`
Expected: FAIL — `AttributeError: 'ClaudeRuntime' object has no attribute 'harness_commands'`

- [ ] **Step 3: Implement**

`src/tandem/chat/runtime/claude.py`:

```python
from ... import modelcat
from ...commands import Command       # note: ..commands relative to chat/, i.e. `from ..commands import Command`

_BUILTINS = (Command("compact", "claude built-in: compact the conversation", "claude"),
             Command("context", "claude built-in: show context usage", "claude"))
```

(Use `from ..commands import Command` — `commands.py` lives in `chat/`, one level up from `runtime/`.)

In `__init__`: `self.harness_commands: list[Command] = list(_BUILTINS)`.

In `handle_line`, the init branch becomes:

```python
        if t == "system" and m.get("subtype") == "init" and not child:
            if self.on_init is not None and m.get("session_id"):
                self.on_init(str(m["session_id"]))
            names = m.get("slash_commands")
            if isinstance(names, list):
                taken = {c.name for c in _BUILTINS}
                self.harness_commands = list(_BUILTINS) + [
                    Command(n, "claude command", "claude") for n in names
                    if isinstance(n, str) and n and n not in taken]
            return None
```

`run_turn` signature gains `command: str = ""`; right after the assert:

```python
        if command == "compact":
            prompt = "/compact"        # verified 2026-09-26: headless claude runs the built-in from text
```

Add:

```python
    def list_models(self, session) -> list[str]:
        return [f"{f}  claude's alias for the latest {f}" for f in modelcat.CLAUDE_FAMILIES]
```

- [ ] **Step 4: Run the claude tests**

Run: `uv run pytest tests/test_chat_claude.py -q`
Expected: all passed

- [ ] **Step 5: Commit**

```bash
git add src/tandem/chat/runtime/claude.py tests/test_chat_claude.py tests/fakes/fake_claude.py
git commit -m "Claude runtime: report slash commands from init, run /compact, list families

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 5: Codex runtime — `thread/compact/start` and `model/list`

**Files:**
- Modify: `src/tandem/chat/runtime/codex.py` (`__init__`, new `_spawn`/`_teardown`, `run_turn`, `handle`, new `list_models`)
- Modify: `tests/fakes/fake_codex_appserver.py` (`compact` scenario, `model/list`)
- Test: `tests/test_chat_codex.py`

**Interfaces:**
- Produces: `CodexRuntime.harness_commands = []`; `run_turn(..., command="compact")`; `list_models(session)`.
- Consumes: protocol from Task 3.

- [ ] **Step 1: Extend the fake app-server**

In `tests/fakes/fake_codex_appserver.py` `main()` loop, add two branches before the final `elif rid == 7` chain:

```python
        elif meth == "thread/compact/start":
            out({"jsonrpc": "2.0", "id": rid, "result": {}})
            if scenario == "compactsilent":
                continue                        # never says compacted: the caller's timeout owns it
            notify("thread/compacted", {"threadId": m["params"]["threadId"]})
        elif meth == "model/list":
            out({"jsonrpc": "2.0", "id": rid, "result": {"nextCursor": None, "data": [
                {"id": "gpt-5.5", "model": "gpt-5.5", "displayName": "GPT-5.5", "description": "",
                 "hidden": False, "isDefault": True, "defaultReasoningEffort": "medium",
                 "supportedReasoningEfforts": []},
                {"id": "gpt-secret", "model": "gpt-secret", "displayName": "Hidden", "description": "",
                 "hidden": True, "isDefault": False, "defaultReasoningEffort": "medium",
                 "supportedReasoningEfforts": []}]}})
```

- [ ] **Step 2: Write the failing tests**

```python
def test_compact_sends_compact_start_instead_of_turn_start(env, monkeypatch):
    monkeypatch.setenv("FAKE_CODEX_SCENARIO", "compact")
    rec = Recorder()
    out = env.runtime.run_turn(env.session, "thread-1", "/compact", "", rec.emit, rec, command="compact")
    assert out.status == "completed"
    assert env.params("thread/compact/start") == {"threadId": "thread-1"}
    assert env.params("turn/start") is None
    assert rec.kinds()[-1] == "TurnFinished"


def test_compact_without_a_thread_fails_at_once(env):
    rec = Recorder()
    out = env.runtime.run_turn(env.session, None, "/compact", "", rec.emit, rec, command="compact")
    assert out.status == "failed" and "never run" in out.error
    assert env.params("thread/start") is None


def test_compacted_outside_a_compact_is_ignored():
    rt = CodexRuntime(ChatConfig()); rec = Recorder()
    rt._thread_id = "t"
    assert rt.handle({"method": "thread/compacted", "params": {"threadId": "t"}},
                     lambda _: None, rec.emit, rec) is None
    assert rec.events == []


def test_list_models_skips_hidden_ones(env):
    rows = env.runtime.list_models(env.session)
    assert rows == ["gpt-5.5  GPT-5.5"]
    assert env.params("model/list") == {}
    assert env.params("thread/resume") is None and env.params("thread/start") is None
```

- [ ] **Step 3: Run to verify they fail**

Run: `uv run pytest tests/test_chat_codex.py -q -k "compact or list_models"`
Expected: FAIL — `TypeError: run_turn() got an unexpected keyword argument 'command'`

- [ ] **Step 4: Refactor the spawn out of `run_turn`, then implement**

In `CodexRuntime.__init__` add:

```python
        self.harness_commands: list = []      # the app-server has no text-level skill invocation
        self._compacting = False
```

Add the two helpers above `run_turn` (moved verbatim from its head and tail):

```python
    def _spawn(self, cwd: str, tandem_id: str | None):
        """One app-server child with its stderr tail, and a queue its stdout
        lines land on. `None` on the queue is EOF."""
        proc = subprocess.Popen(
            [*self.binary, "app-server"], cwd=cwd,
            env=child_env(tandem_id=tandem_id),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1, start_new_session=True,
        )
        with self._lock:
            self._proc = proc
        tail: deque[str] = deque(maxlen=20)
        drain = threading.Thread(target=lambda: [tail.append(l.rstrip()) for l in proc.stderr],
                                 name="tandem-chat-codex-stderr", daemon=True)
        drain.start()
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

        pump = threading.Thread(target=reader, name="tandem-chat-codex-reader", daemon=True)
        pump.start()
        return proc, q, tail, drain, pump

    def _teardown(self, proc, drain, pump, soft_timeout: float = 5.0) -> None:
        terminate(proc, soft=lambda: proc.stdin.close(), soft_timeout=soft_timeout)
        drain.join(2.0)         # the tail below must be the whole of stderr
        pump.join(2.0)          # neither reader outlives the turn it reads
        with self._lock:
            self._proc = None

    def _init_params(self) -> dict:
        return cp.InitializeParams(clientInfo=cp.ClientInfo(name="tandem", version=_VERSION)) \
            .model_dump(by_alias=True, exclude_none=True)
```

`run_turn` becomes (signature plus the changed middle; the `fail` closure, the resume/start block and the read loop stay as they are):

```python
    def run_turn(self, session, native_id: str | None, prompt: str, model: str,
                 emit: Callable[[LiveEvent], None], answers: Answers,
                 command: str = "") -> TurnOutcome:
        self._interrupted = False
        self._compacting = command == "compact"
        ...  # the existing resets
        if self._compacting and not native_id:
            msg = "nothing to compact: codex has never run in this session"
            emit(Failure(msg)); emit(TurnFinished("failed", ""))
            return TurnOutcome("failed", msg)
        proc, q, tail, drain, pump = self._spawn(session.cwd, session.tandem_id)
        send = lambda obj: self._write(proc, obj)
        ...
        try:
            r = self._call(proc, q, "initialize", self._init_params(), emit, answers)
            ...  # initialized, overrides, thread/resume or thread/start — unchanged
            self._thread_id = thread_id
            if self._compacting:
                r = self._call(proc, q, "thread/compact/start", {"threadId": thread_id}, emit, answers)
                if "error" in r:
                    return fail(str(r["error"].get("message", r["error"])))
            else:
                turn = cp.TurnStartParams(...)      # unchanged
                ...
            while True:                             # unchanged read loop
                ...
        finally:
            self._teardown(proc, drain, pump)
        ...  # unchanged outcome fallback
```

In `handle`, right after the line `child = bool(self._thread_id and thread and thread != self._thread_id)`:

```python
        if method == "thread/compacted":
            # terminal only for a compact this runtime started; an ordinary
            # turn that compacts on its own reports it as a contextCompaction
            # item and ends with turn/completed as ever
            if self._compacting and not child:
                emit(TurnFinished("completed", self._usage))
                return TurnOutcome("completed")
            return None
```

Add `list_models`:

```python
    def list_models(self, session) -> list[str]:
        """`model/list` on a thread-less app-server: one line per visible
        model, the slug first so the pin can be matched against it."""
        proc, q, tail, drain, pump = self._spawn(session.cwd, session.tandem_id)
        quiet = lambda ev: None
        try:
            r = self._call(proc, q, "initialize", self._init_params(), quiet, None)
            if "error" in r:
                raise RuntimeError(f"initialize failed: {r['error'].get('message', r['error'])}")
            self._write(proc, {"jsonrpc": "2.0", "method": "initialized"})
            r = self._call(proc, q, "model/list", {}, quiet, None)
            if "error" in r:
                raise RuntimeError(str(r["error"].get("message", r["error"])))
            data = (r.get("result") or {}).get("data") or []
            return [f"{m.get('model') or m.get('id')}  {m.get('displayName') or ''}".rstrip()
                    for m in data if isinstance(m, dict) and not m.get("hidden")]
        finally:
            self._teardown(proc, drain, pump, soft_timeout=2.0)
```

- [ ] **Step 5: Run the codex tests**

Run: `uv run pytest tests/test_chat_codex.py -q`
Expected: all passed, the pre-existing ones included

- [ ] **Step 6: Commit**

```bash
git add src/tandem/chat/runtime/codex.py tests/fakes/fake_codex_appserver.py tests/test_chat_codex.py
git commit -m "Codex runtime: compact via thread/compact/start, list models via model/list

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 6: Opencode runtime — commands, the command endpoint, summarize, models

**Files:**
- Modify: `src/tandem/chat/runtime/opencode.py` (`__init__`, new `_load_commands`, `run_turn`, new `list_models`)
- Modify: `tests/fakes/fake_opencode_server.py` (`GET /command`, `GET /api/model`, `POST /session/{id}/command`, `POST /session/{id}/summarize`)
- Test: `tests/test_chat_opencode.py`

**Interfaces:**
- Produces: `OpencodeRuntime.harness_commands: list[Command]` (filled from `GET /command` once per server, on the worker); `run_turn(..., command="compact")` posts `/summarize`; a prompt whose first word is `/name` for a listed command posts `/command`; `list_models(session)` from `GET /api/model`.
- Consumes: protocol from Task 3.

- [ ] **Step 1: Extend the fake server**

In `do_GET`, before the 404:

```python
                if self.path == "/command":
                    return self._json(200, [{"name": "init", "description": "guided AGENTS.md setup",
                                             "source": "command", "template": "..."},
                                            {"name": "review", "description": "review changes",
                                             "source": "command", "template": "..."}])
                if self.path == "/api/model":
                    return self._json(200, {"location": {}, "data": [
                        {"id": "big-pickle", "providerID": "opencode", "name": "Big Pickle"},
                        {"id": "gpt-5.5", "providerID": "openai", "name": "GPT-5.5"}]})
```

In `do_POST`, before the 404:

```python
                if parts[0] == "session" and parts[-1] == "command":
                    fake.commands.append(body)
                    return self._json(*fake.run_message(parts[1], {"parts": [{"type": "text", "text": body.get("command", "")}]}))
                if parts[0] == "session" and parts[-1] == "summarize":
                    fake.summaries.append(body)
                    return self._json(200, True)
```

and in `__init__`: `self.commands: list[dict] = []; self.summaries: list[dict] = []`. In `run_message`, make the assistant `info` carry the provider too, so the runtime can retain it:

```python
        info = {"id": "msg_a", "role": "assistant", "sessionID": SID, "tokens": {"input": 120, "output": 7},
                "cost": 0.001, "providerID": "opencode",
                "modelID": body.get("model", {}).get("modelID", "big-pickle")}
```

- [ ] **Step 2: Write the failing tests**

```python
def test_commands_are_loaded_once_and_listed(fake):
    f = fake(); rec = Recorder(); rt = OpencodeRuntime(ChatConfig(), base_url=f.base_url)
    assert rt.harness_commands == []
    rt.run_turn(SESSION, SID, "hi", "", rec.emit, rec)
    assert [c.name for c in rt.harness_commands] == ["init", "review"]
    assert rt.harness_commands[0].description == "guided AGENTS.md setup"
    assert rt.harness_commands[0].origin == "opencode"


def test_a_listed_command_goes_to_the_command_endpoint(fake):
    f = fake(); rec = Recorder(); rt = OpencodeRuntime(ChatConfig(), base_url=f.base_url)
    rt.run_turn(SESSION, SID, "hi", "", rec.emit, rec)              # loads the list
    out = rt.run_turn(SESSION, SID, "/review branch main", "", rec.emit, rec)
    assert out.status == "completed"
    assert f.commands == [{"command": "review", "arguments": "branch main"}]
    assert len(f.posts) == 1                                        # the first turn only


def test_an_unlisted_slash_word_is_ordinary_text(fake):
    f = fake(); rec = Recorder(); rt = OpencodeRuntime(ChatConfig(), base_url=f.base_url)
    rt.run_turn(SESSION, SID, "/nonesuch", "", rec.emit, rec)
    assert f.commands == [] and f.posts[-1]["parts"][0]["text"] == "/nonesuch"


def test_compact_summarizes_with_the_pinned_model(fake):
    f = fake(); rec = Recorder(); rt = OpencodeRuntime(ChatConfig(), base_url=f.base_url)
    out = rt.run_turn(SESSION, SID, "/compact", "openai/gpt-5.5", rec.emit, rec, command="compact")
    assert out.status == "completed"
    assert f.summaries == [{"providerID": "openai", "modelID": "gpt-5.5"}]
    assert f.posts == []


def test_compact_falls_back_to_the_last_turns_model(fake):
    f = fake(); rec = Recorder(); rt = OpencodeRuntime(ChatConfig(), base_url=f.base_url)
    rt.run_turn(SESSION, SID, "hi", "", rec.emit, rec)
    rt.run_turn(SESSION, SID, "/compact", "", rec.emit, rec, command="compact")
    assert f.summaries == [{"providerID": "opencode", "modelID": "big-pickle"}]


def test_compact_without_any_model_fails_with_advice(fake):
    f = fake(); rec = Recorder(); rt = OpencodeRuntime(ChatConfig(), base_url=f.base_url)
    out = rt.run_turn(SESSION, SID, "/compact", "", rec.emit, rec, command="compact")
    assert out.status == "failed" and "pin a model" in out.error and f.summaries == []


def test_list_models_reads_api_model(fake):
    f = fake(); rt = OpencodeRuntime(ChatConfig(), base_url=f.base_url)
    assert rt.list_models(SESSION) == ["opencode/big-pickle  Big Pickle", "openai/gpt-5.5  GPT-5.5"]
```

- [ ] **Step 3: Run to verify they fail**

Run: `uv run pytest tests/test_chat_opencode.py -q -k "commands or command_endpoint or unlisted or compact or list_models"`
Expected: FAIL — `AttributeError: 'OpencodeRuntime' object has no attribute 'harness_commands'`

- [ ] **Step 4: Implement**

`src/tandem/chat/runtime/opencode.py`:

```python
from ..commands import Command
```

In `__init__`:

```python
        self.harness_commands: list[Command] = []
        self._commands_url: str | None = None       # the server the list was read from
        self._last_model: tuple[str, str] | None = None   # (providerID, modelID) of the last reply
```

Add below `ensure_server`:

```python
    def _load_commands(self) -> None:
        """`GET /command` once per server. Runs on the worker, after
        ensure_server, so the window only ever reads the cached list."""
        if self._commands_url == self.base_url:
            return
        try:
            got = self._http("GET", "/command", timeout=10)
        except Exception:
            return                                   # the list is a courtesy; the turn goes on
        if isinstance(got, list):
            self.harness_commands = [
                Command(c["name"], str(c.get("description") or ""), "opencode")
                for c in got if isinstance(c, dict) and isinstance(c.get("name"), str) and c["name"]]
            self._commands_url = self.base_url
```

In `run_turn`, signature gains `command: str = ""`. Replace the body construction at the top with:

```python
        assert native_id, "opencode sessions are created at pair time"
        body: dict | None = None
        path = f"/session/{native_id}/message"
        if model and "/" not in model:
            msg = f"opencode models are spelled provider/model, got {model!r}"
            emit(Failure(msg)); emit(TurnFinished("failed", ""))
            return TurnOutcome("failed", msg)
        try:
            self.ensure_server(session.cwd, tandem_id=session.tandem_id)
        except Exception as exc:        # an unhealthy server, a missing binary: both end the turn
            msg = str(exc) or type(exc).__name__
            emit(Failure(msg)); emit(TurnFinished("failed", ""))
            return TurnOutcome("failed", msg)
        self._load_commands()
        if command == "compact":
            ids = tuple(model.split("/", 1)) if model else self._last_model
            if not ids:
                msg = "nothing to compact with: pin a model (/opencode:provider/model) or run one opencode turn first"
                emit(Failure(msg)); emit(TurnFinished("failed", ""))
                return TurnOutcome("failed", msg)
            path, body = f"/session/{native_id}/summarize", {"providerID": ids[0], "modelID": ids[1]}
        else:
            head = prompt.split(maxsplit=1)
            names = {c.name for c in self.harness_commands}
            if head and head[0].startswith("/") and head[0][1:] in names:
                path = f"/session/{native_id}/command"
                body = {"command": head[0][1:], "arguments": head[1] if len(head) > 1 else ""}
            else:
                body = {"parts": [{"type": "text", "text": prompt}, *mention_parts(prompt, session.cwd)]}
                if model:
                    provider, model_id = model.split("/", 1)
                    body["model"] = {"providerID": provider, "modelID": model_id}
```

(The existing `try: self.ensure_server(...)` block moves up as shown; delete its old position.) The `post()` closure posts to `path`:

```python
                    done["response"] = self._http("POST", path, body, timeout=3600)
```

After the response is read, retain the model:

```python
        if isinstance(resp, dict):
            info = resp.get("info") or {}
            if isinstance(info.get("providerID"), str) and isinstance(info.get("modelID"), str):
                self._last_model = (info["providerID"], info["modelID"])
            tokens = info.get("tokens") or {}
            ...
```

Add:

```python
    def list_models(self, session) -> list[str]:
        """`GET /api/model`: the models of every connected provider, as
        `provider/id` so a line's first word is what `/opencode:…` pins."""
        self.ensure_server(session.cwd, tandem_id=session.tandem_id)
        got = self._http("GET", "/api/model", timeout=10)
        data = (got or {}).get("data") if isinstance(got, dict) else None
        return [f"{m['providerID']}/{m['id']}  {m.get('name') or ''}".rstrip()
                for m in (data or []) if isinstance(m, dict)
                and isinstance(m.get("providerID"), str) and isinstance(m.get("id"), str)]
```

- [ ] **Step 5: Run the opencode tests**

Run: `uv run pytest tests/test_chat_opencode.py -q`
Expected: all passed

- [ ] **Step 6: Commit**

```bash
git add src/tandem/chat/runtime/opencode.py tests/fakes/fake_opencode_server.py tests/test_chat_opencode.py
git commit -m "Opencode runtime: list commands, run them and /compact through the server

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 7: Wire the window, document, gate live, open the PR

**Files:**
- Modify: `src/tandem/chat/window.py` (`run_chat`), `docs/configuration.md:174-206`, `README.md:35-40`
- Create: `/private/tmp/tandem-live-gate/gate_slash.md` (a manual gate checklist, not committed)
- Test: `tests/test_chat_window.py`

**Interfaces:**
- Consumes: everything above.

- [ ] **Step 1: Write the failing pty test**

Add to `tests/test_chat_window.py`, next to `test_at_sign_picks_a_file_from_the_session_directory`:

```python
def test_slash_lists_and_completes_a_command_on_a_pty(env_factory, monkeypatch):
    env = env_factory()
    text = drive_chat(env, keys=b"/hel\t\r/quit\r", ping=False)
    assert "❯ /help" in text          # the picker row was painted
    assert "tandem:" in text          # and Enter ran /help
```

Read `drive_chat` (`tests/test_chat_window.py:371`) first: it launches `run_chat` on a pty with `EchoRuntime`s. Give `EchoRuntime` the two new members so the catalog lambda can read them:

```python
class EchoRuntime:
    harness_commands = []
    def list_models(self, session): return []
    def run_turn(self, session, native_id, prompt, model, emit, answers, command=""):
        ...
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_chat_window.py -k slash_lists -q`
Expected: FAIL — the output has no `❯ /help` (the composer was built without `commands`)

- [ ] **Step 3: Wire `run_chat`**

In `run_chat`, build the runtimes before the composer and hand both callables over:

```python
    runtimes = runtimes if runtimes is not None else make_runtimes(session, cfg)
    harness_commands = lambda: {h: list(getattr(rt, "harness_commands", [])) for h, rt in runtimes.items()}
    composer = Composer(paths=lambda: list_paths(session.cwd), commands=lambda: win.catalog())
```

(`win` is assigned later in the same function; the lambda resolves it at call time, which is after the window exists.) Pass `harness_commands=harness_commands` to the `Window(...)` constructor call. Delete the earlier `runtimes = …` line so it is not assigned twice.

- [ ] **Step 4: Run the whole suite**

Run: `uv run pytest -q`
Expected: everything passes; count ≥ 1400 + the new tests

- [ ] **Step 5: Document**

`docs/configuration.md`, the paragraph beginning "Tandem's own window commands are `/quit` …" (around line 178): replace with

```
Tandem's own window commands are `/help` (list everything a leading `/`
can be), `/quit` (leave the window, as two Ctrl-Cs do), `/status` (print
the session id, the default harness, the participants and any model
pins), `/skip-permissions [on|off]` (turn claude's and codex's permission
prompts off or on from the next turn — see
[`skip_permissions`](#skip_permissions--no-permission-prompts-in-claude-and-codex)),
`/compact` (compact the default harness's conversation: claude runs its
built-in, codex `thread/compact/start`, opencode `summarize` with the
pinned model or the last reply's) and `/model` (list the default
harness's models; `/model NAME` is `/harness:NAME`). Typing `/` opens a
picker under the draft listing these, the routes, and the default
harness's own commands — claude's from its session, opencode's from its
server, none for codex — narrowing by prefix; Tab or Enter completes, Esc
closes. Every other leading `/word` goes to the current harness as its own
slash command; for opencode a listed command runs through its command
endpoint, as its TUI would.
```

`README.md` line 39–40, after the `@` sentence: add "Type `/` to see the commands and routes; `/help` prints them."

- [ ] **Step 6: Live gate (manual, three binaries)**

In a scratch project directory with all three harnesses paired (`tandem` from a directory with a claude, codex and opencode participant), work through and tick:

```
- [ ] `/` shows tandem's rows, /claude /codex /opencode, then claude's commands after one claude turn
- [ ] `/hel` Tab → `/help ` ; Enter prints the catalog grouped tandem: / routes: / claude:
- [ ] `/mode` Enter submits `/mode` as text (no `/model` accepted)
- [ ] `/codex:gpt-5.5 hi` runs on codex with the pin (picker closed on the `:`)
- [ ] `/model` on claude lists four families; `/codex` then `/model` lists codex models, pin marked after `/model gpt-5.5`
- [ ] `/opencode` then `/model` lists provider/model rows
- [ ] `/compact` on claude after a few turns → "compacted" closing row; `tandem doctor` clean
- [ ] `/codex /compact` → completed row; the codex rollout has a compaction item; shadows got the one-line note
- [ ] `/opencode /compact` after one opencode turn → completed; before any turn and with no pin → the advice failure
- [ ] `/opencode` then `/init` (an opencode command) → runs through the command endpoint (opencode's log shows /session/.../command)
- [ ] `/compact keep the notes` on claude → runs as text, claude compacts with the instruction
```

Record the outcome in the PR body. Any failed line is a bug to fix in this PR before it opens.

- [ ] **Step 7: Commit and open the PR**

```bash
git add src/tandem/chat/window.py tests/test_chat_window.py docs/configuration.md README.md
git commit -m "Chat: / typeahead, /help, /compact and /model on every harness

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
git push -u origin chat-slash-commands
gh pr create --title "Chat: / typeahead, /help, /compact and /model on every harness" --body-file /tmp/pr-body.md
```

The PR body: what the four pieces do, the spec path, the live-gate checklist with its results, and the closing line `🤖 Generated with [Claude Code](https://claude.com/claude-code)`.

- [ ] **Step 8: Codex review of the PR**

Dispatch four `tandem:gpt` reviews in one message, one per module group (composer; dispatcher + events + window; claude + codex runtimes; opencode runtime + fakes), each naming its files and capped at six minutes (see memory `codex-review-dispatch-sizing`). Verify each finding against the code before acting; fix real ones in this PR; record dismissed ones in the PR body.
