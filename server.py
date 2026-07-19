"""Dependency-free HTTP API and static-file server for the Kanban board."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import mimetypes
import os
import secrets
import sqlite3
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from database import VALID_STATUSES, get_connection, initialize_database, utc_timestamp


HOST = "127.0.0.1"
PORT = 8000
STATIC_DIR = Path(__file__).with_name("static").resolve()
MAX_BODY_BYTES = 16_384
PASSWORD_ITERATIONS = 310_000
TOKEN_LIFETIME_SECONDS = 3_600


def task_from_row(row: object) -> dict[str, object]:
    """Convert a sqlite Row to a JSON-ready task object."""
    return dict(row)  # type: ignore[arg-type]


def _base64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _base64url_decode(value: str) -> bytes:
    if not value or any(character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_" for character in value):
        raise ValueError("invalid token encoding")
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, binascii.Error) as error:
        raise ValueError("invalid token encoding") from error


def _signing_secret() -> bytes:
    value = os.environ.get("KANBAN_JWT_SECRET", "")
    if len(value.encode("utf-8")) < 32:
        raise RuntimeError("KANBAN_JWT_SECRET must be at least 32 bytes")
    return value.encode("utf-8")


def create_token(user_id: int) -> str:
    """Create a short-lived HS256 JWT for one user."""
    now = int(time.time())
    header = _base64url_encode(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode("utf-8"))
    payload = _base64url_encode(json.dumps({"sub": user_id, "iat": now, "exp": now + TOKEN_LIFETIME_SECONDS}, separators=(",", ":")).encode("utf-8"))
    signed_value = f"{header}.{payload}".encode("ascii")
    signature = hmac.new(_signing_secret(), signed_value, hashlib.sha256).digest()
    return f"{header}.{payload}.{_base64url_encode(signature)}"


def verify_token(token: str) -> int:
    """Return the user id encoded in a valid, unexpired HS256 JWT."""
    try:
        header_part, payload_part, signature_part = token.split(".")
        header = json.loads(_base64url_decode(header_part))
        payload = json.loads(_base64url_decode(payload_part))
        actual_signature = _base64url_decode(signature_part)
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("invalid token") from None
    if not isinstance(header, dict) or header.get("alg") != "HS256" or header.get("typ") != "JWT":
        raise ValueError("invalid token")
    expected_signature = hmac.new(
        _signing_secret(), f"{header_part}.{payload_part}".encode("ascii"), hashlib.sha256
    ).digest()
    if not hmac.compare_digest(actual_signature, expected_signature):
        raise ValueError("invalid token")
    if not isinstance(payload, dict):
        raise ValueError("invalid token")
    user_id, issued_at, expires_at = payload.get("sub"), payload.get("iat"), payload.get("exp")
    if (
        isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0
        or isinstance(issued_at, bool) or not isinstance(issued_at, int)
        or isinstance(expires_at, bool) or not isinstance(expires_at, int)
        or issued_at > int(time.time()) + 60 or expires_at <= int(time.time())
    ):
        raise ValueError("invalid token")
    return user_id


class KanbanHandler(BaseHTTPRequestHandler):
    server_version = "KanbanHTTP/1.0"

    def _send_security_headers(self, *, html: bool = False) -> None:
        """Add browser protections to API and static-file responses."""
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")
        if html:
            # The single-file UI contains its own script and styles, so these
            # directives must temporarily allow inline content.  All external
            # resources, plugins, frames, and form targets remain disallowed.
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                "style-src 'self' 'unsafe-inline'; object-src 'none'; "
                "base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
            )

    def _send_json(self, status: HTTPStatus, body: object | None = None) -> None:
        content = b"" if body is None else json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self._send_security_headers()
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

    @staticmethod
    def _valid_username(value: object) -> str | None:
        if not isinstance(value, str):
            return None
        username = value.strip().lower()
        if not 3 <= len(username) <= 64:
            return None
        # Keep login identifiers ASCII-only.  SQLite's NOCASE collation is
        # ASCII-oriented; accepting visually confusable Unicode usernames
        # would otherwise permit distinct accounts that appear identical.
        return username if all(
            character.isascii() and (character.isalnum() or character in "_.-")
            for character in username
        ) else None

    @staticmethod
    def _valid_password(value: object) -> str | None:
        return value if isinstance(value, str) and 8 <= len(value) <= 256 else None

    def _authenticated_user_id(self) -> int | None:
        authorization = self.headers.get("Authorization", "")
        scheme, _, token = authorization.partition(" ")
        if scheme != "Bearer" or not token or " " in token:
            self._error(HTTPStatus.UNAUTHORIZED, "authentication is required")
            return None
        try:
            return verify_token(token)
        except ValueError:
            self._error(HTTPStatus.UNAUTHORIZED, "invalid or expired token")
            return None

    def _credentials(self) -> tuple[str, str] | None:
        payload = self._read_json()
        if payload is None:
            return None
        if set(payload) != {"username", "password"}:
            self._error(HTTPStatus.BAD_REQUEST, "username and password are required")
            return None
        username = self._valid_username(payload.get("username"))
        password = self._valid_password(payload.get("password"))
        if username is None or password is None:
            self._error(HTTPStatus.BAD_REQUEST, "username or password is invalid")
            return None
        return username, password

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
        self._send_security_headers(html=candidate.suffix == ".html")
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self) -> None:
        if urlparse(self.path).path != "/api/tasks":
            self._serve_static()
            return
        user_id = self._authenticated_user_id()
        if user_id is None:
            return
        with get_connection() as connection:
            rows = connection.execute(
                "SELECT id, title, status, created_at FROM tasks WHERE user_id = ? ORDER BY id DESC",
                (user_id,),
            ).fetchall()
        self._send_json(HTTPStatus.OK, [task_from_row(row) for row in rows])

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/auth/register":
            self._register()
            return
        if path == "/api/auth/login":
            self._login()
            return
        if path != "/api/tasks":
            self._error(HTTPStatus.NOT_FOUND, "not found")
            return
        user_id = self._authenticated_user_id()
        if user_id is None:
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
                "INSERT INTO tasks (title, status, created_at, user_id) VALUES (?, ?, ?, ?)",
                (title, status, utc_timestamp(), user_id),
            )
            row = connection.execute(
                "SELECT id, title, status, created_at FROM tasks WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
        self._send_json(HTTPStatus.CREATED, task_from_row(row))

    def _register(self) -> None:
        credentials = self._credentials()
        if credentials is None:
            return
        username, password = credentials
        salt = secrets.token_bytes(16)
        password_hash = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS)
        try:
            with get_connection() as connection:
                cursor = connection.execute(
                    "INSERT INTO users (username, password_hash, password_salt, created_at) VALUES (?, ?, ?, ?)",
                    (username, password_hash, salt, utc_timestamp()),
                )
        except sqlite3.IntegrityError:
            self._error(HTTPStatus.CONFLICT, "username is already registered")
            return
        self._send_json(HTTPStatus.CREATED, {"token": create_token(cursor.lastrowid), "user": {"id": cursor.lastrowid, "username": username}})

    def _login(self) -> None:
        credentials = self._credentials()
        if credentials is None:
            return
        username, password = credentials
        with get_connection() as connection:
            row = connection.execute(
                "SELECT id, username, password_hash, password_salt FROM users WHERE username = ?", (username,)
            ).fetchone()
        if row is None:
            self._error(HTTPStatus.UNAUTHORIZED, "invalid username or password")
            return
        password_hash = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), row["password_salt"], PASSWORD_ITERATIONS)
        if not hmac.compare_digest(password_hash, row["password_hash"]):
            self._error(HTTPStatus.UNAUTHORIZED, "invalid username or password")
            return
        self._send_json(HTTPStatus.OK, {"token": create_token(row["id"]), "user": {"id": row["id"], "username": row["username"]}})

    def do_PATCH(self) -> None:
        self._update_task()

    def do_PUT(self) -> None:
        self._update_task()

    def _update_task(self) -> None:
        task_id = self._task_id()
        if task_id is None:
            self._error(HTTPStatus.NOT_FOUND, "not found")
            return
        user_id = self._authenticated_user_id()
        if user_id is None:
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
        values.append(user_id)
        with get_connection() as connection:
            cursor = connection.execute(
                f"UPDATE tasks SET {', '.join(updates)} WHERE id = ? AND user_id = ?", values
            )
            if cursor.rowcount == 0:
                self._error(HTTPStatus.NOT_FOUND, "task not found")
                return
            row = connection.execute(
                "SELECT id, title, status, created_at FROM tasks WHERE id = ? AND user_id = ?", (task_id, user_id)
            ).fetchone()
        self._send_json(HTTPStatus.OK, task_from_row(row))

    def do_DELETE(self) -> None:
        task_id = self._task_id()
        if task_id is None:
            self._error(HTTPStatus.NOT_FOUND, "not found")
            return
        user_id = self._authenticated_user_id()
        if user_id is None:
            return
        with get_connection() as connection:
            cursor = connection.execute("DELETE FROM tasks WHERE id = ? AND user_id = ?", (task_id, user_id))
        if cursor.rowcount == 0:
            self._error(HTTPStatus.NOT_FOUND, "task not found")
            return
        self._send_json(HTTPStatus.NO_CONTENT)


def run(host: str = HOST, port: int = PORT) -> None:
    _signing_secret()
    initialize_database()
    with ThreadingHTTPServer((host, port), KanbanHandler) as server:
        print(f"Kanban board running at http://{host}:{port}")
        server.serve_forever()


if __name__ == "__main__":
    run()
