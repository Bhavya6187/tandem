# How tandem works

The mechanics behind the pairing: sync engine, PTY passthrough, the
flip, compatibility ranges, and where your data lives. (Back to the
[README](../README.md).)

- **One model per command — always.** Only the active harness's model is
  ever invoked (or, for `run --on`, the target's). Every shadow side is
  pure local file I/O: tandem tails the active transcript, translates
  each entry, and appends it to each shadow's own session store. A
  shadow's model is never called to "catch up".
- **Two, or three, participants.** A session's participants are the
  harnesses that were installed and usable when it was paired — any two
  of `claude`, `codex`, `opencode`, or all three (the order, and so the
  flip cycle, is configurable; see the `harnesses` key in
  [Configuration](configuration.md)). Not installed is a silent skip;
  installed but unusable — below the compatibility floor, or its runtime
  isn't ready — warns and drops out; fewer than two usable is an error
  that names what's missing and how to install it. On `tandem resume`, a
  member that has since become unusable is dropped from that session for
  good (the first survivor takes over if it was the active one); there is
  no dynamic rejoin. Sync fans out per direction: with three
  participants, each turn on the active side is translated twice, once
  into each shadow.
- **Exit means exit.** Leaving the harness — as opposed to flipping out
  of it — prints the resume hint and returns you to your OS shell; the
  paired session is saved and `tandem resume` re-enters it (`tandem
  sessions` lists recent ones across directories).
  `status` / `sync` / `doctor` / `run --on` / `sync-mcp` run one-shot
  from your shell, targeting the directory's most recently used session.
- **The frame: flip without leaving.** Ctrl-] (configurable, consumed at
  the PTY layer, ignored inside bracketed paste) flips the screen to the
  next harness in the cycle — and then to one more stop, the mixed tab,
  which shows the harness it is focused on and adds `@target` prompt
  routing (`[frame] mixed = false` drops it back to the harnesses alone;
  see the README). Pressed mid-turn it arms and fires at the turn boundary
  (press again to cancel — the bar shows the armed state),
  then tandem exits the fronted CLI gracefully (quit keystrokes, then
  SIGTERM, then a bounded SIGKILL), lets the incremental sync settle,
  rotates roles, and resumes the next side — with no stop in between. If
  the next harness refuses to come up, the ladder tries the one after it,
  and falls back to the one you left as a last resort.
  With claude fronted, the boundary comes from claude's own session
  registry (`~/.claude/sessions/<pid>.json`, the data `claude agents
  --json` prints): `busy` holds the flip, anything else fires it — so an
  idle prompt flips instantly even though claude keeps appending
  housekeeping to its transcript between turns.
  (The registry appeared in claude 2.1.226; on older claudes the probe
  finds nothing and every armed flip fires at once, mid-turn included —
  the deliberate single-tier trade: no silent valve, breakage is loud.)
  With opencode fronted, the boundary is read from its session database:
  busy while the last message is a user prompt or a still-streaming
  assistant, otherwise the flip fires. With codex fronted the
  transcript-marker rules stand: where no marker could be wired (codex
  with a `notify` handler of your own, which tandem won't clobber) ~2s of
  transcript quiescence stands in; where one was wired, a 120s valve
  covers a marker that never arrives — far above any plausible tool-call
  silence, because firing early kills a live turn while firing late only
  costs a wait (and Ctrl-] cancels).
- **Pipelined flips.** The moment a flip fires, the incoming harness
  starts booting *while* the outgoing one is torn down, so the flip lands
  at about the incoming harness's own start-up speed rather than that
  plus the shutdown. Nothing exists before you press the key and nothing
  survives the flip — between flips a tandem session is exactly one
  harness process. Flips *into* opencode run serially: its TUI caches the
  session on boot, so it must open only after the last turn has landed
  in its database. `[frame] warm = false` makes every flip serial.
- **The tab bar.** The bottom terminal row is tandem's one drawn pixel:
  the child is told the terminal is a row shorter, a scroll region keeps
  output above the bar, and a targeted watcher reasserts it after child
  screen resets. It names every participant (● active, ○ shadow), the
  flip key, and — on the active slot — live token accounting read from
  the transcript as it grows: the model's current context (`144k ctx`, or
  `43% ctx` for codex, which reports its window) and the session's
  input↑ / output↓ totals, where input is the whole prompt side as billed
  (fresh input plus cache reads and writes) and output includes
  reasoning. Every slot also carries its account's rate-limit windows
  (`5h 4% 7d 41%`, percent used), polled on a side thread from the same
  endpoints `claude`'s `/usage` and `codex`'s `/status` call, using the
  credentials those CLIs keep on disk — tandem's only network calls, off
  with `[frame] rate_limits = false`. Stats are cosmetic — any failure
  there blanks them and never touches sync — and they're elided in
  tiers when the row is too narrow: the ↑↓ totals first, then the rate
  limits, then the ctx figure. If a terminal can't sustain the bar it drops
  for the session (the flip keeps working) and `tandem doctor` says so
  until you delete the marker file it names; shrinking the window below
  the bar's row floor drops it just as permanently, but silently — that
  one isn't a conflict to fix.
- **PTY passthrough.** tandem launches the real CLI on a pty (raw mode,
  resize forwarding, signals through the line discipline) and never
  scrapes terminal output — the transcript files are the source of truth.
  Turn-complete hooks (`claude --settings` Stop hook, `codex -c
  notify=[…]`) are wired per-invocation as wake-up signals, with
  fs-watching as the data path and fallback (opencode has no per-invocation
  hook; tandem watches its database's WAL file). If your codex config
  already sets `notify`, tandem leaves it alone.
- **Append-only, crash-safe sync.** Each transcript entry is translated as
  it lands — no bulk re-export at switch time. JSONL appends are
  whole-line + fsync, opencode rows land in one transaction per synced
  unit with pre-minted ids, and a write-ahead intent in the sync cursor
  makes translation exactly-once across crashes; on restart, sync resumes
  from the last confirmed entry.
- **Tool calls translate natively.** The harnesses speak different tool
  vocabularies, so between claude and codex each completed call+result
  pair is re-expressed in the shadow's own terms — `Bash` ↔
  `exec_command`, `Edit`/`Write` ↔ `apply_patch`, `TodoWrite` ↔
  `update_plan` — and lands as a real tool-call record, so shadow history
  reads as the shadow's own work. Anything that wouldn't map truthfully
  passes through verbatim (name, arguments, and result intact), which is
  also how every call reaches opencode today; a call whose result never
  arrived is closed with a `(tool result not recorded)` placeholder at
  handoff, since the replay APIs reject dangling calls.
- **Attribution stays legible.** Every synced *text* message is tagged
  `[via claude-code]` / `[via codex]` / `[via opencode]` (tandem's own
  notes use `[tandem]`), so interleaved histories make sense to you and to
  the models. Tool activity is untagged — it's mirrored as native records,
  not prose. Rows tandem writes into opencode carry `providerID: "tandem"`,
  so opencode renders them as synced history rather than as its own model's
  output.
- **Errors are contained.** An entry that fails translation becomes a
  single per-turn placeholder in the shadow, with the raw entry
  quarantined under `~/.tandem/quarantine/…` — and sync continues. The
  shadow is never corrupted or truncated.
- **Memory files stay in step.** Fresh launches and every flip sync
  CLAUDE.md ↔ AGENTS.md (codex and opencode both read AGENTS.md): shared
  content lives in a `<!-- tandem:shared:begin/end -->` block (newer file
  wins), tool-specific text outside the block is preserved, and a file
  without markers is read from but never rewritten. Git state is never
  touched.

## Compatibility

Session formats are internal to the CLIs and drift between releases.
tandem pins what it was built against (observed formats documented in
[docs/formats.md](https://github.com/Bhavya6187/tandem/blob/main/docs/formats.md)):

| CLI | Tested | Accepted range |
| --- | --- | --- |
| Claude Code | 2.1.220 | ≥ 2.0, < 3 |
| Codex CLI | 0.145.0 | ≥ 0.140, < 0.150 |
| opencode | 1.18.15 | ≥ 1.18 (no ceiling) |

Above a range's ceiling, tandem warns and asks you to run `tandem doctor`
before trusting sync; below the floor the harness is excluded from the
session (the format genuinely predates what tandem was built on).
opencode has a floor only, because pre-1.18 opencode predates its SQLite
session storage. Format knowledge is isolated per tool in
`src/tandem/harness/claude_code.py`, `src/tandem/harness/codex.py`, and
`src/tandem/harness/opencode.py`.

## Where your data lives

- `~/.tandem/state.db` — SQLite: session pairing (participants, native
  session ids, active side) + per-direction sync cursors (override the
  directory with `TANDEM_HOME`)
- `~/.tandem/quarantine/<session>/` — raw entries that failed translation
- `~/.tandem/subagents/<session>/` — GPT subagent worker logs and any
  retained forks
- `~/.claude/projects/<munged-cwd>/<session-id>.jsonl` — claude transcript
  (`CLAUDE_CONFIG_DIR` honored)
- `~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<session-id>.jsonl` — codex
  rollout (`CODEX_HOME` honored)
- opencode's own SQLite database — the path `opencode db path` prints
  (`OPENCODE_DB` honored); tandem's session rows live alongside opencode's

Claude session ids are minted by tandem (`claude --session-id`); codex
mints its own on first run and tandem captures it from the new rollout
file; opencode sessions are born through `opencode import`, so they exist
before `opencode -s <id>` is ever asked to open them.
