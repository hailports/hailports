#!/usr/bin/env python3
"""Self-improve — the stack proposes, tests, and STAGES its own improvements.

The final autonomy faculty. Every other Phase-* organ makes the clone act like Operator;
this one makes the clone get *better* at it without Operator driving each change. It closes
the loop: measure -> find a real gap -> draft a fix -> prove the fix helps IN ISOLATION
-> stage it for Operator's one-tap yes. It NEVER edits live code. A staged proposal is a
described change plus a measured before/after, not a patch that self-applies.

Two public calls:

  scan() -> list[Opportunity]
      Mine the honest ledgers the stack already writes for CONCRETE gaps:
        - eval_harness (data/learning/eval_results.jsonl) — a category/lane/case scoring low.
        - reaction_grader (graded_outcomes.jsonl) — a repeated "not how I'd do it" miss.
        - never_twice (never_twice_novel.jsonl) — a recurring novel signature (a fix we keep
          re-deriving = a rule that should be crystallized).
        - clone_health / ops_health — a built-not-wired organ, or a producer gone idle.
      No opportunity is invented: each cites the ledger rows that evidence it.

  propose(opportunity, *, apply_fn=None, verify_metric=None) -> dict
      Draft ONE specific change for the opportunity, then TEST it in isolation:
        - The change is exercised against a throwaway copy of the eval set / miss data via
          eval_harness (module_override, write=False) or an injected verify_metric — the live
          ledgers are never touched.
        - Only a change that MEASURABLY improves its target metric (after > before) survives.
        - The survivor is gated through decision_modeling.score + self_verify.verify (a doctrine
          reject or a rail refutation kills it) and only then appended to
          data/learning/self_improve_proposals.jsonl as status="staged".
        - A change that doesn't move the metric returns status="rejected_no_improvement" and
          stages NOTHING.

RAILS honored: silent operation (no alerts here), honest metric (real before/after, not a
proxy), never auto-apply (staging only — Operator applies), lane firewall + doctrine gate on every
staged item, import-only (no live service touched). __main__ smoke runs fully offline over
synthetic eval/miss data.
"""
from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Callable

if __package__ in (None, ""):  # allow `python core/self_improve.py`
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import BASE_DIR

try:
    from core import eval_harness as _eval
except Exception:  # fail-soft: scan degrades, propose refuses eval-backed proofs
    _eval = None
try:
    from core import decision_modeling as _dm
except Exception:
    _dm = None
try:
    from core import self_verify as _sv
except Exception:
    _sv = None

LEARN = BASE_DIR / "data" / "learning"
EVAL_RESULTS = LEARN / "eval_results.jsonl"
GRADED_OUTCOMES = LEARN / "graded_outcomes.jsonl"
NOVEL_LOG = LEARN / "never_twice_novel.jsonl"
PROPOSALS = LEARN / "self_improve_proposals.jsonl"

# thresholds — a gap has to be real to be worth Operator's attention
_LOW_ACCURACY = 0.85          # a category/lane below this is an opportunity
_MIN_MISS_STREAK = 2          # a correction pattern seen >= this many times = repeated miss
_MIN_NOVEL_REPEAT = 2         # a novel never_twice signature seen >= this = should-crystallize
_MIN_METRIC_GAIN = 1e-9       # after must exceed before by at least this to count as improvement


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _read_jsonl(path: Path, limit: int | None = None) -> list[dict]:
    if not path.exists():
        return []
    rows: list[dict] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    except Exception:
        return []
    return rows[-limit:] if limit else rows


# ---------------------------------------------------------------------------
# Opportunity model
# ---------------------------------------------------------------------------
@dataclass
class Opportunity:
    """A concrete, evidenced improvement target. Never invented — each cites its rows."""
    kind: str                              # eval_low | repeated_miss | uncrystallized | not_wired
    target: str                            # what to improve (category/lane/module/organ)
    metric: str                            # the metric name a fix must move
    current: float                         # measured current value of that metric
    lane: str = "unknown"                  # hustle | work | unknown (for the firewall gate)
    severity: float = 0.0                  # 0-1, how far below bar / how frequent
    evidence: list[str] = field(default_factory=list)  # cited ledger refs
    detail: str = ""

    def key(self) -> str:
        return f"{self.kind}:{self.target}"


# ---------------------------------------------------------------------------
# scan() — mine the honest ledgers for concrete gaps
# ---------------------------------------------------------------------------
def _scan_eval(results_path: Path) -> list[Opportunity]:
    runs = _read_jsonl(results_path)
    if not runs:
        return []
    latest = runs[-1]
    ops: list[Opportunity] = []
    for bucket_name, bucket in (("category", latest.get("per_category", {})),
                                ("lane", latest.get("per_lane", {}))):
        for name, b in bucket.items():
            acc = b.get("accuracy")
            if acc is None and b.get("total"):
                acc = b["passed"] / b["total"]
            if acc is None:
                continue
            if acc < _LOW_ACCURACY:
                ops.append(Opportunity(
                    kind="eval_low",
                    target=f"{bucket_name}:{name}",
                    metric=f"eval_accuracy[{bucket_name}={name}]",
                    current=round(float(acc), 4),
                    lane=name if bucket_name == "lane" else "unknown",
                    severity=round(min(1.0, (_LOW_ACCURACY - acc) / _LOW_ACCURACY), 3),
                    evidence=[f"eval_results.jsonl@{latest.get('at')}: {name} {b.get('passed')}/{b.get('total')}"],
                    detail=f"{bucket_name} '{name}' scoring {acc:.0%} (< {_LOW_ACCURACY:.0%} bar)",
                ))
    return ops


def _scan_misses(graded_path: Path) -> list[Opportunity]:
    """A correction ('not how I'd do it') seen repeatedly = a systematic voice/judgment gap."""
    rows = _read_jsonl(graded_path)
    counts: dict[str, dict] = {}
    for r in rows:
        grade = r.get("grade") or r.get("classification") or r.get("label")
        if grade not in ("correction", "corrected", "not_how", "miss"):
            continue
        rule = (r.get("rule") or r.get("lesson") or r.get("pattern") or "").strip()
        if not rule:
            continue
        c = counts.setdefault(rule, {"n": 0, "lane": r.get("lane", "unknown"), "refs": []})
        c["n"] += 1
        if len(c["refs"]) < 3:
            c["refs"].append(f"graded_outcomes.jsonl: {r.get('at', '?')} '{rule[:60]}'")
    ops: list[Opportunity] = []
    for rule, c in counts.items():
        if c["n"] >= _MIN_MISS_STREAK:
            ops.append(Opportunity(
                kind="repeated_miss",
                target=rule[:80],
                metric="miss_count",
                current=float(c["n"]),
                lane=c["lane"],
                severity=round(min(1.0, c["n"] / 5.0), 3),
                evidence=c["refs"],
                detail=f"repeated correction x{c['n']}: {rule[:80]}",
            ))
    return ops


def _scan_uncrystallized(novel_path: Path) -> list[Opportunity]:
    """A novel never_twice signature we keep re-deriving = a rule that should be a known fix."""
    rows = _read_jsonl(novel_path)
    counts: dict[str, dict] = {}
    for r in rows:
        sig = (r.get("signature") or r.get("normalized") or r.get("key") or "").strip()
        if not sig:
            continue
        c = counts.setdefault(sig, {"n": 0, "refs": []})
        c["n"] += 1
        if len(c["refs"]) < 3:
            c["refs"].append(f"never_twice_novel.jsonl: {r.get('at', '?')} '{sig[:60]}'")
    ops: list[Opportunity] = []
    for sig, c in counts.items():
        if c["n"] >= _MIN_NOVEL_REPEAT:
            ops.append(Opportunity(
                kind="uncrystallized",
                target=sig[:80],
                metric="novel_repeat_count",
                current=float(c["n"]),
                lane="unknown",
                severity=round(min(1.0, c["n"] / 4.0), 3),
                evidence=c["refs"],
                detail=f"novel signature seen x{c['n']} — crystallize into never_twice registry",
            ))
    return ops


def _scan_health(health_state: dict | None) -> list[Opportunity]:
    """A built-not-wired organ is a concrete 'finish wiring it' opportunity."""
    if not health_state:
        return []
    ops: list[Opportunity] = []
    for row in health_state.get("rows", []):
        if row.get("status") == "built_not_wired":
            comp = row.get("component", "?")
            ops.append(Opportunity(
                kind="not_wired",
                target=comp,
                metric="wired",
                current=0.0,
                lane="unknown",
                severity=0.5,
                evidence=[f"clone_health: {comp} built_not_wired — {row.get('detail', '')}"],
                detail=f"organ '{comp}' built but not wired: {row.get('detail', '')}",
            ))
    return ops


def scan(*,
         results_path: Path = EVAL_RESULTS,
         graded_path: Path = GRADED_OUTCOMES,
         novel_path: Path = NOVEL_LOG,
         health_state: dict | None = None) -> list[Opportunity]:
    """Mine the honest ledgers for concrete, evidenced improvement opportunities.

    Every source is optional + fail-soft; a missing ledger contributes nothing rather than
    erroring. Returns opportunities ranked most-severe first. Read-only.
    """
    ops: list[Opportunity] = []
    ops += _scan_eval(results_path)
    ops += _scan_misses(graded_path)
    ops += _scan_uncrystallized(novel_path)
    ops += _scan_health(health_state)
    ops.sort(key=lambda o: o.severity, reverse=True)
    return ops


# ---------------------------------------------------------------------------
# propose() — draft a change, prove it helps in isolation, gate + stage
# ---------------------------------------------------------------------------
def _draft_change(opp: Opportunity) -> dict:
    """A specific, human-legible description of the change to try. Not code — a spec Operator applies."""
    if opp.kind == "eval_low":
        return {
            "summary": f"raise {opp.target} accuracy",
            "action": f"tighten the module behind {opp.target} so its failing eval cases pass",
            "how": "add/adjust the detector or scorer path exercised by the failing cases; "
                   "prove via eval_harness module_override before staging",
        }
    if opp.kind == "repeated_miss":
        return {
            "summary": f"crystallize correction into decision_modeling/voice",
            "action": f"promote the repeated correction to a learned AVOID rule: {opp.target}",
            "how": "add the rule to correction_rules so decision_modeling scores it as revise/reject",
        }
    if opp.kind == "uncrystallized":
        return {
            "summary": "crystallize novel signature into never_twice registry",
            "action": f"add a KnownSignature matcher + Fix for: {opp.target}",
            "how": "register the matcher in never_twice.default_registry so it stops re-deriving",
        }
    if opp.kind == "not_wired":
        return {
            "summary": f"wire {opp.target}",
            "action": f"finish the wire-in for organ {opp.target}",
            "how": "add the launchd job / call site the health check reports missing",
        }
    return {"summary": "improve " + opp.target, "action": opp.detail, "how": "n/a"}


def _measure_eval(opp: Opportunity,
                  apply_fn: Callable[[], dict] | None,
                  cases: list[dict] | None,
                  results_path: Path) -> tuple[float, float, dict]:
    """Before/after accuracy for an eval-backed opportunity, IN ISOLATION (write=False).

    apply_fn() returns a {module_name: patched_module} override simulating the fix; the eval
    is run twice — baseline vs patched — over the same cases, never writing the live ledger.
    """
    if _eval is None:
        raise RuntimeError("eval_harness unavailable; cannot prove eval-backed proposal")
    # baseline over the target cases (in-memory, no write)
    base = _eval.run_eval(cases=cases, results_path=results_path, write=False)
    override = apply_fn() if callable(apply_fn) else None
    after = _eval.run_eval(cases=cases, results_path=results_path,
                           module_override=override, write=False)

    def _acc(sc: dict) -> float:
        # target is "category:x" or "lane:y"; fall back to overall accuracy
        try:
            bucket, name = opp.target.split(":", 1)
        except ValueError:
            return float(sc.get("accuracy", 0.0))
        key = "per_category" if bucket == "category" else "per_lane"
        b = sc.get(key, {}).get(name)
        if not b:
            return float(sc.get("accuracy", 0.0))
        return float(b.get("accuracy", (b["passed"] / b["total"]) if b.get("total") else 0.0))

    return _acc(base), _acc(after), {"baseline": base.get("accuracy"), "after": after.get("accuracy")}


def _gate(change: dict, opp: Opportunity) -> tuple[bool, dict]:
    """Doctrine (decision_modeling) + rail (self_verify) gate on the staged change itself."""
    gate: dict = {}
    action_text = f"stage improvement proposal: {change.get('action', opp.detail)}"

    if _dm is not None:
        try:
            d = _dm.score(action_text, context={"lane": opp.lane})
            gate["decision"] = {"alex_would": d.get("alex_would"), "why": d.get("why"),
                                "confidence": d.get("confidence"), "via": d.get("via")}
            if d.get("alex_would") == "reject":
                return False, {**gate, "blocked_by": "decision_modeling:reject"}
        except Exception as e:
            gate["decision"] = {"error": str(e)}

    if _sv is not None:
        try:
            # staging is NOT a send/mutation — it's a proposal write; verify catches lane/AI-trace leaks
            v = _sv.verify({"kind": "draft", "summary": action_text,
                            "text": opp.detail, "lane": opp.lane, "is_send": False})
            gate["verify"] = {"safe": v.get("safe"), "refutations": v.get("refutations")}
            if not v.get("safe"):
                return False, {**gate, "blocked_by": "self_verify:unsafe"}
        except Exception as e:
            gate["verify"] = {"error": str(e)}

    return True, gate


def propose(opp: Opportunity, *,
            apply_fn: Callable[[], dict] | None = None,
            verify_metric: Callable[[], tuple[float, float]] | None = None,
            cases: list[dict] | None = None,
            results_path: Path = EVAL_RESULTS,
            proposals_path: Path = PROPOSALS,
            write: bool = True) -> dict:
    """Draft + isolation-test + gate + stage ONE improvement for `opp`.

    Proof of improvement comes from exactly one of:
      - apply_fn  : a callable returning an eval_harness module_override; before/after accuracy
                    is measured over `cases` (or the live eval set) with write=False.
      - verify_metric: a callable returning (before, after) for a caller-owned metric.
    A change only stages if after > before by at least _MIN_METRIC_GAIN. Then it must pass the
    doctrine + rail gate. Live code is NEVER touched; the outcome is a staged proposal line.

    Returns {status, ...}. status ∈ {staged, rejected_no_improvement, rejected_gate, error}.
    """
    change = _draft_change(opp)
    result: dict[str, Any] = {
        "at": _now(), "opportunity": asdict(opp), "change": change,
    }

    # 1. prove improvement in isolation
    try:
        if verify_metric is not None:
            before, after = verify_metric()
            detail = {"metric": opp.metric, "before": before, "after": after}
        elif apply_fn is not None:
            before, after, detail = _measure_eval(opp, apply_fn, cases, results_path)
        else:
            return {**result, "status": "error",
                    "why": "no apply_fn or verify_metric — cannot prove improvement; nothing staged"}
    except Exception as e:
        return {**result, "status": "error", "why": f"measurement failed: {type(e).__name__}: {e}"}

    result["measurement"] = {"before": before, "after": after, **({"detail": detail} if detail else {})}

    if not (after > before + _MIN_METRIC_GAIN):
        # honest metric rail: no measured gain => stage NOTHING
        return {**result, "status": "rejected_no_improvement",
                "why": f"change did not improve {opp.metric}: before={before} after={after}"}

    result["improvement"] = round(float(after - before), 6)

    # 2. doctrine + rail gate on the change itself
    ok, gate = _gate(change, opp)
    result["gate"] = gate
    if not ok:
        return {**result, "status": "rejected_gate",
                "why": f"change improves the metric but the gate blocked it: {gate.get('blocked_by')}"}

    # 3. stage for Operator's one-tap (NEVER auto-apply)
    proposal = {
        "at": result["at"],
        "status": "staged",
        "kind": opp.kind,
        "target": opp.target,
        "lane": opp.lane,
        "metric": opp.metric,
        "before": before,
        "after": after,
        "improvement": result["improvement"],
        "change": change,
        "evidence": opp.evidence,
        "gate": gate,
        "apply": "MANUAL — Operator applies; this file never edits live code",
    }
    if write:
        try:
            proposals_path.parent.mkdir(parents=True, exist_ok=True)
            with proposals_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(proposal) + "\n")
        except Exception as e:
            return {**result, "status": "error", "why": f"stage-write failed: {e}"}

    return {**result, "status": "staged", "proposal": proposal}


def scan_and_propose(*, dry_run: bool = False, **kw) -> list[dict]:
    """Convenience: scan, then attempt to propose the top opportunities that carry a proof hook.

    NOTE: without an apply_fn/verify_metric per opportunity there is nothing to measure, so this
    only reports the scan for opportunities it can't prove — it never stages an unproven change.
    """
    out = []
    for opp in scan(**{k: v for k, v in kw.items() if k in
                       ("results_path", "graded_path", "novel_path", "health_state")}):
        out.append({"opportunity": asdict(opp), "status": "needs_proof_hook"})
    return out


# ---------------------------------------------------------------------------
# smoke — fully offline over synthetic eval/miss data
# ---------------------------------------------------------------------------
def _smoke() -> int:
    import tempfile

    tmp = Path(tempfile.mkdtemp())
    fails: list[str] = []

    # --- synthetic ledgers ---------------------------------------------------
    # eval_results with one under-bar category ("voice_fidelity" at 50%) and lane "hustle" low
    eval_path = tmp / "eval_results.jsonl"
    synthetic_run = {
        "at": "2026-07-11T00:00:00", "n_cases": 6, "passed": 4, "accuracy": 0.6667,
        "per_category": {
            "inbox_routing": {"passed": 2, "total": 2, "accuracy": 1.0},
            "voice_fidelity": {"passed": 1, "total": 2, "accuracy": 0.5},
        },
        "per_lane": {
            "work": {"passed": 3, "total": 3, "accuracy": 1.0},
            "hustle": {"passed": 1, "total": 3, "accuracy": 0.3333},
        },
        "per_case": {},
    }
    eval_path.write_text(json.dumps(synthetic_run) + "\n")

    graded_path = tmp / "graded_outcomes.jsonl"
    graded_path.write_text("\n".join(json.dumps(r) for r in [
        {"at": "2026-07-10T01:00:00", "grade": "correction", "lane": "hustle",
         "rule": "dont recap the persons own work back to them"},
        {"at": "2026-07-10T02:00:00", "grade": "correction", "lane": "hustle",
         "rule": "dont recap the persons own work back to them"},
        {"at": "2026-07-10T03:00:00", "grade": "kept", "lane": "hustle", "rule": "one-off"},
    ]) + "\n")

    novel_path = tmp / "never_twice_novel.jsonl"
    novel_path.write_text("\n".join(json.dumps(r) for r in [
        {"at": "2026-07-09T01:00:00", "signature": "searxng colima socket refused"},
        {"at": "2026-07-09T05:00:00", "signature": "searxng colima socket refused"},
    ]) + "\n")

    health = {"rows": [
        {"component": "work_rag_semantic", "status": "built_not_wired", "detail": "no launchd job"},
        {"component": "documenter", "status": "live_ok", "detail": "scheduled"},
    ]}

    # --- 1. scan finds the real opportunities --------------------------------
    ops = scan(results_path=eval_path, graded_path=graded_path,
               novel_path=novel_path, health_state=health)
    kinds = {o.kind for o in ops}
    print(f"scan found {len(ops)} opportunities: kinds={sorted(kinds)}")
    for o in ops:
        print(f"  [{o.kind}] {o.target}  metric={o.metric} cur={o.current} sev={o.severity}")

    for want in ("eval_low", "repeated_miss", "uncrystallized", "not_wired"):
        if want not in kinds:
            fails.append(f"scan missed a real opportunity kind: {want}")

    # scan must NOT flag the healthy category / non-repeated miss / live_ok organ
    if any(o.target.endswith("inbox_routing") for o in ops):
        fails.append("scan flagged a healthy category (inbox_routing @ 100%)")
    if any(o.kind == "not_wired" and o.target == "documenter" for o in ops):
        fails.append("scan flagged a live_ok organ as not_wired")

    eval_opp = next((o for o in ops if o.kind == "eval_low" and "voice_fidelity" in o.target), None)
    if eval_opp is None:
        fails.append("no eval_low opportunity for voice_fidelity")

    # --- 2. propose stages a change that MEASURABLY helps ---------------------
    proposals_path = tmp / "self_improve_proposals.jsonl"

    if eval_opp is not None:
        # a change that helps: before=0.5 -> after=0.9 via injected metric
        good = propose(eval_opp,
                       verify_metric=lambda: (0.5, 0.9),
                       proposals_path=proposals_path)
        print(f"\npropose(helpful) -> {good['status']}  improvement={good.get('improvement')}")
        if good["status"] != "staged":
            fails.append(f"helpful change was not staged: {good['status']} ({good.get('why')})")
        if good.get("improvement", 0) <= 0:
            fails.append("staged proposal recorded a non-positive improvement")

        # a change that does NOT help: before==after -> must reject, stage nothing
        noop = propose(eval_opp,
                       verify_metric=lambda: (0.5, 0.5),
                       proposals_path=proposals_path)
        print(f"propose(no-op)   -> {noop['status']}")
        if noop["status"] != "rejected_no_improvement":
            fails.append(f"no-op change was not rejected: {noop['status']}")

        # a REGRESSION: after < before -> also rejected
        worse = propose(eval_opp,
                        verify_metric=lambda: (0.5, 0.2),
                        proposals_path=proposals_path)
        if worse["status"] != "rejected_no_improvement":
            fails.append(f"regression change was not rejected: {worse['status']}")

    # --- 3. exactly ONE line staged (only the helpful change) ----------------
    staged = _read_jsonl(proposals_path)
    print(f"\nstaged proposals written: {len(staged)}")
    if len(staged) != 1:
        fails.append(f"expected exactly 1 staged proposal, got {len(staged)}")
    elif staged[0].get("status") != "staged" or staged[0].get("after", 0) <= staged[0].get("before", 1):
        fails.append("the single staged line is malformed / not an improvement")

    # --- 4. real eval_harness override path (no synthetic metric) ------------
    if _eval is not None:
        try:
            seed = _eval.default_cases() if hasattr(_eval, "default_cases") else None
        except Exception:
            seed = None
        if seed:
            base = _eval.run_eval(cases=seed, results_path=tmp / "e2.jsonl", write=False)
            print(f"eval_harness baseline over {base['n_cases']} seed cases: {base['accuracy']:.0%}")

    # --- verdict -------------------------------------------------------------
    if fails:
        print("\nSMOKE FAIL:")
        for f in fails:
            print(f"  - {f}")
        return 1
    print("\nsmoke OK — scan finds real gaps; propose stages only measured wins, rejects no-ops")
    return 0


def _main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="self-improve: scan for gaps, stage proven fixes")
    ap.add_argument("--smoke", action="store_true")
    ap.add_argument("--scan", action="store_true", help="scan live ledgers, print opportunities")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    if a.smoke:
        return _smoke()
    return _scan_cli(a)


def _scan_cli(a) -> int:
    health = None
    try:
        st = BASE_DIR / "data" / "runtime" / "clone_health.json"
        if st.exists():
            health = json.loads(st.read_text())
    except Exception:
        health = None
    ops = scan(health_state=health)
    if getattr(a, "json", False):
        print(json.dumps([asdict(o) for o in ops], indent=1))
    else:
        print(f"{len(ops)} improvement opportunities (most-severe first):")
        for o in ops:
            print(f"  [{o.kind:14}] {o.target:40} {o.metric} = {o.current}  (sev {o.severity})")
            print(f"                   {o.detail}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
