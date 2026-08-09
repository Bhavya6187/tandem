"""Frame state machines: pure bytes-in/bytes-out, no PTY, no threads."""

from tandem.frame import FlipDetector, OutputGuard

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


def test_guard_plain_output_no_verdict():
    assert OutputGuard().feed(b"hello \x1b[31mred\x1b[0m") == ""


def test_guard_clear_screen_triggers_reassert():
    assert OutputGuard().feed(b"\x1b[2J") == "reassert"


def test_guard_alt_screen_enter_and_leave_trigger_reassert():
    g = OutputGuard()
    assert g.feed(b"\x1b[?1049h") == "reassert"
    assert g.feed(b"\x1b[?1049l") == "reassert"


def test_guard_ris_triggers_reassert():
    assert OutputGuard().feed(b"\x1bc") == "reassert"


def test_guard_bare_region_reset_triggers_reassert():
    assert OutputGuard().feed(b"\x1b[r") == "reassert"


def test_guard_parameterized_region_triggers_drop():
    # the child drives its own scroll regions: the bar cannot coexist
    assert OutputGuard().feed(b"\x1b[5;40r") == "drop"


def test_guard_drop_wins_over_reassert():
    assert OutputGuard().feed(b"\x1b[2J\x1b[5;40r") == "drop"


def test_guard_sequence_split_across_feeds():
    g = OutputGuard()
    assert g.feed(b"text\x1b[?10") == ""
    assert g.feed(b"49h more") == "reassert"


def test_guard_does_not_double_count_carry():
    g = OutputGuard()
    assert g.feed(b"\x1b[2J") == "reassert"
    # the sequence sits inside the carry window now; a new feed with no
    # fresh trigger must not re-fire it
    assert g.feed(b"quiet") == ""


def test_guard_bare_region_reset_followed_by_digit_still_fires():
    # \x1b[r is a complete bare DECSTBM no matter what follows it, and it can
    # never be the front of a parameterized one (those put the digits *before*
    # the r). Suppressing it when a digit follows makes the verdict depend on
    # where the read boundary fell: split right after the r it fires, unsplit
    # it never does — the same child output, two different frames.
    assert OutputGuard().feed(b"\x1b[r5 lines") == "reassert"
    g = OutputGuard()
    assert g.feed(b"\x1b[r") == "reassert"
    assert g.feed(b"5 lines") == ""      # already fired; not re-fired either


def test_guard_widest_watched_sequence_split_across_feeds():
    # the longest watched sequence is 12 bytes (\x1b[1234;5678r), so the carry
    # window has to span it; a window sized to the common \x1b[5;40r would drop
    # the verdict whenever a read boundary lands near its head
    g = OutputGuard()
    assert g.feed(b"tail\x1b[1234;5678") == ""
    assert g.feed(b"r") == "drop"


def test_guard_ignores_lookalike_sequences():
    # 256-colour SGR (digits and a ; but terminates in m), a cursor position
    # report (capital R, not r) and erase-below (no parameter) are all routine
    # output that must not cost the bar
    assert OutputGuard().feed(b"\x1b[38;5;196mhi\x1b[0m\x1b[24;80R\x1b[J") == ""


def test_guard_refires_once_the_carry_window_has_scrolled_past():
    # suppression is "already reported", not "seen once": a second clear must
    # still be reported
    g = OutputGuard()
    assert g.feed(b"\x1b[2J") == "reassert"
    assert g.feed(b"z" * 20) == ""
    assert g.feed(b"\x1b[2J") == "reassert"


def test_guard_empty_feed_is_inert():
    g = OutputGuard()
    assert g.feed(b"\x1b[2J") == "reassert"
    assert g.feed(b"") == ""
