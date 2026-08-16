"""config.toml: defaults on missing/broken file, validated values."""

from tandem.config import (
    FrameConfig,
    SubagentsConfig,
    load_frame_config,
    load_harness_args,
    load_subagents_config,
)


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


def test_harness_args_reads_lists(tmp_path, monkeypatch):
    home = tmp_path / ".tandem"
    home.mkdir()
    (home / "config.toml").write_text(
        '[claude]\nargs = ["--dangerously-skip-permissions"]\n\n'
        '[codex]\nargs = ["--dangerously-bypass-approvals-and-sandbox"]\n'
    )
    monkeypatch.setenv("TANDEM_HOME", str(home))
    assert load_harness_args("claude") == ["--dangerously-skip-permissions"]
    assert load_harness_args("codex") == [
        "--dangerously-bypass-approvals-and-sandbox"
    ]


def test_harness_args_empty_when_file_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("TANDEM_HOME", str(tmp_path / ".tandem"))
    assert load_harness_args("claude") == []


def test_harness_args_invalid_shapes_fall_back(tmp_path, monkeypatch):
    home = tmp_path / ".tandem"
    home.mkdir()
    monkeypatch.setenv("TANDEM_HOME", str(home))
    cases = (
        '[claude]\nargs = "--not-a-list"\n',      # scalar, not a list
        '[claude]\nargs = ["--ok", 7]\n',         # non-string element
        '[claude]\nargs = ["--ok", ""]\n',        # empty string element
        '[claude]\nargs = ["--x\\u0000y"]\n',     # NUL: exec would raise
        '[subagents]\nroute = "manual"\n',        # table absent entirely
        "[claude\nnot toml",                      # broken TOML
    )
    for body in cases:
        (home / "config.toml").write_text(body)
        assert load_harness_args("claude") == [], body


def _write_config(tmp_path, monkeypatch, text):
    """The file's home/config.toml idiom, folded up: the [frame] tests differ
    only in the table body."""
    home = tmp_path / ".tandem"
    home.mkdir(exist_ok=True)
    (home / "config.toml").write_text(text)
    monkeypatch.setenv("TANDEM_HOME", str(home))


def test_frame_defaults_without_config(tmp_path, monkeypatch):
    monkeypatch.setenv("TANDEM_HOME", str(tmp_path / ".tandem"))
    cfg = load_frame_config()
    assert cfg == FrameConfig(flip_byte=0x1D, bar=True, warm=True)


def test_frame_flip_key_ctrl_name(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch, '[frame]\nflip_key = "ctrl-t"\n')
    assert load_frame_config().flip_byte == 0x14


def test_frame_flip_key_hex(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch, '[frame]\nflip_key = "0x1e"\n')
    assert load_frame_config().flip_byte == 0x1E


def test_frame_flip_key_printable_rejected(tmp_path, monkeypatch):
    # a printable key would swallow real typing — fall back to default
    _write_config(tmp_path, monkeypatch, '[frame]\nflip_key = "a"\n')
    assert load_frame_config().flip_byte == 0x1D


def test_frame_bar_off(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch, '[frame]\nbar = false\n')
    assert load_frame_config().bar is False


def test_frame_warm_defaults_true(tmp_path, monkeypatch):
    monkeypatch.setenv("TANDEM_HOME", str(tmp_path / ".tandem"))
    assert load_frame_config().warm is True


def test_frame_warm_off(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch, "[frame]\nwarm = false\n")
    assert load_frame_config().warm is False


def test_frame_warm_garbage_falls_back_to_default(tmp_path, monkeypatch):
    # a truthy-looking string must not read as "on" — warm gates process
    # spawning, so only a real bool may turn it off.
    _write_config(tmp_path, monkeypatch, '[frame]\nwarm = "yes"\n')
    assert load_frame_config().warm is True


def test_frame_flip_key_multichar_casefold_does_not_raise(tmp_path, monkeypatch):
    # "ß".upper() == "SS", so case-folding before ord() raises TypeError and
    # takes the launch down with it — no [frame] value may ever raise. 0xDF &
    # 0x1F is a control byte, so this one is accepted rather than defaulted.
    _write_config(tmp_path, monkeypatch, '[frame]\nflip_key = "ctrl-ß"\n')
    assert load_frame_config() == FrameConfig(flip_byte=0x1F, bar=True)


def test_frame_malformed_values_fall_back(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch, '[frame]\nflip_key = 29\nbar = "yes"\n')
    assert load_frame_config() == FrameConfig()


def test_frame_rate_limits_defaults_true(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch, "[frame]\n")
    assert load_frame_config().rate_limits is True


def test_frame_rate_limits_off(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch, "[frame]\nrate_limits = false\n")
    assert load_frame_config().rate_limits is False


def test_frame_rate_limits_garbage_falls_back_to_default(tmp_path, monkeypatch):
    # rate_limits gates tandem's only outbound network calls; a stray
    # string must read as the default, not as "on"
    _write_config(tmp_path, monkeypatch, '[frame]\nrate_limits = "off"\n')
    assert load_frame_config().rate_limits is True
