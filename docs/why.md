# Why tandem?

The full pitch — every reason the [README](../README.md) doesn't have
room for. Quick start and commands live there; the mechanics live in
[How tandem works](how-it-works.md).

## 🐣 Other model families, inside Claude Code

Claude Code dispatches subagents on Claude models only. tandem's plugin
lifts that limit: say "ask gpt to review this migration" and the task
runs on a local codex worker instead, with the brief forwarded verbatim
and the result returned through Claude's own machinery. Name a model to
pin one, or set `route = "all"` to reroute every dispatch. Claude
orchestrates; codex does the legwork; your Claude quota stays on the
main thread. Setup and routing modes:
[GPT subagents guide](subagents.md).

## ⏳ Hit a usage limit? Just keep going.

Claude runs out of its usage window mid-refactor? Type `switch` and Codex
continues the **same conversation** a second later — same files, same
history, same plan. Your two subscriptions become one long runway instead
of two separate walls.

## 🌩️ Immune to outages.

An Anthropic or OpenAI outage doesn't stop your work. If the active
model's API goes down, `switch` — the same session continues seamlessly
in the other harness, and you can switch back whenever the outage clears.

## 💳 Subscriptions, not API bills.

tandem wraps the official CLIs under the auth you already have — your
Claude and ChatGPT subscription logins work as-is. No API keys to
provision, no per-token surprises. tandem itself makes **zero network
calls**; every model call happens inside the real CLI, on your existing
plan.

## 🧠 Two model families on one problem.

Claude and GPT have different strengths and different blind spots. Get a
second opinion **with full session context** — no copy-pasting walls of
text between terminals:

```bash
tandem run --on codex "second opinion: why is this test flaky?"
```

## 🏠 Every model in its native harness.

This is not a lowest-common-denominator wrapper UI. Claude runs in real
Claude Code; GPT runs in real Codex CLI. Your keybindings, slash commands,
MCP servers, and muscle memory all work exactly as they do today — tandem
sits underneath, not in between.

## ⚡ Switching is instant.

While one agent is active, tandem quietly keeps the other one's native
session file up to date by translating the transcript as it grows — pure
local file I/O, no model calls, no "exporting…" step. The other side is
*always* resume-ready.

## 🔒 Local, private, no lock-in.

Everything lives in the CLIs' own session files plus a small SQLite
database in `~/.tandem`. No cloud sync, no telemetry. Uninstall tandem
tomorrow and both sessions still resume natively with `claude --resume`
and `codex resume`.
