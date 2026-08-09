<div align="center">

# 🤝 tandem

**One coding session. Two AI agents. Zero lost context.**

Run [Claude Code](https://docs.anthropic.com/en/docs/claude-code) and
[OpenAI Codex CLI](https://github.com/openai/codex) as a single paired
session — each model in its own native harness. Work in either one,
switch at any moment, and pick up exactly where you left off. Only one
model runs per turn; the other stays in sync through pure local file
translation.

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

## Quick start

You'll need Python 3.11+ and the `claude` and `codex` CLIs on your PATH.

```bash
uv tool install tandem-cli   # or: pip install tandem-cli
cd your-project
tandem                       # fresh paired session; drops you into claude
```

Work normally. When you exit the agent, you land at tandem's prompt
instead of your shell:

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

## Why tandem?

### 🆕 0.2 — GPT subagents inside Claude Code

Type "get the code reviewed by gpt" and tandem's plugin hands the task
to a local codex worker; the verdict lands back in your Claude session,
and your Claude quota stays on the main thread. Pin any model your
codex account offers ("ask sol to review it") or set a cheap default
once. Setup and routing:
[GPT subagents guide](https://github.com/Bhavya6187/tandem/blob/main/docs/subagents.md).

![gpt subagent demo — ask for a GPT review in Claude Code, tandem dispatches a codex worker, the verdict comes back, fixes get committed](https://raw.githubusercontent.com/Bhavya6187/tandem/main/docs/gpt-subagent.gif)

### ⏳ Hit a usage limit? Just keep going.

Claude runs out of its usage window mid-refactor? Type `switch` and
Codex continues the **same conversation** a second later — same files,
same history, same plan. Two subscriptions become one long runway.

### 🌩️ Immune to outages.

An Anthropic or OpenAI outage doesn't stop your work: `switch`, keep
going in the other harness, and switch back whenever it clears.

Five more reasons — subscriptions not API bills, two model families on
one problem, native harnesses, instant switching, privacy:
[Why tandem?](https://github.com/Bhavya6187/tandem/blob/main/docs/why.md)

## How it works

- Only one model runs per turn — the other side is never invoked to
  "catch up".
- As you work, tandem translates the growing transcript into the other
  CLI's native session format — pure local file I/O, no model calls —
  so the other side is always resume-ready.
- Each CLI is the real thing running on a PTY: your keybindings, slash
  commands, and MCP servers all work exactly as they do today.
- Everything stays local: the CLIs' own session files plus a small
  SQLite database in `~/.tandem`. No cloud sync, no telemetry.
- Uninstall tandem tomorrow and both sessions still resume natively
  with `claude --resume` and `codex resume`.

Full mechanics — sync engine, crash safety, compatibility ranges:
[How tandem works](https://github.com/Bhavya6187/tandem/blob/main/docs/how-it-works.md).

## Command cheat sheet

| Command | What it does |
| --- | --- |
| `tandem` | Start a fresh paired session (Claude active; `--active codex` to flip) |
| `switch` | Flip active/shadow and enter the other agent — at the tandem prompt, or one-shot from your shell (one-shot only flips, it doesn't enter) |
| `tandem resume [id]` | Continue the most recent (or a specific) session |
| `tandem run --on codex "…"` | One-off prompt to the *other* agent, with full context |
| `tandem sub "…"` | Run one delegated task on a codex model (what GPT subagents use under the hood) |
| `tandem status` | Show pairing, roles, and sync position |
| `tandem plugin install` | Install the Claude Code plugin (also offered on first `tandem` launch) |

Three maintenance commands round it out: `tandem doctor` (health
check), `tandem sync` (manual catch-up translation), and `tandem
sync-mcp` (share MCP server configs between the tools).

## Docs

- [GPT subagents](https://github.com/Bhavya6187/tandem/blob/main/docs/subagents.md) —
  plugin install, worker model, routing modes, sandbox & trust boundary
- [Why tandem?](https://github.com/Bhavya6187/tandem/blob/main/docs/why.md) —
  the full pitch, all eight reasons
- [Configuration](https://github.com/Bhavya6187/tandem/blob/main/docs/configuration.md) —
  the optional `~/.tandem/config.toml`: subagent workers, per-harness
  startup args
- [How tandem works](https://github.com/Bhavya6187/tandem/blob/main/docs/how-it-works.md) —
  sync engine, PTY passthrough, compatibility, where your data lives
- [Developing tandem](https://github.com/Bhavya6187/tandem/blob/main/docs/development.md) —
  dev setup and the converter adapter interface
- [Observed session formats](https://github.com/Bhavya6187/tandem/blob/main/docs/formats.md) —
  the claude/codex transcript details tandem is pinned against

## License

[MIT](https://github.com/Bhavya6187/tandem/blob/main/LICENSE)
