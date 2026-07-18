"""reports.py — P&L, balance sheet, cash flow, and monthly close per book.

All figures derive from the ledger; nothing is stored. Per-brand views come
from the `klass` dimension on the HUSTLE book (klass=None => consolidated).
"""
from __future__ import annotations
import datetime as _dt
from . import ledger as L


def _month_bounds(ym: str | None):
    if ym:
        y, m = map(int, ym.split("-"))
    else:
        t = _dt.date.today(); y, m = t.year, t.month
    start = _dt.date(y, m, 1)
    end = _dt.date(y + (m == 12), (m % 12) + 1, 1) - _dt.timedelta(days=1)
    return start.isoformat(), end.isoformat()


def pnl(book=L.HUSTLE, *, start=None, end=None, klass=None):
    rev = L.account_balances(book, "revenue", klass=klass, start=start, end=end)
    exp = L.account_balances(book, "expense", klass=klass, start=start, end=end)
    total_rev = sum(a for *_, a in rev)
    total_exp = sum(a for *_, a in exp)
    return {"book": book, "klass": klass or "ALL", "start": start, "end": end,
            "revenue": rev, "expenses": exp,
            "total_revenue": total_rev, "total_expense": total_exp,
            "net_income": total_rev - total_exp}


def balance_sheet(book=L.HUSTLE, *, as_of=None):
    assets = L.account_balances(book, "asset", end=as_of)
    liab = L.account_balances(book, "liability", end=as_of)
    eq = L.account_balances(book, "equity", end=as_of)
    ta = sum(a for *_, a in assets)
    tl = sum(a for *_, a in liab)
    te = sum(a for *_, a in eq)
    # retained earnings = cumulative net income not yet closed to equity
    ni = pnl(book, end=as_of)["net_income"]
    return {"book": book, "as_of": as_of, "assets": assets, "liabilities": liab,
            "equity": eq, "total_assets": ta, "total_liabilities": tl,
            "total_equity": te + ni, "retained_earnings": ni,
            "balanced": ta == (tl + te + ni)}


def cash_flow(book=L.HUSTLE, *, start=None, end=None):
    cash = 0
    for code in ("1000", "1010"):
        cash += L.balance(book, account=code, start=start, end=end)  # debit-normal = +inflow
    return {"book": book, "start": start, "end": end, "net_cash_change": cash}


def per_brand(book=L.HUSTLE, *, start=None, end=None):
    rows = []
    for k in L.classes(book):
        p = pnl(book, start=start, end=end, klass=k)
        rows.append({"brand": k, "revenue": p["total_revenue"],
                     "expense": p["total_expense"], "net": p["net_income"]})
    return sorted(rows, key=lambda r: -r["net"])


def monthly_close(book=L.HUSTLE, ym=None):
    start, end = _month_bounds(ym)
    p = pnl(book, start=start, end=end)
    bs = balance_sheet(book, as_of=end)
    return {"book": book, "period": ym or end[:7], "start": start, "end": end,
            "revenue": p["total_revenue"], "expense": p["total_expense"],
            "net_income": p["net_income"], "cash": cash_flow(book, end=end)["net_cash_change"],
            "balance_sheet_balanced": bs["balanced"],
            "trial_balance_ok": L.trial_balance_ok(book),
            "by_brand": per_brand(book, start=start, end=end)}


def close_period(book=L.HUSTLE, ym=None):
    """Archive a locked snapshot of a month's close. Does NOT post closing
    entries — retained earnings stays derived (balance_sheet adds net income),
    so posting them would double-count. Returns the archived path."""
    import os, json
    c = monthly_close(book, ym)
    d = os.path.expanduser("~/claude-stack/data/backoffice/closes")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f"{book}_{c['period']}.json")
    json.dump(c, open(path, "w"), indent=2, default=str)
    return path


def render_pnl_md(book=L.HUSTLE, ym=None):
    start, end = _month_bounds(ym)
    p = pnl(book, start=start, end=end)
    out = [f"### P&L — {book} — {ym or end[:7]}"]
    out.append("**Revenue**")
    for c, n, a in p["revenue"]:
        if a:
            out.append(f"- {n}: {L.dollars(a)}")
    out.append(f"- _total revenue_: **{L.dollars(p['total_revenue'])}**")
    out.append("**Expenses**")
    for c, n, a in p["expenses"]:
        if a:
            out.append(f"- {n}: {L.dollars(a)}")
    out.append(f"- _total expense_: **{L.dollars(p['total_expense'])}**")
    out.append(f"\n**Net income: {L.dollars(p['net_income'])}**")
    return "\n".join(out)


if __name__ == "__main__":
    import json
    print(json.dumps(monthly_close(), indent=2, default=str))
