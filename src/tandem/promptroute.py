"""The chat window's routing grammar: leading `/harness[:model]` or
`@harness:model`. Bare @mentions remain file mentions; only an explicit
model selector opts into @ routing. Other text passes through verbatim.

Returning None means "not a route". Raising RouteError means the user
clearly wrote a route that tandem cannot honor (a non-participant, or a
model name its harness would reject) — the composer shows the message and
keeps the prompt for editing rather than sending it anywhere.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from . import modelcat

HARNESSES = ("claude", "codex", "opencode")

# The name must end the token: `/codex/README.md` is a path, not a route,
# because a slash follows where whitespace or the end of input must be. A
# model name may not carry a slash either, with one exception below.
_MODEL = r"[A-Za-z0-9._-]+"
_ROUTE_RE = re.compile(
    rf"[/@]({'|'.join(HARNESSES)})(?::({_MODEL}(?:/{_MODEL})*))?(?=\s|$)")


@dataclass(frozen=True)
class Route:
    harness: str
    model: str | None = None   # None = keep the harness's pin; "" = clear it


class RouteError(ValueError):
    """A route the user meant, that tandem cannot honor."""


def parse_route(
    prompt: str, participants: Sequence[str],
) -> tuple[Route, str] | None:
    """(route, body) for a prompt that starts with a harness route; None
    when the prompt is ordinary text or someone else's slash command."""
    text = prompt.lstrip()
    m = _ROUTE_RE.match(text)
    if m is None:
        return None
    harness, model = m.group(1), m.group(2)
    if text.startswith("@") and model is None:
        return None
    # The exception: opencode names its models `provider/modelID`, and the
    # id may itself carry slashes (`openrouter/anthropic/claude-sonnet-4`),
    # because opencode splits on the first one only. A slash belongs inside
    # a model name there and nowhere else: `/claude:haiku/x` is a path like
    # `/codex/README.md`, so it is not a route at all — not even a
    # non-participant one, which is why this precedes the check below.
    if model and "/" in model and harness != "opencode":
        return None
    if harness not in participants:
        raise RouteError(
            f"{harness} is not a participant in this session "
            f"(participants: {', '.join(participants)})")
    body = text[m.end():].strip()
    if model is None:
        return Route(harness), body
    if model == "default":
        return Route(harness, ""), body
    try:
        if harness == "codex":
            model = modelcat.resolve(model, modelcat.load_catalog())
        elif harness == "claude":
            model = modelcat.resolve_claude(model)
    except modelcat.UnknownModel as exc:
        raise RouteError(str(exc)) from exc
    return Route(harness, model), body


def is_passthrough_command(prompt: str) -> bool:
    """A leading slash that is not a route belongs to the harness."""
    return prompt.lstrip().startswith("/")
