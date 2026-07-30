# Native tool-call translation — design

2026-07-29. Replaces the summarize-to-prose policy for tool activity in the
sync engine. Approved direction: native in **both** directions, with a
semantic mapping layer that re-expresses common tools in the shadow's own
vocabulary; no attribution tags on tool activity, no output clipping (both
revisitable).

## Why

Tool activity is most of a session: in the session that built tandem M1–M5,
93% of conversation bytes were tool results (92KB) vs 7KB of message text.
The current policy (`converter.py` + `summarize.py`) flattens each completed
call into clipped prose — `Read` results drop to `read <path>`, Bash output
is head/tail sampled, and codex's `update_plan`/`write_stdin`/`js` fall into
a generic clip. The shadow model gets a paraphrase of the work instead of
the work.

## Validation record (all live, this machine)

- **claude→codex** — vibeshub spike (2026-07-25, codex 0.137; amended
  2026-07-26, codex 0.145, see
  `vibeshub/webapp/backend/app/claude_to_codex_rollout.py`): fabricated
  rollouts resume; `function_call` items with foreign names (`Read`) and
  Claude `toolu_*` call ids ride along verbatim and their output reaches the
  model; `turn_context`/`base_instructions` optional; unpaired calls are
  rejected by the Responses API and must be closed with a placeholder
  output; `session_meta.model_provider` is required by the interactive
  `thread/resume` path on codex ≥0.145. Field-proven by rollout
  `019fa0eb-7438-…` (2026-07-27): a vibeshub export containing native
  `Read`/`Edit`/`exec_command` calls that Codex Desktop resumed for two
  full turns.
- **codex→claude** — tandem spike (2026-07-29, claude 2.1.220): a
  fabricated transcript with a synthetic uuid/parentUuid chain, codex-shaped
  call ids (`call_…`), and foreign tool names (`update_plan`, `apply_patch`
  as `tool_use` blocks) resumed cleanly via `claude --resume … -p`; all
  three planted markers in `tool_result` content were quoted back verbatim
  (exit 0, 3/3).

## Converter policy (`converter.py`)

`_apply_policy` changes for `ToolCall`/`ToolResult`; all other event kinds
keep their current handling (text tagged with attribution, thinking dropped,
compaction noted, system skipped).

- **ToolCall**: stash in `ctx.pending_calls[call_id]` (unchanged from
  today — full serialized call).
- **ToolResult**: pop the pending call and emit the mapped **call + result
  as an adjacent native pair** via `toolmap.py`. Pair-at-result emission
  keeps the converter's existing flow, and it means the mapper sees the
  result when rendering the call (needed for Write's `create` vs `update`,
  and lets the Edit→`apply_patch` patch use the result's `structuredPatch`
  context when present). Both replay APIs accept adjacent call/output
  pairs; claude writes them adjacently itself. If the call was never seen
  (orphan — e.g. cursor started mid-turn), keep the existing prose
  fallback (`summarize_orphan_result`): a bare `tool_result` with no
  preceding `tool_use` would itself break Claude resume.
- **Attribution**: tool calls and results carry no `[claude]`/`[codex]`
  tag. Text messages keep their tags unchanged.
- **No clipping**: outputs are carried verbatim. Revisit with a cap knob if
  shadow context bloat shows up in practice.

### Pairing invariant (dangle flush)

Both replay APIs reject a call without a result. With pair-at-result
emission a call is never in the shadow without its result under normal
flow; the invariant covers the interrupted cases: **whenever tandem drains
a source** (role switch, one-off routing, doctor repair), every entry left
in `ctx.pending_calls` is emitted as its mapped call plus a placeholder
result with output `(tool result not recorded)` (same wording as
vibeshub), then `pending_calls` is cleared. Likewise, when a ToolResult
entry fails translation, the pair is emitted as the mapped call plus that
placeholder output alongside the quarantine placeholder, so the shadow
never holds a dangling call.

## Tool mapping layer (`toolmap.py`)

Two tiers, decided per call by a new policy module `toolmap.py` (the
adapters stay dumb — they render whatever name/arguments the policy hands
them):

- **Tier 1 — semantic map**: re-express the call in the shadow's own tool
  vocabulary, so the history reads as work the shadow did with its own
  tools rather than a foreign dialect.
- **Tier 2 — verbatim pass-through**: name and arguments carried unchanged
  (spike-verified safe on both sides). This is the fallback whenever a
  Tier-1 rule doesn't apply cleanly.

**Honesty rule**: a Tier-1 mapping is only allowed when the target-native
rendering is a truthful description of what actually happened — arguments
derivable from the source call's own arguments, result content carried
verbatim. When a call instance doesn't fit (odd arguments, multi-file
patch, ranged read we can't express honestly), that instance falls back to
Tier 2. Mapping never fails an entry.

### Tier 1: claude→codex

| Claude call | Codex rendering |
| --- | --- |
| `Bash {command}` | `function_call exec_command {"cmd": …}`; output verbatim (no fake codex exit-code header) |
| `Read {file_path}` (no offset/limit) | `function_call exec_command {"cmd": "cat -n '<path>'"}` — Claude's Read output is already `cat -n`-shaped, so output rides verbatim |
| `Read` with offset/limit | equivalent `sed -n '<start>,<end>p'` pipe form; exact template an implementation choice — if it can't be expressed honestly, Tier 2 |
| `Write {file_path, content}` (result type `create`) | `custom_tool_call apply_patch`: `*** Begin Patch / *** Add File: <path> / +<content> / *** End Patch`; overwrites (result type `update`) → Tier 2 |
| `Edit {file_path, old_string, new_string}` | `custom_tool_call apply_patch`: `*** Update File: <path>` with `-`old / `+`new lines (V4A matches on context, so the strings map directly) |
| `Grep {pattern, …}` | `function_call exec_command` with the equivalent `rg …` command. The flag must match what the recorded output actually is: `output_mode` omitted (the schema default is `files_with_matches`) or `files_with_matches` → `rg -l`; `output_mode: "content"` → `rg -n`. `output_mode: "count"` → Tier 2 (a `path:<n>` match count would read as a line number under `rg -n`), and so does any `head_limit` (truncated output under a command implying the full result) |
| `Glob {pattern}` | `function_call exec_command` with the equivalent `rg --files -g …` command |
| `TodoWrite {todos}` | `function_call update_plan {plan: [{step, status}]}` — the status vocabularies (`pending/in_progress/completed`) already coincide |

### Tier 1: codex→claude

| Codex call | Claude rendering |
| --- | --- |
| `exec_command {cmd}` | `tool_use Bash {"command": …}`; exit-code header stripped into `toolUseResult {stdout, exitCode}`, `is_error` when non-zero |
| `apply_patch`, single `Add File` | `tool_use Write {file_path, content}`; `toolUseResult {type: "create", filePath, content}` |
| `apply_patch`, single `Update File` | `tool_use Edit {file_path, old_string, new_string}` — old = context+deleted lines, new = context+added lines, reconstructed from the hunk; `patch_apply_end` enrichment (success/changes), when seen, feeds `toolUseResult` |
| `apply_patch`, anything else (multi-file, `Delete File`, malformed) | Tier 2: `tool_use apply_patch {"input": <patch text>}` |
| `update_plan {plan}` | `tool_use TodoWrite {todos: [{content, status}]}`; result content verbatim |

### Tier 2 residue (measured, fine to leave verbatim)

Claude side: `Agent`/`Task`, `TaskCreate`/`TaskUpdate`, `AskUserQuestion`,
`SendMessage`, `Skill`, `ToolSearch`, `WebFetch`… Codex side: `js`,
`write_stdin`, `spawn_agent`/`wait_agent`/`close_agent`, `view_image`….
On the traces on this machine, Tier 1 covers ~87% of Claude-side call
volume (Bash+Edit+Write+Read of 152 calls in the M1–M5 session) and ~97%
of codex-side (exec_command+apply_patch+update_plan of ~930 calls).

## Codex rendering (`harness/codex.py`)

- ToolCall → `response_item` `function_call` `{name, arguments, call_id}`
  (call_id verbatim; `toolu_*` ids proven fine). ToolResult →
  `response_item` `function_call_output` `{call_id, output}`.
- No `event_msg` lines for tool activity (vibeshub-proven optional; codex's
  own history replay uses only `response_item`).
- **Bug-fix rider**: `create_shadow_transcript` gains
  `"model_provider": "openai"` in `session_meta`. Without it, interactive
  `codex resume` of a tandem-minted shadow fails on ≥0.145 with
  ``Model provider `` not found`` (-32600); tandem's M1 validation used
  `codex exec resume`, which tolerates the omission. (Codex caches
  `model_provider` per thread in `~/.codex/state_5.sqlite`; fresh tandem ids
  have no row, so the file value governs.)

## Claude rendering (`harness/claude_code.py`)

- ToolCall → `assistant` entry, one `tool_use` block, `stop_reason:
  "tool_use"`. ToolResult → `user` entry with a `tool_result` block plus a
  `toolUseResult` sibling. Both `is_error` and the `toolUseResult` shape
  come from the mapper: `{stdout, exitCode?}` for exec_command (via the
  existing `_codex_exec_output` header parser, `is_error` on non-zero),
  `{type: "create", filePath, content}`-style for the patch mappings,
  `{stdout}` for Tier-2 pass-through.
- **`message.id` runs**: consecutive rendered `assistant` entries share one
  `message.id` (new `ctx.claude_run_msg_id`, reset whenever a non-assistant
  entry is rendered). Real transcripts share one id across all blocks of an
  API response; per-entry ids would let a reader regroup blocks and strand a
  `tool_use` away from its `tool_result` (vibeshub's `assistant_msg`
  rationale, kept here).

## `SessionContext` changes

- `pending_calls` keeps its current shape (serialized ToolCall per
  `call_id`), including the `patch_apply_end`/`_structured` enrichment —
  the `apply_patch → Edit/Write` mapping consumes it for `toolUseResult`.
  No cursor migration needed.
- New field `claude_run_msg_id: str | None`.

## `summarize.py` shrinks

Retire `summarize_pair` and both per-harness summarizer families. Keep
`summarize_orphan_result` and `_codex_exec_output` (used by the Claude
renderer for `toolUseResult`/`is_error`). Module docstring updated to the
orphan-only role.

## Error handling

Quarantine pipeline unchanged (raw entry quarantined, one placeholder per
turn, sync continues) with the dangle-closing addition above.

## Testing

- Unit: Tier-1 mappings both ways (Bash↔exec_command; Edit/Write↔
  apply_patch incl. old/new-string ↔ hunk reconstruction round-trips;
  Read→cat -n; TodoWrite↔update_plan status vocab); honesty-rule fallbacks
  (overwrite Write, multi-file patch → Tier 2); verbatim pass-through incl.
  `call_id` preservation.
- Pairing: drain flush closes pending calls with placeholder outputs;
  orphan result renders as prose; result-translation failure closes its
  call.
- `message.id` runs: back-to-back tool calls share an id; a user entry
  resets it.
- Golden: regenerate converter goldens from `tests/golden/*-probe.jsonl`.
- Live check (doctor-style, manual): after a synced session with tool
  activity, resume both shadows — interactive `codex resume` (exercises the
  `model_provider` fix) and `claude --resume`.

## Out of scope (deliberate)

Attribution notes for tool activity; output-clipping knobs; Tier-1
mappings beyond the table above (e.g. `js`, `write_stdin`, subagent
tools); `event_msg` fidelity for how foreign items display in the codex
TUI transcript view.
