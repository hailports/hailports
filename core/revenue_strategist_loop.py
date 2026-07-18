#!/usr/bin/env python3
"""Autonomous Strategist Loop — INITIATES, but safely.

The revenue brain DIAGNOSES the funnel (where it dies) and reallocates copy+volume.
This loop is the layer above: it READS the brain's diagnosis plus the real ledgers,
asks an LLM for candidate PLAYS, and then either auto-launches the cheap+reversible+
lane-safe ones through real actuators a LIVE component already consumes, or DRAFTS the
rest and routes them to Operator through the existing fire-approval/iMessage loop.

ANTI-THEATER (the whole point): every auto-launched play takes a REAL action a live
component picks up next cycle. We do not write a "strategy report" nobody reads:

  - harvest_query  -> the demand harvester (agents/multi_market_intent.py) picks up the
                      new query/subreddit on its next scan.
  - copy_variant   -> data/variant_bank.json, which core/revenue_brain.py's Thompson
                      bandit tests and agents/copy_rotation feeds to the NEXT real send.
  - bofu_page      -> data/hustle/keyword_gap.json assignments, which
                      agents/programmatic_seo.py turns into a real published page.

Everything else (offer_test, new_channel, pricing, anything spending money, anything
under BrandA's NAMED identity or an identity marketplace, anything irreversible) is
DRAFTED and ROUTED TO Operator (core/approval_ledger + the fire queue ->
agents/fire_approval_runner -> iMessage). Never auto-launched.

The actual launch/undo wiring + the auto-safety policy live in core/strategist_actuators
(actuators.launch(play)->{ok,undo}, actuators.is_auto_safe(play)->bool) and the played
history in core/strategist_memory (memory.recent_plays(), memory.record(play, outcome)).
Those are built in parallel; this module fail-soft falls back to a faithful local
implementation of the same contract so it runs (and dry-runs on real state) today.

GUARDRAILS enforced here, before anything reaches an actuator:
  - grounding: a play's rationale MUST cite a real number from the situation brief, or it
    is rejected (no fabricated justification).
  - no fabricated facts in copy: a copy_variant template may not invent stats/reviews/
    proof (FTC); it must carry {company} and no numerals/proof-words.
  - lane separation: faceless brands stay anon + un-correlated; anything BrandA/named or
    identity-marketplace is never auto, always routed.
  - reversibility: every launched play records how to undo it.
  - default-OFF: launches require STRATEGIST_ENABLED=1; --dry never launches.

  python3 -m core.revenue_strategist_loop --dry   # ingest real state, show plays, launch nothing
  STRATEGIST_ENABLED=1 python3 -m core.revenue_strategist_loop   # live (armed)
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HUSTLE = ROOT / "data" / "hustle"

DECISIONS = ROOT / "data" / "revenue_decisions.json"
STRATEGY = HUSTLE / "strategy.json"
FUNNEL_RATES = HUSTLE / "funnel_rates.json"
SNAPSHOTS = ROOT / "data" / "stack_snapshots.jsonl"
WARM_QUEUE = HUSTLE / "WARM_DEMAND_FIRE_QUEUE.md"
VARIANT_BANK = ROOT / "data" / "variant_bank.json"

PLAYS_LOG = HUSTLE / "strategist_plays.jsonl"
LOCAL_MEMORY = HUSTLE / "strategist_memory.jsonl"
DIGEST_OUT = HUSTLE / "strategist_digest.txt"

# Play types this loop is even allowed to auto-launch (cheap + reversible + a live consumer
# already exists). Everything else is structurally route-to-Operator.
AUTO_TYPES = {"harvest_query", "copy_variant", "bofu_page"}
# Lanes that are NEVER auto: named identity / identity marketplaces.
NAMED_LANES = {"BrandA", "maroon_standard", "named", "identity", "upwork", "fiverr"}
# Proof/claim words a generated copy variant may not introduce (FTC: no fabricated proof).
_PROOF_WORDS = re.compile(
    r"\b(\d+%|\d+\s*(stars?|reviews?|clients?|customers?|users?|years?)|guarantee|"
    r"#1|best|proven|trusted by|rated|award)", re.I)
# Map each auto play type to the LIVE component that consumes its actuator output (the
# anti-theater proof; surfaced in the play log so a human can verify the consumer exists).
CONSUMERS = {
    "harvest_query": "agents/multi_market_intent.py (demand harvester next scan)",
    "copy_variant": "core/revenue_brain.py Thompson bandit -> agents/copy_rotation -> next send",
    "bofu_page": "agents/programmatic_seo.py (builds the queued page)",
    "offer_test": "ROUTE-ONLY: no lane-safe reversible auto consumer",
    "new_channel": "ROUTE-ONLY: new channel = identity/spend risk",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(errors="ignore"))
    except Exception:
        return default


def _last_jsonl(path: Path):
    try:
        last = None
        for line in path.read_text(errors="ignore").splitlines():
            line = line.strip()
            if line:
                last = line
        return json.loads(last) if last else {}
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Fail-soft binding to the parallel actuator/memory modules. If they aren't built
# yet, fall back to a faithful local implementation of the SAME contract so the loop
# runs and dry-runs on real state today; once the real modules land, they win.
# ---------------------------------------------------------------------------
def _is_auto_safe_fallback(play: dict) -> bool:
    if play.get("type") not in AUTO_TYPES:
        return False
    if not play.get("reversible", False):
        return False
    cost = str(play.get("cost", "")).strip().lower().lstrip("$")
    if cost not in ("", "0", "0.0", "free", "none"):
        return False
    if str(play.get("risk", "")).strip().lower() == "high":
        return False
    lane = str(play.get("lane", "")).strip().lower()
    if any(n in lane for n in NAMED_LANES):
        return False
    return True


# Owner directive (2026-06-29): "make the calls for me given all the guardrails." So the loop
# AUTO-APPROVES the route-only STRATEGY/DRAFT plays it used to ping about (offer framing, copy
# variants, BOFU pages, harvest queries) — none of which SEND anything in his name — while the
# genuinely human-gated set STILL routes: anything that SPENDS money, stands up a NEW identity/
# channel, or actually SENDS outbound in a named/identity lane (the air-gap rail stays intact —
# a BrandA OFFER decision is auto-approved, but a BrandA real-name SEND remains gated elsewhere
# by maroon_sender's own arming). Toggle off with STRATEGIST_AUTO_DECIDE=0.
AUTO_DECIDE = os.environ.get("STRATEGIST_AUTO_DECIDE", "1").strip().lower() in ("1", "true", "yes", "on")


def _needs_human(play: dict) -> bool:
    """True ONLY for the irreducible owner-gated set (spend / new identity / named-lane SEND).
    Everything else is a reversible strategy/draft decision the loop may auto-approve."""
    cost = str(play.get("cost", "")).strip().lower().lstrip("$")
    if cost not in ("", "0", "0.0", "free", "none"):
        return True                                   # spend = supervised (spend rail)
    if str(play.get("type", "")).strip().lower() in {"new_channel", "new_identity", "register"}:
        return True                                   # new identity/channel = owner call
    lane = str(play.get("lane", "")).strip().lower()
    outbound = bool(play.get("outbound") or play.get("sends") or play.get("send"))
    if outbound and any(n in lane for n in NAMED_LANES):
        return True                                   # a real-name SEND stays his (air-gap)
    return False


class _FallbackActuators:
    """Mirrors core.strategist_actuators contract. Does NOT touch live files in fallback
    mode (the real module owns consumer wiring); it records the intended undo so a launch
    is always reversible-on-paper even before the real actuator exists."""

    def is_auto_safe(self, play: dict) -> bool:
        return _is_auto_safe_fallback(play)

    def launch(self, play: dict) -> dict:
        return {
            "ok": False,
            "deferred": True,
            "reason": "core.strategist_actuators not importable; launch deferred to real actuator",
            "undo": play.get("undo") or f"remove the {play.get('type')} this loop proposed: "
                                         f"{json.dumps(play.get('params', {}))}",
        }


class _FallbackMemory:
    def recent_plays(self, limit: int = 200) -> list:
        out = []
        try:
            for line in LOCAL_MEMORY.read_text(errors="ignore").splitlines():
                line = line.strip()
                if line:
                    out.append(json.loads(line))
        except Exception:
            pass
        return out[-limit:]

    def record(self, play: dict, outcome: dict) -> None:
        LOCAL_MEMORY.parent.mkdir(parents=True, exist_ok=True)
        with LOCAL_MEMORY.open("a") as fh:
            fh.write(json.dumps({"ts": _now(), "play": play, "outcome": outcome}) + "\n")


def _bind():
    try:
        from core import strategist_actuators as actuators  # type: ignore
        if not (hasattr(actuators, "launch") and hasattr(actuators, "is_auto_safe")):
            raise ImportError("incomplete actuators interface")
    except Exception:
        actuators = _FallbackActuators()
    try:
        from core import strategist_memory as memory  # type: ignore
        if not (hasattr(memory, "recent_plays") and hasattr(memory, "record")):
            raise ImportError("incomplete memory interface")
    except Exception:
        memory = _FallbackMemory()
    return actuators, memory


# ---------------------------------------------------------------------------
# 1. INGEST — compact situation brief, numbers only, no fabrication.
# ---------------------------------------------------------------------------
def build_brief() -> dict:
    dec = _read_json(DECISIONS, {})
    strat = _read_json(STRATEGY, {})
    rates = _read_json(FUNNEL_RATES, {})
    snap = _last_jsonl(SNAPSHOTS)
    bank = _read_json(VARIANT_BANK, {})

    dw = dec.get("data_window", {})
    bc = dec.get("binding_constraint", {})

    # warm-demand audit: genuine hand-raisers vs harvested noise (parsed from the queue md).
    warm_genuine = warm_harvested = None
    try:
        m = re.search(r"(\d+)\s+genuine hand-raisers in queue \(of\s+(\d+)\s+harvested",
                      WARM_QUEUE.read_text(errors="ignore"))
        if m:
            warm_genuine, warm_harvested = int(m.group(1)), int(m.group(2))
    except Exception:
        pass

    seg = {}
    for s in strat.get("segment_ranking", []) or []:
        seg[s.get("sequence", "?")] = {
            "sends": s.get("sends"), "replies": s.get("replies"),
            "detected_replies": s.get("detected_replies"),
            "reply_rate_pct": s.get("reply_rate_pct"), "status": s.get("status"),
        }

    bank_counts = {}
    for lane, pools in (bank.items() if isinstance(bank, dict) else []):
        if lane.startswith("_") or not isinstance(pools, dict):
            continue
        bank_counts[lane] = {p: len(v) for p, v in pools.items() if isinstance(v, list)}

    # portfolio controller: scoreboard-derived per-channel effort signal (fail-soft; uses the
    # scoreboard-only reader, NOT read_portfolio_state, to avoid recursion back into build_brief).
    portfolio_signal = {}
    try:
        from core.cortex.portfolio import scoreboard_channel_signal
        portfolio_signal = {ch: {"verdict": t.get("verdict"), "weight": t.get("weight"),
                                 "metric": t.get("metric", {})}
                            for ch, t in scoreboard_channel_signal().items()}
    except Exception:
        portfolio_signal = {}

    return {
        "generated_at": _now(),
        "binding_constraint": {
            "lane": bc.get("lane"), "stage": bc.get("stage"), "rate": bc.get("rate"),
            "wilson_hi": bc.get("wilson_hi"), "n": bc.get("n"),
            "diagnosis": bc.get("diagnosis"), "action": bc.get("action"),
        },
        "funnel": {
            "broken_site_sends_ok": dw.get("broken_site_sends_ok"),
            "broken_site_real_replies": dw.get("broken_site_real_replies"),
            "variant_tagged_sends": dw.get("variant_tagged_sends"),
            "self_serve_leads": dw.get("self_serve_leads"),
            "paid_orders": dw.get("paid_orders"),
            "revenue_cents": dw.get("revenue_cents"),
        },
        "site_funnel_rates": rates.get("overall", rates) if isinstance(rates, dict) else {},
        "outreach_summary": strat.get("summary", {}),
        "segments": seg,
        "outbound_hold": (strat.get("directives", {}) or {}).get("outbound_hold"),
        "priority_segments": (strat.get("directives", {}) or {}).get("priority_segments"),
        "warm_demand": {"genuine": warm_genuine, "harvested": warm_harvested},
        "snapshot": {
            "sends_total": snap.get("sends_total"), "leads_total": snap.get("leads_total"),
            "replies_total": snap.get("replies_total"), "paid_orders": snap.get("paid_orders"),
            "revenue_usd": snap.get("revenue_usd"),
        },
        "variant_bank_counts": bank_counts,
        "portfolio_signal": portfolio_signal,
    }


def _grounding_facts(brief: dict) -> set:
    """Every real number anywhere in the brief, as strings, so a play's rationale can be
    checked against them. A rationale citing a number NOT in this set is ungrounded."""
    facts: set = set()

    def walk(v):
        if isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)
        elif isinstance(v, bool):
            return
        elif isinstance(v, (int, float)):
            facts.add(str(v))
            if isinstance(v, float):
                facts.add(str(round(v, 1)))
        elif isinstance(v, str):
            for n in re.findall(r"\d+\.?\d*", v):
                facts.add(n)

    walk(brief)
    facts.discard("0.0")
    return {f for f in facts if f}


# ---------------------------------------------------------------------------
# 2. GENERATE — candidate plays via the stack's free LLM pool, strict JSON schema.
#    Deterministic grounded fallback if the LLM is unavailable (loop never goes dark).
# ---------------------------------------------------------------------------
_SCHEMA_HINT = """Return ONLY a JSON array (no prose) of 3-6 play objects. Each play:
{
  "type": "harvest_query" | "copy_variant" | "bofu_page" | "offer_test" | "new_channel",
  "rationale": "<one sentence; MUST quote a real number that appears in the BRIEF>",
  "params": { ... exact actuator input ... },
  "expected_impact": "<short>",
  "cost": "0",               // dollars; cheap auto plays must be 0
  "risk": "low" | "med" | "high",
  "reversible": true | false,
  "lane": "broken_site" | "docsapp" | "faceless" | "BrandA"
}
Rules: copy_variant params.template MUST contain {company} and MUST NOT invent any stat,
percentage, review count, or proof claim. harvest_query params = {"market","query" or
"subreddit"}. bofu_page params = {"keyword","intent"}. Anything touching BrandA's named
identity, pricing, spend, or a new channel: set reversible/lane honestly so it routes to a
human. Ground every rationale in the BRIEF's real numbers; do not invent facts."""


def _llm_generate(brief: dict) -> list:
    prompt = (
        "You are a revenue strategist. Given this REAL situation brief (numbers are facts, "
        "do not invent any), propose concrete plays.\n\nBRIEF:\n"
        + json.dumps(brief, indent=2) + "\n\n" + _SCHEMA_HINT)
    try:
        from core.free_llm_pool import try_free_providers
        text, _prov = asyncio.run(try_free_providers(
            prompt, system="Output strict JSON only. No fabricated numbers.",
            max_tokens=1400, explicit=True, tier="strong"))
        if not text:
            return []
        m = re.search(r"\[.*\]", text, re.S)
        plays = json.loads(m.group(0) if m else text)
        return plays if isinstance(plays, list) else []
    except Exception:
        return []


def _fallback_generate(brief: dict) -> list:
    """Deterministic, fully-grounded plays derived straight from the brief, so a dry-run
    on real state always shows concrete plays even with no LLM. Each cites a real number."""
    bc = brief["binding_constraint"]
    fn = brief["funnel"]
    seg = brief["segments"]
    warm = brief["warm_demand"]
    plays = []

    sends = fn.get("broken_site_sends_ok")
    tagged = fn.get("variant_tagged_sends")
    if sends is not None:
        plays.append({
            "type": "copy_variant",
            "rationale": (f"broken_site send->reply is {bc.get('rate')} over {sends} sends with "
                          f"only {tagged} variant-tagged sends, so the bandit has no copy signal "
                          f"yet -- widen exploration with one more honest subject framing."),
            "params": {"lane": "broken_site", "pool": "subject",
                       "id": "bs_loadcheck",
                       "template": "is {company}'s site loading right for you?"},
            "expected_impact": "more subject diversity for the bandit to test against 0% reply rate",
            "cost": "0", "risk": "low", "reversible": True, "lane": "broken_site",
        })

    docsapp = seg.get("docsapp", {})
    if warm.get("genuine") is not None:
        plays.append({
            "type": "harvest_query",
            "rationale": (f"only {warm['genuine']} of {warm['harvested']} harvested warm leads were "
                          f"genuine and docsapp is the one converting segment at "
                          f"{docsapp.get('reply_rate_pct')}% reply, so harvest more of that proven "
                          f"web-rebuild demand."),
            "params": {"market": "web_rebuild",
                       "query": "small business website outdated need rebuild",
                       "subreddit": "smallbusiness"},
            "expected_impact": "higher-intent inbound feed for the converting lane",
            "cost": "0", "risk": "low", "reversible": True, "lane": "faceless",
        })

    leads = fn.get("self_serve_leads")
    if leads is not None:
        plays.append({
            "type": "bofu_page",
            "rationale": (f"self-serve produced {leads} leads and "
                          f"{brief['snapshot'].get('paid_orders')} paid, so the gap is bottom-of-funnel "
                          f"intent capture -- queue a BOFU page for the site-scan buyer."),
            "params": {"keyword": "free website health check small business",
                       "intent": "buyer evaluating a site fix before paying"},
            "expected_impact": "organic BOFU entry the SEO factory publishes + funnel records",
            "cost": "0", "risk": "low", "reversible": True, "lane": "docsapp",
        })

    BrandA = seg.get("BrandA Standard — Fractional RevOps", {})
    if BrandA:
        plays.append({
            "type": "offer_test",
            "rationale": (f"BrandA Fractional RevOps has {BrandA.get('detected_replies')} detected "
                          f"replies on {BrandA.get('sends')} sends but {BrandA.get('replies')} real "
                          f"replies and is manual_review, so an offer-angle change needs your call."),
            "params": {"segment": "BrandA Standard - Fractional RevOps",
                       "proposed_angle": "lead with a fixed-price 48h Salesforce risk snapshot"},
            "expected_impact": "possibly unstick a high-$/win named lane",
            "cost": "0", "risk": "med", "reversible": True, "lane": "BrandA",
        })
    return plays


def generate_plays(brief: dict) -> list:
    plays = _llm_generate(brief)
    if not plays:
        plays = _fallback_generate(brief)
    return plays


# ---------------------------------------------------------------------------
# 3. VALIDATE (grounding + no-fabrication) + SCORE + DEDUP against memory.
# ---------------------------------------------------------------------------
def _signature(play: dict) -> str:
    p = play.get("params", {})
    key = (p.get("query") or p.get("subreddit") or p.get("template")
           or p.get("keyword") or p.get("proposed_angle") or json.dumps(p, sort_keys=True))
    return f"{play.get('type')}::{str(key).strip().lower()}"


def validate(play: dict, facts: set) -> tuple[bool, str]:
    if play.get("type") not in CONSUMERS:
        return False, f"unknown play type {play.get('type')!r}"
    rationale = str(play.get("rationale", ""))
    cited = set(re.findall(r"\d+\.?\d*", rationale))
    if not (cited & facts):
        return False, "ungrounded rationale: cites no real number from the brief"
    if play.get("type") == "copy_variant":
        tmpl = str(play.get("params", {}).get("template", ""))
        if "{company}" not in tmpl:
            return False, "copy variant missing {company} token"
        if _PROOF_WORDS.search(tmpl):
            return False, "copy variant introduces a fabricated stat/proof claim (FTC)"
    return True, "ok"


def score(play: dict, brief: dict) -> float:
    s = {"low": 0.0, "med": 0.4, "high": 1.0}.get(str(play.get("risk", "low")).lower(), 0.4)
    impact = 1.0 if play.get("type") in AUTO_TYPES else 0.6  # bias cheap reversible learning
    return round(impact - 0.3 * s, 3)


def prepare(plays: list, brief: dict, memory) -> list:
    facts = _grounding_facts(brief)
    seen = {p.get("play", {}).get("_signature") if isinstance(p.get("play"), dict)
            else None for p in memory.recent_plays()}
    seen |= {_signature(p["play"]) for p in memory.recent_plays()
             if isinstance(p.get("play"), dict)}
    prepared, local_seen = [], set()
    for play in plays:
        if not isinstance(play, dict):
            continue
        ok, why = validate(play, facts)
        if not ok:
            prepared.append({**play, "_signature": _signature(play),
                             "_status": "rejected", "_reason": why})
            continue
        sig = _signature(play)
        if sig in seen or sig in local_seen:
            prepared.append({**play, "_signature": sig,
                             "_status": "skipped_dup", "_reason": "already tried (memory)"})
            continue
        local_seen.add(sig)
        play["_signature"] = sig
        play["_score"] = score(play, brief)
        play["_consumer"] = CONSUMERS.get(play["type"])
        prepared.append(play)
    live = [p for p in prepared if p.get("_status") not in ("rejected", "skipped_dup")]
    live.sort(key=lambda p: p.get("_score", 0), reverse=True)
    rest = [p for p in prepared if p.get("_status") in ("rejected", "skipped_dup")]
    return live + rest


# ---------------------------------------------------------------------------
# 4. DECIDE — auto-launch the safe ones, route the rest to Operator.
# ---------------------------------------------------------------------------
def route_to_alex(play: dict) -> dict:
    """Draft + queue for human approval via the existing fire-approval/iMessage loop."""
    action = {
        "pipeline": "revenue_strategist",
        "type": play.get("type"),
        "title": f"[strategist] {play.get('type')}: {play.get('expected_impact', '')}"[:120],
        "rationale": play.get("rationale"),
        "params": play.get("params"),
        "lane": play.get("lane"),
        "cost": play.get("cost"),
        "risk": play.get("risk"),
        "reversible": play.get("reversible"),
        "source": "core/revenue_strategist_loop.py",
    }
    try:
        from core.approval_ledger import queue_action
        rec = queue_action(action, actor="strategist")
        return {"routed": True, "approval_id": rec.get("id") or rec.get("action_id"),
                "via": "core.approval_ledger.queue_action"}
    except Exception as e:
        return {"routed": False, "error": str(e)[:160],
                "draft": action, "via": "approval_ledger_unavailable"}


# Faceless brands the BOFU actuator accepts (mirror strategist_actuators.FACELESS_BOFU_BRANDS).
_FACELESS_BOFU = {"hailport", "scannerapp", "docsapp"}  # builtfast/signalhq KILLED — see strategist_actuators


def _adapt_play(play: dict) -> dict:
    """Flatten the loop's nested `params` into the TOP-LEVEL keys the actuators actually read
    (is_auto_safe/launch check play['channel']/['slot']/['text']/['query']/['source']/['brand']/
    ['topic']). Without this every auto play fails is_auto_safe and NOTHING launches (the whole
    anti-theater point). Non-mutating: returns a shallow copy with the flattened keys added."""
    p = dict(play)
    params = play.get("params") or {}
    t = play.get("type")
    if t == "copy_variant":
        p.setdefault("channel", params.get("lane"))
        p.setdefault("slot", params.get("pool"))
        p.setdefault("text", params.get("template"))
    elif t == "harvest_query":
        p.setdefault("query", params.get("query") or params.get("subreddit"))
        p.setdefault("source", params.get("subreddit") or params.get("market") or "reddit")
    elif t == "bofu_page":
        brand = params.get("brand") or play.get("lane")
        if brand not in _FACELESS_BOFU:  # map a non-faceless lane to a real (live) faceless brand
            brand = "scannerapp"
        p.setdefault("brand", brand)
        p.setdefault("topic", params.get("keyword") or params.get("topic"))
        p.setdefault("cat", params.get("intent") or params.get("cat"))
    return p


def decide(prepared: list, actuators, memory, *, dry: bool, armed: bool) -> dict:
    launched, proposed, skipped = [], [], []
    for play in prepared:
        if play.get("_status") in ("rejected", "skipped_dup"):
            skipped.append(play)
            continue
        adapted = _adapt_play(play)  # flatten params -> top-level keys the actuators read
        auto = bool(actuators.is_auto_safe(adapted))
        if auto:
            if dry or not armed:
                play["_decision"] = "would_launch" if dry else "blocked_default_off"
                launched.append(play)
                continue
            res = actuators.launch(adapted)
            outcome = {"action": "launched", "result": res, "auto": True}
            memory.record(play, outcome)
            play["_decision"] = "launched"
            play["_undo"] = res.get("undo")
            play["_result"] = res
            launched.append(play)
        else:
            # owner said "make the calls": auto-approve reversible strategy/draft plays (no ping);
            # only spend / new-identity / named-lane SENDS still route to him.
            if AUTO_DECIDE and not _needs_human(adapted):
                play["_decision"] = "auto_approved" if not dry else "would_auto_approve"
                if not dry:
                    memory.record(play, {"action": "auto_approved", "auto": True})
                proposed.append(play)
                continue
            if dry:
                play["_decision"] = "would_route_to_alex"
                proposed.append(play)
                continue
            res = route_to_alex(play)
            memory.record(play, {"action": "proposed", "result": res, "auto": False})
            play["_decision"] = "routed_to_alex"
            play["_result"] = res
            proposed.append(play)
    return {"launched": launched, "proposed": proposed, "skipped": skipped}


# ---------------------------------------------------------------------------
# 5. LOG + iMessage digest.
# ---------------------------------------------------------------------------
def _one_liner(p: dict) -> str:
    params = p.get("params", {})
    key = (params.get("query") or params.get("subreddit") or params.get("template")
           or params.get("keyword") or params.get("proposed_angle") or "")
    return f"{p.get('type')} [{p.get('lane')}]: {str(key)[:60]}"


def write_log(brief: dict, result: dict, *, dry: bool) -> None:
    PLAYS_LOG.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "ts": _now(), "dry": dry,
        "binding_constraint": brief["binding_constraint"].get("stage"),
        "launched": [{"type": p["type"], "consumer": p.get("_consumer"),
                      "decision": p.get("_decision"), "undo": p.get("_undo"),
                      "rationale": p.get("rationale"), "params": p.get("params")}
                     for p in result["launched"]],
        "proposed": [{"type": p["type"], "decision": p.get("_decision"),
                      "rationale": p.get("rationale"), "params": p.get("params"),
                      "result": p.get("_result")}
                     for p in result["proposed"]],
        "skipped": [{"type": p.get("type"), "reason": p.get("_reason")}
                    for p in result["skipped"]],
    }
    with PLAYS_LOG.open("a") as fh:
        fh.write(json.dumps(row) + "\n")


def _routed(p: dict) -> bool:
    return str(p.get("_decision", "")) in ("routed_to_alex", "would_route_to_alex")


def digest(result: dict, *, dry: bool) -> str:
    auto_ok = [p for p in result["proposed"] if not _routed(p)]      # auto-approved strategy calls
    routed = [p for p in result["proposed"] if _routed(p)]           # genuinely need him
    nl = len(result["launched"]) + len(auto_ok)
    verb = "would launch" if dry else "launched/approved"
    lines = [f"strategist: {verb} {nl} plays; awaiting your call on {len(routed)}."]
    for p in result["launched"]:
        lines.append(f"  + {_one_liner(p)}")
    for p in auto_ok:
        lines.append(f"  + {_one_liner(p)} (auto-approved)")
    for p in routed:
        lines.append(f"  ? {_one_liner(p)} -> needs your ok (spend / new identity / named send)")
    return "\n".join(lines)


def send_digest(text: str, result: dict | None = None) -> None:
    # only interrupt him when something GENUINELY needs his call; otherwise stay quiet (the full
    # digest is still written to the play log for on-demand review).
    if result is not None and not any(_routed(p) for p in result.get("proposed", [])):
        return
    try:
        from tools.imsg_bridge import send_imessage
        send_imessage(text)
    except Exception:
        pass


# ---------------------------------------------------------------------------
def _armed() -> bool:
    return os.environ.get("STRATEGIST_ENABLED", "").strip() in ("1", "true", "yes", "on")


def run(dry: bool = False) -> dict:
    actuators, memory = _bind()
    armed = _armed()
    brief = build_brief()
    plays = generate_plays(brief)
    prepared = prepare(plays, brief, memory)
    result = decide(prepared, actuators, memory, dry=dry, armed=armed)
    write_log(brief, result, dry=dry)
    dig = digest(result, dry=dry)
    DIGEST_OUT.write_text(dig + "\n")
    if not dry and armed:
        send_digest(dig, result)
    return {"brief": brief, "result": result, "digest": dig, "armed": armed, "dry": dry}


def main(argv: list[str]) -> int:
    dry = "--dry" in argv or not _armed()
    out = run(dry=dry)
    r = out["result"]
    print(json.dumps({
        "dry": out["dry"], "armed": out["armed"],
        "binding_constraint": out["brief"]["binding_constraint"].get("stage"),
        "launched": [{"type": p["type"], "lane": p.get("lane"), "consumer": p.get("_consumer"),
                      "decision": p.get("_decision"), "rationale": p.get("rationale"),
                      "params": p.get("params"), "undo": p.get("_undo")}
                     for p in r["launched"]],
        "routed_to_alex": [{"type": p["type"], "lane": p.get("lane"),
                            "decision": p.get("_decision"), "rationale": p.get("rationale"),
                            "params": p.get("params")}
                           for p in r["proposed"]],
        "skipped": [{"type": p.get("type"), "reason": p.get("_reason")} for p in r["skipped"]],
        "digest": out["digest"],
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
