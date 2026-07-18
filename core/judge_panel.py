#!/usr/bin/env python3
"""Judge panel — multi-hypothesis + independent judges for HIGH-STAKES calls.

The frontier move against anchoring (Fable §3: "generate at least two hypotheses before
investigating any one"). A single-hypothesis "investigation" is just confirmation. For an
irreversible or expensive decision the clone must NOT run its first idea — it must fan out N
independent approaches from DIVERSE angles (cheap local model), then judge them with
INDEPENDENT passes so no one framing anchors the verdict, then pick the winner and surface
the runner-ups as ideas to GRAFT rather than discard.

decide(question, options=None, n=3, ...) -> {
    winner:     {option, score, why, dimensions},
    runner_ups: [{option, score, graft_hint}],   # not discarded — grafted
    ranked:     [ ...full scored slate... ],
    unanimous, escalated, judges_ran, n_options, via,
}

Each option is scored on three dimensions, reusing the faculties already built — PILFER, don't
reinvent the judgment:
  - correctness  : an injected domain scorer_fn (neutral 0.5 when absent).
  - risk         : core.self_verify.verify — an unsafe/refuted option is near-disqualified.
  - would-Operator   : core.decision_modeling.score — his modeled doctrine (approve|reject|revise).

Aggregation is DETERMINISTIC. The panel runs `judges` independent passes; when they AGREE on
the winner it stops cheap (a unanimous trivial call never pays for more judges). It escalates —
adds judges up to max_judges — ONLY when the passes DISAGREE, spending compute exactly where the
call is genuinely close. With no model-backed judge_fn the passes are identical by construction,
so trivial calls short-circuit to a single pass.

Additive + import-only. No live service touched. Model generation/judging is opt-in (generate_fn
/ judge_fn injected, or allow_model=True); the __main__ smoke runs fully offline with seeded fns.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Callable

if __package__ in (None, ""):  # allow `python core/judge_panel.py`
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from core import decision_modeling as _dm
except Exception:  # fail-soft: doctrine dimension degrades to neutral
    _dm = None
try:
    from core import self_verify as _sv
except Exception:  # fail-soft: risk dimension degrades to neutral-safe
    _sv = None

# Diverse framings so the N hypotheses are genuinely independent, not one idea reworded.
_ANGLES = (
    "the conservative, lowest-risk path",
    "the aggressive, highest-upside path",
    "an orthogonal option the obvious framing misses",
    "the reversible / cheapest-to-undo path",
    "the option that best fits Operator's stated doctrine",
)

# dimension weights — risk is intentionally asymmetric: a safe option gets a small bonus, an
# unsafe one takes a dominating penalty so it can never out-rank a safe option (rails win).
_W_CORRECT = 0.5
_W_DOCTRINE = 0.35
_RISK_SAFE = 0.15
_RISK_UNSAFE = -1.5  # <= -1.20 keeps the safety rail dominant so no unsafe option can out-rank a safe one


def _score_option(opt: str, question: str, ctx: dict,
                  scorer_fn: Callable | None) -> dict:
    """Score one option across correctness / doctrine / risk -> a single aggregate."""
    # -- would-Operator (doctrine) --
    d_score, d_why, d_stance = 0.0, "doctrine module unavailable", "unknown"
    if _dm is not None:
        try:
            r = _dm.score(opt, ctx or None)
            d_stance = r.get("alex_would", "unknown")
            conf = float(r.get("confidence", 0.5) or 0.5)
            if d_stance == _dm.APPROVE:
                d_score = conf
            elif d_stance == _dm.REJECT:
                d_score = -conf
            elif d_stance == _dm.REVISE:
                d_score = -0.2
            d_why = r.get("why", "")
        except Exception:
            pass

    # -- risk (red-team) --
    safe, r_score, r_why = True, _RISK_SAFE, "no refutation"
    if _sv is not None:
        try:
            v = _sv.verify({"summary": opt, "ctx": dict(ctx)})
            safe = bool(v.get("safe", True))
            if not safe:
                r_score = _RISK_UNSAFE
                r_why = "; ".join(v.get("refutations", []))[:200] or "refuted"
        except Exception:
            pass

    # -- correctness (domain) --
    if callable(scorer_fn):
        try:
            c = float(scorer_fn(opt, question))
        except Exception:
            c = 0.5
    else:
        c = 0.5
    c = max(0.0, min(1.0, c))
    c_score = c - 0.5  # center so neutral contributes nothing

    aggregate = _W_CORRECT * c_score + _W_DOCTRINE * d_score + r_score
    return {
        "aggregate": aggregate,
        "correctness": c,
        "doctrine": d_score,
        "doctrine_stance": d_stance,
        "risk": r_score,
        "safe": safe,
        "doctrine_why": d_why,
        "risk_why": r_why,
    }


def _local_gen(question: str, angle: str) -> str | None:
    """Opt-in local generation of one hypothesis from a given angle. Fail-soft, $0."""
    try:
        import asyncio

        from core.local_client import generate
        prompt = (f"High-stakes decision: {question}\n\n"
                  f"Propose ONE concrete option, taking {angle}. One or two sentences, no preamble.")
        out = asyncio.run(generate(prompt, max_tokens=200, temperature=0.4,
                                   system="You are Operator's sharp operator. Output only the option."))
        return (out or "").strip() or None
    except Exception:
        return None


def _generate(question: str, n: int, generate_fn: Callable | None,
              allow_model: bool) -> tuple[list, str]:
    """Fan out N independent hypotheses from diverse angles (anchoring defense)."""
    opts, via = [], "none"
    for i in range(max(1, n)):
        angle = _ANGLES[i % len(_ANGLES)]
        o = None
        if callable(generate_fn):
            via = "model"
            try:
                o = generate_fn(question, angle)
            except Exception:
                o = None
        elif allow_model:
            via = "model"
            o = _local_gen(question, angle)
        if o and str(o).strip():
            opts.append(str(o).strip())
    return opts, via


def _judge_score(judge_fn: Callable | None, opt: str, question: str,
                 dims: dict, seed: int) -> float:
    """One judge's score for one option. Deterministic base; judge_fn perturbs per pass."""
    if callable(judge_fn):
        try:
            return float(judge_fn(opt, question, dims, seed))
        except Exception:
            return dims["aggregate"]
    return dims["aggregate"]


def _why(w: dict) -> str:
    """Lead-with-the-answer rationale for the winner, in plain terms."""
    bits = []
    if w["correctness"] >= 0.66:
        bits.append("strongest correctness")
    if w["doctrine"] > 0.1:
        bits.append(f"Operator would {w['doctrine_stance']}")
    elif w["doctrine"] < -0.05:
        bits.append(f"doctrine caveat ({w['doctrine_stance']})")
    bits.append("clears the risk red-team" if w["safe"] else "carries risk flags")
    return "picked: " + ", ".join(bits) + "."


def _graft_hint(winner: dict, ru: dict) -> str:
    """What the runner-up beats the winner on — the reason to graft it, not discard it."""
    edges = []
    if ru["correctness"] > winner["correctness"] + 1e-9:
        edges.append("its sharper correctness angle")
    if ru["doctrine"] > winner["doctrine"] + 1e-9:
        edges.append("its stronger doctrine fit")
    if ru["safe"] and not winner["safe"]:
        edges.append("its cleaner risk profile")
    return ("graft " + " + ".join(edges)) if edges else "no distinct edge over the winner — hold as fallback"


def decide(question: str, options: list | None = None, n: int = 3, *,
           generate_fn: Callable | None = None,
           scorer_fn: Callable | None = None,
           judge_fn: Callable | None = None,
           judges: int = 3, max_judges: int = 7,
           context: dict | None = None,
           allow_model: bool | None = None) -> dict:
    """Multi-hypothesis + judge-panel decision for a HIGH-STAKES call.

    Fan out N diverse hypotheses (or take `options`), score each on correctness/risk/doctrine,
    run independent judge passes, escalate only on disagreement, return winner + graftable
    runner-ups. Gate irreversible actions through this before acting.
    """
    q = (question or "").strip()
    ctx = context or {}
    allow = bool(allow_model)
    via = "deterministic"

    if options is None:
        options, gvia = _generate(q, n, generate_fn, allow)
        if gvia == "model":
            via = "model"

    options = list(dict.fromkeys(str(o).strip() for o in (options or []) if str(o).strip()))
    if not options:
        return {"question": q, "winner": None, "runner_ups": [], "ranked": [],
                "n_options": 0, "judges_ran": 0, "unanimous": True, "escalated": False,
                "via": via, "why": "no hypotheses to judge — supply options or a generate_fn."}

    base = {opt: _score_option(opt, q, ctx, scorer_fn) for opt in options}

    def _key(opt):  # deterministic tie-break: higher score, then original order
        return (-base[opt]["aggregate"], options.index(opt))

    if judge_fn is None:
        # deterministic judges are identical by construction -> unanimous -> short-circuit
        # to a single effective pass. This is the "trivial call stays cheap" path.
        ranked = sorted(options, key=_key)
        judges_ran, unanimous, escalated = 1, True, False
        mean = {opt: base[opt]["aggregate"] for opt in options}
    else:
        via = "model"
        per_pass: list[dict] = []
        winners: list[str] = []
        judges_ran, escalated = 0, False
        cap = max(1, judges)
        while True:
            for seed in range(judges_ran, cap):
                sc = {opt: _judge_score(judge_fn, opt, q, base[opt], seed) for opt in options}
                per_pass.append(sc)
                winners.append(min(options, key=lambda o: (-sc[o], options.index(o))))
                judges_ran += 1
            unanimous = len(set(winners)) == 1
            if unanimous or judges_ran >= max_judges:
                break
            escalated = True  # passes disagree -> spend more judges where it's genuinely close
            cap = min(max_judges, judges_ran + 2)
        mean = {opt: sum(p[opt] for p in per_pass) / len(per_pass) for opt in options}
        ranked = sorted(options, key=lambda o: (-mean[o], options.index(o)))

    def _entry(opt):
        d = base[opt]
        return {"option": opt, "score": round(mean[opt], 4),
                "dimensions": {k: (round(v, 4) if isinstance(v, float) else v)
                               for k, v in d.items()}}

    win = base[ranked[0]]
    winner = _entry(ranked[0])
    winner["why"] = _why(win)
    runner_ups = []
    for opt in ranked[1:]:
        e = {"option": opt, "score": round(mean[opt], 4),
             "graft_hint": _graft_hint(win, base[opt])}
        runner_ups.append(e)

    return {
        "question": q,
        "winner": winner,
        "runner_ups": runner_ups,
        "ranked": [_entry(o) for o in ranked],
        "n_options": len(options),
        "judges_ran": judges_ran,
        "unanimous": unanimous,
        "escalated": escalated,
        "via": via,
    }


def _smoke() -> int:
    ok = True

    def check(cond, msg):
        nonlocal ok
        print(("PASS" if cond else "FAIL") + " " + msg)
        ok = ok and cond

    # --- Test A: high-stakes call, N hypotheses GENERATED, judged, winner + graftable runner-ups.
    angles_seen = []

    def gen(q, angle):
        angles_seen.append(angle)
        return {
            "the conservative, lowest-risk path":
                "stage a single hand-picked proof send to one intent-established prospect, dry-run first",
            "the aggressive, highest-upside path":
                "blast the cold offer to all contacts in the list today to maximize reach",
            "an orthogonal option the obvious framing misses":
                "publish one faceless build-in-public proof piece and let inbound self-select",
        }.get(angle, f"option for {angle}")

    # correctness scorer: reward the targeted/orthogonal plays, punish the blast
    def scorer(opt, q):
        o = opt.lower()
        if "blast" in o or "all contacts" in o:
            return 0.2
        if "one intent-established" in o or "hand-picked" in o:
            return 0.9
        return 0.7

    res = decide("how do we get our first real revenue this week", n=3,
                 generate_fn=gen, scorer_fn=scorer)
    check(res["n_options"] == 3, f"generated N=3 hypotheses (got {res['n_options']})")
    check(len(set(angles_seen)) == 3, "hypotheses came from 3 DISTINCT angles (anchoring defense)")
    check(res["winner"] is not None and res["winner"].get("why"),
          f"winner picked with a reason: {res['winner']['why'] if res['winner'] else None!r}")
    check("blast" not in res["winner"]["option"].lower(),
          "the spray/blast option did NOT win (risk red-team + correctness sank it)")
    blast = next((r for r in res["ranked"] if "blast" in r["option"].lower()), None)
    check(blast is not None and blast["dimensions"]["safe"] is False,
          "blast option flagged UNSAFE by the self_verify risk dimension")
    check(len(res["runner_ups"]) == 2 and all(r.get("graft_hint") for r in res["runner_ups"]),
          "runner-ups returned as graftable ideas, not discarded")

    # --- Test B: unanimous trivial call short-circuits cheaply (no model judge -> 1 pass, no escalation).
    triv = decide("pick a placeholder label",
                  options=["option alpha", "option beta", "option gamma"],
                  scorer_fn=lambda o, q: 0.9 if o.endswith("alpha") else 0.4)
    check(triv["unanimous"] and not triv["escalated"],
          "trivial call is unanimous and did not escalate")
    check(triv["judges_ran"] == 1,
          f"trivial call short-circuited to a single judge pass (ran {triv['judges_ran']})")
    check(triv["winner"]["option"] == "option alpha", "trivial call picked the best-scored option")

    # --- Test C: disagreeing judges ESCALATE past the base panel; deterministic aggregate still resolves.
    # judge_fn flips its preferred option by seed parity -> base 3 judges split -> must add judges.
    def flip_judge(opt, q, dims, seed):
        favored = "x" if seed % 2 == 0 else "y"
        return 1.0 if opt == favored else 0.0

    disc = decide("close call", options=["x", "y"], judges=3, max_judges=7, judge_fn=flip_judge)
    check(disc["escalated"] and disc["judges_ran"] > 3,
          f"disagreeing judges escalated past base panel (ran {disc['judges_ran']}, escalated={disc['escalated']})")
    check(disc["winner"] is not None, "escalated panel still resolved a deterministic winner")

    print("\n" + ("SMOKE OK" if ok else "SMOKE FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(_smoke())
