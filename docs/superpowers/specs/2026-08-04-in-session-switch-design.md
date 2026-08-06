# In-session switch: drive tandem from inside the harness

Date: 2026-08-04 (revised 2026-08-05)
Status: approved (wrapper-internal trigger; codex plugin ships)

## Problem

Switching harnesses today means leaving the session: exit Claude Code (or
codex), land at the `tandem (active)>` prompt, type `switch`. The wrapper
is doing its job — it owns the terminal and runs live sync — but the user
should never have to interact with it. The goal: type `/tandem:switch`
*inside* the live session and have the other harness take over seconds
later, and reach the common read-only commands (`status`, `doctor`) from
inside as well. The wrapper stays as the invisible terminal owner; the
tandem prompt remains only as an escape hatch.

Decisions made during brainstorming (and revised after a sol/gpt
second-opinion review plus code verification):

- **Keep the wrapper, hide it** — not a wrapper-free plugin-only mode.
- **Both directions** — claude → codex and codex → claude.
- **`/tandem:switch` only** — no natural-language phrase triggering.
- **status + doctor** are the only other commands surfaced in-session.
- **Mechanism: wrapper-internal transcript trigger.** Two earlier
  candidates were superseded: a model-emitted sentinel line (probabilistic
  — trusts model compliance) and UserPromptSubmit/Stop plugin hooks
  (deterministic but a three-process protocol whose shared intent state
  needs session-matching, atomicity, and staleness defenses — the bulk of
  the second-opinion findings). The typed command already lands verbatim
  in the transcript the wrapper live-reads for sync, and turn boundaries
  are already signaled by the launch-injected turn marker
  (`hook_argv_extra`: claude `--settings` Stop hook, codex `-c notify`,
  both touching a marker file the tailer watches). Detecting the *user's*
  message keeps the determinism; keeping the flag in the wrapper's memory
  deletes the cross-process protocol entirely.
- **The plugin ships in both harnesses.** Claude Code rejects unknown
  slash commands locally (they never reach the transcript), so
  `/tandem:switch` must exist as a command — it joins tandem's existing
  claude plugin. Codex ≥0.145 loads claude-format plugins from a
  dual-manifest tree (vibeshub `plugins/cli` is the template), and the
  codex plugin ships too, so `/tandem:switch` is a first-class command
  there as well. The plugin carries **command definitions only** — no
  hooks entries beyond the existing PreToolUse reroute; nothing about the
  switch mechanism depends on plugin hooks.

## Behavior

### Switching from inside

In either harness, the user types `/tandem:switch`. The command file has
the model reply with a one-line confirmation ("Switching to codex — it
will pick up right here"); the reply is pure UX and cannot affect the
switch. The wrapper detects the typed command in the transcript, waits
for the turn to complete and flush, closes the harness, flips roles, and
resumes the other side — a couple of seconds end to end.

If the user keeps typing after `/tandem:switch`, the next non-switch
user message cancels the pending switch: it only fires when
`/tandem:switch` was the last thing the user submitted and its turn has
completed.

### Read-only commands from inside

`/tandem:status` and `/tandem:doctor` run the CLI against the session the
harness is actually running in — pinned via environment, not guessed
from cwd — and relay the output. Their command frontmatter allowlists
exactly `Bash(tandem status:*)` / `Bash(tandem doctor:*)`, so they
prompt for nothing on claude; on codex they are ordinary sandboxed
read-only commands (plan-time verification: whether reading the state
store under codex's sandbox needs lock-file writes and therefore a
one-time approval).

### The escape hatch stays

Typed `switch` at the tandem prompt, plain exits landing at the prompt,
and one-shot `tandem switch` from the OS shell all behave exactly as
today.

## Implementation

### Trigger: detect the typed command in the live transcript

The tail loop already parses every live transcript line into normalized
events. It additionally matches *user-message* events for the switch
command and sets an in-memory switch flag on the runner; a later
non-switch user-message event clears it. Detection applies only to the
active harness's live output — the only file tailed — so replayed
history on the shadow side cannot re-trigger it, and sessions not
running under the wrapper have no detector at all (the feature's exact
scope; no stale state can exist).

Plan-time verification: what each harness writes to its transcript for a
typed plugin slash command (raw text, expanded content, or command-name
metadata). Whatever the form, it is deterministic per harness version;
if it is the expanded content, the command file embeds a fixed trigger
token and the matcher targets that.

### Turn boundary: the existing turn marker

The wrapper already injects a turn-complete signal at launch
(`hook_argv_extra`): claude runs with a per-invocation `--settings` Stop
hook and codex with a `-c notify` handler, each touching a marker file
under `~/.tandem/tmp/` that the tailer already watches as its flush
signal. The switch reuses it: once the flag is armed, the next marker
touch (or, where the marker is unavailable — codex with a user-configured
notify handler tandem refuses to clobber — ~2 s of transcript
quiescence) means the turn is complete and durable.

### Termination and flip

On turn-complete with an armed flag, the runner waits for transcript
quiescence (stable size, complete final line), then terminates the
harness through the PTY layer: `run_in_pty` grows a small control handle;
SIGTERM to the child's process group (harness tool children die with
it), SIGKILL after a bounded timeout, and the handoff records whether it
was graceful. The runner's existing exit path then runs unchanged —
stop and join the tail thread, final locked drain (this ordering already
exists in `InteractiveRunner.run`) — and it surfaces "switch requested"
alongside the exit code. `run_shell` sees it and reuses the existing
`_switch` path verbatim: `ops.switch_session`, re-enter the newly
active harness. No new sync logic; the switch turn is ordinary attributed
content. The tailer's existing incomplete-line retention covers a final
partial line; the post-exit drain resolves it.

### Session pinning for in-session CLI calls

The wrapper exports `TANDEM_SESSION_ID=<tandem_id>` into the harness's
environment at launch (`run_in_pty` already accepts `env`); tool
subprocesses inherit it, and `_resolve_session` prefers it over the
cwd-MRU fallback. This closes the same-directory multi-session
ambiguity for every `tandem` command run from inside a harness —
`cli.py` already documents the hazard ("a second `tandem` in the same
directory becomes the cwd-MRU and silently steals `status`") and guards
prompt-dispatched commands with `_SESSION_ID`; this is the same guard
for the in-session path. The switch trigger needs no such pinning: each
wrapper tails only its own session's transcript, so concurrent sessions
in one directory cannot see or steal each other's switch.

### One plugin, both harnesses — commands only

The existing `plugin/` tree gains codex packaging mirroring vibeshub:
`.codex-plugin/plugin.json` beside the claude manifest and a repo-root
`.codex-plugin/marketplace.json`. Contents:

- `plugin/commands/switch.md` — model confirms the switch in one line;
  if the directory is not paired, say so; mention that if nothing
  happens within a few seconds the session is not running under `tandem`
  (launch with `tandem resume`). Carries the trigger token if the
  transcript probe requires one.
- `plugin/commands/status.md`, `plugin/commands/doctor.md` —
  frontmatter `allowed-tools` scoped to the one command each; run and
  relay.
- `plugin/hooks/hooks.json` — unchanged (existing PreToolUse reroute
  only).

`tandem plugin install` learns the codex steps (marketplace add + plugin
add against this repo). The interactive first-launch offer stays
claude-only for now; `tandem doctor` gains checks: plugin installed in
each harness, and a codex version floor for plugin-command support
(exact floor pinned during implementation; 0.145 verified locally).

## Error handling

- **No wrapper running** (plain `claude`/`codex` with the plugin): the
  command's reply tells the user nothing will happen and to launch with
  `tandem resume`; no detector exists, so no state anywhere.
- **Unpaired directory**: the command file has the model report the
  directory is not paired.
- **Codex without the plugin / codex too old**: `/tandem:switch` may be
  rejected as an unknown command and never reach the transcript;
  `tandem doctor` diagnoses (plugin missing, version floor). Claude-side
  switching is unaffected.
- **Turn marker unavailable or missed**: quiescence fallback promotes
  the armed flag; worst case the switch lands ~2 s later.
- **Child ignores SIGTERM**: process-group SIGKILL after a bounded
  timeout; the transcript is already durable and drained post-exit.
- **`ops.switch_session` raises**: fall back to the tandem prompt with
  the error shown — today's failure behavior, the session is never lost.

## Testing

- Detector: user-message event matching (typed form per the probe, or
  trigger token), arm/cancel semantics (later non-switch message clears),
  no firing on assistant messages or replayed content.
- Runner: armed flag + turn marker → termination via the control handle;
  quiescence fallback; graceful vs forced recorded; existing
  stop/join/final-drain ordering preserved (seams already exist).
- `run_shell` auto-switch path via the injected `run_harness` /
  `input_fn` test seams.
- Session pinning: `_resolve_session` prefers `TANDEM_SESSION_ID` over
  cwd-MRU; two sessions in one directory each resolve to themselves.
- Plugin lint in the existing `test_plugin.py` style: both manifests,
  command files and their `allowed-tools`, hooks.json unchanged.
- `doctor`: codex version floor and per-harness plugin-install checks.
- Live validation (manual, as in prior releases): claude → codex and
  codex → claude round-trips in a scratch repo, the cancel rule, and the
  no-wrapper and unpaired-dir edge cases.

## Out of scope

- Natural-language switch phrases — the slash command is the only
  trigger.
- Removing or changing the tandem prompt for plain exits.
- `sync`, `sync-mcp`, and `run --on` as in-session commands.
- Any change to sync/translation logic.
- The model-sentinel and plugin-hook (UserPromptSubmit/Stop intent
  store) mechanisms — superseded.
- Extending the interactive first-launch install offer to codex
  (`tandem plugin install` covers it; doctor diagnoses).
