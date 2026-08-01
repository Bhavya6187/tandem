# Headless tandem pairing (bench arm A)

Arm A of the A/B bench runs claude with the tandem plugin installed, so every
`Task` dispatch hits `tandem hook-route` and must come back **rerouted to
codex**. This document is the exact recipe for producing that state for a task
working directory, non-interactively, under a bench-owned `TANDEM_HOME`.

Verified live on 2026-08-01 with claude 2.1.220, codex-cli 0.145.0,
tandem 0.1.5 (worktree `.venv`).

---

## 1. What the hook actually requires

`tandem hook-route` (`src/tandem/cli.py::hook_route_cmd`) rewrites a dispatch
only when **both** hold:

```python
with StateStore() as store:                       # TANDEM_HOME/state.db
    session = store.latest_session_for_cwd(cwd)   # (1) a row for this cwd
...
v = get_adapter("codex").detect_version()
codex_ok = v is not None and adapter.version_supported(v)   # (2) codex usable
```

`cwd` comes from the hook payload (`payload["cwd"]`), falling back to the
process cwd. `route()` (`src/tandem/hookroute.py`) then needs
`cfg.route == "all"` (the default), a `tool_name` of `Agent`/`Task`, a
non-empty `tool_input.prompt`, and a `subagent_type` that is not `fork` and
not the bridge itself. Nothing else — no transcript, no live claude session.

`latest_session_for_cwd` is a plain `WHERE cwd = ?` string match ordered by
`COALESCE(last_used_at, created_at) DESC` (`src/tandem/state.py`). So the row
just has to exist with a byte-identical cwd string.

`tandem sub` needs slightly more, and that difference decides the recipe:

| `tandem sub` path | needs | source |
| --- | --- | --- |
| `--context task` (**the default**: config `context = "match"` → `"task"`) | only `session.cwd`; it seeds a brand-new `tandem-sub` rollout and resumes that | `ops.seed_sub_rollout` |
| `--context full` | `session.codex_session_id` **and** its shadow rollout file on disk | `ops.fork_shadow` |

A recipe that only wrote a state row would satisfy the hook and the default
`tandem sub`, but would break `--context full`. The recipe below produces the
full pairing, so both work.

## 2. The recipe

There is **no** non-interactive tandem subcommand that creates a pairing (see
§5). What exists is tandem's own pairing routine, `tandem.cli._pair_session`,
which is pure state creation — the TUI only starts *after* it returns. The
recipe is to call that function in tandem's own interpreter:

```bash
TANDEM_HOME=<bench-home> <venv>/bin/python - <<'PY'
import os
from tandem.cli import _pair_session
from tandem.state import StateStore
cwd = os.path.realpath("<task-dir>")
with StateStore() as store:
    _pair_session(store, cwd, "claude")   # "claude" = initially active harness
PY
```

`bench/pair.py` is that, wrapped with argument handling, idempotency,
verification and teardown:

```bash
python bench/pair.py --tandem-home <bench-home> --cwd <task-dir>
python bench/pair.py --tandem-home <bench-home> --cwd <task-dir> --verify-only
python bench/pair.py --tandem-home <bench-home> --cwd <task-dir> --clean
```

It exits 0 only when the pairing exists **and** a probe `tandem hook-route`
call actually returns the rewrite decision for that cwd; any other outcome
exits 1 with the reason. Re-running it verifies instead of creating a second
pairing. `--probe-sub` additionally runs a real `tandem sub` (one codex call).

Arm A then runs claude with `TANDEM_HOME=<bench-home>` in its environment so
the hook reads the same state.db.

## 3. What lands where

For `--tandem-home H`, `--cwd C`, `--active claude`:

| Location | Content |
| --- | --- |
| `H/state.db` → `sessions` | one row: `tandem_id`, `cwd = C` (realpath), `active = 'claude'`, `claude_session_id` (uuid4), `codex_session_id` (uuid7), `created_at`, `last_used_at` |
| `H/state.db` → `sync_cursors` | one row for source `claude` (the write-ahead cursor) |
| `$CODEX_HOME/sessions/YYYY/MM/DD/rollout-<ts>-<codex_session_id>.jsonl` | the seeded **codex shadow**: a `session_meta` line with `originator: "tandem"`, `model_provider: "openai"`, `cwd = C`, plus the tandem seed note as a `response_item` + `event_msg` pair |
| `H/subagents/<tandem_id>/…` | created later, by `tandem sub` (running markers, `-q` logs, retained forks) |
| `H/warned/<claude-session-id>` | created later, by hook-route's once-per-session "nothing was rerouted" notice |

Nothing is written to `~/.tandem`. Nothing is written to `~/.claude` (with
`--active claude`, claude's own transcript is created by claude itself when
it runs; tandem only mints the id). The **codex rollout is the one artifact
outside the bench home** — see §4.

Observed shadow header:

```json
{"timestamp":"2026-08-01T18:51:35.486Z","type":"session_meta","payload":{"session_id":"019fbeaa-d893-72b8-953b-70e4c75abcd7","id":"019fbeaa-…","cwd":"…/pairing-spike/proj","originator":"tandem","cli_version":"0.145.0","source":"exec","thread_source":"user","model_provider":"openai","history_mode":"legacy"}}
```

## 4. Caveats

1. **cwd must be the realpath, byte-identical.** `latest_session_for_cwd`
   does an exact string match and tandem stores `str(Path.cwd())`, which is
   always symlink-resolved. On macOS `/tmp/x` must be stored as
   `/private/tmp/x`. `pair.py` calls `os.path.realpath` on `--cwd`; the bench
   must hand claude the same resolved path, or the hook silently declines.
   Subdirectories do **not** inherit the pairing: the match is on the exact
   directory, and claude's hook payload carries the session's cwd.

2. **Codex rollouts live in `CODEX_HOME`, not in the bench home.** That is
   tandem's own behavior (`paths.codex_sessions_dir()`), and `CODEX_HOME` is
   left at `~/.codex` on purpose so codex finds the user's existing auth. Each
   pairing therefore leaves one `originator: "tandem"` rollout in
   `~/.codex/sessions/YYYY/MM/DD/`. `pair.py --clean` deletes exactly those
   (it re-reads each file's `session_meta` and refuses to delete anything that
   is not tandem-authored for that cwd). `tandem sub` rollouts clean
   themselves up unless `[subagents] keep_forks = true`. Pointing
   `--codex-home` at a bench directory would isolate this, but codex would
   then find no `auth.json` and every worker call would fail.

3. **Codex version dependence.** `version_supported` pins
   `0.140 <= codex < 0.150` (`src/tandem/compat.py`); outside that,
   `codex_ok` is false and hook-route emits the notice instead of a rewrite —
   the pairing is fine, the routing is not. The `session_meta` fields the
   shadow is written with (`model_provider`, `history_mode`, `thread_source`)
   were derived from codex 0.145.0; a codex upgrade can invalidate them. The
   cheap check after any upgrade is `tandem doctor` plus
   `pair.py --verify-only --probe-sub`.

4. **`_pair_session` is a private function.** The bench is pinned to a tandem
   internal. If it is renamed, `pair.py` fails loudly with the interpreter's
   traceback rather than silently producing partial state — which is the
   behavior we want, but it is a maintenance dependency. Everything else
   (`StateStore`, adapters, `paths`) is stable module-level API.

5. **Memory sync is off by default.** Real `tandem` pairing also runs
   `sync_memory_files(cwd)`, which can create `AGENTS.md` in the task
   directory from `CLAUDE.md` (or vice versa). That mutates the working tree
   under test and would make arm A's tree differ from arm B's, so `pair.py`
   suppresses it unless `--memory-sync` is passed. It is a no-op anyway in a
   directory that has neither file.

6. **The paired `claude_session_id` is a fiction until claude runs under
   tandem.** Arm A's claude process is launched by the bench, not by
   `tandem`, so it gets its own session id and its own transcript. That does
   not affect hook-route or `tandem sub --context task`. It *does* mean
   `--context full` forks a shadow that contains only the seed note, never
   arm A's real claude history. To make `--context full` meaningful the bench
   would have to launch claude as `claude --session-id <claude_session_id>`
   (what `harness/claude_code.py::interactive_argv` does) and let tandem's
   tail loop run — which the headless bench does not do. **Keep arm A on the
   default `--context task`.**

7. **The codex worker inherits the user's `~/.codex/config.toml`.** In the
   live run that meant `sandbox: read-only`, `approval: never`, the user's
   MCP servers (one of which logged an auth error), and user plugins/skills —
   the worker spent two `exec` calls reading a `using-superpowers` skill file
   before answering "ok". For the bench this is non-hermetic (extra tokens,
   extra latency) and, more importantly, **read-only sandbox means rerouted
   subagents cannot edit files.** Pick bench tasks accordingly, or give the
   bench its own codex config.

8. **One pairing per cwd.** Pairing the same cwd twice creates a second row;
   `latest_session_for_cwd` then returns the newer one and the older row's
   shadow rollout leaks. `pair.py` never does this (it verifies instead), and
   `--clean` removes every row for the cwd.

9. **hook-route's warn stamp.** When nothing is rerouted, hook-route prints a
   notice once per claude `session_id` and stamps `H/warned/<session_id>`.
   `pair.py`'s probe uses a fixed bench-owned session id
   (`bench-pair-probe`) so it can never consume the real run's single notice.

10. **`tandem sub` never blocks on approval** (`codex exec` + `approval:
    never`), and `--context full` serializes across processes on
    `H/sub.lock`. Parallel bench runs sharing one `TANDEM_HOME` are safe for
    the default cold path (no lock, no shared cursor).

## 5. Alternatives considered and rejected

| Option | Verdict |
| --- | --- |
| `tandem` with no subcommand | Pairs, then immediately calls `_enter_session` → `shell.run_shell` → `InteractiveRunner` → `ptyrun.run_in_pty(["claude", "--session-id", …])`. Interactive by construction; driving it would mean a pty driver plus a way to make the claude TUI exit. |
| A one-shot `tandem "prompt"` form | Does not exist. `main` is a `click.group(invoke_without_command=True)` with only an `--active` option. Measured: `tandem "reply with ok"` → `Error: No such command 'reply with ok'.`, exit 2. |
| `tandem resume` / `status` / `sync` / `doctor` / `run --on` / `sub` | All call `_require_session` / `latest_session_for_cwd` and **exit 1** when there is no session. Measured, in an unpaired dir: `tandem status` and `tandem sub "reply with ok"` both print "No tandem session for this directory. Run `tandem` to start one." and exit 1. `tandem --help` lists no pair/init/create command. |
| Hand-written state: INSERT the row directly and skip the rollout | Satisfies the hook and default `tandem sub`, but hand-rolls tandem's schema and leaves `--context full` broken. Strictly worse than calling the real routine, which is right there. |
| Hand-written codex rollout | Would duplicate `CodexAdapter.session_meta`, including the non-obvious `model_provider: "openai"` field that codex ≥ 0.145 requires for resume. No reason to fork that knowledge into the bench. |

## 6. Live proof

```
$ python3 bench/pair.py --tandem-home …/final/home --cwd …/final/proj
paired a9c52175f56f  (…/final/proj)
  TANDEM_HOME:   …/final/home
  state.db:      …/final/home/state.db
  codex shadow:  ~/.codex/sessions/2026/08/01/rollout-2026-08-01T19-00-16-019fbeb2-cb27-7ed0-a5ce-5015db36214c.jsonl
  codex version: codex-cli 0.145.0
  hook-route:    REWRITE -> tandem:codex-worker
exit=0

$ cd …/final/proj
$ TANDEM_HOME=…/final/home .venv/bin/tandem hook-route < payload.json
{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow",
 "permissionDecisionReason": "tandem: rerouted to codex-worker",
 "updatedInput": {"description": "Summarize the repo", "prompt": "…",
                  "subagent_type": "tandem:codex-worker", "model": "haiku"}}}

# control, same payload, an empty TANDEM_HOME:
{"systemMessage": "tandem: subagent plugin is active but this directory has no
 paired tandem session — dispatches stay on claude. …"}

$ TANDEM_HOME=…/final/home .venv/bin/tandem sub "reply with ok"
OpenAI Codex v0.145.0
workdir: …/final/proj    model: gpt-5.6-sol    approval: never    sandbox: read-only
session id: 019fbeb2-e6d7-7bed-b54a-70c956284539
user: reply with ok
codex: ok
tokens used 5,071
exit=0
```

Teardown afterwards (`pair.py --clean`) removed the state row and the shadow
rollout; `~/.codex/sessions` was left with exactly the files that predated the
spike.

`tandem doctor` on the same pairing: both CLIs ✓, paired session ✓, codex
transcript resumable ✓, warnings only for the not-yet-created claude
transcript and the unset subagent model.
