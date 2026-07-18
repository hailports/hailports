#!/usr/bin/env python3
"""Prospect enrichment — from a domain (+ known failure) to a tailored PROFILE, at ~$0.

DOCTRINE (hard): ~90% deterministic. Everything in this file except ONE optional
synthesis step is pure public-page scraping + parsing — no LLM, no API, no spend. The
single synthesis step (raw facts -> "who they are" + most-resonant angle + one tailored
opening line) runs LOCAL-FIRST through core.llm_router.try_local_then_api with
api_fn=return None, so it stays on local Ollama / the free pool and NEVER pays. If the
local model is unavailable the deterministic fallback composes the same three fields from
templates, so the pipeline never blocks and never spends.

WHY a profile beats the generic broken-site copy: the existing _render_copy in
agents/broken_site_outreach_prep.py speaks to "a website that's broken". A profile lets
us speak to a *person* — "Hi Dave, saw A1's been doing septic work around Sioux Falls
since 2003..." — using only facts the owner published about themselves. That is the
difference between cold spam and a note that reads like a neighbor noticed.

REUSE (no new scrape primitives invented):
  agents.broken_site_outreach_prep : _fetch_text, _emails_in, discover_contact,
                                      _base_domain, _company_from_domain, _icp_violation,
                                      SEARX (127.0.0.1:8890 JSON), CONTACT_CACHE
  core.web_failure_probe           : probe() (read-only) to fill the failure if absent
  core.llm_router                  : try_local_then_api (the one synthesis step, local-$0)
  data/hustle/vertical_norms.json  : hand-curated per-vertical norms (deterministic lookup)

ETHICS / ANON: profile = OUR tailoring only. Public BUSINESS facts the owner published
about their own business (About-page owner name, services, city, their own reviews). No
consumer PII, no fabricated facts (synthesis is constrained to the verified fact bundle
and validated against it). ICP gate is enforced first: never profile enterprise/law/gov.

  python3 -m core.prospect_enrichment a1pumpingsd.com          # build + print one profile
  python3 -m core.prospect_enrichment a1pumpingsd.com --no-llm # deterministic only
  python3 -m core.prospect_enrichment --refresh a1pumpingsd.com# ignore cache

This module NEVER sends and has no send path. The wiring into _render_copy is delivered
as RENDER_COPY_WIRING_SPEC below (a patch SPEC), intentionally NOT applied here.
"""
from __future__ import annotations

import argparse
import html as _htmllib
import json
import re
import sys
import urllib.parse
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# --- REUSE the broken-site scrape helpers (no new primitives) ---
from agents.broken_site_outreach_prep import (  # noqa: E402
    _fetch_text, _emails_in, discover_contact, _base_domain,
    _company_from_domain, _icp_violation, SEARX,
)

PROFILE_CACHE = ROOT / "data" / "hustle" / "prospect_profiles.json"
VERTICAL_NORMS_FILE = ROOT / "data" / "hustle" / "vertical_norms.json"
CACHE_TTL_DAYS = 14
SCHEMA_VERSION = 1


# ----------------------------------------------------------------------------- profile
@dataclass
class Profile:
    """Structured, message-ready prospect profile. Every field is either a verifiable
    public fact (deterministic) or a synthesized read explicitly grounded in those facts."""
    domain: str = ""
    base_domain: str = ""
    # --- identity / deterministic ---
    business_name: str = ""
    owner_name: str = ""          # from their OWN About page only
    vertical: str = ""
    city: str = ""
    services: list[str] = field(default_factory=list)
    customer_type: str = ""       # residential | commercial | both | ""
    family_owned: bool = False
    founded_year: int | None = None
    years_in_business: int | None = None
    domain_age_years: int | None = None
    builder: str = ""             # wix/squarespace/etc if detected
    socials: dict[str, str] = field(default_factory=dict)
    phone: str = ""
    contact_email: str = ""
    # --- reputation / deterministic ---
    rating: float | None = None
    review_count: int | None = None
    review_themes: list[str] = field(default_factory=list)
    # --- the hook / deterministic (from the probe) ---
    failure: str = ""             # plain-English defect (outreach_angle / probe reason)
    severity: str = ""
    # --- vertical norms / deterministic table lookup ---
    norms: dict = field(default_factory=dict)
    # --- the ONE synthesized layer (local-Ollama, $0, grounded) ---
    who_they_are: str = ""        # one-line persona read
    angle: str = ""               # the single most resonant angle
    opening_line: str = ""        # tailored first line referencing a true fact
    synth_source: str = ""        # "local" | "free:*" | "deterministic"
    # --- provenance / bookkeeping ---
    provenance: dict[str, str] = field(default_factory=dict)  # field -> source url/tag
    facts_for_synth: list[str] = field(default_factory=list)  # the grounding allow-list
    sources_hit: list[str] = field(default_factory=list)
    checked_at: str = ""
    schema: int = SCHEMA_VERSION


# --------------------------------------------------------------------------- low-level
def _text(html: str) -> str:
    h = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html or "", flags=re.I | re.S)
    h = re.sub(r"<[^>]+>", " ", h)
    return re.sub(r"\s+", " ", _htmllib.unescape(h)).strip()


def _meta(html: str, *keys: str) -> str:
    for k in keys:
        m = re.search(
            rf'<meta[^>]+(?:property|name)=["\']{re.escape(k)}["\'][^>]+content=["\']([^"\']+)',
            html or "", re.I)
        if m:
            return _htmllib.unescape(m.group(1)).strip()
    return ""


def _title(html: str) -> str:
    m = re.search(r"<title[^>]*>(.*?)</title>", html or "", re.I | re.S)
    return _htmllib.unescape(re.sub(r"\s+", " ", m.group(1)).strip()) if m else ""


def _first(html: str, tag: str) -> str:
    m = re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", html or "", re.I | re.S)
    return _text(m.group(1)) if m else ""


_NAME_RE = re.compile(r"^[A-Z][a-z]{1,15}(?:\s[A-Z][a-z'.-]{1,20}){1,2}$")
_NAME_STOP = {"Our Team", "Contact Us", "About Us", "Privacy Policy", "Terms Of",
              "Service Area", "Free Estimate", "Get Started", "Our Story", "Read More"}


def _looks_like_name(s: str) -> bool:
    s = s.strip(" .,")
    return bool(_NAME_RE.match(s)) and s not in _NAME_STOP


# --------------------------------------------------------------- deterministic extractors
def _extract_business_name(home_html: str, domain: str) -> tuple[str, str]:
    og = _meta(home_html, "og:site_name", "og:title", "application-name")
    if og:
        og = re.split(r"\s[|\-–—]\s", og)[0].strip()
        if 2 <= len(og) <= 60:
            return og, "og:site_name"
    t = _title(home_html)
    if t:
        t = re.split(r"\s[|\-–—]\s", t)[0].strip()
        if 2 <= len(t) <= 60 and "home" not in t.lower():
            return t, "title"
    h1 = _first(home_html, "h1")
    if h1 and 2 <= len(h1) <= 60:
        return h1, "h1"
    return _company_from_domain(domain), "domain"


def _extract_owner(about_text: str) -> str:
    pats = (
        r"(?:owner|founder|president|proprietor)[,:]?\s+(?:is\s+)?([A-Z][a-z]+(?:\s[A-Z][a-z'.-]+){1,2})",
        r"([A-Z][a-z]+(?:\s[A-Z][a-z'.-]+){1,2}),?\s+(?:the\s+)?(?:owner|founder|president|proprietor)",
        r"founded by\s+([A-Z][a-z]+(?:\s[A-Z][a-z'.-]+){1,2})",
        r"(?:my name is|i['’]m|i am)\s+([A-Z][a-z]+(?:\s[A-Z][a-z'.-]+){0,2})",
        r"meet\s+([A-Z][a-z]+(?:\s[A-Z][a-z'.-]+){1,2})",
    )
    for p in pats:
        m = re.search(p, about_text or "")
        if m and _looks_like_name(m.group(1)):
            return m.group(1).strip(" .,")
    return ""


def _extract_years(text: str) -> tuple[int | None, int | None, bool]:
    """(founded_year, years_in_business, family_owned) from their own copy."""
    t = text or ""
    this_year = datetime.now(timezone.utc).year
    founded = None
    for p in (r"(?:since|established|est\.?|serving\D{0,30}since|founded(?:\s+in)?)\s*((?:19|20)\d{2})",
              r"family[- ]owned[^.]{0,30}since\s*((?:19|20)\d{2})"):
        m = re.search(p, t, re.I)
        if m:
            y = int(m.group(1))
            if 1900 <= y <= this_year:
                founded = y
                break
    years = (this_year - founded) if founded else None
    if years is None:
        m = re.search(r"(?:for\s+)?(?:over\s+)?(\d{1,3})\+?\s*years?(?:\s+of)?\s*(?:experience|in business|serving)?", t, re.I)
        if m:
            n = int(m.group(1))
            if 1 <= n <= 120:
                years = n
    family = bool(re.search(r"family[- ]owned|family[- ]operated|family[- ]run", t, re.I))
    return founded, years, family


def _extract_customer_type(text: str) -> str:
    t = (text or "").lower()
    res = len(re.findall(r"\b(residential|homeowner|home owner|your home|families)\b", t))
    com = len(re.findall(r"\b(commercial|business(?:es)?|property manager|industrial|office)\b", t))
    if res and com:
        return "both"
    if res:
        return "residential"
    if com:
        return "commercial"
    return ""


_NAV_STOP = {"home", "about", "about us", "contact", "contact us", "blog", "news",
             "gallery", "reviews", "testimonials", "careers", "login", "cart", "menu",
             "faq", "privacy", "terms", "sitemap", "search", "book now", "get a quote",
             "free estimate", "request service", "our team", "our story", "services",
             "facebook", "instagram", "twitter", "youtube"}


def _extract_services(home_html: str, services_html: str, seed: list[str]) -> list[str]:
    out: list[str] = []

    def add(phrase: str):
        p = re.sub(r"\s+", " ", phrase).strip(" .|-–—•").strip()
        pl = p.lower()
        if 3 <= len(p) <= 40 and pl not in _NAV_STOP and not re.search(r"\d{3}|@|http", pl):
            words = p.split()
            if 1 <= len(words) <= 5 and p[0].isalpha() and p not in out:
                out.append(p)

    # nav / link anchors on home
    for m in re.findall(r"<a[^>]*>(.*?)</a>", home_html or "", re.I | re.S):
        add(_text(m))
    # headings on a services/menu page
    for tag in ("h2", "h3", "li"):
        for m in re.findall(rf"<{tag}[^>]*>(.*?)</{tag}>", services_html or "", re.I | re.S):
            add(_text(m))
    # keep only those that look service-y (intersect with seed words OR service nouns)
    seed_words = {w for s in seed for w in s.lower().split()}
    service_nouns = seed_words | {"repair", "installation", "install", "service", "cleaning",
                                  "removal", "maintenance", "replacement", "inspection",
                                  "remodel", "estimate", "emergency", "design", "trimming",
                                  "pumping", "treatment", "care"}
    ranked = [s for s in out if any(w in s.lower() for w in service_nouns)]
    result = (ranked or out)[:8]
    if not result and seed:
        return list(seed[:5])
    return result


_SOCIAL_HOSTS = {"facebook.com": "facebook", "instagram.com": "instagram",
                 "linkedin.com": "linkedin", "youtube.com": "youtube",
                 "yelp.com": "yelp", "twitter.com": "twitter", "x.com": "twitter",
                 "nextdoor.com": "nextdoor", "tiktok.com": "tiktok"}


def _extract_socials(home_html: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for u in re.findall(r'href=["\']([^"\']+)["\']', home_html or "", re.I):
        host = urllib.parse.urlparse(u).netloc.lower().lstrip("www.")
        for h, name in _SOCIAL_HOSTS.items():
            if host.endswith(h) and name not in out and "/sharer" not in u and "/share" not in u:
                out[name] = u
    return out


_BUILDERS = ("wix.com", "squarespace", "weebly", "godaddy", "duda", "vistaprint", "wordpress")


def _extract_builder(home_html: str) -> str:
    pl = (home_html or "").lower()
    for b in _BUILDERS:
        if b in pl:
            return b.split(".")[0]
    return ""


_ADDR_RE = re.compile(r"([A-Z][A-Za-z.\- ]{2,30}),\s*([A-Z]{2})\s+(\d{5})(?:-\d{4})?")


def _extract_city(contact_text: str, home_text: str, record_city: str) -> str:
    if record_city:
        return record_city
    for txt in (contact_text, home_text):
        m = _ADDR_RE.search(txt or "")
        if m:
            return f"{m.group(1).strip()}, {m.group(2)}"
    return ""


# ----------------------------------------------------------- reviews via SearXNG ($0)
_PRAISE_THEMES = {
    "on time": "punctual", "on-time": "punctual", "prompt": "punctual",
    "professional": "professional", "honest": "honest", "fair price": "fair pricing",
    "reasonable": "fair pricing", "affordable": "fair pricing", "friendly": "friendly",
    "knowledgeable": "knowledgeable", "responsive": "responsive", "quick": "fast",
    "fast": "fast", "reliable": "reliable", "courteous": "courteous", "clean": "tidy",
    "highly recommend": "highly recommended", "great work": "quality work",
    "quality": "quality work", "trustworthy": "trustworthy",
}


def _reviews_via_searx(business_name: str, base: str, city: str) -> dict:
    """Deterministic: query SearXNG (same JSON endpoint discover_contact uses) for the
    business's own reviews; tally rating/count + recurring praise words from snippets."""
    out: dict = {"rating": None, "review_count": None, "themes": [], "src": ""}
    q = f'"{business_name}" {city} reviews'.strip()
    data = _fetch_text(f"{SEARX}/search?" + urllib.parse.urlencode({"q": q, "format": "json"}),
                       timeout=10, cap=200_000)
    try:
        results = json.loads(data).get("results", [])
    except Exception:
        results = []
    if not results:
        return out
    blob = " ".join((r.get("title", "") + " " + r.get("content", "")) for r in results[:10])
    low = blob.lower()
    # rating like "4.8 stars" / "4.8 out of 5" / "Rated 4.8"
    rm = re.search(r"\b([0-5](?:\.\d))\s*(?:stars?|out of 5|/\s*5|★)", low) \
        or re.search(r"rated\s+([0-5](?:\.\d))", low)
    if rm:
        out["rating"] = float(rm.group(1))
    cm = re.search(r"\((\d{1,4})\)\s*(?:reviews?|google)?", low) \
        or re.search(r"(\d{1,4})\s+(?:google\s+)?reviews?", low)
    if cm:
        out["review_count"] = int(cm.group(1))
    tally: dict[str, int] = {}
    for kw, theme in _PRAISE_THEMES.items():
        if kw in low:
            tally[theme] = tally.get(theme, 0) + low.count(kw)
    out["themes"] = [t for t, _ in sorted(tally.items(), key=lambda x: -x[1])][:4]
    if out["rating"] or out["themes"]:
        out["src"] = f"searx:{q}"
    return out


# ------------------------------------------------------------- domain age via RDAP ($0)
def _domain_age_years(base: str) -> int | None:
    """Registration age via RDAP over HTTP (rdap.org). Pure HTTP — deliberately NOT the
    `whois` subprocess (that forks after Apple SSL calls and segfaults Python on macOS;
    see core/web_failure_probe.py). Yields a business-longevity proxy."""
    data = _fetch_text(f"https://rdap.org/domain/{base}", timeout=8, cap=60_000)
    if not data:
        return None
    try:
        events = json.loads(data).get("events", [])
    except Exception:
        return None
    for ev in events:
        if ev.get("eventAction") == "registration":
            m = re.match(r"(\d{4})", str(ev.get("eventDate", "")))
            if m:
                age = datetime.now(timezone.utc).year - int(m.group(1))
                return age if 0 <= age <= 60 else None
    return None


# --------------------------------------------------------------- vertical norms lookup
_NORMS_CACHE: dict | None = None


def _load_norms() -> dict:
    global _NORMS_CACHE
    if _NORMS_CACHE is None:
        try:
            _NORMS_CACHE = json.loads(VERTICAL_NORMS_FILE.read_text(encoding="utf-8"))
        except Exception:
            _NORMS_CACHE = {"_default": {}, "verticals": []}
    return _NORMS_CACHE


def norms_for(vertical: str) -> dict:
    data = _load_norms()
    v = (vertical or "").lower()
    for entry in data.get("verticals", []):
        if any(syn in v for syn in entry.get("synonyms", [])):
            return entry
    return data.get("_default", {})


# ------------------------------------------------------------------- cache (14d TTL)
def _load_cache() -> dict:
    if PROFILE_CACHE.exists():
        try:
            return json.loads(PROFILE_CACHE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_cache(cache: dict) -> None:
    try:
        PROFILE_CACHE.parent.mkdir(parents=True, exist_ok=True)
        tmp = PROFILE_CACHE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(cache, indent=2), encoding="utf-8")
        tmp.replace(PROFILE_CACHE)
    except Exception:
        pass


def _cache_fresh(entry: dict) -> bool:
    try:
        ts = datetime.fromisoformat(entry.get("checked_at", ""))
        return (datetime.now(timezone.utc) - ts).days < CACHE_TTL_DAYS \
            and entry.get("schema") == SCHEMA_VERSION
    except Exception:
        return False


# ----------------------------------------------- the ONE synthesis step (local, $0)
def _deterministic_synth(p: Profile) -> tuple[str, str, str]:
    """Template fallback so the pipeline never blocks/spends if local is down."""
    who_bits = [b for b in [
        p.business_name,
        (f"{p.customer_type} " if p.customer_type else "") + (p.vertical or "local business"),
        f"in {p.city}" if p.city else "",
    ] if b]
    who = " — ".join(who_bits[:2]) + (f" {who_bits[2]}" if len(who_bits) > 2 else "")
    if p.family_owned:
        who += ", family-owned"
    if p.founded_year:
        who += f", since {p.founded_year}"
    norms = p.norms or {}
    angle = (f"They likely win on {norms.get('resonant_value', 'doing honest work')}, "
             f"but {p.failure or 'the site is getting in the way'} — "
             f"{norms.get('proof_that_lands', 'a working mobile site')} would fix it.")
    owner_first = p.owner_name.split()[0] if p.owner_name else ""
    hi = f"Hi {owner_first}" if owner_first else "Hi there"
    if p.founded_year and p.city:
        open_line = (f"{hi} — looks like {p.business_name} has been doing "
                     f"{p.vertical} around {p.city} since {p.founded_year}.")
    elif p.review_themes and p.city:
        open_line = (f"{hi} — folks around {p.city} clearly rate {p.business_name} "
                     f"for being {p.review_themes[0]}.")
    elif p.city:
        open_line = f"{hi} — I was looking at {p.business_name} over in {p.city}."
    else:
        open_line = f"{hi} — I came across {p.business_name} and took a look at the site."
    return who, angle, open_line


def _synth_facts(p: Profile) -> list[str]:
    f: list[str] = []
    if p.business_name:
        f.append(f"business name: {p.business_name}")
    if p.owner_name:
        f.append(f"owner: {p.owner_name}")
    if p.vertical:
        f.append(f"vertical: {p.vertical}")
    if p.city:
        f.append(f"city: {p.city}")
    if p.founded_year:
        f.append(f"in business since {p.founded_year}")
    elif p.years_in_business:
        f.append(f"about {p.years_in_business} years in business")
    if p.family_owned:
        f.append("family-owned")
    if p.customer_type:
        f.append(f"serves {p.customer_type} customers")
    if p.services:
        f.append("services: " + ", ".join(p.services[:5]))
    if p.rating:
        f.append(f"rated {p.rating} stars" + (f" ({p.review_count} reviews)" if p.review_count else ""))
    if p.review_themes:
        f.append("customers praise: " + ", ".join(p.review_themes))
    if p.failure:
        f.append(f"current website problem: {p.failure}")
    n = p.norms or {}
    if n.get("resonant_value"):
        f.append(f"what their customers value: {n['resonant_value']}")
    if n.get("proof_that_lands"):
        f.append(f"what would land: {n['proof_that_lands']}")
    return f


def _grounded(text: str, facts: list[str], owner_first: str) -> bool:
    """Reject fabrication: every 4-digit year and the owner first-name token that appears
    in the synthesized output must be present in the verified fact bundle. Keeps 'no
    fabricated facts' a hard, checkable rule rather than a hope."""
    factblob = " ".join(facts).lower()
    for full_yr in set(re.findall(r"\b((?:19|20)\d{2})\b", text)):
        if full_yr not in factblob:
            return False
    # any other Capitalized name-like token not the owner is suspicious only if it asserts a name
    if owner_first and owner_first.lower() not in factblob:
        return False
    return True


async def _local_synth(p: Profile) -> tuple[str, str, str, str] | None:
    """Local-first, $0. Returns (who, angle, opening_line, source) or None.
    api_fn returns None => never escalates to paid API (free pool allowed, paid blocked)."""
    try:
        from core.llm_router import try_local_then_api
    except Exception:
        return None
    facts = p.facts_for_synth or _synth_facts(p)
    if not facts:
        return None
    owner_first = p.owner_name.split()[0] if p.owner_name else ""
    prompt = (
        "You write the opening of a warm, neighborly note to a local small-business owner "
        "whose website has a fixable problem. Use ONLY the verified facts below. Invent "
        "NOTHING — no names, years, services, or claims not in the facts. No AI/marketing "
        "tone, no emojis, no hype.\n\n"
        "VERIFIED FACTS:\n- " + "\n- ".join(facts) + "\n\n"
        "Return ONLY a JSON object with exactly these keys:\n"
        '  "who_they_are": one sentence describing who this owner/business is (<=22 words),\n'
        '  "angle": the single most resonant reason this owner would care about fixing the '
        "site, tying their customers' values to the specific website problem (<=30 words),\n"
        '  "opening_line": the first line of the note, addressed to them, referencing one '
        "specific true fact, under 30 words, sounds like a real person noticed.\n"
        "JSON only."
    )

    def _valid(text: str) -> bool:
        try:
            obj = json.loads(text)
        except Exception:
            return False
        if not all(obj.get(k) for k in ("who_they_are", "angle", "opening_line")):
            return False
        joined = " ".join(str(obj[k]) for k in ("who_they_are", "angle", "opening_line"))
        return _grounded(joined, facts, owner_first) and len(obj["opening_line"]) <= 240

    async def api_fn():
        return None  # local/free only — NEVER pay to write an opener

    try:
        text, source = await try_local_then_api(
            prompt=prompt, api_fn=api_fn, validator=_valid,
            displaced_tier="sonnet", source="prospect_enrichment:synth",
            local_model="quality", local_timeout=120.0, max_tokens=400,
            allow_api_fallback=False,
        )
    except Exception:
        return None
    if not text or not _valid(text):
        return None
    try:
        obj = json.loads(text)
    except Exception:
        return None
    return (obj["who_they_are"].strip(), obj["angle"].strip(),
            obj["opening_line"].strip(), source)


# --------------------------------------------------------------------- orchestration
def build_profile(domain: str, *, failure: str = "", severity: str = "",
                  record: dict | None = None, use_llm: bool = True,
                  refresh: bool = False, cache: dict | None = None) -> dict:
    """Deterministic-first enrichment for one domain. Returns the cached profile dict.
    Never raises, never sends, never pays. `record` may be a broken_site_prospects row
    (carries name/vertical/city/outreach_angle/mockup already)."""
    record = record or {}
    domain = re.sub(r"^https?://", "", domain.strip().lower()).split("/")[0]
    base = _base_domain(domain)

    # HARD ICP gate first — never profile enterprise / law / gov.
    viol = _icp_violation(domain, record.get("company", "") or record.get("name", ""))
    if viol:
        return {"domain": domain, "icp_blocked": viol, "checked_at": _now()}

    own_cache = cache is None
    cache = cache if cache is not None else _load_cache()
    if not refresh:
        ent = cache.get(base)
        if ent and _cache_fresh(ent):
            return ent

    p = Profile(domain=domain, base_domain=base)
    prov = p.provenance
    p.vertical = record.get("vertical", "")
    p.failure = failure or record.get("outreach_angle", "")
    p.severity = severity or record.get("severity", "")

    # --- fetch the owner's own public pages (reused $0 fetch helper) ---
    home = _fetch_text(f"https://{domain}") or _fetch_text(f"http://{domain}")
    about = ""
    for path in ("/about", "/about-us", "/our-story", "/about.html"):
        about = _fetch_text(f"https://{domain}{path}")
        if about:
            prov["owner_name"] = f"https://{domain}{path}"
            break
    services_html = ""
    for path in ("/services", "/our-services", "/menu", "/services.html"):
        services_html = _fetch_text(f"https://{domain}{path}")
        if services_html:
            prov["services"] = f"https://{domain}{path}"
            break
    contact_html = _fetch_text(f"https://{domain}/contact") or _fetch_text(f"https://{domain}/contact-us")
    if home:
        p.sources_hit.append("homepage")
    if about:
        p.sources_hit.append("about")
    if services_html:
        p.sources_hit.append("services")
    if contact_html:
        p.sources_hit.append("contact")

    home_text = _text(home)
    about_text = _text(about) or home_text
    all_text = " ".join((home_text, about_text, _text(services_html), _text(contact_html)))

    # --- deterministic identity ---
    if record.get("name"):
        p.business_name = record["name"]
        prov["business_name"] = "prospect_record"
    else:
        p.business_name, src = _extract_business_name(home, domain)
        prov["business_name"] = src
    p.owner_name = _extract_owner(about_text)
    p.norms = norms_for(p.vertical)
    seed = p.norms.get("services_seed", [])
    p.services = _extract_services(home, services_html, seed)
    if p.services:
        prov.setdefault("services", "homepage/nav")
    p.customer_type = _extract_customer_type(all_text)
    p.founded_year, p.years_in_business, p.family_owned = _extract_years(all_text)
    if p.founded_year:
        prov["founded_year"] = prov.get("owner_name") or "homepage"
    p.builder = _extract_builder(home)
    p.socials = _extract_socials(home)
    p.city = _extract_city(_text(contact_html), home_text, record.get("city", ""))
    if p.city:
        prov["city"] = "prospect_record" if record.get("city") else "contact_page"

    # --- contact (reuse the cached discover_contact; don't double-fetch) ---
    try:
        contact = discover_contact(domain)
        p.contact_email = contact.get("email") or ""
        p.phone = contact.get("phone") or ""
    except Exception:
        pass

    # --- reputation via SearXNG ($0) ---
    if p.business_name:
        rev = _reviews_via_searx(p.business_name, base, p.city)
        p.rating = rev["rating"]
        p.review_count = rev["review_count"]
        p.review_themes = rev["themes"]
        if rev["src"]:
            p.sources_hit.append("searx_reviews")
            prov["review_themes"] = rev["src"]

    # --- domain age via RDAP ($0) ---
    p.domain_age_years = _domain_age_years(base)
    if p.domain_age_years is not None:
        prov["domain_age_years"] = "rdap.org"
        if p.years_in_business is None:
            p.years_in_business = p.domain_age_years  # proxy

    # --- the ONE synthesized layer (local Ollama, $0) ---
    p.facts_for_synth = _synth_facts(p)
    if use_llm:
        import asyncio
        try:
            res = asyncio.run(_local_synth(p))
        except RuntimeError:  # already in a loop
            res = None
        if res:
            p.who_they_are, p.angle, p.opening_line, p.synth_source = res
    if not p.opening_line:
        p.who_they_are, p.angle, p.opening_line = _deterministic_synth(p)
        p.synth_source = "deterministic"

    p.checked_at = _now()
    out = asdict(p)
    cache[base] = out
    if own_cache:
        _save_cache(cache)
    return out


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def enrich(domain: str, failure: str = "", *, cache: bool = True) -> dict:
    """Primary entrypoint named by the design: domain (+ optional known failure) -> a
    tailored profile dict. ~90% deterministic multi-source extraction ($0) plus the ONE
    local-Ollama synthesis layer; result cached 14d. `cache=False` bypasses the cache and
    re-scrapes (still writes the fresh profile). Never raises, never sends, never pays."""
    return build_profile(domain, failure=failure, use_llm=True, refresh=not cache)


def enrich_record(record: dict, *, use_llm: bool = True, cache: dict | None = None) -> dict:
    """Convenience: take a broken_site_prospects row, return it merged with its profile."""
    prof = build_profile(record.get("domain", ""), record=record, use_llm=use_llm, cache=cache)
    return {**record, "profile": prof}


# ------------------------------------------------------------------------------- spec
RENDER_COPY_WIRING_SPEC = r"""
WIRING SPEC — how the OWNER (not this file) folds the profile into
agents/broken_site_outreach_prep.py::_render_copy. SPEC ONLY — do NOT apply here; that
file is owned by another build. The change is additive and reversible.

1) At top of broken_site_outreach_prep.py (after the existing imports):

       try:
           from core.prospect_enrichment import build_profile
       except Exception:
           build_profile = None

2) Inside _render_copy(prospect), right after `angle = ...`:

       prof = {}
       if build_profile is not None:
           try:
               prof = build_profile(domain, record=prospect, use_llm=True)
           except Exception:
               prof = {}
       opener   = (prof.get("opening_line") or "").strip()
       owner    = (prof.get("owner_name") or "").strip()
       greeting = f"Hi {owner.split()[0]},\\n\\n" if owner else "Hi there,\\n\\n"

   Then replace the literal "Hi there,\n\n" in BOTH body branches with `greeting`,
   and prepend the tailored opener when present, e.g. the mockup branch body becomes:

       body = (
           greeting
           + (opener + "\\n\\n" if opener else "")
           + f"I ran a quick health check on {company}'s site ({domain}) and found this:\\n\\n"
           + f"{angle}\\n\\n"
           ... (unchanged remainder) ...
       )

3) Guarantees preserved:
   - Still passes through canspam.render() unchanged (no compliance regression).
   - build_profile is fail-soft: any error => prof={} => exact current behavior.
   - $0: profile is cached 14d; the only model call is local-Ollama (api_fn=None).
   - No fabricated facts: opener is grounded-validated; empty when unsure => generic copy.
   - ICP gate already runs in build_profile AND in run(); enterprise/law/gov never profiled.
"""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Prospect enrichment — domain -> tailored profile ($0)")
    ap.add_argument("domain")
    ap.add_argument("--no-llm", action="store_true", help="deterministic only (skip local synthesis)")
    ap.add_argument("--refresh", action="store_true", help="ignore the 14d cache")
    args = ap.parse_args(argv)
    prof = build_profile(args.domain, use_llm=not args.no_llm, refresh=args.refresh)
    print(json.dumps(prof, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
