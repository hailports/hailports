#!/usr/bin/env python3
"""Scrub the programmatic SEO pages to 100% Hailports and render the published set.

Source (READ-ONLY): data/hustle/seo_pages/*.html + data/hustle/seo_pages/generated/*.html
Output (flat, served at hailports.com/guides/<slug>): data/hustle/seo_pages_published/

Every scannerapp.dev / docsapp link -> hailports.com (or the on-page capture CTA), every
"scannerapp"/"docsapp" text mention -> Hailports, /intel/ paths -> /guides/, and a single-step
{email + website} capture form is injected before </body>. Idempotent + resumable: re-running
overwrites the published copies; partial runs can be re-run safely.

    PYTHONPATH=. .venv/bin/python tools/scrub_seo_pages_hailports.py
"""
from __future__ import annotations

import re
import sys
from datetime import date
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
from tools.responsive_baseline import ensure_responsive  # noqa: E402

SRC = BASE / "data" / "hustle" / "seo_pages"
OUT = BASE / "data" / "hustle" / "seo_pages_published"

CAPTURE = (
    '<div id="hp-capture" style="max-width:640px;margin:34px auto 8px;padding:22px 22px 18px;'
    'background:#1a2230;border:1px solid #2c3644;border-radius:10px;'
    'font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#e7e2d6">'
    '<div style="font:700 18px/1.3 Georgia,\'Times New Roman\',serif;color:#e7e2d6;margin-bottom:6px">'
    'Get this week’s list — free</div>'
    '<p style="margin:0 0 14px;color:#9aa3ad;font-size:14px">Tell us where to send it. You’ll get the '
    'current buyer-intent list for your space, plus a short note on how to read it. '
    'No spam, unsubscribe in one click.</p>'
    '<form id="hp-form" onsubmit="hpSubmit(event)">'
    '<input id="hp-email" type="email" required placeholder="user@example.com" '
    'style="width:100%;box-sizing:border-box;padding:11px;margin:0 0 8px;border-radius:7px;'
    'border:1px solid #2c3644;background:#0f1620;color:#e7e2d6;font-size:15px">'
    '<input id="hp-site" type="text" placeholder="yoursite.com (optional)" '
    'style="width:100%;box-sizing:border-box;padding:11px;margin:0 0 10px;border-radius:7px;'
    'border:1px solid #2c3644;background:#0f1620;color:#e7e2d6;font-size:15px">'
    '<button type="submit" style="width:100%;padding:12px;border:0;border-radius:7px;'
    'background:#d98a3d;color:#1a2230;font-weight:700;font-size:15px;cursor:pointer">'
    'Send me the list →</button></form>'
    '<p id="hp-msg" style="margin:10px 0 0;font-size:13px;color:#9aa3ad"></p></div>'
    '<script>function hpSubmit(e){e.preventDefault();'
    "var em=document.getElementById('hp-email').value.trim();"
    "var st=document.getElementById('hp-site').value.trim();"
    "var msg=document.getElementById('hp-msg');"
    "var slug=location.pathname.replace(/^\\/guides\\//,'').replace(/\\.html$/,'').replace(/\\/$/,'');"
    "msg.textContent='Sending\\u2026';"
    "fetch('https://intake.hailports.com/guides/capture',{method:'POST',headers:{'Content-Type':'application/json'},"
    "body:JSON.stringify({email:em,website:st,slug:slug})}).then(function(r){return r.json()})"
    ".then(function(d){msg.textContent=d.ok?"
    "'Got it \\u2014 check your inbox in a minute (and your spam folder, just in case).':"
    "'Hmm, that didn\\'t go through. Mind trying again?';"
    "if(d.ok){document.getElementById('hp-form').style.display='none'}})"
    ".catch(function(){msg.textContent='Network hiccup \\u2014 try again in a sec.'});}</script>"
)

# Faceless live banner injected above the capture form on all guide pages. Keep it generic:
# no hardware specs, operator footprint, exact process counts, commit claims, or origin story.
# Inline styles only (heterogeneous static pages), unique keyframe name, stream + hailports.com.
STREAM_BANNER = (
    '<a id="hp-live" href="https://www.youtube.com/channel/UC_NaJ6WEpYsNlq-CIGD8yAw/live" '
    'target="_blank" rel="noopener" style="display:flex;align-items:center;gap:16px;flex-wrap:wrap;'
    'max-width:640px;margin:34px auto 8px;padding:16px 20px;background:#1a2230;border:1px solid #2c3644;'
    'border-radius:10px;font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;'
    'text-decoration:none;color:#e7e2d6">'
    '<span style="display:inline-flex;align-items:center;gap:8px;font:700 12px/1 -apple-system,Segoe UI,Arial,sans-serif;'
    'letter-spacing:.12em;color:#ff5f56;white-space:nowrap">'
    '<span style="width:9px;height:9px;border-radius:50%;background:#ff5f56;'
    'box-shadow:0 0 0 0 rgba(255,95,86,.7);animation:hpLivePulse 1.7s infinite"></span>LIVE</span>'
    '<span style="flex:1 1 240px;min-width:200px">'
    '<span style="display:block;font:700 16px/1.3 Georgia,\'Times New Roman\',serif;color:#e7e2d6">'
    'Watch the live operations board</span>'
    '<span style="display:block;margin-top:3px;font-size:13.5px;line-height:1.45;color:#9aa3ad">'
    'an anonymized automation board streaming live. activity is bucketed, source details are cloaked, '
    'and operator-identifying details stay off the page.</span></span>'
    '<span style="display:inline-flex;align-items:center;gap:9px;font:700 14px/1 -apple-system,Segoe UI,Arial,sans-serif;'
    'color:#1a2230;background:#d98a3d;padding:11px 16px;border-radius:8px;white-space:nowrap">&#9654; Watch live</span></a>'
    '<div style="max-width:640px;margin:6px auto 0;text-align:center;'
    'font:13px/1.5 -apple-system,Segoe UI,Arial,sans-serif;color:#9aa3ad">or '
    '<a href="https://www.hailports.com/" style="color:#d98a3d;text-decoration:none">see the live board &uarr;</a></div>'
    '<style>@keyframes hpLivePulse{0%{box-shadow:0 0 0 0 rgba(255,95,86,.7)}'
    '70%{box-shadow:0 0 0 9px rgba(255,95,86,0)}100%{box-shadow:0 0 0 0 rgba(255,95,86,0)}}</style>'
)


# ICP spine — the real product ladder every published page must route to (the SAME spine
# prog_seo_factory + the freetools/programmatic/repurpose generators now emit): the free
# AI-visibility scan + the $39 geo_fix_kit Fix Kit. Injected at the publish choke point so a
# page from ANY generator — including legacy pages no longer regenerated — carries both CTAs and
# can never drift back to the phantom free-leads funnel. Deterministic, $0, idempotent.
ICP_FREE_SCAN = "https://www.hailports.com/ai-visibility/check"
ICP_FIX_KIT = "https://intake.hailports.com/buy?tier=geo_fix_kit"
ICP_SPINE = (
    '<div id="hp-icp" style="max-width:640px;margin:30px auto 8px;padding:18px 20px;'
    'background:#14171a;color:#e7e2d6;border-radius:11px;'
    'font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif">'
    '<strong style="color:#e7e2d6">Curious whether AI recommends your business?</strong> '
    'See how ChatGPT, Gemini and Perplexity answer for your category &mdash; '
    f'<a href="{ICP_FREE_SCAN}?utm_source=seo&utm_medium=guide&utm_campaign=ai_visibility_scan" '
    'style="color:#d98a3d;text-decoration:none">run the free AI-visibility scan &rarr;</a> '
    '<span style="color:#9aa3ad">then lock the fixes &middot; '
    f'<a href="{ICP_FIX_KIT}&utm_source=seo&utm_medium=guide&utm_campaign=fix_kit" '
    'style="color:#d98a3d;text-decoration:none">$39 AI-Visibility Fix Kit, one-time &rarr;</a></span></div>'
)
# Live catalog SKUs / the scan path — a page already carrying any of these routes to a real
# product, so the spine is only added where it's genuinely missing (matches seo_health's guard).
_ICP_PRESENT_RE = re.compile(
    r"ai-visibility/check|geo_fix_kit|\bfix_plan\b|ai_visibility_watch|seo_starter|site_health_watch",
    re.I)

OPERATOR_STORY_RE = re.compile(
    r"\bMac\s+mini\b|~\s*220\s+business\s+processes|"
    r"hundreds\s+of\s+business\s+processes|zero\s+humans?|0\s+humans?|"
    r"no\s+humans?\s+in\s+the\s+loop|commits\s+the\s+code|shipping\s+the\s+code|"
    r"finds\s+the\s+work|decade\s+of\s+operational\s+scar\s+tissue|"
    r"client\s+portfolios\s+worth\s+billions|faceless\s+operator|\$5B|5B\s+compan",
    re.I,
)


def scrub_operator_story(s: str) -> str:
    """Remove public-facing operator/hardware/origin-story fingerprints from guide HTML."""
    neutral = (
        "Hailports publishes anonymized buyer-intent and AI-visibility workflows with "
        "bucketed live activity, source-safe reporting, and scrubbed public metadata."
    )
    s = re.sub(
        r"one AI stack running ~220 business processes on a single Mac mini, streaming 24/7\. "
        r"it finds the (?:demand|work), builds the product, ships it, and commits the code "
        r"&mdash; zero humans in the loop\.",
        "an anonymized automation board streaming live. activity is bucketed, source details "
        "are cloaked, and operator-identifying details stay off the page.",
        s,
        flags=re.I,
    )
    s = re.sub(
        r"Hailports is an autonomous AI stack that runs real business processes end-to-end\s*[—-]\s*"
        r"outreach, reporting, ops, content, and self-healing infrastructure\s*[—-]\s*"
        r"building in public on a single Mac mini, with zero humans in the loop\.",
        neutral,
        s,
        flags=re.I,
    )
    s = re.sub(
        r"Hailports is an autonomous AI stack that runs real business processes end-to-end\s*[—-]\s*"
        r"outreach, reporting, ops, content, and self-healing infrastructure\.",
        neutral,
        s,
        flags=re.I,
    )
    s = re.sub(
        r"Built on a decade of operational scar tissue turned into software\s*[—-]\s*"
        r"now an autonomous AI runs those business processes end-to-end\.",
        "Public pages stay intentionally generic and anonymity-preserving.",
        s,
        flags=re.I,
    )
    s = re.sub(
        r"Built by an operator who spent years running operations for client portfolios worth billions\s*[—-]\s*"
        r"now an AI automates those same business processes\.",
        "Public pages stay intentionally generic and anonymity-preserving.",
        s,
        flags=re.I,
    )
    s = re.sub(
        r"Hailports is an (?:innovative )?autonomous AI stack that runs real business processes end-to-end, "
        r"covering outreach, reporting, operations, content creation, and self-healing "
        r"infrastructure\.",
        neutral,
        s,
        flags=re.I,
    )
    s = re.sub(
        r"Hailports is an autonomous AI stack designed to run real business processes end-to-end, "
        r"including outreach, reporting, operations, content creation, and self-healing infrastructure\.",
        neutral,
        s,
        flags=re.I,
    )
    s = re.sub(
        r"Hailports is an autonomous AI stack designed to run real business processes end-to-end"
        r"\s*[—-]\s*outreach, reporting, operations, content creation, and self-healing "
        r"infrastructure\s*[—-]\s*all on a single Mac mini with zero humans in the loop\.",
        neutral,
        s,
        flags=re.I,
    )
    s = re.sub(
        r"Hailports is an autonomous AI stack that runs real business processes end-to-end, "
        r"including outreach, reporting, operations, content creation, and self-healing infrastructure\.",
        neutral,
        s,
        flags=re.I,
    )
    s = re.sub(
        r"It[’']s built on a single Mac mini with zero humans in the loop, making it "
        r"incredibly efficient for startups looking to streamline their operations from day one\.",
        "It keeps the public surface focused on anonymized workflow snapshots and practical "
        "guides for teams comparing options.",
        s,
        flags=re.I,
    )
    s = re.sub(
        r"It[’']s built in public on a single Mac mini with zero humans in the loop, making it "
        r"a powerful solution for solo founders looking to streamline their operations\.",
        "It keeps the public surface focused on anonymized workflow snapshots and practical "
        "guides for teams comparing options.",
        s,
        flags=re.I,
    )
    s = re.sub(
        r"All of this can be built in public on a private infrastructure, with zero humans needed in the loop\.",
        "Public pages stay focused on anonymized workflow snapshots and practical comparison guidance.",
        s,
        flags=re.I,
    )
    s = re.sub(r"Hailports\s+\(built by Alibaba Cloud\)", "Hailports", s, flags=re.I)
    s = re.sub(r",?\s*but it requires a Mac mini setup", "", s, flags=re.I)
    s = re.sub(r"\bMac mini setup\b", "private setup", s, flags=re.I)
    # Last-mile cleanup for truncated meta descriptions or stale fragments in older generated pages.
    s = re.sub(r"\s*[—-]\s*building in public on a single Mac mini, with zero humans in the loop", "", s, flags=re.I)
    s = re.sub(r"\s*on a single Mac mini,? with zero humans in the loop", "", s, flags=re.I)
    s = re.sub(r"\s*with zero humans needed in the loop", " with source-safe public reporting", s, flags=re.I)
    s = re.sub(r"\bsingle Mac mini\b", "private infrastructure", s, flags=re.I)
    s = re.sub(r"\bone mac mini\b", "private infrastructure", s, flags=re.I)
    s = re.sub(r"\b1 Mac mini\b", "private infrastructure", s, flags=re.I)
    s = re.sub(r"\bzero humans in the loop\b", "anonymized automation-first operations", s, flags=re.I)
    s = re.sub(r"\bzero humans needed in the loop\b", "source-safe public reporting", s, flags=re.I)
    s = re.sub(r"\b0 humans in the loop\b", "source-safe public reporting", s, flags=re.I)
    s = re.sub(r"~\s*220\s+business processes", "multiple business workflows", s, flags=re.I)
    s = re.sub(r"client portfolios worth billions", "anonymized operational contexts", s, flags=re.I)
    s = re.sub(r"\bcommits the code\b", "updates the board", s, flags=re.I)
    s = re.sub(r"\bbuilding in public\b", "publishing anonymized summaries", s, flags=re.I)
    return s


def ensure_icp_spine(s: str) -> str:
    """Guarantee the page routes to the real product ladder. Idempotent (skips if hp-icp already
    present) and surgical (skips pages that already carry BOTH the free scan and a $39/SKU path)."""
    if 'id="hp-icp"' in s or "</body>" not in s:
        return s
    has_free = "ai-visibility/check" in s
    has_paid = bool(re.search(r"tier=geo_fix_kit|geo_fix_kit|\bfix_plan\b|"
                              r"ai_visibility_watch|seo_starter|site_health_watch", s, re.I))
    if has_free and has_paid:
        return s
    return s.replace("</body>", ICP_SPINE + "</body>", 1)


def scrub(s: str) -> str:
    s = scrub_operator_story(s)
    # 1. /intel/ -> /guides/  (domain-qualified, root-absolute, AND url-encoded share links;
    #    global so the generated/ subdir is flattened everywhere it appears)
    s = s.replace("https://www.scannerapp.dev/intel/generated/", "https://hailports.com/guides/")
    s = s.replace("https://scannerapp.dev/intel/generated/", "https://hailports.com/guides/")
    s = s.replace("https://www.scannerapp.dev/intel/", "https://hailports.com/guides/")
    s = s.replace("https://scannerapp.dev/intel/", "https://hailports.com/guides/")
    s = s.replace("/intel/generated/", "/guides/")
    s = s.replace("/intel/", "/guides/")
    # 2. remaining scannerapp.dev links (/sample, /go, /biz, ...) -> hailports offer (root)
    s = re.sub(r'https?://(?:www\.)?scannerapp\.dev/[^\s"\'<>)]*', 'https://hailports.com/', s)
    s = re.sub(r'https?://(?:www\.)?scannerapp\.dev\b', 'https://hailports.com', s)
    # 3. docsapp gumroad + docsapp.dev links -> hailports
    s = re.sub(r'https?://docsapp\.gumroad\.com/[^\s"\'<>)]*', 'https://hailports.com/', s)
    s = re.sub(r'https?://(?:www\.)?docsapp\.dev/[^\s"\'<>)]*', 'https://hailports.com/', s)
    s = re.sub(r'https?://(?:www\.)?docsapp\.dev\b', 'https://hailports.com', s)
    # 4. email addresses on those domains -> the hailports research inbox
    s = re.sub(r'[A-Za-z0-9._%+-]+@scannerapp\.dev', 'user@example.com', s)
    s = re.sub(r'[A-Za-z0-9._%+-]+@docsapp\.gumroad\.com', 'user@example.com', s)
    s = re.sub(r'[A-Za-z0-9._%+-]+@docsapp\.dev', 'user@example.com', s)
    # 5. bare domain text mentions
    s = s.replace("scannerapp.dev", "hailports.com")
    s = s.replace("docsapp.gumroad.com", "hailports.com")
    s = s.replace("docsapp.dev", "hailports.com")
    # 6. brand-word swaps, then a case-insensitive sweep guarantees zero tokens
    s = s.replace("scannerapp", "Hailports").replace("scannerapp", "Hailports").replace("scannerapp", "HAILPORTS")
    s = re.sub(r'scannerapp', 'hailports', s, flags=re.IGNORECASE)
    s = s.replace("docsapp", "Hailports").replace("docsapp", "Hailports").replace("docsapp", "HAILPORTS")
    s = re.sub(r'docsapp', 'hailports', s, flags=re.IGNORECASE)
    # 7. defensive: no wrong-TLD artifacts from the sweeps
    s = s.replace("hailports.dev", "hailports.com").replace("hailports.gumroad.com", "hailports.com")
    # 7a. cross-brand CHECKOUT links — a Hailports page pointing at another brand's Stripe/Gumroad
    #     checkout is a one-operator correlation leak the domain-grep deploy guard does NOT catch.
    #     Route every external checkout to the Hailports board (the page capture form is the convert).
    s = re.sub(r'https?://buy\.stripe\.com/[A-Za-z0-9]+', 'https://www.hailports.com/', s)
    s = re.sub(r'https?://[A-Za-z0-9.-]*gumroad\.com/[^\s"\'<>)]*', 'https://www.hailports.com/', s)
    # 7b. canonical host = www (apex 308-redirects to www at Cloudflare) -> no redirect chains.
    #     Only rewrites absolute https links; bare/email mentions (user@example.com) untouched.
    s = s.replace("https://hailports.com", "https://www.hailports.com")
    s = s.replace("https://www.www.hailports.com", "https://www.hailports.com")
    # 7c. Cloudflare Pages serves /guides/<slug> (extensionless) and 308-redirects the .html
    #     form to it, so a .html canonical/internal link is a redirect, not a 200. Point them at
    #     the served URL — a redirect-canonical dilutes/suppresses indexing of the whole set.
    s = re.sub(r'(https://www\.hailports\.com/guides/[A-Za-z0-9_-]+)\.html', r'\1', s)
    # 7d. Open Graph + Twitter-card completeness — older generators (programmatic_seo) emit only
    #     title/description/canonical; derive the social tags deterministically (no LLM) so EVERY
    #     deployed page ships shareable cards. Idempotent: skip if og:title already present.
    if "og:title" not in s and "</head>" in s:
        mt = re.search(r"<title>(.*?)</title>", s, re.I | re.S)
        md = re.search(r'name=["\']description["\'][^>]*content=["\'](.*?)["\']', s, re.I | re.S)
        mc = re.search(r'rel=["\']canonical["\'][^>]*href=["\'](.*?)["\']', s, re.I)
        ttl = (mt.group(1).strip() if mt else "Hailports")
        dsc = (md.group(1).strip() if md else "")
        og = (f'<meta property="og:type" content="article">'
              f'<meta property="og:site_name" content="Hailports">'
              f'<meta property="og:title" content="{ttl}">'
              f'<meta property="og:description" content="{dsc}">'
              + (f'<meta property="og:url" content="{mc.group(1)}">' if mc else "")
              + f'<meta name="twitter:card" content="summary_large_image">'
              f'<meta name="twitter:title" content="{ttl}">'
              f'<meta name="twitter:description" content="{dsc}">')
        s = s.replace("</head>", og + "</head>", 1)
    # 7e. ICP spine — every published page must route to the real product ladder (free
    #     AI-visibility scan + $39 Fix Kit), never the phantom free-leads funnel. Catches legacy
    #     pages whose generator no longer re-runs, so the published surface can't drift off-ICP.
    s = ensure_icp_spine(s)
    # 8. inject the stream banner + first-party capture form once (banner sits above the form)
    if 'id="hp-capture"' not in s and "</body>" in s:
        s = s.replace("</body>", STREAM_BANNER + CAPTURE + "</body>", 1)
    return s


# Pages that belong to a DIFFERENT faceless brand (one-person-one-brand): never publish
# them under Hailports. Token grep passes on these but they'd leak a competing brand.
# The 5 B2C/intent rival brands (redacted/redacted/redacted/redacted/redacted) were KILLed
# in the 2026-06-25 brand review (data/hustle/BRAND_REVIEW_PLAN.md) — hosting their pages on
# hailports.com tied every faceless brand to one operator (the hard-rail host leak). Excluded
# at the slug level so no generator run can ever re-publish them onto the public Hailports tree.
EXCLUDE_SLUGS = {
    "ima_about", "persona1",
    "redacted", "redacted", "redacted", "redacted", "redacted",
}
# competing-brand domains that must never appear on a Hailports page
CROSS_BRAND = ("persona1.com", "redacted.com", "redacted.com",
               "opsapp.app", "opsapp.us", "promptsite.app", "fastaiagency.com")

# OPERATOR-DOMAIN FINGERPRINT — revenue-operations / Salesforce-admin / Agentforce is the
# operator's REAL professional lane (per the anonymity hard-rail: no operator industry/domain on
# any public faceless surface). Any page whose body carries one of these is DROPPED from the
# published/dist tree at the wire, even if upstream generators or stale source pages still emit
# them — so the correlation can never reach hailports.com. Bare "hubspot"/"crm" stay allowed
# (mainstream, non-correlating); only the operator-expertise framing is blocked.
FINGERPRINT_RE = re.compile(
    r"\bsalesforce\b|\bsfdc\b|\bagentforce\b|\brevops\b|revenue operations|"
    r"\bsales ops\b|sales operations|salesforce admin|\bsf admin\b|hubspot admin|"
    r"crm consult|crm admin|\bsoql\b|apex class", re.I)


def _clean_slug(slug: str) -> str:
    slug = re.sub(r'scannerapp', 'hailports', slug, flags=re.IGNORECASE)
    slug = re.sub(r'docsapp', 'hailports', slug, flags=re.IGNORECASE)
    return slug


def _canonicalize(html: str, served: str) -> str:
    """Force canonical + og:url to the EXACT served URL (/guides/<slug>, extensionless) and
    guarantee a twitter:card. Older generators canonicalised to a root .html URL that doesn't
    serve this page — a wrong canonical de-indexes the guide, so we overwrite it deterministically."""
    if re.search(r'<link[^>]+rel=["\']canonical["\']', html, re.I):
        html = re.sub(r'<link[^>]+rel=["\']canonical["\'][^>]*>',
                      f'<link rel="canonical" href="{served}">', html, count=1, flags=re.I)
    elif "</head>" in html:
        html = html.replace("</head>", f'<link rel="canonical" href="{served}"></head>', 1)
    if re.search(r'property=["\']og:url["\']', html, re.I):
        html = re.sub(r'<meta[^>]+property=["\']og:url["\'][^>]*>',
                      f'<meta property="og:url" content="{served}">', html, count=1, flags=re.I)
    elif "</head>" in html:
        html = html.replace("</head>", f'<meta property="og:url" content="{served}"></head>', 1)
    if "twitter:card" not in html and "</head>" in html:
        html = html.replace("</head>", '<meta name="twitter:card" content="summary_large_image"></head>', 1)
    return html


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    for old in OUT.glob("*.html"):  # derived dir — clear stale before re-render
        old.unlink()
    files = sorted(SRC.glob("*.html")) + sorted((SRC / "generated").glob("*.html"))
    slugs: list[str] = []
    seen: set[str] = set()
    written = 0
    for f in files:
        slug = _clean_slug(f.stem)
        if slug in EXCLUDE_SLUGS or f.stem in EXCLUDE_SLUGS:
            continue
        if slug in seen:
            print(f"  collision skipped: {slug}", file=sys.stderr)
            continue
        try:
            out = ensure_responsive(scrub(f.read_text(encoding="utf-8")))
            out = _canonicalize(out, f"https://www.hailports.com/guides/{slug}")
        except Exception as e:
            print(f"  FAIL {f.name}: {e}", file=sys.stderr)
            continue
        low = out.lower()
        if any(b in low for b in CROSS_BRAND):
            print(f"  cross-brand skipped: {slug}", file=sys.stderr)
            continue
        if FINGERPRINT_RE.search(out):
            print(f"  operator-domain fingerprint skipped: {slug}", file=sys.stderr)
            continue
        # operator/employer IDENTITY (name + employer + subsidiaries + work-CRM objects) — the
        # cross-brand + fingerprint greps above do NOT catch these; canonical source of truth.
        from core.anon_scrub import find_identity_leaks
        _idleaks = find_identity_leaks(out)
        if _idleaks:
            print(f"  operator/employer identity skipped: {slug} {_idleaks}", file=sys.stderr)
            continue
        if OPERATOR_STORY_RE.search(out):
            print(f"  operator-story fingerprint skipped: {slug}", file=sys.stderr)
            continue
        seen.add(slug)
        (OUT / f"{slug}.html").write_text(out, encoding="utf-8")
        slugs.append(slug)
        written += 1

    # sitemap of the live hailports guide URLs only (no dead /intel entries)
    today = date.today().isoformat()
    rows = "\n".join(
        f"  <url><loc>https://www.hailports.com/guides/{s}</loc><lastmod>{today}</lastmod></url>"
        for s in sorted(slugs)
    )
    (OUT / "sitemap.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"{rows}\n</urlset>\n",
        encoding="utf-8",
    )

    # simple index page
    links = "\n".join(
        f'<li><a href="/guides/{s}">{s.replace("-", " ")}</a></li>' for s in sorted(slugs)
    )
    (OUT / "index.html").write_text(
        ensure_responsive(
            '<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            "<title>Guides | Hailports</title>"
            '<link rel="canonical" href="https://www.hailports.com/guides/">'
            "<style>body{font:16px/1.6 Georgia,serif;max-width:760px;margin:40px auto;padding:0 16px;"
            "background:#1a2230;color:#e7e2d6}a{color:#d98a3d}ul{columns:2;gap:24px}"
            "li{margin:4px 0;break-inside:avoid;font-size:14px}</style></head><body>"
            "<h1>Hailports guides</h1><p>Independent buyer-intent &amp; ops field guides. "
            "Each one ships the signal and how to read it.</p>"
            f"<ul>{links}</ul></body></html>"
        ),
        encoding="utf-8",
    )
    print(f"published {written} pages -> {OUT}")
    print(f"sitemap: {len(slugs)} urls")

    # ── assemble the Cloudflare Pages dist (served at hailports.com) ──────────────
    # root = clean Hailports landing; /guides/<slug>.html = the scrubbed pages.
    import shutil
    dist = BASE / "data" / "hustle" / "hailports_dist"
    gdir = dist / "guides"
    if gdir.exists():
        shutil.rmtree(gdir)
    gdir.mkdir(parents=True, exist_ok=True)
    for f in OUT.glob("*.html"):
        shutil.copy2(f, gdir / f.name)
    shutil.copy2(OUT / "sitemap.xml", gdir / "sitemap.xml")
    # root landing = the live build-in-public homepage (dashboard polls intake.hailports.com/live.json)
    # + the full guide internal-link grid (SEO). Single source: tools/hailports_home_template.html.
    from tools.build_hailports_home import build as _build_home
    (dist / "index.html").write_text(_build_home(), encoding="utf-8")

    # ── COMPLETE root sitemap: every deployed, indexable URL (not just /guides) ───────
    # Scans the assembled dist so it self-maintains: homepage + guides index + all guide
    # URLs + any other indexable trees (ai-visibility), excluding noindex pages (mockups),
    # 404/experiment/og, and the live dashboard. IndexNow + robots point here.
    root_urls = ["https://www.hailports.com/", "https://www.hailports.com/guides/"]
    root_urls += [f"https://www.hailports.com/guides/{s}" for s in sorted(slugs)]
    EXCL_DIRS = {"mockups", "og", "live"}
    EXCL_FILES = {"index.html", "404.html", "experiment.html"}
    for sub in sorted(p for p in dist.iterdir() if p.is_dir() and p.name not in ({"guides"} | EXCL_DIRS)):
        for f in sorted(sub.glob("*.html")):
            if f.name in EXCL_FILES:
                continue
            if "noindex" in f.read_text(encoding="utf-8", errors="ignore").lower():
                continue
            root_urls.append(f"https://www.hailports.com/{sub.name}/{f.stem}")
    root_rows = "\n".join(
        f"  <url><loc>{u}</loc><lastmod>{today}</lastmod></url>" for u in root_urls)
    (dist / "sitemap.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"{root_rows}\n</urlset>\n", encoding="utf-8")
    (dist / "robots.txt").write_text(
        "User-agent: *\nAllow: /\nSitemap: https://www.hailports.com/sitemap.xml\n",
        encoding="utf-8")
    (dist / "_headers").write_text(
        "/guides/*\n  X-Robots-Tag: index, follow\n  Cache-Control: public, max-age=3600\n",
        encoding="utf-8")
    print(f"pages dist -> {dist} (root index + guides/{len(slugs)} + sitemap + robots + _headers)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
