"""Append-only SQLite event store (BUILD_SPEC §3, M2). Postgres-ready shape:
one row per event, JSON payload, no updates or deletes."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Iterable

from schemas.events import Event

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    event_id    TEXT PRIMARY KEY,
    client_id   TEXT NOT NULL,
    type        TEXT NOT NULL,
    timestamp   TEXT NOT NULL,
    source      TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    payload     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_client_ts ON events (client_id, timestamp);
CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
"""


class EventStore:
    def __init__(self, path: str | Path = ":memory:") -> None:
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path))
        self._db.executescript(_SCHEMA)

    def close(self) -> None:
        self._db.close()

    def append(self, events: Iterable[Event]) -> tuple[int, int]:
        """Returns (inserted, duplicates). Duplicate event_ids are ignored."""
        inserted = dupes = 0
        with self._db:
            for e in events:
                cur = self._db.execute(
                    "INSERT OR IGNORE INTO events VALUES (?,?,?,?,?,?,?)",
                    (e.event_id, e.client_id, e.type, e.timestamp.isoformat(),
                     e.source.value, e.recorded_at.isoformat(), e.payload_json()),
                )
                if cur.rowcount:
                    inserted += 1
                else:
                    dupes += 1
        return inserted, dupes

    def read(self, client_id: str, until: datetime | None = None) -> list[Event]:
        q = "SELECT * FROM events WHERE client_id = ?"
        args: list[str] = [client_id]
        if until is not None:
            q += " AND timestamp <= ?"
            args.append(until.isoformat())
        q += " ORDER BY timestamp, recorded_at, event_id"
        rows = self._db.execute(q, args).fetchall()
        return [
            Event(event_id=r[0], client_id=r[1], type=r[2],
                  timestamp=datetime.fromisoformat(r[3]), source=r[4],
                  recorded_at=datetime.fromisoformat(r[5]), payload=json.loads(r[6]))
            for r in rows
        ]

    def has_consent(self, client_id: str) -> bool:
        rows = [e for e in self.read(client_id) if e.type == "consent_recorded"]
        return bool(rows) and rows[-1].payload.granted
