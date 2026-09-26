# Chat parity: history, slash typeahead, markdown and diffs, `/mode` — design

Status: design approved in brainstorm 2026-09-26; codex-reviewed the same day
(four section reviews, verified findings folded in); awaiting operator review. Builds on the unified chat window
(`docs/specs/2026-09-08-unified-chat-window-design.md`) and the model routes
(`/harness:model`); touches nothing in the native frame.

## Goal

Close the four gaps between `tandem` chat and the native claude and codex
TUIs that a user hits every session, with the least code that gets parity:

1. **History that survives the window.** Up/Down recalls prompts from
   earlier windows in the same directory, and Ctrl-R searches them.
2. **Slash discovery and harness built-ins.** Typing `/` lists what can be
   typed — tandem's commands, the routes, and the harness's own commands —
   and `/help` prints the same list. `/compact` and `/model` work on every
   harness.
3. **Readable replies.** Markdown is rendered as it is on the native TUIs,
   and a file edit shows its diff.
4. **`/mode`.** Plan, accept-edits, ask and skip, switched from the
   composer, mapped to what each harness supports.

The guiding rule is reuse: every feature is wiring around data the headless
protocols already hand tandem, plus one library for markdown. Nothing is
re-rendered after it is printed (the window's append-only rule stands), no
runtime gains a resident process, and nothing here changes what sync writes.

## Verified facts this design rests on (2026-09-26)

- claude 2.1.283 `-p --output-format stream-json` runs built-in slash
  commands from prompt text: `/compact` answered "Not enough messages to
  compact." on a fresh session and `/context` returned its usage table, both
  with `num_turns: 0`. Its `system/init` line carries `slash_commands`
  (list of names; the golden fixture shows `deep-research` and a plugin
  command) and `permissionMode`. `--permission-mode` accepts `acceptEdits`,
  `auto`, `bypassPermissions`, `manual`, `dontAsk`, `plan`.
- codex 0.155.1 app-server (schema generated with
  `codex app-server generate-json-schema`) has `thread/compact/start`,
  `model/list`, `skills/list`, `thread/rollback`, `turn/steer`.
  `turn/start` takes `approvalPolicy`; `thread/resume` and `thread/start`
  take `approvalPolicy` and `sandbox` (already used for skip-permissions).
  `AskForApproval` is `untrusted | on-request | never` or a granular
  object — there is no `on-failure` on the wire, whatever the TUI shows.
  `thread/compact/start {threadId}` returns an empty object; completion is
  the `thread/compacted` server notification (a compaction can also be
  framed as a turn: `turnKind: "compact"` exists in the schema).
  A `fileChange` item's `changes[]` carry `path`, `kind` and a unified
  `diff` string.
- opencode 1.18.31 serve: `GET /command` lists commands (name,
  description), `POST /session/{id}/command {command, arguments}` runs one,
  `POST /session/{id}/summarize {providerID, modelID}` compacts, the
  message body takes `agent` (agents present: `build`, `plan`, `explore`,
  `general`, …). The `/doc` schema types a tool part's completed `metadata`
  as an open object; whether the edit tool puts a diff there is checked at
  implementation.
- Sync already handles compaction: claude's `compact_boundary` and codex's
  `compacted` entries become a one-line attributed note in the other
  shadows (`converter.py`). `/compact` therefore needs no sync work.
- The state store's only destructive rule (`_schema_stale`) fires on a
  `sessions` table without `participants`; every table is created with
  `CREATE TABLE IF NOT EXISTS`, so a new table is additive and moves no
  database aside.
- `rich` is not installed; `pygments` already is (a transitive dev
  dependency). The composer's `@` picker (`composer.py`) already implements
  list, select, scroll and accept for a trigger word.

## Non-goals

- Image input, rewind/backtrack, `!` shell escape, background tasks, a
  runtime verbosity toggle, queue editing, `/new` or `/resume` inside the
  window. Real gaps, separately scoped.
- Re-rendering or collapsing anything already printed.
- Persisting `/mode` with the session (it is per window, like
  `/skip-permissions`).
- Reading the harnesses' own history files (`~/.claude/history.jsonl`,
  `~/.codex/history.jsonl`). Tandem keeps its own; mixing would surface
  prompts from windows that never ran through tandem.
- Making codex skills typeable. The app-server has no text-level skill
  invocation; codex's typeahead lists tandem's commands only.
- A separate codex `/approvals` command. Its three presets (read-only,
  auto, full access) are exactly `/mode plan`, `/mode edits` and
  `/mode skip`; `/mode ask` is "inherit the config".

## 1. History (`state.py`, `chat/composer.py`, `chat/window.py`)

**Storage.** A new table in the state store:

```sql
CREATE TABLE IF NOT EXISTS chat_history (
    id INTEGER PRIMARY KEY,
    cwd TEXT NOT NULL,
    text TEXT NOT NULL,
    ts TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS chat_history_cwd ON chat_history (cwd, id);
```

`StateStore.recent_prompts(cwd, limit)` returns the newest `limit` rows
oldest-first; `StateStore.add_prompt(cwd, text)` inserts — unless the newest
row for that cwd already holds `text`, matching the composer's own
consecutive-duplicate rule so a seeded list behaves like a live one — and
then deletes rows beyond the newest 500 for that cwd, in one `_tx`. Keyed by
cwd, like claude's, because tandem sessions are cwd-bound and a prompt from
another project is noise. The table joins `_SCHEMA` additively; nothing in
`_schema_stale` changes and no existing database is moved aside.

**Window.** At open, `run_chat` seeds `Composer(history=store.recent_prompts(
session.cwd, 200))`. `handle_input` records every `Submit` with
`store.add_prompt` as its first act, before `/quit` returns and before any
window command or dispatch runs — window commands and routes included,
since they were typed and are worth recalling. The store write is wrapped
so a failure is a note, never a lost turn. Only `Submit` is recorded:
approval keys and question answers arrive as `Answer` and never touch
history, and the window never reads `composer.text` to record anything.

**Composer.** `Composer.__init__` takes an optional seed list. Up/Down keep
their behavior (the seed is simply the initial `history`). Ctrl-R (0x12)
in prompt mode enters `search` mode:

- The composer row shows `(search) 'query': candidate`; the region is not
  touched.
- Printable input extends the query; Backspace shortens it; the candidate
  is the newest history entry containing the query (case-insensitive),
  searched from the position last shown so Ctrl-R again steps to the next
  older match, wrapping to the newest with a `(wrapped)` marker.
- Enter or Tab leaves search with the candidate as the draft, cursor at
  the end. Esc leaves search and restores the draft that was there before
  Ctrl-R. Any arrow key leaves search keeping the candidate, then applies.
- With no match the candidate is empty and the row reads
  `(search) 'query': (no match)`; Enter then restores the original draft.
- In approval or question mode Ctrl-R is ignored, as other edits are.

`rows()` returns the single search row when in search mode, with the
cursor after the query.

Ordering and state rules, so search cannot fight the pickers:

- In `feed`, the search-mode branches come before the picker branches:
  while searching, Esc/Enter/Tab/arrows mean what the list above says,
  never "close the picker" or "accept a path". Ctrl-R with a picker open
  closes the picker first, then enters search.
- Paste mode is untouched: a pasted 0x12 or escape sequence is literal
  text, as every pasted byte is today.
- `begin_approval`, `begin_question`, `end_answer` and `_set` reset the
  history cursor (`_hidx`, `_draft`) and the vertical-motion goal
  (`_goal`): today they survive a mode change, so Up after answering a
  question resumes from a stale index, and a recalled multi-line entry can
  inherit an old column.

## 2. Slash typeahead, `/help`, `/compact`, `/model` (`chat/commands.py` new, `chat/composer.py`, `chat/window.py`, `chat/dispatch.py`, `chat/events.py`, runtimes)

**Catalog.** A new module `chat/commands.py` owns one dataclass and one
function:

```python
@dataclass(frozen=True)
class Command:
    name: str          # without the slash
    description: str
    origin: str        # "tandem" | "route" | harness name

def catalog(participants, default, harness_commands: dict[str, list[Command]]) -> list[Command]
```

The catalog for a window is, in order: tandem's window commands (`help`,
`status`, `mode`, `skip-permissions`, `compact`, `model`, `note`, `quit`)
with one-line descriptions; a route per participant (`/claude`, `/codex`,
`/opencode`, described as "route the prompt here and make it the default;
`:model` pins"); then the default harness's own commands. Only the default
harness's commands are listed — a slash command goes where the prompt
goes.

**Harness commands, per runtime.**

- claude: `ClaudeRuntime` records `slash_commands` from every non-child
  `system/init` line it sees (`on_init` already receives the session id;
  it grows to receive the list). The window holds the latest list per
  harness. Until claude has run once in this window the list is the
  verified built-ins alone: `compact`, `context`. Those two are always
  present, first. Names arrive without descriptions; the row shows
  "claude command".
- opencode: `OpencodeRuntime.commands()` does `GET /command` once per
  server and caches it; the window asks after each opencode turn (the
  server exists only from the first turn). Each has a description.
- codex: none (see Non-goals).

**Picker.** The composer's mention picker becomes a general one with an
explicit kind. Today `_mention()` finds the `@` word under the cursor and
`_list_paths` supplies path strings, and Esc/Enter/Tab/Up/Down all assume
that. The generalization is a `_picker` attribute holding `None`, `"path"`
or `"command"`, set by one `_locate()` that replaces `_mention()`: a `/` at
buffer index 0 with the cursor inside the first word is a command picker;
an `@` word under the cursor anywhere is a path picker; else none. Matching,
acceptance, dismissal and row rendering each branch on the kind — paths
keep `match`, `_accept` and `_printable` exactly as they are; commands get
their own three small counterparts. The composer takes a second callable,
`commands: Callable[[], list[Command]]`. Command rows render `/name`
padded to the longest name, then the description dimmed and clipped.
Command matching is prefix on the name, case-insensitive. Accepting inserts
`/name ` (a trailing space) and closes the picker; the `_dismissed` rule
(Esc closes the picker for this word until the word changes) carries over.
`Enter` with the picker open accepts, as it does for paths; `Enter` on a
fully typed name that is the sole candidate submits — the same "a path
typed out in full is not offered back" rule.

A first word that is not a bare name — one carrying `:` (`/codex:gpt-5.5`,
`/codex:default`, `/opencode:openrouter/anthropic/claude-sonnet-4`), a
second `/`, or any character outside `[A-Za-z0-9_-]` — closes the command
picker, so Enter submits the raw text and `parse_route` sees it untouched.
These three routes, the `/codex/README.md` non-route and the `@` picker's
existing tests are the regression set for the generalization.

**Routing.** `window_command` grows to the new names. Everything not in the
catalog still goes to the harness verbatim (a claude skill not yet in the
list, a codex `/word`), so nothing that works today stops working.

**`/help`.** Prints the catalog into the region, grouped by origin, one
row per command, dimmed descriptions.

**`/compact`.** Not a window-level intercept: `window_command` recognizes
it only to hand it to the dispatcher, which resolves the target harness
(the default, or the route in front of it: `/codex /compact` is legal)
before anything harness-specific happens. `Pending` gains
`command: str = ""`; `RuntimeClient.run_turn` gains `command: str = ""`.

- claude: `run_turn(command="compact")` sends the text `/compact` as the
  user message (verified to run as the built-in). Everything else is an
  ordinary turn.
- codex: after `thread/resume`, `run_turn` sends `thread/compact/start
  {threadId}` instead of `turn/start`. The response is an empty object;
  the turn ends on the first of `thread/compacted` for this thread or
  `turn/completed` for this thread (the schema allows a compaction framed
  as a turn), with the existing 60 s `_call` timeout as the backstop.
  `handle` learns `thread/compacted` as a terminal notification only while
  a compact command is running; outside one it stays ignored.
- opencode: `ensure_server` runs as for a turn (so the server exists), then
  `POST /session/{id}/summarize {providerID, modelID}`. The ids come from
  the model pin when set, else from `info.providerID` / `info.modelID` of
  the last assistant message this runtime saw (kept from each `run_turn`
  response — new state); with neither, the command fails with "pin a model
  or run one opencode turn first". The turn ends when the POST returns.
- Dispatcher: a command turn goes through validation, `prepare_turn`, the
  navigator lock and `sync_after_turn` like any turn, but it takes no
  navigator note (`nav.take` is skipped, so a pending note keeps waiting
  for a real prompt), builds no `FactsCollector`, and hands nothing to
  `nav.turn_ended`. The window shows it as `you → codex  /compact` with
  the usual closing row.

**`/model`.**

- `/model` alone lists the default harness's models, one per row: codex
  from `model/list` (a one-off app-server process, no thread), opencode
  from `GET /api/model` after `ensure_server` (`provider/model`, only
  providers with credentials), claude from `modelcat`'s family aliases
  (`fable`, `opus`, `sonnet`, `haiku`) since headless claude has no list
  call. The pinned model, if any, is marked.
- Listing blocks on a child process or an HTTP call, so it never runs on
  the main thread. It is a `Pending(command="models")` on the dispatcher:
  queued behind a running turn like a prompt, run on the worker, and it
  skips the whole transcript pipeline (no validation, no `prepare_turn`,
  no sync, no navigator, no `TurnStarted`). Its result comes back as a new
  `Notice(text)` live event the renderer paints dim, the same way
  `screen.note` prints today; failures come back as `Failure`.
- `/model NAME` is exactly `/{default}:NAME` — same parser, same pin, same
  errors. `/model default` clears the pin. Documented as the alias it is.

## 3. Markdown and diffs (`chat/render.py`, `chat/events.py`, runtimes, `pyproject.toml`)

**Dependency.** `rich>=13` joins `dependencies`. It brings `markdown-it-py`
and `pygments`; the lock is regenerated in the same commit as the
`pyproject.toml` change (release-mechanics rule).

**Markdown.** `Screen` gains a paragraph buffer for the streaming reply:

- `text_delta` appends to the buffer instead of printing, when
  `cfg.markdown` is true. The buffer is flushed — rendered and printed —
  when it contains a blank line outside a fenced code block (everything up
  to and including that blank line goes out; the rest stays), when a fence
  closes, and whenever anything else is about to reach the region. That
  last rule is one idempotent `_flush()` called at the top of every
  painter that writes there — `thinking_delta`, `tool_started`,
  `tool_output`, `tool_finished`, `file_diff`, `approval`, `question`,
  `failure`, `review`, `note`, `turn_started`, `turn_finished` — and at the
  exit of `history()`. `turn_started` also resets the buffer and fence
  state, so an interrupted turn cannot leak text into the next.
- Fence tracking looks only at complete lines (the unterminated tail
  waits for its newline, so a marker split across deltas is never
  misread). An opener is up to three spaces of indent then a run of three
  or more backticks or tildes; the closer is the same character with a run
  at least as long, under the same indent rule. Anything else inside the
  fence is code, blank lines included.
- Rendering: a `rich.console.Console(file=StringIO(), width=self.cols - 1,
  force_terminal=color, color_system="standard" if color else None,
  no_color=not color, highlight=False)` built at flush time — never cached
  — so a resize between buffering and flushing renders at the width
  `print` is tracking. It prints `rich.markdown.Markdown(text,
  code_theme="ansi_dark")`. Each output line is then cleaned in this
  order: OSC sequences removed (rich emits `\x1b]8` hyperlinks; the
  renderer's own escape whitelist is SGR only, and `_cells` would count
  OSC bytes), then trailing whitespace removed even when it sits before a
  trailing run of SGR sequences (rich pads code blocks to the full width
  and closes with a reset, so a plain `rstrip` finds no space; a row that
  fills `cols` trips `print`'s edge rule) with the SGR kept. The lines go
  through `print` as one block. `_safe` runs on the source text before
  rich, never after.
- `cols - 1` leaves the last cell free so no rendered row is exactly
  `cols` wide.
- Thinking deltas are unchanged: dim raw text, printed as they come.
- `history()` renders `AssistantMessage.text` through the same path, so a
  resumed window's back-scroll looks like the live one.
- `show_thinking` and `markdown` are independent. `[chat] markdown = false`
  restores today's raw streaming.

**Trade-off stated.** Text appears per paragraph rather than per token. The
separator's activity line and timer already cover "is it working"; the
native TUIs also commit markdown at block boundaries.

**Diffs.** A new live event:

```python
@dataclass(frozen=True)
class FileDiff:
    call_id: str
    path: str
    diff: str        # unified diff text, hunks included, no file header needed
```

`FileDiff` joins the `LiveEvent` union. Runtimes emit it after the
`ToolFinished` of a successful edit, never before it and never for a
failed one:

- codex: `fileChange` on `item/completed` emits one `FileDiff` per change
  that carries a `diff`. The `item/fileChange/outputDelta` stream is no
  longer forwarded as `ToolOutput` (a patch's streamed output is the same
  diff, which the eight-line tail cap truncated; the structured
  `changes[].diff` is authoritative). Command output streaming is
  unchanged.
- claude: `ClaudeRuntime` remembers each `Edit`/`Write`/`MultiEdit`
  `tool_use` input by id (already summarized at `ToolStarted`) and drops
  them at turn end. On the matching non-error `tool_result` it emits one
  `FileDiff`. `Edit` gives `difflib.unified_diff(old_string.splitlines(),
  new_string.splitlines(), lineterm="", n=2)` with the two `---`/`+++`
  header lines dropped and a hunk header of the form `@@ edit @@`
  (`@@ edit · replace_all @@` when `replace_all` is set — the diff shows
  one occurrence, the tool changed them all). `MultiEdit` does the same
  per entry of `edits[]`, numbered `@@ edit 1 @@`. `Write` emits the new
  text as added lines under `@@ new file @@`. These are snippet diffs, not
  file diffs: no line numbers, no surrounding file context — the header
  form says so. Each is capped to `diff_lines` lines before it is built
  up, so a large write never builds a large string.
- opencode: `TurnState` keeps `call_id → path` from the `edit`/`write`
  input and a `finished` set, so a repeated completed `message.part.updated`
  for the same call emits `ToolFinished` and `FileDiff` once. If the
  completed part's `metadata` (or its state's) holds a `diff` string it
  is emitted; else nothing, and the tool output tail still prints. Which
  of the two carries it is checked at implementation against 1.18.31.

`Screen.file_diff` prints after the tool's status row: `    --- path` dim,
then each diff line — `+` green, `-` red, `@@` dim, others plain —
indented four cells, `_safe`d, clipped to the row, capped to
`[chat] diff_lines` (default 40) with the `… +N lines` trailer the tool
tail uses. With `diff_lines = 0` no diff is painted.

## 4. `/mode` (`chat/window.py`, `config.py`, runtimes)

**Command.** `/mode` prints `mode ask · claude default · codex on-request/
workspace-write · opencode build`. `/mode ask|edits|plan|skip` sets the
window's mode from the next turn (a running turn keeps the one it started
under — the existing `set_cfg` path, which replaces each runtime's `cfg`
reference; every runtime reads its cfg once, at the top of `run_turn`,
before anything blocks, so a turn already spawned is unaffected).
`/skip-permissions [on|off]` stays as an alias: `on` is `/mode skip`,
`off` is `/mode ask`, and it reports what it set. Both commands go through
one `set_mode` on the window.

**Source of truth and precedence.** A new `ChatConfig.mode` string is the
source; `ChatConfig.skip_permissions` stays as the wire-level bool the
runtimes read today and is always `mode == "skip"`, derived at load and
by `set_mode`. The initial mode is, in order: the launch flag
(`--skip-permissions` → `skip`, `--no-skip-permissions` → `ask`), else an
explicit `[chat] mode`, else the legacy top-level `skip_permissions =
true` → `skip`, else `ask`. The legacy key therefore keeps working
unchanged; a `[chat] mode` that is not one of the four words is ignored
with the loader's usual silence.

**Bar marks.** Per harness, the mark is the mode word for every mode but
`ask`; a `?` suffix when that harness cannot honor the mode and runs as
`ask` (opencode `edits?`, `skip?`); `cfg` when an explicit `[chat]
codex_approval_policy` / `codex_sandbox` overrides the mode's codex column
(the config decides, as it does over skip today). `/status` prints the
mode and the same per-harness words.

**Mapping.**

| mode  | claude `--permission-mode` | codex `approvalPolicy` / `sandbox` | opencode |
|-------|----------------------------|-------------------------------------|----------|
| ask   | (none: default)            | inherit config (today's behavior)   | (none)   |
| edits | `acceptEdits`              | `on-request` / `workspace-write`    | (none) — bar shows `edits?` |
| plan  | `plan`                     | `on-request` / `read-only`          | `agent: "plan"` on every message |
| skip  | `bypassPermissions`        | `never` / `danger-full-access`      | (none) — bar shows `skip?` |

The codex column is its `/approvals` presets: `edits` is codex's "auto"
(edits inside the workspace apply without asking, commands run in the
sandbox, escalations ask), `plan` its "read-only" (every write asks),
`skip` its "full access". `on-failure` is not on the 0.155.1 wire and is
not used. An explicit `[chat] codex_approval_policy` / `codex_sandbox`
still wins over the column, as it wins over skip today. Opencode's `agent`
rides each plan-mode message body and nothing is posted to
`/session/{id}/agent`; other modes send no `agent`, and the live gate
checks that a per-message agent does not stick to the session.

**Plan mode and the transcript.** claude in plan mode writes its plan into
the reply; codex in read-only sandbox asks before every write, which the
approval row already handles; opencode's plan agent is read-only by its
own config. Nothing new reaches sync.

## Delivery

One spec, four PRs, in this order, each rebased on the last:

1. **Slash typeahead, `/help`, `/compact`, `/model`** (section 2) — unlocks
   the most, touches the composer picker the others build on.
2. **History and Ctrl-R** (section 1).
3. **`/mode`** (section 4).
4. **Markdown and diffs** (section 3) — the dependency change last, so the
   earlier PRs ship without it.

Each PR: TDD in the existing `tests/test_chat_*.py` files, the golden
fixtures extended where a new wire line is consumed (claude init with
`slash_commands`; codex `thread/compacted`; opencode `/command`), a live
gate on the three binaries, and a codex review whose findings are verified
before they are acted on. Docs: `docs/configuration.md` `[chat]` section
and the README chat paragraph gain the new keys and commands in the PR that
adds them.

## Testing

- Composer: search mode transitions (enter, narrow, step, wrap, accept,
  cancel, no match, ignored in approval mode); the `/` picker opening only
  at index 0, prefix matching, accept inserting `/name `, Enter on a sole
  exact candidate submitting; the `@` picker unchanged.
- State: `add_prompt`/`recent_prompts` round-trip, the 500 cap, cwd
  isolation, schema bump.
- Window: history seeded and appended (also for `/quit`, never for
  answers); `/help`, `/mode`, `/model`, `/compact` routing including
  `/codex /compact`; bar marks per mode with `?` and `cfg`; the
  `/skip-permissions` alias; mode precedence at load.
- Runtimes: claude `slash_commands` captured from init, `FileDiff` from an
  Edit and a Write, `--permission-mode` per mode; codex `compact` request
  sequence against the fake server, `FileDiff` from `fileChange`, policy
  and sandbox per mode; opencode `commands()`, command endpoint dispatch,
  summarize body, `agent` on plan.
- Renderer: paragraph flush rules (blank line, fence open and close with
  mixed markers and indents, a marker split across deltas, every painter
  flushing first, interrupted turn, history exit), width after a mid-turn
  resize, padding-before-SGR and OSC stripped, `markdown = false`
  byte-identical to today; diff coloring, header forms and cap.
- Dispatcher: a command `Pending` takes no navigator note and builds no
  facts; `models` skips the transcript pipeline; `Notice` reaches the
  window.
- Live gate: one scripted window per harness exercising `/help`,
  `/compact`, `/model`, `/mode plan` with an edit request, and a markdown
  reply with a code block.

## Follow-ups (not in this spec)

- Pre-first-turn claude command list from `~/.claude/commands` and
  `.claude/commands`.
- Codex skills in the typeahead once the app-server exposes a text-level
  invocation.
- Image input; rewind via `thread/rollback`; `!` shell via
  `thread/shellCommand` and opencode's `/shell`.
