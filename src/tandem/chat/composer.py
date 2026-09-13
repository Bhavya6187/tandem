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

from .events import ApprovalRequest, QuestionRequest, offered_labels


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


def approval_row(choices: tuple[str, ...] | None = None) -> str:
    return f" {offered_labels(choices)}  (Esc denies and interrupts)"


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
                end = data.find(_PASTE_END, i)
                if end == -1:
                    # a partial end bracket at the tail belongs to the next read
                    chunk = data[i:]
                    held = _trailing_prefix(chunk, _PASTE_END)
                    if held:
                        chunk, self._carry = chunk[:-held], chunk[-held:]
                    self._insert(self._decoder.decode(chunk))
                    break
                self._insert(self._decoder.decode(data[i:end]))
                self._paste = False
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
                self._typed(self._decoder.decode(data[j:i]), actions, first=j == 0)
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
            return approval_row(getattr(self.pending, "choices", None))[:cols], 0
        prompt = "? " if self.mode == "question" else "> "
        text, cur = self.text, self.cur
        nl = text.find("\n")
        if nl != -1:
            text = text[:nl] + f" (+{text.count(chr(10))} lines)"
            cur = min(cur, nl)
        avail = max(1, cols - len(prompt))
        start = cur - avail + 1 if cur >= avail else 0
        return prompt + text[start:start + avail], len(prompt) + (cur - start)


def _trailing_prefix(data: bytes, marker: bytes) -> int:
    """Length of the longest proper prefix of `marker` that `data` ends with."""
    for n in range(min(len(data), len(marker) - 1), 0, -1):
        if data.endswith(marker[:n]):
            return n
    return 0
