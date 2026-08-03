<div align="center">

# 🤝 tandem

**One coding session. Two AI agents. Zero lost context.**

Run [Claude Code](https://docs.anthropic.com/en/docs/claude-code) and
[OpenAI Codex CLI](https://github.com/openai/codex) as a single paired
session — each model in its own native harness. Work in either one,
switch at any moment, and pick up exactly where you left off. No double
spend: only one model runs per turn; the other stays in sync through pure
local file translation.

[![CI](https://github.com/Bhavya6187/tandem/actions/workflows/ci.yml/badge.svg)](https://github.com/Bhavya6187/tandem/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/tandem-cli)](https://pypi.org/project/tandem-cli/)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://github.com/Bhavya6187/tandem/blob/main/pyproject.toml)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](https://github.com/Bhavya6187/tandem/blob/main/LICENSE)

```bash
uv tool install tandem-cli
```

![tandem demo — one session moving between Claude Code and Codex](https://raw.githubusercontent.com/Bhavya6187/tandem/main/docs/demo.gif)

</div>

---

## Why tandem?

### ⏳ Hit a usage limit? Just keep going.

Claude runs out of its usage window mid-refactor? Type `switch` and Codex
continues the **same conversation** a second later — same files, same
history, same plan. Your two subscriptions become one long runway instead
of two separate walls.

### 🌩️ Immune to outages.

An Anthropic or OpenAI outage doesn't stop your work. If the active
model's API goes down, `switch` — the same session continues seamlessly
in the other harness, and you can switch back whenever the outage clears.

### 💳 Subscriptions, not API bills.

tandem wraps the official CLIs under the auth you already have — your
Claude and ChatGPT subscription logins work as-is. No API keys to
provision, no per-token surprises. tandem itself makes **zero network
calls**; every model call happens inside the real CLI, on your existing
plan.

### 🧠 Two model families on one problem.

Claude and GPT have different strengths and different blind spots. Get a
second opinion **with full session context** — no copy-pasting walls of
text between terminals:

```bash
tandem run --on codex "second opinion: why is this test flaky?"
```

### 🐣 Subagents on the cheap model.

Load tandem's Claude Code plugin and Claude's subagent dispatches run on
the codex model you choose instead — automatically, with the task brief
forwarded verbatim and the result returned through Claude's own machinery.
Claude orchestrates; codex does the legwork; your Claude quota stays on the
main thread. Two pieces: the `tandem` binary the hook shells out to, and
the plugin that registers the hook — the plugin lives in this repo, not in
the wheel, and without the binary on PATH it is inert.

```bash
uv tool install tandem-cli   # the binary the hook drives
tandem                       # first launch offers the plugin install — hit enter
```

Said no at the prompt, or running non-interactively? One command performs
both marketplace steps whenever you're ready:

```bash
tandem plugin install   # = claude plugin marketplace add Bhavya6187/tandem
                        #   + claude plugin install tandem@tandem
```

The marketplace tracks this repo's default branch, but nothing updates
behind your back: the first-launch offer asks before touching anything, and
you pull new versions when you run `claude plugin marketplace update` (or
`/plugin marketplace update` inside Claude).

Then create `~/.tandem/config.toml` and pick your plan's cheap model (ids
are listed in `~/.codex/models_cache.json`):

```toml
[subagents]
model = "gpt-5.6-luna"  # ← the whole point: set this
route = "all"           # all | manual | off
context = "match"       # match | task | full
keep_forks = false      # keep each worker's rollout for debugging
```

**Without `model`, workers run on your codex account's default model —
probably not the cheap one.** Every other key above is already the default;
that one is not, and routing is on as soon as the plugin loads, so
`tandem doctor` warns until you set it.

- `route = "manual"` turns off automatic rerouting: dispatches stay on
  Claude until you explicitly ask for the `tandem:gpt` agent — "use gpt
  subagents for this". (`route = "off"` is the same silence, but `manual`
  keeps `tandem doctor`'s subagent checks on, since you still send work to
  codex.) Explicit `tandem:gpt` dispatches work under `route = "all"` too;
  there they're just not the only way in.
- Write access follows your Claude permission mode. Dispatch while you're in
  `acceptEdits` or `bypassPermissions` and the codex worker runs with
  `--sandbox workspace-write`; in any other mode tandem passes no sandbox
  flag at all, so the worker gets codex's own default — read-only, unless
  you configured codex otherwise. A read-only worker that tried to edit
  files comes back with a `[tandem-sub blocked: write]` trailer naming the
  rejected paths; then it's your call — have it rerun with `tandem sub -q
  --sandbox workspace-write`, or apply what it returned yourself.
- Know the trust boundary: `--sandbox` on `tandem sub` overrides whatever
  consent your permission mode stamped, and the only thing stopping a task
  brief from talking the relay into adding that flag is the relay's own
  instructions — so dispatching a task brief you don't trust is handing that
  text the relay's privileges.
- Your own agents named `gpt` or `codex-worker` are never rerouted — the
  loop guard matches on the last segment of the agent name in any scope — so
  a local `.claude/agents/gpt.md` of yours keeps dispatching natively.

Fork dispatches stay on Claude, each worker's full codex log is kept under
`~/.tandem/subagents/`, and `tandem status` lists the workers running right
now. The plugin is installed for every Claude session, so in a directory
with no paired tandem session, dispatches simply run natively — the first
one says so once, then the session stays quiet. `claude plugin uninstall
tandem@tandem` and Claude is stock again — add `claude plugin marketplace
remove tandem` to also unregister the marketplace.

Hacking on the plugin itself? Skip the marketplace and point Claude at your
clone: `claude --plugin-dir /path/to/tandem/plugin`.

### 🏠 Every model in its native harness.

This is not a lowest-common-denominator wrapper UI. Claude runs in real
Claude Code; GPT runs in real Codex CLI. Your keybindings, slash commands,
MCP servers, and muscle memory all work exactly as they do today — tandem
sits underneath, not in between.

### ⚡ Switching is instant.

While one agent is active, tandem quietly keeps the other one's native
session file up to date by translating the transcript as it grows — pure
local file I/O, no model calls, no "exporting…" step. The other side is
*always* resume-ready.

### 🔒 Local, private, no lock-in.

Everything lives in the CLIs' own session files plus a small SQLite
database in `~/.tandem`. No cloud sync, no telemetry. Uninstall tandem
tomorrow and both sessions still resume natively with `claude --resume`
and `codex resume`.

## Quick start

You'll need Python 3.11+ and the `claude` and `codex` CLIs on your PATH.

```bash
uv tool install tandem-cli   # or: pip install tandem-cli
cd your-project
tandem                       # fresh paired session; drops you into claude
```

Work normally. When you exit the agent, you land at tandem's prompt
instead of your shell — that's where the magic lives:

```
tandem (claude)> switch      # continue instantly in codex
tandem (codex)> exit
to continue this session: tandem resume a1b2c3d4e5f6
```

Come back anytime:

```bash
tandem resume                # most recent session in this directory
tandem resume a1b2c3d4e5f6   # a specific one (id from the exit hint)
```

### Command cheat sheet

| Command | What it does |
| --- | --- |
| `tandem` | Start a fresh paired session (Claude active; `--active codex` to flip) |
| `switch` | Flip active/shadow and enter the other agent — at the tandem prompt, or one-shot from your shell (one-shot only flips, it doesn't enter) |
| `tandem resume [id]` | Continue the most recent (or a specific) session |
| `tandem run --on codex "…"` | One-off prompt to the *other* agent, with full context |
| `tandem sub "…"` | Run one delegated task on a codex model (used by the plugin's reroute hook; `--sandbox read-only\|workspace-write` overrides the dispatching session's write consent) |
| `tandem status` | Show pairing, roles, and sync position |
| `tandem plugin install` | Install the tandem Claude Code plugin through claude's own CLI (marketplace add + install) |

There are also three maintenance commands — `tandem doctor` (health
check: verifies both sessions are resumable), `tandem sync` (manual
catch-up translation), and `tandem sync-mcp` (share MCP server configs
between the tools).

## How it works

- **One model per command — always.** Only the active harness's model is
  ever invoked (or, for `run --on`, the target's). The shadow side is pure
  local file I/O: tandem tails the active transcript, translates each
  entry, and appends it to the shadow's session file. The shadow's model
  is never called to "catch up".
- **A persistent prompt, not the OS shell.** Leaving the harness lands you
  at `tandem (claude)>`. There, `switch` flips roles and drops you
  straight into the other tool, Enter re-enters the current one, and
  `status` / `sync` / `doctor` / `run --on` / `sync-mcp` all run against
  this session. `exit` (or Ctrl-D) returns to your shell and prints the
  resume hint. Every command also works one-shot from your shell,
  targeting the directory's most recently used session.
- **PTY passthrough.** tandem launches the real CLI on a pty (raw mode,
  resize forwarding, signals through the line discipline) and never
  scrapes terminal output — the transcript files are the source of truth.
  Turn-complete hooks (`claude --settings` Stop hook, `codex -c
  notify=[…]`) are wired per-invocation as wake-up signals, with
  fs-watching as the data path and fallback. If your codex config already
  sets `notify`, tandem leaves it alone.
- **Append-only, crash-safe sync.** Each transcript entry is translated as
  it lands — no bulk re-export at switch time. Appends are whole-line +
  fsync, and a write-ahead intent in the sync cursor makes translation
  exactly-once across crashes; on restart, sync resumes from the last
  confirmed entry.
- **Tool calls translate natively.** The harnesses speak different tool
  vocabularies, so each completed call+result pair is re-expressed in the
  shadow's own terms — `Bash` ↔ `exec_command`, `Edit`/`Write` ↔
  `apply_patch`, `TodoWrite` ↔ `update_plan` — and lands as a real
  tool-call record, so shadow history reads as the shadow's own work.
  Anything that wouldn't map truthfully passes through verbatim; a call
  whose result never arrived is closed with a `(tool result not recorded)`
  placeholder at handoff, since both replay APIs reject dangling calls.
- **Attribution stays legible.** Every synced *text* message is tagged
  `[via claude-code]` / `[via codex]` (tandem's own notes use `[tandem]`),
  so interleaved histories make sense to you and to the models. Tool
  activity is untagged — it's mirrored as native records, not prose.
- **Errors are contained.** An entry that fails translation becomes a
  single per-turn placeholder in the shadow, with the raw entry
  quarantined under `~/.tandem/quarantine/…` — and sync continues. The
  shadow is never corrupted or truncated.
- **Memory files stay in step.** Fresh launches and every switch sync
  CLAUDE.md ↔ AGENTS.md: shared content lives in a
  `<!-- tandem:shared:begin/end -->` block (newer file wins),
  tool-specific text outside the block is preserved, and a file without
  markers is read from but never rewritten. Git state is never touched.

## Compatibility

Session formats are internal to the CLIs and drift between releases.
tandem pins what it was built against (observed formats documented in
[docs/formats.md](https://github.com/Bhavya6187/tandem/blob/main/docs/formats.md)):

| CLI | Tested | Accepted range |
| --- | --- | --- |
| Claude Code | 2.1.220 | ≥ 2.0, < 3 |
| Codex CLI | 0.145.0 | ≥ 0.140, < 0.150 |

Outside the range, tandem warns and asks you to run `tandem doctor`.
Format knowledge is isolated per tool in
`src/tandem/harness/claude_code.py` and `src/tandem/harness/codex.py`.

## Where your data lives

- `~/.tandem/state.db` — SQLite: session pairing + per-source sync cursors
  (override the directory with `TANDEM_HOME`)
- `~/.tandem/quarantine/<session>/` — raw entries that failed translation
- `~/.claude/projects/<munged-cwd>/<session-id>.jsonl` — claude transcript
- `~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<session-id>.jsonl` — codex
  rollout (`CLAUDE_CONFIG_DIR` / `CODEX_HOME` honored)

Claude session ids are minted by tandem (`claude --session-id`); codex
mints its own on first run and tandem captures it from the new rollout
file.

## Extending tandem

The sync engine talks to a small adapter interface
(`tandem.converter.TraceConverter`):

```python
class TraceConverter(Protocol):
    def translate_entry(entry, direction, ctx) -> list[TargetEntry] | TranslationError
```

`ReferenceConverter` implements it via a normalized event model
(`tandem/events.py`) derived from the observed formats. Pass your own
converter to `SyncEngine(store, session, source, converter=...)`.

## Development

```bash
uv sync && uv run pytest
pipx install .        # or: uv tool install .
```

Dependencies are deliberately small: `click` (CLI), `pydantic` v2 (event
schema), `watchdog` (transcript tailing), `pexpect`/ptyprocess (PTY
passthrough); state is stdlib `sqlite3`.

## License

[MIT](https://github.com/Bhavya6187/tandem/blob/main/LICENSE)
