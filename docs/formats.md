# Observed session formats

Everything tandem knows about the three native session formats was observed
on this machine by creating throwaway sessions and reading the files (or
database) the CLIs wrote. Versions observed:

| CLI | version | session storage |
| --- | --- | --- |
| Claude Code (`claude`) | 2.1.220 | `~/.claude/projects/<munged-cwd>/<sessionId>.jsonl` |
| Codex CLI (`codex`) | 0.145.0 | `~/.codex/sessions/YYYY/MM/DD/rollout-<YYYY-MM-DDThh-mm-ss>-<uuidv7>.jsonl` |
| opencode (`opencode`) | 1.18.15 | one SQLite database, the path `opencode db path` prints (see below) |

Env overrides honored: `CLAUDE_CONFIG_DIR` (claude home), `CODEX_HOME` (codex
home), `OPENCODE_DB` (opencode database), `TANDEM_HOME` (tandem state).

## Claude Code transcript (claude 2.1.220)

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
- Resume: `claude --resume <sessionId>` (from the same cwd). A new session
  can be pinned to a chosen id with `claude --session-id <uuid>`.
- Turn boundary: a `user` entry with string content starts a turn; an
  `assistant` line with `stop_reason: "end_turn"` ends it. The `Stop` hook
  (injectable per-invocation via `--settings '<json>'`) fires at turn end.

## Codex rollout (codex-cli 0.145.0)

- One JSONL file per session under a date-sharded dir; the session id
  (UUIDv7) is embedded in the filename. `~/.codex/session_index.jsonl` maps
  `{id, thread_name, updated_at}`. `~/.codex/history.jsonl` is a global
  prompt history.
- Every line is `{timestamp, type, payload}`.
- `type` values observed:
  - `session_meta` — first line: `{session_id, id, timestamp, cwd,
    originator, cli_version, source, thread_source, model_provider,
    base_instructions, history_mode: "legacy", context_window}`.
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
      exit code header).
    - `custom_tool_call` / `custom_tool_call_output` — `apply_patch` with
      `input` = patch text (`*** Begin Patch ...`).
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

## Mixed-tab routing (v1) live gate — 2026-08-23

Env: claude 2.1.241 (registry status "idle"), codex-cli 0.147→0.149, opencode 1.18.15→1.18.20, tmux 3.7b. Tool reinstalled from this branch; plugin registered from the LOCAL worktree marketplace in BOTH CLIs (claude `plugin marketplace add <wt>` + `plugin install tandem@tandem`; codex `plugin marketplace add <wt>` + `plugin add tandem@tandem`). Scratch project + session c7f12c0b052b.

Six fixes landed during the gate (all with tests, suite 935 green): 548e3d8 (codex `plugin add` spelling), 411ee78 (first mixed entry flips to first routable harness), 6bb399e (claude registry says "idle" not "waiting"), 882076e (non-claude readiness = output quiescence, not a fixed sleep), 9298082 (a paste counts only once the TUI echoes it; re-paste a swallowed one), 253372a (2.5s quiet threshold + inject diagnostics).

Plugin registration (probe 7): PASS. `codex plugin marketplace add <local path>` and `codex plugin add tandem@tandem` both succeed; installed plugin recorded in `~/.codex/config.toml` under `[plugins."tandem@tandem"]` (detection assumption confirmed). Claude registry shows tandem@tandem 0.5.0 from the directory marketplace with `UserPromptSubmit -> tandem hook-prompt || true`. `tandem doctor` reports "mixed-tab routing: claude hook installed / codex hook installed".

Per-probe:
- P1 boot + Ctrl-] cycle + mixed entry: PASS. Bar shows idle `mixed ○` in harness tabs; entering mixed from opencode (hookless, last) flips to claude (first routable) and reads `mixed ● claude`; frame file `{tab:mixed,focus:claude,routing_ok:true}`.
- P2(a) route claude→codex: PASS. Block notice `tandem: → codex — running there`; flip; prompt pasted+submitted; codex replied GATE-2-OK; claimed route file released.
- P2 residue (spec verify item 1): claude records a blocked prompt as a `type:"system"` entry, `subtype:"informational"`, `level:"warning"`, `preventContinuation:true`, content "UserPromptSubmit operation blocked by hook: tandem: → codex — running there / Original prompt: @codex …". It is NOT a user/assistant turn, does NOT sync to shadows (block notice absent from codex rollout), and is inert on resume. No converter filter needed.
- P2(b) route codex→claude + codex hook: PASS after 6bb399e. Codex honors the UserPromptSubmit block (route file appears; no native codex turn); its payload `session_id` equals tandem's recorded codex native id (identity gate passes). No double execution (codex rollout has GATE-2B only as a synced response_item, no native user_message).
- P3 model pins: PASS. `@codex:gpt-5.4-mini` → codex launched `resume <id> -m gpt-5.4-mini`, turn_context.model == gpt-5.4-mini. `@haiku` → claude `--resume … --model haiku`, assistant entry model claude-haiku-4-5. `@Opus` → `--model Opus` accepted, ran claude-opus-5 (case travels raw, harmless). Plain Ctrl-] flip after a pin relaunches WITHOUT `-m` (pin is per-launch).
- P4 multi-line paste: PASS. A 3-line `@codex` prompt arrived as one bracketed-paste block and submitted once.
- P5 opencode target + source: PASS after 882076e/9298082/253372a. `@opencode` delivers (readiness = TUI drew then 2.5s quiet, paste echo-verified). In opencode focus the bar shows `mixed ● opencode (no @-routing)`; `@claude` there runs natively with the prefix as literal text (opencode has no hook); Ctrl-] leaves to the claude harness tab.
- P6(a) cancel-in-flight: PASS. Route then immediate Ctrl-] either lands the flip or leaves no route file; no stuck request.
- P6(b) flip-away during settle: UNIT-VERIFIED, live inconclusive. The injector's flip-in-flight guard (bail before paste and before release, keep the file) and the preserved-prompt note are covered by test_deliver_holds_off_while_a_flip_is_in_flight and test_deliver_keeps_the_request_when_the_flip_lands_mid_paste. Live attempts were confounded: codex intermittently did not submit an `@opencode` prompt typed via tmux send-keys (text sat in its composer; a route was never written — verified with a route-file watcher showing the file never appeared), so no routed turn existed to interrupt. Not a tandem loss (tandem never saw a route); a gate-driving timing artifact.
- P7 resume: PASS ×4 — resume restores the mixed tab and focus, and the startup sweep clears/quotes leftovers. Exit stamp: leaving the mixed tab writes frame `{tab:harness,focus:"",routing_ok:false}`. SIGKILL sub-leg: not cleanly re-run live (attempts hit a stale orphan pid / focus-state timing); the startup sweep-with-note of a claimed leftover is covered by sweep_leftovers tests.
- P8 outside-tandem safety: PASS. After exit, `claude -r <same id>` outside tandem with an `@codex` prompt does NOT trigger the mixed-tab hook (no route file; frame stamped harness). (Claude separately chose its native `tandem:gpt` subagent — the pre-existing reroute feature — which is harmless and unrelated to mixed-tab routing.)

Verdict: PASS-WITH-FIXES. Core routing (both directions, all model-pin forms, multi-line, opencode) proven live after six fixes; two edges (P6b flip-away, P7 SIGKILL) rest on unit coverage. Local plugin registrations left in place; revert with `claude plugin marketplace remove tandem && claude plugin marketplace add Bhavya6187/tandem && claude plugin install tandem@tandem` and `codex plugin remove tandem@tandem && codex plugin marketplace remove tandem`.
