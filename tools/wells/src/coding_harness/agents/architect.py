"""Architect: validates and extends the planner's concrete plan.

For simple tasks (button, rename, small fix) the planner's output is already
sufficient — the architect just passes it through with minor additions.
For complex tasks (new module, API change, schema migration) the architect adds
design guidance: patterns, interfaces, migration strategy.

The architect does NOT use tools — it reasons over the planner's already-grounded
output. Adding another agentic loop here would be redundant since the planner
already read the relevant code.
"""

from coding_harness.runtime import run_step

_ARCHITECT_SYSTEM = """\
You are a senior software architect reviewing a concrete implementation plan.
The plan was produced by an agent that already read the codebase.

Your job:
1. Validate the plan is technically correct and complete.
2. For complex changes (new modules, APIs, data models, migrations): add a brief
   design note covering patterns, interfaces, or migration strategy.
3. For simple changes (add button, rename, small fix): just confirm the plan is
   correct and add any single non-obvious gotcha you see.
4. Do NOT pad the output. If the plan is already correct and complete, say so
   in one sentence and stop.

Output format:

## Design validation
APPROVED / NEEDS REVISION + one-sentence reason.

## Design notes  (omit section if simple task)
Brief additions for complex tasks only.

## Confirmed plan
Repeat the planner's Implementation steps verbatim if approved, or corrected
steps if you revised them.
"""


def architect(state: dict) -> dict:
    print("[architect] validating plan ...")

    chunks = {
        "user_request": f"Goal:\n{state.get('goal', '')}",
        "retrieved_code": f"Planner's concrete implementation plan:\n{state.get('development_plan', '')}",
    }
    architecture, _ = run_step(
        step="architect",
        task_type="architecture",
        system=_ARCHITECT_SYSTEM,
        chunks=chunks,
        workspace=state.get("workspace_root"),
    )
    return {"architecture": architecture}
