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
    assert not re.search(r"(?<!\\r)\\n", t)   # raw tty: never a bare LF


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
    s.print("漢字")                        # two cells apiece
    assert s._col == 7
    s.paint_bottom("bar", "> x", 3, focus_composer=True)
    out.text(clear=True)
    s.print("!")
    assert out.text().startswith("\x1b[21;8H!")


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
