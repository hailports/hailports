#!/usr/bin/env python3
"""case_study_stats_publisher.py — mini-side data plane for the cloud-hosted stream.

Computes the SCRUBBED, bucketed public stats and writes data/hustle/public_stats.json.
When CASE_STUDY_STATS_PUT_URL is set, also PUTs the JSON to that cloud endpoint (R2/KV/
a tiny VM) so the always-on cloud renderer + RTMP encoder can read fresh numbers. The
cloud keeps streaming even when this mini reboots or loses power — this just refreshes
the numbers; a gap here freezes the figures for a minute, it never drops the broadcast.

FAIL-CLOSED: the payload is run through core.anon_scrub.audit(); if ANY leak marker is
found, nothing is written or pushed (an empty/stale file is safer than a leak).

  run:  python tools/case_study_stats_publisher.py            # write local json (+push if configured)
  loop: launchd every ~30-60s
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(os.environ.get("CLAUDE_STACK_DIR", Path(__file__).resolve().parents[1]))
while str(ROOT) in sys.path:
    sys.path.remove(str(ROOT))
sys.path.insert(0, str(ROOT))

from core import anon_scrub  # noqa: E402

OUT = ROOT / "data" / "hustle" / "public_stats.json"
PUT_URL = os.environ.get("CASE_STUDY_STATS_PUT_URL", "").strip()
PUT_TOKEN = os.environ.get("CASE_STUDY_STATS_PUT_TOKEN", "").strip()


def build() -> dict:
    """The JSON contract the cloud index.html renders. Reuses the dashboard's own
    scrub-clean builders so the cloud page == the mini page."""
    try:
        from tools.public_case_study_dashboard import build_metrics
        tiles = build_metrics()
    except Exception:
        tiles = []
    # NO timestamps in the payload: an ISO date trips the number gate AND a precise time is a
    # weak correlation signal. Staleness is computed CLOUD-SIDE from the renderer's own last
    # successful fetch (client clock), so a mini outage shows "resyncing" without any date here.
    payload = {
        "tiles": tiles,
        "as_of_human": "moments ago",
        "stale": False,
        "live": True,
    }
    try:
        from core import roi_model
        pv = roi_model.public_view()
        payload["headline"] = pv.get("headline", {})
        payload["roi"] = pv.get("roi", {})
        payload["lines"] = pv.get("lines", [])
        payload["ticker"] = pv.get("ticker", [])
    except Exception:
        pass
    return payload


def main() -> int:
    payload = build()
    blob = json.dumps(payload, default=str)
    leaks = anon_scrub.audit(blob)
    if leaks:
        print(json.dumps({"ok": False, "wrote": False, "leaks": leaks[:10]}))
        return 1  # fail-closed: never write/push a leak
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(blob)
    pushed = None
    if PUT_URL:
        try:
            req = urllib.request.Request(PUT_URL, data=blob.encode(), method="PUT",
                                         headers={"Content-Type": "application/json"})
            if PUT_TOKEN:
                req.add_header("Authorization", f"Bearer {PUT_TOKEN}")
            with urllib.request.urlopen(req, timeout=20) as r:
                pushed = r.status
        except Exception as e:
            pushed = f"push-failed: {e}"
    print(json.dumps({"ok": True, "wrote": str(OUT), "pushed": pushed,
                      "n_tiles": len(payload.get("tiles", []))}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
