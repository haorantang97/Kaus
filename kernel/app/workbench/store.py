"""Independent additive storage. No changes to the application's domain schema."""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path


class WorkbenchStore:
    def __init__(self, root: Path):
        root.mkdir(parents=True, exist_ok=True)
        self.path = root / "workbench.sqlite3"
        self._lock = threading.RLock()
        with self.connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS documents (
                    bucket TEXT NOT NULL, id TEXT NOT NULL, value TEXT NOT NULL,
                    PRIMARY KEY(bucket,id));
                CREATE TABLE IF NOT EXISTS module_conversations (
                    conversation_id TEXT PRIMARY KEY, module_id TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS idempotency (
                    module_id TEXT NOT NULL, key TEXT NOT NULL, fingerprint TEXT NOT NULL,
                    job_id TEXT NOT NULL, PRIMARY KEY(module_id,key));
            """)
        self.path.chmod(0o600)

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def get(self, bucket: str, key: str):
        with self._lock, self.connect() as connection:
            row = connection.execute("SELECT value FROM documents WHERE bucket=? AND id=?", (bucket, key)).fetchone()
            return json.loads(row[0]) if row else None

    def all(self, bucket: str):
        with self._lock, self.connect() as connection:
            return [json.loads(row[0]) for row in connection.execute("SELECT value FROM documents WHERE bucket=? ORDER BY id", (bucket,))]

    def put(self, bucket: str, key: str, value):
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
        with self._lock, self.connect() as connection:
            connection.execute("INSERT INTO documents VALUES(?,?,?) ON CONFLICT(bucket,id) DO UPDATE SET value=excluded.value", (bucket, key, encoded))
        return value

    def remember_conversation(self, conversation_id: str, module_id: str):
        with self._lock, self.connect() as connection:
            connection.execute("INSERT OR IGNORE INTO module_conversations VALUES(?,?)", (conversation_id, module_id))

    def is_module_conversation(self, conversation_id: str) -> bool:
        with self._lock, self.connect() as connection:
            return connection.execute("SELECT 1 FROM module_conversations WHERE conversation_id=?", (conversation_id,)).fetchone() is not None

    def module_conversation_ids(self) -> set[str]:
        with self._lock, self.connect() as connection:
            return {row[0] for row in connection.execute("SELECT conversation_id FROM module_conversations")}

    def idempotent(self, module_id: str, key: str):
        with self._lock, self.connect() as connection:
            row = connection.execute("SELECT fingerprint,job_id FROM idempotency WHERE module_id=? AND key=?", (module_id, key)).fetchone()
            return tuple(row) if row else None

    def create_job(self, module_id: str, key: str, fingerprint: str, job):
        with self._lock, self.connect() as connection:
            connection.execute("INSERT INTO idempotency VALUES(?,?,?,?)", (module_id, key, fingerprint, job["id"]))
            connection.execute("INSERT INTO documents VALUES('jobs',?,?)", (job["id"], json.dumps(job, ensure_ascii=False)))
