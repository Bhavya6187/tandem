"""Incremental JSONL transcript tailing.

Byte-offset based: reads only what was appended since the last confirmed
offset, keeps a partial trailing line buffered, and never advances the
confirmed offset past an incomplete line. Wake-ups come from watchdog fs
events (with watchdog's PollingObserver as fallback) plus an unconditional
poll interval, so a missed event can only delay — never lose — entries.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.polling import PollingObserver


@dataclass
class TailedLine:
    line_index: int      # 0-based index in the file
    end_offset: int      # byte offset just past this line's newline
    raw: dict | None     # parsed JSON, None if the line was not valid JSON
    text: str


class JsonlTailer:
    """Stateless-ish poller over one growing JSONL file. The caller owns the
    durable cursor; this class tracks only the in-memory read position."""

    def __init__(self, path: Path, start_offset: int = 0, start_line: int = 0):
        self.path = path
        self.offset = start_offset
        self.line_index = start_line

    def poll(self) -> list[TailedLine]:
        """Return all complete lines appended since the confirmed offset.
        A trailing line without a newline is left unconsumed (the offset does
        not advance past it), so it is re-read whole on a later poll."""
        try:
            size = self.path.stat().st_size
        except OSError:
            return []
        if size < self.offset:
            # File shrank: the harness rewrote it (not observed in the pinned
            # versions). Refusing to guess is safer than re-syncing dupes.
            raise TranscriptTruncated(self.path, self.offset, size)
        if size == self.offset:
            return []
        with open(self.path, "rb") as f:
            f.seek(self.offset)
            buf = f.read()
        out: list[TailedLine] = []
        pos = 0
        while True:
            nl = buf.find(b"\n", pos)
            if nl == -1:
                break
            line = buf[pos:nl]
            pos = nl + 1
            text = line.decode("utf-8", errors="replace").strip()
            if text:
                try:
                    raw = json.loads(text)
                    if not isinstance(raw, dict):
                        raw = None
                except json.JSONDecodeError:
                    raw = None
                out.append(TailedLine(self.line_index, self.offset + pos, raw, text))
            self.line_index += 1
        self.offset += pos
        return out


class TranscriptTruncated(RuntimeError):
    def __init__(self, path: Path, offset: int, size: int):
        super().__init__(f"{path} shrank from {offset} to {size} bytes")
        self.path, self.offset, self.size = path, offset, size


class _WakeHandler(FileSystemEventHandler):
    def __init__(self, wake: threading.Event, names: set[str]):
        self._wake = wake
        self._names = names

    def on_any_event(self, event) -> None:
        try:
            name = Path(str(event.src_path)).name
        except Exception:
            name = ""
        if not self._names or name in self._names:
            self._wake.set()


class TranscriptWatcher:
    """Owns the watchdog observer (or its polling fallback) plus the wake
    event that the sync loop blocks on."""

    def __init__(self, poll_interval: float = 0.5):
        self.wake = threading.Event()
        self.poll_interval = poll_interval
        self._observer = None
        self._watched: dict[Path, set[str]] = {}

    def watch(self, file_path: Path) -> None:
        directory = file_path.parent
        self._watched.setdefault(directory, set()).add(file_path.name)
        if self._observer is not None:
            self._schedule(directory)

    def start(self) -> None:
        for cls in (Observer, PollingObserver):
            try:
                self._observer = cls(timeout=self.poll_interval)
                for directory in self._watched:
                    self._schedule(directory)
                self._observer.start()
                return
            except Exception:
                self._observer = None
        # No observer at all: the sync loop's poll interval still covers us.

    def _schedule(self, directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        handler = _WakeHandler(self.wake, self._watched[directory])
        self._observer.schedule(handler, str(directory), recursive=False)

    def wait(self) -> None:
        """Block until an fs event or the fallback interval elapses."""
        self.wake.wait(timeout=self.poll_interval)
        self.wake.clear()

    def stop(self) -> None:
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=2)
            self._observer = None
