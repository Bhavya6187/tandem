# Opencode Third Harness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add opencode as a third full-peer harness — transcript sync both ways, a slot in the Ctrl-] cycle, launch/resume/one-off — by generalizing tandem's two-harness core to N participants and writing a SQLite-backed opencode adapter.

**Architecture:** Phase 1 generalizes the core (participants list, per-direction sync cursors, N-slot bar, cycle flip) while keeping claude↔codex behavior byte-for-byte identical — the existing suite is the regression net. Phase 2 adds `harness/opencode.py`: reads/writes opencode's `opencode.db` (WAL SQLite) directly for incremental sync, delegates session creation to `opencode import`, and consumes completed turns as single native objects.

**Tech Stack:** Python 3.11+ stdlib only (`sqlite3`, `tomllib`, `subprocess`); pytest; no new dependencies.

**Spec:** `docs/specs/2026-08-13-opencode-harness-design.md` (read it first; the plan argues from it)

## Global Constraints

- Branch: `opencode-harness`. Commit after every task with the message given in the task.
- After every task the FULL suite must pass: `python -m pytest tests/ -q`. Phase 1 tasks must not change claude↔codex observable behavior.
- Opencode compat floor: `tested="1.18.15"`, `min_version=(1, 18)`, **no ceiling** (`max_exclusive=None` = unbounded).
- Not-installed is silent: an uninstalled harness gets exactly one PATH lookup and zero other probes, warnings, or state. Installed-but-broken warns and is dropped (fail closed).
- No state-DB migration shims: schema mismatch ⇒ move the old DB aside and recreate.
- Sentinel provider for tandem-authored opencode rows: `providerID: "tandem"`, `modelID: "<synced>"`.
- Echo suppression is cursor-based (fast-forward); the sentinel is attribution + parse-time backstop only.
- No reasoning content in either direction (`Thinking` carries no text).
- Tandem never constructs opencode `session` rows by hand — `opencode import` does (shadow birth only; incremental sync is direct SQL).
- All SQLite access to opencode's DB: read-write connection, `busy_timeout=5000`, `foreign_keys=ON`, one connection per thread, `BEGIN IMMEDIATE` for writes.

## File Structure

| File | Responsibility |
|---|---|
| `src/tandem/compat.py` | optional `max_exclusive`; `COMPAT["opencode"]` |
| `src/tandem/constants.py` | `ATTRIBUTION["opencode"]` |
| `src/tandem/state.py` | `participants` + `native_session_ids` JSON columns; cursors keyed `(tandem_id, source, target)`; recreate-on-mismatch |
| `src/tandem/events.py` | `SessionContext` with `direction: str`, `harness_state` namespace, `source/target_session_id` |
| `src/tandem/harness/__init__.py` | `other()` deleted; ADAPTERS gains opencode |
| `src/tandem/harness/base.py` | storage-capability methods with JSONL defaults; `validate_transcript`; `runtime_ready` |
| `src/tandem/harness/claude_code.py` | `harness_state["claude"]` scratch; entry validation moves in |
| `src/tandem/harness/codex.py` | same mechanical updates |
| `src/tandem/harness/opencode.py` | NEW — the whole opencode adapter (ids, DB, reader, writer, import birth) |
| `src/tandem/sync.py` | `SyncEngine(store, session, source, target)`; adapter-resolved shadow; adapter append/intent hooks |
| `src/tandem/ops.py` | fan-out drain; per-direction fast-forward; `switch_session(to=)`; per-direction one-off echo |
| `src/tandem/runner.py` | `ctx_from_cursor(session, cursor)`; one TailLoop per direction; capability-checked status probe; warm = next-in-cycle with opencode carve-out |
| `src/tandem/flip.py` | visited-set failure ladder |
| `src/tandem/frame.py` | `StatusBar(..., others: list[str])` |
| `src/tandem/ptyrun.py` | `FrameIO.others: list[str]` |
| `src/tandem/config.py` | `load_harnesses()` (top-level `harnesses` key) |
| `src/tandem/cli.py` | participant resolution; N-ary `_pair_session`; resume narrowing; choices updated |
| `src/tandem/doctor.py` | participant loops; dispatch to adapter validators; opencode section |
| `src/tandem/toolmap.py` | target-keyed mapper registry, passthrough default |
| `src/tandem/tailer.py` | `TailedLine.advance(cursor)` + optional `pos` |
| `src/tandem/paths.py` | (no opencode paths — discovery lives in the adapter) |
| `tests/test_state.py`, `tests/test_sync.py`, `tests/test_ops.py`, … | updated signatures |
| `tests/test_participants.py` | NEW — resolution/resume/cycle tests |
| `tests/test_opencode.py` | NEW — adapter unit/golden tests (hermetic mini-DB) |
| `tests/test_opencode_oracle.py` | NEW — real-binary round-trips, skip-if-missing |

**Naming note (conscious deviation from the spec's `Session.participants` phrasing):** the dataclass keeps its `PairedSession` name to avoid a codebase-wide rename with zero behavior value; it gains `participants`/`native_session_ids` fields and the cycle helpers.

**Conscious deviations from the spec's code-model bullets** (all in the spirit of the spec's own "minimal blast radius" rule; flag during review if unwanted):
1. Codex rollout discovery (`runner.py:405-429,613-628`) stays inline in the runner rather than moving behind the adapter — the block only executes when codex is the active harness (`transcript is None` never happens for claude or opencode), so it cannot misfire at N=3.
2. Late-shadow creation (`ops.py:111-147`) keeps its two name-scoped blocks (`codex` id pending, `claude` zero-turn file) — each fires only for its own harness; opencode never needs one (its session is pre-created at pair time).
3. Tandem-written opencode user rows always carry the sentinel `agent`/`model` rather than "the session's values if present" — the renderer would otherwise need a DB read per turn for a display-only nicety.

---

## Phase 1 — N-harness core (claude↔codex behavior frozen)

### Task 1: Compat floor-only + registry entries

**Files:**
- Modify: `src/tandem/compat.py`
- Modify: `src/tandem/constants.py`
- Test: `tests/test_paths_compat.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `CompatRange.max_exclusive: tuple[int, ...] | None` (None = unbounded); `COMPAT["opencode"]`; `ATTRIBUTION["opencode"] == "[via opencode]"`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_paths_compat.py`)

```python
from tandem import compat
from tandem.constants import ATTRIBUTION


def test_opencode_compat_floor_only():
    assert compat.version_supported("opencode", "1.18.15")
    assert compat.version_supported("opencode", "9.9.9")      # no ceiling
    assert not compat.version_supported("opencode", "1.17.0")  # below floor


def test_existing_ranges_unchanged():
    assert compat.version_supported("claude", "2.1.220")
    assert not compat.version_supported("codex", "0.150.0")


def test_opencode_attribution_tag():
    assert ATTRIBUTION["opencode"] == "[via opencode]"
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_paths_compat.py -q`
Expected: FAIL — `KeyError: 'opencode'`

- [ ] **Step 3: Implement**

In `src/tandem/compat.py` change the dataclass and table (`compat.py:17-27`):

```python
@dataclass(frozen=True)
class CompatRange:
    tested: str          # exact version this code was developed against
    min_version: tuple[int, ...]
    max_exclusive: tuple[int, ...] | None = None   # None = no ceiling


COMPAT: dict[str, CompatRange] = {
    "claude": CompatRange(tested="2.1.220", min_version=(2, 0), max_exclusive=(3,)),
    "codex": CompatRange(tested="0.145.0", min_version=(0, 140), max_exclusive=(0, 150)),
    # Floor-only by operator decision (spec: Compat gate). Pre-1.18 opencode
    # predates SQLite session storage and genuinely cannot work.
    "opencode": CompatRange(tested="1.18.15", min_version=(1, 18)),
}
```

And in `version_supported` (`compat.py:57-62`):

```python
def version_supported(harness: str, version_text: str) -> bool:
    rng = COMPAT[harness]
    v = parse_version(version_text)
    if v is None:
        return False
    if rng.max_exclusive is not None and v >= rng.max_exclusive:
        return False
    return rng.min_version <= v
```

In `src/tandem/constants.py` add to `ATTRIBUTION`:

```python
ATTRIBUTION = {
    "claude": "[via claude-code]",
    "codex": "[via codex]",
    "opencode": "[via opencode]",
    "tandem": "[tandem]",
    "user": "",
}
```

- [ ] **Step 4: Run the new tests, then the full suite**

Run: `python -m pytest tests/test_paths_compat.py -q && python -m pytest tests/ -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/tandem/compat.py src/tandem/constants.py tests/test_paths_compat.py
git commit -m "feat: floor-only compat ranges + opencode registry entries"
```

---

### Task 2: State schema v2 — participants, native ids, per-direction cursors

**Files:**
- Modify: `src/tandem/state.py` (full rewrite of schema + dataclasses)
- Modify (mechanical call sites): `src/tandem/ops.py`, `src/tandem/runner.py`, `src/tandem/sync.py`, `src/tandem/warm.py`, `src/tandem/cli.py`, `src/tandem/doctor.py`, `tests/conftest.py`
- Test: `tests/test_state.py`

**Interfaces:**
- Produces:
  - `PairedSession(tandem_id, cwd, active, participants: list[str], native_session_ids: dict[str, str | None], created_at, last_sync_at, last_used_at)`
  - `PairedSession.native_id(harness: str) -> str | None`
  - `PairedSession.targets_for(source: str) -> list[str]` — participants minus source, list order
  - `PairedSession.next_active(current: str) -> str` — next in cycle order
  - `PairedSession.shadow` property KEPT for now (returns `targets_for(active)[0]`) — deleted in Task 4
  - `StateStore.create_session(cwd, active, participants, native_session_ids) -> PairedSession`
  - `StateStore.set_participants(tandem_id, participants: list[str]) -> None`
  - `StateStore.get_cursor(tandem_id, source, target) -> SyncCursor` / `SyncCursor.target: str`
- The DB file is moved aside (renamed `state.db.old`) when the `sessions` table lacks a `participants` column.

- [ ] **Step 1: Write the failing tests** (replace the affected tests in `tests/test_state.py`; keep unrelated ones)

```python
def test_create_session_with_participants(tmp_path):
    with StateStore(db_path=tmp_path / "s.db") as store:
        s = store.create_session(
            "/proj", "claude", ["claude", "codex"],
            {"claude": "c-id", "codex": "x-id"},
        )
        assert s.participants == ["claude", "codex"]
        assert s.native_id("claude") == "c-id"
        assert s.shadow == "codex"
        assert s.targets_for("claude") == ["codex"]
        assert s.next_active("claude") == "codex"
        found = store.get_session(s.tandem_id)
        assert found.native_id("codex") == "x-id"


def test_three_way_cycle(tmp_path):
    with StateStore(db_path=tmp_path / "s.db") as store:
        s = store.create_session(
            "/proj", "claude", ["claude", "codex", "opencode"],
            {"claude": "c", "codex": None, "opencode": "ses_x"},
        )
        assert s.next_active("claude") == "codex"
        assert s.next_active("codex") == "opencode"
        assert s.next_active("opencode") == "claude"
        assert s.targets_for("codex") == ["claude", "opencode"]


def test_set_participants_persists(tmp_path):
    with StateStore(db_path=tmp_path / "s.db") as store:
        s = store.create_session(
            "/proj", "claude", ["claude", "codex", "opencode"],
            {"claude": "c", "codex": "x", "opencode": "o"},
        )
        store.set_participants(s.tandem_id, ["claude", "codex"])
        assert store.get_session(s.tandem_id).participants == ["claude", "codex"]


def test_cursor_keyed_by_direction(tmp_path):
    with StateStore(db_path=tmp_path / "s.db") as store:
        s = store.create_session("/proj", "claude", ["claude", "codex"],
                                 {"claude": "c", "codex": "x"})
        a = store.get_cursor(s.tandem_id, "claude", "codex")
        a.byte_offset = 10
        store.save_cursor(a)
        b = store.get_cursor(s.tandem_id, "claude", "opencode")
        assert b.byte_offset == 0          # independent direction
        assert store.get_cursor(s.tandem_id, "claude", "codex").byte_offset == 10


def test_old_schema_recreated(tmp_path):
    import sqlite3
    db = tmp_path / "s.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE sessions (tandem_id TEXT PRIMARY KEY, cwd TEXT,"
                 " active TEXT, claude_session_id TEXT, codex_session_id TEXT,"
                 " created_at TEXT)")
    conn.execute("INSERT INTO sessions VALUES ('old', '/p', 'claude', 'c', 'x', 't')")
    conn.commit()
    conn.close()
    with StateStore(db_path=db) as store:
        assert store.get_session("old") is None      # fresh DB
    assert (tmp_path / "s.db.old").exists()          # moved aside, not deleted
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_state.py -q`
Expected: FAIL — `create_session()` takes different arguments

- [ ] **Step 3: Implement `state.py`**

Replace `_SCHEMA` (`state.py:19-42`), the dataclasses (`state.py:49-77`), and the store methods:

```python
_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    tandem_id TEXT PRIMARY KEY,
    cwd TEXT NOT NULL,
    active TEXT NOT NULL,
    participants TEXT NOT NULL,
    native_session_ids TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    last_sync_at TEXT,
    last_used_at TEXT
);
CREATE TABLE IF NOT EXISTS sync_cursors (
    tandem_id TEXT NOT NULL,
    source TEXT NOT NULL,
    target TEXT NOT NULL,
    byte_offset INTEGER NOT NULL DEFAULT 0,
    line_index INTEGER NOT NULL DEFAULT 0,
    turn_index INTEGER NOT NULL DEFAULT 0,
    pending TEXT NOT NULL DEFAULT '{}',
    failed_turns INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT,
    PRIMARY KEY (tandem_id, source, target),
    FOREIGN KEY (tandem_id) REFERENCES sessions (tandem_id)
);
"""


@dataclass
class PairedSession:
    tandem_id: str
    cwd: str
    active: str
    participants: list[str]
    native_session_ids: dict[str, str | None]
    created_at: str
    last_sync_at: str | None
    last_used_at: str | None = None

    def native_id(self, harness: str) -> str | None:
        return self.native_session_ids.get(harness)

    def targets_for(self, source: str) -> list[str]:
        return [h for h in self.participants if h != source]

    def next_active(self, current: str) -> str:
        i = self.participants.index(current)
        return self.participants[(i + 1) % len(self.participants)]

    @property
    def shadow(self) -> str:
        # transitional N=2 helper; deleted in the fan-out task
        return self.targets_for(self.active)[0]


@dataclass
class SyncCursor:
    tandem_id: str
    source: str
    target: str
    byte_offset: int = 0
    line_index: int = 0
    turn_index: int = 0
    pending: dict = field(default_factory=dict)
    failed_turns: int = 0
    updated_at: str | None = None
```

`StateStore.__init__` — recreate on mismatch, no migration shims (replaces the
`last_used_at` ALTER block at `state.py:83-90`):

```python
    def __init__(self, db_path: Path | None = None):
        self.db_path = db_path or paths.state_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        if self._schema_stale():
            self._conn.close()
            self.db_path.rename(self.db_path.with_name(self.db_path.name + ".old"))
            self._conn = sqlite3.connect(self.db_path)
            self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def _schema_stale(self) -> bool:
        """A sessions table without the participants column is a pre-N-harness
        DB. No migration by design: move it aside and start fresh."""
        try:
            names = {r[1] for r in self._conn.execute("PRAGMA table_info(sessions)")}
        except sqlite3.Error:
            return False
        return bool(names) and "participants" not in names
```

`create_session`, row mapping, `set_native_session_id`, cursors:

```python
    def create_session(
        self,
        cwd: str,
        active: str,
        participants: list[str],
        native_session_ids: dict[str, str | None],
    ) -> PairedSession:
        tandem_id = uuid.uuid4().hex[:12]
        now = _now()
        with self._conn:
            self._conn.execute(
                "INSERT INTO sessions (tandem_id, cwd, active, participants,"
                " native_session_ids, created_at, last_used_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (tandem_id, cwd, active, json.dumps(participants),
                 json.dumps(native_session_ids), now, now),
            )
        return PairedSession(
            tandem_id, cwd, active, list(participants),
            dict(native_session_ids), now, None, now,
        )

    def _row_to_session(self, row: sqlite3.Row) -> PairedSession:
        return PairedSession(
            tandem_id=row["tandem_id"],
            cwd=row["cwd"],
            active=row["active"],
            participants=json.loads(row["participants"]),
            native_session_ids=json.loads(row["native_session_ids"]),
            created_at=row["created_at"],
            last_sync_at=row["last_sync_at"],
            last_used_at=row["last_used_at"],
        )

    def set_participants(self, tandem_id: str, participants: list[str]) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE sessions SET participants = ? WHERE tandem_id = ?",
                (json.dumps(participants), tandem_id),
            )

    def set_native_session_id(self, tandem_id: str, harness: str, session_id: str) -> None:
        session = self.get_session(tandem_id)
        ids = dict(session.native_session_ids) if session else {}
        ids[harness] = session_id
        with self._conn:
            self._conn.execute(
                "UPDATE sessions SET native_session_ids = ? WHERE tandem_id = ?",
                (json.dumps(ids), tandem_id),
            )

    def get_cursor(self, tandem_id: str, source: str, target: str) -> SyncCursor:
        row = self._conn.execute(
            "SELECT * FROM sync_cursors WHERE tandem_id = ? AND source = ?"
            " AND target = ?",
            (tandem_id, source, target),
        ).fetchone()
        if row is None:
            return SyncCursor(tandem_id=tandem_id, source=source, target=target)
        return SyncCursor(
            tandem_id=row["tandem_id"], source=row["source"], target=row["target"],
            byte_offset=row["byte_offset"], line_index=row["line_index"],
            turn_index=row["turn_index"], pending=json.loads(row["pending"]),
            failed_turns=row["failed_turns"], updated_at=row["updated_at"],
        )

    def save_cursor(self, cursor: SyncCursor) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO sync_cursors (tandem_id, source, target, byte_offset,"
                " line_index, turn_index, pending, failed_turns, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (tandem_id, source, target) DO UPDATE SET"
                " byte_offset = excluded.byte_offset,"
                " line_index = excluded.line_index,"
                " turn_index = excluded.turn_index,"
                " pending = excluded.pending,"
                " failed_turns = excluded.failed_turns,"
                " updated_at = excluded.updated_at",
                (cursor.tandem_id, cursor.source, cursor.target,
                 cursor.byte_offset, cursor.line_index, cursor.turn_index,
                 json.dumps(cursor.pending), cursor.failed_turns, _now()),
            )
```

- [ ] **Step 4: Update every call site mechanically.** All are behavior-preserving; supply the missing `target` with the transitional `other(source)` (still exists until Task 4) and replace attribute access with `native_id()`:

| Site | Old | New |
|---|---|---|
| `ops.py:38` (`source_transcript`) | `getattr(session, f"{source}_session_id")` | `session.native_id(source)` |
| `ops.py:75,91` (`fast_forward`, `unsynced_lines`) | `store.get_cursor(session.tandem_id, source)` | `store.get_cursor(session.tandem_id, source, other(source))` |
| `ops.py:111` | `session.codex_session_id` | `session.native_id("codex")` |
| `ops.py:121-125` | `session.claude_session_id` ×2 | `session.native_id("claude")` |
| `ops.py:141` (`validate_transcript` arg) | `getattr(session, f"{new_active}_session_id")` | `session.native_id(new_active)` |
| `ops.py:156,175` (`_create_*_shadow_late`) | `store.get_cursor(session.tandem_id, session.active)` | `store.get_cursor(session.tandem_id, session.active, session.shadow)` |
| `ops.py:164` | `session.claude_session_id` | `session.native_id("claude")` |
| `ops.py:202` (`run_oneoff`) | `getattr(session, f"{target}_session_id")` | `session.native_id(target)` |
| `ops.py:216` | `store.get_cursor(session.tandem_id, "codex")` | `store.get_cursor(session.tandem_id, "codex", "claude")` |
| `ops.py:228` | `store.get_cursor(session.tandem_id, echo_side)` | `store.get_cursor(session.tandem_id, echo_side, target)` |
| `ops.py:272` (`fork_shadow`) | `session.codex_session_id` | `session.native_id("codex")` |
| `runner.py:400` (`TailLoop.__init__`) | `store.get_cursor(session.tandem_id, source)` | `store.get_cursor(session.tandem_id, source, other(source))` (add `from .harness import other` import; replaced again in Task 6) |
| `runner.py:528` | `getattr(session, f"{active}_session_id")` | `session.native_id(active)` |
| `runner.py:74-75` (`ctx_from_cursor`) | `session.claude_session_id` / `session.codex_session_id` | `session.native_id("claude")` / `session.native_id("codex")` |
| `sync.py:62-75` | `session.claude_session_id` / `session.codex_session_id` | `session.native_id("claude")` / `session.native_id("codex")` |
| `warm.py:52,75` | `getattr(session, f"{side}_session_id")` | `session.native_id(side)` |
| `doctor.py:181` | `getattr(session, f"{hid}_session_id")` | `session.native_id(hid)` |
| `doctor.py:204` | `store.get_cursor(session.tandem_id, source)` | `store.get_cursor(session.tandem_id, source, other(source))` |
| `doctor.py:306,311` | `session.claude_session_id` / `session.codex_session_id` | `session.native_id("claude")` / `session.native_id("codex")` |
| `cli.py:87` (`_pair_session`) | `store.create_session(cwd, active, claude_sid, codex_sid)` | `store.create_session(cwd, active, ["claude", "codex"], {"claude": claude_sid, "codex": codex_sid})` |
| `cli.py:111` | `store.get_cursor(session.tandem_id, active)` | `store.get_cursor(session.tandem_id, active, shadow)` |
| `cli.py:141` | `getattr(session, f"{hid}_session_id")` | `session.native_id(hid)` |
| `cli.py:151` | `store.get_cursor(session.tandem_id, source)` | `store.get_cursor(session.tandem_id, source, other(source))` |
| `tests/conftest.py:105-106` (`Env.__init__`) | `create_session(self.cwd, active, claude_sid, codex_sid)` | `create_session(self.cwd, active, ["claude", "codex"], {"claude": claude_sid, "codex": codex_sid})` |
| `tests/conftest.py:114` | `get_cursor(self.session.tandem_id, "codex")` | `get_cursor(self.session.tandem_id, "codex", "claude")` |

Then sweep the test files: `grep -rn "get_cursor(\|claude_session_id\|codex_session_id\|create_session(" tests/` and apply the same two rewrites everywhere (`get_cursor(tid, src)` → `get_cursor(tid, src, <the other name>)`; `session.claude_session_id` → `session.native_id("claude")`; `create_session(cwd, active, c, x)` → the participants form). `tests/test_state.py:100-140` contains an old-schema fixture block — replace it with the `test_old_schema_recreated` test from Step 1.

- [ ] **Step 5: Run the full suite**

Run: `python -m pytest tests/ -q`
Expected: PASS (identical behavior at N=2)

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "feat: state schema v2 — participants list, native-id map, per-direction cursors"
```

---

### Task 3: SessionContext generalization

**Files:**
- Modify: `src/tandem/events.py:15,84-107`, `src/tandem/converter.py:33,45,62,129`, `src/tandem/runner.py:62-88`, `src/tandem/sync.py:105-115,175-180`, `src/tandem/harness/claude_code.py`, `src/tandem/cli.py:89-95`, `src/tandem/ops.py:150-182`, `tests/conftest.py`, `tests/test_parse.py`, `tests/test_converter.py`, `tests/test_toolmap.py`
- Test: `tests/test_parse.py`

**Interfaces:**
- Produces (consumed by every adapter and by Tasks 4/6/11/12):
  - `SessionContext.direction: str` validated as `"<source>-><target>"` over known adapter ids
  - `SessionContext.source_id` / `SessionContext.target_id` properties (split of `direction`)
  - `SessionContext.source_session_id: str | None`, `SessionContext.target_session_id: str | None`
  - `SessionContext.harness_state: dict[str, dict[str, Any]]` + `state_for(harness_id) -> dict` (setdefault)
  - `Agent = str` (alias kept for annotations)
  - `runner.ctx_from_cursor(session, cursor) -> SessionContext` (derives source/target from the cursor row)
- Removed: `claude_leaf_uuid`, `claude_run_msg_id`, `claude_model`, `claude_session_id`, `codex_session_id` fields.
- Claude scratch keys inside `harness_state["claude"]`: `"leaf_uuid"`, `"run_msg_id"`, `"model"`.

- [ ] **Step 1: Write the failing tests** (update `tests/test_parse.py`'s ctx fixture and add)

```python
import pytest
from tandem.events import SessionContext


def _ctx(direction="claude->codex"):
    return SessionContext(
        tandem_id="t1", cwd="/proj", direction=direction,
        source_session_id="src-id", target_session_id="tgt-id",
    )


def test_direction_split_properties():
    ctx = _ctx("codex->opencode")
    assert ctx.source_id == "codex"
    assert ctx.target_id == "opencode"


def test_direction_validated():
    with pytest.raises(ValueError):
        _ctx("claude->claude")
    with pytest.raises(ValueError):
        _ctx("claude->gemini")
    with pytest.raises(ValueError):
        _ctx("nonsense")


def test_harness_state_namespace_roundtrip():
    ctx = _ctx()
    ctx.state_for("claude")["leaf_uuid"] = "u-1"
    assert ctx.harness_state == {"claude": {"leaf_uuid": "u-1"}}
    assert ctx.state_for("claude")["leaf_uuid"] == "u-1"
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_parse.py -q`
Expected: FAIL — unexpected keyword `source_session_id`

- [ ] **Step 3: Implement `events.py`**

Replace `Agent` (`events.py:15`) and `SessionContext` (`events.py:84-107`):

```python
Agent = str  # harness id, "tandem", or "user"


class SessionContext(BaseModel):
    """Mutable per-direction translation context, persisted in the sync
    cursor so a restart resumes mid-turn without losing tool-call pairings."""

    model_config = ConfigDict(extra="forbid")

    tandem_id: str
    cwd: str
    direction: str  # "<source>-><target>" over registered adapter ids
    turn_index: int = 0
    # call_id -> serialized ToolCall event awaiting its result
    pending_calls: dict[str, dict[str, Any]] = Field(default_factory=dict)
    # per-adapter renderer scratch keyed by harness id (claude: leaf_uuid /
    # run_msg_id / model — see ClaudeCodeAdapter); persisted with the cursor
    harness_state: dict[str, dict[str, Any]] = Field(default_factory=dict)
    source_session_id: str | None = None
    target_session_id: str | None = None

    @field_validator("direction")
    @classmethod
    def _check_direction(cls, v: str) -> str:
        from .harness import ADAPTERS

        parts = v.split("->")
        if (len(parts) != 2 or parts[0] == parts[1]
                or any(p not in ADAPTERS for p in parts)):
            raise ValueError(f"not a harness direction: {v!r}")
        return v

    @property
    def source_id(self) -> str:
        return self.direction.split("->")[0]

    @property
    def target_id(self) -> str:
        return self.direction.split("->")[1]

    def state_for(self, harness_id: str) -> dict[str, Any]:
        return self.harness_state.setdefault(harness_id, {})
```

Add `field_validator` to the pydantic import at `events.py:13`. (The import of `ADAPTERS` is deferred inside the validator to avoid a module cycle: `harness/*` imports `events`.)

- [ ] **Step 4: Update the users of the removed fields**

`src/tandem/harness/claude_code.py` — every claude scratch access goes through `ctx.state_for("claude")`:
- `_base_entry` (`claude_code.py:100-110`): `"parentUuid": ctx.state_for("claude").get("leaf_uuid")` and `"sessionId": ctx.target_session_id` (the claude adapter renders only as target or shadow-birth target).
- `create_shadow_transcript` (`:119`): `ctx.state_for("claude")["leaf_uuid"] = entry["uuid"]`.
- `render_events` (`:289,319`): `st = ctx.state_for("claude")`; `st["run_msg_id"] = None` where `claude_run_msg_id` was cleared; `st["leaf_uuid"] = entry["uuid"]` where assigned.
- `_assistant_message` (`:327-331`): `st = ctx.state_for("claude")`; `if st.get("run_msg_id") is None: st["run_msg_id"] = f"msg_tandem_{entry['uuid'][:8]}"`; model line becomes `"model": st.get("model") or "<synced>"`.
- `render_placeholder` (`:342-345`): same two substitutions.

`src/tandem/runner.py:62-88` — per-direction ctx:

```python
def ctx_from_cursor(session: PairedSession, cursor: SyncCursor) -> SessionContext:
    pending = dict(cursor.pending)
    state = pending.pop("harness_state", {})
    # legacy key from the pre-namespace cursor layout; fold it in
    leaf = pending.pop("claude_leaf_uuid", None)
    if leaf is not None:
        state.setdefault("claude", {})["leaf_uuid"] = leaf
    calls = pending.pop("pending_calls", {})
    return SessionContext(
        tandem_id=session.tandem_id,
        cwd=session.cwd,
        direction=f"{cursor.source}->{cursor.target}",
        turn_index=cursor.turn_index,
        pending_calls=calls,
        harness_state=state,
        source_session_id=session.native_id(cursor.source),
        target_session_id=session.native_id(cursor.target),
    )


def ctx_to_cursor(ctx: SessionContext, cursor: SyncCursor) -> None:
    cursor.turn_index = ctx.turn_index
    cursor.pending.update(
        {
            "pending_calls": ctx.pending_calls,
            "harness_state": ctx.harness_state,
        }
    )
```

All `ctx_from_cursor(session, source, cursor)` callers drop the `source` argument (`runner.py:401`, `ops.py:157,176`). In `ops._create_claude_shadow_late` (`ops.py:150-166`): `ctx.state_for("claude")["leaf_uuid"] = None` replaces the `claude_leaf_uuid = None` reset, and the cursor stash becomes `cursor.pending.setdefault("harness_state", {}).setdefault("claude", {})["leaf_uuid"] = ctx.state_for("claude").get("leaf_uuid")`. In `ops._create_codex_shadow_late` (`ops.py:177`): `ctx.codex_session_id = sid` becomes `ctx.target_session_id = sid`.

`src/tandem/sync.py` claude repair sites (`:105-115` and `:175-180`): `ctx.claude_leaf_uuid = leaf` → `ctx.state_for("claude")["leaf_uuid"] = leaf`; `ctx.claude_model = model` → `ctx.state_for("claude")["model"] = model`.

`src/tandem/converter.py`: delete the `Direction` Literal (`:33`); `direction: Direction` annotations become `direction: str` (`:45,62`); the module keeps working otherwise.

`src/tandem/cli.py:89-95` (`_pair_session` ctx): build with the new fields —

```python
    ctx = SessionContext(
        tandem_id=session.tandem_id,
        cwd=cwd,
        direction=f"{active}->{shadow}",
        source_session_id=session.native_id(active),
        target_session_id=session.native_id(shadow),
    )
```
and the cursor stash at `cli.py:107-113` becomes
`cursor.pending["harness_state"] = ctx.harness_state` (both branches; drop the claude/codex if/else around `cursor_updates`).

`tests/conftest.py` Env ctx (`:96-112`): same SessionContext form (`direction="claude->codex"`, `source_session_id=claude_sid`, `target_session_id=codex_sid`); the `cur.pending["claude_leaf_uuid"] = ...` line becomes `cur.pending["harness_state"] = ctx.harness_state`. Sweep remaining test fixtures: `grep -rn "claude_session_id=\|codex_session_id=\|claude_leaf_uuid" tests/` and apply the same substitutions (`tests/test_parse.py:24-25`, `tests/test_converter.py:21-22,135` — the assertion there becomes `ctx.target_session_id`, `tests/test_toolmap.py:42-43`).

- [ ] **Step 5: Run the full suite**

Run: `python -m pytest tests/ -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "feat: per-direction SessionContext with harness_state namespace"
```

---

### Task 4: Sync fan-out — explicit targets, per-direction ops, other() deleted

**Files:**
- Modify: `src/tandem/sync.py:43-115`, `src/tandem/ops.py:36-260`, `src/tandem/harness/__init__.py`, `src/tandem/converter.py:99`, `src/tandem/state.py` (drop the `shadow` property)
- Test: `tests/test_ops.py`, `tests/test_sync.py`

**Interfaces:**
- Produces:
  - `SyncEngine(store, session, source, target, converter=None)` — explicit target; shadow resolved via `get_adapter(target).transcript_path(session.cwd, session.native_id(target))`
  - `ops.drain_source(store, session, source, *, flush_dangling=False) -> int` — now drains into **every** target in `session.targets_for(source)` (one engine+loop per direction; sum of lines consumed returned; per-direction cursors make progress independent)
  - `ops.fast_forward(store, session, source, target) -> None`
  - `ops.fast_forward_all(store, session, source) -> None` — every outgoing direction of `source`
  - `ops.switch_session(store, session, to: str | None = None)` — target defaults to `session.next_active(session.active)`; step 2 is `fast_forward_all(new_active)`
  - `toolmap.map_pair(call, result, target)` keeps its signature; `converter.py:99` passes `ctx.target_id` instead of `other(source_id)`
- Removed: `harness.other()`, `PairedSession.shadow`.

- [ ] **Step 1: Write the failing tests** (add to `tests/test_ops.py`)

```python
def test_drain_fans_out_to_all_targets(tmp_path, monkeypatch):
    """A 3-participant session drains one claude line into BOTH shadows."""
    env = Env3(tmp_path, monkeypatch)          # helper added below
    write_line(env.claude_active, claude_user("hello from claude"))
    n = ops.drain_source(env.store, env.session, "claude")
    assert n >= 1
    assert any("hello from claude" in t for t in shadow_texts(env.codex_shadow))
    assert any("hello from claude" in t
               for t in env.opencode_texts())    # fake adapter, see helper


def test_fast_forward_all_outgoing(tmp_path, monkeypatch):
    env = Env3(tmp_path, monkeypatch)
    write_line(env.claude_active, claude_user("pre-existing"))
    ops.fast_forward_all(env.store, env.session, "claude")
    for target in ("codex", "opencode"):
        cur = env.store.get_cursor(env.session.tandem_id, "claude", target)
        assert cur.byte_offset == env.claude_active.stat().st_size
    # nothing flows after the fast-forward
    assert ops.drain_source(env.store, env.session, "claude") == 0


def test_switch_session_explicit_target(tmp_path, monkeypatch):
    env = Env3(tmp_path, monkeypatch)
    new_active, problems, mem = ops.switch_session(
        env.store, env.session, to="opencode")
    assert new_active == "opencode"
    assert env.store.get_session(env.session.tandem_id).active == "opencode"


def test_switch_session_default_is_next_in_cycle(tmp_path, monkeypatch):
    env = Env3(tmp_path, monkeypatch)          # active=claude
    new_active, _, _ = ops.switch_session(env.store, env.session)
    assert new_active == "codex"
```

`Env3` goes in `tests/conftest.py`. Until the real opencode adapter exists (Phase 2), it registers a minimal in-memory fake so the fan-out is testable now:

```python
class FakeOpencodeAdapter:
    """Minimal file-backed stand-in registered as 'opencode' for core tests.
    Replaced by the real adapter's tests in Phase 2; core tests keep using
    the fake so they stay hermetic."""
    id = "opencode"
    display_name = "opencode"
    binary = "opencode"

    def __init__(self, root: Path):
        self.root = root

    def transcript_path(self, cwd, session_id):
        p = self.root / f"{session_id}.jsonl"
        return p if p.exists() else None

    def mint_session_id(self):
        return "ses_fake000000000000000000000"

    def create_shadow_transcript(self, cwd, session_id, ctx, note):
        p = self.root / f"{session_id}.jsonl"
        write_line(p, {"type": "note", "text": note})
        return p

    def parse_entry(self, raw, ctx):
        from tandem.events import SystemEvent
        return [SystemEvent(source="opencode", subtype="fake")]

    def render_events(self, events, ctx):
        return [{"type": e.kind, "text": getattr(e, "text", "")} for e in events]

    def render_placeholder(self, text, ctx):
        return [{"type": "note", "text": text}]

    def detect_version(self):
        return "1.18.15"

    def version_supported(self, v):
        return True

    def interactive_argv(self, session_id, fresh):
        return ["opencode", "-s", session_id]

    def oneoff_argv(self, session_id, prompt):
        return ["opencode", "run", "-s", session_id, prompt]

    def hook_argv_extra(self, sentinel):
        return []

    def quit_keystrokes(self):
        return []


class Env3(Env):
    """Env plus a third 'opencode' participant backed by FakeOpencodeAdapter."""

    def __init__(self, tmp_path, monkeypatch, active="claude"):
        from tandem import harness

        fake = FakeOpencodeAdapter(tmp_path / "oc")
        (tmp_path / "oc").mkdir()
        monkeypatch.setitem(harness.ADAPTERS, "opencode", fake)
        super().__init__(tmp_path, monkeypatch, active=active)
        # rebuild the session as a 3-way (Env made a 2-way)
        oc_sid = fake.mint_session_id()
        self.session = self.store.create_session(
            self.cwd, active, ["claude", "codex", "opencode"],
            {"claude": self.session.native_id("claude"),
             "codex": self.session.native_id("codex"),
             "opencode": oc_sid},
        )
        from tandem.events import SessionContext
        ctx = SessionContext(tandem_id=self.session.tandem_id, cwd=self.cwd,
                             direction=f"{active}->opencode",
                             target_session_id=oc_sid)
        self.oc_shadow = fake.create_shadow_transcript(self.cwd, oc_sid, ctx, "[tandem] seed")
        # the claude "active" transcript some tests append to
        self.claude_active = self.source_file

    def opencode_texts(self):
        return [e.get("text", "") for e in read_jsonl(self.oc_shadow)]
```

(`Env` seeds shadows for claude and codex already; `Env3` recreates the session row as 3-way and seeds the fake's file. Tests that drain "claude" as source pass `transcript=env.claude_active` via `ops.source_transcript` monkeypatching if needed — instead, simplest: `Env3` also monkeypatches `tandem.ops.source_transcript` is NOT needed because `drain_source` looks up the claude transcript by native id; write the claude entries into `env.claude_shadow` instead of `env.source_file`. Use `env.claude_shadow` as the claude source file in these tests — it exists and is registered.)

Adjust the two tests above accordingly: replace `env.claude_active` with `env.claude_shadow`.

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_ops.py -q`
Expected: FAIL — `drain_source` writes only one shadow / `switch_session` lacks `to=`

- [ ] **Step 3: Implement**

`src/tandem/sync.py` — explicit target (`sync.py:46-77`):

```python
    def __init__(
        self,
        store: StateStore,
        session: PairedSession,
        source: str,
        target: str,
        converter: TraceConverter | None = None,
    ):
        self.store = store
        self.session = session
        self.source = source
        self.target_id = target
        self.direction = f"{source}->{target}"
        self.converter: TraceConverter = converter or ReferenceConverter()
        self.target = get_adapter(target)

        sid = session.native_id(target)
        if not sid:
            raise SyncSetupError(f"no {target} session id recorded yet")
        found = self.target.transcript_path(session.cwd, sid)
        if found is None or not found.exists():
            raise SyncSetupError(f"shadow transcript missing for {target} ({sid})")
        self.shadow_path = found
```

(The claude/codex name-branch dies; `ClaudeCodeAdapter.transcript_path` already returns None for a missing file, producing the same `SyncSetupError` path as before.)

`src/tandem/ops.py` — fan-out drain and per-direction fast-forward (replaces `ops.py:44-98`):

```python
def drain_source(
    store: StateStore, session: PairedSession, source: str,
    *, flush_dangling: bool = False,
) -> int:
    """Translate any unsynced tail of `source` into EVERY other participant.
    One engine+loop per (source, target) direction; per-direction cursors
    keep progress independent. Returns total lines consumed across targets."""
    transcript = source_transcript(session, source)
    if transcript is None:
        return 0
    total = 0
    for target in session.targets_for(source):
        engine = SyncEngine(store, session, source, target)
        loop = TailLoop(store, session, source, target, transcript, engine)
        while True:
            n = loop.drain()
            total += n
            if n == 0:
                break
        if loop.errors:
            raise SyncSetupError("; ".join(loop.errors))
        if flush_dangling:
            engine.flush_dangling(loop.ctx, loop.cursor)
    return total


def fast_forward(store: StateStore, session: PairedSession, source: str,
                 target: str) -> None:
    """Mark everything currently in `source`'s store as already-synced for
    the (source -> target) direction."""
    transcript = source_transcript(session, source)
    cursor = store.get_cursor(session.tandem_id, source, target)
    if transcript is None:
        cursor.byte_offset = 0
        cursor.line_index = 0
    else:
        data = transcript.read_bytes()
        cursor.byte_offset = len(data)
        cursor.line_index = data.count(b"\n")
    cursor.pending.pop("intent", None)
    store.save_cursor(cursor)


def fast_forward_all(store: StateStore, session: PairedSession, source: str) -> None:
    """Every outgoing direction of `source` — the flip's anti-echo step.
    Only runtime-participant directions exist; cursors toward a dropped
    member are never created, advanced, or inspected."""
    for target in session.targets_for(source):
        fast_forward(store, session, source, target)
```

(`TailLoop` gains a `target` parameter in this task — minimal edit at `runner.py:388-406`: add `target: str` after `source`, and the cursor line becomes `store.get_cursor(session.tandem_id, source, target)`; the transitional `other()` import from Task 2 is removed.)

`unsynced_lines` (`ops.py:88-98`) takes an explicit `target` too; `cli.py:151-152` and `doctor.py:203-215` pass the first element of `session.targets_for(source)` for reporting (any direction shows the same source-side lag unless one target crashed mid-append — good enough for status, noted inline).

`switch_session` (`ops.py:101-148`) — explicit target + fast-forward-all:

```python
def switch_session(store: StateStore, session: PairedSession, to: str | None = None):
    """Flip active role to `to` (default: next in cycle). Drains the old
    active into every target, fast-forwards ALL outgoing directions of the
    new active (anti-echo), then records the switch."""
    from .doctor import validate_transcript

    old_active = session.active
    new_active = to or session.next_active(old_active)
    if new_active not in session.participants:
        raise SyncSetupError(f"{new_active} is not a participant")

    if new_active == "codex" and not session.native_id("codex"):
        _create_codex_shadow_late(store, session)
        session = store.get_session(session.tandem_id) or session

    if new_active == "claude" and session.native_id("claude"):
        expected = get_adapter("claude").expected_transcript_path(
            session.cwd, session.native_id("claude")
        )
        never_ran = all(
            store.get_cursor(session.tandem_id, "claude", t).byte_offset == 0
            for t in session.targets_for("claude")
        )
        if not expected.exists() and never_ran:
            _create_claude_shadow_late(store, session)

    drain_source(store, session, old_active, flush_dangling=True)
    fast_forward_all(store, session, new_active)
    store.set_active(session.tandem_id, new_active)
    ...  # memory sync + validation tail unchanged from ops.py:130-148,
         # with getattr(...) already replaced by session.native_id(new_active)
```

The `_create_*_shadow_late` helpers (`ops.py:150-182`) build their ctx from the `(active -> new_active)` cursor: `cursor = store.get_cursor(session.tandem_id, session.active, new_active)` — pass `new_active` in as a parameter (`_create_claude_shadow_late(store, session)` is only ever called with `new_active == "claude"`, so the direction is `(active -> "claude")` / `(active -> "codex")` respectively).

`run_oneoff` (`ops.py:185-241`) — the echo rule per direction. Replace the `echo_side = other(target)` block (`ops.py:225-240`) with a loop over every OTHER participant:

```python
    # Echo suppression per direction (module docstring): the drain below
    # appends tandem's translation of the target's turn into every other
    # participant's file. Each recipient that was fully synced before the
    # drain fast-forwards its own outgoing cursors past the copy.
    echo_pre: dict[str, tuple[int | None, dict[str, int]]] = {}
    for side in session.targets_for(target):
        size = _file_size(source_transcript(session, side))
        offsets = {
            t: store.get_cursor(session.tandem_id, side, t).byte_offset
            for t in session.targets_for(side)
        }
        echo_pre[side] = (size, offsets)

    drain_source(store, session, target, flush_dangling=True)

    for side, (pre_size, offsets) in echo_pre.items():
        if pre_size is None:
            continue
        if all(off == pre_size for off in offsets.values()):
            fast_forward_all(store, session, side)
    return code
```

`src/tandem/harness/__init__.py` — delete `other()`; `src/tandem/converter.py:99` — `toolmap.map_pair(call, ev, ctx.target_id)`; `src/tandem/state.py` — delete the transitional `shadow` property and fix its remaining readers: `ops.py:107` is gone with the rewrite, `runner.py:589,668` and `flip.py` still reference it — replace `session.shadow` with `session.next_active(session.active)` at `runner.py:589` and `runner.py:668` (full treatment of the runner comes in Task 6, but these two references must compile now); `sync.py`'s `from .harness import get_adapter, other` drops `other`; sweep `grep -rn "other(" src/ tests/`.

`tests/test_state.py:14` (`s.shadow == "codex"`) becomes `s.next_active("claude") == "codex"`.

- [ ] **Step 4: Run the full suite**

Run: `python -m pytest tests/ -q`
Expected: PASS — N=2 flows unchanged (one target in the loop), N=3 covered by the new tests

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat: sync fan-out — per-direction engines, explicit flip target, other() removed"
```

---

### Task 5: Frame — N-slot status bar

**Files:**
- Modify: `src/tandem/frame.py:222-254`, `src/tandem/ptyrun.py` (`FrameIO` dataclass + `StatusBar(...)` construction at `ptyrun.py:209`)
- Test: `tests/test_frame.py`

**Interfaces:**
- Produces: `StatusBar(rows, cols, active: str, others: list[str], key_label="^]")`; `FrameIO.others: list[str] = field(default_factory=list)` (the `other: str` field is deleted).
- Consumed by Task 6 (runner wiring).

- [ ] **Step 1: Write the failing tests** (add to `tests/test_frame.py`; update the existing `StatusBar(... other="codex")` constructions at `tests/test_frame.py:360-435` to `others=["codex"]`)

```python
def test_bar_three_slots():
    bar = StatusBar(rows=40, cols=80, active="claude",
                    others=["codex", "opencode"])
    line = bar.line(armed=False)
    assert " claude ● │ codex ○ │ opencode ○ " in line + " "
    assert "^] flips" in line


def test_bar_two_slots_unchanged():
    bar = StatusBar(rows=40, cols=60, active="claude", others=["codex"])
    assert bar.line(armed=False).startswith(" claude ● │ codex ○ ")


def test_bar_three_slots_truncates_to_cols():
    bar = StatusBar(rows=40, cols=24, active="claude",
                    others=["codex", "opencode"])
    assert len(bar.line(armed=False)) == 24
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_frame.py -q`
Expected: FAIL — unexpected keyword `others`

- [ ] **Step 3: Implement**

`src/tandem/frame.py` (`StatusBar.__init__` at `:228-238` and `line()` at `:244-254`):

```python
    def __init__(self, rows: int, cols: int, active: str, others: list[str],
                 key_label: str = "^]"):
        self.rows = rows
        self.cols = cols
        self.active = active
        self.others = list(others)
        self.key_label = key_label
```

```python
    def line(self, armed: bool) -> str:
        # (keep the existing one-cell-glyph comment at frame.py:245-249)
        if armed:
            text = f" {self.active} ◐ flipping at turn end…  {self.key_label} cancels"
        else:
            slots = " │ ".join([f"{self.active} ●"] + [f"{o} ○" for o in self.others])
            text = f" {slots}   {self.key_label} flips"
        return text[: self.cols].ljust(self.cols)
```

`src/tandem/ptyrun.py` — in the `FrameIO` dataclass replace `other: str = ""` with `others: list = None` is NOT allowed (mutable default); use:

```python
    others: list[str] = dataclasses.field(default_factory=list)
```

(`FrameIO` is a `@dataclass`; add `import dataclasses` or `from dataclasses import field` consistent with the file's existing imports.) At `ptyrun.py:209`:

```python
        StatusBar(rows, cols, frame.active, frame.others, frame.key_label)
```

`src/tandem/runner.py:662-670` (FrameIO construction): `other=session.shadow` → `others=session.targets_for(active)` (this replaces the transitional `next_active` reference from Task 4 at `runner.py:668`).

- [ ] **Step 4: Run the full suite**

Run: `python -m pytest tests/ -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat: N-slot status bar (others list through FrameIO)"
```

---

### Task 6: Runner fan-out — per-direction tail loops, capability-checked probe, warm carve-out

**Files:**
- Modify: `src/tandem/runner.py:384-424` (TailLoop), `runner.py:524-720` (InteractiveRunner)
- Test: `tests/test_runner.py`

**Interfaces:**
- Consumes: `TailLoop(store, session, source, target, transcript, sink)` (Task 4), `SinkFactory` (below), `session.targets_for/next_active`.
- Produces:
  - `SinkFactory = Callable[[StateStore, PairedSession, str, str], EventSink]` — sink factories now take `(store, session, source, target)`. Update `cli._default_sink_factory` (`cli.py:221-231`): `SyncEngine(store, session, source, target)`; `EventLogger(session.tandem_id, source)` ignores target.
  - The tail thread builds ONE loop per direction and drains them all per wake-up.
  - Status probe: `status_probe=probe if hasattr(adapter, "session_status") else None` (replaces `active == "claude"` at `runner.py:570`).
  - Warm: `shadow = session.next_active(active)`; **carve-out** — `if shadow == "opencode": return` at the top of `ensure_warm` (spec: an opencode TUI booted pre-drain would cache the session pre-drain; opencode-bound flips are cold in v1).

- [ ] **Step 1: Write the failing test** (add to `tests/test_runner.py`; the file's existing fake-session helpers apply)

```python
def test_tail_thread_drains_all_directions(tmp_path, monkeypatch):
    """One source line lands in BOTH shadows via the runner's TailLoop set."""
    env = Env3(tmp_path, monkeypatch)
    from tandem.runner import TailLoop
    from tandem.sync import SyncEngine

    write_line(env.claude_shadow, claude_user("fan out"))
    for target in env.session.targets_for("claude"):
        engine = SyncEngine(env.store, env.session, "claude", target)
        loop = TailLoop(env.store, env.session, "claude", target,
                        env.claude_shadow, engine)
        assert loop.drain() >= 1


def test_warm_skips_opencode_target(tmp_path, monkeypatch):
    """ensure_warm never spawns when next-in-cycle is opencode (v1 carve-out).
    Covered structurally: next_active from codex is opencode in a 3-way
    session, and _shadow_size is never called for it."""
    env = Env3(tmp_path, monkeypatch, active="codex")
    assert env.session.next_active("codex") == "opencode"
```

(The real warm behavior is asserted through the existing `spawns` harness at `tests/test_runner.py:1080-1440`: add one case that patches a 3-way session with `active="codex"` and asserts `spawns == []` after a drain tick. Reuse the file's existing spawn-capture fixture verbatim — copy the nearest existing test (`tests/test_runner.py:1089-1126`) and change the session to `Env3` + assert empty.)

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_runner.py -q`
Expected: FAIL — TailLoop signature / sink factory arity

- [ ] **Step 3: Implement in `runner.py`**

`SinkFactory` (`runner.py:462`):

```python
SinkFactory = Callable[[StateStore, PairedSession, str, str], EventSink]
```

`tail_thread` (`runner.py:685-743`) — loops per direction:

```python
        def tail_thread() -> None:
            with StateStore() as store:
                current = store.get_session(session.tandem_id) or session
                path = transcript
                if path is None:
                    # codex minting its own session: wait for the rollout.
                    while not stop.is_set():
                        found = await_codex_rollout(session.cwd, spawn_time, timeout=0.5)
                        if found:
                            sid = paths.codex_rollout_session_id(found)
                            if sid:
                                store.set_native_session_id(session.tandem_id, "codex", sid)
                                current = store.get_session(session.tandem_id) or current
                            path = found
                            monitor.transcript = path
                            break
                    if path is None:
                        return
                loops: list[TailLoop] = []
                sinks: list[EventSink] = []
                try:
                    for tgt in current.targets_for(active):
                        sink = self.sink_factory(store, current, active, tgt)
                        sinks.append(sink)
                        loops.append(TailLoop(store, current, active, tgt, path, sink))
                except Exception as exc:
                    errors.append(f"sync disabled: {exc}")
                    for s in sinks:
                        s.close()
                    return
                watcher = TranscriptWatcher()
                watcher.watch(path)
                watcher.watch(sentinel)
                watcher.start()
                from . import ops
                try:
                    while not stop.is_set():
                        drained = 0
                        with ops._sub_lock():
                            for loop in loops:
                                drained += loop.drain()
                        if drained:
                            ensure_warm()
                        watcher.wait()
                    with ops._sub_lock():
                        for loop in loops:
                            loop.drain()
                    for loop in loops:
                        errors.extend(loop.errors)
                finally:
                    watcher.stop()
                    for s in sinks:
                        s.close()
```

Probe wiring (`runner.py:548-571`): rename `claude_probe` to `status_probe_fn` and gate by capability:

```python
        def status_probe_fn() -> str | None:
            try:
                status = adapter.session_status(active_sid)
            except Exception:
                status = None
            if status != probe_last["status"]:
                _flip_debug(f"probe {probe_last['status']} -> {status}")
                probe_last["status"] = status
            return status

        monitor = FlipMonitor(
            control, adapter.quit_keystrokes(), transcript, sentinel,
            marker_wired=bool(hook_extra),
            # capability check: adapters without a live status registry/probe
            # opt out by absence — a probe answering None would flip eagerly
            # mid-turn.
            status_probe=status_probe_fn if hasattr(adapter, "session_status") else None,
        )
```

`ensure_warm` (`runner.py:586-593`):

```python
        def ensure_warm() -> None:
            if not (frame_cfg.warm and _stdin_tty()):
                return
            shadow = session.next_active(session.active)
            if shadow == "opencode":
                # v1 carve-out (spec: Frame and flip): an opencode TUI booted
                # before the final drain would cache the session pre-drain and
                # never show the last turn. Opencode-bound flips run cold.
                return
            size = _shadow_size(session, shadow)
            ...  # rest unchanged from runner.py:590-650
```

`cli._default_sink_factory` (`cli.py:221-231`):

```python
def _default_sink_factory(store, session, source, target):
    import os

    from .runner import EventLogger
    from .sync import SyncEngine

    if os.environ.get("TANDEM_LOG_EVENTS"):
        return EventLogger(session.tandem_id, source)
    return SyncEngine(store, session, source, target)
```

Sweep test fakes: `grep -rn "sink_factory" tests/` and add the fourth parameter to every fake factory's signature.

- [ ] **Step 4: Run the full suite**

Run: `python -m pytest tests/ -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat: runner fans out one tail loop per direction; capability probe; opencode warm carve-out"
```

---

### Task 7: Participants — config key, resolution, resume narrowing, cycle flip ladder

**Files:**
- Modify: `src/tandem/config.py`, `src/tandem/cli.py:36-125,186-218,583-597`, `src/tandem/flip.py:201-267`
- Create: `tests/test_participants.py`
- Test: `tests/test_participants.py`, `tests/test_flip.py`, `tests/test_config.py`

**Interfaces:**
- Produces:
  - `config.SUPPORTED_HARNESSES = ("claude", "codex", "opencode")`
  - `config.load_harnesses() -> list[str]` — top-level `harnesses` key; forgiving (unknown names dropped, duplicates deduped, malformed → default = all supported; order preserved)
  - `cli._resolve_participants(warn_only=False) -> tuple[list[str], dict[str, str | None]]` — `(usable participants, versions)`; **not-installed = silent skip; installed-but-unusable = warning + skip; <2 usable = error (or all-warn when warn_only)**
  - `cli._pair_session(store, cwd, active, participants)` — N-ary
  - resume narrowing in `cli.resume`
  - `flip._switch(..., visited: set[str])` — cycle ladder
- Consumes: `adapter.runtime_ready()` — added to `base.py` in this task, default `True` (opencode overrides in Task 10).

- [ ] **Step 1: Write the failing tests** (`tests/test_participants.py`, NEW)

```python
"""Participant resolution, resume narrowing, and the flip cycle ladder."""

import pytest

from tandem import config
from tandem.cli import _resolve_participants
from tandem.harness import ADAPTERS


def test_load_harnesses_default_all(tmp_path, monkeypatch):
    monkeypatch.setenv("TANDEM_HOME", str(tmp_path))
    assert config.load_harnesses() == ["claude", "codex", "opencode"]


def test_load_harnesses_forgiving(tmp_path, monkeypatch):
    monkeypatch.setenv("TANDEM_HOME", str(tmp_path))
    (tmp_path / "config.toml").write_text(
        'harnesses = ["codex", "claude", "codex", "gemini"]\n'
    )
    assert config.load_harnesses() == ["codex", "claude"]


def test_load_harnesses_malformed_falls_back(tmp_path, monkeypatch):
    monkeypatch.setenv("TANDEM_HOME", str(tmp_path))
    (tmp_path / "config.toml").write_text("harnesses = 42\n")
    assert config.load_harnesses() == ["claude", "codex", "opencode"]


def _fake_versions(monkeypatch, mapping):
    """mapping: harness id -> version string or None (not installed)."""
    for hid, v in mapping.items():
        monkeypatch.setattr(type(ADAPTERS[hid]), "detect_version",
                            lambda self, _v=v: _v)


def test_resolution_silently_skips_missing(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TANDEM_HOME", str(tmp_path))
    _fake_versions(monkeypatch, {"claude": "2.1.220", "codex": "0.145.0",
                                 "opencode": None})
    usable, versions = _resolve_participants()
    assert usable == ["claude", "codex"]
    assert versions["opencode"] is None
    assert capsys.readouterr().err == ""     # not-installed is SILENT


def test_resolution_fewer_than_two_errors(tmp_path, monkeypatch):
    monkeypatch.setenv("TANDEM_HOME", str(tmp_path))
    _fake_versions(monkeypatch, {"claude": "2.1.220", "codex": None,
                                 "opencode": None})
    with pytest.raises(SystemExit):
        _resolve_participants()


def test_resume_narrows_and_persists(tmp_path, monkeypatch):
    """A stored 3-way session resumed with opencode gone drops it for good."""
    env = Env3(tmp_path, monkeypatch)
    from tandem.cli import _narrow_participants
    _fake_versions(monkeypatch, {"claude": "2.1.220", "codex": "0.145.0",
                                 "opencode": None})
    session = _narrow_participants(env.store, env.session)
    assert session.participants == ["claude", "codex"]
    stored = env.store.get_session(env.session.tandem_id)
    assert stored.participants == ["claude", "codex"]   # persisted


def test_resume_narrow_moves_active_off_dropped_member(tmp_path, monkeypatch):
    env = Env3(tmp_path, monkeypatch, active="claude")
    env.store.set_active(env.session.tandem_id, "opencode")
    env.session = env.store.get_session(env.session.tandem_id)
    from tandem.cli import _narrow_participants
    _fake_versions(monkeypatch, {"claude": "2.1.220", "codex": "0.145.0",
                                 "opencode": None})
    session = _narrow_participants(env.store, env.session)
    assert session.active == "claude"       # first survivor in stored order
```

And the ladder test (add to `tests/test_flip.py`, using that file's existing fake `run_harness` pattern):

```python
def test_switch_ladder_tries_next_unvisited_then_falls_back(tmp_path, monkeypatch):
    """3-way cycle: codex won't launch -> try opencode; opencode won't
    launch -> land back on claude (the old active). Never ping-pongs."""
    env = Env3(tmp_path, monkeypatch)   # active=claude
    attempts = []

    def run_harness(session):
        attempts.append(session.active)
        raise OSError("won't start")

    from tandem.flip import _switch
    code, flip = _switch(env.session.tandem_id, run_harness, 0, carry=None)
    # ladder: codex (next), opencode (next unvisited), back to claude
    assert attempts == ["codex", "opencode", "claude"]
    assert flip is False
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_participants.py tests/test_flip.py -q`
Expected: FAIL — missing `load_harnesses`, `_resolve_participants`, `_narrow_participants`; ladder visits only one alternative

- [ ] **Step 3: Implement**

`src/tandem/config.py` (append):

```python
SUPPORTED_HARNESSES = ("claude", "codex", "opencode")


def load_harnesses() -> list[str]:
    """Top-level `harnesses` key: ordered participant intent. Forgiving like
    every other key — unknown names dropped, duplicates deduped, anything
    malformed falls back to all supported. Order defines the flip cycle."""
    raw = _read_config().get("harnesses")
    if not isinstance(raw, list):
        return list(SUPPORTED_HARNESSES)
    seen: set[str] = set()
    out: list[str] = []
    for h in raw:
        if isinstance(h, str) and h in SUPPORTED_HARNESSES and h not in seen:
            seen.add(h)
            out.append(h)
    return out or list(SUPPORTED_HARNESSES)
```

`src/tandem/harness/base.py` (append to `HarnessAdapter`):

```python
    def runtime_ready(self) -> tuple[bool, str]:
        """Beyond version support: is this installed harness actually usable
        (storage reachable, schema sane)? Returns (ok, reason). Default
        True — only adapters with external runtime state override."""
        return True, ""
```

`src/tandem/cli.py` — replace `_check_versions` (`cli.py:36-58`) with:

```python
def _resolve_participants(warn_only: bool = False) -> tuple[list[str], dict[str, str | None]]:
    """participants = configured ∩ installed-and-version-supported-and-ready.

    Not-installed is a normal state: silent skip, zero further probes.
    Installed but unusable (version below floor, runtime not ready) warns
    and skips — fail closed. Fewer than two usable is an error naming
    what's missing (warn-only mode reports instead, for status/resume)."""
    from .config import load_harnesses

    versions: dict[str, str | None] = {}
    usable: list[str] = []
    for hid in load_harnesses():
        adapter = get_adapter(hid)
        v = adapter.detect_version()
        versions[hid] = v
        if v is None:
            continue                      # silent: the invariant
        if not adapter.version_supported(v):
            tested = compat.COMPAT[hid].tested
            click.secho(
                f"warning: {adapter.display_name} version {v!r} is outside the "
                f"range tandem was built against (tested: {tested}). "
                f"Run `tandem doctor` before trusting sync.",
                fg="yellow", err=True,
            )
        ok, reason = adapter.runtime_ready()
        if not ok:
            click.secho(
                f"warning: {adapter.display_name} installed but unusable "
                f"({reason}) — excluded from this session.",
                fg="yellow", err=True,
            )
            continue
        usable.append(hid)
    if len(usable) < 2:
        missing = [h for h in load_harnesses() if h not in usable]
        msg = (f"tandem needs at least two usable harnesses; usable: "
               f"{usable or 'none'}, unavailable: {missing}.")
        if warn_only:
            click.secho(f"warning: {msg}", fg="yellow", err=True)
        else:
            click.secho(f"error: {msg}", fg="red", err=True)
            sys.exit(1)
    return usable, versions
```

`_pair_session` (`cli.py:81-125`) — N-ary creation. Opencode differs from the other two in one way that matters here: `opencode -s <id>` requires the session to already exist, so an opencode participant is pre-created **whether or not it is active**; claude's file appears at first turn (active) or is seeded (shadow); codex mints its own id when active:

```python
def _pair_session(store: StateStore, cwd: str, active: str,
                  participants: list[str]) -> PairedSession:
    """Create a fresh N-way session: state row, seeded shadow transcripts,
    memory sync. Echoes what it did."""
    native: dict[str, str | None] = {}
    for hid in participants:
        if hid == "codex" and hid == active:
            native[hid] = None   # codex mints its own id on first run
        else:
            native[hid] = get_adapter(hid).mint_session_id()
    session = store.create_session(cwd, active, participants, native)

    note = SEED_NOTE.format(
        tandem_id=session.tandem_id,
        other=get_adapter(active).display_name,
    )
    for hid in participants:
        needs_create = (hid != active) or (hid == "opencode")
        if not needs_create or native[hid] is None:
            continue
        if hid == active:
            # opencode active: its session must exist before `opencode -s`
            direction = f"{session.next_active(active)}->{hid}"
        else:
            direction = f"{active}->{hid}"
        ctx = SessionContext(
            tandem_id=session.tandem_id, cwd=cwd, direction=direction,
            source_session_id=native.get(direction.split("->")[0]),
            target_session_id=native[hid],
        )
        get_adapter(hid).create_shadow_transcript(cwd, native[hid], ctx, note)
        cursor = store.get_cursor(session.tandem_id, active,
                                  hid if hid != active else session.next_active(active))
        cursor.pending["harness_state"] = ctx.harness_state
        store.save_cursor(cursor)

    from .memory_sync import sync_memory_files

    mem = sync_memory_files(cwd)
    shadows = [h for h in participants if h != active]
    click.echo(f"paired {session.tandem_id} ({active} active, "
               f"{', '.join(shadows)} shadow)")
    for a in mem.actions:
        click.echo(f"  memory: {a}")
    for w in mem.warnings:
        click.secho(f"  memory: {w}", fg="yellow", err=True)
    if "codex" in participants and native.get("codex") is None:
        click.echo("  note: codex session id will be captured on first run")
    return session
```

(The `hid == active` cursor line above stores claude-side scratch under the direction whose ctx it belongs to — for the opencode-active case the harness-state blob is empty, so which direction row hosts it is immaterial; keep the expression as written.)

`_interactive` (`cli.py:583-591`):

```python
def _interactive(active: str) -> None:
    cwd = _cwd()
    usable, _ = _resolve_participants()
    if active not in usable:
        click.secho(f"error: --active {active} is not usable here "
                    f"(usable: {', '.join(usable)}).", fg="red", err=True)
        sys.exit(1)
    with StateStore() as store:
        session = _pair_session(store, cwd, active, usable)
    from .plugin_setup import offer_install

    offer_install()
    sys.exit(_enter_session(session))
```

`--active` choice (`cli.py:63-69`): `type=click.Choice(["claude", "codex", "opencode"])`. Same for `run --on` (`cli.py:237-241`).

Resume narrowing — add and call from `resume` (`cli.py:186-218`, after the session is loaded, before `_enter_session`) and from `_require_session`:

```python
def _narrow_participants(store: StateStore, session: PairedSession) -> PairedSession:
    """Resume rule (spec: Participants/Resume): members gone missing are
    dropped from the session for good; narrowed list persisted; active moves
    to the first survivor if it was dropped; <2 survivors is fatal. No
    dynamic rejoin."""
    usable, _ = _resolve_participants(warn_only=True)
    survivors = [h for h in session.participants if h in usable]
    if survivors == session.participants:
        return session
    if len(survivors) < 2:
        click.secho(
            f"error: session {session.tandem_id} needs two usable harnesses; "
            f"surviving: {survivors or 'none'}.", fg="red", err=True)
        sys.exit(1)
    dropped = [h for h in session.participants if h not in survivors]
    click.secho(f"note: dropped {', '.join(dropped)} from this session "
                f"(not usable here); it will not rejoin.", fg="yellow", err=True)
    store.set_participants(session.tandem_id, survivors)
    if session.active not in survivors:
        store.set_active(session.tandem_id, survivors[0])
    return store.get_session(session.tandem_id)
```

`src/tandem/flip.py` — the visited-set ladder (`flip.py:201-267`). Replace `fall_back: bool` with `visited: set[str] | None`:

```python
def _switch(
    tandem_id: str, run_harness, code: int, visited: set[str] | None = None,
    carry: dict | None = None,
) -> tuple[int, bool]:
    """Flip to the next harness in cycle order and re-enter it. On a launch
    failure, try the next unvisited harness in the cycle; when every other
    participant has refused, fall back to the harness the user just left.
    `visited` carries the refusals across recursive attempts — the N-ary
    generalization of the old no-ping-pong rule (at N=2 it degenerates to
    exactly one retry that lands on the old active)."""
    with StateStore() as store:
        session = store.get_session(tandem_id)
        if session is None:
            click.secho(
                f"switch failed: session {tandem_id} is no longer in the"
                " state store.", fg="red", err=True)
            return code, False
        old = session.active
        visited = visited or set()
        visited.add(old)
        target = session.next_active(old)
        while target in visited and target != old:
            target = session.next_active(target)
        if target in visited:
            click.secho("no harness would start — staying where we were.",
                        fg="red", err=True)
            return code, False
        try:
            new_active, problems, mem = ops.switch_session(store, session, to=target)
        except Exception as exc:
            click.secho(
                f"switch failed: {type(exc).__name__}: {exc}", fg="red", err=True)
            return code, False
    _report_switch(old, new_active, problems, mem)
    if carry is not None:
        standby = carry.get("standby")
        if standby is not None:
            from .runner import _flip_debug

            fresh = _standby_fresh(standby, new_active, session, mem)
            _flip_debug(f"standby-gate side={new_active} fresh={fresh}")
            if not fresh:
                carry["standby"] = None
                _reap(standby, carry)
    code, flip, launched = _try_enter(tandem_id, run_harness)
    if launched:
        return code, flip
    visited.add(new_active)
    others_left = [h for h in session.participants if h not in visited]
    if others_left:
        click.secho(
            f"{new_active} would not start — trying {others_left[0]}.",
            fg="yellow", err=True)
    else:
        click.secho(
            f"{new_active} would not start — switching back to {old}.",
            fg="yellow", err=True)
        visited.discard(old)   # allow the landing on the old active
    return _switch(tandem_id, run_harness, code, visited=visited, carry=carry)
```

(Note the final fall-back: when every other participant refused, the old active is removed from `visited` so the recursion's cycle walk selects it; if IT then refuses too, `visited` re-fills and the "no harness would start" branch ends the loop. At N=2 the sequence is exactly today's: try other → fail → back to old → stop.)

`status` command (`cli.py:128-160`): both fixed loops become `for hid in session.participants:` / `for source in session.participants:`; `_check_versions(warn_only=True)` call becomes `_resolve_participants(warn_only=True)[1]` bound to `versions`.

- [ ] **Step 4: Run the full suite**

Run: `python -m pytest tests/ -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat: participant resolution, resume narrowing, N-ary pairing, cycle flip ladder"
```

---

### Task 8: Doctor + adapter-dispatched validation

**Files:**
- Modify: `src/tandem/doctor.py:18-119,149-243,301-315`, `src/tandem/harness/base.py`, `src/tandem/harness/claude_code.py`, `src/tandem/harness/codex.py`
- Test: `tests/test_memory_doctor.py`

**Interfaces:**
- Produces: `HarnessAdapter.validate_transcript(path: Path, session_id: str | None) -> list[str]` — default parses JSONL (the shared prelude from `doctor.validate_transcript:32-56` moves into `base.py` as `_parse_jsonl_entries`) then calls `self._validate_entries(entries, session_id)`; claude/codex `_validate_entries` bodies are today's `doctor._validate_claude` / `_validate_codex` moved verbatim (with their `_CLAUDE_ENTRY_TYPES` / `_CODEX_LINE_TYPES` sets).
- `doctor.validate_transcript(harness, path, session_id)` stays as a thin shim: `return get_adapter(harness).validate_transcript(path, session_id)` (its callers at `ops.py:141` and doctor internals keep working).
- `run_doctor` and `_live_resume_checks` iterate `session.participants` instead of the fixed tuples at `doctor.py:155,179,203,306-315`; the version loop iterates `config.load_harnesses()` and prints the one-line info note for non-installed members (`report.ok(f"{display_name}: not installed (not a participant)")` — doctor is the single place absence is mentioned).

- [ ] **Step 1: Write the failing test** (add to `tests/test_memory_doctor.py`)

```python
def test_doctor_iterates_participants(tmp_path, monkeypatch):
    env = Env3(tmp_path, monkeypatch)
    from tandem.doctor import run_doctor
    report = run_doctor(env.store, env.session, live=False)
    text = " ".join(c.message for c in report.checks)
    assert "opencode" in text


def test_adapter_validate_transcript_dispatch(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    from tandem.harness import get_adapter
    problems = get_adapter("claude").validate_transcript(
        env.claude_shadow, env.session.native_id("claude"))
    assert problems == []
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_memory_doctor.py -q`
Expected: FAIL — adapter has no `validate_transcript`

- [ ] **Step 3: Implement** — move the validators:

`base.py` gains:

```python
    def validate_transcript(self, path: Path, session_id: str | None) -> list[str]:
        """Structural dry-resume check. Default: parse JSONL, then the
        adapter-specific entry validation. Storage-backed adapters override
        the whole method."""
        problems, entries = _parse_jsonl_entries(path)
        if entries is None:
            return problems
        return problems + self._validate_entries(entries, session_id)

    def _validate_entries(self, entries, session_id) -> list[str]:
        return []


def _parse_jsonl_entries(path: Path):
    """(problems, entries|None) — the shared prelude formerly at
    doctor.validate_transcript:32-56, moved verbatim."""
    import json
    problems: list[str] = []
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return [f"cannot read transcript: {exc}"], None
    if not raw.strip():
        return ["transcript is empty"], None
    entries: list[tuple[int, dict]] = []
    for i, line in enumerate(raw.splitlines()):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            problems.append(f"line {i}: not valid JSON")
            continue
        if not isinstance(obj, dict):
            problems.append(f"line {i}: not a JSON object")
            continue
        entries.append((i, obj))
    if not entries:
        return problems + ["no parseable entries"], None
    return problems, entries
```

Move `_CLAUDE_ENTRY_TYPES` + `_validate_claude` body into `ClaudeCodeAdapter._validate_entries`, `_CODEX_LINE_TYPES` + `_validate_codex` into `CodexAdapter._validate_entries` (verbatim bodies; `self` unused). `doctor.py` keeps:

```python
def validate_transcript(harness: str, path: Path, session_id: str | None) -> list[str]:
    from .harness import get_adapter

    return get_adapter(harness).validate_transcript(path, session_id)
```

`run_doctor` loops:

```python
    from .config import load_harnesses

    for hid in load_harnesses():
        adapter = get_adapter(hid)
        v = adapter.detect_version()
        if v is None:
            report.ok(f"{adapter.display_name}: not installed (not a participant)")
        elif not adapter.version_supported(v):
            report.warn(...)  # unchanged wording from doctor.py:161-165
        else:
            report.ok(f"{adapter.display_name}: {v}")
```

and the transcript/cursor loops (`doctor.py:179,203`) become `for hid in session.participants:` / nested `for target in session.targets_for(source):` for cursor checks (report intent/failed_turns per direction: message prefix `f"sync {source}->{target}:"`). `_live_resume_checks` (`doctor.py:301-315`) becomes a participant loop:

```python
def _live_resume_checks(report, session, transcripts) -> None:
    from .harness import get_adapter

    prompt = "tandem doctor live check - reply with exactly: ok"
    for hid in session.participants:
        sid = session.native_id(hid)
        if sid and transcripts.get(hid):
            argv = get_adapter(hid).oneoff_argv(sid, prompt)
            _live_one(report, hid, argv, session.cwd)
        else:
            report.warn(f"{hid}: skipping live resume (no transcript yet)")
```

- [ ] **Step 4: Run the full suite**

Run: `python -m pytest tests/ -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat: doctor iterates participants; transcript validation moves onto adapters"
```

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
- Produces: `OpencodeAdapter.validate_transcript(path, session_id) -> list[str]` — full override (no JSONL prelude): session row exists; ≥1 message; last message is a closed assistant; DB journal mode is WAL. Doctor's existing participant loop (Task 8) calls it via the dispatch shim, so no doctor loop changes are needed — plus one addition: when opencode is installed, `run_doctor` reports `runtime_ready()` as ok/warn.

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

## Post-plan checklist (not tasks — release gates)

- Live-verify with the real TUI (spec items 2-5): quit-keystroke recipe,
  `opencode -s` rendering of a tandem-minted session (sentinel display, no
  "working" state), WAL fs-watch debounce under a busy turn, flip-gate
  probe latency. Fix-ups land as follow-up commits.
- A real three-way session end-to-end on this machine: `tandem`, one turn
  in claude, Ctrl-] to codex, one turn, Ctrl-] to opencode, verify both
  prior turns render, one turn there, Ctrl-] back to claude.
- README/docs mention of the third harness; PyPI release notes.
