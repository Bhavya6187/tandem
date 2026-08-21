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
 claude ● 144k ctx · 7.6M↑ 312k↓ · 5h 4% 7d 41% │ codex ○ 7d 12% │ opencode ○ │ mixed ○   ^] flips
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

## The mixed tab

**Ctrl-]** cycles through your CLIs and then one more stop: **mixed**. The
mixed tab shows a real CLI — whichever one it is focused on — and adds one
thing. A prompt that starts with an `@target` runs that turn somewhere else:

```text
@codex rewrite this migration to be idempotent
@haiku what does this regex do?
@codex:gpt-5.6-luna review the diff for races
```

Three forms are recognized, and only as the prompt's first token:

| Form | Example | What it does |
| --- | --- | --- |
| `@<cli>` | `@claude`, `@codex`, `@opencode` | Runs the turn there, on the model that CLI is already set to |
| `@<model>` | `@opus`, `@haiku`, `@gpt-5.6-luna` | Picks the CLI that owns the model and starts it on that model |
| `@<cli>:<model>` | `@codex:gpt-5.6-luna`, `@opencode:anthropic/claude-sonnet-5` | The explicit form, for names the short ones can't reach |

Claude models are named by family alias (`opus`, `sonnet`, `haiku`, `fable`)
or by full slug (`claude-sonnet-5`). Codex model names resolve against the
catalog Codex keeps in `~/.codex/models_cache.json`, so `@gpt-5.6-luna` and
its display name both work. opencode wants provider-qualified names, which
only the explicit form can carry.

Staying put is free: a prompt with no prefix — or one naming the CLI you are
already in — runs right there, natively. An `@token` that names nothing
routable is left alone, so `@src/foo.py` and `@CLAUDE.md` are still file
mentions. The other tabs are unchanged: `@` means nothing to tandem outside
the mixed tab.

A routed turn does not run where you typed it. That CLI answers with one
line naming where it went instead:

```text
tandem: → codex · gpt-5.6-luna — running there
```

tandem then flips to Codex — relaunching it with that model pinned — pastes
the prompt into its composer, and the turn runs there natively. The reply
syncs back to the other CLIs as usual, so the next turn can go anywhere. A
routed flip always starts the target fresh, so it costs a full start-up
rather than the pipelined one behind a plain **Ctrl-]**.

The model rides that launch, so it is not a one-turn setting: the CLI stays
on it for as long as that process lives — until you flip away and back, or
route to it again naming something else. A CLI that can't take a model on
its command line can't be pinned at all, and tandem says so on exit rather
than answering on a model you didn't pick.

The tab bar carries the tab you are in. In the harness tabs the mixed tab
waits at the end of the row as an idle `mixed ○` slot. Inside it, that slot
is the active one — it holds the focus CLI's name and stats, and the harness
slots go quiet, keeping only the rate limits that decide where to route next:

```text
 claude ○ │ codex ○ 7d 12% │ opencode ○ │ mixed ● claude · 144k ctx · 7.6M↑ 312k↓ · 5h 4% 7d 41%   ^] flips
```

Intercepting a prompt needs tandem's prompt hook inside the CLI you type
into. Claude Code and Codex (0.145+) have one, and `tandem plugin install`
registers tandem with both; opencode has none. Where tandem can't intercept,
it does not pretend to: the prompt runs in the focus CLI as it always would,
`@codex` and all, as literal text the model reads. The bar says so up front,
so the prefix is never swallowed without warning:

```text
 opencode ○ │ claude ○ 5h 4% 7d 41% │ codex ○ 7d 12% │ mixed ● opencode (no @-routing)   ^] flips
```

`tandem doctor` reports which CLIs are set up. Routing *to* a CLI always
works — only routing *from* one needs the hook.

If a routed prompt doesn't make it — the target never got ready, the flip
landed elsewhere, or you cancelled the flip it was riding — tandem says so
on exit and quotes the prompt back in full, so you can re-type it where you
meant it to go. Nothing is dropped silently.

The mixed tab remembers the CLI it last showed. The first time you enter it,
it adopts the one you came from, and `tandem resume` restores both the tab
and its focus. Choosing the target is manual today; a router that proposes
one is the next step, and the `@` prefix stays the way to overrule it.

Don't want the fourth stop? Set `mixed = false` under `[frame]` in
`~/.tandem/config.toml` and **Ctrl-]** cycles through your CLIs only.

## Why tandem?

- **Keep going when a model hits its limit.** The tab bar shows each
  account's rate-limit windows; move to another CLI and continue with the
  same files and conversation history.
- **Bring different models to the same problem.** Ask another CLI for a
  second opinion without copying a wall of context between terminals.
- **Send a single turn to the CLI that suits it.** In the mixed tab,
  `@codex …` or `@haiku …` runs that one turn there and syncs the reply
  back — same session, same files, same history.
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
| `Ctrl-]` | Continue in the next CLI, then in the mixed tab |
| `tandem resume [id]` | Resume the latest or a specific session in this directory |
| `tandem sessions [-n N]` | List recent sessions across directories |
| `tandem run --on codex "…"` | Send one contextual prompt to another CLI (`claude`, `codex`, or `opencode`) |

## Learn more

- [Why tandem?](https://github.com/Bhavya6187/tandem/blob/main/docs/why.md) —
  use cases, subscriptions, native tools, token visibility, and privacy
- [GPT subagents](https://github.com/Bhavya6187/tandem/blob/main/docs/subagents.md) —
  plugin setup, worker models, routing, and sandboxing
- [Configuration](https://github.com/Bhavya6187/tandem/blob/main/docs/configuration.md) —
  participants, startup arguments, the switch key, the status bar, and the
  mixed tab
- [How tandem works](https://github.com/Bhavya6187/tandem/blob/main/docs/how-it-works.md) —
  synchronization, switching, compatibility, and local data
- [Developing tandem](https://github.com/Bhavya6187/tandem/blob/main/docs/development.md) —
  development setup and the harness adapter interface
- [Observed session formats](https://github.com/Bhavya6187/tandem/blob/main/docs/formats.md) —
  Claude Code, Codex, and opencode storage formats

## License

[MIT](https://github.com/Bhavya6187/tandem/blob/main/LICENSE)
