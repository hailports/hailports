#!/usr/bin/env python3
"""truth_report — the report that cannot pacify.

Counts ONLY outcomes a customer caused: money, humans, deliveries, replies, raised
hands answered. Zeros are printed as zeros. Lanes we can't measure are listed as
NOT MEASURED instead of being skipped (a skipped metric is how "everything's fine"
lies happen). No build/infra status appears here, ever — that's what SYSTEM_CHANGELOG
is for. iMessaged to the owner 2x daily (launchd com.claude-stack.truth-report).

    python3 tools/truth_report.py            # compute + send
    python3 tools/truth_report.py --dry      # compute + print only
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HUST = ROOT / "data" / "hustle"
sys.path.insert(0, str(ROOT))

DAY = 86400


def _now() -> float:
    return datetime.now(timezone.utc).timestamp()


def _iso_ts(s: str) -> float:
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def _rows_24h(path: Path, ts_keys=("ts", "found_at", "time", "sent_at", "created_at")) -> int:
    cutoff = _now() - DAY
    n = 0
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        try:
            r = json.loads(line)
        except Exception:
            continue
        for k in ts_keys:
            if r.get(k) and _iso_ts(str(r[k])) > cutoff:
                n += 1
                break
    return n


def money() -> str:
    key = ""
    for ln in (ROOT / ".env").read_text().splitlines():
        if ln.startswith("STRIPE_SECRET_KEY="):
            key = ln.split("=", 1)[1].strip()
    import base64
    auth = "Basic " + base64.b64encode((key + ":").encode()).decode()
    cutoff = int(_now() - DAY)  # Stripe created[gte] needs an int unix ts (float → HTTP 400)
    # Paginate the full 24h window so a >100-charge day isn't silently undercounted, and drop the
    # old "EVER" label — limit=100 only ever saw the last 100 charges, so "EVER" was a lie.
    day_cents, day_n, starting_after = 0, 0, ""
    while True:
        url = f"https://api.stripe.com/v1/charges?limit=100&created[gte]={cutoff}"
        if starting_after:
            url += f"&starting_after={starting_after}"
        req = urllib.request.Request(url)
        req.add_header("Authorization", auth)
        d = json.loads(urllib.request.urlopen(req, timeout=20).read())
        data = d.get("data", [])
        if not data:
            break
        for c in data:
            if c.get("status") == "succeeded":
                day_cents += c["amount"]
                day_n += 1
        if not d.get("has_more"):
            break
        starting_after = data[-1]["id"]
    return f"${day_cents/100:.0f} in 24h | {day_n} successful charges in 24h"


def humans() -> str:
    from core.traffic_classifier import classify
    cutoff = _now() - DAY
    human, attributed = 0, 0
    with (ROOT / "products/self_serve/funnel_events.jsonl").open(errors="ignore") as fh:
        for line in fh:
            try:
                e = json.loads(line)
            except Exception:
                continue
            if _iso_ts(str(e.get("ts", ""))) <= cutoff:
                continue
            v = classify(e)
            if v.get("is_human"):
                human += 1
                if v.get("is_attributed"):
                    attributed += 1
    return f"{human} human events in 24h ({attributed} from our own links)"


def delivered() -> str:
    sends = 0
    for p in HUST.glob("*sent*.jsonl"):
        try:
            sends += _rows_24h(p)
        except Exception:
            continue
    for p in HUST.glob("*sends*.jsonl"):
        try:
            sends += _rows_24h(p)
        except Exception:
            continue
    return f"{sends} outbound emails recorded in 24h (inbox placement NOT verified per-send)"


def hands() -> str:
    q = HUST / "looking_for_queue.jsonl"
    cutoff = _now() - DAY
    fresh, oldest_unfired_h = 0, 0.0
    for line in q.read_text().splitlines():
        try:
            r = json.loads(line)
        except Exception:
            continue
        ts = _iso_ts(str(r.get("found_at", "")))
        if ts > cutoff:
            fresh += 1
            age_h = (_now() - ts) / 3600
            oldest_unfired_h = max(oldest_unfired_h, age_h)
    note = f", oldest sitting {oldest_unfired_h:.0f}h un-answered" if fresh else ""
    return f"{fresh} raised hands found in 24h{note} (replies are owner-fired — not auto-tracked)"


def conversations() -> str:
    p = HUST / "ai_closer_conversations.jsonl"
    n = _rows_24h(p) if p.exists() else 0
    return f"{n} two-way prospect conversations in 24h"


def main() -> int:
    dry = "--dry" in sys.argv
    metrics = [("MONEY", money), ("CONVERSATIONS", conversations), ("HUMANS ON SITE", humans),
               ("RAISED HANDS", hands), ("OUTBOUND", delivered)]
    lines = [f"📊 TRUTH — outcomes only, {datetime.now().strftime('%a %m-%d %H:%M')}"]
    for name, fn in metrics:
        try:
            lines.append(f"{name}: {fn()}")
        except Exception as e:
            lines.append(f"{name}: NOT MEASURED ({type(e).__name__})")
    lines.append("NOT MEASURED AT ALL: email replies (detector has no daily count), "
                 "social DMs, CL relay responses.")
    body = "\n".join(lines)
    print(body)
    if not dry:
        # OWNER RULE 2026-07-15: iMessage is money + defcon-1 ONLY. A daily "outcomes" report is
        # a passive record, not an alert — it only PUSHES when there was real money (>=1 charge);
        # a $0/no-charge day is logged/printed, never texted.
        import re as _re
        m = _re.search(r"(\d+)\s+successful charges", body)
        has_money = bool(m and int(m.group(1)) > 0)
        if has_money:
            try:
                from tools.imsg_bridge import send_imessage
                send_imessage(body)
            except Exception as e:
                print(f"[truth-report] send failed: {e}", file=sys.stderr)
                return 1
        else:
            print("[truth-report] no charges in 24h -> logged, not texted (owner alert rule)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
