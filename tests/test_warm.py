"""warm.py: launch recipes, hidden children, and the standby manager."""

from tandem import paths
from tandem.warm import build_launch


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
