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
