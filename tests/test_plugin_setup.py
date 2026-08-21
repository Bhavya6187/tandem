"""plugin_setup: detection reads claude state; install shells out; the
offer is TTY-gated and stamps once per machine."""

import json

import pytest

from tandem import paths, plugin_setup


@pytest.fixture
def homes(tmp_path, monkeypatch):
    monkeypatch.setenv("TANDEM_HOME", str(tmp_path / ".tandem"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / ".claude"))
    return tmp_path


def write_state(tmp_path, payload) -> None:
    p = tmp_path / ".claude" / "plugins" / "installed_plugins.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(payload if isinstance(payload, str) else json.dumps(payload))


# -- detection ---------------------------------------------------------------

def test_missing_state_file_means_not_installed(homes):
    assert plugin_setup.is_plugin_installed() is False


def test_entry_with_record_means_installed(homes):
    write_state(homes, {"version": 2, "plugins": {
        "tandem@tandem": [{"scope": "user", "version": "0.1.5"}]}})
    assert plugin_setup.is_plugin_installed() is True


def test_absent_entry_means_not_installed(homes):
    write_state(homes, {"version": 2, "plugins": {
        "other@mkt": [{"scope": "user"}]}})
    assert plugin_setup.is_plugin_installed() is False


def test_empty_record_list_means_not_installed(homes):
    write_state(homes, {"version": 2, "plugins": {"tandem@tandem": []}})
    assert plugin_setup.is_plugin_installed() is False


def test_malformed_json_reads_as_installed(homes):
    # Ambiguity must resolve to silence (True = never nag).
    write_state(homes, "{not json")
    assert plugin_setup.is_plugin_installed() is True


def test_unexpected_shape_reads_as_installed(homes):
    write_state(homes, {"version": 3, "plugins": "moved-elsewhere"})
    assert plugin_setup.is_plugin_installed() is True


def test_undecodable_bytes_read_as_installed(homes):
    # read_text() raises UnicodeDecodeError (a ValueError, not an OSError)
    # on a non-UTF-8 registry; that must resolve to silence, not a crash.
    p = paths.claude_installed_plugins_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"\xff\xfe{")
    assert plugin_setup.is_plugin_installed() is True


# -- install -----------------------------------------------------------------

class FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.fixture
def claude_on_path(monkeypatch):
    monkeypatch.setattr(plugin_setup.shutil, "which",
                        lambda name: "/usr/local/bin/claude")


@pytest.fixture
def recorded_runs(monkeypatch):
    """Record subprocess invocations; per-command results set via dict."""
    calls, results = [], {}

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return results.get(tuple(cmd), FakeProc())

    monkeypatch.setattr(plugin_setup.subprocess, "run", fake_run)
    return calls, results


ADD_CMD = ("claude", "plugin", "marketplace", "add", "Bhavya6187/tandem")
INSTALL_CMD = ("claude", "plugin", "install", "tandem@tandem")
CODEX_ADD_CMD = ("codex", "plugin", "marketplace", "add", "Bhavya6187/tandem")
CODEX_INSTALL_CMD = ("codex", "plugin", "install", "tandem@tandem")


def test_install_runs_add_then_install(claude_on_path, recorded_runs, capsys):
    # `claude_on_path` answers every `which`, so codex looks installed too:
    # a successful claude install registers the same tree with codex.
    calls, _ = recorded_runs
    assert plugin_setup.install_plugin() is True
    assert [tuple(c) for c in calls] == [
        ADD_CMD, INSTALL_CMD, CODEX_ADD_CMD, CODEX_INSTALL_CMD]
    out = capsys.readouterr().out
    assert "new Claude sessions" in out


def test_add_failure_is_nonfatal_when_install_succeeds(
        claude_on_path, recorded_runs, capsys):
    calls, results = recorded_runs
    results[ADD_CMD] = FakeProc(returncode=1, stderr="some marketplace noise")
    assert plugin_setup.install_plugin() is True
    assert [tuple(c) for c in calls] == [
        ADD_CMD, INSTALL_CMD, CODEX_ADD_CMD, CODEX_INSTALL_CMD]


def test_install_failure_prints_manual_commands(
        claude_on_path, recorded_runs, capsys):
    calls, results = recorded_runs
    results[INSTALL_CMD] = FakeProc(returncode=1, stderr="boom")
    assert plugin_setup.install_plugin() is False
    # the codex mirror rides on the claude success path only: there is no
    # point registering a tree claude itself refused
    assert not any(c[0] == "codex" for c in calls)
    err = capsys.readouterr().err
    assert "claude plugin marketplace add Bhavya6187/tandem" in err
    assert "claude plugin install tandem@tandem" in err


def test_missing_claude_binary_fails_without_running_anything(
        recorded_runs, monkeypatch, capsys):
    calls, _ = recorded_runs
    monkeypatch.setattr(plugin_setup.shutil, "which", lambda name: None)
    assert plugin_setup.install_plugin() is False
    assert calls == []
    assert "claude plugin install tandem@tandem" in capsys.readouterr().err


# -- codex registration ------------------------------------------------------

@pytest.fixture
def codex_home(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / ".codex"))
    home = tmp_path / ".codex"
    home.mkdir(parents=True, exist_ok=True)
    return home


def test_missing_codex_config_means_not_installed(codex_home):
    assert plugin_setup.is_plugin_installed_codex() is False


def test_codex_plugins_entry_means_installed(codex_home):
    (codex_home / "config.toml").write_text(
        '[plugins."tandem@tandem"]\nenabled = true\n')
    assert plugin_setup.is_plugin_installed_codex() is True


def test_codex_absent_entry_means_not_installed(codex_home):
    (codex_home / "config.toml").write_text(
        '[plugins."other@mkt"]\nenabled = true\n')
    assert plugin_setup.is_plugin_installed_codex() is False


def test_codex_config_without_plugins_table_means_not_installed(codex_home):
    (codex_home / "config.toml").write_text('model = "gpt-5"\n')
    assert plugin_setup.is_plugin_installed_codex() is False


def test_unparseable_codex_config_reads_as_installed(codex_home):
    # Same ambiguity rule as claude's registry: doubt must not nag.
    (codex_home / "config.toml").write_text("not [ toml")
    assert plugin_setup.is_plugin_installed_codex() is True


def test_unexpected_codex_plugins_shape_reads_as_installed(codex_home):
    # A non-table `plugins` is a config tandem does not understand, not
    # proof of absence.
    (codex_home / "config.toml").write_text('plugins = "moved-elsewhere"\n')
    assert plugin_setup.is_plugin_installed_codex() is True


def test_unreadable_codex_config_reads_as_installed(codex_home):
    # a directory where the file should be: OSError, i.e. ambiguous
    (codex_home / "config.toml").mkdir()
    assert plugin_setup.is_plugin_installed_codex() is True


@pytest.fixture
def codex_on_path(monkeypatch):
    monkeypatch.setattr(plugin_setup.shutil, "which",
                        lambda name: "/usr/local/bin/" + name)


def test_install_plugin_codex_runs_both_commands(
        codex_on_path, recorded_runs, capsys):
    calls, _ = recorded_runs
    assert plugin_setup.install_plugin_codex() is True
    assert [tuple(c) for c in calls] == [CODEX_ADD_CMD, CODEX_INSTALL_CMD]
    assert "new codex sessions" in capsys.readouterr().out


def test_install_plugin_codex_missing_binary_is_false(
        recorded_runs, monkeypatch, capsys):
    calls, _ = recorded_runs
    monkeypatch.setattr(plugin_setup.shutil, "which", lambda name: None)
    assert plugin_setup.install_plugin_codex() is False
    assert calls == []
    # no codex, nothing to say: a machine without codex is a normal machine
    assert capsys.readouterr().err == ""


def test_codex_add_failure_is_nonfatal_when_install_succeeds(
        codex_on_path, recorded_runs):
    calls, results = recorded_runs
    results[CODEX_ADD_CMD] = FakeProc(returncode=1, stderr="marketplace noise")
    assert plugin_setup.install_plugin_codex() is True
    assert [tuple(c) for c in calls] == [CODEX_ADD_CMD, CODEX_INSTALL_CMD]


def test_codex_install_failure_is_a_yellow_note_naming_the_consequence(
        codex_on_path, recorded_runs, capsys):
    _, results = recorded_runs
    results[CODEX_INSTALL_CMD] = FakeProc(returncode=1, stderr="boom")
    assert plugin_setup.install_plugin_codex() is False
    err = capsys.readouterr().err
    assert "@-routing from codex" in err
    assert "error" not in err.lower()


# -- hook availability -------------------------------------------------------

def test_hook_available_dispatches_per_harness(homes, monkeypatch):
    seen = []
    monkeypatch.setattr(plugin_setup, "is_plugin_installed",
                        lambda: seen.append("claude") or True)
    monkeypatch.setattr(plugin_setup, "is_plugin_installed_codex",
                        lambda: seen.append("codex") or False)
    assert plugin_setup.hook_available("claude") is True
    assert plugin_setup.hook_available("codex") is False
    assert seen == ["claude", "codex"]


def test_hook_available_is_false_for_an_unknown_harness(homes, monkeypatch):
    # opencode has no prompt hook at all: nothing to detect, and no
    # detection call to make
    monkeypatch.setattr(plugin_setup, "is_plugin_installed",
                        lambda: pytest.fail("probed claude for opencode"))
    monkeypatch.setattr(plugin_setup, "is_plugin_installed_codex",
                        lambda: pytest.fail("probed codex for opencode"))
    assert plugin_setup.hook_available("opencode") is False


# -- offer -------------------------------------------------------------------

@pytest.fixture
def offerable(homes, claude_on_path, monkeypatch):
    """All four gates open: TTY, claude on PATH, no stamp, not installed."""
    monkeypatch.setattr(plugin_setup, "_stdin_is_tty", lambda: True)
    return homes


def stamp_path(tmp_path):
    return tmp_path / ".tandem" / "plugin-offer"


def test_offer_silent_when_not_tty(homes, claude_on_path, monkeypatch, capsys):
    monkeypatch.setattr(plugin_setup, "_stdin_is_tty", lambda: False)
    plugin_setup.offer_install()
    assert capsys.readouterr().out == ""
    assert not stamp_path(homes).exists()


def test_offer_silent_when_no_claude(homes, monkeypatch, capsys):
    monkeypatch.setattr(plugin_setup, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(plugin_setup.shutil, "which", lambda name: None)
    plugin_setup.offer_install()
    assert capsys.readouterr().out == ""
    assert not stamp_path(homes).exists()


def test_offer_silent_when_already_installed(offerable, capsys):
    write_state(offerable, {"version": 2, "plugins": {
        "tandem@tandem": [{"scope": "user"}]}})
    plugin_setup.offer_install()
    assert capsys.readouterr().out == ""
    assert not stamp_path(offerable).exists()


def test_offer_silent_when_stamped(offerable, monkeypatch, capsys):
    stamp_path(offerable).parent.mkdir(parents=True, exist_ok=True)
    stamp_path(offerable).touch()
    monkeypatch.setattr(plugin_setup.click, "confirm",
                        lambda *a, **k: pytest.fail("prompted despite stamp"))
    plugin_setup.offer_install()
    assert capsys.readouterr().out == ""


def test_decline_prints_hint_and_stamps(offerable, monkeypatch, capsys):
    monkeypatch.setattr(plugin_setup.click, "confirm", lambda *a, **k: False)
    plugin_setup.offer_install()
    assert plugin_setup.LATER_HINT in capsys.readouterr().out
    assert stamp_path(offerable).exists()


def test_accept_installs_and_stamps(offerable, monkeypatch):
    installed = []
    monkeypatch.setattr(plugin_setup.click, "confirm", lambda *a, **k: True)
    monkeypatch.setattr(plugin_setup, "install_plugin",
                        lambda: (installed.append(True), True)[1])
    plugin_setup.offer_install()
    assert installed == [True]
    assert stamp_path(offerable).exists()


def test_failed_install_still_stamps(offerable, monkeypatch):
    monkeypatch.setattr(plugin_setup.click, "confirm", lambda *a, **k: True)
    monkeypatch.setattr(plugin_setup, "install_plugin", lambda: False)
    plugin_setup.offer_install()
    assert stamp_path(offerable).exists()


def test_abort_at_prompt_counts_as_decline(offerable, monkeypatch, capsys):
    def raise_abort(*a, **k):
        raise plugin_setup.click.Abort()

    monkeypatch.setattr(plugin_setup.click, "confirm", raise_abort)
    plugin_setup.offer_install()
    assert plugin_setup.LATER_HINT in capsys.readouterr().out
    assert stamp_path(offerable).exists()
