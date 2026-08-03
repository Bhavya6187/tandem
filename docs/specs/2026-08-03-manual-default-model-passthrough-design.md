# Manual routing by default + model pass-through — design

2026-08-03. Ships as 0.1.8.

## Why

Live phrase testing (headless `claude -p`, opus orchestrator, plugin 0.1.7,
`route = "manual"`, one run per phrase) showed that agent *selection* is not
manual mode's weakness: all ten GPT-invoking phrasings — "ask GPT", "use
codex", "ask ChatGPT", "OpenAI's model", "ask gpt-5", "ask o3", "second
opinion from gpt", "what does GPT think", and a GPT+Claude mix-and-match —
dispatched `tandem:gpt`, while the control ("use a subagent") correctly
stayed native. The mix-and-match case is also the argument for the default
flip: under `route = "all"` the hook rewrites the Claude-side dispatch to
codex too, so "compare Claude and GPT" silently becomes "codex twice".

What broke was everything after selection:

- **Named models are dropped or mangled.** "ask gpt-5" ran the config
  default silently. "ask o3" made the orchestrator write meta-instructions
  into the brief ("Use tandem sub to have the o3 model review…" — passed
  verbatim to codex as the task) and the haiku relay went off-script
  (`tandem sub --help`, an invented `--model o3`). `tandem sub -m` exists;
  the relay's fixed command form never exposes it, and nothing ever reports
  which model actually ran.
- On failure, relay and orchestrator flail (retries, `--help`, raw
  `codex exec` fallback that bypasses sandbox-consent stamping).
- Manual mode gives no "run `tandem` here" hint in unpaired directories.

Scope decision: this change ships the default flip and the model
pass-through only. Failure-path hardening, the unpaired-directory notice
under manual, and a mode-switch command are explicitly deferred.

## Section 1 — default flip

`SubagentsConfig.route` default changes `"all"` → `"manual"` in
`config.py`. The `all` code path is unchanged and stays documented; users
who want auto-reroute set it explicitly. The missed-reroute notice keeps
its `route == "all"` gate. `doctor` needs no change (its subagent checks
already stay on under manual). Copy that claims auto-routing is the
default is rewritten around the new story: root README `[subagents]` block
and mode bullets, plugin README ("Routing is enabled by default", hook
behavior section).

## Section 2 — model pass-through

The orchestrator can only reach the relay through prompt text, so the
model name travels as a structured first line of the brief, and the CLI —
not the haiku relay — does the parsing.

- **Description** (`plugin/agents/gpt.md` frontmatter only) gains: "If the
  user asked for a specific model, put `tandem-model: <name>` as the first
  line of the task" — scoped to a *named* model, never a bare "ask gpt",
  which has no name to translate and would only invent an unresolvable one.
  Both relay bodies stay byte-for-byte identical to each other;
  `codex-worker.md` does not solicit headers.
- **Parsing** (`tandem sub`, quiet or not): the brief's first line is
  right-stripped (a trailing space, tab, or a CRLF's `\r` must not
  disqualify it), then matched case-insensitively against
  `tandem-model:` + optional spaces/tabs + `<name>`, where `<name>` is
  1–64 chars of `[A-Za-z0-9._/ :-]` that neither starts nor ends with a
  space. Internal spaces are legal because users *speak* model names
  ("5.4 mini"); resolution normalizes them away. On a match the line is
  stripped from the brief and `<name>` is the requested model.
  - A first line that does not open with the prefix is ordinary task text,
    returned untouched — no guessing, no partial strips.
  - A first line that *does* open with the prefix but does not match is a
    hard failure: `error: malformed tandem-model header: <line>` on
    stderr, exit 1, before any catalog read or session lookup. Silently
    passing a near-miss through is the exact bug this section exists to
    kill — the worker would run the config default while the stray line
    shipped to codex as task text, with nothing said to anyone. The cost
    is that a brief whose first line genuinely opens with the literal
    `tandem-model:` (prose about the protocol) is rejected; that is the
    accepted trade, and it fails loudly enough to reword.
- **Precedence**: explicit `-m` flag > header > `[subagents] model` config
  > codex's own default. The flag wins because only a human at the CLI
  types it. A recognized header line is stripped from the brief even when
  the flag overrides it, and the trailer names the model that actually ran,
  not the one the header asked for.
- **Feedback**: quiet mode appends a `[tandem-sub model: <name>]` trailer
  to stdout only when a header was present (blocked-write-trailer pattern;
  the relay echoes it verbatim upstream). Non-quiet mode prints
  `worker model: <name>` to stderr. The worker log always records the
  resolved model, header or not.
- **Resolution against the live catalog**: codex needs an exact slug —
  probed live (codex-cli 0.145.0, ChatGPT account): `-m gpt-5` and `-m o3`
  both 400 with "The '…' model is not supported when using Codex with a
  ChatGPT account", no suggestions, after a full API round-trip. The valid
  set is account- and version-specific (this machine: `gpt-5.6-sol`,
  `gpt-5.6-terra`, `gpt-5.6-luna`, `gpt-5.5`, `gpt-5.4`, `gpt-5.4-mini`),
  so no hardcoded alias table can stay fresh. Instead, `tandem sub`
  resolves the header value against **`~/.codex/models_cache.json`** — the
  same `models` array `codex debug models` renders, but read straight from
  disk with no subprocess: codex maintains the file (refreshed on every
  exec), the README already points users at it, and reading the wrapped
  CLI's own files is tandem's established method (`docs/formats.md`).
  `codex debug models` remains the human-facing debugging surface for the
  same data. Resolution (run only when a header is present):
  - Normalize both sides: lowercase, strip non-alphanumerics.
  - Exact normalized match on a slug or display name wins.
  - Else a normalized-substring match (header inside candidate) that hits
    exactly one visible model wins — "sol" → `gpt-5.6-sol`,
    "5.4 mini" → `gpt-5.4-mini`.
  - Else fail fast before invoking codex, nonzero, with an error that
    lists the visible slugs: `unknown model 'o3'; this codex offers:
    gpt-5.6-sol, gpt-5.6-terra, …`. The relay returns that verbatim as
    `[tandem-sub failed]`, so the orchestrating session can retry with a
    valid slug or surface the choice to the user.
  - Hidden catalog entries (`visibility: "hide"`, e.g. `codex-auto-review`)
    are excluded from matching and from the error listing. If the catalog
    cannot be read (absent, unreadable, malformed JSON, no `models` array,
    or no usable entries in it), fall back to passing the header value
    through as `-m` verbatim.
  The description sentence tells the orchestrator to pass the model name
  as the user said it — translation is the CLI's job, not the model's.

## Section 3 — versioning

Cross-version compatibility is explicitly out of scope for now (operator
call, 2026-08-03): plugin and CLI are assumed to update together. Version
goes to 0.1.8 in `pyproject.toml` and `plugin/.claude-plugin/plugin.json`
in lockstep (drift-guard test enforces). One structural note stands
because it justifies the parser: when a header exists it is always the
brief's first line — hook rewrites target `codex-worker`, which never
solicits headers, and explicit `tandem:gpt` dispatches pass the hook
untouched — so first-line-only parsing is safe.

## Section 4 — testing

- Unit: config default-flip assertions; header parse (match, no-match,
  malformed name), precedence, and trailer cases alongside the existing
  `sub` tests; catalog resolution against a fixtured `models_cache.json`
  payload (exact slug, display-name, case/punctuation-insensitive,
  unique-substring, ambiguous → error listing, no-match → error listing,
  hidden entries excluded, unreadable catalog → verbatim fallback);
  manual-mode hook tests already assert never-rewrites / never-warns and
  stay unchanged.
- Live acceptance: re-run the phrase-lab bed ("ask gpt-5", "ask o3") with a
  paired session and confirm the header is emitted by the orchestrator,
  `-m` lands in the codex argv, and the trailer comes back — the two runs
  that failed in the research become the acceptance test.
