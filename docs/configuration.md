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
session pairs the listed harnesses that are actually installed
and usable — not-installed ones are skipped silently, ones that are
installed but unusable warn and drop out, and fewer than two usable is
an error that prints the install command for each missing CLI. The
order sets both the **Ctrl-]** cycle and the default starting harness
(`tandem native` enters the first usable one and `--active` overrides
that per launch; a fresh chat session starts on it unless `--on` names
another).
Leave a harness off the list to keep it out of new sessions even though
it's installed — `harnesses = ["claude", "codex"]` gives two-way
sessions on a machine that also has opencode. Unknown names are
dropped, duplicates deduped, and anything else malformed falls back to
all three. Sessions already paired keep their own participant list.

## skip_permissions — no permission prompts in claude and codex

A top-level switch, off by default. When on, every claude and codex
session tandem opens runs without its harness's permission prompts — in
the chat window and in `tandem native` alike:

```toml
skip_permissions = true
```

| | claude | codex |
|---|---|---|
| `tandem native` and each flip | `--dangerously-skip-permissions` | `--dangerously-bypass-approvals-and-sandbox` |
| chat window | `--permission-mode bypassPermissions` | approval policy `never`, sandbox `danger-full-access` |

This removes the harnesses' own safety rails: commands run and files
change without asking, and codex runs unsandboxed. Set it only if that
is what you want. In the chat window the bar marks each slot it applies
to with `skip-perms` (once, after the slots, when that is every slot) and
`/status` reads `permissions skipped` while it is on, claude's questions to you (`AskUserQuestion`) still appear, and an
explicit `[chat] codex_approval_policy` / `codex_sandbox` still wins over
the switch for codex. opencode is
untouched — it has no such flag, and its permissions live in its own
`opencode.json`. One-off relays (`tandem run`), subagent dispatch, and
doctor probes are unaffected.

Only a literal `true` turns it on; any other value (`"true"`, `1`) leaves
the prompts in place.

For a single launch, pass `--skip-permissions` instead — or
`--no-skip-permissions` to keep the prompts for one launch while the
config says `true`. The flag wins over the config, is not saved with the
session (a later `resume` needs it again), and is accepted wherever
tandem opens a session:

```sh
tandem --skip-permissions
tandem resume <id> --skip-permissions
tandem native --skip-permissions
tandem native resume --skip-permissions
```

Inside a chat window, `/skip-permissions` flips it for that window —
`/skip-permissions on` / `off` to say which way. It takes effect from the
next turn (a turn already running keeps the mode it started under) and,
like the flag, is never written to the config or saved with the session.
`tandem native` has no such switch: there the harness was launched with
its flag, and only a relaunch changes it.

It is a usage error on every other command (`tandem run`, `tandem sub`,
`tandem doctor`, …), which never bypass permissions either way.

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
opens (`tandem native`, `tandem native resume`, and each flip) — one-off relays
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
prompts for the sessions `tandem native` launches — set them only if
that is what you want. `args` does not reach the chat window; the
top-level [`skip_permissions`](#skip_permissions--no-permission-prompts-in-claude-and-codex)
switch covers both.
The list is passed to the harness raw: a flag that expects a value can
swallow the settings tandem appends after it and break turn tracking.
Malformed values (a non-list, empty or non-string elements) are
silently ignored rather than failing the launch.

## [frame] — the flip key, the tab bar, and warm flips

The frame is tandem's own surface inside a running session: one reserved
keybind that flips to the next harness in the cycle, the one-line tab
bar on the bottom terminal row (participants, flip key, the active
model's live token stats, and each account's rate-limit windows), and
the pipelined boot behind the flip.

```toml
[frame]
flip_key = "ctrl-]"
bar = true
warm = true          # boot the incoming harness while the outgoing one shuts down
rate_limits = true   # poll each account's usage windows for the bar
```

| key | default | meaning |
| --- | --- | --- |
| `flip_key` | `"ctrl-]"` | The flip keybind, consumed by tandem (never forwarded). Accepts `ctrl-<char>` or a hex byte like `"0x1d"`; printable keys are rejected (they would swallow typing). The bar relabels itself to match (`ctrl-t` shows `^T flips`). |
| `bar` | `true` | The one-line tab bar on the bottom terminal row, including the active slot's token stats. `false` hides it; the flip still works. |
| `warm` | `true` | Overlap the two halves of a flip: the incoming harness starts booting the moment the flip fires (a mid-turn press waits for the turn boundary first), while the outgoing one is still shutting down. `false` gives fully serial flips — the boot only begins once the old harness is gone. Flips *into* opencode are always serial (its TUI must open after the last turn has landed in its database). |
| `rate_limits` | `true` | Show each participant's account rate limits on its slot (`5h 4% 7d 41%` — percent *used* per window). Polled every 60 s and after each response from the same account endpoints `claude`'s `/usage` and `codex`'s `/status` call, with the credentials those CLIs already keep; these are tandem's only outbound network calls. `false` makes none of them; the figures also stay blank for API-key logins or when a fetch fails. |

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

## [chat] — the unified window

Bare `tandem` starts a fresh chat. `tandem resume [id]` reopens one by ID,
or shows a picker across all directories when no ID is given.
`tandem --continue` reopens the most recently used session across directories.
Resuming restores the conversation and model pins in the session's saved
working directory.

The chat window runs every
prompt headless inside the harness that ran the last one, unless the prompt starts with `/claude`, `/codex`, or
`/opencode` (optionally `/codex:gpt-5.5` to pin a model for that harness,
`/codex:default` to clear it). A bare route switches the default without a
turn. Tandem's own window commands are `/quit` (leave the window, as two
Ctrl-Cs do), `/status` (print the session id, the default harness, the
participants and any model pins) and `/skip-permissions [on|off]` (turn
claude's and codex's permission prompts off or on from the next turn —
see [`skip_permissions`](#skip_permissions--no-permission-prompts-in-claude-and-codex)); every other leading `/word` goes to the
current harness as its own slash command.

Routes are only ever spelled with `/`: `@` belongs to file mentions, so
`@codex:astra` is sent as ordinary text. Model names after the `:` may be
shorthand. Codex shorthand is matched against its local model
catalog: `/codex:astra` selects `gpt-6-astra` when that is the unique match.
Unknown or ambiguous names are rejected with available model names.
If the catalog is unavailable, the name passes through verbatim; use a full
model ID in that case. Claude names go by family: `fable`, `opus`, `sonnet` or
`haiku` alone is claude's own alias for the latest model of that family,
`fable-5-1` or `fable-5.1` becomes `claude-fable-5-1`, a name that already
contains `claude` passes through as written, and anything else is rejected.
Opencode models are spelled `provider/model`. No model selector preserves
the saved pin, and `/codex:default` clears it.

Typing `@` opens a file picker under the draft. It lists the session
directory's files and directories (git's tracked and untracked-but-not-ignored
files in a repository, a walk that skips dot-directories elsewhere) and
narrows as you type. Up/Down choose, Tab or Enter completes the path, Esc
closes the list; choosing a directory keeps picking inside it. A path with a
space is written `@"my notes.txt"`. The mention is sent as written, and each
harness takes it the way its own TUI would have: claude expands it, opencode
gets the file attached to the message (for a path inside the session
directory), codex reads it with a tool.

```toml
[chat]
tool_output_lines = 8        # lines of tool output shown per call (rest elided)
history_turns = 50           # turns painted from the transcript at startup
show_thinking = false        # reasoning summaries, dimmed
bell = true                  # ring when a turn needs an answer, or ends after 15 s or more
claude_setting_sources = ["user", "project", "local"]   # what headless claude loads
# codex_approval_policy = "on-request"   # default: inherit ~/.codex/config.toml
# codex_sandbox = "workspace-write"      # default: inherit
```

Keys: Enter sends; Esc interrupts the running turn; Ctrl-C once interrupts,
twice within two seconds quits; Ctrl-L repaints. An approval prompt takes
`y`, `a` (allow for the rest of the session), or `n`; a question takes its
option number or typed text.

The rule above the status bar is the activity line. While a turn runs it
names the harness, what it is doing (`starting`, `thinking`, `running
Bash`, `writing`), and for how long; when the turn needs an approval or an
answer it turns bold and says so; idle, it is a plain rule. Every turn ends
with a closing row — `✓ done`, `■ interrupted` or `✗ failed` — with its
length. With `bell = true` the terminal bell rings when a turn needs an
answer and when one that ran 15 s or more ends, so a window left in the
background can call you back.

Headless claude and codex app-server skip the folder-trust prompts their
TUIs show; opencode's default config auto-allows `bash` and only asks when
your opencode config says so. The bar's rate-limit polling follows
`[frame] rate_limits`.

## Environment variables

Not config keys, but honored everywhere: `TANDEM_HOME` relocates
tandem's own state (`state.db`, config, quarantine, subagent logs) from
`~/.tandem`; the harnesses' own overrides — `CLAUDE_CONFIG_DIR`,
`CODEX_HOME`, `OPENCODE_DB` — are respected when tandem looks for their
session stores.

`TANDEM_SESSION_ID` is set by tandem, not by you: the chat window exports
it to every harness it runs, so a `tandem sub` or `tandem hook-route`
spawned from inside that harness acts on the window's own session rather
than the directory's most recent one (a directory can hold many). Commands
run from a plain shell never see it and keep resolving by directory.
