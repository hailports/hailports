#!/usr/bin/env python3
"""A/B/C outreach variant scoreboard — which message hook actually converts.

Reads sends (the relentless campaign log) + human visits/captures (funnel events, bot-filtered,
attributed by utm_content=variant) per variant. Run anytime; the table fills in as sends fire and
clicks land. This is the 'which hook wins' readout for the rotating proof-first emails.

  python3 tools/variant_report.py
"""
import collections
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _load_variants():
    """Base A/B/C hooks PLUS any strategist-added variants. Additive + guarded file-exists
    (absent => base only); reversible by deleting data/hustle/variant_bank_extra.json. This
    is the LIVE consumer that proves a strategist 'copy_variant' play is real: the bandit
    (tools/variant_optimizer.py) iterates VARIANTS, so a new id here gets weighted + tested."""
    base = ["consideration", "curiosity", "roi"]
    extra_f = ROOT / "data" / "hustle" / "variant_bank_extra.json"
    try:
        if extra_f.exists():
            for x in json.loads(extra_f.read_text()):
                vid = (x.get("id") if isinstance(x, dict) else x)
                vid = str(vid or "").strip().lower()
                if vid and vid not in base:
                    base.append(vid)
    except Exception:
        pass
    return tuple(base)


VARIANTS = _load_variants()


def _rows(p: Path):
    if not p.exists():
        return
    for line in p.read_text(errors="ignore").splitlines():
        try:
            yield json.loads(line)
        except Exception:
            continue


def _ev_variant(ev: dict) -> str:
    v = (ev.get("utm_content")
         or (ev.get("attr") or {}).get("utm_content")
         or (ev.get("attrs") or {}).get("utm_content"))
    if not v:
        u = ev.get("url") or ""
        if "utm_content=" in u:
            v = u.split("utm_content=", 1)[1].split("&")[0]
    return (v or "").strip().lower()


def compute_tally() -> dict:
    """Per-variant {sent, visit, capture} from the campaign log + bot-filtered funnel events."""
    tally = {v: collections.Counter() for v in VARIANTS}

    for r in _rows(ROOT / "data/hustle/relentless_campaign.jsonl"):
        v = str(r.get("variant") or "").lower()
        action = str(r.get("action") or "").lower()
        if v in tally and (action == "sent" or r.get("track") or r.get("sent") is True):
            tally[v]["sent"] += 1

    try:
        from core.traffic_classifier import classify
    except Exception:
        classify = None
    for ev in _rows(ROOT / "products/self_serve/funnel_events.jsonl"):
        v = _ev_variant(ev)
        if v not in tally:
            continue
        human = True
        if classify:
            try:
                human = bool(classify(ev).get("is_human", True))
            except Exception:
                human = True
        if not human:
            continue
        kind = str(ev.get("event") or "").lower()
        if "capture" in kind:
            tally[v]["capture"] += 1
        elif kind == "pageview" or "visit" in kind:
            tally[v]["visit"] += 1
    return tally


def main() -> None:
    tally = compute_tally()
    print(f"{'variant':16}{'sent':>7}{'human visits':>14}{'captures':>10}{'cap/sent':>11}")
    print("-" * 58)
    for v in VARIANTS:
        t = tally[v]
        rate = (t["capture"] / t["sent"] * 100) if t["sent"] else 0.0
        print(f"{v:16}{t['sent']:>7}{t['visit']:>14}{t['capture']:>10}{rate:>10.1f}%")
    total = sum(tally[v]["sent"] for v in VARIANTS)
    best = max(VARIANTS, key=lambda v: (tally[v]["capture"], tally[v]["visit"]))
    print("-" * 58)
    print(f"sends so far: {total}  |  leading hook: "
          f"{best if total else '(no data yet — live sends start 7am CDT)'}")


if __name__ == "__main__":
    main()
