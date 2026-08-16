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

# kitty CSI-u modifier field is 1 + a bitmask; the flip chord is ctrl (4)
# with nothing else held except the lock keys (capslock 64, numlock 128),
# which real keyboards leave lit all the time
_CSIU_CTRL = 4
_CSIU_DISQUALIFIERS = 1 | 2 | 8 | 16 | 32   # shift, alt, super, hyper, meta


def _csiu_codepoint(flip_byte: int) -> int | None:
    """The codepoint a kitty-protocol terminal reports for the flip chord.

    Once a child pushes kitty enhancement flags (codex's TUI does, on any
    terminal that supports them), Ctrl-] arrives as ``\\x1b[93;5u`` and the
    raw 0x1D byte never appears — so the detector must recognize this
    spelling too, or the flip key is silently dead for that whole phase.

    Letters report their lowercase codepoint; 0x1B-0x1D report the
    unshifted punctuation key. 0x1E/0x1F need Shift on common layouts (the
    modifier field would not be plain ctrl), so they keep legacy-byte
    detection only."""
    if 0x01 <= flip_byte <= 0x1A:
        return flip_byte + 96
    if flip_byte in (0x1B, 0x1C, 0x1D):
        return flip_byte + 64
    return None


class FlipDetector:
    """Input-side filter: consume the flip key — as its raw control byte or
    its kitty CSI-u spelling — outside bracketed paste, swallow SGR mouse
    events aimed at the bar row, forward everything else byte-for-byte."""

    def __init__(self, flip_byte: int, bar_row: int | None = None):
        self.flip_byte = flip_byte
        self.bar_row = bar_row
        self._in_paste = False
        self._carry = b""
        cp = _csiu_codepoint(flip_byte)
        if cp is None:
            self._csiu_re = None
            self._partial_re = _PARTIAL_RE
            self._max_partial = _MAX_PARTIAL
        else:
            digits = str(cp).encode()
            # \x1b[<cp>(:shifted(:base)?)?;<mods>(:<event>)?u — alternates
            # come from the "report alternate keys" flag, the event type
            # (1 press, 2 repeat, 3 release) from "report event types";
            # both are part of the >7u push codex uses.
            self._csiu_re = re.compile(
                rb"\x1b\[" + digits
                + rb"(?::\d{1,7}(?::\d{1,7})?)?;(\d{1,3})(?::(\d{1,2}))?u"
            )
            # a trailing fragment that could still become the flip sequence
            # must be carried like the mouse/paste fragments: nested
            # optionals so every prefix of the digits (then of the
            # alternates/mods tail) matches up to \Z
            frag = rb"(?::\d{0,7}(?::\d{0,7})?)?(?:;\d{0,3}(?::\d{0,2})?)?"
            for ch in reversed(digits[1:]):
                frag = b"(?:" + bytes([ch]) + frag + b")?"
            frag = digits[:1] + frag
            self._partial_re = re.compile(
                rb"\x1b(\[(<(\d{0,4}(;\d{0,4}){0,2};?)?|2(0[01]?)?|"
                + frag + rb")?)?\Z"
            )
            # ESC [ + 3 digits + :1234567:1234567 + ;123 + :12
            self._max_partial = 32

    def _split_partial(self, buf: bytes) -> tuple[bytes, bytes]:
        """Split off a trailing fragment of a tracked escape sequence, to be
        retried on the next feed. Anything that cannot become one is not held
        back (a lone ESC keypress must reach the child)."""
        i = buf.rfind(b"\x1b", max(0, len(buf) - self._max_partial))
        if i == -1:
            return buf, b""
        m = self._partial_re.match(buf, i)
        if m and m.group(0) != b"":
            return buf[:i], buf[i:]
        return buf, b""

    def flush(self) -> bytes:
        """Release a stranded fragment verbatim. A carried prefix waits for
        bytes that may never come — a lone ESC is the interrupt key in both
        TUIs — so the pump flushes on an idle tick. Only the carry is
        cleared: paste state is not carry state, and forgetting mid-paste
        would re-arm the flip byte inside someone's pasted text."""
        carry, self._carry = self._carry, b""
        return carry

    def feed(self, data: bytes) -> tuple[bytes, int]:
        buf = self._carry + data
        buf, self._carry = self._split_partial(buf)
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
                if self._csiu_re is not None:
                    m = self._csiu_re.match(buf, i)
                    if m:
                        bits = int(m.group(1)) - 1
                        if bits & _CSIU_CTRL and not bits & _CSIU_DISQUALIFIERS:
                            # ours: count press (bare or :1) and repeat (:2,
                            # matching legacy autorepeat), swallow the
                            # release (:3) — the child never saw the press,
                            # so it must not see the key come back up.
                            if m.group(2) in (None, b"1", b"2"):
                                flips += 1
                            i = m.end()
                            continue
                        # same key, different chord (e.g. ctrl+shift):
                        # not the flip — forward it whole
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


# One union of everything watched, scanned in stream order. The
# parameterized-DECSTBM branch (digits before the r) and the bare `\x1b[r`
# branch can never share a prefix, so the alternation is unambiguous.
_WATCH_RE = re.compile(
    rb"\x1b\[(\d{1,4})(?:;(\d{1,4}))?r"     # child DECSTBM with parameters
    rb"|\x1b\[r"                            # bare DECSTBM reset
    rb"|\x1bc"                              # RIS: full reset
    rb"|\x1b\[\?1049[hl]"                   # alt screen enter/leave
    rb"|\x1b\[[23]J"                        # clear screen / scrollback
)


class OutputGuard:
    """Output-side watcher for the handful of sequences that clobber the
    reserved row or scroll region. Returns a verdict; never alters bytes.
    A small carry window handles sequences split across read chunks; only
    matches ending beyond the carry count (earlier ones fired last feed).

    A parameterized DECSTBM is judged by its bottom edge against `rows`
    (the real terminal height — the bar row). A child that pins a region
    *above* the bar (codex's TUI guards its composer rows this way at
    startup) is not a conflict: the bar row is untouched, so the verdict is
    a repaint, and `child_owns_region` flips on so the repaint knows not to
    stomp the child's region with tandem's own. Only a region that reaches
    the bar row — or one whose bottom is left to default to the last row —
    is a real fight over the same rows and worth dropping the bar for.
    With `rows` unknown (0), every parameterized region is assumed to cover
    the bar. The pump keeps `rows` current across SIGWINCH."""

    # >= the longest watched sequence (12 bytes: ESC [ 1234 ;5678 r), so no
    # split point can hide one from both feeds it straddles
    CARRY = 15

    def __init__(self, rows: int = 0):
        self.rows = rows
        self.child_owns_region = False
        self._carry = b""

    def feed(self, data: bytes) -> str:
        buf = self._carry + data
        base = len(self._carry)
        self._carry = buf[max(0, len(buf) - self.CARRY):]
        verdict = ""
        for m in _WATCH_RE.finditer(buf):
            if m.end() <= base:
                continue
            if m.group(1) is not None:
                bottom = m.group(2)
                if bottom is not None and 0 < int(bottom) < self.rows:
                    self.child_owns_region = True
                    verdict = "reassert"
                    continue
                return "drop"
            if m.group(0) in (b"\x1b[r", b"\x1bc"):
                self.child_owns_region = False
            verdict = "reassert"
        return verdict


class StatusBar:
    """Composes the bar's paint/region/clear byte strings for the real
    bottom terminal row. The child is told the terminal is one row shorter
    (identity row mapping for everything it can address); DECSTBM keeps
    normal-buffer scrolling above the bar."""

    def __init__(self, rows: int, cols: int, active: str, others: list[str],
                 key_label: str = "^]"):
        self.rows = rows
        self.cols = cols
        self.active = active
        self.others = list(others)
        # The bar is the only place the keybind is advertised, so it has to
        # name the key actually bound — `[frame] flip_key` rebinds it, and a
        # bar still saying ^] would send the user pressing a key that goes
        # straight through to the harness.
        self.key_label = key_label

    def resize(self, rows: int, cols: int) -> None:
        self.rows = rows
        self.cols = cols

    def line(self, armed: bool, usage: str = "",
             limits: dict[str, str] | None = None) -> str:
        # padded and truncated by character count, so every glyph here must be
        # one terminal cell wide: a two-cell glyph (any with East_Asian_Width
        # W/F, e.g. ⏳ U+23F3) makes the painted row cols+1 cells and wraps off
        # the last line. ● │ ○ ◐ · are 'A' (ambiguous) — one cell outside
        # CJK-wide terminal configs.
        if armed:
            return (f" {self.active} ◐ flipping at turn end…  "
                    f"{self.key_label} cancels")[: self.cols].ljust(self.cols)

        # `usage` is the active slot's stats, most-important-first and
        # ` · `-separated (UsageSnapshot.bar_text: ctx, then ↑↓ totals);
        # `limits` is per-slot account rate-limit text for any harness.
        parts = [pt for pt in usage.split(" · ") if pt] if usage else []
        limits = limits or {}

        def compose(stats: list[str], with_limits: bool) -> str:
            def slot(name: str, glyph: str, extra: list[str]) -> str:
                lim = limits.get(name, "") if with_limits else ""
                bits = [*extra] + ([lim] if lim else [])
                return f"{name} {glyph}" + (f" {' · '.join(bits)}" if bits else "")
            slots = [slot(self.active, "●", stats)]
            slots += [slot(o, "○", []) for o in self.others]
            return f" {' │ '.join(slots)}   {self.key_label} flips"

        # Elision order when the row is too narrow: the ↑↓ totals first, the
        # rate limits second, the ctx figure third — the numbers that decide a
        # flip outlive the ones that only describe the session — and the key
        # hint last of all: it is the keybind's only advertisement, and the
        # slot names are what it cycles.
        tiers = [
            (parts, True),
            (parts[:1], True),
            (parts[:1], False),
            ([], False),
        ]
        text = compose(*tiers[-1])
        for stats, with_limits in tiers:
            candidate = compose(stats, with_limits)
            if len(candidate) <= self.cols:
                text = candidate
                break
        return text[: self.cols].ljust(self.cols)

    def paint(self, armed: bool, usage: str = "",
              limits: dict[str, str] | None = None) -> bytes:
        return (
            b"\x1b7"
            + f"\x1b[{self.rows};1H".encode()
            + b"\x1b[7m"
            + self.line(armed, usage, limits).encode()
            + b"\x1b[0m\x1b8"
        )

    def region(self) -> bytes:
        # DECSTBM homes the cursor, so save/restore around it. The bottom is
        # floored at row 1: ioctl reports rows=0 when the size is unknown and
        # 1-row panes exist, and `\x1b[1;0r` / `\x1b[1;-1r` would be ignored or
        # malformed — leaving the previous region in force under the bar.
        return b"\x1b7" + f"\x1b[1;{max(1, self.rows - 1)}r".encode() + b"\x1b8"

    def clear(self) -> bytes:
        return (
            b"\x1b7\x1b[r"
            + f"\x1b[{self.rows};1H".encode()
            + b"\x1b[2K\x1b8"
        )
