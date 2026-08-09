# User-First README Restructure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restructure the repo's READMEs so a first-time visitor learns what tandem is → how to use it → why → how it works, with moved-out detail landing in `docs/why.md` and `plugin/README.md`'s internals section.

**Architecture:** Pure docs change, no code. Spec: `docs/specs/2026-08-09-readme-restructure-design.md`. Create `docs/why.md` first so the README's link to it never dangles, then rewrite `README.md`, then restructure `plugin/README.md`, then verify links + pytest.

**Tech Stack:** Markdown, git, grep, pytest (guard run only).

## Global Constraints

- Branch: `readme-restructure` (already created; spec committed as `baeee63`).
- `README.md` uses absolute URLs (`https://github.com/Bhavya6187/tandem/blob/main/...`, `https://raw.githubusercontent.com/Bhavya6187/tandem/main/...`) because it renders on PyPI. Files under `docs/` and `plugin/` use relative links.
- No prose rewrites of claims — moved content is today's text, lightly edited for its new home only.
- Why-section order everywhere: GPT subagents first, then usage-limit, then outages, then the rest.
- Do not touch `docs/subagents.md`, `docs/configuration.md`, `docs/how-it-works.md`, `docs/development.md`, `docs/formats.md`, or anything under `src/`, `plugin/agents/`, `plugin/hooks/`.

---

### Task 1: Create `docs/why.md`

**Files:**
- Create: `docs/why.md`

**Interfaces:**
- Produces: `docs/why.md` with the eight `##` sections below — Task 2's README links to it as `https://github.com/Bhavya6187/tandem/blob/main/docs/why.md`.

- [ ] **Step 1: Write the file** with exactly this content (the five sections leaving the README verbatim, plus full-length versions of the three that stay as README bullets):

````markdown
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
````

- [ ] **Step 2: Verify relative links resolve**

Run: `cd /Users/bhavya/git/tandem && ls docs/how-it-works.md docs/subagents.md README.md`
Expected: all three listed (they are the targets of why.md's relative links).

- [ ] **Step 3: Commit**

```bash
git add docs/why.md
git commit -m "docs: add why.md — full eight-reason pitch, moved from the README"
```

---

### Task 2: Rewrite `README.md`

**Files:**
- Modify: `README.md` (full replacement)

**Interfaces:**
- Consumes: `docs/why.md` from Task 1 (linked absolutely).
- Produces: README with section order hero → Quick start → Why tandem? → How it works → Command cheat sheet → Docs → License.

- [ ] **Step 1: Replace the entire file** with exactly this content:

````markdown
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
````

- [ ] **Step 2: Verify order, length, and that no content was silently lost**

Run: `grep -n '^## \|^### ' README.md && wc -l README.md`
Expected: headings in this exact order — Quick start, Why tandem?, the three `###` bullets (GPT subagents, usage limit, outages), How it works, Command cheat sheet, Docs, License; length ≈120 lines.

Run: `grep -c 'Why tandem' docs/why.md README.md`
Expected: both files nonzero (README links out; why.md exists).

Cross-check the five moved sections exist in `docs/why.md`: `grep -c '^## ' docs/why.md` → `8`.

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs: user-first README — quick start above a three-bullet pitch, GPT subagents first"
```

---

### Task 3: Restructure `plugin/README.md`

**Files:**
- Modify: `plugin/README.md`

**Interfaces:**
- Consumes: nothing from earlier tasks (relative links point at pre-existing `docs/` files).
- Produces: user intro on top; today's maintainer content under `## Internals` with its two `##` headings demoted to `###`; stale README pointer replaced with `../docs/configuration.md` + `../docs/subagents.md`.

- [ ] **Step 1: Replace the file's opening** (everything before `## What's in here`) with:

````markdown
# tandem's Claude Code plugin

Lets Claude Code dispatch subagents to GPT models: in a tandem-paired
session, "ask gpt to review this migration" runs the task on a local
codex worker and the result comes back like any subagent's.

```bash
tandem plugin install   # = claude plugin marketplace add Bhavya6187/tandem
                        #   + claude plugin install tandem@tandem
```

Setup and usage: the [GPT subagents guide](../docs/subagents.md). The
plugin is static registration only — all the work happens in the
`tandem` binary the hook shells out to (`uv tool install tandem-cli`).
Without that binary on PATH the plugin is inert: the hook command exits
nonzero, `|| true` swallows it, and every dispatch runs natively.

## Internals

Hacking on the plugin itself? Point Claude at your clone instead:
`claude --plugin-dir /path/to/tandem/plugin`. Either way, hooks and agents
register when a Claude session starts, so install or update first, then
open a fresh session.
````

- [ ] **Step 2: Demote the two section headings** (content under them unchanged): `## What's in here` → `### What's in here`, `## What the hook does` → `### What the hook does`.

- [ ] **Step 3: Fix the stale config pointer.** Replace the file's final sentence —

> Configuration lives in `~/.tandem/config.toml` under `[subagents]`, and routing needs no config: dispatches reach codex when you ask for the `tandem:gpt` agent, and `route = "all"` is the opt-in that reroutes every dispatch automatically. See the repo [README](../README.md) for the keys — `model` first, since an unset one puts every worker on your codex account's default — and the sandbox/consent story.

— with:

> Configuration lives in `~/.tandem/config.toml` under `[subagents]`, and routing needs no config: dispatches reach codex when you ask for the `tandem:gpt` agent, and `route = "all"` is the opt-in that reroutes every dispatch automatically. The keys are in the [configuration reference](../docs/configuration.md) — `model` first, since an unset one puts every worker on your codex account's default — and the sandbox/consent story is in the [GPT subagents guide](../docs/subagents.md).

- [ ] **Step 4: Verify structure**

Run: `grep -n '^#\|^##\|^###' plugin/README.md`
Expected: `# tandem's Claude Code plugin`, `## Internals`, `### What's in here`, `### What the hook does` — in that order, and no remaining link to `../README.md` for config keys: `grep -c '(\.\./README\.md)' plugin/README.md` → `0`.

- [ ] **Step 5: Commit**

```bash
git add plugin/README.md
git commit -m "docs: plugin README leads with the user story; internals sectioned; config pointer fixed"
```

---

### Task 4: Repo-wide link check + guard tests

**Files:**
- Modify: none expected (fix-ups only if the check finds a dangling link).

**Interfaces:**
- Consumes: all three prior tasks' files.

- [ ] **Step 1: Find every intra-repo markdown link and confirm targets exist**

Run from repo root:

```bash
grep -rnoE '\]\((\.\./)?(docs/|plugin/)?[A-Za-z0-9._-]+\.md' README.md docs/*.md plugin/README.md | sort -u
```

For each hit, confirm the target file exists relative to the linking file (`docs/*.md` links resolve against `docs/`; `plugin/README.md` links against `plugin/`). Expected: zero dangling targets. Also confirm no file still links to a README anchor that no longer exists: `grep -rn 'README.md#' docs/ plugin/` → no output.

- [ ] **Step 2: Run the test suite** (guards against fixtures referencing README text; no code changed, so this must pass)

Run: `uv run pytest`
Expected: all tests pass, zero failures.

- [ ] **Step 3: Fix anything found, amend the owning commit, re-run both checks.** If nothing found, no commit — the branch is complete at three commits past the spec.
