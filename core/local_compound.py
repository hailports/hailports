"""Local compound query handler — runs multiple local tools and synthesizes with Qwen3.

Handles multi-source queries like "what are my priorities today" or "give me a rundown"
entirely locally at $0 cost. Detects which tools are relevant, runs them in parallel,
feeds combined results to Qwen3 for synthesis.

User-aware: respects Operator2's tool scoping (no SF/Outlook/Monday/SharePoint/Teams/Zoom).
"""

from core.constants import LOCAL_MODEL
import asyncio
import json
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path
from core import local_client
from core.basic_questions import is_basic_current_question

log = logging.getLogger(__name__)

# Compound query triggers — broad requests that need multiple data sources
_DAYS = r"(?:today|tomorrow|yesterday|monday|tuesday|wednesday|thursday|friday|saturday|sunday|this\s+week|next\s+week|my\s+week|the\s+week|workday|work\s+day|school\s+day)"
_WEEKDAY_INDEX = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}

COMPOUND_PATTERNS = re.compile(
    # Priorities / urgency
    r"(what.{0,15}(priorities|important|urgent|top|focus|to.?do|action)"
    r"|priorities"
    r"|top\s+priorities"
    r"|what.{0,20}(?:are|is|'?s).{0,10}(?:my\s+)?(?:work\s*items?|tasks?|to.?dos?|priorities)"
    r"|(?:work\s*items?|work\s+queue|action\s+queue|my\s+tasks|my\s+to.?dos)"
    r"|what.{0,20}(pressing|critical|on\s+fire|needs?\s+attention|needs?\s+me|high\s+priority|hot)"
    r"|what.{0,15}(should|need).{0,20}(do|handle|tackle|watch|know)"
    r"|what\s+matters\s+(?:" + _DAYS + r")"
    r"|what.{0,10}(?:is|are|'?s)?\s*(?:pressing|critical|urgent|hot)\s*(?:" + _DAYS + r")?"
    r"|anything\s+(?:pressing|critical|urgent|hot|on\s+fire)"
    r"|where\s+do\s+i\s+start"
    r"|what\s+should\s+i\s+(focus|work|tackle|start)\s+on"

    # Rundown / summary / briefing (casual variations)
    r"|(?:give\s*me|gimme|get\s*me|show\s*me|pull\s*up|i\s+need|let\s*me\s+(?:see|get|have)).{0,15}(rundown|summary|overview|briefing|status|update|recap|lowdown)"
    r"|(?:morning|daily|eod|end\s+of\s+day)\s*(brief|briefing|rundown|update|summary|recap|status)"
    r"|rundown"
    r"|briefing"

    # "What does my day look like" variations
    r"|what.{0,15}(?:day|" + _DAYS + r").{0,15}look"
    r"|how.{0,10}(?:my\s+)?(?:day|" + _DAYS + r").{0,10}look"
    r"|how.{0,25}(?:work|office|business|salesforce|family|parent).{0,20}look"
    r"|(?:business|executive|work|salesforce|architect|admin|parent|family).{0,20}(?:brief|rundown|overview|status|priorities|pressing)"
    r"|(?:work|office|business|salesforce|family|parent).{0,15}(?:day|week).{0,15}look"
    r"|what.{0,10}(?:have|got).{0,15}(?:going|coming|planned|scheduled|on)"
    r"|what.{0,10}(?:on\s+tap|on\s+deck|lined\s+up|in\s+store)"

    # "What's going on" / "what's happening" / "what's on my plate"
    r"|what.{0,10}(?:going\s+on|happening|on\s+my\s+plate|need.{0,5}know)"
    r"|what.{0,5}on\s+my\s+plate"

    # Catch-me-up
    r"|catch\s+me\s+up|bring\s+me\s+up\s+to\s+speed|fill\s+me\s+in"

    # Everything / all sources
    r"|everything\s+(?:" + _DAYS + r"|important)"
    r"|all\s+(?:my|the)\s+(?:stuff|things|items|tasks|work)"
    r"|across\s+all\s+(?:data\s+)?sources"

    # Status / missed
    r"|status\s+update|what.{0,10}(?:miss|missed)|did\s+i\s+miss"

    # My day / today / tomorrow as standalone multi-source intent
    r"|my\s+day"
    r"|(?:" + _DAYS + r").{0,10}look\s+like"
    r"|what.{0,5}(?:" + _DAYS + r")\s+look"

    # "What do I have tomorrow" — broad day-query with implied multi-source
    r"|what\s+(?:do\s+i|have\s+i|did\s+i)\s+(?:have|got)\s+(?:for\s+|on\s+)?(?:" + _DAYS + r")"
    r"|(?:anything|everything|much)\s+(?:going\s+on\s+|happening\s+|planned\s+|scheduled\s+)?(?:for\s+|on\s+)?(?:" + _DAYS + r")"
    r")",
    re.I,
)

# Tool sets per user type
# Each entry: (tool_name, tool_input, label_for_synthesis)
ALEX_TOOLS = [
    ("outlook_get_inbox", {"count": 10}, "Inbox (latest 10)"),
    ("outlook_unread_count", {}, "Unread email count"),
    ("outlook_today_agenda", {}, "Calendar today"),
    ("outlook_upcoming_events", {"days": 7}, "Work calendar next 7 days"),
    ("monday_list_boards", {"limit": 10}, "Monday.com boards"),
    ("littlebird_my_meetings", {"user": "Operator"}, "LittleBird my meetings"),
    ("littlebird_recent_notes", {"count": 8}, "LittleBird recent notes"),
    ("littlebird_action_items", {}, "Action items from meetings"),
    ("cal_today", {"owner": "all"}, "Family/personal calendar today"),
    ("apple_reminders_list", {"list_name": "Reminders", "show_completed": False}, "Open reminders"),
    ("zoom_status", {}, "Zoom status"),
]

def _nicole_tools():
    """Build Operator2's tool set with dynamic dates."""
    from datetime import datetime, timedelta
    today = datetime.now().strftime("%Y-%m-%d")
    in2 = (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%d")
    return [
        ("cal_today", {"owner": "Operator2"}, "Calendar today"),
        ("cal_get_events", {"start_date": today, "end_date": in2, "owner": "Operator2"}, "Calendar next 2 days"),
    ]

# Users who get restricted tool sets
RESTRICTED_USERS = {"partner", "Operator2"}


def is_compound_query(text: str) -> bool:
    """Check if a query needs multiple data sources."""
    if is_basic_current_question(text):
        return False
    lowered = str(text or "").lower()
    if re.search(r"\bweather|temperature|forecast\b", lowered):
        return False
    if re.search(r"\b(my\s+week|this\s+week|next\s+week|the\s+week|week\s+rundown|week\s+brief|what(?:'s| is)?\s+(?:on\s+)?(?:my|the|this)\s+week|how(?:'s| is| does)\s+(?:my\s+)?week\s+look|what(?:'s| is)\s+happening\s+this\s+week)\b", lowered, re.I):
        return True
    return bool(COMPOUND_PATTERNS.search(text))


def _get_tool_set(user_id: str) -> list:
    """Get the appropriate tool set for a user."""
    if user_id in RESTRICTED_USERS:
        return _nicole_tools()
    return ALEX_TOOLS


def _shorten(text: str, limit: int = 92) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip(" -")
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _calendar_brief(calendar: str, max_events: int = 4) -> list[str]:
    raw_lines = [line.strip() for line in str(calendar or "").splitlines() if line.strip()]
    if not raw_lines:
        return []

    header = raw_lines[0]
    count_match = re.search(r"(\d+)\s+event", header, re.I)
    count = int(count_match.group(1)) if count_match else None
    events: list[str] = []
    for line in raw_lines[1:]:
        item = line.strip()
        if re.match(r"^-?\s*\(\d+\s+attendees?\)", item, re.I):
            continue
        if item.startswith("- "):
            item = item[2:].strip()
        item = re.sub(r"\s+", " ", item).strip()
        if item:
            events.append(_shorten(item))

    if not events:
        return [_shorten(header)]

    shown = events[:max_events]
    label_count = count if count is not None else len(events)
    lines = [f"Calendar: {label_count} event(s); first {len(shown)}."]
    lines.extend(f"- {event}" for event in shown)
    remaining = max(label_count - len(shown), 0)
    if remaining:
        lines.append(f"- +{remaining} more.")
    return lines


def _load_json_file(path: Path):
    try:
        if path.exists():
            return json.loads(path.read_text())
    except Exception:
        return None
    return None


def _urgency_rank(value: str) -> int:
    token = str(value or "").strip().lower()
    if token in {"critical", "urgent", "blocker"}:
        return 0
    if token in {"high", "p1"}:
        return 1
    if token in {"medium", "normal", "p2"}:
        return 2
    return 3


def _active_exec_items(root: Path, limit: int = 7) -> tuple[list[dict], int]:
    payload = _load_json_file(root / "data" / "exec_assistant_state.json")
    pending = payload.get("pending") if isinstance(payload, dict) else []
    if not isinstance(pending, list):
        return [], 0
    active = []
    for item in pending:
        if not isinstance(item, dict):
            continue
        status = str(item.get("status") or "").strip().lower()
        if item.get("auto_resolved") or item.get("done_at") or item.get("completed_at"):
            continue
        if status in {"done", "resolved", "closed", "complete", "completed"}:
            continue
        active.append(item)
    active.sort(
        key=lambda item: (
            _urgency_rank(item.get("urgency") or item.get("priority")),
            str(item.get("last_seen") or item.get("created_at") or ""),
        )
    )
    return active[:limit], len(active)


def _ticket_priority_items(root: Path, limit: int = 6) -> list[dict]:
    payload = _load_json_file(root / "data" / "ticket_priorities.json")
    tickets = payload.get("top_20") if isinstance(payload, dict) else []
    if isinstance(tickets, list) and tickets:
        return [t for t in tickets[:limit] if isinstance(t, dict)]

    snapshot = _load_json_file(root / "data" / "runtime" / "salesforce_job_cache" / "salesforce_ticket_snapshot.open_tickets.json")
    payload = snapshot.get("payload") if isinstance(snapshot, dict) else []
    if isinstance(payload, list):
        return [t for t in payload[:limit] if isinstance(t, dict)]
    return []


def _admin_ai_items(root: Path, limit: int = 3) -> tuple[list[dict], int]:
    payload = _load_json_file(root / "data" / "admin_ai_continuation_backlog.json")
    tasks = payload.get("tasks") if isinstance(payload, dict) else []
    if not isinstance(tasks, list):
        return [], int(payload.get("pending_count") or 0) if isinstance(payload, dict) else 0
    active = []
    for task in tasks:
        if not isinstance(task, dict):
            continue
        status = str(task.get("status") or "").strip().lower()
        if status in {"done", "complete", "completed", "failed", "closed"}:
            continue
        active.append(task)
    pending_count = int(payload.get("pending_count") or len(active)) if isinstance(payload, dict) else len(active)
    return active[:limit], pending_count


def _workos_intel_lines(root: Path, limit: int = 3) -> list[str]:
    payload = _load_json_file(root / "data" / "workos_intel.json")
    insights = payload.get("insights") if isinstance(payload, dict) else []
    lines = []
    if isinstance(insights, list):
        for insight in insights:
            if isinstance(insight, dict):
                text = insight.get("summary") or insight.get("title") or insight.get("message") or insight.get("insight")
            else:
                text = insight
            text = _shorten(text, 130)
            if text:
                lines.append(text)
            if len(lines) >= limit:
                break
    return lines


def _work_item_text(value) -> str:
    if isinstance(value, dict):
        ticket = value.get("ticket") or value.get("ticket_id") or value.get("case") or value.get("id")
        action_type = value.get("action_type") or value.get("type") or value.get("kind")
        body = (
            value.get("body")
            or value.get("summary")
            or value.get("message")
            or value.get("text")
            or value.get("title")
            or value.get("subject")
        )
        parts = []
        if ticket:
            parts.append(str(ticket))
        if action_type:
            parts.append(str(action_type).replace("_", " "))
        if body:
            parts.append(str(body))
        if parts:
            return " - ".join(parts)
        try:
            return json.dumps(value, ensure_ascii=False, sort_keys=True)
        except Exception:
            return str(value)
    if isinstance(value, list):
        return "; ".join(part for part in (_work_item_text(v) for v in value[:3]) if part)
    return str(value or "").strip()


def _format_exec_item(item: dict) -> str:
    urgency = str(item.get("urgency") or item.get("priority") or "").strip()
    source = str(item.get("source") or item.get("kind") or "").strip()
    subject = _work_item_text(
        item.get("subject")
        or item.get("title")
        or item.get("preview")
        or item.get("reason")
        or item.get("suggested_action")
        or item.get("action")
    )
    action = _work_item_text(item.get("suggested_action") or item.get("action") or item.get("reason") or "")
    left = " / ".join(part for part in (urgency, source) if part)
    prefix = f"[{left}] " if left else ""
    detail = _shorten(subject, 112)
    if action and action.strip() != subject.strip():
        detail = f"{detail} - {_shorten(action, 88)}"
    return f"- {prefix}{detail}".strip()


def _format_ticket_item(item: dict) -> str:
    ticket = item.get("ticket") or item.get("name") or item.get("Name") or item.get("id") or "ticket"
    priority = item.get("priority") or item.get("Priority__c") or ""
    status = item.get("status") or item.get("Status__c") or ""
    subject = item.get("subject") or item.get("Subject__c") or item.get("summary") or ""
    assigned = item.get("assigned_to") or item.get("Assigned_To__c") or ""
    meta = " / ".join(str(part) for part in (priority, status) if part)
    detail = f"{ticket}"
    if meta:
        detail += f" [{meta}]"
    if subject:
        detail += f" {subject}"
    if assigned:
        detail += f" - {assigned}"
    return f"- {_shorten(detail, 132)}"


def _format_admin_item(item: dict) -> str:
    title = item.get("title") or item.get("summary") or item.get("request") or item.get("text") or item.get("id") or "admin task"
    status = item.get("status") or ""
    owner = item.get("owner") or item.get("agent") or ""
    meta = " / ".join(str(part) for part in (status, owner) if part)
    detail = f"[{meta}] {title}" if meta else str(title)
    return f"- {_shorten(detail, 126)}"


def _summarize_meeting_lines(text: str, max_people: int = 3) -> tuple[list[str], int]:
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    seen: list[str] = []
    count = 0
    for line in lines:
        low = line.lower()
        if "No meetings" in line or low.startswith("error:"):
            continue
        if "|" not in line:
            continue
        parts = [p.strip() for p in line.split("|") if p.strip()]
        if len(parts) < 2:
            continue
        when = parts[0]
        subject = parts[1]
        org = parts[2] if len(parts) > 2 else ""
        subject = re.sub(r"\s+\[ZOOM\]$", "", subject).strip()
        subject = re.sub(r"\s+ID:\s*.+$", "", subject).strip()
        org = re.sub(r"^Organizer:\s*", "", org, flags=re.I).strip()
        blob_parts = []
        if subject:
            blob_parts.append(subject)
        if org:
            blob_parts.append(org)
        if when:
            blob_parts.append(f"when: {when}")
        blob = "; ".join(blob_parts)
        blob = _shorten(blob, 150)
        if blob and blob not in seen:
            seen.append(blob)
        count += 1
    return seen[:max_people], count


def _fast_work_items_snapshot(text: str) -> str | None:
    """Return a deterministic work queue snapshot from local WorkOS artifacts."""
    lowered = str(text or "").lower()
    if not re.search(r"\b(work\s*items?|work\s+queue|action\s+queue|tasks?|to.?dos?|priorit|focus|on\s+my\s+plate)\b", lowered):
        return None

    root = Path(__file__).resolve().parents[1]
    lines = ["Work items I can see locally right now:"]

    include_calendar = bool(re.search(r"\b(today|day|focus|priorit|agenda|schedule|calendar)\b", lowered))
    if include_calendar:
        try:
            from tools.outlook_calendar_sqlite import sqlite_today_agenda

            calendar = sqlite_today_agenda()
            brief = _calendar_brief(str(calendar), max_events=3)
            if brief:
                lines.append("")
                lines.extend(brief)
        except Exception:
            pass

    exec_items, active_count = _active_exec_items(root)
    if active_count:
        lines.append(f"\nWorkOS action queue: {active_count} active item(s); showing top {len(exec_items)}.")
        lines.extend(_format_exec_item(item) for item in exec_items)

    ticket_items = _ticket_priority_items(root)
    if ticket_items:
        lines.append(f"\nSalesforce ticket queue: showing top {len(ticket_items)}.")
        lines.extend(_format_ticket_item(item) for item in ticket_items)

    admin_items, admin_count = _admin_ai_items(root)
    if admin_count:
        lines.append(f"\nAdmin AI continuation backlog: {admin_count} pending item(s); showing top {len(admin_items)}.")
        lines.extend(_format_admin_item(item) for item in admin_items)

    intel = _workos_intel_lines(root)
    if intel:
        lines.append("\nLatest WorkOS intel:")
        lines.extend(f"- {line}" for line in intel)

    if len(lines) == 1:
        return "Work items: I do not see active local WorkOS/Salesforce queue files right now."

    if exec_items:
        top = _shorten(
            _work_item_text(
                exec_items[0].get("suggested_action")
                or exec_items[0].get("action")
                or exec_items[0].get("subject")
                or exec_items[0].get("preview")
                or "the first WorkOS action item"
            ),
            120,
        )
    elif ticket_items:
        top = _shorten(ticket_items[0].get("ticket") or ticket_items[0].get("subject") or "the top Salesforce ticket", 120)
    elif admin_items:
        top = _shorten(admin_items[0].get("title") or admin_items[0].get("summary") or "the top Admin AI item", 120)
    else:
        top = "the first item above"
    lines.append(f"\nTop action: start with {top}.")
    return "\n".join(lines).strip()


def _fast_day_snapshot(text: str) -> str | None:
    """Return a deterministic calendar/priority brief without slow tool fan-out."""
    if is_basic_current_question(text):
        return None
    lowered = str(text or "").lower()
    if re.search(r"\bweather|temperature|forecast\b", lowered):
        return None
    weekday_match = None
    if "monday.com" not in lowered and "monday board" not in lowered and "monday boards" not in lowered:
        weekday_match = re.search(r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", lowered)
    if not re.search(r"\b(day|today|tomorrow|week|calendar|agenda|schedule|meetings?|calls?|workday|work\s+day)\b", lowered) and not weekday_match:
        return None
    if not re.search(r"\b(look|looking|calendar|agenda|schedule|meeting|meetings|calls?|today|tomorrow|week|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", lowered):
        return None

    root = Path(__file__).resolve().parents[1]
    is_week = bool(re.search(r"\b(this\s+week|next\s+week|my\s+week|the\s+week|week)\b", lowered))
    weekday_name = weekday_match.group(1) if weekday_match else ""
    lines = ["Week snapshot:" if is_week else f"{weekday_name.title()} snapshot:" if weekday_name else "Day snapshot:"]

    try:
        from tools.outlook_calendar_sqlite import (
            LOCAL_TZ,
            _format_event,
            _get_events_for_range,
            sqlite_today_agenda,
            sqlite_tomorrow_events,
            sqlite_upcoming_events,
        )

        if is_week:
            calendar = sqlite_upcoming_events(7)
        elif weekday_name:
            now = datetime.now(LOCAL_TZ)
            delta_days = (_WEEKDAY_INDEX[weekday_name] - now.weekday()) % 7
            target = now + timedelta(days=delta_days)
            start = target.replace(hour=0, minute=0, second=0, microsecond=0)
            end = start + timedelta(days=1)
            events = _get_events_for_range(start, end)
            if events:
                rows = [f"{weekday_name.title()} ({start.strftime('%A, %B %-d')}) - {len(events)} event(s):"]
                rows.extend(_format_event(ev) for ev in events)
                calendar = "\n".join(rows)
            else:
                calendar = f"No events on {weekday_name.title()} ({start.strftime('%A, %B %-d')})."
        elif "tomorrow" in lowered:
            calendar = sqlite_tomorrow_events()
        else:
            calendar = sqlite_today_agenda()
        if calendar:
            lines.append("")
            lines.extend(_calendar_brief(str(calendar), max_events=6 if is_week else 4))
    except Exception as exc:
        lines.append(f"\nCalendar:\n- unavailable locally ({exc})")

    top_ticket = ""
    try:
        priority_path = root / "data" / "ticket_priorities.json"
        if priority_path.exists():
            payload = json.loads(priority_path.read_text())
            tickets = payload.get("top_20") or []
            if tickets:
                lines.append("\nTop SF:")
                for item in tickets[:3]:
                    ticket = item.get("ticket") or "ticket"
                    priority = item.get("priority") or ""
                    subject = str(item.get("subject") or "").strip()
                    if not top_ticket:
                        top_ticket = ticket
                    detail = f"{ticket} [{priority}] {subject}".strip()
                    lines.append(f"- {_shorten(detail, 100)}")
    except Exception:
        pass

    try:
        draft_path = root / "data" / "draft_queue.json"
        if draft_path.exists():
            drafts = json.loads(draft_path.read_text())
            if isinstance(drafts, list) and drafts:
                lines.append(f"\nDrafts: {len(drafts)} waiting approval.")
    except Exception:
        pass

    if len(lines) == 1:
        return None
    target = top_ticket or "the highest-priority item"
    if is_week:
        lines.append(f"\nFocus: plan around the fixed calendar blocks, then clear {target} before lower-priority admin work.")
    else:
        lines.append(f"\nFocus: protect calendar blocks, then clear {target}.")
    return "\n".join(lines).strip()


def _fast_week_snapshot(text: str) -> str | None:
    """No deterministic weekly snapshot; let the model synthesize from context."""
    return None


async def handle_compound(user_id: str, text: str, tool_registry, frontend: str = "") -> str | None:
    """Run multiple local tools in parallel and synthesize with Qwen3.
    Returns formatted response or None if compound handling fails."""

    fast_week_snapshot = _fast_week_snapshot(text)
    if fast_week_snapshot:
        return fast_week_snapshot

    fast_work_snapshot = _fast_work_items_snapshot(text)
    if fast_work_snapshot:
        return fast_work_snapshot

    fast_snapshot = _fast_day_snapshot(text)
    if fast_snapshot:
        return fast_snapshot

    tools = _get_tool_set(user_id)
    log.info(f"[{user_id}] Compound query — running {len(tools)} local tools in parallel")

    # Run all tools concurrently
    async def _run_tool(tool_name, tool_input, label):
        try:
            result = await asyncio.wait_for(
                tool_registry.execute(tool_name, tool_input),
                timeout=30,
            )
            return label, str(result)[:2000]  # Cap each result to save context
        except asyncio.TimeoutError:
            log.warning(f"Compound tool {tool_name} timed out")
            return label, "(timed out)"
        except Exception as e:
            log.warning(f"Compound tool {tool_name} failed: {e}")
            return label, "(unavailable)"

    tasks = [_run_tool(name, inp, label) for name, inp, label in tools]
    results = await asyncio.gather(*tasks)

    # Build context for Qwen3
    context_parts = []
    try:
        from core.context_brain import get_now_snapshot, get_today_snapshot

        now_snap = get_now_snapshot(user_id)
        today_snap = get_today_snapshot(user_id)
        if now_snap:
            context_parts.append(f"=== Live now snapshot ===\n{json.dumps(now_snap, default=str)[:3500]}")
        if today_snap:
            context_parts.append(f"=== Live today snapshot ===\n{json.dumps(today_snap, default=str)[:3500]}")
    except Exception as exc:
        log.debug("[%s] context brain snapshot unavailable: %s", user_id, exc)

    for label, data in results:
        if data and data != "(timed out)" and data != "(unavailable)":
            context_parts.append(f"=== {label} ===\n{data}")

    if not context_parts:
        log.warning(f"[{user_id}] All compound tools failed")
        return None

    context = "\n\n".join(context_parts)

    # Synthesize with Qwen3 — use chat() for better instruction following.
    # If the local model is unavailable or returns junk, still return the real
    # deterministic data. Broad "what's pressing" questions must never fall
    # through to a generic model failure.
    system = """You are Operator's chief of staff and executive assistant. Respond naturally, not as a template. 
Use the live context to synthesize across all available work sources. 
Do not ask clarifying questions. Do not ask which systems to check. 
If some sources are silent, mention that briefly and continue.
Prefer meeting names, people, blockers, decisions, action items, and anything notable from the week.
Do not invent events or claim certainty where the data is missing."""

    user_msg = f"""The user asked: "{text}"

Here is REAL data from their systems:

{context}

Respond as a natural work-week catch-up. 
Lead with the most important things they missed.
Include notable meetings, changes, blockers, and follow-ups.
If there are call notes or meeting notes available, point that out.
Do not use a rigid numbered template."""

    log.info(f"[{user_id}] Synthesizing {len(context_parts)} data sources with Qwen3 ({len(context)} chars)")

    # Use chat() with /no_think to suppress Qwen3 reasoning tokens
    result = ""
    for attempt, max_tok in enumerate([4096, 8192], 1):
        try:
            result = await local_client.chat(
                messages=[{"role": "user", "content": user_msg}],
                system=system,
                max_tokens=max_tok,
                num_ctx=32768,
            )
        except Exception as exc:
            log.warning(f"[{user_id}] Qwen3 synthesis unavailable on attempt {attempt}: {exc}")
            result = ""
            break
        # Handle tuple return from chat() when tools are involved
        if isinstance(result, tuple):
            result = result[0]
        # Strip <think> blocks — Qwen3 sometimes still includes them
        if result:
            result = re.sub(r'<think>.*?</think>', '', result, flags=re.DOTALL).strip()
            if '</think>' in result:
                result = result.split('</think>')[-1].strip()
        if result and len(result.strip()) > 20:
            break
        log.warning(f"[{user_id}] Qwen3 synthesis attempt {attempt} returned empty/short (max_tokens={max_tok})")

    if not result or len(result.strip()) < 20:
        log.warning(f"[{user_id}] Qwen3 synthesis failed after retries; returning deterministic fallback")
        return _fallback_compound_brief(text, results)

    # Strip any hallucinated tool calls
    result = re.sub(r'\[/?tool_use\]', '', result)
    result = re.sub(r'\[tool_call\].*?\[/tool_call\]', '', result, flags=re.DOTALL)

    # Log savings (displaces a Sonnet compound query)
    from core.llm_router import log_savings
    log_savings(text, result, "sonnet", f"{frontend}:local_compound", local_model=LOCAL_MODEL)

    return result.strip()


def _interesting_lines(data: str, max_lines: int = 4) -> list[str]:
    """Extract compact, user-facing lines from one source blob."""
    lines: list[str] = []
    for raw in str(data or "").splitlines():
        line = re.sub(r"\s+", " ", raw).strip()
        if not line:
            continue
        low = line.lower()
        if low in {"{}", "[]"} or low.startswith(("error: unknown tool", "(timed out", "(unavailable")):
            continue
        if line.startswith(("{", "[")):
            try:
                parsed = json.loads(line)
            except Exception:
                parsed = None
            if isinstance(parsed, dict):
                if parsed.get("error"):
                    continue
                if set(parsed.keys()).issubset({"status", "in_meeting", "window_count", "windows"}):
                    continue
            elif parsed is not None:
                continue
        if len(line) > 220:
            line = line[:217].rstrip() + "..."
        lines.append(line)
        if len(lines) >= max_lines:
            break
    return lines


def _fallback_compound_brief(text: str, results: list[tuple[str, str]]) -> str | None:
    sections: list[tuple[str, list[str]]] = []
    for label, data in results:
        lines = _interesting_lines(data)
        if lines:
            sections.append((label, lines))
    if not sections:
        return None

    prompt = str(text or "").strip()
    lines = ["Here's what I can see locally right now:"]
    lowered = prompt.lower()
    if any(word in lowered for word in ("pressing", "urgent", "priority", "focus", "start", "attention")):
        lines[0] = "Pressing items from local context:"
    elif any(word in lowered for word in ("look", "day", "monday", "work")):
        lines[0] = "Your work/life snapshot from local context:"

    for label, items in sections[:7]:
        lines.append(f"\n{label}:")
        for item in items:
            lines.append(f"- {item}")
    lines.append("\nTop action: review the calendar and highest-priority ticket/email above first.")
    return "\n".join(lines).strip()
