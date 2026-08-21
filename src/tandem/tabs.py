"""The tab cycle: participants in order, then the mixed tab, then around.

The mixed tab is a view + routing mode over the same harness processes, so
a press has two possible costs and this module's whole job is telling them
apart: a *bar* move changes only tandem's own state (tab flag, frame file,
bar repaint — instant, the harness keeps running), while a *flip* move
means the shown harness really changes (terminate + relaunch through the
existing flip machinery). Entering the mixed tab from the harness whose
focus it already holds is a bar move; every other transition that changes
the shown harness is a flip.

Threading: press() runs on the pty pump thread inside the stdin branch —
it must stay allocation-light, lock-free and I/O-free (the
FlipMonitor.flip_pressed contract). routed() runs on the mixer thread;
pending_target()/settle()/cancelled() run on the flip loop between runs.
All state is plain attributes: the GIL makes each assignment atomic, and
no reader needs a consistent multi-field view (the pump only paints
snapshots, and a paint one tick stale is invisible).

Cancel semantics are preserved from the pre-mixed frame: a press while a
flip is pending cancels it (returns a "cancel" move; the caller toggles
the monitor off). Multi-tab skipping is deliberately not a thing.
"""

from __future__ import annotations

from dataclasses import dataclass

MIXED = "mixed"


@dataclass(frozen=True)
class TabMove:
    kind: str            # "bar" | "flip" | "cancel"
    tab: str             # destination tab ("harness" | "mixed")
    focus: str           # mixed focus after the move
    target: str = ""     # harness to launch (kind == "flip" only)


class TabState:
    def __init__(self, participants: list[str], tab: str = "harness",
                 focus: str = ""):
        self.participants = list(participants)
        self.tab = tab if tab in ("harness", MIXED) else "harness"
        self.focus = focus if focus in self.participants else ""
        self.pending: TabMove | None = None
        self.version = 0

    def press(self, active: str) -> TabMove:
        if self.pending is not None:
            move = TabMove("cancel", self.tab, self.focus)
            self.pending = None
            self.version += 1
            return move
        if self.tab == MIXED:
            first = self.participants[0]
            if self.focus == first:
                self.tab = "harness"
                self.version += 1
                return TabMove("bar", "harness", self.focus)
            move = TabMove("flip", "harness", self.focus, target=first)
        else:
            try:
                idx = self.participants.index(active)
            except ValueError:
                idx = 0
            if idx < len(self.participants) - 1:
                move = TabMove("flip", "harness", self.focus,
                               target=self.participants[idx + 1])
            else:
                focus = self.focus or active
                if focus == active:
                    self.tab, self.focus = MIXED, focus
                    self.version += 1
                    return TabMove("bar", MIXED, focus)
                move = TabMove("flip", MIXED, focus, target=focus)
        self.pending = move
        self.version += 1
        return move

    def routed(self, target: str) -> None:
        self.pending = TabMove("flip", MIXED, target, target=target)
        self.version += 1

    def pending_target(self) -> str:
        p = self.pending
        return p.target if p is not None else ""

    def settle(self, new_active: str) -> None:
        p, self.pending = self.pending, None
        if p is not None:
            self.tab = p.tab
        if self.tab == MIXED:
            self.focus = new_active
        self.version += 1

    def cancelled(self) -> None:
        if self.pending is not None:
            self.pending = None
            self.version += 1

    def snapshot(self, active: str, routing_ok: bool = True) -> dict:
        return {
            "tab": self.tab,
            "focus": active if self.tab == MIXED else "",
            "routing_ok": routing_ok,
        }
