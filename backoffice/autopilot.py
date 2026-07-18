"""autopilot.py — the back office running itself.

One idempotent tick fires every seat that's due by its own cadence, self-heals
on integrity drift, and locks each month on rollover. Armed as a launchd tick
(every ~15m); it decides what's due from its own state file, so it "loops
everything on its own" without a fragile long-lived process.

AUTONOMY BOUNDARY (HARD): this moves NO money and sends nothing. It only keeps
books — ingest, reconcile, close, compliance, digest. Anything that pays a bill,
pays a contractor, or sends an invoice stays a human-gated action in cli.py.
On integrity drift it QUARANTINES (alerts + flags), it never "fixes" by mutating
the ledger blind.
"""
from __future__ import annotations
import os, sys, json, datetime as _dt
from . import ledger as L, ingest, reports as R, admin, controller, org

STATE = os.path.expanduser("~/claude-stack/data/backoffice/autopilot_state.json")
ALERT = os.path.expanduser("~/claude-stack/data/backoffice/INTEGRITY_ALERT.txt")

# seat -> cadence: 'tick' every run, 'daily', 'monthly'
CADENCE = {
    "bookkeeper": "tick", "procurement": "tick", "digest": "tick",
    "compliance": "daily", "reconcile": "daily", "period_lock": "monthly",
}


def _load():
    try:
        return json.load(open(STATE))
    except Exception:
        return {}


def _save(s):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    json.dump(s, open(STATE, "w"), indent=2, default=str)


def _due(seat, state, now):
    cad = CADENCE[seat]
    last = state.get(seat)
    if cad == "tick" or not last:
        return True
    last_dt = _dt.datetime.fromisoformat(last)
    if cad == "daily":
        return last_dt.date() < now.date()
    if cad == "monthly":
        return (last_dt.year, last_dt.month) < (now.year, now.month)
    return True


def tick():
    L.init(); admin.init()
    now = _dt.datetime.now()
    state = _load()
    did = []

    # bookkeeper — pull real finance data into the GL (firewall-clean, idempotent)
    if _due("bookkeeper", state, now):
        ing = ingest.run_all()
        for v in ing["subscriptions"]["vendors"]:
            admin.upsert_vendor(v["service"] or "unknown", amount_cents=v["amount_cents"],
                                frequency=v.get("frequency") or "", next_charge=v.get("next"))
        state["bookkeeper"] = now.isoformat(); did.append("ingest")

    # compliance — recompute federal due dates
    if _due("compliance", state, now):
        admin.refresh_compliance_dates()
        state["compliance"] = now.isoformat(); did.append("compliance")

    # reconcile — flag cash variance vs external truth (no auto-fix)
    if _due("reconcile", state, now):
        rec = admin.reconcile_cash()
        state["reconcile"] = now.isoformat()
        did.append(f"reconcile(var={L.dollars(rec['variance_cents'])})")

    # period lock — archive last month's close on rollover
    if _due("period_lock", state, now):
        prev = (now.replace(day=1) - _dt.timedelta(days=1)).strftime("%Y-%m")
        for book in L.BOOKS:
            R.close_period(book, prev)
        state["period_lock"] = now.isoformat(); did.append(f"locked {prev}")

    # integrity self-heal = QUARANTINE, never blind-fix
    ok = (L.trial_balance_ok(L.HUSTLE) and L.trial_balance_ok(L.HOUSEHOLD)
          and R.balance_sheet(L.HUSTLE)["balanced"])
    if not ok:
        msg = (f"[{now.isoformat()}] INTEGRITY DRIFT — HUSTLE tb0="
               f"{L.trial_balance_ok(L.HUSTLE)} HOUSEHOLD tb0={L.trial_balance_ok(L.HOUSEHOLD)} "
               f"BS_balanced={R.balance_sheet(L.HUSTLE)['balanced']}. Books quarantined; "
               f"no autonomous ledger mutation performed. Human review required.\n")
        open(ALERT, "a").write(msg)
        did.append("QUARANTINE")
    elif os.path.exists(ALERT):
        os.remove(ALERT)  # cleared once healthy again

    # digest — always refresh the team heartbeat + dashboard
    controller.run()
    state["digest"] = now.isoformat()
    state["_last_tick"] = now.isoformat()
    state["_last_actions"] = did
    state["_healthy"] = ok
    _save(state)
    return {"tick": now.isoformat(), "did": did, "healthy": ok}


if __name__ == "__main__":
    r = tick()
    if "--print" in sys.argv:
        print(org.render())
        print("\nautopilot:", json.dumps(r, default=str))
    print(f"autopilot tick — did {r['did']} — healthy={r['healthy']}")
