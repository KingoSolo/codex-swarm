"""Regression tests for the Kanban API handler without opening a network socket."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import database
from server import KanbanHandler


class HandlerHarness(KanbanHandler):
    """In-process handler harness that captures JSON responses."""

    def __init__(self, path: str, payload: dict[str, object] | None = None) -> None:
        self.path = path
        self.payload = payload
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
        database.DATABASE_PATH = Path(self.temp_dir.name) / "kanban.db"
        database.initialize_database()

    def tearDown(self) -> None:
        database.DATABASE_PATH = self.original_database_path
        self.temp_dir.cleanup()

    def test_task_lifecycle_and_validation(self) -> None:
        create = HandlerHarness("/api/tasks", {"title": "Write tests"})
        create.do_POST()
        self.assertEqual(create.response[0], 201)
        task = create.response[1]
        self.assertEqual(task["status"], "todo")

        task_id = task["id"]
        update = HandlerHarness(f"/api/tasks/{task_id}", {"status": "in_progress", "title": "Test API"})
        update.do_PATCH()
        self.assertEqual(update.response[0], 200)
        self.assertEqual(update.response[1]["status"], "in_progress")
        self.assertEqual(update.response[1]["title"], "Test API")

        listing = HandlerHarness("/api/tasks")
        listing.do_GET()
        self.assertEqual(listing.response[1], [update.response[1]])

        delete = HandlerHarness(f"/api/tasks/{task_id}")
        delete.do_DELETE()
        self.assertEqual(delete.response[0], 204)

        invalid = HandlerHarness("/api/tasks", {"title": "Bad", "status": "later"})
        invalid.do_POST()
        self.assertEqual(invalid.response[0], 400)


if __name__ == "__main__":
    unittest.main()
