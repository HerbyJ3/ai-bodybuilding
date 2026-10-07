"""Dashboard storage helpers: find client databases under the data folder and keep
Mr. J chat history (coach and client audiences, kept apart) and coach <-> client support
messages next to each client's events (same SQLite file). These tables are not events and
are never engine inputs (BUILD_SPEC §15)."""
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
    content    TEXT NOT NULL,
    audience   TEXT NOT NULL DEFAULT 'coach'   -- coach | client (who talks to Mr. J)
);
CREATE INDEX IF NOT EXISTS chat_client ON chat_messages (client_id, id);
"""

_SUPPORT_SCHEMA = """
CREATE TABLE IF NOT EXISTS support_messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id  TEXT NOT NULL,
    created_at TEXT NOT NULL,
    sender     TEXT NOT NULL CHECK (sender IN ('client', 'coach')),
    content    TEXT NOT NULL,
    read_at    TEXT
);
CREATE INDEX IF NOT EXISTS support_client ON support_messages (client_id, id);
"""

AUDIENCES = ("coach", "client")
SENDERS = ("client", "coach")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


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
    """Mr. J chat per client and audience: the coach's chat and the client's chat never mix."""

    def __init__(self, store: EventStore, audience: str = "coach") -> None:
        if audience not in AUDIENCES:
            raise ValueError(f"audience must be one of {AUDIENCES}, not {audience!r}")
        self.audience = audience
        self.db = store._db
        self.db.executescript(_CHAT_SCHEMA)
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(chat_messages)")}
        if "audience" not in cols:  # databases made before the client app: old rows are coach chat
            with self.db:
                self.db.execute("ALTER TABLE chat_messages "
                                "ADD COLUMN audience TEXT NOT NULL DEFAULT 'coach'")

    def history(self, client_id: str) -> list[dict]:
        rows = self.db.execute(
            "SELECT role, kind, content, created_at FROM chat_messages "
            "WHERE client_id = ? AND audience = ? ORDER BY id",
            (client_id, self.audience)).fetchall()
        return [{"role": r, "kind": k, "content": c, "created_at": t} for r, k, c, t in rows]

    def add(self, client_id: str, role: str, content: str, kind: str = "chat") -> None:
        with self.db:
            self.db.execute("INSERT INTO chat_messages "
                            "(client_id, created_at, role, kind, content, audience) "
                            "VALUES (?,?,?,?,?,?)",
                            (client_id, _now(), role, kind, content, self.audience))

    def clear(self, client_id: str) -> None:
        with self.db:
            self.db.execute("DELETE FROM chat_messages WHERE client_id = ? AND audience = ?",
                            (client_id, self.audience))


class SupportStore:
    """Plain coach <-> client message thread (support only, no AI)."""

    def __init__(self, store: EventStore) -> None:
        self.db = store._db
        self.db.executescript(_SUPPORT_SCHEMA)

    def add(self, client_id: str, sender: str, content: str) -> None:
        if sender not in SENDERS:
            raise ValueError(f"sender must be one of {SENDERS}, not {sender!r}")
        with self.db:
            self.db.execute("INSERT INTO support_messages (client_id, created_at, sender, content) "
                            "VALUES (?,?,?,?)", (client_id, _now(), sender, content))

    def thread(self, client_id: str) -> list[dict]:
        rows = self.db.execute(
            "SELECT id, sender, content, created_at, read_at FROM support_messages "
            "WHERE client_id = ? ORDER BY id", (client_id,)).fetchall()
        return [{"id": i, "sender": s, "content": c, "created_at": t, "read_at": r}
                for i, s, c, t, r in rows]

    def unread_for_coach(self, client_id: str) -> int:
        """Client messages the coach hasn't read yet."""
        return self.db.execute(
            "SELECT COUNT(*) FROM support_messages "
            "WHERE client_id = ? AND sender = 'client' AND read_at IS NULL",
            (client_id,)).fetchone()[0]

    def mark_read(self, client_id: str) -> None:
        """The coach has read the thread: stamp every unread client message."""
        with self.db:
            self.db.execute("UPDATE support_messages SET read_at = ? "
                            "WHERE client_id = ? AND sender = 'client' AND read_at IS NULL",
                            (_now(), client_id))
