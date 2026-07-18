#!/usr/bin/env python3
"""Spend / savings / ROI / API-call / automation-run tracker — terminal dashboard.

Revives the at-a-glance tracker that "died with MyOS". Read-only over the live
data sources. Never mutates anything, never makes outbound calls, robust to
missing/empty files (degrades to zeros instead of crashing).

Canonical numbers (TODAY + ALL-TIME) come from the existing single-source-of-
truth engine: core.savings.compute(). This module adds:
  - a 7-DAY rollup computed directly over the JSONL ledgers
  - ROI (revenue vs paid spend) from any populated revenue records
  - automation-run health parsed from `launchctl list`

Run:
    PYTHONPATH=. .venv/bin/python tools/spend_dashboard.py
    PYTHONPATH=. .venv/bin/python tools/spend_dashboard.py --json
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
LOG_DIR = BASE_DIR / "data" / "logs"
HUSTLE_DIRS = [BASE_DIR / "data" / "hustle", BASE_DIR / "data.internal" / "hustle"]

# Conservative free-pool baseline (Haiku-class) used only for the 7-day savings
# rollup of free_pool rows that the canonical engine intentionally values at $0.
# We surface free-pool *call counts* but keep free-pool *dollars* at $0 to match
# core.savings (free routes don't displace this stack's paid spend).


# ─────────────────────────────────────────────────────────────────────────────
# helpers
# ─────────────────────────────────────────────────────────────────────────────
def _read_jsonl(path: Path):
    if not path.exists():
        return
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
    except Exception:
        return


def _read_json(path: Path):
    try:
        if path.exists():
            return json.loads(path.read_text())
    except Exception:
        pass
    return None


def _entry_date(entry: dict) -> str:
    d = entry.get("date")
    if d:
        return str(d)[:10]
    ts = entry.get("ts")
    if ts is None:
        return ""
    try:
        if isinstance(ts, (int, float)):
            return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
        return str(ts)[:10]
    except Exception:
        return ""


def _money(v: float) -> str:
    return f"${v:,.2f}" if abs(v) >= 1 else f"${v:.4f}"


def _short_model(model: str) -> str:
    m = (model or "unknown").lower()
    for marker in ("haiku", "sonnet", "opus"):
        if marker in m:
            return marker
    return model or "unknown"


# ─────────────────────────────────────────────────────────────────────────────
# canonical snapshot (today + all-time) via existing engine
# ─────────────────────────────────────────────────────────────────────────────
def canonical_snapshot() -> dict:
    try:
        from core import savings as savings_mod

        return savings_mod.compute()
    except Exception as exc:  # pragma: no cover - defensive
        return {"_error": f"core.savings.compute failed: {exc}"}


# ─────────────────────────────────────────────────────────────────────────────
# 7-day rollup over the raw ledgers (the engine only emits today + all-time)
# ─────────────────────────────────────────────────────────────────────────────
def seven_day_rollup(now: datetime | None = None) -> dict:
    now = now or datetime.now()
    cutoff = (now - timedelta(days=6)).strftime("%Y-%m-%d")  # inclusive 7 days

    paid_spend = 0.0
    paid_calls = 0
    paid_by_model = defaultdict(lambda: {"cost": 0.0, "calls": 0})
    for e in _read_jsonl(LOG_DIR / "cost.jsonl"):
        if _entry_date(e) < cutoff:
            continue
        cost = float(e.get("cost") or 0)
        paid_spend += cost
        paid_calls += 1
        m = _short_model(e.get("model", "unknown"))
        paid_by_model[m]["cost"] += cost
        paid_by_model[m]["calls"] += 1

    local_saved = 0.0
    local_calls = 0
    free_calls = 0
    by_source = defaultdict(lambda: {"saved": 0.0, "calls": 0})
    for e in _read_jsonl(LOG_DIR / "savings.jsonl"):
        if _entry_date(e) < cutoff:
            continue
        tier = str(e.get("tier") or "").strip().lower()
        saved = float(e.get("saved") or e.get("saved_usd") or 0)
        if tier == "free_pool":
            # match canonical engine: count the call, but $0 displaced paid spend
            free_calls += 1
            saved = 0.0
        else:
            local_calls += 1
            local_saved += saved
        src = str(e.get("source") or "unknown").split(":")[0]
        by_source[src]["saved"] += saved
        by_source[src]["calls"] += 1

    hybrid_saved = 0.0
    hybrid_calls = 0
    for e in _read_jsonl(LOG_DIR / "hybrid_savings.jsonl"):
        if _entry_date(e) < cutoff:
            continue
        hybrid_saved += float(e.get("saved") or e.get("saved_usd") or 0)
        hybrid_calls += 1

    avoided = local_saved + hybrid_saved
    return {
        "since": cutoff,
        "paid_spend": round(paid_spend, 4),
        "paid_calls": paid_calls,
        "paid_by_model": {k: {"cost": round(v["cost"], 4), "calls": v["calls"]} for k, v in paid_by_model.items()},
        "local_saved": round(local_saved, 4),
        "hybrid_saved": round(hybrid_saved, 4),
        "avoided_cost": round(avoided, 4),
        "net_saved": round(avoided - paid_spend, 4),
        "local_calls": local_calls,
        "free_calls": free_calls,
        "hybrid_calls": hybrid_calls,
        "total_calls": local_calls + free_calls + hybrid_calls + paid_calls,
        "top_sources": dict(sorted(
            ((k, round(v["saved"], 4)) for k, v in by_source.items()),
            key=lambda kv: -kv[1])[:8]),
    }


# ─────────────────────────────────────────────────────────────────────────────
# revenue / ROI numerator
# ─────────────────────────────────────────────────────────────────────────────
def revenue_summary() -> dict:
    """Best-effort total realized revenue from any populated record.

    Returns {"revenue": float, "source": str, "detail": str}. Zero if no
    populated revenue records exist (the common case right now).
    """
    total = 0.0
    detail = []
    src = "none"

    # Gumroad sales ledger
    for d in HUSTLE_DIRS:
        gum = _read_json(d / "gumroad_sales.json")
        if isinstance(gum, dict):
            sales = gum.get("sales") or []
            for s in sales:
                try:
                    total += float(s.get("price") or s.get("amount") or 0) / (
                        100.0 if (s.get("price_cents") or s.get("in_cents")) else 1.0)
                except Exception:
                    pass
            if sales:
                src = "gumroad"
                detail.append(f"gumroad:{len(sales)} sale(s)")

    # revenue_dashboard total_revenue string like "$0.00"
    for d in HUSTLE_DIRS:
        dash = _read_json(d / "revenue_dashboard.json")
        if isinstance(dash, dict):
            tr = (dash.get("today") or {}).get("total_revenue")
            if isinstance(tr, str) and tr.strip().startswith("$"):
                try:
                    v = float(tr.replace("$", "").replace(",", ""))
                    if v > 0:
                        total += v
                        src = src if src != "none" else "revenue_dashboard"
                        detail.append(f"dashboard_today:{tr}")
                except Exception:
                    pass

    return {"revenue": round(total, 2), "source": src, "detail": ", ".join(detail) or "no realized revenue recorded"}


# ─────────────────────────────────────────────────────────────────────────────
# automation-run health via launchctl
# ─────────────────────────────────────────────────────────────────────────────
JOB_PREFIXES = ("com.claude-stack.", "com.imma.", "com.openclaw.", "ai.openclaw.")


def automation_health() -> dict:
    try:
        out = subprocess.run(
            ["launchctl", "list"], capture_output=True, text=True, timeout=15
        ).stdout
    except Exception as exc:
        return {"_error": f"launchctl failed: {exc}", "jobs": []}

    jobs = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        pid_s, status_s, label = parts[0].strip(), parts[1].strip(), parts[2].strip()
        if not label.startswith(JOB_PREFIXES):
            continue
        running = pid_s not in ("-", "") and pid_s.isdigit()
        try:
            status = int(status_s)
        except ValueError:
            status = None
        jobs.append({"label": label, "pid": pid_s, "running": running, "status": status})

    running = [j for j in jobs if j["running"]]
    # status 0 = last run clean exit (or never run); negative = signaled (often
    # SIGTERM -15 on long-lived daemons, normal); positive = error exit code.
    failing = [j for j in jobs if (j["status"] or 0) > 0 and not j["running"]]
    signaled = [j for j in jobs if (j["status"] or 0) < 0 and not j["running"]]
    clean = [j for j in jobs if j["status"] == 0 and not j["running"]]

    return {
        "total": len(jobs),
        "running": running,
        "clean": clean,
        "signaled": signaled,
        "failing": failing,
    }


# ─────────────────────────────────────────────────────────────────────────────
# render
# ─────────────────────────────────────────────────────────────────────────────
def build_report() -> dict:
    return {
        "as_of": datetime.now().isoformat(timespec="seconds"),
        "canonical": canonical_snapshot(),
        "seven_day": seven_day_rollup(),
        "revenue": revenue_summary(),
        "automation": automation_health(),
    }


def _hr(char="─", n=64):
    return char * n


def render(report: dict) -> str:
    L = []
    can = report["canonical"]
    wk = report["seven_day"]
    rev = report["revenue"]
    auto = report["automation"]

    today = can.get("today", {}) if "_error" not in can else {}
    alltime = can.get("alltime", {}) if "_error" not in can else {}

    L.append("")
    L.append("  SPEND / SAVINGS / ROI TRACKER".ljust(50) + report["as_of"])
    L.append(_hr("═"))

    if "_error" in can:
        L.append(f"  ! canonical engine unavailable: {can['_error']}")

    # TODAY
    L.append("  TODAY")
    L.append(f"    Paid API spend     {_money(today.get('api_cost', 0)):>14}   ({today.get('api_calls', 0)} calls)")
    L.append(f"    Local+free saved   {_money(today.get('local_saved', 0)):>14}   ({today.get('local_calls', 0)} calls)")
    L.append(f"    Hybrid saved       {_money(today.get('hybrid_saved', 0)):>14}   ({today.get('hybrid_calls', 0)} calls)")
    L.append(f"    Automation saved   {_money(today.get('automation_saved', 0)):>14}   ({today.get('automation_calls', 0)} runs)")
    L.append(f"    Gross avoided      {_money(today.get('avoided_cost', 0)):>14}")
    L.append(f"    NET (saved-spend)  {_money(today.get('net_saved', 0)):>14}")
    dist = today.get("call_distribution", {})
    if dist:
        L.append("    Calls: " + "  ".join(f"{k}={v}" for k, v in dist.items()))

    # 7-DAY
    L.append("")
    L.append(f"  LAST 7 DAYS  (since {wk['since']})")
    L.append(f"    Paid API spend     {_money(wk['paid_spend']):>14}   ({wk['paid_calls']} calls)")
    L.append(f"    Local saved        {_money(wk['local_saved']):>14}   ({wk['local_calls']} calls)")
    L.append(f"    Free-pool calls    {'':>14}   ({wk['free_calls']} calls, $0 displaced)")
    L.append(f"    Hybrid saved       {_money(wk['hybrid_saved']):>14}   ({wk['hybrid_calls']} calls)")
    L.append(f"    Gross avoided      {_money(wk['avoided_cost']):>14}")
    L.append(f"    NET (saved-spend)  {_money(wk['net_saved']):>14}")
    L.append(f"    Total calls        {wk['total_calls']:>14,}")
    savers = ", ".join(f"{k} ({_money(v)})" for k, v in wk["top_sources"].items() if v > 0)
    L.append("    Top savers: " + (savers[:240] if savers else "(free-pool only, $0 displaced)"))

    # ALL-TIME + ROI
    L.append("")
    L.append("  ALL-TIME")
    L.append(f"    Paid API spend     {_money(alltime.get('api_cost', 0)):>14}   ({alltime.get('api_calls', 0)} calls)")
    L.append(f"    Gross avoided      {_money(alltime.get('avoided_cost', 0)):>14}")
    L.append(f"    NET                {_money(alltime.get('net_saved', 0)):>14}   over {alltime.get('days_active', 0)} days")
    payoff = can.get("payoff", {}) if "_error" not in can else {}
    if payoff:
        L.append(f"    Device payoff      {payoff.get('pct_paid_off', 0)}%   ({_money(payoff.get('amount_paid_off', 0))} / {_money(payoff.get('device_cost', 0))})")

    # ROI
    spend_alltime = float(alltime.get("api_cost", 0) or 0)
    revenue = rev["revenue"]
    L.append("")
    L.append("  ROI")
    if revenue > 0 and spend_alltime > 0:
        roi = ((revenue - spend_alltime) / spend_alltime) * 100
        L.append(f"    Revenue {_money(revenue)}  vs  spend {_money(spend_alltime)}   →  ROI {roi:,.0f}%")
    elif revenue > 0:
        L.append(f"    Revenue {_money(revenue)}  (spend ~$0)")
    else:
        L.append(f"    No realized revenue recorded yet ({rev['detail']}).")
        L.append(f"    Value delivered so far = avoided cost {_money(alltime.get('avoided_cost', 0))}.")

    # AUTOMATION
    L.append("")
    L.append("  AUTOMATION HEALTH")
    if "_error" in auto:
        L.append(f"    ! {auto['_error']}")
    else:
        L.append(f"    Jobs tracked       {auto['total']:>5}")
        L.append(f"    Running now        {len(auto['running']):>5}")
        L.append(f"    Last exit clean(0) {len(auto['clean']):>5}")
        L.append(f"    Signaled (<0,ok)   {len(auto['signaled']):>5}")
        L.append(f"    FAILING (exit>0)   {len(auto['failing']):>5}")
        if auto["failing"]:
            L.append("    ↳ failing: " + ", ".join(
                f"{j['label'].split('.')[-1]}({j['status']})" for j in auto["failing"]))
    L.append(_hr("═"))
    L.append("")
    return "\n".join(L)


DEVICE_COST = 1138.49


def _days_owned() -> tuple[int, str]:
    """Best-effort days since the Mac mini's first boot. Defensible signals, in order:
    .AppleSetupDone birth time → home-dir birth → earliest ledger entry. Returns (days, source)."""
    import os
    now = datetime.now().timestamp()
    for path, label in [("/var/db/.AppleSetupDone", "first-boot setup"),
                        (str(Path.home()), "home dir created")]:
        try:
            bt = os.stat(path).st_birthtime
            if bt and bt < now:
                return max(1, int((now - bt) / 86400)), label
        except Exception:
            pass
    # fallback: earliest cost/savings ledger entry
    earliest = None
    for name in ("cost.jsonl", "savings.jsonl"):
        for e in _read_jsonl(LOG_DIR / name):
            d = _entry_date(e)
            if d and (earliest is None or d < earliest):
                earliest = d
    if earliest:
        try:
            dt = datetime.fromisoformat(earliest)
            return max(1, (datetime.now() - dt).days), "earliest ledger entry"
        except Exception:
            pass
    return 47, "fallback default"


def lifetime_estimate(owned_days: int | None = None) -> dict:
    """Retroactive lifetime value estimate (conservative/likely/optimistic).

    Pure read + arithmetic. Extrapolates beyond the ~logged window because
    instrumentation only started ~Apr 13 and is partial (serious undercount).
    """
    can = canonical_snapshot()
    wk = seven_day_rollup()
    alltime = can.get("alltime", {}) if "_error" not in can else {}
    days_active = float(alltime.get("days_active", 0) or 0) or 47.0
    logged_net = float(alltime.get("net_saved", 0) or 0)
    logged_avoided = float(alltime.get("avoided_cost", 0) or 0)

    if owned_days is None:
        owned_days, owned_src = _days_owned()
    else:
        owned_src = "user-specified"
    pre_instrumentation_days = max(0, owned_days - days_active)

    # daily rates
    rate_7d = float(wk.get("net_saved", 0) or 0) / 7.0          # current rate
    rate_alltime = logged_net / days_active if days_active else 0  # blended logged rate

    # docs generated
    docs_dir = Path.home() / "Documents" / "Claude Outputs"
    try:
        docs = sum(1 for f in docs_dir.glob("*") if f.suffix.lower() in (".docx", ".html", ".pdf"))
    except Exception:
        docs = 0
    # social posts (verify ledger, best-effort)
    posts = 0
    for vf in (Path.home() / ".openclaw/workspace/android-logs/engage-verify.jsonl",):
        try:
            posts = sum(1 for _ in open(vf, encoding="utf-8"))
        except Exception:
            pass

    def scenario(name, ramp, doc_val, post_val, rate):
        extrapolated = pre_instrumentation_days * rate * ramp
        doc_value = docs * doc_val
        post_value = posts * post_val
        total = logged_net + extrapolated + doc_value + post_value
        return {
            "scenario": name,
            "logged_net_saved": round(logged_net, 2),
            "pre_instrumentation_extrapolated": round(extrapolated, 2),
            "docs_value": round(doc_value, 2),
            "social_value": round(post_value, 2),
            "total_value": round(total, 2),
            "pct_of_device": round(100 * total / DEVICE_COST, 1),
            "assumptions": f"ramp={ramp}, $/doc={doc_val}, $/post={post_val}, $/day={round(rate,3)}",
        }

    return {
        "device_cost": DEVICE_COST,
        "days_owned": owned_days,
        "days_owned_source": owned_src,
        "days_logged": round(days_active, 1),
        "pre_instrumentation_days": pre_instrumentation_days,
        "docs_generated": docs,
        "social_posts_logged": posts,
        "current_daily_rate_7d": round(rate_7d, 3),
        "blended_logged_daily_rate": round(rate_alltime, 3),
        "scenarios": {
            "conservative": scenario("conservative", 0.25, 2, 0.05, rate_alltime),
            "likely": scenario("likely", 0.45, 5, 0.15, rate_alltime),
            "optimistic": scenario("optimistic", 0.70, 10, 0.40, max(rate_7d, rate_alltime)),
        },
        "note": ("Estimate, not ledger truth. Logged instrumentation is partial "
                 "(known undercount); pre-instrumentation value is extrapolated with a "
                 "ramp discount. Override days with --owned-days N."),
    }


def render_lifetime(est: dict) -> str:
    L = ["", "  LIFETIME VALUE ESTIMATE (since Mac mini first boot)".ljust(50) + datetime.now().isoformat(timespec="seconds"), _hr("═")]
    L.append(f"    Device cost        {_money(est['device_cost']):>14}")
    L.append(f"    Days owned         {est['days_owned']:>14}   ({est['days_owned_source']})")
    L.append(f"    Days logged        {est['days_logged']:>14}   ({est['pre_instrumentation_days']} days pre-instrumentation)")
    L.append(f"    Docs generated     {est['docs_generated']:>14}")
    L.append(f"    Social posts (log) {est['social_posts_logged']:>14}")
    L.append(f"    Daily rate (7d)    {_money(est['current_daily_rate_7d']):>14}   blended {_money(est['blended_logged_daily_rate'])}")
    L.append("")
    for key in ("conservative", "likely", "optimistic"):
        s = est["scenarios"][key]
        L.append(f"  {key.upper()}")
        L.append(f"    logged {_money(s['logged_net_saved'])}  + extrapolated {_money(s['pre_instrumentation_extrapolated'])}  "
                 f"+ docs {_money(s['docs_value'])}  + social {_money(s['social_value'])}")
        L.append(f"    ➤ TOTAL {_money(s['total_value'])}   ({s['pct_of_device']}% of device)")
        L.append(f"      ({s['assumptions']})")
    L.append(_hr("═"))
    L.append("  " + est["note"])
    L.append("")
    return "\n".join(L)


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    if "--lifetime" in argv:
        owned = None
        if "--owned-days" in argv:
            try:
                owned = int(argv[argv.index("--owned-days") + 1])
            except Exception:
                owned = None
        est = lifetime_estimate(owned)
        if "--json" in argv:
            print(json.dumps(est, indent=2, default=str))
        else:
            print(render_lifetime(est))
        return 0
    report = build_report()
    if "--json" in argv:
        print(json.dumps(report, indent=2, default=str))
    else:
        print(render(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
