#!/usr/bin/env python3
"""Deterministic "is this site broken RIGHT NOW" probe — the hook for Broken-Site Rescue.

$0 and fully autonomous: pure network checks (HTTP reachability, TLS cert expiry,
domain expiry via `whois`, HTTPS presence, mobile-viewport heuristic). NO LLM calls.
Objective signals only — every flag is a fact the business owner can verify, which is
what makes the cold outreach land ("your site's SSL expired 3 days ago", not "looks dated").

Feeds agents/cold_outreach_compliant.py: probe a batch of public businesses, keep the
ones that are measurably broken, and that list is the prospect queue.

  python3 -m core.web_failure_probe example.com httpbin.org
  python3 -m core.web_failure_probe --file data/hustle/biz_domains.txt --broken-only --json
"""
from __future__ import annotations

import ipaddress
import json
import re
import socket
import ssl
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone

TIMEOUT = 8
UA = "Mozilla/5.0 (compatible; SiteHealthBot/1.0; +https://scannerapp.dev)"
SSL_SOON_DAYS = 14


def _norm(domain: str) -> str:
    d = domain.strip().lower()
    d = re.sub(r"^https?://", "", d).split("/")[0].split("?")[0]
    return d.strip().strip(".")


def _host_blocked(host: str) -> bool:
    """Makes 'passive-only' a real check, not just docstring prose: True if the host
    resolves to any non-public (loopback/private/link-local/reserved) address — the
    SSRF block. Unresolvable hosts are NOT blocked here, so a genuinely-dead domain
    still surfaces its real network error instead of a misleading 'blocked' reason."""
    if not host:
        return True
    try:
        infos = socket.getaddrinfo(host, None)
    except Exception:
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            return True
    return False


def _has_dns(host: str):
    """True if the host has at least one A/AAAA record, False if it resolves to
    NOTHING (NXDOMAIN / no address — the domain effectively doesn't exist), None on
    a transient lookup error. Lets the probe tell 'domain doesn't resolve at all'
    (often a lapsed/never-configured domain — a sharp, verifiable fact) apart from
    'resolves but the server is dead'. Reuses getaddrinfo only — no new primitive."""
    if not host:
        return None
    try:
        return bool(socket.getaddrinfo(host, None))
    except socket.gaierror:
        return False
    except Exception:
        return None


class _NoPrivateRedirect(urllib.request.HTTPRedirectHandler):
    """Re-validate every redirect hop — a public domain must not be able to bounce
    the probe into an internal host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        host = urllib.parse.urlparse(newurl).hostname or ""
        if _host_blocked(host):
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_SAFE_OPENER = urllib.request.build_opener(_NoPrivateRedirect)


def _fetch(url: str):
    """Return (status, html, error). Never raises. SSRF-guarded: refuses hosts that
    resolve to internal addresses and validates each redirect hop. Body capped 200KB."""
    host = urllib.parse.urlparse(url).hostname or ""
    if _host_blocked(host):
        return None, "", "blocked-non-public-host"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with _SAFE_OPENER.open(req, timeout=TIMEOUT) as r:
            body = r.read(200_000).decode("utf-8", "ignore")
            return r.status, body, None
    except urllib.error.HTTPError as e:
        return e.code, "", None
    except Exception as e:
        return None, "", f"{type(e).__name__}: {str(e)[:80]}"


def _ssl_days_left_detailed(host: str):
    """(days_left, invalid_reason). days_left: int (negative=expired, -9999=present
    but fails verification), or None when there's no/failed TLS. invalid_reason is a
    plain-English sentence ONLY when the cert is present but invalid — it classifies
    WHY (hostname mismatch / expired / self-signed / untrusted issuer) so the outreach
    states the exact verifiable fact instead of a generic 'invalid'."""
    if _host_blocked(host):
        return None, None
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((host, 443), timeout=TIMEOUT) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as ss:
                cert = ss.getpeercert()
        not_after = cert.get("notAfter")
        if not not_after:
            return None, None
        exp = datetime.strptime(not_after, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
        return int((exp - datetime.now(timezone.utc)).total_seconds() // 86400), None
    except ssl.SSLCertVerificationError as e:  # cert present but invalid -> treat as broken
        msg = (getattr(e, "verify_message", "") or str(e) or "").lower()
        if "hostname mismatch" in msg or "doesn't match" in msg or "ip address mismatch" in msg:
            reason = ("SSL certificate is issued for a different domain (hostname mismatch) — "
                      "browsers show a full-page security warning")
        elif "expired" in msg:
            reason = "SSL certificate has expired"
        elif "self-signed" in msg or "self signed" in msg:
            reason = "SSL certificate is self-signed — browsers don't trust it and warn visitors"
        elif "unable to get local issuer" in msg or "unable to get issuer" in msg \
                or "unable to verify the first certificate" in msg:
            reason = "SSL certificate isn't from a trusted authority — browsers warn visitors"
        else:
            reason = "SSL certificate is invalid/expired"
        return -9999, reason
    except Exception:
        return None, None


def _ssl_days_left(host: str):
    """Days until the TLS cert expires; negative = already expired; -9999 = present but
    invalid. None = no/failed TLS. Back-compat wrapper over _ssl_days_left_detailed()."""
    return _ssl_days_left_detailed(host)[0]


# Parked / for-sale landing markers — registrar/domain-broker park pages. Specific
# phrases only (never a bare "for sale"), so a real shop selling products won't trip it.
_PARK_MARKERS = (
    "this domain is for sale", "this domain may be for sale", "the domain is for sale",
    "domain is for sale", "domain for sale", "buy this domain", "this domain name is for sale",
    "is parked free", "parkingcrew", "sedoparking", "hugedomains", "afternic", "bodis",
    "domainparking", "interested in this domain", "inquire about this domain",
    "this web page is parked", "the domain you have entered is",
)
# Exposed default scaffolding — a server placeholder where a real site should be. Each
# is unambiguous; the "coming soon"/"under construction" ones are gated on a short page
# so the phrase buried in a real content page doesn't false-positive.
_SCAFFOLD_MARKERS = (
    ("apache2 ubuntu default page", "default Apache placeholder page is showing — the real site was never deployed"),
    ("apache2 debian default page", "default Apache placeholder page is showing — the real site was never deployed"),
    ("it works!", "the server's default 'It works!' placeholder is showing instead of a website"),
    ("welcome to nginx", "default nginx placeholder page is showing — the real site was never deployed"),
    ("index of /", "the server is exposing a bare 'Index of /' file listing instead of a website"),
    ("welcome to the famous five-minute wordpress", "an unfinished WordPress install screen is publicly exposed"),
    ("wordpress &rsaquo; installation", "an unfinished WordPress install screen is publicly exposed"),
    ("coming soon", "the site is still just a 'coming soon' placeholder, not a real website"),
    ("under construction", "the site is still an 'under construction' placeholder, not a real website"),
)


def probe(domain: str, security: bool = True) -> dict:
    """Deterministic health verdict for one business domain."""
    host = _norm(domain)
    reasons: list[str] = []
    r = {"domain": host, "broken": False, "reasons": reasons, "severity": "ok"}

    https_status, html, https_err = _fetch(f"https://{host}")
    http_status, http_html, http_err = _fetch(f"http://{host}")
    reachable = https_status is not None or http_status is not None

    if not reachable:
        # Distinguish "domain has no DNS at all" (NXDOMAIN — completely offline, often a
        # lapsed/misconfigured domain) from "resolves but the server is dead". Both critical.
        if _has_dns(host) is False:
            reasons.append("domain has no DNS records — it doesn't resolve at all "
                           "(completely offline; usually a lapsed or misconfigured domain)")
        else:
            reasons.append(f"site unreachable ({https_err or http_err or 'no response'})")
        r["severity"] = "critical"
    else:
        status = https_status or http_status
        if status and status >= 500:
            reasons.append(f"server error (HTTP {status})")
            r["severity"] = "critical"
        else:
            # 4xx homepage: flag ONLY when no scheme returned a success (<400), so a
            # bot-blocking 403 on https while http serves 200 isn't mislabeled broken.
            _codes = [c for c in (https_status, http_status) if c is not None]
            _4xx = [c for c in _codes if 400 <= c < 500]
            if _codes and _4xx and all(c >= 400 for c in _codes):
                reasons.append(f"homepage returns an error (HTTP {_4xx[0]}) — visitors can't load the site")
                r["severity"] = r["severity"] if r["severity"] == "critical" else "high"
        # No working HTTPS at all = modern-broken / insecure
        if https_status is None and http_status is not None:
            reasons.append("no HTTPS (insecure, browsers flag it)")
            r["severity"] = r["severity"] if r["severity"] == "critical" else "high"

    ssl_days, ssl_reason = _ssl_days_left_detailed(host)
    r["ssl_days_left"] = ssl_days
    if ssl_days is not None:
        if ssl_days < 0:
            if ssl_days == -9999:
                reasons.append(ssl_reason or "SSL certificate invalid/expired")
            else:
                reasons.append(f"SSL certificate expired ({-ssl_days}d ago)")
            r["severity"] = "critical"
        elif ssl_days <= SSL_SOON_DAYS:
            reasons.append(f"SSL expires in {ssl_days}d")
            r["severity"] = r["severity"] if r["severity"] == "critical" else "high"

    # domain-expiry (whois) check removed: subprocess fork after Apple-networking SSL calls
    # segfaults Python on macOS (nw_settings_child_has_forked). Weakest signal anyway.
    r["domain_days_left"] = None

    page = html or http_html
    if page and "viewport" not in page.lower():
        reasons.append("not mobile-responsive (no viewport meta)")
        r["severity"] = r["severity"] if r["severity"] in ("critical", "high") else "medium"

    # Staleness — cheapest, highest-yield qualifier: a footer copyright year well in the past
    # is a strong signal of a neglected, rebuild-receptive site (the owner has disengaged).
    if page:
        years = [int(y) for y in re.findall(r"(?:©|&copy;|copyright)[^0-9]{0,12}((?:19|20)\d{2})", page, re.I)]
        if years:
            newest = max(years)
            this_year = datetime.now(timezone.utc).year
            if newest <= this_year - 2:
                reasons.append(f"site looks abandoned (copyright {newest}, {this_year - newest}y stale)")
                r["severity"] = r["severity"] if r["severity"] in ("critical", "high") else "medium"
            r["copyright_year"] = newest

    # Parked / for-sale landing — there is no real site, just a broker/registrar page. High:
    # anyone who visits sees no business at all. Verifiable in 5s by loading the domain.
    if page:
        _pl = page.lower()
        _hit = next((m for m in _PARK_MARKERS if m in _pl), None)
        if _hit:
            reasons.append(f"domain shows a parked / 'for sale' page (\"{_hit}\") — there's no real website here")
            r["severity"] = r["severity"] if r["severity"] == "critical" else "high"

    # Exposed default scaffolding / placeholder — server default page, dir listing, unfinished
    # install, or a stale "coming soon". Medium: the real site was never finished or got wiped.
    if page:
        _pl = page.lower()
        for _marker, _msg in _SCAFFOLD_MARKERS:
            if _marker in _pl:
                if _marker in ("coming soon", "under construction") and len(page) > 4000:
                    continue  # phrase buried in a real content page, not a placeholder
                reasons.append(_msg)
                r["severity"] = r["severity"] if r["severity"] in ("critical", "high") else "medium"
                break

    # Mixed content on a working HTTPS page — active content over plain http. Browsers BLOCK
    # it and flag "Not fully secure", so forms/scripts silently break. High + verifiable.
    if https_status is not None and html:
        _hl = html.lower()
        if re.search(r'<form[^>]+action=["\']http://', _hl):
            reasons.append("a form on the secure (https) page submits over plain http — "
                           "browsers warn/block it and the submission isn't encrypted")
            r["severity"] = r["severity"] if r["severity"] == "critical" else "high"
        elif re.search(r'<(?:script|iframe)[^>]+src=["\']http://', _hl):
            reasons.append("the secure (https) page loads scripts over insecure http (mixed content) — "
                           "browsers block it and flag the page 'Not fully secure'")
            r["severity"] = r["severity"] if r["severity"] == "critical" else "high"

    # Abuser fingerprint — if a broken/stale site ALSO shows a paid vendor's mark, the owner
    # is PAYING someone for this. Turns "your site is broken" into "you're paying for this."
    if page and reasons:
        pl = page.lower()
        m = re.search(r"(?:designed|built|developed|powered|site)\s+by\s*:?\s*([A-Z][\w&.\- ]{2,40})", page)
        if m:
            r["paying_vendor"] = m.group(1).strip()
            reasons.append(f"appears vendor-managed (\"{m.group(1).strip()[:40]}\") yet still broken — likely overpaying")
        else:
            for b in ("wix.com", "godaddy", "squarespace", "weebly", "duda", "vistaprint"):
                if b in pl:
                    r["builder"] = b
                    break

    r["broken"] = bool(reasons)
    if not reasons:
        r["reasons"] = ["healthy"]
    return r


def probe_batch(domains, broken_only=False) -> list[dict]:
    out = []
    for d in domains:
        d = d.strip()
        if not d or d.startswith("#"):
            continue
        v = probe(d)
        if broken_only and not v["broken"]:
            continue
        out.append(v)
    return out


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    as_json = "--json" in argv
    broken_only = "--broken-only" in argv
    domains = [a for a in argv if not a.startswith("--")]
    if "--file" in argv:
        i = argv.index("--file")
        if i + 1 < len(argv):
            with open(argv[i + 1], encoding="utf-8") as f:
                domains = [l for l in f.read().splitlines()]
            domains = [d for d in domains if not d.startswith("--")]
    if not domains:
        print("usage: python3 -m core.web_failure_probe <domain...> | --file <list> [--broken-only] [--json]")
        return 1
    results = probe_batch(domains, broken_only=broken_only)
    if as_json:
        print(json.dumps(results, indent=2))
    else:
        for r in results:
            mark = "BROKEN" if r["broken"] else "ok"
            print(f"[{r['severity']:>8}] {r['domain']:<32} {mark}: {'; '.join(r['reasons'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
