#!/usr/bin/env python3
"""queue_premium_shorts.py — write upload metadata + queue the rendered premium product
Shorts (scripts/build_premium_short.py output) for the autonomous Data-API publisher.

For each product topic it finds the newest data/hustle/premium_shorts/<topic>_9x16_* run
that has a real .mp4, writes title/description/tags into its metadata.json (faceless,
product link in the description), and appends an idempotent entry to
data/runtime/youtube/upload_queue.jsonl tagged engine="premium_short", channel="builtfast"
(brand key redacted). Nothing is uploaded here — that's the token-gated runner's job.
"""
from __future__ import annotations
import hashlib, json, os, sys
from pathlib import Path

ROOT = Path(os.path.expanduser("~/claude-stack"))
sys.path.insert(0, str(ROOT))
from core.pii_guard import _scan_pii  # noqa: E402

SHORTS = ROOT / "data" / "hustle" / "premium_shorts"
QUEUE = ROOT / "data" / "runtime" / "youtube" / "upload_queue.jsonl"

# Per-product upload metadata. Links: confirmed live /go checkout where one exists; the
# three waitlist products point to the redacted.com store (no live checkout yet — see
# data/YT_SCALE_AND_POSTING.md). All faceless, no surname.
STORE = "https://redacted.com"
PRODUCT_META = {
    "cold_email": {
        "title": "Cold Email Templates You Can Actually Send Today",
        "link": f"{STORE}/go/cold-email-swipe-file", "live": True,
        "what": "A swipe file of 50 ready-to-send cold emails — subject, body and follow-up for each. Fill in three blanks and send.",
        "tags": ["cold email", "email templates", "small business", "outreach", "sales", "freelance", "lead generation", "shorts"],
    },
    "chatgpt_prompts": {
        "title": "200+ ChatGPT Prompts for Consultants (Fill-in-the-Blank)",
        "link": f"{STORE}/go/chatgpt-prompt-pack", "live": True,
        "what": "Fill-in-the-blank ChatGPT prompts for the real work of a consulting business — proposals, discovery, scope, renewals. Tuned to sound like a person.",
        "tags": ["chatgpt", "ai prompts", "consultant", "consulting", "productivity", "ai for business", "prompt pack", "shorts"],
    },
    "notion_crm": {
        "title": "Build a Client CRM in Notion in About 20 Minutes",
        "link": STORE, "live": False,
        "what": "Three import-ready files plus a setup guide that turn Notion into a real client pipeline — drag deals lead to won, with a follow-up view. No monthly software.",
        "tags": ["notion", "notion crm", "crm", "freelance", "small business", "pipeline", "productivity", "shorts"],
    },
    "rate_calculator": {
        "title": "The Freelance Rate That Actually Pays You",
        "link": STORE, "live": False,
        "what": "A calculator that turns your real numbers — salary goal, expenses, weeks off, billable hours — into a floor hourly, day and project price, plus three send-ready proposals.",
        "tags": ["freelance", "freelancer", "pricing", "rates", "consulting", "self employed", "small business", "shorts"],
    },
    "business_plan": {
        "title": "Lender-Ready Business Plan Template (Fill-in-the-Blank)",
        "link": f"{STORE}/go/business-plan-template", "live": True,
        "what": "Every section a bank, SBA lender or investor expects — prompted, in order — plus a linked workbook that builds your 3-year projections as you type.",
        "tags": ["business plan", "small business", "sba loan", "startup", "entrepreneur", "funding", "template", "shorts"],
    },
    "saas_metrics": {
        "title": "Your SaaS MRR, Churn and Runway on One Screen",
        "link": STORE, "live": False,
        "what": "Drop in your monthly numbers and get MRR, churn, expansion, LTV, CAC payback and runway — with trend charts and a tab explaining every formula. 12 months of sample data pre-loaded.",
        "tags": ["saas", "startup metrics", "mrr", "churn", "founder", "saas metrics", "dashboard", "shorts"],
    },
}

CHANNEL = "builtfast"
HANDLE = "@builtfastwithai"
BRAND = "redacted"


def newest_run(topic: str) -> Path | None:
    runs = sorted(SHORTS.glob(f"{topic}_9x16_*"), reverse=True)
    for r in runs:
        mp4 = r / f"{topic}_9x16.mp4"
        if mp4.is_file() and mp4.stat().st_size > 200_000:
            return r
    return None


def build_description(m: dict) -> str:
    line = "Get it here:" if m["live"] else "See it here:"
    tags = " ".join("#" + t.replace(" ", "") for t in m["tags"])
    return f"{m['what']}\n\n{line} {m['link']}\n\n{tags}"


def fingerprint(video: str) -> str:
    return "premium_" + hashlib.sha1(video.encode()).hexdigest()[:16]


def existing_keys() -> set[str]:
    keys = set()
    if QUEUE.exists():
        for ln in QUEUE.read_text().splitlines():
            ln = ln.strip()
            if ln:
                try:
                    keys.add(json.loads(ln).get("fingerprint", ""))
                except Exception:
                    pass
    return keys


def main() -> int:
    have = existing_keys()
    appended, skipped, results = [], [], []
    for topic, m in PRODUCT_META.items():
        run = newest_run(topic)
        if not run:
            results.append({"topic": topic, "state": "no_render"})
            continue
        mp4 = run / f"{topic}_9x16.mp4"
        title, desc = m["title"], build_description(m)
        # faceless guard before anything is written
        hits = [h for t in (title, desc) for h in _scan_pii(t, "premium_short") if h[1] == "HARD"]
        if hits:
            results.append({"topic": topic, "state": "pii_blocked", "hits": str(hits)})
            continue
        # write upload metadata into the run's metadata.json
        mp = run / "metadata.json"
        meta = json.loads(mp.read_text()) if mp.exists() else {}
        meta.update({"title": title, "description": desc, "tags": m["tags"],
                     "channel": CHANNEL, "handle": HANDLE, "brand": BRAND,
                     "product_link": m["link"], "engine": "premium_short"})
        mp.write_text(json.dumps(meta, indent=2, ensure_ascii=False))

        fp = fingerprint(str(mp4))
        entry = {"video": str(mp4), "channel": CHANNEL, "handle": HANDLE, "brand": BRAND,
                 "engine": "premium_short", "topic": topic, "title": title,
                 "description": desc, "tags": m["tags"], "product_link": m["link"],
                 "status": "ready_for_upload", "uploaded": False, "fingerprint": fp}
        if fp in have:
            skipped.append(topic)
            results.append({"topic": topic, "state": "already_queued", "video": str(mp4)})
            continue
        with QUEUE.open("a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        have.add(fp)
        appended.append(topic)
        results.append({"topic": topic, "state": "queued", "video": str(mp4),
                        "live_checkout": m["live"], "link": m["link"]})

    print(json.dumps({"appended": appended, "skipped": skipped, "detail": results}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
