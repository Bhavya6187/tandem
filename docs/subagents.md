# GPT subagents

Dispatch delegated tasks from Claude Code to a GPT model. Basic setup
first; the full configuration and routing reference after. (Back to the
[README](../README.md).)

Load tandem's Claude Code plugin and GPT subagents are one ask away: "ask
gpt to review this migration" runs the dispatch on a codex model instead of
Claude's, with the task brief forwarded verbatim. Name a model — "ask
sol to review it" — and the worker runs on exactly that one. Want
every dispatch rerouted without asking each time? Set `route = "all"`.
Either way the result comes back through Claude's own machinery. Claude
orchestrates; codex does the legwork; your Claude quota stays on the
main thread. Two pieces: the `tandem` binary the hook shells out to, and
the plugin that registers the hook — the plugin lives in this repo, not in
the wheel, and without the binary on PATH it is inert.

## Basic setup

No config file needed: routing is manual by default. All the basics need
is the `codex` CLI installed and signed in, plus tandem and its plugin:

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

That's it. In any tandem-paired Claude session, ask: "ask gpt to review
this migration", or pin a model by name: "ask sol to review it". The
worker runs on your codex account's default model, read-only, and its
answer comes back like any subagent's. Everything below is optional
tuning.

The marketplace tracks this repo's default branch, but nothing updates
behind your back: the first-launch offer asks before touching anything, and
you pull new versions when you run `claude plugin marketplace update` (or
`/plugin marketplace update` inside Claude).

## Advanced

### Pick the worker model

Create `~/.tandem/config.toml` and pick your plan's cheap model as the
worker default — the ids your account can actually use are listed in
`~/.codex/models_cache.json`, the same catalog a per-dispatch model request
resolves against:

```toml
[subagents]
model = "gpt-5.6-luna"  # ← the whole point: set this
route = "manual"        # manual | all | off
context = "match"       # match | task | full
keep_forks = false      # keep each worker's rollout for debugging
```

**Without `model`, workers run on your codex account's default model —
probably not the cheap one.** Every other key above is already the default;
that one is not, and an explicitly asked-for gpt subagent bills that
default just as automatic rerouting would, so `tandem doctor` warns until
you set it.

### Routing modes

- `route = "manual"` (the default) keeps dispatches on Claude until you ask
  for codex — "use gpt subagents for this", "ask codex to review the
  migration". Name a model in the ask and the request rides along as a
  `tandem-model:` first line in the brief, which tandem resolves against
  your codex install's own catalog before codex is ever invoked; say the
  name however you say it out loud, since matching ignores case and
  punctuation. A name that resolves to nothing fails fast, listing the
  slugs your account actually offers, while a generic one ("gpt", "codex")
  names no model at all and just runs your `model` above — or your codex
  account's default, reported as `codex default`. A reply from a
  model-pinned dispatch ends with a `[tandem-sub model: …]` trailer naming
  what ran. The pin is also stashed out-of-band at dispatch time (keyed by
  a hash of the brief body, expiring after an hour), so `tandem sub`
  recovers it even when the relay's echo of the brief loses the header
  line — the trailer tells you either way. Mechanism and rationale:
  [model-pin recovery design](specs/2026-08-09-model-pin-recovery-design.md).
  (`route = "off"` is the same routing silence, but `manual` keeps `tandem
  doctor`'s subagent checks on, since you still send work to codex.)
- `route = "all"` reroutes every native subagent dispatch to codex
  automatically, no asking. It's all or nothing, though: under `all`, "have
  Claude and GPT both review this" comes back as codex twice — `manual` is
  the mode where mix-and-match works.

### Write access and the sandbox

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

## Day to day

Fork dispatches stay on Claude even under `route = "all"`, each worker's
full codex log is kept under `~/.tandem/subagents/`, and `tandem status`
lists the workers running right now. The plugin is installed for every
Claude session, but nothing reaches codex from a directory with no paired
tandem session: dispatches run natively (under `route = "all"` the first
one says so once, then the session stays quiet), and a hand-picked gpt
subagent there comes back telling you to run `tandem` in that directory
first. `claude plugin uninstall
tandem@tandem` and Claude is stock again — add `claude plugin marketplace
remove tandem` to also unregister the marketplace.

Hacking on the plugin itself? Skip the marketplace and point Claude at your
clone: `claude --plugin-dir /path/to/tandem/plugin`.

The worker is always codex, so the `codex` CLI must be installed and
signed in, but codex needn't be one of the session's participants: the
default task-only worker starts cold from a fresh seed, so a
claude+opencode pair dispatches just fine. Only `context = "full"` forks
the session's codex shadow, and that history exists when codex is a
participant.

Startup flags for the harnesses themselves (`[claude]` / `[codex]` /
`[opencode]` args) are not a subagent setting; they live in the
[configuration reference](configuration.md).
