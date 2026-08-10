# How tandem works

The mechanics behind the pairing: sync engine, PTY passthrough,
compatibility ranges, and where your data lives. (Back to the
[README](../README.md).)

- **One model per command — always.** Only the active harness's model is
  ever invoked (or, for `run --on`, the target's). The shadow side is pure
  local file I/O: tandem tails the active transcript, translates each
  entry, and appends it to the shadow's session file. The shadow's model
  is never called to "catch up".
- **A persistent prompt, not the OS shell.** Exiting the harness — as
  opposed to flipping out of it — lands you at `tandem (claude)>`. There,
  `switch` flips roles and drops you straight into the other tool, Enter
  re-enters the current one, and
  `status` / `sync` / `doctor` / `run --on` / `sync-mcp` all run against
  this session. `exit` (or Ctrl-D) returns to your shell and prints the
  resume hint. Every command also works one-shot from your shell,
  targeting the directory's most recently used session.
- **The frame: flip without leaving.** Ctrl-] (configurable, consumed at
  the PTY layer, ignored inside bracketed paste) flips the screen to the
  other harness: pressed mid-turn it arms and fires at the turn boundary
  (press again to cancel — the bar shows the armed state), then
  tandem exits the fronted CLI gracefully (quit keystrokes, then SIGTERM,
  then a bounded SIGKILL), lets the incremental sync settle, flips roles,
  and resumes the other side — with no stop at the prompt in between.
  With claude fronted, the boundary comes from claude's own session
  registry (`~/.claude/sessions/<pid>.json`, the data `claude agents
  --json` prints): `busy` holds the flip, anything else fires it — so an
  idle prompt flips instantly even though claude keeps appending
  housekeeping to its transcript between turns.
  (The registry appeared in claude 2.1.226; on older claudes the probe
  finds nothing and every armed flip fires at once, mid-turn included —
  the deliberate single-tier trade: no silent valve, breakage is loud.)
  With codex fronted the
  transcript-marker rules stand: where no marker could be wired (codex
  with a `notify` handler of your own, which tandem won't clobber) ~2s of
  transcript quiescence stands in; where one was wired, a 120s valve
  covers a marker that never arrives — far above any plausible tool-call
  silence, because firing early kills a live turn while firing late only
  costs a wait (and Ctrl-] cancels). The
  bottom terminal row is tandem's one drawn pixel: the child is told the
  terminal is a row shorter, a scroll region keeps output above the bar,
  and a targeted watcher reasserts it after child screen resets. If a
  terminal can't sustain the bar it drops for the session (the flip keeps
  working) and `tandem doctor` says so until you delete the marker file it
  names; shrinking the window below the bar's row floor drops it just as
  permanently, but silently — that one isn't a conflict to fix.
- **PTY passthrough.** tandem launches the real CLI on a pty (raw mode,
  resize forwarding, signals through the line discipline) and never
  scrapes terminal output — the transcript files are the source of truth.
  Turn-complete hooks (`claude --settings` Stop hook, `codex -c
  notify=[…]`) are wired per-invocation as wake-up signals, with
  fs-watching as the data path and fallback. If your codex config already
  sets `notify`, tandem leaves it alone.
- **Append-only, crash-safe sync.** Each transcript entry is translated as
  it lands — no bulk re-export at switch time. Appends are whole-line +
  fsync, and a write-ahead intent in the sync cursor makes translation
  exactly-once across crashes; on restart, sync resumes from the last
  confirmed entry.
- **Tool calls translate natively.** The harnesses speak different tool
  vocabularies, so each completed call+result pair is re-expressed in the
  shadow's own terms — `Bash` ↔ `exec_command`, `Edit`/`Write` ↔
  `apply_patch`, `TodoWrite` ↔ `update_plan` — and lands as a real
  tool-call record, so shadow history reads as the shadow's own work.
  Anything that wouldn't map truthfully passes through verbatim; a call
  whose result never arrived is closed with a `(tool result not recorded)`
  placeholder at handoff, since both replay APIs reject dangling calls.
- **Attribution stays legible.** Every synced *text* message is tagged
  `[via claude-code]` / `[via codex]` (tandem's own notes use `[tandem]`),
  so interleaved histories make sense to you and to the models. Tool
  activity is untagged — it's mirrored as native records, not prose.
- **Errors are contained.** An entry that fails translation becomes a
  single per-turn placeholder in the shadow, with the raw entry
  quarantined under `~/.tandem/quarantine/…` — and sync continues. The
  shadow is never corrupted or truncated.
- **Memory files stay in step.** Fresh launches and every switch sync
  CLAUDE.md ↔ AGENTS.md: shared content lives in a
  `<!-- tandem:shared:begin/end -->` block (newer file wins),
  tool-specific text outside the block is preserved, and a file without
  markers is read from but never rewritten. Git state is never touched.

## Compatibility

Session formats are internal to the CLIs and drift between releases.
tandem pins what it was built against (observed formats documented in
[docs/formats.md](https://github.com/Bhavya6187/tandem/blob/main/docs/formats.md)):

| CLI | Tested | Accepted range |
| --- | --- | --- |
| Claude Code | 2.1.220 | ≥ 2.0, < 3 |
| Codex CLI | 0.145.0 | ≥ 0.140, < 0.150 |

Outside the range, tandem warns and asks you to run `tandem doctor`.
Format knowledge is isolated per tool in
`src/tandem/harness/claude_code.py` and `src/tandem/harness/codex.py`.

## Where your data lives

- `~/.tandem/state.db` — SQLite: session pairing + per-source sync cursors
  (override the directory with `TANDEM_HOME`)
- `~/.tandem/quarantine/<session>/` — raw entries that failed translation
- `~/.claude/projects/<munged-cwd>/<session-id>.jsonl` — claude transcript
- `~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<session-id>.jsonl` — codex
  rollout (`CLAUDE_CONFIG_DIR` / `CODEX_HOME` honored)

Claude session ids are minted by tandem (`claude --session-id`); codex
mints its own on first run and tandem captures it from the new rollout
file.
