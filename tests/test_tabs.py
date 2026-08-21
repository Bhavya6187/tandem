"""Tab state machine: which presses are bar-only and which are real flips."""

from tandem.tabs import TabMove, TabState

PARTS = ["claude", "codex", "opencode"]


def test_harness_cycle_presses_flip_to_next():
    t = TabState(PARTS)
    m = t.press("claude")
    assert m.kind == "flip" and m.target == "codex" and m.tab == "harness"
    assert t.pending_target() == "codex"
    t.settle("codex")
    assert t.tab == "harness" and t.pending_target() == ""


def test_last_harness_press_enters_mixed_bar_only():
    """First-ever mixed entry adopts the harness you came from; focus ==
    active means no process change — a bar-only move, applied immediately."""
    t = TabState(PARTS)
    m = t.press("opencode")
    assert m == TabMove(kind="bar", tab="mixed", focus="opencode")
    assert t.tab == "mixed" and t.focus == "opencode"
    assert t.pending_target() == ""


def test_mixed_entry_with_sticky_focus_elsewhere_is_a_flip():
    t = TabState(PARTS, tab="harness", focus="codex")
    m = t.press("opencode")
    assert m.kind == "flip" and m.target == "codex" and m.tab == "mixed"
    t.settle("codex")
    assert t.tab == "mixed" and t.focus == "codex"


def test_mixed_press_leaves_to_first_participant():
    t = TabState(PARTS, tab="mixed", focus="codex")
    m = t.press("codex")
    assert m.kind == "flip" and m.target == "claude" and m.tab == "harness"
    t.settle("claude")
    assert t.tab == "harness"
    assert t.focus == "codex"   # sticky across visits


def test_mixed_press_bar_only_when_focus_is_first():
    t = TabState(PARTS, tab="mixed", focus="claude")
    m = t.press("claude")
    assert m == TabMove(kind="bar", tab="harness", focus="claude")
    assert t.tab == "harness"


def test_second_press_cancels_pending_flip():
    t = TabState(PARTS)
    t.press("claude")
    m = t.press("claude")
    assert m.kind == "cancel"
    assert t.pending_target() == ""
    assert t.tab == "harness"


def test_routed_sets_pending_and_settle_moves_focus():
    t = TabState(PARTS, tab="mixed", focus="claude")
    t.routed("codex")
    assert t.pending_target() == "codex"
    t.settle("codex")
    assert t.tab == "mixed" and t.focus == "codex"


def test_cancelled_clears_pending():
    t = TabState(PARTS, tab="mixed", focus="claude")
    t.routed("codex")
    t.cancelled()
    assert t.pending_target() == ""


def test_version_bumps_on_visible_changes():
    t = TabState(PARTS)
    v0 = t.version
    t.press("opencode")           # bar move into mixed
    assert t.version > v0


def test_snapshot():
    t = TabState(PARTS, tab="mixed", focus="codex")
    assert t.snapshot("codex") == {
        "tab": "mixed", "focus": "codex", "routing_ok": True}
    assert t.snapshot("codex", routing_ok=False)["routing_ok"] is False
    t2 = TabState(PARTS)
    assert t2.snapshot("claude") == {
        "tab": "harness", "focus": "", "routing_ok": True}


def test_two_participant_cycle():
    t = TabState(["claude", "codex"])
    assert t.press("claude").target == "codex"
    t.settle("codex")
    m = t.press("codex")
    assert m.kind == "bar" and m.tab == "mixed"
