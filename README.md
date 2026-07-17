# Codex Org

**Question this project answers:** can multiple Codex agents function as an actual
software engineering organization — not just parallel code generation, but role-based
planning, delegation, blocking, and a real retrospective — end to end?

## How it works
- `state/state.json` is the single shared source of truth (tasks, ownership, messages, decisions).
- `agents/roles/*.md` define each role's responsibilities (Manager, Architect, Backend,
  Frontend, Database, QA, Security).
- `orchestrator/run.py` loops through roles in dependency order, calls **Codex CLI
  (`codex exec`)** with a role-specific prompt + shared context, and forces structured
  output via `--output-schema state/schemas/agent_turn.json`.
- Each agent's turn is applied to the shared state and committed to git under that
  agent's name — the git log **is** the org's real work history, not a simulation.
- At the end, the Manager reads the full message/blocker log and generates a grounded
  retrospective (no invented events — only what actually happened in the run).

## Where Codex accelerated the workflow
[to fill in after the real run: e.g. specific moments Codex made a design call,
resolved a blocker, or caught something we didn't anticipate]

## Setup
```bash
git clone <this repo>
cd codex-org
npm install -g @openai/codex   # or your existing Codex CLI install
codex login                    # or set CODEX_API_KEY
```

Run a full sprint with real Codex:
```bash
CODEX_ORG_MOCK=0 python3 orchestrator/run.py
```

Run the dry-run (no credits needed, uses canned responses to verify the pipeline):
```bash
CODEX_ORG_MOCK=1 python3 orchestrator/run.py
```

## Codex Session ID
`/feedback` session ID: [fill in from the real run used for core functionality]

## Sample data
None required — the sprint goal is set in `state/state.json` (`sprint.goal`); edit it to
point the org at whatever demo app you want it to build.
