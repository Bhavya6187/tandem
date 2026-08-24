"""The mixed tab's @-prefix grammar: v1 of the routing seam.

Pure decision logic in the hookroute.py mold — the CLI wrapper owns stdin,
files and exit codes. Returning None means 'passthrough': the prompt runs
natively on the focus harness, untouched. That is the failure mode for
everything unrecognized, because a wrong block destroys a typed prompt while
a wrong passthrough merely shows the model a stray @token — the costs are
wildly asymmetric, so every doubt resolves to None.

Two forms, both keyed on a participant's name: `@codex …` and
`@codex:model …`. A bare model name (`@haiku`, `@sol`) is deliberately not
a route. Resolving one needs a hand-kept claude alias list plus a catalog
lookup on every prompt, and any file or directory whose name is a substring
of a model name (`@sol`, `@luna`, `@CLAUDE.md`) would be routed instead of
mentioned. The explicit form reaches every model the bare one could.

This module IS the v1 implementation of the route() seam the spec fixes for
the v2 model-based router: route_prompt(prompt, focus, participants) is the
decision function, and RouteDecision is its vocabulary. v2 adds a second
implementation consulted when this one returns None; nothing else changes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from . import modelcat

# One whitespace-delimited token: @ then a name that may carry one ':' for
# the harness:model form. Anything with spaces before the token is not a
# prefix — routing is a first-token-only protocol by spec.
_TOKEN_RE = re.compile(r"@([A-Za-z0-9._/-]+)(?::([A-Za-z0-9._/-]+))?\Z")


@dataclass(frozen=True)
class RouteDecision:
    harness: str
    model: str = ""     # "" = keep the target harness's configured model
    reason: str = ""    # user-facing: the bar and the block message


def _reason(harness: str, model: str) -> str:
    return f"→ {harness}" + (f" · {model}" if model else "")


def parse_prefix(
    prompt: str, participants: list[str]
) -> tuple[RouteDecision, str] | None:
    """(decision, prompt body) when the first token is a participant's name
    with a non-empty body; None otherwise (passthrough)."""
    parts = prompt.strip().split(None, 1)   # any whitespace: a shift-enter
    if len(parts) < 2:                      # after the token still routes
        return None
    first, body = parts[0], parts[1].strip()
    m = _TOKEN_RE.fullmatch(first)
    if m is None or not body:
        return None
    name, model = m.group(1), m.group(2)
    if name not in participants:
        return None
    if model is None:
        return RouteDecision(name, "", _reason(name, "")), body
    if name == "codex":
        # Explicit @codex:name mirrors `tandem sub` semantics: resolve
        # against the catalog when there is one, verbatim otherwise; a
        # standin ("gpt") resolves to "" = harness-only. Only a name the
        # catalog positively rejects is passthrough.
        try:
            model = modelcat.resolve(model, modelcat.load_catalog())
        except modelcat.UnknownModel:
            return None
    return RouteDecision(name, model, _reason(name, model)), body


def route_prompt(
    prompt: str, focus: str, participants: list[str]
) -> tuple[RouteDecision, str] | None:
    """The v1 route() seam: None = stay on `focus` (native turn, prefix text
    and all), a decision = block-and-route. Stay-on-focus keeps its prefix
    visible to the model on purpose: rewriting the prompt in an allow
    decision is a per-harness capability tandem does not depend on."""
    got = parse_prefix(prompt, participants)
    if got is None or got[0].harness == focus:
        return None
    return got
