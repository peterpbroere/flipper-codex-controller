"""One bridge owns Codex controls; companion windows share its local mailbox."""
import hashlib
import json
from pathlib import Path
import sqlite3
import time
import uuid

from .runtime import read_json, write_json


class ControlPublisher:
    def __init__(self, folder, instance):
        self.path = Path(folder) / "controls.json"
        self.instance = instance
        self.revision = ""
        self.next_update = 0

    def update(self, rows):
        value = {r["id"]: {k: r.get(k) for k in (
            "session_id", "actions", "control_token", "review", "control_hint")} for r in rows}
        revision = hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
        if revision != self.revision:
            write_json(self.path, {"instance": self.instance, "revision": revision, "rows": value})
            self.revision = revision


class CommandChannel:
    def __init__(self, folder):
        path = Path(folder) / "control-commands.sqlite3"
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), timeout=.2)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS commands (id TEXT PRIMARY KEY, instance TEXT, created REAL, "
                        "agent TEXT, session TEXT, verb TEXT, token TEXT, status TEXT, error TEXT)")
        self.db.commit()

    def enqueue(self, instance, agent, session, verb, token):
        if verb not in {"focus", "stop", "continue", "approve", "reject"}:
            raise ValueError("Unknown agent action")
        key = str(uuid.uuid4())
        with self.db:
            self.db.execute("INSERT INTO commands VALUES (?,?,?,?,?,?,?,'pending','')",
                            (key, instance, time.time(), agent, session, verb, token))
        return key

    def take(self, instance):
        with self.db:
            self.db.execute("UPDATE commands SET status='failed',error='Request expired; review again' "
                            "WHERE status='pending' AND (instance<>? OR created<?)", (instance, time.time()-5))
            row = self.db.execute("SELECT id,agent,session,verb,token FROM commands "
                                  "WHERE status='pending' AND instance=? ORDER BY created LIMIT 1", (instance,)).fetchone()
            if not row:
                return None
            changed = self.db.execute("UPDATE commands SET status='running' WHERE id=? AND status='pending'", (row[0],))
            if changed.rowcount != 1:
                return None
        return dict(zip(("id", "agent", "session", "verb", "token"), row))

    def finish(self, key, error=""):
        with self.db:
            self.db.execute("UPDATE commands SET status=?,error=? WHERE id=? AND status='running'",
                            ("failed" if error else "done", error, key))

    def result(self, key):
        return self.db.execute("SELECT status,error FROM commands WHERE id=?", (key,)).fetchone()

    def close(self):
        self.db.close()


class ControlClient:
    def __init__(self, manager):
        self.manager = manager

    def decorate(self, health, rows):
        value = read_json(self.manager.state_dir / "controls.json") if health.get("running") else {}
        valid = bool(health.get("instance")) and value.get("instance") == health.get("instance") and (
            bool(health.get("control_revision")) and value.get("revision") == health.get("control_revision"))
        for row in rows:
            view = value.get("rows", {}).get(row["id"]) if valid else None
            if view and view.get("session_id") == row["session_id"]:
                row.update(view)
            else:
                row.update(actions=[], control_token="-", review="",
                           control_hint="Start the updated bridge to enable agent controls.")
        return rows

    def execute(self, selector, session, verb, token):
        health = self.manager.health()
        rows = self.decorate(health, [{"id": selector, "session_id": session}])
        if verb not in rows[0]["actions"] or token != rows[0]["control_token"]:
            raise ValueError("Session or request changed. Review it again.")
        channel = CommandChannel(self.manager.state_dir)
        try:
            key = channel.enqueue(health["instance"], selector, session, verb, token)
            until = time.monotonic()+18
            while time.monotonic() < until:
                status, error = channel.result(key)
                if status == "done":
                    return
                if status == "failed":
                    raise RuntimeError(error)
                time.sleep(.05)
            raise RuntimeError("Action not confirmed. Check the session on PC; it was not retried.")
        finally:
            channel.close()
