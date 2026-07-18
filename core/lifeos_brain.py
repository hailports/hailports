from __future__ import annotations

"""LifeOS Brain — executive-assistant intelligence layer for home/family/life.

Produces a structured day-phase payload consumed by the /api/lifeos/intel
endpoint.  Sections:

  now          — needs attention RIGHT NOW (next 2 hours, overdue, urgent money)
  next         — coming up today (remaining appointments, tasks, deadlines)
  needs_closure — past events today that need checkoff/followup
  family_kids  — all kid/school/medical events (Mason, etc.)
  money_admin  — bills, charges, payments, financial items
  tomorrow     — tomorrow's schedule + prep needed
  waiting      — items waiting on someone else
  quiet        — low-priority backlog suppressed from main view

Priority ordering within each section:
  1. Money in / opportunities
  2. Bills / money out
  3. Appointments (time-sorted)
  4. Kids / family / school
  5. Household / admin
  6. Low-value noise
"""

import json
import re
import time
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("lifeos_brain")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_NOW_WINDOW_HOURS = 2
_STALE_RECURRING_DAYS = 5  # filter out recurring events older than this

_PRIORITY_ORDER = {
    "money in": 0,
    "money out": 1,
    "appointment": 2,
    "calendar": 2,
    "kids/family": 3,
    "family": 3,
    "delivery": 4,
    "household": 5,
    "account": 6,
    "admin": 7,
    "noise": 99,
}

_FAMILY_TOKENS = (
    "mason", "school", "teacher", "parent", "ard", "iep",
    "doctor", "dentist", "orthodont", "pediatric", "family",
    "birthday", "practice", "game", "daycare", "pickup",
    "pick up", "drop off", "drop-off", "kid", "kids",
    "child", "children", "recital", "lesson", "field trip",
    "school event", "medical",
)

_MONEY_IN_TOKENS = (
    "payment received", "invoice paid", "paid invoice", "deposit received",
    "money in", "revenue", "gig purchased", "opportunity",
    "proposal accepted", "new customer", "client approved",
    "contract signed", "lead converted", "closed won",
)

_MONEY_OUT_TOKENS = (
    "bill", "payment due", "autopay", "statement", "invoice due",
    "amount due", "past due", "overdue", "utility", "mortgage",
    "rent", "subscription", "renewal", "charge", "receipt",
    "bank", "credit card", "insurance premium", "tuition",
    "was unsuccessful", "couldn't process", "payment failed",
)

_WAITING_TOKENS = (
    "waiting on", "pending approval", "no reply", "awaiting",
    "follow up", "followup", "follow-up", "waiting for",
)

_SUPPRESS_TOKENS = (
    "new event", "untitled event", "no title",
)


def _file_age_hours(path: Path) -> float | None:
    try:
        if not path.exists():
            return None
        return round(max(0.0, time.time() - path.stat().st_mtime) / 3600.0, 2)
    except Exception:
        return None


def _read_json_dict(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text())
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _source_health(user_id: str) -> dict:
    """Return source freshness/counts without exposing message contents."""
    if user_id != "Operator2":
        return {"source_stale": False}

    local_cache = DATA_DIR / "runtime" / "nicole_local_sources.json"
    legacy_inbox = DATA_DIR / "nicole_inbox_state.json"
    legacy_insights = DATA_DIR / "nicole_insights.json"
    cache = _read_json_dict(local_cache)
    cache_age_h = _file_age_hours(local_cache)
    active_cache_fresh = cache_age_h is not None and cache_age_h <= 6.0

    return {
        "source_stale": not active_cache_fresh,
        "active_cache": "nicole_local_sources",
        "active_cache_age_hours": cache_age_h,
        "cached_inbox_items": len(cache.get("inbox") or []) if isinstance(cache.get("inbox"), list) else 0,
        "cached_calendar_items": len(cache.get("calendar") or []) if isinstance(cache.get("calendar"), list) else 0,
        "cached_unread_accounts": len(cache.get("unread_counts") or {}) if isinstance(cache.get("unread_counts"), dict) else 0,
        "legacy_inbox_age_hours": _file_age_hours(legacy_inbox),
        "legacy_inbox_stale": (_file_age_hours(legacy_inbox) or 999999.0) > 6.0,
        "legacy_insights_age_hours": _file_age_hours(legacy_insights),
        "legacy_insights_stale": (_file_age_hours(legacy_insights) or 999999.0) > 6.0,
    }


def _get_nicole_local_source_bundle(max_age_hours: float = 6.0) -> dict:
    path = DATA_DIR / "runtime" / "nicole_local_sources.json"
    age_h = _file_age_hours(path)
    if age_h is None or age_h > max_age_hours:
        return {}
    return _read_json_dict(path)


# ---------------------------------------------------------------------------
# Timezone helper
# ---------------------------------------------------------------------------
def _get_tz():
    """Return the configured stack timezone."""
    try:
        from core import _get_stack_tz
        return _get_stack_tz()
    except Exception:
        from zoneinfo import ZoneInfo
        return ZoneInfo("America/Chicago")


def _now() -> datetime:
    return datetime.now(_get_tz())


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------
def _item_text(item: dict) -> str:
    fields = [
        item.get("subject"), item.get("title"), item.get("preview"),
        item.get("suggested_action"), item.get("from"),
        item.get("from_name"), item.get("from_email"),
        item.get("account"), item.get("calendar"),
        item.get("type"), item.get("kind"), item.get("source"),
    ]
    return " ".join(str(v or "") for v in fields).lower()


def _has_any(text: str, tokens: tuple[str, ...]) -> bool:
    return any(t in text for t in tokens)


def _classify_category(item: dict) -> str:
    text = _item_text(item)
    kind = str(item.get("type") or item.get("kind") or "").lower()
    if _has_any(text, _MONEY_IN_TOKENS):
        return "money in"
    if _has_any(text, _MONEY_OUT_TOKENS):
        return "money out"
    if kind in ("calendar", "appointment") or _has_any(text, ("appointment", "meeting", "invite", "invitation", "rsvp", "reservation")):
        if _has_any(text, _FAMILY_TOKENS):
            return "kids/family"
        return "appointment"
    if _has_any(text, _FAMILY_TOKENS):
        return "kids/family"
    if _has_any(text, ("icloud", "apple id", "find my", "storage full", "password", "security alert")):
        return "account"
    if _has_any(text, ("delivery", "delivered", "out for delivery", "tracking", "shipped", "usps", "ups", "fedex")):
        return "delivery"
    return item.get("_life_category") or item.get("type") or ""


def _is_family_event(item: dict) -> bool:
    text = _item_text(item)
    return _has_any(text, _FAMILY_TOKENS)


def _is_waiting(item: dict) -> bool:
    text = _item_text(item)
    return _has_any(text, _WAITING_TOKENS)


def _is_suppressed(item: dict) -> bool:
    text = _item_text(item)
    return _has_any(text, _SUPPRESS_TOKENS) and len(str(item.get("title") or item.get("subject") or "").strip()) < 15


def _priority_score(item: dict) -> float:
    cat = _classify_category(item)
    base = float(_PRIORITY_ORDER.get(cat, 8))
    urgency = str(item.get("urgency") or "").lower()
    if urgency in ("urgent", "high"):
        base -= 0.4
    elif urgency == "warn":
        base -= 0.1
    return base


# ---------------------------------------------------------------------------
# Time parsing
# ---------------------------------------------------------------------------
def _parse_event_time(item: dict, now: datetime) -> datetime | None:
    """Try to parse a start datetime from item fields."""
    for field in ("when", "start", "start_raw", "start_time"):
        raw = str(item.get(field) or "").strip()
        if not raw:
            continue
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
            try:
                dt = datetime.strptime(raw[:19], fmt)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=now.tzinfo)
                return dt
            except Exception:
                continue
        try:
            dt = datetime.fromisoformat(raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=now.tzinfo)
            return dt
        except Exception:
            continue
    return None


def _parse_end_time(item: dict, now: datetime) -> datetime | None:
    for field in ("end", "end_time", "end_raw"):
        raw = str(item.get(field) or "").strip()
        if not raw:
            continue
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
            try:
                dt = datetime.strptime(raw[:19], fmt)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=now.tzinfo)
                return dt
            except Exception:
                continue
    return None


def _time_label(dt: datetime, now: datetime) -> str:
    if dt.date() == now.date():
        return f"Today {dt.strftime('%-I:%M %p')}"
    if dt.date() == (now + timedelta(days=1)).date():
        return f"Tomorrow {dt.strftime('%-I:%M %p')}"
    return f"{dt.strftime('%a %b %-d')} {dt.strftime('%-I:%M %p')}"


# ---------------------------------------------------------------------------
# Calendar data sources
# ---------------------------------------------------------------------------
def _get_alex_calendar_events(now: datetime) -> list[dict]:
    """Fetch Operator's Outlook calendar events for today + tomorrow."""
    events = []
    try:
        from tools.outlook_calendar_sqlite import _get_events_for_range
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=2)
        raw_events = _get_events_for_range(start, end)
        for ev in raw_events:
            start_dt = ev.get("start_time")
            end_dt = ev.get("end_time")
            events.append({
                "title": str(ev.get("subject") or "").strip(),
                "type": "calendar",
                "_life_category": "appointment",
                "when": start_dt.strftime("%Y-%m-%d %H:%M") if start_dt else "",
                "end": end_dt.strftime("%Y-%m-%d %H:%M") if end_dt else "",
                "start_time": start_dt,
                "end_time": end_dt,
                "is_recurring": ev.get("is_recurring", False),
                "attendees": ev.get("attendees", 0),
                "join_url": ev.get("join_url", ""),
                "location": ev.get("location", ""),
                "calendar": "Outlook",
                "source": "outlook_sqlite",
                "user": "Operator",
            })
    except Exception as exc:
        log.warning("Operator calendar fetch: %r", exc)
    return events


def _get_apple_calendar_events(now: datetime, user_id: str = "Operator") -> list[dict]:
    """Fetch Apple Calendar events for today + tomorrow.

    These cover Operator2's calendars and shared family calendars.
    """
    events = []
    if user_id == "Operator2":
        bundle = _get_nicole_local_source_bundle()
        cached = bundle.get("calendar")
        if isinstance(cached, list) and cached:
            for ev in cached:
                if not isinstance(ev, dict):
                    continue
                subject = str(ev.get("subject") or ev.get("title") or "").strip()
                if not subject:
                    continue
                events.append({
                    "title": subject,
                    "type": "calendar",
                    "_life_category": "appointment",
                    "when": str(ev.get("start") or ev.get("start_raw") or "").strip(),
                    "end": str(ev.get("end") or "").strip(),
                    "calendar": str(ev.get("calendar") or ""),
                    "account": str(ev.get("account") or ev.get("owner") or ""),
                    "source": "nicole_local_sources",
                    "user": "Operator2",
                })
            return events

    try:
        from tools.nicole_mail import _get_nicole_calendar_events_from_sqlite
        raw = _get_nicole_calendar_events_from_sqlite(days=2)
        for ev in raw:
            if not isinstance(ev, dict):
                continue
            subject = str(ev.get("subject") or "").strip()
            if not subject:
                continue
            events.append({
                "title": subject,
                "type": "calendar",
                "_life_category": "appointment",
                "when": str(ev.get("start") or "").strip(),
                "end": str(ev.get("end") or "").strip(),
                "calendar": str(ev.get("calendar") or ""),
                "account": str(ev.get("account") or ""),
                "source": "apple_calendar",
                "user": "Operator2",
            })
    except Exception as exc:
        log.warning("Apple calendar fetch: %r", exc)
    return events


def _get_all_calendar_events(now: datetime, user_id: str = "Operator") -> list[dict]:
    """Merge calendar events from all sources and deduplicate."""
    outlook = _get_alex_calendar_events(now) if user_id != "Operator2" else []
    apple = _get_apple_calendar_events(now, user_id)
    all_events = outlook + apple

    # Deduplicate by (subject_lower, start_time)
    seen: set[tuple[str, str]] = set()
    deduped: list[dict] = []
    for ev in all_events:
        title = str(ev.get("title") or "").strip().lower()
        when = str(ev.get("when") or "").strip()
        key = (title, when)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(ev)

    # Sort by start time
    def sort_key(ev: dict) -> str:
        return str(ev.get("when") or "9999")
    deduped.sort(key=sort_key)
    return deduped


# ---------------------------------------------------------------------------
# Mail data sources
# ---------------------------------------------------------------------------
def _get_alex_mail_items(limit: int = 80) -> list[dict]:
    """Fetch Operator's personal mail items via apple_mail_index."""
    try:
        from core import apple_mail_index
        rows = apple_mail_index.unread_inbox(limit=max(limit, 80), since_hours=336)
        rows.extend(apple_mail_index.recent_inbox(limit=max(limit, 80)))
    except Exception:
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        key = str(row.get("rowid") or row.get("message_id") or f"{row.get('subject')}:{row.get('date')}")
        if key in seen:
            continue
        seen.add(key)
        out.append(_normalize_mail(row, source="apple_mail_index", user="Operator"))
        if len(out) >= limit:
            break
    return out


def _get_nicole_mail_items(limit: int = 40) -> list[dict]:
    """Fetch Operator2's personal mail items."""
    bundle = _get_nicole_local_source_bundle()
    cached = bundle.get("inbox")
    if isinstance(cached, list) and cached:
        return [_normalize_mail(r, source="nicole_local_sources", user="Operator2") for r in cached[:limit] if isinstance(r, dict)]

    try:
        from tools.nicole_mail import get_nicole_inbox
        raw = get_nicole_inbox(count=limit)
        return [_normalize_mail(r, source="nicole_mail", user="Operator2") for r in raw]
    except Exception:
        return []


def _normalize_mail(row: dict, *, source: str = "mail", user: str = "Operator") -> dict:
    sender = row.get("from") or row.get("sender_str") or row.get("from_email") or ""
    account = row.get("account") or row.get("account_email") or ""
    return {
        "title": str(row.get("subject") or "").strip(),
        "subject": str(row.get("subject") or "").strip(),
        "from": str(sender).strip(),
        "from_name": str(row.get("from_name") or "").strip(),
        "account": str(account).strip(),
        "preview": str(row.get("preview") or row.get("summary") or "").strip()[:300],
        "read": row.get("read"),
        "urgency": row.get("urgency"),
        "needs_reply": row.get("needs_reply"),
        "date": str(row.get("date") or row.get("received") or "").strip(),
        "source": source,
        "user": user,
        "type": "email",
    }


# ---------------------------------------------------------------------------
# Work item filter (reuse bridge logic)
# ---------------------------------------------------------------------------
_WORK_DOMAINS = {
    "kipi.ai", "cnovate.io", "CompanyA.com", "wns.com", "tavant.com",
    "docsapp.dev", "promptsite.app", "branda.com",
}

_WORK_TOKENS = (
    "salesforce", "sf-", "admin ai", "territory reassignment",
    "dealer permissions", "opportunity view access", "snowflake",
    "capgemini", "technical coe", "employee success", "stand-up",
    "standup", "pipeline", "forecast", "sprint",
)


def _email_domain(value: str) -> str:
    m = re.search(r"[a-z0-9._%+\-]+@([a-z0-9.\-]+\.[a-z]{2,})", str(value or "").lower())
    return m.group(1) if m else ""


def _is_work_item(item: dict) -> bool:
    """True if this item is clearly work-related and should be excluded from LifeOS."""
    text = _item_text(item)
    source = str(item.get("source") or "").lower()
    if source in ("exec", "workos", "salesforce", "sf_admin"):
        return True
    acct_dom = _email_domain(str(item.get("account") or ""))
    sender_dom = _email_domain(str(item.get("from") or ""))
    if acct_dom in _WORK_DOMAINS or sender_dom in _WORK_DOMAINS:
        # Family override: kid/family tokens on work calendar still show
        if _is_family_event(item):
            return False
        return True
    if any(t in text for t in _WORK_TOKENS):
        if _is_family_event(item):
            return False
        return True
    return False


# ---------------------------------------------------------------------------
# Day-phase assignment
# ---------------------------------------------------------------------------
def _assign_phase(item: dict, now: datetime) -> str:
    """Assign an item to a day-phase section.

    Returns one of: now, next, needs_closure, family_kids, money_admin,
    tomorrow, waiting, quiet
    """
    if _is_suppressed(item):
        return "quiet"

    if _is_waiting(item):
        return "waiting"

    cat = _classify_category(item)
    start_dt = _parse_event_time(item, now)
    end_dt = _parse_end_time(item, now)

    today = now.date()
    tomorrow = (now + timedelta(days=1)).date()
    window_end = now + timedelta(hours=_NOW_WINDOW_HOURS)

    # Calendar events: phase by time
    if start_dt:
        ev_date = start_dt.date()

        # Stale recurring from before today (> STALE_RECURRING_DAYS ago)
        if ev_date < today - timedelta(days=_STALE_RECURRING_DAYS):
            return "quiet"

        # Past events today → needs_closure
        if ev_date == today:
            effective_end = end_dt if end_dt else (start_dt + timedelta(hours=1))
            if effective_end <= now:
                return "needs_closure"
            if start_dt <= window_end:
                # Family events always cross-surface
                if _is_family_event(item):
                    return "now"  # will also appear in family_kids via cross-surface
                return "now"
            return "next"

        # Tomorrow
        if ev_date == tomorrow:
            return "tomorrow"

        # Future beyond tomorrow → quiet backlog
        if ev_date > tomorrow:
            return "quiet"

        # Past day (yesterday or earlier) → needs closure only if today
        return "quiet"

    # Non-calendar items
    if cat in ("money in", "money out"):
        return "money_admin"
    if cat in ("kids/family", "family"):
        return "family_kids"
    if cat == "account":
        return "money_admin"
    if cat == "delivery":
        urgency = str(item.get("urgency") or "").lower()
        if urgency in ("urgent", "action-needed"):
            return "now"
        return "next"

    # Default: if urgency is high → now, otherwise next
    urgency = str(item.get("urgency") or "").lower()
    if urgency in ("urgent", "high", "action-needed"):
        return "now"
    return "next"


# ---------------------------------------------------------------------------
# Section builder
# ---------------------------------------------------------------------------
def _sort_section(items: list[dict], now: datetime) -> list[dict]:
    """Sort items within a section by priority order, then by time."""
    def key(item: dict) -> tuple[float, str]:
        score = _priority_score(item)
        start_dt = _parse_event_time(item, now)
        time_key = start_dt.strftime("%Y-%m-%d %H:%M") if start_dt else "9999"
        return (score, time_key)
    return sorted(items, key=key)


def _build_section(
    name: str,
    label: str,
    items: list[dict],
    now: datetime,
    *,
    limit: int = 15,
) -> dict:
    """Build a section dict for the payload."""
    sorted_items = _sort_section(items, now)[:limit]
    # Annotate each item with a display time label
    for item in sorted_items:
        start_dt = _parse_event_time(item, now)
        if start_dt:
            item["_time_label"] = _time_label(start_dt, now)
        cat = _classify_category(item)
        if cat:
            item["_life_category"] = cat
    return {
        "name": name,
        "label": label,
        "count": len(sorted_items),
        "items": sorted_items,
    }


# ---------------------------------------------------------------------------
# Cross-surface logic: kid/family events appear in family_kids AND their
# time-based section (now/next/tomorrow)
# ---------------------------------------------------------------------------
def _cross_surface_family(
    sections: dict[str, list[dict]],
    all_items: list[dict],
    now: datetime,
) -> None:
    """Copy family/kid events into the family_kids section even if they're
    already assigned to now/next/tomorrow."""
    family_titles: set[str] = set()
    for item in sections.get("family_kids", []):
        family_titles.add(str(item.get("title") or "").strip().lower())

    for item in all_items:
        if not _is_family_event(item):
            continue
        title = str(item.get("title") or "").strip().lower()
        if title in family_titles:
            continue
        family_titles.add(title)
        sections.setdefault("family_kids", []).append(item)


# ---------------------------------------------------------------------------
# Insights generator
# ---------------------------------------------------------------------------
def _generate_insights(sections: dict[str, list[dict]], now: datetime) -> list[dict]:
    insights: list[dict] = []
    now_count = len(sections.get("now", []))
    closure_count = len(sections.get("needs_closure", []))
    tomorrow_count = len(sections.get("tomorrow", []))
    money_count = len(sections.get("money_admin", []))
    family_count = len(sections.get("family_kids", []))
    waiting_count = len(sections.get("waiting", []))

    if now_count:
        insights.append({
            "level": "urgent",
            "text": f"{now_count} item(s) need attention right now.",
        })
    if closure_count:
        insights.append({
            "level": "info",
            "text": f"{closure_count} earlier event(s) today need checkoff or followup.",
        })
    if money_count:
        money_items = sections.get("money_admin", [])
        money_in = sum(1 for i in money_items if _classify_category(i) == "money in")
        money_out = sum(1 for i in money_items if _classify_category(i) == "money out")
        parts = []
        if money_in:
            parts.append(f"{money_in} money-in")
        if money_out:
            parts.append(f"{money_out} bill/charge")
        other = money_count - money_in - money_out
        if other:
            parts.append(f"{other} account")
        insights.append({
            "level": "warn" if money_out else "info",
            "text": f"{', '.join(parts)} item(s) in money/admin.",
        })
    if family_count:
        insights.append({
            "level": "info",
            "text": f"{family_count} family/kids event(s) are on the radar.",
        })
    if tomorrow_count:
        insights.append({
            "level": "info",
            "text": f"{tomorrow_count} item(s) on tomorrow's schedule; review prep needed.",
        })
    if waiting_count:
        insights.append({
            "level": "info",
            "text": f"{waiting_count} item(s) waiting on someone else.",
        })

    # Day-phase contextual
    hour = now.hour
    if hour >= 20:
        insights.append({
            "level": "info",
            "text": "Evening wind-down: check off remaining items, preview tomorrow.",
        })
    elif hour >= 15 and closure_count:
        insights.append({
            "level": "info",
            "text": "Late-day recap: close out earlier events before tomorrow prep.",
        })

    if not insights:
        insights.append({"level": "info", "text": "Personal queue is clear right now."})
    return insights


# ---------------------------------------------------------------------------
# Main payload builder
# ---------------------------------------------------------------------------
def build_lifeos_intel(
    user_id: str = "Operator",
    now: datetime | None = None,
) -> dict:
    """Build the full LifeOS intelligence payload for a user.

    Returns a dict with day-phase sections, insights, and metadata.
    """
    now = now or _now()

    # ── Gather raw data ──
    calendar_events = _get_all_calendar_events(now, user_id)
    if user_id == "Operator2":
        mail_items = _get_nicole_mail_items(limit=40)
    else:
        mail_items = _get_alex_mail_items(limit=80)

    # ── Filter to personal-only ──
    all_items: list[dict] = []

    for ev in calendar_events:
        if _is_work_item(ev):
            # Family override: kid events on work calendar cross-surface
            if _is_family_event(ev):
                ev["_cross_surfaced"] = True
            else:
                continue
        all_items.append(ev)

    for mail in mail_items:
        if _is_work_item(mail):
            continue
        cat = _classify_category(mail)
        if not cat or cat in ("strategic", ""):
            # Only include if it matches a personal category
            continue
        all_items.append(mail)

    # ── Assign day-phase ──
    sections: dict[str, list[dict]] = {
        "now": [],
        "next": [],
        "needs_closure": [],
        "family_kids": [],
        "money_admin": [],
        "tomorrow": [],
        "waiting": [],
        "quiet": [],
    }

    for item in all_items:
        phase = _assign_phase(item, now)
        sections.setdefault(phase, []).append(item)

    # ── Cross-surface family events ──
    _cross_surface_family(sections, all_items, now)

    # ── Build structured sections ──
    payload_sections = [
        _build_section("now", "Now", sections.get("now", []), now, limit=10),
        _build_section("next", "Next", sections.get("next", []), now, limit=12),
        _build_section("needs_closure", "Needs Closure", sections.get("needs_closure", []), now, limit=8),
        _build_section("family_kids", "Family & Kids", sections.get("family_kids", []), now, limit=10),
        _build_section("money_admin", "Money & Admin", sections.get("money_admin", []), now, limit=8),
        _build_section("tomorrow", "Tomorrow Preview", sections.get("tomorrow", []), now, limit=10),
        _build_section("waiting", "Waiting / No Reply", sections.get("waiting", []), now, limit=6),
        _build_section("quiet", "Quiet Backlog", sections.get("quiet", []), now, limit=5),
    ]

    # ── Generate insights ──
    insights = _generate_insights(sections, now)
    source_health = _source_health(user_id)
    if source_health.get("source_stale"):
        insights.insert(0, {
            "level": "warn",
            "text": "LifeOS source cache is stale or unavailable; showing best-effort data until the next local sync.",
        })

    # ── Legacy flat list (backward compat with current UI) ──
    personal_items: list[dict] = []
    for section in payload_sections:
        if section["name"] == "quiet":
            continue
        personal_items.extend(section["items"])

    # Cap legacy flat list
    personal_items = personal_items[:20]

    # ── Source summary ──
    cal_count = sum(1 for i in all_items if str(i.get("type") or "").lower() == "calendar")
    mail_count = sum(1 for i in all_items if str(i.get("type") or "").lower() == "email")
    family_count = len(sections.get("family_kids", []))

    return {
        # New structured format
        "sections": payload_sections,
        # Legacy flat format for backward compat
        "personal_items": personal_items,
        "noise": sections.get("quiet", [])[:5],
        "personal_insights": insights,
        # Metadata
        "updated": now.isoformat(),
        "builder": "lifeos_brain_v3",
        "user": user_id,
        "day_phase": _current_day_phase(now),
        "source_summary": {
            "calendar_items": cal_count,
            "mail_items": mail_count,
            "family_items": family_count,
            "total_items": len(all_items),
            **source_health,
        },
        "source_stale": bool(source_health.get("source_stale")),
    }


def _current_day_phase(now: datetime) -> str:
    """Return a human label for the current time of day."""
    h = now.hour
    if h < 6:
        return "early_morning"
    if h < 9:
        return "morning"
    if h < 12:
        return "late_morning"
    if h < 14:
        return "midday"
    if h < 17:
        return "afternoon"
    if h < 20:
        return "evening"
    return "night"


# ---------------------------------------------------------------------------
# Convenience: write payload to disk (for cron/scheduled use)
# ---------------------------------------------------------------------------
def write_lifeos_intel(user_id: str = "Operator", path: Path | None = None) -> dict:
    """Build and write LifeOS intel to JSON file."""
    payload = build_lifeos_intel(user_id=user_id)
    out_path = path or (DATA_DIR / "lifeos_intel.json")
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(payload, indent=2, default=str))
    except Exception as exc:
        log.warning("Failed to write lifeos intel: %r", exc)
    return payload


# ---------------------------------------------------------------------------
# CLI self-test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys
    user = sys.argv[1] if len(sys.argv) > 1 else "Operator"
    payload = build_lifeos_intel(user_id=user)
    print(json.dumps(payload, indent=2, default=str))
