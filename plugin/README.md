# tandem's Claude Code plugin

Lets Claude Code dispatch subagents to GPT models. In a tandem-paired
session, "ask gpt to review this migration" runs the task on a local codex
worker — with the brief forwarded verbatim — and the result comes back
like any subagent's. Name a model ("ask sol to review it") to pin the
worker to it; set `route = "all"` to reroute every dispatch without
asking. Claude orchestrates, codex does the legwork, and your Claude
quota stays on the main thread. The worker is always codex, so the `codex`
CLI must be installed and signed in — whether the paired session is
claude+codex, all three with opencode, or even claude+opencode (a
task-only worker starts cold; only `context = "full"` forks the session's
codex history, which exists when codex is a participant).

```bash
tandem plugin install   # = claude plugin marketplace add Bhavya6187/tandem
                        #   + claude plugin install tandem@tandem
```

The first `tandem` launch offers this install too. Setup and usage: the
[GPT subagents guide](../docs/subagents.md); config keys (`model`, `route`,
`context`, `keep_forks`) in the
[configuration reference](../docs/configuration.md). The plugin is static
registration only — all the work happens in the `tandem` binary the hook
shells out to (`uv tool install tandem-cli`). Without that binary on PATH
the plugin is inert: the hook command exits nonzero, `|| true` swallows it,
and every dispatch runs natively. Flipping between harnesses with Ctrl-]
does not need the plugin at all; it exists only for GPT subagents.

## Internals

Hacking on the plugin itself? Point Claude at your clone instead:
`claude --plugin-dir /path/to/tandem/plugin`. Either way, hooks and agents
register when a Claude session starts, so install or update first, then
open a fresh session.

### What's in here

- **`.claude-plugin/plugin.json`** — manifest. `name` is `tandem`, and it
  is load-bearing: Claude resolves a plugin's agents as
  `<plugin-name>:<agent-name>`, so the hook's rewrite target is
  `tandem:codex-worker` (the bare name does not resolve). `version` is
  hand-maintained in lockstep with `pyproject.toml` — a drift-guard test
  fails if they disagree — because `claude plugin marketplace update` skips
  a plugin whose resolved version already matches the installed one, so a
  stale version means shipped fixes never reach users.
- **`hooks/hooks.json`** — `PreToolUse`, matcher `Agent|Task`, command
  `tandem hook-route || true`. The `|| true` is load-bearing: click's
  usage-error path exits 2 (version skew — plugin installed, an older
  `tandem` on PATH with no `hook-route` subcommand), and exit 2 *blocks* the
  dispatch. `hook-route` itself always exits 0 and prints nothing when in
  doubt, so its failure mode is "dispatch natively".
- **`agents/codex-worker.md`** — the bridge the hook rewrites dispatches
  to. `model: haiku`, `tools: Bash(tandem sub:*)`. It runs exactly one
  command — `tandem sub -q` with the brief on stdin via heredoc — and
  returns that command's stdout as its final message, verbatim; the body's
  first rule is that it never does the task itself. Its description tells
  the orchestrating model not to select it by hand.
- **`agents/gpt.md`** — the same relay, with a user-facing description
  ("Runs the task on a GPT model via tandem's codex pairing"). This is the
  agent to select when you ask for GPT subagents by name, and under the
  `route = "manual"` default it is the normal path to codex — nothing is
  rerouted for you. A brief whose first line is `tandem-model: <name>`
  picks the worker's model: the description asks the orchestrating model to
  emit that line (one space-free token) whenever you name a model, and
  `tandem sub` resolves the name against codex's own catalog. The body is
  identical to `codex-worker.md`; only the description differs, so both
  agents behave the same once dispatched.

Both relay names are in the hook's loop guard, matched on the last segment
of the agent name in any scope — a dispatch that already names a relay
(`gpt`, `tandem:gpt`, `codex-worker`, `tandem:codex-worker`) is passed
through untouched instead of being rewritten into itself.

### What the hook does

For each `Agent`/`Task` dispatch, in any directory that has a paired tandem
session, it:

- records the dispatching session's write consent (`acceptEdits` /
  `bypassPermissions` → `--sandbox workspace-write`, every other mode →
  codex's own default) under `$TANDEM_HOME/sandbox/<tandem-id>`, where the
  relay's `tandem sub` reads it. Stamped whatever the route setting is: a
  hand-picked `tandem:gpt` dispatch consents through your permission mode
  just the same;
- with `route = "all"` (opt-in; the default is `"manual"`) and a supported
  codex, rewrites the dispatch to `tandem:codex-worker` on `haiku`,
  prefixing the dispatched agent's own definition — when it is a
  file-defined one, from `.claude/agents/` here or under `~/.claude/` — to
  the brief, so the codex worker gets the same instructions the native
  agent would have;
- with `route = "manual"` (the default) or `"off"`, rewrites nothing;
  explicitly selected relay agents still reach codex.

When `route = "all"` but nothing can be rerouted — no paired session for
this directory, or codex missing/unsupported — it prints a one-time
`systemMessage` naming the cause (once per Claude session) and the dispatch
runs natively.

Configuration lives in `~/.tandem/config.toml` under `[subagents]`, and
routing needs no config: dispatches reach codex when you ask for the
`tandem:gpt` agent, and `route = "all"` is the opt-in that reroutes every
dispatch automatically. The keys are in the
[configuration reference](../docs/configuration.md) — `model` first, since
an unset one puts every worker on your codex account's default — and the
sandbox/consent story is in the [GPT subagents guide](../docs/subagents.md).
