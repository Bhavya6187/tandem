"""What a leading `/` can be: tandem's own window commands, a route per
participant, and the default harness's own commands. One list, in that
order, for the composer's picker and for `/help`. Pure — the runtimes hand
in the harness lists; nothing here reads a file or a socket."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class Command:
    name: str          # without the slash
    description: str
    origin: str        # "tandem" | "route" | a harness name


WINDOW: tuple[Command, ...] = (
    Command("help", "list what a leading / can be", "tandem"),
    Command("status", "session id, default harness, participants, model pins", "tandem"),
    Command("compact", "compact the default harness's conversation", "tandem"),
    Command("model", "list the default harness's models; /model NAME pins one", "tandem"),
    Command("skip-permissions", "[on|off] skip claude's and codex's permission prompts", "tandem"),
    Command("note", "[dismiss|good|bad] the navigator's pending note", "tandem"),
    Command("quit", "leave the window (two Ctrl-Cs do the same)", "tandem"),
)

_ROUTE_DESCRIPTION = "run the prompt here and make it the default; :model pins"


def catalog(participants: Sequence[str], default: str,
            harness_commands: dict[str, list[Command]]) -> list[Command]:
    """Window commands, then a route per participant, then the default
    harness's own commands. A harness command named like a window command
    is dropped: the window's wins, and one row per name keeps Enter
    unambiguous."""
    out = list(WINDOW)
    out += [Command(h, _ROUTE_DESCRIPTION, "route") for h in participants]
    taken = {c.name for c in out}
    out += [c for c in harness_commands.get(default, []) if c.name not in taken]
    return out


def help_lines(cmds: list[Command]) -> list[str]:
    """`/help`: one row per command under a heading per origin."""
    width = max((len(c.name) for c in cmds), default=0) + 1
    lines: list[str] = []
    for origin in dict.fromkeys(c.origin for c in cmds):
        lines.append("tandem:" if origin == "tandem" else "routes:" if origin == "route" else f"{origin}:")
        lines += [f"  /{c.name}".ljust(width + 3) + " " + c.description
                  for c in cmds if c.origin == origin]
    return lines
