#!/usr/bin/env python3.12
"""
find_times.py — suggest open meeting slots from Operator's OWN Outlook calendar.

NOTE: this reads only Operator's local Outlook calendar (and any calendars shared into
it). It does NOT see other attendees' free/busy — that needs Microsoft Graph
(getSchedule / findMeetingTimes) with a tenant app registration. See README note.

Usage:
  python3.12 find_times.py next-week [--mins 30] [--count 5] [--start 9] [--end 17]
  python3.12 find_times.py range 2026-06-08 2026-06-12 [--mins 30] ...
Outputs JSON list of free slots (local time) you can drop straight into a meeting draft.
"""
import sys, json, datetime
sys.path.insert(0, "/home/user/claude-stack")
sys.path.insert(0, "/home/user/claude-stack/tools")
import outlook_calendar_sqlite as cal

def _opt(args, name, default, cast=int):
    if name in args:
        return cast(args[args.index(name) + 1])
    return default

def free_slots(start_date, end_date, slot_mins=30, work_start=9, work_end=17,
               lunch=(12, 13), max_results=8):
    """Return open slots on business days within working hours, minus busy events."""
    results = []
    day = start_date
    while day <= end_date and len(results) < max_results:
        if day.weekday() < 5:  # Mon-Fri
            ws = datetime.datetime(day.year, day.month, day.day, work_start, 0)
            we = datetime.datetime(day.year, day.month, day.day, work_end, 0)
            events = cal._get_events_for_range(ws, we)
            def _local_naive(dt):
                if dt.tzinfo is not None:
                    dt = dt.astimezone().replace(tzinfo=None)
                return dt
            busy = []
            for e in events:
                busy.append((_local_naive(e["start_time"]), _local_naive(e["end_time"])))
            # lunch block
            busy.append((datetime.datetime(day.year, day.month, day.day, lunch[0], 0),
                         datetime.datetime(day.year, day.month, day.day, lunch[1], 0)))
            busy.sort()
            cursor = ws
            step = datetime.timedelta(minutes=slot_mins)
            while cursor + step <= we and len(results) < max_results:
                slot_end = cursor + step
                clash = any(cursor < b_end and slot_end > b_start for b_start, b_end in busy)
                if not clash:
                    results.append({
                        "day": cursor.strftime("%a %b %-d"),
                        "start": cursor.strftime("%Y-%m-%d %H:%M"),
                        "end": slot_end.strftime("%Y-%m-%d %H:%M"),
                        "label": cursor.strftime("%a %b %-d, %-I:%M %p") + " – " + slot_end.strftime("%-I:%M %p"),
                    })
                cursor = slot_end
        day += datetime.timedelta(days=1)
    return results

def next_week_range(today):
    # Monday of next week through Friday
    monday = today + datetime.timedelta(days=(7 - today.weekday()))
    return monday, monday + datetime.timedelta(days=4)

if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        print(__doc__); sys.exit(0)
    mins = _opt(args, "--mins", 30)
    count = _opt(args, "--count", 8)
    ws = _opt(args, "--start", 9)
    we = _opt(args, "--end", 17)
    today = datetime.date.today()
    if args[0] == "next-week":
        s, e = next_week_range(today)
    elif args[0] == "range":
        s = datetime.date.fromisoformat(args[1]); e = datetime.date.fromisoformat(args[2])
    else:
        print(__doc__); sys.exit(0)
    slots = free_slots(datetime.datetime(s.year, s.month, s.day),
                       datetime.datetime(e.year, e.month, e.day, 23, 59),
                       slot_mins=mins, work_start=ws, work_end=we, max_results=count)
    print(json.dumps({"window": f"{s} .. {e}", "slot_mins": mins, "free_slots": slots}, indent=2))
