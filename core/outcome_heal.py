#!/usr/bin/env python3
"""Outcome-driven self-heal supervisor — the layer the stack was missing.

The old healers (watchdog/jit-healer/health_check) check LIVENESS ("is the process
up?") and respond with RESTARTS. That misses the failures that actually cost money:
a job that is "up" but produces NOTHING (X posted 0 for 3 days), a clean-exit crash
loop (diagnostician churned for days), a wiped .env. Restarting those does nothing.

This supervisor is different on three axes:
  1. OUTCOME-based — a lane is healthy only if it PRODUCED its expected output in-window.
  2. KNOWN-FIX registry — apply the specific repair (re-harvest a session, restore .env),
     not a blind restart. If there is no safe known fix, it does NOT thrash.
  3. CHRONIC escalation + verify + learn — it counts consecutive misses in a ledger; a
     transient gets one fix attempt (then re-checks), a CHRONIC miss stops band-aiding and
     escalates ONCE/day with a root-cause hypothesis. No infinite restart loops.

  python3 -m core.outcome_heal          # one sense→fix/escalate→verify pass (scheduled)
  python3 -m core.outcome_heal --dry    # report only, take no action
"""
from __future__ import annotations
import json, os, subprocess, sys, time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
HUST = ROOT / "data" / "hustle"
LEDGER = ROOT / "data" / "integrity" / "outcome_heal_ledger.json"
# Rung 1 — the LEARNED fix registry. Built-in lane fixes are the seed; this file grows as
# new repairs are discovered (via register_fix / --register) so the supervisor auto-applies
# a known-good fix next time instead of escalating the same failure forever. Each fix tracks
# how often it actually resolved the miss (verify-on-success) — confidence accrues.
FIX_REGISTRY = ROOT / "data" / "integrity" / "known_fixes.json"
CHRONIC_AFTER = 3          # consecutive misses before we stop fixing and escalate
PY = str(ROOT / ".venv" / "bin" / "python")


def _load_registry() -> dict:
    try:
        return json.loads(FIX_REGISTRY.read_text())
    except Exception:
        return {}


def _save_registry(reg: dict):
    FIX_REGISTRY.parent.mkdir(parents=True, exist_ok=True)
    FIX_REGISTRY.write_text(json.dumps(reg, indent=2))


def register_fix(lane: str, cmd: list, note: str = "") -> dict:
    """Teach the supervisor a verified repair for a lane → auto-applied on the next miss."""
    reg = _load_registry()
    e = reg.setdefault(lane, {})
    e["fix"] = cmd
    if note:
        e["note"] = note
    e.setdefault("verified", 0)
    e["registered_at"] = datetime.now(timezone.utc).isoformat()
    _save_registry(reg)
    return e


def _age_h(p: Path) -> float:
    try:
        return (time.time() - p.stat().st_mtime) / 3600
    except OSError:
        return 1e9


def _newer_than(glob_pat: str, hours: float) -> int:
    cut = time.time() - hours * 3600
    return sum(1 for p in ROOT.glob(glob_pat) if p.is_file() and p.stat().st_mtime >= cut)


# ── outcome signals: each returns (ok: bool, detail: str) ────────────────────────
def _sig_x_posting():
    f = HUST / "content_poster_state.json"
    try:
        d = json.loads(f.read_text())
    except Exception:
        return False, "no content_poster_state"
    from datetime import date, timedelta
    today, yest = date.today().isoformat(), (date.today() - timedelta(days=1)).isoformat()
    n = int(d.get(f"tweets_{today}", 0)) + int(d.get(f"tweets_{yest}", 0))
    return n >= 1, f"{n} tweets in 24h"


def _sig_reels():
    n = _newer_than("data/hustle/ima_reels/*/ima_system_reel.mp4", 72)
    return n >= 1, f"{n} reels rendered in 72h"


def _sig_fulfillment():
    f = ROOT.parent / "Library/Logs/claude-stack/stripe-fulfillment.out.log"
    f = Path(os.path.expanduser("~/Library/Logs/claude-stack/stripe-fulfillment.out.log"))
    try:
        tail = f.read_text(errors="ignore")[-500:]
    except Exception:
        return True, "no log yet"
    return ("not configured" not in tail), "key error in last run" if "not configured" in tail else "ok"


def _sig_env():
    try:
        from dotenv import dotenv_values
        v = dotenv_values(str(ROOT / ".env"))
        ok = bool(v.get("STRIPE_SECRET_KEY")) and len(v) >= 50
        return ok, f"{len(v)} vars"
    except Exception as e:
        return False, f"env unreadable ({e})"


def _sig_intent_leads():
    age = _age_h(HUST / "INTENT_STRIKE_LIST.md")
    return age < 12, f"strike list {age:.1f}h old"


def _sig_looking_for():
    # The radar ran "fine" for a day while its best source returned 0 forever (CL RSS dead).
    # Outcome check: the QUEUE must actually GAIN rows — declared-intent posts appear on
    # these boards many times per day, so 24h of zero new hands = a dead source, not quiet.
    q = HUST / "looking_for_queue.jsonl"
    try:
        cutoff = datetime.now(timezone.utc).timestamp() - 86400
        recent = 0
        for line in q.read_text().splitlines():
            try:
                ts = json.loads(line).get("found_at", "")
                if ts and datetime.fromisoformat(ts).timestamp() > cutoff:
                    recent += 1
            except Exception:
                continue
        return recent > 0, f"{recent} new raised hands in 24h"
    except Exception as e:
        return False, f"queue unreadable ({e})"


def _sig_comments():
    # engagement output: any comment posted in 24h (bossbabe/persona1 engagement ledgers)
    for name in ("bossbabe_engagement.json", "ima_engagement_received.jsonl"):
        if _age_h(HUST / name) < 24:
            return True, "engagement active <24h"
    return False, "no engagement output in 24h"


# fix=None  → no safe auto-fix; miss escalates immediately with the hint.
LANES = [
    {"name": "x_posting", "signal": _sig_x_posting, "fix": [PY, "-m", "agents.harvest_x_session"],
     "hint": "X login cookie expired — re-harvest from CDP Chrome (auto)."},
    {"name": "stripe_fulfillment", "signal": _sig_fulfillment, "fix": [PY, "scripts/env_guard.py"],
     "hint": "fulfillment can't read STRIPE_SECRET_KEY — .env likely wiped; restore (auto)."},
    {"name": "env_health", "signal": _sig_env, "fix": [PY, "scripts/env_guard.py"],
     "hint": ".env missing critical keys — restore from snapshot (auto)."},
    {"name": "intent_leads", "signal": _sig_intent_leads, "fix": None,
     "hint": "intent harvest stalled >12h — check intent-engine/strike-list jobs."},
    {"name": "looking_for_radar", "signal": _sig_looking_for,
     "fix": [PY, "-m", "agents.looking_for_radar"],
     "hint": "0 new raised hands in 24h — a source is silently dead (CL scrape/SearXNG/"
             "Reddit 403). Re-run once (auto); if still 0, diagnose the fetch path."},
    {"name": "comments", "signal": _sig_comments, "fix": None,
     "hint": "social engagement produced nothing in 24h — check engage-cdp session/CDP Chrome."},
    {"name": "reels", "signal": _sig_reels, "fix": [PY, "scripts/build_ima_reel.py"],
     "hint": "no reel rendered in 72h — render or b-roll pool issue (auto-render once)."},
]


def _load_ledger() -> dict:
    try:
        return json.loads(LEDGER.read_text())
    except Exception:
        return {}


def _alert(msg: str):
    try:
        from tools.imsg_bridge import send_imessage
        send_imessage(msg)
    except Exception:
        pass


def run(dry: bool = False) -> int:
    led = _load_ledger()
    reg = _load_registry()
    now = datetime.now(timezone.utc)
    today = now.date().isoformat()
    healthy, fixed, escalated = [], [], []

    for lane in LANES:
        name = lane["name"]
        st = led.setdefault(name, {"fails": 0, "last_good": None, "last_alert": None})
        ok, detail = lane["signal"]()
        if ok:
            st["fails"] = 0
            st["last_good"] = now.isoformat()
            healthy.append(f"{name} ({detail})")
            continue

        st["fails"] += 1
        chronic = st["fails"] >= CHRONIC_AFTER
        # Effective fix = a LEARNED registry fix (preferred) OR the built-in one. This is
        # how a lane that started escalate-only (fix:None) gains an auto-repair over time.
        fix = (reg.get(name) or {}).get("fix") or lane["fix"]
        # Transient + a known safe fix → repair once, then VERIFY.
        if fix and not chronic and not dry:
            # A hung/erroring fix must not abort the whole heal pass (other lanes still
            # need checking). Bound it and treat a timeout/crash as "fix didn't resolve".
            try:
                subprocess.run(fix, cwd=ROOT, capture_output=True, timeout=240)
            except Exception as _fx:
                detail = f"{detail}; auto-fix raised {type(_fx).__name__}"
            ok2, detail2 = lane["signal"]()
            if ok2:
                st["fails"] = 0
                st["last_good"] = now.isoformat()
                # learning signal: this fix actually resolved the miss → bump confidence
                e = reg.setdefault(name, {"fix": fix})
                e["verified"] = int(e.get("verified", 0)) + 1
                e["last_worked"] = now.isoformat()
                _save_registry(reg)
                fixed.append(f"{name}: {detail} → fixed ({detail2}) [fix verified ×{e['verified']}]")
                continue
            detail = f"{detail}; auto-fix did not resolve"

        # Chronic, or no safe fix, or fix failed → escalate ONCE/day, stop thrashing.
        if st.get("last_alert") != today:
            tag = "CHRONIC" if chronic else "miss"
            _alert(f"🩺 self-heal [{tag}] {name}: {detail} (×{st['fails']}). {lane['hint']}")
            st["last_alert"] = today
        escalated.append(f"{name}: {detail} ×{st['fails']} → {lane['hint']}")

    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    LEDGER.write_text(json.dumps(led, indent=2))
    print(f"[outcome-heal] healthy={len(healthy)} auto-fixed={len(fixed)} escalated={len(escalated)}")
    for f in fixed:
        print(f"  ✅ {f}")
    for e in escalated:
        print(f"  ⚠️  {e}")
    for h in healthy:
        print(f"  · {h}")
    return 0


if __name__ == "__main__":
    if "--register" in sys.argv:
        i = sys.argv.index("--register")
        lane, cmd = sys.argv[i + 1], sys.argv[i + 2:]
        register_fix(lane, cmd, note="operator-registered")
        print(f"registered fix for {lane}: {' '.join(cmd)}")
        raise SystemExit(0)
    raise SystemExit(run(dry="--dry" in sys.argv))
