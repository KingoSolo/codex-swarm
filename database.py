"""SQLite persistence and schema initialization for the Kanban board."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path


DATABASE_PATH = Path(__file__).with_name("kanban.db")
VALID_STATUSES = frozenset({"todo", "in_progress", "done"})


def get_connection() -> sqlite3.Connection:
    """Return a connection configured for named-column access."""
    connection = sqlite3.connect(DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def initialize_database() -> None:
    """Create the tasks table when the application starts."""
    with get_connection() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'todo'
                    CHECK (status IN ('todo', 'in_progress', 'done')),
                created_at TEXT NOT NULL
            )
            """
        )


def utc_timestamp() -> str:
    """Return an ISO-8601 timestamp suitable for the created_at column."""
    return datetime.now(timezone.utc).isoformat()
