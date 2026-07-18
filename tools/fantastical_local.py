"""Fantastical / Calendar — unified calendar access across all accounts.

Reads directly from Calendar.sqlitedb (EventKit store) for both Operator and
Operator2.  Event creation uses Calendar.app AppleScript.  No Fantastical.app
required at runtime — the name is historical.

Ported from fantastical_mcp_server.py (Mac Mini MCP server) to BaseTool.
"""

import asyncio
import json
import logging
import os
import plistlib
import sqlite3
import subprocess
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from tools.base import BaseTool, make_tool_def

logger = logging.getLogger("fantastical-local")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
CALDB = (
    Path.home()
    / "Library/Group Containers/group.com.apple.calendar"
    / "Calendar.sqlitedb"
)
# Core Data epoch: seconds since 2001-01-01
CD_EPOCH = datetime(2001, 1, 1)

# Fallback store IDs — used only when Calendar.sqlitedb is unavailable
_NICOLE_STORE_IDS_FALLBACK = (21, 24, 28, 29)
_ALEX_STORE_IDS_FALLBACK = (6, 9, 10, 11, 15, 17, 18)

# Calendar names shared between both people
SHARED_NAMES = {"Family", "Home", "US Holidays", "Holidays in United States"}

# Specifier day-of-week abbreviation -> Python weekday (0=Mon ... 6=Sun)
_DOW_MAP = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}

# ---------------------------------------------------------------------------
# Store ID resolution (dynamic)
# ---------------------------------------------------------------------------

def _resolve_store_ids():
    # type: () -> Tuple[Tuple, Tuple]
    """Dynamically resolve Operator2's and Operator's Calendar.sqlitedb store IDs."""
    if not CALDB.exists():
        return _NICOLE_STORE_IDS_FALLBACK, _ALEX_STORE_IDS_FALLBACK
    try:
        conn = sqlite3.connect(str(CALDB))
        rows = conn.execute(
            "SELECT ROWID, name, owner_name FROM Store WHERE type IN (1, 2)"
        ).fetchall()
        conn.close()
    except Exception:
        return _NICOLE_STORE_IDS_FALLBACK, _ALEX_STORE_IDS_FALLBACK

    nicole_ids = []  # type: List[int]
    alex_ids = []  # type: List[int]
    system_stores = {1, 2, 3, 5}
    for rowid, name, owner_name in rows:
        if rowid in system_stores:
            continue
        name_lc = (name or "").lower()
        owner_lc = (owner_name or "").lower()
        if "Operator2" in name_lc or "Operator2" in owner_lc:
            nicole_ids.append(rowid)
        else:
            alex_ids.append(rowid)

    return (
        tuple(nicole_ids) if nicole_ids else _NICOLE_STORE_IDS_FALLBACK,
        tuple(alex_ids) if alex_ids else _ALEX_STORE_IDS_FALLBACK,
    )


NICOLE_STORE_IDS, ALEX_STORE_IDS = _resolve_store_ids()
logger.info("Store IDs resolved -- Operator2: %s, Operator: %s", NICOLE_STORE_IDS, ALEX_STORE_IDS)

# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

def _run_script_file(script, timeout=30):
    # type: (str, int) -> str
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".scpt", delete=False)
    tmp.write(script)
    tmp.close()
    try:
        result = subprocess.run(
            ["osascript", tmp.name],
            capture_output=True, text=True, timeout=timeout,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip()[:300])
        return result.stdout.strip()
    finally:
        os.unlink(tmp.name)


# ---------------------------------------------------------------------------
# Calendar.sqlitedb queries
# ---------------------------------------------------------------------------

def _events_from_caldb(store_ids, start_dt, end_dt, owner):
    # type: (Tuple, datetime, datetime, str) -> List[Dict]
    """Query non-recurring events from Calendar.sqlitedb for given store IDs."""
    if not CALDB.exists():
        return []
    start_cd = (start_dt - CD_EPOCH).total_seconds()
    end_cd = (end_dt - CD_EPOCH).total_seconds()
    ph = ",".join("?" * len(store_ids))
    try:
        conn = sqlite3.connect(str(CALDB))
        rows = conn.execute(
            f"""
            SELECT ci.summary, ci.start_date, ci.end_date, ci.all_day,
                   c.title, s.name,
                   l.title as location
            FROM CalendarItem ci
            JOIN Calendar c ON ci.calendar_id = c.ROWID
            JOIN Store s    ON c.store_id = s.ROWID
            LEFT JOIN Location l ON l.ROWID = ci.location_id
            WHERE s.ROWID IN ({ph})
              AND ci.start_date BETWEEN ? AND ?
            ORDER BY ci.start_date
            """,
            list(store_ids) + [start_cd, end_cd],
        ).fetchall()
        conn.close()
    except Exception as e:
        logger.error("caldb query failed (%s): %s", owner, e)
        return []

    events = []  # type: List[Dict]
    seen = set()  # type: Set[Tuple]
    for title, s_cd, e_cd, all_day, cal_title, store_name, location in rows:
        if not title or not s_cd:
            continue
        start = CD_EPOCH + timedelta(seconds=s_cd)
        end = CD_EPOCH + timedelta(seconds=e_cd) if e_cd else start + timedelta(hours=1)
        key = (title, start.strftime("%Y-%m-%d %H:%M"))
        if key in seen:
            continue
        seen.add(key)
        events.append({
            "title": title,
            "calendar": cal_title,
            "source": "Calendar.app (%s)" % owner.capitalize(),
            "owner": owner,
            "start": start.strftime("%Y-%m-%d %H:%M"),
            "end": end.strftime("%Y-%m-%d %H:%M"),
            "location": location or "",
            "all_day": bool(all_day),
        })
    return events


def _get_recurring_events(store_ids, start_dt, end_dt, owner):
    # type: (Tuple, datetime, datetime, str) -> List[Dict]
    """Expand recurring weekly events for any store IDs into the query window."""
    if not CALDB.exists():
        return []
    try:
        conn = sqlite3.connect(str(CALDB))
        ph = ",".join("?" * len(store_ids))
        rows = conn.execute(
            f"""
            SELECT ci.summary, ci.start_date, ci.end_date, ci.all_day,
                   c.title, s.name,
                   r.frequency, r.interval, r.specifier, r.end_date
            FROM CalendarItem ci
            JOIN Calendar c  ON ci.calendar_id = c.ROWID
            JOIN Store s     ON c.store_id = s.ROWID
            JOIN Recurrence r ON r.owner_id = ci.ROWID
            WHERE s.ROWID IN ({ph})
              AND r.frequency = 2
            ORDER BY ci.summary, ci.start_date DESC
            """,
            list(store_ids),
        ).fetchall()
        conn.close()
    except Exception as e:
        logger.error("Recurring query failed (%s): %s", owner, e)
        return []

    series = {}  # type: Dict[Tuple, Dict]
    for title, s_cd, e_cd, all_day, cal_title, store_name, freq, interval, specifier, recur_end_cd in rows:
        if not title or not s_cd or not specifier:
            continue
        anchor = CD_EPOCH + timedelta(seconds=s_cd)
        duration = timedelta(seconds=max(e_cd - s_cd, 0)) if e_cd else timedelta(hours=1)
        recur_end = CD_EPOCH + timedelta(seconds=recur_end_cd) if recur_end_cd else None
        skey = (title, specifier, interval, cal_title, store_name)
        if skey not in series or anchor > series[skey]["anchor"]:
            series[skey] = {
                "title": title,
                "anchor": anchor,
                "duration": duration,
                "all_day": bool(all_day),
                "cal_title": cal_title,
                "store_name": store_name,
                "interval": interval,
                "specifier": specifier,
                "recur_end": recur_end,
            }

    events = []  # type: List[Dict]
    for skey, s in series.items():
        step = timedelta(weeks=s["interval"])
        anchor = s["anchor"]
        recur_end = s["recur_end"]
        if recur_end and recur_end < start_dt:
            continue
        occurrence = anchor
        if occurrence > end_dt:
            while occurrence > end_dt:
                occurrence -= step
        elif occurrence < start_dt:
            while occurrence < start_dt:
                occurrence += step
            occurrence -= step
        while occurrence <= end_dt:
            if occurrence >= start_dt:
                if recur_end and occurrence > recur_end:
                    break
                events.append({
                    "title": s["title"],
                    "calendar": s["cal_title"],
                    "source": "Calendar.app (%s, recurring)" % owner.capitalize(),
                    "owner": owner,
                    "start": occurrence.strftime("%Y-%m-%d %H:%M"),
                    "end": (occurrence + s["duration"]).strftime("%Y-%m-%d %H:%M"),
                    "location": "",
                    "all_day": s["all_day"],
                })
            occurrence += step
    return events


# ---------------------------------------------------------------------------
# Per-owner event fetchers
# ---------------------------------------------------------------------------

def _get_nicole_events(start_dt, end_dt, max_events=200):
    # type: (datetime, datetime, int) -> List[Dict]
    events = _events_from_caldb(NICOLE_STORE_IDS, start_dt, end_dt, "wife")
    try:
        recur = _get_recurring_events(NICOLE_STORE_IDS, start_dt, end_dt, "wife")
        existing = {(e["title"], e["start"]) for e in events}
        for evt in recur:
            key = (evt["title"], evt["start"])
            if key not in existing:
                events.append(evt)
                existing.add(key)
    except Exception as e:
        logger.warning("Operator2 recurring supplement failed: %s", e)
    events.sort(key=lambda x: x.get("start", ""))
    return events[:max_events]


def _get_alex_events(start_dt, end_dt):
    # type: (datetime, datetime) -> List[Dict]
    events = _events_from_caldb(ALEX_STORE_IDS, start_dt, end_dt, "Operator")
    recur = _get_recurring_events(ALEX_STORE_IDS, start_dt, end_dt, "Operator")
    existing = {(e["title"], e["start"]) for e in events}
    for evt in recur:
        key = (evt["title"], evt["start"])
        if key not in existing:
            events.append(evt)
            existing.add(key)
    events.sort(key=lambda x: x.get("start", ""))
    return events


# ---------------------------------------------------------------------------
# Synchronous tool implementations (run in executor from async handle)
# ---------------------------------------------------------------------------

def _do_cal_today(owner):
    # type: (str) -> str
    today = datetime.now().strftime("%Y-%m-%d")
    return _do_cal_get_events(today, today, owner, 100)


def _do_cal_get_events(start_date, end_date, owner, max_events):
    # type: (str, str, str, int) -> str
    now = datetime.now()
    if not start_date:
        start_date = now.strftime("%Y-%m-%d")
    if not end_date:
        end_date = start_date
    try:
        start_dt = datetime.strptime(start_date, "%Y-%m-%d")
        end_dt = datetime.strptime(end_date, "%Y-%m-%d").replace(hour=23, minute=59, second=59)
    except ValueError as e:
        return json.dumps({"error": "Invalid date: %s" % e})

    events = []  # type: List[Dict]
    if owner in ("Operator", "all"):
        events.extend(_get_alex_events(start_dt, end_dt))
    if owner in ("wife", "all"):
        events.extend(_get_nicole_events(start_dt, end_dt, max_events=max_events))

    events.sort(key=lambda x: x.get("start", ""))
    events = events[:max_events]

    return json.dumps({
        "start_date": start_date,
        "end_date": end_date,
        "owner_filter": owner,
        "count": len(events),
        "events": events,
    }, indent=2)


def _do_cal_add_event(title, start, end, calendar_name, location, notes, all_day, for_owner):
    # type: (str, str, str, str, str, str, bool, str) -> str
    try:
        if all_day:
            start_dt = datetime.fromisoformat(start[:10])
            end_dt = datetime.fromisoformat(end[:10])
            start_as = 'date "%s"' % start_dt.strftime("%B %d, %Y")
            end_as = 'date "%s"' % end_dt.strftime("%B %d, %Y")
        else:
            start_dt = datetime.fromisoformat(start)
            end_dt = datetime.fromisoformat(end)
            start_as = 'date "%s"' % start_dt.strftime("%B %d, %Y %I:%M:%S %p")
            end_as = 'date "%s"' % end_dt.strftime("%B %d, %Y %I:%M:%S %p")
    except ValueError as e:
        return json.dumps({"error": "Invalid date format: %s" % e})

    props = [
        'summary:"%s"' % title,
        "start date:%s" % start_as,
        "end date:%s" % end_as,
    ]
    if all_day:
        props.append("allday event:true")
    if location:
        safe_loc = location.replace('"', '\\"')
        props.append('location:"%s"' % safe_loc)
    if notes:
        safe_notes = notes.replace('"', '\\"')
        props.append('description:"%s"' % safe_notes)

    props_str = ", ".join(props)

    if calendar_name:
        safe_cal = calendar_name.replace('"', '\\"')
        cal_target = 'tell calendar "%s"' % safe_cal
    else:
        cal_target = "tell first calendar"

    script = """
tell application "Calendar"
    %s
        set newEvent to make new event at end with properties {%s}
        set uid to uid of newEvent
    end tell
    reload calendars
    return uid
end tell
""" % (cal_target, props_str)

    try:
        try:
            subprocess.run(["open", "-gj", "-a", "Calendar"], capture_output=True, timeout=10)
        except Exception:
            pass
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode == 0:
            return json.dumps({
                "status": "created",
                "title": title,
                "start": start,
                "end": end,
                "calendar": calendar_name or "default",
                "uid": result.stdout.strip(),
                "for_owner": for_owner,
            }, indent=2)
        raise RuntimeError(result.stderr.strip())
    except Exception as e:
        return json.dumps({"error": "Calendar.app event creation failed: %s" % e})


def _do_cal_search(query, owner, days_back, days_forward):
    # type: (str, str, int, int) -> str
    now = datetime.now()
    start = (now - timedelta(days=days_back)).strftime("%Y-%m-%d")
    end = (now + timedelta(days=days_forward)).strftime("%Y-%m-%d")

    all_raw = _do_cal_get_events(start, end, owner, 500)
    try:
        data = json.loads(all_raw)
    except Exception:
        return all_raw
    if "error" in data:
        return all_raw

    q = query.lower()
    matches = [
        e for e in data.get("events", [])
        if q in e.get("title", "").lower() or q in e.get("location", "").lower()
    ]
    return json.dumps({
        "query": query,
        "owner_filter": owner,
        "count": len(matches),
        "events": matches,
    }, indent=2)


def _do_cal_list_cals():
    # type: () -> str
    alex_cals = []  # type: List[Dict]
    try:
        script = """
tell application "Calendar"
    set output to ""
    repeat with cal in every calendar
        set output to output & (name of cal) & linefeed
    end repeat
    return output
end tell
"""
        raw = _run_script_file(script, timeout=15)
        for line in raw.strip().splitlines():
            name = line.strip()
            if name:
                cal_owner = "shared" if name in SHARED_NAMES else "Operator"
                alex_cals.append({"name": name, "owner": cal_owner, "source": "Calendar.app"})
    except Exception as e:
        alex_cals = [{"error": "Calendar unavailable"}]

    return json.dumps({
        "alex_calendars": alex_cals,
        "nicole_store_ids": list(NICOLE_STORE_IDS),
        "note": "Operator2's calendars are read from Calendar.sqlitedb store IDs above.",
    }, indent=2)


def _do_cal_health():
    # type: () -> str
    try:
        subprocess.run(["open", "-gj", "-a", "Calendar"], capture_output=True, timeout=10)
    except Exception:
        pass
    calendar_ok = subprocess.run(
        ["osascript", "-e", 'tell application "Calendar" to return count of every calendar'],
        capture_output=True, text=True, timeout=10,
    ).returncode == 0

    caldb_ok = CALDB.exists()

    return json.dumps({
        "status": "ok",
        "calendar_app_responsive": calendar_ok,
        "caldb_accessible": caldb_ok,
        "alex_store_ids": list(ALEX_STORE_IDS),
        "nicole_store_ids": list(NICOLE_STORE_IDS),
    }, indent=2)


# ---------------------------------------------------------------------------
# BaseTool class
# ---------------------------------------------------------------------------

class FantasticalLocalTool(BaseTool):
    name = "fantastical_local"
    description = (
        "Unified calendar access across all accounts (Exchange, iCloud, Gmail). "
        "Reads Calendar.sqlitedb directly. Creates events via Calendar.app AppleScript."
    )

    def get_definitions(self):
        # type: () -> List[Dict]
        return [
            make_tool_def(
                "cal_today",
                "Get today's events, filtered by person.",
                {
                    "owner": {
                        "type": "string",
                        "description": "'Operator' | 'wife' | 'all' (default 'all')",
                    },
                },
                [],
            ),
            make_tool_def(
                "cal_get_events",
                "Get calendar events for a date range, filtered by person.",
                {
                    "start_date": {
                        "type": "string",
                        "description": "YYYY-MM-DD (default: today)",
                    },
                    "end_date": {
                        "type": "string",
                        "description": "YYYY-MM-DD (default: same as start_date)",
                    },
                    "owner": {
                        "type": "string",
                        "description": "'Operator' | 'wife' | 'all' (default 'all')",
                    },
                    "max_events": {
                        "type": "integer",
                        "description": "Max events to return (default 100)",
                    },
                },
                [],
            ),
            make_tool_def(
                "cal_add_event",
                "Create a calendar event in Apple Calendar.",
                {
                    "title": {
                        "type": "string",
                        "description": "Event title",
                    },
                    "start": {
                        "type": "string",
                        "description": "Start datetime ISO 8601 (e.g. '2026-04-15T14:00:00') or date for all-day",
                    },
                    "end": {
                        "type": "string",
                        "description": "End datetime ISO 8601 (e.g. '2026-04-15T15:00:00') or date for all-day",
                    },
                    "calendar_name": {
                        "type": "string",
                        "description": "Calendar name (e.g. 'Work', 'Family'). Uses default if empty.",
                    },
                    "location": {
                        "type": "string",
                        "description": "Optional location string",
                    },
                    "notes": {
                        "type": "string",
                        "description": "Optional notes/description",
                    },
                    "all_day": {
                        "type": "boolean",
                        "description": "True for an all-day event (default false)",
                    },
                    "for_owner": {
                        "type": "string",
                        "description": "'Operator' | 'wife' -- for logging only (default 'Operator')",
                    },
                },
                ["title", "start", "end"],
            ),
            make_tool_def(
                "cal_search",
                "Search events by keyword across calendars.",
                {
                    "query": {
                        "type": "string",
                        "description": "Keyword to search (title, location)",
                    },
                    "owner": {
                        "type": "string",
                        "description": "'Operator' | 'wife' | 'all' (default 'all')",
                    },
                    "days_back": {
                        "type": "integer",
                        "description": "Days back to search (default 30)",
                    },
                    "days_forward": {
                        "type": "integer",
                        "description": "Days forward to search (default 90)",
                    },
                },
                ["query"],
            ),
            make_tool_def(
                "cal_list_cals",
                "List all calendars with their ownership (Operator / wife / shared).",
                {},
                [],
            ),
            make_tool_def(
                "cal_health",
                "Check Calendar MCP server health.",
                {},
                [],
            ),
        ]

    async def handle(self, tool_name, tool_input):
        # type: (str, Dict) -> str
        loop = asyncio.get_event_loop()

        if tool_name == "cal_today":
            owner = tool_input.get("owner", "all")
            return await loop.run_in_executor(None, _do_cal_today, owner)

        elif tool_name == "cal_get_events":
            start_date = tool_input.get("start_date", "")
            end_date = tool_input.get("end_date", "")
            owner = tool_input.get("owner", "all")
            max_events = tool_input.get("max_events", 100)
            return await loop.run_in_executor(
                None, _do_cal_get_events, start_date, end_date, owner, max_events
            )

        elif tool_name == "cal_add_event":
            return await loop.run_in_executor(
                None,
                _do_cal_add_event,
                tool_input["title"],
                tool_input["start"],
                tool_input["end"],
                tool_input.get("calendar_name", ""),
                tool_input.get("location", ""),
                tool_input.get("notes", ""),
                tool_input.get("all_day", False),
                tool_input.get("for_owner", "Operator"),
            )

        elif tool_name == "cal_search":
            query = tool_input["query"]
            owner = tool_input.get("owner", "all")
            days_back = tool_input.get("days_back", 30)
            days_forward = tool_input.get("days_forward", 90)
            return await loop.run_in_executor(
                None, _do_cal_search, query, owner, days_back, days_forward
            )

        elif tool_name == "cal_list_cals":
            return await loop.run_in_executor(None, _do_cal_list_cals)

        elif tool_name == "cal_health":
            return await loop.run_in_executor(None, _do_cal_health)

        else:
            return "Unknown fantastical tool: %s" % tool_name
