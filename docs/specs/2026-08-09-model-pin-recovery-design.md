# Model-pin recovery — design

2026-08-09. Companion to the manual-routing model pass-through
([2026-08-03](2026-08-03-manual-default-model-passthrough-design.md)):
that spec gave briefs a `tandem-model:` first line; this one makes the
line's delivery survive the relay that carries it.

## Why

A transcript audit (2026-08-08, all thirteen relay dispatches recorded
under `~/.claude/projects/…/subagents/`) split the pass-through pipeline
into its three hops and measured each:

- **Parent → relay: lossless.** Every dispatch where the user named a
  model (6/6) reached the relay with `tandem-model: …` intact as the
  first line of its task message. Selection and header-authoring are not
  the problem.
- **Relay → `tandem sub`: lossy.** The haiku relay dropped the header
  line from the heredoc it echoed in 5 of those 6 dispatches. The relay
  prompt demanded the task be copied "byte-for-byte", but never mentioned
  the header — and `tandem-model: sol` reads exactly like envelope
  metadata addressed to the relay layer, so haiku treated it as already
  consumed and started "the task" at line 2.
- **`tandem sub` → codex: deterministic.** When the header arrives, it
  resolves and pins correctly; when it does not arrive, nothing errs —
  the brief is still a valid brief, so the worker silently runs the
  config default. This silence is the whole severity of the bug: "runs
  on luna" stays true even when you asked for sol, and only the missing
  trailer hints at it.

First layer of the fix: the relay bodies now name the header explicitly
("that line is part of the task message … copy it exactly like every
other line"). Live re-test (nine probes pinned to terra): in-band
retention rose from ~1/6 to 5/7. Better, but a prompt can only make a
small model *likelier* to copy a line; ~2/7 still arrived headerless.
This spec is the second layer: make the pin survive even when the copy
fails.

## The shape of the problem

The pin must travel parent → relay → `tandem sub`, and the middle hop is
the unreliable one. Any scheme that keeps the pin *inside* the brief — a
first line, a trailing line, a `-m` flag the relay is told to write —
still depends on haiku reproducing it. The only two channels that do not
pass through haiku at all:

- the **parent prompt → hook**: `tandem hook-route` already runs on every
  Agent/Task PreToolUse and receives `tool_input.prompt` pristine;
- the **filesystem → `tandem sub`**: both processes share `$TANDEM_HOME`.

The sandbox-consent stamp already rides exactly this road, and for the
stated reason ("it cannot ride the dispatch itself, since the relay's
only channel to the worker is the untrusted brief"). The pin follows it,
with one difference: consent is a property of the *session*, so a single
last-write-wins stamp file suffices; a pin is a property of *one
dispatch*, and parallel dispatches with different pins are routine
(mix-and-match is manual mode's selling point). The pin therefore needs
a per-dispatch key, and the brief body is the only identifier both ends
can see.

## Design

Two halves plus a small module (`pinstash.py`), all best-effort.

**Capture (hook side).** `hookroute.relay_pin(payload)` returns
`(requested, body)` when the dispatch both targets a relay
(`gpt`/`codex-worker`, scope-insensitive like the loop guard) and opens
with a well-formed `tandem-model:` header; `None` otherwise. The CLI
then writes the stash entry:

    ~/.tandem/model-pins/<sha256(body.strip())>   ← file name: body fingerprint
    contents: sol                                 ← the requested name, unresolved

Relay-only on purpose: a native agent's brief never reaches `tandem sub`
under manual routing, and the `route="all"` rewrite prepends agent
instructions, so a body captured pre-rewrite would not match what that
relay echoes. Malformed headers are not stashed — if the relay preserves
one, `sub` fails loudly on its own, and the stash must not launder a name
the header parser rejects. The name is stored *unresolved* so that
resolution (and its loud unknown-model failure listing real slugs)
happens in `tandem sub`, identically to the in-band path.

**Recovery (sub side).** When `tandem sub`'s stdin arrives with no
header and no `-m` flag was given, it fingerprints its (stripped) task
text and consults the stash. A hit rejoins the normal header path —
catalog resolution, `worker model:` announcement, `[tandem-sub model: …]`
trailer — so a recovered pin is indistinguishable from a preserved one.
A miss changes nothing. Precedence, strongest first:

    -m flag  >  in-band header  >  stashed pin  >  config default

`-m` callers skip the lookup entirely: they never rode the header
protocol, and a stale pin must not be able to abort their dispatch.

**Stash contract** (`pinstash.py`):

- **Exact match only.** The key is `sha256(body.strip())`; both writers
  normalize edge whitespace and nothing else. No fuzzy matching: if the
  relay ever mangles the body too (never observed — the drop is always
  exactly the header line), the hash misses and the dispatch degrades to
  today's behavior. Fail-open, never fail-worse.
- **Entries expire, TTL one hour.** Long enough for a slow codex run
  plus the relay's one sanctioned retry; short enough that an identical
  brief re-dispatched much later does not inherit a stale pin. Expired
  entries are pruned opportunistically on each stash.
- **Lookups do not consume.** The sanctioned `--sandbox workspace-write`
  rerun re-sends the same brief and must re-match the same pin.
- **Every failure is silent.** A stash write error never reaches the
  dispatch (mirror of `_stamp_sandbox`); a corrupt or implausible entry
  reads as "no pin" — a recovered name is re-validated through the
  header parser before it may reach `resolve()`, because aborting a
  dispatch over stash bookkeeping would be worse than the silent default
  the stash exists to improve on.

## Rejected alternatives

- **Prompt fix alone.** Shipped as layer one, measurably insufficient
  (5/7). A prompt can raise a small model's copy fidelity, not guarantee
  it.
- **Single per-session stamp file** (the sandbox pattern verbatim).
  ~15 lines, but last-write-wins misroutes under parallelism: a
  headerless default-model dispatch running alongside a pinned one would
  consume the neighbor's pin — a *new* silent-wrong-model failure in
  exactly the mix-and-match scenario manual mode exists for.
- **Teach the relay to pass `-m`.** Relocates the same fidelity
  dependence from a heredoc line to a flag; haiku can drop either.
- **Fuzzy body matching.** A near-match heuristic converts "no pin" into
  "possibly the wrong pin". The failure mode being eliminated is silent
  wrong-model; a design that can reintroduce it under fuzz is worse than
  one that occasionally falls back to the default.

## Observability

Unchanged and sufficient: a pinned dispatch — in-band or recovered —
ends with the `[tandem-sub model: …]` trailer naming what actually ran,
and the non-quiet path announces `worker model:` on stderr. Known relay
infidelity, out of scope here: the relay sometimes trims the trailer
from its final reply (observed 2/5 in live probes), so a *missing*
trailer does not prove the pin failed; the worker log under
`~/.tandem/subagents/…/logs/` is the ground truth.
