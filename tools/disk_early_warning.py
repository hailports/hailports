#!/usr/bin/env python3
"""disk_early_warning.py — PROACTIVE (not reactive) internal-disk trend monitor.

disk_guardian.sh is the emergency REACTIVE reflex: it only *reclaims*, and (since the
2026-07-12 dead-zone fix) its WARN tier reclaims SILENTLY and only pages Operator at
CRIT-can't-recover. That leaves a blind spot: if free space grinds down toward the
floor over an hour while reclaim + cold tiering run every tick but can't keep up,
nobody hears about it until it's already an emergency.

This monitor closes that gap. It does NO reclaim (so it can never fight the guardian
or the tiering lane) — it only *observes a trend* and, when the internal disk has sat
inside the early-warning band for N consecutive checks WITHOUT recovering (net slope
<= 0 → reclaim/tiering is losing), it routes ONE warn-severity alert through
core.alert_gateway. warn severity means the gateway digests it (never pages, respects
quiet hours) — a heads-up, not a 3am siren. The gateway's own issue-key dedup plus a
local cooldown guarantee "ONE alert", not a flap.

NEVER-FAIL: measures only the INTERNAL volume (External state is irrelevant to it), so
an External disconnect/absence can't wedge or crash it. Every failure path is caught
and it always exits 0 — a monitor must never be the thing that takes a job down.

Bands (env-tunable, defaults track disk_guardian's floors):
  EDW_FLOOR_GB   26   authoritative crit floor (== DG_CRIT_GB)
  EDW_BUFFER_GB   9   early-warning headroom above the floor
  EDW_BAND_GB    35   free < this = "in the band" (default FLOOR+BUFFER; overrides both)
  EDW_CONSEC      4   consecutive in-band+non-recovering samples before alerting
  EDW_COOLDOWN_S 21600  local re-alert cooldown (6h) on top of gateway dedup
  EDW_FORCE_FREE_GB  (test only) override the measured free-GB reading

Runs headless every ~15 min via com.claude-stack.disk-early-warning.
Deterministic, stdlib-only, $0, reversible (one script + one plist + one state file).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

HOME = Path.home()
STATE = HOME / ".disk-early-warning.state.json"
LOG = HOME / "Library" / "Logs" / "claude-stack" / "disk-early-warning.log"
REPO = Path(__file__).resolve().parent.parent

FLOOR_GB = int(os.environ.get("EDW_FLOOR_GB", os.environ.get("DG_CRIT_GB", "26")))
BUFFER_GB = int(os.environ.get("EDW_BUFFER_GB", "9"))
BAND_GB = int(os.environ.get("EDW_BAND_GB", str(FLOOR_GB + BUFFER_GB)))
CONSEC = int(os.environ.get("EDW_CONSEC", "4"))
COOLDOWN_S = int(os.environ.get("EDW_COOLDOWN_S", "21600"))
KEEP_SAMPLES = max(CONSEC + 4, 12)


def _ts() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _note(msg: str) -> None:
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a") as fh:
            fh.write(f"{_ts()} {msg}\n")
    except Exception:
        pass


def free_gb() -> int | None:
    """Whole GB free on the INTERNAL boot container. Uses BSD df explicitly (GNU df
    in PATH rejects -g and would return empty → a false 'fine'). Returns None on any
    failure so the caller can no-op instead of acting on a bogus reading."""
    forced = os.environ.get("EDW_FORCE_FREE_GB")
    if forced not in (None, ""):
        try:
            return int(float(forced))
        except Exception:
            return None
    try:
        out = subprocess.run(
            ["/bin/df", "-g", "/"], capture_output=True, text=True, timeout=10
        ).stdout
        parts = out.splitlines()[1].split()
        return int(parts[3])
    except Exception as e:
        _note(f"free_gb read failed: {e}")
        return None


def _load_state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except Exception:
        return {"samples": [], "last_alert_ts": 0}


def _save_state(st: dict) -> None:
    try:
        st["samples"] = st.get("samples", [])[-KEEP_SAMPLES:]
        tmp = STATE.with_suffix(".tmp")
        tmp.write_text(json.dumps(st))
        tmp.replace(STATE)
    except Exception as e:
        _note(f"state save failed: {e}")


def _alert(free: int, window: list[int]) -> bool:
    """Route ONE warn via the central gateway (dedup + quiet-hours honored there).
    Returns True if the route call was made. Never raises."""
    span = window[0] - window[-1]  # GB lost across the window (positive = shrinking)
    subject = (
        f"internal disk trending toward floor: {free}GB free, in the "
        f"{BAND_GB}GB early-warning band for {len(window)} checks and NOT recovering "
        f"(lost {span}GB over the window, floor={FLOOR_GB}GB). Reclaim + cold-tiering "
        f"are running but not keeping up — a heads-up before it hits crit-can't-recover."
    )
    body = (
        "Proactive early-warning from tools/disk_early_warning.py (observation only, "
        "no reclaim). disk_guardian is already reclaiming silently every tick; this "
        f"fires because free space stayed < {BAND_GB}GB and did not recover across "
        f"{len(window)} consecutive checks (samples GB: {window}). If this persists, "
        "check the whales: OrbStack VM image, ollama model dupes, Downloads, large "
        "data exports — the regenerable sources the reaper already clears aren't enough."
    )
    try:
        sys.path.insert(0, str(REPO))
        from core.alert_gateway import route
        res = route(
            severity="warn",
            source="disk_early_warning",
            subject=subject,
            body=body,
            issue_key="disk_early_warning_trend",
        )
        _note(f"alert routed: {res}")
        return True
    except Exception as e:
        _note(f"alert route failed (non-fatal): {e}")
        return False


def main() -> int:
    free = free_gb()
    if free is None:
        _note("no reading — no-op")
        return 0

    st = _load_state()
    now = int(time.time())
    st.setdefault("samples", []).append([now, free])
    st.setdefault("last_alert_ts", 0)

    # Trend test over the last CONSEC samples: every one in-band AND net non-recovering
    # (oldest >= newest, i.e. slope <= 0). Recovery in the window clears the condition.
    recent = [s[1] for s in st["samples"][-CONSEC:]]
    in_band = all(v < BAND_GB for v in recent)
    have_window = len(recent) >= CONSEC
    not_recovering = have_window and recent[0] >= recent[-1]
    triggered = have_window and in_band and not_recovering

    if triggered and (now - st["last_alert_ts"]) >= COOLDOWN_S:
        _note(f"TREND ALERT: free={free}GB band<{BAND_GB} window={recent} floor={FLOOR_GB}")
        if _alert(free, recent):
            st["last_alert_ts"] = now
    elif triggered:
        _note(f"trend holds but within cooldown (free={free}GB window={recent})")
    else:
        state = "in-band" if in_band else "healthy"
        _note(f"ok — free={free}GB ({state}), window={recent}, trigger={triggered}")

    _save_state(st)
    print(json.dumps({
        "free_gb": free, "band_gb": BAND_GB, "floor_gb": FLOOR_GB,
        "window": recent, "in_band": in_band, "not_recovering": not_recovering,
        "triggered": triggered,
    }))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # absolute never-fail floor
        _note(f"fatal caught, exiting 0: {e}")
        sys.exit(0)
