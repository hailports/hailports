#!/usr/bin/env python3
"""
core/url_tracker.py — Rebrandly URL tracking wrapper.
Every URL that goes into any outbound email/post runs through this.
Caches links so we don't re-create duplicates.
"""
import os, re, json, hashlib, logging, requests
from pathlib import Path
from urllib.parse import urlparse

log = logging.getLogger(__name__)

REBRANDLY_KEY  = os.environ.get("REBRANDLY_API_KEY", "")
REBRANDLY_WS   = os.environ.get("REBRANDLY_WORKSPACE_ID", "")
CACHE_FILE     = Path("/home/user/claude-stack/data/hustle/rebrandly_links.json")
DOMAIN         = os.environ.get("REBRANDLY_DOMAIN", "docsapp.link")  # branded short domain

_SKIP_DOMAINS = {
    "unsubscribe", "mailto:", "tel:", "sms:", "#",
    "localhost", "127.0.0.1",
}

def _load_cache():
    try:
        return json.loads(CACHE_FILE.read_text()) if CACHE_FILE.exists() else {}
    except Exception:
        return {}

def _save_cache(cache):
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(json.dumps(cache, indent=2))

def _should_skip(url: str) -> bool:
    if not url or not url.startswith("http"):
        return True
    return any(s in url for s in _SKIP_DOMAINS)

def shorten(url: str, title: str = "", campaign: str = "") -> str:
    """Return Rebrandly short URL. Falls back to original on any error."""
    if _should_skip(url):
        return url
    if not REBRANDLY_KEY:
        return url  # no key yet, pass-through

    cache = _load_cache()
    key = hashlib.md5(url.encode()).hexdigest()
    if key in cache:
        return cache[key]["short"]

    try:
        payload = {"destination": url}
        if campaign:
            payload["tags"] = [{"name": campaign}]
        if title:
            payload["title"] = title[:100]

        headers = {
            "apikey": REBRANDLY_KEY,
            "Content-Type": "application/json",
        }
        if REBRANDLY_WS:
            headers["workspace"] = REBRANDLY_WS

        resp = requests.post(
            "https://api.rebrandly.com/v1/links",
            json=payload, headers=headers, timeout=8
        )
        if resp.status_code in (200, 201):
            data = resp.json()
            short = f"https://{data.get('domain', {}).get('fullName', 'rebrand.ly')}/{data['slashtag']}"
            cache[key] = {"original": url, "short": short, "id": data.get("id"), "campaign": campaign}
            _save_cache(cache)
            log.debug(f"[rebrandly] {url} -> {short}")
            return short
        else:
            log.warning(f"[rebrandly] {resp.status_code}: {resp.text[:120]}")
    except Exception as e:
        log.warning(f"[rebrandly] failed for {url}: {e}")
    return url

# URL regex — matches http/https URLs in text/HTML
_URL_RE = re.compile(r'https?://[^\s\'"<>\]]+', re.IGNORECASE)

def track_all(text: str, campaign: str = "") -> str:
    """Replace every URL in a block of text/HTML with Rebrandly tracked links."""
    if not REBRANDLY_KEY:
        return text
    def replace(m):
        original = m.group(0).rstrip('.,;:!?)')
        suffix = m.group(0)[len(original):]
        return shorten(original, campaign=campaign) + suffix
    return _URL_RE.sub(replace, text)

def stats(link_id: str) -> dict:
    """Fetch click stats for a link."""
    if not REBRANDLY_KEY or not link_id:
        return {}
    try:
        r = requests.get(
            f"https://api.rebrandly.com/v1/links/{link_id}/clicks",
            headers={"apikey": REBRANDLY_KEY}, timeout=8
        )
        return r.json() if r.ok else {}
    except Exception:
        return {}

def all_link_stats() -> list:
    """Pull click counts for all cached links."""
    cache = _load_cache()
    results = []
    for key, info in cache.items():
        clicks = stats(info.get("id", ""))
        results.append({**info, "clicks": clicks.get("clicks", 0)})
    return sorted(results, key=lambda x: x.get("clicks", 0), reverse=True)
