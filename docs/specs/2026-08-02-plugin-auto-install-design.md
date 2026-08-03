# Plugin auto-install on first run — design

**Date:** 2026-08-02
**Status:** approved

## Problem

Installing tandem is two disconnected steps on two channels: `pip install
tandem-cli` for the CLI, then two `claude plugin` commands (marketplace add +
install) for the plugin. Users who skip the second half get a tandem that
pairs sessions but never reroutes subagents. Literal install-time automation
is impossible — wheels have no post-install hooks — so the CLI itself closes
the gap at first run.

`claude plugin install` cannot take a GitHub link directly (verified: it only
installs from added marketplaces), so "one command" means a tandem command
that wraps both of Claude's.

## Design

### 1. Shared install routine — `src/tandem/plugin_setup.py`

- `is_plugin_installed() -> bool` — reads
  `~/.claude/plugins/installed_plugins.json` and checks for a `tandem@tandem`
  entry with at least one install record. A missing file or an absent/empty
  `tandem@tandem` entry is definitively not installed (`False` — a fresh
  Claude install has no registry, and that user must see the offer). An
  unreadable or unparseable file, or an unexpected shape, returns `True`:
  ambiguity means stay silent, never nag. Read-only — Claude's CLI
  remains the sole writer of its own state.
- `install_plugin() -> bool` — runs `claude plugin marketplace add
  Bhavya6187/tandem` then `claude plugin install tandem@tandem`, echoing each
  command as it runs. "Marketplace already exists" from the first step is
  success; proceed to install. On success, print one note that the plugin
  takes effect in new Claude sessions only (hooks register at session start).
  On failure, print the two manual commands and return False.
- `offer_install() -> None` — the first-run prompt (§3).

### 2. `tandem plugin install`

New `tandem plugin` click group with one subcommand, `install`, calling
`install_plugin()`. Exits nonzero on failure — including `claude` missing
from PATH, with a clear message. No detection gate: the explicit command
always attempts the real thing, so it doubles as the retry path.

### 3. First-run offer in bare `tandem`

In `_interactive()`, after the pairing echoes and before the shell starts,
prompt only when all four hold:

1. stdin is a TTY (scripts and CI are never blocked),
2. `claude` is on PATH,
3. `is_plugin_installed()` is False,
4. no answer stamp exists.

Prompt: `Install the tandem Claude Code plugin for codex-model subagents?
[Y/n]`. Yes → `install_plugin()`. No → print `You can install it later with:
tandem plugin install`. Either answer writes a best-effort stamp at
`$TANDEM_HOME/plugin-offer` (same pattern as the `warned/` stamps), so the
question is asked once per machine — matching the plugin's user scope. A
failed install still stamps; the printed wrapper command is the retry path.

### 4. Deliberately unchanged

The plugin still ships from GitHub via the marketplace. Auto-install pulls
current main, not the pip-pinned version — same as the manual flow today; the
`tandem hook-route || true` guard and the version drift-guard test already
cover skew. No plugin files in the wheel. No writes to Claude's plugin state.

### 5. README

Quickstart becomes: install the package, run `tandem`, answer the prompt —
with `tandem plugin install` as the manual/later path. The raw `claude
plugin` commands stay documented for transparency; uninstall docs unchanged.

### 6. Testing

Unit tests in the existing style, TDD during implementation:

- detection: present / absent / malformed `installed_plugins.json` fixtures,
- prompt gating: non-TTY → silent, stamp present → silent, installed →
  silent,
- decline: stamp written, hint printed,
- install sequencing and already-added-marketplace tolerance via mocked
  subprocess calls.
