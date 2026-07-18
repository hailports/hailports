"""Send window logic — time-based gating for all outreach channels.

B2B (BrandA Standard):
  - Primary:   Tue–Thu  08:00–10:00 ET  (morning ramp, highest reply rate)
  - Secondary: Tue–Thu  13:30–15:30 ET  (post-lunch, pre-afternoon meetings)
  - Allowed:   Mon/Fri  09:00–10:30 ET  (lower priority)
  - Weekend:   Sat      11:00–13:00 ET  (quiet inbox triage)
  - Weekend:   Sat      15:00–18:00 ET  (catch-up / owner-operator planning)
  - Weekend:   Sun      16:00–19:00 ET  (pre-week planning)

B2C (persona1 MLM drip — suburban mom persona):
  - Primary:   Mon–Fri  19:30–21:30 ET  (post-kids-bedtime scroll)
  - Weekend:   Sat–Sun  09:00–11:00 ET  (morning coffee / leisure)
  - Weekend:   Sat–Sun  15:00–18:00 ET  (creator/TikTok afternoon scroll)
  - Weekend:   Sat–Sun  19:30–21:30 ET  (post-dinner scroll)
  - Bonus:     Tue/Thu  12:00–13:00 ET  (lunch break scroll)
"""
from __future__ import annotations
from datetime import datetime, time, timedelta
import zoneinfo

ET = zoneinfo.ZoneInfo("America/New_York")

# (weekday 0=Mon, start_time, end_time, priority)
_MAROON_WINDOWS = [
    (1, time(8,  0), time(10, 0), "primary"),    # Tue AM
    (2, time(8,  0), time(10, 0), "primary"),    # Wed AM
    (3, time(8,  0), time(10, 0), "primary"),    # Thu AM
    (1, time(11, 0), time(13, 0), "allowed"),    # Tue midday
    (2, time(11, 0), time(13, 0), "allowed"),    # Wed midday
    (3, time(11, 0), time(13, 0), "allowed"),    # Thu midday
    (1, time(13,30), time(16, 0), "primary"),    # Tue PM (extended)
    (2, time(13,30), time(16, 0), "primary"),    # Wed PM (extended)
    (3, time(13,30), time(16, 0), "primary"),    # Thu PM (extended)
    (0, time(9,  0), time(11, 0), "allowed"),    # Mon AM
    (4, time(9,  0), time(11, 0), "allowed"),    # Fri AM
    (5, time(11, 0), time(13, 0), "weekend"),    # Sat late morning
    (5, time(15, 0), time(18, 0), "weekend"),    # Sat catch-up
    (6, time(16, 0), time(19, 0), "weekend"),    # Sun pre-week planning
]

_IMA_WINDOWS = [
    (0, time(19,30), time(21,30), "primary"),    # Mon eve
    (1, time(19,30), time(21,30), "primary"),    # Tue eve
    (1, time(12, 0), time(13, 0), "bonus"),      # Tue lunch
    (2, time(19,30), time(21,30), "primary"),    # Wed eve
    (3, time(19,30), time(21,30), "primary"),    # Thu eve
    (3, time(12, 0), time(13, 0), "bonus"),      # Thu lunch
    (4, time(19,30), time(21,30), "primary"),    # Fri eve
    (5, time(9,  0), time(11, 0), "primary"),    # Sat morning
    (5, time(15, 0), time(18, 0), "weekend"),     # Sat creator scroll
    (5, time(19,30), time(21,30), "primary"),     # Sat post-dinner scroll
    (6, time(9,  0), time(11, 0), "primary"),    # Sun morning
    (6, time(15, 0), time(18, 0), "weekend"),     # Sun creator scroll
    (6, time(19,30), time(21,30), "primary"),     # Sun post-dinner scroll
]


def _in_windows(windows: list, now: datetime | None = None) -> tuple[bool, str]:
    now = now or datetime.now(ET)
    wd = now.weekday()
    t = now.time()
    for day, start, end, priority in windows:
        if wd == day and start <= t <= end:
            return True, priority
    return False, ""


def maroon_in_window(now: datetime | None = None) -> tuple[bool, str]:
    """Returns (ok_to_send, priority). priority = 'primary'|'allowed'|''"""
    return _in_windows(_MAROON_WINDOWS, now)


def ima_in_window(now: datetime | None = None) -> tuple[bool, str]:
    """Returns (ok_to_send, priority). priority = 'primary'|'bonus'|''"""
    return _in_windows(_IMA_WINDOWS, now)


def _next_window(windows: list, now: datetime | None = None) -> datetime | None:
    now = now or datetime.now(ET)
    now = now.astimezone(ET) if now.tzinfo else now.replace(tzinfo=ET)
    candidates: list[datetime] = []
    for day_offset in range(14):
        target_date = (now + timedelta(days=day_offset)).date()
        for day, start, _end, _priority in windows:
            if target_date.weekday() != day:
                continue
            candidate = datetime.combine(target_date, start, tzinfo=ET)
            if candidate > now:
                candidates.append(candidate)
    return min(candidates) if candidates else None


def next_maroon_window(now: datetime | None = None) -> str:
    """Human-readable next BrandA window for logging."""
    candidate = _next_window(_MAROON_WINDOWS, now)
    if not candidate:
        return "unknown"
    return candidate.strftime("%a %b %d %H:%M ET")
