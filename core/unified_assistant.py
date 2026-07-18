from __future__ import annotations

from core.constants import LOCAL_MODEL
"""Unified assistant — single omnichannel command router.

Every frontend (WebUI, Telegram, iMessage) calls handle_message() here.
Slash commands and natural-language intents get routed to snapshot
formatters or the LLM pipeline. Responses are adapted per channel.
"""

import json
import logging
import re
import time
from pathlib import Path
from typing import Optional

from core import BASE_DIR, mountain_now

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data paths
# ---------------------------------------------------------------------------
_DATA = BASE_DIR / "data"
_EXEC_STATE = _DATA / "exec_assistant_state.json"
_DIAG_REPORT = _DATA / "diagnostic_report.json"
_WORKOS_INTEL = _DATA / "workos_intel.json"
_LIFEOS_INTEL = _DATA / "lifeos_intel.json"
_REVENUE_METRICS = _DATA / "revenue_daily_metrics.json"
_HEALTH_STATE = _DATA / "health_state.json"

# ---------------------------------------------------------------------------
# Channel limits
# ---------------------------------------------------------------------------
_CHANNEL_LIMITS = {
    "webui": 8000,
    "telegram": 2000,
    "imessage": 500,
}


def _limit(channel: str) -> int:
    return _CHANNEL_LIMITS.get(channel, 2000)


# ---------------------------------------------------------------------------
# Intent detection
# ---------------------------------------------------------------------------

_SLASH_COMMANDS: dict[str, str] = {
    "/now": "now",
    "/today": "today",
    "/week": "week",
    "/tomorrow": "tomorrow",
    "/life": "life",
    "/work": "work",
    "/revenue": "revenue",
    "/money": "revenue",
    "/infra": "infra",
    "/health": "infra",
    "/queue": "queue",
}

_NL_INTENTS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"^what should i do\b", re.I), "now"),
    (re.compile(r"^what.?s (next|up)\b", re.I), "now"),
    (re.compile(r"^what.?s on (my |today.?s )?(agenda|calendar|plate)\b", re.I), "today"),
    (re.compile(r"^(?:what(?:'s| is)?|show|give me|get me|pull up).{0,20}(?:my\s+|this\s+|the\s+)?week\b", re.I), "week"),
    (re.compile(r"^how.{0,20}(?:my\s+)?week.{0,20}look\b", re.I), "week"),
    (re.compile(r"^(show|get) ?(me )?(the )?week\b", re.I), "week"),
    (re.compile(r"^(show|get) ?(me )?(the )?queue\b", re.I), "queue"),
    (re.compile(r"^(show|get) ?(me )?(the )?revenue\b", re.I), "revenue"),
    (re.compile(r"^(how.?s |show |get )?infra(structure)?\b", re.I), "infra"),
    (re.compile(r"^system (health|status)\b", re.I), "infra"),
]


def _detect_intent(text: str) -> tuple[str, str]:
    """Return (intent, extra_arg) or ("", "") if no match."""
    stripped = text.strip()
    lower = stripped.lower()

    # /done <id> is special
    m = re.match(r"^/done\s+(.+)", stripped, re.I)
    if m:
        return "done", m.group(1).strip()

    # /life [user]
    m = re.match(r"^/life(?:\s+(\w+))?$", stripped, re.I)
    if m:
        return "life", (m.group(1) or "").strip().lower()

    # Exact slash commands
    token = lower.split()[0] if lower else ""
    if token in _SLASH_COMMANDS:
        return _SLASH_COMMANDS[token], ""

    # Natural language intents
    for pattern, intent in _NL_INTENTS:
        if pattern.search(lower):
            return intent, ""

    return "", ""


# ---------------------------------------------------------------------------
# Safe JSON loader
# ---------------------------------------------------------------------------

def _load_json(path: Path) -> dict | list | None:
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Snapshot builders
# ---------------------------------------------------------------------------

def _now_snapshot() -> dict:
    """Build a 'what should I do right now' snapshot."""
    now = mountain_now()
    result: dict = {"time": now.strftime("%I:%M %p %Z"), "focus": None, "next": None, "meetings_left": 0}

    # Queue items
    state = _load_json(_EXEC_STATE)
    if state:
        pending = [p for p in state.get("pending", []) if not p.get("done_at")]
        high = [p for p in pending if p.get("urgency") == "high"]
        result["focus"] = high[0].get("subject", "Check queue") if high else (pending[0].get("subject", "Check queue") if pending else "Queue empty")
        result["next"] = pending[1].get("subject") if len(pending) > 1 else None
        result["queue_size"] = len(pending)

    # WorkOS insights
    intel = _load_json(_WORKOS_INTEL)
    if intel:
        insights = intel.get("insights", [])
        urgent = [i for i in insights if i.get("level") == "urgent"]
        if urgent:
            result["urgent_insights"] = len(urgent)

    return result


def _today_snapshot() -> dict:
    """Build today's agenda snapshot."""
    now = mountain_now()
    result: dict = {"date": now.strftime("%A, %B %d"), "queue_items": 0, "insights": 0}

    state = _load_json(_EXEC_STATE)
    if state:
        pending = [p for p in state.get("pending", []) if not p.get("done_at")]
        result["queue_items"] = len(pending)
        by_urgency: dict[str, int] = {}
        for p in pending:
            u = p.get("urgency", "low")
            by_urgency[u] = by_urgency.get(u, 0) + 1
        result["by_urgency"] = by_urgency

    intel = _load_json(_WORKOS_INTEL)
    if intel:
        result["insights"] = len(intel.get("insights", []))

    return result


def _week_snapshot() -> dict:
    """Build a live week context snapshot from the unified context brain."""
    now = mountain_now()
    result: dict = {
        "date": now.strftime("%A, %B %d"),
        "today": {},
        "tomorrow": {},
        "revenue": {},
        "infra": {},
        "family": {},
        "meeting_notes": [],
    }

    try:
        from core.context_brain import (
            get_today_snapshot,
            get_tomorrow_preview,
            get_revenue_snapshot,
            get_infra_snapshot,
            get_family_snapshot,
        )

        today = get_today_snapshot("Operator") or {}
        tomorrow = get_tomorrow_preview("Operator") or {}
        revenue = get_revenue_snapshot() or {}
        infra = get_infra_snapshot() or {}
        family = get_family_snapshot() or {}

        if isinstance(today, dict):
            result["today"] = today
        if isinstance(tomorrow, dict):
            result["tomorrow"] = tomorrow
        if isinstance(revenue, dict):
            result["revenue"] = revenue
        if isinstance(infra, dict):
            result["infra"] = infra
        if isinstance(family, dict):
            result["family"] = family

        meeting_lines: list[str] = []
        cal = today.get("calendar") if isinstance(today, dict) else None
        if isinstance(cal, list):
            for block in cal:
                if isinstance(block, dict):
                    for key in ("today", "tomorrow", "upcoming"):
                        val = block.get(key)
                        if val and str(val).strip() and str(val).strip() != "unavailable":
                            meeting_lines.extend(
                                line.strip()
                                for line in str(val).splitlines()
                                if line.strip()
                            )
        upcoming = today.get("calendar_upcoming") if isinstance(today, dict) else None
        if upcoming and str(upcoming).strip() and str(upcoming).strip() != "unavailable":
            meeting_lines.extend(line.strip() for line in str(upcoming).splitlines() if line.strip())

        try:
            from tools.littlebird_local import _reconcile_meetings_with_calendar

            meeting_blob = str(_reconcile_meetings_with_calendar("Operator") or "").strip()
            meeting_lines.extend(line.strip() for line in meeting_blob.splitlines() if line.strip() and "|" in line)
        except Exception as e:
            log.debug("week littlebird snapshot failed: %s", e)

        # Deduplicate while preserving order.
        seen: set[str] = set()
        deduped: list[str] = []
        for line in meeting_lines:
            key = line.strip()
            if not key or key in seen:
                continue
            seen.add(key)
            deduped.append(key)

        result["meeting_notes"] = deduped[:24]
    except Exception as e:
        log.debug("week context brain snapshot failed: %s", e)

    return result


def _tomorrow_snapshot() -> dict:
    """Preview for tomorrow."""
    return {"note": "Tomorrow preview not yet wired to calendar. Check /today for current state."}


def _life_snapshot(user: str = "") -> dict:
    """LifeOS snapshot."""
    data = _load_json(_LIFEOS_INTEL)
    if not data:
        return {"status": "No LifeOS data available."}
    result: dict = {}
    items = data.get("personal_items", [])
    if user:
        items = [i for i in items if user in str(i).lower()]
    result["items"] = len(items)
    result["top"] = [str(i.get("subject", i) if isinstance(i, dict) else i)[:80] for i in items[:5]]
    insights = data.get("personal_insights", [])
    if insights:
        result["insights"] = [str(i.get("text", i) if isinstance(i, dict) else i)[:80] for i in insights[:3]]
    return result


def _work_snapshot() -> dict:
    """WorkOS snapshot."""
    intel = _load_json(_WORKOS_INTEL)
    if not intel:
        return {"status": "No WorkOS data available."}
    result: dict = {}
    insights = intel.get("insights", [])
    result["total_insights"] = len(insights)
    by_level: dict[str, int] = {}
    for i in insights:
        lvl = i.get("level", "info")
        by_level[lvl] = by_level.get(lvl, 0) + 1
    result["by_level"] = by_level
    result["top"] = [i.get("text", "")[:80] for i in insights[:5]]
    predrafts = intel.get("predrafts", [])
    result["predrafts"] = len(predrafts)
    return result


def _revenue_snapshot() -> dict:
    """Revenue / money snapshot."""
    data = _load_json(_REVENUE_METRICS)
    if not data:
        return {"status": "No revenue data available."}
    # Get latest day
    dates = sorted(data.keys())
    if not dates:
        return {"status": "No revenue data available."}
    latest = data[dates[-1]]
    return {
        "date": dates[-1],
        "emails_sent": latest.get("emails_sent", 0),
        "proposals": latest.get("proposals_submitted", 0),
        "scans": latest.get("scans_started", 0),
        "revenue": latest.get("revenue", 0),
        "prospects_total": latest.get("prospects_total", 0),
    }


def _infra_snapshot() -> dict:
    """Infrastructure / health snapshot."""
    report = _load_json(_DIAG_REPORT)
    if not report:
        return {"status": "No diagnostic data available."}
    fc = report.get("finding_counts", {})
    return {
        "critical": fc.get("critical", 0),
        "warn": fc.get("warn", 0),
        "info": fc.get("info", 0),
        "services": report.get("services_seen", 0),
        "as_of": report.get("as_of_iso", "?"),
        "synthesis": str(report.get("synthesis", ""))[:300],
    }


def _queue_snapshot() -> dict:
    """Action queue snapshot."""
    state = _load_json(_EXEC_STATE)
    if not state:
        return {"items": [], "count": 0}
    pending = [p for p in state.get("pending", []) if not p.get("done_at")]
    items = []
    order = {"high": 0, "medium": 1, "low": 2}
    for p in sorted(pending, key=lambda x: order.get(x.get("urgency", "low"), 3))[:15]:
        items.append({
            "urgency": p.get("urgency", "?"),
            "kind": p.get("kind", "?"),
            "subject": p.get("subject", ""),
            "from": p.get("from_name", ""),
            "id": p.get("id", ""),
        })
    return {"items": items, "count": len(pending)}


def _mark_done(item_id: str) -> str:
    """Mark a queue item as done."""
    try:
        state = json.loads(_EXEC_STATE.read_text())
    except Exception:
        return "Could not load queue state."
    for p in state.get("pending", []):
        if str(p.get("id", "")) == item_id or str(p.get("subject", "")).lower().startswith(item_id.lower()):
            p["done_at"] = mountain_now().isoformat()
            _EXEC_STATE.write_text(json.dumps(state, indent=2))
            return f"Marked done: {p.get('subject', item_id)}"
    return f"No queue item matching '{item_id}'."


# ---------------------------------------------------------------------------
# Response formatters
# ---------------------------------------------------------------------------

def _fmt_now(snap: dict, channel: str) -> str:
    focus = snap.get("focus", "Nothing urgent")
    nxt = snap.get("next")
    queue_size = snap.get("queue_size", 0)
    urgent = snap.get("urgent_insights", 0)

    if channel == "imessage":
        parts = [f"Focus: {focus[:60]}"]
        if nxt:
            parts.append(f"Next: {nxt[:50]}")
        parts.append(f"{queue_size} queued")
        if urgent:
            parts.append(f"{urgent} urgent")
        return ". ".join(parts)

    bold = "**" if channel == "telegram" else "**"
    parts = [f"{bold}Focus on:{bold} {focus}"]
    if nxt:
        parts.append(f"{bold}Next:{bold} {nxt}")
    parts.append(f"{queue_size} items in queue.")
    if urgent:
        parts.append(f"{urgent} urgent insight(s) need attention.")
    return "\n".join(parts)


def _fmt_today(snap: dict, channel: str) -> str:
    date = snap.get("date", "Today")
    qi = snap.get("queue_items", 0)
    by_u = snap.get("by_urgency", {})
    insights = snap.get("insights", 0)

    if channel == "imessage":
        return f"{date}: {qi} queue items ({by_u.get('high',0)} high). {insights} insights."

    parts = [f"**{date}**", f"Queue: {qi} items"]
    if by_u:
        parts.append(f"  High: {by_u.get('high',0)} | Med: {by_u.get('medium',0)} | Low: {by_u.get('low',0)}")
    parts.append(f"Insights: {insights}")
    return "\n".join(parts)


def _fmt_week(snap: dict, channel: str) -> str:
    # Keep this path model-driven; the snapshot is only live context.
    lines: list[str] = [f"Week of {snap.get('date', 'this week')}"]
    today = snap.get("today", {}) if isinstance(snap.get("today", {}), dict) else {}
    tomorrow = snap.get("tomorrow", {}) if isinstance(snap.get("tomorrow", {}), dict) else {}
    revenue = snap.get("revenue", {}) if isinstance(snap.get("revenue", {}), dict) else {}
    infra = snap.get("infra", {}) if isinstance(snap.get("infra", {}), dict) else {}
    family = snap.get("family", {}) if isinstance(snap.get("family", {}), dict) else {}
    meeting_notes = snap.get("meeting_notes", []) if isinstance(snap.get("meeting_notes", []), list) else []

    if today:
        lines.append(f"Today snapshot: {json.dumps(today, default=str)[:1200]}")
    if tomorrow:
        lines.append(f"Tomorrow snapshot: {json.dumps(tomorrow, default=str)[:1200]}")
    if revenue:
        lines.append(f"Revenue snapshot: {json.dumps(revenue, default=str)[:800]}")
    if infra:
        lines.append(f"Infra snapshot: {json.dumps(infra, default=str)[:800]}")
    if family:
        lines.append(f"Family snapshot: {json.dumps(family, default=str)[:800]}")
    if meeting_notes:
        lines.append("Meeting sources:")
        lines.extend(f"- {m}" for m in meeting_notes[:24])
    return "\n".join(lines)


def _fmt_tomorrow(snap: dict, channel: str) -> str:
    return snap.get("note", "No preview available.")


def _fmt_life(snap: dict, channel: str) -> str:
    if snap.get("status"):
        return snap["status"]
    items = snap.get("items", 0)
    top = snap.get("top", [])
    insights = snap.get("insights", [])

    if channel == "imessage":
        parts = [f"Life: {items} items"]
        for t in top[:2]:
            parts.append(f"- {t[:50]}")
        return "\n".join(parts)

    parts = [f"**LifeOS** ({items} items)"]
    for t in top[:5]:
        parts.append(f"- {t}")
    if insights:
        parts.append("\n**Insights:**")
        for i in insights:
            parts.append(f"- {i}")
    return "\n".join(parts)


def _fmt_work(snap: dict, channel: str) -> str:
    if snap.get("status"):
        return snap["status"]
    total = snap.get("total_insights", 0)
    by_level = snap.get("by_level", {})
    top = snap.get("top", [])
    predrafts = snap.get("predrafts", 0)

    if channel == "imessage":
        return f"Work: {total} insights ({by_level.get('urgent',0)} urgent). {predrafts} predrafts."

    parts = [f"**WorkOS** ({total} insights)"]
    parts.append(f"Urgent: {by_level.get('urgent',0)} | Warn: {by_level.get('warn',0)} | Info: {by_level.get('info',0)}")
    if predrafts:
        parts.append(f"{predrafts} predrafted replies ready")
    for t in top[:5]:
        parts.append(f"- {t}")
    return "\n".join(parts)


def _fmt_revenue(snap: dict, channel: str) -> str:
    if snap.get("status"):
        return snap["status"]
    emails = snap.get("emails_sent", 0)
    proposals = snap.get("proposals", 0)
    scans = snap.get("scans", 0)
    rev = snap.get("revenue", 0)
    prospects = snap.get("prospects_total", 0)

    if channel == "imessage":
        return f"Rev: {emails} emails, {proposals} proposals, {scans} scans. ${rev} revenue. {prospects:,} prospects."

    parts = [f"**Revenue** ({snap.get('date','today')})"]
    parts.append(f"Emails sent: {emails}")
    parts.append(f"Proposals: {proposals}")
    parts.append(f"Scans: {scans}")
    parts.append(f"Revenue: ${rev}")
    parts.append(f"Prospects: {prospects:,}")
    return "\n".join(parts)


def _fmt_infra(snap: dict, channel: str) -> str:
    if snap.get("status"):
        return snap["status"]
    crit = snap.get("critical", 0)
    warn = snap.get("warn", 0)
    info = snap.get("info", 0)
    svcs = snap.get("services", 0)
    synth = snap.get("synthesis", "")

    status = "ALL UP" if crit == 0 else f"{crit} CRITICAL"

    if channel == "imessage":
        return f"Infra: {status}. {svcs} services. {warn} warnings."

    parts = [f"**Infrastructure: {status}**"]
    parts.append(f"Services: {svcs} | Critical: {crit} | Warn: {warn} | Info: {info}")
    if synth:
        limit = _limit(channel) - 200
        parts.append(synth[:limit])
    return "\n".join(parts)


def _fmt_queue(snap: dict, channel: str) -> str:
    items = snap.get("items", [])
    count = snap.get("count", 0)
    if not items:
        return "Action queue is empty."

    urgency_icon = {"high": "[H]", "medium": "[M]", "low": "[L]"}
    if channel != "imessage":
        urgency_icon = {"high": "RED", "medium": "YEL", "low": "BLU"}

    if channel == "imessage":
        parts = [f"Queue ({count}):"]
        for it in items[:5]:
            parts.append(f"{urgency_icon.get(it['urgency'],'[?]')} {it['subject'][:40]}")
        return "\n".join(parts)

    parts = [f"**Action Queue ({count} items)**"]
    for it in items:
        icon = urgency_icon.get(it["urgency"], "[?]")
        kind = it.get("kind", "?")[:8]
        subj = it.get("subject", "")[:60]
        sender = it.get("from", "")[:20]
        parts.append(f"[{icon}] [{kind}] {sender}: {subj}")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# LLM fallback for natural language
# ---------------------------------------------------------------------------

async def _llm_respond(text: str, user_id: str, channel: str, thread_id: str | None) -> str:
    """Route natural language through Claude API -> local Ollama fallback."""
    from core.api_client import APIClient
    from core import conversation, local_client

    api = APIClient()
    history = conversation.get_history(user_id, thread_id=thread_id)

    # Add current message
    history.append({"role": "user", "content": text})

    # Build context addon
    now = mountain_now()
    addon = f"Channel: {channel}. Time: {now.strftime('%I:%M %p %Z, %A %B %d')}."
    try:
        from core.smart_context import build_chat_context

        live_context = build_chat_context(user_id, text)
        if live_context:
            addon += "\n" + live_context
    except Exception as e:
        log.debug("smart context addon failed: %s", e)
    if channel == "imessage":
        addon += " Keep response under 500 chars, plain text only."
    elif channel == "telegram":
        addon += " Keep response under 2000 chars. Bold/italic OK."

    # Try Claude API
    try:
        from core import SETTINGS
        model = SETTINGS["routing"]["sonnet_model"]
        response = await api.create_message(
            model=model,
            messages=history,
            user_addon=addon,
            max_tokens=1024 if channel == "imessage" else 4096,
            cost_source=f"unified_assistant:{channel}",
        )
        if response:
            # Extract text from response
            for block in response.content:
                if hasattr(block, "text"):
                    result = block.text
                    conversation.save_message(user_id, "user", text, thread_id=thread_id)
                    conversation.save_message(user_id, "assistant", result, thread_id=thread_id)
                    return _truncate(result, channel)
    except Exception as e:
        log.warning("Claude API failed in unified_assistant, falling back to local: %s", e)

    # Fallback to local Ollama
    try:
        result = await local_client.chat(
            messages=history[-10:],
            model=LOCAL_MODEL,
            system=f"You are a helpful assistant. {addon}",
            max_tokens=1024 if channel == "imessage" else 2048,
            source="unified_assistant",
        )
        if result:
            text_result = result if isinstance(result, str) else result[0] if isinstance(result, tuple) else str(result)
            conversation.save_message(user_id, "user", text, thread_id=thread_id)
            conversation.save_message(user_id, "assistant", text_result, thread_id=thread_id)
            return _truncate(text_result, channel)
    except Exception as e:
        log.error("Local LLM also failed in unified_assistant: %s", e)

    try:
        from core.chat_intelligence import availability_fallback_reply

        return availability_fallback_reply(text, history, channel=channel)
    except Exception:
        return "I need a little more context. Send the target message, ticket, file, app, or exact action."


def _truncate(text: str, channel: str) -> str:
    limit = _limit(channel)
    if len(text) <= limit:
        return text
    return text[:limit - 3] + "..."


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

_INTENT_HANDLERS: dict[str, tuple] = {
    "now": (_now_snapshot, _fmt_now),
    "today": (_today_snapshot, _fmt_today),
    "week": (_week_snapshot, _fmt_week),
    "tomorrow": (_tomorrow_snapshot, _fmt_tomorrow),
    "life": (None, None),  # special — takes arg
    "work": (_work_snapshot, _fmt_work),
    "revenue": (_revenue_snapshot, _fmt_revenue),
    "infra": (_infra_snapshot, _fmt_infra),
    "queue": (_queue_snapshot, _fmt_queue),
}


async def handle_message(
    text: str,
    user_id: str,
    channel: str,
    thread_id: Optional[str] = None,
) -> str:
    """Single entry point for all channels.

    Parameters
    ----------
    text : str
        Raw user message.
    user_id : str
        Resolved user identifier.
    channel : str
        One of "webui", "telegram", "imessage".
    thread_id : str, optional
        Conversation thread identifier.

    Returns
    -------
    str
        Formatted response text adapted for the channel.
    """
    if not text or not text.strip():
        return ""

    intent, arg = _detect_intent(text)

    if not intent:
        # No command detected — pass through to LLM
        return await _llm_respond(text, user_id, channel, thread_id)

    # /done <id>
    if intent == "done":
        return _mark_done(arg)

    # /life [user]
    if intent == "life":
        snap = _life_snapshot(arg)
        return _fmt_life(snap, channel)

    # Standard snapshot commands
    handler = _INTENT_HANDLERS.get(intent)
    if intent == "week":
        snap = _week_snapshot()
        # Keep the answer model-driven; this snapshot only feeds live context.
        return await _llm_respond(f"{text}\n\nLive week context:\n{_fmt_week(snap, channel)}", user_id, channel, thread_id)
    if handler and handler[0] and handler[1]:
        snap_fn, fmt_fn = handler
        snap = snap_fn()
        return fmt_fn(snap, channel)

    return await _llm_respond(text, user_id, channel, thread_id)


# ---------------------------------------------------------------------------
# Intent check for frontends — fast, no async needed
# ---------------------------------------------------------------------------

def is_command(text: str) -> bool:
    """Return True if text matches a known command/intent.

    Frontends use this to decide whether to route through unified_assistant
    or fall through to the existing engine.
    """
    if not text or not text.strip():
        return False
    intent, _ = _detect_intent(text)
    return bool(intent)
