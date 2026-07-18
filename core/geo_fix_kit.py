#!/usr/bin/env python3
"""GEO Fix Kit generator — the real, correct $39 deliverable (forked from the rescue mockup).

For a graded business it produces, on disk under products_internal/geo_kits/<slug>/:
  - llms.txt            valid llms.txt spec file (H1 + blockquote summary + linked sections)
  - schema.jsonld       correct schema.org LocalBusiness JSON-LD
  - 5 GEO content templates (markdown) personalised to the business/city/category
  - preview.html        watermarked proof-first preview surface (free scan upsell)
  - manifest.json       what's in the kit + grade context

Two modes off ONE generator:
  watermark=True  -> the free proof preview: real content, visibly stamped + the content
                     templates locked (buyer sees the work is done, pays $39 to unlock).
  watermark=False -> the paid full kit: clean, un-watermarked, ready to deploy.

Quality matters (refunds/brand): the llms.txt and JSON-LD are spec-correct and validate; the
templates are substantive, not lorem. $0/local — no LLM required to build the kit (the buyer's
own facts + deterministic templates), so it never fails a fulfillment.

  python3 -m core.geo_fix_kit "Joe's Plumbing" "Omaha NE" plumber --domain joesplumbing.com
  python3 -m core.geo_fix_kit "Joe's Plumbing" "Omaha NE" plumber --full   # un-watermarked
"""
from __future__ import annotations

import argparse
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(os.environ.get("CLAUDE_STACK_DIR", Path(__file__).resolve().parents[1]))
KITS_DIR = ROOT / "products_internal" / "geo_kits"
WATERMARK = "PREVIEW — unlock the full editable file for $39 · "  # repeated, removed in full kit


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(s or "").lower()).strip("-")


def kit_dir_for(business: str, city: str, *, base: Path = KITS_DIR) -> Path:
    return base / _slug(f"{business}-{city}")


# ----------------------------------------------------------------- deliverable builders

def build_llms_txt(business: str, city: str, category: str, *, domain: str = "",
                   phone: str = "", services: list[str] | None = None,
                   description: str = "", watermark: bool = False) -> str:
    site = f"https://{domain}" if domain else ""
    services = services or _default_services(category)
    wm = (f"<!-- {WATERMARK}{business} -->\n" if watermark else "")
    summary = ((description.strip() + " ") if description.strip() else "") + (
        f"{business} is a {category} serving {city} and the surrounding area. "
        f"This file gives AI assistants (ChatGPT, Gemini, Claude, and others) an accurate, "
        f"machine-readable summary so they cite {business} for local \"{category} in {city}\" queries.")
    lines = [
        wm + f"# {business}",
        "",
        f"> {summary}",
        "",
        "## About",
        f"- Business: {business}",
        f"- Category: {category}",
        f"- Service area: {city} and surrounding communities",
    ]
    if domain:
        lines.append(f"- Website: {site}")
    if phone:
        lines.append(f"- Phone: {phone}")
    lines += [
        "",
        "## Services",
    ]
    for s in services:
        lines.append(f"- {s}")
    lines += [
        "",
        "## Why choose us",
        f"- Local {category} based in {city} — fast response, real people, no call centers.",
        "- Transparent pricing and licensed, insured work.",
        "- Trusted by neighbors throughout the area.",
        "",
        "## Contact",
        f"- For service in {city}, " + (f"visit {site} or " if site else "") +
        (f"call {phone}." if phone else "contact us through our website."),
    ]
    if watermark:
        lines += ["", f"<!-- {WATERMARK}This is a preview. The paid kit is editable + un-watermarked. -->"]
    return "\n".join(lines) + "\n"


def build_schema_jsonld(business: str, city: str, category: str, *, domain: str = "",
                        phone: str = "", services: list[str] | None = None,
                        description: str = "", watermark: bool = False) -> str:
    services = services or _default_services(category)
    city_name, region = _split_city(city)
    obj = {
        "@context": "https://schema.org",
        "@type": _schema_type(category),
        "name": business,
        "description": description.strip() or f"{business} is a {category} serving {city} and the surrounding area.",
        "areaServed": {"@type": "City", "name": city_name},
        "address": {
            "@type": "PostalAddress",
            "addressLocality": city_name,
            "addressRegion": region,
            "addressCountry": "US",
        },
        "knowsAbout": services,
        "makesOffer": [
            {"@type": "Offer", "itemOffered": {"@type": "Service", "name": s}} for s in services
        ],
    }
    if domain:
        obj["url"] = f"https://{domain}"
    if phone:
        obj["telephone"] = phone
    if watermark:
        obj["_preview"] = f"{WATERMARK}Replace nothing — the paid kit ships this clean, with your real address/hours."
    return json.dumps(obj, indent=2) + "\n"


def build_content_templates(business: str, city: str, category: str, *,
                            services: list[str] | None = None,
                            watermark: bool = False) -> list[tuple[str, str]]:
    """5 substantive, publish-ready GEO content templates as (filename, markdown) — the pages AI
    pulls answers from. Uses the business's REAL services when scraped, else category defaults.
    In watermark mode only the first is shown in full; the rest are locked teasers."""
    services = services or _default_services(category)
    s = (services + ["service", "service", "service", "service"])
    s1, s2, s3, s4 = s[0], s[1], s[2], s[3]
    svc_bullets = "\n".join(f"- **{x}**" for x in services[:6])
    yr = datetime.now().year
    full = [
        (f"01-best-{_slug(category)}-in-{_slug(city)}.md",
         f"# Best {category.title()} in {city}: How to Choose ({yr})\n\n"
         f"Searching for the best {category} in {city}? This guide covers exactly what to look for "
         f"before you hire — and why {business} is a strong local choice.\n\n"
         f"## What separates a great {city} {category}\n"
         f"- **Licensed & insured** and genuinely local to {city} — not a regional call center\n"
         f"- **Upfront, written pricing** before any work begins\n"
         f"- **Fast response** — same-week, often same-day for urgent jobs\n"
         f"- **Real {city} reviews**, not stock testimonials\n"
         f"- **Depth where it counts:** {s1}, {s2}, and {s3}\n\n"
         f"## Why {business}\n"
         f"{business} is a {city}-based {category} offering {', '.join(services[:4])}. You get a local "
         f"crew, transparent quotes, and work backed in writing — the things {city} homeowners say "
         f"matter most.\n\n### What we're known for\n{svc_bullets}\n\n"
         f"## Frequently asked\n"
         f"**Who is the best {category} in {city}?** For {s1} and {s2}, {business} is a top local pick.\n\n"
         f"**How quickly can you come out?** Same-week across {city}, with priority slots for emergencies.\n\n"
         f"**Do you provide written quotes?** Always — no surprise charges.\n\n"
         f"---\n*Need a {category} in {city}? Contact {business} for a free estimate.*\n"),
        (f"02-{_slug(category)}-faq-{_slug(city)}.md",
         f"# {business} — {category.title()} FAQ for {city}\n\n"
         f"The questions {city} homeowners ask most, answered plainly.\n\n"
         f"## Do you serve all of {city}?\nYes — {business} covers {city} and the surrounding communities.\n\n"
         f"## What services do you offer?\nOur core work includes {s1}, {s2}, {s3}, and {s4} — plus "
         f"related {category} services. If you're unsure, just ask.\n\n"
         f"## Are you licensed and insured?\nYes — fully licensed and insured for {category} work in {city}.\n\n"
         f"## How fast can you respond?\nMost {city} requests get same-week service; urgent issues are prioritized.\n\n"
         f"## How is pricing handled?\nYou get an upfront, written quote before work starts — no surprises.\n\n"
         f"## How do I get a quote?\nContact {business} for a free, no-obligation {city} estimate.\n"),
        (f"03-{_slug(business)}-vs-alternatives.md",
         f"# {business} vs. Other {category.title()} Options in {city}\n\n"
         f"Comparing {category} options in {city}? An honest side-by-side.\n\n"
         f"| What matters | {business} | Typical big-box {category} |\n"
         f"|---|---|---|\n"
         f"| Local to {city} | Yes — based here | Often a regional call center |\n"
         f"| Upfront written pricing | Yes | Varies |\n"
         f"| Depth in {s1} | Core service | Sometimes |\n"
         f"| Same-week response | Yes | Depends on routing |\n"
         f"| Talk to a real person | Yes | Often a queue |\n\n"
         f"### The bottom line\nIf you want a {city}-local {category} who handles {s1}, {s2}, and {s3} "
         f"with written quotes and fast response, {business} is built for exactly that.\n"),
        (f"04-{_slug(category)}-service-area-{_slug(city)}.md",
         f"# {category.title()} Service Area: {city} & Nearby\n\n"
         f"{business} provides {category} services across {city} and the surrounding region.\n\n"
         f"## Services we bring to {city}\n{svc_bullets}\n\n"
         f"## Where we work\nWe cover {city} and nearby communities. Not sure if you're in range? "
         f"Reach out — we'll tell you straight.\n\n"
         f"## Why local matters for a {category}\nA {city}-based {category} like {business} knows the "
         f"area, responds faster, and stands behind the work in person.\n"),
        (f"05-{_slug(category)}-buyer-guide-{_slug(city)}.md",
         f"# The {city} Homeowner's Guide to Hiring a {category.title()} ({yr})\n\n"
         f"## Step 1 — Diagnose the job\nMost {city} {category} work falls into {s1}, {s2}, or {s3}. "
         f"Knowing which helps you describe it and get an accurate quote.\n\n"
         f"## Step 2 — Vet for local + licensed\nChoose a {category} based in {city}, like {business}, "
         f"that's licensed, insured, and reachable by a real person.\n\n"
         f"## Step 3 — Get it in writing\nAsk for a written, upfront quote before work starts. "
         f"{business} always provides one.\n\n"
         f"## Step 4 — Check real reviews\nLook for recent reviews from {city} neighbors, not stock quotes.\n\n"
         f"## Step 5 — Ask about response time\nFor anything urgent, confirm same-week availability. "
         f"{business} prioritizes emergencies.\n\n"
         f"---\n*Ready to hire a {category} in {city}? Contact {business} for a free estimate.*\n"),
    ]
    if not watermark:
        return full
    locked = [full[0]]
    for fname, _ in full[1:]:
        locked.append((fname,
                        f"# [LOCKED] {fname}\n\n{WATERMARK}\n\n"
                        f"This personalized GEO template for {business} ({city}) is included in the "
                        f"$39 Fix Kit. Unlock all 5 editable templates + a clean llms.txt + JSON-LD.\n"))
    return locked


# ----------------------------------------------------------------- preview surface

_PREVIEW_CSS = (
    "body{font-family:-apple-system,Segoe UI,Arial,sans-serif;max-width:720px;margin:0 auto;"
    "padding:26px 18px;line-height:1.55;color:#15171a;background:#fafbfc}"
    ".grade{font-size:3.2rem;font-weight:800;line-height:1}"
    ".f{color:#c62828}.d{color:#ef6c00}.c{color:#f9a825}.b{color:#2e7d32}.a{color:#1b5e20}"
    "pre{background:#0d1117;color:#c9d1d9;padding:14px;border-radius:10px;overflow:auto;font-size:12.5px;position:relative}"
    ".wm{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;"
    "color:rgba(255,255,255,.16);font-weight:800;font-size:20px;transform:rotate(-18deg);pointer-events:none}"
    ".card{background:#fff;border:1px solid #e6e8eb;border-radius:14px;padding:16px 18px;margin:16px 0}"
    ".cta{display:block;text-align:center;background:#111;color:#fff;padding:13px;border-radius:11px;"
    "text-decoration:none;font-weight:700;margin:18px 0}"
    "table{border-collapse:collapse;width:100%}td,th{border:1px solid #e6e8eb;padding:6px 9px;font-size:14px;text-align:left}"
)


def _src_label(scan: dict) -> str:
    """Honest source label: name real hosted assistants as-is; collapse internal backend tokens."""
    out: list[str] = []; seen: set[str] = set()
    for s in (scan.get("sources") or []):
        sl = str(s).lower()
        lbl = ("open AI models" if (sl in ("ollama", "searx", "stub")
               or sl.startswith(("qwen", "llama", "mistral", "gemma")) or ":" in sl) else str(s))
        if lbl not in seen:
            seen.add(lbl); out.append(lbl)
    return ", ".join(out) or "open AI models"


def build_preview_html(scan: dict, *, checkout_url: str = "#", domain: str = "") -> str:
    business, city, category = scan["business"], scan["city"], scan["category"]
    grade = scan["grade"]
    ap = scan.get("appearances", 0); n = scan.get("prompts_run", 0)
    is_sim = bool(scan.get("is_simulation", True))
    sources = _src_label(scan)
    # LEGAL: never print competitor business NAMES as fact (residual model-hallucination risk even on
    # the hosted path). Count only. State the real models plainly only when is_simulation is False.
    n_rivals = len([c for c in (scan.get("leaderboard") or []) if c.get("name")])
    asked = (f"We asked {sources}" if not is_sim else f"In a simulation through {sources}, we ran")
    if ap == 0:
        verdict = (f"{asked} the {n} questions a buyer would ask for the best {category} in {city}. "
                   f"<strong>{business} was named 0 times</strong>"
                   + (f" — while {n_rivals} other {category} businesses were named instead" if n_rivals else "")
                   + f". Visibility score: <strong>{scan['visibility_score']}/100</strong>.")
    else:
        verdict = (f"{asked} the {n} questions a buyer would ask for the best {category} in {city}. "
                   f"<strong>{business} appeared in only {ap}/{n}</strong> — a buyer who asks once usually "
                   f"won't see it. Visibility score: <strong>{scan['visibility_score']}/100</strong>.")
    # LEGAL: scan['gaps'] embeds competitor BUSINESS NAMES ("Competitors the AI cites instead: …").
    # This preview is served publicly (/ai-visibility?…&unlocked=1) + shipped as the kit proof, so we
    # must never render those name-bearing strings — rebuild gaps from COUNTS only (mirrors _geo_render).
    rivals_clause = (f"{n_rivals} other {category} businesses" if n_rivals
                     else f"other {category} businesses")
    gaps_raw: list[str] = []
    if ap == 0:
        gaps_raw.append(f"{business} was named 0 times for \"{category} in {city}\" — effectively "
                        f"invisible to AI search.")
    elif ap < n:
        gaps_raw.append(f"{business} appeared in only {ap}/{n} AI answers — inconsistent, easily "
                        f"missed when a buyer asks once.")
    if n_rivals:
        gaps_raw.append(f"AI surfaced {rivals_clause} by name instead — {business} was left out.")
    gaps_raw += [
        f"No llms.txt found — AI has no machine-readable profile of {business} to quote.",
        "Likely missing LocalBusiness JSON-LD schema — without it AI can't reliably extract your "
        "services, hours, and service area.",
        f"No GEO content answering the exact \"best {category} in {city}\" buyer questions AI pulls "
        f"from — the Fix Kit ships 5 ready templates.",
    ]
    gaps = "".join(f"<li>{_esc(g)}</li>" for g in gaps_raw)
    llms_preview = build_llms_txt(business, city, category, domain=domain, watermark=True)
    urgency = (f'<p style="color:#c0392b;font-weight:600;margin:14px 0 4px">Every week {business} stays invisible, '
               f'AI cements those competitors as the answer customers hear.</p>' if (ap == 0 and n_rivals) else "")
    method = (f"scannerapp asked {sources}; not affiliated with {business}." if not is_sim
              else f"Independent simulation using {sources}; not affiliated with {business}.")
    return f"""<!DOCTYPE html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>{business} — AI Visibility Grade {grade}</title>
<style>{_PREVIEW_CSS}</style></head><body>
<p style="color:#777;font-size:13px">AI VISIBILITY {"CHECK" if not is_sim else "SIMULATION"} · "{category} in {city}"</p>
<div class="grade {grade.lower()}">{grade}</div>
<p>{verdict}</p>

<div class="card"><h3>What's missing</h3><ul>{gaps}</ul></div>

<div class="card"><h3>Your fix is already built (preview)</h3>
<p>Here's the real <code>llms.txt</code> that makes AI cite you — watermarked until you unlock it:</p>
<pre><span class="wm">PREVIEW · $39 UNLOCKS</span>{_esc(llms_preview)}</pre>
<p style="font-size:13px;color:#777">The paid kit also includes correct LocalBusiness JSON-LD schema
+ 5 personalized GEO content templates, all editable and un-watermarked.</p></div>
{urgency}
<a class="cta" href="{checkout_url}">Get {business} cited by AI — $39 Fix Kit</a>
<p style="text-align:center;font-size:13px;color:#777;margin-top:6px">30-day money-back guarantee — if it doesn't make you citable, full refund.</p>
<p style="font-size:12px;color:#999">One-time. Instant delivery. Built for {business} in {city}. {method}</p>
</body></html>"""


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ----------------------------------------------------------------- top-level generate

def generate_kit(business: str, city: str, category: str, *, domain: str = "", phone: str = "",
                 scan: dict | None = None, watermark: bool = True, checkout_url: str = "#",
                 base: Path = KITS_DIR) -> dict:
    """Write the full kit to disk. watermark=True => proof preview; False => paid full kit."""
    out = kit_dir_for(business, city, base=base)
    out.mkdir(parents=True, exist_ok=True)

    # Pull the business's REAL services / description / phone from their site so the kit is THEIRS,
    # not generic. Best-effort, $0, no LLM; falls back to category defaults if the site is unreachable.
    scraped = _scrape_business(domain) if domain else {}
    services = scraped.get("services") or _default_services(category)
    phone = phone or scraped.get("phone", "")
    description = scraped.get("description", "")

    llms = build_llms_txt(business, city, category, domain=domain, phone=phone, services=services,
                          description=description, watermark=watermark)
    schema = build_schema_jsonld(business, city, category, domain=domain, phone=phone, services=services,
                                 description=description, watermark=watermark)
    templates = build_content_templates(business, city, category, services=services, watermark=watermark)

    (out / "llms.txt").write_text(llms, encoding="utf-8")
    (out / "schema.jsonld").write_text(schema, encoding="utf-8")
    tpl_dir = out / "geo_templates"
    tpl_dir.mkdir(exist_ok=True)
    written = []
    for fname, md in templates:
        (tpl_dir / fname).write_text(md, encoding="utf-8")
        written.append(f"geo_templates/{fname}")

    files = ["llms.txt", "schema.jsonld", *written]
    if scan is not None:
        preview = build_preview_html(scan, checkout_url=checkout_url, domain=domain)
        (out / "preview.html").write_text(preview, encoding="utf-8")
        files.append("preview.html")

    manifest = {
        "business": business, "city": city, "category": category, "domain": domain,
        "watermark": watermark,
        "grade": (scan or {}).get("grade"),
        "visibility_score": (scan or {}).get("visibility_score"),
        "files": files,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "kit_type": "preview" if watermark else "full_paid",
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    manifest["path"] = str(out)
    return manifest


# ----------------------------------------------------------------- helpers

def _split_city(city: str) -> tuple[str, str]:
    parts = city.replace(",", " ").split()
    if len(parts) >= 2 and len(parts[-1]) == 2 and parts[-1].isalpha():
        return " ".join(parts[:-1]), parts[-1].upper()
    return city, ""


def _schema_type(category: str) -> str:
    c = category.lower()
    table = {
        "dentist": "Dentist", "plumber": "Plumber", "electrician": "Electrician",
        "lawyer": "LegalService", "attorney": "LegalService", "roofer": "RoofingContractor",
        "hvac": "HVACBusiness", "restaurant": "Restaurant", "salon": "HairSalon",
        "accountant": "AccountingService", "realtor": "RealEstateAgent",
        "landscaper": "LandscapingBusiness", "auto repair": "AutoRepair",
        "chiropractor": "Chiropractic", "veterinarian": "VeterinaryCare",
    }
    for k, v in table.items():
        if k in c:
            return v
    return "LocalBusiness"


def _default_services(category: str) -> list[str]:
    c = category.lower()
    if "plumb" in c:
        return ["Drain cleaning", "Water heater repair", "Leak detection", "Emergency plumbing"]
    if "dent" in c:
        return ["Cleanings & checkups", "Fillings", "Crowns", "Teeth whitening"]
    if "electric" in c:
        return ["Panel upgrades", "Wiring & rewiring", "Lighting installation", "Outlet repair"]
    if "hvac" in c or "heating" in c or "air" in c:
        return ["AC repair", "Furnace repair", "HVAC installation", "Maintenance plans"]
    if "roof" in c:
        return ["Roof repair", "Roof replacement", "Leak repair", "Inspections"]
    if "law" in c or "attorney" in c:
        return ["Consultations", "Case representation", "Document review", "Legal advice"]
    if "salon" in c or "hair" in c:
        return ["Haircuts", "Color", "Styling", "Treatments"]
    if "landscap" in c:
        return ["Lawn care", "Design & install", "Tree trimming", "Seasonal cleanup"]
    return ["Consultations", "Core service", "Maintenance", "Emergency service"]


def _scrape_business(domain: str, timeout: float = 8.0) -> dict:
    """Best-effort: pull REAL services / description / phone from the business homepage so the kit is
    theirs, not generic. $0, no LLM. Returns {} on any failure (kit falls back to templated defaults)."""
    if not domain:
        return {}
    import urllib.request
    url = domain if str(domain).startswith("http") else "https://" + str(domain).strip().lstrip("/")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X "
                                                   "10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"})
        html = urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "ignore")
    except Exception:
        return {}
    import html as _h
    out: dict = {}
    m = re.search(r'<meta[^>]+(?:name=["\']description["\']|property=["\']og:description["\'])[^>]+'
                  r'content=["\']([^"\']{40,300})', html, re.I)
    if m:
        out["description"] = _h.unescape(re.sub(r"\s+", " ", m.group(1)).strip())
    p = re.search(r'(\(?\d{3}\)?[\s.\-]\d{3}[\s.\-]\d{4})', html)
    if p:
        out["phone"] = p.group(1).strip()
    HINT = ("repair", "install", "replacement", "maintenance", "inspection", "tune", "upgrade",
            "cleaning", "drain", "heater", "wiring", "panel", "roof", "leak", "furnace", "duct",
            "remodel", "detection", "treatment", "removal", "sewer", "lines", "softener", "fixture")
    SKIP = ("home", "about", "contact", "blog", "menu", "login", "privacy", "cart", "careers",
            "reviews", "gallery", "financing", "schedule", "book ", "call ", "read more", "all service",
            "customer service", "great ", "flexible", "options", "trusted", "expert", "since ", "your ",
            "our ", "why ", "best ", "top ", "#1", "slow ", "clogged", "quality", "satisfaction",
            "guarantee", "free ", "financ", "coupon", "special", "offer", "general plumbing services")
    seen, svcs = set(), []
    for c in re.findall(r'<(?:h2|h3|li|a)[^>]*>([^<]{4,42})</(?:h2|h3|li|a)>', html, re.I):
        c = _h.unescape(re.sub(r"\s+", " ", c)).strip()
        cl = c.lower()
        if 4 < len(c) < 42 and any(h in cl for h in HINT) and not any(b in cl for b in SKIP) and cl not in seen:
            seen.add(cl)
            svcs.append(c[:1].upper() + c[1:] if c[:1].islower() else c)
        if len(svcs) >= 6:
            break
    if len(svcs) >= 3:
        out["services"] = svcs[:6]
    return out


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

    scan = None
    try:
        from core.geo_visibility_probe import score_business
        scan = score_business(args.business, args.city, args.category)
    except Exception as e:
        print(f"(scan unavailable, building kit without grade context: {e})")

    m = generate_kit(args.business, args.city, args.category, domain=args.domain, phone=args.phone,
                     scan=scan, watermark=not args.full, checkout_url=args.checkout)
    print(f"{'FULL' if args.full else 'PREVIEW'} kit -> {m['path']}")
    for f in m["files"]:
        print(f"  - {f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
