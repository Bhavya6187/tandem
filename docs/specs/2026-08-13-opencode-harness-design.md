# Opencode as a third harness

**Date:** 2026-08-13
**Status:** Revised after review (import-based shadow birth, storage
capabilities instead of an entry-store refactor, JSON state columns,
explicit flip fast-forward); awaiting operator approval

## Problem

Tandem pairs exactly two harnesses — claude code and codex — and the
pairing is a compile-time constant expressed six different ways (an
`other()` toggle, two id columns, two `Literal` unions, two-element
tuples, a two-slot status bar, a binary flip ladder). Opencode
(opencode.ai) should join as a third first-class harness: transcripts
synced both ways, a slot in the Ctrl-] cycle, launch/resume/one-off
support. Opencode also breaks a deeper assumption than arity: its
sessions live in **SQLite** (`opencode.db`, WAL mode), not an
append-only JSONL transcript.

## Scope decisions (operator calls)

- **Full peer harness** — sync + flip + launch, not a subagent-only or
  configurable-pair integration.
- **Ctrl-] cycles all participants** in order; no direct-jump keys, no
  active-pair-plus-parked model.
- **Subagent dispatch to opencode is deferred.** The subagent lane
  (`hookroute.py`, `plugin/`, `modelcat.py`, `pinstash.py`,
  `ops.run_sub`) shares no code with flip/sync and stays claude→codex.
- **No backward compatibility for existing tandem state.** The state
  schema changes ship without migration shims; on schema-version
  mismatch the state DB is recreated. Old pairings lose only the
  tandem-side linkage — native transcripts in each harness survive.
- **Minimal blast radius on the stable core.** The claude/codex JSONL
  sync machinery (`JsonlTailer`, `SyncEngine`'s file appends, the
  write-ahead `pre_size` intent) is not refactored; opencode's storage
  differences live behind adapter methods that default to today's
  file-backed behavior.

## Non-goals (v1)

- Opencode subagent dispatch (`tandem:opencode` agent, model pinning).
- MCP-config sync for opencode (`tandem sync-mcp` stays claude↔codex).
- Opencode's experimental v2 storage (`session_message`/
  `session_input`) — v1 targets the v1 `message`/`part` tables only;
  a future cutover is future maintenance (see Compat gate).
- A persistent opencode idle-hook plugin (fs-watch on the WAL file is
  the v1 wake-up signal).
- Reasoning content in either direction. Tandem's `Thinking` event
  carries no text by design (claude's reasoning is signature-bound,
  codex's encrypted); opencode reasoning parts parse to `Thinking` and
  are dropped by converters like the others, and tandem writes **no
  reasoning parts** into opencode.

## Architecture overview

### Participants and resolution

A top-level `harnesses` config key holds an ordered list of intended
participants; omitted means "all supported" (`["claude", "codex",
"opencode"]`). Config handling follows `config.py`'s forgiving
philosophy: unknown names, duplicates, or malformed values fall back to
the default list — configuration must never be the reason a launch
breaks.

At session creation tandem resolves **participants = configured ∩
installed-and-version-supported**, persists the resolved list on the
session, and that single list drives the flip cycle, the sync fan-out,
and the status bar. Fewer than two usable harnesses is an actionable
error naming what's missing (replacing the hard both-binaries check at
`cli.py:585`).

**Resume:** each resume recomputes availability (stored participants ∩
currently installed-and-supported), in this order:

1. Compute the surviving list. Fewer than two → fail with an
   actionable error **without modifying the stored session**.
2. Otherwise atomically persist the narrowed list and a valid active:
   if the stored active was itself removed, the first surviving
   participant in stored cycle order takes over. One-line notice
   either way.
3. Build the fan-out engines, flip cycle, and status bar from the
   persisted list.

A dropped member is gone from that session **permanently**; the
session continues at N−1. No dynamic rejoin — this is a deliberate
scope decision, not a cursor impossibility: cursors toward an absent
member are never touched (see the invariant below), so its backlog
would technically survive absence, but letting it rejoin safely would
require a reconciliation phase that drains every surviving source into
the returner before it enters the cycle. v1 skips that machinery; a
reinstalled CLI participates in fresh sessions, not old ones. The
surviving list is this session's **runtime participants**.

**Invariant: not-installed is a normal state, not a degraded one.** The
only probe an uninstalled harness ever receives is the PATH lookup
during resolution. Excluded harnesses trigger no discovery, no DB open,
no shadow session, no cursors, no bar slot, no doctor probes, and no
warnings — `tandem` with opencode absent behaves byte-for-byte like
today's claude↔codex tandem. Absence is mentioned in exactly one place:
a one-line informational note in `tandem doctor`. Warnings are reserved
for **installed but unusable** (version below floor, DB discovery
failure, schema drift). The rule is symmetric: codex uninstalled yields
a silent claude↔opencode tandem.

### Sync topology

The active harness is the single source of truth; its tailer drains new
entries into **N−1 shadow engines** (one `SyncEngine` per
`(source, target)` direction). With three participants each turn fans
out to two shadows.

**Echo suppression stays cursor-based**, exactly as today
(`ops.py`'s module docstring), generalized to N: whatever tandem
appends to a shadow is by construction already represented in the
source, so before a harness becomes a source its outgoing cursors
fast-forward past everything already present. The sentinel provider id
(below) is attribution and a parse-time backstop, not the primary
dedup mechanism.

**The flip sequence** (`ops.switch_session`, generalized):

1. Drain the old active into every other runtime participant (flushing
   dangling tool calls).
2. Fast-forward the new active's outgoing cursors — one per
   `(new_active, target)` direction, **for runtime-participant targets
   only** — to the new active's current end-of-store position. Cursors
   involving a non-participant are never created, advanced, or
   inspected, anywhere.
3. Set active; launch.

One-off turns (`run_oneoff`) generalize the same way: after the
target's turn drains, every recipient that was fully synced before the
drain fast-forwards, per today's echo rule applied per direction.

### Storage capabilities on the adapter (no entry-store refactor)

Instead of rewriting the file path behind a new abstraction, the
adapter interface grows four methods whose **base implementations are
today's JSONL behavior** — claude and codex inherit them unchanged, and
the existing tailer/sync/cursor code keeps working byte-for-byte:

- `make_source_reader(session, cursor)` — yields consumable units
  since the cursor. JSONL default: `JsonlTailer` lines. Opencode: one
  native object per completed turn (below).
- `append_rendered(session, entries, intent)` — durable append of
  rendered entries. JSONL default: write-ahead `pre_size` intent +
  `append_jsonl_fsync`. Opencode: one SQLite transaction with
  pre-minted ids.
- `fast_forward_position(session)` — the value that marks "everything
  currently in this store is known". JSONL default: byte size + line
  count. Opencode: `(time_created, id)` of the last message row.
- `watch_paths(session)` — files whose changes wake the tailer. JSONL
  default: the transcript. Opencode: `opencode.db-wal`.

`transcript_path` remains for file-backed harnesses and doctor display.

## N-harness generalization

### State schema (`state.py`)

Two JSON columns on `sessions`, no child table (matching how cursor
`pending` already stores JSON):

- `participants` TEXT — ordered JSON array, resolved at creation.
- `native_session_ids` TEXT — JSON object `{harness: id | null}`,
  replacing the `claude_session_id`/`codex_session_id` columns.
- The `CHECK (active IN ('claude','codex'))` constraint goes.
- `sync_cursors` re-keys from `(tandem_id, source)` to
  `(tandem_id, source, target)`; the write-ahead `intent`,
  `pending_calls`, and leaf-chain state are per-direction facts. The
  cursor's position fields stay per-source in meaning (same source
  store), but live per-direction so each target commits independently.
- No migration. Schema version bumps; mismatched DBs are recreated.

### Code model

- `other()` (`harness/__init__.py:15`) and `PairedSession.shadow`
  (`state.py:60`) are replaced by `Session.participants` (the runtime
  participants), `next_active(current)` (cycle order = list order), and
  `targets_for(source)`. All six call sites move (`sync.py:56`,
  `converter.py:99`, `ops.py:226`, `ops.py:107`, `runner.py:550`,
  `runner.py:588`).
- `Direction` literals (`converter.py:33`, `events.py:92`) become plain
  validated `"<source>-><target>"` strings; the `Agent` literal
  (`events.py:15`) becomes `str`.
- `SessionContext` (`events.py:84-107`) carries per-direction identity
  only: `source_session_id` and `target_session_id` replace the two
  named id fields (a direction's renderer never needs an unrelated
  harness's id). Claude's renderer scratch (leaf uuid, run message id,
  resume model) moves to a per-adapter namespace
  `harness_state[harness_id]`; `ctx_from_cursor`/`ctx_to_cursor`
  (`runner.py:62-88`) persist the namespace blob.
- Name-branches become adapter dispatch: shadow store resolution via
  the storage capabilities above (`sync.py:61-75`), claude leaf-repair
  via an optional adapter hook (`sync.py:109,178`), the status probe
  via a capability check (`runner.py:533`), codex rollout discovery
  behind the adapter (`runner.py:405-429,613-628`), entry-shape
  validation in doctor via a per-adapter method (`doctor.py:18-61`),
  late-shadow creation via adapter policy flags
  (`ops.py:111-147,202-219`).
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

One module, `src/tandem/harness/opencode.py`; SQLite helpers live
inside it. Schema facts verified against opencode 1.18.15 (installed) /
1.18.17 (checkout at `~/git/opencode`) and the live DB from the
operator's first session. This is an **internal-format integration**:
opencode's tables are not a published contract, so tandem pins a
minimum supported version and validates against opencode's own
importer/exporter in tests.

### Compat gate

Floor only, no ceiling (operator call: assume forward versions keep
working). `COMPAT["opencode"]` pins `tested="1.18.15"`,
`min_version=(1, 18)` — versions below the floor predate the SQLite
storage era and genuinely cannot work. `CompatRange.max_exclusive`
becomes optional (`None` = unbounded) rather than inventing an
artificial giant version tuple. If opencode's storage does move (the repo carries an
experimental v2 `session_message` model), the tripwires are the cheap
startup sanity check that `message`/`part` exist (failing closed:
opencode dropped from the usable set with a warning) and the oracle
tests; handling an actual cutover is future maintenance, not a v1
gate.

### Storage facts

- DB: `~/.local/share/opencode/opencode.db`, WAL mode. Filename is
  channel-suffixed (`opencode-<channel>.db`) — discover via
  `opencode db path` once per process. **Discovery failure fails
  closed:** opencode is dropped from the usable set with a warning
  (installed-but-unusable), never a guessed default path.
- Tables: `session`, `message` (`id, session_id, time_created,
  time_updated, data`), `part` (`id, message_id, session_id, …,
  data`). `data` is the JSON payload minus the id/fk fields. FKs
  cascade part → message → session → project.
- Read order (opencode's own): messages by `(time_created, id)`; parts
  by part id alone.
- IDs: `<prefix>_` + 12 hex chars (48-bit `Date.now()*4096 + counter`)
  + 14 random base62. Sessions (`ses_`) bitwise-NOT the timestamp so
  they sort descending; messages (`msg_`) and parts (`prt_`) sort
  ascending. (`packages/schema/src/identifier.ts`; encoding verified
  against a real id.)
- Threading is flat: assistant messages carry `parentID` = the user
  message that opened the turn; several assistants share one parent.
- Spelling trap: the session row's `model` JSON uses `{id, providerID,
  variant?}`; message payloads use `{modelID, providerID}`.
- User messages schema-require `agent` and `model`; **session-level
  `agent` and `model` are optional** (`packages/schema/src/session.ts`).
- `opencode -s <id>` validates only that the id exists (no directory
  match) before opening exactly that session.
- TUI hazards: a running TUI full-syncs a session once and then trusts
  events — external rows written to an already-synced session stay
  invisible until relaunch. A session whose last message is not a
  completed assistant renders as perpetually "working". The session
  list window is 30 days by `time_updated`.
- A foreign `providerID` on an assistant message makes opencode degrade
  its parts on replay instead of re-sending provider metadata
  (`message-v2.ts:243-247`) — synthetic turns must never impersonate a
  real provider.

### Connection

Open read-write (a read-only open that can't attach the WAL sees stale
or empty data; read-write sidesteps the whole class), one connection
per thread (never shared across the tailer thread boundary),
`busy_timeout=5000`, `foreign_keys=ON`, `BEGIN IMMEDIATE` for write
transactions, short transactions only.

### Shadow birth: delegate to `opencode import`

Tandem does not construct session rows. To create the opencode shadow:

1. Mint the `ses_` id (descending, tandem's port of the id scheme).
2. Write a minimal export-format JSON to the scratchpad:
   `{info: {id, title, time, cost, tokens, …}, messages: [seed turn]}`
   — structure templated from a captured `opencode export` of a real
   session. **Session-level** `agent`/`model` are omitted (optional;
   opencode applies its default on resume). **Message-level** fields
   are not optional and must be present: the seed user message carries
   `agent: "build"` and the sentinel
   `model: {providerID: "tandem", modelID: "<synced>"}`, and the seed
   assistant carries the matching sentinel `providerID`/`modelID`. The
   seed turn is that user message with the tandem seed note plus one
   closed assistant acknowledgment (`finish: "stop"`, `time.completed`
   set), so the session lists as idle and shows its provenance like
   the other harnesses' seeded shadows.
3. Run `opencode import <file>` in the session cwd. Import runs
   opencode's own schema decoders, bootstraps/uses the project row via
   its app context, re-homes `projectID`/`directory`/`path`, and
   upserts the session — so re-running is idempotent and tandem never
   touches project resolution, slugs, or session-row SQL.
4. Verify the session exists (one `SELECT`), then delete the temp file.

One subprocess, once per session, off the sync path — the boot cost
that made per-tick subprocesses unacceptable is irrelevant here.
Everything *incremental* stays direct SQLite.

### Read path (opencode active)

Opencode mutates tool-part rows **in place** as state transitions
(pending → running → completed), so the reader never exposes raw rows.
The consumable unit is **one completed turn as one native object**:

```python
{"user": {"message": …, "parts": […]},
 "assistants": [{"message": …, "parts": […]}, …]}
```

`make_source_reader` waits until the turn's last assistant message has
`time.completed` and a terminal `finish`, then assembles that object in
native order and yields it. `parse_entry` receives the whole object and
emits the turn's full normalized event list — the existing
`parse_entry` pipeline, one entry in, events out; no second parser API,
and mutable rows never cross the adapter boundary. The durable cursor
is the turn's terminal assistant `(time_created, id)` plus its opening
user message id; an incomplete turn is simply not yielded, so nothing
is half-consumed and re-reads cannot duplicate the user message.
Per-direction cursors advance independently as each target's append of
the translated turn commits.

Mapping within a turn: user text part → UserMessage, `text` →
AssistantMessage, `reasoning` → Thinking (no text — dropped
downstream), `tool` → ToolCall + ToolResult (split from the one part),
`step-start`/`step-finish`/`snapshot`/`patch` → SystemEvent. Unknown
part types degrade to SystemEvent, never raise. The turn-boundary probe
doubles as the flip gate's `session_status`: last message user, or
assistant without `time.completed` ⇒ "busy".

### Write path (opencode shadow)

One SQLite transaction per synced turn:

1. User message row — `agent` and `model` stamped with the session's
   values if present, else the sentinel (schema-required on user
   messages).
2. Assistant message row(s) — `providerID: "tandem"` (sentinel),
   `modelID` = real source model (display), `parentID` = the turn's
   user message id, `path.cwd`/`path.root`, zeroed tokens/cost.
3. Parts — text parts and tool parts only (no reasoning parts). Tool
   parts land as `state.status: "completed"`, pairing ToolCall with its
   ToolResult by `call_id` (the renderer buffers the call until its
   result arrives; unpaired at turn end closes as `"error"`).
4. Close: last assistant gets `time.completed` and `finish: "stop"` —
   the completed-assistant invariant, so the TUI never shows a synced
   session as "working". Placeholders render as a user-role note
   followed by a closed assistant acknowledgment.

`time_created`/`time_updated` are NOT NULL with app-side defaults only
— tandem supplies both. Message/part ids are pre-minted into the
cursor's write-ahead intent before touching opencode's DB; crash replay
re-inserts the same ids with `ON CONFLICT DO NOTHING`, idempotent by
construction. `parse_entry` additionally skips sentinel-attributed
messages as a backstop; the cursor fast-forward is what actually
prevents echo.

### Launch surface

- Interactive: `opencode -s <session_id>` (+ `[opencode] args`
  passthrough — `config.load_harness_args` is already generic).
- One-off: `opencode run -s <session_id> <prompt>` (404-exits if the
  session doesn't exist; blocks until idle).
- `hook_argv_extra` → `[]`; the WAL fs-watch is the wake-up signal.
- Quit keystrokes: live-verified and pinned at implementation; the
  SIGTERM ladder backstops.

## Frame and flip

- `StatusBar` (`frame.py:222-277`) renders the runtime participant
  list — ` claude ● │ codex ○ │ opencode ○   ^] flips` — same
  one-cell-glyph and truncation rules. `FrameIO` (`ptyrun.py:136-153`)
  carries `active` + `others: list[str]`. The bar renders the session's
  resolved participants, never config directly.
- Ctrl-] resolves next-in-cycle; `ops.switch_session` takes an explicit
  `to=`. The flip failure ladder (`flip.py:256-264`) generalizes with a
  visited set: target won't launch → next unvisited in cycle; all fail
  → fall back to the old active. At N=2 this reduces to today's
  no-ping-pong rule.
- Warmup: `fire_warm` (`runner.py:550`) warms next-in-cycle. An
  **opencode-bound** standby arms only after the final drain completes
  (a TUI booted pre-drain would cache the session and never show the
  last turn); opencode-bound flips are colder in v1, claude/codex-bound
  flips keep the full pipeline.

## Error handling and doctor

- Untranslatable opencode turns quarantine + placeholder (existing
  policy); never block the pipeline.
- DB locked beyond `busy_timeout` → the append retries next tailer tick
  (idempotent replay makes this safe).
- `opencode db path` failure or schema drift at startup → opencode
  dropped from the usable set with a warning (fail closed; never guess).
- Doctor opencode section (runs only when the binary exists): version
  vs. floor, DB discovered and writable, WAL mode on, paired-session
  structure (last message a completed assistant). Non-installed
  harnesses: one informational line. `--live` resume checks iterate
  runtime participants (`doctor.py:301-315`).

## Testing

- **Unit/golden:** fixture rows from the operator's real first-session
  DB drive `parse_entry` round-trips; renderer tests for the
  call/result pairing buffer, the completed-assistant invariant, id
  minting monotonicity/encoding (verified against a real id:
  `ses_007b079c2ffe…` ⇔ `~(1786577389117*4096+1)`).
- **Oracle (local, skip-if-missing):** with the real binary: shadow
  birth via `opencode import` succeeds in a fresh directory; a
  tandem-written session round-trips through `opencode export`; and
  `opencode session list` shows it. These are the schema-fidelity
  tests — no zod codegen, no checkout dependency in CI.
- **Core generalization:** cycle/failure-ladder at N=2 and N=3,
  per-direction cursor fan-out and crash-replay, the flip fast-forward
  of all outgoing directions (the anti-echo property: a turn synced
  into opencode never flows back out of it), N-slot bar
  width/truncation, participant resolution (each CLI missing in turn,
  config subsets, <2 error, resume with a missing member — asserting
  the member is dropped, the narrowed list persists, and the session
  continues at N−1).
- **Regression net:** existing claude↔codex tests pass untouched — the
  JSONL storage path is deliberately not refactored, so they prove N=2
  behavior didn't move.

## Live-verification items (implementation time)

1. Minimal `opencode import` file shape: template from a real
   `opencode export`; confirm omitted `agent`/`model` resumes cleanly
   and the seed turn renders as intended.
2. Opencode TUI quit-keystroke recipe (pin per version).
3. A tandem-minted, tandem-synced session renders cleanly in the TUI
   (sentinel provider display, tool parts, no "working" state).
4. WAL fs-watch debounce behavior under a busy turn.
5. Flip-gate probe latency (DB poll cost while opencode active).

## Reference index (opencode source, checkout at ~/git/opencode)

- Tables: `packages/core/src/session/sql.ts`
- Message/part schema: `packages/schema/src/v1/session.ts`
- Session info schema: `packages/schema/src/session.ts`
- ID scheme: `packages/schema/src/identifier.ts`
- Import (shadow-birth delegate): `packages/opencode/src/cli/cmd/import.ts`
- Read ordering / replay degradation: `packages/opencode/src/session/message-v2.ts`
- TUI cache/working-state: `packages/tui/src/context/sync.tsx`
- DB path/pragmas: `packages/core/src/database/database.ts`
