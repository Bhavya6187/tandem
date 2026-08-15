"""Participant resolution, resume narrowing, and the flip cycle ladder."""

import pytest

from conftest import Env3, FakeOpencodeAdapter
from tandem import config
from tandem.cli import _resolve_participants
from tandem.harness import ADAPTERS


@pytest.fixture(autouse=True)
def _register_fake_opencode(tmp_path, monkeypatch):
    """PR 1 ships no opencode adapter; these tests exercise the 3-way paths
    through the conftest fake (PR 2 replaces it with the real one)."""
    root = tmp_path / "oc-fake"
    root.mkdir(exist_ok=True)
    monkeypatch.setitem(ADAPTERS, "opencode", FakeOpencodeAdapter(root))


def test_load_harnesses_default_all(tmp_path, monkeypatch):
    monkeypatch.setenv("TANDEM_HOME", str(tmp_path))
    assert config.load_harnesses() == ["claude", "codex", "opencode"]


def test_load_harnesses_forgiving(tmp_path, monkeypatch):
    monkeypatch.setenv("TANDEM_HOME", str(tmp_path))
    (tmp_path / "config.toml").write_text(
        'harnesses = ["codex", "claude", "codex", "gemini"]\n'
    )
    assert config.load_harnesses() == ["codex", "claude"]


def test_load_harnesses_malformed_falls_back(tmp_path, monkeypatch):
    monkeypatch.setenv("TANDEM_HOME", str(tmp_path))
    (tmp_path / "config.toml").write_text("harnesses = 42\n")
    assert config.load_harnesses() == ["claude", "codex", "opencode"]


def _fake_versions(monkeypatch, mapping):
    """mapping: harness id -> version string or None (not installed)."""
    for hid, v in mapping.items():
        monkeypatch.setattr(type(ADAPTERS[hid]), "detect_version",
                            lambda self, _v=v: _v)


def test_resolution_silently_skips_missing(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TANDEM_HOME", str(tmp_path))
    _fake_versions(monkeypatch, {"claude": "2.1.220", "codex": "0.145.0",
                                 "opencode": None})
    usable, versions = _resolve_participants()
    assert usable == ["claude", "codex"]
    assert versions["opencode"] is None
    assert capsys.readouterr().err == ""     # not-installed is SILENT


def test_resolution_fewer_than_two_errors(tmp_path, monkeypatch):
    monkeypatch.setenv("TANDEM_HOME", str(tmp_path))
    _fake_versions(monkeypatch, {"claude": "2.1.220", "codex": None,
                                 "opencode": None})
    with pytest.raises(SystemExit):
        _resolve_participants()


def test_resolution_error_includes_install_hints(tmp_path, monkeypatch, capsys):
    """The <2-usable error tells a new user how to get a second harness:
    one install line per not-installed harness."""
    monkeypatch.setenv("TANDEM_HOME", str(tmp_path))
    _fake_versions(monkeypatch, {"claude": "2.1.220", "codex": None,
                                 "opencode": None})
    with pytest.raises(SystemExit):
        _resolve_participants()
    err = capsys.readouterr().err
    assert "npm install -g @openai/codex" in err
    assert "npm install -g opencode-ai" in err


def test_resolution_no_hint_for_installed_but_unusable(tmp_path, monkeypatch, capsys):
    """A below-floor codex already got its own exclusion warning; the
    install hint is only for harnesses that are absent altogether."""
    monkeypatch.setenv("TANDEM_HOME", str(tmp_path))
    _fake_versions(monkeypatch, {"claude": "2.1.220", "codex": "0.100.0",
                                 "opencode": None})
    with pytest.raises(SystemExit):
        _resolve_participants()
    err = capsys.readouterr().err
    assert "npm install -g @openai/codex" not in err
    assert "npm install -g opencode-ai" in err


def test_resume_narrows_and_persists(tmp_path, monkeypatch):
    """A stored 3-way session resumed with opencode gone drops it for good."""
    env = Env3(tmp_path, monkeypatch)
    from tandem.cli import _narrow_participants
    _fake_versions(monkeypatch, {"claude": "2.1.220", "codex": "0.145.0",
                                 "opencode": None})
    session = _narrow_participants(env.store, env.session)
    assert session.participants == ["claude", "codex"]
    stored = env.store.get_session(env.session.tandem_id)
    assert stored.participants == ["claude", "codex"]   # persisted


def test_resume_narrow_moves_active_off_dropped_member(tmp_path, monkeypatch):
    env = Env3(tmp_path, monkeypatch, active="claude")
    env.store.set_active(env.session.tandem_id, "opencode")
    env.session = env.store.get_session(env.session.tandem_id)
    from tandem.cli import _narrow_participants
    _fake_versions(monkeypatch, {"claude": "2.1.220", "codex": "0.145.0",
                                 "opencode": None})
    session = _narrow_participants(env.store, env.session)
    assert session.active == "claude"       # first survivor in stored order


def test_resolution_drops_below_floor_version(tmp_path, monkeypatch, capsys):
    """Below the compat floor the session format predates what tandem was
    built on — fail closed: warn and exclude."""
    monkeypatch.setenv("TANDEM_HOME", str(tmp_path))
    _fake_versions(monkeypatch, {"claude": "2.1.220", "codex": "0.100.0",
                                 "opencode": "1.18.15"})
    usable, versions = _resolve_participants()
    assert usable == ["claude", "opencode"]
    err = capsys.readouterr().err
    assert "excluded" in err


def test_resolution_admits_above_ceiling_with_warning(tmp_path, monkeypatch, capsys):
    """Above the ceiling is drift, not proven breakage: today's behavior is
    warn-and-proceed (`tandem doctor` is the advertised next step), and the
    resolution keeps it — otherwise the next codex release would hard-brick
    tandem until a compat bump ships."""
    monkeypatch.setenv("TANDEM_HOME", str(tmp_path))
    _fake_versions(monkeypatch, {"claude": "2.1.220", "codex": "0.150.0",
                                 "opencode": None})
    usable, _ = _resolve_participants()
    assert usable == ["claude", "codex"]
    assert "outside the range" in capsys.readouterr().err
