"""warm.py: launch recipes, hidden children, and the standby manager."""

import dataclasses
import os
import time

from tandem import paths
from tandem.warm import (
    WarmChild,
    WarmStandby,
    _ensure_shadow_default,
    _shadow_size,
    build_launch,
    spawn_hidden,
)


def test_build_launch_claude_resume(env_factory):
    env = env_factory(active="claude")
    r = build_launch(env.session, "claude")
    assert r.side == "claude"
    assert r.argv[:3] == ["claude", "--resume", env.session.claude_session_id]
    assert r.fresh is False
    assert r.transcript is not None and r.transcript.exists()
    assert r.cwd == env.session.cwd
    assert r.sentinel == (
        paths.tandem_home() / "tmp" / f"{env.session.tandem_id}-claude.turn"
    )
    assert r.sentinel.parent.is_dir()
    # the Stop-hook extras are recorded separately AND present in argv
    assert r.hook_extra and r.argv[-len(r.hook_extra):] == r.hook_extra


def test_build_launch_claude_fresh_uses_session_id_and_expected_path(env_factory):
    env = env_factory(active="claude")
    env.claude_shadow.unlink()   # no file yet -> fresh launch
    r = build_launch(env.session, "claude")
    assert r.argv[:3] == ["claude", "--session-id", env.session.claude_session_id]
    assert r.fresh is True
    # monitor still needs somewhere to look for the transcript
    assert r.transcript == paths.claude_transcript_path(
        env.session.cwd, env.session.claude_session_id
    )


def test_build_launch_codex_resume(env_factory):
    env = env_factory(active="claude")
    r = build_launch(env.session, "codex")
    assert r.argv[:3] == ["codex", "resume", env.session.codex_session_id]
    assert r.side == "codex"
    assert r.fresh is False


def test_build_launch_orders_user_args_before_hook_extras(env_factory):
    env = env_factory(active="claude")
    (paths.tandem_home() / "config.toml").write_text(
        '[claude]\nargs = ["--model", "opus"]\n'
    )
    r = build_launch(env.session, "claude")
    i_args = r.argv.index("--model")
    i_hook = r.argv.index(r.hook_extra[0])
    assert i_args < i_hook


class _FakePty:
    """PtyProcess stand-in: fd is a pipe the test can feed/close."""

    def __init__(self):
        self.rd, self.wr = os.pipe()
        self.pid = os.getpid()
        self.written = []
        self._alive = True

    @property
    def fd(self):
        return self.rd

    def read(self, n):
        data = os.read(self.rd, n)
        if not data:
            raise EOFError
        return data

    def write(self, data):
        self.written.append(bytes(data))

    def isalive(self):
        return self._alive


def _recipe(env, side="codex"):
    return build_launch(env.session, side)


def test_shadow_size_reads_bytes(env_factory):
    env = env_factory(active="claude")
    size = _shadow_size(env.session, "codex")
    assert size == env.codex_shadow.stat().st_size
    env.claude_shadow.unlink()
    assert _shadow_size(env.session, "claude") is None


def test_shadow_size_none_without_session_id(env_factory):
    env = env_factory(active="claude")
    unpaired = dataclasses.replace(env.session, codex_session_id=None)
    assert _shadow_size(unpaired, "codex") is None


def test_shadow_size_none_when_transcript_vanishes(env_factory, monkeypatch):
    """The rollout can be deleted between path resolution and stat()."""
    env = env_factory(active="claude")
    from tandem.harness.codex import CodexAdapter

    doomed = env.codex_shadow
    monkeypatch.setattr(CodexAdapter, "transcript_path", lambda s, cwd, sid: doomed)
    doomed.unlink()
    assert _shadow_size(env.session, "codex") is None


def test_warm_child_discards_output_without_blocking(env_factory):
    env = env_factory()
    fake = _FakePty()
    wc = WarmChild(_recipe(env), fake, shadow_size=10)
    # a chatty hidden child must be drained, not left to fill the pipe:
    # write well past the pipe buffer; progress is only possible if the
    # discard reader keeps consuming the other end
    os.set_blocking(fake.wr, False)
    total = 0
    deadline = time.time() + 3
    while total < 200_000:
        try:
            total += os.write(fake.wr, b"x" * 4096)
        except BlockingIOError:
            assert time.time() < deadline, "discard reader is not draining"
            time.sleep(0.02)
    assert wc.alive()
    os.close(fake.wr)
    wc.release()


def test_release_joins_reader_and_returns_child(env_factory):
    env = env_factory()
    fake = _FakePty()
    wc = WarmChild(_recipe(env), fake, shadow_size=0)
    got = wc.release()
    assert got is fake
    assert not wc._reader.is_alive()


class _WedgedReader:
    """A reader thread that never joins — join() returns, is_alive() stays True."""

    def join(self, timeout=None):
        return None

    def is_alive(self):
        return True


def test_release_refuses_to_hand_over_a_still_owned_fd(env_factory):
    env = env_factory()
    fake = _FakePty()
    wc = WarmChild(_recipe(env), fake, shadow_size=0)
    real = wc._reader
    wc._reader = _WedgedReader()
    # two readers on one fd would split the harness's output stream: the
    # caller must be told to cold-spawn instead
    assert wc.release() is None
    wc._reader = real
    assert wc.release() is fake


def test_kill_runs_the_short_ladder(env_factory, monkeypatch):
    env = env_factory()
    fake = _FakePty()
    wc = WarmChild(_recipe(env), fake, shadow_size=0)
    calls = {}

    def fake_terminate(self, soft, soft_timeout=3.0, term_timeout=2.0,
                       attach_timeout=5.0):
        calls["soft"] = soft
        calls["timeouts"] = (soft_timeout, term_timeout)
        return "soft"

    from tandem import ptyrun
    monkeypatch.setattr(ptyrun.PtyControl, "terminate", fake_terminate)
    wc.kill()
    from tandem.harness import get_adapter
    assert calls["soft"] == get_adapter("codex").quit_keystrokes()
    assert calls["timeouts"] == (1.5, 1.0)
    assert not wc._reader.is_alive()


def test_spawn_hidden_is_one_column_narrow(env_factory):
    env = env_factory()
    seen = {}

    def fake_spawn(argv, cwd=None, env=None, dimensions=None):
        seen["argv"] = argv
        seen["cwd"] = cwd
        seen["dimensions"] = dimensions
        return _FakePty()

    r = _recipe(env)
    wc = spawn_hidden(r, (40, 120), shadow_size=7, spawn=fake_spawn)
    assert seen["dimensions"] == (39, 119)
    assert seen["argv"] == r.argv
    assert seen["cwd"] == r.cwd
    assert wc.shadow_size == 7
    wc.release()


def test_spawn_hidden_floors_tiny_windows(env_factory):
    env = env_factory()
    def fake_spawn(argv, cwd=None, env=None, dimensions=None):
        assert dimensions == (1, 1)
        return _FakePty()
    spawn_hidden(_recipe(env), (1, 1), 0, spawn=fake_spawn).release()


class _Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


class _Spawned:
    """Stands in for WarmChild in manager tests."""

    def __init__(self, recipe, shadow_size):
        self.recipe = recipe
        self.shadow_size = shadow_size
        self.killed = False
        self._alive = True

    def alive(self):
        return self._alive

    def kill(self):
        self.killed = True
        self._alive = False


def _standby(env, **kw):
    clock = kw.pop("clock", _Clock())
    spawned = []

    def spawner(recipe, dims, shadow_size):
        wc = _Spawned(recipe, shadow_size)
        spawned.append(wc)
        return wc

    sb = WarmStandby(
        env.session,
        kw.pop("is_idle", lambda: True),
        winsize=lambda: (40, 120),
        spawner=kw.pop("spawner", spawner),
        clock=clock,
        sync_memory=kw.pop("sync_memory", lambda: None),
        ensure_shadow=kw.pop("ensure_shadow", lambda s: s),
        **kw,
    )
    return sb, clock, spawned


def _settle(sb, clock, ticks=4, step=1.0):
    """Drive the state machine directly — no thread, no sleeps."""
    for _ in range(ticks):
        clock.t += step
        sb._tick()


def test_spawns_after_idle_and_debounce(env_factory):
    env = env_factory(active="claude")
    sb, clock, spawned = _standby(env)
    sb._tick()             # first sighting: starts the stability window
    assert not spawned
    _settle(sb, clock)     # > 1.5s stable
    assert len(spawned) == 1
    assert spawned[0].recipe.side == "codex"
    assert spawned[0].shadow_size == env.codex_shadow.stat().st_size


def test_no_spawn_while_busy(env_factory):
    env = env_factory(active="claude")
    sb, clock, spawned = _standby(env, is_idle=lambda: False)
    _settle(sb, clock, ticks=6)
    assert not spawned


def test_shadow_growth_restarts_debounce(env_factory):
    env = env_factory(active="claude")
    sb, clock, spawned = _standby(env)
    sb._tick()
    clock.t += 1.0
    with open(env.codex_shadow, "a") as f:
        f.write("{}\n")      # a turn synced in
    sb._tick()               # sees the new size: window restarts
    clock.t += 1.0
    sb._tick()               # only 1.0s stable — still waiting
    assert not spawned
    clock.t += 1.0
    sb._tick()
    assert len(spawned) == 1


def test_held_child_invalidated_on_shadow_growth(env_factory):
    env = env_factory(active="claude")
    sb, clock, spawned = _standby(env)
    _settle(sb, clock)
    assert len(spawned) == 1
    with open(env.codex_shadow, "a") as f:
        f.write("{}\n")
    sb._tick()
    assert spawned[0].killed
    _settle(sb, clock)       # respawns once stable again
    assert len(spawned) == 2


def test_fresh_child_is_left_alone(env_factory):
    env = env_factory(active="claude")
    sb, clock, spawned = _standby(env)
    _settle(sb, clock)
    _settle(sb, clock, ticks=10)
    assert len(spawned) == 1
    assert not spawned[0].killed


def test_self_dying_children_consume_retries_then_marker(env_factory):
    env = env_factory(active="claude")
    sb, clock, spawned = _standby(env, max_retries=2)
    for expected in (1, 2, 3):
        _settle(sb, clock)
        assert len(spawned) == expected
        spawned[-1]._alive = False    # boot crash
        sb._tick()                    # notices the death
    _settle(sb, clock, ticks=10)
    assert len(spawned) == 3          # 1 initial + 2 retries, then gave up
    marker = paths.tandem_home() / "tmp" / f"{env.session.tandem_id}-warm-failed"
    assert marker.exists()
    assert "exited" in marker.read_text()


def test_missing_shadow_calls_ensure_and_refuses_without_file(env_factory):
    env = env_factory(active="claude")
    env.codex_shadow.unlink()
    calls = []

    def ensure(session):
        calls.append(session.tandem_id)
        return session          # "created nothing" (data-loss guard case)

    sb, clock, spawned = _standby(env, ensure_shadow=ensure)
    _settle(sb, clock)
    assert calls                # ensure ran
    assert not spawned          # but no file -> never a blank --session-id


def test_memory_sync_runs_before_spawn(env_factory):
    env = env_factory(active="claude")
    order = []
    sb, clock, spawned = _standby(
        env, sync_memory=lambda: order.append("sync"),
        spawner=lambda r, d, s: order.append("spawn") or _Spawned(r, s),
    )
    _settle(sb, clock)
    assert order == ["sync", "spawn"]


def test_shutdown_keep_child_hands_it_over(env_factory):
    env = env_factory(active="claude")
    sb, clock, spawned = _standby(env)
    _settle(sb, clock)
    child = sb.shutdown(keep_child=True)
    assert child is spawned[0] and not child.killed
    assert sb.child is None


def test_shutdown_without_keep_kills(env_factory):
    env = env_factory(active="claude")
    sb, clock, spawned = _standby(env)
    _settle(sb, clock)
    assert sb.shutdown(keep_child=False) is None
    assert spawned[0].killed


def test_disabled_standby_never_starts_its_thread(env_factory):
    env = env_factory(active="claude")
    sb, clock, spawned = _standby(env, enabled=False)
    sb.start()
    assert not sb._thread.is_alive()
    assert sb.shutdown(keep_child=True) is None


def test_dead_child_gets_kill_called(env_factory):
    env = env_factory(active="claude")
    sb, clock, spawned = _standby(env)
    _settle(sb, clock)
    spawned[0]._alive = False
    sb._tick()
    # the process is gone, but the discard reader still owns the pty fd and
    # keeps the WarmChild referenced: dropping the handle alone leaks it
    assert spawned[0].killed


def test_spawn_racing_shutdown_kills_rather_than_orphans(env_factory):
    env = env_factory(active="claude")
    sb, clock, spawned = _standby(env)
    sb._stop.set()   # shutdown already joined and emptied the child slot
    sb._spawn(_shadow_size(env.session, "codex"))
    assert len(spawned) == 1 and spawned[0].killed
    assert sb.child is None


def test_ensure_shadow_default_never_mints_over_a_recorded_codex_id(env_factory):
    """File gone but the id still recorded: minting a replacement rollout
    here would be rejected by the runner's mtime-cutoff discovery, so the
    recorded id survives untouched and the standby simply refuses."""
    env = env_factory(active="claude")
    env.codex_shadow.unlink()
    fresh = _ensure_shadow_default(env.session)
    assert fresh is not None
    assert fresh.codex_session_id == env.session.codex_session_id
    assert _shadow_size(fresh, "codex") is None


def test_spawn_failures_consume_retries_then_marker(env_factory):
    """A spawner that keeps blowing up (bad argv, no pty) must give up on
    the same budget a crash-on-boot child does, not retry every poll."""
    attempts = []

    def boom(recipe, dims, shadow_size):
        attempts.append(recipe)
        raise OSError("no ptys left")

    env = env_factory(active="claude")
    sb, clock, spawned = _standby(env, spawner=boom, max_retries=2)
    _settle(sb, clock, ticks=12)
    assert len(attempts) == 3    # 1 + 2 retries over 12 ticks, then it stops
    assert sb.child is None
    marker = paths.tandem_home() / "tmp" / f"{env.session.tandem_id}-warm-failed"
    assert "spawn failed: OSError: no ptys left" in marker.read_text()


def test_build_launch_failures_consume_retries_too(env_factory, monkeypatch):
    """Recipe building is inside the budget: its sentinel mkdir can fail on a
    read-only home, and an uncounted failure would retry forever in silence."""
    attempts = []

    def boom(session, side):
        attempts.append(side)
        raise OSError("read-only file system")

    monkeypatch.setattr("tandem.warm.build_launch", boom)
    env = env_factory(active="claude")
    sb, clock, spawned = _standby(env, max_retries=2)
    _settle(sb, clock, ticks=12)
    assert len(attempts) == 3
    assert not spawned
    marker = paths.tandem_home() / "tmp" / f"{env.session.tandem_id}-warm-failed"
    assert "spawn failed: OSError: read-only file system" in marker.read_text()


def _burn_the_budget(sb, clock, spawned):
    for _ in range(3):
        _settle(sb, clock)
        spawned[-1]._alive = False    # boot crash
        sb._tick()
    _settle(sb, clock, ticks=4)


def test_new_content_after_giving_up_resumes_warming(env_factory):
    """Giving up is only ever 'until the next invalidation': a turn syncing
    in starts a new idle period, so the retry budget re-arms."""
    env = env_factory(active="claude")
    sb, clock, spawned = _standby(env, max_retries=2)
    _burn_the_budget(sb, clock, spawned)
    assert len(spawned) == 3
    with open(env.codex_shadow, "a") as f:
        f.write("{}\n")              # a turn synced in
    _settle(sb, clock, ticks=4)
    assert len(spawned) == 4


def test_ensure_shadow_is_not_re_asked_every_poll(env_factory):
    """The permanent-refuse case (file gone, un-creatable) must latch, not
    reopen the state store once a second forever."""
    env = env_factory(active="claude")
    env.codex_shadow.unlink()
    calls = []
    sb, clock, spawned = _standby(
        env, ensure_shadow=lambda s: calls.append(s.tandem_id) or s
    )
    _settle(sb, clock, ticks=20)
    assert len(calls) == 1
    assert not spawned
