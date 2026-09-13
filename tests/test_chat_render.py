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
    s.paint_bottom("bar", "> hi", 4, focus_composer=True)
    out.text(clear=True)
    s.print(" world")
    assert out.text().startswith("\x1b[21;6H world")
    assert s._col == 11
    s.print("\n"); assert s._col == 0


def test_exactly_full_line_breaks_before_the_next_chunk(screen):
    s, out = screen
    s.enter(); s.print("x" * 40)
    assert s._col == 0 and out.text().endswith("x" * 40 + "\r\n")


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
    assert "\r\n  completed · 1000↑ 200↓\r\n" in t
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
    s.paint_bottom("bar", "> x", 3, focus_composer=True)
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

    def test_cells_still_counts_the_renderers_own_sgr_as_zero(self, screen):
        s, out = screen
        s.enter()
        s.print(s._dim("x" * 5))
        assert s._col == 5


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
    s.paint_bottom("claude ● │ codex ○", "> hello", 7, focus_composer=True)
    t = out.text()
    assert "\x1b[22;1H" in t and "─" * 40 in t          # separator row
    assert "\x1b[23;1H\x1b[7m" in t and "claude ● │ codex ○" in t   # bar, reverse video
    assert "\x1b[24;1H\x1b[2K> hello" in t               # composer row
    assert t.endswith("\x1b[24;8H")                       # cursor after the text
    out.text(clear=True)
    s.paint_bottom("bar", "> x", 3, focus_composer=False)
    assert out.text().endswith("\x1b8")                   # cursor restored to the region


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
