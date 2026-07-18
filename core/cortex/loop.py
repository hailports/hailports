"""cortex.loop — the closed OODA loop that ties it all together.

    SENSE   -> DIAGNOSE -> DECIDE -> ACT -> VERIFY -> LEARN

DECIDE classifies each change by reversibility; ACT routes it:
  internal -> core.strategist_actuators / cortex.portfolio (writes a config a live loop reads)
  code     -> cortex.code_actuator (gated, isolated, test+rail-gated, auto-rollback)
  outward  -> core.approval_ledger (staged for Operator; NEVER auto-sent)
VERIFY joins prior changes to real downstream outcomes (strategist_memory); LEARN records
each change->outcome. Default posture is a dry-run that mutates nothing; actuation requires
CORTEX_ENABLED=1 (internal) / CORTEX_SELF_CODE[_APPLY]=1 (code). Master kill: data/hustle/CORTEX_OFF.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from core.cortex import code_actuator, diagnose, gates, sensors

ROOT = gates.ROOT
STATE_DIR = ROOT / "data" / "cortex"
LAST_RUN = STATE_DIR / "last_run.json"
DIGEST = STATE_DIR / "digest.txt"
RUN_LOG = STATE_DIR / "runs.jsonl"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


# --- ACT: one dispatcher per reversibility class -----------------------------
def _act_internal(ch: dict, *, dry: bool) -> dict:
    typ = ch.get("type")
    if dry or not gates.internal_armed():
        return {"action": "would_actuate", "armed": gates.internal_armed()}
    try:
        if typ == "effort_reallocation":
            from core.cortex import portfolio

            state = sensors.sense()
            pol = portfolio.reallocate(state)
            safe = portfolio.apply_is_safe(pol) if hasattr(portfolio, "apply_is_safe") else True
            return {"action": "effort_policy_written" if safe else "effort_policy_unsafe_skipped",
                    "safe": bool(safe)}
        if typ in ("copy_variant", "harvest_query", "bofu_page"):
            from core import strategist_actuators
            from core.revenue_strategist_loop import _adapt_play

            adapted = _adapt_play({"type": typ, "lane": ch.get("target"), "params": ch.get("params", {})})
            if not strategist_actuators.is_auto_safe(adapted):
                return {"action": "not_auto_safe_routed"}
            res = strategist_actuators.launch(adapted)
            return {"action": "launched", "result": res}
    except Exception as e:
        return {"action": "error", "reason": str(e)[:180]}
    return {"action": "no_actuator"}


def _act_code(ch: dict, *, dry: bool) -> dict:
    change = {
        "target": ch.get("target"),
        "instruction": ch.get("params", {}).get("instruction") or ch.get("expected_impact") or ch.get("rationale"),
        "rationale": ch.get("rationale"),
        "tests": ch.get("params", {}).get("tests"),
        "candidate": ch.get("params", {}).get("candidate"),
    }
    return {"action": "code_actuator", "result": code_actuator.propose_code_change(change, dry=dry)}


def _act_outward(ch: dict, *, dry: bool) -> dict:
    if dry:
        return {"action": "would_route_to_owner"}
    try:
        from core.approval_ledger import queue_action

        rec = queue_action({
            "pipeline": "cortex", "kind": ch.get("type"), "title": f"[cortex] {ch.get('target')}",
            "rationale": ch.get("rationale"), "params": ch.get("params"),
            "approval_required": True,
            "approval_reason": "cortex proposal — owner GO/NO required (spend/identity/outward)",
        }, actor="cortex")
        return {"action": "routed_to_owner", "approval_id": rec.get("id") or rec.get("action_id")}
    except Exception as e:
        return {"action": "route_failed", "reason": str(e)[:160]}


_DISPATCH = {"internal": _act_internal, "code": _act_code, "outward": _act_outward}


# --- VERIFY + LEARN ----------------------------------------------------------
def _verify_and_learn(acted: list) -> dict:
    joined = 0
    try:
        from core import strategist_memory

        for a in acted:
            ch = a["change"]
            strategist_memory.record(
                {"type": ch.get("type"), "lane": ch.get("target"), "params": ch.get("params", {})},
                outcome={"decision": a["result"].get("action"), "reversibility": ch.get("reversibility")},
            )
        joined = len(strategist_memory.attach_outcomes() or [])
    except Exception:
        pass
    try:
        from core.health_ledger import record_cycle

        record_cycle()
    except Exception:
        pass
    return {"outcomes_joined": joined}


# --- the loop ----------------------------------------------------------------
def run(dry: bool | None = None) -> dict:
    if gates.is_off():
        return {"off": True, "note": "CORTEX_OFF present — no-op"}
    if dry is None:
        dry = not gates.internal_armed()

    state = sensors.sense()
    diag = diagnose.diagnose(state)
    acted = []
    for ch in diag["changes"]:
        fn = _DISPATCH.get(ch.get("reversibility"))
        result = fn(ch, dry=dry) if fn else {"action": "unknown_reversibility"}
        acted.append({"change": ch, "result": result})
    learn = _verify_and_learn(acted) if not dry else {"outcomes_joined": 0, "note": "dry"}

    run_rec = {
        "ts": _now(), "dry": dry, "arming": gates.arming(),
        "diagnose_source": diag["source"], "facts": diag["facts_count"],
        "changes": len(diag["changes"]), "dropped": len(diag["dropped"]),
        "acted": [{"track": a["change"].get("track"), "type": a["change"].get("type"),
                   "reversibility": a["change"].get("reversibility"),
                   "target": a["change"].get("target"), "action": a["result"].get("action")}
                  for a in acted],
        "learn": learn,
    }
    _persist(run_rec, diag, acted)
    return run_rec


def _persist(run_rec: dict, diag: dict, acted: list) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        LAST_RUN.write_text(json.dumps({"run": run_rec, "changes": diag["changes"]}, indent=2, default=str))
        with RUN_LOG.open("a") as f:
            f.write(json.dumps(run_rec, default=str) + "\n")
        lines = [f"cortex {run_rec['ts']} ({'DRY' if run_rec['dry'] else 'ARMED'}) "
                 f"src={run_rec['diagnose_source']} changes={run_rec['changes']}:"]
        for a in acted:
            c = a["change"]
            lines.append(f"  [{c.get('reversibility')}] {c.get('track')}/{c.get('type')} "
                         f"-> {a['result'].get('action')} :: {str(c.get('rationale',''))[:90]}")
        DIGEST.write_text("\n".join(lines) + "\n")
    except Exception:
        pass


def main(argv: list) -> int:
    dry = "--dry" in argv or not gates.internal_armed()
    if "--arm" in argv:  # convenience: force a real (armed) run if flags allow
        dry = False
    out = run(dry=dry)
    print(json.dumps(out, indent=2, default=str))
    return 0


def _selftest() -> int:
    import os

    for e in ("CORTEX_ENABLED", "CORTEX_SELF_CODE", "CORTEX_SELF_CODE_APPLY"):
        os.environ.pop(e, None)
    before = _snapshot_side_effect_files()
    out = run(dry=True)
    after = _snapshot_side_effect_files()
    assert out.get("dry") is True, "disarmed run must be dry"
    assert all(a["action"].startswith("would") or a["action"] in ("code_actuator",)
               for a in [x for x in out["acted"]]) or not out["acted"], \
        f"dry run took a non-would action: {out['acted']}"
    # dry run must not have written a live effort_policy / launched a strategist play
    leaked = [f for f in (before | after) if before.get(f) != after.get(f)
              and "cortex/" not in f]  # cortex/ run-log/digest writes are expected
    assert not leaked, f"dry run mutated non-cortex files: {leaked}"
    print(f"LOOP SELFTEST OK — dry run, source={out['diagnose_source']}, "
          f"changes={out['changes']}, no non-cortex side effects")
    return 0


def _snapshot_side_effect_files() -> dict:
    watch = [
        ROOT / "data" / "variant_bank.json",
        ROOT / "data" / "hustle" / "effort_policy.json",
        ROOT / "data" / "hustle" / "strategist_plays.jsonl",
        ROOT / "agents" / "multi_market_intent.py",
    ]
    out = {}
    for p in watch:
        try:
            out[str(p)] = p.stat().st_mtime if p.exists() else None
        except Exception:
            out[str(p)] = None
    return out


if __name__ == "__main__":
    import sys

    if "--selftest" in sys.argv:
        raise SystemExit(_selftest())
    raise SystemExit(main(sys.argv[1:]))
