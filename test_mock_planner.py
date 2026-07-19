"""Unit tests for deterministic, scope-aware mock sprint planning."""

from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch
from pathlib import Path
import os
import stat

from orchestrator import run
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

    def test_task_search_filter_sort_uses_backend_and_frontend(self) -> None:
        self.assert_plan(
            "Implement task search, filtering, and sorting", ["architect", "backend", "frontend", "qa"]
        )

    def test_planning_is_deterministic_and_goal_specific(self) -> None:
        goal = "Add a new UI settings page"
        self.assertEqual(mock_plan_for_goal(self.sprint, goal), mock_plan_for_goal(self.sprint, goal))
        self.assertNotEqual(
            [task["owner"] for task in self.plan(goal)],
            [task["owner"] for task in self.plan("Add JWT login authentication")],
        )


class MockPlanningFlowTests(unittest.TestCase):
    def planned_state(self, sprint: int, goal: str) -> dict[str, object]:
        state = run.new_state()
        state["sprint"] = {"number": sprint, "goal": goal, "status": "in_progress"}
        with patch.object(run, "git_commit"), patch.object(run, "save_state"):
            run.plan_sprint(state)
        return state

    def test_sprint_one_seed_has_implementation_graph(self) -> None:
        state = self.planned_state(1, "Build a Kanban application")
        self.assertEqual([task["owner"] for task in state["tasks"]], ["database", "backend", "frontend"])

    def test_jwt_sprint_manager_creates_complete_graph(self) -> None:
        state = self.planned_state(2, "Add JWT authentication with login and registration")
        self.assertEqual(
            [task["owner"] for task in state["tasks"]],
            ["architect", "database", "backend", "frontend", "qa", "security"],
        )
        self.assertEqual(state["tasks"][2]["depends_on"], ["db-2"])
        self.assertEqual(state["tasks"][3]["depends_on"], ["be-2"])

    def test_search_sprint_has_backend_and_frontend_work(self) -> None:
        state = self.planned_state(3, "Implement task search, filtering, and sorting")
        self.assertEqual([task["owner"] for task in state["tasks"]], ["architect", "backend", "frontend", "qa"])
        self.assertEqual(state["tasks"][1]["depends_on"], ["arch-3"])
        self.assertEqual(state["tasks"][2]["depends_on"], ["be-3"])

    def test_planning_freeze_prevents_a_second_graph(self) -> None:
        state = self.planned_state(2, "Add JWT authentication with login and registration")
        original_ids = [task["id"] for task in state["tasks"]]
        with patch.object(run, "call_codex") as call_codex:
            run.plan_sprint(state)
        call_codex.assert_not_called()
        self.assertEqual([task["id"] for task in state["tasks"]], original_ids)

    def test_completed_sprint_cannot_be_rerun_without_a_new_goal(self) -> None:
        state = run.new_state()
        state["sprint"]["status"] = "complete"
        state["sprint"]["number"] = 9
        with (
            patch.object(run, "load_state", return_value=state),
            patch.object(run, "save_state") as save_state,
            patch.object(run, "plan_sprint") as plan_sprint,
            patch.object(run, "run_dev_loop") as run_dev_loop,
            patch.object(run, "run_retrospective") as run_retrospective,
            patch("builtins.print") as print_mock,
        ):
            run.run_sprint()

        self.assertEqual(state["sprint"]["status"], "complete")
        save_state.assert_not_called()
        plan_sprint.assert_not_called()
        run_dev_loop.assert_not_called()
        run_retrospective.assert_not_called()
        self.assertIn("Start a new sprint", print_mock.call_args_list[-1].args[0])

    def test_running_orchestrator_lock_prevents_a_second_run(self) -> None:
        with TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            state = run.new_state()
            with (
                patch.object(run, "STATE_PATH", state_path),
                patch.object(run, "load_state") as load_state,
                patch.object(run, "save_state") as save_state,
                patch.object(run, "plan_sprint") as plan_sprint,
                patch.object(run, "run_dev_loop") as run_dev_loop,
                patch.object(run, "run_retrospective") as run_retrospective,
                patch("builtins.print") as print_mock,
            ):
                with run.orchestrator_lock():
                    run.run_sprint()

            load_state.assert_not_called()
            save_state.assert_not_called()
            plan_sprint.assert_not_called()
            run_dev_loop.assert_not_called()
            run_retrospective.assert_not_called()
            self.assertIn("Another orchestrator is already running", str(print_mock.call_args.args[0]))
            self.assertFalse(state_path.with_name("state.lock").exists())

    def test_retrospective_commits_before_marking_sprint_complete(self) -> None:
        state = run.new_state()
        state["sprint"]["status"] = "in_progress"
        events = []

        def record_commit(*args, **kwargs):
            events.append(("commit", state["sprint"]["status"]))

        def record_save(saved_state):
            events.append(("save", saved_state["sprint"]["status"]))

        with (
            patch.object(run, "call_codex", return_value={"summary": "Sprint retrospective"}),
            patch.object(run, "git_commit", side_effect=record_commit),
            patch.object(run, "save_state", side_effect=record_save),
        ):
            run.run_retrospective(state)

        self.assertEqual(events, [("commit", "in_progress"), ("save", "complete")])
        self.assertEqual(state["sprint"]["status"], "complete")

    def test_retrospective_failure_leaves_sprint_incomplete_and_retryable(self) -> None:
        state = run.new_state()
        state["sprint"]["status"] = "in_progress"
        state["retrospective"] = "previous retrospective"
        with (
            patch.object(run, "call_codex", return_value={"summary": "new retrospective"}),
            patch.object(run, "git_commit", side_effect=run.CommitHistoryError("history unavailable")),
            patch.object(run, "save_state") as save_state,
            self.assertRaisesRegex(run.CommitHistoryError, "history unavailable"),
        ):
            run.run_retrospective(state)

        save_state.assert_not_called()
        self.assertEqual(state["sprint"]["status"], "in_progress")
        self.assertIsNone(state["sprint"]["completed_at"])
        self.assertEqual(state["retrospective"], "previous retrospective")

    def test_retrospective_state_write_failure_restores_incomplete_state(self) -> None:
        state = run.new_state()
        state["sprint"]["status"] = "in_progress"
        with (
            patch.object(run, "call_codex", return_value={"summary": "new retrospective"}),
            patch.object(run, "git_commit") as git_commit,
            patch.object(run, "save_state", side_effect=OSError("disk full")),
            self.assertRaisesRegex(OSError, "disk full"),
        ):
            run.run_retrospective(state)

        git_commit.assert_called_once()
        self.assertEqual(state["sprint"]["status"], "in_progress")
        self.assertIsNone(state["sprint"]["completed_at"])
        self.assertIsNone(state["retrospective"])


class AdaptiveSprintTests(unittest.TestCase):
    def blocked_state(self) -> dict[str, object]:
        state = run.new_state()
        state["sprint"] = {"number": 5, "goal": "Adaptive Sprint Management", "status": "in_progress"}
        state["adaptive"]["blocker_threshold_seconds"] = 5
        backend = {"id": "be-5", "title": "Implement adaptive API", "owner": "backend", "depends_on": [], "status": "blocked", "fallback_owners": ["frontend"]}
        frontend = {"id": "fe-5", "title": "Build adaptive dashboard", "owner": "frontend", "depends_on": ["be-5"], "status": "todo"}
        state["tasks"] = [backend, frontend]
        state["task_events"] = [{"task_id": "be-5", "status": "blocked", "time": 0, "sprint": 5}]
        return state

    def test_automatic_replanning_creates_validated_recovery_and_pauses_downstream(self) -> None:
        state = self.blocked_state()

        self.assertTrue(run.monitor_adaptive_sprint(state, now=10))

        recovery = next(task for task in state["tasks"] if task.get("recovery_for") == "be-5")
        backend = state["tasks"][0]
        self.assertTrue(recovery["validated_by_architect"])
        self.assertIn(recovery["id"], backend["depends_on"])
        self.assertEqual(backend["owner"], "frontend")
        self.assertEqual(state["tasks"][1]["status"], "paused")
        self.assertFalse(run.dependency_cycle(state["tasks"]))
        self.assertIn("recovery_created", [event["action"] for event in state["replanning_events"]])
        self.assertIn("pause", [event["action"] for event in state["replanning_events"]])
        self.assertIn("reassign", [event["action"] for event in state["replanning_events"]])

    def test_reassignment_history_is_persisted(self) -> None:
        state = self.blocked_state()
        backend = state["tasks"][0]

        self.assertTrue(run.reassign_task(state, backend, "frontend", "Backend owner is unavailable."))
        self.assertEqual(backend["owner"], "frontend")
        self.assertEqual(state["reassignment_history"][0]["from"], "backend")
        self.assertEqual(state["reassignment_history"][0]["to"], "frontend")
        self.assertIn("reassign", [event["action"] for event in state["replanning_events"]])

    def test_paused_dependency_chain_resumes_after_recovery(self) -> None:
        state = self.blocked_state()
        run.monitor_adaptive_sprint(state, now=10)
        backend, frontend = state["tasks"][:2]
        recovery = next(task for task in state["tasks"] if task.get("recovery_for") == backend["id"])
        recovery["status"] = "done"

        run.monitor_adaptive_sprint(state, now=11)
        self.assertEqual(backend["status"], "todo")
        backend["status"] = "done"
        run.monitor_adaptive_sprint(state, now=12)
        self.assertEqual(frontend["status"], "todo")
        self.assertIn("resume", [event["action"] for event in state["replanning_events"]])

    def test_adaptive_state_survives_save_and_load(self) -> None:
        state = self.blocked_state()
        run.monitor_adaptive_sprint(state, now=10)
        with TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            with patch.object(run, "STATE_PATH", state_path):
                run.save_state(state)
                loaded = run.load_state()

        self.assertEqual(loaded["replanning_events"], state["replanning_events"])
        self.assertEqual(loaded["reassignment_history"], state["reassignment_history"])
        self.assertEqual(loaded["dependency_changes"], state["dependency_changes"])

    def test_replanning_is_committed_as_manager_work(self) -> None:
        state = self.blocked_state()
        run.monitor_adaptive_sprint(state, now=10)
        with patch.object(run, "save_state") as save_state, patch.object(run, "git_commit") as git_commit:
            run.persist_replanning(state)

        save_state.assert_called_once_with(state)
        self.assertEqual(git_commit.call_args.args[0], "manager")
        self.assertIn("Adaptive replanning", git_commit.call_args.args[1])
        self.assertEqual(git_commit.call_args.args[3]["files_changed"], ["state/state.json"])


class AgentCommitTests(unittest.TestCase):
    def test_commit_stages_only_declared_agent_files(self) -> None:
        calls = []
        head_reads = 0

        def fake_run(command, **kwargs):
            nonlocal head_reads
            calls.append(command)
            if command[:3] == ["git", "status", "--porcelain"]:
                return Mock(stdout=" M server.py\n", returncode=0)
            if command == ["git", "rev-parse", "HEAD"]:
                head_reads += 1
                return Mock(stdout=("before\n" if head_reads == 1 else "after\n"), returncode=0)
            if command == ["git", "rev-parse", "--short", "HEAD"]:
                return Mock(stdout="abc123\n", returncode=0)
            if command[:2] == ["git", "log"]:
                return Mock(stdout="abc123|backend|1|[backend] update", returncode=0)
            return Mock(stdout="", returncode=0)

        with TemporaryDirectory() as temp_dir:
            temporary_root = Path(temp_dir)
            (temporary_root / "logs").mkdir()
            (temporary_root / "logs" / "commits.json").write_text("[]")
            with (
                patch.object(run, "ROOT", temporary_root),
                patch.object(run.subprocess, "run", side_effect=fake_run),
            ):
                committed = run.git_commit(
                    "backend", "update", 4, {"files_changed": ["server.py"]}
                )

        self.assertTrue(committed)
        self.assertIn(["git", "add", "--", "server.py"], calls)
        commit_call = next(command for command in calls if command[:2] == ["git", "-c"])
        self.assertIn("--only", commit_call)
        self.assertEqual(commit_call[-2:], ["--", "server.py"])
        self.assertNotIn("-A", [part for command in calls for part in command])

    def test_no_declared_or_actual_changes_creates_no_commit(self) -> None:
        with patch.object(run.subprocess, "run") as run_command:
            committed = run.git_commit("backend", "no-op", 4, {"files_changed": []})

        self.assertFalse(committed)
        run_command.assert_not_called()

    def test_declared_file_without_a_diff_creates_no_commit(self) -> None:
        with patch.object(run.subprocess, "run", return_value=Mock(stdout="", returncode=0)) as run_command:
            committed = run.git_commit("backend", "no-op", 4, {"files_changed": ["server.py"]})

        self.assertFalse(committed)
        run_command.assert_called_once_with(
            ["git", "status", "--porcelain", "--", "server.py"],
            cwd=run.ROOT, check=False, capture_output=True, text=True,
        )

    def test_unsafe_or_unrelated_paths_are_not_staged(self) -> None:
        self.assertEqual(
            run.agent_changed_paths(["server.py", "../private.txt", ".git/config", "", "server.py"]),
            ["server.py"],
        )


class CommitHistoryTests(unittest.TestCase):
    def test_corrupt_history_aborts_archiving_without_losing_commit_activity(self) -> None:
        corrupt_inputs = {
            "malformed": '[{"hash": "abc"}, invalid]',
            "truncated": '[{"hash": "abc"}',
            "empty": "",
        }
        for name, content in corrupt_inputs.items():
            with self.subTest(name=name), TemporaryDirectory() as temp_dir:
                temporary_root = Path(temp_dir)
                (temporary_root / "logs").mkdir()
                history_path = temporary_root / "logs" / "commits.json"
                history_path.write_text(content)
                state = run.new_state()
                original_state = json.loads(json.dumps(state))

                with patch.object(run, "ROOT", temporary_root):
                    with self.assertRaisesRegex(run.CommitHistoryError, "Cannot load commit history"):
                        run.start_new_sprint(state, "Next sprint")

                self.assertEqual(history_path.read_text(), content)
                self.assertEqual(state, original_state)

    def test_corrupt_history_prevents_a_new_agent_commit(self) -> None:
        with TemporaryDirectory() as temp_dir:
            temporary_root = Path(temp_dir)
            (temporary_root / "logs").mkdir()
            (temporary_root / "logs" / "commits.json").write_text("{")
            with (
                patch.object(run, "ROOT", temporary_root),
                patch.object(run, "worktree_has_changes", return_value=True),
                patch.object(run.subprocess, "run") as run_command,
                self.assertRaises(run.CommitHistoryError),
            ):
                run.git_commit("backend", "update", 4, {"files_changed": ["server.py"]})

            run_command.assert_not_called()


class StateLoadingTests(unittest.TestCase):
    def assert_invalid_state_is_preserved(self, content: str) -> None:
        with TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            state_path.write_text(content)

            with patch.object(run, "STATE_PATH", state_path):
                with self.assertRaisesRegex(run.StateLoadError, "Cannot load persisted state"):
                    run.load_state()

            self.assertEqual(state_path.read_text(), content)

    def test_malformed_json_fails_without_replacing_sprint_history(self) -> None:
        self.assert_invalid_state_is_preserved(
            '{"sprint_history": [{"number": 4, "goal": "Keep this"}], broken}'
        )

    def test_empty_state_file_fails_without_overwriting_it(self) -> None:
        self.assert_invalid_state_is_preserved("")

    def test_truncated_state_file_fails_without_overwriting_it(self) -> None:
        self.assert_invalid_state_is_preserved(
            '{"sprint": {"number": 8}, "sprint_history": [{"number": 7}'
        )


class AtomicStateWriteTests(unittest.TestCase):
    def test_save_state_fsyncs_then_replaces_and_preserves_permissions(self) -> None:
        with TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            state_path.write_text('{"old": true}')
            os.chmod(state_path, 0o640)
            state = run.new_state()
            state["sprint"]["number"] = 6

            with (
                patch.object(run, "STATE_PATH", state_path),
                patch.object(run.os, "replace", wraps=os.replace) as replace,
                patch.object(run.os, "fsync", wraps=os.fsync) as fsync,
            ):
                run.save_state(state)

            replace.assert_called_once()
            self.assertEqual(replace.call_args.args[1], state_path)
            self.assertGreaterEqual(fsync.call_count, 2)
            self.assertEqual(json.loads(state_path.read_text())["sprint"]["number"], 6)
            self.assertEqual(stat.S_IMODE(state_path.stat().st_mode), 0o640)
            self.assertEqual(list(Path(temp_dir).glob(".state.json.*.tmp")), [])

    def test_failed_replace_leaves_the_existing_state_unchanged(self) -> None:
        with TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            original = '{"sprint_history": [{"number": 4}]}'
            state_path.write_text(original)

            with (
                patch.object(run, "STATE_PATH", state_path),
                patch.object(run.os, "replace", side_effect=OSError("replace failed")),
                self.assertRaisesRegex(OSError, "replace failed"),
            ):
                run.save_state(run.new_state())

            self.assertEqual(state_path.read_text(), original)
            self.assertEqual(list(Path(temp_dir).glob(".state.json.*.tmp")), [])


class AtomicCommitHistoryWriteTests(unittest.TestCase):
    def test_commit_history_fsyncs_replaces_and_preserves_permissions(self) -> None:
        with TemporaryDirectory() as temp_dir:
            temporary_root = Path(temp_dir)
            history_path = temporary_root / "logs" / "commits.json"
            history_path.parent.mkdir()
            history_path.write_text('[{"hash": "old"}]')
            os.chmod(history_path, 0o640)
            commits = [{"hash": "new", "sprint": 7, "message": "new metadata"}]

            with (
                patch.object(run, "ROOT", temporary_root),
                patch.object(run.os, "replace", wraps=os.replace) as replace,
                patch.object(run.os, "fsync", wraps=os.fsync) as fsync,
            ):
                run.save_commit_history(commits)

            self.assertEqual(replace.call_args.args[1], history_path)
            self.assertGreaterEqual(fsync.call_count, 2)
            self.assertEqual(json.loads(history_path.read_text()), commits)
            self.assertEqual(stat.S_IMODE(history_path.stat().st_mode), 0o640)
            self.assertEqual(list(history_path.parent.glob(".commits.json.*.tmp")), [])

    def test_failed_commit_history_replace_preserves_existing_history(self) -> None:
        with TemporaryDirectory() as temp_dir:
            temporary_root = Path(temp_dir)
            history_path = temporary_root / "logs" / "commits.json"
            history_path.parent.mkdir()
            original = '[{"hash": "preserve-me", "sprint": 3}]'
            history_path.write_text(original)

            with (
                patch.object(run, "ROOT", temporary_root),
                patch.object(run.os, "replace", side_effect=OSError("replace failed")),
                self.assertRaisesRegex(OSError, "replace failed"),
            ):
                run.save_commit_history([{"hash": "new"}])

            self.assertEqual(history_path.read_text(), original)
            self.assertEqual(list(history_path.parent.glob(".commits.json.*.tmp")), [])


class DashboardReplayTests(unittest.TestCase):
    def build_replay(self, view: dict[str, object]) -> dict[str, object]:
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node.js is required to execute dashboard replay regression tests")
        dashboard_source = Path(__file__).with_name("dashboard").joinpath("index.html").read_text()
        replay_source = dashboard_source.split("<script>", 1)[1].split("function initReplay", 1)[0]
        duration_source = dashboard_source.split("function computeSprintDuration", 1)[1].split("function fmtDurationHuman", 1)[0]
        harness = """
const elements = {};
globalThis.document = {
  getElementById: id => elements[id] ||= { addEventListener() {}, querySelectorAll() { return []; } }
};
""" + replay_source + "\nfunction computeSprintDuration" + duration_source + """
const input = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const view = input.view || input;
liveState = { _usage: input.usage || [] };
const timeline = buildTimeline(view);
const phaseAt = count => deriveStateAt(view, timeline, count).engineerPhase.backend;
if (input.payload) renderEventFeed([{ from: 'backend', to: 'qa', content: input.payload, time: 1 }]);
if (input.commitHistoryError) renderCommitFeed([], input.commitHistoryError);
console.log(JSON.stringify({
  timeline: timeline.map(event => ({ type: event.type, status: event.data.status, action: event.data.action, hash: event.data.hash, order: event.order })),
  phases: [phaseAt(1), phaseAt(2), phaseAt(3)],
  duration: computeSprintDuration(view),
  renderedMessageFeed: elements.messages?.innerHTML || '',
  renderedCommitFeed: elements.commits?.innerHTML || '',
}));
"""
        result = subprocess.run(
            [node, "-e", harness], input=json.dumps(view), text=True, capture_output=True, check=True
        )
        return json.loads(result.stdout)

    def test_same_second_task_completion_precedes_commit_and_replay_phases(self) -> None:
        view = {
            "tasks": [{"id": "be-4", "owner": "backend", "status": "done", "depends_on": []}],
            "messages": [], "blockers": [],
            # Git records seconds; state events retain fractions in that same second.
            "task_events": [
                {"task_id": "be-4", "status": "in_progress", "time": 1000.8},
                {"task_id": "be-4", "status": "done", "time": 1000.9},
            ],
            "commits": [
                {"hash": "b-commit", "role": "backend", "message": "backend update", "time": 1000},
                {"hash": "a-commit", "role": "backend", "message": "another update", "time": 1000},
            ],
        }
        replay = self.build_replay(view)

        self.assertEqual(
            [(event["type"], event["status"]) for event in replay["timeline"][:2]],
            [("task", "in_progress"), ("task", "done")],
        )
        self.assertEqual(
            [event["hash"] for event in replay["timeline"][2:]], ["a-commit", "b-commit"]
        )
        self.assertEqual(replay["phases"], ["working", "review", "done"])

    def test_untagged_usage_is_unknown_and_tagged_usage_keeps_its_sprint_duration(self) -> None:
        view = {
            "number": 8, "tasks": [], "messages": [], "blockers": [], "task_events": [], "commits": [],
        }
        unknown = self.build_replay({"view": view, "usage": [{"duration_sec": 999}]})
        tagged = self.build_replay({
            "view": view,
            "usage": [{"sprint": 8, "duration_sec": 12.5}, {"sprint": 7, "duration_sec": 999}],
        })

        self.assertEqual(unknown["duration"], {"seconds": None, "label": None, "unknown": True})
        self.assertEqual(tagged["duration"]["seconds"], 12.5)
        self.assertEqual(tagged["duration"]["label"], "(Codex execution time, approximate)")

    def test_lifecycle_timestamps_win_over_historical_commit_timestamps(self) -> None:
        view = {
            "number": 1,
            "started_at": 1_784_431_609.884696,
            "completed_at": 1_784_431_802.40007,
            "tasks": [], "messages": [], "blockers": [], "task_events": [],
            # Archived Git Activity can include a historical commit that was
            # inferred into this sprint after the fact.
            "commits": [
                {"hash": "older", "time": 1_784_247_622},
                {"hash": "current", "time": 1_784_431_792},
            ],
        }

        duration = self.build_replay(view)["duration"]

        self.assertAlmostEqual(duration["seconds"], 192.515374, places=6)
        self.assertIsNone(duration["label"])

    def test_persisted_dashboard_payload_is_rendered_as_text(self) -> None:
        payload = "<img src=x onerror=alert(1)>"
        rendered = self.build_replay({
            "view": {"tasks": [], "messages": [], "blockers": [], "task_events": [], "commits": []},
            "payload": payload,
        })["renderedMessageFeed"]

        self.assertIn("&lt;img src=x onerror=alert(1)&gt;", rendered)
        self.assertNotIn(payload, rendered)

    def test_corrupt_commit_history_is_rendered_as_unavailable_not_empty(self) -> None:
        rendered = self.build_replay({
            "view": {"tasks": [], "messages": [], "blockers": [], "task_events": [], "commits": []},
            "commitHistoryError": "logs/commits.json could not be loaded or parsed",
        })["renderedCommitFeed"]

        self.assertIn("Commit history unavailable", rendered)
        self.assertNotIn("No commits yet", rendered)

    def test_replay_includes_manager_replanning_events(self) -> None:
        replay = self.build_replay({
            "tasks": [], "messages": [], "blockers": [], "task_events": [], "commits": [],
            "replanning_events": [{"action": "pause", "text": "Paused frontend until backend recovers.", "by": "manager", "time": 10}],
        })

        self.assertEqual([(event["type"], event["action"]) for event in replay["timeline"]], [("replan", "pause")])


if __name__ == "__main__":
    unittest.main()
