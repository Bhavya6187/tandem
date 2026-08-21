# Developing tandem

Dev setup and the extension surface. (Back to the
[README](../README.md).)

## Development

```bash
uv sync && uv run pytest
uv tool install .     # or: pipx install .
```

Dependencies are deliberately small: `click` (CLI), `pydantic` v2 (event
schema), `watchdog` (transcript tailing), `pexpect`/ptyprocess (PTY
passthrough); state is stdlib `sqlite3`.

The suite is hermetic — no harness binaries needed — except
`tests/test_opencode_oracle.py`, which round-trips through a real
`opencode` binary against a throwaway database (`OPENCODE_DB`) and skips
itself when the binary is absent. Two guards worth knowing about:
`plugin/.claude-plugin/plugin.json`'s `version` must match
`pyproject.toml` — and `plugin/.codex-plugin/plugin.json` (the same tree's
codex manifest) must match both — so a release bumps all three (a drift
test fails otherwise — see the [plugin README](../plugin/README.md) for
why); and CI installs with `uv sync --locked`, so a version bump needs
`uv lock` in the same commit.

## Layout

- `src/tandem/cli.py` — every subcommand; `_resolve_participants` is the
  one place that decides which harnesses a session gets.
- `src/tandem/harness/` — one adapter module per CLI (`claude_code.py`,
  `codex.py`, `opencode.py`) behind `base.HarnessAdapter`; **all**
  session-format knowledge lives here, so a format change in one tool
  touches one file. `docs/formats.md` records what each adapter was
  built against.
- `src/tandem/sync.py`, `runner.py`, `tailer.py` — the sync engine: one
  tail loop per (source → target) direction, append-only with a
  write-ahead intent in the cursor.
- `src/tandem/events.py`, `converter.py`, `toolmap.py` — the normalized
  event model, the reference converter, and native tool-call mapping.
- `src/tandem/flip.py`, `frame.py`, `ptyrun.py`, `warm.py` — the frame:
  PTY passthrough, the flip key and ladder, the tab bar, pipelined boots.
- `src/tandem/hookroute.py`, `modelcat.py`, `pinstash.py`,
  `plugin_setup.py` — GPT subagents: the Claude Code hook, model-name
  resolution, and plugin install.
- `src/tandem/state.py`, `compat.py`, `config.py`, `doctor.py` — the
  SQLite state store, pinned version ranges, `config.toml`, health checks.

## Extending tandem

### Adding a harness

Every CLI tandem fronts is one `HarnessAdapter` subclass in
`src/tandem/harness/`, registered in `harness/__init__.py`'s `ADAPTERS`
and named in `config.SUPPORTED_HARNESSES` and `compat.COMPAT`. The base
class in `harness/base.py` is the contract; the pieces an adapter
provides:

- **Detection and readiness** — `detect_version`, `version_supported`
  (against its `compat.COMPAT` range), `runtime_ready` (fail closed:
  installed but unusable drops out of the session with a reason), and
  an `install_hint` for the "fewer than two harnesses" error.
- **Session identity and storage** — `mint_session_id`,
  `transcript_path`, `create_shadow_transcript` (how a resume-ready
  shadow is born), plus the storage-capability hooks that default to
  file-backed JSONL and that opencode overrides for SQLite:
  `make_source_reader`, `watch_paths`, `shadow_append` /
  `shadow_intent` / `intent_landed`, `fast_forward_cursor`,
  `pending_units`, `prepare_shadow`.
- **Launch** — `interactive_argv`, `oneoff_argv`, `hook_argv_extra`
  (the per-invocation turn-complete signal, if the CLI has one),
  `quit_keystrokes` (the graceful half of the exit ladder), and
  `session_status` when the CLI can answer "is a turn running?" — that
  is what lets a mid-turn flip fire at the boundary.
- **Translation** — `parse_entry` (native record → normalized events)
  and `render_events` / `render_placeholder` (normalized events → native
  records), with `toolmap` optionally given a Tier-1 table so common tool
  calls read as the harness's own; without one, calls pass through
  verbatim.
- **Cosmetics** — `make_usage_meter` for the tab bar's token stats.

`tests/` has per-adapter fixtures; an oracle test against the real binary
(like the opencode one) is the strongest check that shadow sessions
actually resume.

### Swapping the converter

The sync engine talks to a small adapter interface
(`tandem.converter.TraceConverter`):

```python
class TraceConverter(Protocol):
    def translate_entry(entry, direction, ctx) -> list[TargetEntry] | TranslationError
```

`ReferenceConverter` implements it via a normalized event model
(`tandem/events.py`) derived from the observed formats. Pass your own
converter to `SyncEngine(store, session, source, target, converter=...)`.
