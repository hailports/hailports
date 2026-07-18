#!/usr/bin/env python3
"""GEO result-page publisher — the viral SEO surface for hailports.com/ai-visibility/<brand-city>.

This is the auto-indexed, screenshot-bait public page: the A-F letter grade, the competitor
leaderboard, an embeddable "AI Visibility: F" badge (backlink), LocalBusiness JSON-LD, and the
$39 unlock CTA. Forked from the pSEO auto-publish + sitemap pipeline (agents/hailports_site.py,
tools/prog_seo_factory.py) that already serves 271 pages.

STAGED OFF BY DEFAULT. Pages render to a STAGING dir and are NOT added to the live sitemap and
NOT deployed unless GEO_PUBLISH_ENABLED=1. Going public is gated on the PII/anonymity audit
(wf_3efbd094) clearing + owner go. Until then this only writes local files for review.

  python3 -m core.geo_result_page --demo                 # render 1 sample page to staging
  GEO_PUBLISH_ENABLED=1 python3 -m core.geo_result_page --rebuild-sitemap   # (gated) live
"""
from __future__ import annotations

import argparse
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(os.environ.get("CLAUDE_STACK_DIR", Path(__file__).resolve().parents[1]))
STAGING_DIR = ROOT / "data" / "hustle" / "geo_pages_staging"
PUBLIC_BASE = "https://hailports.com/ai-visibility"
DOMAIN = "hailports.com"


def publish_enabled() -> bool:
    return os.environ.get("GEO_PUBLISH_ENABLED", "") == "1"


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(s or "").lower()).strip("-")


def page_slug(business: str, city: str) -> str:
    return _slug(f"{business}-{city}")


_CSS = (
    "body{font-family:-apple-system,Segoe UI,Arial,sans-serif;max-width:760px;margin:0 auto;"
    "padding:28px 18px;line-height:1.55;color:#15171a}"
    ".grade{font-size:4rem;font-weight:800;line-height:1;margin:6px 0}"
    ".f{color:#c62828}.d{color:#ef6c00}.c{color:#f9a825}.b{color:#2e7d32}.a{color:#1b5e20}"
    "table{border-collapse:collapse;width:100%;margin:14px 0}td,th{border:1px solid #e6e8eb;padding:7px 10px;text-align:left;font-size:14px}"
    ".badge{display:inline-block;border:2px solid #c62828;border-radius:8px;padding:6px 12px;font-weight:800;color:#c62828}"
    ".cta{display:block;text-align:center;background:#111;color:#fff;padding:13px;border-radius:11px;text-decoration:none;font-weight:700;margin:18px 0}"
    ".disc{background:#fffbe6;border:1px solid #f0d060;border-radius:8px;padding:8px 14px;font-size:13px;margin:14px 0}"
)


def badge_embed(business: str, city: str, grade: str) -> str:
    url = f"{PUBLIC_BASE}/{page_slug(business, city)}"
    return (f'<a href="{url}" style="display:inline-block;border:2px solid #c62828;border-radius:8px;'
            f'padding:6px 12px;font:700 13px sans-serif;color:#c62828;text-decoration:none">'
            f'AI Visibility: {grade} — check yours</a>')


def _src_label(scan: dict) -> str:
    """Honest source label: name real hosted assistants as-is; collapse internal backend tokens to
    'open AI models' so a page never advertises a qwen/searx string. Mirrors geo_fix_kit._src_label."""
    out: list[str] = []; seen: set[str] = set()
    for s in (scan.get("sources") or []):
        sl = str(s).lower()
        lbl = ("open AI models" if (sl in ("ollama", "searx", "stub")
               or sl.startswith(("qwen", "llama", "mistral", "gemma")) or ":" in sl) else str(s))
        if lbl not in seen:
            seen.add(lbl); out.append(lbl)
    return ", ".join(out) or "open AI models"


def render_page(scan: dict, *, checkout_url: str = "#", domain: str = "") -> str:
    business, city, category = scan["business"], scan["city"], scan["category"]
    grade = scan["grade"]
    ap = scan.get("appearances", 0); n = scan.get("prompts_run", 0)
    is_sim = bool(scan.get("is_simulation", True))
    sources = _src_label(scan)
    # LEGAL: this is a public/staged SEO page. Never print competitor business NAMES as fact (model
    # hallucination + FTC/defamation risk, even on the hosted path) — COUNT only. State the real
    # models plainly only when is_simulation is False; otherwise frame strictly as a simulation.
    # Rebuild gaps from counts too — the stored scan['gaps'] embed competitor names. Mirrors
    # core.geo_fix_kit.build_preview_html and products.self_serve.app._geo_render.
    n_rivals = len([c for c in (scan.get("leaderboard") or []) if c.get("name")])
    kicker = "AI VISIBILITY REPORT" if not is_sim else "AI VISIBILITY SIMULATION"
    asked = (f"We asked {sources}" if not is_sim
             else f"In an independent simulation through {sources}, we ran")
    rivals_clause = (f"{n_rivals} other {category} businesses" if n_rivals
                     else f"other {category} businesses")
    if ap == 0:
        verdict = (f"{asked} the {n} questions a buyer asks for the best {category} in {city}. "
                   f"<strong>{business} was named 0 times</strong>"
                   + (f" — while {n_rivals} other {category} businesses were named instead" if n_rivals else "")
                   + f". Visibility score: <strong>{scan['visibility_score']}/100</strong>.")
    else:
        verdict = (f"{asked} the {n} questions a buyer asks for the best {category} in {city}. "
                   f"<strong>{business} appeared in only {ap}/{n}</strong> — a buyer who asks once "
                   f"usually won't see it. Visibility score: <strong>{scan['visibility_score']}/100</strong>.")
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
    gaps = "".join(f"<li>{g}</li>" for g in gaps_raw)
    method = (f"scannerapp asked {sources}; not affiliated with {business}." if not is_sim
              else f"Independent simulation using {sources}; not affiliated with {business}.")
    meta_desc = (f"{asked} the best {category} in {city}. {business} scored {grade}. See the gap and "
                 f"how to get cited.")
    jsonld = {
        "@context": "https://schema.org", "@type": "Article",
        "headline": f"{business} AI Visibility Grade: {grade} for \"{category} in {city}\"",
        "datePublished": datetime.now(timezone.utc).date().isoformat(),
        "publisher": {"@type": "Organization", "name": "Hailports"},
    }
    return f"""<!DOCTYPE html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>{business} AI Visibility: Grade {grade} ({category} in {city})</title>
<meta name=description content="{meta_desc}">
<link rel=canonical href="{PUBLIC_BASE}/{page_slug(business, city)}">
<style>{_CSS}</style>
<script type="application/ld+json">{json.dumps(jsonld)}</script></head><body>
<p style="color:#777;font-size:13px">{kicker} · "{category} in {city}"</p>
<h1>{business}: AI Visibility Grade</h1>
<div class="grade {grade.lower()}">{grade}</div>
<p>{verdict}</p>

<h2>Why {business} gets skipped</h2><ul>{gaps}</ul>

<h2>Embed your badge</h2>
<p>{badge_embed(business, city, grade)}</p>
<div class="disc">{method} Grades reflect AI model outputs at the time of the scan and vary by model and date. Informational only.</div>

<a class="cta" href="{checkout_url}">Get the $39 Fix Kit — get cited by AI</a>
</body></html>"""


def stage_page(scan: dict, *, checkout_url: str = "#", domain: str = "", base: Path = STAGING_DIR) -> Path:
    """Render the page to the STAGING dir (never live). Returns the file path."""
    base.mkdir(parents=True, exist_ok=True)
    p = base / f"{page_slug(scan['business'], scan['city'])}.html"
    p.write_text(render_page(scan, checkout_url=checkout_url, domain=domain), encoding="utf-8")
    return p


def rebuild_sitemap(base: Path = STAGING_DIR) -> Path:
    """Build sitemap.xml from staged pages. Pings search engines ONLY if publish_enabled()."""
    base.mkdir(parents=True, exist_ok=True)
    pages = sorted(p.name for p in base.glob("*.html"))
    today = datetime.now(timezone.utc).date().isoformat()
    urls = "".join(
        f"<url><loc>{PUBLIC_BASE}/{name[:-5]}</loc><lastmod>{today}</lastmod>"
        f"<changefreq>weekly</changefreq></url>"
        for name in pages
    )
    sm = (f'<?xml version="1.0" encoding="UTF-8"?>'
          f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>')
    out = base / "sitemap.xml"
    out.write_text(sm, encoding="utf-8")
    if publish_enabled():
        _ping_search_engines()  # gated — only fires when owner flips GEO_PUBLISH_ENABLED=1
    return out


def _ping_search_engines() -> None:
    """IndexNow / sitemap ping — STAGED OFF; runs only when publish is enabled + audit cleared."""
    if not publish_enabled():
        return
    import urllib.request
    for ping in (
        f"https://www.google.com/ping?sitemap={PUBLIC_BASE}/sitemap.xml",
        f"https://www.bing.com/ping?sitemap={PUBLIC_BASE}/sitemap.xml",
    ):
        try:
            urllib.request.urlopen(ping, timeout=10)
        except Exception:
            pass


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--demo", action="store_true", help="render one sample page to staging")
    ap.add_argument("--rebuild-sitemap", action="store_true")
    args = ap.parse_args(argv)

    if args.demo:
        try:
            from core.geo_visibility_probe import score_business, _stub_market
            scan = score_business("Acme Dental", "Austin TX", "dentist", offline_stub=_stub_market())
        except Exception as e:
            print(f"demo scan failed: {e}")
            return 1
        p = stage_page(scan, checkout_url="https://hailports.com/go/geo-fix-kit")
        print(f"staged (NOT live): {p}")
        print(f"publish_enabled={publish_enabled()}  (set GEO_PUBLISH_ENABLED=1 + audit-green to go live)")
    if args.rebuild_sitemap:
        sm = rebuild_sitemap()
        print(f"sitemap -> {sm}  (pinged={publish_enabled()})")
    if not (args.demo or args.rebuild_sitemap):
        ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
