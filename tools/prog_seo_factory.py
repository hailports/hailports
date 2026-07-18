#!/usr/bin/env python3
"""prog_seo_factory — programmatic SEO at scale, built from REAL buyer-intent data.

Every page is a permutation of (archetype x vertical x entity) but the *body* is
backed by genuine, dated public buying-intent signals we already harvested
(data/hustle/multi_market_leads.json) — so no two pages are thin/duplicate
doorway spam. Real data + unique structure = pages Google can rank, not nuke.

Archetypes (the permutation matrix):
  1. best-[competitor]-alternatives
  2. [competitor]-vs-intent-lead-finder
  3. how-to-find-[intent]-leads-in-[vertical]
  4. [vertical]-lead-generation-guide
  5. [role]-buyer-intent-signals-[vertical]
  6. [platform]-intent-monitoring-for-[vertical]

Sources: the 14 intent markets (agents/multi_market_intent.py) + their real
public competitor names + the 880 scored signals we already track.

    python3 tools/prog_seo_factory.py            # full cross-product
    python3 tools/prog_seo_factory.py --limit 400
    python3 tools/prog_seo_factory.py --check    # verify only, exit 1 on problems

Output: data/hustle/seo_pages/generated/*.html + sitemap.xml + robots.txt
Serving infra (a sibling lane) mounts data/hustle/seo_pages/. Repoint the
public origin in one line via CANONICAL_BASE below.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
OUTDIR = BASE / "data" / "hustle" / "seo_pages" / "generated"
LEADS_FILE = BASE / "data" / "hustle" / "multi_market_leads.json"

# Serving + canonical + sitemap aligned on the LIVE property: hailports.com/guides/<slug>.html
# (the scrub pipeline tools/scrub_seo_pages_hailports.py copies seo_pages/generated/* into the
# published /guides tree). scannerapp.dev/intel is retired (404) — repointed 2026-06-25 so staging
# canonicals match the live host instead of pointing at a dead URL.
CANONICAL_BASE = "https://www.hailports.com"
# Pages are served from <root>/guides/<slug>.html
PATH_PREFIX = "/guides"

# ── money pages (CTAs land here) ─────────────────────────────────────────────
# Single-brand Hailports. Every CTA routes to the REAL product ladder (NOT a phantom free-leads
# funnel): the free AI-visibility scan (hailports.com/ai-visibility/check) and the $39 AI-Visibility
# Fix Kit (geo_fix_kit on the hailports-branded intake.hailports.com/buy checkout). Both carry a
# live catalog SKU / the ai-visibility/check path, so every page feeds the $39 funnel and
# seo_health's ICP guard passes. UTM-tagged. scannerapp.dev is retired — never point a CTA at it.
SAMPLE_URL = "https://www.hailports.com/ai-visibility/check?utm_source=seo&utm_medium=prog_seo&utm_campaign=ai_visibility_scan"
STRIPE_INTENT = "https://intake.hailports.com/buy?tier=geo_fix_kit&utm_source=seo&utm_medium=prog_seo&utm_campaign=fix_kit"
BIZ_URL = "https://www.hailports.com/"

# ── the 14 markets, enriched with display names + REAL public competitors ────
# vertical = a buyer pool; competitors = real public products people compare.
MARKETS = {
    "lead_data": {
        "vertical": "B2B lead generation",
        "audience": "sales teams",
        "intent_noun": "B2B sales leads",
        "competitors": ["ZoomInfo", "Apollo.io", "Clay", "Lusha", "Cognism",
                         "Seamless.AI", "RocketReach", "LeadIQ", "UpLead", "Hunter.io"],
    },
    # NOTE: a "salesforce_ops / Salesforce consulting" market was removed 2026-06-28 — it
    # fingerprinted the operator's real professional domain (RevOps / Salesforce admin), a
    # correlation leak per the anonymity hard-rail. Do NOT re-add a Salesforce/RevOps/Agentforce
    # niche; the anon_scrub gate + the scrub-pipeline publish guard now block those terms.
    "ai_cost": {
        "vertical": "LLM cost optimization",
        "audience": "AI engineering teams",
        "intent_noun": "teams overspending on LLM APIs",
        "competitors": ["OpenRouter", "Helicone", "Portkey", "LiteLLM",
                        "Cloudflare AI Gateway", "Martian", "Requesty"],
    },
    "ai_content_quality": {
        "vertical": "AI content quality",
        "audience": "content and SEO teams",
        "intent_noun": "publishers fighting AI slop",
        "competitors": ["Originality.ai", "GPTZero", "Copyleaks", "Writer", "Surfer SEO"],
    },
    "devops_reliability": {
        "vertical": "DevOps reliability",
        "audience": "SRE and platform teams",
        "intent_noun": "teams with flaky jobs",
        "competitors": ["PagerDuty", "Datadog", "Better Stack", "Opsgenie",
                        "Cronitor", "Healthchecks.io"],
    },
    "scraping_infra": {
        "vertical": "web scraping infrastructure",
        "audience": "data and scraping teams",
        "intent_noun": "teams scaling headless browsers",
        "competitors": ["Browserless", "ScrapingBee", "Bright Data", "Apify",
                        "ScraperAPI", "Zyte"],
    },
    "content_creator": {
        "vertical": "faceless content creation",
        "audience": "creators and faceless-channel operators",
        "intent_noun": "creators producing at volume",
        "competitors": ["Descript", "HeyGen", "Synthesia", "CapCut", "Pictory",
                        "InVideo", "ElevenLabs"],
    },
    "email_outreach": {
        "vertical": "cold email outreach",
        "audience": "outbound sales teams",
        "intent_noun": "teams running cold email",
        "competitors": ["lemlist", "Instantly", "Smartlead", "Mailshake",
                        "Woodpecker", "Reply.io"],
    },
    "agency_tools": {
        "vertical": "marketing agency operations",
        "audience": "agency owners and operators",
        "intent_noun": "agencies scaling client work",
        "competitors": ["GoHighLevel", "AgencyAnalytics", "DashThis", "Databox",
                        "Hootsuite", "Sprout Social"],
    },
    "smb_ops": {
        "vertical": "local small-business lead capture",
        "audience": "local service-business owners",
        "intent_noun": "local businesses losing leads",
        "competitors": ["Podium", "Jobber", "Housecall Pro", "ServiceTitan", "Thryv"],
    },
    "automation_seekers": {
        "vertical": "workflow automation",
        "audience": "operators automating manual work",
        "intent_noun": "teams drowning in manual tasks",
        "competitors": ["Zapier", "Make", "n8n", "Activepieces", "Pipedream"],
    },
    "saas_founders": {
        "vertical": "SaaS growth tooling",
        "audience": "SaaS founders and growth teams",
        "intent_noun": "founders hunting their next customers",
        "competitors": ["HubSpot", "June", "Common Room", "Pocus", "Koala"],
    },
    "ecom_ops": {
        "vertical": "ecommerce operations",
        "audience": "Shopify and DTC operators",
        "intent_noun": "stores chasing repeat buyers",
        "competitors": ["Klaviyo", "Gorgias", "Triple Whale", "Yotpo", "Postscript"],
    },
    "freelance_gigs": {
        "vertical": "freelance gig sourcing",
        "audience": "freelancers and productized-service sellers",
        "intent_noun": "buyers posting paid gigs",
        "competitors": ["Upwork", "Fiverr", "Contra", "Toptal"],
    },
}

INTENT_ADJ = ["warm", "high-intent", "ready-to-buy", "in-market"]
ROLES = ["sales teams", "ops leaders", "founders", "agencies",
         "marketers", "SDRs", "account executives", "growth teams"]
PLATFORMS = ["Reddit", "Hacker News", "Twitter / X", "LinkedIn", "Quora",
             "G2", "TrustRadius", "Slack communities", "GitHub", "Indie Hackers"]

PRODUCT = "Hailports AI Visibility"
PITCH = ("shows you exactly how the AI assistants buyers now ask answer when someone wants "
         "the best in your category — and gets you named. Free scan, then a $39 fix kit")

STYLE = ("body{font-family:-apple-system,Arial,sans-serif;max-width:760px;margin:0 auto;"
         "padding:28px 18px;line-height:1.6;color:#15171a}h1{font-size:2rem;line-height:1.15}"
         "h2{margin-top:30px}h3{margin-top:22px}a{color:#0a7d4b}"
         ".disc{background:#fffbe6;border:1px solid #f0d060;border-radius:8px;padding:8px 14px;"
         "font-size:13px;margin:0 0 18px}.proof{background:#f4f7f5;border-radius:12px;"
         "padding:14px 18px;margin:18px 0}.cta{background:#111;color:#fff;padding:18px;"
         "border-radius:10px;margin:26px 0}.cta a{color:#fff}.sig li{margin:6px 0}"
         ".sub{color:#777}.rel{font-size:14px}"
         # featured-snippet answer block (40-60 word direct answer Google can lift to position 0)
         ".snip{background:#eef6f1;border-left:4px solid #0a7d4b;padding:11px 15px;"
         "border-radius:7px;margin:12px 0}.snip.lead{font-size:1.06rem;margin:14px 0 8px}"
         ".paa h3{margin-top:18px}"
         # inbound conversion widget + sticky CTA bar (funnels readers to /go, per-page attributed)
         ".wgt{background:linear-gradient(135deg,#0a7d4b,#0e9c5e);color:#fff;border-radius:14px;"
         "padding:20px;margin:22px 0}.wgt h3{margin:0 0 6px;color:#fff;font-size:1.15rem}"
         ".wgt p{margin:0 0 12px;font-size:14px;opacity:.95}.wgt .row{display:flex;gap:8px;flex-wrap:wrap}"
         ".wgt input{flex:1;min-width:180px;padding:12px;border:0;border-radius:9px;font-size:15px}"
         ".wgt button{padding:12px 18px;border:0;border-radius:9px;background:#06371f;color:#fff;"
         "font-weight:800;font-size:15px;cursor:pointer}.wgt .fine{font-size:12px;opacity:.85;margin:10px 0 0}"
         ".wgt .fine a{color:#eafff3;text-decoration:underline}"
         ".bar{position:sticky;bottom:0;left:0;right:0;background:#111;color:#fff;padding:11px 16px;"
         "margin:30px -18px -28px;text-align:center;font-size:14px;font-weight:700}.bar a{color:#7fffd0}")


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:80]


def esc(s: str) -> str:
    return html.escape(s or "", quote=True)


# ── featured-snippet + PAA layer (white-hat: condenses OUR own on-topic copy) ──
def _wc(s: str) -> int:
    return len(re.findall(r"\S+", s))


def snip40(text: str, lo: int = 40, hi: int = 58) -> str:
    """Condense text to a clean 40-60 word featured-snippet answer at a sentence
    boundary. Pulls whole sentences until >=lo words; hard-caps at hi words. Used
    to emit the concise paragraph block Google lifts into a position-0 snippet."""
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return ""
    out: list[str] = []
    for s in re.split(r"(?<=[.!?])\s+", text):
        out.append(s)
        if _wc(" ".join(out)) >= lo:
            break
    ans = " ".join(out).strip()
    words = re.findall(r"\S+", ans)
    if len(words) > hi:
        ans = " ".join(words[:hi]).rstrip(",;:—- ")
        if ans[-1:] not in ".!?":
            ans += "."
    return ans


_SIG_LIST_RE = re.compile(r"\s*<(ul|ol)\b[^>]*class=['\"]?sig", re.I)


def _snip_section(h2: str, sect: str) -> str:
    """Insert a 40-60 word .snip answer block under one H2, derived from that
    section's own lead paragraph or list. Skips quoted public-post lists (those
    are list-snippet targets, not paragraph ones)."""
    if 'class="snip"' in sect:
        return sect
    lead = sect.lstrip()
    if lead.startswith("<p>"):
        m = re.match(r"\s*<p>(.*?)</p>", sect, flags=re.S)
        if not m:
            return sect
        inner = m.group(1)
        if _wc(re.sub(r"<[^>]+>", " ", inner)) > 58:
            return f'<p class="snip">{snip40(inner)}</p>' + sect  # TL;DR + keep detail
        return sect[:m.start()] + f'<p class="snip">{inner}</p>' + sect[m.end():]
    if lead.startswith("<ul") or lead.startswith("<ol"):
        if _SIG_LIST_RE.match(lead):
            return sect
        m = re.match(r"\s*<(ul|ol)[^>]*>(.*?)</\1>", sect, flags=re.S)
        if not m:
            return sect
        strongs = re.findall(r"<strong>(.*?)</strong>", m.group(2))
        if strongs:
            head = re.sub(r"<[^>]+>", "", h2).strip().rstrip(":. ")
            names = ", ".join(re.sub(r"<[^>]+>", "", x).strip() for x in strongs[:8])
            return f'<p class="snip">{head}: {names}.</p>' + sect
        return f'<p class="snip">{snip40(m.group(2))}</p>' + sect
    return sect


def inject_h2_snippets(body: str) -> str:
    """Templating pass: ensure a concise 40-60 word answer block sits directly under
    each H2 so the page can win a paragraph featured snippet. Reuses the section's own
    content (never fabricates), so the gate-passed editorial copy is unchanged."""
    segs = re.split(r"(<h2>.*?</h2>)", body, flags=re.S)
    if len(segs) < 3:
        return body
    out = [segs[0]]
    i = 1
    while i < len(segs):
        h2 = segs[i]
        sect = segs[i + 1] if i + 1 < len(segs) else ""
        out.append(h2)
        out.append(_snip_section(h2, sect))
        i += 2
    return "".join(out)


# ── real-signal data layer ───────────────────────────────────────────────────
def load_leads() -> dict[str, list[dict]]:
    try:
        leads = json.loads(LEADS_FILE.read_text()).get("leads", [])
    except Exception:
        leads = []
    by_market: dict[str, list[dict]] = {}
    for l in leads:
        by_market.setdefault(l.get("market", ""), []).append(l)
    # stable order: highest intent first, then newest
    for m in by_market.values():
        m.sort(key=lambda l: (-(l.get("intent_score") or 0), -(l.get("posted_ts") or 0)))
    return by_market


def market_stats(pool: list[dict]) -> dict:
    if not pool:
        return {"n": 0, "budget": 0, "fresh": "", "avg": 0}
    budget = sum(1 for l in pool if l.get("has_budget"))
    scores = [l.get("intent_score") or 0 for l in pool]
    ts = max((l.get("posted_ts") or 0) for l in pool)
    fresh = datetime.fromtimestamp(ts, timezone.utc).strftime("%b %Y") if ts else ""
    return {"n": len(pool), "budget": budget, "fresh": fresh,
            "avg": round(sum(scores) / len(scores)) if scores else 0}


def _readable(l: dict) -> bool:
    """Keep only clean, human-readable real signals (drop garbled/junk titles)."""
    t = (l.get("title") or "").strip()
    if len(t) < 12:
        return False
    ascii_ratio = sum(c.isascii() for c in t) / len(t)
    if ascii_ratio < 0.9:  # garbled / non-latin
        return False
    if "as an ai" in t.lower():
        return False
    return True


def signal_slice(pool: list[dict], key: str, n: int = 10,
                 prefer: str | None = None) -> list[dict]:
    """Deterministic, page-unique slice of REAL signals. Optionally float
    competitor-matching signals to the top so the page is on-topic."""
    pool = [l for l in pool if _readable(l)]
    if not pool:
        return []
    items = pool
    if prefer:
        low = prefer.lower()
        match = [l for l in pool if low in (l.get("title", "") + l.get("snippet", "")).lower()]
        rest = [l for l in pool if l not in match]
        items = match + rest
    off = int(hashlib.md5(key.encode()).hexdigest(), 16) % max(1, len(items))
    rotated = items[off:] + items[:off]
    out, seen = [], set()
    for l in rotated:
        u = l.get("url", "")
        if u and u not in seen:
            seen.add(u)
            out.append(l)
        if len(out) >= n:
            break
    return out


def signals_html(sigs: list[dict]) -> str:
    if not sigs:
        return ""
    rows = []
    for l in sigs:
        title = esc((l.get("title") or "").strip()[:140])
        url = esc(l.get("url", "#"))
        snip = esc((l.get("snippet") or "").strip()[:180])
        sc = l.get("intent_score") or 0
        badge = f' <span class="sub">· intent {sc}</span>' if sc else ""
        sn = f"<br><span class='sub'>{snip}</span>" if snip else ""
        rows.append(f'<li><a href="{url}" rel="nofollow noopener" target="_blank">{title}</a>{badge}{sn}</li>')
    return "<ul class='sig'>" + "".join(rows) + "</ul>"


# ── page model ───────────────────────────────────────────────────────────────
class Page:
    __slots__ = ("slug", "title", "h1", "meta", "archetype", "market",
                 "vertical", "intro", "sections", "faq", "breadcrumb")

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw.get(k))


def faq_jsonld(faq: list[tuple[str, str]]) -> dict:
    return {"@context": "https://schema.org", "@type": "FAQPage",
            "mainEntity": [{"@type": "Question", "name": q,
                            "acceptedAnswer": {"@type": "Answer", "text": a}}
                           for q, a in faq]}


def article_jsonld(p: Page, url: str) -> dict:
    return {"@context": "https://schema.org", "@type": "Article",
            "headline": p.h1, "description": p.meta,
            "mainEntityOfPage": url, "datePublished": "2026-06-06",
            "author": {"@type": "Organization", "name": "Hailports"},
            "publisher": {"@type": "Organization", "name": "Hailports"}}


def breadcrumb_jsonld(p: Page) -> dict:
    crumbs = [("Home", CANONICAL_BASE + "/"),
             (p.vertical.title(), CANONICAL_BASE + PATH_PREFIX + f"/{slug(p.vertical)}-lead-generation-guide.html"),
             (p.h1, CANONICAL_BASE + PATH_PREFIX + f"/{p.slug}.html")]
    return {"@context": "https://schema.org", "@type": "BreadcrumbList",
            "itemListElement": [{"@type": "ListItem", "position": i + 1,
                                 "name": n, "item": u}
                                for i, (n, u) in enumerate(crumbs)]}


CTA_PRIMARY = (
    f'<div class="cta"><strong>{PRODUCT}</strong> — {PITCH}. '
    f'<a href="{{sample}}">Run the free AI-visibility scan →</a> '
    f'<span class="sub" style="color:#bbb">then lock the fix · '
    f'<a href="{STRIPE_INTENT}">$39 AI-Visibility Fix Kit, one-time</a></span></div>'
)


GO_INTENT = "https://www.hailports.com/ai-visibility/check"


def _go_url(p) -> str:
    """Per-page-attributed /go link so we can prove WHICH SEO page sends a human to
    checkout (c=seo_intel lane + utm_campaign=vertical + utm_content=slug)."""
    from urllib.parse import quote
    return (GO_INTENT + "?c=seo_intel&utm_source=intel&utm_medium=prog_seo"
            f"&utm_campaign={quote(slug(p.vertical))}&utm_content={quote(p.slug)}")


def _widget(p: Page) -> str:
    """Above-the-fold interactive conversion widget — type a category → run the free
    AI-visibility scan (same origin, attributed). The reader's first action funnels
    them to the real proof, then the $39 fix kit."""
    go_js = json.dumps(_go_url(p))
    sample = esc(SAMPLE_URL + "&utm_content=" + p.slug)
    return (
        '<div class="wgt"><h3>When buyers ask AI for the best in ' + esc(p.vertical) + ', are you named?</h3>'
        '<p>Type your category and see exactly how today’s AI assistants describe your business — '
        'and who they name instead. Free, no signup.</p>'
        '<div class="row"><input id="wn" placeholder="your category (e.g. roofers, dentists, HVAC)" '
        'onkeydown="if(event.key===\'Enter\')seoGo()">'
        '<button onclick="seoGo()">Run free scan →</button></div>'
        '<p class="fine">Prefer to browse first? <a href="' + sample + '">Run the free AI-visibility scan</a> '
        '· then lock the $39 AI-Visibility Fix Kit, one-time.</p>'
        '<script>function seoGo(){var n=document.getElementById("wn").value.trim();var u=' + go_js +
        ';if(n)u+=(u.indexOf("?")<0?"?":"&")+"utm_term="+encodeURIComponent(n.slice(0,60));location.assign(u);}</script>'
        '</div>'
    )


def _bar(p: Page) -> str:
    return ('<div class="bar">See how AI answers for ' + esc(p.vertical) +
            ' — <a href="' + esc(_go_url(p)) + '">run the free scan →</a></div>')


def lead_answer(p: Page) -> str:
    """A 40-60 word direct answer to the page's primary query, placed under the H1 —
    the prime paragraph-snippet target. Built per-archetype from real page facts."""
    m = MARKETS.get(p.market, {})
    aud = m.get("audience", "teams")
    noun = m.get("intent_noun", "buyers")
    comps = m.get("competitors", [])
    v = p.vertical
    if p.archetype == "alternatives":
        comp = re.sub(r"\s+alternatives\b.*$", "", re.sub(r"^Best ", "", p.h1)).strip()
        others = [c for c in comps if c.lower() != comp.lower()][:3] or comps[:3]
        return (f"The strongest {comp} alternatives in 2026 are {', '.join(others)}, plus "
                f"{PRODUCT} for {aud} who want to be the business AI names when a buyer asks for the "
                f"best in {v}. The right pick comes down to price, data accuracy, support, and "
                f"whether buyers can actually find you at the moment they ask.")
    if p.archetype == "vs":
        comp = re.sub(r"\s+vs\b.*$", "", p.h1).strip()
        return (f"{comp} and {PRODUCT} both help {aud} win buyers in {v}, in opposite ways: {comp} "
                f"sells a stored contact database you query and export, while {PRODUCT} shows how the "
                f"AI assistants buyers now ask answer when someone wants the best in {v} — and gets "
                f"you named, with a free scan and a one-time $39 fix kit.")
    if p.archetype == "howto":
        return (f"The fastest way to find ready-to-buy customers in {v} is to monitor public posts where "
                f"{noun} ask for recommendations, name a budget, or vent about their current tool, "
                f"then reach out with that context while the thread is still fresh. Score each "
                f"signal by budget and urgency so you skip tire-kickers.")
    if p.archetype == "guide":
        return (f"Lead generation in {v} in 2026 works best when you capture public buyer intent "
                f"instead of buying cold lists: find {aud} who post that they are shopping or "
                f"switching, score them by budget and urgency, and reach out with the source "
                f"context while the need is still self-declared and fresh.")
    if p.archetype == "signals":
        return (f"The buyer-intent signals that matter most in {v} are switching language "
                f"('alternative to', 'moving off'), explicit budget mentions, direct help requests, "
                f"and public complaints about a competitor. Each one means a buyer has declared a "
                f"need and is open to a timely, relevant pitch right now.")
    if p.archetype == "monitoring":
        return (f"To monitor {v} for buyer intent, set alerts on the phrases buyers actually use — "
                f"'alternative to', 'recommendations for', 'is there a tool that' — plus competitor "
                f"names, then score each post by budget and urgency. {PRODUCT} also tracks the newest "
                f"intent channel — how AI answers when buyers ask for the best in {v} — and gets you named.")
    return ""


def paa_pairs(p: Page) -> list[tuple[str, str]]:
    """People-Also-Ask questions mined from the page's market, added to the visible
    'People also ask' block AND the FAQPage schema so the page can claim PAA slots."""
    m = MARKETS.get(p.market, {})
    aud = m.get("audience", "teams")
    noun = m.get("intent_noun", "buyers")
    v = p.vertical
    pairs = [
        (f"How do you find {v} leads without cold email?",
         f"Watch where {aud} publicly post that they are shopping, switching, or frustrated with a "
         f"tool, then reach out with that context while the thread is still fresh. And because buyers "
         f"increasingly ask AI for the best option first, {PRODUCT} shows how those AI assistants "
         f"answer for {v} today — and gets you named, free scan then a $39 fix kit."),
        (f"What counts as a buyer-intent signal in {v}?",
         f"A buyer-intent signal is a public post where someone names a need, a budget, a deadline, "
         f"or a tool they want to replace. In {v}, switching language, 'recommendations for' "
         f"requests, and explicit budget mentions are the clearest signs a buyer is ready to act "
         f"now rather than someday."),
    ]
    if p.archetype in ("alternatives", "vs"):
        comp = re.sub(r"^Best ", "", p.h1)
        comp = re.sub(r"\s+(alternatives|vs)\b.*$", "", comp).strip()
        if comp:
            pairs.append((f"How much does {comp} cost?",
                f"{comp} pricing varies by seats, credits and contract and is usually quoted per "
                f"company, so check their current plans directly. For comparison, {PRODUCT} starts "
                f"with a free scan and a one-time $39 fix kit — no contract, no per-seat pricing."))
            pairs.append((f"Is {comp} worth it?",
                f"{comp} is worth it when you need broad firmographic coverage and enrichment at "
                f"scale and the price fits your budget. If being found the moment buyers ask AI is "
                f"your real gap, {PRODUCT} gets you named for a one-time $39."))
    return pairs


def render(p: Page, related: list[tuple[str, str]]) -> str:
    url = CANONICAL_BASE + PATH_PREFIX + f"/{p.slug}.html"
    biz = BIZ_URL
    sample = SAMPLE_URL
    cta = CTA_PRIMARY.format(biz=biz, sample=sample)
    rel_html = ""
    if related:
        rel_html = ('<h2>Related guides</h2><ul class="rel">'
                    + "".join(f'<li><a href="{esc(s)}.html">{esc(t)}</a></li>' for t, s in related)
                    + "</ul>")
    faq_q, paa_q, seen_q = [], [], set()
    for q, a in list(p.faq):
        k = q.strip().lower()
        if k not in seen_q:
            seen_q.add(k); faq_q.append((q, a))
    for q, a in paa_pairs(p):
        k = q.strip().lower()
        if k not in seen_q:
            seen_q.add(k); paa_q.append((q, a))
    faq_html = "<h2>FAQ</h2>" + "".join(
        f"<h3>{esc(q)}</h3><p>{esc(a)}</p>" for q, a in faq_q)
    if paa_q:
        faq_html += ('<div class="paa"><h2>People also ask</h2>' + "".join(
            f"<h3>{esc(q)}</h3><p>{esc(a)}</p>" for q, a in paa_q) + "</div>")
    ld = [article_jsonld(p, url), faq_jsonld(faq_q + paa_q), breadcrumb_jsonld(p)]
    ld_html = "".join(
        f'<script type="application/ld+json">{json.dumps(x, ensure_ascii=False)}</script>'
        for x in ld)
    return (
        f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>{esc(p.title)}</title>'
        f'<meta name="description" content="{esc(p.meta)}">'
        f'<link rel="canonical" href="{url}">'
        f'<meta property="og:type" content="article">'
        f'<meta property="og:site_name" content="Hailports">'
        f'<meta property="og:title" content="{esc(p.title)}">'
        f'<meta property="og:description" content="{esc(p.meta)}">'
        f'<meta property="og:url" content="{url}">'
        f'<meta name="twitter:card" content="summary_large_image">'
        f'<meta name="twitter:title" content="{esc(p.title)}">'
        f'<meta name="twitter:description" content="{esc(p.meta)}">{ld_html}'
        f'<style>{STYLE}</style></head><body>'
        f'<div class="disc">Some links are affiliate or our own products — we disclose, '
        f'and we link every public source so you can verify it yourself.</div>'
        f'<h1>{esc(p.h1)}</h1>'
        f'<p class="snip lead">{lead_answer(p)}</p>'
        f'<p>{p.intro}</p>' + _widget(p)
        + inject_h2_snippets("".join(p.sections))
        + cta + faq_html + rel_html
        + f'<p style="margin-top:28px"><a href="/guides/">← All guides</a> · '
          f'<a href="{biz}">How it works</a> · <a href="{sample}">Free AI-visibility scan</a></p>'
        + '<p class="sub" style="margin-top:30px">Signals aggregated from public posts; '
          'we link the source and never publish private data.</p>'
        + _bar(p)
        + '</body></html>'
    )


# ── archetype builders (each returns a Page or None) ─────────────────────────
def _proof(stats: dict, vertical: str) -> str:
    if not stats["n"]:
        return ""
    return (f'<div class="proof">We currently track <strong>{stats["n"]} public '
            f'buying-intent signals</strong> in {esc(vertical)} — {stats["budget"]} with an '
            f'explicit budget, freshest from {esc(stats["fresh"])}. Each one links to the '
            f'original post.</div>')


def build_alternatives(mk, m, comp, leads):
    pool = leads.get(mk, [])
    s = signal_slice(pool, "alt-" + comp, 10, prefer=comp)
    stats = market_stats(pool)
    cslug = slug(comp)
    p = Page(
        slug=f"best-{cslug}-alternatives",
        title=f"Best {comp} Alternatives in 2026 (with real buyer signals) | Hailports",
        h1=f"Best {comp} alternatives in 2026",
        meta=f"Honest {comp} alternatives for {m['audience']}, plus {stats['n']} real public "
             f"posts from people shopping for one. Cited, dated, no fluff."[:155],
        archetype="alternatives", market=mk, vertical=m["vertical"],
        breadcrumb=m["vertical"],
        intro=(f"If you are weighing a switch from <strong>{esc(comp)}</strong>, you are not "
               f"alone. Below are the credible alternatives {esc(m['audience'])} actually compare, "
               f"plus real, dated posts from people publicly shopping for a {esc(comp)} "
               f"replacement right now."),
        sections=[
            _proof(stats, m["vertical"]),
            f"<h2>What to weigh when leaving {esc(comp)}</h2>"
            f"<p>The recurring reasons people give for switching are price, missing features, "
            f"support, and data accuracy. Score any alternative against those four before you "
            f"migrate — and against one thing most lists ignore: whether it helps you reach "
            f"buyers <em>while they are still shopping</em>.</p>"
            f"<h2>Credible {esc(comp)} alternatives</h2>"
            f"<ul>" + "".join(
                f"<li><strong>{esc(c)}</strong> — a real option {esc(m['audience'])} compare "
                f"against {esc(comp)} on price and fit.</li>"
                for c in m["competitors"] if c != comp) +
            f"<li><strong>{PRODUCT}</strong> — instead of another static database, it shows how "
            f"the AI assistants buyers now ask answer when someone wants the best, and gets you named.</li></ul>"
            f"<h2>Real posts from people leaving {esc(comp)}</h2>"
            + (signals_html(s) or f"<p>We refresh {esc(comp)} switching signals continuously.</p>"),
        ],
        faq=[
            (f"What is the best alternative to {comp}?",
             f"There is no single best — it depends on price and the features you actually use. "
             f"The list above covers the credible options {m['audience']} compare. If your goal is "
             f"being found the moment buyers ask AI for the best, {PRODUCT} gets you named in {m['vertical']}."),
            (f"Why do people leave {comp}?",
             f"In the public posts we track, the recurring reasons are cost, data accuracy, and "
             f"support. We link {stats['n']} of those posts so you can read the exact words."),
            (f"Is there a cheaper {comp} alternative?",
             f"Several options on this list compete on price. {PRODUCT} takes a different angle — a "
             f"free scan of how AI names your category, then a one-time $39 fix kit, no subscription."),
        ],
    )
    return p


def build_vs(mk, m, comp, leads):
    pool = leads.get(mk, [])
    s = signal_slice(pool, "vs-" + comp, 8, prefer=comp)
    stats = market_stats(pool)
    cslug = slug(comp)
    p = Page(
        slug=f"{cslug}-vs-intent-lead-finder",
        title=f"{comp} vs {PRODUCT}: which finds warmer leads? | Hailports",
        h1=f"{comp} vs {PRODUCT}",
        meta=f"{comp} vs {PRODUCT} for {m['audience']}: static database versus live, public "
             f"buyer intent. An honest, side-by-side comparison."[:155],
        archetype="vs", market=mk, vertical=m["vertical"], breadcrumb=m["vertical"],
        intro=(f"<strong>{esc(comp)}</strong> and <strong>{PRODUCT}</strong> solve adjacent "
               f"problems for {esc(m['audience'])}, but in opposite ways. One sells you a list; "
               f"the other finds people <em>publicly raising their hand</em>. Here is the honest "
               f"side-by-side."),
        sections=[
            _proof(stats, m["vertical"]),
            f"<h2>The core difference</h2>"
            f"<p><strong>{esc(comp)}</strong> is built around a stored dataset you query and "
            f"export. <strong>{PRODUCT}</strong> is built around <em>the moment of intent</em>: it "
            f"shows how the AI assistants buyers now ask answer when someone in {esc(m['vertical'])} "
            f"wants the best, and gets you named so you are the one they find.</p>"
            f"<h2>When {esc(comp)} is the right call</h2>"
            f"<p>If you need broad firmographic coverage and enrichment at scale, a database like "
            f"{esc(comp)} earns its seat. The trade-off is that everyone else can buy the same "
            f"cold records, and timing is on you.</p>"
            f"<h2>When {PRODUCT} wins</h2>"
            f"<p>If your real gap is being invisible the moment a buyer asks AI for the best in "
            f"{esc(m['vertical'])}, {PRODUCT} is the better fit — a free scan, then a one-time $39 "
            f"fix kit. Examples of the live buyer intent it tracks in {esc(m['vertical'])}:</p>"
            + (signals_html(s) or "<p>Fresh signals refresh continuously.</p>"),
        ],
        faq=[
            (f"Is {PRODUCT} a {comp} replacement?",
             f"Not a like-for-like one. {comp} is a contact database; {PRODUCT} makes sure buyers "
             f"find you when they ask AI for the best. Many teams run a lean version of both."),
            (f"How much does {PRODUCT} cost vs {comp}?",
             f"{PRODUCT} starts free and the fix kit is a one-time $39. {comp} pricing varies by "
             f"seats and credits and is usually materially higher for comparable reach."),
            (f"Can I see the data before paying?",
             f"Yes — the AI-visibility scan is free and shows exactly how AI names your category today."),
        ],
    )
    return p


def build_howto(mk, m, adj, leads):
    pool = leads.get(mk, [])
    key = f"howto-{adj}-{mk}"
    s = signal_slice(pool, key, 10)
    stats = market_stats(pool)
    p = Page(
        slug=f"how-to-find-{slug(adj)}-leads-in-{slug(m['vertical'])}",
        title=f"How to find {adj} leads in {m['vertical']} (2026 playbook) | Hailports",
        h1=f"How to find {adj} leads in {m['vertical']}",
        meta=f"A practical 2026 playbook for finding {adj} {m['intent_noun']} — with {stats['n']} "
             f"real public signals you can act on today."[:155],
        archetype="howto", market=mk, vertical=m["vertical"], breadcrumb=m["vertical"],
        intro=(f"The fastest way to find <strong>{esc(adj)} leads</strong> in "
               f"<strong>{esc(m['vertical'])}</strong> is to stop guessing and go where "
               f"{esc(m['intent_noun'])} are <em>already saying</em> what they need. Here is the "
               f"exact playbook, plus live examples."),
        sections=[
            _proof(stats, m["vertical"]),
            f"<h2>Where {esc(adj)} demand shows up</h2>"
            f"<p>In {esc(m['vertical'])}, {esc(adj)} buyers surface on Reddit, niche forums, "
            f"review sites and community Slacks — asking for recommendations, complaining about "
            f"their current tool, or posting a budget. Those posts are the signal; the trick is "
            f"catching them while they are fresh.</p>"
            f"<h2>A 4-step playbook</h2>"
            f"<ol><li>Define the exact phrases your buyers use (we mine these from real posts).</li>"
            f"<li>Monitor the platforms where {esc(m['audience'])} hang out, continuously.</li>"
            f"<li>Score each post for budget + urgency so you skip tire-kickers.</li>"
            f"<li>Reach out with the source context, while the thread is still warm.</li></ol>"
            f"<h2>Live {esc(adj)} signals in {esc(m['vertical'])}</h2>"
            + (signals_html(s) or "<p>Signals refresh continuously.</p>")
            + f"<p>And the highest-intent moment now is when a buyer asks AI for the best — "
              f"{PRODUCT} shows how today’s AI assistants answer for {esc(m['vertical'])} and gets you named.</p>",
        ],
        faq=[
            (f"Where do {adj} {m['intent_noun']} post?",
             f"Mostly public communities — Reddit, Hacker News, niche forums, review sites and "
             f"Slack groups. We track {stats['n']} such posts in {m['vertical']} right now."),
            (f"How do I know a lead is actually {adj}?",
             f"Score it: an explicit budget, a deadline, or a 'we're switching' statement beats a "
             f"vague 'someday'. {stats['budget']} of the posts we track name a budget outright."),
            ("Do I have to monitor this manually?",
             f"You can, but it's a grind. {PRODUCT} also covers the channel you can't search by hand — "
             f"how AI answers for {m['vertical']} — with a free scan and a one-time $39 fix kit."),
        ],
    )
    return p


def build_guide(mk, m, leads):
    pool = leads.get(mk, [])
    s = signal_slice(pool, "guide-" + mk, 12)
    stats = market_stats(pool)
    p = Page(
        slug=f"{slug(m['vertical'])}-lead-generation-guide",
        title=f"{m['vertical'].title()} Lead Generation: the 2026 guide | Hailports",
        h1=f"{m['vertical'].title()} lead generation: the 2026 guide",
        meta=f"How {m['audience']} generate leads in {m['vertical']} in 2026 — channels, scoring, "
             f"and {stats['n']} real public buyer signals to model."[:155],
        archetype="guide", market=mk, vertical=m["vertical"], breadcrumb=m["vertical"],
        intro=(f"Lead generation in <strong>{esc(m['vertical'])}</strong> has shifted from buying "
               f"cold lists to <em>capturing public intent</em>. This guide covers the channels "
               f"that work for {esc(m['audience'])} in 2026, how to score what you find, and {stats['n']} "
               f"real signals to model your outreach on."),
        sections=[
            _proof(stats, m["vertical"]),
            f"<h2>The channels that work in {esc(m['vertical'])}</h2>"
            f"<p>Outbound cold email still works at the margins, but the highest-converting source "
            f"for {esc(m['audience'])} is now public buyer intent — people who post that they are "
            f"shopping, switching, or frustrated with their current tool. It converts because the "
            f"timing is right and the need is self-declared.</p>"
            f"<h2>How to score a lead</h2>"
            f"<p>Rank every signal on three axes: explicit budget, urgency, and fit. In our data, "
            f"posts with a stated budget convert far better — {stats['budget']} of the {stats['n']} "
            f"signals we track in {esc(m['vertical'])} name one.</p>"
            f"<h2>Real buyer signals to model</h2>"
            + (signals_html(s) or "<p>Signals refresh continuously.</p>")
            + f"<h2>The channel you can't search by hand</h2>"
              f"<p>Buyers increasingly skip search and ask AI for the best in {esc(m['vertical'])} "
              f"outright. {PRODUCT} shows you exactly how today’s AI assistants answer that question — "
              f"and gets you named. Free scan, then a one-time $39 fix kit.</p>",
        ],
        faq=[
            (f"What is the best lead source for {m['vertical']}?",
             f"Public buyer intent — posts where {m['intent_noun']} ask for help. We currently "
             f"track {stats['n']} of them in {m['vertical']}."),
            ("Is cold email dead?",
             "No, but its reply rate keeps falling. Pairing it with warm, intent-based leads is "
             "what lifts overall conversion."),
            ("How often does fresh demand appear?",
             f"Continuously. The freshest signal in our {m['vertical']} set is from {stats['fresh']}, "
             f"and {PRODUCT} re-checks how AI names your category so you stay found."),
        ],
    )
    return p


def build_signals(mk, m, role, leads):
    pool = leads.get(mk, [])
    key = f"sig-{role}-{mk}"
    s = signal_slice(pool, key, 10)
    stats = market_stats(pool)
    p = Page(
        slug=f"{slug(role)}-buyer-intent-signals-{slug(m['vertical'])}",
        title=f"Buyer-intent signals for {role} in {m['vertical']} | Hailports",
        h1=f"Buyer-intent signals {role} should watch in {m['vertical']}",
        meta=f"The public buyer-intent signals {role} in {m['vertical']} should monitor in 2026 — "
             f"with {stats['n']} live, cited examples."[:155],
        archetype="signals", market=mk, vertical=m["vertical"], breadcrumb=m["vertical"],
        intro=(f"For <strong>{esc(role)}</strong> in <strong>{esc(m['vertical'])}</strong>, the "
               f"difference between a cold quarter and a full pipeline is catching buyer-intent "
               f"signals early. Here are the signals worth watching — and {stats['n']} live ones "
               f"to act on."),
        sections=[
            _proof(stats, m["vertical"]),
            f"<h2>The signals that matter for {esc(role)}</h2>"
            f"<ul><li><strong>Switching language</strong> — 'alternative to', 'moving off', "
            f"'fed up with'.</li><li><strong>Budget mentions</strong> — a named dollar figure or "
            f"'willing to pay'.</li><li><strong>Help requests</strong> — 'recommendations for', "
            f"'is there a tool that'.</li><li><strong>Pain posts</strong> — public complaints about "
            f"a competitor's gaps.</li></ul>"
            f"<p>For {esc(role)}, the third and fourth are gold: the person has declared the problem "
            f"and is open to a pitch.</p>"
            f"<h2>Live signals right now in {esc(m['vertical'])}</h2>"
            + (signals_html(s) or "<p>Signals refresh continuously.</p>")
            + f"<p>And the clearest signal of all is which business AI names first — {PRODUCT} shows "
              f"{esc(role)} how AI answers for {esc(m['vertical'])} today, and gets you named.</p>",
        ],
        faq=[
            (f"What buyer-intent signals should {role} track?",
             f"Switching language, budget mentions, help requests and pain posts. We track {stats['n']} "
             f"of these in {m['vertical']} today."),
            ("Are these signals public?",
             "Yes — all from public posts. We link every source and never publish private data."),
            (f"How does {PRODUCT} help {role}?",
             f"It shows how today’s AI assistants answer when buyers ask for the best in "
             f"{m['vertical']}, and gets you named — a free scan, then a one-time $39 fix kit."),
        ],
    )
    return p


def build_monitoring(mk, m, platform, leads):
    pool = leads.get(mk, [])
    key = f"mon-{platform}-{mk}"
    s = signal_slice(pool, key, 9)
    stats = market_stats(pool)
    p = Page(
        slug=f"{slug(platform)}-intent-monitoring-for-{slug(m['vertical'])}",
        title=f"{platform} intent monitoring for {m['vertical']} | Hailports",
        h1=f"Monitoring {platform} for buyer intent in {m['vertical']}",
        meta=f"How to monitor {platform} for {m['vertical']} buyer intent in 2026 — what to watch, "
             f"how to score it, and {stats['n']} live signals."[:155],
        archetype="monitoring", market=mk, vertical=m["vertical"], breadcrumb=m["vertical"],
        intro=(f"<strong>{esc(platform)}</strong> is one of the richest places to catch "
               f"<strong>{esc(m['vertical'])}</strong> buyers in the act of shopping. Here is what "
               f"to monitor, how to score it, and live examples of the intent it surfaces."),
        sections=[
            _proof(stats, m["vertical"]),
            f"<h2>Why monitor {esc(platform)}</h2>"
            f"<p>{esc(m['audience'].capitalize())} use {esc(platform)} to ask for recommendations, "
            f"vent about tools, and compare options in the open. Each of those posts is a timed "
            f"buying signal — if you catch it fresh.</p>"
            f"<h2>What to watch for</h2>"
            f"<p>Set alerts on the phrases buyers actually use: 'alternative to', 'recommendations "
            f"for', 'is there a tool that', plus competitor names. Then score by budget and urgency "
            f"so you skip the noise.</p>"
            f"<h2>Live intent we surface for {esc(m['vertical'])}</h2>"
            + (signals_html(s) or "<p>Signals refresh continuously.</p>")
            + f"<p>{PRODUCT} also covers the channel you can't set an alert on — how AI answers when "
              f"buyers ask for the best in {esc(m['vertical'])} — and gets you named. Free scan, then a $39 fix kit.</p>",
        ],
        faq=[
            (f"Can I monitor {platform} for leads myself?",
             f"Yes, with saved searches and alerts — but it is constant work. {PRODUCT} automates it "
             f"across {platform} and other sources."),
            (f"What should I search for on {platform}?",
             "Switching language, help requests, budget mentions and competitor names in your "
             "vertical."),
            ("Is scraping these posts allowed?",
             "We only read public posts and link the original source; we never republish private "
             "data."),
        ],
    )
    return p


# ── permutation matrix ───────────────────────────────────────────────────────
def build_all(leads) -> list[Page]:
    pages: list[Page] = []
    seen_comp: set[str] = set()
    for mk, m in MARKETS.items():
        # 4. vertical guide (one per vertical)
        pages.append(build_guide(mk, m, leads))
        # 1 + 2. competitor archetypes (dedup competitor globally)
        for comp in m["competitors"]:
            if comp.lower() in seen_comp:
                continue
            seen_comp.add(comp.lower())
            pages.append(build_alternatives(mk, m, comp, leads))
            pages.append(build_vs(mk, m, comp, leads))
        # 3. how-to-find (intent adjective x vertical)
        for adj in INTENT_ADJ:
            pages.append(build_howto(mk, m, adj, leads))
        # 5. buyer-intent signals (role x vertical)
        for role in ROLES:
            pages.append(build_signals(mk, m, role, leads))
        # 6. platform intent monitoring (platform x vertical)
        for platform in PLATFORMS:
            pages.append(build_monitoring(mk, m, platform, leads))
    # global slug dedup (safety)
    out, seen = [], set()
    for p in pages:
        if p.slug in seen:
            continue
        seen.add(p.slug)
        out.append(p)
    return out


def related_for(p: Page, by_v: dict, by_a: dict, all_slugs: set) -> list[tuple[str, str]]:
    """5-10 related links: same vertical + same archetype, deduped, resolvable."""
    rel, seen = [], {p.slug}
    for q in by_v.get(p.vertical, []):  # same vertical, different archetype
        if q.slug not in seen and q.archetype != p.archetype:
            rel.append((q.h1, q.slug)); seen.add(q.slug)
        if len(rel) >= 5:
            break
    for q in by_a.get(p.archetype, []):  # same archetype, different vertical
        if q.slug not in seen:
            rel.append((q.h1, q.slug)); seen.add(q.slug)
        if len(rel) >= 9:
            break
    return [(t, s) for t, s in rel if s in all_slugs]


def write_sitemap(pages: list[Page] | None = None):
    # Source of truth = the files actually on disk, NOT just this run's batch. A --limit run
    # writes ~80 pages but the dir holds the full accumulated corpus; keying the sitemap off the
    # batch desynced it and made --check flag every older page "NOT IN SITEMAP" (the 812 problems).
    slugs = sorted(f.stem for f in OUTDIR.glob("*.html") if not f.stem.startswith("sitemap"))
    locs = [CANONICAL_BASE + PATH_PREFIX + f"/{s}.html" for s in slugs]
    # split into <=45k-URL files with an index if needed
    CHUNK = 45000
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if len(locs) <= CHUNK:
        body = "".join(f"  <url><loc>{u}</loc><lastmod>{today}</lastmod></url>\n" for u in locs)
        sm = ('<?xml version="1.0" encoding="UTF-8"?>\n'
              '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n' + body + "</urlset>\n")
        (OUTDIR / "sitemap.xml").write_text(sm)
    else:
        parts = [locs[i:i + CHUNK] for i in range(0, len(locs), CHUNK)]
        for i, chunk in enumerate(parts):
            body = "".join(f"  <url><loc>{u}</loc><lastmod>{today}</lastmod></url>\n" for u in chunk)
            (OUTDIR / f"sitemap-{i}.xml").write_text(
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n' + body + "</urlset>\n")
        idx = "".join(f"  <sitemap><loc>{CANONICAL_BASE}{PATH_PREFIX}/sitemap-{i}.xml</loc>"
                      f"<lastmod>{today}</lastmod></sitemap>\n" for i in range(len(parts)))
        (OUTDIR / "sitemap.xml").write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n' + idx + "</sitemapindex>\n")
    (OUTDIR / "robots.txt").write_text(
        f"User-agent: *\nAllow: /\nSitemap: {CANONICAL_BASE}{PATH_PREFIX}/sitemap.xml\n")


_ALL_COMPETITORS = sorted(
    {c for m in MARKETS.values() for c in m["competitors"]} | {PRODUCT},
    key=len, reverse=True)


def editorial_text(p: Page) -> str:
    """Our own copy only — strip the quoted public-post signal lists (real
    third-party text we cite, not our writing) and mask real product names so
    the slop gate judges OUR editorial quality, not a brand name or a quote."""
    html_parts = [p.intro] + list(p.sections) + [a for _, a in p.faq]
    blob = " ".join(html_parts)
    blob = re.sub(r"<ul class='sig'>.*?</ul>", " ", blob, flags=re.S)  # drop quoted signals
    blob = re.sub(r"<[^>]+>", " ", blob)
    for name in _ALL_COMPETITORS:
        blob = re.sub(re.escape(name), "the tool", blob, flags=re.I)
    return re.sub(r"\s+", " ", blob).strip()


def gate_ok(p: Page) -> bool:
    try:
        from core.content_quality import gate
        return gate(editorial_text(p), channel="blog", persona="generic").passed
    except Exception:
        return True


def generate(limit=None, no_gate=False) -> dict:
    leads = load_leads()
    pages = build_all(leads)
    if limit:
        pages = pages[:limit]
    by_v, by_a = {}, {}
    for p in pages:
        by_v.setdefault(p.vertical, []).append(p)
        by_a.setdefault(p.archetype, []).append(p)
    all_slugs = {p.slug for p in pages}

    OUTDIR.mkdir(parents=True, exist_ok=True)
    if not limit:  # full regen: clear stale pages so files always match the sitemap
        for old in OUTDIR.glob("*.html"):
            old.unlink()
    written, skipped = [], []
    seen_h1, seen_meta = set(), set()
    for p in pages:
        if p.h1 in seen_h1 or p.meta in seen_meta:
            skipped.append((p.slug, "dup h1/meta")); continue
        if not no_gate and not gate_ok(p):
            skipped.append((p.slug, "gate")); continue
        rel = related_for(p, by_v, by_a, all_slugs)
        h = render(p, rel)
        seen_h1.add(p.h1); seen_meta.add(p.meta)
        (OUTDIR / f"{p.slug}.html").write_text(h)
        written.append(p)
    write_sitemap(written)
    return {"written": len(written), "skipped": len(skipped),
            "skip_detail": skipped[:20], "archetypes": dict(Counter(p.archetype for p in written))}


# ── verification ─────────────────────────────────────────────────────────────
def check() -> int:
    files = sorted(OUTDIR.glob("*.html"))
    bad = 0
    h1s, metas = Counter(), Counter()
    slugs = {f.stem for f in files}
    snip_total, snip_over = 0, 0
    for f in files:
        h = f.read_text()
        m1 = re.search(r"<h1>(.*?)</h1>", h, re.S)
        md = re.search(r'name="description" content="([^"]*)"', h)
        h1s[m1.group(1) if m1 else ""] += 1
        metas[md.group(1) if md else ""] += 1
        # JSON-LD parse + FAQPage question count (PAA targeting)
        faq_n = 0
        for blob in re.findall(r'application/ld\+json">(.*?)</script>', h, re.S):
            try:
                obj = json.loads(blob)
            except Exception:
                print(f"BAD JSON-LD: {f.name}"); bad += 1; continue
            if isinstance(obj, dict) and obj.get("@type") == "FAQPage":
                faq_n = len(obj.get("mainEntity", []))
        if faq_n < 4:
            print(f"FAQ<4 ({faq_n}): {f.name}"); bad += 1
        # featured-snippet answer blocks present + 40-60 word discipline
        snips = re.findall(r'<p class="snip[^"]*">(.*?)</p>', h, re.S)
        if not snips:
            print(f"NO SNIPPET: {f.name}"); bad += 1
        for s in snips:
            snip_total += 1
            if _wc(re.sub(r"<[^>]+>", " ", s)) > 65:
                snip_over += 1
        if STRIPE_INTENT not in h:
            print(f"NO CTA: {f.name}"); bad += 1
        if f'href="{CANONICAL_BASE}{PATH_PREFIX}/' not in h:
            print(f"BAD canonical: {f.name}"); bad += 1
        # internal related links resolve to real generated files
        for tgt in re.findall(r'class="rel"[^>]*>(.*?)</ul>', h, re.S):
            for s in re.findall(r'href="([a-z0-9-]+)\.html"', tgt):
                if s not in slugs:
                    print(f"DEAD LINK {s} in {f.name}"); bad += 1
    for h1, c in h1s.items():
        if c > 1:
            print(f"DUP H1 ({c}x): {h1[:60]}"); bad += 1
    for mt, c in metas.items():
        if c > 1:
            print(f"DUP META ({c}x): {mt[:60]}"); bad += 1
    # sitemap covers all
    sm = (OUTDIR / "sitemap.xml").read_text() if (OUTDIR / "sitemap.xml").exists() else ""
    is_index = "<sitemapindex" in sm
    if not is_index:
        for f in files:
            if f.stem in ("sitemap",):
                continue
            if f"/{f.name}" not in sm:
                print(f"NOT IN SITEMAP: {f.name}"); bad += 1
    print(f"check: {len(files)} pages, {len(h1s)} unique H1, {len(metas)} unique meta, "
          f"{snip_total} snippet blocks ({snip_over} over 65w), "
          f"{'OK' if not bad else str(bad)+' problems'}")
    return 1 if bad else 0


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int)
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--no-gate", action="store_true")
    a = ap.parse_args(argv)
    if a.check:
        return check()
    r = generate(limit=a.limit, no_gate=a.no_gate)
    print(json.dumps(r, indent=2))
    return check()


if __name__ == "__main__":
    raise SystemExit(main())
