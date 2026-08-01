# Codex-model subagents — design

2026-07-31. Status: approved design, pre-implementation.

## Goal

When Claude Code is the active harness in a tandem pair, native subagent
dispatches transparently execute on a cheap Codex model instead of a Claude
model. Routing policy lives in tandem configuration, not in prompts or agent
descriptions. Both harnesses stay stock: uninstall the plugin and Claude is
byte-for-byte native again.

Non-goals for v1: rerouting fork-type dispatches, tier/matcher routing
policies, Monitor-based streaming, a batch orchestrator, any MCP surface, and
the reverse direction (codex-active dispatching Claude minis). See "Later"
at the end.

## Why this shape

Claude Code's extension surface (verified against docs and live traces,
claude 2.1.220):

- The `Agent` tool (renamed from `Task` in v2.1.63) dispatches **one tool
  call per subagent** — parallel fanout is several independent calls, each
  with its own id, prompt, and result.
- A PreToolUse hook may return `allow` + `updatedInput`, replacing the
  entire input object before the subagent spawns. This is the interception
  seam.
- **No hook can fabricate a successful tool result.** The architecture is
  therefore *redirect the dispatch, don't forge the return*: the actual
  return travels through Claude's native machinery.
- A named (non-fork) subagent starts cold: its context is the delegation
  prompt Claude writes, the agent definition's body as system prompt,
  CLAUDE.md hierarchy (except Explore/Plan), and a git-status snapshot.
  Claude composes self-contained briefs *because* it knows workers are cold.
- A `fork`-type subagent (experimental; default-enabled ≥2.1.161) inherits
  the entire conversation. It is identified by `subagent_type == "fork"` in
  the dispatch input.
- Subagents run in the background by default (≥2.1.198): the Agent tool
  result is launch metadata, and the real result arrives later as a
  `<task-notification>` user-type transcript entry with the full result
  text inline.

Codex CLI facts (verified live, codex 0.145): `codex exec -m <model>`
selects a model per invocation; `codex exec resume <id>` resumes a rollout,
including rollouts tandem authors from scratch; billing follows the CLI's
auth (ChatGPT subscription), not interactivity.

## Runtime actors

Only two things act at runtime: the stock harnesses and the tandem binary.
The plugin is a distribution vehicle — static files registered into Claude,
never a process.

```
user ──▶ claude (native UI; native Agent dispatch, one call per subagent)
             │  PreToolUse (plugin-registered) runs: tandem hook-route
             │  → allow + updatedInput {subagent_type: codex-worker,
             │     model: <cheap claude driver>, prompt: brief}
             ▼
         codex-worker bridge (haiku driver, Bash-only, plugin-shipped)
             │  runs: tandem sub -m <mini>   (brief on stdin)
             ▼
         tandem: match-mode → seed empty rollout, `codex exec resume` in cwd
                 full-mode  → drain, fork shadow rollout, `codex exec resume`
             ▼
         codex model works in the stock codex harness (may spawn_agent deeper)
             ▼
         final message → bridge returns it verbatim → native return path
         (foreground: tool result; background: task-notification, result inline)
             ▼
         tandem's normal sync mirrors the dispatch + result into the shadow
```

Management is layered and native at every level: Claude orchestrates the
fleet (scheduling, background tasks, panel UI, depth budget, aggregation);
tandem executes exactly one worker per `tandem sub` invocation and holds no
fleet state; codex manages its own interior fanout.

## Components

### `tandem sub` (new op + CLI command)

`tandem sub [-m MODEL] [--context task|full] [-q] [TASK]` — task read from
stdin when not passed as an argument (Claude briefs are multi-paragraph;
stdin avoids argv quoting and length limits). With no `--context` flag the
config policy decides: `match` resolves to `task`, because every rerouted
dispatch in v1 is fresh-type (forks pass through natively).

- **Invariant: the brief is forwarded verbatim — no truncation, no
  summarization, at any step.**
- `--context task` (what match-mode resolves to for non-fork dispatches):
  no fork, no drain, no lock — seed a minimal rollout (fresh uuid7,
  `originator: "tandem-sub"`, `model_provider: "openai"`, one note line and
  no history) and run `codex exec -m <model> --skip-git-repo-check resume
  <seed-id>` with the brief, in the session cwd. The worker still starts
  cold; the seed exists so tandem *authors* the rollout rather than letting
  codex mint one. A plain `codex exec` would write an ordinary rollout in
  the session cwd — non-tandem originator, fresh mtime — which is exactly
  what `await_codex_rollout` treats as "codex just minted a session": a
  concurrent fresh-codex launch or one-off would bind the pair's
  `codex_session_id` to a throwaway worker transcript that `tandem sub`
  then deletes. Seeding puts cold runs behind the same originator guard as
  forks, so no sub rollout is ever adoptable. Project rules reach the worker
  the native way: codex reads the AGENTS.md tandem already keeps synced.
- `--context full`: drain the active source with `flush_dangling` (same
  machinery as `run_oneoff`), copy the shadow rollout to a fresh uuid7
  rollout in codex's sessions dir — rewrite `session_meta` id, set
  `originator: "tandem-sub"`, keep `model_provider: "openai"` — then
  `codex exec resume <fork-id> -m <model>` with the brief. The fork is
  **never registered as a sync source**: no cursor, no echo-suppression
  changes, shadow untouched.
- Fanout: pass the codex feature flag enabling multi-agent tools when
  `allow_fanout = true` (flag name pinned by spike S2), so the worker may
  `spawn_agent` natively at its own discretion.
- Output: codex exec's streamed activity passes through to stdout as it
  runs; the final message is printed last. Exit code mirrors codex exec.
- `-q/--quiet` (what the bridge agent uses): the raw transcript is
  redirected to `~/.tandem/subagents/<tandem-id>/logs/<run-id>.log` and the
  command's **entire stdout is the worker's final message**, captured via
  codex's own `-o/--output-last-message` into `<run-id>.last`. Without this
  the bridge's Bash output is the whole exec log — header, actions, token
  counts — and "return the final message verbatim" is not something a
  haiku-tier relay can do reliably; quiet mode removes the extraction step
  rather than trusting it. If the `.last` file is missing or empty (codex
  died first), the log's tail is printed instead, so a failed relay still
  carries the error text. Both files are retained as the debugging trail.
  Non-quiet behavior is unchanged: stdio is inherited and manual runs
  stream live.
- Cleanup: delete the worker's rollout (fork or seed) on completion.
  `keep_forks = true` retains it under `~/.tandem/subagents/<tandem-id>/`
  for debugging (and as the future resume path for follow-up messages to a
  finished worker).
- If codex is missing, unsupported, or the cwd has no paired session:
  exit nonzero with a one-line reason (the bridge relays it).

### `tandem hook-route` (new CLI command)

Reads the PreToolUse JSON from stdin. Emits nothing (exit 0) — meaning
"dispatch proceeds natively" — when any of these hold:

- `tool_name` is not `Agent`/`Task` (defense in depth: the hook matcher is
  config we do not control at call time);
- no paired tandem session for the cwd, or codex missing/unsupported
  (the plugin is installed globally in Claude; outside tandem sessions the
  hook must be an invisible no-op);
- `route = "off"` in config;
- `subagent_type` is `"fork"` (claude forks stay native in v1 — they're
  prompt-cache-cheap and semantically claude's own full-context worker);
- `subagent_type` is already `codex-worker` (loop guard).

Otherwise emit `allow` + `updatedInput`. `updatedInput` replaces the whole
input object, so it carries every original field with three rewrites:

1. `subagent_type` → `codex-worker`.
2. `model` → the cheap claude driver (`haiku`). **Load-bearing:** real
   traces show dispatches carry `model: "opus"`, and the per-invocation
   model param overrides agent frontmatter — without this rewrite the
   bridge itself would run on opus.
3. `prompt` → the original brief, prefixed with the named agent's
   definition body when `subagent_type` matched a file in `.claude/agents/`
   (walking up from cwd) or `~/.claude/agents/` — that body is the system
   prompt Claude's own worker would have received. Built-in types
   (Explore, general-purpose, Plan, …) forward the brief alone, as do
   plugin-scoped types (ids containing `:`) in v1.
   `description` is UI chrome Claude's own workers never see; it is left
   as-is and otherwise ignored.

Exit-code discipline: catch everything; **never exit 2** (exit 2 blocks
the dispatch). Any internal failure → exit 0 with no output, i.e. native
dispatch. The failure mode of this whole feature is "no savings", never
"broken subagents".

The command body cannot enforce this alone: click's usage-error path exits
2 *before* the body runs — the realistic case is version skew, where the
plugin is installed but an older `tandem` on PATH has no `hook-route`
subcommand, which would then block every dispatch in that session. The hook
is therefore registered as `tandem hook-route || true`; the shell-level
guard is what makes exit 2 unreachable. `route()` additionally re-checks
`tool_name ∈ {Agent, Task}` itself, so a mis-scoped matcher can never make
it rewrite an unrelated tool's input.

### The plugin (`plugin/` in this repo)

- `agents/codex-worker.md` — frontmatter: `model: haiku`,
  `tools: Bash(tandem sub:*)`. Body: run `tandem sub -q` with the brief on
  stdin and a generous Bash timeout (codex runs can be long; background
  subagents keep Bash, so backgrounding is safe); the command's entire
  output IS the final message, so return it verbatim — no summarizing, no
  commentary; on nonzero exit, return the output prefixed
  `[tandem-sub failed]` and stop. The heredoc delimiter is chosen per
  dispatch: `TANDEM_TASK_EOF` unless that string occurs in the brief, else
  the same with random digits appended. A fixed delimiter is a shell
  injection hazard — a brief containing that line (this repo's own docs do)
  would end the heredoc early and run the remainder as shell.
- `hooks/hooks.json` — PreToolUse, matcher `Agent|Task` (defensive: the
  alias guarantee covers settings/agent definitions, not hook matchers
  explicitly), command `tandem hook-route || true`. The `|| true` is
  load-bearing, not cosmetic: click's usage-error path exits 2 outside the
  command body (version skew — plugin installed, older tandem on PATH
  lacking the subcommand), and exit 2 blocks the dispatch.
- Install: local plugin from the repo for v1 (marketplace later).
  Registration is the plugin's entire job; policy lives in tandem config.

### Config (`~/.tandem/config.toml`, new file; `TANDEM_HOME` honored)

```toml
[subagents]
route = "all"          # all | off        (later: matchers, route_forks)
model = "…"            # cheap codex model id; default pinned by spike S1
context = "match"      # match | task | full
allow_fanout = true    # codex-native spawn_agent inside workers
keep_forks = false
```

`context = "match"` is the core policy: mirror the context decision Claude
itself made per dispatch. Fresh-type dispatch → task-only (Claude wrote a
self-contained brief for a cold worker; dumping history on a small model
distracts it and burns subscription). Fork-type dispatch → full (that's the
contract a fork asks for — only reachable in v1 by forcing `context =
"full"`, since forks pass through natively).

## Return path and sync coherence

- Foreground dispatch: the bridge's final message is the Agent tool result.
- Background dispatch (the default): the tool result is launch metadata;
  the result text arrives inline in a `<task-notification>` user-type
  entry. Verified in real traces (full multi-KB results embedded). The
  notification's `<output-file>` path points into session tmp and may not
  outlive the session; harmless, the text is inline.
- Nothing is ever appended to a live Claude's context by tandem: a running
  claude holds its conversation in memory and does not re-read its
  transcript. Both return channels above are Claude-native.
- Shadow sync needs no new machinery: `Agent` call/result pairs are not in
  toolmap's mapping tables, so they ride the existing Tier-2 passthrough;
  task-notification entries sync as user messages; subagent internals never
  appear in the parent transcript (verified: zero sidechain entries in
  current-format traces); forks are not sync sources.

## Progress visibility

- Claude's native task panel lists every bridge as a running subagent —
  fleet liveness for free.
- Bridge runs no longer stream into the tool view: the bridge passes `-q`,
  so codex's activity goes to a log file and only the final message reaches
  stdout. That is the deliberate trade for a relay that cannot garble the
  result. Manual `tandem sub` (no `-q`) still streams live in the terminal,
  and the per-run logs under `~/.tandem/subagents/<tandem-id>/logs/` are the
  debugging trail for what a worker actually did.
- `tandem status` (run from a second terminal, or `! tandem status` inside
  claude, which costs a little context) grows a section listing active
  workers: model, cold/fork, rollout path, retained forks.
- The documented streaming channel — a plugin-declared Monitor tailing the
  fork rollout — is v1.1.
- The main model sees only the final result, by design.

## Billing and auth

All codex invocations go through the CLI's stored auth: ChatGPT
subscription, consistent with tandem's zero-API-key stance. `tandem sub`
must not inject API-key env; `tandem doctor` warns when codex auth is
key-based or `OPENAI_API_KEY` is set (either could flip billing to API).
The bridge costs a few hundred haiku-tier relay tokens per dispatch — the
price of "no forged results" under the documented hook surface.

## Error handling

| Failure | Behavior |
| --- | --- |
| hook-route crashes / config unreadable | exit 0, no output → native dispatch |
| no paired session / codex missing | hook passthrough; if reached anyway, `tandem sub` exits nonzero, bridge relays `[tandem-sub failed]`, main model does the work itself |
| codex exec nonzero / outage | bridge relays output + `[tandem-sub failed]`; main model retries or does the work natively |
| shadow missing on `--context full` | seed a fresh rollout from the drained active side (existing `_create_codex_shadow_late` pattern) |
| parallel dispatches, full mode | file lock serializes drain-then-copy; execs then run concurrently. Match mode touches no shared state — no contention |
| dangling calls at fork time | `flush_dangling` closes them (both replay APIs reject dangles) |

## Known v1 limitations (stated, accepted)

- CLAUDE.md content **outside** the `tandem:shared` block reaches Claude's
  own workers but never codex workers (AGENTS.md sync carries only the
  shared block). `tandem doctor` nudges users to move subagent-relevant
  rules into the shared block.
- Named agents' `skills:` preloads are not forwarded (definition body is).
- Rerouted workers are not resumable via SendMessage the way Claude
  subagents are; `keep_forks` + `codex exec resume` is the v2 path.
- Thinking blocks do not cross translation (dropped, as elsewhere in
  tandem).
- Fork-type dispatches stay on Claude (`route_forks` is a later option).

## Spikes (before or during implementation)

- **S1**: enumerate cheap codex model ids available under this ChatGPT
  plan; confirm `codex exec resume <fork> -m <model>` honors the override
  on a tandem-authored rollout (fresh uuid7 should dodge the per-thread
  model cache in `state_5.sqlite` — verify).
- **S2**: codex multi-agent feature flag name for `--enable`; whether
  spawned workers start cold (expected) — informs `allow_fanout` wiring.
- **S3**: confirm the `Agent|Task` hook matcher fires on claude 2.1.220,
  and the exact `updatedInput` echo behavior on an Agent call.
- **S4**: haiku bridge relays long results verbatim without embellishment
  (prompt-tune the agent body if not).

## Testing

- Unit: `hook-route` stdin→stdout fixtures (rewrite, all four passthrough
  cases, model-field rewrite, named-agent body inlining, crash→exit-0);
  fork op golden tests (session_meta rewrite, fork never in sync cursors);
  `tandem sub` argv/stdin handling via the existing `_run` seam; config
  parsing.
- Integration: golden claude transcript containing background Agent
  dispatch + task-notification → shadow sync unchanged and complete.
- Live E2E (manual, this repo): dispatch a real subagent under the plugin,
  observe reroute, codex execution, result return, `switch`, and both-side
  resume; `tandem doctor` green throughout.

## Later

Tier/matcher routing (`route` grows match rules on agent type and model
tier), `route_forks`, Monitor streaming, skill-preload forwarding, worker
resume via retained forks, batch orchestrator (one codex orchestrator per
fanout batch — only if S2 shows spawned workers can inherit context),
MCP front-end, reverse direction (codex-active → claude minis; codex has
no hook seam today, so model-chooses only).
