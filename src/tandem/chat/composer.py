"""The editor at the bottom of the window, and the key parser that drives
it. Pure: bytes in, actions out, no terminal access.

Three modes. `prompt` edits text and submits on Enter; `approval` answers
a permission request with one key — a lone keypress at the head of a read,
so two keystrokes the terminal coalesced into one (`yn`) answer nothing and
land in the draft, by design; `question` picks a numbered option or takes
free text. The draft is multi-line: Option-Enter, Ctrl-J, a backslash
before Enter, and Shift-Enter where the terminal reports it all break the
line, and a bracketed paste keeps its newlines. `rows` lays the draft out
as the rows the window paints — long lines wrapped, a tall draft scrolled
around the cursor. A partial escape sequence at the end of a read is
carried to the next one; a lone Esc is a key.

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
command is sent as written."""

from __future__ import annotations

import codecs
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Union
from unicodedata import east_asian_width

from .commands import Command
from .events import ApprovalRequest, QuestionRequest, offered_labels
from .files import match


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
_PASTE_END = b"\x1b[201~"
# Meta (Option) keys arrive as Esc + the key: macOS terminals send Esc b /
# Esc f for option-left / option-right, Esc DEL for option-backspace.
_META = {ord("b"): "word_left", ord("f"): "word_right", ord("d"): "word_delete",
         0x7F: "word_backspace", 0x08: "word_backspace",
         0x0D: "newline", 0x0A: "newline"}
# Shift-Enter (any modified Enter) from a terminal that reports it: CSI-u, or
# xterm's modifyOtherKeys. Neither is switched on from here.
_MODIFIED_ENTER = re.compile(r"13;[2-9]\d*u|27;[2-9]\d*;13~")
_NO_WRAP = 1 << 30
_WORD_ARROW = {"left": "word_left", "right": "word_right"}
_PICKER_ROWS = 6
_PICKER_MATCHES = 50
_CMD_NAME = re.compile(r"[A-Za-z0-9_-]*")


def approval_row(choices: tuple[str, ...] | None = None) -> str:
    return f" {offered_labels(choices)}  (Esc denies and interrupts)"


class Composer:
    def __init__(self, history_limit: int = 200,
                 paths: Callable[[], list[str]] | None = None,
                 commands: Callable[[], list[Command]] | None = None):
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
        self._paste_cr = False
        self._carry = b""
        self._avail = _NO_WRAP          # the width `rows` last wrapped to: Up/Down follow those rows
        self._top = 0
        self._goal: tuple[int, int] | None = None   # (cursor, column) a run of Up/Down aims for
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
                end = data.find(_PASTE_END, i)
                if end == -1:
                    # a partial end bracket at the tail belongs to the next read
                    chunk = data[i:]
                    held = _trailing_prefix(chunk, _PASTE_END)
                    if held:
                        chunk, self._carry = chunk[:-held], chunk[-held:]
                    self._pasted(self._decoder.decode(chunk))
                    break
                self._pasted(self._decoder.decode(data[i:end]))
                self._paste = self._paste_cr = False
                i = end + len(_PASTE_END)
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
                    if self.candidates:
                        self._dismissed = self._locate()[1]
                    else:
                        actions.append(Cancel() if self.mode != "prompt" else Interrupt())
                elif name:
                    self._key(name)
                continue
            i += 1
            if b == 0x03:
                actions.append(CtrlC())
            elif b == 0x0C:
                actions.append(Repaint())
            elif b == 0x0D:
                self._enter(actions)
            elif b == 0x0A:                       # Ctrl-J: the tty is raw, so Enter is CR
                self._key("newline")
            elif b in (0x7F, 0x08):
                self._backspace()
            elif b == 0x01:
                self.cur = self._line_start()
            elif b == 0x05:
                self.cur = self._line_end()
            elif b == 0x15:
                # at a line's edge a kill takes the newline, so the lines join
                start = self._line_start()
                start = max(0, self.cur - 1) if start == self.cur else start
                del self.buf[start:self.cur]
                self.cur = start
            elif b == 0x0B:
                end = self._line_end()
                del self.buf[self.cur:end + 1 if end == self.cur else end]
            elif b == 0x09:
                if self.candidates:
                    self._accept()
            elif b < 0x20:
                pass                              # other control bytes: ignored
            else:
                j = i - 1
                while i < len(data) and data[i] >= 0x20 and data[i] not in (0x7F, 0x1B):
                    i += 1
                self._typed(self._decoder.decode(data[j:i]), actions, first=j == 0)
        self._sync_picker()
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
            if _MODIFIED_ENTER.fullmatch(params + chr(final)):
                return "newline", j + 1
            if final == ord("~"):
                return _TILDE.get(params, ""), j + 1
            name = _CSI_FINAL.get(final, "")
            if params.rpartition(";")[2] not in ("", "1", "2"):   # alt/ctrl arrow (1;3D, 1;5C): by word
                name = _WORD_ARROW.get(name, name)
            return name, j + 1
        if data[1] in _META:
            return _META[data[1]], 2
        if data[1:2] == b"O":
            if len(data) < 3:
                return "", 0
            return _CSI_FINAL.get(data[2], ""), 3
        return "esc", 1                           # Esc then an ordinary key

    def _offers(self, choice: str) -> bool:
        """A key for a choice this request does not offer answers nothing."""
        choices = getattr(self.pending, "choices", None)
        return choice in choices if choices else True

    def _typed(self, text: str, actions: list[Action], *, first: bool = False) -> None:
        if not text:
            return
        if self.mode == "approval":
            # An answer is a lone keypress at the head of the read, never a
            # character scanned out of a longer run: the window drains live
            # events — painting the approval row — before it reads stdin in
            # the same pass, so text with anything glued to it was already in
            # the tty buffer when the request appeared. Answering from it
            # would approve a command the user has not seen ("and then fix
            # the tests" both starts with and contains an `a`). What is not
            # an answer stays in the composer rather than being swallowed.
            choice = _APPROVAL_KEYS.get(text.lower()) if first and len(text) == 1 else None
            if choice is not None and self._offers(choice):
                actions.append(Answer(choice))
            else:
                self._insert(text)
            return
        if (self.mode == "question" and self.pending is not None and self.pending.options
                and not self.buf and text.strip().isdecimal()):
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
            self.cur = self._line_start()
        elif name == "end":
            self.cur = self._line_end()
        elif name == "newline":
            if self.mode != "approval":
                self._insert("\n")
        elif name == "delete":
            if self.cur < len(self.buf):
                del self.buf[self.cur]
        elif name == "word_left":
            self.cur = self._word_left()
        elif name == "word_right":
            self.cur = self._word_right()
        elif name == "word_backspace":
            start = self._word_left()
            del self.buf[start:self.cur]
            self.cur = start
        elif name == "word_delete":
            del self.buf[self.cur:self._word_right()]
        elif name in ("up", "down") and self.candidates:
            self.selected = (self.selected + (1 if name == "down" else -1)) % len(self.candidates)
        elif name == "up":
            self._vertical(-1) or self._history_step(-1)
        elif name == "down":
            self._vertical(1) or self._history_step(1)

    # -- the pickers -----------------------------------------------------------

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
        """`"path"`, `"command"`, or `""` for a closed picker."""
        self._sync_picker()
        return self._kind

    @property
    def candidates(self) -> list:
        """What the picker is offering — paths or Commands; empty is closed."""
        return self._sync_picker()

    def _sync_picker(self) -> list:
        """Bring the picker in line with the draft. Items are listed once per
        word, not once per keystroke; the selection starts over when the
        query changes. A name typed out in full is not offered back — Enter
        has to submit it — and for a command that includes a full name with
        a longer sibling (`/mode` beside `/model`)."""
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

    def _line_start(self) -> int:
        i = self.cur
        while i > 0 and self.buf[i - 1] != "\n":
            i -= 1
        return i

    def _line_end(self) -> int:
        i, n = self.cur, len(self.buf)
        while i < n and self.buf[i] != "\n":
            i += 1
        return i

    def _vertical(self, step: int) -> bool:
        """Move a row up or down at the same column; False at the draft's
        first or last row, where the key steps through history instead."""
        rows, starts, (r, col) = self._layout(self._avail)
        if self._goal is not None and self._goal[0] == self.cur:
            col = self._goal[1]                   # a short row in between does not pull the column in
        t = r + step
        if not 0 <= t < len(rows):
            return False
        # the index a row's start + 1 belongs to is the last the cursor can
        # hold on the row above: its newline, or the character that wrapped
        i, end, width = starts[t], starts[t + 1] - 1 if t + 1 < len(rows) else len(self.buf), 0
        while i < end and width + _width(self.buf[i]) <= col:
            width += _width(self.buf[i])
            i += 1
        self.cur, self._goal = i, (i, col)
        return True

    def _word_left(self) -> int:
        i = self.cur
        while i > 0 and self.buf[i - 1].isspace():
            i -= 1
        while i > 0 and not self.buf[i - 1].isspace():
            i -= 1
        return i

    def _word_right(self) -> int:
        i, n = self.cur, len(self.buf)
        while i < n and self.buf[i].isspace():
            i += 1
        while i < n and not self.buf[i].isspace():
            i += 1
        return i

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
        # a recalled prompt that ends in a mention, or is a command, does not
        # open the picker: the next Up has to keep stepping through history
        loc = self._locate()
        self._dismissed = loc[1] if loc else None

    def _insert(self, text: str) -> None:
        if text:
            self.buf[self.cur:self.cur] = list(text)
            self.cur += len(text)

    def _pasted(self, text: str) -> None:
        """Terminals paste a line break as CR or CRLF; the draft holds LF. A
        CRLF can straddle two reads."""
        if self._paste_cr and text.startswith("\n"):
            text = text[1:]
        if text:
            self._paste_cr = text.endswith("\r")
        self._insert(text.replace("\r\n", "\n").replace("\r", "\n"))

    def _backspace(self) -> None:
        if self.cur > 0:
            del self.buf[self.cur - 1]
            self.cur -= 1

    def _enter(self, actions: list[Action]) -> None:
        if self.mode == "approval":
            return
        if self.candidates:
            self._accept()
            return
        if self.cur and self.buf[self.cur - 1] == "\\":   # a backslash before Enter continues the line
            self.buf[self.cur - 1] = "\n"
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

    # -- rows ------------------------------------------------------------------

    def _layout(self, avail: int) -> tuple[list[str], list[int], tuple[int, int]]:
        """The draft as visual rows `avail` cells wide: (row texts, the buffer
        index each row starts at, the cursor's (row, cell column))."""
        rows, starts, width, pos = [""], [0], 0, (0, 0)
        for i, ch in enumerate([*self.buf, ""]):  # "" is the cursor's slot past the end
            w = 0 if ch in ("", "\n") else _width(ch)
            # the cursor takes a cell of its own, so past a full row it wraps too
            if width and width + max(w, i == self.cur) > avail:
                rows.append(""); starts.append(i); width = 0
            if i == self.cur:
                pos = (len(rows) - 1, width)
            if ch == "\n":
                rows.append(""); starts.append(i + 1); width = 0
            elif ch:
                rows[-1] += ch if ch >= " " else " "     # a pasted tab is one cell, like its width
                width += w
        return rows, starts, pos

    def rows(self, cols: int, max_rows: int) -> tuple[list[str], int, int]:
        """(the rows to paint, the cursor's row among them, its column).
        Continuation rows sit under the prompt; a draft taller than
        `max_rows` scrolls only when the cursor leaves the view."""
        if self.mode == "approval":
            return [approval_row(getattr(self.pending, "choices", None))[:cols]], 0, 0
        prompt = "? " if self.mode == "question" else "> "
        self._avail = max(1, cols - len(prompt))
        rows, _, (r, col) = self._layout(self._avail)
        picks = self.candidates
        n = min(len(picks), _PICKER_ROWS, max_rows - 1)
        max_rows -= n
        top = min(self._top, max(0, len(rows) - max_rows), r)
        self._top = top = max(top, r - max_rows + 1)
        shown = [(" " * len(prompt) if i else prompt) + row
                 for i, row in enumerate(rows)][top:top + max_rows]
        # the picker's rows ride under the draft and scroll with the selection
        self._pick_top = min(max(self._pick_top, self.selected - n + 1), self.selected)
        if self._kind == "command":
            width = max((len(c.name) for c in picks), default=0) + 2
            shown += [("  ❯ " if i == self.selected else "    ")
                      + f"/{picks[i].name}".ljust(width) + " " + _printable(picks[i].description)
                      for i in range(self._pick_top, self._pick_top + n)]
        else:
            shown += [("  ❯ " if i == self.selected else "    ") + _printable(picks[i])
                      for i in range(self._pick_top, self._pick_top + n)]
        return shown, r - top, len(prompt) + col


def _printable(text: str) -> str:
    return "".join(ch if ch >= " " and ch != "\x7f" else "?" for ch in text)


def _width(ch: str) -> int:
    return 2 if east_asian_width(ch) in ("W", "F") else 1


def _trailing_prefix(data: bytes, marker: bytes) -> int:
    """Length of the longest proper prefix of `marker` that `data` ends with."""
    for n in range(min(len(data), len(marker) - 1), 0, -1):
        if data.endswith(marker[:n]):
            return n
    return 0
