#!/usr/bin/env python3
"""seo_local_rescue_gen — broken-site rescue pages that funnel into the FREE site checker.

WHITE-HAT MODEL (default = HUBS ONLY)
-------------------------------------
By default this generator writes exactly FOUR standalone, non-templated symptom hub
pages — one per real problem a small-business owner searches for:

    website-down · website-not-secure · website-slow · ssl-certificate-expired

Each hub HOSTS the free server-side broken-site checker on-page (a real, working tool —
not thin text) and explains that one specific problem in genuinely useful, per-symptom
copy. There is NO city or trade in these URLs. These 4 hubs (plus the index) are the
ONLY rescue pages this script ever adds to the sitemap.

WHY NOT 1280 CITY×TRADE×SYMPTOM PAGES
-------------------------------------
The full CITY (40) × TRADE (8) × SYMPTOM (4) = 1280-page permutation matrix is a
token-swap farm: 1280 near-duplicate pages that differ only by swapping the city/trade
nouns. Google classifies exactly this as **scaled content abuse** and **doorway pages**
in its spam policies. Shipping them indexable would be the single biggest penalty risk
on the whole property. So the permutation matrix is:

  * Gated behind an explicit, OFF-by-default `--full` opt-in (never auto-run), and
  * Every permutation page it writes carries `<meta name="robots" content="noindex,follow">`
    so the long tail stays crawlable for direct visitors / internal links but can NEVER
    compete as an indexable doorway, and
  * NONE of those noindexed URLs are ever added to sitemap.xml.

robots.txt host policy is owned by `agents/seo_sitemap.py` — this script does NOT write
robots.txt (one robots writer, not two emitting conflicting hosts).

    python3 tools/seo_local_rescue_gen.py            # DEFAULT: 4 hub pages + index + sitemap
    python3 tools/seo_local_rescue_gen.py --no-gate  # skip the content_quality gate
    python3 tools/seo_local_rescue_gen.py --full      # ALSO emit the 1280 perms as NOINDEX tail
    python3 tools/seo_local_rescue_gen.py --full --limit 16  # small noindexed perm batch

Output: data/hustle/seo_pages/generated/<symptom>.html        (4 indexable hubs)
        data/hustle/seo_pages/generated/index.html             (indexable hub directory)
        data/hustle/seo_pages/generated/sitemap.xml            (indexable pages only)
        data/hustle/seo_pages/generated/<city>-<state>-<trade>-<symptom>.html  (--full, NOINDEX)
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS_DIR))
import prog_seo_factory as pf  # noqa: E402  (JSON-LD builders, slug, OUTDIR, CANONICAL_BASE, STRIPE_INTENT)
import seo_freetools_gen as ft  # noqa: E402  (esc, SHARED_CSS, OUTDIR=seo_pages)

sys.path.insert(0, str(pf.BASE))  # repo root → make `core.content_quality` importable as a script

OUTDIR = pf.OUTDIR                       # data/hustle/seo_pages/generated
SEO_ROOT = ft.OUTDIR                     # data/hustle/seo_pages
CANON = pf.CANONICAL_BASE               # https://www.hailports.com
PREFIX = pf.PATH_PREFIX                 # /guides
SITE = "https://www.hailports.com"
CHECKER_URL = f"{CANON}/broken-site-checker.html"   # /intel/broken-site-checker.html
# Reuse the EXACT prog_seo STRIPE_INTENT string for the muted secondary cross-link so
# prog_seo_factory.check()'s CTA-presence test stays green across the whole generated/ dir.
STRIPE_INTENT = pf.STRIPE_INTENT
esc = ft.esc
slugify = pf.slug

# Marker injected into every noindexed permutation page; write_sitemap() uses it to
# guarantee a noindexed URL can never leak into sitemap.xml.
NOINDEX_META = '<meta name="robots" content="noindex,follow">'

STYLE = ft.SHARED_CSS + (
    ".rel{font-size:14px}.sub{color:#777}.crumb{font-size:13px;color:#777;margin:0 0 10px}"
    ".checker{background:#f4f7f5;border-radius:12px;padding:18px 18px 8px;margin:18px 0}"
    ".proof{background:#f4f7f5;border-radius:12px;padding:14px 18px;margin:18px 0}"
)

SYMPTOMS = ["website-down", "website-not-secure", "website-slow", "ssl-certificate-expired"]
SYMPTOM_LABEL = {
    "website-down": "Website down",
    "website-not-secure": "Website not secure",
    "website-slow": "Website slow",
    "ssl-certificate-expired": "SSL certificate expired",
}

# Trims a domain the visitor pastes (strips scheme/path) before it hits /site-scan.
# Kept as a plain (non-f) string so the JS braces and escapes pass through verbatim.
CHECKER_JS = """<script>
(function(){
  var f=document.querySelector('.checker form');
  if(!f){return;}
  f.addEventListener('submit',function(){
    var d=document.getElementById('domain');
    if(d&&d.value){d.value=d.value.trim().replace(/^https?:\\/\\//i,'').replace(/\\/.*$/,'');}
  });
})();
</script>"""


def checker_widget(content_slug: str) -> str:
    """The free server-side broken-site checker, embedded inline (real on-page tool).

    Same GET form the standalone /intel/broken-site-checker.html uses, so every page that
    hosts it is a genuinely useful interactive tool — not a thin doorway."""
    return (
        '<div class="checker">'
        '<form action="https://www.hailports.com/" method="get">'
        '<label for="domain">Website to check</label>'
        '<input id="domain" name="domain" placeholder="yourdomain.com" autocomplete="off" required>'
        '<input type="hidden" name="utm_source" value="seo">'
        '<input type="hidden" name="utm_medium" value="prog_seo">'
        '<input type="hidden" name="utm_campaign" value="site_scan">'
        f'<input type="hidden" name="utm_content" value="{esc(content_slug)}">'
        '<button type="submit">Run the free check &rarr;</button>'
        '</form>'
        '<p class="sub">Runs a real test from our server and shows the result instantly — enter a '
        'domain you own or manage. No login, no card. We only store an email if you ask for the full '
        'fix plan. (A page in your browser can’t legitimately read another domain’s SSL, '
        'HTTP status or response time, so the scan runs server-side.)</p>'
        '</div>' + CHECKER_JS
    )


def _checklist(items: list[str]) -> str:
    return "<ul>" + "".join(f"<li>{i}</li>" for i in items) + "</ul>"


# ── HUB copy (standalone, non-templated, one genuinely-distinct explainer per symptom) ──
def build_hub_copy(symptom: str) -> dict:
    if symptom == "website-down":
        return {
            "title": "Website Down? Free 60-Second Check to Find Out Why | Hailports",
            "h1": "Is your website down? Find out in 60 seconds",
            "meta": ("Website not loading? Run a free 60-second check for downtime, server errors, DNS "
                     "and expired hosting — then see exactly how to get it back up.")[:155],
            "intro": ("When a small-business website stops loading, every hour it’s dark is calls, "
                      "bookings and orders going to whoever ranks next. Before you panic or wait on a web "
                      "person, the free checker below tells you what’s actually wrong in about a "
                      "minute — so you walk into the fix knowing whether it’s hosting, DNS, the "
                      "domain, or the server."),
            "sections": [
                "<h2>What “down” usually means</h2>"
                "<p>A site goes dark for a short list of unglamorous reasons: the hosting plan lapsed or hit "
                "a resource limit, the domain renewal was missed, a DNS record points nowhere after a host "
                "change, or the server is throwing a 500-class error after a plugin or theme update. To a "
                "visitor they all look identical — a blank page or a browser error — but each one "
                "has a different fix, so guessing wastes the time you don’t have.</p>",
                "<h2>Check it from outside before you call anyone</h2>"
                "<p>Run the checker above. It loads your domain from outside your own network — your "
                "office Wi-Fi can serve a cached copy and hide an outage that every new visitor is hitting "
                "— and reports the exact HTTP status or timeout, whether DNS still resolves, and whether "
                "HTTPS and the certificate are valid once it’s back. That single result usually points "
                "straight at the cause.</p>"
                + _checklist([
                    "Is the site reachable at all, or timing out / refusing the connection?",
                    "Is it returning a 500-class server error after a recent change?",
                    "Does the domain still resolve, or has DNS gone stale after a host move?",
                    "Did the hosting plan or domain registration quietly expire?",
                ]),
                "<h2>How fast it comes back</h2>"
                "<p>Hosting and DNS problems are usually same-day once you know which one you have: restore "
                "the plan, fix the record, or roll back the change that broke it. An expired domain can take "
                "longer if it has lapsed past the grace period, which is exactly why an outside check that "
                "names the cause early is worth the sixty seconds.</p>",
            ],
            "faq": [
                ("Why is my website suddenly down?",
                 "Most often expired hosting or a missed domain renewal, a broken DNS record after a host "
                 "change, or a server error following an update. The free check reports the exact status "
                 "your site returns from outside your network so you know which one it is."),
                ("How can I tell if a site is really down or just down for me?",
                 "Your own browser or network can cache an old copy, so the site can look fine to you while "
                 "every new visitor hits a dead page. A check run from outside your network is the only "
                 "honest test of what customers actually see."),
                ("How long does it take to get a downed site back up?",
                 "Hosting and DNS issues are usually resolved the same day once the cause is identified. The "
                 "check narrows it down in under a minute so no time is lost guessing."),
            ],
        }
    if symptom == "website-not-secure":
        return {
            "title": "Website Says ‘Not Secure’? Free HTTPS & SSL Check | Hailports",
            "h1": "Why does your website say ‘Not Secure’?",
            "meta": ("Browser showing a ‘Not secure’ warning on your site? Free check for missing "
                     "HTTPS and SSL so visitors stop seeing the red flag before they read your prices.")[:155],
            "intro": ("When a browser stamps <strong>‘Not secure’</strong> next to your address, "
                      "visitors hesitate before they read a single price — and many bounce straight to "
                      "a competitor. The warning almost never means you were hacked; it means the page is "
                      "served without working HTTPS. The free checker above confirms exactly what’s "
                      "missing so the fix is usually quick."),
            "sections": [
                "<h2>What triggers the ‘Not secure’ label</h2>"
                "<p>Browsers now flag any page served over plain HTTP — no padlock — as ‘Not "
                "secure’. That puts the warning on your contact form, your quote request and your phone "
                "number. It happens when a site never had an SSL certificate installed, or has one but isn’t "
                "redirecting HTTP traffic to the secure HTTPS version, so some pages still load insecurely.</p>",
                "<h2>Confirm it in one check</h2>"
                "<p>The checker above loads your site the way a customer’s browser does and reports "
                "whether HTTPS is present, whether plain HTTP silently upgrades to it, and whether the "
                "certificate itself is valid and matches your domain. That tells you whether you need a "
                "certificate installed, or just the HTTPS redirect switched on.</p>"
                + _checklist([
                    "Does the site load over HTTPS with a valid padlock?",
                    "Does plain HTTP redirect to HTTPS, or stay insecure?",
                    "Is there any SSL certificate installed at all?",
                    "Does the certificate match the exact domain and www. variant customers type?",
                ]),
                "<h2>Why it’s worth fixing today</h2>"
                "<p>A security warning on a checkout or quote form measurably lowers how many visitors "
                "finish, and search engines favor HTTPS, so an insecure site loses both trust and "
                "visibility at once. Most hosts now include a free certificate, so the work is usually "
                "installing it and forcing the redirect — not a new expense.</p>",
            ],
            "faq": [
                ("Why does my website say ‘Not secure’?",
                 "Because it’s being served over HTTP without a working SSL certificate, or HTTP isn’t "
                 "redirecting to the secure version. Browsers flag every such page automatically — it "
                 "usually is not a sign of being hacked."),
                ("Does the ‘Not secure’ warning actually cost customers?",
                 "Yes. A visitor who sees a security warning on a form is far more likely to leave, and the "
                 "warning also suppresses you in search results that favor secure sites."),
                ("Is fixing ‘Not secure’ expensive?",
                 "Usually not. Most hosts include a free certificate; the job is installing it and forcing "
                 "the HTTPS redirect. The free check confirms which of those steps you actually need."),
            ],
        }
    if symptom == "website-slow":
        return {
            "title": "Website Slow? Free Speed Check & the Common Fixes | Hailports",
            "h1": "Is your website too slow?",
            "meta": ("Website loading slowly? Free check of real load and first-byte time, plus the common "
                     "fixes that win back impatient visitors and search rankings.")[:155],
            "intro": ("A slow website bleeds visitors before the page even appears — most people leave "
                      "after a few seconds of staring at a blank screen, and on a phone they’re even "
                      "less patient. The good news: speed is measurable, and the usual culprits are a short, "
                      "well-known list. The free checker above shows your real load time from outside, so "
                      "you can target the right one."),
            "sections": [
                "<h2>Why a small-business site loads slowly</h2>"
                "<p>The common causes are heavy unoptimized photos (before/after galleries are notorious), a "
                "cheap or overloaded shared host, render-blocking scripts and plugins, and no caching so the "
                "server rebuilds every page from scratch. Any one of them can push a site past the point "
                "where a phone visitor gives up and calls a faster-loading competitor instead.</p>",
                "<h2>Measure it from the outside</h2>"
                "<p>The checker above records how long your site takes to send its first byte and finish "
                "loading from outside your network, so you see the same speed a real customer gets — not "
                "the fast cached version sitting on your own device. From a real number, the fixes become "
                "predictable instead of guesswork.</p>"
                + _checklist([
                    "How long until the server sends the first byte (server / hosting speed)?",
                    "Are images compressed, or shipping at full camera resolution?",
                    "Is browser and server caching turned on?",
                    "Is the hosting plan keeping up, or overloaded at peak times?",
                ]),
                "<h2>The fixes that move the number</h2>"
                "<p>Compressing and right-sizing images, turning on caching, and moving off an overloaded "
                "shared plan are the three changes that recover the most time for the least effort on a "
                "typical small-business site. Re-run the check after each one to confirm the load time "
                "actually dropped rather than assuming it did.</p>",
            ],
            "faq": [
                ("How slow is too slow for a website?",
                 "Past about three seconds to load, most visitors leave. Mobile users — often someone "
                 "standing on a job site or in a parking lot — are even less patient, so faster is "
                 "always better."),
                ("What slows a small-business site down the most?",
                 "Almost always oversized images and cheap shared hosting, followed by missing caching. The "
                 "check shows your real load time so you can target the biggest culprit first."),
                ("Does website speed affect my Google ranking?",
                 "Yes. Page speed is a ranking signal, so a slow site loses impatient visitors and search "
                 "visibility at the same time."),
            ],
        }
    # ssl-certificate-expired
    return {
        "title": "SSL Certificate Expired? Free Check & Fix Guide | Hailports",
        "h1": "Has your SSL certificate expired?",
        "meta": ("Site showing an SSL or certificate error? Free check of your certificate’s validity "
                 "and expiry date, plus how to clear the full-page browser warning.")[:155],
        "intro": ("An expired SSL certificate slams a full-page <strong>red warning</strong> in front of "
                  "everyone trying to reach your site — “Your connection is not private” — "
                  "and most people will never click past it. Nothing about your business changed; a date "
                  "lapsed. It’s one of the fastest problems to confirm and fix once you know the expiry, "
                  "and the free checker above reads it for you."),
        "sections": [
            "<h2>What an expired certificate does</h2>"
            "<p>When the certificate lapses, browsers stop trusting the connection and show a scary "
            "interstitial instead of your homepage. A customer who was ready to book sees a security alarm "
            "and leaves — the warning is severe enough that even careful visitors back out rather than "
            "click ‘proceed anyway’.</p>",
            "<h2>Check the expiry date now</h2>"
            "<p>The checker above reads your certificate the way a browser does and reports whether it’s "
            "valid, when it expires, and whether it matches your exact domain. If it’s already lapsed or "
            "mismatched, renewing or reissuing it clears the warning — often within the hour.</p>"
            + _checklist([
                "Is the certificate currently valid, or already expired?",
                "How many days until it expires (so you renew before it lapses again)?",
                "Does it cover the exact domain and the www. variant customers type?",
                "Is auto-renewal configured so this doesn’t recur?",
            ]),
            "<h2>Stop it recurring</h2>"
            "<p>Most expired-certificate emergencies are a renewal that silently failed. After you renew, "
            "confirm auto-renewal is enabled with your host or certificate provider, and keep a calendar "
            "reminder a couple of weeks before the next expiry as a backstop. The free check reports the "
            "exact expiry date so you know when that is.</p>",
        ],
        "faq": [
            ("How do I know my SSL certificate expired?",
             "Browsers show a full-page ‘your connection is not private’ or ‘certificate "
             "expired’ warning. The free check confirms it and reports the exact expiry date."),
            ("How do I fix an expired SSL certificate?",
             "Renew or reissue it through your host or certificate provider, then make sure auto-renewal is "
             "on so it can’t lapse again. It’s typically a same-day fix."),
            ("Will an expired certificate hurt my search ranking?",
             "Indirectly, yes — visitors bounce off the warning and search engines distrust an insecure "
             "site, so both traffic and rankings suffer until it’s renewed."),
        ],
    }


# ── per-symptom CITY×TRADE copy (PERMUTATION pages — NOINDEX-only, --full gated) ──
def build_copy(symptom: str, city: str, state: str, t: dict) -> dict:
    one, biz, svc = t["one"], t["biz"], t["svc"]
    loc = f"{city}, {state}"
    if symptom == "website-down":
        return {
            "title": f"{city} {one.title()} Website Down? Free 60-Second Check & Fix | Hailports",
            "h1": f"Is your {city} {biz} website down?",
            "meta": (f"{city}, {state} {one} website not loading? Run a free 60-second check for "
                     f"downtime, server errors and DNS issues, then see exactly how to get it back up.")[:155],
            "intro": (f"If your <strong>{esc(biz)} website in {esc(loc)}</strong> isn't loading, every "
                      f"hour it's down is calls and bookings going to the next {esc(one)} in the search "
                      f"results. Before you panic or wait on a web guy, confirm what's actually wrong in "
                      f"about a minute."),
            "sections": [
                f"<h2>What 'down' usually means for a {esc(svc)} site</h2>"
                f"<p>A {esc(biz)} site goes dark for a handful of boring reasons: the hosting plan "
                f"lapsed or hit a limit, the domain renewal was missed, a DNS record points nowhere, or "
                f"the server is returning a 500-class error after an update. Each one looks identical to a "
                f"customer in {esc(city)} — a blank page or a browser error — but the fix is different.</p>",
                f"<h2>Check it before you call anyone</h2>"
                f"<p>Run the free check below. It pings your domain from outside your own network "
                f"(your office Wi-Fi can cache an old copy and hide the outage), reports the exact HTTP "
                f"status or timeout, and flags whether HTTPS and your certificate are still valid — so "
                f"you walk into the conversation knowing if it's hosting, DNS, or the certificate.</p>"
                + _checklist([
                    "Is the site reachable at all, or timing out / refusing the connection?",
                    "Is it returning a 500-class server error after a recent change?",
                    "Does the domain still resolve, or has DNS gone stale?",
                    "Is HTTPS and the SSL certificate still valid once it's back?",
                ]),
            ],
            "faq": [
                (f"Why is my {city} {one} website down?",
                 f"Most often expired hosting or domain, a broken DNS record, or a server error after an "
                 f"update. The free check tells you which by reporting the exact status your site returns "
                 f"from outside your network."),
                ("How fast can it be fixed?",
                 "Hosting and DNS issues are usually same-day once you know which one it is. The check "
                 "narrows it down in under a minute so no time is wasted guessing."),
                (f"Will customers in {esc(loc)} see the outage?",
                 "Yes — your own browser may show a cached copy, so the site can look fine to you while "
                 "every new visitor hits a dead page. An outside check is the only honest test."),
            ],
        }
    if symptom == "website-not-secure":
        return {
            "title": f"{city} {one.title()} Website 'Not Secure'? Free HTTPS Check | Hailports",
            "h1": f"Why does your {city} {biz} website say 'Not Secure'?",
            "meta": (f"{city}, {state} {one} site showing a 'Not secure' warning? Free check for missing "
                     f"HTTPS and SSL so customers stop seeing the red flag in their browser.")[:155],
            "intro": (f"When a browser stamps <strong>'Not secure'</strong> on your <strong>{esc(biz)} "
                      f"website in {esc(loc)}</strong>, visitors hesitate before they ever read your "
                      f"prices — and many bounce straight back to a competing {esc(one)}. The fix is "
                      f"usually quick once you know what's missing."),
            "sections": [
                f"<h2>What triggers the 'Not secure' label</h2>"
                f"<p>Browsers now flag any page served over plain HTTP — no padlock — as 'Not secure'. "
                f"For a {esc(svc)} business that means your contact form, quote request, and phone number "
                f"all sit behind a warning. It happens when a site never had an SSL certificate installed, "
                f"or has one that isn't being used to redirect HTTP traffic to HTTPS.</p>",
                f"<h2>Confirm it in one check</h2>"
                f"<p>The free check below loads your {esc(city)} site the way a customer's browser does and "
                f"reports whether HTTPS is present, whether HTTP silently upgrades to it, and whether the "
                f"certificate itself is valid. That tells you if you need a certificate installed or just a "
                f"redirect turned on.</p>"
                + _checklist([
                    "Does the site load over HTTPS with a valid padlock?",
                    "Does plain HTTP redirect to HTTPS, or stay insecure?",
                    "Is there any SSL certificate installed at all?",
                    "Does the certificate match the exact domain customers type?",
                ]),
            ],
            "faq": [
                (f"Why does my {city} {one} site say 'Not secure'?",
                 "Because it's being served over HTTP without a working SSL certificate, or HTTP isn't "
                 "redirecting to the secure version. Browsers flag every such page automatically."),
                ("Does 'Not secure' really cost me customers?",
                 f"Yes — a {esc(svc)} customer who sees a security warning on your quote form is far more "
                 "likely to leave. It also suppresses you in search results that favor HTTPS."),
                ("Is fixing it expensive?",
                 "Usually not. Most hosts include a free certificate; the work is installing it and forcing "
                 "the HTTPS redirect. The free check confirms which step you actually need."),
            ],
        }
    if symptom == "website-slow":
        return {
            "title": f"{city} {one.title()} Website Slow? Free Speed Check & Fixes | Hailports",
            "h1": f"Is your {city} {biz} website too slow?",
            "meta": (f"{city}, {state} {one} website loading slowly? Free check of load time and first-byte "
                     f"speed, plus the common fixes that win back visitors and rankings.")[:155],
            "intro": (f"A slow <strong>{esc(biz)} website in {esc(loc)}</strong> bleeds customers before "
                      f"the page even appears — most visitors leave after a few seconds of staring at a "
                      f"blank screen. The good news: site speed is measurable, and the usual culprits for a "
                      f"{esc(svc)} site are well known."),
            "sections": [
                f"<h2>Why a {esc(svc)} site loads slowly</h2>"
                f"<p>The common causes are heavy unoptimized photos (before/after galleries are notorious), "
                f"a cheap overloaded shared host, render-blocking scripts, and no caching. Any one of them "
                f"can push your {esc(city)} site past the point where a phone visitor gives up and calls a "
                f"faster-loading {esc(one)} instead.</p>",
                f"<h2>Measure it from the outside</h2>"
                f"<p>The free check below records how long your site takes to respond and finish loading "
                f"from outside your network, so you see the same speed a real customer in {esc(loc)} gets — "
                f"not the fast cached version on your own device. From there the fixes are predictable.</p>"
                + _checklist([
                    "How long until the server sends the first byte?",
                    "Are images compressed, or shipping at full camera resolution?",
                    "Is browser and server caching turned on?",
                    "Is the hosting plan keeping up, or overloaded at peak times?",
                ]),
            ],
            "faq": [
                (f"How slow is too slow for a {one} website?",
                 "Past about three seconds to load, most visitors leave. Mobile users on a job site are "
                 "even less patient, so faster is always better."),
                ("What slows a small-business site down most?",
                 f"For {esc(svc)} sites it's almost always oversized images and cheap shared hosting, "
                 "followed by missing caching. The check shows your real load time so you can target it."),
                ("Does speed affect my Google ranking?",
                 "Yes. Page speed is a ranking signal, so a slow site loses both impatient visitors and "
                 "search visibility at the same time."),
            ],
        }
    # ssl-certificate-expired
    return {
        "title": f"{city} {one.title()} SSL Certificate Expired? Free Check & Fix | Hailports",
        "h1": f"Has your {city} {biz} SSL certificate expired?",
        "meta": (f"{city}, {state} {one} website showing an SSL or certificate error? Free check of your "
                 f"certificate's validity and expiry, plus how to clear the full-page browser warning.")[:155],
        "intro": (f"An expired SSL certificate slams a full-page <strong>red warning</strong> in front of "
                  f"everyone trying to reach your <strong>{esc(biz)} website in {esc(loc)}</strong> — most "
                  f"will never click past it. It's one of the fastest problems to confirm and fix once you "
                  f"know the expiry date."),
        "sections": [
            f"<h2>What an expired certificate does to a {esc(svc)} site</h2>"
            f"<p>When the certificate lapses, browsers stop trusting the connection and show a scary "
            f"'Your connection is not private' interstitial instead of your homepage. For a {esc(city)} "
            f"{esc(one)}, that means a customer ready to book sees a security alarm and bounces — even "
            f"though nothing about your service changed.</p>",
            f"<h2>Check the expiry date now</h2>"
            f"<p>The free check below reads your certificate the way a browser does and reports whether it's "
            f"valid, when it expires, and whether it matches your exact domain. If it's lapsed or mismatched, "
            f"renewing or reissuing it clears the warning, usually within the hour.</p>"
            + _checklist([
                "Is the certificate currently valid, or already expired?",
                "How many days until it expires (so you renew before it lapses again)?",
                "Does it cover the exact domain and www. variant customers type?",
                "Is auto-renewal configured so this doesn't recur?",
            ]),
        ],
        "faq": [
            (f"How do I know my {city} {one} certificate expired?",
             "Browsers show a full-page 'not private' or 'certificate expired' warning. The free check "
             "confirms it and reports the exact expiry date."),
            ("How do I fix an expired SSL certificate?",
             "Renew or reissue it through your host or certificate provider, then make sure auto-renewal "
             "is on so it can't lapse again. It's typically a same-day fix."),
            ("Will an expired certificate hurt my search ranking?",
             "Indirectly, yes — visitors bounce off the warning and search engines distrust an insecure "
             "site, so both traffic and rankings suffer until it's renewed."),
        ],
    }


# ── permutation matrix (PERMUTATION pages only — see --full) ──────────────────
CITIES = [
    ("Austin", "TX"), ("Houston", "TX"), ("Dallas", "TX"), ("San Antonio", "TX"),
    ("Fort Worth", "TX"), ("Phoenix", "AZ"), ("Tucson", "AZ"), ("Los Angeles", "CA"),
    ("San Diego", "CA"), ("San Jose", "CA"), ("San Francisco", "CA"), ("Sacramento", "CA"),
    ("Fresno", "CA"), ("Denver", "CO"), ("Seattle", "WA"), ("Portland", "OR"),
    ("Las Vegas", "NV"), ("Chicago", "IL"), ("New York", "NY"), ("Philadelphia", "PA"),
    ("Pittsburgh", "PA"), ("Boston", "MA"), ("Atlanta", "GA"), ("Miami", "FL"),
    ("Orlando", "FL"), ("Tampa", "FL"), ("Jacksonville", "FL"), ("Charlotte", "NC"),
    ("Raleigh", "NC"), ("Nashville", "TN"), ("Memphis", "TN"), ("Columbus", "OH"),
    ("Cleveland", "OH"), ("Detroit", "MI"), ("Minneapolis", "MN"), ("Kansas City", "MO"),
    ("St Louis", "MO"), ("Indianapolis", "IN"), ("Milwaukee", "WI"), ("Salt Lake City", "UT"),
]

TRADES = [
    {"slug": "roofer", "one": "roofer", "biz": "roofing company", "svc": "roofing"},
    {"slug": "plumber", "one": "plumber", "biz": "plumbing company", "svc": "plumbing"},
    {"slug": "hvac", "one": "HVAC contractor", "biz": "HVAC company", "svc": "heating and cooling"},
    {"slug": "dentist", "one": "dentist", "biz": "dental practice", "svc": "dental"},
    {"slug": "electrician", "one": "electrician", "biz": "electrical company", "svc": "electrical"},
    {"slug": "landscaper", "one": "landscaper", "biz": "landscaping company", "svc": "landscaping"},
    {"slug": "auto-repair", "one": "auto repair shop", "biz": "auto repair shop", "svc": "auto repair"},
    {"slug": "chiropractor", "one": "chiropractor", "biz": "chiropractic clinic", "svc": "chiropractic"},
]


# ── page model + render ──────────────────────────────────────────────────────
class RPage:
    __slots__ = ("slug", "title", "h1", "meta", "intro", "sections", "faq",
                 "city", "state", "trade", "symptom", "svc", "is_hub")

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw.get(k))


def make_hub(symptom) -> RPage:
    c = build_hub_copy(symptom)
    return RPage(slug=symptom, title=c["title"], h1=c["h1"], meta=c["meta"], intro=c["intro"],
                 sections=c["sections"], faq=c["faq"], city=None, state=None,
                 trade=None, symptom=symptom, svc=None, is_hub=True)


def make_page(city, state, t, symptom) -> RPage:
    s = slugify(f"{city}-{state}-{t['slug']}-{symptom}")
    c = build_copy(symptom, city, state, t)
    return RPage(slug=s, title=c["title"], h1=c["h1"], meta=c["meta"], intro=c["intro"],
                 sections=c["sections"], faq=c["faq"], city=city, state=state,
                 trade=t["slug"], symptom=symptom, svc=t["svc"], is_hub=False)


def breadcrumb_jsonld(p: RPage, url: str) -> dict:
    crumbs = [("Home", f"{SITE}/"),
              ("Broken Website Checker", CHECKER_URL),
              (p.h1, url)]
    return {"@context": "https://schema.org", "@type": "BreadcrumbList",
            "itemListElement": [{"@type": "ListItem", "position": i + 1, "name": n, "item": u}
                                for i, (n, u) in enumerate(crumbs)]}


def render(p: RPage, related: list[tuple[str, str]], noindex: bool) -> str:
    url = f"{CANON}{PREFIX}/{p.slug}.html"
    # Indexable hubs get full structured data; noindexed permutation pages get NONE
    # (no point indexing it, and it keeps any "scaled structured data" concern off the table).
    if noindex:
        ld_html = ""
        og = ""
        robots = NOINDEX_META
    else:
        art = pf.article_jsonld(pf.Page(h1=p.h1, meta=p.meta, slug=p.slug, vertical=p.svc), url)
        ld = [art, pf.faq_jsonld(p.faq), breadcrumb_jsonld(p, url)]
        ld_html = "".join(
            f'<script type="application/ld+json">{json.dumps(x, ensure_ascii=False)}</script>'
            for x in ld)
        og = (f'<meta property="og:type" content="article">'
              f'<meta property="og:title" content="{esc(p.title)}">'
              f'<meta property="og:description" content="{esc(p.meta)}">'
              f'<meta property="og:url" content="{url}">'
              '<meta property="og:site_name" content="Hailports">'
              '<meta name="twitter:card" content="summary">'
              f'<meta name="twitter:title" content="{esc(p.title)}">'
              f'<meta name="twitter:description" content="{esc(p.meta)}">')
        robots = ""

    crumb_tail = (esc(SYMPTOM_LABEL.get(p.symptom, p.symptom))
                  if p.is_hub else f"{esc(p.city)} {esc(p.trade)}")
    widget = checker_widget(p.slug)
    faq_html = "<h2>FAQ</h2>" + "".join(
        f"<h3>{esc(qa[0])}</h3><p>{esc(qa[1])}</p>" for qa in p.faq)
    rel_html = ""
    if related:
        rel_html = ('<h2>Related checks</h2><ul class="rel">'
                    f'<li><a href="{CHECKER_URL}?utm_source=seo_internal">'
                    'Free broken-site checker (test any URL)</a></li>'
                    + "".join(
                        f'<li><a href="{CANON}{PREFIX}/{s}.html?utm_source=seo_internal">{esc(title)}</a></li>'
                        for title, s in related)
                    + "</ul>")
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>{esc(p.title)}</title>'
        f'<meta name="description" content="{esc(p.meta)}">'
        f'{robots}<link rel="canonical" href="{url}">{og}{ld_html}'
        f'<style>{STYLE}</style></head><body>'
        f'<p class="crumb"><a href="{SITE}/">Home</a> &rsaquo; '
        f'<a href="{CHECKER_URL}">Broken Website Checker</a> &rsaquo; {crumb_tail}</p>'
        f'<h1>{esc(p.h1)}</h1>'
        f'<p>{p.intro}</p>'
        '<h2>Check your site now — free</h2>' + widget
        + "".join(p.sections)
        + faq_html + rel_html
        + f'<p style="margin-top:28px"><a href="{SITE}/">Home</a> &middot; '
          f'<a href="{CHECKER_URL}">Check any site free</a></p>'
        # Muted secondary cross-sell (NOT the primary CTA). Doubles as the prog_seo
        # check()'s CTA-presence marker so the shared generated/ dir stays green.
        + f'<p class="sub" style="margin-top:14px">Site already healthy? '
          f'<a href="{STRIPE_INTENT}">See local customers publicly asking for work this week '
          f'→ Intent Lead Finder</a></p>'
        + '<p class="note" style="margin-top:24px">Checks run on our server against public endpoints; '
          'we never store credentials or private data.</p>'
        '</body></html>'
    )


# ── content_quality gate (in-process) ─────────────────────────────────────────
def editorial_text(p: RPage) -> str:
    blob = " ".join([p.intro] + list(p.sections) + [a for _, a in p.faq])
    blob = re.sub(r"<[^>]+>", " ", blob)
    return re.sub(r"\s+", " ", blob).strip()


def gate_ok(p: RPage) -> bool:
    try:
        from core.content_quality import gate
        return gate(editorial_text(p), channel="blog", persona="generic", recent=[]).passed
    except Exception:
        return True


# ── related-link picker for PERMUTATION pages (absolute URLs, filtered to written) ──
def related_for(p: RPage, by_ct: dict, written: set) -> list[tuple[str, str]]:
    out, seen = [], {p.slug}
    # 1. same city+trade, the other symptoms (always real siblings)
    for q in by_ct.get((p.city, p.state, p.trade), []):
        if q.slug not in seen and q.slug in written:
            out.append((q.h1, q.slug)); seen.add(q.slug)
        if len(out) >= 2:
            break
    # 2. same city, sibling trades (same symptom) for cross-trade breadth
    for q in by_ct.get((p.city, p.state, None), []):
        if q.symptom == p.symptom and q.trade != p.trade and q.slug not in seen and q.slug in written:
            out.append((q.h1, q.slug)); seen.add(q.slug)
        if len(out) >= 3:
            break
    return out[:3]


# ── sitemap — INDEXABLE pages only (never any noindexed permutation URL) ──────
def write_sitemap() -> int:
    """Glob generated/ but EXCLUDE any page carrying the noindex robots meta, so a
    noindexed permutation URL can never leak into the sitemap. Includes the 4 hubs,
    the index, and any pre-existing prog_seo pages that are themselves indexable."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    files = []
    for f in sorted(OUTDIR.glob("*.html")):
        try:
            if 'content="noindex' in f.read_text():
                continue
        except OSError:
            continue
        files.append(f)
    locs = [f"{CANON}{PREFIX}/{f.name}" for f in files]
    body = "".join(f"  <url><loc>{u}</loc><lastmod>{today}</lastmod></url>\n" for u in locs)
    sm = ('<?xml version="1.0" encoding="UTF-8"?>\n'
          '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n' + body + "</urlset>\n")
    (OUTDIR / "sitemap.xml").write_text(sm)
    return len(locs)


def write_index(hubs: list[RPage]):
    url = f"{CANON}{PREFIX}/index.html"
    items = "".join(
        f'<li><a href="{CANON}{PREFIX}/{p.slug}.html?utm_source=seo_internal">{esc(p.h1)}</a> '
        f'<span class="sub">— {esc(SYMPTOM_LABEL.get(p.symptom, p.symptom))}</span></li>'
        for p in hubs)
    ld = {"@context": "https://schema.org", "@type": "CollectionPage",
          "name": "Free website health checks — down, not secure, slow, expired SSL",
          "url": url, "publisher": {"@type": "Organization", "name": "Hailports"}}
    html = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>Free Website Health Checks — Down, Not Secure, Slow, Expired SSL | Hailports</title>'
        '<meta name="description" content="Free, no-login checks for a website that is down, not secure, '
        'slow, or serving an expired SSL certificate. Run an instant server-side scan and see exactly '
        'what is broken.">'
        f'<link rel="canonical" href="{url}">'
        f'<script type="application/ld+json">{json.dumps(ld, ensure_ascii=False)}</script>'
        f'<style>{STYLE}</style></head><body>'
        '<h1>Is a website broken? Check it free</h1>'
        '<p>Free, no-login checks for a site that is <strong>down</strong>, <strong>not secure</strong>, '
        '<strong>slow</strong>, or serving an <strong>expired SSL certificate</strong>. Run the instant '
        'server-side check below, or read the guide for the exact problem you’re seeing.</p>'
        '<h2>Check any website now — free</h2>' + checker_widget("index")
        + '<h2>Guides by problem</h2><ul class="rel">' + items + '</ul>'
        # secondary cross-link (also the prog_seo check CTA marker)
        f'<p class="sub" style="margin-top:18px">Running a healthy site already? '
        f'<a href="{STRIPE_INTENT}">Find local buyers publicly asking for work → Intent Lead Finder</a></p>'
        '</body></html>'
    )
    (OUTDIR / "index.html").write_text(html)


# ── driver ────────────────────────────────────────────────────────────────────
def generate(full=False, limit=None, no_gate=False) -> dict:
    OUTDIR.mkdir(parents=True, exist_ok=True)

    # 1) HUBS — the only indexable rescue pages. Always built, never gated out silently.
    hubs = [make_hub(s) for s in SYMPTOMS]
    hub_written: list[RPage] = []
    hub_failed = []
    for p in hubs:
        if not no_gate and not gate_ok(p):
            hub_failed.append(p.slug); continue
        hub_written.append(p)
    if len(hub_written) != 4:
        raise SystemExit(
            f"HUB GATE FAILURE: only {len(hub_written)}/4 symptom hubs passed content_quality "
            f"({hub_failed}). The 4 hubs are mandatory — fix the copy, do not ship a partial set.")
    hub_slugs = {p.slug for p in hub_written}
    for p in hub_written:
        rel = [(q.h1, q.slug) for q in hub_written if q.slug != p.slug]  # cross-link the other hubs
        (OUTDIR / f"{p.slug}.html").write_text(render(p, rel, noindex=False))

    # 2) PERMUTATIONS — OFF by default. Token-swap farm = doorway/scaled-content antipattern,
    #    so every page is NOINDEX and never enters the sitemap. Opt-in via --full only.
    perm_written = 0
    perm_skipped = 0
    if full:
        models: list[RPage] = []
        for city, state in CITIES:
            for t in TRADES:
                for sym in SYMPTOMS:
                    models.append(make_page(city, state, t, sym))
        if limit:
            models = models[:limit]
        by_ct: dict[tuple, list[RPage]] = {}
        for p in models:
            by_ct.setdefault((p.city, p.state, p.trade), []).append(p)
            by_ct.setdefault((p.city, p.state, None), []).append(p)
        written: list[RPage] = []
        seen_h1, seen_meta = set(), set()
        for p in models:
            if p.h1 in seen_h1 or p.meta in seen_meta:
                perm_skipped += 1; continue
            if not no_gate and not gate_ok(p):
                perm_skipped += 1; continue
            seen_h1.add(p.h1); seen_meta.add(p.meta)
            written.append(p)
        written_slugs = {p.slug for p in written}
        for p in written:
            # link UP to the matching indexable hub (passes equity via noindex,follow) + siblings
            rel = [(SYMPTOM_LABEL[p.symptom], p.symptom)] if p.symptom in hub_slugs else []
            rel += related_for(p, by_ct, written_slugs)[:2]
            (OUTDIR / f"{p.slug}.html").write_text(render(p, rel, noindex=True))
        perm_written = len(written)

    write_index(hub_written)
    # Guard: the 4 hubs (+ index) are the only rescue pages allowed in the sitemap.
    assert len(hub_written) == 4, "expected exactly 4 indexable hub pages before sitemap write"
    n_sitemap = write_sitemap()
    # NOTE: robots.txt is intentionally NOT written here — agents/seo_sitemap.py owns it.
    return {"mode": "full" if full else "hubs-only",
            "hubs_written": len(hub_written), "hub_slugs": sorted(hub_slugs),
            "perms_written_noindex": perm_written, "perms_skipped": perm_skipped,
            "sitemap_urls": n_sitemap, "robots_written": False}


def main(argv=None):
    ap = argparse.ArgumentParser(description="Broken-site rescue pages (4 hubs by default).")
    ap.add_argument("--full", action="store_true",
                    help="ALSO emit the 1280 city×trade×symptom permutations as NOINDEX pages "
                         "(off by default; never indexed, never in sitemap).")
    ap.add_argument("--limit", type=int, help="cap the --full permutation count (spot-checks).")
    ap.add_argument("--no-gate", action="store_true", help="skip the content_quality gate.")
    a = ap.parse_args(argv)
    r = generate(full=a.full, limit=a.limit, no_gate=a.no_gate)
    print(json.dumps(r, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
