"""simulate.py — prove the back office runs a full company's G&A unassisted.

Seeds a synthetic company into a THROWAWAY database (the real ledger is never
touched), drives a fiscal period of real transactions across every domain, then
runs the entire accounting cycle + every G&A process with zero human input and
checks the books still balance. Emits SIMULATION_REPORT.md.

    python3 -m apps.backoffice.simulate
"""
from __future__ import annotations
import os, sys, tempfile, datetime as _dt
from . import ledger as L

REPORT = os.path.expanduser("~/claude-stack/data/backoffice/SIMULATION_REPORT.md")


def run():
    # isolate onto a scratch DB so real books are untouched
    L.DB_PATH = os.path.join(tempfile.mkdtemp(), "sim.db")
    from . import admin, reports as R, processes as P
    L.init(); admin.init(); P.init()
    log = []

    def step(desc, fn):
        fn(); log.append(desc)

    # --- seed a synthetic company across every domain ---
    step("owner funds the company $10,000",
         lambda: L.post(L.HUSTLE, [("1000", 1000000, "general"), ("3000", -1000000, "general")],
                        memo="owner contribution"))
    # revenue: 3 invoices, collect 2 (one stays in AR)
    inv = []
    for cust, amt, brand in [("Acme Co", 250000, "acme"), ("Beta LLC", 150000, "globex"),
                             ("Gamma Inc", 90000, "geo")]:
        step(f"invoice {cust} {L.dollars(amt)} ({brand})",
             (lambda c=cust, a=amt, b=brand: inv.append(admin.issue_invoice(c, a, brand=b))))
    step("collect invoice 1", lambda: admin.collect_invoice(inv[0]))
    step("collect invoice 2", lambda: admin.collect_invoice(inv[1]))
    # procurement + opex
    step("onboard vendor Anthropic + accrue $200/mo",
         lambda: (P.vendor_onboarding("Anthropic API"), admin.upsert_vendor("Anthropic API", 20000, "monthly")))
    step("onboard vendor Hosting + accrue $50/mo",
         lambda: admin.upsert_vendor("Cloud Hosting", 5000, "monthly"))
    step("raise PO for laptop $1,800", lambda: P.create_po("Apple", 180000, memo="dev laptop"))
    # AP: 3 bills, pay 2
    step("bill: domain renewal $60 (create AP)",
         lambda: L.post(L.HUSTLE, [("5100", 6000, "ops"), ("2000", -6000, "ops")], memo="domains", source="bill", ext_id="d1"))
    step("bill: legal $500 (create AP)",
         lambda: L.post(L.HUSTLE, [("5900", 50000, "ops"), ("2000", -50000, "ops")], memo="legal", source="bill", ext_id="l1"))
    step("pay down $60 of AP", lambda: admin.pay_bill(6000, "pay domains"))
    # HR / payroll
    step("onboard contractor Jane (dev)", lambda: P.contractor_onboarding("Jane Dev", "developer"))
    step("onboard contractor Sam (designer)", lambda: P.contractor_onboarding("Sam Design", "designer"))
    step("pay Jane $2,000", lambda: admin.pay_contractor("Jane Dev", 200000))
    step("pay Sam $700", lambda: admin.pay_contractor("Sam Design", 70000))
    # legal + budget
    step("register MSA contract (renews soon)",
         lambda: P.add_contract("Acme Co", "MSA", 250000,
                                renews=(_dt.date.today() + _dt.timedelta(days=30)).isoformat()))
    ym = _dt.date.today().strftime("%Y-%m")
    step("set marketing budget $1,000/mo", lambda: P.set_budget(L.HUSTLE, "5300", ym, 100000))

    # --- run EVERY G&A process unassisted ---
    results = P.run_all(L.HUSTLE)
    done = {k: v for k, v in results.items() if v["status"] == "done"}
    awaiting = {k: v for k, v in results.items() if v["status"] == "awaiting_input"}

    # integrity after the whole simulated period
    integrity = {
        "trial_balance_zero": L.trial_balance_ok(L.HUSTLE),
        "balance_sheet_balanced": R.balance_sheet(L.HUSTLE)["balanced"],
        "contractor_1099_flagged": len(P.form_1099_prep()["filers"]),
        "open_AR_after_cycle": len(admin.open_ar()),
        "AP_outstanding": admin.open_ap()["accounts_payable_cents"],
    }
    close = R.monthly_close(L.HUSTLE)

    # --- report ---
    md = [f"# Back Office — Unassisted G&A Simulation",
          f"_synthetic company · throwaway DB · real books untouched · {_dt.date.today()}_", "",
          f"**{len(log)} transactions driven across finance, AR/AP, procurement, HR/payroll, legal.**", "",
          "## processes run with ZERO human input",
          f"- ✅ **{len(done)}/{len(results)} completed autonomously**",
          f"- ⏸️ {len(awaiting)} correctly held for a required input (not faked):"]
    for k, v in awaiting.items():
        md.append(f"    - `{k}` — needs: {v['needs']}")
    md += ["", "## completed process artifacts"]
    for k, v in done.items():
        md.append(f"- **{k}** — {v['artifact']}")
    md += ["", "## period result",
           f"- revenue {L.dollars(close['revenue'])} · expense {L.dollars(close['expense'])} · "
           f"net {L.dollars(close['net_income'])}",
           f"- per-brand: " + " · ".join(f"{b['brand']} {L.dollars(b['net'])}" for b in close["by_brand"]),
           "", "## integrity after a full simulated period",
           f"- trial balance zero: {'✅' if integrity['trial_balance_zero'] else '❌'}",
           f"- balance sheet balanced: {'✅' if integrity['balance_sheet_balanced'] else '❌'}",
           f"- 1099 contractors flagged: {integrity['contractor_1099_flagged']}",
           f"- open AR after cycle: {integrity['open_AR_after_cycle']} · "
           f"AP outstanding: {L.dollars(integrity['AP_outstanding'])}"]
    verdict = integrity["trial_balance_zero"] and integrity["balance_sheet_balanced"]
    md += ["", f"## verdict: {'✅ books balanced & self-run end-to-end' if verdict else '❌ REVIEW'}"]

    open(REPORT, "w").write("\n".join(md))
    return {"transactions": len(log), "processes_done": len(done),
            "processes_awaiting": len(awaiting), "integrity_ok": verdict}


if __name__ == "__main__":
    r = run()
    print(f"simulation: {r['transactions']} txns · {r['processes_done']} processes autonomous · "
          f"{r['processes_awaiting']} awaiting-input · integrity_ok={r['integrity_ok']}")
    print(f"→ {REPORT}")
