"""A local durable event journal: hooks work even while the bridge is stopped."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import time
from contextlib import closing


def default_store():
    if os.environ.get("AGENT_DECK_STATE_DIR"):
        return Path(os.environ["AGENT_DECK_STATE_DIR"]) / "events.sqlite3"
    base = Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local/share"))
    return base / "FlipperAgentDeck" / "events.sqlite3"


class EventStore:
    def __init__(self, path=None):
        self.path = Path(path) if path else default_store()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path), timeout=0.5)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, received REAL NOT NULL, payload TEXT NOT NULL)")
        self.db.commit()

    def append(self, payload):
        with self.db:
            cursor = self.db.execute("INSERT INTO events(received,payload) VALUES (?,?)", (time.time(), json.dumps(payload)))
        return cursor.lastrowid

    def read(self, after=0, limit=1000):
        result = []
        for index, received, payload in self.db.execute(
                "SELECT id,received,payload FROM events WHERE id>? ORDER BY id LIMIT ?", (after, limit)):
            event = json.loads(payload)
            event["observed_at"] = received
            result.append((index, event))
        return result

    def close(self):
        self.db.close()


class EventReader:
    """Read the journal without creating a database or running migrations."""
    def __init__(self, path=None):
        self.path = Path(path) if path else default_store()

    def read(self, after=0, limit=1000):
        if not self.path.exists():
            return []
        with closing(sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.2)) as db:
            rows = db.execute("SELECT id,received,payload FROM events WHERE id>? ORDER BY id LIMIT ?", (after, limit)).fetchall()
        result = []
        for index, received, payload in rows:
            event = json.loads(payload)
            event["observed_at"] = received
            result.append((index, event))
        return result
