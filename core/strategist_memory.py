#!/usr/bin/env python3
"""strategist_memory.py — memory + big-bet routing for the AUTONOMOUS STRATEGIST.

The strategist is allowed to INITIATE, but only safely. This module is the
learning ledger + the safe/unsafe split:

  * record(play, outcome) / recent_plays(n) / outcome_for(play) over
    data/hustle/strategist_memory.jsonl — so the loop DEDUPS and LEARNS
    (stop repeating plays that produced nothing; double down on what worked).
  * outcome_for(play) JOINS each play to REAL downstream data:
      - harvest_query -> did it produce leads?      (multi_market_leads.json)
      - copy_variant  -> is the bandit testing it + capture rate
                                                      (variant_optimizer.jsonl + tools.variant_report)
      - bofu_page     -> was the page actually BUILT? (data/hustle/seo_pages/<slug>.html)
      - routed bets   -> owner's GO/NO               (core.approval_ledger)
    A "strategy report" nobody consumes is theater — every play here either
    feeds a LIVE consumer (a harvester / the bandit / the SEO factory) or is a
    proposal routed to the owner. No consumer => not a play.

  * launch(play) — AUTO-LAUNCH only the cheap + reversible + lane-safe +
    no-identity-risk types (harvest_query / copy_variant / bofu_page). It writes
    to the EXISTING consumer's input so a live component picks it up:
      harvest_query -> data/hustle/strategist_harvest_queue.json  (read by
                       agents/multi_market_intent.py:scan())
      copy_variant  -> data/hustle/variant_bank_extra.json        (read by
                       tools/variant_report.py:VARIANTS -> the bandit weights it)
      bofu_page     -> keyword_gap.json["assignments"]["programmatic_seo"] (read
                       by agents/programmatic_seo.py --gap)
    Every launch records an UNDO recipe and is reversible.

  * route_to_owner(play) — everything else (new OFFERS, PRICING, new CHANNELS,
    anything spending money, anything under BrandA's named identity or an
    identity marketplace, anything irreversible) is DRAFTED and pushed into the
    SAME fire-queue/approval path the fire_approval_runner already texts
    (fire_queue_pending.jsonl + core.approval_ledger), so Operator gets a
    'new play proposed — GO/NO' text and approves a big bet exactly like a
    warm-demand fire. These NEVER auto-launch.

Guardrails: faceless brands stay anon + un-correlated; BrandA is employment-
screened by the runner; NO fabricated facts/stats in generated copy (FTC); lane
separation is absolute. AUTO-LAUNCH is default-OFF until armed
(data/hustle/STRATEGIST_ARMED) — un-armed, even safe plays route to the owner.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(os.environ.get("CLAUDE_STACK_DIR", Path(__file__).resolve().parents[1]))
HUSTLE = ROOT / "data" / "hustle"

MEMORY = HUSTLE / "strategist_memory.jsonl"

# --- consumer inputs (the proof a LIVE component picks the play up) ----------
HARVEST_QUEUE = HUSTLE / "strategist_harvest_queue.json"      # multi_market_intent.scan()
VARIANT_BANK_EXTRA = HUSTLE / "variant_bank_extra.json"       # tools.variant_report.VARIANTS -> bandit
KEYWORD_GAP = HUSTLE / "keyword_gap.json"                     # programmatic_seo.py --gap
SEO_PAGES = HUSTLE / "seo_pages"                              # programmatic_seo build output
MULTI_MARKET_LEADS = HUSTLE / "multi_market_leads.json"       # harvest outcome
VARIANT_OPTIMIZER_LOG = HUSTLE / "variant_optimizer.jsonl"    # bandit outcome
STRATEGIST_POLICY = HUSTLE / "strategist_policy.json"         # LEARNED per-(type,lane) effectiveness
REVENUE_SCOREBOARD = HUSTLE / "revenue_scoreboard.jsonl"      # per-channel real metrics
FIRE_QUEUE = HUSTLE / "fire_queue_pending.jsonl"              # owner approval texter

# --- arming / kill -----------------------------------------------------------
ARMED_FLAG = HUSTLE / "STRATEGIST_ARMED"   # absent => auto-launch OFF (route instead)
OFF_FLAG = HUSTLE / "STRATEGIST_OFF"       # present => do nothing at all

# Types the strategist may AUTO-LAUNCH (cheap, reversible, lane-safe, no identity risk).
AUTO_SAFE_TYPES = {"harvest_query", "copy_variant", "bofu_page"}

# Anything matching these ALWAYS routes to the owner, never auto.
BIG_BET_TYPES = {
    "new_offer", "pricing_change", "new_channel", "spend", "ad_spend",
    "partnership", "maroon_play", "identity_marketplace", "rebrand", "hire",
}

# Lanes that carry a real/named identity -> always owner-routed.
NAMED_OR_IDENTITY_LANES = {"BrandA", "branda", "Operator", "CompanyA"}
IDENTITY_MARKETPLACE_CHANNELS = {"upwork", "freelancer", "rfp", "rfp_email", "clutch", "fiverr"}

# Conservative FTC fabrication tripwire: hard numeric/stat/superlative claims in
# generated copy can't be auto-shipped (we can't prove them) -> route for human eyes.
_FABRICATION_PATTERNS = [
    re.compile(r"\b\d{1,3}\s?%"),                      # "73%"
    re.compile(r"\b\d{2,}\s?(customers|clients|users|companies|reviews|stars)\b", re.I),
    re.compile(r"\b(guarantee|guaranteed|proven|#1|number one|best[- ]rated|award[- ]winning)\b", re.I),
    re.compile(r"\$\s?\d[\d,]*\s?(saved|revenue|in sales|profit)\b", re.I),
    re.compile(r"\b\d+x\b"),                           # "10x"
]


# --------------------------------------------------------------------------- util
def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_ts(s: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:
        return None


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(row, sort_keys=True) + "\n")


def _read_json(path: Path, default: Any) -> Any:
    try:
        if path.exists():
            return json.loads(path.read_text())
    except Exception:
        pass
    return default


def _slug(q: str) -> str:
    # MUST match agents/programmatic_seo.py:slug so the built-page join lines up.
    return re.sub(r"[^a-z0-9]+", "-", q.lower()).strip("-")[:60]


# --------------------------------------------------------------------------- play identity
def _sig_fields(play: dict[str, Any]) -> dict[str, Any]:
    """The fields that make two plays 'the same' for dedup. Type + the load-bearing param."""
    p = play.get("params", {}) or {}
    t = play.get("type", "")
    key = ""
    if t == "harvest_query":
        key = (p.get("query") or p.get("subreddit") or "").strip().lower()
    elif t == "copy_variant":
        key = (p.get("variant_id") or p.get("id") or "").strip().lower()
    elif t == "bofu_page":
        key = _slug(p.get("query") or p.get("keyword") or "")
    else:
        key = json.dumps(p, sort_keys=True, default=str).lower()
    return {"type": t, "key": key}


def play_id(play: dict[str, Any]) -> str:
    seed = json.dumps(_sig_fields(play), sort_keys=True)
    return hashlib.sha256(seed.encode()).hexdigest()[:16]


def normalize_play(play: dict[str, Any]) -> dict[str, Any]:
    pl = dict(play)
    pl.setdefault("params", {})
    pl.setdefault("lane", "faceless")
    pl["type"] = pl.get("type", "")
    pl["id"] = play_id(pl)
    return pl


# --------------------------------------------------------------------------- guardrails
def _copy_text(play: dict[str, Any]) -> str:
    p = play.get("params", {}) or {}
    return " ".join(str(p.get(k, "")) for k in ("text", "subject", "body", "draft", "copy", "headline"))


def fabrication_flags(play: dict[str, Any]) -> list[str]:
    text = _copy_text(play)
    hits = []
    for rx in _FABRICATION_PATTERNS:
        m = rx.search(text)
        if m:
            hits.append(m.group(0))
    return hits


def classify(play: dict[str, Any]) -> dict[str, Any]:
    """Decide auto-launch vs owner-route, with the reason. Conservative: when in
    doubt, ROUTE. Returns {decision: 'auto'|'route', reason, fabrication}."""
    t = play.get("type", "")
    lane = str(play.get("lane", "")).lower()
    channel = str((play.get("params") or {}).get("channel", "")).lower()
    fab = fabrication_flags(play)

    if t in BIG_BET_TYPES:
        return {"decision": "route", "reason": f"big-bet type '{t}'", "fabrication": fab}
    if lane in NAMED_OR_IDENTITY_LANES:
        return {"decision": "route", "reason": f"named/identity lane '{lane}'", "fabrication": fab}
    if channel in IDENTITY_MARKETPLACE_CHANNELS:
        return {"decision": "route", "reason": f"identity marketplace '{channel}'", "fabrication": fab}
    if play.get("params", {}).get("spend_usd"):
        return {"decision": "route", "reason": "spends money", "fabrication": fab}
    if t not in AUTO_SAFE_TYPES:
        return {"decision": "route", "reason": f"type '{t}' not in auto-safe set", "fabrication": fab}
    if fab:
        return {"decision": "route", "reason": f"possible fabricated claim {fab} (FTC) — needs human ok",
                "fabrication": fab}
    return {"decision": "auto", "reason": "cheap + reversible + lane-safe + no identity risk",
            "fabrication": fab}


# --------------------------------------------------------------------------- memory: record / read
def record(play: dict[str, Any], outcome: dict[str, Any] | None = None,
           *, event: str = "play", **extra: Any) -> dict[str, Any]:
    """Append a memory event. event='play' logs a launched/routed/proposed play;
    event='outcome' attaches an outcome to an existing play id."""
    pl = normalize_play(play)
    row = {
        "ts": _now(),
        "event": event,
        "id": pl["id"],
        "type": pl["type"],
        "lane": pl.get("lane"),
        "params": pl.get("params", {}),
        "status": pl.get("status", event),
        "auto": bool(pl.get("auto", False)),
        "undo": pl.get("undo"),
        "outcome": outcome,
    }
    row.update(extra)
    _append_jsonl(MEMORY, row)
    return row


def _all_events() -> list[dict[str, Any]]:
    if not MEMORY.exists():
        return []
    out = []
    for line in MEMORY.read_text(errors="ignore").splitlines():
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def recent_plays(n: int = 50) -> list[dict[str, Any]]:
    """Newest-first, ONE folded record per play id (latest play-event merged with
    its latest attached outcome). Lets the loop DEDUP + LEARN, not just log."""
    state: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for ev in _all_events():
        pid = ev.get("id")
        if not pid:
            continue
        if ev.get("event") == "outcome":
            if pid in state:
                state[pid]["outcome"] = ev.get("outcome")
                state[pid]["outcome_ts"] = ev.get("ts")
            continue
        if pid not in state:
            order.append(pid)
        rec = dict(ev)
        # preserve an already-attached outcome if the new play-event lacks one
        if state.get(pid) and not rec.get("outcome"):
            rec["outcome"] = state[pid].get("outcome")
            rec["outcome_ts"] = state[pid].get("outcome_ts")
        state[pid] = rec
    folded = [state[pid] for pid in order]
    folded.sort(key=lambda r: r.get("ts", ""), reverse=True)
    return folded[:n]


def already_played(play: dict[str, Any]) -> dict[str, Any] | None:
    """Dedup hook: has this exact play (type+key) been launched/routed before?
    Returns its folded record (with outcome) so the loop can decide to skip or
    double down. None if never seen."""
    pid = play_id(play)
    for rec in recent_plays(n=10_000):
        if rec.get("id") == pid:
            return rec
    return None


# --------------------------------------------------------------------------- OUTCOME JOINS (real data)
def outcome_for(play: dict[str, Any]) -> dict[str, Any]:
    """Join a play to REAL downstream data and return its outcome. This is what
    makes the loop learn: a play that produced nothing reads back 'dead'."""
    t = play.get("type", "")
    return {
        "harvest_query": _outcome_harvest,
        "copy_variant": _outcome_variant,
        "bofu_page": _outcome_bofu,
        "code_edit": _outcome_code_edit,
        "effort_reallocation": _outcome_effort_reallocation,
    }.get(t, _outcome_routed)(play)   # offer_test / new_channel / route_owner -> owner-decision


def _outcome_harvest(play: dict[str, Any]) -> dict[str, Any]:
    """Did the harvested query/subreddit produce leads? Join multi_market_leads.json
    by market/subreddit + found_at >= when we launched it."""
    p = play.get("params", {}) or {}
    market = (p.get("market") or "").strip()
    sub = (p.get("subreddit") or "").strip().lower()
    since = _parse_ts(p.get("launched_at") or play.get("ts"))
    data = _read_json(MULTI_MARKET_LEADS, {})
    leads = data.get("leads", data) if isinstance(data, dict) else data
    leads = leads if isinstance(leads, list) else []
    matched = []
    for l in leads:
        if not isinstance(l, dict):
            continue
        fa = _parse_ts(l.get("found_at"))
        if since and fa and fa < since:
            continue
        src = str(l.get("source", "")).lower()
        mk = str(l.get("market", "")).lower()
        if (market and mk == market.lower()) or (sub and f"r/{sub}" in src):
            matched.append(l)
    produced = len(matched)
    return {
        "metric": "produced_leads",
        "produced_leads": produced,
        "sample_urls": [m.get("url") for m in matched[:3]],
        "verdict": "productive" if produced else "dead",
        "joined_from": MULTI_MARKET_LEADS.name,
    }


def _outcome_variant(play: dict[str, Any]) -> dict[str, Any]:
    """Is the bandit actually testing this variant, and what's its capture rate?
    Join the variant bandit's log + the live tally."""
    p = play.get("params", {}) or {}
    vid = (p.get("variant_id") or p.get("id") or "").strip().lower()
    weight = None
    last = None
    if VARIANT_OPTIMIZER_LOG.exists():
        for line in VARIANT_OPTIMIZER_LOG.read_text(errors="ignore").splitlines():
            try:
                last = json.loads(line)
            except Exception:
                continue
        if last:
            weight = (last.get("weights") or {}).get(vid)
    sent = capture = 0
    try:
        import sys
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        from tools.variant_report import compute_tally
        tally = compute_tally()
        if vid in tally:
            sent = tally[vid].get("sent", 0)
            capture = tally[vid].get("capture", 0)
    except Exception:
        pass
    tested = weight is not None
    rate = (capture / sent) if sent else 0.0
    return {
        "metric": "reply_rate",
        "in_bandit": tested,
        "bandit_weight": weight,
        "sent": sent,
        "captures": capture,
        "reply_rate": round(rate, 4),
        "verdict": ("testing" if tested and sent == 0 else
                    "converting" if capture else
                    "dead" if sent else "queued"),
        "joined_from": f"{VARIANT_OPTIMIZER_LOG.name} + tools.variant_report",
    }


def _outcome_bofu(play: dict[str, Any]) -> dict[str, Any]:
    """Was the BOFU/SEO page actually BUILT by the SEO factory? Join the build output dir."""
    p = play.get("params", {}) or {}
    slug = _slug(p.get("query") or p.get("keyword") or "")
    page = SEO_PAGES / f"{slug}.html"
    built = page.exists()
    queued = False
    gap = _read_json(KEYWORD_GAP, {})
    assigns = (gap.get("assignments") or {}).get("programmatic_seo") or []
    queued = any(_slug(q if isinstance(q, str) else q.get("query", "")) == slug for q in assigns)
    return {
        "metric": "page_built",
        "built": built,
        "queued": queued,
        "path": str(page) if built else None,
        "bytes": page.stat().st_size if built else 0,
        "verdict": "built" if built else ("queued" if queued else "missing"),
        "joined_from": f"{SEO_PAGES.name}/ + {KEYWORD_GAP.name}",
    }


def _outcome_routed(play: dict[str, Any]) -> dict[str, Any]:
    """Routed big-bets: the outcome is the owner's GO/NO, read from the ledger."""
    aid = (play.get("params", {}) or {}).get("approval_id") or play.get("approval_id")
    status = "pending"
    if aid:
        try:
            import sys
            if str(ROOT) not in sys.path:
                sys.path.insert(0, str(ROOT))
            from core import approval_ledger
            act = approval_ledger.get_action(aid)
            if act:
                status = act.get("status", "pending")
        except Exception:
            pass
    return {
        "metric": "owner_decision",
        "approval_id": aid,
        "ledger_status": status,
        "verdict": {"executed": "approved", "approved": "approved",
                    "rejected": "rejected"}.get(status, "awaiting_owner"),
        "joined_from": "core.approval_ledger",
    }


def _outcome_code_edit(play: dict[str, Any]) -> dict[str, Any]:
    """Did the self-coded edit STICK? The actuator recorded apply/verify/rollback on the play's
    decision; a reverted edit reads 'reverted' (bad), a verified one 'verified' (good)."""
    dec = play.get("outcome") or {}
    d = str(dec.get("decision") or dec.get("action") or "").lower()
    verified = dec.get("verified")
    ok = bool((dec.get("result") or {}).get("ok"))
    verdict = ("reverted" if ("roll" in d or "revert" in d) else
               "verified" if verified is True else
               "applied" if (ok or "appl" in d) else
               "unverified" if verified is False else "dead")
    return {"metric": "code_stuck", "decision": d or None, "verified": verified,
            "verdict": verdict, "joined_from": "play decision + code_actuator"}


def _outcome_effort_reallocation(play: dict[str, Any]) -> dict[str, Any]:
    """Did shifting effort to this channel move its metric? Join the real per-channel value from the
    revenue scoreboard AFTER the reallocation. Honest 'unmeasured' if no source yet."""
    p = play.get("params", {}) or {}
    channel = str(p.get("channel") or "").strip().lower()
    metric = str(p.get("metric") or "").strip()
    since = _parse_ts(play.get("ts"))
    val, seen = None, False
    if REVENUE_SCOREBOARD.exists():
        for line in REVENUE_SCOREBOARD.read_text(errors="ignore").splitlines():
            try:
                row = json.loads(line)
            except Exception:
                continue
            if str(row.get("channel", row.get("lane", ""))).lower() != channel:
                continue
            rts = _parse_ts(row.get("ts"))
            if since and rts and rts < since:
                continue
            v = row.get(metric, row.get("outcome", row.get("value")))
            if isinstance(v, (int, float)):
                val = (val or 0) + v
                seen = True
    verdict = ("moved" if (val or 0) > 0 else "flat") if seen else "unmeasured"
    return {"metric": "effort_effect", "channel": channel, "tracked_metric": metric,
            "value_since": val, "verdict": verdict, "joined_from": REVENUE_SCOREBOARD.name}


# non-terminal verdicts RE-JOIN each sweep so an outcome MATURES (queued->converting, pending->approved)
_TERMINAL_VERDICTS = {"dead", "productive", "converting", "built", "verified", "reverted",
                      "approved", "rejected", "missing", "moved", "flat"}


def attach_outcomes(min_age_minutes: int = 0) -> list[dict[str, Any]]:
    """Join every matured play to its REAL downstream outcome. A play logged with only a launch
    DECISION ({'decision': ...}, no 'metric') is NOT considered joined — that was the bug that pinned
    outcomes_joined at 0. Skip only plays already carrying a TERMINAL metric outcome."""
    cutoff = datetime.now(timezone.utc).timestamp() - (min_age_minutes * 60)
    written = []
    for rec in recent_plays(n=10_000):
        oc = rec.get("outcome") or {}
        if isinstance(oc, dict) and oc.get("metric") and oc.get("verdict") in _TERMINAL_VERDICTS:
            continue  # already has a real, settled outcome
        ts = _parse_ts(rec.get("ts"))
        if ts and ts.timestamp() > cutoff:
            continue
        joined = outcome_for(rec)
        written.append(record(rec, outcome=joined, event="outcome"))
    return written


# --------------------------------------------------------------------------- LEARN (join -> policy)
_GOOD_VERDICTS = {"productive", "converting", "built", "verified", "approved", "moved"}
_BAD_VERDICTS = {"dead", "reverted", "rejected", "missing", "flat", "unverified"}


def learn() -> dict[str, Any]:
    """THE learn step: aggregate joined real outcomes into a per-(type,lane) effectiveness policy the
    diagnoser reads to favor what works and skip what's proven dead. Turns joins into behavior."""
    stats: dict[str, dict[str, Any]] = {}
    for rec in recent_plays(n=10_000):
        oc = rec.get("outcome") or {}
        if not (isinstance(oc, dict) and oc.get("metric")):
            continue  # only REAL outcomes count, never launch decisions
        key = f"{rec.get('type')}::{rec.get('lane')}"
        st = stats.setdefault(key, {"type": rec.get("type"), "lane": rec.get("lane"),
                                    "n": 0, "good": 0, "bad": 0})
        st["n"] += 1
        v = oc.get("verdict")
        if v in _GOOD_VERDICTS:
            st["good"] += 1
        elif v in _BAD_VERDICTS:
            st["bad"] += 1
    for st in stats.values():
        st["score"] = round((st["good"] - st["bad"]) / st["n"], 3) if st["n"] else 0.0
        st["weight"] = round(max(0.1, 1.0 + st["score"]), 3)   # 0.1 (dead) .. 2.0 (proven)
    pol = {"updated": _now(), "n_outcomes": sum(s["n"] for s in stats.values()), "by_key": stats}
    _atomic_write(STRATEGIST_POLICY, json.dumps(pol, indent=2))
    return pol


def policy_weight(play_type: str, lane: Any) -> float:
    """Learned selection weight for a (type,lane): 1.0 unknown, <1 tends-dead, >1 proven-productive."""
    try:
        st = (_read_json(STRATEGIST_POLICY, {}).get("by_key") or {}).get(f"{play_type}::{lane}")
        return float(st.get("weight", 1.0)) if st else 1.0
    except Exception:
        return 1.0


def is_dead_play(play_type: str, lane: Any, min_n: int = 3) -> bool:
    """True once a (type,lane) has a PROVEN-dead record (>=min_n real outcomes, score<=-0.67)."""
    try:
        st = (_read_json(STRATEGIST_POLICY, {}).get("by_key") or {}).get(f"{play_type}::{lane}")
        return bool(st and st.get("n", 0) >= min_n and st.get("score", 0.0) <= -0.67)
    except Exception:
        return False


# --------------------------------------------------------------------------- arming
def is_off() -> bool:
    return OFF_FLAG.exists()


def is_armed() -> bool:
    return ARMED_FLAG.exists() and not OFF_FLAG.exists()


# --------------------------------------------------------------------------- AUTO-LAUNCH (safe types)
def launch(play: dict[str, Any]) -> dict[str, Any]:
    """Auto-launch a cheap/reversible/lane-safe play by writing to the EXISTING
    consumer's input so a LIVE component picks it up. Records an UNDO recipe.

    Refuses (routes instead) if the play isn't auto-safe or the strategist isn't
    armed. Returns the recorded memory row."""
    if is_off():
        return {"refused": "STRATEGIST_OFF present"}
    verdict = classify(play)
    if verdict["decision"] != "auto":
        return route_to_owner(play, reason=verdict["reason"])
    if not is_armed():
        return route_to_owner(play, reason="auto-launch disarmed (no STRATEGIST_ARMED) — owner-gated")

    pl = normalize_play(play)
    prior = already_played(pl)
    if prior and prior.get("status") in ("launched", "routed"):
        # learn: don't re-launch the same play; surface its outcome to the caller
        return {"deduped": True, "id": pl["id"], "prior_outcome": prior.get("outcome"),
                "note": "already launched — not repeating"}

    t = pl["type"]
    if t == "harvest_query":
        undo = _launch_harvest(pl)
    elif t == "copy_variant":
        undo = _launch_variant(pl)
    elif t == "bofu_page":
        undo = _launch_bofu(pl)
    else:
        return route_to_owner(play, reason=f"no launcher for '{t}'")

    pl["status"] = "launched"
    pl["auto"] = True
    pl["undo"] = undo
    return record(pl, event="play")


def _launch_harvest(pl: dict[str, Any]) -> str:
    p = pl["params"]
    queue = _read_json(HARVEST_QUEUE, [])
    if not isinstance(queue, list):
        queue = []
    entry = {
        "market": p.get("market", "ai_wealth"),
        "query": p.get("query"),
        "subreddit": p.get("subreddit"),
        "added_by": "strategist",
        "added_at": _now(),
        "id": pl["id"],
    }
    queue.append(entry)
    _atomic_write(HARVEST_QUEUE, json.dumps(queue, indent=2) + "\n")
    return (f"remove the entry with id={pl['id']} from {HARVEST_QUEUE} "
            f"(or delete the file to clear all strategist-added harvest queries)")


def _launch_variant(pl: dict[str, Any]) -> str:
    p = pl["params"]
    vid = (p.get("variant_id") or p.get("id") or "").strip().lower()
    bank = _read_json(VARIANT_BANK_EXTRA, [])
    if not isinstance(bank, list):
        bank = []
    if vid not in [str(x.get("id", x)).lower() if isinstance(x, dict) else str(x).lower() for x in bank]:
        bank.append({"id": vid, "label": p.get("label", vid), "added_at": _now(),
                     "play_id": pl["id"]})
    _atomic_write(VARIANT_BANK_EXTRA, json.dumps(bank, indent=2) + "\n")
    return (f"remove id={vid} from {VARIANT_BANK_EXTRA} (the bandit drops it next run; "
            f"existing weights renormalize)")


def _launch_bofu(pl: dict[str, Any]) -> str:
    p = pl["params"]
    query = p.get("query")
    gap = _read_json(KEYWORD_GAP, {})
    if not isinstance(gap, dict):
        gap = {}
    assigns = gap.setdefault("assignments", {})
    lst = assigns.setdefault("programmatic_seo", [])
    if query not in lst:
        lst.append(query)
    gap.setdefault("strategist_added", []).append({"query": query, "at": _now(), "id": pl["id"]})
    _atomic_write(KEYWORD_GAP, json.dumps(gap, indent=2) + "\n")
    return (f"remove the query {query!r} from {KEYWORD_GAP}['assignments']['programmatic_seo'] "
            f"and delete data/hustle/seo_pages/{_slug(query)}.html if it was built")


# --------------------------------------------------------------------------- BIG-BET ROUTING (owner)
def _proposal_text(play: dict[str, Any], reason: str) -> str:
    p = play.get("params", {}) or {}
    what = p.get("summary") or p.get("title") or p.get("query") or play.get("type")
    why = p.get("rationale") or reason
    lane = play.get("lane", "faceless")
    return (f"new play proposed: {what}\n"
            f"type: {play.get('type')} · lane: {lane}\n"
            f"why: {why}\n"
            f"reversible: {p.get('reversible', 'review before launch')}")


def route_to_owner(play: dict[str, Any], *, reason: str = "big-bet") -> dict[str, Any]:
    """Draft the play as an approval item and push it into the SAME fire-queue +
    approval-ledger path the fire_approval_runner already texts, so Operator gets a
    'new play proposed — GO/NO' iMessage and approves it exactly like a warm fire.

    Identity marketplaces are forced through BrandA-named identity; faceless lanes
    stay faceless. A GO posts NOWHERE — the runner's 'strategy' firer only RECORDS
    the decision (registered separately). Returns the recorded memory row."""
    if is_off():
        return {"refused": "STRATEGIST_OFF present"}
    pl = normalize_play(play)
    prior = already_played(pl)
    if prior and prior.get("status") == "routed":
        return {"deduped": True, "id": pl["id"], "note": "already routed to owner — not re-texting"}

    p = pl.get("params", {}) or {}
    lane = str(pl.get("lane", "faceless")).lower()
    # lane separation: BrandA proposals carry the BrandA identity (runner employment-
    # screens it); everything else proposes under a neutral 'house' identity that never
    # correlates a faceless brand to anything.
    identity = "BrandA" if lane in NAMED_OR_IDENTITY_LANES else "house"
    draft = _proposal_text(pl, reason)
    title = (p.get("title") or p.get("summary") or f"{pl['type']} proposal")[:140]

    # Stable approval id; the runner's token + the ledger entry key off this exact id.
    approval_id = hashlib.sha256(("strategy:" + pl["id"]).encode()).hexdigest()[:20]

    item = {
        "token": f"sp-{pl['id'][:6]}",
        "channel": "strategy",                 # registered in fire_approval_runner; posts NOWHERE
        "identity": identity,
        "target": f"strategy://{pl['type']}/{pl['id']}",
        "draft_text": draft,
        "title": title,
        "employment_screen": "pass" if identity != "BrandA" else "screen",
        "submittable": True,
        "status": "pending",
        "approval_id": approval_id,
        "ledger_status": "pending",
        "lane": lane,
        "strategist_play_id": pl["id"],
        "reason": reason,
    }
    _append_jsonl(FIRE_QUEUE, item)

    # Mirror into the approval ledger so the runner's GO/dedup/audit all key off one id.
    ledger_ok = None
    try:
        import sys
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        from core import approval_ledger
        action = {
            "id": approval_id,
            "kind": "strategy_proposal",
            "pipeline": "strategist",
            "identity": identity,
            "destination": item["target"],
            "subject": title,
            "body": draft,
            "approval_required": True,
            "approval_reason": "autonomous strategist big-bet proposal — owner GO/NO required",
            "fire_channel": "strategy",
            "lane": lane,
        }
        approval_ledger.queue_action(action, actor="strategist")
        ledger_ok = True
    except Exception as exc:  # ledger is best-effort; the queue line is the source of truth
        ledger_ok = f"skipped: {exc}"

    pl["status"] = "routed"
    pl["auto"] = False
    pl["approval_id"] = approval_id
    pl.setdefault("params", {})["approval_id"] = approval_id
    pl["undo"] = (f"remove the line with approval_id={approval_id} from {FIRE_QUEUE}; "
                  f"reject_action({approval_id}) in the approval ledger")
    return record(pl, event="play", routed_reason=reason, approval_id=approval_id,
                  ledger_ok=ledger_ok)


def note_owner_decision(approval_id: str, item: dict[str, Any], *, approved: bool) -> dict[str, Any]:
    """Called by the fire_approval_runner 'strategy' firer on GO. Records the owner's
    decision against the originating play (no external action is taken — nothing posts)."""
    play = {
        "type": (item.get("fire_channel") == "strategy") and "routed_decision" or "routed_decision",
        "lane": item.get("lane", "house"),
        "params": {"approval_id": approval_id, "title": item.get("title")},
    }
    pid = item.get("strategist_play_id")
    row = {
        "ts": _now(),
        "event": "owner_decision",
        "id": pid or play_id(play),
        "approval_id": approval_id,
        "approved": bool(approved),
        "status": "approved" if approved else "rejected",
    }
    _append_jsonl(MEMORY, row)
    return row


# --------------------------------------------------------------------------- top-level decide
def propose(play: dict[str, Any]) -> dict[str, Any]:
    """The strategist's single entry point: classify, then either auto-launch a safe
    play (if armed) or route a big bet to the owner. Always dedups + always records."""
    verdict = classify(play)
    if verdict["decision"] == "auto":
        return launch(play)
    return route_to_owner(play, reason=verdict["reason"])


if __name__ == "__main__":
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "recent"
    if cmd == "recent":
        print(json.dumps(recent_plays(int(sys.argv[2]) if len(sys.argv) > 2 else 20), indent=2))
    elif cmd == "attach":
        print(json.dumps(attach_outcomes(), indent=2))
    else:
        print("usage: strategist_memory.py [recent N|attach]")
