"""Frame state machines: pure bytes-in/bytes-out, no PTY, no threads."""

from tandem.frame import FlipDetector

FLIP = 0x1D  # Ctrl-]


def test_flip_byte_consumed_and_counted():
    d = FlipDetector(FLIP)
    out, flips = d.feed(b"hello\x1dworld")
    assert out == b"helloworld"
    assert flips == 1


def test_plain_bytes_pass_through_untouched():
    d = FlipDetector(FLIP)
    out, flips = d.feed(b"ls -la\r")
    assert out == b"ls -la\r"
    assert flips == 0


def test_flip_byte_inside_bracketed_paste_passes_through():
    d = FlipDetector(FLIP)
    out, flips = d.feed(b"\x1b[200~abc\x1ddef\x1b[201~")
    assert out == b"\x1b[200~abc\x1ddef\x1b[201~"
    assert flips == 0


def test_flip_after_paste_end_fires():
    d = FlipDetector(FLIP)
    out, flips = d.feed(b"\x1b[200~x\x1b[201~\x1d")
    assert out == b"\x1b[200~x\x1b[201~"
    assert flips == 1


def test_paste_marker_split_across_feeds():
    d = FlipDetector(FLIP)
    out1, f1 = d.feed(b"\x1b[20")          # partial paste-begin marker
    out2, f2 = d.feed(b"0~\x1d\x1b[201~")  # completes it; 0x1D is pasted
    assert out1 + out2 == b"\x1b[200~\x1d\x1b[201~"
    assert f1 == 0 and f2 == 0


def test_paste_state_persists_across_feeds():
    d = FlipDetector(FLIP)
    d.feed(b"\x1b[200~abc")
    out, flips = d.feed(b"\x1ddef")        # still inside the paste
    assert out == b"\x1ddef"
    assert flips == 0


def test_paste_begin_split_after_one_digit():
    # \x1b[2 is a legitimate prefix of both paste markers and must be carried:
    # flushing it de-syncs paste state and eats a pasted 0x1D as a phantom flip
    d = FlipDetector(FLIP)
    out1, f1 = d.feed(b"\x1b[2")
    out2, f2 = d.feed(b"00~x\x1dy\x1b[201~")
    assert out1 + out2 == b"\x1b[200~x\x1dy\x1b[201~"
    assert f1 == 0 and f2 == 0          # pasted 0x1D relays, no phantom flip


def test_paste_end_split_after_one_digit():
    # same split inside PASTE_END: if it flushes, _in_paste latches on and the
    # flip keybind is silently dead for the rest of the session
    d = FlipDetector(FLIP)
    d.feed(b"\x1b[200~abc")
    out1, f1 = d.feed(b"\x1b[2")
    out2, f2 = d.feed(b"01~")
    out3, f3 = d.feed(b"\x1d")           # paste closed: flip must fire again
    assert out1 + out2 == b"\x1b[201~"
    assert f3 == 1
    assert out3 == b"" and f1 == 0 and f2 == 0


def test_mouse_click_on_bar_row_swallowed():
    d = FlipDetector(FLIP, bar_row=40)
    out, flips = d.feed(b"\x1b[<0;12;40M")
    assert out == b""
    assert flips == 0


def test_mouse_click_elsewhere_passes_through():
    d = FlipDetector(FLIP, bar_row=40)
    out, _ = d.feed(b"\x1b[<0;12;39M")
    assert out == b"\x1b[<0;12;39M"


def test_mouse_ignored_when_no_bar():
    d = FlipDetector(FLIP, bar_row=None)
    out, _ = d.feed(b"\x1b[<0;12;40M")
    assert out == b"\x1b[<0;12;40M"


def test_lone_esc_not_swallowed():
    # a bare ESC keypress must reach the child promptly-ish: it is carried
    # only while it could still become a tracked sequence, and flushed as
    # soon as following bytes rule that out
    d = FlipDetector(FLIP)
    out1, _ = d.feed(b"\x1b")
    out2, _ = d.feed(b"q")
    assert out1 + out2 == b"\x1bq"


def test_repeated_flips_in_one_chunk_all_counted():
    d = FlipDetector(FLIP)
    out, flips = d.feed(b"a\x1db\x1d\x1dc")
    assert out == b"abc"
    assert flips == 3


def test_wide_mouse_event_split_across_feeds_still_swallowed():
    # scroll button (64) at a 3-digit column: the fragment before the final
    # M is 12 bytes, so the carry window must span the longest trackable
    # prefix, not just the width of the narrowest mouse event
    d = FlipDetector(FLIP, bar_row=40)
    out1, _ = d.feed(b"\x1b[<64;120;40")
    out2, _ = d.feed(b"M")
    assert out1 + out2 == b""


def test_flip_after_dead_escape_fragment_still_fires():
    # \x1b[< is carried as a possible mouse event; the flip byte rules that
    # out, so the fragment flushes verbatim and the flip still counts
    d = FlipDetector(FLIP)
    out1, f1 = d.feed(b"\x1b[<")
    out2, f2 = d.feed(b"\x1d")
    assert out1 + out2 == b"\x1b[<"
    assert f1 + f2 == 1
