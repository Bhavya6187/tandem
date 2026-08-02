"""User configuration: $TANDEM_HOME/config.toml, [subagents] table only.

Unknown keys are ignored and every error yields defaults — configuration
must never be the reason subagent routing breaks (the hook's failure mode
is 'dispatch natively', and this module upholds it)."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass

from . import paths


@dataclass(frozen=True)
class SubagentsConfig:
    route: str = "all"          # "all" | "manual" | "off"
    model: str = ""             # "" -> omit -m; codex's configured default
    context: str = "match"      # "match" | "task" | "full"
    fanout_feature: str = ""    # --enable <name>; "" -> flag not passed
    keep_forks: bool = False


# "manual": no auto-reroute and no missed-reroute notice — dispatch to codex
# only when the model/user explicitly picks a bridge agent (`tandem:gpt`,
# `tandem:codex-worker`). The hook treats it exactly like "off"; the rest of
# tandem does not — `doctor._subagent_checks` silences its subagent billing
# warnings only under "off", because a manual user still dispatches to codex
# and still wants to know the worker model is unset.
_ROUTES = ("all", "manual", "off")
_CONTEXTS = ("match", "task", "full")


def load_subagents_config() -> SubagentsConfig:
    try:
        with open(paths.tandem_home() / "config.toml", "rb") as f:
            data = tomllib.load(f)
    # ValueError covers TOMLDecodeError (a subclass), the UnicodeDecodeError
    # tomllib raises when the file is not UTF-8, and open()'s embedded-NUL path.
    except (OSError, ValueError):
        return SubagentsConfig()
    raw = data.get("subagents")
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
