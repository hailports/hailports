#!/usr/bin/env python3
"""yt_studio_upload.py — off-screen Studio uploader that drives an ALREADY-RUNNING,
already-logged-in Chrome over CDP WITHOUT relaunching it.

Unlike agents/youtube_uploader.py (which owns its profile and relaunches Chrome
headless), this attaches to a long-lived owner-logged-in session (the AI Built Fast
brand channel) on a fixed CDP port and never kills / relaunches / focuses the window.
The window stays parked off-screen exactly where the owner left it — we only open a
fresh background tab (Target.createTarget) inside that same browser, drive the upload,
and close our tab. That satisfies the hard off-screen rule.

Reuses the proven CDP driver + shadow-DOM JS from agents/youtube_uploader.py.

CLI:
  # single video, publish PUBLIC, on the AI Built Fast channel:
  python3 tools/yt_studio_upload.py --port 18866 \
      --channel-id UCaKRvPTYIEpFJ72jeVFvj5g \
      --video /abs/sops_9x16.mp4 \
      --title "..." --desc "..." --tags smallbusiness,SOP --public --confirm

  # autonomous batch: upload every ready premium short with a known product link:
  python3 tools/yt_studio_upload.py --port 18866 \
      --channel-id UCaKRvPTYIEpFJ72jeVFvj5g --batch --public --confirm
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(os.path.expanduser("~/claude-stack"))
sys.path.insert(0, str(ROOT))

from agents.youtube_uploader import (  # reuse the battle-tested pieces
    CDP, CDPError, CDPTimeout, DEEP, JS_OPEN_DIALOG, JS_CLICK_UPLOAD_ITEM,
    JS_FILE_INPUT, JS_EDITOR_READY, JS_NOT_MADE_FOR_KIDS, JS_SHOW_MORE,
    JS_VISIBILITY_STEP, JS_NEXT, JS_DONE_ENABLED, JS_CLICK_DONE, JS_SHARE_URL,
    _js_set_box, _js_set_tags, _js_set_visibility, _extract_video_id, scrub, log,
)

try:
    from core import pii_guard
    HAVE_PII = True
except Exception:
    HAVE_PII = False

PREMIUM_DIR = ROOT / "data" / "hustle" / "premium_shorts"
STATE_FILE = ROOT / "data" / "runtime" / "youtube" / "studio_uploads.jsonl"

# topic -> Gumroad product link (the CTA that monetizes each short). Only topics with a
# real link get auto-uploaded by --batch; anything unmapped is skipped (never publish a
# monetized short pointing nowhere).
PRODUCT_LINKS = {
    "sops": "https://docsapp.gumroad.com/l/ofzwla",  # Small Business SOPs
    "cold_email": "https://docsapp.gumroad.com/l/nnxxo",  # Cold Email Swipe File
    "chatgpt_prompts": "https://docsapp.gumroad.com/l/fbayam",  # ChatGPT Prompt Pack for Consultants
    "notion_crm": "https://docsapp.gumroad.com/l/uukxsg",  # Notion CRM
    "rate_calculator": "https://docsapp.gumroad.com/l/qmkdmq",  # Freelancer Rate Calculator
    "business_plan": "https://docsapp.gumroad.com/l/dcwpj",  # Business Plan Template
    "saas_dashboard": "https://docsapp.gumroad.com/l/dfeiro",  # SaaS Metrics Dashboard
    "saas_metrics": "https://docsapp.gumroad.com/l/dfeiro",  # builder topic alias for saas dashboard
}

# Per-product upload metadata (title/desc-base/tags). Keyed by BASE topic; angle-B renders
# ("<base>_b") resolve to the same entry. Without this every product inherited generic
# SOP titles + #SOP tags — wrong copy on 6 of 7 products.
PRODUCT_META = {
    "sops": {"title": "Make your business run without you (steal these 5 SOPs)",
             "desc": "The repeatable systems that let a small business run when you step away.",
             "tags": ["small business", "SOP", "operations", "systems", "entrepreneur", "standard operating procedures"]},
    "cold_email": {"title": "Fix your cold email reply rate (50 send-ready templates)",
                   "desc": "Why cold outreach dies in the first line — and the swipe file that fixes it.",
                   "tags": ["cold email", "outreach", "sales", "B2B", "lead generation", "email templates"]},
    "chatgpt_prompts": {"title": "Make ChatGPT stop sounding like a robot (consultant prompts)",
                        "desc": "The prompts that make AI write proposals like a senior consultant, not corporate mush.",
                        "tags": ["ChatGPT", "AI prompts", "consulting", "freelance", "productivity", "prompt engineering"]},
    "notion_crm": {"title": "Stop losing deals you forgot to follow up on (Notion CRM)",
                   "desc": "Run your whole client pipeline on one Notion board so follow-ups never slip.",
                   "tags": ["Notion", "CRM", "small business", "sales pipeline", "client management", "productivity"]},
    "rate_calculator": {"title": "The freelance rate that actually pays you (free calculator)",
                        "desc": "Stop guessing your rate. Get a floor price you can say like a fact.",
                        "tags": ["freelance", "freelancer rates", "pricing", "consulting", "self employed", "small business"]},
    "business_plan": {"title": "The business plan section lenders actually read (template)",
                      "desc": "A lender- and investor-ready plan with a workbook that builds your projections.",
                      "tags": ["business plan", "small business", "funding", "startup", "entrepreneur", "financial projections"]},
    "saas_metrics": {"title": "Your whole SaaS on one screen (paste-in metrics dashboard)",
                     "desc": "MRR, churn, LTV, CAC payback, and runway — clear enough to send an investor.",
                     "tags": ["SaaS", "startup metrics", "MRR", "churn", "founder", "unit economics"]},
}


def _base_topic(topic: str) -> str:
    """Map an angle-variant topic ('cold_email_b', 'saas_metrics_2') back to its base
    product key for link + metadata lookup."""
    for suf in ("_b", "_2", "_c", "_3"):
        if topic.endswith(suf):
            return topic[: -len(suf)]
    return topic


VIDEO_BUDGET_SEC = 700
MAX_PUBLISH_PER_RUN = 1  # paced: at most one new public Short per 6h batch (no 0-sub burst)


def _oembed_public(video_id: str) -> dict:
    """Authoritative public-resolve check: YouTube oembed returns full metadata ONLY for a
    public/embeddable video; it returns 'Forbidden'/401 for a draft/private/unlisted one.
    Returns {} if not publicly resolvable."""
    import urllib.request
    if not video_id:
        return {}
    url = f"https://www.youtube.com/oembed?url=https://youtu.be/{video_id}&format=json"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        return json.loads(urllib.request.urlopen(req, timeout=15).read().decode("utf-8", "ignore"))
    except Exception:
        return {}


def _latest_short_id(cdp: CDP, channel_id: str, deadline: float) -> tuple[str, str, str]:
    """Resolve the most-recent short's (video_id, title, visibility) from the Studio
    listing — the reliable id source (the upload 'share' dialog leaks YouTube's own tip
    link, so never trust it). Clicking a row title opens a drawer whose URL carries
    ?udvid=<real video id>."""
    cdp.navigate(f"https://studio.youtube.com/channel/{channel_id}/videos/short", settle=8.0)
    cdp.eval(DEEP)
    title = cdp.eval(r"""((window.__deep('ytcp-video-row')[0]&&
        window.__deep('#video-title',window.__deep('ytcp-video-row')[0])[0]||{}).innerText||'').trim()""") or ""
    vis = cdp.eval(r"""(function(){var r=window.__deep('ytcp-video-row')[0];if(!r)return '';
        var lab='';window.__deep('*',r).forEach(function(e){if(e.childElementCount===0){
        var t=(e.innerText||'').trim();if(/^(Public|Private|Unlisted|Scheduled|Draft)$/.test(t)&&!lab)lab=t}});return lab})()""") or ""
    cdp.eval(r"""(function(){var r=window.__deep('ytcp-video-row')[0];if(!r)return;
        var a=window.__deep('a#video-title',r)[0];if(a){a.scrollIntoView();a.click()}})()""")
    import re as _re
    vid = ""
    for _ in range(10):
        if time.monotonic() > deadline:
            break
        time.sleep(1.5)
        href = cdp.eval("location.href") or ""
        m = _re.search(r"[?&]udvid=([\w-]{6,})", href) or _re.search(r"/video/([\w-]{6,})", href)
        if m:
            vid = m.group(1)
            break
    return vid, title, vis


def _republish_draft(cdp: CDP, video_id: str, deadline: float) -> None:
    """A Short can land as a Draft when 'Done' fires before processing finishes. Reopen
    the draft wizard, force Public, and finalize again."""
    cdp.navigate(f"https://studio.youtube.com/video/{video_id}/edit", settle=8.0)
    cdp.eval(DEEP)
    cdp.eval(r"""(function(){var b=window.__deep('ytcp-button,button').find(function(x){
        return /edit draft/i.test((x.innerText||''))});if(b)b.click();})()""")
    time.sleep(6)
    cdp.eval(DEEP)
    for _ in range(10):
        if time.monotonic() > deadline:
            return
        if cdp.eval(JS_VISIBILITY_STEP):
            break
        cdp.eval(JS_NEXT)
        time.sleep(2.5)
    cdp.eval(_js_set_visibility(True))
    time.sleep(2)
    for _ in range(80):
        if time.monotonic() > deadline:
            return
        if cdp.eval(JS_DONE_ENABLED):
            cdp.eval(JS_CLICK_DONE)
            break
        time.sleep(3)
    time.sleep(5)


def _verify_active_channel(cdp: CDP, channel_id: str, deadline: float) -> bool:
    """Navigate the fresh tab to Studio for `channel_id` and HARD-VERIFY the active
    channel + that we are not bounced to a Google login (session-isolation guard)."""
    cdp.navigate(f"https://studio.youtube.com/channel/{channel_id}", settle=8.0)
    for _ in range(12):
        if time.monotonic() > deadline:
            return False
        href = cdp.eval("location.href") or ""
        if "accounts.google" in href or "ServiceLogin" in href:
            log("FAIL: bounced to Google login — session is NOT authenticated")
            return False
        active = cdp.eval(r"((location.href.match(/channel\/(UC[\w-]+)/)||[])[1])||''")
        if active == channel_id:
            return True
        time.sleep(2)
    return False


def upload(channel_id: str, video: str, title: str, desc: str, tags: list[str],
           *, port: int, public: bool, confirm: bool) -> dict:
    deadline = time.monotonic() + VIDEO_BUDGET_SEC

    def left() -> float:
        return max(1.0, deadline - time.monotonic())

    video = os.path.abspath(video)
    if not os.path.exists(video):
        return {"ok": False, "state": "failed", "reason": "video file missing", "video": video}

    title = scrub(title)[:100]
    desc = scrub(desc)[:4900]
    tags = [t for t in (scrub(t) for t in tags) if t][:15]

    # Draft-spam circuit breaker: the brandmaster Google account's default channel is HailPorts.
    # A mis-passed --channel-id (or a silent switch) staging a file there leaves a private DRAFT
    # on HailPorts. Refuse unless explicitly allowed. Set YT_ALLOW_HAILPORTS=1 to override.
    if channel_id == "UC_NaJ6WEpYsNlq-CIGD8yAw" and os.environ.get("YT_ALLOW_HAILPORTS", "0") != "1":
        return {"ok": False, "state": "blocked_wrong_channel",
                "reason": "channel_id is HailPorts; refusing to upload (draft-spam guard). "
                          "Set YT_ALLOW_HAILPORTS=1 to override.", "video": video}

    if HAVE_PII:
        ok, issues = pii_guard.pii_ok(title + "\n" + desc)
        if not ok:
            return {"ok": False, "state": "blocked", "reason": f"pii_guard: {issues}", "video": video}

    cdp = CDP(port)
    try:
        # attach to the EXISTING browser; CDP.connect() opens a fresh background tab
        # (Target.createTarget about:blank) — it never relaunches/focuses the window.
        cdp.connect(timeout=min(20.0, left()))

        if not _verify_active_channel(cdp, channel_id, deadline):
            return {"ok": False, "state": "failed",
                    "reason": "could not verify active channel (isolation/auth guard)", "video": video}
        log(f"active channel verified: {channel_id}")

        cdp.eval(DEEP)

        st = cdp.eval(JS_OPEN_DIALOG)
        if st == "create-clicked":
            time.sleep(2.5)
            up = cdp.eval(JS_CLICK_UPLOAD_ITEM)
            if up != "ok":
                log(f"upload menu item: {up} — direct-navigating to upload dialog")
                cdp.navigate(f"https://studio.youtube.com/channel/{channel_id}/videos/upload?d=ud", settle=6.0)
                cdp.eval(DEEP)
            else:
                time.sleep(3)
        elif st == "no-create":
            cdp.navigate(f"https://studio.youtube.com/channel/{channel_id}/videos/upload?d=ud", settle=6.0)
            cdp.eval(DEEP)

        staged = False
        for _ in range(8):
            if time.monotonic() > deadline:
                break
            if cdp.set_file_input(JS_FILE_INPUT, video, timeout=min(30.0, left())):
                staged = True
                break
            time.sleep(2)
        if not staged:
            return {"ok": False, "state": "failed", "reason": "file input never appeared", "video": video}
        log(f"file staged ({os.path.getsize(video)} bytes)")

        ready = False
        for _ in range(45):
            if time.monotonic() > deadline:
                break
            if cdp.eval(JS_EDITOR_READY):
                ready = True
                break
            time.sleep(2)
        if not ready:
            return {"ok": False, "state": "private_draft",
                    "reason": "editor never mounted; left as private draft", "video": video}
        log("editor mounted")

        t_res = cdp.eval(_js_set_box("#title-textarea #textbox", title))
        d_res = cdp.eval(_js_set_box("#description-textarea #textbox", desc))
        mfk = cdp.eval(JS_NOT_MADE_FOR_KIDS)
        tag_res = "skipped"
        if tags:
            try:
                cdp.eval(JS_SHOW_MORE)
                time.sleep(1.5)
                tag_res = cdp.eval(_js_set_tags(tags))
            except Exception as e:
                tag_res = f"err:{str(e)[:30]}"
        log(f"title={t_res!r} desc={d_res!r} mfk={mfk} tags={tag_res}")
        time.sleep(2)

        if not confirm:
            return {"ok": True, "state": "dry_run_draft", "video": video, "title": title,
                    "reason": "staged file + metadata, did NOT finalize (private draft)"}

        reached = False
        for _ in range(8):
            if time.monotonic() > deadline:
                break
            if cdp.eval(JS_VISIBILITY_STEP):
                reached = True
                break
            cdp.eval(JS_NEXT)
            time.sleep(2.5)
        if not reached:
            return {"ok": False, "state": "private_draft",
                    "reason": "could not reach Visibility step; left as private draft", "video": video}

        vis = cdp.eval(_js_set_visibility(public))
        log(f"visibility -> {vis} ({'PUBLIC' if public else 'PRIVATE'} requested)")
        time.sleep(1.5)

        done_ok = False
        for _ in range(80):
            if time.monotonic() > deadline:
                break
            if cdp.eval(JS_DONE_ENABLED):
                done_ok = cdp.eval(JS_CLICK_DONE)
                break
            time.sleep(3)
        if not done_ok:
            return {"ok": False, "state": "private_draft",
                    "reason": "Done never enabled (still processing); left as private draft", "video": video}
        time.sleep(5)

        # VERIFIED-ONLY: resolve the real id from the Studio listing (never the share
        # dialog — it leaks YouTube's own tip link), then confirm it actually resolves
        # public via oembed. If it landed as a Draft, republish once and re-verify.
        if not public:
            vid_id, _, _ = _latest_short_id(cdp, channel_id, deadline)
            return {"ok": True, "state": "published_private", "video_id": vid_id,
                    "url": f"https://youtu.be/{vid_id}" if vid_id else "",
                    "channel_id": channel_id, "title": title, "video": video}

        vid_id, lst_title, vis = _latest_short_id(cdp, channel_id, deadline)
        log(f"listing -> id={vid_id} title={lst_title!r} visibility={vis!r}")
        oe = _oembed_public(vid_id)
        if not oe and (vis == "Draft" or not vis) and time.monotonic() < deadline:
            log("not public yet -> republishing draft")
            _republish_draft(cdp, vid_id, deadline)
            for _ in range(6):
                oe = _oembed_public(vid_id)
                if oe:
                    break
                time.sleep(5)

        if oe and oe.get("title"):
            url = f"https://www.youtube.com/watch?v={vid_id}"
            log(f"VERIFIED PUBLIC -> {url} [{oe.get('author_name')}]")
            return {"ok": True, "state": "published_public", "url": url,
                    "shorts_url": f"https://www.youtube.com/shorts/{vid_id}",
                    "video_id": vid_id, "channel_id": channel_id,
                    "title": title, "verified_title": oe.get("title"),
                    "author": oe.get("author_name"), "video": video}
        return {"ok": False, "state": "unverified_draft", "video_id": vid_id,
                "channel_id": channel_id, "title": title, "video": video,
                "reason": "finalized but oembed never confirmed public (likely still a draft/processing)"}

    except (CDPTimeout, CDPError) as e:
        return {"ok": False, "state": "failed", "reason": f"cdp: {str(e)[:120]}", "video": video}
    except Exception as e:
        return {"ok": False, "state": "failed", "reason": str(e)[:160], "video": video}
    finally:
        cdp.close()


def _record(res: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    res = dict(res)
    res["at"] = datetime.now(timezone.utc).isoformat()
    with STATE_FILE.open("a") as f:
        f.write(json.dumps(res, ensure_ascii=False) + "\n")


def _topic_of(video: str) -> str:
    try:
        m = json.loads((Path(video).parent / "metadata.json").read_text())
        return m.get("topic", "")
    except Exception:
        return ""


def _published_videos() -> set[str]:
    """Absolute paths of renders already published (per state log). Dedup is per RENDER,
    not per topic, so distinct angle-A / angle-B shorts of the same product both go live —
    each render dir is unique, so this never republishes the same file."""
    out: set[str] = set()
    if not STATE_FILE.exists():
        return out
    for ln in STATE_FILE.read_text().splitlines():
        try:
            r = json.loads(ln)
        except Exception:
            continue
        if r.get("state", "").startswith("published") and r.get("video"):
            out.add(os.path.abspath(r["video"]))
    return out


def _title_for(topic: str, meta: dict) -> str:
    t = (meta.get("yt_title") or "").strip()
    if t:
        return t
    pm = PRODUCT_META.get(_base_topic(topic))
    if pm:
        return pm["title"]
    return f"{topic.replace('_', ' ').title()} — small business tips"


def _desc_for(topic: str, meta: dict, link: str) -> str:
    pm = PRODUCT_META.get(_base_topic(topic), {})
    base = meta.get("yt_desc") or pm.get("desc") or (
        "Quick, practical systems for small business owners. "
        "Save this and put it to work this week.")
    tags = pm.get("tags") or ["small business", "operations", "systems", "entrepreneur"]
    hashtags = " ".join("#" + t.replace(" ", "") for t in tags[:5])
    return f"{base}\n\nGet it here: {link}\n{hashtags}"


def batch(channel_id: str, *, port: int, public: bool, confirm: bool) -> list[dict]:
    """Upload every ready premium short whose topic has a known product link and that
    hasn't already been published. Off-screen, one tab per video, sequential."""
    results = []
    done_videos = _published_videos()
    published_this_run = 0
    for run_dir in sorted(PREMIUM_DIR.glob("*_9x16_*")):
        mp4s = list(run_dir.glob("*.mp4"))
        meta_p = run_dir / "metadata.json"
        if not mp4s or not meta_p.exists():
            continue
        try:
            meta = json.loads(meta_p.read_text())
        except Exception:
            continue
        topic = meta.get("topic", "")
        link = PRODUCT_LINKS.get(_base_topic(topic))
        video = os.path.abspath(str(mp4s[0]))
        if not link:
            results.append({"ok": False, "state": "skipped", "video": video,
                            "reason": f"no product link mapped for topic {topic!r}"})
            continue
        if video in done_videos:
            results.append({"ok": True, "state": "already_published", "topic": topic, "video": video})
            continue
        if confirm and public and published_this_run >= MAX_PUBLISH_PER_RUN:
            results.append({"ok": True, "state": "deferred", "topic": topic, "video": video,
                            "reason": f"paced: {MAX_PUBLISH_PER_RUN}/run cap reached, next 6h run"})
            continue
        pm = PRODUCT_META.get(_base_topic(topic), {})
        title = _title_for(topic, meta)
        desc = _desc_for(topic, meta, link)
        tags = pm.get("tags") or ["small business", "operations", "systems", "entrepreneur"]
        log(f"BATCH uploading {topic}: {video}")
        res = upload(channel_id, video, title, desc, tags, port=port, public=public, confirm=confirm)
        res["topic"] = topic
        _record(res)
        results.append(res)
        if res.get("state", "").startswith("published"):
            published_this_run += 1
            done_videos.add(video)
    return results


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=18866)
    ap.add_argument("--channel-id", default="UCaKRvPTYIEpFJ72jeVFvj5g")
    ap.add_argument("--video")
    ap.add_argument("--title", default="")
    ap.add_argument("--desc", default="")
    ap.add_argument("--tags", default="small business,SOP,operations,systems,entrepreneur")
    ap.add_argument("--public", action="store_true")
    ap.add_argument("--confirm", action="store_true")
    ap.add_argument("--batch", action="store_true")
    args = ap.parse_args()

    if args.batch:
        results = batch(args.channel_id, port=args.port, public=args.public, confirm=args.confirm)
    else:
        if not args.video:
            ap.error("--video required (or use --batch)")
        res = upload(args.channel_id, args.video, args.title, args.desc,
                     [t.strip() for t in args.tags.split(",") if t.strip()],
                     port=args.port, public=args.public, confirm=args.confirm)
        _record(res)
        results = [res]

    print()
    for r in results:
        print(json.dumps(r, ensure_ascii=False))
    published = sum(1 for r in results if r.get("state", "").startswith("published"))
    return 0 if (published or not args.confirm) else 2


if __name__ == "__main__":
    raise SystemExit(main())
