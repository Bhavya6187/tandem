"""CLI-level tests: pairing, resume, one-shot plumbing. The interactive
entry (`_enter_session`) is monkeypatched; pairing runs for real under
tmp homes (same env vars as conftest.Env)."""

import click.testing
import pytest

from tandem import cli
from tandem.state import StateStore


@pytest.fixture
def homes(tmp_path, monkeypatch):
    monkeypatch.setenv("TANDEM_HOME", str(tmp_path / ".tandem"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / ".codex"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / ".claude"))
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.setattr(cli, "_cwd", lambda: str(proj))
    return proj


@pytest.fixture
def ok_versions(monkeypatch):
    monkeypatch.setattr(
        cli, "_check_versions",
        lambda warn_only=False: {"claude": "2.1.220", "codex": "0.145.0"},
    )


@pytest.fixture
def entered(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "_enter_session", lambda s: (calls.append(s), 0)[1])
    return calls


def test_bare_tandem_pairs_fresh_each_launch(homes, ok_versions, entered):
    runner = click.testing.CliRunner()
    r1 = runner.invoke(cli.main, [])
    r2 = runner.invoke(cli.main, [])
    assert r1.exit_code == 0 and r2.exit_code == 0
    assert "paired" in r1.output and "claude active, codex shadow" in r1.output
    ids = {s.tandem_id for s in entered}
    assert len(ids) == 2  # two launches -> two distinct sessions


def test_active_codex_flips_roles(homes, ok_versions, entered):
    r = click.testing.CliRunner().invoke(cli.main, ["--active", "codex"])
    assert r.exit_code == 0
    assert entered[0].active == "codex"
    assert "codex active, claude shadow" in r.output


class _NoBin:
    display_name = "Claude Code"
    binary = "claude"

    def detect_version(self):
        return None


def test_missing_binary_blocks_pairing(homes, entered, monkeypatch):
    monkeypatch.setattr(cli, "get_adapter", lambda hid: _NoBin())
    r = click.testing.CliRunner().invoke(cli.main, [])
    assert r.exit_code == 1
    assert entered == []  # never paired, never entered
    with StateStore() as store:
        assert store.latest_session_for_cwd(cli._cwd()) is None


def test_start_is_gone(homes):
    r = click.testing.CliRunner().invoke(cli.main, ["start"])
    assert r.exit_code == 2  # click usage error: no such command


def test_one_shot_without_session_hints_tandem(homes, ok_versions):
    r = click.testing.CliRunner().invoke(cli.main, ["status"])
    assert r.exit_code == 1
    # click >= 8.2 (repo has 8.4.2): err=True output lands in r.stderr
    assert "Run `tandem` to start one" in r.stderr


def _mk_session(cwd, active="claude", n=0):
    with StateStore() as store:
        return store.create_session(str(cwd), active, f"c-{n}", f"x-{n}")


def test_resume_picks_most_recently_used(homes, ok_versions, entered):
    s1 = _mk_session(homes, n=1)
    _mk_session(homes, n=2)
    with StateStore() as store:
        store.touch_used(s1.tandem_id)
    r = click.testing.CliRunner().invoke(cli.main, ["resume"])
    assert r.exit_code == 0
    assert entered[0].tandem_id == s1.tandem_id


def test_resume_by_id(homes, ok_versions, entered):
    s1 = _mk_session(homes, n=1)
    _mk_session(homes, n=2)
    r = click.testing.CliRunner().invoke(cli.main, ["resume", s1.tandem_id])
    assert r.exit_code == 0
    assert entered[0].tandem_id == s1.tandem_id
    with StateStore() as store:  # resume bumps last_used_at
        assert (
            store.latest_session_for_cwd(str(homes)).tandem_id == s1.tandem_id
        )


def test_resume_unknown_id_errors(homes, ok_versions, entered):
    r = click.testing.CliRunner().invoke(cli.main, ["resume", "nope00000000"])
    assert r.exit_code == 1
    assert entered == []


def test_resume_id_from_other_directory_errors(homes, ok_versions, entered, tmp_path):
    other_dir = tmp_path / "elsewhere"
    other_dir.mkdir()
    s = _mk_session(other_dir)
    r = click.testing.CliRunner().invoke(cli.main, ["resume", s.tandem_id])
    assert r.exit_code == 1
    assert str(other_dir) in r.stderr  # tells the user where it lives
    assert entered == []


def test_resume_with_no_sessions_hints_tandem(homes, ok_versions, entered):
    r = click.testing.CliRunner().invoke(cli.main, ["resume"])
    assert r.exit_code == 1
    assert "Run `tandem` to start one" in r.stderr


def test_resume_warns_but_proceeds_without_binaries(homes, entered, monkeypatch):
    s = _mk_session(homes)
    monkeypatch.setattr(cli, "get_adapter", lambda hid: _NoBin())
    r = click.testing.CliRunner().invoke(cli.main, ["resume"])
    assert r.exit_code == 0  # warn-only: resume is not blocked like pairing
    assert "warning:" in r.stderr
    assert entered[0].tandem_id == s.tandem_id


def test_doctor_no_session_hints_tandem(homes, ok_versions):
    r = click.testing.CliRunner().invoke(cli.main, ["doctor"])
    assert r.exit_code == 1
    assert "tandem start" not in r.output
    assert "run `tandem` to start one" in r.output


def test_one_shot_switch_hints_resume(homes, ok_versions, monkeypatch):
    class Mem:
        actions: list = []
        warnings: list = []

    s = _mk_session(homes)

    def fake_switch(store, session):
        store.set_active(session.tandem_id, "codex")
        return "codex", [], Mem()

    monkeypatch.setattr("tandem.ops.switch_session", fake_switch)
    r = click.testing.CliRunner().invoke(cli.main, ["switch"])
    assert r.exit_code == 0
    assert "tandem resume" in r.output
    assert "Run `tandem` to continue" not in r.output
