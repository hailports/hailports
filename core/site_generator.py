#!/usr/bin/env python3
"""site_generator.py — bespoke single-page business sites, a different one every run.

The revenue keystone for the Broken-Site Rescue play (data/AUTONOMOUS_REVENUE_BLUEPRINT.md):
the probe + rescue loop already finds local-biz sites that are measurably broken and queues
the owners. The missing piece was the *thing to show them* — a clean, modern rebuild mockup.
This builds it, headless and $0.

Design contract (why it never embarrasses a validated lead):
  • The STRUCTURE is pure Python. Curated libraries (palettes, system-font pairs, M>=3
    hand-built variants per section) are composed by a seeded RNG, so every output is
    always-valid standalone HTML and renders fully OFFLINE (system fonts + inline SVG only,
    zero remote <link>/<script>/<img>).
  • AI (local qwen via core.content_generator._ollama, $0) touches ONLY non-structural copy
    + a constrained theme *nudge*. It can never emit markup, never break layout, never the
    critical path — with the LLM stopped, the deterministic fallback bank still ships a page.
  • Randomization is GUARANTEED, not hoped-for: each run draws palette/font/section-order/
    optional-section-mask/per-section-variant/knobs, and a structural fingerprint is rerolled
    against a seen-ledger so consecutive runs differ on >=1 structural axis.

Public API:
    from core.site_generator import generate_site, generate_mockup
    html = generate_site({"domain": "acme.com", "vertical": "plumber", "city": "Austin"})
    out  = generate_mockup({"domain": "acme.com", "reasons": ["SSL expired"]})
    # out -> {"path", "url", "seed", "fingerprint", "manifest"}

CLI (writes sample pages to data/runtime/site_samples/):
    python3 -m core.site_generator --count 2
    python3 -m core.site_generator --domain joesplumbing.com --vertical plumber --no-llm
"""
from __future__ import annotations

import hashlib
import html as _html
import json
import os
import random
import sys
import time
from html.parser import HTMLParser
from pathlib import Path

sys.path.insert(0, os.path.expanduser("~/claude-stack"))

ROOT = Path(__file__).resolve().parent.parent
MOCKUP_DIR = ROOT / "products_internal" / "landing" / "mockups"
SAMPLE_DIR = ROOT / "data" / "runtime" / "site_samples"
SEEN_FILE = ROOT / "data" / "hustle" / "site_gen_seen.jsonl"
SEEN_KEEP = 400  # fingerprints compared against for the collision reroll

__all__ = ["generate_site", "generate_mockup", "compose"]


# ── curated palettes (light + a few dark; ink/bg pre-contrast-checked at runtime) ─
# index 0 seeded from core/maroon_branding.py (BrandA #6b0f0f).
PALETTES = [
    {"name": "BrandA",    "bg": "#fbf7f5", "surface": "#ffffff", "ink": "#1a1313", "muted": "#6f615e", "primary": "#6b0f0f", "accent": "#b3402f"},
    {"name": "slate",     "bg": "#f5f7fa", "surface": "#ffffff", "ink": "#14202b", "muted": "#566576", "primary": "#1f3a5f", "accent": "#2e74b5"},
    {"name": "forest",    "bg": "#f4f8f3", "surface": "#ffffff", "ink": "#15241a", "muted": "#566a5b", "primary": "#1f5135", "accent": "#2e9e5b"},
    {"name": "midnight",  "bg": "#0f141c", "surface": "#1a212c", "ink": "#eef2f7", "muted": "#9aa6b5", "primary": "#5b8def", "accent": "#7aa7ff"},
    {"name": "sunset",    "bg": "#fff8f2", "surface": "#ffffff", "ink": "#2a1a12", "muted": "#7a6253", "primary": "#c2410c", "accent": "#ea580c"},
    {"name": "teal",      "bg": "#f1faf9", "surface": "#ffffff", "ink": "#0f2826", "muted": "#52706d", "primary": "#0f766e", "accent": "#14b8a6"},
    {"name": "plum",      "bg": "#faf5fb", "surface": "#ffffff", "ink": "#241526", "muted": "#6b5a6f", "primary": "#7c2d92", "accent": "#a855f7"},
    {"name": "charcoal",  "bg": "#14171a", "surface": "#20242b", "ink": "#f0f2f4", "muted": "#99a2ad", "primary": "#f59e0b", "accent": "#fbbf24"},
    {"name": "navy-gold", "bg": "#f7f6f2", "surface": "#ffffff", "ink": "#1a1d24", "muted": "#67707b", "primary": "#1e293b", "accent": "#a9842b"},
    {"name": "sky",       "bg": "#f3f9fe", "surface": "#ffffff", "ink": "#0f2433", "muted": "#506576", "primary": "#0369a1", "accent": "#0ea5e9"},
    {"name": "rose",      "bg": "#fff5f6", "surface": "#ffffff", "ink": "#2a141a", "muted": "#7a5c63", "primary": "#9f1239", "accent": "#e11d48"},
    {"name": "sand",      "bg": "#f8f6f1", "surface": "#ffffff", "ink": "#221d14", "muted": "#6f665a", "primary": "#7c5a2e", "accent": "#b8862f"},
]

# system-font stacks ONLY — no Google Fonts <link>, so the file renders offline, zero calls.
FONT_PAIRS = [
    {"name": "system",   "head": "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif", "body": "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"},
    {"name": "georgia",  "head": "Georgia,'Times New Roman',serif", "body": "-apple-system,'Segoe UI',Roboto,sans-serif"},
    {"name": "palatino", "head": "'Palatino Linotype',Palatino,'Book Antiqua',Georgia,serif", "body": "Georgia,serif"},
    {"name": "trebuchet","head": "'Trebuchet MS','Segoe UI',Tahoma,sans-serif", "body": "'Trebuchet MS','Segoe UI',Tahoma,sans-serif"},
    {"name": "avenir",   "head": "'Avenir Next',Avenir,'Segoe UI',sans-serif", "body": "'Avenir Next',Avenir,'Segoe UI',sans-serif"},
    {"name": "cambria",  "head": "Cambria,Georgia,'Times New Roman',serif", "body": "-apple-system,'Segoe UI',sans-serif"},
    {"name": "optima",   "head": "Optima,Candara,'Segoe UI',sans-serif", "body": "Candara,'Segoe UI',sans-serif"},
    {"name": "baskervil","head": "Baskerville,'Hoefler Text',Georgia,serif", "body": "Baskerville,'Hoefler Text',Georgia,serif"},
    {"name": "tahoma",   "head": "Tahoma,Geneva,Verdana,sans-serif", "body": "Tahoma,Geneva,Verdana,sans-serif"},
    {"name": "mono-head","head": "ui-monospace,'SF Mono',Menlo,Consolas,monospace", "body": "-apple-system,'Segoe UI',Roboto,sans-serif"},
    {"name": "gill",     "head": "'Gill Sans','Gill Sans MT',Calibri,sans-serif", "body": "Calibri,'Segoe UI',sans-serif"},
]

# knob pools — emitted as CSS custom properties / class snippets.
RADII = [0, 6, 12, 20]
BUTTON_SHAPES = ["solid", "outline", "pill", "ghost"]
CARD_STYLES = ["flat", "bordered", "shadow"]
SHADOW_DEPTHS = ["none", "sm", "md", "lg"]
IMAGE_TREATMENTS = ["flat", "duotone", "grayscale", "gradient-overlay"]
SPACING_SCALES = {"compact": 50, "normal": 78, "airy": 112}
CONTAINER_WIDTHS = ["1040px", "1140px", "1240px"]
SECTION_ALIGNS = ["left", "center"]

_SHADOW_CSS = {
    "none": "none",
    "sm": "0 1px 3px rgba(0,0,0,.09)",
    "md": "0 8px 24px rgba(0,0,0,.10)",
    "lg": "0 20px 55px rgba(0,0,0,.16)",
}
_IMG_FILTER = {
    "flat": "none",
    "duotone": "contrast(1.06) saturate(1.25) hue-rotate(-8deg)",
    "grayscale": "grayscale(1) contrast(1.05)",
    "gradient-overlay": "none",
}

# line-icon glyphs (24x24), rendered with currentColor — no remote assets.
ICONS = {
    "drop": '<path d="M12 3c4 5 6 8 6 11a6 6 0 11-12 0c0-3 2-6 6-11z"/>',
    "leaf": '<path d="M5 19c0-8 6-14 14-14 0 8-6 14-14 14z"/><path d="M5 19c4-4 7-6 10-7"/>',
    "flame": '<path d="M12 3c3 4 5 6 5 9a5 5 0 11-10 0c0-2 1-3 2-4 1 1 2 1 2 0 0-2-1-3 1-5z"/>',
    "shield": '<path d="M12 3l7 3v6c0 5-7 9-7 9s-7-4-7-9V6z"/>',
    "scissors": '<circle cx="6" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><path d="M8.5 7.5L20 18M8.5 16.5L20 6"/>',
    "tooth": '<path d="M7 4c-2 0-3 2-3 5 0 4 1 6 2 9 .6 1.8 2 1.8 2.4 0l1-4c.3-1 1.9-1 2.2 0l1 4c.4 1.8 1.8 1.8 2.4 0 1-3 2-5 2-9 0-3-1-5-3-5-2 0-2 1-4 1s-2-1-4-1z"/>',
    "fork": '<path d="M7 3v7a2 2 0 004 0V3M9 3v18M17 3c-2 0-3 2-3 5s1 4 3 4v9"/>',
    "home": '<path d="M3 11l9-7 9 7M5 10v10h14V10z"/>',
    "wrench": '<path d="M21 4a5 5 0 01-6.5 6.5L5 20l-1-1 9.5-9.5A5 5 0 0121 4z"/>',
    "bolt": '<path d="M13 2L4 14h6l-1 8 9-12h-6z"/>',
    "phone": '<path d="M5 3h4l2 5-3 2c1 3 3 5 6 6l2-3 5 2v4c0 1-1 2-2 2C11 23 1 13 1 5c0-1 1-2 2-2z"/>',
    "check": '<path d="M20 6L9 17l-5-5"/>',
    "star": '<path d="M12 3l2.5 6 6.5.5-5 4 1.6 6.3L12 16.5 6.4 19.8 8 13.5l-5-4 6.5-.5z"/>',
    "clock": '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
}


# ── per-vertical defaults: trade-appropriate copy bank + theme mood + service set ─
VERTICAL_DEFAULTS = {
    "plumber": {
        "label": "Plumbing",
        "mood": [1, 3, 5, 8, 9],
        "icons": ["drop", "wrench", "flame", "shield", "clock"],
        "headline": "Reliable plumbing, done right the first time",
        "subhead": "Local plumbing pages for fast repairs, installs, and emergencies — clear services, clean layout, easy contact.",
        "about": "This rebuild gives visitors the essentials fast: what you fix, how to reach you, and why the site is safe to trust. From a dripping faucet to a full repipe, the page keeps the path from problem to quote simple.",
        "services": [("Emergency Repairs", "Burst pipes and leaks get a clear, urgent path to request help."),
                     ("Water Heaters", "Repair or replace tank and tankless units sized to your home."),
                     ("Drain Cleaning", "Clear stubborn clogs with camera inspection so it stays clear."),
                     ("Repiping & Installs", "Explain larger installs with plain service details and easy quote requests.")],
        "cta": "Get a free quote",
    },
    "hvac": {
        "label": "Heating & Cooling",
        "mood": [1, 3, 5, 9, 8],
        "icons": ["flame", "bolt", "shield", "home", "clock"],
        "headline": "Comfortable every season",
        "subhead": "Heating and air conditioning service, repair, and installs from techs who answer the phone.",
        "about": "This rebuild makes heating and cooling services easy to scan, with clear repair, install, and maintenance paths. Visitors can understand the next step without digging through a dated or broken site.",
        "services": [("AC Repair", "Fast cooling fixes with a clear diagnosis before any work begins."),
                     ("Furnace Service", "Tune-ups and repairs that keep the heat on through winter."),
                     ("New System Installs", "Present system options clearly so visitors can request the right quote."),
                     ("Maintenance Plans", "Seasonal checkups that catch small issues before they cost you.")],
        "cta": "Schedule service",
    },
    "restaurant": {
        "label": "Restaurant",
        "mood": [0, 4, 10, 11, 7],
        "icons": ["fork", "flame", "star", "leaf", "clock"],
        "headline": "Made fresh, served warm, every day",
        "subhead": "A neighborhood kitchen serving honest food, generous plates, and a welcome that feels like home.",
        "about": "This rebuild puts the menu, hours, ordering, and event details where hungry visitors expect them. The page is built to turn a quick search into a visit, pickup order, or reservation request.",
        "services": [("Dine In", "A warm room and a menu that has something for everyone at the table."),
                     ("Takeout & Pickup", "Order ahead and your food is hot and ready when you arrive."),
                     ("Catering", "Feed the whole crew with platters built for offices and parties."),
                     ("Private Events", "Book the space for birthdays, showers, and team dinners.")],
        "cta": "View the menu",
    },
    "salon": {
        "label": "Salon & Spa",
        "mood": [6, 10, 0, 11, 5],
        "icons": ["scissors", "star", "leaf", "drop", "clock"],
        "headline": "Look like yourself, only better",
        "subhead": "Cuts, color, and care from stylists who actually listen — leave feeling like the best version of you.",
        "about": "This rebuild keeps services, booking, pricing notes, and contact details easy to find. Visitors can see the right service path quickly and move straight to an appointment request.",
        "services": [("Cut & Style", "A consultation-first cut shaped to your hair and your routine."),
                     ("Color", "Balayage, highlights, and gloss work that grows out beautifully."),
                     ("Treatments", "Repair and shine services that bring tired hair back to life."),
                     ("Special Occasions", "Updos and makeup for weddings, photos, and big nights.")],
        "cta": "Book an appointment",
    },
    "landscaping": {
        "label": "Landscaping",
        "mood": [2, 5, 11, 8],
        "icons": ["leaf", "drop", "home", "star", "clock"],
        "headline": "A yard you're proud to come home to",
        "subhead": "Design, maintenance, and cleanups that keep your property sharp through every season.",
        "about": "This rebuild makes lawn care, cleanups, design work, and irrigation services easy to compare. Visitors can understand the offer quickly and request an estimate without hunting for a contact form.",
        "services": [("Lawn Care", "Mowing, edging, and feeding on a schedule you can forget about."),
                     ("Design & Build", "Beds, patios, and plantings designed for your space and soil."),
                     ("Cleanups", "Spring and fall cleanups that reset the whole property fast."),
                     ("Irrigation", "Smart watering that keeps things green without the waste.")],
        "cta": "Get a free estimate",
    },
    "dentist": {
        "label": "Dental Care",
        "mood": [1, 5, 9, 3],
        "icons": ["tooth", "shield", "star", "check", "clock"],
        "headline": "Gentle dental care for the whole family",
        "subhead": "A modern dental page layout with clear services, booking paths, and trust-building information.",
        "about": "This rebuild puts dental services, emergency visit details, office information, and appointment requests in a clean structure. Patients can understand the next step quickly on desktop or mobile.",
        "services": [("Checkups & Cleanings", "Thorough, comfortable visits that catch issues early."),
                     ("Cosmetic", "Whitening and veneers for a smile you're happy to show."),
                     ("Restorative", "Crowns, fillings, and implants that look and feel natural."),
                     ("Emergency Visits", "Same-day relief when a tooth can't wait.")],
        "cta": "Request an appointment",
    },
    "generic": {
        "label": "Local Business",
        "mood": [0, 1, 2, 4, 5, 6, 8, 9, 11],
        "icons": ["star", "shield", "check", "home", "clock"],
        "headline": "Local service you can count on",
        "subhead": "Friendly, dependable help from a team that treats your business like it's our own.",
        "about": "This rebuild gives visitors a clean first impression, clear service details, and an easy path to contact you. It replaces broken pages with a fast, mobile-friendly layout built around action.",
        "services": [("Our Services", "Dependable work tailored to exactly what you need."),
                     ("Free Consultation", "A no-pressure conversation about your goals and options."),
                     ("Fast Turnaround", "We respect your time and deliver when we say we will."),
                     ("Ongoing Support", "We're here after the job is done, not just before.")],
        "cta": "Get in touch",
    },
}

# keyword fallback for vertical detection when the LLM is offline.
_VERTICAL_KEYWORDS = {
    "plumber": ("plumb", "rooter", "pipe", "leak", "drain"),
    "hvac": ("hvac", "heating", "cooling", "air", "furnace", "climate", "comfort"),
    "restaurant": ("restaurant", "cafe", "grill", "pizza", "kitchen", "bistro", "eatery", "diner", "bbq", "taco", "sushi"),
    "salon": ("salon", "hair", "nail", "spa", "beauty", "barber", "lash", "studio"),
    "landscaping": ("landscap", "lawn", "garden", "yard", "tree", "irrigation", "outdoor"),
    "dentist": ("dental", "dentist", "smile", "orthodon", "ortho", "teeth"),
}

FACTS = ["Mobile-first layout", "Secure HTTPS-ready", "Fast-loading structure", "Clear service pages",
         "Visible call-to-action", "Easy quote path", "Local SEO basics", "Plain contact section"]
PROOF_POINTS = [
    ("Clear next steps", "Visitors can see what you offer and how to request help without digging."),
    ("Verified proof slots", "The page leaves room for real customer feedback, credentials, and photos when you provide them."),
    ("Built for phones", "Responsive sections keep calls, services, and booking paths usable on small screens."),
    ("Safer launch path", "Unverified dates, scores, guarantees, and credentials stay out until you provide them."),
]
FAQS = [
    ("Can this use my real proof and photos?", "Yes — add verified customer quotes, team photos, credentials, and project images before launch."),
    ("Is this ready for mobile visitors?", "Yes. The layout is responsive and keeps contact actions easy to reach on phones."),
    ("Can the copy be updated?", "Yes. Services, pricing notes, service area, and contact details are placeholders until verified."),
    ("What changes before going live?", "Replace placeholders with real business details, connect the domain, and run a final QA pass."),
]


# ── color helpers (WCAG-ish contrast so ink stays readable on every palette) ─────
def _rgb(hexc: str) -> tuple[int, int, int]:
    h = hexc.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _lum(hexc: str) -> float:
    def lin(c: float) -> float:
        c = c / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = _rgb(hexc)
    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)


def _contrast(a: str, b: str) -> float:
    la, lb = _lum(a), _lum(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def _on(bg: str) -> str:
    """Readable text color over any bg — guarantees button/band text contrast."""
    return "#0b0f14" if _contrast("#0b0f14", bg) >= _contrast("#ffffff", bg) else "#ffffff"


def _palette_ok(p: dict) -> bool:
    return _contrast(p["ink"], p["bg"]) >= 4.5 and _contrast(p["ink"], p["surface"]) >= 4.5


# ── seeded inline-SVG placeholder imagery (self-contained, offline, palette-tinted) ─
def _svg_banner(t: dict, brng: random.Random) -> str:
    p, a, s = t["primary"], t["accent"], t["surface"]
    shapes = [f'<rect width="1200" height="540" fill="url(#bg)"/>']
    for _ in range(brng.randint(4, 7)):
        cx, cy, r = brng.randint(80, 1120), brng.randint(60, 480), brng.randint(60, 220)
        col = brng.choice([p, a, "#ffffff"])
        op = round(brng.uniform(0.06, 0.22), 2)
        shapes.append(f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="{col}" opacity="{op}"/>')
    for _ in range(brng.randint(1, 3)):
        x, y, w = brng.randint(0, 900), brng.randint(0, 380), brng.randint(160, 360)
        col = brng.choice([a, p, "#ffffff"])
        op = round(brng.uniform(0.05, 0.16), 2)
        shapes.append(f'<polygon points="{x},{y} {x + w},{y + 40} {x + w - 60},{y + w} {x - 40},{y + w - 30}" fill="{col}" opacity="{op}"/>')
    if t["image_treatment"] == "gradient-overlay":
        shapes.append(f'<rect width="1200" height="540" fill="{p}" opacity="0.28"/>')
    return (
        f'<svg viewBox="0 0 1200 540" preserveAspectRatio="xMidYMid slice" '
        f'xmlns="http://www.w3.org/2000/svg" role="img" aria-label="Banner" '
        f'style="width:100%;height:100%;display:block">'
        f'<defs><linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">'
        f'<stop offset="0" stop-color="{p}"/><stop offset="1" stop-color="{a}"/>'
        f'</linearGradient></defs>{"".join(shapes)}</svg>'
    )


def _svg_tile(t: dict, brng: random.Random) -> str:
    p, a = t["primary"], t["accent"]
    base = brng.choice([p, a])
    parts = [f'<rect width="400" height="300" fill="{base}" opacity="0.14"/>']
    for _ in range(brng.randint(2, 4)):
        cx, cy, r = brng.randint(40, 360), brng.randint(30, 270), brng.randint(30, 110)
        parts.append(f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="{brng.choice([p, a])}" opacity="{round(brng.uniform(0.12, 0.4), 2)}"/>')
    return (
        f'<svg viewBox="0 0 400 300" preserveAspectRatio="xMidYMid slice" '
        f'xmlns="http://www.w3.org/2000/svg" aria-hidden="true" '
        f'style="width:100%;height:100%;display:block">{"".join(parts)}</svg>'
    )


def _icon(name: str) -> str:
    inner = ICONS.get(name, ICONS["star"])
    return (
        '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
        'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
        f"{inner}</svg>"
    )


# ── section variant library — each fn(t, c) -> a complete <section id="sec-NAME">. ─
# Markup is authored here (the ONLY place), so validity is structural, never generated.
def _sec(name: str, inner: str, cls: str = "section", style: str = "") -> str:
    st = f' style="{style}"' if style else ""
    return f'<section id="sec-{name}" class="{cls}"{st}>{inner}</section>'


def _btn(c: dict, label_key: str = "cta_button", accent: bool = False) -> str:
    cls = "btn btn-accent" if accent else "btn"
    return f'<a class="{cls}" href="#sec-contact">{c[label_key]}</a>'


# hero ----------------------------------------------------------------------------
def _hero_split(t, c):
    inner = (
        '<div class="container"><div class="row" style="gap:40px">'
        f'<div><p class="eyebrow">{c["eyebrow"]}</p><h1>{c["headline"]}</h1>'
        f'<p class="lead">{c["subhead"]}</p><p style="margin-top:8px">{_btn(c)} '
        f'<a class="btn btn-ghost" href="#sec-services_cards">See services</a></p></div>'
        f'<div class="banner" style="min-height:340px">{t["banner"]}</div>'
        "</div></div>"
    )
    return _sec("hero", inner, cls="section hero")


def _hero_centered(t, c):
    inner = (
        '<div class="container center" style="max-width:820px">'
        f'<p class="eyebrow">{c["eyebrow"]}</p><h1>{c["headline"]}</h1>'
        f'<p class="lead">{c["subhead"]}</p><p style="margin-top:10px">{_btn(c, accent=True)}</p>'
        f'<div class="banner" style="margin-top:34px;min-height:300px">{t["banner"]}</div>'
        "</div>"
    )
    return _sec("hero", inner, cls="section hero")


def _hero_overlay(t, c):
    inner = (
        f'<div class="hero-overlay banner" style="position:relative">{t["banner"]}'
        '<div class="hero-overlay-text" style="position:absolute;inset:0;display:flex;'
        'align-items:center"><div class="container">'
        f'<div style="max-width:640px;color:#fff;text-shadow:0 2px 18px rgba(0,0,0,.45)">'
        f'<p class="eyebrow" style="color:#fff">{c["eyebrow"]}</p>'
        f'<h1 style="color:#fff">{c["headline"]}</h1>'
        f'<p class="lead" style="color:#f3f3f3">{c["subhead"]}</p>{_btn(c, accent=True)}'
        "</div></div></div></div>"
    )
    return _sec("hero", inner, cls="section hero", style="padding:0")


# about ---------------------------------------------------------------------------
def _about_split(t, c):
    facts = "".join(f"<li>{f}</li>" for f in c["facts"])
    inner = (
        '<div class="container"><div class="row" style="gap:40px">'
        f'<div><p class="eyebrow">{c["about_eyebrow"]}</p><h2>{c["about_title"]}</h2>'
        f'<hr class="divider"><p>{c["about_body"]}</p>{_btn(c)}</div>'
        f'<div><ul class="clean">{facts}</ul></div></div></div>'
    )
    return _sec("about", inner, cls="section surface")


def _about_centered(t, c):
    stats = "".join(
        f'<div><div class="stat">{s}</div><div class="muted">{lbl}</div></div>'
        for s, lbl in c["stats"]
    )
    inner = (
        '<div class="container center" style="max-width:780px">'
        f'<p class="eyebrow">{c["about_eyebrow"]}</p><h2>{c["about_title"]}</h2><p class="lead">{c["about_body"]}</p>'
        f'<div class="grid g3" style="margin-top:28px;text-align:center">{stats}</div></div>'
    )
    return _sec("about", inner)


def _about_zigzag(t, c):
    facts = "".join(f"<li>{f}</li>" for f in c["facts"][:3])
    inner = (
        '<div class="container"><div class="row" style="gap:40px;flex-direction:row-reverse">'
        f'<div class="banner" style="min-height:260px">{t["tiles"][0]}</div>'
        f'<div><p class="eyebrow">{c["about_eyebrow"]}</p><h2>{c["about_title"]}</h2>'
        f'<p>{c["about_body"]}</p><ul class="clean">{facts}</ul></div></div></div>'
    )
    return _sec("about", inner, cls="section surface")


# services ------------------------------------------------------------------------
def _services_grid(t, c):
    cards = "".join(
        f'<div class="card"><span style="color:var(--accent)">{_icon(ic)}</span>'
        f'<h3 style="margin-top:14px">{name}</h3><p class="muted">{blurb}</p></div>'
        for (name, blurb, ic) in c["services"]
    )
    inner = (
        f'<div class="container"><p class="eyebrow">{c["services_eyebrow"]}</p>'
        f'<h2>{c["services_title"]}</h2><div class="grid g3" style="margin-top:26px">{cards}</div></div>'
    )
    return _sec("services_cards", inner)


def _services_list(t, c):
    rows = "".join(
        '<div class="card" style="display:flex;gap:18px;align-items:flex-start;margin-bottom:16px">'
        f'<span style="color:var(--accent);flex:0 0 auto">{_icon(ic)}</span>'
        f'<div><h3>{name}</h3><p class="muted" style="margin:0">{blurb}</p></div></div>'
        for (name, blurb, ic) in c["services"]
    )
    inner = (
        f'<div class="container" style="max-width:820px"><p class="eyebrow">{c["services_eyebrow"]}</p>'
        f'<h2>{c["services_title"]}</h2><div style="margin-top:24px">{rows}</div></div>'
    )
    return _sec("services_cards", inner, cls="section surface")


def _services_zigzag(t, c):
    rows = []
    for i, (name, blurb, ic) in enumerate(c["services"]):
        rev = ";flex-direction:row-reverse" if i % 2 else ""
        tile = t["tiles"][i % len(t["tiles"])]
        rows.append(
            f'<div class="row" style="gap:36px;margin-bottom:28px{rev}">'
            f'<div class="banner" style="min-height:200px">{tile}</div>'
            f'<div><span style="color:var(--accent)">{_icon(ic)}</span><h3 style="margin-top:10px">{name}</h3>'
            f'<p class="muted">{blurb}</p></div></div>'
        )
    inner = (
        f'<div class="container"><p class="eyebrow">{c["services_eyebrow"]}</p>'
        f'<h2>{c["services_title"]}</h2><div style="margin-top:24px">{"".join(rows)}</div></div>'
    )
    return _sec("services_cards", inner)


# proof / trust-building ----------------------------------------------------------
def _proof_cards(t, c):
    cards = "".join(
        f'<div class="card"><h3>{title}</h3>'
        f'<p class="muted" style="margin:0">{body}</p></div>'
        for (title, body) in c["proof_points"][:3]
    )
    inner = (
        f'<div class="container center"><p class="eyebrow">{c["proof_eyebrow"]}</p>'
        f'<h2>{c["proof_title"]}</h2><div class="grid g3" style="margin-top:26px;text-align:left">{cards}</div></div>'
    )
    return _sec("proof", inner, cls="section surface")


def _proof_single(t, c):
    title, body = c["proof_points"][0]
    inner = (
        '<div class="container center" style="max-width:760px">'
        f'<div style="color:var(--accent)">{_icon("star")}</div>'
        f'<h2>{title}</h2>'
        f'<p class="lead">{body}</p></div>'
    )
    return _sec("proof", inner)


def _proof_stats(t, c):
    stats = "".join(
        f'<div><div class="stat">{s}</div><div class="muted">{lbl}</div></div>'
        for s, lbl in c["stats"]
    )
    title, body = c["proof_points"][1]
    inner = (
        f'<div class="container"><div class="grid g3" style="text-align:center">{stats}</div>'
        f'<p class="center" style="max-width:680px;margin:28px auto 0;font-size:1.15rem">'
        f'<strong>{title}:</strong> <span class="muted">{body}</span></p></div>'
    )
    return _sec("proof", inner, cls="section surface")


# gallery -------------------------------------------------------------------------
def _gallery_grid(t, c):
    tiles = "".join(
        f'<div class="banner" style="min-height:170px">{t["tiles"][i % len(t["tiles"])]}</div>'
        for i in range(c["gallery_count"])
    )
    inner = (
        f'<div class="container"><p class="eyebrow">{c["gallery_eyebrow"]}</p>'
        f'<h2>{c["gallery_title"]}</h2><div class="grid g3" style="margin-top:24px">{tiles}</div></div>'
    )
    return _sec("gallery", inner)


def _gallery_masonry(t, c):
    tiles = "".join(
        f'<div class="banner" style="min-height:{150 + (i % 3) * 60}px;break-inside:avoid;margin-bottom:18px">'
        f'{t["tiles"][i % len(t["tiles"])]}</div>'
        for i in range(c["gallery_count"])
    )
    inner = (
        f'<div class="container"><p class="eyebrow">{c["gallery_eyebrow"]}</p>'
        f'<h2>{c["gallery_title"]}</h2>'
        f'<div style="column-count:3;column-gap:18px;margin-top:24px">{tiles}</div></div>'
    )
    return _sec("gallery", inner, cls="section surface")


def _gallery_strip(t, c):
    tiles = "".join(
        f'<div class="banner" style="flex:0 0 280px;min-height:180px">{t["tiles"][i % len(t["tiles"])]}</div>'
        for i in range(c["gallery_count"])
    )
    inner = (
        f'<div class="container"><p class="eyebrow">{c["gallery_eyebrow"]}</p><h2>{c["gallery_title"]}</h2></div>'
        f'<div style="display:flex;gap:18px;overflow-x:auto;padding:24px;scroll-snap-type:x mandatory">{tiles}</div>'
    )
    return _sec("gallery", inner)


# faq -----------------------------------------------------------------------------
def _faq_accordion(t, c):
    items = "".join(
        f'<details><summary>{q}</summary><p class="muted" style="margin:.6em 0 0">{a}</p></details>'
        for (q, a) in c["faqs"]
    )
    inner = (
        f'<div class="container" style="max-width:780px"><p class="eyebrow">{c["faq_eyebrow"]}</p>'
        f'<h2>{c["faq_title"]}</h2><div style="margin-top:18px">{items}</div></div>'
    )
    return _sec("faq", inner, cls="section surface")


def _faq_twocol(t, c):
    items = "".join(
        f'<div><h3>{q}</h3><p class="muted">{a}</p></div>' for (q, a) in c["faqs"]
    )
    inner = (
        f'<div class="container"><p class="eyebrow">{c["faq_eyebrow"]}</p><h2>{c["faq_title"]}</h2>'
        f'<div class="grid g2" style="margin-top:24px">{items}</div></div>'
    )
    return _sec("faq", inner)


def _faq_list(t, c):
    items = "".join(
        f'<div class="card" style="margin-bottom:14px"><h3>{q}</h3>'
        f'<p class="muted" style="margin:0">{a}</p></div>'
        for (q, a) in c["faqs"]
    )
    inner = (
        f'<div class="container" style="max-width:760px"><p class="eyebrow">{c["faq_eyebrow"]}</p>'
        f'<h2>{c["faq_title"]}</h2><div style="margin-top:20px">{items}</div></div>'
    )
    return _sec("faq", inner, cls="section surface")


# cta band ------------------------------------------------------------------------
def _cta_centered(t, c):
    inner = (
        '<div class="container center band" style="max-width:760px">'
        f'<h2 style="color:var(--on-primary)">{c["cta_title"]}</h2>'
        f'<p style="color:var(--on-primary);opacity:.92">{c["cta_sub"]}</p>{_btn(c, accent=True)}</div>'
    )
    return _sec("cta_band", inner, cls="section band-wrap")


def _cta_split(t, c):
    inner = (
        '<div class="container band"><div class="row" style="gap:24px">'
        f'<div><h2 style="color:var(--on-primary);margin:0">{c["cta_title"]}</h2>'
        f'<p style="color:var(--on-primary);opacity:.92;margin:.4em 0 0">{c["cta_sub"]}</p></div>'
        f'<div style="flex:0 0 auto">{_btn(c, accent=True)}</div></div></div>'
    )
    return _sec("cta_band", inner, cls="section band-wrap")


def _cta_boxed(t, c):
    inner = (
        '<div class="container"><div class="band" style="border-radius:var(--radius);text-align:center">'
        f'<h2 style="color:var(--on-primary)">{c["cta_title"]}</h2>'
        f'<p style="color:var(--on-primary);opacity:.92">{c["cta_sub"]}</p>'
        f'<p class="muted" style="color:var(--on-primary);opacity:.8;margin:.2em 0 1em">'
        f'{c["phone"]} &middot; {c["email"]}</p>{_btn(c, accent=True)}</div></div>'
    )
    return _sec("cta_band", inner)


# contact -------------------------------------------------------------------------
def _contact_split(t, c):
    inner = (
        '<div class="container"><div class="row" style="gap:40px">'
        f'<div><p class="eyebrow">{c["contact_eyebrow"]}</p><h2>{c["contact_title"]}</h2>'
        f'<p class="muted">{c["contact_body"]}</p>{_btn(c, label_key="cta_button", accent=True)}</div>'
        f'<div class="card"><p><strong>Call</strong><br>{c["phone"]}</p>'
        f'<p><strong>Email</strong><br>{c["email"]}</p>'
        f'<p><strong>Hours</strong><br>{c["hours"]}</p>'
        f'{("<p><strong>Area</strong><br>" + c["city"] + "</p>") if c["city"] else ""}</div></div></div>'
    )
    return _sec("contact", inner, cls="section surface")


def _contact_cards(t, c):
    cards = (
        f'<div class="card center"><span style="color:var(--accent)">{_icon("phone")}</span>'
        f'<h3>Call us</h3><p class="muted">{c["phone"]}</p></div>'
        f'<div class="card center"><span style="color:var(--accent)">{_icon("check")}</span>'
        f'<h3>Email</h3><p class="muted">{c["email"]}</p></div>'
        f'<div class="card center"><span style="color:var(--accent)">{_icon("clock")}</span>'
        f'<h3>Hours</h3><p class="muted">{c["hours"]}</p></div>'
    )
    inner = (
        f'<div class="container center"><p class="eyebrow">{c["contact_eyebrow"]}</p>'
        f'<h2>{c["contact_title"]}</h2><div class="grid g3" style="margin-top:26px;text-align:left">{cards}</div></div>'
    )
    return _sec("contact", inner)


def _contact_band(t, c):
    inner = (
        '<div class="container band" style="text-align:center">'
        f'<h2 style="color:var(--on-primary)">{c["contact_title"]}</h2>'
        f'<p style="color:var(--on-primary);opacity:.92">{c["phone"]} &middot; {c["email"]}'
        f'{(" &middot; " + c["hours"]) if c["hours"] else ""}</p>{_btn(c, accent=True)}</div>'
    )
    return _sec("contact", inner, cls="section band-wrap")


# footer --------------------------------------------------------------------------
def _footer_simple(t, c):
    inner = (
        f'<div class="container center"><strong style="font-family:var(--font-head)">{c["name"]}</strong>'
        f'<p class="muted" style="margin:.4em 0 0">{c["phone"]} &middot; {c["email"]}</p>'
        f'<p class="muted" style="margin:.4em 0 0">&copy; {c["year"]} {c["name"]}. All rights reserved.</p></div>'
    )
    return _sec("footer", f"<footer>{inner}</footer>", cls="section surface")


def _footer_columns(t, c):
    inner = (
        '<div class="container"><div class="grid g3">'
        f'<div><strong style="font-family:var(--font-head)">{c["name"]}</strong>'
        f'<p class="muted">{c["subhead"]}</p></div>'
        f'<div><strong>Contact</strong><p class="muted">{c["phone"]}<br>{c["email"]}</p></div>'
        f'<div><strong>Hours</strong><p class="muted">{c["hours"]}'
        f'{("<br>" + c["city"]) if c["city"] else ""}</p></div></div>'
        f'<p class="muted center" style="margin-top:24px">&copy; {c["year"]} {c["name"]}.</p></div>'
    )
    return _sec("footer", f"<footer>{inner}</footer>", cls="section surface")


def _footer_band(t, c):
    inner = (
        '<div class="container band" style="text-align:center">'
        f'<strong style="color:var(--on-primary);font-family:var(--font-head)">{c["name"]}</strong>'
        f'<p style="color:var(--on-primary);opacity:.9;margin:.4em 0 0">{c["phone"]} &middot; {c["email"]}</p>'
        f'<p style="color:var(--on-primary);opacity:.75;margin:.4em 0 0">&copy; {c["year"]} {c["name"]}.</p></div>'
    )
    return _sec("footer", f"<footer>{inner}</footer>", cls="section band-wrap")


SECTIONS = {
    "hero": [_hero_split, _hero_centered, _hero_overlay],
    "about": [_about_split, _about_centered, _about_zigzag],
    "services_cards": [_services_grid, _services_list, _services_zigzag],
    "proof": [_proof_cards, _proof_single, _proof_stats],
    "gallery": [_gallery_grid, _gallery_masonry, _gallery_strip],
    "faq": [_faq_accordion, _faq_twocol, _faq_list],
    "cta_band": [_cta_centered, _cta_split, _cta_boxed],
    "contact": [_contact_split, _contact_cards, _contact_band],
    "footer": [_footer_simple, _footer_columns, _footer_band],
}

# static stylesheet (references var(--*); no interpolation here, so braces are literal).
_BASE_CSS = """
*,*::before,*::after{box-sizing:border-box}
html{scroll-behavior:smooth}
body{margin:0;background:var(--bg);color:var(--ink);font-family:var(--font-body);line-height:1.6;font-size:17px}
h1,h2,h3{font-family:var(--font-head);line-height:1.15;margin:0 0 .5em;color:var(--ink);font-weight:700}
h1{font-size:clamp(2rem,5vw,3.4rem)}
h2{font-size:clamp(1.5rem,3vw,2.3rem)}
h3{font-size:1.2rem;margin:0 0 .4em}
p{margin:0 0 1em}
a{color:var(--primary)}
img,svg{max-width:100%}
.container{width:100%;max-width:var(--maxw);margin:0 auto;padding:0 24px}
.section{padding:var(--pad-y) 0;text-align:var(--align)}
.surface{background:var(--surface)}
.muted{color:var(--muted)}
.eyebrow{text-transform:uppercase;letter-spacing:.12em;font-size:.78rem;font-weight:700;color:var(--accent);margin:0 0 .8em}
.lead{font-size:1.15rem;color:var(--muted)}
.grid{display:grid;gap:24px}
.g2{grid-template-columns:repeat(2,1fr)}
.g3{grid-template-columns:repeat(3,1fr)}
.center{text-align:center}
.banner{border-radius:var(--radius);overflow:hidden;filter:var(--img-filter);background:var(--surface)}
.icon{width:34px;height:34px}
.row{display:flex;gap:24px;align-items:center;flex-wrap:wrap}
.row>*{flex:1 1 300px}
.stat{font-family:var(--font-head);font-size:2rem;font-weight:700;color:var(--primary)}
.divider{height:3px;width:60px;background:var(--accent);border:0;margin:0 0 1.2em}
ul.clean{list-style:none;padding:0;margin:0}
ul.clean li{padding:9px 0 9px 28px;position:relative}
ul.clean li::before{content:"";position:absolute;left:0;top:16px;width:12px;height:12px;border-radius:3px;background:var(--accent)}
details{border-top:1px solid rgba(127,127,127,.28);padding:14px 0}
summary{cursor:pointer;font-weight:600;font-family:var(--font-head)}
blockquote{margin:0}
footer{color:var(--muted);font-size:.92rem}
.band-wrap{padding:0}
.band{background:var(--primary);color:var(--on-primary);padding:var(--pad-y) 40px}
.btn{display:inline-block;font-family:var(--font-head);font-weight:600;text-decoration:none;cursor:pointer;border:2px solid var(--primary);padding:14px 26px;line-height:1;transition:filter .15s}
.btn:hover{filter:brightness(1.08)}
.btn-accent{background:var(--accent);color:var(--on-accent);border-color:var(--accent)}
@media(max-width:760px){.g2,.g3{grid-template-columns:1fr}.section{padding:calc(var(--pad-y)*.62) 0}.band{padding:48px 22px}}
"""

_BTN_CSS = {
    "solid":   ".btn{background:var(--primary);color:var(--on-primary);border-radius:var(--radius)}",
    "outline": ".btn{background:transparent;color:var(--primary);border-radius:var(--radius)}",
    "pill":    ".btn{background:var(--primary);color:var(--on-primary);border-radius:999px}",
    "ghost":   ".btn{background:rgba(127,127,127,.12);color:var(--primary);border-color:transparent;border-radius:var(--radius)}.btn-ghost{background:transparent;border:2px solid var(--primary);color:var(--primary);border-radius:var(--radius)}",
}
_CARD_CSS = {
    "flat":     ".card{background:var(--surface);border-radius:var(--radius);padding:26px}",
    "bordered": ".card{background:var(--surface);border-radius:var(--radius);padding:26px;border:1px solid rgba(127,127,127,.26)}",
    "shadow":   ".card{background:var(--surface);border-radius:var(--radius);padding:26px;box-shadow:var(--shadow)}",
}
# default .btn-ghost when not the ghost button shape
_GHOST_FALLBACK = ".btn-ghost{background:transparent;border:2px solid var(--primary);color:var(--primary);border-radius:var(--radius)}"


# ── LLM copy layer (local qwen via content_generator._ollama; always has a fallback) ─
def _llm_available() -> bool:
    if os.environ.get("SITE_GEN_NO_LLM"):
        return False
    return True


def _has_unverified_claim(txt: str) -> bool:
    import re
    patterns = (
        r"\bsince\s+(19|20)\d{2}\b",
        r"\b\d+\+?\s*(years?|yrs?)\b",
        r"\b\d(?:\.\d)?\s*(?:star|stars|★)\b",
        r"\b(5-star|five-star|top-rated|award-winning)\b",
        r"\b(licensed|insured|certified|accredited)\b",
        r"\b(family-owned|family owned|family-run|family run)\b",
        r"\b(guaranteed|satisfaction guarantee|same-day|upfront pricing)\b",
        r"\b(reviews?|testimonials?)\b",
    )
    blob = (txt or "").lower()
    return any(re.search(p, blob) for p in patterns)


def _light_clean(txt: str, max_len: int, strip_end_punct: bool = False) -> str:
    import re
    txt = re.sub(r"https?://\S+|www\.\S+", "", txt or "")  # never let copy carry a fetchable URL
    txt = " ".join(txt.split())
    txt = txt.strip().strip('"').strip("'").strip()
    if len(txt) > max_len:
        cut = txt[:max_len].rsplit(" ", 1)[0]
        txt = cut if cut else txt[:max_len]
    if strip_end_punct:
        txt = txt.rstrip(".!,; ")
    return txt


def _slot(prompt: str, fallback: str, max_len: int, *, use_llm: bool,
          strip_end_punct: bool = False, max_tokens: int = 70) -> str:
    """One copy slot: LLM draft -> light slop filter -> escape, else escaped fallback."""
    if use_llm:
        try:
            from core.content_generator import _ollama
            from core.content_quality import gate
        except Exception:
            use_llm = False
    if use_llm:
        for attempt in range(2):
            draft = _ollama(prompt, temperature=0.7 + 0.2 * attempt, max_tokens=max_tokens,
                            single_line=False)
            draft = _light_clean(draft, max_len, strip_end_punct)
            if not draft or len(draft) < 3:
                continue
            if _has_unverified_claim(draft):
                continue
            try:
                v = gate(draft, channel="instagram_caption", persona="generic", recent=[])
                bad = bool(v.checks.get("slop") or v.checks.get("forbidden")) or any(
                    ("artifact" in r or "garbled" in r) for r in v.reasons
                )
            except Exception:
                bad = False
            if not bad:
                return _html.escape(draft)
    return _html.escape(_light_clean(fallback, max_len, strip_end_punct))


def _ai_theme_index(vertical: str, name: str, use_llm: bool) -> int | None:
    """Constrained theme nudge: LLM returns a palette index 0..N-1, else None."""
    if not use_llm:
        return None
    try:
        from core.content_generator import _ollama
    except Exception:
        return None
    moods = "; ".join(f"{i}={p['name']}" for i, p in enumerate(PALETTES))
    prompt = (
        f"Pick the best color mood for a {vertical} business website named {name}. "
        f"Options: {moods}. Reply with ONLY the single integer index, nothing else."
    )
    raw = _ollama(prompt, temperature=0.3, max_tokens=6, single_line=True)
    digits = "".join(ch for ch in (raw or "") if ch.isdigit())
    if not digits:
        return None
    try:
        idx = int(digits[:2])
    except ValueError:
        return None
    return idx if 0 <= idx < len(PALETTES) else None


# ── enrichment: fill {name, vertical, city} the queue record lacks ───────────────
def _norm_domain(domain: str) -> str:
    d = (domain or "").strip().lower()
    for pre in ("https://", "http://", "www."):
        if d.startswith(pre):
            d = d[len(pre):]
    return d.split("/")[0].split("?")[0].strip(". ")


def _name_from_domain(domain: str) -> str:
    base = _norm_domain(domain).split(".")[0]
    if base in ("www", "web", "site", "home") and "." in domain:
        base = _norm_domain(domain).split(".")[1]
    words = base.replace("-", " ").replace("_", " ").split()
    return " ".join(w.capitalize() for w in words) or "Your Business"


def _guess_vertical(domain: str, title: str, use_llm: bool) -> str:
    blob = f"{domain} {title}".lower()
    for vert, kws in _VERTICAL_KEYWORDS.items():
        if any(k in blob for k in kws):
            return vert
    if use_llm:
        try:
            from core.content_generator import _ollama
            keys = ", ".join(k for k in VERTICAL_DEFAULTS if k != "generic")
            raw = _ollama(
                f"Domain '{domain}', page title '{title[:120]}'. Which category fits best? "
                f"Reply with exactly ONE word from: {keys}, generic.",
                temperature=0.2, max_tokens=8, single_line=True,
            ).lower().strip().strip(".")
            for k in VERTICAL_DEFAULTS:
                if k in raw:
                    return k
        except Exception:
            pass
    return "generic"


def _title_from_html(page: str) -> str:
    if not page:
        return ""
    import re
    m = re.search(r"<title[^>]*>(.*?)</title>", page, re.I | re.S)
    t = m.group(1).strip() if m else ""
    if not t:
        m = re.search(r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']+)', page, re.I)
        t = m.group(1).strip() if m else ""
    return " ".join(t.split())[:160]


def _enrich(profile: dict) -> dict:
    p = dict(profile or {})
    domain = _norm_domain(p.get("domain") or p.get("url") or "example.com")
    p["domain"] = domain
    use_llm = bool(p.get("use_llm", True)) and _llm_available()
    p["use_llm"] = use_llm

    title = _title_from_html(p.get("html") or "")
    if not title and p.get("refetch"):
        try:
            from core.web_failure_probe import _fetch
            _, page, _ = _fetch(f"https://{domain}")
            title = _title_from_html(page)
        except Exception:
            title = ""

    vertical = (p.get("vertical") or "").strip().lower()
    if vertical not in VERTICAL_DEFAULTS:
        # Keep the REAL trade (e.g. "med spa", "chiropractor") as the human label so the
        # copy + title stay bespoke even though the theme/copy-bank falls back to generic.
        if vertical:
            p["vertical_label"] = vertical.title()
        vertical = _guess_vertical(domain, title, use_llm)
    p["vertical"] = vertical
    p["name"] = (p.get("name") or "").strip() or _name_from_domain(domain)
    p["city"] = (p.get("city") or "").strip()
    p["broken_findings"] = p.get("broken_findings") or p.get("reasons") or []
    return p


# ── composition: seeded structural draw + collision reroll ───────────────────────
def compose(profile: dict, rng: random.Random) -> dict:
    vert = VERTICAL_DEFAULTS.get(profile.get("vertical", "generic"), VERTICAL_DEFAULTS["generic"])

    moods = [i for i in vert["mood"] if _palette_ok(PALETTES[i])] or list(range(len(PALETTES)))
    ai_pref = profile.get("_ai_palette")
    if ai_pref is not None and ai_pref not in moods and _palette_ok(PALETTES[ai_pref]):
        moods = moods + [ai_pref]
    palette = rng.choice(moods)
    while not _palette_ok(PALETTES[palette]):
        palette = rng.randrange(len(PALETTES))
    fonts = rng.randrange(len(FONT_PAIRS))

    show_proof = rng.random() < 0.82
    show_gallery = rng.random() < 0.58
    show_faq = rng.random() < 0.72
    content = ["about", "services_cards"]
    if show_proof:
        content.append("proof")
    if show_gallery:
        content.append("gallery")
    if show_faq:
        content.append("faq")
    rng.shuffle(content)
    tail = ["cta_band", "contact"]
    rng.shuffle(tail)
    order = ["hero"] + content + tail + ["footer"]

    knobs = {
        "radius": rng.choice(RADII),
        "button_shape": rng.choice(BUTTON_SHAPES),
        "card_style": rng.choice(CARD_STYLES),
        "shadow_depth": rng.choice(SHADOW_DEPTHS),
        "image_treatment": rng.choice(IMAGE_TREATMENTS),
        "spacing_scale": rng.choice(list(SPACING_SCALES)),
        "container_width": rng.choice(CONTAINER_WIDTHS),
        "section_align": rng.choice(SECTION_ALIGNS),
        "services_count": rng.choice([3, 4]),
        "gallery_count": rng.choice([4, 6]),
        "faq_count": rng.choice([3, 4]),
    }
    variants = {name: rng.randrange(len(SECTIONS[name])) for name in order}

    return {
        "domain": profile["domain"],
        "vertical": profile.get("vertical", "generic"),
        "name": profile["name"],
        "city": profile.get("city", ""),
        "palette": palette,
        "fonts": fonts,
        "order": order,
        "variants": variants,
        "knobs": knobs,
    }


def _fingerprint(m: dict) -> str:
    struct = {k: m[k] for k in ("palette", "fonts", "order", "variants", "knobs")}
    return hashlib.sha256(json.dumps(struct, sort_keys=True).encode()).hexdigest()[:16]


def _load_seen() -> set[str]:
    seen: set[str] = set()
    if SEEN_FILE.exists():
        lines = SEEN_FILE.read_text(encoding="utf-8").splitlines()[-SEEN_KEEP:]
        for line in lines:
            try:
                seen.add(json.loads(line)["fp"])
            except Exception:
                continue
    return seen


def _append_seen(fp: str, domain: str) -> None:
    try:
        SEEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(SEEN_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps({"fp": fp, "domain": domain, "ts": time.strftime("%Y-%m-%dT%H:%M:%S")}) + "\n")
    except Exception:
        pass


def _compose_unique(profile: dict, base_seed: int, seen: set[str]) -> dict:
    s = base_seed & 0x7FFFFFFFFFFF
    m = None
    for _ in range(16):
        rng = random.Random(s)
        m = compose(profile, rng)
        fp = _fingerprint(m)
        if fp not in seen:
            m["seed"], m["fingerprint"] = s, fp
            return m
        s = (s * XPHONEX + 12345) & 0x7FFFFFFFFFFF
    m["seed"], m["fingerprint"] = s, _fingerprint(m)  # accept (astronomically unlikely)
    return m


# ── copy assembly ────────────────────────────────────────────────────────────────
def _build_copy(profile: dict, manifest: dict, use_llm: bool) -> dict:
    vert = VERTICAL_DEFAULTS.get(profile.get("vertical", "generic"), VERTICAL_DEFAULTS["generic"])
    name = profile["name"]
    city = profile.get("city", "")
    label = profile.get("vertical_label") or vert["label"]  # real trade if outside the 7 known verticals
    where = f" in {city}" if city else ""
    domain = profile["domain"]
    n_serv = manifest["knobs"]["services_count"]
    verified_only = (
        "Do not invent years in business, ratings, reviews, licenses, insurance, awards, "
        "guarantees, same-day service, prices, family ownership, or specific credentials."
    )

    headline = _slot(
        f"Write a short punchy website hero headline (max 8 words) for {name}, a {label} business{where}. "
        f"{verified_only} No quotes, no period, plain text only.",
        vert["headline"], 64, use_llm=use_llm, strip_end_punct=True, max_tokens=24,
    )
    subhead = _slot(
        f"Write one warm website hero subheading sentence (max 18 words) for {name}, a {label} business{where}. "
        f"{verified_only} Plain text only, no quotes.",
        vert["subhead"], 150, use_llm=use_llm, max_tokens=48,
    )
    about_body = _slot(
        f"Write a short 'about us' paragraph (2 to 3 sentences, max 50 words) for {name}, a {label} business{where}. "
        f"{verified_only} Friendly, trustworthy, concrete. Plain text, no quotes.",
        vert["about"], 340, use_llm=use_llm, max_tokens=110,
    )
    cta_button = _slot(
        f"Write a 2 to 4 word call-to-action button label for a {label} business. Plain text only, no quotes.",
        vert["cta"], 26, use_llm=use_llm, strip_end_punct=True, max_tokens=10,
    )

    icons = vert["icons"]
    services = []
    for i, (sname, sblurb) in enumerate(vert["services"][:n_serv]):
        blurb = _slot(
            f"Write one sentence (max 16 words) describing the '{sname}' service from a {label} business. "
            f"{verified_only} Plain text, no quotes.",
            sblurb, 130, use_llm=use_llm, max_tokens=40,
        )
        services.append((_html.escape(sname), blurb, icons[i % len(icons)]))

    name_e = _html.escape(name)
    return {
        "name": name_e,
        "city": _html.escape(city),
        "eyebrow": _html.escape(label),
        "headline": headline,
        "subhead": subhead,
        "about_eyebrow": "About us",
        "about_title": _html.escape(f"Why {name} is your local choice"),
        "about_body": about_body,
        "services_eyebrow": "What we do",
        "services_title": "Services built around you",
        "services": services,
        "proof_eyebrow": "Trust-ready",
        "proof_title": "Built for real proof, not fake claims",
        "proof_points": [(_html.escape(a), _html.escape(b)) for a, b in PROOF_POINTS],
        "gallery_eyebrow": "Our work",
        "gallery_title": "Recent work",
        "gallery_count": manifest["knobs"]["gallery_count"],
        "faq_eyebrow": "FAQ",
        "faq_title": "Questions, answered",
        "faqs": FAQS[: manifest["knobs"]["faq_count"]],
        "cta_title": _html.escape(f"Ready to get started with {name}?"),
        "cta_sub": "Reach out and a member of our team will get right back to you.",
        "cta_button": cta_button,
        "contact_eyebrow": "Get in touch",
        "contact_title": "Let's talk",
        "contact_body": "Call, email, or send a message and we'll respond quickly during business hours.",
        "phone": _html.escape(profile.get("phone") or "Add your phone number"),
        "email": _html.escape(profile.get("email") or f"Add your real @{domain} email"),
        "hours": _html.escape(profile.get("hours") or "Add your business hours"),
        "facts": [_html.escape(f) for f in FACTS],
        "stats": [("Real", "Proof slots"), ("Clear", "Contact path"), ("Safe", "Copy claims")],
        "year": time.strftime("%Y"),
    }


# ── render ────────────────────────────────────────────────────────────────────────
def _tokens(manifest: dict) -> dict:
    pal = PALETTES[manifest["palette"]]
    fp = FONT_PAIRS[manifest["fonts"]]
    k = manifest["knobs"]
    t = dict(pal)
    t.update({
        "on-primary": _on(pal["primary"]),
        "on-accent": _on(pal["accent"]),
        "radius": f'{k["radius"]}px',
        "shadow": _SHADOW_CSS[k["shadow_depth"]],
        "maxw": k["container_width"],
        "pad-y": f'{SPACING_SCALES[k["spacing_scale"]]}px',
        "align": k["section_align"],
        "font-head": fp["head"],
        "font-body": fp["body"],
        "img-filter": _IMG_FILTER[k["image_treatment"]],
        "image_treatment": k["image_treatment"],
    })
    return t


def _root_vars(t: dict) -> str:
    keys = ["bg", "surface", "ink", "muted", "primary", "accent", "on-primary", "on-accent",
            "radius", "shadow", "maxw", "pad-y", "align", "font-head", "font-body", "img-filter"]
    decls = ";".join(f"--{k}:{t[k]}" for k in keys)
    return ":root{" + decls + "}"


def _render(manifest: dict, copy: dict) -> str:
    t = _tokens(manifest)
    brng = random.Random(manifest.get("seed", 0) ^ 0xBA117E)
    t["banner"] = _svg_banner(t, brng)
    t["tiles"] = [_svg_tile(t, brng) for _ in range(6)]

    k = manifest["knobs"]
    css = (
        _root_vars(t) + _BASE_CSS + _BTN_CSS[k["button_shape"]] + _CARD_CSS[k["card_style"]]
    )
    if k["button_shape"] != "ghost":
        css += _GHOST_FALLBACK

    body = []
    for name in manifest["order"]:
        fn = SECTIONS[name][manifest["variants"][name]]
        body.append(fn(t, copy))

    _loc = f' in {copy["city"]}' if copy.get("city") else ""
    title = f'{manifest["name"]} — {copy["eyebrow"]}{_loc}'
    desc = copy["subhead"]
    return (
        "<!doctype html>\n"
        '<html lang="en">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width,initial-scale=1">\n'
        f"<title>{_html.escape(title)}</title>\n"
        f'<meta name="description" content="{desc}">\n'
        "<style>" + css + "</style>\n"
        "</head>\n<body>\n" + "\n".join(body) + "\n</body>\n</html>\n"
    )


# ── validation: well-formedness + required tags/sections present ─────────────────
class _Checker(HTMLParser):
    def __init__(self):
        super().__init__()
        self.counts: dict[str, int] = {}
        self.section_ids: set[str] = set()

    def handle_starttag(self, tag, attrs):
        self.counts[tag] = self.counts.get(tag, 0) + 1
        if tag == "section":
            for k, v in attrs:
                if k == "id" and v:
                    self.section_ids.add(v)


def _validate(html_str: str, manifest: dict) -> bool:
    if not html_str or "<html" not in html_str or "<body" not in html_str:
        return False
    if "<style" not in html_str or "</style>" not in html_str:
        return False
    if "http://" in html_str or "https://www" in html_str:
        # only allow the xmlns declaration (not a fetchable resource)
        leftover = html_str.replace('xmlns="http://www.w3.org/2000/svg"', "")
        if "http://" in leftover or "https://" in leftover:
            return False
    p = _Checker()
    try:
        p.feed(html_str)
        p.close()
    except Exception:
        return False
    for tag in ("html", "head", "body"):
        if p.counts.get(tag, 0) < 1:
            return False
    for name in manifest["order"]:
        if f"sec-{name}" not in p.section_ids:
            return False
    return True


# ── public API ───────────────────────────────────────────────────────────────────
def _build(profile: dict, *, write_seen: bool = True) -> tuple[str, dict]:
    p = _enrich(profile)
    use_llm = p["use_llm"]
    p["_ai_palette"] = _ai_theme_index(p["vertical"], p["name"], use_llm)

    base_seed = p.get("seed")
    if base_seed is None:
        base_seed = random.getrandbits(46)
    base_seed = int(base_seed)

    seen = _load_seen()
    manifest = _compose_unique(p, base_seed, seen)
    copy = _build_copy(p, manifest, use_llm)
    html_str = _render(manifest, copy)

    attempts = 0
    while not _validate(html_str, manifest) and attempts < 4:
        manifest = _compose_unique(p, manifest["seed"] + 1, seen)
        copy = _build_copy(p, manifest, use_llm)
        html_str = _render(manifest, copy)
        attempts += 1

    if write_seen:
        _append_seen(manifest["fingerprint"], p["domain"])
    return html_str, manifest


def generate_site(profile: dict) -> str:
    """Return a standalone, self-contained, visibly-randomized HTML site for a profile."""
    return _build(profile)[0]


def generate_mockup(prospect: dict) -> dict:
    """Build a per-prospect rebuild mockup. Writes <domain>.html + sidecar manifest.

    Returns {path, url, seed, fingerprint, manifest}. No sends, no outward calls
    beyond the local LLM. The file is served by serve_landing at /mockups/<domain>.html.
    """
    profile = {
        "domain": prospect.get("domain") or prospect.get("url"),
        "vertical": prospect.get("vertical"),
        "name": prospect.get("name"),
        "city": prospect.get("city"),
        "broken_findings": prospect.get("reasons") or prospect.get("broken_findings"),
        "html": prospect.get("html"),
        "seed": prospect.get("design_seed"),
    }
    html_str, manifest = _build(profile)
    # Validate BEFORE writing — a broken/empty mockup must never become an outreach proof
    # link ("preview your rebuilt site" → garbage page is worse than no email at all).
    valid = _validate(html_str, manifest)
    if not valid:
        return {"valid": False, "path": None, "url": None,
                "seed": manifest.get("seed"), "fingerprint": manifest.get("fingerprint"),
                "manifest": manifest}
    safe = _norm_domain(profile["domain"]).replace("/", "_") or "site"
    MOCKUP_DIR.mkdir(parents=True, exist_ok=True)
    out = MOCKUP_DIR / f"{safe}.html"
    out.write_text(html_str, encoding="utf-8")
    (MOCKUP_DIR / f"{safe}.manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    return {
        "valid": True,
        "path": str(out),
        "url": f"/mockups/{safe}.html",
        "seed": manifest["seed"],
        "fingerprint": manifest["fingerprint"],
        "manifest": manifest,
    }


def _cli(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Generate bespoke single-page business sites.")
    ap.add_argument("--domain", default="brightpathplumbing.com")
    ap.add_argument("--vertical", default=None, help="plumber/hvac/restaurant/salon/landscaping/dentist/generic")
    ap.add_argument("--city", default="Austin, TX")
    ap.add_argument("--name", default=None)
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--count", type=int, default=2, help="how many sample sites to emit")
    ap.add_argument("--no-llm", action="store_true", help="skip the LLM; use the deterministic copy bank")
    ap.add_argument("--out", default=str(SAMPLE_DIR))
    args = ap.parse_args(argv)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    if args.no_llm:
        os.environ["SITE_GEN_NO_LLM"] = "1"

    fps = []
    for i in range(args.count):
        profile = {"domain": args.domain, "vertical": args.vertical, "city": args.city, "name": args.name}
        if args.seed is not None:
            profile["seed"] = args.seed + i
        html_str, manifest = _build(profile)
        ok = _validate(html_str, manifest)
        path = out_dir / f"sample_{i + 1}.html"
        path.write_text(html_str, encoding="utf-8")
        fps.append(manifest["fingerprint"])
        pal = PALETTES[manifest["palette"]]["name"]
        font = FONT_PAIRS[manifest["fonts"]]["name"]
        print(f"[{i + 1}] {path}  valid={ok}  fp={manifest['fingerprint']}  "
              f"palette={pal} font={font} order={'>'.join(manifest['order'])}")
    if len(fps) > 1:
        print("distinct fingerprints:", len(set(fps)), "of", len(fps),
              "->", "DIFFER" if len(set(fps)) == len(fps) else "COLLISION")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
