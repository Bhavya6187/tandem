# README restructure — user-first docs

**Date:** 2026-08-09
**Status:** approved design, pending implementation

## Goal

A first-time visitor should learn, in order: what tandem is, how to use
it (minimal setup), why they'd want it, and how it works in simple
terms — without scrolling through release banners or an eight-section
pitch. Detail that serves returning users or maintainers moves to the
right file elsewhere in the repo instead of being deleted.

## Decisions (user-confirmed)

1. Root README shape: **short pitch + user-first** — quick start moves
   above the pitch; the pitch shrinks to three bullets; the remaining
   five sections move to a new `docs/why.md`.
2. `plugin/README.md`: **user pointer on top, internals below** — a
   short user-facing intro, then today's maintainer content unchanged
   under an `## Internals` heading.
3. The "New in 0.2 — GPT subagents" banner: **folded into the Why
   section** as a 🆕-marked bullet (with the gpt-subagent gif), and that
   bullet is **first** in the list. The README opens with the hero.

## File-by-file

### `README.md` (~110 lines, down from 175; also the PyPI page)

Order: what it is → how to use it → why → how it works → reference.

- **Hero** — centered title + tagline ("One coding session. Two AI
  agents. Zero lost context."), then three plain sentences: run Claude
  Code and Codex CLI as one paired session; work in either, `switch`
  at any moment and continue the same conversation; only one model runs
  per turn while the other stays in sync through local file
  translation. Badges, `uv tool install tandem-cli`, demo gif. This is
  today's hero minus the banner above it.
- **Quick start** — current section moved up, near-verbatim:
  prerequisites (Python 3.11+, `claude` + `codex` on PATH), install,
  `tandem`, the `switch` / `exit` / `resume` walkthrough.
- **Why tandem** — three bullets, 2–3 lines each, in this order:
  1. 🆕 **0.2: GPT subagents** — "ask gpt to review this migration"
     runs the dispatch on a local codex worker; result returns through
     Claude's own machinery. gpt-subagent gif here; link to
     `docs/subagents.md`.
  2. **Hit a usage limit? `switch`.** Codex continues the same
     conversation; two subscriptions become one runway.
  3. **Outage-immune.** Either provider down → `switch`, keep working,
     switch back later.

  Closing line: "Five more reasons (native harnesses, instant
  switching, privacy…): [Why tandem?](docs/why.md)".
- **How it works, in five lines** — new plain-English summary: one
  model per turn, never both; the idle side's session file is kept
  current by translating the transcript as it grows (pure local file
  I/O, no model calls); each CLI is the real CLI, untouched —
  keybindings, slash commands, MCP servers all work; everything is
  local (`~/.tandem` plus the CLIs' own session files); uninstall
  tandem and both sessions still resume natively. Link to
  `docs/how-it-works.md`.
- **Command cheat sheet** — kept as a table; the `tandem sub` row
  drops its plugin-internals parenthetical (that detail lives in the
  subagents guide). The maintenance-commands paragraph stays.
- **Docs + License** — current link list plus `docs/why.md`.

Constraint: README keeps absolute
`https://github.com/Bhavya6187/tandem/blob/main/...` /
`raw.githubusercontent.com` URLs because it renders on PyPI; files
under `docs/` keep relative links.

### `docs/why.md` (new)

Standalone full pitch, eight sections: full-length versions of the
three README bullets plus the five sections moving out of the README —
subscriptions not API bills; two model families on one problem
(`tandem run --on codex …`); every model in its native harness;
switching is instant; local, private, no lock-in. Opens with a
one-line framing and a link back to the README. Content is today's
"Why tandem?" prose, lightly edited for the new home — no rewrites of
claims.

### `plugin/README.md`

Top: ~8 user-facing lines — the plugin lets Claude Code dispatch
subagents to GPT models via tandem; install with
`tandem plugin install` (or the two `claude plugin` commands); setup
and usage live in `docs/subagents.md`; without the `tandem` binary on
PATH the plugin is inert. Then `## Internals`, containing today's
content unchanged.

Fix while there: the final paragraph currently points readers to "the
repo README" for the `[subagents]` config keys — stale since PR #32
moved those to `docs/configuration.md`. Point it at
`../docs/configuration.md` (and the subagents guide for semantics).

### Unchanged

`docs/subagents.md`, `docs/configuration.md`, `docs/how-it-works.md`,
`docs/development.md`, `docs/formats.md`. They already hold the split
detail (PRs #30–32) and nothing new moves into them.

## Verification

- Grep every tracked `.md` for links to `README.md` anchors and moved
  sections; confirm each resolves after the restructure.
- Render check: README section order matches the design; total length
  ≈110 lines; no content lost — every removed README paragraph exists
  in `docs/why.md` or was an intentional cut (the `tandem sub`
  parenthetical, the 0.2 banner framing).
- `uv run pytest` still passes (no code changes expected; guards
  against accidental fixture references to README text).

## Out of scope

Rewriting `docs/subagents.md` or `docs/how-it-works.md` prose; any
plugin/manifest/code changes; release/version bumps.
