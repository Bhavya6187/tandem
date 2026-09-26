"""The conversation view, in raw ANSI on the main screen.

The top of the screen is a DECSTBM scroll region the conversation prints
into, so the terminal's own scrollback and mouse wheel keep working; under
it sit a separator, the status bar (reverse video, like the frame's) and the
composer, which is one row until the draft needs more — the region gives up
rows as the composer grows and takes them back when it shrinks. Output is
append-only: a tool call is a row when it starts, its output tail, and a
status row when it ends — nothing is redrawn later.

The cursor lives in the region while a turn streams and in the composer
while the window waits for input. `_row` and `_col` track where the region
was left so a return from the composer resumes mid-line; the row is the
region's last until a shrinking composer hands rows back under it. Widths are
counted in terminal cells by `_cells`: the styled painters hand `print`
strings that already carry SGR wrappers, which move the cursor by nothing,
and a W/F glyph moves it by two. Every newline is CRLF — the window runs
the tty raw, so the terminal returns no carriage of its own."""

from __future__ import annotations

import re
import textwrap
from typing import Callable
from unicodedata import east_asian_width

from ..events import AssistantMessage, ToolCall, ToolResult, UserMessage
from .events import (ApprovalRequest, Failure, QuestionRequest, ReviewFinished, TextDelta,
                     ThinkingDelta, ToolFinished, ToolOutput, ToolStarted, TurnFinished, TurnStarted,
                     offered_labels)
from .activity import elapsed_text
from .runtime import first_line, summarize_args

_CSI = "\x1b["
_CSI_RE = re.compile(r"\x1b\[[0-9:;<=>?]*[ -/]*[@-~]")
# Every styled painter hands `print` a pre-wrapped string, so the column has
# to be counted in cells, not characters: an SGR sequence advances the cursor
# by nothing, and a W/F glyph (CJK, most emoji) by two.
_ERROR_BUFFER_LINES = 200
_ESCAPE_RE = re.compile(
    r"\x1b\[[0-9:;<=>?]*[ -/]*[@-~]"            # CSI … final
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?"      # OSC … BEL/ST (or truncated)
    r"|\x1b[PX^_][^\x1b]*(?:\x1b\\)?"           # DCS/SOS/PM/APC … ST
    r"|\x1b[@-Z\\-~]"                           # two-character escapes (ESC 7, ESC c)
)
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _safe(text: str) -> str:
    """Strip what a harness could otherwise use to drive the terminal: whole
    escape sequences, then every remaining C0 control (a bare ESC included)
    but the tab and the newlines the painters already handle themselves. A
    command string, tool name, tool output or model delta carrying CSI can
    erase and rewrite the approval row, escape the scroll region, or switch
    to the alternate screen. Applied before the SGR wrappers go on, so the
    renderer's own sequences survive — they are the only escapes that reach
    the terminal."""
    return _CONTROL_RE.sub("", _ESCAPE_RE.sub("", text or ""))


def _cells(text: str) -> int:
    return sum(2 if east_asian_width(ch) in ("W", "F") else 1
               for ch in _CSI_RE.sub("", text))


def _clip(text: str, cells: int) -> str:
    """The longest prefix of unstyled `text` that fits in `cells`."""
    used = 0
    for i, ch in enumerate(text):
        used += 2 if east_asian_width(ch) in ("W", "F") else 1
        if used > cells:
            return text[:i]
    return text


_CLOSING = {"completed": "✓ done", "interrupted": "■ interrupted", "failed": "✗ failed"}


class Screen:
    def __init__(self, write: Callable[[bytes], None], rows: int, cols: int, cfg, *,
                 color: bool = True):
        self.write = write
        self.rows = max(4, rows)
        self.cols = max(10, cols)
        self.cfg = cfg
        self.color = color
        self._bottom = 1                # composer rows under the bar
        self._row, self._col = self.region_rows, 0
        self._focus = "region"          # "region" | "composer"
        self._speaker_shown = False
        self._turn_harness = ""
        self._tool_lines: dict[str, int] = {}
        self._tool_held: dict[str, list[str]] = {}
        self._tool_dropped: dict[str, int] = {}
        self._tool_partial: dict[str, str] = {}      # the unterminated tail of a call's output

    @property
    def region_rows(self) -> int:
        return max(1, self.rows - 2 - self._bottom)

    @property
    def composer_max_rows(self) -> int:
        return max(1, min(8, self.rows // 3))

    # -- plumbing ------------------------------------------------------------

    def _w(self, s: str) -> None:
        # a model's text can carry an unpaired surrogate; a strict encode
        # would raise out of the paint path and take the window down
        self.write(s.encode(errors="replace"))

    def _region_cmd(self) -> str:
        return f"{_CSI}1;{self.region_rows}r"

    def enter(self, *, fresh: bool = False) -> None:
        """`fresh` is the window opening, as opposed to a Ctrl-L repaint: the
        terminal still shows whatever ran before — the shell, a previous
        session — and the region would overwrite it cell by cell. One LF per
        row from the bottom row, with no region set yet, scrolls it all into
        scrollback; ED 2 would erase it outright on most terminals."""
        if fresh:
            self._w(f"{_CSI}r{_CSI}{self.rows};1H" + "\r\n" * self.rows)
        self._w(f"{_CSI}?2004h" + self._region_cmd() + f"{_CSI}{self.region_rows};1H")
        self._row, self._col, self._focus = self.region_rows, 0, "region"

    def leave(self) -> None:
        # the separator, bar and composer are painted rows like any other:
        # left behind they scroll into the terminal's scrollback as soon as
        # the shell prompt returns, so erase from the top of the block down
        self._w(f"{_CSI}r{_CSI}?2004l{_CSI}{self.region_rows + 1};1H{_CSI}J\r\n")

    def resize(self, rows: int, cols: int) -> None:
        self.rows, self.cols = max(4, rows), max(10, cols)
        self._bottom = min(self._bottom, self.rows - 3)
        self._row, self._col = self.region_rows, min(self._col, self.cols - 1)
        self._w("\x1b7" + self._region_cmd() + "\x1b8")
        self._focus = "composer"        # force a reposition before the next region write

    def _goto_region(self) -> None:
        if self._focus != "region":
            self._w(f"{_CSI}{self._row};{self._col + 1}H")
            self._focus = "region"

    def print(self, text: str) -> None:
        """Append to the region, tracking the bottom-row column.

        Every newline is CRLF: the window runs the tty raw, so OPOST/ONLCR is
        off and a bare LF would drop a row without returning the carriage."""
        if not text:
            return
        self._goto_region()
        segments = text.split("\n")
        for i, seg in enumerate(segments):
            if i:
                self._newline()
            if seg:
                self._w(seg)
                # a segment wider than the row is wrapped by the terminal
                # itself; the cursor lands in the next row at the remainder
                total = self._col + _cells(seg)
                self._row = min(self.region_rows, self._row + max(0, total - 1) // self.cols)
                self._col = total % self.cols
                if total and self._col == 0:
                    # exactly on the edge: the terminal's pending-wrap state
                    # does not survive a cursor move, so end the line here
                    # rather than guess
                    self._newline()

    def _newline(self) -> None:
        self._w("\r\n")
        self._row, self._col = min(self.region_rows, self._row + 1), 0

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
        self._tool_lines.clear(); self._tool_held.clear(); self._tool_dropped.clear()
        self._tool_partial.clear()
        label = f"you → {ev.harness}" + (f" · {ev.model}" if ev.model else "")
        prompt = _safe(ev.prompt)
        self.line()
        if "\n" in prompt:
            self.line(self._bold(label))
            self.print(prompt + "\n")
        else:
            self.line(self._bold(label) + "  " + prompt)
        if ev.carried:
            self.line(self._dim(f"  + navigator note: {_safe(ev.carried)}"))

    def text_delta(self, ev: TextDelta) -> None:
        self._ensure_speaker()
        self.print(_safe(ev.text))

    def thinking_delta(self, ev: ThinkingDelta) -> None:
        if self.cfg.show_thinking:
            self._ensure_speaker()
            self.print(self._dim(_safe(ev.text)))

    def tool_started(self, ev: ToolStarted) -> None:
        self._ensure_speaker()
        self._tool_lines[ev.call_id] = 0
        self._tool_held[ev.call_id] = []
        self._tool_dropped[ev.call_id] = 0
        self._tool_partial[ev.call_id] = ""
        self.line(self._dim(f"  ▸ {_safe(ev.tool)} {_safe(ev.summary)}".rstrip()))

    def tool_output(self, ev: ToolOutput) -> None:
        """Print the head of the output up to the cap and hold the rest: a
        call that turns out to have failed gets its tail flushed by
        tool_finished, where the lines the user actually needs are. The hold
        is bounded — past _ERROR_BUFFER_LINES the overflow is only counted.

        Output streams in arbitrary chunks, so a line counts when its newline
        arrives; the unterminated tail waits for the next chunk, or for
        tool_finished."""
        buf = self._tool_partial.get(ev.call_id, "") + ev.text
        *lines, self._tool_partial[ev.call_id] = buf.split("\n")
        for line in lines:
            self._tool_line(ev.call_id, line)

    def _tool_line(self, call_id: str, line: str) -> None:
        raw = _safe(line.rstrip("\r"))
        if self._tool_lines.get(call_id, 0) < self.cfg.tool_output_lines:
            self._tool_lines[call_id] = self._tool_lines.get(call_id, 0) + 1
            self.line(self._dim("    " + raw))
        else:
            held = self._tool_held.setdefault(call_id, [])
            if len(held) < _ERROR_BUFFER_LINES:
                held.append(raw)
            else:
                self._tool_dropped[call_id] = self._tool_dropped.get(call_id, 0) + 1

    def tool_finished(self, ev: ToolFinished) -> None:
        tail = self._tool_partial.pop(ev.call_id, "")
        if tail:
            self._tool_line(ev.call_id, tail)
        held = self._tool_held.pop(ev.call_id, [])
        beyond = self._tool_dropped.pop(ev.call_id, 0)
        self._tool_lines.pop(ev.call_id, None)
        if not ev.ok:
            for raw in held:
                self.line(self._dim("    " + raw))
            held = []
        hidden = len(held) + beyond
        if hidden:
            self.line(self._dim(f"    … +{hidden} lines"))
        status = "ok" if ev.ok else "error"
        summary = _safe(ev.summary)
        self.line(self._dim(f"    {status}" + (f" · {summary}" if summary else "")))

    def approval(self, ev: ApprovalRequest) -> None:
        self.line(self._bold(f"  ▸ Allow {_safe(ev.kind)}: {_safe(ev.detail)}")
                  + "   " + offered_labels(ev.choices))

    def question(self, ev: QuestionRequest) -> None:
        self.line(self._bold(f"  ? {_safe(ev.prompt)}"))
        for i, option in enumerate(ev.options, 1):
            self.line(f"    {i}. {_safe(option)}")
        if not ev.options:
            self.line(self._dim("    (type an answer)"))

    def turn_finished(self, ev: TurnFinished, elapsed: float | None = None) -> None:
        """Every turn gets a closing row, a completed one with nothing to
        report included: the region is append-only, and without it a finished
        answer and a model gone quiet look the same."""
        if self._col:
            self.print("\n")
        bits = [_CLOSING.get(ev.status, _safe(ev.status))]
        if elapsed is not None:
            bits.append(elapsed_text(elapsed))
        if ev.usage:
            bits.append(ev.usage)
        self.line(self._dim("  " + " · ".join(bits)))

    def failure(self, ev: Failure) -> None:
        # a failure message is usually the harness's own: a stderr tail, a
        # provider error, a protocol line tandem could not read
        self.line(self._bold("error: ") + _safe(ev.message))

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
        # the two-space indent plus a row one short of the edge: a row that
        # fills the width trips print's edge newline and leaves a blank row
        width = max(8, self.cols - 3)
        for para in _safe(v.note).split("\n"):
            for row in textwrap.wrap(para, width) or [""]:
                self.line("  " + row)
        for e in v.evidence:
            where = f"{_safe(e.file)}:{e.line}" + (f" — {_safe(e.why)}" if e.why else "")
            self.line(self._dim("  " + _clip(where, width)))

    def note(self, text: str) -> None:
        """A dim line outside any turn. Sanitized like every other painter:
        a `/model` row or a `/help` description is the harness's text."""
        self.line(self._dim(_safe(text)))

    def bell(self) -> None:
        self._w("\x07")                 # moves nothing, so it is safe from either focus

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
                self.line(_safe(ev.text))
            elif isinstance(ev, ToolCall):
                summary = summarize_args(ev.tool, ev.arguments)
                self.line(self._dim(f"  ▸ {_safe(ev.tool)} {_safe(summary)}".rstrip()))
            elif isinstance(ev, ToolResult):
                text = _safe(first_line(ev.output))
                if text:
                    self.line(self._dim("    " + text))

    # -- bottom block ------------------------------------------------------------

    def _fit_bottom(self, n: int) -> str:
        """Make the composer `n` rows tall. Growing takes rows off the region's
        end: what the conversation has on them is scrolled up first — off the
        region's top, into scrollback — and the cursor's row goes with it.
        Shrinking hands rows back blank; the conversation stays where it is
        and `_row` lets it carry on from there."""
        old_top, old = self.region_rows + 1, self._bottom
        self._bottom = n
        if n > old:
            push = max(0, self._row - self.region_rows)
            self._row -= push
            return (f"{_CSI}{old_top - 1};1H" + "\n" * push if push else "") + self._region_cmd()
        return self._region_cmd() + "".join(f"{_CSI}{r};1H{_CSI}2K"
                                            for r in range(old_top, self.region_rows + 1))

    def _separator(self, activity: str, urgent: bool) -> str:
        """The rule over the bar, carrying the activity line while a turn
        runs: `── ⠹ claude · thinking · 12s ────`. Exactly `cols` cells — one
        more wraps onto the bar's row."""
        if not activity:
            return self._dim("─" * self.cols)
        text = "── " + _clip(_safe(activity), max(0, self.cols - 4)) + " "
        text = _clip(text, self.cols)
        text += "─" * (self.cols - _cells(text))
        return self._bold(text) if urgent else self._dim(text)

    def paint_bottom(self, bar_line: str, composer_rows: list[str], cursor_row: int,
                     cursor_col: int, focus_composer: bool, *, activity: str = "",
                     urgent: bool = False) -> None:
        composer_rows = composer_rows[: self.rows - 3] or [""]
        moved = len(composer_rows) != self._bottom
        out = ["\x1b7"]
        if moved:
            out.append(self._fit_bottom(len(composer_rows)))
        top = self.region_rows + 1
        out += [f"{_CSI}{top};1H" + self._separator(activity, urgent),
                f"{_CSI}{top + 1};1H{_CSI}7m" + bar_line[: self.cols].ljust(self.cols) + f"{_CSI}0m"]
        out += [f"{_CSI}{top + 2 + i};1H{_CSI}2K" + row[: self.cols]
                for i, row in enumerate(composer_rows)]
        if focus_composer:
            row = top + 2 + min(cursor_row, len(composer_rows) - 1)
            out.append(f"{_CSI}{row};{min(cursor_col, self.cols - 1) + 1}H")
            self._focus = "composer"
        elif moved:
            # ESC 8 would restore where the cursor was; a push moved the line it was on
            out.append(f"{_CSI}{self._row};{self._col + 1}H")
        else:
            out.append("\x1b8")
        self._w("".join(out))
