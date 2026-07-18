#!/usr/bin/env python3
"""brand_dedup.py — cross-brand succession dedup for the house of brands.

Eight publicly-rival brands share ONE private backend. This is the seam that
guarantees a prospect is never hit by two brands back-to-back: every send writes
to a SHARED append-only ledger, and every send first checks it.

Privacy invariant: the shared ledger stores only HASHED identity keys (sha256 of
the normalized email / domain). It never aggregates plaintext PII that could
itself link the brands together if the file leaked.

Public surface (what the senders call):
  - record_contact(email, brand, ...)               -> append a contact row
  - suppressed_by_other_brand(email, brand, days)   -> True if a DIFFERENT brand
                                                       contacted within cooldown
  - may_contact(email, brand, days)                 -> decision dict (also honors
                                                       universal opt-out FIRST)
  - per-brand prospect pools (physically separate files):
      tag_to_pool(email, brand, ...) / pool_contains(email, brand) / iter_pool(brand)
  - choose_owner(email, candidate_brands)           -> deterministic single owner

Global cooldown default: env CROSS_BRAND_COOLDOWN_DAYS (~45d); per-brand override
is passed by the caller (registry cooldown_days).

Run `python3 -m core.brand_dedup --selftest` for unit checks.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(os.environ.get("CLAUDE_STACK_DIR", Path(__file__).resolve().parents[1]))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Ledger path is env-overridable so a test run never touches the live file.
LEDGER = Path(os.environ.get(
    "BRAND_DEDUP_LEDGER",
    str(ROOT / "data" / "hustle" / "cross_brand_contacts.jsonl"),
))
POOL_DIR = Path(os.environ.get(
    "BRAND_DEDUP_POOL_DIR",
    str(ROOT / "data" / "hustle" / "prospect_pools"),
))

DEFAULT_COOLDOWN_DAYS = int(os.environ.get("CROSS_BRAND_COOLDOWN_DAYS", "45"))

# Deterministic owner ordering for overlapping pools (the public-rivalry brands).
BRAND_PRIORITY = (
    "scannerapp", "docsapp", "signalhq", "hailport",
    "builtfast", "opsapp", "frontcounter", "promptsite",
)

# Universal opt-out: an unsubscribe to ANY brand suppresses across ALL of them.
try:
    from agents.core import canspam  # type: ignore
except Exception:  # pragma: no cover
    canspam = None  # type: ignore


# ── Normalization + hashing ───────────────────────────────────────────────────

def _norm_email(email: str | None) -> str:
    return str(email or "").strip().lower()


def _norm_domain(value: str | None) -> str:
    v = str(value or "").strip().lower()
    if "@" in v:
        v = v.split("@", 1)[1]
    return v[4:] if v.startswith("www.") else v


def _norm_brand(brand: str | None) -> str:
    return str(brand or "").strip().lower()


def _h(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _identity_hashes(email: str, *, domain: str = "", company: str = "") -> list[str]:
    """Hashed identity keys: email, domain (so a person matches via their company
    domain too), and company name. Mirrors agents.cross_brand_dedup.identity_keys
    but hashed for the shared ledger."""
    out: list[str] = []
    e = _norm_email(email)
    d = _norm_domain(domain or e)
    c = " ".join(str(company or "").lower().replace(",", " ").replace(".", " ").split())
    if e:
        out.append("email:" + _h(e))
    if d:
        out.append("domain:" + _h(d))
    if c:
        out.append("company:" + _h(c))
    return out


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(ts: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(ts)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


# ── Shared contact ledger ─────────────────────────────────────────────────────

def record_contact(
    email: str,
    brand: str,
    *,
    lane: str = "",
    channel: str = "email",
    domain: str = "",
    company: str = "",
    now: datetime | None = None,
) -> bool:
    """Append one contact row to the shared ledger. Never raises — dedup logging
    must not break a sender. Stores hashed identity keys only."""
    try:
        e = _norm_email(email)
        b = _norm_brand(brand)
        if not e or not b:
            return False
        LEDGER.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "ts": (now or _now()).isoformat(),
            "brand": b,
            "lane": lane or b,
            "channel": channel or "email",
            "id": _h(e),
            "keys": _identity_hashes(e, domain=domain, company=company),
        }
        with LEDGER.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, sort_keys=True) + "\n")
        return True
    except Exception:
        return False


def _iter_ledger():
    if not LEDGER.exists():
        return
    for line in LEDGER.read_text(errors="ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except Exception:
            continue
        if isinstance(row, dict):
            yield row


def contacted_brands(
    email: str,
    *,
    cooldown_days: int | None = None,
    domain: str = "",
    company: str = "",
    now: datetime | None = None,
) -> dict[str, str]:
    """Brands that contacted this identity within the cooldown window.
    Returns {brand -> most_recent_iso_ts}. Matches on any hashed identity key
    (email/domain/company), so a company-domain match counts."""
    days = DEFAULT_COOLDOWN_DAYS if cooldown_days is None else int(cooldown_days)
    now = now or _now()
    want = set(_identity_hashes(email, domain=domain, company=company))
    email_hash = _h(_norm_email(email)) if _norm_email(email) else ""
    if not want:
        return {}
    out: dict[str, str] = {}
    for row in _iter_ledger():
        keys = set(row.get("keys") or [])
        # match any hashed identity key, or the bare email-hash `id` of older rows
        if not (want & keys) and (not email_hash or row.get("id") != email_hash):
            continue
        ts = _parse_ts(row.get("ts", ""))
        if ts is None:
            continue
        if days >= 0 and (now - ts).total_seconds() > days * 86400:
            continue
        b = row.get("brand", "")
        if b and (b not in out or ts.isoformat() > out[b]):
            out[b] = ts.isoformat()
    return out


def suppressed_by_other_brand(
    email: str,
    brand: str,
    cooldown_days: int | None = None,
    *,
    domain: str = "",
    company: str = "",
    now: datetime | None = None,
) -> bool:
    """True if a DIFFERENT brand contacted this prospect within the cooldown
    window — so no prospect is hit by two brands in succession. Same-brand
    re-contact is NOT suppressed here (that's the per-sender velocity job of
    core.outreach_governor)."""
    b = _norm_brand(brand)
    hits = contacted_brands(email, cooldown_days=cooldown_days, domain=domain, company=company, now=now)
    return any(other != b for other in hits)


def may_contact(
    email: str,
    brand: str,
    cooldown_days: int | None = None,
    *,
    domain: str = "",
    company: str = "",
    now: datetime | None = None,
) -> dict:
    """Full pre-send gate decision.

    Order: (1) universal opt-out via canspam.is_suppressed — an unsubscribe to
    ANY brand blocks ALL brands; (2) cross-brand cooldown. Returns
    {allowed, reason, blocking_brand, days_remaining}.
    """
    if canspam is not None and canspam.is_suppressed(email):
        return {"allowed": False, "reason": "opted_out", "blocking_brand": None, "days_remaining": None}
    b = _norm_brand(brand)
    days = DEFAULT_COOLDOWN_DAYS if cooldown_days is None else int(cooldown_days)
    now = now or _now()
    hits = contacted_brands(email, cooldown_days=days, domain=domain, company=company, now=now)
    others = {k: v for k, v in hits.items() if k != b}
    if others:
        blocker = max(others, key=lambda k: others[k])
        ts = _parse_ts(others[blocker]) or now
        elapsed = (now - ts).total_seconds() / 86400.0
        return {
            "allowed": False,
            "reason": "cross_brand_cooldown",
            "blocking_brand": blocker,
            "days_remaining": max(0, round(days - elapsed, 1)),
        }
    return {"allowed": True, "reason": "ok", "blocking_brand": None, "days_remaining": None}


# ── Per-brand prospect pools (physically separate) ────────────────────────────

def prospect_pool_path(brand: str) -> Path:
    return POOL_DIR / f"{_norm_brand(brand)}.jsonl"


def _pool_email_hashes(brand: str) -> set[str]:
    path = prospect_pool_path(brand)
    out: set[str] = set()
    if not path.exists():
        return out
    for line in path.read_text(errors="ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except Exception:
            continue
        if isinstance(row, dict) and row.get("id"):
            out.add(row["id"])
    return out


def pool_contains(email: str, brand: str) -> bool:
    return _h(_norm_email(email)) in _pool_email_hashes(brand)


def tag_to_pool(email: str, brand: str, *, domain: str = "", company: str = "", **fields) -> bool:
    """Add a prospect to THIS brand's own pool file (hashed id, deduped within the
    pool). Pools are physically separate so brands never share rows on disk.
    Returns True if newly added, False if already present / invalid."""
    e = _norm_email(email)
    b = _norm_brand(brand)
    if not e or not b:
        return False
    if _h(e) in _pool_email_hashes(b):
        return False
    path = prospect_pool_path(b)
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "ts": _now().isoformat(),
        "brand": b,
        "id": _h(e),
        "keys": _identity_hashes(e, domain=domain, company=company),
    }
    for k, v in fields.items():
        if k not in row:
            row[k] = v
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, sort_keys=True) + "\n")
    return True


def iter_pool(brand: str):
    path = prospect_pool_path(brand)
    if not path.exists():
        return
    for line in path.read_text(errors="ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except Exception:
            continue
        if isinstance(row, dict):
            yield row


def choose_owner(email: str, candidate_brands) -> str | None:
    """Deterministic single owner for a prospect claimable by several brands.
    Stable hash(email) -> one brand, tie-broken by BRAND_PRIORITY, so overlapping
    pools never race the same lead. Returns None if no candidates."""
    cands = sorted({_norm_brand(b) for b in (candidate_brands or []) if _norm_brand(b)},
                   key=lambda b: (BRAND_PRIORITY.index(b) if b in BRAND_PRIORITY else 99, b))
    if not cands:
        return None
    e = _norm_email(email)
    if not e:
        return cands[0]
    idx = int(_h(e), 16) % len(cands)
    return cands[idx]


# ── Self-test ─────────────────────────────────────────────────────────────────

def _selftest() -> int:
    import tempfile
    from datetime import timedelta

    fails: list[str] = []
    tmp = Path(tempfile.mkdtemp(prefix="brand_dedup_test_"))
    global LEDGER, POOL_DIR
    LEDGER = tmp / "ledger.jsonl"
    POOL_DIR = tmp / "pools"

    e = "user@example.com"

    # fresh prospect: no brand has contacted -> allowed, not suppressed-by-other
    if suppressed_by_other_brand(e, "signalhq", 45):
        fails.append("fresh prospect wrongly suppressed")
    if not may_contact(e, "signalhq", 45)["allowed"]:
        fails.append("fresh prospect not allowed")

    # signalhq contacts -> a DIFFERENT brand is now suppressed, same brand is not
    record_contact(e, "signalhq", lane="signalhq")
    if not suppressed_by_other_brand(e, "hailport", 45):
        fails.append("second brand NOT suppressed after first contact")
    if suppressed_by_other_brand(e, "signalhq", 45):
        fails.append("same brand wrongly suppressed (should defer to velocity)")
    dec = may_contact(e, "hailport", 45)
    if dec["allowed"] or dec["blocking_brand"] != "signalhq" or dec["reason"] != "cross_brand_cooldown":
        fails.append(f"may_contact cross-brand decision wrong: {dec}")

    # domain-level match: a different mailbox on the same company domain is caught
    if not suppressed_by_other_brand("user@example.com", "hailport", 45,
                                     domain="acme-shop.io"):
        fails.append("company-domain match not suppressed")

    # cooldown expiry: an old contact (60d ago) no longer suppresses at 45d
    old = "old@example.org"
    record_contact(old, "builtfast", now=_now() - timedelta(days=60))
    if suppressed_by_other_brand(old, "opsapp", 45):
        fails.append("expired (60d>45d) contact still suppressed")
    if not suppressed_by_other_brand(old, "opsapp", 90):
        fails.append("contact within 90d not suppressed")

    # per-brand pools are physically separate + deduped
    if not tag_to_pool(e, "signalhq", domain="acme-shop.io"):
        fails.append("tag_to_pool first add failed")
    if tag_to_pool(e, "signalhq"):
        fails.append("tag_to_pool not deduped within a pool")
    if not pool_contains(e, "signalhq"):
        fails.append("pool_contains miss after tag")
    if pool_contains(e, "hailport"):
        fails.append("pools not separate (leak across brands)")
    if prospect_pool_path("signalhq") == prospect_pool_path("hailport"):
        fails.append("pool paths not distinct per brand")

    # deterministic owner assignment
    o1 = choose_owner(e, ["signalhq", "hailport", "opsapp"])
    o2 = choose_owner(e, ["opsapp", "hailport", "signalhq"])
    if o1 != o2 or o1 is None:
        fails.append(f"choose_owner not deterministic: {o1} vs {o2}")

    # ledger stores no plaintext email
    ledger_blob = LEDGER.read_text()
    if e in ledger_blob or "acme-shop.io" in ledger_blob:
        fails.append("plaintext PII leaked into shared ledger")

    if fails:
        print("BRAND_DEDUP SELFTEST FAILED:")
        for f in fails:
            print("  -", f)
        return 1
    print("BRAND_DEDUP SELFTEST PASSED (cooldown, cross-brand suppression, domain-match, "
          "separate pools, deterministic owner, hashed ledger)")
    return 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        raise SystemExit(_selftest())
    raise SystemExit(_selftest())
