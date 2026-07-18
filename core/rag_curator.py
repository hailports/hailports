#!/usr/bin/env python3
"""rag_curator.py — autonomous topic-RAG curator (self-detects + self-fills + loops).

The substrate already exists (core.nervous_system: a lane-firewalled facts/recall store,
two physical DBs hustle.db ⟂ work.db). What was missing is the thing Operator asked for: nothing
watched the activity stream, RECOGNISED a recurring topic worth a corpus, and filled+maintained
it on its own. This does that.

Per lane it: (1) gathers recent signal, (2) auto-detects recurring topics — the work ones
(DPP, SP Portal, tickets…), the hustle ones (BrandA GTM, hailports strategy…), and the stack's
own tuning (diagnostician/healer/swap/RAG…), (3) after a topic RECURS (persistence gate, so
one-off noise never gets RAG'd) synthesises durable facts and remember()s them into the correct
lane's nervous_system, (4) loops on a schedule, refreshing live topics and ageing out dead ones.

FIREWALL (HARD): work topics are sourced ONLY from work data and written ONLY to work.db.
Everything hustle-side — including stack-tuning meta, which is hustle INFRASTRUCTURE, never
the employer work lane — goes to hustle.db (tagged topic:stack-*). VALID_LANES is left untouched.

Reuses, never rebuilds: nervous_system (store+recall), git log (stack-tuning), strategist_memory
(hustle plays). LLM synthesis is best-effort + headroom-gated; falls back to a deterministic
summary so the curator always works $0 and never adds memory pressure under load.
"""
from __future__ import annotations
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import nervous_system as ns  # lane-firewalled store

NERVOUS_DIR = ROOT / "data" / "nervous"
REGISTRY = lambda lane: NERVOUS_DIR / f"rag_topics_{lane}.json"

WINDOW_S = 7 * 24 * 3600          # look back a week
MIN_ITEMS = 3                     # a topic needs this many signals in a run OR a prior sighting
MAX_TOPICS = 24                   # cap promoted topics per lane per run
FACT_MAX = 400

# Seed entities per concern — a BOOSTER for the frequency detector, not the whole story; emergent
# topics are still caught by capitalised-phrase / acronym frequency. Extend freely.
SEEDS = {
    "work":   ["dpp", "sp portal", "sp-portal", "salesforce", "ticket", "ado", "exalate",
               "rebate", "forecast", "aftermarket", "monday", "outlook", "zoom", "permission set",
               "flow", "apex", "sandbox", "sales order", "login as", "assignee"],
    "hustle": ["BrandA", "hailports", "scannerapp", "geo", "broken site", "broken-site", "gtm",
               "persona3", "gumroad", "case study", "outreach", "deliverability", "rescue",
               "self-serve", "landing", "mockup"],
    # stack-tuning lives in the hustle lane (hustle infra) but has its own seed vocabulary
    "stack":  ["diagnostician", "healer", "self-heal", "watchdog", "alert", "swap", "ram", "perf",
               "launchd", "ollama", "mlx", "rag", "producer freshness", "searxng", "colima",
               "nervous system", "clean_capture", "preemption", "mockup deploy", "pii gate"],
}

# ---------------------------------------------------------------------------
# signal sources (each fail-open; each is lane-bound by construction)
# ---------------------------------------------------------------------------
def _events(lane_db: str, since_s: float) -> list[dict]:
    """Read subjects from a nervous_system lane db — already lane-separated by construction."""
    import sqlite3
    p = NERVOUS_DIR / f"{lane_db}.db"
    if not p.exists():
        return []
    try:
        conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=3)
        try:
            rows = conn.execute(
                "SELECT ts, COALESCE(kind,''), COALESCE(source,''), COALESCE(subject,'') "
                "FROM events WHERE ts>=? ORDER BY ts DESC LIMIT 4000", (since_s,)).fetchall()
        finally:
            conn.close()
        return [{"ts": r[0], "text": f"{r[1]} {r[3]}".strip(), "source": r[2]} for r in rows if (r[1] or r[3])]
    except Exception:
        return []


def _git_signal(since_days: int = 7) -> list[dict]:
    """Stack-tuning signal: recent commit subjects (this is the machine describing its own work)."""
    try:
        out = subprocess.run(
            ["git", "-C", str(ROOT), "log", f"--since={since_days} days ago",
             "--pretty=format:%ct\t%s"], capture_output=True, text=True, timeout=15).stdout
    except Exception:
        return []
    items = []
    for ln in out.splitlines():
        if "\t" not in ln:
            continue
        ts, subj = ln.split("\t", 1)
        try:
            items.append({"ts": float(ts), "text": subj.strip(), "source": "git"})
        except ValueError:
            pass
    return items


def _work_index_signal(since_s: float) -> list[dict]:
    """Work-lane topical signal: recent doc TITLES from the unified work-context index
    (email/meetings/SF/Monday/Zoom) — where DPP, SP Portal, tickets etc. actually surface.
    Titles only (never bodies) keeps it light and leak-safe within the work lane."""
    import sqlite3
    p = ROOT / "data" / "work_context.db"
    if not p.exists():
        return []
    try:
        conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=3)
        try:
            rows = conn.execute(
                "SELECT ts, COALESCE(source,''), COALESCE(title,'') FROM docs "
                "WHERE title IS NOT NULL AND title!='' ORDER BY ts DESC LIMIT 3000").fetchall()
        finally:
            conn.close()
        out = []
        for ts, src, title in rows:
            try:
                if float(ts or 0) >= since_s:
                    out.append({"ts": float(ts), "text": title.strip()[:200], "source": f"work:{src}"})
            except (ValueError, TypeError):
                out.append({"ts": 0, "text": title.strip()[:200], "source": f"work:{src}"})
        return out
    except Exception:
        return []


def _strategist_signal() -> list[dict]:
    try:
        from core import strategist_memory as sm
        plays = sm.recent_plays(80) or []
    except Exception:
        return []
    items = []
    for p in plays:
        txt = " ".join(str(p.get(k, "")) for k in ("kind", "query", "variant", "slug", "note"))
        if txt.strip():
            items.append({"ts": 0, "text": txt.strip()[:300], "source": "strategist"})
    return items


def _signal_for(lane: str) -> list[dict]:
    """Lane-correct sourcing. WORK reads only work data; HUSTLE reads hustle + stack-meta."""
    since = time.time() - WINDOW_S
    if lane == "work":
        return _events("work", since) + _work_index_signal(since)
    # hustle: hustle events + strategist plays + stack-tuning (git)
    return _events("hustle", since) + _strategist_signal() + _git_signal()


# ---------------------------------------------------------------------------
# topic detection (deterministic; $0; no LLM needed to work)
# ---------------------------------------------------------------------------
_STOP = set("the and for with that this from your you have will are was has not but can its into "
            "our out off now new fix fixed add added via per job run ran log logs stack test".split())
# generic words the emergent capitalised/acronym detector picks up that are never real topics
_JUNK = set("recent utc gmt closed open Operator true false none null error warn info notice ok yes no "
            "today yesterday week day time date update updated new the this that http https www com "
            "id re fwd am pm eod eow".split())


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:48]


def _detect(items: list[dict], seeds: list[str]) -> dict[str, dict]:
    """Return slug -> {title, count, samples[]}. Seeds are boosted; emergent multi-word
    capitalised phrases and repeated significant tokens are also surfaced."""
    texts = [it["text"] for it in items if it.get("text")]
    blob = " \n".join(texts)
    low = blob.lower()
    cand: dict[str, dict] = {}

    def bump(key: str, title: str, sample: str):
        c = cand.setdefault(key, {"title": title, "count": 0, "samples": []})
        c["count"] += 1
        if sample and len(c["samples"]) < 4 and sample not in c["samples"]:
            c["samples"].append(sample[:160])

    # 1) seeded entities
    for seed in seeds:
        s = seed.lower()
        for it in items:
            t = (it.get("text") or "")
            if s in t.lower():
                bump(_slug(seed), seed.title() if " " in seed else seed.upper() if len(seed) <= 4 else seed.capitalize(), t)

    # 2) emergent: repeated multi-word Capitalised phrases + ALLCAPS acronyms
    for m in re.finditer(r"\b([A-Z][a-zA-Z0-9]+(?:[ -][A-Z][a-zA-Z0-9]+){0,2}|[A-Z]{2,6}\b)", blob):
        phrase = m.group(1).strip()
        # emergent topics must be MULTI-WORD (or hyphenated) — a single Capitalised word is almost
        # always a person's name or a generic term, not a topic. Single-word real topics (DPP,
        # ticket, rebate…) are caught by the seed pass instead.
        if " " not in phrase and "-" not in phrase:
            continue
        toks = [w for w in re.split(r"[ -]", phrase.lower()) if w and w not in _STOP]
        if not toks or len(phrase) < 5:
            continue
        # only keep phrases that recur
        if low.count(phrase.lower()) >= MIN_ITEMS:
            bump(_slug(phrase), phrase, phrase)

    # keep only recurring candidates; drop generic-junk slugs
    return {k: v for k, v in cand.items()
            if v["count"] >= 1 and k not in _JUNK and k.replace("-", " ").strip() not in _JUNK}


# ---------------------------------------------------------------------------
# fact synthesis (LLM best-effort + headroom-gated; deterministic fallback)
# ---------------------------------------------------------------------------
def _synthesize(title: str, samples: list[str], count: int = 0) -> str:
    det = f"{title}: active topic — {count or len(samples)} recent signals incl. " + "; ".join(samples[:3])
    det = det[:FACT_MAX]
    try:
        from tools import rag as _rag
        if not _rag._headroom_ok():   # never load a model under memory pressure
            return det
        prompt = ("Summarise this recurring topic in ONE durable factual sentence an agent could "
                  f"reuse later. Topic: {title}. Recent signals:\n- " + "\n- ".join(samples[:6]) +
                  "\nOne sentence, concrete, no preamble.")
        out = (_rag.generate(prompt, num_predict=90, temperature=0.1) or "").strip()
        out = out.splitlines()[0].strip() if out else ""
        return (f"{title}: {out}")[:FACT_MAX] if len(out) > 12 else det
    except Exception:
        return det


# ---------------------------------------------------------------------------
# registry (persistence gate + loop state)
# ---------------------------------------------------------------------------
def _load_reg(lane: str) -> dict:
    try:
        return json.loads(REGISTRY(lane).read_text())
    except Exception:
        return {}


def _save_reg(lane: str, reg: dict) -> None:
    try:
        REGISTRY(lane).parent.mkdir(parents=True, exist_ok=True)
        tmp = REGISTRY(lane).with_suffix(".tmp")
        tmp.write_text(json.dumps(reg, indent=2))
        tmp.replace(REGISTRY(lane))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def curate(lane: str, *, dry_run: bool = False) -> dict:
    """Detect + fill topic RAG for one lane. lane in {'work','hustle'} — 'stack' folds into hustle."""
    store_lane = "hustle" if lane == "stack" else lane
    if store_lane not in ("hustle", "work"):
        return {"ok": False, "error": f"bad lane {lane}"}

    seeds = SEEDS.get(lane, SEEDS.get(store_lane, []))
    # when curating hustle, include the stack seed vocabulary too (stack-tuning lives here)
    if lane == "hustle":
        seeds = seeds + SEEDS["stack"]

    items = _signal_for(lane)
    detected = _detect(items, seeds)
    reg = _load_reg(store_lane)
    now = time.time()

    promoted, skipped = [], []
    brain = None if dry_run else ns.NervousSystem(store_lane)

    for slug, info in sorted(detected.items(), key=lambda kv: kv[1]["count"], reverse=True)[:MAX_TOPICS * 2]:
        rec = reg.get(slug, {"seen": 0, "first": now, "facts": 0})
        rec["seen"] += 1
        rec["last"] = now
        rec["title"] = info["title"]
        # PERSISTENCE GATE: promote only once a topic recurs — either enough signals THIS run,
        # or it was seen in a prior run. One-off noise (seen==1, count<MIN_ITEMS) is held back.
        eligible = info["count"] >= MIN_ITEMS or rec["seen"] >= 2
        if eligible and len(promoted) < MAX_TOPICS:
            fact = _synthesize(info["title"], info["samples"], info["count"])
            tag = "stack" if (lane == "hustle" and slug in {_slug(s) for s in SEEDS["stack"]}) else lane
            if not dry_run and brain is not None:
                conf = min(0.85, 0.55 + 0.03 * info["count"])
                brain.remember(fact, confidence=conf, tags=["auto-rag", f"topic:{slug}", tag])
                rec["facts"] += 1
            promoted.append({"slug": slug, "title": info["title"], "count": info["count"],
                             "seen": rec["seen"], "fact": fact})
        else:
            skipped.append(slug)
        reg[slug] = rec

    # age-out: drop registry topics unseen for >30d
    for slug in list(reg):
        if now - reg[slug].get("last", now) > 30 * 24 * 3600:
            reg.pop(slug, None)

    if not dry_run:
        _save_reg(store_lane, reg)
        try:
            ns.NervousSystem(store_lane).observe(
                "rag_curate", "rag_curator",
                subject=f"{lane}: promoted {len(promoted)} topics from {len(items)} signals")
        except Exception:
            pass

    return {"ok": True, "lane": lane, "store": store_lane, "signals": len(items),
            "detected": len(detected), "promoted": len(promoted),
            "topics": [p["slug"] for p in promoted], "dry_run": dry_run,
            "sample": promoted[:8]}


def _selftest() -> int:
    r = curate("hustle", dry_run=True)
    ok = r.get("ok") and r["signals"] >= 0
    print(json.dumps(r, indent=2)[:1500])
    print("SELFTEST", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="autonomous topic-RAG curator")
    ap.add_argument("--lane", choices=["work", "hustle", "stack"], help="which lane to curate")
    ap.add_argument("--all", action="store_true", help="curate work + hustle (hustle incl. stack)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        raise SystemExit(_selftest())
    if a.all:
        for ln in ("work", "hustle"):
            print(json.dumps(curate(ln, dry_run=a.dry_run)))
    elif a.lane:
        print(json.dumps(curate(a.lane, dry_run=a.dry_run), indent=2))
    else:
        ap.print_help()
