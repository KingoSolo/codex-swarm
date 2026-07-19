"""Unit tests for deterministic, scope-aware mock sprint planning."""

from __future__ import annotations

import unittest

from orchestrator.run import mock_plan_for_goal


class MockPlannerTests(unittest.TestCase):
    sprint = 17

    def plan(self, goal: str) -> list[dict[str, object]]:
        return mock_plan_for_goal(self.sprint, goal)[1]

    def assert_plan(self, goal: str, expected_owners: list[str]) -> list[dict[str, object]]:
        tasks = self.plan(goal)
        self.assertEqual([task["owner"] for task in tasks], expected_owners)
        self.assertTrue(all(task["id"].endswith(f"-{self.sprint}") for task in tasks))

        positions = {task["id"]: index for index, task in enumerate(tasks)}
        for index, task in enumerate(tasks):
            for dependency in task["depends_on"]:
                self.assertIn(dependency, positions)
                self.assertLess(positions[dependency], index)
        return tasks

    def test_frontend_feature_uses_frontend_qa_and_architecture(self) -> None:
        tasks = self.assert_plan("Add a new UI settings page", ["architect", "frontend", "qa"])
        self.assertEqual(tasks[1]["depends_on"], ["arch-17"])
        self.assertEqual(tasks[2]["depends_on"], ["fe-17"])

    def test_backend_api_feature_skips_database_without_persistence_work(self) -> None:
        tasks = self.assert_plan("Add a REST API endpoint", ["architect", "backend", "qa", "security"])
        self.assertEqual(tasks[1]["depends_on"], ["arch-17"])
        self.assertEqual(tasks[2]["depends_on"], ["be-17"])
        self.assertEqual(tasks[3]["depends_on"], ["be-17"])

    def test_authentication_feature_uses_all_relevant_disciplines(self) -> None:
        tasks = self.assert_plan(
            "Add JWT login authentication", ["architect", "database", "backend", "frontend", "qa", "security"]
        )
        self.assertEqual(tasks[1]["depends_on"], ["arch-17"])
        self.assertEqual(tasks[2]["depends_on"], ["db-17"])
        self.assertEqual(tasks[3]["depends_on"], ["be-17"])
        self.assertEqual(tasks[4]["depends_on"], ["fe-17"])
        self.assertEqual(tasks[5]["depends_on"], ["fe-17"])

    def test_documentation_uses_manager_and_qa(self) -> None:
        tasks = self.assert_plan("Update the README documentation", ["manager", "qa"])
        self.assertEqual(tasks[1]["depends_on"], ["mgr-17"])

    def test_infrastructure_uses_architecture_backend_and_security(self) -> None:
        tasks = self.assert_plan("Add a CI deployment pipeline", ["architect", "backend", "security"])
        self.assertEqual(tasks[1]["depends_on"], ["arch-17"])
        self.assertEqual(tasks[2]["depends_on"], ["be-17"])

    def test_frontend_bug_fix_uses_only_frontend_and_qa(self) -> None:
        tasks = self.assert_plan("Fix button alignment bug", ["frontend", "qa"])
        self.assertEqual(tasks[0]["depends_on"], [])
        self.assertEqual(tasks[1]["depends_on"], ["fe-17"])

    def test_planning_is_deterministic_and_goal_specific(self) -> None:
        goal = "Add a new UI settings page"
        self.assertEqual(mock_plan_for_goal(self.sprint, goal), mock_plan_for_goal(self.sprint, goal))
        self.assertNotEqual(
            [task["owner"] for task in self.plan(goal)],
            [task["owner"] for task in self.plan("Add JWT login authentication")],
        )


if __name__ == "__main__":
    unittest.main()
