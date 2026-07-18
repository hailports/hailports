"""self_tuning — components auto-tune their own NOISE/EFFICIENCY thresholds from real
outcomes, so the stack sharpens itself without Operator lifting a finger.

The frontier here is: get *better* on its own — but only where being wrong is cheap and
reversible. Every proposal must be grounded in EVIDENCE (N observations + the metric it
improves); a blind tweak is refused. Safety rails and firewall thresholds are NEVER
auto-tuned — tuning is for cooldowns, dedup limits, and detector sensitivity, not for the
gates that keep lanes separate or keep anonymity fail-closed.

Pipeline per tunable:
    evidence_fn(ctx) -> candidate | None
        -> _guard()            (rail? bounds? enough evidence? real change?)
        -> [dry] log proposal / refusal to data/learning/tuning_proposals.jsonl
        -> [apply] decision_modeling.score() must NOT reject
                   -> self_verify() (bounds + rail + reversible + evidence, belt & braces)
                   -> _apply()  (write reversible override + append applied ledger)

Nothing here mutates another module's source. An applied tuning is a persisted OVERRIDE
(data/learning/tuning_overrides.json) that consumers opt into via `effective(param, default)`;
it always records the prior value so a rollback is a one-line revert.

Run:
    python -m core.self_tuning --run     # dry: propose only (default)
    python -m core.self_tuning --apply   # gated apply
    python -m core.self_tuning --smoke    # self-test
"""
from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

if __package__ in (None, ""):  # allow direct `python core/self_tuning.py`
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import BASE_DIR
from core import decision_modeling

LEARN = BASE_DIR / "data" / "learning"
PROPOSALS = LEARN / "tuning_proposals.jsonl"
OVERRIDES = LEARN / "tuning_overrides.json"
APPLIED = LEARN / "tuning_applied.jsonl"

ALERT_STATE = BASE_DIR / "data" / "alert_gateway_state.json"
GRADED_LEDGER = LEARN / "graded_outcomes.jsonl"

DAY = 86400.0


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# override store — the reversible "applied" surface consumers opt into
# ---------------------------------------------------------------------------
def _load_overrides() -> dict:
    try:
        return json.loads(OVERRIDES.read_text())
    except Exception:
        return {}


def effective(param_id: str, default):
    """Consumers call this to honor an applied tuning; falls back to their own default.

    A module wires in one line, e.g.
        CHRONIC_COOLDOWN = self_tuning.effective("alert_gateway.chronic_cooldown", 24*3600)
    Until a module opts in, an applied override is inert — so a bad tune can never silently
    change behavior in a place nobody asked it to.
    """
    ov = _load_overrides().get(param_id)
    if isinstance(ov, dict) and "value" in ov:
        return ov["value"]
    return default


# ---------------------------------------------------------------------------
# tunable registry
# ---------------------------------------------------------------------------
@dataclass
class Tunable:
    id: str                       # stable override key, e.g. "alert_gateway.chronic_cooldown"
    module: str
    param: str
    lo: float                     # hard lower bound (never below)
    hi: float                     # hard upper bound (never above)
    default: float                # baseline if no override present
    min_obs: int                  # evidence floor
    rail: bool                    # safety/firewall knob -> NEVER auto-tuned
    evidence_fn: Callable[[dict], Optional[dict]] = field(repr=False, default=None)
    note: str = ""

    def current(self) -> float:
        return float(effective(self.id, self.default))


@dataclass
class Verdict:
    allow: bool
    reason: str


def _guard(t: Tunable, cand: dict) -> Verdict:
    """Decide whether a candidate may even be PROPOSED for apply. Fail-closed."""
    # 1. rails are categorically excluded — never widened, never disabled, never touched.
    if t.rail:
        return Verdict(False, "safety rail — excluded from auto-tuning (rails are never "
                              "widened or disabled)")

    old = t.current()
    new = cand.get("new_value")
    if new is None:
        return Verdict(False, "no candidate value")

    # 2. bounds — clamp is a refusal signal, not a silent squeeze: if evidence points past a
    #    bound, we honor the bound and say so, we don't pretend the bound wasn't hit.
    clamped = max(t.lo, min(t.hi, float(new)))
    if clamped != float(new):
        cand["new_value"] = clamped
        new = clamped

    # 3. a tune must actually change something within bounds.
    if abs(new - old) < 1e-9:
        return Verdict(False, "already at target / at bound — no change")

    # 4. never widen a firewall value even on a non-rail (defense in depth: a direction
    #    marked unsafe on the candidate is rejected).
    if cand.get("unsafe_direction"):
        return Verdict(False, "proposed change moves a safety-relevant value the unsafe way")

    # 5. evidence floor.
    n = int(cand.get("n_obs", 0))
    if n < t.min_obs:
        return Verdict(False, f"insufficient evidence (n={n} < {t.min_obs})")

    return Verdict(True, "ok")


def self_verify(t: Tunable, cand: dict) -> tuple[bool, str]:
    """Independent re-check right before apply. Repeats the load-bearing invariants so an
    apply can never ride on a stale guard decision."""
    if t.rail:
        return False, "rail"
    new = float(cand["new_value"])
    if not (t.lo <= new <= t.hi):
        return False, f"out of bounds [{t.lo},{t.hi}]"
    if "old_value" not in cand:
        return False, "not reversible (old value not captured)"
    if int(cand.get("n_obs", 0)) < t.min_obs:
        return False, "evidence floor"
    if abs(new - float(cand["old_value"])) < 1e-9:
        return False, "no-op"
    return True, "ok"


# ---------------------------------------------------------------------------
# evidence functions — each reads a real outcome ledger and grounds a candidate
# ---------------------------------------------------------------------------
def _read_alert_state(ctx: dict) -> dict:
    p = Path(ctx.get("alert_state", ALERT_STATE))
    try:
        return json.loads(p.read_text())
    except Exception:
        return {}


def _worst_chronic(ctx: dict) -> Optional[dict]:
    """Find the issue re-firing hardest in its own 24h window. Uses the latest page as the
    window reference so synthetic fixtures don't depend on wall-clock."""
    state = _read_alert_state(ctx)
    issues = state.get("issues", {}) or {}
    worst = None
    for ik, rec in issues.items():
        pages = [float(x) for x in (rec.get("pages_24h") or [])]
        if not pages:
            continue
        ref = max(pages)
        window = [x for x in pages if ref - x < DAY]
        n = len(window)
        if worst is None or n > worst["n"]:
            worst = {"issue_key": ik, "n": n, "subject": rec.get("subject", ik)}
    return worst


def _ev_chronic_cooldown(ctx: dict) -> Optional[dict]:
    """A chronic that keeps re-firing well past the page limit => its cooldown is too short.
    Propose LENGTHENING the chronic cooldown (tighten = fewer repeat pages)."""
    w = _worst_chronic(ctx)
    if not w:
        return None
    limit = int(effective("alert_gateway.chronic_page_limit",
                          ctx.get("chronic_page_limit", 3)))
    # only act when it's firing at least 2x past the limit — that's the "keeps re-firing" bar.
    if w["n"] < max(2, limit * 2):
        return None
    old = float(effective("alert_gateway.chronic_cooldown", 24 * 3600))
    new = old * 1.5
    return {
        "new_value": new,
        "old_value": old,
        "n_obs": w["n"],
        "metric": f"chronic '{w['subject'][:48]}' paged {w['n']}x/24h (limit {limit}); "
                  f"lengthening cooldown cuts repeat pages",
        "detail": {"issue_key": w["issue_key"], "pages_24h": w["n"], "page_limit": limit},
    }


def _ev_chronic_page_limit(ctx: dict) -> Optional[dict]:
    """Same signal, complementary knob: if a chronic blows way past the limit, LOWER the
    page limit so the breaker trips sooner next time."""
    w = _worst_chronic(ctx)
    if not w:
        return None
    limit = int(effective("alert_gateway.chronic_page_limit",
                          ctx.get("chronic_page_limit", 3)))
    if w["n"] < max(3, limit * 2):  # only tighten the trip point on a *severe* overrun
        return None
    old = float(limit)
    new = max(1.0, old - 1.0)
    return {
        "new_value": new,
        "old_value": old,
        "n_obs": w["n"],
        "metric": f"chronic '{w['subject'][:48]}' overran limit {limit} by {w['n']}x; "
                  f"tripping the breaker one page sooner",
        "detail": {"issue_key": w["issue_key"], "pages_24h": w["n"]},
    }


def _ev_reaction_sim(ctx: dict) -> Optional[dict]:
    """reaction_grader flags a 'miss' when Operator re-asks and token-similarity >= threshold
    (0.5). If most misses are borderline (sim just over threshold) the detector is noisy —
    RAISE the threshold so weak matches stop counting as misses."""
    p = Path(ctx.get("graded_ledger", GRADED_LEDGER))
    rows = []
    try:
        for ln in p.read_text().splitlines():
            ln = ln.strip()
            if ln:
                rows.append(json.loads(ln))
    except Exception:
        return None
    thr = float(effective("reaction_grader.sim_threshold", 0.5))
    misses = [r for r in rows if r.get("label") == "miss"]
    if not misses:
        return None
    borderline = [r for r in misses
                  if thr <= float(r.get("confidence", 0)) < thr + 0.08]
    frac = len(borderline) / len(misses)
    if len(borderline) < 5 or frac <= 0.5:
        return None
    old = thr
    new = min(0.7, round(thr + 0.05, 2))
    return {
        "new_value": new,
        "old_value": old,
        "n_obs": len(borderline),
        "metric": f"{len(borderline)}/{len(misses)} misses ({frac*100:.0f}%) are borderline "
                  f"(sim<{thr+0.08:.2f}); raising threshold trims false misses",
        "detail": {"borderline": len(borderline), "total_misses": len(misses)},
    }


def _ev_anon_gate(ctx: dict) -> Optional[dict]:
    """SAFETY RAIL demo evidence. Given a pile of anon_scrub blocks, a naive noise-reduction
    instinct would LOWER the publish gate to cut 'false positives'. This function deliberately
    produces that (unsafe) candidate so we can prove the guard REFUSES it — the anonymity gate
    is fail-closed and never auto-loosened."""
    blocks = ctx.get("anon_blocks")
    if not blocks:
        return None
    old = float(effective("anon_scrub.gate_threshold", 1.0))
    return {
        "new_value": max(0.0, old - 0.3),  # loosen — this is exactly what must be refused
        "old_value": old,
        "n_obs": len(blocks),
        "metric": f"{len(blocks)} publish blocks — (naive) loosen the anonymity gate",
        "detail": {"blocks": len(blocks)},
    }


def _ev_inbox_confidence(ctx: dict) -> Optional[dict]:
    """Inbox draft-confidence gate: if auto-drafts below the gate were mostly corrected while
    those above were accepted, the gate is fine; if many just-above-gate drafts got corrected,
    RAISE the gate. Reads an optional outcomes list; inert without data (no live ledger yet)."""
    outs = ctx.get("inbox_outcomes")
    if not outs:
        return None
    gate = float(effective("inbox.draft_confidence_gate", 0.6))
    near = [o for o in outs if gate <= float(o.get("confidence", 0)) < gate + 0.1]
    bad = [o for o in near if o.get("outcome") == "corrected"]
    if len(near) < 5 or len(bad) / max(1, len(near)) <= 0.5:
        return None
    old = gate
    new = min(0.9, round(gate + 0.05, 2))
    return {
        "new_value": new,
        "old_value": old,
        "n_obs": len(near),
        "metric": f"{len(bad)}/{len(near)} just-above-gate drafts were corrected; "
                  f"raising the draft-confidence gate",
        "detail": {"near_gate": len(near), "corrected": len(bad)},
    }


TUNABLES: list[Tunable] = [
    Tunable("alert_gateway.chronic_cooldown", "alert_gateway", "CHRONIC_COOLDOWN",
            lo=6 * 3600, hi=3 * DAY, default=24 * 3600, min_obs=6, rail=False,
            evidence_fn=_ev_chronic_cooldown,
            note="how long a chronic crit is throttled once the breaker trips"),
    Tunable("alert_gateway.chronic_page_limit", "alert_gateway", "CHRONIC_PAGE_LIMIT",
            lo=1, hi=5, default=3, min_obs=6, rail=False,
            evidence_fn=_ev_chronic_page_limit,
            note="pages within 24h before the chronic breaker trips"),
    Tunable("reaction_grader.sim_threshold", "reaction_grader", "_similar>=",
            lo=0.4, hi=0.7, default=0.5, min_obs=5, rail=False,
            evidence_fn=_ev_reaction_sim,
            note="token-similarity above which a re-ask is graded a 'miss'"),
    Tunable("inbox.draft_confidence_gate", "work_reply_voice", "DRAFT_CONF_GATE",
            lo=0.4, hi=0.9, default=0.6, min_obs=5, rail=False,
            evidence_fn=_ev_inbox_confidence,
            note="min confidence to auto-draft an inbox reply"),
    # --- rails: registered so intent is explicit, but categorically never tuned ---
    Tunable("anon_scrub.gate_threshold", "anon_scrub", "PUBLISH_GATE",
            lo=1.0, hi=1.0, default=1.0, min_obs=1, rail=True,
            evidence_fn=_ev_anon_gate,
            note="RAIL: fail-closed anonymity publish gate — never auto-loosened"),
]


# ---------------------------------------------------------------------------
# apply
# ---------------------------------------------------------------------------
def _apply(t: Tunable, cand: dict) -> dict:
    ov = _load_overrides()
    old = cand["old_value"]
    new = cand["new_value"]
    ov[t.id] = {"value": new, "prev": old, "at": _now_iso(),
                "evidence": cand.get("metric", "")}
    OVERRIDES.parent.mkdir(parents=True, exist_ok=True)
    OVERRIDES.write_text(json.dumps(ov, indent=1))
    rec = {"at": _now_iso(), "id": t.id, "module": t.module, "param": t.param,
           "old": old, "new": new, "n_obs": cand.get("n_obs"),
           "metric": cand.get("metric")}
    with APPLIED.open("a") as f:
        f.write(json.dumps(rec) + "\n")
    return rec


def _log_proposal(rec: dict) -> None:
    PROPOSALS.parent.mkdir(parents=True, exist_ok=True)
    with PROPOSALS.open("a") as f:
        f.write(json.dumps(rec) + "\n")


# ---------------------------------------------------------------------------
# main entry
# ---------------------------------------------------------------------------
def tune(dry: bool = True, ctx: Optional[dict] = None, log: bool = True) -> dict:
    """Read outcome ledgers, propose evidence-grounded tunings, and (dry=False) apply the
    ones that clear the decision-model + self-verify gates. Returns a structured summary."""
    ctx = ctx or {}
    proposals, refusals, applied, blocked = [], [], [], []

    for t in TUNABLES:
        try:
            cand = t.evidence_fn(ctx) if t.evidence_fn else None
        except Exception as e:  # a broken evidence fn must never take the tuner down
            cand = None
            if log:
                _log_proposal({"at": _now_iso(), "id": t.id, "status": "evidence_error",
                               "error": str(e)})
        if not cand:
            continue

        verdict = _guard(t, cand)
        base = {"at": _now_iso(), "id": t.id, "module": t.module, "param": t.param,
                "current": t.current(), "proposed": cand.get("new_value"),
                "n_obs": cand.get("n_obs"), "metric": cand.get("metric"),
                "rail": t.rail}

        if not verdict.allow:
            rec = {**base, "status": "refused", "reason": verdict.reason}
            refusals.append(rec)
            if log:
                _log_proposal(rec)
            continue

        rec = {**base, "status": "proposed"}
        proposals.append(rec)
        if log:
            _log_proposal(rec)

        if dry:
            continue

        # --- gated apply ---
        action = (f"auto-tune {t.module}.{t.param}: {cand['old_value']} -> "
                  f"{cand['new_value']} to reduce noise ({cand['metric']})")
        score = decision_modeling.score(action, {"self_tuning": True, "reversible": True})
        if score.get("alex_would") == decision_modeling.REJECT:
            blocked.append({**rec, "status": "blocked", "reason": "decision_modeling reject",
                            "why": score.get("why")})
            if log:
                _log_proposal(blocked[-1])
            continue

        ok, why = self_verify(t, cand)
        if not ok:
            blocked.append({**rec, "status": "blocked", "reason": f"self_verify: {why}"})
            if log:
                _log_proposal(blocked[-1])
            continue

        arec = _apply(t, cand)
        applied.append(arec)

    return {"dry": dry, "proposals": proposals, "refusals": refusals,
            "applied": applied, "blocked": blocked}


# ---------------------------------------------------------------------------
# smoke test
# ---------------------------------------------------------------------------
def _smoke() -> int:
    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="self_tuning_smoke_"))
    ok = True

    def check(label, cond):
        nonlocal ok
        print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
        ok = ok and cond

    # synthetic alert state: a chronic paging 8x in its 24h window (limit 3) + one quiet issue
    ref = time.time()
    alert_state = {"issues": {
        "outcome-heal:intent_leads": {
            "subject": "self-heal CHRONIC: intent_leads stalled",
            "active": True,
            "pages_24h": [ref - i * 1800 for i in range(8)],  # 8 pages, all within 24h
        },
        "quiet_issue": {
            "subject": "occasional blip",
            "pages_24h": [ref - 100],  # 1 page — not chronic
        },
    }}
    astate = tmp / "alert_gateway_state.json"
    astate.write_text(json.dumps(alert_state))

    # synthetic graded outcomes: mostly borderline misses (sim just over 0.5)
    graded = tmp / "graded_outcomes.jsonl"
    with graded.open("w") as f:
        for i in range(7):
            f.write(json.dumps({"label": "miss", "confidence": 0.52}) + "\n")
        f.write(json.dumps({"label": "miss", "confidence": 0.9}) + "\n")
        f.write(json.dumps({"label": "hit", "confidence": 0.8}) + "\n")

    ctx = {
        "alert_state": astate,
        "graded_ledger": graded,
        "chronic_page_limit": 3,
        # feed the RAIL's evidence so we can prove the refusal fires
        "anon_blocks": [{"i": i} for i in range(20)],
    }

    # redirect proposal log into the sandbox so the smoke never touches live ledgers
    global PROPOSALS, OVERRIDES, APPLIED
    PROPOSALS = tmp / "tuning_proposals.jsonl"
    OVERRIDES = tmp / "tuning_overrides.json"
    APPLIED = tmp / "tuning_applied.jsonl"

    out = tune(dry=True, ctx=ctx)

    by_id = {p["id"]: p for p in out["proposals"]}
    refby_id = {r["id"]: r for r in out["refusals"]}

    # 1. chronic cooldown proposal fires, tightens (lengthens), with evidence
    cd = by_id.get("alert_gateway.chronic_cooldown")
    check("chronic cooldown PROPOSED", cd is not None)
    if cd:
        check("cooldown tightened (proposed > current)", cd["proposed"] > cd["current"])
        check("cooldown proposal carries N observations (=8)", cd["n_obs"] == 8)
        check("cooldown proposal names the metric it improves", bool(cd["metric"]))
        check("cooldown within hard bound", cd["proposed"] <= 3 * DAY)

    # 2. page-limit breaker also proposes tripping sooner (8x is a severe overrun of 3)
    pl = by_id.get("alert_gateway.chronic_page_limit")
    check("chronic page-limit PROPOSED (tighter)", pl is not None and pl["proposed"] < pl["current"])

    # 3. reaction-grader threshold raised on noisy borderline misses
    rg = by_id.get("reaction_grader.sim_threshold")
    check("reaction sim-threshold PROPOSED (raised)", rg is not None and rg["proposed"] > rg["current"])

    # 4. SAFETY RAIL: candidate existed but was REFUSED, never proposed
    check("anon gate NOT in proposals", "anon_scrub.gate_threshold" not in by_id)
    rail_ref = refby_id.get("anon_scrub.gate_threshold")
    check("anon gate explicitly REFUSED", rail_ref is not None)
    if rail_ref:
        check("refusal cites safety rail", "rail" in rail_ref["reason"].lower())

    # 5. dry run applied nothing
    check("dry run applied nothing", out["applied"] == [])

    # 6. quiet (non-chronic) issue did not trigger the breaker
    check("no proposal from the single-page issue only", cd is not None and cd["n_obs"] == 8)

    # 7. proposals + refusal were logged to the ledger
    logged = PROPOSALS.read_text().strip().splitlines() if PROPOSALS.exists() else []
    check("proposals + refusal written to ledger", len(logged) >= 4)

    # 8. a rail can never be applied even if forced through apply mode
    out2 = tune(dry=False, ctx={"anon_blocks": [{"i": 0}] * 5})
    rail_applied = any(a["id"] == "anon_scrub.gate_threshold" for a in out2["applied"])
    check("rail NEVER applied even in apply mode", not rail_applied)

    print(f"\n  smoke {'OK' if ok else 'FAILED'} (sandbox: {tmp})")
    return 0 if ok else 1


def _print_summary(out: dict) -> None:
    print(f"self_tuning (dry={out['dry']}): {len(out['proposals'])} proposed, "
          f"{len(out['refusals'])} refused, {len(out['applied'])} applied, "
          f"{len(out['blocked'])} blocked")
    for p in out["proposals"]:
        print(f"  PROPOSE {p['id']}: {p['current']} -> {p['proposed']}  ({p['metric']})")
    for r in out["refusals"]:
        print(f"  REFUSE  {r['id']}: {r['reason']}")
    for a in out["applied"]:
        print(f"  APPLIED {a['id']}: {a['old']} -> {a['new']}")
    for b in out["blocked"]:
        print(f"  BLOCKED {b['id']}: {b['reason']}")


def main(argv: list[str]) -> int:
    if "--smoke" in argv:
        return _smoke()
    dry = "--apply" not in argv
    out = tune(dry=dry)
    _print_summary(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
