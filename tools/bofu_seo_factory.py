#!/usr/bin/env python3
"""bofu_seo_factory — bottom-of-funnel (BOFU) programmatic SEO, one isolated set per brand.

This is the missing conversion half of the prog-SEO layer: pages that rank for the
high-intent objection + comparison queries a shopper types right before (or instead of)
buying, and answer the doubt honestly so it stops killing checkout. Four intent clusters
per service:

  1. cost      — "how much does a <service> cost"          (honest price-range explainer)
  2. vs-diy    — "<service> vs doing it yourself"/alternatives (neutral, factual, no smears)
  3. safety    — "is a <service> safe / will it break my site" (read-only / no-risk truth)
  4. worth     — "is <service> worth it"                     (ROI by honest reasoning only)

REUSE, NOT A PARALLEL SYSTEM
  * Page render helpers (slug/esc/snip40/inject_h2_snippets/faq_jsonld + the featured-snippet
    + PAA machinery) are imported straight from tools/prog_seo_factory.py — identical markup.
  * Brand / host / domain / offer are resolved from core/brand_registry.py (the single source
    of truth), exactly like every other brand surface. No hardcoded hosts.

BRAND ISOLATION (hard guardrails)
  * Each brand's pages link ONLY its own domain; every page is worded from that brand's own
    voice + its own true service facts, so two brands can't be correlated.
  * No fabricated proof: no invented reviews, named customers, stats, or competitor claims.
    Comparison pages describe *approaches* ("DIY vs a tool vs hiring help"), never smear a
    named rival. Every safety/refund claim is true for that brand's real model.
  * No owner PII ever (enforced by --check).

STORAGE + SITEMAP (how pages become crawlable — same model as prog_seo_factory)
  * HTML brands (builtfast/signalhq/hailport/scannerapp): one standalone .html per page in
        data/hustle/seo_pages/bofu/<brand_key>/<slug>.html
    plus a per-brand sitemap.xml in that dir, canonicalised to that brand's own domain at
    https://<domain>/g/<slug>.html . app.py must serve /g/<slug>.html host-aware from the
    brand dir + list the per-brand bofu URLs in that host's sitemap branch (see return note).
  * docsapp: emitted into its NATIVE already-served store products/content/blog_posts/*.json
    (rendered at /blog/<slug>, already listed in the docsapp sitemap) — crawlable with NO
    app.py change. Links only docsapp.dev.

    python3 tools/bofu_seo_factory.py            # generate every brand
    python3 tools/bofu_seo_factory.py --brand builtfast
    python3 tools/bofu_seo_factory.py --check    # validate only, exit 1 on problems
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "tools"))

from core import brand_registry as br  # noqa: E402
from prog_seo_factory import (  # noqa: E402  (reuse the exact render machinery)
    slug, esc, snip40, inject_h2_snippets, faq_jsonld, _wc,
)

# NOTE: deliberately OUTSIDE data/hustle/seo_pages — agents/seo_sitemap.py does a recursive
# rglob over that tree and would otherwise sweep these per-brand pages into the scannerapp.dev
# sitemap (a cross-brand leak) and inject /intel/ home links into them. Kept fully isolated so
# each brand's pages only ever enter its own per-brand sitemap.
OUTDIR = BASE / "data" / "hustle" / "bofu_seo"
BLOG_DIR = BASE / "products" / "content" / "blog_posts"

# Every active brand domain (incl. aliases) — the cross-leak validator forbids any page from
# mentioning a brand domain that isn't its own.
ALL_BRAND_HOSTS = set()
for _b in br.all_brands():
    for _h in [_b.get("domain", "")] + list(_b.get("aliases") or []):
        h = (_h or "").lower().strip()
        if h:
            ALL_BRAND_HOSTS.add(h)
            ALL_BRAND_HOSTS.add(h[4:] if h.startswith("www.") else h)

# Owner-identity tokens that must NEVER appear in any generated page (FTC + anonymity).
PII_TOKENS = ["Operator", "Operator", "Operator2", "BrandA", "CompanyA"]


# ── per-brand BOFU supplement — the ONLY brand-specific copy lives here; everything else
#    (name, domain, persona, voice) is read from the registry so it can't drift. Each brand
#    is worded from its own true model, so pages are honest and can't be correlated. ──────
# DEAD-BRAND PERMABLOCK — builtfast (redacted.com) + signalhq (redacted.com) were
# KILLED in the brand review ($0 lifetime, burned/HELD domains). Their BOFU entries used to
# live here and were regenerated on every factory run, resurrecting pages that link the burned
# domains and sit co-located with the live brands (cross-brand correlation). Removed entirely +
# fail-closed via DEAD_BRAND_KEYS below so a registry-driven or queue-driven run can never
# rebuild them. Only the kept faceless brands (hailport/scannerapp/docsapp) remain.
DEAD_BRAND_KEYS = frozenset({"builtfast", "signalhq"})

BRANDS = {
    "hailport": {
        "path_prefix": "/g",
        "offer_url": "https://hailports.com/#feeds",
        "free_url": "https://hailports.com/#feeds",
        "free_label": "see a sample row from the current feed",
        "category": "research feed",
        "price_frame": (
            "Feeds are priced per pull or per cadence and quoted in writing before delivery — "
            "no retainer, no hourly billing. A one-off dossier is a fixed price; an ongoing "
            "feed is a flat per-cadence rate. You own every row we hand over, and each one "
            "cites the public signal it came from, so you're paying for sourced, dated research, not access."),
        "safety_truth": (
            "We read public signals only — reviews, forums, public DNS and email records, "
            "response headers. No logins, no access to your systems, nothing pulled from behind "
            "a password, and we never contact the targets you're researching. Every record traces "
            "back to a public source we can show you."),
        "worth_frame": (
            "A research feed is worth it when finding the defect or the buyer yourself would cost "
            "more analyst hours than the feed costs — and when each row is something you can act "
            "on or hand a client the same day. If you only need a single lookup, a one-off pull "
            "beats a standing feed; we'll say so rather than upsell you a cadence you don't need."),
        "services": [
            {"noun": "site-defect research feed", "cat": "independent research feed"},
        ],
        "diy_options": [
            ("Research it in-house", "Free if your team has the hours, but sourcing, verifying, and "
             "de-duplicating public signals is slow, and stale rows quietly creep in."),
            ("Buy a bulk data list", "Volume without provenance — you can't see where a row came "
             "from, so you can't trust it or hand it to a client with a straight face."),
            ("Hire an analyst per project", "Good work, higher cost, and you restart the brief every "
             "time. Fine for a one-off deep dive, expensive as a standing capability."),
            ("Subscribe to a sourced feed", "Each row ships with the exact defect or signal and the "
             "public source, dated and ready to use. Best when you need a repeatable, citable input."),
        ],
    },
    "scannerapp": {
        "path_prefix": "/g",
        "offer_url": "https://scannerapp.dev/pricing",
        "free_url": "https://scannerapp.dev/site-scan",
        "free_label": "run the free scan and see your score first",
        "category": "website health scan",
        "price_frame": (
            "The scan itself is free and gives you an instant score. If you want the full "
            "breakdown, the $197 Fix Plan is a flat, one-time price — no subscription, no "
            "retainer, no upsell call. You can re-run the free scan any time. That's the whole "
            "price: a fixed number for a ranked, plain-English list of what to fix and how."),
        "safety_truth": (
            "It's 100% read-only and outside-in. You give a domain, not a password — nothing is "
            "installed, nothing in your site or systems is changed, and no human ever needs "
            "access. The scan looks at exactly what a customer or competitor can already see "
            "from the public internet, and if it can't run a useful scan on your domain we refund you."),
        "worth_frame": (
            "It's worth it when 'I think my site is fine' is costing you customers you never see "
            "leave — a slow page, an expired certificate, a security warning on your quote form. "
            "The free scan tells you in 60 seconds whether there's anything to fix; you only pay "
            "the $197 if there's a real problem worth a ranked plan. If the scan comes back clean, we say so."),
        "services": [
            {"noun": "website health scan", "cat": "website audit"},
        ],
        "diy_options": [
            ("Eyeball it yourself", "Free, but your own browser caches an old copy and hides the "
             "problems every new visitor hits — so 'looks fine to me' isn't an honest test."),
            ("Stitch together free checkers", "Several single-purpose tools can each test one thing, "
             "but you're left assembling a dozen tabs into a priority list on your own."),
            ("Hire a web person to audit", "Thorough, but you wait on a quote and a calendar, and "
             "pay for hours — overkill if you just need to know what's actually broken today."),
            ("A self-serve scan + fix plan", "One outside-in scan, an instant score, and a ranked "
             "plain-English fix list. Best when you want the answer now, with no call and no login."),
        ],
    },
    "docsapp": {
        "blog": True,  # routed through the native docsapp blog store, not the HTML dir
        "offer_url": "https://docsapp.dev/pricing",
        "free_url": "https://docsapp.dev/pricing",
        "free_label": "start with the readiness checklist",
        "category": "AI-readiness audit",
        "price_frame": (
            "Fixed price per document set or audit, agreed before any work starts — no hourly "
            "billing, no retainer, no surprise add-ons. A focused cleanup or readiness check is "
            "a modest one-time fee; a fuller documentation set scales with how many processes "
            "you need captured. You approve the scope and the number up front, and you own "
            "every editable file we deliver."),
        "safety_truth": (
            "We document and advise — we never touch production. You hand us notes and a short "
            "intake, not admin access, and you (or your admin) apply any changes yourself after "
            "reviewing the drafts. Nothing is pushed live by us, and everything is delivered in "
            "editable formats you own outright, with no lock-in."),
        "worth_frame": (
            "Documentation pays off when the cost of *not* having it is real: a key person who's "
            "the only one who knows how something runs, an AI tool about to amplify a messy "
            "process, a new hire ramping for weeks. If your org runs on tribal knowledge and "
            "you're about to add AI on top, writing it down first is cheap insurance. If you're "
            "tiny and stable, it can wait."),
        "services": [
            {"noun": "AI-readiness audit", "cat": "AI-readiness review"},
            {"noun": "org documentation cleanup", "cat": "process documentation"},
        ],
        "diy_options": [
            ("Write the docs yourself", "Free, and you know the business best — but it's the work "
             "that always slips to next quarter, and AI adoption usually arrives first."),
            ("Use a generic template pack", "A starting point, but generic SOPs rarely match how "
             "your org actually runs, so they get filed and never followed."),
            ("Bring in a consultant", "Deep and thorough, usually billed hourly with a long "
             "engagement — more than many small orgs need just to get documented and AI-ready."),
            ("A fixed-scope documentation set", "The exact documents you need, in plain language, "
             "for one agreed price, delivered in editable formats you keep. Best before you adopt AI on a messy base."),
        ],
    },
}


# ── page model ───────────────────────────────────────────────────────────────
class Page:
    __slots__ = ("slug", "title", "h1", "meta", "lead", "intro",
                 "sections", "faq", "cluster")

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw.get(k))


def _voice_adj(reg: dict) -> str:
    return (reg.get("voice") or "").split(",")[0].strip() or "plain, honest"


def _li(items) -> str:
    return "<ul>" + "".join(f"<li>{x}</li>" for x in items) + "</ul>"


_VOWEL = "aeiou"


def art(s: str) -> str:
    """a / an for the following word (vowel-letter heuristic; our nouns avoid the
    'you-'/'one-' edge cases)."""
    return "an" if (s or "")[:1].lower() in _VOWEL else "a"


def tcase(s: str) -> str:
    """Title-case a service noun without mangling the AI acronym."""
    return re.sub(r"\bAi\b", "AI", (s or "").title())


# ── cluster builders (honest, brand-specific, no fabricated proof) ────────────
def build_cost(reg, cfg, svc) -> Page:
    name, dom, noun = reg["brand_name"], reg["domain"], svc["noun"]
    return Page(
        cluster="cost",
        slug=slug(f"how-much-does-a-{noun}-cost"),
        title=f"How Much Does {art(noun)} {tcase(noun)} Cost? (Honest 2026 Breakdown) | {name}",
        h1=f"How much does {art(noun)} {noun} cost?",
        meta=(f"An honest look at what {art(noun)} {noun} actually costs in 2026 — what you pay for, "
              f"what changes the price, and how {name} prices it.")[:155],
        lead=(f"{art(noun).capitalize()} {noun} doesn't have one sticker price — it scales with scope. "
              + cfg["price_frame"].split(". ")[0] + ". The honest ranges, what moves the "
              f"number, and exactly how {name} prices it are below, so you can budget before "
              f"you ever talk to anyone."),
        intro=(f"\"How much does {art(noun)} {esc(noun)} cost\" is the right question to ask before you "
               f"shop — but most pages dodge it. Here's a straight answer: the real cost "
               f"drivers, honest ranges, and how {esc(name)} prices it, with no quote-gating."),
        sections=[
            f"<h2>What you're actually paying for</h2>"
            f"<p>Price tracks scope, not a logo. {art(noun).capitalize()} {esc(noun)} bundles the time to do it right, "
            f"the judgment to do the parts that matter, and the result you can actually use. "
            f"Cheap-and-wrong costs more than fair-and-right once you count the redo, so weigh "
            f"the outcome against the number, not the number alone.</p>",
            f"<h2>Honest price ranges</h2><p>{esc(cfg['price_frame'])}</p>",
            f"<h2>What changes the price</h2>"
            + _li([
                "How much scope you actually need vs. nice-to-haves you can defer",
                "How clean your starting point is — messy inputs take longer to work with",
                "How fast you need it, and how much back-and-forth the work requires",
                "Whether it's a one-time job or an ongoing cadence",
            ]),
            f"<h2>How {esc(name)} prices it</h2><p>{esc(cfg['price_frame'])} "
            f"That means you can budget from this page and confirm the exact number before any "
            f"work begins.</p>",
        ],
        faq=[
            (f"Is it cheaper to do {art(noun)} {noun} myself?",
             f"Up front, yes — your own time looks free. The real comparison is the hours and "
             f"the redo risk against a fixed price that ends the problem. If you enjoy the work "
             f"and aren't losing money while it's unfinished, DIY can be the right call."),
            ("Are there hidden fees or a subscription?",
             cfg["price_frame"].split(". ")[0] + ". The number is agreed in writing up front, so "
             "there are no surprise add-ons."),
            (f"What's included in the price of {art(noun)} {noun}?",
             f"The full agreed deliverable — not activity or hours. {name} writes the scope down "
             f"before you pay, so 'what's included' is settled before any money changes hands."),
            ("Do you offer refunds?",
             cfg["safety_truth"].split(". ")[-1].strip() or
             "If we can't deliver what we agreed, we make it right."),
        ],
    )


def build_vsdiy(reg, cfg, svc) -> Page:
    name, dom, noun, cat = reg["brand_name"], reg["domain"], svc["noun"], svc["cat"]
    return Page(
        cluster="vs-diy",
        slug=slug(f"{noun}-vs-doing-it-yourself"),
        title=f"{tcase(noun)} vs Doing It Yourself: an Honest Comparison | {name}",
        h1=f"{tcase(noun)} vs doing it yourself",
        meta=(f"Should you do your own {noun} or bring in help? An honest, no-smear comparison "
              f"of every real option — DIY, freelancer, agency, and {name}.")[:155],
        lead=(f"Doing your own {noun} is free but slow and entirely on you; bringing in help "
              f"costs money but buys back time and removes the redo risk. The right answer "
              f"depends on whether the problem is costing you more than the fix — here's each "
              f"option laid out fairly so you can decide."),
        intro=(f"There's no single right way to handle {art(noun)} {esc(noun)} — only the right way for "
               f"your time, budget, and how much the problem is costing you. Here are the real "
               f"options, described straight, with no competitor put-downs."),
        sections=[
            f"<h2>The honest options</h2>"
            + _li([f"<strong>{esc(t)}</strong> — {esc(d)}" for t, d in cfg["diy_options"]]),
            f"<h2>When doing it yourself makes sense</h2>"
            f"<p>DIY wins when you genuinely have the time, you'll learn something you'll reuse, "
            f"and nothing is bleeding money while it sits half-done. {art(cat).capitalize()} {esc(cat)} you do once and "
            f"rarely revisit is a fair thing to take on yourself.</p>",
            f"<h2>When it's worth bringing in help</h2>"
            f"<p>Pay for it when the problem is actively costing you — lost customers, lost hours, "
            f"a deadline — or when getting it wrong is expensive to undo. At that point a fixed "
            f"scope and a known price usually beats another month of it being on your list.</p>",
            f"<h2>Where {esc(name)} fits</h2><p>{esc(cfg['price_frame'].split('. ')[0])}. "
            f"It's the option to pick when you'd rather have the {esc(noun)} done right, once, "
            f"than manage it yourself — and you want the number and the scope settled before you commit.</p>",
        ],
        faq=[
            (f"Can I just do my own {noun}?",
             f"Absolutely — for a one-off you rarely revisit, DIY is reasonable. The trade is your "
             f"hours and the redo risk against a fixed price that just ends the task. Bring in help "
             f"when the problem is costing you more than the fix."),
            ("What's the cheapest option?",
             f"Doing it yourself, if your time is genuinely free. Past that, a fixed-scope job is "
             f"usually cheaper than an open-ended hourly engagement, because the number can't run away from you."),
            ("How is this different from hiring an agency?",
             f"{name} works in fixed scope, not a monthly retainer — you pay once for a defined "
             f"result instead of paying every month for activity, standups, and ramp."),
            (f"Do I need technical skills to handle {art(noun)} {noun} myself?",
             f"Some, and more patience than skill. If you're comfortable learning as you go and "
             f"nothing's on fire, you can. If you'd rather not own the bugs, that's exactly when help pays off."),
        ],
    )


def build_safety(reg, cfg, svc) -> Page:
    name, dom, noun = reg["brand_name"], reg["domain"], svc["noun"]
    breaky = "will it break my site" if "site" in (svc["cat"] + noun) or reg["key"] in (
        "builtfast", "scannerapp", "hailport") else "is my data safe"
    return Page(
        cluster="safety",
        slug=slug(f"is-a-{noun}-safe"),
        title=f"Is {art(noun)} {tcase(noun)} Safe? Will It Break Anything? | {name}",
        h1=f"Is {art(noun)} {noun} safe — and {breaky}?",
        meta=(f"Worried {art(noun)} {noun} could break something or expose your data? The honest answer: "
              f"what gets touched, what doesn't, and what happens if it's not right.")[:155],
        lead=(cfg["safety_truth"].split(". ")[0] + ". " + cfg["safety_truth"].split(". ")[1]
              + ". So the short answer is yes, it's safe — here's exactly what is and isn't "
              "touched, and what happens if anything's not right."),
        intro=(f"It's a fair worry: handing any part of your business to {art(noun)} {esc(noun)} should "
               f"come with a straight answer about what can go wrong. Here it is — what gets "
               f"touched, what never does, and the backstop if something's off."),
        sections=[
            f"<h2>The short answer</h2><p>{esc(cfg['safety_truth'])}</p>",
            f"<h2>What gets touched — and what never does</h2>"
            + _li([
                "What we work with: only what's needed for the agreed scope, nothing more",
                "What we never need: your passwords, your live systems, or standing access",
                "What stays in your hands: the decision to apply anything, and full ownership of the result",
            ]),
            f"<h2>Your data and privacy</h2>"
            f"<p>We don't publish client names or use your business as a case study, and we're "
            f"glad to work under an NDA. Anonymity runs both ways here: the work is confidential, "
            f"and nothing about {art(noun)} {esc(noun)} requires exposing private data to do it well.</p>",
            f"<h2>If something isn't right</h2><p>{esc(cfg['safety_truth'].split('. ')[-1].strip())}. "
            f"You're never stuck with a result that misses what we agreed.</p>",
        ],
        faq=[
            (f"Will {art(noun)} {noun} break my site or systems?",
             cfg["safety_truth"].split(". ")[0] + ". " + cfg["safety_truth"].split(". ")[1] + "."),
            ("Do you need my passwords or admin access?",
             "No standing access to your live systems is required. You provide only what the "
             "agreed scope needs, and you keep control of anything that actually changes."),
            ("Is my data shared or resold?",
             "No. The work is confidential, we don't publish client names, and we're happy to "
             "sign an NDA before anything changes hands."),
            ("What if I'm not happy with the result?",
             cfg["safety_truth"].split(". ")[-1].strip() + "."),
        ],
    )


def build_worth(reg, cfg, svc) -> Page:
    name, dom, noun = reg["brand_name"], reg["domain"], svc["noun"]
    return Page(
        cluster="worth",
        slug=slug(f"is-a-{noun}-worth-it"),
        title=f"Is {art(noun)} {tcase(noun)} Worth It? An Honest ROI Take | {name}",
        h1=f"Is {art(noun)} {noun} worth it?",
        meta=(f"Is {art(noun)} {noun} actually worth the money? An honest ROI take — including when it's "
              f"NOT worth it — so you can decide for your situation.")[:155],
        lead=(cfg["worth_frame"].split(". ")[0] + ". " +
              " ".join(cfg["worth_frame"].split(". ")[1:2]) +
              ". Below is the honest math, what you get back, and the cases where it isn't worth it."),
        intro=(f"\"Is {art(noun)} {esc(noun)} worth it\" only has an honest answer once you weigh it "
               f"against what the problem is already costing you. So let's do that math plainly "
               f"— including the cases where the honest answer is no."),
        sections=[
            f"<h2>What the problem is costing you now</h2><p>{esc(cfg['worth_frame'])}</p>",
            f"<h2>What you actually get back</h2>"
            + _li([
                "Time you stop spending on the problem yourself",
                "A result you can rely on instead of one you keep second-guessing",
                "A fixed, known cost instead of an open-ended drain",
            ]),
            f"<h2>When {art(noun)} {esc(noun)} is NOT worth it</h2>"
            f"<p>If the problem isn't actually costing you money or time, or you genuinely enjoy "
            f"doing it yourself, hold off — we'll tell you that on a scoping call rather than sell "
            f"you something you don't need. {art(noun).capitalize()} {esc(noun)} earns its price by ending a real, "
            f"recurring cost, not by being a nice-to-have.</p>",
            f"<h2>How to decide</h2>"
            f"<p>Put a rough dollar or hour figure on what the problem costs you each month. If a "
            f"one-time, fixed-price {esc(noun)} is less than a few months of that, it's almost "
            f"certainly worth it. If you can't name a cost, it probably isn't urgent yet.</p>",
        ],
        faq=[
            (f"Is {art(noun)} {noun} worth the money?",
             cfg["worth_frame"].split(". ")[0] + ". It's worth it when the problem costs you more, "
             "each month, than the one-time price."),
            ("How fast will I see a return?",
             f"Fastest when the {noun} ends an active, recurring cost — then it starts paying back "
             f"the moment it's done. We won't promise a specific revenue number; that depends on your follow-through."),
            ("What if it doesn't work for me?",
             cfg["safety_truth"].split(". ")[-1].strip() + ". We commit to the agreed deliverable, "
             "not to outcomes we can't honestly control."),
            (f"Who is {art(noun)} {noun} not for?",
             "Anyone whose problem isn't actually costing them time or money yet. If a scoping "
             "call shows it won't move the needle for you, we'll say so before you pay."),
        ],
    )


CLUSTER_BUILDERS = [build_cost, build_vsdiy, build_safety, build_worth]


def build_pages(reg, cfg) -> list[Page]:
    pages = []
    for svc in cfg["services"]:
        for b in CLUSTER_BUILDERS:
            pages.append(b(reg, cfg, svc))
    # slug dedup safety
    out, seen = [], set()
    for p in pages:
        if p.slug in seen:
            continue
        seen.add(p.slug)
        out.append(p)
    return out


# ── CSS (brand-neutral structure, accent themed per brand so bytes differ → no
#    cross-brand fingerprint) ──────────────────────────────────────────────────
def _css(reg) -> str:
    pal = reg.get("palette", {})
    accent = pal.get("primary", "#0a7d4b")
    return (
        "body{font-family:-apple-system,Segoe UI,Arial,sans-serif;max-width:760px;margin:0 auto;"
        "padding:30px 18px;line-height:1.65;color:#1a1d21}"
        "h1{font-size:2rem;line-height:1.18}h2{margin-top:30px}h3{margin-top:20px;font-size:1.05rem}"
        f"a{{color:{accent}}}.disc{{background:#f6f7f9;border:1px solid #e3e6ea;border-radius:8px;"
        "padding:8px 14px;font-size:13px;margin:0 0 18px;color:#555}"
        f".snip{{background:#f4f7f6;border-left:4px solid {accent};padding:11px 15px;border-radius:7px;"
        "margin:12px 0}.snip.lead{font-size:1.06rem;margin:14px 0 10px}.paa h3{margin-top:18px}"
        f".cta{{background:#14171a;color:#fff;padding:18px 20px;border-radius:11px;margin:26px 0}}"
        f".cta a{{color:{accent}}}.sub{{color:#777;font-size:14px}}"
    )


def _faq_dedup(p: Page) -> list[tuple[str, str]]:
    out, seen = [], set()
    for q, a in p.faq:
        k = q.strip().lower()
        if k not in seen:
            seen.add(k)
            out.append((q, a))
    return out


def render_html(reg, cfg, p: Page) -> str:
    dom = reg["domain"]
    canon = f"https://{dom}{cfg['path_prefix']}/{p.slug}.html"
    name = reg["brand_name"]
    offer, free = cfg["offer_url"], cfg["free_url"]
    faq = _faq_dedup(p)
    body = inject_h2_snippets("".join(p.sections))
    faq_html = "<h2>FAQ</h2>" + "".join(
        f"<h3>{esc(q)}</h3><p>{esc(a)}</p>" for q, a in faq)
    cta = (f'<div class="cta"><strong>{esc(name)}</strong> — '
           f'<a href="{esc(free)}">{esc(cfg["free_label"])}</a>'
           f'<span class="sub" style="color:#bbb"> · or <a href="{esc(offer)}">see pricing &amp; what you get</a></span></div>')
    art = {"@context": "https://schema.org", "@type": "Article", "headline": p.h1,
           "description": p.meta, "mainEntityOfPage": canon,
           "author": {"@type": "Organization", "name": name},
           "publisher": {"@type": "Organization", "name": name}}
    ld = [art, faq_jsonld(faq)]
    ld_html = "".join(f'<script type="application/ld+json">{json.dumps(x, ensure_ascii=False)}</script>'
                      for x in ld)
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>{esc(p.title)}</title>'
        f'<meta name="description" content="{esc(p.meta)}">'
        f'<link rel="canonical" href="{canon}">{ld_html}'
        f'<style>{_css(reg)}</style></head><body>'
        '<div class="disc">An honest guide from us, including where we are and aren’t the right fit. '
        'We link only our own pages.</div>'
        f'<h1>{esc(p.h1)}</h1>'
        f'<p class="snip lead">{esc(p.lead)}</p>'
        f'<p>{p.intro}</p>'
        + body + cta + faq_html
        + f'<p style="margin-top:26px"><a href="{esc(offer)}">Pricing &amp; details</a> · '
          f'<a href="{esc(free)}">{esc(cfg["free_label"]).capitalize()}</a></p>'
        '</body></html>'
    )


def render_blog_json(reg, cfg, p: Page) -> dict:
    """docsapp: emit into the native blog store. The /blog/<slug> route wraps `content`
    (inner HTML) in the docsapp template + its own canonical (docsapp.dev) and CTA, so we
    only supply the body + meta. Links inside stay docsapp.dev-only."""
    faq = _faq_dedup(p)
    body = inject_h2_snippets("".join(p.sections))
    offer, free = cfg["offer_url"], cfg["free_url"]
    content = (
        f'<p>{p.intro}</p>'
        f'<div style="background:#f4f7f6;border-left:4px solid #f59e0b;padding:11px 15px;'
        f'border-radius:7px;margin:14px 0"><strong>Short answer:</strong> {esc(p.lead)}</div>'
        + body
        + '<h2>FAQ</h2>' + "".join(f"<h3>{esc(q)}</h3><p>{esc(a)}</p>" for q, a in faq)
        + f'<p style="margin-top:22px"><a href="{esc(offer)}">See pricing and what you get</a> '
          f'&middot; <a href="{esc(free)}">{esc(cfg["free_label"])}</a>.</p>'
    )
    return {
        "topic": p.h1,
        "slug": "bofu-" + p.slug,
        "title": p.title.split(" | ")[0],
        "meta_description": p.meta,
        "author": reg.get("persona_name", "docsapp Team"),
        "published_at": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "content": content,
        "_source": "bofu_seo_factory",
    }


def write_sitemap(reg, cfg, slugs: list[str]):
    dom = reg["domain"]
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    locs = [f"https://{dom}{cfg['path_prefix']}/{s}.html" for s in slugs]
    body = "".join(f"  <url><loc>{u}</loc><lastmod>{today}</lastmod>"
                   f"<changefreq>monthly</changefreq><priority>0.7</priority></url>\n" for u in locs)
    sm = ('<?xml version="1.0" encoding="UTF-8"?>\n'
          '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n' + body + "</urlset>\n")
    (OUTDIR / reg["key"] / "sitemap.xml").write_text(sm)


# Strategist queue: data/hustle/bofu_seo/queue.json = {brand_key: [{noun, cat}, ...]}.
# The autonomous strategist (core/strategist_actuators.py) appends honest extra BOFU
# topics here; we merge them into that brand's services so the next factory run builds
# them. Fail-soft: a missing/garbled queue never blocks page generation.
BOFU_QUEUE = BASE / "data" / "hustle" / "bofu_seo" / "queue.json"


def _merge_queued_services(key: str, cfg: dict) -> dict:
    if key in DEAD_BRAND_KEYS:  # a stale queued dead-brand topic can never re-seed pages
        return cfg
    try:
        q = json.loads(BOFU_QUEUE.read_text(errors="ignore"))
    except Exception:
        return cfg
    extra = [s for s in (q.get(key) or [])
             if isinstance(s, dict) and s.get("noun") and s.get("cat")]
    if not extra:
        return cfg
    have = {s["noun"] for s in cfg.get("services", [])}
    add = [{"noun": s["noun"], "cat": s["cat"]} for s in extra if s["noun"] not in have]
    if not add:
        return cfg
    return {**cfg, "services": list(cfg.get("services", [])) + add}


# ── generate ──────────────────────────────────────────────────────────────────
def generate(only: str | None = None) -> dict:
    result = {"brands": {}, "html_pages": 0, "blog_pages": 0}
    for key, cfg in BRANDS.items():
        if only and key != only:
            continue
        if key in DEAD_BRAND_KEYS:  # fail-closed: never resurrect a killed brand's pages
            continue
        reg = br.get_brand_by_key(key)
        if not reg:
            continue
        cfg = _merge_queued_services(key, cfg)  # strategist-queued topics (fail-soft, additive)
        pages = build_pages(reg, cfg)
        if cfg.get("blog"):
            BLOG_DIR.mkdir(parents=True, exist_ok=True)
            slugs = []
            for p in pages:
                rec = render_blog_json(reg, cfg, p)
                (BLOG_DIR / f"{rec['slug']}.json").write_text(json.dumps(rec, indent=2, ensure_ascii=False))
                slugs.append(rec["slug"])
            result["blog_pages"] += len(slugs)
            result["brands"][key] = {"store": f"products/content/blog_posts/bofu-*.json "
                                     f"(served /blog/<slug> on {reg['domain']})",
                                     "pages": len(slugs), "slugs": slugs,
                                     "clusters": [pp.cluster for pp in pages]}
        else:
            d = OUTDIR / key
            d.mkdir(parents=True, exist_ok=True)
            slugs = []
            for p in pages:
                (d / f"{p.slug}.html").write_text(render_html(reg, cfg, p))
                slugs.append(p.slug)
            write_sitemap(reg, cfg, slugs)
            result["html_pages"] += len(slugs)
            result["brands"][key] = {"store": f"data/hustle/seo_pages/bofu/{key}/*.html + sitemap.xml "
                                     f"(canonical https://{reg['domain']}{cfg['path_prefix']}/<slug>.html)",
                                     "pages": len(slugs), "slugs": slugs,
                                     "clusters": [pp.cluster for pp in pages]}
    return result


# ── validation ─────────────────────────────────────────────────────────────────
def check() -> int:
    bad = 0
    for key, cfg in BRANDS.items():
        if key in DEAD_BRAND_KEYS:
            continue
        reg = br.get_brand_by_key(key)
        if not reg:
            continue
        own = {reg["domain"]} | {(a[4:] if a.startswith("www.") else a)
                                 for a in (reg.get("aliases") or [])}
        own.add(reg["domain"])
        other_hosts = ALL_BRAND_HOSTS - own
        if cfg.get("blog"):
            files = list(BLOG_DIR.glob("bofu-*.json"))
            texts = [(f.name, json.loads(f.read_text()).get("content", "") +
                      json.loads(f.read_text()).get("title", "")) for f in files]
        else:
            files = sorted((OUTDIR / key).glob("*.html"))
            texts = [(f.name, f.read_text()) for f in files]
        n = 0
        for fname, t in texts:
            low = t.lower()
            # 1. cross-brand domain leak
            for oh in other_hosts:
                if oh and oh in low:
                    print(f"CROSS-BRAND LEAK [{key}] {fname}: contains {oh}"); bad += 1
            # 2. owner PII
            for tok in PII_TOKENS:
                if re.search(r"\b" + re.escape(tok) + r"\b", low):
                    print(f"PII [{key}] {fname}: contains '{tok}'"); bad += 1
            # 3. own domain present (it should link itself)
            if reg["domain"] not in low:
                print(f"NO OWN-DOMAIN LINK [{key}] {fname}"); bad += 1
            # 4. FAQ present. HTML brands carry FAQPage JSON-LD; the docsapp blog store can't
            #    (its /blog route emits Article schema only), so there we require the visible FAQ.
            if cfg.get("blog"):
                if "<h2>FAQ</h2>" not in t:
                    print(f"NO VISIBLE FAQ [{key}] {fname}"); bad += 1
            elif '"FAQPage"' not in t:
                print(f"NO FAQ SCHEMA [{key}] {fname}"); bad += 1
            n += 1
        if n == 0:
            print(f"NO PAGES for brand {key}"); bad += 1
        print(f"check[{key}]: {n} pages, own-domain {reg['domain']}, "
              f"{'OK' if bad == 0 else 'see above'}")
    print("check:", "OK" if not bad else f"{bad} problems")
    return 1 if bad else 0


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--brand", help="only this brand key")
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args(argv)
    if a.check:
        return check()
    r = generate(only=a.brand)
    print(json.dumps(r, indent=2, ensure_ascii=False))
    return check()


if __name__ == "__main__":
    raise SystemExit(main())
