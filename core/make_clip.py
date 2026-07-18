#!/usr/bin/env python3
"""make_clip.py — reusable faceless-video generator: a business -> a finished clip.

Turns a business dict into a finished 1080x1920 social clip, fully $0 and local:

    script  (content_generator + content_quality gate, regen-until-pass)
      -> 2-3 on-brand stills  (free_image_pool: $0 CF flux / Gemini / Pollinations / local FLUX)
      -> VO narration          (ima_voice / Kokoro, offline, commercial-safe)
      -> assembled clip        (kenburns b-roll  OR  remotion branded card)

This is BOTH the fulfillment engine behind a video product AND the proof asset for
outreach. `proof_clip_for_prospect()` pulls a real broken-site prospect, builds the
clip, runs it through the anonymity + ship gates, and STAGES it (never sends — staging
and sending are separate steps per the rails).

    from core.make_clip import make_clip, proof_clip_for_prospect
    mp4 = make_clip({"name": "A1 Pumping", "vertical": "septic service",
                     "city": "Sioux Falls SD", "domain": "a1pumpingsd.com"})
    staged = proof_clip_for_prospect("a1pumpingsd.com")

Fail-soft: every entry point returns None instead of raising.
"""
from __future__ import annotations

import base64
import io
import json
import logging
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import anon_scrub  # noqa: E402
from core import free_image_pool  # noqa: E402
from core import ima_voice  # noqa: E402
from core import kenburns  # noqa: E402
from core.content_generator import generate as generate_script  # noqa: E402
from core.ship_guard import ship_ok  # noqa: E402

log = logging.getLogger("make_clip")

OUT_DIR = ROOT / "data" / "hustle" / "faceless_clips"
STAGE_DIR = ROOT / "data" / "hustle" / "proof_clips_staged"
PROSPECTS = ROOT / "data" / "hustle" / "broken_site_prospects.jsonl"

# Kokoro-local / free-only: never let a paid image backend or cloud TTS sneak onto
# this path — the whole point is a $0 fulfillment/proof engine.
os.environ.setdefault("IMAGE_GEN_ALLOW_REMOTE_PAID", "0")
os.environ.setdefault("IMA_VOICE_LOCAL_ONLY", "1")

# stills are generated smaller (both dims /16 so local mflux is happy) then
# kenburns/remotion up-scales + crops to the 1080x1920 frame, so exact size is moot.
_STILL_W, _STILL_H = 832, 1216


def _slug(s: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in (s or "").lower()).strip("-")[:48] or "clip"


# ── VO script ────────────────────────────────────────────────────────────────────
def _ad_brief(business: dict) -> str:
    vertical = business.get("vertical") or "local service"
    return (
        f"You are the confident voice-over of a 15-second local small-business ad for "
        f"{business.get('name','this business')}, a {vertical} business in "
        f"{business.get('city','town')}. Warm, direct, specific, proud-of-the-work. "
        f"2-3 short punchy sentences: name the business and town and make ONE concrete "
        f"promise a real neighbor would believe. Sound like a real local ad read — no "
        f"hashtags, no links, no corporate filler."
    )


def _fallback_scripts(business: dict) -> list[str]:
    name = business.get("name", "our team")
    city = business.get("city", "your area")
    vertical = business.get("vertical", "the work")
    return [
        f"{name} keeps {city} covered when it matters most. {vertical} done right, on "
        f"time, and priced with no surprises. call the local crew that shows up and gets "
        f"it done.",
        f"when {city} needs {vertical}, {name} answers. honest work, clear pricing, and a "
        f"finish you can be proud of. reach out today and see the difference for yourself.",
    ]


def _make_script(business: dict) -> str | None:
    """On-brand VO script that clears the $0 quality gate, or a curated fallback that
    also clears it. content_generator regenerates until the gate passes."""
    res = generate_script(
        persona="generic",
        channel="reel_script",
        topic=f"{business.get('name','')}, {business.get('vertical','')} in {business.get('city','')}",
        client_brief=_ad_brief(business),
        fallbacks={"reel_script": _fallback_scripts(business)},
        recent=[],
    )
    if not res.get("passed"):
        log.warning("make_clip: script never cleared the quality gate: %s", res.get("reasons"))
        return None
    return res["text"].strip()


# ── stills ─────────────────────────────────────────────────────────────────────
def _brand_cue(business: dict) -> str:
    colors = business.get("brand_colors")
    if isinstance(colors, (list, tuple)) and colors:
        return "brand palette " + ", ".join(str(c) for c in colors[:3])
    if isinstance(colors, str) and colors.strip():
        return f"brand palette {colors.strip()}"
    return "confident premium color palette"


def _still_specs(business: dict) -> list[tuple[str, str]]:
    """(model, prompt) pairs. 'graphic' = brand/hero title card (Gemini text-in-image),
    'flux' = photoreal service shots."""
    name = business.get("name", "Local Business")
    vertical = business.get("vertical", "local service")
    city = business.get("city", "")
    cue = _brand_cue(business)
    return [
        ("graphic",
         f"clean modern vertical brand title card for '{name}', a {vertical} business, "
         f"bold minimal sans-serif wordmark, {cue}, lots of negative space, premium local "
         f"ad look, 9:16 portrait"),
        ("flux",
         f"photorealistic professional {vertical} work in progress, local {city} small "
         f"business, natural daylight, candid documentary shot, crisp detail, 9:16 portrait"),
        ("flux",
         f"photorealistic satisfied customer with a finished {vertical} result, warm "
         f"inviting, trustworthy neighborhood business feel, 9:16 portrait"),
    ]


def _make_stills(business: dict, workdir: Path) -> list[str]:
    stills: list[str] = []
    for i, (model, prompt) in enumerate(_still_specs(business)):
        try:
            p = free_image_pool.generate(prompt, model=model, width=_STILL_W, height=_STILL_H)
        except Exception as e:  # generate never raises, belt-and-suspenders
            log.warning("make_clip: still %d failed: %s", i, e)
            p = None
        if p:
            stills.append(str(p))
    return stills


# ── authentic style: REAL photos + kinetic captions + music, NO AI stills/VO ──────
# The AI is invisible here (editing only). Real imagery in strict priority order:
#   (a) the prospect's OWN photos (scraped live site, then the rebuild mockup's embeds)
#   (b) real stock — Pexels (keyed) → Openverse/Wikimedia (keyless, commercial-safe)
#   (c) a single AI still ONLY as a last resort so a clip still ships
# Filters out logos/icons/tiny/broken images and prefers landscape real photos.

_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 authentic-clip"
_FONTS = [
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
]


def _http_get(url: str, timeout: int = 25, headers: dict | None = None) -> bytes | None:
    req = urllib.request.Request(url, headers={"User-Agent": _UA, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except Exception:
        # broken-site prospects (our whole pool) routinely fail TLS verification —
        # that's literally the defect we're pitching. We're only pulling PUBLIC images,
        # never sending secrets, so retry once with verification off to reach their
        # own photos instead of falling straight through to stock.
        try:
            import ssl
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
                return r.read()
        except Exception:
            return None


def _accept_photo(data: bytes) -> "object | None":
    """Return a landscape-ish RGB PIL image if the bytes are a real photo (not a
    logo/icon/tiny/near-square graphic), else None."""
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(data))
        im.load()
        w, h = im.size
        if w < 600 or h < 400:            # too small → logo/icon/thumbnail
            return None
        ar = w / h
        # portrait REAL photos (salon/stylist shots, GBP owner uploads) are ideal for a
        # 9:16 reel — only reject skinny sidebar banners / ultrawide strips, not portraits.
        if ar < 0.5 or ar > 2.6:
            return None
        return im.convert("RGB")
    except Exception:
        return None


def _save_photo(im, work: Path, tag: str) -> str | None:
    try:
        from PIL import Image  # noqa: F401
        p = work / f"real_{tag}.jpg"
        im.save(p, "JPEG", quality=90)
        return str(p)
    except Exception:
        return None


def _photo_seen(im, seen: set) -> bool:
    """Cheap dedupe: 8x8 average-hash. Mutates `seen`."""
    try:
        g = im.convert("L").resize((8, 8))
        px = list(g.getdata())
        avg = sum(px) / len(px)
        h = 0
        for v in px:
            h = (h << 1) | (1 if v >= avg else 0)
        if h in seen:
            return True
        seen.add(h)
        return False
    except Exception:
        return False


def _page_image_urls(page: str, base: str) -> list[str]:
    """Every plausible photo URL on a page: <img src/data-src/data-lazy>, srcset
    (largest candidate), and CSS background-image. Junk (logo/icon/…) filtered."""
    srcs: list[str] = []
    srcs += re.findall(r'<img[^>]+(?:src|data-src|data-lazy-src|data-original)=["\']([^"\']+)["\']', page, re.I)
    for pair in re.findall(r'srcset=["\']([^"\']+)["\']', page, re.I):
        # take the widest candidate in each srcset
        cands = re.findall(r'(\S+)\s+\d+w', pair) or [c.strip().split()[0] for c in pair.split(",") if c.strip()]
        if cands:
            srcs.append(cands[-1])
    srcs += re.findall(r'background-image\s*:\s*url\(["\']?([^"\')]+)', page, re.I)
    out, seen_u = [], set()
    for s in srcs:
        s = (s or "").strip()
        if not s or s.startswith("data:"):
            continue
        if re.search(r'(logo|icon|sprite|favicon|badge|button|pixel|spacer|avatar|placeholder)', s, re.I):
            continue
        u = urllib.parse.urljoin(base + "/", s)
        if u not in seen_u:
            seen_u.add(u)
            out.append(u)
    return out


def _scrape_site_images(domain: str, work: Path, want: int, seen: set) -> list[str]:
    """(b) The prospect's OWN photos — aggressive: homepage + gallery/about/service
    subpages, srcset + lazy + background-image. v2 only pulled the homepage <img src>."""
    out: list[str] = []
    if not domain:
        return out
    dom = domain.strip().lower().replace("https://", "").replace("http://", "").strip("/")
    home, base = None, None
    for scheme in ("https", "http"):  # broken-site prospects often only serve http
        home = _http_get(f"{scheme}://{dom}", timeout=18)
        if home:
            base = f"{scheme}://{dom}"
            break
    if not home:
        return out
    try:
        page0 = home.decode("utf-8", "ignore")
    except Exception:
        return out
    # follow a few internal content pages (galleries/services carry the real work photos)
    pages = [base]
    for href in re.findall(r'href=["\']([^"\']+)["\']', page0, re.I):
        if re.search(r'gallery|photo|about|service|work|project|portfolio', href, re.I):
            u = urllib.parse.urljoin(base + "/", href.strip())
            if u.startswith(("http://" + dom, "https://" + dom, base)) and u not in pages:
                pages.append(u)
    pages = pages[:6]

    urls: list[str] = _page_image_urls(page0, base)
    for pg in pages[1:]:
        data = _http_get(pg, timeout=16)
        if not data:
            continue
        try:
            urls += _page_image_urls(data.decode("utf-8", "ignore"), base)
        except Exception:
            continue
    # dedupe URL list preserving order
    seen_u, uniq = set(), []
    for u in urls:
        if u not in seen_u:
            seen_u.add(u)
            uniq.append(u)
    for u in uniq:
        if len(out) >= want:
            break
        data = _http_get(u, timeout=20)
        if not data:
            continue
        im = _accept_photo(data)
        if im and not _photo_seen(im, seen):
            p = _save_photo(im, work, f"site{len(out)}")
            if p:
                out.append(p)
    return out


def _mockup_images(mockup_path: str, work: Path, want: int, seen: set) -> list[str]:
    """(a') Prospect-derived photos embedded in the rebuild mockup (base64 data-URIs)."""
    out: list[str] = []
    try:
        if not mockup_path or not Path(mockup_path).exists():
            return out
        html = Path(mockup_path).read_text(errors="ignore")
    except Exception:
        return out
    for m in re.finditer(r'data:image/(?:jpeg|jpg|png|webp);base64,([A-Za-z0-9+/=]+)', html):
        if len(out) >= want:
            break
        try:
            data = base64.b64decode(m.group(1))
        except Exception:
            continue
        im = _accept_photo(data)
        if im and not _photo_seen(im, seen):
            p = _save_photo(im, work, f"mock{len(out)}")
            if p:
                out.append(p)
    return out


def _photo_query(business: dict) -> str:
    v = (business.get("vertical") or "local service").lower()
    for k, q in (
        ("septic", "septic truck pumping service"),
        ("plumb", "plumber working pipes"),
        ("excavat", "excavator digging construction"),
        ("hvac", "hvac technician air conditioner"),
        ("roof", "roofer working roof"),
        ("electric", "electrician working"),
        ("landscap", "landscaping crew yard"),
        ("clean", "cleaning service professional"),
        ("paint", "house painter working"),
        ("lawn", "lawn care mowing"),
        ("concrete", "concrete pouring construction"),
        ("tree", "tree service arborist"),
        ("pest", "pest control technician"),
        ("auto", "auto mechanic garage"),
    ):
        if k in v:
            return q
    return re.sub(r'\b(service|services|company|llc|inc)\b', '', v).strip() + " work"


def _pexels_images(query: str, work: Path, want: int, seen: set) -> list[str]:
    """(b) Real stock via Pexels (keyed). Landscape, commercial-safe."""
    out: list[str] = []
    key = (os.environ.get("PEXELS_API_KEY") or "").strip()
    if not key:
        try:
            for ln in (ROOT / ".env").read_text(errors="ignore").splitlines():
                if ln.startswith("PEXELS_API_KEY="):
                    key = ln.split("=", 1)[1].strip().strip("'\"")
                    break
        except Exception:
            pass
    if not key:
        return out
    url = "https://api.pexels.com/v1/search?" + urllib.parse.urlencode(
        {"query": query, "orientation": "landscape", "per_page": 20, "size": "large"})
    body = _http_get(url, timeout=25, headers={"Authorization": key})
    if not body:
        return out
    try:
        photos = json.loads(body).get("photos", [])
    except Exception:
        return out
    for ph in photos:
        if len(out) >= want:
            break
        src = (ph.get("src") or {})
        u = src.get("large2x") or src.get("large") or src.get("original")
        if not u:
            continue
        data = _http_get(u, timeout=30)
        if not data:
            continue
        im = _accept_photo(data)
        if im and not _photo_seen(im, seen):
            p = _save_photo(im, work, f"pex{len(out)}")
            if p:
                out.append(p)
    return out


def _openverse_images(query: str, work: Path, want: int, seen: set) -> list[str]:
    """(b') Keyless real stock — Openverse (federates Wikimedia/Flickr CC), commercial-only."""
    out: list[str] = []
    url = "https://api.openverse.org/v1/images/?" + urllib.parse.urlencode(
        {"q": query, "license_type": "commercial", "size": "large",
         "aspect_ratio": "wide", "page_size": 12})
    body = _http_get(url, timeout=25)
    if not body:
        return out
    try:
        results = json.loads(body).get("results", [])
    except Exception:
        return out
    for r in results:
        if len(out) >= want:
            break
        u = r.get("url") or r.get("thumbnail")
        if not u:
            continue
        data = _http_get(u, timeout=30)
        if not data:
            continue
        im = _accept_photo(data)
        if im and not _photo_seen(im, seen):
            p = _save_photo(im, work, f"ov{len(out)}")
            if p:
                out.append(p)
    return out


def _wikimedia_images(query: str, work: Path, want: int, seen: set) -> list[str]:
    """(b'') Keyless real stock — Wikimedia Commons image search, commercial-safe."""
    out: list[str] = []
    api = "https://commons.wikimedia.org/w/api.php?" + urllib.parse.urlencode({
        "action": "query", "format": "json", "generator": "search",
        "gsrsearch": f"filetype:bitmap {query}", "gsrnamespace": "6", "gsrlimit": "12",
        "prop": "imageinfo", "iiprop": "url", "iiurlwidth": "1600"})
    body = _http_get(api, timeout=25)
    if not body:
        return out
    try:
        pages = (json.loads(body).get("query", {}) or {}).get("pages", {}) or {}
    except Exception:
        return out
    for pg in pages.values():
        if len(out) >= want:
            break
        info = (pg.get("imageinfo") or [{}])[0]
        u = info.get("thumburl") or info.get("url")
        if not u:
            continue
        data = _http_get(u, timeout=30)
        if not data:
            continue
        im = _accept_photo(data)
        if im and not _photo_seen(im, seen):
            p = _save_photo(im, work, f"wiki{len(out)}")
            if p:
                out.append(p)
    return out


def _env_key(*names: str) -> str:
    """A key from the process env or .env (broken-site prospects run headless)."""
    for n in names:
        v = (os.environ.get(n) or "").strip()
        if v:
            return v
    try:
        for ln in (ROOT / ".env").read_text(errors="ignore").splitlines():
            for n in names:
                if ln.startswith(n + "="):
                    return ln.split("=", 1)[1].strip().strip("'\"")
    except Exception:
        pass
    return ""


def _upscale_gusercontent(u: str, px: int = 1600) -> str:
    """Upscale a Google photo URL to ~px. gps-cs-s URLs carry a signed param string and
    400 if you strip it, so REPLACE the size token in place and keep everything else;
    only bare /p/ URLs (no size token) get a fresh one appended."""
    if re.search(r'=w\d+-h\d+', u):
        return re.sub(r'=w\d+-h\d+', f'=w{px}-h{px}', u)
    if re.search(r'=s\d+', u):
        return re.sub(r'=s\d+', f'=s{px}', u)
    if "=" in u:  # some other param tail — replace it wholesale
        return u.split("=")[0] + f"=w{px}-h{px}"
    return f"{u}=w{px}-h{px}"


def _places_api_images(business: dict, work: Path, want: int, seen: set) -> list[str]:
    """(a) The prospect's REAL Google Business Profile photos via the Places API —
    the clean, unblockable path. No key → []. Uses Places API (New): searchText →
    place.photos → media endpoint."""
    out: list[str] = []
    key = _env_key("GOOGLE_PLACES_API_KEY", "GOOGLE_MAPS_API_KEY", "GOOGLE_API_KEY", "GMAPS_API_KEY")
    if not key:
        return out
    name = business.get("name") or ""
    city = business.get("city") or ""
    body = json.dumps({"textQuery": f"{name} {city}".strip()}).encode()
    req = urllib.request.Request(
        "https://places.googleapis.com/v1/places:searchText",
        data=body, method="POST",
        headers={"Content-Type": "application/json", "X-Goog-Api-Key": key,
                 "X-Goog-FieldMask": "places.id,places.photos", "User-Agent": _UA})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            data = json.loads(r.read())
    except Exception:
        return out
    photos = ((data.get("places") or [{}])[0] or {}).get("photos", []) or []
    for ph in photos:
        if len(out) >= want:
            break
        ref = ph.get("name")  # "places/XXX/photos/YYY"
        if not ref:
            continue
        murl = (f"https://places.googleapis.com/v1/{ref}/media"
                f"?maxHeightPx=1600&maxWidthPx=1600&key={urllib.parse.quote(key)}")
        raw = _http_get(murl, timeout=30)
        if not raw:
            continue
        im = _accept_photo(raw)
        if im and not _photo_seen(im, seen):
            p = _save_photo(im, work, f"gbp{len(out)}")
            if p:
                out.append(p)
    return out


def _gbp_scrape_images(business: dict, work: Path, want: int, seen: set) -> list[str]:
    """(a') Keyless REAL GBP photos — headless Playwright scrape of the Maps listing.
    Searches "<name> <city>", lands on the listing, and harvests the contributed-photo
    URLs (lh*.googleusercontent.com/p|gps-cs) from network + DOM, upscaled. Fail-soft:
    Playwright missing / Google blocks → []. Small businesses may have only a cover."""
    out: list[str] = []
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return out
    name = business.get("name") or ""
    city = business.get("city") or ""
    if not name:
        return out
    # broken-site scan labels look like "Duo Tones: Duluth MN Hair Salon" — the ": …"
    # tail is a descriptor, not part of the GBP name, and a messy query lands on a
    # results list instead of the place (so the hero viewer never opens). Cut it.
    name = re.split(r'[:|–—-]\s', name)[0].strip() or name
    q = f"{name} {city}".strip().replace(" ", "+")
    url = "https://www.google.com/maps/search/" + urllib.parse.quote(q, safe="+")
    rx = re.compile(r'googleusercontent\.com/(?:p/|gps-cs)')
    # keep FULL urls (params intact — gps-cs-s 400s if stripped) keyed by base path so
    # the same photo at several sizes collapses to one; pick a sized variant per base.
    by_base: "dict[str, str]" = {}

    def _remember(u: str):
        try:
            if not u or not rx.search(u):
                return
            base = u.split("=")[0]
            # prefer a variant that carries an explicit size token (upscalable)
            if base not in by_base or (re.search(r'=w\d+-h\d+|=s\d+', u)
                                       and not re.search(r'=w\d+-h\d+|=s\d+', by_base[base])):
                by_base[base] = u
        except Exception:
            pass

    try:
        with sync_playwright() as p:
            b = p.chromium.launch(headless=True, args=["--no-sandbox", "--mute-audio"])
            pg = b.new_page(locale="en-US", viewport={"width": 1300, "height": 1000})
            pg.on("response", lambda r: _remember(r.url))
            pg.goto(url, wait_until="domcontentloaded", timeout=45000)
            pg.wait_for_timeout(4000)
            for sel in ('button:has-text("Accept all")', 'button:has-text("Reject all")',
                        'form[action*="consent"] button'):
                try:
                    if pg.locator(sel).count():
                        pg.locator(sel).first.click(timeout=3000)
                        pg.wait_for_timeout(1500)
                        break
                except Exception:
                    pass
            # scroll the LEFT panel (hover it first) so review/owner photo refs lazy-load
            try:
                pg.mouse.move(275, 500)
                for _ in range(8):
                    pg.mouse.wheel(0, 1200)
                    pg.wait_for_timeout(250)
            except Exception:
                pass
            # open the hero photo VIEWER, then arrow-cycle the whole gallery. The prior
            # code clicked the hero then mouse-wheeled — which never advances the viewer,
            # so it only ever saw the cover. ArrowRight walks every contributed/owner
            # photo, each firing a network fetch the sniffer captures.
            opened = False
            for sel in ('button[aria-label^="Photo of"]', 'button[aria-label^="Photo"]',
                        'button[jsaction*="heroHeaderImage"]'):
                try:
                    if pg.locator(sel).count():
                        pg.locator(sel).first.click(timeout=4000)
                        pg.wait_for_timeout(2200)
                        opened = True
                        break
                except Exception:
                    pass
            if opened:
                # ArrowRight advances a FLAT photo viewer; when Maps opens the immersive
                # photosphere instead it just rotates, so also click an explicit "Next
                # photo" control when present. Bail early once we have plenty.
                nxt = None
                for nsel in ('button[aria-label="Next photo"]', 'button[aria-label^="Next"]',
                             'button[jsaction*="next"]'):
                    try:
                        if pg.locator(nsel).count():
                            nxt = pg.locator(nsel).first
                            break
                    except Exception:
                        pass
                for _ in range(40):
                    if len(by_base) >= want * 4:
                        break
                    try:
                        if nxt is not None:
                            nxt.click(timeout=1500)
                        else:
                            pg.keyboard.press("ArrowRight")
                        pg.wait_for_timeout(340)
                    except Exception:
                        try:
                            pg.keyboard.press("ArrowRight")
                            pg.wait_for_timeout(340)
                        except Exception:
                            break
            try:
                for s in pg.eval_on_selector_all("img", "e=>e.map(x=>x.src)"):
                    _remember(s)
            except Exception:
                pass
            b.close()
    except Exception:
        return out

    for u in list(by_base.values()):
        if len(out) >= want:
            break
        raw = _http_get(_upscale_gusercontent(u), timeout=30, headers={"Referer": "https://www.google.com/"})
        if not raw:
            continue
        im = _accept_photo(raw)
        if im and not _photo_seen(im, seen):
            p = _save_photo(im, work, f"gbp{len(out)}")
            if p:
                out.append(p)
    return out


# real-photo provenance: which sources are genuinely the PROSPECT'S own imagery
# vs generic stock vs AI. Drives the honest "theirs vs stock" tally on every clip.
# NOTE: rebuild-mockup embeds are NOT counted as "theirs" — the mockup generator
# often fills them with AI stills (observed: uniform 768x512 FLUX outputs), so they're
# treated as unverified/AI-ish and ranked below real stock, above only the AI fallback.
_OWN_SOURCES = {"gbp-api", "gbp-scrape", "own-site"}
_STOCK_SOURCES = {"pexels", "openverse", "wikimedia"}
_AI_SOURCES = {"own-mockup", "ai-fallback"}


def _authentic_images(business: dict, work: Path, want: int = 5) -> tuple[list[str], str, dict]:
    """Gather `want` REAL photos in strict priority order. Returns
    (paths, source_note, provenance) where provenance tallies theirs-vs-stock."""
    seen: set = set()
    imgs: list[str] = []
    sources: list[str] = []
    per_source: dict[str, int] = {}

    def _take(fn, label, *args):
        if len(imgs) >= want:
            return
        got = fn(*args, work, want - len(imgs), seen)
        if got:
            imgs.extend(got)
            sources.append(f"{label}:{len(got)}")
            per_source[label] = per_source.get(label, 0) + len(got)

    domain = business.get("domain") or ""
    q = _photo_query(business)
    # (a) the prospect's REAL Google Business Profile photos — MOST "that's my business"
    _take(_places_api_images, "gbp-api", business)      # clean API path (needs key)
    _take(_gbp_scrape_images, "gbp-scrape", business)   # keyless Maps scrape
    # (b) the prospect's OWN website photos — aggressive multi-page crawl
    _take(_scrape_site_images, "own-site", domain)
    # (c) real commercial-safe stock as FILL — genuinely-photographed work, never AI
    _take(_pexels_images, "pexels", q)
    _take(_openverse_images, "openverse", q)
    _take(_wikimedia_images, "wikimedia", q)
    # (d) rebuild-mockup embeds LAST (often AI stills the generator chose) — only if
    # real GBP + site + stock all came up short, so the reel never leads with AI
    _take(_mockup_images, "own-mockup", business.get("mockup_path") or "")

    # (e) last resort: a SINGLE AI still so a clip still ships (never the primary path)
    if not imgs:
        try:
            p = free_image_pool.generate(
                f"photorealistic candid documentary photo of {q}, natural daylight, 9:16",
                model="flux", width=_STILL_W, height=_STILL_H)
            if p:
                imgs.append(str(p))
                sources.append("ai-fallback:1")
                per_source["ai-fallback"] = 1
        except Exception:
            pass

    own = sum(v for k, v in per_source.items() if k in _OWN_SOURCES)
    stock = sum(v for k, v in per_source.items() if k in _STOCK_SOURCES)
    ai = sum(v for k, v in per_source.items() if k in _AI_SOURCES)
    prov = {"per_source": per_source, "own_count": own, "stock_count": stock,
            "ai_count": ai,
            "gbp_scrape_worked": per_source.get("gbp-scrape", 0) > 0,
            "gbp_api_used": per_source.get("gbp-api", 0) > 0}
    return imgs, ", ".join(sources) if sources else "none", prov


# ── kinetic captions (rendered as PNG overlays; this ffmpeg has no drawtext) ──────
def _load_font(size: int):
    from PIL import ImageFont
    for f in _FONTS:
        try:
            return ImageFont.truetype(f, size)
        except Exception:
            continue
    return ImageFont.load_default()


_STOP_TAIL = {"we", "and", "the", "a", "an", "to", "of", "in", "for", "with", "our",
              "your", "you", "so", "that", "this", "is", "are", "at", "on", "or", "but",
              "because", "when", "if", "it", "they", "he", "she"}


def _shorten(text: str, max_words: int = 8, *, clause: bool = True) -> str:
    """Trim to a clean caption: optionally cut at the first clause boundary (comma/dash),
    cap words, and drop a dangling stopword tail so it never ends on 'we'/'and'/'because'."""
    t = re.sub(r'\s+', ' ', (text or "").strip())
    t = re.split(r'[—–]', t)[0]                      # em/en dash: split anywhere
    if clause:
        t = re.split(r'\s*[,:;]\s+|\s+-\s+', t)[0]   # comma/colon/spaced-hyphen clause
    words = t.strip(".!?,").split()[:max_words]
    while words and words[-1].lower() in _STOP_TAIL:  # no dangling connector
        words.pop()
    return " ".join(words)


def _caption_lines(script: str, business: dict) -> list[str]:
    """3 punchy, specific, human caption cards derived from the gate-passing script."""
    name = business.get("name") or "our crew"
    city = business.get("city") or "your area"
    sents = [s.strip() for s in re.split(r'(?<=[.!?])\s+', (script or "").strip()) if s.strip()]
    hook = _shorten(sents[0], 7) if sents else f"{name}"
    if len(hook.split()) < 2:                        # too thin → concrete local hook
        hook = f"{name} — {city}"
    value = _shorten(sents[1], 8, clause=False) if len(sents) > 1 else _shorten(name, 6)
    cta_src = sents[-1] if sents else ""
    cta = _shorten(cta_src, 7) if re.search(r'call|book|reach|get|today|now', cta_src, re.I) \
        else f"call {name} today"
    lines = [hook, value, cta]
    # de-dup / drop empties, keep order
    out, low = [], set()
    for l in lines:
        k = l.lower()
        if l and k not in low:
            out.append(l)
            low.add(k)
    return out or [f"{name} — {city}"]


# Vertical safe area (fraction of 1920px height) — captions live strictly inside
# this band so the phone UI (top clock/notch, bottom nav + engagement rail) never
# clips them. MIN_EDGE is the hard "no glyph within N px of any frame edge" rule.
_CAP_MARGIN_X = 80          # ≥80px side margins → text lives in ~85% of frame width
_CAP_SAFE_TOP = 0.12        # never above 12% of height
_CAP_SAFE_BOT = 0.82        # never below 82% of height
_CAP_MIN_EDGE = 40          # provable: no rendered glyph within 40px of any edge


def _wrap_to_width(draw, text: str, font, max_w: float) -> list[str]:
    """Greedy word-wrap; hard-break any single word that alone exceeds max_w."""
    lines: list[str] = []
    cur = ""
    for w in (text or "").split():
        test = (cur + " " + w).strip()
        if draw.textlength(test, font=font) <= max_w:
            cur = test
            continue
        if cur:
            lines.append(cur)
        if draw.textlength(w, font=font) <= max_w:
            cur = w
        else:  # single over-long word → break it char-wise
            piece = ""
            for ch in w:
                if draw.textlength(piece + ch, font=font) <= max_w:
                    piece += ch
                else:
                    if piece:
                        lines.append(piece)
                    piece = ch
            cur = piece
    if cur:
        lines.append(cur)
    return lines or [""]


def _render_caption_png(text: str, out: str, *, size=(1080, 1920),
                        accent="#ffd166", lower_third: bool = True) -> dict | None:
    """Render a caption as a 1080x1920 RGBA overlay that is PROVABLY inside the
    vertical safe area.

    Word-wraps to ≤ (W - 2*80)px, auto-shrinks the font until the longest line fits
    AND the wrapped block fits the 12%–82% vertical band, then positions the block as
    a lower-third (or centered). Text + accent are drawn on their own transparent
    layer so the block's true glyph bbox can be measured independent of the scrim;
    the returned dict carries that bbox and a `safe` flag asserting no glyph lands
    within 40px of any edge. Returns None only on hard failure.
    """
    try:
        from PIL import Image, ImageDraw
        W, H = size
        margin = _CAP_MARGIN_X
        max_w = W - 2 * margin
        safe_top = int(H * _CAP_SAFE_TOP)
        safe_bot = int(H * _CAP_SAFE_BOT)
        avail_h = safe_bot - safe_top

        probe = ImageDraw.Draw(Image.new("RGBA", (W, H)))
        bar_h, bar_gap = 12, 26

        chosen = None
        for size_px in range(104, 34, -4):
            font = _load_font(size_px)
            lines = _wrap_to_width(probe, text, font, max_w)
            asc, desc = font.getmetrics()
            lh = int((asc + desc) * 1.24)
            block_h = bar_h + bar_gap + lh * len(lines)
            longest = max((probe.textlength(l, font=font) for l in lines), default=0)
            if longest <= max_w and block_h <= avail_h and len(lines) <= 4:
                chosen = (size_px, font, lines, lh, block_h)
                break
        if chosen is None:  # smallest attempt, still clamp line count
            size_px = 36
            font = _load_font(size_px)
            lines = _wrap_to_width(probe, text, font, max_w)[:4]
            asc, desc = font.getmetrics()
            lh = int((asc + desc) * 1.24)
            block_h = min(avail_h, bar_h + bar_gap + lh * len(lines))
            chosen = (size_px, font, lines, lh, block_h)
        size_px, font, lines, lh, block_h = chosen

        # vertical placement: lower-third sits with its BASE at ~80% of height,
        # centered mode straddles ~62%. Clamp fully inside the safe band.
        if lower_third:
            y0 = int(H * 0.80) - block_h
        else:
            y0 = int(H * 0.62) - block_h // 2
        y0 = max(safe_top, min(y0, safe_bot - block_h))

        # scrim (own layer, may reach the edges — it's decoration, not measured)
        scrim = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        sd = ImageDraw.Draw(scrim)
        scrim_top = max(0, y0 - int(H * 0.10))
        for yy in range(scrim_top, H):
            t = (yy - scrim_top) / max(1, H - scrim_top)
            sd.line([(0, yy), (W, yy)], fill=(0, 0, 0, int(205 * t)))

        # text + accent (own transparent layer → its bbox is the true glyph extent)
        txt = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        td = ImageDraw.Draw(txt)
        try:
            ac = tuple(int(accent.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4)) + (255,)
        except Exception:
            ac = (255, 209, 102, 255)
        bar_w = 128
        bx = (W - bar_w) // 2
        td.rounded_rectangle([bx, y0, bx + bar_w, y0 + bar_h], radius=bar_h // 2, fill=ac)
        y = y0 + bar_h + bar_gap
        for line in lines:
            tw = td.textlength(line, font=font)
            x = (W - tw) / 2
            td.text((x + 3, y + 3), line, font=font, fill=(0, 0, 0, 210))
            td.text((x, y), line, font=font, fill=(255, 255, 255, 255))
            y += lh

        bbox = txt.getbbox()  # tight extent of every non-transparent (glyph/bar) pixel
        safe = bool(bbox and bbox[0] >= _CAP_MIN_EDGE and bbox[1] >= _CAP_MIN_EDGE
                    and bbox[2] <= W - _CAP_MIN_EDGE and bbox[3] <= H - _CAP_MIN_EDGE)

        Image.alpha_composite(scrim, txt).save(out, "PNG")
        # sidecar text-only layer = the provable artifact the assertion re-checks
        txt.save(str(Path(out).with_suffix("")) + ".textonly.png", "PNG")
        return {"png": out, "textonly": str(Path(out).with_suffix("")) + ".textonly.png",
                "bbox": bbox, "safe": safe, "font_px": size_px, "lines": lines, "text": text}
    except Exception as e:
        log.warning("make_clip: caption render failed: %s", e)
        return None


def verify_caption_safe(textonly_png: str, size=(1080, 1920)) -> dict:
    """Re-open a caption's text-only layer and PROVE no glyph is within 40px of any
    edge. Independent of the render call — reads the saved artifact fresh."""
    from PIL import Image
    W, H = size
    try:
        bbox = Image.open(textonly_png).getbbox()
    except Exception as e:
        return {"png": textonly_png, "safe": False, "error": str(e)}
    if not bbox:
        return {"png": textonly_png, "safe": False, "bbox": None}
    l, t, r, b = bbox
    ok = (l >= _CAP_MIN_EDGE and t >= _CAP_MIN_EDGE
          and r <= W - _CAP_MIN_EDGE and b <= H - _CAP_MIN_EDGE)
    return {"png": textonly_png, "safe": ok, "bbox": bbox,
            "min_edge_px": min(l, t, W - r, H - b)}


# ── music bed (free, commercial-safe; $0) ─────────────────────────────────────────
def _gen_music_bed(work: Path, seconds: float = 20.0) -> tuple[str | None, str]:
    """Guaranteed $0 no-attribution bed: an ffmpeg-synthesized soft lo-fi pad (three
    detuned sine partials + gentle tremolo + lowpass). Public-domain BY CONSTRUCTION —
    we author it, so zero license/attribution risk. Used only when no CC0 track fetches."""
    out = work / "music.m4a"
    fc = (
        "sine=frequency=220:sample_rate=44100[a];"
        "sine=frequency=277:sample_rate=44100[b];"
        "sine=frequency=330:sample_rate=44100[c];"
        "[a][b][c]amix=inputs=3:normalize=1,"
        "tremolo=f=5:d=0.5,lowpass=f=1200,volume=0.5,"
        f"afade=t=in:st=0:d=1.5,afade=t=out:st={max(0.0, seconds-1.5):.2f}:d=1.5[m]"
    )
    cmd = ["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc", "-filter_complex", fc,
           "-map", "[m]", "-t", f"{seconds:.2f}", "-c:a", "aac", "-b:a", "160k", str(out)]
    try:
        import subprocess as _sp
        r = _sp.run(cmd, capture_output=True, text=True, timeout=60)
        if r.returncode == 0 and out.exists() and out.stat().st_size > 10_000:
            return str(out), "generated-ambient (public domain by construction, no attribution)"
    except Exception:
        pass
    return None, ""


def _music_bed(work: Path) -> tuple[str | None, str]:
    """A free, commercial-safe music track — CC0/no-attribution preferred.
    Order: local library → Openverse strict CC0 → Free Music Archive CC0 →
    generated ambient (guaranteed $0, public-domain by construction). The note names
    the EXACT license so attribution obligations are never silently shipped."""
    # (1) local library — honor any operator-dropped track
    for cand in (ROOT / "data" / "hustle" / "music", ROOT / "assets" / "music"):
        if cand.exists():
            for ext in ("*.mp3", "*.m4a", "*.wav"):
                hits = sorted(cand.glob(ext))
                if hits:
                    return str(hits[0]), f"local-library:{hits[0].name} (verify license locally)"
    # (2) Openverse audio filtered to the EXACT cc0 license (no attribution required)
    url = "https://api.openverse.org/v1/audio/?" + urllib.parse.urlencode(
        {"q": "calm ambient background", "license": "cc0", "page_size": 12})
    body = _http_get(url, timeout=25)
    if body:
        try:
            results = json.loads(body).get("results", [])
        except Exception:
            results = []
        for r in results:
            if (r.get("license") or "").lower() != "cc0":
                continue
            u = r.get("url")
            data = _http_get(u, timeout=40) if u else None
            if data and len(data) > 40_000:
                p = work / "music_cc0.mp3"
                try:
                    p.write_bytes(data)
                    return str(p), f"openverse-cc0 (no attribution):{(r.get('title') or 'track')[:40]}"
                except Exception:
                    continue
    # (3) Free Music Archive — CC0 curation
    fma = "https://freemusicarchive.org/api/trackSearch?" + urllib.parse.urlencode(
        {"q": "ambient", "license": "cc0", "limit": 10})
    body = _http_get(fma, timeout=20)
    if body:
        try:
            for tr in (json.loads(body).get("aTracks") or [])[:10]:
                u = tr.get("track_file_url") or tr.get("track_url")
                if not u or "cc0" not in json.dumps(tr).lower():
                    continue
                data = _http_get(u, timeout=40)
                if data and len(data) > 40_000:
                    p = work / "music_fma.mp3"
                    p.write_bytes(data)
                    return str(p), f"fma-cc0 (no attribution):{(tr.get('track_title') or 'track')[:40]}"
        except Exception:
            pass
    # (4) generated ambient — guaranteed, no license/attribution at all
    return _gen_music_bed(work)


def _assemble_authentic(business: dict, script: str, out: str, work: Path,
                        meta: dict) -> Path | None:
    imgs, src, prov = _authentic_images(business, work, want=5)
    meta["image_source"] = src
    meta["image_count"] = len(imgs)
    meta["provenance"] = prov
    meta["own_photos"] = prov.get("own_count", 0)
    meta["stock_photos"] = prov.get("stock_count", 0)
    meta["ai_photos"] = prov.get("ai_count", 0)
    if not imgs:
        log.warning("make_clip[authentic]: no real images sourced")
        return None
    n = len(imgs)
    # slightly faster cuts than v2 (was 3.2–4.6s) → snappier CapCut-style pacing
    per = max(2.6, min(3.6, 13.0 / n + 0.4))  # keep total ~11-16s
    base = str(work / "authentic_base.mp4")
    if not kenburns.stills_to_clip(imgs, None, base, per_still=per,
                                   xfade=0.4, zoom_step=0.0015):
        return None
    dur = kenburns._probe_duration(base)
    caps = _caption_lines(script, business)
    meta["captions"] = caps
    cap_specs = []
    cap_safety = []
    seg = dur / max(1, len(caps))
    for i, text in enumerate(caps):
        png = str(work / f"cap_{i}.png")
        r = _render_caption_png(text, png)
        if r:
            # PROVE safe area from the freshly-saved artifact, not the render's own claim
            v = verify_caption_safe(r["textonly"])
            cap_safety.append({"i": i, "text": text, "font_px": r["font_px"],
                               "lines": r["lines"], "bbox": v.get("bbox"),
                               "min_edge_px": v.get("min_edge_px"), "safe": v["safe"]})
            cap_specs.append({"png": png, "start": i * seg + 0.2, "end": (i + 1) * seg - 0.2})
    meta["caption_safe_area"] = cap_safety
    meta["caption_all_safe"] = bool(cap_safety) and all(c["safe"] for c in cap_safety)
    music, mnote = _music_bed(work)
    meta["music"] = mnote
    if not music:
        log.warning("make_clip[authentic]: shipping silent cut — %s", mnote)
    res = kenburns.overlay_captions_music(base, cap_specs, music, out)
    return Path(res) if res and Path(res).exists() else None


# ── rebuild_reveal style: THEIR animated rebuilt site is the hero ─────────────────
# The strongest answer to "reeks of stock": the reel leads with the prospect's OWN
# rebuilt site scrolling inside a phone (before->after with their broken live site),
# captions + music around it, and their real GBP/site photos only as SECONDARY b-roll.
def _mockup_brand(mockup_path: str) -> dict:
    """Pull the REAL business name + brand palette the rebuild was skinned in, from the
    mockup's sidecar manifest. This makes the reel unmistakably a custom ad for THEIR
    business (real name + their colors), and sidesteps any hallucinated script name."""
    out = {"name": None, "primary": "#141413", "accent": "#d97757"}
    try:
        mp = Path(mockup_path)
        man = mp.with_name(mp.stem + ".manifest.json")
        if man.exists():
            m = json.loads(man.read_text(errors="ignore"))
            out["name"] = m.get("name") or None
            br = m.get("brand") or {}
            if br.get("primary"):
                out["primary"] = br["primary"]
            if br.get("accent"):
                out["accent"] = br["accent"]
    except Exception:
        pass
    return out


def _assemble_rebuild_reveal(business: dict, script: str, out: str, work: Path,
                             meta: dict) -> Path | None:
    """THE product: a studio-style bespoke ad of THEIR rebuilt site. Before (their real
    broken/dated site, dull-graded) -> flash cut -> after (the bespoke rebuild scrolling
    in a phone, vibrant-graded, their brand accent) -> short real-photo b-roll -> a
    brand-colored endcard. Kinetic captions + music over the top. Nobody can prompt this
    in ChatGPT — it's a custom redesign of one specific business, animated."""
    from core import site_video

    mockup_path = business.get("mockup_path") or ""
    if not mockup_path or not Path(mockup_path).exists():
        log.warning("make_clip[rebuild_reveal]: no valid mockup_path — cannot lead with rebuild")
        return None

    brand = _mockup_brand(mockup_path)
    real_name = brand["name"] or business.get("name") or "your business"
    brand["name"] = real_name
    accent = brand["accent"]
    meta["brand"] = brand
    meta["real_name"] = real_name

    # PRIMARY renderer: Remotion (frame-perfect studio motion — camera push-in, parallax,
    # beat-timed kinetic type, their real palette + logo). Un-gettable from a generic prompt.
    remo = site_video.remotion_reveal_clip(business, brand, out, work, meta=meta)
    if remo and Path(remo).exists():
        return Path(remo)
    log.warning("make_clip[rebuild_reveal]: Remotion path unavailable/failed — "
                "using the ffmpeg edited fallback")
    meta["renderer"] = "ffmpeg-fallback"

    # (1) HERO: before (their live broken site) -> flash -> after (bespoke rebuild scroll)
    domain = (business.get("domain") or "").strip()
    hero = str(work / "hero.mp4")
    hero_res = site_video.before_after_clip(
        domain, mockup_path, hero, viewport="mobile", accent=accent,
        before_seconds=2.6, after_seconds=6.8, work=str(work / "hero_w"))
    if not hero_res or not Path(hero_res).exists():
        log.warning("make_clip[rebuild_reveal]: hero (mockup scroll) failed")
        return None
    meta["hero"] = "before_after_mockup_scroll"
    meta["before_after"] = bool(domain)
    meta["mockup_path"] = mockup_path
    segments = [str(hero_res)]

    # (2) SECONDARY b-roll: their REAL photos (GBP/site) as a short tail — the rebuild
    # stays the star. Fail-soft; stock only fills, never leads here.
    try:
        imgs, src, prov = _authentic_images(business, work, want=2)
    except Exception:
        imgs, src, prov = [], "none", {}
    meta["broll_image_source"] = src
    meta["provenance"] = prov
    meta["own_photos"] = prov.get("own_count", 0)
    meta["stock_photos"] = prov.get("stock_count", 0)
    meta["ai_photos"] = prov.get("ai_count", 0)
    if imgs:
        tail = str(work / "broll_tail.mp4")
        if kenburns.stills_to_clip(imgs[:2], None, tail, per_still=2.0, xfade=0.4,
                                   zoom_step=0.0018):
            segments.append(tail)

    # (3) branded ENDCARD in their real palette + name + CTA
    endcard = str(work / "endcard.mp4")
    ec = site_video.endcard_clip(real_name, "book now", brand["primary"], accent,
                                 endcard, seconds=2.2, work=str(work / "end_w"))
    if ec:
        segments.append(str(ec))
    meta["endcard"] = bool(ec)

    base = str(work / "reveal_base.mp4")
    based = site_video.concat_segments(segments, base)
    if not based or not Path(based).exists():
        log.warning("make_clip[rebuild_reveal]: base concat failed")
        return None
    dur = kenburns._probe_duration(str(based))
    if dur <= 0:
        return None

    # (4) KINETIC captions built from the REAL name (not the script) so the branding is
    # correct: a value line over the rebuild, a CTA over the endcard. Brand-accent bar.
    city = business.get("city") or ""
    value = f"{real_name} — rebuilt" if real_name else "your site, rebuilt"
    cta = f"call {real_name} today" if real_name and len(real_name) < 24 else "book online today"
    picks = [(_shorten(value, 7), 3.4, min(dur * 0.5, 6.2)),
             (_shorten(cta, 7), max(dur - 2.2, dur * 0.66), dur - 0.35)]
    meta["captions"] = [p[0] for p in picks]
    cap_specs, cap_safety = [], []
    for i, (text, start, end) in enumerate(picks):
        if not text:
            continue
        png = str(work / f"rr_cap_{i}.png")
        r = _render_caption_png(text, png, accent=accent)
        if not r:
            continue
        v = verify_caption_safe(r["textonly"])
        cap_safety.append({"i": i, "text": text, "font_px": r["font_px"],
                           "bbox": v.get("bbox"), "min_edge_px": v.get("min_edge_px"),
                           "safe": v["safe"]})
        cap_specs.append({"png": png, "start": start, "end": end, "slide": 30})
    meta["caption_safe_area"] = cap_safety
    meta["caption_all_safe"] = bool(cap_safety) and all(c["safe"] for c in cap_safety)

    music, mnote = _music_bed(work)
    meta["music"] = mnote
    if not music:
        log.warning("make_clip[rebuild_reveal]: shipping silent cut — %s", mnote)
    res = site_video.finish_reel(base, cap_specs, music, out)
    return Path(res) if res and Path(res).exists() else None


# ── assembly ─────────────────────────────────────────────────────────────────────
def _assemble_branded(business: dict, stills: list[str], vo_wav: str, out: str,
                      script: str) -> str | None:
    from core import remotion_video
    if not remotion_video.configured():
        log.warning("make_clip: remotion not configured; falling back to broll")
        return kenburns.stills_to_clip(stills, vo_wav, out)
    hook = script.split(".")[0].strip()[:90] or business.get("name", "")
    props = {
        "hook": hook,
        "cta": "call today",
        "brand": business.get("name", ""),
        "images": stills,
        "audio": vo_wav,
    }
    colors = business.get("brand_colors")
    if isinstance(colors, (list, tuple)) and len(colors) >= 2:
        props["bg"], props["accent"] = str(colors[0]), str(colors[1])
    return remotion_video.render(props, out)


def make_clip(business: dict, script: str | None = None, style: str = "broll",
              out: str | None = None) -> Path | None:
    """Turn a business dict into a finished 1080x1920 mp4. Fail-soft (returns None).

    business : {name, vertical, city, domain, brand_colors?, mockup_path?}
    script   : VO/caption text; if None, an on-brand gate-passing ad script is generated.
    style    : 'broll' (kenburns motion from AI stills + VO), 'branded' (remotion card),
               'authentic' (REAL photos + kinetic captions + music, NO AI stills/VO —
               the AI is invisible, doing only the editing), or 'rebuild_reveal'
               (THEIR animated rebuilt site is the hero: before->after phone-scroll of
               their broken live site into the rebuild mockup, captions + music around
               it, real photos only as secondary b-roll; needs business['mockup_path']).
    out      : output path; defaults under data/hustle/faceless_clips/.
    """
    try:
        if not isinstance(business, dict) or not business.get("name"):
            log.warning("make_clip: business dict needs at least a name")
            return None

        script = (script or "").strip() or _make_script(business)
        if not script:
            return None

        OUT_DIR.mkdir(parents=True, exist_ok=True)
        stem = f"{_slug(business.get('name'))}_{datetime.now():%Y%m%d_%H%M%S}"
        out_path = Path(out) if out else OUT_DIR / f"{stem}.mp4"
        work = OUT_DIR / stem
        work.mkdir(parents=True, exist_ok=True)

        if style == "rebuild_reveal":
            # THEIR animated rebuilt site is the star (before->after phone scroll)
            meta: dict = {}
            business["_authentic_meta"] = meta
            res = _assemble_rebuild_reveal(business, script, str(out_path), work, meta)
            if not res or not Path(res).exists():
                log.warning("make_clip: rebuild_reveal assembly failed")
                return None
            return Path(res)

        if style == "authentic":
            # real imagery + on-screen text + music; NO AI stills, NO robot voice-over
            meta = {}
            business["_authentic_meta"] = meta
            res = _assemble_authentic(business, script, str(out_path), work, meta)
            if not res or not Path(res).exists():
                log.warning("make_clip: authentic assembly failed")
                return None
            return Path(res)

        stills = _make_stills(business, work)
        if not stills:
            log.warning("make_clip: no stills produced")
            return None

        vo_wav = str(work / "vo.wav")
        if not ima_voice.speak(script, Path(vo_wav)):
            log.warning("make_clip: VO synthesis failed")
            vo_wav = None  # kenburns/remotion still produce a silent clip

        if style == "branded":
            res = _assemble_branded(business, stills, vo_wav, str(out_path), script)
        else:
            res = kenburns.stills_to_clip(stills, vo_wav, str(out_path))

        if not res or not Path(res).exists():
            log.warning("make_clip: assembly failed")
            return None
        return Path(res)
    except Exception as e:
        log.warning("make_clip: unexpected failure: %s", e)
        return None


# ── prospect proof asset (STAGES only) ─────────────────────────────────────────────
def _load_prospect(domain: str) -> dict | None:
    domain = (domain or "").strip().lower()
    if not domain or not PROSPECTS.exists():
        return None
    try:
        with open(PROSPECTS) as f:
            for line in f:
                line = line.strip()
                if not line or domain not in line:
                    continue
                rec = json.loads(line)
                if (rec.get("domain") or "").strip().lower() == domain:
                    return rec
    except Exception as e:
        log.warning("make_clip: prospect lookup failed: %s", e)
    return None


def _publish_gate(business: dict, script: str) -> tuple[bool, list[str]]:
    """Fail-closed anonymity + ship gate on the CLIP'S TEXT before it's a deliverable.

    anon_scrub's $0 deterministic layer is the operator-identity firewall (name /
    employer / faceless brands / method / PII); the client's own business name+town are
    legitimately on a proof clip made FOR them, so we gate on operator-leak + ship-slop,
    NOT the paid judge (which would false-block a real client name and isn't $0). The
    prospect domain is deliberately kept OUT of gated text (it trips the domain regex)."""
    gated_text = "\n".join(
        str(business.get(k, "")) for k in ("name", "vertical", "city")
    ) + "\n" + script
    ok_anon, leaks = anon_scrub.verdict(gated_text)
    ok_ship, ship_issues = ship_ok(script)
    hard_ship = [i for i in ship_issues if not i.startswith("(warn)")]
    reasons = ([f"anon-leak:{l}" for l in leaks] + [f"ship:{i}" for i in hard_ship])
    return (ok_anon and ok_ship and not hard_ship), reasons


def proof_clip_for_prospect(domain: str, out_dir: str | None = None,
                            style: str = "broll") -> Path | None:
    """Build a proof clip from a real broken-site prospect and STAGE it (never send).

    Pulls the prospect's real business data, builds the clip, runs it through the
    anonymity + ship gates, then copies the passing clip into the staging dir with a
    manifest note. `style` selects the recipe ('rebuild_reveal' = THEIR animated rebuilt
    site leads, before->after phone scroll; 'authentic' = real photos + kinetic captions
    + music, no AI-generated footage or robot voice). Returns the STAGED mp4 path, or None.

    Default behavior: a broken-site prospect that already HAS a valid rebuild mockup is
    auto-upgraded to 'rebuild_reveal' so the reel leads with their animated rebuild — the
    strongest, uniquely-theirs hero. Falls back to 'authentic' if the reveal build fails."""
    try:
        rec = _load_prospect(domain)
        if not rec:
            log.warning("proof_clip_for_prospect: no prospect for %s", domain)
            return None

        business = {
            "name": rec.get("name") or rec.get("domain"),
            "vertical": rec.get("vertical") or "local service",
            "city": rec.get("city") or "",
            "domain": rec.get("domain"),
        }
        # lead with their animated rebuild when a valid mockup exists (default upgrade)
        has_mockup = bool(rec.get("mockup_path") and Path(rec["mockup_path"]).exists()
                          and rec.get("mockup_valid", True))
        auto_upgraded = False
        if style == "broll" and has_mockup:
            style = "rebuild_reveal"
            auto_upgraded = True
            log.info("proof_clip_for_prospect: upgraded %s -> rebuild_reveal (has mockup)", domain)
        # rebuild_reveal + authentic both source the prospect's OWN photos + rebuild mockup
        if style in ("authentic", "rebuild_reveal") and rec.get("mockup_path"):
            business["mockup_path"] = rec["mockup_path"]

        script = _make_script(business)
        if not script:
            return None

        ok, reasons = _publish_gate(business, script)
        if not ok:
            log.warning("proof_clip_for_prospect: gate BLOCKED %s: %s", domain, reasons)
            return None

        t0 = time.time()
        clip = make_clip(business, script=script, style=style)
        # fail-soft: if the auto-upgraded reveal couldn't build, drop to authentic
        if not clip and auto_upgraded:
            log.warning("proof_clip_for_prospect: rebuild_reveal failed for %s — falling back to authentic", domain)
            style = "authentic"
            clip = make_clip(business, script=script, style=style)
        wall = round(time.time() - t0, 1)
        if not clip:
            return None
        auth_meta = business.get("_authentic_meta") or {}

        stage = Path(out_dir) if out_dir else STAGE_DIR
        stage.mkdir(parents=True, exist_ok=True)
        suffix = {"authentic": "_v2_authentic",
                  "rebuild_reveal": "_rebuild_reveal"}.get(style, "")
        staged = stage / f"{_slug(business['name'])}_{_slug(rec.get('domain',''))}{suffix}.mp4"
        import shutil
        shutil.copy2(clip, staged)

        note = {
            "staged_at": datetime.now().isoformat(timespec="seconds"),
            "domain": rec.get("domain"),
            "business": business["name"],
            "vertical": business["vertical"],
            "city": business["city"],
            "style": style,
            "clip": str(staged),
            "script": script,
            "gate": {"anon_scrub": "clean", "ship_guard": "pass"},
            "wall_seconds": wall,
            "cost_usd": 0.0,
            "status": "STAGED — not sent (send is a separate gated step)",
        }
        if auth_meta:
            note["authentic"] = auth_meta
        staged.with_suffix(".json").write_text(json.dumps(note, indent=2))
        with open(stage / "STAGED_MANIFEST.jsonl", "a") as f:
            f.write(json.dumps(note) + "\n")
        log.info("proof_clip_for_prospect: STAGED %s (%.1fs, $0)", staged, wall)
        return staged
    except Exception as e:
        log.warning("proof_clip_for_prospect: unexpected failure: %s", e)
        return None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    import argparse
    ap = argparse.ArgumentParser(description="faceless-video generator")
    ap.add_argument("--prospect", help="broken-site prospect domain -> staged proof clip")
    ap.add_argument("--name")
    ap.add_argument("--vertical")
    ap.add_argument("--city")
    ap.add_argument("--style", default="broll",
                    choices=["broll", "branded", "authentic", "rebuild_reveal"])
    args = ap.parse_args()
    if args.prospect:
        print(proof_clip_for_prospect(args.prospect, style=args.style))
    elif args.name:
        print(make_clip({"name": args.name, "vertical": args.vertical or "local service",
                         "city": args.city or ""}, style=args.style))
    else:
        ap.print_help()
