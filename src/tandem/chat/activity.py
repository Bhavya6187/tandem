"""What the running turn is doing, for the activity line above the bar.

The conversation is append-only and a model can think for a minute without
printing a row, so the region alone cannot say whether a turn is running,
stuck on an approval, or over. This is the state the window keeps for that:
fed the same live events the screen paints, read back as one line of text
on every repaint. Idle is the empty string — the row is a plain rule again."""

from __future__ import annotations

import time
from typing import Callable

from .events import (ApprovalRequest, Idle, LiveEvent, QuestionRequest, TextDelta,
                     ThinkingDelta, ToolFinished, ToolStarted, TurnFinished, TurnStarted)

# one cell wide each, like every glyph that shares a row with the bar
_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
_FRAME_SECONDS = 0.1
_HINT = "esc interrupts"


def elapsed_text(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60:02d}s"
    return f"{s // 3600}h {s % 3600 // 60:02d}m"


class Activity:
    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self.active = False
        self.waiting = False            # an approval or a question is up
        self.harness = ""
        self.phase = ""
        self.last_elapsed = 0.0         # the turn that just ended
        self._started = 0.0

    def on_event(self, ev: LiveEvent) -> None:
        if isinstance(ev, TurnStarted):
            self.active, self.waiting = True, False
            self.harness, self.phase = ev.harness, "starting"
            self._started = self._clock()
        elif not self.active:
            return
        elif isinstance(ev, ThinkingDelta):
            self.phase = "thinking"
        elif isinstance(ev, TextDelta):
            self.phase = "writing"
        elif isinstance(ev, ToolStarted):
            self.phase = f"running {ev.tool}"
        elif isinstance(ev, ToolFinished):
            self.phase = "working"
        elif isinstance(ev, (ApprovalRequest, QuestionRequest)):
            self.waiting = True
        elif isinstance(ev, (TurnFinished, Idle)):
            self.last_elapsed = self._clock() - self._started
            self.active = self.waiting = False

    def answered(self) -> None:
        if self.waiting:
            self.waiting, self.phase = False, "working"

    def text(self, queued: int = 0, width: int | None = None) -> str:
        if not self.active:
            return ""
        if self.waiting:
            return f"● {self.harness} is waiting for your answer"
        elapsed = self._clock() - self._started
        frame = _FRAMES[int(elapsed / _FRAME_SECONDS) % len(_FRAMES)]
        parts = [f"{frame} {self.harness}", self.phase, elapsed_text(elapsed)]
        extras = ([f"+{queued} queued"] if queued else []) + [_HINT]
        # too narrow: the hint goes first, then the queue count
        while extras and width is not None and len(" · ".join(parts + extras)) > width:
            extras.pop()
        return " · ".join(parts + extras)
