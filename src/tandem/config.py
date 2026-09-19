"""User configuration: $TANDEM_HOME/config.toml.

[subagents] controls codex subagent routing; [claude] / [codex] hold an
`args` list appended to every interactive launch of that harness; [frame]
holds the meta-harness flip keybind, its status bar toggle, the
pipelined-flip toggle, and the bar's rate-limit poll toggle; [chat]
tunes the unified chat window; top-level `skip_permissions` turns off
claude's and codex's permission prompts in every session tandem opens.

Unknown keys are ignored and every error yields defaults — configuration
must never be the reason a launch breaks or subagent routing stops (the
hook's failure mode is 'dispatch natively', and this module upholds it)."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass

from . import paths


@dataclass(frozen=True)
class SubagentsConfig:
    route: str = "manual"       # "manual" | "all" | "off"
    model: str = ""             # "" -> omit -m; codex's configured default
    context: str = "match"      # "match" | "task" | "full"
    fanout_feature: str = ""    # --enable <name>; "" -> flag not passed
    keep_forks: bool = False


# "manual" — the default: no auto-reroute and no missed-reroute notice —
# dispatch to codex only when the model/user explicitly picks a bridge agent
# (`tandem:gpt`, `tandem:codex-worker`). The hook treats it exactly like
# "off"; the rest of tandem does not — `doctor._subagent_checks` silences
# its subagent billing warnings only under "off", because a manual user
# still dispatches to codex and still wants to know the worker model is
# unset. "all" opts back in to rerouting every native dispatch.
_ROUTES = ("all", "manual", "off")
_CONTEXTS = ("match", "task", "full")


def _read_config() -> dict:
    """Parsed config.toml, or {} when unreadable in any way."""
    try:
        with open(paths.tandem_home() / "config.toml", "rb") as f:
            return tomllib.load(f)
    # ValueError covers TOMLDecodeError (a subclass), the UnicodeDecodeError
    # tomllib raises when the file is not UTF-8, and open()'s embedded-NUL path.
    except (OSError, ValueError):
        return {}


def load_subagents_config() -> SubagentsConfig:
    raw = _read_config().get("subagents")
    if not isinstance(raw, dict):
        return SubagentsConfig()
    d = SubagentsConfig()

    def pick(key: str, kind: type, default, allowed=None):
        v = raw.get(key, default)
        if not isinstance(v, kind) or (allowed and v not in allowed):
            return default
        return v

    return SubagentsConfig(
        route=pick("route", str, d.route, _ROUTES),
        model=pick("model", str, d.model),
        context=pick("context", str, d.context, _CONTEXTS),
        fanout_feature=pick("fanout_feature", str, d.fanout_feature),
        keep_forks=pick("keep_forks", bool, d.keep_forks),
    )


def load_harness_args(harness: str) -> list[str]:
    """`args` from the [claude] / [codex] table: extra argv appended to
    every interactive launch of that harness. Anything malformed -> []."""
    table = _read_config().get(harness)
    args = table.get("args") if isinstance(table, dict) else None
    # Empty and NUL-bearing strings can't be real flags but would make the
    # exec fail; one bad element rejects the list, like non-strings do.
    if not isinstance(args, list) or not all(
        isinstance(a, str) and a and "\x00" not in a for a in args
    ):
        return []
    return args


# Each harness's own "ask me nothing" flag for an interactive launch.
# opencode has none — its permissions live in its own opencode.json.
_SKIP_PERMISSION_ARGS = {
    "claude": "--dangerously-skip-permissions",
    "codex": "--dangerously-bypass-approvals-and-sandbox",
}


# `--skip-permissions` / `--no-skip-permissions` for this launch; None = the
# config decides. Held here rather than in the environment: an env var would
# ride into every harness child, and a `tandem` started from inside one would
# inherit a bypass nobody asked it for.
_skip_permissions_override: bool | None = None


def set_skip_permissions(value: bool | None) -> None:
    global _skip_permissions_override
    _skip_permissions_override = value


def _skip_permissions(config: dict) -> bool:
    if _skip_permissions_override is not None:
        return _skip_permissions_override
    return config.get("skip_permissions") is True


def load_skip_permissions() -> bool:
    """The launch's `--[no-]skip-permissions` flag, else top-level
    `skip_permissions`. Forgiving the other way round from the rest: only a
    real TOML `true` turns it on, so a typo can never be the reason a
    permission prompt disappears."""
    return _skip_permissions(_read_config())


def skip_permission_args(harness: str) -> list[str]:
    """The bypass flag for an interactive launch of `harness`; [] when
    `skip_permissions` is off or the harness has no such flag."""
    flag = _SKIP_PERMISSION_ARGS.get(harness)
    return [flag] if flag and load_skip_permissions() else []


@dataclass(frozen=True)
class FrameConfig:
    flip_byte: int = 0x1D   # Ctrl-]
    bar: bool = True
    warm: bool = True       # boot the other harness during the flip's teardown
    rate_limits: bool = True  # poll each account's usage windows for the bar


def _parse_flip_key(value: str) -> int | None:
    """'ctrl-]' / 'ctrl-t' / '0x1d' -> byte value. None when unparseable or
    not a control byte (a printable key would swallow real typing)."""
    v = value.strip().lower()
    if v.startswith("ctrl-") and len(v) == 6:
        # No .upper() before ord(): & 0x1F already folds case across printable
        # ASCII, and case-folding can expand one char into several ("ß" -> "SS"),
        # which would raise TypeError out of a config read.
        code = ord(v[5]) & 0x1F
    elif v.startswith("0x"):
        try:
            code = int(v, 16)
        except ValueError:
            return None
    else:
        return None
    return code if 0 < code < 0x20 else None


def load_frame_config() -> FrameConfig:
    """[frame] table: the flip keybind, the status bar toggle, the
    pipelined-flip toggle, and the bar's rate-limit poll toggle."""
    raw = _read_config().get("frame")
    if not isinstance(raw, dict):
        return FrameConfig()
    d = FrameConfig()
    key = raw.get("flip_key")
    byte = _parse_flip_key(key) if isinstance(key, str) else None
    bar = raw.get("bar")
    warm = raw.get("warm")
    limits = raw.get("rate_limits")
    return FrameConfig(
        flip_byte=byte if byte is not None else d.flip_byte,
        bar=bar if isinstance(bar, bool) else d.bar,
        warm=warm if isinstance(warm, bool) else d.warm,
        rate_limits=limits if isinstance(limits, bool) else d.rate_limits,
    )


SUPPORTED_HARNESSES = ("claude", "codex", "opencode")


def load_harnesses() -> list[str]:
    """Top-level `harnesses` key: ordered participant intent. Forgiving like
    every other key — unknown names dropped, duplicates deduped, anything
    malformed falls back to all supported. Order defines the flip cycle."""
    raw = _read_config().get("harnesses")
    if not isinstance(raw, list):
        return list(SUPPORTED_HARNESSES)
    seen: set[str] = set()
    out: list[str] = []
    for h in raw:
        if isinstance(h, str) and h in SUPPORTED_HARNESSES and h not in seen:
            seen.add(h)
            out.append(h)
    return out or list(SUPPORTED_HARNESSES)


_SETTING_SOURCES = ("user", "project", "local")
# codex's own app-server enums: a value outside them is rejected by the
# launch, so an unknown one degrades to "" (inherit ~/.codex/config.toml)
# rather than becoming the reason a launch breaks.
_CODEX_APPROVAL_POLICIES = ("untrusted", "on-request", "never")
_CODEX_SANDBOXES = ("read-only", "workspace-write", "danger-full-access")


@dataclass(frozen=True)
class ChatConfig:
    """[chat]: the unified window. Every field forgiving, like the rest."""
    tool_output_lines: int = 8          # tail printed per tool call
    history_turns: int = 50             # turns painted at startup
    show_thinking: bool = False
    claude_setting_sources: tuple[str, ...] = _SETTING_SOURCES
    codex_approval_policy: str = ""     # "" = inherit ~/.codex/config.toml
    codex_sandbox: str = ""             # "" = inherit
    skip_permissions: bool = False      # the top-level key, carried to the runtimes


def load_chat_config() -> ChatConfig:
    config = _read_config()
    skip = _skip_permissions(config)
    raw = config.get("chat")
    if not isinstance(raw, dict):
        return ChatConfig(skip_permissions=skip)
    d = ChatConfig()

    def pick(key: str, kind: type, default, allowed=None):
        v = raw.get(key, default)
        # bool is an int subclass: a `true` must not pass as an int
        if not isinstance(v, kind) or (kind is int and isinstance(v, bool)):
            return default
        if allowed and v not in allowed:
            return default
        return v

    sources = raw.get("claude_setting_sources")
    if isinstance(sources, list):
        kept = tuple(s for s in sources if isinstance(s, str) and s in _SETTING_SOURCES)
        sources = kept or d.claude_setting_sources
    else:
        sources = d.claude_setting_sources
    return ChatConfig(
        tool_output_lines=max(0, pick("tool_output_lines", int, d.tool_output_lines)),
        history_turns=max(0, pick("history_turns", int, d.history_turns)),
        show_thinking=pick("show_thinking", bool, d.show_thinking),
        claude_setting_sources=sources,
        codex_approval_policy=pick("codex_approval_policy", str,
                                   d.codex_approval_policy, _CODEX_APPROVAL_POLICIES),
        codex_sandbox=pick("codex_sandbox", str, d.codex_sandbox, _CODEX_SANDBOXES),
        skip_permissions=skip,
    )
