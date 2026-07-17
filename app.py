"""REST API for the lightweight Kanban board."""

import os
import sqlite3
from pathlib import Path

from flask import Flask, jsonify, request


BASE_DIR = Path(__file__).resolve().parent
SCHEMA_PATH = BASE_DIR / "schema.sql"
DATABASE_PATH = Path(os.environ.get("KANBAN_DB", BASE_DIR / "kanban.db"))
VALID_STATUSES = {"todo", "in_progress", "done"}

app = Flask(__name__)


def get_connection():
    """Open an initialized database connection for a request."""
    connection = sqlite3.connect(DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    with SCHEMA_PATH.open(encoding="utf-8") as schema_file:
        connection.executescript(schema_file.read())
    return connection


def task_dict(row):
    return dict(row)


def error(message, status=400):
    return jsonify(error=message), status


def json_object():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return None, error("Request body must be a JSON object.")
    return payload, None


def validate_payload(payload, *, partial=False):
    allowed = {"title", "description", "status"}
    unexpected = set(payload) - allowed
    if unexpected:
        return None, f"Unsupported field: {sorted(unexpected)[0]}."
    if partial and not payload:
        return None, "Provide at least one field to update."
    if not partial and "title" not in payload:
        return None, "title is required."

    clean = {}
    if "title" in payload:
        title = payload["title"]
        if not isinstance(title, str):
            return None, "title must be a string."
        title = title.strip()
        if not 1 <= len(title) <= 200:
            return None, "title must be between 1 and 200 characters."
        clean["title"] = title

    if "description" in payload:
        description = payload["description"]
        if not isinstance(description, str):
            return None, "description must be a string."
        clean["description"] = description

    if "status" in payload:
        status = payload["status"]
        if not isinstance(status, str) or status not in VALID_STATUSES:
            return None, "status must be todo, in_progress, or done."
        clean["status"] = status

    return clean, None


def fetch_task(connection, task_id):
    return connection.execute(
        "SELECT id, title, description, status, created_at, updated_at "
        "FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()


@app.get("/")
def index():
    return app.send_static_file("index.html")


@app.get("/api/tasks")
def list_tasks():
    with get_connection() as connection:
        rows = connection.execute(
            "SELECT id, title, description, status, created_at, updated_at FROM tasks "
            "ORDER BY CASE status WHEN 'todo' THEN 1 WHEN 'in_progress' THEN 2 ELSE 3 END, "
            "updated_at DESC, id DESC"
        ).fetchall()
    return jsonify([task_dict(row) for row in rows])


@app.post("/api/tasks")
def create_task():
    payload, response = json_object()
    if response:
        return response
    values, problem = validate_payload(payload)
    if problem:
        return error(problem)

    with get_connection() as connection:
        cursor = connection.execute(
            "INSERT INTO tasks (title, description, status) VALUES (?, ?, ?)",
            (values["title"], values.get("description", ""), values.get("status", "todo")),
        )
        task = fetch_task(connection, cursor.lastrowid)
    return jsonify(task_dict(task)), 201


@app.get("/api/tasks/<int:task_id>")
def get_task(task_id):
    with get_connection() as connection:
        task = fetch_task(connection, task_id)
    if task is None:
        return error("Task not found.", 404)
    return jsonify(task_dict(task))


@app.patch("/api/tasks/<int:task_id>")
def update_task(task_id):
    payload, response = json_object()
    if response:
        return response
    values, problem = validate_payload(payload, partial=True)
    if problem:
        return error(problem)

    assignments = ", ".join(f"{field} = ?" for field in values)
    with get_connection() as connection:
        if fetch_task(connection, task_id) is None:
            return error("Task not found.", 404)
        connection.execute(
            f"UPDATE tasks SET {assignments} WHERE id = ?",
            (*values.values(), task_id),
        )
        task = fetch_task(connection, task_id)
    return jsonify(task_dict(task))


@app.delete("/api/tasks/<int:task_id>")
def delete_task(task_id):
    with get_connection() as connection:
        deleted = connection.execute("DELETE FROM tasks WHERE id = ?", (task_id,)).rowcount
    if not deleted:
        return error("Task not found.", 404)
    return "", 204


if __name__ == "__main__":
    app.run(debug=True)
