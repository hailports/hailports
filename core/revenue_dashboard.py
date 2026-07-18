#!/usr/bin/env python3
"""revenue_dashboard.py — ONE command for the whole revenue picture.

  python3 -m core.revenue_dashboard

Pulls live Stripe (revenue, products, abandoned carts), the funnel ledger
(send->reply->purchase by lane/variant), lead-list inventory, email pool health,
and today's social posts. Deterministic + free API reads. No LLM cost.
"""
from __future__ import annotations

import json
import os
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(os.environ.get("CLAUDE_STACK_DIR", Path(__file__).resolve().parents[1]))
HUSTLE = ROOT / "data" / "hustle"   # internal now (was /Volumes/External/data/hustle)


def _env(key: str) -> str:
    for line in (ROOT / ".env").read_text(errors="ignore").splitlines():
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip().strip('"\'')
    return ""


def _stripe(path: str, params: str = "") -> dict:
    key = _env("STRIPE_SECRET_KEY")
    if not key:
        return {"error": "no key"}
    try:
        url = f"https://api.stripe.com/v1/{path}" + (f"?{params}" if params else "")
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"})
        return json.loads(urllib.request.urlopen(req, timeout=12).read())
    except Exception as e:
        return {"error": str(e)[:80]}


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def build() -> dict:
    out: dict = {"generated_at": datetime.now(timezone.utc).isoformat()}

    # --- STRIPE revenue ---
    charges = _stripe("charges", "limit=100")
    paid = [c for c in charges.get("data", []) if c.get("paid") and c.get("status") == "succeeded"]
    revenue = sum(c.get("amount", 0) for c in paid) / 100
    sessions = _stripe("checkout/sessions", "limit=50&status=open")
    open_carts = [s for s in sessions.get("data", [])
                  if (s.get("customer_details") or {}).get("email")
                  and (s.get("customer_details") or {}).get("email") not in ("user@example.com", "buyer@example.com")]
    out["stripe"] = {
        "total_revenue_usd": round(revenue, 2),
        "paid_charges": len(paid),
        "real_open_carts": len(open_carts),
        "open_cart_value_usd": round(sum((s.get("amount_total") or 0) for s in open_carts) / 100, 2),
    }

    # --- FUNNEL ledger ---
    try:
        from core.funnel_tracker import summary as funnel_summary
        out["funnel"] = funnel_summary()
    except Exception as e:
        out["funnel"] = {"error": str(e)[:80]}

    # --- LEAD-LIST inventory ---
    man = ROOT / "products" / "leadlists" / "manifest.json"
    if man.exists():
        m = json.loads(man.read_text())
        out["leadlist_inventory"] = {v["name"]: f"{v['count']} leads ({v['verified_emails']} verified)"
                                     for v in m.get("products", {}).values()}

    # --- EMAIL pool health ---
    try:
        prospects = json.loads((ROOT / "products" / "outreach" / "prospects.json").read_text())
        def is_maroon(p):
            b = " ".join(str(p.get(k) or "") for k in ("brand", "segment", "sender_alias")).lower()
            return "BrandA" in b
        sendable = [p for p in prospects if p.get("status") in ("active", "new", "queued", "probing", "role_address")
                    and p.get("current_step", 0) == 0
                    and str(p.get("email_verify") or "").lower() not in ("undeliverable", "invalid")]
        out["email_pool"] = {
            "sendable_total": len(sendable),
            "docsapp": sum(1 for p in sendable if not is_maroon(p)),
            "BrandA": sum(1 for p in sendable if is_maroon(p)),
            "deliverable_verified": sum(1 for p in sendable if p.get("email_verify") == "deliverable"),
        }
    except Exception as e:
        out["email_pool"] = {"error": str(e)[:80]}

    # --- EMAIL sent today (from sent log) ---
    try:
        sent_log = ROOT / "data" / "hustle" / "outreach_sent.jsonl"
        today = _today()
        sent_today = sum(1 for l in sent_log.read_text().splitlines()
                         if l.strip() and json.loads(l).get("date") == today)
        out["email_sent_today"] = sent_today
    except Exception:
        out["email_sent_today"] = 0

    # --- SOCIAL posts today ---
    try:
        results = Path.home() / ".openclaw/workspace/android-logs/engage-results.jsonl"
        today = _today()
        from collections import Counter
        posts = Counter()
        for l in results.read_text().splitlines():
            if not l.strip():
                continue
            r = json.loads(l)
            if r.get("verified") and (r.get("ts", "")[:10] == today):
                posts[r.get("platform", "?")] += 1
        out["social_posts_today"] = dict(posts)
    except Exception:
        out["social_posts_today"] = {}

    return out


def render(d: dict) -> str:
    L = []
    L.append("=" * 58)
    L.append(f"  REVENUE DASHBOARD — {d['generated_at'][:16]}")
    L.append("=" * 58)
    s = d.get("stripe", {})
    L.append(f"\n  MONEY")
    L.append(f"    Revenue (lifetime):    ${s.get('total_revenue_usd', 0)}")
    L.append(f"    Paid charges:          {s.get('paid_charges', 0)}")
    L.append(f"    Real open carts:       {s.get('real_open_carts', 0)}  (${s.get('open_cart_value_usd', 0)} at risk)")
    f = d.get("funnel", {})
    if "by_stage" in f:
        bs = f["by_stage"]
        L.append(f"\n  FUNNEL (lifetime)")
        L.append(f"    sends={bs.get('send', 0)}  replies={bs.get('reply', 0)}  "
                 f"checkouts={bs.get('checkout_started', 0)}  purchases={bs.get('purchase', 0)}")
        if f.get("by_variant"):
            L.append(f"    A/B: " + " | ".join(f"{k}: {v.get('send',0)} sent" for k, v in f["by_variant"].items()))
    L.append(f"\n  EMAIL")
    ep = d.get("email_pool", {})
    L.append(f"    Sendable pool:         {ep.get('sendable_total', 0)}  (docsapp {ep.get('docsapp',0)} / BrandA {ep.get('BrandA',0)})")
    L.append(f"    Deliverable-verified:  {ep.get('deliverable_verified', 0)}")
    L.append(f"    Sent today:            {d.get('email_sent_today', 0)}")
    L.append(f"\n  LEAD-LIST INVENTORY (sellable now)")
    for name, info in d.get("leadlist_inventory", {}).items():
        L.append(f"    {name}: {info}")
    L.append(f"\n  SOCIAL POSTS TODAY")
    sp = d.get("social_posts_today", {})
    L.append(f"    " + (" | ".join(f"{k}: {v}" for k, v in sp.items()) if sp else "none yet"))
    L.append("=" * 58)
    return "\n".join(L)


if __name__ == "__main__":
    import sys
    d = build()
    if "--json" in sys.argv:
        print(json.dumps(d, indent=2))
    else:
        print(render(d))
