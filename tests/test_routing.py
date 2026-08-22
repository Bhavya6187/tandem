"""RouteCoordinator: one run's routed-turn lifecycle, driven directly.

The runner owns the threads, the pty and the launch; this is everything a
routed turn does between them, exercised without any of it — no real
monitor, no real harness. The tests that drive the same code on the real
threads (mixer, injector, monitor cancel) stay in test_runner.py.
"""

import threading

from tandem import routefile
from tandem.ptyrun import PtyControl
from tandem.routefile import RouteRequest
from tandem.routing import RouteCoordinator
from tandem.tabs import TabState


class _StubMonitor:
    """Everything `pickup` and `deliver` read off the monitor: the two
    guards that say a flip is already in flight, and the arm the mixer
    fires."""

    def __init__(self, armed=False, flip_requested=False):
        self.pressed = 0
        self._armed = armed
        self.flip_requested = flip_requested

    def armed(self):
        return self._armed

    def flip_pressed(self):
        self.pressed += 1


class _InjectChild:
    def __init__(self):
        self.written = b""

    def isalive(self):
        return True

    def write(self, data):
        self.written += data


# Longer than the 60 characters the exit notes used to truncate at. Once the
# request is gone the note IS the prompt, so it has to carry all of it.
_LONG_PROMPT = ("rewrite this migration to be idempotent, then run the whole"
                " suite twice and report what changed")
assert len(_LONG_PROMPT) > 60


def _coord(env, active, tabs=None, inject=None, adapter=None,
           routing_ok=True):
    """A coordinator bound to a launch, the way `_run` binds one."""
    return RouteCoordinator(env.session, tabs, active,
                            env.session.native_id(active), adapter,
                            routing_ok, inject, [])


def _claimed(tandem_id, req):
    """Put a request where the frame's own slot has it: written by the hook
    and already claimed. That is what an injecting run inherits, and what a
    crashed run leaves behind."""
    routefile.write_route(tandem_id, req)
    assert routefile.claim(tandem_id, req.id) is True


# -- pickup -------------------------------------------------------------------


def test_pickup_arms_and_claims(env_factory):
    env = env_factory(active="claude")
    req = RouteRequest("codex", "", "do it", "claude", "→ codex")
    routefile.write_route(env.session.tandem_id, req)
    tabs = TabState(env.session.participants, tab="mixed", focus="claude")
    c = _coord(env, "claude", tabs=tabs)
    monitor = _StubMonitor()
    c.pickup(monitor)
    assert c.route_request is not None and c.route_request.target == "codex"
    # the claim is the rename: out of the hook's slot, into the frame's
    assert routefile.read_claimed(env.session.tandem_id) == req
    assert routefile.read_pending(env.session.tandem_id) is None
    assert monitor.pressed == 1
    assert tabs.pending_target() == "codex"


def test_pickup_ignores_a_route_to_the_current_harness(env_factory):
    env = env_factory(active="claude")
    routefile.write_route(env.session.tandem_id, RouteRequest(
        "claude", "", "do it", "claude", "→ claude"))
    tabs = TabState(env.session.participants, tab="mixed", focus="claude")
    c = _coord(env, "claude", tabs=tabs)
    monitor = _StubMonitor()
    c.pickup(monitor)
    assert c.route_request is None and monitor.pressed == 0
    assert routefile.read_pending(env.session.tandem_id) is None   # dropped


def test_pickup_drops_a_route_to_a_stranger(env_factory):
    env = env_factory(active="claude")
    routefile.write_route(env.session.tandem_id, RouteRequest(
        "gemini", "", "do it", "claude", "→ gemini"))
    tabs = TabState(env.session.participants, tab="mixed", focus="claude")
    c = _coord(env, "claude", tabs=tabs)
    monitor = _StubMonitor()
    c.pickup(monitor)
    assert c.route_request is None and monitor.pressed == 0
    assert routefile.read_pending(env.session.tandem_id) is None


def test_pickup_leaves_the_request_when_a_press_owns_the_slot(env_factory):
    # The pump records a user press before the glue arms the monitor, so a
    # tick landing in that window sees an unarmed monitor over a taken slot.
    # The claim fails there: nothing may be taken or armed, and the request
    # stays pending for the next tick.
    env = env_factory(active="codex")
    routefile.write_route(env.session.tandem_id, RouteRequest(
        "claude", "", "do it", "codex", "→ claude"))
    tabs = TabState(env.session.participants, tab="mixed", focus="codex")
    assert tabs.press("codex").kind == "flip"     # a user press owns the slot
    c = _coord(env, "codex", tabs=tabs)
    monitor = _StubMonitor()
    c.pickup(monitor)
    assert c.route_request is None and monitor.pressed == 0
    assert routefile.read_pending(env.session.tandem_id) is not None


def test_pickup_gives_the_tab_move_back_when_the_slot_changed(env_factory):
    """Between validating the request and claiming it, a second prompt can
    overwrite the pending slot. The tab move is already taken by then, and
    keeping it would leave a pending flip toward a request nobody holds —
    the next Ctrl-] would read as a cancel."""
    env = env_factory(active="claude")
    routefile.write_route(env.session.tandem_id, RouteRequest(
        "codex", "", "do it", "claude", "→ codex"))
    tabs = TabState(env.session.participants, tab="mixed", focus="claude")
    c = _coord(env, "claude", tabs=tabs)
    real_routed = tabs.routed

    def routed_then_overwrite(target):
        got = real_routed(target)
        routefile.write_route(env.session.tandem_id, RouteRequest(
            "codex", "", "and then this one", "claude", "→ codex"))
        return got

    tabs.routed = routed_then_overwrite
    monitor = _StubMonitor()
    c.pickup(monitor)
    assert c.route_request is None and monitor.pressed == 0
    assert tabs.pending is None
    # the newcomer is untouched, and the next tick can take it
    assert routefile.read_pending(env.session.tandem_id).prompt \
        == "and then this one"
    assert routefile.read_claimed(env.session.tandem_id) is None


def test_pickup_holds_off_while_a_flip_is_already_in_flight(env_factory):
    env = env_factory(active="claude")
    routefile.write_route(env.session.tandem_id, RouteRequest(
        "codex", "", "do it", "claude", "→ codex"))
    tabs = TabState(env.session.participants, tab="mixed", focus="claude")
    c = _coord(env, "claude", tabs=tabs)
    c.pickup(_StubMonitor(armed=True))
    c.pickup(_StubMonitor(flip_requested=True))
    assert c.route_request is None
    assert routefile.read_pending(env.session.tandem_id) is not None
    # and never outside the mixed tab
    tabs.tab = "harness"
    c.pickup(_StubMonitor())
    assert c.route_request is None
    assert routefile.read_pending(env.session.tandem_id) is not None


def test_tick_publishes_the_frame_file_once_per_tab_change(env_factory):
    """`tabs.version` is the whole change feed: the file the hook reads must
    keep up with it without being rewritten four times a second."""
    env = env_factory(active="claude")
    tabs = TabState(env.session.participants, tab="mixed", focus="claude")
    c = _coord(env, "claude", tabs=tabs)
    writes = []
    real_publish = c.publish_frame
    c.publish_frame = lambda: writes.append(1) or real_publish()
    monitor = _StubMonitor()
    c.tick(monitor)
    c.tick(monitor)
    assert writes == [1]
    assert routefile.read_frame_state(env.session.tandem_id) == {
        "tab": "mixed", "focus": "claude", "routing_ok": True}
    tabs.press("claude")            # mixed -> harness: a bar move
    c.tick(monitor)
    assert writes == [1, 1]
    assert routefile.read_frame_state(env.session.tandem_id)["tab"] == "harness"


# -- deliver ------------------------------------------------------------------


def test_deliver_pastes_and_releases(env_factory, monkeypatch):
    env = env_factory(active="codex")
    req = RouteRequest("codex", "", "do it", "claude", "→ codex")
    _claimed(env.session.tandem_id, req)

    class StubAdapter:      # no session_status attr: fixed-delay path
        pass

    control, child = PtyControl(), _InjectChild()
    control.attach(child)
    c = _coord(env, "codex", inject=req, adapter=StubAdapter())
    monkeypatch.setattr("time.sleep", lambda s: None)   # skip settle delays
    c.deliver(control, threading.Event(), _StubMonitor())
    assert child.written == b"\x1b[200~do it\x1b[201~\r"
    assert c.inject_failed is False
    assert routefile.read_claimed(env.session.tandem_id) is None


def test_deliver_waits_for_a_waiting_status(env_factory, monkeypatch):
    env = env_factory(active="claude")
    req = RouteRequest("claude", "", "do it", "codex", "→ claude")
    _claimed(env.session.tandem_id, req)
    answers = ["busy", "busy", "waiting"]

    class StubAdapter:
        def session_status(self, sid):
            return answers.pop(0)

    control, child = PtyControl(), _InjectChild()
    control.attach(child)
    c = _coord(env, "claude", inject=req, adapter=StubAdapter())
    monkeypatch.setattr("time.sleep", lambda s: None)
    c.deliver(control, threading.Event(), _StubMonitor())
    assert answers == [] and child.written.endswith(b"\r")
    assert c.inject_failed is False


def test_deliver_never_believes_opencodes_status(tmp_path, monkeypatch):
    """opencode HAS a `session_status`, and it is the wrong question: it
    reads the transcript sqlite (unknown sid and a resumed session's last
    row both answer "waiting"), which says nothing about whether the TUI has
    drawn. Believing it would either write before the child is attached or
    paste into a TUI that is not listening and then release the request.
    So the gate is claude-by-name and opencode takes the fixed settle."""
    from conftest import Env3
    from tandem.harness.opencode import OpencodeAdapter

    env = Env3(tmp_path, monkeypatch)
    adapter = OpencodeAdapter()
    assert hasattr(adapter, "session_status")     # the trap this guards
    asked = []
    monkeypatch.setattr(OpencodeAdapter, "session_status",
                        lambda self, sid: asked.append(sid) or "waiting")
    slept = []
    monkeypatch.setattr("time.sleep", lambda s: slept.append(s))
    req = RouteRequest("opencode", "", "do it", "claude", "→ opencode")
    _claimed(env.session.tandem_id, req)
    control, child = PtyControl(), _InjectChild()
    control.attach(child)
    c = _coord(env, "opencode", inject=req, adapter=adapter)
    c.deliver(control, threading.Event(), _StubMonitor())
    assert asked == []                    # the probe is never consulted
    assert slept[0] == 2.5                # the fixed settle from spawn
    assert child.written == b"\x1b[200~do it\x1b[201~\r"
    assert c.inject_failed is False


def test_deliver_into_the_wrong_target_keeps_the_request(env_factory):
    env = env_factory(active="claude")
    req = RouteRequest("codex", "", "do it", "claude", "→ codex")
    _claimed(env.session.tandem_id, req)
    c = _coord(env, "claude", inject=req, adapter=object())
    c.deliver(PtyControl(), threading.Event(), _StubMonitor())
    assert c.inject_failed is True
    assert routefile.read_claimed(env.session.tandem_id) is not None


def test_deliver_keeps_the_request_when_the_write_fails(env_factory,
                                                        monkeypatch):
    env = env_factory(active="codex")
    req = RouteRequest("codex", "", "do it", "claude", "→ codex")
    _claimed(env.session.tandem_id, req)

    class StubAdapter:
        pass

    c = _coord(env, "codex", inject=req, adapter=StubAdapter())
    monkeypatch.setattr("time.sleep", lambda s: None)
    c.deliver(PtyControl(),            # nothing attached: the write fails
              threading.Event(), _StubMonitor())
    assert c.inject_failed is True
    assert routefile.read_claimed(env.session.tandem_id) is not None


def test_deliver_gives_up_when_the_run_is_stopping(env_factory, monkeypatch):
    env = env_factory(active="claude")
    req = RouteRequest("claude", "", "do it", "codex", "→ claude")
    _claimed(env.session.tandem_id, req)

    class StubAdapter:
        def session_status(self, sid):
            return "busy"

    control, child = PtyControl(), _InjectChild()
    control.attach(child)
    stop = threading.Event()
    stop.set()
    c = _coord(env, "claude", inject=req, adapter=StubAdapter())
    monkeypatch.setattr("time.sleep", lambda s: None)
    c.deliver(control, stop, _StubMonitor())
    assert c.inject_failed is True and child.written == b""
    assert routefile.read_claimed(env.session.tandem_id) is not None


def test_deliver_holds_off_while_a_flip_is_in_flight(env_factory, monkeypatch):
    """`stop` is set only after `run_in_pty` returns, but the quit ladder runs
    while the child is still alive. A paste landing in there goes into a
    harness that is on its way out and the release would then destroy the
    prompt believing it delivered — so the flip state is checked too."""
    env = env_factory(active="codex")
    req = RouteRequest("codex", "", "do it", "claude", "→ codex")

    class StubAdapter:
        pass

    for monitor in (_StubMonitor(flip_requested=True), _StubMonitor(armed=True)):
        _claimed(env.session.tandem_id, req)
        control, child = PtyControl(), _InjectChild()
        control.attach(child)
        c = _coord(env, "codex", inject=req, adapter=StubAdapter())
        monkeypatch.setattr("time.sleep", lambda s: None)
        c.deliver(control, threading.Event(), monitor)
        assert child.written == b""          # nothing typed into a dying CLI
        assert c.inject_failed is True
        assert routefile.read_claimed(env.session.tandem_id) is not None


def test_deliver_keeps_the_request_when_the_flip_lands_mid_paste(
        env_factory, monkeypatch):
    """The ladder can start between the paste and the release. Re-checking
    before it is what turns that into a kept prompt (a possibly duplicate
    exit note) instead of a destroyed one."""
    env = env_factory(active="codex")
    req = RouteRequest("codex", "", "do it", "claude", "→ codex")
    _claimed(env.session.tandem_id, req)
    monitor = _StubMonitor()

    class StubAdapter:
        pass

    class _FlippingChild(_InjectChild):
        def write(self, data):
            super().write(data)
            if data == b"\r":       # the ladder fires as the turn is submitted
                monitor.flip_requested = True

    control, child = PtyControl(), _FlippingChild()
    control.attach(child)
    c = _coord(env, "codex", inject=req, adapter=StubAdapter())
    monkeypatch.setattr("time.sleep", lambda s: None)
    c.deliver(control, threading.Event(), monitor)
    assert c.inject_failed is True
    assert routefile.read_claimed(env.session.tandem_id) is not None


def test_deliver_never_releases_someone_elses_request(env_factory,
                                                      monkeypatch):
    """A second routed prompt can land while this one is still in flight
    (the hook writes durably before it blocks, and never waits for the
    frame). The release is by id, so the newcomer is not its business."""
    env = env_factory(active="codex")
    req = RouteRequest("codex", "", "do it", "claude", "→ codex")
    _claimed(env.session.tandem_id, req)
    later = RouteRequest("claude", "", "and then this one", "codex",
                         "→ claude")

    class StubAdapter:
        pass

    class _OverwritingChild(_InjectChild):
        def write(self, data):
            super().write(data)
            if data == b"\r":
                routefile.write_route(env.session.tandem_id, later)

    control, child = PtyControl(), _OverwritingChild()
    control.attach(child)
    c = _coord(env, "codex", inject=req, adapter=StubAdapter())
    monkeypatch.setattr("time.sleep", lambda s: None)
    c.deliver(control, threading.Event(), _StubMonitor())
    assert c.inject_failed is False          # this one did land
    assert routefile.read_claimed(env.session.tandem_id) is None
    assert routefile.read_pending(env.session.tandem_id) == later


# -- cancel, sweep, notes -----------------------------------------------------


def test_cancel_releases_the_request_and_quotes_it(env_factory):
    env = env_factory(active="claude")
    req = RouteRequest("codex", "", _LONG_PROMPT, "claude", "→ codex")
    routefile.write_route(env.session.tandem_id, req)
    tabs = TabState(env.session.participants, tab="mixed", focus="claude")
    c = _coord(env, "claude", tabs=tabs)
    c.pickup(_StubMonitor())
    c.cancelled()
    assert c.route_request is None and tabs.pending is None
    assert routefile.read_claimed(env.session.tandem_id) is None
    # the file is gone, so the note IS the prompt: all of it
    assert c.notes == ["routed turn cancelled — the prompt was "
                       f"discarded: {_LONG_PROMPT!r}"]


def test_cancel_without_a_route_only_drops_the_tab_move(env_factory):
    env = env_factory(active="claude")
    tabs = TabState(env.session.participants, tab="mixed", focus="claude")
    tabs.press("claude")                   # a plain user flip, nothing routed
    c = _coord(env, "claude", tabs=tabs)
    c.cancelled()
    assert tabs.pending is None and c.notes == []


def test_cancel_leaves_a_newer_request_alone(env_factory):
    # a second prompt landed in the pending slot after the arm was taken;
    # undoing this run's arm must not eat it
    env = env_factory(active="claude")
    routefile.write_route(env.session.tandem_id, RouteRequest(
        "codex", "", "do it", "claude", "→ codex"))
    tabs = TabState(env.session.participants, tab="mixed", focus="claude")
    c = _coord(env, "claude", tabs=tabs)
    c.pickup(_StubMonitor())
    later = RouteRequest("codex", "", "and then this one", "claude", "→ codex")
    routefile.write_route(env.session.tandem_id, later)
    c.cancelled()
    assert routefile.read_pending(env.session.tandem_id) == later


def test_sweep_notes_both_slots_and_clears_them(env_factory):
    env = env_factory(active="claude")
    _claimed(env.session.tandem_id, RouteRequest(
        "codex", "", "the one that never landed", "claude", "→ codex"))
    routefile.write_route(env.session.tandem_id, RouteRequest(
        "opencode", "", _LONG_PROMPT, "claude", "→ opencode"))
    c = _coord(env, "claude")
    c.sweep_leftovers()
    assert routefile.sweep(env.session.tandem_id) == (None, None)
    assert c.notes == [
        f"a routed prompt was never picked up and was kept: {_LONG_PROMPT!r}"
        " (target opencode)",
        "a routed prompt was never delivered and was kept: "
        "'the one that never landed' (target codex)",
    ]


def test_sweep_keeps_the_request_this_run_is_delivering(env_factory):
    env = env_factory(active="codex")
    req = RouteRequest("codex", "", "do it", "claude", "→ codex")
    _claimed(env.session.tandem_id, req)
    c = _coord(env, "codex", inject=req)
    c.sweep_leftovers()
    assert routefile.read_claimed(env.session.tandem_id) == req
    assert c.notes == []


def test_exit_notes_quote_an_undelivered_prompt(env_factory):
    env = env_factory(active="codex")
    req = RouteRequest("claude", "", _LONG_PROMPT, "codex", "→ claude")
    c = _coord(env, "codex", inject=req)
    c.exit_notes()
    assert c.notes == []                   # nothing failed yet
    c.inject_failed = True
    c.exit_notes()
    assert c.notes == [
        "routed prompt was not delivered — it is preserved; re-type"
        f" it in claude ({_LONG_PROMPT!r})"]
