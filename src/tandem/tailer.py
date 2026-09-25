"""Incremental JSONL transcript tailing.

Byte-offset based: reads only what was appended since the last confirmed
offset, keeps a partial trailing line buffered, and never advances the
confirmed offset past an incomplete line. Wake-ups come from watchdog fs
events (with watchdog's PollingObserver as fallback) plus an unconditional
poll interval, so a missed event can only delay — never lose — entries.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import json
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.polling import PollingObserver

# The bootstrap service watchdog's FSEvents emitter talks to. A seatbelt
# sandbox (codex's, for one) denies the lookup; see fsevents_reachable().
_FSEVENTS_SERVICE = b"com.apple.FSEvents"


def _fsevents_lookup_kr() -> int:
    """kern_return of a bootstrap lookup of the FSEvents service: 0 when
    this process may talk to it, BOOTSTRAP_NOT_PRIVILEGED (1100) when a
    sandbox profile denies the mach-lookup. A plain mach call: safe to make
    where starting an FSEvents stream is not."""
    libc = ctypes.CDLL(ctypes.util.find_library("System"))
    libc.bootstrap_look_up.argtypes = [ctypes.c_uint, ctypes.c_char_p,
                                       ctypes.POINTER(ctypes.c_uint)]
    libc.bootstrap_look_up.restype = ctypes.c_int
    bootstrap_port = ctypes.c_uint.in_dll(libc, "bootstrap_port")
    port = ctypes.c_uint(0)
    kr = libc.bootstrap_look_up(bootstrap_port, _FSEVENTS_SERVICE, ctypes.byref(port))
    if kr == 0 and port.value:
        libc.mach_port_deallocate(ctypes.c_uint.in_dll(libc, "mach_task_self_"), port)
    return kr


def fsevents_reachable() -> bool:
    """Whether an FSEvents observer may be started in this process.

    watchdog's FSEvents emitter must not be started where the service is
    unreachable: its stream start fails, but the failed watch stays
    registered, and the observer's stop() then stops/invalidates/releases
    that already-released stream — heap corruption that kills the process
    later, in whatever code mallocs next (seen from sqlite and pydantic).
    Codex's seatbelt sandbox denies the lookup, so every pytest run codex
    made of this suite could die that way. Off macOS the observer is not
    FSEvents at all. A probe that cannot answer means polling: wrongly
    polling costs up to one poll interval of latency, wrongly starting
    FSEvents costs the process."""
    if sys.platform != "darwin":
        return True
    try:
        return _fsevents_lookup_kr() == 0
    except Exception:
        return False


@dataclass
class TailedLine:
    line_index: int      # 0-based index in the file (or turn ordinal)
    end_offset: int      # byte offset just past this line's newline (0 for DB units)
    raw: dict | None     # parsed JSON / native unit, None if not valid JSON
    text: str
    pos: dict | None = None   # storage-adapter cursor coords (opencode turns)

    def advance(self, cursor) -> None:
        """The one place a consumed unit moves the durable cursor. File units
        move byte/line; DB units additionally record their native position."""
        cursor.byte_offset = self.end_offset
        cursor.line_index = self.line_index + 1
        if self.pos is not None:
            cursor.pending["source_pos"] = self.pos


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
            if self.offset:
                # read once, gone now (claude's EnterWorktree renames it
                # away): "no news" here would drop every later turn unseen
                raise TranscriptMissing(self.path, self.offset)
            return []       # not created yet: the harness writes it on its first turn
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


class TranscriptMissing(RuntimeError):
    def __init__(self, path: Path, offset: int):
        super().__init__(f"{path} is gone after {offset} bytes of it were synced")
        self.path, self.offset = path, offset


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
        backends = (Observer, PollingObserver) if fsevents_reachable() else (PollingObserver,)
        for cls in backends:
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
