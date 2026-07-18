"""org.py — the back-office org chart: each admin seat, what it owns, its
cadence, and a live status line derived from the ledger. This is what makes the
modules read as a *team* rather than a pile of functions.
"""
from __future__ import annotations
from . import ledger as L, admin, reports as R, ingest


def roster():
    close = R.monthly_close(L.HUSTLE)
    ap = admin.open_ap()["accounts_payable_cents"]
    ar = admin.open_ar()
    comp = admin.compliance()
    next_comp = next((c for c in comp if c["due"]), None)
    treasury = ingest.ingest_treasury()
    recon = admin.reconcile_cash()
    seats = [
        ("Controller / CFO", "controller.run", "daily",
         f"net {L.dollars(close['net_income'])} · books close-ready", close["trial_balance_ok"]),
        ("Bookkeeper", "ingest.run_all", "daily",
         f"trial-balance {'0 ✓' if close['trial_balance_ok'] else 'OFF'}", close["trial_balance_ok"]),
        ("AP clerk", "admin.pay_bill", "on-bill",
         f"payables outstanding {L.dollars(ap)}", ap >= 0),
        ("AR clerk", "admin.collect_invoice", "on-invoice",
         f"{len(ar)} open invoice(s)", True),
        ("Payroll / HR", "admin.pay_contractor", "pay-run",
         f"{len(admin.contractors())} contractor(s) on file", True),
        ("Procurement", "admin.vendors", "monthly",
         f"vendor run-rate {L.dollars(admin.monthly_vendor_spend())}/mo", True),
        ("Compliance officer", "admin.compliance", "calendar",
         f"next: {next_comp['item']+' '+next_comp['due'] if next_comp else 'confirm entity/state'}", True),
        ("Treasury", "ingest.ingest_treasury", "daily",
         "live feed" if treasury["active"] else "dark — awaiting balance feed", True),
        ("Reconciliation", "admin.reconcile_cash", "daily",
         f"cash variance {L.dollars(recon['variance_cents'])}" +
         ("" if recon["reconciled"] else " (no external truth yet)"), True),
    ]
    return [{"seat": s, "fn": f, "cadence": c, "status": st, "healthy": h}
            for s, f, c, st, h in seats]


def render():
    out = ["## Admin team (live)"]
    for r in roster():
        mark = "🟢" if r["healthy"] else "🔴"
        out.append(f"- {mark} **{r['seat']}** ({r['cadence']}) — {r['status']}")
    return "\n".join(out)


if __name__ == "__main__":
    print(render())
