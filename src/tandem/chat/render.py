"""The conversation view, in raw ANSI on the main screen.

Rows 1..rows-3 are a DECSTBM scroll region the conversation prints into, so
the terminal's own scrollback and mouse wheel keep working; rows-2 is a
separator, rows-1 the status bar (reverse video, like the frame's), rows the
composer. Output is append-only: a tool call is a row when it starts, its
output tail, and a status row when it ends — nothing is redrawn later.

The cursor lives in the region while a turn streams and in the composer
while the window waits for input. `_col` tracks where the region's bottom
row was left so a return from the composer resumes mid-line. Widths are
counted in terminal cells by `_cells`: the styled painters hand `print`
strings that already carry SGR wrappers, which move the cursor by nothing,
and a W/F glyph moves it by two. Every newline is CRLF — the window runs
the tty raw, so the terminal returns no carriage of its own."""

from __future__ import annotations

import re
from typing import Callable
from unicodedata import east_asian_width

from ..events import AssistantMessage, ToolCall, ToolResult, UserMessage
from .events import (ApprovalRequest, Failure, QuestionRequest, TextDelta, ThinkingDelta,
                     ToolFinished, ToolOutput, ToolStarted, TurnFinished, TurnStarted,
                     offered_labels)
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
        self._tool_held: dict[str, list[str]] = {}
        self._tool_dropped: dict[str, int] = {}
        self._tool_partial: dict[str, str] = {}      # the unterminated tail of a call's output

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
        # the separator, bar and composer are painted rows like any other:
        # left behind they scroll into the terminal's scrollback as soon as
        # the shell prompt returns, so erase from the top of the block down
        self._w(f"{_CSI}r{_CSI}?2004l{_CSI}{self.rows - 2};1H{_CSI}J\r\n")

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
        """Append to the region, tracking the bottom-row column.

        Every newline is CRLF: the window runs the tty raw, so OPOST/ONLCR is
        off and a bare LF would drop a row without returning the carriage."""
        if not text:
            return
        self._goto_region()
        segments = text.split("\n")
        for i, seg in enumerate(segments):
            if i:
                self._w("\r\n")
                self._col = 0
            if seg:
                self._w(seg)
                # a segment wider than the row is wrapped by the terminal
                # itself; the cursor lands in the next row at the remainder
                total = self._col + _cells(seg)
                self._col = total % self.cols
                if total and self._col == 0:
                    # exactly on the edge: the terminal's pending-wrap state
                    # does not survive a cursor move, so end the line here
                    # rather than guess
                    self._w("\r\n")

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

    def turn_finished(self, ev: TurnFinished) -> None:
        if self._col:
            self.print("\n")
        if ev.usage:
            self.line(self._dim(f"  {ev.status} · {ev.usage}"))
        elif ev.status != "completed":
            self.line(self._dim(f"  {ev.status}"))

    def failure(self, ev: Failure) -> None:
        # a failure message is usually the harness's own: a stderr tail, a
        # provider error, a protocol line tandem could not read
        self.line(self._bold("error: ") + _safe(ev.message))

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
                self.line(_safe(ev.text))
            elif isinstance(ev, ToolCall):
                summary = summarize_args(ev.tool, ev.arguments)
                self.line(self._dim(f"  ▸ {_safe(ev.tool)} {_safe(summary)}".rstrip()))
            elif isinstance(ev, ToolResult):
                text = _safe(first_line(ev.output))
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
