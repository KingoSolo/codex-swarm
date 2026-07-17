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
import time as _time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE_PATH = ROOT / "state" / "state.json"
SCHEMA_PATH = ROOT / "state" / "schemas" / "agent_turn.json"
ROLES_DIR = ROOT / "agents" / "roles"
SESSION_ID_PATH = ROOT / "logs" / "session_id.txt"
MOCK = os.environ.get("CODEX_ORG_MOCK", "1") == "1"
USAGE_LOG_PATH = ROOT / "logs" / "usage_log.jsonl"


ROLE_ORDER = ["manager", "architect", "backend", "frontend", "database", "qa", "security"]


def load_state():
    return json.loads(STATE_PATH.read_text())


def save_state(state):
    STATE_PATH.write_text(json.dumps(state, indent=2))


def role_prompt(role):
    return (ROLES_DIR / f"{role}.md").read_text()


def get_session_id():
    return SESSION_ID_PATH.read_text().strip() if SESSION_ID_PATH.exists() else None


def save_session_id(sid):
    SESSION_ID_PATH.write_text(sid)

def build_prompt(role, state, task):
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

_call_count = [0]

def call_codex_real(role, prompt):
    _call_count[0] += 1
    start = _time.time()
    out_path = ROOT / "logs" / f"{role}_last_output.json"
    session_id = get_session_id()
    base_flags = [
        "--sandbox", "workspace-write", "--skip-git-repo-check", "--json",
        "--output-schema", str(SCHEMA_PATH), "-o", str(out_path),
    ]
    cmd = ["codex", "exec"] + base_flags + ([prompt] if session_id is None else ["resume", session_id, prompt])
    result = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=900)
    duration = _time.time() - start

    log_entry = {
        "call_number": _call_count[0], "role": role, "prompt_chars": len(prompt),
        "duration_sec": round(duration, 1), "exit_code": result.returncode,
        "response_chars": len(result.stdout),
    }
    with open(USAGE_LOG_PATH, "a") as f:
        f.write(json.dumps(log_entry) + "\n")
    print(f"  [usage] call #{_call_count[0]} | {role} | {log_entry['prompt_chars']} chars in | {round(duration,1)}s")

    if result.returncode != 0:
        raise RuntimeError(f"codex exec failed for {role} (exit {result.returncode}):\n--- stdout tail ---\n{result.stdout[-3000:]}\n--- stderr tail ---\n{result.stderr[-3000:]}")

    if session_id is None:
        for line in result.stdout.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "thread.started" and event.get("thread_id"):
                save_session_id(event["thread_id"])
                break
    return json.loads(out_path.read_text())


def call_codex_mock(role, prompt):
    """Fake response so we can test the full pipeline before credits arrive.
    Simulates one plausible turn per role so the demo data flows end-to-end."""
    _time.sleep(0.2)
    canned = {
        "manager": {
            "summary": "Broke sprint goal into initial tasks.", 
            "task_status": "done",
            "new_tasks": [{"id": "T1", "title": "Design system architecture", "owner": "architect", "depends_on": []}],
            "messages": [{"to": "architect", "content": "Please design the stack and file layout."}]
            },
        
        "architect": {
            "summary": "Chose Flask + SQLite + vanilla JS, defined module boundaries.", 
            "task_status": "done",
            "decisions": ["Use Flask for API, SQLite for storage, vanilla JS for board UI."],
            "new_tasks": [
                          {"id": "T2", "title": "Implement task CRUD + move endpoints", "owner": "backend", "depends_on": ["T1"]},
                          {"id": "T3", "title": "Implement schema", "owner": "database", "depends_on": ["T1"]},
                          {"id": "T4", "title": "Build Kanban board UI", "owner": "frontend", "depends_on": ["T2"]},
                      ]},

        "backend": {
            "summary": "Implemented /api/tasks CRUD + move endpoint.", 
            "task_status": "done",
            "files_changed": ["app.py"],
            "messages": [{"to": "frontend", "content": "API ready at /api/tasks."}]
            },
        
        "database": {
            "summary": "Created tasks table with status column.", 
            "task_status": "done", 
            "files_changed": ["schema.sql"]
            },

        "frontend": {
            "summary": "Built drag-drop board with three columns.", 
            "task_status": "done",
            "files_changed": ["index.html"], 
            "blockers": ["Drag-drop across columns took longer than expected."]
            },

        "qa": {
            "summary": "Ran manual pass on move/edit/delete.", 
            "task_status": "done",
            "blockers": ["Found a regression: deleting a task in Done doesn't refresh the board."]
            },

        "security": {
            "summary": "Reviewed input handling on task fields.", 
            "task_status": "done",
            "decisions": ["Recommend sanitizing task titles before rendering (XSS)."]
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

def run_dev_loop(state, max_rounds=6):
    seen_blocker_signature = None
    for round_num in range(max_rounds):
        any_work = False
        current_blockers = tuple(sorted(t["id"] for t in state["tasks"] if t["status"] == "blocked"))
        for role in ROLE_ORDER:
            task = next_ready_task(state, role)
            if role == "manager":
                if round_num > 0 and (not current_blockers or current_blockers == seen_blocker_signature):
                    continue
                seen_blocker_signature = current_blockers
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

    for role in ["qa", "security"]:
        if not any(t["owner"] == role for t in state["tasks"]):
            state["tasks"].append({"id": f"auto-{role}-s{state['sprint']['number']}", "title": f"Final {role} pass",
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


def run_retrospective(state):
    print("--- Retrospective ---")
    retro_prompt = build_prompt("manager", state, None) + (
        "\n\nThe sprint is complete. Using the full message log, blockers, and decisions above, "
        "write a grounded retrospective as JSON field 'retrospective_dialogue': an array of "
        "{speaker, line} objects, one or two lines per agent, based only on what actually happened."
    )
    retro = call_codex("manager", retro_prompt)
    state["retrospective"] = retro.get("retrospective_dialogue", retro.get("summary"))
    state["sprint"]["status"] = "complete"
    save_state(state)
    git_commit("manager", f"Sprint {state['sprint']['number']} retrospective complete")
    print(json.dumps(state["retrospective"], indent=2))


def seed_sprint_1(state):
    if state["sprint"]["number"] != 1 or state["tasks"]:
        return  # only seed a fresh sprint 1
    state["architecture_decisions"].append({
        "by": "architect",
        "decision": "Python stdlib http.server + sqlite3 for backend, single static HTML+vanilla JS file for frontend. No external dependencies (sandbox has no network access).",
        "sprint": 1,
    })
    state["tasks"] = [
        {"id": "db-1", "title": "Create tasks table schema (id, title, status, created_at)", "owner": "database", "depends_on": [], "status": "todo"},
        {"id": "be-1", "title": "Implement REST API: create/list/update/delete tasks, move between statuses", "owner": "backend", "depends_on": ["db-1"], "status": "todo"},
        {"id": "fe-1", "title": "Build Kanban board UI (3 columns, drag or click-to-move) consuming the API", "owner": "frontend", "depends_on": ["be-1"], "status": "todo"},
    ]

def start_new_sprint(state, new_goal):
    """Archive the finished sprint and open the next one, same session,
    same files_owned (the app persists — it's evolving, not restarting)."""
    state["sprint_history"].append({
        "number": state["sprint"]["number"],
        "goal": state["sprint"]["goal"],
        "tasks": state["tasks"],
        "messages": state["messages"],
        "retrospective": state["retrospective"],
    })
    state["sprint"] = {"number": state["sprint"]["number"] + 1, "goal": new_goal, "status": "not_started"}
    state["tasks"] = []
    state["messages"] = []
    state["retrospective"] = None
    for role in state["agents"]:
        state["agents"][role] = {"status": "idle", "current_task": None}
    return state

def run_sprint(goal_override=None):
    state = load_state()
    if goal_override:
        state = start_new_sprint(state, goal_override)
    state["sprint"]["status"] = "in_progress"
    save_state(state)
    seed_sprint_1(state)
    run_dev_loop(state)
    run_retrospective(state)


if __name__ == "__main__":
    goal_arg = sys.argv[1] if len(sys.argv) > 1 else None
    run_sprint(goal_arg)
