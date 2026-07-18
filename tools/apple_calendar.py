"""Apple Calendar tool — reads from the working Outlook SQLite backend.

AppleScript calendar access is broken on this machine (Outlook for Mac exposes
no usable AppleScript dictionary), which caused the gateway "bridge/refresh
failure" stalls. Reads now go through tools.outlook_calendar_sqlite (fast, the
real data source); AppleScript is kept only as a fallback and for event writes.
"""

import asyncio
import subprocess
from tools.base import BaseTool, make_tool_def
from tools import outlook_calendar_sqlite as _sql


def _ensure_calendar_backgrounded() -> None:
    # `tell application "Calendar"` auto-launches the GUI into the foreground when
    # it isn't already running — which pops Calendar open on every scan (violates
    # the headless/never-foreground rail). Pre-launch it hidden + backgrounded so
    # the subsequent `tell` (no `activate`) never steals focus. Best-effort.
    try:
        subprocess.run(["open", "-gj", "-a", "Calendar"], capture_output=True, timeout=10)
    except Exception:
        pass


def _osascript(script: str) -> str:
    _ensure_calendar_backgrounded()
    result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(f"AppleScript error: {result.stderr.strip()}")
    return result.stdout.strip()


class AppleCalendarTool(BaseTool):
    name = "apple_calendar"
    description = "Apple Calendar — view, create, search events via AppleScript"

    def get_definitions(self) -> list:
        return [
            make_tool_def("calendar_today_agenda", "Get today's calendar events.",
                          {}, []),
            make_tool_def("calendar_upcoming", "Get upcoming events for the next N days.",
                          {"days": {"type": "integer", "description": "Number of days ahead (default 7)"}},
                          []),
            make_tool_def("calendar_create_event", "Create a calendar event.",
                          {"title": {"type": "string"},
                           "start_date": {"type": "string", "description": "Start date/time, e.g. 'April 14, 2026 at 2:00 PM'"},
                           "end_date": {"type": "string", "description": "End date/time"},
                           "calendar_name": {"type": "string", "description": "Calendar name (optional, uses default)"},
                           "notes": {"type": "string", "description": "Event notes (optional)"},
                           "location": {"type": "string", "description": "Event location (optional)"}},
                          ["title", "start_date", "end_date"]),
            make_tool_def("calendar_search", "Search calendar events by title.",
                          {"query": {"type": "string"}, "days_ahead": {"type": "integer", "description": "How many days ahead to search (default 30)"}},
                          ["query"]),
            make_tool_def("calendar_list_calendars", "List all available calendars.",
                          {}, []),
        ]

    async def handle(self, tool_name: str, tool_input: dict) -> str:
        loop = asyncio.get_event_loop()

        if tool_name == "calendar_today_agenda":
            try:
                return await loop.run_in_executor(None, _sql.sqlite_today_agenda)
            except Exception:
                pass
            script = '''
                tell application "Calendar"
                    set today to current date
                    set todayStart to today - (time of today)
                    set todayEnd to todayStart + (1 * days)
                    set output to ""
                    repeat with c in calendars
                        set evts to (every event of c whose start date >= todayStart and start date < todayEnd)
                        repeat with e in evts
                            set output to output & (start date of e as string) & " - " & (end date of e as string) & " | " & (summary of e) & " | " & (name of c) & linefeed
                        end repeat
                    end repeat
                    if output = "" then return "No events today."
                    return output
                end tell'''
            return await loop.run_in_executor(None, _osascript, script)

        elif tool_name == "calendar_upcoming":
            days = tool_input.get("days", 7)
            try:
                return await loop.run_in_executor(None, _sql.sqlite_upcoming_events, days)
            except Exception:
                pass
            script = f'''
                tell application "Calendar"
                    set today to current date
                    set todayStart to today - (time of today)
                    set futureEnd to todayStart + ({days} * days)
                    set output to ""
                    repeat with c in calendars
                        set evts to (every event of c whose start date >= todayStart and start date < futureEnd)
                        repeat with e in evts
                            set output to output & (start date of e as string) & " - " & (end date of e as string) & " | " & (summary of e) & " | " & (name of c) & linefeed
                        end repeat
                    end repeat
                    if output = "" then return "No upcoming events."
                    return output
                end tell'''
            return await loop.run_in_executor(None, _osascript, script)

        elif tool_name == "calendar_create_event":
            title = tool_input["title"].replace('"', '\\"')
            start = tool_input["start_date"].replace('"', '\\"')
            end = tool_input["end_date"].replace('"', '\\"')
            cal_name = tool_input.get("calendar_name", "")
            notes = tool_input.get("notes", "").replace('"', '\\"')
            location = tool_input.get("location", "").replace('"', '\\"')

            if cal_name:
                cal_ref = f'calendar "{cal_name}"'
            else:
                cal_ref = "first calendar"

            props = f'summary:"{title}", start date:(date "{start}"), end date:(date "{end}")'
            if notes:
                props += f', description:"{notes}"'
            if location:
                props += f', location:"{location}"'

            script = f'''
                tell application "Calendar"
                    tell {cal_ref}
                        set newEvent to make new event with properties {{{props}}}
                        if ((end date of newEvent) - (start date of newEvent)) > 1500 then
                            set end date of newEvent to (start date of newEvent) + 1500
                        end if
                    end tell
                end tell
                return "Event created: {title}"'''
            return await loop.run_in_executor(None, _osascript, script)

        elif tool_name == "calendar_search":
            query = tool_input["query"].replace('"', '\\"')
            days = tool_input.get("days_ahead", 30)
            try:
                return await loop.run_in_executor(None, _sql.sqlite_search_events, tool_input["query"], days)
            except Exception:
                pass
            script = f'''
                tell application "Calendar"
                    set today to current date
                    set todayStart to today - (time of today)
                    set futureEnd to todayStart + ({days} * days)
                    set output to ""
                    repeat with c in calendars
                        set evts to (every event of c whose summary contains "{query}" and start date >= todayStart and start date < futureEnd)
                        repeat with e in evts
                            set output to output & (start date of e as string) & " - " & (end date of e as string) & " | " & (summary of e) & " | " & (name of c) & linefeed
                        end repeat
                    end repeat
                    if output = "" then return "No matching events found."
                    return output
                end tell'''
            return await loop.run_in_executor(None, _osascript, script)

        elif tool_name == "calendar_list_calendars":
            script = '''
                tell application "Calendar"
                    set output to ""
                    repeat with c in calendars
                        set output to output & (name of c) & " (" & (description of c) & ")" & linefeed
                    end repeat
                    return output
                end tell'''
            return await loop.run_in_executor(None, _osascript, script)

        else:
            return f"Unknown calendar tool: {tool_name}"
