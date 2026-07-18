from __future__ import annotations

"""Append-only ledger for machine payoff credits not represented in savings.jsonl.

Examples: Claude Code/Opus subscription credits consumed to improve the stack.
These are machine-payoff value entries, not revenue and not API spend.
"""

import fcntl
import json
import uuid
from pathlib import Path
from typing import Any

from core import BASE_DIR, mountain_now

LOG_FILE = BASE_DIR / "data" / "logs" / "machine_payoff_credits.jsonl"


def _num(value: Any) -> float:
    try:
        return float(value or 0.0)
    except Exception:
        return 0.0


def append_credit(
    *,
    source: str,
    kind: str,
    amount_usd: float,
    model: str = "",
    description: str = "",
    metadata: dict[str, Any] | None = None,
    log_file: Path | None = None,
) -> dict[str, Any]:
    amount = round(max(_num(amount_usd), 0.0), 6)
    now = mountain_now()
    entry: dict[str, Any] = {
        "id": "payoff-" + uuid.uuid4().hex[:12],
        "ts": now.isoformat(),
        "date": now.strftime("%Y-%m-%d"),
        "source": str(source or "unknown"),
        "kind": str(kind or "machine_payoff_credit"),
        "amount_usd": amount,
    }
    if model:
        entry["model"] = str(model)
    if description:
        entry["description"] = str(description)[:500]
    if metadata:
        entry["metadata"] = metadata

    path = log_file or LOG_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        f.write(json.dumps(entry, sort_keys=True, default=str) + "\n")
    return entry


def amount_from_entry(entry: dict[str, Any]) -> float:
    return max(_num(entry.get("amount_usd") or entry.get("credit_usd") or entry.get("value_usd")), 0.0)
