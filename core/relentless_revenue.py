#!/usr/bin/env python3
"""Relentless revenue — the engine that NEVER STOPS.

One disciplined cycle (run_round) of the only motion that ever produced human signal:

  real harvested local business  ->  REAL AI-visibility scan (core.geo_visibility_probe)
        -> if it grades D/F        ->  build the FINISHED kit (core.geo_fix_kit) on disk
        -> send ONE personalized   ->  finished-proof email from the anon GEO brand scannerapp.dev
           proof email                  (CTA = scannerapp.dev/ai-visibility?...&utm_source=relentless)

This does NOT duplicate agents/proof_bomb.py — it reuses its building blocks (lead loading,
contact validation, market cache, bundle assembly) and wraps them in hard discipline:

  RAILS (all enforced here, every round):
    * kill-switch file  data/hustle/RELENTLESS_OFF   -> round no-ops
    * outbound gate     engine.outbound_sends_enabled -> fail-closed precheck before any send
    * daily cap         env RELENTLESS_DAILY_CAP (default 20), tracked in a per-day counter
    * per-round pace    env RELENTLESS_PER_ROUND (default 3) + business-hours send window
    * dedup             geo_blitz + engine sent.jsonl + suppressions + proof_bomb staged + own log
    * anonymity         sends ONLY from scannerapp.dev; never the owner/employer/sibling brands
    * snipe not spray   only D/F-scored businesses, each with a finished, personalized proof

Every attempt + outcome is appended to data/hustle/relentless_campaign.jsonl.

CLI:
    python3 -m core.relentless_revenue --dry      # scan + build + queue, NEVER send
    python3 -m core.relentless_revenue --round    # live, capped + paced
    python3 -m core.relentless_revenue --status   # show today's counter / rails
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(os.environ.get("CLAUDE_STACK_DIR", Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(ROOT))

HUSTLE = ROOT / "data" / "hustle"
CONTACTABLE = HUSTLE / "contactable_leads.jsonl"      # produced by agents/contact_enricher.py
INTENT_LEADS = HUSTLE / "intent_leads.jsonl"          # declared-intent harvest (optional)
CAMPAIGN_LOG = HUSTLE / "relentless_campaign.jsonl"   # every attempt + outcome
DAILY_COUNTER = HUSTLE / "relentless_daily_counter.json"
KILL = HUSTLE / "RELENTLESS_OFF"

CLAIM_BASE = "https://scannerapp.dev/ai-visibility"

# Sender identity — anonymity rail: the GEO brand ONLY. Never the owner/employer/sibling brands.
FROM_EMAIL = os.environ.get("RELENTLESS_FROM_EMAIL", "user@example.com")
FROM_NAME = os.environ.get("RELENTLESS_FROM_NAME", "scannerapp")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except Exception:
        return default


def _truthy(name: str, default: str = "1") -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


# ── lead loading (contactable_leads from enricher + intent + the proof_bomb local-biz pool) ──
def _load_jsonl(path: Path) -> list[dict]:
    out: list[dict] = []
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def _normalize(lead: dict) -> dict | None:
    """Map any lead shape (enricher / intent / local-biz) to {name,city,category,email,website}."""
    name = (lead.get("name") or lead.get("company") or lead.get("business") or "").strip()
    city = (lead.get("city") or lead.get("location") or "").strip()
    cat = (lead.get("category") or lead.get("segment") or lead.get("market") or "").strip()
    email = (lead.get("email") or lead.get("contact_email") or lead.get("to") or "").strip()
    website = (lead.get("website") or lead.get("domain") or "").strip()
    if not (name and city and cat):
        return None
    if len(name) > 70 or name.lower().startswith(("the best", "top ", "best ")):
        return None  # listicle/aggregator title, not a real business
    return {"name": name, "city": city, "category": cat, "email": email,
            "website": website, "handle": lead.get("handle", ""),
            "source": lead.get("source", "relentless")}


def load_leads() -> list[dict]:
    """Fresh contactable leads (enricher) + intent leads, then the proof_bomb local-biz pool —
    deduped by (name, city). Enricher/intent leads (with verified emails) come first."""
    from agents import proof_bomb  # reuse its validated local-biz loader; do not duplicate
    rows: list[dict] = []
    for src in (CONTACTABLE, INTENT_LEADS):
        for d in _load_jsonl(src):
            n = _normalize(d)
            if n:
                rows.append(n)
    for d in proof_bomb.load_leads():
        n = _normalize(d)
        if n:
            rows.append(n)
    seen: set[tuple] = set()
    deduped: list[dict] = []
    for r in rows:
        key = (r["name"].lower(), r["city"].lower())
        if key in seen:
            continue
        seen.add(key)
        deduped.append(r)
    return deduped


def _own_sent_emails() -> set[str]:
    out: set[str] = set()
    for d in _load_jsonl(CAMPAIGN_LOG):
        if d.get("action") == "sent" and d.get("email"):
            out.add(d["email"].strip().lower())
    return out


def _daily_counter() -> dict:
    try:
        return json.loads(DAILY_COUNTER.read_text()) if DAILY_COUNTER.exists() else {}
    except Exception:
        return {}


def _bump_daily() -> int:
    c = _daily_counter()
    t = _today()
    c[t] = int(c.get(t, 0)) + 1
    DAILY_COUNTER.parent.mkdir(parents=True, exist_ok=True)
    DAILY_COUNTER.write_text(json.dumps(c, indent=2))
    return c[t]


def _log(entry: dict) -> None:
    entry = {"ts": _now(), **entry}
    CAMPAIGN_LOG.parent.mkdir(parents=True, exist_ok=True)
    with CAMPAIGN_LOG.open("a") as f:
        f.write(json.dumps(entry) + "\n")


def _claim_link(business: str, city: str = "", category: str = "", variant: str = "") -> str:
    # MUST carry city+category: the /ai-visibility page only renders the finished proof when all
    # three are present; business-only lands the click on a blank "type your city" form (the bug
    # that silently broke proof-first). unlocked=1 skips the email gate (we already have them).
    params = {"business": business, "unlocked": "1",
              "utm_source": "relentless", "utm_medium": "email", "utm_campaign": "ai_visibility"}
    if city:
        params["city"] = city
    if category:
        params["category"] = category
    if variant:
        params["utm_content"] = variant   # A/B/C variant -> signal brain attributes conversions per hook
    return CLAIM_BASE + "?" + urllib.parse.urlencode(params)


def run_round(dry: bool = False, *, verbose: bool = False) -> dict:
    """Execute ONE disciplined cycle. Returns a summary; never raises on a single bad lead."""
    from agents import proof_bomb
    from core.geo_visibility_probe import score_business
    from core import geo_fix_kit

    started = time.time()
    daily_cap = _env_int("RELENTLESS_DAILY_CAP", 20)
    per_round = _env_int("RELENTLESS_PER_ROUND", 3)
    new_market_budget = _env_int("RELENTLESS_NEW_MARKET_BUDGET", 0 if dry else 2)
    respect_window = _truthy("RELENTLESS_RESPECT_WINDOW", "1")

    summary = {
        "mode": "dry" if dry else "live", "ts": _now(),
        "leads_loaded": 0, "scanned": 0, "df_total": 0, "df_no_email": 0,
        "graded_pass": 0, "kits_built": 0, "queued": 0, "sent": 0,
        "skipped_already": 0, "new_markets_used": 0,
        "daily_cap": daily_cap, "per_round": per_round,
        "sent_today_before": int(_daily_counter().get(_today(), 0)),
        "status": "ok", "gate_reason": "", "elapsed_s": 0.0,
    }

    # ── RAIL 1: kill-switch ──────────────────────────────────────────────────
    # Hard block on LIVE sends. Dry runs (which send NOTHING) are allowed to proceed so the
    # pipeline can still be verified while held — matching the hold's intent ("before any live send").
    kill_present = KILL.exists()
    summary["kill_switch"] = kill_present
    if kill_present and not dry:
        summary["status"] = "killed"
        summary["gate_reason"] = f"kill-switch present: {KILL}"
        summary["elapsed_s"] = round(time.time() - started, 2)
        _log({"action": "killed", "reason": "RELENTLESS_OFF present"})
        return summary

    # ── RAIL 2: outbound gate precheck (fail-closed) ─ live only ─────────────
    from products.outreach import engine
    if not dry:
        enabled, reason = engine.outbound_sends_enabled("relentless_revenue", recipient_email="")
        summary["gate_reason"] = reason
        if not enabled:
            summary["status"] = "gate_closed"
            summary["elapsed_s"] = round(time.time() - started, 2)
            _log({"action": "gate_closed", "reason": reason})
            return summary

    # ── RAIL 3: business-hours send window (pacing) ─ live only ──────────────
    if not dry and respect_window and not engine._in_send_window():
        summary["status"] = "window_closed"
        summary["gate_reason"] = "outside send window; next: " + engine._next_send_window()
        summary["elapsed_s"] = round(time.time() - started, 2)
        _log({"action": "window_closed", "next": engine._next_send_window()})
        return summary

    # ── RAIL 4: daily cap ────────────────────────────────────────────────────
    sent_today = int(_daily_counter().get(_today(), 0))
    remaining_daily = daily_cap - sent_today
    if not dry and remaining_daily <= 0:
        summary["status"] = "daily_cap_reached"
        summary["elapsed_s"] = round(time.time() - started, 2)
        _log({"action": "daily_cap_reached", "sent_today": sent_today, "cap": daily_cap})
        return summary

    # queue budget for THIS round
    if dry:
        queue_budget = daily_cap            # show how many D/F we could queue up to the cap
    else:
        queue_budget = min(per_round, remaining_daily)

    # ── anonymity rail: force the GEO-brand sender for every send this round ──
    engine.FROM_EMAIL = FROM_EMAIL
    engine.FROM_NAME = FROM_NAME

    # ── load + dedup ─────────────────────────────────────────────────────────
    leads = load_leads()
    summary["leads_loaded"] = len(leads)
    contacted = proof_bomb._already_contacted() | _own_sent_emails()
    cached = proof_bomb._cached_markets()

    # snipe order: contactable + cached-market first (instant, $0, real)
    def rank(l: dict):
        em = l.get("email", "").strip()
        web = l.get("website", "").strip()
        ok = bool(em) and proof_bomb._is_real_business_email(em, web) and em.lower() not in contacted
        is_cached = proof_bomb._market_slug(l["category"], l["city"]) in cached
        return (0 if ok else 1, 0 if is_cached else 1)

    leads.sort(key=rank)

    new_markets_used = 0
    for lead in leads:
        if summary["queued"] >= queue_budget:
            break
        email = lead.get("email", "").strip().lower()
        website = lead.get("website", "").strip()
        contactable = bool(email) and proof_bomb._is_real_business_email(email, website)

        if contactable and email in contacted:
            summary["skipped_already"] += 1
            continue

        slug = proof_bomb._market_slug(lead["category"], lead["city"])
        is_cached = slug in cached
        if not is_cached:
            if new_markets_used >= new_market_budget:
                continue  # bound runtime / stay $0; never fabricate a grade
            new_markets_used += 1

        try:
            scan = score_business(lead["name"], lead["city"], lead["category"])
        except Exception as e:
            sys.stderr.write(f"[scan-fail] {lead['name']}: {type(e).__name__}: {e}\n")
            continue
        summary["scanned"] += 1

        if scan["grade"] not in ("D", "F"):
            summary["graded_pass"] += 1
            continue  # already AI-visible — proof-first has nothing to sell them

        summary["df_total"] += 1
        competitors = [c["name"] for c in scan.get("leaderboard", [])][:3]
        variant = proof_bomb.variant_for(lead["name"])          # stable A/B/C hook per business
        claim = _claim_link(lead["name"], lead["city"], lead["category"], variant)

        # finished proof on disk (the kit). Skip the site scrape in dry to stay fast/offline.
        kit = None
        try:
            kit = geo_fix_kit.generate_kit(
                lead["name"], lead["city"], lead["category"],
                domain=("" if dry else proof_bomb._registrable(website) if website else ""),
                scan=scan, watermark=True, checkout_url=claim,
            )
            summary["kits_built"] += 1
        except Exception as e:
            sys.stderr.write(f"[kit-fail] {lead['name']}: {type(e).__name__}: {e}\n")

        if not contactable:
            summary["df_no_email"] += 1
            _log({"action": "df_no_email", "business": lead["name"], "city": lead["city"],
                  "category": lead["category"], "grade": scan["grade"],
                  "kit_path": (kit or {}).get("path"), "channel": "social_or_none"})
            continue  # finished proof exists; the gap is the channel, not the work

        message = proof_bomb.build_message(
            lead["name"], lead["city"], lead["category"], scan["grade"], competitors, claim,
            scan.get("sources"), variant)
        subject = proof_bomb.build_subject(lead["name"], scan["grade"], variant)

        base_rec = {
            "business": lead["name"], "city": lead["city"], "category": lead["category"],
            "grade": scan["grade"], "visibility_score": scan["visibility_score"],
            "appearances": scan["appearances"], "prompts_run": scan["prompts_run"],
            "competitors": competitors, "email": email, "from": FROM_EMAIL,
            "claim_link": claim, "kit_path": (kit or {}).get("path"),
            "subject": subject, "variant": variant, "source": lead.get("source"),
        }

        if dry:
            summary["queued"] += 1
            _log({"action": "dry_queue", **base_rec})
            if verbose:
                print(f"[dry] would-send -> {email}  {lead['name']} ({scan['grade']}) "
                      f"{lead['category']} / {lead['city']}")
            continue

        # ── LIVE send (engine applies its OWN final gate + suppression + bounce guard) ──
        res = engine.send_email(email, subject, message, from_name=FROM_NAME)
        if res:
            n_today = _bump_daily()
            contacted.add(email)
            summary["queued"] += 1
            summary["sent"] += 1
            summary["sent_today_after"] = n_today
            _log({"action": "sent", "track": res if isinstance(res, str) else None,
                  "sent_today": n_today, **base_rec})
        else:
            _log({"action": "send_failed", "reason": "engine refused (gate/suppress/bounce)",
                  **base_rec})

    summary["new_markets_used"] = new_markets_used
    summary["sent_today_after"] = int(_daily_counter().get(_today(), 0))
    summary["remaining_daily"] = daily_cap - summary["sent_today_after"]
    summary["elapsed_s"] = round(time.time() - started, 2)
    if not dry:
        _log({"action": "round_summary", **{k: summary[k] for k in
              ("scanned", "df_total", "queued", "sent", "graded_pass", "kits_built",
               "skipped_already", "sent_today_after", "remaining_daily")}})
    return summary


def status() -> dict:
    c = _daily_counter()
    return {
        "kill_switch": KILL.exists(), "kill_file": str(KILL),
        "daily_cap": _env_int("RELENTLESS_DAILY_CAP", 20),
        "per_round": _env_int("RELENTLESS_PER_ROUND", 3),
        "sent_today": int(c.get(_today(), 0)),
        "from": FROM_EMAIL, "from_name": FROM_NAME,
        "campaign_log": str(CAMPAIGN_LOG),
        "log_lines": len(_load_jsonl(CAMPAIGN_LOG)),
        "contactable_leads_present": CONTACTABLE.exists(),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Relentless revenue engine (never stops).")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--round", action="store_true", help="live, capped + paced disciplined round")
    g.add_argument("--dry", action="store_true", help="scan + build + queue, NEVER send")
    g.add_argument("--status", action="store_true", help="show counter / rails")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    if args.status:
        print(json.dumps(status(), indent=2))
        return 0
    res = run_round(dry=args.dry, verbose=args.verbose)
    print(json.dumps(res, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
