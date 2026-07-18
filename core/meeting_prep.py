#!/usr/bin/env python3
"""meeting_prep.py — CLONE Phase 4 anticipation: before EVERY work meeting, STAGE a grounded
brief, unprompted. It NEVER sends, NEVER alerts, NEVER emails. It writes one artifact to Operator's
review surface and stops. Silent by construction.

This is the ANTICIPATION sibling of the existing agents/meeting_prep.py — and deliberately NOT
that module. agents/meeting_prep.py SENDS an HTML briefing email 30 min out; that violates the
Phase-4 mandate (no sends, no pushes). This module reuses that module's Outlook calendar reader
(the one piece worth pilfering) and throws away its send path entirely.

PILFER, don't reimplement:
  - agents.meeting_prep._get_upcoming_meetings  -> the Outlook AppleScript calendar read
  - core.graph_memory.traverse                  -> who each attendee is (work-lane entity walk)
  - core.work_rag.search                        -> the relevant tickets/threads (semantic + FTS)
  - core.episodic_memory.recall / redacted_memory-> last call's notes + open action items
  - data/runtime/meeting_debriefs/*.json        -> prior LittleBird/Zoom debriefs (personal already
                                                   stripped upstream by the LittleBird decoder)

HARD LANE FIREWALL (work ⟂ personal):
  _is_work_meeting() drops any personal / 1:1-non-work event BEFORE it is ever briefed. A personal
  event never reaches graph_memory or the review surface. graph_memory.traverse and
  episodic.recall are themselves lane-scoped (lane="work"), so even the retrieval can't surface a
  personal-lane node. Two independent guarantees, same as the rest of the stack.

OUTCOME-verify, not proxy: brief() returns the assembled brief; stage() returns the written path
and the smoke proof RE-READS the file off disk and asserts the attendee identity + tickets +
last-notes are actually in it — not merely that the function exited 0 (the TikTok trap).

NO SEND SURFACE: this module imports nothing that can send. There is no email_sender, no
apple_mail, no alert_gateway import anywhere below. Staging = a local file write, reversible and
inert, so it needs no decision_modeling approval gate (that gate is for OUTWARD autonomous acts).

    .venv/bin/python -m core.meeting_prep            # run the smoke proof (default, no live svc)
    .venv/bin/python -m core.meeting_prep run        # real: read calendar, brief+stage work mtgs
    .venv/bin/python -m core.meeting_prep run --min 90

STAGED — the launchd plist (deploy/launchagents/com.claude-stack.meeting-prep-clone.plist) is
written but NOT loaded. Review the staged briefs, then load it.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

WORK = "work"
REVIEW_DIR = BASE_DIR / "data" / "runtime" / "meeting_prep_briefs"  # the review surface (work-lane)
DEBRIEFS_DIR = BASE_DIR / "data" / "runtime" / "meeting_debriefs"   # prior LittleBird/Zoom debriefs

# Personal-lane markers on a calendar EVENT. If one of these is present AND there is no CompanyA
# work signal, the event is personal → skipped. Kept conservative: a false "personal" only costs a
# missed brief; a false "work" could leak a personal event onto the review surface, so we fail
# toward personal. (The LittleBird decoder already strips personal calls from any NOTES we pull;
# this is the calendar-side gate.)
_PERSONAL_MARKERS = (
    "therapy", "therapist", "counsel", "doctor", "dentist", "dr.", "appt", "appointment",
    "personal", "family", "school", "pickup", "pick up", "date night", "birthday", "vet",
    "workout", "gym", "haircut", "littlebird 1:1", "1:1 personal", "coach", "mom", "dad",
)
_WORK_DOMAINS = ("CompanyA", "vrm", "Operator.com")


# ───────────────────────────────────────────────────────── attendee parsing (pilfered helpers)
def _attendee_emails(attendees_str: str) -> list[str]:
    return [e.lower() for e in re.findall(r"<([^>]+@[^>]+)>", attendees_str or "")]


def _attendee_names(attendees_str: str) -> list[str]:
    names = []
    for part in (attendees_str or "").split(";"):
        part = part.strip()
        name = part.split("<")[0].strip() if "<" in part else part
        if name and "@" not in name:
            names.append(name)
    return names


def _meeting_key(meeting: dict) -> str:
    raw = f"{meeting.get('subject','')}|{meeting.get('start','')}"
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


# ───────────────────────────────────────────────────────────────── FIREWALL: work vs personal
def _is_work_meeting(meeting: dict) -> bool:
    """True iff this is a WORK meeting worth briefing. Fail toward personal (skip) on ambiguity."""
    subject = (meeting.get("subject") or "").lower()
    if not subject or subject in ("lunch", "block", "focus time", "busy", "hold", "ooo", "pto"):
        return False

    hay = " ".join([
        subject,
        (meeting.get("attendees") or "").lower(),
        (meeting.get("organizer") or "").lower(),
        (meeting.get("body") or "").lower()[:400],
    ])
    emails = _attendee_emails(meeting.get("attendees", "")) + [
        (meeting.get("organizer") or "").lower()]
    has_work_signal = any(d in e for e in emails for d in _WORK_DOMAINS) or any(
        d in hay for d in _WORK_DOMAINS)
    personal_hit = any(m in hay for m in _PERSONAL_MARKERS)

    if personal_hit and not has_work_signal:
        return False          # personal 1:1 / non-work event → firewall
    return has_work_signal     # only brief events with a real work signal


# ───────────────────────────────────────────────────────────────────── provider seam (for DI)
@dataclass
class Providers:
    """Injectable retrieval seam. Defaults wire to the live work-lane stores; the smoke proof
    injects controlled fakes so it never touches Outlook / Ollama / a live DB."""
    identify: Callable[[str], dict] = None            # name -> {who, citations}
    search_context: Callable[[str, int], list] = None  # (topic, k) -> [{text, source, ref}]
    last_notes: Callable[[dict], dict] = None          # meeting -> {notes, action_items, citations}

    def __post_init__(self):
        self.identify = self.identify or _default_identify
        self.search_context = self.search_context or _default_search_context
        self.last_notes = self.last_notes or _default_last_notes


def _default_identify(name: str) -> dict:
    """Who is this attendee — work-lane entity walk + memory. All defensive; degrades to empty."""
    who, cites = "", []
    try:
        from core import graph_memory
        g = graph_memory.traverse(name, lane=WORK, summarize=False)
        people = [p["name"] for p in g.get("people", [])]
        tickets = [t["name"] for t in g.get("tickets", [])]
        bits = []
        if tickets:
            bits.append("linked to " + ", ".join(sorted(set(tickets))[:4]))
        if people and len(people) > 1:
            bits.append("connects to " + ", ".join(sorted(set(p for p in people if p != name))[:4]))
        who = "; ".join(bits)
        for e in g.get("edges", [])[:4]:
            if e.get("source_ref"):
                cites.append(e["source_ref"])
    except Exception:
        pass
    try:
        from core import redacted_memory
        for m in redacted_memory.search(name, limit=2):
            snip = (m.get("summary") or m.get("content") or "").strip()
            if snip and not who:
                who = snip[:180]
            ref = m.get("id") or m.get("systems")
            if ref:
                cites.append(f"memory:{ref}")
    except Exception:
        pass
    return {"who": who, "citations": cites[:6]}


def _default_search_context(topic: str, k: int = 6) -> list:
    """Relevant tickets/threads via hybrid work RAG. Degrades to [] if Ollama/index unavailable."""
    try:
        from core import work_rag
        hits = work_rag.search(topic, k=k)
        return [{"text": (h.get("text") or "")[:280],
                 "source": h.get("source", "?"),
                 "ref": h.get("ref", "?")} for h in hits]
    except Exception:
        return []


def _default_last_notes(meeting: dict) -> dict:
    """Last call's notes + open action items. Prior debriefs (LittleBird/Zoom, personal already
    stripped upstream) + episodic recall. All work-lane, all defensive."""
    subject = meeting.get("subject", "")
    names = set(_attendee_names(meeting.get("attendees", "")))
    notes, actions, cites = [], [], []

    # Prior debriefs: match a debrief whose person is an attendee, or whose text overlaps subject.
    try:
        if DEBRIEFS_DIR.exists():
            subj_toks = {t for t in re.findall(r"[a-z]{4,}", subject.lower())}
            picked = []
            for fp in sorted(DEBRIEFS_DIR.glob("*.json"),
                             key=lambda p: p.stat().st_mtime, reverse=True)[:400]:
                try:
                    d = json.loads(fp.read_text())
                except Exception:
                    continue
                person = str(d.get("person", ""))
                blob = json.dumps(d.get("results", {})).lower()
                name_hit = person and any(person.lower() in n.lower() or n.lower() in person.lower()
                                          for n in names)
                topic_hit = subj_toks and len(subj_toks & set(re.findall(r"[a-z]{4,}", blob))) >= 2
                if name_hit or topic_hit:
                    picked.append((fp, d))
                if len(picked) >= 3:
                    break
            for fp, d in picked:
                res = d.get("results", {})
                ai = res.get("littlebird_action_items")
                if ai:
                    for line in _to_lines(ai)[:6]:
                        actions.append(line)
                lb = res.get("littlebird_search") or res.get("littlebird_transcripts")
                if lb:
                    for line in _to_lines(lb)[:4]:
                        notes.append(line)
                cites.append(f"debrief:{fp.stem}")
    except Exception:
        pass

    # Episodic recall around the topic (lane-scoped work).
    try:
        from core import episodic_memory
        for ev in episodic_memory.recall(subject, WORK, limit=6):
            s = (ev.get("summary") or "").strip()
            if s:
                notes.append(s[:200])
                if ev.get("id"):
                    cites.append(f"episodic:{ev['id']}")
    except Exception:
        pass

    # de-dup, bound
    notes = _dedup(notes)[:8]
    actions = _dedup(actions)[:8]
    return {"notes": notes, "action_items": actions, "citations": cites[:8]}


def _to_lines(val) -> list[str]:
    if isinstance(val, list):
        out = []
        for v in val:
            out.extend(_to_lines(v))
        return out
    text = val if isinstance(val, str) else json.dumps(val)
    lines = [l.strip(" -•\t") for l in text.splitlines() if l.strip()]
    return [l for l in lines if len(l) > 3]


def _dedup(items: list[str]) -> list[str]:
    seen, out = set(), []
    for it in items:
        k = it.lower()[:80]
        if k not in seen:
            seen.add(k)
            out.append(it)
    return out


# ───────────────────────────────────────────────────────────────────────────────── upcoming()
def upcoming(within_min: int = 60, *, meetings: list[dict] | None = None,
             now: datetime | None = None) -> list[dict]:
    """Work meetings starting within `within_min` minutes. Pass `meetings=` to bypass the live
    Outlook read (the smoke path). Personal/1:1-non-work events are dropped by the firewall."""
    if meetings is None:
        try:
            from agents.meeting_prep import _get_upcoming_meetings
            hours = max(1, (within_min + 59) // 60)
            meetings = _get_upcoming_meetings(hours)
        except Exception:
            meetings = []

    now = now or datetime.now(timezone.utc)
    out = []
    for m in meetings:
        if not _is_work_meeting(m):        # FIREWALL — personal never proceeds
            continue
        mins = _minutes_until(m.get("start", ""), now)
        if mins is None or 0 <= mins <= within_min:   # unparseable start: already window-bounded
            out.append(m)
    return out


def _minutes_until(start: str, now: datetime) -> float | None:
    if not start:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S%z", "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            dt = datetime.strptime(start.strip(), fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return (dt - now).total_seconds() / 60.0
        except ValueError:
            continue
    return None


# ─────────────────────────────────────────────────────────────────────────────────── brief()
def brief(meeting: dict, *, providers: Providers | None = None) -> dict:
    """Assemble a grounded, cited, work-lane brief for one meeting. Pure assembly — no send, no
    model call by default. Raises nothing on empty stores; sections degrade to empty + a note."""
    if not _is_work_meeting(meeting):
        raise ValueError("refusing to brief a non-work / personal meeting (firewall)")

    p = providers or Providers()
    subject = meeting.get("subject", "Untitled")
    names = _attendee_names(meeting.get("attendees", ""))
    emails = _attendee_emails(meeting.get("attendees", ""))
    topic = f"{subject} {(meeting.get('body') or '')[:160]}".strip()

    attendees = []
    for i, name in enumerate(names[:8]):
        ident = {}
        try:
            ident = p.identify(name) or {}
        except Exception:
            ident = {}
        attendees.append({
            "name": name,
            "email": emails[i] if i < len(emails) else "",
            "who": ident.get("who", ""),
            "citations": ident.get("citations", []),
        })

    try:
        tickets = p.search_context(topic, 6) or []
    except Exception:
        tickets = []
    try:
        ln = p.last_notes(meeting) or {}
    except Exception:
        ln = {}
    notes = ln.get("notes", [])
    actions = ln.get("action_items", [])

    summary = _three_bullets(subject, attendees, tickets, notes, actions)

    return {
        "key": _meeting_key(meeting),
        "subject": subject,
        "start": meeting.get("start", ""),
        "location": meeting.get("location", ""),
        "organizer": meeting.get("organizer", ""),
        "lane": WORK,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "attendees": attendees,
        "tickets_threads": tickets,
        "last_notes": notes,
        "open_action_items": actions,
        "citations": ln.get("citations", []),
        "summary": summary,
    }


def _three_bullets(subject, attendees, tickets, notes, actions) -> list[str]:
    """3-bullet 'what this is about + what's unresolved' — deterministic, grounded in what we pulled."""
    b = []
    top = tickets[0]["text"] if tickets else (notes[0] if notes else "")
    if top:
        b.append(f"About: {subject} — most relevant thread: {top[:160]}")
    else:
        b.append(f"About: {subject} (no linked tickets/threads found in the work index)")

    known = [a["name"] for a in attendees if a.get("who")]
    if known:
        b.append("Who: " + ", ".join(f"{a['name']} ({a['who'][:70]})"
                                     for a in attendees if a.get("who"))[:220])
    elif attendees:
        b.append("Who: " + ", ".join(a["name"] for a in attendees[:6]) + " (no prior graph context)")
    else:
        b.append("Who: attendees not listed on the invite")

    if actions:
        b.append("Unresolved: " + "; ".join(actions[:3])[:220])
    elif tickets:
        b.append(f"Unresolved: {len(tickets)} open thread(s) to reconcile; no explicit action items on file")
    else:
        b.append("Unresolved: nothing on file — walk in clean, capture fresh actions")
    return b


# ─────────────────────────────────────────────────────────────────────────────────── stage()
def stage(brief_obj: dict, *, review_dir: Path | None = None) -> Path:
    """Write the brief to the review surface. NEVER sends. Returns the JSON path.

    Staging is a local, reversible file write (no outward effect) → no decision_modeling gate
    needed; that gate governs OUTWARD autonomous actions, of which this has none."""
    d = review_dir or REVIEW_DIR
    d.mkdir(parents=True, exist_ok=True)
    key = brief_obj.get("key") or hashlib.sha1(
        brief_obj.get("subject", "").encode()).hexdigest()[:16]

    json_path = d / f"{key}.json"
    json_path.write_text(json.dumps(brief_obj, indent=2, default=str))
    (d / f"{key}.md").write_text(_render_md(brief_obj))
    _append_index(d, brief_obj, key)
    return json_path


def _render_md(b: dict) -> str:
    L = [f"# meeting brief — {b.get('subject','?')}",
         f"start: {b.get('start','')}   loc: {b.get('location','') or 'n/a'}",
         f"staged: {b.get('generated_at','')}  (review only — NOT sent)", "",
         "## what this is about + what's unresolved"]
    L += [f"- {x}" for x in b.get("summary", [])]
    L += ["", "## attendees"]
    for a in b.get("attendees", []):
        who = f" — {a['who']}" if a.get("who") else ""
        L.append(f"- {a['name']} <{a.get('email','')}>{who}")
    L += ["", "## relevant tickets / threads"]
    for t in b.get("tickets_threads", []) or ["(none found in the work index)"]:
        if isinstance(t, dict):
            L.append(f"- [{t.get('source','?')}:{t.get('ref','?')}] {t.get('text','')}")
        else:
            L.append(f"- {t}")
    L += ["", "## open action items (from last call)"]
    L += [f"- {a}" for a in b.get("open_action_items", [])] or ["- (none on file)"]
    if b.get("last_notes"):
        L += ["", "## last call notes"]
        L += [f"- {n}" for n in b.get("last_notes", [])]
    if b.get("citations"):
        L += ["", "## sources", "- " + ", ".join(str(c) for c in b.get("citations", []))]
    return "\n".join(L) + "\n"


def _append_index(d: Path, b: dict, key: str) -> None:
    idx = d / "INDEX.md"
    line = f"- {b.get('generated_at','')[:16]}  {b.get('subject','?')[:60]}  -> {key}.md\n"
    try:
        with idx.open("a") as fh:
            fh.write(line)
    except OSError:
        pass


# ─────────────────────────────────────────────────────────────────────────────────── run()
def run(within_min: int = 60, *, meetings: list[dict] | None = None) -> list[Path]:
    """Real cadence entry point: brief + stage every upcoming work meeting once. Idempotent —
    a meeting already staged this run is skipped. Silent: returns paths, tells no one."""
    staged = []
    for m in upcoming(within_min, meetings=meetings):
        key = _meeting_key(m)
        if (REVIEW_DIR / f"{key}.json").exists():
            continue
        try:
            staged.append(stage(brief(m)))
        except Exception:
            continue   # silent self-correction: one bad meeting never breaks the batch
    return staged


# ─────────────────────────────────────────────────────────────────────────────────── smoke
def _smoke() -> int:
    import tempfile
    fails = []

    work_mtg = {
        "subject": "Valley Partners portal rollout sync",
        "start": "2026-07-11 15:30:00",
        "location": "Zoom",
        "organizer": "user@example.com",
        "attendees": "Blake Harmon <user@example.com>; Priya Nair <user@example.com>;",
        "body": "review remaining portal permission scope before go-live",
    }
    personal_mtg = {
        "subject": "1:1 personal — therapy",
        "start": "2026-07-11 15:45:00",
        "location": "",
        "organizer": "user@example.com",
        "attendees": "Dr. Sam Rivera <user@example.com>;",
        "body": "weekly personal appointment",
    }

    # 1) FIREWALL: upcoming() keeps the work mtg, drops the personal one.
    now = datetime(2026, 7, 11, 15, 0, tzinfo=timezone.utc)
    up = upcoming(60, meetings=[work_mtg, personal_mtg], now=now)
    subjects = [m["subject"] for m in up]
    if work_mtg["subject"] not in subjects:
        fails.append("firewall: work meeting was dropped")
    if personal_mtg["subject"] in subjects:
        fails.append("FIREWALL LEAK: personal 1:1 reached the brief queue")

    # brief() itself must refuse a personal meeting.
    try:
        brief(personal_mtg)
        fails.append("FIREWALL LEAK: brief() accepted a personal meeting")
    except ValueError:
        pass

    # 2) brief() pulls attendee identity + tickets + last-notes — via injected providers (no
    #    live Outlook/Ollama/DB). OUTCOME-verified: we re-read the staged file off disk.
    fake = Providers(
        identify=lambda name: {
            "who": f"portal admin, owns SF-1234" if name == "Blake Harmon" else "delivery lead",
            "citations": [f"graph:{name.split()[0].lower()}"]},
        search_context=lambda topic, k: [
            {"text": "portal permission scope finalized read-only to Valley Partners",
             "source": "salesforce", "ref": "share_blake"},
            {"text": "MVP shipped to production, community switched to Live",
             "source": "salesforce", "ref": "portal_live"}],
        last_notes=lambda mtg: {
            "notes": ["last sync: agreed write access stays denied"],
            "action_items": ["confirm SP_Portal_Machine_Attribution perm set assigned",
                             "revert UserOpportunityController to without sharing"],
            "citations": ["debrief:abc123"]},
    )
    b = brief(work_mtg, providers=fake)

    if not any(a["name"] == "Blake Harmon" and "SF-1234" in a["who"] for a in b["attendees"]):
        fails.append("brief: attendee identity (graph) not assembled")
    if not any(t.get("ref") == "share_blake" for t in b["tickets_threads"]):
        fails.append("brief: related tickets/threads (work_rag) not assembled")
    if not any("perm set" in a.lower() for a in b["open_action_items"]):
        fails.append("brief: last-call open action items not assembled")
    if len(b["summary"]) != 3:
        fails.append(f"brief: expected 3 summary bullets, got {len(b['summary'])}")

    # 3) stage() writes to the review surface (a TEMP dir here) and RE-READS to prove content
    #    landed. No send occurs (no sender is importable in this module).
    with tempfile.TemporaryDirectory() as td:
        path = stage(b, review_dir=Path(td))
        if not path.exists():
            fails.append("stage: json artifact not written")
        else:
            disk = json.loads(path.read_text())
            if not any("SF-1234" in a["who"] for a in disk["attendees"]):
                fails.append("stage: attendee identity missing from staged file")
            if not any(t.get("ref") == "share_blake" for t in disk["tickets_threads"]):
                fails.append("stage: tickets missing from staged file")
            if not disk["open_action_items"]:
                fails.append("stage: action items missing from staged file")
            md = (Path(td) / f"{disk['key']}.md").read_text()
            if "Blake Harmon" not in md or "NOT sent" not in md:
                fails.append("stage: markdown render incomplete / send-warning absent")

    # 4) no-send structural guarantee: no send surface is bound in this module's namespace.
    g = globals()
    for banned in ("send_apple_mail", "email_sender", "alert_gateway", "RECIPIENT_EMAIL"):
        if banned in g:
            fails.append(f"NO-SEND VIOLATION: {banned} is bound in module namespace")
    if "core.email_sender" in sys.modules:
        fails.append("NO-SEND VIOLATION: importing meeting_prep pulled in core.email_sender")

    if fails:
        print("SMOKE FAIL:")
        for f in fails:
            print("  -", f)
        return 1
    print("SMOKE PASS")
    print(f"  firewall: kept {subjects}, dropped personal 1:1")
    print(f"  brief assembled: {len(b['attendees'])} attendees, "
          f"{len(b['tickets_threads'])} threads, {len(b['open_action_items'])} open actions")
    print("  3-bullet summary:")
    for line in b["summary"]:
        print("    •", line)
    print("  staged->reread OK; no send surface imported")
    return 0


def _main() -> int:
    args = sys.argv[1:]
    if args and args[0] == "run":
        within = 60
        if "--min" in args:
            try:
                within = int(args[args.index("--min") + 1])
            except (ValueError, IndexError):
                pass
        paths = run(within)
        print(f"staged {len(paths)} brief(s) to {REVIEW_DIR}")
        return 0
    return _smoke()


if __name__ == "__main__":
    raise SystemExit(_main())
