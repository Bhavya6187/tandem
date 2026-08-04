"""[subagents] config: defaults on missing/broken file, validated values."""

from tandem.config import SubagentsConfig, load_subagents_config


def test_defaults_when_file_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("TANDEM_HOME", str(tmp_path / ".tandem"))
    cfg = load_subagents_config()
    assert cfg == SubagentsConfig()
    assert (cfg.route, cfg.model, cfg.context) == ("manual", "", "match")
    assert (cfg.fanout_feature, cfg.keep_forks) == ("", False)


def test_reads_values(tmp_path, monkeypatch):
    home = tmp_path / ".tandem"
    home.mkdir()
    (home / "config.toml").write_text(
        '[subagents]\nroute = "off"\nmodel = "gpt-x-mini"\n'
        'context = "full"\nfanout_feature = "collab"\nkeep_forks = true\n'
    )
    monkeypatch.setenv("TANDEM_HOME", str(home))
    cfg = load_subagents_config()
    assert cfg.route == "off"
    assert cfg.model == "gpt-x-mini"
    assert cfg.context == "full"
    assert cfg.fanout_feature == "collab"
    assert cfg.keep_forks is True


def test_routes_are_accepted_not_just_defaulted(tmp_path, monkeypatch):
    # Unlisted values degrade to the default, which is now "manual" — so
    # `route = "manual"` alone proves nothing: it passes even if "manual"
    # were dropped from _ROUTES. Pin the listing itself, and read back a
    # non-default name that only survives by being listed.
    from tandem import config

    assert set(config._ROUTES) == {"all", "manual", "off"}
    home = tmp_path / ".tandem"
    home.mkdir()
    monkeypatch.setenv("TANDEM_HOME", str(home))
    (home / "config.toml").write_text('[subagents]\nroute = "all"\n')
    assert load_subagents_config().route == "all"
    (home / "config.toml").write_text('[subagents]\nroute = "manual"\n')
    assert load_subagents_config().route == "manual"


def test_invalid_values_fall_back(tmp_path, monkeypatch):
    home = tmp_path / ".tandem"
    home.mkdir()
    (home / "config.toml").write_text(
        '[subagents]\nroute = "sometimes"\ncontext = 7\nkeep_forks = "yes"\n'
    )
    monkeypatch.setenv("TANDEM_HOME", str(home))
    cfg = load_subagents_config()
    assert cfg == SubagentsConfig()  # every bad value -> default


def test_broken_toml_falls_back(tmp_path, monkeypatch):
    home = tmp_path / ".tandem"
    home.mkdir()
    (home / "config.toml").write_text("[subagents\nnot toml")
    monkeypatch.setenv("TANDEM_HOME", str(home))
    assert load_subagents_config() == SubagentsConfig()


def test_non_utf8_falls_back(tmp_path, monkeypatch):
    home = tmp_path / ".tandem"
    home.mkdir()
    (home / "config.toml").write_bytes(b'[subagents]\nmodel = "caf\xe9"\n')
    monkeypatch.setenv("TANDEM_HOME", str(home))
    assert load_subagents_config() == SubagentsConfig()
