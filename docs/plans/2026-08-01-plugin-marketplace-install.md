# Plugin marketplace install + missed-reroute notice

Make the tandem Claude Code plugin installable directly from GitHub (no clone),
and surface a one-time, user-visible notice when the plugin is active but no
tandem session backs it. Design is agreed — do not re-explore alternatives.

## Context

All prior plugin work merged via PR #16 (commit 070e87d):

- The plugin lives at `plugin/` — manifest `plugin/.claude-plugin/plugin.json`,
  `plugin/hooks/hooks.json` (whose command `tandem hook-route || true` is
  load-bearing — never simplify it), `plugin/agents/codex-worker.md`. Today it
  installs only via `claude --plugin-dir <clone>/plugin`.
- `tandem hook-route` (`src/tandem/cli.py:349` wrapper + `src/tandem/hookroute.py`
  decision logic) deliberately emits nothing and exits 0 when the cwd has no
  paired tandem session — invisible no-op, dispatch proceeds natively.
- `src/tandem/hookroute.py` is pure decision logic; the CLI wrapper owns process
  concerns (stdin, stdout, exit codes, filesystem side effects).
- Config: `src/tandem/config.py` — `SubagentsConfig.route` is `"all" | "off"`,
  loaded from `$TANDEM_HOME/config.toml`; `paths.tandem_home()` resolves
  `$TANDEM_HOME` (default `~/.tandem`).
- Structural plugin tests: `tests/test_plugin.py` (4 tests). Hook tests:
  `tests/test_hookroute.py` (15 tests). Suite baseline: 194 passed.
- README has a "Subagents on the cheap model" section documenting clone +
  `--plugin-dir`. Design spec: `docs/specs/2026-07-31-codex-subagents-design.md`.
- Local claude CLI is 2.1.220.

## Global Constraints

- **Exit discipline (invariant):** `tandem hook-route` must NEVER block a
  dispatch and NEVER exit nonzero — every failure (stamp I/O, config, missing
  dirs, bad payload) degrades to native dispatch, silently or with the notice.
  No permission `deny` anywhere. The `|| true` in hooks.json stays.
- **TDD red-first:** write the failing test, watch it fail, then implement.
- **Full suite green:** `uv run pytest` — 194 baseline tests keep passing plus
  the new ones.
- **Purity split:** decision logic in `hookroute.py` stays pure (no I/O);
  stamp-file reads/writes and stdout live in the CLI wrapper layer.
- Never commit on main; work stays on this branch; one squash-merged PR.

## Task 1: Marketplace manifest, version alignment, structural tests

1. Tests first, in `tests/test_plugin.py` (same style as the existing
   hook-rewrite-target drift guard):
   - repo-root `.claude-plugin/marketplace.json` exists and parses as JSON;
   - its plugin entry's `source` resolves to the `plugin/` directory
     (relative to repo root);
   - its plugin entry's `name` matches `plugin/.claude-plugin/plugin.json`'s
     `name` (drift guard);
   - `plugin/.claude-plugin/plugin.json` `version` matches `pyproject.toml`'s
     `version` (drift guard for item 2).
2. Add repo-root `.claude-plugin/marketplace.json` declaring the plugin:
   marketplace name `"tandem"`, owner, `plugins: [{name: "tandem",
   source: "./plugin", description: …}]`. The exact schema MUST follow the
   verified findings in the workspace file `docs-verification.md` (checked
   against code.claude.com/docs/en/plugins-reference and the marketplace docs)
   — not this plan's field list.
3. Align `plugin/.claude-plugin/plugin.json` `"version"` from `0.1.0` to
   `0.1.5` (pyproject.toml's current version). Note in the commit message that
   this is a hand-maintained version string; single-sourcing is out of scope.

## Task 2: Missed-reroute notice

When the hook fires (plugin installed) but the cwd has NO paired tandem
session, surface a user-visible notice instead of pure silence — once per
claude session, not once per dispatch.

- **Mechanism:** emit hook JSON with a top-level `"systemMessage"` field and NO
  permission decision, so the dispatch still proceeds natively. The verified
  behavior of `systemMessage` for PreToolUse on claude 2.1.220 is recorded in
  the workspace file `docs-verification.md` — follow it. If it says
  systemMessage is NOT honored for PreToolUse, do NOT substitute a deny;
  document the limitation in the spec and skip the feature (report this back).
- **Scope of the notice:** only for `Agent`/`Task` payloads (same tool_name
  guard as routing). Warn when `cfg.route == "all"` and either (a) no paired
  session for cwd, or (b) a session IS paired but codex is missing or its
  version unsupported — text names the actual cause. NEVER warn when
  `cfg.route == "off"` (explicit user choice). No notice when a rewrite is
  emitted, and none for fork/bridge dispatches when a session is paired and
  codex is fine.
- **Suggested no-session text:** "tandem: subagent plugin is active but this
  directory has no paired tandem session — dispatches stay on claude. Run
  `tandem` here to enable codex subagents." Codex-missing text: same style,
  naming that cause instead.
- **Once per session:** the hook's stdin payload carries `session_id`; keep a
  stamp under `$TANDEM_HOME` (e.g. `warned/<session_id>`) and skip the message
  when stamped. Prune old stamps opportunistically (mtime-based is fine).
  Stamp I/O failures degrade to warn-anyway or stay-silent — never a nonzero
  exit. Stamp writes happen in the CLI wrapper (purity split); the pure layer
  decides "would warn" from inputs (route, has_session, codex_ok, tool_name,
  already-warned flag).
- **Tests** (in `tests/test_hookroute.py`, red-first):
  - notice emitted with NO permission decision on first dispatch (no
    `hookSpecificOutput`, no `decision` key);
  - suppressed on second dispatch with the same `session_id`;
  - suppressed when `route == "off"`;
  - codex-missing-with-paired-session variant emits the cause-naming notice;
  - rewrite path unchanged when a session exists and codex is fine;
  - exit code 0 everywhere, including stamp-dir failures (e.g. `TANDEM_HOME`
    pointing at an unwritable/occupied path).
- **Spec update** (`docs/specs/2026-07-31-codex-subagents-design.md`): the
  no-session row of the failure ladder / passthrough list becomes "native
  dispatch + one-time notice", and the exit-discipline section gains the
  systemMessage mechanism.

## Task 3: README + spec install story

1. README "Subagents on the cheap model": replace the `--plugin-dir`
   instructions with the two-step story:
   (a) `uv tool install tandem-cli` — the hook/bridge need the `tandem` binary
   on PATH; the plugin alone is inert without it;
   (b) `claude plugin marketplace add Bhavya6187/tandem` then
   `claude plugin install tandem@tandem`.
   Keep `--plugin-dir` documented as the local-development path. Mention that
   marketplace installs track the repo's default branch on update.
2. Update the spec's plugin/install bullet
   (`docs/specs/2026-07-31-codex-subagents-design.md`) to match the new
   install story.

## Task 4: Live verification

Before the PR (adapt commands to the verified CLI syntax in
`docs-verification.md` if it differs):

1. `claude plugin marketplace add <this repo's worktree path>` +
   `claude plugin install tandem@tandem`; `claude plugin list` shows it.
2. In a directory WITHOUT a tandem session, one subagent dispatch surfaces the
   notice exactly once and runs natively (headless `claude -p` is fine).
3. In a paired session, dispatches still reroute — or at minimum a
   `tandem hook-route` smoke test via stdin both ways (no-session → notice
   JSON; paired-session shape → rewrite JSON).
4. Clean up: remove the locally-added marketplace afterwards so the user's
   claude config is left as found.

Record the exact commands and observed output in the workspace report file.

## Ship

Branch + PR to main following repo conventions (squash merge, PR body
convention from prior PRs).
