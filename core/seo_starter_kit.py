#!/usr/bin/env python3
"""Local SEO Starter Kit — the real, business-specific $49 deliverable for the smallest local SMBs
(cafés, restaurants, salons, barbers, bakeries) who search "get my business on Google", not
"AI visibility". This is a SECOND DOORWAY on the existing scannerapp machine — same proof-first
funnel + same on-demand fulfillment as the GEO Fix Kit, just SEO-framed.

For one business it produces, on disk under products_internal/seo_kits/<slug>/:
  - gbp_checklist.md     Google Business Profile optimization checklist (category, services,
                         photos, reviews, Posts cadence, NAP) — built for THIS business
  - onpage_basics.md     optimized <title> + meta description + H1 + 5 local keyword targets +
                         a "near me" content block written for THIS business
  - citations.md         the canonical NAP block + a citation target list (Google/Bing/Apple/Yelp…)
  - schema.jsonld        correct schema.org LocalBusiness JSON-LD (reuses core.geo_fix_kit)
  - llms.txt             BONUS — the same llms.txt the GEO kit ships ("you're ready for AI search too")
  - preview.html         watermarked proof-first preview surface (free-scan upsell)
  - manifest.json        what's in the kit + grade context

Two modes off ONE generator (mirrors geo_fix_kit):
  watermark=True  -> the free proof preview: GBP checklist shown in full (the hook), the copy-paste
                     on-page + citation deliverables locked behind the $49 unlock.
  watermark=False -> the paid full kit: clean, un-watermarked, ready to deploy.

Deterministic, $0, no LLM (the buyer's own facts + templates) so it never fails at fulfillment.
Scrapes the real homepage when a domain is present (reuses geo_fix_kit._scrape_business).

  python3 -m core.seo_starter_kit "Galaxy Cafe" "Austin, TX" cafe --domain galaxycafeaustin.com
  python3 -m core.seo_starter_kit "Galaxy Cafe" "Austin, TX" cafe --full   # un-watermarked
"""
from __future__ import annotations

import argparse
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from core.geo_fix_kit import (
    WATERMARK,
    _default_services,
    _scrape_business,
    _slug,
    _split_city,
    build_llms_txt,
    build_schema_jsonld,
)

ROOT = Path(os.environ.get("CLAUDE_STACK_DIR", Path(__file__).resolve().parents[1]))
KITS_DIR = ROOT / "products_internal" / "seo_kits"

# Freemail domains that are NOT a business website (so an email like user@example.com never
# gets scraped as if it were the café's site).
_FREEMAIL = {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "aol.com", "icloud.com",
             "comcast.net", "att.net", "live.com", "msn.com", "me.com", "proton.me"}


def _services_for(category: str) -> list[str]:
    """Sensible default services for the common local SMB verticals this kit targets (the ones in
    data/hustle/local_biz_leads.jsonl), falling back to geo_fix_kit's trade defaults. Used only when
    the homepage scrape can't find ≥3 real services — keeps a florist/photographer/etc. from getting
    a generic "Emergency service" (or, for car repair, a wrong HVAC) default in the paid kit."""
    c = (category or "").lower()
    if any(k in c for k in ("restaurant", "cafe", "café", "coffee", "diner", "bistro", "eatery", "grill", "food")):
        return ["Breakfast & brunch", "Coffee & espresso", "Lunch", "Fresh-made meals"]
    if "bakery" in c or "bake" in c or "patisserie" in c:
        return ["Fresh breads", "Pastries & desserts", "Custom cakes", "Coffee"]
    if "butcher" in c or "meat" in c:
        return ["Fresh-cut meats", "Custom cuts", "Sausages & deli", "Special orders"]
    if any(k in c for k in ("bar", "pub", "brewery", "taproom")):
        return ["Craft cocktails", "Local beer", "Happy hour", "Bar food"]
    if "barber" in c:
        return ["Haircuts", "Beard trims", "Hot-towel shave", "Kids' cuts"]
    if any(k in c for k in ("med spa", "medspa", "medical spa", "aesthetic", "botox")):
        return ["Injectables & Botox", "Facials & skincare", "Laser treatments", "Body contouring"]
    if any(k in c for k in ("salon", "hair", "spa", "nail", "beauty", "lash", "brow")):
        return ["Haircuts", "Color", "Styling", "Treatments"]
    if "florist" in c or "flower" in c:
        return ["Fresh flower bouquets", "Wedding & event flowers", "Sympathy & funeral flowers", "Same-day delivery"]
    if "photograph" in c:
        return ["Portrait sessions", "Wedding photography", "Event coverage", "Family & headshots"]
    if any(k in c for k in ("fitness", "gym", "yoga", "pilates", "crossfit", "martial", "dojo")):
        return ["Personal training", "Group fitness classes", "Memberships", "Nutrition coaching"]
    if "veterinar" in c or "animal" in c:
        return ["Wellness exams", "Vaccinations", "Dental care", "Spay & neuter"]
    if any(k in c for k in ("car_repair", "car repair", "auto", "mechanic", "tire", "body shop", "transmission")):
        return ["Oil changes", "Brake service", "Engine diagnostics", "Tire rotation & repair"]
    if "bicycle" in c or "bike" in c:
        return ["Bike sales", "Tune-ups & repairs", "Parts & accessories", "Bike fittings"]
    if "jewel" in c:
        return ["Custom jewelry design", "Ring & watch repair", "Engagement & wedding rings", "Appraisals"]
    if any(k in c for k in ("optic", "eyewear", "optometr")):
        return ["Eye exams", "Prescription glasses", "Contact lens fittings", "Designer frames"]
    if any(k in c for k in ("clothes", "cloth", "apparel", "boutique", "fashion")):
        return ["In-store shopping", "Seasonal collections", "Personal styling", "Alterations"]
    if "furniture" in c:
        return ["Living room furniture", "Bedroom furniture", "Custom orders", "Delivery & assembly"]
    if any(k in c for k in ("estate", "realtor", "realty")):
        return ["Buyer representation", "Home selling & listings", "Market valuations", "Rental & property search"]
    if "insurance" in c:
        return ["Auto insurance", "Home insurance", "Life insurance", "Business coverage"]
    if any(k in c for k in ("account", "bookkeep", "cpa", "tax")):
        return ["Tax preparation", "Bookkeeping", "Payroll", "Business advisory"]
    if "clean" in c:
        return ["Recurring house cleaning", "Deep cleaning", "Move-in / move-out cleaning", "Office cleaning"]
    if "hardware" in c:
        return ["Tools & supplies", "Paint & sundries", "Plumbing & electrical parts", "Key cutting"]
    if "carpenter" in c or "carpentry" in c:
        return ["Custom carpentry", "Cabinets & built-ins", "Trim & finish work", "Repairs"]
    if "paint" in c:
        return ["Interior painting", "Exterior painting", "Cabinet refinishing", "Drywall repair"]
    if "garden" in c:
        return ["Garden design & planting", "Lawn care", "Pruning & maintenance", "Seasonal cleanup"]
    if any(k in c for k in ("clinic", "medical", "doctor", "health")):
        return ["Patient consultations", "Routine checkups", "Preventive care", "Same-day appointments"]
    return _default_services(category)


def kit_dir_for(business: str, city: str, *, base: Path = KITS_DIR) -> Path:
    return base / _slug(f"{business}-{city}")


# ----------------------------------------------------------------- deliverable builders

def build_gbp_checklist(business: str, city: str, category: str, *, services: list[str] | None = None,
                        phone: str = "", domain: str = "", watermark: bool = False) -> str:
    services = services or _services_for(category)
    city_name, region = _split_city(city)
    loc = f"{city_name}, {region}".strip().strip(",") or city
    site = (f"https://{domain}" if domain and not str(domain).startswith("http") else (domain or "")).strip()
    svc = "\n".join(f"  - [ ] {s}" for s in services[:8])
    wm = (f"<!-- {WATERMARK}{business} -->\n\n" if watermark else "")
    return wm + (
        f"# Google Business Profile Optimization Checklist — {business} ({loc})\n\n"
        "Your Google Business Profile (the panel that shows on the right of Google search and on "
        "Google Maps) is the single biggest lever for getting found by people nearby. Work this "
        "top to bottom — most owners are missing half of it.\n\n"
        "## 1. Claim & verify\n"
        f"- [ ] Claim the profile at business.google.com (search \"{business} {city_name}\" first to "
        "avoid creating a duplicate)\n"
        "- [ ] Complete verification (postcard, phone, or video) — you can't rank unmverified\n\n"
        "## 2. Categories (this decides which searches you show up for)\n"
        f"- [ ] Set your PRIMARY category to the closest match for a {category} "
        "(this matters more than any other single field)\n"
        "- [ ] Add 2-4 secondary categories for everything else you do\n\n"
        "## 3. NAP — Name, Address, Phone (must match your website + every listing EXACTLY)\n"
        f"- [ ] Name: {business}\n"
        f"- [ ] Address: your real street address in {city_name} (set a service area if you don't have a storefront)\n"
        f"- [ ] Phone: {phone or 'your main business line (a local number, not a call-tracking 800#)'}\n"
        f"- [ ] Website: {site or 'your homepage URL'}\n"
        "- [ ] Hours — including holiday hours (wrong hours is the #1 review complaint)\n\n"
        "## 4. Services / products (Google indexes these for \"near me\" searches)\n"
        f"- [ ] Add each of these as a Service with a 1-2 sentence description:\n{svc}\n"
        "- [ ] Restaurants/cafés: add your menu (or link it) + popular items as Products\n\n"
        "## 5. Photos (profiles with photos get ~2x the clicks)\n"
        "- [ ] Logo + a strong cover photo\n"
        "- [ ] 10+ photos: storefront/exterior, interior, your team, and your product/food\n"
        "- [ ] Add a few NEW photos every month — Google rewards freshness\n\n"
        "## 6. Reviews (the trust + ranking signal)\n"
        "- [ ] Ask every happy customer for a Google review — text/email them your review link\n"
        "- [ ] Reply to EVERY review, good or bad, within a day or two\n"
        "- [ ] Aim for a steady trickle (a few a month) over a one-time burst — Google flags spikes\n\n"
        "## 7. Google Posts (free mini-ads in your profile)\n"
        "- [ ] Post weekly: an offer, an update, an event, or a new product\n"
        "- [ ] Always include one photo + a clear call-to-action button\n\n"
        "## 8. Q&A + messaging\n"
        "- [ ] Seed your own profile with 3-5 real customer questions and answer them\n"
        "- [ ] Turn on messaging so customers can text you from the profile\n\n"
        "## 9. Keep NAP consistent everywhere\n"
        "- [ ] Make sure the exact name/address/phone above appears identically on your website and "
        "every directory (see citations.md) — mismatches quietly sink local ranking.\n"
        + (f"\n---\n*{WATERMARK}The paid kit also hands you copy-paste on-page tags, your canonical NAP "
           "block, and a ready citation list.*\n" if watermark else "")
    )


def _title_tag(business: str, loc: str, category: str, services: list[str]) -> str:
    extra = ", ".join(services[:2]) if services else category.title()
    base = f"{business} | {category.title()} in {loc}"
    if len(base) < 50 and extra:
        base = f"{business} — {category.title()} in {loc} | {extra}"
    if len(base) <= 60:
        return base
    # trim to <=60 on a word boundary (no mid-word cut), then drop a dangling separator
    cut = base[:60].rsplit(" ", 1)[0].rstrip(" ,|—-")
    return cut


def _meta_desc(business: str, loc: str, category: str, services: list[str]) -> str:
    svc = ", ".join(services[:3]) if services else f"{category} services"
    d = (f"{business} is a local {category} in {loc}. {svc.capitalize()}. "
         f"Call or visit — see hours, reviews, and directions.")
    return d[:155]


def _local_keywords(business: str, city_name: str, category: str, services: list[str]) -> list[str]:
    c = category.lower()
    base = [
        f"{c} in {city_name.lower()}",
        f"best {c} {city_name.lower()}",
        f"{c} near me",
    ]
    if services:
        base.append(f"{services[0].lower()} {city_name.lower()}")
    base.append(f"{business.lower()} {city_name.lower()}")
    # de-dupe preserving order, cap at 5
    seen, out = set(), []
    for k in base:
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out[:5]


def build_onpage_basics(business: str, city: str, category: str, *, services: list[str] | None = None,
                        domain: str = "", watermark: bool = False) -> str:
    services = services or _services_for(category)
    city_name, region = _split_city(city)
    loc = f"{city_name}, {region}".strip().strip(",") or city
    title = _title_tag(business, loc, category, services)
    meta = _meta_desc(business, loc, category, services)
    h1 = f"{business} — {category.title()} in {city_name}"
    kws = _local_keywords(business, city_name, category, services)
    kw_md = "\n".join(f"{i+1}. {k}" for i, k in enumerate(kws))
    svc_line = ", ".join(services[:3]) if services else f"{category} services"
    if watermark:
        return (
            f"# [LOCKED] On-Page SEO Basics for {business}\n\n{WATERMARK}\n\n"
            f"The $49 Local SEO Starter Kit unlocks your ready-to-paste, business-specific:\n"
            f"- optimized <title> tag\n- meta description (length-checked for Google)\n"
            f"- H1 heading\n- 5 local keyword targets for {city_name}\n"
            f"- a written \"near me\" content block for {business}\n"
        )
    near_me = (
        f"Looking for a {category} near you in {city_name}? {business} serves {city_name} and the "
        f"surrounding {region or 'area'} with {svc_line}. We're a local, independent {category} — when "
        f"you search \"{category} near me\" in {city_name}, we want to be the easy yes: convenient hours, "
        f"a real local team, and the {svc_line} {city_name} neighbors come back for. Stop in, call ahead, "
        f"or check our Google profile for hours, directions, and reviews before you visit."
    )
    return (
        f"# On-Page SEO Basics — {business} ({loc})\n\n"
        "Paste these into your homepage. They tell Google (and visitors) exactly what you do and "
        "where, which is what gets you into local \"near me\" results.\n\n"
        "## 1. Page <title> tag (goes in your homepage <head>)\n"
        f"```html\n<title>{title}</title>\n```\n"
        f"*{len(title)} chars — Google shows ~60. Front-loads your category + city.*\n\n"
        "## 2. Meta description (your search-result snippet)\n"
        f"```html\n<meta name=\"description\" content=\"{meta}\">\n```\n"
        f"*{len(meta)} chars — Google shows ~155. Written to earn the click.*\n\n"
        "## 3. H1 heading (the one big headline on the page)\n"
        f"```html\n<h1>{h1}</h1>\n```\n"
        "*One H1 per page. Includes your category + city — most small-business sites get this wrong.*\n\n"
        "## 4. Five local keyword targets\n"
        "Use these naturally in your headings, page copy, image alt text, and Google Posts:\n\n"
        f"{kw_md}\n\n"
        "## 5. \"Near me\" content block (paste into your homepage or an About section)\n"
        f"> {near_me}\n\n"
        "*This block deliberately uses your category, city, and \"near me\" — the exact phrasing local "
        "searchers type — without keyword-stuffing. Edit the details to match your real hours/offer.*\n"
    )


def build_citations(business: str, city: str, category: str, *, phone: str = "", domain: str = "",
                    watermark: bool = False) -> str:
    city_name, region = _split_city(city)
    loc = f"{city_name}, {region}".strip().strip(",") or city
    site = (f"https://{domain}" if domain and not str(domain).startswith("http") else (domain or "")).strip()
    if watermark:
        return (
            f"# [LOCKED] NAP + Citation List for {business}\n\n{WATERMARK}\n\n"
            "The $49 kit unlocks your canonical NAP block (the exact name/address/phone to use "
            "everywhere) plus the prioritized directory list to submit it to.\n"
        )
    c = category.lower()
    base = [
        ("Google Business Profile", "business.google.com", "The #1 priority — covers Google Search + Maps."),
        ("Bing Places", "bingplaces.com", "Powers Bing + can import straight from Google."),
        ("Apple Business Connect", "businessconnect.apple.com", "How you show up in Apple Maps / Siri."),
        ("Yelp", "biz.yelp.com", "High-trust, ranks well, feeds Apple Maps too."),
        ("Facebook Page", "facebook.com", "A local Page is itself a citation + a review surface."),
        ("Instagram", "instagram.com", "Set the business profile with your city + address."),
        ("Nextdoor Business", "business.nextdoor.com", "Neighborhood-level local discovery."),
        ("Yellow Pages", "yellowpages.com", "Old-school but still a counted citation."),
        ("Foursquare", "foursquare.com", "Feeds data to many maps + apps."),
    ]
    if any(k in c for k in ("restaurant", "cafe", "café", "coffee", "bakery", "bar", "diner", "food")):
        base += [
            ("TripAdvisor", "tripadvisor.com", "Major for food/hospitality discovery."),
            ("Google Maps menu / Order", "—", "Add your menu so it shows in the profile."),
        ]
    if any(k in c for k in ("salon", "barber", "hair", "spa", "nail")):
        base += [("Booksy / Vagaro", "—", "Booking platforms double as local citations.")]
    rows = "\n".join(f"| {i+1} | {n} | {u} | {why} |" for i, (n, u, why) in enumerate(base))
    return (
        f"# NAP + Citation List — {business} ({loc})\n\n"
        "## Your canonical NAP (use this EXACT text everywhere)\n"
        "The single most common local-SEO mistake is the name/address/phone being slightly different "
        "across listings. Pick one format and copy-paste it identically every time:\n\n"
        "```\n"
        f"{business}\n"
        f"[your street address], {loc}\n"
        f"{phone or '[your local phone number]'}\n"
        + (f"{site}\n" if site else "")
        + "```\n\n"
        "## Where to submit it (in priority order)\n\n"
        "| # | Directory | Where | Why it matters |\n|---|---|---|---|\n"
        f"{rows}\n\n"
        "**How to use this:** create/claim each listing, paste the canonical NAP above verbatim, add "
        "your category + a couple of photos, and link back to your website. Consistency across these is "
        "what tells Google your business is real and local — which is what lifts you in the map results.\n"
    )


# ----------------------------------------------------------------- free-scan local-SEO audit

def local_seo_audit(domain: str, *, business: str = "", city: str = "", timeout: float = 9.0) -> dict:
    """Honest, public-signals-only local-SEO read of a homepage. No fabricated claims: we report
    only what the page itself shows (or fails to show) plus reachability from web_failure_probe.
    Returns {grade, score, checks[], missing[], present[], reachable, html_len}."""
    out: dict = {"domain": domain, "checks": [], "missing": [], "present": [], "reachable": None}
    html = ""
    # reachability / broken signal (reuse the deterministic probe)
    try:
        from core.web_failure_probe import probe as _probe
        p = _probe(domain)
        out["reachable"] = not (p.get("severity") == "critical" and not p.get("ssl_days_left"))
        if p.get("broken") and p.get("reasons") and p["reasons"] != ["healthy"]:
            out["site_problems"] = [r for r in p["reasons"] if r and r != "healthy"]
    except Exception:
        pass
    # fetch the homepage ourselves for the on-page checks
    if domain:
        import urllib.request
        url = domain if str(domain).startswith("http") else "https://" + str(domain).strip().lstrip("/")
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel "
                "Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"})
            html = urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "ignore")
        except Exception:
            html = ""
    out["html_len"] = len(html)
    low = html.lower()

    def add(label: str, ok: bool, fix: str):
        out["checks"].append({"label": label, "ok": ok, "fix": fix})
        (out["present"] if ok else out["missing"]).append(label if ok else fix)

    has_schema = bool(re.search(r'application/ld\+json', low)) and (
        "localbusiness" in low or '"@type"' in low and re.search(r'"@type"\s*:\s*"(?:[a-z]*business|restaurant|'
        r'cafe|store|foodestablishment|hairsalon|bakery)', low))
    title_m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    title = re.sub(r"\s+", " ", title_m.group(1)).strip() if title_m else ""
    has_meta = bool(re.search(r'<meta[^>]+name=["\']description["\'][^>]+content=["\'][^"\']{20,}', low))
    has_h1 = bool(re.search(r"<h1[\s>]", low))
    has_viewport = "viewport" in low
    city_name = (_split_city(city)[0] if city else "").lower().strip()
    mentions_city = bool(city_name and city_name in low)
    has_phone = bool(re.search(r'(\(?\d{3}\)?[\s.\-]\d{3}[\s.\-]\d{4})', html))

    if html:
        add("LocalBusiness schema markup", has_schema,
            "No LocalBusiness schema found — Google can't read your business details as structured data.")
        add("Title tag with your category + city", bool(title) and (mentions_city or len(title) > 10),
            "Your homepage <title> is missing or generic — it should name what you do and your city.")
        add("Meta description", has_meta,
            "No meta description — Google writes its own snippet for your search result instead of you.")
        add("One clear H1 heading", has_h1,
            "No H1 heading — Google has no single signal of what this page is primarily about.")
        add("Mobile viewport tag", has_viewport,
            "No mobile viewport tag — the page may render badly on phones (most local searches are mobile).")
        if city:
            add(f"Your city ({_split_city(city)[0]}) mentioned on the page", mentions_city,
                "Your city isn't on the homepage — local searchers and Google both look for it.")
        add("Phone number visible on the page", has_phone,
            "No phone number found on the homepage — add a click-to-call number near the top.")
    else:
        # couldn't fetch — don't fabricate; report honestly and still pitch the kit
        out["missing"] = [
            "We couldn't fully read your homepage, so we can't confirm your on-page basics.",
            "A LocalBusiness schema + optimized title/meta/H1 are what get you into local results.",
        ]

    n = len(out["checks"])
    passed = sum(1 for c in out["checks"] if c["ok"])
    score = round(100 * passed / n) if n else 30
    out["score"] = score
    out["grade"] = ("A" if score >= 90 else "B" if score >= 75 else "C" if score >= 55
                    else "D" if score >= 35 else "F")
    return out


# ----------------------------------------------------------------- preview surface

_PREVIEW_CSS = (
    "body{font-family:-apple-system,system-ui,Segoe UI,Arial,sans-serif;max-width:720px;margin:0 auto;"
    "padding:26px 18px 60px;line-height:1.55;color:#e9e9f2;background:#0a0a18}"
    ".grade{display:inline-flex;align-items:center;justify-content:center;width:72px;height:72px;"
    "border-radius:16px;font-size:38px;font-weight:800;color:#0a0a18;margin-right:12px;vertical-align:middle}"
    ".a,.b{background:#34d399}.c{background:#fbbf24}.d{background:#fb923c}.f{background:#f87171}"
    "h1{font-size:23px}h2{font-size:18px;margin:26px 0 8px}"
    ".card{background:#14142a;border:1px solid #26264a;border-radius:14px;padding:16px 18px;margin:14px 0}"
    "pre{white-space:pre-wrap;background:#0d0d1f;border:1px solid #26264a;border-radius:10px;padding:14px;"
    "font-size:12.5px;overflow:auto;position:relative;color:#c9d1d9}"
    ".wm{display:block;color:#fbbf24;font-weight:700;font-size:11px;letter-spacing:.05em;margin-bottom:8px}"
    ".cta{display:block;text-align:center;background:linear-gradient(135deg,#ffd479,#ff9f43);color:#0a0a18;"
    "font-weight:800;text-decoration:none;padding:15px;border-radius:12px;margin:16px 0;font-size:17px}"
    "ul{padding-left:20px}li{margin:5px 0}.muted{color:#8a90b0;font-size:12.5px}.ok{color:#34d399}.no{color:#fb923c}"
)


def _esc(s: str) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def build_seo_preview_html(audit: dict, business: str, city: str, category: str, *,
                           checkout_url: str = "#", domain: str = "", services: list[str] | None = None) -> str:
    grade = audit.get("grade", "?")
    gl = str(grade).lower()
    score = audit.get("score", 0)
    checks = audit.get("checks", [])
    rows = "".join(
        f"<li><span class='{ 'ok' if c['ok'] else 'no' }'>{ '✓' if c['ok'] else '✕' }</span> "
        f"{_esc(c['label'] if c['ok'] else c['fix'])}</li>" for c in checks
    ) or "".join(f"<li class=no>{_esc(m)}</li>" for m in audit.get("missing", []))
    gbp = build_gbp_checklist(business, city, category, services=services, domain=domain)
    gbp_excerpt = "\n".join(gbp.splitlines()[:22])
    onpage = build_onpage_basics(business, city, category, services=services, domain=domain)
    # show a real snippet of the actual deliverable (the title/meta block), watermarked
    onpage_excerpt = "\n".join(onpage.splitlines()[:14])
    return (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        f"<title>{_esc(business)} — Local SEO Grade {grade}</title>"
        "<meta name=viewport content=\"width=device-width,initial-scale=1\">"
        f"<style>{_PREVIEW_CSS}</style></head><body>"
        f"<p class=muted>LOCAL SEO SCAN · \"{_esc(category)} in {_esc(city)}\"</p>"
        f"<div><span class=\"grade {gl}\">{grade}</span><b style='font-size:19px'>{_esc(business)}</b> "
        f"scored <b>{score}/100</b> on the local-SEO basics Google uses to rank you nearby.</div>"
        f"<div class=card><h2 style='margin-top:0'>What's helping / what's missing</h2><ul>{rows}</ul></div>"
        "<div class=card><h2 style='margin-top:0'>Your kit is already built (preview)</h2>"
        "<p class=muted>Here's the start of your business-specific Google Business Profile checklist — "
        "the full kit also unlocks copy-paste on-page tags, your NAP block, schema, and citation list:</p>"
        f"<pre><span class=wm>PREVIEW · $49 UNLOCKS THE FULL EDITABLE KIT</span>{_esc(gbp_excerpt)}\n…</pre>"
        "<p class=muted>And the on-page tags, written for your business:</p>"
        f"<pre><span class=wm>PREVIEW · $49 UNLOCKS</span>{_esc(onpage_excerpt)}\n…</pre></div>"
        f"<a class=cta href=\"{checkout_url}\">Get Found on Google — unlock the full kit, $49</a>"
        f"<p class=muted>One-time. Instant delivery. Built specifically for {_esc(business)} in "
        f"{_esc(city)}: a GBP optimization checklist, copy-paste on-page tags, your NAP + citation list, "
        "LocalBusiness schema, and a bonus llms.txt so you're ready for AI search too.</p>"
        "</body></html>"
    )


# ----------------------------------------------------------------- top-level generate

def build_seo_kit(business: str, city: str, category: str, domain: str = "", *, phone: str = "",
                  audit: dict | None = None, watermark: bool = True, checkout_url: str = "#",
                  base: Path = KITS_DIR) -> dict:
    """Write the full Local SEO Starter Kit to disk and return a manifest.
    watermark=True => free proof preview (deliverables locked); False => paid full kit."""
    out = kit_dir_for(business, city, base=base)
    out.mkdir(parents=True, exist_ok=True)

    # personalize from the real site when we have one (skip freemail-derived "domains")
    dom = (domain or "").strip()
    if dom and "@" in dom:
        dom = dom.split("@")[-1]
    if dom and dom.lower() in _FREEMAIL:
        dom = ""
    scraped = _scrape_business(dom) if dom else {}
    services = scraped.get("services") or _services_for(category)
    phone = phone or scraped.get("phone", "")
    description = scraped.get("description", "")

    gbp = build_gbp_checklist(business, city, category, services=services, phone=phone,
                              domain=dom, watermark=watermark)
    onpage = build_onpage_basics(business, city, category, services=services, domain=dom, watermark=watermark)
    citations = build_citations(business, city, category, phone=phone, domain=dom, watermark=watermark)
    schema = build_schema_jsonld(business, city, category, domain=dom, phone=phone, services=services,
                                 description=description, watermark=watermark)
    llms = build_llms_txt(business, city, category, domain=dom, phone=phone, services=services,
                          description=description, watermark=watermark)

    (out / "gbp_checklist.md").write_text(gbp, encoding="utf-8")
    (out / "onpage_basics.md").write_text(onpage, encoding="utf-8")
    (out / "citations.md").write_text(citations, encoding="utf-8")
    (out / "schema.jsonld").write_text(schema, encoding="utf-8")
    (out / "llms.txt").write_text(llms, encoding="utf-8")
    files = ["gbp_checklist.md", "onpage_basics.md", "citations.md", "schema.jsonld", "llms.txt"]

    if audit is not None:
        preview = build_seo_preview_html(audit, business, city, category, checkout_url=checkout_url,
                                         domain=dom, services=services)
        (out / "preview.html").write_text(preview, encoding="utf-8")
        files.append("preview.html")

    manifest = {
        "business": business, "city": city, "category": category, "domain": dom,
        "watermark": watermark,
        "grade": (audit or {}).get("grade"),
        "score": (audit or {}).get("score"),
        "files": files,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "kit_type": "preview" if watermark else "full_paid",
        "product": "seo_starter",
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    manifest["path"] = str(out)
    return manifest


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("business")
    ap.add_argument("city")
    ap.add_argument("category")
    ap.add_argument("--domain", default="")
    ap.add_argument("--phone", default="")
    ap.add_argument("--full", action="store_true", help="un-watermarked paid kit (default = preview)")
    ap.add_argument("--checkout", default="#")
    args = ap.parse_args(argv)

    audit = None
    if args.domain:
        try:
            audit = local_seo_audit(args.domain, business=args.business, city=args.city)
        except Exception as e:
            print(f"(audit unavailable, building kit without grade context: {e})")

    m = build_seo_kit(args.business, args.city, args.category, args.domain, phone=args.phone,
                      audit=audit, watermark=not args.full, checkout_url=args.checkout)
    print(f"{'FULL' if args.full else 'PREVIEW'} kit -> {m['path']}")
    if audit:
        print(f"  grade {audit['grade']} ({audit['score']}/100), {len(audit.get('missing', []))} gaps")
    for f in m["files"]:
        print(f"  - {f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
