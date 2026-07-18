"""Domain Buyer — autonomous domain acquisition for revenue agents.

Called by product_incubator or any revenue agent when a market opportunity
needs a brand + domain + email NOW. Fully autonomous within safety limits.

Safety rails:
  - Hard budget cap: $15/domain (Cloudflare at-cost pricing)
  - Daily purchase limit: 2 domains/day. 3rd attempt blocks and alerts operator.
  - Every purchase sends a Telegram notification with domain + reason.
  - All purchases logged to data/hustle/domain_purchases.jsonl.
  - Never purchases domains containing operator PII.

Flow:
  Agent finds opportunity → calls acquire_domain(concept, reason)
  → LLM generates 40 catchy candidates
  → RDAP checks real availability
  → scores by catchiness/brandability
  → purchases #1 via Cloudflare Registrar
  → wires iCloud email DNS
  → notifies operator via Telegram
  → returns domain + email-ready status
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
import urllib.request
import urllib.error
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("domain_buyer")

BASE = Path(os.path.expanduser("~/claude-stack"))
PURCHASE_LOG = BASE / "data" / "hustle" / "domain_purchases.jsonl"
CDT = timezone(timedelta(hours=-5))

# ── Safety Constants ─────────────────────────────────────────────────────────
MAX_DAILY_PURCHASES = 2
MAX_PRICE_USD = 15.00
BLOCKED_TERMS = re.compile(
    r"CompanyA|Operator|alexdemo|Operator2|tavant|vrm|branda",
    re.IGNORECASE,
)

# ── iCloud Custom Email DNS ──────────────────────────────────────────────────
ICLOUD_MX = [
    {"priority": 10, "content": "mx01.mail.icloud.com"},
    {"priority": 10, "content": "mx02.mail.icloud.com"},
]
ICLOUD_SPF = "v=spf1 include:icloud.com ~all"

# ── TLD scoring (lower rank = better brand) ──────────────────────────────────
_TLD_RANK = {
    ".com": 1, ".dev": 2, ".io": 3, ".co": 4, ".app": 5,
    ".pro": 6, ".cloud": 7, ".tech": 8, ".net": 9, ".org": 10,
}

# Cloudflare at-cost price estimates by TLD
_TLD_PRICES = {
    ".com": 10.11, ".dev": 12.00, ".io": 33.98, ".co": 11.50,
    ".app": 14.00, ".net": 10.55, ".org": 10.11, ".cloud": 8.14,
    ".tech": 3.48, ".pro": 9.27,
}

# TLDs that fit our budget
_BUDGET_TLDS = [tld for tld, price in _TLD_PRICES.items() if price <= MAX_PRICE_USD]


# ── Cloudflare API ───────────────────────────────────────────────────────────

def _cf_token() -> str:
    token = os.environ.get("CLOUDFLARE_API_TOKEN", "")
    if not token:
        try:
            from dotenv import load_dotenv
            load_dotenv(BASE / ".env")
            token = os.environ.get("CLOUDFLARE_API_TOKEN", "")
        except Exception:
            pass
    if not token:
        raise ValueError("CLOUDFLARE_API_TOKEN not set")
    return token


def _cf_headers() -> dict:
    return {
        "Authorization": f"Bearer {_cf_token()}",
        "Content-Type": "application/json",
    }


def _cf_request(method: str, path: str, body: dict | None = None) -> dict:
    url = f"https://api.cloudflare.com/client/v4{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, headers=_cf_headers(), method=method)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def _cf_get(path: str) -> dict:
    return _cf_request("GET", path)


def _cf_post(path: str, body: dict) -> dict:
    return _cf_request("POST", path, body)


def _get_account_id() -> str:
    data = _cf_get("/accounts?page=1&per_page=5")
    accounts = data.get("result", [])
    if not accounts:
        raise ValueError("No Cloudflare accounts found")
    return accounts[0]["id"]


# ── Availability Check (RDAP/whois) ─────────────────────────────────────────

def check_available(domain: str) -> bool:
    """Check real-world domain availability via whois."""
    try:
        result = subprocess.run(
            ["whois", domain],
            capture_output=True, text=True, timeout=10,
        )
        output = result.stdout.lower()
        # Domain is available if whois says so
        available_signals = [
            "no match for",
            "not found",
            "no data found",
            "domain not found",
            "no entries found",
            "status: available",
            "status: free",
            "no object found",
        ]
        taken_signals = [
            "domain name:",
            "registrar:",
            "creation date:",
            "registered on:",
            "registry domain id:",
        ]
        for signal in available_signals:
            if signal in output:
                return True
        for signal in taken_signals:
            if signal in output:
                return False
        # If unclear, assume taken
        return False
    except Exception as e:
        log.warning(f"whois check failed for {domain}: {e}")
        return False


# ── Safety: Daily Purchase Ledger ────────────────────────────────────────────

def _purchases_today() -> list[dict]:
    """Read today's purchases from the log."""
    if not PURCHASE_LOG.exists():
        return []
    today = date.today().isoformat()
    purchases = []
    for line in PURCHASE_LOG.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
            if entry.get("date") == today:
                purchases.append(entry)
        except json.JSONDecodeError:
            continue
    return purchases


def _check_daily_limit() -> tuple[bool, int]:
    """Returns (allowed, count_today). Blocks on 3rd attempt."""
    today_purchases = _purchases_today()
    count = len(today_purchases)
    return count < MAX_DAILY_PURCHASES, count


def _log_purchase(domain: str, reason: str, price: float | None, success: bool):
    PURCHASE_LOG.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "domain": domain,
        "reason": reason,
        "price_est": price,
        "success": success,
        "date": date.today().isoformat(),
        "timestamp": datetime.now(CDT).isoformat(),
    }
    with open(PURCHASE_LOG, "a") as f:
        f.write(json.dumps(entry) + "\n")


# ── Telegram Notifications ───────────────────────────────────────────────────

def _notify(message: str):
    """Send Telegram notification for every domain event."""
    try:
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        if not token:
            from dotenv import load_dotenv
            load_dotenv(BASE / ".env")
            token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        if not token:
            log.warning("No Telegram token for domain notification")
            return

        # Get chat_id from users.toml
        chat_id = ""
        try:
            try:
                import tomllib
            except ImportError:
                import tomli as tomllib
            users = BASE / "config" / "users.toml"
            if users.exists():
                cfg = tomllib.loads(users.read_text())
                chat_id = str(cfg.get("Operator", {}).get("telegram_id", ""))
        except Exception:
            pass
        if not chat_id or chat_id == "0":
            return

        body = json.dumps({"chat_id": chat_id, "text": message[:4000]}).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        log.warning(f"Telegram notification failed: {e}")


# ── Domain Name Generation ───────────────────────────────────────────────────

def generate_candidates(concept: str, count: int = 40) -> list[str]:
    """Generate catchy domain candidates. Uses local LLM, falls back to algorithmic."""
    prompt = f"""Generate {count} catchy, brandable domain names for this business concept:
"{concept}"

Rules:
- 2-3 syllables, under 12 characters before the TLD
- Easy to spell, say, and remember
- Professional but distinctive
- Use these TLDs: .com .dev .co .app .tech .cloud .pro
- Prefer .com and .dev
- NO hyphens, NO numbers
- NO generic names like "bestsalesforce.com"
- Think startup naming: punchy, invented words, clever compounds
- Examples: stripe.com, vercel.com, linear.app, notion.so

Return ONLY domain names, one per line."""

    candidates = []
    try:
        from core import local_client
        import asyncio
        result = asyncio.run(local_client.generate(prompt))
        if result:
            for line in result.strip().split("\n"):
                line = line.strip().lower()
                line = re.sub(r"^[\d.\-)\s]+", "", line)
                line = line.strip("`\"' *")
                if "." in line and 3 < len(line) < 30 and " " not in line:
                    candidates.append(line)
    except Exception as e:
        log.info(f"Local LLM unavailable, using algorithmic fallback: {e}")

    # Algorithmic fallback / supplement
    if len(candidates) < count:
        words = re.findall(r"[a-z]+", concept.lower())
        prefixes = [w[:4] for w in words if len(w) >= 4][:3]
        suffixes = ["hq", "ops", "lab", "hub", "go", "ly", "fy", "io", "x", "ry",
                     "set", "kit", "box", "bay", "run", "zen", "ark", "way"]
        for prefix in prefixes:
            for suffix in suffixes:
                for tld in _BUDGET_TLDS[:4]:
                    name = f"{prefix}{suffix}{tld}"
                    if name not in candidates and len(name) < 25:
                        candidates.append(name)
                        if len(candidates) >= count:
                            break

    # Filter out PII
    candidates = [c for c in candidates if not BLOCKED_TERMS.search(c)]
    # Filter to budget TLDs only
    candidates = [c for c in candidates if any(c.endswith(tld) for tld in _BUDGET_TLDS)]

    return candidates[:count]


# ── Scoring ──────────────────────────────────────────────────────────────────

def score_domain(domain: str) -> float:
    """Score a domain for brandability. Higher = better."""
    name, tld_with_dot = domain.rsplit(".", 1)
    tld = "." + tld_with_dot
    score = 100.0

    # TLD quality
    tld_rank = _TLD_RANK.get(tld, 12)
    score -= tld_rank * 2.5

    # Length: sweet spot is 5-8 chars
    if len(name) <= 5:
        score += 15
    elif len(name) <= 8:
        score += 10
    elif len(name) <= 10:
        score += 0
    else:
        score -= (len(name) - 10) * 5

    # Pronounceability: vowel-consonant mix
    vowels = sum(1 for c in name if c in "aeiou")
    ratio = vowels / max(len(name), 1)
    if 0.25 <= ratio <= 0.55:
        score += 8  # Good mix

    # No triple letters
    if re.search(r"(.)\1\1", name):
        score -= 20

    # No awkward double letters at TLD boundary
    if name[-1] == tld_with_dot[0]:
        score -= 5

    # Price bonus
    price = _TLD_PRICES.get(tld, 15)
    score += max(0, (MAX_PRICE_USD - price))

    return round(score, 1)


# ── Core: Acquire Domain ────────────────────────────────────────────────────

def acquire_domain(concept: str, reason: str = "") -> dict:
    """Fully autonomous domain acquisition.

    Called by revenue agents when an opportunity needs a brand.
    Generates names, checks availability, buys the best one,
    wires DNS for iCloud email, notifies operator.

    Args:
        concept: Business concept / market opportunity description
        reason: Why this domain is needed (for audit trail)

    Returns:
        dict with domain, purchased, dns_configured, etc.
    """
    log.info(f"Domain acquisition: concept='{concept}', reason='{reason}'")

    # ── Safety check: daily limit ────────────────────────────────────────
    allowed, count_today = _check_daily_limit()
    if not allowed:
        msg = (
            f"[REVENUE] DOMAIN PURCHASE BLOCKED\n"
            f"Daily limit reached ({MAX_DAILY_PURCHASES}/day).\n"
            f"Concept: {concept}\n"
            f"Reason: {reason}\n"
            f"Purchases today: {count_today}\n"
            f"Approve or wait until tomorrow."
        )
        _notify(msg)
        log.warning("Daily purchase limit reached, blocking")
        return {
            "success": False,
            "error": "daily_limit_reached",
            "purchases_today": count_today,
            "max_daily": MAX_DAILY_PURCHASES,
            "concept": concept,
        }

    # ── Generate candidates ──────────────────────────────────────────────
    candidates = generate_candidates(concept)
    log.info(f"Generated {len(candidates)} candidates")

    if not candidates:
        return {"success": False, "error": "no_candidates_generated"}

    # ── Check availability (batch, with rate limiting) ───────────────────
    available = []
    checked = 0
    for domain in candidates:
        if len(available) >= 5:
            break  # We have enough good options
        checked += 1
        if check_available(domain):
            s = score_domain(domain)
            price = _TLD_PRICES.get("." + domain.rsplit(".", 1)[1], None)
            if price and price <= MAX_PRICE_USD:
                available.append({"domain": domain, "score": s, "price": price})
                log.info(f"  AVAILABLE: {domain} (score={s}, ~${price})")
        time.sleep(0.5)  # Rate limit whois

    if not available:
        log.info(f"No available domains found after checking {checked}")
        return {
            "success": False,
            "error": "no_available_domains",
            "candidates_checked": checked,
        }

    # ── Pick the best ────────────────────────────────────────────────────
    available.sort(key=lambda x: x["score"], reverse=True)
    best = available[0]
    domain = best["domain"]
    log.info(f"Best: {domain} (score={best['score']}, ~${best['price']})")

    # ── Purchase via Cloudflare ──────────────────────────────────────────
    purchased = False
    purchase_error = None
    try:
        account_id = _get_account_id()
        result = _cf_post(f"/accounts/{account_id}/registrar/domains", {
            "name": domain,
            "auto_renew": True,
        })
        purchased = result.get("success", False)
        if not purchased:
            purchase_error = str(result.get("errors", []))
    except Exception as e:
        purchase_error = str(e)

    _log_purchase(domain, reason or concept, best.get("price"), purchased)

    if not purchased:
        log.error(f"Purchase failed for {domain}: {purchase_error}")
        # Try the runner-up
        if len(available) > 1:
            log.info("Trying runner-up...")
            domain = available[1]["domain"]
            try:
                result = _cf_post(f"/accounts/{account_id}/registrar/domains", {
                    "name": domain,
                    "auto_renew": True,
                })
                purchased = result.get("success", False)
                if purchased:
                    _log_purchase(domain, reason or concept, available[1].get("price"), True)
            except Exception:
                pass

    if not purchased:
        _notify(
            f"[REVENUE] Domain purchase FAILED\n"
            f"Concept: {concept}\n"
            f"Tried: {best['domain']}\n"
            f"Error: {purchase_error}\n"
            f"Alternatives checked: {[a['domain'] for a in available[:3]]}"
        )
        return {
            "success": False,
            "error": "purchase_failed",
            "attempted": best["domain"],
            "purchase_error": purchase_error,
            "alternatives": [a["domain"] for a in available[1:4]],
        }

    # ── Wire iCloud DNS ──────────────────────────────────────────────────
    dns_ok = False
    try:
        dns_result = _setup_icloud_dns(domain)
        dns_ok = bool(dns_result.get("records_created"))
    except Exception as e:
        log.warning(f"DNS setup failed: {e}")

    # ── Notify operator ──────────────────────────────────────────────────
    _, new_count = _check_daily_limit()
    _notify(
        f"[REVENUE] Domain purchased: {domain}\n"
        f"Price: ~${best.get('price', '?')}/yr\n"
        f"Concept: {concept}\n"
        f"Reason: {reason}\n"
        f"iCloud DNS: {'configured' if dns_ok else 'needs manual setup'}\n"
        f"Email ready: contact@{domain} (verify in Settings > iCloud)\n"
        f"Purchases today: {new_count}/{MAX_DAILY_PURCHASES}"
    )

    return {
        "success": True,
        "domain": domain,
        "price_est": best.get("price"),
        "score": best["score"],
        "dns_configured": dns_ok,
        "email_domain": f"contact@{domain}",
        "icloud_next_step": "Settings > iCloud > Custom Email Domain > Add Domain" if dns_ok else None,
        "purchases_today": new_count,
        "alternatives": [a["domain"] for a in available[1:4]],
    }


# ── DNS Setup ────────────────────────────────────────────────────────────────

def _setup_icloud_dns(domain: str) -> dict:
    """Add MX + SPF + DKIM records for iCloud Custom Email."""
    account_id = _get_account_id()

    # Add zone if not exists
    zones = _cf_get(f"/zones?name={domain}")
    zone_results = zones.get("result", [])
    if not zone_results:
        log.info(f"Adding zone {domain}...")
        zone_resp = _cf_post("/zones", {
            "name": domain,
            "account": {"id": account_id},
            "type": "full",
        })
        zone_id = zone_resp.get("result", {}).get("id")
        if not zone_id:
            return {"error": "zone_creation_failed"}
    else:
        zone_id = zone_results[0]["id"]

    records = []

    # MX records
    for mx in ICLOUD_MX:
        try:
            _cf_post(f"/zones/{zone_id}/dns_records", {
                "type": "MX", "name": domain,
                "content": mx["content"], "priority": mx["priority"], "ttl": 3600,
            })
            records.append(f"MX {mx['content']}")
        except Exception as e:
            log.warning(f"MX failed: {e}")

    # SPF
    try:
        _cf_post(f"/zones/{zone_id}/dns_records", {
            "type": "TXT", "name": domain, "content": ICLOUD_SPF, "ttl": 3600,
        })
        records.append("TXT SPF")
    except Exception as e:
        log.warning(f"SPF failed: {e}")

    # DKIM CNAME
    try:
        dkim_target = f"sig1.dkim.{domain.replace('.', '-')}.at.icloudmailadmin.com"
        _cf_post(f"/zones/{zone_id}/dns_records", {
            "type": "CNAME", "name": f"sig1._domainkey.{domain}",
            "content": dkim_target, "ttl": 3600, "proxied": False,
        })
        records.append("CNAME DKIM")
    except Exception as e:
        log.warning(f"DKIM failed: {e}")

    return {"domain": domain, "zone_id": zone_id, "records_created": records}


# ── CLI ──────────────────────────────────────────────────────────────────────

def list_domains() -> list[dict]:
    account_id = _get_account_id()
    data = _cf_get(f"/accounts/{account_id}/registrar/domains")
    return data.get("result", [])


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Domain Buyer")
    parser.add_argument("--concept", help="Business concept for domain brainstorming")
    parser.add_argument("--reason", default="", help="Why this domain is needed")
    parser.add_argument("--dry-run", action="store_true", help="Check availability only, don't buy")
    parser.add_argument("--list", action="store_true", help="List owned domains")
    parser.add_argument("--check", help="Check single domain availability")
    parser.add_argument("--dns", help="Set up iCloud DNS for a domain you already own")
    parser.add_argument("--ledger", action="store_true", help="Show purchase history")
    args = parser.parse_args()

    try:
        from dotenv import load_dotenv
        load_dotenv(BASE / ".env")
    except Exception:
        pass

    if args.list:
        for d in list_domains():
            print(f"  {d.get('name', '?')} — {d.get('status', '?')}")

    elif args.check:
        avail = check_available(args.check)
        print(f"  {args.check}: {'AVAILABLE' if avail else 'taken'}")

    elif args.dns:
        result = _setup_icloud_dns(args.dns)
        print(json.dumps(result, indent=2))

    elif args.ledger:
        if PURCHASE_LOG.exists():
            for line in PURCHASE_LOG.read_text().splitlines():
                if line.strip():
                    print(line)
        else:
            print("No purchases yet.")

    elif args.concept:
        if args.dry_run:
            candidates = generate_candidates(args.concept)
            print(f"Checking {len(candidates)} candidates...")
            for domain in candidates:
                avail = check_available(domain)
                if avail:
                    s = score_domain(domain)
                    tld = "." + domain.rsplit(".", 1)[1]
                    price = _TLD_PRICES.get(tld, "?")
                    print(f"  AVAILABLE: {domain:25s}  score={s:5.1f}  ~${price}")
                time.sleep(0.5)
        else:
            result = acquire_domain(args.concept, args.reason)
            print(json.dumps(result, indent=2))

    else:
        parser.print_help()
