"""Rolling task-state summary (Phase 3).

On loop iterations the coder would otherwise re-send the full plan + architecture
verbatim. When that durable context is large we replace it with a compact,
structured summary; when it is small we keep it verbatim. This protects output
quality on small tasks (zero change) while saving tokens on large ones.

The actionable signal (the latest review feedback) is never summarized away — it
travels verbatim as ``tool_outputs`` in the coder step.
"""

from __future__ import annotations

from coding_harness.config import (
    SMALL_BUDGET,
    SUMMARIZE_ON_LOOP,
    SUMMARIZE_THRESHOLD,
)
from coding_harness.tokens import estimate_tokens

SUMMARY_SYSTEM = (
    "You are a precise technical summarizer. Condense the provided development "
    "plan and architecture into a short, structured task-state summary. Preserve "
    "every durable fact, constraint, decision, and key risk verbatim where short. "
    "Omit verbose reasoning. Do not invent details."
)

SUMMARY_SCHEMA = """## Task State Summary

### Goal
### User Constraints
### Current Plan
### Architecture Decisions
### Important Findings / Risks
### Next Action
"""


def _durable_context(state: dict) -> str:
    plan = state.get("development_plan", "")
    arch = state.get("architecture", "")
    parts = []
    if plan:
        parts.append(f"Development plan:\n{plan}")
    if arch:
        parts.append(f"Architecture:\n{arch}")
    return "\n\n".join(parts)


def should_summarize(state: dict) -> bool:
    """True if the durable context is large enough that summarizing is worth it."""
    return estimate_tokens(_durable_context(state)) > SUMMARIZE_THRESHOLD


def summarizer_node(state: dict) -> dict:
    """Graph node: produce a task_summary used by later coder iterations.

    - summarization disabled -> empty summary (coder falls back to verbatim).
    - durable context small   -> keep it verbatim (no model call, no savings).
    - durable context large   -> condense via a cheap model call.
    """
    if not SUMMARIZE_ON_LOOP:
        return {"task_summary": ""}

    durable = _durable_context(state)
    if not durable:
        return {"task_summary": ""}

    if estimate_tokens(durable) <= SUMMARIZE_THRESHOLD:
        # Small enough to keep verbatim: zero output impact, no extra call.
        return {"task_summary": durable}

    # Large context: condense the durable plan + architecture. The latest review
    # feedback is intentionally NOT summarized here; it travels verbatim to the
    # coder as ``tool_outputs`` so the actionable signal is never lossy.
    chunks = {"task_state_summary": SUMMARY_SCHEMA, "retrieved_code": durable}

    from coding_harness.runtime import run_step

    summary, _ = run_step(
        step="summarizer",
        task_type="summarization",
        system=SUMMARY_SYSTEM,
        chunks=chunks,
        budget=SMALL_BUDGET,
    )
    return {"task_summary": summary}
