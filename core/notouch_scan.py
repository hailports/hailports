#!/usr/bin/env python3
"""No-touch posture scan — reads PUBLIC third-party records ABOUT a target without ever
connecting to the target's own systems. Safe + legal on ANY entity (SMB, enterprise,
nonprofit, government) because there is zero contact with them — only public sources.

Sources (all free / $0, low-overhead):
  - DNS resolvers      → SPF/DMARC/DKIM (email spoofability)   [reuses email_auth_probe]
  - RDAP registry      → domain expiry                          [reuses shadow scanner]
  - crt.sh CT logs     → subdomains, cert renewal-gap, typosquats   (via curl/IPv6:
                         crt.sh is Cloudflare-fronted and bans this host's IPv4)
  - public GitHub      → leaked secrets / domain in public repos     (gh CLI or token)

ACTING on findings still differs by target (SMB = proof-first outreach; big-entity =
authorized bounty/VDP only). This module only DETECTS — universally and anonymously.

  python3 -m core.notouch_scan example.com
"""
from __future__ import annotations
import json, re, subprocess, sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _curl_json(url: str, timeout: int = 20):
    """curl negotiates IPv6, dodging the IPv4 Cloudflare ASN ban that blocks urllib here."""
    try:
        out = subprocess.run(["curl", "-s", "--max-time", str(timeout),
                              "-H", "User-Agent: notouch-recon/1.0", url],
                             capture_output=True, text=True, timeout=timeout + 5).stdout
        return json.loads(out) if out.strip() else None
    except Exception:
        return None


def _crt_sh(domain: str) -> list:
    findings, rows = [], _curl_json(f"https://crt.sh/?q=%25.{domain}&output=json", 25) or []
    if not rows:
        return findings
    subs, le_newest = set(), None
    for r in rows:
        for nm in str(r.get("name_value", "")).splitlines():
            nm = nm.strip().lstrip("*.").lower()
            if nm.endswith(domain):
                subs.add(nm)
        # track newest Let's Encrypt cert to spot a stalled auto-renew
        if "let's encrypt" in str(r.get("issuer_name", "")).lower():
            try:
                nb = datetime.strptime(r["not_before"][:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
                le_newest = nb if (le_newest is None or nb > le_newest) else le_newest
            except Exception:
                pass
        # typosquat/lookalike cert minted for a confusable name
        nv = str(r.get("name_value", "")).lower()
        if domain.split(".")[0] in nv and domain not in nv and re.search(r"(login|secure|pay|verify|-)", nv):
            findings.append({"severity": "high", "kind": "typosquat_cert",
                             "proof": f"lookalike cert for {nv.splitlines()[0][:40]} — impersonation kit may be staged"})
    if le_newest:
        age = (datetime.now(timezone.utc) - le_newest).days
        if 70 <= age <= 120:
            findings.append({"severity": "high", "kind": "cert_renewal_stalled",
                             "proof": f"newest Let's Encrypt cert is {age}d old — auto-renew likely broke; 'Not Secure' wall imminent"})
    if len(subs) > 25:
        findings.append({"severity": "info", "kind": "attack_surface",
                         "proof": f"{len(subs)} subdomains visible in CT logs — large public attack surface to review"})
    return findings[:5]


def _github_leaks(domain: str) -> list:
    """Public GitHub code search for the domain (potential leaked config/secrets). Reports
    the leak LOCATION only, never the secret. Uses gh CLI (preferred) or GITHUB_TOKEN."""
    import os
    q = f'"{domain}" (password OR secret OR api_key OR token OR AKIA)'
    data = None
    try:  # gh CLI carries its own auth
        out = subprocess.run(["gh", "api", "-X", "GET", "search/code", "-f", f"q={q}", "-f", "per_page=5"],
                             capture_output=True, text=True, timeout=25)
        if out.returncode == 0 and out.stdout.strip():
            data = json.loads(out.stdout)
    except Exception:
        data = None
    if data is None:
        tok = os.environ.get("GITHUB_TOKEN", "").strip()
        if not tok:
            return []
        import urllib.request, urllib.parse
        try:
            req = urllib.request.Request(
                "https://api.github.com/search/code?q=" + urllib.parse.quote(q) + "&per_page=5",
                headers={"Authorization": "Bearer " + tok, "Accept": "application/vnd.github+json",
                         "User-Agent": "notouch-recon"})
            data = json.load(urllib.request.urlopen(req, timeout=20))
        except Exception:
            return []
    n = data.get("total_count", 0) if isinstance(data, dict) else 0
    if n:
        return [{"severity": "high", "kind": "github_exposure",
                 "proof": f"{n} public GitHub file(s) mention {domain} alongside secret keywords — review for leaked credentials"}]
    return []


def notouch_scan(domain: str) -> dict:
    domain = re.sub(r"^https?://", "", (domain or "").strip().lower()).split("/")[0]
    if "." not in domain:
        return {"domain": domain, "error": "invalid domain", "findings": []}
    findings = []
    try:
        from agents.email_auth_probe import probe as _email
        e = _email(domain)
        for g in (e.get("gaps") or []):
            findings.append({"severity": e.get("severity", "high"), "kind": "email_spoofable", "proof": g})
    except Exception:
        pass
    try:
        from agents.shadow_handraiser_scan import _rdap_expiry
        exp = _rdap_expiry(domain)
        if exp is not None:
            days = (exp - datetime.now(timezone.utc)).days
            if days < 0:
                findings.append({"severity": "critical", "kind": "domain_expired", "proof": "domain registration EXPIRED"})
            elif days < 45:
                findings.append({"severity": "high", "kind": "domain_expiring", "proof": f"domain expires in {days} days"})
    except Exception:
        pass
    findings += _crt_sh(domain)
    findings += _github_leaks(domain)
    rank = {"critical": 4, "high": 3, "medium": 2, "low": 1, "info": 0}
    findings.sort(key=lambda f: -rank.get(f.get("severity", "info"), 0))
    return {"domain": domain, "scanned_at": datetime.now(timezone.utc).isoformat(),
            "no_touch": True, "findings": findings,
            "severity": findings[0]["severity"] if findings else "clean",
            "score": sum(rank.get(f.get("severity", "info"), 0) for f in findings)}


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python3 -m core.notouch_scan <domain>"); raise SystemExit(2)
    print(json.dumps(notouch_scan(sys.argv[1]), indent=2))
