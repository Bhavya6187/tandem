"""Shell loop tests with a fake harness runner and scripted input."""

import sqlite3

import pytest

from tandem import paths, shell
from tandem.state import StateStore


class FakeMem:
    actions: list = []
    warnings: list = []


@pytest.fixture
def sess(tmp_path, monkeypatch):
    monkeypatch.setenv("TANDEM_HOME", str(tmp_path / ".tandem"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / ".codex"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / ".claude"))
    with StateStore() as store:
        return store.create_session(str(tmp_path / "proj"), "claude", "c-id", "x-id")


def scripted(*lines):
    it = iter(lines)

    def input_fn(prompt):
        item = next(it, EOFError)
        if item is EOFError:
            raise EOFError
        if item is KeyboardInterrupt:
            raise KeyboardInterrupt
        return item

    return input_fn


def fake_runner(log, codes=None):
    codes = list(codes or [])

    def run_harness(session):
        log.append(session.active)
        return codes.pop(0) if codes else 0

    return run_harness


def test_switch_flips_and_reenters(sess, monkeypatch):
    def fake_switch(store, session):
        new = "codex" if session.active == "claude" else "claude"
        store.set_active(session.tandem_id, new)
        return new, [], FakeMem()

    monkeypatch.setattr(shell.ops, "switch_session", fake_switch)
    log = []
    code = shell.run_shell(
        sess.tandem_id, None, input_fn=scripted("switch", "exit"),
        run_harness=fake_runner(log),
    )
    assert log == ["claude", "codex"]
    assert code == 0


def test_enter_and_resume_reenter_without_flip(sess):
    log = []
    shell.run_shell(
        sess.tandem_id, None, input_fn=scripted("", "resume", "exit"),
        run_harness=fake_runner(log),
    )
    assert log == ["claude", "claude", "claude"]


def test_resume_with_id_rejected_at_prompt(sess, capsys):
    log = []
    shell.run_shell(
        sess.tandem_id, None, input_fn=scripted("resume abc123"),
        run_harness=fake_runner(log),
    )
    out = capsys.readouterr().out
    assert "tandem resume <id>" in out
    assert log == ["claude"]  # only the initial entry


def test_exit_prints_resume_hint_and_last_code(sess, capsys):
    log = []
    code = shell.run_shell(
        sess.tandem_id, None, input_fn=scripted("exit"),
        run_harness=fake_runner(log, codes=[7]),
    )
    assert code == 7
    assert f"to continue this session: tandem resume {sess.tandem_id}" in (
        capsys.readouterr().out
    )


def test_eof_exits_and_ctrl_c_does_not(sess):
    log = []
    shell.run_shell(
        sess.tandem_id, None,
        input_fn=scripted(KeyboardInterrupt, ""),  # ^C, then Enter, then EOF
        run_harness=fake_runner(log),
    )
    assert log == ["claude", "claude"]  # survived ^C, re-entered on Enter


def test_unknown_input_prints_command_list(sess, capsys):
    log = []
    shell.run_shell(
        sess.tandem_id, None, input_fn=scripted("frobnicate"),
        run_harness=fake_runner(log),
    )
    assert "commands:" in capsys.readouterr().out


def test_unbalanced_quote_does_not_kill_the_prompt(sess, capsys):
    log = []
    shell.run_shell(
        sess.tandem_id, None, input_fn=scripted('status "oops'),
        run_harness=fake_runner(log),
    )
    out = capsys.readouterr().out
    assert "could not parse" in out
    assert f"tandem resume {sess.tandem_id}" in out  # survived to a clean exit


def test_launch_options_neither_pair_nor_nest(sess, capsys, monkeypatch):
    """`--active codex` at the prompt must not re-enter the group's
    invoke_without_command path (pairing a fresh session and nesting a
    second shell inside this one)."""
    from tandem import cli

    monkeypatch.setattr(cli, "_cwd", lambda: sess.cwd)
    monkeypatch.setattr(cli, "_check_versions", lambda warn_only=False: {})
    nested = []
    monkeypatch.setattr(cli, "_enter_session", lambda s: (nested.append(s), 0)[1])
    log = []
    shell.run_shell(
        sess.tandem_id, None,
        input_fn=scripted("--active codex", "--active=codex", "--"),
        run_harness=fake_runner(log),
    )
    out = capsys.readouterr().out
    assert nested == []  # no shell nested inside the shell
    assert "paired" not in out  # no orphan session
    assert log == ["claude"]  # only the initial entry
    assert "commands:" in out  # the user gets the command list instead
    with StateStore() as store:
        assert store.latest_session_for_cwd(sess.cwd).tandem_id == sess.tandem_id


def test_non_click_exception_keeps_the_prompt_alive(sess, capsys, monkeypatch):
    from tandem import cli

    def boom(store):
        raise OSError("transcript directory vanished")

    monkeypatch.setattr(cli, "_require_session", boom)
    log = []
    shell.run_shell(
        sess.tandem_id, None, input_fn=scripted("status", ""),
        run_harness=fake_runner(log),
    )
    cap = capsys.readouterr()
    # the exception type is part of the message: str(KeyError()) is empty
    assert "command failed: OSError: transcript directory vanished" in cap.err
    assert log == ["claude", "claude"]  # loop survived, Enter still re-enters
    assert f"to continue this session: tandem resume {sess.tandem_id}" in cap.out


def test_status_dispatches_through_cli(sess, capsys, monkeypatch):
    from tandem import cli

    monkeypatch.setattr(cli, "_cwd", lambda: sess.cwd)
    monkeypatch.setattr(
        cli, "_check_versions",
        lambda warn_only=False: {"claude": "2.1.220", "codex": "0.145.0"},
    )
    shell.run_shell(
        sess.tandem_id, None, input_fn=scripted("status"),
        run_harness=fake_runner([]),
    )
    # the one-shot status implementation ran (ops.unsynced_lines returns 0
    # for a missing transcript, so no files are needed)
    assert f"tandem session {sess.tandem_id}" in capsys.readouterr().out


def test_prompt_shows_active_harness(sess):
    prompts = []

    def input_fn(prompt):
        prompts.append(prompt)
        raise EOFError

    shell.run_shell(sess.tandem_id, None, input_fn=input_fn,
                    run_harness=fake_runner([]))
    assert prompts == ["tandem (claude)> "]


class Rival:
    """A second `tandem` in the same directory, started while this shell sits
    inside its harness: by the time the user types a command it — not the
    shell's own session — is the most-recently-used session for the cwd."""

    def __init__(self, cwd):
        with StateStore() as store:
            self.tandem_id = store.create_session(cwd, "codex", "c-2", "x-2").tandem_id
        self.mru_at_prompt = None

    def take_over(self, session):
        """Stands in for run_harness: the rival launches while we are busy."""
        with StateStore() as store:
            store.touch_used(self.tandem_id)
            self.mru_at_prompt = store.latest_session_for_cwd(session.cwd).tandem_id
        return 0


def test_dispatch_targets_this_shell_not_the_cwd_mru(sess, capsys, monkeypatch):
    """A second `tandem` in the same directory becomes the most-recently-used
    session for that cwd; commands typed in the older shell must keep acting
    on the older session (`run --on` there would otherwise inject a model turn
    into someone else's transcripts)."""
    from tandem import cli

    monkeypatch.setattr(cli, "_cwd", lambda: sess.cwd)
    monkeypatch.setattr(cli, "_check_versions", lambda warn_only=False: {})
    rival = Rival(sess.cwd)
    shell.run_shell(
        sess.tandem_id, None, input_fn=scripted("status"),
        run_harness=rival.take_over,
    )
    out = capsys.readouterr().out
    assert rival.mru_at_prompt == rival.tandem_id  # the setup really did shadow us
    assert f"tandem session {sess.tandem_id}" in out
    assert rival.tandem_id not in out
    assert cli._SESSION_ID is None  # override cleared after dispatch


def test_doctor_at_the_prompt_targets_this_shell(sess, capsys, monkeypatch):
    from tandem import cli

    monkeypatch.setattr(cli, "_cwd", lambda: sess.cwd)
    monkeypatch.setattr(
        "tandem.compat.detect_cli_version",
        lambda binary: {"claude": "2.1.220", "codex": "0.145.0"}.get(binary),
    )
    rival = Rival(sess.cwd)
    shell.run_shell(
        sess.tandem_id, None, input_fn=scripted("doctor"),
        run_harness=rival.take_over,
    )
    out = capsys.readouterr().out
    assert rival.mru_at_prompt == rival.tandem_id
    assert f"paired session {sess.tandem_id}" in out
    assert rival.tandem_id not in out


def test_failed_reentry_returns_to_the_prompt(sess, capsys):
    """The harness binary vanishing mid-shell must not kill the session."""
    calls = []

    def run_harness(session):
        calls.append(session.active)
        if len(calls) == 2:
            raise FileNotFoundError("claude: command not found")
        return 0

    shell.run_shell(
        sess.tandem_id, None, input_fn=scripted("", "", "exit"),
        run_harness=run_harness,
    )
    cap = capsys.readouterr()
    assert len(calls) == 3  # entry, failed re-entry, then a working one
    assert "could not run the harness: FileNotFoundError" in cap.err
    assert f"to continue this session: tandem resume {sess.tandem_id}" in cap.out


def test_resume_hint_prints_even_when_the_loop_raises(sess, capsys):
    """The hint is the only place the id is shown, so it must survive an
    unexpected exception escaping the loop body."""
    def input_fn(prompt):
        raise RuntimeError("terminal went away")

    with pytest.raises(RuntimeError):
        shell.run_shell(sess.tandem_id, None, input_fn=input_fn,
                        run_harness=fake_runner([]))
    assert f"to continue this session: tandem resume {sess.tandem_id}" in (
        capsys.readouterr().out
    )


def test_vanished_session_row_leaves_the_loop_cleanly(sess, capsys):
    def run_harness(session):
        conn = sqlite3.connect(paths.state_db_path())
        with conn:
            conn.execute(
                "DELETE FROM sessions WHERE tandem_id = ?", (sess.tandem_id,)
            )
        conn.close()
        return 0

    shell.run_shell(
        sess.tandem_id, None, input_fn=scripted("", "exit"),
        run_harness=run_harness,
    )
    cap = capsys.readouterr()
    assert "no longer in the state store" in cap.err  # not an AttributeError
    assert f"tandem resume {sess.tandem_id}" in cap.out


def _capture_oneoff(monkeypatch, sess):
    from tandem import cli, ops

    seen = []
    monkeypatch.setattr(cli, "_cwd", lambda: sess.cwd)
    monkeypatch.setattr(
        ops, "run_oneoff",
        lambda store, session, target, prompt: seen.append(
            (session.tandem_id, target, prompt)
        ) or 0,
    )
    return seen


def test_run_prompt_with_an_apostrophe_is_literal_text(sess, capsys, monkeypatch):
    seen = _capture_oneoff(monkeypatch, sess)
    rival = Rival(sess.cwd)  # a real model turn must not land in its files
    shell.run_shell(
        sess.tandem_id, None,
        input_fn=scripted("run --on codex what's wrong with test_foo?"),
        run_harness=rival.take_over,
    )
    assert rival.mru_at_prompt == rival.tandem_id
    assert seen == [(sess.tandem_id, "codex", "what's wrong with test_foo?")]
    assert "could not parse" not in capsys.readouterr().out


def test_run_quoted_prompt_keeps_one_shot_meaning(sess, monkeypatch):
    seen = _capture_oneoff(monkeypatch, sess)
    shell.run_shell(
        sess.tandem_id, None,
        input_fn=scripted(
            'run --on codex "why is this flaky?"',
            "run --on=codex second opinion, please",
        ),
        run_harness=fake_runner([]),
    )
    assert [p for _, _, p in seen] == [
        "why is this flaky?",          # outer quotes stripped, as one-shot
        "second opinion, please",      # --on=<harness> form
    ]


def test_run_prompt_starting_with_a_dash_is_not_read_as_an_option(sess, monkeypatch):
    seen = _capture_oneoff(monkeypatch, sess)
    shell.run_shell(
        sess.tandem_id, None,
        input_fn=scripted("run --on codex --help me read this stack trace"),
        run_harness=fake_runner([]),
    )
    assert [p for _, _, p in seen] == ["--help me read this stack trace"]


def test_malformed_run_still_gets_click_usage_error(sess, capsys, monkeypatch):
    from tandem import cli

    monkeypatch.setattr(cli, "_cwd", lambda: sess.cwd)
    shell.run_shell(
        sess.tandem_id, None, input_fn=scripted("run", "run --on codex"),
        run_harness=fake_runner([]),
    )
    cap = capsys.readouterr()
    assert "--on" in cap.err or "--on" in cap.out  # click's own usage error
    assert "commands:" in cap.out


def test_prompt_switch_reports_like_the_one_shot(sess, capsys, monkeypatch):
    """Display names, memory actions and the doctor advisory — the prompt is
    the primary switch path, so it must not report less than `tandem switch`."""
    class Mem:
        actions = ["wrote shared block into AGENTS.md"]
        warnings = ["CLAUDE.md has no tandem markers; read-only"]

    problems = ["transcript for newly active harness does not exist yet"]

    def fake_switch(store, session):
        store.set_active(session.tandem_id, "codex")
        return "codex", problems, Mem()

    monkeypatch.setattr(shell.ops, "switch_session", fake_switch)
    shell.run_shell(
        sess.tandem_id, None, input_fn=scripted("switch", "exit"),
        run_harness=fake_runner([]),
    )
    cap = capsys.readouterr()
    assert "active harness: Claude Code -> Codex CLI" in cap.out
    assert "memory: wrote shared block into AGENTS.md" in cap.out
    assert "memory: CLAUDE.md has no tandem markers" in cap.err
    assert "transcript for newly active harness does not exist yet" in cap.err
    assert "run `tandem doctor` for details." in cap.err
    # the prompt re-enters the harness itself; no resume instruction
    assert "Run `tandem resume` to continue" not in cap.out
