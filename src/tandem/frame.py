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
_PARTIAL_RE = re.compile(rb"\x1b(\[(<(\d{0,4}(;\d{0,4}){0,2};?)?|2(0[01]?)?)?)?\Z")
# longest byte string _PARTIAL_RE can match: ESC [ < 1234 ;1234 ;1234 ;
_MAX_PARTIAL = 18


def _split_partial(buf: bytes) -> tuple[bytes, bytes]:
    """Split off a trailing fragment of a tracked escape sequence, to be
    retried on the next feed. Anything that cannot become one is not held
    back (a lone ESC keypress must reach the child)."""
    i = buf.rfind(b"\x1b", max(0, len(buf) - _MAX_PARTIAL))
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


_DROP_RE = re.compile(rb"\x1b\[\d{1,4}(;\d{1,4})?r")     # child's own DECSTBM
# bare `\x1b[r` needs no guard against half-matching a parameterized region:
# that form spells its digits *before* the r, so the two can never share a
# prefix. `_DROP_RE` is scanned first regardless, so drop still wins.
_REASSERT_RE = re.compile(rb"\x1bc|\x1b\[\?1049[hl]|\x1b\[[23]J|\x1b\[r")


class OutputGuard:
    """Output-side watcher for the handful of sequences that clobber the
    reserved row or scroll region. Returns a verdict; never alters bytes.
    A small carry window handles sequences split across read chunks; only
    matches ending beyond the carry count (earlier ones fired last feed)."""

    # >= the longest watched sequence (12 bytes: ESC [ 1234 ;5678 r), so no
    # split point can hide one from both feeds it straddles
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
