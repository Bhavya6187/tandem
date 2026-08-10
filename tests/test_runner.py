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
        lambda argv, cwd=None: calls.update(argv=argv) or 0,
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
        lambda argv, cwd=None: calls.update(argv=argv) or 0,
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
                  marker_wired=False):
        seen.update(quiesce=quiesce, poll=poll, marker_wired=marker_wired)
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
    assert seen == {"quiesce": 30.0, "poll": 0.05, "marker_wired": True}
