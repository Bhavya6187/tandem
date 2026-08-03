"""The tandem-model brief header: how a dispatching session asks for a
specific codex model.

The orchestrating model can only reach the relay through prompt text, so
the request travels as the brief's first line (`tandem-model: <name>`); the
relay forwards it byte-for-byte and `tandem sub` — this module — does the
parsing and the translation. Translation resolves the user's words against
codex's own on-disk catalog because codex needs an exact slug: a wrong one
costs a full API round-trip and 400s with no suggestions (observed live,
codex-cli 0.145.0 on a ChatGPT account, 2026-08-03), and the valid set is
account- and version-specific, so no hardcoded table can stay fresh."""

from __future__ import annotations

import json
import re

from . import paths

# Public protocol, like ops.BLOCKED_HEADER: the plugin's gpt agent
# description tells the orchestrator to emit this exact line shape, and
# dispatching models match on the footer. Change either and instructions rot.
HEADER_PREFIX = "tandem-model:"
_HEADER_RE = re.compile(r"^tandem-model:[ \t]*([A-Za-z0-9._/:-]{1,64})$")


class UnknownModel(ValueError):
    """The requested name matched nothing (or too much) in the catalog."""


def split_model_header(task: str) -> tuple[str, str]:
    """(requested model, task without the header line). A first line that
    does not full-match the grammar stays in the brief untouched — no
    guessing, no partial strips."""
    first, sep, rest = task.partition("\n")
    m = _HEADER_RE.match(first)
    if not m:
        return "", task
    return m.group(1), rest if sep else ""


def load_catalog() -> list[dict] | None:
    """The catalog's models array, or None when it cannot be read — the
    caller then passes the requested name through verbatim rather than
    refusing to dispatch over bookkeeping.

    A catalog with no usable entries is None, not []: an empty list is a
    catalog that answers "no such model" to everything, which would refuse
    every dispatch with an error listing nothing."""
    try:
        data = json.loads(paths.codex_models_cache_path().read_text())
    except (OSError, ValueError):
        return None
    models = data.get("models") if isinstance(data, dict) else None
    if not isinstance(models, list):
        return None
    return [m for m in models if isinstance(m, dict)] or None


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def resolve(name: str, models: list[dict] | None) -> str:
    """The exact slug for a user-worded model name.

    Exact normalized match on slug or display name wins; else a normalized
    substring match that hits exactly one visible model; else UnknownModel
    listing the visible slugs — raised before codex is invoked, so the
    round-trip that would 400 is never spent."""
    if models is None:
        return name
    visible = [m for m in models
               if m.get("visibility") != "hide"
               and isinstance(m.get("slug"), str)]
    n = _norm(name)
    for m in visible:
        if n and (n == _norm(m["slug"])
                  or n == _norm(str(m.get("display_name") or ""))):
            return m["slug"]
    hits = {m["slug"] for m in visible
            if n and (n in _norm(m["slug"])
                      or n in _norm(str(m.get("display_name") or "")))}
    if len(hits) == 1:
        return next(iter(hits))
    raise UnknownModel(
        f"unknown model {name!r}; this codex offers: "
        + ", ".join(m["slug"] for m in visible))


def model_footer(slug: str) -> str:
    return f"[tandem-sub model: {slug}]"
