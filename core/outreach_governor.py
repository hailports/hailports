#!/usr/bin/env python3
"""Outreach governor — the reputation-safety brain in front of every cold sender.

It lets outbound volume scale AGGRESSIVELY but ONLY while deliverability stays
clean. Four levers, one stable API:

  1. Auto-raising warm-up ramp (per sender domain): effective daily cap =
     min(configured, WARMUP_START + days_since_first_seen*WARMUP_STEP). It raises
     itself as a sender ages — but only while health is green. Mirrors the curve
     in cold_outreach_compliant._warmed_pool (same WARMUP_START/STEP env, same
     first_seen seed) so the two lanes behave identically.
  2. Bounce/complaint auto-throttle: bounce>3% OR complaint>0.5% cuts every
     effective cap 30%; bounce>5% OR sustained complaint>=0.5% PAUSES (can_send
     denies) until the signal clears. Green metrics let the caps keep ramping.
  3. Per-domain velocity + per-sender daily caps: defer >2 sends to one recipient
     domain in 1h, >5 to a brand-new domain in 24h, and deny once a sender hits
     its effective daily cap.
  4. Centralized suppression + dedup: deny if canspam says suppressed or the
     recipient is already in the lane's durable sent log (defense-in-depth behind
     each sender's own checks).

FAIL-SAFE CONTRACT (critical — this sits in the live cron path):
  * can_send FAILS OPEN. If state/metrics cannot be computed (missing/corrupt
    files, parse errors, an empty history, or a sample too small to judge), it
    falls back to the existing static caps and ALLOWS — it never breaks the live
    broken_site_sender 2x/day cron or the cold lane.
  * It only FAILS CLOSED (pauses / denies on the health gate) when it has
    POSITIVE evidence of a bad signal: a real bounce/complaint spike over a
    non-trivial sample. No evidence => no block.
  * It is ADDITIVE and can only TIGHTEN: a sender's own DAILY_CAP / pool cap
    stays the hard floor; the governor may deny earlier, never send more.

API (stable):
  can_send(sender_email, recipient_email, *, lane) -> Decision(allow, reason, effective_cap)
  record_send(sender_email, recipient_email, lane) -> None
  health() -> dict snapshot

State persists in data/hustle/outreach_governor_state.json.
"""
from __future__ import annotations

import json
import math
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, date
from pathlib import Path

ROOT = Path(os.environ.get("CLAUDE_STACK_DIR", Path(__file__).resolve().parent.parent))

STATE_FILE = ROOT / "data" / "hustle" / "outreach_governor_state.json"
PERSONA_MAIL_LOG = ROOT / "data" / "logs" / "persona_mail.jsonl"
SUPPRESSION_LIST = ROOT / "data" / "hustle" / "suppression_list.txt"
BOUNCE_DOMAINS = ROOT / "data" / "hustle" / "bounce_domains.json"
SHIP_QA_LEDGER = ROOT / "data" / "hustle" / "ship_qa_ledger.jsonl"
# Seed warm-up first_seen from the cold lane's own state so the ramps agree.
COLD_STATE_FILE = ROOT / "data" / "hustle" / "cold_outreach_state.json"

# Mirror cold_outreach_compliant's curve (same env vars on purpose).
WARMUP_START = int(os.environ.get("COLD_OUTREACH_WARMUP_START", "20"))
WARMUP_STEP = int(os.environ.get("COLD_OUTREACH_WARMUP_STEP", "15"))
# Senders that skip the ramp (already warmed) — mirrors `"warmed": True` in the pool.
WARMED_SENDERS = frozenset(
    s.strip().lower()
    for s in os.environ.get("COLD_OUTREACH_WARMED_SENDERS", "user@example.com").split(",")
    if s.strip()
)

# Per-lane configured daily cap (the static floor the governor can only tighten).
LANE_DEFAULT_CAP = {
    "cold_outreach": int(os.environ.get("COLD_OUTREACH_PER_SENDER_CAP", "60")),
    "broken_site": int(os.environ.get("BROKEN_SITE_DAILY_CAP", "8")),
}
_FALLBACK_CAP = 60

# Per-lane durable sent logs for centralized dedup (defense-in-depth).
LANE_SENT_LOG = {
    "cold_outreach": (ROOT / "data" / "hustle" / "cold_outreach_sent.jsonl", "email"),
    "broken_site": (ROOT / "data" / "hustle" / "broken_site_sent.jsonl", "to"),
}

# Auto-throttle thresholds.
BOUNCE_THROTTLE = float(os.environ.get("OUTREACH_BOUNCE_THROTTLE", "0.03"))   # >3% -> cut 30%
BOUNCE_PAUSE = float(os.environ.get("OUTREACH_BOUNCE_PAUSE", "0.05"))         # >5% -> pause
COMPLAINT_THROTTLE = float(os.environ.get("OUTREACH_COMPLAINT_THROTTLE", "0.005"))  # >0.5% -> cut
COMPLAINT_PAUSE = float(os.environ.get("OUTREACH_COMPLAINT_PAUSE", "0.005"))        # >=0.5% sustained -> pause
FAIL_THROTTLE = float(os.environ.get("OUTREACH_FAIL_THROTTLE", "0.05"))      # >5% provider failures -> cut
THROTTLE_MULT = float(os.environ.get("OUTREACH_THROTTLE_MULT", "0.70"))      # 30% cut
# Don't judge (and never pause) on a sample too small to be evidence — fail open.
MIN_SAMPLE = int(os.environ.get("OUTREACH_GOVERNOR_MIN_SAMPLE", "20"))
# ...EXCEPT when a small sample is already overwhelming evidence. A flat MIN_SAMPLE cutoff
# ignores 19-bounces-in-19-sends as "insufficient evidence" — by the time the sample is big
# enough to judge, the sending domain is already burned. So below MIN_SAMPLE we escalate on
# the Wilson score LOWER bound: pause only when even the pessimistic-for-pausing end of the
# confidence interval still clears BOUNCE_PAUSE. That keeps fail-open for weak evidence
# (0/19 and 1/19 stay 'unknown') while catching the catastrophic case (4/20 lb=8.1% -> pause).
# Requires an absolute floor of bounces too, so a 1/1 or 2/2 fluke can't pause the lane.
SMALL_SAMPLE_MIN_BOUNCES = int(os.environ.get("OUTREACH_SMALL_SAMPLE_MIN_BOUNCES", "3"))  # 0 disables
HEALTH_WINDOW_HOURS = int(os.environ.get("OUTREACH_GOVERNOR_WINDOW_HOURS", "72"))
HEALTH_TTL_SEC = int(os.environ.get("OUTREACH_GOVERNOR_HEALTH_TTL", "120"))

# Per-domain velocity (allow up to N, deny on the next).
DOMAIN_HOURLY_MAX = int(os.environ.get("OUTREACH_DOMAIN_HOURLY_MAX", "2"))     # >2 in 1h -> defer
NEW_DOMAIN_24H_MAX = int(os.environ.get("OUTREACH_NEW_DOMAIN_24H_MAX", "5"))   # >5 to a fresh domain in 24h -> defer
NEW_DOMAIN_AGE_H = 24


@dataclass
class Decision:
    allow: bool
    reason: str
    effective_cap: int


# ── helpers ───────────────────────────────────────────────────────────────────

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _today() -> str:
    return _now().strftime("%Y-%m-%d")


def _domain_of(email: str) -> str:
    return (email or "").strip().lower().rsplit("@", 1)[-1]


def _lane_cap(lane: str) -> int:
    return LANE_DEFAULT_CAP.get(lane, _FALLBACK_CAP)


# ── state (load / persist; per-sender + per-domain counters, warmup first_seen) ─

_STATE: dict | None = None
_SEED_CACHE: dict | None = None


def _seed_first_seen() -> dict:
    """The cold lane's own first_seen map — reused so the warm-up ramps agree."""
    global _SEED_CACHE
    if _SEED_CACHE is not None:
        return _SEED_CACHE
    seed: dict = {}
    try:
        data = json.loads(COLD_STATE_FILE.read_text())
        seed = {str(k).strip().lower(): v for k, v in (data.get("first_seen") or {}).items()}
    except Exception:
        seed = {}
    _SEED_CACHE = seed
    return seed


def _load_state() -> dict:
    global _STATE
    if _STATE is not None:
        return _STATE
    st = {"senders": {}, "domains": {}, "updated": None}
    try:
        if STATE_FILE.exists():
            loaded = json.loads(STATE_FILE.read_text())
            if isinstance(loaded, dict):
                st.update(loaded)
                st.setdefault("senders", {})
                st.setdefault("domains", {})
    except Exception:
        pass  # corrupt state => start clean (fail open: empty counters never block)
    _STATE = st
    return st


def _save_state(st: dict) -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        st["updated"] = _now().isoformat()
        tmp = STATE_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(st, indent=2, sort_keys=True))
        os.replace(tmp, STATE_FILE)
    except Exception:
        pass  # never raise into the live send loop


def _get_first_seen(st: dict, sender: str) -> str:
    rec = st["senders"].get(sender) or {}
    fs = rec.get("first_seen")
    if fs:
        return fs
    return _seed_first_seen().get(sender) or _today()


def _days_since_first_seen(st: dict, sender: str) -> int:
    try:
        return max(0, (date.fromisoformat(_today()) - date.fromisoformat(_get_first_seen(st, sender))).days)
    except Exception:
        return 0


def _sender_daily_count(st: dict, sender: str) -> int:
    rec = st["senders"].get(sender) or {}
    daily = rec.get("daily") or {}
    return int(daily.get("count", 0)) if daily.get("date") == _today() else 0


def _domain_velocity(st: dict, domain: str) -> tuple[int, int, float | None]:
    """(sends_last_1h, sends_last_24h, hours_since_first_seen|None) for a domain."""
    rec = st["domains"].get(domain) or {}
    events = rec.get("events") or []
    now = _now()
    h1 = h24 = 0
    for iso in events:
        try:
            dt = datetime.fromisoformat(iso)
        except Exception:
            continue
        age = (now - dt).total_seconds()
        if age <= 3600:
            h1 += 1
        if age <= 86400:
            h24 += 1
    age_h = None
    fs = rec.get("first_seen")
    if fs:
        try:
            age_h = (now - datetime.fromisoformat(fs)).total_seconds() / 3600.0
        except Exception:
            age_h = None
    return h1, h24, age_h


# ── health (bounce/complaint/fail rates from local signals — no network) ────────

_HEALTH_OVERRIDE: dict | None = None  # selftest / manual hook
_HEALTH_CACHE: dict = {"ts": 0.0, "val": None}


def _window_start() -> datetime:
    return _now() - timedelta(hours=HEALTH_WINDOW_HOURS)


def _count_sends_failed(cutoff: datetime) -> tuple[int, int]:
    """(sent, send_failed) in the window, from the persona_mail audit log."""
    sent = failed = 0
    for line in PERSONA_MAIL_LOG.read_text(errors="ignore").splitlines():
        if '"event"' not in line:
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        ev = r.get("event")
        if ev not in ("sent", "send_failed"):
            continue
        try:
            if datetime.fromisoformat(r["ts"]) < cutoff:
                continue
        except Exception:
            pass
        if ev == "sent":
            sent += 1
        else:
            failed += 1
    return sent, failed


def _count_bounces_complaints(cutoff: datetime) -> tuple[int, int]:
    """(bounces, complaints) in the window, from the dated auto-suppression tags
    the resend_delivery_monitor writes (auto-bounced/auto-complained/auto-failed)."""
    bounces = complaints = 0
    cutoff_date = cutoff.date()
    pat = re.compile(r"auto-(bounced|complained|failed)\s+(\d{4}-\d{2}-\d{2})")
    for line in SUPPRESSION_LIST.read_text(errors="ignore").splitlines():
        m = pat.search(line)
        if not m:
            continue
        try:
            if date.fromisoformat(m.group(2)) < cutoff_date:
                continue
        except Exception:
            continue
        kind = m.group(1)
        if kind == "complained":
            complaints += 1
        else:  # bounced / failed both hurt deliverability the same way
            bounces += 1
    return bounces, complaints


def _qa_block_rate(cutoff: datetime) -> tuple[int, int]:
    """(blocked, total) email ship-QA decisions in the window — surfaced, soft."""
    blocked = total = 0
    if not SHIP_QA_LEDGER.exists():
        return 0, 0
    for line in SHIP_QA_LEDGER.read_text(errors="ignore").splitlines():
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("kind") != "email":
            continue
        try:
            if datetime.strptime(r["ts"], "%Y-%m-%dT%H:%M:%S") < cutoff.replace(tzinfo=None):
                continue
        except Exception:
            continue
        total += 1
        if not r.get("passed"):
            blocked += 1
    return blocked, total


def _signal_counts() -> dict | None:
    """Raw deliverability counts over the window. None == can't compute => fail open."""
    cutoff = _window_start()
    try:
        sent, failed = _count_sends_failed(cutoff) if PERSONA_MAIL_LOG.exists() else (0, 0)
    except Exception:
        return None
    try:
        bounces, complaints = _count_bounces_complaints(cutoff) if SUPPRESSION_LIST.exists() else (0, 0)
    except Exception:
        bounces = complaints = 0  # missing suppression tags is not evidence of harm
    try:
        qa_blocked, qa_total = _qa_block_rate(cutoff)
    except Exception:
        qa_blocked = qa_total = 0
    return {"sent": sent, "failed": failed, "bounces": bounces,
            "complaints": complaints, "qa_blocked": qa_blocked, "qa_total": qa_total}


def _wilson_lower_bound(k: int, n: int, z: float = 1.96) -> float:
    """Lower bound of the 95% Wilson score interval for k successes in n trials.

    Used to judge a small sample honestly: the bound stays near 0 for a clean or
    ambiguous small sample and rises fast once the observed rate is extreme.
    """
    if n <= 0 or k <= 0:
        return 0.0
    p = k / n
    denom = 1.0 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - margin)


def _classify(c: dict | None) -> dict:
    """Map raw counts -> health snapshot. Pure (no I/O) so the selftest can drive it.

    Fail-OPEN: no counts, or a sample below MIN_SAMPLE => status 'unknown',
    multiplier 1.0 (full static caps), never a pause.
    Fail-CLOSED only on POSITIVE evidence (a real bounce/complaint spike).
    """
    if not c:
        return {"status": "unknown", "throttle_multiplier": 1.0, "reasons": ["no metrics — fail open"],
                "sends": 0, "bounce_rate": 0.0, "complaint_rate": 0.0, "fail_rate": 0.0,
                "qa_block_rate": 0.0, "window_hours": HEALTH_WINDOW_HOURS}
    sends = int(c.get("sent", 0))
    bounces = int(c.get("bounces", 0))
    complaints = int(c.get("complaints", 0))
    failed = int(c.get("failed", 0))
    qa_blocked = int(c.get("qa_blocked", 0))
    qa_total = int(c.get("qa_total", 0))
    qa_rate = (qa_blocked / qa_total) if qa_total else 0.0

    if sends < MIN_SAMPLE:
        # Small sample: still fail open UNLESS the evidence is already overwhelming.
        lb = _wilson_lower_bound(bounces, sends)
        if (SMALL_SAMPLE_MIN_BOUNCES and bounces >= SMALL_SAMPLE_MIN_BOUNCES
                and lb > BOUNCE_PAUSE):
            return {"status": "pause", "throttle_multiplier": 0.0,
                    "reasons": [f"bounce {bounces}/{sends}={bounces / sends:.0%} "
                                f"(95% lower bound {lb:.1%}>{BOUNCE_PAUSE:.0%}) — "
                                f"small sample, overwhelming evidence, PAUSE"],
                    "sends": sends, "bounce_rate": round(bounces / sends, 4),
                    "complaint_rate": round(complaints / sends, 4),
                    "fail_rate": round(failed / sends, 4),
                    "qa_block_rate": round(qa_rate, 4), "window_hours": HEALTH_WINDOW_HOURS}
        return {"status": "unknown", "throttle_multiplier": 1.0,
                "reasons": [f"sample {sends}<{MIN_SAMPLE} — insufficient evidence, fail open"],
                "sends": sends, "bounce_rate": 0.0, "complaint_rate": 0.0, "fail_rate": 0.0,
                "qa_block_rate": round(qa_rate, 4), "window_hours": HEALTH_WINDOW_HOURS}

    bounce_rate = bounces / sends
    complaint_rate = complaints / sends
    fail_rate = failed / sends
    reasons: list[str] = []

    pause = False
    throttle = False
    if bounce_rate > BOUNCE_PAUSE:
        pause = True
        reasons.append(f"bounce {bounce_rate:.1%}>{BOUNCE_PAUSE:.0%} PAUSE")
    elif bounce_rate > BOUNCE_THROTTLE:
        throttle = True
        reasons.append(f"bounce {bounce_rate:.1%}>{BOUNCE_THROTTLE:.0%} throttle")
    # complaints: any rate over threshold throttles; sustained (>=2 events) pauses
    if complaint_rate >= COMPLAINT_PAUSE and complaints >= 2:
        pause = True
        reasons.append(f"complaint {complaint_rate:.2%}>={COMPLAINT_PAUSE:.1%} sustained PAUSE")
    elif complaint_rate > COMPLAINT_THROTTLE:
        throttle = True
        reasons.append(f"complaint {complaint_rate:.2%}>{COMPLAINT_THROTTLE:.1%} throttle")
    if fail_rate > FAIL_THROTTLE:
        throttle = True
        reasons.append(f"provider-fail {fail_rate:.1%}>{FAIL_THROTTLE:.0%} throttle")
    if qa_rate >= 0.5 and qa_total >= MIN_SAMPLE:
        reasons.append(f"ship-QA blocking {qa_rate:.0%} of emails — check discovery")

    status = "pause" if pause else ("throttle" if throttle else "green")
    mult = 0.0 if pause else (THROTTLE_MULT if throttle else 1.0)
    if not reasons:
        reasons = [f"green: bounce {bounce_rate:.1%} complaint {complaint_rate:.2%} over {sends} sends"]
    return {"status": status, "throttle_multiplier": mult, "reasons": reasons,
            "sends": sends, "bounce_rate": round(bounce_rate, 4),
            "complaint_rate": round(complaint_rate, 4), "fail_rate": round(fail_rate, 4),
            "qa_block_rate": round(qa_rate, 4), "window_hours": HEALTH_WINDOW_HOURS}


def health() -> dict:
    """Current deliverability health snapshot (cached HEALTH_TTL_SEC)."""
    if _HEALTH_OVERRIDE is not None:
        return _HEALTH_OVERRIDE
    now = time.time()
    if _HEALTH_CACHE["val"] is not None and (now - _HEALTH_CACHE["ts"]) < HEALTH_TTL_SEC:
        return _HEALTH_CACHE["val"]
    try:
        val = _classify(_signal_counts())
    except Exception:
        val = _classify(None)  # any failure => fail open
    _HEALTH_CACHE["ts"] = now
    _HEALTH_CACHE["val"] = val
    return val


# ── dedup (centralized, defense-in-depth behind each sender's own check) ────────

_DEDUP_CACHE: dict[str, set] = {}


def _lane_dedup_set(lane: str) -> set:
    if lane in _DEDUP_CACHE:
        return _DEDUP_CACHE[lane]
    seen: set = set()
    spec = LANE_SENT_LOG.get(lane)
    if spec:
        log, field = spec
        try:
            if log.exists():
                for line in log.read_text(errors="ignore").splitlines():
                    try:
                        v = json.loads(line).get(field)
                    except Exception:
                        continue
                    if v:
                        seen.add(str(v).strip().lower())
        except Exception:
            pass
    _DEDUP_CACHE[lane] = seen
    return seen


def _effective_cap(st: dict, sender: str, lane: str, h: dict) -> int:
    configured = _lane_cap(lane)
    if sender in WARMED_SENDERS:
        base = configured
    else:
        base = min(configured, WARMUP_START + _days_since_first_seen(st, sender) * WARMUP_STEP)
    mult = float(h.get("throttle_multiplier", 1.0))
    return max(0, int(base * mult))


# ── public API ──────────────────────────────────────────────────────────────

def can_send(sender_email: str, recipient_email: str, *, lane: str) -> Decision:
    """Gate one outbound send. FAILS OPEN on any compute error; FAILS CLOSED only
    on positive evidence (suppression, dedup, a proven bounce/complaint pause, or
    a breached velocity/daily cap). effective_cap is always reported."""
    sender = (sender_email or "").strip().lower()
    recipient = (recipient_email or "").strip().lower()
    try:
        st = _load_state()
    except Exception:
        return Decision(True, "fail_open:state", _lane_cap(lane))
    try:
        h = health()
    except Exception:
        h = {"status": "unknown", "throttle_multiplier": 1.0}
    try:
        eff = _effective_cap(st, sender, lane, h)
    except Exception:
        eff = _lane_cap(lane)

    # 1. suppression (CAN-SPAM opt-out) — always deny
    try:
        from agents.core import canspam
        if recipient and canspam.is_suppressed(recipient):
            return Decision(False, "suppressed", eff)
    except Exception:
        pass  # fail open: can't read suppressions => let the sender's own check handle it

    # 2. centralized dedup
    try:
        if recipient and recipient in _lane_dedup_set(lane):
            return Decision(False, "already_sent_dedup", eff)
    except Exception:
        pass

    # 3. health pause — fail-CLOSED only on a proven bad signal
    if h.get("status") == "pause":
        return Decision(False, "paused:" + "; ".join(h.get("reasons", [])[:2]), eff)

    # 4. per-sender daily cap (eff already reflects warmup + throttle)
    try:
        used = _sender_daily_count(st, sender)
    except Exception:
        used = 0  # fail open
    if eff > 0 and used >= eff:
        return Decision(False, f"sender_daily_cap {used}/{eff}", eff)

    # 5. per-domain velocity
    try:
        dom = _domain_of(recipient)
        h1, h24, age_h = _domain_velocity(st, dom)
        if h1 >= DOMAIN_HOURLY_MAX:
            return Decision(False, f"domain_velocity_1h>={DOMAIN_HOURLY_MAX} ({dom})", eff)
        if age_h is not None and age_h < NEW_DOMAIN_AGE_H and h24 >= NEW_DOMAIN_24H_MAX:
            return Decision(False, f"new_domain_24h>={NEW_DOMAIN_24H_MAX} ({dom})", eff)
    except Exception:
        pass  # fail open

    return Decision(True, h.get("status", "green"), eff)


def record_send(sender_email: str, recipient_email: str, lane: str) -> None:
    """Record a real send so warmup ages, per-sender daily + per-domain velocity
    counters advance, and the dedup cache stays warm. Never raises."""
    try:
        sender = (sender_email or "").strip().lower()
        recipient = (recipient_email or "").strip().lower()
        st = _load_state()
        now_iso = _now().isoformat()
        today = _today()

        srec = st["senders"].setdefault(sender, {})
        srec.setdefault("first_seen", _get_first_seen(st, sender))
        daily = srec.get("daily") or {}
        if daily.get("date") != today:
            daily = {"date": today, "count": 0}
        daily["count"] = int(daily.get("count", 0)) + 1
        srec["daily"] = daily

        dom = _domain_of(recipient)
        if dom:
            drec = st["domains"].setdefault(dom, {})
            drec.setdefault("first_seen", now_iso)
            events = [e for e in (drec.get("events") or []) if _within_24h(e)]
            events.append(now_iso)
            drec["events"] = events[-50:]  # bound growth

        _save_state(st)
        if recipient:
            _lane_dedup_set(lane).add(recipient)
    except Exception:
        pass


def _within_24h(iso: str) -> bool:
    try:
        return (_now() - datetime.fromisoformat(iso)).total_seconds() <= 86400
    except Exception:
        return False


# ── selftest / CLI ────────────────────────────────────────────────────────────

def _selftest() -> int:
    global _STATE, _HEALTH_OVERRIDE, _DEDUP_CACHE
    import tempfile
    failures: list[str] = []

    def reset(state=None):
        global _STATE, _DEDUP_CACHE, _HEALTH_OVERRIDE
        _STATE = state if state is not None else {"senders": {}, "domains": {}, "updated": None}
        _DEDUP_CACHE = {}

    # 1. _classify fail-OPEN on no metrics + tiny sample
    if _classify(None)["status"] != "unknown":
        failures.append("no-metrics did not fail open to 'unknown'")
    # A tiny sample must still fail open when the evidence is weak: a clean small sample,
    # and a small sample whose bounce count is under the absolute floor.
    if _classify({"sent": 19, "bounces": 0})["status"] != "unknown":
        failures.append("clean below-MIN_SAMPLE did not fail open")
    if _classify({"sent": 19, "bounces": 2})["status"] != "unknown":
        failures.append("below-MIN_SAMPLE under bounce floor did not fail open")

    # 1b. ...but an overwhelming small sample MUST pause (a flat cutoff would burn the domain
    # before the sample ever got big enough to judge).
    for sent, bounces in ((5, 5), (20, 4), (12, 3)):
        sp = _classify({"sent": sent, "bounces": bounces})
        if sp["status"] != "pause" or sp["throttle_multiplier"] != 0.0:
            failures.append(f"overwhelming small sample {bounces}/{sent} did not pause: {sp['status']}")

    # 2. green metrics => green, full multiplier
    g = _classify({"sent": 500, "bounces": 5, "complaints": 0, "failed": 2})
    if g["status"] != "green" or g["throttle_multiplier"] != 1.0:
        failures.append(f"clean metrics not green: {g['status']}")

    # 3. throttle on bounce>3%
    t = _classify({"sent": 500, "bounces": 20, "complaints": 0, "failed": 0})  # 4%
    if t["status"] != "throttle" or t["throttle_multiplier"] != THROTTLE_MULT:
        failures.append(f"bounce>3% did not throttle: {t['status']}")

    # 4. pause on bounce>5% (fail CLOSED on positive evidence)
    p = _classify({"sent": 500, "bounces": 30, "complaints": 0, "failed": 0})  # 6%
    if p["status"] != "pause" or p["throttle_multiplier"] != 0.0:
        failures.append(f"bounce>5% did not pause: {p['status']}")

    # 5. sustained complaint pause
    cp = _classify({"sent": 1000, "bounces": 0, "complaints": 5, "failed": 0})  # 0.5%
    if cp["status"] != "pause":
        failures.append(f"sustained complaint did not pause: {cp['status']}")

    # 6. warm-up ramp: fresh non-warmed sender capped at WARMUP_START; warmed at full
    reset()
    _HEALTH_OVERRIDE = {"status": "green", "throttle_multiplier": 1.0, "reasons": []}
    st = _load_state()
    fresh = "user@example.com"
    cap0 = _effective_cap(st, fresh, "cold_outreach", _HEALTH_OVERRIDE)
    if cap0 != min(_lane_cap("cold_outreach"), WARMUP_START):
        failures.append(f"fresh-sender warmup cap wrong: {cap0} (want {min(_lane_cap('cold_outreach'), WARMUP_START)})")
    warm = next(iter(WARMED_SENDERS))
    capw = _effective_cap(st, warm, "cold_outreach", _HEALTH_OVERRIDE)
    if capw != _lane_cap("cold_outreach"):
        failures.append(f"warmed sender not at full cap: {capw}")
    # ramp raises with age
    st["senders"][fresh] = {"first_seen": (date.fromisoformat(_today()) - timedelta(days=2)).isoformat()}
    cap2 = _effective_cap(st, fresh, "cold_outreach", _HEALTH_OVERRIDE)
    if cap2 <= cap0:
        failures.append(f"warmup did not auto-raise with age: {cap0}->{cap2}")

    # 7. throttle cuts the effective cap ~30%
    throttled_h = {"status": "throttle", "throttle_multiplier": THROTTLE_MULT, "reasons": []}
    capt = _effective_cap(st, warm, "cold_outreach", throttled_h)
    if capt != int(_lane_cap("cold_outreach") * THROTTLE_MULT):
        failures.append(f"throttle did not cut cap: {capt}")

    # 8. per-sender daily cap denies once hit
    reset()
    _HEALTH_OVERRIDE = {"status": "green", "throttle_multiplier": 1.0, "reasons": []}
    orig = STATE_FILE
    try:
        tmp = Path(tempfile.mkdtemp()) / "gov_state.json"
        globals()["STATE_FILE"] = tmp
        s = "user@example.com"  # broken_site, cap 8
        # fill to cap with a fresh domain each time so velocity never trips first
        for i in range(_lane_cap("broken_site")):
            record_send(s, f"a{i}@biz{i}.com", "broken_site")
        d = can_send(s, "user@example.com", lane="broken_site")
        if d.allow:
            failures.append("per-sender daily cap did not deny at cap")

        # 9. per-domain 1h velocity: 2 to a domain ok, 3rd denied
        reset()
        for i in range(DOMAIN_HOURLY_MAX):
            record_send("user@example.com", f"r{i}@samedomain.com", "cold_outreach")
        dv = can_send("user@example.com", "user@example.com", lane="cold_outreach")
        if dv.allow:
            failures.append("per-domain 1h velocity did not defer the 3rd send")

        # 10. dedup denies a recipient already sent
        reset()
        record_send("user@example.com", "user@example.com", "cold_outreach")
        if can_send("user@example.com", "user@example.com", lane="cold_outreach").allow:
            failures.append("dedup did not deny an already-sent recipient")

        # 11. fail-CLOSED: a pause health denies; 12. fail-OPEN: unknown allows
        reset()
        _HEALTH_OVERRIDE = {"status": "pause", "throttle_multiplier": 0.0, "reasons": ["bounce 6% PAUSE"]}
        if can_send("user@example.com", "user@example.com", lane="cold_outreach").allow:
            failures.append("pause signal did not fail closed (deny)")
        _HEALTH_OVERRIDE = {"status": "unknown", "throttle_multiplier": 1.0, "reasons": ["no metrics"]}
        reset()
        if not can_send("user@example.com", "user@example.com", lane="cold_outreach").allow:
            failures.append("unknown health did not fail open (allow)")
    finally:
        globals()["STATE_FILE"] = orig
        _HEALTH_OVERRIDE = None
        reset()

    if failures:
        print("OUTREACH_GOVERNOR SELFTEST FAILED:")
        for f in failures:
            print("  -", f)
        return 1
    print("OUTREACH_GOVERNOR SELFTEST PASSED (12 checks: warmup, throttle, pause, "
          "per-sender cap, per-domain velocity, dedup, fail-open/closed)")
    return 0


def _cli(argv=None) -> int:
    import sys
    argv = argv if argv is not None else sys.argv[1:]
    if "--selftest" in argv:
        return _selftest()
    # default: print the live health snapshot (read-only, $0)
    print(json.dumps(health(), indent=2))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_cli())
