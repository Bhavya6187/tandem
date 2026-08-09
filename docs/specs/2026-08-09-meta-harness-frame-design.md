# The tandem frame: Ctrl-] flips between native harnesses

Date: 2026-08-09
Status: approved (brainstorm)

## Problem

tandem's product story inverts: instead of an accessory you occasionally
visit between sessions, tandem becomes the CLI you launch — a meta
harness. Claude Code and Codex are faces inside it, and moving between
them should feel like changing tabs, not like leaving one program to
start another. Today that move is a ceremony: exit the harness, land at
the `tandem (active)>` prompt, type `switch`, re-enter.

The goal: launch `tandem` once and never leave it. One keybind flips the
screen to the other native TUI a couple of seconds later, resumed with
the same history (the sync engine has already kept the shadow transcript
current). The native TUIs do all rendering — tandem builds no chat UI
and scrapes no output; it stays the invisible terminal owner it already
is.

Decisions made during brainstorming:

- **Native UIs only.** tandem renders no conversation UI in any mode.
  PTY passthrough remains the only display path; transcripts remain the
  only data path.
- **Keybind only.** Ctrl-] (configurable) is the sole flip trigger. No
  `/tandem:switch` slash command, no transcript command detection, no
  plugin changes, no in-session tandem commands. (A separately parked
  branch, `in-session-switch`, explored the slash-command route; it
  stays parked and this design does not depend on it.)
- **Subagents unchanged.** Quick cross-model consults remain the 0.2
  delegation path ("ask gpt to review this"); this feature only changes
  how you move your whole seat between harnesses.
- **One line of chrome: the tab bar.** tandem reserves the terminal's
  bottom row for a status bar (`claude ● │ codex ○  ⌃] flips`) and
  renders no other pixel — conversation UI stays entirely native. The
  bar also surfaces armed-flip state (`⏳ flipping at turn end…`), which
  is otherwise invisible. A `[frame] bar = false` kill-switch and
  graceful auto-drop cap the risk.
- **The escape hatch stays.** A plain exit (Ctrl-D) still lands at the
  tandem prompt; `exit` there returns to the OS shell with the resume
  hint. One-shot commands from the OS shell are untouched.

## Behavior

- `tandem` / `tandem resume` enters the frame: the active harness runs
  full-screen on its PTY, exactly as today.
- **Ctrl-] while the harness is idle:** flip now. The fronted CLI is
  exited gracefully, roles flip, the screen clears, and the other
  harness resumes with the same history. End to end ~1–3 s.
- **Ctrl-] mid-turn:** the flip arms and fires when the turn completes
  (tandem's existing turn-complete marker; quiescence fallback). A
  second Ctrl-] while armed cancels — the key is a toggle.
- **Unpaired or plain sessions:** the keybind is a no-op. Sessions not
  running under tandem are unaffected (there is no detector outside the
  wrapper).
- The flip key is configurable (`[frame] flip_key` in config.toml) for
  anyone who needs the raw byte in their TUI.
- **The tab bar** sits on the bottom terminal row for the whole session:
  active harness highlighted, the other dimmed, the flip hint, and —
  while a mid-turn flip is armed — `⏳ flipping at turn end…`. It
  repaints on resize and after child screen resets, disables via
  `[frame] bar = false`, and auto-drops for the session (flip still
  works) if the terminal can't sustain it.

## Implementation

### Detector: one byte in the PTY input relay

`ptyrun`'s input relay watches the keyboard stream for the flip byte
(0x1D default) and consumes it — it is never forwarded to the child.
Bracketed-paste state is tracked (ESC `[200~` … ESC `[201~`) so a paste
containing the byte cannot trigger a flip. Everything else passes
through untouched, as today.

### Arm, turn boundary, cancel

The detector sets an in-memory armed flag on the runner — no shared
state, no cross-process protocol. If the harness is idle the flip fires
immediately (the tailer already distinguishes in-flight turns: a
user-message event opens one, the marker touch closes it). Otherwise it fires on the next turn-complete signal: the
marker file tandem already injects per-invocation (claude `--settings`
Stop hook, codex `-c notify`) and the tailer already watches, with ~2 s
of transcript quiescence as the fallback where the marker is
unavailable. A second Ctrl-] while armed clears the flag.

### Termination ladder

`run_in_pty` grows a small control handle for cross-thread termination.
Escalation: first a soft app-level exit — clear the composer, then the
harness's own quit keystroke (adapter-owned recipes in
`harness/claude_code.py` / `harness/codex.py`; exact keystrokes pinned
at implementation per harness version), letting the CLI finalize
its own transcript — then SIGTERM to the child's process group, then
SIGKILL after a bounded timeout. Whether the exit was graceful is
recorded. Transcript appends are whole-line and durable, so even the
forced path cannot corrupt history.

### Status bar: a reserved bottom row

The child is told the terminal is one row shorter (`rows-1` in the PTY
winsize, on launch and on every resize), and tandem paints the bar on
the real bottom row. Bottom — not top — is load-bearing: the child's
rows 1..rows-1 map to real rows identically, so cursor-position
reports, mouse coordinates, and absolute addressing all stay truthful
with zero translation. A scroll region (DECSTBM rows 1..rows-1) keeps
normal-buffer scrolling (Claude Code scrolls the real buffer) above the
bar.

The one new touch on the output path: a small state machine scans the
child's output for the few sequences that would clobber the setup —
RIS, `CSI r`, alt-screen enter/leave (`CSI ?1049h/l`), full-screen ED —
and reasserts the scroll region and repaints the bar after each
(handling sequences split across read chunks). It is a targeted filter,
not a terminal emulator; all other bytes relay verbatim as today. Keys
landing on the bar row (mouse reports with the bottom row's coordinate)
are swallowed. If the filter encounters output it cannot reconcile, the
bar drops for the session and the frame continues bar-less.

### Flip and re-enter

The runner's existing exit path runs unchanged (stop and join the tail
thread, final locked drain), surfacing "flip requested" alongside the
exit code. `run_shell` sees it and reuses the existing `_switch` path
verbatim — `ops.switch_session` (role flip in the state store, memory
sync CLAUDE.md ⟷ AGENTS.md) — then clears the screen and immediately
re-enters the newly active harness instead of stopping at the prompt.
No new sync logic anywhere.

## Error handling

- **Resume failure** (version drift, missing binary, unhealthy rollout):
  relaunch the harness the user just left — never strand them. If that
  also fails, fall to the tandem prompt with the error shown; the resume
  hint still prints from the existing `finally` (the session is never
  lost). Post-flip rollout problems surface through the existing
  "may not resume cleanly; run `tandem doctor`" advisory.
- **Translation failure during the final drain:** the existing
  quarantine path applies; the flip proceeds.
- **Child ignores the soft quit:** SIGTERM, then bounded SIGKILL, as
  above.
- **Marker unavailable or missed:** the quiescence fallback promotes the
  armed flag; worst case the flip lands ~2 s late.
- **Bar can't be sustained** (irreconcilable child output, exotic
  terminal): auto-drop the bar for the session, keep flipping; note it
  in `tandem doctor`. `[frame] bar = false` disables it outright.

## Testing

- **Detector** (extends `test_ptyrun.py`): flip-byte match,
  bracketed-paste suppression, consume-not-forward, config override.
- **Runner:** armed flag → marker → termination ordering; idle
  fires immediately; cancel toggle; quiescence fallback; graceful vs
  forced recorded; existing stop/join/final-drain ordering preserved
  (seams already exist).
- **Shell:** the auto-flip loop via the existing injected
  `run_harness` / `input_fn` seams — flip lands back in the loop without
  a prompt stop; plain exit still reaches the prompt.
- `ops.switch_session` is unchanged and already covered.
- **Bar:** winsize lie on launch/resize; scroll-region reassert after
  each watched sequence (including sequences split across chunks); armed
  state rendering; auto-drop path; kill-switch.
- **Live validation** (manual, operator-run, as in prior releases):
  flips both directions in a scratch repo; mid-turn arm and cancel; a
  paste containing 0x1D; resume-failure fallback; unpaired no-op; bar
  behavior across Terminal.app, iTerm2, and the VS Code terminal
  (scrollback, resize, alt-screen switches).

## Docs

README reframe to match the inverted story: launch `tandem`, work in
whichever native TUI is fronted, `Ctrl-]` to flip; the pairing engine
becomes the implementation detail rather than the headline.

## Out of scope

- Peer-turn directive routing (`@gpt …` prefixes running whole turns on
  the other model in-place) — discussed during brainstorming and
  deliberately dropped; no design is retained.
- `/tandem:switch` or any in-session tandem commands (parked branch,
  untouched).
- Richer chrome than the one-row bar (menus, mouse interaction, per-turn
  stats).
- Any change to sync/translation, subagents, pairing, or the tandem
  prompt's plain-exit behavior.
