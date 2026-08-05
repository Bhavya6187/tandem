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
# NAME may carry internal spaces ("5.4 mini") because models get spoken, not
# typed; resolve() normalizes punctuation and spaces away. It may not start or
# end with one, and the line's trailing whitespace is stripped before matching
# — a stray space or a CRLF must not turn a header into task text.
_NAME = r"[A-Za-z0-9._/:-](?:[A-Za-z0-9._/ :-]{0,62}[A-Za-z0-9._/:-])?"
_HEADER_RE = re.compile(
    rf"^{re.escape(HEADER_PREFIX)}[ \t]*({_NAME})$", re.IGNORECASE)


# Names that pick a *harness*, not a model. "ask gpt" names nothing to
# translate, so a header carrying one is a standin for "no preference": it
# resolves to the empty model and falls through the usual precedence
# (`[subagents] model`, else codex's own default). The gpt agent description
# still tells the orchestrator not to emit a header for a bare "gpt" — this is
# the backstop for the ones that do, so obeying and not obeying converge
# instead of one of them hard-failing. Compared against _norm()'d names, so
# entries here are normalized (lowercase, alphanumerics only).
STANDIN_MODELS = frozenset({"gpt", "codex"})
# What to call the empty model in user-facing feedback: nothing was picked
# here, so naming a slug would be a lie and "" reads as a bug.
DEFAULT_MODEL_LABEL = "codex default"


class UnknownModel(ValueError):
    """The requested name matched nothing (or too much) in the catalog."""


class MalformedHeader(ValueError):
    """The first line announced a header but the name is unusable."""


def split_model_header(task: str) -> tuple[str, str]:
    """(requested model, task without the header line).

    A first line that does not open with `tandem-model:` is ordinary task
    text and is returned untouched. One that does open with it must parse:
    a near-miss (decorated name, empty name, an over-long one, prose) raises
    MalformedHeader rather than silently falling through, because the
    fall-through is the exact failure this protocol exists to prevent — the
    worker would run on the config default and the header line would ship to
    codex as part of the brief, with nothing said to anyone."""
    first, sep, rest = task.partition("\n")
    first = first.rstrip()
    if not first.lower().startswith(HEADER_PREFIX):
        return "", task
    m = _HEADER_RE.match(first)
    if not m:
        raise MalformedHeader(f"malformed tandem-model header: {first}")
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


def _substring_hits(n: str, visible: list[dict]) -> set[str]:
    """Slugs whose slug or display name contains the normalized query `n`.

    An empty query hits nothing on purpose: "" sits inside every string, so
    matching it would resolve to whatever the catalog happens to list —
    a model nobody asked for. That guard is what makes the callers below
    safe to write branchlessly."""
    return {m["slug"] for m in visible
            if n and (n in _norm(m["slug"])
                      or n in _norm(str(m.get("display_name") or "")))}


def resolve(name: str, models: list[dict] | None) -> str:
    """The exact slug for a user-worded model name, or "" for no preference.

    Exact normalized match on slug or display name wins; else a generic
    standin (STANDIN_MODELS) resolves to the empty model; else a normalized
    substring match that hits exactly one visible model; else the same
    substring match retried with one leading family token stripped; else
    UnknownModel listing the visible slugs — raised before codex is
    invoked, so the round-trip that would 400 is never spent.

    The standin sits after exact matching so a codex that really ships a
    model named `gpt` still resolves it, and before substring matching
    because substrings are what made `gpt` ambiguous-fail in the first
    place. It applies with no catalog too: `gpt` must never reach
    `codex -m` verbatim, which 400s after a full round-trip.

    The stripped retry sits last so nothing it can do changes an answer the
    earlier passes already gave, and it keeps the one-hit rule: `gpt-5`
    strips to `5`, still hits several models, and still fails loudly. No
    pass resolves to a model the user did not ask for."""
    n = _norm(name)
    if models is None:
        return "" if n in STANDIN_MODELS else name
    visible = [m for m in models
               if m.get("visibility") != "hide"
               and isinstance(m.get("slug"), str)]
    for m in visible:
        if n and (n == _norm(m["slug"])
                  or n == _norm(str(m.get("display_name") or ""))):
            return m["slug"]
    if n in STANDIN_MODELS:
        return ""
    hits = _substring_hits(n, visible)
    if len(hits) == 1:
        return next(iter(hits))
    # Observed live: an agent asked for "gpt terra" emits `gpt-terra`, and
    # normalization keeps that query contiguous — `gptterra` cannot sit
    # inside `gpt56terra` — so the pass above misses an unambiguous intent.
    # Strip one leading family token and retry. At most one standin can
    # prefix a given query, so the set's iteration order cannot matter, and
    # a query that *was* just the token already returned at the standin arm
    # — the remainder here is never empty, and an empty one would hit
    # nothing anyway.
    stripped = next((n[len(t):] for t in STANDIN_MODELS if n.startswith(t)), "")
    hits = _substring_hits(stripped, visible)
    if len(hits) == 1:
        return next(iter(hits))
    raise UnknownModel(
        f"unknown model {name!r}; this codex offers: "
        + ", ".join(m["slug"] for m in visible))


def model_label(slug: str) -> str:
    """What to call the model that actually ran, for humans and for the
    dispatching session. Empty means nobody picked one — a standin header,
    or no config default — so codex chose, and the feedback says that
    rather than printing an empty name."""
    return slug or DEFAULT_MODEL_LABEL


def model_footer(slug: str) -> str:
    return f"[tandem-sub model: {model_label(slug)}]"
