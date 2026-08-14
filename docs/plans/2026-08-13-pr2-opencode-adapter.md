# PR 2: Opencode Adapter — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship `src/tandem/harness/opencode.py` — a full-peer opencode harness: SQLite-backed transcript sync both ways, session creation via `opencode import`, launch/resume/one-off, doctor checks, and oracle tests against the real binary.

**Architecture:** Tandem pairs AI-CLI harnesses by translating each one's native session transcript into the others' formats (parse → normalized events → render), so the user can flip between them mid-session (Ctrl-]) with full context. PR 1 (branch `nharness-core`, plan `docs/plans/2026-08-13-pr1-nharness-core.md`) generalized the core to N participants; **this PR requires PR 1 merged**. Opencode stores sessions in one WAL-mode SQLite DB (`opencode.db`: `session`/`message`/`part` rows with JSON payloads), not a transcript file — this PR implements the storage-capability seams the adapter needs (with file-backed defaults so claude/codex are untouched), then the adapter itself: a completed-turn reader (tool parts mutate in place, so raw rows never cross the adapter boundary), a transactional idempotent writer with sentinel attribution, and shadow birth delegated to `opencode import`.

**Tech Stack:** Python 3.11+ stdlib only (`sqlite3`, `subprocess`); pytest; the installed `opencode` binary for oracle tests only (skip-if-missing).

**Spec:** `docs/specs/2026-08-13-opencode-harness-design.md` — this PR implements "The opencode adapter", "Storage capabilities", and the opencode parts of "Error handling and doctor" / "Testing". Read the spec first; the plan argues from it and the spec carries the storage facts (ordering rules, ID scheme, TUI hazards) with references into the opencode checkout at `~/git/opencode`.

## Branch / PR

- Requires PR 1 (`nharness-core`) merged. Branch from updated main: `git checkout main && git pull && git checkout -b opencode-adapter`.
- Commit after every task with the message given in the task.
- PR `opencode-adapter` → `main` when the merge gate at the bottom passes. Origin main is PR-only.

## What PR 1 built (the interfaces this plan consumes)

Verify these against the merged code before starting; PR 1's plan is the reference if anything looks off.

- `PairedSession` (`state.py`): fields `participants: list[str]`, `native_session_ids: dict[str, str | None]`; helpers `native_id(harness)`, `targets_for(source)` (participants minus source, list order), `next_active(current)` (cycle). No `shadow` property, no `other()` anywhere.
- `StateStore`: `create_session(cwd, active, participants, native_session_ids)`; `set_participants(tandem_id, list)`; `get_cursor(tandem_id, source, target)` / `SyncCursor.target` — cursors are per **direction**; DB recreated (moved to `state.db.old`) on schema mismatch.
- `SessionContext` (`events.py`): `direction: str` (`"<source>-><target>"`, validated against `ADAPTERS` membership), `source_id`/`target_id` properties, `source_session_id`/`target_session_id`, `harness_state: dict[str, dict]` + `state_for(harness_id)` (claude keeps `leaf_uuid`/`run_msg_id`/`model` under `"claude"`); persisted via `runner.ctx_from_cursor(session, cursor)` / `ctx_to_cursor`.
- `SyncEngine(store, session, source, target)` (`sync.py`): shadow resolved via `get_adapter(target).transcript_path(...)`; write-ahead intent in `cursor.pending["intent"]`.
- `ops`: `drain_source(store, session, source, *, flush_dangling=False)` fans out to every target; `fast_forward(store, session, source, target)`; `fast_forward_all(store, session, source)`; `switch_session(store, session, to=None)` (drain old → fast-forward-all new → set active); `run_oneoff` echo handled per direction.
- `runner`: `TailLoop(store, session, source, target, transcript, sink)`; sink factories take `(store, session, source, target)`; tail thread runs one loop per direction; `FlipMonitor` takes `status_probe` when `hasattr(adapter, "session_status")`; `ensure_warm` targets `session.next_active(active)` and **returns early when that is `"opencode"`** (the v1 cold-flip carve-out — already in place, nothing to do here).
- `HarnessAdapter` (`harness/base.py`): abstract interface plus `runtime_ready() -> tuple[bool, str]` (default `(True, "")`), `validate_transcript(path, session_id)` (default: JSONL parse + `_validate_entries`).
- `cli`: `_resolve_participants(warn_only=False)` (config ∩ installed ∩ ready; **skips names missing from `ADAPTERS`** — registering the real adapter in Task 10 makes opencode resolvable); `_pair_session(store, cwd, active, participants)` pre-creates the session of every non-active participant AND of opencode even when active (`opencode -s` needs the session to exist); `_narrow_participants` (resume drop rule); `config.load_harnesses()` with `SUPPORTED_HARNESSES = ("claude", "codex", "opencode")`.
- `COMPAT["opencode"] = CompatRange(tested="1.18.15", min_version=(1, 18))` (no ceiling); `ATTRIBUTION["opencode"] = "[via opencode]"`.
- `toolmap.map_pair(call, result, target)` — Task 13 here converts its target branch into the `_MAPPERS` registry with passthrough default.
- tests: `conftest.FakeOpencodeAdapter` + `conftest.Env3` (3-way env with the fake registered via `monkeypatch.setitem`) — core tests keep the fake even after this PR registers the real adapter; `tests/test_participants.py` has an autouse fixture registering the fake.

## Global Constraints

- After every task the FULL suite must pass: `python -m pytest tests/ -q`.
- Opencode compat floor: `1.18.15`+, no ceiling. Installed-but-unusable (DB undiscoverable, tables missing) ⇒ warn + drop from participants (fail closed); not-installed stays silent.
- Sentinel provider for every tandem-authored opencode row: `providerID: "tandem"`, `modelID: "<synced>"` — attribution, echo-skip marker, and what makes opencode degrade replay metadata instead of re-sending forged provider signatures.
- Echo suppression is cursor-based (PR 1's fast-forward); the sentinel parse-skip is the backstop.
- No reasoning content in either direction: opencode `reasoning` parts parse to `Thinking` (no text, dropped by converters); tandem writes NO reasoning parts.
- Tandem never constructs opencode `session` rows by hand — `opencode import` does (shadow birth only; incremental sync is direct SQL).
- All SQLite access to opencode's DB: read-write connection, `busy_timeout=5000`, `foreign_keys=ON`, one connection per thread, `BEGIN IMMEDIATE` for writes, short transactions.
- The opencode DB is discovered via `$OPENCODE_DB` (opencode's own override flag — also the test isolation mechanism) else `opencode db path`; discovery failure fails closed, never a guessed path.
- Completed-turn reads only: a turn is consumable when a later user row exists OR its last assistant has `time.completed` and a terminal `finish` (not `"tool-calls"`). Every transaction tandem writes ends on a closed assistant (`time.completed` + `finish: "stop"`) so the TUI never shows the session as perpetually "working".

## File Structure

| File | Responsibility |
|---|---|
| `src/tandem/harness/base.py` | storage-capability methods with JSONL defaults; `prepare_shadow` hook; `ShadowBusy` |
| `src/tandem/tailer.py` | `TailedLine.advance(cursor)` + optional `pos` |
| `src/tandem/sync.py` | append/intent through adapter hooks |
| `src/tandem/runner.py` | TailLoop reads through `make_source_reader`; busy-retry |
| `src/tandem/ops.py` | fast-forward/pending through adapter hooks |
| `src/tandem/harness/claude_code.py` | `prepare_shadow` override (leaf-repair moves out of sync.py) |
| `src/tandem/harness/opencode.py` | NEW — ids, DB access, reader, writer, import birth, doctor |
| `src/tandem/harness/__init__.py` | register `OpencodeAdapter` |
| `src/tandem/toolmap.py` | `_MAPPERS` registry, passthrough default |
| `src/tandem/doctor.py` | `runtime_ready` reporting line |
| `docs/formats.md` | opencode section |
| `tests/test_opencode.py` | NEW — adapter unit/golden tests (hermetic mini-DB via `OPENCODE_DB`) |
| `tests/test_opencode_oracle.py` | NEW — real-binary round-trips, skip-if-missing |

**Conscious deviation from the spec:** tandem-written opencode user rows always carry the sentinel `agent`/`model` rather than "the session's values if present" — the renderer would otherwise need a DB read per turn for a display-only nicety.

**Task numbering** continues from PR 1 (tasks 1–8) so cross-references stay stable: this PR is tasks 9–15.

---

## Phase 2 — the opencode adapter
### Task 9: Storage-capability seams (file-backed defaults, zero behavior change)

**Files:**
- Modify: `src/tandem/tailer.py:22-28` (TailedLine), `src/tandem/runner.py` (TailLoop), `src/tandem/sync.py` (append/intent via adapter), `src/tandem/ops.py:73-98` (fast-forward/pending via adapter), `src/tandem/harness/base.py`
- Test: `tests/test_tail.py`, `tests/test_sync.py`

**Interfaces:**
- Produces (defaults reproduce today's behavior exactly; only opencode overrides later):
  - `TailedLine.pos: dict | None = None` and `TailedLine.advance(cursor)` — the ONE place cursor position advances (used by both TailLoop and SyncEngine)
  - `HarnessAdapter.make_source_reader(session, cursor, transcript) -> SourceReader` — default `JsonlSourceReader` (wraps `JsonlTailer`; `poll() -> list[TailedLine]`)
  - `HarnessAdapter.watch_paths(session, transcript) -> list[Path]` — default `[transcript]`
  - `HarnessAdapter.shadow_append(ref, entries) -> None` — default `append_jsonl_fsync`
  - `HarnessAdapter.shadow_intent(ref, entries) -> dict` — default `{"pre_size": ref.stat().st_size}`
  - `HarnessAdapter.intent_landed(ref, intent) -> bool` — default size-grew check
  - `HarnessAdapter.fast_forward_cursor(session, cursor) -> None` — default file-bytes (source side)
  - `HarnessAdapter.pending_units(session, cursor) -> int` — default unsynced-lines count
- `ref` is whatever the target adapter's `transcript_path` returned (a file for claude/codex; the DB path for opencode).

- [ ] **Step 1: Write the failing tests** (add to `tests/test_tail.py`)

```python
def test_tailedline_advance_updates_cursor(tmp_path):
    from tandem.state import SyncCursor
    from tandem.tailer import TailedLine

    cur = SyncCursor(tandem_id="t", source="claude", target="codex")
    line = TailedLine(line_index=4, end_offset=100, raw={}, text="{}")
    line.advance(cur)
    assert (cur.byte_offset, cur.line_index) == (100, 5)
    assert "source_pos" not in cur.pending

    line2 = TailedLine(line_index=5, end_offset=0, raw={}, text="{}",
                       pos={"time": 9, "id": "msg_x"})
    line2.advance(cur)
    assert cur.pending["source_pos"] == {"time": 9, "id": "msg_x"}


def test_default_reader_matches_jsonl_tailer(tmp_path):
    from tandem.harness import get_adapter
    from tandem.state import SyncCursor

    p = tmp_path / "t.jsonl"
    p.write_text('{"a": 1}\n{"b": 2}\n')
    cur = SyncCursor(tandem_id="t", source="claude", target="codex")
    reader = get_adapter("claude").make_source_reader(None, cur, p)
    lines = reader.poll()
    assert [l.raw for l in lines] == [{"a": 1}, {"b": 2}]
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_tail.py -q`
Expected: FAIL — no `advance` / `make_source_reader`

- [ ] **Step 3: Implement**

`src/tandem/tailer.py` — extend the dataclass (`tailer.py:22-28`):

```python
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
```

`src/tandem/harness/base.py` — the capability block (append to `HarnessAdapter`, plus a module-level reader class):

```python
class JsonlSourceReader:
    """Default source reader: the existing byte-offset JSONL tailer."""

    def __init__(self, path: Path, cursor):
        from ..tailer import JsonlTailer

        self.tailer = JsonlTailer(
            path, start_offset=cursor.byte_offset, start_line=cursor.line_index
        )

    def poll(self):
        return self.tailer.poll()
```

```python
    # -- storage capabilities (file-backed defaults; opencode overrides) -----

    def make_source_reader(self, session, cursor, transcript: Path):
        return JsonlSourceReader(transcript, cursor)

    def watch_paths(self, session, transcript: Path) -> list[Path]:
        return [transcript]

    def shadow_append(self, ref: Path, entries: list[dict]) -> None:
        from ..util import append_jsonl_fsync

        append_jsonl_fsync(ref, entries)

    def shadow_intent(self, ref: Path, entries: list[dict]) -> dict:
        return {"pre_size": ref.stat().st_size}

    def intent_landed(self, ref: Path, intent: dict) -> bool:
        try:
            return ref.stat().st_size > int(intent["pre_size"])
        except (OSError, KeyError, ValueError):
            return False

    def fast_forward_cursor(self, session, cursor) -> None:
        """Mark everything currently in this SOURCE's store as consumed."""
        sid = session.native_id(self.id)
        path = self.transcript_path(session.cwd, sid) if sid else None
        if path is None:
            cursor.byte_offset = 0
            cursor.line_index = 0
            return
        data = path.read_bytes()
        cursor.byte_offset = len(data)
        cursor.line_index = data.count(b"\n")

    def pending_units(self, session, cursor) -> int:
        sid = session.native_id(self.id)
        path = self.transcript_path(session.cwd, sid) if sid else None
        if path is None:
            return 0
        try:
            data = path.read_bytes()
        except OSError:
            return 0
        if len(data) <= cursor.byte_offset:
            return 0
        return data[cursor.byte_offset:].count(b"\n")
```

Rewire the callers:
- `runner.TailLoop.__init__`: `self.reader = get_adapter(source).make_source_reader(session, self.cursor, transcript)`; `drain()` calls `self.reader.poll()` and per line: `self.sink.handle(line, self.ctx, self.cursor)` then `line.advance(self.cursor)` (replacing the two direct assignments at `runner.py:417-418`).
- `runner._run` watcher block: `for p in get_adapter(active).watch_paths(session, path): watcher.watch(p)` (replacing `watcher.watch(path)`).
- `sync.SyncEngine._prepare` (`sync.py:88-104`): `grew = self.target.intent_landed(self.shadow_path, intent)` replaces the stat comparison.
- `sync.SyncEngine._append_once` (`sync.py:181-191`): 

```python
        else:
            intent = self.target.shadow_intent(self.shadow_path, entries)
            cursor.pending["intent"] = {"line": line.line_index, **intent}
            self.store.save_cursor(cursor)
            self.target.shadow_append(self.shadow_path, entries)

        cursor.pending.pop("intent", None)
        line.advance(cursor)
        ctx_to_cursor(ctx, cursor)
        self.store.save_cursor(cursor)
```

  and the same substitution in `flush_dangling` (`sync.py:158-161`) and `_handle_failure`'s cursor advance (`sync.py:203-204` → `line.advance(cursor)`).
- `ops.fast_forward`: 

```python
def fast_forward(store: StateStore, session: PairedSession, source: str,
                 target: str) -> None:
    cursor = store.get_cursor(session.tandem_id, source, target)
    get_adapter(source).fast_forward_cursor(session, cursor)
    cursor.pending.pop("intent", None)
    store.save_cursor(cursor)
```

- `ops.unsynced_lines(session, store, source, target)` delegates to `get_adapter(source).pending_units(session, cursor)`.

- [ ] **Step 4: Two more seams — claude leaf-repair hook + busy-shadow retry**

The spec's code-model bullet moves the claude leaf-repair out of the sync engine, and its error-handling section requires "DB locked beyond busy_timeout → the append retries next tailer tick". Both land here.

Add to `HarnessAdapter` (`base.py`):

```python
    def prepare_shadow(self, ref, ctx) -> None:
        """Re-anchor renderer state to what is actually in the shadow store.
        Called before the first append of a sync run and after a crash-skip
        re-append. Default: nothing."""


class ShadowBusy(RuntimeError):
    """Shadow store locked beyond busy_timeout. The append transaction
    rolled back — nothing landed — so the tailer retries next tick."""
```

`ClaudeCodeAdapter` override (replaces the `if self.target_id == "claude":` blocks at `sync.py:105-115` and `sync.py:175-180` — both become `self.target.prepare_shadow(self.shadow_path, ctx)`):

```python
    def prepare_shadow(self, ref, ctx) -> None:
        """The claude file may have grown since tandem last wrote (its own
        CLI appends while claude is active): chain onto the real leaf and
        pick up the model claude last used for rendered entries."""
        st = ctx.state_for("claude")
        leaf = self.derive_leaf_uuid(ref)
        if leaf:
            st["leaf_uuid"] = leaf
        model = self.derive_last_model(ref)
        if model:
            st["model"] = model
```

`runner.TailLoop` — store `self.transcript = transcript`, count consumed lines, and catch the busy signal (nothing landed, so rebuilding in-memory state from the durable cursor is a clean retry):

```python
    def drain(self) -> int:
        """Process everything new; returns number of units consumed."""
        from .harness.base import ShadowBusy

        try:
            lines = self.reader.poll()
        except TranscriptTruncated as exc:
            self.errors.append(str(exc))
            return 0
        consumed = 0
        for line in lines:
            try:
                self.sink.handle(line, self.ctx, self.cursor)
            except ShadowBusy:
                # transactional append rolled back; rebuild context from the
                # durable cursor and let the next wake-up retry the unit
                self.cursor = self.store.get_cursor(
                    self.session.tandem_id, self.source, self.target)
                self.ctx = ctx_from_cursor(self.session, self.cursor)
                self.reader = get_adapter(self.source).make_source_reader(
                    self.session, self.cursor, self.transcript)
                break
            line.advance(self.cursor)
            ctx_to_cursor(self.ctx, self.cursor)
            consumed += 1
        if consumed:
            self.store.save_cursor(self.cursor)
            self.store.touch_sync(self.session.tandem_id)
        return consumed
```

Test (add to `tests/test_sync.py`):

```python
def test_shadow_busy_retries_without_advancing(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    from tandem.harness.base import ShadowBusy
    from tandem.harness import get_adapter

    write_line(env.claude_shadow, claude_user("busy test"))
    calls = {"n": 0}
    real_append = type(get_adapter("codex")).shadow_append

    def flaky_append(self, ref, entries):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ShadowBusy("locked")
        return real_append(self, ref, entries)

    monkeypatch.setattr(type(get_adapter("codex")), "shadow_append", flaky_append)
    loop = env.loop(source="claude", transcript=env.claude_shadow)
    assert loop.drain() == 0                       # busy: nothing consumed
    assert loop.drain() >= 1                       # retried clean
    assert any("busy test" in t for t in shadow_texts(env.codex_shadow))
```

- [ ] **Step 5: Run the full suite** — this task is seam-extraction plus the two behaviors above; every existing test must pass unchanged.

Run: `python -m pytest tests/ -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "refactor: storage-capability seams, claude prepare hook, busy-shadow retry"
```

---

### Task 10: opencode module — ids, DB access, discovery, readiness

**Files:**
- Create: `src/tandem/harness/opencode.py`
- Create: `tests/test_opencode.py`
- Test: `tests/test_opencode.py`

**Interfaces:**
- Produces:
  - `opencode.mint_id(prefix: str, descending: bool = False) -> str` — `<prefix>_` + 12 lowercase hex (48-bit `ms*4096+counter`, bitwise-NOT for descending) + 14 random base62; per-ms counter
  - `opencode.db_path() -> Path | None` — `$OPENCODE_DB` if set, else `opencode db path` (subprocess, cached; `_reset_db_cache()` test seam); None on any failure
  - `opencode.connect(db: Path) -> sqlite3.Connection` — row factory, `busy_timeout=5000`, `foreign_keys=ON`
  - `OpencodeAdapter(HarnessAdapter)` with `id="opencode"`, `display_name="opencode"`, `binary="opencode"`, `detect_version`, `version_supported`, `mint_session_id` (descending `ses_`), `runtime_ready() -> tuple[bool, str]`
  - registered in `ADAPTERS` (`harness/__init__.py`)
- Test fixture: `tests/test_opencode.py::mini_db` — a hermetic SQLite file with opencode's `project`/`session`/`message`/`part` tables (schema below), pointed at via `OPENCODE_DB`.

- [ ] **Step 1: Write the failing tests** (`tests/test_opencode.py`, NEW)

```python
"""Opencode adapter units against a hermetic mini-DB (no binary needed)."""

import sqlite3

import pytest

from tandem.harness import opencode

MINI_SCHEMA = """
CREATE TABLE project (id TEXT PRIMARY KEY, worktree TEXT NOT NULL);
CREATE TABLE session (
    id TEXT PRIMARY KEY, project_id TEXT NOT NULL, slug TEXT NOT NULL,
    directory TEXT NOT NULL, path TEXT, title TEXT NOT NULL,
    version TEXT NOT NULL, cost REAL NOT NULL DEFAULT 0,
    tokens_input INTEGER NOT NULL DEFAULT 0,
    tokens_output INTEGER NOT NULL DEFAULT 0,
    tokens_reasoning INTEGER NOT NULL DEFAULT 0,
    tokens_cache_read INTEGER NOT NULL DEFAULT 0,
    tokens_cache_write INTEGER NOT NULL DEFAULT 0,
    parent_id TEXT, agent TEXT, model TEXT,
    time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL,
    time_archived INTEGER
);
CREATE TABLE message (
    id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
    time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL,
    data TEXT NOT NULL
);
CREATE TABLE part (
    id TEXT PRIMARY KEY, message_id TEXT NOT NULL, session_id TEXT NOT NULL,
    time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL,
    data TEXT NOT NULL
);
"""


@pytest.fixture
def mini_db(tmp_path, monkeypatch):
    db = tmp_path / "opencode.db"
    conn = sqlite3.connect(db)
    conn.executescript(MINI_SCHEMA)
    conn.commit()
    conn.close()
    monkeypatch.setenv("OPENCODE_DB", str(db))
    opencode._reset_db_cache()
    return db


def test_mint_id_format_and_order(monkeypatch):
    ms = [1786577389138]
    monkeypatch.setattr(opencode, "_now_ms", lambda: ms[0])
    a = opencode.mint_id("msg")
    b = opencode.mint_id("msg")
    assert a[:16] == "msg_ff84f8652001"          # verified against a live id
    assert a < b                                  # ascending within the ms
    assert len(a) == 4 + 12 + 14


def test_mint_id_descending_sessions(monkeypatch):
    monkeypatch.setattr(opencode, "_now_ms", lambda: 1786577389117)
    s = opencode.mint_id("ses", descending=True)
    assert s[:16] == "ses_007b079c2ffe"          # ~(ms*4096+1), live-verified
    monkeypatch.setattr(opencode, "_now_ms", lambda: 1786577389117 + 5000)
    later = opencode.mint_id("ses", descending=True)
    assert later < s                              # newer sorts smaller


def test_db_path_env_override(mini_db):
    assert opencode.db_path() == mini_db


def test_db_path_none_when_undiscoverable(monkeypatch):
    monkeypatch.delenv("OPENCODE_DB", raising=False)
    opencode._reset_db_cache()
    monkeypatch.setattr(opencode, "_run",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("no binary")))
    assert opencode.db_path() is None


def test_runtime_ready_ok(mini_db):
    ok, reason = opencode.OpencodeAdapter().runtime_ready()
    assert ok, reason


def test_runtime_ready_fails_closed_without_tables(tmp_path, monkeypatch):
    db = tmp_path / "empty.db"
    sqlite3.connect(db).close()
    monkeypatch.setenv("OPENCODE_DB", str(db))
    opencode._reset_db_cache()
    ok, reason = opencode.OpencodeAdapter().runtime_ready()
    assert not ok and "table" in reason


def test_registered_in_adapters():
    from tandem.harness import get_adapter
    assert get_adapter("opencode").id == "opencode"
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_opencode.py -q`
Expected: FAIL — no module `tandem.harness.opencode`

- [ ] **Step 3: Implement** — `src/tandem/harness/opencode.py` (module head + this task's members):

```python
"""Opencode adapter.

Session storage observed on opencode 1.18.15 (docs/formats.md and the spec's
reference index into the checkout at ~/git/opencode):
- everything lives in one WAL-mode SQLite DB (`opencode db path`), tables
  session / message / part with JSON `data` payloads
- ids: <prefix>_ + 12 hex chars (48-bit ms*4096+counter; sessions bitwise-NOT
  it so they sort descending) + 14 random base62
- messages order by (time_created, id); parts by part id alone
- tool parts mutate IN PLACE (pending -> running -> completed), so reads
  consume whole completed turns, never raw rows
- external writes are the `opencode import` recipe: plain INSERTs; a session
  whose last message is not a completed assistant renders as "working"
- tandem-authored rows carry providerID "tandem": degrades reasoning replay
  and marks echoes
"""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Any

from .. import compat
from ..events import (
    AssistantMessage,
    NormalizedEvent,
    SessionContext,
    SystemEvent,
    Thinking,
    ToolCall,
    ToolResult,
    UserMessage,
)
from .base import HarnessAdapter

SENTINEL_PROVIDER = "tandem"
SENTINEL_MODEL = "<synced>"

# seam for tests (matches ops.py's convention)
_run = subprocess.run

_ID_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
_id_state = {"ms": 0, "counter": 0}


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def mint_id(prefix: str, descending: bool = False) -> str:
    """Port of opencode's identifier scheme (packages/schema/src/identifier.ts):
    48-bit big-endian hex of ms*4096+counter (counter resets each ms), NOT'd
    for descending ids, then 14 random base62 chars."""
    ms = _now_ms()
    if ms != _id_state["ms"]:
        _id_state["ms"] = ms
        _id_state["counter"] = 0
    _id_state["counter"] += 1
    value = (ms * 0x1000 + _id_state["counter"]) & 0xFFFFFFFFFFFF
    if descending:
        value = ~value & 0xFFFFFFFFFFFF
    suffix = "".join(secrets.choice(_ID_ALPHABET) for _ in range(14))
    return f"{prefix}_{value:012x}{suffix}"


_db_cache: dict = {}


def _reset_db_cache() -> None:
    _db_cache.clear()


def db_path() -> Path | None:
    """Opencode's DB. $OPENCODE_DB (opencode's own override flag) wins; else
    `opencode db path` once per process. None on ANY failure — discovery
    fails closed (the adapter is dropped from the usable set), never a
    guessed default path (the filename is channel-suffixed)."""
    env = os.environ.get("OPENCODE_DB")
    if env:
        return Path(env)
    if "path" in _db_cache:
        return _db_cache["path"]
    try:
        out = _run(["opencode", "db", "path"], capture_output=True, text=True,
                   timeout=20)
        line = out.stdout.strip().splitlines()[-1] if out.returncode == 0 and \
            out.stdout.strip() else ""
        p = Path(line) if line else None
        _db_cache["path"] = p if p is not None and p.is_file() else None
    except (OSError, subprocess.TimeoutExpired):
        _db_cache["path"] = None
    return _db_cache["path"]


def connect(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


class OpencodeAdapter(HarnessAdapter):
    id = "opencode"
    display_name = "opencode"
    binary = "opencode"

    # -- environment ---------------------------------------------------------

    def detect_version(self) -> str | None:
        return compat.detect_cli_version(self.binary)

    def version_supported(self, version_text: str) -> bool:
        return compat.version_supported("opencode", version_text)

    def runtime_ready(self) -> tuple[bool, str]:
        db = db_path()
        if db is None:
            return False, "database not discoverable (`opencode db path` failed)"
        try:
            with connect(db) as conn:
                names = {r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
        except sqlite3.Error as exc:
            return False, f"database unreadable: {exc}"
        missing = {"session", "message", "part"} - names
        if missing:
            return False, f"expected table(s) missing: {sorted(missing)}"
        return True, ""

    def mint_session_id(self) -> str:
        return mint_id("ses", descending=True)
```

Register in `src/tandem/harness/__init__.py`:

```python
from .base import HarnessAdapter
from .claude_code import ClaudeCodeAdapter
from .codex import CodexAdapter
from .opencode import OpencodeAdapter

ADAPTERS: dict[str, HarnessAdapter] = {
    "claude": ClaudeCodeAdapter(),
    "codex": CodexAdapter(),
    "opencode": OpencodeAdapter(),
}
```

(The class is abstract until Tasks 11-13 add the remaining methods; for THIS task add temporary `raise NotImplementedError` stubs for the abstract methods `transcript_path`, `create_shadow_transcript`, `interactive_argv`, `oneoff_argv`, `hook_argv_extra`, `parse_entry`, `render_events`, `render_placeholder` so the class instantiates — each stub is replaced by a later task. `tests/conftest.py`'s `Env` must also pin `OpencodeAdapter.detect_version` to `COMPAT["opencode"].tested` alongside the claude/codex pins at `conftest.py:84-92`, keeping every core test hermetic.)

- [ ] **Step 4: Run the new tests, then the full suite**

Run: `python -m pytest tests/test_opencode.py -q && python -m pytest tests/ -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat: opencode adapter skeleton — id scheme, db discovery, fail-closed readiness"
```

---

### Task 11: opencode read path — completed-turn reader, parse_entry, status probe

**Files:**
- Modify: `src/tandem/harness/opencode.py`
- Test: `tests/test_opencode.py`

**Interfaces:**
- Produces:
  - `OpencodeTurnReader(session_id, db, cursor)` — `poll() -> list[TailedLine]` where `raw` is one completed-turn object `{"user": {"message": {...}, "parts": [...]}, "assistants": [{"message": {...}, "parts": [...]}, ...]}` and `pos` is the terminal assistant's `{"time": int, "id": str}`; incomplete turns are not yielded
  - `OpencodeAdapter.make_source_reader(session, cursor, transcript)` / `watch_paths` (db + `-wal`) overrides
  - `OpencodeAdapter.parse_entry(turn_obj, ctx) -> list[NormalizedEvent]`
  - `OpencodeAdapter.session_status(session_id) -> str | None` — "busy" / "waiting" / None (capability the flip gate probes)
  - `OpencodeAdapter.fast_forward_cursor` / `pending_units` overrides
- Turn-completeness rule: a user row followed by ≥1 assistant rows, complete iff a later user row exists OR the last assistant's `data.time.completed` is set and `data.finish` not in `(None, "tool-calls")`.
- Echo rule: a turn whose user message `model.providerID == "tandem"` parses to `[SystemEvent(subtype="tandem_echo")]` (cursor advances; converter drops system events).

- [ ] **Step 1: Write the failing tests** (add to `tests/test_opencode.py`; helper builders included)

```python
def _insert_session(db, sid="ses_00test0000000000000000000", cwd="/proj"):
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO project VALUES ('p1', ?)", (cwd,))
        conn.execute(
            "INSERT INTO session (id, project_id, slug, directory, path, title,"
            " version, time_created, time_updated) VALUES (?, 'p1', 's', ?, '',"
            " 't', '1.18.15', 1, 1)", (sid, cwd))
    return sid


def _insert_message(db, sid, msg_id, data, t):
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO message VALUES (?, ?, ?, ?, ?)",
                     (msg_id, sid, t, t, json.dumps(data)))


def _insert_part(db, sid, msg_id, part_id, data, t=1):
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO part VALUES (?, ?, ?, ?, ?, ?)",
                     (part_id, msg_id, sid, t, t, json.dumps(data)))


def _seed_turn(db, sid, complete=True, provider="openai"):
    """user('is the readme updated?') + assistant(reasoning, text, tool,
    step parts) — shapes lifted from the operator's real 1.18.15 session."""
    _insert_message(db, sid, "msg_aa0000000001x", {
        "role": "user", "time": {"created": 1000}, "agent": "build",
        "model": {"providerID": provider, "modelID": "gpt-5.6-sol"}}, 1000)
    _insert_part(db, sid, "msg_aa0000000001x", "prt_aa0000000001x",
                 {"type": "text", "text": "is the readme updated?"})
    adata = {
        "parentID": "msg_aa0000000001x", "role": "assistant", "mode": "build",
        "agent": "build", "path": {"cwd": "/proj", "root": "/proj"}, "cost": 0,
        "tokens": {"total": 1, "input": 1, "output": 1, "reasoning": 0,
                   "cache": {"write": 0, "read": 0}},
        "modelID": "gpt-5.6-sol", "providerID": provider,
        "time": {"created": 2000},
    }
    if complete:
        adata["time"]["completed"] = 2500
        adata["finish"] = "stop"
    _insert_message(db, sid, "msg_ab0000000001x", adata, 2000)
    for pid, pdata in [
        ("prt_ab0000000001x", {"type": "step-start", "snapshot": "abc"}),
        ("prt_ab0000000002x", {"type": "reasoning",
                               "text": "**Inspecting git state**"}),
        ("prt_ab0000000003x", {"type": "tool", "tool": "bash",
                               "callID": "call_1",
                               "state": {"status": "completed",
                                         "input": {"command": "git status"},
                                         "output": "(no output)",
                                         "metadata": {}, "title": "git status",
                                         "time": {"start": 1, "end": 2}}}),
        ("prt_ab0000000004x", {"type": "text", "text": "Yes, it is current."}),
        ("prt_ab0000000005x", {"type": "step-finish", "reason": "stop",
                               "tokens": {}, "cost": 0}),
    ]:
        _insert_part(db, sid, "msg_ab0000000001x", pid, pdata)


def _cursor():
    from tandem.state import SyncCursor
    return SyncCursor(tandem_id="t", source="opencode", target="claude")


def test_reader_yields_completed_turn(mini_db):
    sid = _insert_session(mini_db)
    _seed_turn(mini_db, sid, complete=True)
    cur = _cursor()
    reader = opencode.OpencodeTurnReader(sid, mini_db, cur)
    lines = reader.poll()
    assert len(lines) == 1
    turn = lines[0].raw
    assert turn["user"]["parts"][0]["text"] == "is the readme updated?"
    assert len(turn["assistants"]) == 1
    assert lines[0].pos == {"time": 2000, "id": "msg_ab0000000001x"}
    # committing the pos means a second poll yields nothing
    lines[0].advance(cur)
    assert opencode.OpencodeTurnReader(sid, mini_db, cur).poll() == []


def test_reader_holds_incomplete_turn(mini_db):
    sid = _insert_session(mini_db)
    _seed_turn(mini_db, sid, complete=False)
    reader = opencode.OpencodeTurnReader(sid, mini_db, _cursor())
    assert reader.poll() == []


def test_parse_entry_maps_part_types(mini_db):
    sid = _insert_session(mini_db)
    _seed_turn(mini_db, sid, complete=True)
    reader = opencode.OpencodeTurnReader(sid, mini_db, _cursor())
    turn = reader.poll()[0].raw
    from tandem.events import SessionContext
    ctx = SessionContext(tandem_id="t", cwd="/proj",
                         direction="opencode->claude")
    events = opencode.OpencodeAdapter().parse_entry(turn, ctx)
    kinds = [e.kind for e in events]
    assert kinds == ["user_message", "system", "thinking", "tool_call",
                     "tool_result", "assistant_message", "system"]
    call = events[3]
    result = events[4]
    assert call.tool == "bash" and call.call_id == "call_1"
    assert result.output == "(no output)" and result.call_id == "call_1"
    assert events[0].turn_index == 1        # user message bumps the turn


def test_parse_entry_skips_tandem_echo(mini_db):
    sid = _insert_session(mini_db)
    _seed_turn(mini_db, sid, complete=True, provider="tandem")
    reader = opencode.OpencodeTurnReader(sid, mini_db, _cursor())
    turn = reader.poll()[0].raw
    from tandem.events import SessionContext
    ctx = SessionContext(tandem_id="t", cwd="/proj",
                         direction="opencode->claude")
    events = opencode.OpencodeAdapter().parse_entry(turn, ctx)
    assert [e.kind for e in events] == ["system"]
    assert events[0].subtype == "tandem_echo"


def test_session_status_probe(mini_db):
    sid = _insert_session(mini_db)
    adapter = opencode.OpencodeAdapter()
    _seed_turn(mini_db, sid, complete=False)
    assert adapter.session_status(sid) == "busy"
    with sqlite3.connect(mini_db) as conn:
        conn.execute(
            "UPDATE message SET data = json_set(data,"
            " '$.time.completed', 2500, '$.finish', 'stop')"
            " WHERE id = 'msg_ab0000000001x'")
    assert adapter.session_status(sid) == "waiting"
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_opencode.py -q`
Expected: FAIL — no `OpencodeTurnReader`

- [ ] **Step 3: Implement** (in `opencode.py`; replaces the Task-10 stubs for `parse_entry`)

```python
_AFTER_POS_SQL = (
    "SELECT id, time_created, data FROM message WHERE session_id = ?"
    " AND (time_created > ? OR (time_created = ? AND id > ?))"
    " ORDER BY time_created, id"
)


def _parts_for(conn: sqlite3.Connection, message_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT id, data FROM part WHERE message_id = ? ORDER BY id",
        (message_id,),
    ).fetchall()
    return [{**json.loads(r["data"]), "id": r["id"]} for r in rows]


def _is_closed(msg_data: dict) -> bool:
    time_completed = (msg_data.get("time") or {}).get("completed")
    return bool(time_completed) and msg_data.get("finish") not in (None, "tool-calls")


class OpencodeTurnReader:
    """Yields whole completed turns as single native objects. Tool parts
    mutate in place while a turn runs, so raw rows never cross this
    boundary; an incomplete turn is simply not yielded and re-read whole
    on a later poll."""

    def __init__(self, session_id: str, db: Path, cursor):
        self.session_id = session_id
        self.db = db
        self.cursor = cursor

    def poll(self):
        from ..tailer import TailedLine

        pos = self.cursor.pending.get("source_pos") or {"time": 0, "id": ""}
        out = []
        with connect(self.db) as conn:
            rows = conn.execute(
                _AFTER_POS_SQL,
                (self.session_id, pos["time"], pos["time"], pos["id"]),
            ).fetchall()
            msgs = [{**json.loads(r["data"]), "id": r["id"],
                     "_time_created": r["time_created"]} for r in rows]
            # partition into user-headed turns, in stored order
            turns: list[dict] = []
            for m in msgs:
                if m.get("role") == "user":
                    turns.append({"user": m, "assistants": []})
                elif turns:
                    turns[-1]["assistants"].append(m)
                # assistants before any user row: tail of a turn whose user
                # half was already consumed — cannot happen, because the
                # cursor only ever advances to a terminal assistant.
            index = self.cursor.line_index
            for i, t in enumerate(turns):
                followed_by_user = i + 1 < len(turns)
                a = t["assistants"]
                closed = bool(a) and _is_closed(a[-1])
                if not (followed_by_user or closed):
                    break   # incomplete turn; nothing after it is ready either
                turn_obj = {
                    "user": {"message": t["user"],
                             "parts": _parts_for(conn, t["user"]["id"])},
                    "assistants": [
                        {"message": m, "parts": _parts_for(conn, m["id"])}
                        for m in a
                    ],
                }
                last = a[-1] if a else t["user"]
                out.append(TailedLine(
                    line_index=index, end_offset=0, raw=turn_obj,
                    text=f"opencode turn {index}",
                    pos={"time": last["_time_created"], "id": last["id"]},
                ))
                index += 1
        return out
```

Adapter methods (parse + capability overrides):

```python
    # -- storage capabilities -------------------------------------------------

    def make_source_reader(self, session, cursor, transcript):
        return OpencodeTurnReader(session.native_id("opencode"), db_path(), cursor)

    def watch_paths(self, session, transcript) -> list[Path]:
        db = db_path()
        if db is None:
            return []
        return [db, db.with_name(db.name + "-wal")]

    def fast_forward_cursor(self, session, cursor) -> None:
        db = db_path()
        sid = session.native_id("opencode")
        if db is None or not sid:
            return
        with connect(db) as conn:
            row = conn.execute(
                "SELECT id, time_created FROM message WHERE session_id = ?"
                " ORDER BY time_created DESC, id DESC LIMIT 1", (sid,)
            ).fetchone()
        if row is not None:
            cursor.pending["source_pos"] = {"time": row["time_created"],
                                            "id": row["id"]}

    def pending_units(self, session, cursor) -> int:
        """Approximation: message rows past the cursor (a live turn's rows
        count until it closes) — good enough for status/doctor reporting."""
        db = db_path()
        sid = session.native_id("opencode")
        if db is None or not sid:
            return 0
        pos = cursor.pending.get("source_pos") or {"time": 0, "id": ""}
        with connect(db) as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM message WHERE session_id = ?"
                " AND (time_created > ? OR (time_created = ? AND id > ?))",
                (sid, pos["time"], pos["time"], pos["id"])).fetchone()
        return int(row[0])

    # -- flip gate ------------------------------------------------------------

    def session_status(self, session_id: str) -> str | None:
        """Busy iff the last message is a user prompt (turn starting) or an
        assistant still streaming (no time.completed / tool-calls finish)."""
        db = db_path()
        if db is None or not session_id:
            return None
        try:
            with connect(db) as conn:
                row = conn.execute(
                    "SELECT data FROM message WHERE session_id = ?"
                    " ORDER BY time_created DESC, id DESC LIMIT 1",
                    (session_id,)).fetchone()
        except sqlite3.Error:
            return None
        if row is None:
            return "waiting"
        data = json.loads(row["data"])
        if data.get("role") == "user":
            return "busy"
        return "waiting" if _is_closed(data) else "busy"

    # -- parsing --------------------------------------------------------------

    def parse_entry(self, raw: dict[str, Any], ctx: SessionContext) -> list[NormalizedEvent]:
        """One completed-turn object -> normalized events. Never raises on
        shape surprises; anything unrecognized degrades to a SystemEvent."""
        def sysev(subtype: str, text: str = "") -> SystemEvent:
            return SystemEvent(source="opencode", turn_index=ctx.turn_index,
                               subtype=subtype, text=text)

        if "user" not in raw or "assistants" not in raw:
            return [sysev(f"opencode:{raw.get('type', '?')}")]

        user_model = (raw["user"].get("message") or {}).get("model") or {}
        if user_model.get("providerID") == SENTINEL_PROVIDER:
            return [sysev("tandem_echo")]

        events: list[NormalizedEvent] = []
        ctx.turn_index += 1
        text = "\n".join(p.get("text", "") for p in raw["user"].get("parts", [])
                         if p.get("type") == "text")
        events.append(UserMessage(source="user", turn_index=ctx.turn_index,
                                  text=text))
        for a in raw["assistants"]:
            model = (a.get("message") or {}).get("modelID")
            for p in a.get("parts", []):
                ptype = p.get("type")
                if ptype == "text":
                    events.append(AssistantMessage(
                        source="opencode", turn_index=ctx.turn_index,
                        text=p.get("text", ""), model=model))
                elif ptype == "reasoning":
                    events.append(Thinking(source="opencode",
                                           turn_index=ctx.turn_index))
                elif ptype == "tool":
                    state = p.get("state") or {}
                    call_id = p.get("callID", "")
                    args = state.get("input")
                    events.append(ToolCall(
                        source="opencode", turn_index=ctx.turn_index,
                        call_id=call_id, tool=p.get("tool", ""),
                        arguments=args if isinstance(args, (dict, str)) else {}))
                    output = state.get("output", "")
                    if state.get("status") == "error":
                        output = str(state.get("error", output))
                    events.append(ToolResult(
                        source="opencode", turn_index=ctx.turn_index,
                        call_id=call_id,
                        output=output if isinstance(output, str) else str(output),
                        is_error=state.get("status") == "error"))
                else:
                    # step-start, step-finish, snapshot, patch, file, unknown
                    events.append(sysev(f"part:{ptype}"))
        return events
```

- [ ] **Step 4: Run the new tests, then the full suite**

Run: `python -m pytest tests/test_opencode.py -q && python -m pytest tests/ -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat: opencode read path — completed-turn reader, parser, status probe"
```

---

### Task 12: opencode write path — turn transaction writer, placeholder, idempotent intent

**Files:**
- Modify: `src/tandem/harness/opencode.py`
- Test: `tests/test_opencode.py`

**Interfaces:**
- Produces:
  - `OpencodeAdapter.transcript_path(cwd, session_id) -> Path | None` — the DB path when the session row exists, else None (this is what `SyncEngine` existence-checks and what `build_launch` sees, making opencode launches never "fresh")
  - `OpencodeAdapter.render_events(events, ctx) -> list[dict]` — row-op entries `{"table": "message"|"part", "id", "session_id", "message_id"?, "time", "data"}`; ids minted at render time (that is what makes crash replay idempotent)
  - `OpencodeAdapter.render_placeholder(text, ctx) -> list[dict]`
  - `OpencodeAdapter.shadow_append(ref, entries)` — one `BEGIN IMMEDIATE` transaction: `INSERT OR IGNORE` every row + bump `session.time_updated`
  - `OpencodeAdapter.shadow_intent(ref, entries) -> {"ids": [...]}` / `intent_landed(ref, intent)` — first-id existence check
- Renderer scratch in `ctx.state_for("opencode")`: `"turn_user_id"` (current turn's user row), `"assistant_id"` (the turn's open—already closed—assistant row).
- Every assistant row is written closed (`time.completed` + `finish: "stop"`, sentinel `providerID`), preserving the completed-assistant invariant after every transaction that ends in assistant content.

- [ ] **Step 1: Write the failing tests** (add to `tests/test_opencode.py`)

```python
def _render_ctx(sid="ses_00test0000000000000000000"):
    from tandem.events import SessionContext
    return SessionContext(tandem_id="t", cwd="/proj",
                          direction="claude->opencode",
                          target_session_id=sid)


def _events_turn():
    from tandem.events import AssistantMessage, ToolCall, ToolResult, UserMessage
    return [
        UserMessage(source="user", turn_index=1, text="[via claude-code] hi"),
        ToolCall(source="claude", turn_index=1, call_id="c1", tool="Bash",
                 arguments={"command": "ls"}),
        ToolResult(source="claude", turn_index=1, call_id="c1", output="file.txt"),
        AssistantMessage(source="claude", turn_index=1,
                         text="[via claude-code] done", model="claude-fable-5"),
    ]


def test_render_and_append_turn(mini_db):
    sid = _insert_session(mini_db)
    adapter = opencode.OpencodeAdapter()
    ctx = _render_ctx(sid)
    entries = adapter.render_events(_events_turn(), ctx)
    adapter.shadow_append(mini_db, entries)
    with sqlite3.connect(mini_db) as conn:
        conn.row_factory = sqlite3.Row
        msgs = conn.execute(
            "SELECT id, data FROM message WHERE session_id = ?"
            " ORDER BY time_created, id", (sid,)).fetchall()
    datas = [json.loads(m["data"]) for m in msgs]
    assert [d["role"] for d in datas] == ["user", "assistant"]
    assert datas[0]["agent"] and datas[0]["model"]          # schema-required
    assert datas[1]["providerID"] == "tandem"               # sentinel
    assert datas[1]["modelID"] == "claude-fable-5"
    assert datas[1]["time"]["completed"] and datas[1]["finish"] == "stop"
    with sqlite3.connect(mini_db) as conn:
        parts = conn.execute(
            "SELECT data FROM part WHERE message_id = ? ORDER BY id",
            (msgs[1]["id"],)).fetchall()
    pdatas = [json.loads(p[0]) for p in parts]
    assert [p["type"] for p in pdatas] == ["tool", "text"]
    assert pdatas[0]["state"]["status"] == "completed"
    assert pdatas[0]["callID"] == "c1"


def test_append_is_idempotent_by_ids(mini_db):
    sid = _insert_session(mini_db)
    adapter = opencode.OpencodeAdapter()
    entries = adapter.render_events(_events_turn(), _render_ctx(sid))
    adapter.shadow_append(mini_db, entries)
    adapter.shadow_append(mini_db, entries)      # crash replay
    with sqlite3.connect(mini_db) as conn:
        n = conn.execute("SELECT COUNT(*) FROM message WHERE session_id = ?",
                         (sid,)).fetchone()[0]
    assert n == 2                                # not 4


def test_intent_landed_checks_ids(mini_db):
    sid = _insert_session(mini_db)
    adapter = opencode.OpencodeAdapter()
    entries = adapter.render_events(_events_turn(), _render_ctx(sid))
    intent = adapter.shadow_intent(mini_db, entries)
    assert not adapter.intent_landed(mini_db, intent)
    adapter.shadow_append(mini_db, entries)
    assert adapter.intent_landed(mini_db, intent)


def test_written_turn_reads_back_as_echo(mini_db):
    """The sentinel closes the loop: what tandem writes, the reader skips."""
    sid = _insert_session(mini_db)
    adapter = opencode.OpencodeAdapter()
    adapter.shadow_append(
        mini_db, adapter.render_events(_events_turn(), _render_ctx(sid)))
    reader = opencode.OpencodeTurnReader(sid, mini_db, _cursor())
    lines = reader.poll()
    assert len(lines) == 1
    from tandem.events import SessionContext
    ctx = SessionContext(tandem_id="t", cwd="/proj",
                         direction="opencode->claude")
    events = adapter.parse_entry(lines[0].raw, ctx)
    assert [e.kind for e in events] == ["system"]
    assert events[0].subtype == "tandem_echo"


def test_placeholder_renders_closed(mini_db):
    sid = _insert_session(mini_db)
    adapter = opencode.OpencodeAdapter()
    entries = adapter.render_placeholder("[tandem: turn 3 could not be"
                                         " translated]", _render_ctx(sid))
    adapter.shadow_append(mini_db, entries)
    assert adapter.session_status(sid) == "waiting"   # never stuck "working"


def test_transcript_path_requires_session_row(mini_db):
    adapter = opencode.OpencodeAdapter()
    assert adapter.transcript_path("/proj", "ses_missing") is None
    sid = _insert_session(mini_db)
    assert adapter.transcript_path("/proj", sid) == mini_db
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_opencode.py -q`
Expected: FAIL — `render_events` still a stub

- [ ] **Step 3: Implement** (replacing the Task-10 stubs)

```python
    # -- session files -------------------------------------------------------

    def transcript_path(self, cwd: str, session_id: str) -> Path | None:
        """Opencode has no per-session file; the "transcript" is the DB, and
        it exists for a session exactly when the session row does."""
        db = db_path()
        if db is None or not session_id:
            return None
        try:
            with connect(db) as conn:
                row = conn.execute("SELECT 1 FROM session WHERE id = ?",
                                   (session_id,)).fetchone()
        except sqlite3.Error:
            return None
        return db if row is not None else None

    # -- rendering (shadow append) -------------------------------------------

    def _message_entry(self, sid: str, msg_id: str, data: dict, ms: int) -> dict:
        return {"table": "message", "id": msg_id, "session_id": sid,
                "time": ms, "data": data}

    def _part_entry(self, sid: str, msg_id: str, data: dict, ms: int) -> dict:
        return {"table": "part", "id": mint_id("prt"), "session_id": sid,
                "message_id": msg_id, "time": ms, "data": data}

    def _open_user(self, out: list, st: dict, sid: str, text: str, ms: int) -> None:
        uid = mint_id("msg")
        st["turn_user_id"] = uid
        st["assistant_id"] = None
        out.append(self._message_entry(sid, uid, {
            "role": "user", "time": {"created": ms}, "agent": "build",
            "model": {"providerID": SENTINEL_PROVIDER,
                      "modelID": SENTINEL_MODEL},
        }, ms))
        out.append(self._part_entry(sid, uid, {"type": "text", "text": text}, ms))

    def _ensure_assistant(self, out: list, st: dict, sid: str, ctx,
                          ms: int, model: str | None) -> str:
        if st.get("assistant_id"):
            return st["assistant_id"]
        if not st.get("turn_user_id"):
            # assistant content with no turn open (first sync after seed):
            # a context row keeps parentID valid
            self._open_user(out, st, sid, "[tandem] (context sync)", ms)
        aid = mint_id("msg")
        st["assistant_id"] = aid
        out.append(self._message_entry(sid, aid, {
            "parentID": st["turn_user_id"], "role": "assistant",
            "mode": "build", "agent": "build",
            "path": {"cwd": ctx.cwd, "root": ctx.cwd},
            "cost": 0,
            "tokens": {"input": 0, "output": 0, "reasoning": 0,
                       "cache": {"read": 0, "write": 0}},
            "modelID": model or SENTINEL_MODEL,
            "providerID": SENTINEL_PROVIDER,
            "time": {"created": ms, "completed": ms},
            "finish": "stop",
        }, ms))
        return aid

    def render_events(
        self, events: list[NormalizedEvent], ctx: SessionContext
    ) -> list[dict[str, Any]]:
        """Normalized events -> row-op entries. One user row per turn; one
        (closed) assistant row per turn carrying text and tool parts. The
        converter emits ToolCall/ToolResult adjacent, so pairing is local;
        a call that somehow arrives without its result closes as an error
        part rather than dangling (opencode's replay rejects dangles)."""
        sid = ctx.target_session_id
        st = ctx.state_for("opencode")
        out: list[dict[str, Any]] = []
        pending_call: ToolCall | None = None

        def flush_dangle(ms: int) -> None:
            nonlocal pending_call
            if pending_call is None:
                return
            aid = self._ensure_assistant(out, st, sid, ctx, ms, None)
            out.append(self._part_entry(sid, aid, self._tool_part(
                pending_call, output="(tool result not recorded)",
                is_error=True), ms))
            pending_call = None

        for ev in events:
            ms = _now_ms()
            if ev.kind == "user_message":
                flush_dangle(ms)
                self._open_user(out, st, sid, ev.text, ms)
            elif ev.kind == "assistant_message":
                flush_dangle(ms)
                aid = self._ensure_assistant(out, st, sid, ctx, ms, ev.model)
                out.append(self._part_entry(
                    sid, aid, {"type": "text", "text": ev.text}, ms))
            elif ev.kind == "tool_call":
                flush_dangle(ms)
                pending_call = ev
            elif ev.kind == "tool_result":
                if pending_call is None:
                    continue   # converter never emits an orphan result natively
                aid = self._ensure_assistant(out, st, sid, ctx, ms, None)
                out.append(self._part_entry(sid, aid, self._tool_part(
                    pending_call, output=ev.output, is_error=ev.is_error), ms))
                pending_call = None
            # thinking/system: dropped (no reasoning parts by spec)
        flush_dangle(_now_ms())
        return out

    def _tool_part(self, call: ToolCall, output: str, is_error: bool) -> dict:
        ms = _now_ms()
        return {
            "type": "tool", "tool": call.tool, "callID": call.call_id,
            "state": {
                "status": "error" if is_error else "completed",
                "input": call.arguments if isinstance(call.arguments, dict)
                         else {"input": call.arguments},
                **({"error": output} if is_error else {"output": output}),
                "title": "", "metadata": {},
                "time": {"start": ms, "end": ms},
            },
        }

    def render_placeholder(self, text: str, ctx: SessionContext) -> list[dict[str, Any]]:
        sid = ctx.target_session_id
        st = ctx.state_for("opencode")
        out: list[dict[str, Any]] = []
        ms = _now_ms()
        self._open_user(out, st, sid, text, ms)
        self._ensure_assistant(out, st, sid, ctx, ms, None)
        aid = st["assistant_id"]
        out.append(self._part_entry(
            sid, aid, {"type": "text", "text": "[tandem] (turn recorded as a"
                       " placeholder; see the note above)"}, ms))
        return out

    # -- shadow append (transactional, idempotent) ----------------------------

    def shadow_append(self, ref: Path, entries: list[dict]) -> None:
        """One transaction per synced unit. A lock outlasting busy_timeout
        raises ShadowBusy — the transaction rolled back, nothing landed, and
        the tailer retries the whole unit next tick (spec: error handling)."""
        from .base import ShadowBusy

        if not entries:
            return
        conn = connect(ref)
        try:
            try:
                conn.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as exc:
                raise ShadowBusy(str(exc)) from exc
            for e in entries:
                if e["table"] == "message":
                    conn.execute(
                        "INSERT OR IGNORE INTO message"
                        " (id, session_id, time_created, time_updated, data)"
                        " VALUES (?, ?, ?, ?, ?)",
                        (e["id"], e["session_id"], e["time"], e["time"],
                         json.dumps(e["data"])))
                else:
                    conn.execute(
                        "INSERT OR IGNORE INTO part"
                        " (id, message_id, session_id, time_created,"
                        " time_updated, data) VALUES (?, ?, ?, ?, ?, ?)",
                        (e["id"], e["message_id"], e["session_id"], e["time"],
                         e["time"], json.dumps(e["data"])))
            conn.execute("UPDATE session SET time_updated = ? WHERE id = ?",
                         (entries[-1]["time"], entries[0]["session_id"]))
            conn.commit()
        finally:
            conn.close()

    def shadow_intent(self, ref: Path, entries: list[dict]) -> dict:
        return {"ids": [(e["table"], e["id"]) for e in entries]}

    def intent_landed(self, ref: Path, intent: dict) -> bool:
        ids = intent.get("ids") or []
        if not ids:
            return False
        table, first = ids[0]
        if table not in ("message", "part"):
            return False
        try:
            with connect(ref) as conn:
                row = conn.execute(
                    f"SELECT 1 FROM {table} WHERE id = ?", (first,)).fetchone()
        except sqlite3.Error:
            return False
        return row is not None
```

- [ ] **Step 4: Run the new tests, then the full suite**

Run: `python -m pytest tests/test_opencode.py -q && python -m pytest tests/ -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat: opencode write path — transactional turn writer, sentinel attribution, idempotent replay"
```

---

### Task 13: Shadow birth via `opencode import`, launch surface, toolmap registry

**Files:**
- Modify: `src/tandem/harness/opencode.py`, `src/tandem/toolmap.py:44-57`
- Test: `tests/test_opencode.py`, `tests/test_toolmap.py`

**Interfaces:**
- Produces:
  - `OpencodeAdapter.create_shadow_transcript(cwd, session_id, ctx, note) -> Path` — writes an export-format JSON, runs `opencode import <file>` in `cwd`, verifies the session row, seeds `ctx.state_for("opencode")["turn_user_id"]`, returns the DB path; raises `RuntimeError` with import's stderr tail on failure
  - `OpencodeAdapter.interactive_argv(session_id, fresh) -> ["opencode", "-s", sid]`
  - `OpencodeAdapter.oneoff_argv(session_id, prompt) -> ["opencode", "run", "-s", sid, prompt]`
  - `OpencodeAdapter.hook_argv_extra(sentinel) -> []` (WAL fs-watch is the wake-up)
  - `OpencodeAdapter.quit_keystrokes() -> [b"\x03", b"\x03"]` — **live-verification item 2**: pin the real recipe against the installed TUI before release, like the claude `[^C,^C,^C]` recipe was
  - `toolmap._MAPPERS: dict[str, dict]` — `map_pair` falls through to `_passthrough` for any unlisted target (opencode gets Tier-2 passthrough in v1)

- [ ] **Step 1: Write the failing tests**

```python
def test_create_shadow_via_import(mini_db, tmp_path, monkeypatch):
    """The import subprocess is simulated: assert the payload shape, then
    perform the inserts import would do."""
    adapter = opencode.OpencodeAdapter()
    captured = {}

    def fake_run(argv, **kwargs):
        assert argv[:3] == ["opencode", "import", argv[2]]
        payload = json.loads(Path(argv[2]).read_text())
        captured["payload"] = payload
        info = payload["info"]
        with sqlite3.connect(mini_db) as conn:
            conn.execute("INSERT OR IGNORE INTO project VALUES ('p1', ?)",
                         (kwargs.get("cwd"),))
            conn.execute(
                "INSERT INTO session (id, project_id, slug, directory, path,"
                " title, version, time_created, time_updated)"
                " VALUES (?, 'p1', 'slug', ?, '', ?, ?, ?, ?)",
                (info["id"], kwargs.get("cwd"), info["title"],
                 info["version"], info["time"]["created"],
                 info["time"]["updated"]))
            for m in payload["messages"]:
                mi = m["info"]
                conn.execute(
                    "INSERT INTO message VALUES (?, ?, ?, ?, ?)",
                    (mi["id"], info["id"], mi["time"]["created"],
                     mi["time"]["created"],
                     json.dumps({k: v for k, v in mi.items()
                                 if k not in ("id", "sessionID")})))
                for p in m["parts"]:
                    conn.execute(
                        "INSERT INTO part VALUES (?, ?, ?, ?, ?, ?)",
                        (p["id"], mi["id"], info["id"], 1, 1,
                         json.dumps({k: v for k, v in p.items()
                                     if k not in ("id", "sessionID",
                                                  "messageID")})))
        import subprocess
        return subprocess.CompletedProcess(argv, 0, stdout="Imported", stderr="")

    monkeypatch.setattr(opencode, "_run", fake_run)
    sid = adapter.mint_session_id()
    ctx = _render_ctx(sid)
    path = adapter.create_shadow_transcript(str(tmp_path), sid, ctx,
                                            "[tandem] seed note")
    assert path == mini_db
    payload = captured["payload"]
    info = payload["info"]
    assert info["id"] == sid
    assert "agent" not in info and "model" not in info     # session-level omitted
    user, assistant = (m["info"] for m in payload["messages"])
    assert user["agent"] == "build"                        # message-level REQUIRED
    assert user["model"] == {"providerID": "tandem", "modelID": "<synced>"}
    assert assistant["providerID"] == "tandem"
    assert assistant["time"]["completed"] and assistant["finish"] == "stop"
    assert ctx.state_for("opencode")["turn_user_id"] == user["id"]
    assert adapter.session_status(sid) == "waiting"        # lists as idle


def test_create_shadow_raises_on_import_failure(mini_db, tmp_path, monkeypatch):
    import subprocess
    monkeypatch.setattr(
        opencode, "_run",
        lambda argv, **k: subprocess.CompletedProcess(argv, 1, stdout="",
                                                      stderr="boom"))
    adapter = opencode.OpencodeAdapter()
    with pytest.raises(RuntimeError, match="boom"):
        adapter.create_shadow_transcript(str(tmp_path), adapter.mint_session_id(),
                                         _render_ctx(), "[tandem] seed")


def test_launch_surface():
    adapter = opencode.OpencodeAdapter()
    assert adapter.interactive_argv("ses_x", fresh=False) == \
        ["opencode", "-s", "ses_x"]
    assert adapter.oneoff_argv("ses_x", "hi") == \
        ["opencode", "run", "-s", "ses_x", "hi"]
    assert adapter.hook_argv_extra(Path("/tmp/s")) == []
    assert adapter.quit_keystrokes() == [b"\x03", b"\x03"]
```

And in `tests/test_toolmap.py`:

```python
def test_map_pair_unknown_target_passthrough():
    from tandem.events import ToolCall, ToolResult
    from tandem.toolmap import map_pair
    call = ToolCall(source="claude", call_id="c", tool="Bash",
                    arguments={"command": "ls"})
    result = ToolResult(source="claude", call_id="c", output="ok")
    mc, mr = map_pair(call, result, "opencode")
    assert mc.tool == "Bash" and mc.arguments == {"command": "ls"}
    assert mr.output == "ok"
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_opencode.py tests/test_toolmap.py -q`
Expected: FAIL

- [ ] **Step 3: Implement**

`opencode.py`:

```python
    # -- launching -----------------------------------------------------------

    def interactive_argv(self, session_id: str | None, fresh: bool) -> list[str]:
        assert session_id, "opencode sessions are created at pair time"
        return [self.binary, "-s", session_id]

    def oneoff_argv(self, session_id: str, prompt: str) -> list[str]:
        return [self.binary, "run", "-s", session_id, prompt]

    def hook_argv_extra(self, sentinel: Path) -> list[str]:
        # No per-invocation hook flag exists; the tailer's fs-watch on the
        # -wal file is the wake-up signal (watch_paths).
        return []

    def quit_keystrokes(self) -> list[bytes]:
        # LIVE-VERIFY before release (spec item 2): pinned per version like
        # the claude [^C,^C,^C] recipe; the SIGTERM ladder backstops.
        return [b"\x03", b"\x03"]

    # -- shadow birth (delegated to `opencode import`) ------------------------

    def create_shadow_transcript(
        self, cwd: str, session_id: str, ctx: SessionContext, note: str
    ) -> Path:
        """Tandem never hand-writes opencode session rows. Mint the ids,
        write a minimal export-format file, and let `opencode import` run
        its own schema decoders, project bootstrap, and directory re-homing.
        One subprocess, once per session, off the sync path."""
        import tempfile

        now = _now_ms()
        version = self.detect_version() or "0.0.0"
        user_id = mint_id("msg")
        asst_id = mint_id("msg")
        sentinel_model = {"providerID": SENTINEL_PROVIDER,
                          "modelID": SENTINEL_MODEL}
        payload = {
            "info": {
                # projectID/directory/path are placeholders: import re-homes
                # them from its own instance context. agent/model omitted
                # (optional session-level; opencode's default applies).
                "id": session_id,
                "projectID": "tandem-import",
                "directory": cwd,
                "title": "tandem paired session",
                "version": version,
                "time": {"created": now, "updated": now},
                "cost": 0,
                "tokens": {"input": 0, "output": 0, "reasoning": 0,
                           "cache": {"read": 0, "write": 0}},
            },
            "messages": [
                {"info": {"id": user_id, "sessionID": session_id,
                          "role": "user", "time": {"created": now},
                          "agent": "build", "model": sentinel_model},
                 "parts": [{"id": mint_id("prt"), "sessionID": session_id,
                            "messageID": user_id, "type": "text",
                            "text": note}]},
                {"info": {"id": asst_id, "sessionID": session_id,
                          "role": "assistant", "parentID": user_id,
                          "time": {"created": now, "completed": now},
                          "modelID": SENTINEL_MODEL,
                          "providerID": SENTINEL_PROVIDER,
                          "mode": "build", "agent": "build",
                          "path": {"cwd": cwd, "root": cwd},
                          "cost": 0, "finish": "stop",
                          "tokens": {"input": 0, "output": 0, "reasoning": 0,
                                     "cache": {"read": 0, "write": 0}}},
                 "parts": [{"id": mint_id("prt"), "sessionID": session_id,
                            "messageID": asst_id, "type": "text",
                            "text": "[tandem] Session created; context syncs"
                                    " in from the paired session."}]},
            ],
        }
        with tempfile.NamedTemporaryFile(
            "w", suffix=".json", prefix="tandem-oc-seed-", delete=False
        ) as f:
            json.dump(payload, f)
            seed_file = Path(f.name)
        try:
            out = _run([self.binary, "import", str(seed_file)], cwd=cwd,
                       capture_output=True, text=True, timeout=120)
            if out.returncode != 0:
                tail = (out.stderr or out.stdout or "").strip().splitlines()
                raise RuntimeError(
                    f"opencode import failed ({out.returncode})"
                    + (f": {tail[-1][:200]}" if tail else ""))
        finally:
            seed_file.unlink(missing_ok=True)
        db = self.transcript_path(cwd, session_id)
        if db is None:
            raise RuntimeError(
                f"opencode import reported success but session {session_id}"
                " is not in the database")
        ctx.state_for("opencode")["turn_user_id"] = user_id
        ctx.state_for("opencode")["assistant_id"] = None
        return db
```

`toolmap.py` (`:44-57`):

```python
_MAPPERS: dict[str, dict] = {}   # filled after the mapper tables below


def map_pair(
    call: ToolCall, result: ToolResult, target: str
) -> tuple[ToolCall, ToolResult]:
    """One completed source-native pair -> one target-native pair. Targets
    without a Tier-1 table (opencode, any future harness) pass through."""
    try:
        fn = _MAPPERS.get(target, {}).get(call.tool)
        if fn is not None:
            mapped = fn(call, result)
            if mapped is not None:
                return mapped
    except Exception:
        pass  # honesty rule: surprises fall through to pass-through
    return _passthrough(call, result, target)
```

and at the bottom of the file, after `_TO_CODEX` / `_TO_CLAUDE`:

```python
_MAPPERS.update({"codex": _TO_CODEX, "claude": _TO_CLAUDE})
```

- [ ] **Step 4: Run the new tests, then the full suite**

Run: `python -m pytest tests/test_opencode.py tests/test_toolmap.py -q && python -m pytest tests/ -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat: opencode shadow birth via import, launch surface, toolmap passthrough registry"
```

---

### Task 14: Doctor — opencode section

**Files:**
- Modify: `src/tandem/harness/opencode.py`, `src/tandem/doctor.py`
- Test: `tests/test_opencode.py`, `tests/test_memory_doctor.py`

**Interfaces:**
- Produces: `OpencodeAdapter.validate_transcript(path, session_id) -> list[str]` — full override (no JSONL prelude): session row exists; ≥1 message; last message is a closed assistant; DB journal mode is WAL. Doctor's existing participant loop (PR 1, Task 8) calls it via the dispatch shim, so no doctor loop changes are needed — plus one addition: when opencode is installed, `run_doctor` reports `runtime_ready()` as ok/warn.

- [ ] **Step 1: Write the failing tests** (add to `tests/test_opencode.py`)

```python
def test_validate_transcript_healthy(mini_db):
    sid = _insert_session(mini_db)
    _seed_turn(mini_db, sid, complete=True)
    adapter = opencode.OpencodeAdapter()
    assert adapter.validate_transcript(mini_db, sid) == []


def test_validate_transcript_flags_open_turn(mini_db):
    sid = _insert_session(mini_db)
    _seed_turn(mini_db, sid, complete=False)
    problems = opencode.OpencodeAdapter().validate_transcript(mini_db, sid)
    assert any("completed" in p for p in problems)


def test_validate_transcript_missing_session(mini_db):
    problems = opencode.OpencodeAdapter().validate_transcript(
        mini_db, "ses_nope")
    assert any("not in the opencode database" in p for p in problems)
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_opencode.py -q`
Expected: FAIL — opencode inherits the JSONL default validator

- [ ] **Step 3: Implement** (`opencode.py`)

```python
    # -- doctor ---------------------------------------------------------------

    def validate_transcript(self, path: Path, session_id: str | None) -> list[str]:
        """Structural dry-resume for a DB-backed session. `path` is the DB."""
        problems: list[str] = []
        try:
            with connect(path) as conn:
                if session_id is not None:
                    row = conn.execute("SELECT 1 FROM session WHERE id = ?",
                                       (session_id,)).fetchone()
                    if row is None:
                        return [f"session {session_id} not in the opencode database"]
                mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
                if str(mode).lower() != "wal":
                    problems.append(f"journal_mode is {mode!r}, expected wal")
                last = conn.execute(
                    "SELECT data FROM message WHERE session_id = ?"
                    " ORDER BY time_created DESC, id DESC LIMIT 1",
                    (session_id,)).fetchone()
        except sqlite3.Error as exc:
            return [f"cannot read opencode database: {exc}"]
        if last is None:
            problems.append("no messages (session would render empty)")
            return problems
        data = json.loads(last["data"])
        if data.get("role") != "assistant" or not _is_closed(data):
            problems.append(
                "last message is not a completed assistant — the opencode TUI"
                " would show this session as perpetually working")
        return problems
```

(The mini-DB is created without `PRAGMA journal_mode=WAL`, so `test_validate_transcript_healthy` needs the fixture to set it: add `conn.execute("PRAGMA journal_mode=WAL")` to `mini_db` before the schema script.)

In `doctor.run_doctor`, inside the version loop's installed-and-supported branch, add the readiness line:

```python
        else:
            report.ok(f"{adapter.display_name}: {v}")
            ok, reason = adapter.runtime_ready()
            if not ok:
                report.warn(f"{adapter.display_name}: installed but unusable — {reason}")
```

- [ ] **Step 4: Run the full suite**

Run: `python -m pytest tests/ -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat: doctor validates opencode sessions structurally"
```

---

### Task 15: Oracle tests + docs

**Files:**
- Create: `tests/test_opencode_oracle.py`
- Modify: `docs/formats.md`, `src/tandem/memory_sync.py:37` (comment only)
- Test: `tests/test_opencode_oracle.py`

**Interfaces:**
- The oracle suite runs ONLY when a supported opencode binary is on PATH; it isolates via `OPENCODE_DB` pointing at a temp DB (opencode's own override flag), so the user's real database is never touched.

- [ ] **Step 1: Write the tests** (`tests/test_opencode_oracle.py`, NEW — these are expected to pass immediately where the binary exists, and skip elsewhere; they are the schema-fidelity net)

```python
"""Round-trips through the REAL opencode binary (skip-if-missing).

Isolated via OPENCODE_DB -> a temp database that opencode migrates into
existence on first run; the operator's real DB is never touched.
"""

import json
import os
import shutil
import subprocess

import pytest

from tandem import compat
from tandem.harness import opencode

_version = compat.detect_cli_version("opencode")
pytestmark = pytest.mark.skipif(
    shutil.which("opencode") is None
    or _version is None
    or not compat.version_supported("opencode", _version),
    reason="no supported opencode binary on PATH",
)


@pytest.fixture
def oracle_env(tmp_path, monkeypatch):
    db = tmp_path / "oracle.db"
    monkeypatch.setenv("OPENCODE_DB", str(db))
    opencode._reset_db_cache()
    cwd = tmp_path / "proj"
    cwd.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=cwd, check=True)
    env = dict(os.environ, OPENCODE_DB=str(db))
    return db, str(cwd), env


def _export(sid, cwd, env):
    out = subprocess.run(["opencode", "export", sid], cwd=cwd, env=env,
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_import_birth_roundtrips_through_export(oracle_env):
    db, cwd, env = oracle_env
    adapter = opencode.OpencodeAdapter()
    from tandem.events import SessionContext
    sid = adapter.mint_session_id()
    ctx = SessionContext(tandem_id="t", cwd=cwd,
                         direction="claude->opencode",
                         target_session_id=sid)
    path = adapter.create_shadow_transcript(cwd, sid, ctx, "[tandem] oracle seed")
    assert path == db
    data = _export(sid, cwd, env)
    assert data["info"]["id"] == sid
    texts = [p.get("text", "") for m in data["messages"] for p in m["parts"]]
    assert any("oracle seed" in t for t in texts)


def test_synced_turn_survives_export(oracle_env):
    db, cwd, env = oracle_env
    adapter = opencode.OpencodeAdapter()
    from tandem.events import (AssistantMessage, SessionContext, ToolCall,
                               ToolResult, UserMessage)
    sid = adapter.mint_session_id()
    ctx = SessionContext(tandem_id="t", cwd=cwd,
                         direction="claude->opencode",
                         target_session_id=sid)
    adapter.create_shadow_transcript(cwd, sid, ctx, "[tandem] oracle seed")
    events = [
        UserMessage(source="user", turn_index=1, text="[via claude-code] hello"),
        ToolCall(source="claude", turn_index=1, call_id="c1", tool="Bash",
                 arguments={"command": "true"}),
        ToolResult(source="claude", turn_index=1, call_id="c1", output="ok"),
        AssistantMessage(source="claude", turn_index=1,
                         text="[via claude-code] done", model="claude-fable-5"),
    ]
    adapter.shadow_append(db, adapter.render_events(events, ctx))
    data = _export(sid, cwd, env)
    texts = [p.get("text", "") for m in data["messages"] for p in m["parts"]]
    assert any("hello" in t for t in texts)
    assert any("done" in t for t in texts)
    roles = [m["info"]["role"] for m in data["messages"]]
    assert roles[-1] == "assistant"


def test_session_listed(oracle_env):
    db, cwd, env = oracle_env
    adapter = opencode.OpencodeAdapter()
    from tandem.events import SessionContext
    sid = adapter.mint_session_id()
    ctx = SessionContext(tandem_id="t", cwd=cwd,
                         direction="claude->opencode",
                         target_session_id=sid)
    adapter.create_shadow_transcript(cwd, sid, ctx, "[tandem] oracle seed")
    out = subprocess.run(["opencode", "session", "list"], cwd=cwd, env=env,
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    assert sid in out.stdout
```

- [ ] **Step 2: Run them** (on this machine the binary exists)

Run: `python -m pytest tests/test_opencode_oracle.py -q`
Expected: PASS (or targeted debugging of the real format — THIS is where live-verification items 1 and 3 get settled; adjust the seed payload if opencode's decoder rejects a field, and record what changed in docs/formats.md)

- [ ] **Step 3: Document** — append to `docs/formats.md`:

```markdown
## opencode session storage (opencode 1.18.15)

- Storage: one WAL-mode SQLite DB for everything — `opencode db path`
  (channel-suffixed filename; `$OPENCODE_DB` overrides). No per-session
  files. Tables: `session`, `message`, `part`; JSON payloads in `data`
  minus the id/fk columns. FKs cascade part -> message -> session -> project.
- Ordering: messages by `(time_created, id)`; parts by part id ONLY.
- IDs: `<prefix>_` + 12 hex chars (48-bit `ms*4096+counter`) + 14 random
  base62. `ses_` NOTs the value (descending); `msg_`/`prt_` ascending.
  Live-verified: msg @ ms=1786577389138 ctr=1 -> `ff84f8652001`.
- Threading is flat: assistant `parentID` = the turn's user message id.
- Spelling trap: session-row `model` JSON uses `{id, providerID}`; message
  payloads use `{modelID, providerID}`. Session-level `agent`/`model` are
  optional; MESSAGE-level are required on user messages.
- Tool parts mutate in place (pending -> running -> completed): tandem reads
  whole completed turns only (last assistant has `time.completed` + terminal
  `finish`).
- External writes: the `opencode import` recipe (plain INSERT, conflict-
  ignore). Tandem's shadow birth delegates to `opencode import`; incremental
  sync writes rows directly with pre-minted ids (idempotent replay).
- Tandem attribution: `providerID: "tandem"`, `modelID: "<synced>"` — marks
  echoes for the parser and makes opencode degrade replay metadata instead
  of re-sending forged provider signatures.
- Hazards: a running TUI never sees external rows for an already-synced
  session (writes land while opencode is closed; flips relaunch it); a
  session whose last message is not a completed assistant renders as
  perpetually "working"; the session list window is 30 days by
  `time_updated`; always open the DB read-write (WAL).
- Resume: `opencode -s <id>` (id must exist; no directory match);
  one-off: `opencode run -s <id> "<prompt>"`. No per-invocation
  turn-complete hook — tandem fs-watches the `-wal` file.
```

And in `src/tandem/memory_sync.py:37`, a comment (no behavior change):

```python
# opencode reads AGENTS.md natively, same as codex — it shares codex's side
# of the merge, so this map deliberately stays two-file.
FILES = {"claude": "CLAUDE.md", "codex": "AGENTS.md"}
```

- [ ] **Step 4: Run the full suite**

Run: `python -m pytest tests/ -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "test: opencode oracle round-trips through the real binary; document the format"
```

---

## PR 2 merge gate (not tasks — release gates)

- Full suite green, including `tests/test_opencode.py`; oracle suite green
  on this machine (`python -m pytest tests/test_opencode_oracle.py -q`).

- Live-verify with the real TUI (spec items 2-5): quit-keystroke recipe,
  `opencode -s` rendering of a tandem-minted session (sentinel display, no
  "working" state), WAL fs-watch debounce under a busy turn, flip-gate
  probe latency. Fix-ups land as follow-up commits.
- A real three-way session end-to-end on this machine: `tandem`, one turn
  in claude, Ctrl-] to codex, one turn, Ctrl-] to opencode, verify both
  prior turns render, one turn there, Ctrl-] back to claude.
- README/docs mention of the third harness; PyPI release notes.
