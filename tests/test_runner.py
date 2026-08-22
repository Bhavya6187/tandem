"""InteractiveRunner: user-configured [harness] args land in the spawned argv."""

import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

from tandem import paths, routefile, runner
from tandem.routefile import RouteRequest
from tandem.runner import FlipMonitor, wait_until_safe
from tandem.tabs import TabState


class _Sink:
    def handle(self, line, ctx, cursor): ...

    def close(self): ...


def _null_sink(store, session, source, target):
    return _Sink()


def _run_capturing_argv(env, monkeypatch):
    calls = {}
    monkeypatch.setattr(
        runner, "run_in_pty",
        lambda argv, cwd=None, **kw: calls.update(argv=argv) or 0,
    )
    code = runner.InteractiveRunner(
        env.session, lambda store, session, source: _Sink()).run()
    assert code == 0
    return calls["argv"]


def test_claude_resume_gets_args_before_hook_extras(env_factory, monkeypatch):
    env = env_factory(active="claude")
    (paths.tandem_home() / "config.toml").write_text(
        '[claude]\nargs = ["--dangerously-skip-permissions"]\n'
    )
    argv = _run_capturing_argv(env, monkeypatch)
    i = argv.index("--resume")
    assert argv[i + 1] == env.session.native_id("claude")
    assert argv[i + 2] == "--dangerously-skip-permissions"
    assert argv[i + 3] == "--settings"  # hook extras immediately after


def test_claude_fresh_launch_gets_args(env_factory, monkeypatch):
    env = env_factory(active="claude")
    env.claude_shadow.unlink()  # no transcript -> fresh --session-id launch
    (paths.tandem_home() / "config.toml").write_text(
        '[claude]\nargs = ["--dangerously-skip-permissions"]\n'
    )
    argv = _run_capturing_argv(env, monkeypatch)
    i = argv.index("--session-id")
    assert argv[i + 2] == "--dangerously-skip-permissions"


def test_codex_gets_its_own_args(env_factory, monkeypatch):
    env = env_factory(active="codex")
    (paths.tandem_home() / "config.toml").write_text(
        '[codex]\nargs = ["--dangerously-bypass-approvals-and-sandbox"]\n\n'
        '[claude]\nargs = ["--should-not-appear"]\n'
    )
    argv = _run_capturing_argv(env, monkeypatch)
    i = argv.index("resume")
    assert argv[i + 1] == env.session.native_id("codex")
    assert argv[i + 2] == "--dangerously-bypass-approvals-and-sandbox"
    assert "--should-not-appear" not in argv


def test_codex_fresh_mint_gets_args_after_bare_binary(env_factory, monkeypatch):
    # codex minting its own session id launches as bare `codex`, so the
    # configured args are the first tokens after the binary.
    env = env_factory(active="codex")
    session = env.store.create_session(
        env.cwd, "codex", ["claude", "codex"],
        {"claude": env.session.native_id("claude"), "codex": None},
    )
    (paths.tandem_home() / "config.toml").write_text(
        '[codex]\nargs = ["--dangerously-bypass-approvals-and-sandbox"]\n'
    )
    calls = {}
    monkeypatch.setattr(
        runner, "run_in_pty",
        lambda argv, cwd=None, **kw: calls.update(argv=argv) or 0,
    )
    code = runner.InteractiveRunner(
        session, lambda store, session, source: _Sink()).run()
    assert code == 0
    argv = calls["argv"]
    assert argv[:2] == ["codex", "--dangerously-bypass-approvals-and-sandbox"]


def test_oneoff_argv_never_gains_args(tmp_path, monkeypatch):
    # `tandem run` and doctor probes build argv via oneoff_argv; a configured
    # args list must never leak in (the append lives in InteractiveRunner).
    from tandem.harness import get_adapter

    home = tmp_path / ".tandem"
    home.mkdir()
    monkeypatch.setenv("TANDEM_HOME", str(home))
    (home / "config.toml").write_text(
        '[claude]\nargs = ["--should-not-appear"]\n\n'
        '[codex]\nargs = ["--should-not-appear"]\n'
    )
    assert get_adapter("claude").oneoff_argv("sid", "task") == [
        "claude", "--resume", "sid", "-p", "task"]
    assert get_adapter("codex").oneoff_argv("sid", "task") == [
        "codex", "exec", "--skip-git-repo-check", "resume", "sid", "task"]


def test_no_config_leaves_argv_unchanged(env_factory, monkeypatch):
    env = env_factory(active="claude")
    argv = _run_capturing_argv(env, monkeypatch)
    assert argv[:3] == ["claude", "--resume", env.session.native_id("claude")]
    assert argv[3] == "--settings"


def _touch(path, mtime):
    path.write_text("x")
    os.utime(path, (mtime, mtime))


def test_wait_idle_when_sentinel_newer_than_transcript(tmp_path):
    t, s = tmp_path / "t.jsonl", tmp_path / "s.turn"
    now = time.time()
    _touch(t, now - 10)
    _touch(s, now - 5)   # marker closed the last turn
    assert wait_until_safe(t, s, cancelled=lambda: False) is True


def test_wait_idle_when_no_files(tmp_path):
    assert (
        wait_until_safe(tmp_path / "none", tmp_path / "none2",
                        cancelled=lambda: False)
        is True
    )


def test_wait_quiescence_fallback(tmp_path):
    # The marker-less shape, built honestly: tandem refused to clobber a
    # user-configured codex notify, so the hook was never wired and the
    # sentinel file never appears at all (mtime 0 < transcript, forever).
    # Quiescence is the only exit, which is exactly what the fallback is for.
    t, s = tmp_path / "t.jsonl", tmp_path / "never-touched.turn"
    _touch(t, time.time() - 3)   # transcript quiet for 3s, no marker since
    assert not s.exists()
    assert (
        wait_until_safe(t, s, cancelled=lambda: False, quiesce=2.0,
                        marker_wired=False) is True
    )


def test_wait_idle_when_no_files_wired(tmp_path):
    # Fresh session: the harness has written nothing and the hook has not run
    # yet, so 0 >= 0 reads idle in *both* modes. The flip must fire at once,
    # not sit behind the wired mode's missed-marker valve.
    assert (
        wait_until_safe(tmp_path / "none", tmp_path / "none2",
                        cancelled=lambda: False, marker_wired=True)
        is True
    )


def test_wait_wired_ignores_turn_pacing_quiet(tmp_path):
    # With the marker wired, transcript quiet is NOT a turn boundary: a long
    # tool call writes nothing between the tool_use append and the tool_result
    # append, and firing there kills the harness mid-turn. quiesce is scaled
    # down (1.0 stands in for the real 120s valve) to keep the test fast — the
    # valve does eventually trip, it is just never the normal trigger.
    t, s = tmp_path / "t.jsonl", tmp_path / "s.turn"
    _touch(t, time.time())         # turn in flight
    _touch(s, time.time() - 30)    # marker live, but this turn is still open
    done = threading.Event()
    result = {}

    def waiter():
        result["ok"] = wait_until_safe(t, s, cancelled=lambda: False,
                                       quiesce=1.0, poll=0.05,
                                       marker_wired=True)
        done.set()

    threading.Thread(target=waiter, daemon=True).start()
    assert not done.wait(timeout=0.5)   # 0.5s of quiet: still waiting
    assert done.wait(timeout=3)         # valve trips once quiesce elapses
    assert result["ok"] is True


def test_wait_wired_default_quiesce_is_the_valve_not_2s(tmp_path):
    # The mode's whole point: the same 3s-quiet mid-turn transcript that the
    # marker-less mode calls idle must still be mid-turn when the marker is
    # wired, on default settings.
    t, s = tmp_path / "t.jsonl", tmp_path / "s.turn"
    _touch(t, time.time() - 3)
    _touch(s, time.time() - 30)
    assert wait_until_safe(t, s, cancelled=lambda: False,
                           marker_wired=False) is True
    cancel, done = threading.Event(), threading.Event()
    result = {}

    def waiter():
        result["ok"] = wait_until_safe(t, s, cancelled=cancel.is_set,
                                       poll=0.05, marker_wired=True)
        done.set()

    threading.Thread(target=waiter, daemon=True).start()
    assert not done.wait(timeout=0.4)   # parked behind the 120s valve
    cancel.set()                        # don't leave it parked for 120s
    assert done.wait(timeout=2)
    assert result["ok"] is False


def test_wait_wired_marker_touch_releases_promptly(tmp_path):
    # The marker path is unchanged by the mode: the touch is the trigger.
    t, s = tmp_path / "t.jsonl", tmp_path / "s.turn"
    _touch(t, time.time())
    _touch(s, time.time() - 30)
    done = threading.Event()
    result = {}

    def waiter():
        result["ok"] = wait_until_safe(t, s, cancelled=lambda: False,
                                       poll=0.05, marker_wired=True)
        done.set()

    threading.Thread(target=waiter, daemon=True).start()
    time.sleep(0.2)
    assert not done.is_set()
    _touch(s, time.time() + 1)      # marker fires
    assert done.wait(timeout=2)
    assert result["ok"] is True


def test_wait_blocks_midturn_then_marker_releases(tmp_path):
    t, s = tmp_path / "t.jsonl", tmp_path / "s.turn"
    _touch(t, time.time())          # a line just landed: turn in flight
    _touch(s, time.time() - 30)
    done = threading.Event()
    result = {}

    def waiter():
        result["ok"] = wait_until_safe(t, s, cancelled=lambda: False,
                                       quiesce=30.0, poll=0.05)
        done.set()

    threading.Thread(target=waiter, daemon=True).start()
    time.sleep(0.2)
    assert not done.is_set()        # still waiting
    _touch(s, time.time() + 1)      # marker fires
    assert done.wait(timeout=2)
    assert result["ok"] is True


def test_wait_unknown_transcript_wired_is_not_read_as_idle(tmp_path):
    # codex mints its own session id, so the flip monitor starts with no
    # transcript at all. _mtime(None) is 0.0, which would make `s >= t` true
    # against any sentinel and fire the flip instantly — mid-turn. No
    # evidence is not idleness: hold, and let the valve decide.
    s = tmp_path / "s.turn"
    _touch(s, time.time() - 30)      # a previous turn's marker exists
    done = threading.Event()
    result = {}

    def waiter():
        result["ok"] = wait_until_safe(None, s, cancelled=lambda: False,
                                       quiesce=1.0, poll=0.05,
                                       marker_wired=True)
        done.set()

    threading.Thread(target=waiter, daemon=True).start()
    assert not done.wait(timeout=0.5)   # did NOT fire instantly
    assert done.wait(timeout=3)         # valve, anchored to arm time
    assert result["ok"] is True


def test_wait_provider_publishing_a_path_restores_normal_rules(tmp_path):
    # The tail thread discovers the rollout mid-wait and publishes it; from
    # that poll on the ordinary marker/quiescence rules apply.
    t, s = tmp_path / "t.jsonl", tmp_path / "s.turn"
    _touch(s, time.time() - 30)
    published: list = [None]
    done = threading.Event()
    result = {}

    def waiter():
        result["ok"] = wait_until_safe(None, s, cancelled=lambda: False,
                                       quiesce=30.0, poll=0.05,
                                       marker_wired=True,
                                       provider=lambda: published[0])
        done.set()

    threading.Thread(target=waiter, daemon=True).start()
    assert not done.wait(timeout=0.3)   # unknown transcript: parked
    _touch(t, time.time())              # a turn is in flight
    published[0] = t
    assert not done.wait(timeout=0.3)   # known now, and mid-turn: still parked
    _touch(s, time.time() + 1)          # marker closes the turn
    assert done.wait(timeout=2)
    assert result["ok"] is True


def test_wait_standalone_default_provider_reads_its_argument(tmp_path):
    # Back-compat: with no provider the positional transcript is what every
    # poll reads, exactly as before the provider existed.
    t, s = tmp_path / "t.jsonl", tmp_path / "s.turn"
    _touch(t, time.time())
    _touch(s, time.time() - 30)
    done = threading.Event()
    result = {}

    def waiter():
        result["ok"] = wait_until_safe(t, s, cancelled=lambda: False,
                                       poll=0.05, marker_wired=True)
        done.set()

    threading.Thread(target=waiter, daemon=True).start()
    assert not done.wait(timeout=0.3)
    _touch(s, time.time() + 1)
    assert done.wait(timeout=2)
    assert result["ok"] is True


def test_monitor_transcript_published_midwait_is_picked_up(tmp_path):
    # The monitor half of the same story: the runner's tail thread assigns
    # monitor.transcript once codex's rollout appears, and the live wait sees
    # it on its next poll.
    t, s = tmp_path / "t.jsonl", tmp_path / "s.turn"
    _touch(t, time.time())
    _touch(s, time.time() - 30)
    control = _StubControl()
    m = FlipMonitor(control, [b"\x04"], transcript=None, sentinel=s,
                    marker_wired=True, quiesce=30.0, poll=0.05)
    m.start()
    m.flip_pressed()
    time.sleep(0.3)
    assert m.flip_requested is False     # unknown transcript: no instant fire
    m.transcript = t                     # tail thread discovered the rollout
    time.sleep(0.2)
    assert m.flip_requested is False     # mid-turn under the normal rules
    _touch(s, time.time() + 1)           # marker fires
    deadline = time.time() + 3
    while not m.flip_requested and time.time() < deadline:
        time.sleep(0.05)
    m.stop()
    assert m.flip_requested is True
    assert control.calls == [[b"\x04"]]


def test_runner_publishes_the_discovered_codex_rollout_to_the_monitor(
    env_factory, monkeypatch
):
    # Fresh codex (no session id yet): the tail thread finds the rollout and
    # hands it to the monitor, so an armed flip stops being judged blind.
    env = env_factory(active="codex")
    session = env.store.create_session(
        env.cwd, "codex", ["claude", "codex"],
        {"claude": env.session.native_id("claude"), "codex": None},
    )
    rollout = env.codex_shadow
    monkeypatch.setattr(
        runner, "await_codex_rollout",
        lambda cwd, after, timeout=None: rollout,
    )
    made = {}
    real = runner.FlipMonitor

    def capture(*a, **kw):
        made["monitor"] = real(*a, **kw)
        return made["monitor"]

    monkeypatch.setattr(runner, "FlipMonitor", capture)

    def fake_run_in_pty(argv, cwd=None, frame=None, control=None, child=None):
        deadline = time.time() + 3
        while made["monitor"].transcript is None and time.time() < deadline:
            time.sleep(0.02)
        return 0

    monkeypatch.setattr(runner, "run_in_pty", fake_run_in_pty)
    runner.InteractiveRunner(session, lambda st, se, so, tg: _Sink()).run()
    assert made["monitor"].transcript == rollout


def test_wait_cancelled(tmp_path):
    t, s = tmp_path / "t.jsonl", tmp_path / "s.turn"
    _touch(t, time.time())
    _touch(s, time.time() - 30)
    assert (
        wait_until_safe(t, s, cancelled=lambda: True, quiesce=30.0) is False
    )


class _StubControl:
    def __init__(self):
        self.calls = []

    def terminate(self, soft, **kw):
        self.calls.append(soft)
        return "soft"


def test_monitor_arm_wait_terminate(tmp_path):
    control = _StubControl()
    m = FlipMonitor(control, [b"\x04"], transcript=None,
                    sentinel=tmp_path / "s.turn")
    m.start()
    assert m.armed() is False
    m.flip_pressed()                 # idle (no files) -> fires immediately
    deadline = time.time() + 3
    while not m.flip_requested and time.time() < deadline:
        time.sleep(0.05)
    m.stop()
    assert m.flip_requested is True
    assert m.how == "soft"
    assert control.calls == [[b"\x04"]]


class _OrderingControl:
    """Records the ladder against the fire on one list: the hook appends
    "fired", `terminate` appends "ladder"."""

    def __init__(self, order):
        self.order = order

    def terminate(self, soft, **kw):
        self.order.append("ladder")
        return "soft"


def _fire_and_join(control, hook, tmp_path):
    """Drive one decided flip through a real FlipMonitor with `hook` wired
    through the constructor, and join the thread so the ladder has finished
    before anything is asserted. `status_probe` makes the boundary wait
    return at once; the loop is the same settle `test_monitor_arm_wait_
    terminate` uses, because `stop()` landing before the thread wakes from
    its arm would cancel the flip instead of firing it."""
    m = FlipMonitor(control, [b"\x04"], transcript=None,
                    sentinel=tmp_path / "s.turn",
                    status_probe=lambda: "waiting",
                    on_flip_decided=hook)
    m.start()
    m.flip_pressed()
    deadline = time.time() + 3
    while not m.flip_requested and time.time() < deadline:
        time.sleep(0.05)
    m.stop()
    return m


def test_monitor_fires_the_flip_hook_before_the_ladder(tmp_path):
    # The hook is where the incoming harness is spawned, and the whole point
    # of firing it from here is that the boot overlaps the outgoing harness's
    # teardown. Run after `control.terminate` it would still spawn, still
    # hand over, still pass every runner test — and serialize precisely what
    # this pipelines. So the order is the contract.
    order = []
    m = _fire_and_join(_OrderingControl(order), lambda: order.append("fired"),
                       tmp_path)
    assert order == ["fired", "ladder"]
    assert m.flip_requested is True
    assert m.how == "soft"          # the ladder's answer still lands


def test_monitor_survives_a_raising_flip_hook(tmp_path):
    # A hook that raises costs a cold flip and nothing else. Without the
    # swallow the exception kills this thread mid-flip: `flip_requested` is
    # already True, so the runner still reports a flip and the tests above
    # still pass, but the ladder never runs and the harness the user just
    # pressed Ctrl-] in is never terminated. The ladder's survival is the
    # assertion, not a warning.
    order = []

    def boom():
        raise OSError("spawn failed")

    m = _fire_and_join(_OrderingControl(order), boom, tmp_path)
    assert order == ["ladder"]
    assert m.flip_requested is True
    assert m.how == "soft"


def test_monitor_toggle_cancels(tmp_path):
    t, s = tmp_path / "t.jsonl", tmp_path / "s.turn"
    _touch(t, time.time())           # mid-turn: monitor will block
    _touch(s, time.time() - 30)
    control = _StubControl()
    m = FlipMonitor(control, [b"\x04"], transcript=t, sentinel=s)
    m.start()
    m.flip_pressed()
    time.sleep(0.1)
    assert m.armed() is True
    m.flip_pressed()                 # toggle: cancel
    time.sleep(0.3)
    assert m.armed() is False
    assert m.flip_requested is False
    m.stop()
    assert control.calls == []


def test_monitor_stop_unblocks_cleanly(tmp_path):
    m = FlipMonitor(_StubControl(), [b"\x04"], transcript=None,
                    sentinel=tmp_path / "s.turn")
    m.start()
    m.stop()                         # never armed: must not hang or fire
    assert m.flip_requested is False


def test_monitor_quiesce_defaults_by_mode(tmp_path):
    s = tmp_path / "s.turn"
    unwired = FlipMonitor(_StubControl(), [], None, s)
    wired = FlipMonitor(_StubControl(), [], None, s, marker_wired=True)
    override = FlipMonitor(_StubControl(), [], None, s, marker_wired=True,
                           quiesce=1.5)
    assert unwired.quiesce == 2.0        # marker-less fallback
    assert wired.quiesce == 120.0        # missed-marker valve, not turn pacing
    assert override.quiesce == 1.5       # explicit injection wins
    # The valve has to outlast the longest silence a real turn can produce (a
    # full test suite between the tool_use and tool_result appends), because
    # firing early kills that turn while firing late only costs a wait.
    assert wired.quiesce > 60.0


def test_monitor_passes_mode_through_to_wait(tmp_path, monkeypatch):
    seen = {}

    def fake_wait(transcript, sentinel, cancelled, quiesce=None, poll=0.2,
                  marker_wired=False, provider=None, status_probe=None):
        seen.update(quiesce=quiesce, poll=poll, marker_wired=marker_wired,
                    provider_reads=provider())
        return True

    monkeypatch.setattr(runner, "wait_until_safe", fake_wait)
    control = _StubControl()
    m = FlipMonitor(control, [b"\x04"], transcript=None,
                    sentinel=tmp_path / "s.turn", marker_wired=True,
                    poll=0.05)
    m.start()
    m.flip_pressed()
    deadline = time.time() + 3
    while not m.flip_requested and time.time() < deadline:
        time.sleep(0.05)
    m.stop()
    assert m.flip_requested is True
    assert seen == {"quiesce": 120.0, "poll": 0.05, "marker_wired": True,
                    "provider_reads": None}


# -- the runner wiring the frame ------------------------------------------


class _DeadChild:
    """Stands in for the pty child a real run_in_pty attaches; the
    termination ladder reads it as already gone ("dead") without waiting out
    the attach timeout."""

    def isalive(self):
        return False


def test_runner_passes_frame_and_control(env_factory, monkeypatch):
    env = env_factory(active="claude")
    seen = {}

    def fake_run_in_pty(argv, cwd=None, frame=None, control=None, child=None):
        seen.update(frame=frame, control=control)
        return 0

    monkeypatch.setattr(runner, "run_in_pty", fake_run_in_pty)
    r = runner.InteractiveRunner(env.session, lambda st, se, so, tg: _Sink())
    code = r.run()
    assert code == 0
    assert seen["control"] is not None
    frame = seen["frame"]
    assert frame is not None
    assert frame.flip_byte == 0x1D
    assert frame.key_label == "^]"
    assert frame.active == "claude" and frame.others == ["codex"]
    assert r.flip_requested is False


def test_runner_labels_a_rebound_flip_key_for_the_bar(env_factory, monkeypatch):
    # [frame] flip_key = "ctrl-t" must reach the bar as "^T", not just as the
    # byte the detector watches.
    env = env_factory(active="claude")
    (paths.tandem_home() / "config.toml").write_text(
        '[frame]\nflip_key = "ctrl-t"\n'
    )
    seen = {}
    monkeypatch.setattr(
        runner, "run_in_pty",
        lambda argv, cwd=None, frame=None, control=None, child=None:
            seen.update(frame=frame) or 0,
    )
    runner.InteractiveRunner(env.session, lambda st, se, so, tg: _Sink()).run()
    assert seen["frame"].flip_byte == 0x14
    assert seen["frame"].key_label == "^T"


def test_key_label_spells_control_bytes_with_a_caret():
    assert runner._key_label(0x1D) == "^]"
    assert runner._key_label(0x14) == "^T"
    assert runner._key_label(0x01) == "^A"
    assert runner._key_label(0x7F) == "0x7f"   # not a control byte: fallback


def test_runner_reports_flip_requested(env_factory, monkeypatch):
    env = env_factory(active="claude")
    sentinel = paths.tandem_home() / "tmp" / f"{env.session.tandem_id}-claude.turn"

    def fake_run_in_pty(argv, cwd=None, frame=None, control=None, child=None):
        control.attach(_DeadChild())
        frame.on_flip()  # user pressed the keybind
        deadline = time.time() + 3
        while not frame.armed() and time.time() < deadline:
            time.sleep(0.02)
        sentinel.touch()  # turn-complete marker: the wait releases
        # the monitor's ladder finds no real child: control.terminate
        # returns "dead", flip_requested still set
        deadline = time.time() + 3
        while frame.armed() and time.time() < deadline:
            time.sleep(0.02)
        return 0

    monkeypatch.setattr(runner, "run_in_pty", fake_run_in_pty)
    r = runner.InteractiveRunner(env.session, lambda st, se, so, tg: _Sink())
    r.run()
    assert r.flip_requested is True


def test_runner_writes_bar_drop_marker(env_factory, monkeypatch, capsys):
    env = env_factory(active="claude")

    def fake_run_in_pty(argv, cwd=None, frame=None, control=None, child=None):
        frame.bar_dropped = True
        return 0

    monkeypatch.setattr(runner, "run_in_pty", fake_run_in_pty)
    r = runner.InteractiveRunner(env.session, lambda st, se, so, tg: _Sink())
    r.run()
    marker = paths.tandem_home() / "tmp" / f"{env.session.tandem_id}-bar-dropped"
    assert marker.exists()
    # A dropped bar is a note, not a sync failure: labelling it "sync error"
    # sends the user hunting a transcript divergence that never happened.
    out = capsys.readouterr().out
    assert "status bar disabled for this session" in out
    assert "sync error" not in out
    assert out.count("status bar disabled for this session") == 1  # no dupes
    assert r.reports == [
        "tandem: status bar disabled for this session (terminal conflict);"
        " set [frame] bar = false to silence"
    ]


def test_runner_holds_its_reports_back_for_a_flip(env_factory, monkeypatch, capsys):
    # A flip clears the screen a moment after run() returns, so anything
    # printed here is wiped before the user can read it. Collect, don't print
    # — the flip loop reprints onto the fresh screen.
    env = env_factory(active="claude")
    sentinel = paths.tandem_home() / "tmp" / f"{env.session.tandem_id}-claude.turn"

    def fake_run_in_pty(argv, cwd=None, frame=None, control=None, child=None):
        control.attach(_DeadChild())
        frame.bar_dropped = True
        frame.on_flip()
        deadline = time.time() + 3
        while not frame.armed() and time.time() < deadline:
            time.sleep(0.02)
        sentinel.touch()
        deadline = time.time() + 3
        while frame.armed() and time.time() < deadline:
            time.sleep(0.02)
        return 0

    monkeypatch.setattr(runner, "run_in_pty", fake_run_in_pty)
    r = runner.InteractiveRunner(env.session, lambda st, se, so, tg: _Sink())
    r.run()
    assert r.flip_requested is True
    assert any("status bar disabled" in line for line in r.reports)
    assert "status bar disabled" not in capsys.readouterr().out


def _run_capturing_monitor(env, monkeypatch):
    made = {}
    real = runner.FlipMonitor

    def capture(*a, **kw):
        made["monitor"] = real(*a, **kw)
        return made["monitor"]

    monkeypatch.setattr(runner, "FlipMonitor", capture)
    monkeypatch.setattr(
        runner, "run_in_pty",
        lambda argv, cwd=None, frame=None, control=None, child=None: 0,
    )
    runner.InteractiveRunner(env.session, lambda st, se, so, tg: _Sink()).run()
    return made["monitor"]


def test_marker_wired_derived_from_the_argv_hook_extras(env_factory, monkeypatch):
    # The hook extras that went into argv decide the mode, and they are read
    # once: hook_argv_extra re-reads config per call, so a second call could
    # disagree with what was actually launched.
    from tandem.harness.claude_code import ClaudeCodeAdapter

    env = env_factory(active="claude")
    calls = []
    real_extra = ClaudeCodeAdapter.hook_argv_extra

    def counting(self, sentinel):
        calls.append(sentinel)
        return real_extra(self, sentinel)

    monkeypatch.setattr(ClaudeCodeAdapter, "hook_argv_extra", counting)
    monitor = _run_capturing_monitor(env, monkeypatch)
    assert monitor.marker_wired is True
    assert len(calls) == 1


def test_marker_wired_false_when_adapter_injects_no_hook(env_factory, monkeypatch):
    # The marker-less shape: the adapter declined to wire a hook (codex
    # refusing to clobber a user notify), so quiescence is the only boundary.
    from tandem.harness.claude_code import ClaudeCodeAdapter

    env = env_factory(active="claude")
    monkeypatch.setattr(ClaudeCodeAdapter, "hook_argv_extra", lambda self, s: [])
    monitor = _run_capturing_monitor(env, monkeypatch)
    assert monitor.marker_wired is False


# ---- claude session-status probe -------------------------------------------


def _registry(tmp_path, monkeypatch, entries):
    """Fake ~/.claude/sessions with the given {filename: dict-or-raw} files."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / ".claude"))
    d = tmp_path / ".claude" / "sessions"
    d.mkdir(parents=True)
    for name, entry in entries.items():
        text = entry if isinstance(entry, str) else json.dumps(entry)
        (d / name).write_text(text)
    return d


_SID = "11111111-1111-4111-8111-111111111111"


def _claude_adapter():
    from tandem.harness import get_adapter
    return get_adapter("claude")


def test_pid_alive_own_pid():
    from tandem.harness.claude_code import _pid_alive
    assert _pid_alive(os.getpid()) is True


def test_session_status_reads_busy_and_waiting(tmp_path, monkeypatch):
    me = os.getpid()
    _registry(tmp_path, monkeypatch, {
        f"{me}.json": {"pid": me, "sessionId": _SID, "status": "busy"},
    })
    assert _claude_adapter().session_status(_SID) == "busy"
    _registry(tmp_path.joinpath("b"), monkeypatch, {
        f"{me}.json": {"pid": me, "sessionId": _SID, "status": "waiting",
                       "waitingFor": "input needed"},
    })
    assert _claude_adapter().session_status(_SID) == "waiting"


def test_session_status_none_when_no_match(tmp_path, monkeypatch):
    me = os.getpid()
    _registry(tmp_path, monkeypatch, {
        f"{me}.json": {"pid": me, "sessionId": "someone-else", "status": "busy"},
    })
    assert _claude_adapter().session_status(_SID) is None


def test_session_status_none_when_dir_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / ".claude"))
    assert _claude_adapter().session_status(_SID) is None


def test_session_status_skips_stale_dead_pid_entry(tmp_path, monkeypatch):
    # A crashed run of this same resumed session leaves a dead-pid file
    # frozen at "busy"; the live entry must win.
    from tandem.harness import claude_code
    me = os.getpid()
    _registry(tmp_path, monkeypatch, {
        "99999.json": {"pid": 99999, "sessionId": _SID, "status": "busy"},
        f"{me}.json": {"pid": me, "sessionId": _SID, "status": "waiting"},
    })
    monkeypatch.setattr(claude_code, "_pid_alive", lambda pid: pid == me)
    assert claude_code.ClaudeCodeAdapter().session_status(_SID) == "waiting"
    # dead-only: no live entry at all reads as no answer
    monkeypatch.setattr(claude_code, "_pid_alive", lambda pid: False)
    assert claude_code.ClaudeCodeAdapter().session_status(_SID) is None


def test_session_status_busy_wins_among_live_matches(tmp_path, monkeypatch):
    from tandem.harness import claude_code
    me = os.getpid()
    _registry(tmp_path, monkeypatch, {
        "11.json": {"pid": 11, "sessionId": _SID, "status": "waiting"},
        "22.json": {"pid": 22, "sessionId": _SID, "status": "busy"},
    })
    monkeypatch.setattr(claude_code, "_pid_alive", lambda pid: True)
    assert claude_code.ClaudeCodeAdapter().session_status(_SID) == "busy"


def test_session_status_tolerates_garbage_files(tmp_path, monkeypatch):
    me = os.getpid()
    _registry(tmp_path, monkeypatch, {
        "junk.json": "not json{",
        "list.json": '["not", "a", "dict"]',
        "nopid.json": {"sessionId": _SID, "status": "busy"},
        f"{me}.json": {"pid": me, "sessionId": _SID, "status": "waiting"},
    })
    assert _claude_adapter().session_status(_SID) == "waiting"


# ---- status probe replaces the mtime rules ---------------------------------


def test_wait_probe_waiting_overrides_busy_mtimes(tmp_path):
    # transcript newer than sentinel: the mtime rules read mid-turn and
    # would hold for the 120s valve. The probe says the session is at its
    # prompt, and the probe is the whole test now.
    t, s = tmp_path / "t.jsonl", tmp_path / "s.turn"
    _touch(t, time.time())
    _touch(s, time.time() - 30)
    assert wait_until_safe(t, s, cancelled=lambda: False, marker_wired=True,
                           status_probe=lambda: "waiting") is True


def test_wait_probe_no_answer_flips_eagerly(tmp_path):
    # single tier by spec: registry missing/unreadable -> flip now.
    t, s = tmp_path / "t.jsonl", tmp_path / "s.turn"
    _touch(t, time.time())
    _touch(s, time.time() - 30)
    assert wait_until_safe(t, s, cancelled=lambda: False, marker_wired=True,
                           status_probe=lambda: None) is True


def test_wait_probe_busy_blocks_then_releases(tmp_path):
    t, s = tmp_path / "t.jsonl", tmp_path / "s.turn"
    _touch(s, time.time())  # mtime rules would say idle (sentinel newest)
    _touch(t, time.time() - 30)
    state = {"status": "busy"}
    result = {}
    done = threading.Event()

    def wait():
        result["ok"] = wait_until_safe(t, s, cancelled=lambda: False,
                                       marker_wired=True, poll=0.05,
                                       status_probe=lambda: state["status"])
        done.set()

    threading.Thread(target=wait, daemon=True).start()
    assert not done.wait(timeout=0.4)   # busy verdict outranks idle mtimes
    state["status"] = "waiting"
    assert done.wait(timeout=3)
    assert result["ok"] is True


def test_wait_probe_busy_suppresses_valve(tmp_path):
    # A long-silent tool call: transcript ancient, quiesce tiny — the old
    # valve would fire and kill the live turn. The probe's "busy" holds.
    t, s = tmp_path / "t.jsonl", tmp_path / "s.turn"
    _touch(t, time.time() - 3600)
    _touch(s, time.time() - 7200)
    state = {"status": "busy"}
    result = {}
    done = threading.Event()

    def wait():
        result["ok"] = wait_until_safe(t, s, cancelled=lambda: False,
                                       marker_wired=True, quiesce=0.1,
                                       poll=0.05,
                                       status_probe=lambda: state["status"])
        done.set()

    threading.Thread(target=wait, daemon=True).start()
    assert not done.wait(timeout=0.5)   # outlives quiesce: no valve
    state["status"] = "waiting"
    assert done.wait(timeout=3)
    assert result["ok"] is True


def test_wait_probe_busy_cancel_honored(tmp_path):
    t, s = tmp_path / "t.jsonl", tmp_path / "s.turn"
    _touch(t, time.time())
    cancel = threading.Event()
    result = {}
    done = threading.Event()

    def wait():
        result["ok"] = wait_until_safe(t, s, cancelled=cancel.is_set,
                                       marker_wired=True, poll=0.05,
                                       status_probe=lambda: "busy")
        done.set()

    threading.Thread(target=wait, daemon=True).start()
    assert not done.wait(timeout=0.3)
    cancel.set()
    assert done.wait(timeout=3)
    assert result["ok"] is False


def test_monitor_probe_waiting_fires_immediately(tmp_path):
    # End-to-end through FlipMonitor: mtimes scream mid-turn, probe says
    # waiting -> the ladder runs. Mirrors test_monitor_arm_wait_terminate.
    t, s = tmp_path / "t.jsonl", tmp_path / "s.turn"
    _touch(t, time.time())
    control = _StubControl()
    m = FlipMonitor(control, [b"\x04"], transcript=t, sentinel=s,
                    marker_wired=True, poll=0.05,
                    status_probe=lambda: "waiting")
    m.start()
    m.flip_pressed()
    deadline = time.time() + 3
    while not m.flip_requested and time.time() < deadline:
        time.sleep(0.05)
    m.stop()
    assert m.flip_requested is True
    assert m.how == "soft"
    assert control.calls == [[b"\x04"]]


def test_runner_wires_status_probe_for_claude(env_factory, monkeypatch):
    env = env_factory(active="claude")
    made = {}
    real = runner.FlipMonitor

    def capture(*a, **kw):
        made["kw"] = kw
        return real(*a, **kw)

    monkeypatch.setattr(runner, "FlipMonitor", capture)
    monkeypatch.setattr(
        runner, "run_in_pty",
        lambda argv, cwd=None, frame=None, control=None, child=None: 0,
    )
    runner.InteractiveRunner(env.session, lambda st, se, so, tg: _Sink()).run()
    probe = made["kw"]["status_probe"]
    assert probe is not None
    # the probe closes over the claude sid: feed the registry and ask it
    me = os.getpid()
    d = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{me}.json").write_text(json.dumps(
        {"pid": me, "sessionId": env.session.native_id("claude"),
         "status": "busy"}))
    assert probe() == "busy"


def test_runner_wires_no_probe_for_codex(env_factory, monkeypatch):
    env = env_factory(active="codex")
    made = {}
    real = runner.FlipMonitor

    def capture(*a, **kw):
        made["kw"] = kw
        return real(*a, **kw)

    monkeypatch.setattr(runner, "FlipMonitor", capture)
    monkeypatch.setattr(
        runner, "run_in_pty",
        lambda argv, cwd=None, frame=None, control=None, child=None: 0,
    )
    runner.InteractiveRunner(env.session, lambda st, se, so, tg: _Sink()).run()
    assert made["kw"]["status_probe"] is None


def test_session_status_rejects_nonpositive_and_bool_pids(tmp_path, monkeypatch):
    # os.kill(0,0)/os.kill(-N,0) probe process groups and would read
    # "alive"; with busy-wins and no valve a pid-0 busy entry pins the
    # flip forever. The guard drops them before _pid_alive runs.
    _registry(tmp_path, monkeypatch, {
        "zero.json": {"pid": 0, "sessionId": _SID, "status": "busy"},
        "neg.json": {"pid": -1, "sessionId": _SID, "status": "busy"},
        "bool.json": {"pid": True, "sessionId": _SID, "status": "busy"},
    })
    assert _claude_adapter().session_status(_SID) is None


def test_runner_probe_swallows_raising_session_status(env_factory, monkeypatch):
    # OverflowError from os.kill on an absurd pid is not an OSError; a
    # probe that raises would kill the flip thread. The wiring maps any
    # escape to None (single tier: no answer -> flippable).
    env = env_factory(active="claude")
    made = {}
    real = runner.FlipMonitor

    def capture(*a, **kw):
        made["kw"] = kw
        return real(*a, **kw)

    monkeypatch.setattr(runner, "FlipMonitor", capture)
    monkeypatch.setattr(
        runner, "run_in_pty",
        lambda argv, cwd=None, frame=None, control=None, child=None: 0,
    )
    runner.InteractiveRunner(env.session, lambda st, se, so, tg: _Sink()).run()
    from tandem.harness.claude_code import ClaudeCodeAdapter
    monkeypatch.setattr(
        ClaudeCodeAdapter, "session_status",
        lambda self, sid: (_ for _ in ()).throw(OverflowError()),
    )
    assert made["kw"]["status_probe"]() is None


# -- the fire-at-flip warm spawn ------------------------------------------


class _StdinWithFileno:
    """Stand-in for the terminal the fire measures. pytest replaces
    `sys.stdin` with a pseudofile whose `fileno()` raises, so a fire that
    reads the window size would blow up before it ever spawned. In
    production the gate the fire checks first — `_stdin_tty()` — is exactly
    what guarantees the fileno is there, so a test that forces that gate
    open has to supply the fileno too."""

    def fileno(self):
        return 0


class _QuietWatcher:
    """No native FSEvents thread in tests whose runner exits immediately."""

    def watch(self, path):
        pass

    def start(self):
        pass

    def wait(self):
        time.sleep(0.01)

    def stop(self):
        pass


def _flip_driver(env):
    """The file's established flip-driving `run_in_pty` stand-in: attach a
    dead child (the ladder reads it as already gone), press the keybind,
    touch the turn-complete marker, then hold until the monitor has actually
    decided the flip — `armed()` goes False the moment `flip_requested`
    lands. Without that settle a `run_in_pty` that returns first would race
    `monitor.stop()` and the flip would never fire. The fire itself finishes
    under `stop()`'s join, which is what the runner's finally reads the slot
    after."""
    sentinel = paths.tandem_home() / "tmp" / \
        f"{env.session.tandem_id}-{env.session.active}.turn"

    def fake_run_in_pty(argv, cwd=None, env=None, frame=None, control=None,
                        child=None):
        control.attach(_DeadChild())
        frame.on_flip()                      # arm the flip
        deadline = time.time() + 3
        while not frame.armed() and time.time() < deadline:
            time.sleep(0.02)
        # the sentinel touch satisfies the boundary wait; then wait for the
        # monitor to fire and run its (stubbed-fast) ladder
        sentinel.touch()
        deadline = time.time() + 3
        while frame.armed() and time.time() < deadline:
            time.sleep(0.02)
        return 0

    return fake_run_in_pty


def _drive_flip(monkeypatch, env, *, warm_cfg=True, tty=True, spawns=None,
                expect_spawn=True):
    """Run a real InteractiveRunner through a driven flip (the file's
    established _DeadChild/on_flip/sentinel recipe), recording fire-time
    spawns. Returns the runner after run() completes."""
    import tandem.runner as runner_mod

    if not warm_cfg:
        (paths.tandem_home() / "config.toml").write_text("[frame]\nwarm = false\n")
    monkeypatch.setattr(runner_mod, "_stdin_tty", lambda: tty)
    monkeypatch.setattr(sys, "stdin", _StdinWithFileno())
    recorded = spawns if spawns is not None else []
    spawn_done = threading.Event()

    class FakeChild:
        def __init__(self, recipe, dims, shadow_size):
            self.recipe = recipe
            self.dims = dims
            self.shadow_size = shadow_size
            self.killed = False

        def alive(self):
            return True

        def kill(self):
            self.killed = True

    def fake_spawn_hidden(recipe, dims, shadow_size):
        child = FakeChild(recipe, dims, shadow_size)
        recorded.append(child)
        spawn_done.set()
        return child

    monkeypatch.setattr(runner_mod, "spawn_hidden", fake_spawn_hidden)
    monkeypatch.setattr(runner_mod, "TranscriptWatcher", _QuietWatcher)
    drive_flip = _flip_driver(env)

    def fake_run_in_pty(*args, **kwargs):
        code = drive_flip(*args, **kwargs)
        if expect_spawn and warm_cfg and tty and env.codex_shadow.exists():
            assert spawn_done.wait(3)
        return code

    monkeypatch.setattr(runner_mod, "run_in_pty", fake_run_in_pty)
    r = runner_mod.InteractiveRunner(env.session, sink_factory=_null_sink)
    r.run()
    return r, recorded


def test_flip_fire_spawns_the_shadow_hidden(env_factory, monkeypatch):
    env = env_factory(active="claude")
    r, spawns = _drive_flip(monkeypatch, env)
    assert r.flip_requested
    assert len(spawns) == 1
    assert spawns[0].recipe.side == "codex"
    assert spawns[0].shadow_size == env.codex_shadow.stat().st_size
    assert r.warm_child is spawns[0]


def test_no_fire_when_config_off(env_factory, monkeypatch):
    env = env_factory(active="claude")
    r, spawns = _drive_flip(monkeypatch, env, warm_cfg=False)
    assert r.flip_requested and spawns == [] and r.warm_child is None


def test_no_fire_without_a_tty(env_factory, monkeypatch):
    env = env_factory(active="claude")
    r, spawns = _drive_flip(monkeypatch, env, tty=False)
    assert r.flip_requested                 # or the gate is never reached
    assert spawns == [] and r.warm_child is None


def test_no_fire_when_shadow_transcript_is_missing(env_factory, monkeypatch):
    env = env_factory(active="claude")
    env.codex_shadow.unlink()               # never fresh-mint codex
    r, spawns = _drive_flip(monkeypatch, env)
    assert r.flip_requested                 # or the gate is never reached
    assert spawns == [] and r.warm_child is None


def test_a_raising_fire_still_flips(env_factory, monkeypatch):
    import tandem.runner as runner_mod
    env = env_factory(active="claude")
    monkeypatch.setattr(runner_mod, "spawn_hidden",
                        lambda *a, **kw: (_ for _ in ()).throw(OSError("boom")))
    monkeypatch.setattr(runner_mod, "TranscriptWatcher", _QuietWatcher)
    monkeypatch.setattr(runner_mod, "_stdin_tty", lambda: True)
    monkeypatch.setattr(sys, "stdin", _StdinWithFileno())
    monkeypatch.setattr(runner_mod, "run_in_pty", _flip_driver(env))
    r = runner_mod.InteractiveRunner(env.session, sink_factory=_null_sink)
    assert r.run() == 0                     # monitor thread survived the raise
    assert r.flip_requested and r.warm_child is None


def test_a_slow_fire_does_not_delay_the_termination_ladder(env_factory,
                                                           monkeypatch):
    import tandem.runner as runner_mod
    env = env_factory(active="claude")
    order = []
    release_spawn = threading.Event()
    spawn_done = threading.Event()

    def slow_spawn(*args, **kwargs):
        order.append("spawn-start")
        release_spawn.wait(timeout=1)
        order.append("spawn-done")
        spawn_done.set()
        return _DeadChild()

    def terminate(self, soft, **kwargs):
        order.append("ladder")
        release_spawn.set()
        return "soft"

    monkeypatch.setattr(runner_mod, "spawn_hidden", slow_spawn)
    monkeypatch.setattr(runner_mod, "TranscriptWatcher", _QuietWatcher)
    monkeypatch.setattr(runner_mod.PtyControl, "terminate", terminate)
    monkeypatch.setattr(runner_mod, "_stdin_tty", lambda: True)
    monkeypatch.setattr(sys, "stdin", _StdinWithFileno())
    monkeypatch.setattr(runner_mod, "run_in_pty", _flip_driver(env))
    r = runner_mod.InteractiveRunner(env.session, sink_factory=_null_sink)
    r.run()

    assert r.flip_requested
    assert spawn_done.wait(3)
    assert order.index("ladder") < order.index("spawn-done")


def test_a_slow_recipe_build_does_not_delay_the_termination_ladder(
        env_factory, monkeypatch):
    # The fire's filesystem setup — the shadow stat, build_launch's config
    # reads, the sentinel mkdir — belongs to the launch worker: run on the
    # monitor thread it would sit between the flip decision and the ladder,
    # delaying the very teardown the fire exists to overlap.
    import tandem.runner as runner_mod
    env = env_factory(active="claude")
    order = []
    release_build = threading.Event()
    build_done = threading.Event()
    real_build = runner_mod.build_launch

    def slow_build(session, side, model=""):
        if side != "codex":
            return real_build(session, side, model)  # the active side's launch
        order.append("build-start")
        release_build.wait(timeout=1)
        order.append("build-done")
        build_done.set()
        return real_build(session, side, model)

    def terminate(self, soft, **kwargs):
        order.append("ladder")
        release_build.set()
        return "soft"

    monkeypatch.setattr(runner_mod, "build_launch", slow_build)
    monkeypatch.setattr(runner_mod, "spawn_hidden",
                        lambda *a, **kw: _DeadChild())
    monkeypatch.setattr(runner_mod, "TranscriptWatcher", _QuietWatcher)
    monkeypatch.setattr(runner_mod.PtyControl, "terminate", terminate)
    monkeypatch.setattr(runner_mod, "_stdin_tty", lambda: True)
    monkeypatch.setattr(sys, "stdin", _StdinWithFileno())
    monkeypatch.setattr(runner_mod, "run_in_pty", _flip_driver(env))
    r = runner_mod.InteractiveRunner(env.session, sink_factory=_null_sink)
    r.run()

    assert r.flip_requested
    assert build_done.wait(3)
    assert order.index("ladder") < order.index("build-done")


def test_no_flip_leaves_no_fire_spawn(env_factory, monkeypatch):
    env = env_factory(active="claude")
    import tandem.runner as runner_mod
    spawns = []
    monkeypatch.setattr(runner_mod, "spawn_hidden",
                        lambda *a, **kw: spawns.append(1))
    monkeypatch.setattr(runner_mod, "TranscriptWatcher", _QuietWatcher)
    monkeypatch.setattr(runner_mod, "_stdin_tty", lambda: True)
    monkeypatch.setattr(runner_mod, "run_in_pty", lambda *a, **kw: 0)
    r = runner_mod.InteractiveRunner(env.session, sink_factory=_null_sink)
    r.run()
    assert not r.flip_requested and spawns == [] and r.warm_child is None


def test_fire_kills_its_child_when_the_slot_is_already_closed(env_factory,
                                                             monkeypatch):
    # The one case monitor.stop()'s join cannot cover: its timeout expires
    # with a spawn still in flight, so the runner's finally reads and closes
    # the slot first and the fire lands after. Driven by calling the fire
    # hook once run() has returned — the slot is then closed for good, which
    # is exactly the state an expired join leaves behind. The child must be
    # killed by the fire itself; nothing else is left to reap it.
    import tandem.runner as runner_mod
    env = env_factory(active="claude")
    monitors = []

    class CapturingMonitor(runner_mod.FlipMonitor):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            monitors.append(self)

    class FakeChild:
        def __init__(self):
            self.killed = False

        def kill(self):
            self.killed = True

    spawns = []

    def fake_spawn_hidden(recipe, dims, shadow_size):
        spawns.append(FakeChild())
        return spawns[-1]

    monkeypatch.setattr(runner_mod, "FlipMonitor", CapturingMonitor)
    monkeypatch.setattr(runner_mod, "spawn_hidden", fake_spawn_hidden)
    monkeypatch.setattr(runner_mod, "TranscriptWatcher", _QuietWatcher)
    monkeypatch.setattr(runner_mod, "_stdin_tty", lambda: True)
    monkeypatch.setattr(sys, "stdin", _StdinWithFileno())
    monkeypatch.setattr(runner_mod, "run_in_pty", lambda *a, **kw: 0)
    r = runner_mod.InteractiveRunner(env.session, sink_factory=_null_sink)
    r.run()
    assert spawns == [] and r.warm_child is None   # nothing fired during the run

    monitors[0].on_flip_decided()                  # the in-flight spawn lands
    deadline = time.time() + 3
    while not (spawns and spawns[0].killed) and time.time() < deadline:
        time.sleep(0.02)
    assert len(spawns) == 1 and spawns[0].killed   # reaped, not stranded
    assert r.warm_child is None                    # and never adopted


def test_runner_adopts_a_live_child(env_factory, monkeypatch):
    import tandem.runner as runner_mod
    from tandem.warm import build_launch
    env = env_factory(active="claude")
    recipe = build_launch(env.session, "claude")
    seen = {}

    class FakeWarmChild:
        def __init__(self):
            self.recipe = recipe
            self.released = False

        def alive(self):
            return True

        def release(self):
            self.released = True
            return "raw-child"

    def fake_run_in_pty(argv, cwd=None, env=None, frame=None, control=None,
                        child=None):
        seen["argv"] = argv
        seen["child"] = child
        return 0

    def no_rebuild(*a, **kw):
        # The recipe is bound once, at spawn time: hook_argv_extra re-reads
        # config per call, so a rebuild here could disagree with the argv the
        # adopted child is already running under.
        raise AssertionError("adoption must not rebuild the launch recipe")

    monkeypatch.setattr(runner_mod, "build_launch", no_rebuild)
    monkeypatch.setattr(runner_mod, "run_in_pty", fake_run_in_pty)
    wc = FakeWarmChild()
    r = runner_mod.InteractiveRunner(env.session, sink_factory=_null_sink,
                                     adopt_child=wc)
    r.run()
    assert wc.released
    assert seen["child"] == "raw-child"
    assert seen["argv"] == recipe.argv     # the recorded recipe, not a rebuild


def test_runner_kills_a_child_that_refuses_to_release(env_factory, monkeypatch):
    # release() returns None when the discard reader still owns the fd. The
    # raw None goes to run_in_pty (a cold spawn), and the WarmChild must be
    # killed: nothing else is left to reap the hidden process.
    import tandem.runner as runner_mod
    from tandem.warm import build_launch
    env = env_factory(active="claude")
    recipe = build_launch(env.session, "claude")
    seen = {}

    class WedgedWarmChild:
        def __init__(self):
            self.recipe = recipe
            self.killed = False

        def alive(self):
            return True

        def release(self):
            return None          # reader refused to join

        def kill(self):
            self.killed = True

    def fake_run_in_pty(argv, cwd=None, env=None, frame=None, control=None,
                        child=None):
        seen["child"] = child
        return 0

    monkeypatch.setattr(runner_mod, "run_in_pty", fake_run_in_pty)
    wc = WedgedWarmChild()
    runner_mod.InteractiveRunner(env.session, sink_factory=_null_sink,
                                 adopt_child=wc).run()
    assert seen["child"] is None    # cold spawn, not a half-owned fd
    assert wc.killed


def test_runner_kills_an_adoptee_it_never_handed_over(env_factory, monkeypatch):
    """Anything raising between "we will adopt this" and the handover leaves
    the hidden child unowned: the flip loop popped it out of its carry to
    build this runner and only ever gets `warm_child` back. So the runner
    reaps it — and, on the other side of the same guard, never reaps one it
    did hand over, which would terminate the harness the user is looking at."""
    import tandem.runner as runner_mod
    from tandem.warm import build_launch
    env = env_factory(active="claude")
    recipe = build_launch(env.session, "claude")

    class Adoptee:
        def __init__(self):
            self.recipe = recipe
            self.released = False
            self.killed = False

        def alive(self):
            return True

        def release(self):
            self.released = True
            return "raw-child"

        def kill(self):
            self.killed = True

    def boom():
        raise ValueError("bad [frame] table in config.toml")

    with monkeypatch.context() as m:
        # a pre-handover step that raises; the config parse is the realistic
        # one, but the guard is not specific to it
        m.setattr(runner_mod, "load_frame_config", boom)
        never = Adoptee()
        with pytest.raises(ValueError):
            runner_mod.InteractiveRunner(env.session, sink_factory=_null_sink,
                                         adopt_child=never).run()
    assert never.killed
    assert not never.released

    monkeypatch.setattr(runner_mod, "run_in_pty", lambda *a, **kw: 0)
    handed = Adoptee()
    runner_mod.InteractiveRunner(env.session, sink_factory=_null_sink,
                                 adopt_child=handed).run()
    assert handed.released
    assert not handed.killed   # it is the running harness now


def test_runner_ignores_a_dead_or_mismatched_adoptee(env_factory, monkeypatch):
    # Neither a dead child nor one warmed for the other side is adoptable:
    # the runner rebuilds its own recipe and spawns cold. It still reaps
    # them — the carry was emptied to build this runner, so a live wrong-side
    # standby dropped here would outlive the session with nobody to kill it.
    import tandem.runner as runner_mod
    from tandem.warm import build_launch
    env = env_factory(active="claude")
    seen = {}

    class Adoptee:
        def __init__(self, side, is_alive):
            self.recipe = build_launch(env.session, side)
            self._alive = is_alive
            self.released = False
            self.killed = False

        def alive(self):
            return self._alive

        def release(self):
            self.released = True
            return "raw-child"

        def kill(self):
            self.killed = True

    def fake_run_in_pty(argv, cwd=None, env=None, frame=None, control=None,
                        child=None):
        seen["child"] = child
        return 0

    monkeypatch.setattr(runner_mod, "run_in_pty", fake_run_in_pty)
    for adoptee in (Adoptee("claude", False), Adoptee("codex", True)):
        seen.clear()   # or the second iteration could pass on stale evidence
        runner_mod.InteractiveRunner(env.session, sink_factory=_null_sink,
                                     adopt_child=adoptee).run()
        assert seen["child"] is None
        assert not adoptee.released
        assert adoptee.killed


def test_a_fired_child_is_not_leaked_when_run_in_pty_raises(env_factory,
                                                            monkeypatch):
    # The fire lands on the monitor thread, so a run_in_pty that explodes
    # after it must still surrender the child: the finally publishes it to
    # `warm_child` (the flip loop fills its carry from that same finally) or
    # a hidden harness outlives the session with nobody left to reap it.
    import tandem.runner as runner_mod
    env = env_factory(active="claude")
    spawned = []

    class FakeChild:
        def __init__(self, shadow_size):
            self.shadow_size = shadow_size
            self.killed = False

        def alive(self):
            return True

        def kill(self):
            self.killed = True

    def fake_spawn_hidden(recipe, dims, shadow_size):
        spawned.append(FakeChild(shadow_size))
        return spawned[-1]

    driver = _flip_driver(env)

    def boom(argv, cwd=None, env=None, frame=None, control=None, child=None):
        driver(argv, cwd=cwd, frame=frame, control=control, child=child)
        raise RuntimeError("pty exploded")

    monkeypatch.setattr(runner_mod, "_stdin_tty", lambda: True)
    monkeypatch.setattr(sys, "stdin", _StdinWithFileno())
    monkeypatch.setattr(runner_mod, "spawn_hidden", fake_spawn_hidden)
    monkeypatch.setattr(runner_mod, "run_in_pty", boom)
    r = runner_mod.InteractiveRunner(env.session, sink_factory=_null_sink)
    with pytest.raises(RuntimeError):
        r.run()
    assert len(spawned) == 1
    assert r.flip_requested
    assert r.warm_child is spawned[0]   # the carry's only reference to it
    assert not spawned[0].killed


def test_tail_thread_drains_all_directions(tmp_path, monkeypatch):
    """One source line lands in BOTH shadows via the runner's TailLoop set."""
    from conftest import Env3, claude_user, write_line
    from tandem.runner import TailLoop
    from tandem.sync import SyncEngine

    env = Env3(tmp_path, monkeypatch)
    write_line(env.claude_shadow, claude_user("fan out"))
    for target in env.session.targets_for("claude"):
        engine = SyncEngine(env.store, env.session, "claude", target)
        loop = TailLoop(env.store, env.session, "claude", target,
                        env.claude_shadow, engine)
        assert loop.drain() >= 1


def test_warm_skips_opencode_target(tmp_path, monkeypatch):
    """fire_warm never spawns when next-in-cycle is opencode (v1 carve-out):
    an opencode TUI booted pre-drain would cache the session pre-drain and
    never show the last turn, so opencode-bound flips run cold."""
    from conftest import Env3

    env = Env3(tmp_path, monkeypatch, active="codex")
    assert env.session.next_active("codex") == "opencode"
    r, spawns = _drive_flip(monkeypatch, env, expect_spawn=False)
    assert spawns == [] and r.warm_child is None


# -- rate limits on the bar ---------------------------------------------------


def _wait_for(pred, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


def test_runner_polls_rate_limits_for_every_participant(env_factory, monkeypatch):
    from tandem import ratelimit
    env = env_factory(active="claude")
    monkeypatch.setitem(ratelimit.DEFAULT_FETCHERS, "claude",
                        lambda: [ratelimit.Window("5h", 4), ratelimit.Window("7d", 41)])
    monkeypatch.setitem(ratelimit.DEFAULT_FETCHERS, "codex",
                        lambda: [ratelimit.Window("7d", 12)])
    seen = {}

    def fake_run_in_pty(argv, cwd=None, frame=None, control=None, child=None):
        # the pump reports the bar drawn; only then does the poller start,
        # publishing on its own thread
        assert not any(frame.limits().values())   # seeded blank, nothing fetched yet
        frame.on_bar(True)
        assert _wait_for(lambda: frame.limits() and "codex" in frame.limits())
        seen["limits"] = dict(frame.limits())
        return 0

    monkeypatch.setattr(runner, "run_in_pty", fake_run_in_pty)
    runner.InteractiveRunner(env.session, lambda st, se, so, tg: _Sink()).run()
    assert seen["limits"] == {"claude": "5h 4% 7d 41%", "codex": "7d 12%"}
    # and it is torn down with the run
    assert not any(t.name == "tandem-ratelimit" and t.is_alive()
                   for t in threading.enumerate())


def test_runner_skips_the_rate_limit_poll_when_disabled(env_factory, monkeypatch):
    from tandem import ratelimit
    env = env_factory(active="claude")
    (paths.tandem_home() / "config.toml").write_text("[frame]\nrate_limits = false\n")
    called = []
    monkeypatch.setitem(ratelimit.DEFAULT_FETCHERS, "claude", lambda: called.append(1))
    monkeypatch.setitem(ratelimit.DEFAULT_FETCHERS, "codex", lambda: called.append(1))
    seen = {}
    monkeypatch.setattr(
        runner, "run_in_pty",
        lambda argv, cwd=None, frame=None, control=None, child=None:
            seen.update(frame=frame) or 0,
    )
    runner.InteractiveRunner(env.session, lambda st, se, so, tg: _Sink()).run()
    assert seen["frame"].limits is None
    assert called == []


def test_runner_skips_the_rate_limit_poll_without_a_bar(env_factory, monkeypatch):
    # nothing to paint the figures on: no network calls either
    from tandem import ratelimit
    env = env_factory(active="claude")
    (paths.tandem_home() / "config.toml").write_text("[frame]\nbar = false\n")
    called = []
    monkeypatch.setitem(ratelimit.DEFAULT_FETCHERS, "claude", lambda: called.append(1))
    monkeypatch.setitem(ratelimit.DEFAULT_FETCHERS, "codex", lambda: called.append(1))
    monkeypatch.setattr(runner, "run_in_pty",
                        lambda argv, cwd=None, frame=None, control=None, child=None: 0)
    runner.InteractiveRunner(env.session, lambda st, se, so, tg: _Sink()).run()
    assert called == []


def test_runner_makes_no_rate_limit_calls_when_the_pump_never_draws_a_bar(
        env_factory, monkeypatch):
    # config says bar, but the pump found no tty / too few rows: no bar, no calls
    from tandem import ratelimit
    env = env_factory(active="claude")
    called = []
    monkeypatch.setitem(ratelimit.DEFAULT_FETCHERS, "claude", lambda: called.append(1))
    monkeypatch.setitem(ratelimit.DEFAULT_FETCHERS, "codex", lambda: called.append(1))

    def fake_run_in_pty(argv, cwd=None, frame=None, control=None, child=None):
        frame.on_bar(False)
        time.sleep(0.2)
        return 0

    monkeypatch.setattr(runner, "run_in_pty", fake_run_in_pty)
    runner.InteractiveRunner(env.session, lambda st, se, so, tg: _Sink()).run()
    assert called == []


def test_runner_halts_the_poll_when_the_bar_drops(env_factory, monkeypatch):
    from tandem import ratelimit
    env = env_factory(active="claude")
    calls = {"n": 0}

    def fetch():
        calls["n"] += 1
        return [ratelimit.Window("7d", 12)]

    monkeypatch.setitem(ratelimit.DEFAULT_FETCHERS, "claude", fetch)
    monkeypatch.setitem(ratelimit.DEFAULT_FETCHERS, "codex", fetch)

    def fake_run_in_pty(argv, cwd=None, frame=None, control=None, child=None):
        frame.on_bar(True)
        assert _wait_for(lambda: calls["n"] >= 2)
        frame.on_bar(False)
        assert _wait_for(lambda: not any(
            t.name == "tandem-ratelimit" and t.is_alive() for t in threading.enumerate()))
        return 0

    monkeypatch.setattr(runner, "run_in_pty", fake_run_in_pty)
    runner.InteractiveRunner(env.session, lambda st, se, so, tg: _Sink()).run()


def test_runner_pokes_the_poller_when_a_response_lands(env_factory, monkeypatch):
    """The tail thread sees the active transcript grow; a change in the usage
    text means a response just landed, so the account figures get refreshed
    ahead of the interval."""
    import functools

    from tandem import ratelimit
    env = env_factory(active="claude")
    calls = {"n": 0}

    def fetch():
        calls["n"] += 1
        return [ratelimit.Window("5h", 4)]

    monkeypatch.setitem(ratelimit.DEFAULT_FETCHERS, "claude", fetch)
    monkeypatch.setitem(ratelimit.DEFAULT_FETCHERS, "codex", lambda: None)
    monkeypatch.setattr(runner, "RateLimitPoller",
                        functools.partial(ratelimit.RateLimitPoller,
                                          interval=3600, min_gap=0))

    def fake_run_in_pty(argv, cwd=None, frame=None, control=None, child=None):
        frame.on_bar(True)
        assert _wait_for(lambda: calls["n"] == 1)
        time.sleep(0.2)
        assert calls["n"] == 1
        with open(env.claude_shadow, "a") as f:
            f.write(json.dumps({
                "type": "assistant", "uuid": "a-1", "sessionId": "x",
                "message": {"id": "m1", "role": "assistant",
                            "content": [{"type": "text", "text": "hi"}],
                            "usage": {"input_tokens": 10, "output_tokens": 5}},
            }) + "\n")
        assert _wait_for(lambda: calls["n"] >= 2, timeout=5)
        return 0

    monkeypatch.setattr(runner, "run_in_pty", fake_run_in_pty)
    runner.InteractiveRunner(env.session, lambda st, se, so, tg: _Sink()).run()


# -- mixed tab on the real threads: mixer, injector, monitor cancel ----------
# (the coordinator's own behavior is driven directly in test_routing.py)


# Longer than the 60 characters the exit notes used to truncate at. Once the
# request is gone the note IS the prompt, so it has to carry all of it.
_LONG_PROMPT = ("rewrite this migration to be idempotent, then run the whole"
                " suite twice and report what changed")
assert len(_LONG_PROMPT) > 60


def _claimed(tandem_id, req):
    """Put a request where the frame's own slot has it: written by the hook
    and already claimed. That is what an injecting run inherits, and what a
    crashed run leaves behind."""
    routefile.write_route(tandem_id, req)
    assert routefile.claim(tandem_id, req.id) is True


def test_runner_defaults_have_no_route_state(env_factory):
    # before `run()` there is no launch, so there is no coordinator: the
    # flip loop's read of it has to survive a run that raised early
    env = env_factory(active="claude")
    r = runner.InteractiveRunner(env.session, sink_factory=None)
    assert r.coordinator is None and r.tabs is None and r.inject is None


def test_runner_notes_an_undelivered_routed_prompt(env_factory, monkeypatch):
    # the injector thread really runs here: the ladder landed in codex while
    # the prompt was routed to claude, so it gives up without writing
    env = env_factory(active="codex")
    req = RouteRequest("claude", "", _LONG_PROMPT, "codex", "→ claude")
    _claimed(env.session.tandem_id, req)

    def fake_run_in_pty(argv, cwd=None, frame=None, control=None, child=None):
        assert _wait_for(lambda: r.coordinator.inject_failed)
        return 0

    monkeypatch.setattr(runner, "TranscriptWatcher", _QuietWatcher)
    monkeypatch.setattr(runner, "run_in_pty", fake_run_in_pty)
    r = runner.InteractiveRunner(env.session, _null_sink, inject=req)
    r.run()
    # quoted whole: the route file is the user's only other copy, and it is
    # not a copy they can read
    assert any("routed prompt was not delivered" in line
               and _LONG_PROMPT in line
               and "re-type it in claude" in line for line in r.reports)
    assert routefile.read_claimed(env.session.tandem_id) is not None


@pytest.fixture
def plugin_installed(monkeypatch):
    """`routing_ok` is the adapter's static capability AND the plugin being
    registered on disk; under `env_factory`'s tmp homes no registry exists,
    so the honest reading is "not installed". Tests about the tab plumbing
    pin the on-disk half True — only the two detectors are stubbed, so the
    real `hook_available` dispatch still runs in the runner."""
    from tandem import plugin_setup

    monkeypatch.setattr(plugin_setup, "is_plugin_installed", lambda: True)
    monkeypatch.setattr(plugin_setup, "is_plugin_installed_codex", lambda: True)


def test_frame_gets_the_tab_snapshot_and_the_press_glue(env_factory, monkeypatch,
                                                        plugin_installed):
    env = env_factory(active="claude")
    tabs = TabState(env.session.participants, tab="mixed", focus="claude")
    seen = {}

    def fake_run_in_pty(argv, cwd=None, frame=None, control=None, child=None):
        seen["mode"] = frame.mode()
        frame.on_flip()          # mixed -> harness with focus already here
        seen["after"] = frame.mode()
        return 0

    monkeypatch.setattr(runner, "TranscriptWatcher", _QuietWatcher)
    monkeypatch.setattr(runner, "run_in_pty", fake_run_in_pty)
    r = runner.InteractiveRunner(env.session, _null_sink, tabs=tabs)
    r.run()
    assert seen["mode"] == {"tab": "mixed", "focus": "claude",
                            "routing_ok": True}
    # a bar move: the tab changed and no flip was ever armed
    assert seen["after"] == {"tab": "harness", "focus": "", "routing_ok": True}
    assert r.flip_requested is False and tabs.pending is None


def test_a_flip_move_still_arms_the_monitor(env_factory, monkeypatch):
    env = env_factory(active="claude")
    tabs = TabState(env.session.participants)      # harness tab
    monkeypatch.setattr(runner, "TranscriptWatcher", _QuietWatcher)
    monkeypatch.setattr(runner, "run_in_pty", _flip_driver(env))
    r = runner.InteractiveRunner(env.session, _null_sink, tabs=tabs)
    r.run()
    assert r.flip_requested is True
    assert tabs.pending_target() == "codex"


def test_mixer_publishes_the_frame_state(env_factory, monkeypatch,
                                         plugin_installed):
    env = env_factory(active="claude")
    tabs = TabState(env.session.participants, tab="mixed", focus="claude")

    def fake_run_in_pty(argv, cwd=None, frame=None, control=None, child=None):
        assert _wait_for(
            lambda: routefile.read_frame_state(env.session.tandem_id) is not None)
        return 0

    monkeypatch.setattr(runner, "TranscriptWatcher", _QuietWatcher)
    monkeypatch.setattr(runner, "run_in_pty", fake_run_in_pty)
    runner.InteractiveRunner(env.session, _null_sink, tabs=tabs).run()
    assert routefile.read_frame_state(env.session.tandem_id) == {
        "tab": "mixed", "focus": "claude", "routing_ok": True}
    # and the thread is finished, not merely told to stop: a tick landing
    # after run() could arm a flip nobody is left to take
    assert not any(t.name == "tandem-mixer" and t.is_alive()
                   for t in threading.enumerate())


def test_frame_state_says_routing_is_off_without_the_plugin(env_factory,
                                                            monkeypatch):
    """A run started before `tandem plugin install` must say so. The hook
    is what turns `@codex …` into a route, so with no plugin registered the
    bar shows `(no @-routing)` and the frame file tells the same story —
    better than silently eating the prefix as literal prompt text."""
    env = env_factory(active="claude")      # tmp homes: no plugin registry
    tabs = TabState(env.session.participants, tab="mixed", focus="claude")

    def fake_run_in_pty(argv, cwd=None, frame=None, control=None, child=None):
        assert _wait_for(
            lambda: routefile.read_frame_state(env.session.tandem_id) is not None)
        assert frame.mode()["routing_ok"] is False
        return 0

    monkeypatch.setattr(runner, "TranscriptWatcher", _QuietWatcher)
    monkeypatch.setattr(runner, "run_in_pty", fake_run_in_pty)
    runner.InteractiveRunner(env.session, _null_sink, tabs=tabs).run()
    assert routefile.read_frame_state(env.session.tandem_id) == {
        "tab": "mixed", "focus": "claude", "routing_ok": False}


def test_mixer_startup_surfaces_and_clears_a_leftover_claim(env_factory,
                                                            monkeypatch):
    env = env_factory(active="claude")
    _claimed(env.session.tandem_id, RouteRequest(
        "codex", "", _LONG_PROMPT, "claude", "→ codex"))
    path = routefile._claimed_path(env.session.tandem_id)
    tabs = TabState(env.session.participants, tab="mixed", focus="claude")

    def fake_run_in_pty(argv, cwd=None, frame=None, control=None, child=None):
        assert _wait_for(lambda: not path.exists())
        return 0

    monkeypatch.setattr(runner, "TranscriptWatcher", _QuietWatcher)
    monkeypatch.setattr(runner, "run_in_pty", fake_run_in_pty)
    r = runner.InteractiveRunner(env.session, _null_sink, tabs=tabs)
    r.run()
    assert any("never delivered" in line and _LONG_PROMPT in line
               for line in r.reports)


def test_mixer_startup_surfaces_and_clears_a_leftover_pending_route(
        env_factory, monkeypatch):
    """A leftover in the *pending* slot is as lost as a claimed one, and
    reachable without crashing anything: route while a user flip is already
    armed, the mixer refuses the claim, the flip proceeds, and this sweep is
    what eats the request. The delete is unconditional — the note is what
    stops it being silent."""
    env = env_factory(active="claude")
    routefile.write_route(env.session.tandem_id, RouteRequest(
        "codex", "", _LONG_PROMPT, "claude", "→ codex"))
    tabs = TabState(env.session.participants, tab="mixed", focus="claude")

    def fake_run_in_pty(argv, cwd=None, frame=None, control=None, child=None):
        assert _wait_for(
            lambda: not routefile._pending_path(env.session.tandem_id).exists())
        return 0

    monkeypatch.setattr(runner, "TranscriptWatcher", _QuietWatcher)
    monkeypatch.setattr(runner, "run_in_pty", fake_run_in_pty)
    r = runner.InteractiveRunner(env.session, _null_sink, tabs=tabs)
    r.run()
    assert any("never picked up" in line and _LONG_PROMPT in line
               and "codex" in line for line in r.reports)


def test_mixer_startup_surfaces_an_old_leftover_too(env_factory, monkeypatch):
    """Age is not a reason to keep quiet. A prompt typed before lunch is
    still a prompt the user typed, and the sweep deletes it either way — so
    an age cut-off could only ever turn a note into a silent loss."""
    env = env_factory(active="claude")
    routefile.write_route(env.session.tandem_id, RouteRequest(
        "codex", "", _LONG_PROMPT, "claude", "→ codex"))
    path = routefile._pending_path(env.session.tandem_id)
    old = time.time() - 86400
    os.utime(path, (old, old))
    tabs = TabState(env.session.participants, tab="mixed", focus="claude")

    def fake_run_in_pty(argv, cwd=None, frame=None, control=None, child=None):
        assert _wait_for(lambda: not path.exists())
        return 0

    monkeypatch.setattr(runner, "TranscriptWatcher", _QuietWatcher)
    monkeypatch.setattr(runner, "run_in_pty", fake_run_in_pty)
    r = runner.InteractiveRunner(env.session, _null_sink, tabs=tabs)
    r.run()
    assert not path.exists()
    assert any("never picked up" in line and _LONG_PROMPT in line
               for line in r.reports)


def test_mixer_startup_leaves_the_route_this_run_is_delivering(env_factory,
                                                               monkeypatch):
    env = env_factory(active="codex")
    req = RouteRequest("codex", "", "do it", "claude", "→ codex")
    _claimed(env.session.tandem_id, req)
    tabs = TabState(env.session.participants, tab="mixed", focus="codex")

    def fake_run_in_pty(argv, cwd=None, frame=None, control=None, child=None):
        assert _wait_for(
            lambda: routefile.read_frame_state(env.session.tandem_id) is not None)
        return 0

    monkeypatch.setattr(runner, "TranscriptWatcher", _QuietWatcher)
    monkeypatch.setattr(runner, "run_in_pty", fake_run_in_pty)
    # `inject` does start a real injector thread — it sleeps out its 2.5s
    # settle past the end of this test and its write then fails (nothing is
    # attached to the faked pty), which is harmless: a failed write never
    # touches the file. What is under test is the mixer's startup sweep,
    # which must keep its hands off the route this run is there to deliver.
    r = runner.InteractiveRunner(env.session, _null_sink, tabs=tabs, inject=req)
    r.run()
    assert routefile.read_claimed(env.session.tandem_id) is not None


def test_no_warm_fire_when_the_flip_is_routed(env_factory, monkeypatch):
    # A routed flip spawns cold: the standby would be for the wrong side (the
    # cycle's next, not the route's target) or the wrong model.
    import tandem.runner as runner_mod

    env = env_factory(active="claude")
    spawns = []
    monkeypatch.setattr(runner_mod, "spawn_hidden",
                        lambda *a: spawns.append(a))
    monkeypatch.setattr(runner_mod, "TranscriptWatcher", _QuietWatcher)
    monkeypatch.setattr(runner_mod, "_stdin_tty", lambda: True)
    monkeypatch.setattr(sys, "stdin", _StdinWithFileno())
    drive = _flip_driver(env)
    r = runner_mod.InteractiveRunner(env.session, _null_sink)

    def fake_run_in_pty(*a, **kw):
        # the routed arm is in place before the decision reaches fire_warm
        r.coordinator.route_request = RouteRequest("codex", "", "do it",
                                                   "claude", "→ codex")
        return drive(*a, **kw)

    monkeypatch.setattr(runner_mod, "run_in_pty", fake_run_in_pty)
    r.run()
    assert r.flip_requested is True
    assert spawns == [] and r.warm_child is None


def test_monitor_calls_the_cancel_hook_on_a_cancelled_wait(tmp_path):
    calls = []
    control = _StubControl()
    monitor = FlipMonitor(control, [b"q"], tmp_path / "t.jsonl",
                          tmp_path / "s", quiesce=5.0, poll=0.01)
    monitor.on_wait_cancelled = lambda: calls.append(1)
    (tmp_path / "t.jsonl").write_text("x")     # never idle: the wait blocks
    monitor.start()
    monitor.flip_pressed()                     # arm
    time.sleep(0.05)
    monitor.flip_pressed()                     # toggle off: the wait cancels
    assert _wait_for(lambda: calls == [1])
    assert monitor.flip_requested is False
    monitor.stop()


def test_monitor_survives_a_raising_cancel_hook(tmp_path):
    monitor = FlipMonitor(_StubControl(), [b"q"], tmp_path / "t.jsonl",
                          tmp_path / "s", quiesce=5.0, poll=0.01)
    monitor.on_wait_cancelled = lambda: (_ for _ in ()).throw(OSError("boom"))
    (tmp_path / "t.jsonl").write_text("x")
    monitor.start()
    monitor.flip_pressed()
    time.sleep(0.05)
    monitor.flip_pressed()
    time.sleep(0.05)
    # the thread is still there to serve the next arm
    (tmp_path / "s").touch()
    monitor.flip_pressed()
    assert _wait_for(lambda: monitor.flip_requested)
    monitor.stop()


def test_the_mixer_thread_picks_a_live_route_up_and_a_cancel_undoes_it(
        env_factory, monkeypatch):
    """The whole routed arm end to end on the real threads: the mixer picks
    the request up, claims it and arms the real monitor; the monitor's
    cancel hook then puts everything back."""
    from tandem.harness.claude_code import ClaudeCodeAdapter

    env = env_factory(active="claude")
    # a turn that never ends: the armed monitor waits instead of flipping,
    # which is the state a cancel exists for
    monkeypatch.setattr(ClaudeCodeAdapter, "session_status",
                        lambda self, sid: "busy")
    tabs = TabState(env.session.participants, tab="mixed", focus="claude")
    made = {}
    real = runner.FlipMonitor

    def capture(*a, **kw):
        made["monitor"] = real(*a, **kw)
        return made["monitor"]

    def fake_run_in_pty(argv, cwd=None, frame=None, control=None, child=None):
        # the frame file appearing means the mixer's startup sweep is done,
        # so the route written now is this run's, not a leftover
        assert _wait_for(
            lambda: routefile.read_frame_state(env.session.tandem_id) is not None)
        routefile.write_route(env.session.tandem_id, RouteRequest(
            "codex", "", _LONG_PROMPT, "claude", "→ codex"))
        assert _wait_for(lambda: r.coordinator.route_request is not None)
        assert tabs.pending_target() == "codex"
        assert routefile.read_claimed(env.session.tandem_id) is not None
        assert made["monitor"].armed() is True
        made["monitor"].on_wait_cancelled()      # as the monitor thread does
        return 0

    monkeypatch.setattr(runner, "FlipMonitor", capture)
    monkeypatch.setattr(runner, "TranscriptWatcher", _QuietWatcher)
    monkeypatch.setattr(runner, "run_in_pty", fake_run_in_pty)
    r = runner.InteractiveRunner(env.session, _null_sink, tabs=tabs)
    r.run()
    assert r.coordinator.route_request is None and tabs.pending is None
    assert routefile.read_pending(env.session.tandem_id) is None
    assert any("routed turn cancelled" in line and _LONG_PROMPT in line
               for line in r.reports)


def test_tabs_none_leaves_the_frame_and_the_files_pre_mixed(env_factory,
                                                            monkeypatch):
    env = env_factory(active="claude")
    seen = {}

    def fake_run_in_pty(argv, cwd=None, frame=None, control=None, child=None):
        seen["frame"] = frame
        return 0

    monkeypatch.setattr(runner, "TranscriptWatcher", _QuietWatcher)
    monkeypatch.setattr(runner, "run_in_pty", fake_run_in_pty)
    r = runner.InteractiveRunner(env.session, _null_sink)
    r.run()
    assert seen["frame"].mode is None
    assert not any(t.name == "tandem-mixer" and t.is_alive()
                   for t in threading.enumerate())
    assert routefile.read_frame_state(env.session.tandem_id) is None


def _argv_and_reports(env, monkeypatch, **kw):
    """One run with the pty stubbed out; returns (argv, runner)."""
    calls = {}
    monkeypatch.setattr(runner, "TranscriptWatcher", _QuietWatcher)
    monkeypatch.setattr(
        runner, "run_in_pty",
        lambda argv, cwd=None, **kwargs: calls.update(argv=argv) or 0,
    )
    r = runner.InteractiveRunner(env.session, _null_sink, **kw)
    assert r.run() == 0
    return calls["argv"], r


def test_routed_inject_pins_the_model_at_launch(env_factory, monkeypatch):
    """The runner builds the launch, so a routed turn's model has to reach
    `build_launch` from the inject the flip loop carried in."""
    env = env_factory(active="claude")
    req = RouteRequest("claude", "haiku", "do it", "codex", "→ claude")
    argv, r = _argv_and_reports(env, monkeypatch, inject=req)
    assert argv[argv.index("--model") + 1] == "haiku"
    assert not any("cannot pin a model" in line for line in r.reports)


def test_inject_for_another_target_pins_nothing(env_factory, monkeypatch):
    """The ladder landed somewhere the route never asked for: launch that
    harness the way it would have launched anyway (the injector then keeps
    the prompt instead of typing it in here)."""
    env = env_factory(active="claude")
    req = RouteRequest("codex", "gpt-5.3-codex", "do it", "claude", "→ codex")
    argv, _ = _argv_and_reports(env, monkeypatch, inject=req)
    assert "--model" not in argv


def test_a_model_the_harness_cannot_pin_is_reported(env_factory, monkeypatch):
    """`build_launch` records what was launched, never the intent — so an
    adapter with no launch-time model flag yields recipe.model == "" and the
    user is told the turn is running the harness's default."""
    from tandem.harness.claude_code import ClaudeCodeAdapter

    env = env_factory(active="claude")
    monkeypatch.setattr(ClaudeCodeAdapter, "model_argv", lambda self, m: [])
    req = RouteRequest("claude", "haiku", "do it", "codex", "→ claude")
    argv, r = _argv_and_reports(env, monkeypatch, inject=req)
    assert "--model" not in argv
    assert any("claude cannot pin a model at launch — running its default"
               in line for line in r.reports)
