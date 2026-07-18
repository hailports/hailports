#!/usr/bin/env python3
"""Revenue brain — the closed learning loop for the outreach machine.

The senders fire blind: 181 broken-site sends, no per-variant attribution, no learning.
This ingests the REAL funnel from the real ledgers (sends -> replies -> leads -> $),
computes per-stage conversion with explicit math, finds the single binding constraint
(the stage actually killing the funnel), runs a Thompson-sampling bandit over an honest
variant bank, and WRITES data/revenue_decisions.json that live components READ:

  - agents/copy_rotation.pick_bandit_variant() reads the winning subject variant, so the
    NEXT broken-site send uses the bandit-chosen copy (closes the copy loop).
  - agents/broken_site_sender reads recommended_daily_volume as a CEILING, so the brain can
    actually throttle/hold real send volume (closes the volume loop).
  - the sender records which variant each send used in data/variant_outcomes.jsonl, so the
    bandit learns next cycle.

Anti-theater: this does not merely report a number. It writes config two live senders read
and reallocates real copy + real volume. Fail-soft everywhere: a missing file degrades to
today's behavior, never an exception that breaks a cron.

  python3 -m core.revenue_brain            # ingest real data, write decisions, print summary
  python3 -m core.revenue_brain --dry      # compute + print, do NOT write the decisions file
"""
from __future__ import annotations

import json
import math
import random
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HUSTLE = ROOT / "data" / "hustle"

VARIANT_BANK = ROOT / "data" / "variant_bank.json"
DECISIONS = ROOT / "data" / "revenue_decisions.json"
VARIANT_OUTCOMES = HUSTLE / "variant_outcomes.jsonl"

BROKEN_SITE_SENT = HUSTLE / "broken_site_sent.jsonl"
REPLIES = ROOT / "products" / "outreach" / "replies.json"
PAID_ORDERS = ROOT / "products" / "self_serve" / "paid_orders.jsonl"
FUNNEL_EVENTS = ROOT / "products" / "self_serve" / "funnel_events.jsonl"
COPY_VARIANT_EVENTS = HUSTLE / "copy_variant_events.jsonl"  # docsapp lane (copy_rotation)

# A reply only counts as a real downstream signal if it isn't a bounce/auto-ack.
_DEAD_REPLY = {"bounce", "auto_response", "auto_reply", "out_of_office"}
# Healthy benchmark for the top-of-funnel transition; below this with a confident sample =
# the copy/targeting is the wall. Conservative (cold B2B email reply rates run ~1-5%).
SEND_REPLY_BENCHMARK = 0.05
MIN_CONFIDENT_N = 30          # below this a 0% rate is "unknown", not "dead"
LANE_VOLUME_FLOOR = 6         # never throttle the only live lane below an exploration floor
LANE_VOLUME_CAP = 12          # brain never raises a live cap above this (deliverability safety)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_jsonl(path: Path):
    if not path.exists():
        return
    for line in path.read_text(errors="ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except Exception:
            continue


def _read_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(errors="ignore"))
    except Exception:
        return default


def _email_of(rec: dict) -> str:
    for k in ("reply_sender", "from_email", "email", "from", "to"):
        v = rec.get(k)
        if v:
            m = re.search(r"[\w.+-]+@[\w.-]+\.\w+", str(v))
            if m:
                return m.group(0).lower()
    return ""


# ---------------------------------------------------------------------------
# 1. INGEST — join recipient -> outcome from the real ledgers
# ---------------------------------------------------------------------------
def ingest() -> dict:
    """Build the per-lane funnel from real ledgers. recipient is the join key."""
    sends = [r for r in _read_jsonl(BROKEN_SITE_SENT)]
    ok_sends = [r for r in sends if r.get("ok", True)]
    bs_recipients = {_email_of(r) for r in ok_sends if _email_of(r)}

    # variant attribution: which variant each send used (filled by the sender going forward).
    outcomes = list(_read_jsonl(VARIANT_OUTCOMES))

    # replies: a real (non-bounce) reply whose sender we actually emailed on this lane.
    replies = _read_json(REPLIES, [])
    if not isinstance(replies, list):
        replies = []
    bs_replies = []
    for r in replies:
        cls = str(r.get("classification") or "").lower()
        seq = str(r.get("original_sequence") or "").lower()
        sender = _email_of(r)
        is_bs = ("broken" in seq or "rescue" in seq) or (sender and sender in bs_recipients)
        if is_bs and cls not in _DEAD_REPLY:
            bs_replies.append(r)
    reply_recipients = {_email_of(r) for r in bs_replies if _email_of(r)}

    # leads + revenue: self-serve captures and paid orders (exclude test rows).
    leads = 0
    for ev in _read_jsonl(FUNNEL_EVENTS):
        if "capture" in str(ev.get("event") or "").lower():
            leads += 1
    orders = [o for o in _read_jsonl(PAID_ORDERS)
              if "example.com" not in str(o.get("email") or "") and not o.get("unverified")]
    revenue_cents = sum(int(o.get("amount_total") or 0) for o in orders)

    return {
        "broken_site_sends": sends,
        "broken_site_ok_sends": ok_sends,
        "broken_site_recipients": bs_recipients,
        "broken_site_replies": bs_replies,
        "reply_recipients": reply_recipients,
        "variant_outcomes": outcomes,
        "self_serve_leads": leads,
        "paid_orders": orders,
        "revenue_cents": revenue_cents,
    }


# ---------------------------------------------------------------------------
# 2. COMPUTE — per-stage rates, Wilson bounds, binding constraint
# ---------------------------------------------------------------------------
def _wilson(num: int, den: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval — honest CI on a proportion at small n."""
    if den <= 0:
        return (0.0, 1.0)
    p = num / den
    z2 = z * z
    denom = 1 + z2 / den
    center = (p + z2 / (2 * den)) / denom
    margin = (z * math.sqrt((p * (1 - p) + z2 / (4 * den)) / den)) / denom
    return (max(0.0, center - margin), min(1.0, center + margin))


def _transition(num: int, den: int) -> dict:
    lo, hi = _wilson(num, den)
    return {
        "num": num, "den": den,
        "rate": round(num / den, 4) if den else 0.0,
        "wilson_lo": round(lo, 4), "wilson_hi": round(hi, 4),
        "n": den, "confident": den >= MIN_CONFIDENT_N,
    }


def compute_funnel(data: dict) -> dict:
    send = len(data["broken_site_ok_sends"])
    reply = len(data["reply_recipients"])
    lead = data["self_serve_leads"]
    checkout = len(data["paid_orders"])
    stages = {"send": send, "reply": reply, "lead": lead,
              "checkout": checkout, "revenue_cents": data["revenue_cents"]}
    transitions = {
        "send->reply": _transition(reply, send),
        "reply->lead": _transition(lead, reply),
        "lead->checkout": _transition(checkout, lead),
    }
    return {"stages": stages, "transitions": transitions}


def binding_constraint(funnel: dict) -> dict:
    """The single stage actually killing the funnel.

    A stage is the binding constraint if it is the EARLIEST transition that is provably
    dead: rate below benchmark with a confident sample (n >= MIN_CONFIDENT_N). A 0% rate
    on a tiny sample (e.g. 0 leads from 0-3 replies) is 'unknown', not 'the wall' -- you
    can't fix what you can't yet measure, so the brain points upstream where the evidence is.
    """
    t = funnel["transitions"]
    order = ["send->reply", "reply->lead", "lead->checkout"]
    benchmark = {"send->reply": SEND_REPLY_BENCHMARK, "reply->lead": 0.10, "lead->checkout": 0.05}
    for stage in order:
        tr = t[stage]
        # confident + below benchmark (or its whole CI sits below benchmark) = provable wall.
        if tr["confident"] and tr["wilson_hi"] < benchmark[stage]:
            up, down = stage.split("->")
            if stage == "send->reply":
                action = ("rotate bandit-chosen subject/copy per send AND record variant per "
                          "send (data/variant_outcomes.jsonl) so reply-rate per variant becomes "
                          "measurable; hold volume -- do NOT scale a funnel that earns no replies.")
            else:
                action = (f"build a downstream capture path for {up}s before adding volume; "
                          f"{up}->{down} is provably leaking.")
            return {
                "lane": "broken_site", "stage": stage,
                "rate": tr["rate"], "wilson_hi": tr["wilson_hi"], "n": tr["n"],
                "diagnosis": (f"{stage} = {tr['num']}/{tr['den']} = {tr['rate']:.4f} "
                              f"(95% CI upper {tr['wilson_hi']:.4f}) -- below the {benchmark[stage]:.0%} "
                              f"benchmark with a confident sample. This is where the funnel dies."),
                "action": action,
            }
    # nothing confidently dead yet -> the binding constraint is sample size itself.
    first_zero = next((s for s in order if t[s]["den"] == 0), None)
    if first_zero:
        up = first_zero.split("->")[0]
        return {
            "lane": "broken_site", "stage": first_zero, "rate": 0.0, "n": 0,
            "diagnosis": (f"no {up}s yet -- {first_zero} has zero denominator; the funnel is "
                          f"starved upstream, not converting-poorly downstream."),
            "action": ("keep exploring copy via the bandit and accumulate variant-tagged "
                       f"outcomes until {up} volume is measurable."),
        }
    return {"lane": "broken_site", "stage": "none", "rate": 0.0, "n": 0,
            "diagnosis": "every stage above benchmark or under-sampled.",
            "action": "scale the winning variant."}


# ---------------------------------------------------------------------------
# 3. BANDIT — Thompson sampling over the variant bank, learned from outcomes
# ---------------------------------------------------------------------------
def _bandit_stats(lane: str, pool: str, bank: dict, data: dict) -> dict:
    """alpha/beta per variant from real outcomes. Reward = the recipient replied (non-bounce).

    variant_outcomes.jsonl rows: {lane, pool, variant_id, to, event:'send'}. A send is a
    reward if that recipient is in reply_recipients. Beta(1,1) prior => uniform exploration
    when there's no data yet (today's honest state: 0 variant-tagged sends)."""
    variants = bank.get(lane, {}).get(pool, [])
    stats = {v["id"]: {"sends": 0, "replies": 0} for v in variants}
    reply_recips = data["reply_recipients"]
    for row in data["variant_outcomes"]:
        if row.get("lane") != lane or row.get("pool") != pool:
            continue
        vid = row.get("variant_id")
        if vid not in stats:
            continue
        stats[vid]["sends"] += 1
        if _email_of(row) in reply_recips:
            stats[vid]["replies"] += 1
    for vid, s in stats.items():
        s["alpha"] = 1 + s["replies"]
        s["beta"] = 1 + (s["sends"] - s["replies"])
        s["posterior_mean"] = round(s["alpha"] / (s["alpha"] + s["beta"]), 4)
    return stats


def thompson_pick(lane: str, pool: str, bank: dict, data: dict, rng: random.Random) -> dict:
    variants = bank.get(lane, {}).get(pool, [])
    if not variants:
        return {}
    stats = _bandit_stats(lane, pool, bank, data)
    best_id, best_sample = None, -1.0
    for v in variants:
        s = stats[v["id"]]
        sample = rng.betavariate(s["alpha"], s["beta"])
        if sample > best_sample:
            best_sample, best_id = sample, v["id"]
    chosen = next(v for v in variants if v["id"] == best_id)
    s = stats[best_id]
    return {
        "id": best_id, "template": chosen["template"],
        # carry the variant's optional body template through so the sender renders the WHOLE
        # bandit-chosen message (subject + body), not just the subject line. None when the
        # variant has no body (prep falls back to its proven default body).
        "body": chosen.get("body"), "method": "thompson",
        "alpha": s["alpha"], "beta": s["beta"], "posterior_mean": s["posterior_mean"],
        "sends": s["sends"], "replies": s["replies"],
        "thompson_sample": round(best_sample, 4),
        "_arms": {vid: {k: s2[k] for k in ("sends", "replies", "alpha", "beta", "posterior_mean")}
                  for vid, s2 in stats.items()},
    }


# ---------------------------------------------------------------------------
# 4. DECIDE + WRITE — the config live senders read
# ---------------------------------------------------------------------------
def recommend_volume(constraint: dict, funnel: dict) -> dict:
    """Reallocate real send volume. Throttle a confidently-dead converting lane; protect an
    exploration floor while the constraint is top-of-funnel (you need sends to test copy)."""
    stage = constraint.get("stage")
    # binding constraint is send->reply or starvation: the lane needs sends to learn copy,
    # but must NOT scale (leaky funnel). Hold at the floor, never above the safety cap.
    if stage in ("send->reply", "reply->lead", "lead->checkout"):
        vol = LANE_VOLUME_FLOOR if stage == "send->reply" else max(0, LANE_VOLUME_FLOOR // 2)
        note = ("hold at exploration floor while top-of-funnel copy is the constraint; "
                "do not scale a funnel that isn't earning replies yet."
                if stage == "send->reply"
                else f"throttle: {stage} is a confirmed downstream leak -- fix capture before volume.")
    else:
        vol = LANE_VOLUME_FLOOR
        note = "starved upstream; keep exploration floor and accumulate variant-tagged outcomes."
    return {"broken_site": min(vol, LANE_VOLUME_CAP), "_note": note, "_safety_cap": LANE_VOLUME_CAP}


def decide(seed: int | None = None) -> dict:
    bank = _read_json(VARIANT_BANK, {})
    data = ingest()
    funnel = compute_funnel(data)
    constraint = binding_constraint(funnel)
    rng = random.Random(seed) if seed is not None else random.Random()

    winning = {"broken_site": {}}
    for pool in bank.get("broken_site", {}):
        winning["broken_site"][pool] = thompson_pick("broken_site", pool, bank, data, rng)

    decisions = {
        "generated_at": _now(),
        "schema": 1,
        "data_window": {
            "broken_site_sends_ok": len(data["broken_site_ok_sends"]),
            "broken_site_real_replies": len(data["reply_recipients"]),
            "variant_tagged_sends": len(data["variant_outcomes"]),
            "self_serve_leads": data["self_serve_leads"],
            "paid_orders": len(data["paid_orders"]),
            "revenue_cents": data["revenue_cents"],
        },
        "funnel": {"broken_site": funnel},
        "binding_constraint": constraint,
        "winning_variants": winning,
        "recommended_daily_volume": recommend_volume(constraint, funnel),
    }
    return decisions


def main(argv: list[str]) -> int:
    dry = "--dry" in argv
    seed = None
    for a in argv:
        if a.startswith("--seed="):
            try:
                seed = int(a.split("=", 1)[1])
            except Exception:
                seed = None
    d = decide(seed=seed)
    if not dry:
        DECISIONS.parent.mkdir(parents=True, exist_ok=True)
        DECISIONS.write_text(json.dumps(d, indent=2) + "\n")
    bc = d["binding_constraint"]
    print(json.dumps({
        "wrote": None if dry else str(DECISIONS),
        "data_window": d["data_window"],
        "binding_constraint": {"stage": bc["stage"], "diagnosis": bc["diagnosis"], "action": bc["action"]},
        "winning_subject": {p: v.get("id") for p, v in d["winning_variants"]["broken_site"].items()},
        "recommended_daily_volume": d["recommended_daily_volume"],
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
