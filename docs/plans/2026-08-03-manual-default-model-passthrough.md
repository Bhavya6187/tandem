# Manual Default + Model Pass-Through Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship 0.1.8: `route = "manual"` becomes the default, and "ask gpt-5.6-luna to …" reaches codex with the right `-m` via a `tandem-model:` brief header resolved against codex's model catalog.

**Architecture:** A new `modelcat.py` owns the header protocol (parse, resolve against `~/.codex/models_cache.json`, footer text); `cli.py sub()` wires it around the existing `ops.run_sub` (which is untouched — its `model=` kwarg already exists). The plugin change is one description sentence in `gpt.md`; the relay bodies stay byte-for-byte identical. Spec: `docs/specs/2026-08-03-manual-default-model-passthrough-design.md`.

**Tech Stack:** Python 3.11+ (tomllib), click, pytest with the repo's `env_factory` fixture (fake `TANDEM_HOME`/`CODEX_HOME` per test).

## Global Constraints

- Version goes to `0.1.8` in BOTH `pyproject.toml` and `plugin/.claude-plugin/plugin.json` (drift-guard test in `tests/test_plugin.py` fails on disagreement); run `uv lock` after the bump (precedent: commit `fa44504`).
- Relay agent BODIES (`plugin/agents/gpt.md`, `plugin/agents/codex-worker.md`) must remain byte-for-byte identical to each other; only `gpt.md`'s frontmatter `description:` changes.
- Header grammar (public protocol): first line of the brief, exactly `tandem-model:` + optional spaces/tabs + a name matching `[A-Za-z0-9._/:-]{1,64}`, nothing else on the line.
- Footer (public protocol, like `ops.BLOCKED_HEADER`): `[tandem-sub model: <slug>]`.
- Precedence: `-m` flag > header > `[subagents] model` config > codex default. A recognized header is stripped even when `-m` overrides it; the footer names the model that actually ran.
- Cross-version compatibility is out of scope (spec Section 3).
- Catalog source is `~/.codex/models_cache.json` (same `models` array `codex debug models` renders; codex maintains the file, README already points users at it). Unreadable/absent catalog → pass the requested name through verbatim.
- The error for an unresolvable name must list the visible slugs and reach stderr before codex is invoked: `error: unknown model 'o3'; this codex offers: gpt-5.6-sol, gpt-5.6-terra, …`

---

### Task 1: `modelcat.py` — header protocol and catalog resolution

**Files:**
- Create: `src/tandem/modelcat.py`
- Create: `tests/test_modelcat.py`
- Modify: `src/tandem/paths.py` (one helper, after `codex_sessions_dir()` at line ~64)
- Modify: `docs/specs/2026-08-03-manual-default-model-passthrough-design.md` (catalog source amendment)

**Interfaces:**
- Consumes: `paths.codex_home()` (exists).
- Produces (Task 2 relies on these exact names):
  - `modelcat.split_model_header(task: str) -> tuple[str, str]` — `(requested_name or "", task_without_header)`
  - `modelcat.load_catalog() -> list[dict] | None`
  - `modelcat.resolve(name: str, models: list[dict] | None) -> str` — slug, or `name` verbatim when `models is None`; raises `modelcat.UnknownModel`
  - `modelcat.model_footer(slug: str) -> str` — `"[tandem-sub model: <slug>]"`
  - `paths.codex_models_cache_path() -> Path`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_modelcat.py
"""tandem-model header parsing and catalog resolution."""

import json

import pytest

from tandem import modelcat, paths

CATALOG = [
    {"slug": "gpt-5.6-sol", "display_name": "GPT-5.6-Sol", "visibility": "list"},
    {"slug": "gpt-5.6-terra", "display_name": "GPT-5.6-Terra", "visibility": "list"},
    {"slug": "gpt-5.4-mini", "display_name": "GPT-5.4-Mini", "visibility": "list"},
    {"slug": "codex-auto-review", "display_name": "Codex Auto Review",
     "visibility": "hide"},
]


class TestSplitModelHeader:
    def test_header_is_stripped_and_returned(self):
        got = modelcat.split_model_header("tandem-model: gpt-5.6-sol\nreview x")
        assert got == ("gpt-5.6-sol", "review x")

    def test_no_header_passes_through(self):
        assert modelcat.split_model_header("review x\nline 2") == ("", "review x\nline 2")

    def test_header_requires_first_line(self):
        task = "review x\ntandem-model: gpt-5.6-sol"
        assert modelcat.split_model_header(task) == ("", task)

    def test_malformed_name_left_in_place(self):
        # trailing prose after the name is not a header
        task = "tandem-model: gpt-5.6-sol please\nreview x"
        assert modelcat.split_model_header(task) == ("", task)

    def test_overlong_name_left_in_place(self):
        task = f"tandem-model: {'x' * 65}\nreview x"
        assert modelcat.split_model_header(task) == ("", task)

    def test_header_only_brief_yields_empty_task(self):
        assert modelcat.split_model_header("tandem-model: gpt-5.6-sol") == \
            ("gpt-5.6-sol", "")

    def test_no_space_after_colon_still_parses(self):
        assert modelcat.split_model_header("tandem-model:sol\nt") == ("sol", "t")


class TestResolve:
    def test_exact_slug(self):
        assert modelcat.resolve("gpt-5.6-sol", CATALOG) == "gpt-5.6-sol"

    def test_display_name_case_and_punctuation_insensitive(self):
        assert modelcat.resolve("GPT 5.6 Sol", CATALOG) == "gpt-5.6-sol"

    def test_unique_substring(self):
        assert modelcat.resolve("sol", CATALOG) == "gpt-5.6-sol"
        assert modelcat.resolve("5.4 mini", CATALOG) == "gpt-5.4-mini"

    def test_ambiguous_lists_visible_slugs(self):
        with pytest.raises(modelcat.UnknownModel) as e:
            modelcat.resolve("gpt", CATALOG)
        msg = str(e.value)
        assert "unknown model 'gpt'" in msg
        assert "gpt-5.6-sol" in msg and "gpt-5.4-mini" in msg
        assert "codex-auto-review" not in msg

    def test_no_match_lists_visible_slugs(self):
        with pytest.raises(modelcat.UnknownModel) as e:
            modelcat.resolve("o3", CATALOG)
        assert "unknown model 'o3'; this codex offers: gpt-5.6-sol" in str(e.value)

    def test_hidden_models_never_match(self):
        with pytest.raises(modelcat.UnknownModel):
            modelcat.resolve("auto review", CATALOG)

    def test_none_catalog_passes_name_through(self):
        assert modelcat.resolve("anything-goes", None) == "anything-goes"


class TestLoadCatalog:
    def test_reads_models_array(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CODEX_HOME", str(tmp_path))
        (tmp_path / "models_cache.json").write_text(
            json.dumps({"fetched_at": "t", "models": CATALOG}))
        assert modelcat.load_catalog() == CATALOG

    def test_missing_file_is_none(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CODEX_HOME", str(tmp_path))
        assert modelcat.load_catalog() is None

    def test_broken_json_is_none(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CODEX_HOME", str(tmp_path))
        (tmp_path / "models_cache.json").write_text("{nope")
        assert modelcat.load_catalog() is None

    def test_models_not_a_list_is_none(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CODEX_HOME", str(tmp_path))
        (tmp_path / "models_cache.json").write_text(json.dumps({"models": 7}))
        assert modelcat.load_catalog() is None


def test_model_footer_exact_text():
    assert modelcat.model_footer("gpt-5.6-sol") == "[tandem-sub model: gpt-5.6-sol]"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_modelcat.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'tandem.modelcat'`

- [ ] **Step 3: Add the paths helper**

In `src/tandem/paths.py`, directly under `codex_sessions_dir()`:

```python
def codex_models_cache_path() -> Path:
    """Codex's on-disk model catalog (observed: codex-cli 0.145.0 writes
    {fetched_at, etag, client_version, models:[{slug, display_name,
    visibility, …}]} and refreshes it on every exec)."""
    return codex_home() / "models_cache.json"
```

- [ ] **Step 4: Write `src/tandem/modelcat.py`**

```python
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
    refusing to dispatch over bookkeeping."""
    try:
        data = json.loads(paths.codex_models_cache_path().read_text())
    except (OSError, ValueError):
        return None
    models = data.get("models") if isinstance(data, dict) else None
    if not isinstance(models, list):
        return None
    return [m for m in models if isinstance(m, dict)]


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
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_modelcat.py -q`
Expected: PASS (19 tests)

- [ ] **Step 6: Amend the spec's catalog source**

In `docs/specs/2026-08-03-manual-default-model-passthrough-design.md`, replace the sentence beginning `Instead, `tandem sub` resolves the header value against `codex debug models`` so the bullet reads that resolution uses **`~/.codex/models_cache.json`** (the same `models` array `codex debug models` renders; codex maintains the file and the README already points users at it; reading the wrapped CLI's files is tandem's established method per `docs/formats.md`), with `codex debug models` noted as the debugging surface. Keep the fallback sentence: unreadable catalog → verbatim pass-through.

- [ ] **Step 7: Commit**

```bash
git add src/tandem/modelcat.py src/tandem/paths.py tests/test_modelcat.py \
  docs/specs/2026-08-03-manual-default-model-passthrough-design.md
git commit -m "feat: tandem-model header protocol + catalog resolution (modelcat)"
```

---

### Task 2: wire the header through `tandem sub`

**Files:**
- Modify: `src/tandem/cli.py` — inside `def sub(...)` (lines 329–356)
- Test: `tests/test_sub.py` — new `TestSubModelHeader` class next to the existing `TestSubCommand` (which holds the `_cli_env` helper pattern to copy, line ~440)

**Interfaces:**
- Consumes: `modelcat.split_model_header`, `modelcat.load_catalog`, `modelcat.resolve`, `modelcat.UnknownModel`, `modelcat.model_footer` (Task 1); `ops.run_sub(store, session, task, *, model=..., ...)` (exists, unchanged).
- Produces: `tandem sub` behavior relied on by the plugin: header consumed, `-m` precedence, stderr `worker model:` line (non-quiet), stdout footer (quiet), pre-codex failure on unknown model.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_sub.py` (module already imports `json`, `ops`; the class copies `TestSubCommand`'s `_cli_env` helper shape):

```python
class TestSubModelHeader:
    def _cli_env(self, env, monkeypatch):
        from tandem import cli
        monkeypatch.setattr(cli, "_cwd", lambda: env.cwd)
        monkeypatch.setattr(
            cli, "_check_versions",
            lambda warn_only=False: {"claude": "2.1.220", "codex": "0.145.0"},
        )
        return cli

    def _catalog(self):
        from tandem import paths
        p = paths.codex_models_cache_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"models": [
            {"slug": "gpt-5.6-sol", "display_name": "GPT-5.6-Sol",
             "visibility": "list"},
            {"slug": "gpt-5.4-mini", "display_name": "GPT-5.4-Mini",
             "visibility": "list"},
        ]}))

    def _capture(self, monkeypatch):
        calls = {}
        monkeypatch.setattr(
            ops, "run_sub",
            lambda store, session, task, **kw: calls.update(
                task=task, kw=kw) or 0,
        )
        return calls

    def test_header_resolves_and_is_stripped(self, env_factory, monkeypatch):
        import click.testing
        env = env_factory(active="claude")
        cli = self._cli_env(env, monkeypatch)
        self._catalog()
        calls = self._capture(monkeypatch)
        r = click.testing.CliRunner().invoke(
            cli.main, ["sub"], input="tandem-model: sol\ndo the thing\n")
        assert r.exit_code == 0
        assert calls["task"] == "do the thing"
        assert calls["kw"]["model"] == "gpt-5.6-sol"

    def test_flag_beats_header_but_header_still_stripped(
            self, env_factory, monkeypatch):
        import click.testing
        env = env_factory(active="claude")
        cli = self._cli_env(env, monkeypatch)
        self._catalog()
        calls = self._capture(monkeypatch)
        r = click.testing.CliRunner().invoke(
            cli.main, ["sub", "-m", "flag-model"],
            input="tandem-model: sol\ntask\n")
        assert r.exit_code == 0
        assert calls["task"] == "task"
        assert calls["kw"]["model"] == "flag-model"

    def test_unknown_model_fails_before_codex(self, env_factory, monkeypatch):
        import click.testing
        env = env_factory(active="claude")
        cli = self._cli_env(env, monkeypatch)
        self._catalog()
        calls = self._capture(monkeypatch)
        r = click.testing.CliRunner().invoke(
            cli.main, ["sub"], input="tandem-model: o3\ntask\n")
        assert r.exit_code == 1
        assert "unknown model 'o3'" in r.output
        assert "gpt-5.6-sol" in r.output
        assert calls == {}          # codex was never invoked

    def test_quiet_appends_model_footer(self, env_factory, monkeypatch):
        import click.testing
        env = env_factory(active="claude")
        cli = self._cli_env(env, monkeypatch)
        self._catalog()
        self._capture(monkeypatch)
        r = click.testing.CliRunner().invoke(
            cli.main, ["sub", "-q"], input="tandem-model: sol\ntask\n")
        assert r.output.rstrip().endswith("[tandem-sub model: gpt-5.6-sol]")

    def test_no_header_no_footer(self, env_factory, monkeypatch):
        import click.testing
        env = env_factory(active="claude")
        cli = self._cli_env(env, monkeypatch)
        self._catalog()
        calls = self._capture(monkeypatch)
        r = click.testing.CliRunner().invoke(
            cli.main, ["sub", "-q"], input="just a task\n")
        assert "[tandem-sub model:" not in r.output
        assert calls["kw"]["model"] == ""   # config default: unset

    def test_non_quiet_announces_worker_model(self, env_factory, monkeypatch):
        import click.testing
        env = env_factory(active="claude")
        cli = self._cli_env(env, monkeypatch)
        self._catalog()
        self._capture(monkeypatch)
        r = click.testing.CliRunner().invoke(
            cli.main, ["sub"], input="tandem-model: sol\ntask\n")
        assert "worker model: gpt-5.6-sol" in r.output

    def test_missing_catalog_passes_name_verbatim(
            self, env_factory, monkeypatch):
        import click.testing
        env = env_factory(active="claude")
        cli = self._cli_env(env, monkeypatch)
        # no _catalog() call: CODEX_HOME has no models_cache.json
        calls = self._capture(monkeypatch)
        r = click.testing.CliRunner().invoke(
            cli.main, ["sub"], input="tandem-model: mystery-model\ntask\n")
        assert r.exit_code == 0
        assert calls["kw"]["model"] == "mystery-model"

    def test_header_only_brief_is_an_empty_task_error(
            self, env_factory, monkeypatch):
        import click.testing
        env = env_factory(active="claude")
        cli = self._cli_env(env, monkeypatch)
        self._catalog()
        calls = self._capture(monkeypatch)
        r = click.testing.CliRunner().invoke(
            cli.main, ["sub"], input="tandem-model: sol\n")
        assert r.exit_code == 1
        assert "empty task brief" in r.output
        assert calls == {}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_sub.py::TestSubModelHeader -q`
Expected: FAIL — header reaches `run_sub` as task text, `model` is `""`, no footer.

- [ ] **Step 3: Wire `sub()` in `src/tandem/cli.py`**

Replace the body of `sub()` (currently lines 334–356) with:

```python
    """Run one delegated subagent task on codex (task argument or stdin).

    Used by the tandem plugin's codex-worker bridge; also works manually.
    A brief whose first line is `tandem-model: <name>` picks the codex
    model for this worker: the name is resolved against codex's own model
    catalog (~/.codex/models_cache.json), and an unresolvable name fails
    here, before codex is invoked, with the valid slugs listed."""
    from . import modelcat, ops
    from .config import load_subagents_config

    if task is None or task == "-":
        task = sys.stdin.read()
    task = task.strip()
    requested, task = modelcat.split_model_header(task)
    task = task.strip()
    if not task:
        click.secho("error: empty task brief.", fg="red", err=True)
        sys.exit(1)
    resolved = ""
    if requested:
        try:
            resolved = modelcat.resolve(requested, modelcat.load_catalog())
        except modelcat.UnknownModel as e:
            click.secho(f"error: {e}", fg="red", err=True)
            sys.exit(1)
    cfg = load_subagents_config()
    worker_model = model if model is not None else (resolved or cfg.model)
    if requested and not quiet:
        click.secho(f"worker model: {worker_model}", err=True)
    with StateStore() as store:
        session = _require_session(store)
        code = ops.run_sub(
            store, session, task,
            model=worker_model,
            context=context_mode or ("full" if cfg.context == "full" else "task"),
            fanout_feature=cfg.fanout_feature,
            keep_forks=cfg.keep_forks,
            quiet=quiet,
            sandbox=sandbox if sandbox is not None
                    else _read_sandbox_stamp(session.tandem_id),
        )
    if requested and quiet:
        sys.stdout.write("\n" + modelcat.model_footer(worker_model) + "\n")
        sys.stdout.flush()
    sys.exit(code)
```

(The only changes from the current body: the docstring's second paragraph, the `modelcat` import, the `split_model_header`/`resolve` block before the empty-task check's new position, `model=worker_model` instead of the inline conditional, the stderr announcement, and the footer write before `sys.exit`.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_sub.py -q`
Expected: PASS — the new class and every pre-existing `TestSubCommand` test (the empty-brief and stdin tests must still pass; if `test_sub_reads_task_from_stdin` fails, the wiring reordered something it shouldn't have).

- [ ] **Step 5: Commit**

```bash
git add src/tandem/cli.py tests/test_sub.py
git commit -m "feat: tandem sub honors the tandem-model brief header"
```

---

### Task 3: flip the routing default to manual

**Files:**
- Modify: `src/tandem/config.py:17` and the comment block at lines 24–30
- Modify: `tests/test_config.py:10`
- Modify: `tests/test_hookroute.py:32` (+ one new test in the manual-mode class at line ~176)

**Interfaces:**
- Consumes: nothing new.
- Produces: `SubagentsConfig().route == "manual"` — the shipped default every module reads.

- [ ] **Step 1: Update the tests first**

In `tests/test_config.py` line 10, the missing-file default becomes:

```python
    assert (cfg.route, cfg.model, cfg.context) == ("manual", "", "match")
```

In `tests/test_hookroute.py` line 32, the module-level config the rewrite
tests use stops leaning on the default (they test all-mode behavior):

```python
CFG = SubagentsConfig(route="all")
```

In the class holding `test_manual_never_rewrites` (line ~176), add:

```python
    def test_default_config_never_rewrites(self):
        # the shipped default IS manual now — no cfg file, no rewrite
        assert _route(_payload(), cfg=SubagentsConfig()) is None
```

- [ ] **Step 2: Run tests to verify the new expectations fail**

Run: `uv run pytest tests/test_config.py tests/test_hookroute.py -q`
Expected: `test_defaults_when_file_missing` and `test_default_config_never_rewrites` FAIL (default is still `"all"`); everything else PASSES (line 32 made the rewrite tests explicit).

- [ ] **Step 3: Flip the default**

In `src/tandem/config.py`:

```python
    route: str = "manual"       # "manual" | "all" | "off"
```

and rewrite the comment block above `_ROUTES` (lines 24–30) to:

```python
# "manual" — the default: no auto-reroute and no missed-reroute notice —
# dispatch to codex only when the model/user explicitly picks a bridge agent
# (`tandem:gpt`, `tandem:codex-worker`). The hook treats it exactly like
# "off"; the rest of tandem does not — `doctor._subagent_checks` silences
# its subagent billing warnings only under "off", because a manual user
# still dispatches to codex and still wants to know the worker model is
# unset. "all" opts back in to rerouting every native dispatch.
```

- [ ] **Step 4: Run the full suite**

Run: `uv run pytest -q`
Expected: PASS. Any other failure means a test leaned on the `"all"` default this plan missed — fix it by passing `SubagentsConfig(route="all")` explicitly in that test, since such tests exercise all-mode behavior.

- [ ] **Step 5: Commit**

```bash
git add src/tandem/config.py tests/test_config.py tests/test_hookroute.py
git commit -m "feat!: route defaults to manual — dispatches stay on claude unless asked"
```

---

### Task 4: teach the orchestrator the header (gpt.md description)

**Files:**
- Modify: `plugin/agents/gpt.md:3` (frontmatter `description:` only)

**Interfaces:**
- Consumes: the header grammar from Global Constraints.
- Produces: the description text the orchestrating model acts on; Task 6's live acceptance depends on it.

- [ ] **Step 1: Extend the description**

Line 3 of `plugin/agents/gpt.md` becomes (one line):

```yaml
description: Runs the task on a GPT model via tandem's codex pairing. Select this when the user asks for GPT subagents or to run something on GPT/codex. If the user asked for a specific model, put `tandem-model: <name as the user said it>` alone on the first line of the task; tandem translates it to an exact codex model.
```

- [ ] **Step 2: Verify the bodies are still identical**

Run (from the repo root):

```bash
awk 'f{print} /^---$/{c++; if(c==2) f=1}' plugin/agents/gpt.md > /tmp/gpt-body.txt
awk 'f{print} /^---$/{c++; if(c==2) f=1}' plugin/agents/codex-worker.md > /tmp/cw-body.txt
diff /tmp/gpt-body.txt /tmp/cw-body.txt && echo BODIES-IDENTICAL
```

Expected: `BODIES-IDENTICAL` (only frontmatter differs between the two files).

- [ ] **Step 3: Run the plugin tests**

Run: `uv run pytest tests/test_plugin.py -q`
Expected: PASS (nothing asserts on the description text; this catches accidental frontmatter breakage).

- [ ] **Step 4: Commit**

```bash
git add plugin/agents/gpt.md
git commit -m "feat: gpt agent description solicits a tandem-model header for named models"
```

---

### Task 5: rewrite the README copy around the new default

**Files:**
- Modify: `README.md` (lines 62–70 intro paragraph; lines 93–111 config block and mode bullets)
- Modify: `plugin/README.md` (lines 45–51 gpt.md bullet; lines 58–81 hook behavior; lines 83–94 configuration)

**Interfaces:**
- Consumes: the shipped behavior from Tasks 1–4.
- Produces: user-facing truth; no other task depends on it.

- [ ] **Step 1: README.md — intro paragraph**

Replace lines 64–66 ("Load tandem's Claude Code plugin and Claude's subagent dispatches run on the codex model you choose instead — automatically, with the task brief") with copy stating the new contract: load the plugin and GPT subagents are one ask away — say "ask gpt to review this" and the dispatch runs on a codex model, "ask gpt-5.4-mini" and it runs on that exact model; set `route = "all"` if you want every subagent dispatch rerouted automatically. Keep the rest of the paragraph (result returned through Claude's machinery, quota point, two-pieces sentence) intact.

- [ ] **Step 2: README.md — config block and bullets**

Line 96 becomes:

```toml
route = "manual"        # manual | all | off
```

Rewrite the bullet at lines 106–111 as two bullets:

- `route = "manual"` (the default): dispatches stay on Claude until you ask — "use gpt subagents", "ask codex to …". Name a model — "ask gpt-5.4-mini to …" — and the request travels as a `tandem-model:` first line in the brief that tandem resolves against your codex install's own model catalog (`~/.codex/models_cache.json`); a name that doesn't resolve fails fast listing what your account actually offers, and the worker's reply carries a `[tandem-sub model: …]` trailer naming what ran. (`route = "off"` is the same routing silence, but `manual` keeps `tandem doctor`'s subagent checks on.)
- `route = "all"` reroutes every native subagent dispatch to codex automatically. In this mode a "have Claude and GPT both review it" ask becomes codex twice — manual is the mode where mix-and-match works.

Also update lines 101–104: "routing is on as soon as the plugin loads" is no longer true — the model-unset warning story stays, but tie it to dispatching (`tandem doctor` warns until you set `model`, since explicit gpt dispatches still bill your codex account's default model otherwise).

- [ ] **Step 3: plugin/README.md**

- Lines 46–51 (`agents/gpt.md` bullet): after "This is the agent to select when you ask for GPT subagents by name", add: a brief whose first line is `tandem-model: <name>` picks the worker model — the description solicits it from the orchestrating model when the user names one, and `tandem sub` resolves the name against codex's catalog. Drop "and with `[subagents] route = "manual"` …" framing that implies manual is the exception; manual is now the default and selecting this agent is the normal path to codex.
- Line 69 ("with `route = "all"` (the default)") → `with route = "all" (opt-in; the default is "manual")`.
- Lines 75–76: "with `route = "manual"` or `"off"`, rewrites nothing" → "with `route = "manual"` (the default) or `"off"`, rewrites nothing".
- Lines 85–91 (Configuration): "Routing is enabled by default." → "Dispatches reach codex when you ask for the `tandem:gpt` agent; set `route = "all"` to reroute every dispatch automatically." Flip the example TOML to show `route = "all"` (since manual needs no config).

- [ ] **Step 4: Proofread render**

Run: `grep -n "route" README.md plugin/README.md`
Expected: no surviving claim that `all` is the default or that routing is automatic out of the box.

- [ ] **Step 5: Commit**

```bash
git add README.md plugin/README.md
git commit -m "docs: README copy for manual-default routing and model pass-through"
```

---

### Task 6: version 0.1.8 + full verification + live acceptance

**Files:**
- Modify: `pyproject.toml:3` (`version = "0.1.8"`)
- Modify: `plugin/.claude-plugin/plugin.json:4` (`"version": "0.1.8"`)
- Modify: `uv.lock` (via `uv lock`)

**Interfaces:**
- Consumes: everything prior.
- Produces: the releasable tree.

- [ ] **Step 1: Bump both versions**

`pyproject.toml` line 3 → `version = "0.1.8"`; `plugin/.claude-plugin/plugin.json` line 4 → `"version": "0.1.8",`

- [ ] **Step 2: Sync the lockfile**

Run: `uv lock`
Expected: `uv.lock` updates the `tandem-cli` version only.

- [ ] **Step 3: Full suite**

Run: `uv run pytest -q`
Expected: PASS, including the plugin.json/pyproject drift guard in `tests/test_plugin.py`.

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml plugin/.claude-plugin/plugin.json uv.lock
git commit -m "release: 0.1.8 — manual routing by default, tandem-model pass-through"
```

- [ ] **Step 5: Live acceptance (operator-run; needs a paired session + reinstalled plugin)**

The two phrases that failed in the research are the acceptance test. In a scratch project with a real paired tandem session (`tandem` running there) and the 0.1.8 plugin picked up by a fresh Claude session:

```bash
claude -p "Ask gpt-5.4-mini to review utils.py" --output-format stream-json --verbose --max-turns 8 > accept1.jsonl
claude -p "Ask o3 to review utils.py" --output-format stream-json --verbose --max-turns 8 > accept2.jsonl
```

Verify in `accept1.jsonl`: the `tandem:gpt` dispatch's prompt begins `tandem-model: gpt-5.4-mini` (or the user's wording), the relay's Bash result ends with `[tandem-sub model: gpt-5.4-mini]`, and `~/.tandem/subagents/<id>/logs` shows the codex run on that model. Verify in `accept2.jsonl`: the relay's result contains `[tandem-sub failed] error: unknown model 'o3'; this codex offers: …` and the orchestrating session surfaces the available slugs instead of flailing into `codex exec`.

---

## Self-Review

- **Spec coverage:** default flip → Task 3; description sentence → Task 4; header parse/precedence/trailer → Tasks 1–2; catalog resolution + fail-fast listing + hidden exclusion + verbatim fallback → Task 1; README copy → Task 5; versioning → Task 6; unit matrix from spec Section 4 → Tasks 1–3 tests; live acceptance → Task 6 Step 5. Spec's `codex debug models` line is amended to `models_cache.json` in Task 1 Step 6.
- **Placeholders:** none — every step carries the code or the exact edit.
- **Type consistency:** `split_model_header`, `load_catalog`, `resolve`, `UnknownModel`, `model_footer`, `codex_models_cache_path` are named identically in Task 1 (producer) and Task 2 (consumer); `run_sub`'s signature is untouched.
