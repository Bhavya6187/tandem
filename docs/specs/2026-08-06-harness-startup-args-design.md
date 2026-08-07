# Per-harness startup args in config.toml

**Date:** 2026-08-06
**Status:** Approved

## Problem

Tandem builds the interactive launch command itself (`runner.py` via each
adapter's `interactive_argv()`), so there is no way to pass a flag the user
wants on every session — the motivating case being
`claude --dangerously-skip-permissions`. Users who always run claude with a
given flag lose it the moment tandem is the thing launching claude.

## Behavior

`~/.tandem/config.toml` gains optional per-harness tables whose `args`
list is appended to every **interactive** launch of that harness:

```toml
[claude]
args = ["--dangerously-skip-permissions"]

[codex]
args = ["--dangerously-bypass-approvals-and-sandbox"]
```

- Applies to every path that opens an interactive session — bare `tandem`
  and `tandem resume`, on both fresh starts and resumes. (`tandem switch`
  itself opens nothing; the follow-up `tandem resume` picks the args up.)
- Does **not** apply to one-off relay invocations (`tandem run`),
  subagent dispatch (`tandem sub`), or doctor's live probes.
- Argv order: adapter's own launch args, then user args, then tandem's
  hook extras — e.g.
  `claude --session-id <id> --dangerously-skip-permissions --settings {...}`.

## Config parsing

`config.py` gains `load_harness_args(harness: str) -> list[str]` reading
`data[harness]["args"]`. Same lenient philosophy as `[subagents]`:
missing file, unreadable TOML, absent table, non-list `args`, or any
non-string element all yield `[]` — configuration must never be the
reason a launch breaks. Unknown keys in the new tables are ignored. The
module docstring (which currently claims "[subagents] table only") is
updated.

## Wiring

In `InteractiveRunner.run()` (`runner.py`), immediately after
`argv = adapter.interactive_argv(active_sid, fresh)`, append
`config.load_harness_args(active)` before `hook_argv_extra`. No adapter
signature changes; adapters stay config-unaware.

## Docs

README's config.toml section gains the `[claude]` / `[codex]` example with
a one-line warning that the flags shown disable the harnesses' own
permission prompts.

## Testing

- `tests/test_config.py`: valid list, non-list `args`, non-string element,
  absent table, absent file — the invalid cases all return `[]`.
- Runner-level test asserting the user args land in the spawned argv
  between the adapter's launch args and the hook extras, for both fresh
  and resume launches.

## Out of scope

CLI passthrough flags (`tandem --claude-args ...`), per-mode arg sets,
and any validation of which flags are safe to pass.
