#!/usr/bin/env python3
"""One source of truth for "is this funnel hit a real prospect, us, or a bot?"

Both tools/revenue_scoreboard.py and agents/funnel_interaction_notifier.py import
this so they can't drift (they used to disagree: the scoreboard knew 2 bot IP
prefixes, the notifier knew ~12, and the notifier also hard-required a UA field
the events don't even carry — so it silently classified everything as non-human).

Design: don't guess "human" by IP prefix. Instead
  1. exclude OWNER traffic (known IP / ownertest tag / our own referrers),
  2. exclude obvious BOTS (bot UA when present) and DATACENTER IPs (CIDR match
     against the major cloud/crawler ranges — Google, AWS, Azure, OVH, Tencent,
     DigitalOcean, Hetzner, Linode, Cloudflare, Apple),
  3. of what's left (residential / unknown IP, no bot UA), only call it HUMAN if it
     shows a positive human signal: a session id, an inbound utm_source, or a real
     external referrer. A bare hit from an unknown IP with none of those is "unknown"
     (treated as non-human) — that's how scanners with no UA slip a prefix list.

The headline metric is `is_attributed`: a human who arrived carrying one of OUR
utm_source tags. That's the only count that proves a channel we published actually
delivered a prospect (vs. crawlers indexing the page). Today that number is 0.
"""
from __future__ import annotations

import ipaddress
import os

# Bot/crawler/automation user-agents (only consulted when a UA is present).
_BOT_UA = (
    "bot", "crawler", "spider", "slurp", "curl", "wget", "python-requests", "httpx",
    "go-http-client", "java/", "okhttp", "headless", "phantomjs", "facebookexternalhit",
    "preview", "monitor", "uptime", "pingdom", "statuscake", "scan", "probe", "applebot",
    "ahrefs", "semrush", "mj12bot", "dotbot", "bingpreview", "petalbot", "dataforseo",
    "gptbot", "ccbot", "claudebot", "bytespider", "amazonbot", "yandex", "baiduspider",
    "viewer/", "dataforseo", "zgrab", "masscan", "censys", "expanse", "internet-measurement",
    # server-side HTTP clients (never a human browser) + social link-unfurlers
    "undici", "node-fetch", "axios", "got (", "okhttp", "aiohttp", "urllib", "libwww",
    "facebookexternalhit", "facebookcatalog", "meta-externalagent", "twitterbot",
    "linkedinbot", "slackbot", "discordbot", "telegrambot", "whatsapp", "vercel",
    # cloud-Android emulators + headless/automation farms (referrer-spoofing bot traffic)
    "redroid", "genymotion", "bluestacks", "nox", "memu", "andy", "appium",
    "selenium", "webdriver", "puppeteer", "playwright", "cypress", "katalon",
    # EOL browsers = scanners/bots spoofing old UAs (nobody runs these in 2026)
    "msie 6", "msie 7", "msie 8", "msie 9", "trident/4", "windows nt 5",
)

# Our own surfaces / identities — never a prospect.
_OWNER_REFS = ("docsapp.dev", "redacted", "signalhq", "builtfast", "hailports",
               "Operator", "127.0.0.1", "localhost", "::1")
_OWNER_UTM_SOURCES = ("ownertest", "owner", "test", "selftest", "qa", "internal")

# Major datacenter / cloud / crawler CIDR blocks. Not exhaustive, but covers the
# overwhelming majority of the bot/scraper traffic this funnel actually sees.
# Parsed once at import into _DC_NETS for fast membership checks.
_DC_CIDRS = (
    # Apple
    "17.0.0.0/8",
    # Googlebot + Google services/proxies
    "66.249.64.0/19", "66.102.0.0/20", "72.14.192.0/18", "74.125.0.0/16",
    "209.85.128.0/17", "64.233.160.0/19", "216.58.192.0/19", "142.250.0.0/15",
    "172.217.0.0/16", "108.177.0.0/17", "173.194.0.0/16", "72.13.62.0/24",
    # Google Cloud (GCE)
    "34.64.0.0/10", "35.184.0.0/13", "35.190.0.0/17", "104.196.0.0/14",
    "104.154.0.0/15", "130.211.0.0/16", "146.148.0.0/17", "104.197.0.0/16",
    # AWS
    "3.0.0.0/9", "13.32.0.0/15", "13.224.0.0/14", "15.177.0.0/18", "18.32.0.0/11",
    "52.0.0.0/11", "54.144.0.0/12", "54.224.0.0/12", "99.77.128.0/17", "23.20.0.0/14",
    "44.192.0.0/11", "100.20.0.0/14", "100.24.0.0/13",
    # Azure / Bing (incl. Office365 Safe Links scanner ranges that pre-click email links)
    "13.64.0.0/11", "20.0.0.0/8", "40.64.0.0/10", "40.77.0.0/16", "52.224.0.0/11",
    "104.40.0.0/13", "137.116.0.0/15", "207.46.0.0/16", "157.55.0.0/16", "199.30.16.0/20",
    "135.232.0.0/14", "135.130.0.0/16", "51.4.0.0/15", "51.10.0.0/15", "4.144.0.0/12", "20.150.0.0/15",
    # OVH
    "51.68.0.0/14", "51.75.0.0/16", "51.83.0.0/16", "51.89.0.0/16", "51.91.0.0/16",
    "51.255.0.0/16", "54.36.0.0/14", "137.74.0.0/16", "139.99.0.0/16", "144.217.0.0/16",
    "147.135.0.0/16", "149.56.0.0/16", "158.69.0.0/16", "167.114.0.0/16", "178.32.0.0/15",
    "198.27.64.0/18", "213.32.0.0/16",
    # Tencent Cloud
    "43.128.0.0/10", "49.51.0.0/16", "62.234.0.0/16", "81.68.0.0/14", "101.32.0.0/13",
    "101.42.0.0/15", "119.28.0.0/15", "124.156.0.0/16", "124.220.0.0/14", "124.222.0.0/15",
    "129.211.0.0/16", "129.226.0.0/16", "132.232.0.0/16", "134.175.0.0/16", "150.158.0.0/16",
    "162.62.0.0/16", "170.106.0.0/16", "175.24.0.0/14", "175.27.0.0/16", "175.178.0.0/16",
    "1.12.0.0/14", "1.116.0.0/15", "106.52.0.0/14", "106.55.0.0/16", "109.244.0.0/16",
    "152.136.0.0/16", "159.75.0.0/16", "122.51.0.0/16", "111.229.0.0/16", "111.230.0.0/16",
    "121.4.0.0/16", "121.5.0.0/16", "42.192.0.0/15", "81.69.0.0/16", "1.13.0.0/16", "1.14.0.0/15",
    "82.156.0.0/16", "43.155.0.0/16", "43.156.0.0/15", "139.155.0.0/16", "139.199.0.0/16",
    # DigitalOcean
    "104.131.0.0/16", "138.197.0.0/16", "159.65.0.0/16", "165.227.0.0/16", "167.99.0.0/16",
    "178.62.0.0/16", "188.166.0.0/16", "206.189.0.0/16", "134.209.0.0/16", "64.225.0.0/16",
    "146.190.0.0/16", "143.110.128.0/17",
    # Hetzner
    "5.9.0.0/16", "88.99.0.0/16", "94.130.0.0/16", "116.202.0.0/15", "135.181.0.0/16",
    "144.76.0.0/16", "159.69.0.0/16", "168.119.0.0/16", "195.201.0.0/16", "49.12.0.0/15",
    "65.21.0.0/16", "78.46.0.0/15",
    # Linode / Akamai
    "45.33.0.0/17", "45.56.64.0/18", "45.79.0.0/16", "96.126.96.0/19", "139.162.0.0/16",
    "172.104.0.0/15", "173.255.192.0/18", "192.155.80.0/20", "23.92.16.0/20",
    # Cloudflare
    "104.16.0.0/13", "172.64.0.0/13", "162.158.0.0/15", "173.245.48.0/20", "103.21.244.0/22",
    "188.114.96.0/20", "190.93.240.0/20", "198.41.128.0/17", "131.0.72.0/22",
    # Misc hosting frequently seen scanning
    "23.27.0.0/16",
    # Facebook/Meta crawlers + unfurlers (showed up as fake "humans" from facebook.com referrers)
    "31.13.24.0/21", "31.13.64.0/18", "66.220.144.0/20", "69.63.176.0/20", "69.171.224.0/19",
    "157.240.0.0/16", "173.252.64.0/18", "204.15.20.0/22", "179.60.192.0/22",
    # IPv6 datacenter (Google / AWS / Cloudflare)
    "2600:1900::/28", "2607:f8b0::/32", "2a00:1450::/32", "2600:1f00::/24",
    "2606:4700::/32", "2a06:98c0::/29", "2a03:2880::/29",
)


def _build_nets():
    nets = []
    for c in _DC_CIDRS:
        try:
            nets.append(ipaddress.ip_network(c))
        except ValueError:
            continue
    return nets


_DC_NETS = _build_nets()


def _owner_ips() -> set[str]:
    raw = os.environ.get("OWNER_PUBLIC_IPS", "") or os.environ.get("OWNER_PUBLIC_IP", "")
    return {x.strip() for x in raw.replace(";", ",").split(",") if x.strip()}


def is_bot_ua(ua: str) -> bool:
    ua = (ua or "").lower()
    return bool(ua) and any(b in ua for b in _BOT_UA)


def is_datacenter_ip(ip: str) -> bool:
    ip = str(ip or "").strip()
    if not ip:
        return False
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in n for n in _DC_NETS)


# Synthetic / internal clients that are US, not a prospect: FastAPI TestClient,
# localhost, private/loopback/reserved IPs, and any non-public/unparseable token.
# A real prospect ALWAYS arrives from a valid public IP — anything else is our own
# test traffic, a health check, or junk, and must never count as human.
_SYNTHETIC_IP_TOKENS = ("testclient", "localhost", "127.0.0.1", "::1", "unknown", "-", "none")


def is_internal_ip(ip: str) -> bool:
    ip = str(ip or "").strip().lower()
    if not ip or ip in _SYNTHETIC_IP_TOKENS:
        return True
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True  # unparseable (e.g. "testclient") -> synthetic, not a real visitor
    return (addr.is_private or addr.is_loopback or addr.is_link_local
            or addr.is_reserved or addr.is_unspecified or addr.is_multicast)


def _utm_source(utms: dict) -> str:
    if not isinstance(utms, dict):
        return ""
    for k in ("source", "s", "utm_source"):
        v = utms.get(k)
        if v:
            return str(v)
    return ""


def _real_referrer(ref: str) -> bool:
    ref = (ref or "").strip().lower()
    if not ref:
        return False
    return not any(o in ref for o in _OWNER_REFS)


def classify(e: dict) -> dict:
    """Classify one funnel event.

    Returns dict: verdict (owner|bot|datacenter|human|unknown), is_human (bool),
    is_attributed (bool: a human carrying one of OUR utm_source tags), source (str).
    """
    ip = str(e.get("ip") or "")
    ua = e.get("ua") or ""
    ref = e.get("ref") or ""
    utms = e.get("utms") or {}
    sid = (e.get("sid") or "").strip()
    source = _utm_source(utms)
    src_l = source.lower()

    is_ownertest = src_l in _OWNER_UTM_SOURCES
    owner = (
        is_ownertest
        or is_internal_ip(ip)          # testclient / localhost / private = our own traffic
        or ip in _owner_ips()
        or any(o in ref.lower() for o in _OWNER_REFS)
    )
    if owner:
        return {"verdict": "owner", "is_human": False, "is_attributed": False, "source": source or "owner"}

    if is_bot_ua(ua):
        return {"verdict": "bot", "is_human": False, "is_attributed": False, "source": ""}

    if is_datacenter_ip(ip):
        return {"verdict": "datacenter", "is_human": False, "is_attributed": False, "source": source}

    # A real browser ALWAYS sends a User-Agent. An empty/missing UA is a script, probe, or
    # cookie-setting scanner — never a prospect. This was the funnel-notifier false-positive
    # source: UA-less hits (often datacenter ranges not in the CIDR list) that set a session
    # cookie passed the `sid` signal below and got counted as humans. 39/39 recent "humans"
    # had an empty UA; this drops them to 0 (matching the audited attributed_humans_24h=0).
    if not ua.strip():
        return {"verdict": "no_ua", "is_human": False, "is_attributed": False, "source": source}

    # Residential / unknown IP, real UA. Require a positive human signal so a
    # UA-less scanner from an unlisted host doesn't get counted as a prospect.
    has_signal = bool(sid) or bool(source) or _real_referrer(ref)
    if has_signal:
        return {"verdict": "human", "is_human": True,
                "is_attributed": bool(source), "source": source or ref or "direct"}
    return {"verdict": "unknown", "is_human": False, "is_attributed": False, "source": ""}


def is_human(e: dict) -> bool:
    return classify(e)["verdict"] == "human"


if __name__ == "__main__":  # tiny self-check
    samples = [
        {"ip": "66.249.69.192", "sid": "x", "event": "product_click"},      # googlebot
        {"ip": "170.106.161.78", "sid": "", "event": "storefront_home"},    # tencent dc
        {"ip": "149.56.160.199", "sid": "abc", "event": "pageview"},        # ovh dc
        {"ip": "104.197.69.115", "sid": "abc", "event": "pageview"},        # gce dc
        {"ip": "73.55.12.9", "sid": "s1", "ua": "Mozilla/5.0 (iPhone) Safari", "event": "pageview"},   # residential + session + real UA -> human
        {"ip": "73.55.12.9", "utms": {"source": "seo"}, "ua": "Mozilla/5.0 (Macintosh) Chrome", "event": "pageview"},  # attributed human
        {"ip": "73.55.12.9", "sid": "s1", "event": "pageview"},             # residential + session but NO UA -> NOT human (the fix)
        {"ip": "73.55.12.9", "utms": {"source": "ownertest"}, "event": "pageview"},  # owner
        {"ip": "73.55.12.9", "event": "pageview"},                          # unknown ip, no signal -> not human
        {"ip": "testclient", "utms": {"utm_source": "seo"}, "event": "site_scan_run"},  # FastAPI TestClient -> owner
        {"ip": "10.0.0.5", "sid": "s", "utms": {"source": "seo"}, "event": "pageview"},  # private IP -> owner
    ]
    for s in samples:
        c = classify(s)
        print(f"{s.get('ip'):<16} -> {c['verdict']:<11} human={c['is_human']} attr={c['is_attributed']}")
