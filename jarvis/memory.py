import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .tools import MAX_MEMORY_TEXT, MemoryFull, MemoryItem

MAX_FACTS = 50
HISTORY_LIMIT = 20


@dataclass(frozen=True)
class Exchange:
    user_text: str
    reply: str


class MemoryStore:
    """Memoria local de JARVIS: datos que el usuario quiere que recuerde e historial reciente."""

    def __init__(self, path: Path):
        self.path = path

    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=1)
        connection.execute(
            "CREATE TABLE IF NOT EXISTS facts ("
            "id INTEGER PRIMARY KEY, text TEXT NOT NULL, created_at TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS history ("
            "id INTEGER PRIMARY KEY, user_text TEXT NOT NULL, reply TEXT NOT NULL, "
            "created_at TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        return connection

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def add_fact(self, text: str) -> int:
        text = " ".join(text.split())
        if not 0 < len(text) <= MAX_MEMORY_TEXT:
            raise ValueError("El recuerdo debe tener entre 1 y 300 caracteres.")
        with closing(self._connect()) as connection, connection:
            existing = connection.execute(
                "SELECT id FROM facts WHERE lower(text) = lower(?)", (text,)
            ).fetchone()
            if existing:
                return existing[0]
            (count,) = connection.execute("SELECT COUNT(*) FROM facts").fetchone()
            if count >= MAX_FACTS:
                raise MemoryFull()
            cursor = connection.execute(
                "INSERT INTO facts (text, created_at) VALUES (?, ?)", (text, self._now())
            )
            return cursor.lastrowid

    def facts(self) -> list[MemoryItem]:
        with closing(self._connect()) as connection:
            rows = connection.execute("SELECT id, text FROM facts ORDER BY id").fetchall()
        return [MemoryItem(row[0], row[1]) for row in rows]

    def forget_fact(self, fact_id: int) -> bool:
        with closing(self._connect()) as connection, connection:
            return connection.execute("DELETE FROM facts WHERE id = ?", (fact_id,)).rowcount > 0

    def forget_everything(self) -> int:
        with closing(self._connect()) as connection, connection:
            removed = connection.execute("DELETE FROM facts").rowcount
            connection.execute("DELETE FROM history")
        return removed

    def add_exchange(self, user_text: str, reply: str) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "INSERT INTO history (user_text, reply, created_at) VALUES (?, ?, ?)",
                (user_text[:500], reply[:500], self._now()),
            )
            connection.execute(
                "DELETE FROM history WHERE id NOT IN "
                "(SELECT id FROM history ORDER BY id DESC LIMIT ?)",
                (HISTORY_LIMIT,),
            )

    def recent_exchanges(self, limit: int = 10) -> list[Exchange]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT user_text, reply FROM history ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [Exchange(row[0], row[1]) for row in reversed(rows)]

    def get_setting(self, key: str, default: str | None = None) -> str | None:
        """Preferencias de la interfaz. «olvida todo» no las borra."""
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default

    def set_setting(self, key: str, value: str) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
