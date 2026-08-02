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

> **Boundary of the green light.** `--verify-only` verifies pairing state and
> codex availability — that `tandem hook-route` reroutes *when it is invoked*.
> It does **not** verify hook registration: whether arm A's claude session
> invokes the hook at all depends on the tandem plugin being installed in
> whatever `CLAUDE_CONFIG_DIR` the runner launches claude with (the plugin
> registers `PreToolUse` / matcher `Agent|Task` / `tandem hook-route || true`,
> see `plugin/hooks/hooks.json`). The runner must ensure and check that
> separately; pair.py cannot see it.

The initially-active harness is always `claude` and is deliberately not
configurable — see caveat 11.

## 3. What lands where

For `--tandem-home H`, `--cwd C` (active harness is always `claude`):

| Location | Content |
| --- | --- |
| `H/state.db` → `sessions` | one row: `tandem_id`, `cwd = C` (realpath), `active = 'claude'`, `claude_session_id` (uuid4), `codex_session_id` (uuid7), `created_at`, `last_used_at` |
| `H/state.db` → `sync_cursors` | one row for source `claude` (the write-ahead cursor) |
| `$CODEX_HOME/sessions/YYYY/MM/DD/rollout-<ts>-<codex_session_id>.jsonl` | the seeded **codex shadow**: a `session_meta` line with `originator: "tandem"`, `model_provider: "openai"`, `cwd = C`, plus the tandem seed note as a `response_item` + `event_msg` pair |
| `H/subagents/<tandem_id>/…` | created later, by `tandem sub` (running markers, `-q` logs, retained forks) |
| `H/warned/<claude-session-id>` | created later, by hook-route's once-per-session "nothing was rerouted" notice |

Nothing is written to `~/.tandem`, and nothing is written under
`CLAUDE_CONFIG_DIR`: with the active harness fixed to `claude`, claude's own
transcript is created by claude itself when it runs, and tandem only mints the
id (this is exactly why `active = "codex"` is not offered — caveat 11). The
**codex rollout is the one artifact outside the bench home** — see §4.

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
   internal. If it is renamed or moved, the interpreter raises and `pair.py`
   surfaces the traceback and exits 1 — no partial state. Everything else
   (`StateStore`, adapters, `paths`) is stable module-level API. The one pin
   whose drift would *not* raise by itself is the lazy import inside
   `_pair_session`; caveat 5 covers it.

5. **Memory sync is off by default, and its suppression is asserted.** Real
   `tandem` pairing also runs `sync_memory_files(cwd)`, which can create
   `AGENTS.md` in the task directory from `CLAUDE.md` (or vice versa). That
   mutates the working tree under test and would make arm A's tree differ from
   arm B's, so `pair.py` replaces the function unless `--memory-sync` is
   passed. The replacement works only because `_pair_session` imports it
   lazily, inside the function body — a pin that would silently no-op if the
   import ever moved to module scope. So `pair.py` checks three things after
   pairing and fails loudly (rolling the pairing back) if any of them is off:
   the stub's own call count is exactly 1, no `memory: …` action lines were
   echoed, and both `CLAUDE.md` and `AGENTS.md` in the task tree are
   byte-identical to before. Memory sync is a no-op anyway in a directory that
   has neither file, but the assertions do not depend on that.

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

7. **The codex worker inherits the user's `~/.codex/config.toml`** — MCP
   servers (one logged an auth error in the live run), plugins and skills:
   the worker spent two `exec` calls reading a `using-superpowers` skill
   file before answering "ok". For the bench this is non-hermetic (extra
   tokens, extra latency). The *sandbox*, however, is no longer
   unconditionally the config default: since tandem PR #20 the hook stamps
   `$TANDEM_HOME/sandbox/<tandem-id>` from the dispatching session's
   `permission_mode` on every Agent/Task dispatch (`acceptEdits` /
   `bypassPermissions` → `workspace-write`, anything else → empty = config
   default, read-only in practice), and `tandem sub` reads the stamp. The
   live transcript in §5 shows `sandbox: read-only` because it was captured
   before PR #20 under `default` mode; a bench run under
   `--permission-mode bypassPermissions` gets `workspace-write` workers.

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

11. **`active = "codex"` is not offered, on purpose.** `_pair_session(…,
    "codex")` leaves `codex_session_id = None` and instead seeds a **claude**
    shadow transcript at
    `paths.claude_transcript_path(cwd, claude_session_id)` — i.e. under
    `CLAUDE_CONFIG_DIR`, which defaults to the user's `~/.claude/projects/`.
    That writes outside the bench's own directories, and the resulting pairing
    has no codex rollout, so verification fails and `--clean` (which only
    knows about codex rollouts) would not remove the stray file. `pair.py`
    hard-codes `"claude"` and exposes no `--active` flag. The bench never
    needs the other direction: arm A's active harness is claude by
    construction.

12. **Verification does not cover hook registration.** Restating the boundary
    from §2: exit 0 means the pairing is real and `tandem hook-route` reroutes
    when invoked. Whether claude *invokes* it depends on the tandem plugin
    being present in the `CLAUDE_CONFIG_DIR` the runner launches claude with.
    That check belongs to the runner.

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
