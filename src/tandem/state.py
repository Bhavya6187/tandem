"""Crash-safe local state store under ~/.tandem/ (stdlib sqlite3).

Holds the pairing between the participants' native sessions and the
per-direction sync cursors (last confirmed source line index + byte offset,
plus any pending tool-call pairings serialized as JSON so a restart can
resume mid-turn).
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import paths

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


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


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


class StateStore:
    def __init__(self, db_path: Path | None = None):
        self.db_path = db_path or paths.state_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        if self._schema_stale():
            self._conn.close()
            # replace(), not rename(): a leftover .old from an earlier
            # move-aside is deliberately overwritten — the newest stale DB
            # is the one worth keeping, and startup must never fail on it
            self.db_path.replace(self.db_path.with_name(self.db_path.name + ".old"))
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

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "StateStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- sessions ------------------------------------------------------------

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

    def get_session(self, tandem_id: str) -> PairedSession | None:
        row = self._conn.execute(
            "SELECT * FROM sessions WHERE tandem_id = ?", (tandem_id,)
        ).fetchone()
        return self._row_to_session(row) if row else None

    def latest_session_for_cwd(self, cwd: str) -> PairedSession | None:
        """Most recently used paired session for a working directory.

        COALESCE keeps the ordering NULL-immune: a row whose last_used_at is
        NULL falls back to its creation time instead of sorting behind every
        older row.
        """
        row = self._conn.execute(
            "SELECT * FROM sessions WHERE cwd = ?"
            " ORDER BY COALESCE(last_used_at, created_at) DESC, created_at DESC"
            " LIMIT 1",
            (cwd,),
        ).fetchone()
        return self._row_to_session(row) if row else None

    def list_sessions(self, limit: int = 10) -> list[PairedSession]:
        """Most recently used paired sessions across every working
        directory, newest first (same NULL-immune ordering as
        latest_session_for_cwd)."""
        rows = self._conn.execute(
            "SELECT * FROM sessions"
            " ORDER BY COALESCE(last_used_at, created_at) DESC, created_at DESC"
            " LIMIT ?",
            (limit,),
        ).fetchall()
        return [self._row_to_session(r) for r in rows]

    def touch_used(self, tandem_id: str) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE sessions SET last_used_at = ? WHERE tandem_id = ?",
                (_now(), tandem_id),
            )

    def set_active(self, tandem_id: str, active: str) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE sessions SET active = ? WHERE tandem_id = ?",
                (active, tandem_id),
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

    def touch_sync(self, tandem_id: str) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE sessions SET last_sync_at = ? WHERE tandem_id = ?",
                (_now(), tandem_id),
            )

    # -- sync cursors --------------------------------------------------------

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
