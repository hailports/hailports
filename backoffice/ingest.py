"""ingest.py — arm the existing (dark) finance data into the ledger.

Reads real hustle-lane sources only. HARD firewall: any record whose source is
Outlook/work is EXCLUDED — bills.json/subscriptions.json are polluted with
Work meeting invites scraped from Outlook, and those must never enter the
company books. Every adapter degrades gracefully if its source file is absent.

External aggregates (cumulative revenue, cumulative AI spend) are synced by
posting the *delta* needed to make the GL account match the source total, so
re-running never double-counts.
"""
from __future__ import annotations
import os, json
from . import ledger as L

DATA = os.path.expanduser("~/claude-stack/data")
WORK_SOURCES = {"outlook", "owa", "exchange", "work", "zoom"}  # firewalled out


def _load(name):
    p = os.path.join(DATA, name)
    if not os.path.exists(p):
        return None
    try:
        return json.load(open(p))
    except Exception:
        return None


def _is_work(rec: dict) -> bool:
    src = str(rec.get("source", "")).lower()
    return src in WORK_SOURCES or src.startswith("outlook")


# product/keyword -> brand class. Extend as brands ship products. Default 'general'.
BRAND_MAP = {
    "hailport": "hailports", "hail": "hailports",
    "acme": "acme", "globex": "globex", "initech": "globex",
    "site health": "globex", "broken": "globex",
    "prompt pack": "prompts", "chatgpt prompt": "prompts",
    "ai-visibility": "geo", "geo": "geo", "aeo": "geo",
    "wonka": "wonka", "umbrella": "umbrella",
}


def brand_of(text: str) -> str:
    t = (text or "").lower()
    for kw, brand in BRAND_MAP.items():
        if kw in t:
            return brand
    return "general"


def _resync(book, account, contra, target_cents, source, memo, klass="general"):
    """Idempotent cumulative sync: clears the prior cumulative entry, reposts total."""
    L.init()
    with L.conn() as c:
        row = c.execute("SELECT id FROM journal WHERE source=? AND ext_id=?",
                        (source, f"{source}:cumulative:{account}")).fetchone()
        if row:
            c.execute("DELETE FROM journal WHERE id=?", (row["id"],))
    if target_cents == 0:
        return 0
    typ = dict((cc, t) for cc, _, t in L.CHART)[account]
    if typ == "revenue":
        lines = [(contra, target_cents, klass), (account, -target_cents, klass)]
    else:
        lines = [(account, target_cents, klass), (contra, -target_cents, klass)]
    L.post(book, lines, memo=memo, source=source, ext_id=f"{source}:cumulative:{account}")
    return target_cents


def ingest_ai_cost():
    d = _load("cost_state.json")
    if not d or "alltime" not in d:
        return {"posted_cents": 0, "note": "no cost_state"}
    total = 0
    for model, v in d["alltime"].items():
        if isinstance(v, dict) and "cost_usd" in v:
            total += L.to_cents(v["cost_usd"])
    _resync(L.HUSTLE, "5000", "1000", total, "cost_tracker",
            "cumulative AI/API spend", klass="ops")
    return {"posted_cents": total}


def ingest_revenue():
    d = _load("revenue_state.json")
    if not d:
        return {"posted_cents": 0, "note": "no revenue_state"}
    total = L.to_cents(d.get("total_revenue_usd", 0))
    prods = d.get("products") or []
    klass = brand_of(prods[0]) if len(prods) == 1 else "general"
    _resync(L.HUSTLE, "4000", "1000", total, "revenue_state",
            "cumulative product revenue", klass=klass)
    return {"posted_cents": total, "sales": d.get("total_sales_count", 0), "brand": klass}


def ingest_subscriptions():
    """Non-work subscriptions with a real amount -> Software & Subs expense accrual.
    Returns the vendor list (for procurement registry) regardless of amount."""
    d = _load("subscriptions.json")
    subs = (d or {}).get("subscriptions", [])
    vendors, posted = [], 0
    for s in subs:
        if _is_work(s):
            continue  # firewall: skip Outlook/work-scraped
        amt = L.to_cents(s.get("amount"))
        vendors.append({"service": s.get("service"), "amount_cents": amt,
                        "frequency": s.get("frequency"),
                        "next": s.get("next_charge_date"), "id": s.get("id")})
        if amt > 0:
            L.post(L.HUSTLE, [("5100", amt, "ops"), ("2100", -amt, "ops")],
                   memo=f"subscription accrual: {s.get('service')}",
                   source="subscriptions", ext_id=str(s.get("id")))
            posted += amt
    return {"vendors": vendors, "accrued_cents": posted, "kept": len(vendors)}


def ingest_bills():
    """Non-work bills with a real amount -> Accounts Payable."""
    d = _load("bills.json")
    bills = (d or {}).get("bills", [])
    ap, posted = [], 0
    for b in bills:
        if _is_work(b):
            continue
        amt = L.to_cents(b.get("amount"))
        if amt <= 0:
            continue
        ap.append({"service": b.get("service"), "amount_cents": amt,
                   "due": b.get("due_date"), "id": b.get("id")})
        L.post(L.HUSTLE, [("5900", amt, "ops"), ("2000", -amt, "ops")],
               memo=f"bill: {b.get('service')}", date=b.get("due_date") or None,
               source="bills", ext_id=str(b.get("id")))
        posted += amt
    return {"ap": ap, "posted_cents": posted}


def ingest_treasury():
    """Graceful: sync brokerage/treasury cash from an optional balances file.
    Activates the moment `data/backoffice/treasury_balances.json`
    ({"cash_usd": <float>}) exists — no fabricated numbers until then."""
    d = _load("backoffice/treasury_balances.json")
    if not d or "cash_usd" not in d:
        return {"active": False, "note": "no treasury_balances.json feed yet"}
    target = L.to_cents(d["cash_usd"])
    _resync(L.HUSTLE, "1010", "3000", target, "treasury",
            "treasury/brokerage cash", klass="treasury")
    return {"active": True, "cash_cents": target}


def ingest_household():
    """Graceful: HOUSEHOLD book expense/income from an optional feed file.
    Activates when `data/backoffice/household_ledger.json`
    ({"entries":[{"account","amount_usd","memo","date","ext"}]}) exists."""
    d = _load("backoffice/household_ledger.json")
    entries = (d or {}).get("entries", [])
    if not entries:
        return {"active": False, "note": "no household_ledger.json feed yet"}
    posted = 0
    for e in entries:
        acct = e.get("account", "6000")
        amt = L.to_cents(e.get("amount_usd"))
        if amt <= 0:
            continue
        L.post(L.HOUSEHOLD, [(acct, amt, "household"), ("1000", -amt, "household")],
               memo=e.get("memo", "household"), date=e.get("date"),
               source="household", ext_id=str(e.get("ext") or e.get("memo")))
        posted += amt
    return {"active": True, "posted_cents": posted, "n": len(entries)}


def run_all():
    L.init()
    out = {
        "ai_cost": ingest_ai_cost(),
        "revenue": ingest_revenue(),
        "subscriptions": ingest_subscriptions(),
        "bills": ingest_bills(),
        "treasury": ingest_treasury(),
        "household": ingest_household(),
        "trial_balance_ok": L.trial_balance_ok(L.HUSTLE) and L.trial_balance_ok(L.HOUSEHOLD),
    }
    return out


if __name__ == "__main__":
    print(json.dumps(run_all(), indent=2, default=str))
