# Codex-model subagents — design

2026-07-31. Status: implemented; spikes S1–S4 and the live E2E run against
claude 2.1.220 / codex 0.145.0 on 2026-07-31 (results recorded inline).

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
             │  → allow + updatedInput {subagent_type: tandem:codex-worker,
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
  summarization, at any step.** It also never touches argv: the brief is
  untrusted text, and as a trailing positional codex's own parser reads a
  leading `-` as a flag (`- review …` dies with `unexpected argument`
  before the model runs; `-m gpt-9 …` is silently honored, i.e. argv
  injection). tandem passes codex's documented stdin marker — `resume <id>
  -` — and writes the brief to the child's stdin, in both quiet and
  streaming modes. This also removes the argv length ceiling on
  multi-paragraph briefs.
- `--context task` (what match-mode resolves to for non-fork dispatches):
  no fork, no drain, no lock — seed a minimal rollout (fresh uuid7,
  `originator: "tandem-sub"`, `model_provider: "openai"`, one note line and
  no history) and run `codex exec -m <model> --skip-git-repo-check resume
  <seed-id> -` with the brief on stdin, in the session cwd. The worker
  still starts cold; the seed exists so tandem *authors* the rollout rather
  than letting codex mint one. A plain `codex exec` would write an ordinary
  rollout in the session cwd — non-tandem originator, fresh mtime — which
  is exactly what `await_codex_rollout` treats as "codex just minted a
  session": a concurrent fresh-codex launch or one-off would bind the pair's
  `codex_session_id` to a throwaway worker transcript that `tandem sub`
  then deletes. Seeding puts cold runs behind the same originator guard as
  forks, so no sub rollout is ever adoptable. (`codex exec --ephemeral`
  ["run without persisting session files to disk"] would also keep the cold
  path out of discovery's way, and was rejected: it leaves nothing on disk,
  so `keep_forks` has no worker transcript to retain and the debugging trail
  — the one way to see what a cheap worker actually did — disappears. A
  seeded rollout costs one small file and keeps both.) Project rules reach
  the worker the native way: codex reads the AGENTS.md tandem already keeps
  synced.
- `--context full`: drain the active source with `flush_dangling` (same
  machinery as `run_oneoff`), copy the shadow rollout to a fresh uuid7
  rollout in codex's sessions dir — rewrite `session_meta` id, set
  `originator: "tandem-sub"`, keep `model_provider: "openai"` — then
  `codex exec -m <model> resume <fork-id> -` with the brief on stdin. The
  fork is **never registered as a sync source**: no cursor, no
  echo-suppression changes, shadow untouched.
- Fanout: pass `--enable <fanout_feature>` when configured. On codex 0.145
  nothing needs passing (S2): the `multi_agent` feature is stable and on by
  default, so the worker may `spawn_agent` natively at its own discretion,
  and its children inherit its (cheap) model unless it overrides them.
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

Reads the PreToolUse JSON from stdin. Emits no permission decision (exit 0)
— meaning "dispatch proceeds natively" — when any of these hold:

- `tool_name` is not `Agent`/`Task` (defense in depth: the hook matcher is
  config we do not control at call time);
- no paired tandem session for the cwd, or codex missing/unsupported
  (the plugin is installed globally in Claude; outside tandem sessions the
  hook must not touch the dispatch) — the one case that is not silent: it
  carries the one-time notice described under exit discipline below;
- `route = "off"` in config;
- `subagent_type` is `"fork"` (claude forks stay native in v1 — they're
  prompt-cache-cheap and semantically claude's own full-context worker);
- `subagent_type` is already the bridge (loop guard) — matched
  scope-insensitively (`…:codex-worker` or bare `codex-worker`), because
  the model asks for it both ways once it sees the name in an error.

Otherwise emit `allow` + `updatedInput`. `updatedInput` replaces the whole
input object, so it carries every original field with three rewrites:

1. `subagent_type` → `tandem:codex-worker`. **Plugin-scoped, load-bearing:**
   claude registers a plugin's agents as `<plugin-name>:<agent-name>` and
   rejects the bare name — live E2E (2.1.220, 2026-07-31) failed every
   dispatch with `Agent type 'codex-worker' not found. Available agents:
   …, tandem:codex-worker`. A test pins the rewrite target to
   `plugin.json`'s `name` + the agent file's `name:` so the two cannot drift.
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

**The one silence that gets a voice.** The plugin is installed globally, so
a dispatch in a directory with no paired session — or one where codex is
missing/unsupported — is indistinguishable from a dispatch on a machine
without tandem at all: nothing reroutes, nothing says why. When `route =
"all"` and an `Agent`/`Task` dispatch cannot be rerouted for either of
those reasons, hook-route therefore prints a bare top-level
`{"systemMessage": "…"}` naming the actual cause. `systemMessage` is the
documented universal hook-output field ("warning message shown to the
user", claude 2.1.220), and carrying it *without* a permission decision
leaves the call in claude's normal permission flow — the dispatch still
runs natively, exactly as if the hook had said nothing. Not a `deny`:
blocking a dispatch to explain a missing optimization would be worse than
the optimization's absence. It never accompanies a rewrite, and `route =
"off"` stays silent — that silence is what the user asked for.

Once per claude session, not per dispatch: the PreToolUse payload's
top-level `session_id` names a stamp file under `$TANDEM_HOME/warned/`,
written only after a notice was actually printed, pruned opportunistically
at ~7 days. The stamp is the CLI wrapper's business (the decision layer
stays pure: it is told `already_warned` and answers whether and what to
warn). Every bookkeeping failure — unwritable or occupied `$TANDEM_HOME`,
a payload with no `session_id` — degrades to warn-anyway rather than
stay-silent: a repeated line is a smaller loss than the one message that
explains otherwise-invisible behavior. Exit 0 on every path.

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
  would end the heredoc early and run the remainder as shell. The body
  opens with an unconditional **"you never do the task yourself"** rule
  (S4: without it, haiku answered a one-command task directly instead of
  delegating — correct answer, zero savings, silent policy failure).
- `hooks/hooks.json` — PreToolUse, matcher `Agent|Task` (defensive: the
  alias guarantee covers settings/agent definitions, not hook matchers
  explicitly), command `tandem hook-route || true`. The `|| true` is
  load-bearing, not cosmetic: click's usage-error path exits 2 outside the
  command body (version skew — plugin installed, older tandem on PATH
  lacking the subcommand), and exit 2 blocks the dispatch.
- Install: this repo doubles as its own plugin marketplace
  (`.claude-plugin/marketplace.json` at the root, marketplace name
  `tandem`, source `./plugin`), so the user story is two steps —
  `uv tool install tandem-cli` for the binary, then
  `claude plugin marketplace add Bhavya6187/tandem` +
  `claude plugin install tandem@tandem`. The marketplace tracks the repo's
  default branch, but third-party marketplaces do not auto-update: new
  versions land only when the user runs `claude plugin marketplace update`
  (and only if `plugin.json`'s `version` moved — a matching resolved
  version skips the update). `claude --plugin-dir /path/to/tandem/plugin`
  from a clone remains the local-development path. The wheel packages
  `src/tandem` only, so `pip install tandem-cli` alone never reroutes
  anything, and the plugin without `tandem` on PATH is inert; the README
  says both explicitly. Registration is the plugin's entire job; policy
  lives in tandem config.

### Config (`~/.tandem/config.toml`, new file; `TANDEM_HOME` honored)

```toml
[subagents]
route = "all"           # all | off        (later: matchers, route_forks)
model = "gpt-5.6-luna"  # cheap codex model id ("" = codex's own default)
context = "match"       # match | task | full
fanout_feature = ""     # codex --enable <name>; "" = don't pass the flag
keep_forks = false
```

`fanout_feature` replaces the planned `allow_fanout` boolean: S2 found no
flag to toggle on codex 0.145 (multi-agent is on by default), so a boolean
would have had nothing to switch. The string is the escape hatch for a
future codex that gates fanout behind a named feature — empty means "pass
no `--enable`", which is the correct value today. **Do not set it blindly:**
`codex exec --enable <unknown>` exits 1 with `Error: Unknown feature flag`
before the model is ever called, which would fail every worker.

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
| no paired session / codex missing | native dispatch + a one-time `systemMessage` notice naming the cause (no permission decision, once per claude session); if the bridge is reached anyway, `tandem sub` exits nonzero, bridge relays `[tandem-sub failed]`, main model does the work itself |
| notice stamp unwritable / payload has no `session_id` | notice repeats instead of being suppressed; exit 0, dispatch untouched |
| codex exec nonzero / outage | bridge relays output + `[tandem-sub failed]`; main model retries or does the work natively |
| shadow missing on `--context full` | seed a fresh rollout from the drained active side (existing `_create_codex_shadow_late` pattern) |
| parallel dispatches, full mode | file lock serializes drain-then-copy; execs then run concurrently. The other drainer is the *session's own tail thread*, running continuously against the same cursor in the `tandem run` process; it takes `_sub_lock` around each drain too, so both sides serialize on one flock. (Guarding only the `tandem sub` side left two concurrent drains of one cursor row free to translate the same lines twice — duplicate turns and call ids in the fork.) Match mode touches no shared state — no contention |
| dangling calls at fork time | `flush_dangling` closes them (both replay APIs reject dangles) |

## Known v1 limitations (stated, accepted)

- CLAUDE.md content **outside** the `tandem:shared` block reaches Claude's
  own workers but never codex workers (AGENTS.md sync carries only the
  shared block). `tandem doctor` nudges users to move subagent-relevant
  rules into the shared block.
- Named agents' `skills:` preloads are not forwarded (definition body is).
- A named agent's `tools:` restriction is silently dropped on reroute: the
  definition body is inlined into the brief, but the allow-list is not
  translated into any codex-side constraint, so a read-only Explore-style
  agent runs its codex worker under codex's own sandbox config — which may
  be write-capable. v2 direction: map a read-only `tools:` list to `codex
  exec -s read-only` (and refuse to reroute restrictions that have no codex
  equivalent).
- Rerouted workers are not resumable via SendMessage the way Claude
  subagents are; `keep_forks` + `codex exec resume` is the v2 path.
- Thinking blocks do not cross translation (dropped, as elsewhere in
  tandem).
- Fork-type dispatches stay on Claude (`route_forks` is a later option).

## Spikes — run live 2026-07-31 (claude 2.1.220, codex 0.145.0)

- **S1** — *cheap codex model ids, and whether `-m` survives a resume of a
  tandem-authored rollout.* The plan's slugs come from
  `~/.codex/models_cache.json`: `gpt-5.6-sol` (account default, frontier),
  `gpt-5.6-terra`, `gpt-5.6-luna` ("fast and affordable"), `gpt-5.5`,
  `gpt-5.4`, `gpt-5.4-mini` ("small, fast, cost-efficient"). `codex exec -m
  gpt-5.6-luna` and `-m gpt-5.4-mini` both answered; an unknown id fails
  loudly and distinctively — HTTP 400 `The '<id>' model is not supported
  when using Codex with a ChatGPT account.` **`-m` is honored on resume:**
  `tandem sub -m gpt-5.6-luna --context task` on a freshly seeded
  `originator: "tandem-sub"` rollout produced `turn_context.model =
  "gpt-5.6-luna"` and `thread_settings_applied.model = "gpt-5.6-luna"` in
  the retained rollout, while the account default stayed `gpt-5.6-sol` — no
  per-thread model cache interference. Shipped guidance: `model =
  "gpt-5.6-luna"` (`gpt-5.4-mini` for the cheapest tier). **Setting it is
  required for the cheap-model promise: without `model`, workers run on
  your codex account's default model — probably not the cheap one** (here,
  the frontier `gpt-5.6-sol`). The dataclass default stays `""` on purpose
  — a baked-in id would 400 on an account that lacks it — so the honesty
  lives in the docs and in a `tandem doctor` warning instead.
- **S2** — *fanout feature flag; do spawned workers start cold?* **There is
  no flag to pass on 0.145.** `codex features list` reports `multi_agent`
  as stage `stable`, effective `true` by default; `multi_agent_v2` is
  stable-but-off; `multi_agent_mode` and `enable_fanout` are stage
  `removed`. The tandem-seeded worker rollout recorded
  `turn_context.multi_agent_version = "v1"` with no flag passed, so workers
  can already `spawn_agent`. `fanout_feature` therefore ships empty and
  exists only for a future codex that gates this; a wrong value is fatal
  (`codex exec --enable <unknown>` → `Error: Unknown feature flag`, exit 1,
  before any model call). Spawned workers **do** start cold: the
  `multi_agent_v1` tool schema defines `fork_context` as "True forks the
  current thread history into the new agent; false or omitted starts with
  only the initial prompt", and children "inherit your current model by
  default" — so a cheap parent keeps its whole subtree cheap.
- **S3** — *does the `Agent|Task` matcher fire, and how is `updatedInput`
  echoed?* Fires on 2.1.220 (`hook_name: "PreToolUse:Agent"`), and the
  rewrite is applied: `task_started` carries `subagent_type:
  "tandem:codex-worker"`, the worker's sidechain entries carry
  `attributionAgent: "tandem:codex-worker"` / `attributionPlugin:
  "tandem"`, and it ran on `claude-haiku-4-5` — the `model` rewrite does
  override the dispatch's own model, as the design assumed. Echo behavior:
  the assistant's `tool_use` block in the transcript keeps the **original**
  input; the hook's stdout lands beside it as a `hook_success` attachment
  (and as a `hook_response` event under `--include-hook-events`). So the
  transcript shows the pre-rewrite dispatch — `task_started` is where the
  effective input is visible. **Defect found here:** rewriting to the bare
  `codex-worker` failed every dispatch (`Agent type 'codex-worker' not
  found. Available agents: …, tandem:codex-worker`); the rewrite target is
  now the plugin-scoped id (see hook-route above).
- **S4** — *does the haiku bridge relay verbatim?* Not with the original
  agent body: on a one-command task it answered directly with `find`
  instead of delegating. The body **was** in its system prompt (probed via
  `claude -p --agent tandem:codex-worker`) and `tools: Bash(tandem sub:*)`
  **did** narrow its toolset to Bash alone — but nothing forbade doing the
  work, so it did. Fixed by an unconditional "you never do the task
  yourself … `tandem sub` is the ONLY command you may ever run" rule at the
  top of the body. After the fix the bridge ran `tandem sub -q` with the
  brief on a heredoc and returned codex's final message **byte-for-byte
  identical** to the `-o/--output-last-message` file. Caveat: verified on a
  short result; the mechanism copies stdout, so long-result risk is model
  discipline, not extraction. Note `Bash(tandem sub:*)` constrains the
  *tool list*, not the *command* — a session that broadly allows `Bash`
  still lets the relay run anything, which is how the deviation happened.

## Testing

- Unit: `hook-route` stdin→stdout fixtures (rewrite, all passthrough
  cases, model-field rewrite, named-agent body inlining, crash→exit-0;
  notice emitted with no decision, suppressed on the second dispatch of a
  session, silent under `route = "off"`, codex-cause variant, warn-anyway
  on stamp failure — all exit 0);
  fork op golden tests (session_meta rewrite, fork never in sync cursors);
  `tandem sub` argv/stdin handling via the existing `_run` seam; config
  parsing.
- Integration: golden claude transcript containing background Agent
  dispatch + task-notification → shadow sync unchanged and complete.
- Live E2E — run 2026-07-31 headlessly (`claude -p … --plugin-dir <repo>/plugin
  --output-format stream-json --include-hook-events`, scratch project with a
  paired session under a scratch `TANDEM_HOME`). All of it verified in one
  run: hook fires and rewrites (`task_started.subagent_type =
  tandem:codex-worker`, worker on `claude-haiku-4-5`); bridge runs `tandem
  sub -q` with the brief on a heredoc; a `tandem-sub` rollout is created in
  the session cwd and the codex worker does the work on `gpt-5.6-luna`;
  `tandem status` shows `subagent running: gpt-5.6-luna (task) …` while it
  runs and the marker is gone after; the Agent tool result equals codex's
  final message byte-for-byte, and the parent repeats it verbatim; `tandem
  sync` lands the dispatch in the shadow as a native `function_call`
  `Agent` + `function_call_output` pair; after `tandem switch`, a real
  `codex exec resume <shadow>` answered a question about the subagent's
  result from synced context; `tandem doctor` green.
  Not reachable headlessly, left for manual interactive verification: the
  interactive task-panel view of running workers, and the background
  (`run_in_background: true`) return path — the foreground path was the one
  exercised.

## Later

Tier/matcher routing (`route` grows match rules on agent type and model
tier), `route_forks`, Monitor streaming, skill-preload forwarding, worker
resume via retained forks, batch orchestrator (one codex orchestrator per
fanout batch — only if S2 shows spawned workers can inherit context),
MCP front-end, reverse direction (codex-active → claude minis; codex has
no hook seam today, so model-chooses only).
