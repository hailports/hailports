#!/usr/bin/env python3
"""yt_live_setup.py — mint (or reuse) a YouTube Live broadcast for the dashboard stream.

Uses the AI Built Fast OAuth refresh token to create a reusable liveStream + a
persistent liveBroadcast (auto-start on, auto-stop off so it stays 24/7), binds them,
and writes the RTMP ingest URL to data/hustle/case_study_stream_key.txt for
dashboard_livestream.py to push to. Idempotent via data/hustle/yt_live_state.json.

  python tools/yt_live_setup.py            # create or reuse
  python tools/yt_live_setup.py --public   # flip privacy to public
"""
from __future__ import annotations
import json, os, sys, urllib.request, urllib.parse, urllib.error
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(os.path.expanduser("~/claude-stack"))
KEY_FILE = ROOT / "data" / "hustle" / "case_study_stream_key.txt"
STATE = ROOT / "data" / "hustle" / "yt_live_state.json"
PRIVACY = "public" if "--public" in sys.argv else "unlisted"

for ln in (ROOT / ".env").read_text().splitlines():
    if "=" in ln and not ln.startswith("#"):
        k, _, v = ln.partition("="); os.environ.setdefault(k.strip(), v.strip().strip('"'))
CID = os.environ["YT_CLIENT_ID"]; CS = os.environ["YT_CLIENT_SECRET"]
_tokf = ROOT / "data" / "hustle" / "yt_live_token.json"
RT = (json.loads(_tokf.read_text())["refresh_token"] if _tokf.exists()
      else os.environ["YT_REFRESH_TOKEN_redacted"])

TITLE = "autonomous operations board -- live"
DESC = ("an anonymized live operations board showing aggregate, bucketed activity. "
        "public metadata intentionally avoids hardware specs, operator details, "
        "domain mentions, and exact business-footprint claims.")


def _tok() -> str:
    d = urllib.parse.urlencode({"client_id": CID, "client_secret": CS,
                                "refresh_token": RT, "grant_type": "refresh_token"}).encode()
    r = urllib.request.Request("https://oauth2.googleapis.com/token", data=d)
    with urllib.request.urlopen(r, timeout=20) as x:
        return json.loads(x.read())["access_token"]


AT = _tok()
def api(method, path, params=None, body=None):
    url = f"https://www.googleapis.com/youtube/v3/{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method,
        headers={"Authorization": f"Bearer {AT}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=30) as x:
            return x.status, json.loads(x.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def main():
    state = {}
    if STATE.exists():
        try: state = json.loads(STATE.read_text())
        except Exception: state = {}

    # 1) reusable stream
    sid = state.get("stream_id"); ingest = state.get("ingest")
    if not sid:
        s, st = api("POST", "liveStreams", {"part": "snippet,cdn,contentDetails"},
                    {"snippet": {"title": "hailports-eternal"},
                     "cdn": {"frameRate": "variable", "ingestionType": "rtmp", "resolution": "variable"},
                     "contentDetails": {"isReusable": True}})
        if s != 200:
            print("STREAM ERROR", s, json.dumps(st.get("error", st))); return 1
        sid = st["id"]
        info = st["cdn"]["ingestionInfo"]
        ingest = info["ingestionAddress"].rstrip("/") + "/" + info["streamName"]

    # 2) broadcast (auto-start, no auto-stop => 24/7)
    bid = state.get("broadcast_id")
    if bid:  # verify still usable
        s, chk = api("GET", "liveBroadcasts", {"part": "status", "id": bid})
        life = (chk.get("items") or [{}])[0].get("status", {}).get("lifeCycleStatus", "")
        if life in ("complete", "revoked", ""):
            bid = None
    if not bid:
        start = (datetime.now(timezone.utc) + timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        s, bc = api("POST", "liveBroadcasts", {"part": "snippet,status,contentDetails"},
                    {"snippet": {"title": TITLE, "description": DESC, "scheduledStartTime": start},
                     "status": {"privacyStatus": PRIVACY, "selfDeclaredMadeForKids": False},
                     "contentDetails": {"enableAutoStart": True, "enableAutoStop": False,
                                        "enableDvr": True, "latencyPreference": "ultraLow"}})
        if s != 200:
            print("BROADCAST ERROR", s, json.dumps(bc.get("error", bc))); return 1
        bid = bc["id"]
        # 3) bind
        s, _ = api("POST", "liveBroadcasts/bind",
                   {"part": "id,contentDetails", "id": bid, "streamId": sid})
        if s != 200:
            print("BIND ERROR", s); return 1

    KEY_FILE.write_text(ingest + "\n")
    STATE.write_text(json.dumps({"stream_id": sid, "broadcast_id": bid,
                                 "ingest": ingest, "privacy": PRIVACY}, indent=2))
    masked = ingest[:ingest.rfind("/") + 5] + "…"
    print(json.dumps({"ok": True, "broadcast_id": bid, "privacy": PRIVACY,
                      "ingest": masked, "watch_url": f"https://www.youtube.com/watch?v={bid}",
                      "key_file": str(KEY_FILE)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
