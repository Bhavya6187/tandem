"""CLI-level tests: pairing, resume, one-shot plumbing. The interactive
entry (`_enter_session`) is monkeypatched; pairing runs for real under
tmp homes (same env vars as conftest.Env)."""

import click.testing
import pytest

import tandem
from tandem import cli, compat
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
        cli, "_resolve_participants",
        lambda warn_only=False: (["claude", "codex"],
                                 {"claude": "2.1.220", "codex": "0.145.0"}),
    )


@pytest.fixture
def entered(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "_enter_session", lambda s: (calls.append(s), 0)[1])
    return calls


def test_version_reports_installed_dist():
    # The dist is named tandem-cli, not tandem; --version must come from
    # tandem.__version__ or it crashes in venvs without a "tandem" dist.
    r = click.testing.CliRunner().invoke(cli.main, ["--version"])
    assert r.exit_code == 0
    assert tandem.__version__ in r.output


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
        return store.create_session(str(cwd), active, ["claude", "codex"],
                                    {"claude": f"c-{n}", "codex": f"x-{n}"})


def test_enter_session_runs_the_flip_loop(homes, monkeypatch):
    """Every entry point funnels through `_enter_session`, and every other
    test here patches it away — so pin the one seam it hides: the flip loop
    gets this session's id, and its exit code is what the CLI exits with."""
    from tandem import flip

    seen = []

    def fake_run_session(tandem_id, sink_factory):
        seen.append((tandem_id, sink_factory))
        return 3

    monkeypatch.setattr(flip, "run_session", fake_run_session)
    s = _mk_session(homes)
    assert cli._enter_session(s) == 3      # code propagates to sys.exit
    assert [t for t, _ in seen] == [s.tandem_id]
    assert seen[0][1] is cli._default_sink_factory


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


def test_resume_without_two_usable_harnesses_is_fatal(homes, entered, monkeypatch):
    """Resume recomputes availability (spec: Participants/Resume); fewer
    than two usable survivors is fatal — nothing could run anyway."""
    _mk_session(homes)
    monkeypatch.setattr(cli, "get_adapter", lambda hid: _NoBin())
    r = click.testing.CliRunner().invoke(cli.main, ["resume"])
    assert r.exit_code == 1
    assert "warning:" in r.stderr        # availability reported before the exit
    assert entered == []


def test_doctor_no_session_hints_tandem(homes, monkeypatch):
    # run_doctor probes versions through the adapters, not cli._check_versions,
    # so patch the detection itself: no real `claude`/`codex` subprocess.
    monkeypatch.setattr(
        compat, "detect_cli_version",
        lambda binary: {"claude": "2.1.220", "codex": "0.145.0"}.get(binary),
    )
    r = click.testing.CliRunner().invoke(cli.main, ["doctor"])
    assert r.exit_code == 1
    assert "tandem start" not in r.output
    assert "run `tandem` to start one" in r.output


def test_plugin_install_cmd_exit_codes(monkeypatch):
    from tandem import plugin_setup

    monkeypatch.setattr(plugin_setup, "install_plugin", lambda: True)
    r = click.testing.CliRunner().invoke(cli.main, ["plugin", "install"])
    assert r.exit_code == 0

    monkeypatch.setattr(plugin_setup, "install_plugin", lambda: False)
    r = click.testing.CliRunner().invoke(cli.main, ["plugin", "install"])
    assert r.exit_code == 1


def test_bare_tandem_offers_plugin_after_pairing(
        homes, ok_versions, entered, monkeypatch):
    from tandem import plugin_setup

    calls = []
    monkeypatch.setattr(plugin_setup, "offer_install",
                        lambda: calls.append(len(entered)))
    r = click.testing.CliRunner().invoke(cli.main, [])
    assert r.exit_code == 0
    # offered exactly once, after pairing but before entering the session
    assert calls == [0]
    assert len(entered) == 1


def test_run_on_nonparticipant_is_a_clean_error(homes, ok_versions, monkeypatch):
    """`run --on` accepts every supported name at the Click layer, but a
    target outside this session's participants (e.g. opencode in a PR-1
    build, or any dropped member) must be a normal error — never a
    get_adapter KeyError traceback."""
    _mk_session(homes)
    r = click.testing.CliRunner().invoke(cli.main, ["run", "--on", "opencode", "hi"])
    assert r.exit_code == 1
    assert "not a participant" in r.stderr
    assert r.exception is None or isinstance(r.exception, SystemExit)
