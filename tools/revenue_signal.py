#!/usr/bin/env python3
"""Revenue signal brain — rank channels by REAL-HUMAN yield, kill what doesn't work.

The relentless loop is dumb on its own: it sprays effort evenly. This makes it smart.
It pulls every real-human signal across every channel, filters bots through the one
source of truth (core.traffic_classifier), attributes each human action to the channel
that produced it, and ranks channels by real-human-yield per 100 attempts.

Real-human actions counted (all-time):
  open      email pixel hit (docsapp.dev/t/<id>.png -> opens.jsonl), bot/proxy filtered
  visit     attributed human pageview (funnel_events, is_attributed via utm_source)
  click     human /go click on the funnel
  capture   email capture on the funnel
  checkout  Stripe checkout session a human started
  sale      Stripe paid session OR Gumroad sale (owner/test excluded)
  reply     real inbound email reply (bounce/auto-responder excluded)

Attempts (the denominator) come ONLY from concrete send/distribution logs — never
invented. Pull channels with no send log (organic SEO etc.) report yield = null and are
ranked on absolute real humans, flagged "organic (no attempt cost)".

Writes data/hustle/channel_ranking.json (the file the orchestrator + Operator read each
cycle) with the ranking + a double-down/kill recommendation, and prints a tight board.

CLI: python3 tools/revenue_signal.py
Refreshes hourly via com.claude-stack.revenue-signal (launchd).
"""
from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

try:
    from dotenv import load_dotenv
    load_dotenv(BASE / ".env", override=False)
except Exception:
    pass

from core.traffic_classifier import (  # one source of truth for bot-vs-human
    classify,
    is_bot_ua,
    is_datacenter_ip,
    is_internal_ip,
)

OUT = BASE / "data/hustle/channel_ranking.json"
OWNER_EMAILS = {
    "user@example.com", "user@example.com",
    "user@example.com", "user@example.com",
}
# Our own sending identities — a "reply" FROM one of these is our outbound bleeding
# back through the mail index, never a prospect. Reuses the classifier's owner refs.
SELF_SENDER_DOMAINS = {
    "branda.com", "scannerapp.dev", "docsapp.dev", "redacted.com",
    "Operator.com", "redacted.com", "signalhq.com", "builtfast",
    "hailports",
}
# A channel needs at least this many attempts before we trust its yield enough to
# act on a double-down / kill verdict (otherwise n is too small to mean anything).
MIN_ATTEMPTS_FOR_VERDICT = 15

ACTION_TYPES = ("open", "visit", "click", "capture", "checkout", "sale", "reply")

# Placeholder / test emails that scanners and our own test clicks leave on Stripe
# sessions — never a real buyer. An unpaid session with one of these (or no email at
# all) is not a real-human checkout, it's noise.
_PLACEHOLDER_EMAIL_DOMAINS = {
    "example.com", "test.com", "b.com", "a.com", "x.com", "email.com",
    "domain.com", "test.test", "localhost", "none.com", "sample.com",
}
_PLACEHOLDER_LOCALPARTS = {"test", "buyer", "foo", "bar", "asdf", "a", "b", "x"}


def _real_buyer_email(email: str) -> bool:
    email = (email or "").strip().lower()
    if "@" not in email or "." not in email.split("@")[-1]:
        return False
    local, _, domain = email.partition("@")
    if email in OWNER_EMAILS:
        return False
    if domain in _PLACEHOLDER_EMAIL_DOMAINS or local in _PLACEHOLDER_LOCALPARTS:
        return False
    if any(d in domain for d in SELF_SENDER_DOMAINS):
        return False
    return True


def _read_jsonl(path: Path):
    if not path.exists():
        return
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return default


def _seq_family(seq: str) -> str:
    """Collapse a verbose sequence/campaign name into a short channel family."""
    s = (seq or "").lower()
    table = (
        ("48-hour", "email:fix_audit_48h"),
        ("teardown", "email:teardown_$1"),
        ("$1 salesforce", "email:teardown_$1"),
        ("free sales", "email:free_healthcheck"),
        ("free salesforce", "email:free_healthcheck"),
        ("compliance", "email:compliance"),
        ("revops", "email:revops"),
        ("hiring", "email:hiring"),
        ("boutique", "email:boutique"),
        ("consulting", "email:consulting"),
    )
    for needle, fam in table:
        if needle in s:
            return fam
    if not s or s == "unknown":
        return "email:unknown"
    slug = "".join(c if c.isalnum() else "_" for c in s)[:24].strip("_")
    return f"email:{slug or 'other'}"


class Channel:
    __slots__ = ("name", "attempts", "actions", "kind")

    def __init__(self, name, kind="push"):
        self.name = name
        self.attempts = 0
        self.actions = defaultdict(int)
        self.kind = kind  # push = we have an attempt count; pull = organic/no cost

    @property
    def total_actions(self):
        return sum(self.actions.values())

    @property
    def yield_per_100(self):
        if self.attempts <= 0:
            return None
        return round(self.total_actions / self.attempts * 100, 2)


def _real_open(row) -> bool:
    """An open pixel hit from a real person, not a proxy/scanner/our own test.

    classify() can't judge opens (they carry no sid/utm/ref so it returns 'unknown'),
    so apply the same primitives directly: drop bot UAs, datacenter/proxy IPs (this is
    what strips Apple-Mail-Privacy 17.x and Gmail-image-proxy google ranges that inflate
    open counts), internal/owner IPs, and UA-less script hits.
    """
    ua = str(row.get("ua") or "")
    ip = str(row.get("ip") or "")
    if not ua.strip():
        return False
    if is_bot_ua(ua):
        return False
    if is_internal_ip(ip) or is_datacenter_ip(ip):
        return False
    return True


def collect():
    channels: dict[str, Channel] = {}
    sources_read = {}

    def ch(name, kind="push") -> Channel:
        if name not in channels:
            channels[name] = Channel(name, kind)
        return channels[name]

    # --- 1. Email outreach: sends = attempts, build track_id -> channel map ---
    track_to_channel: dict[str, str] = {}
    n_sent = 0
    for row in _read_jsonl(BASE / "products/outreach/sent.jsonl"):
        n_sent += 1
        fam = _seq_family(row.get("sequence", ""))
        ch(fam).attempts += 1
        tid = row.get("track_id")
        if tid:
            track_to_channel[tid] = fam
    sources_read["outreach_sent"] = n_sent

    # --- 2. geo_blitz: sends = attempts, track id -> channel ---
    n_geo = 0
    for row in _read_jsonl(BASE / "data/hustle/geo_blitz_campaign.jsonl"):
        if row.get("sent"):
            n_geo += 1
            ch("geo_blitz").attempts += 1
            tid = row.get("track")
            if tid:
                track_to_channel[tid] = "geo_blitz"
    sources_read["geo_blitz_sent"] = n_geo

    # --- 3. proof_bomb: staged sends = attempts (attributed via utm_source on funnel) ---
    n_pb = 0
    for row in _read_jsonl(BASE / "data/hustle/proof_bomb_staged.jsonl"):
        n_pb += 1
        ch("proof_bomb").attempts += 1
    sources_read["proof_bomb_staged"] = n_pb

    # --- 4. broken_site_rescue: sends = attempts ---
    n_bs = 0
    for row in _read_jsonl(BASE / "data/hustle/broken_site_sent.jsonl"):
        if row.get("ok"):
            n_bs += 1
            ch("broken_site_rescue").attempts += 1
    sources_read["broken_site_sent"] = n_bs

    # --- 5. intent replies (reddit/forum distribution): each post = an attempt ---
    posted = _read_json(BASE / "data/hustle/response_posted.json", {})
    n_intent = len(posted.get("posted", {})) if isinstance(posted, dict) else 0
    if n_intent:
        ch("intent_reply").attempts += n_intent
    sources_read["intent_posts"] = n_intent

    # --- 6. email opens: pixel hits, bot/proxy filtered, attributed via track_id ---
    n_open_rows = n_open_real = 0
    seen_open = set()
    for row in _read_jsonl(BASE / "products/landing/opens.jsonl"):
        n_open_rows += 1
        if not _real_open(row):
            continue
        tid = row.get("track_id")
        key = (tid, row.get("ip"))
        if key in seen_open:  # dedupe repeat opens of the same mail
            continue
        seen_open.add(key)
        n_open_real += 1
        chan = track_to_channel.get(tid, "email:unknown")
        ch(chan).actions["open"] += 1
    sources_read["open_rows"] = n_open_rows
    sources_read["open_real"] = n_open_real

    # --- 7. funnel events: human visits/clicks/captures attributed by utm_source ---
    n_funnel = n_funnel_human = 0
    seen_visit = set()
    for fpath in (
        BASE / "products/self_serve/funnel_events.jsonl",
        BASE / "products/landing/funnel_events.jsonl",
    ):
        for ev in _read_jsonl(fpath):
            n_funnel += 1
            c = classify(ev)
            kind = (ev.get("event") or "").lower()
            url = (ev.get("url") or "")
            src = c.get("source") or ""
            if kind in ("email_capture", "capture", "email") or kind.endswith("_email_capture"):
                # captures attribute to source if present, else the page's referrer/direct
                # (page-specific events: site_scan_email_capture / ai_visibility_email_capture)
                if c["is_human"] or src:
                    chan = src or "direct"
                    ch(chan, "pull" if not src or src in ("seo", "direct") else "push").actions["capture"] += 1
                    n_funnel_human += 1
                continue
            if url.startswith("/go/") or kind in ("go_click", "click", "cta_click"):
                if c["is_human"] and c["is_attributed"]:
                    ch(src, "pull" if src in ("seo",) else "push").actions["click"] += 1
                    n_funnel_human += 1
                continue
            if kind == "pageview" and c["is_human"] and c["is_attributed"]:
                vkey = (ev.get("sid"), src)
                if vkey in seen_visit:
                    continue
                seen_visit.add(vkey)
                ch(src, "pull" if src in ("seo",) else "push").actions["visit"] += 1
                n_funnel_human += 1
    sources_read["funnel_rows"] = n_funnel
    sources_read["funnel_human_attributed"] = n_funnel_human

    # --- 8. real inbound replies: human responders, bounce/auto-responder excluded ---
    NON_HUMAN_REPLY = {
        "bounce", "auto_response", "auto_reply", "out_of_office",
        "unsubscribe", "spam", "unknown",
    }
    n_reply_real = 0
    replies = _read_json(BASE / "products/outreach/replies.json", [])
    if isinstance(replies, list):
        for r in replies:
            if not isinstance(r, dict):
                continue
            if (r.get("classification") or "unknown").lower() in NON_HUMAN_REPLY:
                continue
            frm = str(r.get("from") or "").lower()
            if frm in OWNER_EMAILS or any(d in frm for d in SELF_SENDER_DOMAINS):
                continue  # our own outbound, not a prospect
            n_reply_real += 1
            chan = _seq_family(r.get("original_sequence", ""))
            ch(chan).actions["reply"] += 1
    sources_read["reply_real"] = n_reply_real

    # --- 9. Gumroad sales (owner/test excluded) ---
    n_gum = 0
    gum = _read_json(BASE / "data/hustle/gumroad_sales.json", {})
    for s in (gum.get("sales", []) if isinstance(gum, dict) else []):
        if s.get("refunded"):
            continue
        if str(s.get("email", "")).lower() in OWNER_EMAILS:
            continue
        n_gum += 1
        ch("gumroad").actions["sale"] += 1
    sources_read["gumroad_sales_real"] = n_gum

    # --- 10. Stripe checkouts + sales (read-only, all-time, owner/test excluded) ---
    stripe_summary = _stripe(channels, ch)
    sources_read["stripe"] = stripe_summary

    return channels, sources_read


def _stripe(channels, ch):
    key = os.environ.get("STRIPE_SECRET_KEY", "")
    if not key:
        return {"error": "no STRIPE_SECRET_KEY"}
    created = paid = 0
    params = {"limit": "100", "expand[]": "data.customer"}
    url = "https://api.stripe.com/v1/checkout/sessions?" + urllib.parse.urlencode(params, doseq=True)
    pages = 0
    try:
        while url and pages < 20:
            pages += 1
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"})
            data = json.loads(urllib.request.urlopen(req, timeout=30).read())
            rows = data.get("data", [])
            for s in rows:
                cd = s.get("customer_details") or {}
                email = (cd.get("email") or s.get("customer_email") or "").lower()
                is_paid = s.get("status") == "complete" or s.get("payment_status") == "paid"
                # A real-human checkout needs a real buyer email (or money on the table).
                # Unpaid sessions with placeholder/no email = scanner/test noise, skip.
                if not is_paid and not _real_buyer_email(email):
                    continue
                meta = s.get("metadata") or {}
                # funnel writes UTMs as attr_<key> (see app.py /api/checkout); read both forms
                chan = (meta.get("utm_source") or meta.get("attr_utm_source")
                        or meta.get("channel") or "stripe:direct")
                created += 1
                ch(chan).actions["checkout"] += 1
                if is_paid:
                    paid += 1
                    ch(chan).actions["sale"] += 1
            url = None
            if data.get("has_more") and rows:
                params["starting_after"] = rows[-1]["id"]
                url = "https://api.stripe.com/v1/checkout/sessions?" + urllib.parse.urlencode(params, doseq=True)
    except Exception as e:
        return {"error": str(e)[:140], "checkouts": created, "sales": paid}
    return {"checkouts": created, "sales": paid, "pages": pages}


def rank_and_recommend(channels: dict[str, Channel]):
    rows = []
    for c in channels.values():
        rows.append({
            "channel": c.name,
            "kind": c.kind,
            "attempts": c.attempts,
            "real_human_actions": c.total_actions,
            "by_action": {k: c.actions[k] for k in ACTION_TYPES if c.actions.get(k)},
            "yield_per_100": c.yield_per_100,
        })
    # Rank: real humans first, then yield (None last), then attempts desc.
    rows.sort(key=lambda r: (
        -r["real_human_actions"],
        -(r["yield_per_100"] if r["yield_per_100"] is not None else -1),
        -r["attempts"],
    ))

    actionable = [r for r in rows if r["attempts"] >= MIN_ATTEMPTS_FOR_VERDICT]
    winners = [r for r in actionable if r["real_human_actions"] > 0]
    double_down = None
    if winners:
        best = max(winners, key=lambda r: (r["yield_per_100"] or 0, r["real_human_actions"]))
        double_down = best["channel"]
    # Kill: most attempts spent for zero real humans.
    dead = sorted(
        [r for r in actionable if r["real_human_actions"] == 0],
        key=lambda r: -r["attempts"],
    )
    kill = [r["channel"] for r in dead[:3]]

    total_attempts = sum(c.attempts for c in channels.values())
    total_actions = sum(c.total_actions for c in channels.values())
    human_channels = [r["channel"] for r in rows if r["real_human_actions"] > 0]
    if double_down:
        note = (f"DOUBLE DOWN on {double_down} (best real-human yield). "
                f"KILL/redeploy: {', '.join(kill) if kill else 'none yet'}.")
    elif human_channels:
        note = (f"Real humans only on: {', '.join(human_channels)} — but these have no "
                f"counted attempts (organic/un-instrumented), so wire attribution there. "
                f"Meanwhile KILL the biggest zero-return spend: "
                f"{', '.join(kill) if kill else 'none'}.")
    elif total_attempts > 0:
        note = (f"HONEST BASELINE: {total_attempts} attempts, 0 real humans across all "
                f"channels. Nothing works yet — this is the zero we measure against. "
                f"Biggest spend with nothing back: {', '.join(kill) if kill else 'n/a'}.")
    else:
        note = "No attempts and no actions logged yet — nothing to rank."

    by_action_total = defaultdict(int)
    for c in channels.values():
        for k, v in c.actions.items():
            by_action_total[k] += v

    return rows, {
        "double_down": double_down,
        "kill": kill,
        "note": note,
    }, {
        "total_attempts": total_attempts,
        "total_real_human_actions": total_actions,
        "by_action": {k: by_action_total[k] for k in ACTION_TYPES if by_action_total.get(k)},
    }


def main():
    channels, sources_read = collect()
    rows, recommendation, totals = rank_and_recommend(channels)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window": "all_time",
        "totals": totals,
        "channels": rows,
        "recommendation": recommendation,
        "data_sources": sources_read,
    }
    # Funnel rates PAST "send" — read the unified ledger (data/funnel_events.jsonl) so the
    # brain sees conversion (visit→lead→checkout→paid), not just send/open counts. Compute
    # inline + persist for the dashboard; runs every cycle with this job (no extra cron).
    try:
        from tools.funnel_rates import compute as _fr_compute
        fr = _fr_compute(days=30)
        (BASE / "data/hustle/funnel_rates.json").write_text(json.dumps(fr, indent=2))
        payload["funnel_rates"] = fr.get("overall")
        payload["funnel_rates_by_brand"] = fr.get("by_brand")
    except Exception as e:
        payload["funnel_rates_error"] = str(e)[:140]
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=2))

    # Tight scoreboard.
    print("REVENUE SIGNAL — real-human yield by channel (all-time)")
    print(f"  attempts={totals['total_attempts']}  real-human actions="
          f"{totals['total_real_human_actions']}  {totals['by_action'] or '{}'}")
    print(f"  {'channel':28} {'attempts':>9} {'humans':>7} {'yld/100':>8}  actions")
    for r in rows:
        y = "n/a" if r["yield_per_100"] is None else f"{r['yield_per_100']:.2f}"
        acts = ",".join(f"{k}:{v}" for k, v in r["by_action"].items()) or "-"
        print(f"  {r['channel'][:28]:28} {r['attempts']:>9} "
              f"{r['real_human_actions']:>7} {y:>8}  {acts}")
    print(f"  -> {recommendation['note']}")
    print(f"  wrote {OUT}")


if __name__ == "__main__":
    main()
