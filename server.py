"""Dependency-free HTTP API and static-file server for the Kanban board."""

from __future__ import annotations

import json
import mimetypes
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from database import VALID_STATUSES, get_connection, initialize_database, utc_timestamp


HOST = "127.0.0.1"
PORT = 8000
STATIC_DIR = Path(__file__).with_name("static").resolve()
MAX_BODY_BYTES = 16_384


def task_from_row(row: object) -> dict[str, object]:
    """Convert a sqlite Row to a JSON-ready task object."""
    return dict(row)  # type: ignore[arg-type]


class KanbanHandler(BaseHTTPRequestHandler):
    server_version = "KanbanHTTP/1.0"

    def _send_json(self, status: HTTPStatus, body: object | None = None) -> None:
        content = b"" if body is None else json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if content:
            self.wfile.write(content)

    def _error(self, status: HTTPStatus, message: str) -> None:
        self._send_json(status, {"error": message})

    def _read_json(self) -> dict[str, object] | None:
        value = self.headers.get("Content-Length")
        if value is None:
            self._error(HTTPStatus.LENGTH_REQUIRED, "Content-Length is required")
            return None
        try:
            length = int(value)
        except ValueError:
            self._error(HTTPStatus.BAD_REQUEST, "invalid Content-Length")
            return None
        if not 0 <= length <= MAX_BODY_BYTES:
            self._error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "request body is too large")
            return None
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._error(HTTPStatus.BAD_REQUEST, "request body must be valid JSON")
            return None
        if not isinstance(payload, dict):
            self._error(HTTPStatus.BAD_REQUEST, "request body must be a JSON object")
            return None
        return payload

    def _task_id(self) -> int | None:
        parts = urlparse(self.path).path.strip("/").split("/")
        if len(parts) != 3 or parts[:2] != ["api", "tasks"]:
            return None
        try:
            task_id = int(parts[2])
        except ValueError:
            return None
        return task_id if task_id > 0 else None

    @staticmethod
    def _valid_title(value: object) -> str | None:
        if not isinstance(value, str) or not (title := value.strip()):
            return None
        return title if len(title) <= 200 else None

    def _serve_static(self) -> None:
        path = urlparse(self.path).path
        requested = "index.html" if path == "/" else path.removeprefix("/static/")
        if path != "/" and not path.startswith("/static/"):
            self._error(HTTPStatus.NOT_FOUND, "not found")
            return
        candidate = (STATIC_DIR / requested).resolve()
        if STATIC_DIR not in candidate.parents or not candidate.is_file():
            self._error(HTTPStatus.NOT_FOUND, "not found")
            return
        content = candidate.read_bytes()
        content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self) -> None:
        if urlparse(self.path).path != "/api/tasks":
            self._serve_static()
            return
        with get_connection() as connection:
            rows = connection.execute(
                "SELECT id, title, status, created_at FROM tasks ORDER BY id DESC"
            ).fetchall()
        self._send_json(HTTPStatus.OK, [task_from_row(row) for row in rows])

    def do_POST(self) -> None:
        if urlparse(self.path).path != "/api/tasks":
            self._error(HTTPStatus.NOT_FOUND, "not found")
            return
        payload = self._read_json()
        if payload is None:
            return
        if set(payload) - {"title", "status"}:
            self._error(HTTPStatus.BAD_REQUEST, "unsupported task field")
            return
        title = self._valid_title(payload.get("title"))
        status = payload.get("status", "todo")
        if title is None:
            self._error(HTTPStatus.BAD_REQUEST, "title is required and must be 200 characters or fewer")
            return
        if status not in VALID_STATUSES:
            self._error(HTTPStatus.BAD_REQUEST, "status must be todo, in_progress, or done")
            return
        with get_connection() as connection:
            cursor = connection.execute(
                "INSERT INTO tasks (title, status, created_at) VALUES (?, ?, ?)",
                (title, status, utc_timestamp()),
            )
            row = connection.execute(
                "SELECT id, title, status, created_at FROM tasks WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
        self._send_json(HTTPStatus.CREATED, task_from_row(row))

    def do_PATCH(self) -> None:
        self._update_task()

    def do_PUT(self) -> None:
        self._update_task()

    def _update_task(self) -> None:
        task_id = self._task_id()
        if task_id is None:
            self._error(HTTPStatus.NOT_FOUND, "not found")
            return
        payload = self._read_json()
        if payload is None:
            return
        if not payload or set(payload) - {"title", "status"}:
            self._error(HTTPStatus.BAD_REQUEST, "provide title and/or status only")
            return
        updates: list[str] = []
        values: list[object] = []
        if "title" in payload:
            title = self._valid_title(payload["title"])
            if title is None:
                self._error(HTTPStatus.BAD_REQUEST, "title is required and must be 200 characters or fewer")
                return
            updates.append("title = ?")
            values.append(title)
        if "status" in payload:
            if payload["status"] not in VALID_STATUSES:
                self._error(HTTPStatus.BAD_REQUEST, "status must be todo, in_progress, or done")
                return
            updates.append("status = ?")
            values.append(payload["status"])
        values.append(task_id)
        with get_connection() as connection:
            cursor = connection.execute(
                f"UPDATE tasks SET {', '.join(updates)} WHERE id = ?", values
            )
            if cursor.rowcount == 0:
                self._error(HTTPStatus.NOT_FOUND, "task not found")
                return
            row = connection.execute(
                "SELECT id, title, status, created_at FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
        self._send_json(HTTPStatus.OK, task_from_row(row))

    def do_DELETE(self) -> None:
        task_id = self._task_id()
        if task_id is None:
            self._error(HTTPStatus.NOT_FOUND, "not found")
            return
        with get_connection() as connection:
            cursor = connection.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        if cursor.rowcount == 0:
            self._error(HTTPStatus.NOT_FOUND, "task not found")
            return
        self._send_json(HTTPStatus.NO_CONTENT)


def run(host: str = HOST, port: int = PORT) -> None:
    initialize_database()
    with ThreadingHTTPServer((host, port), KanbanHandler) as server:
        print(f"Kanban board running at http://{host}:{port}")
        server.serve_forever()


if __name__ == "__main__":
    run()
