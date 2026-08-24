# Configuration

tandem reads one optional file: `~/.tandem/config.toml`. No file is
required; every key has a working default, and a malformed value falls
back to it rather than failing a launch. (Back to the
[README](../README.md).)

## harnesses — who participates, and in what order

A top-level list naming the harnesses a fresh session may include, in
flip-cycle order. Default: all three, claude first.

```toml
harnesses = ["claude", "codex", "opencode"]
```

Naming a harness here is an *intent*, not a requirement: a fresh
`tandem` session pairs the listed harnesses that are actually installed
and usable — not-installed ones are skipped silently, ones that are
installed but unusable warn and drop out, and fewer than two usable is
an error that prints the install command for each missing CLI. The
order sets both the **Ctrl-]** cycle and the default starting harness
(`tandem` enters the first usable one; `--active` overrides that per
launch).
Leave a harness off the list to keep it out of new sessions even though
it's installed — `harnesses = ["claude", "codex"]` gives two-way
sessions on a machine that also has opencode. Unknown names are
dropped, duplicates deduped, and anything else malformed falls back to
all three. Sessions already paired keep their own participant list.

## [subagents] — GPT subagent workers

Worker model, routing mode, and context handling for GPT subagent
dispatches. Key semantics and the full routing story live in the
[GPT subagents guide](subagents.md):

```toml
[subagents]
model = "gpt-5.6-luna"  # worker default; unset = your codex account's default
route = "manual"        # manual | all | off
context = "match"       # match | task | full
keep_forks = false      # keep each worker's rollout for debugging
```

## [claude] / [codex] / [opencode] — per-harness startup args

Optional per-harness tables add flags to every interactive session tandem
opens (`tandem`, `tandem resume`, and each flip) — one-off relays
(`tandem run`), subagent dispatch, and doctor probes are unaffected:

```toml
[claude]
args = ["--dangerously-skip-permissions"]

[codex]
args = ["--dangerously-bypass-approvals-and-sandbox"]

[opencode]
args = []   # same mechanism; opencode's own flags go here
```

The claude/codex flags shown disable the harnesses' own permission
prompts for sessions tandem launches — set them only if that is what you
want.
The list is passed to the harness raw: a flag that expects a value can
swallow the settings tandem appends after it and break turn tracking.
Malformed values (a non-list, empty or non-string elements) are
silently ignored rather than failing the launch.

## [frame] — the flip key, the tab bar, warm flips, and the mixed tab

The frame is tandem's own surface inside a running session: one reserved
keybind that flips to the next harness in the cycle, the one-line tab
bar on the bottom terminal row (participants, flip key, the active
model's live token stats, and each account's rate-limit windows), the
pipelined boot behind the flip, and the mixed tab the cycle ends on.

```toml
[frame]
flip_key = "ctrl-]"
bar = true
warm = true          # boot the incoming harness while the outgoing one shuts down
rate_limits = true   # poll each account's usage windows for the bar
mixed = true         # the mixed tab joins the flip cycle
```

| key | default | meaning |
| --- | --- | --- |
| `flip_key` | `"ctrl-]"` | The flip keybind, consumed by tandem (never forwarded). Accepts `ctrl-<char>` or a hex byte like `"0x1d"`; printable keys are rejected (they would swallow typing). The bar relabels itself to match (`ctrl-t` shows `^T flips`). |
| `bar` | `true` | The one-line tab bar on the bottom terminal row, including the active slot's token stats. `false` hides it; the flip still works. |
| `warm` | `true` | Overlap the two halves of a flip: the incoming harness starts booting the moment the flip fires (a mid-turn press waits for the turn boundary first), while the outgoing one is still shutting down. `false` gives fully serial flips — the boot only begins once the old harness is gone. Flips *into* opencode are always serial (its TUI must open after the last turn has landed in its database). |
| `rate_limits` | `true` | Show each participant's account rate limits on its slot (`5h 4% 7d 41%` — percent *used* per window). Polled every 60 s and after each response from the same account endpoints `claude`'s `/usage` and `codex`'s `/status` call, with the credentials those CLIs already keep; these are tandem's only outbound network calls. `false` makes none of them; the figures also stay blank for API-key logins or when a fetch fails. |
| `mixed` | `true` | The mixed tab — the stop the flip cycle adds after the last harness, where a prompt starting `@codex` or `@codex:gpt-5.6-luna` runs that one turn in the named harness (the [README](../README.md#the-mixed-tab) has the grammar). `false` leaves the cycle holding your harnesses only, and an `@` prefix is then ordinary prompt text everywhere. |

The mixed tab is a view over the harnesses tandem already runs, not a
fourth session: it shows one harness's native UI, and the turns you
prefix are dispatched to another. Moving in or out of it is instant when
the harness on screen stays the same — only a move that changes which
harness runs pays a flip. Routing *from* claude or codex also needs
tandem's plugin registered there (`tandem plugin install`). A focus that
cannot intercept prompts — opencode always, claude or codex without the
plugin — runs every prompt natively, `@codex` prefix included, as literal
text; the bar's `(no @-routing)` is the warning that this is what will
happen, not a guard against it. `tandem doctor` names which harnesses are
set up.

An unparseable value falls back to the default rather than failing the
launch. If a terminal can't sustain the bar, tandem drops it for the rest
of that session — the flip is unaffected — and `tandem doctor` warns
about it until you delete the marker file it names; set `bar = false` if
you'd rather keep the bar off for good. Shrinking the window below the
bar's row floor also drops it for the session, but that is tandem's own
policy rather than a conflict, so nothing is recorded and `doctor` stays
quiet.

Warming is pipelining, not a background service: the moment the flip
fires — a mid-turn `Ctrl-]` waits for the turn boundary first — the
incoming harness starts and the outgoing one is torn down at the same
time, so the flip lands at about the incoming harness's own start-up
speed instead of that plus the shutdown. Nothing exists before you press
the key and nothing survives the flip — between flips a tandem session is
exactly one harness process. `warm = false` restores the fully serial
flip, which is slower but does the same thing.

## Environment variables

Not config keys, but honored everywhere: `TANDEM_HOME` relocates
tandem's own state (`state.db`, config, quarantine, subagent logs) from
`~/.tandem`; the harnesses' own overrides — `CLAUDE_CONFIG_DIR`,
`CODEX_HOME`, `OPENCODE_DB` — are respected when tandem looks for their
session stores.
