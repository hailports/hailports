#!/usr/bin/env python3
"""yt_live_supervisor.py — bring the dashboard live stream up the moment YouTube allows it,
and keep it up forever. Idempotent; meant to run on a launchd interval.

Each run:
  1. ensure the dashboard server is up
  2. try to mint/reuse the YouTube broadcast (writes the RTMP key file) — this fails harmlessly
     until the AI Built Fast channel is enabled for live streaming (youtube.com/features)
  3. if the key file exists and the streamer isn't already running, launch it
This is the "eternal" wiring: launchd restarts the supervisor, the supervisor restarts the
streamer, and the streamer has its own encoder auto-restart.
"""
from __future__ import annotations
import os, subprocess, sys, time, urllib.request
from pathlib import Path

ROOT = Path(os.path.expanduser("~/claude-stack"))
PY = str(ROOT / ".venv" / "bin" / "python")
PY = PY if Path(PY).exists() else sys.executable
KEY_FILE = ROOT / "data" / "hustle" / "case_study_stream_key.txt"
DASH_URL = "http://127.0.0.1:8360/"
LOG = ROOT / ".yt-live-supervisor.out"


def log(msg):
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {msg}"
    print(line, flush=True)
    try:
        with LOG.open("a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def dashboard_up() -> bool:
    try:
        urllib.request.urlopen(DASH_URL, timeout=3).read(1); return True
    except Exception:
        return False


def streamer_running() -> bool:
    r = subprocess.run(["pgrep", "-f", "dashboard_livestream.py"], capture_output=True, text=True)
    return bool(r.stdout.strip())


def main():
    if not dashboard_up():
        subprocess.Popen([PY, str(ROOT / "tools" / "public_case_study_dashboard.py")],
                         cwd=str(ROOT), env={**os.environ, "PYTHONPATH": str(ROOT)},
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(20):
            time.sleep(0.5)
            if dashboard_up():
                break

    # if no RTMP key yet, try minting via API; a manual Studio key already in the file wins
    if not (KEY_FILE.exists() and KEY_FILE.read_text().strip()):
        r = subprocess.run([PY, str(ROOT / "tools" / "yt_live_setup.py")],
                           cwd=str(ROOT), capture_output=True, text=True)
        if not (KEY_FILE.exists() and KEY_FILE.read_text().strip()):
            log(f"waiting for stream key — {r.stdout.strip()[:160] or r.stderr.strip()[:160]}")
            return 0

    if streamer_running():
        log("streamer already running — ok")
        return 0

    env = {**os.environ, "CASE_STUDY_STREAM_LIVE": "1", "PYTHONPATH": str(ROOT)}
    subprocess.Popen([PY, str(ROOT / "tools" / "dashboard_livestream.py")],
                     cwd=str(ROOT), env=env,
                     stdout=open(ROOT / ".livestream.out", "a"),
                     stderr=subprocess.STDOUT)
    log("LIVE: launched dashboard_livestream — stream is going up")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
