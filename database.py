"""SQLite persistence and schema initialization for the Kanban board."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


DATABASE_PATH = Path(__file__).with_name("kanban.db")
VALID_STATUSES = frozenset({"todo", "in_progress", "done"})


def get_connection() -> sqlite3.Connection:
    """Return a connection configured for named-column access."""
    connection = sqlite3.connect(DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


@contextmanager
def managed_connection() -> sqlite3.Connection:
    """Yield a transaction connection and always close it afterwards."""
    connection = get_connection()
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def initialize_database() -> None:
    """Create and migrate the task and user tables."""
    with managed_connection() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL COLLATE NOCASE UNIQUE
                    CHECK(length(username) BETWEEN 3 AND 64),
                password_hash BLOB NOT NULL,
                password_salt BLOB NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'todo'
                    CHECK (status IN ('todo', 'in_progress', 'done')),
                created_at TEXT NOT NULL,
                user_id INTEGER REFERENCES users(id)
            )
            """
        )
        task_columns = {
            column["name"]
            for column in connection.execute("PRAGMA table_info(tasks)").fetchall()
        }
        if "user_id" not in task_columns:
            connection.execute("ALTER TABLE tasks ADD COLUMN user_id INTEGER REFERENCES users(id)")
        connection.execute("CREATE INDEX IF NOT EXISTS tasks_user_id_idx ON tasks(user_id)")


def utc_timestamp() -> str:
    """Return an ISO-8601 timestamp suitable for the created_at column."""
    return datetime.now(timezone.utc).isoformat()
