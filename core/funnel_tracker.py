#!/usr/bin/env python3
"""funnel_tracker.py — unified revenue attribution ledger.

Every revenue surface logs stages here so we can SEE the funnel end to end:
  send -> open -> click -> reply -> checkout_started -> purchase

One append-only JSONL ledger keyed by (email, lane). Deterministic, zero LLM cost.
Read it via revenue_dashboard.py or `python3 -m core.funnel_tracker --summary`.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(os.environ.get("CLAUDE_STACK_DIR", Path(__file__).resolve().parents[1]))
LEDGER = ROOT / "data" / "hustle" / "funnel_ledger.jsonl"

STAGES = ("send", "open", "click", "reply", "email_capture", "checkout_started", "purchase", "refund",
          "referral_share", "referral_signup", "referral_redeem", "referral_visit")

# Ref codes are deterministic but non-reversible: derived from a page slug / scan id (never owner or
# visitor PII), so a shared link can never leak an identity or bridge two house brands. Anonymity-safe.
_REF_SALT = os.environ.get("VIRAL_REF_SALT", "viral-loop-v1")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def log_event(
    stage: str,
    *,
    email: str = "",
    lane: str = "",
    company: str = "",
    amount_usd: float | None = None,
    variant: str = "",
    product: str = "",
    detail: str = "",
) -> None:
    """Append one funnel event. Never raises — funnel logging must not break a sender."""
    try:
        if stage not in STAGES:
            stage = f"other:{stage}"
        LEDGER.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "ts": _now(),
            "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "stage": stage,
            "email": (email or "").strip().lower(),
            "lane": lane or "",
            "company": company or "",
            "variant": variant or "",
            "product": product or "",
        }
        if amount_usd is not None:
            row["amount_usd"] = round(float(amount_usd), 2)
        if detail:
            row["detail"] = detail[:300]
        with open(LEDGER, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, sort_keys=True) + "\n")
    except Exception:
        pass


# Convenience wrappers used by callers
def log_send(email, company="", lane="docsapp", variant="", product=""):
    log_event("send", email=email, company=company, lane=lane, variant=variant, product=product)


def log_reply(email, company="", lane="docsapp", detail=""):
    log_event("reply", email=email, company=company, lane=lane, detail=detail)


def log_purchase(email, amount_usd, product="", lane="", company=""):
    log_event("purchase", email=email, amount_usd=amount_usd, product=product, lane=lane, company=company)


def log_checkout_started(email, amount_usd, product="", lane=""):
    log_event("checkout_started", email=email, amount_usd=amount_usd, product=product, lane=lane)


# Referral loop (give-to-get): the share is rewarded, never a review. `code` is the
# anonymity-safe sharer code (a hash, never an email/name) and rides the `variant` slot.
def log_referral_share(code, lane="referral", product=""):
    log_event("referral_share", lane=lane, variant=code or "", product=product)


def log_referral_signup(code, lane="referral", product="", detail=""):
    log_event("referral_signup", lane=lane, variant=code or "", product=product, detail=detail)


# ── Viral free-tool loop (share-to-unlock + result-sharing) ──────────────────────────────────
# A user shares a result link (referral_share) -> a stranger lands on it (referral_visit) -> either
# party trips a share-to-unlock gate (referral_redeem). The ratio referral_visit / referral_share is
# the viral coefficient K: how many fresh visitors each share drags in. K is the whole point of the
# loop — it multiplies traffic we already earn for $0.
def mint_ref_code(seed: str) -> str:
    """Short, stable, non-reversible ref code for a page/result. Anonymity-safe: seed is a slug or
    scan id, never PII, so the code can't leak an identity. 10 hex chars = plenty of space, no clash."""
    return hashlib.sha1(f"{_REF_SALT}:{(seed or '').strip().lower()}".encode()).hexdigest()[:10]


def log_referral_visit(code, lane="referral", url="", detail=""):
    """A stranger arrived on a shared (ref-tagged) link — the inbound half of the viral coefficient."""
    log_event("referral_visit", lane=lane, variant=code or "", detail=(detail or url)[:300])


def log_referral_redeem(code, lane="referral", product="", detail=""):
    """A share-to-unlock gate was satisfied (bonus revealed / pro result unlocked)."""
    log_event("referral_redeem", lane=lane, variant=code or "", product=product, detail=detail)


def viral_summary(days: int = 30) -> dict:
    """Aggregate the viral loop: shares, inbound referral visits, signups, redeems, and the viral
    coefficient K (= referral_visit / referral_share). Reads the same append-only ledger; $0, no LLM."""
    from collections import defaultdict
    if not LEDGER.exists():
        return {"error": "no ledger yet"}
    counts = defaultdict(int)
    by_lane = defaultdict(lambda: defaultdict(int))
    by_code = defaultdict(lambda: defaultdict(int))
    for line in LEDGER.read_text(errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        if not isinstance(r, dict):
            continue
        st = r.get("stage", "")
        if not st.startswith("referral_"):
            continue
        counts[st] += 1
        by_lane[r.get("lane", "?")][st] += 1
        if r.get("variant"):
            by_code[r["variant"]][st] += 1
    shares = counts.get("referral_share", 0)
    visits = counts.get("referral_visit", 0)
    redeems = counts.get("referral_redeem", 0)
    k = round(visits / shares, 3) if shares else 0.0
    return {
        "shares": shares,
        "referral_visits": visits,
        "signups": counts.get("referral_signup", 0),
        "redeems": redeems,
        "viral_coefficient_k": k,           # new visitors generated per share
        "unlock_rate": round(redeems / visits, 3) if visits else 0.0,
        "by_lane": {k2: dict(v) for k2, v in by_lane.items()},
        "top_codes": dict(sorted(by_code.items(), key=lambda kv: -sum(kv[1].values()))[:10]),
    }


def summary(days: int = 7) -> dict:
    """Aggregate the ledger into a funnel view."""
    from collections import defaultdict
    if not LEDGER.exists():
        return {"error": "no ledger yet"}
    cutoff = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    by_stage = defaultdict(int)
    by_lane_stage = defaultdict(lambda: defaultdict(int))
    by_variant = defaultdict(lambda: defaultdict(int))
    revenue = 0.0
    today_stage = defaultdict(int)
    for line in LEDGER.read_text(errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        if not isinstance(r, dict):
            continue  # skip legacy non-dict rows
        stage = r.get("stage", "?")
        by_stage[stage] += 1
        by_lane_stage[r.get("lane", "?")][stage] += 1
        if r.get("variant"):
            by_variant[r["variant"]][stage] += 1
        if stage == "purchase":
            revenue += float(r.get("amount_usd", 0) or 0)
        if r.get("date") == cutoff:
            today_stage[stage] += 1
    return {
        "by_stage": dict(by_stage),
        "today": dict(today_stage),
        "by_lane": {k: dict(v) for k, v in by_lane_stage.items()},
        "by_variant": {k: dict(v) for k, v in by_variant.items()},
        "total_revenue_usd": round(revenue, 2),
    }


if __name__ == "__main__":
    if "--viral" in sys.argv:
        print(json.dumps(viral_summary(), indent=2))
    else:
        print(json.dumps(summary(), indent=2))
