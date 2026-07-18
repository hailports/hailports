#!/usr/bin/env python3
"""Fable runtime — the reasoning method as a LIVE PROCEDURE, not prose in a prompt.

The fable-method skill is a checklist the model is *supposed* to follow. Supposed-to
leaks: on a hard call the model narrates the steps it skipped. This module executes the
mechanical parts of that checklist deterministically, so the clone runs them instead of
merely claiming to. The clone calls run(task) BEFORE a hard answer/action; the returned
trace is the working state the model then reasons on top of — with the automatable checks
(premise audit, number recompute) already done and their failures already flagged.

run(task, context=None) -> ReasoningTrace.as_dict() with:
  - intent        : parse-twice (literal ask vs. accomplished goal, divergence flag)
  - deliverable   : artifact type + success criterion + constraints
  - problem_type  : lookup / computation / debugging / design / judgment
  - premises      : embedded presuppositions, each STATUS'd (ok / unverified / FALSE)
  - facts / assumptions : the two registers, kept separate (no silent migration)
  - hypotheses    : >= 2, generated before any one is investigated (anchoring defense)
  - number_checks : every figure recomputed a SECOND way; mismatches flagged
  - weak_link     : the single claim that would embarrass us if wrong
  - confidence    : {score, band} traceable to checks run, not fluency
  - answer_lead   : the lead-with-the-answer scaffold
  - checklist     : the 8-item pre-flight, each pass/fail

What is deterministic here: the mechanical rigor the skill spells out — recompute-twice,
percent-base check, premise/presupposition detection, register separation, the >=2
hypothesis floor, the pre-flight. What stays the model's job: the *content* of hypotheses
and the final prose. This scaffolds and gates; it does not pretend to think for the model.

Additive, import-only, zero network. __main__ smoke proves it flags an embedded false
premise AND recomputes an embedded number a second way.
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

if __package__ in (None, ""):  # allow `python core/fable_runtime.py`
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ---------------------------------------------------------------------------
# small numeric helpers
# ---------------------------------------------------------------------------
_SUFFIX = {"k": 1_000.0, "m": 1_000_000.0, "b": 1_000_000_000.0}


def _num(raw: str) -> float:
    """'80k' -> 80000.0, '$1.2m' -> 1200000.0, '3,400' -> 3400.0."""
    s = raw.strip().lower().replace("$", "").replace(",", "").replace(" ", "")
    mult = 1.0
    if s and s[-1] in _SUFFIX:
        mult = _SUFFIX[s[-1]]
        s = s[:-1]
    return float(s) * mult


def _fmt(x: float) -> str:
    if x == int(x) and abs(x) < 1e15:
        return f"{int(x):,}"
    return f"{x:,.2f}".rstrip("0").rstrip(".")


# ---------------------------------------------------------------------------
# problem-type classifier (skill §1)
# ---------------------------------------------------------------------------
_TYPE_CUES = [
    ("computation", r"\b(how many|how much|calculate|compute|total|sum|average|rate|percent|%|\$)\b"),
    ("debugging", r"\b(fix|bug|broken|fails?|error|crash|leak|regression|why (is|does|isn'?t)|not working)\b"),
    ("design", r"\b(design|architect|build|structure|approach|options?|should we (use|build|adopt))\b"),
    ("judgment", r"\b(should (i|we)|worth it|better|recommend|decide|go/no-go|expand|migrate)\b"),
    ("lookup", r"\b(what is|what'?s the|when did|who|where|which version|look up|find the)\b"),
]


def _classify(task: str) -> str:
    t = task.lower()
    for kind, pat in _TYPE_CUES:
        if re.search(pat, t):
            return kind
    return "judgment"


# ---------------------------------------------------------------------------
# premise / presupposition audit (skill §1: "audit the premises")
# ---------------------------------------------------------------------------
@dataclass
class Premise:
    text: str
    kind: str          # "quantitative" | "causal" | "existential"
    status: str        # "ok" | "unverified" | "FALSE"
    note: str = ""

    def as_dict(self) -> dict:
        return {"text": self.text, "kind": self.kind, "status": self.status, "note": self.note}


# "fell/rose/dropped from A to B — a P% reduction/increase/drop/gain/cut"
_PCT_CLAIM = re.compile(
    r"(?P<verb>fell|dropped|rose|grew|increased|decreased|cut|reduced|went)\s+"
    r"(?:from\s+)?(?P<a>\$?\d[\d,\.]*\s*[kmb]?)\s+(?:to|down to|up to)\s+(?P<b>\$?\d[\d,\.]*\s*[kmb]?)"
    r"[^.]*?(?P<p>\d[\d\.]*)\s*%\s*(?P<dir>reduction|decrease|drop|cut|savings?|increase|gain|rise|growth)",
    re.IGNORECASE,
)

# causal presuppositions: "since/because X caused/drove/broke Y", "why does X cause Y"
_CAUSAL = re.compile(
    r"\b(?:since|because|as|given that)\b[^,.]*\b(caused|drove|broke|led to|resulted in|is why|triggered)\b",
    re.IGNORECASE,
)
_WHY_CAUSAL = re.compile(r"\bwhy (?:does|do|did|is|are)\b[^?]*\b(cause|make|break|slow|trigger)", re.IGNORECASE)


def _audit_premises(task: str, checks: list["NumberCheck"]) -> list[Premise]:
    out: list[Premise] = []

    # quantitative premises — cross-checked against the recompute pass
    for c in checks:
        if c.kind == "pct_change":
            if c.agrees:
                out.append(Premise(c.claim, "quantitative", "ok",
                                   f"recompute agrees: {_fmt(c.recomputed)}%"))
            else:
                out.append(Premise(
                    c.claim, "quantitative", "FALSE",
                    f"claimed {c.claimed}% but recompute (Δ/old) = {_fmt(c.recomputed)}%; "
                    f"the {c.claimed}% divides by the NEW value"))

    # causal presuppositions — asserted, never verified inside the question
    for m in list(_CAUSAL.finditer(task)) + list(_WHY_CAUSAL.finditer(task)):
        frag = m.group(0).strip()
        out.append(Premise(
            frag, "causal", "unverified",
            "causation asserted, not shown — a confound could explain the correlation"))

    return out


# ---------------------------------------------------------------------------
# quantitative rigor — recompute every number a SECOND way (skill §4)
# ---------------------------------------------------------------------------
@dataclass
class NumberCheck:
    claim: str
    kind: str              # "pct_change" | "arithmetic"
    claimed: float
    recomputed: float
    agrees: bool
    method: str            # the second route used

    def as_dict(self) -> dict:
        return {"claim": self.claim, "kind": self.kind, "claimed": self.claimed,
                "recomputed": round(self.recomputed, 4), "agrees": self.agrees, "method": self.method}


# explicit arithmetic: "12 * 8 = 96", "80 - 60 = 20", "3 + 4 = 8"
_ARITH = re.compile(r"(?P<x>\$?\d[\d,\.]*\s*[kmb]?)\s*(?P<op>[-+*/x×])\s*(?P<y>\$?\d[\d,\.]*\s*[kmb]?)\s*=\s*(?P<z>\$?\d[\d,\.]*\s*[kmb]?)")


def _recompute_numbers(task: str) -> list[NumberCheck]:
    out: list[NumberCheck] = []

    for m in _PCT_CLAIM.finditer(task):
        a, b, claimed = _num(m.group("a")), _num(m.group("b")), float(m.group("p"))
        if a == 0:
            continue
        # second route: signed percent change off the OLD base, then take magnitude
        signed = (b - a) / a * 100.0
        recomputed = abs(signed)
        agrees = abs(recomputed - claimed) <= 0.5
        out.append(NumberCheck(
            claim=m.group(0).strip(), kind="pct_change", claimed=claimed, recomputed=recomputed,
            agrees=agrees,
            method=f"Δ/old = ({_fmt(b)}-{_fmt(a)})/{_fmt(a)} = {_fmt(signed)}%  (NOT Δ/new = {'n/a (new=0)' if b == 0 else _fmt((b - a) / b * 100)}%)"))

    for m in _ARITH.finditer(task):
        x, y, z = _num(m.group("x")), _num(m.group("y")), _num(m.group("z"))
        op = m.group("op")
        if op in ("*", "x", "×"):
            calc, route = x * y, f"{_fmt(y)}+{_fmt(y)}+… ({int(x) if x==int(x) else x}×) or distributive"
        elif op == "+":
            calc, route = x + y, f"{_fmt(y)}+{_fmt(x)} (commuted)"
        elif op == "-":
            calc, route = x - y, f"check via {_fmt(z)}+{_fmt(y)}={_fmt(z+y)} should equal {_fmt(x)}"
        elif op == "/":
            calc, route = (x / y if y else float("nan")), f"check via {_fmt(z)}×{_fmt(y)}={_fmt(z*y)} should equal {_fmt(x)}"
        else:
            continue
        agrees = calc == calc and abs(calc - z) <= max(0.5, abs(z) * 1e-6)
        out.append(NumberCheck(m.group(0).strip(), "arithmetic", z, calc, agrees, route))

    return out


# ---------------------------------------------------------------------------
# hypotheses — the >=2 floor before investigating any one (skill §3)
# ---------------------------------------------------------------------------
def _generate_hypotheses(task: str, ptype: str, premises: list[Premise]) -> list[str]:
    hyps: list[str] = []
    # any unverified causal premise forces its spurious-correlation alternative to exist
    if any(p.kind == "causal" and p.status != "ok" for p in premises):
        hyps.append("H1: the asserted cause is real and drives the effect")
        hyps.append("H2: the cause is spurious — a confound/coincidence explains the correlation")
    if ptype == "judgment":
        hyps.append("Hj+: proceed — expected value is positive")
        hyps.append("Hj-: hold — the premise is shaky or the downside is asymmetric")
    elif ptype == "debugging":
        hyps.append("Hd1: the fault is where the question points")
        hyps.append("Hd2: the fault is upstream/elsewhere; the pointed-at site is a symptom")
    elif ptype == "design":
        hyps.append("Hg1: the proposed approach fits the constraints")
        hyps.append("Hg2: a simpler existing mechanism already covers it (pilfer before build)")
    # de-dup, preserve order
    seen, uniq = set(), []
    for h in hyps:
        if h not in seen:
            seen.add(h); uniq.append(h)
    while len(uniq) < 2:  # hard floor: the anchoring defense must not be skippable
        uniq.append(f"H{len(uniq)+1}: the leading read is wrong — what would prove it so?")
    return uniq


# ---------------------------------------------------------------------------
# intent (parse-twice) + deliverable (skill §1)
# ---------------------------------------------------------------------------
def _intent(task: str, ptype: str) -> dict:
    literal = task.strip().split("\n")[0][:200]
    # the accomplished goal usually trails the last question mark or an imperative
    goal = literal
    qs = [s.strip() for s in re.split(r"(?<=[?.])\s+", task.strip()) if s.strip()]
    if qs:
        goal = next((s for s in reversed(qs) if s.endswith("?")), qs[-1])[:200]
    diverged = bool(_PCT_CLAIM.search(task) or _CAUSAL.search(task)) and goal != literal
    return {"literal": literal, "accomplished": goal, "diverged": diverged}


def _deliverable(ptype: str, task: str) -> dict:
    artifact = {"computation": "a number", "lookup": "a verified fact", "debugging": "a fix",
                "design": "a chosen approach", "judgment": "a decision"}[ptype]
    constraints = []
    for m in re.finditer(r"\b(under|below|less than|<=?)\s*(\d[\d,]*)\s*(lines|words|chars|characters|ms|s)\b", task, re.I):
        constraints.append(m.group(0))
    return {"artifact": artifact, "success_criterion": f"the asker accepts {artifact} as done",
            "constraints": constraints}


# ---------------------------------------------------------------------------
# trace
# ---------------------------------------------------------------------------
@dataclass
class ReasoningTrace:
    task: str
    intent: dict
    deliverable: dict
    problem_type: str
    premises: list[Premise] = field(default_factory=list)
    facts: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    hypotheses: list[str] = field(default_factory=list)
    number_checks: list[NumberCheck] = field(default_factory=list)
    weak_link: Optional[str] = None
    confidence: dict = field(default_factory=dict)
    answer_lead: str = ""
    checklist: dict = field(default_factory=dict)
    flags: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "task": self.task,
            "intent": self.intent,
            "deliverable": self.deliverable,
            "problem_type": self.problem_type,
            "premises": [p.as_dict() for p in self.premises],
            "facts": self.facts,
            "assumptions": self.assumptions,
            "hypotheses": self.hypotheses,
            "number_checks": [c.as_dict() for c in self.number_checks],
            "weak_link": self.weak_link,
            "confidence": self.confidence,
            "answer_lead": self.answer_lead,
            "checklist": self.checklist,
            "flags": self.flags,
        }


def _confidence(premises, checks, hyps, flags) -> dict:
    score = 0.9
    reasons = []
    for p in premises:
        if p.status == "FALSE":
            score -= 0.4; reasons.append("false premise in the question")
        elif p.status == "unverified":
            score -= 0.15; reasons.append("unverified premise relied on")
    for c in checks:
        if not c.agrees:
            score -= 0.25; reasons.append("a number failed recompute")
    if len(hyps) < 2:
        score -= 0.2; reasons.append("single-hypothesis (anchoring risk)")
    score = max(0.05, min(0.95, score))
    band = "high" if score >= 0.75 else "medium" if score >= 0.45 else "low"
    return {"score": round(score, 2), "band": band,
            "basis": reasons or ["no automated check fired against it"]}


def _weakest(premises, checks) -> Optional[str]:
    # severity order: a false number > a FALSE premise > unverified causal premise
    for c in checks:
        if not c.agrees:
            return f"NUMBER: '{c.claim}' — claimed {c.claimed}, recompute says {_fmt(c.recomputed)} ({c.method})"
    for p in premises:
        if p.status == "FALSE":
            return f"PREMISE (false): '{p.text}' — {p.note}"
    for p in premises:
        if p.status == "unverified":
            return f"PREMISE (unverified): '{p.text}' — {p.note}"
    return None


def run(task: str, context: Optional[dict] = None) -> dict:
    """Execute the Fable procedure as deterministic checks over `task`.

    Returns the reasoning trace the clone reasons on top of. The mechanical checks
    (premise audit, number recompute, >=2 hypotheses, weak-link, confidence, pre-flight)
    are already done; the model supplies hypothesis content and final prose.
    """
    context = context or {}
    if not isinstance(task, str) or not task.strip():
        raise ValueError("run(task): task must be a non-empty string")

    ptype = _classify(task)
    checks = _recompute_numbers(task)
    premises = _audit_premises(task, checks)
    hyps = _generate_hypotheses(task, ptype, premises)
    intent = _intent(task, ptype)
    deliverable = _deliverable(ptype, task)

    # register separation: caller-supplied facts stay facts; nothing migrates silently
    facts = list(context.get("facts", []))
    assumptions = list(context.get("assumptions", []))
    for p in premises:
        if p.status == "ok":
            facts.append(f"verified: {p.text}")
        else:
            assumptions.append(f"{p.status}: {p.text} — {p.note}")

    flags: list[str] = []
    if any(p.status == "FALSE" for p in premises):
        flags.append("FALSE_PREMISE — do not answer the broken question well; correct it first")
    if any(not c.agrees for c in checks):
        flags.append("NUMBER_MISMATCH — a stated figure failed recompute; use the recomputed value")
    if any(p.kind == "causal" and p.status != "ok" for p in premises):
        flags.append("UNVERIFIED_CAUSATION — hold the spurious-correlation alternative open")
    if intent["diverged"]:
        flags.append("INTENT_DIVERGENCE — literal ask ≠ accomplished goal; serve the goal, note the ask")

    weak = _weakest(premises, checks)
    conf = _confidence(premises, checks, hyps, flags)

    checklist = {
        "1_right_question": not intent["diverged"],
        "2_premises_verified": all(p.status != "FALSE" for p in premises),
        "3_numbers_recomputed": all(c.agrees for c in checks),
        "4_anchor_checked": len(hyps) >= 2,
        "5_assumption_ledger": True,  # every non-ok premise is in the assumptions register above
        "6_convenience_checked": True,  # deterministic pass has no convenient branch to protect
        "7_hostile_reread": weak is not None or not premises,
        "8_answer_leads": True,
    }

    lead = "LEAD WITH: "
    if any(c.kind == "pct_change" and not c.agrees for c in checks):
        c = next(c for c in checks if c.kind == "pct_change" and not c.agrees)
        lead += f"the stated {c.claimed}% is wrong — it's {_fmt(c.recomputed)}% (Δ/old, not Δ/new)."
    elif any(p.status == "FALSE" for p in premises):
        lead += "the question's premise is false; here is the corrected framing, then the answer."
    else:
        lead += f"the {deliverable['artifact']}, then only the one caveat that changes the decision."

    trace = ReasoningTrace(
        task=task.strip(), intent=intent, deliverable=deliverable, problem_type=ptype,
        premises=premises, facts=facts, assumptions=assumptions, hypotheses=hyps,
        number_checks=checks, weak_link=weak, confidence=conf, answer_lead=lead,
        checklist=checklist, flags=flags,
    )
    return trace.as_dict()


# ---------------------------------------------------------------------------
# smoke
# ---------------------------------------------------------------------------
def _smoke() -> int:
    fails: list[str] = []

    # a task with an EMBEDDED FALSE PREMISE (33% should be 25%) + a causal presupposition
    task = ("Costs fell from $80k to $60k -- a 33% reduction. Since the new caching layer "
            "caused the drop, should we expand it to the other 3 services?")
    tr = run(task)

    print("problem_type:", tr["problem_type"])
    print("intent.accomplished:", tr["intent"]["accomplished"])
    print("flags:")
    for f in tr["flags"]:
        print("   -", f)
    print("premises:")
    for p in tr["premises"]:
        print(f"   [{p['status']}] {p['text']}  ::  {p['note']}")
    print("number_checks:")
    for c in tr["number_checks"]:
        print(f"   claim={c['claim']!r} claimed={c['claimed']} recomputed={c['recomputed']} "
              f"agrees={c['agrees']}\n      method: {c['method']}")
    print("hypotheses:", tr["hypotheses"])
    print("weak_link:", tr["weak_link"])
    print("confidence:", tr["confidence"])
    print("answer_lead:", tr["answer_lead"])
    print()

    # --- assertions: prove the premise flag ---
    pct = [c for c in tr["number_checks"] if c["kind"] == "pct_change"]
    if not pct:
        fails.append("did not detect the percent-change claim")
    else:
        c = pct[0]
        if c["agrees"]:
            fails.append("false 33% premise was NOT flagged (recompute wrongly agreed)")
        if abs(c["recomputed"] - 25.0) > 0.01:
            fails.append(f"recompute wrong: expected 25.0, got {c['recomputed']}")
    if not any(p["status"] == "FALSE" for p in tr["premises"]):
        fails.append("no premise marked FALSE")
    if "FALSE_PREMISE — do not answer the broken question well; correct it first" not in tr["flags"]:
        fails.append("FALSE_PREMISE flag missing")

    # --- prove the number was recomputed a SECOND way (Δ/old, distinct route stated) ---
    if pct and "Δ/old" not in pct[0]["method"]:
        fails.append("recompute did not use a distinct second route")

    # --- prove causal presupposition caught + >=2 hypotheses ---
    if not any(p["kind"] == "causal" and p["status"] == "unverified" for p in tr["premises"]):
        fails.append("causal presupposition not flagged")
    if len(tr["hypotheses"]) < 2:
        fails.append("hypothesis floor (>=2) breached")
    if tr["weak_link"] is None:
        fails.append("weak_link not identified")

    # --- a clean arithmetic-error task: '12 x 8 = 100' should fail recompute ---
    tr2 = run("Is 12 x 8 = 100 correct for the seat count?")
    ar = [c for c in tr2["number_checks"] if c["kind"] == "arithmetic"]
    if not ar or ar[0]["agrees"]:
        fails.append("arithmetic error (12*8=96, not 100) not caught")

    # --- a CLEAN task: correct number, no false premise -> no mismatch flags ---
    tr3 = run("Revenue rose from $100k to $150k -- a 50% increase. Ship the summary?")
    if any("NUMBER_MISMATCH" in f for f in tr3["flags"]):
        fails.append("clean 50% increase wrongly flagged as mismatch")

    if fails:
        print("SMOKE FAIL:")
        for m in fails:
            print("  -", m)
        return 1
    print("SMOKE OK — false 33% premise FLAGGED, number recomputed a second way as 25% (Δ/old not Δ/new), "
          "causal presupposition held open, >=2 hypotheses forced, weak-link surfaced, clean case stayed clean.")
    return 0


def scaffold_for(task: str, context=None) -> str:
    """Render the reasoning trace as a COMPACT text scaffold to prepend to a small local
    model's context ($0, no LLM). Returns "" when the task is trivial / nothing to steer on,
    so we never add noise. This is the 'smart without a premium API' lever: the local model
    reasons on top of pre-audited premises + a second-route number recompute instead of winging it."""
    try:
        r = run(task, context)
        d = r.as_dict() if hasattr(r, "as_dict") else r
    except Exception:
        return ""
    prem = d.get("premises", []) or []
    false_prem = [p for p in prem if p.get("status") == "FALSE"]
    unver = [p for p in prem if p.get("status") == "unverified"]
    checks = d.get("number_checks", []) or []
    hyps = d.get("hypotheses", []) or []
    # Emit ONLY when there's a concrete thing to steer on — a false/unverified premise or a
    # number to recheck. Generic multi-hypothesis alone (auto-generated for every judgment) is
    # boilerplate; it must not trigger a scaffold on a trivial lookup. Hypotheses still render
    # as context when we DO emit for a real reason.
    if not (false_prem or checks or unver):
        return ""
    L = ["[REASONING SCAFFOLD — auto-verified deterministically. Reason ON TOP of this; do not ignore it.]"]
    dl = d.get("deliverable", {}) or {}
    L.append(f"- Type: {d.get('problem_type','?')} | Deliverable: {dl.get('artifact','?')}")
    for p in false_prem:
        L.append(f"- ⚠ FALSE premise: \"{p.get('text','')}\" — {p.get('note','')}. Correct the asker; don't accept it.")
    for c in checks:
        if c.get("method"):
            L.append(f"- Number recheck (2nd route): {c.get('method')}")
    for p in unver[:2]:
        L.append(f"- Unverified premise (don't assume true): \"{p.get('text','')}\" — {p.get('note','')}")
    if len(hyps) > 1:
        L.append("- Hold ≥2 hypotheses before committing: " + " | ".join(hyps[:3]))
    if d.get("weak_link"):
        L.append(f"- Double-check the weakest link: {d.get('weak_link')}")
    L.append("- Lead with the answer; state confidence honestly.")
    return "\n".join(L)


if __name__ == "__main__":
    raise SystemExit(_smoke())
