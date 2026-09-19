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


@pytest.fixture
def chatted(monkeypatch):
    """Sessions handed to the chat window, which is patched away."""
    calls = []
    monkeypatch.setattr("tandem.chat.window.run_chat",
                        lambda session, store, cfg, **kw: (calls.append(session), 0)[1])
    return calls


def test_version_reports_installed_dist():
    # The dist is named tandem-cli, not tandem; --version must come from
    # tandem.__version__ or it crashes in venvs without a "tandem" dist.
    r = click.testing.CliRunner().invoke(cli.main, ["--version"])
    assert r.exit_code == 0
    assert tandem.__version__ in r.output


def test_native_pairs_fresh_each_launch(homes, ok_versions, entered):
    runner = click.testing.CliRunner()
    r1 = runner.invoke(cli.main, ["native"])
    r2 = runner.invoke(cli.main, ["native"])
    assert r1.exit_code == 0 and r2.exit_code == 0
    assert "paired" in r1.output and "claude active, codex shadow" in r1.output
    ids = {s.tandem_id for s in entered}
    assert len(ids) == 2  # two launches -> two distinct sessions


def test_native_active_codex_flips_roles(homes, ok_versions, entered):
    r = click.testing.CliRunner().invoke(cli.main, ["native", "--active", "codex"])
    assert r.exit_code == 0
    assert entered[0].active == "codex"
    assert "codex active, claude shadow" in r.output


def test_native_defaults_to_first_usable(homes, entered, monkeypatch):
    """No --active given: drop into the first usable harness in configured
    order rather than assuming claude is installed."""
    monkeypatch.setattr(
        cli, "_resolve_participants",
        lambda warn_only=False: (["codex", "claude"],
                                 {"claude": "2.1.220", "codex": "0.145.0"}),
    )
    r = click.testing.CliRunner().invoke(cli.main, ["native"])
    assert r.exit_code == 0
    assert entered[0].active == "codex"
    assert "codex active, claude shadow" in r.output


def test_native_explicit_active_not_usable_still_errors(homes, ok_versions, entered):
    r = click.testing.CliRunner().invoke(cli.main, ["native", "--active", "opencode"])
    assert r.exit_code == 1
    assert "not usable" in r.stderr
    assert entered == []


class _NoBin:
    display_name = "Claude Code"
    binary = "claude"
    install_hint = "npm install -g @anthropic-ai/claude-code"

    def detect_version(self):
        return None


@pytest.mark.parametrize("argv", [[], ["native"]])
def test_missing_binary_blocks_pairing(homes, entered, chatted, monkeypatch, argv):
    monkeypatch.setattr(cli, "get_adapter", lambda hid: _NoBin())
    r = click.testing.CliRunner().invoke(cli.main, argv)
    assert r.exit_code == 1
    assert entered == [] and chatted == []  # never paired, never entered
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


def test_native_offers_plugin_after_pairing(
        homes, ok_versions, entered, monkeypatch):
    from tandem import plugin_setup

    calls = []
    monkeypatch.setattr(plugin_setup, "offer_install",
                        lambda: calls.append(len(entered)))
    r = click.testing.CliRunner().invoke(cli.main, ["native"])
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


# -- tandem sessions ---------------------------------------------------------


def test_sessions_empty_store_hints_tandem(homes):
    r = click.testing.CliRunner().invoke(cli.main, ["sessions"])
    assert r.exit_code == 0
    assert "No tandem sessions yet" in r.output
    assert "Run `tandem` to start one" in r.output


def test_sessions_lists_newest_first_across_directories(homes, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    s1 = _mk_session(homes, n=1)
    s2 = _mk_session(other, active="codex", n=2)
    s3 = _mk_session(homes, n=3)
    with StateStore() as store:
        store.touch_used(s1.tandem_id)
    r = click.testing.CliRunner().invoke(cli.main, ["sessions"])
    assert r.exit_code == 0
    lines = [ln for ln in r.output.splitlines() if ln.strip()]
    rows = [ln for ln in lines if any(s.tandem_id in ln for s in (s1, s2, s3))]
    # column 0 is the this-directory marker; the id is the first field after it
    ids = [ln[1:].split()[0] for ln in rows]
    assert ids == [s1.tandem_id, s3.tandem_id, s2.tandem_id]
    by_id = dict(zip(ids, rows))
    assert by_id[s1.tandem_id][0] == "*"
    assert by_id[s3.tandem_id][0] == "*"
    assert by_id[s2.tandem_id][0] == " "
    # active harness, participants and directory are all visible
    fields = by_id[s2.tandem_id].split()
    assert "codex" in fields and fields.index("codex") < fields.index("claude+codex")
    assert str(other) in by_id[s2.tandem_id]
    assert "tandem resume" in r.output


def test_sessions_defaults_to_ten_and_honours_limit(homes):
    for i in range(12):
        _mk_session(homes, n=i)
    r = click.testing.CliRunner().invoke(cli.main, ["sessions"])
    assert r.exit_code == 0
    assert sum(ln.startswith("*") for ln in r.output.splitlines()) == 10
    r = click.testing.CliRunner().invoke(cli.main, ["sessions", "-n", "3"])
    assert r.exit_code == 0
    assert sum(ln.startswith("*") for ln in r.output.splitlines()) == 3


def test_sessions_marks_missing_directories(homes, tmp_path):
    gone = tmp_path / "gone"
    gone.mkdir()
    s = _mk_session(gone, n=1)
    gone.rmdir()
    r = click.testing.CliRunner().invoke(cli.main, ["sessions"])
    row = next(ln for ln in r.output.splitlines() if s.tandem_id in ln)
    assert "(missing)" in row


def test_sessions_shortens_home_to_tilde(homes, monkeypatch, tmp_path):
    home = tmp_path / "home"
    (home / "work").mkdir(parents=True)
    monkeypatch.setattr(cli.Path, "home", classmethod(lambda cls: home))
    s = _mk_session(home / "work", n=1)
    r = click.testing.CliRunner().invoke(cli.main, ["sessions"])
    row = next(ln for ln in r.output.splitlines() if s.tandem_id in ln)
    assert "~/work" in row
    assert str(home) not in row


def test_sessions_shows_relative_last_used(homes):
    from datetime import datetime, timedelta, timezone

    s = _mk_session(homes, n=1)
    last_used = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
    with StateStore() as store:
        store._conn.execute(
            "UPDATE sessions SET last_used_at = ? WHERE tandem_id = ?",
            (last_used, s.tandem_id),
        )
        store._conn.commit()
    r = click.testing.CliRunner().invoke(cli.main, ["sessions"])
    row = next(ln for ln in r.output.splitlines() if s.tandem_id in ln)
    assert "40d ago" in row
    assert last_used not in row


@pytest.mark.parametrize(
    "delta_s, expected",
    [(5, "just now"), (90, "1m ago"), (3 * 3600 + 5, "3h ago"),
     (2 * 86400 + 3600, "2d ago"), (40 * 86400, "40d ago")],
)
def test_ago_buckets(delta_s, expected):
    from datetime import datetime, timedelta, timezone

    now = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)
    then = (now - timedelta(seconds=delta_s)).isoformat()
    assert cli._ago(then, now=now) == expected


def test_ago_tolerates_garbage():
    assert cli._ago(None) == "?"
    assert cli._ago("not-a-date") == "?"


# -- tandem chat -------------------------------------------------------------


def test_chat_command_uses_the_latest_session_and_honors_on(env_factory, monkeypatch):
    from click.testing import CliRunner

    from tandem import cli

    env = env_factory(active="claude")
    seen = {}
    monkeypatch.setattr(cli, "_cwd", lambda: env.cwd)
    monkeypatch.setattr("tandem.chat.window.run_chat", lambda session, store, cfg, **kw: seen.setdefault("session", session) and 0)
    result = CliRunner().invoke(cli.main, ["chat", "--on", "codex"])
    assert result.exit_code == 0, result.output
    assert seen["session"].active == "codex"
    assert env.store.get_session(env.session.tandem_id).active == "codex"


def test_chat_rejects_a_non_participant(env_factory, monkeypatch):
    from click.testing import CliRunner

    from tandem import cli

    env = env_factory(active="claude")
    monkeypatch.setattr(cli, "_cwd", lambda: env.cwd)
    result = CliRunner().invoke(cli.main, ["chat", "--on", "opencode"])
    assert result.exit_code == 1 and "not a participant" in result.output


def test_chat_on_an_unusable_harness_never_becomes_the_fresh_session_active(
        homes, ok_versions):
    """`--on` names a harness this machine cannot run and the directory has
    no session yet: pairing must not stamp it as the active slot — a session
    whose active harness is not a participant can never run a turn."""
    r = click.testing.CliRunner().invoke(cli.main, ["chat", "--on", "opencode"])
    assert r.exit_code == 1 and "not a participant" in r.output
    with StateStore() as store:
        session = store.latest_session_for_cwd(str(homes))
    assert session.active in session.participants


# -- bare tandem is the chat window ------------------------------------------


def test_bare_tandem_opens_chat_on_the_latest_session(
        homes, ok_versions, entered, chatted):
    s = _mk_session(homes)
    r = click.testing.CliRunner().invoke(cli.main, [])
    assert r.exit_code == 0, r.output
    assert [c.tandem_id for c in chatted] == [s.tandem_id]
    assert entered == []  # the native frame is `tandem native` now


def test_bare_tandem_pairs_when_the_directory_has_no_session(
        homes, ok_versions, entered, chatted):
    r = click.testing.CliRunner().invoke(cli.main, [])
    assert r.exit_code == 0, r.output
    assert "paired" in r.output
    assert len(chatted) == 1 and entered == []


def test_bare_tandem_honors_on(homes, ok_versions, chatted):
    s = _mk_session(homes, active="claude")
    r = click.testing.CliRunner().invoke(cli.main, ["--on", "codex"])
    assert r.exit_code == 0, r.output
    assert chatted[0].active == "codex"
    with StateStore() as store:
        assert store.get_session(s.tandem_id).active == "codex"


@pytest.mark.parametrize("argv", [["--new"], ["chat", "--new"]])
def test_new_pairs_a_fresh_session_for_chat(homes, ok_versions, chatted, argv):
    old = _mk_session(homes)
    r = click.testing.CliRunner().invoke(cli.main, argv)
    assert r.exit_code == 0, r.output
    assert len(chatted) == 1 and chatted[0].tandem_id != old.tandem_id


def test_new_with_on_makes_it_the_fresh_session_active(homes, ok_versions, chatted):
    _mk_session(homes, active="claude")
    r = click.testing.CliRunner().invoke(cli.main, ["--new", "--on", "codex"])
    assert r.exit_code == 0, r.output
    assert chatted[0].active == "codex"


def test_group_active_points_at_native(homes, ok_versions, entered, chatted):
    """`tandem --active X` was the pre-chat-default spelling; it must name
    its replacements rather than die as an unknown option."""
    r = click.testing.CliRunner().invoke(cli.main, ["--active", "codex"])
    assert r.exit_code == 2
    assert "tandem native --active codex" in r.stderr
    assert "tandem --on codex" in r.stderr
    assert entered == [] and chatted == []
    with StateStore() as store:
        assert store.latest_session_for_cwd(str(homes)) is None


def test_chat_offers_plugin_only_when_it_pairs(homes, ok_versions, chatted, monkeypatch):
    from tandem import plugin_setup

    calls = []
    monkeypatch.setattr(plugin_setup, "offer_install",
                        lambda: calls.append(len(chatted)))
    runner = click.testing.CliRunner()
    assert runner.invoke(cli.main, []).exit_code == 0
    # offered once, after pairing but before the window opens
    assert calls == [0]
    assert runner.invoke(cli.main, []).exit_code == 0   # reuses the session
    assert calls == [0]


@pytest.mark.parametrize("argv, active, fresh", [
    (["--new", "chat"], "claude", True),
    (["--on", "codex", "chat"], "codex", False),
    (["--on", "claude", "chat", "--on", "codex"], "codex", False),
])
def test_chat_honors_group_options(homes, ok_versions, chatted, argv, active, fresh):
    old = _mk_session(homes)
    result = click.testing.CliRunner().invoke(cli.main, argv)
    assert result.exit_code == 0, result.output
    assert chatted[0].active == active
    assert (chatted[0].tandem_id != old.tandem_id) == fresh


@pytest.mark.parametrize("argv", [["--new", "status"], ["--on", "codex", "native"]])
def test_chat_options_rejected_for_other_commands(homes, ok_versions, entered, argv):
    result = click.testing.CliRunner().invoke(cli.main, argv)
    assert result.exit_code == 2
    assert "only apply" in result.output
    assert entered == []


def test_invalid_chat_harness_does_not_offer_install(homes, ok_versions, monkeypatch):
    calls = []
    monkeypatch.setattr("tandem.plugin_setup.offer_install", lambda: calls.append(True))
    result = click.testing.CliRunner().invoke(cli.main, ["--on", "opencode"])
    assert result.exit_code == 1
    assert "not a participant" in result.output
    assert calls == []
