"""InteractiveRunner: user-configured [harness] args land in the spawned argv."""

import os
import threading
import time

from tandem import paths, runner
from tandem.runner import FlipMonitor, wait_until_safe


class _Sink:
    def handle(self, line, ctx, cursor): ...

    def close(self): ...


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
    # down (1.0 stands in for the real 30s valve) to keep the test fast — the
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
    assert not done.wait(timeout=0.4)   # parked behind the 30s valve
    cancel.set()                        # don't leave it parked for 30s
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

    def fake_run_in_pty(argv, cwd=None, frame=None, control=None):
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
    assert wired.quiesce == 30.0         # missed-marker valve, not turn pacing
    assert override.quiesce == 1.5       # explicit injection wins


def test_monitor_passes_mode_through_to_wait(tmp_path, monkeypatch):
    seen = {}

    def fake_wait(transcript, sentinel, cancelled, quiesce=None, poll=0.2,
                  marker_wired=False, provider=None):
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
    assert seen == {"quiesce": 30.0, "poll": 0.05, "marker_wired": True,
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

    def fake_run_in_pty(argv, cwd=None, frame=None, control=None):
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
    assert frame.active == "claude" and frame.other == "codex"
    assert r.flip_requested is False


def test_runner_reports_flip_requested(env_factory, monkeypatch):
    env = env_factory(active="claude")
    sentinel = paths.tandem_home() / "tmp" / f"{env.session.tandem_id}-claude.turn"

    def fake_run_in_pty(argv, cwd=None, frame=None, control=None):
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

    def fake_run_in_pty(argv, cwd=None, frame=None, control=None):
        frame.bar_dropped = True
        return 0

    monkeypatch.setattr(runner, "run_in_pty", fake_run_in_pty)
    runner.InteractiveRunner(env.session, lambda st, se, so: _Sink()).run()
    marker = paths.tandem_home() / "tmp" / f"{env.session.tandem_id}-bar-dropped"
    assert marker.exists()
    # A dropped bar is a note, not a sync failure: labelling it "sync error"
    # sends the user hunting a transcript divergence that never happened.
    out = capsys.readouterr().out
    assert "status bar disabled for this session" in out
    assert "sync error" not in out


def _run_capturing_monitor(env, monkeypatch):
    made = {}
    real = runner.FlipMonitor

    def capture(*a, **kw):
        made["monitor"] = real(*a, **kw)
        return made["monitor"]

    monkeypatch.setattr(runner, "FlipMonitor", capture)
    monkeypatch.setattr(
        runner, "run_in_pty",
        lambda argv, cwd=None, frame=None, control=None: 0,
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
