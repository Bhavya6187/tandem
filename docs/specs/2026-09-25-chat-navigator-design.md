# Chat navigator — design

Status: design approved in brainstorm 2026-09-25, awaiting operator review of
this written spec. Builds on the unified chat window
(`docs/specs/2026-09-08-unified-chat-window-design.md`); touches nothing in
the native frame.

## Goal

While the user works in one harness inside `tandem chat`, a second model
family reads the same conversation as it grows and, after each substantive
turn, runs one short headless review on a private fork of its own shadow
transcript. The review stays silent unless it would block a PR over what it
saw. Every review ends with a visible row in the window: a one-line receipt
when clean, the note in full when not. The note can ride the user's next
prompt so the active model sees it without an extra call, and it always
rides a prompt routed to the navigator so the user can argue with the model
that raised it. The navigator never types, never edits, and never takes a
turn in the user's conversation.

The feature is **off by default** and stays off by default after tuning
(operator call 2026-09-25). It costs quota on the navigator's account and
sends every reviewed turn to a second vendor; both are documented next to
the key that enables it.

## Non-goals (v1)

- The native TUI. Trigger by Stop hook / notify / WAL watch, delivery by
  `UserPromptSubmit` hook or Claude Code's inbox socket. The chat window is
  the client and needs none of that; native is a later slice.
- Push delivery (tandem starting a turn of its own with the note). The
  most alive mode and the most expensive; `deliver = "bar" | "prompt"` only.
- Two-stage triage (cheap model first, strong model to confirm).
- Opencode as the navigator. Opencode turns are reviewed; opencode does
  not review. Its server has a fork route, but the runtime client would need
  a schema path it lacks today.
- "The other family always reviews" (symmetric navigator). One fixed
  navigator harness; its own turns are not reviewed.
- Automatic tuning of the gate or the prompt.

## Decisions and their reasons

- **Chat first, because tandem is the client there.** The dispatcher's
  worker runs each turn end to end and calls `ops.sync_after_turn` before it
  emits `Idle` (`chat/dispatch.py`, `Dispatcher._run`). That is the
  turn-end sentinel, and the shadow is already drained at that point: no
  tail thread, no hook, no "drain before fork" step. Delivery is prompt
  text the dispatcher composes and rows the renderer paints, so every
  delivery mode works on all three harnesses.
- **Fixed navigator harness, configured, off by default.** An empty
  `navigator` key means the worker is never constructed: no fork, no extra
  process, no network. A navigator that is not a participant of the
  session leaves the feature off for that window with one dim note.
- **Review on a private fork, never the shadow.** Codex: the `tandem sub`
  fork (`ops.fork_shadow`) — a copy of the shadow rollout under a fresh
  uuid7 with originator `tandem-sub`, never a sync source, behind the same
  discovery guard as subagent rollouts. Claude: the installed CLI's
  `--fork-session` on `-p --resume <shadow>` (confirmed on 2.1.282), which
  mints a new session id from the shadow without touching it. Both forks
  are deleted when the review ends.
- **Separate runtime instances.** `ClaudeRuntime` and `CodexRuntime` each
  hold one live process and one interrupt target; the review gets its own
  instances so it can never collide with the dispatcher's live turn. Codex
  forks have their own writer lock file, so a live codex turn and a codex
  review coexist.
- **A shadow lock between dispatcher and navigator.** The dispatcher drains
  into the navigator's shadow in `prepare_turn` and `sync_after_turn`; a
  claude fork reads that file at spawn. The navigator exposes one
  `threading.Lock`; the dispatcher holds it across those two calls, the
  reviewer holds it from fork creation until the fork is loaded (codex:
  the copy; claude: until `system/init` arrives). `ops._sub_lock` (a file
  lock across `tandem sub` processes) is still taken around the codex copy.
- **Schema-constrained verdicts.** `claude -p --json-schema` and codex
  `turn/start.outputSchema` (present in the generated protocol models) both
  exist. The verdict is parsed from the structured result, with the final
  text parsed as JSON as the fallback; anything unparsable is a failed
  review, logged and silent.
- **A clean review prints a receipt.** Operator call 2026-09-25: a review
  that ran and found nothing must look different from one that never ran.
  The model's output is still constrained to speak only when it would block
  a PR; the receipt is tandem's row, not the model's.
- **Ride is a trailer, not a header.** The user's text leads the prompt and
  the note follows it, so session titles (first human prompt) and the
  `[tandem]`-prefix skip rules are unaffected.
- **Gate from the live event stream, not the transcript.** The dispatcher
  already sees every `ToolStarted` / `ToolFinished` / `TextDelta` of the
  turn in one vocabulary; file-change tools get a `paths` field so "edited
  files" is uniform across harnesses.
- **Log everything, spoken or not.** Precision is measured on real
  sessions, so every gated turn writes one record, and the `/note good|bad`
  marks append feedback records to the same file.

## Architecture

```text
 composer ──▶ dispatcher ──▶ runtime client ──▶ live events ──▶ renderer
                 │  ▲                                             ▲
   after sync    │  │ take(harness): pending notes ride the prompt │
                 ▼  │                                             │
             navigator worker ─── gate ─── reviewer (fork+turn) ──┤ ReviewStarted
                 │                            │                   │ ReviewFinished
                 │                            ▼                   │
                 ▼                       fork deleted             │
             ~/.tandem/navigator/<tandem_id>.jsonl                 │
                                                                  ▼
                                   bar mark (reviewing / note) · activity row
```

Five units.

### 1. Turn facts (`chat/events.py`, `chat/dispatch.py`)

`ToolStarted` gains `paths: tuple[str, ...] = ()`. Each client fills it for
its file-change tools only: claude `Edit` / `Write` / `MultiEdit` /
`NotebookEdit` from `input.file_path` or `notebook_path`; codex
`fileChange` items from their change list; opencode `edit` / `write` from
`input.filePath`. Commands (`Bash`, `exec`, `bash`) leave it empty.

The dispatcher wraps `emit` for the duration of a turn and accumulates:

```python
@dataclass
class TurnFacts:
    harness: str
    prompt: str                    # the user's text, trailer excluded
    carried_note: bool             # a navigator note rode this prompt
    status: str                    # TurnOutcome.status
    paths: tuple[str, ...]         # union of ToolStarted.paths
    commands: int                  # ToolStarted with an empty paths field
    failed_tools: int              # ToolFinished(ok=False)
    final_text: str                # last 2000 chars of TextDelta in the turn
    started: float; ended: float
```

After `sync_after_turn` and the meter poll, and before `Idle`, the worker
calls `navigator.turn_ended(facts)`. That call returns at once.

### 2. Navigator worker and gate (`chat/navigator.py`)

```python
class Navigator:
    shadow_lock: threading.Lock
    def turn_ended(self, facts: TurnFacts) -> None       # gate, then queue
    def take(self, harness: str) -> list[Note]           # notes to ride this prompt
    def pending(self) -> Note | None                      # what the bar/`/note` show
    def dismiss(self, feedback: str | None) -> None       # "good" | "bad" | None
    def mark(self) -> str                                 # "" | "reviewing" | "note"
    def close(self) -> None                               # kill a running review
```

**Gate.** Review when the turn `completed` and at least one of: `paths` is
non-empty; `failed_tools > 0`; `final_text` matches the completion-claim
pattern (`\b(done|fixed|passing|passes|implemented|completed?|works now)\b`,
case-insensitive — a heuristic, stated as one; not applied when
`carried_note` is set). Skip, with the reason
logged, when: the prompt starts with `[tandem`; `facts.harness` is the
navigator; the navigator's five-hour window has less headroom than
`navigator_headroom`; a spoken note landed less than `navigator_interval`
seconds ago; the session's first turn is still pending; the reviewer is
disabled after three consecutive failures.

**Queue.** One review in flight and one pending slot. A newer gated turn
replaces the pending one; it never queues behind it. The worker thread runs
the review, emits `ReviewStarted(navigator)` first and
`ReviewFinished(navigator, verdict)` last, logs the record, and starts the
pending turn if any.

**Dedupe.** A spoken note whose evidence (file, line) set was already spoken
in this window is downgraded to clean and logged as `dup`.

**Headroom.** `RateLimitPoller` publishes `state["windows"][harness]` (the
parsed `Window` list) alongside the bar text, and `LimitsUpdate` carries
the same list for claude's in-turn `rate_limit_event`. The gate reads the
shortest window's `used_percent`; absent data (polling opted out) does not
enforce the floor.

### 3. Reviewers (`chat/reviewers.py`)

```python
class Reviewer(Protocol):
    harness: str
    def review(self, session, model: str, prompt: str, schema: dict,
               shadow_lock: threading.Lock) -> str    # raw verdict text / JSON
    def close(self) -> None
```

**Codex.** Under `shadow_lock` and `ops._sub_lock`, `ops.fork_shadow`
copies the shadow (its inner drain is a no-op after the dispatcher's, but
stays for the `tandem sub` case). A fresh `CodexRuntime` resumes the fork id
with overrides `approvalPolicy = "never"`, `sandbox = "read-only"`, and a new
constructor option `output_schema` that `run_turn` puts on `turn/start`.
Events go to a collecting sink (final agent text) and a deny-all `Answers`.
The fork rollout is unlinked in a `finally`.

**Claude.** A fresh `ClaudeRuntime` with a new constructor option
`review_args` appended to argv: `--fork-session`, `--json-schema <schema>`,
`--allowedTools Read Grep Glob "Bash(git diff *)" "Bash(git log *)"`, and no
bypass. The stdio prompt tool stays, answered by the deny-all `Answers`.
`handle_line` records `session_id` from `system/init`; the reviewer holds
`shadow_lock` until that line arrives, then releases. The verdict comes from
the result message's structured output when present, else from the final
text. The forked transcript at `claude_transcript_path(cwd, fork_id)` is
deleted in a `finally`. The live gate checks the shadow's size before and
after; a shadow that changed marks the claude reviewer degraded (logged,
disabled for the window), because `--fork-session` is documented but the
orphan scan on resume is not (spike 2026-09-06).

**The diff.** Computed by the navigator before the review, in the session
cwd: `git diff --no-color -- <paths>` plus the full contents of any touched
path that is untracked; when `paths` is empty and `commands > 0`, `git
status --porcelain` and `git diff --no-color` for the whole tree. Capped at
20 000 characters with a truncation marker. Bleed from earlier uncommitted
turns is accepted and named in the prompt. Not a git repo: no diff section.

### 4. The prompt and the schema

The fork already holds the whole conversation, so the prompt is short:

```text
[tandem navigator] You are reviewing the assistant turn immediately above
this message, which ran on {harness}. It touched: {paths or "no files"}.
Its diff (may include earlier uncommitted changes in this tree):
{diff}
Speak only if you would block a pull request over something in that turn:
a bug it introduced, a claim it made that its own output contradicts, a
failing command it ignored. Do not restate the turn. Do not raise style.
Reply in the required schema. If nothing rises to that bar, verdict is
"clean" and note is empty.
```

```json
{"type": "object", "additionalProperties": false,
 "required": ["verdict", "severity", "note", "evidence"],
 "properties": {
   "verdict":  {"type": "string", "enum": ["clean", "speak"]},
   "severity": {"type": "string", "enum": ["block", "warn", ""]},
   "note":     {"type": "string", "maxLength": 400},
   "evidence": {"type": "array", "items": {"type": "object",
                "additionalProperties": false,
                "required": ["file", "line", "why"],
                "properties": {"file": {"type": "string"},
                               "line": {"type": "integer"},
                               "why":  {"type": "string"}}}}}}
```

Codex's strict output mode requires every property present and no extras,
so a clean verdict carries `severity: ""` and an empty evidence list.

`Verdict` is the parsed form plus `elapsed`, `error`, and the reviewer's
harness and model. `note` longer than 400 characters is clipped by tandem,
not rejected.

### 5. Delivery (`chat/events.py`, `chat/activity.py`, `chat/window.py`, `chat/render.py`)

Two new live events, `ReviewStarted(harness)` and `ReviewFinished(harness,
verdict)`, posted through the same queue as every other event and handled on
the main thread.

**Status.** While a review runs and no turn is active, the separator row
above the bar carries it, in the turn spinner's own shape:

```text
── ⠹ codex reviewing · 12s ──────────────────────────
```

`Activity` gains a `reviewing` state fed by the two events; a `TurnStarted`
takes the row back for the turn. Whether or not a turn is running, the
navigator's bar slot carries a mark from `Navigator.mark()`: `reviewing`
while in flight, `note` while a spoken note is pending, nothing otherwise
(`Window.bar_line` merges it into `bar.marks` beside `skip-perms`).

**Rows.** Every finished review paints in the scroll region. If a turn is
streaming when the verdict lands, the rows wait in `Window._deferred` and
flush right after that turn's closing row, so a paragraph is never split.

```text
  codex reviewed · no concerns · 18s                     ← dim, clean

codex ⚑ block · 21s                                      ← bold speaker row
  The retry loop swallows ShadowBusy, so a busy shadow drops the lines it
  was about to fast-forward.
  sync.py:142 — except ShadowBusy: continue               ← dim evidence rows
```

A failed review paints nothing. The third consecutive failure paints one dim
row, `codex navigator off: <reason>`, and the worker stops gating until the
window is reopened.

**Ride.** In `Dispatcher._run`, before `run_turn`, `navigator.take(harness)`
returns the pending note when `deliver = "prompt"`, or when `deliver =
"bar"` and `harness` is the navigator. The note is appended to the prompt:

```text
{user's text}

[tandem navigator] {navigator} reviewed the previous {harness} turn and
flagged ({severity}): {note}
{file}:{line} — {why}
```

The turn row shows the user's text, followed by one dim `+ navigator note`
row. A ridden note is consumed and logged as `ridden` with the target
harness. Because the trailer is part of the user message, sync carries it
into every shadow, including the navigator's own canonical one — which is
what lets `/{navigator} why?` argue with the model that raised it.

**Window commands.** `/note` prints the pending note in full or `no pending
note`; `/note dismiss` drops it; `/note good` and `/note bad` append a
feedback record for the last spoken note and drop it. `/status` gains a
`navigator codex · bar` fragment when enabled. No new keys.

### 6. Log and CLI (`chat/navigator.py`, `cli.py`)

`~/.tandem/navigator/<tandem_id>.jsonl`, one line per gated turn:

```json
{"ts": "...", "kind": "review", "turn_harness": "claude", "prompt": "first 120 chars",
 "gate": "review" | "skip:<reason>", "verdict": "clean|speak|empty|error|dup",
 "severity": "", "note": "", "evidence": [], "elapsed": 18.2,
 "navigator": "codex", "model": "", "error": ""}
{"ts": "...", "kind": "ridden", "ref": "<review ts>", "to": "claude"}
{"ts": "...", "kind": "feedback", "ref": "<review ts>", "value": "good" | "bad"}
```

`tandem navigator log [-n N] [--all]` prints the current session's recent
records (or every session's with `--all`) and a footer: reviewed / spoken /
skipped counts and the helpful rate, `good / (good + bad)` over spoken
notes with feedback.

## State and configuration

New keys in the `[chat]` table, all forgiving like the rest of
`ChatConfig`:

```toml
[chat]
navigator = ""            # "claude" | "codex"; "" (default) = off
navigator_model = ""      # model pin for the review turn; "" = the harness default
navigator_deliver = "bar" # "bar": you see it, it rides only prompts routed to the navigator
                          # "prompt": it also rides your next prompt to any harness
navigator_headroom = 20   # skip reviews when the navigator's 5h window has under this % left
navigator_interval = 180  # seconds between spoken notes
```

An unknown `navigator` value or `opencode` is treated as `""` with a dim
note at window open. `docs/configuration.md` gets the privacy line next to
the key: enabling it sends every reviewed turn's conversation and diff to
the navigator's vendor after every turn, on that account's quota.

No new database state. The only persistent artefact is the log file.

## Error handling

- Any exception inside gate, fork, review, parse, or delete: logged with
  `verdict: "error"`, nothing painted, the fork deleted if it was created.
  Three in a row disable the worker for the window (one dim row).
- Fork creation failing under `SyncSetupError` (no shadow yet): a skip, not
  an error.
- A review still running at `/quit`: `Dispatcher.close` calls
  `navigator.close()`, which climbs the same kill ladder as the runtimes and
  deletes the fork.
- An unparsable verdict: error. A parsable one with `verdict: "speak"` and an
  empty note: treated as clean and logged `verdict: "empty"`.
- Rate-limit 429s on the review turn: the runtime reports failure as it
  does for a live turn; the gate's headroom floor is the prevention.

## Loop safety

- Only user-typed turns are reviewed: prompts beginning with `[tandem` are
  skipped, and the navigator's own turns are skipped.
- The active model's reply to a ridden note is reviewed only if it changed
  files or failed a command again; the completion-claim pattern is not
  applied to a turn whose prompt carried a note.
- One note per `navigator_interval`; evidence-set dedupe within the window.
- One review in flight, newest-wins pending slot; the review is never
  queued through the dispatcher.

## Testing

Same shape as the chat suite (`tests/test_chat_*.py`): fake runtimes,
golden fixtures, no live processes.

- `test_chat_navigator.py`: the gate table (each skip reason, each trigger);
  the pending slot (newer replaces, in-flight finishes first); dedupe;
  interval; three-strike disable; log records incl. feedback and ridden.
- `test_chat_reviewers.py`: codex fork created under both locks and deleted
  on success and on failure; claude argv carries the fork, schema and
  allowlist flags; forked transcript deleted; verdict parsed from structured
  output and from final text; deny-all answers.
- `test_chat_dispatch.py`: facts accumulated from the event stream; `take`
  called after sync and the trailer appended; `shadow_lock` held across
  `prepare_turn` and `sync_after_turn`; `close` reaches the navigator.
- `test_chat_activity.py` / `test_chat_window.py` / `test_chat_render.py`:
  reviewing row when idle, mark on the bar during a turn, deferred rows
  flushed after the closing row, receipt / note / evidence rows, `/note`
  commands.
- `test_config.py`: the five keys, defaults, rejection of `opencode` and
  unknown names.
- `test_cli.py`: `tandem navigator log` footer arithmetic.
- Golden fixtures: a claude `result` line with structured output; a codex
  final `agentMessage` carrying schema JSON.
- Live gate (tmux, not CI): codex navigator on a claude turn that edits a
  file → receipt row; a planted off-by-one → note row with evidence;
  `/codex why?` → the trailer is in codex's shadow and the reply refers to
  it; claude navigator on a codex turn → shadow size unchanged, forked
  transcript gone.

## Rollout

1. **Milestone 1, the spike as product code.** Facts, gate, worker, codex
   reviewer, status row, bar mark, receipt and note rows, `/note`, log with
   feedback. `deliver` fixed to `bar`. Measured for a week on the operator's
   real sessions.
2. **Milestone 2.** Claude reviewer, `deliver = "prompt"`, headroom floor,
   interval and dedupe, `tandem navigator log`.
3. **Default for `navigator_deliver`.** Stays `bar` until the log shows a
   helpful rate the operator accepts (proposed: 70 % over at least 20 spoken
   notes). `navigator` itself stays `""` regardless.

## Follow-ups (not in this spec)

- Push delivery: tandem submits a tagged turn with the note (queues behind
  typed prompts, spends the active account).
- Two-stage triage; opencode as navigator (needs a schema path in the
  client); the symmetric "other family reviews" mode.
- The native TUI: Stop-hook / notify trigger, `UserPromptSubmit` delivery
  in the plugin, Claude Code inbox-socket push.
- Cross-session dedupe of evidence; a per-repo gate allowlist.
