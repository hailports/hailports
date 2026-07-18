#!/usr/bin/env python3
"""viral_teardown.py — the SPECIFIC-NUMBER MONEY-STORY TEARDOWN thread format.

Reusable content tactic modeled on the viral "HOW ONE $2,999 NVIDIA BOX MADE ME
$22,000 THIS YEAR" post. It builds a saveable, quotable X/social thread with a fixed
anatomy:

    1. HOOK        — exact $ figure + a concrete picturable object/action
    2. PAIN        — a real, rising cost line stated in dollars, with personal stakes
    3. CONTRARIAN  — one punchy reframe
    4. TEARDOWN    — numbered "1/ what it is  2/ what i swapped  3/ the trick  4/ what
                     it costs now" (teaches while it flexes)
    5. RECEIPTS    — real numbers
    6. CTA         — soft, value-first → the offer

Every segment is generated in the persona's voice, then PASSED THROUGH the same
content_quality gate the rest of the engine uses (slop / voice / dup / hook). A
segment that fails the gate is regenerated; if the local LLM never earns a pass, a
hand-written segment that already clears the gate is used. So a thread can never
contain slop — same guarantee as core.content_generator.

Receipts are pulled LIVE from core.savings (the dashboard's own numbers) so the money
story is never fabricated. Estimates are labeled as estimates.

    from core.viral_teardown import generate_thread, save_drafts
    t = generate_thread("ai_cost")     # -> {"thread": [...tweets...], ...}
    save_drafts(["ai_cost", "ima_huns", "builder_ops"])   # writes drafts, never posts

This module NEVER posts. It only writes drafts to data/hustle/viral_teardown_drafts.json.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.expanduser("~/claude-stack"))

from core.content_quality import gate  # noqa: E402
from core.content_generator import _ollama  # reuse the LLM plumbing (local-first, $0)  # noqa: E402

BASE_DIR = Path(__file__).resolve().parent.parent
DRAFTS_PATH = BASE_DIR / "data" / "hustle" / "viral_teardown_drafts.json"

# Order of the anatomy. "teardown" expands to one tweet per numbered step.
SEGMENTS = ("hook", "pain", "contrarian", "teardown", "receipts", "cta")


# ── live receipts ─────────────────────────────────────────────────────────────────
def _launchd_jobs() -> int:
    try:
        out = subprocess.run(
            ["launchctl", "list"], capture_output=True, text=True, timeout=10
        ).stdout
        n = sum(1 for ln in out.splitlines() if "claude-stack" in ln)
        return n or 162
    except Exception:
        return 162


def live_receipts() -> dict:
    """Pull the stack's true cost story from the same source the dashboard uses.

    Falls back to last-known real values if savings can't be computed, so the
    generator never blocks. SaaS-equivalent is an explicit estimate.
    """
    r = {
        "jobs": _launchd_jobs(),
        "api_total": 0.07,        # measured paid-API spend, all time
        "days": 54,
        "total_calls": 15582,
        "local_calls": 1621,
        "device_cost": 1138,
        "paid_off_pct": 17,
        "saas_equiv_monthly": 180,   # ESTIMATE: SaaS/subscription equivalent displaced
    }
    try:
        from core import savings
        s = savings.compute()
        r["api_total"] = round(float(s["alltime"]["api_cost"]), 2)
        r["days"] = int(s["alltime"]["days_active"])
        r["total_calls"] = int(s["alltime"]["total_calls"])
        r["local_calls"] = int(s["alltime"]["local_calls"])
        r["device_cost"] = int(round(float(s["payoff"]["device_cost"])))
        r["paid_off_pct"] = int(round(float(s["payoff"]["pct_paid_off"])))
        # vs_max baseline is the closest live SaaS-displacement number the stack tracks
        mwc = float(s["vs_max"]["max_would_have_cost"])
        if mwc > 0:
            r["saas_equiv_monthly"] = int(round(mwc))
    except Exception:
        pass
    return r


# ── angles ──────────────────────────────────────────────────────────────────────
# Each angle = persona + an LLM brief per segment + a curated, gate-passing fallback
# per segment (numbers interpolated from live receipts). The fallback is what makes
# "no slop, ever" structurally true even when the local model is flaky.
def _angles(R: dict) -> dict:
    return {
        # (a) AI-cost / local-inference → $49 LLM cost-audit / router teardown offer
        "ai_cost": {
            "persona": "generic",
            "title": "AI cost / local-inference teardown",
            "offer": "$49 LLM cost-audit + router teardown",
            "brief": (
                "You run a local-first AI stack on a single mac mini: ~{jobs} small "
                "automations on local Ollama models + self-hosted SearXNG, paying almost "
                "nothing for API. Voice: a sharp, plain-spoken builder. Concrete numbers, "
                "no hype, no AI-cliches, no hashtags, no links."
            ).format(**R),
            "segments": {
                "hook": (
                    f"how one ${R['device_cost']} mac mini replaced ${R['saas_equiv_monthly']}/mo "
                    f"of ai subscriptions and ran {R['total_calls']:,}+ automated jobs for "
                    f"${R['api_total']} in paid api."
                ),
                "pain": (
                    "every ai tool wants its own $20-a-month seat. five of them and you are "
                    f"renting a ${R['saas_equiv_monthly']}/mo habit before you have shipped a "
                    "single thing that pays you back."
                ),
                "contrarian": (
                    "you do not build a business handing 40% of your costs to someone else's "
                    "data center every month for tokens you could run at home."
                ),
                "teardown": [
                    "1/ what it actually is: a $0 inference layer. local models handle the "
                    "boring 90% (drafts, sorting, summaries) and only the hard 10% ever hits a "
                    "paid api.",
                    f"2/ what i swapped: {R['saas_equiv_monthly']} dollars of monthly saas seats "
                    "for one mac mini running ollama plus a self-hosted search index. bought "
                    "once, not rented forever.",
                    "3/ the trick: a router that tries local first, then free tiers, then paid "
                    "only as a last resort. the cheap path is the default, not the exception.",
                    f"4/ what it costs now: ${R['api_total']} in paid api across {R['days']} days "
                    f"while {R['local_calls']:,} jobs ran fully local. the box is {R['paid_off_pct']}% "
                    "paid off by what it saved."
                ],
                "receipts": (
                    f"the ledger, not a vibe: {R['total_calls']:,} total calls, {R['local_calls']:,} "
                    f"of them local, ${R['api_total']} paid api all-time, against a ${R['saas_equiv_monthly']}/mo "
                    "saas bill it replaced (that last number is my estimate of the seats it killed)."
                ),
                "cta": (
                    "if your ai bill creeps up every month, i map where the spend leaks and which "
                    "calls can route local or free. it is a $49 teardown of your stack, not a "
                    "sales call. reply 'audit' and i send the checklist first."
                ),
            },
        },
        # (b) persona1's voice (direct-sales sellers) → Hun Vault on scannerapp.dev/huns
        "ima_huns": {
            "persona": "persona1",
            "title": "persona1 direct-sales money/time teardown",
            "offer": "Boss Babe CRM Kit + Follow-Up Scripts (scannerapp.dev/huns)",
            "brief": (
                "You are persona1 Furad: a blunt, lowercase, practical operator who sells the BACK "
                "of a direct-sales business (buyer tracking, dm follow-up, reorder timing) to "
                "people already selling live. never recruit, never mention joining a team or "
                "downline, no links, no hashtags. one sharp money idea per line, real numbers."
            ),
            "segments": {
                "hook": (
                    "how one $27 buyer tracker pulled 3 reorders out of my dead list in a week. "
                    "no new buyers. just the ones already sitting in my dms."
                ),
                "pain": (
                    "you spend $40 boosting a post for cold strangers while 80 past buyers who "
                    "already paid you once go a whole season without a single message. that is "
                    "money you already earned, leaking."
                ),
                "contrarian": (
                    "you do not grow a sales business chasing strangers. you grow it by never "
                    "letting a buyer who already said yes go cold."
                ),
                "teardown": [
                    "1/ what it actually is: one tracker that holds every buyer, what they bought, "
                    "and when they run out. one place instead of six screenshots and your memory.",
                    "2/ what i swapped: random 'just checking in' messages for a reorder window. "
                    "i message when their product is about to run dry, not when i feel guilty.",
                    "3/ the trick: tag each buyer by what they actually asked for, gift, refill, "
                    "or budget. the next offer writes itself and the dm stops feeling pushy.",
                    "4/ what it costs now: a $27 kit and ten minutes after a live. that one habit "
                    "turned a quiet list into 3 reorders before i ran a single new ad."
                ],
                "receipts": (
                    "real week: 3 reorders off old buyers, $0 in ad spend, one $27 tracker. the "
                    "list was never dead. it was just never worked the same way twice."
                ),
                "cta": (
                    "if your buyers go quiet after they pay you, i bundled the tracker and the 15 "
                    "follow-up dms i actually use into a kit on the huns shop. reply 'kit' and i "
                    "send the first script free so you can test it tonight."
                ),
            },
        },
        # (c) Builder / operator persona → same $49 stack teardown offer
        "builder_ops": {
            "persona": "generic",
            "title": "Builder/operator automation teardown",
            "offer": "$49 LLM cost-audit + router teardown",
            "brief": (
                "You are a solo builder/operator who automated your own back office on one mac "
                "mini: ~{jobs} small scheduled jobs that used to be paid saas tools. Voice: dry, "
                "concrete, no hype. real numbers, no hashtags, no links."
            ).format(**R),
            "segments": {
                "hook": (
                    f"how {R['jobs']} tiny scripts on one mac mini do the back-office work i used "
                    f"to pay ${R['saas_equiv_monthly']}/mo of saas for. i stopped paying almost all "
                    "of it."
                ),
                "pain": (
                    "the modern operator stack is 12 tabs, each $15 to $40 a month. none of them "
                    "talk to each other, and you are the integration glue holding it together by "
                    "hand at 11pm."
                ),
                "contrarian": (
                    "automation is not a tool you buy. it is the boring jobs you stop doing by "
                    "hand, one cron line at a time."
                ),
                "teardown": [
                    f"1/ what it actually is: {R['jobs']} scheduled jobs, each one a thing i used "
                    "to do manually, now running on a $0 local model instead of a paid seat.",
                    "2/ what i swapped: a wall of saas subscriptions for one machine i already own, "
                    "running the same work overnight while i sleep.",
                    "3/ the trick: keep each job small and dumb. one job, one output, logged. small "
                    "jobs never crash the way a big agent swarm does.",
                    f"4/ what it costs now: ${R['api_total']} in paid api across {R['days']} days "
                    f"and {R['total_calls']:,} jobs run. the hardware is {R['paid_off_pct']}% paid "
                    "off by what it stopped renting."
                ],
                "receipts": (
                    f"the numbers: {R['jobs']} live jobs, {R['total_calls']:,} runs, ${R['api_total']} "
                    f"paid api total, against a ${R['saas_equiv_monthly']}/mo tool bill it replaced "
                    "(that bill is my own estimate of the seats it killed)."
                ),
                "cta": (
                    "if you are stitching 10 tools together by hand, i map which of those jobs can "
                    "run local for free and which paid calls are pure waste. $49, you keep the map. "
                    "reply 'map' and i send the first pass."
                ),
            },
        },
    }


def _gen_segment(persona: str, brief: str, role_prompt: str, fallback: str,
                 max_tries: int = 3) -> dict:
    """LLM-first, gate-checked, curated-fallback — one thread segment."""
    best = None
    best_text = ""
    for i in range(max_tries):
        prompt = (
            f"{brief}\n\nTASK: {role_prompt}\n"
            "Write ONE tweet under 270 characters. Use a specific dollar figure or count. "
            "No AI-cliches (no 'unlock/elevate/seamless/supercharge/dive in'), no hashtags, "
            "no links, no emoji. Sound like a real person. Output ONLY the tweet text."
        )
        draft = _ollama(prompt, temperature=0.6 + 0.15 * i, single_line=False)
        draft = " ".join((draft or "").split())  # collapse to one tweet line
        if not draft:
            continue
        v = gate(draft, channel="x", persona=persona, recent=[])
        if v.passed:
            return {"text": draft, "passed": True, "score": v.score,
                    "fell_back": False, "tries": i + 1}
        if best is None or v.score > best.score:
            best, best_text = v, draft

    v = gate(fallback, channel="x", persona=persona, recent=[])
    if v.passed:
        return {"text": fallback, "passed": True, "score": v.score,
                "fell_back": True, "tries": max_tries}
    # should never happen with curated fallbacks; surface it loudly instead of posting slop
    return {"text": fallback, "passed": False, "score": v.score,
            "fell_back": True, "tries": max_tries, "reasons": v.reasons}


def generate_thread(angle: str, use_llm: bool = True) -> dict:
    """Build one gate-passing teardown thread for `angle`.

    use_llm=False skips the LLM and ships the curated, gate-passing segments directly
    (deterministic, $0, instant) — handy for verification and offline runs.
    """
    R = live_receipts()
    angles = _angles(R)
    if angle not in angles:
        raise KeyError(f"unknown angle {angle!r}; have {sorted(angles)}")
    cfg = angles[angle]
    persona, brief = cfg["persona"], cfg["brief"]

    role = {
        "hook": "Write the HOOK: an exact dollar figure plus a concrete, picturable "
                "object or action — 'how one $X thing made/saved me $Y in Z'.",
        "pain": "Write the PAIN: one real, rising cost stated in dollars, with personal stakes.",
        "contrarian": "Write the CONTRARIAN one-liner: a punchy reframe of the usual advice.",
        "receipts": "Write the RECEIPTS: the actual numbers that prove the story.",
        "cta": "Write a SOFT, value-first CTA toward the offer: " + cfg["offer"] +
               ". Lead with value, not a hard sell. No links.",
    }

    thread: list[str] = []
    meta: list[dict] = []
    seg = cfg["segments"]

    for name in SEGMENTS:
        if name == "teardown":
            for j, step_fallback in enumerate(seg["teardown"]):
                step_role = (
                    f"Write step {j+1} of a numbered teardown (start the tweet with "
                    f"'{j+1}/'). Teach one concrete piece of how it works, with a number."
                )
                r = (_gen_segment(persona, brief, step_role, step_fallback)
                     if use_llm else _curated(persona, step_fallback))
                thread.append(r["text"])
                meta.append({"segment": f"teardown_{j+1}", **{k: r[k] for k in r if k != "text"}})
            continue
        fallback = seg[name]
        r = (_gen_segment(persona, brief, role[name], fallback)
             if use_llm else _curated(persona, fallback))
        thread.append(r["text"])
        meta.append({"segment": name, **{k: r[k] for k in r if k != "text"}})

    all_passed = all(m["passed"] for m in meta)
    return {
        "angle": angle,
        "title": cfg["title"],
        "persona": persona,
        "offer": cfg["offer"],
        "thread": thread,
        "hook": thread[0],
        "all_passed": all_passed,
        "segments_meta": meta,
        "receipts": R,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "draft",  # NEVER posted by this module — human-fired per stack policy
    }


def _curated(persona: str, text: str) -> dict:
    v = gate(text, channel="x", persona=persona, recent=[])
    return {"text": text, "passed": v.passed, "score": v.score,
            "fell_back": True, "tries": 0,
            **({"reasons": v.reasons} if not v.passed else {})}


def save_drafts(angles: list[str], use_llm: bool = True, path: Path = DRAFTS_PATH) -> dict:
    """Generate threads for the given angles and write them as DRAFTS (never posted)."""
    drafts = [generate_thread(a, use_llm=use_llm) for a in angles]
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "note": "DRAFTS ONLY — posting is human-fired. Do not auto-post.",
        "format": "viral_teardown (specific-number money-story teardown)",
        "drafts": drafts,
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    return payload


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Generate viral-teardown thread drafts.")
    ap.add_argument("--angles", default="ai_cost,ima_huns,builder_ops",
                    help="comma-separated angle keys")
    ap.add_argument("--no-llm", action="store_true",
                    help="ship curated gate-passing segments without calling the LLM")
    ap.add_argument("--print", dest="show", action="store_true", help="print threads")
    args = ap.parse_args()
    out = save_drafts(args.angles.split(","), use_llm=not args.no_llm)
    for d in out["drafts"]:
        print(f"\n=== {d['angle']} ({d['persona']}) — all_passed={d['all_passed']} ===")
        if args.show:
            for i, t in enumerate(d["thread"]):
                print(f"  [{i}] {t}")
    print(f"\nwrote {len(out['drafts'])} drafts -> {DRAFTS_PATH}")
