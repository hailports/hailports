#!/usr/bin/env python3
"""Keep OFFSCREEN AUTOMATION Chromes minimized so they never sit on the operator's screen
(the -32000 offscreen position gets clamped/raised back on some displays, and OAuth/consent
steps RE-RAISE an already-parked window — the "youtube window pops up again" report).

This used to run once per launchd tick every 720s (12 MINUTES) — so a re-raised window sat
fully visible for up to 12 minutes. Now it loops internally on a ~3s cadence for ~57s and
launchd relaunches it every 60s (KeepAlive/StartInterval=60 stays clear of the 10s throttle),
giving continuous ~3s worst-case coverage from a single process at a time. cdp_minimize
skips windows already minimized, so idle ticks are cheap.

EXCLUDES personal profiles (littlebird :18821) — only hides automation windows.
"""
import sys, os, time
sys.path.insert(0, os.path.expanduser("~/claude-stack"))
from tools.cdp_minimize import minimize

# ALL known automation ports (NOT 18821 littlebird = personal, left visible for Operator).
PORTS = {
    18800: "x/work-bridge", 18802: "tiktok/persona1", 18805: "youtube/casestudy",
    18808: "misc-automation", 18810: "zoom", 18820: "owa/outlook",
    18823: "timeclock", 18831: "misc-automation2",
}

LOOP_SECONDS = 57  # stay under the 60s launchd relaunch so exactly one process runs at a time
CADENCE = 3

deadline = time.time() + LOOP_SECONDS
while True:
    for port in PORTS:
        try:
            minimize(port)  # silently no-ops if the port isn't up or window already minimized
        except Exception:
            pass
    if time.time() >= deadline:
        break
    time.sleep(CADENCE)
