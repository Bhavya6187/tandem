"""Fresh launches must bind to their own rollout, even in a shared cwd."""

import os
import time
from types import SimpleNamespace

from tandem import ops, paths, runner
from tandem.util import append_jsonl_fsync, uuid7


ORIGINATOR_ENV = "CODEX_INTERNAL_ORIGINATOR_OVERRIDE"


def rollout(cwd, originator, source="cli"):
    from tandem.harness import get_adapter

    sid = uuid7()
    path = get_adapter("codex").rollout_path(sid)
    append_jsonl_fsync(path, [{"type": "session_meta", "payload": {
        "id": sid, "cwd": cwd, "originator": originator, "source": source,
    }}])
    return path


def test_discovery_ignores_newer_foreign_and_child_rollouts(env_factory):
    env = env_factory()
    started = time.time()
    own = rollout(env.cwd, "tandem-launch-own")
    foreign = rollout(env.cwd, "codex_cli_rs")
    child = rollout(env.cwd, "tandem-launch-own", {"subagent": {"thread_spawn": {}}})
    for path in (foreign, child):
        os.utime(path, (started + 10, started + 10))
    assert runner.await_codex_rollout(
        env.cwd, started, timeout=0, originator="tandem-launch-own"
    ) == own


def test_discovery_never_falls_back_to_foreign_rollout(env_factory):
    env = env_factory()
    rollout(env.cwd, "codex_cli_rs")
    assert runner.await_codex_rollout(
        env.cwd, time.time(), timeout=0, originator="tandem-launch-missing"
    ) is None


def test_fresh_native_launch_adopts_only_its_own_rollout(env_factory, monkeypatch):
    env = env_factory(active="codex", seed_active=False)
    monkeypatch.setenv(ORIGINATOR_ENV, "inherited-parent")
    own = []

    def launch(argv, cwd=None, env=None, **kwargs):
        assert env and env[ORIGINATOR_ENV] != "inherited-parent"
        own.append(rollout(cwd, env[ORIGINATOR_ENV]))
        foreign = rollout(cwd, "codex_cli_rs")
        os.utime(foreign, (time.time() + 10,) * 2)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if store.get_session(session.tandem_id).native_id("codex"):
                break
            time.sleep(0.01)
        return 0

    class Sink:
        def handle(self, *args): pass
        def close(self): pass

    store, session = env.store, env.session
    monkeypatch.setattr(runner, "run_in_pty", launch)
    runner.InteractiveRunner(session, lambda *args: Sink()).run()
    assert env.refresh().native_id("codex") == paths.codex_rollout_session_id(own[0])
    assert os.environ[ORIGINATOR_ENV] == "inherited-parent"


def test_fresh_oneoff_adopts_only_its_own_rollout(env_factory, monkeypatch):
    env = env_factory(active="codex", seed_active=False)
    own = []

    def launch(argv, cwd=None, env=None):
        assert env and env.get(ORIGINATOR_ENV)
        own.append(rollout(cwd, env[ORIGINATOR_ENV], "exec"))
        foreign = rollout(cwd, "codex_cli_rs", "exec")
        os.utime(foreign, (time.time() + 10,) * 2)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(ops, "_run", launch)
    assert ops.run_oneoff(env.store, env.session, "codex", "hello") == 0
    assert env.refresh().native_id("codex") == paths.codex_rollout_session_id(own[0])


def test_missing_known_rollout_does_not_discover_another_session(env_factory, monkeypatch):
    env = env_factory(active="codex")
    original_id = env.session.native_id("codex")
    env.codex_shadow.unlink()
    rollout(env.cwd, "codex_cli_rs")
    monkeypatch.setattr(runner, "run_in_pty", lambda *args, **kwargs: 1)
    run = runner.InteractiveRunner(env.session, lambda *args: None)
    assert run.run() == 1
    assert env.refresh().native_id("codex") == original_id
    assert any("transcript missing" in report for report in run.reports)
