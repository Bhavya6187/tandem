## 🆕 New in 0.2 — GPT subagents

Ask for a second opinion without leaving Claude Code. Type "get the code
reviewed by gpt" like you'd ask for anything else — tandem's plugin
dispatches a codex worker with the full task brief, GPT's verdict lands
back in your Claude session, and Claude applies the fixes. Name any model
your codex account offers ("ask gpt-5.4-mini to review it"), or set a
cheap default once and let every dispatch ride on it: Claude
orchestrates, codex does the legwork, and your Claude quota stays on the
main thread.

![gpt subagent demo — ask for a GPT review in Claude Code, tandem dispatches a codex worker, the verdict comes back, fixes get committed](https://raw.githubusercontent.com/Bhavya6187/tandem/main/docs/gpt-subagent.gif)

Setup and the full routing story: [GPT subagents guide](https://github.com/Bhavya6187/tandem/blob/main/docs/subagents.md).

---

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

Load tandem's Claude Code plugin and GPT subagents are one ask away: "ask
gpt to review this migration" runs the dispatch on a codex model instead
of Claude's, with the task brief forwarded verbatim. Name a model to pin
one, or set `route = "all"` to reroute every dispatch without asking.
Claude orchestrates; codex does the legwork; your Claude quota stays on
the main thread.

```bash
tandem plugin install
```

Config reference, routing modes, and the sandbox rules:
[GPT subagents guide](https://github.com/Bhavya6187/tandem/blob/main/docs/subagents.md).

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

## Docs

- [GPT subagents — install, config & routing](https://github.com/Bhavya6187/tandem/blob/main/docs/subagents.md) —
  plugin install, `config.toml` reference, routing modes, sandbox & trust
  boundary
- [How tandem works](https://github.com/Bhavya6187/tandem/blob/main/docs/how-it-works.md) — sync engine, PTY
  passthrough, compatibility ranges, where your data lives
- [Developing tandem](https://github.com/Bhavya6187/tandem/blob/main/docs/development.md) — dev setup and the
  converter adapter interface
- [Observed session formats](https://github.com/Bhavya6187/tandem/blob/main/docs/formats.md) — the claude/codex
  transcript details tandem is pinned against

## License

[MIT](https://github.com/Bhavya6187/tandem/blob/main/LICENSE)
