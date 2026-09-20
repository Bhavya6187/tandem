import pytest

from tandem.chat.composer import Answer, Cancel, Composer, CtrlC, Interrupt, Repaint, Submit
from tandem.chat.events import ApprovalRequest, QuestionRequest


def feed(c, s):
    return c.feed(s if isinstance(s, bytes) else s.encode())


def test_typing_and_submit():
    c = Composer()
    assert feed(c, "hello") == []
    assert c.text == "hello" and c.cur == 5
    assert feed(c, "\r") == [Submit("hello")]
    assert c.text == "" and c.history == ["hello"]


def test_empty_enter_is_a_noop():
    c = Composer()
    assert feed(c, "   \r") == [] and c.text == "   "


def test_editing_keys():
    c = Composer()
    feed(c, "abd")
    feed(c, b"\x1b[D")            # left
    feed(c, "c")
    assert c.text == "abcd" and c.cur == 3
    feed(c, b"\x1b[H"); feed(c, "X")              # home
    feed(c, b"\x1b[F"); feed(c, "Y")              # end
    assert c.text == "XabcdY"
    feed(c, b"\x7f")                              # backspace
    assert c.text == "Xabcd"
    feed(c, b"\x01"); feed(c, b"\x1b[3~")         # ctrl-a, delete
    assert c.text == "abcd"
    feed(c, b"\x05"); feed(c, b"\x15")            # ctrl-e, ctrl-u kills to start
    assert c.text == ""


def test_meta_word_motion():
    c = Composer()
    feed(c, "one two  three")
    assert feed(c, b"\x1bb") == [] and c.cur == 9     # option-left: Esc b, no Interrupt
    assert feed(c, b"\x1bb") == [] and c.cur == 4
    assert feed(c, b"\x1bf") == [] and c.cur == 7     # option-right: Esc f
    assert feed(c, b"\x1bf") == [] and c.cur == 14
    assert feed(c, b"\x1bf") == [] and c.cur == 14    # end stays
    assert c.text == "one two  three"                 # no stray b / f typed
    feed(c, b"\x1b[1;3D"); assert c.cur == 9          # modified arrows move by word too
    feed(c, b"\x1b[1;5D"); assert c.cur == 4
    feed(c, b"\x1b[1;3C"); assert c.cur == 7


def test_meta_word_delete():
    c = Composer()
    feed(c, "one two three")
    assert feed(c, b"\x1b\x7f") == [] and c.text == "one two "    # option-backspace
    feed(c, b"\x01")
    assert feed(c, b"\x1bd") == [] and c.text == " two "          # Esc d


def test_history_with_draft():
    c = Composer()
    feed(c, "one\r"); feed(c, "two\r"); feed(c, "dra")
    feed(c, b"\x1b[A"); assert c.text == "two"
    feed(c, b"\x1b[A"); assert c.text == "one"
    feed(c, b"\x1b[A"); assert c.text == "one"    # top stays
    feed(c, b"\x1b[B"); feed(c, b"\x1b[B"); assert c.text == "dra"   # back to the draft


def test_control_keys():
    c = Composer()
    assert feed(c, b"\x03") == [CtrlC()]
    assert feed(c, b"\x0c") == [Repaint()]
    assert feed(c, b"\x1b") == [Interrupt()]      # lone Esc in prompt mode


def test_escape_sequence_split_across_reads():
    c = Composer()
    feed(c, "ab")
    assert feed(c, b"\x1b[") == []
    assert feed(c, b"D") == [] and c.cur == 1


def test_bracketed_paste_keeps_newlines():
    c = Composer()
    feed(c, b"\x1b[200~line one\nline two\x1b[201~")
    assert c.text == "line one\nline two"
    assert c.rows(40, 8) == (["> line one", "  line two"], 1, 10)
    assert feed(c, "\r") == [Submit("line one\nline two")]


def test_paste_newlines_are_normalized():
    """Terminals paste a line break as CR (iTerm2, Terminal.app) or CRLF; left
    raw it is a control character in the draft, not a row."""
    c = Composer()
    feed(c, b"\x1b[200~a\rb\r\nc\r")
    feed(c, b"\nd\x1b[201~")                        # a CRLF split across two reads is one break
    assert c.text == "a\nb\nc\nd"


def test_paste_end_split_across_reads():
    c = Composer()
    feed(c, b"\x1b[200~pasted\x1b[2")
    assert c.text == "pasted"                     # the partial end bracket is held back
    feed(c, b"01~!")
    assert c.text == "pasted!"


def test_utf8_split_across_reads():
    c = Composer()
    feed(c, "é".encode()[:1]); feed(c, "é".encode()[1:])
    assert c.text == "é"


@pytest.mark.parametrize("key", [b"\x1b\r",          # option-enter
                                 b"\n",              # ctrl-j
                                 b"\\\r",            # backslash, then enter
                                 b"\x1b[13;2u",      # shift-enter, kitty / CSI-u
                                 b"\x1b[27;2;13~"])  # shift-enter, modifyOtherKeys
def test_newline_keys_insert_a_line_break_instead_of_submitting(key):
    c = Composer()
    feed(c, "one")
    assert feed(c, key) == []
    feed(c, "two")
    assert c.text == "one\ntwo"
    assert feed(c, "\r") == [Submit("one\ntwo")]


def test_a_backslash_the_cursor_is_not_behind_still_submits():
    c = Composer()
    feed(c, "a\\b"); feed(c, b"\x1b[D" * 2)           # a|\b
    assert feed(c, "\r") == [Submit("a\\b")]


def test_newline_keys_answer_nothing_in_approval_mode():
    c = Composer()
    c.begin_approval(ApprovalRequest("command", "rm x"))
    assert feed(c, b"\n") == [] and feed(c, b"\x1b\r") == []
    assert c.text == "" and c.mode == "approval"


def test_up_and_down_move_between_lines_and_keep_the_column():
    c = Composer()
    feed(c, b"abcdef\nxy\n123456")
    feed(c, b"\x1b[D" * 2)                          # 1234|56
    feed(c, b"\x1b[A"); feed(c, "!")                # a shorter line clamps to its end
    assert c.text == "abcdef\nxy!\n123456"
    feed(c, b"\x1b[A"); feed(c, "^")
    assert c.text == "abc^def\nxy!\n123456"
    feed(c, b"\x1b[B"); feed(c, b"\x1b[B"); feed(c, "_")
    assert c.text == "abc^def\nxy!\n1234_56"


def test_up_and_down_follow_wrapped_rows_once_the_draft_is_laid_out():
    c = Composer()
    feed(c, "abcdefghij")
    assert c.rows(8, 8) == (["> abcdef", "  ghij"], 1, 6)
    feed(c, b"\x1b[A"); feed(c, "^")                # same line, the row above
    assert c.text == "abcd^efghij"


def test_history_is_reached_from_the_first_and_last_line_only():
    c = Composer()
    feed(c, "old\r"); feed(c, b"one\ntwo")
    feed(c, b"\x1b[A"); assert c.text == "one\ntwo"     # up from line two: line one
    feed(c, b"\x1b[A"); assert c.text == "old"          # up from line one: history
    feed(c, b"\x1b[B"); assert c.text == "one\ntwo"     # and back to the draft
    feed(c, b"\x1b[B"); assert c.text == "one\ntwo"     # no newer entry: stays


def test_line_keys_act_on_the_current_line():
    c = Composer()
    feed(c, b"one\ntwo three\nfour")
    feed(c, b"\x1b[A")                               # two |three… column 4
    feed(c, b"\x01"); feed(c, "<")                   # ctrl-a
    feed(c, b"\x05"); feed(c, ">")                   # ctrl-e
    assert c.text == "one\n<two three>\nfour"
    feed(c, b"\x1b[H"); feed(c, "[")                 # home
    feed(c, b"\x1b[F"); feed(c, "]")                 # end
    assert c.text == "one\n[<two three>]\nfour"
    feed(c, b"\x1b[D" * 7); feed(c, b"\x0b")        # ctrl-k kills to the end of the line
    assert c.text == "one\n[<two \nfour"
    feed(c, b"\x15")                                 # ctrl-u kills to its start
    assert c.text == "one\n\nfour"


def test_kill_at_a_line_edge_joins_the_lines():
    c = Composer()
    feed(c, b"one\ntwo")
    feed(c, b"\x01"); feed(c, b"\x15")              # ctrl-u at the start of line two
    assert c.text == "onetwo" and c.cur == 3
    feed(c, b"\x15"); feed(c, b"\x15")
    assert c.text == "two"
    c = Composer()
    feed(c, b"one\ntwo"); feed(c, b"\x1b[A"); feed(c, b"\x0b")   # ctrl-k at the end of line one
    assert c.text == "onetwo" and c.cur == 3


def test_rows_wrap_a_long_line_under_the_prompt():
    c = Composer()
    feed(c, "x" * 40)
    rows, r, col = c.rows(20, 8)
    assert rows == ["> " + "x" * 18, "  " + "x" * 18, "  " + "x" * 4] and (r, col) == (2, 6)
    feed(c, b"\x01")
    assert c.rows(20, 8)[1:] == (0, 2)


def test_the_cursor_gets_a_row_of_its_own_past_a_full_one():
    c = Composer()
    feed(c, "x" * 18)
    assert c.rows(20, 8) == (["> " + "x" * 18, "  "], 1, 2)


def test_rows_count_wide_glyphs_as_two_cells():
    c = Composer()
    feed(c, "日本語日本")                                # 10 cells into 8
    assert c.rows(10, 8) == (["> 日本語日", "  本"], 1, 4)


def test_rows_scroll_to_keep_the_cursor_in_view():
    c = Composer()
    feed(c, b"\n".join(str(n).encode() for n in range(10)))
    assert c.rows(20, 3) == (["  7", "  8", "  9"], 2, 3)
    feed(c, b"\x1b[A" * 2)                           # inside the view: it holds still
    assert c.rows(20, 3) == (["  7", "  8", "  9"], 0, 3)
    feed(c, b"\x1b[A")                               # above it: one row up
    assert c.rows(20, 3) == (["  6", "  7", "  8"], 0, 3)
    feed(c, b"\x1b[A" * 6)
    assert c.rows(20, 3) == (["> 0", "  1", "  2"], 0, 3)


def test_rows_show_a_pasted_tab_as_one_cell():
    c = Composer()
    feed(c, b"\x1b[200~a\tb\x1b[201~")
    assert c.text == "a\tb" and c.rows(20, 8) == (["> a b"], 0, 5)


def test_approval_mode_keys():
    c = Composer()
    c.begin_approval(ApprovalRequest("command", "rm x"))
    assert c.rows(60, 8)[0][0].startswith(" [y]es [a]lways [n]o")
    assert feed(c, "q") == []                     # not a choice
    assert feed(c, "A") == [Answer("always")]
    assert feed(c, b"\x1b") == [Cancel()]
    assert feed(c, "\r") == []                    # Enter does not pick a default
    c.end_answer()
    assert c.mode == "prompt"


def test_approval_answers_only_on_the_first_character_of_a_read():
    """The window drains live events — painting the approval row — before it
    reads stdin, so anything with characters glued to it was typed while the
    model was working. Scanning such a chunk for a y/a/n would answer a request
    the user has not seen: "and then fix the tests" = always. Only a lone
    keypress at the head of the read is an answer."""
    c = Composer()
    c.begin_approval(ApprovalRequest("command", "rm -rf ~/"))
    assert feed(c, "xa") == []
    assert feed(c, "and then fix the tests") == []
    assert feed(c, "why is it slow?") == []
    assert c.mode == "approval"                   # still waiting for a real answer
    assert feed(c, "y") == [Answer("allow")]      # a keypress does answer


def test_approval_keys_honor_the_offered_choices():
    c = Composer()
    c.begin_approval(ApprovalRequest("command", "rm x", choices=("allow", "deny")))
    assert c.rows(60, 8)[0][0].startswith(" [y]es [n]o")
    assert feed(c, "a") == []                     # not on offer: not an answer
    assert feed(c, "n") == [Answer("deny")]


def test_question_mode_digit_and_free_text():
    c = Composer()
    c.begin_question(QuestionRequest("Which?", ("red", "blue")))
    assert c.rows(60, 8) == (["? "], 0, 2)
    assert feed(c, "2") == [Answer("blue")]
    c.begin_question(QuestionRequest("Name?", ()))
    assert feed(c, "9") == [] and c.text == "9"    # no option 9: it is text
    feed(c, "x")
    assert feed(c, "\r") == [Answer("9x")]


def test_question_mode_non_decimal_digit_is_text():
    c = Composer()
    c.begin_question(QuestionRequest("Which?", ("red", "blue")))
    assert feed(c, "²") == [] and c.text == "²"    # a "digit" int() cannot read
    c.begin_question(QuestionRequest("Which?", ("red", "blue")))
    assert feed(c, "2") == [Answer("blue")]        # a real one still picks
