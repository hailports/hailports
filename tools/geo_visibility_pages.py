#!/usr/bin/env python3
"""Generate substantive, indexable "Is <Business> Visible to AI?" proof pages for REAL local
businesses, each scored by the real core.geo_visibility_probe (Ollama-first, $0) and served live
at https://scannerapp.dev/v/<slug> by products/self_serve/app.py.

Per page (all distinct, real data):
  - real A-F AI-visibility grade vs the competitors an LLM actually names for "<category> in <city>"
  - the real competitor leaderboard (who AI cites instead)
  - 2 real verbatim AI answer excerpts (screenshot-grade evidence)
  - the specific 1-line fix + a business-specific llms.txt preview (core.geo_fix_kit)
  - $39 CTA to the live Stripe checkout (/ai-visibility/buy)

Markets (category, city) are scored ONCE and cached, so a whole town costs one set of LLM calls.

  python3 -m tools.geo_visibility_pages --target 320 --per-market 7
  python3 -m tools.geo_visibility_pages --check        # report counts, no LLM
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

LEADS = ROOT / "data" / "hustle" / "local_biz_leads.jsonl"
OUT_DIR = ROOT / "data" / "hustle" / "geo_pages"
ENT_BLOCK = ROOT / "data" / "hustle" / "enterprise_block.txt"

# Genuine local-SMB categories only (no generic "company"); excludes enterprise/midmarket by design.
LOCAL_CATS = {
    "restaurant", "fast_food", "cafe", "bar", "clothes", "hairdresser", "beauty",
    "fitness_centre", "dentist", "bakery", "car_repair", "clinic", "jewelry", "bicycle",
    "optician", "florist", "car_wash", "veterinary", "pharmacy", "butcher", "spa",
    "tattoo", "nail", "barber", "pet", "dry_cleaning", "plumber", "electrician",
    "locksmith", "roofer", "hvac", "landscaping", "furniture", "estate_agent", "insurance",
}

# Human-readable category label for prompts/display.
CAT_LABEL = {
    "fast_food": "fast food restaurant", "car_repair": "auto repair shop",
    "fitness_centre": "gym", "estate_agent": "real estate agent",
    "dry_cleaning": "dry cleaner", "hairdresser": "hair salon",
    "optician": "optician", "veterinary": "veterinarian",
}

# National chains / franchises — real but not "local SMB" proof targets. Dropped.
CHAINS = {
    "taco bell", "popeyes", "mcdonald's", "mcdonalds", "starbucks", "subway", "wendy's",
    "wendys", "burger king", "kfc", "chipotle", "domino's", "dominos", "pizza hut",
    "dunkin", "dunkin'", "chick-fil-a", "panera", "panera bread", "arby's", "arbys",
    "sonic", "five guys", "jimmy john's", "jimmy johns", "panda express", "in-n-out",
    "whataburger", "dairy queen", "wingstop", "raising cane's", "jack in the box",
    "carl's jr", "hardee's", "little caesars", "papa john's", "papa johns", "qdoba",
    "supercuts", "great clips", "jiffy lube", "midas", "meineke", "planet fitness",
    "anytime fitness", "la fitness", "orangetheory", "cvs", "walgreens", "rite aid",
    "petco", "petsmart", "sephora", "ulta", "gnc", "h&r block", "7-eleven",
}

_BAD_NAME_BITS = ("best ", "top ", " in ", "near me", "services in", "directory", "reviews",
                  " | ", " - ", "www.", ".com", "http", "?", ":", "(", "/")

# Doorway-penalty safety: only the first N pages per (category, city) market stay indexable;
# the rest (and any page with no real scan data) are flagged noindex so a wall of near-identical
# market pages can't drag down the domain shared with the live Stripe checkout.
NOINDEX_AFTER = int(os.environ.get("GEO_NOINDEX_AFTER", "3"))


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")[:90]


def _load_block() -> set[str]:
    out: set[str] = set()
    try:
        for line in ENT_BLOCK.read_text().splitlines():
            line = line.strip().lower()
            if line and not line.startswith("#"):
                out.add(line)
    except Exception:
        pass
    return out


def _readable_name(t: str) -> bool:
    t = (t or "").strip()
    if len(t) < 3 or len(t) > 48:
        return False
    if sum(c.isascii() for c in t) / max(1, len(t)) < 0.9:
        return False
    low = t.lower()
    if low in CHAINS:
        return False
    if any(b in low for b in _BAD_NAME_BITS):
        return False
    if "," in t:
        return False
    if re.match(r"^\d", t):
        return False
    toks = low.split()
    if toks and toks[0] in ("the", "a") and len(toks) > 6:
        return False
    return True


def _city_display(city: str) -> str:
    parts = (city or "").replace("_", " ").split()
    if not parts:
        return ""
    if len(parts[-1]) == 2 and parts[-1].isalpha():
        return " ".join(p.title() for p in parts[:-1]) + ", " + parts[-1].upper()
    return " ".join(p.title() for p in parts)


def _cat_label(cat: str) -> str:
    return CAT_LABEL.get(cat, cat.replace("_", " "))


def load_kept() -> list[dict]:
    block = _load_block()
    kept: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for line in LEADS.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        name = (d.get("name") or "").strip()
        cat = (d.get("category") or "").strip()
        city = (d.get("city") or "").strip()
        web = (d.get("website") or "").lower()
        if not (name and cat and city):
            continue
        if cat not in LOCAL_CATS:
            continue
        if not _readable_name(name):
            continue
        host = urlparse(web).netloc.replace("www.", "") if web else ""
        if host.endswith(".gov") or host.endswith(".mil") or host in block:
            continue
        key = (name.lower(), city.lower())
        if key in seen:
            continue
        seen.add(key)
        kept.append({"name": name, "category": cat, "city": city,
                     "city_display": _city_display(city), "website": d.get("website") or ""})
    return kept


def select(kept: list[dict], target: int, per_market: int) -> list[dict]:
    """Diverse selection: round-robin across categories, cap per market — avoids a wall of
    near-identical restaurant pages (doorway-penalty mitigation)."""
    by_market: dict[tuple[str, str], list[dict]] = collections.defaultdict(list)
    for b in kept:
        by_market[(b["category"], b["city"])].append(b)
    # markets grouped by category so we can round-robin categories for variety
    by_cat: dict[str, list[tuple[str, str]]] = collections.defaultdict(list)
    for mk in sorted(by_market, key=lambda m: -len(by_market[m])):
        by_cat[mk[0]].append(mk)
    cats = sorted(by_cat, key=lambda c: -sum(len(by_market[m]) for m in by_cat[c]))
    chosen: list[dict] = []
    ci = 0
    exhausted: set[str] = set()
    while len(chosen) < target and len(exhausted) < len(cats):
        cat = cats[ci % len(cats)]
        ci += 1
        if cat in exhausted:
            continue
        mks = by_cat[cat]
        if not mks:
            exhausted.add(cat)
            continue
        mk = mks.pop(0)
        bucket = by_market[mk][:per_market]
        chosen.extend(bucket)
        if not mks:
            exhausted.add(cat)
    return chosen[:target]


def _evidence(scan: dict, slug: str, k: int = 2) -> list[str]:
    answers = [a for a in scan.get("_market", {}).get("answers", []) if (a.get("answer") or "").strip()]
    if not answers:
        return []
    off = int(hashlib.md5(slug.encode()).hexdigest(), 16) % len(answers)
    rotated = answers[off:] + answers[:off]
    out = []
    for a in rotated[:k]:
        prompt = (a.get("prompt") or "").strip()
        txt = re.sub(r"\s+", " ", (a.get("answer") or "").strip())[:340]
        if len(a.get("answer", "")) > 340:
            txt += "…"
        out.append(f"Asked “{prompt}” the AI answered: {txt}")
    return out


def _specific_fix(scan: dict) -> str:
    biz = scan["business"]
    lb = scan.get("leaderboard") or []
    top = lb[0]["name"] if lb else "the competitors AI already names"
    if scan.get("appearances", 0) == 0:
        return (f"Publish a machine-readable llms.txt + LocalBusiness JSON-LD for {biz} so AI can "
                f"extract and cite it — the single reason {top} gets named for this search and {biz} "
                f"does not.")
    return (f"{biz} shows up inconsistently — a correct llms.txt + JSON-LD locks in citation so AI "
            f"names {biz} every time, the way it already names {top}.")


_GRADE_GAP = {"F": 5, "D": 4, "C": 3, "B": 2, "A": 0}


def _keep_rank(p: dict) -> tuple:
    """Sort key (reverse) deciding which pages in a market stay indexable: keep the richest —
    most named rivals (more unique content + a stronger 'AI names them, not you' story) and the
    biggest visibility gap to sell. Slug is a stable deterministic tiebreak."""
    g = str(p.get("grade", "")).upper()
    return (len(p.get("leaderboard") or []), _GRADE_GAP.get(g, 1), str(p.get("slug", "")))


PUBLIC_BASE = "https://scannerapp.dev"


def _embed_fields(slug: str, business: str, grade, score) -> dict:
    """Materialize the shareable badge URL + copy-paste embed snippet for a page (reuses the
    badge generator). White-hat backlink flywheel: the snippet is a do-follow anchor to the
    page, shown only because the business chose to display its real grade."""
    from tools.embed_badge_gen import badge_url, embed_snippet
    return {
        "badge_url": badge_url(slug, PUBLIC_BASE),
        "embed_url": f"{PUBLIC_BASE}/v/{slug}/badge",
        "embed_html": embed_snippet(slug, business, PUBLIC_BASE, grade=grade, score=score),
    }


def backfill_embeds() -> int:
    """Add badge_url/embed_url/embed_html to every page JSON on disk (idempotent, $0, no LLM).
    Only rewrites files whose embed snippet actually changed."""
    n = 0
    for f in sorted(OUT_DIR.glob("*.json")):
        if f.stem == "_index":
            continue
        try:
            p = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        slug = p.get("slug") or f.stem
        emb = _embed_fields(slug, p.get("business", ""), p.get("grade"), p.get("visibility_score"))
        if all(p.get(k) == v for k, v in emb.items()):
            continue
        p.update(emb)
        try:
            f.write_text(json.dumps(p, ensure_ascii=False), encoding="utf-8")
            n += 1
        except Exception:
            pass
    return n


def reindex_from_glob() -> int:
    """Rebuild _index.json from EVERY page JSON on disk AND globally reconcile the noindex flags.

    Orphan-pages fix: generate() used to write _index.json from only the pages it wrote in that
    single run, so a small run silently dropped every previously-generated page out of the /v
    index. Sourcing the index from the directory glob makes ALL pages discoverable.

    Doorway-density fix: generate()'s per-market cap is per-RUN (market_rank resets each run), so
    across many runs a (category, city) market can accumulate dozens of near-identical indexable
    pages on the domain shared with the live Stripe checkout — a scaled-content/doorway signal.
    Here we re-derive noindex over the WHOLE corpus: only the NOINDEX_AFTER most substantive pages
    per market stay indexable; grade-A (already cited → thin, nothing to sell) and no-signal pages
    are always noindexed. The flag is written back into each page JSON, so the app's /v index +
    sitemap and tools/indexnow_submit (all of which read p['noindex']) stay consistent. Idempotent:
    only files whose flag actually changes are rewritten."""
    cap = NOINDEX_AFTER
    pages: list[tuple] = []
    seen: set[str] = set()
    for f in sorted(OUT_DIR.glob("*.json")):
        if f.stem == "_index":
            continue
        try:
            p = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        slug = p.get("slug") or f.stem
        if slug in seen:
            continue
        seen.add(slug)
        pages.append((f, p))

    by_market: dict[tuple, list] = {}
    for f, p in pages:
        by_market.setdefault((p.get("category", ""), p.get("city", "")), []).append((f, p))
    for items in by_market.values():
        items.sort(key=lambda fp: _keep_rank(fp[1]), reverse=True)
        kept = 0
        for f, p in items:
            g = str(p.get("grade", "")).upper()
            thin = p.get("appearances", 0) == 0 and not p.get("leaderboard")
            if g == "A" or thin:
                ni = True
            elif kept < cap:
                ni = False
                kept += 1
            else:
                ni = True
            if bool(p.get("noindex")) != ni:
                p["noindex"] = ni
                try:
                    f.write_text(json.dumps(p, ensure_ascii=False), encoding="utf-8")
                except Exception:
                    pass

    rows = [{"slug": p.get("slug") or f.stem, "business": p.get("business", ""),
             "city": p.get("city", ""), "category": p.get("category", ""),
             "grade": p.get("grade", ""), "noindex": bool(p.get("noindex"))}
            for f, p in pages]
    (OUT_DIR / "_index.json").write_text(json.dumps(rows, ensure_ascii=False, indent=0),
                                         encoding="utf-8")
    return len(rows)


def generate(target: int, per_market: int, refresh: bool = False) -> dict:
    from core.geo_visibility_probe import score_business
    from core.geo_fix_kit import build_llms_txt

    kept = load_kept()
    chosen = select(kept, target, per_market)
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    market_cache: set[tuple[str, str]] = set()
    market_rank: collections.Counter = collections.Counter()
    written = noindexed = 0
    for i, b in enumerate(chosen, 1):
        name, cat, city = b["name"], b["category"], b["city"]
        city_disp = b["city_display"] or city.title()
        cat_label = _cat_label(cat)
        slug = _slug(f"{name}-{city}-{cat}")
        try:
            scan = score_business(name, city_disp, cat_label, refresh=refresh)
        except Exception as e:
            print(f"[skip] {name} / {city_disp}: {type(e).__name__}: {e}", flush=True)
            continue
        market_cache.add((cat, city))
        mr = market_rank[(cat, city)]
        market_rank[(cat, city)] += 1
        # Thin = no real signal at all (never named AND no competitor leaderboard) -> noindex.
        # Past the per-market cap -> noindex (doorway-density safety on the shared domain).
        thin = scan.get("appearances", 0) == 0 and not scan.get("leaderboard")
        noindex = bool(mr >= NOINDEX_AFTER or thin)
        llms_preview = build_llms_txt(name, city_disp, cat_label,
                                      domain=urlparse(b["website"]).netloc, watermark=True)
        page = {
            "slug": slug,
            "business": name,
            "city": city_disp,
            "category": cat,
            "category_label": cat_label,
            "grade": scan["grade"],
            "visibility_score": scan["visibility_score"],
            "appearances": scan["appearances"],
            "prompts_run": scan["prompts_run"],
            "avg_rank": scan["avg_rank"],
            "leaderboard": scan.get("leaderboard", []),
            "gaps": scan.get("gaps", []),
            "specific_fix": _specific_fix(scan),
            "llms_preview": llms_preview,
            "evidence": _evidence(scan, slug),
            "model": scan.get("model"),
            "sources": scan.get("sources"),
            "backend": scan.get("backend"),
            "is_simulation": scan.get("is_simulation", True),
            "market_rank": mr + 1,
            "noindex": noindex,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
        page.update(_embed_fields(slug, name, scan["grade"], scan["visibility_score"]))
        (OUT_DIR / f"{slug}.json").write_text(json.dumps(page, ensure_ascii=False), encoding="utf-8")
        written += 1
        noindexed += int(noindex)
        if i % 20 == 0:
            print(f"[{i}/{len(chosen)}] markets={len(market_cache)} pages={written} noindex={noindexed}", flush=True)

    total = reindex_from_glob()
    try:
        idx = json.loads((OUT_DIR / "_index.json").read_text())
        nindex = sum(1 for r in idx if r.get("noindex"))
    except Exception:
        nindex = noindexed
    summary = {"pages": total, "indexable": total - nindex, "noindex": nindex,
               "written_this_run": written, "markets": len(market_cache),
               "out_dir": str(OUT_DIR), "generated_at": datetime.now(timezone.utc).isoformat()}
    print("SUMMARY:", json.dumps(summary))
    return summary


def check() -> int:
    kept = load_kept()
    by_market = collections.Counter((b["category"], b["city"]) for b in kept)
    by_cat = collections.Counter(b["category"] for b in kept)
    print(f"kept={len(kept)} markets={len(by_market)}")
    print("top cats:", by_cat.most_common(12))
    existing = list(OUT_DIR.glob("*.json")) if OUT_DIR.exists() else []
    existing = [p for p in existing if p.name != "_index.json"]
    print(f"existing pages on disk: {len(existing)}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", type=int, default=320)
    ap.add_argument("--per-market", type=int, default=7)
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--reindex", action="store_true",
                    help="rebuild _index.json from all page JSON on disk (no LLM) and exit")
    ap.add_argument("--embeds", action="store_true",
                    help="backfill badge/embed snippet into all page JSON (no LLM) and exit")
    args = ap.parse_args(argv)
    if args.check:
        return check()
    if args.embeds:
        n = backfill_embeds()
        print(f"backfilled embed badge snippet into {n} pages -> {OUT_DIR}")
        return 0
    if args.reindex:
        n = reindex_from_glob()
        print(f"reindexed {n} pages from glob -> {OUT_DIR / '_index.json'}")
        return 0
    generate(args.target, args.per_market, refresh=args.refresh)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
