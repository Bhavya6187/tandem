# Local A/B bench harness: native claude subagents vs codex-rerouted

Measure whether routing Claude Code subagent dispatches to codex/GPT models
(via the tandem plugin) degrades task performance. Ten pinned tasks from three
benchmarks, each run in two arms (reroute ON vs OFF), with objective
scriptable verification. Design agreed in conversation — brainstorming
deliberately skipped; do not re-explore alternatives.

## Context

- Merged on main: the tandem plugin installs from the marketplace
  (`claude plugin marketplace add Bhavya6187/tandem` + `install tandem@tandem`),
  and `tandem hook-route` rewrites Agent/Task dispatches to
  `tandem:codex-worker` when route="all", a paired session exists for the cwd,
  and codex is available. Route="off" (config) disables rewriting AND the
  notice — that is the arm-B toggle.
- The hook resolves state via `$TANDEM_HOME` (config.toml, state.db, warned/).
  The hook subprocess inherits the claude session's environment, so running
  `claude -p` with a bench-owned `TANDEM_HOME` fully isolates the experiment
  from the user's real `~/.tandem`. Verified mechanism: cli.py hook_route_cmd
  reads config + StateStore under paths.tandem_home() which honors the env var.
- PyPI tandem-cli is stale (predates hook-route); the bench machine runs the
  repo's own binary (worktree venv or `uv tool install --from`).
- Machine: darwin arm64. SWE-bench evaluation runs in docker; all five pinned
  instances have prebuilt arm64 images (verified against Docker Hub).
- Verified harness facts (from live research, 2026-08-01):
  - swebench 4.1.0 (pip), `python -m swebench.harness.run_evaluation
    --dataset_name princeton-nlp/SWE-bench_Verified --predictions_path …
    -i <instance_id> --run_id <id>`; predictions jsonl rows need
    `instance_id`, `model_name_or_path`, `model_patch`; resolution = all
    FAIL_TO_PASS and PASS_TO_PASS pass.
  - RepoQA: data at
    github.com/evalplus/repoqa_release/releases/download/2024-06-23/repoqa-2024-06-23.json.gz
    (600 items; rows carry repo, commit_sha, needles with name/path/lines/
    description). Scorer `repoqa.compute_score` (pip package `repoqa`) is
    decoupled: needs jsonl rows with `language, repo, name, output(list[str]),
    position_ratio, needle_token_start, needle_token_end`; pass = tree-sitter
    extract + argmax-name match + BLEU ≥ 0.8. 28/600 descriptions leak the
    function name — screen ours.
  - LCA bug localization: HF dataset `JetBrains-Research/lca-bug-localization`
    (rows carry base_sha/head_sha + issue); metric is set precision/recall/F1
    over changed files. Rows fetchable via the HF datasets-server REST API.
- The 10 pinned tasks (all IDs live-verified):
  - SWE-bench Verified (5): django__django-16263, django__django-11885,
    astropy__astropy-13398, sympy__sympy-16597,
    scikit-learn__scikit-learn-25102.
  - RepoQA (3): psf/black `_merge_string_group` (python), expressjs/express
    `sendfile` (typescript), junegunn/fzf `newRange` (go) — swap any
    name-leaking description for openai/openai-python
    `to_custom_raw_response_wrapper` or seanmonstar/warp `reject`.
  - LCA bug localization (2): rows chosen in Task 3 (mid-sized repos), then
    PINNED in the config (hub row index + repo + shas) for reproducibility.

## Global Constraints

- Harness lives in `bench/` at the repo root. Runtime artifacts (clones,
  results, tandem homes, transcripts) live under `bench/work/`, which is
  git-ignored. Nothing under bench/ imports from src/tandem (subprocess calls
  only) and nothing in src/ or plugin/ changes in this plan.
- Harness code is Python 3.11+ stdlib-only (urllib, json, tomllib,
  subprocess, argparse, pathlib). swebench/repoqa are invoked as external
  commands in their own uv-managed environments (`uvx`/`uv tool run` or a
  documented pip env), never imported as project deps. pyproject.toml deps
  stay untouched.
- The experiment NEVER touches `~/.tandem` or the user's claude plugin
  config: each arm runs with a bench-owned TANDEM_HOME
  (`bench/work/tandem-home-<arm>/`), arm B's config.toml says
  `[subagents] route = "off"`, arm A's says `route = "all"`. The plugin is
  assumed installed by the operator (README documents it); the harness
  verifies preconditions and refuses to run with actionable messages instead
  of mutating the user's config.
- Determinism/bookkeeping: every run writes a self-contained result dir
  `bench/work/results/<run_id>/<task>/<arm>/<repeat>/` containing the
  stream-json transcript, timing, token counts, dispatch counts, verifier
  output, and a verdict.json. The aggregator only reads verdict.json files.
- Unit tests for harness logic (config parsing, dispatch counting from
  stream-json fixtures, verifier output parsing, F1 math) live in
  `tests/test_bench_*.py`, stdlib-only, no network, TDD red-first — they run
  in the repo's normal `uv run pytest` (current baseline: 218 passed).
  Anything needing network/docker/claude stays out of unit tests.
- Tests and README claims about claude CLI behavior must match claude
  2.1.220 (`--output-format stream-json`, `--model`, `-p`). Live smoke is the
  arbiter.
- Branch-only work; one squash-merged PR at the end; commits follow repo
  style.

## Task 1: Headless pairing recipe (research spike — riskiest first)

Arm A needs a paired tandem session for each task working directory, created
non-interactively, inside a bench-owned TANDEM_HOME. Read src/tandem (cli.py,
ops.py, state.py, runner.py) and determine the supported way to create a
session pair for a cwd without an interactive TUI: an existing subcommand, a
one-shot `tandem "prompt"` form, or (last resort) direct StateStore seeding —
but prefer whatever `tandem sub`/hook-route actually requires
(`latest_session_for_cwd`): understand what minimal state satisfies it AND
still lets a real `tandem sub` dispatch succeed end-to-end (the codex side
must genuinely run).

Deliverable: `bench/PAIRING.md` documenting the exact recipe (commands, env,
what state lands where), plus a `bench/pair.py` helper implementing it
(stdlib-only), plus a live proof: in a scratch dir with bench TANDEM_HOME,
`tandem hook-route` fed a realistic payload returns a rewrite decision, and
one real `tandem sub "reply with ok"` completes on codex. If a genuinely
non-interactive path does not exist, STOP and report BLOCKED with the
evidence — the controller decides between a pty driver and a manual step.

## Task 2: Runner + config scaffold

- `bench/tasks.toml`: the 10 pinned tasks (fields per family:
  swebench: instance_id, repo, base_commit env setup ref, arm64_image=true;
  repoqa: language, repo, commit_sha, needle name/path, description ref;
  lca: placeholder section Task 3 fills — schema documented now).
  Plus `[run]` defaults: repeats=1, model="" (session default), timeout_s
  per family, claude extra flags.
- `bench/runner.py` (CLI: `python bench/runner.py --tasks t1,t2 --arms a,b
  --repeats N --run-id X --smoke`):
  - precondition check subcommand (`--check`): claude on PATH + version,
    plugin `tandem` installed (`claude plugin list`), tandem binary has
    hook-route (stdin smoke), codex on PATH + `codex --version`, docker up
    (only if swebench tasks selected), bench TANDEM_HOMEs initialized.
  - per run: provision workdir (Task 3's provisioners), arm env
    (TANDEM_HOME, PATH), build the task prompt from the family template —
    every template mandates subagent use ("investigate with parallel
    subagents before acting") — invoke `claude -p <prompt>
    --output-format stream-json --verbose` with cwd=workdir, capture
    transcript, wall-clock, exit code.
  - post-run extraction: token totals and dispatch counts parsed from the
    stream-json (count Task/Agent tool_use events; count rerouted ones by
    `tandem:codex-worker` in updatedInput/agent type; count notices).
    Arm A runs with 0 reroutes are marked `invalid_no_reroute`, arm B runs
    with any reroute are marked `invalid_leak` — excluded by the aggregator.
  - writes verdict.json skeleton (verifier fills score fields).
- `bench/aggregate.py`: reads all verdict.json under a run id, prints the
  results table (per task and per arm: pass rate, mean wall-clock, tokens,
  dispatches, invalid runs) as markdown.
- Unit tests (TDD): tasks.toml parses and pins exactly the 10 tasks;
  dispatch/token extraction from a fixture stream-json transcript (craft the
  fixture from a real transcript's shape); invalid-run marking; aggregator
  math on synthetic verdicts.

## Task 3: Provisioners + verifiers per family

- swebench: provisioner clones repo at base commit into
  `bench/work/repos/<instance>/` (shallow-ish but full enough for git diff),
  applies the dataset's `environment_setup_commit` checkout semantics
  (document: agent edits only; no test_patch application — the eval harness
  owns that). Post-run: `git diff` → predictions jsonl row; verifier invokes
  swebench run_evaluation (docker) with `-i <instance>` in an isolated uv
  env, parses the report json for resolved status into verdict.json.
- repoqa: provisioner downloads + caches the release json.gz, screens the 3
  pinned needles for name-leak (description contains the function name →
  swap per plan and record the swap in tasks.toml), clones repo@commit_sha,
  computes position_ratio/needle_token_* fields from the dataset row.
  Prompt = the needle description + instruction to reply with the exact
  function in a fenced code block. Post-run: extract fenced block(s) →
  scorer jsonl; verifier runs `repoqa.compute_score` in its own env, parses
  pass/fail into verdict.json.
- lca: provisioner fetches candidate rows via the HF datasets-server REST
  API, selects 2 mid-sized ones (repo clone < ~200MB, ≥2 changed files,
  issue text self-contained), PINS them into tasks.toml (row id, repo,
  base_sha, head_sha, ground-truth file list), clones at base_sha. Prompt
  asks for the exact list of files that must change, one per line, inside a
  fenced block. Verifier: parse list, compute precision/recall/F1 vs ground
  truth in stdlib Python; pass = F1 ≥ 0.5 (threshold recorded in
  tasks.toml, adjustable).
- Unit tests (TDD): patch-extraction → predictions row from a fixture git
  diff; fenced-block extraction (multiple blocks, no block, language tags);
  F1 math including empty-prediction and superset cases; swebench report
  parsing and repoqa result parsing from fixture outputs.

## Task 4: Smoke run + README + results template

- Live smoke (`--smoke`): ONE RepoQA task (cheapest family: no docker), both
  arms, repeats=1, `--model haiku` for the main agent to keep cost minimal.
  Arm A must show ≥1 reroute + a real codex completion; arm B must show 0
  reroutes and ≥1 native subagent. Verifier must produce a real verdict for
  both. Fix whatever the smoke exposes (that is its job); record the full
  smoke evidence in the task report.
- `bench/README.md`: prerequisites (plugin install from marketplace, source
  install of tandem-cli until next release, codex auth, docker for swebench
  family, uv), the two-arm design, how TANDEM_HOME isolation works, run
  cookbook (check → smoke → single task → full matrix), cost/runtime
  warning for the full matrix (order-of-magnitude estimate per family),
  validity rules (invalid_no_reroute / invalid_leak), and how to read the
  aggregate table.
- Root README gets ONE line pointing at bench/README.md under Development
  (do not restructure anything).

## Ship

Branch + squash-merge PR to main per repo conventions. The PR body includes
the smoke-run table as evidence. Running the full 10-task matrix is NOT part
of this plan (cost is the operator's call); the README documents it.
