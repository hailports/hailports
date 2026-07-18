"""ledger.py — double-entry general ledger, the spine of the back office.

Money is stored as signed integer minor units (cents). Debits are positive,
credits negative; every journal entry's lines MUST sum to zero. Balances are
derived, never stored. One HUSTLE book with a per-brand `class` dimension
(consolidated = all classes) and a separate HOUSEHOLD book. Work is never a
book here — the work lane is firewalled out by design.

Correctness rules that must never regress:
  - entries are balanced (sum of line amounts == 0) or they are rejected
  - ingest is idempotent on (source, ext_id) so re-runs never double-post
  - amounts are ints; no float ever touches a stored value
"""
from __future__ import annotations
import os, sqlite3, datetime as _dt
from contextlib import contextmanager

DB_PATH = os.path.expanduser("~/claude-stack/data/backoffice/ledger.db")

# books (entities). Work deliberately absent — HARD firewall.
HUSTLE = "HUSTLE"
HOUSEHOLD = "HOUSEHOLD"
BOOKS = (HUSTLE, HOUSEHOLD)

# account types and their normal balance (debit-normal => +1, credit-normal => -1)
TYPES = {"asset": 1, "expense": 1, "liability": -1, "equity": -1, "revenue": -1}

# seed chart of accounts: code, name, type
CHART = [
    # assets
    ("1000", "Cash - Operating", "asset"),
    ("1010", "Cash - Treasury/Brokerage", "asset"),
    ("1200", "Accounts Receivable", "asset"),
    ("1500", "Equipment (net)", "asset"),
    # liabilities
    ("2000", "Accounts Payable", "liability"),
    ("2100", "Accrued Subscriptions", "liability"),
    ("2200", "Taxes Payable", "liability"),
    # equity
    ("3000", "Owner Equity", "equity"),
    ("3900", "Retained Earnings", "equity"),
    # revenue
    ("4000", "Product Revenue", "revenue"),
    ("4100", "Service Revenue", "revenue"),
    ("4900", "Other Income", "revenue"),
    # expenses
    ("5000", "COGS - AI/API", "expense"),
    ("5100", "Software & Subscriptions", "expense"),
    ("5200", "Infrastructure & Hosting", "expense"),
    ("5300", "Marketing & Ads", "expense"),
    ("5400", "Contractor / 1099", "expense"),
    ("5500", "Fees (Stripe/bank)", "expense"),
    ("5900", "Other Operating Expense", "expense"),
    # household
    ("6000", "Household - Living", "expense"),
    ("6100", "Household - Bills/Utilities", "expense"),
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts(
  code TEXT PRIMARY KEY, name TEXT NOT NULL, type TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS journal(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  date TEXT NOT NULL, book TEXT NOT NULL, memo TEXT,
  source TEXT, ext_id TEXT, created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS lines(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  journal_id INTEGER NOT NULL REFERENCES journal(id) ON DELETE CASCADE,
  account TEXT NOT NULL REFERENCES accounts(code),
  klass TEXT NOT NULL DEFAULT 'general',
  amount INTEGER NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS ux_journal_src ON journal(source, ext_id)
  WHERE source IS NOT NULL AND ext_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_lines_acct ON lines(account);
CREATE INDEX IF NOT EXISTS ix_journal_book_date ON journal(book, date);
"""


def _today() -> str:
    return _dt.date.today().isoformat()


@contextmanager
def conn():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init():
    with conn() as c:
        c.executescript(SCHEMA)
        for code, name, typ in CHART:
            c.execute("INSERT OR IGNORE INTO accounts(code,name,type) VALUES(?,?,?)",
                      (code, name, typ))
    return DB_PATH


def dollars(cents: int) -> str:
    return f"${cents/100:,.2f}"


def to_cents(x) -> int:
    """Parse a money-ish value to integer cents. Never trusts a float store."""
    if x is None or x == "":
        return 0
    if isinstance(x, int):
        return x
    s = str(x).replace("$", "").replace(",", "").strip()
    try:
        return int(round(float(s) * 100))
    except ValueError:
        return 0


def post(book: str, lines: list[tuple], *, date=None, memo="", source=None, ext_id=None):
    """Post a balanced journal entry.

    lines: [(account_code, amount_cents, klass), ...]  debit +, credit -.
    Returns journal id, or None if this (source, ext_id) was already posted
    (idempotent ingest). Raises ValueError if unbalanced or account unknown.
    """
    assert book in BOOKS, f"unknown book {book!r} (Work is not a book)"
    norm = []
    total = 0
    for ln in lines:
        acct, amt = ln[0], int(ln[1])
        klass = ln[2] if len(ln) > 2 else "general"
        norm.append((acct, amt, klass))
        total += amt
    if total != 0:
        raise ValueError(f"unbalanced entry: lines sum to {total}, must be 0")
    with conn() as c:
        for acct, _, _ in norm:
            if not c.execute("SELECT 1 FROM accounts WHERE code=?", (acct,)).fetchone():
                raise ValueError(f"unknown account {acct!r}")
        if source and ext_id:
            dup = c.execute("SELECT id FROM journal WHERE source=? AND ext_id=?",
                            (source, ext_id)).fetchone()
            if dup:
                return None
        cur = c.execute(
            "INSERT INTO journal(date,book,memo,source,ext_id,created) VALUES(?,?,?,?,?,?)",
            (date or _today(), book, memo, source, ext_id, _dt.datetime.now().isoformat()))
        jid = cur.lastrowid
        for acct, amt, klass in norm:
            c.execute("INSERT INTO lines(journal_id,account,klass,amount) VALUES(?,?,?,?)",
                      (jid, acct, klass, amt))
        return jid


def balance(book: str, *, account=None, klass=None, start=None, end=None,
            acct_type=None) -> int:
    """Signed cents balance (raw debit-positive). Filter by any dimension."""
    q = ("SELECT COALESCE(SUM(l.amount),0) FROM lines l JOIN journal j ON j.id=l.journal_id "
         "WHERE j.book=?")
    p = [book]
    if account:
        q += " AND l.account=?"; p.append(account)
    if acct_type:
        q += " AND l.account IN (SELECT code FROM accounts WHERE type=?)"; p.append(acct_type)
    if klass:
        q += " AND l.klass=?"; p.append(klass)
    if start:
        q += " AND j.date>=?"; p.append(start)
    if end:
        q += " AND j.date<=?"; p.append(end)
    with conn() as c:
        return int(c.execute(q, p).fetchone()[0])


def account_balances(book: str, acct_type: str, *, klass=None, start=None, end=None):
    """[(code, name, natural_balance_cents)] for a type, normal-sign applied.

    Book isolation is strict: only lines whose journal.book == book count."""
    sign = TYPES[acct_type]
    q = ("SELECT l.account acc, SUM(l.amount) amt FROM lines l "
         "JOIN journal j ON j.id=l.journal_id "
         "WHERE j.book=? AND l.account IN (SELECT code FROM accounts WHERE type=?)")
    p = [book, acct_type]
    if klass:
        q += " AND l.klass=?"; p.append(klass)
    if start:
        q += " AND j.date>=?"; p.append(start)
    if end:
        q += " AND j.date<=?"; p.append(end)
    q += " GROUP BY l.account"
    with conn() as c:
        sums = {r["acc"]: int(r["amt"]) for r in c.execute(q, p)}
        chart = c.execute("SELECT code,name FROM accounts WHERE type=? ORDER BY code",
                          (acct_type,)).fetchall()
    return [(r["code"], r["name"], sums.get(r["code"], 0) * sign) for r in chart]


def classes(book: str = HUSTLE):
    with conn() as c:
        return [r[0] for r in c.execute(
            "SELECT DISTINCT l.klass FROM lines l JOIN journal j ON j.id=l.journal_id "
            "WHERE j.book=? ORDER BY 1", (book,))]


def trial_balance_ok(book: str) -> bool:
    """Every book must sum to zero across all lines — the integrity invariant."""
    return balance(book) == 0


if __name__ == "__main__":
    print("initialized", init())
    for b in BOOKS:
        print(b, "trial-balance-zero:", trial_balance_ok(b))
