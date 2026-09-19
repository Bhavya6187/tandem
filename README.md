<div align="center">

# 🤝 tandem

**One coding session. Multiple AI agents. Shared context.**

Use [Claude Code](https://docs.anthropic.com/en/docs/claude-code),
[OpenAI Codex CLI](https://github.com/openai/codex), and
[opencode](https://opencode.ai) in one continuous coding session. Work in
their native interfaces, press **Ctrl-]** to move to the next CLI, and
continue without copying prompts or rebuilding context by hand.

[![CI](https://github.com/Bhavya6187/tandem/actions/workflows/ci.yml/badge.svg)](https://github.com/Bhavya6187/tandem/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/tandem-cli)](https://pypi.org/project/tandem-cli/)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://github.com/Bhavya6187/tandem/blob/main/pyproject.toml)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](https://github.com/Bhavya6187/tandem/blob/main/LICENSE)

![tandem demo — one session relayed across Claude Code, Codex and opencode](https://raw.githubusercontent.com/Bhavya6187/tandem/main/docs/demo.gif)

</div>

## Quick start

You need Python 3.11+ and at least two supported CLIs installed and signed
in: [Claude Code](https://docs.anthropic.com/en/docs/claude-code),
[Codex CLI](https://github.com/openai/codex), or
[opencode](https://opencode.ai).

```bash
uv tool install tandem-cli   # or: pip install tandem-cli
cd your-project
tandem
```

`tandem` starts a fresh chat: one composer for every CLI. Prompts run on
the last-used harness; start one with `/claude`, `/codex`, or `/opencode` to
run it there and make that the default. `tandem --on codex` chooses the
harness for the first prompt.

Sessions have their own IDs, so you can keep multiple conversations in the
same project. Resume from any directory; the session uses its saved working
directory, conversation, last-used harness, and model pins:

```bash
tandem resume        # choose from sessions across all directories
tandem resume <id>   # reopen a specific session
tandem --continue    # reopen the most recently used session across directories
tandem sessions      # list recent session IDs
```

Prefer the CLIs' own interfaces? Use the native frame:

```bash
tandem native              # pair a fresh session inside the first usable CLI's own TUI
tandem native resume       # re-enter the latest session in this directory
tandem native resume <id>  # re-enter a specific session from its directory
```

Work normally in the CLI that opens. Press **Ctrl-]** to continue the same
session in the next CLI. If you press it while the model is working, tandem
waits until the response finishes; press it again to cancel the switch.
`tandem native` opens the first usable CLI in your configured order; use
`tandem native --active codex` or `--active opencode` to choose another.

## What's new in 0.5

**Rate-limit windows on the tab bar.** Every slot now shows its account's
rate limits — percent used per window, labeled by window length — so you
can see which subscription has room *before* you press Ctrl-]. The active
slot also carries the model's live context and the session's input↑ /
output↓ token totals:

```text
 claude ● 144k ctx · 7.6M↑ 312k↓ · 5h 4% 7d 41% │ codex ○ 7d 12% │ opencode ○   ^] flips
```

The figures come from the same account endpoints that `claude`'s `/usage`
and `codex`'s `/status` call, using the credentials those CLIs already keep
on disk, refreshed every minute and after each response. They are cosmetic
by design: a failed fetch blanks the figure and never touches sync, and
API-key logins and opencode show none. These polls are tandem's only
network calls — set `rate_limits = false` under `[frame]` in
`~/.tandem/config.toml` to turn them off. See
[Configuration](https://github.com/Bhavya6187/tandem/blob/main/docs/configuration.md)
for the status bar's other settings.

**0.5.1 keeps up with the current CLIs.** claude 2.1.26x and codex 0.153
changed their session records, and codex now holds a writer lock on every
open thread. On those versions 0.5.0 failed `tandem doctor` and every flip
into claude on the new metadata records, warned that codex was out of range
at each start, and could lose a claude → codex flip to `already has an
active writer`. All three are fixed, and codex's list-shaped tool outputs
now reach the other side as text with their exit codes intact.

**0.5.2 stops claude complaining about synced turns.** claude 2.1.261
restores a session's model on resume from its last assistant entry, and a
claude session that had never run a turn of its own (claude → codex →
opencode → claude) opened with `Session model <synced> could not be
restored … using fable instead`. Synced entries now carry claude's own
`<synthetic>` tag until claude has run, which its resume skips; the synced
turns still render and still reach the model.

## Why tandem?

- **Keep going when a model hits its limit.** The tab bar shows each
  account's rate-limit windows; move to another CLI and continue with the
  same files and conversation history.
- **Bring different models to the same problem.** Ask another CLI for a
  second opinion without copying a wall of context between terminals.
- **Keep the native tools you already use.** Claude Code, Codex, and
  opencode retain their own interfaces, commands, keybindings, and MCP
  servers.
- **Work through a single-provider outage.** If another configured CLI is
  available, switch and continue until the affected provider recovers.

Want GPT subagents inside Claude Code too? See the
[GPT subagents guide](https://github.com/Bhavya6187/tandem/blob/main/docs/subagents.md).
For more use cases, see
[Why tandem?](https://github.com/Bhavya6187/tandem/blob/main/docs/why.md).

## How it works

```text
┌──────────────────────────────────────────────┐
│ Work normally in a native coding CLI         │
└──────────────────────┬───────────────────────┘
                       ▼
┌──────────────────────────────────────────────┐
│ tandem keeps the other sessions in sync      │
│ locally — without calling another model      │
└──────────────────────┬───────────────────────┘
                       ▼
┌──────────────────────────────────────────────┐
│ Press Ctrl-] and continue in the next CLI    │
└──────────────────────────────────────────────┘
```

Only the active model runs. tandem translates its growing conversation into
the other CLIs' native session formats using local file access, so they are
ready when you switch. Your session stays in the CLIs' own storage plus a
small local database in `~/.tandem`; tandem adds no cloud sync or telemetry
(its only network calls are the optional rate-limit polls for the tab bar).

See [How tandem works](https://github.com/Bhavya6187/tandem/blob/main/docs/how-it-works.md)
for transcript translation, switching, crash safety, compatibility, and data
locations.

## Everyday commands

| Command | What it does |
| --- | --- |
| `tandem` | Start a fresh chat; `/claude`, `/codex`, `/opencode` route and stick. `--on codex` picks the first harness; `--new` is an explicit spelling of the default |
| `tandem resume [id]` | Resume a chat by ID from anywhere, or choose from all sessions when no ID is given |
| `tandem --continue` / `-c` | Continue the most recently used session across directories |
| `tandem --skip-permissions` | Open the session with claude's and codex's permission prompts off, for this launch only (also on `resume`, `native`, `native resume`; `skip_permissions = true` in the config makes it the default) |
| `tandem native` | Pair a fresh session inside the CLIs' native interfaces (`--active codex` picks the starting CLI) |
| `Ctrl-]` | In the native frame: continue in the next CLI |
| `tandem native resume [id]` | Re-enter the latest or a specific session in this directory in the native frame |
| `tandem sessions [-n N]` | List recent sessions across directories |
| `tandem run --on codex "…"` | Send one contextual prompt to another CLI (`claude`, `codex`, or `opencode`) |

## Learn more

- [Why tandem?](https://github.com/Bhavya6187/tandem/blob/main/docs/why.md) —
  use cases, subscriptions, native tools, token visibility, and privacy
- [GPT subagents](https://github.com/Bhavya6187/tandem/blob/main/docs/subagents.md) —
  plugin setup, worker models, routing, and sandboxing
- [Configuration](https://github.com/Bhavya6187/tandem/blob/main/docs/configuration.md) —
  participants, startup arguments, the switch key, and the status bar
- [How tandem works](https://github.com/Bhavya6187/tandem/blob/main/docs/how-it-works.md) —
  synchronization, switching, compatibility, and local data
- [Developing tandem](https://github.com/Bhavya6187/tandem/blob/main/docs/development.md) —
  development setup and the harness adapter interface
- [Observed session formats](https://github.com/Bhavya6187/tandem/blob/main/docs/formats.md) —
  Claude Code, Codex, and opencode storage formats

## License

[MIT](https://github.com/Bhavya6187/tandem/blob/main/LICENSE)
