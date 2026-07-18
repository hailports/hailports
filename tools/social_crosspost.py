#!/usr/bin/env python3
"""social_crosspost.py — paced, off-screen cross-poster for the AI Built Fast premium
Shorts. Same render that goes to YouTube = free reach on TikTok / Instagram Reels / X
video, but ONLY on AI-Built-Fast-aligned business handles. Never the @persona1 cute-animal
brand (that's cross-contamination).

Doctrine (matches the YouTube uploader):
  - VERIFIED-ONLY: a platform post is recorded POSTED only after the platform itself
    confirms the upload (TikTok content-manager caption match via upload-cdp.js). No
    optimistic "posted" writes.
  - PACED: at most one successful cross-post per platform per run (a launchd cadence,
    not a burst on a 0-follower account).
  - DEDUP: per (platform, render) in data/runtime/social/crosspost.jsonl — a render is
    cross-posted to each platform at most once.
  - SOURCE OF TRUTH: only cross-post renders already VERIFIED public on YouTube (read
    from the studio uploader's state log) — so we never amplify something unproven, and
    the YouTube 1/run cap naturally paces the whole fleet.
  - ANON-SAFE: caption built from the product metadata, scanned through core/pii_guard;
    faceless, no surname, drives to the product link.

Gate model: each platform needs a logged-in business session on its own CDP port. Where
the session is present the post fires off-screen and is verified; where it's missing the
run records a BLOCKED gate (which anon handle + which port to log into) and does nothing
— never a fake post.

  .venv/bin/python tools/social_crosspost.py            # post next eligible, all ready platforms
  .venv/bin/python tools/social_crosspost.py --dry-run  # show what WOULD post + gate status
  .venv/bin/python tools/social_crosspost.py --platform tiktok
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(os.path.expanduser("~/claude-stack"))
sys.path.insert(0, str(ROOT))

from tools.yt_studio_upload import PRODUCT_LINKS, PRODUCT_META, _base_topic  # reuse copy

try:
    from core import pii_guard
    HAVE_PII = True
except Exception:
    HAVE_PII = False

PREMIUM_DIR = ROOT / "data" / "hustle" / "premium_shorts"
YT_STATE = ROOT / "data" / "runtime" / "youtube" / "studio_uploads.jsonl"
STATE = ROOT / "data" / "runtime" / "social" / "crosspost.jsonl"
UPLOAD_CDP_JS = Path(os.path.expanduser("~/.openclaw/workspace/scripts/upload-cdp.js"))

# Each business platform target: its own CDP port (a logged-in business session) + the
# anon handle the owner must create/log-in once. ports chosen clear of 18806 (gumroad)
# and 18866 (youtube uploader) and the persona1/persona2 TikTok ports 18801/18802.
PLATFORMS = {
    "tiktok": {"port": 18870, "account": "redacted", "handle": "@redacted",
               "kind": "tiktok_cdp"},
    "instagram": {"port": 18871, "account": "redacted", "handle": "@redacted",
                  "kind": "unimplemented"},
    "x": {"port": 18872, "account": "redacted", "handle": "@redacted",
          "kind": "unimplemented"},
}

MAX_POST_PER_RUN = 1  # per platform


def _published_youtube() -> list[dict]:
    """Renders verified public on YouTube, newest first — the only cross-post sources."""
    out: list[dict] = []
    if not YT_STATE.exists():
        return out
    for ln in YT_STATE.read_text().splitlines():
        try:
            r = json.loads(ln)
        except Exception:
            continue
        if r.get("state", "").startswith("published") and r.get("video"):
            out.append(r)
    # de-dupe by video path, keep latest record
    seen, uniq = set(), []
    for r in reversed(out):
        v = os.path.abspath(r["video"])
        if v in seen:
            continue
        seen.add(v)
        uniq.append(r)
    return uniq


def _already(platform: str) -> set[str]:
    done: set[str] = set()
    if not STATE.exists():
        return done
    for ln in STATE.read_text().splitlines():
        try:
            r = json.loads(ln)
        except Exception:
            continue
        if r.get("platform") == platform and r.get("state") == "posted" and r.get("video"):
            done.add(os.path.abspath(r["video"]))
    return done


def _record(rec: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    rec = dict(rec)
    rec["at"] = datetime.now(timezone.utc).isoformat()
    with STATE.open("a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _caption(video: str) -> str:
    """Short social caption from the render's product metadata + link, pii-scanned."""
    topic = ""
    try:
        topic = json.loads((Path(video).parent / "metadata.json").read_text()).get("topic", "")
    except Exception:
        pass
    base = _base_topic(topic)
    pm = PRODUCT_META.get(base, {})
    link = PRODUCT_LINKS.get(base, "https://redacted.com")
    hook = pm.get("title", "Practical systems for small business")
    tags = pm.get("tags", ["smallbusiness", "entrepreneur"])[:5]
    hashtags = " ".join("#" + t.replace(" ", "") for t in tags)
    cap = f"{hook}\n\nGet it: {link}\n{hashtags}"
    if HAVE_PII:
        ok, issues = pii_guard.pii_ok(cap)
        if not ok:
            raise SystemExit(f"pii_guard blocked caption: {issues}")
    return cap


def _cdp_up(port: int) -> bool:
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=4)
        return True
    except Exception:
        return False


def _post_tiktok(cfg: dict, video: str, caption: str, dry: bool) -> dict:
    if not _cdp_up(cfg["port"]):
        return {"state": "blocked",
                "reason": f"no business TikTok session on :{cfg['port']} — owner: log "
                          f"{cfg['handle']} into a Chrome started with "
                          f"--remote-debugging-port={cfg['port']} (off-screen), then it auto-posts"}
    if dry:
        return {"state": "ready", "reason": "session up; would post via upload-cdp.js"}
    if not UPLOAD_CDP_JS.exists():
        return {"state": "blocked", "reason": f"upload-cdp.js missing at {UPLOAD_CDP_JS}"}
    cmd = ["node", str(UPLOAD_CDP_JS), "--account", cfg["account"],
           "--video", video, "--caption", caption]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=420)
    except subprocess.TimeoutExpired:
        return {"state": "failed", "reason": "upload-cdp.js timed out"}
    out = (p.stdout or "") + (p.stderr or "")
    # upload-cdp.js prints 'POSTED' only after content-manager caption verification
    if "POSTED" in out and p.returncode == 0:
        return {"state": "posted", "reason": "verified on TikTok content manager"}
    return {"state": "failed", "reason": out.strip()[-300:] or f"rc={p.returncode}"}


def run(only: str | None, dry: bool) -> int:
    sources = _published_youtube()
    if not sources:
        print("no YouTube-verified renders to cross-post yet")
        return 0

    results = []
    for platform, cfg in PLATFORMS.items():
        if only and platform != only:
            continue
        done = _already(platform)
        if cfg["kind"] == "unimplemented":
            results.append({"platform": platform, "state": "blocked",
                            "reason": f"poster not implemented + no session on :{cfg['port']}; "
                                      f"owner: create/login {cfg['handle']} business account, "
                                      f"then a {platform} web-upload flow can be wired (see "
                                      f"data/FULLSEND_VIDEO_SCALE.md)"})
            continue
        posted = 0
        for r in sources:
            if posted >= MAX_POST_PER_RUN:
                break
            video = os.path.abspath(r["video"])
            if not os.path.exists(video):
                continue
            if video in done:
                continue
            caption = _caption(video)
            if cfg["kind"] == "tiktok_cdp":
                res = _post_tiktok(cfg, video, caption, dry)
            else:
                res = {"state": "blocked", "reason": "no handler"}
            res.update({"platform": platform, "video": video,
                        "yt_url": r.get("url") or r.get("shorts_url", "")})
            results.append(res)
            if res["state"] == "posted":
                posted += 1
                done.add(video)
                _record(res)
            elif res["state"] in ("blocked", "ready"):
                break  # session-level gate; don't loop every render
            else:
                _record(res)
    for r in results:
        print(json.dumps(r, ensure_ascii=False))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--platform", choices=list(PLATFORMS))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    return run(args.platform, args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
