#!/usr/bin/env python3
"""Force-index the scannerapp.dev AI-visibility proof pages via IndexNow (Bing + Yandex instant
indexing) — the autonomous, no-GSC, no-outreach way for strangers to actually find these pages.

What it does:
  1. Resolves INDEXNOW_KEY (env -> .env) and the key file it must be served at.
  2. Builds the URL list from the geo_pages directory GLOB (every indexable /v/<slug> page on
     disk, EXCLUDING noindex/thin pages) plus the core scannerapp.dev pages.
  3. POSTs the batch (<=10000 URLs/request) to https://api.indexnow.org/indexnow and reports the
     HTTP status (200 OK / 202 Accepted = success).
  4. Best-effort sitemap ping to Bing.

  python3 -m tools.indexnow_submit            # submit all indexable pages
  python3 -m tools.indexnow_submit --dry-run  # build + verify, no POST
  python3 -m tools.indexnow_submit --limit 50 # submit first 50 (smoke test)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GEO_DIR = ROOT / "data" / "hustle" / "geo_pages"
# The /v proof pages, /intel SEO pages, and every STATIC_PATH below are served on
# scannerapp.dev (the live property), NOT hailports.com — so that's the default host.
# A wrong host silently drops every URL in submit_urls() (host filter) and force-indexes
# 404s in _build_urls(). Override with INDEXNOW_HOST only when a host actually serves them.
HOST = os.environ.get("INDEXNOW_HOST", "scannerapp.dev")
ENDPOINT = "https://api.indexnow.org/indexnow"
BATCH = 10000  # IndexNow hard cap per request

STATIC_PATHS = ["/", "/v", "/sample", "/pricing", "/tools", "/ai-visibility", "/seo-scan",
                "/site-scan", "/partners"]


def _key() -> str:
    k = os.environ.get("INDEXNOW_KEY", "").strip()
    if k:
        return k
    try:
        for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
            if line.startswith("INDEXNOW_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return ""


def _slugs() -> list[str]:
    """Every indexable page slug on disk (noindex/thin pages excluded)."""
    out: list[str] = []
    seen: set[str] = set()
    for f in sorted(GEO_DIR.glob("*.json")):
        if f.stem == "_index":
            continue
        try:
            p = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if p.get("noindex"):
            continue
        slug = p.get("slug") or f.stem
        if slug in seen:
            continue
        seen.add(slug)
        out.append(slug)
    return out


def _build_urls(limit: int | None) -> list[str]:
    urls = [f"https://{HOST}{p}" for p in STATIC_PATHS]
    urls += [f"https://{HOST}/v/{s}" for s in _slugs()]
    if limit:
        urls = urls[:limit]
    return urls


def _verify_keyfile(key: str) -> dict:
    """Confirm the key file is reachable so IndexNow can verify domain ownership. Checks the
    public URL first, then the local app via Host header (the app serves /<key>.txt by route)."""
    out = {"public": None, "local": None}
    loc = f"https://{HOST}/{key}.txt"
    # Cloudflare's WAF 403s the default python-urllib UA; real crawlers (bingbot/IndexNow) send a
    # normal UA, so verify the way they will.
    ua = {"User-Agent": "Mozilla/5.0 (compatible; HailportsBot/1.0; +https://www.hailports.com)"}
    try:
        with urllib.request.urlopen(urllib.request.Request(loc, headers=ua), timeout=8) as r:
            body = r.read().decode("utf-8", "ignore").strip()
            out["public"] = {"status": r.status, "matches": body == key}
    except Exception as e:
        out["public"] = {"error": f"{type(e).__name__}: {e}"}
    for port in (8300, 8330):
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{port}/{key}.txt", headers={"Host": HOST})
            with urllib.request.urlopen(req, timeout=5) as r:
                body = r.read().decode("utf-8", "ignore").strip()
                out["local"] = {"port": port, "status": r.status, "matches": body == key}
                break
        except Exception as e:
            out["local"] = {"port": port, "error": f"{type(e).__name__}: {e}"}
    return out


def _post(key: str, urls: list[str]) -> tuple[int, str]:
    body = json.dumps({
        "host": HOST,
        "key": key,
        "keyLocation": f"https://{HOST}/{key}.txt",
        "urlList": urls,
    }).encode()
    req = urllib.request.Request(ENDPOINT, data=body,
                                 headers={"Content-Type": "application/json; charset=utf-8"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read().decode("utf-8", "ignore")[:300]
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "ignore")[:300]
    except Exception as e:
        return 0, f"{type(e).__name__}: {e}"


def submit_urls(urls: list[str], key: str | None = None, dry_run: bool = False) -> dict:
    """Submit an explicit URL list to IndexNow (used by the content-refresh job to
    re-ping exactly the freshened pages). URLs must belong to HOST; non-matching ones
    are dropped (IndexNow rejects the whole batch otherwise)."""
    key = key or _key()
    if not key:
        return {"ok": False, "error": "no INDEXNOW_KEY"}
    urls = [u for u in dict.fromkeys(urls) if f"//{HOST}/" in u or u.startswith(f"https://{HOST}")]
    if not urls:
        return {"ok": False, "error": f"no urls for host {HOST}"}
    statuses: list = []
    submitted = 0
    for i in range(0, len(urls), BATCH):
        chunk = urls[i:i + BATCH]
        if dry_run:
            statuses.append("dry")
            continue
        status, _snip = _post(key, chunk)
        statuses.append(status)
        if status in (200, 202):
            submitted += len(chunk)
    return {"ok": (True if dry_run else all(s in (200, 202) for s in statuses)),
            "submitted": submitted, "urls": len(urls), "statuses": statuses}


def _ping_sitemap() -> dict:
    sm = f"https://{HOST}/sitemap.xml"
    res = {}
    for name, url in (("bing", f"https://www.bing.com/ping?sitemap={sm}"),):
        try:
            with urllib.request.urlopen(urllib.request.Request(url), timeout=12) as r:
                res[name] = r.status
        except urllib.error.HTTPError as e:
            res[name] = e.code
        except Exception as e:
            res[name] = f"{type(e).__name__}"
    return res


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--url", action="append", default=[], help="submit an explicit URL (repeatable)")
    ap.add_argument("--urls-file", help="newline-separated URLs to submit instead of the disk set")
    args = ap.parse_args(argv)

    key = _key()
    if not key:
        print("ERROR: no INDEXNOW_KEY (env or .env)", file=sys.stderr)
        return 2

    explicit = list(args.url)
    if args.urls_file:
        explicit += [ln.strip() for ln in Path(args.urls_file).read_text().splitlines() if ln.strip()]
    if explicit:
        res = submit_urls(explicit, key=key, dry_run=args.dry_run)
        print("SUMMARY:", json.dumps(res))
        return 0 if res.get("ok") else 1

    urls = _build_urls(args.limit or None)
    verify = _verify_keyfile(key)
    print(f"host={HOST} key={key[:8]}… key_file={json.dumps(verify)}")
    print(f"urls_to_submit={len(urls)} (indexable /v pages + {len(STATIC_PATHS)} core)")

    if args.dry_run:
        print("DRY-RUN — first 3:", urls[:3])
        print("SUMMARY:", json.dumps({"submitted": 0, "urls": len(urls), "dry_run": True}))
        return 0

    submitted = 0
    statuses = []
    for i in range(0, len(urls), BATCH):
        chunk = urls[i:i + BATCH]
        status, snippet = _post(key, chunk)
        statuses.append(status)
        ok = status in (200, 202)
        if ok:
            submitted += len(chunk)
        print(f"batch {i // BATCH + 1}: POST {len(chunk)} urls -> HTTP {status} "
              f"{'OK' if ok else 'FAIL'} {snippet!r}")

    ping = _ping_sitemap()
    print(f"sitemap ping: {json.dumps(ping)}")
    print("SUMMARY:", json.dumps({
        "submitted": submitted, "urls": len(urls),
        "statuses": statuses, "all_ok": all(s in (200, 202) for s in statuses),
        "sitemap_ping": ping,
    }))
    return 0 if statuses and all(s in (200, 202) for s in statuses) else 1


if __name__ == "__main__":
    raise SystemExit(main())
