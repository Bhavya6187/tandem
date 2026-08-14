# Opencode as a third harness

**Date:** 2026-08-13
**Status:** Approved (brainstorm settled 2026-08-13; scope decisions by operator)

## Problem

Tandem pairs exactly two harnesses — claude code and codex — and the
pairing is a compile-time constant expressed six different ways (an
`other()` toggle, two id columns, two `Literal` unions, two-element
tuples, a two-slot status bar, a binary flip ladder). Opencode
(opencode.ai) should join as a third first-class harness: transcripts
synced both ways, a slot in the Ctrl-] cycle, launch/resume/one-off
support. Opencode also breaks a deeper assumption than arity: its
sessions live in **SQLite** (`opencode.db`, WAL mode), not an
append-only JSONL transcript, so "transcript = file of lines" must
become an abstraction.

## Scope decisions (operator calls)

- **Full peer harness** — sync + flip + launch, not a subagent-only or
  configurable-pair integration.
- **Ctrl-] cycles all participants** in order; no direct-jump keys, no
  active-pair-plus-parked model.
- **Subagent dispatch to opencode is deferred.** The subagent lane
  (`hookroute.py`, `plugin/`, `modelcat.py`, `pinstash.py`,
  `ops.run_sub`) shares no code with flip/sync and stays claude→codex.
- **No backward compatibility for existing tandem state.** The state
  schema changes below ship without migration shims; on schema-version
  mismatch the state DB is recreated. Old pairings lose only the
  tandem-side linkage — native transcripts in each harness survive.

## Non-goals (v1)

- Opencode subagent dispatch (`tandem:opencode` agent, model pinning).
- MCP-config sync for opencode (`tandem sync-mcp` stays claude↔codex).
- The experimental v2 storage (`session_message`/`session_input`
  tables) — version-gated out; v1 targets the v1 `message`/`part`
  tables only.
- A persistent opencode idle-hook plugin (fs-watch on the WAL file is
  the v1 wake-up signal; the plugin is the future fix if opencode-bound
  flips feel cold).

## Architecture overview

### Participants and resolution

A top-level `harnesses` config key holds an ordered list of intended
participants; omitted means "all supported" (`["claude", "codex",
"opencode"]`). At session creation tandem resolves **participants =
configured ∩ installed-and-version-supported**, persists the resolved
list on the session, and that single list drives the flip cycle, the
sync fan-out, and the status bar. Fewer than two usable harnesses is an
actionable error naming what's missing (replacing the hard both-binaries
check at `cli.py:585`).

**Invariant: not-installed is a normal state, not a degraded one.** The
only probe an uninstalled harness ever receives is the PATH lookup
during resolution. Excluded harnesses trigger no discovery, no DB open,
no shadow session, no cursors, no bar slot, no doctor probes, and no
warnings — `tandem` with opencode absent behaves byte-for-byte like
today's claude↔codex tandem. Absence is mentioned in exactly one place:
a one-line informational note in `tandem doctor`. Warnings are reserved
for **installed but unusable** (version below floor, DB unreachable,
schema drift). The rule is symmetric: codex uninstalled yields a silent
claude↔opencode tandem.

### Sync topology

Unchanged in principle, generalized in arity: the active harness is the
single source of truth; its tailer drains new entries into **N−1 shadow
engines** (one `SyncEngine` per `(source, target)` direction) instead of
one. With three participants each turn fans out to two shadows.

### The entry-store abstraction

The adapter contract's file-centric methods split into an entry-store
interface:

- `read_new(cursor) -> (entries, cursor)` — native entries appended
  since `cursor`, in native order.
- `append(entries)` — write rendered native entries.

Claude and codex implement it as JSONL file + byte offset (current
behavior, unchanged in substance). Opencode implements it as SQLite
rows keyed by `(time_created, id)`. The tailer keeps fs-watch as its
wake-up signal — for opencode it watches `opencode.db-wal`, which
changes on every write.

## N-harness generalization

Mapped by exploration 2026-08-12; the two schema changes come first
because everything else leans on them.

### State schema (`state.py`)

- `sessions` drops `claude_session_id`/`codex_session_id` for a child
  table `session_harnesses(tandem_id, harness, native_session_id)`, and
  gains `participants` (ordered JSON list). The
  `CHECK (active IN ('claude','codex'))` constraint goes.
- `sync_cursors` re-keys from `(tandem_id, source)` to
  `(tandem_id, source, target)`. The write-ahead `intent`,
  `pending_calls`, and leaf-chain state are per-direction facts; this
  re-key is what makes two-target fan-out crash-safe.
- No migration. Schema version bumps; mismatched DBs are recreated.

### Code model

- `other()` (`harness/__init__.py:15`) and `PairedSession.shadow`
  (`state.py:60`) are replaced by `Session.participants`,
  `next_active(current)` (cycle order = list order), and
  `targets_for(source)` (fan-out set). All six call sites move
  (`sync.py:56`, `converter.py:99`, `ops.py:226`, `ops.py:107`,
  `runner.py:550`, `runner.py:588`).
- `Direction` literals (`converter.py:33`, `events.py:92`) become plain
  validated `"<source>-><target>"` strings; the `Agent` literal
  (`events.py:15`) becomes `str` (six direction combinations don't
  enumerate well).
- `SessionContext` (`events.py:84-107`) loses its claude-prefixed
  fields: native ids move to `native_ids: dict[harness, id]`; renderer
  scratch (leaf uuid, run message id, resume model) moves to a
  per-adapter namespace `harness_state[harness_id]`. `ctx_from_cursor`/
  `ctx_to_cursor` (`runner.py:62-88`) persist the namespace blob.
- Name-branches become adapter dispatch: the sync engine obtains the
  shadow's entry store from the adapter, replacing the name-branched
  path lookup (`sync.py:61-75`; `transcript_path` remains only for
  file-backed harnesses and doctor display), claude leaf-repair via
  an optional adapter hook (`sync.py:109,178`), the status probe via a
  capability check (`runner.py:533`), codex rollout discovery behind
  the adapter (`runner.py:405-429,613-628`), entry-shape validation in
  doctor via a per-adapter method (`doctor.py:18-61`), late-shadow
  creation via adapter policy flags (`ops.py:111-147,202-219`).
- `toolmap` (`toolmap.py:49,63`) gets a target-keyed mapper registry
  with safe passthrough for unknown targets.
- Fixed two-tuples become participant loops (`cli.py:38,139,150`,
  `doctor.py:155,179,203,306-315`).

### Registries

`ATTRIBUTION` (`constants.py`), `COMPAT` (`compat.py`), and the memory
file map (`memory_sync.py:37`) gain opencode entries. Opencode reads
`AGENTS.md` natively, same as codex, so memory sync stays a two-file
CLAUDE.md ↔ AGENTS.md merge with opencode sharing codex's side.

## The opencode adapter

One module, `src/tandem/harness/opencode.py`, same one-file-per-harness
rule; SQLite helpers live inside it. All schema facts below verified
against opencode 1.18.15 (installed) / 1.18.17 (checkout at
`~/git/opencode`) and the live DB from the operator's first session.

### Storage facts

- DB: `~/.local/share/opencode/opencode.db`, WAL mode. Filename is
  channel-suffixed (`opencode-<channel>.db`) — discover via
  `opencode db path` once per process, fall back to the default path.
- Tables: `session` (one row per session; title, slug, directory,
  version, model JSON, token totals), `message` (`id, session_id,
  time_created, time_updated, data`), `part` (`id, message_id,
  session_id, …, data`). `data` is the JSON payload minus the id/fk
  fields. FKs cascade part → message → session → project.
- Read order (opencode's own): messages by `(time_created, id)`; parts
  by part id alone — part `time_created` is ignored.
- IDs: `<prefix>_` + 12 hex chars (48-bit `Date.now()*4096 + counter`)
  + 14 random base62. Sessions (`ses_`) bitwise-NOT the timestamp so
  they sort **descending**; messages (`msg_`) and parts (`prt_`) sort
  ascending. (`packages/schema/src/identifier.ts`)
- Threading is flat: assistant messages carry `parentID` = the user
  message that opened the turn; several assistants share one parent.
- Message schema: `packages/schema/src/v1/session.ts`. User messages
  **require** `agent` and `model`. Part types: `text`, `reasoning`,
  `tool` (call+result in one part; `state.status` ∈ pending/running/
  completed/error), `step-start`, `step-finish`, `file`, `snapshot`,
  `patch`, plus rarities.
- External writes are sanctioned: `opencode import`
  (`packages/opencode/src/cli/cmd/import.ts`) does raw
  `INSERT … ON CONFLICT(id) DO NOTHING` of exactly session + message +
  part rows. A synthetic session made of those three is listable and
  resumable.
- No CLI/API path pins a session id at creation — direct insert is the
  only way, and `opencode -s <id>` validates only that the id exists
  (no directory match) before opening exactly that session.
- TUI hazards: a running TUI full-syncs a session once and then trusts
  events — external rows written to an already-synced session stay
  invisible until relaunch. A session whose last message is not a
  completed assistant renders as perpetually "working"
  (`packages/tui/src/context/sync.tsx`). The session list window is 30
  days by `time_updated`.
- Provider metadata replays verbatim on resume if `providerID/modelID`
  match a real provider — forged reasoning signatures would be
  rejected upstream. A foreign `providerID` makes opencode degrade
  reasoning to plain text and drop metadata
  (`packages/opencode/src/session/message-v2.ts:243-247,362-372`).

### Connection

Open read-write (never read-only: with a 4 KB `.db` and megabytes in
the `-wal`, a reader that can't attach the WAL sees an empty database),
`busy_timeout=5000`, `foreign_keys=ON`, short transactions.

### Shadow birth

Resolve the `project` row by worktree (`SELECT id FROM project WHERE
worktree = ?`; never recompute the project-id hash). If absent,
materialize it by running one cheap no-model opencode command in the
cwd (exact command live-verified at implementation), then fail with
doctor guidance if still absent. Insert the `session` row with a minted
descending `ses_` id: required fields `id, project_id, slug, directory,
title, version (detected binary version), cost, tokens_*`, `path` = cwd
relative to worktree (`""` at root), `agent` `"build"`, `parent_id`
NULL, `time_archived` NULL. `model` resolves through a ladder: an
operator pin in `[opencode] args` if present, else opencode's own
recorded last-used model (state `model.json`), else any existing
session's model in this project, else the sentinel
`{"providerID": "tandem", "modelID": "<synced>"}` — the same value
then stamped on tandem-written user message rows (which schema-require
`model`). NULL-model resume behavior is a live-verification item.

### Read path (opencode active)

Opencode mutates tool-part rows **in place** as state transitions
(pending → running → completed), so a row cursor over inserts would
miss updates. The adapter consumes at **turn boundaries**: wait until
the last assistant message of the session has `time.completed` and a
terminal `finish`, then read the whole turn's messages+parts in native
order and emit normalized events. Mapping: `text` → AssistantMessage,
`reasoning` → Thinking, `tool` → ToolCall + ToolResult (split from the
one part), `step-start`/`step-finish`/`snapshot`/`patch` → SystemEvent,
user message text part → UserMessage. Unknown part types degrade to
SystemEvent, never raise. The turn-boundary probe doubles as the flip
gate's `session_status`: last assistant incomplete ⇒ "busy".

### Write path (opencode shadow)

One SQLite transaction per synced turn:

1. User message row — `agent` and `model` copied from the session row
   (schema-required).
2. Assistant message row(s) — `providerID: "tandem"` (sentinel),
   `modelID` = real source model (display), `parentID` = the turn's
   user message id, `path.cwd`/`path.root`, zeroed tokens/cost.
3. Parts — text; reasoning (text only, no provider metadata); tool
   parts with `state.status: "completed"`, pairing ToolCall with its
   ToolResult by `call_id` (renderer buffers the call until the result
   arrives; unpaired at turn end closes as `state.status: "error"`).
4. Close: last assistant gets `time.completed` and `finish: "stop"` —
   preserving the completed-assistant invariant so the TUI never shows
   a synced session as "working". Placeholders render as a user-role
   note (existing placeholder policy) followed by a closed assistant
   acknowledgment.

Timestamps: `time_created`/`time_updated` are NOT NULL with
drizzle-side defaults only — raw SQL supplies both.

**Echo suppression:** the sentinel `providerID: "tandem"` is the
attribution marker; `parse_entry` skips any message wearing it, so
tandem never re-translates its own writes when opencode becomes active.

**Crash safety:** message/part ids are pre-minted into the cursor's
write-ahead intent before touching opencode's DB; replay after a crash
re-inserts the same ids with `ON CONFLICT DO NOTHING` — idempotent by
construction. (Simpler than the file-append `pre_size` machinery; the
cursor re-key makes the intent per-direction.)

### Launch surface

- Interactive: `opencode -s <session_id>` (+ `[opencode] args`
  passthrough — `config.load_harness_args` is already generic).
- One-off: `opencode run -s <session_id> <prompt>` (404-exits if the
  session doesn't exist; blocks until idle).
- `hook_argv_extra` → `[]`. No per-invocation hook flag exists; the
  tailer's fs-watch on the `-wal` file is the wake-up signal.
- Quit keystrokes: live-verified and pinned at implementation, like the
  other harnesses; the SIGTERM ladder backstops.
- Compat floor: opencode ≥ 1.18.15, plus a startup sanity check that
  the v1 `message`/`part` tables exist (guards the v2 storage
  migration). Installed-but-unusable drops opencode from participants
  with a warning rather than crashing the session.

## Frame and flip

- `StatusBar` (`frame.py:222-277`) renders the participant list —
  ` claude ● │ codex ○ │ opencode ○   ^] flips` — same one-cell-glyph
  and truncation rules. `FrameIO` (`ptyrun.py:136-153`) carries
  `active` + `others: list[str]`. The bar never reads config directly;
  it renders the session's resolved participants so bar and cycle
  cannot disagree.
- `ops.switch_session` gains an explicit `to=` target; Ctrl-] resolves
  next-in-cycle. The flip failure ladder (`flip.py:256-264`)
  generalizes with a visited set: target won't launch → try the next
  unvisited harness in the cycle; all fail → fall back to the old
  active. At N=2 this reduces exactly to today's no-ping-pong rule.
- Warmup: `fire_warm` (`runner.py:550`) warms next-in-cycle. One
  opencode-specific carve-out: an opencode TUI booted before the final
  drain lands would cache the session pre-drain (TUI syncs once, then
  trusts events) and never show the last turn. An **opencode-bound**
  standby therefore arms only after the final drain completes —
  opencode-bound flips are colder in v1; claude/codex-bound flips keep
  the full pipeline. Future fix if it grates: the idle plugin.

## Error handling and doctor

- Untranslatable opencode rows quarantine + placeholder (existing
  policy); never block the pipeline.
- DB locked beyond `busy_timeout` → append retries next tailer tick
  (idempotent replay makes this safe).
- `opencode db path` failure → default path + doctor flags the
  discrepancy.
- Schema drift at startup → opencode dropped from participants with a
  warning.
- Doctor opencode section (runs only when the binary exists): version
  vs. floor, DB reachable/writable, WAL mode on, project row for cwd,
  paired-session structure (last message a completed assistant).
  Non-installed harnesses: one informational line. `--live` resume
  checks iterate participants (`doctor.py:301-315`).

## Testing

- **Unit/golden:** fixture rows from the operator's real first-session
  DB drive `parse_entry` round-trips; renderer tests for the
  call/result pairing buffer, the completed-assistant invariant, id
  minting monotonicity/encoding (verified against a real id:
  `ses_007b079c2ffe…` ⇔ `~(1786577389117*4096+1)`).
- **Schema validation:** dev-time codegen converts opencode's zod
  schemas to JSON Schema (script in tandem, runs in the checkout,
  output vendored under `tests/`); CI validates every rendered
  message/part against them. Regenerated on compat-floor bumps.
- **Oracle (local, skip-if-missing):** with the real binary, write a
  synthetic session; assert `opencode export` round-trips it and
  `opencode session list` shows it.
- **Core generalization:** cycle/failure-ladder at N=2 and N=3, cursor
  fan-out crash-replay, N-slot bar width/truncation, participant
  resolution (each CLI missing in turn, config subsets, <2 error).
- **Regression net:** existing claude↔codex tests pass untouched,
  proving N=2 behavior didn't move.

## Live-verification items (implementation time)

1. Exact no-model command that materializes a `project` row in a fresh
   cwd.
2. Opencode TUI quit-keystroke recipe (pin per version).
3. That `opencode -s <id>` on a tandem-minted session renders cleanly
   (sentinel provider display, reasoning-as-text degradation, and
   resume behavior when the session row's `model` fell through to the
   sentinel).
4. WAL fs-watch debounce behavior under a busy turn.
5. Flip-gate probe latency (DB poll cost while opencode active).

## Reference index (opencode source, checkout at ~/git/opencode)

- Tables: `packages/core/src/session/sql.ts`
- Message/part schema: `packages/schema/src/v1/session.ts`
- ID scheme: `packages/schema/src/identifier.ts`
- External-write recipe: `packages/opencode/src/cli/cmd/import.ts`
- Read ordering: `packages/opencode/src/session/message-v2.ts`
- TUI cache/working-state: `packages/tui/src/context/sync.tsx`
- DB path/pragmas: `packages/core/src/database/database.ts`
- Project-id derivation: `packages/core/src/project.ts`
