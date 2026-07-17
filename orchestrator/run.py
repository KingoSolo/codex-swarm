#!/usr/bin/env python3
"""
Codex Org orchestrator.

Core loop: pick the next ready task -> build a role-specific prompt ->
call `codex exec` (real) or a mock -> parse the structured JSON response ->
apply it to shared state -> commit to git as that "agent".

Set CODEX_ORG_MOCK=1 to run without real Codex credits (for testing the
pipeline end-to-end before credits arrive). Unset it to use real `codex exec`.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE_PATH = ROOT / "state" / "state.json"
SCHEMA_PATH = ROOT / "state" / "schemas" / "agent_turn.json"
ROLES_DIR = ROOT / "agents" / "roles"
MOCK = os.environ.get("CODEX_ORG_MOCK", "1") == "1"  # default ON until credits land

ROLE_ORDER = ["manager", "architect", "backend", "frontend", "database", "qa", "security"]


def load_state():
    return json.loads(STATE_PATH.read_text())


def save_state(state):
    STATE_PATH.write_text(json.dumps(state, indent=2))


def role_prompt(role):
    return (ROLES_DIR / f"{role}.md").read_text()


def build_prompt(role, state, task):
    """Give the agent just enough shared context: the goal, its task, open
    messages addressed to it, and current file ownership (so it doesn't
    collide with another agent's files)."""
    inbox = [m for m in state["messages"] if m.get("to") == role]
    return f"""{role_prompt(role)}

## Sprint goal
{state['sprint']['goal']}

## Your current task
{json.dumps(task, indent=2) if task else "No task assigned yet. If you are the manager, create the first tasks. Otherwise, report task_status: blocked."}

## Files currently owned by others (do not touch these)
{json.dumps({f: o for f, o in state['files_owned'].items() if o != role}, indent=2)}

## Messages addressed to you
{json.dumps(inbox, indent=2)}

Respond with ONLY JSON matching the required schema.
"""


def call_codex_real(role, prompt):
    """Real call via Codex CLI headless mode."""
    out_path = ROOT / "logs" / f"{role}_last_output.json"
    cmd = [
        "codex", "exec",
        "--sandbox", "workspace-write",
        "--skip-git-repo-check",
        "--output-schema", str(SCHEMA_PATH),
        "-o", str(out_path),
        prompt,
    ]
    result = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=900)
    if result.returncode != 0:
        raise RuntimeError(f"codex exec failed for {role}: {result.stderr[-2000:]}")
    return json.loads(out_path.read_text())


def call_codex_mock(role, prompt):
    """Fake response so we can test the full pipeline before credits arrive.
    Simulates one plausible turn per role so the demo data flows end-to-end."""
    time.sleep(0.3)
    canned = {
        "manager": {
            "summary": "Broke sprint goal into initial tasks.",
            "task_status": "done",
            "new_tasks": [
                {"id": "T1", "title": "Design system architecture", "owner": "architect", "depends_on": []},
            ],
            "messages": [{"to": "architect", "content": "Please design the stack and file layout."}],
        },
        "architect": {
            "summary": "Chose Flask + SQLite, defined module boundaries.",
            "task_status": "done",
            "decisions": ["Use Flask for API, SQLite for storage, React for frontend."],
            "new_tasks": [
                {"id": "T2", "title": "Implement API endpoints", "owner": "backend", "depends_on": ["T1"]},
                {"id": "T3", "title": "Implement schema", "owner": "database", "depends_on": ["T1"]},
                {"id": "T4", "title": "Build UI", "owner": "frontend", "depends_on": ["T2"]},
            ],
        },
        "backend": {
            "summary": "Implemented /api/items endpoints.",
            "task_status": "done",
            "files_changed": ["app.py"],
            "messages": [{"to": "frontend", "content": "API ready at /api/items."}],
        },
        "database": {
            "summary": "Created items table schema.",
            "task_status": "done",
            "files_changed": ["schema.sql"],
        },
        "frontend": {
            "summary": "Built list view consuming /api/items.",
            "task_status": "done",
            "files_changed": ["index.html"],
            "blockers": ["Auth took longer than expected to wire up."],
        },
        "qa": {
            "summary": "Ran manual pass, found regressions.",
            "task_status": "done",
            "blockers": ["Three regressions found in item deletion flow."],
        },
        "security": {
            "summary": "Reviewed input handling.",
            "task_status": "done",
            "decisions": ["Recommend enabling CSP headers."],
        },
    }
    return canned.get(role, {"summary": "No-op", "task_status": "done"})


def call_codex(role, prompt):
    return call_codex_mock(role, prompt) if MOCK else call_codex_real(role, prompt)


def apply_turn(state, role, output):
    state["agents"][role]["status"] = output.get("task_status", "done")
    for f in output.get("files_changed", []):
        state["files_owned"][f] = role
    for msg in output.get("messages", []):
        state["messages"].append({"from": role, "to": msg["to"], "content": msg["content"]})
    for t in output.get("new_tasks", []):
        t["status"] = "todo"
        state["tasks"].append(t)
    for d in output.get("decisions", []):
        state["architecture_decisions"].append({"by": role, "decision": d})
    return output.get("blockers", [])


def git_commit(role, summary):
    subprocess.run(["git", "add", "-A"], cwd=ROOT, check=False)
    subprocess.run(
        ["git", "-c", f"user.name={role}", "-c", f"user.email={role}@codex-org.local",
         "commit", "-m", f"[{role}] {summary}", "--allow-empty"],
        cwd=ROOT, check=False, capture_output=True,
    )


def next_ready_task(state, role):
    for t in state["tasks"]:
        if t["owner"] == role and t["status"] == "todo":
            deps = t.get("depends_on", [])
            if all(any(dt["id"] == d and dt["status"] == "done" for dt in state["tasks"]) for d in deps):
                return t
    return None


def run_sprint(max_rounds=6):
    state = load_state()
    state["sprint"]["status"] = "in_progress"

    for round_num in range(max_rounds):
        any_work = False
        for role in ROLE_ORDER:
            task = next_ready_task(state, role)
            if role == "manager" and round_num > 0 and not any(t["status"] == "blocked" for t in state["tasks"]):
                continue  # manager only re-engages if something's blocked
            if not task and role != "manager":
                continue
            prompt = build_prompt(role, state, task)
            print(f"--- Round {round_num+1}: {role} working on {task['id'] if task else '(kickoff)'} ---")
            output = call_codex(role, prompt)
            blockers = apply_turn(state, role, output)
            if task:
                task["status"] = output.get("task_status", "done")
            git_commit(role, output.get("summary", "update"))
            save_state(state)
            any_work = True
            if blockers:
                print(f"  blockers: {blockers}")
        if not any_work:
            break

    # Safety net: QA and Security must always get a pass before retro,
    # even if no other agent explicitly delegated to them.
    for role in ["qa", "security"]:
        if not any(t["owner"] == role for t in state["tasks"]):
            state["tasks"].append({"id": f"auto-{role}", "title": f"Final {role} pass",
                                    "owner": role, "depends_on": [], "status": "todo"})
        task = next_ready_task(state, role)
        if task:
            prompt = build_prompt(role, state, task)
            print(f"--- Final pass: {role} ---")
            output = call_codex(role, prompt)
            blockers = apply_turn(state, role, output)
            task["status"] = output.get("task_status", "done")
            git_commit(role, output.get("summary", "final pass"))
            save_state(state)
            if blockers:
                print(f"  blockers: {blockers}")

    # Retrospective: manager reads the full log and produces closing dialogue
    print("--- Retrospective ---")
    retro_prompt = build_prompt("manager", state, None) + (
        "\n\nThe sprint is complete. Using the full message log, blockers, and decisions above, "
        "write a short retrospective as a JSON field 'retrospective_dialogue': an array of "
        "{speaker, line} objects, one or two lines per agent, grounded only in what actually happened."
    )
    retro = call_codex("manager", retro_prompt)
    state["retrospective"] = retro.get("retrospective_dialogue", retro.get("summary"))
    state["sprint"]["status"] = "complete"
    save_state(state)
    git_commit("manager", "Sprint retrospective complete")
    print(json.dumps(state["retrospective"], indent=2))


if __name__ == "__main__":
    run_sprint()
