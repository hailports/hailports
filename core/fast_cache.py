#!/usr/bin/env python3
"""Fast answer cache — instant responses to frequently-asked questions.

Topics are pre-built (by the warmer) and served from disk in microseconds.
Each topic has a builder that digests already-aggregated sources (the CompanyA
precompute, revenue_state, intent leads, etc.) into a compact answer + data.

Design:
- ask(topic): return cached answer if fresh; else build live, cache, return.
  Every ask() records the topic for the pattern-learner (anticipation).
- build(topic)/warm: (re)compute and persist.
- Builders are defensive: a missing/stale source degrades, never throws.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT / "data/runtime/fastcache"
PRECOMPUTE = Path.home() / ".openclaw/workspace/CompanyA-local/daily-precompute-latest.json"
REVENUE_STATE = ROOT / "data/revenue_state.json"
INTENT_LEADS = ROOT / "data/hustle/intent_leads.json"
LOCAL_TZ = ZoneInfo("America/Chicago")


def _now() -> float:
    return time.time()


def _read_json(p: Path, default=None):
    try:
        return json.loads(Path(p).read_text())
    except Exception:
        return default


def _age_str(secs: float) -> str:
    secs = int(secs)
    if secs < 90:
        return f"{secs}s"
    if secs < 5400:
        return f"{secs // 60}m"
    return f"{secs // 3600}h"


# ── Builders: each returns {"answer": <str>, "data": <dict>} ──────────────────

def _build_work_context() -> dict:
    pc = _read_json(PRECOMPUTE, {}) or {}
    summ = pc.get("summary", {}) if isinstance(pc.get("summary"), dict) else {}
    pressing = summ.get("pressing_items", {}) if isinstance(summ.get("pressing_items"), dict) else {}
    lines = []
    emails = pressing.get("email_actions") or []
    if emails:
        head = str(emails[0])
        n = head.count("[new]") + head.count("[read]")
        lines.append(f"📥 Inbox: ~{n or len(emails)} items need attention")
    sprint = pressing.get("sprint_items") or []
    if sprint:
        lines.append(f"🎯 {len(sprint)} active sprint items")
        lines += [f"   • {str(s)[:90]}" for s in sprint[:5]]
    lines.append(f"📊 systems ok: {len(summ.get('ok_tools', []))}, failed: {len(summ.get('failed_tools', []))}")
    gen = pc.get("generated_at", "?")
    return {
        "answer": "WORK CONTEXT (" + str(pc.get("date", "")) + ")\n" + "\n".join(lines)
                  + f"\n(source precompute @ {gen})",
        "data": {"generated_at": gen, "ok_tools": summ.get("ok_tools", []),
                 "failed_tools": summ.get("failed_tools", []),
                 "sprint_count": len(sprint), "email_action_blocks": len(emails)},
    }


def _build_revenue() -> dict:
    rv = _read_json(REVENUE_STATE, {}) or {}
    leads = _read_json(INTENT_LEADS, []) or []
    hot_leads = [l for l in leads if isinstance(l, dict) and (l.get("intent_score") or 0) >= 7]
    a = [
        "💰 REVENUE SNAPSHOT",
        f"   total: ${rv.get('total_revenue_usd', 0):,.2f} | this month: ${rv.get('this_month_revenue_usd', 0):,.2f} | 30d: ${rv.get('last_30d_revenue_usd', 0):,.2f}",
        f"   sales: {rv.get('total_sales_count', 0)} total, {rv.get('this_month_sales_count', 0)} this month | last sale: {rv.get('last_sale_at') or 'never'}",
        f"   intent leads: {len(leads)} ({len(hot_leads)} hot ≥7)",
    ]
    return {
        "answer": "\n".join(a),
        "data": {"total_usd": rv.get("total_revenue_usd", 0),
                 "month_usd": rv.get("this_month_revenue_usd", 0),
                 "last_30d_usd": rv.get("last_30d_revenue_usd", 0),
                 "sales": rv.get("total_sales_count", 0),
                 "last_sale_at": rv.get("last_sale_at"),
                 "leads_total": len(leads), "leads_hot": len(hot_leads),
                 "updated_at": rv.get("updated_at")},
    }


def _build_inbox() -> dict:
    pc = _read_json(PRECOMPUTE, {}) or {}
    summ = pc.get("summary", {}) if isinstance(pc.get("summary"), dict) else {}
    pressing = summ.get("pressing_items", {}) if isinstance(summ.get("pressing_items"), dict) else {}
    emails = pressing.get("email_actions") or []
    blob = "\n".join(str(e) for e in emails)[:1500]
    return {"answer": "📥 INBOX ATTENTION " + _precompute_freshness() + "\n" + (blob or "no attention items cached"),
            "data": {"blocks": len(emails), "generated_at": pc.get("generated_at")}}


def _tool_result(key: str, limit: int = 600) -> str:
    """Pull one tool's result string out of the precompute systems map."""
    pc = _read_json(PRECOMPUTE, {}) or {}
    sysd = pc.get("systems", {}) if isinstance(pc.get("systems"), dict) else {}
    entry = sysd.get(key) or {}
    res = entry.get("result") if isinstance(entry, dict) else None
    return str(res)[:limit] if res else ""


def _precompute_age_secs() -> float:
    try:
        return _now() - PRECOMPUTE.stat().st_mtime
    except Exception:
        return 1e9


def _precompute_freshness() -> str:
    return f"(work data {_age_str(_precompute_age_secs())} old)"


def _build_meetings() -> dict:
    parts = []
    for label, key in (("📅 Today's agenda", "outlook_today_agenda"),
                       ("🎥 Zoom today", "zoom_today_meetings"),
                       ("⏭️  Upcoming", "outlook_upcoming_events")):
        r = _tool_result(key, 500)
        if r:
            parts.append(f"{label}: {r}")
    return {"answer": "MEETINGS " + _precompute_freshness() + "\n" + ("\n".join(parts) or "none cached"),
            "data": {"generated_at": (_read_json(PRECOMPUTE, {}) or {}).get("generated_at")}}


def _build_action_items() -> dict:
    parts = []
    for label, key in (("📝 LittleBird", "littlebird_action_items"),
                       ("🎯 SF/sprint", "monday_get_salesforce_sprint_items")):
        r = _tool_result(key, 700)
        if r:
            parts.append(f"{label}: {r}")
    return {"answer": "ACTION ITEMS " + _precompute_freshness() + "\n" + ("\n".join(parts) or "none cached"),
            "data": {"generated_at": (_read_json(PRECOMPUTE, {}) or {}).get("generated_at")}}


TOPICS = {
    "work_context": {"ttl": 1800, "builder": _build_work_context,
                     "desc": "cross-system work status (inbox/sprint/meetings)"},
    "revenue": {"ttl": 3600, "builder": _build_revenue,
                "desc": "revenue + leads snapshot"},
    "inbox": {"ttl": 900, "builder": _build_inbox, "desc": "Outlook attention items"},
    "meetings": {"ttl": 1800, "builder": _build_meetings,
                 "desc": "today's agenda + Zoom + upcoming events"},
    "action_items": {"ttl": 1800, "builder": _build_action_items,
                     "desc": "LittleBird + SF/sprint action items"},
}

# Throttle live precompute triggers so we never hammer the live work systems
# (Salesforce/Zoom/Outlook) — at most once per this window.
_PRECOMPUTE_JOB = "com.openclaw.CompanyA-daily-precompute"
_REFRESH_STAMP = CACHE_DIR / "_last_precompute_kick"
_REFRESH_MIN_INTERVAL = 1800  # 30 min


def refresh(force: bool = False) -> dict:
    """Trigger a live cross-system precompute (via the existing launchd job, which
    has the right auth env), throttled. Returns status. Best-effort."""
    import subprocess
    last = 0.0
    try:
        last = float(_REFRESH_STAMP.read_text().strip())
    except Exception:
        pass
    age = _now() - last
    if not force and age < _REFRESH_MIN_INTERVAL:
        return {"triggered": False, "reason": f"throttled ({_age_str(age)} since last)"}
    try:
        uid = __import__("os").getuid()
        subprocess.run(["launchctl", "kickstart", "-k", f"gui/{uid}/{_PRECOMPUTE_JOB}"],
                       capture_output=True, timeout=10)
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _REFRESH_STAMP.write_text(str(_now()))
        return {"triggered": True, "job": _PRECOMPUTE_JOB}
    except Exception as e:
        return {"triggered": False, "reason": str(e)}


def _cache_path(topic: str) -> Path:
    return CACHE_DIR / f"{topic}.json"


def build(topic: str) -> dict:
    spec = TOPICS[topic]
    t0 = time.perf_counter()
    out = spec["builder"]()
    rec = {"topic": topic, "generated_at": _now(),
           "generated_iso": datetime.now(LOCAL_TZ).isoformat(),
           "ttl": spec["ttl"], "built_ms": round((time.perf_counter() - t0) * 1000, 1),
           "answer": out["answer"], "data": out.get("data", {})}
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _cache_path(topic).write_text(json.dumps(rec, indent=2))
    return rec


def get_cached(topic: str):
    return _read_json(_cache_path(topic))


def ask(topic: str, record: bool = True) -> dict:
    if topic not in TOPICS:
        return {"error": f"unknown topic '{topic}'. known: {', '.join(TOPICS)}"}
    if record:
        try:
            from core import question_patterns
            question_patterns.record(topic)
        except Exception:
            pass
    cached = get_cached(topic)
    if cached and (_now() - cached.get("generated_at", 0)) < cached.get("ttl", 0):
        cached["served"] = "cache"
        cached["age"] = _age_str(_now() - cached["generated_at"])
        return cached
    rec = build(topic)
    rec["served"] = "rebuilt"
    rec["age"] = "0s"
    return rec
