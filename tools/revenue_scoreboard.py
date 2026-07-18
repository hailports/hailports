#!/usr/bin/env python3
"""Daily revenue scoreboard — did the machine actually move?

Compiles Stripe (read-only), funnel, distribution, leads and open human gates
into one jsonl row (data/hustle/revenue_scoreboard.jsonl) + a short iMessage
to Operator. Runs 08:30 + 20:30 via com.claude-stack.revenue-scoreboard.
"""
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = Path.home() / "claude-stack"
sys.path.insert(0, str(BASE))
from dotenv import load_dotenv
load_dotenv(BASE / ".env", override=True)

OUT = BASE / "data/hustle/revenue_scoreboard.jsonl"
NOW = datetime.now(timezone.utc)
CUTOFF = NOW - timedelta(hours=24)

# Human gates still blocking revenue (update by hand when one clears)
GATES = [
    "gumroad: log into gumroad.com once so file attach can run (products sell empty otherwise)",
    "reddit: re-run reddit_login_capture.py when session dies",
    # $99/mo intent landing is LIVE at scannerapp.dev (+ /sample free teaser) — verified
    # 2026-06-09. Real gap is QUALIFIED TRAFFIC, not a missing page (the /go clicks logged
    # so far are mostly crawlers/bots). Distribution, not build.
    "traffic: intent landing live at scannerapp.dev but only bot clicks so far — needs real visitors",
]


def _parse_ts(s):
    if not s:
        return None
    try:
        ts = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _recent(ts_str):
    ts = _parse_ts(ts_str)
    return ts is not None and ts >= CUTOFF


def _jload(path, default):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError):
        return default


# A "created" session count is a vanity number — bots and abandoned-pre-email loads mint
# sessions that never had a buyer. These local-parts/emails are our own probes/smoke-tests.
_SYNTH_LOCALPARTS = {"probe", "test", "funneltest", "canary", "selftest", "smoke", "audit"}


def _session_email(s):
    cd = s.get("customer_details") or {}
    return (cd.get("email") or s.get("customer_email") or "").strip().lower()


def _is_synthetic_session(s):
    email = _session_email(s)
    if not email:
        return True  # no email = bot / abandoned before the buyer even typed one
    if email.endswith("@example.com"):
        return True
    if email.split("@", 1)[0] in _SYNTH_LOCALPARTS:
        return True
    if (s.get("metadata") or {}).get("synthetic"):
        return True
    return False


def stripe_block():
    key = os.environ.get("STRIPE_SECRET_KEY", "")
    if not key:
        return {"error": "no STRIPE_SECRET_KEY"}
    created, completed, revenue_cents = 0, 0, 0
    synthetic, no_email = 0, 0
    params = {"limit": "100", "created[gte]": str(int(CUTOFF.timestamp()))}
    url = "https://api.stripe.com/v1/checkout/sessions?" + urllib.parse.urlencode(params)
    try:
        while url:
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"})
            data = json.loads(urllib.request.urlopen(req, timeout=30).read())
            sessions = data.get("data", [])
            created += len(sessions)
            for s in sessions:
                if _is_synthetic_session(s):
                    synthetic += 1
                if not _session_email(s):
                    no_email += 1
                if s.get("status") == "complete" or s.get("payment_status") == "paid":
                    completed += 1
                    revenue_cents += s.get("amount_total") or 0
            url = None
            if data.get("has_more") and sessions:
                params["starting_after"] = sessions[-1]["id"]
                url = "https://api.stripe.com/v1/checkout/sessions?" + urllib.parse.urlencode(params)
    except Exception as e:
        return {"error": str(e)[:120]}
    return {"sessions_created_24h": created,  # keep raw for the record
            "sessions_created_synthetic_24h": synthetic,
            "no_email_sessions_24h": no_email,
            "sessions_created_real_24h": created - synthetic,
            "sessions_completed_24h": completed,
            "revenue_usd_24h": round(revenue_cents / 100, 2)}


def funnel_block():
    # Read BOTH ledgers — products/self_serve (storefront) AND data/ (the unified ledger the
    # hailports checker writes to). Reading only the first made the checker look dead.
    paths = [BASE / "products/self_serve/funnel_events.jsonl",
             BASE / "data/funnel_events.jsonl"]
    if not any(p.exists() for p in paths):
        return {"note": "funnel_events.jsonl not live yet"}
    from core.traffic_classifier import classify  # one source of truth (vs. notifier)
    pageviews, go_clicks, captures = 0, 0, 0
    human_go_clicks, bot_go_clicks = 0, 0
    bot_pageviews, attributed = 0, 0
    human_checks, bot_checks, check_captures = 0, 0, 0
    attributed_sessions = set()
    seen = set()
    for path in paths:
        if not path.exists():
            continue
        for line in path.read_text().splitlines():
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not _recent(ev.get("ts")):
                continue
            # Drop ip + microseconds so the SAME click logged to both ledgers collapses to one
            # row (the double-ledger was double-counting /go clicks).
            k = (str(ev.get("ts") or "")[:19], ev.get("event"), ev.get("url"))
            if k in seen:  # de-dup rows that could appear in both ledgers
                continue
            seen.add(k)
            kind, url = ev.get("event", ""), ev.get("url", "") or ""
            if url.startswith("/go/") or kind == "go_click":
                go_clicks += 1
                if classify(ev)["is_human"]:
                    human_go_clicks += 1
                else:
                    bot_go_clicks += 1
            elif kind in ("email_capture", "capture", "email"):
                if classify(ev)["verdict"] in ("owner", "bot", "datacenter"):
                    continue  # localhost smoke-tests / bots don't count as real captures
                captures += 1
                if url == "/check":
                    check_captures += 1
            elif kind == "scan_start":  # checker run — classify so bot bursts don't inflate it
                if classify(ev)["is_human"]:
                    human_checks += 1
                else:
                    bot_checks += 1
            elif kind == "pageview":
                c = classify(ev)
                if c["is_human"]:
                    pageviews += 1
                    if c["is_attributed"]:
                        attributed += 1
                        attributed_sessions.add(ev.get("sid") or id(ev))
                elif c["verdict"] in ("bot", "datacenter"):
                    bot_pageviews += 1
    return {"human_pageviews_24h": pageviews, "go_clicks_24h": go_clicks,
            "human_go_clicks_24h": human_go_clicks, "bot_go_clicks_24h": bot_go_clicks,
            "email_captures_24h": captures,
            # the only number that proves a channel we published delivered a prospect:
            "attributed_humans_24h": len(attributed_sessions),
            "bot_pageviews_24h": bot_pageviews,
            # checker funnel — the new kill-gate metrics (real human checks -> captures):
            "human_checks_24h": human_checks, "bot_checks_24h": bot_checks,
            "check_captures_24h": check_captures,
            "check_capture_pct": round(check_captures / human_checks, 3) if human_checks else 0.0}


def distribution_block():
    out = {}
    posted = _jload(BASE / "data/hustle/response_posted.json", {})
    out["intent_replies_24h"] = sum(
        1 for v in posted.get("posted", {}).values() if _recent(v.get("ts")))
    out["intent_replies_today"] = posted.get("daily", {}).get(
        NOW.strftime("%Y-%m-%d"), 0)
    queue = _jload(BASE / "data/hustle/response_post_queue.json", {})
    out["reply_queue_depth"] = len(queue.get("items", []))  # all gate-passed drafts
    ledger = _jload(Path.home() / ".openclaw/workspace/android-logs/posted-content-ledger.json", [])
    out["reels_posted_24h"] = sum(1 for r in ledger if _recent(r.get("ts")))
    engage = Path.home() / ".openclaw/workspace/android-logs/engage-results.jsonl"
    comments = 0
    if engage.exists():
        for line in engage.read_text().splitlines():
            try:
                if _recent(json.loads(line).get("ts")):
                    comments += 1
            except json.JSONDecodeError:
                continue
    out["comments_posted_24h"] = comments
    dg = _jload(BASE / "data/hustle/persona3_queue.json", [])
    out["persona3_posted_24h"] = sum(
        1 for x in dg if x.get("status") == "posted" and _recent(x.get("posted_at")))
    return out


def leads_block():
    out = {}
    mm = _jload(BASE / "data/hustle/multi_market_leads.json", {})
    out["intent_leads_total"] = mm.get("count", 0)
    out["intent_leads_new_24h"] = sum(
        1 for l in mm.get("leads", []) if _recent(l.get("found_at")))
    strike = BASE / "data/hustle/INTENT_STRIKE_LIST.md"
    if strike.exists():
        age_h = (NOW.timestamp() - strike.stat().st_mtime) / 3600
        out["strike_list_age_hours"] = round(age_h, 1)
    else:
        out["strike_list_age_hours"] = None
    return out


def send_imessage(subject, body):
    # Route the (signal-gated, earns-only) scoreboard through the gateway: its
    # money keywords ($/revenue/leads) pass the allowlist so it still pages, but
    # it now dedups and can't flood if the job's earn-signal ever flaps.
    import sys
    sys.path.insert(0, str(BASE))
    try:
        from core.alert import alert_alex
        alert_alex(subject, body)
        return True
    except Exception:
        import importlib.util
        spec = importlib.util.spec_from_file_location("_stack_alert", BASE / "core" / "alert.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod._send_imessage_phone(subject, body)


def main():
    row = {
        "ts": NOW.isoformat(),
        "stripe": stripe_block(),
        "funnel": funnel_block(),
        "distribution": distribution_block(),
        "leads": leads_block(),
        "gates_blocking": GATES,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("a") as f:
        f.write(json.dumps(row) + "\n")

    s, fu, d, le = row["stripe"], row["funnel"], row["distribution"], row["leads"]
    rev = s.get("revenue_usd_24h", "?")
    lines = [
        f"💵 ${rev} | checkouts {s.get('sessions_completed_24h', '?')}/{s.get('sessions_created_24h', '?')} (24h)",
        f"🌐 views {fu.get('human_pageviews_24h', '?')} | /go {fu.get('go_clicks_24h', '?')} | emails {fu.get('email_captures_24h', '?')}",
        f"🔎 checks {fu.get('human_checks_24h', '?')} (bot {fu.get('bot_checks_24h', '?')}) → captures {fu.get('check_captures_24h', '?')} ({int(fu.get('check_capture_pct', 0) * 100)}%)",
        f"📣 replies {d['intent_replies_24h']} (queue {d['reply_queue_depth']}) | reels {d['reels_posted_24h']} | comments {d['comments_posted_24h']} | gary {d['persona3_posted_24h']}",
        f"🎯 leads +{le['intent_leads_new_24h']} new ({le['intent_leads_total']} total) | strike list {le['strike_list_age_hours']}h old",
        f"🚧 gates: {len(GATES)} open — " + GATES[0].split(":")[0] + ", " + GATES[2][:40],
    ]
    # Signal-gated: text ONLY when the machine actually EARNS or a hand-raiser appears
    # (real revenue, a completed checkout, or an email capture). A flat $0 still logs the
    # row to OUT for the record but does NOT text — no more hourly $0 spam.
    def _pos(v):
        try:
            return float(v) > 0
        except Exception:
            return False
    earned = (_pos(s.get("revenue_usd_24h")) or _pos(s.get("sessions_completed_24h"))
              or _pos(fu.get("email_captures_24h")))
    ok = send_imessage("revenue scoreboard", "\n".join(lines)) if earned else False
    print(json.dumps({"ok": True, "imessage_sent": ok, "earned": earned, "row": row}, indent=2))


if __name__ == "__main__":
    main()
