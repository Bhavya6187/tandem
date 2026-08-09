"""User configuration: $TANDEM_HOME/config.toml.

[subagents] controls codex subagent routing; [claude] / [codex] hold an
`args` list appended to every interactive launch of that harness; [frame]
holds the meta-harness flip keybind and its status bar toggle.

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


@dataclass(frozen=True)
class FrameConfig:
    flip_byte: int = 0x1D   # Ctrl-]
    bar: bool = True


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
    """[frame] table: the flip keybind and the status bar toggle."""
    raw = _read_config().get("frame")
    if not isinstance(raw, dict):
        return FrameConfig()
    d = FrameConfig()
    key = raw.get("flip_key")
    byte = _parse_flip_key(key) if isinstance(key, str) else None
    bar = raw.get("bar")
    return FrameConfig(
        flip_byte=byte if byte is not None else d.flip_byte,
        bar=bar if isinstance(bar, bool) else d.bar,
    )
