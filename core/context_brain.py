from __future__ import annotations

"""Unified context brain: assembles WorkOS/LifeOS/Revenue/Infra context for any channel."""

import json
import logging
import re
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
HUSTLE_DIR = DATA_DIR / "hustle"
CONFIG_DIR = BASE_DIR / "config"
SELF_IMPROVEMENT_MEMORY = DATA_DIR / "context_brain_self_improvement.json"

CT = ZoneInfo("America/Chicago")

# ---------------------------------------------------------------------------
# Identity resolution
# ---------------------------------------------------------------------------

_USERS: dict | None = None


def _load_users() -> dict:
    global _USERS
    if _USERS is not None:
        return _USERS
    try:
        try:
            import tomllib
        except ModuleNotFoundError:
            import tomli as tomllib
        with open(CONFIG_DIR / "users.toml", "rb") as f:
            _USERS = tomllib.load(f)
    except Exception:
        _USERS = {}
    return _USERS


def resolve_identity(
    *,
    phone: str | None = None,
    email: str | None = None,
    telegram_id: int | str | None = None,
    session_cookie: str | None = None,
    webui_username: str | None = None,
) -> dict:
    """Return {"user_id": "Operator"|"Operator2"|"unknown", "display_name": str, "role": str}."""
    users = _load_users()
    Operator = users.get("Operator", {})
    Operator2 = users.get("partner", {})

    # --- Operator checks ---
    if telegram_id and str(telegram_id) == str(Operator.get("telegram_id", "")):
        return {"user_id": "Operator", "display_name": "Operator", "role": "operator"}
    if phone and _normalize_phone(phone) in [_normalize_phone(h) for h in Operator.get("imessage_handles", []) if h.startswith("+")]:
        return {"user_id": "Operator", "display_name": "Operator", "role": "operator"}
    if email and email.lower() in [h.lower() for h in Operator.get("imessage_handles", []) if "@" in h]:
        return {"user_id": "Operator", "display_name": "Operator", "role": "operator"}
    if email and email.lower() == str(Operator.get("work_email", "")).lower():
        return {"user_id": "Operator", "display_name": "Operator", "role": "operator"}
    if webui_username and webui_username.lower() == str(Operator.get("webui_username", "")).lower():
        return {"user_id": "Operator", "display_name": "Operator", "role": "operator"}

    # --- Operator2 checks ---
    if telegram_id and str(telegram_id) == str(Operator2.get("telegram_id", "")):
        return {"user_id": "Operator2", "display_name": "Operator2", "role": "partner"}
    if phone and _normalize_phone(phone) in [_normalize_phone(h) for h in Operator2.get("imessage_handles", []) if h.startswith("+")]:
        return {"user_id": "Operator2", "display_name": "Operator2", "role": "partner"}
    if email and email.lower() in [h.lower() for h in Operator2.get("imessage_handles", []) if "@" in h]:
        return {"user_id": "Operator2", "display_name": "Operator2", "role": "partner"}
    if webui_username and webui_username.lower() == str(Operator2.get("webui_username", "")).lower():
        return {"user_id": "Operator2", "display_name": "Operator2", "role": "partner"}

    # --- Session cookie fallback ---
    if session_cookie:
        uid = _resolve_session_cookie(session_cookie)
        if uid == "Operator":
            return {"user_id": "Operator", "display_name": "Operator", "role": "operator"}
        if uid == "Operator2":
            return {"user_id": "Operator2", "display_name": "Operator2", "role": "partner"}

    return {"user_id": "unknown", "display_name": "Unknown", "role": "guest"}


def _normalize_phone(p: str) -> str:
    return re.sub(r"[^\d+]", "", str(p or ""))


def _resolve_session_cookie(cookie: str) -> str | None:
    try:
        sessions = json.loads((DATA_DIR / "webui_sessions.json").read_text())
        if isinstance(sessions, dict):
            for uid, sdata in sessions.items():
                if isinstance(sdata, dict) and sdata.get("token") == cookie:
                    return uid
                if isinstance(sdata, list):
                    for s in sdata:
                        if isinstance(s, dict) and s.get("token") == cookie:
                            return uid
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Day-phase detection
# ---------------------------------------------------------------------------

def _day_phase() -> str:
    """Return current day phase in CT: morning, midday, afternoon, evening, night."""
    hour = datetime.now(CT).hour
    if 5 <= hour < 9:
        return "morning"
    if 9 <= hour < 14:
        return "midday"
    if 14 <= hour < 18:
        return "afternoon"
    if 18 <= hour < 22:
        return "evening"
    return "night"


def _now_ct() -> datetime:
    return datetime.now(CT)


# ---------------------------------------------------------------------------
# JSON file readers (safe, never crash)
# ---------------------------------------------------------------------------

def _read_json(path: Path) -> dict | list | None:
    try:
        if path.exists():
            return json.loads(path.read_text())
    except Exception:
        pass
    return None


def _read_jsonl_count(path: Path) -> int:
    try:
        if path.exists():
            return sum(1 for _ in path.open())
    except Exception:
        pass
    return 0


# ---------------------------------------------------------------------------
# Data source readers
# ---------------------------------------------------------------------------

def _read_workos() -> dict:
    """WorkOS: exec assistant state, SF admin intel, current tickets."""
    result: dict = {}
    ea = _read_json(DATA_DIR / "exec_assistant_state.json")
    if isinstance(ea, dict):
        pending = ea.get("pending", [])
        active = [i for i in pending if not i.get("auto_resolved") and not i.get("done_at")]
        result["pending_items"] = len(pending)
        result["active_items"] = len(active)
        result["top_items"] = [
            {"subject": i.get("subject", ""), "urgency": i.get("urgency", ""), "kind": i.get("kind", "")}
            for i in active[:5]
        ]

    sf = _read_json(DATA_DIR / "sf_admin_intel.json")
    if isinstance(sf, dict):
        result["sf_org_health"] = sf.get("org_health_score")
        result["sf_health_label"] = sf.get("org_health_label")
        result["sf_findings"] = sf.get("finding_counts", {})

    tickets = _read_json(DATA_DIR / "ticket_priorities.json")
    if isinstance(tickets, list):
        result["open_tickets"] = len(tickets)
        result["top_tickets"] = [
            {"title": t.get("title", ""), "priority": t.get("priority", ""), "status": t.get("status", "")}
            for t in tickets[:5]
        ]

    return result


def _read_lifeos() -> dict:
    """LifeOS: personal items, bills, deliveries, appointments."""
    result: dict = {}
    intel = _read_json(DATA_DIR / "lifeos_intel.json")
    if isinstance(intel, dict):
        items = intel.get("personal_items", [])
        by_type: dict[str, list] = {}
        for item in items:
            cat = item.get("_life_category") or item.get("type") or "other"
            by_type.setdefault(cat, []).append(item)
        result["total_items"] = len(items)
        result["by_category"] = {k: len(v) for k, v in by_type.items()}
        result["urgent"] = [
            {"title": i.get("title", ""), "type": i.get("type", ""), "action": i.get("suggested_action", "")}
            for i in items if i.get("urgency") in ("warn", "urgent")
        ][:5]
    return result


def _read_calendar_alex() -> list[dict]:
    """Operator's calendar events via Outlook SQLite reader."""
    try:
        from tools.outlook_calendar_sqlite import sqlite_today_agenda, sqlite_tomorrow_events
        return [{"today": sqlite_today_agenda(), "tomorrow": sqlite_tomorrow_events()}]
    except Exception as e:
        log.debug("calendar_alex failed: %s", e)
        return []


def _read_calendar_nicole(days: int = 1) -> list[dict]:
    """Operator2's calendar events via Apple Calendar SQLite."""
    try:
        from tools.nicole_mail import get_nicole_calendar_events
        return get_nicole_calendar_events(days=days)
    except Exception as e:
        log.debug("calendar_nicole failed: %s", e)
        return []


def _read_revenue() -> dict:
    """Revenue machine state."""
    result: dict = {}
    brain = _read_json(HUSTLE_DIR / "revenue_brain_state.json")
    if isinstance(brain, dict):
        metrics = brain.get("last_metrics", {})
        result["cycle_count"] = brain.get("cycle_count", 0)
        result["last_run"] = brain.get("last_run", "")
        result["email_sent_total"] = (metrics.get("email", {}) or {}).get("total_sent", 0)
        result["email_sent_today"] = (metrics.get("email", {}) or {}).get("today", 0)
        result["gumroad_products"] = (metrics.get("gumroad", {}) or {}).get("products", 0)
        result["fiverr_gigs"] = (metrics.get("fiverr", {}) or {}).get("gigs", 0)
        result["digital_products_built"] = metrics.get("digital_products_built", 0)
        result["tiktok_engagement"] = metrics.get("tiktok_engagement", {})
        result["prospects_tiktok"] = metrics.get("prospects_tiktok_creators", 0)

    health = _read_json(HUSTLE_DIR / "revenue_health.json")
    if isinstance(health, dict):
        summary = health.get("summary", {})
        result["agents_total"] = summary.get("total", 0)
        result["agents_healthy"] = summary.get("healthy", 0)
        result["agents_dead"] = summary.get("dead", 0)
        result["agents_stuck"] = summary.get("stuck", 0)
        result["agents_erroring"] = summary.get("erroring", 0)
        result["daily_restarts"] = (health.get("daily_stats", {}) or {}).get("restarts", 0)
        flaky = (health.get("daily_stats", {}) or {}).get("flaky", {})
        result["top_flaky"] = sorted(flaky.items(), key=lambda x: -x[1])[:5] if flaky else []

    outreach_count = _read_jsonl_count(HUSTLE_DIR / "outreach_sent.jsonl")
    result["outreach_sent_total"] = outreach_count

    demand = _read_json(HUSTLE_DIR / "demand_signals.json")
    if isinstance(demand, dict):
        result["total_prospects"] = demand.get("total_prospects", 0)
        segments = demand.get("segments", {})
        result["top_segments"] = [
            {"name": k, "count": v.get("count", 0)}
            for k, v in sorted(segments.items(), key=lambda x: -(x[1].get("count", 0) if isinstance(x[1], dict) else 0))[:5]
        ]

    return result


def _read_infra() -> dict:
    """Infrastructure health: service ports, agent status."""
    result: dict = {}

    # Check key service ports
    services = {
        "webui": 5280,
        "telegram": 8443,
        "cloudflared": 8080,
    }
    for name, port in services.items():
        result[f"svc_{name}"] = _check_port(port)

    # Agent health from revenue_health.json
    health = _read_json(HUSTLE_DIR / "revenue_health.json")
    if isinstance(health, dict):
        summary = health.get("summary", {})
        result["agents_total"] = summary.get("total", 0)
        result["agents_healthy"] = summary.get("healthy", 0)
        result["agents_dead"] = summary.get("dead", 0)
        result["agents_erroring"] = summary.get("erroring", 0)
        result["restarts_today"] = (health.get("daily_stats", {}) or {}).get("restarts", 0)
        agents = health.get("agents", [])
        result["dead_agents"] = [a.get("label", "") for a in agents if a.get("status") == "dead"][:10]
        result["erroring_agents"] = [a.get("label", "") for a in agents if a.get("status") == "erroring"][:10]

    # Watchdog state
    watchdog = _read_json(DATA_DIR / "watchdog_state.json")
    if isinstance(watchdog, dict):
        result["watchdog_ok"] = True
    elif watchdog is None:
        result["watchdog_ok"] = False

    return result


def _read_self_improvement() -> dict:
    """Self-improvement memory: quiet fixes, learned patterns, unresolved work."""
    payload = _read_json(SELF_IMPROVEMENT_MEMORY)
    if not isinstance(payload, dict):
        return {"cycles": 0, "recent_actions": [], "unresolved": []}
    cycles = payload.get("cycles") if isinstance(payload.get("cycles"), list) else []
    latest = cycles[-1] if cycles else {}
    recent_actions = []
    unresolved = []
    for cycle in cycles[-10:]:
        for action in cycle.get("actions", []) if isinstance(cycle, dict) else []:
            if not isinstance(action, dict):
                continue
            recent_actions.append(
                {
                    "kind": action.get("kind", ""),
                    "status": action.get("status", ""),
                    "summary": action.get("summary", ""),
                }
            )
            if action.get("status") not in {"ok", "skipped"}:
                unresolved.append(
                    {
                        "kind": action.get("kind", ""),
                        "status": action.get("status", ""),
                        "summary": action.get("summary", ""),
                    }
                )
    return {
        "cycles": len(cycles),
        "latest_at": latest.get("generated_at") or latest.get("timestamp") or "",
        "latest_summary": latest.get("summary", ""),
        "recent_actions": recent_actions[-10:],
        "unresolved": unresolved[-10:],
    }


def record_self_improvement_learning(event: dict, *, limit: int = 200) -> dict:
    """Append a self-improvement cycle to the context brain memory."""
    payload = _read_json(SELF_IMPROVEMENT_MEMORY)
    if not isinstance(payload, dict):
        payload = {"version": 1, "cycles": []}
    cycles = payload.get("cycles")
    if not isinstance(cycles, list):
        cycles = []
    cleaned = dict(event or {})
    cleaned.setdefault("generated_at", _now_ct().isoformat())
    cycles.append(cleaned)
    payload["version"] = 1
    payload["updated_at"] = _now_ct().isoformat()
    payload["cycles"] = cycles[-max(1, int(limit or 200)):]
    SELF_IMPROVEMENT_MEMORY.parent.mkdir(parents=True, exist_ok=True)
    SELF_IMPROVEMENT_MEMORY.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str))
    return payload


def _check_port(port: int) -> bool:
    """Quick check if a local port is responding."""
    try:
        r = subprocess.run(
            ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", f"http://localhost:{port}/"],
            capture_output=True, text=True, timeout=3,
        )
        return r.stdout.strip() not in ("", "000")
    except Exception:
        return False


def _read_family() -> dict:
    """Family/kids events from multiple sources."""
    result: dict = {}

    # Family calendar digest
    digest = _read_json(DATA_DIR / "family_calendar_digest.json")
    if isinstance(digest, dict):
        d = digest.get("digest", {})
        result["family_event_count"] = d.get("family_count", 0)
        result["alex_event_count"] = d.get("alex_count", 0)
        result["conflicts"] = d.get("conflict_count", 0)
        result["digest_body"] = d.get("body", "")
        result["digest_date"] = digest.get("date", "")

    # LifeOS items tagged as family/kids
    intel = _read_json(DATA_DIR / "lifeos_intel.json")
    if isinstance(intel, dict):
        family_kw = {"kid", "kids", "school", "ard", "mason", "lola", "pediatr", "soccer", "ballet", "daycare", "childcare"}
        family_items = []
        for item in intel.get("personal_items", []):
            title = str(item.get("title", "")).lower()
            if any(kw in title for kw in family_kw):
                family_items.append({
                    "title": item.get("title", ""),
                    "type": item.get("type", ""),
                    "when": item.get("when", item.get("suggested_action", "")),
                })
        result["family_items"] = family_items[:10]

    # Operator2 calendar (family context)
    nicole_events = _read_calendar_nicole(days=2)
    family_kw_set = {"kid", "kids", "school", "ard", "mason", "lola", "pediatr", "soccer", "ballet", "daycare"}
    nicole_family = []
    for ev in nicole_events:
        title = str(ev.get("title") or ev.get("subject") or "").lower()
        if any(kw in title for kw in family_kw_set):
            nicole_family.append({
                "title": ev.get("title") or ev.get("subject", ""),
                "when": ev.get("start") or ev.get("when", ""),
            })
    if nicole_family:
        result["nicole_family_events"] = nicole_family[:10]

    return result


# ---------------------------------------------------------------------------
# Snapshot builders
# ---------------------------------------------------------------------------

def get_now_snapshot(user_id: str) -> dict:
    """What should this person focus on RIGHT NOW."""
    phase = _day_phase()
    now = _now_ct()
    snap: dict = {
        "user_id": user_id,
        "timestamp": now.isoformat(),
        "day_phase": phase,
        "phase_guidance": _phase_guidance(phase),
    }

    if user_id == "Operator":
        snap["workos"] = _read_workos()
        snap["calendar"] = _read_calendar_alex()
        try:
            from tools.outlook_calendar_sqlite import sqlite_upcoming_events
            snap["calendar_upcoming"] = sqlite_upcoming_events(7)
        except Exception:
            snap["calendar_upcoming"] = "unavailable"
        snap["life_urgent"] = _read_lifeos().get("urgent", [])
        if phase in ("evening", "night"):
            snap["revenue_summary"] = _revenue_oneliner()
            snap["family"] = _read_family()
    elif user_id == "Operator2":
        snap["calendar"] = _read_calendar_nicole(days=1)
        snap["life"] = _read_lifeos()
        snap["family"] = _read_family()
    else:
        snap["note"] = "Unknown user; limited context available."

    return snap


def get_today_snapshot(user_id: str) -> dict:
    """Full day view."""
    phase = _day_phase()
    now = _now_ct()
    snap: dict = {
        "user_id": user_id,
        "date": now.strftime("%A, %B %-d, %Y"),
        "timestamp": now.isoformat(),
        "day_phase": phase,
    }

    if user_id == "Operator":
        snap["workos"] = _read_workos()
        snap["calendar"] = _read_calendar_alex()
        try:
            from tools.outlook_calendar_sqlite import sqlite_upcoming_events
            snap["calendar_upcoming"] = sqlite_upcoming_events(7)
        except Exception:
            snap["calendar_upcoming"] = "unavailable"
        snap["life"] = _read_lifeos()
        snap["revenue_summary"] = _revenue_oneliner()
        snap["infra_summary"] = _infra_oneliner()
        snap["self_improvement"] = _read_self_improvement()
        snap["family"] = _read_family()
    elif user_id == "Operator2":
        snap["calendar"] = _read_calendar_nicole(days=1)
        snap["life"] = _read_lifeos()
        snap["family"] = _read_family()

    return snap


def get_tomorrow_preview(user_id: str) -> dict:
    """Tomorrow lookahead."""
    now = _now_ct()
    tomorrow = now + timedelta(days=1)
    snap: dict = {
        "user_id": user_id,
        "date": tomorrow.strftime("%A, %B %-d, %Y"),
        "timestamp": now.isoformat(),
    }

    if user_id == "Operator":
        try:
            from tools.outlook_calendar_sqlite import sqlite_tomorrow_events
            snap["calendar"] = sqlite_tomorrow_events()
        except Exception:
            snap["calendar"] = "unavailable"
        snap["workos_pending"] = _read_workos().get("active_items", 0)
    elif user_id == "Operator2":
        snap["calendar"] = _read_calendar_nicole(days=2)
        snap["family"] = _read_family()

    return snap


def get_revenue_snapshot() -> dict:
    """Revenue machine health, blockers, next moves."""
    rev = _read_revenue()
    infra = _read_infra()
    now = _now_ct()

    snap: dict = {
        "timestamp": now.isoformat(),
        "revenue": rev,
        "agent_health": {
            "total": infra.get("agents_total", 0),
            "healthy": infra.get("agents_healthy", 0),
            "dead": infra.get("agents_dead", 0),
            "erroring": infra.get("agents_erroring", 0),
            "dead_list": infra.get("dead_agents", []),
            "erroring_list": infra.get("erroring_agents", []),
        },
    }

    # Compute blockers
    blockers = []
    if rev.get("agents_dead", 0) > 3:
        blockers.append(f"{rev['agents_dead']} agents dead — restart needed")
    if rev.get("daily_restarts", 0) > 1000:
        blockers.append(f"{rev['daily_restarts']} restarts today — crash loop likely")
    if rev.get("email_sent_today", 0) == 0 and now.hour > 10:
        blockers.append("No emails sent today — outreach may be stalled")
    snap["blockers"] = blockers

    # Next moves
    moves = []
    if rev.get("agents_dead", 0) > 0:
        moves.append("Restart dead agents")
    if rev.get("gumroad_products", 0) < 40:
        moves.append("List more Gumroad products")
    if rev.get("outreach_sent_total", 0) > 0 and rev.get("email_sent_today", 0) == 0:
        moves.append("Check outreach pipeline — 0 sent today")
    snap["next_moves"] = moves

    return snap


def get_infra_snapshot() -> dict:
    """Service health, agent status, errors."""
    return {
        "timestamp": _now_ct().isoformat(),
        "infra": _read_infra(),
        "self_improvement": _read_self_improvement(),
    }


def get_family_snapshot() -> dict:
    """Kids/family events that affect both Operator and Operator2."""
    return {
        "timestamp": _now_ct().isoformat(),
        "family": _read_family(),
    }


# ---------------------------------------------------------------------------
# Phase guidance
# ---------------------------------------------------------------------------

_PHASE_GUIDANCE = {
    "morning": "Plan the day. Check risks, appointments, prep items.",
    "midday": "Focus on now/next. Active items and blockers.",
    "afternoon": "Check progress. Clear blockers, handle pending items.",
    "evening": "Recap the day. Check off completed, preview tomorrow.",
    "night": "Tomorrow prep. Minimal interactions, review only.",
}


def _phase_guidance(phase: str) -> str:
    return _PHASE_GUIDANCE.get(phase, "")


def _revenue_oneliner() -> str:
    rev = _read_revenue()
    parts = []
    if rev.get("email_sent_today"):
        parts.append(f"{rev['email_sent_today']} emails today")
    if rev.get("gumroad_products"):
        parts.append(f"{rev['gumroad_products']} products")
    if rev.get("agents_healthy") is not None:
        parts.append(f"{rev.get('agents_healthy', 0)}/{rev.get('agents_total', 0)} agents healthy")
    return " | ".join(parts) if parts else "no data"


def _infra_oneliner() -> str:
    infra = _read_infra()
    parts = []
    for key in ("svc_webui", "svc_telegram", "svc_cloudflared"):
        name = key.replace("svc_", "")
        status = "up" if infra.get(key) else "DOWN"
        parts.append(f"{name}:{status}")
    if infra.get("agents_dead", 0) > 0:
        parts.append(f"{infra['agents_dead']} dead agents")
    return " | ".join(parts) if parts else "no data"


# ---------------------------------------------------------------------------
# Command handler
# ---------------------------------------------------------------------------

_COMMAND_MAP = {
    "now": "now",
    "what should i do": "now",
    "focus": "now",
    "today": "today",
    "recap": "today",
    "summary": "today",
    "tomorrow": "tomorrow",
    "revenue": "revenue",
    "money": "revenue",
    "sales": "revenue",
    "hustle": "revenue",
    "infra": "infra",
    "health": "infra",
    "services": "infra",
    "status": "infra",
    "life": "family",
    "family": "family",
    "kids": "family",
    "home": "family",
    "calendar": "today",
}


def handle_command(text: str, user_id: str = "Operator") -> dict | None:
    """Route a text command to the right snapshot function. Returns None if no match."""
    normalized = text.strip().lower()

    # Direct keyword match
    for trigger, cmd in _COMMAND_MAP.items():
        if normalized == trigger or normalized.startswith(trigger + " ") or normalized.startswith(trigger + "?"):
            return _dispatch(cmd, user_id)

    # Substring match for natural language
    for trigger, cmd in _COMMAND_MAP.items():
        if trigger in normalized:
            return _dispatch(cmd, user_id)

    return None


def _dispatch(cmd: str, user_id: str) -> dict:
    if cmd == "now":
        return get_now_snapshot(user_id)
    if cmd == "today":
        return get_today_snapshot(user_id)
    if cmd == "tomorrow":
        return get_tomorrow_preview(user_id)
    if cmd == "revenue":
        return get_revenue_snapshot()
    if cmd == "infra":
        return get_infra_snapshot()
    if cmd == "family":
        return get_family_snapshot()
    return get_now_snapshot(user_id)
