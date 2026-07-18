#!/usr/bin/env python3
"""focus_blocker.py — plan "Focus" time blocks in the gaps between meetings.

Reads the rendered Zoom calendar digest (ZOOM_CALENDAR.md), finds free gaps on
work days inside working hours, and proposes Focus blocks. DRY-RUN by default:
it only PRINTS the plan. Actually creating events in Outlook is a separate,
confirmed step (see --emit-plan / outlook_create_event).

Digest line format:  "- YYYY-MM-DD HH:MM · Title · Nm"
Times are in the calendar's local display timezone; blocks stay in the same tz.
"""
import argparse
import re
import subprocess
from datetime import datetime, timedelta, date
try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None

DIGEST = "/home/user/.openclaw/workspace/CompanyA-local/digests/ZOOM_CALENDAR.md"

LINE_RE = re.compile(r"^- (\d{4}-\d{2}-\d{2}) (\d{2}):(\d{2}) · (.+?) · (\d+)m\s*$")


def parse_digest(path):
    events = []  # (start_dt, end_dt, title)
    with open(path) as f:
        for line in f:
            m = LINE_RE.match(line.rstrip("\n"))
            if not m:
                continue
            d, hh, mm, title, dur = m.groups()
            start = datetime.strptime(f"{d} {hh}:{mm}", "%Y-%m-%d %H:%M")
            events.append((start, start + timedelta(minutes=int(dur)), title.strip()))
    return events


def merge(intervals):
    """Merge overlapping (start,end) intervals."""
    intervals = sorted(intervals, key=lambda x: x[0])
    out = []
    for s, e in intervals:
        if out and s <= out[-1][1]:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return out


def plan(events, anchor, days, work_start, work_end, min_block, max_block, workdays_only):
    plans = []
    for offset in range(days):
        day = anchor + timedelta(days=offset)
        if workdays_only and day.weekday() >= 5:
            continue
        day_start = datetime.combine(day, datetime.min.time()).replace(hour=work_start)
        day_end = datetime.combine(day, datetime.min.time()).replace(hour=work_end)
        busy = merge([[max(s, day_start), min(e, day_end)]
                      for s, e, _ in events
                      if s.date() == day and e > day_start and s < day_end])
        cursor = day_start
        for bs, be in busy:
            if bs - cursor >= timedelta(minutes=min_block):
                _add_block(plans, cursor, bs, max_block)
            cursor = max(cursor, be)
        if day_end - cursor >= timedelta(minutes=min_block):
            _add_block(plans, cursor, day_end, max_block)
    return plans


def _add_block(plans, start, end, max_block):
    """Split a free gap into <= max_block chunks."""
    while end - start >= timedelta(minutes=1):
        chunk_end = min(end, start + timedelta(minutes=max_block))
        plans.append((start, chunk_end))
        start = chunk_end


def fmt_applescript_date(dt):
    # AppleScript `date "..."` accepts e.g. "July 16, 2026 9:00:00 AM"
    return dt.strftime("%B %-d, %Y %-I:%M:%S %p")


def digest_now_local():
    """Digest _Updated timestamp (UTC) converted to America/New_York local time."""
    with open(DIGEST) as f:
        m = re.search(r"_Updated (\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})", f.read())
    if not m:
        return None
    utc = datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S")
    if ZoneInfo:
        local = utc.replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo("America/New_York"))
        return local.replace(tzinfo=None)
    return utc - timedelta(hours=4)  # EDT fallback


def create_event(start, end, subject="Focus block"):
    script = (
        'tell application "Microsoft Outlook"\n'
        f'  make new calendar event with properties {{subject:"{subject}", '
        f'start time:date "{fmt_applescript_date(start)}", '
        f'end time:date "{fmt_applescript_date(end)}", '
        f'free busy status:busy, is private:true}}\n'
        'end tell\n'
        f'return "Event created: {subject} {start.isoformat()}"'
    )
    r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=45)
    if r.returncode != 0:
        return False, r.stderr.strip()
    return True, r.stdout.strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--anchor", default=None, help="YYYY-MM-DD; default = digest _Updated_ date")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--work-start", type=int, default=9)
    ap.add_argument("--work-end", type=int, default=18)
    ap.add_argument("--min-block", type=int, default=45, help="minutes; ignore gaps shorter than this")
    ap.add_argument("--max-block", type=int, default=120, help="minutes; split longer gaps")
    ap.add_argument("--all-days", action="store_true", help="include weekends")
    ap.add_argument("--emit-plan", action="store_true", help="print machine-readable plan for event creation")
    ap.add_argument("--skip-past", action="store_true", help="drop blocks starting before the digest's update time (local)")
    ap.add_argument("--create", action="store_true", help="ACTUALLY write the blocks to Outlook")
    args = ap.parse_args()

    events = parse_digest(DIGEST)
    if args.anchor:
        anchor = datetime.strptime(args.anchor, "%Y-%m-%d").date()
    else:
        with open(DIGEST) as f:
            txt = f.read()
        m = re.search(r"_Updated (\d{4}-\d{2}-\d{2})", txt)
        anchor = datetime.strptime(m.group(1), "%Y-%m-%d").date() if m else date.today()

    plans = plan(events, anchor, args.days, args.work_start, args.work_end,
                 args.min_block, args.max_block, not args.all_days)

    if args.skip_past:
        now_local = digest_now_local()
        if now_local:
            plans = [(s, e) for s, e in plans if s >= now_local]

    if args.create:
        print(f"# Creating {len(plans)} focus block(s) in Outlook...")
        ok = 0
        for s, e in plans:
            success, msg = create_event(s, e)
            status = "OK " if success else "ERR"
            print(f"  [{status}] {s.strftime('%a %Y-%m-%d %H:%M')}-{e.strftime('%H:%M')}  {msg}")
            ok += success
        print(f"# Done: {ok}/{len(plans)} created")
        return

    if args.emit_plan:
        for s, e in plans:
            print(f"{s.isoformat()}|{e.isoformat()}|Focus block")
        return

    print(f"# Focus block plan — anchor {anchor}, next {args.days} days")
    print(f"# work hours {args.work_start:02d}:00–{args.work_end:02d}:00, "
          f"min gap {args.min_block}m, max block {args.max_block}m, "
          f"{'all days' if args.all_days else 'weekdays only'}")
    print(f"# {len(plans)} focus block(s) proposed (DRY-RUN — nothing written)\n")
    cur = None
    total = 0
    for s, e in plans:
        if s.date() != cur:
            cur = s.date()
            print(f"\n{s.strftime('%A %Y-%m-%d')}")
        mins = int((e - s).total_seconds() // 60)
        total += mins
        print(f"  {s.strftime('%H:%M')}–{e.strftime('%H:%M')}  ({mins}m)")
    print(f"\n# Total focus time: {total} min ({total/60:.1f} h)")


if __name__ == "__main__":
    main()
