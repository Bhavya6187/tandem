"""User configuration: $TANDEM_HOME/config.toml.

[subagents] controls codex subagent routing; [claude] / [codex] hold an
`args` list appended to every interactive launch of that harness.

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
