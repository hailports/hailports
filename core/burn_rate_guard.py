#!/usr/bin/env python3
"""burn_rate_guard.py — Real-time spend rate circuit breaker.

Checks OpenRouter credits every 2 min. If spend rate exceeds threshold in any
rolling window, it:
  1. Unloads the JIT healer (biggest token burner)
  2. Trips the budget guard state (stops bees)
  3. iMessages Operator immediately
  4. Logs everything

Reset: python3 core/burn_rate_guard.py --reset
"""
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path("/home/user")
STACK = ROOT / "claude-stack"
STATE_FILE = ROOT / ".burn-rate-guard.state.json"
LOG_FILE = ROOT / ".burn-rate-guard.log"
BUDGET_GUARD_STATE = ROOT / ".openrouter-budget-guard.state.json"

# Detection is RATE-based ($/min), not raw cumulative delta. A trip requires TWO
# consecutive *valid* intervals to both run hot (debounce) AND a real-dollar floor.
# This makes false trips structurally impossible: a time gap, a counter reset, a
# month rollover, a reconnect artifact, or any single anomalous tick can never trip —
# only a genuinely sustained burn can. Rewritten 2026-06-06 after a 12h-gap artifact
# false-tripped twice and killed 9 jobs.
TRIP_RATE_PER_MIN    = 0.15   # latest interval must burn ≥ $0.15/min ( = $0.30/2min )
CONFIRM_RATE_PER_MIN = 0.10   # the interval before it must ALSO burn ≥ $0.10/min
MIN_WINDOW_SPEND     = 0.40   # …and real $ across the confirming intervals must clear this
MIN_INTERVAL_SEC     = 30     # an interval shorter than this is a double-tick artifact → invalid
MAX_INTERVAL_SEC     = 360    # an interval longer than this spans a gap → invalid (never trips)
HISTORY_KEEP         = 12      # keep last 12 readings (~24 min of data)
UNKNOWN_TRIP_COUNT   = 3       # page+trip only after repeated unreadable usage

# Jobs to kill when tripped (biggest spenders first)
KILL_JOBS = [
    "com.claude-stack.jit-healer",
    "com.claude-stack.bee-hive",
    "com.claude-stack.revenue-autopilot",
    "com.claude-stack.revenue-engine",
    "com.claude-stack.revenue-hunter",
    "com.claude-stack.revenue-strategist",
    "com.claude-stack.revenue-diversifier",
    "com.claude-stack.revenue-tracker",
    "com.claude-stack.revenue-scheduled-sends",
]

Operator = "XPHONEX"


def log(msg):
    ts = time.strftime("%Y-%m-%dT%H:%M:%S")
    line = f"{ts} {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def get_credits():
    try:
        key = json.loads((ROOT / ".openclaw" / "openclaw.json").read_text())["models"]["providers"]["openrouter"]["apiKey"]
    except Exception:
        for line in (ROOT / ".env").read_text(errors="ignore").splitlines():
            if line.startswith("OPENROUTER_API_KEY"):
                key = line.split("=", 1)[1].strip().strip('"\'')
                break
        else:
            return None
    try:
        req = urllib.request.Request(
            "https://openrouter.ai/api/v1/auth/key",
            headers={"Authorization": f"Bearer {key}"}
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            d = json.loads(r.read())
        usage = d.get("usage")
        limit = d.get("limit")
        if usage is not None:
            return float(usage)
        # fallback: read from budget guard log
    except Exception:
        pass
    # Fallback: parse last line of budget guard log
    try:
        lines = (ROOT / ".openrouter-budget-guard.log").read_text().strip().splitlines()
        for line in reversed(lines):
            if "monthly=" in line:
                val = line.split("monthly=")[1].split()[0]
                return float(val.lstrip("$"))
    except Exception:
        pass
    return None


def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {"tripped": False, "history": [], "killed_jobs": []}


def save_state(s):
    STATE_FILE.write_text(json.dumps(s, indent=2))


def send_imessage(subject, body="", severity="critical", issue_key="burn_rate_guard", healed=False):
    # Route through the central alert gateway so its allow-list/dedup/digest policy
    # applies. Cost emergencies ($/burn) are on the gateway allow-list and text through;
    # routine status digests silently. Never fall back to direct texting.
    try:
        from core import alert_gateway
        alert_gateway.route(severity, source="burn_rate_guard", subject=subject,
                            body=body, issue_key=issue_key, healed=healed)
    except Exception as e:
        log(f"alert_gateway route failed: {e}")


def kill_spenders(state):
    killed = []
    for job in KILL_JOBS:
        try:
            r = subprocess.run(
                ["launchctl", "unload",
                 f"/home/user/Library/LaunchAgents/{job}.plist"],
                capture_output=True, text=True, timeout=10
            )
            if r.returncode == 0:
                killed.append(job.split(".")[-1])
                log(f"KILLED {job}")
        except Exception as e:
            log(f"kill failed {job}: {e}")
    state["killed_jobs"] = killed
    # Also trip the existing budget guard so bees stop
    try:
        bg = json.loads(BUDGET_GUARD_STATE.read_text())
        bg["tripped"] = True
        BUDGET_GUARD_STATE.write_text(json.dumps(bg, indent=2))
    except Exception:
        pass
    return killed


def reset():
    state = load_state()
    state["tripped"] = False
    state["killed_jobs"] = []
    state["trip_reason"] = None
    # Clear history so the next reading starts a FRESH baseline. Leaving stale readings
    # is what let the 12h-gap artifact re-trip immediately after the previous reset.
    state["history"] = []
    save_state(state)
    # Reset budget guard trip
    try:
        bg = json.loads(BUDGET_GUARD_STATE.read_text())
        bg["tripped"] = False
        BUDGET_GUARD_STATE.write_text(json.dumps(bg, indent=2))
    except Exception:
        pass
    # Reload killed jobs
    for job in KILL_JOBS:
        plist = f"/home/user/Library/LaunchAgents/{job}.plist"
        if os.path.exists(plist):
            subprocess.run(["launchctl", "load", plist], capture_output=True)
    log("RESET — guard cleared, jobs reloaded")
    print("Burn rate guard reset. Killed jobs reloaded.")


def main():
    state = load_state()

    if state.get("tripped"):
        log("Guard already tripped — skipping check. Run --reset to clear.")
        return

    usage = get_credits()
    if usage is None:
        misses = int(state.get("unknown_credit_reads") or 0) + 1
        state["unknown_credit_reads"] = misses
        save_state(state)
        log(f"Could not read credits — miss {misses}/{UNKNOWN_TRIP_COUNT}")
        if misses >= UNKNOWN_TRIP_COUNT:
            msg = "🚨 Burn-rate guard cannot read OpenRouter usage after repeated checks. Conservative trip engaged."
            send_imessage("Burn-rate guard: OpenRouter usage unreadable — conservative trip engaged",
                          body=msg, severity="critical", issue_key="burn_rate_guard_unknown_usage")
            try:
                bg = json.loads(BUDGET_GUARD_STATE.read_text()) if BUDGET_GUARD_STATE.exists() else {}
                bg.update({"tripped": True, "reason": "burn_rate_guard_unknown_usage", "ts": time.time()})
                BUDGET_GUARD_STATE.write_text(json.dumps(bg, indent=2))
            except Exception:
                pass
            state["tripped"] = True
            state["trip_reason"] = "unknown_usage"
            save_state(state)
        return
    if state.get("unknown_credit_reads"):
        state["unknown_credit_reads"] = 0

    now = time.time()
    history = state.get("history", [])

    # Counter-reset / month-rollover guard: cumulative usage can ONLY increase. If it
    # dropped, OpenRouter reset the counter (new month, new key, prepaid top-up reset).
    # Throw away history and re-baseline — a "negative burn" must never be readable as
    # spend, and the next reading must not diff against the old higher number.
    if history and usage + 1e-9 < history[-1]["usage"]:
        log(f"usage=${usage:.4f} < last ${history[-1]['usage']:.4f} — counter reset/rollover. Re-baselining.")
        state["history"] = [{"ts": now, "usage": usage}]
        save_state(state)
        return

    history.append({"ts": now, "usage": usage})
    history = history[-HISTORY_KEEP:]
    state["history"] = history

    # Build the list of VALID intervals: consecutive reading pairs whose elapsed time is
    # in [MIN, MAX] seconds and whose usage is monotonic. Any pair outside that band
    # (gap, double-tick, reconnect from $0) is simply excluded — it can't contribute to
    # a trip. Each valid interval yields a true $/min rate.
    intervals = []
    for a, b in zip(history, history[1:]):
        dt = b["ts"] - a["ts"]
        if dt < MIN_INTERVAL_SEC or dt > MAX_INTERVAL_SEC:
            continue
        if a["usage"] <= 0:                      # baseline came from an outage/$0 reading
            continue
        spend = b["usage"] - a["usage"]
        if spend < 0:
            continue
        intervals.append({"spend": spend, "rate": spend / (dt / 60.0), "mins": dt / 60.0})

    # Need at least two consecutive valid intervals to even consider tripping. This
    # debounce is what makes a single anomalous tick incapable of tripping.
    if len(intervals) < 2:
        log(f"usage=${usage:.4f} — {len(intervals)} valid interval(s); need ≥2 to evaluate. No trip.")
        save_state(state)
        return

    last, prev = intervals[-1], intervals[-2]
    confirm_spend = last["spend"] + prev["spend"]
    log(f"usage=${usage:.4f} rate_last=${last['rate']:.3f}/min rate_prev=${prev['rate']:.3f}/min "
        f"confirm_spend=${confirm_spend:.4f} over {last['mins']+prev['mins']:.1f}m")

    # TRIP only if ALL hold: latest interval hot, the one before it also hot (sustained,
    # not a blip), and the real dollars burned across both clear the absolute floor.
    sustained = (last["rate"] >= TRIP_RATE_PER_MIN
                 and prev["rate"] >= CONFIRM_RATE_PER_MIN
                 and confirm_spend >= MIN_WINDOW_SPEND)

    if sustained:
        trip_reason = (f"${last['rate']:.2f}/min sustained (${confirm_spend:.2f} over "
                       f"{last['mins']+prev['mins']:.0f}min, threshold ${TRIP_RATE_PER_MIN}/min)")
        log(f"BURN RATE TRIP: {trip_reason}")
        state["tripped"] = True
        state["trip_reason"] = trip_reason
        state["trip_ts"] = now
        killed = kill_spenders(state)
        save_state(state)
        msg = (f"BURN ALERT — sustained burn {trip_reason}. "
               f"Killed: {', '.join(killed) or 'none'}. "
               f"Run: python3 claude-stack/core/burn_rate_guard.py --reset")
        send_imessage(f"BURN ALERT — sustained burn {trip_reason}", body=msg,
                      severity="critical", issue_key="burn_rate_guard_trip")
        log(f"alert routed: {msg}")
    else:
        save_state(state)


if __name__ == "__main__":
    if "--reset" in sys.argv:
        reset()
    else:
        main()
