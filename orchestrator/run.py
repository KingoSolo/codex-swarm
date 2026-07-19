
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
import re
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
RETROSPECTIVE_SPRINT_RE = re.compile(r"\bSprint\s+(\d+)\s+retrospective\s+complete\b", re.IGNORECASE)


def new_state():
    """The single source of truth for what a fresh project's state looks like.
    Both --reset and load_state's self-heal path use this — never hand-build
    the dict anywhere else."""
    return {
        "sprint": {
            "number": 1,
            "goal": "Build a lightweight Kanban project management app: REST API + minimal web UI. Users can create tasks, move them between Todo, In Progress, and Done, edit task details, and delete tasks.",
            "status": "not_started",
            "started_at": None,
            "completed_at": None,
        },
        "sprint_history": [],
        "tasks": [],
        "agents": {role: {"status": "idle", "current_task": None}
                   for role in ["manager", "architect", "backend", "frontend", "database", "qa", "security"]},
        "files_owned": {},
        "messages": [],
        "architecture_decisions": [],
        "retrospective": None,
        "blockers": [],
        "task_events": [],
        "planning_frozen_sprint": None,
    }

def check_call_budget():
    if _total_calls[0] >= MAX_TOTAL_CALLS:
        print(f"\n!!! HARD STOP: reached {MAX_TOTAL_CALLS} Codex calls this run. Exiting safely to protect credits.")
        sys.exit(1)
    _total_calls[0] += 1


def load_state():
    defaults = new_state()
    if not STATE_PATH.exists():
        STATE_PATH.write_text(json.dumps(defaults, indent=2))
        return defaults
    try:
        loaded = json.loads(STATE_PATH.read_text())
    except json.JSONDecodeError:
        return defaults
    return {**defaults, **loaded}  # any key missing from the file falls back to default


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
    current_sprint = json.loads(STATE_PATH.read_text())["sprint"]["number"]
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
        "response_chars": len(result.stdout), "sprint": current_sprint,  # <-- add sprint
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


def mock_sprint_context(prompt):
    """Extract stable sprint context from build_prompt without guessing from state."""
    match = re.search(r"## Sprint (\d+) goal\n(.*?)(?=\n##|\Z)", prompt, re.DOTALL)
    if not match:
        return 0, "the requested sprint goal"
    goal = " ".join(match.group(2).split())
    return int(match.group(1)), goal[:180] or "the requested sprint goal"


def infer_mock_disciplines(goal):
    """Choose the smallest deterministic team that can reasonably deliver a goal."""
    text = goal.lower()
    has = lambda *terms: any(term in text for term in terms)
    authentication = has("auth", "login", "sign-in", "signin", "password", "jwt", "token", "permission", "authorization")
    documentation = has("documentation", "docs", "readme", "guide", "tutorial")
    infrastructure = has("infrastructure", "ci", "pipeline", "deploy", "deployment", "docker", "terraform", "github actions")
    frontend = has("frontend", "ui", "ux", "css", "layout", "screen", "page", "form", "button")
    backend = has("backend", "api", "endpoint", "server", "service", "webhook")
    database = has("database", "schema", "sql", "sqlite", "migration", "persist", "storage")
    bugfix = has("bug", "fix", "regression", "crash", "error")

    if authentication:
        return {"architect", "database", "backend", "frontend", "qa", "security"}
    if documentation:
        return {"manager", "qa"}
    if infrastructure:
        return {"architect", "backend", "security"}
    if frontend and not (backend or database):
        return ({"architect"} if not bugfix else set()) | {"frontend", "qa"}
    if backend or database:
        disciplines = {"architect", "backend", "qa", "security"}
        if database:
            disciplines.add("database")
        return disciplines
    if bugfix:
        return {"backend", "qa"}
    return {"architect", "backend", "qa"}


def mock_plan_for_goal(sprint, goal):
    """Build a dependency-ordered, scope-aware task graph for dry runs."""
    disciplines = infer_mock_disciplines(goal)
    ids = {"manager": f"mgr-{sprint}", "architect": f"arch-{sprint}", "database": f"db-{sprint}",
           "backend": f"be-{sprint}", "frontend": f"fe-{sprint}", "qa": f"qa-{sprint}", "security": f"sec-{sprint}"}
    titles = {
        "manager": f"Update project documentation for: {goal}",
        "architect": f"Define the technical approach for: {goal}",
        "database": f"Assess and update data requirements for: {goal}",
        "backend": f"Implement service behavior for: {goal}",
        "frontend": f"Implement user interface support for: {goal}",
        "qa": f"Validate the requested behavior for: {goal}",
        "security": f"Review security implications for: {goal}",
    }
    tasks = []
    planning_owner = "manager" if "manager" in disciplines else ("architect" if "architect" in disciplines else None)
    if planning_owner:
        tasks.append({"id": ids[planning_owner], "title": titles[planning_owner], "owner": planning_owner, "depends_on": []})
    previous = ids.get(planning_owner) if planning_owner else None
    for role in ("database", "backend", "frontend"):
        if role not in disciplines:
            continue
        tasks.append({"id": ids[role], "title": titles[role], "owner": role, "depends_on": [previous] if previous else []})
        previous = ids[role]
    for role in ("qa", "security"):
        if role in disciplines:
            tasks.append({"id": ids[role], "title": titles[role], "owner": role, "depends_on": [previous] if previous else []})
    return disciplines, tasks


def call_codex_mock(role, prompt):
    """Deterministic dry-run responses whose work items mirror the sprint goal."""
    _time.sleep(0.2)
    sprint, goal = mock_sprint_context(prompt)
    disciplines, tasks = mock_plan_for_goal(sprint, goal)
    manager_tasks = [task for task in tasks if task["owner"] in {"manager", "architect"}]
    implementation_tasks = [task for task in tasks if task["owner"] not in {"manager", "architect"}]
    canned = {
        "manager": {
            "summary": f"Created a deterministic plan for: {goal}", "task_status": "done",
            "new_tasks": manager_tasks,
            "messages": [{"to": "architect", "content": f"Design an implementation plan for: {goal}"}],
        },
        "architect": {
            "summary": f"Defined the implementation approach for: {goal}", "task_status": "done",
            "decisions": [f"Use the existing project stack to implement: {goal}"],
            "new_tasks": implementation_tasks,
        },
        "backend": {"summary": f"Implemented service behavior for: {goal}", "task_status": "done", "files_changed": ["server.py"]},
        "database": {"summary": f"Updated data support for: {goal}", "task_status": "done", "files_changed": ["database.py"]},
        "frontend": {"summary": f"Implemented UI support for: {goal}", "task_status": "done", "files_changed": ["static/index.html"]},
        "qa": {"summary": f"Validated the requested behavior for: {goal}", "task_status": "done", "blockers": []},
        "security": {"summary": f"Reviewed the implementation for: {goal}", "task_status": "done", "decisions": [f"Review validation and access controls for: {goal}"]},
    }
    if role == "manager" and "The sprint is complete." in prompt:
        return {"summary": f"Mock retrospective: completed the planned work for {goal}.", "task_status": "done"}
    if role == "manager" and '"owner": "manager"' in prompt:
        return {"summary": f"Updated documentation for: {goal}", "task_status": "done"}
    return canned.get(role, {"summary": f"No-op for: {goal}", "task_status": "done"})


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
        record_task_status(state, t, "todo")
        existing_ids.add(t["id"])


def record_task_status(state, task, status):
    """Record task-state transitions so a completed sprint can be replayed."""
    state.setdefault("task_events", []).append({
        "task_id": task["id"], "status": status, "time": _time.time(),
        "sprint": state["sprint"]["number"],
    })


def send_manager_message(state, recipient, content):
    """Persist manager coordination so it is visible live and in sprint replay."""
    state["messages"].append({"from": "manager", "to": recipient, "content": content, "time": _time.time()})


def announce_dependencies(state):
    """Give implementation agents advance notice of their planned dependencies."""
    tasks_by_id = {task["id"]: task for task in state["tasks"]}
    for task in state["tasks"]:
        if task["owner"] not in {"backend", "frontend"} or not task.get("depends_on"):
            continue
        dependencies = [tasks_by_id[dep]["title"] for dep in task["depends_on"] if dep in tasks_by_id]
        if dependencies:
            send_manager_message(state, task["owner"],
                                 f"Dependency notification: your task '{task['title']}' will begin after {', '.join(dependencies)} is complete.")


def coordinate_completed_task(state, completed_task):
    """Notify dependent engineers when work unblocks and hand off completed work for review."""
    if completed_task["status"] != "done":
        return
    tasks_by_id = {task["id"]: task for task in state["tasks"]}
    for task in state["tasks"]:
        if completed_task["id"] not in task.get("depends_on", []):
            continue
        if all(tasks_by_id.get(dep, {}).get("status") == "done" for dep in task["depends_on"]):
            send_manager_message(state, task["owner"],
                                 f"Dependency complete: '{completed_task['title']}' is done. You may now proceed with '{task['title']}'.")


def send_review_handoff(state):
    implementation_tasks = [task for task in state["tasks"] if task["owner"] not in {"manager", "qa", "security"}]
    if implementation_tasks and not all(task["status"] == "done" for task in implementation_tasks):
        return
    messages = state["messages"]
    needs_qa = not MOCK or any(task["owner"] == "qa" for task in state["tasks"])
    needs_security = not MOCK or any(task["owner"] == "security" for task in state["tasks"])
    if needs_qa and not any(message["from"] == "manager" and message["to"] == "qa" and message["content"].startswith("QA handoff:") for message in messages):
        send_manager_message(state, "qa", "QA handoff: implementation work is ready for regression and integration validation.")
    if needs_security and not any(message["from"] == "manager" and message["to"] == "security" and message["content"].startswith("Security review request:") for message in messages):
        send_manager_message(state, "security", "Security review request: implementation work is ready for authentication, authorization, and input-handling review.")


def apply_turn(state, role, output, planning_open=False):
    state["agents"][role]["status"] = output.get("task_status", "done")
    for f in output.get("files_changed", []):
        state["files_owned"][f] = role
    for msg in output.get("messages", []):
        state["messages"].append({"from": role, "to": msg["to"], "content": msg["content"], "time": _time.time()})
    add_tasks(state, output.get("new_tasks", []), allow=planning_open)
    for d in output.get("decisions", []):
        state["architecture_decisions"].append({"by": role, "decision": d, "sprint": state["sprint"]["number"]})
    for b in output.get("blockers", []):
        state["blockers"].append({"role": role, "text": b, "sprint": state["sprint"]["number"], "time": _time.time()})
    return output.get("blockers", [])


def migrate_commit_sprints(commits, fallback_sprint):
    """Backfill legacy commit records using retrospective commits as sprint boundaries.

    Commit logs are newest-first. A retrospective marks the sprint for itself and
    all following older records until the previous retrospective boundary.
    """
    current_sprint = fallback_sprint
    changed = False
    for commit in commits:
        match = RETROSPECTIVE_SPRINT_RE.search(commit.get("message", ""))
        inferred_sprint = int(match.group(1)) if match else current_sprint
        if commit.get("sprint") is None:
            commit["sprint"] = inferred_sprint
            changed = True
        current_sprint = int(commit.get("sprint", inferred_sprint))
        if match:
            current_sprint = int(match.group(1))
    return changed


def git_commit(role, summary, sprint_number=None, output=None):
    """Commit work and persist the agent output that produced that commit."""
    output = output or {}
    metadata = {
        "summary": summary,
        "files_changed": list(output.get("files_changed", [])),
        "decisions": list(output.get("decisions", [])),
        "blockers": list(output.get("blockers", [])),
        "new_tasks": list(output.get("new_tasks", [])),
    }
    try:
        existing_commits = json.loads((ROOT / "logs" / "commits.json").read_text())
    except (OSError, json.JSONDecodeError):
        existing_commits = []
    migrate_commit_sprints(existing_commits, sprint_number)
    known_sprints = {c.get("hash"): c.get("sprint") for c in existing_commits if c.get("sprint") is not None}
    metadata_keys = ("summary", "files_changed", "decisions", "blockers", "new_tasks")
    known_metadata = {c.get("hash"): {key: c[key] for key in metadata_keys if key in c} for c in existing_commits}
    before = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    subprocess.run(["git", "add", "-A"], cwd=ROOT, check=False)
    commit_result = subprocess.run(
        ["git", "-c", f"user.name={role}", "-c", f"user.email={role}@codex-org.local",
         "commit", "-m", f"[{role}] {summary}", "--allow-empty"],
        cwd=ROOT, check=False, capture_output=True,
    )
    after = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    new_hash = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True
    ).stdout.strip() if commit_result.returncode == 0 and after != before else None
    log = subprocess.run(["git", "log", "--pretty=format:%h|%an|%ct|%s"],
                          cwd=ROOT, capture_output=True, text=True)
    commits = []
    for line in log.stdout.splitlines():
        parts = line.split("|", 3)
        if len(parts) == 4:
            entry = {"hash": parts[0], "role": parts[1], "time": int(parts[2]), "message": parts[3]}
            sprint = sprint_number if parts[0] == new_hash else known_sprints.get(parts[0])
            if sprint is not None:
                entry["sprint"] = sprint
            entry.update(metadata if parts[0] == new_hash else known_metadata.get(parts[0], {}))
            commits.append(entry)
    migrate_commit_sprints(commits, sprint_number)
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
    for task in state["tasks"]:
        record_task_status(state, task, "todo")


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
        git_commit("manager", output.get("summary", "sprint planning"), state["sprint"]["number"], output)
        save_state(state)

        prompt = build_prompt("architect", state, None)
        output = call_codex("architect", prompt)
        add_tasks(state, output.get("new_tasks", []), allow=True)
        for d in output.get("decisions", []):
            state["architecture_decisions"].append({"by": "architect", "decision": d, "sprint": state["sprint"]["number"]})
        git_commit("architect", output.get("summary", "architecture planning"), state["sprint"]["number"], output)

    announce_dependencies(state)
    state["planning_frozen_sprint"] = state["sprint"]["number"]
    save_state(state)


def run_dev_loop(state, max_rounds=6):
    seen_blocker_signature = None
    for round_num in range(max_rounds):
        any_work = False
        current_blockers = tuple(sorted(t["id"] for t in state["tasks"] if t["status"] == "blocked"))

        for role in ROLE_ORDER:
            if role == "manager":
                task = next_ready_task(state, role)
                if not task:
                    if not current_blockers or current_blockers == seen_blocker_signature:
                        continue
                    seen_blocker_signature = current_blockers
            else:
                task = next_ready_task(state, role)
                if not task:
                    continue

            prompt = build_prompt(role, state, task)
            print(f"--- Round {round_num+1}: {role} working on {task['id'] if task else '(reviewing blockers)'} ---")
            if task:
                task["status"] = "in_progress"
                record_task_status(state, task, "in_progress")
            output = call_codex(role, prompt)
            blockers = apply_turn(state, role, output, planning_open=False)
            if task:
                task["status"] = output.get("task_status", "done")
                record_task_status(state, task, task["status"])
                coordinate_completed_task(state, task)
                send_review_handoff(state)
            git_commit(role, output.get("summary", "update"), state["sprint"]["number"], output)
            save_state(state)
            any_work = True
            if blockers:
                print(f"  blockers: {blockers}")
        if not any_work:
            break

    send_review_handoff(state)
    final_roles = ["qa", "security"] if not MOCK else [
        role for role in ["qa", "security"] if any(task["owner"] == role for task in state["tasks"])
    ]
    for role in final_roles:
        if not any(t["owner"] == role for t in state["tasks"]):
            task = {"id": f"auto-{role}-s{state['sprint']['number']}", "title": f"Final {role} pass",
                    "owner": role, "depends_on": [], "status": "todo"}
            state["tasks"].append(task)
            record_task_status(state, task, "todo")
        task = next_ready_task(state, role)
        if task:
            prompt = build_prompt(role, state, task)
            print(f"--- Final pass: {role} ---")
            task["status"] = "in_progress"
            record_task_status(state, task, "in_progress")
            output = call_codex(role, prompt)
            blockers = apply_turn(state, role, output, planning_open=False)
            task["status"] = output.get("task_status", "done")
            record_task_status(state, task, task["status"])
            git_commit(role, output.get("summary", "final pass"), state["sprint"]["number"], output)
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
    state["sprint"]["completed_at"] = _time.time()
    save_state(state)
    git_commit("manager", f"Sprint {state['sprint']['number']} retrospective complete", state["sprint"]["number"], retro)
    print(json.dumps(state["retrospective"], indent=2))


def start_new_sprint(state, new_goal):
    sprint_number = state["sprint"]["number"]
    try:
        commits = json.loads((ROOT / "logs" / "commits.json").read_text())
    except (OSError, json.JSONDecodeError):
        commits = []
    if migrate_commit_sprints(commits, sprint_number):
        (ROOT / "logs" / "commits.json").write_text(json.dumps(commits, indent=2))
    state["sprint_history"].append({
        "number": sprint_number, "goal": state["sprint"]["goal"],
        "tasks": state["tasks"], "messages": state["messages"], "retrospective": state["retrospective"],
        "blockers": [b for b in state["blockers"] if b.get("sprint") == sprint_number],
        "task_events": [e for e in state.get("task_events", []) if e.get("sprint") == sprint_number],
        "commits": [c for c in commits if c.get("sprint") == sprint_number],
        "started_at": state["sprint"].get("started_at"),
        "completed_at": state["sprint"].get("completed_at"),
    })
    state["sprint"] = {"number": sprint_number + 1, "goal": new_goal, "status": "not_started", "started_at": None, "completed_at": None}
    state["tasks"] = []
    state["messages"] = []
    state["retrospective"] = None
    state["task_events"] = []
    for role in state["agents"]:
        state["agents"][role] = {"status": "idle", "current_task": None}
    return state


def run_sprint(goal_override=None):
    if MOCK:
        print("\n*** RUNNING IN MOCK MODE (CODEX_ORG_MOCK not set to 0) — no real Codex calls, no credits spent. ***\n")
    state = load_state()
    if goal_override:
        state = start_new_sprint(state, goal_override)
    state["sprint"]["status"] = "in_progress"
    if not state["sprint"].get("started_at"):
        state["sprint"]["started_at"] = _time.time()
    # Persist a newly created sprint before planning invokes the first agent.
    # call_codex_real reads STATE_PATH to tag usage entries by sprint.
    save_state(state)
    plan_sprint(state)
    run_dev_loop(state)
    run_retrospective(state)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--reset":
        STATE_PATH.write_text(json.dumps(new_state(), indent=2))
        if SESSION_ID_PATH.exists():
            SESSION_ID_PATH.unlink()
        if USAGE_LOG_PATH.exists():
            USAGE_LOG_PATH.unlink()
        print("State reset to a fresh project. Session ID and usage log cleared.")
        sys.exit(0)
    goal_arg = sys.argv[1] if len(sys.argv) > 1 else None
    run_sprint(goal_arg)
