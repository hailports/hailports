"""controller.py — the daily back-office loop (the CFO/controller seat).

Runs the whole admin org once: ingest real finance data (firewall-clean) ->
sync vendors into procurement -> monthly close -> write a digest + JSON
dashboard the rest of the stack can read. Idempotent; safe to run on a timer.

    python3 -m apps.backoffice.controller          # run + write digest
    python3 -m apps.backoffice.controller --print   # also print to stdout
"""
from __future__ import annotations
import os, sys, json, datetime as _dt
from . import ledger as L, ingest, reports, admin, org

OUT_MD = os.path.expanduser("~/claude-stack/data/backoffice/BACKOFFICE_DIGEST.md")
OUT_JSON = os.path.expanduser("~/claude-stack/data/backoffice/dashboard.json")


def run():
    L.init(); admin.init()
    ing = ingest.run_all()
    # procurement: fold non-work subscriptions into the vendor registry
    for v in ing["subscriptions"]["vendors"]:
        admin.upsert_vendor(v["service"] or "unknown", amount_cents=v["amount_cents"],
                            frequency=v.get("frequency") or "", next_charge=v.get("next"))
    close = reports.monthly_close(L.HUSTLE)
    hh_close = reports.monthly_close(L.HOUSEHOLD)
    dash = {
        "generated": _dt.datetime.now().isoformat(),
        "hustle": close,
        "household": hh_close,
        "accounts_payable": admin.open_ap(),
        "accounts_receivable_open": admin.open_ar(),
        "vendor_monthly_spend_cents": admin.monthly_vendor_spend(),
        "contractors": admin.contractors(),
        "compliance_open": admin.compliance(),
        "ingest": ing,
        "integrity": {
            "hustle_trial_balance_ok": L.trial_balance_ok(L.HUSTLE),
            "household_trial_balance_ok": L.trial_balance_ok(L.HOUSEHOLD),
            "hustle_bs_balanced": reports.balance_sheet(L.HUSTLE)["balanced"],
        },
    }
    os.makedirs(os.path.dirname(OUT_JSON), exist_ok=True)
    json.dump(dash, open(OUT_JSON, "w"), indent=2, default=str)
    open(OUT_MD, "w").write(_render(dash))
    return dash


def _render(d):
    h = d["hustle"]
    L_ = L
    lines = [f"# Back Office — {h['period']}",
             f"_generated {d['generated'][:19]} · Work firewalled out_", "",
             org.render(), "",
             "## HUSTLE (consolidated)",
             f"- revenue: **{L_.dollars(h['revenue'])}**  ·  expense: **{L_.dollars(h['expense'])}**  ·  "
             f"net: **{L_.dollars(h['net_income'])}**",
             f"- accounts payable: {L_.dollars(d['accounts_payable']['accounts_payable_cents'])}  ·  "
             f"open AR: {len(d['accounts_receivable_open'])} invoices",
             f"- vendor run-rate: {L_.dollars(d['vendor_monthly_spend_cents'])}/mo  ·  "
             f"contractors: {len(d['contractors'])}",
             ""]
    if h["by_brand"]:
        lines.append("### per-brand P&L")
        for b in h["by_brand"]:
            lines.append(f"- {b['brand']}: rev {L_.dollars(b['revenue'])} / exp {L_.dollars(b['expense'])} "
                         f"/ net **{L_.dollars(b['net'])}**")
        lines.append("")
    lines.append("## HOUSEHOLD")
    hh = d["household"]
    lines.append(f"- expense: {L_.dollars(hh['expense'])}  ·  net: {L_.dollars(hh['net_income'])}")
    lines.append("")
    lines.append("## Compliance (federal dates are real; `confirm` = needs entity/state)")
    for c in d["compliance_open"]:
        due = c["due"] or "confirm entity/state"
        lines.append(f"- **{due}** — {c['item']} ({c['cadence']}, {c['jurisdiction']}) [{c['status']}]")
    lines.append("")
    itg = d["integrity"]
    ok = all(itg.values())
    lines.append(f"## Integrity: {'✅ all checks pass' if ok else '❌ REVIEW'}")
    for k, v in itg.items():
        lines.append(f"- {k}: {'✅' if v else '❌'}")
    return "\n".join(lines)


if __name__ == "__main__":
    d = run()
    if "--print" in sys.argv:
        print(open(OUT_MD).read())
    print(f"\nwrote {OUT_MD}\nwrote {OUT_JSON}")
