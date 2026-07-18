"""Budget guard for paid AI calls.

This is deliberately small and file-backed. The stack can use paid models as a
quality accelerator, but it should not silently run past Operator's monthly target.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from core import BASE_DIR, mountain_now
from core.constants import MODEL_COST_RATES


COST_LOG = BASE_DIR / "data" / "logs" / "cost.jsonl"
BUDGET_STATE = BASE_DIR / "data" / "runtime" / "ai_budget_state.json"

DEFAULT_MONTHLY_BUDGET_USD = 120.0
DEFAULT_WARN_FRACTION = 0.80
UNLIMITED_BUDGET_USD = 1_000_000_000_000.0


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def budget_enforced() -> bool:
    if _truthy(os.environ.get("CLAUDE_STACK_DISABLE_AI_BUDGET")):
        return False
    raw_budget = str(os.environ.get("CLAUDE_STACK_MONTHLY_AI_BUDGET_USD", "")).strip().lower()
    if raw_budget in {"0", "off", "none", "no", "false", "disabled", "disable", "unlimited"}:
        return False
    return True


def monthly_budget_usd() -> float:
    if not budget_enforced():
        return UNLIMITED_BUDGET_USD
    raw = str(os.environ.get("CLAUDE_STACK_MONTHLY_AI_BUDGET_USD", "")).strip()
    try:
        value = float(raw) if raw else DEFAULT_MONTHLY_BUDGET_USD
    except Exception:
        value = DEFAULT_MONTHLY_BUDGET_USD
    return max(1.0, value)


def _month_key() -> str:
    return mountain_now().strftime("%Y-%m")


def _entry_month(entry: dict[str, Any]) -> str:
    value = str(entry.get("date") or entry.get("ts") or "").strip()
    if len(value) >= 7:
        return value[:7]
    return ""


def _read_cost_entries() -> list[dict[str, Any]]:
    if not COST_LOG.exists():
        return []
    try:
        lines = COST_LOG.read_text().splitlines()
    except Exception:
        return []
    rows: list[dict[str, Any]] = []
    for line in lines:
        try:
            row = json.loads(line)
        except Exception:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def monthly_spend_usd(month: str | None = None) -> float:
    target = month or _month_key()
    total = 0.0
    for row in _read_cost_entries():
        if _entry_month(row) != target:
            continue
        try:
            total += float(row.get("cost") or 0.0)
        except Exception:
            pass
    return round(total, 6)


def estimate_call_cost_usd(model: str, messages: list | None, *, system: Any = None, max_tokens: int = 4096) -> float:
    """Conservative estimate before the provider returns exact token counts."""
    rates = MODEL_COST_RATES.get(str(model or ""), {"input": 3.0, "output": 15.0})
    text = str(system or "") + "\n" + str(messages or "")
    input_tokens = max(1, int(len(text) / 4))
    # Most chat replies are much shorter than max_tokens. Reserve enough for a
    # useful answer without treating every call like a full document generation.
    output_tokens = max(64, min(int(max_tokens or 0), 1200))
    cost = (input_tokens * rates["input"] / 1_000_000) + (output_tokens * rates["output"] / 1_000_000)
    return round(cost, 6)


def budget_status() -> dict[str, Any]:
    enforced = budget_enforced()
    budget = monthly_budget_usd()
    spent = monthly_spend_usd()
    remaining = max(0.0, budget - spent)
    return {
        "month": _month_key(),
        "budget_enforced": enforced,
        "budget_usd": round(budget, 2),
        "spent_usd": round(spent, 6),
        "remaining_usd": round(remaining, 6),
        "warn_at_usd": round(budget * DEFAULT_WARN_FRACTION, 2),
        "warn": enforced and spent >= budget * DEFAULT_WARN_FRACTION,
        "exhausted": enforced and spent >= budget,
    }


def paid_call_allowed(model: str, messages: list | None, *, system: Any = None, max_tokens: int = 4096) -> dict[str, Any]:
    status = budget_status()
    estimated = estimate_call_cost_usd(model, messages, system=system, max_tokens=max_tokens)
    allowed = (not status.get("budget_enforced", True)) or (status["spent_usd"] + estimated) <= status["budget_usd"]
    decision = {
        **status,
        "model": model,
        "estimated_call_usd": estimated,
        "allowed": allowed,
        "reason": "",
    }
    if not status.get("budget_enforced", True):
        decision["reason"] = "monthly AI budget enforcement disabled"
        return decision
    if not allowed:
        decision["reason"] = (
            f"monthly AI budget would be exceeded: spent ${status['spent_usd']:.2f}, "
            f"estimated call ${estimated:.4f}, budget ${status['budget_usd']:.2f}"
        )
    return decision


def write_budget_state(extra: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = {
        "generated_at": datetime.now().isoformat(),
        **budget_status(),
    }
    if extra:
        payload.update(extra)
    BUDGET_STATE.parent.mkdir(parents=True, exist_ok=True)
    BUDGET_STATE.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str))
    return payload
