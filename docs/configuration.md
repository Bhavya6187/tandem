# Configuration

tandem reads one optional file: `~/.tandem/config.toml`. No file is
required; every key has a working default. (Back to the
[README](../README.md).)

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

## [claude] / [codex] — per-harness startup args

Optional per-harness tables add flags to every interactive session tandem
opens (`tandem`, `tandem resume`) — one-off relays (`tandem run`),
subagent dispatch, and doctor probes are unaffected:

```toml
[claude]
args = ["--dangerously-skip-permissions"]

[codex]
args = ["--dangerously-bypass-approvals-and-sandbox"]
```

The flags shown disable the harnesses' own permission prompts for
sessions tandem launches — set them only if that is what you want.
The list is passed to the harness raw: a flag that expects a value can
swallow the settings tandem appends after it and break turn tracking.
Malformed values (a non-list, empty or non-string elements) are
silently ignored rather than failing the launch.

## [frame] — the flip key, the tab bar, and warm flips

The frame is tandem's own surface inside a running session: one reserved
keybind that flips to the other harness, the one-line tab bar on the
bottom terminal row, and the pipelined boot behind the flip.

```toml
[frame]
flip_key = "ctrl-]"
bar = true
warm = true   # boot the incoming harness while the outgoing one shuts down
```

| key | default | meaning |
| --- | --- | --- |
| `flip_key` | `"ctrl-]"` | The flip keybind, consumed by tandem (never forwarded). Accepts `ctrl-<char>` or a hex byte like `"0x1d"`; printable keys are rejected (they would swallow typing). The bar relabels itself to match (`ctrl-t` shows `^T flips`). |
| `bar` | `true` | The one-line tab bar on the bottom terminal row. `false` hides it; the flip still works. |
| `warm` | `true` | Overlap the two halves of a flip: the incoming harness starts booting the moment you press the key, while the outgoing one is still shutting down. `false` gives fully serial flips — the boot only begins once the old harness is gone. |

An unparseable value falls back to the default rather than failing the
launch. If a terminal can't sustain the bar, tandem drops it for the rest
of that session — the flip is unaffected — and `tandem doctor` warns
about it until you delete the marker file it names; set `bar = false` if
you'd rather keep the bar off for good. Shrinking the window below the
bar's row floor also drops it for the session, but that is tandem's own
policy rather than a conflict, so nothing is recorded and `doctor` stays
quiet.

Warming is pipelining, not a background service: pressing `Ctrl-]` starts
the incoming harness immediately and tears the outgoing one down at the
same time, so the flip lands at about the incoming harness's own start-up
speed instead of that plus the shutdown. Nothing exists before you press
the key and nothing survives the flip — between flips a tandem session is
exactly one harness process. `warm = false` restores the fully serial
flip, which is slower but does the same thing.
