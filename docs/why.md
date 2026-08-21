# Why tandem?

The full pitch — every reason the [README](../README.md) doesn't have
room for. Quick start and commands live there; the mechanics live in
[How tandem works](how-it-works.md).

## 🖥️ One CLI, three harnesses, zero ceremony.

`tandem` is the terminal you live in. It fronts the native Claude Code,
Codex CLI, or opencode interface, adding one status row and the **Ctrl-]**
switch shortcut. Move the session to the next one and keep the same
conversation, files, and history.

## ⏳ Hit a usage limit? Just keep going.

Claude runs out of its usage window mid-refactor? Press **Ctrl-]** and
continue the **same conversation** in Codex when it is ready — same files,
same history, same plan. Your subscriptions become one long runway
instead of separate walls.

## 🌩️ Keep working through a provider outage.

If the active model's provider goes down and another configured CLI is
available, press **Ctrl-]** and continue the same session there. Switch
back after the outage clears.

## 🐣 Other model families, inside Claude Code

Claude Code dispatches subagents on Claude models only. tandem's plugin
lifts that limit: say "ask gpt to review this migration" and the task
runs on a local codex worker instead, with the brief forwarded verbatim
and the result returned through Claude's own machinery. Name a model to
pin one, or set `route = "all"` to reroute every dispatch. Claude
orchestrates; codex does the legwork; your Claude quota stays on the
main thread. Setup and routing modes:
[GPT subagents guide](subagents.md).

## 💳 Use your existing authentication and billing.

tandem uses the authentication and billing already configured in each CLI.
It adds no API keys, model charges, or billing layer of its own. Every model
call happens inside the real CLI; tandem itself makes **zero network
calls**.

## 🧠 Every model on one problem.

Claude, GPT, and whatever opencode is pointed at have different
strengths and different blind spots. Get a second opinion **with full
session context** — no copy-pasting walls of text between terminals:

```bash
tandem run --on codex "second opinion: why is this test flaky?"
tandem run --on opencode "review the diff and list anything risky"
```

## 🏠 Every model in its native harness.

This is not a lowest-common-denominator wrapper UI. Claude runs in real
Claude Code; GPT runs in real Codex CLI; opencode runs in real opencode.
Your keybindings, slash commands, MCP servers, and muscle memory all
work exactly as they do today — tandem sits underneath, not in between.

## ⚡ Switch without a manual handoff.

While one agent is active, tandem quietly keeps every other one's native
session file up to date by translating the transcript as it grows — pure
local file I/O, no model calls, no "exporting…" step. The other sessions
stay ready to resume, and a flip pipelines the handover: the
incoming harness boots while the outgoing one is still shutting down
(flips *into* opencode run serially, since its TUI must open after the
last turn has landed in its database).

## 🧮 Know what you're spending.

The tab bar's active slot shows the model's live context (`144k ctx`, or
`43% ctx` where the harness reports its window) and the session's
input↑ / output↓ token totals — read straight from the transcript,
updated as you work. Every slot also shows its account's rate-limit
windows (percent used), so you can see which subscription has room
before you flip:

```
 claude ● 144k ctx · 7.6M↑ 312k↓ · 5h 4% 7d 41% │ codex ○ 7d 12% │ opencode ○ │ mixed ○   ^] flips
```

## 🔒 Local, private, no lock-in.

Everything lives in the CLIs' own session files plus a small SQLite
database in `~/.tandem`. No cloud sync, no telemetry — the only network
calls tandem makes are the rate-limit polls above, to the same account
endpoints the CLIs' own `/usage` and `/status` use, and `[frame]
rate_limits = false` turns those off. Uninstall tandem
tomorrow and every session still resumes natively with `claude --resume`,
`codex resume`, and `opencode -s`.
