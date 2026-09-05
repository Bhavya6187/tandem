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

Work normally in the CLI that opens. Press **Ctrl-]** to continue the same
session in the next CLI. If you press it while the model is working, tandem
waits until the response finishes; press it again to cancel the switch.

Exit normally when you're done. Come back later with:

```bash
tandem resume      # resume the latest session in this directory
tandem sessions    # find recent sessions across directories
```

By default, tandem opens the first usable CLI in your configured order. Use
`tandem --active codex` or `tandem --active opencode` to choose another.

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
| `tandem` | Start a new session |
| `Ctrl-]` | Continue in the next CLI |
| `tandem resume [id]` | Resume the latest or a specific session in this directory |
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
