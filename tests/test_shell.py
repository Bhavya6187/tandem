"""Shell loop tests with a fake harness runner and scripted input."""

import pytest

from tandem import shell
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
        sess.tandem_id, None, input_fn=scripted('run --on codex "oops'),
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
    assert "command failed: transcript directory vanished" in cap.err
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
