"""Finisher: post-coder git + memory write-back (Layer 3 finalizer node).

Runs at the end of a *successful* harness run (review COMPLETE) to:
  1. Append a structured lesson to AGENTS.md so the harness learns across runs.
  2. Optionally create a ``wells/<slug>`` branch + commit and open a PR via gh.

This is a graph node, not an agent: it does deterministic post-processing using
:mod:`coding_harness.gitops` and :mod:`coding_harness.memory`, and never calls
the model. It runs only when the reviewer marked the work complete OR when the
loop cap was reached (so changes are still captured), and only when there is
something to capture (an actual code change).

Safety: writes go through the safety gate like every other tool. When
``HARNESS_SAFETY=dryrun`` or ``plan_mode`` is on, the finisher skips git commits
and only records what *would* have happened — so dry-runs are truly side-effect
free.
"""

from __future__ import annotations

import os

from coding_harness import gitops, memory
from coding_harness.tools import ToolContext


def finisher(state: dict) -> dict:
    """Finalize a run: write back memory, optionally branch/commit/PR.

    Returns a small dict with ``pr_url`` / ``git_summary`` keys for the final
    report. Failures are non-fatal — the run already succeeded.
    """
    ctx = ToolContext.from_state(state)
    dry = ctx.plan_mode or ctx.safety == "dryrun"

    goal = state.get("goal", "")
    summary = (
        state.get("implementation_steps", "") or state.get("code_changes", "") or ""
    )
    review = state.get("review_result", "") or ""
    complete = bool(state.get("review_complete"))

    # ----- 1. Memory write-back (best-effort, even in plan/dry mode we skip) --
    memory_path = None
    if not dry and summary:
        try:
            memory_path = memory.append_lesson(
                ctx.workspace,
                goal=goal,
                summary=_first_line(summary),
                key_files=_extract_files(summary),
                commands=_extract_commands(review),
                gotchas=_extract_gotchas(review),
            )
        except Exception as e:
            print(f"[finisher] memory write skipped: {e}")

    # ----- 2. Git: branch + commit + (optionally) PR -------------------------
    git_result = None
    open_pr = os.environ.get("WELLS_OPEN_PR", "0") in ("1", "true", "yes")
    if not dry and _wants_git(state):
        try:
            git_result = gitops.create_branch_and_commit(
                ctx,
                goal=goal,
                message=_commit_message(goal, summary, complete),
            )
            if open_pr and git_result.ok:
                body = _pr_body(state, git_result)
                git_result = gitops.push_and_open_pr(
                    ctx, result=git_result, goal=goal, body=body
                )
            print(f"[finisher] {git_result.summary()}")
        except Exception as e:
            print(f"[finisher] git step skipped: {e}")
            git_result = None
    elif dry:
        print(
            "[finisher] dry-run / plan mode — skipping git commit (changes left on disk)"
        )

    out: dict = {"finalized": True}
    if memory_path:
        out["memory_written"] = str(memory_path)
    if git_result is not None:
        out["git_summary"] = git_result.summary()
        if git_result.pr_url:
            out["pr_url"] = git_result.pr_url
    return out


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _wants_git(state: dict) -> bool:
    """True when the run produced code worth capturing in git."""
    # Only commit when the coder actually produced a change summary, regardless
    # of whether the reviewer marked it complete (the cap may have been hit).
    return bool(state.get("implementation_steps") or state.get("code_changes"))


def _first_line(text: str, *, limit: int = 400) -> str:
    for ln in (text or "").splitlines():
        s = ln.strip()
        if s:
            return s[:limit]
    return (text or "").strip()[:limit]


def _commit_message(goal: str, summary: str, complete: bool) -> str:
    status = "complete" if complete else "incomplete (loop cap reached)"
    first = _first_line(summary, limit=160)
    return f"wells: {goal.strip()[:100]} [{status}]\n\n{first}"


def _pr_body(state: dict, result: gitops.GitResult) -> str:
    goal = state.get("goal", "")
    summary = state.get("implementation_steps", "") or ""
    review = state.get("review_result", "") or ""
    body = f"## Goal\n{goal}\n\n## Summary\n{summary[:1500]}\n"
    if review:
        body += f"\n## Review\n{review[:1000]}\n"
    if result.diff_stat:
        body += f"\n## Changes\n```\n{result.diff_stat}\n```\n"
    return body


def _extract_files(text: str, *, max_n: int = 15) -> list[str]:
    """Best-effort file path extraction from the coder's change summary."""
    import re

    if not text:
        return []
    pat = re.compile(
        r"([\w./\-]+\.(?:py|js|ts|tsx|jsx|go|rs|java|rb|md|toml|yaml|yml|json|sh))"
    )
    seen: list[str] = []
    for m in pat.finditer(text):
        f = m.group(1)
        if f not in seen:
            seen.append(f)
        if len(seen) >= max_n:
            break
    return seen


def _extract_commands(text: str, *, max_n: int = 8) -> list[str]:
    """Pull likely test/lint commands out of reviewer/tester output."""
    import re

    if not text:
        return []
    out: list[str] = []
    for m in re.finditer(r"`((?:pytest|npm|yarn|cargo|go|make|uv run)[^`]+)`", text):
        out.append(m.group(1))
        if len(out) >= max_n:
            break
    return out


def _extract_gotchas(text: str, *, max_n: int = 6) -> list[str]:
    """Heuristic: sentences in the review that mention risk/edge/beware."""
    if not text:
        return []
    import re

    out: list[str] = []
    for sentence in re.split(r"(?<=[.!])\s+", text):
        s = sentence.strip()
        if s and re.search(
            r"\b(risk|edge case|beware|caveat|gotcha|watch out|ensure|must)\b", s, re.I
        ):
            out.append(s[:200])
        if len(out) >= max_n:
            break
    return out
