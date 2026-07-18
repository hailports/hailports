#!/usr/bin/env python3
"""Close the demon's outcome loop — this is what makes it "progressively smarter".

Reads the append-only cycle log (revenue_demon_cycles.jsonl), scores each tactic by
the REAL signal that moved AFTER it ran (real dollars dominate, then email captures,
then human pageviews — bot deltas are zero-weighted because the signal is already
human-filtered upstream), recomputes per-tactic weights, and distills a short curated
lessons file that gets injected back into the next ChatGPT think prompt.

House pattern mirrors agents/case_study_metrics.recompute_weights:
  weight = clamp(avg_score(tactic) / overall_avg, 0.3, 2.5)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

HUSTLE = ROOT / "data" / "hustle"
CYCLES = HUSTLE / "revenue_demon_cycles.jsonl"
TACTICS = HUSTLE / "revenue_demon_tactics.json"
LESSONS = HUSTLE / "revenue_demon_lessons.md"

# recurring dollars (MRR) dominate the score; then one-time dollars, captures, pageviews
W_MRR, W_REVENUE, W_CAPTURE, W_PAGEVIEW = 5000.0, 1000.0, 5.0, 1.0
CLAMP_LO, CLAMP_HI = 0.3, 2.5
LESSONS_MAX_CHARS = 4000


def _rows() -> list[dict]:
    if not CYCLES.exists():
        return []
    out = []
    for line in CYCLES.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    return out


def _sig(row: dict) -> dict:
    return row.get("signal_before") or {}


def _score_cycle(cur: dict, nxt: dict) -> float:
    """Outcome of cur's action = the change in real signal by the next observation."""
    a, b = _sig(cur), _sig(nxt)
    d_mrr = float(b.get("mrr_usd", 0) or 0) - float(a.get("mrr_usd", 0) or 0)
    d_rev = float(b.get("real_usd_all_time", 0) or 0) - float(a.get("real_usd_all_time", 0) or 0)
    d_cap = float(b.get("captures_24h", 0) or 0) - float(a.get("captures_24h", 0) or 0)
    d_pv = float(b.get("human_pageviews_24h", 0) or 0) - float(a.get("human_pageviews_24h", 0) or 0)
    return W_MRR * d_mrr + W_REVENUE * d_rev + W_CAPTURE * d_cap + W_PAGEVIEW * d_pv


def _sig_key(r: dict) -> tuple:
    s = _sig(r)
    return (s.get("mrr_usd"), s.get("active_subscriptions"),
            s.get("real_usd_all_time"), s.get("captures_24h"), s.get("human_pageviews_24h"))


def recompute() -> dict:
    rows = _rows()
    scored: dict[str, list[float]] = {}
    notable: list[tuple[float, str, str]] = []  # (abs_score, tactic, ts)
    # Score only on an ACTUAL signal change. The cached scoreboard refreshes ~2x/day while
    # we cycle every ~15 min, so ~94/96 consecutive pairs are identical — diffing every pair
    # would dump a whole jump onto one arbitrary tactic (even 'observe'). Instead, credit each
    # distinct change across the levers that ACTED in that window, and never credit the no-op.
    pending: list[tuple[str, str]] = []
    last = rows[0] if rows else None
    for i in range(1, len(rows)):
        prev_t = str(rows[i - 1].get("tactic") or "").strip()
        if prev_t:
            pending.append((prev_t, str(rows[i - 1].get("ts", ""))[:16]))
        if _sig_key(rows[i]) == _sig_key(last):
            continue
        delta = _score_cycle(last, rows[i])
        actors = [(t, ts) for (t, ts) in pending if t != "observe"]
        share = delta / len(actors) if actors else 0.0
        for t, ts in actors:
            scored.setdefault(t, []).append(share)
            notable.append((abs(share), t, ts))
        for t, _ts in pending:
            if t == "observe":
                scored.setdefault(t, []).append(0.0)  # observe can never cause revenue
        last = rows[i]
        pending = []

    avg = {t: (sum(v) / len(v)) for t, v in scored.items() if v}
    overall = (sum(avg.values()) / len(avg)) if avg else 0.0
    lo = min(avg.values()) if avg else 0.0
    eps = 1e-9
    weights: dict[str, float] = {}
    for t, a in avg.items():
        # sign-stable shifted ratio: preserves ranking even when overall<=0 (the $0 regime),
        # so worse levers still get deprioritized instead of collapsing to a flat all-1.0 table.
        w = (a - lo + eps) / (overall - lo + eps)
        weights[t] = round(max(CLAMP_LO, min(CLAMP_HI, w)), 3)

    TACTICS.parent.mkdir(parents=True, exist_ok=True)
    TACTICS.write_text(json.dumps(weights, indent=2, sort_keys=True))

    _write_lessons(rows, weights, avg, notable)
    return {"tactics_scored": len(weights), "cycles": len(rows), "overall_avg": round(overall, 3)}


def _write_lessons(rows, weights, avg, notable) -> None:
    latest = _sig(rows[-1]) if rows else {}
    top = sorted(weights.items(), key=lambda kv: kv[1], reverse=True)[:4]
    bottom = sorted(weights.items(), key=lambda kv: kv[1])[:4]
    notable.sort(reverse=True)
    lines = ["# revenue demon — lessons (auto-distilled; newest signal first)", ""]
    if latest:
        lines.append(
            f"current signal: ${latest.get('mrr_usd', 0)}/mo MRR "
            f"({latest.get('active_subscriptions', 0)} active subs), "
            f"${latest.get('real_usd_all_time', 0)} one-time all-time, "
            f"{latest.get('captures_24h', 0)} captures/24h, "
            f"{latest.get('human_pageviews_24h', 0)} human pageviews/24h "
            f"over {len(rows)} cycles.")
        lines.append("")
    if top:
        lines.append("what is working (higher weight = moved real signal):")
        lines += [f"- {t}  (w={w})" for t, w in top if w > 1.0] or ["- (nothing has out-performed yet)"]
        lines.append("")
    if bottom:
        lines.append("what is NOT working (deprioritize, don't abandon):")
        lines += [f"- {t}  (w={w})" for t, w in bottom if w < 1.0] or ["- (no clear losers yet)"]
        lines.append("")
    if any(n[0] > 0 for n in notable):
        lines.append("biggest moves observed:")
        for sc, t, ts in notable[:5]:
            if sc > 0:
                lines.append(f"- {ts}: {t} -> |Δsignal|≈{sc:.0f}")
        lines.append("")
    if not rows or all((_sig(r).get("mrr_usd", 0) or 0) == 0 for r in rows):
        lines.append("hard truth: still $0 MRR. prioritize tactics that put qualified humans in "
                     "front of a RECURRING offer ($24/mo site-health-watch, $29/mo "
                     "ai-visibility-watch), not one-time volume or vanity reach.")
    text = "\n".join(lines)[:LESSONS_MAX_CHARS]
    LESSONS.parent.mkdir(parents=True, exist_ok=True)
    LESSONS.write_text(text + "\n")


def read_lessons() -> str:
    try:
        return LESSONS.read_text()
    except Exception:
        return ""


def read_weights() -> dict:
    try:
        return json.loads(TACTICS.read_text())
    except Exception:
        return {}


if __name__ == "__main__":
    print(json.dumps(recompute(), indent=2))
