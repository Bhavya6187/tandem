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
  line of the task." Both relay bodies stay byte-for-byte identical to each
  other; `codex-worker.md` does not solicit headers.
- **Parsing** (`tandem sub`, quiet or not): if the brief's first line
  full-matches `tandem-model: <name>` with `<name>` matching
  `[A-Za-z0-9._/:-]{1,64}`, strip the line and use `<name>` as the codex
  model. A first line that does not full-match stays in the brief
  untouched — no guessing, no partial strips.
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
- **Unknown models**: passed through verbatim; codex's own error surfaces
  via the existing nonzero-exit path. No alias table.

## Section 3 — compatibility

- New plugin + old tandem: the header reaches codex as brief text — a
  mostly harmless meta line; behavior degrades to today's.
- Old plugin + new tandem: no headers are ever emitted; nothing changes.
- Under `route = "all"`, hook rewrites target `codex-worker` (whose
  description doesn't solicit headers) and explicit `tandem:gpt` dispatches
  pass the hook untouched, so when a header exists it is always the brief's
  first line. First-line-only parsing is therefore safe; the hook's
  agent-body prefixing never lands in front of a header.
- Version: 0.1.8 in `pyproject.toml` and `plugin/.claude-plugin/plugin.json`
  in lockstep (drift-guard test enforces).

## Section 4 — testing

- Unit: config default-flip assertions; header parse (match, no-match,
  malformed name), precedence, and trailer cases alongside the existing
  `sub` tests; manual-mode hook tests already assert never-rewrites /
  never-warns and stay unchanged.
- Live acceptance: re-run the phrase-lab bed ("ask gpt-5", "ask o3") with a
  paired session and confirm the header is emitted by the orchestrator,
  `-m` lands in the codex argv, and the trailer comes back — the two runs
  that failed in the research become the acceptance test.
