#!/usr/bin/env python3
"""Passive review-gap detector — the soft signal for Broken-Site Rescue's review play.

$0 and autonomous: asks the local self-hosted SearXNG (127.0.0.1:8890, no API key, no LLM,
no Google/Yelp login) for "<business> <locality> reviews" and reads ONLY what's already in
the public SERP — the rich-snippet rating + review-count that Google/Yelp/Facebook expose
("Rating 4.5(362)", "... 362 Reviews"). From those it infers three owner-verifiable gaps:

  - review_gap / no_public_reviews  : no public listing with reviews surfaces at all
  - review_gap / few_public_reviews : a listing exists but the review count is thin
  - review_gap / low_review_rating  : a listing's public average rating is low

Each finding is emitted in the exact opportunity shape used by core/web_failure_probe.py
(_opportunity_signals): {"snag": "review_gap", "severity": "low|medium|high", "angle": <pitch>}.
It does its OWN SearXNG lookup, so it is a standalone module (it can't live inside
_opportunity_signals, which forbids network calls); a review play imports review_signals()
and merges the result into the prospect record.

HONESTY / PASSIVE LIMITS — what is NOT reliably detectable this way, and is therefore NOT emitted:
  - Owner *responses* to reviews. SERP snippets carry rating, count and review text, but never
    whether the owner replied. Confirming reply-presence needs loading + parsing the live Yelp/
    Google profile (intrusive, JS-rendered, login-gated) — out of bounds. We do not claim it.
  - A clean "no_public_reviews" can be a search miss (SearXNG didn't surface the listing) rather
    than a true absence, so it is capped at medium severity, never high.
Only the directly-observed low rating is treated as high — the owner can reproduce it by searching
their own name. No guessing past what the snippet literally shows.

  python3 -m core.review_gap_probe "Gather Restaurant" "Omaha NE"
  python3 -m core.review_gap_probe "Joe's Plumbing" "Omaha NE" --json
"""
from __future__ import annotations

import json
import re
import sys
import urllib.parse
import urllib.request

SEARX = "http://127.0.0.1:8890"
UA = "Mozilla/5.0 SiteHealthBot/1.0"

LOW_RATING = 4.0          # avg below this on a credible listing = low_review_rating
LOW_RATING_MIN_COUNT = 5  # ...but only once enough reviews exist that the avg isn't a fluke
FEW_REVIEWS = 8           # best public review count below this = few_public_reviews

# Where public reviews actually live. Opposite of the scraper's AGGREGATORS drop-list:
# here these hosts are exactly what we WANT (they carry the rating snippet).
REVIEW_PLATFORMS = {
    "yelp.com": "Yelp",
    "google.com": "Google",
    "g.co": "Google",
    "facebook.com": "Facebook",
    "tripadvisor.com": "Tripadvisor",
    "bbb.org": "BBB",
    "trustpilot.com": "Trustpilot",
    "angi.com": "Angi",
    "healthgrades.com": "Healthgrades",
    "zocdoc.com": "Zocdoc",
    "avvo.com": "Avvo",
}

STOP_TOKENS = {"the", "and", "llc", "inc", "co", "ltd", "corp", "corporation",
               "company", "group", "services", "service", "of", "for"}

_RATING_PAREN = re.compile(r"rating\s*([0-5](?:\.\d)?)\s*\(\s*(\d[\d,]{0,6})\s*\)", re.I)
_REVIEWS_CT = re.compile(r"(\d[\d,]{0,6})\s+reviews?\b", re.I)
_RATING_OUT5 = re.compile(r"\b([0-5](?:\.\d)?)\s*(?:out of\s*5|/\s*5|stars?)\b", re.I)


def _searx_results(query: str, limit: int = 25) -> list[dict]:
    """Raw public SERP rows {url,title,content} from local SearXNG. Never raises."""
    url = f"{SEARX}/search?" + urllib.parse.urlencode({"q": query, "format": "json"})
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
    except Exception as e:
        sys.stderr.write(f"  ! searxng error for '{query}': {type(e).__name__}: {str(e)[:60]}\n")
        return []
    out = []
    for it in (data.get("results") or [])[:limit]:
        out.append({
            "url": it.get("url", "") or "",
            "title": it.get("title", "") or "",
            "content": it.get("content", "") or "",
        })
    return out


def _host(url: str) -> str:
    try:
        host = urllib.parse.urlparse(url).netloc.lower().split(":")[0]
    except Exception:
        return ""
    for p in ("www.", "m.", "mobile."):
        if host.startswith(p):
            host = host[len(p):]
    return host


def _platform(host: str):
    for key, name in REVIEW_PLATFORMS.items():
        if host == key or host.endswith("." + key) or key in host:
            return name
    return None


def _is_business_listing(url: str, platform: str) -> bool:
    """True only for a specific business profile — drop category/search/"best 10" pages
    whose rating is an aggregate, not this business's."""
    u = url.lower()
    path = urllib.parse.urlparse(u).path
    if any(b in u for b in ("/search", "find_loc", "cflt=", "best-10", "best 10",
                            "/c/", "category", "/topic/", "/explore")):
        return False
    if platform == "Yelp":
        return "/biz/" in path
    if platform == "Google":
        return "/maps/place" in u or "/place/" in u or "maps.google" in u
    return len(path.strip("/")) > 0


def _tokens(name: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9]+", name.lower())
            if len(t) >= 3 and t not in STOP_TOKENS]


def _int(s):
    try:
        return int(s.replace(",", ""))
    except (ValueError, AttributeError):
        return None


def _extract(title: str, content: str):
    """(rating, count) from a snippet; either may be None. Never raises."""
    blob = f"{title} {content}"
    rating = count = None
    m = _RATING_PAREN.search(blob)
    if m:
        rating = float(m.group(1))
        count = _int(m.group(2))
    if count is None:
        mc = _REVIEWS_CT.search(title) or _REVIEWS_CT.search(content)
        if mc:
            count = _int(mc.group(1))
    if rating is None:
        mr = _RATING_OUT5.search(blob)
        if mr:
            rating = float(mr.group(1))
    return rating, count


_NO_REVIEWS_ANGLE = (
    "When someone searches your business with 'reviews', no Google or Yelp listing comes up — "
    "to a new customer that reads as a question mark, and they call the competitor who has stars "
    "next to their name. Claiming and seeding those profiles fixes it fast."
)


def _classify(findings: list[dict]) -> list[dict]:
    if not findings:
        return [{"snag": "review_gap", "severity": "medium", "angle": _NO_REVIEWS_ANGLE}]

    low = None
    for f in findings:
        if f["rating"] is not None and f["rating"] < LOW_RATING and (f["count"] or 0) >= LOW_RATING_MIN_COUNT:
            if low is None or f["rating"] < low["rating"]:
                low = f
    if low:
        return [{"snag": "review_gap", "severity": "high",
                 "angle": (f"Your public rating is sitting at {low['rating']:.1f} on {low['platform']} — "
                           "that number is the first thing a searching customer sees, and it's quietly "
                           "sending them elsewhere. Burying the older ones under fresh reviews moves it back up.")}]

    counts = [f["count"] for f in findings if f["count"] is not None]
    best = max(counts) if counts else None
    if best is not None and best < FEW_REVIEWS:
        s = "" if best == 1 else "s"
        return [{"snag": "review_gap", "severity": "medium",
                 "angle": (f"Your public listings show only {best} review{s} — thin enough that people "
                           "notice, and not enough to build trust against busier competitors. A simple "
                           "after-the-job ask gets happy customers posting, and it compounds.")}]
    return []


def review_signals(business: str, locality: str = "", *, _search=None) -> list[dict]:
    """Passive review-gap opportunities for one business. Same {snag,severity,angle} shape as
    core/web_failure_probe.py opportunities. Network-using but never raises; returns [] on any
    failure or when public review presence looks healthy. Pass _search to inject results (tests)."""
    business = (business or "").strip()
    if not business:
        return []
    fetch = _search or _searx_results
    query = " ".join(p for p in (business, locality, "reviews") if p)
    try:
        results = fetch(query)
    except Exception:
        return []

    toks = _tokens(business)
    findings: list[dict] = []
    for it in results or []:
        url = it.get("url", "") or ""
        platform = _platform(_host(url))
        if not platform or not _is_business_listing(url, platform):
            continue
        title = it.get("title", "") or ""
        haystack = f"{title} {url}".lower()
        if toks and not any(t in haystack for t in toks):
            continue  # likely a different business
        rating, count = _extract(title, it.get("content", "") or "")
        if rating is None and count is None:
            continue
        findings.append({"platform": platform, "rating": rating, "count": count, "url": url})

    return _classify(findings)


def name_from_domain(domain: str) -> str:
    """Rough business-name guess from a bare domain (lossy; for loop integration convenience)."""
    host = _host(domain if "//" in domain else f"http://{domain}")
    sld = host.split(".")[-2] if host.count(".") >= 1 else host
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", sld).replace("-", " ").strip()


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    as_json = "--json" in argv
    is_domain = "--domain" in argv
    args = [a for a in argv if not a.startswith("--")]
    if not args:
        print('usage: python3 -m core.review_gap_probe "<Business Name>" ["City ST"] [--json]')
        print('       python3 -m core.review_gap_probe example.com --domain')
        return 1
    business = name_from_domain(args[0]) if is_domain else args[0]
    locality = args[1] if len(args) > 1 else ""
    sigs = review_signals(business, locality)
    if as_json:
        print(json.dumps(sigs, indent=2))
    else:
        if not sigs:
            print(f"{business}: no review gap detected (public review presence looks healthy)")
        for s in sigs:
            print(f"[{s['severity']:>6}] {s['snag']}: {s['angle']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
