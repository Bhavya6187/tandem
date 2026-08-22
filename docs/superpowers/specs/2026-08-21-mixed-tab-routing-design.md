# Mixed tab with manual @-routing — design

Date: 2026-08-21
Status: approved in brainstorming; v1 scope

## Problem and goal

Tandem keeps one harness live and the others resume-ready, with Ctrl-] to
flip. The next step toward a complete meta-harness is routing *within* one
session: each turn goes to the harness and model best suited to it, without
the user copying context or leaving the session.

The long-term goal (v2) is best-harness-per-task routing decided by a small
model. This spec covers v1: a dedicated **mixed tab** where the user routes
each turn manually with an `@target` prefix. v1 builds the entire dispatch
pipeline (intercept → stash → block → flip → inject); v2 later replaces only
the decision function. Nothing in v1 is throwaway.

## Decisions made in brainstorming

- Primary job of the mixed tab: **best harness per task** (not capacity
  arbitrage, ensemble, or orchestrator/workers — though rate-limit headroom
  becomes a v2 tiebreak input).
- Route depth: **harness + model** (e.g. codex on a specific model, claude
  on haiku), resolved through modelcat.
- v1 route picker: **manual `@` prefix**; v2 picker: model-based (Haiku-class
  call) behind the same seam, with the prefix surviving as manual override.
- Surface: **dedicated mixed tab now** (4th tab). The three harness tabs keep
  today's behavior exactly; `@` means nothing outside the mixed tab.
- No third-party router. API-level routers (LiteLLM class) bypass the
  harnesses and lose their tools, subscriptions, and agentic loops. The
  router is a small in-frame module.

## v1 scope

A fourth tab, `mixed`, alongside claude/codex/opencode. Inside it:

- A prompt starting with a recognized `@target` routes to that harness
  (optionally pinning a model).
- An unprefixed prompt stays with the **focus harness** — the harness that
  handled the last mixed-tab turn.

All four tabs share the same underlying harness sessions and sync layer;
mixed is a view + routing mode, not a fourth session.

### Non-goals (v1)

- The model-based auto-router (v2; this spec fixes its seam only).
- Ensemble/fan-out turns, mid-turn delegation (`tandem sub` expansion).
- Any tandem-owned composer UI; typing always happens in a native harness.

## Mixed tab semantics

- **View.** The mixed tab shows the native UI of the current focus harness.
  A routed turn flips the visible PTY underneath the user; they stay "in"
  the mixed tab while the view follows the work. Flips reuse the existing
  machinery (drain, sync, cursor fast-forward, fire-at-flip warm
  pipelining) unchanged.
- **Bar.** The mixed slot shows its focus, e.g.
  `claude ○ │ codex ○ │ opencode ○ │ mixed ● codex·gpt-5.3`. After a routed
  turn the slot annotation is the resolved target; in v2 the same position
  carries the router's one-line reason.
- **Ctrl-].** Mixed joins the cycle as the fourth stop
  (claude → codex → opencode → mixed).
- **Focus lifecycle.** On the first-ever entry the focus defaults to the
  harness the user came from when routing is available from it; otherwise to
  the first participant it is available from (a flip). Entry only ever
  happens from the last participant and the focus only ever moves on a
  routed turn, so adopting a harness with no prompt hook would strand the
  tab with routing permanently off — with the stock cycle, always (opencode
  is last and hookless). The same substitution repairs a saved focus whose
  plugin has since been removed; with nothing routable at all the focus is
  adopted as-is and the bar's `(no @-routing)` hint stands. Thereafter the
  mixed tab keeps its own focus across visits (leaving and re-entering does
  not reset it). Focus is saved in session state, and `tandem resume`
  restores it.

## Route grammar

Recognized only in the mixed tab, and only as the **first token** of the
prompt:

- `@claude` / `@codex` / `@opencode` — pick a harness, keep its current
  model.
- `@<model>` (e.g. `@gpt-5.3-codex`, `@haiku`) — resolve through modelcat to
  (harness, exact model).
- `@harness:model` — explicit combination, for disambiguation.

Any unrecognized `@token` is literal prompt text and passes through
untouched — `@src/foo.py …` still works as a claude file mention inside the
mixed tab.

**Amended during implementation (model pinning).** This spec planned to
inject the target harness's own in-session `/model` command ahead of the
prompt. The shipped mechanism is stronger and needs no per-harness command
syntax: a routed flip relaunches the target anyway, so the pin is applied as
**launch argv** (`adapter.model_argv(model)`, appended after the user's
`[harness] args` so an explicit per-turn pin outranks a static config entry)
and the launch recipe records what was actually launched rather than what
was asked for. Two consequences the `/model` design did not have. A harness
with no launch-time model flag cannot be pinned at all — `recipe.model`
comes back empty and the run says so on exit instead of silently answering
on the wrong model — and the pin is not per-turn: it lasts for that harness
process, i.e. until the next relaunch. Verification item 3 (in-session
`/model` syntax per harness) is therefore moot for v1.

## Dispatch pipeline

For one routed turn, focus harness X, target Y:

1. **Intercept.** The user submits a prompt in the mixed tab. X's
   `UserPromptSubmit` hook (tandem's plugin already registers hooks in
   claude; codex ≥0.145 loads the same claude-format plugin, live-validated
   in the vibeshub spike) reads the frame's state file first: if the active
   tab is not `mixed`, exit allow immediately — harness tabs never pay
   routing overhead. In the mixed tab, parse the first token.
2. **Stay is free.** No recognized prefix, or target resolves to X → exit
   allow; the turn runs natively and nothing else happens.
3. **Route.** Target Y ≠ X → the hook writes a route request
   `{target, model, prompt_body}` into the session state dir
   (pinstash-style, so the prompt is durably stashed before anything else
   moves) and blocks the local turn.
4. **Flip.** The frame picks up the route request and runs the existing flip
   machinery X→Y — drain X, sync shadows, fast-forward Y's cursor
   (echo-suppression invariant), fire-at-flip warm pipelining. Same code
   path as Ctrl-]; routing adds no new flip logic.
5. **Inject.** Once Y is live: write the prompt body + Enter to Y's pty. Y
   runs the turn natively; the existing sync layer propagates it to the
   shadows. Focus becomes Y. (A specified model is not injected here — it
   rode step 4's relaunch as argv; see the grammar section's amendment.)

New code is limited to: the hook's prefix branch, the route-request file
format, and the frame's route-request → flip + inject state machine.

**Protocol v2 (post-review simplification).** The route request is
immutable and carries an `id`; the *filename* is its state, not a field.
The hook writes `<id>-route.json` (pending); the frame claims it with one
atomic rename to `<id>-route.claimed.json`; delivery releases it by id.
There is no TTL — the next frame start sweeps both slots and reports what
they held at any age, since an expiring read was the one path that could
delete a typed prompt silently. The id is also how the hook proves its own
stash landed (a leftover with the same text cannot vouch for it), and how
delivery avoids releasing a second prompt that arrived mid-paste. On the
frame side the whole lifecycle lives in one object, `routing.RouteCoordinator`.

## Router seam (built in v1, exercised by v2)

```
route(prompt: str, ctx: RouterContext) -> RouteDecision | Stay

RouteDecision = {harness, model | None, reason: str}
```

v1 implements `route` as the prefix parser (`reason` = the prefix itself).
v2 adds a model-based router consulted for unprefixed prompts, whose
`RouterContext` carries a roster card per harness: model identity and
strengths (modelcat), rate-limit headroom per window (ratelimit), current
context size, and capability notes (tools/MCP, sandbox posture), plus a
rolling conversation summary and a stickiness signal biased toward staying
put. All roster fields are data tandem already collects for the bar. The
dispatch pipeline is identical for both implementations.

## Failure handling

- **Prompt is never lost.** The stash happens before the block. If the flip
  fails (target CLI missing, crashed, warm child dead), the frame surfaces
  the error on the bar, focus stays on X, and the stashed prompt is offered
  for retry — same backstop pattern as manual-mode pinstash.
- **Blocked-prompt residue.** If a harness writes the blocked prompt into
  its transcript as a user message, the converter filters route-tagged
  blocked entries so the turn appears in exactly one transcript (the
  target's). Marker-based filtering already exists for interrupts.
- **Hook-registration trap.** Hooks register at harness session start only.
  Entering the mixed tab when the focus harness has no registered hook shows
  a bar warning instead of silently eating `@` prefixes; `tandem doctor`
  gets a check.
- **Opencode as a route source.** Opencode as a *target* works
  unconditionally (injection needs no hook). Routing *from* opencode
  requires it to run the hook — unverified. If unsupported, v1 ships with a
  bar hint ("routing unavailable from opencode — Ctrl-] out") rather than
  blocking the feature.
- **Model switch unsupported.** Where a harness cannot switch models
  mid-session, a model-pinned route to it degrades to harness-only with a
  bar notice.

## Verification items (live gate prerequisites)

1. Blocked-prompt residue: does a `UserPromptSubmit` block in claude / codex
   leave a user-message entry in the transcript? Determines whether the
   converter filter is needed.
2. Opencode `UserPromptSubmit` hook support (source-side routing).
3. In-session `/model` command per harness (claude, codex, opencode) and its
   exact syntax for injection.

## Testing

- **Unit:** prefix parser (grammar, modelcat resolution, unknown-token
  passthrough), route-request round-trip, frame dispatch state machine —
  following existing `test_flip` / `test_frame` / `test_pinstash` patterns.
- **Golden fixtures:** hook block decisions per harness.
- **Live gate (tmux recipe, established pattern):** claude→`@codex`→
  `@opencode` relay inside the mixed tab; a model-pinned route; unknown-`@`
  passthrough; flip-failure retry; blocked-prompt residue check;
  `tandem resume` restoring mixed-tab focus.
