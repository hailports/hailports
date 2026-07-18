#!/usr/bin/env python3
"""Deterministic "what's fixable on this site" probe — layer 2 of Broken-Site Rescue.

web_failure_probe.py catches sites that are DOWN/insecure. This catches sites that are
UP but leaving money on the table: slow, no analytics, no SEO structured data, no
sitemap, bad social previews, outdated CMS, missing favicon. Each flag is an objective,
owner-verifiable fact paired with a sellable angle — the cold-outreach hook ("your home
page takes 5.2s to load" / "you have no analytics so you're flying blind").

$0 and autonomous: pure HTML/header inspection plus one FREE Google PageSpeed (CrUX/PSI)
call with a timing fallback. NO LLM calls, NO API key required. Reuses the SSRF-safe
fetch and normalizer from web_failure_probe so it inherits the same internal-host block.

  python3 -m core.site_quality_probe example.com stripe.com
  python3 -m core.site_quality_probe --file data/hustle/biz_domains.txt --gaps-only --json
"""
from __future__ import annotations

import json
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from core.web_failure_probe import _fetch, _norm

PSI_TIMEOUT = 20
SLOW_FETCH_SECONDS = 3.0
PSI_PERF_POOR = 0.5      # PSI performance score 0-1; <0.5 = poor (Google's "red" band)
PSI_PERF_OK = 0.9        # >=0.9 = good, don't flag

_SEV_RANK = {"ok": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}


def _bump(current: str, want: str) -> str:
    return want if _SEV_RANK.get(want, 0) > _SEV_RANK.get(current, 0) else current


def _psi_performance(url: str):
    """Google PageSpeed (CrUX/PSI) performance score 0-1, or None if unavailable.
    Free, keyless at low volume; a 429/403/needs-key just returns None so the caller
    falls back to raw fetch timing. Never raises."""
    api = ("https://www.googleapis.com/pagespeedonline/v5/runPagespeed?url="
           + urllib.parse.quote(url, safe="") + "&category=performance&strategy=mobile")
    host = urllib.parse.urlparse(api).hostname or ""
    # googleapis.com is public; defensive parse only, no SSRF risk on a fixed host.
    try:
        req = urllib.request.Request(api, headers={"User-Agent": "SiteQualityBot/1.0"})
        with urllib.request.urlopen(req, timeout=PSI_TIMEOUT) as r:
            data = json.loads(r.read(2_000_000).decode("utf-8", "ignore"))
        score = (data.get("lighthouseResult", {})
                     .get("categories", {})
                     .get("performance", {})
                     .get("score"))
        if isinstance(score, (int, float)):
            return float(score)
    except Exception:
        return None
    return None


def _check_performance(host: str, fetch_seconds, flags: list):
    """Flag #1: slow / poor Core Web Vitals."""
    try:
        score = _psi_performance(f"https://{host}")
        if score is not None:
            if score < PSI_PERF_POOR:
                flags.append({
                    "signal": "slow_performance",
                    "fact": f"Google PageSpeed performance score {int(score * 100)}/100 (mobile) — poor Core Web Vitals",
                    "angle": "Slow pages bleed conversions and rank lower on Google; a speed pass is a quick, measurable win.",
                })
            return  # PSI authoritative; skip timing fallback
    except Exception:
        pass
    # Fallback: raw fetch timing (only flag a clearly slow page, conservative).
    try:
        if fetch_seconds is not None and fetch_seconds > SLOW_FETCH_SECONDS:
            flags.append({
                "signal": "slow_performance",
                "fact": f"home page took {fetch_seconds:.1f}s to respond (>{SLOW_FETCH_SECONDS:.0f}s)",
                "angle": "Visitors abandon slow sites; trimming load time directly recovers lost sales.",
            })
    except Exception:
        pass


def _check_analytics(page: str, flags: list):
    """Flag #2: no analytics/pixel installed."""
    try:
        pl = page.lower()
        markers = ("gtag(", "googletagmanager.com/gtm.js", "google-analytics.com",
                   "ga('create", "analytics.js", "fbq(", "connect.facebook.net",
                   "plausible.io", "data-domain=", "matomo", "piwik",
                   "clarity.ms", "segment.com/analytics", "mixpanel",
                   "hotjar", "posthog")
        if not any(m in pl for m in markers):
            flags.append({
                "signal": "no_analytics",
                "fact": "no analytics or marketing pixel detected (no GA/GTM/Meta pixel/Plausible)",
                "angle": "They're flying blind — can't see traffic or what converts; analytics setup is fast and high-value.",
            })
    except Exception:
        pass


def _check_structured_data(page: str, flags: list):
    """Flag #3: no JSON-LD structured data (hurts rich SEO results)."""
    try:
        if "application/ld+json" not in page.lower():
            flags.append({
                "signal": "no_structured_data",
                "fact": "no schema.org structured data (no JSON-LD) — invisible to Google rich results",
                "angle": "Adding structured data unlocks rich snippets (ratings, business info) and better local SEO.",
            })
    except Exception:
        pass


def _check_sitemap(host: str, flags: list):
    """Flag #4: no /sitemap.xml."""
    try:
        status, body, err = _fetch(f"https://{host}/sitemap.xml")
        # Only flag when we got a definitive 'not there'. A timeout/error => skip (no false positive).
        if err is not None:
            return
        ok = status == 200 and ("<urlset" in (body or "").lower() or "<sitemapindex" in (body or "").lower())
        if status in (404, 410) or (status == 200 and not ok and not (body or "").strip()):
            flags.append({
                "signal": "no_sitemap",
                "fact": "no sitemap.xml found (404)",
                "angle": "Without a sitemap, Google may miss pages; adding one improves crawl coverage and indexing.",
            })
    except Exception:
        pass


def _check_open_graph(page: str, flags: list):
    """Flag #5: no Open Graph / social meta (broken link previews)."""
    try:
        pl = page.lower()
        has_title = 'property="og:title"' in pl or "property='og:title'" in pl
        has_image = 'property="og:image"' in pl or "property='og:image'" in pl
        twitter = 'name="twitter:card"' in pl or "name='twitter:card'" in pl
        if not (has_title or has_image or twitter):
            flags.append({
                "signal": "no_open_graph",
                "fact": "no Open Graph / social meta tags (og:title, og:image)",
                "angle": "Links to the site look blank/ugly when shared on Facebook, LinkedIn, iMessage — easy fix, better sharing.",
            })
    except Exception:
        pass


def _check_cms(page: str, host: str, flags: list):
    """Flag #6: outdated / identifiable CMS or stale libraries."""
    try:
        pl = page.lower()
        this_year = datetime.now(timezone.utc).year

        gen = re.search(r'<meta[^>]+name=["\']generator["\'][^>]+content=["\']([^"\']+)', page, re.I)
        if gen:
            content = gen.group(1).strip()
            cl = content.lower()
            ver = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", content)
            if "wordpress" in cl and ver:
                major = int(ver.group(1))
                if major < 6:  # WP 6.x is current era; <6 is meaningfully behind
                    flags.append({
                        "signal": "outdated_cms",
                        "fact": f'running {content} (generator meta) — outdated WordPress major version',
                        "angle": "Old WordPress = security holes and broken plugins; a managed update/migration is an easy upsell.",
                    })
            elif ver and any(k in cl for k in ("drupal", "joomla")):
                flags.append({
                    "signal": "outdated_cms",
                    "fact": f'CMS fingerprint exposed: {content}',
                    "angle": "Exposed, dated CMS version is a hacker target; hardening + updates are a clear paid fix.",
                })

        # WordPress readme exposes version on stock installs.
        if "outdated_cms" not in [f["signal"] for f in flags]:
            try:
                st, rbody, rerr = _fetch(f"https://{host}/readme.html")
                if rerr is None and st == 200 and rbody and "wordpress" in rbody.lower():
                    m = re.search(r"version\s+(\d+)\.(\d+)(?:\.(\d+))?", rbody, re.I)
                    if m and int(m.group(1)) < 6:
                        flags.append({
                            "signal": "outdated_cms",
                            "fact": f"WordPress {m.group(1)}.{m.group(2)} exposed via /readme.html — outdated and publicly leaking its version",
                            "angle": "Public version disclosure on an old WordPress is a bot magnet; update + lock down readme.",
                        })
            except Exception:
                pass

        # Stale jQuery (1.x is years EOL and a common vuln).
        if not any(f["signal"] == "stale_library" for f in flags):
            jq = re.search(r"jquery[/-](\d+)\.(\d+)(?:\.\d+)?(?:\.min)?\.js", pl)
            if jq and int(jq.group(1)) < 2:
                flags.append({
                    "signal": "stale_library",
                    "fact": f"loads jQuery {jq.group(1)}.{jq.group(2)} (1.x is end-of-life, known vulnerabilities)",
                    "angle": "EOL JavaScript libraries are an audit/security flag; modernizing the front-end is a tidy project.",
                })

        # ?ver= asset query strings pinned to an old year (theme/plugin staleness).
        if not any(f["signal"] in ("outdated_cms", "stale_library") for f in flags):
            vers = re.findall(r"\?ver=((?:19|20)\d{2})", pl)
            old_years = [int(y) for y in vers if int(y) <= this_year - 3]
            if old_years:
                yr = min(old_years)
                flags.append({
                    "signal": "stale_library",
                    "fact": f"assets versioned ?ver={yr} — theme/plugins not updated in ~{this_year - yr}y",
                    "angle": "Long-unmaintained themes/plugins break and expose the site; a maintenance plan keeps it safe.",
                })
    except Exception:
        pass


def _check_favicon(page: str, host: str, flags: list):
    """Flag #7: no favicon (looks unfinished in browser tabs)."""
    try:
        pl = page.lower()
        if 'rel="icon"' in pl or "rel='icon'" in pl or "shortcut icon" in pl or "apple-touch-icon" in pl:
            return
        # No declared icon in HTML — confirm /favicon.ico is also absent before flagging.
        st, body, err = _fetch(f"https://{host}/favicon.ico")
        if err is not None:
            return  # couldn't verify; stay conservative
        if st in (404, 410):
            flags.append({
                "signal": "no_favicon",
                "fact": "no favicon (no <link rel=icon> and /favicon.ico returns 404)",
                "angle": "Missing favicon makes the browser tab look generic/unfinished — a 5-minute polish that signals legitimacy.",
            })
    except Exception:
        pass


def probe(domain: str) -> dict:
    """Deterministic site-quality verdict for one business domain.

    Returns {domain, flags:[{signal,fact,angle}], severity, broken}. `broken` is True
    when the site doesn't even serve usable HTML (defers the hard-broken verdict to
    web_failure_probe); in that case quality checks are skipped to avoid noise."""
    host = _norm(domain)
    flags: list[dict] = []
    result = {"domain": host, "flags": flags, "severity": "ok", "broken": False}

    t0 = time.monotonic()
    status, html, err = _fetch(f"https://{host}")
    fetch_seconds = time.monotonic() - t0
    if status is None:
        # No HTTPS — try HTTP just to grab HTML for the content checks.
        status, html, err = _fetch(f"http://{host}")

    page = html or ""
    if not page or (status is not None and status >= 500) or status is None:
        result["broken"] = True
        result["note"] = "no usable HTML to analyze (run web_failure_probe for the broken-site verdict)"
        return result

    _check_performance(host, fetch_seconds, flags)
    _check_analytics(page, flags)
    _check_structured_data(page, flags)
    _check_sitemap(host, flags)
    _check_open_graph(page, flags)
    _check_cms(page, host, flags)
    _check_favicon(page, host, flags)

    # Severity = how strong the sales case is. Security-ish gaps rank higher.
    for f in flags:
        if f["signal"] in ("outdated_cms", "stale_library"):
            result["severity"] = _bump(result["severity"], "high")
        elif f["signal"] in ("slow_performance", "no_analytics"):
            result["severity"] = _bump(result["severity"], "medium")
        else:
            result["severity"] = _bump(result["severity"], "low")
    if len(flags) >= 4:
        result["severity"] = _bump(result["severity"], "high")
    return result


def probe_batch(domains, gaps_only=False) -> list[dict]:
    out = []
    for d in domains:
        d = (d or "").strip()
        if not d or d.startswith("#"):
            continue
        v = probe(d)
        if gaps_only and not v["flags"]:
            continue
        out.append(v)
    return out


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    as_json = "--json" in argv
    gaps_only = "--gaps-only" in argv
    domains = [a for a in argv if not a.startswith("--")]
    if "--file" in argv:
        i = argv.index("--file")
        if i + 1 < len(argv):
            with open(argv[i + 1], encoding="utf-8") as f:
                domains = [l for l in f.read().splitlines() if not l.startswith("--")]
    if not domains:
        print("usage: python3 -m core.site_quality_probe <domain...> | --file <list> [--gaps-only] [--json]")
        return 1
    results = probe_batch(domains, gaps_only=gaps_only)
    if as_json:
        print(json.dumps(results, indent=2))
    else:
        for r in results:
            if r["broken"]:
                print(f"[{'broken':>8}] {r['domain']:<32} {r.get('note', 'no HTML')}")
                continue
            sigs = ", ".join(f["signal"] for f in r["flags"]) or "clean"
            print(f"[{r['severity']:>8}] {r['domain']:<32} {len(r['flags'])} gap(s): {sigs}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
