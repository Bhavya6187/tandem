# Native tool-call translation — design

2026-07-29. Replaces the summarize-to-prose policy for tool activity in the
sync engine. Approved direction: native in **both** directions, no
attribution tags on tool activity, no output clipping (both revisitable).

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

- **ToolCall**: emit natively (no longer stash-only). Also record
  `ctx.pending_calls[call_id] = {"tool": name}` — needed for orphan
  detection, `is_error` derivation, and the flush rule below.
- **ToolResult**: if its call is pending, pop it and emit natively. If the
  call was never seen (orphan — e.g. cursor started mid-turn), keep the
  existing prose fallback (`summarize_orphan_result`): a bare `tool_result`
  with no preceding `tool_use` would itself break Claude resume.
- **Attribution**: tool calls and results carry no `[claude]`/`[codex]`
  tag. Text messages keep their tags unchanged.
- **No clipping**: outputs are carried verbatim. Revisit with a cap knob if
  shadow context bloat shows up in practice.

### Pairing invariant (dangle flush)

Both replay APIs reject a call without a result. New invariant: **whenever
tandem drains a source** (role switch, one-off routing, doctor repair), every
entry left in `ctx.pending_calls` is closed by appending a native result
with output `(tool result not recorded)` (same wording as vibeshub) to the
shadow, then `pending_calls` is cleared. Similarly, when a ToolResult entry
fails translation, its quarantine placeholder is accompanied by a native
placeholder output for that `call_id`, so the shadow never holds a dangling
call across a translation failure.

## Tool-name mapping

Verbatim pass-through is the default in both directions (spike-verified on
both sides); only the shell tools get an argument-shape mapping.

| direction | source | target |
| --- | --- | --- |
| claude→codex | `Bash {command}` | `function_call exec_command {"cmd": …}` |
| claude→codex | any other tool | `function_call`, name verbatim, `arguments` = JSON-stringified input |
| codex→claude | `exec_command {cmd}` | `tool_use Bash {"command": …}` |
| codex→claude | `custom_tool_call` (`apply_patch`) | `tool_use`, name verbatim, `input {"input": <patch text>}` |
| codex→claude | any other `function_call` | `tool_use`, name/args verbatim |

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
  "tool_use"`. ToolResult → `user` entry with a `tool_result` block
  (`is_error` when a codex exit-code header parses non-zero) plus a
  `toolUseResult` sibling `{stdout, exitCode?}` — reuse the existing
  `_codex_exec_output` header parser.
- **`message.id` runs**: consecutive rendered `assistant` entries share one
  `message.id` (new `ctx.claude_run_msg_id`, reset whenever a non-assistant
  entry is rendered). Real transcripts share one id across all blocks of an
  API response; per-entry ids would let a reader regroup blocks and strand a
  `tool_use` away from its `tool_result` (vibeshub's `assistant_msg`
  rationale, kept here).

## `SessionContext` changes

- `pending_calls` values shrink to `{"tool": name}`. The
  `patch_apply_end`/`_structured` enrichment is dropped — outputs now ride
  verbatim, so nothing downstream consumes it. Cursor migration: values are
  read via `.get("tool")`, so full ToolCall dumps persisted by older cursors
  load fine.
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

- Unit: argument-shape mapping both ways; verbatim pass-through incl.
  `call_id` preservation; `custom_tool_call` → `{"input": …}`.
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

Attribution notes for tool activity; output-clipping knobs; semantic
mapping of `apply_patch` onto Claude's `Edit`/`Write`; `event_msg` fidelity
for how foreign items display in the codex TUI transcript view.
