# tandem

A meta-harness that runs **Claude Code** and **OpenAI Codex CLI** as one
paired session. Work in either agent and switch between them at any time
without losing context: while one harness is **active**, the other is a
**shadow** whose native session file is kept continuously up to date by
translating the active harness's transcript entries into the shadow's own
format. Switching is instant — the shadow was always resume-ready via that
tool's native `--resume`/`resume` mechanism.

```
$ tandem start          # pair a claude + codex session for this directory
$ tandem                # work in the active harness (untouched native UX)
$ tandem switch         # flip active/shadow, instantly
$ tandem                # continue the same conversation in the other tool
$ tandem run --on codex "second opinion: why is this test flaky?"
$ tandem status         # pairing, roles, sync position
$ tandem doctor [--live]# verify both sessions are resumable
$ tandem sync           # manual catch-up translation (local file I/O only)
$ tandem sync-mcp       # opt-in: copy MCP server configs between the tools
```

## How it works

- **Per-command single-model invariant.** Only one model is ever invoked per
  user command — the active harness's (or, for `run --on`, the target's).
  The shadow side is pure local file I/O: tandem tails the active
  transcript, translates each entry, and appends to the shadow's session
  file. The shadow's model is never called to "catch up".
- **PTY passthrough.** `tandem` launches the real CLI on a pty (raw mode,
  SIGWINCH resize forwarding, signals through the line discipline). Tandem
  never scrapes terminal output; the transcript files are the source of
  truth. Turn-complete hooks (`claude --settings` Stop hook, `codex -c
  notify=[...]`) are wired per-invocation as wake-up signals, with
  fs-watching (watchdog, polling fallback) as the data path and fallback.
  If the user's codex config already sets `notify`, tandem leaves it alone.
- **Append-only sync.** Each new transcript entry is translated as it lands
  (no bulk re-export at switch time). Appends are whole-line + fsync, and a
  write-ahead intent in the sync cursor makes translation exactly-once
  across crashes. On restart, sync resumes from the last confirmed entry.
- **Tool calls become action summaries.** The harnesses have different tool
  vocabularies (Read/Edit/Bash vs shell/apply_patch), so tool calls are
  never replayed. Each completed call is rendered as compact plain text the
  shadow model reads as context — `` ran `pytest -q` -> exit 1 `` with
  head/tail-sampled output, or `edited src/auth.py:` with the unified diff
  (kept in full under 80 lines, truncated beyond).
- **Attribution.** Every synced entry is tagged `[via claude-code]` /
  `[via codex]` (tandem's own notes use `[tandem]`), so interleaved
  histories stay legible to both the user and the models.
- **Error localization.** An entry that fails translation becomes a
  placeholder in the shadow —
  `[tandem: turn N could not be translated from <tool> — <reason>; raw
  entry quarantined at ~/.tandem/quarantine/...]` — at most one per turn,
  and sync continues. The shadow is never corrupted or truncated.
- **Memory files.** `tandem start` and every `tandem switch` sync
  CLAUDE.md ↔ AGENTS.md: shared content lives in a
  `<!-- tandem:shared:begin/end -->` block (newer file wins), tool-specific
  text outside the block is preserved, and a file without markers is read
  from but never rewritten. Git state is never touched.

## Compatibility

Session formats are internal to the CLIs and drift between releases. Tandem
pins what it was built against (observed formats documented in
[docs/formats.md](docs/formats.md)):

| CLI | tested | accepted range |
| --- | --- | --- |
| Claude Code | 2.1.220 | ≥ 2.0, < 3 |
| Codex CLI | 0.145.0 | ≥ 0.140, < 0.150 |

Outside the range tandem warns and asks you to run `tandem doctor`
(`--live` performs a real one-word resume per harness — two small model
calls). Format knowledge is isolated per tool in
`src/tandem/harness/claude_code.py` and `src/tandem/harness/codex.py`.

## Where things live

- `~/.tandem/state.db` — SQLite: session pairing + per-source sync cursors
  (override the directory with `TANDEM_HOME`)
- `~/.tandem/quarantine/<session>/` — raw entries that failed translation
- `~/.claude/projects/<munged-cwd>/<session-id>.jsonl` — claude transcript
- `~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<session-id>.jsonl` — codex
  rollout (`CLAUDE_CONFIG_DIR` / `CODEX_HOME` honored)

Claude session ids are minted by tandem (`claude --session-id`); codex mints
its own on first run and tandem captures it from the new rollout file.

## Swapping in a different converter

The sync engine talks to a small adapter interface
(`tandem.converter.TraceConverter`):

```python
class TraceConverter(Protocol):
    def translate_entry(entry, direction, ctx) -> list[TargetEntry] | TranslationError
```

`ReferenceConverter` implements it via a normalized event model
(`tandem/events.py`) derived from the observed formats. Pass your own
converter to `SyncEngine(store, session, source, converter=...)`.

## Install & development

Requires Python 3.11+ and both CLIs on PATH. No network calls of tandem's
own; all model calls happen inside the wrapped CLIs under your existing
auth.

```bash
pipx install .        # or: uv tool install .
uv sync && uv run pytest   # development
```

Dependencies (deliberately small): `click` (CLI), `pydantic` v2 (event
schema), `watchdog` (transcript tailing), `pexpect`/ptyprocess (PTY
passthrough); state is stdlib `sqlite3`.

## v1 non-goals

One active harness at a time (no concurrent dual-active). No other agents
yet (the adapter interface is generic). No cloud sync or telemetry.
Compaction state is not translated — on switch, the newly active tool's own
compaction handles an over-long context.
