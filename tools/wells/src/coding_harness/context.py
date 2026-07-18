"""Context assembly and budget trimming.

The ContextManager builds the variable part of each model prompt from labeled
category chunks. It measures tokens per category (for the usage report), orders
them deterministically (prompt-cache friendly), and — only when a call would
exceed its budget — trims the lowest-priority categories per the spec's
"Context Assembly Priority". System/developer/user-request and active-error
context are never trimmed.

The assembled text is a clean concatenation of the agent-provided chunks, so the
wording the model sees is essentially unchanged from before (no injected labels).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from coding_harness.tokens import TokenBudget, estimate_tokens

# Categories trimmed FIRST when over budget (most expendable -> least).
# Categories not listed here (system_prompt, developer_instructions, user_request)
# are never trimmed.
TRIM_ORDER = [
    "tool_outputs",        # verbose logs / review chatter
    "recent_conversation", # stale assistant turns
    "diffs",               # diffs not relevant to the current step
    "task_state_summary",  # historical summary
    "retrieved_code",      # low-relevance code chunks
]

# Floor (in tokens) kept for a category when it must be trimmed: we keep the most
# recent tail so the actionable signal (recent errors / latest steps) survives.
_TRIM_FLOOR_TOKENS = {
    "tool_outputs": 400,
    "recent_conversation": 300,
    "diffs": 200,
    "task_state_summary": 200,
    "retrieved_code": 500,
}


@dataclass
class ContextReport:
    category_tokens: dict = field(default_factory=dict)
    trimmed_items: list = field(default_factory=list)
    saved_by_trim: int = 0
    warnings: list = field(default_factory=list)


class ContextManager:
    def __init__(self, budget: TokenBudget | None = None) -> None:
        self.budget = budget or TokenBudget()

    def build_context(self, chunks: dict) -> tuple[str, ContextReport]:
        """Assemble `chunks` (insertion-ordered category -> text) into prompt text.

        Returns the assembled text and a report with per-category token counts and
        any trimming that was applied to fit the budget.
        """
        working = {k: (v or "") for k, v in chunks.items()}
        report = ContextReport(
            category_tokens={k: estimate_tokens(v) for k, v in working.items()}
        )

        total = sum(report.category_tokens.values())
        if total > self.budget.input_allowance:
            self._trim_to_budget(working, report)

        text = "\n\n".join(v for v in working.values() if v.strip())
        return text, report

    def _trim_to_budget(self, working: dict, report: ContextReport) -> None:
        limit = self.budget.input_allowance
        total = sum(report.category_tokens.values())
        for category in TRIM_ORDER:
            if total <= limit:
                break
            if category not in working:
                continue
            original = working[category]
            orig_tokens = report.category_tokens.get(category, 0)
            floor = _TRIM_FLOOR_TOKENS.get(category, 200)
            if orig_tokens <= floor:
                continue
            # Keep the most recent tail (recent errors / latest steps matter most).
            kept = _tail_to_token_budget(original, floor)
            kept_tokens = estimate_tokens(kept)
            saved = orig_tokens - kept_tokens
            if saved <= 0:
                continue
            working[category] = (
                f"...[trimmed {saved} older tokens to stay within budget]...\n{kept}"
            )
            report.category_tokens[category] = kept_tokens
            report.trimmed_items.append(category)
            report.saved_by_trim += saved
            total -= saved
        if total > limit:
            report.warnings.append(
                f"context still {total - limit} tokens over budget after trimming"
            )


def _tail_to_token_budget(text: str, target_tokens: int) -> str:
    """Return the tail of `text` roughly fitting `target_tokens` tokens."""
    if not text:
        return ""

    lines = text.splitlines()
    kept: list[str] = []
    running = 0
    for ln in reversed(lines):
        n = estimate_tokens(ln)
        if running + n > target_tokens and kept:
            break
        kept.append(ln)
        running += n
    kept.reverse()
    out = "\n".join(kept)

    # A single oversized line (no newlines to split on) still blows the budget:
    # char-truncate it down to roughly the target.
    est = estimate_tokens(out)
    if est > target_tokens and out:
        ratio = len(out) / max(1, est)
        cut = max(1, int(ratio * target_tokens))
        out = "..." + out[-cut:]
    return out
