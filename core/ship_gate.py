#!/usr/bin/env python3
"""ship_gate.py — adversarial "would this flop?" harness before a public artifact ships.

The deterministic gates (content_quality.claims_ok, anon_scrub, the explain-back regex)
catch KNOWN failure classes. This catches the UNKNOWN ones: a harsh-editor LLM pass that
tries to find why a post/reply/caption would flop, read as a bot, or embarrass the brand —
the stuff you only notice on read-back. Returns a structured verdict so a caller can skip /
regenerate the weakest 20% instead of shipping it.

LOCAL + $0 (routes through the stack's local model). Fail-OPEN by design: if the judge is
unavailable it returns ship=True (never block a genuine artifact on a broken judge) — the
deterministic gates remain the hard floor; this is a quality lift, not a safety gate.

    from core.ship_gate import ship_check
    v = ship_check(text, kind="x_reply", context=the_post_we_reply_to)
    if not v["ship"]:
        # regenerate or skip; v["weakest"] / v["fix"] say why
"""
from __future__ import annotations

import json
import os
import re
import urllib.request

OLLAMA = os.environ.get("OLLAMA_URL", "http://localhost:11434")
MODEL = os.environ.get("SHIP_GATE_MODEL", os.environ.get("LOCAL_MODEL", "qwen2.5:7b"))
# score >= this ships. Tunable per surface; default 6/10 = "clears the bar, not necessarily great".
SHIP_FLOOR = int(os.environ.get("SHIP_GATE_FLOOR", "6"))

_LENS = {
    "x_reply": "a reply to another builder on X. It flops if it recaps their own post back at "
               "them, is generic, sounds like a bot/coach, or adds nothing they didn't already know.",
    "x_post": "a build-in-public post on X from a faceless AI-ops brand. It flops if it's vague, "
              "brags without a concrete specific, sounds AI-generated, or over-claims.",
    "tiktok_comment": "a comment on someone's short video. It flops if it's generic, salesy, or "
                      "recaps their video back to them.",
    "caption": "a short-video caption. It flops if it's clickbait-hollow, generic, or over-claims.",
    "generic": "a public post. It flops if it's generic, sounds AI-generated, or over-claims.",
}


def _local(prompt: str, *, num_predict: int = 120) -> str:
    body = json.dumps({"model": MODEL, "prompt": prompt, "stream": False,
                       "options": {"num_predict": num_predict, "temperature": 0.0}}).encode()
    req = urllib.request.Request(f"{OLLAMA}/api/generate", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode()).get("response", "").strip()


def ship_check(text: str, *, kind: str = "generic", context: str = "") -> dict:
    """Returns {ship, score, weakest, fix}. Fail-open (ship=True) if the judge is down."""
    text = (text or "").strip()
    if not text:
        return {"ship": False, "score": 0, "weakest": "empty", "fix": ""}
    lens = _LENS.get(kind, _LENS["generic"])
    ctx = f"\nIt is responding to:\n{context[:400]}\n" if context else ""
    # Few-shot ANCHORS calibrate the small model so it scores RELATIVELY (a harsh 7b otherwise
    # rates everything ~2). Anchors: a clear flop=2, a clear win=8.
    prompt = (
        f"You are a sharp editor scoring {lens}\n"
        "Score 0-10: 0-4 = flop (generic/botlike/recap/overclaim), 5-6 = passable, "
        "7-10 = genuinely good (specific, human, adds real value). Calibrate to these:\n"
        'EX_FLOP: "great post! your app sounds robust, keep it up" -> {"score":2,"weakest":"generic filler","fix":"say something specific"}\n'
        'EX_GOOD: "the 32k ctx window is where local r1 chokes for me - are you chunking or '
        'summarizing the transcript first?" -> {"score":8,"weakest":"slightly niche","fix":"none needed"}\n'
        f"{ctx}\nNOW SCORE THIS DRAFT:\n{text}\n\n"
        "Reply as compact JSON on ONE line, same shape as the examples:\n"
        '{"score": <0-10>, "weakest": "<=8 words", "fix": "<=10 words"}'
    )
    try:
        raw = _local(prompt)
    except Exception:
        return {"ship": True, "score": SHIP_FLOOR, "weakest": "judge-unavailable", "fix": ""}
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return {"ship": True, "score": SHIP_FLOOR, "weakest": "unparseable-judge", "fix": ""}
    try:
        d = json.loads(m.group())
    except Exception:
        num = re.search(r'"?score"?\s*:?\s*(\d+)', raw)
        d = {"score": int(num.group(1)) if num else SHIP_FLOOR}
    score = max(0, min(10, int(d.get("score", SHIP_FLOOR))))
    return {"ship": score >= SHIP_FLOOR, "score": score,
            "weakest": str(d.get("weakest", ""))[:80], "fix": str(d.get("fix", ""))[:100]}


def _selftest() -> int:
    cases = [
        ("the app focuses on scraping videos and reporting trends. your tech stack sounds robust.",
         "x_reply", "should FLOP (recap + generic)"),
        ("reranking is the part everyone skips, then wonders why recall tanks. what reranker are you using?",
         "x_reply", "should SHIP (specific + real question)"),
    ]
    ok = True
    for text, kind, note in cases:
        v = ship_check(text, kind=kind)
        print(f"  score={v['score']} ship={v['ship']} weakest='{v['weakest']}'  [{note}]")
    print("selftest ran (LLM-dependent; check scores are directionally sane)")
    return 0 if ok else 1


if __name__ == "__main__":
    import sys
    raise SystemExit(_selftest() if "--selftest" in sys.argv else 0)
