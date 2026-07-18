#!/usr/bin/env python3
"""Bottleneck diagnoser — names the binding constraint AND acts on it.

Reads the revenue brain's decisions + the real funnel ledger, walks the funnel top-to-bottom,
and names the SINGLE binding constraint (the earliest stage where real flow collapses) in plain
language. It does NOT invent numbers: every figure comes from a real ledger/config on disk.

The diagnosis is not notify-only. When the binding constraint is a deliverability / send-stage
collapse (a funnel_leak where outreach is sent but does not become traffic — the signature of mail
not landing/opening), this writes a real outbound-hold directive that the LIVE send path already
honors:
  - products/outreach/compliance_guard.py::outbound_sends_enabled() reads strategy.json
    directives.outbound_hold and FAIL-CLOSES every real provider send for the held components
    (direct_sender, outreach_engine, relentless_revenue, sf_client_blitz, ima_sender, hunter, …).
  - agents/revenue_strategist.py carries the hold forward (via strategy_overrides.json) so a daily
    strategy regeneration does not clobber it.
  - core/next_money_move.py surfaces it as outbound_hold_active.
The diagnoser is the only thing that can see this constraint — the strategist judges sequences by
replies and is blind to "sent-but-not-delivered". The hold protects sender reputation until landing
rate recovers, and auto-expires (TTL) so a transient blip never permanently gags senders.

Outputs (all real, additive / fail-soft):
  - strategy.json directives.outbound_hold      the live switch the compliance guard / senders honor
  - strategy_overrides.json bottleneck_hold     durable record the strategist preserves
  - data/hustle/bottleneck_diagnosis.json       machine-readable diagnosis + what it switched
  - iMessage to the owner via tools.imsg_bridge (side-effect; once / 12h dedup)

CLI:
  python -m core.bottleneck_diagnoser            # diagnose + apply directive, send if due
  python -m core.bottleneck_diagnoser --dry      # diagnose + print, never write/send
  python -m core.bottleneck_diagnoser --force     # send even if inside the 12h window

launchd: com.claude-stack.bottleneck-diagnoser (daily). $0 — local reads only.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(os.environ.get("CLAUDE_STACK_DIR", Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(ROOT))

HUSTLE = ROOT / "data" / "hustle"
# The brain writes revenue_brain_state.json; revenue_decisions.json is the documented/alias path.
DECISIONS_PRIMARY = ROOT / "data" / "revenue_decisions.json"
DECISIONS_FALLBACK = HUSTLE / "revenue_brain_state.json"
FUNNEL_LEDGER = HUSTLE / "funnel_ledger.jsonl"
VARIANT_WEIGHTS = HUSTLE / "variant_weights.json"
STRATEGY = HUSTLE / "strategy.json"
STRATEGY_OVERRIDES = HUSTLE / "strategy_overrides.json"
LEAD_FUNNEL = HUSTLE / "lead_funnel_config.json"
DIAGNOSIS_OUT = HUSTLE / "bottleneck_diagnosis.json"
STATE = HUSTLE / ".bottleneck_diagnoser_state.json"

DEDUP_HOURS = 12.0
MIN_FLOW = 15        # a stage needs at least this many events before we judge its conversion
LEAK_FLOOR = 0.05    # downstream/upstream below this = the funnel is choking here

# Outbound-hold wiring. Only a SEND-stage collapse (sent but not becoming traffic = deliverability)
# or an explicit deliverability constraint triggers a hold — a downstream leak (offer/page problem)
# must NOT blanket-gag cold senders. The hold auto-expires so a blip can't pause sending forever.
HOLD_STAGES = {"send"}
HOLD_KINDS = {"funnel_leak", "deliverability"}
HOLD_TTL_HOURS = 36.0
# Mirror of compliance_guard.STRATEGY_HOLD_COMPONENTS — the cold-outbound send paths that fail-close.
HOLD_COMPONENTS = [
    "outreach_cron",
    "outreach_engine",
    "direct_sender",
    "maroon_sender",
    "sf_client_blitz",
    "hunter",
    "warm_lead_closer",
    "nurture_sequence",
]

# Canonical funnel, top -> bottom. Each stage matches by substring against the ledger's
# `stage` and `lane` fields (lowercased). Order is what makes "earliest collapse" meaningful.
FUNNEL = [
    ("send",     ("send", "sent", "outreach", "outbound")),
    ("traffic",  ("open", "visit", "click", "preview", "monitor_run", "scan", "view", "impression")),
    ("capture",  ("email_capture", "capture", "lead", "signup", "opt_in", "subscribe")),
    ("checkout", ("cart", "checkout", "go_click", "intent", "pricing")),
    ("sale",     ("purchase", "sale", "paid", "charge", "order")),
]
STAGE_LABEL = {
    "send": "outreach sent",
    "traffic": "traffic / page views",
    "capture": "email captures (opt-ins)",
    "checkout": "checkout / offer clicks",
    "sale": "paid sales",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def load_decisions() -> dict:
    if DECISIONS_PRIMARY.exists():
        d = _read_json(DECISIONS_PRIMARY)
        if d:
            return d
    return _read_json(DECISIONS_FALLBACK)


def _classify_stage(row: dict) -> str | None:
    blob = f"{row.get('stage', '')} {row.get('lane', '')}".lower()
    for name, tokens in FUNNEL:
        if any(t in blob for t in tokens):
            return name
    return None


def funnel_counts() -> dict[str, int]:
    counts = {name: 0 for name, _ in FUNNEL}
    if not FUNNEL_LEDGER.exists():
        return counts
    for line in FUNNEL_LEDGER.read_text(errors="ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except Exception:
            continue
        if not isinstance(row, dict):
            continue
        stage = _classify_stage(row)
        if stage:
            counts[stage] += 1
    return counts


def find_binding_constraint(counts: dict[str, int]) -> dict:
    """Walk the funnel top->bottom; the binding constraint is the FIRST stage with real inflow
    whose conversion into everything downstream collapses below LEAK_FLOOR. Fixing upstream first
    is the whole point — a leak at the top makes every downstream number meaningless."""
    order = [name for name, _ in FUNNEL]
    for i, name in enumerate(order[:-1]):
        upstream = counts[name]
        if upstream < MIN_FLOW:
            continue
        downstream = sum(counts[n] for n in order[i + 1:])
        rate = downstream / upstream if upstream else 0.0
        if rate < LEAK_FLOOR:
            nxt = order[i + 1]
            return {
                "stage": name,
                "next_stage": nxt,
                "upstream": upstream,
                "downstream": downstream,
                "conversion_pct": round(rate * 100, 2),
                "kind": "funnel_leak",
            }
    # No collapse with enough volume: the real constraint is lack of measured flow at the top.
    top = order[0]
    if counts[top] < MIN_FLOW:
        return {"stage": top, "next_stage": order[1], "upstream": counts[top],
                "downstream": sum(counts[n] for n in order[1:]),
                "conversion_pct": None, "kind": "no_volume"}
    # Everything converting acceptably end-to-end.
    return {"stage": "sale", "next_stage": None, "upstream": counts[top],
            "downstream": counts["sale"], "conversion_pct": None, "kind": "healthy"}


def describe_remedy(stage: str, decisions: dict) -> str:
    """What is the LIVE machine actually doing about this stage right now? Reads the real config the
    responsible component consumes. Honest when the remedy is weak or absent."""
    if stage == "send":
        # senders + strategist outbound gate
        strat = _read_json(STRATEGY).get("directives", {})
        if strat.get("outbound_hold"):
            return ("revenue-strategist is HOLDING cold outbound (" +
                    str(strat.get("outbound_hold_reason", ""))[:80] + ") — so volume is intentionally paused.")
        return "senders (relentless_revenue / outreach engine) are clear to send; outbound is not the gate."
    if stage in ("traffic", "capture"):
        weights = _read_json(VARIANT_WEIGHTS)
        parts = []
        if weights:
            vals = sorted(weights.items(), key=lambda kv: kv[1], reverse=True)
            if len(set(round(v, 3) for v in weights.values())) <= 1:
                parts.append("variant-optimizer is still EXPLORE-ONLY (equal hook weights) — not enough "
                             "instrumented sends to pick a winning hook yet")
            else:
                parts.append(f"variant-optimizer is favoring the '{vals[0][0]}' hook ({vals[0][1]})")
        if LEAD_FUNNEL.exists():
            parts.append("lead funnel (free quick-check -> email capture) is live at /quick-check")
        # surface the brain's own stalled action if it's about follow-up/engagement
        for a in (decisions.get("last_actions") or []):
            act = str(a.get("action", "")).lower()
            if any(k in act for k in ("follow", "lead", "capture", "engage", "outreach")):
                st = a.get("status", "?")
                if st in ("logged_for_review", "queued"):
                    parts.append(f"brain's top capture action '{str(a.get('action'))[:60]}' is status={st} "
                                 "(NOT executed) — this is the gap")
                break
        return "; ".join(parts) if parts else "no automated remedy is tuning capture — needs attention."
    if stage == "checkout":
        return ("/go intent_sku_router binds buyers to a confirmed SKU + funnel link; if clicks aren't "
                "becoming checkouts the offer/price is the lever, not traffic.")
    if stage == "sale":
        return ("Stripe checkout is the rail; captures aren't converting to paid — the offer/pricing on "
                "the live storefront is the binding lever.")
    return "no mapped automated remedy for this stage."


def build_message(constraint: dict, counts: dict, decisions: dict) -> tuple[str, str]:
    """Returns (constraint_key, human_message). constraint_key is used for 12h dedup."""
    stage = constraint["stage"]
    label = STAGE_LABEL.get(stage, stage)
    kind = constraint["kind"]
    funnel_line = " -> ".join(f"{STAGE_LABEL[n]}:{counts[n]}" for n, _ in FUNNEL)

    if kind == "no_volume":
        key = "no_volume"
        head = (f"binding constraint: NO measured top-of-funnel volume — only {constraint['upstream']} "
                f"outreach events on the ledger. The machine can't convert what it isn't sending/tracking.")
    elif kind == "healthy":
        key = "healthy"
        head = "no single binding constraint — the funnel is converting end-to-end within thresholds."
    else:
        key = f"leak:{stage}->{constraint['next_stage']}"
        head = (f"binding constraint: the funnel chokes at {label}. "
                f"{constraint['upstream']} {label} produced only {constraint['downstream']} downstream "
                f"({constraint['conversion_pct']}% convert past this stage).")

    remedy = describe_remedy(stage, decisions)
    msg = (
        "[bottleneck] " + _now().astimezone().strftime("%Y-%m-%d %H:%M") + "\n"
        + head + "\n"
        + "what the system is doing: " + remedy + "\n"
        + "funnel: " + funnel_line
    )
    return key, msg


def _write_json_atomic(path: Path, payload: dict) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
        os.replace(tmp, path)
        return True
    except Exception as e:
        print(f"[diagnoser] write {path.name} failed: {e}", file=sys.stderr)
        return False


def _hold_reason(constraint: dict) -> str:
    """Aggregate-only, brand-free, PII-free hold reason (counts are not identity)."""
    up = constraint.get("upstream")
    down = constraint.get("downstream")
    pct = constraint.get("conversion_pct")
    return (
        f"deliverability constraint: {up} outreach sent produced only {down} traffic events "
        f"({pct}% past send) — holding cold outbound to protect sender reputation until landing/open "
        f"rate recovers"
    )


def apply_strategy_directive(constraint: dict) -> dict:
    """Turn the binding constraint into a REAL behavior change the live send path honors.

    On a send-stage deliverability collapse it flips strategy.json directives.outbound_hold
    (what products/outreach/compliance_guard.outbound_sends_enabled fail-closes on for every real
    send) and records a durable, TTL'd bottleneck_hold in strategy_overrides.json that
    agents/revenue_strategist.py carries forward. Additive + fail-soft; never raises.

    Returns an action dict recorded into the diagnosis artifact.
    """
    should_hold = bool(
        constraint.get("kind") in HOLD_KINDS and constraint.get("stage") in HOLD_STAGES
    )
    action = {"hold": should_hold, "wrote_override": False, "wrote_strategy": False, "reason": ""}

    # 1) Durable record the strategist preserves across regenerations.
    overrides = _read_json(STRATEGY_OVERRIDES)
    bh = {
        "active": should_hold,
        "set_at": _now().isoformat(),
        "expires_at": (_now() + timedelta(hours=HOLD_TTL_HOURS)).isoformat(),
        "kind": constraint.get("kind"),
        "stage": constraint.get("stage"),
        "components": list(HOLD_COMPONENTS),
        "reason": _hold_reason(constraint) if should_hold else "",
        "source": "bottleneck_diagnoser",
    }
    overrides["bottleneck_hold"] = bh
    overrides["updated_at"] = _now().isoformat()
    action["wrote_override"] = _write_json_atomic(STRATEGY_OVERRIDES, overrides)
    action["reason"] = bh["reason"]

    # 2) Immediate live switch — only ever ADD a hold here (turning a hold OFF is left to the
    #    strategist's next regeneration, which reads bottleneck_hold.active=false / its TTL, so we
    #    never stomp a hold the strategist set for its own reason).
    if should_hold:
        strat = _read_json(STRATEGY)
        directives = strat.get("directives")
        if isinstance(directives, dict):
            existing_reason = str(directives.get("outbound_hold_reason") or "").strip()
            merged_reason = (
                f"{existing_reason} | {bh['reason']}"
                if existing_reason and bh["reason"] not in existing_reason
                else (existing_reason or bh["reason"])
            )
            existing_comps = directives.get("outbound_hold_components") or []
            merged_comps = sorted({str(c) for c in existing_comps} | set(HOLD_COMPONENTS))
            directives["outbound_hold"] = True
            directives["outbound_hold_reason"] = merged_reason
            directives["outbound_hold_components"] = merged_comps
            directives["bottleneck_hold_active"] = True
            # neutralize stale priority/active surfacing while held
            directives["priority_segments"] = []
            strat["directives"] = directives
            action["wrote_strategy"] = _write_json_atomic(STRATEGY, strat)

    return action


def load_state() -> dict:
    return _read_json(STATE)


def save_state(state: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, indent=2))


def _hours_since(iso: str) -> float:
    try:
        prev = datetime.fromisoformat(iso)
        if prev.tzinfo is None:
            prev = prev.replace(tzinfo=timezone.utc)
        return (_now() - prev).total_seconds() / 3600.0
    except Exception:
        return 1e9


def run(dry: bool = False, force: bool = False) -> dict:
    decisions = load_decisions()
    counts = funnel_counts()
    constraint = find_binding_constraint(counts)

    # Act on the diagnosis: flip the live outbound-hold switch the senders/compliance path honor.
    # Skipped on --dry so a dry run never mutates the live send gate.
    action = {"hold": False}
    if not dry:
        try:
            action = apply_strategy_directive(constraint)
        except Exception as e:
            print(f"[diagnoser] apply_strategy_directive failed: {e}", file=sys.stderr)

    key, msg = build_message(constraint, counts, decisions)
    if action.get("hold"):
        msg += "\naction: outbound HOLD set (strategy.json directives.outbound_hold) — senders fail-close via compliance_guard."

    diagnosis = {
        "ts": _now().isoformat(),
        "constraint_key": key,
        "constraint": constraint,
        "funnel_counts": counts,
        "action": action,
        "message": msg,
    }
    # Always write the machine-readable artifact (real, additive — dashboards can read it).
    try:
        DIAGNOSIS_OUT.parent.mkdir(parents=True, exist_ok=True)
        DIAGNOSIS_OUT.write_text(json.dumps(diagnosis, indent=2))
    except Exception:
        pass

    print(msg)

    state = load_state()
    last_key = state.get("last_constraint_key")
    last_sent = state.get("last_sent", "")
    hrs = _hours_since(last_sent)
    due = force or (hrs >= DEDUP_HOURS) or (key != last_key and hrs >= DEDUP_HOURS)
    sent = False
    if dry:
        diagnosis["sent"] = False
        diagnosis["due"] = bool(due)
        return diagnosis
    if due:
        try:
            from tools.imsg_bridge import send_imessage
            send_imessage(msg)
            sent = True
        except Exception as e:
            print(f"imsg failed: {e}", file=sys.stderr)
        if sent:
            state["last_sent"] = _now().isoformat()
            state["last_constraint_key"] = key
            save_state(state)
    else:
        print(f"[skip send] {hrs:.1f}h since last (dedup {DEDUP_HOURS}h)")
    diagnosis["sent"] = sent
    return diagnosis


def main() -> int:
    dry = "--dry" in sys.argv or "--dry-run" in sys.argv
    force = "--force" in sys.argv
    run(dry=dry, force=force)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
