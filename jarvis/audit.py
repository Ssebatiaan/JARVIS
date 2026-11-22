import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path


class Audit:
    def __init__(self, path: Path):
        self.path = path

    def record(
        self,
        request_id: str,
        action: str,
        status: str,
        elapsed_ms: float | None = None,
        *,
        step_index: int | None = None,
    ) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=1)) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY,
                    request_id TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    action TEXT NOT NULL,
                    status TEXT NOT NULL,
                    elapsed_ms REAL,
                    step_index INTEGER
                )
                """
            )
            columns = {row[1] for row in connection.execute("PRAGMA table_info(events)")}
            if "step_index" not in columns:
                connection.execute("ALTER TABLE events ADD COLUMN step_index INTEGER")
            connection.execute(
                """
                INSERT INTO events (request_id, occurred_at, action, status, elapsed_ms, step_index)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    request_id, datetime.now(timezone.utc).isoformat(),
                    action, status, elapsed_ms, step_index,
                ),
            )
