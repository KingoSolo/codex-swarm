"""Dependency-free HTTP server for the Kanban board."""

from __future__ import annotations

import json
import mimetypes
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from database import create_task, delete_task, initialize_database, list_tasks, update_task


HOST = "127.0.0.1"
PORT = 8000
STATIC_DIR = Path(__file__).with_name("static").resolve()
MAX_BODY_BYTES = 16_384


class KanbanHandler(BaseHTTPRequestHandler):
    server_version = "KanbanHTTP/1.0"

    def log_message(self, format: str, *args: object) -> None:
        """Keep normal request logging, with the parent handler implementation."""
        super().log_message(format, *args)

    def _json(self, status: HTTPStatus, payload: object | None = None) -> None:
        body = b"" if payload is None else json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _error(self, status: HTTPStatus, message: str) -> None:
        self._json(status, {"error": message})

    def _read_json(self) -> dict[str, object] | None:
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            self._error(HTTPStatus.LENGTH_REQUIRED, "Content-Length is required")
            return None
        try:
            length = int(raw_length)
        except ValueError:
            self._error(HTTPStatus.BAD_REQUEST, "invalid Content-Length")
            return None
        if length < 0 or length > MAX_BODY_BYTES:
            self._error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "request body is too large")
            return None
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._error(HTTPStatus.BAD_REQUEST, "request body must be valid JSON")
            return None
        if not isinstance(value, dict):
            self._error(HTTPStatus.BAD_REQUEST, "request body must be a JSON object")
            return None
        return value

    def _task_id(self) -> int | None:
        parts = urlparse(self.path).path.strip("/").split("/")
        if len(parts) != 3 or parts[:2] != ["api", "tasks"]:
            return None
        try:
            task_id = int(parts[2])
        except ValueError:
            return None
        return task_id if task_id > 0 else None

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
        if urlparse(self.path).path == "/api/tasks":
            self._json(HTTPStatus.OK, list_tasks())
            return
        self._serve_static()

    def do_POST(self) -> None:
        if urlparse(self.path).path != "/api/tasks":
            self._error(HTTPStatus.NOT_FOUND, "not found")
            return
        payload = self._read_json()
        if payload is None:
            return
        try:
            task = create_task(
                payload.get("title"), payload.get("description", ""), payload.get("status", "todo")
            )
        except ValueError as error:
            self._error(HTTPStatus.BAD_REQUEST, str(error))
            return
        self._json(HTTPStatus.CREATED, task)

    def _update(self) -> None:
        task_id = self._task_id()
        if task_id is None:
            self._error(HTTPStatus.NOT_FOUND, "not found")
            return
        payload = self._read_json()
        if payload is None:
            return
        try:
            task = update_task(task_id, payload)
        except ValueError as error:
            self._error(HTTPStatus.BAD_REQUEST, str(error))
            return
        if task is None:
            self._error(HTTPStatus.NOT_FOUND, "task not found")
            return
        self._json(HTTPStatus.OK, task)

    def do_PUT(self) -> None:
        self._update()

    def do_PATCH(self) -> None:
        self._update()

    def do_DELETE(self) -> None:
        task_id = self._task_id()
        if task_id is None:
            self._error(HTTPStatus.NOT_FOUND, "not found")
            return
        if not delete_task(task_id):
            self._error(HTTPStatus.NOT_FOUND, "task not found")
            return
        self._json(HTTPStatus.NO_CONTENT)

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Allow", "GET, POST, PUT, PATCH, DELETE, OPTIONS")
        self.end_headers()


def run(host: str = HOST, port: int = PORT) -> None:
    initialize_database()
    with ThreadingHTTPServer((host, port), KanbanHandler) as server:
        print(f"Kanban board running at http://{host}:{port}")
        server.serve_forever()


if __name__ == "__main__":
    run()
