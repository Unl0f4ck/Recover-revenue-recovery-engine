"""Durable create-operation claims. An uncertain write is reconciled, never replayed."""
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path


class PendingOperation(RuntimeError):
    pass


class Operations:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS operations (key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, result TEXT)")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        try:
            with db:
                yield db
        finally:
            db.close()

    def claim(self, key, fingerprint):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT fingerprint, result FROM operations WHERE key=?", (key,)).fetchone()
            if row:
                if row[0] != fingerprint:
                    raise ValueError("idempotency key reused with a different payment request")
                return False, json.loads(row[1]) if row[1] else None
            db.execute("INSERT INTO operations VALUES (?, ?, NULL)", (key, fingerprint))
            return True, None

    def finish(self, key, result):
        with self.connect() as db:
            db.execute("UPDATE operations SET result=? WHERE key=?", (json.dumps(result), key))
