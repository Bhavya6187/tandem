"""The fence-aware paragraph splitter and the rich renderer behind the
chat window's markdown, both pure."""

import re

from tandem.chat.markdown import cells, ready_blocks, render_markdown

SGR = re.compile(r"\x1b\[[0-9;]*m")


def test_nothing_is_ready_without_a_blank_line():
    assert ready_blocks("one\ntwo") == ("", "one\ntwo")


def test_a_blank_line_releases_everything_up_to_it():
    assert ready_blocks("one\ntwo\n\nthr\n") == ("one\ntwo\n\n", "thr\n")
    assert ready_blocks("one\ntwo\n\nthr") == ("", "one\ntwo\n\nthr")     # "thr" may still grow into "   thr…"


def test_the_unterminated_tail_is_never_ready():
    assert ready_blocks("one\n\ntwo") == ("", "one\n\ntwo")
    assert ready_blocks("one\n\ntwo\n") == ("one\n\n", "two\n")


def test_a_blank_line_inside_a_fence_releases_nothing():
    buf = "```py\nx = 1\n\ny = 2\n"
    assert ready_blocks(buf) == ("", buf)


def test_a_closing_fence_releases_through_it():
    buf = "intro\n\n```py\nx = 1\n```\nafter"
    assert ready_blocks(buf) == ("intro\n\n```py\nx = 1\n```\n", "after")
    # the blank before the fence waits for the fence line, which does not continue anything
    assert ready_blocks("intro\n\n```py\n")[0] == "intro\n\n"


def test_fence_markers_must_match_and_may_be_longer_or_indented():
    assert ready_blocks("~~~\n```\n\n~~\n")[0] == ""            # backticks inside a tilde fence
    assert ready_blocks("````\n```\n\n````\n")[0] == "````\n```\n\n````\n"
    assert ready_blocks("  ```\ncode\n  ```\n")[0] == "  ```\ncode\n  ```\n"
    assert ready_blocks("```\ncode\n``` not a closer\n\n")[0] == ""  # info string on a closer: still code


def test_a_blank_line_inside_a_loose_list_is_not_a_boundary():
    """Blank lines separate items of a loose list and precede indented
    continuations; cutting there would render `2. second` as a new list
    starting at 2 and the continuation as a paragraph. (Codex plan finding.)"""
    text = "1. first\n\n   continuation\n\n2. second\n\nAfter\n"
    assert ready_blocks(text) == ("1. first\n\n   continuation\n\n2. second\n\n", "After\n")
    assert ready_blocks("- a\n\n- b\n\n- c\n")[0] == ""          # still going: no non-list line yet
    assert ready_blocks("> quote\n\n> more\n\nplain\n")[0] == "> quote\n\n> more\n\n"


def test_a_blank_line_waits_for_the_next_line_before_deciding():
    # the line after the blank has not arrived: undecided, nothing released
    assert ready_blocks("1. first\n\n") == ("", "1. first\n\n")
    assert ready_blocks("para\n\n") == ("", "para\n\n")
    assert ready_blocks("para\n\nnext") == ("", "para\n\nnext")            # a partial line cannot decide
    assert ready_blocks("para\n\nnext\n") == ("para\n\n", "next\n")


def test_streaming_a_loose_list_renders_like_the_whole():
    text = "Intro.\n\n1. first\n\n   continuation\n\n2. second\n\nAfter.\n"
    whole = render_markdown(text, 40, False)
    streamed, buf = [], ""
    for ch in text:                                  # one character at a time
        buf += ch
        ready, buf = ready_blocks(buf)
        if ready:
            streamed += render_markdown(ready, 40, False) + ([""] if ready.endswith("\n\n") else [])
    if buf:
        streamed += render_markdown(buf, 40, False)
    assert streamed == whole


def test_headings_are_not_boxed():
    rows = render_markdown("# Title\n\ntext\n", 40, False)
    assert rows[0].strip() == "Title" and not any(ch in "".join(rows) for ch in "━┃┏┓┗┛│─")


def test_a_marker_split_across_deltas_is_one_opener():
    buf = ""
    for delta in ("``", "`python\nprint(1)\n", "\n", "```\n"):
        buf += delta
    assert ready_blocks(buf) == ("```python\nprint(1)\n\n```\n", "")
    # and nothing was released early: the second delta alone holds an open fence
    assert ready_blocks("```python\nprint(1)\n\n") == ("", "```python\nprint(1)\n\n")


def test_render_bolds_and_wraps_within_the_width():
    rows = render_markdown("This is **bold** text that is fairly long and wraps.\n", 30, True)
    assert any("\x1b[1m" in r for r in rows)
    assert all(cells(r) <= 29 for r in rows)
    assert all(not SGR.sub("", r).endswith(" ") for r in rows)


def test_render_without_color_is_plain():
    rows = render_markdown("# Title\n\n- one\n- two\n", 40, False)
    assert not any("\x1b" in r for r in rows)
    assert any("Title" in r for r in rows) and any("one" in r for r in rows)


def test_render_code_blocks_keep_their_text_and_drop_padding():
    rows = render_markdown("```py\nx = 1\n```\n", 40, True)
    plain = [SGR.sub("", r) for r in rows]
    assert any("x = 1" in p for p in plain)
    assert all(not p.endswith(" ") for p in plain)                # rich pads code rows to the width
    assert all(cells(r) <= 39 for r in rows)


def test_render_strips_hyperlink_osc():
    rows = render_markdown("see [docs](https://example.com) now\n", 40, True)
    assert not any("\x1b]" in r for r in rows)
    assert any("docs" in SGR.sub("", r) for r in rows)


def test_rendered_rows_fit_in_cells_with_wide_glyphs():
    rows = render_markdown("修复 the bug 修复 the bug 修复 the bug 修复 the bug 修复\n", 24, False)
    assert all(cells(r) <= 23 for r in rows)


def test_render_drops_the_trailing_empty_row():
    rows = render_markdown("just a line\n", 40, False)
    assert rows and rows[-1] != ""
