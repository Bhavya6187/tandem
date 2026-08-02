# Observed session formats

Everything tandem knows about the two native session formats was observed on
this machine by creating throwaway sessions and reading the files the CLIs
wrote. Versions observed:

| CLI | version | session storage |
| --- | --- | --- |
| Claude Code (`claude`) | 2.1.220 | `~/.claude/projects/<munged-cwd>/<sessionId>.jsonl` |
| Codex CLI (`codex`) | 0.145.0 | `~/.codex/sessions/YYYY/MM/DD/rollout-<YYYY-MM-DDThh-mm-ss>-<uuidv7>.jsonl` |

Env overrides honored: `CLAUDE_CONFIG_DIR` (claude home), `CODEX_HOME` (codex
home), `TANDEM_HOME` (tandem state).

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
      call_id-matched `custom_tool_call_output`, whose output text contains
      `patch rejected`. That pair is what `ops.blocked_write_paths` matches.
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
