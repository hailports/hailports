#!/usr/bin/env python3
"""scheduling_availability — the meeting-scheduling / availability work-lane specialist.

Domain: "are you free", "do you have time this morning", "5 mins?", "I sent an invite, can you
attend", "please schedule X for Monday". The recurring scheduling ping-pong that today gets a
guessed reply. This specialist turns it into a READY answer grounded in the real work calendar.

Deterministic-first, $0, read-only:
  1. parse ZOOM_CALENDAR.md (the Zoom digest that mirrors the Outlook work calendar) for free/busy,
  2. run a deterministic slot-finder over the parsed events inside the relevant window,
  3. cross-check any named invite time against existing blocks,
  4. recompute the free minutes a SECOND way (conservation: busy + free == the window) before it
     ever claims an answer,
  5. hand back structured draft_material for the reply drafter to stage — Operator sends.

BLAST BEHAVIOR (hard):
  reading free/busy is read-only -> we DELIVER a drafted availability reply.
  SENDING an invite / accepting / declining / writing the calendar is OUTWARD + is_send-fenced ->
  human-gated. A "please schedule X" ask is answered with a PROPOSE ("monday's open, want me to
  send the invite?") — this module NEVER creates, accepts, declines, or writes an event, and never
  stages a write artifact. No LLM is needed on the happy path (pure calendar math); there is no
  paid/subscription escalation here.

Return contract (mirrors the other specialists so core.specialist_dispatch can hand it straight to
the drafter):
    {ran, result, draft_material, staged_artifacts, needs_alex, confidence}
    draft_material = {mode, band, summary, proof, verified, recommendation, numbers}
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

# ── config ───────────────────────────────────────────────────────────────────────────────────────
LOCAL_TZ = ZoneInfo(os.environ.get("WORK_LOCAL_TZ", "America/Chicago"))
WORK_START_H = int(os.environ.get("WORK_DAY_START_HOUR", "9"))
WORK_END_H = int(os.environ.get("WORK_DAY_END_HOUR", "17"))
MORNING_END_H = 12
DEFAULT_MEETING_MIN = 30

# ZOOM_CALENDAR.md — the already-expanded occurrence feed that mirrors the full Outlook work
# calendar (same file the outlook_calendar_sqlite tool reads). Times in it are UTC.
ZOOM_DIGEST = Path(os.environ.get(
    "ZOOM_CALENDAR_DIGEST",
    str(Path.home() / ".openclaw/workspace/CompanyA-local/digests/ZOOM_CALENDAR.md")))

# `- 2026-07-14 17:00 · DPP Sync · 60m`
_ZOOM_LINE = re.compile(r"^-\s*(\d{4})-(\d{2})-(\d{2})\s+(\d{2}):(\d{2})\s*·\s*(.+?)\s*·\s*(\d+)m\s*$")

_WEEKDAYS = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
             "friday": 4, "saturday": 5, "sunday": 6}

# ── ask classification ───────────────────────────────────────────────────────────────────────────
_AVAIL = re.compile(
    r"\b(?:are|r|you)\s+(?:you\s+)?(?:free|available)\b"
    r"|\bdo you have (?:any )?time\b|\bhave (?:you )?(?:got |any )?time\b"
    r"|\bwhen (?:are|r) you free\b|\bwhat'?s your availability\b|\byour availability\b"
    r"|\bgot (?:a|any) (?:few )?min(?:ute)?s?\b|\bhave (?:a|any) (?:few )?min(?:ute)?s?\b"
    r"|\bfree (?:this|tomorrow|today|monday|tuesday|wednesday|thursday|friday|next|for|to|later)\b"
    r"|\bavailable (?:this|tomorrow|today|for|to|on|later|monday|tuesday|wednesday|thursday|friday)\b"
    r"|\bany (?:free )?time (?:this|today|tomorrow|for)\b",
    re.I,
)
# a bare "N mins?" / "5 minutes?" reads as an availability ping ("just off a call, 5 mins?").
_MINS_PING = re.compile(r"\b\d+\s*min(?:ute)?s?\b.*\?|\b\d+\s*min(?:ute)?s?\s*\??\s*$", re.I)
_INVITE_CONFIRM = re.compile(
    r"\b(?:sent|shot|dropped|put)\s+(?:you\s+)?(?:an?\s+|the\s+)?invite\b"
    r"|\bcalendar invite\b|\bmeeting invite\b"
    r"|\bcan you (?:attend|make it|make that|join|be there)\b"
    r"|\blet me know if you can (?:attend|make|join|be there)\b"
    r"|\bwill you be able to (?:attend|join|make it)\b",
    re.I,
)
_SCHEDULE_REQ = re.compile(
    r"\b(?:please\s+)?(?:schedule|set up|book|slot in|line up|get .{0,30}on (?:the|your|my) calendar"
    r"|put .{0,30}on (?:the|your|my) calendar|find (?:a|some) time (?:for|to)|set .{0,20}up for)\b",
    re.I,
)


def classify(text: str) -> dict:
    """Is this a scheduling / availability ask, and which intent? Pure, no I/O.

    Returns {is_scheduling, intent, why}. intent in {schedule_request, invite_confirm, availability}.
    Precedence: an explicit "schedule this" (a create-an-event ask) wins over a plain availability
    check, because it triggers the outward is_send-fenced propose path.
    """
    low = (text or "").lower()
    if _SCHEDULE_REQ.search(low):
        return {"is_scheduling": True, "intent": "schedule_request",
                "why": "asked to schedule/book an event -> outward, propose only (never auto-create)"}
    if _INVITE_CONFIRM.search(low):
        return {"is_scheduling": True, "intent": "invite_confirm",
                "why": "asked to confirm attendance on a sent invite -> cross-check the time read-only"}
    if _AVAIL.search(low) or _MINS_PING.search(low):
        return {"is_scheduling": True, "intent": "availability",
                "why": "an availability check -> answer from real free/busy read-only"}
    return {"is_scheduling": False, "intent": None, "why": "not a scheduling/availability ask"}


# ── voice scrub (no AI/automation trace ever reaches draft_material) ─────────────────────────────
def _scrub(text: str) -> str:
    out = str(text or "")
    try:
        from core import work_reply_voice
        out = work_reply_voice.autofix(out)
    except Exception:
        pass
    return out.strip()


# ── calendar parsing (read-only) ────────────────────────────────────────────────────────────────
def _as_local(dt: Any) -> datetime | None:
    """Coerce a datetime / ISO string into a tz-aware LOCAL_TZ datetime. None on failure."""
    if isinstance(dt, datetime):
        return dt.astimezone(LOCAL_TZ) if dt.tzinfo else dt.replace(tzinfo=LOCAL_TZ)
    if isinstance(dt, str):
        s = dt.strip().replace("Z", "+00:00")
        try:
            d = datetime.fromisoformat(s)
        except Exception:
            return None
        return d.astimezone(LOCAL_TZ) if d.tzinfo else d.replace(tzinfo=LOCAL_TZ)
    return None


def _parse_digest(md: str) -> list[dict]:
    """Parse ZOOM_CALENDAR.md lines into events. Digest times are UTC -> convert to LOCAL_TZ."""
    out: list[dict] = []
    for ln in (md or "").splitlines():
        m = _ZOOM_LINE.match(ln.strip())
        if not m:
            continue
        y, mo, d, hh, mm, subj, dur = m.groups()
        try:
            st = datetime(int(y), int(mo), int(d), int(hh), int(mm),
                          tzinfo=timezone.utc).astimezone(LOCAL_TZ)
        except Exception:
            continue
        out.append({"subject": subj.strip(), "start": st, "end": st + timedelta(minutes=int(dur))})
    return out


def _normalize_events(raw: list[dict]) -> list[dict]:
    """Normalize caller-supplied events (start/end or start_time/end_time, dt or ISO) to local dt."""
    out: list[dict] = []
    for e in raw or []:
        if not isinstance(e, dict):
            continue
        st = _as_local(e.get("start") if e.get("start") is not None else e.get("start_time"))
        en = _as_local(e.get("end") if e.get("end") is not None else e.get("end_time"))
        if st is None or en is None or en <= st:
            continue
        out.append({"subject": str(e.get("subject") or e.get("title") or "").strip(),
                    "start": st, "end": en})
    return out


def _load_events(context: dict) -> tuple[list[dict], str | None]:
    """Deterministic source cascade (all read-only). Returns (events, source) or ([], None).

    context['allow_live'] (default True) gates the on-disk digest + the live Outlook reader; set it
    False to restrict to caller-injected events/calendar_md only.
    """
    # 1) caller handed us parsed events
    ev = context.get("events")
    if isinstance(ev, list) and ev:
        norm = _normalize_events(ev)
        if norm:
            return norm, "events"
    # 2) caller handed us raw ZOOM_CALENDAR.md text
    md = context.get("calendar_md")
    if isinstance(md, str) and md.strip():
        parsed = _parse_digest(md)
        if parsed:
            return parsed, "calendar_md"
    if not context.get("allow_live", True):
        return [], None
    # 3) the real ZOOM_CALENDAR.md on disk
    try:
        if ZOOM_DIGEST.exists():
            parsed = _parse_digest(ZOOM_DIGEST.read_text(encoding="utf-8", errors="ignore"))
            if parsed:
                return parsed, "zoom_digest"
    except Exception:
        pass
    # 4) fall back to the live Outlook calendar reader (also read-only)
    try:
        from tools import outlook_calendar_sqlite as ocs  # lazy: heavy import
        now = _now(context)
        rng = ocs._get_events_for_range(now, now + timedelta(days=8))
        norm = _normalize_events([{"subject": r.get("subject"), "start": r.get("start_time"),
                                   "end": r.get("end_time")} for r in rng])
        if norm:
            return norm, "outlook_sqlite"
    except Exception:
        pass
    return [], None


# ── window + duration parsing ────────────────────────────────────────────────────────────────────
def _now(context: dict) -> datetime:
    n = _as_local(context.get("now")) if context else None
    return n or datetime.now(LOCAL_TZ)


def _at(day: datetime, hour: int, minute: int = 0) -> datetime:
    return day.replace(hour=hour, minute=minute, second=0, microsecond=0)


def _resolve_window(low: str, now: datetime) -> tuple[datetime, datetime, str]:
    """Pick the target free/busy window from the ask. Future days start at the work-day open;
    'today' windows clip to now so we never offer a slot in the past."""
    def day_window(day: datetime, h0: int, h1: int, label: str, clip_now: bool) -> tuple:
        ws, we = _at(day, h0), _at(day, h1)
        if clip_now and now > ws:
            ws = now.replace(second=0, microsecond=0)
        return ws, we, label

    # explicit weekday ("schedule for Monday")
    for name, idx in _WEEKDAYS.items():
        if re.search(rf"\b{name}\b", low):
            ahead = (idx - now.weekday()) % 7
            if ahead == 0:
                ahead = 7  # "Monday" spoken on a Monday = next Monday
            return day_window(now + timedelta(days=ahead), WORK_START_H, WORK_END_H, name, False)

    if re.search(r"\btomorrow\b", low):
        return day_window(now + timedelta(days=1), WORK_START_H, WORK_END_H, "tomorrow", False)
    if re.search(r"\bmorning\b", low):
        ws, we, lbl = day_window(now, WORK_START_H, MORNING_END_H, "this morning", True)
        if ws >= we:  # morning already gone -> tomorrow morning
            return day_window(now + timedelta(days=1), WORK_START_H, MORNING_END_H, "tomorrow morning", False)
        return ws, we, lbl
    if re.search(r"\bafternoon\b", low):
        ws, we, lbl = day_window(now, MORNING_END_H, WORK_END_H, "this afternoon", True)
        if ws >= we:
            return day_window(now + timedelta(days=1), MORNING_END_H, WORK_END_H, "tomorrow afternoon", False)
        return ws, we, lbl

    # default: rest of today; if the work day is over, roll to tomorrow
    ws, we, lbl = day_window(now, WORK_START_H, WORK_END_H, "today", True)
    if ws >= we:
        return day_window(now + timedelta(days=1), WORK_START_H, WORK_END_H, "tomorrow", False)
    return ws, we, lbl


def _resolve_duration(low: str) -> int:
    m = re.search(r"(\d+)\s*(?:min|minute)", low)
    if m:
        return max(5, int(m.group(1)))
    m = re.search(r"(\d+)\s*(?:hr|hour)", low)
    if m:
        return int(m.group(1)) * 60
    if re.search(r"\bhalf (?:an )?hour\b", low):
        return 30
    if re.search(r"\b(?:an|one|1)\s*hour\b|\b60\b", low):
        return 60
    if re.search(r"\b(?:quick|couple min|couple of min|brief)\b", low):
        return 15
    return DEFAULT_MEETING_MIN


def _parse_named_time(low: str, day: datetime) -> datetime | None:
    """Pull a clock time out of an invite-confirm ask ('invite for today at 12:30')."""
    m = re.search(r"\b(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", low)
    if not m:
        m = re.search(r"\bat\s+(\d{1,2})(?::(\d{2}))?\b", low)
        if not m:
            return None
        hh = int(m.group(1))
        mm = int(m.group(2) or 0)
        return _at(day, hh, mm)
    hh = int(m.group(1)) % 12
    mm = int(m.group(2) or 0)
    if m.group(3).lower() == "pm":
        hh += 12
    return _at(day, hh, mm)


# ── deterministic free/busy math ─────────────────────────────────────────────────────────────────
def _merge(intervals: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    merged: list[tuple[datetime, datetime]] = []
    for s, e in sorted(intervals):
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


def _freebusy(events: list[dict], ws: datetime, we: datetime) -> dict:
    """Busy (clipped+merged) and free gaps inside [ws, we]. Free is recomputed a SECOND way and the
    two must reconcile (conservation): busy_minutes + free_minutes == window_minutes."""
    clipped: list[tuple[datetime, datetime]] = []
    window_busy_subjects: list[dict] = []
    for e in events:
        s, en = max(e["start"], ws), min(e["end"], we)
        if s < en:
            clipped.append((s, en))
            window_busy_subjects.append({"subject": e["subject"], "start": e["start"], "end": e["end"]})
    merged = _merge(clipped)

    gaps: list[tuple[datetime, datetime]] = []
    cur = ws
    for s, en in merged:
        if s > cur:
            gaps.append((cur, s))
        cur = max(cur, en)
    if cur < we:
        gaps.append((cur, we))

    def mins(iv):
        return sum(int((b - a).total_seconds() // 60) for a, b in iv)

    window_min = int((we - ws).total_seconds() // 60)
    busy_min = mins(merged)
    free_min = mins(gaps)
    # conservation: sum of gaps == window - busy (the independent second route)
    conservation_ok = (busy_min + free_min == window_min) and (free_min == window_min - busy_min)

    window_busy_subjects.sort(key=lambda d: d["start"])
    return {"busy": merged, "gaps": gaps, "busy_subjects": window_busy_subjects,
            "window_min": window_min, "busy_min": busy_min, "free_min": free_min,
            "conservation_ok": conservation_ok}


def _slots(gaps: list[tuple[datetime, datetime]], min_minutes: int) -> list[tuple[datetime, datetime]]:
    return [(a, b) for a, b in gaps if int((b - a).total_seconds() // 60) >= min_minutes]


def _overlap(t0: datetime, t1: datetime, busy: list[tuple[datetime, datetime]]) -> tuple | None:
    for s, e in busy:
        if t0 < e and s < t1:
            return (s, e)
    return None


# ── formatting (Operator voice, lowercase) ───────────────────────────────────────────────────────────
def _fmt_time(dt: datetime) -> str:
    s = dt.strftime("%-I:%M%p").lower()
    return s.replace(":00", "")


def _day_label(win_label: str) -> str:
    return win_label


def _fmt_slot(a: datetime, b: datetime, we: datetime, day_label: str) -> str:
    if b >= we:
        return f"free after {_fmt_time(a)} {day_label}"
    return f"the {_fmt_time(a)} slot {day_label}"


def _subject_for(dt_or_iv, busy_subjects: list[dict]) -> str | None:
    """Name the block occupying / adjacent to a time, for the 'the noon block is the DPP sync' color."""
    if isinstance(dt_or_iv, tuple):
        s, e = dt_or_iv
    else:
        s = e = dt_or_iv
    for b in busy_subjects:
        if b["start"] < e and s < b["end"] and b["subject"]:
            return b["subject"]
    return None


# ── the specialist ───────────────────────────────────────────────────────────────────────────────
def _handoff(msg: str, needs_alex: bool = True, conf: float = 0.35) -> dict:
    return {"ran": False, "result": {"reason": "no reachable calendar"},
            "draft_material": {"mode": "handoff", "band": "readonly", "summary": _scrub(msg),
                               "proof": None, "verified": False,
                               "recommendation": "hand over the calendar / retry once the digest is reachable",
                               "numbers": None},
            "staged_artifacts": [], "needs_alex": needs_alex, "confidence": conf}


def handle(ask: Any, context: dict | None = None) -> dict:
    """Run the scheduling specialist. Read-only; delivers a grounded availability reply or, for a
    create-an-event ask, a propose (never writes/sends). Fail-soft: any gap -> honest handoff."""
    context = dict(context or {})
    text = ask if isinstance(ask, str) else str(
        (ask or {}).get("last_message") or (ask or {}).get("text") or (ask or {}).get("body") or ask)
    low = text.lower()
    cls = classify(text)
    intent = cls["intent"] or "availability"
    now = _now(context)

    events, source = _load_events(context)
    if source is None:
        return _handoff("on it -- i can't reach the work calendar from here to check free/busy. "
                        "drop me the invite time / window and i'll confirm, or i'll retry once the "
                        "calendar digest is back.")

    ws, we, win_label = _resolve_window(low, now)
    duration = _resolve_duration(low)
    fb = _freebusy(events, ws, we)
    conservation_ok = fb["conservation_ok"]

    numbers = {
        "window": {"start": ws.isoformat(), "end": we.isoformat(), "label": win_label},
        "duration_min": duration,
        "window_min": fb["window_min"], "busy_min": fb["busy_min"], "free_min": fb["free_min"],
        "recompute": {"window_min": fb["window_min"], "busy_plus_free": fb["busy_min"] + fb["free_min"],
                      "free_2nd_route": fb["window_min"] - fb["busy_min"],
                      "conservation_ok": conservation_ok},
        "busy_blocks": [{"subject": b["subject"], "start": b["start"].isoformat(),
                         "end": b["end"].isoformat()} for b in fb["busy_subjects"]],
        "source": source,
    }
    proof = (f"read-only from ZOOM_CALENDAR.md ({source}); recomputed free a 2nd way -- "
             f"{fb['busy_min']}m busy + {fb['free_min']}m free == {fb['window_min']}m window "
             f"(conservation {'ok' if conservation_ok else 'FAILED'})")

    # ── invite-confirm: cross-check the named time against existing blocks ──────────────────────
    if intent == "invite_confirm":
        named = _parse_named_time(low, ws)
        if named is not None:
            clash = _overlap(named, named + timedelta(minutes=duration),
                             [(b["start"], b["end"]) for b in fb["busy_subjects"]])
            if clash is None and conservation_ok:
                summary = (f"yep, {_fmt_time(named)} {win_label} is open on my end -- i can make it. "
                           "go ahead & i'll accept the invite.")
                return _deliver(summary, proof, numbers, needs_alex=False, conf=0.85,
                                result={"intent": intent, "named": named.isoformat(), "clash": None})
            subj = _subject_for(clash, fb["busy_subjects"]) if clash else None
            alt = _slots(fb["gaps"], duration)
            alt_txt = f" -- {_fmt_slot(alt[0][0], alt[0][1], we, win_label)} is clear if we can shift" if alt else ""
            summary = (f"heads up -- {_fmt_time(named)} runs into "
                       f"{('the ' + subj) if subj else 'another block'} "
                       f"({_fmt_time(clash[0])}-{_fmt_time(clash[1])}){alt_txt}. "
                       "lemme know & i'll sort it, or i can grab the alt.")
            return _propose(summary, proof, numbers, needs_alex=True, conf=0.5,
                            result={"intent": intent, "named": named.isoformat(),
                                    "clash": [clash[0].isoformat(), clash[1].isoformat()]})
        # no time in the ask -> give today's shape so Operator can eyeball vs the invite
        blocks = fb["busy_subjects"]
        if not blocks:
            summary = f"{win_label} is wide open on my end, so i can attend -- send it over."
            return _deliver(summary, proof, numbers, needs_alex=False, conf=0.8,
                            result={"intent": intent, "named": None})
        blk = ", ".join(f"{_fmt_time(b['start'])} {b['subject']}" for b in blocks[:3])
        summary = (f"i've got {blk} {win_label} -- if the invite lands clear of those i can attend. "
                   "what time is it for?")
        return _propose(summary, proof, numbers, needs_alex=True, conf=0.5,
                        result={"intent": intent, "named": None})

    # ── schedule_request: OUTWARD (create+send) -> propose only, NEVER write/create ─────────────
    if intent == "schedule_request":
        slots = _slots(fb["gaps"], duration)
        if not conservation_ok:
            return _handoff("the calendar read didn't reconcile cleanly -- lemme re-pull before i "
                            "line anything up. i'll confirm the open slot shortly.")
        if slots:
            first = slots[0]
            note = ""
            subj = None
            for b in fb["busy_subjects"]:
                subj = b["subject"]
                break
            if subj:
                note = f" -- {win_label}'s only real block is {subj}"
            summary = (f"{win_label}'s got room -- {_fmt_slot(first[0], first[1], we, win_label)} "
                       f"works for the {duration}m{note}. want me to send the invite?")
        else:
            blk = ", ".join(f"{_fmt_time(b['start'])} {b['subject']}" for b in fb["busy_subjects"][:3])
            summary = (f"{win_label}'s pretty packed ({blk}) -- nothing clean for {duration}m. "
                       "want me to look at another day, or bump one of these?")
        # NOTE: we never create/accept/decline. is_send-fenced -> Operator sends the invite himself.
        return _propose(summary, proof, numbers, needs_alex=True, conf=0.55,
                        result={"intent": intent, "slots": [[a.isoformat(), b.isoformat()] for a, b in slots[:3]],
                                "outward_action": "send_invite", "human_gated": True})

    # ── availability: pure free/busy answer -> DELIVER ──────────────────────────────────────────
    slots = _slots(fb["gaps"], duration)
    if not conservation_ok:
        return _handoff("i pulled the calendar but the free/busy didn't reconcile -- re-checking "
                        "before i give you a time. one sec.")
    if not slots:
        # nothing today -> look at tomorrow, same work window
        t_ws, t_we = _at(now + timedelta(days=1), WORK_START_H), _at(now + timedelta(days=1), WORK_END_H)
        t_fb = _freebusy(events, t_ws, t_we)
        t_slots = _slots(t_fb["gaps"], duration)
        blk = ", ".join(f"{_fmt_time(b['start'])} {b['subject']}" for b in fb["busy_subjects"][:3])
        if t_slots and t_fb["conservation_ok"]:
            summary = (f"{win_label}'s slammed ({blk}) -- but {_fmt_slot(t_slots[0][0], t_slots[0][1], t_we, 'tomorrow')} "
                       f"is open for the {duration}m. that work?")
            numbers["tomorrow_free_min"] = t_fb["free_min"]
            return _deliver(summary, proof, numbers, needs_alex=False, conf=0.8,
                            result={"intent": intent, "today_slots": 0, "tomorrow_slots": len(t_slots)})
        summary = f"{win_label}'s fully booked ({blk}) & tomorrow's tight too -- want me to reach further out?"
        return _propose(summary, proof, numbers, needs_alex=True, conf=0.45,
                        result={"intent": intent, "today_slots": 0})

    # compose: name the notable block(s) for color ("the noon block is the DPP sync")
    slot_txt = _fmt_slot(slots[0][0], slots[0][1], we, win_label)
    if len(slots) > 1:
        slot_txt += f", or {_fmt_slot(slots[1][0], slots[1][1], we, win_label)}"
    color = ""
    if fb["busy_subjects"]:
        nb = fb["busy_subjects"][0]
        color = f" -- {_fmt_time(nb['start'])}'s the {nb['subject']}"
    summary = f"{slot_txt} for the {duration}m{color}. lmk what works & i'll hold it."
    return _deliver(summary, proof, numbers, needs_alex=False, conf=0.85,
                    result={"intent": intent, "slots": [[a.isoformat(), b.isoformat()] for a, b in slots[:4]]})


def _deliver(summary: str, proof: str, numbers: dict, needs_alex: bool, conf: float, result: dict) -> dict:
    return {"ran": True, "result": result,
            "draft_material": {"mode": "deliver", "band": "readonly", "summary": _scrub(summary),
                               "proof": proof, "verified": True,
                               "recommendation": "safe to send -- read-only free/busy, recomputed",
                               "numbers": numbers},
            "staged_artifacts": [], "needs_alex": needs_alex, "confidence": conf}


def _propose(summary: str, proof: str, numbers: dict, needs_alex: bool, conf: float, result: dict) -> dict:
    return {"ran": True, "result": result,
            "draft_material": {"mode": "propose", "band": "readonly", "summary": _scrub(summary),
                               "proof": proof, "verified": True,
                               "recommendation": "review -- sending/booking the invite is Operator's call (never auto)",
                               "numbers": numbers},
            "staged_artifacts": [], "needs_alex": needs_alex, "confidence": conf}


# ── selftest ────────────────────────────────────────────────────────────────────────────────────
def _selftest() -> int:
    fails: list[str] = []

    def check(cond: bool, label: str) -> None:
        if not cond:
            fails.append(label)
        print(f"  [{'ok' if cond else 'FAIL'}] {label}")

    print("=== scheduling_availability selftest ===\n")

    # A realistic ZOOM_CALENDAR.md slice. Times are UTC; America/Chicago in July is UTC-5 (CDT).
    # now is pinned to Tue 2026-07-14 09:00 CT (== 14:00 UTC).
    #   15:30 UTC = 10:30 CT  Salesforce - Daily Standup  30m  -> busy 10:30-11:00
    #   17:00 UTC = 12:00 CT  DPP Sync                    60m  -> busy 12:00-13:00
    cal_md = (
        "# Zoom Calendar — upcoming\n"
        "- 2026-07-14 15:30 · Salesforce - Daily Standup · 30m\n"
        "- 2026-07-14 17:00 · DPP Sync · 60m\n"
    )
    now_iso = "2026-07-14T09:00:00-05:00"
    ctx = {"calendar_md": cal_md, "now": now_iso}

    # 0) classification
    print("0) classification")
    check(classify("do you have any time this morning, maybe 30 minutes")["intent"] == "availability",
          "morning ask -> availability")
    check(classify("are you available for the data-migration call")["intent"] == "availability",
          "'are you available' -> availability")
    check(classify("just off a call, 5 mins?")["intent"] == "availability", "'5 mins?' -> availability")
    check(classify("I sent you an invite for today, let me know if you can attend")["intent"] == "invite_confirm",
          "sent-invite -> invite_confirm")
    check(classify("please schedule the demo for Monday, my calendar is up to date")["intent"] == "schedule_request",
          "'please schedule' -> schedule_request")
    check(classify("can you get me the count of tickets by status")["is_scheduling"] is False,
          "an analyst ask is NOT claimed by scheduling")

    # 1) availability, this morning, 30m -> real slots + conservation ------------------------------
    print("\n1) availability -> real free/busy this morning (the core proof)")
    d = handle("do you have any time this morning, maybe 30 minutes", ctx)
    dm = d["draft_material"]
    n = dm["numbers"]
    # morning window 9:00-12:00 = 180m. busy = standup 30m (DPP starts at noon, outside morning). free=150.
    check(d["ran"] and dm["mode"] == "deliver", "delivered a grounded availability reply")
    check(n["window_min"] == 180, f"morning window == 180m (got {n['window_min']})")
    check(n["busy_min"] == 30, f"busy == 30m, the standup (got {n['busy_min']})")
    check(n["free_min"] == 150, f"free == 150m (got {n['free_min']})")
    check(n["recompute"]["conservation_ok"] is True, "CONSERVATION: 30 busy + 150 free == 180 window (2nd route)")
    check(n["recompute"]["busy_plus_free"] == n["window_min"] == n["recompute"]["free_2nd_route"] + n["busy_min"],
          "busy+free and window-busy agree (recomputed a 2nd way)")
    check(dm["verified"] and d["needs_alex"] is False, "verified read-only -> needs_alex False (Operator still sends)")
    # first offered slot is 9:00 (before the 10:30 standup); second is 11:00 (after it)
    slots = d["result"]["slots"]
    check(slots[0][0].endswith("09:00:00-05:00"), f"first open slot == 9:00 (got {slots[0][0]})")
    check(any(s[0].endswith("11:00:00-05:00") for s in slots), "11:00 slot offered (after the standup)")
    print(f"     DRAFT: {dm['summary']}")
    print(f"     PROOF: {dm['proof']}")

    # 2) invite-confirm cross-check -> CLASH with the DPP block --------------------------------------
    print("\n2) invite-confirm -> cross-check the named time vs existing blocks")
    d2 = handle("I sent you an invite for today at 12:30, can you attend?", ctx)
    dm2 = d2["draft_material"]
    check(dm2["mode"] == "propose" and d2["needs_alex"] is True, "12:30 clashes -> propose + needs_alex")
    check("dpp sync" in dm2["summary"].lower(), "reply names the real conflicting block (DPP Sync)")
    check(d2["result"]["clash"] is not None, "clash interval recorded")
    print(f"     DRAFT: {dm2['summary']}")
    # a clear time -> deliver a confident yes
    d2b = handle("sent you an invite for today at 11:15, can you make it?", ctx)
    check(d2b["draft_material"]["mode"] == "deliver" and d2b["needs_alex"] is False,
          "11:15 is clear -> deliver 'i can make it'")
    print(f"     DRAFT: {d2b['draft_material']['summary']}")

    # 3) schedule-request -> PROPOSE ONLY, never writes/creates/sends -------------------------------
    print("\n3) schedule-request -> propose (outward is human-gated, no write staged)")
    d3 = handle("please schedule the demo for Monday, my calendar is up to date", ctx)
    dm3 = d3["draft_material"]
    check(dm3["mode"] == "propose" and d3["needs_alex"] is True, "schedule ask -> propose + needs_alex")
    check(d3["staged_artifacts"] == [], "NO write artifact staged (never creates an event)")
    check(d3["result"].get("human_gated") is True and d3["result"].get("outward_action") == "send_invite",
          "outward send-invite flagged human-gated")
    check("invite" in dm3["summary"].lower(), "reply asks before sending the invite (never auto)")
    # Monday 2026-07-20 has no events in the slice -> wide open
    check(dm3["numbers"]["busy_min"] == 0, "Monday shows no conflicts in the slice (free)")
    print(f"     DRAFT: {dm3['summary']}")

    # 4) '5 mins?' quick ping -> next open slot from now --------------------------------------------
    print("\n4) 'just off a call, 5 mins?' -> next open slot")
    d4 = handle("just off a call, 5 mins?", ctx)
    check(d4["draft_material"]["mode"] == "deliver", "quick ping delivered")
    check(d4["draft_material"]["numbers"]["duration_min"] == 5, "duration parsed as 5m")
    print(f"     DRAFT: {d4['draft_material']['summary']}")

    # 5) no reachable calendar -> honest handoff, never a guess -------------------------------------
    print("\n5) no calendar reachable -> handoff (never guesses availability)")
    d5 = handle("are you free this afternoon?", {"now": now_iso, "allow_live": False})  # no injected data
    check(d5["draft_material"]["mode"] == "handoff" and d5["needs_alex"] is True,
          "no data -> handoff + needs_alex (no fabricated slot)")
    check(d5["draft_material"]["verified"] is False, "handoff is not marked verified")
    print(f"     DRAFT: {d5['draft_material']['summary']}")

    print(f"\n{'PASS' if not fails else 'FAIL'} — {len(fails)} failure(s)"
          + ("" if not fails else ": " + "; ".join(fails)))
    return 1 if fails else 0


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        raise SystemExit(_selftest())
    print("scheduling_availability specialist — run with --selftest")
