"""tandem sub: shadow forking and the subagent execution op."""

import contextlib
import json
import subprocess
import time
from pathlib import Path

import pytest

from tandem import modelcat, ops, paths, runner
from tandem.runner import await_codex_rollout
from tandem.util import read_jsonl

from conftest import claude_user, write_line


class TestForkShadow:
    def test_fork_copies_shadow_with_new_identity(self, env_factory):
        env = env_factory(active="claude")
        ops.fast_forward_all(env.store, env.session, "claude")
        write_line(env.claude_shadow, claude_user("context before fork"))

        fork_id, fork_path = ops.fork_shadow(env.store, env.session)

        assert fork_id != env.session.native_id("codex")
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
        ops.fast_forward_all(env.store, env.session, "claude")
        fork_id, fork_path = ops.fork_shadow(env.store, env.session)
        assert validate_transcript("codex", fork_path, fork_id) == []

    def test_fork_leaves_shadow_and_cursors_alone(self, env_factory):
        env = env_factory(active="claude")
        ops.fast_forward_all(env.store, env.session, "claude")
        before_bytes = env.codex_shadow.read_bytes()
        before_cursor = env.store.get_cursor(env.session.tandem_id, "codex", "claude")

        ops.fork_shadow(env.store, env.session)

        assert env.codex_shadow.read_bytes() == before_bytes
        after = env.store.get_cursor(env.session.tandem_id, "codex", "claude")
        assert after.byte_offset == before_cursor.byte_offset
        assert after.line_index == before_cursor.line_index

    def test_fork_is_not_adopted_as_a_freshly_minted_codex_session(self, env_factory):
        """A live fork sits in codex's sessions dir with the session cwd and a
        fresh mtime. If rollout discovery returned it, a concurrent fresh-codex
        launch would bind the pair's codex id to the subagent's throwaway."""
        env = env_factory(active="claude")
        ops.fast_forward_all(env.store, env.session, "claude")
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
        before = env.store.get_cursor(env.session.tandem_id, "claude", "codex")

        _, seed_path = ops.seed_sub_rollout(env.session)

        assert "pair-only context" not in seed_path.read_text()
        assert env.codex_shadow.read_bytes() == before_bytes
        after = env.store.get_cursor(env.session.tandem_id, "claude", "codex")
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
            calls["argv"], calls["cwd"], calls["kw"] = argv, cwd, kw
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
        assert seed_id != env.session.native_id("codex")
        # the brief travels on stdin; argv ends at codex's `-` stdin marker
        assert argv[7:] == ["-"]
        assert calls["kw"]["input"] == b"audit the README"
        assert calls["cwd"] == env.cwd
        assert calls["meta"]["payload"]["originator"] == "tandem-sub"
        assert calls["meta"]["payload"]["cwd"] == env.cwd
        assert calls["adopted"] is None
        assert paths.find_codex_rollout(seed_id) is None  # cleaned up after

    def test_brief_never_reaches_argv(self, env_factory, monkeypatch):
        """A brief is untrusted text, not argv. As a trailing positional,
        anything starting with `-` is parsed by codex's own CLI: `- review …`
        dies with `unexpected argument '- ' found` before the model runs, and
        `-m gpt-9 …` is silently accepted as a flag — argv injection. codex
        documents `-` as "read the prompt from stdin"; that is the transport."""
        env = env_factory(active="claude")
        calls = {}
        monkeypatch.setattr(
            ops, "_run",
            lambda argv, cwd=None, **kw: calls.update(argv=argv, kw=kw) or _R(0),
        )
        task = "- review the diff\n-m gpt-9 --dangerously-bypass\n"
        ops.run_sub(env.store, env.session, task)
        argv = calls["argv"]
        assert argv[-3:-2] == ["resume"] and argv[-1] == "-"
        # not one fragment of the brief is on the command line
        assert not any(a.startswith("- ") or a == "-m gpt-9" for a in argv[:-1])
        assert calls["kw"]["input"] == task.encode()

    def test_full_context_brief_also_goes_over_stdin(self, env_factory,
                                                     monkeypatch):
        env = env_factory(active="claude")
        ops.fast_forward_all(env.store, env.session, "claude")
        calls = {}
        monkeypatch.setattr(
            ops, "_run",
            lambda argv, cwd=None, **kw: calls.update(argv=argv, kw=kw) or _R(0),
        )
        ops.run_sub(env.store, env.session, "-m sneaky", context="full")
        assert calls["argv"][-1] == "-"
        assert calls["kw"]["input"] == b"-m sneaky"

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
        ops.fast_forward_all(env.store, env.session, "claude")
        calls = {}
        monkeypatch.setattr(
            ops, "_run",
            lambda argv, cwd=None, **kw: calls.update(argv=argv) or _R(0),
        )
        ops.run_sub(env.store, env.session, "deep task", context="full")
        argv = calls["argv"]
        assert "resume" in argv
        fork_id = argv[argv.index("resume") + 1]
        assert fork_id != env.session.native_id("codex")
        # the fork was cleaned up (keep_forks=False)
        assert paths.find_codex_rollout(fork_id) is None
        kept = list((paths.tandem_home() / "subagents").rglob("*.jsonl"))
        assert kept == []

    def test_full_context_keep_forks_retains(self, env_factory, monkeypatch):
        env = env_factory(active="claude")
        ops.fast_forward_all(env.store, env.session, "claude")
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

    def test_marker_preview_is_single_line(self, env_factory, monkeypatch):
        """`tandem status` prints the preview on its own line. A raw slice of a
        multi-paragraph Claude brief drags newlines into that line and the
        listing becomes multi-line garbage."""
        env = env_factory(active="claude")
        seen = {}

        def fake_run(argv, cwd=None, **kw):
            run_dir = (paths.tandem_home() / "subagents"
                       / env.session.tandem_id / "running")
            seen["payload"] = json.loads(next(run_dir.glob("*.json")).read_text())
            return _R(0)

        monkeypatch.setattr(ops, "_run", fake_run)
        ops.run_sub(env.store, env.session,
                    "Audit the README.\n\n  Then\treport back.\n")
        preview = seen["payload"]["task_preview"]
        assert preview == "Audit the README. Then report back."
        assert len(preview.splitlines()) == 1

    def test_marker_preview_is_capped(self, env_factory, monkeypatch):
        env = env_factory(active="claude")
        seen = {}

        def fake_run(argv, cwd=None, **kw):
            run_dir = (paths.tandem_home() / "subagents"
                       / env.session.tandem_id / "running")
            seen["payload"] = json.loads(next(run_dir.glob("*.json")).read_text())
            return _R(0)

        monkeypatch.setattr(ops, "_run", fake_run)
        ops.run_sub(env.store, env.session, "word " * 200)
        assert len(seen["payload"]["task_preview"]) == 120


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

    def test_relay_precedes_fork_disposal(self, env_factory, monkeypatch,
                                          capsys):
        """The worker's answer is the whole point of the run; cleanup is
        bookkeeping. With the relay last in `finally`, a raising disposal
        (cross-device rename, unwritable home) destroys a result codex already
        produced and paid for. Relay first, then clean up."""
        env = env_factory(active="claude")

        def fake_run(argv, cwd=None, **kw):
            Path(argv[argv.index("-o") + 1]).write_text("FINAL ANSWER")
            return _R(0)

        def boom(*a, **kw):
            raise OSError("cross-device link")

        monkeypatch.setattr(ops, "_run", fake_run)
        monkeypatch.setattr(ops.shutil, "move", boom)
        with pytest.raises(OSError):
            ops.run_sub(env.store, env.session, "t", quiet=True, keep_forks=True)
        assert capsys.readouterr().out == "FINAL ANSWER\n"

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
        # stdin carries the brief; stdout/stderr stay inherited
        assert set(calls["kw"]) == {"input"}
        assert capsys.readouterr().out == ""


class TestSubLockCoverage:
    """`--context full` drains the active source from a *second* process while
    the session's own tail thread is draining the same cursor row in the
    first. `fork_shadow` holds `_sub_lock`; until the tail thread took it too,
    the lock guarded only one side of the race and two concurrent drains could
    translate the same lines twice (duplicate turns and call ids in the fork).
    Both sides now serialize on the same flock."""

    def test_tail_thread_drains_under_the_sub_lock(self, env_factory,
                                                   monkeypatch):
        env = env_factory(active="claude")
        depth = [0]
        seen: list[int] = []

        @contextlib.contextmanager
        def recording_lock():
            depth[0] += 1
            try:
                yield
            finally:
                depth[0] -= 1

        real_drain = runner.TailLoop.drain

        def drain(self):
            seen.append(depth[0])
            return real_drain(self)

        monkeypatch.setattr(ops, "_sub_lock", recording_lock)
        monkeypatch.setattr(runner.TailLoop, "drain", drain)
        monkeypatch.setattr(runner, "run_in_pty", lambda argv, cwd=None, **kw: 0)

        class _Sink:
            def handle(self, line, ctx, cursor): ...
            def close(self): ...

        code = runner.InteractiveRunner(
            env.session, lambda store, session, source, target: _Sink()).run()

        assert code == 0
        assert seen, "the tail thread never drained"
        assert all(d == 1 for d in seen), f"drain ran outside the lock: {seen}"
        assert depth[0] == 0  # released every time


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


class TestSubModelHeader:
    def _cli_env(self, env, monkeypatch):
        from tandem import cli
        monkeypatch.setattr(cli, "_cwd", lambda: env.cwd)
        monkeypatch.setattr(
            cli, "_check_versions",
            lambda warn_only=False: {"claude": "2.1.220", "codex": "0.145.0"},
        )
        return cli

    def _catalog(self):
        from tandem import paths
        p = paths.codex_models_cache_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"models": [
            {"slug": "gpt-5.6-sol", "display_name": "GPT-5.6-Sol",
             "visibility": "list"},
            {"slug": "gpt-5.6-terra", "display_name": "GPT-5.6-Terra",
             "visibility": "list"},
            {"slug": "gpt-5.4-mini", "display_name": "GPT-5.4-Mini",
             "visibility": "list"},
        ]}))

    def _capture(self, monkeypatch):
        calls = {}
        monkeypatch.setattr(
            ops, "run_sub",
            lambda store, session, task, **kw: calls.update(
                task=task, kw=kw) or 0,
        )
        return calls

    def test_header_resolves_and_is_stripped(self, env_factory, monkeypatch):
        import click.testing
        env = env_factory(active="claude")
        cli = self._cli_env(env, monkeypatch)
        self._catalog()
        calls = self._capture(monkeypatch)
        r = click.testing.CliRunner().invoke(
            cli.main, ["sub"], input="tandem-model: sol\ndo the thing\n")
        assert r.exit_code == 0
        assert calls["task"] == "do the thing"
        assert calls["kw"]["model"] == "gpt-5.6-sol"

    def test_flag_beats_header_but_header_still_stripped(
            self, env_factory, monkeypatch):
        import click.testing
        env = env_factory(active="claude")
        cli = self._cli_env(env, monkeypatch)
        self._catalog()
        calls = self._capture(monkeypatch)
        r = click.testing.CliRunner().invoke(
            cli.main, ["sub", "-m", "flag-model"],
            input="tandem-model: sol\ntask\n")
        assert r.exit_code == 0
        assert calls["task"] == "task"
        assert calls["kw"]["model"] == "flag-model"

    def test_unknown_model_fails_before_codex(self, env_factory, monkeypatch):
        import click.testing
        env = env_factory(active="claude")
        cli = self._cli_env(env, monkeypatch)
        self._catalog()
        calls = self._capture(monkeypatch)
        r = click.testing.CliRunner().invoke(
            cli.main, ["sub"], input="tandem-model: o3\ntask\n")
        assert r.exit_code == 1
        assert "unknown model 'o3'" in r.output
        assert "gpt-5.6-sol" in r.output
        assert calls == {}          # codex was never invoked

    def test_quiet_appends_model_footer(self, env_factory, monkeypatch):
        import click.testing
        env = env_factory(active="claude")
        cli = self._cli_env(env, monkeypatch)
        self._catalog()
        self._capture(monkeypatch)
        r = click.testing.CliRunner().invoke(
            cli.main, ["sub", "-q"], input="tandem-model: sol\ntask\n")
        assert r.output.rstrip().endswith("[tandem-sub model: gpt-5.6-sol]")

    def test_no_header_no_footer(self, env_factory, monkeypatch):
        import click.testing
        env = env_factory(active="claude")
        cli = self._cli_env(env, monkeypatch)
        self._catalog()
        calls = self._capture(monkeypatch)
        r = click.testing.CliRunner().invoke(
            cli.main, ["sub", "-q"], input="just a task\n")
        assert "[tandem-sub model:" not in r.output
        assert calls["kw"]["model"] == ""   # config default: unset

    def test_non_quiet_announces_worker_model(self, env_factory, monkeypatch):
        import click.testing
        env = env_factory(active="claude")
        cli = self._cli_env(env, monkeypatch)
        self._catalog()
        self._capture(monkeypatch)
        r = click.testing.CliRunner().invoke(
            cli.main, ["sub"], input="tandem-model: sol\ntask\n")
        assert "worker model: gpt-5.6-sol" in r.output

    def test_missing_catalog_passes_name_verbatim(
            self, env_factory, monkeypatch):
        import click.testing
        env = env_factory(active="claude")
        cli = self._cli_env(env, monkeypatch)
        # no _catalog() call: CODEX_HOME has no models_cache.json
        calls = self._capture(monkeypatch)
        r = click.testing.CliRunner().invoke(
            cli.main, ["sub"], input="tandem-model: mystery-model\ntask\n")
        assert r.exit_code == 0
        assert calls["kw"]["model"] == "mystery-model"

    def test_malformed_header_fails_before_catalog_or_session(
            self, env_factory, monkeypatch):
        # a near-miss header must never fall through as task text: that is
        # the silent-wrong-model failure this protocol exists to eliminate
        import click.testing
        env = env_factory(active="claude")
        cli = self._cli_env(env, monkeypatch)
        # no _catalog(): the failure has to land before any catalog read
        monkeypatch.setattr(
            modelcat, "load_catalog",
            lambda: pytest.fail("catalog read before the header was validated"))
        calls = self._capture(monkeypatch)
        r = click.testing.CliRunner().invoke(
            cli.main, ["sub"], input="tandem-model: <gpt-5.4-mini>\ntask\n")
        assert r.exit_code == 1
        assert ("error: malformed tandem-model header: "
                "tandem-model: <gpt-5.4-mini>") in r.output
        assert calls == {}

    def test_header_near_misses_still_reach_the_worker(
            self, env_factory, monkeypatch):
        # trailing space + CRLF + mixed-case prefix + a spoken name
        import click.testing
        env = env_factory(active="claude")
        cli = self._cli_env(env, monkeypatch)
        self._catalog()
        calls = self._capture(monkeypatch)
        r = click.testing.CliRunner().invoke(
            cli.main, ["sub"], input="Tandem-Model: 5.4 mini \r\ndo the thing\n")
        assert r.exit_code == 0
        assert calls["task"] == "do the thing"
        assert calls["kw"]["model"] == "gpt-5.4-mini"

    def test_header_only_brief_is_an_empty_task_error(
            self, env_factory, monkeypatch):
        import click.testing
        env = env_factory(active="claude")
        cli = self._cli_env(env, monkeypatch)
        self._catalog()
        calls = self._capture(monkeypatch)
        r = click.testing.CliRunner().invoke(
            cli.main, ["sub"], input="tandem-model: sol\n")
        assert r.exit_code == 1
        assert "empty task brief" in r.output
        assert calls == {}

    # --- stashed-pin recovery: the hook's out-of-band copy of the header ---

    def test_stashed_pin_recovers_dropped_header(self, env_factory,
                                                 monkeypatch):
        # the haiku relay drops the `tandem-model:` first line from the
        # heredoc it echoes (live transcripts, 2026-08-08 audit); the hook
        # stashed the pin off the pristine parent prompt, and a headerless
        # brief recovers it here
        import click.testing

        from tandem import pinstash
        env = env_factory(active="claude")
        cli = self._cli_env(env, monkeypatch)
        self._catalog()
        calls = self._capture(monkeypatch)
        pinstash.stash("do the thing", "sol")
        r = click.testing.CliRunner().invoke(
            cli.main, ["sub"], input="do the thing\n")
        assert r.exit_code == 0
        assert calls["task"] == "do the thing"
        assert calls["kw"]["model"] == "gpt-5.6-sol"

    def test_recovered_pin_appends_quiet_footer(self, env_factory,
                                                monkeypatch):
        # recovery must be visible exactly like an in-band pin: the trailer
        # is the dispatching session's only proof of which model ran
        import click.testing

        from tandem import pinstash
        env = env_factory(active="claude")
        cli = self._cli_env(env, monkeypatch)
        self._catalog()
        self._capture(monkeypatch)
        pinstash.stash("task", "sol")
        r = click.testing.CliRunner().invoke(
            cli.main, ["sub", "-q"], input="task\n")
        assert r.output.rstrip().endswith("[tandem-sub model: gpt-5.6-sol]")

    def test_inband_header_beats_stashed_pin(self, env_factory, monkeypatch):
        # a preserved header is the fresher signal; the stash is only the
        # fallback for briefs that arrive without one
        import click.testing

        from tandem import pinstash
        env = env_factory(active="claude")
        cli = self._cli_env(env, monkeypatch)
        self._catalog()
        calls = self._capture(monkeypatch)
        pinstash.stash("task", "terra")
        r = click.testing.CliRunner().invoke(
            cli.main, ["sub"], input="tandem-model: sol\ntask\n")
        assert r.exit_code == 0
        assert calls["kw"]["model"] == "gpt-5.6-sol"

    def test_explicit_flag_skips_stash_lookup(self, env_factory, monkeypatch):
        # -m callers never rode the header protocol; a stale pin must not
        # abort their dispatch (an unresolvable recovered name would)
        import click.testing

        from tandem import pinstash
        env = env_factory(active="claude")
        cli = self._cli_env(env, monkeypatch)
        self._catalog()
        calls = self._capture(monkeypatch)
        pinstash.stash("task", "mystery-model")
        r = click.testing.CliRunner().invoke(
            cli.main, ["sub", "-m", "flag-model"], input="task\n")
        assert r.exit_code == 0
        assert calls["kw"]["model"] == "flag-model"

    def test_unknown_recovered_pin_fails_loud(self, env_factory, monkeypatch):
        # same contract as an in-band header: resolution failures are loud
        # and land before codex is invoked
        import click.testing

        from tandem import pinstash
        env = env_factory(active="claude")
        cli = self._cli_env(env, monkeypatch)
        self._catalog()
        calls = self._capture(monkeypatch)
        pinstash.stash("task", "o3")
        r = click.testing.CliRunner().invoke(
            cli.main, ["sub"], input="task\n")
        assert r.exit_code == 1
        assert "unknown model 'o3'" in r.output
        assert calls == {}

    # --- generic-name standins: "gpt"/"codex" mean "no preference" ---

    def test_standin_header_runs_the_codex_default(
            self, env_factory, monkeypatch):
        import click.testing
        env = env_factory(active="claude")
        cli = self._cli_env(env, monkeypatch)
        self._catalog()
        calls = self._capture(monkeypatch)
        r = click.testing.CliRunner().invoke(
            cli.main, ["sub", "-q"], input="tandem-model: gpt\ndo the thing\n")
        assert r.exit_code == 0
        assert calls["task"] == "do the thing"
        assert calls["kw"]["model"] == ""     # no -m: codex's own default
        assert r.output.rstrip().endswith("[tandem-sub model: codex default]")

    def test_standin_header_uses_the_configured_model(
            self, env_factory, monkeypatch):
        import click.testing
        env = env_factory(active="claude")
        cli = self._cli_env(env, monkeypatch)
        self._catalog()
        (paths.tandem_home() / "config.toml").write_text(
            '[subagents]\nmodel = "gpt-5.4-mini"\n')
        calls = self._capture(monkeypatch)
        r = click.testing.CliRunner().invoke(
            cli.main, ["sub", "-q"], input="tandem-model: codex\ntask\n")
        assert r.exit_code == 0
        assert calls["kw"]["model"] == "gpt-5.4-mini"
        assert r.output.rstrip().endswith("[tandem-sub model: gpt-5.4-mini]")

    def test_standin_header_announces_codex_default_non_quiet(
            self, env_factory, monkeypatch):
        import click.testing
        env = env_factory(active="claude")
        cli = self._cli_env(env, monkeypatch)
        self._catalog()
        self._capture(monkeypatch)
        r = click.testing.CliRunner().invoke(
            cli.main, ["sub"], input="tandem-model: GPT\ntask\n")
        assert r.exit_code == 0
        assert "worker model: codex default" in r.output

    def test_flag_beats_standin_header(self, env_factory, monkeypatch):
        import click.testing
        env = env_factory(active="claude")
        cli = self._cli_env(env, monkeypatch)
        self._catalog()
        calls = self._capture(monkeypatch)
        r = click.testing.CliRunner().invoke(
            cli.main, ["sub", "-m", "gpt-5.6-sol"],
            input="tandem-model: gpt\ntask\n")
        assert r.exit_code == 0
        assert calls["task"] == "task"
        assert calls["kw"]["model"] == "gpt-5.6-sol"
        assert "worker model: gpt-5.6-sol" in r.output

    def test_standin_without_a_catalog_still_runs_the_default(
            self, env_factory, monkeypatch):
        # `gpt` must never reach `codex -m` verbatim, even unreadable-catalog
        import click.testing
        env = env_factory(active="claude")
        cli = self._cli_env(env, monkeypatch)
        # no _catalog(): CODEX_HOME has no models_cache.json
        calls = self._capture(monkeypatch)
        r = click.testing.CliRunner().invoke(
            cli.main, ["sub", "-q"], input="tandem-model: codex\ntask\n")
        assert r.exit_code == 0
        assert calls["kw"]["model"] == ""
        assert r.output.rstrip().endswith("[tandem-sub model: codex default]")

    def test_family_prefixed_header_resolves(self, env_factory, monkeypatch):
        # live-observed wording: an agent asked for "gpt terra" emits
        # `tandem-model: gpt-terra`, which failed resolution before 0.1.10
        import click.testing
        env = env_factory(active="claude")
        cli = self._cli_env(env, monkeypatch)
        self._catalog()
        calls = self._capture(monkeypatch)
        r = click.testing.CliRunner().invoke(
            cli.main, ["sub", "-q"],
            input="tandem-model: gpt-terra\ndo the thing\n")
        assert r.exit_code == 0
        assert calls["task"] == "do the thing"
        assert calls["kw"]["model"] == "gpt-5.6-terra"
        assert r.output.rstrip().endswith("[tandem-sub model: gpt-5.6-terra]")

    def test_family_name_still_fails_loudly(self, env_factory, monkeypatch):
        # the standin is exactly `gpt`/`codex`, not a prefix rule
        import click.testing
        env = env_factory(active="claude")
        cli = self._cli_env(env, monkeypatch)
        self._catalog()
        calls = self._capture(monkeypatch)
        r = click.testing.CliRunner().invoke(
            cli.main, ["sub"], input="tandem-model: gpt-5\ntask\n")
        assert r.exit_code == 1
        assert "unknown model 'gpt-5'" in r.output
        assert "gpt-5.6-sol" in r.output and "gpt-5.4-mini" in r.output
        assert calls == {}


class TestDoctorAndStatus:
    def test_doctor_warns_on_api_key_env(self, env_factory, monkeypatch):
        from tandem.doctor import run_doctor
        env = env_factory(active="claude")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        report = run_doctor(env.store, env.session, live=False)
        assert any("OPENAI_API_KEY" in c.message for c in report.checks
                   if c.status == "warn")

    def test_doctor_warns_when_no_model_is_configured(self, env_factory,
                                                      monkeypatch):
        """The shipped defaults are route="manual" + model="" — nothing is
        rerouted, so every codex worker is one the user hand-picked. That is
        exactly why the warning still matters: picking `tandem:gpt` chooses a
        harness, never a price, and with no `-m` the worker runs on the codex
        account's default — the frontier tier (S1: gpt-5.6-sol). Silently
        spending frontier money is the one surprise doctor must call out."""
        from tandem import paths
        from tandem.doctor import run_doctor
        env = env_factory(active="claude")
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        report = run_doctor(env.store, env.session, live=False)
        assert any("no model configured" in c.message for c in report.checks
                   if c.status == "warn")
        # configuring one silences it
        (paths.tandem_home() / "config.toml").write_text(
            '[subagents]\nmodel = "gpt-5.6-luna"\n')
        report = run_doctor(env.store, env.session, live=False)
        assert not any("no model configured" in c.message
                       for c in report.checks)

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
        assert r.exit_code == 0
        assert "subagent running: gpt-x-mini (task) audit the README" in r.output
        assert "retained forks: 1" in r.output

    def test_status_names_the_empty_model_like_the_trailer(
            self, env_factory, monkeypatch):
        """A worker with no model picked is the same state everywhere — one
        wording owns it, so status must not invent its own name for what the
        trailer and the announcement call `codex default`."""
        import click.testing
        from tandem import cli, modelcat, paths
        env = env_factory(active="claude")
        monkeypatch.setattr(cli, "_cwd", lambda: env.cwd)
        monkeypatch.setattr(
            cli, "_check_versions",
            lambda warn_only=False: {"claude": "2.1.220", "codex": "0.145.0"},
        )
        run_dir = (paths.tandem_home() / "subagents" / env.session.tandem_id
                   / "running")
        run_dir.mkdir(parents=True)
        (run_dir / "r1.json").write_text(json.dumps(
            {"model": "", "context": "task", "task_preview": "audit", "pid": 1}))
        r = click.testing.CliRunner().invoke(cli.main, ["status"])
        assert r.exit_code == 0
        assert f"subagent running: {modelcat.model_label('')} (task) audit" \
            in r.output
        assert "codex default" in r.output

    def test_doctor_survives_non_object_auth_json(self, env_factory, monkeypatch):
        """auth.json that is valid JSON but not an object must not crash
        doctor: `.get` on a list raises AttributeError, not ValueError."""
        from tandem import paths
        from tandem.doctor import run_doctor
        env = env_factory(active="claude")
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        auth = paths.codex_home() / "auth.json"
        auth.parent.mkdir(parents=True, exist_ok=True)
        auth.write_text("[]")
        report = run_doctor(env.store, env.session, live=False)
        assert not any("API-key" in c.message for c in report.checks)
        assert not any("OPENAI_API_KEY" in c.message for c in report.checks)

    def test_status_skips_non_object_marker(self, env_factory, monkeypatch):
        """A marker holding valid-but-non-object JSON is skipped, not fatal."""
        import click.testing
        from tandem import cli, paths
        env = env_factory(active="claude")
        monkeypatch.setattr(cli, "_cwd", lambda: env.cwd)
        monkeypatch.setattr(
            cli, "_check_versions",
            lambda warn_only=False: {"claude": "2.1.220", "codex": "0.145.0"},
        )
        run_dir = (paths.tandem_home() / "subagents" / env.session.tandem_id
                   / "running")
        run_dir.mkdir(parents=True)
        # sorts first, so it would crash before the good marker is reached
        (run_dir / "bad.json").write_text('"3"')
        (run_dir / "good.json").write_text(json.dumps(
            {"model": "gpt-x-mini", "context": "task",
             "task_preview": "audit the README", "pid": 1}))
        r = click.testing.CliRunner().invoke(cli.main, ["status"])
        assert r.exit_code == 0
        assert "subagent running: gpt-x-mini (task) audit the README" in r.output
        assert r.output.count("subagent running:") == 1


class TestSandboxPlumbing:
    def test_run_sub_passes_sandbox_before_resume(self, env_factory,
                                                  monkeypatch):
        env = env_factory(active="claude")
        calls = {}
        monkeypatch.setattr(
            ops, "_run",
            lambda argv, cwd=None, **kw: calls.update(argv=argv) or _R(0),
        )
        ops.run_sub(env.store, env.session, "t", sandbox="workspace-write")
        argv = calls["argv"]
        i = argv.index("--sandbox")
        assert argv[i + 1] == "workspace-write"
        assert i < argv.index("resume")   # exec-level flag, like -m and -o

    def test_run_sub_default_omits_sandbox(self, env_factory, monkeypatch):
        env = env_factory(active="claude")
        calls = {}
        monkeypatch.setattr(
            ops, "_run",
            lambda argv, cwd=None, **kw: calls.update(argv=argv) or _R(0),
        )
        ops.run_sub(env.store, env.session, "t")
        assert "--sandbox" not in calls["argv"]

    def test_sub_cli_flag_forwards(self, env_factory, monkeypatch):
        import click.testing
        env = env_factory(active="claude")
        cli = TestSubCli()._cli_env(env, monkeypatch)
        calls = {}
        monkeypatch.setattr(
            ops, "run_sub",
            lambda store, session, task, **kw: calls.update(kw=kw) or 0,
        )
        r = click.testing.CliRunner().invoke(
            cli.main, ["sub", "--sandbox", "workspace-write", "brief"])
        assert r.exit_code == 0
        assert calls["kw"]["sandbox"] == "workspace-write"

    def test_sub_cli_reads_stamp_when_no_flag(self, env_factory, monkeypatch):
        import click.testing
        env = env_factory(active="claude")
        cli = TestSubCli()._cli_env(env, monkeypatch)
        stamp = paths.tandem_home() / "sandbox" / env.session.tandem_id
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.write_text("workspace-write")
        calls = {}
        monkeypatch.setattr(
            ops, "run_sub",
            lambda store, session, task, **kw: calls.update(kw=kw) or 0,
        )
        r = click.testing.CliRunner().invoke(cli.main, ["sub", "brief"])
        assert r.exit_code == 0
        assert calls["kw"]["sandbox"] == "workspace-write"

    def test_sub_cli_flag_beats_stamp(self, env_factory, monkeypatch):
        import click.testing
        env = env_factory(active="claude")
        cli = TestSubCli()._cli_env(env, monkeypatch)
        stamp = paths.tandem_home() / "sandbox" / env.session.tandem_id
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.write_text("workspace-write")
        calls = {}
        monkeypatch.setattr(
            ops, "run_sub",
            lambda store, session, task, **kw: calls.update(kw=kw) or 0,
        )
        r = click.testing.CliRunner().invoke(
            cli.main, ["sub", "--sandbox", "read-only", "brief"])
        assert r.exit_code == 0
        assert calls["kw"]["sandbox"] == "read-only"

    def test_sub_cli_rejects_danger_full_access(self, env_factory, monkeypatch):
        """Global constraint: tandem never grants codex an unsandboxed run.
        `danger-full-access` is not in the Choice, so click kills the invocation
        with a usage error (exit 2) before run_sub is reached — the value is
        unreachable through the flag, whatever the brief asks for."""
        import click.testing
        env = env_factory(active="claude")
        cli = TestSubCli()._cli_env(env, monkeypatch)
        calls = {}
        monkeypatch.setattr(
            ops, "run_sub",
            lambda store, session, task, **kw: calls.update(kw=kw) or 0,
        )
        r = click.testing.CliRunner().invoke(
            cli.main, ["sub", "--sandbox", "danger-full-access", "brief"])
        assert r.exit_code == 2
        assert not calls

    def test_sub_cli_garbage_stamp_degrades_to_no_flag(self, env_factory,
                                                       monkeypatch):
        import click.testing
        env = env_factory(active="claude")
        cli = TestSubCli()._cli_env(env, monkeypatch)
        stamp = paths.tandem_home() / "sandbox" / env.session.tandem_id
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.write_text("danger-full-access")   # never act on this
        calls = {}
        monkeypatch.setattr(
            ops, "run_sub",
            lambda store, session, task, **kw: calls.update(kw=kw) or 0,
        )
        r = click.testing.CliRunner().invoke(cli.main, ["sub", "brief"])
        assert r.exit_code == 0
        assert calls["kw"]["sandbox"] == ""


class TestBlockedWriteFooter:
    def _fake_run_blocked(self, last_text="Could not write the file."):
        def fake_run(argv, cwd=None, **kw):
            # non-quiet runs pass no `-o`: stdio is inherited and codex
            # writes no last-message file. The rejected patch still lands.
            if "-o" in argv:
                Path(argv[argv.index("-o") + 1]).write_text(last_text)
            sub_path = paths.find_codex_rollout(
                argv[argv.index("resume") + 1])
            write_line(sub_path, {
                "timestamp": "t", "type": "event_msg",
                "payload": {"type": "patch_apply_end", "stdout": "",
                            "success": False,
                            "changes": {"plugin/README.md": {"type": "add"}}},
            })
            return _R(0)
        return fake_run

    def test_quiet_blocked_write_appends_structured_footer(
            self, env_factory, monkeypatch, capsys):
        env = env_factory(active="claude")
        monkeypatch.setattr(ops, "_run", self._fake_run_blocked())
        code = ops.run_sub(env.store, env.session, "t", quiet=True)
        assert code == 0                       # exit code stays codex's
        out = capsys.readouterr().out
        assert "Could not write the file." in out
        assert out.index("Could not write") < out.index(ops.BLOCKED_HEADER)
        assert "plugin/README.md" in out
        assert "--sandbox workspace-write" in out   # the retry hint

    def test_retry_hint_suppressed_when_already_workspace_write(
            self, env_factory, monkeypatch, capsys):
        # blocked even with write access => the hint would be a lie
        env = env_factory(active="claude")
        monkeypatch.setattr(ops, "_run", self._fake_run_blocked())
        ops.run_sub(env.store, env.session, "t", quiet=True,
                    sandbox="workspace-write")
        out = capsys.readouterr().out
        assert ops.BLOCKED_HEADER in out
        assert "--sandbox workspace-write`" not in out.split(
            ops.BLOCKED_HEADER)[1]

    def test_successful_patches_emit_no_footer(self, env_factory,
                                               monkeypatch, capsys):
        env = env_factory(active="claude")

        def fake_run(argv, cwd=None, **kw):
            Path(argv[argv.index("-o") + 1]).write_text("done")
            sub_path = paths.find_codex_rollout(
                argv[argv.index("resume") + 1])
            write_line(sub_path, {
                "timestamp": "t", "type": "event_msg",
                "payload": {"type": "patch_apply_end", "stdout": "ok",
                            "success": True,
                            "changes": {"a.py": {"type": "update"}}},
            })
            return _R(0)

        monkeypatch.setattr(ops, "_run", fake_run)
        ops.run_sub(env.store, env.session, "t", quiet=True)
        assert ops.BLOCKED_HEADER not in capsys.readouterr().out

    def test_non_quiet_never_prints_footer(self, env_factory, monkeypatch,
                                           capsys):
        # manual runs stream codex's own output; the footer is bridge
        # protocol, not user chrome
        env = env_factory(active="claude")
        monkeypatch.setattr(ops, "_run", self._fake_run_blocked())
        ops.run_sub(env.store, env.session, "t")
        assert ops.BLOCKED_HEADER not in capsys.readouterr().out

    def test_unreadable_rollout_degrades_to_no_footer(self, env_factory,
                                                      monkeypatch, capsys):
        env = env_factory(active="claude")

        def fake_run(argv, cwd=None, **kw):
            Path(argv[argv.index("-o") + 1]).write_text("fine")
            sub_path = paths.find_codex_rollout(
                argv[argv.index("resume") + 1])
            sub_path.write_text("not json {{{\n")
            return _R(0)

        monkeypatch.setattr(ops, "_run", fake_run)
        code = ops.run_sub(env.store, env.session, "t", quiet=True)
        assert code == 0
        out = capsys.readouterr().out
        assert "fine" in out
        assert ops.BLOCKED_HEADER not in out

    def test_full_context_ignores_patches_from_before_this_run(
            self, env_factory, monkeypatch, capsys):
        """A `--context full` fork inherits the pair's real codex rollout —
        apply_patch failures from earlier interactive turns included. Those
        are not this worker's doing, and reporting them sends the
        orchestrator retrying a write this run never attempted."""
        env = env_factory(active="claude")
        ops.fast_forward_all(env.store, env.session, "claude")
        write_line(env.codex_shadow, {
            "timestamp": "t", "type": "event_msg",
            "payload": {"type": "patch_apply_end", "stdout": "",
                        "success": False,
                        "changes": {"old/stale.py": {"type": "add"}}},
        })
        monkeypatch.setattr(ops, "_run", self._fake_run_blocked())
        ops.run_sub(env.store, env.session, "t", quiet=True, context="full")
        out = capsys.readouterr().out
        # this run's own rejection is still reported ...
        assert ops.BLOCKED_HEADER in out
        assert "plugin/README.md" in out
        # ... the inherited one is not
        assert "old/stale.py" not in out

    # Modeled byte-for-byte on a live read-only rejection (controller probe,
    # 2026-08-01, rollout 019fbfe6-…): codex emits NO patch_apply_end when the
    # sandbox refuses a write — the rejection exists only as this call/output
    # pair. The patch sits inside a JS string literal, so its line breaks are
    # literal two-char `\n` escapes; only the JS statements use real newlines.
    _LIVE_EXEC_INPUT = (
        'const patch = "*** Begin Patch\\n*** Add File: probe.txt'
        '\\n+hello\\n*** End Patch";\n'
        'const result = await tools.apply_patch(patch);\ntext(result);\n'
    )

    def _rejection_pair(self, call_id="call_GLGqPXu3fYY5aw9m3cmzCyl4"):
        return [
            {"timestamp": "t", "type": "response_item",
             "payload": {"type": "custom_tool_call", "status": "completed",
                         "call_id": call_id, "name": "exec",
                         "input": self._LIVE_EXEC_INPUT}},
            {"timestamp": "t", "type": "response_item",
             "payload": {"type": "custom_tool_call_output", "call_id": call_id,
                         "output": [
                             {"type": "input_text",
                              "text": "Script failed\nWall time 0.0 seconds\n"
                                      "Output:\n"},
                             {"type": "input_text",
                              "text": "Script error:\npatch rejected: writing "
                                      "is blocked by read-only sandbox; "
                                      "rejected by user approval settings"},
                         ]}},
        ]

    def test_custom_tool_call_rejection_is_detected(self, env_factory,
                                                    monkeypatch, capsys):
        """The real rejection path. Matching only patch_apply_end left the
        feature dead in production: codex emits that event when a patch
        APPLIES, not when the sandbox refuses it."""
        env = env_factory(active="claude")

        def fake_run(argv, cwd=None, **kw):
            Path(argv[argv.index("-o") + 1]).write_text(
                "I was unable to create the file.")
            sub_path = paths.find_codex_rollout(
                argv[argv.index("resume") + 1])
            for e in self._rejection_pair():
                write_line(sub_path, e)
            return _R(0)

        monkeypatch.setattr(ops, "_run", fake_run)
        code = ops.run_sub(env.store, env.session, "t", quiet=True)
        assert code == 0
        out = capsys.readouterr().out
        assert ops.BLOCKED_HEADER in out
        # the path is parsed out of the JS-escaped patch, stopping at the
        # `\n` escape rather than swallowing the rest of the string literal
        assert "Rejected: probe.txt" in out
        assert "+hello" not in out

    def test_custom_tool_call_rejection_below_watermark_is_ignored(
            self, env_factory, monkeypatch, capsys):
        """Same real shape, but inherited by a --context full fork from an
        earlier interactive turn: not this worker's rejection."""
        env = env_factory(active="claude")
        ops.fast_forward_all(env.store, env.session, "claude")
        for e in self._rejection_pair():
            write_line(env.codex_shadow, e)

        def fake_run(argv, cwd=None, **kw):
            Path(argv[argv.index("-o") + 1]).write_text("no edits attempted")
            return _R(0)

        monkeypatch.setattr(ops, "_run", fake_run)
        ops.run_sub(env.store, env.session, "t", quiet=True, context="full")
        out = capsys.readouterr().out
        assert ops.BLOCKED_HEADER not in out
        assert "probe.txt" not in out

    def test_non_patch_output_mentioning_the_marker_is_ignored(
            self, env_factory, monkeypatch, capsys):
        """Dogfooding hazard: this repo's own ops.py contains the literal
        marker, so a worker grepping for it gets the string straight back in
        ordinary tool output. Matching that as a blocked write escalates the
        orchestrator to workspace-write after a run that patched nothing."""
        env = env_factory(active="claude")

        def fake_run(argv, cwd=None, **kw):
            Path(argv[argv.index("-o") + 1]).write_text("Found 3 matches.")
            sub_path = paths.find_codex_rollout(
                argv[argv.index("resume") + 1])
            write_line(sub_path, {
                "timestamp": "t", "type": "response_item",
                "payload": {"type": "custom_tool_call", "call_id": "c-rg",
                            "name": "exec",
                            "input": "rg -n 'patch rejected' src/"}})
            write_line(sub_path, {
                "timestamp": "t", "type": "response_item",
                "payload": {"type": "custom_tool_call_output",
                            "call_id": "c-rg", "output": [
                                {"type": "input_text",
                                 "text": "src/tandem/ops.py:404:"
                                         'PATCH_REJECTED = "patch rejected"\n'
                                         "src/tandem/ops.py:437:carrying "
                                         "`patch rejected`. The\n"}]}})
            return _R(0)

        monkeypatch.setattr(ops, "_run", fake_run)
        ops.run_sub(env.store, env.session, "t", quiet=True)
        out = capsys.readouterr().out
        assert "Found 3 matches." in out
        assert ops.BLOCKED_HEADER not in out

    def test_combined_grep_hit_is_ignored(self, env_factory, monkeypatch,
                                          capsys):
        """The narrowing above gates on the CALL looking like a patch, which a
        single grep for BOTH literals satisfies by itself: the pattern puts the
        patch tool's name in the call input, and this repo's own source hands
        the marker back in the output. A real rejection prints the marker at
        the START of a line ("Script error:\\npatch rejected: …"); an rg hit
        line never does — it is prefixed by `path:lineno:`."""
        env = env_factory(active="claude")

        def fake_run(argv, cwd=None, **kw):
            Path(argv[argv.index("-o") + 1]).write_text("Found 2 matches.")
            sub_path = paths.find_codex_rollout(
                argv[argv.index("resume") + 1])
            write_line(sub_path, {
                "timestamp": "t", "type": "response_item",
                "payload": {"type": "custom_tool_call", "call_id": "c-rg2",
                            "name": "exec",
                            "input": "rg -n 'apply_patch|patch rejected' src/"}})
            write_line(sub_path, {
                "timestamp": "t", "type": "response_item",
                "payload": {"type": "custom_tool_call_output",
                            "call_id": "c-rg2", "output": [
                                {"type": "input_text",
                                 "text": "src/tandem/ops.py:404:"
                                         'PATCH_REJECTED = "patch rejected"\n'
                                         "src/tandem/ops.py:437:carrying "
                                         "`patch rejected`. The\n"}]}})
            return _R(0)

        monkeypatch.setattr(ops, "_run", fake_run)
        ops.run_sub(env.store, env.session, "t", quiet=True)
        out = capsys.readouterr().out
        assert "Found 2 matches." in out
        assert ops.BLOCKED_HEADER not in out

    def test_rejected_patch_with_unparseable_targets_still_reports(
            self, env_factory, monkeypatch, capsys):
        """The narrowing gates on the CALL looking like a patch, not on the
        targets parsing: a real rejection whose paths cannot be extracted must
        still reach the orchestrator, as "(unknown path)"."""
        env = env_factory(active="claude")

        def fake_run(argv, cwd=None, **kw):
            Path(argv[argv.index("-o") + 1]).write_text("could not write")
            sub_path = paths.find_codex_rollout(
                argv[argv.index("resume") + 1])
            write_line(sub_path, {
                "timestamp": "t", "type": "response_item",
                "payload": {"type": "custom_tool_call", "call_id": "c-p",
                            "name": "exec",
                            "input": "await tools.apply_patch(supplied);\n"}})
            write_line(sub_path, {
                "timestamp": "t", "type": "response_item",
                "payload": {"type": "custom_tool_call_output",
                            "call_id": "c-p",
                            "output": "Script error:\npatch rejected: writing "
                                      "is blocked by read-only sandbox"}})
            return _R(0)

        monkeypatch.setattr(ops, "_run", fake_run)
        ops.run_sub(env.store, env.session, "t", quiet=True)
        out = capsys.readouterr().out
        assert ops.BLOCKED_HEADER in out
        assert "(unknown path)" in out
