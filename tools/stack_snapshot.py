#!/usr/bin/env python3
"""Daily snapshot RECORDER for the revenue stack.

Computes a flat dict of key daily metrics (per brand + total) and appends ONE
dated row to data/stack_snapshots.jsonl. Idempotent per date: re-running drops
today's existing row and rewrites it.

Reuses existing single-source-of-truth engines (never re-derives):
  - tools.revenue_scoreboard (stripe_block for live $ today)
  - core.savings.compute()    (api_calls / cost / savings)
  - ~/.llm-router.db          (token counts)
  - launchctl list            (automation job count + running)
  - dated JSONL ledgers       (sends / replies / leads backfill)

    PYTHONPATH=. .venv/bin/python tools/stack_snapshot.py            # seed today
    PYTHONPATH=. .venv/bin/python tools/stack_snapshot.py --backfill # + history
    PYTHONPATH=. .venv/bin/python tools/stack_snapshot.py --json     # print row
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
HOME = Path.home()
sys.path.insert(0, str(BASE))

OUT = BASE / "data" / "stack_snapshots.jsonl"
HUSTLE = BASE / "data" / "hustle"
TODAY = datetime.now().strftime("%Y-%m-%d")

# sent ledger -> default brand (None = read per-record pipeline/brand/segment field)
SENT_LEDGERS = [
    ("broken_site_sent.jsonl", "scannerapp"),
    ("email_auth_outreach_queue_sent.jsonl", "scannerapp"),
    ("leadlist_buyer_sent.jsonl", "leadlist"),
    ("outreach_sent.jsonl", None),
    ("cold_sent_ledger.jsonl", None),
    ("outreach_followup_sent.jsonl", None),
]

# BrandA STANDARD — the NAMED lane (Operator2 + Operator), kept fully SEPARATE from the faceless
# house of brands. Its own ledger + leads + on/off flag; never folded into faceless totals.
MAROON_SENT = BASE / "products" / "outreach" / "sent.jsonl"
MAROON_OFF_FLAG = HUSTLE / "MAROON_OFF"
MAROON_LEADS = HUSTLE / "maroon_aggie_leads.jsonl"


def maroon_dated() -> dict[str, int]:
    out: dict[str, int] = defaultdict(int)
    for r in _iter_jsonl(MAROON_SENT):
        d = _rec_date(r)
        if d:
            out[d] += 1
    return dict(out)


def maroon_block(date: str, mdated: dict[str, int], live: bool) -> dict:
    sends = mdated.get(date, 0)
    flag_off = MAROON_OFF_FLAG.exists()
    # Status reflects REALITY (actual sends), not just the flag — because the MAROON_OFF
    # flag is NOT honored by maroon_sender (it kept sending after the flag was set), so a
    # flag-only status would falsely report "paused" while it's live.
    if sends > 0:
        status = "sending"
    elif flag_off:
        status = "paused"
    else:
        status = "idle"
    blk = {"sends": sends, "status": status, "off_flag_present": flag_off,
           "flag_honored": not (flag_off and sends > 0)}
    if live:
        blk["leads"] = sum(1 for _ in _iter_jsonl(MAROON_LEADS))
    return blk


def _iter_jsonl(path: Path):
    if not path.exists():
        return
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue
    except OSError:
        return


def _rec_date(r: dict) -> str:
    for k in ("date", "ts", "sent_at", "found_at", "posted_at"):
        v = r.get(k)
        if v:
            return str(v)[:10]
    return ""


def _rec_brand(r: dict, default: str | None) -> str:
    return (r.get("pipeline") or r.get("brand") or default
            or (r.get("segment") or "").split("_")[0] or "other")


# ── dated aggregates over ledgers (drives backfill AND today's send/reply/lead) ──
def dated_aggregates() -> dict[str, dict]:
    """date -> {sends_total, sends_by_brand{}, replies_total, leads_total}."""
    agg: dict[str, dict] = defaultdict(
        lambda: {"sends_total": 0, "sends_by_brand": defaultdict(int),
                 "replies_total": 0, "leads_total": 0})

    for fname, default_brand in SENT_LEDGERS:
        for r in _iter_jsonl(HUSTLE / fname):
            d = _rec_date(r)
            if not d:
                continue
            agg[d]["sends_total"] += 1
            agg[d]["sends_by_brand"][_rec_brand(r, default_brand)] += 1

    # intent replies posted (per-date daily counter)
    try:
        posted = json.loads((HUSTLE / "response_posted.json").read_text())
        for d, n in (posted.get("daily") or {}).items():
            agg[d]["replies_total"] += int(n or 0)
    except (OSError, json.JSONDecodeError):
        pass
    # email replies answered (per-date daily counter)
    try:
        rr = json.loads((HUSTLE / "reply_responder_state.json").read_text())
        for d, n in (rr.get("daily") or {}).items():
            agg[d]["replies_total"] += int(n or 0)
    except (OSError, json.JSONDecodeError):
        pass

    # leads by discovery date
    try:
        mm = json.loads((HUSTLE / "multi_market_leads.json").read_text())
        for l in mm.get("leads", []):
            d = str(l.get("found_at") or "")[:10]
            if d:
                agg[d]["leads_total"] += 1
    except (OSError, json.JSONDecodeError):
        pass

    # normalize defaultdicts
    out = {}
    for d, v in agg.items():
        out[d] = {"sends_total": v["sends_total"],
                  "sends_by_brand": dict(v["sends_by_brand"]),
                  "replies_total": v["replies_total"],
                  "leads_total": v["leads_total"]}
    return out


# ── today-only live signals (not historically reconstructable) ──
def llm_block() -> dict:
    out = {"api_calls": None, "tokens": None, "est_cost_usd": None,
           "est_savings_usd": None, "net_saved_alltime": None}
    try:
        from core import savings as sv
        s = sv.compute()
        today, alltime = s.get("today", {}), s.get("alltime", {})
        out["api_calls"] = today.get("total_calls")
        out["est_cost_usd"] = today.get("api_cost")
        out["est_savings_usd"] = today.get("net_saved")
        out["net_saved_alltime"] = alltime.get("net_saved")
    except Exception:
        pass
    # token counts from the local LLM router db (repo copy may be empty -> home)
    tok = 0
    for dbp in (BASE / ".llm-router.db", HOME / ".llm-router.db"):
        if dbp.exists() and dbp.stat().st_size > 0:
            try:
                con = sqlite3.connect(f"file:{dbp}?mode=ro", uri=True)
                row = con.execute(
                    "SELECT COALESCE(SUM(input_tokens+output_tokens),0) "
                    "FROM savings_ledger WHERE substr(ts,1,10)=?", (TODAY,)).fetchone()
                tok += int(row[0] or 0)
                con.close()
            except sqlite3.Error:
                pass
    # also count tokens logged in the paid cost ledger today
    for r in _iter_jsonl(BASE / "data" / "logs" / "cost.jsonl"):
        if str(r.get("date") or r.get("ts", ""))[:10] == TODAY:
            tok += int(r.get("input", 0) or 0) + int(r.get("output", 0) or 0)
    out["tokens"] = tok
    return out


def stripe_today() -> dict:
    try:
        from tools.revenue_scoreboard import stripe_block
        s = stripe_block()
        return {"revenue_usd": s.get("revenue_usd_24h", 0) or 0,
                "paid_orders": s.get("sessions_completed_24h", 0) or 0}
    except Exception:
        return {"revenue_usd": 0, "paid_orders": 0}


def automation_jobs() -> dict:
    try:
        # full path: launchd's PATH doesn't include /bin, so bare "launchctl" threw -> None -> dashboard showed 0
        out = subprocess.run(["/bin/launchctl", "list"], capture_output=True,
                             text=True, timeout=20).stdout
    except Exception:
        return {"automation_jobs": None, "automation_jobs_running": None}
    total = running = 0
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 3 or "claude-stack" not in parts[2]:
            continue
        total += 1
        if parts[0].strip() not in ("-", ""):
            running += 1
    return {"automation_jobs": total, "automation_jobs_running": running}


def build_row(date: str, agg: dict, live: bool, mdated: dict | None = None) -> dict:
    base = agg.get(date, {"sends_total": 0, "sends_by_brand": {},
                          "replies_total": 0, "leads_total": 0})
    row = {
        "BrandA": maroon_block(date, mdated or {}, live),
        "date": date,
        "ts": datetime.now(timezone.utc).isoformat(),
        "sends_total": base["sends_total"],
        "sends_by_brand": base["sends_by_brand"],
        "replies_total": base["replies_total"],
        "leads_total": base["leads_total"],
        "paid_orders": 0,
        "revenue_usd": 0,
        "api_calls": None,
        "tokens": None,
        "est_cost_usd": None,
        "est_savings_usd": None,
        "net_saved_alltime": None,
        "automation_jobs": None,
        "automation_jobs_running": None,
    }
    if live:
        row.update(llm_block())
        row.update(automation_jobs())
        row.update(stripe_today())
    return row


def load_existing() -> dict[str, dict]:
    return {r["date"]: r for r in _iter_jsonl(OUT) if r.get("date")}


def write_rows(rows: dict[str, dict]):
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w") as f:
        for d in sorted(rows):
            f.write(json.dumps(rows[d]) + "\n")


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    backfill = "--backfill" in argv
    agg = dated_aggregates()
    mdated = maroon_dated()
    rows = load_existing()

    if backfill:
        all_dates = set(agg) | set(mdated)
        for d in all_dates:
            if d == TODAY:
                continue
            # don't clobber a richer live row already stored for a past date
            if d not in rows:
                rows[d] = build_row(d, agg, live=False, mdated=mdated)
            else:
                rows[d].update({k: build_row(d, agg, live=False, mdated=mdated)[k]
                                for k in ("sends_total", "sends_by_brand",
                                          "replies_total", "leads_total", "BrandA")})

    rows[TODAY] = build_row(TODAY, agg, live=True, mdated=mdated)  # idempotent overwrite
    write_rows(rows)

    if "--json" in argv:
        print(json.dumps(rows[TODAY], indent=2))
    else:
        backfilled = sum(1 for d in rows if d != TODAY)
        print(f"snapshot seeded {TODAY} -> {OUT}")
        print(f"  total rows: {len(rows)} ({backfilled} historical)")
        r = rows[TODAY]
        print(f"  today: sends={r['sends_total']} replies={r['replies_total']} "
              f"leads={r['leads_total']} ${r['revenue_usd']} "
              f"api_calls={r['api_calls']} tokens={r['tokens']} "
              f"jobs={r['automation_jobs']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
