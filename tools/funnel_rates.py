#!/usr/bin/env python3
"""Funnel-rate summary — reads the unified ledger data/funnel_events.jsonl (the stages
app.py records PAST "send") and computes per-brand + per-campaign conversion rates the
revenue brain + dashboard read. Pure-local, $0, fail-soft.

Stage order: visit -> scan_start -> scan_complete -> lead -> checkout_started -> paid.

Writes data/hustle/funnel_rates.json and prints a tight board.
CLI: python3 tools/funnel_rates.py [days]   (default 30)
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LEDGER = ROOT / "data" / "funnel_events.jsonl"
OUT = ROOT / "data" / "hustle" / "funnel_rates.json"
STAGE_ORDER = ["visit", "scan_start", "scan_complete", "lead", "checkout_started", "paid"]


def _parse_ts(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:
        return None


def _rates(counts: dict) -> dict:
    c = {s: int(counts.get(s, 0)) for s in STAGE_ORDER}

    def pct(a, b):
        return round(100.0 * a / b, 1) if b else None

    return {
        "counts": c,
        "visit_to_lead": pct(c["lead"], c["visit"]),
        "lead_to_checkout": pct(c["checkout_started"], c["lead"]),
        "checkout_to_paid": pct(c["paid"], c["checkout_started"]),
        "visit_to_paid": pct(c["paid"], c["visit"]),
    }


def compute(days: int = 30) -> dict:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    overall: dict = defaultdict(int)
    by_brand: dict = defaultdict(lambda: defaultdict(int))
    by_campaign: dict = defaultdict(lambda: defaultdict(int))
    rows = 0
    if LEDGER.exists():
        for line in LEDGER.read_text(errors="ignore").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except Exception:
                continue
            ts = _parse_ts(e.get("ts"))
            if ts is not None and ts < cutoff:
                continue
            stage = e.get("stage", "")
            if stage not in STAGE_ORDER:
                continue
            rows += 1
            overall[stage] += 1
            by_brand[e.get("brand") or "unknown"][stage] += 1
            camp = e.get("campaign")
            if camp:
                by_campaign[camp][stage] += 1

    top_campaigns = sorted(by_campaign.items(), key=lambda kv: -sum(kv[1].values()))[:20]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window_days": days,
        "ledger_rows": rows,
        "overall": _rates(overall),
        "by_brand": {b: _rates(v) for b, v in by_brand.items()},
        "by_campaign": {b: _rates(v) for b, v in top_campaigns},
    }


def main():
    days = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 30
    payload = compute(days)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=2))
    o = payload["overall"]
    print(f"funnel_rates: rows={payload['ledger_rows']} window={days}d -> {OUT}")
    print("  " + "  ".join(f"{s}={o['counts'][s]}" for s in STAGE_ORDER))
    print(f"  visit→lead={o['visit_to_lead']}%  lead→checkout={o['lead_to_checkout']}%  "
          f"checkout→paid={o['checkout_to_paid']}%  visit→paid={o['visit_to_paid']}%")


if __name__ == "__main__":
    main()
