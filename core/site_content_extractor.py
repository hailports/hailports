"""Rebuild-from-their-content extractor for the broken-site rescue mockups.

The old mockup pipeline built every site from a generic template copy bank + abstract
SVG art, so every result read as a fake template ("that's not my business"). This module
fetches the prospect's OWN live page and pulls their real name / phone / services / hours
/ reviews / images, so the mockup can be a clean rebuild of THEIR content — the single
biggest lever on "does this land as a serious proof."

Hard rules honored:
  * READ-ONLY, no trail: plain GETs, no cookies, no posted data, one polite UA.
  * SELF-CONTAINED output: every image is fetched and re-encoded as a data: URI so the
    generated page keeps its zero-remote-asset guarantee (no <img src=http…>).
  * FAIL-SOFT: any fetch/parse/decode failure degrades to None / [] — the generator
    falls back to curated real-photo + cleaned template copy, never to a broken page.
"""
from __future__ import annotations

import base64
import html as _html
import io
import json
import re
import ssl
import urllib.parse
import urllib.request

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36")
_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE  # broken-site prospects routinely have bad/expired certs

# directory-scrape boilerplate that pollutes titles ("Septic Tank Contractors in San Diego, CA | Superior Septic")
_DIR_PREFIXES = re.compile(
    r"^\s*(?:best|top|local|professional|licensed|affordable|#?\d+\s+)?"
    r"[\w &/-]*?(?:contractors?|services?|companies|near me|in [\w ,.]+?)\s*[|\-–—:]\s*",
    re.I,
)
_TITLE_SUFFIX = re.compile(r"\s*[|\-–—:]\s*(?:home|official site|welcome|[\w ,.&'-]{0,40})\s*$", re.I)


def fetch(url: str, timeout: int = 9, max_bytes: int = 400_000) -> tuple[str | None, str | None]:
    """GET a URL (https then http). Returns (final_url, html) or (None, None). Read-only."""
    if not url:
        return None, None
    if not url.startswith(("http://", "https://")):
        url = "https://" + url.lstrip("/")
    order = [url] if url.startswith("http") else []
    if url.startswith("https://"):
        order.append("http://" + url[len("https://"):])
    for u in order:
        try:
            req = urllib.request.Request(u, headers={"User-Agent": _UA, "Accept": "text/html"})
            with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as r:
                raw = r.read(max_bytes)
                enc = (r.headers.get_content_charset() or "utf-8")
                return r.geturl(), raw.decode(enc, "ignore")
        except Exception:
            continue
    # ADDITIVE (default OFF): the rebuild reads the prospect's OWN site; if a
    # bot-wall blocked the plain GET, retry with a real Chrome TLS fingerprint so
    # the mockup is built from their real content, not a fallback template.
    import os as _os
    if _os.environ.get("WEB_EXTRACT_PROSPECT_ESCALATE") == "1":
        try:
            from core import web_extract
            res = web_extract.fetch(url, timeout=timeout)
            if res.ok:
                return (res.final_url or url), res.text
        except Exception:
            pass
    return None, None


def _jsonld(html: str) -> list[dict]:
    out: list[dict] = []
    for m in re.finditer(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
                          html, re.I | re.S):
        try:
            data = json.loads(m.group(1).strip())
        except Exception:
            continue
        for node in (data if isinstance(data, list) else [data]):
            if isinstance(node, dict):
                out.append(node)
                out.extend(g for g in node.get("@graph", []) if isinstance(g, dict))
    return out


def _meta(html: str, key: str, attr: str = "property") -> str | None:
    m = re.search(rf'<meta[^>]+{attr}=["\']{re.escape(key)}["\'][^>]+content=["\']([^"\']+)', html, re.I)
    if not m:
        m = re.search(rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+{attr}=["\']{re.escape(key)}["\']', html, re.I)
    return _html.unescape(m.group(1).strip()) if m else None


def _clean_name(raw: str | None, fallback: str | None) -> str | None:
    if not raw:
        return fallback
    n = _html.unescape(raw).strip()
    n = _DIR_PREFIXES.sub("", n)
    n = _TITLE_SUFFIX.sub("", n).strip(" |-–—:")
    # cut keyword-stuffed titles: "Air Repair with 24hr service in the Tulsa, Broken Arrow, …"
    # -> "Air Repair". Drop trailing taglines/city-lists that overflow a hero + bloat the subject.
    n = re.split(r"\s+(?:with|serving|offering)\s+", n, maxsplit=1)[0].strip()
    n = n.split(",")[0].split(" - ")[0].split(" | ")[0].strip()
    if len(n) > 42:
        n = n[:42].rsplit(" ", 1)[0].strip()
    # garbage page-titles that are NOT a business name (login/error/CMS/placeholder pages) —
    # emailing "we rebuilt Authentication's site" would burn the domain. Reject to fallback.
    _GARBAGE = {"authentication", "login", "log in", "sign in", "home", "welcome", "untitled",
                "index", "error", "loading", "page not found", "404", "not found", "dashboard",
                "account", "my account", "default", "new page", "coming soon", "under construction",
                "website", "home page", "main page", "test", "example domain"}
    if (len(n) < 3 or n.lower() in _GARBAGE
            or re.search(r"contractors?|near me|services in|^[a-z]+\.(com|net|org)$", n, re.I)):
        return fallback or n
    return n


def _services(html: str, jsonld: list[dict]) -> list[str]:
    svc: list[str] = []
    for node in jsonld:
        ofc = node.get("hasOfferCatalog") or {}
        for el in (ofc.get("itemListElement") or []):
            nm = (el.get("itemOffered") or {}).get("name") or el.get("name")
            if nm:
                svc.append(nm)
    if not svc:  # fall back to nav / heading text that looks like a service list
        for m in re.finditer(r'<(?:h2|h3|li|a)[^>]*>([^<]{3,42})</', html, re.I):
            t = _html.unescape(m.group(1)).strip()
            if re.search(r"repair|install|service|clean|replace|inspect|mainten|emergenc|estimate|"
                         r"drain|heat|cool|panel|wiring|leak|septic|concrete|patio|driveway|paint", t, re.I):
                svc.append(t)
    stop = re.compile(r"terms of service|privacy|policy|sitemap|©|copyright|all rights|"
                      r"^(home|about( us)?|contact( us)?|menu|blog|login|log in|sign in|search|"
                      r"book( now)?|call( us)?( to schedule)?|get (a )?(free )?(quote|estimate)|"
                      r"read more|learn more|see more|click here|our (work|services|team)|reviews?)$", re.I)
    seen, uniq = set(), []
    for s in svc:
        k = s.lower().strip()
        if k in seen or len(s) > 46 or stop.search(k):
            continue
        seen.add(k); uniq.append(s)
    return uniq[:8]


def _phone(html: str, jsonld: list[dict], fallback: str | None) -> str | None:
    for node in jsonld:
        if node.get("telephone"):
            return str(node["telephone"]).strip()
    m = re.search(r'tel:\+?([\d\-\(\)\. ]{7,})', html)
    if m:
        return m.group(1).strip()
    return fallback


_SOCIAL_PATS = {
    "facebook":  r"https?://(?:www\.|m\.)?facebook\.com/[^\s\"'<>)]+",
    "instagram": r"https?://(?:www\.)?instagram\.com/[^\s\"'<>)]+",
    "yelp":      r"https?://(?:www\.|m\.)?yelp\.com/biz/[^\s\"'<>)]+",
    "twitter":   r"https?://(?:www\.)?(?:twitter|x)\.com/[^\s\"'<>)]+",
    "maps":      (r"https?://(?:www\.)?google\.com/maps/[^\s\"'<>)]+"
                  r"|https?://maps\.google\.com/[^\s\"'<>)]+"
                  r"|https?://maps\.app\.goo\.gl/[^\s\"'<>)]+"
                  r"|https?://goo\.gl/maps/[^\s\"'<>)]+"
                  r"|https?://g\.page/[^\s\"'<>)]+"),
}
# junk that hides in social/maps hrefs but isn't the business's real profile/place:
# FB tracking pixels + share/embed widgets, and Google Maps help/terms/contributor pages.
_SOCIAL_JUNK = re.compile(
    r"/(?:tr|sharer|share\.php|intent|plugins|dialog|login|help|contrib|policies|terms)\b"
    r"|[?&]ev=|[?&]id=\d|terms_maps|/embed", re.I)
# booking URL-path signals (matched on the HREF, not anchor text — text-only matching drags
# in facebook/home links). A real book/reserve/order-online path or a known booking host.
_BOOK_PATH_RE = re.compile(r"book|reserv|appointment|schedul|order[\s\-]*online|/order\b|booking", re.I)
_BOOK_HOST_RE = re.compile(
    r"opentable|resy|calendly|acuity|square(?:up)?\.|booksy|vagaro|clover|toasttab|"
    r"doordash|ubereats|grubhub|chownow|housecallpro|getjobber|jobber|setmore|schedulicity", re.I)


def _links(html: str, base: str) -> dict:
    """Pull the prospect's OWN real social / maps / booking links off their live page.
    Only links actually present in their HTML — never invented. Values are http(s) hrefs
    (anchors, not fetched resources, so the self-contained image guarantee is untouched)."""
    out: dict[str, str] = {}
    for key, pat in _SOCIAL_PATS.items():
        for m in re.finditer(pat, html, re.I):
            u = _html.unescape(m.group(0)).rstrip('\\"\'<>).,#')
            if _SOCIAL_JUNK.search(u):
                continue
            if key == "maps" and "/maps/" in u.lower() and not re.search(
                    r"/(place|dir|@|search)|goo\.gl|g\.page|maps\.app", u, re.I):
                continue  # a google.com/maps/... that isn't a real place/directions link
            out[key] = u
            break
    social_urls = set(out.values())
    # a real booking anchor — judged by the HREF (path or host), never by anchor text alone
    for m in re.finditer(r'<a\b[^>]*href=["\']([^"\']+)["\']', html, re.I):
        href = _html.unescape(m.group(1)).strip()
        if href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        # "faceBOOK.com" contains "book" — never let a social profile fill the booking slot
        if re.search(r"facebook\.com|instagram\.com|twitter\.com|/x\.com|\bx\.com|yelp\.com|youtube\.com|linkedin\.com", href, re.I):
            continue
        if _BOOK_PATH_RE.search(href) or _BOOK_HOST_RE.search(href):
            u = urllib.parse.urljoin(base, href).rstrip('\\"\'<>).,')
            if u.startswith(("http://", "https://")) and u not in social_urls:
                out["book"] = u
                break
    return out


def _image_urls(html: str, base: str, jsonld: list[dict]) -> list[str]:
    cand: list[str] = []
    og = _meta(html, "og:image")
    if og:
        cand.append(og)
    for node in jsonld:
        img = node.get("image") or node.get("logo")
        if isinstance(img, str):
            cand.append(img)
        elif isinstance(img, dict) and img.get("url"):
            cand.append(img["url"])
        elif isinstance(img, list):
            cand.extend(x for x in img if isinstance(x, str))
    for m in re.finditer(r'<img[^>]+(?:data-src|src)=["\']([^"\']+)', html, re.I):
        cand.append(m.group(1))
    out, seen = [], set()
    for u in cand:
        u = _html.unescape(u).strip()
        if not u or u.startswith("data:"):
            continue
        u = urllib.parse.urljoin(base, u)
        # skip tracking pixels, sprites, icons, svgs-as-icons
        if re.search(r"pixel|spacer|1x1|sprite|icon|favicon|\.svg($|\?)", u, re.I):
            continue
        if u not in seen:
            seen.add(u); out.append(u)
    return out[:12]


def inline_images(urls: list[str], base: str, max_keep: int = 5,
                  max_px: int = 1400, timeout: int = 8) -> list[dict]:
    """Fetch each image, downscale, re-encode as a data: URI. Drops failures/tiny/huge.
    Returns [{data_uri, w, h, kind}] keeping the self-contained guarantee."""
    try:
        from PIL import Image
    except Exception:
        return []
    kept: list[dict] = []
    for u in urls:
        if len(kept) >= max_keep:
            break
        try:
            req = urllib.request.Request(u, headers={"User-Agent": _UA})
            with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as r:
                blob = r.read(6_000_000)
            im = Image.open(io.BytesIO(blob))
            im.load()
            w, h = im.size
            if w < 200 or h < 120:          # too small to be real content (logo/badge/icon)
                continue
            if im.mode in ("RGBA", "P", "LA"):
                im = im.convert("RGB")
            if max(w, h) > max_px:
                im.thumbnail((max_px, max_px), Image.LANCZOS)
                w, h = im.size
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=82, optimize=True)
            data = base64.b64encode(buf.getvalue()).decode("ascii")
            if len(data) > 900_000:          # ~675KB encoded ceiling per image
                continue
            kind = "wide" if w >= h * 1.3 else ("tall" if h >= w * 1.3 else "square")
            kept.append({"data_uri": f"data:image/jpeg;base64,{data}", "w": w, "h": h, "kind": kind})
        except Exception:
            continue
    return kept


def extract(prospect: dict, do_images: bool = True) -> dict:
    """Fetch the prospect's live site and return real, cleaned content for the rebuild.
    Every field degrades to None/[] on failure so the generator can fall back cleanly."""
    url = prospect.get("url") or prospect.get("domain")
    final_url, html = fetch(url)
    result: dict = {
        "source_url": final_url, "fetched": bool(html),
        "name": prospect.get("company") or prospect.get("name"),
        "tagline": None, "phone": prospect.get("contact_phone"),
        "email": prospect.get("candidate_recipient") or prospect.get("contact_email"),
        "address": None, "hours": None, "services": [], "reviews": [], "images": [],
        "links": {},
    }
    if not html:
        return result
    # keep the raw markup (in-memory only) so the mockup can mine real brand cues
    # (theme-color meta, CSS --primary/--accent vars) — never serialized to disk.
    result["raw_html"] = html[:400_000]
    ld = _jsonld(html)
    title = (re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S) or [None, ""])[1]
    biz = next((n for n in ld if str(n.get("@type", "")).lower().endswith(("business", "localbusiness",
              "organization", "store", "professionalservice")) or "address" in n), {})
    result["name"] = (biz.get("name") or _clean_name(title, result["name"]) or result["name"])
    result["tagline"] = _meta(html, "og:description") or _meta(html, "description", "name")
    result["phone"] = _phone(html, ld, result["phone"])
    addr = biz.get("address")
    if isinstance(addr, dict):
        result["address"] = ", ".join(str(addr[k]) for k in
            ("streetAddress", "addressLocality", "addressRegion", "postalCode") if addr.get(k))
    result["hours"] = biz.get("openingHours") if isinstance(biz.get("openingHours"), (str, list)) else None
    result["services"] = _services(html, ld)
    result["links"] = _links(html, final_url or url)
    for n in ld:
        agg = n.get("aggregateRating")
        if isinstance(agg, dict) and agg.get("ratingValue"):
            result["reviews"].append({"rating": agg.get("ratingValue"), "count": agg.get("reviewCount")})
    if do_images:
        result["images"] = inline_images(_image_urls(html, final_url, ld), final_url)
    return result


if __name__ == "__main__":
    import sys
    for dom in (sys.argv[1:] or ["texmexconcrete.com", "dowlingelectric.com", "superiorsepticsandiego.com"]):
        r = extract({"domain": dom}, do_images=True)
        imgs = r.pop("images")
        print(f"\n### {dom}  fetched={r['fetched']}")
        for k, v in r.items():
            if k in ("fetched", "source_url"):
                continue
            print(f"  {k:9} = {v}")
        summary = [(im["kind"], "{}x{}".format(im["w"], im["h"]),
                    "{}KB".format(len(im["data_uri"]) // 1024)) for im in imgs]
        print("  images    = {} inlined  {}".format(len(imgs), summary))
