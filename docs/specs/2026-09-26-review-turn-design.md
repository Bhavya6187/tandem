# Review mode: the review as a shared turn, then one follow-up — design

Status: design approved in brainstorm 2026-09-26 (approach A), awaiting
operator review of this written spec. Builds on the chat navigator
(`docs/specs/2026-09-25-chat-navigator-design.md`) and the `--review` flag
(PR #85). Touches nothing in the native frame.

## Goal

Under `tandem --review` the reviewer's verdict stops being a private aside
and becomes part of the conversation both models share. After each
substantive turn on the executing harness, the reviewing harness runs its
review as a real turn on its own shared session, so the review prompt, the
files it read and its verdict land in the reviewer's transcript and are
synced into the executor's transcript the way every other turn is. When the
reviewer flags something, the executor then takes one more turn, on its own
transcript that now contains the review, to act on it. No keystroke from
the user in between; the user sees the review and the follow-up paint in
the window like any two turns.

Operator rulings (2026-09-26):

- The follow-up runs only when the reviewer speaks. A clean review is still
  a turn in both transcripts (a short "no concerns" record), but the
  executor does not run again.
- One round only. The follow-up turn is not reviewed. Turn → review →
  follow-up → idle; the user types to go on.
- `--review` selects this behaviour. It is a third delivery mode,
  `navigator_deliver = "turn"`, next to `bar` and `prompt`; config-only
  navigator users keep what they have unless they set it.

## Non-goals

- Looping until clean, capped or not. One round; a later slice can add a
  cap if a week of logs argues for it.
- Reviewing the follow-up turn, or reviewing the reviewer.
- Typing while the review runs. In `turn` mode the round occupies the
  dispatcher; prompts typed meanwhile queue behind it, as they do behind
  any running turn. The async private-fork review stays exactly as it is
  for `bar` and `prompt`.
- Opencode as reviewer (unchanged from the navigator spec). Opencode turns
  are reviewed and can receive a follow-up.
- A per-turn dynamic reviewer. The reviewer is fixed at launch (PR #85).
- Any change to what gets reviewed: gate, headroom, interval, three-strike
  and dedupe rules are the navigator's, unchanged.

## Decisions and their reasons

- **The review is a dispatcher turn, not a navigator thread.** The
  dispatcher's worker is the only thing that runs turns on shared sessions,
  and `ops.sync_after_turn` is the only writer into the other transcripts.
  Running the review through `Dispatcher._run` gives the shared transcript
  for free and rules out the concurrent-writer case the sync model forbids
  (a review syncing codex's session while a user turn syncs claude's). The
  alternatives were rejected in the brainstorm: hand-appending synthetic
  review records to both native transcripts couples tandem to the formats
  where its worst bugs have lived (paginated ordinals, forward parents),
  and running a real-session review off-thread is the concurrent write.
- **No fork in `turn` mode.** The review resumes the reviewer's shared
  session directly: codex on its shadow rollout (or its own thread when
  codex created the session), claude with `-p --resume <shadow>` and no
  `--fork-session`. Nothing to delete afterwards.
- **The review turn is still read-only and never asks.** Same recipe the
  reviewers use today, moved into the runtimes: codex `approvalPolicy
  never` + `sandbox read-only` + the verdict `outputSchema` on the turn;
  claude `--permission-mode default --allowedTools Read Grep Glob
  --disallowedTools Edit Write MultiEdit NotebookEdit Agent Task
  --max-turns 4 --json-schema <SCHEMA>`. Approvals go to a deny-all
  answerer; the window is never asked anything during a review.
- **The verdict keeps the schema.** Structured output is what makes
  `parse_verdict` reliable, and the parser, log and painter already exist.
  The JSON reply is what lands in both transcripts; models read JSON fine.
  The window does not paint the raw JSON — it paints the verdict row it
  paints today.
- **One round falls out of an existing rule.** The follow-up prompt begins
  with `[tandem navigator]`, and the gate already skips any prompt starting
  with `[tandem` (`skip:tandem-prompt`). No round counter, no new state.
- **The review never becomes the default harness.** Every ordinary turn
  makes its harness the default afterwards. A review turn must not: the
  user is working in claude, codex's review is an aside. The follow-up
  runs on the executor and leaves the default where it was.

## Behaviour

### One round

1. The user's prompt runs on the executor (say claude). It syncs. The
   navigator gates it exactly as today. If the gate passes and the mode is
   `turn`, the navigator hands the dispatcher a **review** item, placed at
   the front of the queue before the worker declares itself idle, so a
   prompt typed during the executor's turn cannot jump it.
2. The dispatcher runs the review item as a turn on the reviewer (codex):
   builds the same prompt as today (`build_prompt` with the diff computed
   on the worker), resumes the shared session, read-only, with the schema,
   deny-all answers. Its tool rows paint live under a header that names it
   a review. The final JSON text is collected, not painted. The turn syncs
   into claude's transcript through `sync_after_turn`, close-note logic
   included if it did not complete.
3. The verdict is parsed and settled by the navigator (log line, failure
   count, dedupe, interval clock) and the existing `ReviewFinished` row is
   painted: the receipt when clean, the note and evidence when spoken.
4. If the verdict is `speak`, the dispatcher places a **follow-up** item at
   the front of the queue: harness = executor, model = the executor's pin,
   prompt = the note as text (below). It runs as an ordinary turn under
   the window's current mode with the usual approval prompts. Its header
   names it a follow-up. It syncs; it is not reviewed.
5. Idle. Prompts queued during the round run now, in the order typed.

The follow-up prompt:

```
[tandem navigator] codex reviewed your previous turn and flagged (block): <note>
<file>:<line> — <why>
…
Act on this in this turn: fix what you agree with, and say plainly what you disagree with and why.
```

The first line is the navigator trailer's wording today, so `/note good|bad`
and `tandem navigator log` describe the same thing. The last line is new:
a trailer rides the user's own request; a follow-up is the whole request.

### What ends a round early

| Event | Effect |
|---|---|
| Review turn fails (`status != completed`) | error verdict, counted toward the three strikes, round over, no follow-up |
| Review interrupted (Ctrl-C) | `error` verdict with `interrupted`, **not** counted as a strike, round over |
| Verdict unparsable | error verdict as today; strike; round over |
| Verdict clean / empty / dup | receipt row, round over |
| Follow-up fails or is interrupted | ordinary failed/interrupted turn; nothing more |
| Window closes mid-round | `Dispatcher.close()` clears the queue, so a queued review or follow-up never runs; a running one is interrupted like any turn |
| Third strike | navigator `off` as today; no further review items are queued |

Ctrl-C during the review interrupts the reviewer's runtime, as it does for
any running turn. The review's partial turn syncs with a close note like
any interrupted turn.

### Gate additions

Nothing new is gated. The follow-up prompt is skipped by the existing
`[tandem` rule, and the follow-up collects facts like any prompt turn so
the log records that skip. The review turn collects no facts and is never
offered to the gate. Command turns (`/compact`, `/model`) stay unreviewed.

### Window

- `/status` shows `navigator codex · turn`.
- Review turn header: `codex reviewing claude's turn` (bold), no prompt
  echoed; the tool rows follow as usual; then the verdict row.
- Follow-up header: `codex → claude` (bold) plus the dim
  `  + navigator note: <summary>` line carried notes show today.
- Activity line: the running turn's harness shows as working, as for any
  turn. The `reviewing` spinner and bar word stay for `bar`/`prompt` only.
- `/note` in `turn` mode: nothing is ever pending, so `/note` says `no
  pending note`; `/note good|bad` marks the last spoken review as today.
- The history painter (`paint_history`) renders the synced review and
  follow-up turns from the transcript when a session is resumed, as it
  renders any other turn. The review's JSON reply is shown as text there;
  acceptable for v1.

### Transcripts

Codex's rollout (or thread) gains, per round: the review prompt as a user
message, the review's tool calls, the JSON verdict as the assistant
message. Claude's transcript gains the same turn via the normal drain,
then the follow-up prompt and answer. Opencode as a participant receives
both by the same drain. No tandem code writes records by hand.

## Components

### Config and CLI

- `config._NAVIGATOR_DELIVERY = ("bar", "prompt", "turn")`;
  `navigator_deliver = "turn"` documented in `docs/configuration.md`
  beside `bar` and `prompt`, with the cost note (a review turn plus, when
  spoken, an executor turn, per reviewed turn).
- `cli._review_config`: `--review` sets `navigator=<reviewer>` and
  `navigator_deliver="turn"`; `--no-review` unchanged.

### Runtimes (`chat/runtime/claude.py`, `codex.py`)

`run_turn` gains `review: dict | None = None` — the verdict schema. When
set, the runtime runs the turn under its read-only recipe with that schema
and ignores the window's mode for this turn (claude: the review flags
appended to argv, `--permission-mode default`; codex: `approvalPolicy
never`, `sandbox read-only`, `outputSchema` on `turn/start`). The
constructor-level `output_schema` (codex) and `extra_args` (claude) stay
for the fork reviewers. `TurnOutcome.structured` carries claude's reply as
now; codex's JSON is the final text.

### Navigator (`chat/navigator.py`)

- `Navigator` takes an optional `dispatch: Callable[[TurnFacts], None]`.
  In `turn` mode `turn_ended` gates, then calls `dispatch(facts)` instead
  of `_start`; `_running`/`_pending`/`_thread` are unused in this mode.
- New `settle_round(facts, verdict) -> Note | None`: `_settle`, log the
  review line, post `ReviewFinished`, remember `_last_spoken_ref`, and
  return a `Note` when spoken (not stored in `_note`). The dispatcher calls
  it on its worker after the review turn synced.
- `Note.followup_prompt()`: the text above. `Note.trailer()` unchanged.
- `take()` returns `None` in `turn` mode; `mark()` returns `""` in `turn`
  mode.

### Dispatcher (`chat/dispatch.py`)

- `Pending` gains `kind: str = ""` (`""` | `"review"` | `"followup"`) and
  `facts: TurnFacts | None = None`. `submit` never sets `kind`.
- `start_round(facts)`: the navigator's `dispatch` hook. Builds the review
  item (reviewer harness, `navigator_model` pin, `kind="review"`, facts)
  and `appendleft`s it. Called from the worker's `finally` before
  `_running` clears, so no `_start` race with `submit`.
- `_run`, review item: prompt from `build_prompt(facts, diff)`;
  `run_turn(..., review=SCHEMA)` with `DenyAll` answers and an emit wrapper
  that forwards everything but `TextDelta`, which it collects; no
  `FactsCollector`; `_set_default` skipped; sync as normal; then
  `note = navigator.settle_round(facts, parse_verdict(...))`; if `note`,
  `appendleft` the follow-up item and log `ridden(ref, executor)`.
- `_run`, follow-up item: an ordinary turn (`FactsCollector` attached so
  the gate logs `skip:tandem-prompt`; `TurnStarted(kind="followup",
  carried=summary)`).
- `close()` unchanged: it already clears the queue.

### Events and renderer

- `TurnStarted` gains `kind: str = ""`. The renderer paints the three
  headers described under Window.
- `DenyAll` and `Collector` move from `chat/reviewers.py` to where both the
  reviewers and the dispatcher can import them (`chat/navigator.py`).

## Error handling

- A reviewer with no session id yet: the navigator is a participant, so
  `prepare_turn` seeds its shadow before the executor's turn. The one
  fileless case (a fresh pairing whose codex has never run) is handled the
  way any first codex turn is: the review turn mints the thread and
  `adopt_native_id` records it before the sync.
- `compute_diff` failures return `""` as today; the review still runs.
- Any exception in `settle_round` or in queueing the follow-up is swallowed
  on the worker with the same "the navigator must never take the window
  down" rule, and the round ends.
- `SyncSetupError` on the review turn's sync: `Failure` row, round over,
  logged as an error verdict.

## Testing

Unit (stub runtimes, no binaries; the existing `test_chat_dispatch.py` and
`test_chat_navigator.py` fixtures):

- Full round: executor turn → review item runs first even with a user
  prompt queued → spoken verdict → follow-up runs on the executor with the
  note prompt → queued user prompt runs last → default harness is still
  the executor throughout.
- Clean verdict: receipt row, no follow-up, queued prompt runs next.
- Review failed / interrupted / unparsable: no follow-up; strike counted
  only for failed and unparsable.
- Follow-up is not reviewed: the log shows `skip:tandem-prompt` for it.
- Review turn runs with `review=SCHEMA`, deny-all answers, and paints no
  `TextDelta`.
- Window close with a queued round drops it.
- `--review` yields `navigator_deliver == "turn"`; `[chat] navigator_deliver
  = "turn"` parses; `/status` wording.
- Renderer: the two new headers.
- Runtime argv/params: claude review argv has the read-only flags and
  `--json-schema` and no `--fork-session`; codex `turn/start` carries
  `outputSchema` and the read-only overrides.

Live gate (tmux, scratch repo, throwaway driver beside
`/tmp/review-gate/gate.py`), on claude 2.1.28x / codex 0.155.x:

- `tandem --review`: plant a bug the executor will write (`xs[len(xs)]`),
  confirm the window shows the review header, the verdict row, and the
  follow-up turn; both transcripts end with the three turns in order; the
  review's JSON is claude's synced assistant message; `tandem navigator
  log` shows `review` then `ridden → claude`.
- `tandem --on codex --review`: the mirror, claude reviewing on its shadow
  with no fork file left in `~/.claude/projects`.
- Ctrl-C during the review: no follow-up, window stays usable.
