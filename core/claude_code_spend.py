#!/usr/bin/env python3
"""
Claude Code Spend Tracker
Parses ~/.claude/projects/**/*.jsonl → computes real $ cost per session/day/model
Writes to data/runtime/claude_code_spend.json, exposed via myos-admin /spend endpoint
"""
import json, os, glob, logging
from pathlib import Path
from datetime import datetime, date, timezone
from collections import defaultdict

log = logging.getLogger(__name__)

PROJECTS_DIR = Path.home() / ".claude" / "projects"
OUT_FILE = Path(__file__).parent.parent / "data" / "runtime" / "claude_code_spend.json"

# Per-million-token rates (USD)
MODEL_RATES = {
    # Haiku 4.5
    "claude-haiku-4-5": {"input": 0.80, "output": 4.00, "cache_write": 1.00, "cache_read": 0.08},
    "claude-haiku-4-5-20251001": {"input": 0.80, "output": 4.00, "cache_write": 1.00, "cache_read": 0.08},
    # Sonnet 4.5 / 4.6
    "claude-sonnet-4-5": {"input": 3.00, "output": 15.00, "cache_write": 3.75, "cache_read": 0.30},
    "claude-sonnet-4-20250514": {"input": 3.00, "output": 15.00, "cache_write": 3.75, "cache_read": 0.30},
    "claude-sonnet-4-6": {"input": 3.00, "output": 15.00, "cache_write": 3.75, "cache_read": 0.30},
    # Opus 4.5 / 4.6
    "claude-opus-4-5": {"input": 15.00, "output": 75.00, "cache_write": 18.75, "cache_read": 1.50},
    "claude-opus-4-6": {"input": 15.00, "output": 75.00, "cache_write": 18.75, "cache_read": 1.50},
    "claude-opus-4-20250514": {"input": 15.00, "output": 75.00, "cache_write": 18.75, "cache_read": 1.50},
}
DEFAULT_RATE = {"input": 3.00, "output": 15.00, "cache_write": 3.75, "cache_read": 0.30}


def _rate(model: str) -> dict:
    if not model:
        return DEFAULT_RATE
    for key, r in MODEL_RATES.items():
        if model.startswith(key) or key in model:
            return r
    return DEFAULT_RATE


def _cost(usage: dict, model: str) -> float:
    r = _rate(model)
    m = 1_000_000
    inp    = usage.get("input_tokens", 0)
    out    = usage.get("output_tokens", 0)
    cw     = usage.get("cache_creation_input_tokens", 0)
    cr     = usage.get("cache_read_input_tokens", 0)
    return (inp * r["input"] + out * r["output"] +
            cw  * r["cache_write"] + cr * r["cache_read"]) / m


def scan() -> dict:
    by_day   = defaultdict(float)
    by_model = defaultdict(float)
    by_proj  = defaultdict(float)
    sessions = []
    total    = 0.0

    jsonl_files = list(PROJECTS_DIR.rglob("*.jsonl"))
    log.info("Scanning %d JSONL files", len(jsonl_files))

    for fpath in jsonl_files:
        proj = fpath.parent.name
        sess_cost = 0.0
        sess_model = "unknown"
        sess_ts = None
        try:
            for line in fpath.read_text(errors="replace").splitlines():
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                # Extract model from message if present
                msg = rec.get("message", {})
                model = (msg.get("model") or rec.get("model") or "").strip()
                usage = msg.get("usage") or rec.get("usage")
                ts = rec.get("timestamp") or msg.get("created_at")
                if usage and isinstance(usage, dict):
                    c = _cost(usage, model)
                    sess_cost += c
                    total += c
                    if model:
                        by_model[model] += c
                        sess_model = model
                    if ts:
                        try:
                            day = ts[:10]
                            by_day[day] += c
                            if not sess_ts:
                                sess_ts = ts
                        except Exception:
                            pass
        except Exception as e:
            log.debug("Error reading %s: %s", fpath, e)
            continue

        if sess_cost > 0:
            by_proj[proj] += sess_cost
            sessions.append({
                "file": fpath.name,
                "project": proj,
                "model": sess_model,
                "cost_usd": round(sess_cost, 6),
                "ts": sess_ts or "",
            })

    # Sort days
    sorted_days = sorted(by_day.items(), reverse=True)
    today_str = date.today().isoformat()
    today_cost = by_day.get(today_str, 0.0)
    mtd_cost = sum(v for k, v in by_day.items() if k[:7] == today_str[:7])

    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_usd": round(total, 4),
        "today_usd": round(today_cost, 4),
        "mtd_usd": round(mtd_cost, 4),
        "by_day": {k: round(v, 4) for k, v in sorted_days[:30]},
        "by_model": {k: round(v, 4) for k, v in sorted(by_model.items(), key=lambda x: -x[1])},
        "by_project": {k: round(v, 4) for k, v in sorted(by_proj.items(), key=lambda x: -x[1])[:20]},
        "sessions": sorted(sessions, key=lambda x: -x["cost_usd"])[:50],
    }

    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(json.dumps(result, indent=2))
    log.info("Spend scan complete: total=$%.4f today=$%.4f mtd=$%.4f",
             total, today_cost, mtd_cost)
    return result


def get_cached() -> dict:
    try:
        return json.loads(OUT_FILE.read_text())
    except Exception:
        return scan()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    r = scan()
    print(f"Total: ${r['total_usd']:.4f}  Today: ${r['today_usd']:.4f}  MTD: ${r['mtd_usd']:.4f}")
