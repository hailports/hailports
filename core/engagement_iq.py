#!/usr/bin/env python3
"""engagement_iq.py — the revenue/engagement LEARN loop (the money-pointed twin of
core.health_ledger). health_ledger learns from failures so the stack stays UP;
this learns from the engine's own output so the stack gets BETTER AT EARNING.

The engine logs every comment it fires (engage-results.jsonl: platform, account,
the creator it targeted, the text, whether it verified landing) — 600+ rows of
signal that nothing has ever read back. This module turns that into:
  • a live scoreboard (landing rate, reach, freshness, content mix per platform)
  • failure clustering (where cycles are wasted)
  • promote/demote recommendations → data/runtime/engagement_iq.json
    so a weighted pickComment can lean into what's working.

When like/reply/profile-click harvest lands (record_response below), the same
scoreboard re-weights by REAL engagement instead of just landing — closing the
loop from "we posted" to "this actually pulled people in."

Dependency-free, $0, deterministic. Run: python3 -m core.engagement_iq
"""
from __future__ import annotations
import json
import time
from collections import Counter, defaultdict
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
RUNTIME_DIR = BASE_DIR / "data" / "runtime"
RESULTS = Path.home() / ".openclaw" / "workspace" / "android-logs" / "engage-results.jsonl"
RESPONSES = Path.home() / ".openclaw" / "workspace" / "android-logs" / "engage-responses.jsonl"
OUT = RUNTIME_DIR / "engagement_iq.json"
RUNTIME_DIR.mkdir(parents=True, exist_ok=True)

# Content-mix themes — keyword tagger over the comment text itself (robust to the
# fact that comments are LLM-generated, so they never exactly match a pool line).
THEMES = {
    "comp_plan_truth": ["upline", "comp plan", "downline", "recruit", "$200b", "team call", "onboarding"],
    "follow_up_system": ["follow-up", "follow up", "between parties", "dm", "ghost", "customer list", "back-end", "back end"],
    "automation_ai": ["automat", "ai ", " ai", "system", "tech", "by hand", "scale"],
    "inventory_cashflow": ["inventory", "garage", "sample kit", "cash", "sitting", "warehouse"],
    "party_plan": ["party", "parties", "host", "booking", "bomb party", "live"],
    "top_earner": ["top earner", "top 1%", "rank", "winning", "luck", "trip"],
}


def _rows(path: Path):
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def _theme_of(text: str) -> str:
    t = (text or "").lower()
    best, best_n = "other", 0
    for theme, kws in THEMES.items():
        n = sum(1 for k in kws if k in t)
        if n > best_n:
            best, best_n = theme, n
    return best


def record_response(parent_tid: str, platform: str, likes: int = 0, replies: int = 0,
                    profile_clicks: int = 0) -> None:
    """The engine (or a harvest pass) calls this when it re-reads one of OUR comments
    and sees how it performed. This is the true conversion signal."""
    with open(RESPONSES, "a") as f:
        f.write(json.dumps({"ts": time.time(), "parent_tid": parent_tid, "platform": platform,
                            "likes": likes, "replies": replies, "profile_clicks": profile_clicks}) + "\n")


def _is_true(v) -> bool:
    return str(v).lower() == "true"


def analyze() -> dict:
    rows = _rows(RESULTS)
    responses = _rows(RESPONSES)
    resp_by_tid = {}
    for r in responses:
        resp_by_tid[r.get("parent_tid")] = r

    total = len(rows)
    by_platform = defaultdict(lambda: {"n": 0, "landed": 0, "failed": 0})
    by_theme = defaultdict(lambda: {"n": 0, "landed": 0, "likes": 0, "replies": 0})
    creators = Counter()
    fail_reasons = Counter()

    for r in rows:
        plat = r.get("platform", "?")
        landed = _is_true(r.get("verified"))
        failed = str(r.get("verified")).lower() == "false"
        bp = by_platform[plat]
        bp["n"] += 1
        bp["landed"] += 1 if landed else 0
        bp["failed"] += 1 if failed else 0
        if failed:
            fail_reasons[r.get("verify_why", "unknown")] += 1
        theme = _theme_of(r.get("comment", ""))
        bt = by_theme[theme]
        bt["n"] += 1
        bt["landed"] += 1 if landed else 0
        resp = resp_by_tid.get(r.get("parent_tid"))
        if resp:
            bt["likes"] += int(resp.get("likes", 0) or 0)
            bt["replies"] += int(resp.get("replies", 0) or 0)
        if r.get("parent_url"):
            creators[r["parent_url"]] += 1

    def rate(d):
        return round(100 * d["landed"] / d["n"], 1) if d["n"] else 0.0

    plat_score = {p: {**v, "land_rate_pct": rate(v)} for p, v in by_platform.items()}
    # Theme score: landing rate, and engagement-per-post when responses exist.
    theme_score = {}
    have_responses = len(responses) > 0
    for th, v in by_theme.items():
        eng = (v["likes"] + 2 * v["replies"]) / v["n"] if v["n"] else 0
        theme_score[th] = {**v, "land_rate_pct": rate(v), "eng_per_post": round(eng, 2)}

    unique = len(creators)
    repeats = sum(c - 1 for c in creators.values() if c > 1)

    # Recommendations — promote the best, flag waste.
    # HARD RULE: we sell to DESPERATE / up-and-coming sellers, never to established
    # top-earners (they don't buy). "weight_up" = content that CONVERTS strugglers,
    # NOT content that merely gets likes. When response data is wired it MUST be
    # ICP-gated (count signal only from struggling-seller responders / real clicks),
    # so the engine can never drift toward winner-pleasing vanity content. This
    # weighting steers the MESSAGE only — never who we target (that's search_queries).
    ranked_themes = sorted(theme_score.items(),
                           key=lambda kv: (kv[1]["eng_per_post"], kv[1]["land_rate_pct"], kv[1]["n"]),
                           reverse=True)
    promote = [t for t, _ in ranked_themes[:3] if t != "other"]
    demote = [t for t, v in theme_score.items() if v["land_rate_pct"] < 50 and v["n"] >= 10]
    over_hit = [{"creator": c, "hits": n} for c, n in creators.most_common(5) if n >= 3]

    recs = {
        "weight_up_themes": promote,
        "weight_down_themes": demote,
        "over_hit_creators": over_hit,
        "needs_response_harvest": not have_responses,
        "note": ("Scoring on landing-rate only — wire record_response() (like/reply harvest) "
                 "for true engagement-weighted optimization." if not have_responses
                 else "Engagement-weighted via harvested responses."),
    }

    return {
        "generated_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "total_comments": total,
        "overall_land_rate_pct": round(100 * sum(1 for r in rows if _is_true(r.get("verified"))) / total, 1) if total else 0,
        "unique_creators": unique,
        "repeat_hits": repeats,
        "freshness_pct": round(100 * unique / total, 1) if total else 0,
        "by_platform": plat_score,
        "by_theme": theme_score,
        "top_fail_reasons": dict(fail_reasons.most_common(5)),
        "recommendations": recs,
    }


def write_report() -> dict:
    rep = analyze()
    OUT.write_text(json.dumps(rep, indent=2, default=str))
    return rep


if __name__ == "__main__":
    r = write_report()
    print(f"\n📊 ENGAGEMENT IQ — {r['total_comments']} comments analyzed ({r['generated_iso']})")
    print(f"   landing {r['overall_land_rate_pct']}% · {r['unique_creators']} unique creators · "
          f"{r['freshness_pct']}% fresh · {r['repeat_hits']} repeat hits")
    print("\n   PLATFORM           vol   land%")
    for p, v in sorted(r["by_platform"].items(), key=lambda kv: -kv[1]["n"]):
        print(f"   {p:18}{v['n']:4}   {v['land_rate_pct']}%")
    print("\n   CONTENT THEME (what's being deployed)   vol   land%   eng/post")
    for th, v in sorted(r["by_theme"].items(), key=lambda kv: -kv[1]["n"]):
        print(f"   {th:36}{v['n']:4}   {v['land_rate_pct']:5}%   {v['eng_per_post']}")
    rc = r["recommendations"]
    print("\n   ▲ WEIGHT UP :", ", ".join(rc["weight_up_themes"]) or "—")
    print("   ▼ WEIGHT DOWN:", ", ".join(rc["weight_down_themes"]) or "—")
    if rc["over_hit_creators"]:
        print("   ⚠ over-hit  :", ", ".join(f"{c['creator'].split('/')[-1] or c['creator']}(x{c['hits']})" for c in rc["over_hit_creators"]))
    print(f"\n   {rc['note']}")
    print(f"\n   → {OUT}")
