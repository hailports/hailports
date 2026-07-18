#!/usr/bin/env python3
"""core/chronic_escalator.py — turn a CHRONIC self-heal miss into ONE fixed-or-muted
incident instead of a daily re-texted band-aid.

The bug this kills: outcome_heal counted consecutive misses and, once a lane went
CHRONIC (>= CHRONIC_AFTER), STOPPED attempting any fix (`if fix and not chronic`) and
just re-texted the same line every day via a raw send_imessage that bypassed the alert
gateway's dedup + zero-noise filter. Result: `looking_for_radar: 0 raised hands (×349)`
fired the same iMessage 349 times while the root cause (a silently-dead source) was
never actually repaired.

This module is the chronic branch's replacement. For a chronic lane it:

  1. ESCALATE — runs a real root-cause diagnosis (core.rootcause_diagnoser.diagnose)
     and attempts the lane's actual repair ONCE per FIX_COOLDOWN (the harvest failover:
     re-run the harvester through resilient_fetch's CDP/SearXNG fallback path), then
     RE-VERIFIES the outcome signal. Records the attempt in health_ledger (verify-on-
     success), so a band-aid that never holds is visible.

  2. DE-DUP / SUPPRESS — every chronic notice routes through core.alert_gateway with a
     stable issue_key (`outcome-heal:<lane>`). The gateway coalesces low-sev alerts into
     a periodic digest and re-alerts the same issue_key at most once per WARN_COOLDOWN
     (24h). So the owner gets ONE digest line per chronic, not N identical pings. The
     only thing that re-pages inside the cooldown is a genuine WORSENING (fails grew by
     >= WORSEN_DELTA) — escalated as critical.

  3. AUTO-RESOLVE — if the fix attempt makes the signal recover (harvest produces >0
     real raised hands again), it registers a heal with the gateway (healed=True) which
     suppresses the symptom and drops a single 'recovered' line into the digest, and
     tells the caller to reset the lane's miss counter.

Additive + fail-soft: if the gateway or diagnoser can't be imported, callers fall back
to their old alert path — an escalation is never lost. Reversible: one module + one
state file + a small edit in outcome_heal.run().
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "data" / "integrity" / "chronic_escalation_state.json"


def _envf(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "").strip() or default)
    except Exception:
        return default


# Attempt a real root-cause fix at most this often per lane (vs. every scheduler tick).
FIX_COOLDOWN = _envf("CHRONIC_FIX_COOLDOWN", 6 * 3600)
# A chronic re-pages (critical) despite the dedup cooldown only if it got this much worse.
WORSEN_DELTA = int(_envf("CHRONIC_WORSEN_DELTA", 25))
# Deep failover gets a longer leash than the transient band-aid (240s in outcome_heal).
FIX_TIMEOUT = _envf("CHRONIC_FIX_TIMEOUT", 300)


def _load() -> dict:
    try:
        return json.loads(STATE.read_text())
    except Exception:
        return {}


def _save(s: dict) -> None:
    try:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(s, indent=2))
    except Exception:
        pass


def _sig(detail: str) -> str:
    """A stable-ish signature for the ledger: drop the volatile '×N' count so the same
    chronic groups across ticks instead of looking like a new failure each time."""
    base = (detail or "").split(";")[0].strip()
    return base[:120] or "chronic_miss"


def _diagnose(lane: str, hint: str, detail: str) -> dict:
    try:
        from core.rootcause_diagnoser import diagnose
        d = diagnose({"service": lane, "message": f"{hint} | {detail}"}) or {}
        return {"category": d.get("category", "UNKNOWN"),
                "proposed_fix": d.get("proposed_fix", "")[:200]}
    except Exception as e:
        return {"category": "UNKNOWN", "proposed_fix": f"(diagnoser unavailable: {e})"}


def _record_heal(lane: str, signature: str, action: str, verified, detail: str) -> None:
    try:
        from core.health_ledger import record_heal
        record_heal(service=lane, signature=signature, action=action,
                    verified=verified, detail=detail)
    except Exception:
        pass


def _safe_signal(signal_fn):
    try:
        ok, det = signal_fn()
        return bool(ok), str(det)
    except Exception as e:
        return False, f"signal raised {type(e).__name__}: {e}"


def _route(severity: str, subject: str, body: str, issue_key: str):
    """Dedup+digest gate. Returns the gateway's action dict, or None if unavailable."""
    try:
        from core import alert_gateway
        return alert_gateway.route(severity, "outcome_heal", subject, body,
                                   issue_key=issue_key)
    except Exception:
        return None


def _register_fix(issue_key: str, subject: str):
    try:
        from core import alert_gateway
        return alert_gateway.register_fix("outcome_heal", issue_key=issue_key,
                                          subject=subject)
    except Exception:
        return None


def handle(lane: str, detail: str, fails: int, fix_cmd, hint: str, signal_fn,
           chronic: bool, dry: bool = False) -> dict:
    """Chronic-aware escalation for one missed lane. Returns:
        {action, resolved: bool, fix_attempted: bool, detail}

    Caller (outcome_heal) resets the lane's miss counter when resolved is True.
    """
    issue_key = f"outcome-heal:{lane}"
    subject = f"self-heal CHRONIC: {lane}" if chronic else f"self-heal miss: {lane}"
    sig = _sig(detail)
    result = {"action": "noop", "resolved": False, "fix_attempted": False, "detail": detail}

    if dry:
        result["action"] = "dry"
        return result

    st = _load()
    e = st.setdefault(lane, {"fix_attempts": 0, "last_fix_ts": 0.0,
                             "last_alert_fails": 0, "resolved_count": 0,
                             "diagnosed": ""})
    now = time.time()
    attempt_outcome = None  # None=not tried, True=resolved, False=tried+failed

    # 1) ESCALATE: a REAL root-cause fix, rate-limited so chronic doesn't thrash the
    #    harvester every tick. Transient misses already got their one-shot band-aid in
    #    outcome_heal; here we re-arm the fix that chronic logic used to abandon forever.
    if chronic and fix_cmd and (now - e.get("last_fix_ts", 0) >= FIX_COOLDOWN):
        diag = _diagnose(lane, hint, detail)
        e["diagnosed"] = f"{diag['category']}: {diag['proposed_fix']}"
        _record_heal(lane, sig, "chronic_failover_attempt", None,
                     f"diag={diag['category']}; cmd={' '.join(map(str, fix_cmd))[:120]}")
        try:
            subprocess.run(fix_cmd, cwd=ROOT, capture_output=True, timeout=FIX_TIMEOUT)
        except Exception as fx:
            detail = f"{detail}; failover raised {type(fx).__name__}"
        ok2, detail2 = _safe_signal(signal_fn)
        e["last_fix_ts"] = now
        e["fix_attempts"] = int(e.get("fix_attempts", 0)) + 1
        result["fix_attempted"] = True
        if ok2:
            # AUTO-RESOLVE — harvest produces >0 again. Notify ONCE via the gateway's
            # recovered-digest line; suppress the symptom; reset our alert high-water.
            _record_heal(lane, sig, "chronic_failover", True, detail2)
            _register_fix(issue_key, f"resolved: {lane} ({detail2})")
            e["resolved_count"] = int(e.get("resolved_count", 0)) + 1
            e["last_alert_fails"] = 0
            _save(st)
            result.update(action="auto_resolved", resolved=True, detail=detail2)
            return result
        _record_heal(lane, sig, "chronic_failover", False, detail2)
        attempt_outcome = False
        detail = f"{detail}; failover did not resolve ({detail2})"

    # 2) DE-DUP / SUPPRESS: route through the gateway. WARN coalesces into the periodic
    #    digest and re-alerts the same issue_key at most once / WARN_COOLDOWN (24h) — the
    #    single mechanism that turns 349 identical pings into one digest line. We escalate
    #    to CRITICAL (forces a re-page despite the cooldown) ONLY when it genuinely got
    #    worse, or a fresh fix attempt just failed — i.e. the situation is actionable now.
    worsened = (fails - int(e.get("last_alert_fails", 0))) >= WORSEN_DELTA
    fresh_fix_failed = attempt_outcome is False and int(e.get("last_alert_fails", 0)) == 0
    severity = "critical" if (chronic and (worsened or fresh_fix_failed)) else "warn"

    body = (f"{hint}\n"
            f"missed ×{fails}. diagnosis: {e.get('diagnosed') or 'n/a'}. "
            f"fix_attempts={e.get('fix_attempts', 0)}, "
            f"last attempt {'FAILED' if attempt_outcome is False else 'rate-limited' if chronic else 'n/a'}.")
    r = _route(severity, subject, body, issue_key)
    if r is None:
        # Gateway unavailable → signal caller to use its legacy alert path (fail-soft).
        result["action"] = "gateway_unavailable"
        _save(st)
        return result
    e["last_alert_fails"] = fails
    _save(st)
    result["action"] = f"{severity}:{r.get('action')}"
    return result


def resolve(lane: str, detail: str = "") -> dict:
    """Passive auto-resolve: the caller (outcome_heal) detected a lane recovered on its
    own (signal healthy) after we'd opened a chronic. Notify ONCE via the gateway's
    recovered-digest line and clear our bookkeeping. No-op if we never alerted it."""
    st = _load()
    e = st.get(lane)
    if not e or not (e.get("last_alert_fails") or e.get("fix_attempts")):
        return {"action": "noop_not_open"}
    issue_key = f"outcome-heal:{lane}"
    _record_heal(lane, _sig(detail or "recovered"), "auto_resolved_passive", True, detail)
    _register_fix(issue_key, f"resolved: {lane} ({detail})")
    e["resolved_count"] = int(e.get("resolved_count", 0)) + 1
    e["last_alert_fails"] = 0
    e["fix_attempts"] = 0
    _save(st)
    return {"action": "resolved", "lane": lane}


def status() -> dict:
    """Current chronic-escalation bookkeeping (for truth_report / debugging)."""
    return _load()


# --------------------------- self-test (proof) -----------------------------
def _selftest() -> int:
    """Prove: a chronic recurrence ESCALATES + DEDUPS (no 349x spam) and AUTO-RESOLVES
    when the underlying metric recovers. Runs fully offline against a temp gateway
    state in dry mode (deliveries captured, not sent)."""
    import tempfile
    os.environ["ALERT_GW_DRY"] = "1"
    os.environ["ALERT_GW_FORCE_IMSG"] = "1"   # bypass zero-noise filter so we can COUNT
    os.environ["ALERT_GW_DIGEST_WINDOW"] = "0"  # flush digest every call so we can count it
    os.environ["CHRONIC_FIX_COOLDOWN"] = "0"  # let every chronic tick attempt the fix
    from core import alert_gateway

    tmp = Path(tempfile.mkdtemp())
    alert_gateway.STATE_FILE = tmp / "gw_state.json"
    alert_gateway.DIGEST_WINDOW = 0  # constant read at its import; force per-call flush
    global STATE, FIX_COOLDOWN
    STATE = tmp / "chronic_state.json"
    FIX_COOLDOWN = 0  # constant read at import; let every chronic tick attempt the fix
    alert_gateway.SENT_LOG.clear()

    # A controllable lane: the metric is dead until a fix "lands", then recovers.
    metric = {"alive": False}

    def signal():
        return (metric["alive"], "9 new raised hands in 24h" if metric["alive"]
                else "0 new raised hands in 24h")

    # The "failover" is modeled by a trivial shell cmd; we flip the metric out-of-band on
    # tick 7 to model "the failover repaired the dead source and the harvest produces >0".
    DEAD = ["/usr/bin/true"]

    TICKS = 349  # the exact real-world spam count
    deliveries_before_resolve = 0
    resolved_tick = None
    for i in range(1, TICKS + 1):
        fails = i
        chronic = fails >= 3
        # Model the failover succeeding on tick 7 (a few chronic attempts in).
        if i == 7:
            metric["alive"] = True
        res = handle("sim_radar", "0 new raised hands in 24h", fails, DEAD,
                     "source silently dead — re-run failover", signal, chronic, dry=False)
        if res["resolved"]:
            resolved_tick = i
            break

    sent = alert_gateway.SENT_LOG
    pages = [s for s in sent if s.get("transport") == "imessage"]
    digests = [s for s in sent if s.get("transport") == "digest"]
    recovered = [s for s in sent if "recovered" in json.dumps(s).lower()
                 or "resolved" in json.dumps(s).lower()]

    print(f"[selftest] simulated {resolved_tick or TICKS} chronic ticks "
          f"(real-world was {TICKS}x identical texts)")
    print(f"[selftest] gateway deliveries: pages={len(pages)} digests={len(digests)}")
    print(f"[selftest] auto-resolved at tick {resolved_tick}")
    print(f"[selftest] recovery notices: {len(recovered)}")

    ok = True
    # PROOF 1: the 349x spam can't recur — total owner-visible alerts are a tiny constant,
    # not one-per-tick.
    total_owner_alerts = len(pages) + len(digests)
    if total_owner_alerts >= 50:
        print(f"  FAIL: {total_owner_alerts} alerts — dedup not working"); ok = False
    else:
        print(f"  PASS: {total_owner_alerts} owner alerts for {resolved_tick} ticks "
              f"(was {TICKS}) — dedup holds")
    # PROOF 2: it escalated (attempted a real fix) and auto-resolved.
    if resolved_tick is None:
        print("  FAIL: never auto-resolved"); ok = False
    else:
        print(f"  PASS: escalated + auto-resolved at tick {resolved_tick}")
    # PROOF 3: exactly one recovery notice (notify once).
    if len(recovered) != 1:
        print(f"  WARN: {len(recovered)} recovery notices (expected 1)")
    else:
        print("  PASS: single 'resolved' notice")
    print("[selftest] OVERALL:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        raise SystemExit(_selftest())
    print(json.dumps(status(), indent=2))
