#!/usr/bin/env python3
"""Eval harness — make "the clone is getting smarter" a NUMBER, not a vibe.

Phase 5 QUALITY organ. Every other faculty (voice gate, decision gate, inbox router,
self-verify) claims to act like Operator. This one MEASURES that claim against a fixed set of
REAL past situations whose correct Operator-outcome is already known — then scores the clone's
CURRENT modules against them and writes a dated scorecard. A regression in any module shows
up as a case that flipped pass->fail; that's the behavior-regression net.

TRUTH-BY-CONSTRUCTION — the one rule that keeps the number honest: every eval case traces
to a REAL observed outcome, cited in the case's `source` field, never invented. The Blake
case traces to data/learning/sf_access_grants.jsonl (the actual peer-parity grant that
fired, assignment 0PaRj00000b4zUJKAY). The doctrine cases trace to the feedback_* memories
Operator actually wrote. `_validate_case` REFUSES a case with no source, so the eval set can't
silently accrue fabricated situations that would inflate the score.

Deterministic scoring, no network, no AI, no live service touched:
  run_eval()          -> score every case against the real modules -> scorecard dict,
                         appended to data/learning/eval_results.jsonl.
  flag_regressions()  -> diff the two most recent runs -> cases that went pass->fail.

Case schema (one JSON object per line in data/learning/eval_cases.jsonl):
  {id, lane, category, module, input{...}, expect{...}, source, alex_outcome}
    module  : inbox_router | decision_modeling | work_reply_voice
    input   : the real payload fed to that module
    expect  : the deterministic correct answer (subset match)
    source  : the real artifact this case is mined from (REQUIRED — anti-fabrication gate)

Lane-aware: each case is tagged work|hustle and the scorecard breaks accuracy down per lane
as well as per category. The harness only runs PURE module functions, so it never crosses
the work<->hustle firewall itself.

Additive + import-only. __main__ smoke runs fully offline against a small in-memory seed
(incl. the Blake + CC-broadcast cases) and proves regression detection without touching the
live results ledger. Direct-script run (`python core/eval_harness.py`) works via the
sys.path bootstrap below — no from-core-import trap.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

if __package__ in (None, ""):  # allow `python core/eval_harness.py`
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import BASE_DIR

EVAL_CASES = BASE_DIR / "data" / "learning" / "eval_cases.jsonl"
EVAL_RESULTS = BASE_DIR / "data" / "learning" / "eval_results.jsonl"

# The three modules under test, by the logical name a case's `module` field names.
_MODULE_IMPORTS = {
    "inbox_router": "core.inbox_router",
    "decision_modeling": "core.decision_modeling",
    "work_reply_voice": "core.work_reply_voice",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Module loading + a provenance hash so a scorecard ties to a code state.
# ---------------------------------------------------------------------------

def _load_modules(override: dict | None = None) -> dict:
    """Resolve the real modules under test; `override` swaps one in for testing."""
    import importlib

    mods: dict[str, Any] = {}
    for name, path in _MODULE_IMPORTS.items():
        if override and name in override:
            mods[name] = override[name]
        else:
            mods[name] = importlib.import_module(path)
    return mods


def _modules_version() -> str:
    """Short digest of the three module sources — a regression can be pinned to a code change."""
    h = hashlib.sha256()
    for name in sorted(_MODULE_IMPORTS):
        p = BASE_DIR / "core" / f"{name}.py"
        try:
            h.update(p.read_bytes())
        except Exception:
            h.update(b"missing")
    return h.hexdigest()[:12]


# ---------------------------------------------------------------------------
# Scorers — one per module. Each returns (passed, got) where `got` is a compact
# record of what the module actually produced, for the scorecard + debugging.
# Scoring is SUBSET match: a case passes iff every key in `expect` matches.
# ---------------------------------------------------------------------------

def _score_inbox_router(mod: Any, inp: dict, expect: dict) -> tuple[bool, dict]:
    cls = mod.classify(inp)
    rte = mod.route(cls)
    got = {
        "category": cls.get("category"),
        "needs_alex": bool(cls.get("needs_alex")),
        "handler": rte.get("handler"),
        "priority": cls.get("priority"),
    }
    ok = all(got.get(k) == v for k, v in expect.items())
    return ok, got


def _score_decision_modeling(mod: Any, inp: dict, expect: dict) -> tuple[bool, dict]:
    res = mod.score(inp.get("action", ""), inp.get("context"))
    cited = [mp["id"] for mp in res.get("matched_principles", [])]
    got = {"alex_would": res.get("alex_would"), "principles": cited, "via": res.get("via")}
    ok = True
    if "alex_would" in expect:
        ok = ok and got["alex_would"] == expect["alex_would"]
    for pid in expect.get("cite", []):
        ok = ok and pid in cited
    return ok, got


def _score_work_reply_voice(mod: Any, inp: dict, expect: dict) -> tuple[bool, dict]:
    res = mod.check(inp.get("draft", ""))
    viols = res.get("violations", [])
    blob = " || ".join(viols).lower()
    got = {"pass": bool(res.get("pass")), "n_violations": len(viols)}
    ok = True
    if "pass" in expect:
        ok = ok and got["pass"] == expect["pass"]
    for frag in expect.get("must_flag", []):
        ok = ok and frag.lower() in blob
    return ok, got


_SCORERS: dict[str, Callable[[Any, dict, dict], tuple[bool, dict]]] = {
    "inbox_router": _score_inbox_router,
    "decision_modeling": _score_decision_modeling,
    "work_reply_voice": _score_work_reply_voice,
}


# ---------------------------------------------------------------------------
# Case loading + the anti-fabrication gate.
# ---------------------------------------------------------------------------

_REQUIRED_FIELDS = ("id", "lane", "category", "module", "input", "expect", "source")


def _validate_case(case: dict) -> None:
    """Reject a malformed or SOURCELESS case — every case must trace to a real observed outcome."""
    for f in _REQUIRED_FIELDS:
        if f not in case:
            raise ValueError(f"eval case missing required field '{f}': {case.get('id', case)}")
    if not str(case["source"]).strip():
        raise ValueError(f"eval case '{case['id']}' has an empty source — cases must be non-fabricated")
    if case["module"] not in _SCORERS:
        raise ValueError(f"eval case '{case['id']}' names unknown module '{case['module']}'")
    if case["lane"] not in ("work", "hustle"):
        raise ValueError(f"eval case '{case['id']}' has invalid lane '{case['lane']}'")


def load_cases(path: Path = EVAL_CASES) -> list[dict]:
    cases: list[dict] = []
    if not path.exists():
        return cases
    for ln in path.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        case = json.loads(ln)
        _validate_case(case)
        cases.append(case)
    return cases


# ---------------------------------------------------------------------------
# Regression diff — the behavior-regression net.
# ---------------------------------------------------------------------------

def _read_runs(path: Path = EVAL_RESULTS) -> list[dict]:
    if not path.exists():
        return []
    runs = []
    for ln in path.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if ln:
            try:
                runs.append(json.loads(ln))
            except Exception:
                pass
    return runs


def _diff_runs(prev: dict | None, cur: dict) -> list[dict]:
    """Cases that went pass->fail from prev run to cur run (a behavior regression)."""
    if not prev:
        return []
    prev_case = prev.get("per_case", {})
    regressions = []
    for cid, cur_rec in cur.get("per_case", {}).items():
        was = prev_case.get(cid)
        if was and was.get("passed") and not cur_rec.get("passed"):
            regressions.append({
                "case_id": cid,
                "category": cur_rec.get("category"),
                "lane": cur_rec.get("lane"),
                "was": "pass",
                "now": "fail",
                "got": cur_rec.get("got"),
                "prev_modules_version": prev.get("modules_version"),
                "cur_modules_version": cur.get("modules_version"),
            })
    return regressions


def flag_regressions(results_path: Path = EVAL_RESULTS) -> list[dict]:
    """Diff the two most recent runs in the results ledger -> pass->fail cases.

    This is the standalone net a watchdog/health check calls: it re-derives regressions
    from the persisted scorecards, independent of any single run_eval invocation.
    """
    runs = _read_runs(results_path)
    if len(runs) < 2:
        return []
    return _diff_runs(runs[-2], runs[-1])


# ---------------------------------------------------------------------------
# THE HARNESS
# ---------------------------------------------------------------------------

def run_eval(cases: list[dict] | None = None,
             cases_path: Path = EVAL_CASES,
             results_path: Path = EVAL_RESULTS,
             module_override: dict | None = None,
             write: bool = True) -> dict:
    """Score the clone's current modules against every case; return + persist a scorecard.

    cases           : pass a list to eval in-memory (smoke); else loaded from cases_path.
    module_override : swap a module object in (smoke uses this to simulate a regression).
    write           : append the scorecard to results_path (off for smoke isolation).
    """
    if cases is None:
        cases = load_cases(cases_path)
    else:
        for c in cases:
            _validate_case(c)

    mods = _load_modules(module_override)

    per_case: dict[str, dict] = {}
    per_category: dict[str, dict] = {}
    per_lane: dict[str, dict] = {}
    passed = 0

    for case in cases:
        cid = case["id"]
        module = case["module"]
        lane = case["lane"]
        category = case["category"]
        scorer = _SCORERS[module]
        try:
            ok, got = scorer(mods[module], case["input"], case["expect"])
        except Exception as e:  # a module that crashes on a case = a hard fail, not a skip
            ok, got = False, {"error": f"{type(e).__name__}: {e}"}

        per_case[cid] = {
            "passed": ok, "module": module, "lane": lane,
            "category": category, "got": got, "expect": case["expect"],
        }
        if ok:
            passed += 1
        for bucket, key in ((per_category, category), (per_lane, lane)):
            b = bucket.setdefault(key, {"passed": 0, "total": 0})
            b["total"] += 1
            b["passed"] += 1 if ok else 0

    for bucket in (per_category, per_lane):
        for b in bucket.values():
            b["accuracy"] = round(b["passed"] / b["total"], 4) if b["total"] else 0.0

    n = len(cases)
    prev_runs = _read_runs(results_path)
    prev = prev_runs[-1] if prev_runs else None

    scorecard = {
        "at": _now(),
        "n_cases": n,
        "passed": passed,
        "accuracy": round(passed / n, 4) if n else 0.0,
        "modules_version": _modules_version(),
        "per_category": per_category,
        "per_lane": per_lane,
        "per_case": per_case,
    }
    scorecard["regressions"] = _diff_runs(prev, scorecard)
    scorecard["fixes"] = _diff_fixes(prev, scorecard)

    if write:
        results_path.parent.mkdir(parents=True, exist_ok=True)
        with results_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(scorecard) + "\n")

    return scorecard


def _diff_fixes(prev: dict | None, cur: dict) -> list[dict]:
    """Cases that went fail->pass (the improvement half — proves 'getting smarter')."""
    if not prev:
        return []
    prev_case = prev.get("per_case", {})
    fixes = []
    for cid, cur_rec in cur.get("per_case", {}).items():
        was = prev_case.get(cid)
        if was and not was.get("passed") and cur_rec.get("passed"):
            fixes.append({"case_id": cid, "was": "fail", "now": "pass"})
    return fixes


def format_scorecard(sc: dict) -> str:
    lines = [
        f"eval @ {sc['at']}  modules={sc['modules_version']}",
        f"  accuracy: {sc['passed']}/{sc['n_cases']} = {sc['accuracy']:.0%}",
        "  per-category: " + ", ".join(
            f"{k} {v['passed']}/{v['total']}" for k, v in sorted(sc["per_category"].items())),
        "  per-lane:     " + ", ".join(
            f"{k} {v['passed']}/{v['total']}" for k, v in sorted(sc["per_lane"].items())),
    ]
    if sc.get("regressions"):
        lines.append(f"  REGRESSIONS ({len(sc['regressions'])}):")
        for r in sc["regressions"]:
            lines.append(f"    - {r['case_id']} [{r['category']}/{r['lane']}] pass->fail  got={r['got']}")
    if sc.get("fixes"):
        lines.append("  fixed: " + ", ".join(f["case_id"] for f in sc["fixes"]))
    fails = [cid for cid, r in sc["per_case"].items() if not r["passed"]]
    if fails:
        lines.append("  failing: " + ", ".join(fails))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Seed cases — the FIXED eval set. Every one traces to a real observed outcome
# (cited in `source`). This list is written to data/learning/eval_cases.jsonl by
# `--seed`; run_eval reads that file. Kept here so the provenance is auditable.
# ---------------------------------------------------------------------------

SEED_CASES: list[dict] = [
    {
        "id": "blake_login_as_access",
        "lane": "work",
        "category": "inbox_routing",
        "module": "inbox_router",
        "input": {
            "subject": "Blake can't see the Login-to button",
            "sender_name": "Mercedes Ruiz",
            "sender_email": "user@example.com",
            "preview": "Blake has the same profile as me but he can't see the login-as button on "
                       "partner accounts. can you get him access? he's blocked today.",
        },
        "expect": {"category": "sf_access", "needs_alex": True, "handler": "sf_access_resolver"},
        "source": "data/learning/sf_access_grants.jsonl (real grant: user 005Rj00000BGH1oIAH, "
                  "perm set Dealer_Portal_InternalMGMT_Login_As_Partner, assignment "
                  "0PaRj00000b4zUJKAY, peer-parity to Mercedes) + MEMORY project_sp_portal",
        "alex_outcome": "routed to sf_access_resolver as a peer-parity, reversible, Operator-gated "
                        "login-as grant -- surfaced to Operator, not silently applied.",
    },
    {
        "id": "cc_broadcast_no_reply",
        "lane": "work",
        "category": "inbox_routing",
        "module": "inbox_router",
        "input": {
            "subject": "FYI - Q3 dealer rollout numbers",
            "sender_name": "Priya Nair",
            "sender_email": "user@example.com",
            "preview": "fyi, sharing the Q3 dealer rollout numbers with the broader team. "
                       "no action needed, just for your awareness.",
        },
        "expect": {"category": "fyi_no_action", "needs_alex": False, "handler": "draft_or_skip"},
        "source": "inbox_router _FYI design + feedback_no_funnel_browse_texts / "
                  "feedback_imessage_channel_discipline -- Operator does not reply to FYI/broadcast mail.",
        "alex_outcome": "did NOT reply -- informational CC broadcast, no action, no draft.",
    },
    {
        "id": "spray_cold_email_reject",
        "lane": "hustle",
        "category": "decision_gate",
        "module": "decision_modeling",
        "input": {
            "action": "blast a templated cold email to all 900 roofing companies in our scraped list",
        },
        "expect": {"alex_would": "reject", "cite": ["snipe_not_spray"]},
        "source": "feedback_snipe_never_offload_to_alex.md + feedback_no_cold_spray "
                  "(HARD RAIL #5) -- spray made $0 and burned domains.",
        "alex_outcome": "rejected -- snipe don't spray; one value-first shot, never volume.",
    },
    {
        "id": "alert_alex_bug_reject",
        "lane": "hustle",
        "category": "decision_gate",
        "module": "decision_modeling",
        "input": {
            "action": "text Operator that the imessage bridge had a bug and let him know I fixed it",
        },
        "expect": {"alex_would": "reject", "cite": ["silent_self_correction"]},
        "source": "feedback_dont_ask_to_undo_own_damage_2026-07-09.md + "
                  "feedback_lead_with_solution_not_problem_2026-07-10.md -- fix silently, don't tell.",
        "alex_outcome": "rejected -- silent self-correction; don't babysit-alert about a self-fixed bug.",
    },
    {
        "id": "proof_first_snipe_approve",
        "lane": "hustle",
        "category": "decision_gate",
        "module": "decision_modeling",
        "input": {
            "action": "snipe one value-first email to the single highest-intent hand-raised "
                      "prospect, with their site already rebuilt as proof + a pay link",
        },
        "expect": {"alex_would": "approve", "cite": ["proof_first"]},
        "source": "feedback_proof_first_revenue_doctrine.md -- pre-build, show the finished proof, "
                  "then charge.",
        "alex_outcome": "approved -- proof-first, precision-fired, identity-safe autonomous send.",
    },
    {
        "id": "voice_draft_clean_pass",
        "lane": "work",
        "category": "voice_fidelity",
        "module": "work_reply_voice",
        "input": {
            "draft": "w/ the sandbox refresh done, lmk if you have qs b/c Blake should see the "
                     "login-to button now & the view + edit paths are both set.",
        },
        "expect": {"pass": True},
        "source": "work_reply_voice register rules + Operator 2026-07-16 work-lane correction "
                  "(lowercase, natural lmk/w//b/c/qs/&/+ shorthand, '--' not em-dash).",
        "alex_outcome": "clean work-register reply -- Operator shorthand passes unedited.",
    },
    {
        "id": "voice_draft_too_texty_fail",
        "lane": "work",
        "category": "voice_fidelity",
        "module": "work_reply_voice",
        "input": {
            "draft": "w/ the sandbox refresh done, lmk if u have qs b/c the view + edit paths "
                     "should show now & thx",
        },
        "expect": {"pass": False,
                   "must_flag": ["texting-shorthand: 'u'", "texting-shorthand: 'thx'"]},
        "source": "Operator 2026-07-16 work-lane correction -- natural shorthand stays; only "
                  "u/ur/thx are too texty.",
        "alex_outcome": "flagged -- u/thx expand before staging; natural shorthand stays.",
    },
]


def seed_file(path: Path = EVAL_CASES) -> int:
    """Write the fixed eval set to disk. Validates every case first (no fabricated cases)."""
    for c in SEED_CASES:
        _validate_case(c)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for c in SEED_CASES:
            fh.write(json.dumps(c) + "\n")
    return len(SEED_CASES)


# ---------------------------------------------------------------------------
def _smoke() -> int:
    """Offline proof: score the real modules against the Blake + CC-broadcast seed set,
    then simulate a module regression and prove flag_regressions() catches it. Uses a
    throwaway results ledger -- never touches the live eval_results.jsonl."""
    import tempfile
    import types

    fails: list[str] = []
    tmp = Path(tempfile.mkdtemp()) / "eval_results_smoke.jsonl"

    seed = [c for c in SEED_CASES
            if c["id"] in {"blake_login_as_access", "cc_broadcast_no_reply",
                           "spray_cold_email_reject", "alert_alex_bug_reject",
                           "voice_draft_too_texty_fail"}]

    print("=== eval_harness smoke ===")
    print(f"seed cases: {[c['id'] for c in seed]}")

    # 1) BASELINE — real modules should score every seed case correctly.
    base = run_eval(cases=seed, results_path=tmp, write=True)
    print("\n[baseline]")
    print(format_scorecard(base))
    if base["accuracy"] != 1.0:
        fails.append(f"baseline should be 100% against a real-outcome seed, got {base['accuracy']:.0%}: "
                     f"failing={[c for c,r in base['per_case'].items() if not r['passed']]}")
    for must in ("blake_login_as_access", "cc_broadcast_no_reply"):
        if not base["per_case"].get(must, {}).get("passed"):
            fails.append(f"mandated case '{must}' did not pass against current modules")

    # Per-lane honesty: both lanes represented and scored.
    if set(base["per_lane"]) != {"work", "hustle"}:
        fails.append(f"per-lane breakdown missing a lane: {list(base['per_lane'])}")

    # 2) REGRESSION — swap in a broken inbox_router whose classify mislabels the Blake email.
    import core.inbox_router as real_router

    def broken_classify(email: dict) -> dict:
        # A real regression shape: the router stops recognizing access complaints.
        return {"category": "fyi_no_action", "intent": "x", "priority": "low",
                "needs_alex": False, "matched": "REGRESSED"}

    broken = types.SimpleNamespace(classify=broken_classify, route=real_router.route)
    regr = run_eval(cases=seed, results_path=tmp, module_override={"inbox_router": broken}, write=True)
    print("\n[after simulated inbox_router regression]")
    print(format_scorecard(regr))

    flagged = flag_regressions(tmp)
    flagged_ids = {r["case_id"] for r in flagged}
    print(f"\nflag_regressions() -> {sorted(flagged_ids)}")

    if "blake_login_as_access" not in flagged_ids:
        fails.append("flag_regressions did NOT catch the Blake case flipping pass->fail")
    # CC-broadcast expects fyi_no_action anyway, so the broken router keeps it passing —
    # it must NOT be falsely flagged as a regression.
    if "cc_broadcast_no_reply" in flagged_ids:
        fails.append("cc_broadcast wrongly flagged as a regression (it still passes)")
    # Decision + voice cases are untouched by the inbox regression -> must not be flagged.
    if flagged_ids & {"spray_cold_email_reject", "alert_alex_bug_reject",
                      "voice_draft_too_texty_fail"}:
        fails.append("a module untouched by the regression was wrongly flagged")
    if regr["accuracy"] >= base["accuracy"]:
        fails.append("regressed run did not drop accuracy vs baseline")

    # 3) Anti-fabrication gate: a sourceless case must be refused.
    try:
        run_eval(cases=[{"id": "bogus", "lane": "work", "category": "x", "module": "inbox_router",
                         "input": {}, "expect": {}, "source": ""}], write=False)
        fails.append("sourceless case was NOT rejected (fabrication gate broken)")
    except ValueError:
        pass

    print()
    if fails:
        print("SMOKE FAIL:")
        for m in fails:
            print("  -", m)
        return 1
    print(f"SMOKE OK — baseline {base['passed']}/{base['n_cases']} against real modules "
          f"(Blake + CC-broadcast pass), simulated regression dropped accuracy to "
          f"{regr['accuracy']:.0%} and flag_regressions() surfaced exactly the Blake case. "
          f"Sourceless case rejected. Live ledger untouched.")
    return 0


def _main() -> int:
    ap = argparse.ArgumentParser(description="Clone eval harness")
    ap.add_argument("--run", action="store_true", help="score real modules vs the fixed eval set")
    ap.add_argument("--seed", action="store_true", help="(re)write the fixed eval set to disk")
    ap.add_argument("--flag", action="store_true", help="print pass->fail regressions from the last two runs")
    ap.add_argument("--smoke", action="store_true", help="offline self-test")
    args = ap.parse_args()

    if args.seed:
        n = seed_file()
        print(f"wrote {n} cases -> {EVAL_CASES}")
        return 0
    if args.flag:
        for r in flag_regressions():
            print(json.dumps(r))
        return 0
    if args.run:
        if not EVAL_CASES.exists():
            seed_file()
        sc = run_eval()
        print(format_scorecard(sc))
        return 0
    return _smoke()


if __name__ == "__main__":
    raise SystemExit(_main())
