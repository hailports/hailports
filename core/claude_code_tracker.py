#!/usr/bin/env python3
"""Track Claude Code session costs and feed them into the stack's budget system.

Scans all ~/.claude/projects/*/  JSONL files, extracts token usage,
computes cost, and appends to data/logs/cost.jsonl with source='claude-code'.

Runs every 30 min via launchd. State tracked in data/runtime/cc_tracker_state.json
to avoid double-counting.
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.expanduser("~/claude-stack"))

BASE = Path(os.path.expanduser("~/claude-stack"))
CLAUDE_PROJECTS = Path(os.path.expanduser("~/.claude/projects"))
STATE_FILE = BASE / "data" / "runtime" / "cc_tracker_state.json"
COST_LOG = BASE / "data" / "logs" / "cc_cost.jsonl"  # separate from stack budget

# Pricing per million tokens
PRICING = {
    "claude-opus-4-6":          {"input": 15.0, "output": 75.0, "cache_write": 3.75, "cache_read": 1.50},
    "claude-opus-4-20250514":   {"input": 15.0, "output": 75.0, "cache_write": 3.75, "cache_read": 1.50},
    "claude-sonnet-4-6":        {"input": 3.0,  "output": 15.0, "cache_write": 3.75, "cache_read": 0.30},
    "claude-sonnet-4-20250514": {"input": 3.0,  "output": 15.0, "cache_write": 3.75, "cache_read": 0.30},
    "claude-haiku-4-5-20251001":{"input": 0.80, "output": 4.0,  "cache_write": 1.0,  "cache_read": 0.08},
    "claude-haiku-4-5":         {"input": 0.80, "output": 4.0,  "cache_write": 1.0,  "cache_read": 0.08},
}
DEFAULT_RATES = {"input": 3.0, "output": 15.0, "cache_write": 3.75, "cache_read": 0.30}


def compute_cost(model: str, usage: dict) -> float:
    rates = PRICING.get(model, DEFAULT_RATES)
    inp   = usage.get("input_tokens", 0)
    out   = usage.get("output_tokens", 0)
    cw    = usage.get("cache_creation_input_tokens", 0)
    cr    = usage.get("cache_read_input_tokens", 0)
    return (inp * rates["input"] + out * rates["output"] +
            cw * rates["cache_write"] + cr * rates["cache_read"]) / 1_000_000


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {"seen_uuids": [], "last_run": None, "total_tracked_usd": 0.0}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def append_cost(date_str: str, model: str, cost: float, source_file: str):
    entry = {
        "date": date_str,
        "model": model,
        "cost": round(cost, 8),
        "source": "claude-code",
        "file": Path(source_file).name,
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    with open(COST_LOG, "a") as f:
        f.write(json.dumps(entry) + "\n")


def scan_projects() -> list[Path]:
    if not CLAUDE_PROJECTS.exists():
        return []
    return list(CLAUDE_PROJECTS.rglob("*.jsonl"))


def main():
    state = load_state()
    seen = set(state.get("seen_uuids", []))
    new_seen = []
    total_cost = 0.0
    entries = 0

    for jsonl_file in scan_projects():
        try:
            text = jsonl_file.read_text(errors="ignore")
        except Exception:
            continue
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            uid = d.get("uuid") or d.get("requestId") or d.get("promptId")
            if not uid or uid in seen:
                continue
            msg = d.get("message", {})
            if not isinstance(msg, dict):
                continue
            usage = msg.get("usage")
            model = msg.get("model", "")
            if not usage or not isinstance(usage, dict) or not model:
                continue
            cost = compute_cost(model, usage)
            if cost <= 0:
                continue
            ts = d.get("timestamp", "")
            date_str = ts[:10] if ts else datetime.now(timezone.utc).strftime("%Y-%m-%d")
            append_cost(date_str, model, cost, str(jsonl_file))
            seen.add(uid)
            new_seen.append(uid)
            total_cost += cost
            entries += 1

    state["seen_uuids"] = list(seen)[-50_000:]  # cap to avoid unbounded growth
    state["last_run"] = datetime.now(timezone.utc).isoformat()
    state["total_tracked_usd"] = round(state.get("total_tracked_usd", 0.0) + total_cost, 6)
    save_state(state)

    if entries:
        print(f"cc-tracker: {entries} new entries, ${total_cost:.4f} logged")
    else:
        print("cc-tracker: nothing new")

    # Write CC-only spend summary (never touches stack auto-budget guard)
    try:
        import json as _json
        cc_state_path = BASE / "data" / "runtime" / "cc_spend_state.json"
        cc_cur = {}
        try:
            cc_cur = _json.loads(cc_state_path.read_text())
        except Exception:
            pass
        cc_cur["last_run"] = state["last_run"]
        cc_cur["total_tracked_usd"] = state["total_tracked_usd"]
        cc_cur["entries"] = len(state["seen_uuids"])
        cc_state_path.write_text(_json.dumps(cc_cur, indent=2))
    except Exception as e:
        print(f"cc state update skipped: {e}")


if __name__ == "__main__":
    main()
