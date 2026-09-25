"""config.toml: defaults on missing/broken file, validated values."""

import pytest

from tandem.config import (
    ChatConfig,
    FrameConfig,
    SubagentsConfig,
    load_chat_config,
    load_frame_config,
    load_harness_args,
    load_skip_permissions,
    set_skip_permissions,
    skip_permission_args,
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


def test_chat_config_defaults_when_absent(tmp_path, monkeypatch):
    monkeypatch.setenv("TANDEM_HOME", str(tmp_path / ".tandem"))
    assert load_chat_config() == ChatConfig()


def test_chat_config_reads_and_validates(tmp_path, monkeypatch):
    _write_config(
        tmp_path, monkeypatch,
        '[chat]\ntool_output_lines = 3\nhistory_turns = "lots"\nshow_thinking = true\n'
        'claude_setting_sources = ["user", "bogus"]\ncodex_approval_policy = "never"\n'
        'codex_sandbox = 7\n',
    )
    cfg = load_chat_config()
    assert cfg.tool_output_lines == 3
    assert cfg.history_turns == 50            # wrong type -> default
    assert cfg.show_thinking is True
    assert cfg.claude_setting_sources == ("user",)   # unknown names dropped
    assert cfg.codex_approval_policy == "never"
    assert cfg.codex_sandbox == ""            # wrong type -> default


def test_chat_bell_is_on_unless_turned_off(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch, '[chat]\nshow_thinking = true\n')
    assert load_chat_config().bell is True
    _write_config(tmp_path, monkeypatch, '[chat]\nbell = false\n')
    assert load_chat_config().bell is False
    _write_config(tmp_path, monkeypatch, '[chat]\nbell = "no"\n')
    assert load_chat_config().bell is True     # wrong type -> default


def test_chat_config_keeps_known_codex_vocabularies(tmp_path, monkeypatch):
    _write_config(
        tmp_path, monkeypatch,
        '[chat]\ncodex_approval_policy = "on-request"\n'
        'codex_sandbox = "workspace-write"\n',
    )
    cfg = load_chat_config()
    assert cfg.codex_approval_policy == "on-request"
    assert cfg.codex_sandbox == "workspace-write"


def test_chat_config_unknown_codex_vocabularies_inherit(tmp_path, monkeypatch):
    # both feed codex's own closed enums: a typo must degrade to "" (inherit
    # ~/.codex/config.toml), never reach the launch and have codex reject it
    _write_config(
        tmp_path, monkeypatch,
        '[chat]\ncodex_approval_policy = "sometimes"\n'
        'codex_sandbox = "workspace_write"\n',
    )
    cfg = load_chat_config()
    assert cfg.codex_approval_policy == ""
    assert cfg.codex_sandbox == ""


def test_skip_permissions_defaults_off(tmp_path, monkeypatch):
    monkeypatch.setenv("TANDEM_HOME", str(tmp_path / ".tandem"))
    assert load_skip_permissions() is False
    assert load_chat_config().skip_permissions is False


def test_skip_permissions_on_without_a_chat_table(tmp_path, monkeypatch):
    # the key is top-level: it must reach the chat window with no [chat] table
    _write_config(tmp_path, monkeypatch, "skip_permissions = true\n")
    assert load_skip_permissions() is True
    assert load_chat_config().skip_permissions is True


def test_skip_permissions_rides_alongside_chat_keys(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch,
                  "skip_permissions = true\n[chat]\nhistory_turns = 3\n")
    cfg = load_chat_config()
    assert (cfg.skip_permissions, cfg.history_turns) == (True, 3)


def test_skip_permissions_only_a_real_true_turns_it_on(tmp_path, monkeypatch):
    # a permissions bypass must never come from a typo: "true", 1 and a
    # [chat]-scoped copy of the key all leave it off
    for body in ('skip_permissions = "true"\n', "skip_permissions = 1\n",
                 "skip_permissions = [true]\n", "[chat]\nskip_permissions = true\n"):
        _write_config(tmp_path, monkeypatch, body)
        assert load_skip_permissions() is False, body
        assert load_chat_config().skip_permissions is False, body


def test_skip_permission_args_per_harness(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch, "skip_permissions = true\n")
    assert skip_permission_args("claude") == ["--dangerously-skip-permissions"]
    assert skip_permission_args("codex") == ["--dangerously-bypass-approvals-and-sandbox"]
    assert skip_permission_args("opencode") == []    # no such flag; its own config rules


def test_skip_permission_args_empty_when_off(tmp_path, monkeypatch):
    monkeypatch.setenv("TANDEM_HOME", str(tmp_path / ".tandem"))
    assert skip_permission_args("claude") == []


def test_skip_permissions_override_on_needs_no_config(tmp_path, monkeypatch):
    monkeypatch.setenv("TANDEM_HOME", str(tmp_path / ".tandem"))
    set_skip_permissions(True)
    assert load_skip_permissions() is True
    assert load_chat_config().skip_permissions is True
    assert skip_permission_args("codex") == ["--dangerously-bypass-approvals-and-sandbox"]


def test_skip_permissions_override_off_beats_the_config(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch,
                  "skip_permissions = true\n[chat]\nhistory_turns = 3\n")
    set_skip_permissions(False)
    assert load_skip_permissions() is False
    assert load_chat_config().skip_permissions is False
    assert skip_permission_args("claude") == []


def test_skip_permissions_override_cleared_falls_back_to_the_config(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch, "skip_permissions = true\n")
    set_skip_permissions(False)
    set_skip_permissions(None)
    assert load_skip_permissions() is True


def test_navigator_is_off_by_default(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch, '[chat]\nbell = true\n')
    cfg = load_chat_config()
    assert cfg.navigator == "" and cfg.navigator_model == ""
    assert cfg.navigator_deliver == "bar"
    assert cfg.navigator_headroom == 20 and cfg.navigator_interval == 180


def test_navigator_keys_read_and_validate(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch,
                  '[chat]\nnavigator = "codex"\nnavigator_model = "gpt-5.5"\n'
                  'navigator_deliver = "prompt"\nnavigator_headroom = 35\nnavigator_interval = 60\n')
    cfg = load_chat_config()
    assert cfg.navigator == "codex" and cfg.navigator_model == "gpt-5.5"
    assert cfg.navigator_deliver == "prompt"
    assert cfg.navigator_headroom == 35 and cfg.navigator_interval == 60


@pytest.mark.parametrize("value", ['"opencode"', '"gemini"', "true", "3"])
def test_navigator_rejects_unknown_harnesses(tmp_path, monkeypatch, value):
    _write_config(tmp_path, monkeypatch, f'[chat]\nnavigator = {value}\n')
    assert load_chat_config().navigator == ""


def test_navigator_bad_values_fall_back(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch,
                  '[chat]\nnavigator_deliver = "push"\nnavigator_headroom = -5\n'
                  'navigator_interval = "soon"\n')
    cfg = load_chat_config()
    assert cfg.navigator_deliver == "bar"
    assert cfg.navigator_headroom == 0            # clamped, like history_turns
    assert cfg.navigator_interval == 180
