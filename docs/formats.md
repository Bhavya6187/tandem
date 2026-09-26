# Observed session formats

Everything tandem knows about the three native session formats was observed
on this machine by creating throwaway sessions and reading the files (or
database) the CLIs wrote. Versions observed:

| CLI | version | session storage |
| --- | --- | --- |
| Claude Code (`claude`) | 2.1.261 | `~/.claude/projects/<munged-cwd>/<sessionId>.jsonl` |
| Codex CLI (`codex`) | 0.153.4 | `~/.codex/sessions/YYYY/MM/DD/rollout-<YYYY-MM-DDThh-mm-ss>-<uuidv7>.jsonl` |
| opencode (`opencode`) | 1.18.15 | one SQLite database, the path `opencode db path` prints (see below) |

The versions above are the ones the formats were last re-observed on. The
chat window (`tandem chat`) was built and gated against claude 2.1.265 and
opencode 1.18.20 — the versions `compat.py` pins — without a format
recheck; nothing in it depends on a format change, and the shapes below
have not been re-read on those releases.

Env overrides honored: `CLAUDE_CONFIG_DIR` (claude home), `CODEX_HOME` (codex
home), `OPENCODE_DB` (opencode database), `TANDEM_HOME` (tandem state).

Rechecked 2026-09-05 on claude 2.1.261 / codex 0.153.4 (a live paired
session plus a four-turn claude→codex→claude→codex→claude probe): the
conversation-bearing shapes below are unchanged; the additions are marked
"2.1.26x" / "0.153". Codex's accepted range runs through 0.159; 0.160+
warns until rechecked. Unknown top-level record types still fail
validation rather than being silently declared compatible.

## Claude Code transcript (claude 2.1.220, rechecked 2.1.261)

- One JSONL file per session, named `<sessionId>.jsonl` (sessionId is a
  UUIDv4). Project directory name = cwd with every character outside
  `[A-Za-z0-9]` replaced by `-` (observed: `/private/tmp/claude-501/x` →
  `-private-tmp-claude-501-x`).
- Conversation entries carry `uuid` + `parentUuid` forming a chain, plus
  `sessionId`, `cwd`, `version`, `gitBranch`, `timestamp` (ISO-8601 ms UTC
  `Z`), `isSidechain`, `userType`, `entrypoint`.
- Entry `type`s observed:
  - `user` — `message: {role: "user", content: <string | block list>}`.
    A plain string content = a real user prompt. A block list with
    `tool_result` blocks = a tool result; the sibling field `toolUseResult`
    holds the structured result (e.g. `{type: "create", filePath, content,
    structuredPatch}` for Write, `{stdout, stderr, interrupted}` for Bash),
    and `sourceToolAssistantUUID` points at the entry with the `tool_use`.
  - `assistant` — `message` is an API message; **one content block per JSONL
    line**, consecutive lines share `message.id`. Block types: `thinking`
    (signature-bound), `text`, `tool_use` (`id`, `name`, `input`).
    `stop_reason` on each line (`tool_use` / `end_turn`).
  - `attachment` — hook output, skill/agent listings, deferred tool deltas.
    Not conversation content.
  - `queue-operation` (enqueue/dequeue of the prompt), `last-prompt`
    (`leafUuid` pointer), `summary`, `system` — bookkeeping.
  - `permission-mode`, `ai-title`, `pr-link`, `relocated`, `worktree-state`,
    `file-history-snapshot` + `file-history-delta` — uuid-less session
    metadata (permission mode, AI-generated title, linked PR, worktree
    moves, file-backup tracking). Not conversation content; claude resumes
    transcripts containing them without complaint.
  - 2.1.26x adds four more uuid-less metadata records: `atis-latch`
    (`atis`, `sessionId`), `bridge-session` (`bridgeSessionId`,
    `lastSequenceNum`, owner account/org — routine per-session identity,
    `lastSequenceNum` constant 0), `cost-state` (accumulated cost, timing,
    line counts, `modelUsage`) and `custom-title` (`customTitle`, set when
    the user renames a session). Same handling as the batch above.
  - 2.1.26x can put a `fallback` block (`{from: {model}, to: {model}}`) in
    an assistant message when the model is switched mid-session. No prose;
    tandem drops it like any other non-text assistant block.
- Resume: `claude --resume <sessionId>` (from the same cwd). A new session
  can be pinned to a chosen id with `claude --session-id <uuid>`.
  - 2.1.261 restores the session model on resume from the last assistant
    entry whose `message.model` is a string other than `"<synthetic>"`
    (claude's own tag on model-less stubs such as API-error messages) and
    warns `Session model X could not be restored (not a model this version
    of Claude Code recognizes) — using <default> instead` when that value is
    not a model it knows. Tandem-rendered assistant entries therefore copy
    the model claude last used in that transcript, and carry `"<synthetic>"`
    until claude has run at all (a zero-turn claude flipped back into). The
    `"<synced>"` placeholder tandem ≤0.5.1 wrote there drew the warning;
    entries carrying it are still skipped when tandem derives the model.
- Turn boundary: a `user` entry with string content starts a turn; an
  `assistant` line with `stop_reason: "end_turn"` ends it. The `Stop` hook
  (injectable per-invocation via `--settings '<json>'`) fires at turn end.

## Codex rollout (codex-cli 0.145.0, rechecked 0.153.4)

- One JSONL file per session under a date-sharded dir; the session id
  (UUIDv7) is embedded in the filename. `~/.codex/session_index.jsonl` maps
  `{id, thread_name, updated_at}`. `~/.codex/history.jsonl` is a global
  prompt history.
- Every line is `{timestamp, type, payload}`.
- **Paginated rollouts (0.155.1).** Codex now writes its own threads with
  `history_mode: "paginated"` and a top-level `ordinal` on every record —
  `session_meta` is 0, then every `response_item`, `event_msg`,
  `turn_context`, … counts up contiguously. It keeps a projection of the
  thread in `~/.codex/thread_history_1.sqlite` (`thread_history_projection_
  state` holds the next byte offset + next ordinal it expects). On resume
  it catches the projection up from that offset, and a final record without
  an ordinal is fatal: `final paginated rollout record at <path> is missing
  an ordinal` (thread never starts). `CodexAdapter.shadow_append` therefore
  continues the file's own sequence whenever the last record carries one;
  tandem's seeded shadows say `history_mode: "legacy"`, carry no ordinals,
  and get none (codex still resumes those). Live-checked 2026-09-25 on a
  codex-created thread: unchanged tail → the error verbatim; the same tail
  re-appended with ordinals → codex initialised the thread, advanced its
  projection over tandem's records, numbered its own next records after
  them, and answered a prompt by quoting the synced `[via claude-code]`
  message verbatim. So a paginated thread's model context does include
  tandem's out-of-turn synced records (no `turn_context` / `task_started`
  framing needed), even though codex's `thread_items` projection indexes
  only its own prompts.
- `type` values observed:
  - `session_meta` — first line: `{session_id, id, timestamp, cwd,
    originator, cli_version, source, thread_source, model_provider,
    base_instructions, history_mode: "legacy" | "paginated",
    context_window}`.
  - `response_item` — the **model-facing** history. `payload.type`:
    - `message` — `role` developer/user/assistant; content blocks
      `input_text` (developer/user) or `output_text` (assistant); assistant
      messages carry `phase: "commentary" | "final_answer"`.
      Note: role=user response_items include injected context (permissions,
      plugins, environment) — the real user prompt is identified by the
      matching `event_msg/user_message`.
    - `reasoning` — `encrypted_content`, not portable.
    - `function_call` — `{name, arguments: <json string>, call_id}` (e.g.
      `exec_command`).
    - `function_call_output` — `{call_id, output}` (chunked shell output with
      exit code header). `output` is a string, or (0.153, roughly half of
      all outputs) a list of `{type: "input_text", text}` blocks that are
      consecutive chunks of one output — flatten by concatenation. Same
      for `custom_tool_call_output`.
    - `custom_tool_call` / `custom_tool_call_output` — `apply_patch` with
      `input` = patch text (`*** Begin Patch ...`). 0.153 code mode uses the
      same pair with `name: "exec"` and JavaScript in `input` (plus `id`,
      `status`, `internal_chat_message_metadata_passthrough`, all inert);
      nested tool calls live inside that outer call/output.
  - `event_msg` — the **UI-facing** stream. `payload.type`: `task_started`
    (`turn_id`), `user_message`, `agent_message` (`phase`), `token_count`,
    `patch_apply_end` (`{stdout, success, changes: {path: {type, content}}}`),
    `task_complete` (`last_agent_message`).
    - A patch the sandbox **rejects** emits no `patch_apply_end` at all
      (live probe, 2026-08-01, `codex exec --sandbox read-only`: the event
      is written when a patch applies, and every observed one carried
      `success: true`). The rejection survives only in the `response_item`
      pair — the `custom_tool_call` that ran the patch tool plus its
      call_id-matched `custom_tool_call_output`, whose output text carries
      `patch rejected` at the start of a line (`Script error:\npatch
      rejected: …`). That pair is what `ops.blocked_write_paths` matches, and
      it matches the marker line-anchored — mid-line hits are what a worker
      grepping for these literals gets back, not a rejection.
  - `turn_context` — per turn: cwd, approval_policy, sandbox_policy, model.
  - `world_state` — environment snapshot.
  - `token_usage_record` (0.153) — per-response accounting: thread/turn/
    response ids plus `usage`, `turn_token_usage`, `thread_token_usage`.
    Not model history. `event_msg/token_count` still appears and remains
    the usage meter's source — never sum the two. `event_msg/
    thread_settings_applied` is likewise bookkeeping.
- Resume: `codex resume <session-id>` (interactive) and
  `codex exec resume <session-id> "<prompt>"` (one-shot). Turn-complete
  notification hook: `-c 'notify=["/bin/sh","-c","..."]'` per invocation.
- Turn boundary: `event_msg/task_started` … `event_msg/task_complete`.

## Decisions on the spec's open questions

1. **File discovery** — as above; codex rollouts are found by globbing the
   session id in the filename (no file reads needed); claude transcripts by
   deterministic munged-cwd + session id path.
2. **Hooks vs fs-watch** — both CLIs accept per-invocation hook wiring
   (`claude --settings` Stop hook; `codex -c notify=[...]`), so tandem wires
   a turn-complete hook that touches a sentinel file used to flush the sync
   loop promptly. The transcript tailer (watchdog, polling fallback) remains
   the source of truth and the fallback when hooks fail — hook output is
   never parsed, only used as a wake-up signal.
3. **Placeholder format** — untranslatable entries become, in the shadow's
   native text form:
   `[tandem: turn {turn} could not be translated from {source} — {reason};
   raw entry quarantined at {path}]`
   with the raw source entry written to
   `~/.tandem/quarantine/<tandem-id>/<source>-line-<n>.json`.

## opencode session storage (opencode 1.18.15)

- Storage: one WAL-mode SQLite DB for everything — `opencode db path`
  (channel-suffixed filename; `$OPENCODE_DB` overrides). No per-session
  files. Tables: `session`, `message`, `part`; JSON payloads in `data`
  minus the id/fk columns. FKs cascade part -> message -> session -> project.
- Ordering: messages by `(time_created, id)`; parts by part id ONLY.
- IDs: `<prefix>_` + 12 hex chars (48-bit `ms*4096+counter`) + 14 random
  base62. `ses_` NOTs the value (descending); `msg_`/`prt_` ascending.
  Live-verified: msg @ ms=1786577389138 ctr=1 -> `ff84f8652001`.
- Threading is flat: assistant `parentID` = the turn's user message id.
- Spelling trap: session-row `model` JSON uses `{id, providerID}`; message
  payloads use `{modelID, providerID}`. Session-level `agent`/`model` are
  optional; MESSAGE-level are required on user messages. Session-level
  `slug` is REQUIRED by import's decoder (oracle-verified: the payload is
  rejected `at ["slug"]` without it) but carries no uniqueness constraint —
  opencode mints adjective-noun pairs; tandem seeds `tandem-pair`.
- Tool parts mutate in place (pending -> running -> completed): tandem reads
  whole completed turns only (last assistant has `time.completed` + terminal
  `finish`).
- External writes: the `opencode import` recipe (plain INSERT, conflict-
  ignore). Tandem's shadow birth delegates to `opencode import`; incremental
  sync writes rows directly with pre-minted ids (idempotent replay).
- Tandem attribution: `providerID: "tandem"`, `modelID: "<synced>"` — marks
  echoes for the parser and makes opencode degrade replay metadata instead
  of re-sending forged provider signatures.
- Hazards: a running TUI never sees external rows for an already-synced
  session (writes land while opencode is closed; flips relaunch it); a
  session whose last message is not a completed assistant renders as
  perpetually "working"; the session list window is 30 days by
  `time_updated`; always open the DB read-write (WAL).
- Resume: `opencode -s <id>` (id must exist; no directory match);
  one-off: `opencode run -s <id> "<prompt>"`. No per-invocation
  turn-complete hook — tandem fs-watches the `-wal` file.

## Chat window live gate (claude 2.1.265 / codex 0.153.4 / opencode 1.18.20)

`tools/live_gate_chat.py` drives `tandem chat` in tmux on a private socket:
a three-harness relay (claude → codex → opencode → claude) where each harness
is asked to name the previous one's word and to run a shell command, so
every step is its own cross-harness sync check — a harness can only name
that word if the turn was translated into its own native session file
first, and the file it touches proves the command really ran. codex and
claude are expected to ask for approval; opencode's default config
auto-allows `bash`, and the script tolerates either. Record each run here
as `date · versions · PASS/FAIL · notes`.

- 2026-09-13 · spike relay, headless via the raw protocols (pre-window) · PASS ·
  see `docs/specs/2026-09-08-unified-chat-window-design.md`, Decisions.
- 2026-09-13 · claude 2.1.265 / codex 0.153.4 / opencode 1.18.20 · FAIL (5) ·
  codex could not authenticate (`Your access token could not be refreshed
  because your refresh token was already used`). Plain `codex exec` fails
  identically, so this is an account that needs `codex login`, not a tandem
  fault. Everything not downstream of codex passed: window up, claude's turn,
  the claude approval row and its `y`, the command claude ran, and the
  two-Ctrl-C exit with status 0. The two opencode failures cascade from the
  failed codex turn — a turn that dies after its prompt is recorded still
  syncs that lone user message outward, and an opencode shadow ending on a
  user message is rejected by `validate_transcript`, so no later opencode
  turn can start in that session. The same script with the codex step removed
  (claude → opencode → claude) passed every step. Re-run once codex is
  signed in.
- 2026-09-16 · claude 2.1.273 / codex 0.154.0 / opencode 1.18.20 · FAIL (7) ·
  the gate script's own fault: its approval regex demanded `[a]lways`, but the
  row offers only what the harness listed and codex leaves `acceptForSession`
  out of `availableDecisions` for a plain command, so the row read
  `[y]es [n]o`, the `y` was never sent, and the codex turn waited on the
  approval until the quit denied it (`aborted by user`); everything after
  cascaded. Regex widened to `\[y\]es(?: \[a\]lways)? \[n\]o`. The run did
  verify the shutdown path live: unanswered approval denied on quit, exit in
  0.5 s, status 0.
- 2026-09-16 · claude 2.1.273 / codex 0.154.0 / opencode 1.18.20 · PASS (22/22) ·
  full relay with approvals on both codex and claude, opencode pinned to
  `opencode/big-pickle`, clean exit, `tandem status` shows 0 failed turns on
  every direction. The codex protocol models are still generated from
  0.153.4; `tandem doctor` warns about the 0.154.0 install (its schema adds
  two optional fields and a description — 23 generated lines — which the
  0.153.4 models parse unchanged).
