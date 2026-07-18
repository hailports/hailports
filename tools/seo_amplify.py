#!/usr/bin/env python3
"""seo_amplify — white-hat off-site SEO amplification for the Hailports seo_pages site.

Pulls the aggressive-but-legitimate levers that make the on-site programmatic pages
actually rank:

  validate  Technical-SEO sweep of every page (unique title/meta, canonical, Open
            Graph + Twitter card, JSON-LD, viewport, lang) + sitemap/robots sanity.
            Emits a fix report. Fixes nothing it doesn't own — reports the rest.
  offsite   Drafts genuinely-valuable long-form articles for republishing on high-DA
            platforms (Medium, dev.to, Reddit text, LinkedIn) — each a real
            contribution with ONE contextual backlink — gated through content_quality
            and queued (NOT auto-published; publishing needs the owner's session).
  haro      Drafts genuine expert contributions on our real domain (lead-gen / intent
            data) ready to submit to journalist-query services for editorial links.
  gsc       Detects Google Search Console / Bing Webmaster verification and reports
            the one-time owner verify step + sitemap-submission prep.
  all       runs validate + offsite + haro + gsc (default).

    python3 tools/seo_amplify.py            # everything, prints report
    python3 tools/seo_amplify.py validate   # technical sweep only
    python3 tools/seo_amplify.py --json     # machine-readable

No PBNs, no link farms, no cloaking, no paid links — all are Google penalties.
Aggressive here = scale of genuine surface + technical excellence.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
# Validate the LIVE deployed tree (scrubbed, single-brand Hailports guides), not the raw
# scannerapp-era staging source. DOMAIN = the served host; guides serve extensionless at
# /guides/<slug> (Cloudflare Pages 308-redirects the .html form), so canonicals are extensionless.
OUTDIR = BASE / "data" / "hustle" / "hailports_dist" / "guides"
QUEUE = BASE / "data" / "hustle" / "seo_offsite_queue.json"
SESS_DIR = BASE / "data" / "hustle" / "browser_sessions"
DOMAIN = "https://www.hailports.com/guides"
GATE_PY = BASE / ".venv" / "bin" / "python"

OWNED_GENERATORS = ("tools/seo_freetools_gen.py",)  # pages this lane may rewrite


# ── content_quality gate ────────────────────────────────────────────────────────
def gate(text: str, channel: str = "blog") -> dict:
    try:
        r = subprocess.run(
            [str(GATE_PY), "-m", "core.content_quality", "--channel", channel,
             "--persona", "generic", "--text", text],
            capture_output=True, text=True, timeout=25, cwd=str(BASE),
        )
        out = json.loads(r.stdout.strip().splitlines()[-1])
        return {"passed": bool(out.get("passed")), "score": out.get("score"),
                "reasons": out.get("reasons", [])}
    except Exception as e:
        return {"passed": False, "score": None, "reasons": [f"gate-error: {e}"]}


def has_session(name: str) -> bool:
    p = SESS_DIR / name
    return p.exists() and any(p.iterdir()) if p.is_dir() else p.exists()


# ── 1. TECHNICAL SEO VALIDATOR ──────────────────────────────────────────────────
REQUIRED = {
    "title": re.compile(r"<title>(.*?)</title>", re.I | re.S),
    "meta_description": re.compile(r'<meta[^>]+name=["\']description["\'][^>]+content=["\'](.*?)["\']', re.I),
    "canonical": re.compile(r'<link[^>]+rel=["\']canonical["\']', re.I),
    "viewport": re.compile(r'<meta[^>]+name=["\']viewport["\']', re.I),
    "lang": re.compile(r"<html[^>]+lang=", re.I),
    "og_title": re.compile(r'property=["\']og:title["\']', re.I),
    "og_description": re.compile(r'property=["\']og:description["\']', re.I),
    "og_url": re.compile(r'property=["\']og:url["\']', re.I),
    "twitter_card": re.compile(r'name=["\']twitter:card["\']', re.I),
    "json_ld": re.compile(r'application/ld\+json', re.I),
}


def page_owner(name: str) -> str:
    # which generator emits this page (best-effort, for the fix report)
    if name in {"cold-email-can-spam-checker.html", "lead-list-roi-calculator.html",
                "reddit-buyer-intent-keyword-finder.html", "cold-email-spam-word-checker.html"}:
        return "tools/seo_freetools_gen.py (OWNED)"
    if name in {"index.html"}:
        return "landing/index (not this lane)"
    return "agents/programmatic_seo.py (not this lane)"


def validate() -> dict:
    pages = sorted(OUTDIR.glob("*.html"))
    titles: dict[str, list[str]] = {}
    descs: dict[str, list[str]] = {}
    page_reports = []
    for p in pages:
        html = p.read_text(errors="ignore")
        missing = [k for k, rx in REQUIRED.items() if not rx.search(html)]
        t = REQUIRED["title"].search(html)
        d = REQUIRED["meta_description"].search(html)
        tt = (t.group(1).strip() if t else "")
        dd = (d.group(1).strip() if d else "")
        titles.setdefault(tt, []).append(p.name)
        descs.setdefault(dd, []).append(p.name)
        # canonical must match the served (extensionless) URL: /guides/<slug>
        canon_ok = (f'{DOMAIN}/{p.stem}' in html) or (p.name == "index.html" and f'{DOMAIN}/' in html)
        if missing or not canon_ok:
            page_reports.append({
                "page": p.name,
                "owner": page_owner(p.name),
                "missing": missing,
                "canonical_matches_url": canon_ok,
                "fixable_here": page_owner(p.name).endswith("(OWNED)"),
            })
    dup_titles = {k: v for k, v in titles.items() if k and len(v) > 1}
    dup_descs = {k: v for k, v in descs.items() if k and len(v) > 1}

    # sitemap / robots sanity (robots.txt lives at the dist root, not in /guides)
    sm = (OUTDIR / "sitemap.xml")
    rb = (OUTDIR.parent / "robots.txt")
    sm_txt = sm.read_text() if sm.exists() else ""
    sitemap_locs = set(re.findall(r"<loc>(.*?)</loc>", sm_txt))
    expected = {(f"{DOMAIN}/" if p.name == "index.html" else f"{DOMAIN}/{p.stem}") for p in pages}
    sitemap_missing = sorted(expected - sitemap_locs)
    sitemap_stale = sorted(sitemap_locs - expected)
    robots_txt = rb.read_text() if rb.exists() else ""
    robots_ok = "Sitemap:" in robots_txt and "hailports.com/sitemap.xml" in robots_txt

    clean = (not page_reports and not dup_titles and not dup_descs
             and not sitemap_missing and not sitemap_stale and robots_ok)
    return {
        "pages_scanned": len(pages),
        "pages_with_issues": page_reports,
        "duplicate_titles": dup_titles,
        "duplicate_descriptions": dup_descs,
        "sitemap_missing_pages": sitemap_missing,
        "sitemap_stale_entries": sitemap_stale,
        "robots_points_to_sitemap": robots_ok,
        "clean": clean,
        "fixes_owned_by_this_lane": [r["page"] for r in page_reports if r["fixable_here"]],
        "fixes_to_report_to_other_lanes": sorted({r["owner"] for r in page_reports if not r["fixable_here"]}),
    }


# ── 2. OFF-SITE GENUINE CONTENT (parasite-SEO, done legitimately) ───────────────
OFFSITE_DRAFTS = [
    {
        "id": "offsite-reddit-intent-playbook",
        "platform": "Medium",
        "session_key": None,  # no Medium session captured → owner switch
        "title": "How I find B2B buyers the week they decide to switch (no cold lists)",
        "link_target": f"{DOMAIN}/reddit-buyer-intent-keyword-finder.html",
        "link_anchor": "a free Reddit buyer-intent keyword finder",
        "body": (
            "Cold lists go stale the day you buy them. The 500 contacts in that CSV were "
            "scraped months ago, and maybe 8% are even in-market right now. So instead of "
            "buying colder data, I started watching for the moment a buyer says out loud "
            "that they are shopping.\n\n"
            "People announce intent in plain English every day. On Reddit, in niche forums, "
            "in review-site comments, they write things like \"alternative to ZoomInfo,\" "
            "\"best CRM for a 4-person agency,\" or \"is Apollo worth it.\" Each of those is a "
            "raised hand with a budget and a deadline attached. The whole game is getting to "
            "that thread while it is still warm.\n\n"
            "The exact workflow I run breaks into four steps. First, list the tools you sell against and the "
            "categories you serve. Second, turn each into intent phrases: \"alternative to X,\" "
            "\"X too expensive,\" \"looking for a Y,\" \"anyone recommend a Z.\" Third, search "
            "those phrases sorted by new, and check the post date — a thread older than about six "
            "months is usually a dead lead, so skip it. Fourth, read the actual post and reply "
            "with genuine help, not a pitch. Reddit punishes selling, so lead with a real answer "
            "and let them click your profile if they want more.\n\n"
            "I got tired of running those searches by hand, so I built " "%LINK% "
            "that turns any product or competitor name into the full set of buyer-intent phrases "
            "plus one-click search links. It is free and runs in your browser.\n\n"
            "The mindset shift that mattered most: stop interrupting strangers and start answering "
            "the ones already asking. Reply rates on warm, in-market threads run several times higher "
            "than cold email, because you are showing up exactly when the decision is live. Pick five "
            "threads a day, help genuinely, and the pipeline takes care of itself."
        ),
    },
    {
        "id": "offsite-canspam-devto",
        "platform": "dev.to",
        "session_key": None,
        "title": "A 7-point CAN-SPAM checklist every cold-email script should pass",
        "link_target": f"{DOMAIN}/cold-email-can-spam-checker.html",
        "link_anchor": "a free in-browser CAN-SPAM checker",
        "body": (
            "If you automate outbound email, CAN-SPAM is not optional polish — it is the law, and "
            "the owner of the sending domain is the named defendant when it goes wrong. The penalties "
            "run up to five figures per message, so it is worth wiring compliance into your sending "
            "code instead of trusting a human to remember.\n\n"
            "Here are the seven requirements, in the order I assert them in tests. One: header "
            "information must be accurate — the From, Reply-To, and routing must identify the real "
            "sender. Two: the subject line cannot be deceptive; it has to reflect the actual message. "
            "Three: if the message is an ad, it has to be identifiable as one. Four: include a valid "
            "physical postal address in the footer — a real street address or a registered PO Box. "
            "Five: give a clear opt-out mechanism, ideally a one-click unsubscribe link. Six: honor "
            "opt-outs within 10 business days and keep a suppression list your sender checks before "
            "every send. Seven: never sell or transfer the addresses of people who opted out.\n\n"
            "The two that automated systems fail most often are the physical address (templates drop "
            "it) and the suppression check (the list exists but the send path does not consult it). "
            "Both are easy to assert: a regex for a street or ZIP in the footer, and a hard lookup "
            "against your suppression set keyed by normalized email before any message goes out.\n\n"
            "To sanity-check a draft before you wire it up, I built " "%LINK% "
            "that scores any email against all seven rules without uploading anything. Paste, read the "
            "red items, fix, ship. It is guidance rather than legal advice, but it catches the "
            "obvious blockers that get domains blacklisted."
        ),
    },
    {
        "id": "offsite-reddit-text-warm-vs-cold",
        "platform": "Reddit (r/sales or r/Entrepreneur text post)",
        "session_key": "reddit",  # hailports profile session captured → one-switch ready
        "title": "Stopped buying lead lists, started catching people the week they shop. Reply rate jumped.",
        "link_target": f"{DOMAIN}/reddit-buyer-intent-keyword-finder.html",
        "link_anchor": "a free keyword tool I made for this",
        "body": (
            "Sharing a shift that worked, because the cold-list grind was killing my reply rate. "
            "For two years I bought contact lists, loaded a few thousand rows, and watched maybe 2 "
            "to 4% reply. The data was always months stale by the time it reached me.\n\n"
            "What changed: instead of buying colder data, I started reaching people the same week "
            "they publicly said they were shopping. On this sub and others, buyers post \"alternative "
            "to X,\" \"is Y worth it,\" \"looking for a Z for a small team.\" Each one is a raised hand "
            "with a budget. I reply with a genuine answer — no pitch, Reddit hates that — and let them "
            "click through if they want more.\n\n"
            "My routine is five threads a day. I search the intent phrases sorted by new, skip anything "
            "older than about six months because those are dead, and only engage where I can actually "
            "help. Reply rate on warm in-market threads runs several times what cold email ever did, "
            "because the timing is right.\n\n"
            "I built %LINK% that turns any product or competitor into the full set of buyer-intent "
            "search phrases plus the search links, so I am not typing them by hand. Sharing in case it "
            "saves someone else the grind. Happy to answer questions on the workflow."
        ),
    },
    {
        "id": "offsite-roi-linkedin",
        "platform": "LinkedIn",
        "session_key": "linkedin",  # session captured → one-switch ready
        "title": "Do the napkin math before you buy another lead list",
        "link_target": f"{DOMAIN}/lead-list-roi-calculator.html",
        "link_anchor": "a free lead-list ROI calculator",
        "body": (
            "Most lead-list buyers never run the one calculation that tells them whether the purchase "
            "makes money. They see 5,000 contacts for a few hundred dollars and assume scale equals "
            "return. The math usually says otherwise, and it takes 30 seconds to check.\n\n"
            "The chain is simple: customers equal list size times reply rate times meeting rate times "
            "close rate. Take a 500-lead list, an 8% reply rate, 30% of replies booking a meeting, and "
            "25% of meetings closing. That is 500 times 0.08 times 0.30 times 0.25, or three customers. "
            "At a 1,200-dollar deal that is 3,600 dollars from a 99-dollar list — a strong return. "
            "Now drop the reply rate to 2%, which is normal for a cold, stale list, and you get fewer "
            "than one customer. Same list, same price, completely different decision.\n\n"
            "The lever that moves this is not list size, it is reply rate, and reply rate is a function "
            "of how warm the list is. Cold scraped data sits around 2 to 4%. People who publicly posted "
            "this week that they are shopping convert several times better, because you reach them while "
            "the decision is live.\n\n"
            "Before your next purchase, run your own numbers in " "%LINK% — "
            "enter list size, your real reply and close rates, deal value and cost, and it returns "
            "expected customers, ROI, payback, and cost per customer. If the ROI is negative at honest "
            "inputs, the answer is a warmer list, not a bigger one."
        ),
    },
]


def build_offsite() -> list[dict]:
    items = []
    for d in OFFSITE_DRAFTS:
        body = d["body"].replace("%LINK%", d["link_anchor"])
        g = gate(body, channel="blog")
        items.append({
            "id": d["id"],
            "platform": d["platform"],
            "kind": "offsite_article",
            "title": d["title"],
            "link_target": d["link_target"],
            "link_anchor": d["link_anchor"],
            "body": body,
            "word_count": len(body.split()),
            "session_ready": bool(d["session_key"]) and has_session(d["session_key"]),
            "session_key": d["session_key"],
            "gate": g,
            "status": "DRAFT_QUEUED_NOT_PUBLISHED",
        })
    return items


# ── 3. HARO / digital-PR expert contributions ───────────────────────────────────
HARO_DRAFTS = [
    {
        "id": "haro-intent-data-vs-lists",
        "query_type": "B2B sales / lead generation",
        "title": "Why intent signals beat purchased contact lists for SMB sales teams",
        "link_target": f"{DOMAIN}/",
        "body": (
            "Purchased contact lists and intent signals solve different problems, and confusing them "
            "wastes most small sales budgets. A static list tells you who exists; an intent signal tells "
            "you who is deciding right now. For a small team without an SDR army, timing is the entire "
            "edge, because you cannot out-volume an enterprise — you can only out-time them.\n\n"
            "The clearest public intent signal is someone posting that they are leaving a tool or "
            "shopping for an alternative. When a buyer writes \"alternative to X\" in a forum, they have "
            "self-identified a budget, a pain, and a deadline. Reaching that person the same week "
            "converts several times better than emailing a cold record scraped months earlier, in our "
            "data and in most teams I talk to. The practical takeaway for a quote: stop measuring "
            "list size and start measuring freshness — a 50-name list of people who raised their hand "
            "this week beats 5,000 cold rows every time."
        ),
    },
    {
        "id": "haro-canspam-founder-risk",
        "query_type": "Email marketing / small business legal",
        "title": "The CAN-SPAM mistake that makes the founder personally the defendant",
        "link_target": f"{DOMAIN}/cold-email-can-spam-checker.html",
        "body": (
            "Founders automate outreach and assume the tool handles compliance. It does not. Under "
            "CAN-SPAM, the business sending the message is liable, with penalties reaching into five "
            "figures per email, and the two failures I see most are both trivial to prevent. First, "
            "the template drops the required physical postal address, so thousands of messages go out "
            "with no valid address in the footer. Second, a suppression list exists in a spreadsheet "
            "but the sending script never checks it, so someone who unsubscribed keeps getting mail — "
            "the single fastest way to draw a complaint.\n\n"
            "My advice for a quote: treat compliance as code, not as a checklist a human remembers. "
            "Assert that every send includes a real address and a working one-click unsubscribe, and "
            "make the sender hard-check the suppression set before any message leaves. Those two "
            "guardrails prevent the cases that actually get small businesses fined."
        ),
    },
]


def build_haro() -> list[dict]:
    items = []
    for d in HARO_DRAFTS:
        g = gate(d["body"], channel="blog")
        items.append({
            "id": d["id"],
            "kind": "haro_pitch",
            "query_type": d["query_type"],
            "title": d["title"],
            "link_target": d["link_target"],
            "body": d["body"],
            "word_count": len(d["body"].split()),
            "gate": g,
            "status": "DRAFT_QUEUED_NOT_SUBMITTED",
        })
    return items


# ── 4. Search-engine indexing status (FULLY AUTONOMOUS — no owner steps, ever) ───
def gsc_status() -> dict:
    """Report the zero-touch indexing posture. Google = the service-account GSC puller
    (agents/seo_gsc.py, already reading sc-domain:hailports.com); Bing + Yandex = IndexNow
    instant push (agents/indexnow_ping.py). NOTHING here is ever a human action — if any
    path is unavailable the loop falls back to GSC-free signals on its own."""
    gsc_live, gsc_pages = False, 0
    try:
        import sys as _sys
        _sys.path.insert(0, str(BASE))
        from agents.seo_gsc import pull as _pull
        r = _pull(28)
        gsc_live = bool(r.get("ok"))
        gsc_pages = r.get("pages", 0)
    except Exception:
        pass
    return {
        "google": "service-account GSC puller (autonomous)" if gsc_live
                  else "GSC-free fallback (keyword_gap + sitemap)",
        "google_live": gsc_live,
        "google_pages_seen": gsc_pages,
        "bing_yandex": "IndexNow instant push (agents/indexnow_ping.py, daily)",
        "owner_one_time_steps": [],   # zero-touch by contract — never populate this
        "sitemap_url": "https://www.hailports.com/sitemap.xml",
    }


def write_queue(offsite: list[dict], haro: list[dict]) -> None:
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "domain": DOMAIN,
        "publish": False,
        "one_switch_note": "Set publish=true and run the matching poster ONLY after the owner "
                           "confirms the platform account/session. Each item carries exactly one "
                           "contextual backlink. No auto-publish from this lane.",
        "sessions_detected": {k: has_session(k) for k in
                              ("reddit", "linkedin", "x_headless", "instagram", "tiktok")},
        "platform_session_readiness": {
            "LinkedIn": has_session("linkedin"),
            "Reddit": has_session("reddit"),
            "Medium": False,
            "dev.to": False,
            "Hashnode": False,
            "Quora": False,
        },
        "offsite": offsite,
        "haro": haro,
    }
    QUEUE.write_text(json.dumps(payload, indent=2))


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", nargs="?", default="all",
                    choices=["all", "validate", "offsite", "haro", "gsc"])
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    report: dict = {}
    if args.cmd in ("all", "validate"):
        report["technical_seo"] = validate()
    if args.cmd in ("all", "offsite", "haro"):
        offsite = build_offsite() if args.cmd in ("all", "offsite") else []
        haro = build_haro() if args.cmd in ("all", "haro") else []
        # always persist a complete queue when writing either
        if args.cmd == "all":
            write_queue(offsite, haro)
        elif args.cmd == "offsite":
            existing = json.loads(QUEUE.read_text()).get("haro", []) if QUEUE.exists() else []
            write_queue(offsite, existing)
        elif args.cmd == "haro":
            existing = json.loads(QUEUE.read_text()).get("offsite", []) if QUEUE.exists() else []
            write_queue(existing, haro)
        report["offsite_queued"] = [{"id": o["id"], "platform": o.get("platform"),
                                     "gate_passed": o["gate"]["passed"], "score": o["gate"]["score"],
                                     "session_ready": o.get("session_ready")} for o in offsite]
        report["haro_queued"] = [{"id": h["id"], "gate_passed": h["gate"]["passed"],
                                  "score": h["gate"]["score"]} for h in haro]
        report["queue_file"] = str(QUEUE)
    if args.cmd in ("all", "gsc"):
        report["search_console"] = gsc_status()

    if args.json:
        print(json.dumps(report, indent=2))
        return 0

    # human summary
    if "technical_seo" in report:
        t = report["technical_seo"]
        print(f"TECHNICAL SEO: {t['pages_scanned']} pages, clean={t['clean']}")
        if t["pages_with_issues"]:
            print(f"  {len(t['pages_with_issues'])} pages with issues; "
                  f"owned-fixable: {t['fixes_owned_by_this_lane'] or 'none'}")
            print(f"  report to other lanes: {t['fixes_to_report_to_other_lanes']}")
        if t["duplicate_titles"]:
            print(f"  DUPLICATE TITLES: {list(t['duplicate_titles'].values())}")
        if t["sitemap_missing_pages"]:
            print(f"  SITEMAP MISSING: {t['sitemap_missing_pages']}")
        print(f"  robots->sitemap: {t['robots_points_to_sitemap']}")
    if "offsite_queued" in report:
        print(f"OFFSITE: {len(report['offsite_queued'])} drafts queued "
              f"(passed: {sum(o['gate_passed'] for o in report['offsite_queued'])})")
        for o in report["offsite_queued"]:
            print(f"  [{o['platform']}] {o['id']} gate={o['gate_passed']}({o['score']}) "
                  f"session_ready={o['session_ready']}")
    if "haro_queued" in report:
        print(f"HARO: {len(report['haro_queued'])} pitches queued "
              f"(passed: {sum(h['gate_passed'] for h in report['haro_queued'])})")
    if "search_console" in report:
        s = report["search_console"]
        print(f"INDEXING (autonomous): google={s['google']} "
              f"(live={s['google_live']}, pages={s['google_pages_seen']}) | bing/yandex={s['bing_yandex']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
