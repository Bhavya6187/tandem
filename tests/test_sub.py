"""tandem sub: shadow forking and the subagent execution op."""

import json
import subprocess
import time
from pathlib import Path

from tandem import ops, paths
from tandem.runner import await_codex_rollout
from tandem.util import read_jsonl

from conftest import claude_user, write_line


class TestForkShadow:
    def test_fork_copies_shadow_with_new_identity(self, env_factory):
        env = env_factory(active="claude")
        ops.fast_forward(env.store, env.session, "claude")
        write_line(env.claude_shadow, claude_user("context before fork"))

        fork_id, fork_path = ops.fork_shadow(env.store, env.session)

        assert fork_id != env.session.codex_session_id
        assert fork_path.name.endswith(f"-{fork_id}.jsonl")
        entries = read_jsonl(fork_path)
        meta = entries[0]
        assert meta["type"] == "session_meta"
        assert meta["payload"]["id"] == fork_id
        assert meta["payload"]["session_id"] == fork_id
        assert meta["payload"]["originator"] == "tandem-sub"
        assert meta["payload"]["model_provider"] == "openai"
        # the pre-fork drain landed the claude turn in the fork's history
        dump = json.dumps(entries)
        assert "context before fork" in dump

    def test_fork_is_structurally_resumable(self, env_factory):
        from tandem.doctor import validate_transcript

        env = env_factory(active="claude")
        ops.fast_forward(env.store, env.session, "claude")
        fork_id, fork_path = ops.fork_shadow(env.store, env.session)
        assert validate_transcript("codex", fork_path, fork_id) == []

    def test_fork_leaves_shadow_and_cursors_alone(self, env_factory):
        env = env_factory(active="claude")
        ops.fast_forward(env.store, env.session, "claude")
        before_bytes = env.codex_shadow.read_bytes()
        before_cursor = env.store.get_cursor(env.session.tandem_id, "codex")

        ops.fork_shadow(env.store, env.session)

        assert env.codex_shadow.read_bytes() == before_bytes
        after = env.store.get_cursor(env.session.tandem_id, "codex")
        assert after.byte_offset == before_cursor.byte_offset
        assert after.line_index == before_cursor.line_index

    def test_fork_is_not_adopted_as_a_freshly_minted_codex_session(self, env_factory):
        """A live fork sits in codex's sessions dir with the session cwd and a
        fresh mtime. If rollout discovery returned it, a concurrent fresh-codex
        launch would bind the pair's codex id to the subagent's throwaway."""
        env = env_factory(active="claude")
        ops.fast_forward(env.store, env.session, "claude")
        started = time.time()
        _, fork_path = ops.fork_shadow(env.store, env.session)

        assert await_codex_rollout(env.cwd, started, timeout=0) is None

        # control: a rollout codex itself wrote, same dir, is still discovered
        real_sid = "019faca1-0000-7000-8000-0000000000ff"
        real = fork_path.with_name(f"rollout-2026-07-31T00-00-00-{real_sid}.jsonl")
        write_line(real, {
            "timestamp": "t", "type": "session_meta",
            "payload": {"id": real_sid, "session_id": real_sid,
                        "cwd": env.cwd, "originator": "codex_cli"},
        })
        assert await_codex_rollout(env.cwd, started, timeout=0) == real


class TestSeedSubRollout:
    def test_seed_is_a_resumable_tandem_sub_rollout(self, env_factory):
        from tandem.doctor import validate_transcript

        env = env_factory(active="claude")
        seed_id, seed_path = ops.seed_sub_rollout(env.session)

        assert validate_transcript("codex", seed_path, seed_id) == []
        entries = read_jsonl(seed_path)
        meta = entries[0]
        assert meta["payload"]["id"] == seed_id
        assert meta["payload"]["session_id"] == seed_id
        assert meta["payload"]["cwd"] == env.cwd
        assert meta["payload"]["originator"] == "tandem-sub"
        assert meta["payload"]["model_provider"] == "openai"
        # minimal: the meta plus one note (response_item + event_msg)
        assert len(entries) == 3

    def test_seed_carries_no_history_and_drains_nothing(self, env_factory):
        env = env_factory(active="claude")
        write_line(env.claude_shadow, claude_user("pair-only context"))
        before_bytes = env.codex_shadow.read_bytes()
        before = env.store.get_cursor(env.session.tandem_id, "claude")

        _, seed_path = ops.seed_sub_rollout(env.session)

        assert "pair-only context" not in seed_path.read_text()
        assert env.codex_shadow.read_bytes() == before_bytes
        after = env.store.get_cursor(env.session.tandem_id, "claude")
        assert after.byte_offset == before.byte_offset
        assert after.line_index == before.line_index

    def test_seed_is_not_adopted_as_a_freshly_minted_codex_session(self, env_factory):
        env = env_factory(active="claude")
        started = time.time()
        ops.seed_sub_rollout(env.session)
        assert await_codex_rollout(env.cwd, started, timeout=0) is None


class _R:
    def __init__(self, code=0):
        self.returncode = code


class TestRunSub:
    def test_task_context_resumes_a_tandem_sub_seed(self, env_factory, monkeypatch):
        """A cold worker resumes its own seeded rollout rather than exec-ing
        fresh: a plain `codex exec` writes an ordinary rollout in the session
        cwd, and rollout discovery would then bind the pair's codex id to that
        throwaway (which run_sub deletes on the way out)."""
        env = env_factory(active="claude")
        calls = {}
        started = time.time()

        def fake_run(argv, cwd=None, **kw):
            calls["argv"], calls["cwd"] = argv, cwd
            seed = paths.find_codex_rollout(argv[argv.index("resume") + 1])
            calls["meta"] = read_jsonl(seed)[0]
            # while the worker runs, the seed must not look like a fresh codex
            calls["adopted"] = await_codex_rollout(env.cwd, started, timeout=0)
            return _R(0)

        monkeypatch.setattr(ops, "_run", fake_run)
        code = ops.run_sub(env.store, env.session, "audit the README",
                           model="gpt-x-mini", context="task")
        assert code == 0
        argv = calls["argv"]
        assert argv[:5] == ["codex", "exec", "--skip-git-repo-check",
                            "-m", "gpt-x-mini"]
        assert argv[5] == "resume"
        seed_id = argv[6]
        assert seed_id != env.session.codex_session_id
        assert argv[7:] == ["audit the README"]
        assert calls["cwd"] == env.cwd
        assert calls["meta"]["payload"]["originator"] == "tandem-sub"
        assert calls["meta"]["payload"]["cwd"] == env.cwd
        assert calls["adopted"] is None
        assert paths.find_codex_rollout(seed_id) is None  # cleaned up after

    def test_task_context_keep_forks_retains_seed(self, env_factory, monkeypatch):
        env = env_factory(active="claude")
        monkeypatch.setattr(ops, "_run", lambda *a, **kw: _R(0))
        ops.run_sub(env.store, env.session, "t", keep_forks=True)
        kept = list((paths.tandem_home() / "subagents"
                     / env.session.tandem_id).glob("rollout-*.jsonl"))
        assert len(kept) == 1

    def test_no_model_omits_flag_and_fanout_adds_enable(self, env_factory,
                                                        monkeypatch):
        env = env_factory(active="claude")
        calls = {}
        monkeypatch.setattr(
            ops, "_run",
            lambda argv, cwd=None, **kw: calls.update(argv=argv) or _R(0),
        )
        ops.run_sub(env.store, env.session, "t", fanout_feature="collab")
        assert "-m" not in calls["argv"]
        assert ["--enable", "collab"] == calls["argv"][3:5]

    def test_full_context_forks_resumes_and_deletes(self, env_factory,
                                                    monkeypatch):
        env = env_factory(active="claude")
        ops.fast_forward(env.store, env.session, "claude")
        calls = {}
        monkeypatch.setattr(
            ops, "_run",
            lambda argv, cwd=None, **kw: calls.update(argv=argv) or _R(0),
        )
        ops.run_sub(env.store, env.session, "deep task", context="full")
        argv = calls["argv"]
        assert "resume" in argv
        fork_id = argv[argv.index("resume") + 1]
        assert fork_id != env.session.codex_session_id
        # the fork was cleaned up (keep_forks=False)
        assert paths.find_codex_rollout(fork_id) is None
        kept = list((paths.tandem_home() / "subagents").rglob("*.jsonl"))
        assert kept == []

    def test_full_context_keep_forks_retains(self, env_factory, monkeypatch):
        env = env_factory(active="claude")
        ops.fast_forward(env.store, env.session, "claude")
        monkeypatch.setattr(ops, "_run", lambda *a, **kw: _R(0))
        ops.run_sub(env.store, env.session, "deep task", context="full",
                    keep_forks=True)
        kept = list((paths.tandem_home() / "subagents"
                     / env.session.tandem_id).glob("rollout-*.jsonl"))
        assert len(kept) == 1

    def test_exit_code_mirrors_codex(self, env_factory, monkeypatch):
        env = env_factory(active="claude")
        monkeypatch.setattr(ops, "_run", lambda *a, **kw: _R(3))
        assert ops.run_sub(env.store, env.session, "t") == 3

    def test_running_marker_lifecycle(self, env_factory, monkeypatch):
        env = env_factory(active="claude")
        seen = {}

        def fake_run(argv, cwd=None, **kw):
            run_dir = (paths.tandem_home() / "subagents"
                       / env.session.tandem_id / "running")
            seen["during"] = list(run_dir.glob("*.json"))
            # read the marker while the run is in flight; it is gone after
            seen["payload"] = [json.loads(p.read_text()) for p in seen["during"]]
            return _R(0)

        monkeypatch.setattr(ops, "_run", fake_run)
        ops.run_sub(env.store, env.session, "watch me")
        assert len(seen["during"]) == 1
        assert seen["payload"][0]["task_preview"] == "watch me"
        assert seen["during"][0].exists() is False  # removed on completion


class TestQuietRelay:
    """Quiet mode exists for the bridge agent: with inherited stdio the Bash
    output is codex's ENTIRE exec log (header, actions, token counts), and
    "return the final message verbatim" is not something a haiku relay can do
    reliably. Quiet mode makes the command's whole output BE the final
    message, via codex's own `-o/--output-last-message`."""

    def test_quiet_relays_only_the_last_message(self, env_factory, monkeypatch,
                                                capsys):
        env = env_factory(active="claude")
        calls = {}

        def fake_run(argv, cwd=None, **kw):
            calls["argv"], calls["kw"] = argv, kw
            i = argv.index("-o")
            calls["o_before_resume"] = i < argv.index("resume")
            calls["last"] = Path(argv[i + 1])
            calls["last"].write_text("FINAL ANSWER")
            return _R(0)

        monkeypatch.setattr(ops, "_run", fake_run)
        code = ops.run_sub(env.store, env.session, "t", quiet=True)

        assert code == 0
        # -o is an exec-level flag: it must precede the `resume` subcommand
        assert calls["o_before_resume"]
        # stdout carries the worker's answer and nothing else
        assert capsys.readouterr().out == "FINAL ANSWER\n"
        # the raw transcript is redirected to a retained log for debugging
        logs = (paths.tandem_home() / "subagents" / env.session.tandem_id
                / "logs")
        assert calls["last"].parent == logs
        assert calls["kw"]["stderr"] == subprocess.STDOUT
        assert list(logs.glob("*.log")) != []

    def test_quiet_falls_back_to_the_log_tail(self, env_factory, monkeypatch,
                                              capsys):
        """A codex run that dies before writing a last message must still
        relay something — otherwise the bridge reports an empty failure."""
        env = env_factory(active="claude")

        def fake_run(argv, cwd=None, stdout=None, **kw):
            stdout.write(b"boom: auth failed\nstack line\n")
            stdout.flush()
            return _R(1)

        monkeypatch.setattr(ops, "_run", fake_run)
        code = ops.run_sub(env.store, env.session, "t", quiet=True)

        assert code == 1
        out = capsys.readouterr().out
        assert "boom: auth failed" in out
        assert "stack line" in out

    def test_non_quiet_streams_with_inherited_stdio(self, env_factory,
                                                    monkeypatch, capsys):
        """Manual `tandem sub` keeps streaming: no -o, no redirection."""
        env = env_factory(active="claude")
        calls = {}
        monkeypatch.setattr(
            ops, "_run",
            lambda argv, cwd=None, **kw: calls.update(argv=argv, kw=kw) or _R(0),
        )
        ops.run_sub(env.store, env.session, "t")
        assert "-o" not in calls["argv"]
        assert calls["kw"] == {}
        assert capsys.readouterr().out == ""


class TestSubCli:
    def _cli_env(self, env, monkeypatch):
        from tandem import cli
        monkeypatch.setattr(cli, "_cwd", lambda: env.cwd)
        monkeypatch.setattr(
            cli, "_check_versions",
            lambda warn_only=False: {"claude": "2.1.220", "codex": "0.145.0"},
        )
        return cli

    def test_sub_reads_task_from_stdin(self, env_factory, monkeypatch):
        import click.testing
        env = env_factory(active="claude")
        cli = self._cli_env(env, monkeypatch)
        calls = {}
        monkeypatch.setattr(
            ops, "run_sub",
            lambda store, session, task, **kw: calls.update(
                task=task, kw=kw) or 0,
        )
        r = click.testing.CliRunner().invoke(
            cli.main, ["sub"], input="line one\nline two\n")
        assert r.exit_code == 0
        assert calls["task"] == "line one\nline two"
        assert calls["kw"]["context"] == "task"   # config default: match->task

    def test_sub_flags_override_config(self, env_factory, monkeypatch):
        import click.testing
        env = env_factory(active="claude")
        cli = self._cli_env(env, monkeypatch)
        calls = {}
        monkeypatch.setattr(
            ops, "run_sub",
            lambda store, session, task, **kw: calls.update(kw=kw) or 0,
        )
        r = click.testing.CliRunner().invoke(
            cli.main, ["sub", "-m", "gpt-x-mini", "--context", "full", "brief"])
        assert r.exit_code == 0
        assert calls["kw"]["model"] == "gpt-x-mini"
        assert calls["kw"]["context"] == "full"
        assert calls["kw"]["quiet"] is False

    def test_sub_quiet_flag_forwards(self, env_factory, monkeypatch):
        import click.testing
        env = env_factory(active="claude")
        cli = self._cli_env(env, monkeypatch)
        calls = {}
        monkeypatch.setattr(
            ops, "run_sub",
            lambda store, session, task, **kw: calls.update(kw=kw) or 0,
        )
        r = click.testing.CliRunner().invoke(cli.main, ["sub", "-q", "brief"])
        assert r.exit_code == 0
        assert calls["kw"]["quiet"] is True

    def test_sub_empty_task_errors(self, env_factory, monkeypatch):
        import click.testing
        env = env_factory(active="claude")
        cli = self._cli_env(env, monkeypatch)
        r = click.testing.CliRunner().invoke(cli.main, ["sub"], input="  \n")
        assert r.exit_code == 1
        assert "empty task" in r.output


class TestDoctorAndStatus:
    def test_doctor_warns_on_api_key_env(self, env_factory, monkeypatch):
        from tandem.doctor import run_doctor
        env = env_factory(active="claude")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        report = run_doctor(env.store, env.session, live=False)
        assert any("OPENAI_API_KEY" in c.message for c in report.checks
                   if c.status == "warn")

    def test_doctor_nudges_shared_block(self, env_factory, monkeypatch):
        from pathlib import Path
        from tandem.doctor import run_doctor
        env = env_factory(active="claude")
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        Path(env.cwd, "CLAUDE.md").write_text("# rules, no markers\n")
        report = run_doctor(env.store, env.session, live=False)
        assert any("tandem:shared" in c.message for c in report.checks)

    def test_status_lists_running_and_retained(self, env_factory, monkeypatch):
        import click.testing
        from tandem import cli, paths
        env = env_factory(active="claude")
        monkeypatch.setattr(cli, "_cwd", lambda: env.cwd)
        monkeypatch.setattr(
            cli, "_check_versions",
            lambda warn_only=False: {"claude": "2.1.220", "codex": "0.145.0"},
        )
        sub_root = paths.tandem_home() / "subagents" / env.session.tandem_id
        (sub_root / "running").mkdir(parents=True)
        (sub_root / "running" / "r1.json").write_text(json.dumps(
            {"model": "gpt-x-mini", "context": "task",
             "task_preview": "audit the README", "pid": 1}))
        (sub_root / "rollout-x-1.jsonl").write_text("{}\n")
        r = click.testing.CliRunner().invoke(cli.main, ["status"])
        assert "subagent running: gpt-x-mini (task) audit the README" in r.output
        assert "retained forks: 1" in r.output
