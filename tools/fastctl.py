#!/usr/bin/env python3
"""fastctl — instant answers to frequently-asked questions, served from cache.

  fastctl brief           whole rollup (work+inbox+actions+meetings+revenue) from cache, 0 LLM tokens
  fastctl ask <topic>     instant cached answer (rebuilds if stale); records the ask
  fastctl topics          list available topics
  fastctl warm <topic>    rebuild one topic
  fastctl warm --all      rebuild every topic
  fastctl warm --auto     learn patterns, then pre-warm hot + time-anticipated topics
  fastctl refresh [topic] trigger a LIVE cross-system precompute (throttled), then rebuild
  fastctl hot             show the learned question-pattern ranking
  fastctl learn           re-mine chat history for question patterns
"""
from __future__ import annotations

import json
import sys
import time

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
from core import fast_cache as fc          # noqa: E402
from core import question_patterns as qp   # noqa: E402


def _print_answer(rec: dict):
    if rec.get("error"):
        print(rec["error"]); return
    print(rec["answer"])
    print(f"\n— served from {rec.get('served')} (age {rec.get('age')}, built {rec.get('built_ms','?')}ms)")


def main(argv):
    if not argv:
        print(__doc__); return
    cmd = argv[0]
    if cmd == "ask":
        if len(argv) < 2:
            print("usage: fastctl ask <topic>  | topics:", ", ".join(fc.TOPICS)); return
        _print_answer(fc.ask(argv[1]))
    elif cmd == "topics":
        for t, s in fc.TOPICS.items():
            c = fc.get_cached(t)
            age = fc._age_str(time.time() - c["generated_at"]) if c else "cold"
            print(f"  {t:<14} ttl={s['ttl']}s  cached={age:<6} — {s['desc']}")
    elif cmd == "warm":
        sel = argv[1] if len(argv) > 1 else "--auto"
        if sel == "--all":
            for t in fc.TOPICS:
                r = fc.build(t); print(f"  warmed {t} ({r['built_ms']}ms)")
        elif sel == "--auto":
            qp.learn_from_history()
            # if the underlying work precompute is very stale, trigger a live
            # refresh (throttled internally) so hot work data doesn't rot
            if fc._precompute_age_secs() > 10800:  # 3h
                r = fc.refresh()
                if r.get("triggered"):
                    print(f"  triggered live precompute ({r['job']})")
            want = qp.topics_for_hour()
            # hot/anticipated topics: always refresh; others: refresh if stale
            for t in fc.TOPICS:
                c = fc.get_cached(t)
                stale = (not c) or (time.time() - c.get("generated_at", 0)) > c.get("ttl", 0)
                if t in want or stale:
                    r = fc.build(t)
                    why = "hot/anticipated" if t in want else "stale"
                    print(f"  warmed {t} ({why}, {r['built_ms']}ms)")
        else:
            r = fc.build(sel); print(f"  warmed {sel} ({r['built_ms']}ms)")
    elif cmd == "brief":
        # Whole rollup from cache in one shot — replaces a multi-tool LLM loop ($0).
        order = ["work_context", "inbox", "action_items", "meetings", "revenue"]
        for t in order:
            if t in fc.TOPICS:
                print(fc.ask(t)["answer"]); print()
        print("— full brief served from cache (0 LLM tokens)")
    elif cmd == "refresh":
        r = fc.refresh(force=("--force" in argv))
        print("live precompute:", "triggered" if r.get("triggered") else f"skipped ({r.get('reason')})")
        which = [a for a in argv[1:] if not a.startswith("-")]
        for t in (which or fc.TOPICS):
            if t in fc.TOPICS:
                fc.build(t)
        print("rebuilt:", ", ".join(which or list(fc.TOPICS)))
    elif cmd in ("hot", "learn"):
        p = qp.learn_from_history() if cmd == "learn" else qp.rank()
        print("Question patterns (last %sd, %s turns scanned):" % (p.get("days"), p.get("scanned_turns")))
        for t in p.get("ranked", []):
            print(f"  {t:<14} {p['counts'].get(t,0):>4} asks  {'🔥 HOT' if t in p.get('hot',[]) else ''}")
        print("Anticipated for this hour:", ", ".join(qp.topics_for_hour()) or "(none yet)")
    else:
        print(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
