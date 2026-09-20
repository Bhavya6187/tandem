import re

import pytest

from tandem.chat.events import (ApprovalRequest, Failure, QuestionRequest, TextDelta, ThinkingDelta,
                                ToolFinished, ToolOutput, ToolStarted, TurnFinished, TurnStarted)
from tandem.chat.render import Screen
from tandem.config import ChatConfig
from tandem.events import AssistantMessage, ToolCall, ToolResult, UserMessage


class Out:
    def __init__(self):
        self.chunks = []

    def __call__(self, b: bytes):
        self.chunks.append(b)

    def text(self, clear=False):
        s = b"".join(self.chunks).decode()
        if clear:
            self.chunks.clear()
        return s


@pytest.fixture
def screen():
    out = Out()
    return Screen(out, rows=24, cols=40, cfg=ChatConfig(tool_output_lines=2), color=False), out


def test_enter_sets_the_region_and_bracketed_paste(screen):
    s, out = screen
    s.enter()
    t = out.text()
    assert "\x1b[?2004h" in t and "\x1b[1;21r" in t and "\x1b[21;1H" in t
    assert s.region_rows == 21


def test_a_fresh_enter_scrolls_the_old_screen_into_scrollback(screen):
    """The window opens on whatever the terminal was showing — a previous
    session, the shell. Left in place, the region's first rows overwrite it
    cell by cell. One LF per row from the bottom row, before the region is
    set, pushes it all into scrollback rather than erasing it."""
    s, out = screen
    s.enter(fresh=True)
    t = out.text()
    push = f"\x1b[{s.rows};1H" + "\r\n" * s.rows
    assert push in t and "\x1b[2J" not in t and "\x1b[3J" not in t
    assert t.index(push) < t.index("\x1b[1;21r")


def test_a_repaint_enter_keeps_the_conversation_on_screen(screen):
    s, out = screen
    s.enter()
    assert "\r\n" not in out.text()


def test_leave_resets_everything(screen):
    s, out = screen
    s.enter(); out.text(clear=True); s.leave()
    t = out.text()
    assert "\x1b[r" in t and "\x1b[?2004l" in t
    assert t.endswith("\r\n") and not re.search(r"(?<!\r)\n", t)


def test_print_tracks_the_column_and_returns_to_the_region_after_the_bottom_paint(screen):
    s, out = screen
    s.enter(); s.print("hello")
    assert s._col == 5
    s.paint_bottom("bar", ["> hi"], 0, 4, focus_composer=True)
    out.text(clear=True)
    s.print(" world")
    assert out.text().startswith("\x1b[21;6H world")
    assert s._col == 11
    s.print("\n"); assert s._col == 0


def test_exactly_full_line_breaks_before_the_next_chunk(screen):
    s, out = screen
    s.enter(); s.print("x" * 40)
    assert s._col == 0 and out.text().endswith("x" * 40 + "\r\n")


def test_a_chunk_wider_than_the_row_wraps_without_an_extra_break():
    """The terminal wraps a long delta on its own; the renderer follows the
    cursor onto the new row instead of breaking the paragraph a second time."""
    out = Out()
    s = Screen(out, 24, 10, ChatConfig(), color=False)
    s.enter(); out.text(clear=True)
    s.print("abcdefghijk")
    assert s._col == 1 and out.text() == "abcdefghijk"
    s.print("l")
    assert s._col == 2 and out.text() == "abcdefghijkl"
    s.print("x" * 18)                          # lands exactly on the edge, two rows down
    assert s._col == 0 and out.text().endswith("x" * 18 + "\r\n")


def test_tool_output_lines_are_counted_at_their_newline(screen):
    """Command output streams in arbitrary chunks; a line is a line when its
    newline arrives, not once per chunk — the cap counts lines the command
    printed, and an unterminated last line is still shown at the end."""
    s, out = screen
    s.enter(); out.text(clear=True)
    s.tool_started(ToolStarted("c1", "exec", "make"))
    for chunk in ("he", "ll", "o\nwor", "ld\ntail"):
        s.tool_output(ToolOutput("c1", chunk))
    s.tool_finished(ToolFinished("c1", True, "exit 0"))
    t = out.text()
    assert "    hello\r\n    world\r\n    … +1 lines\r\n    ok · exit 0\r\n" in t
    assert "    he\r\n" not in t and "tail" not in t


def test_turn_and_tool_rows(screen):
    s, out = screen
    s.enter(); out.text(clear=True)
    s.turn_started(TurnStarted("codex", "gpt-5.5", "review it"))
    s.text_delta(TextDelta("Looks "))
    s.text_delta(TextDelta("fine."))
    s.tool_started(ToolStarted("c1", "exec", "pytest -q"))
    s.tool_output(ToolOutput("c1", "l1\nl2\nl3\nl4\n"))
    s.tool_finished(ToolFinished("c1", True, "exit 0"))
    s.turn_finished(TurnFinished("completed", "1000↑ 200↓"))
    t = out.text()
    assert "you → codex · gpt-5.5  review it\r\n" in t
    assert "codex\r\nLooks fine." in t
    assert "  ▸ exec pytest -q\r\n    l1\r\n    l2\r\n" in t
    assert "l3" not in t
    assert "    … +2 lines\r\n    ok · exit 0\r\n" in t
    assert "\r\n  ✓ done · 1000↑ 200↓\r\n" in t
    assert not re.search(r"(?<!\r)\n", t)   # raw tty: never a bare LF


def test_failed_tool_flushes_the_held_output_past_the_cap(screen):
    s, out = screen
    s.enter(); out.text(clear=True)
    s.tool_started(ToolStarted("c1", "exec", "pytest -q"))
    s.tool_output(ToolOutput("c1", "l1\nl2\nl3\nl4\nl5\n"))
    s.tool_finished(ToolFinished("c1", False, "exit 1"))
    t = out.text()
    assert "    l1\r\n    l2\r\n    l3\r\n    l4\r\n    l5\r\n    error · exit 1\r\n" in t
    assert "lines" not in t


def test_successful_tool_only_counts_what_it_dropped(screen):
    s, out = screen
    s.enter(); out.text(clear=True)
    s.tool_started(ToolStarted("c1", "exec", "pytest -q"))
    s.tool_output(ToolOutput("c1", "l1\nl2\nl3\nl4\nl5\n"))
    s.tool_finished(ToolFinished("c1", True, "exit 0"))
    t = out.text()
    assert "    l1\r\n    l2\r\n    … +3 lines\r\n    ok · exit 0\r\n" in t
    assert "l3" not in t


def test_a_failed_tool_counts_output_beyond_the_held_buffer(screen):
    s, out = screen
    s.enter(); out.text(clear=True)
    s.tool_started(ToolStarted("c1", "exec", "pytest -q"))
    s.tool_output(ToolOutput("c1", "\n".join(f"l{i}" for i in range(1, 210))))
    s.tool_finished(ToolFinished("c1", False, "exit 1"))
    t = out.text()
    assert "    l202\r\n" in t          # the last line the buffer held
    assert "l203" not in t
    assert "    … +7 lines\r\n    error · exit 1\r\n" in t


def test_col_counts_cells_not_escape_bytes(screen):
    s, out = screen
    s.enter()
    s.print("\x1b[2mhmm\x1b[0m")          # 3 cells, not 11 characters
    assert s._col == 3
    s.print("\x1b[38:2::1:2:3mrgb\x1b[0m")  # colon-delimited SGR, also 3 cells
    assert s._col == 6
    s.print("漢字")                        # two cells apiece
    assert s._col == 10
    s.paint_bottom("bar", ["> x"], 0, 3, focus_composer=True)
    out.text(clear=True)
    s.print("!")
    assert out.text().startswith("\x1b[21;11H!")


def test_styled_row_does_not_break_early():
    out = Out()
    s = Screen(out, 24, 40, ChatConfig(), color=True)
    s.enter()
    s.print(s._dim("x" * 39))
    assert s._col == 39


def test_speaker_label_is_printed_once_per_turn(screen):
    s, out = screen
    s.enter(); out.text(clear=True)
    s.turn_started(TurnStarted("claude", "", "hi"))
    s.text_delta(TextDelta("a")); s.text_delta(TextDelta("b"))
    s.tool_started(ToolStarted("c", "Bash", "ls"))
    assert out.text().count("\r\nclaude\r\n") == 1


def test_thinking_hidden_unless_configured():
    out = Out()
    Screen(out, 24, 40, ChatConfig(show_thinking=False), color=False).thinking_delta(ThinkingDelta("hmm"))
    assert "hmm" not in out.text()
    out2 = Out()
    Screen(out2, 24, 40, ChatConfig(show_thinking=True), color=False).thinking_delta(ThinkingDelta("hmm"))
    assert "hmm" in out2.text()


def test_prompts_and_failures(screen):
    s, out = screen
    s.enter(); out.text(clear=True)
    s.approval(ApprovalRequest("command", "rm -rf build"))
    s.question(QuestionRequest("Which color?", ("red", "blue")))
    s.failure(Failure("codex thread is open in another process"))
    t = out.text()
    assert "  ▸ Allow command: rm -rf build   [y]es [a]lways [n]o\r\n" in t
    assert "  ? Which color?\r\n    1. red\r\n    2. blue\r\n" in t
    assert "error: codex thread is open in another process\r\n" in t


class TestHarnessTextIsSanitized:
    """Every string a harness controls — deltas, tool names and output, an
    approval detail, a replayed transcript — is printed with ESC and the other
    C0 controls stripped. Left in, a command string or a model delta carrying
    CSI can erase and rewrite the approval row, escape the scroll region, or
    switch to the alternate screen."""

    def test_a_text_delta_cannot_erase_the_line(self, screen):
        s, out = screen
        s.enter(); out.text(clear=True)
        s.text_delta(TextDelta("a\x1b[2Kb"))
        t = out.text()
        assert "\x1b[2K" not in t and "\x1b" not in t and "ab" in t

    def test_an_approval_detail_cannot_move_the_cursor(self, screen):
        s, out = screen
        s.enter(); out.text(clear=True)
        s.approval(ApprovalRequest("command", "echo safe\x1b[2K\x1b[1G  rm -rf ~/"))
        t = out.text()
        assert "\x1b[1G" not in t and "\x1b[2K" not in t
        assert "  ▸ Allow command: echo safe  rm -rf ~/   [y]es [a]lways [n]o\r\n" in t

    def test_tool_rows_and_output_are_stripped(self, screen):
        s, out = screen
        s.enter(); out.text(clear=True)
        s.tool_started(ToolStarted("c1", "ex\x1b[?1049hec", "ls\x1b[1;1Hhijack"))
        s.tool_output(ToolOutput("c1", "out\x1b[2Kput"))
        s.tool_finished(ToolFinished("c1", True, "exit\x1b[J 0"))
        t = out.text()
        assert "\x1b" not in t
        assert "  ▸ exec lshijack\r\n    output\r\n    ok · exit 0\r\n" in t

    def test_a_thinking_delta_and_a_failure_are_stripped(self, screen):
        out = Out()
        s = Screen(out, 24, 40, ChatConfig(show_thinking=True), color=False)
        s.enter(); out.text(clear=True)
        s.thinking_delta(ThinkingDelta("hm\x1b[1;1Hm"))
        s.failure(Failure("boom\x1b[?1049h"))
        t = out.text()
        assert "\x1b" not in t and "hmm" in t and "boom" in t

    def test_the_prompt_and_replayed_history_are_stripped(self, screen):
        s, out = screen
        s.enter(); out.text(clear=True)
        s.turn_started(TurnStarted("codex", "", "go\x1b[2Khome"))
        s.history([
            UserMessage(source="claude", text="fix\x1b[1;1H it"),
            AssistantMessage(source="claude", text="Done\x1b[J."),
            ToolCall(source="claude", call_id="1", tool="Ba\x1bsh",
                     arguments={"command": "pytest\x1b[2K"}),
            ToolResult(source="claude", call_id="1", output="12\x1b[J passed"),
        ], source="claude")
        t = out.text()
        assert "\x1b" not in t
        assert "gohome" in t and "fix it" in t and "Done." in t and "12 passed" in t

    def test_cells_still_counts_the_renderers_own_sgr_as_zero(self):
        out = Out()
        s = Screen(out, rows=24, cols=40, cfg=ChatConfig(), color=True)
        s.enter()
        s.print(s._dim("x" * 5))                  # a real SGR wrapper, 5 cells
        assert "\x1b[2m" in out.text() and s._col == 5


def test_the_approval_row_offers_only_the_available_choices(screen):
    s, out = screen
    s.enter(); out.text(clear=True)
    s.approval(ApprovalRequest("command", "rm x", choices=("allow", "deny")))
    t = out.text()
    assert "  ▸ Allow command: rm x   [y]es [n]o\r\n" in t
    assert "[a]lways" not in t


def test_leave_clears_the_bottom_rows(screen):
    """The bar and composer are painted rows like any other: left behind, they
    scroll into the terminal's scrollback when the shell prompt returns."""
    s, out = screen
    s.enter(); out.text(clear=True); s.leave()
    t = out.text()
    assert f"\x1b[{s.rows - 2};1H" in t and "\x1b[J" in t
    assert t.index("\x1b[J") > t.index(f"\x1b[{s.rows - 2};1H")


def test_bottom_block_rows_and_cursor(screen):
    s, out = screen
    s.enter(); out.text(clear=True)
    s.paint_bottom("claude ● │ codex ○", ["> hello"], 0, 7, focus_composer=True)
    t = out.text()
    assert "\x1b[22;1H" in t and "─" * 40 in t          # separator row
    assert "\x1b[23;1H\x1b[7m" in t and "claude ● │ codex ○" in t   # bar, reverse video
    assert "\x1b[24;1H\x1b[2K> hello" in t               # composer row
    assert t.endswith("\x1b[24;8H")                       # cursor after the text
    out.text(clear=True)
    s.paint_bottom("bar", ["> x"], 0, 3, focus_composer=False)
    assert out.text().endswith("\x1b8")                   # cursor restored to the region


def test_a_taller_composer_pushes_the_conversation_up_before_taking_its_rows(screen):
    """The rows a growing composer takes hold conversation. Scrolled out of
    the old region first, they land in scrollback; painted over, they are
    gone."""
    s, out = screen
    s.enter(); s.print("streaming"); out.text(clear=True)
    s.paint_bottom("bar", ["> one", "  two", "  three"], 2, 7, focus_composer=True)
    t = out.text()
    assert s.region_rows == 19
    push, region = "\x1b[21;1H\n\n", "\x1b[1;19r"
    assert push in t and t.index(push) < t.index(region) < t.index("\x1b[20;1H")
    assert "\x1b[20;1H" + "─" * 40 in t and "\x1b[21;1H\x1b[7mbar" in t
    assert ("\x1b[22;1H\x1b[2K> one" in t and "\x1b[23;1H\x1b[2K  two" in t
            and "\x1b[24;1H\x1b[2K  three" in t)
    assert t.endswith("\x1b[24;8H")
    out.text(clear=True)
    s.print("!")                                           # the line it was on moved up with the rest
    assert out.text().startswith("\x1b[19;10H!")


def test_a_shorter_composer_hands_its_rows_back_and_output_continues_in_place(screen):
    """After a submit the block shrinks. The freed rows are erased and rejoin
    the region; the conversation carries on right under its last line rather
    than jumping to the new bottom row and leaving a gap."""
    s, out = screen
    s.enter()
    s.paint_bottom("bar", ["> one", "  two", "  three"], 2, 7, focus_composer=True)
    out.text(clear=True)
    s.paint_bottom("bar", ["> "], 0, 2, focus_composer=True)
    t = out.text()
    assert s.region_rows == 21 and "\x1b[1;21r" in t
    assert "\x1b[20;1H\x1b[2K" in t and "\x1b[21;1H\x1b[2K" in t
    assert "\x1b[22;1H" + "─" * 40 in t and "\x1b[24;1H\x1b[2K> " in t
    out.text(clear=True)
    s.print("a\nb\nc\nd")
    assert out.text().startswith("\x1b[19;1Ha")
    s.paint_bottom("bar", ["> "], 0, 2, focus_composer=True); out.text(clear=True)
    s.print("!")
    assert out.text().startswith("\x1b[21;2H!")           # 19, 20, 21, and the region scrolls from there


def test_growing_into_rows_the_conversation_has_not_reached_scrolls_nothing(screen):
    s, out = screen
    s.enter()
    s.paint_bottom("bar", ["> 1", "  2", "  3"], 2, 3, focus_composer=True)
    s.paint_bottom("bar", ["> "], 0, 2, focus_composer=True)      # the conversation stays on row 19
    out.text(clear=True)
    s.paint_bottom("bar", ["> 1", "  2"], 1, 3, focus_composer=True)
    t = out.text()
    assert "\n" not in t and "\x1b[1;20r" in t
    out.text(clear=True)
    s.print("!")
    assert out.text().startswith("\x1b[19;1H!")


def test_the_row_follows_a_chunk_the_terminal_wrapped():
    out = Out()
    s = Screen(out, 24, 10, ChatConfig(), color=False)
    s.enter()
    s.paint_bottom("bar", ["> 1", "  2", "  3"], 2, 3, focus_composer=True)
    s.paint_bottom("bar", ["> "], 0, 2, focus_composer=True)      # row 19 of 21
    s.print("x" * 25)                                             # wraps onto 20, then 21
    s.paint_bottom("bar", ["> "], 0, 2, focus_composer=True); out.text(clear=True)
    s.print("!")
    assert out.text().startswith("\x1b[21;6H!")


def test_an_unfocused_paint_that_moved_the_region_puts_the_cursor_back_itself(screen):
    """ESC 8 restores where the cursor was; the push moved the line it was on."""
    s, out = screen
    s.enter(); s.print("hi"); out.text(clear=True)
    s.paint_bottom("bar", ["> 1", "  2"], 1, 3, focus_composer=False)
    assert out.text().endswith("\x1b[20;3H")


def test_leave_erases_a_tall_block_from_its_top(screen):
    s, out = screen
    s.enter()
    s.paint_bottom("bar", ["> 1", "  2", "  3"], 2, 3, focus_composer=True)
    out.text(clear=True); s.leave()
    assert "\x1b[20;1H\x1b[J" in out.text()


def test_resize_keeps_a_row_for_the_conversation(screen):
    s, out = screen
    s.enter()
    s.paint_bottom("bar", ["> "] + ["  x"] * 7, 7, 3, focus_composer=True)
    assert s.region_rows == 14
    s.resize(6, 40)
    assert s.region_rows >= 1 and s.composer_max_rows == 2


def test_composer_max_rows_is_a_third_of_the_screen_up_to_eight():
    cfg = ChatConfig()
    assert [Screen(Out(), rows, 40, cfg).composer_max_rows for rows in (4, 12, 24, 60)] == [1, 4, 8, 8]


def test_resize_reissues_the_region(screen):
    s, out = screen
    s.enter(); out.text(clear=True); s.resize(30, 100)
    assert "\x1b[1;27r" in out.text() and s.region_rows == 27 and s.cols == 100


def test_color_off_emits_no_sgr_bold():
    out = Out()
    s = Screen(out, 24, 40, ChatConfig(), color=False)
    s.turn_started(TurnStarted("codex", "", "x"))
    assert "\x1b[1m" not in out.text()
    out2 = Out()
    Screen(out2, 24, 40, ChatConfig(), color=True).turn_started(TurnStarted("codex", "", "x"))
    assert "\x1b[1m" in out2.text()


def test_history_paints_normalized_events(screen):
    s, out = screen
    s.enter(); out.text(clear=True)
    s.history([
        UserMessage(source="claude", text="fix it"),
        AssistantMessage(source="claude", text="Done."),
        ToolCall(source="claude", call_id="1", tool="Bash", arguments={"command": "pytest"}),
        ToolResult(source="claude", call_id="1", output="12 passed\nmore"),
    ], source="claude")
    t = out.text()
    assert "you → claude  fix it\r\n" in t and "claude\r\nDone.\r\n" in t
    assert "  ▸ Bash pytest\r\n    12 passed\r\n" in t


# -- the activity line and the closing row -------------------------------------


def _separator(text: str, row: int = 22) -> str:
    """What was painted on the separator row, up to the next cursor move."""
    start = text.index(f"\x1b[{row};1H") + len(f"\x1b[{row};1H")
    return text[start:text.index("\x1b[", start)]


def test_the_separator_carries_the_activity_at_full_width(screen):
    s, out = screen
    s.enter(); out.text(clear=True)
    s.paint_bottom("bar", ["> "], 0, 2, focus_composer=True, activity="⠋ claude · thinking · 3s")
    row = _separator(out.text())
    assert row == "── ⠋ claude · thinking · 3s " + "─" * 12
    assert len(row) == 40


def test_an_activity_wider_than_the_row_is_cut_not_wrapped(screen):
    """One cell past the edge wraps onto the bar's row and the bar's paint
    then lands a row low."""
    s, out = screen
    s.enter(); out.text(clear=True)
    s.paint_bottom("bar", ["> "], 0, 2, focus_composer=True, activity="x" * 80)
    assert len(_separator(out.text())) == 40


def test_an_idle_separator_is_the_plain_rule(screen):
    s, out = screen
    s.enter(); out.text(clear=True)
    s.paint_bottom("bar", ["> "], 0, 2, focus_composer=True, activity="⠋ claude · writing · 1s")
    out.text(clear=True)
    s.paint_bottom("bar", ["> "], 0, 2, focus_composer=True)
    assert _separator(out.text()) == "─" * 40


def test_a_waiting_activity_is_bold_not_dim():
    out = Out()
    s = Screen(out, rows=24, cols=40, cfg=ChatConfig(), color=True)
    s.enter(); out.text(clear=True)
    s.paint_bottom("bar", ["> "], 0, 2, focus_composer=True,
                   activity="● claude is waiting for your answer", urgent=True)
    t = out.text()
    assert "\x1b[22;1H\x1b[1m── ● claude is waiting" in t
    out.text(clear=True)
    s.paint_bottom("bar", ["> "], 0, 2, focus_composer=True, activity="⠋ claude · writing · 1s")
    assert "\x1b[22;1H\x1b[2m── ⠋ claude" in out.text()


@pytest.mark.parametrize("event, elapsed, want", [
    (TurnFinished("completed", ""), 42.0, "  ✓ done · 42s"),
    (TurnFinished("completed", "1000↑ 200↓"), 42.0, "  ✓ done · 42s · 1000↑ 200↓"),
    (TurnFinished("interrupted", ""), 8.2, "  ■ interrupted · 8s"),
    (TurnFinished("failed", ""), 3.0, "  ✗ failed · 3s"),
    (TurnFinished("completed", ""), None, "  ✓ done"),
])
def test_every_turn_ends_with_a_closing_row(screen, event, elapsed, want):
    """A completed turn with no usage used to end on nothing at all: a
    finished answer and a model still thinking looked the same."""
    s, out = screen
    s.enter(); out.text(clear=True)
    s.turn_started(TurnStarted("claude", "", "go")); s.text_delta(TextDelta("ok"))
    out.text(clear=True)
    s.turn_finished(event, elapsed)
    assert out.text() == "\r\n" + want + "\r\n"


def test_the_activity_cannot_drive_the_terminal(screen):
    """The phase names the running tool, and a tool name is the harness's
    string like any other."""
    s, out = screen
    s.enter(); out.text(clear=True)
    s.paint_bottom("bar", ["> "], 0, 2, focus_composer=True, activity="⠋ codex · running \x1b[2Jx")
    assert "\x1b[2J" not in out.text() and "running x" in out.text()


def test_a_wide_glyph_in_the_activity_counts_two_cells(screen):
    s, out = screen
    s.enter(); out.text(clear=True)
    s.paint_bottom("bar", ["> "], 0, 2, focus_composer=True, activity="漢" * 30)
    row = _separator(out.text())
    assert row == "── " + "漢" * 18 + " "
