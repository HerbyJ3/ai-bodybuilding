"""Dashboard storage helpers: find client databases under the data folder and keep
coach <-> Mr. J chat history next to each client's events (same SQLite file)."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from store.event_store import EventStore

_CHAT_SCHEMA = """
CREATE TABLE IF NOT EXISTS chat_messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id  TEXT NOT NULL,
    created_at TEXT NOT NULL,
    role       TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    kind       TEXT NOT NULL DEFAULT 'chat',   -- chat | draft
    content    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS chat_client ON chat_messages (client_id, id);
"""


@dataclass(frozen=True)
class ClientRef:
    client_id: str
    db_path: Path


def find_clients(data_dir: Path) -> dict[str, ClientRef]:
    """Every client id found in any *.db under data_dir (one shared DB or one per client)."""
    out: dict[str, ClientRef] = {}
    for db in sorted(data_dir.rglob("*.db")):
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            rows = con.execute("SELECT DISTINCT client_id FROM events").fetchall()
            con.close()
        except sqlite3.DatabaseError:
            continue
        for (cid,) in rows:
            out.setdefault(cid, ClientRef(cid, db))
    return dict(sorted(out.items()))


class ChatStore:
    def __init__(self, store: EventStore) -> None:
        self.db = store._db
        self.db.executescript(_CHAT_SCHEMA)

    def history(self, client_id: str) -> list[dict]:
        rows = self.db.execute(
            "SELECT role, kind, content, created_at FROM chat_messages WHERE client_id = ? ORDER BY id",
            (client_id,)).fetchall()
        return [{"role": r, "kind": k, "content": c, "created_at": t} for r, k, c, t in rows]

    def add(self, client_id: str, role: str, content: str, kind: str = "chat") -> None:
        with self.db:
            self.db.execute("INSERT INTO chat_messages (client_id, created_at, role, kind, content) "
                            "VALUES (?,?,?,?,?)",
                            (client_id, datetime.now(timezone.utc).isoformat(), role, kind, content))

    def clear(self, client_id: str) -> None:
        with self.db:
            self.db.execute("DELETE FROM chat_messages WHERE client_id = ?", (client_id,))
