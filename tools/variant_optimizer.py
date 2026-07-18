#!/usr/bin/env python3
"""Silent self-optimizer for the outreach A/B/C variants — a smoothed epsilon-greedy bandit.

Daily: read per-variant sends + captures, reweight data/hustle/variant_weights.json so NEW prospects
increasingly get the winning hook while a slice keeps exploring the others. Pure exploration until
there's enough data to trust. No human in the loop — the copy tunes itself.
"""
import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.variant_report import compute_tally, VARIANTS

WEIGHTS = ROOT / "data" / "hustle" / "variant_weights.json"
LOG = ROOT / "data" / "hustle" / "variant_optimizer.jsonl"
PRICING_CHANGES = ROOT / "data" / "hustle" / "pricing_changes.jsonl"
MIN_TOTAL = 60      # explore-only (equal weights) until this many sends total
EPSILON = 0.25      # always keep this fraction on exploration
PRICE_BIAS_DAYS = 14    # how recent a competitor pricing move stays "hot"
PRICE_BIAS_MAX = 0.15   # bounded absolute boost to the cost/ROI hook (can't break the bandit)


def pricing_bias(weights: dict) -> tuple[dict, dict | None]:
    """A competitor PRICE HIKE in the last PRICE_BIAS_DAYS is exactly when the cost/value
    angle wins, so bounded-boost the 'roi' hook. Reads the diff monitor's log
    (agents/pricing_watch.py). Bounded + renormalized so it can't destabilize the bandit;
    no recent hike -> weights pass through untouched."""
    if "roi" not in weights or not PRICING_CHANGES.exists():
        return weights, None
    cutoff = datetime.now(timezone.utc) - timedelta(days=PRICE_BIAS_DAYS)
    hikes = []
    for line in PRICING_CHANGES.read_text(errors="ignore").splitlines():
        try:
            ev = json.loads(line)
        except Exception:
            continue
        if ev.get("direction") != "hike":
            continue
        try:
            if datetime.fromisoformat(ev["ts"]) >= cutoff:
                hikes.append(ev.get("competitor", "?"))
        except Exception:
            continue
    if not hikes:
        return weights, None
    boost = min(PRICE_BIAS_MAX, 0.04 * len(hikes))
    w = dict(weights)
    w["roi"] = w.get("roi", 0.0) + boost
    others = [v for v in w if v != "roi"]
    take = boost / len(others) if others else 0.0
    for v in others:
        w[v] = max(0.05, w[v] - take)
    s = sum(w.values()) or 1.0
    w = {v: round(w[v] / s, 4) for v in w}
    return w, {"recent_hikes": len(hikes), "competitors": sorted(set(hikes)),
               "roi_boost": round(boost, 4)}


def main() -> None:
    t = compute_tally()
    total = sum(t[v]["sent"] for v in VARIANTS)
    if total < MIN_TOTAL:
        weights = {v: round(1.0 / len(VARIANTS), 4) for v in VARIANTS}
        best, reason = None, f"explore-only (total sends {total} < {MIN_TOTAL})"
    else:
        # smoothed capture rate (Laplace) so a variant that's 0-so-far isn't killed prematurely
        score = {v: (t[v]["capture"] + 1) / (t[v]["sent"] + 2) for v in VARIANTS}
        ss = sum(score.values()) or 1.0
        raw = {v: EPSILON / len(VARIANTS) + (1 - EPSILON) * score[v] / ss for v in VARIANTS}
        sw = sum(raw.values()) or 1.0
        weights = {v: round(raw[v] / sw, 4) for v in VARIANTS}
        best = max(VARIANTS, key=lambda v: score[v])
        reason = f"exploit: best={best} (smoothed cap-rate {score[best]:.3f})"
    weights, price_signal = pricing_bias(weights)
    if price_signal:
        reason += (f" | +roi {price_signal['roi_boost']} from "
                   f"{price_signal['recent_hikes']} competitor price-hike(s)")
    WEIGHTS.write_text(json.dumps(weights, indent=2) + "\n")
    rec = {"ts": datetime.now(timezone.utc).isoformat(), "total_sends": total,
           "weights": weights, "best": best, "reason": reason,
           "pricing_signal": price_signal,
           "tally": {v: dict(t[v]) for v in VARIANTS}}
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(json.dumps(rec) + "\n")
    print(json.dumps(rec, indent=2))


if __name__ == "__main__":
    main()
