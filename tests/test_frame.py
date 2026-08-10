"""Frame state machines: pure bytes-in/bytes-out, no PTY, no threads."""

from unicodedata import east_asian_width

from tandem.frame import FlipDetector, OutputGuard, StatusBar
from tandem.harness import get_adapter

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


def test_bar_line_shows_active_and_other():
    bar = StatusBar(rows=40, cols=60, active="claude", other="codex")
    line = bar.line(armed=False)
    assert "claude ●" in line and "codex ○" in line and "^] flips" in line
    assert len(line) == 60


def test_bar_line_armed_state():
    bar = StatusBar(rows=40, cols=60, active="claude", other="codex")
    line = bar.line(armed=True)
    assert "flipping at turn end" in line and "^] cancels" in line


def test_bar_line_truncates_to_width():
    bar = StatusBar(rows=40, cols=12, active="claude", other="codex")
    assert len(bar.line(armed=False)) == 12


def test_bar_paint_targets_real_bottom_row_and_restores_cursor():
    bar = StatusBar(rows=40, cols=60, active="claude", other="codex")
    b = bar.paint(armed=False)
    assert b.startswith(b"\x1b7")        # DECSC save cursor
    assert b"\x1b[40;1H" in b            # jump to the real bottom row
    assert b"\x1b[7m" in b               # inverse video
    assert b.endswith(b"\x1b[0m\x1b8")   # reset attrs, DECRC restore


def test_bar_region_reserves_all_but_last_row():
    bar = StatusBar(rows=40, cols=60, active="claude", other="codex")
    b = bar.region()
    assert b == b"\x1b7\x1b[1;39r\x1b8"  # DECSTBM homes the cursor: save/restore


def test_bar_clear_restores_full_region_and_wipes_row():
    bar = StatusBar(rows=40, cols=60, active="claude", other="codex")
    b = bar.clear()
    assert b"\x1b[r" in b                # full-screen scroll region back
    assert b"\x1b[40;1H" in b and b"\x1b[2K" in b


def test_bar_resize_recomputes():
    bar = StatusBar(rows=40, cols=60, active="claude", other="codex")
    bar.resize(rows=30, cols=50)
    assert b"\x1b[30;1H" in bar.paint(armed=False)
    assert len(bar.line(armed=False)) == 50


def _body(painted: bytes) -> str:
    """The line as painted: the bytes between inverse-video on and reset."""
    head, _, rest = painted.partition(b"\x1b[7m")
    body, _, tail = rest.partition(b"\x1b[0m")
    assert head and tail == b"\x1b8"
    return body.decode()


def test_bar_armed_line_also_truncates_to_width():
    # the armed text is the longer of the two, so a narrow terminal truncates
    # it where the idle one still fits: the width invariant has to hold in
    # both states or an armed repaint overruns the row
    bar = StatusBar(rows=40, cols=12, active="claude", other="codex")
    assert len(bar.line(armed=True)) == 12
    assert len(_body(bar.paint(armed=True))) == 12


def test_bar_paint_body_is_exactly_cols_wide_in_both_states():
    # what task 6 writes is paint(), not line(): the width invariant is only
    # worth anything if it survives composition
    bar = StatusBar(rows=40, cols=60, active="claude", other="codex")
    assert len(_body(bar.paint(armed=False))) == 60
    assert len(_body(bar.paint(armed=True))) == 60


def test_bar_line_has_no_double_width_glyphs():
    # line() pads and truncates by character count, so a glyph the terminal
    # draws in two cells makes the painted row one cell wider than cols and
    # wraps off the last row. Ambiguous-width glyphs (● │ ○ ◐, EAW 'A') are
    # one cell outside CJK-wide terminal configs; unconditionally wide ones
    # (EAW 'W'/'F' — e.g. ⏳ U+23F3) are never safe here.
    bar = StatusBar(rows=40, cols=60, active="claude", other="codex")
    for armed in (False, True):
        wide = [c for c in bar.line(armed) if east_asian_width(c) in "WF"]
        assert wide == [], f"armed={armed}: double-width glyphs {wide}"


def test_bar_region_floors_at_one_row():
    # ioctl reports 0×0 when the terminal size is unknown, and a 1-row pane is
    # legal: rows-1 must not emit `\x1b[1;0r` (bottom <= top, silently ignored,
    # leaving whatever region was set) or the malformed `\x1b[1;-1r`
    assert StatusBar(rows=2, cols=60, active="a", other="b").region() == b"\x1b7\x1b[1;1r\x1b8"
    assert StatusBar(rows=1, cols=60, active="a", other="b").region() == b"\x1b7\x1b[1;1r\x1b8"
    assert StatusBar(rows=0, cols=0, active="a", other="b").region() == b"\x1b7\x1b[1;1r\x1b8"


def test_bar_degenerate_width_composes_empty_row():
    # cols=0 (size unknown) must compose an empty bar, not raise
    bar = StatusBar(rows=0, cols=0, active="claude", other="codex")
    assert bar.line(armed=False) == ""
    assert bar.paint(armed=False) == b"\x1b7\x1b[0;1H\x1b[7m\x1b[0m\x1b8"


def test_bar_clear_exact_shape():
    # pinned whole: the region reset has to precede the row wipe (wiping first
    # then resetting leaves the cursor parked on the bar row), and the pair is
    # wrapped in DECSC/DECRC like every other emission
    bar = StatusBar(rows=40, cols=60, active="claude", other="codex")
    assert bar.clear() == b"\x1b7\x1b[r\x1b[40;1H\x1b[2K\x1b8"


def test_bar_region_and_clear_track_resize():
    bar = StatusBar(rows=40, cols=60, active="claude", other="codex")
    bar.resize(rows=30, cols=50)
    assert bar.region() == b"\x1b7\x1b[1;29r\x1b8"
    assert bar.clear() == b"\x1b7\x1b[r\x1b[30;1H\x1b[2K\x1b8"


def test_flush_returns_and_clears_carry():
    # a lone ESC sits in the carry until the next keypress rules the sequence
    # out — and ESC is the interrupt key in both TUIs, so the pump flushes the
    # carry on an idle tick rather than making the user press another key
    d = FlipDetector(FLIP)
    out, flips = d.feed(b"\x1b")
    assert out == b"" and flips == 0
    assert d.flush() == b"\x1b"
    assert d.flush() == b""                  # carry is cleared, not re-emitted


def test_flush_empty_when_nothing_carried():
    d = FlipDetector(FLIP)
    d.feed(b"hello")
    assert d.flush() == b""


def test_flush_preserves_paste_state():
    # paste state is not carry state: flushing a stranded fragment must not
    # reopen the flip keybind in the middle of someone's paste
    d = FlipDetector(FLIP)
    d.feed(b"\x1b[200~abc")
    d.feed(b"\x1b")
    assert d.flush() == b"\x1b"
    out, flips = d.feed(b"\x1d")
    assert out == b"\x1d" and flips == 0     # still inside the paste


def test_feed_after_flush_starts_clean():
    d = FlipDetector(FLIP)
    d.feed(b"\x1b[2")                        # possible paste marker, carried
    assert d.flush() == b"\x1b[2"
    out, flips = d.feed(b"00~\x1d")          # no longer a paste-begin
    assert out == b"00~" and flips == 1


def test_claude_quit_recipe():
    # Ctrl-C clears the composer / interrupts, Ctrl-D on the empty
    # composer exits
    assert get_adapter("claude").quit_keystrokes() == [b"\x03", b"\x04"]


def test_codex_quit_recipe():
    # Ctrl-C clears/interrupts, second Ctrl-C quits ("press again"),
    # Ctrl-D backstops older builds
    assert get_adapter("codex").quit_keystrokes() == [b"\x03", b"\x03", b"\x04"]
