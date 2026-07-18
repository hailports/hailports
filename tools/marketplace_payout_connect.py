#!/usr/bin/env python3
"""Connect the owner's EXISTING KYC'd Stripe account (acct_…AemilvQSfv,
payouts already enabled) as the payout method on marketplaces that accept an
external Stripe account — OFF-SCREEN, idempotent, and NEVER starting a fresh KYC.

Adapters:
  * promptbase — Settings -> Payments/Payouts -> "Connect with Stripe"
    (Stripe Express / OAuth). With the owner's Stripe session live, the connect
    screen offers his already-verified account; we take that path and HARD-STOP
    if we instead land on a fresh-KYC registration form (we reuse, never re-KYC).
  * whop — detected honestly: Whop pays sellers through its OWN onboarding
    (Whop Payments / built-in Stripe Connect); it does NOT accept an external
    Stripe account. The adapter reports that rather than pretending to connect.

Pure logic (status interpretation, fresh-KYC detection, account-match) is
unit-tested headless. The browser connect flow needs the owner's one-time
Stripe + marketplace logins to validate live.

CLI:
    python3 -m tools.marketplace_payout_connect --site promptbase --status   # read-only check
    python3 -m tools.marketplace_payout_connect --site promptbase            # connect (idempotent)
    python3 -m tools.marketplace_payout_connect --site whop                   # honest capability report
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.offscreen_browser import SESSION_ROOT  # noqa: E402

# The owner's existing, KYC'd, payouts-enabled Stripe account. We only ever
# REUSE this — we match on the suffix to confirm we're wiring the right account.
STRIPE_ACCT_SUFFIX = "AemilvQSfv"
PAYOUT_STATUS_FILE = SESSION_ROOT / "payout_status.json"

# substrings that betray a FRESH onboarding/KYC form (we must never fill these —
# the account is already verified, so seeing these means the wrong path).
FRESH_KYC_SIGNALS = (
    "social security number", "ssn", "date of birth", "legal business name",
    "verify your identity", "tell us about your business", "industry",
    "upload a photo of your id", "ein", "tax id",
)
# substrings that indicate an existing-account confirm / already-connected state
EXISTING_ACCOUNT_SIGNALS = (
    "connect my account", "use this account", "you're connected",
    "payouts enabled", "connected account", "continue as", "log in to stripe",
)


# ---------------------------------------------------------------------------
# Pure logic (unit-tested, no browser)
# ---------------------------------------------------------------------------
def account_matches(text: str, suffix: str = STRIPE_ACCT_SUFFIX) -> bool:
    """True if the page text references the owner's existing account suffix."""
    return suffix.lower() in (text or "").lower()


def detects_fresh_kyc(text: str) -> bool:
    """True if the page looks like a brand-new KYC/registration form."""
    low = (text or "").lower()
    return any(sig in low for sig in FRESH_KYC_SIGNALS)


def interpret_payout_status(signals: dict) -> dict:
    """Pure status machine. signals keys:
        external_supported: bool   — does this site accept an external Stripe acct
        connected: bool|None       — is a Stripe acct already linked
        payouts_enabled: bool|None — are payouts enabled on the linked acct
        fresh_kyc: bool            — did we hit a new-KYC form
        account_match: bool|None   — does linked acct match the owner's suffix
    Returns {state, message, done}. done=True means no further action needed."""
    if signals.get("external_supported") is False:
        return {"state": "unsupported_external", "done": True,
                "message": ("marketplace does not accept an external Stripe "
                            "account; it uses its own payout onboarding")}
    if signals.get("connected") and signals.get("payouts_enabled"):
        am = signals.get("account_match")
        suffix_note = "" if am is None else (
            " (owner's account)" if am else " (WARNING: different account)")
        return {"state": "connected", "done": True,
                "message": f"Stripe connected, payouts enabled{suffix_note}"}
    if signals.get("fresh_kyc"):
        return {"state": "needs_action_reuse", "done": False,
                "message": ("hit a fresh-KYC form — stopped to avoid a new "
                            "account; reuse the existing one via 'log in to "
                            "Stripe' on the connect screen")}
    if signals.get("connected") and not signals.get("payouts_enabled"):
        return {"state": "connected_pending", "done": False,
                "message": "Stripe linked but payouts not yet enabled"}
    return {"state": "not_connected", "done": False,
            "message": "no Stripe account connected yet"}


def _save_status(site: str, result: dict) -> Path:
    cur = {}
    if PAYOUT_STATUS_FILE.exists():
        try:
            cur = json.loads(PAYOUT_STATUS_FILE.read_text())
        except Exception:
            cur = {}
    cur[site] = {**result, "checked_at": int(time.time())}
    PAYOUT_STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    PAYOUT_STATUS_FILE.write_text(json.dumps(cur, indent=2))
    return PAYOUT_STATUS_FILE


# ---------------------------------------------------------------------------
# Site adapters (browser; need live logins to validate)
# ---------------------------------------------------------------------------
def _text(page, sel="body") -> str:
    try:
        return page.locator(sel).first.inner_text(timeout=4000)
    except Exception:
        return ""


def _click_first(page, sels) -> bool:
    for s in sels:
        try:
            page.locator(s).first.click(timeout=4000)
            return True
        except Exception:
            continue
    return False


def _promptbase_signals(page) -> dict:
    """Read PromptBase's payout settings + probe the connect screen state."""
    page.goto("https://promptbase.com/account/payments",
              wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(2000)
    body = _text(page)
    low = body.lower()
    connected = any(s in low for s in ("payouts enabled", "stripe account",
                                       "connected", "edit payout"))
    payouts_enabled = "payouts enabled" in low or "enabled" in low
    return {
        "external_supported": True,
        "connected": connected,
        "payouts_enabled": payouts_enabled if connected else None,
        "fresh_kyc": detects_fresh_kyc(body),
        "account_match": account_matches(body),
    }


def _promptbase_connect(ctx) -> dict:
    """Idempotent connect: if already connected -> report; else launch the
    Stripe connect and take the existing-account path, HARD-STOP on fresh KYC."""
    page = ctx.new_page()
    sig = _promptbase_signals(page)
    status = interpret_payout_status(sig)
    if status["done"]:
        return status
    # launch connect
    _click_first(page, ['a:has-text("Connect with Stripe")',
                        'button:has-text("Connect with Stripe")',
                        'a:has-text("Connect Stripe")',
                        'button:has-text("Set up payouts")'])
    page.wait_for_timeout(3500)  # redirect to connect.stripe.com
    body = _text(page)
    if detects_fresh_kyc(body) and not account_matches(body):
        # never fill a new KYC — bail and tell the owner to use existing acct
        return interpret_payout_status({"external_supported": True,
                                        "fresh_kyc": True})
    # existing-account path: confirm/continue with the already-verified account
    _click_first(page, ['button:has-text("Connect my account")',
                        'button:has-text("Use this account")',
                        'button:has-text("Agree & submit")',
                        'button:has-text("Continue")'])
    page.wait_for_timeout(4000)
    # re-read promptbase to confirm
    sig2 = _promptbase_signals(page)
    return interpret_payout_status(sig2)


def _whop_report(ctx) -> dict:
    """Whop pays via its OWN onboarding — detect + report honestly (no external
    Stripe attach point)."""
    page = ctx.new_page()
    # Whop's seller payout setup lives under dashboard payments/getting paid
    for url in ("https://whop.com/dashboard/settings/payouts",
                "https://whop.com/dashboard/settings/billing"):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=25000)
            page.wait_for_timeout(1500)
            break
        except Exception:
            continue
    body = _text(page).lower()
    external = any(s in body for s in ("connect with stripe",
                                       "connect external stripe",
                                       "connect your stripe account"))
    if external:
        return interpret_payout_status({"external_supported": True,
                                        "connected": "connected" in body,
                                        "payouts_enabled": "enabled" in body})
    return interpret_payout_status({"external_supported": False})


ADAPTERS = {
    "promptbase": _promptbase_connect,
    "whop": _whop_report,
}


def connect(site: str, status_only: bool = False) -> int:
    if site not in ADAPTERS:
        print(f"[payout:{site}] no payout adapter (known: {', '.join(ADAPTERS)})")
        return 1
    from tools.offscreen_browser import offscreen_context
    with offscreen_context(site, load_session=True) as ctx:
        if status_only:
            page = ctx.new_page()
            if site == "promptbase":
                result = interpret_payout_status(_promptbase_signals(page))
            else:
                result = _whop_report(ctx)
        else:
            result = ADAPTERS[site](ctx)
    _save_status(site, result)
    flag = "DONE" if result.get("done") else "ACTION NEEDED"
    print(f"[payout:{site}] {result['state']} — {result['message']} [{flag}]")
    return 0 if result.get("done") else 2


def main() -> int:
    ap = argparse.ArgumentParser(description="Connect existing Stripe payout to marketplaces")
    ap.add_argument("--site", required=True, choices=sorted(ADAPTERS))
    ap.add_argument("--status", action="store_true", help="read-only status check")
    a = ap.parse_args()
    return connect(a.site, status_only=a.status)


if __name__ == "__main__":
    raise SystemExit(main())
