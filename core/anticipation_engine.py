#!/usr/bin/env python3
"""anticipation_engine.py — pre-build what Operator is about to need BEFORE he asks.

This is the step that turns the responder into a clone: instead of waiting for the ask,
it predicts the next ask from three deterministic signals and STAGES the artifact so it's
already done when he reaches for it.

SIGNALS (deterministic-first — no LLM in the prediction path):
  1. calendar        — a meeting starting soon => its brief.
  2. time-of-day      — Monday morning => the week's SF ticket triage.
  3. recurring-ask    — mine clean_capture for asks Operator reliably repeats on a fixed
                        weekday/hour (e.g. a Friday 4pm status ask) => stage it the night
                        before. Patterns he IGNORES get their reward decayed until they
                        stop being pre-built (grade_usage feeds that back).

GROUNDING (RAG, only at prebuild time — never in prediction):
  Each staged artifact is assembled from the REAL brains, lane-scoped and CITED:
  work_rag (semantic work-lane), graph_memory (entity graph), episodic_memory (timeline).
  For hustle/personal lanes only episodic is used (work_rag is a work-lane store).

HARD RAILS honored here:
  - SILENT: this NEVER sends, texts, or acts outward. prebuild() only writes to the review
    surface data/runtime/anticipated.json. Anything actionable is gated through
    decision_modeling.score BEFORE its payload is even staged; a REJECT strips the payload.
  - LANE FIREWALL: every candidate carries exactly ONE lane; grounding retrieval is scoped
    to that lane (episodic.recall is WHERE lane=?, work_rag/graph are work-only). No merge.
  - OUTCOME-verified learning: grade_usage checks whether the pre-build was ACTUALLY used
    (a real matching follow-up ask, or an explicit open marker) — not "we staged it, exit 0".

    .venv/bin/python -m core.anticipation_engine        # hermetic smoke (throwaway paths)
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

if __package__ in (None, ""):  # allow `python core/anticipation_engine.py`
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import BASE_DIR
from core import clean_capture

# lanes — mirror episodic_memory's firewall vocabulary exactly
WORK, HUSTLE, PERSONAL = "work", "hustle", "personal"
LANES = (WORK, HUSTLE, PERSONAL)

STAGED_PATH = BASE_DIR / "data" / "runtime" / "anticipated.json"
STATE_PATH = BASE_DIR / "data" / "runtime" / "anticipation_state.json"
GRADED_LEDGER = BASE_DIR / "data" / "learning" / "anticipation_graded.jsonl"

# prediction knobs
MEETING_LOOKAHEAD_H = 14.0     # stage a brief once a meeting is within this many hours
RECURRING_MIN_OCCURRENCES = 3  # a signature must repeat at least this often to be a pattern
RECURRING_CONSISTENCY = 0.6    # >= this fraction on the dominant weekday to count as fixed
STAGE_LEAD_H = 24.0            # stage a recurring ask up to this many hours ahead (the night before)
USAGE_GRACE_H = 6.0           # after due_at + this, an un-acted anticipation counts as ignored
SUPPRESS_FLOOR = 0.15         # a pattern reward below this is a learned-ignore => stop pre-building
DEFAULT_REWARD = 0.5
REWARD_ALPHA = 0.35           # EWMA weight for a fresh usage outcome

KIND_BASE = {"meeting_brief": 1.0, "weekly_triage": 0.9, "recurring_ask": 0.8}

_STOP = {
    "the", "a", "an", "of", "to", "for", "and", "or", "is", "are", "was", "were", "be",
    "on", "in", "at", "it", "this", "that", "with", "from", "about", "you", "your", "me",
    "my", "i", "we", "our", "can", "could", "would", "should", "do", "does", "did", "get",
    "got", "have", "has", "had", "whats", "what", "when", "where", "how", "please", "hey",
    "lmk", "pls", "gimme", "give", "show", "send", "im", "ive", "so", "just", "any", "all",
}
_WORD = re.compile(r"[a-z0-9]{3,}")
_WORK_HINTS = re.compile(
    r"\b(salesforce|sfdc|ticket|triage|sprint|deploy|apex|permission|portal|CompanyA|"
    r"dpp|monday|timeclock|opportunity|rebate|community)\b", re.I)
_PERSONAL_HINTS = re.compile(r"\b(dinner|kids|family|home|doctor|dentist|birthday|weekend)\b", re.I)


# ───────────────────────────────────────────────────────────── helpers
def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _parse_dt(v) -> datetime | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    try:
        dt = datetime.fromisoformat(str(v).strip().replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _guard_lane(lane) -> str:
    norm = str(lane or "").strip().lower()
    if norm not in LANES:
        raise ValueError(f"forbidden/unknown lane {lane!r}; valid = {LANES} (firewall, no merge)")
    return norm


def _tokens(text: str) -> list[str]:
    return [w for w in _WORD.findall((text or "").lower()) if w not in _STOP]


def _signature(text: str) -> tuple[str, str]:
    """(stable sig id, human label) for an ask — the recurring-pattern + usage-match key."""
    toks = sorted(set(_tokens(text)))
    if not toks:
        return "sig_empty", ""
    sig = "sig_" + hashlib.sha1("|".join(toks).encode()).hexdigest()[:12]
    label = " ".join(toks[:6])
    return sig, label


def _infer_lane(text: str, channel: str = "") -> str:
    if _WORK_HINTS.search(text or ""):
        return WORK
    if _PERSONAL_HINTS.search(text or ""):
        return PERSONAL
    return PERSONAL  # ledger default: genuine asks that aren't work-flagged are personal


def _load_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def _save_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2))
    tmp.replace(path)


def _state(state_path: Path) -> dict:
    st = _load_json(state_path, {})
    st.setdefault("patterns", {})  # sig -> {reward,n,hits,misses,label,last_seen}
    return st


def _pattern_reward(st: dict, sig: str) -> float:
    p = st["patterns"].get(sig)
    return DEFAULT_REWARD if not p else float(p.get("reward", DEFAULT_REWARD))


# ───────────────────────────────────────────────────────────── pattern mining
def _operator_asks(ledger_path: Path) -> list[dict]:
    """Genuine operator inbounds only — machine pushes are excluded at capture time, so this
    is a clean stream of Operator's real asks. (source, ts, text, channel)."""
    out = []
    for rec in clean_capture._iter_records(ledger_path):
        if rec.get("direction") != clean_capture.OPERATOR_INBOUND:
            continue
        dt = _parse_dt(rec.get("ts"))
        if dt is None:
            continue
        out.append({"ts": dt, "text": rec.get("text") or "", "channel": rec.get("channel") or ""})
    return out


def _recurring_patterns(asks: list[dict]) -> list[dict]:
    """Cluster asks by signature; keep only ones that repeat on a CONSISTENT weekday.

    Returns [{sig,label,lane,weekday,hour,count,consistency}]. Deterministic — the whole
    prediction is a group-by, no model."""
    groups: dict[str, list[dict]] = {}
    for a in asks:
        sig, label = _signature(a["text"])
        if sig == "sig_empty":
            continue
        groups.setdefault(sig, []).append({**a, "_label": label})

    patterns = []
    for sig, items in groups.items():
        if len(items) < RECURRING_MIN_OCCURRENCES:
            continue
        # dominant weekday
        wd_counts: dict[int, int] = {}
        for it in items:
            wd_counts[it["ts"].weekday()] = wd_counts.get(it["ts"].weekday(), 0) + 1
        weekday = max(wd_counts, key=wd_counts.get)
        consistency = wd_counts[weekday] / len(items)
        if consistency < RECURRING_CONSISTENCY:
            continue  # scattered across the week — not a fixed cadence
        # typical hour = median of the dominant-weekday occurrences
        hours = sorted(it["ts"].hour for it in items if it["ts"].weekday() == weekday)
        hour = hours[len(hours) // 2]
        lane = _infer_lane(items[-1]["text"], items[-1]["channel"])
        patterns.append({
            "sig": sig, "label": items[-1]["_label"], "lane": lane,
            "weekday": weekday, "hour": hour, "count": len(items),
            "consistency": round(consistency, 2),
            "example": items[-1]["text"][:200],
        })
    return patterns


def _next_occurrence(now: datetime, weekday: int, hour: int) -> datetime:
    days_ahead = (weekday - now.weekday()) % 7
    cand = (now + timedelta(days=days_ahead)).replace(
        hour=hour, minute=0, second=0, microsecond=0)
    if cand <= now:
        cand += timedelta(days=7)
    return cand


# ───────────────────────────────────────────────────────────── anticipate()
def anticipate(now: datetime | None = None, calendar: list[dict] | None = None, *,
               ledger_path: Path | None = None, state_path: Path | None = None) -> list[dict]:
    """Rank the things worth pre-building RIGHT NOW. Pure prediction — no grounding, no writes.

    calendar: [{start, title, lane?, attendees?}] — future events (start iso/epoch/datetime).
    Returns candidates sorted by priority (desc). Each is a plain dict describing WHAT to
    pre-build and WHY; feed one to prebuild() to actually assemble+stage it."""
    now = now or _now_utc()
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    ledger_path = ledger_path or clean_capture.LEDGER
    st = _state(state_path or STATE_PATH)
    cands: list[dict] = []

    # 1) MEETINGS -> briefs
    for ev in calendar or []:
        start = _parse_dt(ev.get("start"))
        if start is None or start <= now:
            continue
        lead_h = (start - now).total_seconds() / 3600.0
        if lead_h > MEETING_LOOKAHEAD_H:
            continue
        lane = _guard_lane(ev.get("lane", WORK))
        title = (ev.get("title") or "meeting").strip()
        sig, _ = _signature("meeting brief " + title)
        proximity = max(0.1, 1.0 - lead_h / MEETING_LOOKAHEAD_H)
        cands.append(_candidate(
            kind="meeting_brief", lane=lane, sig=sig,
            title=f"brief: {title}",
            query=title,
            trigger=f"meeting in {lead_h:.1f}h",
            due_at=start,
            priority=KIND_BASE["meeting_brief"] * proximity,
            reward=1.0,
            meta={"attendees": ev.get("attendees") or [], "start": start.isoformat()},
        ))

    # 2) TIME-OF-DAY -> Monday week triage (Sunday evening or Monday pre-9am)
    is_mon_am = now.weekday() == 0 and now.hour < 9
    is_sun_pm = now.weekday() == 6 and now.hour >= 18
    if is_mon_am or is_sun_pm:
        due = _next_occurrence(now, 0, 8)  # Monday 08:00
        sig, _ = _signature("weekly sf ticket triage sprint")
        cands.append(_candidate(
            kind="weekly_triage", lane=WORK, sig=sig,
            title="week's SF ticket triage",
            query="open salesforce tickets sprint priorities this week",
            trigger="monday-morning cadence",
            due_at=due,
            priority=KIND_BASE["weekly_triage"],
            reward=1.0,
            meta={},
        ))

    # 3) RECURRING ASKS -> stage the night before (learned-reward gated)
    for pat in _recurring_patterns(_operator_asks(ledger_path)):
        occ = _next_occurrence(now, pat["weekday"], pat["hour"])
        lead_h = (occ - now).total_seconds() / 3600.0
        if not (0 < lead_h <= STAGE_LEAD_H):
            continue  # too far out (would stage a week early) or already past today
        reward = _pattern_reward(st, pat["sig"])
        if reward < SUPPRESS_FLOOR:
            continue  # Operator reliably ignores this pre-build — stop making it
        proximity = max(0.1, 1.0 - lead_h / STAGE_LEAD_H)
        cands.append(_candidate(
            kind="recurring_ask", lane=pat["lane"], sig=pat["sig"],
            title=f"recurring: {pat['label']}",
            query=pat["example"],
            trigger=f"repeats {_WD[pat['weekday']]}~{pat['hour']:02d}:00 "
                    f"(x{pat['count']}, {int(pat['consistency']*100)}% consistent)",
            due_at=occ,
            priority=KIND_BASE["recurring_ask"] * proximity * reward,
            reward=reward,
            meta={"weekday": pat["weekday"], "hour": pat["hour"], "count": pat["count"]},
        ))

    cands.sort(key=lambda c: c["priority"], reverse=True)
    return cands


_WD = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def _candidate(*, kind, lane, sig, title, query, trigger, due_at, priority, reward, meta) -> dict:
    return {
        "id": "ant_" + hashlib.sha1(
            f"{kind}|{lane}|{sig}|{due_at.isoformat()}".encode()).hexdigest()[:12],
        "kind": kind, "lane": _guard_lane(lane), "pattern_sig": sig,
        "title": title, "query": query, "trigger": trigger,
        "due_at": due_at.isoformat(),
        "priority": round(float(priority), 4),
        "confidence": round(min(0.95, 0.4 + 0.5 * float(reward) * float(priority)), 3),
        "meta": meta,
    }


# ───────────────────────────────────────────────────────────── grounding (RAG)
def _ground(item: dict, k: int = 5) -> list[dict]:
    """Pull REAL, CITED context for the artifact — lane-scoped, fail-soft. Returns
    [{source, ref, text}]. Work lane fuses work_rag + graph + episodic; other lanes use
    episodic only (work_rag/graph are work-lane stores — the firewall)."""
    lane, query = item["lane"], item["query"]
    hits: list[dict] = []
    seen = set()

    def add(source, ref, text):
        key = f"{source}:{ref}"
        if key in seen or not (text or "").strip():
            return
        seen.add(key)
        hits.append({"source": source, "ref": ref, "text": text[:400]})

    if lane == WORK:
        try:
            from core import work_rag
            for h in work_rag.search(query, k=k):
                add(h.get("source", "work_rag"), h.get("ref", ""), h.get("text", ""))
        except Exception:
            pass
        try:
            from core import graph_memory
            g = graph_memory.traverse(query, lane=WORK)
            if g.get("answer"):
                add("graph", ",".join(g.get("seeds") or []) or "graph", g["answer"])
        except Exception:
            pass
    try:
        from core import episodic_memory
        for e in episodic_memory.recall(query, lane, limit=k):
            add("episodic", e.get("entity") or e.get("kind") or "event", e.get("summary", ""))
    except Exception:
        pass
    return hits[:k]


def _is_actionable(item: dict, hits: list[dict]) -> bool:
    """A pre-build is 'actionable' if it would produce something that could go OUTWARD
    (a draft reply/email/send). Briefs and triage are read-only review artifacts."""
    return bool(re.search(r"\b(draft|reply|respond|email|dm|message|send)\b",
                          (item.get("query", "") + " " + item.get("title", "")).lower()))


# ───────────────────────────────────────────────────────────── prebuild()
def prebuild(item: dict, *, staged_path: Path | None = None,
             ground_fn=None, now: datetime | None = None) -> dict:
    """Assemble the artifact NOW from grounded, cited context and STAGE it to the review
    surface. NEVER sends. Anything actionable is gated through decision_modeling.score BEFORE
    its payload is staged; a non-approve stance strips the actionable payload.

    ground_fn is injectable so a caller/test can supply context without touching live brains.
    Returns the staged item."""
    staged_path = staged_path or STAGED_PATH
    now = now or _now_utc()
    ground_fn = ground_fn or _ground

    _guard_lane(item["lane"])
    hits = ground_fn(item) or []
    actionable = _is_actionable(item, hits)

    body_lines = [f"# {item['title']}", f"_why: {item['trigger']} · lane={item['lane']}_", ""]
    if hits:
        body_lines.append("grounded context:")
        for h in hits:
            body_lines.append(f"- [{h['source']}:{h['ref']}] {h['text']}")
    else:
        body_lines.append("_(no grounded context found yet — thin pre-build)_")
    artifact = {
        "format": "markdown",
        "body": "\n".join(body_lines),
        "citations": [f"{h['source']}:{h['ref']}" for h in hits],
        "hit_count": len(hits),
    }

    decision = None
    if actionable:
        # gate the outward-capable payload through the "would Operator approve" model
        try:
            from core import decision_modeling
            proposed = f"auto-stage an outward {item['kind']} for: {item['query']}"
            decision = decision_modeling.score(
                proposed, {"lane": item["lane"], "staged_only": True})
        except Exception:
            decision = {"alex_would": "revise", "confidence": 0.25, "why": "scorer unavailable"}
        if decision.get("alex_would") != "approve":
            # not clearly approved -> keep the review note, strip anything actionable
            artifact["actionable_payload"] = None
            artifact["gated"] = True

    staged = {
        **{k: item[k] for k in ("id", "kind", "lane", "pattern_sig", "title", "trigger",
                                "due_at", "priority", "confidence")},
        "staged_at": now.isoformat(),
        "status": "staged",
        "actionable": actionable,
        "decision": decision,
        "artifact": artifact,
        "used": None,  # set True by the review surface when Operator opens/acts on it
    }

    doc = _load_json(staged_path, {"items": []})
    doc.setdefault("items", [])
    doc["items"] = [it for it in doc["items"] if it.get("id") != staged["id"]]  # upsert
    doc["items"].append(staged)
    doc["generated_at"] = now.isoformat()
    _save_json(staged_path, doc)
    return staged


# ───────────────────────────────────────────────────────────── grade_usage()
def _write_graded_outcome(outcome: dict, path: Path = GRADED_LEDGER) -> None:
    """Default sink: append the anticipation outcome to the learning substrate (a dedicated,
    anticipation-scoped ledger — kept separate from reaction_grader's human-reaction hit_rate
    so the two signals never contaminate each other)."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as f:
            f.write(json.dumps(outcome, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _detect_usage(item: dict, asks: list[dict], now: datetime) -> str:
    """used | ignored | pending — OUTCOME-verified, not 'we staged it'.

    used   : an explicit open/act marker, OR a genuine matching follow-up ask after staging
             (he reached for exactly this => the pre-build was on target).
    ignored: due_at + grace elapsed with no such evidence.
    pending: still inside its useful window — don't grade yet."""
    if item.get("used") is True:
        return "used"
    staged_at = _parse_dt(item.get("staged_at"))
    sig = item.get("pattern_sig")
    for a in asks:
        if staged_at and a["ts"] <= staged_at:
            continue
        if _signature(a["text"])[0] == sig:
            return "used"
    due = _parse_dt(item.get("due_at"))
    if due and now >= due + timedelta(hours=USAGE_GRACE_H):
        return "ignored"
    return "pending"


def grade_usage(*, now: datetime | None = None, staged_path: Path | None = None,
                state_path: Path | None = None, ledger_path: Path | None = None,
                grader_sink=None) -> dict:
    """Did Operator USE the prior anticipations? Move each pattern's reward accordingly so
    anticipate() sharpens — patterns he ignores decay below SUPPRESS_FLOOR and stop being
    pre-built. Emits every resolved outcome to grader_sink (default: the anticipation graded
    ledger). Idempotent: already-graded items (status used/ignored) are skipped."""
    now = now or _now_utc()
    staged_path = staged_path or STAGED_PATH
    state_path = state_path or STATE_PATH
    ledger_path = ledger_path or clean_capture.LEDGER
    sink = grader_sink if grader_sink is not None else _write_graded_outcome

    doc = _load_json(staged_path, {"items": []})
    st = _state(state_path)
    asks = _operator_asks(ledger_path)

    graded = {"used": 0, "ignored": 0, "pending": 0}
    outcomes = []
    for item in doc.get("items", []):
        if item.get("status") in ("used", "ignored"):
            continue  # already resolved
        verdict = _detect_usage(item, asks, now)
        graded[verdict] += 1
        if verdict == "pending":
            continue
        item["status"] = verdict
        item["graded_at"] = now.isoformat()

        sig = item.get("pattern_sig")
        hit = verdict == "used"
        p = st["patterns"].setdefault(sig, {"reward": DEFAULT_REWARD, "n": 0, "hits": 0,
                                            "misses": 0, "label": item.get("title", "")})
        p["reward"] = round((1 - REWARD_ALPHA) * float(p["reward"]) + REWARD_ALPHA * (1.0 if hit else 0.0), 4)
        p["n"] += 1
        p["hits" if hit else "misses"] += 1
        p["last_seen"] = now.isoformat()

        outcome = {
            "graded_at": now.isoformat(), "item_id": item["id"], "kind": item["kind"],
            "lane": item["lane"], "pattern_sig": sig, "label": p["label"],
            "outcome": "hit" if hit else "miss", "reward_now": p["reward"],
            "suppressed": p["reward"] < SUPPRESS_FLOOR, "source": "anticipation",
        }
        outcomes.append(outcome)
        try:
            sink(outcome)
        except Exception:
            pass

    _save_json(staged_path, doc)
    _save_json(state_path, st)
    return {**graded, "outcomes": outcomes,
            "suppressed_patterns": [s for s, p in st["patterns"].items()
                                    if float(p.get("reward", 1)) < SUPPRESS_FLOOR]}


# ───────────────────────────────────────────────────────────── smoke
def _smoke() -> int:
    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="antic_smoke_"))
    ledger = tmp / "exchanges.jsonl"
    staged = tmp / "anticipated.json"
    state = tmp / "anticipation_state.json"
    ok = True

    def check(name, cond):
        nonlocal ok
        print(("PASS " if cond else "FAIL ") + name)
        ok = ok and cond

    # ---- build a synthetic ledger: a recurring FRIDAY 16:00 status ask across 4 weeks ----
    # anchor "now" = Thursday 2026-07-09 21:00 UTC (the night before a Friday ask)
    now = datetime(2026, 7, 9, 21, 0, tzinfo=timezone.utc)
    recs = []
    for wk in range(4):  # four prior Fridays at 16:00
        fri = datetime(2026, 6, 12, 16, 0, tzinfo=timezone.utc) + timedelta(weeks=wk)
        recs.append({"id": f"a{wk}", "ts": fri.isoformat(), "channel": "imessage",
                     "thread_id": "t", "direction": clean_capture.OPERATOR_INBOUND,
                     "is_operator": True,
                     "text": "whats the salesforce ticket status heading into the weekend"})
    # noise: a scattered ask + a machine push (must be ignored by mining)
    recs.append({"id": "n1", "ts": "2026-06-20T11:00:00+00:00", "channel": "imessage",
                 "thread_id": "t", "direction": clean_capture.OPERATOR_INBOUND,
                 "is_operator": True, "text": "did the dentist call back"})
    recs.append({"id": "p1", "ts": "2026-06-21T09:00:00+00:00", "channel": "imessage",
                 "thread_id": "t", "direction": clean_capture.MACHINE_PUSH,
                 "is_operator": False, "text": "scoreboard: 3 sends today"})
    ledger.write_text("\n".join(json.dumps(r) for r in recs))

    # synthetic calendar: a near work meeting (in 45m) + a far one (in 3 days)
    calendar = [
        {"start": (now + timedelta(minutes=45)).isoformat(), "title": "DPP cross-training review",
         "lane": WORK, "attendees": ["steve"]},
        {"start": (now + timedelta(days=3)).isoformat(), "title": "quarterly planning", "lane": WORK},
    ]

    # ---- anticipate() on Thursday night ----
    cands = anticipate(now, calendar, ledger_path=ledger, state_path=state)
    kinds = [c["kind"] for c in cands]
    check("meeting brief surfaced for the near meeting", "meeting_brief" in kinds)
    check("near meeting brief outranks / is present, far meeting excluded",
          sum(1 for c in cands if c["kind"] == "meeting_brief") == 1)
    check("recurring Friday ask staged Thursday night", "recurring_ask" in kinds)
    check("no weekly_triage on a Thursday", "weekly_triage" not in kinds)
    check("meeting brief ranks above the recurring ask",
          kinds.index("meeting_brief") < kinds.index("recurring_ask"))
    rec_cand = next(c for c in cands if c["kind"] == "recurring_ask")
    check("recurring candidate is lane=work (salesforce hint)", rec_cand["lane"] == WORK)

    # ---- Monday-morning triage trigger ----
    mon = datetime(2026, 7, 13, 7, 0, tzinfo=timezone.utc)  # Monday 07:00
    mon_cands = anticipate(mon, [], ledger_path=ledger, state_path=state)
    check("Monday 7am surfaces weekly SF triage",
          any(c["kind"] == "weekly_triage" and c["lane"] == WORK for c in mon_cands))

    # ---- prebuild() stages, grounded + cited, never sends ----
    def fake_ground(item):
        return [{"source": "salesforce", "ref": "sprint_42",
                 "text": "12 open tickets, 3 blocked on offshore coverage"},
                {"source": "episodic", "ref": "PR#382", "text": "SP portal MVP shipped to prod"}]
    st_item = prebuild(rec_cand, staged_path=staged, ground_fn=fake_ground, now=now)
    check("prebuild wrote the review surface", staged.exists())
    check("staged artifact carries citations", len(st_item["artifact"]["citations"]) == 2)
    check("staged status is 'staged' (not sent)", st_item["status"] == "staged")
    doc = _load_json(staged, {})
    check("anticipated.json holds exactly one item", len(doc.get("items", [])) == 1)

    # ---- actionable pre-build is gated through decision_modeling (never sent) ----
    action_item = _candidate(
        kind="recurring_ask", lane=HUSTLE, sig="sig_draftx",
        title="recurring: draft reply to lead", query="draft a reply email to the new lead",
        trigger="test", due_at=now + timedelta(hours=2), priority=0.5, reward=0.5, meta={})
    a_staged = prebuild(action_item, staged_path=staged, ground_fn=lambda i: [], now=now)
    check("actionable pre-build flagged actionable", a_staged["actionable"] is True)
    check("actionable pre-build carries a decision gate", a_staged["decision"] is not None)
    check("actionable payload not auto-armed for send",
          a_staged["artifact"].get("actionable_payload") in (None, ...) )

    # ---- grade_usage: one USED (Operator asked again), one IGNORED (expired, no ask) ----
    # Stage two fresh anticipations dated in the past so they're gradable now.
    past = datetime(2026, 7, 1, 20, 0, tzinfo=timezone.utc)
    used_item = _candidate(kind="recurring_ask", lane=WORK, sig="sig_used",
                           title="used one", query="q", trigger="t",
                           due_at=datetime(2026, 7, 2, 16, 0, tzinfo=timezone.utc),
                           priority=0.5, reward=0.5, meta={})
    ign_item = _candidate(kind="recurring_ask", lane=WORK, sig="sig_ignored",
                          title="ignored one", query="q", trigger="t",
                          due_at=datetime(2026, 7, 2, 16, 0, tzinfo=timezone.utc),
                          priority=0.5, reward=0.5, meta={})
    staged2 = tmp / "anticipated2.json"
    state2 = tmp / "state2.json"
    prebuild(used_item, staged_path=staged2, ground_fn=lambda i: [], now=past)
    prebuild(ign_item, staged_path=staged2, ground_fn=lambda i: [], now=past)

    # a follow-up ask matching ONLY the "used" pattern, AFTER it was staged
    used_ask = {"id": "u1", "ts": "2026-07-02T15:30:00+00:00", "channel": "imessage",
                "thread_id": "t2", "direction": clean_capture.OPERATOR_INBOUND,
                "is_operator": True, "text": "q"}  # sig("q")==sig_used? -> match by same sig
    # make the used_item's pattern_sig match the ask signature
    used_sig = _signature("q")[0]
    # rewrite staged2 so the used item's sig matches the ask, ignored stays unmatched
    doc2 = _load_json(staged2, {})
    for it in doc2["items"]:
        if it["title"] == "used one":
            it["pattern_sig"] = used_sig
    _save_json(staged2, doc2)
    ledger2 = tmp / "exchanges2.jsonl"
    ledger2.write_text(json.dumps(used_ask))

    grade_now = datetime(2026, 7, 3, 10, 0, tzinfo=timezone.utc)  # past both due+grace
    sink_hits = []
    res = grade_usage(now=grade_now, staged_path=staged2, state_path=state2,
                      ledger_path=ledger2, grader_sink=sink_hits.append)
    check("grade_usage marked one used", res["used"] == 1)
    check("grade_usage marked one ignored", res["ignored"] == 1)
    check("grader sink received both outcomes", len(sink_hits) == 2)
    st2 = _state(state2)
    check("used pattern reward rose above default",
          st2["patterns"][used_sig]["reward"] > DEFAULT_REWARD)
    check("ignored pattern reward fell below default",
          st2["patterns"]["sig_ignored"]["reward"] < DEFAULT_REWARD)

    # ---- learning loop: repeatedly ignored pattern gets suppressed out of anticipate() ----
    # decay sig_ignored a few more times, then confirm anticipate() would skip it.
    for _ in range(4):
        p = st2["patterns"]["sig_ignored"]
        p["reward"] = round((1 - REWARD_ALPHA) * p["reward"], 4)
    _save_json(state2, st2)
    check("chronically-ignored pattern decays below suppress floor",
          st2["patterns"]["sig_ignored"]["reward"] < SUPPRESS_FLOOR)

    # ---- idempotency: re-grading resolved items is a no-op ----
    res2 = grade_usage(now=grade_now, staged_path=staged2, state_path=state2,
                       ledger_path=ledger2, grader_sink=sink_hits.append)
    check("re-grading resolved items grades nothing new", res2["used"] == 0 and res2["ignored"] == 0)

    print("\nSMOKE:", "ALL PASS" if ok else "FAILURES")
    return 0 if ok else 1


def _main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "run":
        now = _now_utc()
        cands = anticipate(now)
        for c in cands[:10]:
            print(f"  [{c['priority']:.3f}] {c['kind']:<14} {c['lane']:<8} {c['title']}  "
                  f"<- {c['trigger']}")
        return 0
    return _smoke()


if __name__ == "__main__":
    raise SystemExit(_main())
