#!/usr/bin/env python3
"""youtube_queue_uploader.py — autonomous, token-gated YouTube publisher (Data API v3).

Reads data/runtime/youtube/upload_queue.jsonl and publishes ready, not-yet-uploaded
videos through the documented Data API (tools/youtube_api_upload.upload_video) using the
per-brand refresh token YT_REFRESH_TOKEN_<BRAND>. NO browser, no Studio DOM, no CDP — so
it never touches any login Chrome profile (incl. :18806) and can't pop a window.

Autonomous-safe by construction:
  * If the brand's refresh token is missing, the entry is SKIPPED (clean no-op) — nothing
    posts until the owner runs the one consent command. Safe to wire into launchd today.
  * A video is only marked uploaded after its public watch URL RESOLVES (oEmbed 200) —
    an unresolved URL is reported honestly and the entry stays queued for retry.
  * --max paces uploads (Data API default quota ~6 uploads/day); default 1 per run.

Usage:
    python3 tools/youtube_queue_uploader.py --list
    python3 tools/youtube_queue_uploader.py                       # dry-run (no upload)
    python3 tools/youtube_queue_uploader.py --confirm             # publish (unlisted)
    python3 tools/youtube_queue_uploader.py --confirm --privacy public --max 2
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(os.path.expanduser("~/claude-stack"))
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from tools.youtube_api_upload import upload_video          # noqa: E402
from agents.youtube_uploader import scrub                  # noqa: E402  (real-name scrub)
from tools.video_anonymity_gate import scan_video_package  # noqa: E402  (pre-publish anonymity wall)

QUEUE_FILE = ROOT / "data" / "runtime" / "youtube" / "upload_queue.jsonl"

# channel -> Data API brand key (YT_REFRESH_TOKEN_<BRAND>). The faceless product channel
# is Built Fast with AI / redacted.com.
CHANNEL_BRAND = {
    "builtfast": "redacted",
    "buyersignal": "buyersignal",
    "fastaiagency": "fastaiagency",
}


def log(m: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)


def read_queue() -> list[dict]:
    if not QUEUE_FILE.exists():
        return []
    rows = []
    for ln in QUEUE_FILE.read_text().splitlines():
        ln = ln.strip()
        if ln:
            try:
                rows.append(json.loads(ln))
            except Exception:
                pass
    return rows


def _key(e: dict) -> str:
    return e.get("fingerprint") or e.get("video") or ""


def update_queue(target: dict, patch: dict) -> None:
    key = _key(target)
    rows = read_queue()
    for e in rows:
        if _key(e) == key:
            e.update(patch)
    tmp = QUEUE_FILE.with_suffix(".jsonl.tmp")
    tmp.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in rows))
    tmp.replace(QUEUE_FILE)
    # mirror into the run's metadata.json
    v = abs_video(target)
    mp = Path(v).parent / "metadata.json"
    if mp.exists():
        try:
            m = json.loads(mp.read_text()); m.update(patch)
            mp.write_text(json.dumps(m, indent=2, ensure_ascii=False))
        except Exception:
            pass


def abs_video(e: dict) -> str:
    v = e.get("video", "")
    return v if os.path.isabs(v) else str(ROOT / v)


def brand_for(e: dict) -> str:
    """Authoritative Data-API key. The channel->key map wins (queue 'brand' fields hold a
    human display name like 'Built Fast with AI' that is NOT a token key)."""
    ch = e.get("channel", "")
    if ch in CHANNEL_BRAND:
        return CHANNEL_BRAND[ch]
    import re
    return re.sub(r"[^a-z0-9]+", "", (e.get("brand") or "").lower()) or ch


def has_token(brand: str) -> bool:
    return bool(os.environ.get(f"YT_REFRESH_TOKEN_{brand.upper()}"))


def url_resolves(video_id: str) -> bool:
    """A public/unlisted video answers YouTube's oEmbed endpoint with HTTP 200; a private
    or non-existent one 401/404s. This is the 'a video isn't posted without a resolving
    URL' verification gate."""
    if not video_id:
        return False
    q = urllib.parse.urlencode({"url": f"https://www.youtube.com/watch?v={video_id}",
                                "format": "json"})
    try:
        with urllib.request.urlopen("https://www.youtube.com/oembed?" + q, timeout=15) as r:
            return r.status == 200
    except Exception:
        return False


def meta_for(e: dict) -> tuple[str, str, list]:
    """Pull title/description/tags from the run's metadata.json (preferred) or the queue
    entry, scrubbed of any real-name token."""
    mp = Path(abs_video(e)).parent / "metadata.json"
    m = {}
    if mp.exists():
        try:
            m = json.loads(mp.read_text())
        except Exception:
            m = {}
    title = scrub(m.get("title") or e.get("title") or "")[:100]
    desc = scrub(m.get("description") or e.get("description") or "")[:4900]
    tags = m.get("tags") or e.get("tags") or []
    tags = [scrub(str(t)) for t in tags if str(t).strip()][:15]
    return title, desc, tags


def scan_package_for(e: dict, title: str, desc: str, tags: list) -> dict:
    """Assemble the full text surface (incl. the raw VO script + path) for the anonymity
    gate. scrub() only sanitizes title/desc/tags; the gate also scans the unscrubbed
    script/beats and the mp4 path so nothing leaks the owner, employer, sibling brands,
    or any secret/PII."""
    mp = Path(abs_video(e)).parent / "metadata.json"
    script = beats = ""
    if mp.exists():
        try:
            m = json.loads(mp.read_text())
            script = str(m.get("script") or "")
            b = m.get("beats")
            beats = " ".join(str(x) for x in b) if isinstance(b, list) else str(b or "")
        except Exception:
            pass
    return {"title": title, "description": desc, "tags": tags,
            "transcript": script, "sidecar": beats, "path": abs_video(e)}


def select(rows: list[dict], channel: str | None, engine: str | None) -> list[dict]:
    out = []
    for e in rows:
        if e.get("uploaded"):
            continue
        if e.get("status") != "ready_for_upload":
            continue
        if channel and e.get("channel") != channel:
            continue
        # default: only premium-builder output — never the old 720p graveyard entries
        if engine and e.get("engine") != engine:
            continue
        out.append(e)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Autonomous Data-API YouTube publisher (token-gated).")
    ap.add_argument("--channel", help="filter to one channel (e.g. builtfast)")
    ap.add_argument("--confirm", action="store_true", help="actually upload (else dry-run)")
    ap.add_argument("--privacy", default="unlisted", choices=["private", "unlisted", "public"])
    ap.add_argument("--max", type=int, default=1, help="max uploads this run (quota pacing)")
    ap.add_argument("--engine", default="premium_short",
                    help="only entries with this engine tag (default premium_short; "
                         "use '' / 'any' to include legacy entries)")
    ap.add_argument("--list", action="store_true")
    a = ap.parse_args()
    engine = None if a.engine in ("", "any", "all") else a.engine

    rows = read_queue()
    if a.list:
        for e in rows:
            mark = "UP" if e.get("uploaded") else "  "
            b = brand_for(e); tok = "tok" if has_token(b) else "NO-TOK"
            print(f"[{mark}] {e.get('channel','?'):12} brand={b:12} {tok:6} "
                  f"{e.get('upload_state', e.get('status',''))}  {abs_video(e)}")
        return 0

    pend = select(rows, a.channel, engine)
    if not pend:
        log("no ready, not-yet-uploaded videos in queue")
        return 0

    done = 0
    results = []
    for e in pend:
        if done >= a.max:
            break
        brand = brand_for(e)
        video = abs_video(e)
        if not os.path.exists(video):
            log(f"skip (missing file): {video}")
            continue
        if not has_token(brand):
            log(f"skip {e.get('channel','?')}: YT_REFRESH_TOKEN_{brand.upper()} not set "
                f"(owner consent pending) — clean no-op")
            continue
        title, desc, tags = meta_for(e)
        if not title:
            log(f"skip {video}: empty title")
            continue
        gate = scan_video_package(scan_package_for(e, title, desc, tags))
        if not gate.get("ok"):
            vio = "; ".join(f"{v['vector']}={v['match']}"
                            for v in gate["violations"] if v["severity"] == "hard")
            log(f"BLOCKED by anonymity gate (never uploaded): {video} -> {vio}")
            update_queue(e, {"upload_state": "blocked_anonymity",
                             "upload_note_last": ("anonymity gate: " + vio)[:300],
                             "upload_attempt_at": datetime.now(timezone.utc).isoformat()})
            results.append({"video": video, "state": "blocked_anonymity",
                            "violations": gate["violations"]})
            continue
        if not a.confirm:
            log(f"DRY-RUN would publish [{a.privacy}] '{title}' -> brand {brand}")
            results.append({"video": video, "state": "dry_run", "title": title})
            done += 1
            continue

        log(f"publishing [{a.privacy}] '{title}' (brand {brand})")
        res = upload_video(brand, video, title, desc, tags, a.privacy)
        if not res.get("ok"):
            log(f"FAILED: {res.get('error')}")
            update_queue(e, {"upload_state": "failed",
                             "upload_note_last": res.get("error", "")[:300],
                             "upload_attempt_at": datetime.now(timezone.utc).isoformat()})
            results.append(res)
            continue

        vid_id = res.get("video_id", "")
        ok_url = url_resolves(vid_id) if a.privacy != "private" else True
        if not ok_url:
            time.sleep(8)
            ok_url = url_resolves(vid_id)
        if ok_url:
            log(f"PUBLISHED + VERIFIED: {res.get('url')} (channel {res.get('channel')})")
            update_queue(e, {"uploaded": True,
                             "upload_state": f"published_{a.privacy}",
                             "video_url": res.get("url", ""),
                             "video_id": vid_id,
                             "uploaded_at": datetime.now(timezone.utc).isoformat()})
            done += 1
        else:
            log(f"uploaded but URL did NOT resolve yet: {res.get('url')} — left queued for re-verify")
            update_queue(e, {"upload_state": "uploaded_unverified",
                             "video_url": res.get("url", ""), "video_id": vid_id,
                             "upload_attempt_at": datetime.now(timezone.utc).isoformat()})
        results.append(res)

    print()
    for r in results:
        print(json.dumps(r, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
