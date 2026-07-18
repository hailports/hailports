#!/usr/bin/env python3
"""Single source of truth for revenue selling/outbound gates.

Every selling/outbound lane should ask THIS module whether it may sell, instead
of each script re-implementing (or ignoring) the gate. Fail-closed: if a flag is
missing or unreadable, the gated lane is treated as DISABLED.

Gate model
----------
Social product-selling (persona1 / personas posting buy links):
    enabled  ==  .SOCIAL_SELL_READY exists  AND  .NO_SOCIAL_SELL absent
  - .NO_SOCIAL_SELL  : owner hard-veto file (kill switch; owner-set). While it
                       exists, social selling is OFF no matter what.
  - .SOCIAL_SELL_READY: product+security readiness confirmation. The
                       make-product-real verdict creates this when the product
                       is CONFIRMED ready+secure. Creating it (and the owner
                       dropping .NO_SOCIAL_SELL) AUTO-ENABLES selling with no
                       further human rewiring — agents/revenue_pursuit.py reads
                       this each tick and starts kicking the selling lanes.

Cold email selling (docsapp/BrandA/broken-site/intent-sku email sends):
    enabled  ==  COLD_OUTREACH_SEND in {1,true,yes,on}
  (owner-set env in .env; currently 0 = stage/dry-run only.)

These are the ONLY two flips a human owns for selling. Find/stage/proof/
fulfillment/delivery lanes are never gated by this module — they always run.
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(os.environ.get("CLAUDE_STACK_DIR", Path(__file__).resolve().parents[1]))

VETO_FILE = ROOT / ".NO_SOCIAL_SELL"
READY_FILE = ROOT / ".SOCIAL_SELL_READY"

_TRUE = {"1", "true", "yes", "on"}


def _env(name: str, default: str = "") -> str:
    val = os.environ.get(name)
    if val is not None:
        return val.strip()
    # Fail-safe .env parse so this works from plain python3 (no dotenv loaded).
    env_path = ROOT / ".env"
    try:
        for line in env_path.read_text(errors="ignore").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k.strip() == name:
                return v.strip().strip("'\"")
    except OSError:
        pass
    return default


def social_sell_enabled() -> bool:
    """True only when readiness is confirmed AND the owner veto is gone."""
    try:
        return READY_FILE.exists() and not VETO_FILE.exists()
    except OSError:
        return False  # fail-closed


def cold_email_enabled() -> bool:
    return _env("COLD_OUTREACH_SEND", "0").lower() in _TRUE


def policy() -> dict:
    """Full gate snapshot + the human-actionable blocking list.

    blocking_gates lists exactly the flags/logins a human must flip to open a
    currently-closed selling lane — the orchestrator surfaces this verbatim.
    """
    veto = VETO_FILE.exists()
    ready = READY_FILE.exists()
    social = social_sell_enabled()
    cold = cold_email_enabled()
    blocking = []
    if not social:
        if veto and ready:
            blocking.append(
                "social-sell: product is CONFIRMED ready (.SOCIAL_SELL_READY) — "
                "owner remove .NO_SOCIAL_SELL to go live")
        elif veto:
            blocking.append(
                "social-sell: .NO_SOCIAL_SELL active (owner veto) + product not yet "
                "confirmed ready (.SOCIAL_SELL_READY missing)")
        else:
            blocking.append(
                "social-sell: product not yet confirmed ready — make-product-real "
                "verdict must create .SOCIAL_SELL_READY")
    if not cold:
        blocking.append("cold-email: set COLD_OUTREACH_SEND=1 in .env to enable email sends")
    return {
        "social_sell_enabled": social,
        "cold_email_enabled": cold,
        "no_social_sell_veto": veto,
        "social_sell_ready": ready,
        "docsapp_send_enabled": _env("docsapp_SEND_ENABLED", "false").lower() in _TRUE,
        "maroon_send_enabled": _env("MAROON_SENDER_SEND_ENABLED", "false").lower() in _TRUE,
        "revenue_autopilot_send_enabled": _env("REVENUE_AUTOPILOT_SEND_ENABLED", "false").lower() in _TRUE,
        "brevo_send_enabled": _env("BREVO_SEND_ENABLED", "false").lower() in _TRUE,
        "blocking_gates": blocking,
    }


if __name__ == "__main__":
    import json
    print(json.dumps(policy(), indent=2))
