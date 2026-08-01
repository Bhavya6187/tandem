# The A/B subagent bench

Does rerouting claude's subagents to codex change the outcome, the wall clock
or the token bill? This directory runs the experiment: **the same task, twice,
in two identical trees, with `tandem`'s subagent reroute ON and OFF.**

```
arm A   TANDEM_HOME/config.toml -> [subagents] route = "all"    every Agent
        + a tandem pairing for the task directory                dispatch is
                                                                 rewritten to
                                                                 tandem:codex-worker

arm B   TANDEM_HOME/config.toml -> [subagents] route = "off"     the hook
        + no pairing needed                                      returns
                                                                 nothing;
                                                                 subagents stay
                                                                 native
```

Everything else — model, prompt bytes, flags, permission mode, the checkout —
is identical between the arms. **That identity is the experiment.** The runner
records what actually happened (`extraction.json`), a family-specific verifier
scores it (`verdict.json`), and the aggregator turns the verdicts into a table.

Nothing here imports `tandem`; `bench/` is stdlib-only and shells out. Nothing
writes to `~/.tandem` — the bench owns two TANDEM_HOMEs of its own under
`bench/work/`.

---

## 1. Prerequisites

### 1.1 The tandem plugin, installed in your claude config

Arm A only works if claude actually *invokes* the hook, and that comes from the
plugin, not from the pairing:

```bash
claude plugin marketplace add Bhavya6187/tandem
claude plugin install tandem@tandem
claude plugin list          # expect: tandem@tandem ... enabled
```

The plugin is two files — `plugin/hooks/hooks.json`, which registers
`PreToolUse` / matcher `Agent|Task` / `tandem hook-route || true`, and
`plugin/agents/codex-worker.md`, the bridge agent the hook rewrites dispatches
to.

> **This is a user-level change and it stays installed after a bench run.** It
> is the one thing the bench cannot do inside its own sandbox. It affects every
> claude session on this machine: in an unpaired directory the hook prints a
> one-line notice once per session and changes nothing. To remove it:
>
> ```bash
> claude plugin uninstall tandem@tandem
> claude plugin marketplace remove tandem
> ```

### 1.2 A `tandem` BINARY new enough to have `hook-route`

**The PyPI release is stale and will silently give you an A/A test.** `tandem
hook-route` landed in `070e87d`, two commits *after* the `v0.1.5` tag that PyPI
serves — `git show v0.1.5:src/tandem/cli.py | grep hook` finds nothing, and
`v0.1.5` has no `plugin/` directory at all. A `pip install tandem-cli`
build exits non-zero on `hook-route`, the `|| true` in hooks.json swallows it,
and arm A quietly runs native subagents.

Two ways to get a current binary:

```bash
# a) the repo's own venv — this is what the runner uses by default
uv sync                       # creates .venv/bin/tandem from this checkout

# b) a user-level install from source
uv tool install --force --from /path/to/tandem tandem-cli
```

The runner prepends `dirname(--tandem-bin)` to the `PATH` of the claude child
it launches, so the `tandem` the hook resolves is the one the runner checked —
even if a stale one is earlier on your own PATH. `--tandem-bin` defaults to
`<repo>/.venv/bin/tandem` when it exists. `runner.py check` verifies both that
the binary answers `hook-route` *and* that `tandem` on the run PATH resolves to
that same file.

### 1.3 codex, authenticated

```bash
codex --version         # tandem's compat range is 0.140 <= codex < 0.150
```

Codex must be logged in **user-level**: the bench deliberately leaves
`CODEX_HOME` at `~/.codex` so the workers find your existing auth. See
[PAIRING.md](PAIRING.md) caveat 2 for what that costs (one
`originator: "tandem"` rollout per pairing, under `~/.codex/sessions/`).

### 1.4 uv, python 3.11+, and docker only for swebench

`bench/runner.py`, `verify.py` and `aggregate.py` need python **3.11+**
(`tomllib`). Use the repo venv: `.venv/bin/python bench/runner.py ...`.

The two families with their own scoring code shell out to `uvx`, so `uv` must
be installed; nothing is imported.

**Docker is needed only by the swebench family**, and only at *verify* time.
`runner.py check` asks for it only when a swebench task is selected — the
repoqa and lca families never touch it.

---

## 2. The two-arm design

### 2.1 TANDEM_HOME isolation

`runner.py check` creates and re-asserts two homes:

```
bench/work/tandem-home-a/config.toml     [subagents] route = "all"
bench/work/tandem-home-b/config.toml     [subagents] route = "off"
```

Each arm's claude process is launched with `TANDEM_HOME` pointed at its own
home, so the two arms share no state, no pairings and no subagent logs. A
hand-edit that turned the A/B into an A/A would otherwise be invisible in the
results, so on every `check`/`run` the runner re-asserts the route: a file with
the right `route` is left completely alone (any other `[subagents]` keys you
added survive), and a file with the wrong one is **overwritten whole** — those
other keys go with it.

### 2.2 What `pair.py` does — and the boundary of its green light

`tandem hook-route` rewrites a dispatch only when **both** hold: a state row
exists for this exact cwd, and codex is installed inside the compat range.
There is no non-interactive `tandem` subcommand that creates that row (bare
`tandem` pairs and then immediately enters a PTY), so `bench/pair.py` calls
tandem's own `tandem.cli._pair_session` in tandem's own interpreter.

```bash
python bench/pair.py --tandem-home <dir> --cwd <dir>                # create
python bench/pair.py --tandem-home <dir> --cwd <dir> --verify-only  # check
python bench/pair.py --tandem-home <dir> --cwd <dir> --clean        # tear down
```

`run_one()` calls it twice for every arm-A run: create, then `--verify-only`.

> **The green light stops at hook registration.** Exit 0 means the pairing is
> real and `tandem hook-route` returns a REWRITE decision *when it is invoked*.
> Whether claude invokes it at all depends on §1.1, which `pair.py` cannot see.
> The runner checks that separately (`claude plugin list`).

**Pairing is per-directory and is never inherited by subdirectories**, and the
cwd is matched as a byte-identical string. The runner realpaths every path it
hands to `pair.py` and to claude (`/tmp` → `/private/tmp` on macOS) because a
mismatch there un-pairs the directory silently. A provisioner that clones into
`<workdir>/repo` returns that subdirectory, and *it* is what gets paired.

---

## 3. Cookbook

Run everything from the repo root, with the repo's python.

### 3.1 check — every precondition, no cost

```bash
.venv/bin/python bench/runner.py check
.venv/bin/python bench/runner.py check --tasks repoqa-python-black
```

```
ok    claude (claude): 2.1.220
ok    claude plugin `tandem`: tandem@tandem
ok    tandem hook-route (/…/.venv/bin/tandem): hook-route responds
ok    tandem on the run PATH: /…/.venv/bin/tandem
ok    codex (codex): codex-cli 0.145.0
ok    pairing helper: /…/bench/pair.py
ok    bench TANDEM_HOMEs: a=…/tandem-home-a route=all, b=…/tandem-home-b route=off
ok    tasks selected: 1/1 runnable: repoqa-python-black
all preconditions met
```

The `hook-route` line is a real probe: it feeds the binary an `Agent` dispatch
under an empty `TANDEM_HOME` and requires the "no paired session" notice back.
A stale build fails here rather than in the results.

### 3.2 smoke — one repoqa task, both arms, ~5–7 minutes

```bash
.venv/bin/python bench/runner.py smoke --run-id SMOKE
.venv/bin/python bench/verify.py  --run-id SMOKE
.venv/bin/python bench/aggregate.py --run-id SMOKE
```

`smoke` pins `--tasks` to `[run] smoke_task` (the cheapest family: no docker),
both arms, `--repeats 1`, `--model haiku`. Use it after any change to tandem's
routing, to claude, or to codex. What a healthy smoke looks like:

```
  repoqa-python-black arm a repeat 0 ... valid  exit=0 245.68s dispatches=2 reroutes=2 notices=0
  repoqa-python-black arm b repeat 0 ... valid  exit=0 160.15s dispatches=2 reroutes=0 notices=0
```

Arm A: `reroutes == dispatches`, `notices == 0`, `valid`. Arm B: `reroutes ==
0`, `valid`. Anything else, read §5 before you read the scores.

### 3.3 one task, one arm

```bash
.venv/bin/python bench/runner.py run --run-id R1 \
    --tasks repoqa-go-fzf --arms a,b --repeats 3
.venv/bin/python bench/runner.py run --run-id R1 --tasks lca-1 --arms b   # retry one cell
```

`--dry-run` prints the plan (and the per-family timeout) without launching
anything. Re-running with the same `--run-id` overwrites those cells in place,
so narrow a retry with `--tasks`/`--arms` rather than repeating the matrix.

### 3.4 the full matrix

```bash
.venv/bin/python bench/runner.py run --run-id 20260801-full --repeats 3
```

Read §7 first. A failure inside `run_one` (pairing, provisioning) aborts the
whole matrix on purpose: every cause is an environment problem that would
repeat, and half a matrix of arm-B-only results is worse than none.

### 3.5 what lands where

```
bench/work/
  cache/                       downloaded datasets and dataset rows
  .workdirs/<run>/<task>/<arm>/<repeat>/repo/    the agent's checkout
  results/<run>/<task>/<arm>/<repeat>/
      prompt.txt          the exact prompt, scaffold included
      transcript.jsonl    claude's stream-json, hook events included
      stderr.log
      extraction.json     dispatches, reroutes, notices, tokens, validity
      meta.json           cmd, cwd, versions, timings, provision metadata
      verdict.json        THE record: validity + metrics + the verifier's score
      repoqa-eval/ | swebench-eval/     written by verify.py
  tandem-home-a/  tandem-home-b/        state.db, config.toml, subagents/
```

`.workdirs` is a dot-directory on purpose: it fills with clones of other
people's repositories, and `bench/work/` sits under the repo root, so a plain
`uv run pytest` used to walk straight into them (two checkouts of psf/black
gave pytest two files both claiming to be `tests.conftest`, and collection died
before a single tandem test ran). pytest's default `norecursedirs` skips `.*`.

The whole tree is git-ignored. Deleting `bench/work/` costs you the dataset
cache and every past result; deleting `bench/work/.workdirs/` between a run and
its verify costs you the swebench diff.

---

## 4. verify.py

```bash
.venv/bin/python bench/verify.py --run-id R1
.venv/bin/python bench/verify.py --run-id R1 --tasks lca-1 --arms a
.venv/bin/python bench/verify.py --run-id R1 --force        # re-score
```

The runner never scores: scoring shells out to docker and uvx, takes minutes to
an hour, and must not sit inside the window the bench is timing. A run ends
`"status": "unverified"` and this walks back over the result directories,
calls the family's `verify()`, and merges `status`/`passed`/`score`/`detail`
into each `verdict.json`. Everything the runner wrote is left alone. A verifier
that raises is recorded as `"status": "error"` on that one run and the walk
continues. Already-verified runs are skipped unless `--force`.

- **repoqa** — `uvx --from repoqa --with tree-sitter==0.21.3 python -m
  repoqa.compute_score`. Upstream's own scorer, upstream's own pass rule
  (`is_best_similar AND BLEU >= 0.8`). Seconds per run, but it parses a 71 MB
  json every time. The `tree-sitter` pin is load-bearing: a free resolve gets
  0.26 and every scoring run dies in `get_parser()`.
- **lca** — set-F1 against the pinned `expected_files`, twenty lines of
  stdlib. Instant.
- **swebench** — the real harness in docker (`swebench==4.1.0`). Minutes per
  instance, **emulated x86_64 on Apple Silicon**, and `--force` means it:
  `swebench.verify()` deletes the run's `swebench-eval/` before doing anything,
  so a forced re-verify is a full docker evaluation again — another image pull
  included. That deletion is not optional: the harness short-circuits on an
  existing `report.json` for the same run id, so without it a retry would
  replay the *previous* attempt's grade against the new agent's patch. Force
  one cell at a time.

---

## 5. Validity, and how to read the table

```bash
.venv/bin/python bench/aggregate.py --run-id SMOKE
```

```
| task | arm | runs | valid | invalid | pass rate | mean wall (s) | mean tokens | mean dispatches | mean reroutes |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| repoqa-python-black | a | 1 | 1 | 0 | 100% | 245.7 | 169865 | 2.00 | 2.00 |
| repoqa-python-black | b | 1 | 1 | 0 | 100% | 160.2 | 1563719 | 2.00 | 0.00 |
| **all tasks** | a | 1 | 1 | 0 | 100% | 245.7 | 169865 | 2.00 | 2.00 |
| **all tasks** | b | 1 | 1 | 0 | 100% | 160.2 | 1563719 | 2.00 | 0.00 |
```

The aggregator reads **only** `verdict.json`, so the table can never disagree
with the per-run record it summarises. `--json` dumps the same numbers.

**Invalid runs are excluded from every mean and from the pass rate**, and
counted in a note under the table. That is the whole point of the flag: a run
that did not measure what the arm exists to measure would pull the two arms
towards each other, which is precisely the effect being measured.

| validity | meaning |
| --- | --- |
| `valid` | the run measured its arm |
| `invalid_no_reroute` (arm A) | nothing rerouted, or `tandem hook-route` printed its "nothing was rerouted" notice. The hook fired and declined — no pairing, or codex outside the compat range. This run measured arm B a second time. |
| `invalid_partial_reroute` (arm A) | some dispatches rerouted and some ran natively, silently. `route()` passes through `fork` subagents, dispatches already aimed at the bridge, and blank prompts, and the notice stays silent for exactly those. The scaffold asks for ≥2 parallel dispatches, so one-of-each is entirely reachable — and averaging it in would blend the arms. Only dispatches claude actually spawned (`task_started`) count: a dispatch still in flight when a run is killed is neither native nor rerouted, so a timeout cannot flip an arm-A run into this state and out of the aggregate. |
| `invalid_leak` (arm B) | something rerouted in the arm that must not reroute. Contaminated. |

Warnings do **not** exclude a run, but the table lists which rows have them and
you should open those `verdict.json` files before trusting the row. They fire
for: no dispatches at all (the scaffold did not take), the hook's decline
notice, a partial reroute, more hook rewrites than spawned bridge agents,
dispatches that never reached `task_started` (named by `tool_use_id`, and
counted in `unspawned_dispatches`), a run with **no result event** (killed or
died — its token totals fall back to per-message usage, deduped per assistant
message, and are not session-wide), and an error final result.

Degenerate means to watch for: a `mean` over one valid run is that run;
`pass rate` over zero verified runs prints `-`, not 0%; timeouts are counted,
not excluded, because a run that ran out of clock is a measurement.

The authoritative reroute signal is claude's `system` / `task_started` event
(`subagent_type` after hooks). The `tool_use` block's `subagent_type` is the
type the *model* asked for and still reads `Explore` on a rerouted dispatch —
counting reroutes there reports zero every time. The hook's own stdout
(`--include-hook-events`) corroborates but carries no `tool_use_id`, and the
`tandem sub` logs under the arm-A home are the third, out-of-band witness.

---

## 6. Measurement caveats — read these before quoting a number

1. **Arm A's cost and token columns are claude-side only.** The codex worker's
   spend is invisible to claude's `result` event and therefore to the bench.
   Measured, one repoqa task with two workers: claude-side `$0.117` / 170k
   tokens, plus **111,823 codex tokens** that appear nowhere in the table (they
   are in `bench/work/tandem-home-a/subagents/<id>/logs/*.log`, at the
   "tokens used" line). Any "arm A is cheaper" reading of this bench is
   incomplete by construction.
2. **`tokens_total` is dominated by cache reads.** It is
   input + output + cache_read + cache_creation. Measured: cache reads alone
   were 75% of arm A's total and 93% of arm B's, and with cache creation both
   arms are over 94% cache. Barely 6% of the number in that column is the
   agent's own input and output. Label the column accordingly, or read
   `tokens_input` / `tokens_output` from `verdict.json` instead.
3. **Rerouted workers run in codex's read-only sandbox.** They physically
   cannot edit files, so the shared scaffold tells subagents in *both* arms to
   investigate only and leaves every edit to the main agent. That keeps the
   arms comparable; it also means this bench does not measure writing
   subagents at all.
4. **Arm A is not hermetic.** The worker inherits the user's
   `~/.codex/config.toml`: their model, their MCP servers, their plugins and
   skills. In the smoke, every worker read a `using-superpowers` skill file
   before answering and logged an auth failure from a GitHub MCP server. Both
   cost tokens and latency that have nothing to do with routing, and they will
   differ on someone else's machine.
5. **F1 ≥ 0.5 rewards terse answers (lca).** One correct file out of three
   expected scores exactly 0.5 and *passes*; naming all three plus two wrong
   ones scores 0.75. A single right guess is a pass. Read `precision` and
   `recall` in `verdict.json` before believing a pass rate.
6. **The answer seal is BY NAME only.** `clone_at` deletes every ref, expires
   the reflogs and removes `FETCH_HEAD`, so `git log --all`, `git branch -a`
   and `git log --grep=<issue>` cannot reach the fixing commit. The objects may
   still be in the pack: a determined agent can enumerate them
   (`git cat-file --batch-all-objects`) and, since `remote.origin.url` is
   deliberately kept, one `git fetch` restores everything. Treat a suspiciously
   perfect answer as a finding, not as a score.
7. **Tags are pruned, so `pip install -e .` breaks in a provisioned tree.**
   Any project using `setuptools_scm` cannot derive a version without tags.
   This is symmetric across the arms, and the prompts do not ask for an install
   — but it is what an agent that tries one will hit.
8. **SWE-bench runs x86_64 EMULATED on Apple Silicon.** `make_test_spec()`
   defaults `arch="x86_64"` and `run_evaluation` never overrides it; 4.1.0's
   `USE_X86` set is dead code. `arm64_image = true` in `tasks.toml` records a
   Docker Hub survey, not what gets pulled. It works (a django gold patch
   resolved in 123 s) but budget runtime for emulation.
9. **`--cache_level env` is a deliberate trade.** The harness deletes the
   ~2 GB instance image after every run, so each cell re-pulls: less disk, more
   minutes, and one more chance for a registry timeout. A pull failure is
   recorded as `status: "error"` — not a zero — and should be retried, not
   averaged in.
10. **A `no_code_block` fail can be a formatting miss, not a search miss.**
    The verifier scores the newest turn that actually contains a fenced block,
    so an async dispatch's trailing fence-free "task complete" summary no
    longer erases the answer the agent already gave. It is still the agent's
    job to produce a block at all; `answer_had_code_block: false` in
    `verdict.json` tells a formatting miss from a search miss.
11. **That fix is partial, in arm A's direction.** It only rescues an answer
    from a trailing turn with **no** fence. "Newest turn with any fence" is
    not "newest turn with the answer": when the trailing summary re-quotes
    even one line of the function, that summary outranks the complete earlier
    answer and is what gets scored. `answer_had_code_block` is `true` for the
    fragment and `true` for the full answer, so no *content* field separates
    *the re-quoting summary* from *the answer it displaced* — but both
    repoqa's and lca's `detail` now carry **`answer_turn_index`** (0-based)
    and **`answer_turns_total`**, which say which of the candidate turns was
    scored. `answer_turn_index == answer_turns_total - 1` with a total above
    1 is the shape a displacement takes and the row to open
    `transcript.jsonl` for; a lower index is the rescue working. They narrow
    the search, they do not detect the displacement. Arm A
    structurally produces more of those trailing turns than arm B, and how
    many is not deterministic (the same task gave arm A three result events in
    one smoke run and one in the next), so a smaller, same-signed version of
    the shape-bias the fix exists to remove is still there. A partial or
    oddly-truncated arm-A answer is a reason to open `transcript.jsonl`, not a
    reason to trust the row.

---

## 7. What the full matrix costs

10 tasks × 2 arms × `--repeats`. Grounded in the smoke's measured numbers
(claude **haiku**, repoqa-python-black, two full smokes = 2 runs per arm) and
scaled by each family's timeout and shape. **Assumptions are stated; treat
everything below as an order of magnitude, not a quote.**

Measured, per run: arm A **185 s / $0.123** claude-side, arm B **159 s /
$0.387**. (Arm A is cheaper on claude precisely because the work moved to
codex — see caveat 1.)

| family | cells (repeats=1) | assumption | wall clock | claude-side $ |
| --- | ---: | --- | ---: | ---: |
| repoqa | 3 × 2 = 6 | as measured | ~17 min | ~$1.5 |
| lca | 2 × 2 = 4 | same shape: read-only localization, same 900 s timeout | ~11 min | ~$1.0 |
| swebench | 5 × 2 = 10 | an agent that must actually edit code; 3–8× the repoqa run, 3600 s ceiling | 2–4 h | ~$15 |
| **run total** | **20** | | **~3 h** | **~$18** |
| verify: repoqa + lca | 10 | seconds each | ~1 min | $0 |
| verify: swebench | 10 | ~2–4 min docker each, emulated, plus a ~2 GB pull per cell | 40–90 min | $0 |

So: **a full single-repeat matrix is roughly half a working day and ~$20 of
claude, plus ~20 GB of image pulls and an invisible codex bill on arm A** (the
smoke's 112k codex tokens per repoqa cell is the only measured anchor).

Multipliers, all of them real:

- `--repeats 3` triples everything. With one repeat, a single flaky run moves
  a pass rate from 100% to 0%.
- A stronger main-agent model is the big one: these numbers are **haiku**.
  Budget 5–15× the claude-side dollars for a frontier model, and longer wall
  clocks with it.
- Worst case is the timeout, not the mean: if every swebench cell ran to its
  3600 s ceiling the run phase alone is 10 h.
- The first swebench cell for an instance pulls its image; only
  `django__django-11885`'s has been pulled on this machine. A missing or
  slow-to-pull image shows up as `status: "error"`, which is retryable.

Start with `--tasks` on one family. Nothing in the harness needs the whole
matrix to produce a usable table.

---

## 8. Cleaning up

```bash
python bench/pair.py --tandem-home bench/work/tandem-home-a --cwd <dir> --clean
rm -rf bench/work/.workdirs           # the clones (results survive)
rm -rf bench/work                     # everything, cache included
claude plugin uninstall tandem@tandem # the one user-level change (§1.1)
```

`--clean` removes the state rows for a cwd **and** the tandem-authored codex
rollouts it created under `~/.codex/sessions/` (it re-reads each file's
`session_meta` and refuses to delete anything that is not tandem's for that
cwd). Deleting a bench TANDEM_HOME outright also works and leaves those
rollouts behind.

---

## 9. Files

| file | what it is |
| --- | --- |
| `runner.py` | `check` / `run` / `smoke`: preconditions, the two arms, the stream-json extraction, validity |
| `pair.py` | headless tandem pairing for one directory (arm A only) |
| `PAIRING.md` | how the pairing works, what it writes, and 12 caveats |
| `verify.py` | the scorer CLI: `verdict.json` → verified |
| `aggregate.py` | verdicts → the results table |
| `tasks.toml` | the pinned task set, the run defaults, the pinned scorer versions |
| `family_api.py` | the contract a family module satisfies |
| `family_common.py` | answer extraction, fenced blocks, set F1, cloning, the cache |
| `families/*.py` | one module per benchmark: `provision()` and `verify()` |
