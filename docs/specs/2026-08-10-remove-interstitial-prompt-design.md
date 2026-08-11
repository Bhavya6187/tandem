# Retire the interstitial prompt and `tandem switch`

**Date:** 2026-08-10
**Status:** Approved
**Supersedes:** the prompt loop introduced by
`docs/superpowers/specs/2026-07-29-tandem-shell-design.md`

## Motivation

The `tandem (claude)> ` prompt predates the tandem frame. Its headline
command was `switch`: exit the harness, land at the prompt, type
`switch`, re-enter the other harness. Since the frame shipped
(`b3b09f3`), **Ctrl-]** performs that whole cycle from inside the
harness without stopping anywhere. Every other prompt command
(`status`, `sync`, `doctor`, `sync-mcp`, `run`) is a verbatim
one-shot `tandem <cmd>` available from the OS shell.

The prompt is now a second place to be, with nothing only it can do.
Exiting the harness should mean exiting tandem.

## Scope

**Removed:**

- The interactive prompt loop in `src/tandem/shell.py` (`run_shell`'s
  `while True: input(...)` body, `_dispatch`, `_split_run_line`, `HELP`).
- The one-shot `tandem switch` subcommand in `src/tandem/cli.py`.
  Ctrl-] becomes the only way to flip.
- The `cli._SESSION_ID` thread-through. It existed solely so
  prompt-typed commands targeted the live session instead of the cwd's
  most-recently-used one. One-shot commands resolve the MRU session per
  directory, and the session loop's `finally` calls `touch_used`, so
  after exit the just-exited session *is* the MRU — no behavior loss.

**Kept (explicitly out of removal scope):**

- All other click subcommands: bare `tandem`, `resume`, `status`,
  `run --on`, `sub`, `hook-route`, `doctor`, `sync-mcp`, `sync`,
  `plugin install`. `sub` and `hook-route` are load-bearing for the
  plugin regardless of the frame.
- `ops.switch_session` — after this change its only caller is the flip
  path.
- The entire flip machinery: `_flip_loop`, `_switch` with its
  flip-back ladder, `_try_enter`/`_enter`, held-back report
  reprinting after the flip's screen clear.

## New behavior

Exiting the active harness with no flip pending prints any held-back
sync reports (the runner already prints these itself on a normal
exit), then the existing hint —
`to continue this session: tandem resume <id>` — and returns to the
**OS shell** with the harness's exit code. `tandem resume` re-enters,
exactly as today.

**Failure-ladder terminal state changes.** Today, when a flip lands on
a harness that won't start *and* flipping back also fails, the user is
stranded at the prompt with the session intact. New terminal state:
both errors printed, resume hint printed, OS shell. The session is
equally intact; the invariant that the resume hint prints from a
`finally` (no failure may cost the user their session id) is
preserved unchanged.

## Module changes

- `src/tandem/shell.py` → renamed **`src/tandem/flip.py`**; entry
  point `run_shell` → **`run_session`**. The name "shell" becomes a
  lie once the shell is gone; the module's real job — run the active
  harness, keep re-entering on Ctrl-] until a no-flip exit — keeps its
  file. `cli._enter_session` is the only caller and updates.
- `_report_switch` moves from `cli.py` into `flip.py`: its other
  consumer (`tandem switch`) is deleted, and moving it removes the
  `flip → cli` late import (`shell.py:209`) that existed to share it.
- `run_session` loses the `input_fn` test seam (no input to inject);
  `run_harness` injection stays.

## Docs

- **README**: the "Exit the harness the usual way and you land at
  tandem's prompt" block (lines ~52–66, including the
  `tandem (claude)> switch` example) becomes a short "exit prints the
  resume hint" note; the `switch` row leaves the commands table
  (line ~131).
- **docs/how-it-works.md**: the "persistent prompt, not the OS shell"
  pillar (lines ~12–26) is rewritten around exit-to-shell + Ctrl-].

## Tests

- `tests/test_shell.py` → `tests/test_flip.py`. Prompt-loop tests
  (~15: unknown input, unbalanced quotes, dispatch targeting,
  prompt-typed `run`/`switch`/`resume`, EOF/Ctrl-C handling) are
  deleted. Flip/ladder/report tests (~12) are kept and adapted to the
  new entry point. The resume-hint-on-exit and
  hint-prints-even-when-the-loop-raises tests are kept — they guard
  the surviving invariant.
- `tests/test_cli.py`: `test_one_shot_switch_hints_resume` deleted.

## Release

Breaking removal of a documented command (`tandem switch`) and a
documented behavior (the prompt): next release is **0.3.0**. The
release itself is out of scope for this change.
