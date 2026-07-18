#!/usr/bin/env python3
"""Headless, self-healing YouTube go-live for the 24/7 build-in-public stream.

The gap this closes: the ffmpeg encoder pushes continuously to a reusable stream
key, but the YouTube *broadcast* (the public live event) is a separate object that
ends when the feed drops past YouTube's grace window (e.g. a power-cut). Nothing in
the stack could restart it -- it was started once by hand in Studio -- so a reboot
left the encoder pushing into a dead broadcast ("This live stream recording is not
available"). stream_health_watchdog.sh only babysits the encoder, never the broadcast.

ensure_live() (idempotent): refresh token -> find the reusable stream by key ->
if a live/testing broadcast is already bound to it, done -> else create a new
PUBLIC broadcast with enableAutoStart (YouTube auto-goes-live the moment the
already-running encoder feed is seen) + enableAutoStop=False (a brief blip won't
end it) and bind it to the stream. Run on a loop (launchd) = true 24/7 persistence
with no browser, no Studio, no foreground popup, ever.

One-time prerequisite (the only human step): mint a HailPorts token WITH the
force-ssl scope so this can manage broadcasts:
    python3 scripts/youtube_auth_browser.py --brand hailports --channel-match HailPorts
Until that token exists this exits 0 (no-op) so the keepalive job never crash-loops.
"""
from __future__ import annotations
import json, os, sys, time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv
import requests

load_dotenv(ROOT / ".env")
# YT tokens live in a dedicated unlocked file (the .env is uchg/0o400-locked); it wins.
_YT_TOKENS = ROOT / "data" / "secrets" / "yt_tokens.env"
if _YT_TOKENS.exists():
    load_dotenv(_YT_TOKENS, override=True)

BRAND = os.environ.get("YT_GOLIVE_BRAND", "hailports").upper()
KEY_FILE = ROOT / "data" / "hustle" / "case_study_stream_key.txt"
LOG = ROOT / "data" / "hustle" / "youtube_golive.out"
LOCK = ROOT / "data" / "runtime" / "youtube_golive.lock"
TOKEN_URI = "https://oauth2.googleapis.com/token"
API = "https://www.googleapis.com/youtube/v3"
# Known-anon title (no operator/employer identity). Scrubbed before use.
TITLE = "LIVE: Hailports Autonomous Operations Board — Source-Safe Build in Public"
DESC = ("Watch an anonymized operations board for AI-assisted business workflows: aggregate metrics, "
        "cloaked activity, reversible local changes, and owner-gated outward actions.")
LIVE_STATES = {"live", "liveStarting", "testing", "testStarting"}


def log(msg: str) -> None:
    line = f"{datetime.now().strftime('%Y-%m-%dT%H:%M:%S')} {msg}"
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a") as f:
            f.write(line + "\n")
    except Exception:
        pass
    print(line, flush=True)


def _access_token() -> str | None:
    rt = os.environ.get(f"YT_REFRESH_TOKEN_{BRAND}")
    cid = os.environ.get("YT_CLIENT_ID")
    csec = os.environ.get("YT_CLIENT_SECRET")
    if not (rt and cid and csec):
        return None
    try:
        r = requests.post(TOKEN_URI, data={
            "client_id": cid, "client_secret": csec,
            "refresh_token": rt, "grant_type": "refresh_token"}, timeout=20)
        return r.json().get("access_token")
    except Exception as e:
        log(f"token refresh failed: {e}")
        return None


def _get(tok, path, **params):
    r = requests.get(f"{API}/{path}", headers={"Authorization": f"Bearer {tok}"},
                     params=params, timeout=20)
    return r.json()


def _post(tok, path, body=None, **params):
    r = requests.post(f"{API}/{path}", headers={
        "Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
        params=params, data=json.dumps(body) if body is not None else None, timeout=20)
    return r.json()


def _stream_id_for_key(tok, key: str):
    data = _get(tok, "liveStreams", part="id,cdn,status", mine="true", maxResults=50)
    for it in data.get("items", []):
        if (it.get("cdn", {}).get("ingestionInfo", {}) or {}).get("streamName") == key:
            return it["id"]
    # fall back: a single owned stream is unambiguous
    items = data.get("items", [])
    if len(items) == 1:
        return items[0]["id"]
    if "error" in data:
        log(f"liveStreams error: {json.dumps(data.get('error'))[:200]}")
    return None


def _live_broadcast_bound(tok, stream_id: str):
    for status in ("active", "upcoming"):
        data = _get(tok, "liveBroadcasts", part="id,status,contentDetails",
                    broadcastStatus=status, mine="true", maxResults=50)
        for it in data.get("items", []):
            life = it.get("status", {}).get("lifeCycleStatus")
            bound = it.get("contentDetails", {}).get("boundStreamId")
            if bound == stream_id and life in LIVE_STATES:
                return it["id"], life
    return None, None


def ensure_live() -> dict:
    if LOCK.exists() and (time.time() - LOCK.stat().st_mtime) < 300:
        return {"ok": True, "skipped": "locked"}
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    LOCK.touch()
    try:
        tok = _access_token()
        if not tok:
            log(f"no YT_REFRESH_TOKEN_{BRAND} (or client creds) — run "
                f"scripts/youtube_auth_browser.py --brand {BRAND.lower()} --channel-match HailPorts")
            return {"ok": False, "reason": "no_token"}
        key = KEY_FILE.read_text().strip() if KEY_FILE.exists() else ""
        if not key:
            return {"ok": False, "reason": "no_stream_key"}
        sid = _stream_id_for_key(tok, key)
        if not sid:
            return {"ok": False, "reason": "stream_not_found_for_key"}
        bid, life = _live_broadcast_bound(tok, sid)
        if bid:
            return {"ok": True, "already_live": True, "broadcast": bid, "state": life}
        # create a fresh broadcast, autostart on so the running encoder feed lights it up
        start = (datetime.now(timezone.utc) + timedelta(seconds=30)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        body = {
            "snippet": {"title": TITLE[:100], "description": DESC[:5000], "scheduledStartTime": start},
            "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False},
            "contentDetails": {"enableAutoStart": True, "enableAutoStop": False,
                               "enableDvr": True, "latencyPreference": "low"},
        }
        created = _post(tok, "liveBroadcasts", body=body, part="snippet,status,contentDetails")
        if "id" not in created:
            log(f"create broadcast failed: {json.dumps(created.get('error', created))[:300]}")
            return {"ok": False, "reason": "create_failed", "detail": created.get("error")}
        new_bid = created["id"]
        bound = _post(tok, "liveBroadcasts/bind", part="id,contentDetails", id=new_bid, streamId=sid)
        if "id" not in bound:
            log(f"bind failed: {json.dumps(bound.get('error', bound))[:300]}")
            return {"ok": False, "reason": "bind_failed", "broadcast": new_bid, "detail": bound.get("error")}
        log(f"created+bound new broadcast {new_bid} (autostart on) -> youtube.com/live/{new_bid}")
        return {"ok": True, "created": new_bid, "watch": f"https://youtube.com/live/{new_bid}"}
    finally:
        try:
            LOCK.unlink()
        except Exception:
            pass


if __name__ == "__main__":
    r = ensure_live()
    print(json.dumps(r))
    # expected pre-auth / idle states are not failures (keep launchd from flagging the keepalive)
    benign = r.get("ok") or r.get("reason") in {"no_token", "no_stream_key"}
    sys.exit(0 if benign else 1)
