# Chat markdown and edit diffs — Implementation Plan (parity PR 4 of 4)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replies render as markdown the way the native TUIs show them, and every file edit shows its diff under the tool row.

**Architecture:** `rich` renders markdown to ANSI into a string buffer; a new pure module `chat/markdown.py` owns the fence-aware paragraph splitter and the line cleaner, so `render.py` only buffers deltas and flushes blocks. A new `FileDiff` live event carries a unified diff per edit: codex already has it on the wire, claude's edit inputs go through `difflib` as labelled snippet diffs, opencode's edit metadata is read when present. Nothing already printed is redrawn.

**Tech Stack:** Python 3.12, `rich>=13` (new runtime dependency; brings `markdown-it-py` and `pygments`), pytest, the fake servers under `tests/fakes/`.

**Spec:** `docs/specs/2026-09-26-chat-parity-design.md`, section 3 ("Markdown and diffs").

## Global Constraints

- `rich>=13` joins `dependencies` and `uv lock` is regenerated in the same commit (the release-mechanics rule). No other new dependency.
- The scroll region stays append-only: a block is rendered once, when it is complete or when something else must print; nothing is re-rendered.
- `_safe` runs on the source text before rich, never on rich's output; OSC sequences rich emits are stripped after, SGR is kept.
- Rendered rows are at most `cols - 1` cells wide, with no trailing whitespace before a closing SGR, so `Screen.print`'s edge rule never fires on them.
- `FileDiff` is emitted after `ToolFinished`, only for a successful edit; `diff_lines = 0` paints no diff and claude builds none.
- `[chat] markdown = false` must reproduce today's raw streaming byte for byte.
- Commit messages follow the repo's imperative style and end with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.
- Run the suite with `uv run pytest -q`; the baseline on `main` at `08a201d` is 1527 passed.

## Review Focus

1. A reply whose last paragraph has no trailing blank line must still appear, at turn end. (Task 3: `test_the_last_paragraph_flushes_at_turn_end`.)
2. A code fence the model never closes must be flushed as-is at turn end, not held. (Task 3: `test_an_unclosed_fence_flushes_at_turn_end`.)
3. A fence marker split across two deltas (`` `` `` then `` `python ``) must be read as one opener. (Task 2: `test_a_marker_split_across_deltas_is_one_opener`.)
4. CJK or emoji in a rendered line must not push a row past `cols - 1` cells. (Task 2: `test_rendered_rows_fit_in_cells_with_wide_glyphs`.)
5. An edit tool that fails after its input was remembered must produce no diff. (Task 4: `test_a_failed_edit_produces_no_diff`.)
6. A resize between buffering and flushing must render at the new width. (Task 3: `test_a_resize_before_the_flush_renders_at_the_new_width`.)
7. A loose list (blank lines between items, an indented continuation) streamed block by block must render exactly as the whole text would; a `#` heading must not come out boxed. (Task 2: `test_a_blank_line_inside_a_loose_list_is_not_a_boundary`, `test_streaming_a_loose_list_renders_like_the_whole`, `test_headings_are_not_boxed`. Codex plan finding.)

---

### Task 1: The dependency and the config keys

**Files:**
- Modify: `pyproject.toml` (`dependencies`), `uv.lock` (regenerated), `src/tandem/config.py` (`ChatConfig`, `load_chat_config`), `docs/configuration.md` (the `[chat]` toml block)
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `ChatConfig.markdown: bool = True`, `ChatConfig.diff_lines: int = 40`; `rich` importable.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_config.py`:

```python
def test_markdown_and_diff_lines_default_on(tmp_path, monkeypatch):
    monkeypatch.setenv("TANDEM_HOME", str(tmp_path / ".tandem"))
    cfg = load_chat_config()
    assert cfg.markdown is True and cfg.diff_lines == 40


def test_markdown_and_diff_lines_from_the_chat_table(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch, "[chat]\nmarkdown = false\ndiff_lines = 12\n")
    cfg = load_chat_config()
    assert cfg.markdown is False and cfg.diff_lines == 12
    _write_config(tmp_path, monkeypatch, '[chat]\nmarkdown = "no"\ndiff_lines = -3\n')
    cfg = load_chat_config()
    assert cfg.markdown is True and cfg.diff_lines == 0          # forgiving: bad type → default, clamp at 0
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_config.py -q -k "markdown_and_diff"`
Expected: FAIL — `AttributeError: 'ChatConfig' object has no attribute 'markdown'`

- [ ] **Step 3: Add the dependency and the keys**

```bash
uv add "rich>=13"          # edits pyproject.toml and uv.lock together, installs into .venv
uv run python -c "import rich, markdown_it, pygments; print(rich.__version__)"
```

In `src/tandem/config.py`, `ChatConfig`, after `bell`:

```python
    markdown: bool = True               # render replies as markdown, block by block
    diff_lines: int = 40                # lines of each edit's diff shown (0 = none)
```

In `load_chat_config`'s constructor call, after `bell=...`:

```python
        markdown=pick("markdown", bool, d.markdown),
        diff_lines=max(0, pick("diff_lines", int, d.diff_lines)),
```

In `docs/configuration.md`'s `[chat]` toml block, after `bell`:

```
markdown = true              # render replies as markdown, a paragraph or code block at a time
diff_lines = 40              # lines of each file edit's diff shown under its tool row (0 = none)
```

- [ ] **Step 4: Run the config tests**

Run: `uv run pytest tests/test_config.py -q`
Expected: all passed

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml uv.lock src/tandem/config.py tests/test_config.py docs/configuration.md
git commit -m "Add rich and the [chat] markdown and diff_lines keys

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: `chat/markdown.py` — the splitter and the renderer

**Files:**
- Create: `src/tandem/chat/markdown.py`
- Create: `tests/test_chat_markdown.py`

**Interfaces:**
- Produces: `ready_blocks(buffer: str) -> tuple[str, str]` (the text ready to render, ending in `\n`, and the remainder); `render_markdown(text: str, cols: int, color: bool) -> list[str]` (cleaned ANSI rows, each at most `cols - 1` cells, no trailing whitespace, no OSC); `cells(text: str) -> int`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_chat_markdown.py
"""The fence-aware paragraph splitter and the rich renderer behind the
chat window's markdown, both pure."""

import re

from tandem.chat.markdown import cells, ready_blocks, render_markdown

SGR = re.compile(r"\x1b\[[0-9;]*m")


def test_nothing_is_ready_without_a_blank_line():
    assert ready_blocks("one\ntwo") == ("", "one\ntwo")


def test_a_blank_line_releases_everything_up_to_it():
    assert ready_blocks("one\ntwo\n\nthr\n") == ("one\ntwo\n\n", "thr\n")
    assert ready_blocks("one\ntwo\n\nthr") == ("", "one\ntwo\n\nthr")     # "thr" may still grow into "   thr…"


def test_the_unterminated_tail_is_never_ready():
    assert ready_blocks("one\n\ntwo") == ("one\n\n", "two")
    assert ready_blocks("one\n\ntwo\n") == ("one\n\n", "two\n")


def test_a_blank_line_inside_a_fence_releases_nothing():
    buf = "```py\nx = 1\n\ny = 2\n"
    assert ready_blocks(buf) == ("", buf)


def test_a_closing_fence_releases_through_it():
    buf = "intro\n\n```py\nx = 1\n```\nafter"
    assert ready_blocks(buf) == ("intro\n\n```py\nx = 1\n```\n", "after")
    # the blank before the fence waits for the fence line, which does not continue anything
    assert ready_blocks("intro\n\n```py\n")[0] == "intro\n\n"


def test_fence_markers_must_match_and_may_be_longer_or_indented():
    assert ready_blocks("~~~\n```\n\n~~\n")[0] == ""            # backticks inside a tilde fence
    assert ready_blocks("````\n```\n\n````\n")[0] == "````\n```\n\n````\n"
    assert ready_blocks("  ```\ncode\n  ```\n")[0] == "  ```\ncode\n  ```\n"
    assert ready_blocks("```\ncode\n``` not a closer\n\n")[0] == ""  # info string on a closer: still code


def test_a_blank_line_inside_a_loose_list_is_not_a_boundary():
    """Blank lines separate items of a loose list and precede indented
    continuations; cutting there would render `2. second` as a new list
    starting at 2 and the continuation as a paragraph. (Codex plan finding.)"""
    text = "1. first\n\n   continuation\n\n2. second\n\nAfter\n"
    assert ready_blocks(text) == ("1. first\n\n   continuation\n\n2. second\n\n", "After\n")
    assert ready_blocks("- a\n\n- b\n\n- c\n")[0] == ""          # still going: no non-list line yet
    assert ready_blocks("> quote\n\n> more\n\nplain\n")[0] == "> quote\n\n> more\n\n"


def test_a_blank_line_waits_for_the_next_line_before_deciding():
    # the line after the blank has not arrived: undecided, nothing released
    assert ready_blocks("1. first\n\n") == ("", "1. first\n\n")
    assert ready_blocks("para\n\n") == ("", "para\n\n")
    assert ready_blocks("para\n\nnext") == ("", "para\n\nnext")            # a partial line cannot decide
    assert ready_blocks("para\n\nnext\n") == ("para\n\n", "next\n")


def test_streaming_a_loose_list_renders_like_the_whole():
    text = "Intro.\n\n1. first\n\n   continuation\n\n2. second\n\nAfter.\n"
    whole = render_markdown(text, 40, False)
    streamed, buf = [], ""
    for ch in text:                                  # one character at a time
        buf += ch
        ready, buf = ready_blocks(buf)
        if ready:
            streamed += render_markdown(ready, 40, False) + ([""] if ready.endswith("\n\n") else [])
    if buf:
        streamed += render_markdown(buf, 40, False)
    assert streamed == whole


def test_headings_are_not_boxed():
    rows = render_markdown("# Title\n\ntext\n", 40, False)
    assert rows[0].strip() == "Title" and not any(ch in "".join(rows) for ch in "━┃┏┓┗┛│─")


def test_a_marker_split_across_deltas_is_one_opener():
    buf = ""
    for delta in ("``", "`python\nprint(1)\n", "\n", "```\n"):
        buf += delta
    assert ready_blocks(buf) == ("```python\nprint(1)\n\n```\n", "")
    # and nothing was released early: the second delta alone holds an open fence
    assert ready_blocks("```python\nprint(1)\n\n") == ("", "```python\nprint(1)\n\n")


def test_render_bolds_and_wraps_within_the_width():
    rows = render_markdown("This is **bold** text that is fairly long and wraps.\n", 30, True)
    assert any("\x1b[1m" in r for r in rows)
    assert all(cells(r) <= 29 for r in rows)
    assert all(not SGR.sub("", r).endswith(" ") for r in rows)


def test_render_without_color_is_plain():
    rows = render_markdown("# Title\n\n- one\n- two\n", 40, False)
    assert not any("\x1b" in r for r in rows)
    assert any("Title" in r for r in rows) and any("one" in r for r in rows)


def test_render_code_blocks_keep_their_text_and_drop_padding():
    rows = render_markdown("```py\nx = 1\n```\n", 40, True)
    plain = [SGR.sub("", r) for r in rows]
    assert any("x = 1" in p for p in plain)
    assert all(not p.endswith(" ") for p in plain)                # rich pads code rows to the width
    assert all(cells(r) <= 39 for r in rows)


def test_render_strips_hyperlink_osc():
    rows = render_markdown("see [docs](https://example.com) now\n", 40, True)
    assert not any("\x1b]" in r for r in rows)
    assert any("docs" in SGR.sub("", r) for r in rows)


def test_rendered_rows_fit_in_cells_with_wide_glyphs():
    rows = render_markdown("修复 the bug 修复 the bug 修复 the bug 修复 the bug 修复\n", 24, False)
    assert all(cells(r) <= 23 for r in rows)


def test_render_drops_the_trailing_empty_row():
    rows = render_markdown("just a line\n", 40, False)
    assert rows and rows[-1] != ""
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_chat_markdown.py -q`
Expected: `ModuleNotFoundError: No module named 'tandem.chat.markdown'`

- [ ] **Step 3: Write the module**

```python
# src/tandem/chat/markdown.py
"""Markdown for the chat window's replies, in two pure pieces.

`ready_blocks` decides what a streaming reply has finished: everything up
to the last blank line outside a fenced code block, or up to a fence's
closer. It looks only at complete lines, so a fence marker split across two
deltas is never misread, and the unterminated tail always waits.

`render_markdown` hands one such block to rich and cleans what comes back
for the raw-tty printer: OSC hyperlinks out (the printer's escape
whitelist is SGR only, and its cell count would include them), trailing
padding out even when it sits before a closing SGR (rich pads code rows to
the full width; a row exactly `cols` wide trips the printer's edge rule),
and one column spare."""

from __future__ import annotations

import io
import re
from unicodedata import east_asian_width

from rich.console import Console
from rich.markdown import Heading, Markdown

_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_LIST_ITEM = re.compile(r"^ {0,3}(?:[-*+]|\d{1,9}[.)])\s")
_OSC = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?")
_SGR = re.compile(r"\x1b\[[0-9;]*m")
_TRAILING_PAD = re.compile(r"[ \t]+((?:\x1b\[[0-9;]*m)*)$")


def _continues(prev: str, nxt: str) -> bool:
    """Does `nxt`, after a blank line, continue the container `prev` was in?
    An indented line does (a list item's second paragraph); a list item
    does when the block before was a list item or indented under one (a
    loose list); a `>` line does after a `>` line."""
    if nxt[:1] in (" ", "\t") and nxt.strip():
        return True
    if _LIST_ITEM.match(nxt) and (_LIST_ITEM.match(prev) or prev[:1] in (" ", "\t")):
        return True
    return nxt.startswith(">") and prev.startswith(">")


def ready_blocks(buffer: str) -> tuple[str, str]:
    """(what may be rendered now, what stays). The ready part ends in a
    newline; it is empty when nothing has finished. A blank line is a
    boundary only once the next non-blank line has arrived and does not
    continue a list or quote — so the decision waits for that line."""
    lines = buffer.split("\n")
    tail = lines.pop()                          # the unterminated last line
    cut = 0
    fence: tuple[str, int] | None = None
    prev = ""                                    # the last non-blank line seen
    for i, line in enumerate(lines):
        m = _FENCE.match(line)
        if fence is not None:
            if (m and m.group(1)[0] == fence[0] and len(m.group(1)) >= fence[1]
                    and not m.group(2).strip()):
                fence = None
                cut = i + 1
            prev = line
            continue
        if m:
            fence = (m.group(1)[0], len(m.group(1)))
            prev = line
            continue
        if line.strip():
            prev = line
            continue
        following = next((l for l in lines[i + 1:] if l.strip()), None)
        if following is not None and not _continues(prev, following):
            cut = i + 1
    ready = "\n".join(lines[:cut]) + ("\n" if cut else "")
    rest = "\n".join(lines[cut:] + [tail])
    return ready, rest


class _PlainHeading(Heading):
    """rich boxes an h1 and centres every heading; a chat reply wants the
    heading's text, left-aligned, in the heading style."""

    def __rich_console__(self, console, options):
        text = self.text
        text.justify = "left"
        yield text


class _ChatMarkdown(Markdown):
    elements = {**Markdown.elements, "heading_open": _PlainHeading}


def cells(text: str) -> int:
    """Terminal cells a row takes, SGR aside."""
    return sum(2 if east_asian_width(ch) in ("W", "F") else 1 for ch in _SGR.sub("", text))


def render_markdown(text: str, cols: int, color: bool) -> list[str]:
    """One block of markdown as cleaned ANSI rows, at most `cols - 1` cells
    each. Built per call, never cached: the width is the caller's now."""
    buf = io.StringIO()
    console = Console(file=buf, width=max(10, cols - 1), force_terminal=color,
                      color_system="standard" if color else None, no_color=not color,
                      highlight=False, soft_wrap=False, legacy_windows=False)
    console.print(_ChatMarkdown(text, code_theme="ansi_dark"), end="")
    rows = []
    for row in _OSC.sub("", buf.getvalue()).split("\n"):
        rows.append(_TRAILING_PAD.sub(r"\1", row))
    while rows and not _SGR.sub("", rows[-1]).strip():
        rows.pop()                              # rich's trailing blank row
    return rows
```

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/test_chat_markdown.py -q`
Expected: all passed. If `test_render_bolds_and_wraps_within_the_width` fails on the SGR assertion, check that `force_terminal=True` produced escapes; if `test_render_code_blocks_keep_their_text_and_drop_padding` fails on width, rich's code panel may render a box — the assertion is on cells, which the `cols - 1` width guarantees; read the failing row and adjust the cleaner, not the test.

- [ ] **Step 5: Commit**

```bash
git add src/tandem/chat/markdown.py tests/test_chat_markdown.py
git commit -m "Chat markdown: a fence-aware block splitter and a rich renderer

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3: The renderer buffers, flushes and paints diffs; the window routes `FileDiff`

**Files:**
- Modify: `src/tandem/chat/events.py` (`FileDiff`, union), `src/tandem/chat/render.py` (state, `_flush_md`, `text_delta`, every painter, `file_diff`, `history`), `src/tandem/chat/window.py` (`handle_event`)
- Test: `tests/test_chat_render.py`, `tests/test_chat_window.py`

**Interfaces:**
- Produces: `FileDiff(call_id: str, path: str, diff: str)`; `Screen.file_diff(ev)`; `Screen._flush_md()`.
- Consumes: `ready_blocks`, `render_markdown` (Task 2); `cfg.markdown`, `cfg.diff_lines` (Task 1).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_chat_render.py` (the `screen` fixture builds `ChatConfig(tool_output_lines=2)` with `color=False`, cols 40; add a `md_screen` helper):

```python
from tandem.chat.events import FileDiff


def md(cols=40, color=False, **kw):
    out = Out()
    s = Screen(out, 24, cols, ChatConfig(tool_output_lines=2, **kw), color=color)
    s.enter(); out.text(clear=True)
    return s, out


def test_text_is_held_until_a_paragraph_ends():
    s, out = md()
    s.turn_started(TurnStarted("claude", "", "hi")); out.text(clear=True)
    s.text_delta(TextDelta("First **para"))
    s.text_delta(TextDelta("graph** here."))
    assert "para" not in out.text()                        # nothing until the blank line
    s.text_delta(TextDelta("\n\nSecond"))
    assert "para" not in out.text()                        # the line after the blank is still partial
    s.text_delta(TextDelta(" one\n"))
    t = out.text()
    assert "First paragraph here." in t and "Second" not in t


def test_the_last_paragraph_flushes_at_turn_end():
    s, out = md()
    s.turn_started(TurnStarted("claude", "", "hi")); out.text(clear=True)
    s.text_delta(TextDelta("Only paragraph, no trailing blank"))
    s.turn_finished(TurnFinished("completed", ""))
    t = out.text()
    assert "Only paragraph, no trailing blank" in t
    assert t.index("Only paragraph") < t.index("✓ done")


def test_an_unclosed_fence_flushes_at_turn_end():
    s, out = md()
    s.turn_started(TurnStarted("claude", "", "hi")); out.text(clear=True)
    s.text_delta(TextDelta("```py\nx = 1\n\ny = 2\n"))
    assert "x = 1" not in out.text()
    s.turn_finished(TurnFinished("completed", ""))
    assert "x = 1" in out.text() and "y = 2" in out.text()


def test_a_tool_row_flushes_the_text_before_it():
    s, out = md()
    s.turn_started(TurnStarted("claude", "", "hi")); out.text(clear=True)
    s.text_delta(TextDelta("Let me look"))
    s.tool_started(ToolStarted("c1", "Read", "x.py"))
    t = out.text()
    assert t.index("Let me look") < t.index("▸ Read")


def test_a_new_turn_drops_text_left_by_an_interrupted_one():
    s, out = md()
    s.turn_started(TurnStarted("claude", "", "hi"))
    s.text_delta(TextDelta("half a thou"))
    s.turn_started(TurnStarted("codex", "", "next")); out.text(clear=True)
    s.turn_finished(TurnFinished("completed", ""))
    assert "half a thou" not in out.text()


def test_markdown_off_streams_raw_bytes_as_before():
    s_on, out_on = md(markdown=False)
    s_on.turn_started(TurnStarted("claude", "", "hi")); out_on.text(clear=True)
    for chunk in ("**bo", "ld**\n\nmore"):
        s_on.text_delta(TextDelta(chunk))
    assert out_on.text() == "claude\r\n**bold**\r\n\r\nmore"        # raw, token by token, as before


def test_streamed_paragraphs_keep_a_blank_row_between_them():
    s, out = md()
    s.turn_started(TurnStarted("claude", "", "hi")); out.text(clear=True)
    s.text_delta(TextDelta("First.\n\nSecond.\n\nThird."))
    s.turn_finished(TurnFinished("completed", ""))
    body = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", out.text()).split("✓ done")[0]
    assert "First.\r\n\r\nSecond.\r\n\r\nThird." in body


def test_a_resize_before_the_flush_renders_at_the_new_width():
    s, out = md(cols=80)
    s.turn_started(TurnStarted("claude", "", "hi")); out.text(clear=True)
    s.text_delta(TextDelta("word " * 20))
    s.resize(24, 30); out.text(clear=True)
    s.turn_finished(TurnFinished("completed", ""))
    rows = [r for r in re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", out.text()).split("\r\n") if "word" in r]
    assert rows and all(len(r) <= 29 for r in rows)


def test_history_renders_assistant_text_as_markdown():
    s, out = md()
    s.history([UserMessage(text="q"), AssistantMessage(text="# Heading\n\ntext")], "claude")
    t = out.text()
    assert "Heading" in t and "# Heading" not in t


def test_file_diff_colours_lines_and_caps(screen_factory):
    s, out = screen_factory(cols=60, cfg=ChatConfig(diff_lines=3))
    s.enter(); out.text(clear=True)
    s.file_diff(FileDiff("c1", "src/app.py", "@@ edit @@\n-old line\n+new line\n context\n+more"))
    t = out.text()
    assert "    --- src/app.py" in t
    assert "    @@ edit @@" in t and "    -old line" in t and "    +new line" in t
    assert "context" not in t and "    … +2 lines" in t


def test_file_diff_is_silent_at_zero_lines(screen_factory):
    s, out = screen_factory(cfg=ChatConfig(diff_lines=0))
    s.enter(); out.text(clear=True)
    s.file_diff(FileDiff("c1", "a.py", "+x"))
    assert out.text() == ""


def test_file_diff_colours_when_colour_is_on():
    out = Out()
    s = Screen(out, 24, 60, ChatConfig(), color=True)
    s.enter(); out.text(clear=True)
    s.file_diff(FileDiff("c1", "a.py", "-gone\n+here"))
    t = out.text()
    assert "\x1b[31m    -gone" in t and "\x1b[32m    +here" in t
```

Add to `tests/test_chat_window.py`:

```python
def test_a_file_diff_event_paints_under_the_tool_row(env_factory):
    env = env_factory(); w, d, out, _ = make_window(env)
    w.handle_event(FileDiff("c1", "x.py", "+one"))
    assert "--- x.py" in out.text() and "+one" in out.text()
```

(import `FileDiff` with the other events.)

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_chat_render.py tests/test_chat_window.py -q -k "held_until or flushes or drops_text or streams_raw or resize_before or renders_assistant or file_diff"`
Expected: FAIL — `ImportError: cannot import name 'FileDiff'`

- [ ] **Step 3: Implement**

`src/tandem/chat/events.py`, after `ToolFinished`:

```python
@dataclass(frozen=True)
class FileDiff:
    """One edit's diff, painted under its tool row after ToolFinished."""
    call_id: str
    path: str
    diff: str        # unified diff text, hunks included, no file header needed
```

and add `FileDiff` to the `LiveEvent` union.

`src/tandem/chat/render.py`:

```python
from .events import (ApprovalRequest, Failure, FileDiff, QuestionRequest, ReviewFinished, TextDelta,
                     ThinkingDelta, ToolFinished, ToolOutput, ToolStarted, TurnFinished, TurnStarted,
                     offered_labels)
from .markdown import ready_blocks, render_markdown
```

In `__init__`, after `_tool_partial`:

```python
        self._md = ""                   # the streaming reply not yet rendered (markdown on)
```

Add the markdown machinery after `_ensure_speaker`:

```python
    # -- markdown ----------------------------------------------------------------

    def _render_md(self, text: str) -> None:
        """One finished block. A block that ended on a blank line gets a
        blank row after it: rich only spaces blocks it renders together, and
        the streamed reply must read like the whole would."""
        if not text.strip():
            return
        self._ensure_speaker()
        if self._col:
            self.print("\n")
        rows = render_markdown(text, self.cols, self.color)
        if rows:
            self.print("\n".join(rows) + "\n")
            if text.endswith("\n\n"):
                self.line()

    def _flush_md(self) -> None:
        """Whatever is buffered goes out now: something else is about to
        reach the region, or the turn is over. Idempotent."""
        text, self._md = self._md, ""
        if text:
            self._render_md(text)

    def _bold(self, s: str) -> str: ...        # (unchanged, keep in place)
```

`text_delta` becomes:

```python
    def text_delta(self, ev: TextDelta) -> None:
        if not self.cfg.markdown:
            self._ensure_speaker()
            self.print(_safe(ev.text))
            return
        self._md += _safe(ev.text)
        ready, self._md = ready_blocks(self._md)
        if ready:
            self._render_md(ready)
```

Add `self._flush_md()` as the first statement of `thinking_delta`, `tool_started`, `tool_output`, `tool_finished`, `approval`, `question`, `turn_finished`, `failure`, `review`, `note`. In `turn_started`, first flush then reset:

```python
    def turn_started(self, ev: TurnStarted) -> None:
        self._flush_md()
        self._md = ""                   # an interrupted turn leaves nothing for the next
        self._turn_harness = ev.harness
```

(The flush before the reset renders a previous turn's leftover under that turn's speaker; the reset covers the case where the flush rendered nothing.)

Add the diff painter after `tool_finished`:

```python
    def file_diff(self, ev: FileDiff) -> None:
        """An edit's diff under its tool row: `---` path dim, `+` green,
        `-` red, `@@` dim, the rest plain; four cells in, clipped, capped."""
        self._flush_md()
        cap = self.cfg.diff_lines
        if cap <= 0:
            return
        self.line(self._dim(f"    --- {_safe(ev.path)}"))
        lines = _safe(ev.diff).split("\n")
        for raw in lines[:cap]:
            row = _clip(raw, max(1, self.cols - 5))
            if raw.startswith("+"):
                self.line(self._green("    " + row))
            elif raw.startswith("-"):
                self.line(self._red("    " + row))
            elif raw.startswith("@@"):
                self.line(self._dim("    " + row))
            else:
                self.line("    " + row)
        if len(lines) > cap:
            self.line(self._dim(f"    … +{len(lines) - cap} lines"))

    def _green(self, s: str) -> str:
        return f"{_CSI}32m{s}{_CSI}0m" if self.color else s

    def _red(self, s: str) -> str:
        return f"{_CSI}31m{s}{_CSI}0m" if self.color else s
```

In `history`, the `AssistantMessage` branch:

```python
            elif isinstance(ev, AssistantMessage):
                self._turn_harness = source
                if self.cfg.markdown:
                    self._render_md(_safe(ev.text) + "\n")
                else:
                    self._ensure_speaker()
                    self.line(_safe(ev.text))
```

and `self._flush_md()` as the method's last statement.

`src/tandem/chat/window.py` — import `FileDiff` and add to `handle_event` after the `ToolFinished` branch:

```python
        elif isinstance(ev, FileDiff):
            s.file_diff(ev)
```

- [ ] **Step 4: Run the render and window files, then the whole suite**

Run: `uv run pytest tests/test_chat_render.py tests/test_chat_window.py -q`
Expected: all passed. Pre-existing render tests that streamed raw text and asserted byte output will now see rendered blocks: read each failure; where a test asserted raw streaming of prose, build its screen with `ChatConfig(markdown=False)` (that is the behavior it pins) rather than changing the assertion. Record each such change as a ledger ruling.

Run: `uv run pytest -q`
Expected: all passed

- [ ] **Step 5: Commit**

```bash
git add src/tandem/chat/events.py src/tandem/chat/render.py src/tandem/chat/window.py tests/test_chat_render.py tests/test_chat_window.py
git commit -m "Chat renderer: markdown blocks as they complete, diffs under edit rows

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 4: The runtimes emit `FileDiff`

**Files:**
- Modify: `src/tandem/chat/runtime/claude.py`, `src/tandem/chat/runtime/codex.py`, `src/tandem/chat/runtime/opencode.py`
- Test: `tests/test_chat_claude.py`, `tests/test_chat_codex.py`, `tests/test_chat_opencode.py`

**Interfaces:**
- Produces: `snippet_diff(name: str, inp: dict, cap: int) -> str` in `claude.py`; `TurnState.paths: dict`, `TurnState.finished: set` in `opencode.py`.
- Consumes: `FileDiff` (Task 3), `cfg.diff_lines` (Task 1).

- [ ] **Step 1: Write the failing tests**

`tests/test_chat_claude.py`:

```python
from tandem.chat.events import FileDiff
from tandem.chat.runtime.claude import snippet_diff


def test_snippet_diff_for_an_edit_drops_file_headers_and_labels_the_hunk():
    d = snippet_diff("Edit", {"file_path": "a.py", "old_string": "x = 1\ny = 2", "new_string": "x = 1\ny = 3"}, 40)
    assert d.splitlines()[0] == "@@ edit @@"
    assert "-y = 2" in d and "+y = 3" in d and " x = 1" in d
    assert "---" not in d and "+++" not in d and "@@ -" not in d


def test_snippet_diff_marks_replace_all_and_numbers_multiedits():
    d = snippet_diff("Edit", {"old_string": "a", "new_string": "b", "replace_all": True}, 40)
    assert d.splitlines()[0] == "@@ edit · replace_all @@"
    m = snippet_diff("MultiEdit", {"edits": [{"old_string": "a", "new_string": "b"},
                                             {"old_string": "c", "new_string": "d"}]}, 40)
    assert "@@ edit 1 @@" in m and "@@ edit 2 @@" in m


def test_snippet_diff_for_a_write_is_all_additions_and_capped():
    d = snippet_diff("Write", {"file_path": "n.txt", "content": "\n".join(f"l{i}" for i in range(100))}, 5)
    lines = d.splitlines()
    assert lines[0] == "@@ new file @@" and lines[1] == "+l0"
    assert len(lines) <= 6                                   # header + cap


def test_snippet_diff_is_empty_at_zero_lines():
    assert snippet_diff("Write", {"content": "x"}, 0) == ""


def test_a_successful_edit_emits_a_diff_after_tool_finished():
    rt = ClaudeRuntime(ChatConfig()); rec = Recorder()
    rt.handle_line({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t1", "name": "Edit",
                    "input": {"file_path": "a.py", "old_string": "x", "new_string": "y"}}]}},
                   rec.emit, rec, lambda _: None)
    rt.handle_line({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1",
                    "content": "ok", "is_error": False}]}}, rec.emit, rec, lambda _: None)
    kinds = rec.kinds()
    assert kinds[-2:] == ["ToolFinished", "FileDiff"]
    fd = rec.events[-1]
    assert fd.path == "a.py" and "-x" in fd.diff and "+y" in fd.diff


def test_a_failed_edit_produces_no_diff():
    rt = ClaudeRuntime(ChatConfig()); rec = Recorder()
    rt.handle_line({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t1", "name": "Write",
                    "input": {"file_path": "a.py", "content": "x"}}]}}, rec.emit, rec, lambda _: None)
    rt.handle_line({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1",
                    "content": "EACCES", "is_error": True}]}}, rec.emit, rec, lambda _: None)
    assert "FileDiff" not in rec.kinds()


def test_a_read_tool_produces_no_diff():
    rt = ClaudeRuntime(ChatConfig()); rec = Recorder()
    rt.handle_line({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t1", "name": "Read",
                    "input": {"file_path": "a.py"}}]}}, rec.emit, rec, lambda _: None)
    rt.handle_line({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1",
                    "content": "text", "is_error": False}]}}, rec.emit, rec, lambda _: None)
    assert "FileDiff" not in rec.kinds()
```

`tests/test_chat_codex.py`:

```python
from tandem.chat.events import FileDiff


def test_a_completed_file_change_emits_a_diff_per_change_after_tool_finished():
    rt = CodexRuntime(ChatConfig()); rec = Recorder(); rt._thread_id = "t"
    handle = lambda m: rt.handle(m, lambda _: None, rec.emit, rec)
    handle({"method": "item/completed", "params": {"threadId": "t", "turnId": "u", "completedAtMs": 2,
            "item": {"type": "fileChange", "id": "c1", "status": "completed",
                     "changes": [{"path": "/p/a.py", "kind": {"type": "update"}, "diff": "@@ -1 +1 @@\n-a\n+b"},
                                 {"path": "/p/b.py", "kind": {"type": "add"}, "diff": "@@ -0,0 +1 @@\n+new"}]}}})
    kinds = rec.kinds()
    assert kinds[-3:] == ["ToolFinished", "FileDiff", "FileDiff"]
    assert [e.path for e in rec.events[-2:]] == ["/p/a.py", "/p/b.py"]


def test_a_declined_file_change_emits_no_diff():
    rt = CodexRuntime(ChatConfig()); rec = Recorder(); rt._thread_id = "t"
    rt.handle({"method": "item/completed", "params": {"threadId": "t", "turnId": "u", "completedAtMs": 2,
               "item": {"type": "fileChange", "id": "c1", "status": "declined",
                        "changes": [{"path": "/p/a.py", "kind": {"type": "update"}, "diff": "-a\n+b"}]}}},
              lambda _: None, rec.emit, rec)
    assert "FileDiff" not in rec.kinds() and rec.events[-1] == ToolFinished("c1", False, "")


def test_file_change_output_deltas_are_no_longer_painted_as_output():
    rt = CodexRuntime(ChatConfig()); rec = Recorder(); rt._thread_id = "t"
    rt.handle({"method": "item/fileChange/outputDelta", "params": {"threadId": "t", "turnId": "u",
               "itemId": "c1", "delta": "-a\n+b\n"}}, lambda _: None, rec.emit, rec)
    assert rec.events == []
    rt.handle({"method": "item/commandExecution/outputDelta", "params": {"threadId": "t", "turnId": "u",
               "itemId": "c2", "delta": "hi"}}, lambda _: None, rec.emit, rec)
    assert rec.events == [ToolOutput("c2", "hi")]                 # commands still stream
```

`tests/test_chat_opencode.py`:

```python
from tandem.chat.events import FileDiff


def part_update(**part):
    return {"type": "message.part.updated", "properties": {"sessionID": SID,
            "part": {"sessionID": SID, "messageID": "msg_a", "id": "prt_1", "type": "tool", **part}}}


def test_a_completed_edit_with_a_metadata_diff_emits_one_file_diff():
    rt = OpencodeRuntime(ChatConfig(), base_url="http://127.0.0.1:1"); rec = Recorder()
    st = TurnState(session_id=SID)
    done = part_update(callID="c1", tool="edit", state={"status": "completed", "input": {"filePath": "/p/a.py"},
                       "output": "ok", "title": "a.py", "metadata": {"diff": "-a\n+b"}})
    rt.handle_event(done, st, rec.emit, rec)
    rt.handle_event(done, st, rec.emit, rec)                     # opencode repeats completed updates
    kinds = rec.kinds()
    assert kinds == ["ToolStarted", "ToolOutput", "ToolFinished", "FileDiff"]
    assert rec.events[-1] == FileDiff("c1", "/p/a.py", "-a\n+b")


def test_a_completed_edit_without_a_diff_emits_none():
    rt = OpencodeRuntime(ChatConfig(), base_url="http://127.0.0.1:1"); rec = Recorder()
    st = TurnState(session_id=SID)
    rt.handle_event(part_update(callID="c1", tool="edit", state={"status": "completed",
                    "input": {"filePath": "/p/a.py"}, "output": "ok"}), st, rec.emit, rec)
    assert "FileDiff" not in rec.kinds()


def test_an_errored_edit_emits_no_diff_and_finishes_once():
    rt = OpencodeRuntime(ChatConfig(), base_url="http://127.0.0.1:1"); rec = Recorder()
    st = TurnState(session_id=SID)
    err = part_update(callID="c1", tool="edit", state={"status": "error", "input": {"filePath": "/p/a.py"},
                      "error": "denied", "metadata": {"diff": "-a\n+b"}})
    rt.handle_event(err, st, rec.emit, rec); rt.handle_event(err, st, rec.emit, rec)
    assert rec.kinds() == ["ToolStarted", "ToolFinished"]
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_chat_claude.py tests/test_chat_codex.py tests/test_chat_opencode.py -q -k "diff or deltas_are_no_longer or finishes_once"`
Expected: FAIL — `ImportError: cannot import name 'snippet_diff'` for claude; the codex and opencode files fail on the missing `FileDiff` events

- [ ] **Step 3: Implement**

`src/tandem/chat/runtime/claude.py`:

```python
import difflib
from ..events import (Answers, ApprovalRequest, FileDiff, LimitsUpdate, LiveEvent, QuestionRequest, TextDelta,
                      ThinkingDelta, ToolFinished, ToolOutput, ToolStarted, TurnFinished,
                      TurnOutcome)


def snippet_diff(name: str, inp: dict, cap: int) -> str:
    """The diff of an Edit/MultiEdit/Write from its input alone — no file
    is read. A snippet diff: `@@ edit @@` hunks with no line numbers, `Write`
    as a new file. Capped to `cap` lines while it is built; "" at cap 0."""
    if cap <= 0 or not isinstance(inp, dict):
        return ""
    out: list[str] = []

    def add(line: str) -> bool:
        if len(out) >= cap:
            return False
        out.append(line)
        return True

    if name == "Write":
        add("@@ new file @@")
        for l in str(inp.get("content", "")).splitlines():
            if not add("+" + l):
                break
        return "\n".join(out)
    edits = inp.get("edits") if name == "MultiEdit" else [inp]
    for i, e in enumerate(edits or [], 1):
        if not isinstance(e, dict):
            continue
        head = "@@ edit" + (f" {i}" if name == "MultiEdit" else "") \
               + (" · replace_all" if e.get("replace_all") else "") + " @@"
        if not add(head):
            break
        body = difflib.unified_diff(str(e.get("old_string", "")).splitlines(),
                                    str(e.get("new_string", "")).splitlines(), lineterm="", n=2)
        for l in list(body)[2:]:                 # drop difflib's ---/+++ file headers
            if l.startswith("@@"):
                continue                         # and its numbered hunk headers
            if not add(l):
                return "\n".join(out)
    return "\n".join(out)
```

In `ClaudeRuntime.__init__`: `self._edits: dict[str, tuple[str, dict]] = {}`. In `run_turn`, next to `self._streamed_text = False`: `self._edits.clear()`.

In `handle_line`, the `assistant` branch's `tool_use` case, after `emit(ToolStarted(...))`:

```python
                    if not child and name in _FILE_TOOLS and isinstance(b.get("input"), dict):
                        self._edits[b.get("id", "")] = (name, b["input"])
```

The `user` branch:

```python
        if t == "user":
            for b in (m.get("message") or {}).get("content") or []:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    text = _text_of(b.get("content"))
                    err = bool(b.get("is_error"))
                    tid = b.get("tool_use_id", "")
                    emit(ToolOutput(tid, text))
                    emit(ToolFinished(tid, not err, first_line(text) if err else ""))
                    edit = self._edits.pop(tid, None)
                    if edit is not None and not err:
                        diff = snippet_diff(edit[0], edit[1], self.cfg.diff_lines)
                        if diff:
                            emit(FileDiff(tid, str(edit[1].get("file_path") or ""), diff))
            return None
```

`src/tandem/chat/runtime/codex.py` — import `FileDiff`; the streaming branch:

```python
        elif method == "item/commandExecution/outputDelta":
            item_id, delta = params.get("itemId", ""), params.get("delta", "")
            self._streamed_output.add(item_id)
            emit(ToolOutput(item_id, delta))
        elif method == "item/fileChange/outputDelta":
            return None                          # the structured diff at item/completed is authoritative
```

The `fileChange` completion:

```python
            elif kind == "fileChange":
                ok = getattr(it, "status", "completed") == "completed"
                emit(ToolFinished(it.id, ok, ""))
                if ok:
                    for c in getattr(it, "changes", None) or []:
                        if getattr(c, "diff", ""):
                            emit(FileDiff(it.id, getattr(c, "path", "") or "", c.diff))
```

`src/tandem/chat/runtime/opencode.py` — import `FileDiff`; `TurnState` gains:

```python
    paths: dict = field(default_factory=dict)           # call id -> filePath of an edit/write
    finished: set = field(default_factory=set)          # call ids already finished (updates repeat)
```

The tool-part branch becomes:

```python
                if status in ("running", "completed", "error") and call_id not in st.started:
                    st.started.add(call_id)
                    inp = state.get("input")
                    fp = inp.get("filePath") if tool in ("edit", "write") and isinstance(inp, dict) else None
                    if isinstance(fp, str) and fp:
                        st.paths[call_id] = fp
                    emit(ToolStarted(call_id, tool, summarize_args(tool, inp),
                                     paths=(fp,) if isinstance(fp, str) and fp else ()))
                if status in ("completed", "error"):
                    if call_id in st.finished:
                        return                           # opencode repeats the completed update
                    st.finished.add(call_id)
                if status == "completed":
                    if state.get("output"):
                        emit(ToolOutput(call_id, state["output"]))
                    emit(ToolFinished(call_id, True, first_line(state.get("title") or "")))
                    meta = state.get("metadata") if isinstance(state.get("metadata"), dict) else part.get("metadata")
                    diff = meta.get("diff") if isinstance(meta, dict) else None
                    if isinstance(diff, str) and diff and tool in ("edit", "write"):
                        emit(FileDiff(call_id, st.paths.get(call_id, ""), diff))
                elif status == "error":
                    emit(ToolFinished(call_id, False, first_line(state.get("error") or "error")))
```

- [ ] **Step 4: Run the three runtime files, then the whole suite**

Run: `uv run pytest tests/test_chat_claude.py tests/test_chat_codex.py tests/test_chat_opencode.py -q`
Expected: all passed — the codex golden test still passes (its fileChange line, if any, now yields `FileDiff` instead of `ToolOutput`; adjust that golden assertion if it names the kinds, and ledger it).

Run: `uv run pytest -q`
Expected: all passed

- [ ] **Step 5: Commit**

```bash
git add src/tandem/chat/runtime tests/test_chat_claude.py tests/test_chat_codex.py tests/test_chat_opencode.py
git commit -m "Runtimes: a FileDiff after every successful edit

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 5: Docs, live gate, PR

**Files:**
- Modify: `docs/configuration.md` (`[chat]` paragraph), `README.md`

- [ ] **Step 1: Document**

`docs/configuration.md`, after the history paragraph in the `[chat]` section:

```
Replies are rendered as markdown — headings, emphasis, lists, tables and
fenced code — a paragraph or code block at a time as the model finishes
it, so text arrives in blocks rather than word by word (the activity line
shows the turn is still running). `markdown = false` streams the raw text
as it comes. Every file edit shows its diff under the tool row, `+` and `-`
coloured, capped to `diff_lines` (`0` for none): codex's is the patch it
applied; claude's is built from the edit's old and new text and labelled
`@@ edit @@` (`@@ new file @@` for a write), so it has no line numbers;
opencode's is what its edit tool reports.
```

`README.md`, after the `/mode` sentence: "Replies render as markdown, and every edit shows its diff."

- [ ] **Step 2: Live gate**

From a scratch project with all three harnesses (the `TANDEM_HOME` recipe from `/private/tmp/tandem-live-gate/gate_slash.py`, `skip_permissions = true`): a prompt asking for a reply with a heading, a bullet list and a python code block on claude — the rows render styled, none wider than the terminal, the closing row follows; the same on codex and opencode; an edit request on each harness (`/mode edits` first) — a `--- path` row and coloured `+`/`-` lines under the tool row, and for opencode whether a diff appears at all (its edit metadata) is the fact to record; a window with `markdown = false` in the config streams raw text. Record the outcome in the PR body.

- [ ] **Step 3: Commit, push, open the PR; codex review**

```bash
git add docs/configuration.md README.md
git commit -m "Docs: markdown replies and edit diffs in the chat window

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
git push -u origin chat-markdown-diffs
gh pr create --base main --title "Chat: markdown replies and edit diffs" --body-file /tmp/pr-body.md
```

Stage the branch diff and sources under a `/private/tmp` path without "git" in it and dispatch two `tandem:gpt` reviews — markdown module + renderer, and the three runtimes' diff emission — each capped at six minutes. Verify each finding before acting; fix real ones with a failing test first; record dismissed ones in the PR body.
