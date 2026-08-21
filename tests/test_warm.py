"""warm.py: launch recipes and hidden children."""

import dataclasses
import os
import time

from tandem import paths
from tandem.harness import get_adapter
from tandem.warm import (
    WarmChild,
    _shadow_size,
    build_launch,
    spawn_hidden,
)


def test_build_launch_claude_resume(env_factory):
    env = env_factory(active="claude")
    r = build_launch(env.session, "claude")
    assert r.side == "claude"
    assert r.argv[:3] == ["claude", "--resume", env.session.native_id("claude")]
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
    assert r.argv[:3] == ["claude", "--session-id", env.session.native_id("claude")]
    assert r.fresh is True
    # monitor still needs somewhere to look for the transcript
    assert r.transcript == paths.claude_transcript_path(
        env.session.cwd, env.session.native_id("claude")
    )


def test_build_launch_codex_resume(env_factory):
    env = env_factory(active="claude")
    r = build_launch(env.session, "codex")
    assert r.argv[:3] == ["codex", "resume", env.session.native_id("codex")]
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
    unpaired = dataclasses.replace(
        env.session,
        native_session_ids={**env.session.native_session_ids, "codex": None},
    )
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


def test_kill_keeps_reader_draining_through_the_short_ladder(
    env_factory, monkeypatch
):
    env = env_factory()
    fake = _FakePty()
    wc = WarmChild(_recipe(env), fake, shadow_size=0)
    calls = {}

    def fake_terminate(self, soft, soft_timeout=3.0, term_timeout=2.0,
                       attach_timeout=5.0):
        calls["soft"] = soft
        calls["timeouts"] = (soft_timeout, term_timeout)
        calls["reader_alive"] = wc._reader.is_alive()
        return "soft"

    from tandem import ptyrun
    monkeypatch.setattr(ptyrun.PtyControl, "terminate", fake_terminate)
    wc.kill()
    from tandem.harness import get_adapter
    assert calls["soft"] == get_adapter("codex").quit_keystrokes()
    assert calls["timeouts"] == (1.5, 1.0)
    assert calls["reader_alive"]
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


def test_model_argv_per_adapter():
    assert get_adapter("claude").model_argv("haiku") == ["--model", "haiku"]
    assert get_adapter("codex").model_argv("gpt-5.3-codex") == [
        "-m", "gpt-5.3-codex"]
    assert get_adapter("opencode").model_argv(
        "anthropic/claude-sonnet-5") == ["--model", "anthropic/claude-sonnet-5"]


def test_prompt_hook_capability_flags():
    assert get_adapter("claude").prompt_hook_capable is True
    assert get_adapter("codex").prompt_hook_capable is True
    assert get_adapter("opencode").prompt_hook_capable is False


def test_build_launch_appends_model_argv(env_factory):
    env = env_factory(active="claude")
    r = build_launch(env.session, "claude", model="haiku")
    assert r.model == "haiku"
    i = r.argv.index("--model")
    assert r.argv[i + 1] == "haiku"
    plain = build_launch(env.session, "claude")
    assert plain.model == "" and "--model" not in plain.argv


def test_build_launch_model_pin_outranks_user_args(env_factory):
    # An explicit per-turn route pin (@claude:haiku) is more specific intent
    # than a static [claude] args entry, and both CLIs take the LAST flag —
    # so the pin has to land after the user's args, or config silently wins.
    env = env_factory(active="claude")
    (paths.tandem_home() / "config.toml").write_text(
        '[claude]\nargs = ["--model", "other"]\n'
    )
    r = build_launch(env.session, "claude", model="haiku")
    last = len(r.argv) - 1 - r.argv[::-1].index("--model")
    assert r.argv[last + 1] == "haiku"
    # ...and still ahead of the hook extras, which stay the tail of argv
    assert r.hook_extra and r.argv[-len(r.hook_extra):] == r.hook_extra
