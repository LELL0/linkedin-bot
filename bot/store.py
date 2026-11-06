"""SQLite state, kept outside the repository. It stores ids and timestamps, never message text."""

import sqlite3
import time
from pathlib import Path


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS accepted_people (profile_id TEXT PRIMARY KEY, at_ms INTEGER);
            CREATE TABLE IF NOT EXISTS seen_invites (id TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS answered (message_urn TEXT PRIMARY KEY, at_ms INTEGER);
            """
        )
        self.db.commit()

    def watermark(self) -> int:
        """First-ever start time in ms. Nothing older is touched, and restarts keep it."""
        row = self.db.execute("SELECT value FROM meta WHERE key = 'watermark'").fetchone()
        if row:
            return int(row[0])
        now = int(time.time() * 1000)
        self.db.execute("INSERT INTO meta VALUES ('watermark', ?)", (str(now),))
        self.db.commit()
        return now

    def _add(self, sql: str, *values) -> None:
        self.db.execute(sql, values)
        self.db.commit()

    def _has(self, sql: str, value: str) -> bool:
        return self.db.execute(sql, (value,)).fetchone() is not None

    def add_accepted(self, profile_id: str) -> None:
        self._add(
            "INSERT OR IGNORE INTO accepted_people VALUES (?, ?)", profile_id, int(time.time() * 1000)
        )

    def is_accepted(self, profile_id: str) -> bool:
        return self._has("SELECT 1 FROM accepted_people WHERE profile_id = ?", profile_id)

    def add_seen_invite(self, invite_id: str) -> None:
        self._add("INSERT OR IGNORE INTO seen_invites VALUES (?)", invite_id)

    def is_seen_invite(self, invite_id: str) -> bool:
        return self._has("SELECT 1 FROM seen_invites WHERE id = ?", invite_id)

    def mark_answered(self, message_urns: list[str]) -> None:
        now = int(time.time() * 1000)
        self.db.executemany(
            "INSERT OR IGNORE INTO answered VALUES (?, ?)", [(urn, now) for urn in message_urns]
        )
        self.db.commit()

    def is_answered(self, message_urn: str) -> bool:
        return self._has("SELECT 1 FROM answered WHERE message_urn = ?", message_urn)
