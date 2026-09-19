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
    row, col = c.line(40)
    assert row == "> line one (+1 lines)"
    assert feed(c, "\r") == [Submit("line one\nline two")]


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


def test_line_scrolls_around_the_cursor():
    c = Composer()
    feed(c, "x" * 50)
    row, col = c.line(20)
    assert len(row) == 19 and row.startswith("> ") and col == 19   # 17 visible chars, cursor after the last
    feed(c, b"\x01")
    row, col = c.line(20)
    assert row == "> " + "x" * 18 and col == 2


def test_approval_mode_keys():
    c = Composer()
    c.begin_approval(ApprovalRequest("command", "rm x"))
    assert c.line(60)[0].startswith(" [y]es [a]lways [n]o")
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
    assert c.line(60)[0].startswith(" [y]es [n]o")
    assert feed(c, "a") == []                     # not on offer: not an answer
    assert feed(c, "n") == [Answer("deny")]


def test_question_mode_digit_and_free_text():
    c = Composer()
    c.begin_question(QuestionRequest("Which?", ("red", "blue")))
    assert c.line(60)[0] == "? "
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
