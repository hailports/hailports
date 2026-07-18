#!/usr/bin/env python3
"""Deterministic AEO/GEO-readiness probe — "can AI assistants actually read & cite this store?"

$0 and fully deterministic: pure HTTP / parse. NO LLM calls, no metered API, no scraping of
private data. Every flag is a fact the site owner (or the agency selling them a fix) can verify
in a browser in 5 seconds — which is what makes it a clean picks-and-shovels signal for a
GEO/AEO agency, NOT a "your vibes are off" opinion.

This is the SUPPLY side of the agency play: the LLM-citation grader (core.geo_visibility_probe)
answers "does ChatGPT name this business?" (metered, the SMB-facing motion). THIS probe answers
the cheaper, upstream question — "is the site even structured so an AI CAN cite it?" — using only
deterministic on-page signals, so it scales to whole verticals at $0.

Signals (each a verifiable on-page fact):
  • schema.org / JSON-LD present + parseable + recognized @type (Organization/Product/FAQPage…)
  • llms.txt published (the emerging AI-readability manifest)
  • answer-structured FAQ blocks (FAQPage schema or Q&A markup AI lifts into answers)
  • AI-bot crawlability (robots.txt allows GPTBot/ClaudeBot/PerplexityBot/Google-Extended/…)
  • Shopify /products.json exposure + Product schema (ecom: structured product/price/availability)
  • answerable <title> + meta description, sitemap.xml reachability

Vertical focus: Shopify ecommerce first (best fit — structured product data is exactly what AI
shopping answers need, and Shopify stores are trivially fingerprinted + enumerated).

Reuses core.web_failure_probe for the SSRF-guarded fetch + host block (no duplicate net code).

  python3 -m core.aeo_readiness_probe allbirds.com gymshark.com
  python3 -m core.aeo_readiness_probe --file data/hustle/biz_domains.txt --not-ready --json
  python3 -m core.aeo_readiness_probe --self-test
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone

# Reuse the existing $0, SSRF-guarded fetcher + host block — do NOT re-implement network code.
from core.web_failure_probe import _fetch, _host_blocked, _norm  # noqa: E402

# The AI crawlers that actually matter for assistant/answer-engine visibility. A store that
# Disallows these in robots.txt is, as a verifiable fact, opting OUT of being read by that
# assistant — the single highest-signal AEO gap for an ecom brand that WANTS AI traffic.
AI_BOTS = (
    "GPTBot", "OAI-SearchBot", "ChatGPT-User",        # OpenAI
    "ClaudeBot", "anthropic-ai", "Claude-Web",         # Anthropic
    "PerplexityBot", "Perplexity-User",                # Perplexity
    "Google-Extended",                                  # Google (Gemini/AI Overviews training+grounding)
    "CCBot",                                            # Common Crawl (feeds many models)
    "Applebot-Extended",                                # Apple Intelligence
    "Amazonbot", "Bytespider", "Meta-ExternalAgent",    # Amazon / TikTok / Meta
)

# Recognized schema.org @types that signal AI-extractable structure, weighted by how much they
# help an answer engine cite the business.
_KEY_TYPES = {
    "organization", "localbusiness", "store", "onlinestore", "corporation",
    "product", "productgroup", "offer", "aggregateoffer",
    "faqpage", "question", "qapage",
    "breadcrumblist", "website", "webpage", "itemlist", "review", "aggregaterating",
}


def _grade(score: float) -> str:
    if score >= 85:
        return "A"
    if score >= 70:
        return "B"
    if score >= 55:
        return "C"
    if score >= 40:
        return "D"
    return "F"


# --------------------------------------------------------------------------- JSON-LD parsing

_LD_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.I | re.S,
)


def _iter_types(obj, out: set):
    """Walk a parsed JSON-LD object/graph and collect every lowercased @type."""
    if isinstance(obj, dict):
        t = obj.get("@type")
        if isinstance(t, str):
            out.add(t.lower())
        elif isinstance(t, list):
            for x in t:
                if isinstance(x, str):
                    out.add(x.lower())
        for v in obj.values():
            _iter_types(v, out)
    elif isinstance(obj, list):
        for x in obj:
            _iter_types(x, out)


def _parse_jsonld(html: str) -> dict:
    """Return {blocks, parsed_ok, malformed, types(set)} from the page's JSON-LD scripts.

    'malformed' is a real correctness signal: a JSON-LD block that doesn't parse is invisible to
    the assistant even though the owner thinks they 'have schema' — a high-value thing to flag."""
    blocks = _LD_RE.findall(html or "")
    parsed_ok = 0
    malformed = 0
    types: set[str] = set()
    for raw in blocks:
        txt = raw.strip()
        if not txt:
            continue
        try:
            data = json.loads(txt)
            parsed_ok += 1
            _iter_types(data, types)
        except Exception:
            malformed += 1
    return {"blocks": len(blocks), "parsed_ok": parsed_ok,
            "malformed": malformed, "types": types}


def _has_microdata(html: str) -> bool:
    """Fallback structured-data signal: schema.org microdata/RDFa attributes in the markup."""
    hl = (html or "").lower()
    return ('itemtype="http://schema.org' in hl or "itemtype='http://schema.org" in hl
            or 'itemtype="https://schema.org' in hl or 'vocab="http://schema.org' in hl)


# Bot-challenge / JS-interstitial fingerprints. If the page a non-JS fetch receives is one of
# these walls, we did NOT see the real store — so we must NOT assert content gaps (no schema etc).
# But it is itself a true AEO finding: GPTBot/ClaudeBot/PerplexityBot don't solve JS challenges
# either, so they hit the exact same wall and can't read the catalog.
# HARD-block phrases only — these appear in an actual blocking interstitial, never as incidental
# strings on a real (Cloudflare-fronted) storefront, so they won't false-positive a live page.
_BOT_WALL_MARKERS = (
    "just a moment", "checking your browser before", "attention required! | cloudflare",
    "enable javascript and cookies to continue", "verifying you are human",
    "please turn javascript on and reload",
)


def _bot_walled(html: str) -> bool:
    hl = (html or "").lower()[:8000]
    return any(m in hl for m in _BOT_WALL_MARKERS)


def _faq_markup(html: str) -> bool:
    """Deterministic FAQ-block heuristic (beyond FAQPage schema): <details>/<summary> accordions
    or a 'Frequently Asked Questions' heading — the answer-structured content AI lifts verbatim."""
    hl = (html or "").lower()
    if "<summary" in hl and "<details" in hl:
        return True
    if re.search(r"<h[1-4][^>]*>\s*(?:frequently asked questions|faqs?)\b", hl):
        return True
    return False


# --------------------------------------------------------------------------- robots.txt

def _ai_bot_block(robots_txt: str) -> dict:
    """Parse robots.txt for AI-bot policy. Returns {blocked:[bots], allowed_known:bool, had_robots}.

    Deterministic: a bot is 'blocked' if its User-agent group (or '*') carries a Disallow: /
    (whole-site block). We only assert a block we can literally point to in the file."""
    if robots_txt is None:
        return {"blocked": [], "wildcard_block": False, "had_robots": False}
    lines = [l.strip() for l in robots_txt.splitlines()]
    groups: list[tuple[set, list]] = []  # (agents, disallows)
    cur_agents: set = set()
    cur_dis: list = []
    pending_agents = True
    for ln in lines:
        if not ln or ln.startswith("#"):
            continue
        key, _, val = ln.partition(":")
        key = key.strip().lower()
        val = val.strip()
        if key == "user-agent":
            if not pending_agents and (cur_agents or cur_dis):
                groups.append((cur_agents, cur_dis))
                cur_agents, cur_dis = set(), []
            cur_agents.add(val.lower())
            pending_agents = True
        elif key == "disallow":
            cur_dis.append(val)
            pending_agents = False
        elif key in ("allow", "crawl-delay", "sitemap"):
            pending_agents = False
    if cur_agents or cur_dis:
        groups.append((cur_agents, cur_dis))

    def _blocks_all(disallows: list) -> bool:
        return any(d == "/" for d in disallows)

    wildcard_block = any("*" in ag and _blocks_all(dis) for ag, dis in groups)
    blocked = []
    for bot in AI_BOTS:
        bl = bot.lower()
        for ag, dis in groups:
            if bl in ag and _blocks_all(dis):
                blocked.append(bot)
                break
    return {"blocked": blocked, "wildcard_block": wildcard_block, "had_robots": True}


# --------------------------------------------------------------------------- Shopify

def _shopify_signal(host: str, home_html: str) -> dict:
    """Detect Shopify + read /products.json (the structured product feed AI shopping needs).

    Pure HTTP: Shopify exposes /products.json publicly on storefronts. Reusing this as the
    ecom-vertical fingerprint + product-count signal. {is_shopify, products_json, product_count}."""
    hl = (home_html or "").lower()
    is_shopify = ("cdn.shopify.com" in hl or "myshopify.com" in hl
                  or "shopify.theme" in hl or "x-shopify" in hl or "/cdn/shop/" in hl)
    products_json = False
    product_count = None
    first_handle = None
    status, body, _ = _fetch(f"https://{host}/products.json?limit=5")
    if status == 200 and body:
        try:
            data = json.loads(body)
            if isinstance(data, dict) and isinstance(data.get("products"), list):
                products_json = True
                is_shopify = True
                product_count = len(data["products"])
                if data["products"]:
                    first_handle = data["products"][0].get("handle")
        except Exception:
            pass
    return {"is_shopify": bool(is_shopify), "products_json": products_json,
            "product_count": product_count, "first_handle": first_handle}


# --------------------------------------------------------------------------- main probe

# Weighted check table: (key, weight, label). Weights sum to 100; a store's readiness score is
# the weight of the checks it PASSES. Tuned so the core AI-extractability signals dominate.
_WEIGHTS = {
    "schema_present": 18,      # any valid JSON-LD / microdata at all
    "schema_org_identity": 16, # Organization / LocalBusiness — who the business IS
    "schema_product": 14,      # Product schema — what they sell, with price/availability (ecom)
    "faq_structured": 14,      # FAQPage schema or Q&A markup — the answer-shaped content
    "llms_txt": 12,            # llms.txt manifest published
    "ai_bot_crawlable": 16,    # robots.txt does NOT block the major AI crawlers
    "answerable_meta": 6,      # <title> + meta description present
    "sitemap": 4,              # sitemap.xml reachable
}


def probe(domain: str, *, shopify_only: bool = False) -> dict:
    """Deterministic AEO-readiness verdict for one store. Pure HTTP/parse, $0, no LLM."""
    host = _norm(domain)
    out = {"domain": host, "is_shopify": False, "product_count": None}

    if _host_blocked(host):
        out.update({"severity": "unknown", "aeo_ready": False, "score": 0, "grade": "F",
                    "checks": [], "gaps": ["host is non-public / unresolvable — cannot scan"],
                    "fix_records": [], "error": "blocked-or-unresolvable"})
        return out

    status, home, err = _fetch(f"https://{host}")
    if status is None:
        status, home, err = _fetch(f"http://{host}")
    if not home and status is None:
        out.update({"severity": "unknown", "aeo_ready": False, "score": 0, "grade": "F",
                    "checks": [], "gaps": [f"site did not load ({err or 'no response'})"],
                    "fix_records": [], "error": "unreachable"})
        return out

    shop = _shopify_signal(host, home)
    out["is_shopify"] = shop["is_shopify"]
    out["product_count"] = shop["product_count"]

    # Bot-wall: the fetch got a JS/Cloudflare challenge, not the store. Report ONLY that fact
    # (truthful — we never saw the content), framed as the real AEO gap it is. Non-JS AI crawlers
    # are walled out identically, so the store is genuinely un-citable until the wall is relaxed.
    if _bot_walled(home):
        out.update({
            "score": 35, "grade": "F", "severity": "high", "aeo_ready": False,
            "bot_walled": True,
            "checks": [_chk("Readable by AI crawlers (no JS challenge wall)", False,
                            "homepage serves a JS/Cloudflare bot-challenge to non-browser clients")],
            "gaps": ["AI crawlers (GPTBot/ClaudeBot/PerplexityBot) hit a JS/bot-challenge wall "
                     "before any content loads — assistants can't read the store to cite it"],
            "fix_records": [_fix("Allow verified AI crawlers past the bot wall",
                                 "In Cloudflare (or the WAF/bot-management layer), allowlist the "
                                 "verified AI crawler user-agents / ASNs so GPTBot, ClaudeBot, "
                                 "PerplexityBot and Google-Extended receive the real HTML instead "
                                 "of a JS challenge. Server-render core product/Org/FAQ schema so "
                                 "non-JS crawlers can extract it.")],
            "schema_types": [], "robots_blocked_ai": [],
            "products_json": shop["products_json"],
            "generated_at": datetime.now(timezone.utc).isoformat(),
        })
        return out

    ld = _parse_jsonld(home)
    types = ld["types"]

    if shopify_only and not shop["is_shopify"]:
        out.update({"severity": "skip", "aeo_ready": None, "score": None, "grade": None,
                    "checks": [], "gaps": ["not a Shopify store (skipped — Shopify-only mode)"],
                    "fix_records": [], "skipped": True})
        return out

    # --- individual deterministic checks --------------------------------------------------
    has_schema = (ld["parsed_ok"] > 0) or _has_microdata(home)
    has_identity = bool(types & {"organization", "localbusiness", "store", "onlinestore", "corporation"})
    has_product = bool(types & {"product", "productgroup", "offer", "aggregateoffer"})
    has_faq = bool(types & {"faqpage", "question", "qapage"}) or _faq_markup(home)

    # Product schema lives on PRODUCT pages, not the homepage — so for a Shopify store, read one
    # real product page (handle from /products.json) and merge its JSON-LD before grading. Keeps
    # the highest-value ecom signal truthful instead of falsely failing every store.
    if shop["is_shopify"] and shop.get("first_handle") and not has_product:
        ps_status, ps_html, _ = _fetch(f"https://{host}/products/{shop['first_handle']}")
        if ps_status == 200 and ps_html:
            p_ld = _parse_jsonld(ps_html)
            types = types | p_ld["types"]
            has_product = bool(types & {"product", "productgroup", "offer", "aggregateoffer"})
            has_schema = has_schema or p_ld["parsed_ok"] > 0 or _has_microdata(ps_html)
            has_identity = has_identity or bool(
                types & {"organization", "localbusiness", "store", "onlinestore", "corporation"})
            has_faq = has_faq or bool(types & {"faqpage", "question", "qapage"}) or _faq_markup(ps_html)

    ls_status, ls_body, _ = _fetch(f"https://{host}/llms.txt")
    has_llms = (ls_status == 200 and bool(ls_body)
                and "<html" not in (ls_body or "").lower()[:400])

    rb_status, rb_body, _ = _fetch(f"https://{host}/robots.txt")
    robots = _ai_bot_block(rb_body if rb_status == 200 else None)
    ai_crawlable = not (robots["wildcard_block"] or robots["blocked"])

    has_title = bool(re.search(r"<title[^>]*>\s*\S", home or "", re.I))
    has_meta_desc = bool(re.search(r'<meta[^>]+name=["\']description["\'][^>]+content=["\']\s*\S',
                                   home or "", re.I))
    answerable = has_title and has_meta_desc

    sm_status, _, _ = _fetch(f"https://{host}/sitemap.xml")
    has_sitemap = sm_status == 200

    results = {
        "schema_present": has_schema,
        "schema_org_identity": has_identity,
        "schema_product": has_product,
        "faq_structured": has_faq,
        "llms_txt": has_llms,
        "ai_bot_crawlable": ai_crawlable,
        "answerable_meta": answerable,
        "sitemap": has_sitemap,
    }
    score = round(sum(_WEIGHTS[k] for k, ok in results.items() if ok))
    grade = _grade(score)

    # --- human-readable checks + gaps + fix records ---------------------------------------
    type_list = ", ".join(sorted(types)) if types else "none"
    checks = [
        _chk("Structured data (schema.org/JSON-LD)", has_schema,
             (f"types: {type_list}" if has_schema else
              ("JSON-LD present but MALFORMED — invisible to AI" if ld["malformed"] else "none found"))),
        _chk("Business identity schema (Organization/LocalBusiness)", has_identity,
             "AI can extract who you are" if has_identity else "AI can't reliably identify the business"),
        _chk("Product schema (price / availability)", has_product,
             f"products.json exposes {shop['product_count']} items" if has_product or shop["products_json"]
             else "no Product/Offer schema — AI can't quote your catalog"),
        _chk("Answer-structured FAQ blocks", has_faq,
             "FAQ content is answer-shaped" if has_faq else "no FAQPage schema / Q&A markup to lift into answers"),
        _chk("llms.txt manifest", has_llms,
             "published" if has_llms else "no /llms.txt — no machine-readable profile for assistants"),
        _chk("AI crawlers allowed (robots.txt)", ai_crawlable,
             "GPTBot/ClaudeBot/PerplexityBot/Google-Extended permitted" if ai_crawlable
             else ("robots.txt blocks ALL crawlers" if robots["wildcard_block"]
                   else "robots.txt blocks: " + ", ".join(robots["blocked"]))),
        _chk("Answerable title + meta description", answerable,
             "present" if answerable else "missing title or meta description"),
        _chk("sitemap.xml", has_sitemap, "reachable" if has_sitemap else "not found at /sitemap.xml"),
    ]

    gaps: list[str] = []
    fixes: list[dict] = []
    if not has_schema:
        gaps.append("No structured data (schema.org/JSON-LD) — AI assistants have nothing to extract")
        fixes.append(_fix("Add Organization + Product JSON-LD",
                          "Inject schema.org JSON-LD in the theme: an Organization block (name, url, "
                          "logo, sameAs) site-wide, and Product blocks (name, offers.price, "
                          "availability, brand) on product pages. Shopify themes support this via "
                          "Liquid; one snippet covers the whole catalog."))
    else:
        if ld["malformed"]:
            gaps.append(f"{ld['malformed']} JSON-LD block(s) are MALFORMED and won't parse — "
                        "the schema is effectively invisible to AI")
            fixes.append(_fix("Repair malformed JSON-LD",
                              "Validate every <script type=application/ld+json> block; one trailing "
                              "comma / unescaped quote makes the whole block invisible to assistants."))
        if not has_identity:
            gaps.append("No Organization/LocalBusiness schema — AI can't reliably identify the business")
            fixes.append(_fix("Add Organization schema",
                              "Add a site-wide Organization (or Store) JSON-LD block with name, url, "
                              "logo, and sameAs links to socials."))
        if not has_product and (shop["is_shopify"] or shop["products_json"]):
            gaps.append("Shopify store with no Product schema — AI can't quote price/availability "
                        "in shopping answers")
            fixes.append(_fix("Add Product schema to product pages",
                              "Emit Product JSON-LD per product (name, image, description, "
                              "offers.price, priceCurrency, availability) — Shopify's product object "
                              "maps 1:1, so it's a single theme snippet."))
    if not has_faq:
        gaps.append("No answer-structured FAQ blocks — nothing for AI to lift as a direct answer")
        fixes.append(_fix("Add FAQPage schema",
                          "Convert shipping/returns/sizing Q&A into FAQPage JSON-LD (mainEntity = "
                          "Question/acceptedAnswer pairs). This is the content assistants quote verbatim."))
    if not has_llms:
        gaps.append("No /llms.txt — no machine-readable manifest telling assistants what the store is")
        fixes.append(_fix("Publish /llms.txt",
                          "Add a /llms.txt at the domain root: a short markdown manifest (what the "
                          "store sells, key collections, policies, contact) that AI tools read first."))
    if not ai_crawlable:
        if robots["wildcard_block"]:
            gaps.append("robots.txt blocks ALL crawlers (Disallow: / for *) — assistants can't read "
                        "the store at all")
        else:
            gaps.append("robots.txt explicitly blocks AI crawlers (" + ", ".join(robots["blocked"]) +
                        ") — these assistants are shut out of the catalog")
        fixes.append(_fix("Unblock AI crawlers in robots.txt",
                          "Remove the Disallow rules for GPTBot / ClaudeBot / PerplexityBot / "
                          "Google-Extended (or the wildcard block) so the store can be cited in AI "
                          "shopping answers."))
    if not answerable:
        gaps.append("Missing title or meta description — weak answer snippet for assistants/SERPs")
        fixes.append(_fix("Add title + meta description",
                          "Ensure every page has a descriptive <title> and meta description."))

    severity = ("critical" if score < 40 else "high" if score < 55
                else "medium" if score < 70 else "ok")
    out.update({
        "score": score, "grade": grade, "severity": severity,
        "aeo_ready": score >= 70,            # B+ = genuinely AI-citable
        "checks": checks, "gaps": gaps, "fix_records": fixes,
        "schema_types": sorted(types),
        "robots_blocked_ai": robots["blocked"],
        "products_json": shop["products_json"],
        "generated_at": datetime.now(timezone.utc).isoformat(),
    })
    return out


def _chk(name: str, ok: bool, detail: str = "") -> dict:
    return {"name": name, "ok": bool(ok), "detail": detail}


def _fix(title: str, body: str, note: str = "") -> dict:
    return {"title": title, "body": body, "note": note}


def probe_batch(domains, not_ready_only: bool = False, shopify_only: bool = False) -> list[dict]:
    out = []
    for raw in domains:
        d = (raw or "").strip()
        if not d or d.startswith("#"):
            continue
        v = probe(d, shopify_only=shopify_only)
        if v.get("skipped"):
            continue
        if not_ready_only and v.get("aeo_ready"):
            continue
        out.append(v)
    return out


# --------------------------------------------------------------------------- self-test

def self_test() -> int:
    ok = True
    # JSON-LD extraction
    html = ('<script type="application/ld+json">{"@context":"https://schema.org",'
            '"@type":"Product","name":"X","offers":{"@type":"Offer","price":"9"}}</script>'
            '<script type="application/ld+json">{bad json}</script>')
    ld = _parse_jsonld(html)
    try:
        assert ld["parsed_ok"] == 1 and ld["malformed"] == 1, "jsonld parse/malformed count"
        assert "product" in ld["types"] and "offer" in ld["types"], "type walk"
    except AssertionError as e:
        ok = False; print(f"[FAIL] {e}")
    # robots parse
    rb = "User-agent: GPTBot\nDisallow: /\n\nUser-agent: *\nAllow: /\n"
    r = _ai_bot_block(rb)
    try:
        assert "GPTBot" in r["blocked"] and not r["wildcard_block"], "robots AI block"
        r2 = _ai_bot_block("User-agent: *\nDisallow: /\n")
        assert r2["wildcard_block"], "robots wildcard block"
        assert not _ai_bot_block(None)["had_robots"], "no robots"
    except AssertionError as e:
        ok = False; print(f"[FAIL] {e}")
    # FAQ markup
    try:
        assert _faq_markup("<details><summary>Q?</summary>A</details>"), "details faq"
        assert _faq_markup("<h2>Frequently Asked Questions</h2>"), "heading faq"
        assert not _faq_markup("<p>hello</p>"), "no faq"
    except AssertionError as e:
        ok = False; print(f"[FAIL] {e}")
    # grade boundaries
    try:
        assert _grade(90) == "A" and _grade(72) == "B" and _grade(10) == "F", "grade"
    except AssertionError as e:
        ok = False; print(f"[FAIL] {e}")
    print("SELF-TEST:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv=None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if "--self-test" in argv:
        return self_test()
    as_json = "--json" in argv
    not_ready = "--not-ready" in argv
    shopify_only = "--shopify-only" in argv
    domains = [a for a in argv if not a.startswith("--")]
    if "--file" in argv:
        i = argv.index("--file")
        if i + 1 < len(argv):
            with open(argv[i + 1], encoding="utf-8") as f:
                domains = [l for l in f.read().splitlines() if l.strip() and not l.startswith("#")]
    if not domains:
        print("usage: python3 -m core.aeo_readiness_probe <domain...> | --file <list> "
              "[--not-ready] [--shopify-only] [--json]   |  --self-test")
        return 1
    results = probe_batch(domains, not_ready_only=not_ready, shopify_only=shopify_only)
    if as_json:
        print(json.dumps(results, indent=2))
    else:
        for r in results:
            tag = "SHOPIFY" if r.get("is_shopify") else "web"
            ready = "READY" if r.get("aeo_ready") else "NOT-READY"
            print(f"[{r.get('grade') or '-'}] {r['domain']:<30} {tag:<7} score={r.get('score')}/100 "
                  f"{ready}: {'; '.join(r.get('gaps', []))[:120]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
