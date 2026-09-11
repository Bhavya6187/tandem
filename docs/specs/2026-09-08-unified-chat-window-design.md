# Unified chat window — design

Status: spec approved in brainstorm 2026-09-07/08, awaiting operator review.
Supersedes the flip-and-inject dispatch of the parked mixed-tab routing spec
(`docs/specs/2026-08-21-mixed-tab-routing-design.md`); reuses its prefix
grammar with the sigil changed from `@` to `/` (decided 2026-09-10: `@` is
the file-mention sigil in all three TUIs and still means that headless).

## Goal

One window, one composer, three harnesses. The user types a prompt; it runs
on the harness that ran the last one, unless the prompt starts with
`/claude`, `/codex`, or `/opencode`, in which case it runs there and that
harness becomes the new default. Every turn executes inside the harness's
own engine (its tools, permissions, hooks, MCP servers, instructions files,
session store) and is rendered by tandem in a shared conversation view.

The routine operation is "route the next prompt", not "flip the terminal".
Routed turns must not pay a native-TUI boot or a pty handover.

## Non-goals (v1)

- Automatic routing. Manual prefixes only; the decision function comes
  later and plugs in where the prefix parser returns "no prefix".
- Flipping into a native TUI from inside the chat window. `tandem` (the
  frame) remains the way to use a native TUI on the same session.
- Subagent routing, the `tandem sub` path, and plugin hooks. Untouched.
- Collapsing or re-rendering tool output after it has been printed.
- Filtering claude's synthetic resume entries out of sync (see Follow-ups).

## Decisions and their reasons

- **Programmatic runtimes, resume at dispatch time.** Spike 2026-09-06
  (memory: `headless-dispatch-spike`) ran a headless claude → codex →
  opencode → claude relay on a tandem-synced session. Overheads excluding
  model latency: codex app-server spawn 0.03 s + `thread/resume` 0.68 s;
  claude `-p` spawn-with-resume 0.8–1.3 s (3.9 s on a cold cache);
  opencode `serve` 0 per turn (0.64 s once). Approval round-trips worked on
  all three. Because every turn resumes its thread from disk at dispatch,
  no process is ever stale: the resident-standby failure modes (respawn
  churn, stale in-memory context, killed-mid-turn transcript forks) do not
  arise.
- **No resident claude or codex process.** A pre-spawned headless claude
  runs the orphaned-background-task scan at connect and starts a turn on
  its own (spike-proven: "Continue from where you left off." + a synthetic
  reply). Codex holds the thread writer lock for the process lifetime and
  `thread/unsubscribe` does not release it. One process per turn avoids
  both; the spawn costs are already sub-second.
- **Hand-rolled protocol clients, no new dependencies.** The Agent SDK
  pulls in `mcp`, which drags starlette, uvicorn, httpx and opentelemetry.
  tandem's declared dependencies stay `click`, `pydantic`, `watchdog`,
  `pexpect`. The SDK source (0.2.152 at spike time) is the reference spec
  for the claude wire protocol; codex is JSON-RPC over stdio and opencode
  is HTTP + SSE, both `stdlib`.
- **Raw ANSI renderer on the main screen**, same discipline as the frame:
  a scroll region above a fixed bottom block, so native scrollback and the
  mouse wheel keep working and nothing has to emulate a terminal.
- **Sticky default = the session's `active` column.** `tandem resume`, the
  frame, and the chat window then agree on which harness is current.

## Architecture

```text
 ┌───────────────────────── tandem chat ──────────────────────────┐
 │ composer ──prefix grammar──▶ dispatcher ──▶ runtime client      │
 │    ▲                             │  ▲          │ (per harness)  │
 │    │ answers (approve/question)  │  │ live     ▼                │
 │ renderer ◀───────────────────────┘  │ events  claude -p         │
 │    ▲ history (adapter.parse_entry)  │         codex app-server  │
 │    │                                │         opencode serve    │
 │ status bar (StatusBar + ratelimit + usage meters)               │
 └─────────────────────────────────────────────────────────────────┘
   sync: drain_source / fast_forward / echo suppression (ops.py) — unchanged
```

Four units. Each has one job, one interface, and can be tested alone.

### 1. Composer, prefix grammar, and slash pass-through (`chat/composer.py`, `promptroute.py`)

Single-line editor at the bottom row: cursor movement, backspace, history
(up/down), Enter submits, Ctrl-L repaints, Ctrl-C ladder (see Errors).
Bracketed paste is accepted; newlines inside a paste stay in the prompt and
the row shows the first line plus `(+N lines)`.

Grammar (lifted from the parked branch, `promptroute.py` + its tests,
sigil swapped to `/`):

```text
prompt   := route? text
route    := "/" harness (":" model)? (whitespace | end)
harness  := "claude" | "codex" | "opencode"
model    := [A-Za-z0-9._/-]+
```

- The route is recognized only at the start of the prompt and only for
  the three harness names, followed by whitespace, end of input, or
  `:model`. `/codex/README.md …` is not a route (a slash follows the
  name); `/claude` inside prose is literal text.
- `/codex fix the tests` → runs on codex, codex becomes the default.
- `/codex` alone → codex becomes the default, no turn.
- `/codex:gpt-5.5 …` → runs on codex with that model and pins it for codex
  until changed. `/codex:default` clears the pin. `/codex:sol` resolves
  through the codex catalog (`modelcat.py`) as on the parked branch;
  claude models pass through (`haiku`, `sonnet`, full ids); opencode takes
  `provider/model`.
- The default harness is `session.active`. Model pins are per (session,
  harness) in a new `chat_pins` table (see State).

Slash pass-through: any other `/word` at the start of a prompt is not
tandem's. It is sent to the current harness unchanged, so the harness's
own slash commands and skills keep working (claude `-p` runs skills and
the built-ins it supports from prompt text; codex and opencode behavior is
confirmed at implementation and documented). Tandem's own window commands
(`/quit`, `/status`) are the only other reserved names and are listed in
the docs. No known harness command uses the three harness names; the
tandem plugin's commands are namespaced under `tandem:`.

File mentions: `@path` is never interpreted by tandem. The prompt reaches
the harness verbatim. Headless claude expands `@file` mentions itself
(verified 2026-09-10: `claude -p "@mention.txt …"` answered from the file
in one turn with no tool call). Codex and opencode receive the mention as
text and read the file with a tool, which is an accepted v1 degradation
for opencode, whose TUI would have sent a file part.

### 2. Runtime clients (`chat/runtime/{claude,codex,opencode}.py`)

One interface:

```python
class RuntimeClient(Protocol):
    def run_turn(self, session, native_id, prompt, model,
                 events: Callable[[LiveEvent], None],
                 answers: Answers) -> TurnOutcome: ...
    def interrupt(self) -> None: ...
    def close(self) -> None: ...
```

`LiveEvent` is a small vocabulary, deliberately narrower than
`events.NormalizedEvent`:

| event | payload |
|---|---|
| `text_delta` | text |
| `thinking_delta` | text (rendered dim, may be dropped by config) |
| `tool_started` | call id, tool name, one-line argument summary |
| `tool_output` | call id, text chunk |
| `tool_finished` | call id, ok/error, one-line result summary |
| `approval` | kind (command / file change / permission), detail, choices |
| `question` | prompt text, options |
| `turn_finished` | status (completed / interrupted / failed), usage |
| `error` | message |

`Answers` is the renderer's callback object: `approve(kind) -> "allow" |
"deny" | "always"` and `answer(question) -> str`. A client blocks its
protocol loop on these; the dispatcher runs the client on a worker thread
so the main thread keeps drawing.

Child environment for every client: the user's environment minus any
`CLAUDE*` variables (the frame's pty probes found an inherited marker
changes how claude handles quit keys; the spike stripped them too).

**Claude** — process per turn.

```text
claude -p --resume <sid> --input-format stream-json --output-format stream-json
       --verbose --include-partial-messages --permission-prompt-tool stdio
       --setting-sources user,project,local [--model <pin>]
```

`--session-id <sid>` replaces `--resume` while the transcript does not
exist yet (fresh claude side, same rule as `interactive_argv`). The client
writes one `user` message, then reads newline-delimited JSON: `system/init`
(model, commands), `stream_event` (text deltas), `assistant` and `user`
(tool calls and results), `control_request` with subtype `can_use_tool`
(answered by a `control_response` carrying `allow`/`deny`; "always" adds a
session-scoped allow rule for that tool in the same response), `result`
(usage, turn end). Interrupt is a `control_request` of subtype
`interrupt`. After `result`, stdin closes and the process is waited on;
exit is the turn boundary, so no Stop-hook sentinel is wired. Message
shapes are pinned in the client module against the SDK version and the
claude version range in `compat.py`.

**Codex** — process per turn, JSON-RPC 2.0, newline-delimited over stdio.

```text
initialize {clientInfo}   → initialized (notification)
thread/resume {threadId, cwd}           # or thread/start {cwd} when no id yet
turn/start {threadId, input:[{type:"text", text}], model?}
… notifications …                        → turn/completed
```

Approval policy and sandbox are not passed, so the user's `config.toml`
applies; `[chat]` may override both. Consumed notifications:
`item/agentMessage/delta`, `item/started`, `item/completed`,
`item/commandExecution/outputDelta`, `item/fileChange/*`,
`turn/completed`, `thread/tokenUsage/updated`,
`account/rateLimits/updated` (fed straight to the codex bar slot). Server
requests answered: `item/commandExecution/requestApproval`,
`item/fileChange/requestApproval`, `item/permissions/requestApproval`
(decisions `accept` / `decline` / `acceptForSession`),
`item/tool/requestUserInput`. Interrupt is `turn/interrupt`. On
`turn/completed` stdin closes; the process gets 5 s, then the SIGTERM
ladder. A fresh codex side uses `thread/start` and records the returned
thread id with `store.set_native_session_id` — no rollout-directory watch
needed.

Per-turn model overrides trigger a context compaction inside codex
(spike: 17 s to first token instead of 5 s). Pins are sticky per harness
partly for this reason; the renderer shows the compaction item so the wait
is explained.

**Opencode** — one `serve` for the life of the chat window.

At start, when opencode participates: bind a free localhost port, release
it, spawn `opencode serve --port N --hostname 127.0.0.1` in the session
cwd, poll `/global/health` for up to 10 s. One reader thread on
`GET /event` (SSE) for the whole window. A turn is
`POST /session/<sid>/message {model?, parts:[{type:"text", text}]}` on a
worker (it blocks until the turn ends). Events consumed:
`message.updated` (to learn assistant message ids), `message.part.delta`
(text), `message.part.updated` (tool parts: name, state, output),
`permission.asked` → `POST /permission/<id>/reply {reply}` (`once` /
`reject` / `always`), question events (name confirmed from the server's
`/doc` at implementation) → `POST /session/<sid>/question/<id>/reply`. Interrupt is
`POST /session/<sid>/abort`. Close sends SIGTERM to the server. Default
opencode config auto-allows `bash`; a permission prompt appears only when
the user's opencode config asks for one, which is opencode's own rule.

### 3. Dispatcher (`chat/dispatch.py`)

The bookkeeping already in `ops.run_oneoff`, with a streaming runner in
place of the blocking subprocess and one addition: a same-thread guard.

```text
submit(prompt)
  parse prefix → (target, model) ; bare prefix → set_active, return
  if a turn is running → queue (renderer shows "queued → codex")
  validate target transcript (adapter.validate_transcript) → report, abort
  drain_source(active, flush_dangling)          # shadows catch up
  fast_forward_all(target) if target != active
  outcome = client.run_turn(...)                 # worker thread
  set_active(target); persist model pin
  drain_source(target) + echo suppression       # as run_oneoff
  feed usage meters; refresh bar
  dispatch next queued prompt
```

Same-thread guard: before a codex turn, any codex process the dispatcher
itself started for this thread must be dead (it always is after a normal
turn; the guard covers a crashed client). A lock held by a process tandem
did not start is reported, never killed.

One turn at a time across all harnesses, matching tandem's model. Esc
interrupts the running turn; the queue is not flushed by an interrupt.

### 4. Renderer (`chat/render.py`)

Layout on a `rows × cols` terminal: scroll region rows 1..rows-3,
separator on rows-2, status bar on rows-1, composer on rows. The scroll
region is set with DECSTBM as the frame does; SIGWINCH recomputes it and
repaints the bottom block.

Startup paints history from the active harness's transcript through
`adapter.parse_entry`, tagged by source (`[via codex]` provenance is
already in the translated text), limited to the last `history_turns`
turns. Live turns print from `LiveEvent`s:

- speaker labels: `you → codex` for the prompt, `codex` for the reply;
- text deltas append and wrap at `cols`;
- a tool row per call: `▸ Bash pytest -q` while running, then `· ok` or
  `· error` with the one-line summary; output prints as it streams, capped
  at `tool_output_lines`, errors uncapped;
- approvals: `▸ Allow: touch notes.md   [y]es [a]lways [n]o` and the
  composer switches to answer mode; questions list numbered options;
- `turn_finished` prints a dim usage line.

Colors are limited to bold labels and dim tool rows; everything degrades
to plain text under `NO_COLOR`.

Status bar reuses `frame.StatusBar` and the rate-limit poller: the default
harness is the marked slot, usage figures come from the meters the
dispatcher feeds after each drain, and the codex slot also accepts the
app-server's rate-limit notification.

## State and configuration

- `sessions.active` is the sticky default. The chat window sets it after
  every completed turn and on a bare prefix.
- New table `chat_pins(tandem_id, harness, model)`; added through the
  StateStore schema-version mechanism.
- `[chat]` in `~/.tandem/config.toml`:

```toml
[chat]
tool_output_lines = 8        # tail printed per tool call
history_turns = 50           # turns painted at startup
show_thinking = false
claude_setting_sources = ["user", "project", "local"]
# codex_approval_policy = "on-request"   # default: inherit ~/.codex/config.toml
# codex_sandbox = "workspace-write"
```

- `tandem chat [--on <harness>]` starts the window; `--on` sets the
  default before the first prompt. In an unpaired directory it pairs the
  way `tandem` does.

## Error handling

- Runtime process death or an unrecognized protocol shape: the turn ends
  as `failed` with the stderr tail printed; the target's transcript is
  drained anyway so a partial turn still syncs; the target stays the
  default.
- Codex `already has an active writer`: printed as "this codex thread is
  open in another process"; no retry.
- Claude resume rejections (a bad model tag, a malformed transcript) are
  caught by the pre-dispatch validation where possible, otherwise surface
  from stderr.
- Approvals wait indefinitely; Esc during an approval answers `deny` and
  interrupts the turn.
- Ctrl-C: first press interrupts a running turn; a second within 2 s exits.
  Exit terminates every process the window started through the kill
  ladder (`WarmChild.kill` shape: keep draining while the ladder runs).
- Opencode server death: restarted once on the next opencode turn, then
  reported.
- Sync problems are printed the way `_report_switch` prints them today,
  including quarantine notices.
- Trust: headless claude and codex app-server skip the folder-trust
  prompts their TUIs show (spike-observed). The docs say so; v1 adds no
  prompt of its own.

## Testing

- **Runtime clients** against scripted fake CLIs: a Python script stands
  in for `claude`, `codex app-server`, or the opencode server, emits
  canned stream-json / JSON-RPC / HTTP+SSE, and asserts the requests it
  receives. Cases per client: plain turn, approval allow / deny / always,
  question, interrupt, crash mid-turn, malformed line; codex adds the
  writer-lock error and `thread/start` id capture; opencode adds
  `permission.asked` and the server-restart path.
- **Grammar** table tests: the three routes with and without `:model`,
  bare routes, `/codex:default` clearing a pin, `/codex/README.md` and
  mid-prose `/codex` as non-routes, other `/word` prompts passing through
  unchanged, and `@path` mentions left verbatim.
- **Dispatcher** with the existing `conftest.Env`: drain / fast-forward /
  set_active order, queueing, echo suppression, the same-thread guard,
  failure-still-drains; modeled on the `run_oneoff` tests.
- **Renderer** byte-level tests at fixed sizes: region setup, wrapping,
  tool-row truncation, approval prompt, resize, `NO_COLOR`.
- **Live gate**: a tmux-driven script on a private socket (the previous
  gates' recipe) runs a three-harness relay with one approval per harness
  and checks each native transcript, recorded in `docs/formats.md`.
- **Compat**: `compat.py` ranges gain the features each client depends on
  (claude stream-json control channel and stdio permission tool, codex
  app-server v2 methods, opencode HTTP paths); `tandem doctor` reports
  them. Tested at spike time: claude 2.1.263, codex 0.153.4, opencode
  1.18.20.

## Follow-ups (not in this spec)

- Sync filter for claude's synthetic resume entries ("Continue from where
  you left off." / "No response requested."), which otherwise mirror into
  the other harnesses as noise.
- Rename foreign tool names during translation: a model reading synced
  `bash` / `exec` calls tried to call `bash` on claude (spike). Belongs to
  `toolmap.py`.
- The automatic router, behind the "no route" branch of the grammar.
- A native-TUI flip from inside the chat window.
- Collapsible tool output (requires a virtual scrollback).
