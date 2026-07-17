"""SQLite persistence for the Kanban task board."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


DATABASE_PATH = Path(__file__).with_name("kanban.db")
VALID_STATUSES = frozenset({"todo", "in_progress", "done"})


def _connection() -> sqlite3.Connection:
    connection = sqlite3.connect(DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _validate_title(title: Any) -> str:
    if not isinstance(title, str) or not (clean_title := title.strip()):
        raise ValueError("title is required")
    if len(clean_title) > 200:
        raise ValueError("title must be 200 characters or fewer")
    return clean_title


def _validate_description(description: Any) -> str:
    if not isinstance(description, str):
        raise ValueError("description must be a string")
    return description


def _validate_status(status: Any) -> str:
    if status not in VALID_STATUSES:
        raise ValueError("status must be todo, in_progress, or done")
    return str(status)


def _task(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def initialize_database() -> None:
    """Create the durable task table if it has not been created yet."""
    with _connection() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL CHECK(length(title) <= 200),
                description TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'todo'
                    CHECK(status IN ('todo', 'in_progress', 'done')),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )


def list_tasks() -> list[dict[str, Any]]:
    initialize_database()
    with _connection() as connection:
        rows = connection.execute(
            "SELECT id, title, description, status, created_at, updated_at "
            "FROM tasks ORDER BY id DESC"
        ).fetchall()
    return [dict(row) for row in rows]


def create_task(title: Any, description: Any = "", status: Any = "todo") -> dict[str, Any]:
    initialize_database()
    clean_title = _validate_title(title)
    clean_description = _validate_description(description)
    clean_status = _validate_status(status)
    now = _timestamp()
    with _connection() as connection:
        cursor = connection.execute(
            "INSERT INTO tasks (title, description, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (clean_title, clean_description, clean_status, now, now),
        )
        row = connection.execute(
            "SELECT id, title, description, status, created_at, updated_at "
            "FROM tasks WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
    return _task(row)  # type: ignore[return-value]


def update_task(task_id: int, fields: Mapping[str, Any]) -> dict[str, Any] | None:
    """Apply the supplied mutable fields, returning None when the task is absent."""
    allowed = {"title", "description", "status"}
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError("unsupported task field")
    if not fields:
        raise ValueError("at least one task field is required")

    updates: dict[str, Any] = {}
    if "title" in fields:
        updates["title"] = _validate_title(fields["title"])
    if "description" in fields:
        updates["description"] = _validate_description(fields["description"])
    if "status" in fields:
        updates["status"] = _validate_status(fields["status"])
    updates["updated_at"] = _timestamp()

    assignments = ", ".join(f"{column} = ?" for column in updates)
    values = [*updates.values(), task_id]
    initialize_database()
    with _connection() as connection:
        cursor = connection.execute(
            f"UPDATE tasks SET {assignments} WHERE id = ?", values
        )
        if cursor.rowcount == 0:
            return None
        row = connection.execute(
            "SELECT id, title, description, status, created_at, updated_at "
            "FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
    return _task(row)


def delete_task(task_id: int) -> bool:
    """Delete a task and report whether it existed."""
    initialize_database()
    with _connection() as connection:
        cursor = connection.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    return cursor.rowcount > 0
