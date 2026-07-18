"""cortex.diagnose — DIAGNOSE: turn machine-state into a grounded change-set.

This is where looping meets harnessing. When armed + within budget it shells to
scripts/agent_run.sh and asks Claude Code to run a DYNAMIC WORKFLOW — fan out
specialist reviewers (revenue / growth / reasoning-quality / harness-quality /
ops-health), each proposing changes, synthesized into one JSON change-set. It
fail-softs to the free-LLM pool, then to a fully deterministic diagnosis derived
straight from the state — so a dry run always yields concrete, grounded proposals
at $0 and the loop never goes dark.

Every change must cite a real number from the state (grounding), or it's dropped.

change = {
  track:        revenue|growth|reasoning|harness|ops
  type:         effort_reallocation|copy_variant|harvest_query|bofu_page|code_edit|route_owner
  reversibility internal | code | outward
  target:       actuator target / file path
  rationale:    one sentence, MUST quote a real number from the state
  expected_impact: short
  params:       {...}
}
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

from core.cortex import gates, sensors

ROOT = gates.ROOT
AGENT_RUN = ROOT / "scripts" / "agent_run.sh"

VALID_TRACKS = {"revenue", "growth", "reasoning", "harness", "ops"}
INTERNAL_TYPES = {"effort_reallocation", "copy_variant", "harvest_query", "bofu_page"}
CODE_TYPES = {"code_edit"}


def _prompt(state: dict) -> str:
    compact = json.dumps(state, indent=2, default=str)[:12000]
    return (
        "You are the diagnostic brain of an autonomous revenue+work stack. Use the Workflow tool "
        "to fan out FIVE specialist reviewers in parallel — revenue, growth, reasoning-quality, "
        "harness-quality, ops-health — each reading the machine-state below and proposing the single "
        "highest-leverage reversible change in its lane. Then synthesize their outputs.\n\n"
        "Return ONLY a JSON array (no prose) of 3-6 change objects with EXACTLY these keys: "
        "track, type, reversibility, target, rationale, expected_impact, params.\n"
        "Rules: track in [revenue,growth,reasoning,harness,ops]; type in [effort_reallocation,"
        "copy_variant,harvest_query,bofu_page,code_edit,route_owner]; reversibility 'internal' for the "
        "first four types, 'code' for code_edit, 'outward' for route_owner. Every rationale MUST quote a "
        "real number that appears in the state (no fabricated facts). copy_variant params.template must "
        "contain {company} and invent no stat/proof. Anything spending money, standing up a new identity/"
        "channel, or touching a named (BrandA) lane => type 'route_owner', reversibility 'outward'.\n\n"
        f"MACHINE-STATE:\n```json\n{compact}\n```"
    )


def _extract_array(text: str) -> list:
    if not text:
        return []
    m = re.search(r"\[.*\]", text, re.S)
    try:
        val = json.loads(m.group(0) if m else text)
        return val if isinstance(val, list) else []
    except Exception:
        return []


def _agent_diagnose(state: dict) -> list:
    """Dynamic-workflow diagnosis via the stack's agentic runner. Costs one budget unit."""
    if not gates.spend_one():
        return []
    prompt = _prompt(state)
    # Prefer the supervised harness (track B) which adds verify+retry; else raw agent_run.sh.
    try:
        from core.cortex import harness

        obj, _meta = harness.run_json(prompt, schema_hint="array", max_turns=12, max_retries=1)
        if isinstance(obj, list) and obj:
            return obj
    except Exception:
        pass
    try:
        if not AGENT_RUN.exists():
            return []
        p = subprocess.run(["bash", str(AGENT_RUN), prompt, "12"], cwd=str(ROOT),
                           capture_output=True, text=True, timeout=int(os.environ.get("CORTEX_DIAGNOSE_TIMEOUT", "1800")))
        return _extract_array(p.stdout)
    except Exception:
        return []


def _llm_diagnose(state: dict) -> list:
    try:
        import asyncio

        from core.free_llm_pool import try_free_providers

        text, _prov = asyncio.run(try_free_providers(
            _prompt(state).replace("Use the Workflow tool to fan out FIVE specialist reviewers in parallel",
                                   "Reason across five lenses"),
            system="Output strict JSON array only. No fabricated numbers.",
            max_tokens=1600, explicit=True, tier="strong"))
        return _extract_array(text or "")
    except Exception:
        return []


def _deterministic_diagnose(state: dict) -> list:
    """Grounded change-set derived straight from the state — no LLM, always runs."""
    changes: list = []

    # babysitting: the single highest "keeps needing Operator" driver -> routed to the owner with its
    # real count. When the agent-workflow path runs it can propose a systemic fix (e.g. harden the
    # session keepalive) instead; the deterministic path stays safe/staged.
    bs = (state.get("babysitting") or {}).get("top") or []
    if bs and isinstance(bs[0], dict):
        d = bs[0]
        changes.append({
            "track": "ops", "type": "route_owner", "reversibility": "outward",
            "target": str(d.get("key", "babysitting")),
            "rationale": f"{(state.get('babysitting') or {}).get('total')} recurring interventions tracked; "
                         f"#1 is {d.get('category')} (count {d.get('count')}): {str(d.get('evidence',''))[:100]}",
            "expected_impact": "kill the biggest recurring Operator-intervention at its source, not one-off",
            "params": {"driver": d},
        })

    # ops: chronic failures -> root-cause proposals routed to the owner (never auto-coded blind).
    chronic = (state.get("chronic_failures") or {}).get("items") or []
    for item in chronic[:2]:
        if not isinstance(item, dict):
            continue
        sig = item.get("signature") or item.get("service") or "recurring failure"
        cyc = item.get("recurrence_cycles") or item.get("cycles") or item.get("count")
        if cyc is None:
            continue
        changes.append({
            "track": "ops", "type": "route_owner", "reversibility": "outward",
            "target": str(item.get("service") or sig),
            "rationale": f"{sig} recurred {cyc} cycles (band_aid={item.get('band_aid')}) — root cause, not liveness.",
            "expected_impact": "stop band-aiding a chronic failure at its source",
            "params": {"chronic": item},
        })

    # revenue: propose a portfolio reallocation (track A). READ-ONLY here — the actual
    # effort_policy.json write is the ACTUATION, done in loop.act only when armed + not dry.
    port = state.get("portfolio") or {}
    if port.get("available") is not False:
        try:
            channels = port.get("channels") or port.get("items") or {}
            worst = None
            if isinstance(channels, dict) and channels:
                worst = min(channels.items(),
                            key=lambda kv: (kv[1] or {}).get("outcome", kv[1].get("weight", 1.0) if isinstance(kv[1], dict) else 1.0))
                name, metrics = worst
                num = None
                if isinstance(metrics, dict):
                    for k in ("outcome", "leads", "orders", "sends", "weight"):
                        if isinstance(metrics.get(k), (int, float)):
                            num = metrics[k]
                            break
                if num is not None:
                    changes.append({
                        "track": "revenue", "type": "effort_reallocation", "reversibility": "internal",
                        "target": str(name),
                        "rationale": f"channel {name} shows outcome metric {num} over the window — reweight effort.",
                        "expected_impact": "shift effort toward channels with real outcomes",
                        "params": {"channel": str(name), "metric": num},
                    })
        except Exception:
            pass

    # revenue/growth: fall back to the strategist's own grounded plays (copy/harvest/bofu).
    try:
        from core.revenue_strategist_loop import _fallback_generate

        for play in (_fallback_generate(state.get("strategist") or {}) or [])[:2]:
            t = play.get("type")
            if t not in INTERNAL_TYPES and t != "copy_variant":
                continue
            changes.append({
                "track": "growth" if t == "harvest_query" else "revenue",
                "type": t, "reversibility": "internal",
                "target": play.get("lane", "faceless"),
                "rationale": play.get("rationale", ""),
                "expected_impact": play.get("expected_impact", ""),
                "params": play.get("params", {}),
            })
    except Exception:
        pass

    return changes


def validate_change(change: dict, facts: set) -> tuple[bool, str]:
    if not isinstance(change, dict):
        return False, "not an object"
    if change.get("track") not in VALID_TRACKS:
        return False, f"bad track {change.get('track')!r}"
    typ = change.get("type")
    rev = change.get("reversibility")
    if typ in INTERNAL_TYPES and rev != "internal":
        return False, f"{typ} must be reversibility=internal"
    if typ in CODE_TYPES and rev != "code":
        return False, "code_edit must be reversibility=code"
    if typ == "route_owner" and rev != "outward":
        return False, "route_owner must be reversibility=outward"
    # a dot only counts as decimal when followed by digits — otherwise "count 1." greedily
    # captures "1." and never matches the fact "1", falsely dropping a grounded change.
    cited = set(re.findall(r"\d+(?:\.\d+)?", str(change.get("rationale", ""))))
    if not (cited & facts):
        return False, "ungrounded rationale (cites no real number from state)"
    if typ == "copy_variant":
        tmpl = str((change.get("params") or {}).get("template", ""))
        if "{company}" not in tmpl:
            return False, "copy_variant missing {company}"
    return True, "ok"


def diagnose(state: dict | None = None, *, allow_agent: bool | None = None, max_changes: int = 6) -> dict:
    """Produce a validated, grounded change-set. Returns {changes, source, dropped}."""
    state = state or sensors.sense()
    facts = sensors.grounding_facts(state)
    if allow_agent is None:
        allow_agent = gates.internal_armed()  # dry/disarmed runs stay at $0 (deterministic/free)

    raw, source = [], "deterministic"
    if allow_agent and gates.budget_remaining() > 0:
        raw = _agent_diagnose(state)
        source = "agent_workflow" if raw else source
    if not raw:
        raw = _llm_diagnose(state)
        source = "free_llm" if raw else source
    if not raw:
        raw = _deterministic_diagnose(state)
        source = "deterministic"

    valid, dropped = [], []
    for ch in raw:
        ok, why = validate_change(ch, facts)
        (valid if ok else dropped).append(ch if ok else {**(ch if isinstance(ch, dict) else {"raw": ch}), "_drop": why})
    return {"changes": valid[:max_changes], "dropped": dropped, "source": source,
            "facts_count": len(facts)}


def _selftest() -> int:
    # deterministic path must run on real state with no LLM and produce grounded changes.
    import os

    for e in ("CORTEX_ENABLED", "CORTEX_SELF_CODE"):
        os.environ.pop(e, None)
    out = diagnose(allow_agent=False)
    assert isinstance(out, dict) and "changes" in out, "diagnose must return {changes,...}"
    assert out["source"] in ("deterministic", "free_llm"), f"unexpected source {out['source']}"
    # every surviving change is grounded + well-typed
    facts = sensors.grounding_facts(sensors.sense())
    for ch in out["changes"]:
        ok, why = validate_change(ch, facts)
        assert ok, f"invalid change survived: {why} :: {ch}"
    # grounding actually bites: a fabricated-number rationale is dropped
    bad = {"track": "revenue", "type": "route_owner", "reversibility": "outward",
           "target": "x", "rationale": "made up 99999999 figure", "params": {}}
    ok, _ = validate_change(bad, facts)
    assert not ok or "99999999" in facts, "grounding failed to reject a fabricated number"
    print(f"DIAGNOSE SELFTEST OK — source={out['source']} changes={len(out['changes'])} "
          f"dropped={len(out['dropped'])} facts={out['facts_count']}")
    return 0


if __name__ == "__main__":
    import sys

    if "--selftest" in sys.argv:
        raise SystemExit(_selftest())
    print(json.dumps(diagnose(allow_agent=False), indent=2, default=str)[:6000])
