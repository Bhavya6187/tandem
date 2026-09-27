"""Markdown for the chat window's replies, in two pure pieces.

`ready_blocks` decides what a streaming reply has finished: everything up
to the last blank line outside a fenced code block that ends a block, or
up to a fence's closer. It looks only at complete lines, so a fence marker
split across two deltas is never misread, and the unterminated tail always
waits. A blank line is a boundary only once the next non-blank line has
arrived and does not continue a container — an indented continuation, the
next item of a loose list, another quoted line.

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
_OSC = re.compile(r"\x1b\][^\x07\x1b\n]*(?:\x07|\x1b\\)?")   # never across a row break
_SGR = re.compile(r"\x1b\[[0-9;]*m")
_TRAILING_PAD = re.compile(r"[ \t]+((?:\x1b\[[0-9;]*m)*)$")


def open_fence(buffer: str) -> str | None:
    """The opener line of a fence still open at the end of `buffer`'s
    complete lines, or None. What a flush inside a block needs to close
    the block and reopen it for the rest."""
    fence: tuple[str, int] | None = None
    opener = None
    for line in buffer.split("\n")[:-1]:
        m = _FENCE.match(line)
        if fence is None:
            if m:
                fence, opener = (m.group(1)[0], len(m.group(1))), line
        elif (m and m.group(1)[0] == fence[0] and len(m.group(1)) >= fence[1]
              and not m.group(2).strip()):
            fence, opener = None, None
    return opener


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
    while rows and not _SGR.sub("", rows[0]).strip():
        rows.pop(0)                             # and the leading one it gives a list: the block
    return rows                                 # before this one already ended on a blank row
