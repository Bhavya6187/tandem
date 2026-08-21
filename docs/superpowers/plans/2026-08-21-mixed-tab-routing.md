# Mixed Tab with Manual @-Routing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a fourth "mixed" tab to tandem where a prompt starting with `@claude` / `@codex` / `@opencode` / `@<model>` routes that turn to the named harness (optionally pinning a model), building the full intercept→stash→block→flip→inject pipeline that a v2 auto-router will later plug into.

**Architecture:** A `UserPromptSubmit` hook (registered in both claude and codex via the existing tandem plugin) intercepts prompts typed in the mixed tab, parses the `@` prefix, stashes a route request under `$TANDEM_HOME/tmp/`, and blocks the local turn. The runner's new mixer thread picks the request up, arms the existing flip machinery toward the route's target, and the flip loop relaunches the target harness with the pinned model on its argv and injects the stashed prompt into its pty as a bracketed paste. Tab state (harness vs mixed, sticky focus) lives in a new session-meta column and a frame-state file the hook reads.

**Tech Stack:** Python 3.11+ stdlib + existing deps only (click, ptyprocess, pytest). No new dependencies.

**Spec:** `docs/superpowers/specs/2026-08-21-mixed-tab-routing-design.md` — read it first; this plan argues from it. One deliberate improvement over the spec: model pinning rides the *launch argv* of the routed flip (`claude --model`, `codex -m`, `opencode --model`) instead of injecting a `/model` slash command — routed turns always relaunch the target CLI, so launch flags are strictly more reliable, and spec verification item 3 becomes an argv check in the live gate.

## Global Constraints

- Python 3.11+; stdlib + click + ptyprocess only (no new dependencies; `uv.lock` untouched except by version bumps).
- Every config error yields defaults; configuration must never break a launch (config.py module docstring is the contract).
- Hook commands ALWAYS exit 0 and are registered as `tandem <cmd> || true` (see `hook_route_cmd` docstring for why).
- All route/frame files live under `paths.tandem_home()`; writes are write-then-rename (pinstash pattern); every read tolerates absent/corrupt files.
- The pty pump thread must never do file I/O or take locks in its input path (`FlipMonitor.flip_pressed` docstring is the contract).
- Cross-thread mutable state uses plain attribute assignment under the GIL, documented at the assignment (the `monitor.transcript` pattern) — no new locks unless a task says so.
- Test commands: `uv run pytest tests/<file> -q` per task; full suite `uv run pytest -q` before each commit. The suite (~744 tests) must stay green.
- Repo pushes: origin main is PR-only; all commits land on branch `mixed-tab-routing`. `docs/superpowers/` is ignored via `.git/info/exclude`, so doc commits need `git add -f`.
- Comment style: explain constraints the code can't show, match the repo's dense-docstring idiom.

## File Structure

| File | Responsibility |
|---|---|
| `src/tandem/promptroute.py` (new) | Pure @-prefix grammar: parse the first token into a `RouteDecision`, cross-harness bare-model resolution, hook decision JSON. Mirrors `hookroute.py`'s pure-logic style. |
| `src/tandem/routefile.py` (new) | Route-request + frame-state files under `$TANDEM_HOME/tmp/`: atomic writes, pending/dispatched lifecycle, TTL. |
| `src/tandem/tabs.py` (new) | `TabState`: the tab cycle (participants + mixed), sticky focus, pending flip moves, cancel semantics. Pure in-memory state machine. |
| `src/tandem/state.py` | New `meta` JSON column on sessions (+ additive migration) with `get_meta`/`set_meta`. |
| `src/tandem/config.py` | `FrameConfig.mixed` toggle (`[frame] mixed`, default true). |
| `src/tandem/harness/base.py` + 3 adapters | `model_argv(model)` (launch-time model flag) and `prompt_hook_capable` class attr. |
| `src/tandem/warm.py` | `LaunchRecipe.model` + `build_launch(..., model="")`. |
| `src/tandem/frame.py` | `StatusBar` mixed-slot rendering (`mode` dict). |
| `src/tandem/ptyrun.py` | `PtyControl.write()`; `FrameIO.mode` callable + repaint-on-change. |
| `src/tandem/runner.py` | Mixer thread (frame-file persistence + route pickup + routed arm), injector thread, warm-fire suppression on routed flips. |
| `src/tandem/flip.py` | Tab-aware flip loop: target override, meta persistence, inject carry, model-aware standby gate. |
| `src/tandem/cli.py` | `tandem hook-prompt` command. |
| `plugin/hooks/hooks.json`, `plugin/.codex-plugin/plugin.json`, `src/tandem/plugin_setup.py` | UserPromptSubmit registration; codex-side plugin install. |
| `tests/test_promptroute.py`, `tests/test_routefile.py`, `tests/test_tabs.py` (new) + additions to `test_state`, `test_config`, `test_warm`, `test_frame`, `test_ptyrun`, `test_runner`, `test_flip`, `test_cli`, `test_plugin_setup` | Unit coverage per module. |

Interface note for all tasks: "participants" is always the ordered list from `PairedSession.participants` (e.g. `["claude", "codex", "opencode"]`); harness ids are the strings `"claude"`, `"codex"`, `"opencode"`; the mixed tab is the string `"mixed"` and is never a participant.

---

### Task 1: `promptroute.py` — the @-prefix grammar

**Files:**
- Create: `src/tandem/promptroute.py`
- Test: `tests/test_promptroute.py`

**Interfaces:**
- Consumes: `modelcat.resolve`, `modelcat.load_catalog`, `modelcat.UnknownModel` (existing).
- Produces (later tasks rely on these exact names):
  - `RouteDecision(harness: str, model: str = "", reason: str = "")` frozen dataclass.
  - `parse_prefix(prompt: str, participants: list[str]) -> tuple[RouteDecision, str] | None` — `(decision, body)` or None for passthrough.
  - `route_prompt(prompt: str, focus: str, participants: list[str]) -> tuple[RouteDecision, str] | None` — like `parse_prefix` but returns None when the target equals `focus` (stay is free).
  - `CLAUDE_MODEL_ALIASES` frozenset.

- [ ] **Step 1: Write the failing tests**

```python
"""tests/test_promptroute.py"""
import pytest

from tandem import promptroute
from tandem.promptroute import RouteDecision, parse_prefix, route_prompt

PARTS = ["claude", "codex", "opencode"]


def test_harness_prefix_routes():
    d, body = parse_prefix("@codex fix the flaky test", PARTS)
    assert d == RouteDecision(harness="codex", model="", reason="→ codex")
    assert body == "fix the flaky test"


def test_prefix_is_first_token_only():
    assert parse_prefix("please ask @codex to fix it", PARTS) is None


def test_unknown_at_token_is_passthrough():
    # claude file mentions must survive the mixed tab
    assert parse_prefix("@src/foo.py explain this", PARTS) is None


def test_bare_prefix_with_no_body_is_passthrough():
    assert parse_prefix("@codex", PARTS) is None
    assert parse_prefix("@codex   ", PARTS) is None


def test_non_participant_harness_is_passthrough():
    assert parse_prefix("@codex do it", ["claude", "opencode"]) is None


def test_harness_colon_model():
    d, body = parse_prefix("@opencode:anthropic/claude-sonnet-5 go", PARTS)
    assert d.harness == "opencode"
    assert d.model == "anthropic/claude-sonnet-5"
    assert body == "go"


def test_codex_colon_model_resolves_via_catalog(monkeypatch):
    monkeypatch.setattr(promptroute.modelcat, "load_catalog",
                        lambda: [{"slug": "gpt-5.3-codex", "visibility": "show"}])
    d, _ = parse_prefix("@codex:5.3 go", PARTS)
    assert d.model == "gpt-5.3-codex"


def test_codex_colon_unresolvable_model_is_passthrough(monkeypatch):
    monkeypatch.setattr(promptroute.modelcat, "load_catalog",
                        lambda: [{"slug": "gpt-5.3-codex", "visibility": "show"}])
    assert parse_prefix("@codex:no-such-model go", PARTS) is None


def test_codex_colon_model_verbatim_without_catalog(monkeypatch):
    # mirrors `tandem sub`: no catalog = pass the name through
    monkeypatch.setattr(promptroute.modelcat, "load_catalog", lambda: None)
    d, _ = parse_prefix("@codex:gpt-5.3-codex go", PARTS)
    assert d.model == "gpt-5.3-codex"


def test_newline_after_token_still_routes():
    d, body = parse_prefix("@codex\nfix the flaky test", PARTS)
    assert d.harness == "codex" and body == "fix the flaky test"


def test_bare_claude_alias_routes_to_claude():
    d, body = parse_prefix("@haiku summarize the diff", PARTS)
    assert d == RouteDecision(harness="claude", model="haiku",
                              reason="→ claude · haiku")
    assert body == "summarize the diff"


def test_bare_codex_model_needs_catalog(monkeypatch):
    monkeypatch.setattr(promptroute.modelcat, "load_catalog", lambda: None)
    assert parse_prefix("@gpt-5.3-codex go", PARTS) is None
    monkeypatch.setattr(promptroute.modelcat, "load_catalog",
                        lambda: [{"slug": "gpt-5.3-codex", "visibility": "show"}])
    d, _ = parse_prefix("@gpt-5.3-codex go", PARTS)
    assert d == RouteDecision(harness="codex", model="gpt-5.3-codex",
                              reason="→ codex · gpt-5.3-codex")


def test_route_prompt_stay_is_none():
    assert route_prompt("@codex go", focus="codex", participants=PARTS) is None


def test_route_prompt_move():
    got = route_prompt("@codex go", focus="claude", participants=PARTS)
    assert got is not None and got[0].harness == "codex"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_promptroute.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'tandem.promptroute'`

- [ ] **Step 3: Write the implementation**

```python
"""src/tandem/promptroute.py

The mixed tab's @-prefix grammar: v1 of the routing seam.

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
    """The claude/codex model a bare `@name` asks for, or None.

    The claude arm is gated on a path-free raw token because a file mention
    must never be eaten: `@CLAUDE.md`, `@.claude/settings.json` and
    `@claude/agents/foo.md` all normalize to something starting "claude", and
    routing one would block a typed prompt to run `claude --model CLAUDE.md`.
    No claude model name carries a '/' or a '.', so full slugs
    (`claude-sonnet-5`) still route; a dotted spelling like `@claude-3.5`
    falls back to passthrough, which is the cheap failure of the two."""
    n = re.sub(r"[^a-z0-9]", "", name.lower())
    path_free = "/" not in name and "." not in name
    if "claude" in participants and path_free and (
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_promptroute.py -q`
Expected: PASS (13 tests)

- [ ] **Step 5: Full suite, then commit**

Run: `uv run pytest -q` — expected green.

```bash
git add src/tandem/promptroute.py tests/test_promptroute.py
git commit -m "feat: @-prefix route grammar (promptroute)"
```

---

### Task 2: `routefile.py` — route-request and frame-state files

**Files:**
- Create: `src/tandem/routefile.py`
- Test: `tests/test_routefile.py`

**Interfaces:**
- Consumes: `paths.tandem_home()`.
- Produces:
  - `RouteRequest(target: str, model: str, prompt: str, source: str, reason: str, state: str = "pending")` dataclass.
  - `write_route(tandem_id: str, req: RouteRequest) -> None`
  - `read_route(tandem_id: str) -> RouteRequest | None` — only fresh (≤ `ROUTE_TTL`) well-formed requests; state carried as-is.
  - `mark_dispatched(tandem_id: str, req: RouteRequest) -> None`
  - `clear_route(tandem_id: str) -> None`
  - `write_frame_state(tandem_id: str, snapshot: dict) -> None`
  - `read_frame_state(tandem_id: str) -> dict | None`
  - `ROUTE_TTL = 600`
- Files: `$TANDEM_HOME/tmp/<tandem_id>-route.json`, `$TANDEM_HOME/tmp/<tandem_id>-frame.json`.

- [ ] **Step 1: Write the failing tests**

```python
"""tests/test_routefile.py"""
import json
import os
import time

import pytest

from tandem import routefile
from tandem.routefile import RouteRequest


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("TANDEM_HOME", str(tmp_path))
    return tmp_path


REQ = RouteRequest(target="codex", model="gpt-5.3-codex",
                   prompt="fix it", source="claude", reason="→ codex")


def test_route_round_trip():
    routefile.write_route("abc123", REQ)
    got = routefile.read_route("abc123")
    assert got == REQ and got.state == "pending"


def test_read_missing_is_none():
    assert routefile.read_route("abc123") is None


def test_read_corrupt_is_none(home):
    p = home / "tmp" / "abc123-route.json"
    p.parent.mkdir(parents=True)
    p.write_text("{not json")
    assert routefile.read_route("abc123") is None


def test_read_stale_is_none(home):
    routefile.write_route("abc123", REQ)
    p = home / "tmp" / "abc123-route.json"
    old = time.time() - routefile.ROUTE_TTL - 5
    os.utime(p, (old, old))
    assert routefile.read_route("abc123") is None


def test_mark_dispatched_then_clear():
    routefile.write_route("abc123", REQ)
    routefile.mark_dispatched("abc123", REQ)
    got = routefile.read_route("abc123")
    assert got is not None and got.state == "dispatched"
    routefile.clear_route("abc123")
    assert routefile.read_route("abc123") is None


def test_clear_missing_is_quiet():
    routefile.clear_route("nope")   # must not raise


def test_frame_state_round_trip():
    routefile.write_frame_state("abc123", {"tab": "mixed", "focus": "codex"})
    assert routefile.read_frame_state("abc123") == {
        "tab": "mixed", "focus": "codex"}
    assert routefile.read_frame_state("other") is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_routefile.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'tandem.routefile'`

- [ ] **Step 3: Write the implementation**

```python
"""src/tandem/routefile.py

Route-request and frame-state files: the two channels between the
UserPromptSubmit hook (a separate short-lived process inside the harness)
and the tandem frame that owns the session.

- frame state (`<id>-frame.json`): frame → hook. Which tab is active and
  which harness has focus; rewritten by the runner's mixer thread on every
  tab change. The hook treats a missing/corrupt file as "not the mixed tab"
  and stays silent — the safe default.
- route request (`<id>-route.json`): hook → frame. One pending routed turn.
  Lifecycle: the hook writes `pending` (durably, BEFORE blocking the turn —
  the stash is what makes the prompt unlosable); the frame flips it to
  `dispatched` at pickup so its own next run cannot re-arm on it; the
  injector deletes it once the prompt has landed in the target. A
  `dispatched` file still on disk at the next frame start is a routed prompt
  that never landed; the frame surfaces it there — but only while it is
  within ROUTE_TTL, since that is the only window any reader here can see.
  An older leftover is invisible to `read_route` and is cleared silently at
  frame start rather than replayed into a stale session.

Best-effort like pinstash: writes go through `util.write_file_atomic`
(fsync + rename in the destination dir) so a concurrent read never sees a
torn entry, and every failure degrades to "no route" rather than raising
into a hook or the frame."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass

from . import paths, util

ROUTE_TTL = 600


@dataclass(frozen=True)
class RouteRequest:
    target: str
    model: str
    prompt: str
    source: str
    reason: str
    state: str = "pending"


def _route_path(tandem_id: str):
    return paths.tandem_home() / "tmp" / f"{tandem_id}-route.json"


def _frame_path(tandem_id: str):
    return paths.tandem_home() / "tmp" / f"{tandem_id}-frame.json"


def _write_json(path, obj: dict) -> None:
    try:
        # serialize first, so a bad snapshot leaves no file and no scratch
        # file behind; write_file_atomic then does mkdir + fsync + rename
        # under a random scratch name, which is what keeps the hook and the
        # frame's mixer thread from clobbering each other mid-write
        util.write_file_atomic(path, json.dumps(obj))
    # TypeError/ValueError: an unserializable snapshot must not raise into
    # the mixer thread that writes it — a missing file already means "not
    # the mixed tab", which is the safe default on the reading side too
    except (OSError, TypeError, ValueError):
        pass


def _read_json(path) -> dict | None:
    try:
        obj = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def write_route(tandem_id: str, req: RouteRequest) -> None:
    _write_json(_route_path(tandem_id), asdict(req))


def read_route(tandem_id: str) -> RouteRequest | None:
    p = _route_path(tandem_id)
    try:
        if time.time() - p.stat().st_mtime > ROUTE_TTL:
            return None
    except OSError:
        return None
    obj = _read_json(p)
    if obj is None:
        return None
    try:
        return RouteRequest(**{k: str(obj[k]) for k in
                               ("target", "model", "prompt", "source",
                                "reason")},
                            state=str(obj.get("state", "pending")))
    except (KeyError, TypeError):
        return None


def mark_dispatched(tandem_id: str, req: RouteRequest) -> None:
    write_route(tandem_id, RouteRequest(
        req.target, req.model, req.prompt, req.source, req.reason,
        state="dispatched"))


def clear_route(tandem_id: str) -> None:
    try:
        _route_path(tandem_id).unlink(missing_ok=True)
    except OSError:
        pass


def write_frame_state(tandem_id: str, snapshot: dict) -> None:
    _write_json(_frame_path(tandem_id), snapshot)


def read_frame_state(tandem_id: str) -> dict | None:
    return _read_json(_frame_path(tandem_id))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_routefile.py -q`
Expected: PASS (7 tests)

- [ ] **Step 5: Full suite, then commit**

```bash
git add src/tandem/routefile.py tests/test_routefile.py
git commit -m "feat: route-request and frame-state files (routefile)"
```

---

### Task 3: session `meta` column in the state store

**Files:**
- Modify: `src/tandem/state.py` (schema at top, `StateStore.__init__` migration, new accessors after `touch_sync`)
- Test: `tests/test_state.py` (append)

**Interfaces:**
- Produces: `StateStore.get_meta(tandem_id: str) -> dict`, `StateStore.set_meta(tandem_id: str, meta: dict) -> None`. Meta keys used later: `{"tab": "harness"|"mixed", "mixed_focus": "<harness-id>"}`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_state.py`)

```python
def test_meta_round_trip(tmp_path):
    with StateStore(tmp_path / "s.db") as store:
        s = store.create_session("/w", "claude", ["claude", "codex"],
                                 {"claude": "c1", "codex": None})
        assert store.get_meta(s.tandem_id) == {}
        store.set_meta(s.tandem_id, {"tab": "mixed", "mixed_focus": "codex"})
        assert store.get_meta(s.tandem_id) == {
            "tab": "mixed", "mixed_focus": "codex"}


def test_meta_column_added_to_existing_db(tmp_path):
    """A pre-mixed-tab DB (no meta column) is migrated in place, keeping
    its rows — unlike the participants check, which moves the DB aside."""
    import sqlite3
    db = tmp_path / "s.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE sessions (tandem_id TEXT PRIMARY KEY, cwd TEXT NOT NULL,"
        " active TEXT NOT NULL, participants TEXT NOT NULL,"
        " native_session_ids TEXT NOT NULL DEFAULT '{}',"
        " created_at TEXT NOT NULL, last_sync_at TEXT, last_used_at TEXT);"
        "INSERT INTO sessions (tandem_id, cwd, active, participants,"
        " created_at) VALUES ('t1', '/w', 'claude',"
        " '[\"claude\", \"codex\"]', '2026-01-01');")
    conn.commit()
    conn.close()
    with StateStore(db) as store:
        assert store.get_session("t1") is not None
        assert store.get_meta("t1") == {}
        store.set_meta("t1", {"tab": "mixed"})
        assert store.get_meta("t1") == {"tab": "mixed"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_state.py -q`
Expected: the two new tests FAIL (`AttributeError: ... get_meta` / `no such column: meta`)

- [ ] **Step 3: Implement**

In `_SCHEMA`, add to the `sessions` CREATE TABLE (after `last_used_at TEXT`):

```sql
    meta TEXT NOT NULL DEFAULT '{}'
```

In `StateStore.__init__`, after the `executescript(_SCHEMA)` / `commit()` pair:

```python
        self._ensure_meta_column()
```

New methods on `StateStore` (place `_ensure_meta_column` next to `_schema_stale`, the accessors after `touch_sync`):

```python
    def _ensure_meta_column(self) -> None:
        """Additive migration: CREATE TABLE IF NOT EXISTS never retrofits a
        column onto an existing table, and (unlike a missing participants
        column) a missing meta column carries no data worth abandoning the
        DB over — ALTER in place and keep every session."""
        names = {r[1] for r in self._conn.execute("PRAGMA table_info(sessions)")}
        if "meta" not in names:
            with self._conn:
                self._conn.execute(
                    "ALTER TABLE sessions ADD COLUMN meta TEXT"
                    " NOT NULL DEFAULT '{}'")

    def get_meta(self, tandem_id: str) -> dict:
        row = self._conn.execute(
            "SELECT meta FROM sessions WHERE tandem_id = ?", (tandem_id,)
        ).fetchone()
        if row is None:
            return {}
        try:
            obj = json.loads(row["meta"])
        except ValueError:
            return {}
        return obj if isinstance(obj, dict) else {}

    def set_meta(self, tandem_id: str, meta: dict) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE sessions SET meta = ? WHERE tandem_id = ?",
                (json.dumps(meta), tandem_id),
            )
```

Note: `PairedSession` deliberately does NOT grow a `meta` field — nothing reads it in the hot path, and widening the dataclass would touch every constructor in the test suite. Meta is read exactly where tab state is needed (flip loop startup).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_state.py -q` — expected PASS.

- [ ] **Step 5: Full suite, then commit**

```bash
git add src/tandem/state.py tests/test_state.py
git commit -m "feat: session meta column for tab state"
```

---

### Task 4: `[frame] mixed` config toggle

**Files:**
- Modify: `src/tandem/config.py` (`FrameConfig`, `load_frame_config`)
- Test: `tests/test_config.py` (append)

**Interfaces:**
- Produces: `FrameConfig.mixed: bool = True`, parsed from `[frame] mixed`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_config.py`, following that file's existing TANDEM_HOME/tmp fixture pattern for writing a config.toml)

```python
def test_frame_mixed_default_true(tmp_path, monkeypatch):
    monkeypatch.setenv("TANDEM_HOME", str(tmp_path))
    assert load_frame_config().mixed is True


def test_frame_mixed_opt_out(tmp_path, monkeypatch):
    monkeypatch.setenv("TANDEM_HOME", str(tmp_path))
    (tmp_path / "config.toml").write_text("[frame]\nmixed = false\n")
    assert load_frame_config().mixed is False


def test_frame_mixed_malformed_falls_back(tmp_path, monkeypatch):
    monkeypatch.setenv("TANDEM_HOME", str(tmp_path))
    (tmp_path / "config.toml").write_text('[frame]\nmixed = "nope"\n')
    assert load_frame_config().mixed is True
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_config.py -q`
Expected: new tests FAIL (`AttributeError: 'FrameConfig' object has no attribute 'mixed'`)

- [ ] **Step 3: Implement**

In `FrameConfig` add `mixed: bool = True  # the mixed tab joins the flip cycle`; in `load_frame_config` read it exactly like `bar`:

```python
    mixed = raw.get("mixed")
```
and in the constructor call: `mixed=mixed if isinstance(mixed, bool) else d.mixed,`

- [ ] **Step 4: Run to verify pass**, **Step 5: Full suite + commit**

```bash
git add src/tandem/config.py tests/test_config.py
git commit -m "feat: [frame] mixed config toggle"
```

---

### Task 5: adapter `model_argv` / `prompt_hook_capable` + model-aware `build_launch`

**Files:**
- Modify: `src/tandem/harness/base.py`, `src/tandem/harness/claude_code.py`, `src/tandem/harness/codex.py`, `src/tandem/harness/opencode.py`, `src/tandem/warm.py`
- Test: `tests/test_warm.py` (append), `tests/test_toolmap.py` or a small new block in `tests/test_warm.py` for adapter attrs (keep it in test_warm.py)

**Interfaces:**
- Produces:
  - `HarnessAdapter.model_argv(model: str) -> list[str]` — argv tail that pins the model for one interactive launch; `[]` means "this harness cannot pin a model at launch" (the caller degrades to harness-only routing).
  - `HarnessAdapter.prompt_hook_capable: bool` — whether this harness runs tandem's UserPromptSubmit hook (claude True, codex True, opencode False until the live gate proves otherwise).
  - `LaunchRecipe.model: str = ""` and `build_launch(session, side, model="") -> LaunchRecipe`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_warm.py`)

```python
from tandem.harness import get_adapter


def test_model_argv_per_adapter():
    assert get_adapter("claude").model_argv("haiku") == ["--model", "haiku"]
    assert get_adapter("codex").model_argv("gpt-5.3-codex") == [
        "-m", "gpt-5.3-codex"]
    assert get_adapter("opencode").model_argv(
        "anthropic/claude-sonnet-5") == ["--model", "anthropic/claude-sonnet-5"]


def test_prompt_hook_capability_flags():
    assert get_adapter("claude").prompt_hook_capable is True
    assert get_adapter("codex").prompt_hook_capable is True
    assert get_adapter("opencode").prompt_hook_capable is False


def test_build_launch_appends_model_argv(tmp_path, monkeypatch):
    # session construction mirrors tests/test_flip.py's `sess` fixture;
    # reuse test_warm.py's own session helper if it already has one.
    monkeypatch.setenv("TANDEM_HOME", str(tmp_path / ".tandem"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / ".claude"))
    with StateStore() as store:
        sess = store.create_session(str(tmp_path / "proj"), "claude",
                                    ["claude", "codex"],
                                    {"claude": "c-id", "codex": "x-id"})
    recipe = build_launch(sess, "claude", model="haiku")
    assert recipe.model == "haiku"
    i = recipe.argv.index("--model")
    assert recipe.argv[i + 1] == "haiku"
    plain = build_launch(sess, "claude")
    assert plain.model == "" and "--model" not in plain.argv
```

(Before writing, read the top of `tests/test_warm.py` — if it already builds sessions through a fixture, reuse that fixture instead of the inline construction; the assertions are the contract.)

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_warm.py -q`
Expected: new tests FAIL (`AttributeError: model_argv` / `TypeError: build_launch() got an unexpected keyword argument 'model'`)

- [ ] **Step 3: Implement**

`harness/base.py`, on `HarnessAdapter` (near `quit_keystrokes`):

```python
    # Does this harness load tandem's plugin and run its UserPromptSubmit
    # hook? Decides whether the mixed tab can route *from* it (targets need
    # no hook — injection is a plain pty write). opencode stays False until
    # the live gate proves otherwise (spec verification item 2).
    prompt_hook_capable: bool = False

    def model_argv(self, model: str) -> list[str]:
        """argv tail that pins `model` for one interactive launch, or []
        when this harness cannot pin a model at launch (the routed flip then
        degrades to harness-only with a bar notice)."""
        return []
```

`claude_code.py` (class body): `prompt_hook_capable = True` and

```python
    def model_argv(self, model: str) -> list[str]:
        return ["--model", model]
```

`codex.py`: `prompt_hook_capable = True` and

```python
    def model_argv(self, model: str) -> list[str]:
        return ["-m", model]
```

`opencode.py`: leave `prompt_hook_capable` inherited (False) and

```python
    def model_argv(self, model: str) -> list[str]:
        # opencode wants provider-qualified names (`anthropic/claude-…`);
        # the @-grammar's explicit form (@opencode:provider/model) is the
        # only road here, and the name travels verbatim.
        return ["--model", model]
```

`warm.py`: add `model: str = ""` to `LaunchRecipe` (after `cwd`), and in `build_launch`:

```python
def build_launch(session: PairedSession, side: str, model: str = "") -> LaunchRecipe:
```
after `argv += load_harness_args(side)` — i.e. *after* the user's `[harness] args` and before `hook_argv_extra`, making the order interactive + user [args] + model pin + hook extras — insert:

```python
    if model:
        argv += adapter.model_argv(model)
```
The pin goes last of the two because an explicit per-turn route pin (`@codex:gpt-5.3`) is more specific intent than a static `[harness] args` entry, and both CLIs take the last occurrence of a flag: placed earlier, a user's `args = ["--model", …]` would silently defeat the route. Pass `model=model if model and adapter.model_argv(model) else ""` into the `LaunchRecipe(...)` constructor (recipe.model records what was actually pinned, so the standby freshness gate in Task 10 compares real launch state, not intent).

- [ ] **Step 4: Run to verify pass**: `uv run pytest tests/test_warm.py -q`

- [ ] **Step 5: Full suite, then commit**

```bash
git add src/tandem/harness/base.py src/tandem/harness/claude_code.py \
        src/tandem/harness/codex.py src/tandem/harness/opencode.py \
        src/tandem/warm.py tests/test_warm.py
git commit -m "feat: launch-time model pinning and prompt-hook capability per adapter"
```

---

### Task 6: `tabs.py` — the tab state machine

**Files:**
- Create: `src/tandem/tabs.py`
- Test: `tests/test_tabs.py`

**Interfaces:**
- Consumes: nothing (pure).
- Produces (Tasks 9–11 rely on these exact names):
  - `TabMove(kind: str, tab: str, focus: str, target: str = "")` with `kind in {"bar", "flip", "cancel"}`.
  - `TabState(participants: list[str], tab: str = "harness", focus: str = "")` with:
    - `.tab: str`, `.focus: str`, `.version: int` (bumped on every visible change; the mixer thread persists on version change)
    - `.press(active: str) -> TabMove` — one Ctrl-] press; applies bar moves immediately, records flip moves as pending, returns a cancel move when one was pending.
    - `.routed(target: str) -> bool` — *claims* the pending slot for a routed flip, keeping the current tab; returns False when a user press already owns it (the caller must then leave the route request pending and retry, never overwrite — see Task 10's route-pickup branch).
    - `.pending_target() -> str` — the pending flip's target, `""` when none (caller falls back to `session.next_active`).
    - `.settle(new_active: str) -> None` — commit the pending move after the flip landed; in the mixed tab, focus follows the new active.
    - `.cancelled() -> None` — clear pending (monitor wait was cancelled).
    - `.snapshot(active: str, routing_ok: bool = True) -> dict` — `{"tab", "focus", "routing_ok"}`; `focus` is `active` in the mixed tab (typing surface == shown harness), `""` otherwise.

Threading contract (document in the module docstring): `press` runs on the pty pump thread and must stay allocation-light and I/O-free; `routed` runs on the mixer thread; `pending_target`/`settle`/`cancelled` run on the flip loop between runs or the monitor thread. All state is plain attributes; the GIL makes single assignments atomic, same as `FlipMonitor.transcript`.

- [ ] **Step 1: Write the failing tests**

```python
"""tests/test_tabs.py"""
from tandem.tabs import TabMove, TabState

PARTS = ["claude", "codex", "opencode"]


def test_harness_cycle_presses_flip_to_next():
    t = TabState(PARTS)
    m = t.press("claude")
    assert m.kind == "flip" and m.target == "codex" and m.tab == "harness"
    assert t.pending_target() == "codex"
    t.settle("codex")
    assert t.tab == "harness" and t.pending_target() == ""


def test_last_harness_press_enters_mixed_bar_only():
    """First-ever mixed entry adopts the harness you came from; focus ==
    active means no process change — a bar-only move, applied immediately."""
    t = TabState(PARTS)
    m = t.press("opencode")
    assert m == TabMove(kind="bar", tab="mixed", focus="opencode")
    assert t.tab == "mixed" and t.focus == "opencode"
    assert t.pending_target() == ""


def test_mixed_entry_with_sticky_focus_elsewhere_is_a_flip():
    t = TabState(PARTS, tab="harness", focus="codex")
    m = t.press("opencode")
    assert m.kind == "flip" and m.target == "codex" and m.tab == "mixed"
    t.settle("codex")
    assert t.tab == "mixed" and t.focus == "codex"


def test_mixed_press_leaves_to_first_participant():
    t = TabState(PARTS, tab="mixed", focus="codex")
    m = t.press("codex")
    assert m.kind == "flip" and m.target == "claude" and m.tab == "harness"
    t.settle("claude")
    assert t.tab == "harness"
    assert t.focus == "codex"   # sticky across visits


def test_mixed_press_bar_only_when_focus_is_first():
    t = TabState(PARTS, tab="mixed", focus="claude")
    m = t.press("claude")
    assert m == TabMove(kind="bar", tab="harness", focus="claude")
    assert t.tab == "harness"


def test_second_press_cancels_pending_flip():
    t = TabState(PARTS)
    t.press("claude")
    m = t.press("claude")
    assert m.kind == "cancel"
    assert t.pending_target() == ""
    assert t.tab == "harness"


def test_routed_sets_pending_and_settle_moves_focus():
    t = TabState(PARTS, tab="mixed", focus="claude")
    t.routed("codex")
    assert t.pending_target() == "codex"
    t.settle("codex")
    assert t.tab == "mixed" and t.focus == "codex"


def test_cancelled_clears_pending():
    t = TabState(PARTS, tab="mixed", focus="claude")
    t.routed("codex")
    t.cancelled()
    assert t.pending_target() == ""


def test_version_bumps_on_visible_changes():
    t = TabState(PARTS)
    v0 = t.version
    t.press("opencode")           # bar move into mixed
    assert t.version > v0


def test_snapshot():
    t = TabState(PARTS, tab="mixed", focus="codex")
    assert t.snapshot("codex") == {
        "tab": "mixed", "focus": "codex", "routing_ok": True}
    assert t.snapshot("codex", routing_ok=False)["routing_ok"] is False
    t2 = TabState(PARTS)
    assert t2.snapshot("claude") == {
        "tab": "harness", "focus": "", "routing_ok": True}


def test_two_participant_cycle():
    t = TabState(["claude", "codex"])
    assert t.press("claude").target == "codex"
    t.settle("codex")
    m = t.press("codex")
    assert m.kind == "bar" and m.tab == "mixed"
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_tabs.py -q`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write the implementation**

```python
"""src/tandem/tabs.py

The tab cycle: participants in order, then the mixed tab, then around.

The mixed tab is a view + routing mode over the same harness processes, so
a press has two possible costs and this module's whole job is telling them
apart: a *bar* move changes only tandem's own state (tab flag, frame file,
bar repaint — instant, the harness keeps running), while a *flip* move
means the shown harness really changes (terminate + relaunch through the
existing flip machinery). Entering the mixed tab from the harness whose
focus it already holds is a bar move; every other transition that changes
the shown harness is a flip.

Threading: press() runs on the pty pump thread inside the stdin branch —
it must stay allocation-light, lock-free and I/O-free (the
FlipMonitor.flip_pressed contract). routed() runs on the mixer thread;
pending_target()/settle()/cancelled() run on the flip loop between runs.
All state is plain attributes: the GIL makes each assignment atomic, and
no reader needs a consistent multi-field view (the pump only paints
snapshots, and a paint one tick stale is invisible).

Cancel semantics are preserved from the pre-mixed frame: a press while a
flip is pending cancels it (returns a "cancel" move; the caller toggles
the monitor off). Multi-tab skipping is deliberately not a thing.
"""

from __future__ import annotations

from dataclasses import dataclass

MIXED = "mixed"


@dataclass(frozen=True)
class TabMove:
    kind: str            # "bar" | "flip" | "cancel"
    tab: str             # destination tab ("harness" | "mixed")
    focus: str           # mixed focus after the move
    target: str = ""     # harness to launch (kind == "flip" only)


class TabState:
    def __init__(self, participants: list[str], tab: str = "harness",
                 focus: str = ""):
        self.participants = list(participants)
        self.tab = tab if tab in ("harness", MIXED) else "harness"
        self.focus = focus if focus in self.participants else ""
        self.pending: TabMove | None = None
        self.version = 0

    def press(self, active: str) -> TabMove:
        if self.pending is not None:
            move = TabMove("cancel", self.tab, self.focus)
            self.pending = None
            self.version += 1
            return move
        if self.tab == MIXED:
            first = self.participants[0]
            if self.focus == first:
                self.tab = "harness"
                self.version += 1
                return TabMove("bar", "harness", self.focus)
            move = TabMove("flip", "harness", self.focus, target=first)
        else:
            try:
                idx = self.participants.index(active)
            except ValueError:
                idx = 0
            if idx < len(self.participants) - 1:
                move = TabMove("flip", "harness", self.focus,
                               target=self.participants[idx + 1])
            else:
                focus = self.focus or active
                if focus == active:
                    self.tab, self.focus = MIXED, focus
                    self.version += 1
                    return TabMove("bar", MIXED, focus)
                move = TabMove("flip", MIXED, focus, target=focus)
        self.pending = move
        self.version += 1
        return move

    def routed(self, target: str) -> bool:
        if self.pending is not None:
            return False   # a user press already owns the pending slot; the
                           # caller retries next tick — never overwrite, never
                           # double-toggle the monitor
        self.pending = TabMove("flip", self.tab, target, target=target)
        self.version += 1
        return True

    def pending_target(self) -> str:
        p = self.pending
        return p.target if p is not None else ""

    def settle(self, new_active: str) -> None:
        p, self.pending = self.pending, None
        if p is not None:
            self.tab = p.tab
        if self.tab == MIXED:
            self.focus = new_active
        self.version += 1

    def cancelled(self) -> None:
        if self.pending is not None:
            self.pending = None
            self.version += 1

    def snapshot(self, active: str, routing_ok: bool = True) -> dict:
        return {
            "tab": self.tab,
            "focus": active if self.tab == MIXED else "",
            "routing_ok": routing_ok,
        }
```

- [ ] **Step 4: Run to verify pass**: `uv run pytest tests/test_tabs.py -q`

- [ ] **Step 5: Full suite, then commit**

```bash
git add src/tandem/tabs.py tests/test_tabs.py
git commit -m "feat: tab state machine for the mixed tab cycle"
```

---

### Task 7: status bar mixed-slot rendering

**Files:**
- Modify: `src/tandem/frame.py` (`StatusBar.line`, `StatusBar.paint`)
- Modify: `src/tandem/ptyrun.py` (`FrameIO` field, `paint()`, pump repaint tick)
- Test: `tests/test_frame.py` (append), `tests/test_ptyrun.py` (append if it exercises paint; otherwise frame-level tests suffice)

**Interfaces:**
- `StatusBar.line(armed, usage="", limits=None, mode=None)` and `StatusBar.paint(armed, usage="", limits=None, mode=None)` — `mode` is `TabState.snapshot()`'s dict or None (None = today's rendering, so every existing caller/test is untouched).
- `FrameIO.mode: Callable[[], dict] | None = None` — read on every paint; the pump repaints when the returned dict changes (equality compare, like `limits`).

Rendering rules (mode is not None):
- `mode["tab"] == "harness"`: today's line plus a trailing idle mixed slot: `… │ mixed ○   ^] flips`.
- `mode["tab"] == "mixed"`: every harness slot renders `○` with no stats, then `mixed ● <focus>` where `<focus>` carries the active slot's stats/limits (they describe the focus harness — it IS the active one), e.g. ` claude ○ │ codex ○ │ opencode ○ │ mixed ● codex · 12% ctx · 5h 4%   ^] flips`.
- `mode["routing_ok"] is False` in the mixed tab appends ` (no @-routing)` to the mixed slot annotation — the opencode-focus hint from the spec.
- Elision tiers: extend the existing 4-tier list so the mixed annotation's stats elide first, then limits, then the ` (no @-routing)` hint is NEVER elided before the focus name (the hint prevents silent @-swallowing, which outranks cosmetics); the final tier keeps `mixed ● <focus>`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_frame.py`; follow the file's existing StatusBar test style)

```python
def test_bar_harness_tab_shows_idle_mixed_slot():
    bar = StatusBar(30, 120, "claude", ["codex"])
    line = bar.line(False, mode={"tab": "harness", "focus": "",
                                 "routing_ok": True})
    assert "claude ●" in line and "mixed ○" in line


def test_bar_mixed_tab_marks_mixed_active_with_focus():
    bar = StatusBar(30, 120, "codex", ["claude", "opencode"])
    line = bar.line(False, usage="12% ctx",
                    mode={"tab": "mixed", "focus": "codex",
                          "routing_ok": True})
    assert "mixed ● codex" in line and "12% ctx" in line
    assert "codex ○" in line and "codex ●" not in line.replace(
        "mixed ● codex", "")


def test_bar_mixed_tab_no_routing_hint():
    bar = StatusBar(30, 120, "opencode", ["claude", "codex"])
    line = bar.line(False, mode={"tab": "mixed", "focus": "opencode",
                                 "routing_ok": False})
    assert "mixed ● opencode (no @-routing)" in line


def test_bar_without_mode_is_unchanged():
    bar = StatusBar(30, 120, "claude", ["codex"])
    assert "mixed" not in bar.line(False)


def test_bar_mixed_narrow_elides_stats_keeps_focus():
    bar = StatusBar(30, 46, "codex", ["claude", "opencode"])
    line = bar.line(False, usage="12% ctx · 7.6M↑ 312k↓",
                    mode={"tab": "mixed", "focus": "codex",
                          "routing_ok": True})
    assert "mixed ● codex" in line and "312k" not in line
```

- [ ] **Step 2: Run to verify failure**: `uv run pytest tests/test_frame.py -q` — new tests FAIL (`TypeError: line() got an unexpected keyword argument 'mode'`).

- [ ] **Step 3: Implement**

In `StatusBar.line`, accept `mode: dict | None = None`. Restructure `compose` so the slot list is built from a plan:

```python
        mixed_mode = bool(mode) and mode.get("tab") == MIXED_TAB
        show_mixed = mode is not None

        def compose(stats: list[str], with_limits: bool) -> str:
            def slot(name: str, glyph: str, extra: list[str]) -> str:
                lim = limits.get(name, "") if with_limits else ""
                bits = [*extra] + ([lim] if lim else [])
                return f"{name} {glyph}" + (f" {' · '.join(bits)}" if bits else "")
            if mixed_mode:
                # active-first, same as the harness-tab rendering: the bar's
                # slot order has always been per-run construction order, and
                # inventing a different order for one tab would make slots
                # jump when entering/leaving mixed
                slots = [slot(n, "○", []) for n in (self.active, *self.others)]
                focus = mode.get("focus") or self.active
                hint = "" if mode.get("routing_ok", True) else " (no @-routing)"
                mixed_bits = [focus + hint, *stats]
                lim = limits.get(self.active, "") if with_limits else ""
                if lim:
                    mixed_bits.append(lim)
                slots.append("mixed ● " + " · ".join(mixed_bits))
            else:
                slots = [slot(self.active, "●", stats)]
                slots += [slot(o, "○", []) for o in self.others]
                if show_mixed:
                    slots.append("mixed ○")
            return f" {' │ '.join(slots)}   {self.key_label} flips"
```

Define `MIXED_TAB = "mixed"` at frame.py module level (frame.py must not import tabs.py — frame stays a pure bytes/strings module with no tandem imports, per its docstring).

Keep the existing 4 elision tiers; they already shrink `stats`/`limits`, which now flow into the mixed slot. The hint rides the focus string so no tier can drop it separately.

`StatusBar.paint` passes `mode` through to `line`.

In `ptyrun.py`:
- `FrameIO` gains, after `limits`:

```python
    # the tab-state snapshot for the bar's mixed slot (None = pre-mixed
    # rendering); published by TabState via plain attribute reads, so the
    # callable is cheap and safe on the pump thread
    mode: Callable[[], dict] | None = None
```

- In `run_in_pty`'s `paint()` closure, read `mode_now = frame.mode() if frame.mode is not None else None` and pass `mode=mode_now` into `b.paint(...)`.
- In the pump loop, mirror the `limits` change-detection block:

```python
            if bar is not None and frame.mode is not None:
                mode_now = frame.mode()
                if mode_now != last_mode:
                    last_mode = mode_now
                    paint()
```
seeding `last_mode = frame.mode() if bar is not None and frame.mode is not None else None` next to `last_limits`.

- [ ] **Step 4: Run to verify pass**: `uv run pytest tests/test_frame.py tests/test_ptyrun.py -q`

- [ ] **Step 5: Full suite, then commit**

```bash
git add src/tandem/frame.py src/tandem/ptyrun.py tests/test_frame.py
git commit -m "feat: status bar mixed tab rendering"
```

---

### Task 8: `PtyControl.write`

**Files:**
- Modify: `src/tandem/ptyrun.py` (`PtyControl`)
- Test: `tests/test_ptyrun.py` (append)

**Interfaces:**
- Produces: `PtyControl.write(data: bytes) -> bool` — best-effort write to the attached child from any thread; False when there is no live attached child or the write raised.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_ptyrun.py`, following its existing fake-child style — it already has fakes for `terminate`; reuse them)

```python
def test_control_write_delivers_to_attached_child():
    class Child:
        def __init__(self):
            self.written = b""
        def isalive(self):
            return True
        def write(self, data):
            self.written += data
    control = PtyControl()
    child = Child()
    control.attach(child)
    assert control.write(b"hello") is True
    assert child.written == b"hello"


def test_control_write_without_child_is_false():
    assert PtyControl().write(b"x") is False


def test_control_write_dead_child_is_false():
    class Dead:
        def isalive(self):
            return False
    control = PtyControl()
    control.attach(Dead())
    assert control.write(b"x") is False


def test_control_write_swallow_raise():
    class Bad:
        def isalive(self):
            return True
        def write(self, data):
            raise OSError("gone")
    control = PtyControl()
    control.attach(Bad())
    assert control.write(b"x") is False
```

- [ ] **Step 2: Run to verify failure**: `uv run pytest tests/test_ptyrun.py -q` — FAIL (`AttributeError: write`).

- [ ] **Step 3: Implement** (on `PtyControl`, after `attach`)

```python
    def write(self, data: bytes) -> bool:
        """Best-effort write to the attached child from any thread — the
        injector's road into a routed target. No attach wait: a caller with
        nothing attached yet has its own readiness gate, and blocking here
        would put an arbitrary stall on that thread. False for every
        failure; the caller owns retry/report."""
        child = self._child
        if child is None or not _is_alive(child):
            return False
        try:
            child.write(data)
        except Exception:
            return False
        return True
```

- [ ] **Step 4: Run to verify pass**, **Step 5: Full suite + commit**

```bash
git add src/tandem/ptyrun.py tests/test_ptyrun.py
git commit -m "feat: cross-thread pty write on PtyControl"
```

---

### Task 9: `tandem hook-prompt` + plugin hook registration

**Files:**
- Modify: `src/tandem/cli.py` (new command after `hook_route_cmd`)
- Modify: `plugin/hooks/hooks.json`
- Test: `tests/test_cli.py` (append; follow the existing `hook-route` CliRunner tests' fixture pattern for TANDEM_HOME + a paired session), `tests/test_plugin.py` (hooks.json shape assertions, mirroring its existing checks)

**Interfaces:**
- Consumes: `promptroute.route_prompt` (Task 1), `routefile.read_frame_state` / `write_route` / `RouteRequest` (Task 2).
- Produces: CLI command `tandem hook-prompt`: reads UserPromptSubmit JSON on stdin (`{"session_id", "cwd", "prompt", ...}`), prints either nothing (allow) or `{"decision": "block", "reason": "tandem: <reason> — running there"}` and writes the route request. ALWAYS exits 0.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_cli.py`)

```python
def _hook_prompt(runner, payload):
    return runner.invoke(main, ["hook-prompt"], input=json.dumps(payload))


def test_hook_prompt_silent_without_session(tmp_path, monkeypatch, runner):
    monkeypatch.setenv("TANDEM_HOME", str(tmp_path))
    res = _hook_prompt(runner, {"cwd": str(tmp_path), "prompt": "@codex hi"})
    assert res.exit_code == 0 and res.output.strip() == ""


def test_hook_prompt_silent_outside_mixed_tab(paired_cli_session, runner):
    # paired_cli_session: the fixture pattern existing hook-route tests use —
    # a StateStore with one session for the test cwd under a tmp TANDEM_HOME.
    session, cwd = paired_cli_session
    res = _hook_prompt(runner, {"cwd": cwd, "prompt": "@codex hi"})
    assert res.exit_code == 0 and res.output.strip() == ""


def test_hook_prompt_blocks_and_stashes_in_mixed_tab(paired_cli_session, runner):
    from tandem import routefile
    session, cwd = paired_cli_session
    routefile.write_frame_state(session.tandem_id,
                                {"tab": "mixed", "focus": "claude",
                                 "routing_ok": True})
    res = _hook_prompt(runner, {"cwd": cwd, "prompt": "@codex fix the test"})
    assert res.exit_code == 0
    decision = json.loads(res.output)
    assert decision["decision"] == "block"
    assert "codex" in decision["reason"]
    req = routefile.read_route(session.tandem_id)
    assert req is not None and req.target == "codex"
    assert req.prompt == "fix the test" and req.state == "pending"
    assert req.source == "claude"


def test_hook_prompt_stay_on_focus_is_silent(paired_cli_session, runner):
    from tandem import routefile
    session, cwd = paired_cli_session
    routefile.write_frame_state(session.tandem_id,
                                {"tab": "mixed", "focus": "claude",
                                 "routing_ok": True})
    res = _hook_prompt(runner, {"cwd": cwd, "prompt": "@claude hi"})
    assert res.exit_code == 0 and res.output.strip() == ""
    assert routefile.read_route(session.tandem_id) is None


def test_hook_prompt_garbage_stdin_exits_zero(runner):
    res = runner.invoke(main, ["hook-prompt"], input="{not json")
    assert res.exit_code == 0
```

(Read the existing hook-route tests in `tests/test_cli.py` first and reuse their exact fixture names; the assertions above are the contract, the fixture plumbing is theirs.)

- [ ] **Step 2: Run to verify failure**: `uv run pytest tests/test_cli.py -q` — new tests FAIL (`No such command 'hook-prompt'`).

- [ ] **Step 3: Implement**

In `cli.py`, after `hook_route_cmd`:

```python
@main.command(name="hook-prompt")
def hook_prompt_cmd() -> None:
    """UserPromptSubmit hook: @-route mixed-tab prompts to another harness.

    Reads hook JSON on stdin; prints a block decision or nothing. ALWAYS
    exits 0 — like hook-route, every failure must degrade to the native
    turn, and the plugin registers it as `tandem hook-prompt || true` so
    click's own exit-2 usage path (version skew) cannot block a prompt.

    Ordering is the unlosable-prompt invariant (spec: Dispatch pipeline):
    the route request is durably stashed BEFORE the block decision prints.
    A stash that fails silently allows the turn instead — a prompt that
    runs on the wrong harness beats a prompt that vanishes.

    Silent (allow) whenever: no paired session for the cwd, no frame state
    or not the mixed tab, no recognized prefix, or the target is the focus
    harness itself. The frame file's focus field names the harness the user
    is typing in; `session.active` may lag it by a beat mid-flip, so the
    frame file is the authority here.

    The hook also has to prove the prompt was typed in THIS session: the
    plugin is installed user-wide, so a second claude window opened in the
    same directory runs this same hook against the same paired session — and
    routing there would block that window's prompt only to inject it into the
    tandem session's harness, in a terminal the user is not looking at. So
    routing requires the payload's `session_id` to be exactly the focus
    harness's native id. Every doubt fails OPEN to the native turn: no
    session_id in the payload, a non-string one, no native id recorded for
    the focus harness yet, or any mismatch — a prompt that runs natively in
    the window it was typed in is always recoverable; one that vanishes into
    another window is not."""
    try:
        from . import promptroute, routefile

        payload = json.loads(sys.stdin.read() or "{}")
        prompt = payload.get("prompt")
        cwd = payload.get("cwd") or _cwd()
        if not isinstance(prompt, str) or not prompt.strip():
            sys.exit(0)
        with StateStore() as store:
            session = store.latest_session_for_cwd(cwd)
        if session is None:
            sys.exit(0)
        frame = routefile.read_frame_state(session.tandem_id)
        if not frame or frame.get("tab") != "mixed":
            sys.exit(0)
        focus = frame.get("focus") or session.active
        native = session.native_id(focus)
        sid = payload.get("session_id")
        if not native or not isinstance(sid, str) or sid != native:
            sys.exit(0)   # another window, or an unidentifiable one
        got = promptroute.route_prompt(prompt, focus, session.participants)
        if got is None:
            sys.exit(0)
        decision, body = got
        routefile.write_route(session.tandem_id, routefile.RouteRequest(
            target=decision.harness, model=decision.model, prompt=body,
            source=focus, reason=decision.reason))
        req = routefile.read_route(session.tandem_id)
        # THIS request has to be the one on disk, not merely some request: a
        # leftover from an earlier prompt is still inside the TTL, so a bare
        # existence check would let it vouch for a write that failed on a
        # disk fault — and the block would then destroy the typed prompt.
        # `state` is deliberately not compared: the frame can pick the
        # request up and flip it to "dispatched" between the write and this
        # read, and demanding "pending" would turn a landed stash into an
        # allow — the prompt would run here AND there.
        if req is None or req.prompt != body or req.target != decision.harness:
            sys.exit(0)   # stash didn't land: allow the native turn
        click.echo(json.dumps({
            "decision": "block",
            "reason": f"tandem: {decision.reason} — running there",
        }))
    except SystemExit:
        raise
    except Exception:
        pass
    sys.exit(0)
```

In `plugin/hooks/hooks.json`, add a `UserPromptSubmit` key beside `PreToolUse`:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Agent|Task",
        "hooks": [
          {"type": "command", "command": "tandem hook-route || true"}
        ]
      }
    ],
    "UserPromptSubmit": [
      {
        "hooks": [
          {"type": "command", "command": "tandem hook-prompt || true"}
        ]
      }
    ]
  }
}
```

Add to `tests/test_plugin.py` (mirroring its existing hooks.json assertions):

```python
def test_hooks_json_registers_prompt_hook():
    hooks = json.loads((PLUGIN_DIR / "hooks" / "hooks.json").read_text())
    ups = hooks["hooks"]["UserPromptSubmit"]
    assert ups[0]["hooks"][0]["command"] == "tandem hook-prompt || true"
```

- [ ] **Step 4: Run to verify pass**: `uv run pytest tests/test_cli.py tests/test_plugin.py -q`

- [ ] **Step 5: Full suite, then commit**

```bash
git add src/tandem/cli.py plugin/hooks/hooks.json tests/test_cli.py tests/test_plugin.py
git commit -m "feat: hook-prompt UserPromptSubmit command + plugin registration"
```

---

### Task 10: runner mixed-mode wiring — mixer thread, routed arm, injector

**Files:**
- Modify: `src/tandem/runner.py` (`InteractiveRunner.__init__`, `_run`)
- Test: `tests/test_runner.py` (append)

This is the largest task. Read `InteractiveRunner._run` end to end before editing; every insertion point below is named relative to existing code.

**Interfaces:**
- Consumes: `TabState` (Task 6), `routefile` (Task 2), `PtyControl.write` (Task 8), `FrameIO.mode` (Task 7), `LaunchRecipe.model` (Task 5).
- Produces:
  - `InteractiveRunner(session, sink_factory, adopt_child=None, tabs=None, inject=None)` — `tabs: TabState | None` (None = pre-mixed behavior, which is what every existing test constructs); `inject: RouteRequest | None` — a routed prompt to deliver into THIS run's harness.
  - `InteractiveRunner.route_request: RouteRequest | None` — set when this run's exit was caused by a routed flip; the flip loop consumes it.
  - `InteractiveRunner.inject_failed: bool` — the injector could not deliver (flip loop surfaces the preserved prompt).

**Behavior to implement, point by point:**

1. **Ctrl-] goes through TabState.** In `_run`, where `FrameIO` is built, when `self.tabs is not None` replace `on_flip=monitor.flip_pressed` with a glue closure and wire `mode`:

```python
        tabs = self.tabs

        def on_flip() -> None:
            # pump thread: TabState.press is allocation-light and I/O-free
            # by contract; bar moves repaint via mode() on the next tick and
            # the mixer thread persists the frame file
            move = tabs.press(active)
            if move.kind == "bar":
                return
            monitor.flip_pressed()   # arms a flip, or toggles off a pending
                                     # one (move.kind == "cancel")

        frame = FrameIO(
            ...,
            on_flip=on_flip if tabs is not None else monitor.flip_pressed,
            mode=(lambda: tabs.snapshot(active, routing_ok=routing_ok))
                if tabs is not None else None,
            ...
        )
```

   where `routing_ok = adapter.prompt_hook_capable` is computed once before
   the FrameIO construction (Task 12 tightens it with the plugin-installed
   check — the spec's hook-registration trap). Use the same `routing_ok`
   local in the mixer thread's `write_frame_state` call instead of
   re-deriving it there.

   The monitor's `wait_until_safe` cancel path must also clear TabState's pending move: pass `on_cancelled=tabs.cancelled` — implement by extending the `cancelled=lambda: ...` check no further; instead, in `FlipMonitor._run`, after `if not ok:` add a callback hook `self.on_wait_cancelled` (a plain attribute like `on_flip_decided`, default None, called inside try/except-pass). The runner assigns `monitor.on_wait_cancelled = tabs.cancelled` when tabs is not None.

2. **Mixer thread.** Started right after `monitor.start()` when `tabs is not None`, stopped in the `finally` (set its own stop event before `monitor.stop()`, then `join(timeout=2)` after it — a tick still in flight would otherwise arm state on a run that has already exited; `mixer_stop.wait` returns the moment the event is set, so the join costs only the in-flight tick), daemon, name `tandem-mixer`:

```python
        def mixer_thread() -> None:
            last_version = -1
            # A leftover route from a crashed run must not replay a stale
            # prompt: pending older than a fresh boot window is cleared; a
            # dispatched leftover means a routed prompt never landed and is
            # surfaced through notes at exit.
            left = routefile.read_route(session.tandem_id)
            if left is not None and self.inject is None:
                if left.state == "dispatched":
                    notes.append(
                        "a routed prompt was never delivered and was kept: "
                        f"{left.prompt[:60]!r} (target {left.target})")
                routefile.clear_route(session.tandem_id)
            while not mixer_stop.is_set():
                if tabs.version != last_version:
                    last_version = tabs.version
                    routefile.write_frame_state(
                        session.tandem_id,
                        tabs.snapshot(active,
                                      routing_ok=adapter.prompt_hook_capable))
                if (tabs.tab == "mixed" and self.route_request is None
                        and not monitor.armed() and not monitor.flip_requested):
                    req = routefile.read_route(session.tandem_id)
                    if req is not None and req.state == "pending":
                        if req.target == active or req.target not in \
                                session.participants:
                            routefile.clear_route(session.tandem_id)
                        elif tabs.routed(req.target):
                            # the claim gates everything below it: a False
                            # claim means a user press owns the pending slot
                            # (recorded before the pump arms the monitor, so
                            # the guards above can't see it), and the route
                            # request stays pending for the next tick rather
                            # than retargeting the press and double-toggling
                            # the monitor
                            routefile.mark_dispatched(session.tandem_id, req)
                            self.route_request = req
                            monitor.flip_pressed()
                mixer_stop.wait(0.25)
```

   `mixer_stop = threading.Event()` defined beside `stop`; `notes` already exists. `self.route_request = None` initialized in `__init__` (alongside `warm_child`), plus `self.tabs = tabs`, `self.inject = inject`, `self.inject_failed = False`.

3. **Warm-fire suppression.** At the top of `fire_warm()` add:

```python
            if self.route_request is not None:
                return   # routed flips spawn cold in v1: the standby would
                         # be for the wrong side or the wrong model
```

   (Closure capture: `fire_warm` is defined inside `_run`, which is a method — `self` is already in scope.)

4. **Cancel of a routed arm.** `monitor.on_wait_cancelled` (see point 1) must also undo a routed arm: when tabs is present assign a closure that calls `tabs.cancelled()` and, if `self.route_request is not None`, clears it and the route file and appends a note `"routed turn cancelled — the prompt was discarded: <first 60 chars>"`. (The monitor thread may do file I/O — it already stats files in `wait_until_safe`.)

5. **Injector thread.** Started (daemon, name `tandem-inject`) just before `monitor.start()` when `self.inject is not None`:

```python
        def injector_thread() -> None:
            req = self.inject
            if req.target != active:
                self.inject_failed = True   # ladder landed elsewhere; keep
                return                      # the file for the user to see
            deadline = time.time() + 30
            ready = False
            while time.time() < deadline and not stop.is_set():
                # claude BY NAME, not by `hasattr(adapter, "session_status")`:
                # opencode has that method and it answers off the transcript
                # sqlite (unknown sid → "waiting"; a resumed session's last
                # row → "waiting"), which says nothing about whether the TUI
                # has drawn and can take a paste. Believing it either writes
                # before the child is attached (every routed opencode turn
                # fails) or pastes into a TUI that is not listening and then
                # clears the route file, destroying the prompt. The hasattr
                # idiom stays correct for the FLIP gate in `_run`, where a
                # stale "waiting" only flips a beat early.
                if active == "claude" and active_sid:
                    try:
                        if adapter.session_status(active_sid) == "waiting":
                            ready = True
                            break
                    except Exception:
                        pass
                    time.sleep(0.3)
                else:
                    # no usable readiness signal (codex/opencode): fixed
                    # settle delay from spawn — verified in the live gate
                    time.sleep(2.5)
                    ready = True
                    break
            if not ready or stop.is_set():
                self.inject_failed = True
                return
            body = req.prompt.encode()
            # bracketed paste: multi-line prompts must not submit per line
            ok = control.write(b"\x1b[200~" + body + b"\x1b[201~")
            if ok:
                time.sleep(0.15)   # let the composer ingest the paste
                ok = control.write(b"\r")
            if ok:
                routefile.clear_route(session.tandem_id)
            else:
                self.inject_failed = True
```

   In the `finally`, after reports are assembled, when `self.inject_failed` append to `notes`:
   `f"routed prompt was not delivered — it is preserved; re-type it in {self.inject.target} ({self.inject.prompt[:60]!r})"`.

6. **Imports.** `from . import routefile` at module top of runner.py (it already imports siblings).

- [ ] **Step 1: Write the failing tests** (append to `tests/test_runner.py`; these test the pieces without a real pty, following that file's existing style of driving `_run` collaborators directly)

```python
from tandem.routefile import RouteRequest
from tandem.tabs import TabState


def _mk_session(tmp_path, monkeypatch):
    # mirrors tests/test_flip.py's `sess` fixture; reuse test_runner.py's
    # own session helper if it already has one
    monkeypatch.setenv("TANDEM_HOME", str(tmp_path / ".tandem"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / ".claude"))
    with StateStore() as store:
        return store.create_session(str(tmp_path / "proj"), "claude",
                                    ["claude", "codex"],
                                    {"claude": "c-id", "codex": "x-id"})


class StubMonitor:
    def __init__(self):
        self.pressed = 0
        self.flip_requested = False
    def armed(self):
        return False
    def flip_pressed(self):
        self.pressed += 1


def test_runner_defaults_have_no_route_state(tmp_path, monkeypatch):
    sess = _mk_session(tmp_path, monkeypatch)
    r = InteractiveRunner(sess, sink_factory=None)
    assert r.route_request is None and r.tabs is None
    assert r.inject is None and r.inject_failed is False


def test_mixer_pickup_arms_and_marks_dispatched(tmp_path, monkeypatch):
    from tandem import routefile
    sess = _mk_session(tmp_path, monkeypatch)
    req = RouteRequest("codex", "", "do it", "claude", "→ codex")
    routefile.write_route(sess.tandem_id, req)
    tabs = TabState(sess.participants, tab="mixed", focus="claude")
    r = InteractiveRunner(sess, sink_factory=None, tabs=tabs)
    monitor = StubMonitor()
    r._pickup_route("claude", monitor)
    assert r.route_request is not None and r.route_request.target == "codex"
    assert routefile.read_route(sess.tandem_id).state == "dispatched"
    assert monitor.pressed == 1
    assert tabs.pending_target() == "codex"


def test_mixer_pickup_ignores_route_to_current_harness(tmp_path, monkeypatch):
    from tandem import routefile
    sess = _mk_session(tmp_path, monkeypatch)
    routefile.write_route(sess.tandem_id, RouteRequest(
        "claude", "", "do it", "claude", "→ claude"))
    tabs = TabState(sess.participants, tab="mixed", focus="claude")
    r = InteractiveRunner(sess, sink_factory=None, tabs=tabs)
    monitor = StubMonitor()
    r._pickup_route("claude", monitor)
    assert r.route_request is None and monitor.pressed == 0
    assert routefile.read_route(sess.tandem_id) is None   # cleared


def test_deliver_inject_pastes_and_clears(tmp_path, monkeypatch):
    from tandem import routefile
    from tandem.ptyrun import PtyControl
    sess = _mk_session(tmp_path, monkeypatch)
    req = RouteRequest("codex", "", "do it", "claude", "→ codex",
                       state="dispatched")
    routefile.write_route(sess.tandem_id, req)

    class Child:
        def __init__(self):
            self.written = b""
        def isalive(self):
            return True
        def write(self, data):
            self.written += data

    class StubAdapter:      # no session_status attr: fixed-delay path
        pass

    control, child = PtyControl(), Child()
    control.attach(child)
    r = InteractiveRunner(sess, sink_factory=None, inject=req)
    monkeypatch.setattr("time.sleep", lambda s: None)   # skip settle delays
    r._deliver_inject("codex", "x-id", StubAdapter(), control,
                      threading.Event())
    assert child.written == b"\x1b[200~do it\x1b[201~\r"
    assert r.inject_failed is False
    assert routefile.read_route(sess.tandem_id) is None


def test_deliver_inject_wrong_target_keeps_file(tmp_path, monkeypatch):
    from tandem import routefile
    from tandem.ptyrun import PtyControl
    sess = _mk_session(tmp_path, monkeypatch)
    req = RouteRequest("codex", "", "do it", "claude", "→ codex",
                       state="dispatched")
    routefile.write_route(sess.tandem_id, req)
    r = InteractiveRunner(sess, sink_factory=None, inject=req)
    r._deliver_inject("claude", "c-id", object(), PtyControl(),
                      threading.Event())
    assert r.inject_failed is True
    assert routefile.read_route(sess.tandem_id) is not None
```

(`_pickup_route` and `_deliver_inject` signatures per the extraction note
below; `threading` and `StateStore` imports at the top of the test file.)

Extraction contract that makes this testable WITHOUT a pty: the mixer loop's route-pickup branch becomes `InteractiveRunner._pickup_route(active: str, monitor) -> None` (behavior point 2's inner `if` block verbatim — reads the route file, validates the target, claims the pending slot with `tabs.routed` and only on a successful claim marks the file dispatched, sets `self.route_request` and arms `monitor.flip_pressed`), and the injector's readiness+write step becomes `InteractiveRunner._deliver_inject(active: str, active_sid, adapter, control, stop) -> None` (behavior point 5's body from the target check onward, using `self.session.tandem_id` for the route-file calls). The thread bodies are then one-liners around these methods.

- [ ] **Step 2: Run to verify failure**: `uv run pytest tests/test_runner.py -q`

- [ ] **Step 3: Implement** per the six behavior points, with `_pickup_route` and `_deliver_inject` as extracted methods so the thread bodies are one-liners around them.

- [ ] **Step 4: Run to verify pass**: `uv run pytest tests/test_runner.py -q`

- [ ] **Step 5: Full suite, then commit**

```bash
git add src/tandem/runner.py tests/test_runner.py
git commit -m "feat: mixed-mode runner wiring (mixer, routed arm, injector)"
```

---

### Task 11: flip loop — tab cycle, routed switch, inject carry

**Files:**
- Modify: `src/tandem/flip.py` (`run_session`, `_flip_loop`, `_switch`, `_standby_fresh`)
- Test: `tests/test_flip.py` (append)

**Interfaces:**
- Consumes: `TabState` (Task 6), `StateStore.get_meta`/`set_meta` (Task 3), `build_launch(model=...)` + `LaunchRecipe.model` (Task 5), `InteractiveRunner(tabs=, inject=)` + `.route_request` (Task 10), `load_frame_config().mixed` (Task 4).
- Produces: `_switch(tandem_id, run_harness, code, visited=None, carry=None, to="", route=None)` — `to` overrides the cycle target; `route: RouteRequest | None` rides `carry["route"]` into the next run's `inject`.

**Behavior:**

1. **`run_session`** builds the TabState once and threads it through:

```python
    from .config import load_frame_config
    from .tabs import TabState

    with StateStore() as store:
        session = store.get_session(tandem_id)
        meta = store.get_meta(tandem_id) if session else {}
    tabs = (TabState(session.participants,
                     tab=meta.get("tab", "harness"),
                     focus=meta.get("mixed_focus", ""))
            if session is not None and load_frame_config().mixed else None)
```

   The default `run_harness` closure passes `tabs=tabs` and `inject=carry.pop("route", None)` into `InteractiveRunner`, and after the run stores `carry["route"] = r.route_request`. Injected test runners never see tabs (they replace `run_harness` wholesale) — existing tests unchanged.

   Amended: when the session exists but `tabs is None` (`[frame] mixed = false`), overwrite the frame file once at startup with `routefile.write_frame_state(tandem_id, {"tab": "harness", "focus": "", "routing_ok": False})`. The hook reads that file, not the config, so a leftover `tab: "mixed"` from a run that had the tab on would keep it stashing prompts for a mixer that no longer runs. With `tabs` live the runner's mixer owns the file and the flip loop must not write it.

2. **Meta persistence**: in the default `run_harness`, before constructing the runner:

```python
            if tabs is not None:
                with StateStore() as s:
                    s.set_meta(session.tandem_id,
                               {"tab": tabs.tab, "mixed_focus": tabs.focus})
```

3. **`_flip_loop`** passes `tabs` down (new parameter, default None) and `_switch` consumes it:

```python
    while flip:
        _clear_screen()
        ...
        route = carry.pop("route", None) if carry else None
        to = tabs.pending_target() if tabs is not None else ""
        to = to or (route.target if route is not None else "")
        code, flip = _switch(tandem_id, run_harness, code, carry=carry,
                             to=to, route=route, tabs=tabs)
```

   The pending slot wins, not the route (amended): `TabState.routed` is a
   *claim*, so in the normal routed flow the mixer already owns the slot and
   the two agree. They differ only when a stale or stranded `route_request`
   coexists with a fresh user press — and then the press must win. The route
   still rides the carry into the next run, where `_deliver_inject`'s target
   check turns it into `inject_failed` + the preserved-prompt note instead of
   a prompt typed into a harness the user chose for something else.

4. **`_switch`** grows `to: str = ""`, `route=None`, `tabs=None`:
   - Target selection: `target = to or session.next_active(old)`; the visited-ladder loop is unchanged (it advances past refusals; a refused routed target falls back through the cycle exactly like a refused Ctrl-] target — the injector's target check from Task 10 keeps the prompt from landing on the wrong harness).
   - After a successful `ops.switch_session`: `tabs.settle(new_active)` when tabs is not None, then re-persist meta (`store` is already open in that block — add `store.set_meta(...)` beside the switch).
   - Standby freshness: pass the route's model through — extend `_standby_fresh(standby, new_active, session, mem, route_model="")` with:

```python
    if route_model and standby.recipe.model != route_model:
        return False   # warmed without the pinned model: wrong launch
```

   and at the call site `route_model=route.model if route is not None else ""`.
   - Ladder recursion threads `tabs` (but NOT `to`/`route` — a refused
     target must not be re-targeted, and the route rides `carry`): the
     recursive call becomes `_switch(tandem_id, run_harness, code,
     visited=visited, carry=carry, tabs=tabs)`.
   - Hand the route to the next run: `carry["route"] = route` just before `_try_enter` (the default `run_harness` pops it as `inject`).
   - Model-pinned launch: the *runner* builds the launch, so the model must reach `build_launch`. Thread it via the carry too: in the default `run_harness`, `inject = carry.pop("route", None)`, and when `inject is not None and inject.model` construct the runner with a model-pinned recipe. Concretely, `InteractiveRunner` (Task 10) already receives `inject`; in `_run`, change the recipe line to:

```python
        recipe = (self.adopt_child.recipe if adopting
                  else build_launch(session, active,
                                    model=(self.inject.model
                                           if self.inject is not None and
                                           self.inject.target == active
                                           else "")))
```

   (This line lands in Task 10's file but belongs to this task's behavior; implement it here, where the flip-side contract is being wired, and extend Task 10's construction accordingly.)
   - Degrade notice: when `self.inject.model` is set but the recipe came back with `recipe.model == ""` (adapter returned no argv), append to `notes`: `f"{active} cannot pin a model at launch — running its default"`.

5. **Ladder-failure note**: if `_switch` exhausts the ladder while `route is not None`, the routed prompt survives in the route file (`dispatched`); the next mixer startup surfaces it (Task 10 point 2). No extra code — verify with a test.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_flip.py`, using its existing injected-`run_harness` style)

`test_flip.py` already has the pieces these tests need: the `sess` fixture (a 2-participant in-store session under tmp homes — extend a copy to 3 participants where a test needs opencode), `fake_runner(log, codes)` (records `session.active` per run), and `_flipping_switch(monkeypatch)` (patches `ops.switch_session` to a fake that honors `to=`). Reuse them — do not invent new fixtures.

```python
from tandem.routefile import RouteRequest
from tandem.tabs import TabState


def test_switch_honors_target_override(sess3, monkeypatch):
    """_switch(to='opencode') lands on opencode even though next_active
    from claude is codex. `sess3` = the `sess` fixture widened to
    ["claude", "codex", "opencode"]."""
    _flipping_switch(monkeypatch)
    log = []
    code, flip = flip._switch(sess3.tandem_id, fake_runner(log), 0,
                              to="opencode")
    assert log == ["opencode"]


def test_switch_settles_tabs_and_persists_meta(sess3, monkeypatch):
    _flipping_switch(monkeypatch)
    tabs = TabState(sess3.participants, tab="mixed", focus="claude")
    tabs.routed("codex")
    flip._switch(sess3.tandem_id, fake_runner([]), 0, to="codex", tabs=tabs)
    assert tabs.tab == "mixed" and tabs.focus == "codex"
    with StateStore() as store:
        assert store.get_meta(sess3.tandem_id)["mixed_focus"] == "codex"


def test_standby_stale_when_route_model_differs(sess, monkeypatch):
    from tandem.warm import LaunchRecipe

    class Standby:
        recipe = LaunchRecipe(side="codex", argv=["codex"],
                              sentinel=paths.tandem_home() / "s",
                              hook_extra=[], transcript=None, fresh=False,
                              cwd=sess.cwd, model="")
        def alive(self):
            return True
    assert flip._standby_fresh(Standby(), "codex", sess, FakeMem(),
                               route_model="gpt-5.3-codex") is False


def test_route_carry_reaches_next_run(sess, monkeypatch):
    """_switch puts the route back into carry before _try_enter, so the
    default run_harness pops it as the next run's inject."""
    _flipping_switch(monkeypatch)
    seen = []

    def run_harness(session):
        seen.append(carry.pop("route", None))
        return 0, False

    carry = {"standby": None, "reapers": []}
    req = RouteRequest("codex", "", "do it", "claude", "→ codex")
    flip._switch(sess.tandem_id, run_harness, 0, carry=carry,
                 to="codex", route=req)
    assert seen == [req]
```

- [ ] **Step 2: Run to verify failure**, **Step 3: Implement** per the five points, **Step 4: Run to verify pass**: `uv run pytest tests/test_flip.py tests/test_runner.py -q`

- [ ] **Step 5: Full suite, then commit**

```bash
git add src/tandem/flip.py src/tandem/runner.py tests/test_flip.py
git commit -m "feat: tab-aware flip loop with routed switch and inject carry"
```

---

### Task 12: codex-side plugin registration

**Files:**
- Create: `plugin/.codex-plugin/plugin.json`
- Modify: `src/tandem/plugin_setup.py`, `src/tandem/runner.py` (routing_ok tightening), `src/tandem/doctor.py` (routing checks)
- Test: `tests/test_plugin_setup.py` (append), `tests/test_plugin.py` (manifest shape), `tests/test_memory_doctor.py` (append)

**Interfaces:**
- Produces:
  - `plugin_setup.install_plugin_codex() -> bool` — best-effort mirror of `install_plugin` shelling out to `codex plugin marketplace add` / `codex plugin install`; called from `install_plugin()` after a successful claude install when `codex` is on PATH. Failure prints a yellow note (routing *from* codex needs it; everything else works without).
  - `plugin_setup.is_plugin_installed_codex() -> bool` — parses `paths.codex_home() / "config.toml"` with tomllib and looks for any key starting `"tandem@"` in its `plugins` table. Same ambiguity rule as `is_plugin_installed`: a missing file or absent entry is definitively False, an unreadable/unparseable config is True (doubt must not nag or flash warnings).
  - `plugin_setup.hook_available(harness_id: str) -> bool` — `"claude"` → `is_plugin_installed()`, `"codex"` → `is_plugin_installed_codex()`, anything else → False. This is the spec's hook-registration-trap signal.
- Also modifies: `src/tandem/runner.py` — tighten Task 10's `routing_ok` local to `adapter.prompt_hook_capable and plugin_setup.hook_available(active)` (computed once per run; a run started before plugin install shows the `(no @-routing)` hint instead of silently eating prefixes). And `src/tandem/doctor.py` — a new check in `run_doctor`: when a session exists, report ok/warn per hook-capable participant on `hook_available` (`"mixed-tab routing: <harness> hook installed"` / warn `"mixed-tab routing: <harness> plugin not installed — @-routing from it is unavailable (tandem plugin install)"`).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_plugin.py
def test_codex_plugin_manifest_mirrors_claude():
    claude = json.loads(
        (PLUGIN_DIR / ".claude-plugin" / "plugin.json").read_text())
    codex = json.loads(
        (PLUGIN_DIR / ".codex-plugin" / "plugin.json").read_text())
    assert codex["name"] == claude["name"] == "tandem"
    assert codex["version"] == claude["version"]


# tests/test_plugin_setup.py — follow its existing subprocess-stub pattern
def test_install_plugin_codex_runs_both_commands(monkeypatch):
    calls = []
    monkeypatch.setattr(plugin_setup.shutil, "which", lambda b: "/bin/" + b)
    monkeypatch.setattr(plugin_setup, "_run",
                        lambda cmd: calls.append(cmd) or
                        subprocess.CompletedProcess(cmd, 0, "", ""))
    assert plugin_setup.install_plugin_codex() is True
    assert calls[0][:4] == ["codex", "plugin", "marketplace", "add"]
    assert calls[1][:3] == ["codex", "plugin", "install"]


def test_install_plugin_codex_missing_binary_is_false(monkeypatch):
    monkeypatch.setattr(plugin_setup.shutil, "which", lambda b: None)
    assert plugin_setup.install_plugin_codex() is False
```

- [ ] **Step 2: Run to verify failure**

- [ ] **Step 3: Implement**

`plugin/.codex-plugin/plugin.json` (dual-manifest pattern proven by vibeshub's plugins/cli — codex ≥0.145 loads claude-format plugin trees and reads `.codex-plugin/` metadata):

```json
{
  "name": "tandem",
  "description": "Mixed-tab @-routing and subagent reroute hooks for tandem sessions.",
  "version": "0.5.0",
  "author": {"name": "tandem"}
}
```

(Version must match `.claude-plugin/plugin.json`; the release checklist bumps both — add `.codex-plugin/plugin.json` to the version-bump list in `docs/` release notes if one exists.)

`plugin_setup.py`, after `install_plugin`:

```python
def install_plugin_codex() -> bool:
    """Register the same plugin tree with codex (≥0.145 loads claude-format
    plugins; the .codex-plugin manifest is its metadata). Best-effort: only
    routing *from* codex in the mixed tab needs it, so failure is a note,
    not an error."""
    if shutil.which("codex") is None:
        return False
    add = _run(["codex", "plugin", "marketplace", "add", MARKETPLACE_REPO])
    if add is not None and add.returncode != 0:
        detail = (add.stderr or add.stdout).strip()
        if detail:
            click.secho(f"  codex marketplace add failed: {detail}",
                        fg="yellow", err=True)
    ins = _run(["codex", "plugin", "install", PLUGIN_ID])
    if ins is None or ins.returncode != 0:
        click.secho(
            "  codex plugin install failed — @-routing from codex in the "
            "mixed tab will be unavailable (routing to codex still works).",
            fg="yellow", err=True)
        return False
    click.echo("Codex plugin installed (takes effect in new codex sessions).")
    return True
```

and at the end of `install_plugin()`'s success path, before `return True`: `install_plugin_codex()`.

Also in `plugin_setup.py`:

```python
def is_plugin_installed_codex() -> bool:
    """Is the tandem plugin registered with codex? Reads codex's own
    config.toml plugins table. Ambiguity rule mirrors is_plugin_installed:
    missing file / absent entry is False, unreadable is True."""
    import tomllib
    try:
        with open(paths.codex_home() / "config.toml", "rb") as f:
            cfg = tomllib.load(f)
    except FileNotFoundError:
        return False
    except (OSError, ValueError):
        return True
    plugins = cfg.get("plugins")
    if not isinstance(plugins, dict):
        return False
    return any(k.startswith("tandem@") for k in plugins)


def hook_available(harness_id: str) -> bool:
    """Can @-routing intercept prompts typed in this harness right now?
    Static capability lives on the adapter (prompt_hook_capable); this is
    the dynamic half — is the plugin actually installed there."""
    if harness_id == "claude":
        return is_plugin_installed()
    if harness_id == "codex":
        return is_plugin_installed_codex()
    return False
```

with tests (append to `tests/test_plugin_setup.py`, using its TANDEM/CODEX home env pattern):

```python
def test_is_plugin_installed_codex(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    assert plugin_setup.is_plugin_installed_codex() is False
    (tmp_path / "config.toml").write_text(
        '[plugins."tandem@tandem"]\nenabled = true\n')
    assert plugin_setup.is_plugin_installed_codex() is True
    (tmp_path / "config.toml").write_text("not [ toml")
    assert plugin_setup.is_plugin_installed_codex() is True
```

Then tighten the runner (Task 10's `routing_ok` local) to:

```python
        routing_ok = (adapter.prompt_hook_capable
                      and plugin_setup.hook_available(active))
```

(import `plugin_setup` at runner.py's top) and add the doctor check in `doctor.py`'s `run_doctor`, next to `_subagent_checks`:

```python
def _routing_checks(report: DoctorReport, session) -> None:
    if session is None:
        return
    from .harness import get_adapter
    from . import plugin_setup
    for hid in session.participants:
        if not get_adapter(hid).prompt_hook_capable:
            continue
        if plugin_setup.hook_available(hid):
            report.ok(f"mixed-tab routing: {hid} hook installed")
        else:
            report.warn(
                f"mixed-tab routing: {hid} plugin not installed — "
                f"@-routing from it is unavailable (tandem plugin install)")
```

called from `run_doctor` beside `_subagent_checks(report, session)`, with a matching test in `tests/test_memory_doctor.py` following its existing report-assertion style.

NOTE: the exact codex subcommand names (`codex plugin marketplace add` / `codex plugin install`) are the claude-compatible spelling and MUST be confirmed in Task 14's live gate; if codex spells them differently, fix here and re-run this task's tests.

- [ ] **Step 4: Run to verify pass**: `uv run pytest tests/test_plugin.py tests/test_plugin_setup.py -q`

- [ ] **Step 5: Full suite, then commit**

```bash
git add plugin/.codex-plugin/plugin.json src/tandem/plugin_setup.py \
        src/tandem/runner.py src/tandem/doctor.py \
        tests/test_plugin.py tests/test_plugin_setup.py tests/test_memory_doctor.py
git commit -m "feat: codex plugin registration, hook availability, doctor check"
```

---

### Task 13: docs

**Files:**
- Modify: `README.md` (new "The mixed tab" section under the feature docs, + one line in "Why tandem?")
- Modify: `docs/configuration.md` (`[frame] mixed`)

- [ ] **Step 1: Write the README section** (place after the "What's new" block, matching the README's voice; cover: what the mixed tab is, the `@` grammar with the three forms and examples, stay-is-free, the bar's mixed slot, the opencode no-routing hint, `[frame] mixed = false` opt-out, and that `@`-tokens that aren't targets pass through so file mentions keep working).

- [ ] **Step 2: Document `[frame] mixed`** in `docs/configuration.md` beside `bar`/`warm`/`rate_limits`.

- [ ] **Step 3: Commit**

```bash
git add README.md docs/configuration.md
git commit -m "docs: mixed tab and @-routing"
```

---

### Task 14: live verification gate (tmux) + contingent residue filter

**Files:**
- Modify: `docs/formats.md` (record findings)
- Possibly modify: `src/tandem/harness/claude_code.py` / `codex.py` (residue filter — contingent), `src/tandem/harness/opencode.py` (`prompt_hook_capable` — contingent), `src/tandem/plugin_setup.py` (codex subcommand spelling — contingent)

This task runs on the operator's machine against the installed CLIs, using the established tmux gate recipe (drive a real terminal via `tmux new-session -d`, `tmux send-keys`, `tmux capture-pane`; the memory notes from the opencode workstream describe the pattern). Reinstall the tool first: `uv tool install --force --from . tandem-cli` — and remember plugin hooks register at session start only, so restart harness sessions after install.

- [ ] **Probe 1 — claude blocked-prompt residue (spec verification item 1).** In a tmux pane: `tandem` in a scratch repo, Ctrl-] to the mixed tab (claude focus), type `@codex say hello` + Enter. Confirm claude shows the block reason and does NOT run the turn. Then inspect the claude transcript JSONL for the session: does the blocked prompt appear as any entry? Record the exact finding (entry type/shape or "no residue") in `docs/formats.md`.
  - **If residue exists:** add a filter in `claude_code.py`'s `parse_entry` that drops the residue entry shape (key it on whatever claude actually writes — e.g. a user entry flagged by the hook block, matched by its recorded shape, never by prompt-text heuristics), with a golden fixture captured from this probe and a unit test. Commit as `fix: filter blocked-prompt residue from claude sync`.
  - **If no residue:** note it in docs/formats.md; no code.
- [ ] **Probe 2 — the full routed relay.** Same session: confirm the flip to codex fires, the injected prompt appears in codex's composer and submits, codex answers, and the turn syncs back to the claude shadow (flip back with Ctrl-] ×2 and check the conversation shows the codex turn). Then `@claude` from codex focus (tests the codex-side hook: block honored, route fires). Record pass/fail per leg. **If codex does not honor the block decision:** set `codex.prompt_hook_capable = False`, adjust Task 5's test, record in docs/formats.md — routing *from* codex then shows the bar hint instead.
- [ ] **Probe 3 — model pin.** `@codex:gpt-5.3-codex say hi` from claude focus: confirm codex launches with `-m gpt-5.3-codex` (check the rollout's session_meta or `/status`). `@haiku say hi` from codex focus: confirm claude relaunches with `--model haiku` and the resumed session still carries history. Also confirm a plain Ctrl-] flip AFTER a model-pinned routed turn relaunches codex withOUT the pin (recipe.model only set when inject targets that side).
- [ ] **Probe 4 — injection timing.** Route a multi-line prompt (paste a 3-line prompt after `@codex `): confirm it arrives as one composer block (bracketed paste) and submits once. If the 2.5 s codex settle delay proves flaky, tune `_deliver_inject`'s delay constant and note the number in docs/formats.md.
- [ ] **Probe 5 — opencode.** Route TO opencode (`@opencode …`): target-side injection must work. Check whether opencode runs claude-format UserPromptSubmit hooks at all (type `@claude x` with opencode focus; if the prompt just runs natively, capability stays False and the bar hint covers it). Record either way.
- [ ] **Probe 6 — resume + stale routes.** Exit tandem from the mixed tab; `tandem resume`; confirm the mixed tab and focus are restored. Kill tandem (SIGKILL) mid-route (after block, before injection); resume; confirm the preserved-prompt note appears and no phantom flip fires.
- [ ] **Probe 7 — codex plugin subcommands.** `tandem plugin install` on a machine with codex: confirm the codex-side registration commands exist and succeed (fix `install_plugin_codex` spelling if not; the codex config.toml gains `[plugins."tandem@tandem"]`).
- [ ] **Record everything** in `docs/formats.md` under a "Mixed-tab routing (v1) live gate" heading with CLI versions, then commit:

```bash
git add docs/formats.md src/tandem/harness/ tests/
git commit -m "docs: mixed-tab routing live-gate findings"
```

- [ ] **Full suite one last time**: `uv run pytest -q` — green, then the branch is ready for PR (`gh pr create` per repo release mechanics; checks are not required on merge — branch on `gh pr checks` exit code).

---

## Self-review notes (already applied)

- Spec coverage: every spec section maps to a task — grammar (T1), stash/files (T2), persistence (T3), toggle (T4), model pinning (T5, upgraded from `/model` injection to launch argv, recorded in the header), tab semantics (T6/T11), bar (T7), injection (T8/T10), hook + registration (T9/T12), hook-registration trap: bar hint + doctor check (T12), failure handling (T2 TTL + T10 mixer notes + T11 ladder), verification items (T14 probes 1/2/5/3), testing section (unit tests per task + T14 tmux gate).
- The spec's "reason shown on the bar" is satisfied by the mixed slot showing focus (+model via the launch recipe) and the block reason showing the route in the source harness's own UI; a transient "→ codex · …" bar flash is deliberately NOT built (YAGNI — the block reason already tells the user, in the harness they were typing in).
- Type consistency: `RouteRequest` is defined once (routefile) and consumed by promptroute's CLI wrapper (cli.py), runner, and flip; `RouteDecision` never crosses a process boundary; `TabMove.kind` strings are pinned in Task 6's tests.
