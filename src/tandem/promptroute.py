"""The mixed tab's @-prefix grammar: v1 of the routing seam.

Pure decision logic in the hookroute.py mold — the CLI wrapper owns stdin,
files and exit codes. Returning None means 'passthrough': the prompt runs
natively on the focus harness, untouched. That is the failure mode for
everything unrecognized, because a wrong block destroys a typed prompt while
a wrong passthrough merely shows the model a stray @token — the costs are
wildly asymmetric, so every doubt resolves to None.

This module IS the v1 implementation of the route() seam the spec fixes for
the v2 model-based router: route_prompt(prompt, focus, participants) is the
decision function, and RouteDecision is its vocabulary. v2 adds a second
implementation consulted when this one returns None; nothing else changes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from . import modelcat

# Bare `@<alias>` names that pick claude. Claude has no on-disk model catalog
# to resolve against (unlike codex's models_cache.json), so the family
# aliases its --model flag documents are pinned here; anything starting
# "claude" passes through as-is for full slugs.
CLAUDE_MODEL_ALIASES = frozenset({"opus", "sonnet", "haiku", "fable"})

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


def _resolve_codex(name: str) -> str | None:
    """Exact codex slug for `name`, or None when it cannot be resolved.
    Unlike `tandem sub`, an unresolvable name here means passthrough, not a
    loud error — blocking would destroy the typed prompt. With no catalog a
    bare name is never treated as a codex model: modelcat.resolve would pass
    it through verbatim, which would route every unknown @token to codex."""
    models = modelcat.load_catalog()
    if models is None:
        return None
    try:
        slug = modelcat.resolve(name, models)
    except modelcat.UnknownModel:
        return None
    return slug or None


def _bare_model(name: str, participants: list[str]) -> RouteDecision | None:
    n = re.sub(r"[^a-z0-9]", "", name.lower())
    if "claude" in participants and (
            n in CLAUDE_MODEL_ALIASES or n.startswith("claude")):
        return RouteDecision("claude", name, _reason("claude", name))
    if "codex" in participants:
        slug = _resolve_codex(name)
        if slug:
            return RouteDecision("codex", slug, _reason("codex", slug))
    return None


def parse_prefix(
    prompt: str, participants: list[str]
) -> tuple[RouteDecision, str] | None:
    """(decision, prompt body) when the first token is a recognized @target
    with a non-empty body; None otherwise (passthrough)."""
    parts = prompt.strip().split(None, 1)   # any whitespace: a shift-enter
    if len(parts) < 2:                      # after the token still routes
        return None
    first, body = parts[0], parts[1].strip()
    m = _TOKEN_RE.fullmatch(first)
    if m is None or not body:
        return None
    name, model = m.group(1), m.group(2)
    if name in participants:
        if model is None:
            return RouteDecision(name, "", _reason(name, "")), body
        if name == "codex":
            # Explicit @codex:name mirrors `tandem sub` semantics: resolve
            # against the catalog when there is one, verbatim otherwise; a
            # standin ("gpt") resolves to "" = harness-only. Only a name the
            # catalog positively rejects is passthrough.
            try:
                slug = modelcat.resolve(model, modelcat.load_catalog())
            except modelcat.UnknownModel:
                return None
            model = slug
        return RouteDecision(name, model, _reason(name, model)), body
    if model is not None:
        return None      # `@notaharness:x` names nothing routable
    decision = _bare_model(name, participants)
    return (decision, body) if decision else None


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
