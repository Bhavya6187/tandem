"""InteractiveRunner: user-configured [harness] args land in the spawned argv."""

import json
import os
import threading
import time
from pathlib import Path

import pytest

from tandem import paths, runner
from tandem.runner import FlipMonitor, wait_until_safe


@pytest.fixture(autouse=True)
def _warm_gate_closed(monkeypatch):
    """No test may boot a hidden harness for real. `pytest -s` on a real
    terminal leaves stdin a tty, which would open the warm gate in every
    runner test here; pin it shut, and let the two tests that exercise the
    gate reopen it themselves."""
    monkeypatch.setattr(runner, "_stdin_tty", lambda: False)


class _Sink:
    def handle(self, line, ctx, cursor): ...

    def close(self): ...


def _null_sink(store, session, source):
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
    assert argv[i + 1] == env.session.claude_session_id
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
    assert argv[i + 1] == env.session.codex_session_id
    assert argv[i + 2] == "--dangerously-bypass-approvals-and-sandbox"
    assert "--should-not-appear" not in argv


def test_codex_fresh_mint_gets_args_after_bare_binary(env_factory, monkeypatch):
    # codex minting its own session id launches as bare `codex`, so the
    # configured args are the first tokens after the binary.
    env = env_factory(active="codex")
    session = env.store.create_session(
        env.cwd, "codex", env.session.claude_session_id, None
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
    assert argv[:3] == ["claude", "--resume", env.session.claude_session_id]
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
        env.cwd, "codex", env.session.claude_session_id, None
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
    runner.InteractiveRunner(session, lambda st, se, so: _Sink()).run()
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
    r = runner.InteractiveRunner(env.session, lambda st, se, so: _Sink())
    code = r.run()
    assert code == 0
    assert seen["control"] is not None
    frame = seen["frame"]
    assert frame is not None
    assert frame.flip_byte == 0x1D
    assert frame.key_label == "^]"
    assert frame.active == "claude" and frame.other == "codex"
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
    runner.InteractiveRunner(env.session, lambda st, se, so: _Sink()).run()
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
    r = runner.InteractiveRunner(env.session, lambda st, se, so: _Sink())
    r.run()
    assert r.flip_requested is True


def test_runner_writes_bar_drop_marker(env_factory, monkeypatch, capsys):
    env = env_factory(active="claude")

    def fake_run_in_pty(argv, cwd=None, frame=None, control=None, child=None):
        frame.bar_dropped = True
        return 0

    monkeypatch.setattr(runner, "run_in_pty", fake_run_in_pty)
    r = runner.InteractiveRunner(env.session, lambda st, se, so: _Sink())
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
    r = runner.InteractiveRunner(env.session, lambda st, se, so: _Sink())
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
    runner.InteractiveRunner(env.session, lambda st, se, so: _Sink()).run()
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
    runner.InteractiveRunner(env.session, lambda st, se, so: _Sink()).run()
    probe = made["kw"]["status_probe"]
    assert probe is not None
    # the probe closes over the claude sid: feed the registry and ask it
    me = os.getpid()
    d = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{me}.json").write_text(json.dumps(
        {"pid": me, "sessionId": env.session.claude_session_id,
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
    runner.InteractiveRunner(env.session, lambda st, se, so: _Sink()).run()
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
    runner.InteractiveRunner(env.session, lambda st, se, so: _Sink()).run()
    from tandem.harness.claude_code import ClaudeCodeAdapter
    monkeypatch.setattr(
        ClaudeCodeAdapter, "session_status",
        lambda self, sid: (_ for _ in ()).throw(OverflowError()),
    )
    assert made["kw"]["status_probe"]() is None


# -- the warm standby wiring ----------------------------------------------


def test_idle_probe_claude_busy_and_waiting():
    from tandem.runner import _idle_probe

    class FakeMonitor:
        flip_requested = False
        transcript = None

    class BusyAdapter:
        def session_status(self, sid):
            return "busy"

    class WaitingAdapter:
        def session_status(self, sid):
            return "waiting"

    class RaisingAdapter:
        def session_status(self, sid):
            raise OSError("registry unreadable")

    m = FakeMonitor()
    assert _idle_probe("claude", BusyAdapter(), "sid", m, None)() is False
    assert _idle_probe("claude", WaitingAdapter(), "sid", m, None)() is True
    # eager on drift, like the flip probe: the freshness gate protects us
    assert _idle_probe("claude", RaisingAdapter(), "sid", m, None)() is True
    # a pending flip reads idle regardless of status
    m.flip_requested = True
    assert _idle_probe("claude", BusyAdapter(), "sid", m, None)() is True


def test_idle_probe_codex_mtime_rule(tmp_path):
    from tandem.runner import _idle_probe

    class FakeMonitor:
        flip_requested = False
        transcript = tmp_path / "rollout.jsonl"

    sentinel = tmp_path / "s.turn"
    m = FakeMonitor()
    m.transcript.write_text("{}\n")
    _touch(m.transcript, 1000)
    idle = _idle_probe("codex", None, None, m, sentinel)
    assert idle() is False            # no sentinel touch yet: can't know
    sentinel.touch()
    _touch(sentinel, 2000)
    assert idle() is True             # sentinel newer: between turns
    _touch(m.transcript, 3000)
    assert idle() is False            # transcript moved: mid-turn


def test_runner_hands_standby_over_only_on_flip(env_factory, monkeypatch):
    """The runner adopts nothing here; it must shutdown(keep_child=flip)."""
    import tandem.runner as runner_mod
    env = env_factory(active="claude")
    shutdowns = []

    class FakeStandby:
        def __init__(self, *a, **kw):
            self.kw = kw

        def start(self):
            pass

        def shutdown(self, keep_child):
            shutdowns.append(keep_child)
            return "the-child" if keep_child else None

    monkeypatch.setattr(runner_mod, "WarmStandby", FakeStandby)
    monkeypatch.setattr(runner_mod, "run_in_pty",
                        lambda *a, **kw: 0)
    r = runner_mod.InteractiveRunner(env.session, sink_factory=_null_sink)
    code = r.run()
    assert code == 0
    assert shutdowns == [False]       # no flip: child killed, not handed over
    assert r.warm_child is None


def test_runner_hands_the_standby_over_on_a_flip(env_factory, monkeypatch):
    # The other half of the handover: a flip must keep the warmed child and
    # publish it, or the whole feature quietly degrades to cold spawns.
    import tandem.runner as runner_mod
    env = env_factory(active="claude")
    sentinel = paths.tandem_home() / "tmp" / f"{env.session.tandem_id}-claude.turn"
    shutdowns = []

    class FakeStandby:
        def __init__(self, *a, **kw):
            pass

        def start(self):
            pass

        def shutdown(self, keep_child):
            shutdowns.append(keep_child)
            return "the-child" if keep_child else None

    def fake_run_in_pty(argv, cwd=None, env=None, frame=None, control=None,
                        child=None):
        control.attach(_DeadChild())
        frame.on_flip()               # user pressed the keybind
        deadline = time.time() + 3
        while not frame.armed() and time.time() < deadline:
            time.sleep(0.02)
        sentinel.touch()              # turn-complete marker: the wait releases
        deadline = time.time() + 3
        while frame.armed() and time.time() < deadline:
            time.sleep(0.02)
        return 0

    monkeypatch.setattr(runner_mod, "WarmStandby", FakeStandby)
    monkeypatch.setattr(runner_mod, "run_in_pty", fake_run_in_pty)
    r = runner_mod.InteractiveRunner(env.session, sink_factory=_null_sink)
    r.run()
    assert r.flip_requested is True
    assert shutdowns == [True]        # a flip keeps the child alive...
    assert r.warm_child == "the-child"   # ...and hands it to the next run


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


def test_runner_ignores_a_dead_or_mismatched_adoptee(env_factory, monkeypatch):
    # Neither a dead child nor one warmed for the other side is adoptable:
    # the runner rebuilds its own recipe and spawns cold.
    import tandem.runner as runner_mod
    from tandem.warm import build_launch
    env = env_factory(active="claude")
    seen = {}

    class Adoptee:
        def __init__(self, side, is_alive):
            self.recipe = build_launch(env.session, side)
            self._alive = is_alive
            self.released = False

        def alive(self):
            return self._alive

        def release(self):
            self.released = True
            return "raw-child"

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


def _standby_enabled_for(env, monkeypatch):
    """Run once with a real terminal underneath (the autouse fixture pins
    `_stdin_tty` shut, so the tty half has to be reopened by hand) and
    report the `enabled` the runner computed for its standby."""
    import tandem.runner as runner_mod
    made = {}

    class FakeStandby:
        def __init__(self, session, is_idle, **kw):
            made["kw"] = kw
            made["is_idle"] = is_idle

        def start(self):
            pass

        def shutdown(self, keep_child):
            return None

    monkeypatch.setattr(runner_mod, "_stdin_tty", lambda: True)
    monkeypatch.setattr(runner_mod, "WarmStandby", FakeStandby)
    monkeypatch.setattr(runner_mod, "run_in_pty", lambda *a, **kw: 0)
    runner_mod.InteractiveRunner(env.session, sink_factory=_null_sink).run()
    assert callable(made["is_idle"])
    return made["kw"]["enabled"]


def test_runner_warming_follows_the_config_flag(env_factory, monkeypatch):
    # Both halves of the gate: on a real terminal the default config warms,
    # and [frame] warm = false is what turns it off. Asserted with the tty
    # half forced open, or "enabled" would read False either way and the
    # config could be dropped from the gate unnoticed.
    env = env_factory(active="claude")
    assert _standby_enabled_for(env, monkeypatch) is True
    (paths.tandem_home() / "config.toml").write_text('[frame]\nwarm = false\n')
    assert _standby_enabled_for(env, monkeypatch) is False


def test_runner_never_warms_without_a_tty(env_factory, monkeypatch):
    # The non-tty path never flips, so a standby there could only ever leak
    # a hidden harness — config on, gate still shut. (The autouse fixture
    # supplies the non-tty half.)
    import tandem.runner as runner_mod
    env = env_factory(active="claude")
    made = {}

    class FakeStandby:
        def __init__(self, session, is_idle, **kw):
            made["kw"] = kw

        def start(self):
            pass

        def shutdown(self, keep_child):
            return None

    monkeypatch.setattr(runner_mod, "WarmStandby", FakeStandby)
    monkeypatch.setattr(runner_mod, "run_in_pty", lambda *a, **kw: 0)
    runner_mod.InteractiveRunner(env.session, sink_factory=_null_sink).run()
    assert made["kw"]["enabled"] is False


def test_runner_shuts_the_standby_down_when_the_pty_raises(env_factory,
                                                           monkeypatch):
    # Every exit path of run() must dispose of the standby: a started-then-
    # abandoned one leaves a hidden harness running with nobody to reap it.
    import tandem.runner as runner_mod
    env = env_factory(active="claude")
    shutdowns = []

    class FakeStandby:
        def __init__(self, *a, **kw):
            pass

        def start(self):
            pass

        def shutdown(self, keep_child):
            shutdowns.append(keep_child)
            return None

    def boom(*a, **kw):
        raise RuntimeError("pty exploded")

    monkeypatch.setattr(runner_mod, "WarmStandby", FakeStandby)
    monkeypatch.setattr(runner_mod, "run_in_pty", boom)
    r = runner_mod.InteractiveRunner(env.session, sink_factory=_null_sink)
    try:
        r.run()
    except RuntimeError:
        pass
    else:                                   # pragma: no cover - guard
        raise AssertionError("run() swallowed the pty failure")
    assert shutdowns == [False]
