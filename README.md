# hailports

Open-source pieces of an autonomous operations stack — from the team behind [hailports.com](https://hailports.com).

This repo is the **self-running back office**: a real double-entry accounting core plus the general-and-administrative (G&A) processes built on top of it, designed to run unattended and prove its own integrity.

## What's here

### `backoffice/` — a bookkeeping department that runs itself
- **`ledger.py`** — double-entry general ledger. Money is stored as integer cents, every journal entry must sum to zero, and ingest is idempotent on `(source, id)` so re-runs never double-post. Two isolated books (`HUSTLE`, `HOUSEHOLD`); a per-brand class dimension gives consolidated *and* per-brand P&L from one book.
- **`ingest.py`** — pulls real financial data (spend, revenue, subscriptions, bills) into the ledger. Fail-closed source firewall: anything from an excluded lane is dropped, never booked.
- **`reports.py`** — P&L, balance sheet, cash flow, and monthly close, per book and per brand.
- **`admin.py`** — the registries: A/R invoicing, A/P, 1099 contractor payroll, vendor/procurement, and a compliance calendar with real federal deadlines computed from statute.
- **`processes.py`** — 16 G&A processes (financial statements, budget-vs-actual, cash-flow forecast, runway, PO/vendor onboarding, headcount, 1099 prep, contract renewals, records retention, KPI pack, management report). Each returns a real artifact or *honestly* holds for a required input — it never fabricates a number to look finished.
- **`controller.py`** / **`autopilot.py`** — the daily loop and the autonomous self-scheduler. Each seat runs on its own cadence; on integrity drift the autopilot **quarantines** (alerts, refuses to mutate the ledger) rather than guessing.
- **`org.py`** — a live 9-seat org chart (Controller/CFO, Bookkeeper, A/P, A/R, Payroll, Procurement, Compliance, Treasury, Reconciliation) with per-seat health.
- **`test_backoffice.py`** — 13 invariants (balanced entries, book isolation, idempotency, full A/R–A/P–payroll cycle) that must never regress.
- **`simulate.py`** — drives a synthetic company through 18 transactions and every process on a throwaway database to prove the whole thing runs unassisted with the books balanced end to end.

### `tools/`
- **`jobs_inventory.py`** — inventories scheduled background jobs (launchd), grouped by function, flagging what's live vs. dormant.

## Design principles

- **Correctness compounds** — the ledger is the one place a silent bug is unacceptable, so it's integer-only, balanced-by-construction, and invariant-tested.
- **Idempotent everywhere** — every ingest and post is safe to re-run.
- **Fail-closed** — firewalls drop on doubt; the autopilot quarantines on drift.
- **Human-gated on the irreversible** — the machine keeps the books; anything that moves money or sends stays a human decision.
- **Never fabricate** — a process that lacks an input says so; it doesn't invent a number.

## Running it

```bash
python3 -m backoffice.simulate        # prove it end-to-end on synthetic data
python3 -m backoffice.test_backoffice # run the invariants
python3 -m backoffice.cli report      # P&L + balance sheet
```

## License

MIT — see [LICENSE](LICENSE).
