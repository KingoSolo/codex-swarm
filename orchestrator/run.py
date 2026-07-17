
"""
Codex Org orchestrator.

Lifecycle: each sprint has exactly ONE planning phase that produces a frozen
task graph (plan_sprint). After that, no agent — including the manager — can
add new tasks; any attempt is dropped and logged. The manager's role in the
dev loop is purely reactive: unblock/reassign existing tasks, never invent
new ones. Duplicate task IDs are rejected before every save.
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
USAGE_LOG_PATH = ROOT / "logs" / "usage_log.jsonl"
MOCK = os.environ.get("CODEX_ORG_MOCK", "1") == "1"
MAX_TOTAL_CALLS = int(os.environ.get("CODEX_ORG_MAX_CALLS", "12"))

(ROOT / "logs").mkdir(parents=True, exist_ok=True)

_total_calls = [0]
_call_count = [0]

ROLE_ORDER = ["manager", "architect", "backend", "frontend", "database", "qa", "security"]


def check_call_budget():
    if _total_calls[0] >= MAX_TOTAL_CALLS:
        print(f"\n!!! HARD STOP: reached {MAX_TOTAL_CALLS} Codex calls this run. Exiting safely to protect credits.")
        sys.exit(1)
    _total_calls[0] += 1


def load_state():
    return json.loads(STATE_PATH.read_text())


def validate_state(state):
    ids = [t["id"] for t in state["tasks"]]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        raise ValueError(
            f"Duplicate task IDs detected: {dupes}. Refusing to save state — "
            f"this means planning ran more than once. Fix before continuing."
        )


def save_state(state):
    validate_state(state)
    STATE_PATH.write_text(json.dumps(state, indent=2))


def role_prompt(role):
    return (ROLES_DIR / f"{role}.md").read_text()


def get_session_id():
    return SESSION_ID_PATH.read_text().strip() if SESSION_ID_PATH.exists() else None


def save_session_id(sid):
    SESSION_ID_PATH.write_text(sid)


def build_prompt(role, state, task):
    inbox = [m for m in state["messages"] if m.get("to") == role]
    task_overview = ""
    if role == "manager":
        task_overview = f"""
## All existing tasks (planning is frozen — never create a task that duplicates one already here)
{json.dumps(state["tasks"], indent=2)}
"""
    return f"""{role_prompt(role)}

## Sprint {state['sprint']['number']} goal
{state['sprint']['goal']}
{task_overview}
## Your current task
{json.dumps(task, indent=2) if task else "No task assigned right now. Report task_status: blocked if you have nothing to do."}

## Files currently owned by others (do not touch these)
{json.dumps({f: o for f, o in state['files_owned'].items() if o != role}, indent=2)}

## Messages addressed to you
{json.dumps(inbox, indent=2)}

Respond with ONLY JSON matching the required schema.
"""


def call_codex_real(role, prompt):
    check_call_budget()
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
    _time.sleep(0.2)
    canned = {
        "manager": {"summary": "Reviewed blockers, no new tasks needed.", "task_status": "done", "messages": []},
        "architect": {"summary": "Chose stdlib http.server + sqlite3 + vanilla JS.", "task_status": "done",
                      "decisions": ["Python stdlib only, no network dependencies."]},
        "backend": {"summary": "Implemented task CRUD + move endpoint.", "task_status": "done", "files_changed": ["server.py"]},
        "database": {"summary": "Created tasks table with status column.", "task_status": "done", "files_changed": ["database.py"]},
        "frontend": {"summary": "Built board with three columns.", "task_status": "done", "files_changed": ["index.html"]},
        "qa": {"summary": "Ran manual pass.", "task_status": "done", "blockers": ["Found a minor UI refresh bug."]},
        "security": {"summary": "Reviewed input handling.", "task_status": "done", "decisions": ["Sanitize task titles."]},
    }
    return canned.get(role, {"summary": "No-op", "task_status": "done"})


def call_codex(role, prompt):
    return call_codex_mock(role, prompt) if MOCK else call_codex_real(role, prompt)


def add_tasks(state, new_tasks, allow=True):
    if not new_tasks:
        return
    if not allow:
        print(f"  [planning frozen] dropped {len(new_tasks)} proposed task(s) — planning already closed this sprint.")
        return
    existing_ids = {t["id"] for t in state["tasks"]}
    for t in new_tasks:
        if t["id"] in existing_ids:
            print(f"  [warning] duplicate task id '{t['id']}' proposed — skipped.")
            continue
        t["status"] = "todo"
        state["tasks"].append(t)
        existing_ids.add(t["id"])


def apply_turn(state, role, output, planning_open=False):
    state["agents"][role]["status"] = output.get("task_status", "done")
    for f in output.get("files_changed", []):
        state["files_owned"][f] = role
    for msg in output.get("messages", []):
        state["messages"].append({"from": role, "to": msg["to"], "content": msg["content"]})
    add_tasks(state, output.get("new_tasks", []), allow=planning_open)
    for d in output.get("decisions", []):
        state["architecture_decisions"].append({"by": role, "decision": d, "sprint": state["sprint"]["number"]})
    return output.get("blockers", [])


def git_commit(role, summary):
    subprocess.run(["git", "add", "-A"], cwd=ROOT, check=False)
    subprocess.run(
        ["git", "-c", f"user.name={role}", "-c", f"user.email={role}@codex-org.local",
         "commit", "-m", f"[{role}] {summary}", "--allow-empty"],
        cwd=ROOT, check=False, capture_output=True,
    )
    log = subprocess.run(["git", "log", "--pretty=format:%h|%an|%s", "-30"], cwd=ROOT, capture_output=True, text=True)
    commits = []
    for line in log.stdout.splitlines():
        parts = line.split("|", 2)
        if len(parts) == 3:
            commits.append({"hash": parts[0], "role": parts[1], "message": parts[2]})
    (ROOT / "logs" / "commits.json").write_text(json.dumps(commits, indent=2))


def next_ready_task(state, role):
    for t in state["tasks"]:
        if t["owner"] == role and t["status"] == "todo":
            deps = t.get("depends_on", [])
            if all(any(dt["id"] == d and dt["status"] == "done" for dt in state["tasks"]) for d in deps):
                return t
    return None


def seed_sprint_1(state):
    """Deterministic seed for the known Kanban demo — skips paying for
    manager/architect to 'discover' an architecture we already decided on."""
    state["architecture_decisions"].append({
        "by": "architect",
        "decision": "Python stdlib http.server + sqlite3 for backend, single static HTML+vanilla JS file for frontend. No external dependencies (sandbox has no network access).",
        "sprint": 1,
    })
    state["tasks"] = [
        {"id": "db-1", "title": "Create tasks table schema (id, title, status, created_at) in database.py", "owner": "database", "depends_on": [], "status": "todo"},
        {"id": "be-1", "title": "Implement REST API in server.py: create/list/update/delete tasks, move between statuses", "owner": "backend", "depends_on": ["db-1"], "status": "todo"},
        {"id": "fe-1", "title": "Build Kanban board UI in index.html (3 columns, click-to-move) consuming the API", "owner": "frontend", "depends_on": ["be-1"], "status": "todo"},
    ]


def plan_sprint(state):
    """Exactly one planning phase per sprint. Produces a frozen task graph;
    nothing after this point may add tasks except via a new sprint."""
    if state.get("planning_frozen_sprint") == state["sprint"]["number"]:
        return  # already planned

    if state["sprint"]["number"] == 1:
        seed_sprint_1(state)
    else:
        prompt = build_prompt("manager", state, None)
        output = call_codex("manager", prompt)
        add_tasks(state, output.get("new_tasks", []), allow=True)
        git_commit("manager", output.get("summary", "sprint planning"))
        save_state(state)

        prompt = build_prompt("architect", state, None)
        output = call_codex("architect", prompt)
        add_tasks(state, output.get("new_tasks", []), allow=True)
        for d in output.get("decisions", []):
            state["architecture_decisions"].append({"by": "architect", "decision": d, "sprint": state["sprint"]["number"]})
        git_commit("architect", output.get("summary", "architecture planning"))

    state["planning_frozen_sprint"] = state["sprint"]["number"]
    save_state(state)


def run_dev_loop(state, max_rounds=6):
    seen_blocker_signature = None
    for round_num in range(max_rounds):
        any_work = False
        current_blockers = tuple(sorted(t["id"] for t in state["tasks"] if t["status"] == "blocked"))

        for role in ROLE_ORDER:
            if role == "manager":
                if not current_blockers or current_blockers == seen_blocker_signature:
                    continue
                seen_blocker_signature = current_blockers
                task = None
            else:
                task = next_ready_task(state, role)
                if not task:
                    continue

            prompt = build_prompt(role, state, task)
            print(f"--- Round {round_num+1}: {role} working on {task['id'] if task else '(reviewing blockers)'} ---")
            output = call_codex(role, prompt)
            blockers = apply_turn(state, role, output, planning_open=False)
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
            blockers = apply_turn(state, role, output, planning_open=False)
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


def start_new_sprint(state, new_goal):
    state["sprint_history"].append({
        "number": state["sprint"]["number"], "goal": state["sprint"]["goal"],
        "tasks": state["tasks"], "messages": state["messages"], "retrospective": state["retrospective"],
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
    plan_sprint(state)
    run_dev_loop(state)
    run_retrospective(state)


if __name__ == "__main__":
    goal_arg = sys.argv[1] if len(sys.argv) > 1 else None
    run_sprint(goal_arg)