#!/usr/bin/env python3
"""pattern_automation.py — the ANTICIPATION layer: "Operator always does X at trigger T" -> stage X.

The problem this closes: there are actions Operator does on a stable cadence — submits the timecard
Friday, checks ticket status Monday AM, reviews PRs after standup. Today he has to remember and
do each one manually. This module MINES that regularity deterministically, and when a pattern is
due, STAGES the finished thing on the review surface for a one-tap yes. It never nags ("hey, it's
Friday, submit your timecard") — per the silent-self-correct mandate it surfaces a READY artifact,
not a reminder.

learn_patterns(actions=None) -> list[Pattern]
  Mines clean_capture (operator turns) + episodic (operator actions) for RECURRING actions with a
  stable cadence/trigger. DETERMINISTIC: frequency + regularity only, no LLM. A candidate is only
  surfaced when seen >= MIN_OCCURRENCES times with LOW variance (coefficient-of-variation on the
  inter-arrival gaps under MAX_CV) and, for a weekday-anchored cadence, a dominant-weekday share
  over MIN_DOW_FRACTION. Scattered/one-off noise fails all three and is dropped.

propose(pattern, now, ...) -> Proposal
  For a DUE pattern, STAGE the action to the anticipated review surface, gated through
  decision_modeling.score. Precedence is honest: the score runs on the pattern's REAL underlying
  action wrapped in the staging intent, so a hard-rail violation (e.g. a recurring "mass-email the
  list") still REJECTS and is never even offered as a one-tap. NEVER auto-executes an outward/
  irreversible action (submit/send/post/deploy) — those are staged for his one-tap yes. Reversible
  internal prep is flagged autorun-eligible for the caller; propose() itself only stages.

HARD LANE FIREWALL: the lane is part of every pattern key, so a hustle cadence and a work cadence
never merge into one pattern. Loaders are fail-soft (a cold/missing store returns []).

SILENT: nothing here texts Operator or calls alert_gateway. staging writes a ready card to a review-
surface file (or returns it when no path is given). No send, no outward execution, ever.

    .venv/bin/python -m core.pattern_automation          # run the smoke proof (dry, no side effects)
"""
from __future__ import annotations

import json
import re
import sqlite3
import statistics
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path

if __package__ in (None, ""):  # allow `python core/pattern_automation.py`
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import BASE_DIR

try:
    from core import decision_modeling as _dm
except Exception:  # pragma: no cover - decision layer is a hard dep but stay fail-soft
    _dm = None

# ─────────────────────────────────────────────────────────────────── tunables
MIN_OCCURRENCES = 3        # a pattern must be seen at least this many times
MAX_CV = 0.40             # low-variance gate: stdev/mean of inter-arrival gaps must be <= this
MIN_DOW_FRACTION = 0.70   # weekday-anchored: dominant weekday must own this share of occurrences
MIN_REGULARITY = 0.60     # combined cadence-confidence floor

_WEEKLY_GAP = (5.0, 9.0)
_BIWEEKLY_GAP = (12.0, 16.0)
_MONTHLY_GAP = (26.0, 33.0)
_DAILY_GAP = (0.5, 1.5)

_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")

DEFAULT_SURFACE = BASE_DIR / "data" / "learning" / "anticipation_queue.md"

# operator-action event kinds worth anticipating (episodic). system-noise kinds excluded.
_OPERATOR_KINDS = ("send", "decision", "ticket_change", "deploy", "fix")

# outward / irreversible verbs — these NEVER auto-execute; staged for a one-tap yes.
_OUTWARD = re.compile(
    r"\b(submit|send|post|publish|deploy|pay|wire|purchase|buy|delete|email|dm|"
    r"message|approve|file|blast|mass|sign|wire|release|merge|push)\b",
    re.IGNORECASE,
)

_STOP = set(
    "the a an is are to of and or for in on at it that this be i you we my your our with "
    "please can could would do does did just now today then again pull get check review "
    "want need me him her them so as by from up out".split()
)
_WORD = re.compile(r"[a-z][a-z0-9]*", re.IGNORECASE)


# ─────────────────────────────────────────────────────────────────── model
@dataclass
class Pattern:
    key: str
    lane: str
    label: str                # human phrase, e.g. "submit my timecard"
    representative: str        # the modal raw action text (fed to the decision gate)
    count: int
    cadence: str              # weekly | weekdays | biweekly | monthly | daily
    trigger: dict             # {type, weekday?, weekday_name?, hour}
    regularity: float
    variance: float           # coefficient of variation on gaps (lower = tighter)
    last_seen: str
    samples: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


# ─────────────────────────────────────────────────────────────────── time helpers
def _parse_ts(v) -> datetime | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        try:
            return datetime.fromtimestamp(float(v), tz=timezone.utc)
        except Exception:
            return None
    s = str(v).strip()
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        pass
    try:  # bare epoch smuggled as a string
        return datetime.fromtimestamp(float(s), tz=timezone.utc)
    except Exception:
        return None


def _hour_bucket(h: int) -> str:
    if h < 5:
        return "night"
    if h < 9:
        return "early"
    if h < 12:
        return "morning"
    if h < 17:
        return "afternoon"
    if h < 21:
        return "evening"
    return "late"


# ─────────────────────────────────────────────────────────────────── canonicalization
def _content_tokens(text: str) -> list[str]:
    toks = []
    for m in _WORD.finditer(text.lower()):
        w = m.group(0)
        if w in _STOP or w.isdigit() or len(w) < 2:
            continue
        if w.endswith("s") and len(w) > 3:  # light singularize
            w = w[:-1]
        toks.append(w)
    return toks


def _canon_key(lane: str, text: str) -> str | None:
    """Lane-scoped canonical signature = sorted salient tokens. Lane is baked in so a hustle
    cadence can never merge with a work cadence (HARD firewall)."""
    toks = sorted(set(_content_tokens(text)))
    if not toks:
        return None
    return lane + "::" + "|".join(toks)


def _jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 1.0
    u = a | b
    return len(a & b) / len(u) if u else 0.0


# ─────────────────────────────────────────────────────────────────── loaders (fail-soft)
def _load_capture_actions(ledger_path: Path, default_lane: str) -> list[dict]:
    out = []
    try:
        from core import clean_capture as cc
        for rec in cc._iter_records(ledger_path):
            if rec.get("direction") != cc.OPERATOR_INBOUND:
                continue
            text = (rec.get("text") or "").strip()
            if not text:
                continue
            out.append({"ts": rec.get("ts"), "text": text, "lane": default_lane,
                        "source": "capture"})
    except Exception:
        return []
    return out


def _load_episodic_actions(db_path: Path, lanes) -> list[dict]:
    out = []
    try:
        if not Path(db_path).exists():
            return []
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        try:
            qmarks = ",".join("?" * len(_OPERATOR_KINDS))
            rows = con.execute(
                f"SELECT ts, lane, summary FROM events WHERE kind IN ({qmarks})",
                _OPERATOR_KINDS,
            ).fetchall()
        finally:
            con.close()
        want = set(lanes) if lanes else None
        for r in rows:
            lane = r["lane"]
            if want and lane not in want:
                continue
            text = (r["summary"] or "").strip()
            if text:
                out.append({"ts": r["ts"], "text": text, "lane": lane, "source": "episodic"})
    except Exception:
        return []
    return out


def _gather(actions, ledger_path, db_path, default_lane, lanes) -> list[dict]:
    if actions is not None:
        return list(actions)
    merged = []
    merged += _load_capture_actions(ledger_path or (BASE_DIR / "data" / "learning" / "exchanges.jsonl"),
                                    default_lane)
    merged += _load_episodic_actions(db_path or (BASE_DIR / "data" / "learning" / "episodic.sqlite"),
                                     lanes)
    return merged


# ─────────────────────────────────────────────────────────────────── cadence detection
def _classify_cadence(dts: list[datetime]):
    """Return (cadence, trigger, regularity, cv) or (None, ...) if no stable cadence.
    Deterministic: gap regularity + dominant-weekday share."""
    dts = sorted(dts)
    n = len(dts)
    if n < MIN_OCCURRENCES:
        return None, None, 0.0, 1.0

    gaps = [(dts[i + 1] - dts[i]).total_seconds() / 86400.0 for i in range(n - 1)]
    gaps = [g for g in gaps if g >= 0]
    if len(gaps) < 2:
        return None, None, 0.0, 1.0
    mean_gap = statistics.mean(gaps)
    if mean_gap <= 0:
        return None, None, 0.0, 1.0
    cv = statistics.stdev(gaps) / mean_gap
    gap_score = max(0.0, 1.0 - cv)  # cv 0 -> 1.0, cv 0.4 -> 0.6

    wd_counts = Counter(d.weekday() for d in dts)
    dom_wd, dom_n = wd_counts.most_common(1)[0]
    dom_frac = dom_n / n
    typical_hour = int(round(statistics.median(d.hour for d in dts)))

    def _in(rng):
        return rng[0] <= mean_gap <= rng[1]

    all_weekdays = all(d.weekday() < 5 for d in dts)

    cadence = None
    trigger = None
    regularity = 0.0
    if _in(_WEEKLY_GAP):
        if dom_frac >= MIN_DOW_FRACTION:
            cadence = "weekly"
            trigger = {"type": "weekly", "weekday": dom_wd, "weekday_name": _WEEKDAYS[dom_wd],
                       "hour": typical_hour, "hour_bucket": _hour_bucket(typical_hour)}
            regularity = 0.5 * gap_score + 0.5 * dom_frac
    elif _in(_DAILY_GAP):
        cadence = "weekdays" if all_weekdays else "daily"
        trigger = {"type": cadence, "hour": typical_hour, "hour_bucket": _hour_bucket(typical_hour)}
        regularity = gap_score
    elif _in(_BIWEEKLY_GAP) and dom_frac >= MIN_DOW_FRACTION:
        cadence = "biweekly"
        trigger = {"type": "biweekly", "weekday": dom_wd, "weekday_name": _WEEKDAYS[dom_wd],
                   "hour": typical_hour, "hour_bucket": _hour_bucket(typical_hour)}
        regularity = 0.5 * gap_score + 0.5 * dom_frac
    elif _in(_MONTHLY_GAP):
        cadence = "monthly"
        trigger = {"type": "monthly", "day": int(round(statistics.median(d.day for d in dts))),
                   "hour": typical_hour, "hour_bucket": _hour_bucket(typical_hour)}
        regularity = gap_score

    if cadence is None:
        return None, None, regularity, cv
    return cadence, trigger, regularity, cv


# ─────────────────────────────────────────────────────────────────── learn
def learn_patterns(actions=None, *, ledger_path: Path | None = None, db_path: Path | None = None,
                   default_lane: str = "hustle", lanes=None) -> list[Pattern]:
    """Mine recurring operator actions with a stable cadence. Returns accepted Patterns only
    (seen >= MIN_OCCURRENCES, cv <= MAX_CV, regularity >= MIN_REGULARITY). Noise is dropped."""
    raw = _gather(actions, ledger_path, db_path, default_lane, lanes)

    # group by lane-scoped canonical signature
    groups: dict[str, list[dict]] = defaultdict(list)
    tokset: dict[str, set] = {}
    for a in raw:
        lane = str(a.get("lane") or default_lane)
        text = str(a.get("text") or "")
        dt = _parse_ts(a.get("ts"))
        if dt is None:
            continue
        key = _canon_key(lane, text)
        if key is None:
            continue
        groups[key].append({"dt": dt, "text": text, "lane": lane, "source": a.get("source")})
        tokset.setdefault(key, set(_content_tokens(text)))

    # merge near-duplicate signatures (same lane, jaccard >= 0.6) to tolerate phrasing drift.
    # deterministic: process keys largest-group-first, stable secondary sort by key.
    order = sorted(groups, key=lambda k: (-len(groups[k]), k))
    merged: dict[str, list[dict]] = {}
    merged_tok: dict[str, set] = {}
    for k in order:
        lane_k = k.split("::", 1)[0]
        placed = False
        for mk in merged:
            if mk.split("::", 1)[0] != lane_k:
                continue
            if _jaccard(tokset[k], merged_tok[mk]) >= 0.6:
                merged[mk].extend(groups[k])
                placed = True
                break
        if not placed:
            merged[k] = list(groups[k])
            merged_tok[k] = set(tokset[k])

    patterns: list[Pattern] = []
    for key, items in merged.items():
        if len(items) < MIN_OCCURRENCES:
            continue
        dts = [it["dt"] for it in items]
        cadence, trigger, regularity, cv = _classify_cadence(dts)
        if cadence is None or cv > MAX_CV or regularity < MIN_REGULARITY:
            continue
        texts = [it["text"] for it in items]
        representative = Counter(texts).most_common(1)[0][0]
        lane = items[0]["lane"]
        last = max(dts)
        patterns.append(Pattern(
            key=key,
            lane=lane,
            label=_label_from(representative),
            representative=representative,
            count=len(items),
            cadence=cadence,
            trigger=trigger,
            regularity=round(regularity, 3),
            variance=round(cv, 3),
            last_seen=last.isoformat(),
            samples=sorted({t for t in texts})[:5],
        ))

    patterns.sort(key=lambda p: (-p.count, -p.regularity, p.key))
    return patterns


def _label_from(text: str) -> str:
    t = re.sub(r"\s+", " ", text).strip().lower()
    t = re.sub(r"\b(for|this|next|the) (week|month|sprint|day|morning)\b", "", t).strip()
    return t or text.strip().lower()


# ─────────────────────────────────────────────────────────────────── due + propose
def _is_due(pattern: Pattern, now: datetime) -> tuple[bool, str]:
    trig = pattern.trigger or {}
    ttype = trig.get("type")
    hour = int(trig.get("hour", 9))
    if ttype in ("weekly", "biweekly"):
        wd = trig.get("weekday")
        period = f"{now.isocalendar().year}-W{now.isocalendar().week:02d}"
        due = (now.weekday() == wd) and (now.hour >= hour)
        return due, period
    if ttype in ("daily", "weekdays"):
        if ttype == "weekdays" and now.weekday() >= 5:
            return False, now.date().isoformat()
        return now.hour >= hour, now.date().isoformat()
    if ttype == "monthly":
        day = int(trig.get("day", 1))
        period = f"{now.year}-{now.month:02d}"
        return (now.day >= day) and (now.hour >= hour), period
    return False, now.isoformat()


def _decision_string(pattern: Pattern) -> str:
    """Honest composed action fed to decision_modeling: the staging intent (reversible, one-tap)
    AROUND the pattern's REAL underlying action — so a hard-rail violation in the underlying
    action still REJECTS (reject dominates approve) and is never offered as a one-tap."""
    return ("reversible, identity-safe: stage a ready draft on the review surface for one-tap "
            f"approval of this recurring action — {pattern.representative} — no autonomous "
            "submit/send")


def _stage_card(pattern: Pattern, period: str, decision: dict, requires_tap: bool) -> str:
    conf = decision.get("confidence")
    verb = "one-tap 'yes' to run — no auto-submit" if requires_tap else "ready; autorun-eligible"
    return (
        f"## [ready] {pattern.label} — {pattern.cadence} pattern (seen {pattern.count}x)\n"
        f"- action: {pattern.representative}\n"
        f"- trigger: {pattern.trigger.get('weekday_name', pattern.trigger.get('type'))} "
        f"~{pattern.trigger.get('hour')}:00 ({pattern.trigger.get('hour_bucket', '')})\n"
        f"- period: {period}\n"
        f"- regularity: {pattern.regularity} / variance(cv): {pattern.variance}\n"
        f"- decision: {decision.get('alex_would')} (conf {conf})\n"
        f"- {verb}\n"
    )


def propose(pattern: Pattern, now: datetime, *, surface_path: Path | None = None,
            staged_periods: set | None = None, score_fn=None) -> dict:
    """For a DUE, decision-approved pattern, STAGE a ready card to the review surface for a
    one-tap yes. Returns a Proposal dict. staged=True ONLY when due AND not rejected. Never sends,
    never auto-executes an outward action. Reversible internal prep is flagged autorun-eligible for
    the caller; propose() itself only stages.

    staged_periods (optional) dedups: a pattern already staged for its current period is skipped.
    """
    due, period = _is_due(pattern, now)
    proposal = {
        "pattern_key": pattern.key,
        "lane": pattern.lane,
        "label": pattern.label,
        "due": due,
        "period": period,
        "staged": False,
        "requires_tap": True,
        "autorun_allowed": False,
        "decision": None,
        "reason": "",
        "card": None,
    }
    if not due:
        proposal["reason"] = "not due — trigger conditions not met at `now`"
        return proposal
    if staged_periods is not None and (pattern.key, period) in staged_periods:
        proposal["reason"] = "already staged this period (idempotent)"
        return proposal

    scorer = score_fn or (_dm.score if _dm else None)
    if scorer is None:
        proposal["reason"] = "decision layer unavailable — refuse to stage un-gated"
        return proposal
    decision = scorer(_decision_string(pattern))
    proposal["decision"] = decision
    if decision.get("alex_would") == "reject":
        proposal["reason"] = "decision gate REJECTED — Operator would not approve; not staged: " \
            + str(decision.get("why", ""))
        return proposal

    is_outward = bool(_OUTWARD.search(pattern.representative))
    requires_tap = is_outward  # outward/irreversible ALWAYS needs his one-tap
    autorun_allowed = (not is_outward) and decision.get("alex_would") == "approve"
    card = _stage_card(pattern, period, decision, requires_tap)

    if surface_path is not None:
        try:
            p = Path(surface_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            with p.open("a", encoding="utf-8") as f:
                f.write(card + "\n")
        except Exception:
            pass  # staging is best-effort; the proposal itself carries the card

    proposal.update({
        "staged": True,
        "requires_tap": requires_tap,
        "autorun_allowed": autorun_allowed,
        "reason": "staged for one-tap" if requires_tap else "staged (reversible prep, autorun-eligible)",
        "card": card,
    })
    if staged_periods is not None:
        staged_periods.add((pattern.key, period))
    return proposal


def run(now: datetime | None = None, *, actions=None, surface_path: Path | None = None,
        **learn_kw) -> list[dict]:
    """Convenience: learn then propose every due pattern. Dry unless surface_path is given."""
    now = now or datetime.now(timezone.utc)
    pats = learn_patterns(actions=actions, **learn_kw)
    seen: set = set()
    return [propose(p, now, surface_path=surface_path, staged_periods=seen) for p in pats]


# ─────────────────────────────────────────────────────────────────── smoke
def _smoke() -> int:
    from datetime import timedelta
    ok = True

    def check(cond, msg):
        nonlocal ok
        print(("  ok  " if cond else " FAIL ") + msg)
        ok = ok and cond

    base = datetime(2026, 5, 1, tzinfo=timezone.utc)  # 2026-05-01 is a Friday
    check(base.weekday() == 4, "anchor 2026-05-01 is a Friday")

    actions = []
    # SIGNAL 1: submits timecard every Friday ~16:00 for 8 weeks (clean weekly cadence)
    for w in range(8):
        d = base + timedelta(weeks=w, hours=16)
        actions.append({"ts": d.isoformat(), "text": "submit my timecard for this week",
                        "lane": "hustle", "source": "capture"})
    # SIGNAL 2: checks ticket status every Monday ~9:00 for 6 weeks
    mon = base + timedelta(days=3)  # first Monday after
    for w in range(6):
        d = mon + timedelta(weeks=w, hours=9)
        actions.append({"ts": d.isoformat(), "text": "check ticket status for the sprint",
                        "lane": "hustle", "source": "capture"})
    # SIGNAL 3 (hard-rail): recurring Friday "mass-email the leads list" — a real cadence, but the
    # decision gate must REJECT it (snipe-not-spray) and never stage it.
    for w in range(5):
        d = base + timedelta(weeks=w, hours=10)
        actions.append({"ts": d.isoformat(), "text": "mass-email the entire leads list",
                        "lane": "hustle", "source": "capture"})
    # NOISE: scattered one-offs and irregular actions — must NOT surface as a pattern.
    noise = [
        ("2026-05-02T11:00:00+00:00", "restart the colima vm"),
        ("2026-05-06T14:00:00+00:00", "fix the mockup deploy timeout"),
        ("2026-05-19T08:00:00+00:00", "reply to the reddit thread"),
        ("2026-05-20T20:00:00+00:00", "restart the colima vm"),   # 2x, 18 days apart, high cv
        ("2026-06-11T13:00:00+00:00", "review the gumroad payout"),
    ]
    for ts, txt in noise:
        actions.append({"ts": ts, "text": txt, "lane": "hustle", "source": "capture"})

    pats = learn_patterns(actions=actions)
    labels = {p.label: p for p in pats}
    print("\n-- learned patterns --")
    for p in pats:
        print(f"   [{p.cadence}] {p.label!r} x{p.count} reg={p.regularity} cv={p.variance} "
              f"trig={p.trigger.get('weekday_name', p.trigger.get('type'))}")

    tc = next((p for p in pats if "timecard" in p.label), None)
    tk = next((p for p in pats if "ticket" in p.label), None)
    spray = next((p for p in pats if "mass-email" in p.representative or "leads" in p.label), None)

    check(tc is not None, "isolated the weekly TIMECARD pattern")
    check(tk is not None, "isolated the Monday TICKET-STATUS pattern")
    check(tc is not None and tc.cadence == "weekly" and tc.trigger.get("weekday_name") == "Friday",
          "timecard cadence=weekly, trigger=Friday")
    check(tk is not None and tk.trigger.get("weekday_name") == "Monday",
          "ticket-status trigger=Monday")
    # noise must be excluded
    noise_leak = [p for p in pats if any(w in p.label for w in ("colima", "reddit", "mockup", "gumroad"))]
    check(not noise_leak, "NOISE excluded (no colima/reddit/mockup/gumroad pattern)")

    if _dm is None:
        print(" FAIL decision_modeling unavailable"); return 1

    # --- propose: DUE Friday 16:00 -> timecard staged, one-tap (outward, never auto-submit) ---
    friday_pm = datetime(2026, 6, 26, 16, 30, tzinfo=timezone.utc)  # a Friday, past 16:00
    check(friday_pm.weekday() == 4, "test-now 2026-06-26 is a Friday")
    prop = propose(tc, friday_pm)
    check(prop["due"] and prop["staged"], "timecard STAGED when due (Friday PM)")
    check(prop["requires_tap"] and not prop["autorun_allowed"],
          "timecard requires one-tap, NOT autorun (outward/irreversible submit)")
    check(prop["decision"]["alex_would"] in ("approve", "revise"),
          f"timecard decision-approved (={prop['decision']['alex_would']})")
    check("[ready]" in (prop["card"] or ""), "staged a READY card (not a nag)")

    # --- propose: NOT due (Tuesday) -> not staged ---
    tuesday = datetime(2026, 6, 23, 16, 30, tzinfo=timezone.utc)
    check(tuesday.weekday() == 1, "test-now 2026-06-23 is a Tuesday")
    prop_nd = propose(tc, tuesday)
    check(not prop_nd["due"] and not prop_nd["staged"], "timecard NOT staged when not due (Tuesday)")

    # --- propose: too early Friday morning -> not due yet ---
    friday_am = datetime(2026, 6, 26, 8, 0, tzinfo=timezone.utc)
    prop_am = propose(tc, friday_am)
    check(not prop_am["due"], "timecard NOT due Friday 08:00 (before typical 16:00)")

    # --- decision gate blocks the spray pattern even though it's a real cadence + due ---
    if spray is not None:
        prop_spray = propose(spray, base + timedelta(weeks=1, hours=10))  # a due Friday
        check(not prop_spray["staged"] and prop_spray["decision"]["alex_would"] == "reject",
              "spray pattern REJECTED by decision gate — never staged")
    else:
        check(True, "spray cadence not surfaced (also acceptable)")

    # --- idempotency: same period not staged twice ---
    seen: set = set()
    p1 = propose(tc, friday_pm, staged_periods=seen)
    p2 = propose(tc, friday_pm, staged_periods=seen)
    check(p1["staged"] and not p2["staged"], "idempotent — second propose in same period skipped")

    print("\n" + ("SMOKE PASS" if ok else "SMOKE FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(_smoke())
