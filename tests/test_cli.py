"""CLI-level tests: pairing, resume, one-shot plumbing. The interactive
entry (`_enter_session`) is monkeypatched; pairing runs for real under
tmp homes (same env vars as conftest.Env)."""

import json

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
    """Sessions handed to the chat window, which is patched away. The stand-in
    runs what a first prompt would (`first_turn`), so the session is a used
    one; `chat_left_idle` is the window nobody typed into."""
    calls = []

    def run_chat(session, store, cfg, *, first_turn=None, **kw):
        calls.append(session)
        if first_turn is not None:
            first_turn()
        return 0

    monkeypatch.setattr("tandem.chat.window.run_chat", run_chat)
    return calls


@pytest.fixture
def chat_left_idle(monkeypatch):
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


def test_native_resume_picks_most_recently_used(homes, ok_versions, entered):
    s1 = _mk_session(homes, n=1)
    _mk_session(homes, n=2)
    with StateStore() as store:
        store.touch_used(s1.tandem_id)
    r = click.testing.CliRunner().invoke(cli.main, ["native", "resume"])
    assert r.exit_code == 0
    assert entered[0].tandem_id == s1.tandem_id


def test_native_resume_by_id(homes, ok_versions, entered):
    s1 = _mk_session(homes, n=1)
    _mk_session(homes, n=2)
    r = click.testing.CliRunner().invoke(cli.main, ["native", "resume", s1.tandem_id])
    assert r.exit_code == 0
    assert entered[0].tandem_id == s1.tandem_id
    with StateStore() as store:  # resume bumps last_used_at
        assert (
            store.latest_session_for_cwd(str(homes)).tandem_id == s1.tandem_id
        )


def test_native_resume_unknown_id_errors(homes, ok_versions, entered):
    r = click.testing.CliRunner().invoke(cli.main, ["native", "resume", "nope00000000"])
    assert r.exit_code == 1
    assert entered == []


def test_native_resume_id_from_other_directory_errors(homes, ok_versions, entered, tmp_path):
    other_dir = tmp_path / "elsewhere"
    other_dir.mkdir()
    s = _mk_session(other_dir)
    r = click.testing.CliRunner().invoke(cli.main, ["native", "resume", s.tandem_id])
    assert r.exit_code == 1
    assert str(other_dir) in r.stderr  # tells the user where it lives
    assert entered == []


def test_native_resume_with_no_sessions_hints_tandem(homes, ok_versions, entered):
    r = click.testing.CliRunner().invoke(cli.main, ["native", "resume"])
    assert r.exit_code == 1
    assert "Run `tandem` to start one" in r.stderr


def test_native_resume_without_two_usable_harnesses_is_fatal(homes, entered, monkeypatch):
    """Resume recomputes availability (spec: Participants/Resume); fewer
    than two usable survivors is fatal — nothing could run anyway."""
    _mk_session(homes)
    monkeypatch.setattr(cli, "get_adapter", lambda hid: _NoBin())
    r = click.testing.CliRunner().invoke(cli.main, ["native", "resume"])
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


# -- session titles ------------------------------------------------------------
# A row's TITLE is the first real user prompt in one of its transcripts, so two
# sessions in the same directory stop looking identical.


def _write_claude_transcript(cwd, sid, entries):
    from conftest import claude_user
    from tandem import paths

    path = paths.claude_transcript_path(str(cwd), sid)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        for e in entries:
            f.write(json.dumps(claude_user(e) if isinstance(e, str) else e) + "\n")
    return path


def _write_codex_rollout(sid, prompts):
    from tandem import paths

    path = (paths.codex_sessions_dir() / "2026" / "09" / "25"
            / f"rollout-2026-09-25T00-00-00-{sid}.jsonl")
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [{"timestamp": "t", "type": "session_meta",
              "payload": {"id": sid, "cwd": "/x", "originator": "codex_cli_rs"}}]
    for text in prompts:
        lines.append({"timestamp": "t", "type": "event_msg",
                      "payload": {"type": "task_started", "turn_id": "x"}})
        lines.append({"timestamp": "t", "type": "event_msg",
                      "payload": {"type": "user_message", "message": text}})
    with open(path, "w") as f:
        for ln in lines:
            f.write(json.dumps(ln) + "\n")
    return path


def _row_for(output, tandem_id):
    return next(ln for ln in output.splitlines() if tandem_id in ln)


def test_sessions_shows_first_prompt_as_title(homes, monkeypatch):
    monkeypatch.setenv("COLUMNS", "160")     # room for the whole title
    s = _mk_session(homes, n=1)
    _write_claude_transcript(homes, "c-1", ["fix the flaky sync test", "and then ship it"])
    r = click.testing.CliRunner().invoke(cli.main, ["sessions"])
    assert r.exit_code == 0, r.output
    header = next(ln for ln in r.output.splitlines() if "ID" in ln and "DIRECTORY" in ln)
    assert "TITLE" in header
    assert header.index("TITLE") < header.index("DIRECTORY")
    row = _row_for(r.output, s.tandem_id)
    assert "fix the flaky sync test" in row
    assert "and then ship it" not in row
    # the title sits between the participants and the directory
    assert row.index("claude+codex") < row.index("fix the flaky") < row.index(str(homes))


def test_session_title_skips_tandem_notes_and_strips_attribution(homes, monkeypatch):
    from tandem.constants import SEED_NOTE

    monkeypatch.setenv("COLUMNS", "160")
    s = _mk_session(homes, n=1)
    seed = SEED_NOTE.format(tandem_id=s.tandem_id, other="codex")
    _write_claude_transcript(homes, "c-1", [seed, "[via codex] tighten the retry loop"])
    r = click.testing.CliRunner().invoke(cli.main, ["sessions"])
    row = _row_for(r.output, s.tandem_id)
    assert "tighten the retry loop" in row
    assert "[via codex]" not in row and "[tandem]" not in row


def test_session_title_falls_back_to_another_participant(homes, monkeypatch):
    # the active harness (claude) never ran: its transcript is missing;
    # codex holds the only prompt
    monkeypatch.setenv("COLUMNS", "160")
    s = _mk_session(homes, n=1)
    _write_codex_rollout("x-1", ["from the codex side"])
    r = click.testing.CliRunner().invoke(cli.main, ["sessions"])
    row = _row_for(r.output, s.tandem_id)
    assert "from the codex side" in row


def test_session_title_prefers_the_active_harness(homes, monkeypatch):
    monkeypatch.setenv("COLUMNS", "160")
    s = _mk_session(homes, active="codex", n=1)
    _write_claude_transcript(homes, "c-1", ["[via codex] mirrored copy"])
    _write_codex_rollout("x-1", ["the original words"])
    r = click.testing.CliRunner().invoke(cli.main, ["sessions"])
    row = _row_for(r.output, s.tandem_id)
    assert "the original words" in row
    assert "mirrored copy" not in row


def test_session_title_placeholder_when_no_turns(homes):
    s = _mk_session(homes, n=1)
    r = click.testing.CliRunner().invoke(cli.main, ["sessions"])
    assert r.exit_code == 0, r.output
    assert "(no turns yet)" in _row_for(r.output, s.tandem_id)


def test_session_title_is_one_line_and_truncated(homes, monkeypatch):
    monkeypatch.setenv("COLUMNS", "120")
    s = _mk_session(homes, n=1)
    # a pasted report opens with a rule: the title is the first line that
    # says something, not the first line that exists
    long = "-----\n\nfirst line   with  gaps " + "x" * 200 + "\nsecond line"
    _write_claude_transcript(homes, "c-1", [long])
    r = click.testing.CliRunner().invoke(cli.main, ["sessions"])
    row = _row_for(r.output, s.tandem_id)
    assert "first line with gaps" in row
    assert "-----" not in row
    assert "second line" not in row
    assert "…" in row
    assert "x" * 100 not in row
    assert str(homes) in row       # the directory survives after the cut


def test_session_title_never_tracebacks(homes, monkeypatch):
    s = _mk_session(homes, n=1)
    _write_claude_transcript(homes, "c-1", ["a prompt"])

    def boom(*a, **k):
        raise RuntimeError("adapter exploded")

    monkeypatch.setattr(cli, "get_adapter", boom)
    r = click.testing.CliRunner().invoke(cli.main, ["sessions"])
    assert r.exit_code == 0, r.output
    row = _row_for(r.output, s.tandem_id)
    assert " ? " in row and str(homes) in row


def test_chat_resume_picker_shows_titles(homes, ok_versions, chatted, monkeypatch):
    monkeypatch.setenv("COLUMNS", "160")
    s = _mk_session(homes, n=1)
    _write_claude_transcript(homes, "c-1", ["rename the bar meters"])
    r = click.testing.CliRunner().invoke(cli.main, ["resume"], input="0\n")
    assert s.tandem_id in r.output
    assert "rename the bar meters" in _row_for(r.output, s.tandem_id)
    assert chatted == []


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


def test_chat_resume_honors_on(env_factory, monkeypatch):
    from click.testing import CliRunner

    from tandem import cli

    env = env_factory(active="claude")
    seen = {}
    monkeypatch.setattr(cli, "_cwd", lambda: env.cwd)
    monkeypatch.setattr("tandem.chat.window.run_chat", lambda session, store, cfg, **kw: seen.setdefault("session", session) and 0)
    result = CliRunner().invoke(cli.main, ["resume", env.session.tandem_id, "--on", "codex"])
    assert result.exit_code == 0, result.output
    assert seen["session"].active == "codex"
    assert env.store.get_session(env.session.tandem_id).active == "codex"


def test_chat_rejects_a_non_participant(env_factory, monkeypatch):
    from click.testing import CliRunner

    from tandem import cli

    env = env_factory(active="claude")
    monkeypatch.setattr(cli, "_cwd", lambda: env.cwd)
    result = CliRunner().invoke(cli.main, ["resume", env.session.tandem_id, "--on", "opencode"])
    assert result.exit_code == 1 and "not a participant" in result.output


def test_chat_on_an_unusable_harness_never_becomes_the_fresh_session_active(
        homes, ok_versions):
    """`--on` names a harness this machine cannot run and the directory has
    no session yet: a session whose active harness is not a participant can
    never run a turn, so the launch is refused before any row is written."""
    r = click.testing.CliRunner().invoke(cli.main, ["--on", "opencode"])
    assert r.exit_code == 1 and "not a participant" in r.output
    assert "paired" not in r.output
    with StateStore() as store:
        assert store.latest_session_for_cwd(str(homes)) is None


# -- bare tandem is the chat window ------------------------------------------


def test_chat_launches_create_independent_sessions(
        homes, ok_versions, entered, chatted):
    s = _mk_session(homes)
    for _ in range(2):
        r = click.testing.CliRunner().invoke(cli.main, [])
        assert r.exit_code == 0, r.output
    assert len({s.tandem_id, *(c.tandem_id for c in chatted)}) == 3
    with StateStore() as store:
        assert len(store.list_sessions()) == 3
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
        assert store.get_session(s.tandem_id).active == "claude"
        assert store.get_session(chatted[0].tandem_id).active == "codex"


@pytest.mark.parametrize("argv", [[], ["--new"]])
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
    assert runner.invoke(cli.main, ["--continue"]).exit_code == 0
    assert calls == [0]


@pytest.mark.parametrize("argv, active", [
    (["--on", "codex", "resume"], "codex"),
    (["--on", "claude", "resume", "--on", "codex"], "codex"),
])
def test_resume_honors_group_on(homes, ok_versions, chatted, argv, active):
    old = _mk_session(homes)
    result = click.testing.CliRunner().invoke(cli.main, [*argv, old.tandem_id])
    assert result.exit_code == 0, result.output
    assert chatted[0].active == active
    assert chatted[0].tandem_id == old.tandem_id


@pytest.mark.parametrize("argv", [
    ["--new", "status"], ["--on", "codex", "native"], ["--continue", "native"],
])
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


def test_chat_resume_by_id_from_any_directory(homes, ok_versions, chatted, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    wanted = _mk_session(other, active="codex", n=1)
    newer = _mk_session(homes, n=2)
    with StateStore() as store:
        store.set_pin(wanted.tandem_id, "codex", "gpt-5.5")
    r = click.testing.CliRunner().invoke(cli.main, ["resume", wanted.tandem_id])
    assert r.exit_code == 0, r.output
    assert chatted[0].tandem_id == wanted.tandem_id
    assert chatted[0].cwd == str(other)
    assert chatted[0].native_session_ids == wanted.native_session_ids
    assert chatted[0].active == "codex"
    with StateStore() as store:
        assert store.get_pin(wanted.tandem_id, "codex") == "gpt-5.5"
        assert [s.tandem_id for s in store.list_sessions()] == [wanted.tandem_id, newer.tandem_id]


@pytest.mark.parametrize("argv", [["--continue"], ["-c"]])
def test_chat_continue_selects_last_used_across_directories(
        homes, ok_versions, chatted, tmp_path, argv):
    other = tmp_path / "other"
    other.mkdir()
    wanted = _mk_session(other, n=1)
    _mk_session(homes, n=2)
    with StateStore() as store:
        store.touch_used(wanted.tandem_id)
    r = click.testing.CliRunner().invoke(cli.main, argv)
    assert r.exit_code == 0, r.output
    assert chatted[0].tandem_id == wanted.tandem_id
    assert chatted[0].cwd == str(other)


def test_chat_resume_picker_includes_older_sessions_in_other_directories(
        homes, ok_versions, chatted, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    wanted = _mk_session(other)
    for i in range(12):
        _mk_session(homes, n=i + 1)
    r = click.testing.CliRunner().invoke(cli.main, ["resume"], input="14\n13\n")
    assert r.exit_code == 0, r.output
    assert wanted.tandem_id in r.output and str(other) in r.output
    assert chatted[0].tandem_id == wanted.tandem_id


@pytest.mark.parametrize("answer", ["0\n", ""])
def test_chat_resume_picker_can_cancel_without_mutating_sessions(
        homes, ok_versions, chatted, answer):
    old = _mk_session(homes)
    r = click.testing.CliRunner().invoke(cli.main, ["resume"], input=answer)
    assert r.exit_code in (0, 1), r.output
    assert chatted == []
    with StateStore() as store:
        assert store.list_sessions() == [old]


@pytest.mark.parametrize("argv", [["resume"], ["--continue"], ["resume", "unknown"]])
def test_chat_resume_never_silently_creates_a_session(homes, ok_versions, chatted, argv):
    r = click.testing.CliRunner().invoke(cli.main, argv)
    assert r.exit_code == 1, r.output
    assert "session" in r.output
    assert chatted == []
    with StateStore() as store:
        assert store.list_sessions() == []


def test_chat_resume_reports_missing_working_directory(homes, ok_versions, chatted, tmp_path):
    old = _mk_session(tmp_path / "gone")
    r = click.testing.CliRunner().invoke(cli.main, ["resume", old.tandem_id])
    assert r.exit_code == 1, r.output
    assert old.cwd in r.output and "directory" in r.output
    assert chatted == []
    with StateStore() as store:
        assert store.list_sessions() == [old]


@pytest.mark.parametrize("argv", [
    ["--new", "--continue"], ["--new", "resume"], ["--continue", "resume"],
])
def test_chat_rejects_conflicting_session_options_before_pairing(homes, chatted, argv):
    r = click.testing.CliRunner().invoke(cli.main, argv)
    assert r.exit_code == 2, r.output
    assert "cannot be combined" in r.output
    assert chatted == []
    with StateStore() as store:
        assert store.list_sessions() == []


def test_chat_command_is_removed(homes, chatted):
    result = click.testing.CliRunner().invoke(cli.main, ["chat"])
    assert result.exit_code == 2
    assert "No such command" in result.output
    assert chatted == []


def test_native_active_rejected_on_resume(homes, entered):
    old = _mk_session(homes)
    result = click.testing.CliRunner().invoke(
        cli.main, ["native", "--active", "codex", "resume", old.tandem_id])
    assert result.exit_code == 2
    assert entered == []
    with StateStore() as store:
        assert store.list_sessions() == [old]


# -- the session a chat child belongs to ----------------------------------------

def _two_sessions(proj):
    """Two chat windows opened in one directory: `newer` is what a cwd lookup
    finds, `older` is the window whose harness is asking."""
    with StateStore() as store:
        older = store.create_session(str(proj), "claude", ["claude", "codex"],
                                     {"claude": "c-old", "codex": "x-old"})
        newer = store.create_session(str(proj), "claude", ["claude", "codex"],
                                     {"claude": "c-new", "codex": "x-new"})
        store.touch_used(newer.tandem_id)
    return older, newer


def test_status_follows_the_session_named_by_the_environment(homes, ok_versions, monkeypatch):
    older, newer = _two_sessions(homes)
    monkeypatch.setenv("TANDEM_SESSION_ID", older.tandem_id)
    r = click.testing.CliRunner().invoke(cli.main, ["status"])
    assert r.exit_code == 0, r.output
    assert f"tandem session {older.tandem_id}" in r.output
    assert newer.tandem_id not in r.output


def test_unknown_session_in_the_environment_falls_back_to_cwd(homes, ok_versions, monkeypatch):
    _, newer = _two_sessions(homes)
    monkeypatch.setenv("TANDEM_SESSION_ID", "tdm-gone")
    r = click.testing.CliRunner().invoke(cli.main, ["status"])
    assert r.exit_code == 0, r.output
    assert f"tandem session {newer.tandem_id}" in r.output


def test_hook_route_stamps_consent_on_the_asking_window(homes, monkeypatch):
    import json

    from tandem import paths

    older, newer = _two_sessions(homes)
    monkeypatch.setenv("TANDEM_SESSION_ID", older.tandem_id)
    payload = {"tool_name": "Agent", "cwd": str(homes), "session_id": "s-1",
               "permission_mode": "acceptEdits",
               "tool_input": {"subagent_type": "tandem:gpt", "prompt": "do it"}}
    r = click.testing.CliRunner().invoke(cli.main, ["hook-route"], input=json.dumps(payload))
    assert r.exit_code == 0, r.output
    assert (paths.tandem_home() / "sandbox" / older.tandem_id).read_text() == "workspace-write"
    assert not (paths.tandem_home() / "sandbox" / newer.tandem_id).exists()


# -- a fresh chat session costs nothing until it is used ------------------------

def _native_files(tmp_path):
    return sorted(str(p.relative_to(tmp_path)) for root in (".claude", ".codex")
                  for p in (tmp_path / root).rglob("*.jsonl"))


def test_chat_opened_and_left_leaves_no_session_behind(homes, ok_versions, chat_left_idle, tmp_path):
    r = click.testing.CliRunner().invoke(cli.main, [])
    assert r.exit_code == 0, r.output
    assert len(chat_left_idle) == 1
    with StateStore() as store:
        assert store.list_sessions(limit=None) == []
        assert store.get_session(chat_left_idle[0].tandem_id) is None
    assert _native_files(tmp_path) == []


def test_chat_seeds_the_shadows_on_its_first_turn(homes, ok_versions, chatted, tmp_path):
    r = click.testing.CliRunner().invoke(cli.main, [])
    assert r.exit_code == 0, r.output
    with StateStore() as store:
        session = store.get_session(chatted[0].tandem_id)
    assert session is not None
    # claude is active and writes its own file on its first turn; codex is
    # the shadow, seeded with the note — what pairing used to do up front
    files = _native_files(tmp_path)
    assert len(files) == 1 and files[0].startswith(".codex/")
    assert session.native_id("codex") in files[0]


def test_chat_window_that_dies_before_a_turn_leaves_no_session_behind(
        homes, ok_versions, monkeypatch, tmp_path):
    def boom(session, store, cfg, **kw):
        raise OSError("no terminal")

    monkeypatch.setattr("tandem.chat.window.run_chat", boom)
    r = click.testing.CliRunner().invoke(cli.main, [])
    assert r.exit_code == 1 and isinstance(r.exception, OSError)
    with StateStore() as store:
        assert store.list_sessions(limit=None) == []
    assert _native_files(tmp_path) == []


def test_resumed_chat_left_idle_is_kept(homes, ok_versions, chatted, chat_left_idle):
    with StateStore() as store:
        kept = store.create_session(str(homes), "claude", ["claude", "codex"],
                                    {"claude": "c-1", "codex": "x-1"})
    r = click.testing.CliRunner().invoke(cli.main, ["resume", kept.tandem_id])
    assert r.exit_code == 0, r.output
    with StateStore() as store:
        assert store.get_session(kept.tandem_id) is not None


# -- a window killed before its first turn (a closed terminal tab: SIGHUP runs
#    no cleanup) leaves a row with no shadows; the next launch drops it ---------

def _dead_pid():
    import subprocess
    import sys

    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def _unused_marker(tandem_id):
    from tandem import paths

    return paths.unused_marker(tandem_id)


def test_open_unused_window_is_marked_with_its_pid(homes, ok_versions, monkeypatch):
    import os

    seen = {}

    def run_chat(session, store, cfg, *, first_turn=None, **kw):
        marker = _unused_marker(session.tandem_id)
        seen["open"] = marker.read_text()
        first_turn()
        seen["after_turn"] = marker.exists()
        return 0

    monkeypatch.setattr("tandem.chat.window.run_chat", run_chat)
    r = click.testing.CliRunner().invoke(cli.main, [])
    assert r.exit_code == 0, r.output
    assert seen == {"open": str(os.getpid()), "after_turn": False}


def test_window_left_idle_clears_its_marker(homes, ok_versions, chat_left_idle):
    click.testing.CliRunner().invoke(cli.main, [])
    assert not _unused_marker(chat_left_idle[0].tandem_id).exists()


def _abandon(homes, pid):
    with StateStore() as store:
        s = store.create_session(str(homes), "claude", ["claude", "codex"],
                                 {"claude": "c-dead", "codex": "x-dead"})
        store.touch_used(s.tandem_id)
    marker = _unused_marker(s.tandem_id)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(str(pid))
    return s


def test_continue_never_lands_on_a_window_that_died_unused(homes, ok_versions, chatted):
    dead = _abandon(homes, _dead_pid())
    r = click.testing.CliRunner().invoke(cli.main, ["--continue"])
    assert r.exit_code != 0 and "No tandem sessions yet" in r.output
    assert chatted == []
    with StateStore() as store:
        assert store.get_session(dead.tandem_id) is None
    assert not _unused_marker(dead.tandem_id).exists()


def test_sessions_list_drops_a_window_that_died_unused(homes, ok_versions):
    dead = _abandon(homes, _dead_pid())
    r = click.testing.CliRunner().invoke(cli.main, ["sessions"])
    assert r.exit_code == 0 and dead.tandem_id not in r.output


def test_unused_window_that_is_still_open_is_left_alone(homes, ok_versions, chatted):
    import os

    live = _abandon(homes, os.getpid())
    click.testing.CliRunner().invoke(cli.main, [])
    with StateStore() as store:
        assert store.get_session(live.tandem_id) is not None
    assert _unused_marker(live.tandem_id).exists()


# -- --skip-permissions -------------------------------------------------------


@pytest.fixture
def chat_cfgs(monkeypatch):
    """The config each chat window was opened with."""
    cfgs = []

    def run_chat(session, store, cfg, *, first_turn=None, **kw):
        cfgs.append(cfg)
        if first_turn is not None:
            first_turn()
        return 0

    monkeypatch.setattr("tandem.chat.window.run_chat", run_chat)
    return cfgs


@pytest.fixture
def native_skips(monkeypatch):
    """What `skip_permissions` resolved to as each native session was entered
    — the moment the flip loop starts building launches."""
    from tandem.config import load_skip_permissions

    seen = []
    monkeypatch.setattr(cli, "_enter_session", lambda s: (seen.append(load_skip_permissions()), 0)[1])
    return seen


@pytest.mark.parametrize("argv", [
    ["--skip-permissions"],
    ["resume", "{id}", "--skip-permissions"],
    ["--skip-permissions", "resume", "{id}"],
])
def test_skip_permissions_flag_reaches_the_chat_window(homes, ok_versions, chat_cfgs, argv):
    old = _mk_session(homes)
    argv = [a.format(id=old.tandem_id) for a in argv]
    result = click.testing.CliRunner().invoke(cli.main, argv)
    assert result.exit_code == 0, result.output
    assert [c.skip_permissions for c in chat_cfgs] == [True]


def test_chat_keeps_its_prompts_without_the_flag(homes, ok_versions, chat_cfgs):
    result = click.testing.CliRunner().invoke(cli.main, [])
    assert result.exit_code == 0, result.output
    assert [c.skip_permissions for c in chat_cfgs] == [False]


@pytest.mark.parametrize("argv", [
    ["native", "--skip-permissions"],
    ["native", "resume", "--skip-permissions"],
    ["--skip-permissions", "native"],
])
def test_skip_permissions_flag_reaches_native_sessions(homes, ok_versions, native_skips, argv):
    _mk_session(homes)
    result = click.testing.CliRunner().invoke(cli.main, argv)
    assert result.exit_code == 0, result.output
    assert native_skips == [True]


@pytest.mark.parametrize("argv", [
    ["--no-skip-permissions"],
    ["resume", "{id}", "--no-skip-permissions"],
])
def test_no_skip_permissions_flag_beats_the_config(homes, ok_versions, chat_cfgs, argv):
    from tandem import paths

    old = _mk_session(homes)
    paths.tandem_home().mkdir(parents=True, exist_ok=True)
    (paths.tandem_home() / "config.toml").write_text("skip_permissions = true\n")
    argv = [a.format(id=old.tandem_id) for a in argv]
    result = click.testing.CliRunner().invoke(cli.main, argv)
    assert result.exit_code == 0, result.output
    assert [c.skip_permissions for c in chat_cfgs] == [False]


def test_no_skip_permissions_flag_beats_the_config_in_native(homes, ok_versions, native_skips):
    from tandem import paths

    paths.tandem_home().mkdir(parents=True, exist_ok=True)
    (paths.tandem_home() / "config.toml").write_text("skip_permissions = true\n")
    result = click.testing.CliRunner().invoke(cli.main, ["native", "--no-skip-permissions"])
    assert result.exit_code == 0, result.output
    assert native_skips == [False]


def test_a_subcommand_flag_beats_the_group_flag(homes, ok_versions, chat_cfgs):
    # the nearer spelling wins, as `resume --on` beats a group `--on`
    old = _mk_session(homes)
    result = click.testing.CliRunner().invoke(
        cli.main, ["--skip-permissions", "resume", old.tandem_id, "--no-skip-permissions"])
    assert result.exit_code == 0, result.output
    assert [c.skip_permissions for c in chat_cfgs] == [False]


@pytest.mark.parametrize("argv", [
    ["--skip-permissions", "status"], ["--skip-permissions", "run", "hi"],
    ["--no-skip-permissions", "doctor"],
])
def test_skip_permissions_flag_rejected_where_no_session_opens(homes, ok_versions, entered, argv):
    from tandem.config import load_skip_permissions

    result = click.testing.CliRunner().invoke(cli.main, argv)
    assert result.exit_code == 2
    assert "--skip-permissions only applies" in result.output
    assert load_skip_permissions() is False


# -- --review -------------------------------------------------------------------


def _config(text: str) -> None:
    from tandem import paths

    paths.tandem_home().mkdir(parents=True, exist_ok=True)
    (paths.tandem_home() / "config.toml").write_text(text)


def test_review_flag_makes_the_other_harness_the_navigator(homes, ok_versions, chat_cfgs):
    # a fresh pairing starts on claude, so codex follows and comments
    result = click.testing.CliRunner().invoke(cli.main, ["--review"])
    assert result.exit_code == 0, result.output
    assert [c.navigator for c in chat_cfgs] == ["codex"]


def test_review_flag_follows_the_executing_harness(homes, ok_versions, chat_cfgs):
    result = click.testing.CliRunner().invoke(cli.main, ["--on", "codex", "--review"])
    assert result.exit_code == 0, result.output
    assert [c.navigator for c in chat_cfgs] == ["claude"]


@pytest.mark.parametrize("argv", [
    ["resume", "{id}", "--review"],
    ["--review", "resume", "{id}"],
])
def test_review_flag_reaches_a_resumed_window(homes, ok_versions, chat_cfgs, argv):
    old = _mk_session(homes, active="codex")
    argv = [a.format(id=old.tandem_id) for a in argv]
    result = click.testing.CliRunner().invoke(cli.main, argv)
    assert result.exit_code == 0, result.output
    assert [c.navigator for c in chat_cfgs] == ["claude"]


def test_review_flag_honours_a_configured_navigator_that_is_not_executing(
        homes, ok_versions, chat_cfgs):
    _config('[chat]\nnavigator = "codex"\nnavigator_model = "o3"\n')
    result = click.testing.CliRunner().invoke(cli.main, ["--on", "claude", "--review"])
    assert result.exit_code == 0, result.output
    assert [(c.navigator, c.navigator_model) for c in chat_cfgs] == [("codex", "o3")]


def test_review_flag_passes_over_a_configured_navigator_that_is_executing(
        homes, ok_versions, chat_cfgs):
    # the configured reviewer is the one taking the prompts: it cannot
    # review its own turns, so the other harness follows instead
    _config('[chat]\nnavigator = "codex"\n')
    result = click.testing.CliRunner().invoke(cli.main, ["--on", "codex", "--review"])
    assert result.exit_code == 0, result.output
    assert [c.navigator for c in chat_cfgs] == ["claude"]


def test_chat_has_no_navigator_without_the_flag(homes, ok_versions, chat_cfgs):
    result = click.testing.CliRunner().invoke(cli.main, [])
    assert result.exit_code == 0, result.output
    assert [c.navigator for c in chat_cfgs] == [""]


@pytest.mark.parametrize("argv", [
    ["--no-review"],
    ["resume", "{id}", "--no-review"],
])
def test_no_review_flag_beats_the_config(homes, ok_versions, chat_cfgs, argv):
    _config('[chat]\nnavigator = "codex"\n')
    old = _mk_session(homes)
    argv = [a.format(id=old.tandem_id) for a in argv]
    result = click.testing.CliRunner().invoke(cli.main, argv)
    assert result.exit_code == 0, result.output
    assert [c.navigator for c in chat_cfgs] == [""]


def test_no_review_flag_silences_a_rejected_config_value(homes, ok_versions, chat_cfgs):
    # off means off: the window must not print the "not supported" note
    _config('[chat]\nnavigator = "opencode"\n')
    result = click.testing.CliRunner().invoke(cli.main, ["--no-review"])
    assert result.exit_code == 0, result.output
    assert [(c.navigator, c.navigator_invalid) for c in chat_cfgs] == [("", "")]


def test_a_subcommand_review_flag_beats_the_group_flag(homes, ok_versions, chat_cfgs):
    old = _mk_session(homes)
    result = click.testing.CliRunner().invoke(
        cli.main, ["--review", "resume", old.tandem_id, "--no-review"])
    assert result.exit_code == 0, result.output
    assert [c.navigator for c in chat_cfgs] == [""]


def test_review_flag_fails_when_no_other_harness_can_review(homes, ok_versions, chat_cfgs):
    with StateStore() as store:
        lone = store.create_session(str(homes), "claude", ["claude"], {"claude": "c-9"})
    result = click.testing.CliRunner().invoke(cli.main, ["resume", lone.tandem_id, "--review"])
    assert result.exit_code == 1
    assert "--review needs a second participant" in result.output
    assert chat_cfgs == []


def test_review_flag_failure_leaves_no_fresh_session_behind(homes, chat_cfgs, monkeypatch):
    # the pairing exists before the reviewer is chosen: a launch refused for
    # want of one must not leave a row the next launch has to explain
    monkeypatch.setattr(cli, "_resolve_participants",
                        lambda warn_only=False: (["claude", "opencode"],
                                                 {"claude": "2.1.220", "opencode": "1.18.31"}))
    result = click.testing.CliRunner().invoke(cli.main, ["--review"])
    assert result.exit_code == 1
    assert "--review needs a second participant" in result.output
    assert "paired" not in result.output      # never announced, never memory-synced
    assert not (homes / "AGENTS.md").exists()
    assert chat_cfgs == []
    with StateStore() as store:
        assert store.list_sessions() == []


def test_review_flag_refusal_keeps_the_resumed_sessions_active_slot(homes, chat_cfgs, monkeypatch):
    monkeypatch.setattr(cli, "_resolve_participants",
                        lambda warn_only=False: (["claude", "opencode"],
                                                 {"claude": "2.1.220", "opencode": "1.18.31"}))
    with StateStore() as store:
        old = store.create_session(str(homes), "opencode", ["claude", "opencode"],
                                   {"claude": "c-9", "opencode": "o-9"})
    result = click.testing.CliRunner().invoke(
        cli.main, ["resume", old.tandem_id, "--on", "claude", "--review"])
    assert result.exit_code == 1
    assert "--review needs a second participant" in result.output
    with StateStore() as store:
        assert store.get_session(old.tandem_id).active == "opencode"


def test_review_flag_with_continue(homes, ok_versions, chat_cfgs):
    _mk_session(homes, active="codex")
    result = click.testing.CliRunner().invoke(cli.main, ["--continue", "--review"])
    assert result.exit_code == 0, result.output
    assert [c.navigator for c in chat_cfgs] == ["claude"]


def test_group_no_review_reaches_a_resumed_window(homes, ok_versions, chat_cfgs):
    _config('[chat]\nnavigator = "codex"\n')
    old = _mk_session(homes)
    result = click.testing.CliRunner().invoke(cli.main, ["--no-review", "resume", old.tandem_id])
    assert result.exit_code == 0, result.output
    assert [c.navigator for c in chat_cfgs] == [""]


def test_review_flag_falls_back_to_claude_when_opencode_executes(homes, chat_cfgs, monkeypatch):
    # a stored three-way session, resumed: a fresh pairing's first turn
    # would seed opencode's shadow through its binary, which CI lacks
    monkeypatch.setattr(cli, "_resolve_participants",
                        lambda warn_only=False: (["claude", "codex", "opencode"],
                                                 {"claude": "2.1.220", "codex": "0.145.0",
                                                  "opencode": "1.18.31"}))
    with StateStore() as store:
        old = store.create_session(str(homes), "opencode", ["claude", "codex", "opencode"],
                                   {"claude": "c-9", "codex": "x-9", "opencode": "o-9"})
    result = click.testing.CliRunner().invoke(cli.main, ["resume", old.tandem_id, "--review"])
    assert result.exit_code == 0, result.output
    assert [c.navigator for c in chat_cfgs] == ["claude"]



@pytest.mark.parametrize("argv", [
    ["--review", "status"], ["--review", "run", "hi"],
    ["--no-review", "doctor"], ["--review", "native"],
])
def test_review_flag_rejected_where_no_chat_window_opens(homes, ok_versions, entered, argv):
    result = click.testing.CliRunner().invoke(cli.main, argv)
    assert result.exit_code == 2
    assert "--review only applies" in result.output
    assert entered == []


def _nav_log(home, tandem_id, records):
    from tandem.chat.navigator import log_path
    p = log_path(tandem_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("".join(json.dumps(r) + "\n" for r in records))
    return p


NAV_RECORDS = [
    {"ts": "2026-09-25T14:01:00.000001+00:00", "kind": "review", "turn_harness": "claude",
     "prompt": "fix the loop", "gate": "review", "verdict": "speak", "severity": "block",
     "note": "The retry loop swallows ShadowBusy", "evidence": [], "elapsed": 21.3,
     "navigator": "codex", "model": "", "error": ""},
    {"ts": "2026-09-25T14:03:00.000001+00:00", "kind": "review", "turn_harness": "claude",
     "prompt": "explain", "gate": "skip:quiet", "verdict": "", "severity": "", "note": "",
     "evidence": [], "elapsed": 0, "navigator": "codex", "model": "", "error": ""},
    {"ts": "2026-09-25T14:05:00.000001+00:00", "kind": "review", "turn_harness": "opencode",
     "prompt": "add tests", "gate": "review", "verdict": "clean", "severity": "", "note": "",
     "evidence": [], "elapsed": 9.0, "navigator": "codex", "model": "", "error": ""},
    {"ts": "2026-09-25T14:05:30.000001+00:00", "kind": "feedback",
     "ref": "2026-09-25T14:01:00.000001+00:00", "value": "good"},
]


def test_navigator_log_prints_the_current_sessions_records_and_a_footer(homes, ok_versions, monkeypatch):
    with StateStore() as store:
        session = cli._pair_session(store, str(homes), "claude", ["claude", "codex"], seed=False)
    _nav_log(homes, session.tandem_id, NAV_RECORDS)
    r = click.testing.CliRunner().invoke(cli.main, ["navigator", "log"])
    assert r.exit_code == 0, r.output
    lines = r.output.rstrip("\n").split("\n")
    assert lines[0].startswith("14:01") and "claude → codex" in lines[0] and "speak block" in lines[0]
    assert "The retry loop swallows ShadowBusy" in lines[0] and lines[0].endswith("[good]")
    assert lines[1].startswith("14:03") and "skip:quiet" in lines[1]
    assert lines[2].startswith("14:05") and "clean" in lines[2]
    assert lines[-1] == "reviewed 2 · spoken 1 · skipped 1 · helpful 1/1 (100%)"


def test_navigator_log_limit_and_all(homes, ok_versions):
    with StateStore() as store:
        s1 = cli._pair_session(store, str(homes), "claude", ["claude", "codex"], seed=False)
    _nav_log(homes, s1.tandem_id, NAV_RECORDS)
    _nav_log(homes, "tdm-other", NAV_RECORDS[:1])
    r = click.testing.CliRunner().invoke(cli.main, ["navigator", "log", "-n", "1"])
    assert r.output.count("\n") == 2                       # one row + footer
    assert "14:05" in r.output                              # the newest
    r = click.testing.CliRunner().invoke(cli.main, ["navigator", "log", "--all"])
    assert r.output.count("14:01") == 2 and "tdm-other" in r.output


def test_navigator_log_without_a_session_or_records(homes, ok_versions):
    r = click.testing.CliRunner().invoke(cli.main, ["navigator", "log"])
    assert r.exit_code == 0 and "no navigator log" in r.output
