#!/usr/bin/env python3
"""Exposure Audit — passive, PUBLIC-ONLY security-posture scan + proof-first report.

White-hat BY CONSTRUCTION. It reads only publicly-observable signals — DNS records, the
TLS certificate a server presents, HTTP response headers, and whether a sensitive file is
already being served PUBLICLY. There is NO exploitation, NO auth bypass, NO intrusion, and
a hard guard that refuses any private/internal/loopback host (so it can never be turned
into an internal-network scanner). One low-volume request per check, identifying UA. Every
finding is something the owner could see themselves, and each ships with the exact fix.

  python3 -m core.exposure_scan example.com
  from core.exposure_scan import scan
  report = scan("example.com")   # {domain, findings:[...], severity, score, report_md}
"""
from __future__ import annotations
import ipaddress
import re
import socket
import ssl
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

UA = "Mozilla/5.0 (compatible; ExposureAudit/1.0; +https://scannerapp.dev/security)"
TIMEOUT = 8

# Sensitive files that must NEVER be publicly served. We GET each once and only flag if the
# response both succeeds AND matches a content signature (avoids false positives from a
# catch-all 200/SPA). Conservative, well-known set — standard for external posture tools.
EXPOSED_PATHS = [
    ("/.env", "env file with live secrets", re.compile(r"(?im)^\s*[A-Z0-9_]*(API_KEY|SECRET|PASSWORD|TOKEN|DB_)")),
    ("/.git/config", "git repo exposed (source + history downloadable)", re.compile(r"\[core\]")),
    ("/.git/HEAD", "git repo exposed", re.compile(r"(?m)^ref:\s+refs/")),
    ("/wp-config.php.bak", "WordPress DB credentials in a public backup", re.compile(r"(?i)DB_PASSWORD")),
    ("/phpinfo.php", "phpinfo() exposes full server config", re.compile(r"(?i)phpinfo\(\)|PHP Version")),
    ("/server-status", "Apache server-status leaks internal traffic", re.compile(r"(?i)Apache Server Status")),
    ("/.aws/credentials", "AWS keys in a public file", re.compile(r"(?i)aws_secret_access_key")),
    ("/backup.sql", "database dump publicly downloadable", re.compile(r"(?i)INSERT INTO|CREATE TABLE")),
]
SEC_HEADERS = {
    "strict-transport-security": "HSTS — forces HTTPS, blocks downgrade attacks",
    "content-security-policy": "CSP — blocks injected scripts / XSS",
    "x-frame-options": "clickjacking protection",
    "x-content-type-options": "MIME-sniffing protection",
}
SEV_RANK = {"critical": 4, "high": 3, "medium": 2, "low": 1, "info": 0}


def _norm(domain: str) -> str:
    d = (domain or "").strip().lower()
    d = re.sub(r"^https?://", "", d).split("/")[0].split("?")[0].split(":")[0]
    return d


def _is_public(host: str) -> bool:
    """HARD white-hat guard: only scan hosts that resolve to PUBLIC IPs. Refuses
    private/loopback/link-local/reserved so this can never probe internal systems."""
    try:
        infos = socket.getaddrinfo(host, None)
    except Exception:
        return False
    if not infos:
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except Exception:
            return False
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return False
    return True


def _get(url: str, head: bool = False):
    req = urllib.request.Request(url, method="HEAD" if head else "GET", headers={"User-Agent": UA})
    return urllib.request.urlopen(req, timeout=TIMEOUT)


def _f(fid, sev, title, proof, fix):
    return {"id": fid, "severity": sev, "title": title, "proof": proof, "fix": fix}


def _tls_findings(host: str) -> list:
    out = []
    ctx = ssl.create_default_context()
    try:
        with socket.create_connection((host, 443), timeout=TIMEOUT) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as ss:
                cert = ss.getpeercert()
        exp = datetime.strptime(cert["notAfter"], "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
        days = (exp - datetime.now(timezone.utc)).days
        if days < 0:
            out.append(_f("tls_expired", "critical", "TLS certificate EXPIRED",
                          f"Cert expired {-days} days ago ({cert['notAfter']}).",
                          "Renew the certificate now (Let's Encrypt auto-renews free)."))
        elif days < 15:
            out.append(_f("tls_expiring", "high", "TLS certificate expiring imminently",
                          f"Cert expires in {days} days.", "Renew / enable auto-renewal."))
    except ssl.SSLCertVerificationError as e:
        out.append(_f("tls_invalid", "high", "TLS certificate invalid / untrusted",
                      f"Browsers will warn visitors: {str(e)[:80]}.", "Install a valid cert (free via Let's Encrypt)."))
    except (socket.timeout, ConnectionRefusedError, OSError):
        out.append(_f("no_https", "high", "No working HTTPS",
                      "Port 443 did not present a valid TLS service.",
                      "Enable HTTPS — Chrome flags HTTP sites 'Not Secure' to every visitor."))
    return out


def _header_findings(host: str):
    findings, body, reached_https = [], "", False
    for scheme in ("https", "http"):
        try:
            resp = _get(f"{scheme}://{host}/")
            reached_https = reached_https or scheme == "https"
            hdrs = {k.lower(): v for k, v in resp.headers.items()}
            if scheme == "http":
                # is HTTP transparently served (no redirect to https)?
                if resp.url.startswith("http://"):
                    findings.append(_f("no_https_redirect", "medium", "HTTP not redirected to HTTPS",
                                       "Plain http:// stays http:// — traffic can be intercepted.",
                                       "301-redirect all HTTP to HTTPS and add HSTS."))
            missing = [SEC_HEADERS[h] for h in SEC_HEADERS if h not in hdrs]
            if missing and scheme == "https":
                findings.append(_f("missing_headers", "medium", f"{len(missing)} security headers missing",
                                   "Missing: " + "; ".join(missing) + ".",
                                   "Add the missing response headers at the server/CDN."))
            try:
                body = resp.read(200_000).decode("utf-8", "ignore")
            except Exception:
                body = ""
            if scheme == "https":
                break
        except urllib.error.HTTPError:
            break
        except Exception:
            continue
    return findings, body


def _exposed_file_findings(host: str) -> list:
    out = []
    for path, label, sig in EXPOSED_PATHS:
        for scheme in ("https", "http"):
            try:
                resp = _get(f"{scheme}://{host}{path}")
                if resp.status == 200:
                    chunk = resp.read(20000).decode("utf-8", "ignore")
                    if sig.search(chunk):
                        out.append(_f(f"exposed{path}", "critical", f"Publicly exposed: {label}",
                                      f"GET {scheme}://{host}{path} returns 200 with sensitive content.",
                                      f"Block public access to {path} immediately (deny in web server / move out of webroot)."))
                break
            except urllib.error.HTTPError:
                break  # 403/404 = good, not exposed
            except Exception:
                continue
    return out


def scan(domain: str) -> dict:
    host = _norm(domain)
    if not host or "." not in host:
        return {"domain": domain, "error": "invalid domain", "findings": []}
    if not _is_public(host):
        return {"domain": host, "error": "host is not a public internet domain (refused)", "findings": []}

    findings: list = []
    # 1. email auth (SPF/DMARC/DKIM) — pure DNS, reuse existing probe
    try:
        from agents.email_auth_probe import probe as _email_probe
        ea = _email_probe(host)
        for g in (ea.get("gaps") or []):
            findings.append(_f("email_auth", ea.get("severity", "high") if ea.get("severity") in SEV_RANK else "high",
                               "Email spoofing exposure", g,
                               "Publish the SPF/DMARC/DKIM records (we provide the exact lines)."))
    except Exception:
        pass
    # 2. TLS  3. headers/redirect  4. exposed files — all public, low-volume
    findings += _tls_findings(host)
    hf, _body = _header_findings(host)
    findings += hf
    findings += _exposed_file_findings(host)

    sev = max((f["severity"] for f in findings), key=lambda s: SEV_RANK.get(s, 0), default="info")
    score = sum(SEV_RANK.get(f["severity"], 0) for f in findings)
    return {"domain": host, "scanned_at": datetime.now(timezone.utc).isoformat(),
            "findings": findings, "severity": sev, "score": score, "report_md": _report_md(host, findings)}


def _report_md(host: str, findings: list) -> str:
    if not findings:
        return f"# Exposure Audit — {host}\n\nNo publicly-observable exposures found. Clean posture. ✅"
    order = sorted(findings, key=lambda f: -SEV_RANK.get(f["severity"], 0))
    lines = [f"# Exposure Audit — {host}", "",
             f"Found **{len(findings)}** publicly-observable exposure(s). Every item below is "
             "visible to anyone on the internet right now — and each has a fix.", ""]
    for f in order:
        lines += [f"### [{f['severity'].upper()}] {f['title']}",
                  f"- **What's exposed:** {f['proof']}",
                  f"- **Fix:** {f['fix']}", ""]
    return "\n".join(lines)


if __name__ == "__main__":
    import json
    target = sys.argv[1] if len(sys.argv) > 1 else ""
    if not target:
        print("usage: python3 -m core.exposure_scan <domain>")
        raise SystemExit(2)
    r = scan(target)
    print(json.dumps({k: v for k, v in r.items() if k != "report_md"}, indent=2))
    print("\n" + r.get("report_md", ""))
