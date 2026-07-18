"""cli.py — operate the back office by hand.

    python3 -m apps.backoffice.cli report [--book HUSTLE] [--month YYYY-MM]
    python3 -m apps.backoffice.cli brands
    python3 -m apps.backoffice.cli invoice --customer "X" --amount 1500 --brand acme
    python3 -m apps.backoffice.cli collect --id 3
    python3 -m apps.backoffice.cli pay-bill --amount 49.99 --memo "domain renewal"
    python3 -m apps.backoffice.cli pay-contractor --name "Jane" --amount 800
    python3 -m apps.backoffice.cli contractor --name "Jane"       # 1099 YTD
    python3 -m apps.backoffice.cli vendor --service "Anthropic" --amount 20 --freq monthly
    python3 -m apps.backoffice.cli reconcile
    python3 -m apps.backoffice.cli close [--month YYYY-MM]
    python3 -m apps.backoffice.cli run            # full daily controller pass
"""
import argparse, json
from . import ledger as L, admin, reports as R, controller


def _c(x):
    return L.to_cents(x)


def main():
    ap = argparse.ArgumentParser(prog="backoffice")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("report"); p.add_argument("--book", default=L.HUSTLE); p.add_argument("--month")
    sub.add_parser("brands"); sub.add_parser("reconcile"); sub.add_parser("run")
    p = sub.add_parser("close"); p.add_argument("--month")
    p = sub.add_parser("invoice"); p.add_argument("--customer", required=True)
    p.add_argument("--amount", required=True); p.add_argument("--brand", default="general"); p.add_argument("--due")
    p = sub.add_parser("collect"); p.add_argument("--id", type=int, required=True)
    p = sub.add_parser("pay-bill"); p.add_argument("--amount", required=True); p.add_argument("--memo", default="bill")
    p = sub.add_parser("pay-contractor"); p.add_argument("--name", required=True); p.add_argument("--amount", required=True)
    p = sub.add_parser("contractor"); p.add_argument("--name", required=True)
    p = sub.add_parser("vendor"); p.add_argument("--service", required=True)
    p.add_argument("--amount", default="0"); p.add_argument("--freq", default="monthly"); p.add_argument("--cat", default="software")
    a = ap.parse_args()
    L.init(); admin.init()

    if a.cmd == "report":
        print(R.render_pnl_md(a.book, a.month))
        bs = R.balance_sheet(a.book)
        print(f"\nassets {L.dollars(bs['total_assets'])} · liab {L.dollars(bs['total_liabilities'])} "
              f"· equity {L.dollars(bs['total_equity'])} · balanced {bs['balanced']}")
    elif a.cmd == "brands":
        for b in R.per_brand():
            print(f"{b['brand']:14s} net {L.dollars(b['net'])}  (rev {L.dollars(b['revenue'])} / exp {L.dollars(b['expense'])})")
    elif a.cmd == "invoice":
        iid = admin.issue_invoice(a.customer, _c(a.amount), brand=a.brand, due=a.due)
        print(f"issued invoice #{iid} · {a.customer} · {L.dollars(_c(a.amount))} · {a.brand}")
    elif a.cmd == "collect":
        r = admin.collect_invoice(a.id); print("collected" if r else "not found/already paid", a.id)
    elif a.cmd == "pay-bill":
        admin.pay_bill(_c(a.amount), a.memo); print(f"paid {L.dollars(_c(a.amount))} — {a.memo}")
    elif a.cmd == "pay-contractor":
        admin.pay_contractor(a.name, _c(a.amount)); print(f"paid {a.name} {L.dollars(_c(a.amount))} · YTD {L.dollars(admin.contractor_ytd(a.name))}")
    elif a.cmd == "contractor":
        print(f"{a.name} 1099 YTD: {L.dollars(admin.contractor_ytd(a.name))}")
    elif a.cmd == "vendor":
        admin.upsert_vendor(a.service, _c(a.amount), a.freq, category=a.cat)
        print(f"vendor {a.service} · {L.dollars(_c(a.amount))}/{a.freq} · run-rate {L.dollars(admin.monthly_vendor_spend())}/mo")
    elif a.cmd == "reconcile":
        print(json.dumps(admin.reconcile_cash(), indent=2))
    elif a.cmd == "close":
        print(json.dumps(R.monthly_close(L.HUSTLE, a.month), indent=2, default=str))
    elif a.cmd == "run":
        controller.run(); print("daily pass complete → data/backoffice/BACKOFFICE_DIGEST.md")


if __name__ == "__main__":
    main()
