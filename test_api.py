"""Regression tests for authenticated Kanban API behavior without sockets."""

from __future__ import annotations

import os
import tempfile
import unittest
import hashlib
from pathlib import Path
from unittest.mock import MagicMock, patch

import database
import server
from server import KanbanHandler, create_token


class HandlerHarness(KanbanHandler):
    """In-process handler harness that captures JSON responses."""

    def __init__(
        self, path: str, payload: dict[str, object] | None = None, token: str | None = None
    ) -> None:
        self.path = path
        self.payload = payload
        self.headers = {} if token is None else {"Authorization": f"Bearer {token}"}
        self.response: tuple[int, object | None] | None = None

    def _read_json(self) -> dict[str, object] | None:
        return self.payload

    def _send_json(self, status: object, body: object | None = None) -> None:
        self.response = (int(status), body)

    def _error(self, status: object, message: str) -> None:
        self._send_json(status, {"error": message})


class KanbanApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_database_path = database.DATABASE_PATH
        self.original_secret = os.environ.get("KANBAN_JWT_SECRET")
        os.environ["KANBAN_JWT_SECRET"] = "test-secret-with-at-least-thirty-two-bytes"
        database.DATABASE_PATH = Path(self.temp_dir.name) / "kanban.db"
        database.initialize_database()

    def tearDown(self) -> None:
        database.DATABASE_PATH = self.original_database_path
        if self.original_secret is None:
            os.environ.pop("KANBAN_JWT_SECRET", None)
        else:
            os.environ["KANBAN_JWT_SECRET"] = self.original_secret
        self.temp_dir.cleanup()

    def request(
        self, method: str, path: str, payload: dict[str, object] | None = None, token: str | None = None
    ) -> tuple[int, object | None]:
        handler = HandlerHarness(path, payload, token)
        getattr(handler, f"do_{method}")()
        assert handler.response is not None
        return handler.response

    def register(self, username: str, password: str = "correct horse battery") -> dict[str, object]:
        status, response = self.request("POST", "/api/auth/register", {"username": username, "password": password})
        self.assertEqual(status, 201)
        self.assertIsInstance(response, dict)
        return response

    def test_registration_login_and_password_storage(self) -> None:
        registered = self.register("Alice")
        self.assertEqual(registered["user"]["username"], "alice")
        self.assertIsInstance(registered["token"], str)

        with database.managed_connection() as connection:
            row = connection.execute("SELECT password_hash, password_salt FROM users WHERE username = ?", ("alice",)).fetchone()
        self.assertNotEqual(row["password_hash"], b"correct horse battery")
        self.assertNotEqual(row["password_salt"], b"correct horse battery")

        status, _ = self.request("POST", "/api/auth/register", {"username": "ALICE", "password": "correct horse battery"})
        self.assertEqual(status, 409)
        status, logged_in = self.request("POST", "/api/auth/login", {"username": "ALICE", "password": "correct horse battery"})
        self.assertEqual(status, 200)
        self.assertEqual(logged_in["user"]["username"], "alice")
        status, _ = self.request("POST", "/api/auth/login", {"username": "alice", "password": "wrong password"})
        self.assertEqual(status, 401)

    def test_unknown_user_login_performs_dummy_pbkdf2_and_matches_failure_response(self) -> None:
        self.register("alice")
        password = "wrong password"
        with patch("server.hashlib.pbkdf2_hmac", wraps=hashlib.pbkdf2_hmac) as derive:
            known_status, known_response = self.request(
                "POST", "/api/auth/login", {"username": "alice", "password": password}
            )
            unknown_status, unknown_response = self.request(
                "POST", "/api/auth/login", {"username": "nobody", "password": password}
            )

        self.assertEqual((unknown_status, unknown_response), (known_status, known_response))
        self.assertEqual((known_status, known_response), (401, {"error": "invalid username or password"}))
        self.assertEqual(derive.call_count, 2)
        unknown_call = derive.call_args_list[1].args
        self.assertEqual(
            unknown_call,
            ("sha256", password.encode("utf-8"), server.DUMMY_PASSWORD_SALT, server.PASSWORD_ITERATIONS),
        )

    def test_registration_rejects_confusable_unicode_username(self) -> None:
        status, _ = self.request(
            "POST",
            "/api/auth/register",
            {"username": "аlice", "password": "correct horse battery"},
        )
        self.assertEqual(status, 400)

    def test_protected_lifecycle_and_user_isolation(self) -> None:
        alice = self.register("alice")
        bob = self.register("bob")
        alice_token, bob_token = alice["token"], bob["token"]

        status, _ = self.request("GET", "/api/tasks")
        self.assertEqual(status, 401)
        status, task = self.request("POST", "/api/tasks", {"title": "Private task"}, alice_token)
        self.assertEqual(status, 201)
        task_id = task["id"]

        status, bob_tasks = self.request("GET", "/api/tasks", token=bob_token)
        self.assertEqual(status, 200)
        self.assertEqual(bob_tasks, [])
        status, _ = self.request("PATCH", f"/api/tasks/{task_id}", {"status": "done"}, bob_token)
        self.assertEqual(status, 404)
        status, _ = self.request("DELETE", f"/api/tasks/{task_id}", token=bob_token)
        self.assertEqual(status, 404)

        status, updated = self.request("PATCH", f"/api/tasks/{task_id}", {"status": "in_progress"}, alice_token)
        self.assertEqual(status, 200)
        self.assertEqual(updated["status"], "in_progress")
        status, _ = self.request("DELETE", f"/api/tasks/{task_id}", token=alice_token)
        self.assertEqual(status, 204)

    def test_task_search_filter_sort_and_user_isolation(self) -> None:
        alice = self.register("alice")
        bob = self.register("bob")
        alice_token, bob_token = alice["token"], bob["token"]
        for title, task_status in (
            ("Beta report", "todo"),
            ("alpha report", "done"),
            ("100% complete", "done"),
        ):
            status, _ = self.request(
                "POST", "/api/tasks", {"title": title, "status": task_status}, alice_token
            )
            self.assertEqual(status, 201)
        status, _ = self.request("POST", "/api/tasks", {"title": "Alice report"}, bob_token)
        self.assertEqual(status, 201)

        status, results = self.request(
            "GET", "/api/tasks?q=report&sort=title_asc", token=alice_token
        )
        self.assertEqual(status, 200)
        self.assertEqual([task["title"] for task in results], ["alpha report", "Beta report"])
        status, results = self.request("GET", "/api/tasks?status=done&q=%25", token=alice_token)
        self.assertEqual(status, 200)
        self.assertEqual([task["title"] for task in results], ["100% complete"])
        status, results = self.request("GET", "/api/tasks?q=report", token=bob_token)
        self.assertEqual(status, 200)
        self.assertEqual([task["title"] for task in results], ["Alice report"])

        for path in ("/api/tasks?sort=title;DROP", "/api/tasks?status=todo&status=done", "/api/tasks?unknown=x"):
            status, _ = self.request("GET", path, token=alice_token)
            self.assertEqual(status, 400)

    def test_invalid_and_expired_tokens_are_rejected(self) -> None:
        alice = self.register("alice")
        status, _ = self.request("GET", "/api/tasks", token=f"{alice['token']}tampered")
        self.assertEqual(status, 401)
        with patch("server.time.time", return_value=1_000):
            expired_token = create_token(alice["user"]["id"])
        status, _ = self.request("GET", "/api/tasks", token=expired_token)
        self.assertEqual(status, 401)

    def test_frontend_auth_smoke_contract(self) -> None:
        source = Path(__file__).with_name("static").joinpath("index.html").read_text()
        self.assertIn("sessionStorage", source)
        self.assertIn("Authorization", source)
        self.assertIn("/api/auth/", source)
        self.assertIn("response.status === 401", source)

    def test_static_html_csp_allows_only_hashed_inline_assets(self) -> None:
        headers: dict[str, str] = {}
        handler = object.__new__(KanbanHandler)
        handler.send_header = lambda name, value: headers.__setitem__(name, value)

        handler._send_security_headers(html=True)

        csp = headers["Content-Security-Policy"]
        self.assertNotIn("unsafe-inline", csp)
        self.assertGreaterEqual(csp.count("sha256-"), 2)

    def test_managed_connection_closes_after_use(self) -> None:
        connection = MagicMock()
        with patch.object(database, "get_connection", return_value=connection):
            with database.managed_connection() as yielded:
                self.assertIs(yielded, connection)

        connection.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
