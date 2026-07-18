"""test_backoffice.py — invariants that must never regress. Plain asserts, no
pytest needed. Runs against a throwaway DB so the real ledger is untouched.

    python3 -m apps.backoffice.test_backoffice
"""
import os, tempfile
from . import ledger as L


def run():
    # isolate: point the whole package at a temp DB
    tmp = tempfile.mkdtemp()
    L.DB_PATH = os.path.join(tmp, "t.db")
    from . import admin, reports as R, ingest
    L.init(); admin.init()
    n = 0

    def ok(cond, msg):
        nonlocal n
        assert cond, "FAIL: " + msg
        n += 1

    # 1. unbalanced entry rejected
    try:
        L.post(L.HUSTLE, [("1000", 100, "g")]); ok(False, "unbalanced accepted")
    except ValueError:
        ok(True, "unbalanced rejected")
    # 2. unknown account rejected
    try:
        L.post(L.HUSTLE, [("9999", 100, "g"), ("1000", -100, "g")]); ok(False, "bad acct accepted")
    except ValueError:
        ok(True, "unknown account rejected")
    # 3. balanced entry posts + trial balance stays 0
    L.post(L.HUSTLE, [("5000", 500, "ops"), ("1000", -500, "ops")], memo="t")
    ok(L.trial_balance_ok(L.HUSTLE), "hustle trial balance 0")
    # 4. book isolation — household unaffected
    ok(L.balance(L.HOUSEHOLD) == 0 and R.pnl(L.HOUSEHOLD)["total_expense"] == 0, "book isolation")
    # 5. idempotent ingest on (source, ext_id)
    j1 = L.post(L.HUSTLE, [("5100", 200, "ops"), ("2100", -200, "ops")], source="s", ext_id="x")
    j2 = L.post(L.HUSTLE, [("5100", 200, "ops"), ("2100", -200, "ops")], source="s", ext_id="x")
    ok(j1 and j2 is None, "idempotent ext_id")
    # 6. AR lifecycle: invoice -> AR up, collect -> AR clears to cash
    iid = admin.issue_invoice("C", 1500, brand="acme", ext="inv1")
    ok(admin.issue_invoice("C", 1500, brand="acme", ext="inv1") is None, "invoice idempotent")
    ar_before = L.balance(L.HUSTLE, account="1200")
    admin.collect_invoice(iid)
    ok(L.balance(L.HUSTLE, account="1200") == ar_before - 1500, "collect clears AR")
    ok(not admin.open_ar(), "no open AR after collect")
    # 7. AP lifecycle: create payable then pay it down
    L.post(L.HUSTLE, [("5900", 999, "ops"), ("2000", -999, "ops")], memo="bill", source="b", ext_id="b1")
    ap0 = admin.open_ap()["accounts_payable_cents"]
    admin.pay_bill(999, "pay")
    ok(admin.open_ap()["accounts_payable_cents"] == ap0 - 999, "pay_bill clears AP")
    # 8. payroll + 1099 YTD
    admin.pay_contractor("Jane", 80000)
    ok(admin.contractor_ytd("Jane") == 80000, "contractor YTD tracked")
    # 9. firewall — work-sourced record excluded from ingest
    ok(ingest._is_work({"source": "outlook"}) and not ingest._is_work({"source": "gumroad"}),
       "outlook/work firewall")
    # 10. balance sheet balances after all activity
    ok(R.balance_sheet(L.HUSTLE)["balanced"], "balance sheet balances")
    ok(L.trial_balance_ok(L.HUSTLE), "final trial balance 0")

    print(f"OK — {n} invariants passed")


if __name__ == "__main__":
    run()
