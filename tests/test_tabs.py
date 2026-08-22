"""Tab state machine: which presses are bar-only and which are real flips."""

from tandem.frame import MIXED_TAB
from tandem.tabs import MIXED, TabMove, TabState

PARTS = ["claude", "codex", "opencode"]


def test_the_bar_reads_the_same_mixed_marker_this_module_writes():
    """`snapshot()["tab"]` is written here and read by the bar (and by the
    prompt hook). The two constants are declared in different modules, so a
    rename on either side would silently stop the bar showing the mixed slot
    as active — with nothing else failing."""
    assert MIXED_TAB == MIXED


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


# -- first entry must land somewhere you can route *from* --------------------
#
# Ctrl-] only ever enters the mixed tab from the *last* participant, and the
# focus moves after that only by way of a routed turn — which needs a prompt
# hook in the focus harness. With the stock cycle the last participant is
# opencode, which has no hook: adopting it would make the mixed tab's focus
# both un-routable and unreachable, i.e. routing off for good.

def test_first_entry_skips_a_focus_you_cannot_route_from():
    """opencode is last in the stock cycle and hookless, so the entry press
    lands on the first participant that *can* route — a flip, not a bar
    move."""
    t = TabState(PARTS, routable={"claude", "codex"})
    m = t.press("opencode")
    assert m.kind == "flip" and m.target == "claude" and m.tab == MIXED
    t.settle("claude")
    assert t.tab == MIXED and t.focus == "claude"


def test_first_entry_takes_the_first_routable_in_cycle_order():
    t = TabState(PARTS, routable={"codex"})
    m = t.press("opencode")
    assert m.kind == "flip" and m.target == "codex" and m.tab == MIXED
    t.settle("codex")
    assert t.tab == MIXED and t.focus == "codex"


def test_first_entry_adopts_the_active_harness_when_it_can_route():
    """Nothing to fix: the harness you came from is routable, so entry stays
    the free bar move it always was."""
    t = TabState(PARTS, routable={"claude", "opencode"})
    assert t.press("opencode") == TabMove(kind="bar", tab=MIXED,
                                          focus="opencode")
    assert t.tab == MIXED and t.focus == "opencode"


def test_first_entry_adopts_the_active_harness_when_nothing_is_routable():
    """No plugin anywhere: there is no better focus to move to, so the old
    bar move stands and the bar's `(no @-routing)` hint carries the news."""
    t = TabState(PARTS, routable=set())
    assert t.press("opencode") == TabMove(kind="bar", tab=MIXED,
                                          focus="opencode")
    assert t.tab == MIXED and t.focus == "opencode"


def test_a_sticky_focus_that_lost_its_hook_is_repaired_on_entry():
    """Same substitution heals a focus saved when the plugin was installed
    and re-entered after it was removed."""
    t = TabState(PARTS, tab="harness", focus="claude", routable={"codex"})
    m = t.press("opencode")
    assert m.kind == "flip" and m.target == "codex" and m.tab == MIXED


def test_unknown_routability_leaves_every_participant_eligible():
    """`routable=None` is "not measured" — the caller that never passes it
    (and every pre-existing one) keeps the plain adopt-the-active rule."""
    t = TabState(PARTS, routable=None)
    assert t.press("opencode").kind == "bar"
    assert t.focus == "opencode"


def test_leaving_the_mixed_tab_ignores_routability():
    """The exit press goes to the first participant whatever its hooks: it
    is a harness tab, not a focus."""
    t = TabState(PARTS, tab=MIXED, focus="codex", routable={"codex"})
    m = t.press("codex")
    assert m.kind == "flip" and m.target == "claude" and m.tab == "harness"


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


def test_routed_declines_when_a_press_owns_the_pending_slot():
    """The pump records a press before it arms the monitor, so a mixer tick
    can land on an unarmed monitor with a press already pending. The claim
    must lose there — overwriting would retarget the user's flip and double-
    toggle the monitor."""
    t = TabState(PARTS)
    t.press("claude")                       # user armed a flip to codex
    v = t.version
    assert t.routed("opencode") is False
    assert t.pending_target() == "codex"    # untouched
    assert t.version == v                   # nothing visible changed


def test_press_cancels_a_pending_that_came_from_routed():
    t = TabState(PARTS, tab="mixed", focus="claude")
    assert t.routed("codex") is True
    m = t.press("claude")
    assert m.kind == "cancel"
    assert t.pending_target() == ""
    assert t.tab == "mixed" and t.focus == "claude"


def test_routed_from_the_harness_tab_stays_in_the_harness_tab():
    t = TabState(PARTS, tab="harness")
    assert t.routed("codex") is True
    assert t.pending.tab == "harness"
    t.settle("codex")
    assert t.tab == "harness" and t.focus == ""


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
