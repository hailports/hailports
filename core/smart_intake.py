"""Smart intake classifier — picks cost tier before any model call.

Tier 0: Local Ollama (free)      — simple lookups, counts, quick facts
Tier 1: DeepSeek V4 Flash        — moderate reasoning, queries, triage
Tier 2: Haiku / GPT-4o-mini      — multi-tool, drafts, summarization
Tier 3: Sonnet / GPT-4o          — complex tasks, important writing
Tier 4: Opus / o3                — explicit god-mode or high-stakes only

Budget cap: $2 per interaction (configurable via INTAKE_BUDGET_USD).
"""

import re
import os

BUDGET_CAP_USD = float(os.environ.get("INTAKE_BUDGET_USD", "2.0"))

# Tier 4 — explicit escalation only
_TIER4_TRIGGERS = re.compile(
    r"\b(god mode|go deep|think hard|full analysis|spare no expense|opus|o3|best model)\b", re.I
)

# Tier 3 — complex writing/synthesis
_TIER3_TRIGGERS = re.compile(
    r"\b(write a report|full strategy|deep dive|root cause|comprehensive|executive summary|"
    r"presentation deck|slide deck|prepare a brief|long form|detailed plan)\b", re.I
)

# Tier 2 — moderate multi-step or external writes
_TIER2_TRIGGERS = re.compile(
    r"\b(draft an email|send email|reply to|schedule a meeting|create an? (event|task|item|ticket)|"
    r"update (salesforce|monday|sf)|book|reschedule|cancel meeting|"
    r"summarize (my|the) (email|inbox|thread|week)|weekly briefing|"
    r"cross.reference|compare|multi.step)\b", re.I
)

# Tier 0 anchors — always free regardless of length
_TIER0_ANCHORS = re.compile(
    r"\b(how many|count|ping|status|health|what time|what day|is .+ running|"
    r"quick (check|look|question)|inbox count|unread|next meeting)\b", re.I
)

# Multi-tool signals bump tier up by 1
_MULTI_TOOL_SIGNALS = re.compile(
    r"\b(and (also|then)|plus|as well as|while you're at it|also (check|look|send|update))\b", re.I
)

# Approximate cost per tier (USD per 1K tokens, blended in/out)
TIER_COST_PER_1K = {
    0: 0.0,
    1: 0.0002,
    2: 0.002,
    3: 0.008,
    4: 0.06,
}

# Model names per tier
TIER_MODELS = {
    0: "ollama/qwen2.5:7b",
    1: "openrouter/deepseek/deepseek-chat",
    2: "openrouter/anthropic/claude-haiku-4-5",
    3: "openrouter/anthropic/claude-sonnet-4-6",
    4: "openrouter/anthropic/claude-opus-4-7",
}


def classify(text: str, tool_count: int = 0) -> dict:
    """Return tier, model, estimated cost, and rationale for a given prompt."""
    text = str(text or "").strip()
    token_estimate = max(len(text) // 4, 50) * 3  # rough: prompt + expected response

    # Start with base tier
    if _TIER0_ANCHORS.search(text) and tool_count <= 1:
        tier = 0
        reason = "simple lookup / status check"
    elif _TIER4_TRIGGERS.search(text):
        tier = 4
        reason = "explicit god-mode trigger"
    elif _TIER3_TRIGGERS.search(text):
        tier = 3
        reason = "complex writing/synthesis"
    elif _TIER2_TRIGGERS.search(text) or tool_count >= 3:
        tier = 2
        reason = "multi-tool or external write"
    elif len(text) > 400 or tool_count >= 2:
        tier = 1
        reason = "moderate complexity"
    else:
        tier = 1
        reason = "default tier"

    # Multi-tool bump (not for tier 4)
    if tier < 4 and _MULTI_TOOL_SIGNALS.search(text):
        tier = min(tier + 1, 3)
        reason += " + multi-tool bump"

    # Cost check
    est_cost = (token_estimate / 1000) * TIER_COST_PER_1K[tier]
    if est_cost > BUDGET_CAP_USD:
        return {
            "tier": -1,
            "model": None,
            "estimated_cost_usd": round(est_cost, 4),
            "budget_cap_usd": BUDGET_CAP_USD,
            "blocked": True,
            "reason": f"estimated cost ${est_cost:.2f} exceeds ${BUDGET_CAP_USD} cap",
        }

    return {
        "tier": tier,
        "model": TIER_MODELS[tier],
        "estimated_cost_usd": round(est_cost, 6),
        "budget_cap_usd": BUDGET_CAP_USD,
        "blocked": False,
        "reason": reason,
    }
