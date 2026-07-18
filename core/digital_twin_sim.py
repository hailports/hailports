#!/usr/bin/env python3
"""Digital-twin sim — DRY-RUN a risky mutation against a sandbox/temp twin before prod.

The Blake lesson: the clone reported `applied: true` on a permission grant that changed
nothing — the target already held the perm set, so the "fix" was a no-op dressed as a win.
self_verify catches that AFTER a structured mutation is described; this catches it BEFORE by
actually *running* the proposed change against a throwaway copy and measuring the effect.

simulate(action) takes a proposed mutation and, WITHOUT touching prod:
  1. builds a SANDBOX twin of the affected state (a temp file copy, a described SF state, a
     launchd load-state),
  2. APPLIES the proposed mutation to the twin,
  3. MEASURES before -> after and PREDICTS whether it is effective (changes state at all) and
     whether it ACHIEVES THE GOAL (moves state to what the caller actually wanted),
  4. gates the prediction through self_verify (so simulate() is a strict superset of the
     no-op/lane/doctrine rails), and returns the combined verdict.

Contract: any real mutation MUST pass simulate() first and abort on safe_to_apply=False.

Deterministic where possible. The real-world validators (sf CLI --dry-run against a sandbox
org, running a caller-supplied test against the temp copy) are OFF by default (run_real=False)
and only fire when the caller opts in AND the tool/org is present — the __main__ smoke runs
fully offline and never shells out, never touches prod, never writes a real path.

Dispatch by kind:
  file_edit / config_edit  -> copy source to a temp twin, apply the edit, diff, optional test
  sf_change / perm_grant   -> deterministic no-op prediction (Blake trap) + optional sandbox
                              `sf project deploy start --dry-run` compile-validation
  scheduler                -> predict whether activating a job actually starts something new
                              (already-loaded == no-op)

Additive + import-only. Fail-soft on self_verify. __main__ smoke is offline & prod-safe.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

if __package__ in (None, ""):  # allow `python core/digital_twin_sim.py`
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from core import self_verify as _sv
except Exception:  # fail-soft: the sim still predicts; the rails gate degrades to a no-op pass
    _sv = None


# ---------------------------------------------------------------------------
# proposed mutation
# ---------------------------------------------------------------------------
@dataclass
class Action:
    kind: str = "unknown"            # file_edit|config_edit|sf_change|perm_grant|scheduler
    goal: str = ""                   # what this change is supposed to ACHIEVE (plain words)
    summary: str = ""                # one-line description (fed to self_verify's doctrine gate)
    lane: str = "unknown"            # hustle|work|unknown (for the rails)
    surface: str = ""                # salesforce|launchd|filesystem|...

    # file_edit / config_edit
    path: str = ""                   # real source path (read-only; the twin is a copy)
    new_content: str | None = None   # proposed replacement content
    transform: Callable[[str], str] | None = None  # or a pure fn old_text -> new_text
    test: Callable[[str], Any] | None = None        # optional predicate(temp_path)->bool|(bool,str)

    # sf_change / perm_grant
    mutation: dict | None = None     # structured before/after (before_assigned, before, after, ...)
    source_dir: str = ""             # metadata dir for a sandbox --dry-run deploy
    target_org: str = ""             # sandbox/scratch org alias (NEVER a prod alias)

    # generic goal check against the predicted after-state
    goal_fn: Callable[[Any], Any] | None = None  # predicate(after)->bool|(bool,str)
    ctx: dict = field(default_factory=dict)


def _normalize(action: Any) -> Action:
    if isinstance(action, Action):
        return action
    if isinstance(action, str):
        return Action(summary=action.strip(), goal=action.strip())
    if isinstance(action, dict):
        d = dict(action)
        known = set(Action.__dataclass_fields__)
        kwargs = {k: v for k, v in d.items() if k in known}
        if not kwargs.get("summary"):
            kwargs["summary"] = d.get("description") or d.get("goal") or ""
        return Action(**kwargs)
    return Action(summary=str(action))


@dataclass
class Sim:
    achieves_goal: bool          # would the mutation move state to what the caller wanted
    effective: bool              # would the mutation change state AT ALL (not a no-op)
    safe_to_apply: bool          # sim prediction AND self_verify rails both clear
    method: str                  # how it was predicted (twin_copy|deterministic|sandbox_dry_run|...)
    goal: str
    predicted: dict              # before/after/delta evidence
    reasons: list[str]           # human-readable why
    verify: dict                 # the self_verify result (rails)

    def as_dict(self) -> dict:
        return {
            "achieves_goal": self.achieves_goal,
            "effective": self.effective,
            "safe_to_apply": self.safe_to_apply,
            "method": self.method,
            "goal": self.goal,
            "predicted": self.predicted,
            "reasons": self.reasons,
            "verify": self.verify,
        }


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _h(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:12]


def _as_bool_detail(res: Any) -> tuple[bool, str]:
    if isinstance(res, tuple):
        ok, detail = (list(res) + [""])[:2]
        return bool(ok), str(detail)
    return bool(res), ""


def _predict_noop(m: dict | None, kind: str) -> tuple[bool, str]:
    """Deterministic Blake-trap detector: is this structured mutation a no-op?

    Returns (is_noop, reason). A no-op is a mutation that measurably changes nothing.
    """
    if not isinstance(m, dict):
        return (False, "")
    if m.get("effective") is False or m.get("changed") is False:
        return (True, "mutation self-reports no effective change")
    if "before" in m and "after" in m and m["before"] == m["after"]:
        return (True, f"before == after ({m['before']!r})")
    if kind in ("perm_grant", "permission", "assignment") or m.get("kind") in (
        "perm_grant", "permission", "assignment"
    ):
        if m.get("before_assigned") and m.get("after_assigned"):
            return (True, f"target already holds '{m.get('permset', 'the perm set')}' — grant is a no-op (Blake trap)")
        mc = m.get("member_count")
        if mc is not None and mc == 0:
            return (True, f"'{m.get('permset', 'the perm set')}' would be assigned to 0 members")
    return (False, "")


# ---------------------------------------------------------------------------
# per-kind simulators — each returns (effective, achieves, method, predicted, reasons)
# ---------------------------------------------------------------------------
def _sim_file_edit(a: Action, run_real: bool) -> tuple:
    reasons: list[str] = []
    src = Path(a.path) if a.path else None
    before = ""
    if src and src.exists():
        before = src.read_text(encoding="utf-8", errors="replace")
    elif a.mutation and "before" in a.mutation:
        before = str(a.mutation["before"])

    # derive the proposed after-content
    if a.new_content is not None:
        after = a.new_content
    elif callable(a.transform):
        after = a.transform(before)
    elif a.mutation and "after" in a.mutation:
        after = str(a.mutation["after"])
    else:
        return (False, False, "twin_copy", {"before_hash": _h(before)},
                ["no proposed content (new_content/transform/mutation.after) — nothing to apply"])

    effective = after != before
    predicted = {
        "before_hash": _h(before),
        "after_hash": _h(after),
        "changed": effective,
        "diff": _unified(before, after, str(src or "content")),
    }
    if not effective:
        reasons.append("proposed content is identical to the source — no-op edit, changes nothing")

    achieves = effective
    method = "twin_copy"
    # apply to a temp TWIN and (optionally) run the caller's test against it — never the real path
    if callable(a.test):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=(src.suffix if src else ".txt"), delete=True,
            dir=_scratch(), encoding="utf-8",
        ) as tf:
            tf.write(after)
            tf.flush()
            try:
                ok, detail = _as_bool_detail(a.test(tf.name))
            except Exception as e:
                return (effective, False, "twin_copy",
                        {**predicted, "test": f"raised: {e}"},
                        reasons + [f"test against the twin raised: {e}"])
        method = "twin_copy+test"
        predicted["test_passed"] = ok
        achieves = effective and ok
        reasons.append(f"twin test: {'PASS' if ok else 'FAIL'}{(' - ' + detail) if detail else ''}")
        if not ok:
            reasons.append("edit applies but does not achieve the goal (twin test failed)")
    elif callable(a.goal_fn):
        ok, detail = _as_bool_detail(a.goal_fn(after))
        achieves = effective and ok
        reasons.append(f"goal check on predicted state: {'met' if ok else 'unmet'}{(' - ' + detail) if detail else ''}")

    return (effective, achieves, method, predicted, reasons)


def _sim_sf(a: Action, run_real: bool) -> tuple:
    reasons: list[str] = []
    m = a.mutation
    is_noop, why = _predict_noop(m, a.kind)
    if is_noop:
        reasons.append(f"predicted NO-OP: {why}")
        return (False, False, "deterministic", {"noop": True, "why": why, "mutation": m}, reasons)

    effective = True
    predicted: dict = {"noop": False, "mutation": m}
    if isinstance(m, dict) and "before" in m and "after" in m:
        predicted["delta"] = {"before": m["before"], "after": m["after"]}
        reasons.append(f"predicted change: {m['before']!r} -> {m['after']!r}")
    else:
        reasons.append("predicted an effective SF state change")

    method = "deterministic"
    achieves = True
    # optional: validate the metadata actually COMPILES/deploys against a SANDBOX via --dry-run.
    # never a prod org; only when the caller opts in and the CLI + sandbox alias are present.
    if run_real and a.source_dir and a.target_org and shutil.which("sf"):
        ok, detail = _sf_dry_deploy(a.source_dir, a.target_org)
        method = "sandbox_dry_run"
        predicted["sf_dry_run"] = {"passed": ok, "detail": detail}
        reasons.append(f"sandbox --dry-run deploy: {'validated' if ok else 'FAILED'} ({detail})")
        if not ok:
            achieves = False  # won't even deploy -> cannot achieve the goal
    if callable(a.goal_fn):
        ok, detail = _as_bool_detail(a.goal_fn(m))
        achieves = achieves and ok
        reasons.append(f"goal check: {'met' if ok else 'unmet'}{(' - ' + detail) if detail else ''}")
    elif method == "deterministic":
        reasons.append("NOTE: runtime effect predicted deterministically, not sandbox-validated "
                       "(pass run_real=True + target_org to compile-check)")
    return (effective, achieves, method, predicted, reasons)


def _sim_scheduler(a: Action, run_real: bool) -> tuple:
    """Activating a launchd/cron job: no-op if it's already loaded/active."""
    reasons: list[str] = []
    m = a.mutation or {}
    already = m.get("already_active")
    if already is None and "before_loaded" in m and "after_loaded" in m:
        # loaded->loaded, or the same state = no new work started
        already = bool(m["before_loaded"]) and bool(m["after_loaded"])
    if already:
        why = f"job '{m.get('label', a.summary or 'it')}' is already active — activating again starts nothing new"
        reasons.append(f"predicted NO-OP: {why}")
        return (False, False, "deterministic", {"noop": True, "why": why}, reasons)
    reasons.append(f"predicted activation would start '{m.get('label', a.summary or 'the job')}'")
    achieves = True
    if callable(a.goal_fn):
        ok, detail = _as_bool_detail(a.goal_fn(m))
        achieves = ok
        reasons.append(f"goal check: {'met' if ok else 'unmet'}{(' - ' + detail) if detail else ''}")
    return (True, achieves, "deterministic", {"noop": False, "mutation": m}, reasons)


_SIMULATORS = {
    "file_edit": _sim_file_edit,
    "config_edit": _sim_file_edit,
    "sf_change": _sim_sf,
    "perm_grant": _sim_sf,
    "permission": _sim_sf,
    "assignment": _sim_sf,
    "scheduler": _sim_scheduler,
    "launchd": _sim_scheduler,
    "cron": _sim_scheduler,
}


def _sim_generic(a: Action, run_real: bool) -> tuple:
    """Fallback: use the structured mutation's own before/after + any goal_fn."""
    reasons: list[str] = []
    is_noop, why = _predict_noop(a.mutation, a.kind)
    if is_noop:
        reasons.append(f"predicted NO-OP: {why}")
        return (False, False, "deterministic", {"noop": True, "why": why}, reasons)
    effective = a.mutation is not None
    achieves = effective
    if callable(a.goal_fn):
        ok, detail = _as_bool_detail(a.goal_fn(a.mutation))
        achieves = bool(effective) and ok  # a no-op (no mutation) can't "achieve" even if goal_fn passes
        reasons.append(f"goal check: {'met' if ok else 'unmet'}{(' - ' + detail) if detail else ''}")
    elif not effective:
        reasons.append("no structured mutation to simulate for this kind")
    return (effective, achieves, "deterministic", {"mutation": a.mutation}, reasons)


# ---------------------------------------------------------------------------
# real-world validators (opt-in, guarded — smoke never reaches these)
# ---------------------------------------------------------------------------
def _sf_dry_deploy(source_dir: str, target_org: str) -> tuple[bool, str]:
    """sf project deploy start --dry-run against a SANDBOX org. Compile-validates, deploys nothing."""
    if "prod" in target_org.lower():  # hard guard: never dry-run against a prod-named alias
        return (False, f"refused: target_org '{target_org}' looks like prod")
    cmd = ["sf", "project", "deploy", "start", "--dry-run",
           "--source-dir", source_dir, "--target-org", target_org, "--json"]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except Exception as e:
        return (False, f"sf invocation failed: {e}")
    try:
        out = json.loads(p.stdout or "{}")
    except Exception:
        return (p.returncode == 0, (p.stderr or p.stdout or "")[:200])
    status = out.get("result", {}).get("status") or out.get("status")
    ok = p.returncode == 0 and str(status).lower() in ("succeeded", "0", "success")
    return (ok, str(status))


def _scratch() -> str:
    for c in (
        "/private/tmp/claude-501/-Users-user/"
        "9764a1b4-8940-4a11-bdd3-93891dff7982/scratchpad",
    ):
        if Path(c).is_dir():
            return c
    return tempfile.gettempdir()


def _unified(before: str, after: str, label: str) -> list[str]:
    d = list(difflib.unified_diff(
        before.splitlines(), after.splitlines(),
        fromfile=f"a/{label}", tofile=f"b/{label}", lineterm="", n=1,
    ))
    return d[:40]


# ---------------------------------------------------------------------------
# the gate
# ---------------------------------------------------------------------------
def simulate(action: Any, *, run_real: bool = False) -> dict:
    """Dry-run a proposed mutation against a sandbox twin; predict + gate WITHOUT touching prod.

    Returns a Sim.as_dict(): achieves_goal, effective, safe_to_apply, method, predicted, reasons,
    verify. safe_to_apply is False if the sim predicts a no-op / goal-miss OR self_verify's rails
    refute it. Callers MUST abort a real mutation when safe_to_apply is False.

    run_real=False (default) = pure prediction, fully offline, no subprocess, no prod contact.
    run_real=True opts into the sandbox validators (sf --dry-run, twin test) when available.
    """
    a = _normalize(action)
    sim = _SIMULATORS.get(a.kind, _sim_generic)
    effective, achieves, method, predicted, reasons = sim(a, run_real)

    # feed the PREDICTION into self_verify: an ineffective/goal-missing sim becomes an
    # outcome_fn that reality-refutes, so the rails layer independently catches the no-op too.
    verify = {"safe": True, "verdict": "safe", "refutations": [], "checks": [],
              "note": "self_verify unavailable — rails degraded to pass"}
    if _sv is not None:
        try:
            verify = _sv.verify({
                "kind": a.kind,
                "summary": a.summary or a.goal,
                "lane": a.lane,
                "surface": a.surface,
                "mutation": a.mutation,
                "outcome_fn": (lambda ach=achieves, why=reasons:
                               (ach, "; ".join(why) if not ach else "sim predicts effective goal-achieving change")),
                "ctx": a.ctx,
            })
        except Exception as e:
            verify = {"safe": True, "verdict": "safe", "refutations": [],
                      "checks": [], "note": f"self_verify errored ({e}) — sim prediction still holds"}

    safe = bool(achieves) and bool(verify.get("safe", True))
    if not achieves and "predicted NO-OP" not in " ".join(reasons):
        reasons.append("would NOT achieve the goal — do not apply to prod")
    if not effective:
        reasons.append("BLOCKED before prod: a change that changes nothing is a false 'applied:true' (Blake)")

    return Sim(
        achieves_goal=bool(achieves),
        effective=bool(effective),
        safe_to_apply=safe,
        method=method,
        goal=a.goal or a.summary,
        predicted=predicted,
        reasons=reasons,
        verify=verify,
    ).as_dict()


# ---------------------------------------------------------------------------
def _smoke() -> int:
    fails: list[str] = []

    # (1) Blake-style no-op perm grant — target already holds the perm set. MUST predict no-effect.
    noop = simulate({
        "kind": "perm_grant",
        "goal": "give the SP portal integration user rebate-connector access",
        "summary": "assign the Rebate_Connector_Access permission set to the SP portal integration user",
        "lane": "work", "surface": "salesforce",
        "mutation": {"kind": "perm_grant", "permset": "Rebate_Connector_Access",
                     "before_assigned": True, "after_assigned": True},
    })
    if noop["effective"] or noop["achieves_goal"] or noop["safe_to_apply"]:
        fails.append(f"no-op perm grant must predict NO effect + unsafe, got {noop['effective']=} "
                     f"{noop['achieves_goal']=} {noop['safe_to_apply']=}")
    if not any("NO-OP" in r for r in noop["reasons"]):
        fails.append("no-op perm grant reasons must name the NO-OP (Blake trap)")

    # (2) a REAL SF change (before != after) predicts effect + achieves.
    real_sf = simulate({
        "kind": "sf_change",
        "goal": "flip UserOpportunityController back to without sharing so portal coverage passes",
        "summary": "change UserOpportunityController sharing modifier from with sharing to without sharing",
        "lane": "work", "surface": "salesforce",
        "mutation": {"kind": "config", "before": "with sharing", "after": "without sharing"},
        "goal_fn": lambda m: (m["after"] == "without sharing", "sharing modifier now permissive"),
    })
    if not (real_sf["effective"] and real_sf["achieves_goal"] and real_sf["safe_to_apply"]):
        fails.append(f"real SF change must predict effect+achieve+safe, got {real_sf}")

    # (3) a REAL file edit applied to a TEMP twin + tested — proves effect without touching prod.
    src = Path(_scratch()) / "twin_smoke_src.txt"
    src.write_text("timeout 3300\nembed images inline\n", encoding="utf-8")
    real_edit = simulate({
        "kind": "file_edit",
        "goal": "raise the deploy timeout so the mockup regen job stops SIGTERM-ing itself",
        "summary": "bump the mockup deploy timeout from 3300 to 5400 seconds",
        "path": str(src),
        "new_content": "timeout 5400\nembed images inline\n",
        "test": lambda p: ("5400" in Path(p).read_text(), "new timeout present in twin"),
    })
    if not (real_edit["effective"] and real_edit["achieves_goal"] and real_edit["safe_to_apply"]):
        fails.append(f"real file edit must predict effect+achieve+safe, got {real_edit['reasons']}")
    if src.read_text() != "timeout 3300\nembed images inline\n":
        fails.append("PROD-SAFETY VIOLATION: simulate() mutated the real source file")
    src.unlink(missing_ok=True)

    # (4) a NO-OP file edit (new content identical) predicts no effect.
    src2 = Path(_scratch()) / "twin_smoke_noop.txt"
    src2.write_text("already correct\n", encoding="utf-8")
    noop_edit = simulate({
        "kind": "file_edit", "goal": "fix the config",
        "summary": "set the config value", "path": str(src2),
        "new_content": "already correct\n",
    })
    if noop_edit["effective"] or noop_edit["achieves_goal"] or noop_edit["safe_to_apply"]:
        fails.append(f"identical file edit must predict NO effect, got {noop_edit}")
    src2.unlink(missing_ok=True)

    # (5) scheduler activation that's already-active = no-op.
    noop_sched = simulate({
        "kind": "scheduler", "goal": "start the parity job",
        "summary": "activate the work-brain-parity launchd job",
        "mutation": {"label": "work-brain-parity", "already_active": True},
    })
    if noop_sched["effective"] or noop_sched["safe_to_apply"]:
        fails.append(f"already-active scheduler must predict no-op, got {noop_sched}")

    # (6) deterministic: identical inputs -> identical prediction.
    a = simulate({"kind": "sf_change", "summary": "s", "goal": "g",
                  "mutation": {"before": "a", "after": "b"}})
    b = simulate({"kind": "sf_change", "summary": "s", "goal": "g",
                  "mutation": {"before": "a", "after": "b"}})
    if a["effective"] != b["effective"] or a["achieves_goal"] != b["achieves_goal"]:
        fails.append("simulate() is not deterministic across identical inputs")

    if fails:
        print("SMOKE FAIL:")
        for f in fails:
            print("  -", f)
        return 1
    print("SMOKE OK — no-op perm grant PREDICTED no-effect + BLOCKED (Blake trap caught pre-prod); "
          "real SF change + real file edit PREDICTED effect and achieved goal; twin edit left the real "
          "source untouched; no-op file edit + already-active scheduler both blocked; deterministic. "
          "No prod mutation, no subprocess (run_real=False).")
    return 0


if __name__ == "__main__":
    raise SystemExit(_smoke())
