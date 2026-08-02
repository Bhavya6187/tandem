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


def test_install_runs_add_then_install(claude_on_path, recorded_runs, capsys):
    calls, _ = recorded_runs
    assert plugin_setup.install_plugin() is True
    assert [tuple(c) for c in calls] == [ADD_CMD, INSTALL_CMD]
    out = capsys.readouterr().out
    assert "new Claude sessions" in out


def test_add_failure_is_nonfatal_when_install_succeeds(
        claude_on_path, recorded_runs, capsys):
    calls, results = recorded_runs
    results[ADD_CMD] = FakeProc(returncode=1, stderr="some marketplace noise")
    assert plugin_setup.install_plugin() is True
    assert [tuple(c) for c in calls] == [ADD_CMD, INSTALL_CMD]


def test_install_failure_prints_manual_commands(
        claude_on_path, recorded_runs, capsys):
    _, results = recorded_runs
    results[INSTALL_CMD] = FakeProc(returncode=1, stderr="boom")
    assert plugin_setup.install_plugin() is False
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
