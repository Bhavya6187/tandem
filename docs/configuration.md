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
